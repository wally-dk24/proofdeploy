"""Tests for the run pair, scoring, records, and secret redaction (WAL-58, WO-4)."""

import json

import pytest

from proofdeploy.executor import ProbeResult, Verdict
from proofdeploy.runpair import (
    DEFAULT_SCORING_VERSION,
    SCORING_VERSION_V1,
    SCORING_VERSION_V2,
    RunPairVerdict,
    build_evidence_record,
    build_score_record,
    read_records,
    redact_secrets,
    score_clean_diff,
    score_run_pair,
    write_evidence_record,
    write_score_record,
)


def _result(verdict: Verdict, index: int = 0) -> ProbeResult:
    return ProbeResult(probe_index=index, verdict=verdict)


# ---------------------------------------------------------------------------
# v2 per-probe scoring (registered rule, PR #38)
# ---------------------------------------------------------------------------


def test_v2_is_default():
    assert DEFAULT_SCORING_VERSION == SCORING_VERSION_V2


def test_v2_catch_one_discriminating_probe():
    parent = [_result(Verdict.FAIL, 0)]
    fix = [_result(Verdict.PASS, 0)]
    assert score_run_pair(parent, fix) == RunPairVerdict.CATCH


def test_v2_sibling_inconclusive_does_not_cancel_catch():
    # Registered rule: a sibling INCONCLUSIVE does not cancel the catch.
    parent = [_result(Verdict.FAIL, 0), _result(Verdict.INCONCLUSIVE, 1)]
    fix = [_result(Verdict.PASS, 0), _result(Verdict.PASS, 1)]
    assert score_run_pair(parent, fix) == RunPairVerdict.CATCH


def test_v2_sibling_fail_both_sides_does_not_cancel_catch():
    parent = [_result(Verdict.FAIL, 0), _result(Verdict.FAIL, 1)]
    fix = [_result(Verdict.PASS, 0), _result(Verdict.FAIL, 1)]
    assert score_run_pair(parent, fix) == RunPairVerdict.CATCH


def test_v2_no_catch_both_pass():
    parent = [_result(Verdict.PASS, 0)]
    fix = [_result(Verdict.PASS, 0)]
    assert score_run_pair(parent, fix) == RunPairVerdict.NO_CATCH


def test_v2_no_catch_both_fail():
    parent = [_result(Verdict.FAIL, 0)]
    fix = [_result(Verdict.FAIL, 0)]
    assert score_run_pair(parent, fix) == RunPairVerdict.NO_CATCH


def test_v2_inconclusive_when_fix_side_unreachable():
    # FAIL on fix^ but every fix probe INCONCLUSIVE: nothing to compare
    # on the fix side -> INCONCLUSIVE (registered whole-side rule).
    parent = [_result(Verdict.FAIL, 0)]
    fix = [_result(Verdict.INCONCLUSIVE, 0)]
    assert score_run_pair(parent, fix) == RunPairVerdict.INCONCLUSIVE


def test_v2_inconclusive_when_parent_none():
    assert score_run_pair(None, [_result(Verdict.PASS)]) == RunPairVerdict.INCONCLUSIVE


def test_v2_inconclusive_when_fix_none():
    assert score_run_pair([_result(Verdict.FAIL)], None) == RunPairVerdict.INCONCLUSIVE


def test_v2_inconclusive_when_empty():
    assert score_run_pair([], [_result(Verdict.PASS)]) == RunPairVerdict.INCONCLUSIVE


def test_v2_inconclusive_when_all_probes_unreachable():
    # "the target is unreachable for every probe" -> INCONCLUSIVE.
    parent = [_result(Verdict.INCONCLUSIVE, 0), _result(Verdict.INCONCLUSIVE, 1)]
    fix = [_result(Verdict.PASS, 0)]
    assert score_run_pair(parent, fix) == RunPairVerdict.INCONCLUSIVE
    parent = [_result(Verdict.FAIL, 0)]
    fix = [_result(Verdict.INCONCLUSIVE, 0), _result(Verdict.INCONCLUSIVE, 1)]
    assert score_run_pair(parent, fix) == RunPairVerdict.INCONCLUSIVE


def test_v2_probes_matched_by_index():
    parent = [_result(Verdict.PASS, 0), _result(Verdict.FAIL, 1)]
    fix = [_result(Verdict.PASS, 0), _result(Verdict.PASS, 1)]
    assert score_run_pair(parent, fix) == RunPairVerdict.CATCH


def test_v2_unknown_version_raises():
    with pytest.raises(ValueError):
        score_run_pair([_result(Verdict.FAIL)], [_result(Verdict.PASS)], "v9")


# ---------------------------------------------------------------------------
# v1 legacy scoring (behind the flag)
# ---------------------------------------------------------------------------


def test_v1_catch():
    parent = [_result(Verdict.FAIL)]
    fix = [_result(Verdict.PASS)]
    assert score_run_pair(parent, fix, SCORING_VERSION_V1) == RunPairVerdict.CATCH


def test_v1_inconclusive_poison():
    # Legacy rule: any INCONCLUSIVE poisons the pair.
    parent = [_result(Verdict.FAIL), _result(Verdict.INCONCLUSIVE)]
    fix = [_result(Verdict.PASS)]
    assert score_run_pair(parent, fix, SCORING_VERSION_V1) == RunPairVerdict.INCONCLUSIVE


def test_v1_no_catch_both_pass():
    parent = [_result(Verdict.PASS)]
    fix = [_result(Verdict.PASS)]
    assert score_run_pair(parent, fix, SCORING_VERSION_V1) == RunPairVerdict.NO_CATCH


# ---------------------------------------------------------------------------
# Strict clean-diff scoring
# ---------------------------------------------------------------------------


def test_clean_diff_all_pass_no_alarm():
    assert score_clean_diff([_result(Verdict.PASS), _result(Verdict.PASS)]) is False


def test_clean_diff_fail_is_false_alarm():
    assert score_clean_diff([_result(Verdict.PASS), _result(Verdict.FAIL)]) is True


def test_clean_diff_inconclusive_is_false_alarm():
    assert score_clean_diff([_result(Verdict.INCONCLUSIVE)]) is True


def test_clean_diff_could_not_run_is_false_alarm():
    assert score_clean_diff(None) is True
    assert score_clean_diff([]) is True


# ---------------------------------------------------------------------------
# Secret redaction
# ---------------------------------------------------------------------------


def test_redact_admin_key():
    rec = {"fixture": {"ADMIN_KEY": "sk-admin-secret-123"}, "other": "keep"}
    out = redact_secrets(rec)
    assert out["fixture"]["ADMIN_KEY"] == "***REDACTED***"
    assert out["other"] == "keep"


def test_redact_token_keys():
    rec = {"auth_token": "abc", "X-Api-Key": "def", "nested": {"refresh_token": "ghi"}}
    out = redact_secrets(rec)
    assert out["auth_token"] == "***REDACTED***"
    assert out["X-Api-Key"] == "***REDACTED***"
    assert out["nested"]["refresh_token"] == "***REDACTED***"


def test_redact_authorization_header():
    rec = {
        "http_headers": {
            "Authorization": "Bearer my-bearer-token-xyz",
            "Content-Type": "application/json",
        }
    }
    out = redact_secrets(rec)
    assert out["http_headers"]["Authorization"] == "***REDACTED***"
    assert out["http_headers"]["Content-Type"] == "application/json"


def test_redact_jwt_in_string():
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
    rec = {"details": [f"minted token {jwt} for request"]}
    out = redact_secrets(rec)
    assert "eyJ" not in out["details"][0]
    assert "***REDACTED_JWT***" in out["details"][0]


def test_redact_bearer_in_free_text():
    rec = {"runner_log": ["sending Bearer abc123.def456"]}
    out = redact_secrets(rec)
    assert "abc123" not in out["runner_log"][0]
    assert "Bearer ***REDACTED***" in out["runner_log"][0]


def test_redact_does_not_touch_non_secrets():
    rec = {"lockfile_sha256": "abc123", "runtime_version": "v20.20.0", "verdict": "pass"}
    assert redact_secrets(rec) == rec


# ---------------------------------------------------------------------------
# Evidence records: unique, append-only, complete
# ---------------------------------------------------------------------------


def _evidence_kwargs(**over):
    kw = dict(
        run_kind="bug",
        bug_id="H1",
        fix_sha="02ee6d44" + "0" * 32,
        fix_parent_sha="701da565" + "a" * 32,
        prompt="write probes for the diff",
        raw_model_response='{"probes": []}',
        parsed_probes=[{"act": {"method": "GET", "path": "/x"}}],
        bundle_manifest={"skill_sha256": "abc"},
        skill_sha256="abc",
        fix_parent_provision={
            "status": "ready",
            "runtime_version": "v20.20.0",
            "lockfile_sha256": "11" * 32,
            "logs": ["build ok"],
        },
        fix_provision={
            "status": "ready",
            "runtime_version": "v20.20.0",
            "lockfile_sha256": "22" * 32,
            "logs": ["build ok"],
        },
        fix_parent_results=[_result(Verdict.FAIL, 0)],
        fix_results=[_result(Verdict.PASS, 0)],
        runner_log=["run started"],
    )
    kw.update(over)
    return kw


def test_evidence_record_has_all_required_fields(tmp_path):
    record = build_evidence_record(**_evidence_kwargs())
    for f in (
        "record_id",
        "timestamp",
        "record_type",
        "run_kind",
        "bug_id",
        "fix_sha",
        "fix_parent_sha",
        "prompt",
        "prompt_sha256",
        "raw_model_response",
        "parsed_probes",
        "parsed_probes_sha256",
        "bundle_manifest",
        "skill_sha256",
        "harness_version",
        "scoring_version",
        "fix_parent_provision",
        "fix_provision",
        "fix_parent_results",
        "fix_results",
        "runner_log",
        "verdict",
    ):
        assert f in record, f"missing field {f}"
    assert record["record_type"] == "evidence"
    assert record["verdict"] is None  # evidence is written before scoring
    # Per-probe results carry reasons.
    assert record["fix_parent_results"][0]["verdict"] == "fail"
    assert "reason" in record["fix_parent_results"][0]
    # Provisioning carries runtime versions and lock hashes.
    assert record["fix_parent_provision"]["runtime_version"] == "v20.20.0"
    assert record["fix_parent_provision"]["lockfile_sha256"] == "11" * 32


def test_evidence_record_no_skill(tmp_path):
    record = build_evidence_record(**_evidence_kwargs(skill_sha256=None))
    assert record["skill_sha256"] is None


def test_records_are_unique_and_append_only(tmp_path):
    r1 = build_evidence_record(**_evidence_kwargs())
    r2 = build_evidence_record(**_evidence_kwargs())
    assert r1["record_id"] != r2["record_id"]
    id1, path = write_evidence_record(r1, tmp_path)
    id2, _ = write_evidence_record(r2, tmp_path)
    assert id1 != id2
    records = read_records(tmp_path)
    assert len(records) == 2
    assert records[0]["record_id"] == id1
    assert records[1]["record_id"] == id2
    # Repeats never overwrite: the log has both lines.
    lines = (tmp_path / "runs.jsonl").read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 2
    assert json.loads(lines[0])["record_id"] == id1


def test_evidence_is_secret_redacted_on_write(tmp_path):
    record = build_evidence_record(
        **_evidence_kwargs(
            bundle_manifest={"ADMIN_KEY": "sk-admin-xyz"},
            fix_parent_results=[
                ProbeResult(
                    probe_index=0,
                    verdict=Verdict.PASS,
                    http_headers={"Authorization": "Bearer tok123"},
                )
            ],
        )
    )
    _, path = write_evidence_record(record, tmp_path)
    data = json.loads(path.read_text(encoding="utf-8").strip())
    assert data["bundle_manifest"]["ADMIN_KEY"] == "***REDACTED***"
    assert data["fix_parent_results"][0]["http_headers"]["Authorization"] == "***REDACTED***"


def test_score_record_references_evidence(tmp_path):
    ev = build_evidence_record(**_evidence_kwargs())
    ev_id, _ = write_evidence_record(ev, tmp_path)
    verdict = score_run_pair([_result(Verdict.FAIL, 0)], [_result(Verdict.PASS, 0)])
    score = build_score_record(
        evidence_record_id=ev_id,
        run_kind="bug",
        bug_id="H1",
        verdict=verdict,
    )
    score_id, _ = write_score_record(score, tmp_path)
    records = read_records(tmp_path)
    assert len(records) == 2
    assert records[0]["record_type"] == "evidence"
    assert records[1]["record_type"] == "score"
    assert records[1]["evidence_record_id"] == ev_id
    assert records[1]["verdict"] == "catch"
    assert records[1]["record_id"] == score_id
    assert records[1]["scoring_version"] == SCORING_VERSION_V2


def test_clean_diff_score_record(tmp_path):
    ev = build_evidence_record(
        **_evidence_kwargs(run_kind="clean", clean_id="a9bf20b", bug_id=None)
    )
    ev_id, _ = write_evidence_record(ev, tmp_path)
    alarm = score_clean_diff([_result(Verdict.PASS)])
    score = build_score_record(
        evidence_record_id=ev_id, run_kind="clean", clean_id="a9bf20b", false_alarm=alarm
    )
    write_score_record(score, tmp_path)
    records = read_records(tmp_path)
    assert records[1]["false_alarm"] is False
    assert records[1]["verdict"] is None
