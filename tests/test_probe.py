"""Tests for the probe schema harness (WAL-58)."""

import json

import pytest

from proofdeploy.probe import (
    HARNESS_VERSION,
    ProbeRejected,
    ProbeSet,
    parse_probes,
    validate_probe,
)


def _valid_probe() -> dict:
    return {
        "act": {"method": "GET", "path": "/items/1", "headers": {"Accept": "application/json"}},
        "assert": [
            {"type": "status", "equals": 200},
            {"type": "body", "contains": "widget"},
        ],
    }


def _fenced(probe: dict) -> str:
    return "Some intro text\n```json\n" + json.dumps(probe) + "\n```\nTrailing text."


def test_harness_version_recorded():
    assert HARNESS_VERSION == "1.0.0"
    ps = ProbeSet.from_author_output(_fenced(_valid_probe()))
    assert ps.manifest()["harness_version"] == "1.0.0"


def test_valid_probe_passes():
    assert validate_probe(_valid_probe()) == _valid_probe()


def test_parse_probes_extracts_fenced_json():
    probes = parse_probes(_fenced(_valid_probe()))
    assert len(probes) == 1
    assert probes[0]["act"]["method"] == "GET"


def test_rejects_unknown_top_level_field():
    p = _valid_probe()
    p["explain"] = "why"
    with pytest.raises(ProbeRejected):
        validate_probe(p)


def test_rejects_missing_act():
    with pytest.raises(ProbeRejected):
        validate_probe({"assert": [{"type": "status", "equals": 200}]})


def test_rejects_empty_assert():
    p = _valid_probe()
    p["assert"] = []
    with pytest.raises(ProbeRejected):
        validate_probe(p)


def test_rejects_bad_method():
    p = _valid_probe()
    p["act"]["method"] = "BREW"
    with pytest.raises(ProbeRejected):
        validate_probe(p)


def test_rejects_relative_path():
    p = _valid_probe()
    p["act"]["path"] = "items/1"
    with pytest.raises(ProbeRejected):
        validate_probe(p)


def test_rejects_python_code_probe():
    p = _valid_probe()
    p["act"]["body"] = "```python\nimport subprocess\nsubprocess.run(['x'])\n```"
    with pytest.raises(ProbeRejected):
        validate_probe(p)


def test_rejects_eval_in_assertion():
    p = _valid_probe()
    p["assert"].append({"type": "body", "contains": "x'); eval('evil"})
    with pytest.raises(ProbeRejected):
        validate_probe(p)


def test_db_assertion_requires_select():
    p = _valid_probe()
    p["assert"] = [{"type": "db", "query": "DELETE FROM items", "count": 0}]
    with pytest.raises(ProbeRejected):
        validate_probe(p)


def test_db_assertion_accepts_select():
    p = _valid_probe()
    p["assert"] = [{"type": "db", "query": "SELECT count(*) FROM items", "equals": 3}]
    validate_probe(p)


def test_harness_app_setup_step():
    p = _valid_probe()
    p["setup"] = [
        {
            "type": "harness_app",
            "language": "python",
            "source": "from bottle import Bottle\napp = Bottle()",
            "entrypoint": "app",
        }
    ]
    ps = ProbeSet.from_author_output(_fenced(p))
    assert len(ps.harness_app_hashes) == 1
    assert len(ps.harness_app_hashes[0]) == 64  # sha256 hex


def test_harness_app_needs_source():
    p = _valid_probe()
    p["setup"] = [
        {"type": "harness_app", "language": "python", "source": "  ", "entrypoint": "app"}
    ]
    with pytest.raises(ProbeRejected):
        validate_probe(p)


def test_harness_app_rejected_when_not_allowed():
    """Eval repos must reject harness_app; only dev-set library repos allow it."""
    p = _valid_probe()
    p["setup"] = [
        {
            "type": "harness_app",
            "language": "python",
            "source": "from bottle import Bottle\napp = Bottle()",
            "entrypoint": "app",
        }
    ]
    # Allowed by default (dev-set library repos)
    validate_probe(p, allow_harness_app=True)
    # Rejected for eval repos
    with pytest.raises(ProbeRejected, match="only allowed for dev-set library repos"):
        validate_probe(p, allow_harness_app=False)
    with pytest.raises(ProbeRejected):
        parse_probes(_fenced(p), allow_harness_app=False)


def test_harness_app_source_allows_imports():
    """harness_app.source IS code by definition; the blocklist must not apply."""
    p = _valid_probe()
    p["setup"] = [
        {
            "type": "harness_app",
            "language": "python",
            "source": "import os\nfrom bottle import Bottle\napp = Bottle()\n",
            "entrypoint": "app",
        }
    ]
    # Must pass: the schema allowlist is fail-closed, no substring blocklist on source
    validate_probe(p, allow_harness_app=True)


def test_rejects_no_fenced_blocks():
    with pytest.raises(ProbeRejected):
        parse_probes("just some prose, no blocks")


def test_rejects_invalid_json_block():
    with pytest.raises(ProbeRejected):
        parse_probes("```json\n{not json}\n```")


def test_one_bad_probe_rejects_all():
    good = _fenced(_valid_probe())
    bad = "```json\n" + json.dumps({"act": {"method": "GET", "path": "/x"}}) + "\n```"
    with pytest.raises(ProbeRejected):
        parse_probes(good + "\n" + bad)
