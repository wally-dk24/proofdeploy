"""Tests for setup capture and auth injection (WAL-58)."""

import pytest

from proofdeploy.setup_auth import (
    SetupContext,
    capture_bindings,
    substitute_bindings,
    validate_harness_app_allowed,
)


def test_substitute_simple():
    assert substitute_bindings("Bearer ${AUTH_TOKEN}", {"AUTH_TOKEN": "abc"}) == "Bearer abc"


def test_substitute_unknown_left_as_is():
    assert substitute_bindings("${NOPE}", {}) == "${NOPE}"


def test_substitute_nested():
    v = {"headers": {"Authorization": "Bearer ${TOKEN}"}, "list": ["${A}", "b"]}
    out = substitute_bindings(v, {"TOKEN": "t", "A": "a"})
    assert out == {"headers": {"Authorization": "Bearer t"}, "list": ["a", "b"]}


def test_capture_from_body():
    bindings = capture_bindings(
        '{"token": "xyz", "user": {"id": 7}}',
        {},
        {"TOKEN": {"from": "body", "json_path": "token"}},
    )
    assert bindings == {"TOKEN": "xyz"}


def test_capture_from_header():
    bindings = capture_bindings(
        "",
        {"x-token": "hdr-val"},
        {"TOKEN": {"from": "header", "name": "X-Token"}},
    )
    assert bindings == {"TOKEN": "hdr-val"}


def test_capture_missing_skipped():
    bindings = capture_bindings(
        "{}",
        {},
        {"TOKEN": {"from": "body", "json_path": "nope"}},
    )
    assert bindings == {}


def test_setup_context_auth_token_wins():
    ctx = SetupContext(auth_token="fixture-token")
    ctx.add({"AUTH_TOKEN": "setup-token", "OTHER": "x"})
    assert ctx.bindings["AUTH_TOKEN"] == "fixture-token"
    assert ctx.bindings["OTHER"] == "x"


def test_setup_context_apply():
    ctx = SetupContext(auth_token="tok")
    probe = {"act": {"headers": {"Authorization": "Bearer ${AUTH_TOKEN}"}}}
    out = ctx.apply(probe)
    assert out["act"]["headers"]["Authorization"] == "Bearer tok"
    # Original untouched.
    assert probe["act"]["headers"]["Authorization"] == "Bearer ${AUTH_TOKEN}"


def test_harness_app_rejected_when_not_allowed():
    steps = [{"type": "harness_app", "source": "x"}]
    with pytest.raises(ValueError, match="only allowed for dev-set"):
        validate_harness_app_allowed(steps, allow_harness_app=False)


def test_harness_app_allowed_for_devset():
    steps = [{"type": "harness_app", "source": "x"}]
    validate_harness_app_allowed(steps, allow_harness_app=True)  # no raise


def test_http_steps_pass_through():
    steps = [{"type": "http", "act": {}}]
    validate_harness_app_allowed(steps, allow_harness_app=False)  # no raise
