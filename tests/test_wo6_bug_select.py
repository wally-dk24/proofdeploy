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
import json
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


def side_outcome(
    *,
    ready: bool = False,
    status: str | None = None,
    typed_reason: str | None = None,
    last_error: str | None = None,
) -> dict[str, Any]:
    """One injected check-(d) side outcome, in the real checker's
    shape: the readiness row's fields plus the run's evidence."""
    return {
        "ready": ready,
        "status": status
        if status is not None
        else ("READY" if ready else "BUILD_FAILED"),
        "typed_reason": typed_reason,
        "last_error": last_error,
        "engine": "podman (fake)",
        "isolation_mode": "userns-remap (fake)",
        "network_mode": "none+socket",
        "network_none_evidence": "fake: app container ran with --net=none",
        "contract_store_sha256": "c" * 64,
    }


class FakeReady:
    """Injected check (d), in the real checker's protocol.

    A sha in `ready` provisions READY. A sha in `scripted` yields
    its outcomes in order, one per provisioning attempt of that
    side (the last one repeats). Any other sha yields a typed
    `not supported:` outcome — a genuine (d) failure — so tests
    that are not about classification see plain rejections."""

    def __init__(
        self,
        ready: Any = (),
        scripted: dict[str, list[dict[str, Any]]] | None = None,
    ) -> None:
        self.ready = set(ready)
        self.scripted = {k: list(v) for k, v in (scripted or {}).items()}
        self.calls: list[tuple[str, str, tuple[str, ...]]] = []

    def __call__(
        self, parent_sha: str, fix_sha: str, roles: tuple[str, ...]
    ) -> dict[str, dict[str, Any]]:
        self.calls.append((parent_sha, fix_sha, roles))
        out: dict[str, dict[str, Any]] = {}
        for role in roles:
            sha = parent_sha if role == "fix^" else fix_sha
            seq = self.scripted.get(sha)
            if seq:
                out[role] = seq.pop(0) if len(seq) > 1 else seq[0]
            elif sha in self.ready:
                out[role] = side_outcome(ready=True)
            else:
                out[role] = side_outcome(
                    typed_reason="not supported: fake refusal (test default)"
                )
        return out


def app_dict(head: str) -> dict[str, Any]:
    return {
        "repo": "paperless-ngx/paperless-ngx",
        "head_sha": head,
        "default_branch": "main",
        "app_path": "src",
    }


def small_prompt(_parent: str, _b: str) -> int:
    """Injected check (e): a prompt well under the R-SIZE budget."""
    return 1234


def select(repo_path: Path, head: str, ready: FakeReady, **kw: Any) -> dict[str, Any]:
    kw.setdefault("prompt_size", small_prompt)
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


# ---------------------------------------------------------------------
# R-SEL (D242): migration paths and check (b).
# ---------------------------------------------------------------------

PAPERLESS_FIX_PATHS = [
    "src/paperless_mail/migrations/0001_1_clamp_mailrule_maximum_age.py",
    "src/paperless_mail/migrations/0002_optimize_integer_field_sizes.py",
    "src/paperless_mail/tests/test_migrations.py",
]


@pytest.mark.parametrize(
    "path,expected",
    [
        ("src/app/migrations/0001_x.py", True),
        ("alembic/versions/abc.py", True),
        ("db/migrate/20240101_add.rb", True),
        ("lib/migrator.py", True),           # file name contains `migrat`
        ("src/App/Migrations/0001_x.py", True),   # case-insensitive
        ("src/app/migration/x.py", True),
        ("src/app/models.py", False),
    ],
)
def test_is_migration_path(path: str, expected: bool) -> None:
    assert mod.is_migration_path(path) is expected


def test_paperless_wo6_fix_paths_fail_check_b_upgrade_path_only(
    tmp_path: Path,
) -> None:
    repo = init_repo(tmp_path)
    commit(repo, "Initial commit", ts(2026, 1, 10),
           {p: "l1\n" for p in PAPERLESS_FIX_PATHS})
    head = commit(repo, "Fix the mail rule migration", ts(2026, 8, 1),
                  {p: "l1\nl2\n" for p in PAPERLESS_FIX_PATHS})
    record = select(repo, head, FakeReady())
    entry = record["walked"][-1]
    assert entry["failed_check"] == "b"
    assert "upgrade-path only" in entry["reason"]
    assert entry["changed_classes"] == {
        "SOURCE": 2, "TEST": 1, "DOC": 0, "OTHER": 0,
    }


def test_migration_plus_non_migration_source_passes_check_b(
    tmp_path: Path,
) -> None:
    repo = init_repo(tmp_path)
    commit(repo, "Initial commit", ts(2026, 1, 10),
           {"src/app/migrations/0001_x.py": "m1\n",
            "src/app.py": "l1\nl2\nl3\n"})
    b = commit(repo, "Introduce the counter", ts(2026, 2, 1),
               {"src/app.py": "l1\nl2-bug\nl3\n"})
    add_tag(repo, "v1.0", ts(2026, 2, 2))
    fix = commit(repo, "Fix the counter", ts(2026, 8, 15),
                 {"src/app.py": "l1\nl2-fixed\nl3\n",
                  "src/app/migrations/0001_x.py": "m1\nm2\n"})
    record = select(repo, fix, FakeReady({b, fix}))
    entry = record["walked"][-1]
    assert entry["failed_check"] != "b"
    assert record["outcome"] == "selected"


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


def test_check_c_tag_on_the_fix_itself_rejected(tmp_path: Path) -> None:
    """D190 (2): the equality edge in check (c). A LIGHTWEIGHT tag
    on the fix commit itself has creatordate == the fix's committer
    date (a lightweight tag's creatordate is the tagged commit's
    committer date, %(creatordate)). The tag contains B, but (c)
    requires a containing tag created STRICTLY BEFORE the fix —
    equality is not before — so with no earlier tag the candidate
    is rejected at (c)."""
    repo, _base, _b, _mid, fix = standard_history(tmp_path)
    git(repo, "tag", "-d", "v1.0")
    git(repo, "tag", "v-eq", fix)  # lightweight, on the fix itself
    record = select(repo, fix, FakeReady())
    entry = record["walked"][-1]
    assert entry["failed_check"] == "c"
    assert "at or after the fix" in entry["reason"]
    assert "v-eq" in entry["reason"]
    assert record["outcome"] == "exhausted"


def two_blamed_commits_history(
    tmp_path: Path, *, tag_target: str
) -> tuple[Path, str, str, str]:
    """base (Jan) -> older changes line 2 (Feb) -> newer changes
    line 3 (Mar) -> fix changes BOTH lines (Aug 15). Blame at fix^
    attributes the fix's removed lines to TWO commits: line 2 to
    `older`, line 3 to `newer`. B is the MOST RECENT blamed commit
    (REG-C item 7): `newer`. The single tag v1.0 is placed on
    `tag_target` ("older" or "newer"), created the day after it —
    always before the fix."""
    repo = init_repo(tmp_path)
    commit(repo, "Initial commit", ts(2026, 1, 10),
           {"src/app.py": "l1\nl2\nl3\nl4\n"})
    older = commit(repo, "Introduce the counter", ts(2026, 2, 1),
                   {"src/app.py": "l1\nl2-old\nl3\nl4\n"})
    newer = commit(repo, "Introduce the meter", ts(2026, 3, 1),
                   {"src/app.py": "l1\nl2-old\nl3-new\nl4\n"})
    if tag_target == "older":
        add_tag(repo, "v1.0", ts(2026, 2, 2), target=older)
    else:
        add_tag(repo, "v1.0", ts(2026, 3, 2), target=newer)
    fix = commit(repo, "Fix the counter and the meter", ts(2026, 8, 15),
                 {"src/app.py": "l1\nl2-fixed\nl3-fixed\nl4\n"})
    return repo, older, newer, fix


def test_check_c_b_is_the_most_recent_blamed_commit_untagged(
    tmp_path: Path,
) -> None:
    """D177 (1), case (i): the fix's removed lines blame to two
    commits; only the OLDER is contained in a tag created before
    the fix. B is the NEWER blamed commit, which is untagged, so
    the candidate is rejected at (c) — selecting the older commit
    as B (e.g. min instead of max) would wrongly pass it."""
    repo, older, newer, fix = two_blamed_commits_history(
        tmp_path, tag_target="older"
    )
    record = select(repo, fix, FakeReady({newer, fix}))
    entry = record["walked"][-1]
    assert entry["b_sha"] == newer  # the most recent blamed commit
    assert entry["b_sha"] != older
    assert entry["failed_check"] == "c"
    assert "contained in no tag" in entry["reason"]
    assert record["outcome"] == "exhausted"


def test_check_c_b_is_the_most_recent_blamed_commit_tagged(
    tmp_path: Path,
) -> None:
    """D177 (1), case (ii): only the NEWER blamed commit is tagged
    before the fix. B (the newer) is shipped, check (c) passes, and
    the selected record names the newer commit as B."""
    repo, older, newer, fix = two_blamed_commits_history(
        tmp_path, tag_target="newer"
    )
    record = select(repo, fix, FakeReady({newer, fix}))
    entry = record["walked"][-1]
    assert entry["outcome"] == "selected"
    assert entry["b_sha"] == newer
    assert entry["b_sha"] != older
    assert entry["tag"] == "v1.0"
    assert record["selected"]["b"] == newer
    assert record["selected"]["tag"] == "v1.0"


def test_diff_non_ascii_path_parses(tmp_path: Path) -> None:
    """D177 (3): the diff is taken with core.quotePath=false, so a
    path git would otherwise C-quote (non-ASCII) parses normally
    and its removed lines reach check (c)."""
    repo = init_repo(tmp_path)
    path = "src/café.py"
    commit(repo, "Initial commit", ts(2026, 1, 10),
           {path: "l1\nl2\nl3\n"})
    parent = commit(repo, "Introduce the counter", ts(2026, 2, 1),
                    {path: "l1\nl2-bug\nl3\n"})
    add_tag(repo, "v1.0", ts(2026, 2, 2))
    fix = commit(repo, "Fix the counter", ts(2026, 8, 15),
                 {path: "l1\nl2-fixed\nl3\n"})
    app_repo = mod.AppRepo(repo)
    assert app_repo.removed_or_changed_lines(parent, fix) == {path: [2]}
    record = select(repo, fix, FakeReady({parent, fix}))
    assert record["outcome"] == "selected"
    assert record["selected"]["b"] == parent


def test_diff_quoted_path_header_fails_closed(tmp_path: Path) -> None:
    """D177 (3): a path containing a double quote is C-quoted by
    git in the `--- ` header even with core.quotePath=false. The
    parser raises HarnessError — it never silently skips the
    file's removed lines (which would corrupt B)."""
    repo = init_repo(tmp_path)
    path = 'src/we"ird.py'
    commit(repo, "Initial commit", ts(2026, 1, 10),
           {path: "l1\nl2\n"})
    parent = commit(repo, "Introduce the counter", ts(2026, 2, 1),
                    {path: "l1\nl2-bug\n"})
    fix = commit(repo, "Fix the counter", ts(2026, 8, 15),
                 {path: "l1\nl2-fixed\n"})
    app_repo = mod.AppRepo(repo)
    with pytest.raises(mod.HarnessError):
        app_repo.removed_or_changed_lines(parent, fix)


def test_diff_removed_line_starting_with_dashes_is_content(
    tmp_path: Path,
) -> None:
    """D177 (3): hunk bodies are consumed by their declared line
    counts, so a removed content line that itself starts with
    `-- ` is content — its line number is recorded — and is never
    mistaken for a `--- ` header."""
    repo = init_repo(tmp_path)
    path = "src/notes.py"
    commit(repo, "Initial commit", ts(2026, 1, 10),
           {path: "l1\n-- old comment\nl3\n"})
    parent = commit(repo, "Introduce the counter", ts(2026, 2, 1),
                    {path: "l1\n-- old comment\nl3-bug\n"})
    fix = commit(repo, "Fix the comment", ts(2026, 8, 15),
                 {path: "l1\n-- new comment\nl3-bug\n"})
    app_repo = mod.AppRepo(repo)
    assert app_repo.removed_or_changed_lines(parent, fix) == {path: [2]}


def test_check_d_requires_both_sides_ready(tmp_path: Path) -> None:
    repo, _base, _b, mid, fix = standard_history(tmp_path)

    record = select(repo, fix, FakeReady({fix}))
    entry = record["walked"][-1]
    assert entry["failed_check"] == "d"
    assert entry["reason"] == "fix^ is not READY"
    assert entry["parent_ready"] is False and entry["fix_ready"] is True
    # The default fake failure is a typed `not supported:` — a
    # genuine (d) failure, classified and recorded per side (D177).
    assert entry["readiness"]["fix_parent"]["class"] == "not_supported"
    assert entry["readiness"]["fix"]["class"] == "ready"
    assert entry["readiness"]["fix"]["engine"] == "podman (fake)"
    assert entry["readiness"]["fix"]["contract_store_sha256"] == "c" * 64

    record = select(repo, fix, FakeReady({mid}))
    entry = record["walked"][-1]
    assert entry["failed_check"] == "d"
    assert entry["reason"] == "fix is not READY"

    record = select(repo, fix, FakeReady())
    entry = record["walked"][-1]
    assert entry["failed_check"] == "d"
    assert entry["reason"] == "fix^ and fix are not READY"


# ---------------------------------------------------------------------
# Check (d) classification (D48, per D177).
# ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "outcome,expected",
    [
        (side_outcome(ready=True), "ready"),
        (side_outcome(typed_reason="not supported: no frozen lockfile"),
         "not_supported"),
        # D190: only the `not supported:` prefix is a genuine
        # refusal; the unprefixed string falls through to unruled.
        (side_outcome(typed_reason="runtime not supported"), "unruled"),
        (side_outcome(typed_reason="harness error: git fetch failed"),
         "harness"),
        (side_outcome(
            typed_reason=(
                "SandboxError: measured run refused: no bridge "
                "network for the install phase"
            )),
         "environment"),
        (side_outcome(
            typed_reason="RuntimeError: git fetch failed",
            last_error="Could not resolve host github.com"),
         "environment"),
        (side_outcome(
            typed_reason="submodules nest past the depth limit (our gap)"),
         "our_gap"),
        (side_outcome(typed_reason="the frobnicate refused the widget"),
         "unruled"),
        (side_outcome(), "unruled"),
    ],
)
def test_classify_side(outcome: dict[str, Any], expected: str) -> None:
    assert mod.classify_side(outcome) == expected


def test_check_d_not_supported_moves_to_next_candidate(
    tmp_path: Path,
) -> None:
    """D177 (2): a typed `not supported:` side is a genuine (d)
    failure — recorded with its class, never retried — and the
    walk moves on to the next candidate, which is selected."""
    repo = init_repo(tmp_path)
    commit(repo, "Initial commit", ts(2026, 1, 10),
           {"src/app.py": "l1\nl2\nl3\n"})
    commit(repo, "Introduce the counter", ts(2026, 2, 1),
           {"src/app.py": "l1\nl2-bug\nl3\n"})
    add_tag(repo, "v1.0", ts(2026, 2, 2))
    bad_parent = commit(repo, "Introduce the meter", ts(2026, 3, 1),
                        {"src/app.py": "l1\nl2-bug\nl3-bug\n"})
    fix1 = commit(repo, "Fix the counter", ts(2026, 8, 10),
                  {"src/app.py": "l1\nl2-fixed\nl3-bug\n"})
    add_tag(repo, "v2.0", ts(2026, 8, 12), target=fix1)
    # fix2 changes the line fix1 wrote, so its B is fix1 itself.
    fix2 = commit(repo, "Fix the counter properly", ts(2026, 8, 20),
                  {"src/app.py": "l1\nl2-fixed2\nl3-bug\n"})
    fake = FakeReady(
        ready={fix1, fix2},
        scripted={
            bad_parent: [
                side_outcome(
                    typed_reason="not supported: stub runtime refused"
                )
            ]
        },
    )
    record = select(repo, fix2, fake)
    assert record["outcome"] == "selected"
    assert record["selected"]["fix"] == fix2
    assert record["selected"]["b"] == fix1
    walked = {e["sha"]: e for e in record["walked"]}
    entry1 = walked[fix1]
    assert entry1["failed_check"] == "d"
    assert entry1["readiness"]["fix_parent"]["class"] == "not_supported"
    assert entry1["readiness"]["fix"]["class"] == "ready"
    assert len(entry1["readiness"]["fix_parent"]["attempts"]) == 1
    # The not-supported side was provisioned exactly once: a typed
    # refusal is never retried.
    assert [c for c in fake.calls if c[0] == bad_parent] == [
        (bad_parent, fix1, ("fix^", "fix"))
    ]


@pytest.mark.parametrize(
    "typed_reason,expected_class",
    [
        ("harness error: git fetch failed in the clone: stub", "harness"),
        (
            "SandboxError: measured run refused: no bridge network "
            "for the install phase",
            "environment",
        ),
    ],
)
def test_check_d_retryable_failure_retries_then_selects(
    tmp_path: Path, typed_reason: str, expected_class: str
) -> None:
    """D177 (2): a harness/environment failure is retried per
    side (up to 2 further attempts); READY on a retry selects, and
    every attempt is journaled."""
    repo, _base, _b, mid, fix = standard_history(tmp_path)
    journal, _, _ = _open_journal(tmp_path, fix)
    fake = FakeReady(
        ready={mid},
        scripted={
            fix: [side_outcome(typed_reason=typed_reason),
                  side_outcome(ready=True)]
        },
    )
    record = select(repo, fix, fake, journal=journal)
    assert record["outcome"] == "selected"
    entry = record["walked"][-1]
    fix_side = entry["readiness"]["fix"]
    assert [a["class"] for a in fix_side["attempts"]] == [
        expected_class, "ready",
    ]
    assert fix_side["class"] == "ready"
    # The retry provisioned only the failing side.
    assert fake.calls[0] == (mid, fix, ("fix^", "fix"))
    assert fake.calls[1] == (mid, fix, ("fix",))
    # Every attempt journaled, per side, in order.
    journaled = journal.read_attempts()
    assert [(a["role"], a["class"]) for a in journaled] == [
        ("fix^", "ready"), ("fix", expected_class), ("fix", "ready"),
    ]
    assert all(a["commit"] == fix for a in journaled)


def test_check_d_our_gap_stops_run_not_rejected(tmp_path: Path) -> None:
    """D177 (2): an our-gap side stops the run for a ruling. The
    record's outcome is "stopped", the commit gains NO walk entry
    (it is not marked rejected — in the record or the journal),
    the stop is journaled as an event, and a resume with the gap
    fixed re-evaluates the same commit and selects it."""
    repo, _base, _b, mid, fix = standard_history(tmp_path)
    journal, _, _ = _open_journal(tmp_path, fix)
    fake = FakeReady(
        ready={mid},
        scripted={
            fix: [side_outcome(
                typed_reason="fixture render failed: boom (our gap)"
            )]
        },
    )
    record = select(repo, fix, fake, journal=journal)
    assert record["outcome"] == "stopped"
    assert record["stopped"]["sha"] == fix
    assert record["stopped"]["check"] == "d"
    assert record["stopped"]["role"] == "fix"
    assert record["stopped"]["class"] == "our_gap"
    assert record["stopped"]["retries_exhausted"] is False
    assert fix not in [e["sha"] for e in record["walked"]]
    assert fix not in [e["sha"] for e in journal.read_walk()]
    events = [
        json.loads(ln)
        for ln in (journal.dir / "events.jsonl").read_text().splitlines()
        if ln.strip()
    ]
    assert any(
        e.get("type") == "stop" and e.get("class") == "our_gap"
        and e.get("sha") == fix
        for e in events
    )
    # The stop's attempts were journaled too.
    assert [a["class"] for a in journal.read_attempts()
            if a["role"] == "fix"] == ["our_gap"]

    # Resume with the gap fixed: the same commit is re-evaluated
    # (nothing was rejected) and selected.
    journal2, resumed2, _ = _open_journal(tmp_path, fix)
    assert resumed2 is True
    record2 = select(repo, fix, FakeReady({mid, fix}), journal=journal2)
    assert record2["outcome"] == "selected"
    assert record2["selected"]["fix"] == fix


def test_check_d_unruled_class_stops_run(tmp_path: Path) -> None:
    """D177 (2): a failure matching no ruled class is unruled and
    stops the run the same way — it never steers the selection."""
    repo, _base, _b, mid, fix = standard_history(tmp_path)
    fake = FakeReady(
        ready={mid},
        scripted={
            fix: [side_outcome(
                typed_reason="the frobnicate refused the widget"
            )]
        },
    )
    record = select(repo, fix, fake)
    assert record["outcome"] == "stopped"
    assert record["stopped"]["class"] == "unruled"
    assert record["stopped"]["role"] == "fix"
    assert fix not in [e["sha"] for e in record["walked"]]


def test_check_d_unprefixed_runtime_not_supported_stops_run(
    tmp_path: Path,
) -> None:
    """D190 (1): a side whose typed reason is exactly "runtime not
    supported" — WITHOUT the `not supported:` prefix — is not a
    genuine refusal (D48 R1 / D98(7): only the prefix is). It
    falls through to unruled and stops the run for a ruling: the
    commit is NOT marked rejected (no walk entry, in the record
    or the journal), and the stop is journaled."""
    repo, _base, _b, mid, fix = standard_history(tmp_path)
    journal, _, _ = _open_journal(tmp_path, fix)
    fake = FakeReady(
        ready={mid},
        scripted={
            fix: [side_outcome(typed_reason="runtime not supported")]
        },
    )
    record = select(repo, fix, fake, journal=journal)
    assert record["outcome"] == "stopped"
    assert record["stopped"]["sha"] == fix
    assert record["stopped"]["check"] == "d"
    assert record["stopped"]["role"] == "fix"
    assert record["stopped"]["class"] == "unruled"
    assert record["stopped"]["retries_exhausted"] is False
    assert fix not in [e["sha"] for e in record["walked"]]
    assert fix not in [e["sha"] for e in journal.read_walk()]
    events = [
        json.loads(ln)
        for ln in (journal.dir / "events.jsonl").read_text().splitlines()
        if ln.strip()
    ]
    assert any(
        e.get("type") == "stop" and e.get("class") == "unruled"
        and e.get("sha") == fix
        for e in events
    )
    assert [a["class"] for a in journal.read_attempts()
            if a["role"] == "fix"] == ["unruled"]


def test_check_d_retries_exhausted_stops_run(tmp_path: Path) -> None:
    """D177 (2): a harness/environment side still failing after
    its 3 attempts stops the run instead of being rejected — the
    environment must not steer the selection."""
    repo, _base, _b, mid, fix = standard_history(tmp_path)
    fake = FakeReady(
        ready={mid},
        scripted={
            fix: [side_outcome(
                typed_reason="harness error: git fetch failed: stub"
            )]
        },
    )
    record = select(repo, fix, fake)
    assert record["outcome"] == "stopped"
    assert record["stopped"]["class"] == "harness"
    assert record["stopped"]["retries_exhausted"] is True
    attempts = record["stopped"]["readiness"]["fix"]["attempts"]
    assert [a["class"] for a in attempts] == ["harness"] * 3
    assert [a["attempt"] for a in attempts] == [1, 2, 3]
    assert fix not in [e["sha"] for e in record["walked"]]


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
    assert record["merge_base_with_main"] is None
    assert record["app"]["head_sha"] == fix
    assert record["app"]["app_path"] == "src"
    assert record["boundary"] == "2026-07-01T00:00:00Z"
    assert record["walk"] == "first-parent"
    for entry in record["walked"]:
        if entry["outcome"] == "rejected":
            assert entry["failed_check"] in ("a", "b", "c", "d", "e")
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


# ---------------------------------------------------------------------
# Check (d) through readiness.yml on the GitHub runners (D194).
# The dispatcher is a fake: no network, no gh. Each scripted run
# is one dispatch: {"wait": bool, "rows": list | None}.
# ---------------------------------------------------------------------


def gh_row(
    role: str,
    *,
    status: str = "READY",
    typed_reason: str | None = None,
    last_error: str | None = None,
) -> dict[str, Any]:
    """One readiness row as the rows artifact carries it (the
    pair-mode shape of D195: the row's own fields plus its
    engine/isolation/network/contract evidence)."""
    return {
        "kind": "readiness",
        "role": role,
        "status": status,
        "typed_reason": typed_reason,
        "last_error": last_error,
        "network_mode": "none+socket",
        "engine": "podman 5.4.0 (fake runner)",
        "isolation_mode": "userns-remap",
        "contract_store_sha256": "d" * 64,
    }


class FakeDispatcher:
    """A scripted readiness.yml dispatcher (D194).

    dispatch_pair records the call and hands out run IDs in
    order (7000, 7001, ...); wait and fetch_rows replay the
    scripted run's outcome. An unscripted dispatch defaults to a
    successful run with no rows."""

    def __init__(self, scripted: list[dict[str, Any]] | None = None) -> None:
        self.scripted = list(scripted or [])
        self.dispatches: list[dict[str, Any]] = []
        self._by_id: dict[str, dict[str, Any]] = {}

    def dispatch_pair(
        self,
        *,
        repo: str,
        fix_parent_sha: str,
        fix_sha: str,
        ref: str,
    ) -> str:
        run_id = str(7000 + len(self.dispatches))
        self.dispatches.append({
            "repo": repo,
            "fix_parent_sha": fix_parent_sha,
            "fix_sha": fix_sha,
            "ref": ref,
            "run_id": run_id,
        })
        self._by_id[run_id] = (
            self.scripted.pop(0)
            if self.scripted
            else {"wait": True, "rows": []}
        )
        return run_id

    def wait(self, run_id: str) -> bool:
        return bool(self._by_id[run_id].get("wait", True))

    def fetch_rows(
        self, run_id: str, *, repo: str
    ) -> list[dict[str, Any]] | None:
        return self._by_id[run_id].get("rows")


def github_checker(fake: FakeDispatcher) -> Any:
    return mod.make_github_pair_checker(
        repo_name="paperless-ngx/paperless-ngx", dispatcher=fake
    )


def test_github_check_d_happy_path() -> None:
    """Dispatch -> rows: both sides READY, the rows' evidence
    carried through verbatim, the run ID on every outcome, and
    the dispatch made from main with the pair inputs."""
    fake = FakeDispatcher([
        {"wait": True, "rows": [gh_row("fix^"), gh_row("fix")]},
    ])
    outcomes = github_checker(fake)("a" * 40, "b" * 40, ("fix^", "fix"))
    assert fake.dispatches == [{
        "repo": "paperless-ngx/paperless-ngx",
        "fix_parent_sha": "a" * 40,
        "fix_sha": "b" * 40,
        "ref": "main",
        "run_id": "7000",
    }]
    for role in ("fix^", "fix"):
        outcome = outcomes[role]
        assert outcome["ready"] is True
        assert outcome["status"] == "READY"
        assert mod.classify_side(outcome) == "ready"
        assert outcome["engine"] == "podman 5.4.0 (fake runner)"
        assert outcome["isolation_mode"] == "userns-remap"
        assert outcome["network_mode"] == "none+socket"
        assert "--net=none" in outcome["network_none_evidence"]
        assert outcome["contract_store_sha256"] == "d" * 64
        assert outcome["workflow_run_id"] == "7000"


def test_github_check_d_not_ready_rows_classify() -> None:
    """READY is read from the row's status: a BUILD_FAILED row
    with a typed `not supported:` reason is not ready and
    classifies as the genuine refusal it is."""
    fake = FakeDispatcher([
        {
            "wait": True,
            "rows": [
                gh_row("fix^"),
                gh_row(
                    "fix",
                    status="BUILD_FAILED",
                    typed_reason="not supported: no frozen lockfile",
                ),
            ],
        },
    ])
    outcomes = github_checker(fake)("a" * 40, "b" * 40, ("fix^", "fix"))
    assert outcomes["fix^"]["ready"] is True
    assert outcomes["fix"]["ready"] is False
    assert outcomes["fix"]["status"] == "BUILD_FAILED"
    assert mod.classify_side(outcomes["fix"]) == "not_supported"


def test_github_check_d_failed_workflow_is_environment() -> None:
    """A workflow run that does not complete successfully yields
    environment-class outcomes (D194) — never a guessed READY —
    with the failed run's ID still recorded."""
    fake = FakeDispatcher([{"wait": False}])
    outcomes = github_checker(fake)("a" * 40, "b" * 40, ("fix^", "fix"))
    for role in ("fix^", "fix"):
        assert outcomes[role]["ready"] is False
        assert mod.classify_side(outcomes[role]) == "environment"
        assert outcomes[role]["workflow_run_id"] == "7000"


def test_github_check_d_missing_artifact_is_environment() -> None:
    """A successful run whose rows artifact is missing or
    unreadable yields environment-class outcomes (D194)."""
    fake = FakeDispatcher([{"wait": True, "rows": None}])
    outcomes = github_checker(fake)("a" * 40, "b" * 40, ("fix^", "fix"))
    for role in ("fix^", "fix"):
        assert outcomes[role]["ready"] is False
        assert mod.classify_side(outcomes[role]) == "environment"
        assert outcomes[role]["workflow_run_id"] == "7000"


def test_github_check_d_dispatch_failure_is_environment() -> None:
    """A dispatch that raises (gh down, workflow missing) yields
    environment-class outcomes with no run ID — the ruled retry
    path, not a crash and not a READY."""

    class RaisingDispatcher(FakeDispatcher):
        def dispatch_pair(self, **kw: Any) -> str:
            raise RuntimeError("gh: executable not found (fake)")

    outcomes = github_checker(RaisingDispatcher())(
        "a" * 40, "b" * 40, ("fix^", "fix")
    )
    for role in ("fix^", "fix"):
        assert outcomes[role]["ready"] is False
        assert mod.classify_side(outcomes[role]) == "environment"
        assert outcomes[role]["workflow_run_id"] is None


@pytest.mark.parametrize(
    "bad_sha",
    ["abc123", "A" * 40, "g" * 40, "a" * 39, "a" * 41, ""],
)
def test_github_check_d_refuses_non_sha40(bad_sha: str) -> None:
    """Fail closed before dispatch: a SHA that is not exactly 40
    lowercase hex is refused (D194), on either side, and nothing
    is dispatched."""
    fake = FakeDispatcher()
    checker = github_checker(fake)
    with pytest.raises(mod.HarnessError):
        checker(bad_sha, "b" * 40, ("fix^", "fix"))
    with pytest.raises(mod.HarnessError):
        checker("a" * 40, bad_sha, ("fix^", "fix"))
    with pytest.raises(mod.HarnessError):
        mod._validate_sha40(bad_sha, what="sha")
    assert fake.dispatches == []


def test_github_dispatch_ref_must_be_main() -> None:
    """The real dispatcher refuses any ref but main before any
    gh call is made (D194: dispatches come from main only)."""
    dispatcher = mod.GhReadinessDispatcher()
    with pytest.raises(mod.HarnessError):
        dispatcher.dispatch_pair(
            repo="paperless-ngx/paperless-ngx",
            fix_parent_sha="a" * 40,
            fix_sha="b" * 40,
            ref="feat/not-main",
        )


def test_github_check_d_run_ids_journaled_per_attempt(
    tmp_path: Path,
) -> None:
    """Through the walk: a failed workflow run is one journaled
    attempt (class environment, its run ID recorded); the retry
    is a NEW dispatch with a NEW run ID; READY on the retry
    selects. The record's per-side attempts carry the run IDs."""
    repo, _base, _b, mid, fix = standard_history(tmp_path)
    journal, _, _ = _open_journal(tmp_path, fix)
    fake = FakeDispatcher([
        {"wait": False},
        {"wait": True, "rows": [gh_row("fix^"), gh_row("fix")]},
    ])
    record = select(repo, fix, github_checker(fake), journal=journal)
    assert record["outcome"] == "selected"
    assert [d["run_id"] for d in fake.dispatches] == ["7000", "7001"]
    assert all(d["ref"] == "main" for d in fake.dispatches)
    journaled = journal.read_attempts()
    assert [
        (a["role"], a["class"], a["workflow_run_id"])
        for a in journaled
    ] == [
        ("fix^", "environment", "7000"),
        ("fix", "environment", "7000"),
        ("fix^", "ready", "7001"),
        ("fix", "ready", "7001"),
    ]
    entry = record["walked"][-1]
    fix_side = entry["readiness"]["fix"]
    assert [a["class"] for a in fix_side["attempts"]] == [
        "environment", "ready",
    ]
    assert [a["workflow_run_id"] for a in fix_side["attempts"]] == [
        "7000", "7001",
    ]


REAL_ROWS_FIXTURE = (
    Path(__file__).resolve().parent / "fixtures" / "readiness_rows_real.json"
)
REAL_CONTRACT_SHA = (
    "eb60559b9fdd879e5cb8ca30d16c7cbfb1cea356e9385b8e367a249ab7c19e6a"
)


def _patch_gh_download(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_gh(*args: str) -> subprocess.CompletedProcess[str]:
        dest = Path(args[args.index("--dir") + 1])
        (dest / "rows-attempt1.json").write_text(
            REAL_ROWS_FIXTURE.read_text()
        )
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(mod, "_gh", fake_gh)


def test_gh_fetch_rows_reads_real_rows_artifact_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real artifact is an object {kind, repo, attempt,
    network_mode, rows, regd_summaries}; the reader must return
    its rows list, not treat it as unreadable."""
    _patch_gh_download(monkeypatch)
    rows = mod.GhReadinessDispatcher().fetch_rows(
        "39321472420", repo="paperless-ngx/paperless-ngx"
    )
    assert rows is not None
    assert [r["role"] for r in rows] == ["fix^", "fix"]


def test_gh_check_d_real_rows_artifact_both_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_gh_download(monkeypatch)
    real = mod.GhReadinessDispatcher()

    class RealParseDispatcher(FakeDispatcher):
        def fetch_rows(
            self, run_id: str, *, repo: str
        ) -> list[dict[str, Any]] | None:
            return real.fetch_rows(run_id, repo=repo)

    fake = RealParseDispatcher([{"wait": True}])
    outcomes = github_checker(fake)("a" * 40, "b" * 40, ("fix^", "fix"))
    for role in ("fix^", "fix"):
        assert outcomes[role]["ready"] is True
        assert outcomes[role]["isolation_mode"] == "userns-remap"
        assert outcomes[role]["network_mode"] == "none+socket"
        assert outcomes[role]["contract_store_sha256"] == REAL_CONTRACT_SHA


def test_gh_fetch_rows_list_and_other_shapes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def make(payload: str) -> Any:
        def fake_gh(*args: str) -> subprocess.CompletedProcess[str]:
            dest = Path(args[args.index("--dir") + 1])
            (dest / "rows-attempt1.json").write_text(payload)
            return subprocess.CompletedProcess(args, 0, "", "")

        return fake_gh

    d = mod.GhReadinessDispatcher()
    monkeypatch.setattr(mod, "_gh", make('[{"role": "fix"}]'))
    assert d.fetch_rows("1", repo="o/r") == [{"role": "fix"}]
    monkeypatch.setattr(mod, "_gh", make('{"rows": "x"}'))
    assert d.fetch_rows("1", repo="o/r") is None
    monkeypatch.setattr(mod, "_gh", make('"x"'))
    assert d.fetch_rows("1", repo="o/r") is None


# ---------------------------------------------------------------------
# R-SIZE (D253): check (e), the author prompt size budget.
# ---------------------------------------------------------------------


def two_fix_history(tmp_path: Path) -> tuple[Path, str, str, str, str]:
    """base -> B (two buggy lines, tagged) -> fix1 (Aug 1) -> fix2
    (Aug 15). Both fixes blame B and both pass (a)-(c)."""
    repo = init_repo(tmp_path)
    base = commit(repo, "Initial commit", ts(2026, 1, 10),
                  {"src/app.py": "l1\nl2\nl3\nl4\n"})
    b = commit(repo, "Introduce the counter", ts(2026, 2, 1),
               {"src/app.py": "l1\nl2-bug\nl3\nl4-bug\n"})
    add_tag(repo, "v1.0", ts(2026, 2, 2))
    fix1 = commit(repo, "Fix the first counter", ts(2026, 8, 1),
                  {"src/app.py": "l1\nl2-fixed\nl3\nl4-bug\n"})
    fix2 = commit(repo, "Fix the second counter", ts(2026, 8, 15),
                  {"src/app.py": "l1\nl2-fixed\nl3\nl4-fixed\n"})
    return repo, base, b, fix1, fix2


class SizeScript:
    """Injected check (e): returns the scripted sizes in order and
    records the (B^, B) range each call was made for."""

    def __init__(self, sizes: list[int]) -> None:
        self.sizes = list(sizes)
        self.calls: list[tuple[str, str]] = []

    def __call__(self, parent: str, b: str) -> int:
        self.calls.append((parent, b))
        return self.sizes.pop(0)


def test_r_size_budget_constant_is_the_signed_80000() -> None:
    assert mod.R_SIZE_MAX_PROMPT_CHARS == 80000


def test_check_e_boundary_80000_selected_80001_rejected(
    tmp_path: Path,
) -> None:
    repo, base, b, fix1, fix2 = two_fix_history(tmp_path)
    # fix1's parent is B; fix2's parent is fix1.
    ready = FakeReady({b, fix1, fix2})
    script = SizeScript([80001, 80000])
    record = select(repo, fix2, ready, prompt_size=script)

    walked = {e["sha"]: e for e in record["walked"]}
    over = walked[fix1]
    assert over["outcome"] == "rejected"
    assert over["failed_check"] == "e"
    assert over["reason"] == "e: author prompt too large (80001 chars > 80000)"
    assert over["author_prompt_chars"] == 80001
    # The walk continued to the next commit and selected it.
    fits = walked[fix2]
    assert fits["outcome"] == "selected"
    assert fits["author_prompt_chars"] == 80000
    assert record["outcome"] == "selected"
    assert record["selected"]["fix"] == fix2
    # The range handed to the builder is B^..B (B's first parent).
    assert script.calls == [(base, b), (base, b)]
    assert record["rejected_by_check"]["e"] == 1


def test_check_e_not_evaluated_for_commits_failing_a_to_d(
    tmp_path: Path,
) -> None:
    repo, _base, _b, mid, fix = standard_history(tmp_path)
    script = SizeScript([])  # any call would raise IndexError
    # mid fails (a); fix fails (d) because nothing is READY.
    record = select(repo, fix, FakeReady(), prompt_size=script)
    assert script.calls == []
    for entry in record["walked"]:
        assert "author_prompt_chars" not in entry
    assert record["walked"][-1]["failed_check"] == "d"
    assert record["rejected_by_check"]["e"] == 0


def test_check_e_size_recorded_without_content(tmp_path: Path) -> None:
    repo, _base, _b, mid, fix = standard_history(tmp_path)
    record = select(repo, fix, FakeReady({mid, fix}),
                    prompt_size=SizeScript([42]))
    assert record["walked"][-1]["author_prompt_chars"] == 42
    tally = record["rejected_by_check"]
    assert tally == {"a": 1, "b": 0, "c": 0, "d": 0, "e": 0, "selected": 1}


def test_main_prints_tally_with_e_and_no_subject(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Build the real history and drive main() with injected pieces.
    root = tmp_path / "hist"
    root.mkdir()
    repo, _base, b, fix1, fix2 = two_fix_history(root)
    survivors = tmp_path / "survivors.json"
    survivors.write_text(json.dumps([{
        "full_name": "o/r", "head_sha": fix2, "default_branch": "main",
        "stacks": {"python": {"passed": True, "app_path": "src"}},
    }]))
    monkeypatch.setattr(mod, "_clone_app",
                        lambda *a, **k: mod.AppRepo(repo))
    monkeypatch.setattr(mod, "make_provision_pair_checker",
                        lambda **k: FakeReady({b, fix1, fix2}))
    monkeypatch.setattr(mod, "make_author_prompt_size_builder",
                        lambda **k: SizeScript([90000, 10]))
    rc = mod.main([
        "--repo", "o/r", "--survivors", str(survivors),
        "--out", str(tmp_path / "out" / "rec.json"),
        "--main-sha", "f" * 40,
    ])
    out = capsys.readouterr().out
    assert rc == 0
    assert "tally: a=0 b=0 c=0 d=0 e=1 selected=1" in out
    assert "Fix the" not in out
    assert "author prompt too large (90000 chars > 80000)" in out


def test_default_builder_builds_prompt_through_orchestrator(
    tmp_path: Path,
) -> None:
    """The real (e) builder runs Orchestrator._build_bundle for a
    tiny synthetic repo and a temporary contract-store entry:
    positive int, no network, no model, no sandbox."""
    root = Path(__file__).resolve().parents[1]
    store = tmp_path / "store" / "golden__node-express-sqlite.yml"
    store.parent.mkdir()
    store.write_bytes(
        (root / "readiness" / "contracts"
         / "golden__node-express-sqlite.yml").read_bytes()
    )
    repo = init_repo(tmp_path)
    base = commit(repo, "Initial commit", ts(2026, 1, 10),
                  {"server.js": "const a = 1;\n", "package.json": "{}\n"})
    b = commit(repo, "Introduce the counter", ts(2026, 2, 1),
               {"server.js": "const a = 2;\nconst b = 3;\n"})
    builder = mod.make_author_prompt_size_builder(
        repo_name="golden/node-express-sqlite",
        repo=mod.AppRepo(repo),
        contract_path=store,
    )
    n = builder(base, b)
    assert isinstance(n, int) and n > 0
    # Deterministic: the same range builds the same prompt length.
    assert builder(base, b) == n


def test_default_builder_refusal_stops_the_walk_not_rejects(
    tmp_path: Path,
) -> None:
    repo = init_repo(tmp_path)
    base = commit(repo, "Initial commit", ts(2026, 1, 10), {"a.js": "1\n"})
    builder = mod.make_author_prompt_size_builder(
        repo_name="o/r",
        repo=mod.AppRepo(repo),
        contract_path=tmp_path / "missing.yml",
    )
    # An empty range (and a missing contract) is an environment/our-gap
    # refusal: HarnessError, never a size.
    with pytest.raises(mod.HarnessError, match="check \\(e\\) refused"):
        builder(base, base)


def test_default_builder_declares_benign_contract_env_keys_public(
    tmp_path: Path,
) -> None:
    """D203 class: a benign contract env value that also appears in
    the app's own files must not trip the bundle leak gate, while a
    credential-named key stays forbidden."""
    root = Path(__file__).resolve().parents[1]
    text = (root / "readiness" / "contracts"
            / "golden__node-express-sqlite.yml").read_text()
    text = text.replace(
        "env:\n",
        "env:\n  APP_BACKEND_CLASS: \"pkg.backends.BenignBackendClass\"\n",
        1,
    )
    store = tmp_path / "store.yml"
    store.write_text(text)
    repo = init_repo(tmp_path)
    base = commit(repo, "Initial commit", ts(2026, 1, 10),
                  {"server.js": "x\n", "package.json": "{}\n"})
    b = commit(repo, "Introduce the counter", ts(2026, 2, 1),
               {"server.js": "// pkg.backends.BenignBackendClass\n"})
    builder = mod.make_author_prompt_size_builder(
        repo_name="golden/node-express-sqlite",
        repo=mod.AppRepo(repo),
        contract_path=store,
    )
    assert builder(base, b) > 0
    # With nothing declared public the same range is refused by the
    # leak gate (a harness stop, never a size or a rejection).
    strict = mod.make_author_prompt_size_builder(
        repo_name="golden/node-express-sqlite",
        repo=mod.AppRepo(repo),
        contract_path=store,
        public_env_keys=[],
    )
    with pytest.raises(mod.HarnessError, match="secret leak"):
        strict(base, b)
