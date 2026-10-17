"""Offline tests for scripts/wo6/regc_bug_select.py (REG-C item 7,
as implemented by D169; plan 1.5(d)).

No network: every git history is a synthetic repo built in tmp, and
check (d)'s readiness is an injected fake. The classification tests
pin the D169 Q2 classes at their boundaries; the history tests pin
one branch per check (a)-(d), passing and failing; the walk tests
pin the D169 Q1 first-parent topology, the strict 2026-07-01
boundary and the 200-commit exhaustion; the journal/lock tests pin
the D74 resume guarantees and the D140 single-writer lock.
"""
from __future__ import annotations

import hashlib
import importlib.util
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "wo6" / "regc_bug_select.py"
_spec = importlib.util.spec_from_file_location("regc_bug_select", SCRIPT)
assert _spec is not None and _spec.loader is not None
mod = importlib.util.module_from_spec(_spec)
sys.modules["regc_bug_select"] = mod
_spec.loader.exec_module(mod)


def ts(year: int, month: int, day: int, hour: int = 12) -> int:
    return int(datetime(year, month, day, hour, tzinfo=timezone.utc).timestamp())


BOUNDARY = ts(2026, 7, 1, 0)


def git(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        env=full_env,
    )
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {proc.stderr}")
    return proc.stdout.strip()


def init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "app"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "user.name", "Bug Select Tests")
    git(repo, "config", "commit.gpgsign", "false")
    git(repo, "config", "tag.gpgsign", "false")
    return repo


def commit(
    repo: Path,
    subject: str,
    when: int,
    files: dict[str, str] | None = None,
) -> str:
    for rel, content in (files or {}).items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    git(repo, "add", "-A")
    stamp = f"{when} +0000"
    git(
        repo, "commit", "-q", "-m", subject,
        env={"GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp},
    )
    return git(repo, "rev-parse", "HEAD")


def add_tag(repo: Path, name: str, when: int, target: str = "HEAD") -> None:
    git(
        repo, "tag", "-a", "-m", f"release {name}", name, target,
        env={"GIT_COMMITTER_DATE": f"{when} +0000"},
    )


class FakeReady:
    """Injected check (d): the named shas are READY, nothing else is."""

    def __init__(self, ready: Any = ()) -> None:
        self.ready = set(ready)
        self.calls: list[tuple[str, str]] = []

    def __call__(self, parent_sha: str, fix_sha: str) -> dict[str, bool]:
        self.calls.append((parent_sha, fix_sha))
        return {
            "parent_ready": parent_sha in self.ready,
            "fix_ready": fix_sha in self.ready,
        }


def app_dict(head: str) -> dict[str, Any]:
    return {
        "repo": "paperless-ngx/paperless-ngx",
        "head_sha": head,
        "default_branch": "main",
        "app_path": "src",
    }


def select(repo_path: Path, head: str, ready: FakeReady, **kw: Any) -> dict[str, Any]:
    return mod.select_fix(
        repo=mod.AppRepo(repo_path),
        app=app_dict(head),
        ready_pair=ready,
        main_sha="f" * 40,
        script_sha256="s" * 64,
        **kw,
    )


def standard_history(tmp_path: Path) -> tuple[Path, str, str, str, str]:
    """base (Jan) -> B changes a line (Feb, tagged v1.0) -> mid adds
    an unrelated module (Aug 1) -> fix repairs B's line (Aug 15).
    B != fix^ (mid sits between them)."""
    repo = init_repo(tmp_path)
    base = commit(repo, "Initial commit", ts(2026, 1, 10),
                  {"src/app.py": "l1\nl2\nl3\n"})
    b = commit(repo, "Introduce the counter", ts(2026, 2, 1),
               {"src/app.py": "l1\nl2-bug\nl3\n"})
    add_tag(repo, "v1.0", ts(2026, 2, 2))
    mid = commit(repo, "Add other module", ts(2026, 8, 1),
                 {"src/other.py": "x = 1\n"})
    fix = commit(repo, "Fix the counter", ts(2026, 8, 15),
                 {"src/app.py": "l1\nl2-fixed\nl3\n"})
    return repo, base, b, mid, fix


# ---------------------------------------------------------------------
# D169 Q2 classification: one per class at its boundaries.
# ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "path,expected",
    [
        ("src/paperless_testing/x.py", "TEST"),   # _testing segment suffix
        ("docs/x.py", "DOC"),                     # docs segment
        ("requirements.txt", "OTHER"),            # dependency file / .txt
        ("src-ui/src/app/x.ts", "SOURCE"),
        ("src-ui/src/app/x.spec.ts", "TEST"),     # *.spec.<ext> basename
        ("src/locale/x.po", "OTHER"),             # locale segment / translation
        (".github/workflows/x.yml", "OTHER"),     # CI
        ("src/tests/x.py", "TEST"),
        ("src/conftest.py", "TEST"),
        ("src/x_test.py", "TEST"),
        ("src/test_app.py", "TEST"),
        ("README.md", "DOC"),
        ("CHANGELOG", "DOC"),
        ("src/contest/x.py", "SOURCE"),           # "contest" is not *_test
        ("src/app.py", "SOURCE"),
        ("src/notes.txt", "OTHER"),               # any .txt
        ("package.json", "OTHER"),
        ("docker-compose.yml", "OTHER"),
        ("docs/x.po", "DOC"),                     # DOC is checked before OTHER
        ("src/Makefile", "OTHER"),                # no SOURCE extension
    ],
)
def test_classify_path(path: str, expected: str) -> None:
    assert mod.classify_path(path) == expected


@pytest.mark.parametrize(
    "subject,expected",
    [
        ("Fix the counter", True),
        ("Fixed the counter", True),
        ("fixes #12: the counter", True),
        ("A FIX for the counter", True),
        ("Prefix the parser", False),   # the word boundary is load-bearing
        ("hotfix the parser", False),
        ("Repair the counter", False),
    ],
)
def test_subject_matches(subject: str, expected: bool) -> None:
    assert mod.subject_matches(subject) is expected


# ---------------------------------------------------------------------
# The happy path and one failing branch per check.
# ---------------------------------------------------------------------


def test_selects_first_passing_fix(tmp_path: Path) -> None:
    repo, _base, b, mid, fix = standard_history(tmp_path)
    record = select(repo, fix, FakeReady({mid, fix}))
    assert record["outcome"] == "selected"
    assert record["selected"] == {
        "fix": fix,
        "fix_parent": mid,
        "b": b,  # B != fix^ is allowed (D169 / REG-C item 7)
        "b_committer_date": record["walked"][-1]["b_committer_date"],
        "tag": "v1.0",
        "tag_created": record["walked"][-1]["tag_created"],
    }
    walked = {e["sha"]: e for e in record["walked"]}
    assert walked[mid]["failed_check"] == "a"
    assert walked[fix]["outcome"] == "selected"
    assert record["walked_count"] == 2


def test_check_a_failure_is_recorded_first(tmp_path: Path) -> None:
    """A commit failing both (a) and (b) records (a): checks run in
    the registered order and the record names the first failure."""
    repo = init_repo(tmp_path)
    commit(repo, "Initial commit", ts(2026, 1, 10), {"docs/x.md": "hi\n"})
    head = commit(repo, "Improve the guide", ts(2026, 8, 1),
                  {"docs/x.md": "hi again\n"})
    record = select(repo, head, FakeReady())
    entry = record["walked"][-1]
    assert entry["failed_check"] == "a"
    assert record["outcome"] == "exhausted"


def test_check_b_docs_and_tests_only_fix_rejected(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    commit(repo, "Initial commit", ts(2026, 1, 10),
           {"src/app.py": "l1\n", "docs/g.md": "a\n", "tests/test_a.py": "t\n"})
    head = commit(repo, "Fix the guide and its test", ts(2026, 8, 1),
                  {"docs/g.md": "b\n", "tests/test_a.py": "u\n"})
    record = select(repo, head, FakeReady())
    entry = record["walked"][-1]
    assert entry["failed_check"] == "b"
    assert entry["changed_classes"] == {
        "SOURCE": 0, "TEST": 1, "DOC": 1, "OTHER": 0,
    }


def test_check_c_add_only_fix_has_no_b(tmp_path: Path) -> None:
    repo, _base, _b, _mid, _fix = standard_history(tmp_path)
    head = commit(repo, "Fix by adding a guard line", ts(2026, 8, 20),
                  {"src/app.py": "l1\nl2-fixed\nl3\nguard\n"})
    record = select(repo, head, FakeReady())
    entry = record["walked"][-1]
    assert entry["failed_check"] == "c"
    assert "no removed or changed lines" in entry["reason"]


def test_check_c_b_in_no_tag_rejected(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    commit(repo, "Initial commit", ts(2026, 1, 10), {"src/app.py": "l1\nl2\n"})
    commit(repo, "Introduce the counter", ts(2026, 2, 1),
           {"src/app.py": "l1\nl2-bug\n"})
    head = commit(repo, "Fix the counter", ts(2026, 8, 15),
                  {"src/app.py": "l1\nl2-fixed\n"})
    record = select(repo, head, FakeReady())
    entry = record["walked"][-1]
    assert entry["failed_check"] == "c"
    assert "contained in no tag" in entry["reason"]


def test_check_c_tag_created_after_fix_rejected(tmp_path: Path) -> None:
    repo, _base, b, _mid, _fix = standard_history(tmp_path)
    # Remove the timely tag; add one created after the fix.
    git(repo, "tag", "-d", "v1.0")
    add_tag(repo, "v9.9", ts(2026, 9, 1), target=b)
    head = git(repo, "rev-parse", "HEAD")
    record = select(repo, head, FakeReady())
    entry = record["walked"][-1]
    assert entry["failed_check"] == "c"
    assert "at or after the fix" in entry["reason"]


def test_check_d_requires_both_sides_ready(tmp_path: Path) -> None:
    repo, _base, _b, mid, fix = standard_history(tmp_path)

    record = select(repo, fix, FakeReady({fix}))
    entry = record["walked"][-1]
    assert entry["failed_check"] == "d"
    assert entry["reason"] == "fix^ is not READY"
    assert entry["parent_ready"] is False and entry["fix_ready"] is True

    record = select(repo, fix, FakeReady({mid}))
    entry = record["walked"][-1]
    assert entry["failed_check"] == "d"
    assert entry["reason"] == "fix is not READY"

    record = select(repo, fix, FakeReady())
    entry = record["walked"][-1]
    assert entry["failed_check"] == "d"
    assert entry["reason"] == "fix^ and fix are not READY"


# ---------------------------------------------------------------------
# The walk: boundary, topology, exhaustion (D169 Q1).
# ---------------------------------------------------------------------


def test_date_boundary_is_strict(tmp_path: Path) -> None:
    """A qualifying fix committed exactly at 2026-07-01T00:00:00Z is
    NOT walked; the next qualifying fix (one second later counts,
    here the next day) is selected instead."""
    repo = init_repo(tmp_path)
    commit(repo, "Initial commit", ts(2026, 1, 10),
           {"src/app.py": "l1\nl2\nl3\n"})
    b1 = commit(repo, "Introduce the counter", ts(2026, 2, 1),
                {"src/app.py": "l1\nl2-bug\nl3\n"})
    add_tag(repo, "v1.0", ts(2026, 2, 2))
    b2 = commit(repo, "Introduce the meter", ts(2026, 3, 1),
                {"src/app.py": "l1\nl2-bug\nl3-bug\n"})
    add_tag(repo, "v2.0", ts(2026, 3, 2))
    at_boundary = commit(repo, "Fix the counter", BOUNDARY,
                         {"src/app.py": "l1\nl2-fixed\nl3-bug\n"})
    after = commit(repo, "Fix the meter", ts(2026, 8, 15),
                   {"src/app.py": "l1\nl2-fixed\nl3-fixed\n"})
    assert b1 != b2
    # Every side is READY, so the boundary commit WOULD be selected
    # if it were walked: its absence from the walk is the assertion.
    record = select(repo, after, FakeReady({b2, at_boundary, after}))
    walked_shas = [e["sha"] for e in record["walked"]]
    assert at_boundary not in walked_shas
    assert record["selected"]["fix"] == after
    assert record["selected"]["b"] == b2


def test_side_branch_fix_is_not_walked_its_merge_is(tmp_path: Path) -> None:
    """D169 topology: a qualifying fix that exists only on a merged
    side branch is not on the first-parent chain; its merge commit
    is evaluated instead (and fails (a) on its own subject)."""
    repo = init_repo(tmp_path)
    commit(repo, "Initial commit", ts(2026, 1, 10),
           {"src/app.py": "l1\nl2\nl3\n"})
    commit(repo, "Introduce the counter", ts(2026, 2, 1),
           {"src/app.py": "l1\nl2-bug\nl3\n"})
    add_tag(repo, "v1.0", ts(2026, 2, 2))
    mid = commit(repo, "Add other module", ts(2026, 8, 1),
                 {"src/other.py": "x = 1\n"})
    git(repo, "checkout", "-q", "-b", "side", mid)
    side_fix = commit(repo, "Fix the counter", ts(2026, 8, 5),
                      {"src/app.py": "l1\nl2-fixed\nl3\n"})
    git(repo, "checkout", "-q", "main")
    stamp = f"{ts(2026, 8, 10)} +0000"
    git(repo, "merge", "-q", "--no-ff", "-m", "Merge branch 'side'", "side",
        env={"GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp})
    head = git(repo, "rev-parse", "HEAD")
    record = select(repo, head, FakeReady({mid, side_fix, head}))
    walked_shas = [e["sha"] for e in record["walked"]]
    assert side_fix not in walked_shas
    merge_entry = record["walked"][-1]
    assert merge_entry["sha"] == head
    assert merge_entry["failed_check"] == "a"
    assert record["outcome"] == "exhausted"


def test_exhaustion_at_200_walked(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    commit(repo, "Initial commit", ts(2026, 1, 10), {"src/app.py": "l1\nl2\n"})
    b = commit(repo, "Introduce the counter", ts(2026, 2, 1),
               {"src/app.py": "l1\nl2-bug\n"})
    add_tag(repo, "v1.0", ts(2026, 2, 2))
    head = b
    for i in range(200):
        head = commit(repo, f"Chore step {i}", ts(2026, 8, 1) + i * 60,
                      {f"src/mod{i}.py": f"x = {i}\n"})
    qualifying = commit(repo, "Fix the counter", ts(2026, 8, 20),
                        {"src/app.py": "l1\nl2-fixed\n"})
    record = select(repo, qualifying, FakeReady({head, qualifying}))
    assert record["outcome"] == "exhausted"
    assert record["walked_count"] == 200
    assert record["selected"] is None
    assert qualifying not in [e["sha"] for e in record["walked"]]
    assert "next READY candidate" in record["next"]


# ---------------------------------------------------------------------
# The record, the journal (D74) and the lock (D140).
# ---------------------------------------------------------------------


def test_record_carries_identity_and_full_walk(tmp_path: Path) -> None:
    repo, _base, b, mid, fix = standard_history(tmp_path)
    record = select(repo, fix, FakeReady({mid, fix}))
    assert record["kind"] == "wo6-bug-select"
    assert record["main_sha"] == "f" * 40
    assert record["app"]["head_sha"] == fix
    assert record["app"]["app_path"] == "src"
    assert record["boundary"] == "2026-07-01T00:00:00Z"
    assert record["walk"] == "first-parent"
    for entry in record["walked"]:
        if entry["outcome"] == "rejected":
            assert entry["failed_check"] in ("a", "b", "c", "d")
            assert entry["reason"]


def test_script_sha256_is_the_file_itself() -> None:
    digest = hashlib.sha256(SCRIPT.read_bytes()).hexdigest()
    assert mod._script_sha256() == digest


def _open_journal(tmp_path: Path, head: str) -> Any:
    return mod.Journal.open(
        tmp_path / "journal",
        script_sha256="s" * 64,
        main_sha="f" * 40,
        app_repo="paperless-ngx/paperless-ngx",
        app_head_sha=head,
    )


def test_journal_resume_reuses_entries(tmp_path: Path) -> None:
    repo, _base, _b, mid, fix = standard_history(tmp_path)
    journal, resumed, _ = _open_journal(tmp_path, fix)
    assert resumed is False
    first = select(repo, fix, FakeReady(), journal=journal)
    assert first["outcome"] == "exhausted"  # nothing READY in run 1

    journal2, resumed2, _ = _open_journal(tmp_path, fix)
    assert resumed2 is True
    spy = FakeReady({mid, fix})
    second = select(repo, fix, spy, journal=journal2)
    # Built only from the journal: the new checker is never called
    # and the recorded outcome stands.
    assert spy.calls == []
    assert second["walked"] == first["walked"]
    assert second["outcome"] == "exhausted"


def test_journal_identity_mismatch_refuses(tmp_path: Path) -> None:
    _open_journal(tmp_path, "a" * 40)
    with pytest.raises(mod.JournalMismatch):
        _open_journal(tmp_path, "b" * 40)


def test_journal_torn_final_line_is_dropped(tmp_path: Path) -> None:
    journal, _, _ = _open_journal(tmp_path, "a" * 40)
    journal.append_walk({"sha": "x" * 40, "outcome": "rejected"})
    with (journal.dir / "walk.jsonl").open("a") as f:
        f.write('{"sha": "torn')  # a death mid-append
    entries = journal.read_walk()
    assert [e["sha"] for e in entries] == ["x" * 40]


def test_single_writer_lock(tmp_path: Path) -> None:
    first = mod.SingleWriterLock(tmp_path / "journal")
    first.acquire()
    second = mod.SingleWriterLock(tmp_path / "journal")
    with pytest.raises(mod.SingleWriterError):
        second.acquire()
    first.release()
    second.acquire()
    second.release()
