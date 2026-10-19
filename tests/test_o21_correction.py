# ruff: noqa: E501  (long embedded stub JSON / fixture lines)
"""R-METHOD (D242): the correction round in `Orchestrator._author_probes`.

Real golden bundles and prompts, a stub model (no model, no network).
Covers: a correction that fixes the answer, one that does not, the
transport retry, at most one correction call, the record fields and
the schema version.
"""

from __future__ import annotations

import json

import o21_helpers as h
import pytest

from proofdeploy.evidence import analyse_answer, correction_message, sha256_text
from proofdeploy.model_client import ModelCallError
from proofdeploy.probe import SCHEMA_VERSION

APP = "node-express-sqlite"


def _get_note(*assertions: dict) -> dict:
    return {
        "setup": [],
        "act": {
            "method": "GET",
            "path": "/notes/1",
            "headers": {"Authorization": "Bearer ${AUTH_TOKEN}"},
        },
        "assert": list(assertions),
    }


def _done(value: bool, source: str, quote: str) -> dict:
    return {
        "type": "body",
        "json_path": {"path": "done", "equals": value},
        "evidence": {"source": source, "quote": quote},
    }


# right: the fixture says note 1 is done=false
GOOD = _done(False, "fixture", "id 1 (done=false)")
# wrong direction: asserts the diff's value and cites the changed line
BAD_CHANGED = _done(True, "server.js", "done: note.done !== 1")
# wrong direction, quoting the fixture: the quote lacks "true"
BAD_DIRECTION = _done(True, "fixture", "id 1 (done=false)")


class Runner:
    """A stub model: answers in order, records every prompt it was sent."""

    def __init__(self, *steps):
        self.steps = list(steps)
        self.prompts: list[str] = []

    def __call__(self, prompt: str):
        self.prompts.append(prompt)
        step = self.steps[len(self.prompts) - 1]
        if isinstance(step, BaseException):
            raise step
        return h.response(step, usage={"total_tokens": len(self.prompts)})


@pytest.fixture(scope="module")
def golden(tmp_path_factory):
    return h.golden_bundle(tmp_path_factory.mktemp("o21corr"), APP)


def _author(golden, runner, monkeypatch=None, sleeps=None):
    golden.orch.cfg.model_runner = runner
    if monkeypatch is not None:
        monkeypatch.setattr(
            "proofdeploy.orchestrator.time.sleep", lambda s: sleeps.append(s)
        )
    return golden.orch._author_probes(golden.prompt, golden.sources)


def test_a_correct_first_answer_is_one_call_and_no_correction(golden):
    runner = Runner(h.probe_answer(_get_note(GOOD)))
    probeset, raw = _author(golden, runner)
    assert len(runner.prompts) == 1
    ev = probeset.author_evidence
    assert ev["correction_used"] is False
    assert ev["correction_prompt_sha256"] is None
    assert ev["second_answer"] is None and ev["final_answer"] == "first"
    assert [c["result"] for c in ev["checks"]["r1"]] == ["ok"]
    assert ev["checks"]["r2"] is None
    assert len(probeset.probes) == 1 and not probeset.invalid_blocks


def test_d_a_correction_that_fixes_the_answer_runs_the_probe(golden):
    first = h.probe_answer(_get_note(BAD_CHANGED))
    second = h.probe_answer(_get_note(GOOD))
    runner = Runner(first, second)
    probeset, raw = _author(golden, runner)
    assert len(runner.prompts) == 2
    # the follow-up is exactly: prompt + separator + first answer + message
    a1 = analyse_answer(first, golden.sources)
    msg = correction_message(a1)
    assert "- probe 1, body assertion: evidence_self_justifying" in msg
    want = golden.prompt + "\n\n---\n\nYour previous answer:\n\n" + first + "\n\n" + msg
    assert runner.prompts[1] == want
    assert runner.prompts[0] == golden.prompt
    # the second answer is final and the probe runs with the corrected assertion
    assert raw.content == second
    assert probeset.probes[0]["assert"] == [GOOD]
    assert not probeset.invalid_blocks
    ev = probeset.author_evidence
    assert ev["correction_used"] is True
    assert ev["correction_prompt_sha256"] == sha256_text(want)
    assert ev["final_answer"] == "second"
    assert ev["first_answer"]["content"] == first and ev["second_answer"]["content"] == second
    assert ev["first_answer"]["finish_reason"] == "stop"
    assert ev["first_answer"]["usage"] == {"total_tokens": 1}
    assert ev["second_answer"]["usage"] == {"total_tokens": 2}
    assert [c["result"] for c in ev["checks"]["r1"]] == ["evidence_self_justifying"]
    assert [c["result"] for c in ev["checks"]["r2"]] == ["ok"]


def test_d_a_correction_that_does_not_fix_it_drops_the_assertion(golden):
    first = h.probe_answer(_get_note(BAD_CHANGED, GOOD))
    second = h.probe_answer(_get_note(BAD_DIRECTION, GOOD))
    runner = Runner(first, second)
    probeset, raw = _author(golden, runner)
    assert len(runner.prompts) == 2
    assert [c["result"] for c in probeset.author_evidence["checks"]["r2"]] == [
        "evidence_direction_mismatch", "ok",
    ]
    # the rejected assertion is gone, the good one stays, the probe runs
    assert probeset.probes[0]["assert"] == [GOOD]
    assert not probeset.invalid_blocks


def test_d_with_no_assertion_left_the_block_is_invalid_and_never_sent(golden):
    first = h.probe_answer(_get_note(BAD_CHANGED))
    second = h.probe_answer(_get_note(BAD_DIRECTION))
    runner = Runner(first, second)
    probeset, raw = _author(golden, runner)
    assert probeset.probes == []
    (inv,) = probeset.invalid_blocks
    assert inv["block_index"] == 0
    assert json.loads(inv["raw_text"]) == _get_note(BAD_DIRECTION)
    assert "evidence_direction_mismatch" in inv["rejection_reason"]
    assert raw.content == second


def test_a_schema_invalid_block_also_gets_the_one_correction(golden):
    broken = '{"act": {"method": "GET", "path": "/notes/1"}}'
    first = "```json\n" + broken + "\n```\n" + h.probe_answer(_get_note(GOOD))
    runner = Runner(first, h.probe_answer(_get_note(GOOD)))
    probeset, _raw = _author(golden, runner)
    assert len(runner.prompts) == 2
    assert "- probe 1: schema_error: block 0: probe needs 'assert'" in runner.prompts[1]
    assert len(probeset.probes) == 1 and not probeset.invalid_blocks


def test_an_answer_without_any_block_is_not_corrected(golden):
    runner = Runner("I could not devise a probe.")
    probeset, _raw = _author(golden, runner)
    assert len(runner.prompts) == 1
    assert probeset.probes == [] and probeset.invalid_blocks[0]["block_index"] == -1
    assert probeset.author_evidence["correction_used"] is False


def test_h_at_most_one_correction_call(golden):
    bad = h.probe_answer(_get_note(BAD_CHANGED))
    runner = Runner(bad, bad, bad, bad)
    probeset, _raw = _author(golden, runner)
    assert len(runner.prompts) == 2  # the original and the one correction
    assert probeset.probes == [] and len(probeset.invalid_blocks) == 1


# ---------------------------------------------------------------------
# (e) the correction call's transport failure
# ---------------------------------------------------------------------


def test_e_transport_failure_is_retried_once_after_60s(golden, monkeypatch):
    first = h.probe_answer(_get_note(BAD_CHANGED, GOOD))
    second = h.probe_answer(_get_note(GOOD))
    runner = Runner(first, ModelCallError("connection reset", "model_transport"), second)
    sleeps: list[int] = []
    probeset, raw = _author(golden, runner, monkeypatch, sleeps)
    assert sleeps == [60]
    assert len(runner.prompts) == 3
    assert runner.prompts[1] == runner.prompts[2]  # the same follow-up, retried
    assert raw.content == second
    ev = probeset.author_evidence
    assert ev["final_answer"] == "second" and ev["correction_call_error"] is None
    assert ev["correction_call_attempts"] == [
        "correction_attempt_1: model_transport: connection reset",
        "correction_attempt_2: success",
    ]


def test_e_a_bare_runtime_error_counts_as_transport(golden, monkeypatch):
    first = h.probe_answer(_get_note(BAD_CHANGED, GOOD))
    runner = Runner(first, RuntimeError("model API call failed: timed out"), h.probe_answer(_get_note(GOOD)))
    sleeps: list[int] = []
    probeset, _raw = _author(golden, runner, monkeypatch, sleeps)
    assert sleeps == [60] and len(runner.prompts) == 3
    assert probeset.author_evidence["final_answer"] == "second"


def test_e_two_failures_use_the_first_answer_minus_its_rejections(golden, monkeypatch):
    first = h.probe_answer(_get_note(BAD_CHANGED, GOOD))
    runner = Runner(
        first,
        ModelCallError("down", "model_transport"),
        ModelCallError("still down", "model_transport"),
    )
    sleeps: list[int] = []
    probeset, raw = _author(golden, runner, monkeypatch, sleeps)
    assert sleeps == [60] and len(runner.prompts) == 3
    assert raw.content == first
    assert probeset.probes[0]["assert"] == [GOOD]  # BAD_CHANGED removed
    ev = probeset.author_evidence
    assert ev["correction_used"] is True and ev["final_answer"] == "first"
    assert ev["second_answer"] is None and ev["checks"]["r2"] is None
    assert "still down" in ev["correction_call_error"]
    assert ev["correction_call_attempts"] == [
        "correction_attempt_1: model_transport: down",
        "correction_attempt_2: model_transport: still down",
    ]


def test_e_an_overflow_of_the_correction_is_not_retried(golden, monkeypatch):
    first = h.probe_answer(_get_note(BAD_CHANGED, GOOD))
    runner = Runner(first, ModelCallError("context length", "model_overflow"))
    sleeps: list[int] = []
    probeset, raw = _author(golden, runner, monkeypatch, sleeps)
    assert sleeps == [] and len(runner.prompts) == 2
    assert raw.content == first and probeset.probes[0]["assert"] == [GOOD]


def test_the_first_call_failing_still_raises_for_the_callers_retry(golden):
    runner = Runner(ModelCallError("down", "model_transport"))
    with pytest.raises(ModelCallError):
        _author(golden, runner)


# ---------------------------------------------------------------------
# the record
# ---------------------------------------------------------------------


def test_record_carries_both_answers_the_prompt_hash_and_the_checks(tmp_path):
    """measure_bug on a golden unit whose author never gets valid evidence:
    no probe is provisioned (author_output_invalid), the record is complete."""
    from proofdeploy.runpair import read_records

    bad = h.probe_answer(_get_note(BAD_CHANGED))
    g = h.golden_bundle(tmp_path, APP, model_runner=Runner(bad, bad))
    g.cfg.output_dir = tmp_path / "records"
    g.cfg.workdir = tmp_path / "measure-work"
    summary = g.orch.measure_bug()
    # a bug unit with no valid probe counts as not caught (Addendum 11 rule 6)
    assert summary["verdict"] == "no_catch"
    assert summary["cause"] == "author_output_invalid"
    (evidence,) = [r for r in read_records(g.cfg.output_dir) if r["record_type"] == "evidence"]
    assert evidence["schema_version"] == SCHEMA_VERSION == "1.9.0"
    ae = evidence["author_evidence"]
    assert ae["correction_used"] is True
    assert ae["first_answer"]["content"] == bad and ae["second_answer"]["content"] == bad
    assert len(ae["correction_prompt_sha256"]) == 64
    assert ae["first_answer"]["finish_reason"] == "stop"
    assert [c["result"] for c in ae["checks"]["r1"]] == ["evidence_self_justifying"]
    assert [c["result"] for c in ae["checks"]["r2"]] == ["evidence_self_justifying"]
    assert evidence["raw_model_response"] == bad
    assert len(evidence["invalid_probe_blocks"]) == 1
    # the route map and the flag are in the record too
    assert evidence["bundle_manifest"]["route_map"]["shown_above"] == ["server.js"]
    assert evidence["truncation_flags"]["route_map_truncated"] is False


def test_schema_version_is_1_9_0_and_older_records_still_load():
    from proofdeploy.runpair import build_evidence_record
    from proofdeploy.versions import check_schema_version

    assert SCHEMA_VERSION == "1.9.0"
    for old in ("1.0.0", "1.7.0", "1.8.0", SCHEMA_VERSION):
        check_schema_version(old)  # does not raise
    from proofdeploy.versions import UnknownSchemaVersionError

    with pytest.raises(UnknownSchemaVersionError):
        check_schema_version("2.0.0")
    rec = build_evidence_record(run_kind="bug")
    assert rec["schema_version"] == "1.9.0" and rec["author_evidence"] is None
