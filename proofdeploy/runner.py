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
    # Live process handle on READY. Caller-owned: the caller must terminate
    # it when done. Excluded from to_dict.
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
            "package_manager": self.package_manager,
            "reason": self.reason,
            "target_log_path": self.target_log_path,
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
        snap = Path(snapshot_dir)
        contract_env: dict[str, str] = dict(getattr(contract, "env", None) or {})

        def _bail(
            status: ProvisionStatus,
            reason: str,
            **kw: Any,
        ) -> ProvisionResult:
            return self._fail(status, reason, logs, **kw)

        # 1. Detect runtime and declared version range.
        try:
            runtime, declared_range = detect_runtime(snap)
        except ValueError as e:
            return _bail(ProvisionStatus.BUILD_FAILED, str(e))
        logs.append(f"runtime: {runtime} (declared range: {declared_range})")

        # Base environment for probing (allowlist + contract env).
        probe_env = build_env(contract_env)

        # 2. Select an installed binary satisfying the declared range
        # (finding 2). Records the ACTUAL version, not the range.
        runtime_bin, actual_version, bin_reason = select_runtime_binary(
            runtime, declared_range, probe_env, logs
        )
        if runtime_bin is None:
            assert bin_reason is not None
            return _bail(
                ProvisionStatus.BUILD_FAILED,
                bin_reason,
                runtime=runtime,
                runtime_version=None,
            )
        logs.append(f"runtime version in use: {actual_version}")

        # 3. Find the frozen lockfile, preferring the packageManager's
        # lockfile when several exist (finding 3).
        preferred: list[str] | None = None
        if runtime == "node":
            pm_name, _ = read_package_manager(snap)
            if pm_name and pm_name in _PM_LOCKFILES:
                preferred = list(_PM_LOCKFILES[pm_name])
                logs.append(
                    f"packageManager declares {pm_name}; "
                    f"preferring lockfile(s) {preferred}"
                )
        found = find_lockfile(snap, runtime, preferred)
        if found is None:
            return _bail(
                ProvisionStatus.BUILD_FAILED,
                f"no frozen lockfile found in {snap} for runtime {runtime!r}",
                runtime=runtime,
                runtime_version=actual_version,
            )
        lockfile_name, lockfile_sha = found
        logs.append(f"lockfile: {lockfile_name} sha256: {lockfile_sha[:16]}...")

        # 4. requirements.txt must be frozen (finding 4).
        if lockfile_name == "requirements.txt":
            not_frozen = check_requirements_frozen(snap / "requirements.txt")
            if not_frozen is not None:
                return _bail(
                    ProvisionStatus.BUILD_FAILED,
                    not_frozen,
                    runtime=runtime,
                    runtime_version=actual_version,
                    lockfile_sha256=lockfile_sha,
                )

        # 5. Lockfile match check (required for measured runs).
        if require_lockfile_match and expected_lockfile_sha256 is None:
            return _bail(
                ProvisionStatus.BUILD_FAILED,
                "lockfile hash required for measured runs "
                "(expected_lockfile_sha256 is None)",
                runtime=runtime,
                runtime_version=actual_version,
                lockfile_sha256=lockfile_sha,
            )
        if expected_lockfile_sha256 is not None and lockfile_sha != expected_lockfile_sha256:
            return _bail(
                ProvisionStatus.BUILD_FAILED,
                "lockfile mismatch: frozen lockfile changed",
                runtime=runtime,
                runtime_version=actual_version,
                lockfile_sha256=lockfile_sha,
            )

        # 6. Select the package manager matching the lockfile.
        try:
            package_manager, install_cmd = select_package_manager(
                snap, runtime, lockfile_name
            )
        except ValueError as e:
            return _bail(
                ProvisionStatus.BUILD_FAILED,
                str(e),
                runtime=runtime,
                runtime_version=actual_version,
                lockfile_sha256=lockfile_sha,
            )
        logs.append(f"package manager: {package_manager}")
        logs.append(f"install command: {' '.join(install_cmd)}")

        # Step environment: allowlist + contract env. For Python the fresh
        # per-run venv's bin goes first on PATH (finding 5).
        venv_bin: str | None = None
        if package_manager == "pip":
            venv_dir = self._make_venv(runtime_bin, run_id, logs, probe_env)
            if venv_dir is None:
                return _bail(
                    ProvisionStatus.BUILD_FAILED,
                    "could not create isolated venv",
                    runtime=runtime,
                    runtime_version=actual_version,
                    package_manager=package_manager,
                    lockfile_sha256=lockfile_sha,
                )
            venv_bin = str(venv_dir / "bin")
            logs.append(f"venv: {venv_dir} (bin first on PATH)")
        step_env = build_env(contract_env, prepend_path=venv_bin)

        # 7. Install (frozen). Python pip installs into the per-run venv,
        # never the host environment.
        if package_manager == "pip":
            assert venv_bin is not None
            req_file = snap / "requirements.txt"
            cmd = [os.path.join(venv_bin, "pip"), "install", "-r", str(req_file)]
            if "--hash" in req_file.read_text(encoding="utf-8"):
                cmd.append("--require-hashes")
        else:
            cmd = install_cmd
        ok, reason = self._exec(cmd, snap, self.build_timeout, logs, step_env)
        if not ok:
            return _bail(
                ProvisionStatus.BUILD_FAILED,
                f"install failed: {reason}",
                runtime=runtime,
                runtime_version=actual_version,
                package_manager=package_manager,
                lockfile_sha256=lockfile_sha,
            )

        # 8. Contract steps in order: build, migrate, seed.
        for step_name in ("build", "migrate", "seed"):
            step_cmd = getattr(contract, step_name, None)
            if not step_cmd:
                continue
            logs.append(f"contract step: {step_name}")
            ok, reason = self._exec(
                shlex.split(step_cmd), snap, self.build_timeout, logs, step_env
            )
            if not ok:
                return _bail(
                    ProvisionStatus.BUILD_FAILED,
                    f"contract step '{step_name}' failed: {reason}",
                    runtime=runtime,
                    runtime_version=actual_version,
                    package_manager=package_manager,
                    lockfile_sha256=lockfile_sha,
                )

        # 9. Start the target. Output is captured to a per-run log file
        # (finding 7), not thrown away.
        start_cmd = getattr(contract, "start", "")
        if not start_cmd:
            return _bail(
                ProvisionStatus.BUILD_FAILED,
                "contract missing 'start' command",
                runtime=runtime,
                runtime_version=actual_version,
                package_manager=package_manager,
                lockfile_sha256=lockfile_sha,
            )
        target_url = f"http://127.0.0.1:{port}"
        start_env = build_env(
            {**contract_env, "PORT": str(port)}, prepend_path=venv_bin
        )
        logs.append(f"starting: {start_cmd} -> {target_url}")

        log_dir = self.workdir / "target-logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        target_log_path = log_dir / f"{run_id}.log"

        def _start_failed(reason: str) -> ProvisionResult:
            tail = _target_log_tail(target_log_path)
            if tail:
                logs.append(f"target output (tail):\n{tail}")
            return self._fail(
                ProvisionStatus.START_FAILED,
                reason,
                logs,
                runtime=runtime,
                runtime_version=actual_version,
                package_manager=package_manager,
                lockfile_sha256=lockfile_sha,
                target_log_path=str(target_log_path),
            )

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
            return ProvisionResult(
                status=ProvisionStatus.READY,
                target_url=target_url,
                logs=logs,
                lockfile_sha256=lockfile_sha,
                runtime=runtime,
                runtime_version=actual_version,
                package_manager=package_manager,
                target_log_path=str(target_log_path),
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
