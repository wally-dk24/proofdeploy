"""Tests for proofdeploy.leakcheck (WO-5)."""

import pytest

from proofdeploy.leakcheck import (
    MIN_SECRET_LENGTH,
    SecretLeakError,
    assert_no_secrets,
    collect_forbidden_values,
    find_secret_occurrences,
)


def test_short_values_ignored():
    assert find_secret_occurrences("the admin user", ["admin"]) == []


def test_finds_secret_value():
    secret = "sk-live-abcdef1234567890"
    assert find_secret_occurrences(f"token={secret}!", [secret]) == [secret]


def test_deduplicates_and_orders():
    s1 = "token-alpha-12345678"
    s2 = "token-beta-123456789"
    text = f"{s2} then {s1} then {s2}"
    assert find_secret_occurrences(text, [s1, s2, s1]) == [s2, s1]


def test_non_string_values_skipped():
    assert find_secret_occurrences("12345", [12345, None, b"bytes"]) == []


def test_assert_no_secrets_passes_clean():
    assert_no_secrets("hello world", ["super-secret-value-123"], where="prompt")


def test_assert_no_secrets_raises():
    secret = "super-secret-value-123"
    with pytest.raises(SecretLeakError):
        assert_no_secrets(f"prompt with {secret} inside", [secret], where="prompt")


def test_min_secret_length_boundary():
    assert MIN_SECRET_LENGTH == 8
    assert find_secret_occurrences("abcdefgh", ["abcdefgh"]) == ["abcdefgh"]
    assert find_secret_occurrences("abcdefg", ["abcdefg"]) == []


def test_collect_forbidden_values_respects_public_keys():
    fixture = {
        "repo_name": "demo",
        "auth_token": "fixture-secret-token-xyz",
    }
    env = {"APP_ENV": "verification", "DB_PASSWORD": "db-secret-pw-123"}
    forbidden = collect_forbidden_values(
        fixture,
        env,
        public_fixture_keys=["repo_name"],
        public_contract_env_keys=["APP_ENV"],
        extra_secrets=["minted-jwt-value-abc"],
    )
    assert "fixture-secret-token-xyz" in forbidden
    assert "db-secret-pw-123" in forbidden
    assert "minted-jwt-value-abc" in forbidden
    assert "demo" not in forbidden
    assert "verification" not in forbidden
