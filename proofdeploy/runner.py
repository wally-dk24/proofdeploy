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
    "python": ("uv.lock", "poetry.lock", "requirements.txt"),
}


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
    # Wildcards: 1.x, 1.2.x
    low = ver.lower()
    if "x" in low or low == "*":
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
        for tok in re.split(r"[,\s]+", group_str):
            if tok:
                comparators.extend(_parse_token(tok, ecosystem))
        groups.append(comparators)
    return groups


def satisfies_range(version: str, range_spec: str, ecosystem: str = "node") -> bool:
    """True if `version` satisfies the declared range spec.

    Supports the common npm (`engines`) and PEP 440 (`requires-python`)
    range syntax: >=, <=, >, <, ==, !=, ^, ~, ~=, x-wildcards, hyphen
    ranges, comma AND, || OR.
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


def check_requirements_frozen(req_path: Path) -> str | None:
    """Check that requirements.txt is frozen. Returns a reason if not.

    Finding 4: every package-spec line must be pinned with `==`. Blank
    lines, `#` comments, and `-`/`--` option directives (e.g. `--index-url`,
    `-r includes`) are not package specs and are exempt.
    """
    try:
        lines = req_path.read_text(encoding="utf-8").splitlines()
    except OSError as e:
        return f"cannot read requirements.txt: {e}"
    for lineno, line in enumerate(lines, 1):
        s = line.strip()
        if not s or s.startswith("#") or s.startswith("-"):
            continue
        if "==" not in s:
            return (
                f"requirements.txt line {lineno} is not pinned with '==': "
                f"{s[:60]} (not a frozen lockfile)"
            )
    return None


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
            return "poetry", ["poetry", "install"]
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
            entry["exit_code"] = _exit_code_from_reason(ok, reason)
            executed.append(entry)
            return ok, reason

        snap = Path(snapshot_dir)
        contract_env: dict[str, str] = dict(getattr(contract, "env", None) or {})

        # WO-7: this Provisioner is the HOST executor. The plan comes
        # from the runtime's provisioning adapter (proofdeploy.
        # provisioning) — the same plan the sandbox executor runs. The
        # adapter is imported lazily: provisioning imports this
        # module's helpers, so a top-level import would be circular.
        from proofdeploy.provisioning import (
            InstallPlan,
            plan_provisioning,
        )

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
            if _plan is not None:
                result.install_plan = _plan.to_dict()
                result.install_plan_sha256 = _plan.sha256()
            return result

        # 1. Plan through the adapter: runtime detection, declared
        # range, lockfile selection, and the frozen check all happen
        # there now (one source of truth, shared with the sandbox).
        try:
            plan = plan_provisioning(snap, contract)
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

        # 4. Lockfile match check (required for measured runs).
        if require_lockfile_match and expected_lockfile_sha256 is None:
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
        install_step = next(
            (s for s in plan.steps if s.phase == "install"), None
        )
        assert install_step is not None and install_step.argv is not None
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
        first_on_path = run_bin or venv_bin
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

        # Render the plan's install step for this executor. pip's argv
        # is a template (venv/checkout/flags placeholders); every other
        # manager's argv is already concrete.
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
            install_cmd = list(install_step.argv)

        # 6c. Tools that choose their own Python must use the selected one.
        if package_manager == "uv":
            install_cmd = [*install_cmd, "--python", runtime_bin]
            logs.append(f"uv pinned to python: {runtime_bin}")
        elif package_manager == "poetry":
            ok, reason = self._exec(
                ["poetry", "env", "use", runtime_bin],
                snap,
                self.build_timeout,
                logs,
                step_env,
            )
            if not ok:
                return _bail(
                    ProvisionStatus.BUILD_FAILED,
                    f"poetry env use failed: {reason}",
                    plan,
                    runtime=runtime,
                    runtime_version=actual_version,
                    package_manager=package_manager,
                    lockfile_sha256=lockfile_sha,
                )

        # 7. Install (frozen). Python pip installs into the per-run venv,
        # never the host environment. The plan step's own env rides
        # along (e.g. COREPACK_ENABLE_DOWNLOAD_PROMPT=0 for a
        # packageManager-pinned Node install).
        install_env = {**step_env, **install_step.env}
        ok, reason = _run_step(
            install_cmd, snap, self.build_timeout, install_env,
            phase="install",
        )
        if not ok:
            return _bail(
                ProvisionStatus.BUILD_FAILED,
                f"install failed: {reason}",
                plan,
                runtime=runtime,
                runtime_version=actual_version,
                package_manager=package_manager,
                lockfile_sha256=lockfile_sha,
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
                shlex.split(step.shell), snap, self.build_timeout,
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
        executed.append(start_record)
        proc: subprocess.Popen[str] | None = None
        keep_proc = False  # set True on READY to transfer ownership to caller
        try:
            log_fh = open(target_log_path, "w", encoding="utf-8")
            try:
                proc = subprocess.Popen(
                    shlex.split(start_cmd),
                    cwd=snap,
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
