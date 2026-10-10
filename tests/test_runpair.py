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
    JudgmentRejectedError,
    PairScore,
    RunPairVerdict,
    ScoreMismatchError,
    build_evidence_record,
    build_judgment_record,
    collect_record_secret_values,
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
        "fixture",
        "contract_env",
        "public_fixture_keys",
        "public_contract_env_keys",
        "captured_values",
        "minted_tokens",
        "verdict",
    ):
        assert f in record, f"missing field {f}"
    assert record["record_type"] == "evidence"
    assert record["verdict"] is None  # evidence is written before scoring
    assert record["fix_parent_results"][0]["verdict"] == "fail"
    assert "reason" in record["fix_parent_results"][0]
    assert "request" in record["fix_parent_results"][0]  # replayability
    assert "setup_requests" in record["fix_parent_results"][0]  # setup replayability
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


def _write_evidence(tmp_path, **over):
    """Write an evidence record to a fresh git repo; return (repo, out, ev_id, commit)."""
    repo = _git_repo(tmp_path)
    out = tmp_path / "runs"
    record = build_evidence_record(**_evidence_kwargs(**over))
    ev_id, path, commit_sha = write_evidence_record(record, out, repo)
    return repo, out, ev_id, commit_sha


def test_evidence_write_commits_and_score_requires_it(tmp_path):
    repo, out, ev_id, commit_sha = _write_evidence(tmp_path)
    assert evidence_commit_sha(ev_id, repo, out) == commit_sha
    # Score refuses without committed evidence.
    with pytest.raises(EvidenceNotCommittedError):
        write_score_record(out, repo, evidence_record_id="nope", run_kind="bug")
    # With committed evidence the score is COMPUTED from it: CATCH on
    # probe 0, stored as "candidate_catch" (not "catch").
    score_id, _ = write_score_record(out, repo, evidence_record_id=ev_id, run_kind="bug")
    records = read_records(out)
    assert records[1]["record_type"] == "score"
    assert records[1]["evidence_commit_sha"] == commit_sha
    assert records[1]["verdict"] == "candidate_catch"
    assert records[1]["candidate_catch"] == [0]
    assert records[1]["record_id"] == score_id
    # Score goes in a LATER commit.
    score_commit = commit_records(repo, out, f"score: {score_id}")
    assert score_commit != commit_sha
    assert evidence_commit_sha(ev_id, repo, out) == commit_sha  # evidence commit unchanged


def test_score_refuses_claims_that_disagree_with_evidence(tmp_path):
    # The orchestrator's repro: a CATCH on probe 7 was accepted for
    # evidence with zero probes. Now the score is computed from the
    # committed evidence; any disagreeing claim is refused.
    repo, out, ev_id, _ = _write_evidence(tmp_path)
    with pytest.raises(ScoreMismatchError):
        write_score_record(
            out,
            repo,
            evidence_record_id=ev_id,
            run_kind="bug",
            verdict=RunPairVerdict.NO_CATCH,  # evidence says CATCH
        )
    with pytest.raises(ScoreMismatchError):
        write_score_record(
            out,
            repo,
            evidence_record_id=ev_id,
            run_kind="bug",
            candidate_catch=[7],  # evidence says [0]
        )
    with pytest.raises(ScoreMismatchError):
        write_score_record(
            out,
            repo,
            evidence_record_id=ev_id,
            run_kind="bug",
            bug_id="OTHER",  # evidence says THROWAWAY-1
        )
    # Correct claims pass.
    score_id, _ = write_score_record(
        out,
        repo,
        evidence_record_id=ev_id,
        run_kind="bug",
        bug_id="THROWAWAY-1",
        verdict=RunPairVerdict.CATCH,
        candidate_catch=[0],
    )
    assert read_records(out)[1]["record_id"] == score_id


def test_score_computed_from_empty_evidence_is_inconclusive_not_catch(tmp_path):
    repo = _git_repo(tmp_path)
    out = tmp_path / "runs"
    record = build_evidence_record(**_evidence_kwargs(fix_parent_results=[], fix_results=[]))
    ev_id, _, _ = write_evidence_record(record, out, repo)
    # A CATCH claim on empty evidence is refused; the computed verdict
    # is INCONCLUSIVE.
    with pytest.raises(ScoreMismatchError):
        write_score_record(
            out,
            repo,
            evidence_record_id=ev_id,
            run_kind="bug",
            verdict=RunPairVerdict.CATCH,
            candidate_catch=[7],
        )
    score_id, _ = write_score_record(out, repo, evidence_record_id=ev_id, run_kind="bug")
    assert read_records(out)[1]["verdict"] == "inconclusive"


def test_score_verdict_is_candidate_catch_not_catch(tmp_path):
    # Before judgment, the score record says "candidate_catch"; only the
    # judgment record can say "catch".
    repo, out, ev_id, ev_commit = _write_evidence(tmp_path)
    score_id, _ = write_score_record(out, repo, evidence_record_id=ev_id, run_kind="bug")
    assert read_records(out)[1]["verdict"] == "candidate_catch"
    assert "catch" != read_records(out)[1]["verdict"]
    # The judgment turns it into a catch.
    judgment = build_judgment_record(
        evidence_record_id=ev_id,
        score_record_id=score_id,
        evidence_commit_sha=ev_commit,
        run_kind="bug",
        bug_id="THROWAWAY-1",
        probe_index=0,
        judge="reviewer",
        judge_role="reviewer",
        condition3_met=True,
    )
    j_id, _ = write_judgment_record(judgment, out, repo, score_record_id=score_id)
    assert read_records(out)[-1]["record_id"] == j_id
    assert read_records(out)[-1]["verdict"] == "catch"


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


# ---------------------------------------------------------------------------
# Item 1: fail-closed redaction
# ---------------------------------------------------------------------------


def test_fail_closed_redaction_collects_plain_named_keys(tmp_path):
    # The orchestrator's repro: STAFF_KEY and DIRECTUS_STATIC leaked
    # because redaction only caught secret-looking names. Now every
    # fixture/contract-env value is secret unless declared public.
    repo = _git_repo(tmp_path)
    out = tmp_path / "runs"
    record = build_evidence_record(
        **_evidence_kwargs(
            fixture={
                "STAFF_KEY": "staff-secret-value-1",
                "DIRECTUS_STATIC": "directus-token-9",
                "APP_NAME": "throwaway",
            },
            contract_env={"API_URL": "https://example.invalid"},
            public_fixture_keys=["APP_NAME"],
            public_contract_env_keys=["API_URL"],
            prompt="login with staff-secret-value-1 and directus-token-9",
            runner_log=["using throwaway", "key directus-token-9 used"],
        )
    )
    _, path, _ = write_evidence_record(record, out, repo)  # no caller secret_values
    data = json.loads(path.read_text(encoding="utf-8").strip().split("\n")[0])
    blob = json.dumps(data)
    assert "staff-secret-value-1" not in blob
    assert "directus-token-9" not in blob
    # Declared-public values are untouched.
    assert data["contract_env"]["API_URL"] == "https://example.invalid"
    # Non-secret fixture keys stay readable too.
    assert data["fixture"]["APP_NAME"] == "throwaway"
    assert "throwaway" in data["runner_log"][0]


def test_captured_values_and_minted_tokens_are_collected(tmp_path):
    repo = _git_repo(tmp_path)
    out = tmp_path / "runs"
    record = build_evidence_record(
        **_evidence_kwargs(
            captured_values={"TOKEN": "captured-session-abc"},
            minted_tokens=["minted-jwt-value"],
            prompt="token captured-session-abc used; minted minted-jwt-value",
        )
    )
    _, path, _ = write_evidence_record(record, out, repo)
    blob = path.read_text(encoding="utf-8")
    assert "captured-session-abc" not in blob
    assert "minted-jwt-value" not in blob


def test_collect_record_secret_values_fail_closed():
    record = {
        "fixture": {"STAFF_KEY": "staff-one", "NAME": "myapp", "TTL": 300},
        "public_fixture_keys": ["NAME"],
        "contract_env": {"DIRECTUS_STATIC": "directus-two"},
        "public_contract_env_keys": [],
        "captured_values": {"T": "captured-three"},
        "minted_tokens": ["minted-four"],
    }
    vals = collect_record_secret_values(record)
    assert "staff-one" in vals
    assert "directus-two" in vals
    assert "captured-three" in vals
    assert "minted-four" in vals
    assert "myapp" not in vals  # declared public
    # non-string TTL is not collected
    assert not any("300" == v for v in vals)


def test_probes_stored_verbatim_placeholders_not_redacted(tmp_path):
    # Placeholders and capture specs are structure, not secrets: the
    # stored parsed_probes must still hash to parsed_probes_sha256.
    repo = _git_repo(tmp_path)
    out = tmp_path / "runs"
    probes = [
        {
            "setup": [
                {
                    "type": "http",
                    "act": {"method": "GET", "path": "/login"},
                    "capture": {"TOKEN": {"from": "body", "json_path": "token"}},
                }
            ],
            "act": {
                "method": "GET",
                "path": "/admin",
                "headers": {"Authorization": "Bearer ${TOKEN}"},
            },
            "assert": [{"type": "status", "equals": 200}],
        }
    ]
    record = build_evidence_record(
        **_evidence_kwargs(
            parsed_probes=probes,
            fixture={"ADMIN_KEY": "admin-secret"},
        )
    )
    _, path, _ = write_evidence_record(record, out, repo)
    data = json.loads(path.read_text(encoding="utf-8").strip().split("\n")[0])
    assert data["parsed_probes"] == probes
    import hashlib as _hl

    assert (
        _hl.sha256(json.dumps(probes, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        == data["parsed_probes_sha256"]
    )


def test_authorization_header_keeps_scheme_when_redacted(tmp_path):
    # "Bearer ***REDACTED***": the scheme is structure, the credential
    # is secret.
    repo = _git_repo(tmp_path)
    out = tmp_path / "runs"
    record = build_evidence_record(
        **_evidence_kwargs(
            fixture={"ADMIN_KEY": "admin-secret-value"},
            fix_results=[
                ProbeResult(
                    probe_index=0,
                    verdict=Verdict.PASS,
                    request={
                        "method": "GET",
                        "url": "http://x/admin",
                        "headers": {"Authorization": "Bearer admin-secret-value"},
                        "body": None,
                    },
                )
            ],
        )
    )
    _, path, _ = write_evidence_record(record, out, repo)
    data = json.loads(path.read_text(encoding="utf-8").strip().split("\n")[0])
    auth = data["fix_results"][0]["request"]["headers"]["Authorization"]
    assert auth == "Bearer ***REDACTED***"
    assert "admin-secret-value" not in json.dumps(data)


def test_jwt_pattern_backstop(tmp_path):
    # A JWT-shaped string the collector never saw is still redacted by
    # the pattern backstop.
    from proofdeploy.runpair import redact_secrets

    red = redact_secrets(
        {"details": ["minted eyJhbGciOiJIUzI1NiJ9.eyJleHAiOjEyM30.c2ln for setup"]},
        [],
    )
    assert "eyJhbGci" not in red["details"][0]
    assert "***REDACTED_JWT***" in red["details"][0]


# ---------------------------------------------------------------------------
# Item 4: judgment validation
# ---------------------------------------------------------------------------


def _bug_score(tmp_path):
    """Write evidence + score for a bug run; return (repo, out, ev_id, ev_commit, score_id)."""
    repo, out, ev_id, ev_commit = _write_evidence(tmp_path)
    score_id, _ = write_score_record(out, repo, evidence_record_id=ev_id, run_kind="bug")
    return repo, out, ev_id, ev_commit, score_id


def test_judgment_record_shape(tmp_path):
    repo, out, ev_id, ev_commit, score_id = _bug_score(tmp_path)
    judgment = build_judgment_record(
        evidence_record_id=ev_id,
        score_record_id=score_id,
        evidence_commit_sha=ev_commit,
        run_kind="bug",
        bug_id="THROWAWAY-1",
        probe_index=0,
        judge="reviewer",
        judge_role="reviewer",
        condition3_met=True,
        note="probe targets the auth check the fix added",
    )
    j_id, _ = write_judgment_record(judgment, out, repo, score_record_id=score_id)
    records = read_records(out)
    assert records[-1]["record_type"] == "judgment"
    assert records[-1]["record_id"] == j_id
    assert records[-1]["verdict"] == "catch"
    assert records[-1]["condition3_met"] is True
    assert records[-1]["probe_index"] == 0
    assert records[-1]["judge_role"] == "reviewer"
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
        judge_role="reviewer",
        condition3_met=False,
    )
    assert j2["verdict"] == "no_catch"


def test_judgment_refuses_probe_not_in_candidate_catch(tmp_path):
    # The orchestrator's repro: probe 99 was accepted. Now the probe
    # must be in the score's candidate_catch.
    repo, out, ev_id, ev_commit, score_id = _bug_score(tmp_path)
    judgment = build_judgment_record(
        evidence_record_id=ev_id,
        score_record_id=score_id,
        evidence_commit_sha=ev_commit,
        run_kind="bug",
        probe_index=99,
        judge="reviewer",
        judge_role="reviewer",
        condition3_met=True,
    )
    with pytest.raises(JudgmentRejectedError):
        write_judgment_record(judgment, out, repo, score_record_id=score_id)


def test_judgment_requires_boolean_condition3(tmp_path):
    # The orchestrator's repro: condition3_met=None was accepted.
    repo, out, ev_id, ev_commit, score_id = _bug_score(tmp_path)
    with pytest.raises(JudgmentRejectedError):
        build_judgment_record(
            evidence_record_id=ev_id,
            score_record_id=score_id,
            evidence_commit_sha=ev_commit,
            run_kind="bug",
            probe_index=0,
            judge="reviewer",
            judge_role="reviewer",
            condition3_met=None,
        )
    # ...and a hand-built record with None is refused at write time too.
    bad = {
        "record_id": "x",
        "record_type": "judgment",
        "run_kind": "bug",
        "probe_index": 0,
        "judge": "reviewer",
        "judge_role": "orchestrator",
        "condition3_met": None,
        "verdict": "no_catch",
    }
    with pytest.raises(JudgmentRejectedError):
        write_judgment_record(bad, out, repo, score_record_id=score_id)


def test_judgment_role_allowlist_and_reviewer_only_on_bug_runs(tmp_path):
    # The orchestrator's repro: judge="Wally" with role "reviewer", and
    # judge="wally-dk24" with role "implementer", were both accepted on a
    # bug run. Now the role is allowlisted and bug runs require
    # role "reviewer" with a non-empty judge.
    repo, out, ev_id, ev_commit, score_id = _bug_score(tmp_path)
    base = dict(
        evidence_record_id=ev_id,
        score_record_id=score_id,
        evidence_commit_sha=ev_commit,
        run_kind="bug",
        probe_index=0,
        condition3_met=True,
    )
    # Unknown role refused at build time.
    with pytest.raises(JudgmentRejectedError):
        build_judgment_record(**base, judge="reviewer", judge_role="builder")
    with pytest.raises(JudgmentRejectedError):
        build_judgment_record(**base, judge="reviewer", judge_role=None)
    # "Wally" with role reviewer passes the allowlist: the gate is on
    # the role, not the name. The old name-based block ("wally" only)
    # was fragile; the role allowlist is the enforcement.
    wally_caps = build_judgment_record(**base, judge="Wally", judge_role="reviewer")
    j_id, _ = write_judgment_record(wally_caps, out, repo, score_record_id=score_id)
    assert read_records(out)[-1]["record_id"] == j_id
    # implementer role refused on bug runs.
    impl = build_judgment_record(**base, judge="wally-dk24", judge_role="implementer")
    with pytest.raises(JudgmentRejectedError):
        write_judgment_record(impl, out, repo, score_record_id=score_id)
    # Empty judge refused on bug runs.
    anon = build_judgment_record(**base, judge="", judge_role="reviewer")
    with pytest.raises(JudgmentRejectedError):
        write_judgment_record(anon, out, repo, score_record_id=score_id)
    # A named reviewer passes.
    ok = build_judgment_record(**base, judge="reviewer", judge_role="reviewer")
    j2_id, _ = write_judgment_record(ok, out, repo, score_record_id=score_id)
    assert read_records(out)[-1]["record_id"] == j2_id


def test_judgment_refuses_evidence_mismatch(tmp_path):
    # A judgment whose evidence id or commit differs from the score's
    # is refused.
    repo, out, ev_id, ev_commit, score_id = _bug_score(tmp_path)
    base = dict(
        score_record_id=score_id,
        run_kind="bug",
        probe_index=0,
        judge="reviewer",
        judge_role="reviewer",
        condition3_met=True,
    )
    wrong_id = build_judgment_record(
        **base, evidence_record_id="0" * 32, evidence_commit_sha=ev_commit
    )
    with pytest.raises(JudgmentRejectedError):
        write_judgment_record(wrong_id, out, repo, score_record_id=score_id)
    wrong_commit = build_judgment_record(
        **base, evidence_record_id=ev_id, evidence_commit_sha="f" * 40
    )
    with pytest.raises(JudgmentRejectedError):
        write_judgment_record(wrong_commit, out, repo, score_record_id=score_id)


def test_judgment_refuses_missing_score_record(tmp_path):
    repo = _git_repo(tmp_path)
    out = tmp_path / "runs"
    judgment = build_judgment_record(
        evidence_record_id="ev",
        score_record_id="nope",
        evidence_commit_sha="c" * 40,
        run_kind="bug",
        probe_index=0,
        judge="reviewer",
        judge_role="reviewer",
        condition3_met=True,
    )
    with pytest.raises(JudgmentRejectedError):
        write_judgment_record(judgment, out, repo, score_record_id="nope")


def test_clean_diff_score_record(tmp_path):
    repo = _git_repo(tmp_path)
    out = tmp_path / "runs"
    ev = build_evidence_record(
        run_kind="clean",
        clean_id="c1",
        c_sha="c" * 40,
        c_provision={"status": "ready"},
        c_results=[ProbeResult(probe_index=0, verdict=Verdict.PASS)],
    )
    ev_id, _, _ = write_evidence_record(ev, out, repo)
    # Computed from the committed evidence: no false alarm.
    score_id, _ = write_score_record(out, repo, evidence_record_id=ev_id, run_kind="clean")
    records = read_records(out)
    assert records[1]["record_id"] == score_id
    assert records[1]["false_alarm"] is False
    assert records[1]["verdict"] is None
    # A disagreeing claim is refused.
    with pytest.raises(ScoreMismatchError):
        write_score_record(out, repo, evidence_record_id=ev_id, run_kind="clean", false_alarm=True)


def test_clean_diff_false_alarm_computed(tmp_path):
    repo = _git_repo(tmp_path)
    out = tmp_path / "runs"
    ev = build_evidence_record(
        run_kind="clean",
        clean_id="c2",
        c_sha="c" * 40,
        c_provision={"status": "ready"},
        c_results=[ProbeResult(probe_index=0, verdict=Verdict.FAIL)],
    )
    ev_id, _, _ = write_evidence_record(ev, out, repo)
    _, _ = write_score_record(out, repo, evidence_record_id=ev_id, run_kind="clean")
    assert read_records(out)[1]["false_alarm"] is True


def test_probe_result_round_trip_preserves_verdict_and_reason():
    r = ProbeResult(
        probe_index=3,
        verdict=Verdict.INCONCLUSIVE,
        reason=InconclusiveReason.ENVIRONMENT,
        details=["x"],
        http_status=500,
        request={"method": "GET", "url": "http://x/", "headers": {}, "body": None},
        setup_requests=[{"method": "POST", "url": "http://x/login", "headers": {}, "body": {}}],
    )
    r2 = ProbeResult.from_dict(r.to_dict())
    assert r2.probe_index == 3
    assert r2.verdict == Verdict.INCONCLUSIVE
    assert r2.reason == InconclusiveReason.ENVIRONMENT
    assert r2.request["url"] == "http://x/"
    assert r2.setup_requests[0]["method"] == "POST"


def test_score_reads_evidence_at_commit_not_working_tree(tmp_path):
    # The score is computed from the evidence AT the commit, so a
    # working-tree mutation after the commit cannot change the score.
    repo, out, ev_id, _ = _write_evidence(tmp_path)
    # Mutate the working tree: append a tampered evidence-looking line.
    log = out / "runs.jsonl"
    with log.open("a", encoding="utf-8") as f:
        f.write('{"record_id":"tamper","record_type":"evidence"}\n')
    score_id, _ = write_score_record(out, repo, evidence_record_id=ev_id, run_kind="bug")
    records = read_records(out)
    score = next(r for r in records if r.get("record_id") == score_id)
    assert score["candidate_catch"] == [0]
    assert score["verdict"] == "candidate_catch"


# ---------------------------------------------------------------------------
# Executor hands its secrets to the record (WO-4 third review, item 1)
# ---------------------------------------------------------------------------


def test_executor_attaches_captured_and_minted_secrets_to_result():
    # The executor's ProbeResult carries the run's captured bindings and
    # minted tokens (never serialized). build_evidence_record collects
    # them from the results, so a caller that forgets captured_values
    # cannot leak a session token.
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    TOKEN = "session-token-live-xyz-789"

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            assert self.path == "/login"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"token": TOKEN}).encode())

        def do_GET(self):
            assert self.path == "/me"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            # Echo the captured token in the response body (the leak
            # scenario: the token appears in http_body).
            self.wfile.write(json.dumps({"echo": TOKEN}).encode())

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        from proofdeploy.executor import Executor

        ex = Executor(target_url=f"http://127.0.0.1:{port}", provisioned=True)
        probe = {
            "setup": [
                {
                    "type": "http",
                    "act": {"method": "POST", "path": "/login"},
                    "capture": {"TOKEN": {"from": "body", "json_path": "token"}},
                }
            ],
            "act": {"method": "GET", "path": "/me"},
            "assert": [{"type": "status", "equals": 200}],
        }
        r = ex.run_probe(probe, 0)
        assert r.verdict == Verdict.PASS
        # The executor attached the secrets to the result...
        assert r.captured_bindings.get("TOKEN") == TOKEN
        # ...and they are never serialized.
        assert "captured_bindings" not in r.to_dict()
        assert "minted_tokens" not in r.to_dict()
    finally:
        srv.shutdown()


def test_evidence_auto_collects_secrets_from_results_no_caller_values(tmp_path):
    # The orchestrator's repro: a session token captured in setup and
    # echoed in the act's response body leaked when the caller didn't
    # pass captured_values. Now build_evidence_record collects from the
    # results automatically.
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    from proofdeploy.executor import Executor

    TOKEN = "session-token-live-xyz-789"

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"token": TOKEN}).encode())

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"echo": TOKEN}).encode())

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        ex = Executor(target_url=f"http://127.0.0.1:{port}", provisioned=True)
        probe = {
            "setup": [
                {
                    "type": "http",
                    "act": {"method": "POST", "path": "/login"},
                    "capture": {"TOKEN": {"from": "body", "json_path": "token"}},
                }
            ],
            "act": {"method": "GET", "path": "/me"},
            "assert": [{"type": "status", "equals": 200}],
        }
        r = ex.run_probe(probe, 0)
        assert r.verdict == Verdict.PASS
        assert TOKEN in r.http_body  # the leak scenario: token in body

        repo = _git_repo(tmp_path)
        out = tmp_path / "runs"
        # NO captured_values / minted_tokens passed by the caller.
        record = build_evidence_record(**_evidence_kwargs(fix_parent_results=[r], fix_results=[r]))
        _, path, _ = write_evidence_record(record, out, repo)
        blob = path.read_text(encoding="utf-8")
        assert TOKEN not in blob, "captured session token leaked into the record"
    finally:
        srv.shutdown()


def test_evidence_record_carries_registered_model_config():
    # WO-5 brief: "The model and its settings are recorded." The pinned
    # model client config must land in the evidence record and survive
    # the secret redaction applied before the record is committed.
    record = build_evidence_record(
        run_kind="bug",
        bug_id="X",
        fix_sha="a" * 40,
        fix_parent_sha="b" * 40,
        model_id="openai/gpt-oss-120b",
        model_temperature=0.2,
        model_attempts=1,
    )
    assert record["model_id"] == "openai/gpt-oss-120b"
    assert record["model_temperature"] == 0.2
    assert record["model_attempts"] == 1
    out = redact_record(record, [])
    assert out["model_id"] == "openai/gpt-oss-120b"
    assert out["model_temperature"] == 0.2
    assert out["model_attempts"] == 1
