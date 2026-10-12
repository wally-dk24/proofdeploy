"""Addendum 36: ONE replay implementation, in the product
(proofdeploy/replay.py), running the executor's own verdict loop.

Covered: per-probe re-decision from recorded responses; the
executor's first-inconclusive-reason semantics (the kit's old
re-implementation kept the LAST reason, which could be None);
typed NotReplayableError for db assertions and truncated bodies;
and evaluate_assertions' precedence (FAIL > INCONCLUSIVE > PASS).
"""
import json

import pytest

from proofdeploy.executor import evaluate_assertions
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
