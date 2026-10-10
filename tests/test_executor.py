"""Tests for the probe executor (WAL-58)."""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from proofdeploy.executor import Executor, InconclusiveReason, Verdict


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/ok":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("X-Custom", "hello-world")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "ok", "n": 42}).encode())
        elif self.path == "/missing":
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"not found")
        elif self.path == "/bad":
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"bad request")
        elif self.path == "/denied":
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b"unauthorized")
        elif self.path == "/forbidden":
            self.send_response(403)
            self.end_headers()
            self.wfile.write(b"forbidden")
        else:
            self.send_response(500)
            self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""
        self.send_response(201)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"echo": body.decode()}).encode())

    def log_message(self, *args):
        pass


@pytest.fixture()
def server():
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{port}"
    srv.shutdown()


def _probe(**kw):
    p = {"act": {"method": "GET", "path": "/ok"}, "assert": []}
    # 'assert' is a keyword; tests pass assert_ instead
    if "assert_" in kw:
        kw["assert"] = kw.pop("assert_")
    p.update(kw)
    return p


def test_status_assertion_pass(server):
    ex = Executor(target_url=server)
    r = ex.run_probe(_probe(assert_=[{"type": "status", "equals": 200}]))
    # Note: _probe uses "assert" key; run_probe reads probe["assert"]
    assert r.verdict == Verdict.PASS


def test_status_assertion_fail(server):
    ex = Executor(target_url=server)
    p = _probe()
    p["assert"] = [{"type": "status", "equals": 404}]
    r = ex.run_probe(p)
    assert r.verdict == Verdict.FAIL


def test_header_assertion(server):
    ex = Executor(target_url=server)
    p = _probe()
    p["assert"] = [
        {"type": "header", "name": "x-custom", "equals": "hello-world"},
        {"type": "header", "name": "x-custom", "contains": "hello"},
    ]
    r = ex.run_probe(p)
    assert r.verdict == Verdict.PASS


def test_header_missing_is_fail(server):
    ex = Executor(target_url=server)
    p = _probe()
    p["assert"] = [{"type": "header", "name": "x-nope", "equals": "x"}]
    r = ex.run_probe(p)
    assert r.verdict == Verdict.FAIL


def test_body_json_path(server):
    ex = Executor(target_url=server)
    p = _probe()
    p["assert"] = [{"type": "body", "json_path": {"path": "n", "equals": 42}}]
    r = ex.run_probe(p)
    assert r.verdict == Verdict.PASS


def test_body_contains(server):
    ex = Executor(target_url=server)
    p = _probe()
    p["assert"] = [{"type": "body", "contains": '"status": "ok"'}]
    r = ex.run_probe(p)
    assert r.verdict == Verdict.PASS


def test_post_act(server):
    ex = Executor(target_url=server, provisioned=True)
    p = {
        "act": {"method": "POST", "path": "/ok", "body": "hi"},
        "assert": [{"type": "status", "equals": 201}],
    }
    r = ex.run_probe(p)
    assert r.verdict == Verdict.PASS
    assert r.http_status == 201


def test_unreachable_target_is_inconclusive():
    ex = Executor(target_url="http://127.0.0.1:1", http_timeout=2)
    r = ex.run_probe(_probe(assert_=[{"type": "status", "equals": 200}]))
    assert r.verdict == Verdict.INCONCLUSIVE


def test_db_rejects_write(tmp_path):
    from proofdeploy.executor import _do_db_query

    result, logs = _do_db_query(str(tmp_path / "x.db"), "DROP TABLE t")
    assert result is None
    assert any("non-read-only" in log for log in logs)


def test_db_select(tmp_path):
    import sqlite3

    from proofdeploy.executor import _do_db_query

    db = tmp_path / "t.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE t (a INT)")
    conn.execute("INSERT INTO t VALUES (1)")
    conn.commit()
    conn.close()
    rows, _ = _do_db_query(str(db), "SELECT a FROM t")
    assert rows == [(1,)]


def test_db_assertion_count(tmp_path, server):
    import sqlite3

    db = tmp_path / "t.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE t (a INT)")
    conn.execute("INSERT INTO t VALUES (1)")
    conn.commit()
    conn.close()
    ex = Executor(target_url=server, db_path=str(db))
    p = _probe()
    p["assert"] = [{"type": "db", "query": "SELECT a FROM t", "count": 1}]
    r = ex.run_probe(p)
    assert r.verdict == Verdict.PASS


def test_result_to_dict(server):
    ex = Executor(target_url=server)
    r = ex.run_probe(_probe(assert_=[{"type": "status", "equals": 200}]))
    d = r.to_dict()
    assert d["verdict"] == "pass"
    assert d["http_status"] == 200
    assert d["reason"] is None
    assert d["writes"] == []


# --- WO-2: fail-closed executor tests ---


class _Handler2(_Handler):
    def do_GET(self):
        if self.path == "/redir":
            self.send_response(302)
            self.send_header("Location", "/ok")
            self.end_headers()
        elif self.path == "/boom":
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b"setup exploded")
        else:
            super().do_GET()


@pytest.fixture()
def server2():
    srv = HTTPServer(("127.0.0.1", 0), _Handler2)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{port}"
    srv.shutdown()


def test_invalid_probe_rejected_before_http():
    # Unreachable target + invalid probe: schema validation must run first,
    # so the reason is PROBE, not ENVIRONMENT.
    ex = Executor(target_url="http://127.0.0.1:1", http_timeout=2)
    r = ex.run_probe({"act": {"method": "GET", "path": "/ok"}})  # no 'assert'
    assert r.verdict == Verdict.INCONCLUSIVE
    assert r.reason == InconclusiveReason.PROBE
    assert any("rejected by schema" in d for d in r.details)


def test_unknown_setup_type_is_inconclusive_probe(server):
    # validate_probe rejects unknown setup types at the schema gate first...
    ex = Executor(target_url=server)
    p = _probe(
        setup=[{"type": "bogus_step"}],
        assert_=[{"type": "status", "equals": 200}],
    )
    r = ex.run_probe(p)
    assert r.verdict == Verdict.INCONCLUSIVE
    assert r.reason == InconclusiveReason.PROBE
    # ...and run_setup itself fails closed if called directly.
    logs, reason, detail, _ = ex.run_setup([{"type": "bogus_step"}])
    assert reason == InconclusiveReason.PROBE
    assert detail is not None and "unknown setup type" in detail


def test_setup_failure_is_inconclusive_environment(server2):
    ex = Executor(target_url=server2)
    p = _probe(
        setup=[{"type": "http", "act": {"method": "GET", "path": "/boom"}}],
        assert_=[{"type": "status", "equals": 200}],
    )
    r = ex.run_probe(p)
    assert r.verdict == Verdict.INCONCLUSIVE
    assert r.reason == InconclusiveReason.ENVIRONMENT
    # The act must not run after a failed setup.
    assert not any(d.startswith("act:") for d in r.details)


def test_verdict_precedence_fail_beats_inconclusive(server):
    ex = Executor(target_url=server)
    p = _probe(
        assert_=[
            {"type": "status", "equals": 404},  # FAIL (target returns 200)
            {"type": "body", "json_path": {"path": "nope.missing", "equals": 1}},
            # INCONCLUSIVE (drift): path not in the JSON body
        ]
    )
    r = ex.run_probe(p)
    assert r.verdict == Verdict.FAIL
    assert r.reason is None


def test_verdict_precedence_inconclusive_beats_pass(server):
    ex = Executor(target_url=server)
    p = _probe(
        assert_=[
            {"type": "status", "equals": 200},  # PASS
            {"type": "body", "json_path": {"path": "nope.missing", "equals": 1}},
            # INCONCLUSIVE (drift)
        ]
    )
    r = ex.run_probe(p)
    assert r.verdict == Verdict.INCONCLUSIVE
    assert r.reason == InconclusiveReason.DRIFT


def test_db_assertions_get_independent_results(tmp_path, server):
    import sqlite3

    db = tmp_path / "t.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE t (a INT)")
    conn.execute("INSERT INTO t VALUES (1)")
    conn.execute("INSERT INTO t VALUES (2)")
    conn.commit()
    conn.close()
    ex = Executor(target_url=server, db_path=str(db))
    p = _probe(
        assert_=[
            {"type": "db", "query": "SELECT a FROM t", "count": 2},
            {"type": "db", "query": "SELECT a FROM t WHERE a = 999", "count": 0},
        ]
    )
    r = ex.run_probe(p)
    # Under the old shared-result code, the second assertion would see the
    # first query's 2 rows and FAIL. Independent results: both PASS.
    assert r.verdict == Verdict.PASS


def test_redirect_not_followed(server2):
    ex = Executor(target_url=server2)
    p = _probe(assert_=[{"type": "status", "equals": 302}])
    p["act"] = {"method": "GET", "path": "/redir"}
    r = ex.run_probe(p)
    assert r.http_status == 302
    assert r.verdict == Verdict.PASS


def test_remote_post_rejected_before_http():
    # Remote target (not localhost): POST must be refused without any request.
    ex = Executor(target_url="http://example.invalid", http_timeout=2)
    p = {
        "act": {"method": "POST", "path": "/x", "body": "hi"},
        "assert": [{"type": "status", "equals": 200}],
    }
    r = ex.run_probe(p)
    assert r.verdict == Verdict.INCONCLUSIVE
    assert r.reason == InconclusiveReason.PROBE
    assert any("not allowed on remote target" in d for d in r.details)


def test_remote_get_allowed_policy_only():
    # GET on a remote target passes the policy gate (it then fails to
    # connect, which is ENVIRONMENT — proving the gate let it through).
    ex = Executor(target_url="http://example.invalid", http_timeout=2)
    p = _probe(assert_=[{"type": "status", "equals": 200}])
    r = ex.run_probe(p)
    assert r.verdict == Verdict.INCONCLUSIVE
    assert r.reason == InconclusiveReason.ENVIRONMENT


def test_writes_are_logged(server):
    ex = Executor(target_url=server, provisioned=True)
    p = {
        "act": {"method": "POST", "path": "/ok", "body": "hi"},
        "assert": [{"type": "status", "equals": 201}],
    }
    r = ex.run_probe(p)
    assert r.verdict == Verdict.PASS
    assert len(r.writes) == 1
    assert r.writes[0].startswith("write: POST ")
    assert "-> 201" in r.writes[0]
    d = r.to_dict()
    assert d["writes"] == r.writes


def test_setup_write_is_logged(server):
    ex = Executor(target_url=server, provisioned=True)
    p = _probe(
        setup=[{"type": "http", "act": {"method": "POST", "path": "/ok", "body": "s"}}],
        assert_=[{"type": "status", "equals": 200}],
    )
    r = ex.run_probe(p)
    assert r.verdict == Verdict.PASS
    assert any(w.startswith("write: POST ") for w in r.writes)


def test_inconclusive_reason_in_to_dict():
    ex = Executor(target_url="http://127.0.0.1:1", http_timeout=2)
    r = ex.run_probe(_probe(assert_=[{"type": "status", "equals": 200}]))
    assert r.verdict == Verdict.INCONCLUSIVE
    assert r.reason == InconclusiveReason.ENVIRONMENT
    assert r.to_dict()["reason"] == "environment"


def test_non_2xx_setup_is_inconclusive_environment(server):
    # Any other path returns 500 on the server fixture. A non-2xx setup
    # response is INCONCLUSIVE (environment), not silently continued.
    ex = Executor(target_url=server)
    p = _probe(
        setup=[{"type": "http", "act": {"method": "GET", "path": "/explode"}}],
        assert_=[{"type": "status", "equals": 200}],
    )
    r = ex.run_probe(p)
    assert r.verdict == Verdict.INCONCLUSIVE
    assert r.reason == InconclusiveReason.ENVIRONMENT
    assert any("500" in d for d in r.details)
    # The act must not run after a failed setup.
    assert not any(d.startswith("act:") for d in r.details)


def _setup_inconclusive_reason(server, path):
    """Run a probe whose setup hits `path`; return its INCONCLUSIVE reason."""
    ex = Executor(target_url=server)
    p = _probe(
        setup=[{"type": "http", "act": {"method": "GET", "path": path}}],
        assert_=[{"type": "status", "equals": 200}],
    )
    r = ex.run_probe(p)
    assert r.verdict == Verdict.INCONCLUSIVE
    # The act must not run after a failed setup.
    assert not any(d.startswith("act:") for d in r.details)
    return r.reason


def test_setup_401_is_inconclusive_auth(server):
    # 401 from setup: credential problem -> AUTH (only ENVIRONMENT may be
    # downgraded to "warn" by an admin; AUTH and PROBE never can).
    assert _setup_inconclusive_reason(server, "/denied") == InconclusiveReason.AUTH


def test_setup_403_is_inconclusive_auth(server):
    assert _setup_inconclusive_reason(server, "/forbidden") == InconclusiveReason.AUTH


def test_setup_400_is_inconclusive_probe(server):
    # 400 from setup: the probe's request was wrong -> PROBE.
    assert _setup_inconclusive_reason(server, "/bad") == InconclusiveReason.PROBE


def test_setup_404_is_inconclusive_probe(server):
    assert _setup_inconclusive_reason(server, "/missing") == InconclusiveReason.PROBE


def test_setup_500_is_inconclusive_environment(server):
    assert _setup_inconclusive_reason(server, "/explode") == InconclusiveReason.ENVIRONMENT


def test_write_act_on_non_provisioned_target_is_inconclusive_probe(server):
    # Writes need provisioned=True; localhost alone is not sufficient.
    ex = Executor(target_url=server)  # provisioned defaults to False
    p = {
        "act": {"method": "POST", "path": "/ok", "body": "hi"},
        "assert": [{"type": "status", "equals": 201}],
    }
    r = ex.run_probe(p)
    assert r.verdict == Verdict.INCONCLUSIVE
    assert r.reason == InconclusiveReason.PROBE
    assert any("provisioned" in d for d in r.details)
    # No write was attempted or logged.
    assert r.writes == []


def test_write_setup_on_non_provisioned_target_is_inconclusive_probe(server):
    ex = Executor(target_url=server)
    p = _probe(
        setup=[{"type": "http", "act": {"method": "POST", "path": "/ok", "body": "s"}}],
        assert_=[{"type": "status", "equals": 200}],
    )
    r = ex.run_probe(p)
    assert r.verdict == Verdict.INCONCLUSIVE
    assert r.reason == InconclusiveReason.PROBE
    assert r.writes == []


def test_harness_app_setup_is_inconclusive_environment(server):
    # run_setup fails closed on harness_app even when validate_probe would
    # have allowed it (allow_harness_app=True).
    ex = Executor(target_url=server, allow_harness_app=True)
    logs, reason, detail, _ = ex.run_setup(
        [{"type": "harness_app", "language": "python", "source": "x", "entrypoint": "a"}]
    )
    assert reason == InconclusiveReason.ENVIRONMENT
    assert detail is not None and "WO-5" in detail
    assert any("WO-5" in line for line in logs)

    # And through run_probe: the probe is INCONCLUSIVE, not executed.
    p = _probe(
        setup=[{"type": "harness_app", "language": "python", "source": "x", "entrypoint": "a"}],
        assert_=[{"type": "status", "equals": 200}],
    )
    r = ex.run_probe(p)
    assert r.verdict == Verdict.INCONCLUSIVE
    assert r.reason == InconclusiveReason.ENVIRONMENT
    assert not any(d.startswith("act:") for d in r.details)


# ---------------------------------------------------------------------------
# WO-3: setup capture, strict placeholders, AUTH_TOKEN precedence, refresh.
# ---------------------------------------------------------------------------


class _AuthHandler(BaseHTTPRequestHandler):
    """Live server for WO-3: capture, placeholders, and credential refresh."""

    def _json(self, code, obj, headers=None):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(json.dumps(obj).encode())

    def do_GET(self):
        if self.path == "/me":
            # Echoes back the Authorization header it received.
            self._json(200, {"auth": self.headers.get("Authorization")})
        elif self.path == "/protected":
            if self.headers.get("Authorization") == "Bearer new-token":
                self._json(200, {"status": "ok"})
            else:
                self.send_response(401)
                self.end_headers()
                self.wfile.write(b"unauthorized")
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        if length:
            self.rfile.read(length)
        if self.path == "/login":
            self._json(200, {"token": "tok-123"})
        elif self.path == "/auth/refresh":
            self._json(200, {"access_token": "new-token"})
        elif self.path == "/auth/refresh-fail":
            self.send_response(500)
            self.end_headers()
        elif self.path == "/login-echo":
            # Returns the Authorization header it saw, for capture tests.
            self._json(200, {"seen": self.headers.get("Authorization")})
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args):
        pass


@pytest.fixture()
def auth_server():
    srv = HTTPServer(("127.0.0.1", 0), _AuthHandler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{port}"
    srv.shutdown()


def test_unknown_placeholder_is_inconclusive_probe(auth_server):
    # ${TYPO} is not env, fixture, AUTH_TOKEN, or a declared capture:
    # fail closed, never sent literally.
    ex = Executor(target_url=auth_server, provisioned=True)
    p = _probe(
        act={"method": "GET", "path": "/me", "headers": {"Authorization": "Bearer ${TYPO}"}},
        assert_=[{"type": "status", "equals": 200}],
    )
    r = ex.run_probe(p)
    assert r.verdict == Verdict.INCONCLUSIVE
    assert r.reason == InconclusiveReason.PROBE
    assert any("unknown placeholder" in d for d in r.details)


def test_capture_flows_into_later_steps(auth_server):
    # Setup POST /login captures TOKEN; the act sends it; the target echoes it.
    ex = Executor(target_url=auth_server, provisioned=True)
    p = _probe(
        setup=[
            {
                "type": "http",
                "act": {"method": "POST", "path": "/login"},
                "capture": {"TOKEN": {"from": "body", "json_path": "token"}},
            }
        ],
        act={"method": "GET", "path": "/me", "headers": {"Authorization": "Bearer ${TOKEN}"}},
        assert_=[
            {"type": "status", "equals": 200},
            {"type": "body", "json_path": {"path": "auth", "equals": "Bearer tok-123"}},
        ],
    )
    r = ex.run_probe(p)
    assert r.verdict == Verdict.PASS, r.details


def test_capture_from_header(auth_server):
    # Header captures work too (X-Token from a response).
    ex = Executor(target_url=auth_server, provisioned=True)
    p = _probe(
        setup=[
            {
                "type": "http",
                "act": {"method": "POST", "path": "/login"},
                "capture": {"CT": {"from": "header", "name": "Content-Type"}},
            }
        ],
        act={"method": "GET", "path": "/me", "headers": {"X-Cap": "${CT}"}},
        assert_=[{"type": "status", "equals": 200}],
    )
    r = ex.run_probe(p)
    assert r.verdict == Verdict.PASS, r.details


def test_missing_capture_is_inconclusive_probe(auth_server):
    # Declared capture whose json_path does not exist: the act referencing
    # it fails closed instead of sending "${TOKEN}" literally.
    ex = Executor(target_url=auth_server, provisioned=True)
    p = _probe(
        setup=[
            {
                "type": "http",
                "act": {"method": "POST", "path": "/login"},
                "capture": {"TOKEN": {"from": "body", "json_path": "nope.missing"}},
            }
        ],
        act={"method": "GET", "path": "/me", "headers": {"Authorization": "Bearer ${TOKEN}"}},
        assert_=[{"type": "status", "equals": 200}],
    )
    r = ex.run_probe(p)
    assert r.verdict == Verdict.INCONCLUSIVE
    assert r.reason == InconclusiveReason.PROBE
    assert any("never set" in d for d in r.details)


def test_auth_token_fixture_wins_over_capture(auth_server):
    # A setup step capturing AUTH_TOKEN cannot override the fixture's.
    ex = Executor(target_url=auth_server, provisioned=True, auth_token="fixture-secret")
    p = _probe(
        setup=[
            {
                "type": "http",
                "act": {"method": "POST", "path": "/login"},
                "capture": {"AUTH_TOKEN": {"from": "body", "json_path": "token"}},
            }
        ],
        act={
            "method": "POST",
            "path": "/login-echo",
            "headers": {"Authorization": "Bearer ${AUTH_TOKEN}"},
        },
        assert_=[
            {"type": "status", "equals": 200},
            {"type": "body", "json_path": {"path": "seen", "equals": "Bearer fixture-secret"}},
        ],
    )
    r = ex.run_probe(p)
    assert r.verdict == Verdict.PASS, r.details


def test_refresh_on_401_success(auth_server):
    # Expired fixture token -> 401 -> refresh -> retry with new token -> PASS.
    ex = Executor(
        target_url=auth_server,
        provisioned=True,
        auth_token="expired-token",
        refresh_config={
            "path": "/auth/refresh",
            "method": "POST",
            "token_json_path": "access_token",
        },
    )
    p = _probe(
        act={
            "method": "GET",
            "path": "/protected",
            "headers": {"Authorization": "Bearer ${AUTH_TOKEN}"},
        },
        assert_=[{"type": "status", "equals": 200}],
    )
    r = ex.run_probe(p)
    assert r.verdict == Verdict.PASS, r.details
    assert any("credential refresh succeeded" in d for d in r.details)


def test_refresh_on_401_failure(auth_server):
    # Refresh endpoint 500s: the probe is INCONCLUSIVE/auth, not a FAIL.
    ex = Executor(
        target_url=auth_server,
        provisioned=True,
        auth_token="expired-token",
        refresh_config={
            "path": "/auth/refresh-fail",
            "method": "POST",
            "token_json_path": "access_token",
        },
    )
    p = _probe(
        act={
            "method": "GET",
            "path": "/protected",
            "headers": {"Authorization": "Bearer ${AUTH_TOKEN}"},
        },
        assert_=[{"type": "status", "equals": 200}],
    )
    r = ex.run_probe(p)
    assert r.verdict == Verdict.INCONCLUSIVE
    assert r.reason == InconclusiveReason.AUTH
    assert any("credential refresh failed" in d for d in r.details)


def test_no_refresh_without_config(auth_server):
    # Without a refresh config, a 401 on the act is just a 401: the
    # assertions decide the verdict (here FAIL, since 200 was expected).
    ex = Executor(target_url=auth_server, provisioned=True, auth_token="expired-token")
    p = _probe(
        act={
            "method": "GET",
            "path": "/protected",
            "headers": {"Authorization": "Bearer ${AUTH_TOKEN}"},
        },
        assert_=[{"type": "status", "equals": 200}],
    )
    r = ex.run_probe(p)
    assert r.verdict == Verdict.FAIL


def test_capture_schema_rejected():
    # The schema validates capture specs: bad 'from', bad name, unknown keys.
    from proofdeploy.probe import ProbeRejected, validate_probe

    bad_from = _probe(
        setup=[
            {
                "type": "http",
                "act": {"method": "GET", "path": "/ok"},
                "capture": {"T": {"from": "cookie", "json_path": "t"}},
            }
        ],
        assert_=[{"type": "status", "equals": 200}],
    )
    with pytest.raises(ProbeRejected):
        validate_probe(bad_from)

    bad_field = _probe(
        setup=[
            {
                "type": "http",
                "act": {"method": "GET", "path": "/ok"},
                "capture": {"T": {"from": "body"}},
            }
        ],
        assert_=[{"type": "status", "equals": 200}],
    )
    with pytest.raises(ProbeRejected):
        validate_probe(bad_field)

    ok = _probe(
        setup=[
            {
                "type": "http",
                "act": {"method": "GET", "path": "/ok"},
                "capture": {"T": {"from": "body", "json_path": "a.b"}},
            }
        ],
        assert_=[{"type": "status", "equals": 200}],
    )
    validate_probe(ok)  # no raise
