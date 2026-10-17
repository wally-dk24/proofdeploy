#!/usr/bin/env python3
"""WO-6 bug selection (plan 1.5(d)): REG-C item 7, implemented exactly
as registered in docs/registration/wo6-selection-rule.md (REG-C,
2026-10-11; Addendum 2, 2026-10-14, leaves items 4-10 unchanged), with
the reviewer's D169 rulings (2026-10-17) on the two points the
registration leaves open. D166 part B commissioned the script; no
live run happens until the reviewer accepts the PR carrying it.

REG-C item 7, verbatim:

    "Bug selection in that app (script, not a person). Walk the
    default branch's commits in committer-date order, starting
    strictly after 2026-07-01. The first commit that passes all of
    these is the fix: subject matches (?i)\\bfix(e[sd])?\\b; changes
    at least one non-test, non-doc source file; B = the most recent
    commit that last touched any line the fix removes or changes
    (git blame at fix^), B must exist, B != fix^ is allowed, and B
    must be contained in a release tag created before the fix (git
    tag --contains), which is the "shipped" test; fix^ and the fix
    both reach READY. If 200 commits are walked without a pass, the
    next READY candidate is used."

D169 rulings implemented here (implementation of item 7; no
rule-text change):

- Walk topology (Q1): FIRST-PARENT. The walk is
  `git rev-list --first-parent <default branch>` from the recorded
  HEAD SHA, committer date > 2026-07-01T00:00:00Z, sorted by
  committer date, oldest first. For a merge commit: fix^ is its
  first parent; "lines the fix removes or changes" is the diff
  against the first parent; the subject is the merge's own subject.
  A qualifying fix that exists only on a merged side branch is
  therefore never walked; its merge commit is evaluated instead.
- File classes (Q2): path-based, generic, checked in the order
  TEST, DOC, OTHER, SOURCE; each path gets exactly one class, and
  check (b) passes iff at least one changed path is SOURCE. See
  classify_path() for the exact rules.
- Check (c) stays literal: B is the most recent commit that last
  touched ANY line the fix removes or changes, in any file. The
  unmerged 2026-10-10 "v3" draft's extras (skip add-only fixes,
  exclude merge/revert fixes, b_parent) are NOT built: an add-only
  fix fails (c) naturally, because it removes or changes no line,
  so no B exists.

The checks, in order (the record names the FIRST failing one):

  a. the subject matches (?i)\\bfix(e[sd])?\\b;
  b. at least one changed path classifies SOURCE;
  c. B exists (the fix removes or changes at least one line, and
     git blame at fix^ attributes those lines), and B is contained
     in a release tag whose creation date is strictly before the
     fix's committer date (annotated tag: its tagger date;
     lightweight tag: the tagged commit's committer date — git's
     %(creatordate) in both cases);
  d. fix^ and fix both reach READY through the real sandboxed
     runner (provisioning-only readiness, readiness/provision.py —
     the measured path's own Orchestrator._provision_side_sandbox,
     driven exactly as readiness.yml drives it). The run executing
     this script is launched from a checkout of current main (the
     record carries that main SHA), per D155.

CONTAMINATION GUARD: this script records commit metadata and check
outcomes only. It never prints or writes the fix's diff, any file
content, test content, or any commit message body — to stdout, to
the journal, or to the record. Subject lines are recorded because
check (a) is defined on them; nothing else of a commit's text is
read (git is asked for hashes, dates, subjects, path names, and
blame attributions only). Diff hunks are parsed in-process for
their old-side line numbers and discarded.

Run mechanics (for the future accepted run; building them is all
this PR does):

- Single-writer lock (D140): an flock(LOCK_EX | LOCK_NB) on
  <journal>/run.lock, held for the process lifetime; a second
  writer refuses instead of interleaving journal lines.
- Resumable journal (D74 guarantees, mirroring
  scripts/wo6/regc_add2_run.py): header.json identifies the run
  (script sha256, main SHA, app repo, app HEAD SHA from the
  committed ordered list); walk.jsonl gains one line per examined
  commit as it completes; events.jsonl records starts, resumes and
  the completed exit. A death loses nothing already examined; a
  resume re-examines nothing already recorded and refuses when
  the header names a different script, main SHA or app HEAD. The
  output record is built only from the journal.
- Input: the signed-off app from the committed ordered list
  (docs/registration/wo6-regc-add2-closeout-2026-10-17/
  regc-add2-survivors.json): repo, HEAD SHA, default branch and
  app.path are read from the list entry, never typed at the
  command line (D166: paperless-ngx/paperless-ngx @
  0cb2cdbfdd91a2078236f8851cd310261284591c, app.path src).

Exit status: 0 when the run completed (a selection and an
exhaustion are both results); 2 when the run itself failed
(journal identity mismatch, lock held, clone/harness errors).
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# REG-C item 7: "starting strictly after 2026-07-01". D169 pins the
# instant: committer date > 2026-07-01T00:00:00Z. A commit dated
# exactly at the boundary is NOT walked.
BOUNDARY_TS = int(datetime(2026, 7, 1, tzinfo=timezone.utc).timestamp())
BOUNDARY_ISO = "2026-07-01T00:00:00Z"

# REG-C item 7: "If 200 commits are walked without a pass, the
# next READY candidate is used."
MAX_WALKED = 200

# REG-C item 7 / check (a), verbatim pattern.
FIX_SUBJECT_RE = re.compile(r"\bfix(e[sd])?\b", re.IGNORECASE)

# The empty tree: the diff base for a parentless commit.
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

DEFAULT_SURVIVORS = (
    "docs/registration/wo6-regc-add2-closeout-2026-10-17/"
    "regc-add2-survivors.json"
)
DEFAULT_REPO = "paperless-ngx/paperless-ngx"  # D166 sign-off.


class HarnessError(Exception):
    """The run itself could not proceed (clone/git/harness failure).

    A harness failure is never a check outcome: it aborts the run
    with exit status 2 and is journaled only as a death (no exit
    record), exactly as in regc_add2_run.py."""


class JournalMismatch(Exception):
    """A resume was attempted against a journal whose header names a
    different script, main SHA or app HEAD. One journal is one run,
    under one identity (D74/A5)."""


class SingleWriterError(Exception):
    """Another bug-select run holds this journal's lock (D140)."""


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _script_sha256() -> str:
    return hashlib.sha256(Path(__file__).resolve().read_bytes()).hexdigest()


def _detect_main_sha() -> str | None:
    """HEAD of the ProofDeploy checkout containing this script (the
    run record names the main SHA it ran from); None when it cannot
    be determined."""
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).resolve().parents[2],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    sha = proc.stdout.strip()
    return sha if proc.returncode == 0 and sha else None


# ---------------------------------------------------------------------
# D169 Q2 file classes. Checked in the order TEST, DOC, OTHER,
# SOURCE; each path gets exactly one class. Case-insensitivity
# applies exactly where D169 states it (the TEST segment rule).
# ---------------------------------------------------------------------

_TEST_SEGMENTS = {"test", "tests", "testing", "__tests__", "spec", "specs", "e2e"}
_TEST_SEGMENT_SUFFIXES = ("_test", "_tests", "_testing")
_DOC_SEGMENTS = {"doc", "docs"}
_DOC_EXTENSIONS = {".md", ".rst", ".adoc"}
_DOC_BASENAME_PREFIXES = ("README", "CHANGELOG", "LICENSE", "AUTHORS", "CONTRIBUTING")
_TRANSLATION_EXTENSIONS = {".po", ".pot", ".mo", ".xlf", ".xliff"}
_TRANSLATION_SEGMENTS = {"locale", "locales", "i18n"}
_LOCK_BASENAMES = {"pyproject.toml", "uv.lock", "poetry.lock", "package.json", "pnpm-lock.yaml"}
SOURCE_EXTENSIONS = {
    ".py", ".pyi", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx",
    ".vue", ".svelte", ".html", ".htm", ".css", ".scss", ".sass",
    ".less", ".go", ".java", ".kt", ".rb", ".php", ".rs", ".cs",
}


def _extension(basename: str) -> str:
    """The basename's final extension, dot included, lowercased;
    "" when the basename has no dot."""
    if "." not in basename:
        return ""
    return "." + basename.rsplit(".", 1)[1].lower()


def _is_test_path(segments: list[str], basename: str) -> bool:
    for seg in segments:
        low = seg.lower()
        if low in _TEST_SEGMENTS or low.endswith(_TEST_SEGMENT_SUFFIXES):
            return True
    if basename == "conftest.py":
        return True
    if basename.startswith("test_") and basename.endswith(".py"):
        return True
    if basename.endswith("_test.py"):
        return True
    parts = basename.split(".")
    return len(parts) >= 3 and parts[-2] in ("test", "spec")


def _is_doc_path(segments: list[str], basename: str) -> bool:
    if any(seg in _DOC_SEGMENTS for seg in segments):
        return True
    if _extension(basename) in _DOC_EXTENSIONS:
        return True
    return basename.startswith(_DOC_BASENAME_PREFIXES)


def _is_other_path(segments: list[str], basename: str) -> bool:
    """OTHER: everything D169 names explicitly, plus — as its last
    rule — any extension not in the SOURCE list."""
    ext = _extension(basename)
    if ext in _TRANSLATION_EXTENSIONS:
        return True
    if any(seg in _TRANSLATION_SEGMENTS for seg in segments):
        return True
    if (
        (basename.startswith("requirements") and basename.endswith(".txt"))
        or basename in _LOCK_BASENAMES
        or basename.startswith("Pipfile")
        or (basename.endswith("-lock.json") and len(basename) > len("-lock.json"))
        or basename.endswith(".lock")
    ):
        return True
    if (
        segments[0] == ".github"
        or basename.startswith("Dockerfile")
        or basename.startswith("docker-compose")
        or any(seg == "docker" for seg in segments)
    ):
        return True
    if basename.endswith(".txt"):
        return True
    return ext not in SOURCE_EXTENSIONS


def classify_path(path: str) -> str:
    """D169 Q2: exactly one of TEST, DOC, OTHER, SOURCE per path."""
    segments = path.split("/")
    basename = segments[-1]
    if _is_test_path(segments, basename):
        return "TEST"
    if _is_doc_path(segments, basename):
        return "DOC"
    if _is_other_path(segments, basename):
        return "OTHER"
    return "SOURCE"


def subject_matches(subject: str) -> bool:
    """Check (a): the REG-C item 7 pattern, verbatim."""
    return FIX_SUBJECT_RE.search(subject) is not None


# ---------------------------------------------------------------------
# Git access to the app clone. Only metadata ever leaves git:
# hashes, committer timestamps, subject lines, path names, blame
# attributions and tag dates. Diff hunks are parsed for old-side
# line numbers in-process and discarded (contamination guard).
# ---------------------------------------------------------------------


@dataclass(frozen=True)
class CommitMeta:
    sha: str
    committer_ts: int
    subject: str


class AppRepo:
    def __init__(self, path: Path) -> None:
        self.path = path

    def _git(self, *args: str) -> str:
        proc = subprocess.run(
            ["git", "-C", str(self.path), *args],
            capture_output=True,
            text=True,
            timeout=900,
        )
        if proc.returncode != 0:
            raise HarnessError(
                f"git {' '.join(args)} failed in {self.path}: "
                f"{proc.stderr.strip()}"
            )
        return proc.stdout

    def has_commit(self, sha: str) -> bool:
        proc = subprocess.run(
            ["git", "-C", str(self.path), "cat-file", "-e", f"{sha}^{{commit}}"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        return proc.returncode == 0

    def walk_order(self, head_sha: str) -> list[CommitMeta]:
        """D169 Q1: `git rev-list --first-parent` from the recorded
        HEAD, committer date strictly after the boundary, sorted by
        committer date, oldest first."""
        raw = self._git("rev-list", "--first-parent", head_sha)
        shas = [ln.strip() for ln in raw.splitlines() if ln.strip()]
        metas: list[CommitMeta] = []
        for i in range(0, len(shas), 100):
            chunk = shas[i : i + 100]
            out = self._git(
                "show", "-s", "--format=%ct %s", *chunk
            )
            lines = out.splitlines()
            if len(lines) != len(chunk):
                raise HarnessError(
                    "git show -s returned "
                    f"{len(lines)} lines for {len(chunk)} commits"
                )
            for sha, line in zip(chunk, lines, strict=True):
                ts_text, _, subject = line.partition(" ")
                metas.append(
                    CommitMeta(
                        sha=sha,
                        committer_ts=int(ts_text),
                        subject=subject,
                    )
                )
        walked = [m for m in metas if m.committer_ts > BOUNDARY_TS]
        walked.sort(key=lambda m: m.committer_ts)
        return walked

    def first_parent(self, sha: str) -> str | None:
        out = self._git("show", "-s", "--format=%P", sha).strip()
        parents = out.split()
        return parents[0] if parents else None

    def changed_paths(self, parent: str | None, fix: str) -> list[str]:
        base = parent if parent is not None else EMPTY_TREE
        out = self._git(
            "-c", "core.quotePath=false",
            "diff", "--name-only", "--no-renames", base, fix,
        )
        return [ln for ln in out.splitlines() if ln]

    def removed_or_changed_lines(
        self, parent: str | None, fix: str
    ) -> dict[str, list[int]]:
        """Old-side (fix^) line numbers the fix removes or changes,
        per old path. Pure additions contribute nothing. Parsed
        from -U0 hunks; the hunk text itself is discarded.

        The parse FAILS CLOSED (D177): the diff is taken with
        core.quotePath=false so ordinary paths are never C-quoted,
        and any header the parser cannot account for raises
        HarnessError instead of silently dropping removed lines —
        a C-quoted path header (git C-quotes a path containing a
        double quote or control characters even with quotePath
        off), any other unparseable `--- ` header followed by a
        hunk, a malformed hunk header, or a hunk claiming old-side
        lines under a /dev/null (added-file) header. A silent drop
        would corrupt the blamed set and steer check (c). Hunk
        bodies are consumed by their declared line counts, so a
        removed content line that itself starts with `-- ` is
        content, never a header."""
        base = parent if parent is not None else EMPTY_TREE
        out = self._git(
            "-c", "core.quotePath=false",
            "diff", "-U0", "--no-renames", base, fix,
        )
        result: dict[str, list[int]] = {}
        current: str | None = None
        # The last `--- ` header's kind: "path", "devnull", "bad",
        # or None before any file header.
        header: str | None = None
        hunk_left = 0  # content lines left in the current hunk
        for line in out.splitlines():
            if hunk_left > 0:
                if line.startswith("\\"):
                    continue  # "\ No newline at end of file"
                if line[:1] in ("-", "+"):
                    hunk_left -= 1
                    continue
                raise HarnessError(
                    f"unexpected line inside a diff hunk: {line[:80]}"
                )
            if line.startswith("--- "):
                rest = line[4:]
                if rest.startswith('"'):
                    raise HarnessError(
                        "diff path header is C-quoted and cannot "
                        f"be parsed safely: {line[:80]}"
                    )
                if rest == "/dev/null":
                    current, header = None, "devnull"
                elif rest.startswith("a/"):
                    current, header = rest[2:], "path"
                else:
                    current, header = None, "bad"
            elif line.startswith("@@ "):
                if header == "bad":
                    raise HarnessError(
                        "unparseable diff --- header followed by "
                        f"a hunk: {line[:80]}"
                    )
                m = re.match(
                    r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line
                )
                if m is None:
                    raise HarnessError(
                        f"unparseable diff hunk header: {line[:80]}"
                    )
                start = int(m.group(1))
                count = int(m.group(2)) if m.group(2) is not None else 1
                new_count = (
                    int(m.group(4)) if m.group(4) is not None else 1
                )
                if count > 0:
                    if header != "path" or current is None:
                        raise HarnessError(
                            "diff hunk claims old-side lines under "
                            "a /dev/null or missing --- header"
                        )
                    result.setdefault(current, []).extend(
                        range(start, start + count)
                    )
                hunk_left = count + new_count
        return result

    def blame_commit(self, rev: str, path: str, lineno: int) -> str:
        out = self._git(
            "blame", "-L", f"{lineno},{lineno}", "--porcelain",
            rev, "--", path,
        )
        first = out.splitlines()[0]
        return first.split(" ", 1)[0]

    def committer_ts(self, sha: str) -> int:
        return int(self._git("show", "-s", "--format=%ct", sha).strip())

    def tags_containing(self, sha: str) -> list[tuple[str, int]]:
        """(tag name, creation date as unix ts) for every tag
        containing sha. Creation date is git's %(creatordate):
        the tagger date for an annotated tag, the tagged commit's
        committer date for a lightweight one (D169 check (c))."""
        out = self._git("tag", "--contains", sha)
        tags: list[tuple[str, int]] = []
        for name in (ln.strip() for ln in out.splitlines() if ln.strip()):
            ref = self._git(
                "for-each-ref",
                "--format=%(creatordate:unix)",
                f"refs/tags/{name}",
            ).strip()
            tags.append((name, int(ref)))
        return tags


# ---------------------------------------------------------------------
# The checks. Each commit is evaluated in the registered order and
# the record names the FIRST failing check.
# ---------------------------------------------------------------------

# A readiness pair check: (fix^ sha, fix sha) -> {"parent_ready":
# bool, "fix_ready": bool}. Tests inject a fake; the live run wires
# readiness.provision (make_provision_pair_checker below).
ReadyPairCheck = Callable[[str, str], dict[str, bool]]


def evaluate_commit(
    repo: AppRepo, meta: CommitMeta, ready_pair: ReadyPairCheck
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "sha": meta.sha,
        "committer_date": _iso(meta.committer_ts),
        "subject": meta.subject,
        "outcome": "rejected",
        "failed_check": None,
        "reason": None,
    }

    def reject(check: str, reason: str) -> dict[str, Any]:
        entry["failed_check"] = check
        entry["reason"] = reason
        return entry

    # (a) subject.
    if not subject_matches(meta.subject):
        return reject("a", "subject does not match (?i)\\bfix(e[sd])?\\b")

    parent = repo.first_parent(meta.sha)
    entry["fix_parent"] = parent

    # (b) at least one SOURCE file changed.
    paths = repo.changed_paths(parent, meta.sha)
    classes = [classify_path(p) for p in paths]
    counts = {c: classes.count(c) for c in ("SOURCE", "TEST", "DOC", "OTHER")}
    entry["changed_classes"] = counts
    if counts["SOURCE"] == 0:
        return reject(
            "b",
            "no SOURCE file changed (changed classes: "
            + ", ".join(f"{k}={v}" for k, v in counts.items())
            + ")",
        )

    # (c) B exists and is shipped in a tag created before the fix.
    lines = repo.removed_or_changed_lines(parent, meta.sha)
    if not lines:
        return reject(
            "c", "no removed or changed lines: B does not exist"
        )
    assert parent is not None  # lines exist only against a real fix^
    blamed: set[str] = set()
    for path, linenos in lines.items():
        for lineno in linenos:
            blamed.add(repo.blame_commit(parent, path, lineno))
    b_sha = max(blamed, key=lambda s: (repo.committer_ts(s), s))
    b_ts = repo.committer_ts(b_sha)
    entry["b_sha"] = b_sha
    entry["b_committer_date"] = _iso(b_ts)
    containing = repo.tags_containing(b_sha)
    if not containing:
        return reject("c", f"B {b_sha} is contained in no tag")
    qualifying = [(n, ts) for n, ts in containing if ts < meta.committer_ts]
    if not qualifying:
        name, ts = min(containing, key=lambda t: (t[1], t[0]))
        return reject(
            "c",
            "every tag containing B was created at or after the "
            f"fix (earliest: {name} created {_iso(ts)})",
        )
    tag_name, tag_ts = min(qualifying, key=lambda t: (t[1], t[0]))
    entry["tag"] = tag_name
    entry["tag_created"] = _iso(tag_ts)

    # (d) fix^ and fix both READY (provisioning-only).
    readiness = ready_pair(parent, meta.sha)
    entry["parent_ready"] = bool(readiness["parent_ready"])
    entry["fix_ready"] = bool(readiness["fix_ready"])
    if not (entry["parent_ready"] and entry["fix_ready"]):
        if not entry["parent_ready"] and not entry["fix_ready"]:
            reason = "fix^ and fix are not READY"
        elif not entry["parent_ready"]:
            reason = "fix^ is not READY"
        else:
            reason = "fix is not READY"
        return reject("d", reason)

    entry["outcome"] = "selected"
    return entry


# ---------------------------------------------------------------------
# The journal (D74) and the single-writer lock (D140), mirroring
# scripts/wo6/regc_add2_run.py's conventions.
# ---------------------------------------------------------------------


class Journal:
    """One directory is one run. header.json identifies the run;
    walk.jsonl gains one line per examined commit as it completes;
    events.jsonl records every start/resume and the completed
    exit. The output record is built only from these files, so a
    death loses nothing already examined."""

    def __init__(self, dir_path: Path, header: dict[str, Any]) -> None:
        self.dir = dir_path
        self.header = header

    @classmethod
    def open(
        cls,
        dir_path: Path,
        *,
        script_sha256: str,
        main_sha: str | None,
        app_repo: str,
        app_head_sha: str,
    ) -> tuple[Journal, bool, str | None]:
        """(journal, resumed, previous exit cause). A directory
        without a header starts a new run; one with a header
        resumes it — after verifying the identity fields; any
        difference raises JournalMismatch before anything is
        written."""
        header_path = dir_path / "header.json"
        identity = {
            "script_sha256": script_sha256,
            "main_sha": main_sha,
            "app_repo": app_repo,
            "app_head_sha": app_head_sha,
        }
        if not header_path.exists():
            dir_path.mkdir(parents=True, exist_ok=True)
            journal = cls(dir_path, {"format": 1, **identity})
            journal._write_atomic("header.json", journal.header)
            return journal, False, None
        header = json.loads(header_path.read_text())
        for field, expected in identity.items():
            if header.get(field) != expected:
                raise JournalMismatch(
                    f"journal {field} is {header.get(field)!r}, "
                    f"this run has {expected!r}; refusing to resume"
                )
        journal = cls(dir_path, header)
        return journal, True, journal._previous_exit_cause()

    def _previous_exit_cause(self) -> str:
        events = self._read_events()
        if events and events[-1].get("type") == "exit":
            return str(events[-1].get("cause", "completed"))
        if events:
            return (
                "not recorded: the previous session ended "
                "without an exit record"
            )
        return "not recorded: no prior session events"

    def _read_events(self) -> list[dict[str, Any]]:
        path = self.dir / "events.jsonl"
        if not path.exists():
            return []
        return [
            json.loads(ln)
            for ln in path.read_text().splitlines()
            if ln.strip()
        ]

    def _write_atomic(self, name: str, payload: dict[str, Any]) -> None:
        tmp = self.dir / f".{name}.tmp"
        tmp.write_text(json.dumps(payload, indent=1))
        os.replace(tmp, self.dir / name)

    def _append_line(self, name: str, payload: dict[str, Any]) -> None:
        with (self.dir / name).open("a") as f:
            f.write(json.dumps(payload) + "\n")
            f.flush()
            os.fsync(f.fileno())

    def record_start(
        self, *, resumed: bool, previous_exit_cause: str | None
    ) -> None:
        self._append_line("events.jsonl", {
            "type": "start",
            "at": datetime.now(timezone.utc).isoformat(),
            "resumed": resumed,
            "previous_exit_cause": previous_exit_cause,
        })

    def record_exit(self, cause: str) -> None:
        self._append_line("events.jsonl", {
            "type": "exit",
            "at": datetime.now(timezone.utc).isoformat(),
            "cause": cause,
        })

    def append_walk(self, entry: dict[str, Any]) -> None:
        self._append_line("walk.jsonl", entry)

    def read_walk(self) -> list[dict[str, Any]]:
        """Examined-commit entries in walk order. A torn final
        line (a death mid-append) is dropped: that commit was
        never durably recorded and is re-examined on resume. A
        corrupt line anywhere else raises."""
        path = self.dir / "walk.jsonl"
        if not path.exists():
            return []
        lines = [ln for ln in path.read_text().splitlines() if ln.strip()]
        entries: list[dict[str, Any]] = []
        for i, line in enumerate(lines):
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                if i == len(lines) - 1:
                    break
                raise
        return entries


class SingleWriterLock:
    """D140: an flock held for the process lifetime on
    <journal>/run.lock. A second writer refuses (LOCK_NB) instead
    of interleaving journal lines."""

    def __init__(self, journal_dir: Path) -> None:
        self._dir = journal_dir
        self._fd: int | None = None

    def acquire(self) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._dir / "run.lock", os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise SingleWriterError(
                f"another bug-select run holds the lock on "
                f"{self._dir}; refusing to write (D140)"
            ) from None
        self._fd = fd

    def release(self) -> None:
        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None


# ---------------------------------------------------------------------
# The walk.
# ---------------------------------------------------------------------


def select_fix(
    *,
    repo: AppRepo,
    app: dict[str, Any],
    ready_pair: ReadyPairCheck,
    journal: Journal | None = None,
    main_sha: str | None,
    script_sha256: str,
    limit: int = MAX_WALKED,
) -> dict[str, Any]:
    """Walk in the registered order and build the output record.

    With a journal, already-recorded entries are reused verbatim
    (a resume re-examines nothing); the record is built only from
    the journal's entries, as in regc_add2_run.py's finalize."""
    ordered = repo.walk_order(str(app["head_sha"]))
    prior: dict[str, dict[str, Any]] = {}
    if journal is not None:
        prior = {e["sha"]: e for e in journal.read_walk()}
    entries: list[dict[str, Any]] = []
    selected: dict[str, Any] | None = None
    for meta in ordered:
        if len(entries) >= limit:
            break
        entry = prior.get(meta.sha)
        if entry is None:
            entry = evaluate_commit(repo, meta, ready_pair)
            if journal is not None:
                journal.append_walk(entry)
        entries.append(entry)
        if entry["outcome"] == "selected":
            selected = entry
            break
    record: dict[str, Any] = {
        "kind": "wo6-bug-select",
        "rule": (
            "REG-C item 7 (docs/registration/wo6-selection-rule.md), "
            "as implemented by D169"
        ),
        "app": {
            "repo": app["repo"],
            "head_sha": app["head_sha"],
            "default_branch": app["default_branch"],
            "app_path": app["app_path"],
        },
        "main_sha": main_sha,
        "script_sha256": script_sha256,
        "boundary": BOUNDARY_ISO,
        "walk": "first-parent",
        "walked_count": len(entries),
        "outcome": "selected" if selected is not None else "exhausted",
        "walked": entries,
        "selected": None,
    }
    if selected is not None:
        record["selected"] = {
            "fix": selected["sha"],
            "fix_parent": selected["fix_parent"],
            "b": selected["b_sha"],
            "b_committer_date": selected["b_committer_date"],
            "tag": selected["tag"],
            "tag_created": selected["tag_created"],
        }
    else:
        record["next"] = (
            "exhausted: the next READY candidate from the ordered "
            "list is used (REG-C item 7); no hand-picking"
        )
    return record


# ---------------------------------------------------------------------
# Input: the signed-off app from the committed ordered list.
# ---------------------------------------------------------------------


def load_candidate(survivors_path: Path, repo_name: str) -> dict[str, Any]:
    """The list entry for repo_name: repo, HEAD SHA, default
    branch and app.path, all from the committed ordered list."""
    data = json.loads(survivors_path.read_text())
    for entry in data:
        if entry.get("full_name") != repo_name:
            continue
        passed = [
            (name, stack)
            for name, stack in (entry.get("stacks") or {}).items()
            if stack.get("passed")
        ]
        if len(passed) != 1:
            raise HarnessError(
                f"{repo_name}: expected exactly one passed stack in "
                f"the ordered list, found {[n for n, _ in passed]}"
            )
        return {
            "repo": repo_name,
            "head_sha": entry["head_sha"],
            "default_branch": entry["default_branch"],
            "app_path": passed[0][1]["app_path"],
        }
    raise HarnessError(f"{repo_name} is not in {survivors_path}")


# ---------------------------------------------------------------------
# The real readiness pair check (live run only; imported lazily so
# the offline tests never pull the orchestrator stack).
# ---------------------------------------------------------------------


def make_provision_pair_checker(
    *, repo_name: str, workdir: Path
) -> ReadyPairCheck:
    """Check (d) through the real sandboxed runner: fix^ and fix
    are provisioned as one synthetic unit by
    readiness.provision.provision_repo — the same
    Orchestrator._provision_side_sandbox path readiness.yml drives
    (provisioning-only; no model call, no probes, no measured
    records). READY is the readiness row's own status."""
    from readiness.provision import provision_repo

    def check(parent_sha: str, fix_sha: str) -> dict[str, bool]:
        unit = {
            "unit_id": f"WO6-BUGSEL-{fix_sha[:8]}",
            "repo": repo_name,
            "class": "wo6-bug-select",
            "sides": [
                {"role": "fix^", "sha": parent_sha, "source": "regc-bug-select"},
                {"role": "fix", "sha": fix_sha, "source": "regc-bug-select"},
            ],
        }
        result = provision_repo(
            {"units": [unit]}, repo_name, workdir, attempt=1
        )
        rows = {row["role"]: row for row in result["rows"]}
        return {
            "parent_ready": rows["fix^"]["status"] == "READY",
            "fix_ready": rows["fix"]["status"] == "READY",
        }

    return check


def _clone_app(clone_dir: Path, repo_name: str, head_sha: str) -> AppRepo:
    """A full clone of the app (the walk needs history, blame and
    tags), pinned to the list's HEAD SHA: the recorded commit must
    exist in the clone; branch movement after the snapshot is
    irrelevant because the walk starts from the SHA."""
    if not (clone_dir / ".git").exists():
        clone_dir.parent.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(
            [
                "git", "clone", "--quiet",
                f"https://github.com/{repo_name}.git", str(clone_dir),
            ],
            capture_output=True,
            text=True,
            timeout=1800,
        )
        if proc.returncode != 0:
            raise HarnessError(
                f"git clone of {repo_name} failed: {proc.stderr.strip()}"
            )
    repo = AppRepo(clone_dir)
    if not repo.has_commit(head_sha):
        raise HarnessError(
            f"recorded HEAD {head_sha} of {repo_name} is not in "
            f"the clone at {clone_dir}"
        )
    return repo


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--repo",
        default=DEFAULT_REPO,
        help="app repo from the committed ordered list "
        "(default: the D166 signed-off app)",
    )
    ap.add_argument(
        "--survivors",
        type=Path,
        default=Path(__file__).resolve().parents[2] / DEFAULT_SURVIVORS,
        help="the committed ordered list (REG-C Addendum 2 output)",
    )
    ap.add_argument(
        "--out",
        type=Path,
        default=Path("regc-bug-select-record.json"),
        help="the output record JSON (committed before the author runs)",
    )
    ap.add_argument(
        "--journal",
        type=Path,
        default=None,
        help="run journal directory (D74); defaults to <out>-journal",
    )
    ap.add_argument(
        "--clone-dir",
        type=Path,
        default=None,
        help="the app clone; defaults to <journal>/../app-clone",
    )
    ap.add_argument(
        "--workdir",
        type=Path,
        default=None,
        help="readiness workdir; defaults to <journal>/../readiness-work",
    )
    ap.add_argument(
        "--main-sha",
        default=None,
        help="the main SHA this run is from (recorded in the "
        "journal header and the record); defaults to the HEAD of "
        "the checkout containing this script",
    )
    args = ap.parse_args(argv)

    journal_dir = args.journal or args.out.with_suffix("").with_name(
        args.out.stem + "-journal"
    )
    clone_dir = args.clone_dir or journal_dir.parent / "app-clone"
    workdir = args.workdir or journal_dir.parent / "readiness-work"
    main_sha = args.main_sha or _detect_main_sha()
    script_sha256 = _script_sha256()

    try:
        app = load_candidate(args.survivors, args.repo)
        lock = SingleWriterLock(journal_dir)
        lock.acquire()
        try:
            journal, resumed, previous_cause = Journal.open(
                journal_dir,
                script_sha256=script_sha256,
                main_sha=main_sha,
                app_repo=app["repo"],
                app_head_sha=str(app["head_sha"]),
            )
            journal.record_start(
                resumed=resumed, previous_exit_cause=previous_cause
            )
            repo = _clone_app(clone_dir, app["repo"], str(app["head_sha"]))
            ready_pair = make_provision_pair_checker(
                repo_name=app["repo"], workdir=workdir
            )
            record = select_fix(
                repo=repo,
                app=app,
                ready_pair=ready_pair,
                journal=journal,
                main_sha=main_sha,
                script_sha256=script_sha256,
            )
            args.out.parent.mkdir(parents=True, exist_ok=True)
            tmp = args.out.with_suffix(".tmp")
            tmp.write_text(json.dumps(record, indent=1))
            os.replace(tmp, args.out)
            journal.record_exit("completed")
        finally:
            lock.release()
    except (HarnessError, JournalMismatch, SingleWriterError) as exc:
        print(f"regc_bug_select: {exc}", file=sys.stderr)
        return 2

    # Progress lines carry hashes and check outcomes only — never a
    # subject, a diff or a message body (contamination guard).
    for entry in record["walked"]:
        if entry["outcome"] == "selected":
            print(f"walked {entry['sha'][:12]} -> selected")
        else:
            print(
                f"walked {entry['sha'][:12]} -> rejected at check "
                f"{entry['failed_check']}: {entry['reason']}"
            )
    if record["outcome"] == "selected":
        sel = record["selected"]
        print(
            f"SELECTED fix={sel['fix']} fix^={sel['fix_parent']} "
            f"B={sel['b']} tag={sel['tag']}"
        )
    else:
        print(f"EXHAUSTED after {record['walked_count']} walked")
    print(f"record written to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
