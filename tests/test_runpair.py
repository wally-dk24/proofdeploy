"""Tests for the run pair and JSONL records (WAL-58)."""

import json

from proofdeploy.executor import ProbeResult, Verdict
from proofdeploy.runpair import (
    RunPairVerdict,
    RunRecord,
    score_run_pair,
    write_record,
)


def _result(verdict: Verdict) -> ProbeResult:
    return ProbeResult(probe_index=0, verdict=verdict)


def test_catch():
    parent = [_result(Verdict.FAIL)]
    fix = [_result(Verdict.PASS)]
    assert score_run_pair(parent, fix) == RunPairVerdict.CATCH


def test_no_catch_both_pass():
    parent = [_result(Verdict.PASS)]
    fix = [_result(Verdict.PASS)]
    assert score_run_pair(parent, fix) == RunPairVerdict.NO_CATCH


def test_no_catch_both_fail():
    parent = [_result(Verdict.FAIL)]
    fix = [_result(Verdict.FAIL)]
    assert score_run_pair(parent, fix) == RunPairVerdict.NO_CATCH


def test_inconclusive_when_parent_none():
    assert score_run_pair(None, [_result(Verdict.PASS)]) == RunPairVerdict.INCONCLUSIVE


def test_inconclusive_when_fix_none():
    assert score_run_pair([_result(Verdict.FAIL)], None) == RunPairVerdict.INCONCLUSIVE


def test_inconclusive_probe_poison():
    parent = [_result(Verdict.FAIL), _result(Verdict.INCONCLUSIVE)]
    fix = [_result(Verdict.PASS)]
    assert score_run_pair(parent, fix) == RunPairVerdict.INCONCLUSIVE


def test_inconclusive_empty():
    assert score_run_pair([], [_result(Verdict.PASS)]) == RunPairVerdict.INCONCLUSIVE


def test_write_record(tmp_path):
    record = RunRecord(
        timestamp="2026-10-09T18:30:00Z",
        bug_id="H1",
        fix_sha="02ee6d44" + "0" * 32,
        fix_parent_sha="abc123" + "0" * 34,
        prompt="test prompt",
        raw_model_response="raw",
        parsed_probes=[{"act": {}}],
        bundle_manifest={"skill_sha256": "abc"},
        skill_sha256="abc",
        harness_version="1.0.0",
        verdict="catch",
    )
    path = write_record(record, tmp_path)
    assert path.exists()
    line = path.read_text(encoding="utf-8").strip()
    data = json.loads(line)
    assert data["bug_id"] == "H1"
    assert data["verdict"] == "catch"
    assert "prompt_sha256" in data
    assert data["skill_sha256"] == "abc"


def test_write_record_no_skill(tmp_path):
    record = RunRecord(
        timestamp="2026-10-09T18:30:00Z",
        bug_id="H1",
        fix_sha="02ee6d44" + "0" * 32,
        fix_parent_sha="abc123" + "0" * 34,
        prompt="p",
        raw_model_response="r",
        parsed_probes=[],
        bundle_manifest={"skill_sha256": None},
        skill_sha256=None,
        harness_version="1.0.0",
    )
    path = write_record(record, tmp_path)
    data = json.loads(path.read_text(encoding="utf-8").strip())
    assert data["skill_sha256"] is None
