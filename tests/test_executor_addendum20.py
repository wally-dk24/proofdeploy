"""Addendum 20 (WO-5.1): executor comparison semantics.

E1: `body equals` with a non-string expected value compares parsed JSON.
E2: captures keep their JSON type; whole-value `${NAME}` substitutes typed.
Plus a replay test from acceptance run 38152175992's real record.
"""
import json
from pathlib import Path

from proofdeploy.executor import _check_assertion
from proofdeploy.setup_auth import SetupContext, capture_bindings, substitute_strict


def _check(assertion, status=200, body="", headers=None):
    return _check_assertion(assertion, status, headers or {}, body, None)


# --- E1: body equals with non-string expectations ---


def test_body_equals_dict_matches_json_body():
    # The acceptance run's probe 1: {"type": "body", "equals": {}} vs "{}\n".
    ok, detail, reason = _check({"type": "body", "equals": {}}, body="{}\n")
    assert ok is True, detail
    assert reason is None


def test_body_equals_dict_fails_on_different_json():
    ok, detail, reason = _check(
        {"type": "body", "equals": {}}, body='{"error":"not found"}\n'
    )
    assert ok is False, detail
    assert reason is None


def test_body_equals_dict_fails_on_invalid_json():
    ok, detail, reason = _check({"type": "body", "equals": {}}, body="not json")
    assert ok is False, detail
    assert "not valid JSON" in detail
    assert reason is None


def test_body_equals_list_compares_json():
    ok, detail, _ = _check(
        {"type": "body", "equals": [1, 2]}, body='[1, 2]\n'
    )
    assert ok is True, detail


def test_body_equals_string_compares_raw_text():
    # String expectations keep the old exact-text behavior.
    ok, _, _ = _check({"type": "body", "equals": "hello"}, body="hello")
    assert ok is True
    ok, _, _ = _check({"type": "body", "equals": "hello"}, body="hello\n")
    assert ok is False


# --- E2: typed captures and placeholders ---


def test_capture_keeps_int_type():
    bindings = capture_bindings(
        '{"id": 1}', {}, {"ITEM_ID": {"from": "body", "json_path": "id"}}
    )
    assert bindings == {"ITEM_ID": 1}
    assert isinstance(bindings["ITEM_ID"], int)


def test_capture_keeps_string_type():
    bindings = capture_bindings(
        '{"name": "x"}', {}, {"NAME": {"from": "body", "json_path": "name"}}
    )
    assert bindings == {"NAME": "x"}


def test_whole_placeholder_substitutes_typed_value():
    out = substitute_strict("${ITEM_ID}", {"ITEM_ID": 1}, {"ITEM_ID"})
    assert out == 1
    assert isinstance(out, int)


def test_embedded_placeholder_substitutes_text_form():
    out = substitute_strict("/items/${ITEM_ID}", {"ITEM_ID": 1}, {"ITEM_ID"})
    assert out == "/items/1"
    assert isinstance(out, str)


def test_json_path_assertion_with_typed_capture_passes():
    # End to end of the E2 defect: int capture vs int JSON value.
    ctx = SetupContext()
    ctx.declare({"ITEM_ID"})
    ctx.add(capture_bindings('{"id": 1}', {}, {"ITEM_ID": {"from": "body", "json_path": "id"}}))
    assertion = ctx.apply_strict(
        {"type": "body", "json_path": {"path": "id", "equals": "${ITEM_ID}"}}
    )
    assert assertion["json_path"]["equals"] == 1
    ok, detail, reason = _check(assertion, body='{"id":1}\n')
    assert ok is True, detail
    assert reason is None


# --- Replay of acceptance run 38152175992 ---

_FIXTURE = Path(__file__).parent / "fixtures" / "acceptance-run-38152175992.json"


def _replay_probe(probe, recorded):
    """Run one recorded probe through the fixed executor.

    Captures are derived from the recorded act response body: for probe 0
    the act fetches the exact item the setup created, so its body carries
    the captured id. Returns the list of (passed, detail) per assertion.
    """
    ctx = SetupContext()
    ctx.declare_from_setup(probe.get("setup", []))
    for step in probe.get("setup", []):
        capture = step.get("capture")
        if capture:
            ctx.add(
                capture_bindings(
                    recorded["http_body"], recorded["http_headers"], capture
                )
            )
    results = []
    for assertion in probe["assert"]:
        substituted = ctx.apply_strict(assertion)
        passed, detail, _ = _check_assertion(
            substituted,
            recorded["http_status"],
            recorded["http_headers"],
            recorded["http_body"],
            None,
        )
        results.append((passed, detail))
    return results


def test_replay_acceptance_run_38152175992():
    fixture = json.loads(_FIXTURE.read_text())
    probes = fixture["parsed_probes"]
    assert len(probes) == 2

    def verdicts(side):
        out = []
        for i, probe in enumerate(probes):
            results = _replay_probe(probe, fixture["sides"][side][i])
            out.append([passed for passed, _ in results])
        return out

    # Probe 0: the id assertion now compares typed values -> PASS both sides.
    fix_parent = verdicts("fix_parent_results")
    fix = verdicts("fix_results")
    assert fix_parent[0] == [True, True, True], fix_parent[0]
    assert fix[0] == [True, True, True], fix[0]

    # Probe 1: body {} matches at fix^ (JSON equality), fails at fix.
    assert fix_parent[1] == [True, True], fix_parent[1]
    assert fix[1] == [False, False], fix[1]
