"""Tests for setup capture and auth injection (WAL-58)."""

import pytest

from proofdeploy.setup_auth import (
    MissingCapture,
    SetupContext,
    UnknownPlaceholder,
    capture_bindings,
    collect_capture_names,
    substitute_bindings,
    substitute_strict,
    validate_harness_app_allowed,
)


def test_substitute_simple():
    assert substitute_bindings("Bearer ${AUTH_TOKEN}", {"AUTH_TOKEN": "abc"}) == "Bearer abc"


def test_substitute_unknown_left_as_is():
    assert substitute_bindings("${NOPE}", {}) == "${NOPE}"


def test_substitute_strict_known():
    out = substitute_strict("Bearer ${TOKEN}", {"TOKEN": "t"}, {"TOKEN"})
    assert out == "Bearer t"


def test_substitute_strict_unknown_raises():
    # Unknown names fail closed, never pass through literally.
    with pytest.raises(UnknownPlaceholder):
        substitute_strict("${NOPE}", {}, set())
    with pytest.raises(UnknownPlaceholder):
        substitute_strict({"h": "${NOPE}"}, {"TOKEN": "t"}, {"TOKEN"})


def test_substitute_strict_missing_capture_raises():
    # Declared but never set: a different, typed failure.
    with pytest.raises(MissingCapture):
        substitute_strict("${TOKEN}", {}, {"TOKEN"})


def test_collect_capture_names():
    steps = [
        {"type": "http", "act": {}, "capture": {"TOKEN": {"from": "body", "json_path": "t"}}},
        {"type": "http", "act": {}},
    ]
    assert collect_capture_names(steps) == {"TOKEN"}


def test_context_strict_flow():
    # End-to-end: declare, capture, substitute; unknown rejected.
    ctx = SetupContext()
    ctx.declare_from_setup(
        [{"type": "http", "act": {}, "capture": {"TOKEN": {"from": "body", "json_path": "t"}}}]
    )
    with pytest.raises(MissingCapture):
        ctx.apply_strict("Bearer ${TOKEN}")
    ctx.add(capture_bindings('{"t": "abc"}', {}, {"TOKEN": {"from": "body", "json_path": "t"}}))
    assert ctx.apply_strict("Bearer ${TOKEN}") == "Bearer abc"
    with pytest.raises(UnknownPlaceholder):
        ctx.apply_strict("${TYPO}")


def test_auth_token_precedence():
    # Fixture AUTH_TOKEN beats setup captures; refresh beats fixture.
    ctx = SetupContext(auth_token="fixture-token")
    ctx.add({"AUTH_TOKEN": "captured-token"})
    assert ctx.bindings["AUTH_TOKEN"] == "fixture-token"
    ctx.refresh_token("refreshed-token")
    assert ctx.bindings["AUTH_TOKEN"] == "refreshed-token"
    # A later capture must not clobber the refreshed token.
    ctx.add({"OTHER": "x"})
    assert ctx.bindings["AUTH_TOKEN"] == "refreshed-token"


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
