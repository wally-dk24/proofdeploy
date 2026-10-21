"""Write the lean gate's bug unit from an R-LEAN-SEL selection record (D267).

    python experiments/lean-poc/write_bug_unit.py <record.json> <relay-path>

Writes ``experiments/lean-poc/paperless-bug-unit.json`` with exactly seven keys:
``unit_repo, b_parent, b, fix_parent, fix, selection_record,
selection_record_sha256``. ``selection_record`` is the relay-repo path given on
the command line; ``selection_record_sha256`` is the sha256 of the record
file's bytes.

Refuses a record whose rule is not ``R-LEAN-SEL`` or whose outcome is not
``selected``. BLINDNESS: only the record's top-level rule/outcome, app repo and
the four SHAs are read; commit subjects (the record's ``walked`` rows) are never
read into output, printed or written.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

RULE = "R-LEAN-SEL"
OUT_PATH = Path(__file__).resolve().parent / "paperless-bug-unit.json"
KEYS = (
    "unit_repo",
    "b_parent",
    "b",
    "fix_parent",
    "fix",
    "selection_record",
    "selection_record_sha256",
)
SHA40 = re.compile(r"^[0-9a-f]{40}$")


class Refusal(Exception):
    pass


def build_unit(record_path: Path, relay_path: str) -> dict[str, Any]:
    raw = record_path.read_bytes()
    try:
        record = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise Refusal(f"record is not JSON: {exc.msg}") from exc
    if not isinstance(record, dict):
        raise Refusal("record is not a JSON object")
    if record.get("rule") != RULE:
        raise Refusal(f"record rule is not {RULE}")
    if record.get("outcome") != "selected":
        raise Refusal("record outcome is not 'selected'")
    selected = record.get("selected")
    app = record.get("app")
    if not isinstance(selected, dict) or not isinstance(app, dict):
        raise Refusal("record has no selected unit or app")
    repo = app.get("repo")
    if not isinstance(repo, str) or not repo:
        raise Refusal("record names no app repo")
    unit: dict[str, Any] = {"unit_repo": repo}
    for key in ("b_parent", "b", "fix_parent", "fix"):
        value = selected.get(key)
        if not isinstance(value, str) or not SHA40.match(value):
            raise Refusal(f"selected.{key} is not a 40-hex SHA")
        unit[key] = value
    unit["selection_record"] = relay_path
    unit["selection_record_sha256"] = hashlib.sha256(raw).hexdigest()
    return {k: unit[k] for k in KEYS}


def write_unit(record_path: Path, relay_path: str, out_path: Path | None = None) -> dict[str, Any]:
    unit = build_unit(record_path, relay_path)
    (out_path or OUT_PATH).write_text(json.dumps(unit, indent=2) + "\n")
    return unit


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2:
        print("usage: write_bug_unit.py <record.json> <relay-path>", file=sys.stderr)
        return 2
    try:
        write_unit(Path(args[0]), args[1])
    except (Refusal, OSError) as exc:
        print(f"write_bug_unit: refused: {exc}", file=sys.stderr)
        return 2
    print(f"wrote {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
