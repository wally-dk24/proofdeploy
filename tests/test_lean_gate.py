"""Lean P6b (D258): the gate driver and the lean-poc workflow. No real model call is made.

Covers the unit list (28, in order, equal to the registered files), the pre-registered criteria
on synthetic result rows (every boundary), the confirm direction rule, blindness of the log, the
workflow guards (environment, serialization group, pinned actions, the key in the gate step
only), and a host-mode smoke of the driver on golden-bug plus two mutants with a stub judge.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
import yaml
from lean_helpers import PromptStub, bug_rule

from proofdeploy.lean import hostrun, pipeline
from proofdeploy.lean import run as lean_run
from proofdeploy.lean.compare import RunData
from proofdeploy.lean.http import Exchange, Step
from proofdeploy.lean.http import Scenario as HttpScenario
from proofdeploy.lean.pipeline import LeanResult
from proofdeploy.lean.verdict import FAIL, INCONCLUSIVE, PASS, Verdict

ROOT = Path(__file__).resolve().parent.parent
GATE_PATH = ROOT / "experiments" / "lean-poc" / "gate.py"
_spec = importlib.util.spec_from_file_location("lean_poc_gate", GATE_PATH)
assert _spec is not None and _spec.loader is not None
gate = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = gate
_spec.loader.exec_module(gate)

MUTANTS = json.loads((ROOT / "experiments/lean-poc/mutants.json").read_text(encoding="utf-8"))
CLEAN = json.loads(
    (ROOT / "experiments/lean-poc/paperless-clean-units.json").read_text(encoding="utf-8")
)


def _synthetic_bug(n: int) -> dict[str, Any]:
    c = str(n)
    fix = c * 4 + "abcd" + "0" * 32
    return {
        "id": f"paperless-bug-{fix[:8]}",
        "note": f"synthetic unit {n}",
        "unit_repo": "paperless-ngx/paperless-ngx",
        "b_parent": c * 40,
        "b": "a" + c * 39,
        "fix_parent": "b" + c * 39,
        "fix": fix,
        "selection_record": f"evidence/synthetic/selection-{n}.json",
        "selection_record_sha256": c * 64,
    }


BUG_UNITS = [_synthetic_bug(n) for n in (1, 2, 3)]
BUG_UNIT = BUG_UNITS[0]
BUG_IDS = [u["id"] for u in BUG_UNITS]
BUG_A = BUG_IDS[0]


def write_bug_units(path: Path, **override: Any) -> Path:
    """The synthetic units file; ``override`` edits the SECOND unit (None drops a key)."""
    second = {**BUG_UNITS[1], **override}
    second = {k: v for k, v in second.items() if v is not None}
    path.write_text(json.dumps({"units": [BUG_UNITS[0], second, BUG_UNITS[2]]}), encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def bug_unit_file(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """A synthetic paperless-bug-units.json (three units); the real file has its own test."""
    path = write_bug_units(tmp_path_factory.mktemp("bugunit") / "paperless-bug-units.json")
    monkeypatch.setattr(gate, "PAPERLESS_BUG_UNITS_JSON", path)
    return path


# --------------------------------------------------------------------------
# The unit list
# --------------------------------------------------------------------------


def test_unit_list_is_28_units_in_the_registered_order() -> None:
    units = gate.load_units()
    ids = [u.id for u in units]
    assert len(ids) == len(set(ids)) == 28 == 2 + 20 + 3 + 3
    expected = (
        ["golden-bug", "golden-clean"]
        + [f"mutant-{m['id']}" for m in MUTANTS["mutants"]]
        + BUG_IDS
        + [f"paperless-clean-{u['sha'][:12]}" for u in CLEAN["units"]]
    )
    assert ids == expected
    assert len(MUTANTS["mutants"]) == 20 and len(CLEAN["units"]) == 3


def test_unit_definitions_follow_the_work_order() -> None:
    by = {u.id: u for u in gate.load_units()}
    assert (by["golden-bug"].base, by["golden-bug"].head) == ("base", "bug")
    assert (by["golden-clean"].base, by["golden-clean"].head) == ("fix", "clean")
    for u in by.values():
        if u.group == "golden":
            assert (u.mode, u.intent) == ("sandbox", None)  # intent = the head commit's message
    mutants = [u for u in by.values() if u.kind == "mutant"]
    assert len(mutants) == 20
    for m, reg in zip(mutants, MUTANTS["mutants"], strict=True):
        assert (m.mode, m.base, m.head) == ("host", "fix", reg["branch"])
        assert m.intent == "no behaviour change intended" == MUTANTS["judge_intent"]
    for bug, reg in zip((by[i] for i in BUG_IDS), BUG_UNITS, strict=True):
        assert (bug.base, bug.head) == (reg["b_parent"], reg["b"])
        assert (bug.fix_parent, bug.fix) == (reg["fix_parent"], reg["fix"])
        assert (bug.kind, bug.mode, bug.intent, bug.paperless) == ("bug", "sandbox", None, True)
    cleans = [u for u in by.values() if u.id.startswith("paperless-clean-")]
    for c, reg in zip(cleans, CLEAN["units"], strict=True):
        assert (c.base, c.head, c.kind, c.mode) == (
            reg["first_parent"],
            reg["sha"],
            "clean",
            "sandbox",
        )
    assert gate.PAPERLESS_UNIT_REPO == "paperless-ngx/paperless-ngx"
    assert gate.PAPERLESS_OPENAPI == "/api/schema/"
    assert gate.PAPERLESS_OVERLAY == ["CELERY_TASK_ALWAYS_EAGER=True"]


def test_selection_by_group() -> None:
    units = gate.load_units()
    counts = {g: len(gate.select_units(units, g)) for g in gate.GROUPS}
    assert counts == {"all": 28, "golden": 2, "mutants": 20, "paperless": 6}
    with pytest.raises(gate.GateError):
        gate.select_units(units, "everything")


def test_regenerated_mutants_equal_the_registered_file(tmp_path: Path) -> None:
    repo, shas = gate.materialize_golden_repo(tmp_path / "g")
    assert set(shas) >= {"base", "bug", "fix", "clean"}
    branches = gate._git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads").split()
    assert sorted(b for b in branches if b.startswith("mutant/")) == sorted(
        m["branch"] for m in MUTANTS["mutants"]
    )
    # each mutant branch is one commit on fix
    for m in MUTANTS["mutants"]:
        assert gate._git(repo, "rev-parse", f"{m['branch']}~1") == shas["fix"]


# --------------------------------------------------------------------------
# The criteria, on synthetic rows
# --------------------------------------------------------------------------


def row(
    unit: str,
    verdict: str | None,
    *,
    kind: str | None = None,
    observable: bool = False,
    unexercised: int = 0,
    tokens: int = 0,
    cause: str | None = "x",
    confirmed: bool | None = None,
    error: str | None = None,
    model_errors: list[str] | None = None,
) -> dict[str, Any]:
    if kind is None:
        kind = "mutant" if unit.startswith("mutant-") else "clean" if "clean" in unit else "bug"
    return {
        "unit": unit,
        "kind": kind,
        "verdict": verdict,
        "cause": cause,
        "observable": observable,
        "unexercised": unexercised,
        "tokens": tokens,
        "confirmed": confirmed,
        "error": error,
        "model_errors": model_errors or [],
        "wall_seconds": 1.0,
    }


def golden_rows(bug: str = FAIL, clean: str = PASS) -> list[dict[str, Any]]:
    return [row("golden-bug", bug), row("golden-clean", clean)]


def mutant_rows(
    caught: int, observable: int, total: int = 20, *, extra: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    rows = []
    for i in range(total):
        if i < caught:
            rows.append(row(f"mutant-{i:02d}", FAIL, observable=True))
        elif i < observable:
            rows.append(row(f"mutant-{i:02d}", INCONCLUSIVE, observable=True))
        else:
            rows.append(row(f"mutant-{i:02d}", INCONCLUSIVE, unexercised=1))
    return rows + (extra or [])


def crit(rows: list[dict[str, Any]], cid: str) -> dict[str, Any]:
    return gate.compute_summary(rows)["criteria"][cid]  # type: ignore[no-any-return]


def test_criteria_text_is_the_registered_text() -> None:
    assert set(gate.CRITERIA) == {"1", "2", "3", "4", "5"}
    assert gate.CRITERIA["1"] == "golden-bug verdict FAIL; golden-clean verdict PASS."
    assert ">= 70% of observable mutants caught" in gate.CRITERIA["2"]
    assert "reported, not required" in gate.CRITERIA["3"]
    assert "<= 1 across the 3 paperless clean units" in gate.CRITERIA["4"]
    assert "every unit <= 10,000 AI tokens" in gate.CRITERIA["5"]


@pytest.mark.parametrize(
    ("bug", "clean", "status"),
    [
        (FAIL, PASS, "PASS"),
        (PASS, PASS, "FAIL"),
        (INCONCLUSIVE, PASS, "FAIL"),
        (FAIL, FAIL, "FAIL"),
        (FAIL, INCONCLUSIVE, "FAIL"),
    ],
)
def test_criterion_1(bug: str, clean: str, status: str) -> None:
    assert crit(golden_rows(bug, clean), "1")["status"] == status


def test_criterion_1_not_run_when_a_golden_unit_is_missing_or_errored() -> None:
    assert crit([row("golden-bug", FAIL)], "1")["status"] == "NOT_RUN"
    assert (
        crit([row("golden-bug", None, error="X"), row("golden-clean", PASS)], "1")["status"]
        == "FAIL"
    )


@pytest.mark.parametrize(
    ("caught", "observable", "status"),
    [
        (7, 10, "PASS"),  # exactly 70%
        (6, 10, "FAIL"),  # 60%
        (14, 20, "PASS"),  # exactly 70% of 20
        (13, 20, "FAIL"),  # 65%
        (3, 4, "PASS"),  # 75%
        (2, 3, "FAIL"),  # 66.7%: no rounding up
        (1, 1, "PASS"),
        (0, 0, "FAIL"),  # nothing observable: the ratio cannot be shown
    ],
)
def test_criterion_2_ratio_boundary(caught: int, observable: int, status: str) -> None:
    c = crit(mutant_rows(caught, observable), "2")
    assert c["status"] == status, c
    assert c["observable"] == observable and c["caught"] == caught


def test_criterion_2_counts_only_observable_mutants_in_the_denominator() -> None:
    # 7 caught of 10 observable; 10 unobservable ones do not dilute the ratio.
    c = crit(mutant_rows(7, 10), "2")
    assert c["status"] == "PASS" and len(c["unobservable"]) == 10


def test_criterion_2_violation_unobservable_pass_with_unexercised_lines() -> None:
    rows = mutant_rows(14, 20, total=21)
    rows[-1] = row("mutant-zz", PASS, observable=False, unexercised=2)
    c = crit(rows, "2")
    assert c["violations"] == ["mutant-zz"] and c["status"] == "FAIL"
    # an unobservable PASS with nothing unexercised is not a violation
    rows[-1] = row("mutant-zz", PASS, observable=False, unexercised=0)
    c = crit(rows, "2")
    assert c["violations"] == [] and c["status"] == "PASS"


def test_criterion_2_not_run_without_all_the_expected_mutants() -> None:
    rows = mutant_rows(7, 10, total=19)
    summary = gate.compute_summary(rows, expected=[f"mutant-{i:02d}" for i in range(20)])
    assert summary["criteria"]["2"]["status"] == "NOT_RUN"
    assert summary["complete"] is False


def bug_rows(*verdicts: str | None, **kw: Any) -> list[dict[str, Any]]:
    return [row(i, v, **kw) for i, v in zip(BUG_IDS, verdicts, strict=True)]


def test_criterion_3_is_reported_not_required_and_per_unit() -> None:
    caught = row(BUG_IDS[0], FAIL, confirmed=True, cause="judged_regression")
    c = crit([caught, row(BUG_IDS[1], PASS), row(BUG_IDS[2], None, cause=None, error="X")], "3")
    assert c["status"] == "FAIL" and c["required"] is False  # a unit with an error is not real
    full = crit(
        [
            caught,
            row(BUG_IDS[1], FAIL, confirmed=False),
            row(BUG_IDS[2], INCONCLUSIVE, cause="needs_information"),
        ],
        "3",
    )
    assert full["status"] == "PASS" and list(full["units"]) == BUG_IDS
    assert full["units"][BUG_IDS[0]]["caught"] is True
    assert full["units"][BUG_IDS[1]]["caught"] is False  # FAIL but not confirmed
    assert (
        full["units"][BUG_IDS[2]]["caught"] is False and full["units"][BUG_IDS[2]]["real_verdict"]
    )
    env = crit(
        [
            caught,
            row(BUG_IDS[1], INCONCLUSIVE, cause="environment"),
            row(BUG_IDS[2], PASS),
        ],
        "3",
    )
    assert env["status"] == "FAIL"
    assert (
        env["units"][BUG_IDS[1]]["status"] == "FAIL"
        and not env["units"][BUG_IDS[1]]["real_verdict"]
    )
    assert env["units"][BUG_IDS[0]]["status"] == "PASS"  # judged per unit, not pooled
    err = crit(
        [
            row(BUG_IDS[0], None, cause=None, error="LeanEnvironmentError"),
            row(BUG_IDS[1], PASS),
            row(BUG_IDS[2], PASS),
        ],
        "3",
    )
    assert err["units"][BUG_IDS[0]]["error"] is True and err["status"] == "FAIL"
    assert crit([], "3")["status"] == "NOT_RUN"


def test_criterion_3_not_run_until_every_expected_bug_unit_ran() -> None:
    rows = bug_rows(FAIL, PASS, PASS)[:2]
    summary = gate.compute_summary(rows, expected=BUG_IDS)
    assert summary["criteria"]["3"]["status"] == "NOT_RUN"
    assert summary["criteria"]["3"]["missing"] == [BUG_IDS[2]]
    assert summary["round4"]["bug_units_missing"] == [BUG_IDS[2]]


def test_criterion_3_reads_a_provider_limit_as_not_a_real_verdict() -> None:
    limit = crit(bug_rows(INCONCLUSIVE, PASS, PASS, cause="provider_limit"), "3")
    assert limit["status"] == "FAIL"
    one = limit["units"][BUG_IDS[0]]
    assert one["real_verdict"] is False and one["cause"] == "provider_limit"


def test_round4_block_reports_each_bug_unit() -> None:
    def rich(unit: str, verdict: str, **kw: Any) -> dict[str, Any]:
        r = row(unit, verdict, cause="judged_regression")
        r.update(
            confirmed=True,
            executable_changed_lines=12,
            lines_reached=9,
            requests_hitting_changed_code=7,
            differences=3,
            differences_judged=2,
            **kw,
        )
        return r

    rows = [rich(BUG_IDS[0], FAIL), rich(BUG_IDS[1], INCONCLUSIVE), rich(BUG_IDS[2], PASS)]
    summary = gate.compute_summary(rows, expected=BUG_IDS)
    r4 = summary["round4"]
    assert list(r4["bug_units"]) == BUG_IDS and r4["bug_units_missing"] == []
    assert r4["bug_units"][BUG_IDS[1]] == {
        "verdict": INCONCLUSIVE,
        "cause": "judged_regression",
        "confirmed": True,
        "executable_changed_lines": 12,
        "lines_reached": 9,
        "requests_hitting_changed_code": 7,
        "differences_found": 3,
        "differences_judged": 2,
    }
    # the pre-registered criteria text is unchanged
    assert summary["criteria_text"] == gate.CRITERIA
    assert "reported, not required" in summary["criteria_text"]["3"]


def test_golden_clean_runs_fix_to_clean_and_mutants_sit_on_fix() -> None:
    by = {u.id: u for u in gate.select_units(gate.load_units("golden"), "golden")}
    assert list(by) == ["golden-bug", "golden-clean"]
    assert (by["golden-clean"].base, by["golden-clean"].head) == ("fix", "clean")
    assert (by["golden-bug"].base, by["golden-bug"].head) == ("base", "bug")
    assert MUTANTS["base_revision"] == "fix"
    assert all(u.base == "fix" for u in gate.select_units(gate.load_units("mutants"), "mutants"))


def test_golden_and_mutant_groups_need_no_bug_unit_file(
    bug_unit_file: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(gate, "PAPERLESS_BUG_UNITS_JSON", tmp_path / "absent.json")
    assert len(gate.select_units(gate.load_units("golden"), "golden")) == 2
    assert len(gate.select_units(gate.load_units("mutants"), "mutants")) == 20
    for which in ("paperless", "all"):
        with pytest.raises(gate.GateError, match="paperless bug units not selected yet"):
            gate.load_units(which)


def test_absent_bug_unit_file_stops_run_gate_and_list_before_anything_runs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(gate, "PAPERLESS_BUG_UNITS_JSON", tmp_path / "absent.json")
    with mock.patch.object(lean_run, "lean_verify") as verify:
        with pytest.raises(gate.GateError, match="paperless bug units not selected yet"):
            gate.run_gate(gate.GateConfig(out=tmp_path / "o", workdir=tmp_path / "w"))
        verify.assert_not_called()
    assert not (tmp_path / "o").exists()
    assert gate.main(["--units", "all", "--list"]) == lean_run.EXIT_USAGE
    assert "paperless bug units not selected yet" in capsys.readouterr().err
    assert gate.main(["--units", "golden", "--list"]) == 0
    assert capsys.readouterr().out.split() == ["golden-bug", "golden-clean"]


def test_list_covers_all_three_bug_units(capsys: pytest.CaptureFixture[str]) -> None:
    assert gate.main(["--units", "paperless", "--list"]) == 0
    listed = capsys.readouterr().out.split()
    assert listed[:3] == BUG_IDS and len(listed) == 6
    assert gate.main(["--units", "all", "--list"]) == 0
    assert set(BUG_IDS) <= set(capsys.readouterr().out.split())
    assert [u.id for u in gate.rehearsal_units("paperless")][:3] == BUG_IDS


@pytest.mark.parametrize(
    "override",
    [
        {"b_parent": "1" * 39},
        {"b": "A" * 40},
        {"fix_parent": "g" * 40},
        {"fix": None},
        {"id": None},
        {"note": None},
        {"id": "paperless-bug"},
        {"id": BUG_A},  # repeated
        {"selection_record_sha256": "5" * 40},
        {"selection_record": ""},
        {"unit_repo": "someone/else"},
        {"extra": "x"},
    ],
)
def test_invalid_bug_unit_file_is_a_gate_error(
    override: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = write_bug_units(tmp_path / "bad.json", **override)
    monkeypatch.setattr(gate, "PAPERLESS_BUG_UNITS_JSON", path)
    with pytest.raises(gate.GateError, match="paperless bug units not selected yet"):
        gate.load_units("paperless")
    path.write_text("not json", encoding="utf-8")
    with pytest.raises(gate.GateError, match="paperless bug units not selected yet"):
        gate.load_units("all")
    assert len(gate.select_units(gate.load_units("golden"), "golden")) == 2


def test_a_unit_listed_twice_is_a_gate_error_naming_its_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "twice.json"
    path.write_text(json.dumps({"units": [BUG_UNITS[0], BUG_UNITS[1], BUG_UNITS[0]]}), "utf-8")
    monkeypatch.setattr(gate, "PAPERLESS_BUG_UNITS_JSON", path)
    with pytest.raises(gate.GateError, match=f"id {BUG_A} is repeated"):
        gate.load_paperless_bug_units()
    with pytest.raises(gate.GateError, match=f"id {BUG_A} is repeated"):
        gate.load_units("paperless")


@pytest.mark.parametrize("content", ['{"units": []}', "[]", '{"units": {}}', '{"unit": []}'])
def test_bug_unit_file_shape_is_validated(
    content: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "shape.json"
    path.write_text(content, encoding="utf-8")
    monkeypatch.setattr(gate, "PAPERLESS_BUG_UNITS_JSON", path)
    with pytest.raises(gate.GateError, match="paperless bug units not selected yet"):
        gate.load_paperless_bug_units()


@pytest.mark.parametrize(
    "override",
    [
        {"b": "a" * 41},
        {"fix": "a" * 40 + "\n"},
        {"b_parent": "a" * 40 + "\n"},
        {"selection_record_sha256": "5" * 65},
        {"selection_record_sha256": "5" * 64 + "\n"},
    ],
)
def test_bug_unit_shas_must_match_exactly(
    override: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = write_bug_units(tmp_path / "long.json", **override)
    monkeypatch.setattr(gate, "PAPERLESS_BUG_UNITS_JSON", path)
    with pytest.raises(gate.GateError, match="paperless bug units not selected yet"):
        gate.load_paperless_bug_units()


def test_valid_bug_unit_file_is_loaded_not_constants() -> None:
    assert gate.load_paperless_bug_units() == BUG_UNITS
    assert not hasattr(gate, "PAPERLESS_FIX") and not hasattr(gate, "PAPERLESS_BASE")


# The committed file: the three bugs already found (WO-R4-B), with the sha256 of each relay record.
REAL_BUG_UNITS = {
    "paperless-bug-371c4f57": (
        "3a34bdc7b078486bc85a97ce309a1c8180cdfc88",
        "7dca0bfa6af103b76185aada3ce736b0ac1abc01",
        "5569447ea00c50de8c0c1c75136418bb3343dad6",
        "371c4f57d3ab208fc261e159fa86ca1dc80cb334",
        "selection/wo6-bug-selection-record.json",
        "6dd2956a100e79d39a2957fa55993f511fe8c9157836b183b32d1681eb2d318d",
    ),
    "paperless-bug-d3710856": (
        "17dc482872cf62e12965f6207772c83d1deb651b",
        "aa67fd3aefa6489984fc15a5c32c1ad2b16ac23a",
        "14b6a41b88e75fb7d35aa84b33986870bfd1aa69",
        "d3710856992a91783783819217b43392aab73e00",
        "selection/wo6-c4-2026-10-20/wo6-bug-selection-record.json",
        "cd5b78361b8829d4095dca3ca323d630c62483bee988c391698eba3b23a12784",
    ),
    "paperless-bug-b992f9fc": (
        "e01a762e81dd65dff927988aa51594906d8a417f",
        "aed9abe48c02b1b7df07e429db3f2ed8c3a58ce8",
        "6d249b49327fdab58ff915d3f5435e49de742464",
        "b992f9fc05bedd2f26f7291028b0b38471e697db",
        "selection/lean-r2-2026-10-21/out",
        "44a411590b3f58cbbda295f293b89817d693c34d221f33061ebc528d92b5907a",
    ),
}


def test_the_committed_file_holds_the_three_registered_bug_units() -> None:
    real = gate.load_paperless_bug_units(ROOT / "experiments/lean-poc/paperless-bug-units.json")
    assert [u["id"] for u in real] == list(REAL_BUG_UNITS)
    for u in real:
        got = (
            u["b_parent"],
            u["b"],
            u["fix_parent"],
            u["fix"],
            u["selection_record"],
            u["selection_record_sha256"],
        )
        assert got == REAL_BUG_UNITS[u["id"]]
    notes = {u["id"]: u["note"] for u in real}
    assert (
        notes["paperless-bug-371c4f57"]
        == "upgrade-path only (D229); expected not observable over HTTP"
    )
    assert (
        notes["paperless-bug-d3710856"]
        == "frontend-only B (round 1); expected not observable over HTTP"
    )
    assert gate.PAPERLESS_BUG_UNITS_JSON.name == "paperless-bug-units.json"
    assert not (ROOT / "experiments/lean-poc/paperless-bug-unit.json").exists()


def paperless_cleans(*verdicts: str | None) -> list[dict[str, Any]]:
    return [row(f"paperless-clean-{i}", v) for i, v in enumerate(verdicts)]


@pytest.mark.parametrize(
    ("golden", "paperless", "status"),
    [
        (PASS, (PASS, PASS, PASS), "PASS"),
        (PASS, (FAIL, PASS, PASS), "PASS"),  # one false alarm is allowed
        (PASS, (FAIL, FAIL, PASS), "FAIL"),  # two are not
        (FAIL, (PASS, PASS, PASS), "FAIL"),  # none allowed on golden-clean
        (INCONCLUSIVE, (INCONCLUSIVE, INCONCLUSIVE, INCONCLUSIVE), "PASS"),  # not an alarm
    ],
)
def test_criterion_4_boundary(golden: str, paperless: tuple[str, ...], status: str) -> None:
    c = crit([row("golden-clean", golden), *paperless_cleans(*paperless)], "4")
    assert c["status"] == status, c


def test_criterion_4_reports_inconclusive_clean_units_as_their_own_count() -> None:
    c = crit([row("golden-clean", INCONCLUSIVE), *paperless_cleans(INCONCLUSIVE, PASS, FAIL)], "4")
    assert c["inconclusive_clean_units"] == 2
    assert c["golden_clean_false_alarms"] == 0 and c["paperless_clean_false_alarms"] == 1
    assert c["status"] == "PASS"


def test_criterion_4_not_run_when_a_clean_unit_did_not_run() -> None:
    assert crit([row("golden-clean", PASS)], "4")["status"] == "NOT_RUN"
    assert crit(paperless_cleans(PASS, PASS, PASS), "4")["status"] == "NOT_RUN"


def test_criterion_5_token_cap_and_provider_failures() -> None:
    ok = crit([row("a", PASS, tokens=10_000), row("b", PASS, tokens=0)], "5")
    assert ok["status"] == "PASS" and ok["max_tokens"] == 10_000
    over = crit([row("a", PASS, tokens=10_001)], "5")
    assert over["status"] == "FAIL" and over["over_cap"] == ["a"]
    # a provider failure is reported, not a failure of the cost criterion itself
    prov = crit([row("a", INCONCLUSIVE, tokens=5, model_errors=["model_rate_limit"])], "5")
    assert prov["status"] == "PASS"
    assert prov["provider_failures"] == {"a": ["model_rate_limit"]}


def test_partial_selection_never_passes_on_part_of_the_data() -> None:
    summary = gate.compute_summary(golden_rows(), expected=[u.id for u in gate.load_units()])
    assert summary["complete"] is False and summary["units_run"] == 2
    assert summary["criteria"]["1"]["status"] == "PASS"
    for cid in ("2", "3", "4", "5"):
        assert summary["criteria"][cid]["status"] == "NOT_RUN", cid


# --------------------------------------------------------------------------
# The confirm direction rule
# --------------------------------------------------------------------------

SID = "abc123abc123"


def _run(label: str, body: Any, status: int = 200, step: int = 1) -> RunData:
    data = RunData(label)
    ex = Exchange(
        scenario_id=SID,
        step_index=step,
        request={"method": "GET", "url_path": "/api/documents/1/"},
        status=status,
        headers_subset={},
        body_json_or_text=body,
        elapsed_ms=1.0,
    )
    key = (SID, step, 0)
    data.exchanges[key] = ex
    data.order.append(key)
    return data


def _pair(body: Any, status: int = 200) -> tuple[RunData, RunData]:
    return _run("r1", body, status), _run("r2", body, status)


FLAG = [gate.Flagged(f"{SID}:1", "title", "B-value")]


def test_confirmed_when_fix_parent_shows_the_flagged_value_and_fix_moves_away() -> None:
    out = gate.confirm_flagged(FLAG, _pair({"title": "B-value"}), _pair({"title": "good"}))
    assert out["confirmed"] is True and out["items"][0]["reason"] == "confirmed"


def test_not_confirmed_when_the_fix_keeps_the_flagged_value() -> None:
    out = gate.confirm_flagged(FLAG, _pair({"title": "B-value"}), _pair({"title": "B-value"}))
    assert out["confirmed"] is False
    assert out["items"][0]["reason"] == "fix_does_not_move_away"


def test_not_confirmed_when_fix_parent_already_differs_from_b() -> None:
    out = gate.confirm_flagged(FLAG, _pair({"title": "other"}), _pair({"title": "good"}))
    assert out["confirmed"] is False
    assert out["items"][0]["reason"] == "fix_parent_differs_from_head"


def test_not_confirmed_when_a_run_pair_disagrees() -> None:
    unstable = (_run("r1", {"title": "B-value"}), _run("r2", {"title": "noise"}))
    out = gate.confirm_flagged(FLAG, unstable, _pair({"title": "good"}))
    assert out["confirmed"] is False
    out = gate.confirm_flagged(FLAG, _pair({"title": "B-value"}), unstable)
    assert out["confirmed"] is False


def test_every_flagged_field_must_confirm() -> None:
    flagged = [gate.Flagged(f"{SID}:1", "title", "B-value"), gate.Flagged(f"{SID}:1", "n", 1)]
    both = gate.confirm_flagged(
        flagged, _pair({"title": "B-value", "n": 1}), _pair({"title": "good", "n": 2})
    )
    assert both["confirmed"] is True
    one = gate.confirm_flagged(
        flagged, _pair({"title": "B-value", "n": 1}), _pair({"title": "good", "n": 1})
    )
    assert one["confirmed"] is False


def test_missing_exchange_or_no_flagged_items_never_confirm() -> None:
    assert gate.confirm_flagged([], _pair({}), _pair({}))["confirmed"] is False
    other = [gate.Flagged("zzz:9", "title", "B-value")]
    assert (
        gate.confirm_flagged(other, _pair({"title": "B-value"}), _pair({"title": "g"}))["confirmed"]
        is False
    )


def test_array_fields_and_absent_fields() -> None:
    flag = [gate.Flagged(f"{SID}:1", "results[].done", True)]
    fp = _pair({"results": [{"done": True}, {"done": True}]})
    fx = _pair({"results": [{"done": False}, {"done": False}]})
    assert gate.confirm_flagged(flag, fp, fx)["confirmed"] is True
    mixed = _pair({"results": [{"done": False}, {"done": True}]})
    assert gate.confirm_flagged(flag, fp, mixed)["confirmed"] is False
    # B removed a field (after = None); the fix brings it back
    gone = [gate.Flagged(f"{SID}:1", "tag", None)]
    assert gate.confirm_flagged(gone, _pair({}), _pair({"tag": "x"}))["confirmed"] is True
    assert gate.confirm_flagged(gone, _pair({}), _pair({}))["confirmed"] is False


def test_status_flag_for_a_hard_oracle_failure() -> None:
    verdict = {
        "cause": "hard_oracle",
        "details": {
            "hard_failures": [
                {"request_id": f"{SID}:1", "kind": "server_error", "detail": "d"},
                {"request_id": "", "kind": "traceback", "detail": "d"},
            ]
        },
    }
    flagged = gate.flagged_from_verdict(verdict, [{"request_id": f"{SID}:1", "status": 500}])
    assert flagged[0] == gate.Flagged(f"{SID}:1", "$status", 500)
    assert flagged[1].field_path == gate.UNCONFIRMABLE
    ok = gate.confirm_flagged(flagged[:1], _pair({}, 500), _pair({}, 200))
    assert ok["confirmed"] is True
    # an unconfirmable item blocks the whole confirmation
    assert gate.confirm_flagged(flagged, _pair({}, 500), _pair({}, 200))["confirmed"] is False


def test_flagged_from_a_judged_regression_covers_each_request_of_the_group() -> None:
    verdict = {
        "cause": "judged_regression",
        "details": {
            "regressions": [
                {"field_path": "done", "after": True, "request_ids": ["a:1", "b:2"]},
            ]
        },
    }
    assert gate.flagged_from_verdict(verdict, []) == [
        gate.Flagged("a:1", "done", True),
        gate.Flagged("b:2", "done", True),
    ]
    assert gate.flagged_from_verdict({"cause": "no_regression_found"}, []) == []


# --------------------------------------------------------------------------
# Capture of the replayed scenarios and the confirm run (no sandbox, no model)
# --------------------------------------------------------------------------


def _scenario() -> HttpScenario:
    return HttpScenario(
        steps=[Step("GET", "/api/documents/1/")], target_operation="GET /api/documents/{id}/"
    )


def test_capture_replays_records_the_first_scenario_set_and_delegates() -> None:
    sc = _scenario()
    fake = mock.Mock(return_value="run")
    with mock.patch.object(pipeline, "replay_run", fake), gate._capture_replays() as cap:
        replay: Any = pipeline.replay_run  # the spy installed by _capture_replays
        assert replay("drv", "base", [sc], "auth") == "run"
        replay("drv", "head", [], None)
    assert cap["scenarios"] == [sc] and cap["auth"] == "auth"
    assert fake.call_count == 2


class FakeSide:
    def __init__(self, label: str) -> None:
        self.label = label
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _confirm_unit(unit_id: str, tmp_path: Path) -> tuple[Any, list[FakeSide], list[Any]]:
    """Run run_confirm for one unit; returns (output, sides, provisioned (base, head) pairs)."""
    sides: list[FakeSide] = []
    provisioned: list[tuple[str, str]] = []
    calls: list[tuple[str, str]] = []

    def provision(rc: Any, contract_file: Any, work: Path, timings: Any) -> Any:
        assert (rc.base_sha, rc.head_sha) == (rc.base, rc.head)
        assert rc.unit_repo == gate.PAPERLESS_UNIT_REPO and contract_file is not None
        provisioned.append((rc.base, rc.head))
        sides.extend([FakeSide("fixp"), FakeSide("fix")])
        return sides[-2], sides[-1], object()

    def replay(driver: FakeSide, label: str, scenarios: list[Any], auth: Any) -> RunData:
        calls.append((driver.label, label))
        return _run(label, {"title": "B-value" if driver.label == "fixp" else "good"})

    cfg = gate.GateConfig(out=tmp_path, workdir=tmp_path, provision=provision, replay=replay)
    rc = lean_run.LeanRunConfig(
        repo=tmp_path,
        base="x",
        head="y",
        output_dir=tmp_path / "u",
        unit_repo=gate.PAPERLESS_UNIT_REPO,
        django_overlay={"CELERY_TASK_ALWAYS_EAGER": True},
        openapi_path="/api/schema/",
    )
    unit = next(u for u in gate.load_units("paperless") if u.id == unit_id)
    with mock.patch.object(gate.pipeline, "resolve_sha", lambda _r, rev: rev):
        out = gate.run_confirm(
            cfg, rc, [_scenario()], None, [gate.Flagged(f"{SID}:1", "title", "B-value")], unit
        )
    assert calls == [("fixp", "fixp"), ("fixp", "fixp2"), ("fix", "fix"), ("fix", "fix2")]
    return out, sides, provisioned


@pytest.mark.parametrize("n", [0, 1, 2])
def test_run_confirm_uses_each_units_own_fix_parent_and_fix(n: int, tmp_path: Path) -> None:
    mine = BUG_UNITS[n]
    out, sides, provisioned = _confirm_unit(mine["id"], tmp_path)
    assert out["confirmed"] is True and out["runs_each"] == 2
    assert (out["fix_parent"], out["fix"]) == (mine["fix_parent"], mine["fix"])
    assert provisioned == [(mine["fix_parent"], mine["fix"])]
    for other in BUG_UNITS:
        if other is not mine:
            assert other["fix"] not in (out["fix"], provisioned[0][1])
    assert all(s.closed for s in sides)


def test_run_unit_confirms_a_failing_bug_unit_against_its_own_fix(tmp_path: Path) -> None:
    unit = next(u for u in gate.load_units("paperless") if u.id == BUG_IDS[2])
    seen: list[Any] = []

    def fake_confirm(cfg: Any, rc: Any, scenarios: Any, auth: Any, flagged: Any, u: Any) -> Any:
        seen.append(u)
        return {"confirmed": True, "items": [], "fix": u.fix, "fix_parent": u.fix_parent}

    verdict = Verdict(
        FAIL,
        "judged_regression",
        {"regressions": [{"field_path": "title", "after": "B", "request_ids": [f"{SID}:1"]}]},
    )
    manifest = {"mode": "sandbox", "compared": {"differences": 1}}
    result = LeanResult(verdict, mock.Mock(total_tokens=1, calls=[], groups=[]), manifest)

    def verify(rc: Any, *, model_runner: Any = None, log: Any = lambda _m: None) -> Any:
        replay: Any = pipeline.replay_run
        replay("drv", "head", [_scenario()], None)  # the spy records the impact set
        return result, 1

    cfg = gate.GateConfig(out=tmp_path / "o", workdir=tmp_path / "w", paperless_repo=tmp_path)
    with (
        mock.patch.object(lean_run, "lean_verify", verify),
        mock.patch.object(pipeline, "replay_run", mock.Mock()),
        mock.patch.object(gate, "run_confirm", fake_confirm),
    ):
        r = gate.run_unit(cfg, unit, gate._Repos())
    assert r["confirmed"] is True and r["unit"] == BUG_IDS[2], r
    assert [(u.id, u.fix) for u in seen] == [(BUG_IDS[2], BUG_UNITS[2]["fix"])]
    assert (tmp_path / "o/units" / BUG_IDS[2] / "confirm.json").is_file()


# --------------------------------------------------------------------------
# Blindness
# --------------------------------------------------------------------------

SECRET = "SECRET-COMMIT-MESSAGE-AND-FILE-CONTENT"


def test_paperless_output_never_reaches_the_log(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    unit = next(u for u in gate.load_units() if u.id == BUG_A)

    def leaky(rc: Any, *, model_runner: Any = None, log: Any = lambda _m: None) -> Any:
        log(f"index: {SECRET}")
        print(SECRET)
        print(SECRET, file=sys.stderr)
        raise RuntimeError(SECRET)

    cfg = gate.GateConfig(out=tmp_path / "out", workdir=tmp_path / "w", paperless_repo=tmp_path)
    with mock.patch.object(lean_run, "lean_verify", leaky):
        r = gate.run_unit(cfg, unit, gate._Repos())
    line = gate.log_line(r, unit.paperless)
    shown = capsys.readouterr()
    assert SECRET not in shown.out + shown.err + line
    assert r["error"].startswith("RuntimeError") and SECRET in r["error"]  # the artifact has it
    assert SECRET in (tmp_path / "out/units" / BUG_A / "console.log").read_text("utf-8")
    assert line.startswith(f"[{BUG_A}] ERROR RuntimeError")


def test_row_counts_and_flags_from_a_lean_result() -> None:
    unit = next(u for u in gate.load_units() if u.id == "golden-bug")
    manifest = {
        "mode": "sandbox",
        "compared": {"differences": 1},
        "noise": [{"x": 1}, {"x": 2}],
        "unexercised_lines": [],
        "hard_failures": [],
        "flaky_hard_failures": [],
        "impacted_operations_head": {"GET /a": {"requests": 4, "2xx": 3, "2xx_rate": 0.75}},
        "timings_seconds": {"judge": 1.5},
        "observable": True,
    }
    record = mock.Mock(
        total_tokens=321, calls=[{"status": "ok", "raw_content": "r", "attempts": ["a"]}]
    )
    result = LeanResult(Verdict(FAIL, "judged_regression", {}), record, manifest)
    r = gate.row_from_result(unit, result, 1, wall_seconds=2.0)
    assert (r["verdict"], r["cause"], r["observable"], r["differences"], r["noise"]) == (
        FAIL,
        "judged_regression",
        True,
        1,
        2,
    )
    assert (r["tokens"], r["model_calls"], r["rate_2xx"], r["confirmed"]) == (321, 1, 0.75, None)
    assert r["measured_result"] is False and len(r["judge_raw_sha256"]) == 64
    # the round-4 counts: reached = executable - unexercised - load-only; judged = differences
    # in groups the model actually judged (an unjudged group does not count)
    manifest.update(
        executable_changed_lines=12,
        unexercised_lines=["a:1", "a:2"],
        coverage={"head": {"load_only_declarations": ["d1"]}, "base": {}},
    )
    record.groups = [
        {"group": {"request_ids": ["s:1", "s:2"]}, "judgment": {"verdict": "correct"}},
        {"group": {"request_ids": ["s:3"]}, "judgment": {"verdict": "unjudged"}},
        {"group": {"request_ids": ["s:4"]}, "judgment": {"verdict": "regression"}},
    ]
    r4 = gate.row_from_result(unit, result, 1, wall_seconds=0.0)
    assert (r4["executable_changed_lines"], r4["lines_reached"]) == (12, 9)
    assert (r4["requests_hitting_changed_code"], r4["differences_judged"]) == (4, 3)
    manifest.update(unexercised_lines=[], coverage={})
    # flaky only is observable; nothing at all is not
    manifest.update(compared={"differences": 0}, flaky_hard_failures=[{"x": 1}])
    assert gate.row_from_result(unit, result, 1, wall_seconds=0.0)["observable"] is True
    manifest.update(flaky_hard_failures=[])
    assert gate.row_from_result(unit, result, 1, wall_seconds=0.0)["observable"] is False


# --------------------------------------------------------------------------
# The workflow
# --------------------------------------------------------------------------

WORKFLOW = ROOT / ".github" / "workflows" / "lean-poc.yml"


def _wf() -> dict[str, Any]:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def test_workflow_is_dispatch_only_with_the_units_choice() -> None:
    data: Any = _wf()
    on = data.get("on", data.get(True))
    assert list(on) == ["workflow_dispatch"]
    units = on["workflow_dispatch"]["inputs"]["units"]
    assert units["type"] == "choice" and units["default"] == "all"
    assert units["options"] == ["all", "golden", "mutants", "paperless", "linkding"]


def test_workflow_gates_serialization_permissions_and_timeout() -> None:
    data = _wf()
    assert data["permissions"] == {"contents": "read"}
    assert data["concurrency"] == {
        "group": "proofdeploy-model-calls",
        "cancel-in-progress": False,
        "queue": "max",
    }
    job = data["jobs"]["gate"]
    assert job["environment"] == "measurement"
    assert job["timeout-minutes"] == 300
    assert job["runs-on"] == "ubuntu-latest"  # as sandbox-tests
    assert list(data["jobs"]) == ["gate"]


def test_workflow_actions_are_sha_pinned_and_match_the_existing_pins() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    uses = re.findall(r"uses:\s*(\S+)", text)
    assert uses
    for u in uses:
        assert re.fullmatch(r"[\w./-]+@[0-9a-f]{40}", u), u
    known = set()
    for other in ("measure.yml", "sandbox-tests.yml"):
        known |= set(
            re.findall(r"uses:\s*(\S+)", (ROOT / ".github/workflows" / other).read_text("utf-8"))
        )
    assert set(uses) <= known
    for step in _wf()["jobs"]["gate"]["steps"]:
        if str(step.get("uses", "")).startswith("actions/checkout"):
            assert step["with"]["persist-credentials"] is False


def test_workflow_key_is_in_the_gate_step_only_and_inputs_reach_the_shell_via_env() -> None:
    steps = _wf()["jobs"]["gate"]["steps"]
    with_key = [s["name"] for s in steps if "GROQ_API_KEY" in (s.get("env") or {})]
    assert with_key == ["Run the gate"]
    assert re.findall(r"secrets\.(\w+)", WORKFLOW.read_text(encoding="utf-8")) == ["GROQ_API_KEY"]
    for s in steps:
        assert "${{" not in s.get("run", ""), s.get("name")
    gate_step = next(s for s in steps if s.get("name") == "Run the gate")
    assert gate_step["env"]["UNITS"] == "${{ inputs.units }}"
    assert "experiments/lean-poc/gate.py" in gate_step["run"]


def test_workflow_podman_setup_is_the_sandbox_tests_one_and_nothing_is_pushed() -> None:
    steps = _wf()["jobs"]["gate"]["steps"]
    podman = next(s for s in steps if s.get("name") == "Install podman")
    assert podman["run"].split() == "sudo apt-get update sudo apt-get install -y podman".split()
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "git push" not in text and "contents: write" not in text
    upload = steps[-1]
    assert upload["if"] == "${{ always() }}" and upload["with"]["path"] == "/tmp/lean-poc-out/"


# --------------------------------------------------------------------------
# Host-mode smoke of the driver (stub judge; golden-bug + 2 mutants)
# --------------------------------------------------------------------------


def _smoke_rule(op: str, field: str, kind: str, before: Any, after: Any) -> dict[str, Any] | None:
    got = bug_rule(op, field, kind, before, after)
    return got if got is not None else {"verdict": "unclear", "question": "stub: cannot tell"}


def test_host_smoke_golden_bug_and_two_mutants(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    if not hostrun.pypi_reachable():
        pytest.skip("PyPI unreachable: cannot create the hash-pinned golden venv (no network)")
    # CI: pytest-cov exports these; the app must not inherit them.
    monkeypatch.setenv("COVERAGE_FILE", str(tmp_path / ".coverage.host"))
    monkeypatch.setenv("COV_CORE_SOURCE", "proofdeploy")
    monkeypatch.setenv("COV_CORE_CONFIG", "x")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    stub = PromptStub(_smoke_rule)
    two = [m["id"] for m in MUTANTS["mutants"][:2]]
    only = ["golden-bug", *[f"mutant-{i}" for i in two]]
    cfg = gate.GateConfig(
        out=tmp_path / "out",
        workdir=tmp_path / "work",
        runner=stub,
        golden_mode="host",
        only=only,
    )
    summary = gate.run_gate(cfg)
    printed = capsys.readouterr().out
    lines = [
        json.loads(x)
        for x in (cfg.out / "gate-results.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [r["unit"] for r in lines] == only, printed
    for r in lines:
        assert r["error"] is None, r
        assert r["mode"] == "host" and r["measured_result"] is False
        assert (cfg.out / "units" / r["unit"] / "lean-record.json").is_file() or r["tokens"] == 0
        assert (cfg.out / "units" / r["unit"] / "manifest.json").is_file()
        assert not (cfg.out / "units" / r["unit"] / "work").exists()  # cleaned up
        assert len(r["judge_raw_sha256"]) == 64
    bug = lines[0]
    assert (bug["verdict"], bug["cause"]) == (FAIL, "judged_regression"), bug
    assert bug["observable"] is True and bug["differences"] >= 1 and bug["tokens"] > 0
    assert bug["model_calls"] >= 1 and bug["confirmed"] is None
    assert all(r["kind"] == "mutant" for r in lines[1:])
    assert stub.prompts
    # the summary was computed from these rows, and a partial selection is not a PASS
    on_disk = json.loads((cfg.out / "gate-summary.json").read_text(encoding="utf-8"))
    assert on_disk == json.loads(json.dumps(summary))
    assert on_disk["units_run"] == 3 and on_disk["complete"] is True
    assert on_disk["criteria"]["1"]["status"] == "NOT_RUN"  # golden-clean was not selected
    assert on_disk["criteria"]["5"]["status"] == "PASS"
    assert "criterion 1: NOT_RUN" in printed
    assert "[golden-bug] FAIL (judged_regression) mode=host" in printed
