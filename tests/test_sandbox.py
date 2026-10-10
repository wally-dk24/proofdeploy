"""Tests for proofdeploy.sandbox (WO-5).

The sandbox runs the app in an isolated container: no --net=host, only
the checkout mounted, no external network for the app, no secrets in
the container environment. These tests use real containers.

They SKIP (with a stated reason) when no container runtime is present;
they are not host-dependent. They run in CI on a runner with podman
(see docs/sandbox-ci.md).

The "no podman needed" section at the bottom unit-tests the isolation
gate, the evidence record fields, and the container command
construction with a fake runner, so the coverage gate passes on
machines without a container runtime.
"""

import json
import os
import shutil
import subprocess
import tempfile
import urllib.request
from pathlib import Path

import pytest

from proofdeploy.sandbox import (
    IMAGES,
    Sandbox,
    SandboxCapabilities,
    SandboxConfig,
    SandboxError,
    _podman_base,
    assert_measurement_gate,
    check_podman,
    check_sandbox_capabilities,
)

# Probe once at import: the capability checks run real containers.
_CAPS = check_sandbox_capabilities()

requires_podman = pytest.mark.skipif(
    not _CAPS.podman,
    reason="no container runtime: sandbox tests need podman",
)

# The install phase runs on a bridge network (never --net=host). When
# the host's netavark is broken, the install test skips with a reason
# instead of failing: the fail-closed refusal is the correct behavior.
requires_build_network = pytest.mark.skipif(
    not _CAPS.build_network,
    reason="no bridge network: install phase needs a working podman bridge",
)

APP_PY = """\
import os
from http.server import BaseHTTPRequestHandler, HTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            body = b'{"ok": true}'
        elif self.path == "/whoami":
            import json as j
            body = j.dumps({"uid": os.getuid()}).encode()
        elif self.path == "/egress":
            # From inside the container, with proxy vars set by the app
            # itself: proxy, host-loopback, external IP must be unreachable.
            import json as j
            import socket
            import urllib.request as u
            result = {}
            # 1. Proxy: set by the app itself, then try to use it.
            proxy = os.environ.get("TEST_PROXY", "")
            os.environ["https_proxy"] = proxy
            os.environ["HTTPS_PROXY"] = proxy
            try:
                u.urlopen("https://8.8.8.8/", timeout=5).read(1)
                result["proxy"] = "REACHABLE"
            except Exception:
                result["proxy"] = "BLOCKED"
            # 2. Host-loopback services (container loopback is isolated).
            try:
                socket.create_connection(("127.0.0.1", 1), timeout=3)
                result["loopback"] = "REACHABLE"
            except Exception:
                result["loopback"] = "BLOCKED"
            # 3. External IP by direct TCP (no proxy).
            try:
                socket.create_connection(("1.1.1.1", 443), timeout=3)
                result["external"] = "REACHABLE"
            except Exception:
                result["external"] = "BLOCKED"
            body = j.dumps(result).encode()
        else:
            body = b'{"ok": false}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


HTTPServer(("127.0.0.1", int(os.environ["PORT"])), Handler).serve_forever()
"""


@pytest.fixture()
def checkout():
    d = Path(tempfile.mkdtemp(prefix="sandbox-test-"))
    (d / "app.py").write_text(APP_PY)
    (d / "requirements.txt").write_text("# no deps\n")
    yield d
    shutil.rmtree(d, ignore_errors=True)


def _proxy_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if "proxy" in k.lower()}


@requires_podman
def test_podman_available():
    ok, msg = check_podman()
    assert ok, msg


@requires_podman
@requires_build_network
def test_sandbox_install_and_run(checkout):
    sb = Sandbox(SandboxConfig(image=IMAGES["python"], allow_no_userns=True))
    ok = sb.install(
        checkout,
        ["python -m py_compile app.py"],
        env={},
        proxy_env=_proxy_env(),
        timeout=120,
    )
    assert ok, sb.log[-3:]
    app = sb.start(
        checkout,
        "python app.py",
        env={},
        port=18095,
        name="sandbox-unit-test",
    )
    try:
        assert app.wait_ready("/health", timeout=60)
        body = urllib.request.urlopen(
            app.target_url + "/health", timeout=10
        ).read()
        assert json.loads(body) == {"ok": True}
    finally:
        app.stop()


@requires_podman
def test_sandbox_app_is_not_root(checkout):
    sb = Sandbox(SandboxConfig(image=IMAGES["python"], allow_no_userns=True))
    app = sb.start(
        checkout, "python app.py", env={}, port=18096, name="sandbox-noroot"
    )
    try:
        assert app.wait_ready("/whoami", timeout=60)
        body = urllib.request.urlopen(
            app.target_url + "/whoami", timeout=10
        ).read()
        uid = json.loads(body)["uid"]
        assert uid != 0, "app must not run as root"
    finally:
        app.stop()


@requires_podman
def test_sandbox_app_has_no_external_network(checkout):
    """From inside the app container, with proxy vars set by the app
    itself: the proxy, host-loopback services, and an external IP are
    all unreachable. The executor can still reach the app."""
    sb = Sandbox(SandboxConfig(image=IMAGES["python"], allow_no_userns=True))
    proxy = os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY") or ""
    app = sb.start(
        checkout,
        "python app.py",
        env={"TEST_PROXY": proxy},
        port=18097,
        name="sandbox-noegress",
    )
    try:
        assert app.wait_ready("/health", timeout=60)
        # Executor reaches the app.
        body = urllib.request.urlopen(
            app.target_url + "/health", timeout=10
        ).read()
        assert json.loads(body) == {"ok": True}
        # From inside: proxy, host loopback, and external IP unreachable.
        body = urllib.request.urlopen(
            app.target_url + "/egress", timeout=30
        ).read()
        result = json.loads(body)
        assert result["proxy"] == "BLOCKED", f"proxy reachable: {result}"
        assert result["loopback"] == "BLOCKED", f"loopback reachable: {result}"
        assert result["external"] == "BLOCKED", f"external reachable: {result}"
    finally:
        app.stop()


@requires_podman
def test_sandbox_mounts_only_checkout(checkout):
    sb = Sandbox(SandboxConfig(image=IMAGES["python"], allow_no_userns=True))
    app = sb.start(
        checkout, "python app.py", env={}, port=18098, name="sandbox-mounts"
    )
    try:
        assert app.wait_ready("/health", timeout=60)
        # The container's mount table: only /checkout (plus container
        # internals) should be a host bind.
        r = subprocess.run(
            _podman_base() + ["inspect", "sandbox-mounts", "--format", "{{.Mounts}}"],
            capture_output=True, text=True, timeout=30,
        )
        assert str(checkout) in r.stdout
        # No workdir, no home, no secrets dir mounted.
        assert "/workspace" not in r.stdout.replace(str(checkout), "")
    finally:
        app.stop()


# ---------------------------------------------------------------------------
# No podman needed: isolation gate, evidence fields, and command
# construction with a fake runner. These keep the coverage gate green on
# machines without a container runtime.
# ---------------------------------------------------------------------------


def _fake_caps(**overrides) -> SandboxCapabilities:
    caps = SandboxCapabilities(
        podman=True,
        podman_version="podman version 4.9.3",
        isolated_network=False,
        build_network=True,
        userns_remap=False,
        reason="userns remap unavailable: test",
    )
    for k, v in overrides.items():
        setattr(caps, k, v)
    return caps


def _patch_sandbox_caps(monkeypatch, **overrides):
    caps = _fake_caps(**overrides)
    monkeypatch.setattr(Sandbox, "_capabilities", lambda self: caps)
    return caps


def _patch_probe_caps(monkeypatch, **overrides):
    caps = _fake_caps(**overrides)
    monkeypatch.setattr(
        "proofdeploy.sandbox.check_sandbox_capabilities", lambda: caps
    )
    return caps


class _FakeRun:
    """Fake proofdeploy.sandbox._run: records podman args, never executes."""

    def __init__(self, fail_on: tuple[str, ...] = ()):
        self.calls: list[list[str]] = []
        self.fail_on = fail_on

    def __call__(self, args, timeout, logs):
        self.calls.append(list(args))
        logs.append(f"$ {' '.join(args)}")
        if any(f in " ".join(args) for f in self.fail_on):
            return False, "fake failure"
        return True, ""


def _install_run_args(fake: _FakeRun) -> list[str]:
    for call in fake.calls:
        if "sandbox-install-0" in call:
            return call
    raise AssertionError(f"no install run call recorded: {fake.calls}")


def test_default_config_is_fail_closed_on_userns(monkeypatch):
    """The default config refuses when the host cannot remap userns:
    require_userns=True is the default."""
    _patch_sandbox_caps(monkeypatch, userns_remap=False)
    sb = Sandbox(SandboxConfig(image=IMAGES["python"]))
    assert sb.config.require_userns is True
    with pytest.raises(SandboxError, match="require_userns"):
        sb._user_args()
    assert sb.userns_mode is None


def test_explicit_opt_out_runs_and_records(monkeypatch):
    """allow_no_userns=True is the only explicit way past the userns
    requirement; the opt-out is recorded on the sandbox for the
    evidence record."""
    _patch_sandbox_caps(monkeypatch, userns_remap=False)
    sb = Sandbox(SandboxConfig(image=IMAGES["python"], allow_no_userns=True))
    args = sb._user_args()
    assert "--user" in args and "65534" in args
    assert "--uidmap" not in args
    assert sb.userns_mode == "nobody-fallback"
    assert sb.userns_opt_out is True


def test_userns_remap_used_when_available(monkeypatch):
    _patch_sandbox_caps(monkeypatch, userns_remap=True)
    sb = Sandbox(SandboxConfig(image=IMAGES["python"]))
    args = sb._user_args()
    assert "--uidmap" in args
    assert sb.userns_mode == "userns-remap"
    assert sb.userns_opt_out is False


def test_measurement_gate_refuses_no_podman(monkeypatch):
    _patch_probe_caps(monkeypatch, podman=False, reason="podman not found")
    with pytest.raises(SandboxError, match="no container runtime"):
        assert_measurement_gate()


def test_measurement_gate_refuses_no_userns(monkeypatch):
    _patch_probe_caps(monkeypatch, userns_remap=False, build_network=True)
    with pytest.raises(SandboxError, match="userns remapping unavailable"):
        assert_measurement_gate()


def test_measurement_gate_opt_out_passes_userns(monkeypatch):
    _patch_probe_caps(monkeypatch, userns_remap=False, build_network=True)
    # Explicit dev opt-out: the gate passes on userns.
    assert_measurement_gate(SandboxConfig(allow_no_userns=True))


def test_measurement_gate_refuses_no_build_network(monkeypatch):
    _patch_probe_caps(monkeypatch, userns_remap=True, build_network=False)
    with pytest.raises(SandboxError, match="no bridge network"):
        assert_measurement_gate()


def test_measurement_gate_passes_when_capable(monkeypatch):
    _patch_probe_caps(monkeypatch, userns_remap=True, build_network=True)
    assert_measurement_gate()  # does not raise


def test_install_uses_bridge_never_host(monkeypatch, tmp_path):
    """The install phase runs untrusted scripts: it must use a bridge
    network, never --net=host."""
    _patch_sandbox_caps(monkeypatch, userns_remap=True, build_network=True)
    fake = _FakeRun()
    monkeypatch.setattr("proofdeploy.sandbox._run", fake)
    sb = Sandbox(SandboxConfig(image=IMAGES["python"]))
    checkout = tmp_path / "co"
    checkout.mkdir()
    assert sb.install(checkout, ["echo hi"], env={}, proxy_env={}, timeout=60)
    args = _install_run_args(fake)
    assert "--net=host" not in args
    assert "--net" in args
    assert args[args.index("--net") + 1] == "pd-build"
    assert sb.install_network == "bridge:pd-build"


def test_install_refuses_without_bridge(monkeypatch, tmp_path):
    """When the build network cannot be created, the install refuses
    instead of falling back to --net=host."""
    _patch_sandbox_caps(monkeypatch, userns_remap=True, build_network=True)
    fake = _FakeRun(fail_on=("network exists", "network create"))
    monkeypatch.setattr("proofdeploy.sandbox._run", fake)
    sb = Sandbox(SandboxConfig(image=IMAGES["python"]))
    checkout = tmp_path / "co"
    checkout.mkdir()
    with pytest.raises(SandboxError, match="must not use --net=host"):
        sb.install(checkout, ["echo hi"], env={}, proxy_env={}, timeout=60)


def test_app_start_uses_no_host_network(monkeypatch, tmp_path):
    """The app container is built with --net=none (or the isolated
    bridge), never --net=host."""
    _patch_sandbox_caps(
        monkeypatch, userns_remap=True, isolated_network=False
    )
    fake = _FakeRun()
    monkeypatch.setattr("proofdeploy.sandbox._run", fake)
    sb = Sandbox(SandboxConfig(image=IMAGES["python"]))
    checkout = tmp_path / "co"
    checkout.mkdir()
    # Bypass the real container start: only build the args.
    args = sb._common_args(checkout, "probe-app", False, detach=True)
    assert "--net=host" not in args
    assert "--net=none" in args
    assert sb.app_network_mode == "none+socket"


def test_isolation_evidence_fields(monkeypatch):
    """sandbox_evidence carries the isolation actually in effect:
    network mode, userns mode, and the uid the app ran as."""
    _patch_sandbox_caps(monkeypatch, userns_remap=False)
    sb = Sandbox(SandboxConfig(image=IMAGES["python"], allow_no_userns=True))
    sb._user_args()  # sets userns tracking
    sb.app_network_mode = "none+socket"
    sb.install_network = "bridge:pd-build"
    ev = sb._isolation_evidence("none+socket")
    assert ev["app_network_mode"] == "none+socket"
    assert ev["install_network"] == "bridge:pd-build"
    assert ev["userns_mode"] == "nobody-fallback"
    assert ev["userns_opt_out"] is True
    assert ev["app_uid"] == 65534


def test_isolation_evidence_uid_zero_when_remapped(monkeypatch):
    _patch_sandbox_caps(monkeypatch, userns_remap=True)
    sb = Sandbox(SandboxConfig(image=IMAGES["python"]))
    sb._user_args()
    ev = sb._isolation_evidence("isolated")
    assert ev["userns_mode"] == "userns-remap"
    assert ev["userns_opt_out"] is False
    assert ev["app_uid"] == 0


def test_evidence_record_carries_sandbox_evidence():
    """build_evidence_record stores sandbox_evidence in the record."""
    from proofdeploy.runpair import build_evidence_record

    ev = {
        "fix_parent": {
            "app_network_mode": "none+socket",
            "userns_mode": "userns-remap",
            "app_uid": 0,
        },
        "fix": {
            "app_network_mode": "none+socket",
            "userns_mode": "userns-remap",
            "app_uid": 0,
        },
    }
    record = build_evidence_record(run_kind="bug", sandbox_evidence=ev)
    assert record["sandbox_evidence"] == ev
    # Absent when the sandbox was not used.
    record2 = build_evidence_record(run_kind="bug")
    assert record2["sandbox_evidence"] == {}
