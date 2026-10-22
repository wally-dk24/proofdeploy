"""Blind, mechanical selection of paperless CLEAN gate units (Lean P6a, D258).

Rule (pre-registered in the plan of record):

    In walk order (the committed WO-6 walk journal's order: first-parent,
    committer date ascending, after 2026-07-01), take the first N commits
    whose journal ``failed_check`` is ``a`` (the subject does not match the
    fix pattern) AND that change at least one SOURCE file under the WO-6
    script's own ``classify_path`` AND that are not B, B's first parent, or
    the fix pair.

BLINDNESS: the journal rows carry commit subjects. This script projects each
row to ``sha``, ``failed_check`` and ``committer_date`` immediately on
reading; subjects are never held past that, printed, or written. Changed
paths are read as names only (``git diff --name-only``), used to classify,
and discarded; only per-class COUNTS are recorded or printed.

No model is called. The repo is a (blobless) clone; only trees are needed.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any

RULE_TEXT = (
    "In walk order (the committed WO-6 walk journal's order: first-parent, committer date "
    "ascending, after 2026-07-01), the first N commits with failed_check == 'a' (subject not "
    "fix-titled) that change at least one SOURCE file under the WO-6 script's own classify_path "
    "(paths from git diff --name-only <first-parent>..<sha>, --no-renames) and that are not B, "
    "B's first parent, or the fix pair (fix^ and fix)."
)

KEEP = ("sha", "failed_check", "committer_date")

# The WO-6 bug unit (already chosen) and its fix pair, as named in WO-P6a. These are the DEFAULT
# excludes (the paperless set): B, B's first parent and the fix pair. Another app passes its own
# four SHAs with --exclude-sha.
B_SHA = "aa67fd3aefa6489984fc15a5c32c1ad2b16ac23a"
FIX_PAIR_PREFIXES = ("14b6a41b", "d3710856")

DEFAULT_APP_NAME = "paperless-ngx/paperless-ngx"
_SHA1_RE = re.compile(r"[0-9a-f]{40}")

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "wo6" / "regc_bug_select.py"


def load_wo6() -> Any:
    spec = importlib.util.spec_from_file_location("regc_bug_select", SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def read_journal(path: Path) -> tuple[list[dict[str, str]], str]:
    """(rows projected to KEEP, sha256 of the file bytes). Subjects are dropped here."""
    raw = path.read_bytes()
    rows: list[dict[str, str]] = []
    for line in raw.decode("utf-8").splitlines():
        if not line.strip():
            continue
        full = json.loads(line)
        rows.append({k: full.get(k) for k in KEEP})  # type: ignore[misc]
        del full
    return rows, hashlib.sha256(raw).hexdigest()


def select_clean_units(
    rows: list[dict[str, str]],
    repo: Any,
    wo6: Any,
    *,
    n: int,
    exclude: set[str],
    exclude_prefixes: tuple[str, ...] = (),
) -> dict[str, Any]:
    dates = [r["committer_date"] for r in rows]
    ascending = dates == sorted(dates)
    exclude = set(exclude)
    for row in rows:
        if any(row["sha"].startswith(p) for p in exclude_prefixes):
            exclude.add(row["sha"])
    selected: list[dict[str, Any]] = []
    examined = 0
    skipped_excluded = 0
    skipped_no_source = 0
    for row in rows:
        if len(selected) == n:
            break
        if row["failed_check"] != "a":
            continue
        if row["sha"] in exclude:
            skipped_excluded += 1
            continue
        examined += 1
        parent = repo.first_parent(row["sha"])
        paths = repo.changed_paths(parent, row["sha"])
        counts = {c: 0 for c in ("SOURCE", "TEST", "DOC", "OTHER")}
        for p in paths:
            counts[wo6.classify_path(p)] += 1
        del paths  # names are never kept
        if counts["SOURCE"] < 1:
            skipped_no_source += 1
            continue
        selected.append(
            {
                "sha": row["sha"],
                "first_parent": parent,
                "committer_date": row["committer_date"],
                "changed_class_counts": counts,
                "changed_source_paths": counts["SOURCE"],
            }
        )
    if len(selected) < n:
        raise SystemExit(f"only {len(selected)} units satisfy the rule, wanted {n}")
    return {
        "journal_order_is_committer_date_ascending": ascending,
        "candidates_examined": examined,
        "skipped_excluded": skipped_excluded,
        "skipped_no_source_file": skipped_no_source,
        "units": selected,
    }


def resolve_excludes(repo: Any, b_sha: str, prefixes: tuple[str, ...]) -> set[str]:
    out = {b_sha}
    parent = repo.first_parent(b_sha)
    if parent:
        out.add(parent)
    for p in prefixes:
        out.add(repo._git("rev-parse", f"{p}^{{commit}}").strip())
    return out


def _full_sha(value: str) -> str:
    if not _SHA1_RE.fullmatch(value):
        raise argparse.ArgumentTypeError(f"{value!r} is not a full 40-character lowercase hex sha")
    return value


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    ap.add_argument("--journal", type=Path, required=True)
    ap.add_argument("--repo", type=Path, required=True, help="(blobless) clone of the app")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("-n", type=int, default=3)
    ap.add_argument("--journal-ref", default="", help="where the journal came from (recorded)")
    ap.add_argument(
        "--exclude-sha",
        action="append",
        type=_full_sha,
        default=None,
        help="full 40-hex sha to exclude (repeatable); default: the paperless B, B^ and fix pair",
    )
    ap.add_argument(
        "--app-name",
        default=DEFAULT_APP_NAME,
        help="owner/name of the app (recorded when it is not the default)",
    )
    args = ap.parse_args(argv)

    wo6 = load_wo6()
    repo = wo6.AppRepo(args.repo)
    rows, journal_sha = read_journal(args.journal)
    if args.exclude_sha is None:
        excludes = resolve_excludes(repo, B_SHA, FIX_PAIR_PREFIXES)
    else:
        excludes = set(args.exclude_sha)
    result = select_clean_units(rows, repo, wo6, n=args.n, exclude=excludes)
    record = {
        "rule": RULE_TEXT,
        "journal_ref": args.journal_ref,
        "journal_sha256": journal_sha,
        "journal_rows": len(rows),
        "app_repo_head": repo._git("rev-parse", "HEAD").strip(),
        "excluded": sorted(excludes),
        "n": args.n,
        **result,
        "measured_result": False,
    }
    if args.app_name != DEFAULT_APP_NAME:
        # Absent for the default so the paperless record re-derives byte-identically.
        record["app_name"] = args.app_name
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    for u in record["units"]:
        c = u["changed_class_counts"]
        print(
            u["sha"],
            u["first_parent"],
            u["committer_date"],
            f"SOURCE={c['SOURCE']} TEST={c['TEST']} DOC={c['DOC']} OTHER={c['OTHER']}",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
