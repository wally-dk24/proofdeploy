"""Offline tests for scripts/wo6/regc_add2_closeout.py (D149).

The close-out driver derives a stopped run's outputs from its
journal with the run script's own _winners_and_error_history and
_finalize. These tests pin, on a small synthetic journal:

- the driver's regc-add2-raw.json and regc-add2-survivors.json are
  BYTE-IDENTICAL to calling _finalize directly on the same journal
  (same ordering, same serialisation — the driver adds nothing of
  its own to those two files);
- UNSCREENED.json lists exactly the pool repos the journal never
  screened, in pool order, with stars and queries;
- the close-out header states the pool / screened / unscreened /
  survivors / errored counts and the _retry_errored no-op;
- the journal directory is byte-untouched by the driver (D149);
- a journal whose latest attempts still carry errors is REFUSED:
  finishing it would need the real retry pass, not a close-out.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
RUN_SCRIPT = ROOT / "scripts" / "wo6" / "regc_add2_run.py"
CLOSEOUT_SCRIPT = ROOT / "scripts" / "wo6" / "regc_add2_closeout.py"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


run_mod = _load("regc_add2_run", RUN_SCRIPT)
closeout_mod = _load("regc_add2_closeout", CLOSEOUT_SCRIPT)

SNAPSHOT_DATE = "2026-10-16"
COUNTS = {"JavaScript": 2, "TypeScript": 1, "Python": 2}
SEARCH_META = {
    lang: {"pages": [], "incomplete_results": False} for lang in COUNTS
}

POOL = [
    {"full_name": "owner/alpha", "stars": 500, "default_branch": "main",
     "queries": ["Python"]},
    {"full_name": "owner/beta", "stars": 900, "default_branch": "main",
     "queries": ["JavaScript"]},
    {"full_name": "owner/gamma", "stars": 900, "default_branch": "main",
     "queries": ["JavaScript"]},
    {"full_name": "owner/delta", "stars": 100, "default_branch": "main",
     "queries": ["TypeScript"]},
    {"full_name": "owner/epsilon", "stars": 700, "default_branch": "main",
     "queries": ["Python"]},
]


def _entry(name: str, stars: int, attempt: int, **kw: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "full_name": name,
        "stars": stars,
        "default_branch": "main",
        "queries": ["Python"],
        "excluded": None,
        "head_sha": "c" * 40,
        "tree_truncated": False,
        "stacks": {},
        "survivor": False,
        "attempt": attempt,
    }
    entry.update(kw)
    return entry


ENTRIES = [
    _entry("owner/alpha", 500, 1, survivor=True),
    # beta: attempt 1 errored, attempt 2 screened out — the winner is
    # attempt 2 and carries no error, so ERRORED stays 0.
    _entry("owner/beta", 900, 1, error="harness or environment: boom"),
    _entry("owner/beta", 900, 2, survivor=False),
    _entry("owner/gamma", 900, 1, survivor=True),
    _entry("owner/delta", 100, 1, survivor=False),
    # owner/epsilon: never screened — the unscreened pool tail.
]


def _write_journal(dir_path: Path, entries: list[dict[str, Any]]) -> Path:
    dir_path.mkdir(parents=True)
    script_sha = hashlib.sha256(RUN_SCRIPT.read_bytes()).hexdigest()
    (dir_path / "header.json").write_text(json.dumps({
        "format": 1,
        "script_sha256": script_sha,
        "main_sha": "b" * 40,
        "snapshot_date": SNAPSHOT_DATE,
    }, indent=1))
    (dir_path / "pool.json").write_text(json.dumps({
        "pool": POOL,
        "query_counts": COUNTS,
        "search_meta": SEARCH_META,
    }, indent=1))
    (dir_path / "entries.jsonl").write_text(
        "".join(json.dumps(e) + "\n" for e in entries)
    )
    (dir_path / "events.jsonl").write_text(
        json.dumps({"type": "start", "at": "2026-10-16T12:02:26+00:00",
                    "resumed": False, "previous_exit_cause": None}) + "\n"
    )
    return dir_path


def _journal_hashes(dir_path: Path) -> dict[str, str]:
    return {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(dir_path.iterdir())
    }


def _reference_finalize(journal_dir: Path, out_dir: Path) -> None:
    """_finalize called directly, on inputs built from the journal
    the way run() builds them — the byte reference for the driver."""
    stored = json.loads((journal_dir / "pool.json").read_text())
    pool_list = stored["pool"]
    history: dict[str, list[dict[str, Any]]] = {}
    for line in (journal_dir / "entries.jsonl").read_text().splitlines():
        if line.strip():
            entry = json.loads(line)
            history.setdefault(str(entry["full_name"]), []).append(entry)
    screened = [rec for rec in pool_list if rec["full_name"] in history]
    entries, error_history = run_mod._winners_and_error_history(screened, history)
    run_mod._finalize(
        entries,
        snapshot_date=SNAPSHOT_DATE,
        counts=stored["query_counts"],
        search_meta=stored["search_meta"],
        pool_size=len(pool_list),
        authenticated=True,
        out_dir=out_dir,
        error_history=error_history,
    )


def test_driver_outputs_match_finalize_bytes(tmp_path: Path) -> None:
    journal_dir = _write_journal(tmp_path / "journal", ENTRIES)
    before = _journal_hashes(journal_dir)

    out_dir = tmp_path / "closeout"
    header = closeout_mod.closeout(
        journal_dir,
        out_dir,
        stopped_at="2026-10-17T11:43:32Z",
        stopped_by="Master's word (D148)",
        last_append_at="2026-10-17T03:00:07Z",
    )

    ref_dir = tmp_path / "reference"
    _reference_finalize(journal_dir, ref_dir)
    for name in ("regc-add2-raw.json", "regc-add2-survivors.json"):
        assert (out_dir / name).read_bytes() == (ref_dir / name).read_bytes(), name

    raw = json.loads((out_dir / "regc-add2-raw.json").read_text())
    assert raw["pool_size"] == 5
    assert raw["survivor_count"] == 2
    assert raw["error_count"] == 0
    survivors = json.loads((out_dir / "regc-add2-survivors.json").read_text())
    # Item 4: stars descending — gamma (900) before alpha (500).
    assert [s["full_name"] for s in survivors] == ["owner/gamma", "owner/alpha"]
    assert [s["order"] for s in survivors] == [1, 2]

    unscreened = json.loads((out_dir / "UNSCREENED.json").read_text())
    assert unscreened == [
        {"full_name": "owner/epsilon", "stars": 700, "queries": ["Python"]}
    ]

    assert header["pool_size"] == 5
    assert header["screened"] == 4
    assert header["unscreened"] == 1
    assert header["survivors"] == 2
    assert header["errored"] == 0
    assert header["max_unscreened_stars"] == 700
    assert "no-op" in header["retry_errored"]
    on_disk = json.loads((out_dir / "closeout-header.json").read_text())
    assert on_disk == header

    # D149: the journal is byte-untouched by the close-out.
    assert _journal_hashes(journal_dir) == before


def test_closeout_refuses_a_still_errored_journal(tmp_path: Path) -> None:
    entries = [
        _entry("owner/alpha", 500, 1, survivor=True),
        _entry("owner/beta", 900, 1, error="harness or environment: boom"),
    ]
    journal_dir = _write_journal(tmp_path / "journal", entries)
    out_dir = tmp_path / "closeout"
    with pytest.raises(closeout_mod.CloseoutRefused, match="_retry_errored"):
        closeout_mod.closeout(
            journal_dir,
            out_dir,
            stopped_at="2026-10-17T11:43:32Z",
            stopped_by="Master's word (D148)",
            last_append_at="2026-10-17T03:00:07Z",
        )
    assert not (out_dir / "regc-add2-raw.json").exists()
