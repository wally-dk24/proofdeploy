"""Tests for setup capture and auth injection (WAL-58)."""

import pytest

from proofdeploy.setup_auth import (
    CredentialProvider,
    MissingCapture,
    SetupContext,
    UnknownPlaceholder,
    capture_bindings,
    collect_capture_names,
    mint_ghost_jwt,
    substitute_strict,
    validate_harness_app_allowed,
)


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
    # Fixture AUTH_TOKEN beats setup captures; provider token beats fixture.
    ctx = SetupContext(auth_token="fixture-token")
    ctx.add({"AUTH_TOKEN": "captured-token"})
    assert ctx.bindings["AUTH_TOKEN"] == "fixture-token"
    ctx.provide_token("provided-token", expires_in_seconds=300)
    assert ctx.bindings["AUTH_TOKEN"] == "provided-token"
    # A later capture must not clobber the provider token.
    ctx.add({"OTHER": "x"})
    assert ctx.bindings["AUTH_TOKEN"] == "provided-token"


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


# ---------------------------------------------------------------------------
# CredentialProvider: proactive, never reactive.
# ---------------------------------------------------------------------------


def test_mint_ghost_jwt_structure():
    import base64
    import hashlib
    import hmac
    import json as jsonlib

    key_id = "a1b2c3d4e5f60718293a4b5c6"
    secret_hex = "d4" * 32
    token = mint_ghost_jwt(f"{key_id}:{secret_hex}", ttl_seconds=300)
    header_b64, payload_b64, sig_b64 = token.split(".")

    def _dec(s):
        return jsonlib.loads(base64.urlsafe_b64decode(s + "=" * (-len(s) % 4)))

    header = _dec(header_b64)
    payload = _dec(payload_b64)
    assert header == {"alg": "HS256", "typ": "JWT", "kid": key_id}
    assert payload["aud"] == "/admin/"
    assert payload["exp"] - payload["iat"] == 300
    # Signature verifies against the secret.
    expected = (
        base64.urlsafe_b64encode(
            hmac.new(
                bytes.fromhex(secret_hex),
                f"{header_b64}.{payload_b64}".encode("ascii"),
                hashlib.sha256,
            ).digest()
        )
        .rstrip(b"=")
        .decode("ascii")
    )
    assert sig_b64 == expected


def test_mint_ghost_jwt_malformed_key():
    import pytest as _pytest

    with _pytest.raises(ValueError):
        mint_ghost_jwt("no-colon-here")
    with _pytest.raises(ValueError):
        mint_ghost_jwt("id:not-hex-zz")


def test_provider_ghost_jwt_always_needs_token():
    ctx = SetupContext(auth_token="fixture-token")
    p = CredentialProvider({"mode": "ghost_jwt", "key": "${ADMIN_KEY}"})
    # Even with a fixture token, ghost_jwt mints fresh per request.
    assert p.token_needed(ctx) is True


def test_provider_refresh_skips_fixture_token():
    # A fixture token has no known expiry: never refreshed proactively.
    ctx = SetupContext(auth_token="fixture-token")
    p = CredentialProvider({"mode": "refresh", "path": "/auth/refresh"})
    assert p.token_needed(ctx) is False


def test_provider_refresh_when_no_token():
    ctx = SetupContext()
    p = CredentialProvider({"mode": "refresh", "path": "/auth/refresh"})
    assert p.token_needed(ctx) is True


def test_provider_refresh_when_near_expiry():
    ctx = SetupContext()
    ctx.provide_token("tok", expires_in_seconds=30)  # within the 60s margin
    p = CredentialProvider({"mode": "refresh", "path": "/auth/refresh"})
    assert p.token_needed(ctx) is True


def test_provider_refresh_not_when_fresh():
    ctx = SetupContext()
    ctx.provide_token("tok", expires_in_seconds=3600)
    p = CredentialProvider({"mode": "refresh", "path": "/auth/refresh"})
    assert p.token_needed(ctx) is False


def test_token_near_expiry_unknown_is_false():
    # No expiry info (e.g. fixture token): never "near expiry".
    ctx = SetupContext(auth_token="x")
    assert ctx.token_near_expiry() is False
