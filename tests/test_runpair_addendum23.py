"""Addendum 23 — WO-5.2: repeated capture names (R1) and record provenance (R2).

R1: when the same capture name is bound in several probes, or on both
sides of the pair, EVERY value is a secret. Before this fix,
``build_evidence_record`` merged per-probe captured bindings with
``dict.setdefault``, so only the first value per name reached
``record["captured_values"]`` — and therefore only that value was
collected for redaction. The later values (e.g. the fix side's session
token, because each side logs in separately) were written to the
committed record in plaintext. The same defect existed one level down:
``SetupContext.run_captures`` kept only the latest value when a single
probe run captured the same name twice.

R2: the evidence record names the ProofDeploy commit that produced it,
so the audit trail does not have to infer the code version from
behavior. Round 1 recorded it as a top-level ``proofdeploy_commit``;
Addendum 24 replaced that with the ``provenance.harness`` object
(commit, source, dirty, workflow_ref) — see
tests/test_runpair_addendum24.py.
"""

import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from proofdeploy.executor import Executor, ProbeResult, Verdict
from proofdeploy.runpair import (
    REDACTED,
    build_evidence_record,
    collect_record_secret_values,
    collect_secret_values,
    read_records,
    write_evidence_record,
)

REPO_ROOT = Path(__file__).resolve().parent.parent

# Four distinct session tokens under ONE capture name: two probes, both
# sides. A login-style app mints a fresh token per login, so this is the
# normal case, not an edge case.
TOKENS = {
    "parent_probe0": "sess-parent-probe0-aaaa1111",
    "parent_probe1": "sess-parent-probe1-bbbb2222",
    "fix_probe0": "sess-fix-probe0-cccc3333",
    "fix_probe1": "sess-fix-probe1-dddd4444",
}


def _git_repo(path):
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)
    (path / "README.md").write_text("x")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=path, check=True)
    return path


def _result_with_capture(verdict: Verdict, index: int, token: str) -> ProbeResult:
    """A probe result whose run captured ``token`` as SESSION and echoed it."""
    r = ProbeResult(
        probe_index=index,
        verdict=verdict,
        details=[f"setup login returned session {token}"],
        http_body=json.dumps({"session_echo": token}),
        request={
            "method": "GET",
            "url": f"http://127.0.0.1:8000/me/{token}",
            "headers": {},
            "body": None,
        },
    )
    r.captured_bindings = {"SESSION": token}
    return r


def _record_with_repeated_captures() -> dict:
    return build_evidence_record(
        run_kind="bug",
        bug_id="ADDENDUM-23",
        fix_sha="a" * 40,
        fix_parent_sha="b" * 40,
        fix_parent_results=[
            _result_with_capture(Verdict.FAIL, 0, TOKENS["parent_probe0"]),
            _result_with_capture(Verdict.FAIL, 1, TOKENS["parent_probe1"]),
        ],
        fix_results=[
            _result_with_capture(Verdict.PASS, 0, TOKENS["fix_probe0"]),
            _result_with_capture(Verdict.PASS, 1, TOKENS["fix_probe1"]),
        ],
    )


def test_repeated_capture_name_collects_every_value():
    record = _record_with_repeated_captures()
    collected = set(collect_record_secret_values(record))
    assert set(TOKENS.values()) <= collected


def test_repeated_capture_name_all_values_redacted_in_written_record(tmp_path):
    repo = _git_repo(tmp_path)
    out = tmp_path / "runs"
    record = _record_with_repeated_captures()
    _, path, _ = write_evidence_record(record, out, repo)
    blob = path.read_text(encoding="utf-8")
    for token in TOKENS.values():
        assert token not in blob, "a repeated capture value leaked in plaintext"
    written = read_records(out)[0]
    # Every value survived into captured_values (as redaction markers):
    # nothing was dropped by the merge.
    assert written["captured_values"]["SESSION"] == [REDACTED] * 4


def test_collectors_collect_every_value_of_a_repeated_name():
    toks = ["sess-one-aaaa1111", "sess-two-bbbb2222"]
    captured = {"SESSION": list(toks)}
    assert set(toks) <= set(collect_secret_values(captured=captured))
    assert set(toks) <= set(collect_record_secret_values({"captured_values": captured}))


def test_recapture_within_one_probe_run_keeps_every_value(tmp_path):
    # One probe run whose setup logs in twice under the same capture
    # name (e.g. a re-login): both tokens are secrets of that run.
    issued: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            token = f"sess-login-{len(issued) + 1}-0123456789abcdef"
            issued.append(token)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"session": token}).encode())

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        ex = Executor(target_url=f"http://127.0.0.1:{port}", provisioned=True)
        login = {
            "type": "http",
            "act": {"method": "POST", "path": "/login"},
            "capture": {"SESSION": {"from": "body", "json_path": "session"}},
        }
        probe = {
            "setup": [login, dict(login)],
            "act": {"method": "GET", "path": "/me"},
            "assert": [{"type": "status", "equals": 200}],
        }
        r = ex.run_probe(probe, 0)
        assert r.verdict == Verdict.PASS
        assert len(issued) == 2

        record = build_evidence_record(
            run_kind="bug",
            bug_id="ADDENDUM-23",
            fix_sha="a" * 40,
            fix_parent_sha="b" * 40,
            fix_parent_results=[r],
            fix_results=[r],
        )
        collected = set(collect_record_secret_values(record))
        assert set(issued) <= collected

        repo = _git_repo(tmp_path)
        out = tmp_path / "runs"
        _, path, _ = write_evidence_record(record, out, repo)
        blob = path.read_text(encoding="utf-8")
        for token in issued:
            assert token not in blob
    finally:
        srv.shutdown()
    srv.server_close()


def test_evidence_record_names_proofdeploy_commit(tmp_path):
    # Addendum 24: the commit now lives in provenance.harness.
    expected = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    record = build_evidence_record(
        run_kind="bug",
        bug_id="ADDENDUM-23",
        fix_sha="a" * 40,
        fix_parent_sha="b" * 40,
    )
    harness = record["provenance"]["harness"]
    assert harness["commit"] == expected
    assert harness["source"] == "git"

    repo = _git_repo(tmp_path)
    out = tmp_path / "runs"
    write_evidence_record(record, out, repo)
    written = read_records(out)[0]
    assert written["provenance"]["harness"]["commit"] == expected
