"""Addendum 36: ONE replay implementation, in the product
(proofdeploy/replay.py), running the executor's own verdict loop.

Covered: per-probe re-decision from recorded responses; the
executor's first-inconclusive-reason semantics (the kit's old
re-implementation kept the LAST reason, which could be None);
typed NotReplayableError for db assertions and truncated bodies;
and evaluate_assertions' precedence (FAIL > INCONCLUSIVE > PASS).
"""
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from proofdeploy.executor import (
    Executor,
    ProbeResult,
    Verdict,
    evaluate_assertions,
)
from proofdeploy.replay import (
    NotReplayableError,
    replay_side,
    replay_verdict,
)


def _result(**kw):
    base = {
        "probe_index": 0, "verdict": "pass", "sent": True,
        "http_status": 200, "http_headers": {},
        "http_body": json.dumps({"done": False}), "reason": None,
        "http_body_truncated": False,
        # Addendum 38: the live run DID evaluate this probe's
        # assertions (step 5), so replay may re-decide it.
        "assertions_evaluated": True,
    }
    base.update(kw)
    return base


def _evidence(parent_body, fix_body, assertions=None):
    probe = {
        "act": {"method": "GET", "path": "/notes/1"},
        "setup": [],
        "assert": assertions or [
            {"type": "status", "equals": 200},
            {"type": "body",
             "json_path": {"path": "done", "equals": False}},
        ],
    }
    return {
        "record_type": "evidence", "run_kind": "bug",
        "parsed_probes": [probe],
        "fix_parent_results": [
            _result(verdict="fail", http_body=json.dumps(parent_body))
        ],
        "fix_results": [
            _result(verdict="pass", http_body=json.dumps(fix_body))
        ],
    }


def test_replay_redecides_from_recorded_responses():
    ev = _evidence({"done": True}, {"done": False})
    assert replay_verdict(ev) == "candidate_catch"
    parent = replay_side(ev, "fix_parent_results")
    fix = replay_side(ev, "fix_results")
    assert parent[0].verdict.value == "fail"
    assert fix[0].verdict.value == "pass"


def test_replay_keeps_first_inconclusive_reason():
    """Executor semantics: the FIRST inconclusive reason wins
    (here `probe` from an unknown assertion type, before a `drift`
    json_path miss). A last-reason implementation returns drift —
    or None."""
    assertions = [
        {"type": "mystery"},  # -> INCONCLUSIVE, reason probe
        {"type": "body",
         "json_path": {"path": "missing", "equals": 1}},  # -> drift
    ]
    ev = _evidence({"done": True}, {"done": False}, assertions)
    fix = replay_side(ev, "fix_results")
    assert fix[0].verdict.value == "inconclusive"
    assert fix[0].reason is not None
    assert fix[0].reason.value == "probe"


def test_evaluate_assertions_precedence():
    verdict, reason, _ = evaluate_assertions(
        [
            {"type": "mystery"},  # inconclusive
            {"type": "status", "equals": 201},  # fail
        ],
        200, {}, "{}", {},
    )
    assert verdict.value == "fail"
    assert reason is None
    verdict, reason, _ = evaluate_assertions(
        [{"type": "status", "equals": 200}], 200, {}, "{}", {},
    )
    assert verdict.value == "pass"


def test_replay_db_assertion_is_typed_not_replayable():
    assertions = [{"type": "db", "query": "select 1", "count": 1}]
    ev = _evidence({"done": True}, {"done": False}, assertions)
    with pytest.raises(NotReplayableError, match="db assertions"):
        replay_side(ev, "fix_results")


def test_replay_truncated_body_is_typed_not_replayable():
    ev = _evidence({"done": True}, {"done": False})
    ev["fix_results"][0]["http_body_truncated"] = True
    with pytest.raises(NotReplayableError, match="truncated"):
        replay_side(ev, "fix_results")


# ---------------------------------------------------------------------------
# Addendum 38: replay must not invent verdicts. A probe whose live
# run returned before step 5 never had its assertions evaluated;
# the record now says so (assertions_evaluated), and replay carries
# the recorded INCONCLUSIVE + reason, labeled not re-decided —
# never a fabricated PASS or FAIL.
# ---------------------------------------------------------------------------


def _unevaluated(**kw):
    base = _result(
        verdict="inconclusive", reason="environment",
        http_status=None, http_headers={}, http_body="",
        assertions_evaluated=False,
    )
    base.update(kw)
    return base


def _evidence_with(result, assertions):
    probe = {
        "act": {"method": "GET", "path": "/x"},
        "setup": [],
        "assert": assertions,
    }
    return {
        "record_type": "evidence", "run_kind": "bug",
        "parsed_probes": [probe],
        "fix_parent_results": [result],
        "fix_results": [result],
    }


def test_replay_no_response_record_is_not_redecided_as_fail():
    """(a) http_status null — the live run was INCONCLUSIVE before
    step 5. Evaluating the header/body-contains assertions against
    the empty recorded response would produce FAIL: on fix^ that
    fabricates a catch that never happened."""
    assertions = [
        {"type": "header", "name": "x-flag", "equals": "on"},
        {"type": "body", "contains": "expected text"},
    ]
    ev = _evidence_with(_unevaluated(), assertions)
    out = replay_side(ev, "fix_results")
    assert out[0].verdict.value == "inconclusive"
    assert out[0].reason.value == "environment"
    assert any("not re-decided" in d for d in out[0].details)


def test_replay_unevaluated_record_keeps_its_recorded_reason():
    """(b) a setup capture was never set: live INCONCLUSIVE with
    reason `probe`. This record predates assertions_evaluated (the
    field is ABSENT); replay must keep the recorded reason instead
    of re-deriving `environment` from the null status."""
    result = _unevaluated(reason="probe")
    del result["assertions_evaluated"]
    assertions = [{"type": "status", "equals": 200}]
    ev = _evidence_with(result, assertions)
    out = replay_side(ev, "fix_results")
    assert out[0].verdict.value == "inconclusive"
    assert out[0].reason.value == "probe"
    assert any("not re-decided" in d for d in out[0].details)


def test_replay_capture_substitution_is_not_replayable():
    """(c) the assertion compares against ${USER_ID}: live, the
    value was substituted from a setup capture; captures are not
    recorded, so a literal replay comparison invents a FAIL."""
    assertions = [
        {"type": "body",
         "json_path": {"path": "owner_id", "equals": "${USER_ID}"}},
    ]
    result = _result(http_body=json.dumps({"owner_id": "u-123"}))
    ev = _evidence_with(result, assertions)
    with pytest.raises(NotReplayableError, match="capture"):
        replay_side(ev, "fix_results")


def test_replay_redacted_body_is_not_replayable():
    """(d) the recorded body holds a redacted value: a body
    assertion would be decided against ***REDACTED_JWT***, not
    against the response the app actually sent."""
    assertions = [{"type": "body", "contains": "session"}]
    result = _result(
        http_body=json.dumps({"session": "***REDACTED_JWT***"})
    )
    ev = _evidence_with(result, assertions)
    with pytest.raises(NotReplayableError, match="redacted"):
        replay_side(ev, "fix_results")


def test_replay_redacted_header_is_not_replayable():
    """(d) same for a header assertion: the recorded header value
    is the redaction marker, not the value the app sent."""
    assertions = [
        {"type": "header", "name": "x-session", "contains": "abc"},
    ]
    result = _result(http_headers={"x-session": "***REDACTED***"})
    ev = _evidence_with(result, assertions)
    with pytest.raises(NotReplayableError, match="redacted"):
        replay_side(ev, "fix_results")


# ---------------------------------------------------------------------------
# Addendum 38: assertions_evaluated is RECORDED per result, and
# every early return in run_probe is INCONCLUSIVE.
# ---------------------------------------------------------------------------


class _BigHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/big":
            body = "x" * 2500
        else:
            body = json.dumps({"ok": True})
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, *args):
        pass


@pytest.fixture()
def live_server():
    srv = HTTPServer(("127.0.0.1", 0), _BigHandler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def _closed_port_url():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return f"http://127.0.0.1:{s.getsockname()[1]}"


def test_every_early_return_is_inconclusive_and_records_not_evaluated(
    live_server,
):
    """Each fail-closed path out of run_probe BEFORE step 5 yields
    INCONCLUSIVE and records assertions_evaluated: false — the
    record must never claim assertions were evaluated when the run
    returned early."""
    dead = _closed_port_url()
    cases = [
        # Remote write method: refused before any request.
        (
            Executor(target_url="http://example.com"),
            {"act": {"method": "POST", "path": "/x"}, "assert": []},
        ),
        # Write gate: POST to a local target that was not
        # provisioned.
        (
            Executor(target_url=live_server),
            {"act": {"method": "POST", "path": "/ok"}, "assert": []},
        ),
        # Unknown setup type: fail closed in setup.
        (
            Executor(target_url=live_server),
            {
                "act": {"method": "GET", "path": "/ok"},
                "setup": [{"type": "bogus"}],
                "assert": [],
            },
        ),
        # Setup HTTP step against an unreachable target.
        (
            Executor(target_url=dead),
            {
                "act": {"method": "GET", "path": "/ok"},
                "setup": [
                    {"type": "http",
                     "act": {"method": "GET", "path": "/"}}
                ],
                "assert": [],
            },
        ),
        # Act placeholder naming an unknown binding: refused
        # before the request is sent.
        (
            Executor(target_url=live_server),
            {
                "act": {"method": "GET", "path": "/${NOPE}"},
                "assert": [],
            },
        ),
        # Act against an unreachable target: no response.
        (
            Executor(target_url=dead),
            {"act": {"method": "GET", "path": "/ok"}, "assert": []},
        ),
    ]
    for ex, probe in cases:
        r = ex.run_probe(probe)
        assert r.verdict is Verdict.INCONCLUSIVE, (probe, r)
        assert r.reason is not None, (probe, r)
        assert r.assertions_evaluated is False, (probe, r)
        assert r.to_dict()["assertions_evaluated"] is False, (probe, r)


def test_evaluated_probe_records_assertions_evaluated(live_server):
    """The step-5 path: assertions WERE evaluated, and the record
    says so (this is what licenses replay to re-decide)."""
    ex = Executor(target_url=live_server)
    r = ex.run_probe(
        {
            "act": {"method": "GET", "path": "/ok"},
            "assert": [{"type": "status", "equals": 200}],
        }
    )
    assert r.verdict is Verdict.PASS
    assert r.assertions_evaluated is True
    assert r.to_dict()["assertions_evaluated"] is True


def test_http_body_truncated_boundary():
    """N8 (Addendum 38): the recorded flag is exactly
    len(body) > 2000 — a body of exactly 2000 records false and is
    stored whole; 2001 records true and is stored cut to 2000."""
    at = ProbeResult(
        probe_index=0, verdict=Verdict.PASS, http_body="x" * 2000
    )
    d = at.to_dict()
    assert d["http_body_truncated"] is False
    assert len(d["http_body"]) == 2000
    over = ProbeResult(
        probe_index=0, verdict=Verdict.PASS, http_body="x" * 2001
    )
    d = over.to_dict()
    assert d["http_body_truncated"] is True
    assert len(d["http_body"]) == 2000


def test_http_body_truncated_recorded_from_a_live_run(live_server):
    """N8 end to end: a live 2500-char response records
    http_body_truncated: true with the stored body cut to 2000."""
    ex = Executor(target_url=live_server)
    r = ex.run_probe(
        {
            "act": {"method": "GET", "path": "/big"},
            "assert": [{"type": "status", "equals": 200}],
        }
    )
    assert r.verdict is Verdict.PASS
    d = r.to_dict()
    assert d["http_body_truncated"] is True
    assert len(d["http_body"]) == 2000
