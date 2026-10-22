"""Lean round 5, WO-R5-K (D267): the linkding contract, the generic ``static_token`` auth mode
(validation), the measured path's refusal of an auth mode it does not drive, the contract's
``proof_path`` and the gate's ``linkding`` app group.

No model, no podman, no network beyond 127.0.0.1 test servers.
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
import yaml
from test_lean_env import CONTRACT, FakeSide, proof_server  # noqa: F401

from proofdeploy.executor import Executor
from proofdeploy.lean import pipeline
from proofdeploy.lean import run as lean_run
from proofdeploy.lean.generate import ScenarioRunner, generate_scenarios
from proofdeploy.lean.http import (
    HttpClient,
    MissingStaticToken,
    Scenario,
    Step,
    UnsupportedAuth,
    auth_from_contract,
    presentation_scheme,
)
from proofdeploy.lean.index import index_from_flask_url_map
from proofdeploy.orchestrator import Orchestrator
from proofdeploy.yml import RepoContract, load_contract

ROOT = Path(__file__).resolve().parent.parent
CONTRACT_FILE = ROOT / "readiness/contracts/sissbruecker__linkding.yml"
SEED_FILE = ROOT / "readiness/seeds/linkding-seed.py"
UNITS_FILE = ROOT / "experiments/lean-poc/linkding-units.json"
LINKDING_HEAD = "27b7303baf41bb28babc610ac8eaa486e1ddfab5"

GATE_PATH = ROOT / "experiments" / "lean-poc" / "gate.py"
_spec = importlib.util.spec_from_file_location("lean_poc_gate_static", GATE_PATH)
assert _spec is not None and _spec.loader is not None
gate = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = gate
_spec.loader.exec_module(gate)


def _auth(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "mode": "static_token",
        "token_env": "APP_API_TOKEN",
        "proof_path": "/api/me/",
        "bootstrap_env": ["APP_ADMIN_NAME", "APP_API_TOKEN"],
    }
    base.update(over)
    return {k: v for k, v in base.items() if v is not None}


def _contract_dict(auth: dict[str, Any]) -> dict[str, Any]:
    return {"build": "true", "start": "true", "readiness": "/health", "auth": auth}


# --------------------------------------------------------------------------
# yml: static_token validation
# --------------------------------------------------------------------------


def test_static_token_loads_and_keeps_its_fields() -> None:
    c = RepoContract.from_dict(_contract_dict(_auth()))
    assert c.auth == {
        "mode": "static_token",
        "bootstrap_env": ["APP_ADMIN_NAME", "APP_API_TOKEN"],
        "token_env": "APP_API_TOKEN",
        "proof_path": "/api/me/",
    }
    # proof_path is optional (the in-code map is the fallback)
    assert "proof_path" not in (
        RepoContract.from_dict(_contract_dict(_auth(proof_path=None))).auth or {}
    )


def test_static_token_without_token_env_is_refused() -> None:
    with pytest.raises(ValueError, match=r"auth\.token_env' must be a non-empty string"):
        RepoContract.from_dict(_contract_dict(_auth(token_env=None)))


def test_static_token_env_outside_bootstrap_env_is_refused() -> None:
    with pytest.raises(ValueError, match="must be listed in 'auth.bootstrap_env'"):
        RepoContract.from_dict(_contract_dict(_auth(token_env="OTHER_TOKEN")))
    with pytest.raises(ValueError, match="must be listed in 'auth.bootstrap_env'"):
        RepoContract.from_dict(_contract_dict(_auth(bootstrap_env=None)))


@pytest.mark.parametrize(
    "bad",
    [
        {"token_env": "not a name"},
        {"proof_path": "//evil.example/x"},
        {"proof_path": "https://evil.example/x"},
        {"proof_path": "relative"},
        {"path": "/api/token/"},  # a login key is not a static_token key
        {"token": "committed-secret"},
    ],
)
def test_static_token_refuses_bad_values_and_foreign_keys(bad: dict[str, Any]) -> None:
    auth = _auth(**bad)
    if "token_env" in bad:
        auth["bootstrap_env"] = [bad["token_env"]]
    with pytest.raises(ValueError):
        RepoContract.from_dict(_contract_dict(auth))


# --------------------------------------------------------------------------
# The committed contract and seed
# --------------------------------------------------------------------------


def test_linkding_contract_loads_with_the_ruled_fields() -> None:
    c = load_contract(CONTRACT_FILE)
    assert c.app_path == "."
    assert c.build == "mkdir -p data/favicons data/previews data/assets"
    assert c.migrate == "python manage.py migrate --noinput"
    assert c.start == "python manage.py runserver 0.0.0.0:$PORT --noreload"
    assert c.readiness == "/health"
    assert c.database == {"kind": "sqlite", "path": "data/db.sqlite3"}
    assert c.env == {"DJANGO_SETTINGS_MODULE": "bookmarks.settings.prod"}
    assert c.auth == {
        "mode": "static_token",
        "token_env": "LD_API_TOKEN",
        "proof_path": "/api/user/profile/",
        "bootstrap_env": ["LD_SUPERUSER_NAME", "LD_API_TOKEN"],
    }
    assert set(c.fixture) == {
        "repo_name",
        "seed_note",
        "auth_mechanism",
        "auth_scope",
        "flags_note",
    }
    assert c.fixture["repo_name"] == "sissbruecker/linkding"
    assert c.fixture["auth_mechanism"] == (
        "the token is presented in the Authorization header in the form 'Token <token>'"
    )
    assert c.fixture["flags_note"] == (
        "application settings are at this version's defaults except the run's deployment "
        "choices (SQLite database, production settings module, no background task worker); "
        "per-user options are at their defaults; no feature flags were changed"
    )
    assert presentation_scheme(c.fixture) == "Token"


def test_linkding_contract_commits_no_credential_value() -> None:
    text = CONTRACT_FILE.read_text(encoding="utf-8")
    data = yaml.safe_load(text)
    # only env NAMES: no value for a bootstrap name anywhere, and no password declared
    assert set(data["env"]) == {"DJANGO_SETTINGS_MODULE"}
    assert "LD_SUPERUSER_PASSWORD" not in data["auth"]["bootstrap_env"]
    assert "LD_API_TOKEN=" not in text and "LD_SUPERUSER_NAME=" not in text


def test_the_contract_seed_is_the_seed_script_verbatim() -> None:
    seed = SEED_FILE.read_text(encoding="utf-8")
    c = load_contract(CONTRACT_FILE)
    assert c.seed == f"python manage.py shell <<'PD_LINKDING_SEED'\n{seed}PD_LINKDING_SEED\n"
    # the seed creates one superuser through the app's own command and one token, nothing else
    code = "\n".join(
        ln for ln in seed.splitlines() if ln.strip() and not ln.lstrip().startswith("#")
    )
    assert code.count("call_command(") == 1 and 'call_command("create_initial_superuser")' in code
    assert code.count(".objects.create(") == 1
    assert 'key = os.environ["LD_API_TOKEN"]' in code and "key=key" in code
    compile(seed, "linkding-seed.py", "exec")


def test_the_seed_bash_heredoc_runs_the_script_text_intact() -> None:
    """The contract's seed command is run by ``bash -c``: the heredoc must hand over the script."""
    c = load_contract(CONTRACT_FILE)
    assert c.seed is not None
    shell = c.seed.replace("python manage.py shell", "cat", 1)
    out = subprocess.run(["bash", "-c", shell], capture_output=True, text=True, check=True)
    assert out.stdout == SEED_FILE.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# The measured path: an auth mode it does not drive is a typed refusal, never unauthenticated
# --------------------------------------------------------------------------


class _Counting(BaseHTTPRequestHandler):
    hits: list[tuple[str, str | None]] = []

    def log_message(self, *_a: Any) -> None:
        return None

    def do_GET(self) -> None:
        type(self).hits.append((self.path, self.headers.get("Authorization")))
        data = b"{}"
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture
def counting_server() -> Iterator[str]:
    _Counting.hits = []
    srv = HTTPServer(("127.0.0.1", 0), _Counting)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_port}"
    finally:
        srv.shutdown()
        srv.server_close()


def _credential_config_of(contract: RepoContract) -> dict[str, Any] | None:
    """What ``Orchestrator._credential_config`` hands the executor for a side with this contract."""
    orch = object.__new__(Orchestrator)
    side = SimpleNamespace(contract=contract, generated_secrets=[], snapshot_dir=Path("."))
    return orch._credential_config(side)  # type: ignore[arg-type]


def test_measured_path_refuses_static_token_with_a_typed_error_and_sends_nothing(
    counting_server: str,
) -> None:
    from proofdeploy.executor import InconclusiveReason, Verdict

    cfg = _credential_config_of(load_contract(CONTRACT_FILE))
    assert cfg is not None and cfg["mode"] == "static_token"
    ex = Executor(target_url=counting_server, provisioned=True, credential_config=cfg)
    # a probe that uses the token AND one that does not: neither may run unauthenticated
    for act in (
        {
            "method": "GET",
            "path": "/api/user/profile/",
            "headers": {"Authorization": "Token ${AUTH_TOKEN}"},
        },
        {"method": "GET", "path": "/api/user/profile/"},
    ):
        r = ex.run_probe({"act": act, "assert": [{"type": "status", "equals": 200}]})
        assert r.verdict == Verdict.INCONCLUSIVE
        assert r.reason == InconclusiveReason.AUTH
        assert any("unknown mode 'static_token'" in d for d in r.details), r.details
    assert _Counting.hits == [], "a request reached the app although the auth mode is not driven"


def test_measured_path_setup_step_is_refused_too(counting_server: str) -> None:
    from proofdeploy.executor import InconclusiveReason, Verdict

    cfg = _credential_config_of(load_contract(CONTRACT_FILE))
    ex = Executor(target_url=counting_server, provisioned=True, credential_config=cfg)
    probe = {
        "setup": [{"type": "http", "act": {"method": "GET", "path": "/setup"}}],
        "act": {"method": "GET", "path": "/api/user/profile/"},
        "assert": [{"type": "status", "equals": 200}],
    }
    r = ex.run_probe(probe)
    assert r.verdict == Verdict.INCONCLUSIVE and r.reason == InconclusiveReason.AUTH
    assert _Counting.hits == []


# --------------------------------------------------------------------------
# The sign-in proof path comes from the contract first
# --------------------------------------------------------------------------


def test_auth_proof_path_reads_the_contract_first_then_the_map() -> None:
    cfg = pipeline.LeanConfig(repo=Path("."), base="a", head="b", unit_repo="sissbruecker/linkding")
    contract = load_contract(CONTRACT_FILE)
    assert pipeline.auth_proof_path(cfg, contract) == "/api/user/profile/"
    # the contract wins over the in-code map
    paperless = SimpleNamespace(auth={"mode": "login", "proof_path": "/from/contract/"}, fixture={})
    pcfg = pipeline.LeanConfig(
        repo=Path("."), base="a", head="b", unit_repo="paperless-ngx/paperless-ngx"
    )
    assert pipeline.auth_proof_path(pcfg, paperless) == "/from/contract/"
    # no declaration: the map is the fallback, an unknown unit is still a typed refusal
    plain = SimpleNamespace(auth={"mode": "login"}, fixture={})
    assert pipeline.auth_proof_path(pcfg, plain) == "/api/profile/"
    with pytest.raises(pipeline.LeanRefusal, match="no registered proof path"):
        pipeline.auth_proof_path(cfg, plain)


# --------------------------------------------------------------------------
# The gate's linkding group
# --------------------------------------------------------------------------


def _group_json(**over: Any) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(UNITS_FILE.read_text(encoding="utf-8"))
    data.update(over)
    return data


def _write_group(tmp: Path, data: dict[str, Any]) -> Path:
    p = tmp / "linkding-units.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def test_the_linkding_units_file_is_registered_empty_with_the_head_check_sha() -> None:
    data = json.loads(UNITS_FILE.read_text(encoding="utf-8"))
    assert data == {
        "bug_units": [],
        "check_sha": LINKDING_HEAD,
        "clean_units": [],
        "contract": "sissbruecker__linkding",
        "openapi_path": None,
        "repo": "sissbruecker/linkding",
    }
    group = gate.load_app_group("linkding")
    assert (group.repo, group.contract, group.openapi_path) == (
        "sissbruecker/linkding",
        "sissbruecker__linkding",
        None,
    )
    assert gate.load_units("linkding") == []  # no unit is selected yet
    assert "linkding" in gate.CLI_GROUPS and "linkding" not in gate.GROUPS


def test_rehearsal_of_the_group_is_one_check_unit_at_head_on_both_sides() -> None:
    units = gate.rehearsal_units("linkding")
    assert [u.id for u in units] == ["linkding-check"]
    u = units[0]
    assert (u.kind, u.group, u.mode, u.app) == ("check", "linkding", "sandbox", "linkding")
    assert u.base == u.head == LINKDING_HEAD and u.external and not u.paperless


def test_the_registered_gate_is_unchanged_by_the_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bugs = tmp_path / "paperless-bug-units.json"
    unit = {
        "id": "paperless-bug-11112222",
        "note": "n",
        "unit_repo": "paperless-ngx/paperless-ngx",
        "b_parent": "1" * 40,
        "b": "2" * 40,
        "fix_parent": "3" * 40,
        "fix": "11112222" + "0" * 32,
        "selection_record": "r",
        "selection_record_sha256": "4" * 64,
    }
    bugs.write_text(json.dumps({"units": [unit]}), encoding="utf-8")
    monkeypatch.setattr(gate, "PAPERLESS_BUG_UNITS_JSON", bugs)
    every = gate.select_units(gate.load_units("all"), "all")
    assert not any(u.group == "linkding" or u.app for u in every)
    assert all(u.paperless for u in every if u.group == "paperless")
    # a populated linkding file never leaks into "all" either
    path = _write_group(
        tmp_path,
        _group_json(clean_units=[{"sha": "5" * 40, "first_parent": "6" * 40}]),
    )
    monkeypatch.setitem(gate.APP_UNITS_FILES, "linkding", path)
    assert [u.id for u in gate.select_units(gate.load_units("linkding"), "linkding")] == [
        "linkding-clean-" + "5" * 12
    ]
    assert not any(u.app for u in gate.select_units(gate.load_units("all"), "all"))
    # and the selector itself keeps a group's units out of "all" even when handed them together
    golden = gate.select_units(gate.load_units("golden"), "golden")
    mixed = [*gate.load_units("linkding"), *golden]
    assert [u.id for u in gate.select_units(mixed, "all")] == ["golden-bug", "golden-clean"]
    assert [u.id for u in gate.select_units(mixed, "linkding")] == ["linkding-clean-" + "5" * 12]


def _linkding_bug(fix: str = "ab" * 20) -> dict[str, Any]:
    return {
        "id": f"linkding-bug-{fix[:8]}",
        "note": "n",
        "unit_repo": "sissbruecker/linkding",
        "b_parent": "1" * 40,
        "b": "2" * 40,
        "fix_parent": "3" * 40,
        "fix": fix,
        "selection_record": "r",
        "selection_record_sha256": "4" * 64,
    }


def test_group_units_are_built_from_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bug = _linkding_bug()
    path = _write_group(
        tmp_path,
        _group_json(
            bug_units=[bug],
            clean_units=[{"sha": "5" * 40, "first_parent": "6" * 40, "extra": 1}],
        ),
    )
    monkeypatch.setitem(gate.APP_UNITS_FILES, "linkding", path)
    units = gate.load_units("linkding")
    assert [(u.id, u.kind, u.base, u.head) for u in units] == [
        (bug["id"], "bug", "1" * 40, "2" * 40),
        ("linkding-clean-" + "5" * 12, "clean", "6" * 40, "5" * 40),
    ]
    assert units[0].fix_parent == "3" * 40 and units[0].fix == bug["fix"]
    assert all(u.app == "linkding" and u.group == "linkding" and u.external for u in units)
    # with units selected the rehearsal rehearses them, not the check unit
    assert [u.id for u in gate.rehearsal_units("linkding")] == [u.id for u in units]


@pytest.mark.parametrize(
    ("over", "why"),
    [
        ({"repo": "not-a-slug"}, "repo is not owner/name"),
        ({"contract": "other__repo"}, "contract is not the repo's contract-store stem"),
        ({"openapi_path": "api/schema"}, "openapi_path"),
        ({"check_sha": "27b7303b"}, "check_sha"),
        ({"bug_units": [{"id": "x"}]}, "bug unit 0 keys differ"),
        ({"bug_units": [{**_linkding_bug(), "unit_repo": "a/b"}]}, "unit_repo is not"),
        ({"bug_units": [{**_linkding_bug(), "id": "paperless-bug-abababab"}]}, "id is not"),
        ({"bug_units": [_linkding_bug(), _linkding_bug()]}, "is repeated"),
        ({"clean_units": [{"sha": "5" * 40}]}, "lacks sha and first_parent"),
        ({"clean_units": [{"sha": "zz", "first_parent": "6" * 40}]}, "not a 40-character"),
        ({"surprise": 1}, "keys differ"),
    ],
)
def test_an_invalid_group_file_stops_the_gate_before_it_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, over: dict[str, Any], why: str
) -> None:
    monkeypatch.setitem(
        gate.APP_UNITS_FILES, "linkding", _write_group(tmp_path, _group_json(**over))
    )
    with pytest.raises(gate.GateError, match=why):
        gate.load_units("linkding")
    assert gate.main(["--units", "linkding", "--list"]) == lean_run.EXIT_USAGE


def test_the_group_cli_lists_and_needs_its_own_repo_flag(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    assert gate.main(["--units", "linkding", "--rehearse", "--list"]) == 0
    assert capsys.readouterr().out.split() == ["linkding-check"]
    assert gate.main(["--units", "linkding", "--list"]) == 0
    assert capsys.readouterr().out.split() == []
    out, work = tmp_path / "out", tmp_path / "work"
    code = gate.main(
        ["--units", "linkding", "--rehearse", "--out", str(out), "--workdir", str(work)]
    )
    assert code == 1  # no clone given: an error row, never a silent pass
    row = json.loads((out / "rehearsal.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert row["error"] == "linkding repo not given (--linkding-repo)" and row["ok"] is False


def _app_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "linkding-clone"
    repo.mkdir()
    env = {"GIT_CONFIG_GLOBAL": "/dev/null", "PATH": "/usr/bin:/bin"}
    for args in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "t@example.invalid"],
        ["config", "user.name", "t"],
        ["config", "commit.gpgsign", "false"],
    ):
        subprocess.run(["git", "-C", str(repo), *args], check=True, env=env)
    (repo / "manage.py").write_text("# app\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, env=env)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "c1"], check=True, env=env)
    sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
        env=env,
    ).stdout.strip()
    return repo, sha


def test_the_group_rehearses_with_stub_drivers_at_head_on_both_sides(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    proof_server: str,  # noqa: F811
) -> None:
    repo, sha = _app_repo(tmp_path)
    monkeypatch.setitem(
        gate.APP_UNITS_FILES, "linkding", _write_group(tmp_path, _group_json(check_sha=sha))
    )
    seen: dict[str, Any] = {}
    sides = {"base": FakeSide("base", proof_server), "head": FakeSide("head", proof_server)}

    def provision(rc: Any, contract_file: Any, work: Path, timings: Any) -> Any:
        seen.update(
            repo=rc.repo,
            unit_repo=rc.unit_repo,
            base=rc.base_sha,
            head=rc.head_sha,
            contract_file=contract_file,
            no_sandbox=rc.no_sandbox,
            openapi=rc.openapi_path,
            overlay=rc.django_overlay,
        )
        return sides["base"], sides["head"], stub_contract

    # the stub server's note endpoint stands in for the app's proof path; the contract names it
    stub_contract = SimpleNamespace(
        **{**CONTRACT.__dict__, "auth": {**CONTRACT.auth, "proof_path": "/notes/1"}}
    )
    cfg = gate.GateConfig(
        out=tmp_path / "out",
        workdir=tmp_path / "work",
        which="linkding",
        app_repos={"linkding": repo},
        provision=provision,
    )
    rows = gate.run_rehearsal(cfg)
    assert [(r["unit"], r["side"], r["sha"]) for r in rows] == [
        ("linkding-check", "base", sha),
        ("linkding-check", "head", sha),
    ]
    assert seen["repo"] == repo and seen["unit_repo"] == "sissbruecker/linkding"
    assert seen["base"] == seen["head"] == sha and seen["no_sandbox"] is False
    assert seen["openapi"] is None and seen["overlay"] == {}
    assert Path(seen["contract_file"]).name == "sissbruecker__linkding.yml"
    assert all(r["measured_result"] is False for r in rows)
    # the stub contract is the login-mode test double: the group plumbing is what is proved here,
    # and the proof path came from the contract (the unit has no entry in the in-code map)
    assert all(r["request"] == {"path": "/notes/1", "status": 200} for r in rows)
    assert all(r["ok"] is True for r in rows), rows
    assert not (tmp_path / "out" / "units" / "linkding-check" / "work").exists()


def test_the_gate_run_of_a_group_unit_reads_its_clone_and_keeps_the_console_out_of_the_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _write_group(
        tmp_path, _group_json(clean_units=[{"sha": "5" * 40, "first_parent": "6" * 40}])
    )
    monkeypatch.setitem(gate.APP_UNITS_FILES, "linkding", path)
    cfg = gate.GateConfig(out=tmp_path / "o", workdir=tmp_path / "w", which="linkding")
    summary = gate.run_gate(cfg)
    assert summary["units_run"] == 1
    row = json.loads((tmp_path / "o" / "gate-results.jsonl").read_text().splitlines()[0])
    assert row["error"] == "linkding repo not given (--linkding-repo)"
    # an app unit is logged like a paperless unit: the error shows its class only
    assert gate.log_line(row, True).startswith(f"[{row['unit']}] ERROR linkding repo not given")
    rc = gate._unit_rc(
        gate.GateConfig(
            out=tmp_path / "o", workdir=tmp_path / "w", app_repos={"linkding": tmp_path}
        ),
        gate.load_units("linkding")[0],
        gate._Repos(),
        tmp_path / "u",
        "sandbox",
    )
    assert (rc.repo, rc.unit_repo, rc.openapi_path, rc.no_sandbox) == (
        tmp_path,
        "sissbruecker/linkding",
        None,
        False,
    )
    assert lean_run._contract_file(rc) == CONTRACT_FILE


# --------------------------------------------------------------------------
# A. The lean runner drives static_token: no login request, the header on every authenticated step
# --------------------------------------------------------------------------

TOKEN = "t0k3n" + "ab12" * 7  # a 33-character generated-looking value
SUPERUSER = "su" + "cd34" * 7
ENV = {"LD_API_TOKEN": TOKEN, "LD_SUPERUSER_NAME": SUPERUSER}


class _App(BaseHTTPRequestHandler):
    """Requires 'Token <TOKEN>' on /api/*; echoes what it was sent (to test redaction)."""

    hits: list[tuple[str, str, str | None]] = []

    def log_message(self, *_a: Any) -> None:
        return None

    def _reply(self) -> None:
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n).decode() if n else ""
        auth = self.headers.get("Authorization")
        type(self).hits.append((self.command, self.path, auth))
        body: dict[str, Any]
        if self.path.startswith("/deny"):
            status, body = 401, {"detail": "no"}
        elif self.path.startswith("/api/") and auth != f"Token {TOKEN}":
            status, body = 401, {"detail": "Invalid token."}
        else:
            status = 200
            body = {"path": self.path, "auth": auth, "body": raw, "echo": TOKEN, "user": SUPERUSER}
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_POST = _reply


@pytest.fixture
def app_url() -> Iterator[str]:
    _App.hits = []
    srv = HTTPServer(("127.0.0.1", 0), _App)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_port}"
    finally:
        srv.shutdown()
        srv.server_close()


def _static_auth() -> Any:
    auth = auth_from_contract(CONTRACT_FILE)
    assert auth is not None
    return auth


def test_auth_from_contract_returns_a_static_spec_with_no_login_step() -> None:
    auth = _static_auth()
    assert auth.is_static and auth.static_token_env == "LD_API_TOKEN"
    assert auth.scheme == "Token" and auth.header("x") == "Token x"
    assert auth.login_steps() == []
    # the other modes keep their login step
    login = auth_from_contract(ROOT / "readiness/contracts/golden__python-flask-sqlite.yml")
    assert login is not None and not login.is_static and len(login.login_steps()) == 1
    with pytest.raises(UnsupportedAuth):
        auth_from_contract(SimpleNamespace(auth={"mode": "refresh"}, fixture={}))


def test_generated_scenarios_carry_no_login_step_for_a_static_token() -> None:
    index = index_from_flask_url_map(
        [{"rule": "/notes/<int:note_id>", "methods": ["GET"], "endpoint": "get_note"}],
        app="a",
        commit_sha="c",
    )
    auth = _static_auth()
    scs = generate_scenarios(index, auth=auth)
    assert scs and all(st.path != auth.login.path for sc in scs for st in sc.steps)
    assert any(sc.steps[-1].auth is True for sc in scs)
    assert all(sc.steps[0].auth is True or not sc.steps[-1].auth for sc in scs)


def test_no_login_request_is_ever_sent_and_the_header_is_on_every_authenticated_step(
    app_url: str,
) -> None:
    auth = _static_auth()
    runner = ScenarioRunner(HttpClient(app_url), auth=auth, env=ENV, repair=False)
    profile = Step("GET", "/api/user/profile/", auth=True)
    create = Step("POST", "/api/bookmarks/", body={"url": "https://e.example/"}, auth=True)
    plain = Step("GET", "/health", auth=False)
    # even a scenario that (wrongly) still lists the placeholder login never sends it
    scenarios = [
        Scenario([profile], "GET /api/user/profile/"),
        Scenario([auth.login, create, profile], "POST /api/bookmarks/"),
        Scenario([plain], "GET /health"),
    ]
    results = [runner(sc) for sc in scenarios]
    assert runner.logins == 0
    assert [(m, p) for m, p, _ in _App.hits] == [
        ("GET", "/api/user/profile/"),
        ("POST", "/api/bookmarks/"),
        ("GET", "/api/user/profile/"),
        ("GET", "/health"),
    ]
    assert all(a == f"Token {TOKEN}" for _m, p, a in _App.hits if p.startswith("/api/"))
    assert _App.hits[-1][2] is None  # a step that is not authenticated carries no header
    assert all(e.status == 200 for r in results for e in r.exchanges)
    assert [e.auth_sent for r in results for e in r.exchanges] == [True, True, True, False]
    assert runner.planned_requests(scenarios[1]) == 2


def test_a_scenario_that_targets_the_placeholder_login_still_sends_no_login(app_url: str) -> None:
    auth = _static_auth()
    runner = ScenarioRunner(HttpClient(app_url), auth=auth, env=ENV, repair=False)
    target = f"{auth.login.method} {auth.login.path}"
    res = runner(Scenario([auth.login], target))
    assert res.exchanges == [] and runner.logins == 0 and _App.hits == []


def test_the_generated_value_is_redacted_from_every_recorded_exchange(app_url: str) -> None:
    runner = ScenarioRunner(HttpClient(app_url), auth=_static_auth(), env=ENV, repair=False)
    leaky = Step("GET", "/echo", query={"k": TOKEN, "who": SUPERUSER}, auth=True)
    res = runner(Scenario([leaky, Step("GET", "/api/user/profile/", auth=True)], "GET /echo"))
    recorded = json.dumps([e.to_dict() for e in res.exchanges])
    assert TOKEN not in recorded and SUPERUSER not in recorded
    assert "<redacted:bootstrap>" in recorded
    # the Authorization header value is never part of an exchange either
    assert "Authorization" not in recorded and f"Token {TOKEN}" not in recorded
    assert res.scenario.id  # the scenario content id is derived from steps, not from the value


def test_a_401_is_data_and_never_clears_a_static_token(app_url: str) -> None:
    runner = ScenarioRunner(HttpClient(app_url), auth=_static_auth(), env=ENV, repair=False)
    denied = runner(Scenario([Step("GET", "/deny", auth=True)], "GET /deny"))
    assert denied.exchanges[0].status == 401 and denied.exchanges[0].auth_sent
    after = runner(
        Scenario([Step("GET", "/api/user/profile/", auth=True)], "GET /api/user/profile/")
    )
    assert after.exchanges[0].status == 200 and after.exchanges[0].auth_sent
    assert runner.logins == 0 and [p for _m, p, _a in _App.hits] == ["/deny", "/api/user/profile/"]


@pytest.mark.parametrize("env", [{}, {"LD_API_TOKEN": ""}, {"LD_SUPERUSER_NAME": SUPERUSER}])
def test_a_missing_generated_value_sends_nothing(app_url: str, env: dict[str, str]) -> None:
    with pytest.raises(MissingStaticToken):
        ScenarioRunner(HttpClient(app_url), auth=_static_auth(), env=env)
    assert _App.hits == []


class _Driver:
    """A SideDriver double: one app on the test server; counts starts."""

    root_prefix = ""
    label = "head"
    sha = "0" * 40

    def __init__(self, url: str, env: dict[str, str]) -> None:
        self.url, self.env, self.starts = url, env, 0

    def bootstrap_env(self) -> dict[str, str]:
        return dict(self.env)

    def start(self, *, measured: bool) -> Any:
        self.starts += 1
        return SimpleNamespace(base_url=self.url, session=None, stop=lambda: None)


def test_prove_auth_with_a_static_token_sends_one_authenticated_request(app_url: str) -> None:
    drv = _Driver(app_url, ENV)
    proof = pipeline.prove_auth(cast(Any, drv), _static_auth(), "/api/user/profile/", side="head")
    assert proof == {
        "side": "head",
        "ok": True,
        "outcome": "2xx",
        "proof_path": "/api/user/profile/",
        "login_status": None,
        "proof_status": 200,
        "proof_auth_sent": True,
    }
    assert _App.hits == [("GET", "/api/user/profile/", f"Token {TOKEN}")]


def test_prove_auth_without_the_generated_value_is_unproven_and_starts_nothing(
    app_url: str,
) -> None:
    drv = _Driver(app_url, {"LD_SUPERUSER_NAME": SUPERUSER})
    proof = pipeline.prove_auth(cast(Any, drv), _static_auth(), "/api/user/profile/", side="base")
    assert proof["ok"] is False and proof["outcome"] == "unproven"
    assert proof["reason"] == "static_token_missing" and proof["proof_auth_sent"] is False
    assert drv.starts == 0 and _App.hits == []


def test_prove_auth_with_a_rejected_token_is_unproven(app_url: str) -> None:
    drv = _Driver(app_url, {"LD_API_TOKEN": "wrong-value", "LD_SUPERUSER_NAME": SUPERUSER})
    proof = pipeline.prove_auth(cast(Any, drv), _static_auth(), "/api/user/profile/", side="head")
    assert proof["ok"] is False and proof["proof_status"] == 401 and proof["proof_auth_sent"]


def test_the_group_rehearses_the_real_contract_with_a_static_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, app_url: str
) -> None:
    repo, sha = _app_repo(tmp_path)
    monkeypatch.setitem(
        gate.APP_UNITS_FILES, "linkding", _write_group(tmp_path, _group_json(check_sha=sha))
    )
    contract = load_contract(CONTRACT_FILE)
    sides = [_Driver(app_url, ENV), _Driver(app_url, ENV)]
    cfg = gate.GateConfig(
        out=tmp_path / "out",
        workdir=tmp_path / "work",
        which="linkding",
        app_repos={"linkding": repo},
        provision=lambda *_a: (sides[0], sides[1], contract),
    )
    rows = gate.run_rehearsal(cfg)
    assert [r["ok"] for r in rows] == [True, True], rows
    assert all(
        r["auth_proof"]
        == {
            "outcome": "2xx",
            "login_status": None,
            "proof_status": 200,
            "unauthenticated_status": 401,  # the fake app refuses /api/ without the token
        }
        for r in rows
    )
    assert all(r["request"] == {"path": "/api/user/profile/", "status": 200} for r in rows)
    assert all(m == "GET" and p == "/api/user/profile/" for m, p, _a in _App.hits)
    # per side: the sign-in proof and the third start's request carry the token, then one bare
    # request (no Authorization header) after them: 6 hits, 4 authenticated and 2 bare
    assert len(_App.hits) == 6
    assert [a for _m, _p, a in _App.hits] == [f"Token {TOKEN}", f"Token {TOKEN}", None] * 2
    assert [d.starts for d in sides] == [3, 3]
    assert TOKEN not in (tmp_path / "out" / "rehearsal.jsonl").read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# B. The workflows offer the linkding group
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "options"),
    [
        ("lean-poc.yml", ["all", "golden", "mutants", "paperless", "linkding"]),
        ("lean-rehearse.yml", ["all", "golden", "paperless", "linkding"]),
    ],
)
def test_workflows_offer_linkding_and_clone_it_only_for_that_choice(
    name: str, options: list[str]
) -> None:
    text = (ROOT / ".github/workflows" / name).read_text(encoding="utf-8")
    data = yaml.safe_load(text)
    on = data.get("on", data.get(True))
    assert on["workflow_dispatch"]["inputs"]["units"]["options"] == options
    job = next(iter(data["jobs"].values()))
    clones = [st for st in job["steps"] if "linkding" in str(st.get("run", ""))]
    clone = next(st for st in job["steps"] if st.get("name") == "Check out linkding (full history)")
    assert clone["if"] == "${{ inputs.units == 'linkding' }}"
    assert (
        "git clone --quiet https://github.com/sissbruecker/linkding /tmp/linkding" in clone["run"]
    )
    assert clone in clones
    gate_step = next(st for st in job["steps"] if "gate.py" in str(st.get("run", "")))
    assert "--linkding-repo /tmp/linkding" in gate_step["run"]
    assert "--paperless-repo /tmp/paperless" in gate_step["run"]
    # the paperless clone keeps its own condition
    pl = next(
        st for st in job["steps"] if st.get("name") == "Check out paperless-ngx (full history)"
    )
    assert pl["if"] == "${{ inputs.units == 'all' || inputs.units == 'paperless' }}"
    # the inputs still reach the shell through env, never inline
    assert "${{ inputs" not in gate_step["run"]


# --------------------------------------------------------------------------
# C. The app block of a linkding run
# --------------------------------------------------------------------------


def _row(unit: str, kind: str, group: str, verdict: str, **kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "unit": unit,
        "kind": kind,
        "group": group,
        "verdict": verdict,
        "cause": kw.pop("cause", None),
        "confirmed": None,
        "executable_changed_lines": 12,
        "lines_reached": 9,
        "requests_hitting_changed_code": 14,
        "differences": 3,
        "differences_judged": 2,
        "rate_2xx": 0.75,
        "impacted_2xx": {"GET /api/bookmarks/": {"requests": 14, "2xx": 10}},
        "tokens": 0,
        "model_errors": [],
        "error": None,
        "observable": True,
        "unexercised": 0,
        "wall_seconds": 1.0,
    }
    base.update(kw)
    return base


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return gate.compute_summary(rows, expected=[r["unit"] for r in rows])  # type: ignore[no-any-return]


def test_the_app_block_reports_bug_units_clean_units_false_alarms_and_cost() -> None:
    rows = [
        _row(
            "linkding-bug-aaaaaaaa",
            "bug",
            "linkding",
            gate.FAIL,
            cause="judged_regression",
            confirmed=True,
            tokens=4100,
            mapped_by={"head": "route_table", "base": None},
            request_acceptance={"head": 0.9, "base": 0.8},
        ),
        _row("linkding-bug-bbbbbbbb", "bug", "linkding", gate.PASS, tokens=900),
        _row("linkding-clean-111111111111", "clean", "linkding", gate.PASS, tokens=300),
        _row(
            "linkding-clean-222222222222",
            "clean",
            "linkding",
            gate.FAIL,
            cause="hard_oracle",
            tokens=7200,
        ),
        _row(
            "linkding-clean-333333333333",
            "clean",
            "linkding",
            gate.INCONCLUSIVE,
            cause="environment",
            model_errors=["rate_limit"],
            tokens=10,
        ),
    ]
    block = _summary(rows)["app_block"]["linkding"]
    bug = block["bug_units"]["linkding-bug-aaaaaaaa"]
    assert bug == {
        "verdict": gate.FAIL,
        "cause": "judged_regression",
        "confirmed": True,
        "executable_changed_lines": 12,
        "lines_reached": 9,
        "requests_hitting_changed_code": 14,
        "differences_found": 3,
        "differences_judged": 2,
        "mapped_by": {"head": "route_table", "base": None},
        "request_acceptance": {"head": 0.9, "base": 0.8},
        "impacted_2xx_rate": 0.75,
        "impacted_2xx": {"GET /api/bookmarks/": {"requests": 14, "2xx": 10}},
    }
    absent = block["bug_units"]["linkding-bug-bbbbbbbb"]  # a row that carries neither key
    assert absent["mapped_by"] is None and absent["request_acceptance"] is None
    assert block["clean_units"] == {
        "linkding-clean-111111111111": {"verdict": gate.PASS, "cause": None},
        "linkding-clean-222222222222": {"verdict": gate.FAIL, "cause": "hard_oracle"},
        "linkding-clean-333333333333": {"verdict": gate.INCONCLUSIVE, "cause": "environment"},
    }
    assert block["false_alarms"] == ["linkding-clean-222222222222"]
    assert block["false_alarm_count"] == 1 and block["clean_units_run"] == 3
    assert block["max_tokens"] == 7200
    assert block["provider_failures"] == {"linkding-clean-333333333333": ["rate_limit"]}


def test_no_app_block_for_the_registered_groups_and_their_criteria_text_is_untouched() -> None:
    rows = [
        _row("golden-bug", "bug", "golden", gate.FAIL),
        _row("golden-clean", "clean", "golden", gate.PASS),
    ]
    summary = _summary(rows)
    assert "app_block" not in summary
    assert summary["criteria_text"] == gate.CRITERIA
    assert summary["criteria"]["1"]["status"] == "PASS"
    mixed = _summary([*rows, _row("linkding-clean-1", "clean", "linkding", gate.PASS)])
    assert list(mixed["app_block"]) == ["linkding"]
    assert mixed["criteria"]["1"] == summary["criteria"]["1"]
    assert mixed["criteria_text"] == gate.CRITERIA


def test_the_row_copies_mapped_by_and_request_acceptance_from_the_manifest() -> None:
    from unittest import mock

    unit = gate.Unit("linkding-bug-aaaaaaaa", "bug", "linkding", "sandbox", "b", "h", None)
    manifest: dict[str, Any] = {
        "mode": "sandbox",
        "compared": {"differences": 1},
        "impacted_operations_head": {"GET /a": {"requests": 4, "2xx": 3}},
        "mapped_by": {"head": "route_table", "base": None},
        "request_acceptance": {"head": 0.9, "base": None},
        "mapping_attempts": 2,
    }
    record = mock.Mock(total_tokens=5, calls=[])
    from proofdeploy.lean.pipeline import LeanResult
    from proofdeploy.lean.verdict import Verdict

    result = LeanResult(Verdict(gate.FAIL, "judged_regression", {}), record, manifest)
    row = gate.row_from_result(unit, result, 1, wall_seconds=1.0)
    assert row["mapped_by"] == {"head": "route_table", "base": None}
    assert row["request_acceptance"] == {"head": 0.9, "base": None}
    assert row["rate_2xx"] == 0.75
    # absent keys and error rows give None
    del manifest["mapped_by"], manifest["request_acceptance"]
    bare = gate.row_from_result(unit, result, 1, wall_seconds=1.0)
    assert bare["mapped_by"] is None and bare["request_acceptance"] is None
    err = gate.error_row(unit, "boom", wall_seconds=0.0)
    assert err["mapped_by"] is None and err["request_acceptance"] is None
    # and the app block reports them from the row, rate_2xx separately
    row["group"], row["kind"] = "linkding", "bug"
    bug = _summary([row])["app_block"]["linkding"]["bug_units"][unit.id]
    assert bug["mapped_by"] == {"head": "route_table", "base": None}
    assert bug["request_acceptance"] == {"head": 0.9, "base": None}
    assert bug["impacted_2xx_rate"] == 0.75


def test_mypy_does_not_check_the_seed_scripts() -> None:
    """Seeds run in the measured app's environment; CI's `mypy ... readiness` must skip them."""
    import tomllib

    cfg = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    exclude = re.compile(cfg["tool"]["mypy"]["exclude"])
    assert exclude.search("readiness/seeds/linkding-seed.py")
    assert exclude.search("readiness/seeds/anything.py")
    assert exclude.search("conformance/golden/x/app.py")  # the earlier exclusion is kept
    assert not exclude.search("readiness/provision.py")
    assert not exclude.search("proofdeploy/lean/pipeline.py")
    assert not exclude.search("tests/readiness/seeds/x.py")  # anchored to the readiness tree
