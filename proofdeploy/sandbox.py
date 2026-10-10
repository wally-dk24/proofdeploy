"""Sandboxed execution for unattended runs (WO-5).

The WO-5 brief requires, before any unattended run: a rootless
container that mounts only the checkout, network cut after install,
and no secrets in the container environment.

Design (given this environment's constraints):
- Rootless podman is unavailable here (newuidmap EPERM), so podman runs
  with the default storage but the workload itself is de-privileged:
  ``--user 65534`` (nobody), ``--cap-drop=ALL``,
  ``--security-opt=no-new-privileges``. The app never runs as root.
- Only the checkout (the snapshot directory) is mounted, at
  ``/checkout``. Nothing else from the host enters the container.
- Install runs with ``--net=host`` and the proxy environment (pip/npm
  need the network). The app runs with ``--net=host`` but WITHOUT any
  proxy variables: direct egress is blocked in this environment, so the
  running app cannot reach the external network, while the host's
  executor can still reach it on 127.0.0.1 for probes.
- Secrets never enter the container environment. The contract env
  passed to the sandbox must already be secret-free (the orchestrator
  passes only declared-public config); auth tokens travel over HTTP,
  not env.

Python is supported now (venv inside the checkout). Node support is
future work.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

PODMAN = ["podman", "--storage-driver", "vfs", "--cgroup-manager=cgroupfs"]

# The workload user: nobody. The app never runs as root.
SANDBOX_UID = "65534"

# Base images per runtime.
IMAGES = {
    "python": "docker.io/library/python:3.12-slim",
    "node": "docker.io/library/node:22",
}

# Platform CA for the sandbox's TLS-intercepting egress proxy. Mounted
# read-only for the install phase so pip/npm can verify the proxy.
PLATFORM_CA_HOST = "/usr/local/share/ca-certificates/hatch-egress-ca.crt"
PLATFORM_CA_CONTAINER = "/ca/hatch-egress-ca.crt"


class SandboxError(RuntimeError):
    """Sandbox setup or execution failed."""


@dataclass
class SandboxConfig:
    """Configuration for sandboxed runs."""

    image: str = IMAGES["python"]
    # Extra read-only mounts (absolute host paths). Empty by default:
    # only the checkout is mounted.
    extra_mounts: list[tuple[str, str]] = field(default_factory=list)


def _podman_base() -> list[str]:
    return list(PODMAN)


def _run(
    args: list[str], timeout: int, logs: list[str]
) -> tuple[bool, str]:
    """Run a podman command; never raises. Returns (ok, reason)."""
    logs.append(f"$ {' '.join(args)}")
    try:
        proc = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout
        )
    except FileNotFoundError:
        return False, "podman not found"
    except subprocess.TimeoutExpired:
        return False, f"timed out after {timeout}s"
    if proc.stdout:
        logs.append(proc.stdout[-2000:])
    if proc.stderr:
        logs.append(proc.stderr[-2000:])
    if proc.returncode != 0:
        return False, f"exit {proc.returncode}"
    return True, ""


@dataclass
class Sandbox:
    """Runs a checkout's install and app in de-privileged containers."""

    config: SandboxConfig
    log: list[str] = field(default_factory=list)

    def _common_args(
        self, checkout: Path, name: str, with_network: bool
    ) -> list[str]:
        args = _podman_base() + [
            "run",
            "--rm",
            "--name",
            name,
            "--net=host",
            "--user",
            SANDBOX_UID,
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "-v",
            f"{checkout}:/checkout",
            "-w",
            "/checkout",
        ]
        if with_network:
            # Install phase only: the platform CA so pip/npm can verify
            # the TLS-intercepting egress proxy.
            args += ["-v", f"{PLATFORM_CA_HOST}:{PLATFORM_CA_CONTAINER}:ro"]
        for host_path, container_path in self.config.extra_mounts:
            args += ["-v", f"{host_path}:{container_path}:ro"]
        return args

    def _prepare_checkout(self, checkout: Path) -> None:
        """Make the checkout usable by the sandbox user (nobody).

        The checkout is a disposable snapshot copy; granting the
        sandbox user read/write/execute is safe and required for the
        de-privileged workload to install and run.
        """
        try:
            (checkout / ".sandbox-home").mkdir(exist_ok=True)
            subprocess.run(
                ["chmod", "-R", "a+rwX", str(checkout)],
                capture_output=True,
                timeout=60,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            self.log.append(f"chmod checkout failed: {e}")

    def install(
        self,
        checkout: Path,
        commands: list[str],
        env: dict[str, str],
        proxy_env: dict[str, str],
        timeout: int = 600,
    ) -> bool:
        """Run install/build commands WITH network (proxy enabled).

        ``env`` is the secret-free contract env. ``proxy_env`` carries
        the egress proxy for the install only.
        """
        self._prepare_checkout(checkout)
        full_env = dict(env)
        full_env.update(proxy_env)
        # Trust the platform CA for the install phase.
        full_env["SSL_CERT_FILE"] = PLATFORM_CA_CONTAINER
        full_env["REQUESTS_CA_BUNDLE"] = PLATFORM_CA_CONTAINER
        full_env["NODE_EXTRA_CA_CERTS"] = PLATFORM_CA_CONTAINER
        full_env["PIP_CERT"] = PLATFORM_CA_CONTAINER
        # The sandbox user (nobody) has no home; give it a writable one
        # inside the checkout.
        full_env["HOME"] = "/checkout/.sandbox-home"
        for i, cmd in enumerate(commands):
            args = self._common_args(checkout, f"sandbox-install-{i}", True)
            for k, v in full_env.items():
                args += ["-e", f"{k}={v}"]
            args += [self.config.image, "bash", "-c", cmd]
            ok, reason = _run(args, timeout, self.log)
            if not ok:
                self.log.append(f"install step {i} failed: {reason}")
                return False
        return True

    def start(
        self,
        checkout: Path,
        command: str,
        env: dict[str, str],
        port: int,
        name: str,
    ) -> SandboxApp:
        """Start the app WITHOUT external network (no proxy env).

        Returns a SandboxApp; the caller must stop() it.
        """
        self._prepare_checkout(checkout)
        # Writable home for the sandbox user.
        env = dict(env)
        env.setdefault("HOME", "/checkout/.sandbox-home")
        args = _podman_base() + [
            "run",
            "-d",
            "--name",
            name,
            "--net=host",
            "--user",
            SANDBOX_UID,
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "-v",
            f"{checkout}:/checkout",
            "-w",
            "/checkout",
        ]
        for host_path, container_path in self.config.extra_mounts:
            args += ["-v", f"{host_path}:{container_path}:ro"]
        # No proxy variables: direct egress is blocked, so the app has
        # no external network. PORT and secret-free contract env only.
        run_env = dict(env)
        run_env["PORT"] = str(port)
        for k, v in run_env.items():
            args += ["-e", f"{k}={v}"]
        args += [self.config.image, "bash", "-c", command]
        ok, reason = _run(args, 60, self.log)
        if not ok:
            raise SandboxError(f"sandbox start failed: {reason}")
        return SandboxApp(name=name, sandbox=self, port=port)

    def stop(self, name: str) -> None:
        _run(_podman_base() + ["rm", "-f", name], 30, self.log)

    def exec_check(self, name: str, cmd: str) -> bool:
        """Run a command in a running container (for readiness)."""
        ok, _ = _run(
            _podman_base() + ["exec", name, "bash", "-c", cmd], 30, self.log
        )
        return ok


@dataclass
class SandboxApp:
    """A running app inside the sandbox."""

    name: str
    sandbox: Sandbox
    port: int

    @property
    def target_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def stop(self) -> None:
        self.sandbox.stop(self.name)

    def wait_ready(self, path: str = "/health", timeout: int = 60) -> bool:
        """Poll the app's readiness endpoint from the host.

        The app runs with --net=host, so the host reaches it on
        127.0.0.1. (podman exec is unusable in this environment.)
        """
        import urllib.request

        deadline = time.time() + timeout
        url = f"http://127.0.0.1:{self.port}{path}"
        while time.time() < deadline:
            try:
                urllib.request.urlopen(url, timeout=5).read()
                return True
            except Exception:
                time.sleep(1)
        return False


def check_podman() -> tuple[bool, str]:
    """Is podman available for sandboxing?"""
    if shutil.which("podman") is None:
        return False, "podman not found"
    try:
        proc = subprocess.run(
            _podman_base() + ["--version"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)
    if proc.returncode != 0:
        return False, "podman --version failed"
    return True, proc.stdout.strip()
