"""The verdict: code decides, the AI only judged.

``decide`` is a pure function of the hard failures, the checked judgments,
the unexercised changed lines and whether the change is observable over
HTTP. ``run_lean`` runs the judge and records everything in a
``LeanRecord``; ``replay`` re-derives the verdict from the record's stored
raw model outputs without calling a model, and must match.

Order of the rules (first match wins):

1. any hard failure                      -> FAIL  hard_oracle (no AI used)
2. any judged regression (evidence ok)   -> FAIL  judged_regression
3. a hard failure in only one head run  -> INCONCLUSIVE flaky_hard_failure
   (base clean in both runs): a crash that
   appears only after the change, even
   intermittently, is never a PASS
4. observable is False                   -> INCONCLUSIVE not_observable_over_http
5. unexercised changed lines             -> INCONCLUSIVE change_not_exercised
6. any unclear group                     -> INCONCLUSIVE needs_information
                                            (cause ``environment`` when a model
                                            call failed for a transport reason;
                                            ``provider_limit`` when the provider
                                            kept answering HTTP 429)
7. otherwise                             -> PASS
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from proofdeploy.lean.judge import (
    DEFAULT_PROMPT_CAP,
    DEFAULT_TOKEN_BUDGET,
    CallResult,
    Difference,
    HardFailure,
    JudgeContext,
    JudgedGroup,
    Judgment,
    group_differences,
    judge_from_calls,
    plan_calls,
    run_judge,
)
from proofdeploy.model_client import ModelRunner

PASS = "PASS"
FAIL = "FAIL"
INCONCLUSIVE = "INCONCLUSIVE"


@dataclass
class Verdict:
    status: str
    cause: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def decide(
    hard_failures: list[HardFailure],
    groups_with_judgments: list[JudgedGroup],
    unexercised_lines: list[str],
    observable: bool,
    flaky_hard_failures: list[HardFailure] | None = None,
) -> Verdict:
    if hard_failures:
        return Verdict(FAIL, "hard_oracle", {"hard_failures": [h.to_dict() for h in hard_failures]})

    regressions = [
        jg
        for jg in groups_with_judgments
        if jg.judgment.verdict == "regression" and jg.judgment.check.get("ok")
    ]
    if regressions:
        return Verdict(
            FAIL,
            "judged_regression",
            {
                "regressions": [
                    {
                        "group": jg.group.index,
                        "method": jg.group.method,
                        "path": jg.group.path_template,
                        "field_path": jg.group.field_path,
                        "after": jg.group.after,
                        "expected_value": jg.judgment.expected_value,
                        "source": jg.judgment.evidence_source,
                        "quote": jg.judgment.quote,
                        "rule": jg.judgment.check.get("rule"),
                        "request_ids": jg.group.request_ids,
                    }
                    for jg in regressions
                ]
            },
        )

    if flaky_hard_failures:
        return Verdict(
            INCONCLUSIVE,
            "flaky_hard_failure",
            {"flaky_hard_failures": [h.to_dict() for h in flaky_hard_failures]},
        )

    if not observable:
        return Verdict(INCONCLUSIVE, "not_observable_over_http", {})

    if unexercised_lines:
        return Verdict(
            INCONCLUSIVE, "change_not_exercised", {"unexercised_lines": list(unexercised_lines)}
        )

    unclear = [jg for jg in groups_with_judgments if jg.judgment.verdict != "correct"]
    if unclear:
        causes = [jg.judgment.cause or "" for jg in unclear]
        limit = any(c == "environment:provider_limit" for c in causes)
        env = any(c.startswith("environment") for c in causes)
        return Verdict(
            INCONCLUSIVE,
            "provider_limit" if limit else "environment" if env else "needs_information",
            {
                "questions": [
                    {
                        "group": jg.group.index,
                        "cause": jg.judgment.cause,
                        "question": jg.judgment.question,
                        "request_ids": jg.group.request_ids,
                    }
                    for jg in unclear
                ]
            },
        )

    return Verdict(PASS, "no_regression_found", {})


# --------------------------------------------------------------------------
# The record and replay
# --------------------------------------------------------------------------


@dataclass
class LeanRecord:
    inputs: dict[str, Any]
    prompts_sha256: list[str]
    calls: list[dict[str, Any]]  # raw model outputs, usage, attempts, status
    groups: list[dict[str, Any]]  # each group with its judgment and checks
    verdict: dict[str, Any]
    total_tokens: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> LeanRecord:
        return cls(**d)


class ReplayMismatch(Exception):
    """The record's stored outputs do not re-derive the stored verdict."""


def _inputs(
    differences: list[Difference],
    hard_failures: list[HardFailure],
    ctx: JudgeContext,
    unexercised_lines: list[str],
    observable: bool,
    token_budget: int,
    prompt_cap: int,
    templates: list[str],
    flaky_hard_failures: list[HardFailure] | None = None,
) -> dict[str, Any]:
    return {
        "differences": [d.to_dict() for d in differences],
        "hard_failures": [h.to_dict() for h in hard_failures],
        "flaky_hard_failures": [h.to_dict() for h in flaky_hard_failures or []],
        "context": ctx.to_dict(),
        "unexercised_lines": list(unexercised_lines),
        "observable": observable,
        "token_budget": token_budget,
        "prompt_cap": prompt_cap,
        "templates": list(templates),
    }


def _unjudged(groups_: list[Any]) -> list[JudgedGroup]:
    return [
        JudgedGroup(
            g,
            Judgment(
                group=g.index,
                verdict="unjudged",
                cause="hard_oracle_short_circuit",
                check={"ok": False, "reason": "not_judged", "passed": []},
            ),
        )
        for g in groups_
    ]


def run_lean(
    differences: list[Difference],
    hard_failures: list[HardFailure],
    ctx: JudgeContext,
    unexercised_lines: list[str],
    observable: bool,
    *,
    runner: ModelRunner | None = None,
    token_budget: int = DEFAULT_TOKEN_BUDGET,
    prompt_cap: int = DEFAULT_PROMPT_CAP,
    templates: list[str] | None = None,
    flaky_hard_failures: list[HardFailure] | None = None,
) -> LeanRecord:
    """Judge (unless a hard failure already decides) and decide."""
    tpls = list(templates or [])
    inputs = _inputs(
        differences,
        hard_failures,
        ctx,
        unexercised_lines,
        observable,
        token_budget,
        prompt_cap,
        tpls,
        flaky_hard_failures,
    )
    if hard_failures:
        # The hard oracle decides; no model call is made or needed.
        groups = group_differences(differences, tpls)
        judged = _unjudged(groups)
        plans: list[Any] = []
        results: list[CallResult] = []
    else:
        run = run_judge(
            differences,
            ctx,
            runner=runner,
            token_budget=token_budget,
            prompt_cap=prompt_cap,
            templates=tpls,
        )
        judged, plans, results = run.judged, run.plans, run.results
    verdict = decide(hard_failures, judged, unexercised_lines, observable, flaky_hard_failures)
    return LeanRecord(
        inputs=inputs,
        prompts_sha256=[p.prompt_sha256 for p in plans],
        calls=[r.to_dict() for r in results],
        groups=[jg.to_dict() for jg in judged],
        verdict=verdict.to_dict(),
        total_tokens=sum(r.tokens for r in results),
    )


def replay(record: LeanRecord | dict[str, Any]) -> Verdict:
    """Re-derive the verdict from the stored raw outputs, with no model.

    Rebuilds the groups and prompts from the stored inputs, checks the
    prompts' sha256 against the record, re-parses and re-checks every raw
    output, and decides again. Raises ``ReplayMismatch`` if anything
    differs from what was stored.
    """
    rec = record if isinstance(record, LeanRecord) else LeanRecord.from_dict(record)
    inp = rec.inputs
    differences = [Difference.from_dict(d) for d in inp["differences"]]
    hard_failures = [HardFailure.from_dict(h) for h in inp["hard_failures"]]
    ctx = JudgeContext.from_dict(inp["context"])
    groups = group_differences(differences, inp.get("templates") or [])
    if hard_failures:
        judged = _unjudged(groups)
        plans: list[Any] = []
    else:
        plans, too_big = plan_calls(groups, ctx, inp["prompt_cap"])
        stored = [CallResult(**c) for c in rec.calls]
        judged = judge_from_calls(groups, ctx, plans, too_big, stored)
    got_hashes = [p.prompt_sha256 for p in plans]
    if got_hashes != rec.prompts_sha256:
        raise ReplayMismatch("prompts differ from the recorded sha256 values")
    flaky = [HardFailure.from_dict(h) for h in inp.get("flaky_hard_failures") or []]
    verdict = decide(hard_failures, judged, inp["unexercised_lines"], inp["observable"], flaky)
    if [jg.to_dict() for jg in judged] != rec.groups:
        raise ReplayMismatch("re-derived group judgments differ from the record")
    if verdict.to_dict() != rec.verdict:
        raise ReplayMismatch(f"re-derived verdict {verdict.to_dict()} != recorded {rec.verdict}")
    return verdict


__all__ = [
    "FAIL",
    "INCONCLUSIVE",
    "PASS",
    "LeanRecord",
    "ReplayMismatch",
    "Verdict",
    "decide",
    "replay",
    "run_lean",
]
