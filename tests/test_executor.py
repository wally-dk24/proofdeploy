"""Tests for the probe executor (WAL-58)."""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from proofdeploy.executor import Executor, Verdict


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
    ex = Executor(target_url=server)
    p = {"act": {"method": "POST", "path": "/ok", "body": "hi"},
         "assert": [{"type": "status", "equals": 201}]}
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
