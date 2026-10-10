"""Target provisioner for the ProofDeploy runner (WAL-58).

Builds and starts a probe target from a `proofdeploy.yml` repo contract.
All failures are typed results, never crashes: a target that cannot build
or start yields INCONCLUSIVE (per the start-failure scoring rule), not an
exception escaping the runner.

The provisioner:
- detects the runtime and its version from the repo manifests
  (`engines.node` / `packageManager` for Node, `requires-python` for Python);
- verifies the frozen lockfile (mismatch = build failure);
- installs with the package manager matching the lockfile;
- runs the contract's `build`, `migrate`, and `seed` steps in order;
- starts the target and waits for the readiness check.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
import time
import urllib.request
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
    runtime_version: str | None = None
    package_manager: str | None = None
    reason: str | None = None  # human-readable cause for BUILD_FAILED/START_FAILED
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


def find_lockfile(snapshot_dir: Path, runtime: str) -> tuple[str, str] | None:
    """Find the frozen lockfile. Returns (lockfile_name, sha256) or None."""
    for name in LOCKFILES.get(runtime, ()):
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
    """
    if runtime == "node":
        if lockfile_name == "package-lock.json":
            return "npm", ["npm", "ci", "--no-audit", "--no-fund"]
        if lockfile_name == "yarn.lock":
            if _is_yarn_berry(snapshot_dir):
                return "yarn", ["yarn", "install", "--immutable"]
            return "yarn", ["yarn", "install", "--frozen-lockfile"]
        if lockfile_name == "pnpm-lock.yaml":
            return "pnpm", ["pnpm", "install", "--frozen-lockfile"]
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


def build_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Build the subprocess environment from an allowlist.

    Copies PATH and HOME from the current environment, sets CI and
    DEBIAN_FRONTEND, then applies `extra` (contract env, PORT, ...).
    Never an empty dict: a missing binary becomes BUILD_FAILED, not a
    FileNotFoundError escaping the runner.
    """
    env: dict[str, str] = {}
    for key in ("PATH", "HOME"):
        val = os.environ.get(key)
        if val:
            env[key] = val
    env["CI"] = "true"
    env["DEBIAN_FRONTEND"] = "noninteractive"
    if extra:
        env.update(extra)
    return env


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
        )

    def _make_venv(self, logs: list[str], env: dict[str, str]) -> Path | None:
        """Create an isolated venv under workdir. Returns its dir, or None."""
        venv_dir = self.workdir / "venvs" / "runner-venv"
        ok, reason = self._exec(
            ["python3", "-m", "venv", str(venv_dir)],
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
        logs: list[str] = []
        snap = Path(snapshot_dir)
        contract_env: dict[str, str] = dict(getattr(contract, "env", None) or {})

        # 1. Detect runtime and version.
        try:
            runtime, runtime_version = detect_runtime(snap)
        except ValueError as e:
            return self._fail(ProvisionStatus.BUILD_FAILED, str(e), logs)
        logs.append(f"runtime: {runtime} (declared version: {runtime_version})")

        # 2. Find the frozen lockfile.
        found = find_lockfile(snap, runtime)
        if found is None:
            return self._fail(
                ProvisionStatus.BUILD_FAILED,
                f"no frozen lockfile found in {snap} for runtime {runtime!r}",
                logs,
                runtime=runtime,
                runtime_version=runtime_version,
            )
        lockfile_name, lockfile_sha = found
        logs.append(f"lockfile: {lockfile_name} sha256: {lockfile_sha[:16]}...")

        # 3. Lockfile match check (required for measured runs).
        if require_lockfile_match and expected_lockfile_sha256 is None:
            return self._fail(
                ProvisionStatus.BUILD_FAILED,
                "lockfile hash required for measured runs "
                "(expected_lockfile_sha256 is None)",
                logs,
                runtime=runtime,
                runtime_version=runtime_version,
                lockfile_sha256=lockfile_sha,
            )
        if expected_lockfile_sha256 is not None and lockfile_sha != expected_lockfile_sha256:
            return self._fail(
                ProvisionStatus.BUILD_FAILED,
                "lockfile mismatch: frozen lockfile changed",
                logs,
                runtime=runtime,
                runtime_version=runtime_version,
                lockfile_sha256=lockfile_sha,
            )

        # 4. Select the package manager matching the lockfile.
        try:
            package_manager, install_cmd = select_package_manager(
                snap, runtime, lockfile_name
            )
        except ValueError as e:
            return self._fail(
                ProvisionStatus.BUILD_FAILED,
                str(e),
                logs,
                runtime=runtime,
                runtime_version=runtime_version,
                lockfile_sha256=lockfile_sha,
            )
        logs.append(f"package manager: {package_manager}")

        # Base environment: allowlist + contract env.
        env = build_env(contract_env)

        # 5. Install (frozen). Python pip installs into an isolated venv,
        # never the host environment.
        if package_manager == "pip":
            venv_dir = self._make_venv(logs, env)
            if venv_dir is None:
                return self._fail(
                    ProvisionStatus.BUILD_FAILED,
                    "could not create isolated venv",
                    logs,
                    runtime=runtime,
                    runtime_version=runtime_version,
                    package_manager=package_manager,
                    lockfile_sha256=lockfile_sha,
                )
            pip_bin = str(venv_dir / "bin" / "pip")
            req_file = snap / "requirements.txt"
            cmd = [pip_bin, "install", "-r", str(req_file)]
            if "--hash" in req_file.read_text(encoding="utf-8"):
                cmd.append("--require-hashes")
        else:
            cmd = install_cmd
        ok, reason = self._exec(cmd, snap, self.build_timeout, logs, env)
        if not ok:
            return self._fail(
                ProvisionStatus.BUILD_FAILED,
                f"install failed: {reason}",
                logs,
                runtime=runtime,
                runtime_version=runtime_version,
                package_manager=package_manager,
                lockfile_sha256=lockfile_sha,
            )

        # 6. Contract steps in order: build, migrate, seed.
        for step_name in ("build", "migrate", "seed"):
            step_cmd = getattr(contract, step_name, None)
            if not step_cmd:
                continue
            logs.append(f"contract step: {step_name}")
            ok, reason = self._exec(
                shlex.split(step_cmd), snap, self.build_timeout, logs, env
            )
            if not ok:
                return self._fail(
                    ProvisionStatus.BUILD_FAILED,
                    f"contract step '{step_name}' failed: {reason}",
                    logs,
                    runtime=runtime,
                    runtime_version=runtime_version,
                    package_manager=package_manager,
                    lockfile_sha256=lockfile_sha,
                )

        # 7. Start the target.
        start_cmd = getattr(contract, "start", "")
        if not start_cmd:
            return self._fail(
                ProvisionStatus.BUILD_FAILED,
                "contract missing 'start' command",
                logs,
                runtime=runtime,
                runtime_version=runtime_version,
                package_manager=package_manager,
                lockfile_sha256=lockfile_sha,
            )
        target_url = f"http://127.0.0.1:{port}"
        start_env = build_env({**contract_env, "PORT": str(port)})
        logs.append(f"starting: {start_cmd} -> {target_url}")

        def _start_failed(reason: str) -> ProvisionResult:
            return self._fail(
                ProvisionStatus.START_FAILED,
                reason,
                logs,
                runtime=runtime,
                runtime_version=runtime_version,
                package_manager=package_manager,
                lockfile_sha256=lockfile_sha,
            )

        proc: subprocess.Popen[str] | None = None
        keep_proc = False  # set True on READY to transfer ownership to caller
        try:
            try:
                proc = subprocess.Popen(
                    shlex.split(start_cmd),
                    cwd=snap,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    env=start_env,
                )
            except FileNotFoundError as e:
                return _start_failed(f"missing binary for start command: {e.filename}")
            except OSError as e:
                return _start_failed(f"start failed: {e}")

            # 8. Wait for readiness.
            readiness = getattr(contract, "readiness", "")
            deadline = time.time() + self.readiness_timeout
            ready = False
            while time.time() < deadline:
                assert proc is not None
                if proc.poll() is not None:
                    return _start_failed(
                        f"target exited with code {proc.returncode} before ready"
                    )
                try:
                    with urllib.request.urlopen(
                        readiness or target_url, timeout=5
                    ) as r:
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
                runtime_version=runtime_version,
                package_manager=package_manager,
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
