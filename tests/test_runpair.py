"""Tests for the run pair, scoring, records, and secret redaction (WAL-58, WO-4)."""

import hashlib
import json
import subprocess

import pytest

from proofdeploy.executor import InconclusiveReason, ProbeResult, Verdict
from proofdeploy.runpair import (
    DEFAULT_SCORING_VERSION,
    SCORING_VERSION,
    EvidenceNotCommittedError,
    PairScore,
    RunPairVerdict,
    build_evidence_record,
    build_judgment_record,
    build_score_record,
    collect_secret_values,
    commit_records,
    evidence_commit_sha,
    read_records,
    redact_record,
    redact_secrets,
    score_clean_diff,
    score_run_pair,
    score_run_pair_detailed,
    write_evidence_record,
    write_judgment_record,
    write_score_record,
)


def _result(
    verdict: Verdict,
    index: int = 0,
    reason: InconclusiveReason | None = None,
) -> ProbeResult:
    return ProbeResult(probe_index=index, verdict=verdict, reason=reason)


def _git_repo(path):
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)
    (path / "README.md").write_text("x")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=path, check=True)
    return path


# ---------------------------------------------------------------------------
# Registered per-probe scoring (PR #38) — the only version
# ---------------------------------------------------------------------------


def test_v2_is_default_and_only_version():
    assert DEFAULT_SCORING_VERSION == SCORING_VERSION == "v2-per-probe"


def test_catch_one_discriminating_probe():
    parent = [_result(Verdict.FAIL, 0)]
    fix = [_result(Verdict.PASS, 0)]
    assert score_run_pair(parent, fix) == RunPairVerdict.CATCH
    detailed = score_run_pair_detailed(parent, fix)
    assert detailed == PairScore(RunPairVerdict.CATCH, [0])


def test_candidate_catch_lists_every_discriminating_probe():
    parent = [_result(Verdict.FAIL, 0), _result(Verdict.FAIL, 1), _result(Verdict.PASS, 2)]
    fix = [_result(Verdict.PASS, 0), _result(Verdict.PASS, 1), _result(Verdict.PASS, 2)]
    detailed = score_run_pair_detailed(parent, fix)
    assert detailed.verdict == RunPairVerdict.CATCH
    assert detailed.candidate_catch == [0, 1]


def test_sibling_inconclusive_does_not_cancel_catch():
    parent = [_result(Verdict.FAIL, 0), _result(Verdict.INCONCLUSIVE, 1)]
    fix = [_result(Verdict.PASS, 0), _result(Verdict.PASS, 1)]
    assert score_run_pair(parent, fix) == RunPairVerdict.CATCH


def test_sibling_fail_both_sides_does_not_cancel_catch():
    parent = [_result(Verdict.FAIL, 0), _result(Verdict.FAIL, 1)]
    fix = [_result(Verdict.PASS, 0), _result(Verdict.FAIL, 1)]
    assert score_run_pair(parent, fix) == RunPairVerdict.CATCH


def test_no_catch_both_pass():
    parent = [_result(Verdict.PASS, 0)]
    fix = [_result(Verdict.PASS, 0)]
    assert score_run_pair(parent, fix) == RunPairVerdict.NO_CATCH


def test_no_catch_both_fail():
    parent = [_result(Verdict.FAIL, 0)]
    fix = [_result(Verdict.FAIL, 0)]
    assert score_run_pair(parent, fix) == RunPairVerdict.NO_CATCH


def test_inconclusive_when_side_cannot_build():
    assert score_run_pair(None, [_result(Verdict.PASS)]) == RunPairVerdict.INCONCLUSIVE
    assert score_run_pair([_result(Verdict.FAIL)], None) == RunPairVerdict.INCONCLUSIVE
    assert score_run_pair([], [_result(Verdict.PASS)]) == RunPairVerdict.INCONCLUSIVE


def test_inconclusive_when_every_probe_unreachable_environment():
    # Registered text: "the target is unreachable for every probe".
    parent = [
        _result(Verdict.INCONCLUSIVE, 0, InconclusiveReason.ENVIRONMENT),
        _result(Verdict.INCONCLUSIVE, 1, InconclusiveReason.ENVIRONMENT),
    ]
    fix = [_result(Verdict.PASS, 0)]
    assert score_run_pair(parent, fix) == RunPairVerdict.INCONCLUSIVE
    parent = [_result(Verdict.FAIL, 0)]
    fix = [
        _result(Verdict.INCONCLUSIVE, 0, InconclusiveReason.ENVIRONMENT),
        _result(Verdict.INCONCLUSIVE, 1, InconclusiveReason.ENVIRONMENT),
    ]
    assert score_run_pair(parent, fix) == RunPairVerdict.INCONCLUSIVE


def test_all_probe_reason_inconclusive_is_miss_not_infrastructure():
    # Orchestrator repro: three schema-invalid probes (reason "probe")
    # must be a miss (NO_CATCH), not pair INCONCLUSIVE.
    parent = [_result(Verdict.INCONCLUSIVE, i, InconclusiveReason.PROBE) for i in range(3)]
    fix = [_result(Verdict.PASS, 0)]
    assert score_run_pair(parent, fix) == RunPairVerdict.NO_CATCH


def test_mixed_reasons_not_unreachable():
    # One environment + one probe reason: not "unreachable for every probe".
    parent = [
        _result(Verdict.INCONCLUSIVE, 0, InconclusiveReason.ENVIRONMENT),
        _result(Verdict.INCONCLUSIVE, 1, InconclusiveReason.PROBE),
    ]
    fix = [_result(Verdict.PASS, 0)]
    assert score_run_pair(parent, fix) == RunPairVerdict.NO_CATCH


def test_probes_matched_by_index():
    parent = [_result(Verdict.PASS, 0), _result(Verdict.FAIL, 1)]
    fix = [_result(Verdict.PASS, 0), _result(Verdict.PASS, 1)]
    detailed = score_run_pair_detailed(parent, fix)
    assert detailed.verdict == RunPairVerdict.CATCH
    assert detailed.candidate_catch == [1]


def test_unknown_version_raises():
    with pytest.raises(ValueError):
        score_run_pair([_result(Verdict.FAIL)], [_result(Verdict.PASS)], "v1-all-probes")
    with pytest.raises(ValueError):
        score_run_pair([_result(Verdict.FAIL)], [_result(Verdict.PASS)], "v9")


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
# Secret redaction by value
# ---------------------------------------------------------------------------


def test_collect_secret_values():
    vals = collect_secret_values(
        fixture={"ADMIN_KEY": "id:secret-abc", "BASE_URL": "http://x"},
        contract_env={"API_TOKEN": "tok-1"},
        captured={"TOKEN": "sess-999"},
        minted_tokens=["jwt-aaa"],
    )
    assert "id:secret-abc" in vals
    assert "tok-1" in vals
    assert "sess-999" in vals
    assert "jwt-aaa" in vals
    assert "http://x" not in vals  # not a secret key
    # short values skipped
    vals2 = collect_secret_values(captured={"A": "x"})
    assert vals2 == []


def test_redact_by_value_in_free_text():
    # Orchestrator repro: Ghost ADMIN_KEY "id:secret" leaked in prompt
    # and runner_log. Value-based redaction catches it.
    rec = {
        "prompt": "use key id:secret-abc to log in",
        "runner_log": ["login with id:secret-abc ok"],
    }
    out = redact_secrets(rec, ["id:secret-abc"])
    assert "id:secret-abc" not in json.dumps(out)
    assert out["prompt"] == "use key ***REDACTED*** to log in"


def test_redact_patterns_still_backstop():
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


def test_parsed_probes_stored_verbatim():
    # Orchestrator repro: placeholders like "Bearer ${USER_TOKEN}" and
    # capture specs were redacted, breaking replay. parsed_probes is
    # structure, never redacted.
    probes = [
        {
            "setup": [
                {
                    "type": "http",
                    "act": {"method": "POST", "path": "/login"},
                    "capture": {"TOKEN": {"from": "body", "json_path": "token"}},
                }
            ],
            "act": {
                "method": "GET",
                "path": "/admin",
                "headers": {"Authorization": "Bearer ${USER_TOKEN}"},
            },
            "assert": [{"type": "status", "equals": 401}],
        }
    ]
    record = build_evidence_record(
        run_kind="bug",
        bug_id="X",
        parsed_probes=probes,
        fix_sha="a" * 40,
        fix_parent_sha="b" * 40,
    )
    out = redact_record(record, ["real-secret-value"])
    assert out["parsed_probes"] == probes
    # The stored hash still matches the stored probes after redaction.
    h = hashlib.sha256(
        json.dumps(out["parsed_probes"], sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert h == out["parsed_probes_sha256"]


def test_redact_record_keeps_probes_but_redacts_rest():
    record = {
        "parsed_probes": [{"act": {"headers": {"Authorization": "Bearer ${T}"}}}],
        "prompt": "key is s3cr3t-val",
    }
    out = redact_record(record, ["s3cr3t-val"])
    assert out["parsed_probes"][0]["act"]["headers"]["Authorization"] == "Bearer ${T}"
    assert out["prompt"] == "key is ***REDACTED***"


# ---------------------------------------------------------------------------
# Evidence records: unique, append-only, commit-enforced
# ---------------------------------------------------------------------------


def _evidence_kwargs(**over):
    kw = dict(
        run_kind="bug",
        bug_id="THROWAWAY-1",
        fix_sha="a" * 40,
        fix_parent_sha="b" * 40,
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


def test_evidence_record_has_all_required_fields():
    record = build_evidence_record(**_evidence_kwargs())
    for f in (
        "record_id",
        "timestamp",
        "record_type",
        "run_kind",
        "bug_id",
        "fix_sha",
        "fix_parent_sha",
        "c_sha",
        "c_provision",
        "c_results",
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
    assert record["fix_parent_results"][0]["verdict"] == "fail"
    assert "reason" in record["fix_parent_results"][0]
    assert "request" in record["fix_parent_results"][0]  # replayability
    assert record["fix_parent_provision"]["runtime_version"] == "v20.20.0"


def test_clean_diff_uses_explicit_c_fields():
    record = build_evidence_record(
        run_kind="clean",
        clean_id="c1",
        c_sha="c" * 40,
        c_provision={"status": "ready"},
        c_results=[_result(Verdict.PASS, 0)],
    )
    assert record["c_sha"] == "c" * 40
    assert record["c_provision"] == {"status": "ready"}
    assert record["c_results"][0]["verdict"] == "pass"
    assert record["fix_sha"] is None
    assert record["fix_results"] == []


def test_evidence_write_commits_and_score_requires_it(tmp_path):
    repo = _git_repo(tmp_path)
    out = tmp_path / "runs"
    record = build_evidence_record(**_evidence_kwargs())
    ev_id, path, commit_sha = write_evidence_record(record, out, repo, ["s3cr3t"])
    assert evidence_commit_sha(ev_id, repo, out) == commit_sha
    # Score refuses without committed evidence.
    with pytest.raises(EvidenceNotCommittedError):
        write_score_record(
            build_score_record(evidence_record_id="nope", run_kind="bug"),
            out,
            repo,
            evidence_record_id="nope",
        )
    # With committed evidence it writes and cites the commit.
    score = build_score_record(
        evidence_record_id=ev_id,
        run_kind="bug",
        bug_id="THROWAWAY-1",
        verdict=RunPairVerdict.CATCH,
        candidate_catch=[0],
    )
    score_id, _ = write_score_record(score, out, repo, evidence_record_id=ev_id)
    records = read_records(out)
    assert records[1]["evidence_commit_sha"] == commit_sha
    assert records[1]["candidate_catch"] == [0]
    assert records[1]["record_id"] == score_id
    # Score goes in a LATER commit.
    score_commit = commit_records(repo, out, f"score: {score_id}")
    assert score_commit != commit_sha
    assert evidence_commit_sha(ev_id, repo, out) == commit_sha  # evidence commit unchanged


def test_records_are_unique_and_append_only(tmp_path):
    repo = _git_repo(tmp_path)
    out = tmp_path / "runs"
    r1 = build_evidence_record(**_evidence_kwargs())
    r2 = build_evidence_record(**_evidence_kwargs())
    assert r1["record_id"] != r2["record_id"]
    id1, _, _ = write_evidence_record(r1, out, repo)
    id2, _, _ = write_evidence_record(r2, out, repo)
    records = read_records(out)
    assert [r["record_id"] for r in records] == [id1, id2]


def test_evidence_is_secret_redacted_on_write(tmp_path):
    repo = _git_repo(tmp_path)
    out = tmp_path / "runs"
    record = build_evidence_record(
        **_evidence_kwargs(
            bundle_manifest={"ADMIN_KEY": "sk-admin-xyz"},
            prompt="login with sk-admin-xyz",
            fix_parent_results=[
                ProbeResult(
                    probe_index=0,
                    verdict=Verdict.PASS,
                    http_headers={"Authorization": "Bearer tok123"},
                    request={
                        "method": "GET",
                        "url": "http://x/admin",
                        "headers": {"Authorization": "Bearer tok123"},
                        "body": None,
                    },
                )
            ],
        )
    )
    _, path, _ = write_evidence_record(record, out, repo, ["sk-admin-xyz", "tok123"])
    data = json.loads(path.read_text(encoding="utf-8").strip().split("\n")[0])
    assert data["bundle_manifest"]["ADMIN_KEY"] == "***REDACTED***"
    assert "sk-admin-xyz" not in data["prompt"]
    res = data["fix_parent_results"][0]
    assert "tok123" not in json.dumps(res["request"])


def test_judgment_record_shape(tmp_path):
    repo = _git_repo(tmp_path)
    out = tmp_path / "runs"
    ev = build_evidence_record(**_evidence_kwargs())
    ev_id, _, ev_commit = write_evidence_record(ev, out, repo)
    score = build_score_record(
        evidence_record_id=ev_id,
        run_kind="bug",
        bug_id="THROWAWAY-1",
        verdict=RunPairVerdict.CATCH,
        candidate_catch=[0],
    )
    score_id, _ = write_score_record(score, out, repo, evidence_record_id=ev_id)
    judgment = build_judgment_record(
        evidence_record_id=ev_id,
        score_record_id=score_id,
        evidence_commit_sha=ev_commit,
        run_kind="bug",
        bug_id="THROWAWAY-1",
        probe_index=0,
        judge="reviewer",
        condition3_met=True,
        note="probe targets the auth check the fix added",
    )
    j_id, _ = write_judgment_record(judgment, out)
    records = read_records(out)
    assert records[-1]["record_type"] == "judgment"
    assert records[-1]["record_id"] == j_id
    assert records[-1]["verdict"] == "catch"
    assert records[-1]["condition3_met"] is True
    assert records[-1]["probe_index"] == 0
    assert records[-1]["evidence_commit_sha"] == ev_commit
    # A rejected judgment is no_catch.
    j2 = build_judgment_record(
        evidence_record_id=ev_id,
        score_record_id=score_id,
        evidence_commit_sha=ev_commit,
        run_kind="bug",
        bug_id="B",
        probe_index=0,
        judge="reviewer",
        condition3_met=False,
    )
    assert j2["verdict"] == "no_catch"


def test_clean_diff_score_record(tmp_path):
    repo = _git_repo(tmp_path)
    out = tmp_path / "runs"
    ev = build_evidence_record(
        run_kind="clean",
        clean_id="c1",
        c_sha="c" * 40,
        c_provision={"status": "ready"},
        c_results=[_result(Verdict.PASS)],
    )
    ev_id, _, _ = write_evidence_record(ev, out, repo)
    alarm = score_clean_diff([_result(Verdict.PASS)])
    score = build_score_record(
        evidence_record_id=ev_id, run_kind="clean", clean_id="c1", false_alarm=alarm
    )
    write_score_record(score, out, repo, evidence_record_id=ev_id)
    records = read_records(out)
    assert records[1]["false_alarm"] is False
    assert records[1]["verdict"] is None
