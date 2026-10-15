"""Target provisioner for the ProofDeploy runner (WAL-58).

Builds and starts a probe target from a `proofdeploy.yml` repo contract.
All failures are typed results, never crashes: a target that cannot build
or start yields INCONCLUSIVE (per the start-failure scoring rule), not an
exception escaping the runner.

The provisioner:
- detects the runtime and SELECTS an installed binary satisfying the
  declared range (`engines.node` / `packageManager` for Node,
  `requires-python` for Python); records the actual version used
  (unsatisfiable range = BUILD_FAILED);
- verifies the frozen lockfile, preferring the `packageManager` lockfile
  when several exist (mismatch = build failure); rejects unpinned
  requirements.txt;
- installs with the package manager matching the lockfile (Python pip
  goes into a fresh per-run venv, never the host);
- runs the contract's `build`, `migrate`, and `seed` steps in order;
- captures target stdout/stderr to a per-run log file;
- starts the target and waits for the readiness check (a `/path`
  readiness is joined to the assigned target URL).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import time
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class ProvisionStatus(Enum):
    """Typed outcome of provisioning a target."""

    READY = "ready"  # built, started, readiness check passed
    BUILD_FAILED = "build_failed"  # lockfile mismatch, install error, etc.
    START_FAILED = "start_failed"  # process exited or never became ready


@dataclass
class ProvisionResult:
    """The outcome of provisioning, with logs for the run record."""

    status: ProvisionStatus
    target_url: str | None = None  # set when READY
    logs: list[str] = field(default_factory=list)
    lockfile_sha256: str | None = None
    runtime: str | None = None
    runtime_version: str | None = None  # actual version used, from `--version`
    package_manager: str | None = None
    reason: str | None = None  # human-readable cause for BUILD_FAILED/START_FAILED
    target_log_path: str | None = None  # captured target stdout/stderr
    run_id: str | None = None  # per-run scratch dirs: workdir/venvs/<run_id>, workdir/runs/<run_id>
    # Sandbox image digest (recorded when provisioned in the sandbox).
    image_digest: str | None = None
    # The install plan this provision executed (WO-7), and its sha256.
    # Recorded in the evidence on every path that planned.
    install_plan: dict | None = None
    install_plan_sha256: str | None = None
    # Addendum 31: the commands that actually ran, per executed
    # step: {"phase", "executor", "argv_rendered" | "shell",
    # "exit_code"}. The plan holds placeholders and executors render
    # them differently (pip flags differ host vs sandbox), so the
    # record — not the plan — is what proves what ran.
    executed_steps: list[dict] = field(default_factory=list)
    # REG-A (WO-8.1): for a GENERATED Python lock, the generated lock
    # text and its sha256, read back from the checkout after the
    # resolve step ran: {"text": ..., "sha256": ...}. None for a
    # committed lock (whose own sha256 is lockfile_sha256) and when
    # generation never produced a file.
    generated_lock: dict | None = None
    # WO-8.1c: for a committed-manager plan (which builds the root
    # project during install), the build backend that actually
    # built it: {"name": ..., "version": ...}, read back from the
    # built wheel's Generator by the executors (provisioning's
    # build_backend_evidence). Evidence only — non-scoring. None
    # when the install never built the root project (the pip
    # paths) or nothing could be determined.
    build_backend: dict | None = None
    # Live process handle on READY. Caller-owned: the caller must terminate
    # it when done and call cleanup_run(run_id). Excluded from to_dict.
    proc: Any = field(default=None, repr=False, compare=False)

    @property
    def ready(self) -> bool:
        return self.status == ProvisionStatus.READY

    def to_dict(self) -> dict:
        return {
            "status": self.status.value,
            "target_url": self.target_url,
            "lockfile_sha256": self.lockfile_sha256,
            "runtime": self.runtime,
            "runtime_version": self.runtime_version,
            "image_digest": self.image_digest,
            "package_manager": self.package_manager,
            "reason": self.reason,
            "target_log_path": self.target_log_path,
            "run_id": self.run_id,
            "install_plan": self.install_plan,
            "install_plan_sha256": self.install_plan_sha256,
            "executed_steps": self.executed_steps,
            "generated_lock": self.generated_lock,
            "build_backend": self.build_backend,
            "logs": self.logs,
        }


# Lockfiles we recognize, in preference order per ecosystem.
LOCKFILES = {
    "node": (
        "package-lock.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "bun.lock",
        "bun.lockb",
    ),
    "python": ("uv.lock", "poetry.lock", "pdm.lock", "requirements.txt"),
}


# Error-line extraction for failed install output (Addendum
# 33/35): a strong marker line (``npm error``, ``ERR_PNPM_``,
# ``ELIFECYCLE``, ``gyp ERR!``) names WHY an install failed, where
# the bare step failure does not; a generic error line is next;
# if nothing matches, fall back to the LAST non-empty line of the
# output — an unmatched failure still gets its most concrete line
# into the provision reason instead of a bare "install failed".
# Defined here (WO-8.1b) so BOTH executors surface a build
# failure's first error line the same way; the orchestrator's
# sandbox path imports it from this module. The generic class
# covers pip's ``ERROR:``, Python's ``Error:`` (e.g. a traceback's
# final line) and the Rust-family tools' lowercase ``error:``
# (uv's lock check: "error: The lockfile at `uv.lock` needs to be
# updated..." — without it, that failure named uv's trailing
# hint line instead of its error).
_ERROR_MARKERS_STRONG = ("npm error", "ERR_PNPM_", "ELIFECYCLE", "gyp ERR!")
_ERROR_MARKERS_GENERIC = ("ERROR:", "Error:", "error:")


def _first_error_line(lines: list) -> str | None:
    """The first concrete error line in a list of log entries.

    Entries may be multi-line blobs; scan line by line. The
    ``$ podman ...`` command echo the sandbox logs before each
    run is NOT evidence (Addendum 38): it is skipped entirely — a
    marker inside the echoed command must not be named, and the
    echo must not serve as the fallback line."""
    last_nonempty: str | None = None
    generic: str | None = None
    for entry in lines:
        for line in str(entry).splitlines():
            stripped = line.strip()
            if stripped.startswith("$ podman"):
                continue
            if stripped:
                last_nonempty = stripped
            if any(marker in line for marker in _ERROR_MARKERS_STRONG):
                return stripped[:300]
            if generic is None and any(
                marker in line for marker in _ERROR_MARKERS_GENERIC
            ):
                generic = stripped
    if generic is not None:
        return generic[:300]
    return last_nonempty[:300] if last_nonempty is not None else None


# Plan 1.2(j) (D33, Master's option A on the reviewer's O9): a
# committed lockfile that does not match its manifest is a typed
# NOT-SUPPORTED class for every package manager — the same
# mechanism as D18's "no frozen lockfile", one class over. The
# repo's own install is never repaired, regenerated or
# date-resolved (rule 9; REG-D D1(c)).
#
# Classification reads ONLY the manager's own refusal, from the
# failed install step's own output — ProofDeploy never
# re-implements version matching. Each pattern below was verified
# against a REAL run of the pinned manager version on a drifted
# project (one dependency bumped in the manifest, lockfile
# unchanged) on 2026-10-15; the captures are committed as
# fixtures under tests/fixtures/drift/ and the typed reason
# embeds the first matching line verbatim:
# - npm 10.9.4, `npm ci`: "npm error code EUSAGE" + the
#   "can only install packages when your package.json and
#   package-lock.json ... are in sync" prose (and, for other
#   drift shapes, "Invalid: lock file's X does not satisfy Y" /
#   "Missing: ... from lock file").
# - pnpm 9.15.0, `pnpm install --frozen-lockfile`:
#   "ERR_PNPM_OUTDATED_LOCKFILE" (the stable error code).
# - yarn classic 1.22.22, `yarn install --frozen-lockfile`:
#   "error Your lockfile needs to be updated, but yarn was run
#   with `--frozen-lockfile`."
# - yarn berry 4.18.1, `yarn install --immutable`: code YN0028
#   with "The lockfile would have been modified by this install,
#   which is explicitly forbidden."
# - bun 1.4.3 (the plan's digest pin), `bun install
#   --frozen-lockfile`: "error: lockfile had changes, but
#   lockfile is frozen".
# - uv 0.13.0 (the bootstrap pin), `uv lock --check` exit 1:
#   "error: The lockfile at `uv.lock` needs to be updated, but
#   `--check` was provided."
# - pdm 2.29.2 (the bootstrap pin), `pdm lock --check` exit 1:
#   "WARNING: Lockfile doesn't satisfy the project requirements,
#   packages may be outdated" (the check prints no other line).
# - poetry 2.5.1 (the bootstrap pin), `poetry install --sync`
#   exit 1: "pyproject.toml changed significantly since
#   poetry.lock was last generated. ..."
# Patterns are tuples of substrings that must ALL appear on one
# output line; alternatives are tried per line in output order
# and the first matching line is the manager's refusal line.
# Anything else — registry/network failures (E502, ETIMEDOUT,
# ECONNRESET, ECONNREFUSED), peer-resolution failures
# (ERESOLVE), every other install error — matches nothing here
# and stays exactly the BUILD_FAILED it is today.
_LOCKFILE_DRIFT_PATTERNS: dict[str, tuple[tuple[str, ...], ...]] = {
    "npm": (
        (
            "can only install packages when your package.json "
            "and package-lock.json",
        ),
        ("npm error", "does not satisfy"),
        ("npm error", "Missing:", "from lock file"),
    ),
    "pnpm": (("ERR_PNPM_OUTDATED_LOCKFILE",),),
    "yarn": (
        ("Your lockfile needs to be updated",),
        ("YN0028", "lockfile would have been modified"),
    ),
    "bun": (("lockfile had changes, but lockfile is frozen",),),
    "uv": (("The lockfile at", "needs to be updated"),),
    "pdm": (("Lockfile doesn't satisfy the project requirements",),),
    "poetry": (("pyproject.toml changed significantly",),),
}


def _lockfile_drift_reason(manager: str | None, lines: list) -> str | None:
    """The typed not-supported reason when a failed install
    step's own output is the package manager's frozen-install
    refusal of a drifted lockfile (plan 1.2(j)); None otherwise.

    ``lines`` has the same shape as ``_first_error_line``'s input
    (log entries, possibly multi-line blobs); the ``$ podman``
    command echo is skipped for the same Addendum 38 reason.
    """
    if manager is None:
        return None
    alternatives = _LOCKFILE_DRIFT_PATTERNS.get(manager)
    if not alternatives:
        return None
    for entry in lines:
        for line in str(entry).splitlines():
            stripped = line.strip()
            if stripped.startswith("$ podman"):
                continue
            for alternative in alternatives:
                if all(part in stripped for part in alternative):
                    return (
                        "not supported: lockfile out of sync with "
                        f"its manifest ({manager}): {stripped[:300]}"
                    )
    return None


def _exit_code_from_reason(ok: bool, reason: str | None) -> int | None:
    """The exit code behind an _exec result (Addendum 31 recording).

    _exec's failure reasons are this module's own contract:
    "command failed with code N: ..." carries the process's code.
    Anything else (missing binary, timeout) means no process ran to
    completion: None.
    """
    if ok:
        return 0
    prefix = "command failed with code "
    if reason and reason.startswith(prefix):
        try:
            return int(reason[len(prefix):].split(":", 1)[0])
        except ValueError:
            return None
    return None


def _sha256_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def detect_runtime(snapshot_dir: Path) -> tuple[str, str | None]:
    """Detect the runtime and its declared version from repo manifests.

    Node: `engines.node` in package.json (the declared Node version range).
    Python: `requires-python` in pyproject.toml (`project` or
    `tool.poetry.dependencies`).

    Returns (runtime, version); version is None when not declared.
    Raises ValueError if no runtime can be determined.
    """
    pkg = snapshot_dir / "package.json"
    if pkg.is_file():
        version: str | None = None
        try:
            data = json.loads(pkg.read_text(encoding="utf-8"))
            engines = data.get("engines") or {}
            if isinstance(engines, dict):
                node_range = engines.get("node")
                if isinstance(node_range, str) and node_range.strip():
                    version = node_range.strip()
        except (json.JSONDecodeError, OSError):
            pass
        return "node", version
    pyproject = snapshot_dir / "pyproject.toml"
    if pyproject.is_file():
        version = _requires_python_from_toml(pyproject)
        return "python", version
    requirements = snapshot_dir / "requirements.txt"
    if requirements.is_file():
        return "python", None
    raise ValueError(
        f"cannot detect runtime in {snapshot_dir}: no package.json or pyproject.toml"
    )


def _requires_python_from_toml(pyproject: Path) -> str | None:
    """Extract `requires-python` from pyproject.toml without a TOML parser.

    Works on Python 3.10 (no stdlib tomllib). Handles PEP 621
    `[project] requires-python` and Poetry's `[tool.poetry.dependencies]`
    python entry. Returns the raw version specifier, or None.
    """
    try:
        text = pyproject.read_text(encoding="utf-8")
    except OSError:
        return None
    # PEP 621: requires-python = ">=3.11"
    m = re.search(
        r'^\s*requires-python\s*=\s*["\']([^"\']+)["\']', text, re.MULTILINE
    )
    if m:
        return m.group(1).strip() or None
    # Poetry: [tool.poetry.dependencies] ... python = ">=3.11,<3.13"
    m = re.search(
        r'^\s*python\s*=\s*["\']([^"\']+)["\']', text, re.MULTILINE
    )
    if m:
        val = m.group(1).strip()
        # Skip Poetry's "^3.11" caret syntax? No — return it raw; the
        # record just needs the declared specifier.
        return val or None
    return None


# --- version range matching (finding 2: select, don't just record) ---


def _parse_version(v: str) -> tuple[int, int, int]:
    """Parse a version string into a (major, minor, patch) tuple.

    Strips leading 'v', prerelease/build suffixes, and non-numeric tails.
    """
    v = v.strip().lstrip("vV")
    v = re.split(r"[-+]", v, maxsplit=1)[0]
    parts: list[int] = []
    for p in v.split("."):
        m = re.match(r"\d+", p)
        parts.append(int(m.group()) if m else 0)
    while len(parts) < 3:
        parts.append(0)
    return (parts[0], parts[1], parts[2])


def _eval_cmp(
    op: str, ver: tuple[int, int, int], ref: tuple[int, ...]
) -> bool:
    n = len(ref)
    if op == "==":
        return ver == ref  # type: ignore[comparison-overlap]
    if op == "!=":
        return ver != ref  # type: ignore[comparison-overlap]
    if op == ">=":
        return ver >= ref  # type: ignore[operator]
    if op == "<=":
        return ver <= ref  # type: ignore[operator]
    if op == ">":
        return ver > ref  # type: ignore[operator]
    if op == "<":
        return ver < ref  # type: ignore[operator]
    if op == "prefix==":
        return ver[:n] == ref
    if op == "prefix!=":
        return ver[:n] != ref
    raise ValueError(f"unknown comparator: {op}")


def _expand_caret(t: tuple[int, int, int]) -> list[tuple[str, tuple[int, ...]]]:
    """npm ^x.y.z semantics."""
    if t[0] != 0:
        return [(">=", t), ("<", (t[0] + 1, 0, 0))]
    if t[1] != 0:
        return [(">=", t), ("<", (0, t[1] + 1, 0))]
    return [(">=", t), ("<", (0, 0, t[2] + 1))]


def _expand_tilde(
    t: tuple[int, int, int], specified: int
) -> list[tuple[str, tuple[int, ...]]]:
    """npm ~x.y.z semantics (patch-level changes allowed)."""
    if specified >= 3:
        return [(">=", t), ("<", (t[0], t[1] + 1, 0))]
    if specified == 2:
        return [(">=", (t[0], t[1], 0)), ("<", (t[0] + 1, 0, 0))]
    return [(">=", (t[0], 0, 0)), ("<", (t[0] + 1, 0, 0))]


def _expand_compatible(
    t: tuple[int, int, int], specified: int
) -> list[tuple[str, tuple[int, ...]]]:
    """PEP 440 ~= semantics: ~= X.Y means >=X.Y, ==X.*; ~= X.Y.Z means
    >=X.Y.Z, ==X.Y.*."""
    if specified >= 3:
        return [(">=", t), ("prefix==", t[:2])]
    return [(">=", (t[0], t[1], 0)), ("prefix==", t[:1])]


def _parse_token(
    tok: str, ecosystem: str
) -> list[tuple[str, tuple[int, ...]]]:
    """Parse one range token into comparators."""
    if tok in ("*", "x", "X", "latest"):
        return []
    m = re.match(r"^(>=|<=|!=|==|~=|>|<|=|\^|~)?\s*(.+?)\s*$", tok)
    if not m:
        return []
    op, ver = m.group(1) or "", m.group(2)
    # Wildcards: 1.x, 1.2.x, 1.* — a bare token whose components
    # include "*" is the x form spelled with an asterisk. (The
    # op guard keeps PEP 440's ==x.y.* / !=x.y.* on their own
    # prefix path below.)
    low = ver.lower()
    if "x" in low or low == "*" or (op in ("", "=") and "*" in low.split(".")):
        nums = [p for p in re.split(r"[.]", low) if p not in ("x", "*")]
        t = _parse_version(".".join(nums) if nums else "0")
        if len(nums) <= 1:
            return [(">=", (t[0], 0, 0)), ("<", (t[0] + 1, 0, 0))]
        return [(">=", (t[0], t[1], 0)), ("<", (t[0], t[1] + 1, 0))]
    # PEP 440 == with .* suffix
    if op == "==" and ver.endswith(".*"):
        prefix = tuple(int(p) for p in ver[:-2].split(".") if p.isdigit())
        return [("prefix==", prefix)]
    if op == "!=" and ver.endswith(".*"):
        prefix = tuple(int(p) for p in ver[:-2].split(".") if p.isdigit())
        return [("prefix!=", prefix)]
    t = _parse_version(ver)
    specified = len([p for p in ver.split(".") if p.strip()])
    if op == "^":
        return _expand_caret(t)
    if op == "~" and ecosystem == "node":
        return _expand_tilde(t, specified)
    if op == "~=":
        return _expand_compatible(t, specified)
    if op in ("", "="):
        # npm: a bare partial version is a range over its
        # unspecified components — "22" means 22.x and "22.2"
        # means 22.2.x; only a full x.y.z is exact. (WO-8.4a:
        # reading "22" as ==22.0.0 refused Directus's declared
        # range against the pinned 22.23.3 image.) PEP 440 has
        # no bare specifier, so other ecosystems keep the exact
        # reading.
        if ecosystem == "node" and specified < 3:
            if specified <= 1:
                return [(">=", (t[0], 0, 0)), ("<", (t[0] + 1, 0, 0))]
            return [(">=", (t[0], t[1], 0)), ("<", (t[0], t[1] + 1, 0))]
        return [("==", t)]
    return [(op, t)]


def _parse_range(
    spec: str, ecosystem: str
) -> list[list[tuple[str, tuple[int, ...]]]]:
    """Parse a range spec into OR-groups of AND-comparators."""
    groups: list[list[tuple[str, tuple[int, ...]]]] = []
    for group_str in spec.split("||"):
        group_str = group_str.strip()
        if not group_str:
            continue
        # npm hyphen range: "1.2.3 - 2.3.4"
        if ecosystem == "node" and " - " in group_str:
            lo, hi = group_str.split(" - ", 1)
            groups.append(
                [(">=", _parse_version(lo)), ("<=", _parse_version(hi))]
            )
            continue
        comparators: list[tuple[str, tuple[int, ...]]] = []
        # npm and PEP 440 both allow whitespace between an operator
        # and its version (uptime-kuma declares ">= 26.2.0"): remove
        # it before tokenizing, or the bare operator becomes a token
        # of its own and the comparator is lost.
        group_str = re.sub(
            r"(>=|<=|!=|==|~=|>|<|=|\^|~)\s+", r"\1", group_str
        )
        for tok in re.split(r"[,\s]+", group_str):
            if tok:
                comparators.extend(_parse_token(tok, ecosystem))
        groups.append(comparators)
    return groups


def satisfies_range(version: str, range_spec: str, ecosystem: str = "node") -> bool:
    """True if `version` satisfies the declared range spec.

    Supports the common npm (`engines`) and PEP 440 (`requires-python`)
    range syntax: >=, <=, >, <, ==, !=, ^, ~, ~=, x-wildcards, bare
    partial versions (npm: "22" means 22.x), hyphen ranges, comma
    AND, || OR.
    """
    spec = (range_spec or "").strip()
    if not spec or spec in ("*", "x", "X", "latest"):
        return True
    ver = _parse_version(version)
    try:
        groups = _parse_range(spec, ecosystem)
    except Exception:
        return False
    if not groups:
        return True
    return any(
        all(_eval_cmp(op, ver, ref) for op, ref in group) for group in groups
    )


_NODE_BINARIES = ["node", "nodejs", "node24", "node22", "node20", "node18", "node16"]
_PYTHON_BINARIES = [
    "python3",
    "python3.12",
    "python3.11",
    "python3.10",
    "python3.9",
    "python3.8",
]


def _binary_version(binary: str, env: dict[str, str]) -> str | None:
    """Return `binary --version` output, or None if unavailable."""
    try:
        proc = subprocess.run(
            [binary, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
        )
    except (FileNotFoundError, OSError):
        return None
    if proc.returncode != 0:
        return None
    out = (proc.stdout or proc.stderr or "").strip()
    if not out:
        return None
    # "v24.20.0" -> "v24.20.0"; "Python 3.12.3" -> "3.12.3"
    first = out.split()[0]
    if first.lower() == "python" and len(out.split()) > 1:
        return out.split()[1]
    return first


def select_runtime_binary(
    runtime: str,
    declared_range: str | None,
    env: dict[str, str],
    logs: list[str],
) -> tuple[str | None, str | None, str | None]:
    """Pick an installed binary satisfying the declared range.

    Returns (binary, actual_version, failure_reason). failure_reason is
    None on success. With no declared range, uses the default binary
    without probing (the install step reports a missing binary).
    """
    default = "node" if runtime == "node" else "python3"
    if declared_range is None:
        ver = _binary_version(default, env)
        if ver is not None:
            logs.append(f"runtime binary: {default} ({ver}), no declared range")
        return default, ver, None
    candidates = _NODE_BINARIES if runtime == "node" else _PYTHON_BINARIES
    probed: list[str] = []
    for binary in candidates:
        ver = _binary_version(binary, env)
        if ver is None:
            continue
        probed.append(f"{binary}={ver}")
        if satisfies_range(ver, declared_range, runtime):
            logs.append(
                f"runtime binary: {binary} ({ver}) satisfies {declared_range!r}"
            )
            return binary, ver, None
    if not probed:
        return None, None, f"no {runtime} binary found on PATH"
    return (
        None,
        None,
        f"no installed {runtime} satisfies declared range {declared_range!r} "
        f"(found: {', '.join(probed)})",
    )


def read_package_manager(snapshot_dir: Path) -> tuple[str | None, str | None]:
    """Read the `packageManager` field: (name, version) or (None, None)."""
    pkg = snapshot_dir / "package.json"
    try:
        data = json.loads(pkg.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None, None
    pm = data.get("packageManager")
    if not isinstance(pm, str) or "@" not in pm:
        return None, None
    name, _, version = pm.partition("@")
    name, version = name.strip(), version.strip()
    if not name:
        return None, None
    return name, version or None


# packageManager name -> preferred lockfile(s)
_PM_LOCKFILES: dict[str, tuple[str, ...]] = {
    "npm": ("package-lock.json",),
    "yarn": ("yarn.lock",),
    "pnpm": ("pnpm-lock.yaml",),
    "bun": ("bun.lock", "bun.lockb"),
}


def find_lockfile(
    snapshot_dir: Path,
    runtime: str,
    preferred: list[str] | None = None,
) -> tuple[str, str] | None:
    """Find the frozen lockfile. Returns (lockfile_name, sha256) or None.

    When `preferred` names exist, they are tried first (finding 3: the
    lockfile matching `packageManager` wins over the hard-coded order).
    """
    names = list(LOCKFILES.get(runtime, ()))
    if preferred:
        names = [n for n in preferred if n in names] + [
            n for n in names if n not in preferred
        ]
    for name in names:
        p = snapshot_dir / name
        if p.is_file():
            return name, _sha256_file(p)
    return None


def verify_lockfile(snapshot_dir: Path, runtime: str) -> str:
    """Verify the frozen lockfile exists and return its sha256.

    Raises ValueError if no recognized lockfile is present.
    """
    found = find_lockfile(snapshot_dir, runtime)
    if found is None:
        raise ValueError(
            f"no frozen lockfile found in {snapshot_dir} for runtime {runtime!r}"
        )
    return found[1]


# REG-A completeness allowlist (WO-8.1, Addendum 46): the global
# pip options a requirements file may carry while still being, on
# its own, the complete lock. Everything else on an option line —
# an include, an editable, or an option not listed here — means
# the file alone does not pin the installed set.
_FROZEN_GLOBAL_OPTIONS = frozenset({
    "--index-url", "-i", "--extra-index-url", "--find-links", "-f",
    "--trusted-host", "--pre", "--prefer-binary", "--only-binary",
    "--no-binary",
})
_FROZEN_INCLUDE_OPTIONS = frozenset({
    "-r", "--requirement", "-c", "--constraint",
})
_FROZEN_EDITABLE_OPTIONS = frozenset({"-e", "--editable"})


def _split_option_token(head: str) -> tuple[str, str | None]:
    """Split an option token into (option, inline value).

    Handles ``--opt=value``, the short attached form (``-rbase.txt``),
    and a bare option whose value is the next token (None here).
    """
    if "=" in head:
        opt, _, value = head.partition("=")
        return opt, value or None
    if head.startswith("-") and not head.startswith("--") and len(head) > 2:
        return head[:2], head[2:]
    return head, None


def _logical_entries(lines: list[str]) -> list[tuple[int, str]]:
    """Join backslash-continued physical lines into logical entries.

    The pip-compile form puts each ``--hash=`` on its own
    continuation line; an entry is checked as a whole.
    """
    entries: list[tuple[int, str]] = []
    start: int | None = None
    buf: list[str] = []
    for lineno, raw in enumerate(lines, 1):
        line = raw.rstrip()
        if start is None:
            start = lineno
            buf = [line]
        else:
            buf.append(line)
        if line.endswith("\\"):
            continue
        entries.append((start, " ".join(buf)))
        start = None
        buf = []
    if buf and start is not None:
        entries.append((start, " ".join(buf)))
    return entries


def _strip_inline_comment(entry: str) -> str:
    """Remove an inline comment: whitespace followed by ``#``.

    A ``--hash`` appearing only inside a comment is not a hash
    (Addendum 46) — strip before looking for one.
    """
    return re.split(r"\s+#", entry, maxsplit=1)[0]


def _check_requirements_file(
    path: Path, root: Path, seen: frozenset[Path]
) -> str | None:
    """Check one requirements file; reason if it is not complete.

    ``root`` is the checkout root (the top-level file's directory):
    includes must resolve INSIDE it. ``seen`` guards include cycles.
    """
    try:
        display = path.relative_to(root).as_posix()
    except ValueError:
        display = path.name
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as e:
        return f"cannot read {display}: {e}"
    for lineno, entry in _logical_entries(lines):
        body = _strip_inline_comment(entry).strip()
        if not body or body.startswith("#"):
            continue
        tokens = body.split()
        head = tokens[0]
        if head.startswith("-"):
            opt, inline = _split_option_token(head)
            if opt in _FROZEN_GLOBAL_OPTIONS:
                continue
            if opt in _FROZEN_INCLUDE_OPTIONS:
                target = inline
                if target is None and len(tokens) > 1:
                    target = tokens[1]
                reason = _check_include(
                    target, path, root, seen, display, lineno
                )
                if reason is not None:
                    return reason
                continue
            if opt in _FROZEN_EDITABLE_OPTIONS:
                return (
                    f"{display} line {lineno} is an editable install "
                    f"({head}); an editable pins no artifact "
                    f"(not a complete lock under the Python lock rule)"
                )
            return (
                f"{display} line {lineno} uses option {opt}, which the "
                f"Python lock rule does not exempt "
                f"(not a complete lock under the Python lock rule)"
            )
        if not re.search(r"--hash(?:=|\s|$)", body):
            return (
                f"{display} line {lineno} carries no --hash: "
                f"{body[:60]} (not a complete lock under the Python lock rule)"
            )
    return None


def _check_include(
    target: str | None,
    including: Path,
    root: Path,
    seen: frozenset[Path],
    display: str,
    lineno: int,
) -> str | None:
    """Follow one -r/-c include; reason if it cannot be part of the lock."""
    where = f"{display} line {lineno}"
    suffix = "(not a complete lock under the Python lock rule)"
    if not target:
        return f"{where} includes no file {suffix}"
    if "://" in target or re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", target):
        return (
            f"{where} includes {target!r} by URL; an include by URL "
            f"is not part of the checkout {suffix}"
        )
    resolved = (including.parent / target).resolve()
    if not resolved.is_relative_to(root):
        return (
            f"{where} includes {target!r}, which resolves outside "
            f"the checkout {suffix}"
        )
    if not resolved.is_file():
        return f"{where} includes {target!r}, which does not exist {suffix}"
    if resolved in seen:
        return None  # include cycle: already vetted on this chain
    return _check_requirements_file(
        resolved, root, seen | {resolved}
    )


def check_requirements_frozen(req_path: Path) -> str | None:
    """Check that requirements.txt is a COMPLETE lock. Reason if not.

    REG-A (docs/registration/python-lock-rule.md item 1, WO-8.1): a
    requirements file is a complete lock only when every package-spec
    entry carries ``--hash``. An ``==``-pinned file without hashes is
    NOT a lock under the rule — it goes through lock generation
    instead (the WAL-76 gap: the old check accepted ``==`` alone, and
    the file then installed with ``--no-deps``, so missing transitive
    dependencies surfaced only as a start failure).

    Addendum 46 tightens the check to an ALLOWLIST. The old check
    skipped every option line, so it called three incomplete files
    complete (each then failed as BUILD_FAILED under
    ``--require-hashes`` instead of going to generation):

    - an include-only file (``-r requirements/base.txt`` with the
      include unhashed): includes are now FOLLOWED — ``-r`` /
      ``--requirement`` and ``-c`` / ``--constraint`` targets inside
      the checkout are checked the same way, recursively, with a
      cycle guard; an include outside the checkout, a URL include,
      or a missing file is not complete;
    - an editable (``-e .``): an editable pins no artifact;
    - a pin whose ``--hash`` appears only inside a comment: an
      inline comment (whitespace, then ``#``) is stripped before
      looking for ``--hash``.

    Only the global pip options are exempt: ``--index-url``/``-i``,
    ``--extra-index-url``, ``--find-links``/``-f``, ``--trusted-host``,
    ``--pre``, ``--prefer-binary``, ``--only-binary``, ``--no-binary``.
    Entries are logical lines (a trailing backslash continues an
    entry). A file with no spec entries at all is trivially complete
    (the empty set is fully pinned).
    """
    top = req_path.resolve()
    return _check_requirements_file(top, top.parent, frozenset({top}))


def _is_yarn_berry(snapshot_dir: Path) -> bool:
    """True for Yarn Berry (v2+), which uses --immutable; v1 uses --frozen-lockfile."""
    if (snapshot_dir / ".yarnrc.yml").is_file():
        return True
    pkg = snapshot_dir / "package.json"
    try:
        data = json.loads(pkg.read_text(encoding="utf-8"))
        pm = str(data.get("packageManager") or "")
        if pm.startswith("yarn@"):
            major = pm.split("@", 1)[1].split(".", 1)[0]
            return major.isdigit() and int(major) >= 2
    except (json.JSONDecodeError, OSError, IndexError):
        pass
    return False


def select_package_manager(
    snapshot_dir: Path, runtime: str, lockfile_name: str
) -> tuple[str, list[str]]:
    """Pick the package manager and frozen install command for a lockfile.

    Returns (manager_name, install_argv). Raises ValueError for an
    unsupported lockfile.

    Finding 2: when `packageManager` pins a version (e.g. `pnpm@9.1.0`) and
    corepack is available, the install runs through corepack so the pinned
    version is used. Otherwise the plain binary is used and a note is
    logged by the caller.
    """
    pm_name, pm_version = (
        read_package_manager(snapshot_dir) if runtime == "node" else (None, None)
    )
    corepack = shutil.which("corepack")

    def _cmd(manager: str, args: list[str]) -> list[str]:
        if (
            corepack
            and pm_name == manager
            and pm_version
            and manager in ("pnpm", "yarn", "npm")
        ):
            return ["corepack", f"{manager}@{pm_version}", *args]
        return [manager, *args]

    if runtime == "node":
        if lockfile_name == "package-lock.json":
            return "npm", _cmd("npm", ["ci", "--no-audit", "--no-fund"])
        if lockfile_name == "yarn.lock":
            if _is_yarn_berry(snapshot_dir):
                return "yarn", _cmd("yarn", ["install", "--immutable"])
            return "yarn", _cmd("yarn", ["install", "--frozen-lockfile"])
        if lockfile_name == "pnpm-lock.yaml":
            return "pnpm", _cmd("pnpm", ["install", "--frozen-lockfile"])
        if lockfile_name in ("bun.lock", "bun.lockb"):
            return "bun", ["bun", "install", "--frozen-lockfile"]
    else:
        if lockfile_name == "uv.lock":
            return "uv", ["uv", "sync", "--frozen"]
        if lockfile_name == "poetry.lock":
            # REG-A item 1: the frozen form is `poetry install --sync`
            # (install exactly the locked set, never update the lock).
            return "poetry", ["poetry", "install", "--sync"]
        if lockfile_name == "pdm.lock":
            # REG-A item 1: `pdm sync` installs exactly the locked set.
            return "pdm", ["pdm", "sync"]
        if lockfile_name == "requirements.txt":
            # The pip binary path is filled in by provision() (isolated venv).
            return "pip", ["pip", "install"]
    raise ValueError(f"unsupported lockfile: {lockfile_name}")


def build_env(
    extra: dict[str, str] | None = None,
    prepend_path: str | None = None,
) -> dict[str, str]:
    """Build the subprocess environment from an allowlist.

    Copies PATH and HOME from the current environment, sets CI and
    DEBIAN_FRONTEND, then applies `extra` (contract env, PORT, ...).
    Never an empty dict: a missing binary becomes BUILD_FAILED, not a
    FileNotFoundError escaping the runner.

    Finding 5: `prepend_path` (the per-run venv's bin dir) goes first on
    PATH so build/migrate/seed/start use the isolated Python.
    """
    env: dict[str, str] = {}
    for key in ("PATH", "HOME"):
        val = os.environ.get(key)
        if val:
            env[key] = val
    if prepend_path:
        env["PATH"] = prepend_path + os.pathsep + env.get("PATH", "")
    env["CI"] = "true"
    env["DEBIAN_FRONTEND"] = "noninteractive"
    if extra:
        env.update(extra)
    return env


def _resolve_readiness(readiness: str, port: int, logs: list[str]) -> str:
    """Resolve the readiness check URL (finding 6).

    A readiness starting with `/` is joined to the assigned target URL.
    A full URL is used as-is, with a warning if its port differs from the
    assigned port. Empty readiness falls back to the target root.
    """
    target_url = f"http://127.0.0.1:{port}"
    if not readiness:
        return target_url
    if readiness.startswith("/"):
        return target_url + readiness
    try:
        parsed = urllib.parse.urlparse(readiness)
        if parsed.port and parsed.port != port:
            logs.append(
                f"warning: readiness URL port {parsed.port} != "
                f"assigned port {port}; using readiness as-is"
            )
    except Exception:
        pass
    return readiness


def _target_log_tail(log_path: Path, n: int = 40) -> str:
    """Read the last n lines of a target log file (finding 7)."""
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(lines[-n:])


@dataclass
class Provisioner:
    """Builds and starts probe targets from repo contracts."""

    workdir: Path
    build_timeout: int = 600
    start_timeout: int = 120
    readiness_timeout: int = 60

    def _exec(
        self,
        cmd: list[str],
        cwd: Path,
        timeout: int,
        logs: list[str],
        env: dict[str, str],
    ) -> tuple[bool, str | None]:
        """Run a command. Returns (success, failure_reason). Never raises.

        A missing binary yields (False, "missing binary: <name>") instead of
        FileNotFoundError escaping the runner.
        """
        logs.append(f"$ {' '.join(cmd)}")
        try:
            proc = subprocess.run(
                cmd,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
            )
        except FileNotFoundError:
            return False, f"missing binary: {cmd[0]}"
        except subprocess.TimeoutExpired:
            return False, f"command timed out after {timeout}s: {' '.join(cmd)}"
        logs.append(proc.stdout[-2000:])
        if proc.stderr:
            logs.append(proc.stderr[-2000:])
        if proc.returncode != 0:
            return False, f"command failed with code {proc.returncode}: {' '.join(cmd)}"
        return True, None

    def _fail(
        self,
        status: ProvisionStatus,
        reason: str,
        logs: list[str],
        runtime: str | None = None,
        runtime_version: str | None = None,
        package_manager: str | None = None,
        lockfile_sha256: str | None = None,
        target_log_path: str | None = None,
        run_id: str | None = None,
    ) -> ProvisionResult:
        logs.append(reason)
        return ProvisionResult(
            status=status,
            logs=logs,
            lockfile_sha256=lockfile_sha256,
            runtime=runtime,
            runtime_version=runtime_version,
            package_manager=package_manager,
            reason=reason,
            target_log_path=target_log_path,
            run_id=run_id,
        )

    def _make_venv(
        self,
        python_bin: str,
        run_id: str,
        logs: list[str],
        env: dict[str, str],
    ) -> Path | None:
        """Create a fresh isolated venv for this run (finding 5).

        One venv per provision call at workdir/venvs/<run_id>/, never
        reused across runs, so fix^ and fix cannot share installed
        packages.
        """
        venv_dir = self.workdir / "venvs" / run_id
        ok, reason = self._exec(
            [python_bin, "-m", "venv", str(venv_dir)],
            self.workdir,
            self.build_timeout,
            logs,
            env,
        )
        if not ok:
            logs.append(f"venv creation failed: {reason}")
            return None
        return venv_dir

    def _make_run_bin(
        self,
        runtime_bin: str,
        run_id: str,
        logs: list[str],
        env: dict[str, str],
    ) -> Path | None:
        """Create a per-run bin/ dir pinning the selected Node runtime.

        Blockers fix: selecting `node24` is not enough — install, build
        and start must actually run it. This dir holds symlinks
        `node`/`npm`/`npx` resolving to the selected install, and goes
        first on PATH for every step, so the recorded version is the
        version the target really uses.
        """
        bin_dir = self.workdir / "runs" / run_id / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        node_path = shutil.which(runtime_bin, path=env.get("PATH"))
        if node_path is None:
            logs.append(f"cannot resolve selected runtime binary: {runtime_bin}")
            return None
        node_dir = str(Path(node_path).parent)
        linked = 0
        # `node` must point at the SELECTED binary, not just any node on PATH.
        targets = {"node": node_path}
        for name in ("npm", "npx"):
            # Prefer the tool alongside the selected node; fall back to PATH.
            found = shutil.which(
                name, path=node_dir + os.pathsep + env.get("PATH", "")
            )
            if found is not None:
                targets[name] = found
            else:
                logs.append(f"warning: {name} not found for selected runtime")
        for name, target in targets.items():
            link = bin_dir / name
            try:
                if link.is_symlink() or link.exists():
                    link.unlink()
                link.symlink_to(target)
                linked += 1
            except OSError as e:
                logs.append(f"warning: cannot link {name}: {e}")
        if linked == 0:
            return None
        logs.append(f"run bin: {bin_dir} (node -> {node_path})")
        return bin_dir

    def _effective_runtime_version(
        self, runtime: str, env: dict[str, str], logs: list[str]
    ) -> str | None:
        """Re-probe the runtime version through the step PATH.

        Blocker fix: the recorded version must be the one the target
        really runs. Resolves `node` (or `python3`) via the same PATH
        every step uses and returns its `--version` output.
        """
        name = "node" if runtime == "node" else "python3"
        path = shutil.which(name, path=env.get("PATH"))
        if path is None:
            logs.append(f"effective runtime probe: {name} not found on step PATH")
            return None
        ver = _binary_version(path, env)
        logs.append(f"effective runtime: {name} -> {path} ({ver})")
        return ver

    def cleanup_run(self, run_id: str, logs: list[str] | None = None) -> None:
        """Delete a run's scratch dirs: venvs/<run_id> and runs/<run_id>.

        Called automatically on every failure path. On READY the caller
        owns the target process and must call this when done with it.
        Tolerates missing dirs.

        NOTE: this does NOT stop the target process. Use stop_target()
        first to terminate and reap the process, then cleanup_run() to
        remove the scratch dirs.
        """
        for sub in ("venvs", "runs"):
            d = self.workdir / sub / run_id
            if d.is_dir():
                shutil.rmtree(d, ignore_errors=True)
                if logs is not None:
                    logs.append(f"cleaned up: {d}")

    def stop_target(self, proc: Any, logs: list[str] | None = None) -> None:
        """Terminate and reap a READY target's process group (O2).

        Called on every exit path for a READY target, before cleanup_run().
        The target was started with start_new_session=True, so it leads its
        own process group. Terminates the whole group gracefully, escalates
        to kill, and reaps to avoid zombies. Tolerates already-exited
        processes.
        """
        if proc is None:
            return
        try:
            import os
            import signal

            if proc.poll() is None:
                # Still running: terminate the whole process group
                # gracefully, then kill.
                try:
                    pgid = os.getpgid(proc.pid)
                except (OSError, ProcessLookupError):
                    pgid = None
                if pgid is not None:
                    try:
                        os.killpg(pgid, signal.SIGTERM)
                    except (OSError, ProcessLookupError):
                        pass
                    try:
                        proc.wait(timeout=10)
                    except Exception:
                        try:
                            os.killpg(pgid, signal.SIGKILL)
                        except (OSError, ProcessLookupError):
                            pass
                        try:
                            proc.wait(timeout=5)
                        except Exception:
                            pass
                else:
                    # Fallback: no process group (shouldn't happen with
                    # start_new_session=True, but tolerate it).
                    proc.terminate()
                    try:
                        proc.wait(timeout=10)
                    except Exception:
                        try:
                            proc.kill()
                        except Exception:
                            pass
                        try:
                            proc.wait(timeout=5)
                        except Exception:
                            pass
                if logs is not None:
                    logs.append("target process group terminated")
            else:
                # Already exited: reap to avoid zombie.
                try:
                    proc.wait(timeout=5)
                except Exception:
                    pass
        except Exception as e:
            if logs is not None:
                logs.append(f"target stop failed: {e}")

    def provision(
        self,
        snapshot_dir: Path,
        contract: Any,
        port: int,
        expected_lockfile_sha256: str | None = None,
        require_lockfile_match: bool = True,
        bootstrap_env: dict[str, str] | None = None,
        lock_cutoff: Any = None,
    ) -> ProvisionResult:
        """Provision a target: install, build, migrate, seed, start, readiness.

        Returns a typed ProvisionResult. Never raises on build/start
        failure — those become BUILD_FAILED / START_FAILED, which the
        scorer maps to INCONCLUSIVE.
        """
        run_id = uuid.uuid4().hex[:12]
        logs: list[str] = [f"run_id: {run_id}"]
        # Addendum 31: the executed steps of this provision, recorded
        # as they run and attached to every result on every path.
        executed: list[dict] = []

        def _run_step(
            cmd: list[str],
            cwd: Path,
            timeout: int,
            env: dict[str, str],
            *,
            phase: str,
            shell: str | None = None,
        ) -> tuple[bool, str | None]:
            """Run one plan step through _exec and record it
            (Addendum 31): the rendered argv (or the originating
            shell string for shell-declared steps) and the real exit
            code — recovered from _exec's own failure contract
            ("command failed with code N"), None when no process ran
            to completion (missing binary, timeout).
            """
            ok, reason = self._exec(cmd, cwd, timeout, logs, env)
            entry: dict = {"phase": phase, "executor": "host"}
            if shell is not None:
                entry["shell"] = shell
            else:
                entry["argv_rendered"] = list(cmd)
            if cwd != snap:
                # Plan 1.2b: a workspace-scoped step ran in the app
                # member's directory; record where, relative to the
                # checkout root (the sandbox records the same fact).
                entry["cwd"] = cwd.relative_to(snap).as_posix()
            entry["exit_code"] = _exit_code_from_reason(ok, reason)
            executed.append(entry)
            return ok, reason

        snap = Path(snapshot_dir)
        contract_env: dict[str, str] = dict(getattr(contract, "env", None) or {})
        # WO-8.3: per-run bootstrap credential values merge over the
        # contract env (they win a name collision — the credential the
        # instance is bootstrapped with is the generated one). They
        # reach every step and the started app, exactly as the sandbox
        # path injects them into the install and app containers.
        if bootstrap_env:
            contract_env.update(bootstrap_env)

        # WO-7: this Provisioner is the HOST executor. The plan comes
        # from the runtime's provisioning adapter (proofdeploy.
        # provisioning) — the same plan the sandbox executor runs. The
        # adapter is imported lazily: provisioning imports this
        # module's helpers, so a top-level import would be circular.
        from proofdeploy.provisioning import (
            InstallPlan,
            build_backend_evidence,
            plan_provisioning,
        )

        # REG-A (WO-8.1): the generated lock read back after the
        # resolve step runs (text + sha256 for the evidence). Set
        # once, right after the install step succeeds; _bail and the
        # result builders below attach it when present.
        gen_lock: dict | None = None

        # WO-8.1c: the build backend that actually built the root
        # project, read back the same way (see
        # build_backend_evidence); attached wherever gen_lock is.
        build_backend_ev: dict | None = None

        def _bail(
            status: ProvisionStatus,
            reason: str,
            _plan: InstallPlan | None = None,
            **kw: Any,
        ) -> ProvisionResult:
            # Failure path: nothing is running; remove the run's scratch dirs.
            self.cleanup_run(run_id, logs)
            result = self._fail(status, reason, logs, run_id=run_id, **kw)
            result.executed_steps = executed
            if gen_lock is not None:
                result.generated_lock = gen_lock
            if build_backend_ev is not None:
                result.build_backend = build_backend_ev
            if _plan is not None:
                result.install_plan = _plan.to_dict()
                result.install_plan_sha256 = _plan.sha256()
            return result

        # 1. Plan through the adapter: runtime detection, declared
        # range, lockfile selection, and the REG-A completeness check
        # all happen there now (one source of truth, shared with the
        # sandbox). lock_cutoff is the unit's resolution cutoff for a
        # generated Python lock (REG-A item 2) — a timestamp or a
        # lazy zero-arg callable producing one (provisioning's
        # LockCutoff); the adapter evaluates it only when a
        # generated lock is planned.
        try:
            plan = plan_provisioning(snap, contract, lock_cutoff=lock_cutoff)
        except ValueError as e:
            return _bail(ProvisionStatus.BUILD_FAILED, str(e))
        runtime = plan.runtime
        declared_range = plan.declared_range
        logs.append(f"runtime: {runtime} (declared range: {declared_range})")
        logs.append(
            f"install plan: sha256 {plan.sha256()[:16]}... "
            f"(adapter: {plan.adapter})"
        )
        # Base environment for probing (allowlist + contract env).
        probe_env = build_env(contract_env)

        # 2. Select an installed binary satisfying the declared range
        # (finding 2). actual_version is the probed version here; it is
        # re-probed through the final step PATH below, so the recorded
        # version is the one the target really runs. (Host executor:
        # the runtime comes from the host's own binaries; the plan's
        # pinned image selection is the sandbox executor's input.)
        runtime_bin, actual_version, bin_reason = select_runtime_binary(
            runtime, declared_range, probe_env, logs
        )
        if runtime_bin is None:
            assert bin_reason is not None
            return _bail(
                ProvisionStatus.BUILD_FAILED,
                bin_reason,
                plan,
                runtime=runtime,
                runtime_version=None,
            )

        # 2b. Pin the selected runtime first on PATH for EVERY step.
        #
        # Blocker fix: selecting `node24` meant nothing while install,
        # build and start ran whichever `node` was first on PATH. The
        # per-run bin/ dir holds symlinks to the selected install, so the
        # version we record is the version the target really uses.
        run_bin: str | None = None
        if runtime == "node":
            run_bin_dir = self._make_run_bin(runtime_bin, run_id, logs, probe_env)
            if run_bin_dir is None:
                return _bail(
                    ProvisionStatus.BUILD_FAILED,
                    f"could not pin selected node runtime {runtime_bin!r}",
                    plan,
                    runtime=runtime,
                    runtime_version=None,
                )
            run_bin = str(run_bin_dir)

        # 3. The plan's lockfile facts (the adapter already applied
        # the packageManager preference, finding 3, and the frozen
        # check, finding 4). A refusal in the plan is a BUILD_FAILED
        # here, with the plan's lockfile hash recorded.
        if plan.lockfile_name is None:
            return _bail(
                ProvisionStatus.BUILD_FAILED,
                f"no frozen lockfile found in {snap} for runtime {runtime!r}",
                plan,
                runtime=runtime,
                runtime_version=actual_version,
            )
        lockfile_name = plan.lockfile_name
        lockfile_sha = plan.lockfile_sha256
        assert lockfile_sha is not None
        logs.append(f"lockfile: {lockfile_name} sha256: {lockfile_sha[:16]}...")
        if plan.refusal is not None:
            return _bail(
                ProvisionStatus.BUILD_FAILED,
                plan.refusal,
                plan,
                runtime=runtime,
                runtime_version=actual_version,
                lockfile_sha256=lockfile_sha,
            )

        # 4. Lockfile match check (required for measured runs). A
        # GENERATED lock (REG-A) has no committed lockfile to pin:
        # the plan's lockfile_sha256 is the declared source file's
        # hash, and the generated lock's own text + sha256 land in
        # the evidence instead — so the "hash required" bail applies
        # only to committed locks.
        if (
            require_lockfile_match
            and expected_lockfile_sha256 is None
            and plan.lock_source != "generated"
        ):
            return _bail(
                ProvisionStatus.BUILD_FAILED,
                "lockfile hash required for measured runs "
                "(expected_lockfile_sha256 is None)",
                plan,
                runtime=runtime,
                runtime_version=actual_version,
                lockfile_sha256=lockfile_sha,
            )
        if expected_lockfile_sha256 is not None and lockfile_sha != expected_lockfile_sha256:
            return _bail(
                ProvisionStatus.BUILD_FAILED,
                "lockfile mismatch: frozen lockfile changed",
                plan,
                runtime=runtime,
                runtime_version=actual_version,
                lockfile_sha256=lockfile_sha,
            )

        # 5. The package manager and the install step come from the
        # plan (the adapter selected both from the lockfile).
        package_manager = plan.package_manager
        assert package_manager is not None
        install_steps = [
            s for s in plan.steps
            if s.phase == "install" and s.argv is not None
        ]
        assert install_steps
        # The dependency install is the LAST install-phase step;
        # any earlier ones are bootstraps (Addendum 35's persisted
        # corepack install) that run first, verbatim.
        install_step = install_steps[-1]
        assert install_step.argv is not None
        logs.append(f"package manager: {package_manager}")
        logs.append(f"install command: {' '.join(install_step.argv)}")

        # Step environment: allowlist + contract env. The selected runtime
        # goes first on PATH for EVERY step: the per-run bin/ dir for Node
        # (blocker fix), the fresh per-run venv's bin for pip Python.
        #
        # The plan's venv step (python3 -m venv <venv>) is materialized
        # by _make_venv at the host executor's per-run location
        # (workdir/venvs/<run_id>): one venv per provision call, never
        # reused across runs, so fix^ and fix cannot share installed
        # packages (finding 5). The sandbox executor renders the same
        # step at /checkout/.venv.
        from proofdeploy.provisioning import pip_flags, render_argv

        venv_bin: str | None = None
        venv_dir: Path | None = None
        if package_manager == "pip":
            venv_dir = self._make_venv(runtime_bin, run_id, logs, probe_env)
            # Addendum 31: the venv step is recorded like every other
            # executed step (its exit code is not surfaced by
            # _make_venv: 0 on success, None when it failed).
            executed.append({
                "phase": "venv",
                "executor": "host",
                "argv_rendered": [
                    runtime_bin, "-m", "venv",
                    str(venv_dir or (self.workdir / "venvs" / run_id)),
                ],
                "exit_code": 0 if venv_dir is not None else None,
            })
            if venv_dir is None:
                return _bail(
                    ProvisionStatus.BUILD_FAILED,
                    "could not create isolated venv",
                    plan,
                    runtime=runtime,
                    runtime_version=actual_version,
                    package_manager=package_manager,
                    lockfile_sha256=lockfile_sha,
                )
            venv_bin = str(venv_dir / "bin")
            logs.append(f"venv: {venv_dir} (bin first on PATH)")
        elif package_manager in ("uv", "poetry", "pdm"):
            # WO-8.1b: the manager installs into the CHECKOUT venv
            # (snap/.venv) — the venv the plan's steps target
            # ({venv} renders to it below). It is created here,
            # from the selected runtime: uv is pointed at it by
            # the plan (UV_PROJECT_ENVIRONMENT + --python), poetry
            # and pdm adopt an existing in-project .venv. From here
            # on it is treated exactly like the pip venv: its bin
            # goes first on PATH for install/build/migrate/seed/
            # start, so the app and the contract steps run against
            # the manager-installed environment.
            venv_dir = snap / ".venv"
            ok, venv_reason = _run_step(
                [runtime_bin, "-m", "venv", str(venv_dir)],
                snap,
                self.build_timeout,
                probe_env,
                phase="venv",
            )
            if not ok:
                return _bail(
                    ProvisionStatus.BUILD_FAILED,
                    f"could not create isolated venv: {venv_reason}",
                    plan,
                    runtime=runtime,
                    runtime_version=actual_version,
                    package_manager=package_manager,
                    lockfile_sha256=lockfile_sha,
                )
            venv_bin = str(venv_dir / "bin")
            logs.append(f"venv: {venv_dir} (bin first on PATH)")
        first_on_path = run_bin or venv_bin
        if (
            plan.corepack_version is not None
            and package_manager in ("pnpm", "yarn")
        ):
            # Plan 1.2(f): a corepack-pinned plan bootstraps corepack
            # into the checkout's .pd-tools (persisted); the corepack
            # package's own bin dir carries the manager shims
            # (pnpm/pnpx/yarn/yarnpkg), which resolve the snapshot's
            # packageManager pin — so contract steps (and the start)
            # calling bare `pnpm` / `yarn` run the plan's pinned
            # version, never a PATH fallback. The manager's bin goes
            # first on PATH — the host rendering of the sandbox's
            # corepack PATH treatment.
            manager_bin = str(snap / ".pd-tools" / "node_modules" / ".bin")
            first_on_path = (
                manager_bin + os.pathsep + first_on_path
                if first_on_path
                else manager_bin
            )
        if package_manager == "bun":
            # Plan 1.2e: the plan's bootstrapped bun lives in the
            # checkout's .pd-tools/bin; an app's own contract steps
            # may call `bun` (its build/start), so that dir goes
            # first on PATH for every step — the host rendering of
            # the sandbox's bun PATH treatment.
            bun_bin = str(snap / ".pd-tools" / "bin")
            first_on_path = (
                bun_bin + os.pathsep + first_on_path
                if first_on_path
                else bun_bin
            )
        step_env = build_env(contract_env, prepend_path=first_on_path)

        # 6b. Re-probe the runtime version through the final step PATH.
        # This is the version the target will actually run — not just the
        # binary we selected, but the one PATH resolves for every step.
        effective = self._effective_runtime_version(runtime, step_env, logs)
        if effective is None:
            return _bail(
                ProvisionStatus.BUILD_FAILED,
                f"selected {runtime} runtime {runtime_bin!r} not usable "
                f"via step PATH",
                plan,
                runtime=runtime,
                runtime_version=actual_version,
                package_manager=package_manager,
                lockfile_sha256=lockfile_sha,
            )
        actual_version = effective
        logs.append(f"runtime version in use: {actual_version}")

        # Render the plan's install steps for this executor. pip's
        # argv is a template (venv/checkout/flags placeholders);
        # other managers' argv may carry {checkout} (the persisted
        # corepack paths), {venv} (WO-8.1b: the checkout venv the
        # lock managers install into) and {runtime_bin}, rendered
        # the same way — as are the steps' env values (e.g. the uv
        # plan's UV_PROJECT_ENVIRONMENT={venv}).
        step_mapping: dict[str, str] = {
            "{runtime_bin}": runtime_bin,
            "{checkout}": str(snap),
        }
        if venv_dir is not None:
            step_mapping["{venv}"] = str(venv_dir)

        def _step_env(step: Any) -> dict[str, str]:
            return {
                **step_env,
                **{
                    key: render_argv([value], step_mapping, [])[0]
                    for key, value in step.env.items()
                },
            }

        if package_manager == "pip":
            assert venv_dir is not None
            install_cmd = render_argv(
                install_step.argv,
                {
                    "{runtime_bin}": runtime_bin,
                    "{venv}": str(venv_dir),
                    "{checkout}": str(snap),
                },
                pip_flags(plan, sandbox=False),
            )
        else:
            install_cmd = render_argv(
                install_step.argv, step_mapping, []
            )

        # Tools that choose their own Python use the selected one
        # BY PLAN (WO-8.1b): the uv plan's install step carries
        # --python {runtime_bin} itself, and the poetry/pdm plans
        # install into the checkout venv created above from the
        # selected runtime. (Pre-WO-8.1b the host executor appended
        # --python for uv and ran a bare `poetry env use` with a
        # system poetry here — the sandbox could do neither, which
        # is why those plans were sandbox-refused.)

        # 7. Install (frozen). Python pip installs into the per-run
        # venv, never the host environment; the WO-8.1b managers
        # install into the checkout venv. The plan step's own env
        # rides along (e.g. COREPACK_ENABLE_DOWNLOAD_PROMPT=0 for a
        # packageManager-pinned Node install). Bootstrap steps
        # (the persisted corepack install; WO-8.1b: the .pd-tools
        # manager bootstrap and the lock-freshness check) run
        # first, in plan order. A failed step's reason carries the
        # step output's first error line when it has one — the
        # same surfacing the sandbox path applies (Addendum 33) —
        # so a stale committed lock is BUILD_FAILED with the
        # tool's own line (e.g. uv's "error: The lockfile at
        # `uv.lock` needs to be updated...") on both executors.
        for boot in install_steps[:-1]:
            assert boot.argv is not None
            boot_cmd = render_argv(boot.argv, step_mapping, [])
            log_mark = len(logs)
            ok, reason = _run_step(
                boot_cmd, snap, self.build_timeout,
                _step_env(boot),
                phase="install",
            )
            if not ok:
                drift = _lockfile_drift_reason(
                    package_manager, logs[log_mark:]
                )
                if drift is not None:
                    return _bail(
                        ProvisionStatus.BUILD_FAILED,
                        drift,
                        plan,
                        runtime=runtime,
                        runtime_version=actual_version,
                        package_manager=package_manager,
                        lockfile_sha256=lockfile_sha,
                    )
                first_error = _first_error_line(logs[log_mark:])
                return _bail(
                    ProvisionStatus.BUILD_FAILED,
                    "install step failed: "
                    f"{first_error if first_error is not None else reason}",
                    plan,
                    runtime=runtime,
                    runtime_version=actual_version,
                    package_manager=package_manager,
                    lockfile_sha256=lockfile_sha,
                )
        install_env = _step_env(install_step)
        log_mark = len(logs)
        ok, reason = _run_step(
            install_cmd, snap, self.build_timeout, install_env,
            phase="install",
        )
        if not ok:
            drift = _lockfile_drift_reason(package_manager, logs[log_mark:])
            if drift is not None:
                return _bail(
                    ProvisionStatus.BUILD_FAILED,
                    drift,
                    plan,
                    runtime=runtime,
                    runtime_version=actual_version,
                    package_manager=package_manager,
                    lockfile_sha256=lockfile_sha,
                )
            first_error = _first_error_line(logs[log_mark:])
            return _bail(
                ProvisionStatus.BUILD_FAILED,
                "install failed: "
                f"{first_error if first_error is not None else reason}",
                plan,
                runtime=runtime,
                runtime_version=actual_version,
                package_manager=package_manager,
                lockfile_sha256=lockfile_sha,
            )

        # REG-A (WO-8.1): read the generated lock back for the
        # evidence (its text and sha256 are recorded, item 4). The
        # resolve step wrote it into the checkout; a generated plan
        # whose file is missing after a successful install is a
        # BUILD_FAILED, never a silent pass.
        if plan.lock_source == "generated":
            assert plan.generated_lockfile_name is not None
            gen_path = snap / plan.generated_lockfile_name
            try:
                gen_text = gen_path.read_text(encoding="utf-8")
            except OSError:
                return _bail(
                    ProvisionStatus.BUILD_FAILED,
                    "generated lock missing after install: "
                    f"{plan.generated_lockfile_name}",
                    plan,
                    runtime=runtime,
                    runtime_version=actual_version,
                    package_manager=package_manager,
                    lockfile_sha256=lockfile_sha,
                )
            gen_lock = {
                "text": gen_text,
                "sha256": hashlib.sha256(gen_text.encode()).hexdigest(),
            }
            logs.append(
                f"generated lock: {plan.generated_lockfile_name} "
                f"sha256 {gen_lock['sha256'][:16]}..."
            )

        # WO-8.1c: a committed manager's install built the root
        # project; read the backend that actually built it back
        # for the evidence (the built wheel's Generator, stamped
        # in the checkout venv the managers install into). Never
        # affects the outcome — evidence only.
        if plan.build_backend is not None:
            build_backend_ev = build_backend_evidence(plan, snap)
            if build_backend_ev is not None:
                logs.append(
                    f"build backend: {build_backend_ev['name']} "
                    f"{build_backend_ev['version']}"
                )

        # 8. Contract steps in order: build, migrate, seed (from the
        # plan's steps; the sandbox executor runs the same steps).
        for step in plan.steps:
            if step.phase not in ("build", "migrate", "seed"):
                continue
            if step.executor not in (None, "host"):
                continue
            assert step.shell is not None
            step_name = step.phase
            logs.append(f"contract step: {step_name}")
            ok, reason = _run_step(
                shlex.split(step.shell),
                snap / step.cwd if step.cwd else snap,
                self.build_timeout,
                {**step_env, **step.env},
                phase=step.phase, shell=step.shell,
            )
            if not ok:
                return _bail(
                    ProvisionStatus.BUILD_FAILED,
                    f"contract step '{step_name}' failed: {reason}",
                    plan,
                    runtime=runtime,
                    runtime_version=actual_version,
                    package_manager=package_manager,
                    lockfile_sha256=lockfile_sha,
                )

        # 9. Start the target (the plan's start step). Output is
        # captured to a per-run log file (finding 7), not thrown away.
        start_step = next(
            (s for s in plan.steps if s.phase == "start"), None
        )
        start_cmd = start_step.shell if start_step is not None else None
        if not start_cmd:
            return _bail(
                ProvisionStatus.BUILD_FAILED,
                "contract missing 'start' command",
                plan,
                runtime=runtime,
                runtime_version=actual_version,
                package_manager=package_manager,
                lockfile_sha256=lockfile_sha,
            )
        target_url = f"http://127.0.0.1:{port}"
        start_env = build_env(
            {**contract_env, "PORT": str(port)}, prepend_path=first_on_path
        )
        logs.append(f"starting: {start_cmd} -> {target_url}")

        log_dir = self.workdir / "target-logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        target_log_path = log_dir / f"{run_id}.log"

        def _start_failed(reason: str) -> ProvisionResult:
            tail = _target_log_tail(target_log_path)
            if tail:
                logs.append(f"target output (tail):\n{tail}")
            result = self._fail(
                ProvisionStatus.START_FAILED,
                reason,
                logs,
                runtime=runtime,
                runtime_version=actual_version,
                package_manager=package_manager,
                lockfile_sha256=lockfile_sha,
                target_log_path=str(target_log_path),
            )
            result.install_plan = plan.to_dict()
            result.install_plan_sha256 = plan.sha256()
            result.executed_steps = executed
            if gen_lock is not None:
                result.generated_lock = gen_lock
            if build_backend_ev is not None:
                result.build_backend = build_backend_ev
            return result

        # Addendum 31: the start step is recorded as it is issued;
        # its exit_code stays None while the target runs (READY) and
        # is filled in when the target exits before readiness.
        start_record: dict = {
            "phase": "start",
            "executor": "host",
            "shell": start_cmd,
            "exit_code": None,
        }
        if start_step is not None and start_step.cwd:
            # Plan 1.2b: the workspace app starts in its member
            # directory (recorded, as the sandbox records it).
            start_record["cwd"] = start_step.cwd
        executed.append(start_record)
        proc: subprocess.Popen[str] | None = None
        keep_proc = False  # set True on READY to transfer ownership to caller
        try:
            log_fh = open(target_log_path, "w", encoding="utf-8")
            try:
                proc = subprocess.Popen(
                    shlex.split(start_cmd),
                    cwd=(
                        snap / start_step.cwd
                        if start_step is not None and start_step.cwd
                        else snap
                    ),
                    stdout=log_fh,
                    stderr=subprocess.STDOUT,
                    text=True,
                    env=start_env,
                    # O2: start the target in its own session so the whole
                    # process group (wrapper + children) can be killed.
                    start_new_session=True,
                )
            except FileNotFoundError as e:
                return _start_failed(f"missing binary for start command: {e.filename}")
            except OSError as e:
                return _start_failed(f"start failed: {e}")
            finally:
                # The child inherited its own dup of the fd; the parent's
                # copy can be closed immediately.
                log_fh.close()

            # 10. Wait for readiness (finding 6: path joined to the
            # assigned target URL).
            readiness_url = _resolve_readiness(
                getattr(contract, "readiness", ""), port, logs
            )
            logs.append(f"readiness: {readiness_url}")
            deadline = time.time() + self.readiness_timeout
            ready = False
            while time.time() < deadline:
                assert proc is not None
                if proc.poll() is not None:
                    start_record["exit_code"] = proc.returncode
                    return _start_failed(
                        f"target exited with code {proc.returncode} before ready"
                    )
                try:
                    with urllib.request.urlopen(readiness_url, timeout=5) as r:
                        if r.status < 500:
                            ready = True
                            break
                except Exception:
                    pass
                time.sleep(2)
            if not ready:
                return _start_failed("readiness check timed out")

            logs.append("target ready")
            keep_proc = True
            logs.append(
                f"run scratch kept for caller cleanup: {run_id} "
                f"(call cleanup_run when done with the target)"
            )
            return ProvisionResult(
                status=ProvisionStatus.READY,
                target_url=target_url,
                logs=logs,
                lockfile_sha256=lockfile_sha,
                runtime=runtime,
                runtime_version=actual_version,
                package_manager=package_manager,
                target_log_path=str(target_log_path),
                run_id=run_id,
                install_plan=plan.to_dict(),
                install_plan_sha256=plan.sha256(),
                executed_steps=executed,
                generated_lock=gen_lock,
                build_backend=build_backend_ev,
                proc=proc,
            )
        finally:
            # Kill and reap on every exit path except the READY handoff,
            # which transfers process ownership to the caller.
            if proc is not None and not keep_proc:
                if proc.poll() is None:
                    proc.kill()
                try:
                    proc.wait(timeout=10)
                except Exception:
                    pass
