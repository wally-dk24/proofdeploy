"""Lean P5 (D258): the judge and the verdict. No real model call: every test
passes a stub runner to the judge."""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from proofdeploy.lean.judge import (
    JUDGE_PROMPT,
    Difference,
    HardFailure,
    JudgeContext,
    estimate_tokens,
    group_differences,
    path_template,
    plan_calls,
)
from proofdeploy.lean.verdict import (
    FAIL,
    INCONCLUSIVE,
    PASS,
    LeanRecord,
    ReplayMismatch,
    decide,
    replay,
    run_lean,
)
from proofdeploy.model_client import ModelCallError, ModelResponse

FACTS = [
    "seed user seeduser/seedpass owns two notes after seed: id 1 (done=false) and id 2 (done=true)"
]
INTENT = "return the note's done flag"
HUNKS = (
    "diff --git a/app.py b/app.py\n"
    "@@ -10,3 +10,3 @@ def get_note\n"
    '-    return {"id": n.id, "done": False}\n'
    '+    return {"id": n.id, "done": n.done}\n'
)


class Stub:
    """A model runner returning canned answers; records the prompts."""

    def __init__(self, *answers: Any, usage: dict[str, Any] | None = None):
        self.answers = list(answers)
        self.prompts: list[str] = []
        self.usage = (
            usage
            if usage is not None
            else {"prompt_tokens": 500, "completion_tokens": 100, "total_tokens": 600}
        )

    def __call__(self, prompt: str) -> ModelResponse:
        self.prompts.append(prompt)
        assert self.answers, "unexpected model call"
        a = self.answers.pop(0)
        if isinstance(a, BaseException):
            raise a
        content = a if isinstance(a, str) else json.dumps(a)
        return ModelResponse(content=content, finish_reason="stop", usage=self.usage)


def diff(
    rid: str = "r1",
    path: str = "/notes/1",
    fp: str = "done",
    before: Any = False,
    after: Any = True,
    kind: str = "changed",
    method: str = "GET",
) -> Difference:
    return Difference(rid, method, path, None, fp, before, after, kind)


def ctx(intent: str = INTENT, hunks: str = HUNKS, facts: list[str] | None = None) -> JudgeContext:
    return JudgeContext(intent, hunks, FACTS if facts is None else facts, {})


def answer(
    verdict: str,
    source: str = "facts",
    quote: str = "id 1 (done=false)",
    expected: Any = False,
    group: int = 1,
    question: str | None = None,
) -> dict[str, Any]:
    a: dict[str, Any] = {
        "group": group,
        "verdict": verdict,
        "expected_value": expected,
        "evidence": {"source": source, "quote": quote},
    }
    if question is not None:
        a["question"] = question
    return a


def lean(
    diffs: list[Difference],
    stub: Stub | None,
    c: JudgeContext | None = None,
    *,
    hard: list[HardFailure] | None = None,
    unexercised: list[str] | None = None,
    observable: bool = True,
    **kw: Any,
) -> LeanRecord:
    return run_lean(diffs, hard or [], c or ctx(), unexercised or [], observable, runner=stub, **kw)


# --- grouping ---------------------------------------------------------------


def test_path_template():
    assert path_template("/notes/1") == "/notes/{id}"
    assert path_template("/u/123e4567-e89b-12d3-a456-426614174000/x?y=1") == "/u/{id}/x"
    assert path_template("/notes/abc", ["/notes/{note_id}"]) == "/notes/{note_id}"


def test_grouping_dedups_across_requests_and_keeps_ids():
    ds = [
        diff("r1", "/notes/1"),
        diff("r2", "/notes/2"),
        diff("r2", "/notes/2"),
        diff("r3", "/notes/3", after=False),
        diff("r4", "/notes/4", fp="title"),
        diff("r5", "/notes/5", method="POST"),
    ]
    groups = group_differences(ds)
    assert len(groups) == 4
    assert groups[0].request_ids == ["r1", "r2"] and groups[0].path_template == "/notes/{id}"
    assert [g.index for g in groups] == [1, 2, 3, 4]
    # bool true is not int 1
    assert len(group_differences([diff(after=True), diff("r2", after=1)])) == 2


def test_difference_kind_validated():
    with pytest.raises(ValueError):
        diff(kind="nope")
    with pytest.raises(ValueError):
        HardFailure("r", "nope", "")


# --- batching ---------------------------------------------------------------


def test_batching_under_the_cap_and_one_call_when_small():
    ds = [diff(f"r{i}", f"/notes/{i}", fp=f"f{i}") for i in range(12)]
    plans, too_big = plan_calls(group_differences(ds), ctx(), 3000)
    assert len(plans) == 1 and not too_big
    assert all(estimate_tokens(p.prompt) <= 3000 for p in plans)
    assert JUDGE_PROMPT in plans[0].prompt
    # a tight cap forces several calls, each still under it, covering every group once
    cap = 700
    plans, too_big = plan_calls(group_differences(ds), ctx(), cap)
    assert len(plans) > 1 and not too_big
    assert all(estimate_tokens(p.prompt) <= cap for p in plans)
    assert sorted(g for p in plans for g in p.group_ids) == list(range(1, 13))


def test_estimate_is_ceil_chars_over_3_2():
    assert estimate_tokens("a" * 32) == 10
    assert estimate_tokens("a" * 33) == 11
    assert estimate_tokens("") == 0


def test_oversize_group_hunks_trimmed_then_ai_budget():
    junk = "".join(
        f"@@ -{i},1 +{i},1 @@\n-x{i} = {'q' * 80}\n+y{i} = {'q' * 80}\n" for i in range(60)
    )
    big = HUNKS + junk
    g = group_differences([diff()])
    plans, too_big = plan_calls(g, ctx(hunks=big), 800)
    assert not too_big and plans[0].hunks_trimmed is True
    assert "x5 =" not in plans[0].prompt and '"done"' in plans[0].prompt
    assert "only hunks touching" in plans[0].prompt  # never trimmed silently
    # intent so long that nothing fits -> ai_budget
    plans, too_big = plan_calls(g, ctx(intent="word " * 2000), 800)
    assert plans == [] and too_big == {1: "ai_budget"}
    rec = lean([diff()], Stub(), ctx(intent="word " * 2000))
    assert rec.verdict["status"] == INCONCLUSIVE and rec.calls == []
    assert rec.groups[0]["judgment"]["cause"] == "ai_budget"


# --- golden-like scenario ---------------------------------------------------


def test_golden_regression_with_valid_evidence_fails():
    stub = Stub([answer("regression")])
    rec = lean([diff()], stub)
    assert rec.verdict["status"] == FAIL and rec.verdict["cause"] == "judged_regression"
    reg = rec.verdict["details"]["regressions"][0]
    assert reg["expected_value"] is False and reg["quote"] == "id 1 (done=false)"
    assert rec.total_tokens == 600 and len(stub.prompts) == 1
    assert "[1] GET /notes/{id}" in stub.prompts[0]


def test_self_justifying_quote_is_rejected():
    hunks = HUNKS + "+    # return the note's done flag\n"
    stub = Stub(
        [answer("correct", source="intent", quote="return the note's done flag", expected=None)]
    )
    rec = lean([diff()], stub, ctx(hunks=hunks))
    j = rec.groups[0]["judgment"]
    assert j["cause"] == "evidence_rejected:self_justifying"
    assert rec.verdict["status"] == INCONCLUSIVE and rec.verdict["cause"] == "needs_information"
    # same answer with a clean diff is accepted
    ok = lean(
        [diff()],
        Stub(
            [answer("correct", source="intent", quote="return the note's done flag", expected=None)]
        ),
    )
    assert ok.verdict["status"] == PASS


def test_correct_quoting_the_changed_line_itself_is_rejected():
    stub = Stub([answer("correct", source="facts", quote='"done": n.done', expected=None)])
    rec = lean([diff()], stub)
    assert rec.groups[0]["judgment"]["cause"] == "evidence_rejected:quote_not_in_source"
    assert rec.verdict["status"] == INCONCLUSIVE


def test_ambiguous_boolean_quote_rejected():
    both = FACTS[0]
    stub = Stub([answer("regression", quote=both)])
    rec = lean([diff()], stub)
    assert rec.groups[0]["judgment"]["cause"] == "evidence_rejected:ambiguous_boolean_quote"
    assert rec.verdict["status"] == INCONCLUSIVE


@pytest.mark.parametrize(
    ("ans", "reason"),
    [
        (answer("regression", expected=True), "expected_equals_after"),
        (answer("regression", expected=None), "missing_expected_value"),
        (
            answer("regression", quote="seeduser/seedpass owns two notes", expected=False),
            "quote_lacks_expected_value",
        ),
        (answer("regression", source="schema"), "quote_not_in_source"),
        (answer("regression", source="bogus"), "bad_source"),
        (answer("regression", quote="  "), "empty_quote"),
        ({"group": 1, "verdict": "regression", "expected_value": False}, "no_evidence"),
    ],
)
def test_regression_evidence_rules(ans, reason):
    rec = lean([diff()], Stub([ans]))
    assert rec.groups[0]["judgment"]["cause"] == f"evidence_rejected:{reason}"
    assert rec.verdict["status"] == INCONCLUSIVE


def test_quote_whitespace_is_normalized_and_bool_case_insensitive():
    facts = ["Note 1 is  Done=False\nat seed"]
    rec = lean(
        [diff()],
        Stub([answer("regression", quote="Note 1 is Done=False at seed")]),
        ctx(facts=facts),
    )
    assert rec.verdict["status"] == FAIL


def test_correct_rules_intent_needs_field_name():
    c = ctx(intent="make notes better")
    rec = lean(
        [diff()],
        Stub([answer("correct", source="intent", quote="make notes better", expected=None)]),
        c,
    )
    assert rec.groups[0]["judgment"]["cause"] == "evidence_rejected:quote_lacks_after_or_field"
    c2 = ctx(intent="the flag is true when ready")
    rec = lean(
        [diff()],
        Stub(
            [answer("correct", source="intent", quote="the flag is true when ready", expected=None)]
        ),
        c2,
    )
    assert rec.groups[0]["judgment"]["cause"] == "evidence_rejected:intent_lacks_field_name"


def test_model_unclear_carries_question():
    rec = lean([diff()], Stub([answer("unclear", question="Should done change?", expected=None)]))
    assert rec.verdict["status"] == INCONCLUSIVE and rec.verdict["cause"] == "needs_information"
    assert rec.verdict["details"]["questions"][0]["question"] == "Should done change?"


def test_bad_output_shapes_become_unclear():
    for raw, cause in [
        ("not json at all", "unparsed_answer"),
        ("[]", "no_answer"),
        (json.dumps([{"group": 1, "verdict": "great"}]), "bad_verdict"),
    ]:
        rec = lean([diff()], Stub(raw))
        assert rec.groups[0]["judgment"]["cause"] == cause
        assert rec.verdict["status"] == INCONCLUSIVE
    fenced = "```json\n" + json.dumps([answer("regression")]) + "\n```"
    assert lean([diff()], Stub(fenced)).verdict["status"] == FAIL


# --- clean scenario ---------------------------------------------------------


def test_clean_added_field_passes():
    d = diff(fp="tag", before=None, after="work", kind="added_field")
    c = ctx(intent="add a tag field to note responses", hunks='+    "tag": n.tag\n')
    stub = Stub(
        [
            answer(
                "correct", source="intent", quote="add a tag field to note responses", expected=None
            )
        ]
    )
    rec = lean([d], stub, c)
    assert rec.verdict["status"] == PASS
    assert rec.groups[0]["judgment"]["check"]["ok"] is True


def test_no_differences_exercised_passes_without_a_call():
    stub = Stub()
    rec = lean([], stub)
    assert rec.verdict["status"] == PASS and stub.prompts == []


# --- verdict order ----------------------------------------------------------


def test_hard_failure_fails_with_zero_model_calls():
    stub = Stub()
    rec = lean([diff()], stub, hard=[HardFailure("r9", "5xx", "500 on GET /notes/9")])
    assert rec.verdict["status"] == FAIL and rec.verdict["cause"] == "hard_oracle"
    assert stub.prompts == [] and rec.calls == [] and rec.total_tokens == 0
    assert replay(rec).status == FAIL


def test_unexercised_lines_never_pass():
    ok = [answer("correct", source="facts", quote="id 1 (done=false)", expected=None)]
    ok[0]["evidence"]["quote"] = "id 2 (done=true)"
    rec = lean([diff()], Stub(ok), unexercised=["app.py:12", "app.py:13"])
    assert rec.verdict["status"] == INCONCLUSIVE and rec.verdict["cause"] == "change_not_exercised"
    assert rec.verdict["details"]["unexercised_lines"] == ["app.py:12", "app.py:13"]
    assert lean([], Stub(), unexercised=["a.py:1"]).verdict["status"] == INCONCLUSIVE


def test_regression_beats_unexercised_and_hard_beats_regression():
    rec = lean([diff()], Stub([answer("regression")]), unexercised=["a.py:1"])
    assert rec.verdict["cause"] == "judged_regression"
    g = group_differences([diff()])
    assert decide([HardFailure("r", "crash", "x")], [], ["a:1"], False).cause == "hard_oracle"
    assert g  # grouping used above


def test_not_observable():
    rec = lean([], Stub(), observable=False)
    assert (
        rec.verdict["status"] == INCONCLUSIVE and rec.verdict["cause"] == "not_observable_over_http"
    )


# --- budget and transport ---------------------------------------------------


def test_token_budget_exhaustion_marks_remaining_ai_budget():
    ds = [diff(f"r{i}", f"/notes/{i}", fp=f"f{i}") for i in range(40)]
    n_calls = len(plan_calls(group_differences(ds), ctx(), 600)[0])
    assert n_calls >= 4
    # each call "uses" 6000 tokens: the second pushes the total past 10_000
    stub = Stub(*(["[]"] * n_calls), usage={"total_tokens": 6000})
    rec = lean(ds, stub, prompt_cap=600, token_budget=10_000)
    statuses = [c["status"] for c in rec.calls]
    assert statuses[:2] == ["ok", "ok"] and set(statuses[2:]) == {"skipped_budget"}
    assert len(stub.prompts) == 2 and rec.total_tokens == 12_000
    causes = [g["judgment"]["cause"] for g in rec.groups]
    assert "ai_budget" in causes and causes[0] == "no_answer"
    assert rec.verdict["status"] == INCONCLUSIVE
    assert replay(rec).status == INCONCLUSIVE


def test_budget_checks_prompt_estimate_before_calling():
    stub = Stub()
    rec = lean([diff()], stub, token_budget=10)
    assert stub.prompts == [] and rec.groups[0]["judgment"]["cause"] == "ai_budget"


def test_transport_error_retries_once_then_inconclusive_environment():
    stub = Stub(
        RuntimeError("model API call failed: boom"), RuntimeError("model API call failed: boom")
    )
    rec = lean([diff()], stub)
    assert len(stub.prompts) == 2
    assert rec.verdict["status"] == INCONCLUSIVE and rec.verdict["cause"] == "environment"
    assert rec.groups[0]["judgment"]["cause"] == "environment:model_transport"
    assert rec.calls[0]["attempts"][0].startswith("attempt_1: model_transport")
    assert replay(rec).cause == "environment"


def test_transport_error_then_success():
    stub = Stub(RuntimeError("model API call failed: boom"), [answer("regression")])
    rec = lean([diff()], stub)
    assert len(stub.prompts) == 2 and rec.verdict["status"] == FAIL


def test_overflow_is_not_retried():
    stub = Stub(ModelCallError("too long", cause="model_overflow"))
    rec = lean([diff()], stub)
    assert (
        len(stub.prompts) == 1
        and rec.groups[0]["judgment"]["cause"] == "environment:model_overflow"
    )


def test_usage_missing_falls_back_to_estimate():
    stub = Stub([answer("regression")], usage={})
    rec = lean([diff()], stub)
    assert rec.calls[0]["tokens_estimated"] is True and rec.total_tokens > 0


# --- record and replay ------------------------------------------------------


def test_record_is_json_and_replay_matches():
    rec = lean([diff(), diff("r2", "/notes/2")], Stub([answer("regression")]))
    blob = json.loads(json.dumps(rec.to_dict()))
    assert len(rec.prompts_sha256) == 1 and len(rec.prompts_sha256[0]) == 64
    assert rec.inputs["context"]["intent"] == INTENT
    assert rec.calls[0]["raw_content"] and rec.calls[0]["usage"]["total_tokens"] == 600
    v = replay(blob)
    assert v.to_dict() == rec.verdict


def test_replay_detects_tampering():
    rec = lean([diff()], Stub([answer("regression")]))
    bad = copy.deepcopy(rec.to_dict())
    bad["calls"][0]["raw_content"] = json.dumps([answer("correct", quote="id 1 (done=false)")])
    with pytest.raises(ReplayMismatch):
        replay(bad)
    bad2 = copy.deepcopy(rec.to_dict())
    bad2["inputs"]["context"]["intent"] = "something else"
    with pytest.raises(ReplayMismatch):
        replay(bad2)


def test_regression_quote_naming_field_without_value_is_rejected():
    c = JudgeContext(INTENT, HUNKS, FACTS, {"done": "done: whether the note is done"})
    ans = answer("regression", source="schema", quote="done: whether the note is done")
    rec = lean([diff()], Stub([ans]), c)
    assert rec.groups[0]["judgment"]["cause"] == "evidence_rejected:quote_lacks_expected_value"
    assert rec.verdict["status"] == INCONCLUSIVE and rec.verdict["cause"] == "needs_information"


def test_removed_field_regression_may_quote_field_name_only():
    c = JudgeContext(INTENT, HUNKS, FACTS, {"done": "done: whether the note is done"})
    d = diff(before=False, after=None, kind="removed_field")
    ans = answer("regression", source="schema", quote="done: whether the note is done")
    rec = lean([d], Stub([ans]), c)
    assert rec.verdict["status"] == FAIL and rec.verdict["cause"] == "judged_regression"


# --- WO-R2-J (a): evidence quotes survive list markers (D261, D266 C1a) ------

FACT = "seed user seeduser/seedpass owns note id 1 (done=false) after seed"


@pytest.fixture
def _fresh_pacer():
    from proofdeploy.lean import judge as J

    J.reset_pacer()
    yield J
    J.reset_pacer()


@pytest.mark.parametrize("prefix", ["- ", "1. ", "12) ", "* ", "• ", "  - ", "-   "])
def test_list_marker_prefix_passes_verbatim_step(prefix):
    # the exact round-1 golden-bug shape: the prompt shows "- <fact>", the model copies it
    c = ctx(facts=[FACT])
    rec = lean([diff()], Stub([answer("regression", quote=prefix + FACT)]), c)
    j = rec.groups[0]["judgment"]
    assert "verbatim_in_source" in j["check"]["passed"], j
    assert rec.verdict["status"] == FAIL and rec.verdict["cause"] == "judged_regression"


def test_multiline_quote_markers_stripped_per_line():
    c = JudgeContext(INTENT, HUNKS, ["alpha beta one. gamma delta two"], {})
    quote = "- alpha beta one.\n- gamma delta two"
    ans = answer("correct", quote=quote, expected=None)
    d = diff(before="x", after="one")
    rec = lean([d], Stub([ans]), c)
    assert "verbatim_in_source" in rec.groups[0]["judgment"]["check"]["passed"]


def test_marker_only_quote_is_empty_quote():
    rec = lean([diff()], Stub([answer("regression", quote="- ")]))
    assert rec.groups[0]["judgment"]["cause"] == "evidence_rejected:empty_quote"


def test_changed_word_after_marker_still_not_in_source():
    q = "- " + FACT.replace("owns", "holds")
    rec = lean([diff()], Stub([answer("regression", quote=q)]), ctx(facts=[FACT]))
    assert rec.groups[0]["judgment"]["cause"] == "evidence_rejected:quote_not_in_source"


def test_only_one_marker_is_stripped():
    rec = lean([diff()], Stub([answer("regression", quote="- - " + FACT)]), ctx(facts=[FACT]))
    assert rec.groups[0]["judgment"]["cause"] == "evidence_rejected:quote_not_in_source"


# --- WO-R2-J (b): declared refactor decided by code (D266 C1b) --------------


def test_declares_no_behaviour_change_phrases():
    from proofdeploy.lean.judge import declares_no_behaviour_change as dnb

    for phrase in (
        "no behaviour change",
        "No behavior change",
        "no functional change",
        "no behaviour changes",
        "no behavior changes",
        "No  functional   changes",
    ):
        assert dnb(f"tidy. {phrase} intended") is not None, phrase
    assert dnb("refactor: tidy note handling") is None
    assert dnb("Refactor the code. Behaviour is unchanged.") is None
    assert dnb("") is None


def test_declared_no_change_fails_with_base_value_and_no_calls():
    stub = Stub()
    rec = lean([diff(before=False, after=True)], stub, ctx("no behaviour change intended"))
    assert stub.prompts == [] and rec.calls == [] and rec.prompts_sha256 == []
    assert rec.total_tokens == 0
    assert rec.verdict["status"] == FAIL and rec.verdict["cause"] == "judged_regression"
    reg = rec.verdict["details"]["regressions"][0]
    assert reg["expected_value"] is False and reg["source"] == "intent"
    assert reg["quote"] == "no behaviour change intended"
    assert reg["rule"] == "declared_no_behaviour_change"
    j = rec.groups[0]["judgment"]
    assert j["model_verdict"] is None and j["check"]["passed"] == ["declared_no_behaviour_change"]
    assert replay(rec).to_dict() == rec.verdict


def test_declared_no_change_covers_every_group_and_added_field():
    stub = Stub()
    diffs = [diff(), diff("r2", "/notes/2", "extra", None, 5, "added_field")]
    rec = lean(diffs, stub, ctx("Tidy. No functional changes."))
    assert stub.prompts == []
    assert [g["judgment"]["verdict"] for g in rec.groups] == ["regression", "regression"]
    added = rec.groups[1]["judgment"]
    assert added["expected_value"] is None and added["check"]["expected_absent"] is True
    assert rec.groups[0]["judgment"]["check"].get("expected_absent") is None
    assert replay(json.loads(json.dumps(rec.to_dict()))).to_dict() == rec.verdict


def test_declared_sentence_is_the_matching_one_in_multi_sentence_intent():
    intent = "Move helpers into utils.\nNo functional changes. Also renames a variable."
    rec = lean([diff()], Stub(), ctx(intent))
    assert rec.groups[0]["judgment"]["quote"] == "No functional changes."
    assert replay(rec).cause == "judged_regression"


def test_refactor_word_alone_uses_the_model_path():
    stub = Stub([answer("regression")])
    rec = lean([diff()], stub, ctx("refactor: tidy note handling"))
    assert len(stub.prompts) == 1
    assert rec.groups[0]["judgment"]["model_verdict"] == "regression"
    assert replay(rec).to_dict() == rec.verdict


def test_declared_replay_detects_tampering():
    rec = lean([diff()], Stub(), ctx("no behaviour change intended"))
    bad = copy.deepcopy(rec.to_dict())
    bad["inputs"]["context"]["intent"] = "refactor: tidy note handling"
    with pytest.raises(ReplayMismatch):
        replay(bad)


def test_model_judged_regression_detail_has_rule_none():
    rec = lean([diff()], Stub([answer("regression")]))
    assert rec.verdict["details"]["regressions"][0]["rule"] is None


# --- WO-R2-J (c): pacing and HTTP 429 backoff (D266 C2) ---------------------


class FakeTime:
    def __init__(self, J, monkeypatch):
        self.now = 1000.0
        self.sleeps: list[float] = []
        monkeypatch.setattr(J, "_clock", lambda: self.now)
        monkeypatch.setattr(J, "_sleep", self.sleep)

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.now += s


E429 = "model API call failed: HTTP 429: too many requests"


def test_pacer_second_call_waits_for_the_interval(monkeypatch, _fresh_pacer):
    J = _fresh_pacer
    monkeypatch.setenv("PROOFDEPLOY_MIN_CALL_INTERVAL", "13")
    ft = FakeTime(J, monkeypatch)
    stub = Stub([answer("regression")], [answer("regression")])
    first = J.execute_call("p", 0, stub)
    assert ft.sleeps == [] and first.waited_seconds == 0.0
    ft.now += 1.0
    second = J.execute_call("p", 1, stub)
    assert ft.sleeps == [12.0] and second.waited_seconds == 12.0 and second.status == "ok"


def test_pacer_no_wait_when_interval_already_elapsed(monkeypatch, _fresh_pacer):
    J = _fresh_pacer
    monkeypatch.setenv("PROOFDEPLOY_MIN_CALL_INTERVAL", "13")
    ft = FakeTime(J, monkeypatch)
    stub = Stub([answer("regression")], [answer("regression")])
    J.execute_call("p", 0, stub)
    ft.now += 20.0
    J.execute_call("p", 1, stub)
    assert ft.sleeps == []


def test_429_then_success_backs_off_20s_once(monkeypatch, _fresh_pacer):
    J = _fresh_pacer
    monkeypatch.setenv("PROOFDEPLOY_MIN_CALL_INTERVAL", "13")
    ft = FakeTime(J, monkeypatch)
    stub = Stub(RuntimeError(E429), [answer("regression")])
    r = J.execute_call("p", 0, stub)
    assert r.status == "ok" and ft.sleeps == [20.0] and r.waited_seconds == 20.0
    assert r.attempts[0].startswith("attempt_1: model_transport")
    assert r.attempts[1] == "attempt_2: waited 20.0s after 429"
    assert r.attempts[2] == "attempt_2: success"


def test_pacing_applies_on_top_of_429_backoff(monkeypatch, _fresh_pacer):
    J = _fresh_pacer
    monkeypatch.setenv("PROOFDEPLOY_MIN_CALL_INTERVAL", "30")
    ft = FakeTime(J, monkeypatch)
    stub = Stub(RuntimeError(E429), [answer("regression")])
    J.execute_call("p", 0, stub)
    # backoff of 20 s, then 10 s more pacing to reach 30 s since the failed call started
    assert ft.sleeps == [20.0, 10.0]


def test_four_429s_end_as_provider_limit_inconclusive(monkeypatch, _fresh_pacer):
    J = _fresh_pacer
    ft = FakeTime(J, monkeypatch)
    stub = Stub(*[RuntimeError(E429) for _ in range(4)])
    rec = lean([diff()], stub)
    assert len(stub.prompts) == 4
    assert ft.sleeps == [20.0, 40.0, 60.0]
    call = rec.calls[0]
    assert call["status"] == "error" and call["error_cause"] == "provider_limit"
    assert call["waited_seconds"] == 120.0
    assert rec.groups[0]["judgment"]["cause"] == "environment:provider_limit"
    assert rec.verdict["status"] == INCONCLUSIVE and rec.verdict["cause"] == "provider_limit"
    assert replay(json.loads(json.dumps(rec.to_dict()))).to_dict() == rec.verdict


def test_429_retries_env_override(monkeypatch, _fresh_pacer):
    J = _fresh_pacer
    ft = FakeTime(J, monkeypatch)
    monkeypatch.setenv("PROOFDEPLOY_429_RETRIES", "1")
    stub = Stub(RuntimeError(E429), RuntimeError(E429))
    r = J.execute_call("p", 0, stub)
    assert r.error_cause == "provider_limit" and ft.sleeps == [20.0] and len(stub.prompts) == 2


def test_non_429_transport_error_keeps_single_retry(monkeypatch, _fresh_pacer):
    J = _fresh_pacer
    ft = FakeTime(J, monkeypatch)
    stub = Stub(RuntimeError("model API call failed: HTTP 500: boom"), RuntimeError("x"))
    r = J.execute_call("p", 0, stub)
    assert len(stub.prompts) == 2 and r.error_cause == "model_transport"
    assert ft.sleeps == [] and r.waited_seconds == 0.0
    assert not any("waited" in a for a in r.attempts)


def test_provider_limit_beats_other_environment_causes():
    from proofdeploy.lean.verdict import decide as decide_

    def jg(i: int, cause: str):
        from proofdeploy.lean.judge import JudgedGroup, Judgment

        g = group_differences([diff(rid=f"r{i}", path=f"/n{i}")])[0]
        return JudgedGroup(g, Judgment(group=1, verdict="unclear", cause=cause))

    v = decide_(
        [], [jg(1, "environment:model_transport"), jg(2, "environment:provider_limit")], [], True
    )
    assert v.status == INCONCLUSIVE and v.cause == "provider_limit"
    v2 = decide_([], [jg(1, "environment:model_transport")], [], True)
    assert v2.cause == "environment"


def test_env_override_parsed_and_invalid_names_variable(monkeypatch):
    from proofdeploy.lean import judge as J

    assert J.min_call_interval() == 0.0  # conftest sets 0 for the suite
    monkeypatch.setenv("PROOFDEPLOY_MIN_CALL_INTERVAL", "2.5")
    assert J.min_call_interval() == 2.5
    monkeypatch.delenv("PROOFDEPLOY_MIN_CALL_INTERVAL")
    assert J.min_call_interval() == 13.0
    for bad in ("abc", "-1", "nan"):
        monkeypatch.setenv("PROOFDEPLOY_MIN_CALL_INTERVAL", bad)
        with pytest.raises(ValueError, match="PROOFDEPLOY_MIN_CALL_INTERVAL"):
            J.min_call_interval()
    monkeypatch.setenv("PROOFDEPLOY_429_RETRIES", "x")
    with pytest.raises(ValueError, match="PROOFDEPLOY_429_RETRIES"):
        J.max_429_retries()


def test_old_call_records_without_waited_seconds_still_load():
    rec = lean([diff()], Stub([answer("regression")]))
    old = copy.deepcopy(rec.to_dict())
    for c in old["calls"]:
        del c["waited_seconds"]
    assert replay(old).to_dict() == rec.verdict
