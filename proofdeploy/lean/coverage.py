"""Lean step 2, IMPACT: which HTTP requests execute the changed lines.

The app server runs under coverage.py with one dynamic context per request.
Nothing about the app changes: not its start command, not its files. A tool
directory (``/pd-home/.pd-tools/pdcov`` in the sandbox) holds a coveragerc, a
startup module and a copy of the pinned ``coverage`` package; a ``pdcov.pth``
file in the app interpreter's site-packages plus ``COVERAGE_PROCESS_START``
make the interpreter start measuring by itself.

Host/sandbox layout of the tool directory::

    <dir>/coveragerc        [run] parallel, sigterm, data_file, source, relative_files
    <dir>/pdcov_startup.py  process_startup() + context-follower thread
    <dir>/lib/coverage/     copy of the pinned coverage package
    <dir>/data/             parallel ``.coverage.*`` data files
    <dir>/context           control file (host writes the request id)
    <dir>/context.ack       acknowledgement (listener writes the same content)

Pieces:
  * ``ChangedLines.from_diff``       new-side added / old-side removed lines
  * ``install_pdcov`` / ``install_pth``  the injection
  * ``CoverageSession``              set_context(request_id), stop_and_collect()
  * ``impact`` / ``unexercised``     join changed lines with per-request coverage
  * ``write_django_settings_overlay``  settings module overlay helper
"""

from __future__ import annotations

import ast
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import coverage
from coverage.data import CoverageData, combine_parallel_data

from proofdeploy.lean import hostfs

# Default location of the tool directory inside the sandbox (userns-mapped
# home of the app user); host-side runs pass their own directory.
SANDBOX_TOOL_DIR = "/pd-home/.pd-tools/pdcov"

CONTEXT_FILE = "context"
ACK_FILE = "context.ack"
FLUSH_FILE = "flush"
FLUSH_ACK_FILE = "flush.ack"
ACK_TIMEOUT_SECONDS = 2.0
_POLL_SECONDS = 0.005
_STARTUP_SOURCE = Path(__file__).with_name("pdcov_startup.py")

# {context: {path: {line, ...}}}; the "" context is code run outside any
# request (import time, startup).
Coverage = dict[str, dict[str, set[int]]]


class DiffParseError(ValueError):
    """The diff cannot be read safely; callers must treat the run as failed."""


class CoverageHarnessError(RuntimeError):
    """The coverage injection or collection did not work."""


# --------------------------------------------------------------------------
# 1. Changed lines from a unified diff
# --------------------------------------------------------------------------

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_QUOTED_MSG = (
    "C-quoted path in diff ({line!r}): git quotes paths with special or "
    "non-ASCII characters; run git with -c core.quotepath=off or rename the "
    "file. Failing closed rather than guessing the path."
)


def split_diff_lines(text: str) -> list[str]:
    """Split diff (or git output) text into lines on ``\\n`` ONLY.

    ``str.splitlines()`` also breaks on ``\\x0b \\x0c \\x1c-\\x1e \\x85`` and U+2028/2029, so a
    source line containing one of them (legal in a file) would yield extra
    "lines" in a hunk and break the header counts (issue-register R6). A
    trailing ``\\r`` stays part of the line: it is file content.
    """
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def split_diff_lines_keepends(text: str) -> list[str]:
    """Like :func:`split_diff_lines`, each line keeping its ``\\n`` (the last may lack one)."""
    return re.findall(r"[^\n]*\n|[^\n]+", text)


def _clean_path(raw: str, line: str) -> str | None:
    """Path from a ``---``/``+++``/``rename`` line; None for /dev/null."""
    raw = raw.split("\t", 1)[0].rstrip("\r\n")
    if raw.startswith('"'):
        raise DiffParseError(_QUOTED_MSG.format(line=line))
    if raw == "/dev/null":
        return None
    if raw.startswith(("a/", "b/")):
        return raw[2:]
    return raw


class ChangedLines(dict[str, dict[str, set[int]]]):
    """``{path: {"added": {new-side line numbers}, "removed": {old-side line numbers}}}``.

    A changed line shows up as removed (old numbering) and added (new
    numbering). Paths are repo-relative, new-side (old-side for deleted
    files). ``renames`` maps new path -> old path for renamed files so
    old-side lookups use the old name; ``binary_files`` lists paths git
    reported as binary (no line information exists for them).
    """

    renames: dict[str, str]
    binary_files: set[str]

    def __init__(self) -> None:
        super().__init__()
        self.renames = {}
        self.binary_files = set()

    @classmethod
    def from_diff(cls, diff_text: str) -> ChangedLines:
        out = cls()
        old_path: str | None = None
        new_path: str | None = None
        have_file = False
        binary = False
        saw_plus = False
        # hunk state
        old_ln = new_ln = old_rem = new_rem = 0
        in_hunk = False
        entry: dict[str, set[int]] | None = None

        def finish_file() -> None:
            nonlocal have_file, binary, saw_plus, old_path, new_path, entry
            if have_file:
                path = new_path if new_path is not None else old_path
                if path is not None:
                    if binary:
                        out.binary_files.add(path)
                    else:
                        out.setdefault(path, {"added": set(), "removed": set()})
                        if new_path is not None and old_path is not None and new_path != old_path:
                            out.renames[new_path] = old_path
            have_file = False
            binary = False
            saw_plus = False
            old_path = new_path = None
            entry = None

        def cur_entry() -> dict[str, set[int]]:
            nonlocal entry
            path = new_path if new_path is not None else old_path
            if path is None:
                raise DiffParseError("hunk without file paths")
            if entry is None:
                entry = out.setdefault(path, {"added": set(), "removed": set()})
            return entry

        in_binary_patch = False
        for line in split_diff_lines(diff_text):
            if in_binary_patch and not line.startswith("diff --git "):
                continue  # base85 payload of a ``GIT binary patch``: no line information
            in_binary_patch = False
            if in_hunk:
                if line.startswith("\\"):  # "\ No newline at end of file"
                    continue
                if old_rem > 0 or new_rem > 0:
                    tag = line[:1]
                    if tag == "-" and old_rem > 0:
                        cur_entry()["removed"].add(old_ln)
                        old_ln += 1
                        old_rem -= 1
                        continue
                    if tag == "+" and new_rem > 0:
                        cur_entry()["added"].add(new_ln)
                        new_ln += 1
                        new_rem -= 1
                        continue
                    if (tag == " " or line == "") and old_rem > 0 and new_rem > 0:
                        old_ln += 1
                        new_ln += 1
                        old_rem -= 1
                        new_rem -= 1
                        continue
                    raise DiffParseError(f"hunk line does not match header counts: {line!r}")
                in_hunk = False  # counts exhausted; fall through to headers
            if line.startswith("diff --git "):
                finish_file()
                if '"' in line:
                    raise DiffParseError(_QUOTED_MSG.format(line=line))
                have_file = True
                rest = line[len("diff --git ") :]
                # Unambiguous only when both sides are the same path.
                n = (len(rest) - 5) // 2
                same = rest[2 : 2 + n]
                if len(rest) % 2 == 1 and n > 0 and rest == f"a/{same} b/{same}":
                    old_path = new_path = same
                continue
            if line.startswith("rename from "):
                have_file = True
                old_path = _clean_path(line[len("rename from ") :], line)
                continue
            if line.startswith("rename to "):
                have_file = True
                new_path = _clean_path(line[len("rename to ") :], line)
                continue
            if line.startswith(("Binary files ", "GIT binary patch")):
                have_file = True
                binary = True
                in_binary_patch = line.startswith("GIT binary patch")
                continue
            if line.startswith("--- "):
                if saw_plus:  # plain (non-git) diff: a new file section begins
                    finish_file()
                have_file = True
                old_path = _clean_path(line[4:], line)
                continue
            if line.startswith("+++ "):
                have_file = True
                saw_plus = True
                new_path = _clean_path(line[4:], line)
                continue
            if line.startswith("@@@"):
                raise DiffParseError("combined (merge) diffs are not supported")
            m = _HUNK_RE.match(line)
            if m:
                old_ln = int(m.group(1))
                old_rem = int(m.group(2)) if m.group(2) is not None else 1
                new_ln = int(m.group(3))
                new_rem = int(m.group(4)) if m.group(4) is not None else 1
                in_hunk = True
                continue
            # index/mode/similarity/new file/deleted file/other lines: ignored
        finish_file()
        return out


# --------------------------------------------------------------------------
# 2. Injection
# --------------------------------------------------------------------------


def installed_coverage_version() -> str:
    return str(coverage.__version__)


def _interpreter_tag(python: str | Path) -> tuple[int, int, str]:
    """(major, minor, implementation) of the app interpreter."""
    code = "import sys;print(sys.version_info[0],sys.version_info[1],sys.implementation.name)"
    proc = subprocess.run(
        [str(python), "-c", code], capture_output=True, text=True, timeout=30, check=False
    )
    if proc.returncode != 0:
        raise CoverageHarnessError(f"cannot run app interpreter {python}: {proc.stderr[-300:]}")
    major, minor, impl = proc.stdout.split()
    return int(major), int(minor), impl


def app_site_packages(python: str | Path) -> Path:
    """The purelib directory of the app interpreter (where ``pdcov.pth`` goes)."""
    proc = subprocess.run(
        [str(python), "-c", "import sysconfig;print(sysconfig.get_paths()['purelib'])"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        raise CoverageHarnessError(f"cannot locate site-packages of {python}: {proc.stderr[-300:]}")
    return Path(proc.stdout.strip())


def check_interpreter_compatible(python: str | Path) -> str:
    """Fail closed unless the app interpreter can load the copied coverage package.

    The copy of the host's installed ``coverage`` package contains a compiled
    tracer (``tracer.cpython-3XY-*.so``) bound to the host's CPython minor
    version. Same major.minor and implementation as the host is required;
    otherwise ship a wheel for the app's Python instead (see report/README).
    Returns a one-line description for evidence.
    """
    major, minor, impl = _interpreter_tag(python)
    host = (sys.version_info[0], sys.version_info[1], sys.implementation.name)
    if (major, minor, impl) != host:
        raise CoverageHarnessError(
            f"app interpreter is {impl} {major}.{minor} but the coverage copy comes "
            f"from host {host[2]} {host[0]}.{host[1]}; install a coverage wheel for the "
            f"app's Python into <dir>/lib instead of copying"
        )
    return f"coverage {installed_coverage_version()} for {impl} {major}.{minor}"


@dataclass(frozen=True)
class PdCov:
    """Handle on an installed tool directory."""

    tool_dir: Path

    @property
    def rcfile(self) -> Path:
        return self.tool_dir / "coveragerc"

    @property
    def data_dir(self) -> Path:
        return self.tool_dir / "data"

    @property
    def lib_dir(self) -> Path:
        return self.tool_dir / "lib"

    @property
    def context_file(self) -> Path:
        return self.tool_dir / CONTEXT_FILE

    @property
    def ack_file(self) -> Path:
        return self.tool_dir / ACK_FILE

    @property
    def flush_file(self) -> Path:
        return self.tool_dir / FLUSH_FILE

    @property
    def flush_ack_file(self) -> Path:
        return self.tool_dir / FLUSH_ACK_FILE

    def env(self) -> dict[str, str]:
        """Environment additions for the app process (and nothing else)."""
        return {"COVERAGE_PROCESS_START": str(self.rcfile)}

    def pth_line(self) -> str:
        return (
            f"import sys; sys.path.append({str(self.lib_dir)!r}); "
            f"sys.path.append({str(self.tool_dir)!r}); import pdcov_startup\n"
        )


# Variables the app may inherit from the host. Everything else is dropped:
# the host (e.g. pytest-cov in CI) sets COVERAGE_FILE / COV_CORE_* which would
# override the rcfile's data_file (COVERAGE_FILE wins over config) and send
# the app's data somewhere the harness never looks.
ENV_ALLOW = ("PATH", "HOME", "USER", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "TZ")


def clean_app_env(
    pd: PdCov,
    extra: Mapping[str, str] | None = None,
    *,
    base: Mapping[str, str] | None = None,
    measured: bool = True,
) -> dict[str, str]:
    """Environment for the app: allow-list of ``base`` + ``extra`` (+ the coverage hook).

    ``measured=False`` omits ``COVERAGE_PROCESS_START`` (for migrate/seed steps).
    """
    src = os.environ if base is None else base
    env = {k: src[k] for k in ENV_ALLOW if k in src}
    env.update(extra or {})
    if measured:
        env.update(pd.env())
    return env


def render_coveragerc(tool_dir: Path, source_root: str | Path) -> str:
    return (
        "[run]\n"
        "parallel = true\n"
        "sigterm = true\n"
        f"data_file = {tool_dir / 'data' / '.coverage'}\n"
        f"source = {source_root}\n"
        "relative_files = true\n"
    )


def install_pdcov(
    tool_dir: str | Path,
    source_root: str | Path,
    *,
    coverage_package: Path | None = None,
) -> PdCov:
    """Create the tool directory: coveragerc, startup module, coverage copy.

    ``coverage_package`` defaults to the host's installed ``coverage`` package
    directory (a copy, so the app interpreter imports exactly the version the
    host later uses to read the data). Idempotent; clears old data/control files.
    """
    tool = Path(tool_dir)
    tool.mkdir(parents=True, exist_ok=True)
    pd = PdCov(tool)
    pd.data_dir.mkdir(exist_ok=True)
    pd.lib_dir.mkdir(exist_ok=True)
    pd.rcfile.write_text(render_coveragerc(tool, source_root), encoding="utf-8")
    shutil.copyfile(_STARTUP_SOURCE, tool / "pdcov_startup.py")
    src = coverage_package or Path(coverage.__file__).resolve().parent
    dest = pd.lib_dir / "coverage"
    if dest.exists():
        hostfs.remove_tree(dest)
    shutil.copytree(src, dest, ignore=shutil.ignore_patterns("__pycache__"))
    reset_runtime_state(pd)
    return pd


def reset_runtime_state(pd: PdCov) -> None:
    # The data files and the acks are written by the app (the userns-mapped uid in the sandbox).
    for f in pd.data_dir.glob(".coverage*"):
        hostfs.remove_tree(f)
    for f in (pd.context_file, pd.ack_file, pd.flush_file, pd.flush_ack_file):
        hostfs.remove_tree(f)


def install_pth(site_packages: str | Path, pd: PdCov) -> Path:
    """Place ``pdcov.pth`` into the app interpreter's site-packages."""
    pth = Path(site_packages) / "pdcov.pth"
    pth.write_text(pd.pth_line(), encoding="utf-8")
    return pth


# --------------------------------------------------------------------------
# 3. Host-side session
# --------------------------------------------------------------------------


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".host.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
    except PermissionError:  # the tool directory is not writable by the host user
        with tempfile.TemporaryDirectory() as scratch:
            staged = Path(scratch) / "staged"
            staged.write_text(text, encoding="utf-8")
            hostfs.copy_into(staged, tmp)
    hostfs.replace(tmp, path)


def _read_ack(path: Path) -> str:
    """Text of a control or ack file the app wrote (OSError when absent)."""
    return hostfs.read_bytes(path).decode("utf-8")


class CoverageSession:
    """Drive per-request contexts and collect the result."""

    def __init__(self, pd: PdCov, *, ack_timeout: float = ACK_TIMEOUT_SECONDS) -> None:
        self.pd = pd
        self.ack_timeout = ack_timeout
        self._flushes = 0

    def start(self) -> None:
        """Clear data and control files; call before starting the app."""
        reset_runtime_state(self.pd)

    def set_context(self, request_id: str) -> None:
        """Make the app attribute subsequent execution to ``request_id``.

        Blocks until the in-app listener acknowledges the same content;
        raises CoverageHarnessError after ``ack_timeout`` seconds.
        """
        if not request_id:
            raise ValueError("request_id must be non-empty")
        _write_atomic(self.pd.context_file, request_id)
        deadline = time.monotonic() + self.ack_timeout
        while True:
            try:
                if _read_ack(self.pd.ack_file) == request_id:
                    return
            except OSError:
                pass
            if time.monotonic() >= deadline:
                raise CoverageHarnessError(
                    f"coverage listener did not acknowledge context {request_id!r} "
                    f"within {self.ack_timeout}s (is the app running with pdcov injected?)"
                )
            time.sleep(_POLL_SECONDS)

    def flush(self) -> None:
        """Make the running app save the coverage collected so far (P3).

        Blocks until the in-app listener acknowledges; raises
        CoverageHarnessError after ``ack_timeout`` seconds. Measurement
        continues afterwards.
        """
        self._flushes += 1
        token = f"{os.getpid()}-{self._flushes}-{time.time_ns()}"
        _write_atomic(self.pd.flush_file, token)
        deadline = time.monotonic() + self.ack_timeout
        while True:
            try:
                if _read_ack(self.pd.flush_ack_file) == token:
                    return
            except OSError:
                pass
            if time.monotonic() >= deadline:
                raise CoverageHarnessError(
                    f"coverage listener did not acknowledge flush within "
                    f"{self.ack_timeout}s (is the app running with pdcov injected?)"
                )
            time.sleep(_POLL_SECONDS)

    def snapshot(self, *, root_prefix: str = "") -> Coverage:
        """Per-context coverage so far, read while the app keeps running."""
        self.flush()
        return collect_coverage(self.pd.data_dir, root_prefix=root_prefix)

    def stop_and_collect(
        self,
        proc: subprocess.Popen[Any] | None = None,
        *,
        stop_timeout: float = 15.0,
        root_prefix: str = "",
        log_path: str | Path | None = None,
    ) -> Coverage:
        """SIGTERM ``proc`` (if given), combine parallel data, return per-context lines.

        ``root_prefix`` is prepended to coverage paths (which are relative to
        the app source root) when the app root is a subdirectory of the repo
        that the diff paths are relative to.
        """
        if proc is not None and proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=stop_timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                raise CoverageHarnessError(
                    "app did not exit on SIGTERM; coverage data unsafe"
                ) from None
        try:
            return collect_coverage(self.pd.data_dir, root_prefix=root_prefix)
        except CoverageHarnessError as exc:
            raise CoverageHarnessError(f"{exc}{_diagnostics(proc, log_path)}") from None


def _diagnostics(proc: subprocess.Popen[Any] | None, log_path: str | Path | None) -> str:
    parts = []
    if proc is not None:
        parts.append(f"app exit code: {proc.poll()}")
    if log_path is not None:
        try:
            tail = Path(log_path).read_text(encoding="utf-8", errors="replace")[-2000:]
        except OSError as exc:
            tail = f"<unreadable: {exc}>"
        parts.append(f"app output (tail):\n{tail}")
    return ("\n" + "\n".join(parts)) if parts else ""


def collect_coverage(data_dir: str | Path, *, root_prefix: str = "") -> Coverage:
    data_dir = Path(data_dir)
    parts = sorted(data_dir.glob(".coverage.*"))
    if not parts:
        raise CoverageHarnessError(
            f"no coverage data files in {data_dir}: the app wrote none "
            f"(pdcov.pth missing, COVERAGE_PROCESS_START unset, COVERAGE_FILE inherited\n"
            f"and overriding data_file, or app killed without SIGTERM)"
        )
    # Data files written by the app can be unreadable for the host user (mapped uid): then
    # they are copied through hostfs into a scratch directory and combined there.
    with tempfile.TemporaryDirectory() as scratch:
        src_dir = data_dir
        if not all(_host_can_open(p) for p in parts):
            src_dir = Path(scratch)
            for p in parts:
                (src_dir / p.name).write_bytes(hostfs.read_bytes(p))
        data = CoverageData(basename=str(src_dir / ".coverage"))
        combine_parallel_data(data, data_paths=[str(src_dir)], strict=True, keep=True)
        data.read()
        return _contexts_to_coverage(data, root_prefix)


def _host_can_open(path: Path) -> bool:
    try:
        with path.open("rb"):
            return True
    except PermissionError:
        return False


def _contexts_to_coverage(data: CoverageData, root_prefix: str) -> Coverage:
    prefix = root_prefix.rstrip("/") + "/" if root_prefix else ""
    result: Coverage = {}
    for filename in data.measured_files():
        for lineno, contexts in data.contexts_by_lineno(filename).items():
            for ctx in contexts:
                result.setdefault(ctx, {}).setdefault(prefix + filename, set()).add(lineno)
    return result


# --------------------------------------------------------------------------
# 4. Impact and unexercised
# --------------------------------------------------------------------------


def _old_path(changed: ChangedLines, path: str) -> str:
    return changed.renames.get(path, path)


def _hits(cov: Coverage, ctx: str, path: str, lines: set[int]) -> set[int]:
    return cov.get(ctx, {}).get(path, set()) & lines


def impact(
    changed: ChangedLines, cov_old: Coverage, cov_new: Coverage
) -> dict[str, dict[str, list[str]]]:
    """Per request id: changed lines it executed, as sorted ``"path:line"`` strings.

    ``hits_added`` are added lines hit in the NEW-side run; ``hits_removed`` are
    removed lines hit in the OLD-side run (old path for renames). The "" context
    (outside any request) is not a request and is excluded.
    """
    ids = sorted((set(cov_old) | set(cov_new)) - {""})
    out: dict[str, dict[str, list[str]]] = {}
    for rid in ids:
        added: list[tuple[str, int]] = []
        removed: list[tuple[str, int]] = []
        for path, sides in changed.items():
            added += [(path, n) for n in _hits(cov_new, rid, path, sides["added"])]
            old = _old_path(changed, path)
            removed += [(path, n) for n in _hits(cov_old, rid, old, sides["removed"])]
        out[rid] = {
            "hits_added": [f"{p}:{n}" for p, n in sorted(added)],
            "hits_removed": [f"{p}:{n}" for p, n in sorted(removed)],
        }
    return out


def unexercised(
    changed: ChangedLines, cov_old: Coverage, cov_new: Coverage
) -> dict[str, dict[str, list[int]]]:
    """Changed lines that no request executed: ``{path: {"added": [...], "removed": [...]}}``.

    Sorted lists; paths with nothing unexercised are omitted. Removed lines are
    checked against the OLD-side coverage (old path for renames), added lines
    against the NEW-side coverage. The "" context does not count as a request.
    """
    out: dict[str, dict[str, list[int]]] = {}
    for path, sides in changed.items():
        old = _old_path(changed, path)
        seen_new = {ln for ctx, files in cov_new.items() if ctx for ln in files.get(path, ())}
        seen_old = {ln for ctx, files in cov_old.items() if ctx for ln in files.get(old, ())}
        miss_added = sorted(sides["added"] - seen_new)
        miss_removed = sorted(sides["removed"] - seen_old)
        if miss_added or miss_removed:
            out[path] = {"added": miss_added, "removed": miss_removed}
    return out


# --------------------------------------------------------------------------
# 5. Django settings overlay
# --------------------------------------------------------------------------

_DOTTED = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
OVERLAY_MODULE = "pd_settings_overlay"


def write_django_settings_overlay(
    directory: str | Path, base_module: str, overrides: dict[str, Any]
) -> str:
    """Write ``<directory>/pd_settings_overlay.py``; return its module name.

    The overlay does ``from <base_module> import *`` then assigns each
    override (for example ``CELERY_TASK_ALWAYS_EAGER=True``). Use the returned
    name as ``DJANGO_SETTINGS_MODULE`` with ``directory`` on ``PYTHONPATH``.
    Override values must be plain Python literals (checked by round trip).
    """
    if not _DOTTED.match(base_module):
        raise ValueError(f"base_module is not a dotted module name: {base_module!r}")
    lines = [
        "# Generated by proofdeploy.lean.coverage; do not edit.",
        f"from {base_module} import *  # noqa: F401,F403",
        "",
    ]
    for key, value in overrides.items():
        if not _IDENT.match(key):
            raise ValueError(f"override name is not an identifier: {key!r}")
        text = repr(value)
        try:
            ok = ast.literal_eval(text) == value
        except (ValueError, SyntaxError):
            ok = False
        if not ok:
            raise ValueError(f"override {key} is not a plain literal: {value!r}")
        lines.append(f"{key} = {text}")
    d = Path(directory)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{OVERLAY_MODULE}.py").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return OVERLAY_MODULE
