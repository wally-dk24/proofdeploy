"""Addendum 24 — WO-5.2 round 2: harness provenance done properly (R2).

Round 1 recorded a top-level ``proofdeploy_commit`` resolved env-first:
an environment variable could claim any commit, the value could
silently be empty, and a package installed inside another project's
git checkout (e.g. in a venv) recorded THAT project's commit.

The replacement is ``provenance.harness`` on every evidence record:
``{commit, source, dirty, workflow_ref}`` where

- ``commit`` comes from ``git rev-parse HEAD`` ONLY when the package's
  parent directory is the repo's toplevel (``git rev-parse
  --show-toplevel``); any other layout yields None. ``source`` is
  ``"git"`` or None. No env var supplies or overrides the commit:
  ``PROOFDEPLOY_COMMIT`` is dropped entirely and ``GITHUB_SHA`` is a
  cross-check for the gate, never a source.
- ``dirty`` is True when any tracked file differs from HEAD
  (``git diff --quiet HEAD``).
- ``workflow_ref`` is ``GITHUB_WORKFLOW_REF``, or ``GITHUB_REF``.

The measurement gate (``assert_harness_commit_gate``) refuses to start
a measured run when the commit is missing, the tree is dirty, or
``GITHUB_SHA`` disagrees with HEAD. Unmeasured dev runs record whatever
is true and continue.

Also here: the M6 regression test (caller-supplied captures must not
replace auto-collected ones) and the schema 1.1.0 bump test (a 1.0.0
record with scalar ``captured_values`` and no harness still loads and
re-scores).
"""

import json
import subprocess
from pathlib import Path

import pytest

from proofdeploy import runpair
from proofdeploy.executor import ProbeResult, Verdict
from proofdeploy.probe import SCHEMA_VERSION
from proofdeploy.runpair import (
    build_evidence_record,
    collect_record_secret_values,
    read_records,
    write_evidence_record,
    write_score_record,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Ambient CI env must not leak into provenance resolution."""
    for var in ("PROOFDEPLOY_COMMIT", "GITHUB_SHA", "GITHUB_WORKFLOW_REF", "GITHUB_REF"):
        monkeypatch.delenv(var, raising=False)


def _harness():
    return runpair.harness_provenance()


def _gate():
    return runpair.assert_harness_commit_gate()


def _gate_error():
    return runpair.CommitGateError


def _git(cwd: Path, *args: str) -> str:
    p = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True, text=True, check=True,
    )
    return p.stdout.strip()


def _init_repo(root: Path) -> str:
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    return _git(root, "rev-parse", "HEAD")


def _fake_package(monkeypatch, pkg_root: Path) -> None:
    """A proofdeploy package under pkg_root; runpair resolves from it."""
    pkg = pkg_root / "proofdeploy"
    pkg.mkdir(parents=True, exist_ok=True)
    fake = pkg / "runpair.py"
    fake.write_text("# fake package module for provenance tests\n")
    (pkg / "__init__.py").write_text("")
    monkeypatch.setattr(runpair, "__file__", str(fake))


def _own_checkout(monkeypatch, tmp_path: Path) -> tuple[Path, str]:
    """A git repo whose toplevel IS the package's parent directory."""
    root = tmp_path / "own"
    _fake_package(monkeypatch, root)
    (root / "README.md").write_text("proofdeploy checkout\n")
    head = _init_repo(root)
    return root, head


# ---------------------------------------------------------------------------
# Resolution: git only, only at the toplevel, env never overrides.
# ---------------------------------------------------------------------------


def test_commit_comes_from_git_when_package_at_toplevel(monkeypatch, tmp_path):
    _, head = _own_checkout(monkeypatch, tmp_path)
    harness = _harness()
    assert harness["commit"] == head
    assert harness["source"] == "git"
    assert harness["dirty"] is False


def test_env_override_cannot_claim_a_commit(monkeypatch):
    """PROOFDEPLOY_COMMIT is dropped: an env var can claim any commit."""
    monkeypatch.setenv("PROOFDEPLOY_COMMIT", "f" * 40)
    real_head = _git(REPO_ROOT, "rev-parse", "HEAD")
    harness = _harness()
    assert harness["commit"] == real_head
    assert harness["commit"] != "f" * 40


def test_github_sha_is_a_crosscheck_not_a_source(monkeypatch):
    """GITHUB_SHA never supplies the commit value itself."""
    monkeypatch.setenv("GITHUB_SHA", "0" * 40)
    real_head = _git(REPO_ROOT, "rev-parse", "HEAD")
    harness = _harness()
    assert harness["commit"] == real_head


def test_package_below_toplevel_records_no_commit(monkeypatch, tmp_path):
    """A venv inside a customer checkout must not record its commit."""
    root = tmp_path / "customer"
    pkg_root = root / ".venv" / "lib" / "python3.12" / "site-packages"
    _fake_package(monkeypatch, pkg_root)
    (root / "app.py").write_text("# the customer's own app\n")
    _init_repo(root)
    harness = _harness()
    assert harness["commit"] is None
    assert harness["source"] is None
    with pytest.raises(_gate_error()):
        _gate()


def test_no_git_repo_records_no_commit(monkeypatch, tmp_path):
    _fake_package(monkeypatch, tmp_path / "plain")
    harness = _harness()
    assert harness["commit"] is None
    with pytest.raises(_gate_error()):
        _gate()


def test_workflow_ref_recorded(monkeypatch, tmp_path):
    _own_checkout(monkeypatch, tmp_path)
    monkeypatch.setenv("GITHUB_REF", "refs/heads/main")
    assert _harness()["workflow_ref"] == "refs/heads/main"
    monkeypatch.setenv(
        "GITHUB_WORKFLOW_REF",
        "wally-dk24/proofdeploy/.github/workflows/measure.yml@refs/heads/main",
    )
    assert _harness()["workflow_ref"] == (
        "wally-dk24/proofdeploy/.github/workflows/measure.yml@refs/heads/main"
    )


# ---------------------------------------------------------------------------
# The gate: refuses on dirty, on GITHUB_SHA mismatch; passes when proven.
# ---------------------------------------------------------------------------


def test_dirty_tracked_file_marks_dirty_and_gate_refuses(monkeypatch, tmp_path):
    root, _ = _own_checkout(monkeypatch, tmp_path)
    (root / "proofdeploy" / "runpair.py").write_text("# edited after commit\n")
    harness = _harness()
    assert harness["dirty"] is True
    with pytest.raises(_gate_error()):
        _gate()


def test_gate_refuses_on_github_sha_mismatch(monkeypatch, tmp_path):
    _, head = _own_checkout(monkeypatch, tmp_path)
    monkeypatch.setenv("GITHUB_SHA", "0" * 40)
    with pytest.raises(_gate_error()):
        _gate()
    monkeypatch.setenv("GITHUB_SHA", head)
    assert _gate()["commit"] == head


def test_gate_passes_on_clean_own_checkout(monkeypatch, tmp_path):
    _, head = _own_checkout(monkeypatch, tmp_path)
    assert _gate()["commit"] == head


# ---------------------------------------------------------------------------
# M6: caller-supplied captures never replace auto-collected ones.
# ---------------------------------------------------------------------------


def _records_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "t@t")
    _git(path, "config", "user.name", "t")
    (path / "README.md").write_text("x")
    _git(path, "add", ".")
    _git(path, "commit", "-qm", "init")
    return path


def test_caller_captures_do_not_replace_auto_collected(tmp_path):
    auto_token = "sess-auto-collected-0123456789"
    caller_token = "sess-caller-supplied-0123456789"
    r = ProbeResult(
        probe_index=0,
        verdict=Verdict.PASS,
        details=[f"setup login returned session {auto_token}"],
        http_body=json.dumps({"session_echo": auto_token}),
        request={
            "method": "GET",
            "url": f"http://127.0.0.1:8000/me/{auto_token}",
            "headers": {},
            "body": None,
        },
    )
    r.captured_bindings = {"SESSION": auto_token}
    record = build_evidence_record(
        run_kind="bug",
        bug_id="ADDENDUM-24-M6",
        fix_sha="a" * 40,
        fix_parent_sha="b" * 40,
        fix_results=[r],
        captured_values={"SESSION": caller_token},
    )
    captured = record["captured_values"]["SESSION"]
    assert isinstance(captured, list)
    assert set(captured) == {auto_token, caller_token}
    collected = set(collect_record_secret_values(record))
    assert {auto_token, caller_token} <= collected

    repo = _records_repo(tmp_path)
    out = tmp_path / "runs"
    _, path, _ = write_evidence_record(record, out, repo)
    blob = path.read_text(encoding="utf-8")
    assert auto_token not in blob
    assert caller_token not in blob


# ---------------------------------------------------------------------------
# Schema 1.1.0: new records carry it; 1.0.0 records still load + re-score.
# ---------------------------------------------------------------------------


def _result(verdict: Verdict, token: str) -> ProbeResult:
    r = ProbeResult(
        probe_index=0,
        verdict=verdict,
        details=[f"session {token}"],
        http_body=json.dumps({"session_echo": token}),
        request={
            "method": "GET",
            "url": "http://127.0.0.1:8000/me",
            "headers": {},
            "body": None,
        },
    )
    # Both sides capture the same single value: the 1.0.0 scalar shape.
    r.captured_bindings = {"SESSION": token}
    return r


def test_schema_bump_and_old_record_loads_and_rescores(tmp_path):
    assert SCHEMA_VERSION == "1.3.0"

    token = "sess-schema-check-0123456789"
    record = build_evidence_record(
        run_kind="bug",
        bug_id="ADDENDUM-24-SCHEMA",
        fix_sha="a" * 40,
        fix_parent_sha="b" * 40,
        fix_parent_results=[_result(Verdict.FAIL, token)],
        fix_results=[_result(Verdict.PASS, token)],
    )
    assert record["schema_version"] == "1.3.0"
    assert record["provenance"]["harness"]["commit"]

    # Downgrade the record to the 1.0.0 shape: scalar captured_values,
    # 1.0.0 stamps everywhere, no harness provenance.
    record["schema_version"] = "1.0.0"
    for side in ("fix_parent_results", "fix_results"):
        for res in record[side]:
            res["schema_version"] = "1.0.0"
    assert record["captured_values"]["SESSION"] == token  # scalar already
    record["provenance"] = {}

    repo = _records_repo(tmp_path)
    out = tmp_path / "runs"
    record_id, _, _ = write_evidence_record(record, out, repo)
    written = read_records(out)
    assert written[0]["schema_version"] == "1.0.0"
    assert written[0]["captured_values"]["SESSION"] != token  # redacted

    # Re-score from the committed 1.0.0 evidence: FAIL -> PASS is a
    # candidate catch, exactly as a 1.1.0 record would score.
    _, score_file = write_score_record(
        out, repo,
        evidence_record_id=record_id,
        run_kind="bug",
        bug_id="ADDENDUM-24-SCHEMA",
    )
    records = read_records(out)
    score = [r for r in records if r["record_type"] == "score"]
    assert len(score) == 1
    assert score[0]["verdict"] == "candidate_catch"
    assert score[0]["candidate_catch"] == [0]
    assert score_file.name == "runs.jsonl"
