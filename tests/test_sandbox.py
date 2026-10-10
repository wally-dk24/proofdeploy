"""Tests for proofdeploy.sandbox (WO-5).

The sandbox runs the app in a de-privileged container: non-root user,
only the checkout mounted, no external network for the app, no secrets
in the container environment. These tests use real containers.
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
            # Try to reach the external network; report the outcome.
            import urllib.request as u
            try:
                u.urlopen("https://8.8.8.8/", timeout=8).read(1)
                body = b'{"egress": true}'
            except Exception as e:
                body = ('{"egress": false, "err": "%s"}' % type(e).__name__).encode()
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


def test_podman_available():
    ok, msg = check_podman()
    assert ok, msg


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
        body = urllib.request.urlopen("http://127.0.0.1:18095/health", timeout=10).read()
        assert json.loads(body) == {"ok": True}
    finally:
        app.stop()


def test_sandbox_app_is_not_root(checkout):
    sb = Sandbox(SandboxConfig(image=IMAGES["python"]))
    app = sb.start(
        checkout, "python app.py", env={}, port=18096, name="sandbox-noroot"
    )
    try:
        assert app.wait_ready("/whoami", timeout=60)
        body = urllib.request.urlopen("http://127.0.0.1:18096/whoami", timeout=10).read()
        uid = json.loads(body)["uid"]
        assert uid != 0, "app must not run as root"
        assert uid == 65534, f"expected nobody (65534), got {uid}"
    finally:
        app.stop()


def test_sandbox_app_has_no_external_network(checkout):
    sb = Sandbox(SandboxConfig(image=IMAGES["python"]))
    app = sb.start(
        checkout, "python app.py", env={}, port=18097, name="sandbox-noegress"
    )
    try:
        assert app.wait_ready("/egress", timeout=60)
        body = urllib.request.urlopen("http://127.0.0.1:18097/egress", timeout=30).read()
        result = json.loads(body)
        assert result["egress"] is False, "app must not reach the external network"
    finally:
        app.stop()


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
