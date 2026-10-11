"""Addendum 18 tests: guards for the S1-S4 / B1-B3 sandbox fixes.

Tests-only round: no product code changes. Every test here drives the
REAL product code podman-free (stubs only replace the container runtime)
and is verified BOTH ways: it FAILS with its planted bug (or on the
pre-fix code) and PASSES on the branch. The planted-bug procedure for
each test is in its docstring.

The previous round's regression test
(test_bridge_socket_path_is_short_despite_long_checkout) is deleted: it
re-implemented the fix inside the test and passed on the broken code.
These tests drive the real `_start_none_socket` / `_wait_for_socket` /
`_provision_side_sandbox` instead.
"""
import shutil
import socket as pysocket
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from proofdeploy.sandbox import (
    Sandbox,
    SandboxCapabilities,
    SandboxConfig,
    SandboxError,
)

# ---------------------------------------------------------------------------
# Shared podman-free scaffolding.
# ---------------------------------------------------------------------------


def _fake_caps(**overrides) -> SandboxCapabilities:
    caps = SandboxCapabilities(
        podman=True,
        podman_version="podman version 4.9.3",
        isolated_network=False,
        build_network=True,
        userns_remap=True,
        reason="",
    )
    for k, v in overrides.items():
        setattr(caps, k, v)
    return caps


def _patch_caps(monkeypatch, **overrides):
    """Stub Sandbox._capabilities so _user_args() works without podman."""
    caps = _fake_caps(**overrides)
    monkeypatch.setattr(Sandbox, "_capabilities", lambda self: caps)
    return caps


class _StopAfterCapture(Exception):
    """Raised by _CapturingRun after recording the podman args."""


class _CapturingRun:
    """Fake proofdeploy.sandbox._run: records args, then stops the call.

    Lets a test inspect the exact podman command line the real code
    built, without starting any container.
    """

    def __init__(self):
        self.calls: list[list[str]] = []

    def __call__(self, args, timeout, logs):
        self.calls.append(list(args))
        logs.append(f"$ {' '.join(args)}")
        raise _StopAfterCapture()


class _OkRun:
    """Fake _run: records args, always reports success."""

    def __init__(self):
        self.calls: list[list[str]] = []

    def __call__(self, args, timeout, logs):
        self.calls.append(list(args))
        logs.append(f"$ {' '.join(args)}")
        return True, ""


def _volumes(args: list[str]) -> dict[str, str]:
    """Map container mount point -> host path from podman -v args."""
    mounts: dict[str, str] = {}
    for i, a in enumerate(args):
        if a == "-v" and i + 1 < len(args):
            host, _, container = args[i + 1].partition(":")
            mounts[container] = host
    return mounts


def _bash_command(args: list[str]) -> str:
    """The shell command passed to `bash -c` in the podman args."""
    i = args.index("-c")
    return args[i + 1]


# ---------------------------------------------------------------------------
# Test 1: the bridge socket lives in a short dir outside the checkout.
# Replaces the tautological test_bridge_socket_path_is_short_despite_long_checkout.
# ---------------------------------------------------------------------------


def test_bridge_socket_mount_is_short_and_outside_checkout(monkeypatch, tmp_path):
    """B2/Addendum 17 regression: the bridge socket must live in a short
    per-run directory OUTSIDE the checkout, bind-mounted at /pd-sock.

    AF_UNIX paths are limited to 107 bytes on Linux; the CI checkout
    path was 121 chars, so a socket inside the checkout could never be
    connected to ("AF_UNIX path too long"). Drives the REAL
    `_start_none_socket` with a >107-byte checkout path. The fake _run
    records the podman args and stops before any container starts, so no
    podman is needed.

    Planted bug: socket back in the checkout (dd7228a) -> no /pd-sock
    mount in the recorded args -> FAIL. Verified: fails on dd7228a,
    passes on the branch.
    """
    from proofdeploy import sandbox as sandbox_mod

    _patch_caps(monkeypatch)
    long_checkout = tmp_path / ("c" * 100) / ("d" * 100)
    long_checkout.mkdir(parents=True)
    assert len(str(long_checkout).encode()) > 107, "test setup: path not long"

    cap = _CapturingRun()
    monkeypatch.setattr(sandbox_mod, "_run", cap)

    sb = Sandbox(SandboxConfig(image="python:3.12-slim"))
    with pytest.raises(_StopAfterCapture):
        sb._start_none_socket(
            long_checkout, "python3 app.py", {}, 8080, "pd-test"
        )
    assert len(cap.calls) == 1, f"expected one podman run, got {cap.calls}"
    args = cap.calls[0]

    mounts = _volumes(args)
    assert "/pd-sock" in mounts, (
        "no /pd-sock mount in podman args (socket would live in the "
        f"checkout): {args}"
    )
    sock_dir = mounts["/pd-sock"]
    try:
        assert not sock_dir.startswith(str(long_checkout)), (
            f"socket dir inside the checkout: {sock_dir}"
        )
        sock_path = str(Path(sock_dir) / "app.sock")
        assert len(sock_path.encode()) <= 100, (
            f"bridge socket path too long: {sock_path}"
        )
        cmd = _bash_command(args)
        assert "/pd-sock/app.sock" in cmd, (
            f"bridge not told to use /pd-sock/app.sock: {cmd}"
        )
        assert ".pd-app.sock" not in cmd, (
            f"bridge still references the checkout socket: {cmd}"
        )
    finally:
        shutil.rmtree(sock_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Test 2 (SB3): non-requirements.txt lockfiles fail closed in the sandbox.
# ---------------------------------------------------------------------------


def _stub_provision_side(monkeypatch, snap: Path, install=None):
    """Drive Orchestrator._provision_side_sandbox podman-free.

    Stubs git/snapshot/contract/podman/runtime checks and the sandbox
    capabilities; the sandbox install is stubbed (or the given fake).
    Returns (orchestrator, side).
    """
    from proofdeploy import orchestrator as orch_mod
    from proofdeploy.orchestrator import MeasureConfig, Orchestrator
    from proofdeploy.sandbox import Sandbox
    from proofdeploy.yml import RepoContract

    monkeypatch.setattr(orch_mod, "_git", lambda *a: "fakesha1234567890abcdef")
    monkeypatch.setattr(
        orch_mod,
        "create_snapshot",
        lambda **kw: SimpleNamespace(dir=snap),
    )
    contract = RepoContract(
        build="", start="python app.py", readiness="/health", env={}
    )
    monkeypatch.setattr(orch_mod, "load_contract", lambda p: contract)
    monkeypatch.setattr(orch_mod, "check_podman", lambda: (True, ""))
    monkeypatch.setattr(
        orch_mod,
        "check_sandbox_runtime",
        lambda *a: ("3.12.1", "sha256:fake", None),
    )
    monkeypatch.setattr(
        Sandbox, "_capabilities", lambda self: _fake_caps()
    )
    if install is not None:
        monkeypatch.setattr(Sandbox, "install", install)

    cfg = MeasureConfig(
        repo_dir=snap,
        output_dir=snap / "out",
        workdir=snap / "work",
        no_skill=True,
    )
    orch = Orchestrator(cfg)
    unit_dir = snap / "unit"
    unit_dir.mkdir(exist_ok=True)
    return orch


@pytest.mark.parametrize("lockfile", ["uv.lock", "poetry.lock"])
def test_sandbox_rejects_non_requirements_lockfile(
    monkeypatch, tmp_path, lockfile
):
    """SB3: a non-requirements.txt lockfile fails closed in the sandbox
    path: BUILD_FAILED with "not yet supported in the sandbox".

    The sandbox pip flow can only install requirements.txt; anything
    else must fail closed, never half-provision. Podman-free: drives
    the real `_provision_side_sandbox` with stubbed git/snapshot/
    contract/podman/runtime checks (the install is never reached).

    Planted bug: remove the `lockfile_name != "requirements.txt"`
    check -> the run proceeds past lockfile selection and the reason is
    no longer "not yet supported in the sandbox" -> FAIL.
    """
    import hashlib

    from proofdeploy.runner import ProvisionStatus
    from proofdeploy.sandbox import Sandbox, SandboxError

    snap = tmp_path / "snap"
    snap.mkdir()
    (snap / "requirements.txt").write_text("# no third-party deps\n")
    (snap / lockfile).write_text("fake lockfile content\n")

    orch = _stub_provision_side(
        monkeypatch, snap, install=_CaptureInstall()
    )

    def _stop_after_install(self, *a, **k):
        raise SandboxError("test stops after install")

    monkeypatch.setattr(Sandbox, "start", _stop_after_install)
    side = orch._provision_side_sandbox("fakerev", 18080, snap / "unit", "t")
    logs = "\n".join(side.provision.logs)

    assert side.provision.status == ProvisionStatus.BUILD_FAILED, logs
    assert side.provision.reason == "not yet supported in the sandbox", logs
    assert lockfile in logs, logs
    # S3: the lockfile hash is recorded even on this failure.
    expected_sha = hashlib.sha256(b"fake lockfile content\n").hexdigest()
    assert side.provision.lockfile_sha256 == expected_sha


# ---------------------------------------------------------------------------
# Test 3 (SB4): the real connect error reaches the failure message.
# ---------------------------------------------------------------------------


def test_start_reports_concrete_socket_error(monkeypatch, tmp_path):
    """SB4: when the bridge socket exists but refuses connections, the
    start failure must report the CONCRETE connect error, never a generic
    message.

    Addendum 17's lesson: `_wait_for_socket` used to swallow the OSError
    and report "bridge script failed to start", turning a one-line
    diagnosis into hours of guessing. Podman-free: a bound-but-never-
    listening Unix socket is pre-placed where the code puts app.sock;
    the real `_wait_for_socket` runs against it with an accelerated
    clock so the timeout passes instantly.

    Planted bug: swallow the connect OSError in `_wait_for_socket`
    (leave last_err unchanged) -> the message has no concrete error ->
    FAIL.
    """
    from proofdeploy import sandbox as sandbox_mod
    from proofdeploy.sandbox import Sandbox, SandboxConfig

    _patch_caps(monkeypatch)
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    sock_dir = tmp_path / "sockdir"
    sock_dir.mkdir()

    # A socket file that exists but has no listener: connect() is refused.
    dead = pysocket.socket(pysocket.AF_UNIX, pysocket.SOCK_STREAM)
    dead.bind(str(sock_dir / "app.sock"))

    class _FakeTempfile:
        @staticmethod
        def mkdtemp(*a, **k):
            return str(sock_dir)

    monkeypatch.setattr(sandbox_mod, "tempfile", _FakeTempfile())

    class _FastClock:
        def __init__(self):
            self.now = 1000.0

        def time(self):
            self.now += 1.0
            return self.now

        def sleep(self, s):
            pass

    monkeypatch.setattr(sandbox_mod, "time", _FastClock())
    monkeypatch.setattr(sandbox_mod, "_run", _OkRun())

    sb = Sandbox(SandboxConfig(image="python:3.12-slim"))
    try:
        with pytest.raises(SandboxError) as ei:
            sb._start_none_socket(
                checkout, "python3 app.py", {}, 8080, "pd-test"
            )
    finally:
        dead.close()
    msg = str(ei.value)
    assert "connect failed" in msg, f"concrete error missing: {msg}"
    assert "bridge script failed to start" not in msg, (
        f"generic message leaked back in: {msg}"
    )


# ---------------------------------------------------------------------------
# Test 4 (SB5): the per-run socket dir is cleaned up.
# ---------------------------------------------------------------------------


def test_bridge_socket_dir_cleaned_up(monkeypatch, tmp_path):
    """SB5: the per-run /tmp/pd-* socket dir must be removed after stop()
    AND after a failed start. No leftovers behind.

    Podman-free: drives the real `_start_none_socket`; the fake _run
    reports success and `_wait_for_socket` is stubbed. The failed-start
    path is checked by diffing /tmp/pd-* before and after.

    Planted bug: disable the cleanup (no-op `_cleanup_sock_dir`, no
    rmtree in `SandboxApp.stop`) -> the dirs remain -> FAIL.
    """
    from proofdeploy import sandbox as sandbox_mod
    from proofdeploy.sandbox import Sandbox, SandboxConfig

    _patch_caps(monkeypatch)
    monkeypatch.setattr(sandbox_mod, "_run", _OkRun())
    monkeypatch.setattr(
        Sandbox, "_wait_for_socket", lambda self, p, n, timeout=15: None
    )
    checkout = tmp_path / "checkout"
    checkout.mkdir()

    sb = Sandbox(SandboxConfig(image="python:3.12-slim"))
    app = sb._start_none_socket(checkout, "python3 app.py", {}, 8080, "pd-a")
    sock_dir = app._bridge_sock_dir
    assert sock_dir is not None and sock_dir.exists()
    app.stop()
    assert not sock_dir.exists(), f"socket dir left behind after stop: {sock_dir}"

    # Failed start: the socket dir must be cleaned up too.
    before = set(Path("/tmp").glob("pd-*"))
    monkeypatch.setattr(
        Sandbox, "_wait_for_socket", lambda self, p, n, timeout=15: "boom"
    )
    with pytest.raises(SandboxError):
        sb._start_none_socket(checkout, "python3 app.py", {}, 8081, "pd-b")
    leftover = set(Path("/tmp").glob("pd-*")) - before
    assert not leftover, f"socket dirs left behind after failed start: {leftover}"


# ---------------------------------------------------------------------------
# Tests 5 (SB6): --require-hashes is used; a tampered hash fails the install.
# ---------------------------------------------------------------------------


class _CaptureInstall:
    """Fake Sandbox.install: records commands, reports success."""

    def __init__(self):
        self.commands: list[str] = []

    def __call__(self, checkout, commands, env, proxy_env, timeout=600):
        self.commands.extend(commands)
        return True


def _pip_install_cmd(commands: list[str]) -> str:
    cmds = [c for c in commands if "/bin/pip install" in c]
    assert len(cmds) == 1, f"expected one pip install command: {commands}"
    return cmds[0]


def test_sandbox_install_uses_require_hashes(monkeypatch, tmp_path):
    """SB6a: hash-pinned requirements install with `--require-hashes` in
    the sandboxed pip command.

    The frozen-lockfile guarantee (rule 9) is only real if pip actually
    verifies the hashes. Podman-free: drives the real
    `_provision_side_sandbox`; the fake install captures the commands
    and the fake start stops the run right after the install step.

    Planted bug: drop `--require-hashes` from pip_flags -> the command
    lacks the flag -> FAIL.
    """
    from proofdeploy.runner import ProvisionStatus
    from proofdeploy.sandbox import Sandbox, SandboxError

    snap = tmp_path / "snap"
    snap.mkdir()
    (snap / "requirements.txt").write_text(
        "six==1.17.0 \\\n"
        "    --hash=sha256:ff703d0fc3e08526949441584fca3fc209d20cf45a3f54c0fb84f79a80d0d95b1\n"
    )

    cap = _CaptureInstall()
    orch = _stub_provision_side(monkeypatch, snap, install=cap)

    def _stop_after_install(self, *a, **k):
        raise SandboxError("test stops after install")

    monkeypatch.setattr(Sandbox, "start", _stop_after_install)
    side = orch._provision_side_sandbox("fakerev", 18082, snap / "unit", "t")

    pip_cmd = _pip_install_cmd(cap.commands)
    assert "--require-hashes" in pip_cmd.split(), (
        f"--require-hashes missing from install command: {pip_cmd}"
    )
    # The run got past the install step (it failed later, at start).
    assert side.provision.status == ProvisionStatus.START_FAILED


def _build_test_wheel(tmp_path: Path) -> Path:
    """Build a tiny local wheel (offline) for the pip hash tests."""
    pkg = tmp_path / "pkg"
    (pkg / "mypkg").mkdir(parents=True)
    (pkg / "mypkg" / "__init__.py").write_text("x = 1\n")
    (pkg / "pyproject.toml").write_text(
        "[build-system]\n"
        'requires = ["setuptools"]\n'
        'build-backend = "setuptools.build_meta"\n'
        "[project]\n"
        'name = "mypkg"\n'
        'version = "0.1"\n'
    )
    dist = tmp_path / "dist"
    dist.mkdir()
    r = subprocess.run(
        [sys.executable, "-m", "pip", "wheel", str(pkg), "--no-deps",
         "--no-build-isolation", "-w", str(dist), "-q"],
        capture_output=True, text=True, timeout=180,
    )
    assert r.returncode == 0, f"wheel build failed: {r.stderr}"
    wheels = list(dist.glob("*.whl"))
    assert len(wheels) == 1
    return dist


class _RealPipInstall:
    """Fake Sandbox.install that runs the REAL pip hash check locally.

    Stands in for the install container: it takes the pip command the
    orchestrator generated (honoring its flags, including
    --require-hashes), and runs it against a local wheel dir with the
    real requirements file. A tampered hash fails exactly as pip would
    fail inside the container (exit 1, "THESE PACKAGES DO NOT MATCH THE
    HASHES"). No network: --no-index --find-links.
    """

    def __init__(self, wheel_dir: Path, work: Path):
        self.wheel_dir = wheel_dir
        self.work = work
        self.commands: list[str] = []
        self.n = 0

    def __call__(self, checkout, commands, env, proxy_env, timeout=600):
        self.commands.extend(commands)
        pip_cmd = _pip_install_cmd(commands)
        before, _, after = pip_cmd.partition("-r")
        flags = before.split("install", 1)[1].split()
        req_name = after.strip().split()[0]
        req_path = Path(checkout) / Path(req_name).name
        target = self.work / f"pip-target-{self.n}"
        self.n += 1
        cmd = [
            sys.executable, "-m", "pip", "install", *flags,
            "--no-index", "--find-links", str(self.wheel_dir),
            "-r", str(req_path), "--target", str(target), "-q",
        ]
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=300
        )
        return r.returncode == 0


def test_sandboxed_tampered_hash_fails_install(monkeypatch, tmp_path):
    """SB6b: a tampered hash fails the sandboxed install (BUILD_FAILED).

    The requirements file pins mypkg==0.1 with a WRONG sha256. The fake
    install runs real pip with the orchestrator's flags: pip rejects the
    download ("THESE PACKAGES DO NOT MATCH THE HASHES", exit 1), the
    fake reports failure, and the orchestrator must fail closed with
    BUILD_FAILED "sandbox install failed" (S3: lockfile hash recorded).

    Planted bug: drop `--require-hashes` from pip_flags -> pip no
    longer checks the hash -> install succeeds -> the side is not
    BUILD_FAILED "sandbox install failed" -> FAIL.
    """
    from proofdeploy.runner import ProvisionStatus

    dist = _build_test_wheel(tmp_path)
    snap = tmp_path / "snap"
    snap.mkdir()
    (snap / "requirements.txt").write_text(
        "mypkg==0.1 \\\n"
        "    --hash=sha256:0000000000000000000000000000000000000000000000000000000000000000\n"
    )

    fake = _RealPipInstall(dist, tmp_path)
    orch = _stub_provision_side(monkeypatch, snap, install=fake)
    side = orch._provision_side_sandbox("fakerev", 18083, snap / "unit", "t")
    logs = "\n".join(side.provision.logs)

    assert side.provision.status == ProvisionStatus.BUILD_FAILED, logs
    assert side.provision.reason == "sandbox install failed", logs
    # S3: the lockfile hash is recorded even on this failure.
    assert side.provision.lockfile_sha256, logs
    # And the flag that arms pip's check was in the command.
    assert "--require-hashes" in _pip_install_cmd(fake.commands).split()

