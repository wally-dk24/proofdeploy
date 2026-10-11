"""Addendum 21 (B1): numeric captures must keep the redaction guarantee.

E2 (Addendum 20) made captures keep their JSON type, but the secret
collectors only collected string captures, so an int/float capture (a
numeric session id, an OTP, a numeric API key) leaked into evidence
records in plaintext. These tests pin the fix: both collectors treat
non-bool int/float captures as secrets in their text form (subject to
``_MIN_SECRET_LEN``, exactly like strings), bools and short values stay
uncollected, and the written record contains no plaintext form of a
numeric capture anywhere — including the ``captured_values`` leaf
itself, which becomes the redaction marker.
"""

import json
import subprocess

from proofdeploy.executor import ProbeResult, Verdict
from proofdeploy.runpair import (
    REDACTED,
    build_evidence_record,
    collect_record_secret_values,
    collect_secret_values,
    write_evidence_record,
)

INT_SECRET = 987654321012
INT_SECRET_TEXT = "987654321012"
FLOAT_SECRET = 123456789.25
FLOAT_SECRET_TEXT = "123456789.25"  # json.dumps(FLOAT_SECRET)


def _git_repo(path):
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)
    (path / "README.md").write_text("x")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=path, check=True)
    return path


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
        fix_parent_results=[ProbeResult(probe_index=0, verdict=Verdict.FAIL)],
        fix_results=[ProbeResult(probe_index=0, verdict=Verdict.PASS)],
        runner_log=["run started"],
    )
    kw.update(over)
    return kw


def _write_record(tmp_path, **over):
    repo = _git_repo(tmp_path)
    out = tmp_path / "runs"
    record = build_evidence_record(**_evidence_kwargs(**over))
    _, path, _ = write_evidence_record(record, out, repo)
    blob = path.read_text(encoding="utf-8")
    data = json.loads(blob.strip().splitlines()[-1])
    return record, blob, data


# ---------------------------------------------------------------------------
# (a) int capture echoed in a response body: redacted in the written record
# ---------------------------------------------------------------------------


def test_int_capture_echoed_in_body_is_redacted_in_written_record(tmp_path):
    echoed = ProbeResult(
        probe_index=0,
        verdict=Verdict.PASS,
        http_status=200,
        http_body='{"session": 987654321012, "ok": true}',
    )
    record, blob, data = _write_record(
        tmp_path,
        fix_results=[echoed],
        captured_values={"SESSION": INT_SECRET},
    )
    # No plaintext form anywhere in the written record.
    assert INT_SECRET_TEXT not in blob
    # The captured_values leaf itself becomes the redaction marker.
    assert data["captured_values"]["SESSION"] == REDACTED
    # parsed_probes stays verbatim; its hash still matches.
    assert data["parsed_probes"] == record["parsed_probes"]
    assert data["parsed_probes_sha256"] == record["parsed_probes_sha256"]


# ---------------------------------------------------------------------------
# (b) float capture echoed in a response body: redacted in the written record
# ---------------------------------------------------------------------------


def test_float_capture_echoed_in_body_is_redacted_in_written_record(tmp_path):
    echoed = ProbeResult(
        probe_index=0,
        verdict=Verdict.PASS,
        http_status=200,
        http_body='{"code": 123456789.25, "ok": true}',
    )
    record, blob, data = _write_record(
        tmp_path,
        fix_results=[echoed],
        captured_values={"CODE": FLOAT_SECRET},
    )
    assert FLOAT_SECRET_TEXT not in blob
    assert data["captured_values"]["CODE"] == REDACTED
    assert data["parsed_probes"] == record["parsed_probes"]
    assert data["parsed_probes_sha256"] == record["parsed_probes_sha256"]


# ---------------------------------------------------------------------------
# (c) a bool capture isn't collected (bool is an int subclass: pin the guard)
# ---------------------------------------------------------------------------


def test_bool_capture_is_not_collected():
    captured = {"FLAG": True, "OFF": False, "SESSION": INT_SECRET}
    runtime_vals = collect_secret_values(captured=captured)
    record_vals = collect_record_secret_values({"captured_values": captured})
    for vals in (runtime_vals, record_vals):
        assert INT_SECRET_TEXT in vals  # the numeric capture IS collected
        assert "True" not in vals
        assert "true" not in vals
        assert "False" not in vals
        assert "false" not in vals


# ---------------------------------------------------------------------------
# (d) an int capture shorter than _MIN_SECRET_LEN isn't collected
# ---------------------------------------------------------------------------


def test_short_int_capture_is_not_collected():
    captured = {"PIN": 123, "SESSION": INT_SECRET}
    runtime_vals = collect_secret_values(captured=captured)
    record_vals = collect_record_secret_values({"captured_values": captured})
    for vals in (runtime_vals, record_vals):
        assert INT_SECRET_TEXT in vals  # the long numeric capture IS collected
        assert "123" not in vals  # the short one is not (parity with strings)
    # Parity: a short string capture is skipped too.
    assert collect_secret_values(captured={"PIN": "123"}) == []
    assert collect_record_secret_values({"captured_values": {"PIN": "123"}}) == []


# ---------------------------------------------------------------------------
# (e) both collectors (runtime path and record path) behave the same way
# ---------------------------------------------------------------------------


def test_both_collectors_treat_numeric_captures_as_secrets():
    captured = {
        "INT_ID": INT_SECRET,
        "FLOAT_CODE": FLOAT_SECRET,
        "TOKEN": "string-secret-value",
        "FLAG": True,
        "SHORT": 42,
    }
    runtime_vals = collect_secret_values(captured=captured)
    record_vals = collect_record_secret_values({"captured_values": captured})
    for vals in (runtime_vals, record_vals):
        assert INT_SECRET_TEXT in vals
        assert FLOAT_SECRET_TEXT in vals  # float collected as json.dumps form
        assert "string-secret-value" in vals
        assert "True" not in vals
        assert "42" not in vals
    assert set(runtime_vals) == set(record_vals)
