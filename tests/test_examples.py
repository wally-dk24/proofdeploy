"""Examples stay honest: the worked probes.json must validate, and the
deliberately broken file must fail with the expected problems."""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VALIDATE = ROOT / "examples" / "validating-probes" / "validate.py"
GOOD = ROOT / "examples" / "authoring-a-probe" / "probes.json"
BAD = ROOT / "examples" / "validating-probes" / "invalid-example.json"


def run(path):
    r = subprocess.run([sys.executable, str(VALIDATE), str(path)],
                       capture_output=True, text=True)
    return r.returncode, r.stdout


def test_worked_example_validates():
    code, out = run(GOOD)
    assert code == 0, f"worked example failed validation:\n{out}"
    assert "valid (2 probes, schema v0)" in out


def test_invalid_example_fails_loudly():
    code, out = run(BAD)
    assert code == 1, "invalid example passed validation"
    for fragment in (
        "'version' must be \"0\"",
        "duplicate id 'dup'",
        "at least one of 'expect.json' / 'expect.contains'",
        "must be a single SELECT",
        "'type' must be 'http' or 'db'",
    ):
        assert fragment in out, f"missing expected error: {fragment}"


def test_schema_json_is_well_formed_and_current():
    schema = json.loads((ROOT / "examples" / "validating-probes" / "probe-schema.json").read_text())
    assert schema["properties"]["version"]["const"] == "0"
    assert "http" in schema["$defs"]["probe"]["properties"]["type"]["enum"]
