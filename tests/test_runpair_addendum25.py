"""Addendum 25 — WO-5.2 round 3: the three surviving mutants (tests only).

Round 2's suite caught 11 of 14 planted bugs. Three survived because
the tests exercised the right code in the wrong layout:

- H1: ``GITHUB_SHA`` used as the commit SOURCE when the package is
  not at its own toplevel. This is exactly the customer-CI case:
  ProofDeploy pip-installed inside a customer's workflow, where
  ``GITHUB_SHA`` is the CUSTOMER's commit. With the bug, the gate
  passes and the record names the customer's commit as ProofDeploy's.
  Round 2's tests set ``GITHUB_SHA`` only inside ProofDeploy's own
  checkout, where git overwrites it. The tests here set it with the
  package below the toplevel, and with no git repo at all.
- H6: caller-supplied provenance containing a ``"harness"`` key
  overrides the real harness object. The record's harness is computed
  in ``build_evidence_record`` and must never be caller-supplied.
"""

import subprocess
from pathlib import Path

import pytest

from proofdeploy import runpair
from proofdeploy.runpair import build_evidence_record


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Ambient CI env must not leak into provenance resolution."""
    for var in ("PROOFDEPLOY_COMMIT", "GITHUB_SHA", "GITHUB_WORKFLOW_REF", "GITHUB_REF"):
        monkeypatch.delenv(var, raising=False)


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


# ---------------------------------------------------------------------------
# H1: GITHUB_SHA is never a commit source, whatever the layout.
# ---------------------------------------------------------------------------


def test_github_sha_not_a_source_below_toplevel(monkeypatch, tmp_path):
    """Customer CI: package in a venv inside the customer's checkout.

    GITHUB_SHA there is the customer's commit. It must not become
    ProofDeploy's commit, and the gate must refuse.
    """
    root = tmp_path / "customer"
    pkg_root = root / ".venv" / "lib" / "python3.12" / "site-packages"
    _fake_package(monkeypatch, pkg_root)
    (root / "app.py").write_text("# the customer's own app\n")
    customer_head = _init_repo(root)
    monkeypatch.setenv("GITHUB_SHA", customer_head)
    harness = runpair.harness_provenance()
    assert harness["commit"] is None
    assert harness["source"] is None
    with pytest.raises(runpair.CommitGateError, match="commit is unknown"):
        runpair.assert_harness_commit_gate()


def test_github_sha_not_a_source_without_git(monkeypatch, tmp_path):
    """No git repo at all: GITHUB_SHA alone still proves nothing."""
    _fake_package(monkeypatch, tmp_path / "plain")
    monkeypatch.setenv("GITHUB_SHA", "c" * 40)
    harness = runpair.harness_provenance()
    assert harness["commit"] is None
    assert harness["source"] is None
    with pytest.raises(runpair.CommitGateError, match="commit is unknown"):
        runpair.assert_harness_commit_gate()


# ---------------------------------------------------------------------------
# H6: a caller-supplied "harness" key never overrides the real harness.
# ---------------------------------------------------------------------------


def test_caller_provenance_cannot_override_harness():
    fake = {
        "commit": "f" * 40,
        "source": "caller",
        "dirty": False,
        "workflow_ref": "refs/heads/fake",
    }
    record = build_evidence_record(
        run_kind="bug",
        bug_id="ADDENDUM-25-H6",
        fix_sha="a" * 40,
        fix_parent_sha="b" * 40,
        provenance={"harness": fake, "run_url": "https://example.invalid/run/1"},
    )
    harness = record["provenance"]["harness"]
    assert harness == runpair.harness_provenance()
    assert harness["commit"] != "f" * 40
    # Other caller provenance keys are still recorded verbatim.
    assert record["provenance"]["run_url"] == "https://example.invalid/run/1"
