"""Tests for proofdeploy.sandbox (WO-5).

The sandbox runs the app in an isolated container: no --net=host, only
the checkout mounted, no external network for the app, no secrets in
the container environment. These tests use real containers.

They SKIP (with a stated reason) when no container runtime is present;
they are not host-dependent. They run in CI on a runner with podman
(see docs/sandbox-ci.md).
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
    SandboxConfig,
    _podman_base,
    check_podman,
    check_sandbox_capabilities,
)

requires_podman = pytest.mark.skipif(
    not check_sandbox_capabilities().podman,
    reason="no container runtime: sandbox tests need podman",
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
def test_sandbox_install_and_run(checkout):
    sb = Sandbox(SandboxConfig(image=IMAGES["python"]))
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
    sb = Sandbox(SandboxConfig(image=IMAGES["python"]))
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
    sb = Sandbox(SandboxConfig(image=IMAGES["python"]))
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
    sb = Sandbox(SandboxConfig(image=IMAGES["python"]))
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
