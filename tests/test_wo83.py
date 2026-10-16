"""WO-8.3: auth providers — auth from the contract, per-run credentials.

Covers Addendum 44 items 1-5 at the unit/host level (the sandbox
end-to-end proofs live in tests/test_wo83_sandbox.py):

- yml.py validates the four modes and their keys; unknown keys or
  modes are refused; bootstrap_env declares names only.
- generate_bootstrap_env: fresh random values per call (per side and
  per run), email-shaped for EMAIL names.
- login / refresh (login, then refresh near expiry) / ghost_jwt
  (seed-written key file) / register end to end against the golden
  auth app (tests/fixtures/wo83_auth_app) running on the host.
- credentials differ per side: one side's bootstrap values do not
  authenticate against another side's instance.
- a missing key file -> INCONCLUSIVE auth, never a crash.
- planted leaks: a generated value planted in a record is redacted
  on write; planted in a prompt or bundle text it is refused by the
  leak check (the generated values are in the forbidden-values set).
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

from proofdeploy.executor import (
    Executor,
    InconclusiveReason,
    ProbeResult,
    Verdict,
)
from proofdeploy.leakcheck import (
    SecretLeakError,
    assert_no_secrets,
    collect_forbidden_values,
)
from proofdeploy.probe import SCHEMA_VERSION
from proofdeploy.runpair import (
    build_evidence_record,
    read_records,
    write_evidence_record,
)
from proofdeploy.yml import RepoContract, load_contract


def generate_bootstrap_env(names: list[str]) -> dict[str, str]:
    """The WO-8.3 generator, imported lazily so this module collects
    (and its tests fail individually) on a tree without it."""
    from proofdeploy.setup_auth import generate_bootstrap_env as _gen

    return _gen(names)

FIXTURE_APP = Path(__file__).parent / "fixtures" / "wo83_auth_app"

BASE_YML = "build: 'true'\nstart: 'python app.py'\nreadiness: /health\n"


def _contract(tmp_path: Path, auth_yaml: str) -> RepoContract:
    (tmp_path / "proofdeploy.yml").write_text(
        BASE_YML + "auth:\n" + auth_yaml, encoding="utf-8"
    )
    return load_contract(tmp_path / "proofdeploy.yml")


# ----------------------------------------------------------------------
# yml validation (Addendum 44 item 4)
# ----------------------------------------------------------------------


def test_login_mode_validates(tmp_path):
    contract = _contract(
        tmp_path,
        "  mode: login\n"
        "  path: /auth/login\n"
        "  method: POST\n"
        "  body:\n"
        "    email: '${ADMIN_EMAIL}'\n"
        "    password: '${ADMIN_PASSWORD}'\n"
        "  token_json_path: access_token\n"
        "  bootstrap_env: [ADMIN_EMAIL, ADMIN_PASSWORD]\n",
    )
    assert contract.auth is not None
    assert contract.auth["mode"] == "login"
    assert contract.auth["body"] == {
        "email": "${ADMIN_EMAIL}",
        "password": "${ADMIN_PASSWORD}",
    }
    assert contract.auth["bootstrap_env"] == ["ADMIN_EMAIL", "ADMIN_PASSWORD"]


def test_refresh_mode_with_login_validates(tmp_path):
    contract = _contract(
        tmp_path,
        "  mode: refresh\n"
        "  path: /auth/refresh\n"
        "  body:\n"
        "    refresh_token: '${REFRESH_TOKEN}'\n"
        "  token_json_path: access_token\n"
        "  refresh_token_json_path: refresh_token\n"
        "  login_path: /login\n"
        "  login_body:\n"
        "    email: '${ADMIN_EMAIL}'\n"
        "    password: '${ADMIN_PASSWORD}'\n",
    )
    assert contract.auth is not None
    assert contract.auth["mode"] == "refresh"
    assert contract.auth["login_path"] == "/login"


def test_ghost_jwt_key_file_validates(tmp_path):
    contract = _contract(
        tmp_path,
        "  mode: ghost_jwt\n  key_file: .pd-auth/admin_key\n  ttl_seconds: 300\n",
    )
    assert contract.auth is not None
    assert contract.auth["key_file"] == ".pd-auth/admin_key"
    assert contract.auth["ttl_seconds"] == 300


def test_ghost_jwt_key_placeholder_validates(tmp_path):
    contract = _contract(tmp_path, "  mode: ghost_jwt\n  key: '${ADMIN_KEY}'\n")
    assert contract.auth is not None
    assert contract.auth["key"] == "${ADMIN_KEY}"


def test_unknown_auth_mode_refused(tmp_path):
    with pytest.raises(ValueError, match="auth.mode"):
        _contract(tmp_path, "  mode: oauth2\n  path: /login\n")


def test_unknown_auth_key_refused(tmp_path):
    with pytest.raises(ValueError, match="unknown keys"):
        _contract(
            tmp_path,
            "  mode: login\n  path: /login\n  token_json_path: token\n"
            "  grant_type: password\n",
        )


def test_login_unknown_key_from_other_mode_refused(tmp_path):
    # username_field belongs to register; in login mode it is unknown.
    with pytest.raises(ValueError, match="unknown keys"):
        _contract(
            tmp_path,
            "  mode: login\n  path: /login\n  token_json_path: token\n"
            "  username_field: username\n",
        )


def test_bootstrap_env_must_be_a_list_of_names(tmp_path):
    with pytest.raises(ValueError, match="bootstrap_env"):
        _contract(
            tmp_path,
            "  mode: login\n  path: /login\n  token_json_path: token\n"
            "  bootstrap_env: ADMIN_EMAIL\n",
        )


def test_bootstrap_env_bad_name_refused(tmp_path):
    with pytest.raises(ValueError, match="bootstrap_env"):
        _contract(
            tmp_path,
            "  mode: login\n  path: /login\n  token_json_path: token\n"
            "  bootstrap_env: ['NOT-A-NAME']\n",
        )


def test_bootstrap_env_duplicate_refused(tmp_path):
    with pytest.raises(ValueError, match="bootstrap_env"):
        _contract(
            tmp_path,
            "  mode: login\n  path: /login\n  token_json_path: token\n"
            "  bootstrap_env: [ADMIN_EMAIL, ADMIN_EMAIL]\n",
        )


def test_ghost_jwt_needs_exactly_one_key_source(tmp_path):
    with pytest.raises(ValueError, match="exactly one"):
        _contract(tmp_path, "  mode: ghost_jwt\n")
    with pytest.raises(ValueError, match="exactly one"):
        _contract(
            tmp_path,
            "  mode: ghost_jwt\n  key: '${ADMIN_KEY}'\n"
            "  key_file: .pd-auth/admin_key\n",
        )


def test_ghost_jwt_key_file_must_stay_in_checkout(tmp_path):
    with pytest.raises(ValueError, match="inside"):
        _contract(tmp_path, "  mode: ghost_jwt\n  key_file: /etc/passwd\n")
    with pytest.raises(ValueError, match="inside"):
        _contract(tmp_path, "  mode: ghost_jwt\n  key_file: ../admin_key\n")


def test_ghost_jwt_ttl_must_be_positive_int(tmp_path):
    with pytest.raises(ValueError, match="ttl_seconds"):
        _contract(
            tmp_path,
            "  mode: ghost_jwt\n  key_file: .pd-auth/admin_key\n"
            "  ttl_seconds: '300'\n",
        )


# ----------------------------------------------------------------------
# Bootstrap generation (Addendum 43 conditions 1-2)
# ----------------------------------------------------------------------


def test_generate_bootstrap_env_fresh_per_call():
    names = ["ADMIN_EMAIL", "ADMIN_PASSWORD"]
    first = generate_bootstrap_env(names)
    second = generate_bootstrap_env(names)
    assert set(first) == set(names)
    # Fresh per run: no value is reused across calls.
    assert first != second
    for value in first.values():
        assert value not in second.values()
    # Email-shaped for EMAIL names; long random hex otherwise.
    assert first["ADMIN_EMAIL"].endswith("@example.com")
    assert len(first["ADMIN_PASSWORD"]) >= 16


def test_generate_bootstrap_env_distinct_within_call():
    values = generate_bootstrap_env(["A_SECRET", "B_SECRET", "C_SECRET"])
    assert len(set(values.values())) == 3


# ----------------------------------------------------------------------
# The golden auth app on the host
# ----------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_ready(url: str, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url + "/health", timeout=2) as resp:
                if resp.status == 200:
                    return
        except Exception:
            time.sleep(0.2)
    raise RuntimeError(f"golden auth app did not become ready at {url}")


@pytest.fixture()
def auth_app(tmp_path):
    """The golden auth app, migrated + seeded like provisioning does.

    Yields (base_url, workdir, bootstrap_values). The seed runs with
    the generated bootstrap values in its env — the same values the
    install container would see.
    """

    def _start(ttl_seconds: str | None = None, seed: bool = True):
        work = Path(tmp_path) / f"app-{_free_port()}"
        work.mkdir(parents=True)
        for name in ("app.py", "migrate.py", "seed.py"):
            (work / name).write_text(
                (FIXTURE_APP / name).read_text(encoding="utf-8"), encoding="utf-8"
            )
        bootstrap = generate_bootstrap_env(["ADMIN_EMAIL", "ADMIN_PASSWORD"])
        env = {
            **os.environ,
            "APP_DB": str(work / "app.db"),
            "ADMIN_EMAIL": bootstrap["ADMIN_EMAIL"],
            "ADMIN_PASSWORD": bootstrap["ADMIN_PASSWORD"],
        }
        if ttl_seconds is not None:
            env["TOKEN_TTL_SECONDS"] = ttl_seconds
        subprocess.run(
            [sys.executable, "migrate.py"], cwd=work, env=env, check=True,
            capture_output=True,
        )
        if seed:
            subprocess.run(
                [sys.executable, "seed.py"], cwd=work, env=env, check=True,
                capture_output=True,
            )
        port = _free_port()
        proc = subprocess.Popen(
            [sys.executable, "app.py"],
            cwd=work,
            env={**env, "PORT": str(port)},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        url = f"http://127.0.0.1:{port}"
        try:
            _wait_ready(url)
        except Exception:
            proc.terminate()
            raise
        return url, work, bootstrap, proc

    procs: list[subprocess.Popen] = []

    def start(**kwargs):
        url, work, bootstrap, proc = _start(**kwargs)
        procs.append(proc)
        return url, work, bootstrap

    yield start
    for proc in procs:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def _me_probe() -> dict:
    return {
        "act": {
            "method": "GET",
            "path": "/me",
            "headers": {"Authorization": "Bearer ${AUTH_TOKEN}"},
        },
        "assert": [{"type": "status", "equals": 200}],
    }


LOGIN_CONFIG = {
    "mode": "login",
    "path": "/login",
    "method": "POST",
    "body": {"email": "${ADMIN_EMAIL}", "password": "${ADMIN_PASSWORD}"},
    "token_json_path": "access_token",
    "expires_in_json_path": "expires_in",
}


def test_login_mode_end_to_end(auth_app):
    url, _work, bootstrap = auth_app()
    ex = Executor(
        target_url=url,
        provisioned=True,
        credential_config=dict(LOGIN_CONFIG),
        bootstrap_env=bootstrap,
    )
    r = ex.run_probe(_me_probe())
    assert r.verdict == Verdict.PASS, r.details
    assert any("(login)" in d for d in r.details)
    # The bootstrap values rode the result's redaction channel.
    assert set(bootstrap.values()) <= set(r.bootstrap_values)


def test_login_with_other_sides_credentials_fails(auth_app):
    """Credentials differ per side: side A's generated values do not
    authenticate against side B's instance (Addendum 44 item 5)."""
    url_a, _wa, bootstrap_a = auth_app()
    url_b, _wb, bootstrap_b = auth_app()
    assert bootstrap_a != bootstrap_b
    ex = Executor(
        target_url=url_b,
        provisioned=True,
        credential_config=dict(LOGIN_CONFIG),
        bootstrap_env=bootstrap_a,
    )
    r = ex.run_probe(_me_probe())
    assert r.verdict == Verdict.INCONCLUSIVE
    assert r.reason == InconclusiveReason.AUTH


# ----------------------------------------------------------------------
# login mode: token_header (D120) — the token arrives in a response
# header, not the JSON body. Driven through Executor.run_probe with
# the executor's HTTP replaced by a scripted target.
# ----------------------------------------------------------------------

HEADER_LOGIN_CONFIG = {
    "mode": "login",
    "path": "/login",
    "method": "POST",
    "body": {"username": "${ADMIN_USER}", "password": "${ADMIN_PASSWORD}"},
    "token_header": "X-Session-Token",
}

_HEADER_BOOTSTRAP = {"ADMIN_USER": "root", "ADMIN_PASSWORD": "pw"}
_HEADER_TOKEN = "header-token-123"


def _fake_http(monkeypatch, login_response, seen):
    """Script the executor's HTTP layer.

    `login_response` is the (status, headers, body) for POST /login.
    Any other request is the probe's act: it is recorded in `seen`
    and answered 200 only when it carries the header token as Bearer.
    """
    import proofdeploy.executor as executor_mod

    def fake_do_http(method, url, headers, body, timeout):
        if url.endswith("/login"):
            status, resp_headers, resp_body = login_response
            return status, resp_headers, resp_body, []
        seen.append((method, url, dict(headers or {})))
        if (headers or {}).get("Authorization") == f"Bearer {_HEADER_TOKEN}":
            return 200, {}, '{"ok": true}', []
        return 401, {}, '{"error": "unauthorized"}', []

    monkeypatch.setattr(executor_mod, "_do_http", fake_do_http)


def _header_executor() -> Executor:
    return Executor(
        target_url="http://127.0.0.1:8931",
        provisioned=True,
        credential_config=dict(HEADER_LOGIN_CONFIG),
        bootstrap_env=dict(_HEADER_BOOTSTRAP),
    )


def test_login_token_header_validates(tmp_path):
    contract = _contract(
        tmp_path,
        "  mode: login\n"
        "  path: /admin/login\n"
        "  token_header: X-Auth-Token\n",
    )
    assert contract.auth is not None
    assert contract.auth["token_header"] == "X-Auth-Token"
    assert "token_json_path" not in contract.auth


def test_login_both_token_sources_refused(tmp_path):
    with pytest.raises(ValueError, match="exactly one"):
        _contract(
            tmp_path,
            "  mode: login\n  path: /login\n  token_json_path: token\n"
            "  token_header: X-Auth-Token\n",
        )


def test_login_no_token_source_refused(tmp_path):
    with pytest.raises(ValueError, match="exactly one"):
        _contract(tmp_path, "  mode: login\n  path: /login\n")


def test_login_token_header_present_token_used(monkeypatch):
    seen: list = []
    _fake_http(
        monkeypatch,
        (200, {"x-session-token": _HEADER_TOKEN}, '{"user": {"id": 1}}'),
        seen,
    )
    r = _header_executor().run_probe(_me_probe())
    assert r.verdict == Verdict.PASS, r.details
    assert any("(login)" in d for d in r.details)
    # The act carried the header token, presented as before (Bearer).
    assert seen and seen[0][2].get("Authorization") == f"Bearer {_HEADER_TOKEN}"


def test_login_token_header_name_case_insensitive(monkeypatch):
    # The declared name is matched case-insensitively (the executor
    # normalizes the response header names itself).
    seen: list = []
    _fake_http(
        monkeypatch,
        (200, {"X-SeSsIoN-ToKeN": _HEADER_TOKEN}, '{"user": {"id": 1}}'),
        seen,
    )
    r = _header_executor().run_probe(_me_probe())
    assert r.verdict == Verdict.PASS, r.details


def test_login_token_header_missing_is_inconclusive_auth(monkeypatch):
    seen: list = []
    _fake_http(monkeypatch, (200, {}, '{"user": {"id": 1}}'), seen)
    r = _header_executor().run_probe(_me_probe())
    assert r.verdict == Verdict.INCONCLUSIVE
    assert r.reason == InconclusiveReason.AUTH
    assert seen == []  # the act is never sent without a credential


def test_login_token_header_empty_is_inconclusive_auth(monkeypatch):
    seen: list = []
    _fake_http(monkeypatch, (200, {"x-session-token": ""}, "{}"), seen)
    r = _header_executor().run_probe(_me_probe())
    assert r.verdict == Verdict.INCONCLUSIVE
    assert r.reason == InconclusiveReason.AUTH
    assert seen == []


def test_login_200_without_header_never_a_token(monkeypatch):
    # GraphQL-style failure: HTTP 200, an error body — and even a
    # token-shaped value in the body. In header mode the body is
    # never the token carrier, so this is an auth failure.
    seen: list = []
    _fake_http(
        monkeypatch,
        (
            200,
            {},
            '{"errors": [{"message": "invalid credentials"}],'
            ' "data": {"login": {"token": "body-token-must-not-be-used"}}}',
        ),
        seen,
    )
    r = _header_executor().run_probe(_me_probe())
    assert r.verdict == Verdict.INCONCLUSIVE
    assert r.reason == InconclusiveReason.AUTH
    assert seen == []


def test_refresh_mode_logs_in_then_refreshes_near_expiry(auth_app):
    # TTL 59 s is inside the provider's 60 s proactive-refresh margin,
    # so the second ensure in the same probe run must refresh.
    url, _work, bootstrap = auth_app(ttl_seconds="59")
    config = {
        "mode": "refresh",
        "path": "/auth/refresh",
        "method": "POST",
        "body": {"refresh_token": "${REFRESH_TOKEN}"},
        "token_json_path": "access_token",
        "refresh_token_json_path": "refresh_token",
        "expires_in_json_path": "expires_in",
        "login_path": "/login",
        "login_body": {"email": "${ADMIN_EMAIL}", "password": "${ADMIN_PASSWORD}"},
    }
    ex = Executor(
        target_url=url,
        provisioned=True,
        credential_config=config,
        bootstrap_env=bootstrap,
    )
    # A setup GET plus the act GET: the provider runs before each
    # request — login before the first, refresh before the second.
    probe = {
        "setup": [
            {
                "type": "http",
                "act": {
                    "method": "GET",
                    "path": "/me",
                    "headers": {"Authorization": "Bearer ${AUTH_TOKEN}"},
                },
            }
        ],
        "act": {
            "method": "GET",
            "path": "/me",
            "headers": {"Authorization": "Bearer ${AUTH_TOKEN}"},
        },
        "assert": [{"type": "status", "equals": 200}],
    }
    r = ex.run_probe(probe)
    assert r.verdict == Verdict.PASS, r.details
    assert any("(login)" in d for d in r.details), r.details
    assert any(
        d == "credential provider: POST /auth/refresh" for d in r.details
    ), r.details
    # The refresh token from login was captured for redaction and use.
    assert "REFRESH_TOKEN" in r.captured_bindings


# ----------------------------------------------------------------------
# expires_in_unit (D137): the value at expires_in_json_path is a
# lifetime in seconds unless the contract declares milliseconds —
# Directus's data.expires is 900000 ms for a 15-minute token, and
# read as seconds it lands ~10 days out, so a provider-issued token
# would never come near expiry and proactive refresh never fires.
# The golden app reports expires_in = its TOKEN_TTL_SECONDS number
# verbatim, so the ttl values below ARE the reported lifetimes, in
# whichever unit the config declares.
# ----------------------------------------------------------------------


def test_expires_in_unit_validates_login_and_refresh(tmp_path):
    contract = _contract(
        tmp_path,
        "  mode: login\n  path: /login\n  token_json_path: token\n"
        "  expires_in_json_path: expires_in\n"
        "  expires_in_unit: milliseconds\n",
    )
    assert contract.auth is not None
    assert contract.auth["expires_in_unit"] == "milliseconds"
    contract = _contract(
        tmp_path,
        "  mode: refresh\n  path: /auth/refresh\n  token_json_path: token\n"
        "  expires_in_json_path: expires_in\n  expires_in_unit: seconds\n",
    )
    assert contract.auth is not None
    assert contract.auth["expires_in_unit"] == "seconds"


def test_expires_in_unit_bad_value_refused(tmp_path):
    with pytest.raises(ValueError, match="expires_in_unit"):
        _contract(
            tmp_path,
            "  mode: login\n  path: /login\n  token_json_path: token\n"
            "  expires_in_json_path: expires_in\n"
            "  expires_in_unit: minutes\n",
        )
    with pytest.raises(ValueError, match="expires_in_unit"):
        _contract(
            tmp_path,
            "  mode: refresh\n  path: /auth/refresh\n"
            "  token_json_path: token\n  expires_in_unit: ms\n",
        )


def test_expires_in_unit_refused_in_register_mode(tmp_path):
    # register mode never reads expires_in_json_path, so the unit
    # key is unknown there and refused like any other stray key.
    with pytest.raises(ValueError, match="unknown keys"):
        _contract(
            tmp_path,
            "  mode: register\n  path: /register\n"
            "  username_field: username\n  password_field: password\n"
            "  token_json_path: token\n  expires_in_unit: seconds\n",
        )


def _refresh_config(**over):
    config = {
        "mode": "refresh",
        "path": "/auth/refresh",
        "method": "POST",
        "body": {"refresh_token": "${REFRESH_TOKEN}"},
        "token_json_path": "access_token",
        "refresh_token_json_path": "refresh_token",
        "expires_in_json_path": "expires_in",
        "login_path": "/login",
        "login_body": {"email": "${ADMIN_EMAIL}", "password": "${ADMIN_PASSWORD}"},
    }
    config.update(over)
    return config


def _two_request_probe() -> dict:
    # A setup GET plus the act GET: the provider runs before each
    # request, so a token that lands inside the proactive-refresh
    # margin at acquisition is renewed before the second request.
    return {
        "setup": [
            {
                "type": "http",
                "act": {
                    "method": "GET",
                    "path": "/me",
                    "headers": {"Authorization": "Bearer ${AUTH_TOKEN}"},
                },
            }
        ],
        "act": {
            "method": "GET",
            "path": "/me",
            "headers": {"Authorization": "Bearer ${AUTH_TOKEN}"},
        },
        "assert": [{"type": "status", "equals": 200}],
    }


def test_refresh_expires_in_milliseconds_near_expiry_refreshes(auth_app):
    # expires_in = 59000 in the declared unit (milliseconds) is a
    # 59 s lifetime — inside the provider's 60 s margin, so the
    # second request refreshes. Read as seconds it would look ~16
    # hours fresh and no refresh would fire.
    url, _work, bootstrap = auth_app(ttl_seconds="59000")
    ex = Executor(
        target_url=url,
        provisioned=True,
        credential_config=_refresh_config(expires_in_unit="milliseconds"),
        bootstrap_env=bootstrap,
    )
    r = ex.run_probe(_two_request_probe())
    assert r.verdict == Verdict.PASS, r.details
    assert sum("(login)" in d for d in r.details) == 1
    assert any(
        d == "credential provider: POST /auth/refresh" for d in r.details
    ), r.details


def test_refresh_expires_in_seconds_near_expiry_refreshes(auth_app):
    # The same drive with the unit declared explicitly as seconds:
    # 59 s is inside the margin and refresh fires before request 2.
    url, _work, bootstrap = auth_app(ttl_seconds="59")
    ex = Executor(
        target_url=url,
        provisioned=True,
        credential_config=_refresh_config(expires_in_unit="seconds"),
        bootstrap_env=bootstrap,
    )
    r = ex.run_probe(_two_request_probe())
    assert r.verdict == Verdict.PASS, r.details
    assert sum("(login)" in d for d in r.details) == 1
    assert any(
        d == "credential provider: POST /auth/refresh" for d in r.details
    ), r.details


def test_refresh_expires_in_milliseconds_fresh_token_not_refreshed(auth_app):
    # 3_600_000 ms = 3600 s: far outside the margin — the unit must
    # not cause spurious refreshes either. One login, no refresh.
    url, _work, bootstrap = auth_app(ttl_seconds="3600000")
    ex = Executor(
        target_url=url,
        provisioned=True,
        credential_config=_refresh_config(expires_in_unit="milliseconds"),
        bootstrap_env=bootstrap,
    )
    r = ex.run_probe(_two_request_probe())
    assert r.verdict == Verdict.PASS, r.details
    assert sum("(login)" in d for d in r.details) == 1
    assert not any(
        d == "credential provider: POST /auth/refresh" for d in r.details
    ), r.details


def test_login_expires_in_milliseconds_near_expiry_reacquires(auth_app):
    # The login exchange honors the unit too: a 59000 ms lifetime
    # lands inside the margin, so the second request re-acquires
    # (a second login) instead of riding a token it believes fresh
    # for ~16 hours.
    url, _work, bootstrap = auth_app(ttl_seconds="59000")
    config = dict(LOGIN_CONFIG)
    config["expires_in_unit"] = "milliseconds"
    ex = Executor(
        target_url=url,
        provisioned=True,
        credential_config=config,
        bootstrap_env=bootstrap,
    )
    r = ex.run_probe(_two_request_probe())
    assert r.verdict == Verdict.PASS, r.details
    assert sum("(login)" in d for d in r.details) == 2, r.details


def test_login_expires_in_seconds_default_single_acquisition(auth_app):
    # No unit declared: the lifetime is seconds, unchanged — a
    # 3600 s token needs no second acquisition across both requests.
    url, _work, bootstrap = auth_app(ttl_seconds="3600")
    ex = Executor(
        target_url=url,
        provisioned=True,
        credential_config=dict(LOGIN_CONFIG),
        bootstrap_env=bootstrap,
    )
    r = ex.run_probe(_two_request_probe())
    assert r.verdict == Verdict.PASS, r.details
    assert sum("(login)" in d for d in r.details) == 1, r.details


def test_ghost_jwt_from_seed_written_key_file(auth_app):
    url, work, _bootstrap = auth_app()
    key_file = work / ".pd-auth" / "admin_key"
    assert key_file.is_file()
    key_value = key_file.read_text(encoding="utf-8").strip()
    ex = Executor(
        target_url=url,
        provisioned=True,
        credential_config={
            "mode": "ghost_jwt",
            "key_value": key_value,
            "ttl_seconds": 300,
        },
    )
    probe = {
        "act": {
            "method": "GET",
            "path": "/admin/site",
            "headers": {"Authorization": "Ghost ${AUTH_TOKEN}"},
        },
        "assert": [{"type": "status", "equals": 200}],
    }
    r = ex.run_probe(probe)
    assert r.verdict == Verdict.PASS, r.details
    # The read key joined the run's minted/redaction channel.
    assert key_value in r.minted_tokens


def test_ghost_jwt_missing_key_file_is_inconclusive_auth(auth_app):
    url, _work, _bootstrap = auth_app()
    ex = Executor(
        target_url=url,
        provisioned=True,
        credential_config={
            "mode": "ghost_jwt",
            "key_error": (
                "auth key file not found or empty: .pd-auth/admin_key "
                "(declared by the contract; the instance's seed must "
                "write it during provisioning)"
            ),
        },
    )
    probe = {
        "act": {
            "method": "GET",
            "path": "/admin/site",
            "headers": {"Authorization": "Ghost ${AUTH_TOKEN}"},
        },
        "assert": [{"type": "status", "equals": 200}],
    }
    r = ex.run_probe(probe)  # never a crash
    assert r.verdict == Verdict.INCONCLUSIVE
    assert r.reason == InconclusiveReason.AUTH
    assert any("key file not found" in d for d in r.details)


def test_register_mode_still_works_on_golden(auth_app):
    url, _work, _bootstrap = auth_app()
    ex = Executor(
        target_url=url,
        provisioned=True,
        credential_config={
            "mode": "register",
            "path": "/register",
            "method": "POST",
            "username_field": "username",
            "password_field": "password",
            "token_json_path": "token",
        },
    )
    r = ex.run_probe(_me_probe())
    assert r.verdict == Verdict.PASS, r.details


# ----------------------------------------------------------------------
# Orchestrator wiring (no sandbox): key file read, config shaping
# ----------------------------------------------------------------------


def _orchestrator(tmp_path: Path):
    from proofdeploy.orchestrator import MeasureConfig, Orchestrator

    cfg = MeasureConfig(
        repo_dir=tmp_path,
        output_dir=tmp_path / "out",
        workdir=tmp_path / "work",
        bug_id="b",
        rev_bug_base="x",
        rev_bug="x",
        rev_fix="x",
    )
    return Orchestrator(cfg)


def _side(tmp_path: Path, contract: RepoContract):
    from proofdeploy.orchestrator import _Side
    from proofdeploy.runner import ProvisionResult, ProvisionStatus

    return _Side(
        rev="x",
        sha="0" * 40,
        snapshot_dir=tmp_path,
        contract=contract,
        provision=ProvisionResult(status=ProvisionStatus.READY),
    )


def test_credential_config_reads_ghost_key_file(tmp_path):
    contract = _contract(
        tmp_path,
        "  mode: ghost_jwt\n  key_file: .pd-auth/admin_key\n"
        "  bootstrap_env: [ADMIN_EMAIL]\n",
    )
    key_dir = tmp_path / ".pd-auth"
    key_dir.mkdir()
    (key_dir / "admin_key").write_text("abc123:deadbeef\n", encoding="utf-8")
    orch = _orchestrator(tmp_path)
    side = _side(tmp_path, contract)
    cfg = orch._credential_config(side)
    assert cfg is not None
    assert cfg["key_value"] == "abc123:deadbeef"
    assert "key_file" not in cfg
    assert "bootstrap_env" not in cfg
    # The read key is a generated secret of the side (redaction).
    assert "abc123:deadbeef" in side.generated_secrets


def test_credential_config_missing_key_file_no_crash(tmp_path):
    contract = _contract(
        tmp_path, "  mode: ghost_jwt\n  key_file: .pd-auth/admin_key\n"
    )
    orch = _orchestrator(tmp_path)
    side = _side(tmp_path, contract)
    cfg = orch._credential_config(side)  # never raises
    assert cfg is not None
    assert "key_value" not in cfg
    assert "key file not found" in cfg["key_error"]


def test_side_bootstrap_and_record_shapes(tmp_path):
    contract = _contract(
        tmp_path,
        "  mode: login\n  path: /login\n  token_json_path: token\n"
        "  bootstrap_env: [ADMIN_EMAIL, ADMIN_PASSWORD]\n",
    )
    orch = _orchestrator(tmp_path)
    values = orch._side_bootstrap(contract)
    assert set(values) == {"ADMIN_EMAIL", "ADMIN_PASSWORD"}
    side = _side(tmp_path, contract)
    side.bootstrap_env = values
    side.generated_secrets = list(values.values())
    # The record names the env names as "generated" — never values.
    entry = orch._auth_bootstrap(side)
    assert entry == {"generated": ["ADMIN_EMAIL", "ADMIN_PASSWORD"]}
    assert json.dumps(entry).find(values["ADMIN_PASSWORD"]) == -1
    # Redaction values: configured extras plus the side's secrets.
    assert set(values.values()) <= set(orch._record_secret_values([side]))


def test_side_bootstrap_absent_means_no_values(tmp_path):
    contract = _contract(
        tmp_path,
        "  mode: register\n  path: /register\n  method: POST\n"
        "  username_field: username\n  password_field: password\n"
        "  token_json_path: token\n",
    )
    orch = _orchestrator(tmp_path)
    assert orch._side_bootstrap(contract) == {}


# ----------------------------------------------------------------------
# Planted leaks are refused / redacted (Addendum 44 item 5)
# ----------------------------------------------------------------------


def _evidence_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "records"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    return repo


def test_planted_bootstrap_value_in_record_is_redacted(tmp_path):
    """A generated value planted in the record text never reaches the
    committed record: the write path's secret set includes it."""
    value = generate_bootstrap_env(["ADMIN_PASSWORD"])["ADMIN_PASSWORD"]
    record = build_evidence_record(
        run_kind="clean",
        clean_id="c1",
        runner_log=[f"planted leak: {value}"],
    )
    repo = _evidence_repo(tmp_path)
    write_evidence_record(record, repo, repo, secret_values=[value])
    committed = (repo / "runs.jsonl").read_text(encoding="utf-8")
    assert value not in committed
    assert "***REDACTED***" in committed


def test_bootstrap_values_auto_collected_from_probe_results(tmp_path):
    """Even a caller that passes no secret_values cannot leak a
    bootstrap value: build_evidence_record collects the values from
    the probe results themselves (the minted-tokens channel)."""
    bootstrap = generate_bootstrap_env(["ADMIN_EMAIL", "ADMIN_PASSWORD"])
    result = ProbeResult(
        probe_index=0,
        verdict=Verdict.PASS,
        details=[f"planted leak: {bootstrap['ADMIN_PASSWORD']}"],
        bootstrap_values=list(bootstrap.values()),
    )
    record = build_evidence_record(
        run_kind="clean", clean_id="c1", c_results=[result]
    )
    assert bootstrap["ADMIN_PASSWORD"] in record["minted_tokens"]
    repo = _evidence_repo(tmp_path)
    write_evidence_record(record, repo, repo)  # no secret_values passed
    committed = (repo / "runs.jsonl").read_text(encoding="utf-8")
    assert bootstrap["ADMIN_PASSWORD"] not in committed
    assert bootstrap["ADMIN_EMAIL"] not in committed


def test_planted_bootstrap_value_in_prompt_is_refused():
    value = generate_bootstrap_env(["ADMIN_PASSWORD"])["ADMIN_PASSWORD"]
    forbidden = collect_forbidden_values(None, None, extra_secrets=[value])
    assert value in forbidden
    with pytest.raises(SecretLeakError):
        assert_no_secrets(
            f"the prompt mentions {value}", forbidden, where="author prompt"
        )


def test_planted_bootstrap_value_in_bundle_is_refused(tmp_path):
    value = generate_bootstrap_env(["ADMIN_PASSWORD"])["ADMIN_PASSWORD"]
    forbidden = collect_forbidden_values(None, None, extra_secrets=[value])
    bundle_file = tmp_path / "bundle.txt"
    bundle_file.write_text(f"bundle content {value}", encoding="utf-8")
    with pytest.raises(SecretLeakError):
        assert_no_secrets(
            bundle_file.read_text(encoding="utf-8"), forbidden, where="bundle file"
        )


def test_record_names_bootstrap_generated_never_values(tmp_path):
    bootstrap = generate_bootstrap_env(["ADMIN_EMAIL", "ADMIN_PASSWORD"])
    record = build_evidence_record(
        run_kind="clean",
        clean_id="c1",
        auth_bootstrap={"c": {"generated": sorted(bootstrap)}},
    )
    assert record["schema_version"] == "1.6.0"
    assert record["auth_bootstrap"] == {
        "c": {"generated": ["ADMIN_EMAIL", "ADMIN_PASSWORD"]}
    }
    blob = json.dumps(record)
    for value in bootstrap.values():
        assert value not in blob


def test_sandbox_log_never_holds_registered_secret():
    """A43 condition 3 at the sandbox layer: podman command echoes
    carry -e KEY=VALUE into the provision logs (they land in evidence
    records and CI logs). Once the orchestrator registers a side's
    generated values, no log line — already written or future — may
    hold one. Non-secret values stay readable."""
    from proofdeploy.sandbox import Sandbox, SandboxConfig

    sb = Sandbox(SandboxConfig())
    sb.log.append("$ podman run -e ADMIN_PASSWORD=hunter2 -e APP_DB=app.db img")
    sb.register_secret_values(["hunter2"])
    joined = "\n".join(sb.log)
    assert "hunter2" not in joined
    assert "***REDACTED***" in joined
    assert "app.db" in joined
    sb.log.append("output mentioning hunter2 again")
    assert "hunter2" not in "\n".join(sb.log)


def test_older_records_still_load(tmp_path):
    """Schema discipline: a 1.3.0 record (no auth_bootstrap) loads."""
    old = {
        "record_id": "old1",
        "record_type": "evidence",
        "schema_version": "1.3.0",
        "run_kind": "clean",
    }
    (tmp_path / "runs.jsonl").write_text(json.dumps(old) + "\n", encoding="utf-8")
    records = read_records(tmp_path)
    assert records[0]["record_id"] == "old1"
    assert records[0].get("auth_bootstrap") is None
    assert SCHEMA_VERSION == "1.6.0"


# ── Addendum 62: an auth path must not be able to rewrite the target ──
#
# The provider builds its URL by concatenating the contract's path
# onto the target URL, and the write gate (WO-7.3) allows any host
# for a provisioned executor — so a path like "@evil.example/x"
# (userinfo) would send the login POST, generated credentials in the
# body, to a third party. Two layers refuse it: the validator at
# load, and the executor's parsed host/port check before any send.

_EVIL_AUTH_PATHS = [
    "@evil.example/x",  # userinfo: rewrites the host outright
    "//evil.example/x",  # scheme-relative host
    "http://evil.example/x",  # a scheme, not a path
    "login",  # no leading slash at all
    "/log\\in",  # backslash escape
    "/x://evil.example",  # a scheme embedded in the path
]


def _register_yaml(path: str) -> str:
    return (
        "  mode: register\n"
        f"  path: '{path}'\n"
        "  method: POST\n"
        "  username_field: username\n"
        "  password_field: password\n"
        "  token_json_path: token\n"
    )


@pytest.mark.parametrize("bad_path", _EVIL_AUTH_PATHS)
def test_register_path_cannot_rewrite_host(tmp_path, bad_path):
    with pytest.raises(ValueError, match=r"auth\.path"):
        _contract(tmp_path, _register_yaml(bad_path))


@pytest.mark.parametrize("bad_path", _EVIL_AUTH_PATHS)
def test_login_path_cannot_rewrite_host(tmp_path, bad_path):
    with pytest.raises(ValueError, match=r"auth\.path"):
        _contract(
            tmp_path,
            "  mode: login\n"
            f"  path: '{bad_path}'\n"
            "  token_json_path: token\n",
        )


@pytest.mark.parametrize("bad_path", _EVIL_AUTH_PATHS)
def test_refresh_path_cannot_rewrite_host(tmp_path, bad_path):
    with pytest.raises(ValueError, match=r"auth\.path"):
        _contract(
            tmp_path,
            "  mode: refresh\n"
            f"  path: '{bad_path}'\n"
            "  token_json_path: token\n",
        )


@pytest.mark.parametrize("bad_path", _EVIL_AUTH_PATHS)
def test_refresh_login_path_cannot_rewrite_host(tmp_path, bad_path):
    with pytest.raises(ValueError, match=r"auth\.login_path"):
        _contract(
            tmp_path,
            "  mode: refresh\n"
            "  path: /auth/refresh\n"
            "  token_json_path: token\n"
            f"  login_path: '{bad_path}'\n",
        )


def _recording_server():
    """A listening server that records every request path it receives."""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    hits: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def _record(self):
            hits.append(self.path)
            body = json.dumps(
                {"access_token": "tok", "refresh_token": "ref", "expires_in": 3600}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        do_GET = _record
        do_POST = _record

        def log_message(self, *args):
            pass

    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, hits


@pytest.fixture()
def two_servers():
    a, hits_a = _recording_server()
    b, hits_b = _recording_server()
    yield (
        f"http://127.0.0.1:{a.server_address[1]}",
        f"http://127.0.0.1:{b.server_address[1]}",
        hits_a,
        hits_b,
    )
    a.shutdown()
    b.shutdown()


def _probe_result_for(ex: Executor) -> ProbeResult:
    return ex.run_probe(
        {
            "act": {"method": "GET", "path": "/ok"},
            "assert": [{"type": "status", "equals": 200}],
        }
    )


def test_provider_login_url_cannot_leave_target(two_servers):
    target, other, _hits_target, hits_other = two_servers
    other_port = other.rsplit(":", 1)[1]
    ex = Executor(
        target_url=target,
        provisioned=True,
        credential_config={
            "mode": "login",
            "path": f"@127.0.0.1:{other_port}/login",
            "token_json_path": "access_token",
            "body": {"email": "e@example.com", "password": "pw"},
        },
    )
    result = _probe_result_for(ex)
    assert result.verdict == Verdict.INCONCLUSIVE
    assert result.reason == InconclusiveReason.AUTH
    assert any("leaves the target" in d for d in result.details)
    assert hits_other == []  # no request reaches the second server


def test_provider_register_url_cannot_leave_target(two_servers):
    target, other, _hits_target, hits_other = two_servers
    other_port = other.rsplit(":", 1)[1]
    ex = Executor(
        target_url=target,
        provisioned=True,
        credential_config={
            "mode": "register",
            "path": f"@127.0.0.1:{other_port}/register",
            "token_json_path": "access_token",
            "username_field": "username",
            "password_field": "password",
            "method": "POST",
        },
    )
    result = _probe_result_for(ex)
    assert result.verdict == Verdict.INCONCLUSIVE
    assert result.reason == InconclusiveReason.AUTH
    assert any("leaves the target" in d for d in result.details)
    assert hits_other == []


def test_provider_refresh_login_url_cannot_leave_target(two_servers):
    target, other, _hits_target, hits_other = two_servers
    other_port = other.rsplit(":", 1)[1]
    ex = Executor(
        target_url=target,
        provisioned=True,
        credential_config={
            "mode": "refresh",
            "path": "/auth/refresh",
            "token_json_path": "access_token",
            "refresh_token_json_path": "refresh_token",
            "login_path": f"@127.0.0.1:{other_port}/login",
            "login_body": {"email": "e@example.com", "password": "pw"},
        },
    )
    result = _probe_result_for(ex)
    assert result.verdict == Verdict.INCONCLUSIVE
    assert result.reason == InconclusiveReason.AUTH
    assert any("leaves the target" in d for d in result.details)
    assert hits_other == []


# ── Addendum 62: the ghost_jwt key file must stay a real checkout file ──
#
# The contract check was lexical only, and the instance's own seed
# writes the checkout — so a seed could plant a symlink and have the
# orchestrator read a host file as the key. Symlinks (the file or any
# directory component), escapes, and oversized files are key_error.


def test_credential_config_refuses_symlink_key_file(tmp_path):
    host_file = tmp_path.parent / f"{tmp_path.name}-host-secret.txt"
    host_file.write_text("host-secret-key-material", encoding="utf-8")
    key_dir = tmp_path / ".pd-auth"
    key_dir.mkdir()
    (key_dir / "admin_key").symlink_to(host_file)
    contract = _contract(
        tmp_path, "  mode: ghost_jwt\n  key_file: .pd-auth/admin_key\n"
    )
    cfg = _orchestrator(tmp_path)._credential_config(_side(tmp_path, contract))
    assert cfg is not None
    assert "key_value" not in cfg
    assert "symlink" in cfg["key_error"]


def test_credential_config_refuses_symlinked_dir_component(tmp_path):
    host_dir = tmp_path.parent / f"{tmp_path.name}-host-dir"
    host_dir.mkdir()
    (host_dir / "admin_key").write_text("host-secret-key-material", encoding="utf-8")
    (tmp_path / ".pd-auth").symlink_to(host_dir, target_is_directory=True)
    contract = _contract(
        tmp_path, "  mode: ghost_jwt\n  key_file: .pd-auth/admin_key\n"
    )
    cfg = _orchestrator(tmp_path)._credential_config(_side(tmp_path, contract))
    assert cfg is not None
    assert "key_value" not in cfg
    assert "symlink" in cfg["key_error"]


def test_credential_config_refuses_oversized_key_file(tmp_path):
    key_dir = tmp_path / ".pd-auth"
    key_dir.mkdir()
    (key_dir / "admin_key").write_text("k" * 70_000, encoding="utf-8")
    contract = _contract(
        tmp_path, "  mode: ghost_jwt\n  key_file: .pd-auth/admin_key\n"
    )
    cfg = _orchestrator(tmp_path)._credential_config(_side(tmp_path, contract))
    assert cfg is not None
    assert "key_value" not in cfg
    assert "too large" in cfg["key_error"]


# ── Addendum 62: schema 1.5.0 names the union; 1.4.0 records load ──


def test_schema_140_records_still_load(tmp_path):
    record = build_evidence_record(run_kind="bug", bug_id="WO83-SCHEMA-140")
    record["schema_version"] = "1.4.0"
    runs = tmp_path / "runs.jsonl"
    runs.write_text(json.dumps(record) + "\n", encoding="utf-8")
    loaded = read_records(tmp_path)
    assert len(loaded) == 1
    assert loaded[0]["schema_version"] == "1.4.0"
