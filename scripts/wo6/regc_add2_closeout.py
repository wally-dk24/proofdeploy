"""D149 stopped-run close-out driver for the REG-C Addendum 2 run.

The live run of scripts/wo6/regc_add2_run.py was stopped on Master's
word (D148, 2026-10-17T11:43:32Z) at 2,936 of 3,000 pool repos
screened. The reviewer ruled (D149) that the journal stays
byte-untouched — no resume, no "completed" exit event the run did
not earn — and that the run's outputs are instead derived here,
with the run script's own code, from the journal exactly as it lies.

This driver therefore:
- imports regc_add2_run unchanged and reuses its Journal readers,
  its _winners_and_error_history, and its _finalize (the item 4
  ordering and serialisation are the script's, not re-implemented);
- performs NO journal writes of any kind: no Journal.open, no
  record_start / record_exit, no append_entry, no write_pool;
- restricts the pool handed to _winners_and_error_history to the
  repos the journal actually screened (the function resolves every
  pool record against the history; an unscreened record has no
  winner). Pool order is preserved, and _finalize is given the
  journal's recorded pool_size (3,000), so regc-add2-raw.json
  states the true pool alongside the screened results;
- does NOT silently skip the _retry_errored pass. That pass
  re-screens repos whose latest attempt carries an "error". This
  journal has none (ERRORED = 0), so the pass would be a no-op;
  the driver verifies that precondition and states it in the
  close-out header. A journal whose latest attempts DO carry
  errors is refused: finishing it needs the real retry pass,
  i.e. a real resume of the run, not a close-out;
- writes, beside the two _finalize outputs, a close-out header
  (closeout-header.json) and UNSCREENED.json (the unscreened pool
  tail, in pool order, each with its stars and language queries).

Usage:
    python3 scripts/wo6/regc_add2_closeout.py \
        --journal /path/to/journal-dir --out /path/to/closeout-dir \
        --stopped-at 2026-10-17T11:43:32Z \
        --last-append-at 2026-10-17T03:00:07Z
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

_SCRIPT = Path(__file__).resolve().with_name("regc_add2_run.py")
_spec = importlib.util.spec_from_file_location("regc_add2_run", _SCRIPT)
assert _spec is not None and _spec.loader is not None
run_mod = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("regc_add2_run", run_mod)
_spec.loader.exec_module(run_mod)

JOURNAL_FILES = ("header.json", "pool.json", "entries.jsonl", "events.jsonl")


class CloseoutRefused(Exception):
    """The journal is not in a state a stopped-run close-out may
    finalise (see the module docstring)."""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def closeout(
    journal_dir: Path,
    out_dir: Path,
    *,
    stopped_at: str,
    stopped_by: str,
    last_append_at: str,
    authenticated: bool = True,
) -> dict[str, Any]:
    """Derive the run outputs from the journal as-is. Returns the
    close-out header dict (also written to closeout-header.json)."""
    header = json.loads((journal_dir / "header.json").read_text())
    journal = run_mod.Journal(journal_dir, header)
    stored = journal.read_pool()
    if stored is None:
        raise CloseoutRefused(f"journal {journal_dir} has no pool.json")
    pool_list, counts, search_meta = stored

    history: dict[str, list[dict[str, Any]]] = {}
    for line in journal.read_entries():
        history.setdefault(str(line["full_name"]), []).append(line)

    screened = [rec for rec in pool_list if rec["full_name"] in history]
    unscreened = [rec for rec in pool_list if rec["full_name"] not in history]

    # The _retry_errored precondition, stated rather than skipped:
    # the pass re-screens every repo whose LATEST attempt carries an
    # "error". If any existed here, a close-out must not finalise —
    # the run would have to be resumed so the pass really runs.
    still_errored = [
        name for name, lines in history.items() if "error" in lines[-1]
    ]
    if still_errored:
        raise CloseoutRefused(
            "_retry_errored is NOT a no-op for this journal: "
            f"{len(still_errored)} repo(s) have an errored latest attempt "
            f"({', '.join(sorted(still_errored)[:5])}"
            f"{', ...' if len(still_errored) > 5 else ''}); "
            "a close-out cannot finalise it — the run must be resumed "
            "so the retry pass actually re-screens them."
        )
    retry_statement = (
        "_retry_errored not run: verified no-op for this journal — "
        "no repo's latest attempt carries an 'error' (ERRORED = 0), "
        "so the pass would re-screen nothing."
    )

    entries, error_history = run_mod._winners_and_error_history(screened, history)
    result = run_mod._finalize(
        entries,
        snapshot_date=str(header["snapshot_date"]),
        counts=counts,
        search_meta=search_meta,
        pool_size=len(pool_list),
        authenticated=authenticated,
        out_dir=out_dir,
        error_history=error_history,
    )

    unscreened_rows = [
        {
            "full_name": rec["full_name"],
            "stars": rec["stars"],
            "queries": rec["queries"],
        }
        for rec in unscreened
    ]
    (out_dir / "UNSCREENED.json").write_text(json.dumps(unscreened_rows, indent=1))

    closeout_header: dict[str, Any] = {
        "label": "stopped-run close-out (D148/D149)",
        "rule": (
            "Outputs derived with the run script's own "
            "_winners_and_error_history and _finalize from the "
            "journal as-is; the journal was not resumed, edited, or "
            "given an exit record."
        ),
        "stopped_at": stopped_at,
        "stopped_by": stopped_by,
        "last_append_at": last_append_at,
        "pool_size": len(pool_list),
        "screened": len(screened),
        "unscreened": len(unscreened),
        "survivors": result["survivor_count"],
        "errored": result["error_count"],
        "retry_errored": retry_statement,
        "authenticated": authenticated,
        "authenticated_basis": (
            "A _finalize input the journal header does not carry; "
            "the run's sessions executed with a GitHub token "
            "(GH_TOKEN), as recorded in the run record."
        ),
        "script_sha256": _sha256(_SCRIPT),
        "journal_sha256": {
            name: _sha256(journal_dir / name) for name in JOURNAL_FILES
        },
        "max_unscreened_stars": max(
            (int(rec["stars"]) for rec in unscreened), default=0
        ),
    }
    (out_dir / "closeout-header.json").write_text(
        json.dumps(closeout_header, indent=1)
    )
    return closeout_header


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--journal", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--stopped-at", required=True)
    ap.add_argument("--last-append-at", required=True)
    ap.add_argument(
        "--stopped-by",
        default="Master's word (D148)",
    )
    args = ap.parse_args()
    try:
        header = closeout(
            args.journal,
            args.out,
            stopped_at=args.stopped_at,
            stopped_by=args.stopped_by,
            last_append_at=args.last_append_at,
        )
    except CloseoutRefused as err:
        print(f"error: {err}", file=sys.stderr)
        raise SystemExit(2) from err
    print(f"pool: {header['pool_size']}  screened: {header['screened']}  "
          f"unscreened: {header['unscreened']}")
    print(f"survivors: {header['survivors']}  errored: {header['errored']}")
    print(f"max unscreened stars: {header['max_unscreened_stars']}")
    print(header["retry_errored"])


if __name__ == "__main__":
    main()
