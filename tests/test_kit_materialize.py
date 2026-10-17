"""D150: the conformance kit's materialized history shape.

D146 made the golden seeds uniform across every revision of every
golden app, so the fix-parent tree is byte-identical to the
intermediate tree BY DESIGN. materialize_golden must still build
the full seven-commit history — committing fix-parent as the one
intentional empty commit (kit.EXPECTED_EMPTY_REVISIONS) — and must
keep FAILING when any other revision's tree collapses onto its
predecessor, so a future accidental collapse of two distinct
revisions cannot slip through silently (D150: no blanket
--allow-empty).

These tests are host-side (git + filesystem only, no sandbox): one
pins the materialized history of every real golden app, the other
the refusal on a synthetic collapsed revision outside the set.
"""

from __future__ import annotations

import subprocess
import sys
from itertools import pairwise
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "conformance"))
import kit

_FIX_PARENT_MESSAGE = (
    "fix parent: no tree change since seeds were made uniform (D146)"
)


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=True, timeout=60,
    )
    return proc.stdout.strip()


@pytest.mark.parametrize("app", kit.APPS)
def test_materialized_history_shape(app: str, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    shas = kit.materialize_golden(app, repo)
    trees = {rev: _git(repo, "rev-parse", f"{sha}^{{tree}}") for rev, sha in shas.items()}
    # The one designed collapse (D146): fix-parent == intermediate.
    assert trees["fix-parent"] == trees["intermediate"]
    # Every other consecutive pair differs.
    revs = list(kit.REVISIONS)
    for prev, rev in pairwise(revs):
        if rev == "fix-parent":
            continue
        assert trees[prev] != trees[rev], (
            f"{app}: {prev} and {rev} trees must differ"
        )
    # B != fix^ still holds (the WO-5 lesson): the bug commit is
    # neither the fix-parent commit nor the fix-parent tree.
    assert shas["bug"] != shas["fix-parent"]
    assert trees["bug"] != trees["fix-parent"]
    # The record is true: the empty commit says why it is empty.
    assert _git(repo, "log", "-1", "--format=%s", shas["fix-parent"]) == (
        _FIX_PARENT_MESSAGE
    )


def test_collapsed_revision_outside_the_expected_set_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Synthetic: `intermediate` — NOT in EXPECTED_EMPTY_REVISIONS —
    repeats its predecessor's tree, so materialize_golden must fail
    at that commit instead of recording a silent empty commit."""
    app_dir = tmp_path / "golden" / "synthetic-app"
    contents = {
        "base": "v1\n",
        "bug": "v2\n",
        "intermediate": "v2\n",  # collapsed onto bug — must fail
        "fix-parent": "v2\n",  # the designed collapse (in the set)
        "fix": "v3\n",
        "clean": "v4\n",
        "lockmismatch": "v5\n",
    }
    for rev, content in contents.items():
        rev_dir = app_dir / "revisions" / rev
        rev_dir.mkdir(parents=True)
        (rev_dir / "app.txt").write_text(content, encoding="utf-8")
    monkeypatch.setattr(kit, "GOLDEN_DIR", tmp_path / "golden")
    with pytest.raises(subprocess.CalledProcessError):
        kit.materialize_golden("synthetic-app", tmp_path / "repo")
