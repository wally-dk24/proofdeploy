"""Sandboxed execution for unattended runs (WO-5).

The WO-5 brief requires, before any unattended run: a rootless
container that mounts only the checkout, network cut after install,
and no secrets in the container environment.

Network isolation (orchestrator review 2026-10-10): the app must NOT
use ``--net=host``. Two modes, chosen by capability:

- ``isolated``: a ``podman network create --internal`` bridge. The app
  gets a container IP; the executor reaches it via that IP. ``--internal``
  means no external route. Used when the host's container networking
  works (netavark).
- ``none+socket``: ``--net=none`` (no network interfaces at all) plus a
  Unix-socket bridge. The app binds to the container's loopback; a
  small Python bridge inside the container forwards a Unix socket at
  ``/checkout/.pd-app.sock`` to the app's TCP port; a host-side TCP
  proxy forwards the executor's connections to that socket. The proxy,
  host-loopback services, and external IPs are all unreachable from
  inside; the executor reaches the app through the proxy. Used when
  the host cannot create isolated networks.

User namespaces (orchestrator review 2026-10-10): the sandbox prefers
``--uidmap`` so container root is not host root. When the host cannot
do userns remapping (this sandbox: newuidmap EPERM), it falls back to
``--user 65534`` (nobody) with ``--cap-drop=ALL`` and
``no-new-privileges``, and records the limitation. Set
``require_userns=True`` to fail closed instead.

Secrets never enter the container environment. The contract env passed
to the sandbox must already be secret-free (the orchestrator passes
only declared-public config); auth tokens travel over HTTP, not env.

Python is supported (venv inside the checkout), and Node too
(node_modules inside the checkout, frozen installs from the lockfile,
WO-7 PR 2).
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from proofdeploy.provisioning import select_image

PODMAN = ["podman", "--storage-driver", "vfs", "--cgroup-manager=cgroupfs"]

# The workload user when userns remapping is unavailable: nobody.
SANDBOX_UID = "65534"

# Default image per runtime: the pinned images.lock.json row for the
# runtime's default line (WO-7), digest-pinned like every plan image.
# Kept as the capability probe's image and SandboxConfig's default;
# provisioning itself always uses the plan's selected image.
def _default_image(runtime: str) -> str:
    row = select_image(runtime, None)
    assert row is not None, f"no pinned image rows for runtime {runtime!r}"
    return row.pinned_ref


IMAGES = {
    "python": _default_image("python"),
    "node": _default_image("node"),
}

# In-container path for an optional platform CA (see
# SandboxConfig.ca_cert_host). The host path is configuration, never
# hardcoded: it differs per machine.
PLATFORM_CA_CONTAINER = "/ca/hatch-egress-ca.crt"

# Env vars that must never enter a container, even if present in the
# caller's environment or contract env. The Groq key authenticates the
# model client on the host; the container has no use for it and must
# never see it.
_SECRET_ENV_BLOCKLIST = frozenset({"GROQ_API_KEY"})


def _strip_secret_env(env: dict[str, str]) -> dict[str, str]:
    """Remove blocklisted secret env vars before they reach a container."""
    return {k: v for k, v in env.items() if k not in _SECRET_ENV_BLOCKLIST}


# Unix socket bridging the executor to an app in a --net=none
# container. Lives in a short per-run dir under /tmp (bind-mounted
# at /pd-sock), NOT in the checkout: AF_UNIX paths are limited to
# 107 bytes on Linux, and checkout paths in CI exceed that.
_BRIDGE_SCRIPT_NAME = ".pd-bridge.py"

# Python TCP->Unix-socket bridge run inside the --net=none container.
# Listens on the Unix socket; forwards to the app on container loopback.
_BRIDGE_SCRIPT = """\
import socket, sys, threading, os

SOCK_PATH = sys.argv[1]
TARGET_PORT = int(sys.argv[2])

def _forward(src, dst):
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        for s in (src, dst):
            try:
                s.close()
            except OSError:
                pass

srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
try:
    os.unlink(SOCK_PATH)
except OSError:
    pass
srv.bind(SOCK_PATH)
# The host-side proxy (different uid) must be able to connect.
os.chmod(SOCK_PATH, 0o777)
srv.listen(128)
while True:
    client, _ = srv.accept()
    try:
        target = socket.create_connection(("127.0.0.1", TARGET_PORT), timeout=10)
    except OSError:
        client.close()
        continue
    threading.Thread(target=_forward, args=(client, target), daemon=True).start()
    threading.Thread(target=_forward, args=(target, client), daemon=True).start()
"""


class SandboxError(RuntimeError):
    """Sandbox setup or execution failed."""


@dataclass
class SandboxCapabilities:
    """What the host's container runtime can do."""

    podman: bool = False
    podman_version: str = ""
    # Can podman create an --internal isolated network? (needs netavark)
    isolated_network: bool = False
    # Can podman create a bridge network with an external route (for
    # the install phase)? (needs netavark)
    build_network: bool = False
    # Can podman do --uidmap userns remapping?
    userns_remap: bool = False
    reason: str = ""


# Capability-probe reachability polling (WO-7.4). A detached probe
# container's app needs a moment to listen after `podman run -d`
# returns; one connect attempt races that startup and reads a
# healthy host as incapable (the reviewer's host: isolated reported
# 1 time in 4 with a single connect, 3 of 3 with a polled one). Poll
# until the deadline instead, and keep the attempt count and the
# last error for the recorded reason, so a refusal shows the polling
# happened rather than one raced refusal.
_PROBE_CONNECT_DEADLINE_S = 10.0
_PROBE_CONNECT_INTERVAL_S = 0.3
# One attempt is time-boxed well under the deadline: a connect into
# an unroutable network is not refused, it hangs — with the whole
# deadline as its timeout it would consume the poll window in a
# single attempt (seen on the GitHub runners: "1 connect attempts
# over 10 s, last error: timed out").
_PROBE_CONNECT_ATTEMPT_TIMEOUT_S = 1.0


def _poll_probe_app(
    container: str, network_name: str, port: int
) -> tuple[str, int, str]:
    """Poll a probe container until the host can connect to it.

    Returns ``(ip, attempts, last_error)``: the container's IP (""
    when it never became readable), how many connect attempts were
    made, and the last attempt's error ("" on success). The IP read
    is polled too — inspect can lag the detached start just like
    the app's listen can.
    """
    ip = ""
    attempts = 0
    last_error = ""
    deadline = time.monotonic() + _PROBE_CONNECT_DEADLINE_S
    while True:
        if not ip:
            ip = _container_ip(container, network_name)
        if ip:
            attempts += 1
            remaining = deadline - time.monotonic()
            attempt_timeout = max(
                0.1, min(remaining, _PROBE_CONNECT_ATTEMPT_TIMEOUT_S)
            )
            try:
                with socket.create_connection(
                    (ip, port), timeout=attempt_timeout
                ):
                    return ip, attempts, ""
            except OSError as e:
                last_error = str(e) or repr(e)
        if time.monotonic() >= deadline:
            return ip, attempts, last_error
        time.sleep(_PROBE_CONNECT_INTERVAL_S)


def _cleanup_probe(network_name: str, container: str = "") -> None:
    """Best-effort removal of a capability probe's network/container.

    Called from a finally: a probe must never leave its network
    behind (a leftover fixed-name network once poisoned every later
    probe), and a cleanup failure must never mask the probe's own
    verdict.
    """
    steps: list[list[str]] = []
    if container:
        steps.append(["stop", "-t", "0", container])
    steps.append(["network", "rm", network_name])
    for args in steps:
        try:
            subprocess.run(PODMAN + args, capture_output=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            pass


def check_sandbox_capabilities() -> SandboxCapabilities:
    """Probe the host for sandbox capabilities.

    Starts transient probe containers/networks; every one is
    removed again before returning (cleanup in a finally, WO-7.4).
    """
    caps = SandboxCapabilities()
    if shutil.which("podman") is None:
        caps.reason = "podman not found"
        return caps
    try:
        proc = subprocess.run(
            PODMAN + ["--version"], capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        caps.reason = f"podman --version failed: {e}"
        return caps
    if proc.returncode != 0:
        caps.reason = "podman --version failed"
        return caps
    caps.podman = True
    caps.podman_version = proc.stdout.strip()
    # Isolated network: start a detached container on an --internal
    # network, look up its IP through the SAME code path a real run
    # uses (JSON-parsed `podman inspect`), and verify the host can
    # actually reach it. Creating the network alone succeeds even when
    # netavark can't set up the namespace; a --rm run exits before its
    # IP can be read; and an IP the host cannot route to is useless.
    # The detached start + IP lookup + TCP connect is the real test.
    # WO-7.4: the IP read and the connect are POLLED to a deadline
    # (a single connect races the probe app's startup), the network
    # and container names are unique per probe (a leftover from a
    # crashed probe once made every later probe report "already
    # exists"), and cleanup runs in a finally.
    suffix = uuid.uuid4().hex[:12]
    net_name = f"pd-capability-probe-{suffix}"
    probe_container = f"pd-capability-probe-app-{suffix}"
    probe_port = 18099
    try:
        proc = subprocess.run(
            PODMAN + ["network", "create", "--internal", net_name],
            capture_output=True, text=True, timeout=60,
        )
        if proc.returncode != 0:
            caps.reason = (
                f"isolated network unavailable: {proc.stderr.strip()[:150]}"
            )
        else:
            run = subprocess.run(
                PODMAN
                + [
                    "run", "-d", "--rm", "--name", probe_container,
                    "--net", net_name,
                    IMAGES["python"], "python3", "-c",
                    f"import http.server, functools; "
                    f"http.server.HTTPServer(('0.0.0.0', {probe_port}), "
                    f"http.server.SimpleHTTPRequestHandler).serve_forever()",
                ],
                capture_output=True, text=True, timeout=90,
            )
            if run.returncode != 0:
                caps.reason = (
                    "isolated network unavailable: "
                    f"{run.stderr.strip()[:150]}"
                )
            else:
                ip, attempts, last_error = _poll_probe_app(
                    probe_container, net_name, probe_port
                )
                if ip and not last_error:
                    caps.isolated_network = True
                elif ip:
                    caps.reason = (
                        "isolated network unavailable: container has IP "
                        f"{ip} but the host cannot reach it: {attempts} "
                        "connect attempts over "
                        f"{_PROBE_CONNECT_DEADLINE_S:.0f} s, last error: "
                        f"{last_error}"
                    )
                else:
                    caps.reason = (
                        "isolated network unavailable: container started "
                        "but its IP could not be read after "
                        f"{_PROBE_CONNECT_DEADLINE_S:.0f} s of polling"
                    )
    except (OSError, subprocess.TimeoutExpired) as e:
        caps.reason = f"isolated network probe failed: {e}"
    finally:
        _cleanup_probe(net_name, probe_container)
    # Userns remapping: try --uidmap on a trivial container.
    try:
        proc = subprocess.run(
            PODMAN
            + [
                "run", "--rm", "--uidmap", "0:1:65536",
                IMAGES["python"], "python3", "-c", "pass",
            ],
            capture_output=True, text=True, timeout=90,
        )
        if proc.returncode == 0:
            caps.userns_remap = True
        elif not caps.reason:
            caps.reason = f"userns remap unavailable: {proc.stderr.strip()[:150]}"
    except (OSError, subprocess.TimeoutExpired) as e:
        if not caps.reason:
            caps.reason = f"userns probe failed: {e}"
    # Build network: a bridge with an external route for the install
    # phase (the install needs the egress proxy). Same netavark
    # machinery as the isolated probe; probed separately because the
    # install must never fall back to --net=host.
    build_net = f"pd-capability-probe-build-{uuid.uuid4().hex[:12]}"
    try:
        proc = subprocess.run(
            PODMAN + ["network", "create", build_net],
            capture_output=True, text=True, timeout=60,
        )
        if proc.returncode == 0:
            run = subprocess.run(
                PODMAN
                + [
                    "run", "--rm", "--net", build_net,
                    IMAGES["python"], "python3", "-c", "pass",
                ],
                capture_output=True, text=True, timeout=90,
            )
            if run.returncode == 0:
                caps.build_network = True
            elif not caps.reason:
                caps.reason = (
                    f"build network unavailable: {run.stderr.strip()[:150]}"
                )
        elif not caps.reason:
            caps.reason = f"build network unavailable: {proc.stderr.strip()[:150]}"
    except (OSError, subprocess.TimeoutExpired) as e:
        if not caps.reason:
            caps.reason = f"build network probe failed: {e}"
    finally:
        _cleanup_probe(build_net)
    return caps


def resolve_network_mode(
    requested: str, caps: SandboxCapabilities | None = None
) -> tuple[str, str]:
    """Resolve the app network mode for a run, ONCE (WO-7.3).

    Returns ``(mode, reason)``: the concrete mode every side of the
    run must use, and why. With ``auto`` the host's capability probe
    decides and the reason records the choice AND the capability
    evidence behind it (the fallback reason when isolated networks
    are unavailable). Explicit modes resolve to themselves, with the
    same refusals a Sandbox enforces when it picks a mode per side:
    ``host`` is refused outright, and ``isolated`` on a host that
    cannot create isolated networks is refused with the probe's
    reason. The orchestrator calls this once per measured run and
    hands the resolved mode to every side; ``Sandbox`` delegates
    here too, so there is exactly one implementation of the choice.
    """
    if caps is None:
        caps = check_sandbox_capabilities()
    if requested == "host":
        raise SandboxError(
            "network_mode=host is refused: the app must not share "
            "the host network"
        )
    if requested == "auto":
        if caps.isolated_network:
            return (
                "isolated",
                "auto: isolated network available "
                "(sandbox capability probe passed)",
            )
        return (
            "none+socket",
            f"auto: {caps.reason or 'isolated network unavailable'}; "
            "falling back to none+socket",
        )
    if requested == "isolated" and not caps.isolated_network:
        raise SandboxError(
            "network_mode=isolated requested but the host cannot "
            f"create isolated networks: {caps.reason}"
        )
    if requested not in ("isolated", "none+socket"):
        raise SandboxError(f"unknown network_mode {requested!r}")
    return requested, f"explicit network_mode={requested}"


@dataclass
class SandboxConfig:
    """Configuration for sandboxed runs."""

    image: str = IMAGES["python"]
    # Extra read-only mounts (absolute host paths). Empty by default:
    # only the checkout is mounted.
    extra_mounts: list[tuple[str, str]] = field(default_factory=list)
    # Network mode: "auto" (isolated bridge if available, else none+socket),
    # "isolated", "none+socket". "host" is refused for unattended runs.
    network_mode: str = "auto"
    # Fail closed if userns remapping is unavailable. The default is
    # True: a measured run is refused on a host that cannot remap
    # container root away from host root.
    require_userns: bool = True
    # Explicit dev-only opt-out of the userns requirement. When set, the
    # sandbox falls back to --user 65534 (nobody) with a logged warning,
    # and the opt-out is written into the evidence record. This is the
    # only way to run without userns remapping; it is never silent.
    allow_no_userns: bool = False
    # Host path to a CA certificate for the install phase (a
    # TLS-intercepting egress proxy's CA). Mounted read-only for the
    # install ONLY when set AND the file exists. None (the default)
    # means no CA mount; the install then relies on the image's own
    # trust store. The evidence record notes whether a CA was used.
    ca_cert_host: str | None = None


def _podman_base() -> list[str]:
    return list(PODMAN)


def _head_tail(
    text: str,
    head_lines: int = 40,
    head_chars: int = 4000,
    tail_chars: int = 2000,
) -> str:
    """Keep the HEAD and the TAIL of long command output.

    The old capture kept only the last 2000 characters: a tool that
    prints its real error first and then pages of usage/help text
    (npm does exactly this on EUSAGE) lost the error line entirely,
    which hid the actual cause of a failing install behind the help
    text (Addendum 33). Keep the first ``head_lines`` lines (capped
    at ``head_chars`` characters — the total is capped too,
    Addendum 36: forty enormous lines are still enormous) plus the
    last ``tail_chars`` characters, with an elision marker between.
    """
    lines = text.splitlines()
    if len(lines) <= head_lines and len(text) <= head_chars + tail_chars:
        return text
    head = "\n".join(lines[:head_lines])[:head_chars]
    tail = text[-tail_chars:]
    elided = max(len(lines) - head_lines - tail.count("\n"), 0)
    return f"{head}\n[… {elided} lines elided …]\n{tail}"


def _run_rc(
    args: list[str], timeout: int, logs: list[str]
) -> int:
    """Run a podman command and return its real exit code.

    Output is appended to ``logs``; a failure never raises. Synthetic
    codes for "no process ran to completion": -1 on timeout, -2 when
    podman is missing — never confused with a container's own code.
    """
    logs.append(f"$ {' '.join(args)}")
    try:
        proc = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout
        )
    except FileNotFoundError:
        logs.append("podman not found")
        return -2
    except subprocess.TimeoutExpired:
        logs.append(f"timed out after {timeout}s")
        return -1
    if proc.stdout:
        logs.append(_head_tail(proc.stdout))
    if proc.stderr:
        logs.append(_head_tail(proc.stderr))
    return proc.returncode


def _run(
    args: list[str], timeout: int, logs: list[str]
) -> tuple[bool, str]:
    """Run a podman command; never raises. Returns (ok, reason)."""
    rc = _run_rc(args, timeout, logs)
    if rc == -2:
        return False, "podman not found"
    if rc == -1:
        return False, f"timed out after {timeout}s"
    if rc != 0:
        return False, f"exit {rc}"
    return True, ""


def _exit_code_from_run(ok: bool, reason: str) -> int:
    """The exit code behind a _run result (Addendum 31 recording).

    _run's failure reasons are this module's own contract: "exit N"
    carries the container's code; the two synthetic failures map to
    _run_rc's synthetic codes (-2 podman missing, -1 timeout).
    """
    if ok:
        return 0
    if reason.startswith("exit "):
        try:
            return int(reason[5:])
        except ValueError:
            return -1
    if reason == "podman not found":
        return -2
    return -1


def _tcp_reachable(host: str, port: int, timeout: int = 10) -> bool:
    """True when a TCP connection to host:port succeeds."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _container_ip(name: str, network_name: str) -> str:
    """Read a container's IP on a named network via `podman inspect`.

    Parses the JSON in Python: Go templates cannot address hyphenated
    network names (e.g. "pd-isolated") with dot syntax. Returns "" when
    the IP cannot be read. This is the single code path for IP lookup;
    the capability probe and the real start both use it.
    """
    try:
        proc = subprocess.run(
            PODMAN + ["inspect", name],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if proc.returncode != 0:
        return ""
    try:
        data = json.loads(proc.stdout)
        ip = data[0]["NetworkSettings"]["Networks"][network_name]["IPAddress"]
        return ip if isinstance(ip, str) else ""
    except (json.JSONDecodeError, KeyError, IndexError, TypeError):
        return ""


class _TcpToUnixProxy:
    """Host-side TCP proxy: 127.0.0.1:<port> -> Unix socket.

    Lets the executor (TCP) reach an app in a --net=none container
    (Unix socket in the shared checkout).
    """

    def __init__(self, sock_path: Path) -> None:
        self.sock_path = sock_path
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(128)
        self.port = int(self._srv.getsockname()[1])
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _serve(self) -> None:
        self._srv.settimeout(1.0)
        while not self._stop.is_set():
            try:
                client, _ = self._srv.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            threading.Thread(
                target=self._bridge, args=(client,), daemon=True
            ).start()

    def _bridge(self, client: socket.socket) -> None:
        try:
            ups = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            ups.settimeout(10)
            ups.connect(str(self.sock_path))
            ups.settimeout(None)
        except OSError:
            client.close()
            return
        t1 = threading.Thread(
            target=_copy, args=(client, ups), daemon=True
        )
        t2 = threading.Thread(
            target=_copy, args=(ups, client), daemon=True
        )
        t1.start()
        t2.start()
        t1.join()
        t2.join()

    def stop(self) -> None:
        self._stop.set()
        try:
            self._srv.close()
        except OSError:
            pass


def _copy(src: socket.socket, dst: socket.socket) -> None:
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        try:
            src.shutdown(socket.SHUT_RD)
        except OSError:
            pass


@dataclass
class Sandbox:
    """Runs a checkout's install and app in isolated containers."""

    config: SandboxConfig
    log: list[str] = field(default_factory=list)
    _caps: SandboxCapabilities | None = field(default=None, repr=False)
    _network_name: str | None = field(default=None, repr=False)
    _build_network_name: str | None = field(default=None, repr=False)
    # Isolation actually in effect, for the evidence record. Set by
    # _user_args() and start()/install().
    userns_mode: str | None = field(default=None, repr=False)
    userns_opt_out: bool = field(default=False, repr=False)
    app_network_mode: str | None = field(default=None, repr=False)
    install_network: str | None = field(default=None, repr=False)
    # The failed install step's OWN captured output (Addendum 36):
    # set by install_steps when a step fails, so the provision
    # reason's first-error scan reads that step only — never an
    # earlier step's or another phase's error lines.
    failed_step_output: str | None = field(default=None, repr=False)

    def _capabilities(self) -> SandboxCapabilities:
        if self._caps is None:
            self._caps = check_sandbox_capabilities()
        return self._caps

    def _resolve_network_mode(self) -> str:
        """Pick the network mode for the app container.

        Delegates to the module-level resolve_network_mode (WO-7.3):
        one implementation of the choice, whether the orchestrator
        resolved the run's mode up front or a lone Sandbox is used.
        """
        mode, _reason = resolve_network_mode(
            self.config.network_mode, self._capabilities()
        )
        return mode

    def _ensure_isolated_network(self) -> str:
        """Create (once) an --internal bridge network; return its name."""
        if self._network_name:
            return self._network_name
        # Deterministic name per checkout hash would be nicer; a fixed
        # name with create-if-missing is enough for one sandbox.
        name = "pd-isolated"
        ok, reason = _run(
            _podman_base() + ["network", "exists", name], 30, self.log
        )
        if not ok:
            ok, reason = _run(
                _podman_base() + ["network", "create", "--internal", name],
                60,
                self.log,
            )
            if not ok:
                raise SandboxError(
                    f"could not create isolated network: {reason}"
                )
        self._network_name = name
        self.log.append(f"isolated network: {name} (--internal, no external route)")
        return name

    def _ensure_build_network(self) -> str:
        """Create (once) a bridge network for the install phase.

        The install runs untrusted install scripts, so it must not use
        ``--net=host``: in the host namespace the scripts can reach
        host-loopback services and metadata endpoints. The build network
        is a normal bridge (external route for the egress proxy).
        """
        if self._build_network_name:
            return self._build_network_name
        name = "pd-build"
        ok, reason = _run(
            _podman_base() + ["network", "exists", name], 30, self.log
        )
        if not ok:
            ok, reason = _run(
                _podman_base() + ["network", "create", name],
                60,
                self.log,
            )
            if not ok:
                raise SandboxError(
                    "could not create build network for the install phase: "
                    f"{reason}; measured runs are refused without an "
                    "isolated install network (the install must not use "
                    "--net=host)"
                )
        self._build_network_name = name
        self.log.append(f"build network: {name} (bridge, external route for proxy)")
        return name

    def _user_args(self) -> list[str]:
        """Userns remapping if available, else fail closed or explicit opt-out.

        Fail-closed by default (``require_userns=True``): when the host
        cannot remap container root away from host root, the run is
        refused. The only way past is the explicit dev flag
        ``allow_no_userns=True``, which falls back to ``--user 65534``
        (nobody) with a logged warning and records the opt-out on the
        Sandbox (``userns_opt_out``) for the evidence record.
        """
        caps = self._capabilities()
        if caps.userns_remap:
            self.userns_mode = "userns-remap"
            self.userns_opt_out = False
            self.log.append("userns remapping: --uidmap 0:1:65536")
            return ["--uidmap", "0:1:65536", "--cap-drop=ALL",
                    "--security-opt=no-new-privileges"]
        if self.config.require_userns and not self.config.allow_no_userns:
            raise SandboxError(
                "userns remapping unavailable on this host "
                f"({caps.reason}); require_userns=True refuses to run "
                "(explicit dev opt-out: allow_no_userns=True)"
            )
        self.userns_mode = "nobody-fallback"
        self.userns_opt_out = self.config.allow_no_userns
        self.log.append(
            "WARNING: userns remapping unavailable "
            f"({caps.reason}); falling back to --user 65534 "
            "(nobody). Container root still maps to host root."
            + (" Explicit dev opt-out (allow_no_userns=True)."
               if self.config.allow_no_userns else "")
        )
        return ["--user", SANDBOX_UID, "--cap-drop=ALL",
                "--security-opt=no-new-privileges"]

    def _common_args(
        self, checkout: Path, name: str, with_network: bool, detach: bool = False,
        rm: bool = True,
    ) -> list[str]:
        cmd = ["run", "-d"] if detach else ["run"]
        args = _podman_base() + cmd
        # S2: app containers keep --rm off so `podman logs` can diagnose
        # a crash after the fact; app.stop() (rm -f) cleans up.
        # One-shot install containers keep --rm.
        if rm:
            args += ["--rm"]
        args += [
            "--name",
            name,
        ]
        if with_network:
            # Install phase: bridge network with the proxy (never
            # --net=host: install scripts are untrusted code and must
            # not reach host-loopback services or metadata endpoints).
            build_net = self._ensure_build_network()
            args += ["--net", build_net]
            self.install_network = f"bridge:{build_net}"
        else:
            mode = self._resolve_network_mode()
            if mode == "isolated":
                args += ["--net", self._ensure_isolated_network()]
            else:
                args += ["--net=none"]
            self.app_network_mode = mode
        args += self._user_args()
        args += [
            "-v",
            f"{checkout}:/checkout",
            "-w",
            "/checkout",
        ]
        if with_network:
            # Install phase only: mount the platform CA (if configured
            # and present) so pip/npm can verify a TLS-intercepting
            # egress proxy.
            if self._ca_available():
                args += [
                    "-v",
                    f"{self.config.ca_cert_host}:{PLATFORM_CA_CONTAINER}:ro",
                ]
        for host_path, container_path in self.config.extra_mounts:
            args += ["-v", f"{host_path}:{container_path}:ro"]
        return args

    def _ca_available(self) -> bool:
        """True when a CA cert is configured and the file exists."""
        ca = self.config.ca_cert_host
        return ca is not None and Path(ca).is_file()

    def _prepare_checkout(self, checkout: Path) -> None:
        """Make the checkout usable by the sandbox user.

        The checkout is a disposable snapshot copy; granting read/write
        is safe and required for the de-privileged workload.
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
        steps = [{"phase": "install", "command": c} for c in commands]
        results = self.install_steps(checkout, steps, env, proxy_env, timeout)
        return len(results) == len(commands) and all(
            r["exit_code"] == 0 for r in results
        )

    def install_steps(
        self,
        checkout: Path,
        steps: list[dict],
        env: dict[str, str],
        proxy_env: dict[str, str],
        timeout: int = 600,
    ) -> list[dict]:
        """Run install-phase step commands, one container each, WITH
        network (proxy enabled), and report every step's exit code.

        ``steps`` are descriptors: ``{"phase": str, "command": str,
        "env": {..} optional}`` — a step's own env rides on top of the
        base env for that step's container only (e.g.
        COREPACK_ENABLE_DOWNLOAD_PROMPT=0 for a pinned Node install).
        Returns ``[{"phase": ..., "exit_code": int}]`` for the steps
        ATTEMPTED, stopping after the first failure: a step that
        never ran is not reported as run (Addendum 31).
        ``env`` is the secret-free contract env; ``proxy_env`` carries
        the egress proxy for the install only.
        """
        self._prepare_checkout(checkout)
        full_env = _strip_secret_env(dict(env))
        full_env.update(proxy_env)
        # Trust the platform CA for the install phase, but only when a
        # CA is configured and present. Otherwise the image's own trust
        # store is used.
        ca_used = self._ca_available()
        if ca_used:
            full_env["SSL_CERT_FILE"] = PLATFORM_CA_CONTAINER
            full_env["REQUESTS_CA_BUNDLE"] = PLATFORM_CA_CONTAINER
            full_env["NODE_EXTRA_CA_CERTS"] = PLATFORM_CA_CONTAINER
            full_env["PIP_CERT"] = PLATFORM_CA_CONTAINER
        self.log.append(f"install: ca_cert_used={ca_used}")
        # The sandbox user has no home; give it a writable one inside
        # the checkout.
        full_env["HOME"] = "/checkout/.sandbox-home"
        results: list[dict] = []
        self.failed_step_output = None
        for i, step in enumerate(steps):
            step_env = dict(full_env)
            step_env.update(step.get("env") or {})
            # Strip secrets AFTER the step env is merged (Addendum
            # 33): a blocklisted key arriving via a step's own env
            # must not reach the container either.
            step_env = _strip_secret_env(step_env)
            args = self._common_args(checkout, f"sandbox-install-{i}", True)
            for k, v in step_env.items():
                args += ["-e", f"{k}={v}"]
            args += [self.config.image, "bash", "-c", step["command"]]
            log_mark = len(self.log)
            ok, reason = _run(args, timeout, self.log)
            rc = _exit_code_from_run(ok, reason)
            results.append({"phase": step["phase"], "exit_code": rc})
            if rc != 0:
                # Addendum 36: keep THIS step's output slice — the
                # provision reason scans it, and only it.
                self.failed_step_output = "\n".join(self.log[log_mark:])
                self.log.append(
                    f"install step {i} ({step['phase']}) failed: exit {rc}"
                )
                break
        return results

    def start(
        self,
        checkout: Path,
        command: str,
        env: dict[str, str],
        port: int,
        name: str,
    ) -> SandboxApp:
        """Start the app with the network cut.

        The app never gets ``--net=host``. On ``isolated`` mode it runs
        on an --internal bridge (executor reaches it via container IP).
        On ``none+socket`` mode it runs with ``--net=none`` and a Unix
        socket bridge (executor reaches it via a host-side TCP proxy).
        No proxy variables are set: the app has no external network.
        Returns a SandboxApp; the caller must stop() it.
        """
        self._prepare_checkout(checkout)
        mode = self._resolve_network_mode()
        env = _strip_secret_env(dict(env))
        env.setdefault("HOME", "/checkout/.sandbox-home")
        # No proxy variables, ever, for the app.
        env = {k: v for k, v in env.items() if "proxy" not in k.lower()}
        run_env = dict(env)
        run_env["PORT"] = str(port)

        if mode == "isolated":
            app = self._start_isolated(checkout, command, run_env, name)
        else:
            app = self._start_none_socket(checkout, command, run_env, port, name)
        app.sandbox_evidence = self._isolation_evidence(mode)
        return app

    def isolation_evidence_best_effort(self) -> dict[str, object]:
        """Isolation evidence even when the app never started (S3).

        Never raises; returns {} when the network mode cannot be
        resolved (e.g. the sandbox was never usable on this host).
        """
        try:
            mode = self._resolve_network_mode()
        except Exception:
            return {}
        return dict(self._isolation_evidence(mode))

    def app_logs(self, name: str, tail_lines: int = 100) -> str:
        """Tail of a container's stdout+stderr, for failure diagnosis (S2).

        Never raises; returns "" when the logs cannot be read (e.g. the
        container never existed).
        """
        try:
            proc = subprocess.run(
                _podman_base() + ["logs", "--tail", str(tail_lines), name],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired):
            return ""
        if proc.returncode != 0:
            return ""
        out = (proc.stdout or "") + (proc.stderr or "")
        return out[-8000:]

    def _isolation_evidence(self, app_mode: str) -> dict[str, object]:
        """Isolation actually in effect, for the evidence record."""
        uid = 0 if self.userns_mode == "userns-remap" else 65534
        return {
            "app_network_mode": app_mode,
            "install_network": self.install_network,
            "userns_mode": self.userns_mode,
            "userns_opt_out": self.userns_opt_out,
            "app_uid": uid,
            "install_ca_used": self._ca_available(),
        }

    def _start_isolated(
        self,
        checkout: Path,
        command: str,
        run_env: dict[str, str],
        name: str,
    ) -> SandboxApp:
        # S2: no --rm (see _common_args): the container must persist so
        # a crash can be diagnosed from its logs; stop() removes it.
        args = self._common_args(checkout, name, False, detach=True, rm=False)
        for k, v in _strip_secret_env(run_env).items():
            args += ["-e", f"{k}={v}"]
        args += [self.config.image, "bash", "-c", command]
        ok, reason = _run(args, 60, self.log)
        if not ok:
            raise SandboxError(f"sandbox start failed: {reason}")
        # Container IP on the isolated network, through the shared
        # lookup (same path the capability probe exercises).
        network_name = self._ensure_isolated_network()
        ip = _container_ip(name, network_name)
        if not ip:
            self.stop(name)
            raise SandboxError("sandbox start: no container IP on isolated network")
        self.log.append(f"sandbox app container IP: {ip} (isolated network)")
        port = int(run_env["PORT"])
        return SandboxApp(
            name=name,
            sandbox=self,
            target_url=f"http://{ip}:{port}",
            _proxy=None,
        )

    def _start_none_socket(
        self,
        checkout: Path,
        command: str,
        run_env: dict[str, str],
        port: int,
        name: str,
    ) -> SandboxApp:
        # B2 (Addendum 16): the bridge Unix socket must live on a
        # SHORT host path. AF_UNIX paths are limited to 107 bytes on
        # Linux; the checkout path in CI is 121 chars, so a socket
        # inside the checkout can never be connected to. Use a short
        # per-run dir under /tmp instead, bind-mounted at /pd-sock.
        sock_dir = Path(tempfile.mkdtemp(prefix="pd-", dir="/tmp"))
        os.chmod(sock_dir, 0o777)
        sock_path = sock_dir / "app.sock"
        if len(str(sock_path).encode()) > 100:
            raise SandboxError(
                f"bridge socket path too long: {sock_path}"
            )
        # Bridge script lives in the checkout (path length doesn't
        # matter for a regular file).
        script_path = checkout / _BRIDGE_SCRIPT_NAME
        script_path.write_text(_BRIDGE_SCRIPT, encoding="utf-8")
        # The script was written after _prepare_checkout's chmod; make it
        # readable by the sandbox user.
        try:
            subprocess.run(
                ["chmod", "a+rX", str(script_path)],
                capture_output=True, timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
        # The app binds to the container's own loopback (isolated).
        # The bridge forwards the Unix socket at /pd-sock/app.sock
        # to it.
        wrapped = (
            f"(python3 /checkout/{_BRIDGE_SCRIPT_NAME} "
            f"/pd-sock/app.sock {port} &); "
            f"{command}"
        )
        # S2: no --rm: the container must persist so a crash can be
        # diagnosed from its logs; app.stop() (rm -f) cleans up.
        args = _podman_base() + [
            "run", "-d", "--name", name, "--net=none",
        ]
        args += self._user_args()
        args += ["-v", f"{checkout}:/checkout", "-w", "/checkout"]
        # The short socket dir, visible to both host and container.
        args += ["-v", f"{sock_dir}:/pd-sock"]
        for host_path, container_path in self.config.extra_mounts:
            args += ["-v", f"{host_path}:{container_path}:ro"]
        for k, v in _strip_secret_env(run_env).items():
            args += ["-e", f"{k}={v}"]
        args += [self.config.image, "bash", "-c", wrapped]
        ok, reason = _run(args, 60, self.log)
        if not ok:
            self._cleanup_sock_dir(sock_dir)
            raise SandboxError(f"sandbox start failed: {reason}")
        # B2: verify the bridge socket becomes ready. Fail fast with
        # the container's logs (and the last connect error) instead
        # of a silent 120s readiness timeout.
        last_err = self._wait_for_socket(sock_path, name, timeout=15)
        if last_err is not None:
            app_log = self.app_logs(name)
            self.stop(name)
            self._cleanup_sock_dir(sock_dir)
            raise SandboxError(
                "sandbox start: bridge socket "
                f"{sock_path.name} never became ready: {last_err}. "
                f"Container log tail:\n{app_log[-2000:] if app_log else '(empty)'}"
            )
        self.log.append(f"bridge socket ready: {sock_path}")
        # Host-side TCP proxy: executor -> Unix socket.
        proxy = _TcpToUnixProxy(sock_path)
        proxy.start()
        self.log.append(
            f"sandbox app: --net=none, Unix socket bridge, "
            f"executor proxy at 127.0.0.1:{proxy.port}"
        )
        return SandboxApp(
            name=name,
            sandbox=self,
            target_url=f"http://127.0.0.1:{proxy.port}",
            _proxy=proxy,
            _bridge_sock=sock_path,
            _bridge_sock_dir=sock_dir,
        )

    def _cleanup_sock_dir(self, sock_dir: Path) -> None:
        """Remove a per-run bridge socket dir (best effort)."""
        try:
            shutil.rmtree(sock_dir, ignore_errors=True)
        except OSError:
            pass

    def _wait_for_socket(
        self, sock_path: Path, container_name: str, timeout: int = 15
    ) -> str | None:
        """Wait for the bridge socket to appear and accept connections.

        Returns None on success, or the last connect error message on
        timeout. The caller reports the error (never a generic
        message) so the real cause is visible.
        """
        import socket as _socket
        import stat

        last_err = "socket file never appeared"
        deadline = time.time() + timeout
        while time.time() < deadline:
            if sock_path.exists():
                try:
                    if stat.S_ISSOCK(sock_path.stat().st_mode):
                        # Verify someone is actually listening.
                        s = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
                        s.settimeout(2)
                        try:
                            s.connect(str(sock_path))
                            s.close()
                            return None
                        except OSError as e:
                            last_err = (
                                f"socket present but connect failed: "
                                f"{type(e).__name__}: {e}"
                            )
                        finally:
                            try:
                                s.close()
                            except OSError:
                                pass
                    else:
                        last_err = "path exists but is not a socket"
                except OSError as e:
                    last_err = f"stat failed: {type(e).__name__}: {e}"
            time.sleep(0.5)
        self.log.append(
            f"bridge socket {sock_path.name} did not become ready within "
            f"{timeout}s: {last_err}"
        )
        return last_err

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
    target_url: str
    _proxy: _TcpToUnixProxy | None = None
    # Isolation actually in effect for this app (network mode, userns
    # mode, uid). Set by Sandbox.start(); goes in the evidence record.
    sandbox_evidence: dict[str, object] = field(default_factory=dict)
    # Host path to the bridge Unix socket (none+socket mode only).
    # Lets the orchestrator distinguish "bridge down" from "app down"
    # in the readiness record (B2).
    _bridge_sock: Path | None = None
    # Host dir holding the bridge socket (per-run, under /tmp).
    # Removed on stop() so no /tmp/pd-* is left behind.
    _bridge_sock_dir: Path | None = None

    @property
    def port(self) -> int:
        """The executor-facing port (host proxy or container port)."""
        return int(self.target_url.rsplit(":", 1)[1])

    def bridge_socket_present(self) -> bool:
        """True when the bridge socket exists (none+socket mode).

        For isolated mode (no socket), always True. Lets the
        orchestrator report "bridge down" vs "app down" separately.
        """
        if self._bridge_sock is None:
            return True
        try:
            import stat
            return stat.S_ISSOCK(self._bridge_sock.stat().st_mode)
        except OSError:
            return False

    def stop(self) -> None:
        if self._proxy is not None:
            self._proxy.stop()
        self.sandbox.stop(self.name)
        # Clean up the per-run socket dir on every exit path.
        if self._bridge_sock_dir is not None:
            try:
                shutil.rmtree(self._bridge_sock_dir, ignore_errors=True)
            except OSError:
                pass
            self._bridge_sock_dir = None

    def wait_ready(self, path: str = "/health", timeout: int = 60) -> bool:
        """Poll the app's readiness endpoint via the executor path."""
        import urllib.request

        deadline = time.time() + timeout
        url = self.target_url.rstrip("/") + path
        while time.time() < deadline:
            try:
                urllib.request.urlopen(url, timeout=5).read()
                return True
            except Exception:
                time.sleep(1)
        return False


def check_podman() -> tuple[bool, str]:
    """Is podman available for sandboxing?"""
    caps = check_sandbox_capabilities()
    if not caps.podman:
        return False, caps.reason or "podman not found"
    return True, caps.podman_version


def sandbox_evidence() -> dict[str, object]:
    """Capability evidence for the record: what isolation was available."""
    caps = check_sandbox_capabilities()
    return {
        "podman": caps.podman,
        "podman_version": caps.podman_version,
        "isolated_network": caps.isolated_network,
        "build_network": caps.build_network,
        "userns_remap": caps.userns_remap,
        "reason": caps.reason,
    }


def assert_measurement_gate(
    config: SandboxConfig | None = None,
) -> dict[str, object]:
    """Fail-closed gate for measured runs: refuse unless the host can
    meet the isolation requirements.

    A measured run is refused when:
    - podman is unavailable;
    - the host cannot remap container root away from host root
      (``--uidmap``), unless the explicit dev flag
      ``allow_no_userns=True`` is set;
    - the host cannot create a bridge network for the install phase
      (the install must not use ``--net=host``).

    Raises SandboxError on refusal. Returns the capability probe
    result as a dict (for the evidence record's provenance). The
    orchestrator calls this before provisioning any measured side.
    """
    cfg = config or SandboxConfig()
    caps = check_sandbox_capabilities()
    if not caps.podman:
        raise SandboxError(
            f"measured run refused: no container runtime ({caps.reason})"
        )
    if not caps.userns_remap and not cfg.allow_no_userns:
        raise SandboxError(
            "measured run refused: userns remapping unavailable on this host "
            f"({caps.reason}); require_userns=True is the default "
            "(explicit dev opt-out: allow_no_userns=True)"
        )
    if not caps.build_network:
        raise SandboxError(
            "measured run refused: no bridge network for the install phase "
            f"({caps.reason}); the install must not use --net=host"
        )
    return {
        "podman": caps.podman,
        "podman_version": caps.podman_version,
        "isolated_network": caps.isolated_network,
        "build_network": caps.build_network,
        "userns_remap": caps.userns_remap,
        "reason": caps.reason,
    }
