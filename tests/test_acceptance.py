"""Acceptance test for the ProofDeploy runner (WAL-58, part 5 of 5).

Blind dry run: a small app with a database and a login, run end to end
by the runner pieces (provisioner → setup/auth → executor → run pair).

The test app is a minimal Python HTTP server with:
- POST /login → returns a token (from the sqlite users table)
- GET /profile → requires the token, returns user data
- GET /health → readiness check

This is also Ghost's deadline per the registered rules.
"""

import json
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from proofdeploy.executor import Executor, Verdict
from proofdeploy.runpair import RunPairVerdict, score_run_pair
from proofdeploy.setup_auth import SetupContext


class _AppHandler(BaseHTTPRequestHandler):
    """Minimal app: login + profile + health, backed by sqlite."""

    db_path: str = ""

    def _db(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
        elif self.path == "/profile":
            auth = self.headers.get("Authorization", "")
            token = auth.replace("Bearer ", "")
            conn = self._db()
            try:
                row = conn.execute(
                    "SELECT username FROM users WHERE token = ?", (token,)
                ).fetchone()
            finally:
                conn.close()
            if row:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"username": row["username"]}).encode())
            else:
                self.send_response(401)
                self.end_headers()
                self.wfile.write(b"unauthorized")
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == "/login":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            conn = self._db()
            try:
                row = conn.execute(
                    "SELECT token FROM users WHERE username = ? AND password = ?",
                    (body.get("username"), body.get("password")),
                ).fetchone()
            finally:
                conn.close()
            if row:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"token": row["token"]}).encode())
            else:
                self.send_response(401)
                self.end_headers()
                self.wfile.write(b"bad credentials")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args):
        pass


@pytest.fixture()
def app(tmp_path):
    """A running app with a seeded sqlite database."""
    db = tmp_path / "app.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE users (username TEXT, password TEXT, token TEXT)")
    conn.execute(
        "INSERT INTO users VALUES ('alice', 'secret', 'tok-alice-123')"
    )
    conn.commit()
    conn.close()

    _AppHandler.db_path = str(db)
    srv = HTTPServer(("127.0.0.1", 0), _AppHandler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{port}", str(db)
    srv.shutdown()


def test_acceptance_login_and_profile(app):
    """End-to-end: setup captures a token, probe uses it, assertions pass."""
    url, db_path = app

    # 1. Setup: log in, capture the token.
    import urllib.request

    login_body = json.dumps({"username": "alice", "password": "secret"}).encode()
    req = urllib.request.Request(
        url + "/login",
        data=login_body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        login_resp = json.loads(resp.read())
    assert login_resp["token"] == "tok-alice-123"

    # 2. SetupContext with the captured token as AUTH_TOKEN.
    ctx = SetupContext(auth_token=login_resp["token"])

    # 3. Probe: GET /profile with the token, assert on the username.
    probe = {
        "act": {
            "method": "GET",
            "path": "/profile",
            "headers": {"Authorization": "Bearer ${AUTH_TOKEN}"},
        },
        "assert": [
            {"type": "status", "equals": 200},
            {"type": "body", "json_path": {"path": "username", "equals": "alice"}},
        ],
    }
    filled = ctx.apply(probe)

    # 4. Execute.
    ex = Executor(target_url=url, db_path=db_path)
    result = ex.run_probe(filled)
    assert result.verdict == Verdict.PASS, result.details


def test_acceptance_wrong_token_fails(app):
    """A bad token yields 401 → status assertion fails → FAIL."""
    url, db_path = app
    ctx = SetupContext(auth_token="wrong-token")
    probe = {
        "act": {
            "method": "GET",
            "path": "/profile",
            "headers": {"Authorization": "Bearer ${AUTH_TOKEN}"},
        },
        "assert": [{"type": "status", "equals": 200}],
    }
    ex = Executor(target_url=url, db_path=db_path)
    result = ex.run_probe(ctx.apply(probe))
    assert result.verdict == Verdict.FAIL


def test_acceptance_db_assertion(app):
    """DB assertions read the sqlite database directly."""
    url, db_path = app
    ex = Executor(target_url=url, db_path=db_path)
    probe = {
        "act": {"method": "GET", "path": "/health"},
        "assert": [
            {"type": "status", "equals": 200},
            {"type": "db", "query": "SELECT username FROM users", "count": 1},
        ],
    }
    result = ex.run_probe(probe)
    assert result.verdict == Verdict.PASS, result.details


def test_acceptance_run_pair_scoring(app):
    """The run pair scores FAIL→PASS as a catch."""
    from proofdeploy.executor import ProbeResult

    parent = [ProbeResult(probe_index=0, verdict=Verdict.FAIL)]
    fix = [ProbeResult(probe_index=0, verdict=Verdict.PASS)]
    assert score_run_pair(parent, fix) == RunPairVerdict.CATCH
