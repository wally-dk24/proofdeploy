"""Target provisioner for the ProofDeploy runner (WAL-58).

Builds and starts a probe target from a `proofdeploy.yml` repo contract.
All failures are typed results, never crashes: a target that cannot build
or start yields INCONCLUSIVE (per the start-failure scoring rule), not an
exception escaping the runner.

The provisioner:
- selects the runtime per commit from `engines`, `packageManager`, or
  `requires-python`;
- verifies the frozen lockfile (mismatch = build failure);
- runs non-interactive installs;
- waits for the readiness check.
"""

from __future__ import annotations

import hashlib
import subprocess
import time
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

    @property
    def ready(self) -> bool:
        return self.status == ProvisionStatus.READY

    def to_dict(self) -> dict:
        return {
            "status": self.status.value,
            "target_url": self.target_url,
            "lockfile_sha256": self.lockfile_sha256,
            "logs": self.logs,
        }


# Lockfiles we recognize, in preference order per ecosystem.
LOCKFILES = {
    "node": ("package-lock.json", "yarn.lock", "pnpm-lock.yaml"),
    "python": ("requirements.txt", "poetry.lock", "Pipfile.lock"),
}


def _sha256_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def detect_runtime(snapshot_dir: Path) -> str:
    """Choose the runtime from the snapshot's manifests.

    Reads `engines` / `packageManager` from package.json, or
    `requires-python` from pyproject.toml. Returns "node" or "python".
    Raises ValueError if no runtime can be determined.
    """
    pkg = snapshot_dir / "package.json"
    if pkg.is_file():
        return "node"
    pyproject = snapshot_dir / "pyproject.toml"
    if pyproject.is_file():
        return "python"
    requirements = snapshot_dir / "requirements.txt"
    if requirements.is_file():
        return "python"
    raise ValueError(f"cannot detect runtime in {snapshot_dir}: no package.json or pyproject.toml")


def verify_lockfile(snapshot_dir: Path, runtime: str) -> str:
    """Verify the frozen lockfile exists and return its sha256.

    Raises ValueError if no recognized lockfile is present.
    """
    for name in LOCKFILES.get(runtime, ()):
        p = snapshot_dir / name
        if p.is_file():
            return _sha256_file(p)
    raise ValueError(f"no frozen lockfile found in {snapshot_dir} for runtime {runtime!r}")


def _run(
    cmd: list[str], cwd: Path, timeout: int, logs: list[str]
) -> subprocess.CompletedProcess[str]:
    """Run a command non-interactively, capturing output into logs."""
    logs.append(f"$ {' '.join(cmd)}")
    proc = subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        env={"CI": "true", "DEBIAN_FRONTEND": "noninteractive"},
    )
    logs.append(proc.stdout[-2000:])
    if proc.stderr:
        logs.append(proc.stderr[-2000:])
    return proc


@dataclass
class Provisioner:
    """Builds and starts probe targets from repo contracts."""

    workdir: Path
    build_timeout: int = 600
    start_timeout: int = 120
    readiness_timeout: int = 60

    def provision(
        self,
        snapshot_dir: Path,
        contract: Any,
        port: int,
        expected_lockfile_sha256: str | None = None,
    ) -> ProvisionResult:
        """Provision a target: install, start, wait for readiness.

        Returns a typed ProvisionResult. Never raises on build/start
        failure — those become BUILD_FAILED / START_FAILED, which the
        scorer maps to INCONCLUSIVE.
        """
        logs: list[str] = []
        snap = Path(snapshot_dir)

        # 1. Detect runtime.
        try:
            runtime = detect_runtime(snap)
        except ValueError as e:
            logs.append(str(e))
            return ProvisionResult(status=ProvisionStatus.BUILD_FAILED, logs=logs)
        logs.append(f"runtime: {runtime}")

        # 2. Verify frozen lockfile.
        try:
            lockfile_sha = verify_lockfile(snap, runtime)
        except ValueError as e:
            logs.append(str(e))
            return ProvisionResult(status=ProvisionStatus.BUILD_FAILED, logs=logs)
        logs.append(f"lockfile sha256: {lockfile_sha[:16]}...")
        if expected_lockfile_sha256 is not None and lockfile_sha != expected_lockfile_sha256:
            logs.append("lockfile mismatch: frozen lockfile changed")
            return ProvisionResult(
                status=ProvisionStatus.BUILD_FAILED,
                logs=logs,
                lockfile_sha256=lockfile_sha,
            )

        # 3. Non-interactive install.
        try:
            if runtime == "node":
                install = _run(
                    ["npm", "ci", "--no-audit", "--no-fund"],
                    snap,
                    self.build_timeout,
                    logs,
                )
            else:
                install = _run(
                    ["pip", "install", "-r", "requirements.txt", "--quiet"],
                    snap,
                    self.build_timeout,
                    logs,
                )
        except subprocess.TimeoutExpired:
            logs.append("install timed out")
            return ProvisionResult(status=ProvisionStatus.BUILD_FAILED, logs=logs)
        if install.returncode != 0:
            logs.append(f"install failed with code {install.returncode}")
            return ProvisionResult(status=ProvisionStatus.BUILD_FAILED, logs=logs)

        # 4. Optional migrate.
        migrate_cmd = getattr(contract, "migrate", None)
        if migrate_cmd:
            try:
                m = _run(migrate_cmd.split(), snap, self.build_timeout, logs)
            except subprocess.TimeoutExpired:
                logs.append("migrate timed out")
                return ProvisionResult(status=ProvisionStatus.BUILD_FAILED, logs=logs)
            if m.returncode != 0:
                logs.append(f"migrate failed with code {m.returncode}")
                return ProvisionResult(status=ProvisionStatus.BUILD_FAILED, logs=logs)

        # 5. Start the target.
        start_cmd = getattr(contract, "start", "")
        if not start_cmd:
            logs.append("contract missing 'start' command")
            return ProvisionResult(status=ProvisionStatus.BUILD_FAILED, logs=logs)
        target_url = f"http://127.0.0.1:{port}"
        logs.append(f"starting: {start_cmd} -> {target_url}")
        try:
            proc = subprocess.Popen(
                start_cmd.split(),
                cwd=snap,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                env={"PORT": str(port), "CI": "true"},
            )
        except OSError as e:
            logs.append(f"start failed: {e}")
            return ProvisionResult(status=ProvisionStatus.START_FAILED, logs=logs)

        # 6. Wait for readiness.
        readiness = getattr(contract, "readiness", "")
        deadline = time.time() + self.readiness_timeout
        import urllib.request

        ready = False
        while time.time() < deadline:
            if proc.poll() is not None:
                logs.append(f"target exited with code {proc.returncode} before ready")
                return ProvisionResult(status=ProvisionStatus.START_FAILED, logs=logs)
            try:
                with urllib.request.urlopen(readiness or target_url, timeout=5) as r:
                    if r.status < 500:
                        ready = True
                        break
            except Exception:
                pass
            time.sleep(2)
        if not ready:
            logs.append("readiness check timed out; killing target")
            proc.kill()
            return ProvisionResult(status=ProvisionStatus.START_FAILED, logs=logs)

        logs.append("target ready")
        # The caller owns the process lifetime; we stash it on the result.
        result = ProvisionResult(
            status=ProvisionStatus.READY,
            target_url=target_url,
            logs=logs,
            lockfile_sha256=lockfile_sha,
        )
        result.proc = proc  # type: ignore[attr-defined]
        return result
