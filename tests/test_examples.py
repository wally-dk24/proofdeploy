"""Examples stay honest: the worked probes.json must validate, and the
deliberately broken file must fail with the expected problems."""

import json
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
VALIDATE = ROOT / "examples" / "validating-probes" / "validate.py"
GOOD = ROOT / "examples" / "authoring-a-probe" / "probes.json"
BAD = ROOT / "examples" / "validating-probes" / "invalid-example.json"


def run(path):
    r = subprocess.run([sys.executable, str(VALIDATE), str(path)], capture_output=True, text=True)
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


YML_DIR = ROOT / "examples" / "proofdeploy-yml"
REQUIRED_YML_KEYS = {"build", "migrate", "seed", "start", "readiness", "env"}
SECRET_HINTS = ("password", "secret", "token", "passwd", "credential")


def _load_yml(path):
    return yaml.safe_load(path.read_text())


def test_proofdeploy_yml_examples():
    files = sorted(YML_DIR.glob("*.yml"))
    assert len(files) >= 3, "expected per-stack proofdeploy.yml examples"
    for path in files:
        doc = _load_yml(path)
        assert set(doc) == REQUIRED_YML_KEYS, f"{path.name}: keys {sorted(doc)}"
        for key in ("build", "migrate", "seed", "start", "readiness"):
            val = doc[key]
            assert isinstance(val, str) and val.strip(), (
                f"{path.name}: '{key}' must be a non-empty string"
            )
        assert isinstance(doc["env"], dict), f"{path.name}: 'env' must be a mapping"
        for k, v in doc["env"].items():
            assert isinstance(v, str), f"{path.name}: env['{k}'] must be a string"
            lowered = k.lower()
            assert not any(h in lowered for h in SECRET_HINTS), (
                f"{path.name}: env['{k}'] looks like a credential — "
                "proofdeploy.yml carries non-secret config only"
            )
