"""Lean round 4 (WO-R4-J, register R3 and R7): the request budget scales with the change,
and the judge sees only the code that ran. No real model call: stub runners only."""

from __future__ import annotations

import difflib
import json
import re
from pathlib import Path
from typing import Any

import pytest
from lean_helpers import FACT_OPEN, PromptStub, bug_rule

# the golden fixtures (the module-scoped golden repo, the CI env) come from the pipeline tests
from test_lean_pipeline import ci_env, golden, run_golden  # noqa: F401

from proofdeploy.cli import build_parser
from proofdeploy.lean import pipeline
from proofdeploy.lean import run as lean_run
from proofdeploy.lean.coverage import ChangedLines
from proofdeploy.lean.generate import executable_view, parse_diff_text
from proofdeploy.lean.judge import (
    NO_CHANGED_LINE_RAN,
    Difference,
    JudgeContext,
    estimate_tokens,
    group_differences,
    group_hunk_labels,
    parse_hunks,
    plan_calls,
)
from proofdeploy.lean.pipeline import (
    DEFAULT_REQUEST_BUDGET,
    LeanConfig,
    request_budget_for,
)
from proofdeploy.lean.verdict import FAIL, LeanRecord, ReplayMismatch, replay, run_lean
from proofdeploy.model_client import ModelResponse

FACTS = ["seed account acme owns two invoices after seed: id 1 (paid=false) and id 2 (paid=true)"]
INTENT = "round invoice line totals to two decimals"


# --------------------------------------------------------------------------
# Realistic data: a big multi-file diff built with difflib from generated modules
# --------------------------------------------------------------------------


def _module(n_funcs: int, with_totals: bool) -> str:
    out = ['"""Invoice helpers."""', "", "from decimal import Decimal", ""]
    for i in range(n_funcs):
        out += [
            "",
            f"def price_line_{i}(qty, unit):",
            f'    """Price line {i}."""',
            "    total = Decimal(qty) * Decimal(unit)",
            f"    if total > Decimal({1000 + i}):",
            "        total = total * Decimal('0.95')",
            "    return total",
            "",
        ]
        if with_totals and i % 3 == 0:
            out += [
                f"def total_for_{i}(lines):",
                "    result = Decimal(0)",
                "    for line in lines:",
                f"        result += price_line_{i}(line['qty'], line['unit'])",
                "    # rounded to two decimals",
                "    result = result.quantize(Decimal('0.01'))",
                "    return result",
                "",
            ]
    return "\n".join(out) + "\n"


def _unified(path: str, base: str, head: str, n: int = 3) -> str:
    body = list(
        difflib.unified_diff(
            base.splitlines(keepends=True),
            head.splitlines(keepends=True),
            f"a/{path}",
            f"b/{path}",
            n=n,
        )
    )
    return f"diff --git a/{path} b/{path}\n" + "".join(body)


def _big_diff(files: int = 12) -> tuple[str, dict[str, str]]:
    """A change of ~4,000 diff lines over ``files`` modules: every function gets a
    rounding block (a realistic sweeping edit). Returns the diff and the head sources."""
    out = []
    heads = {}
    for k in range(files):
        base = _module(30 + k, with_totals=True)
        head = base.replace(
            "    return total\n",
            "    total = total.quantize(Decimal('0.01'))\n"
            "    if total < 0:\n"
            "        total = Decimal(0)\n"
            "    log_total(total)\n"
            "    return total\n",
        )
        path = f"billing/lines_{k}.py"
        heads[path] = head
        out.append(_unified(path, base, head))
    return "".join(out), heads


def _added_line_no(diff: str, path: str, containing: str, nth: int = 0) -> int:
    """New-side line number of the nth added line containing ``containing`` in ``path``."""
    seen = 0
    for h in parse_hunks(diff):
        if h.path != path:
            continue
        for tag, raw, _, nl in h.entries:
            if tag == "+" and containing in raw:
                if seen == nth:
                    return nl
                seen += 1
    raise AssertionError("no such line")


def _diff(rid: str = "9f2c1ab07d3e:1", fp: str = "paid") -> Difference:
    return Difference(rid, "GET", "/invoices/1", None, fp, False, True, "changed")


def _ctx(diff: str, executed: dict[str, dict[str, list[str]]] | None) -> JudgeContext:
    return JudgeContext(INTENT, diff, FACTS, {}, executed=executed)


class _Answer:
    """A stub runner answering group 1 as the golden regression."""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def __call__(self, prompt: str) -> ModelResponse:
        self.prompts.append(prompt)
        a = [
            {
                "group": 1,
                "verdict": "regression",
                "expected_value": False,
                "evidence": {"source": "facts", "quote": "id 1 (paid=false)"},
            }
        ]
        usage = {"prompt_tokens": 500, "completion_tokens": 100, "total_tokens": 600}
        return ModelResponse(json.dumps(a), "stop", usage)


# --------------------------------------------------------------------------
# 1. The request budget scales with the change
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("lines", "expected", "source"),
    [
        (0, 200, "default"),
        (1, 200, "default"),
        (100, 200, "default"),
        (101, 402, "scaled"),
        (250, 700, "scaled"),
        (399, 998, "scaled"),
        (400, 1000, "scaled"),
        (401, 1000, "scaled"),
        (4000, 1000, "scaled"),
    ],
)
def test_request_budget_rule_boundaries(lines: int, expected: int, source: str) -> None:
    got = request_budget_for(None, lines)
    assert got["value"] == expected and got["source"] == source
    assert got["executable_changed_lines"] == lines and "min(1000, 200 + 2 * lines)" in got["rule"]


def test_an_explicit_request_budget_wins_over_the_rule() -> None:
    for lines in (10, 100, 101, 900):
        for explicit in (50, 200, 5000):
            got = request_budget_for(explicit, lines)
            assert got["value"] == explicit and got["source"] == "explicit"


def test_the_run_config_leaves_the_budget_to_the_rule_unless_given() -> None:
    assert lean_run.LeanRunConfig.__dataclass_fields__["budget_requests"].default is None
    assert LeanConfig(repo=Path("."), base="a", head="b").budget_requests is None
    assert DEFAULT_REQUEST_BUDGET == 200


def _run_unset_budget(g: dict[str, Any], out: Path) -> dict[str, Any]:
    """The golden bug run with the request budget left unset."""
    shas = g["shas"]
    args = build_parser().parse_args(
        [
            "lean-verify",
            "--repo",
            str(g["repo"]),
            "--base",
            shas["base"],
            "--head",
            shas["bug"],
            "--no-sandbox",
            "--python",
            str(g["py"]),
            "--output-dir",
            str(out),
        ]
    )
    args.budget_requests = None
    assert lean_run.cmd_lean_verify(args, model_runner=PromptStub(bug_rule)) == 1
    return json.loads((out / "manifest.json").read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def test_the_manifest_records_the_budget_and_its_rule(
    golden: dict[str, Any],  # noqa: F811
    ci_env: None,  # noqa: F811
    tmp_path: Path,
) -> None:
    m = _run_unset_budget(golden, tmp_path / "out")
    rule = m["inputs"]["budget_requests_rule"]
    assert rule["source"] == "default" and rule["value"] == m["inputs"]["budget_requests"] == 200
    assert rule["executable_changed_lines"] == m["executable_changed_lines"] <= 100
    assert "min(1000, 200 + 2 * lines)" in rule["rule"]
    # an explicit value is recorded as such and wins
    code, m = run_golden(
        golden, "base", "bug", tmp_path / "out2", PromptStub(bug_rule), "--budget-requests", "77"
    )
    assert code == 1
    assert m["inputs"]["budget_requests"] == 77
    assert m["inputs"]["budget_requests_rule"]["source"] == "explicit"


# --------------------------------------------------------------------------
# 2. The judge sees only the code that ran
# --------------------------------------------------------------------------


def test_the_big_diff_is_a_realistic_4000_line_change() -> None:
    diff, _ = _big_diff()
    assert len(diff.splitlines()) >= 4000
    assert len({h.path for h in parse_hunks(diff)}) == 12


def test_round3_shape_one_difference_two_executed_lines_is_one_judged_call_under_3k_tokens() -> (
    None
):
    diff, _ = _big_diff()
    path = "billing/lines_4.py"
    rid = "9f2c1ab07d3e:1"
    ran = [
        f"{path}:{_added_line_no(diff, path, 'quantize', 3)}",
        f"{path}:{_added_line_no(diff, path, 'log_total', 3)}",
    ]
    c = _ctx(diff, {rid: {"hits_added": ran, "hits_removed": []}})
    plans, too_big = plan_calls(group_differences([_diff(rid, "total")]), c)
    assert len(plans) == 1 and not too_big
    assert estimate_tokens(plans[0].prompt) <= 3000
    assert "log_total(total)" in plans[0].prompt and path in plans[0].prompt
    assert (
        "billing/lines_5.py" not in plans[0].prompt and "billing/lines_0.py" not in plans[0].prompt
    )
    # the whole diff does not fit: without the filter the group is ai_budget
    plans0, too_big0 = plan_calls(group_differences([_diff(rid, "total")]), _ctx(diff, None))
    assert too_big0 == {1: "ai_budget"} and plans0 == []
    # end to end with a stub: one call, the group judged
    stub = _Answer()
    rec = run_lean([_diff(rid, "total")], [], c, [], True, runner=stub)
    assert len(stub.prompts) == 1 and estimate_tokens(stub.prompts[0]) <= 3000
    assert rec.groups[0]["judgment"]["cause"] != "ai_budget"
    assert rec.verdict["status"] == FAIL and rec.verdict["cause"] == "judged_regression"


def test_only_hunks_with_an_executed_changed_line_are_shown_and_the_note_says_so() -> None:
    diff, _ = _big_diff(2)
    path = "billing/lines_1.py"
    n = _added_line_no(diff, path, "log_total", 2)
    c = _ctx(diff, {"r:1": {"hits_added": [f"{path}:{n}"], "hits_removed": []}})
    [plan], _ = plan_calls(group_differences([_diff("r:1")]), c)
    labels = group_hunk_labels(c, _g(c))
    assert len(labels) == 1 and labels[0].startswith(path)
    assert "only the changed lines that ran in the request are shown" in plan.prompt
    assert plan.prompt.count("@@ -") == 1


def _g(c: JudgeContext, rid: str = "r:1") -> Any:
    return group_differences([_diff(rid)])[0]


def test_no_executed_line_shows_the_note_and_no_code() -> None:
    diff, _ = _big_diff(2)
    for executed in ({}, {"r:1": {"hits_added": [], "hits_removed": []}}, {"zz:9": {}}):
        c = _ctx(diff, executed)  # type: ignore[arg-type]
        [plan], _ = plan_calls(group_differences([_diff("r:1")]), c)
        assert NO_CHANGED_LINE_RAN in plan.prompt
        assert (
            "billing/lines_0.py" not in plan.prompt
            and "quantize" not in plan.prompt.split("DIFFERENCES:")[0].split("CHANGED CODE")[1]
        )
        assert group_hunk_labels(c, _g(c)) == []


def test_a_removed_line_selects_its_hunk_from_the_base_side_hits() -> None:
    base = _module(6, with_totals=True)
    head = base.replace("        total = total * Decimal('0.95')\n", "", 1)
    diff = _unified("billing/lines.py", base, head)
    [h] = parse_hunks(diff)
    gone = next(ol for tag, raw, ol, _ in h.entries if tag == "-")
    c = _ctx(diff, {"r:1": {"hits_added": [], "hits_removed": [f"billing/lines.py:{gone}"]}})
    [plan], _ = plan_calls(group_differences([_diff("r:1")]), c)
    assert "-        total = total * Decimal('0.95')" in plan.prompt
    # the same number on the added side selects nothing
    c2 = _ctx(diff, {"r:1": {"hits_added": [f"billing/lines.py:{gone}"], "hits_removed": []}})
    [plan2], _ = plan_calls(group_differences([_diff("r:1")]), c2)
    assert NO_CHANGED_LINE_RAN in plan2.prompt


def test_each_shown_hunk_is_trimmed_to_three_context_lines_with_a_matching_header() -> None:
    base = _module(8, with_totals=False)
    needle = "    total = Decimal(qty) * Decimal(unit)\n"
    head = base.replace(needle, needle + "    total = clamp_to_credit_limit(total)\n", 3)
    diff = _unified("billing/lines.py", base, head, n=8)  # git -U8: wide context
    hunks = parse_hunks(diff)
    first = hunks[0]
    ln = next(nl for tag, _, _, nl in first.entries if tag == "+")
    c = _ctx(diff, {"r:1": {"hits_added": [f"billing/lines.py:{ln}"], "hits_removed": []}})
    [plan], _ = plan_calls(group_differences([_diff("r:1")]), c)
    block = plan.prompt.split("CHANGED CODE")[1].split("DIFFERENCES:")[0]
    lines = block.splitlines()
    at = next(i for i, x in enumerate(lines) if x.startswith("@@ "))
    body = lines[at + 1 :]
    while body and body[-1] == "":
        body.pop()  # the blank line that separates the sections
    adds = [i for i, x in enumerate(body) if x.startswith("+")]
    assert adds, block
    context_before = adds[0]
    assert context_before == 3 and len(body) - 1 - adds[-1] == 3, block
    hdr = next(x for x in block.splitlines() if x.startswith("@@ "))
    m = re.match(r"@@ -(\d+),(\d+) \+(\d+),(\d+) @@", hdr)
    assert m is not None, hdr
    assert int(m.group(2)) == sum(1 for x in body if x[:1] in {" ", "-"})
    assert int(m.group(4)) == sum(1 for x in body if x[:1] in {" ", "+"})
    # the trimmed hunk starts where its first shown line really is
    assert int(m.group(3)) == first.new_start + (
        first.entries.index(next(e for e in first.entries if e[0] == "+")) - context_before
    )


def test_hunk_parsing_follows_the_counts_not_the_prefixes() -> None:
    sql = (
        "diff --git a/db/queries.py b/db/queries.py\n"
        "--- a/db/queries.py\n"
        "+++ b/db/queries.py\n"
        "@@ -10,4 +10,4 @@ def due\n"
        " def due(cur):\n"
        "--- skip cancelled invoices\n"  # a removed line whose text is "-- skip ..."
        "+-- skip void invoices\n"  # an added line whose text is "- skip ..."
        "     return cur\n"
    )
    [h] = parse_hunks(sql)
    assert h.path == "db/queries.py"
    assert [(t, o, n) for t, _, o, n in h.entries] == [
        (" ", 10, 10),
        ("-", 11, 11),
        ("+", 12, 11),
        (" ", 12, 12),
    ]
    added, removed = h.changed_keys()
    assert added == {"db/queries.py:11"} and removed == {"db/queries.py:11"}


def test_a_batch_labels_blocks_when_groups_ran_different_code() -> None:
    diff, _ = _big_diff(3)
    p1, p2 = "billing/lines_0.py", "billing/lines_2.py"
    executed = {
        "a:1": {
            "hits_added": [f"{p1}:{_added_line_no(diff, p1, 'log_total', 1)}"],
            "hits_removed": [],
        },
        "b:1": {
            "hits_added": [f"{p2}:{_added_line_no(diff, p2, 'log_total', 1)}"],
            "hits_removed": [],
        },
        "c:1": {"hits_added": [], "hits_removed": []},
    }
    ds = [
        _diff("a:1", "paid"),
        _diff("b:1", "due"),
        _diff("c:1", "total"),
    ]
    c = _ctx(diff, executed)
    plans, too_big = plan_calls(group_differences(ds), c)
    assert len(plans) == 1 and not too_big
    prompt = plans[0].prompt
    assert "for [1]:" in prompt and "for [2]:" in prompt and "for [3]:" in prompt
    assert NO_CHANGED_LINE_RAN in prompt
    assert p1 in prompt and p2 in prompt and "billing/lines_1.py" not in prompt


def test_groups_that_share_a_selection_share_one_block() -> None:
    diff, _ = _big_diff(2)
    p = "billing/lines_0.py"
    hit = {"hits_added": [f"{p}:{_added_line_no(diff, p, 'log_total', 0)}"], "hits_removed": []}
    c = _ctx(diff, {"a:1": hit, "b:1": hit})
    [plan], _ = plan_calls(group_differences([_diff("a:1", "paid"), _diff("b:1", "due")]), c)
    assert plan.prompt.count("@@ -") == 1 and "for [" not in plan.prompt


def test_the_group_is_judged_on_its_first_request_only() -> None:
    diff, _ = _big_diff(2)
    p = "billing/lines_1.py"
    hit = {"hits_added": [f"{p}:{_added_line_no(diff, p, 'log_total', 0)}"], "hits_removed": []}
    # two requests produce the identical difference; the first one ran nothing changed
    ds = [_diff("a:1"), _diff("b:1")]
    c = _ctx(diff, {"a:1": {"hits_added": [], "hits_removed": []}, "b:1": hit})
    [plan], _ = plan_calls(group_differences(ds), c)
    assert NO_CHANGED_LINE_RAN in plan.prompt and p not in plan.prompt


def test_replay_rebuilds_the_same_prompt_from_the_stored_selection() -> None:
    diff, _ = _big_diff()
    path = "billing/lines_3.py"
    rid = "9f2c1ab07d3e:1"
    hit = {
        "hits_added": [f"{path}:{_added_line_no(diff, path, 'log_total', 5)}"],
        "hits_removed": [],
    }
    c = _ctx(diff, {rid: hit})
    rec = run_lean([_diff(rid)], [], c, [], True, runner=_Answer())
    assert rec.inputs["context"]["executed"] == {rid: hit}
    stored = LeanRecord.from_dict(json.loads(json.dumps(rec.to_dict())))
    assert replay(stored).status == FAIL
    plans, _ = plan_calls(
        group_differences([_diff(rid)]), JudgeContext.from_dict(stored.inputs["context"])
    )
    assert [p.prompt_sha256 for p in plans] == stored.prompts_sha256
    # change the stored selection and the prompt hash no longer matches the record
    forged = json.loads(json.dumps(rec.to_dict()))
    other = f"billing/lines_3.py:{_added_line_no(diff, path, 'log_total', 20)}"
    forged["inputs"]["context"]["executed"][rid]["hits_added"] = [other]
    with pytest.raises(ReplayMismatch):
        replay(forged)


def test_a_record_from_before_round_4_still_replays_with_the_whole_diff() -> None:
    old = JudgeContext.from_dict({"intent": INTENT, "changed_hunks": "x", "seed_facts": FACTS})
    assert old.executed is None
    diff, _ = _big_diff(1)
    [plan], _ = plan_calls(group_differences([_diff()]), _ctx(diff, None))
    assert "only the changed lines that ran" not in plan.prompt


# --------------------------------------------------------------------------
# The pipeline feeds the judge from the coverage session
# --------------------------------------------------------------------------


class _Session:
    def __init__(self, cov: dict[str, dict[str, set[int]]]) -> None:
        self.cov = cov

    def snapshot(self, *, root_prefix: str = "") -> dict[str, dict[str, set[int]]]:
        return self.cov


def test_request_hits_come_per_request_from_the_side_that_ran() -> None:
    base = _module(4, with_totals=False)
    head = base.replace(
        "    return total\n", "    total = round_half_even(total)\n    return total\n", 2
    )
    diff = _unified("billing/lines.py", base, head)
    changed = ChangedLines.from_diff(diff)
    view = executable_view(changed, parse_diff_text(diff), "new")
    added = sorted(changed["billing/lines.py"]["added"])
    cov = {
        "": {"billing/lines.py": {added[0]}},  # load time: not a request
        "s1:1": {"billing/lines.py": {added[0], 3}},
        "s1:2": {"billing/lines.py": {3}},  # ran nothing changed
        "s2:1": {"billing/lines.py": {added[1]}},
    }
    got = pipeline._request_hits(_Session(cov), view, "new", "")  # type: ignore[arg-type]
    assert got == {
        "s1:1": {"hits_added": [f"billing/lines.py:{added[0]}"], "hits_removed": []},
        "s2:1": {"hits_added": [f"billing/lines.py:{added[1]}"], "hits_removed": []},
    }
    removed_view = executable_view(changed, parse_diff_text(diff), "old")
    assert pipeline._request_hits(_Session(cov), removed_view, "old", "") == {}  # type: ignore[arg-type]


def test_golden_bug_prompt_still_holds_the_done_hunk_and_replays(
    golden: dict[str, Any],  # noqa: F811
    ci_env: None,  # noqa: F811
    tmp_path: Path,
) -> None:
    stub = PromptStub(bug_rule)
    code, m = run_golden(golden, "base", "bug", tmp_path / "out", stub)
    assert code == 1 and m["verdict"]["cause"] == "judged_regression"
    assert stub.prompts and FACT_OPEN in stub.prompts[0]
    changed_block = stub.prompts[0].split("CHANGED CODE")[1].split("DIFFERENCES:")[0]
    assert "done" in changed_block and "@@ -" in changed_block, changed_block
    assert NO_CHANGED_LINE_RAN not in changed_block
    record = LeanRecord.from_dict(
        json.loads((tmp_path / "out" / "lean-record.json").read_text(encoding="utf-8"))
    )
    executed = record.inputs["context"]["executed"]
    assert executed and any(v["hits_added"] for v in executed.values())
    shown = m["judge"]["hunks_shown"]
    assert shown and all(labels for labels in shown.values()), shown
    assert replay(record).status == FAIL


def test_lean_verify_budget_requests_is_unset_by_default_and_explicit_when_given() -> None:
    base = ["lean-verify", "--repo", "r", "--base", "a", "--head", "b", "--output-dir", "o"]
    assert build_parser().parse_args(base).budget_requests is None
    assert build_parser().parse_args([*base, "--budget-requests", "50"]).budget_requests == 50
