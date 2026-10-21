"""Lean round 5, WO-R5-K (D267): the linkding contract, the generic ``static_token`` auth mode
(validation), the measured path's refusal of an auth mode it does not drive, the contract's
``proof_path`` and the gate's ``linkding`` app group.

No model, no podman, no network beyond 127.0.0.1 test servers.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from test_lean_env import CONTRACT, FakeSide, proof_server  # noqa: F401

from proofdeploy.executor import Executor
from proofdeploy.lean import pipeline
from proofdeploy.lean import run as lean_run
from proofdeploy.lean.http import UnsupportedAuth, auth_from_contract, presentation_scheme
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


def test_the_lean_path_still_refuses_static_token_until_the_runner_drives_it() -> None:
    """Fail closed: no static_token driver in the lean generator yet; never unauthenticated."""
    with pytest.raises(UnsupportedAuth, match="static_token"):
        auth_from_contract(CONTRACT_FILE)


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
