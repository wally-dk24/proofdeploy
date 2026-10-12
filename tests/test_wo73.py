"""WO-7.3 (Addendum 40): writes granted by provenance; one network
mode per measured run.

The defect (pre-existing on main, Addendum 36 item 5): the executor's
remote-method gate decided write permission by HOSTNAME
(``_LOCAL_HOSTS``), so a sandbox app reached at its container IP
(isolated network mode) counted as "remote" and every register /
login / write probe was refused — the side went INCONCLUSIVE. And
each side's Sandbox resolved its network mode independently, so one
run could mix modes (fix^ on 127.0.0.1, fix at a container IP).

The WO-7.3 rules proven here:

- Writes are allowed iff ``Executor.provisioned`` — the grant is
  provenance (the orchestrator provisioned the target), whatever
  the host. Everything else stays GET/HEAD only. No CLI flag,
  contract field or env var sets the grant.
- One network mode per measured run: resolved ONCE before the first
  side, passed to every side, recorded per side; a mixed-mode run
  is refused with a typed environment error and NO score record.
- The evidence records ``write_grant: {provisioned, target_url,
  network_mode}`` per side (schema 1.3.0, non-scoring).

Fail-before: on the pre-WO-7.3 code the provisioned container-IP
POST is refused by the hostname gate, MeasureConfig has no
network_mode, records carry no write_grants, the schema is 1.2.0,
and mixed-mode runs are scored instead of refused.
"""
import json
import subprocess
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from proofdeploy import executor as executor_mod
from proofdeploy.executor import Executor, InconclusiveReason, Verdict

# ---------------------------------------------------------------------------
# A real HTTP server at a NON-localhost loopback address.
#
# 127.0.0.2 is loopback (127.0.0.0/8), so requests genuinely reach
# the server — but its hostname is NOT in the executor's
# _LOCAL_HOSTS ({"localhost", "127.0.0.1", "::1"}), so the old
# hostname gate treats it exactly like a container IP: "remote".
# ---------------------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    hits: dict = {"GET": 0, "POST": 0}

    def do_GET(self):
        type(self).hits["GET"] += 1
        if self.path == "/health":
            body, code = {"ok": True}, 200
        elif self.path == "/value":
            body, code = {"v": 1}, 200
        elif self.path == "/ok":
            body, code = {"status": "ok"}, 200
        else:
            body, code = {"error": "not found"}, 404
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        type(self).hits["POST"] += 1
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length:
            self.rfile.read(length)
        raw = json.dumps({"created": True}).encode()
        self.send_response(201)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):
        pass


@pytest.fixture()
def app_server():
    """HTTP server bound to all interfaces, addressed via 127.0.0.2
    (a "container-IP-style" host) — and via 127.0.0.1 for the fake
    sandbox apps in the orchestrator tests."""
    _Handler.hits = {"GET": 0, "POST": 0}
    srv = HTTPServer(("0.0.0.0", 0), _Handler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield {
        "container_ip_url": f"http://127.0.0.2:{port}",
        "loopback_url": f"http://127.0.0.1:{port}",
        "hits": _Handler.hits,
    }
    srv.shutdown()
    srv.server_close()


@pytest.fixture(autouse=True)
def _proxy_free_opener(monkeypatch):
    """The executor's opener inherits any ambient proxy env (this
    dev sandbox intercepts HTTP); CI and normal hosts have none.
    Give the executor the proxy-free opener a normal host has, so
    the tests judge the GATE, not the sandbox's egress proxy."""
    opener = urllib.request.build_opener(
        executor_mod._NoRedirect, urllib.request.ProxyHandler({})
    )
    monkeypatch.setattr(executor_mod, "_OPENER", opener)


# ---------------------------------------------------------------------------
# The write gate: provenance, not hostname.
# ---------------------------------------------------------------------------


def test_provisioned_container_ip_setup_post_allowed_and_sent(app_server):
    """Container-IP target + provisioned=True: the setup POST is
    allowed AND actually sent (the server saw it), and an act POST
    is sent too.

    Planted bug this catches: the hostname check restored — the
    gate deciding by _is_local_url again — refuses both POSTs and
    the probe goes INCONCLUSIVE.
    """
    ex = Executor(target_url=app_server["container_ip_url"], provisioned=True)
    p = {
        "setup": [
            {"type": "http", "act": {"method": "POST", "path": "/ok", "body": "s"}}
        ],
        "act": {"method": "POST", "path": "/ok", "body": "hi"},
        "assert": [{"type": "status", "equals": 201}],
    }
    r = ex.run_probe(p)
    assert r.verdict == Verdict.PASS
    assert app_server["hits"]["POST"] == 2  # setup POST + act POST, both sent
    assert len(r.writes) == 2
    assert r.request is not None
    assert r.request["method"] == "POST"


def test_same_url_not_provisioned_refused(app_server):
    """The SAME container-IP URL with provisioned=False: the setup
    POST is refused before any HTTP — nothing reaches the server.

    Planted bug this catches: the grant set for a non-provisioned
    target (e.g. provisioned defaulting True, or the gate removed).
    """
    ex = Executor(target_url=app_server["container_ip_url"])  # not provisioned
    p = {
        "setup": [
            {"type": "http", "act": {"method": "POST", "path": "/ok", "body": "s"}}
        ],
        "act": {"method": "GET", "path": "/ok"},
        "assert": [{"type": "status", "equals": 200}],
    }
    r = ex.run_probe(p)
    assert r.verdict == Verdict.INCONCLUSIVE
    assert r.reason == InconclusiveReason.PROBE
    assert r.writes == []
    assert app_server["hits"]["POST"] == 0  # nothing was sent


def test_non_provisioned_remote_url_refused_and_provisioned_gate_passes():
    """A non-provisioned remote URL refuses writes; a PROVISIONED
    executor aimed at the same unroutable host passes the gate
    (the send is attempted and fails as ENVIRONMENT — the gate,
    not the network, is what provenance controls).
    """
    p = {
        "act": {"method": "POST", "path": "/x", "body": "hi"},
        "assert": [{"type": "status", "equals": 200}],
    }
    refused = Executor(target_url="http://example.invalid", http_timeout=2)
    r = refused.run_probe(p)
    assert r.verdict == Verdict.INCONCLUSIVE
    assert r.reason == InconclusiveReason.PROBE
    assert any("not allowed" in d or "provisioned" in d for d in r.details)

    granted = Executor(
        target_url="http://example.invalid", http_timeout=2, provisioned=True
    )
    r2 = granted.run_probe(p)
    # The gate let it through; the unroutable host is ENVIRONMENT.
    assert r2.verdict == Verdict.INCONCLUSIVE
    assert r2.reason == InconclusiveReason.ENVIRONMENT
    assert not any("not allowed" in d for d in r2.details)


# ---------------------------------------------------------------------------
# Orchestrator: one resolved mode per run; the write grant recorded.
# The Sandbox is faked at the orchestrator's namespace (the
# Addendum 18 pattern): configs are captured, installs succeed, and
# start() returns a real SandboxApp pointed at the test server.
# ---------------------------------------------------------------------------

MINI_APP = '''\
import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer

VALUE = __VALUE__


class Handler(BaseHTTPRequestHandler):
    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._json(200, {"ok": True})
        elif self.path == "/value":
            self._json(200, {"v": VALUE})
        else:
            self._json(404, {"error": "not found"})

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()
'''

MINI_YML = """\
build: "python -m py_compile app.py"
start: "python app.py"
readiness: "/health"
env:
  APP_ENV: verification
fixture:
  repo_name: "mini-app"
  seed_note: "empty; create users with POST /register"
  auth_mechanism: "Bearer token in the Authorization header"
  auth_scope: "a regular user token"
  flags_note: "no feature flags"
"""


def _git(repo: Path, *args: str) -> str:
    p = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=True,
    )
    return p.stdout.strip()


def _make_mini_repo(root: Path) -> dict[str, str]:
    repo = root / "mini-app"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "test")
    _git(repo, "config", "user.email", "test@test")
    (repo / "requirements.txt").write_text("# no third-party deps\n")
    (repo / "proofdeploy.yml").write_text(MINI_YML)
    shas = {}
    for tag, value in (("base", 1), ("bug", 2), ("fix", 1)):
        (repo / "app.py").write_text(MINI_APP.replace("__VALUE__", str(value)))
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", tag)
        shas[tag] = _git(repo, "rev-parse", "HEAD")
    return {"repo": str(repo), **shas}


def _canned_probes(prompt: str):
    from proofdeploy.model_client import ModelResponse

    assert "mini-app" in prompt
    return ModelResponse(
        content=(
            "```json\n"
            '{"setup": [], '
            '"act": {"method": "GET", "path": "/value"}, '
            '"assert": [{"type": "body", "json_path": {"path": "v", "equals": 1}}]}'
            "\n```\n"
        ),
        finish_reason="stop",
    )


class _FakeSandbox:
    """Stands in for proofdeploy.sandbox.Sandbox in the orchestrator.

    Captures every SandboxConfig it is built with; install succeeds
    (or fails for instance indexes in ``fail_install_for``); start
    returns a real SandboxApp at the test server whose isolation
    evidence reports the config's network mode — unless
    ``flip_second_app_mode`` makes the second app report the OTHER
    mode (the mixed-mode defect).
    """

    instances: list = []
    app_url: str = ""
    flip_second_app_mode: bool = False
    fail_install_for: set = set()

    def __init__(self, config):
        self.config = config
        self.log: list[str] = []
        self.failed_step_output = None
        self.index = len(_FakeSandbox.instances)
        _FakeSandbox.instances.append(self)

    def install_steps(self, checkout, steps, env, proxy_env, timeout=600):
        results = []
        for s in steps:
            rc = 1 if self.index in _FakeSandbox.fail_install_for else 0
            results.append({"phase": s["phase"], "exit_code": rc})
            if rc != 0:
                self.failed_step_output = "fake install failure output"
                break
        return results

    def start(self, checkout, command, env, port, name):
        from proofdeploy.sandbox import SandboxApp

        app = SandboxApp(name=name, sandbox=self, target_url=_FakeSandbox.app_url)
        mode = self.config.network_mode
        if _FakeSandbox.flip_second_app_mode and self.index == 1:
            mode = "none+socket" if mode != "none+socket" else "isolated"
        app.sandbox_evidence = {
            "app_network_mode": mode,
            "install_network": "bridge:pd-build",
            "userns_mode": "userns-remap",
            "userns_opt_out": False,
            "app_uid": 0,
            "install_ca_used": False,
        }
        return app

    def app_logs(self, name, tail_lines=100):
        return ""

    def stop(self, name):
        pass

    def isolation_evidence_best_effort(self):
        return {"app_network_mode": self.config.network_mode}


def _stubbed_orchestrator(monkeypatch, tmp_path, app_url, resolver=None):
    """An Orchestrator over a real mini repo, with the measurement
    gates, podman checks and the Sandbox stubbed; the resolver is
    deterministic (no real capability probe)."""
    from proofdeploy import orchestrator as orch_mod
    from proofdeploy.orchestrator import MeasureConfig, Orchestrator

    monkeypatch.setattr(orch_mod, "assert_measurement_gate", lambda: {})
    monkeypatch.setattr(
        orch_mod,
        "assert_harness_commit_gate",
        lambda: {
            "commit": "0" * 40,
            "source": "git",
            "dirty": False,
            "workflow_ref": None,
        },
    )
    monkeypatch.setattr(orch_mod, "check_podman", lambda: (True, ""))
    monkeypatch.setattr(
        orch_mod, "check_sandbox_runtime", lambda *a: ("3.12.15", "sha256:fake", None)
    )
    _FakeSandbox.instances = []
    _FakeSandbox.app_url = app_url
    _FakeSandbox.flip_second_app_mode = False
    _FakeSandbox.fail_install_for = set()
    monkeypatch.setattr(orch_mod, "Sandbox", _FakeSandbox)
    if resolver is None:

        def resolver(requested):
            return ("none+socket", "auto: stubbed capability reason")
    monkeypatch.setattr(orch_mod, "resolve_network_mode", resolver, raising=False)

    info = _make_mini_repo(tmp_path)
    out = tmp_path / "records"
    work = tmp_path / "work"
    cfg = MeasureConfig(
        repo_dir=Path(info["repo"]),
        output_dir=out,
        workdir=work,
        no_skill=True,
        bug_id="wo73-mini",
        rev_bug_base=info["base"],
        rev_bug=info["bug"],
        rev_fix=info["fix"],
        model_runner=_canned_probes,
        public_contract_env_keys=["APP_ENV"],
        sandbox=True,
    )
    return Orchestrator(cfg), out, info


def test_mode_resolved_once_and_passed_to_both_sides(
    monkeypatch, tmp_path, app_server
):
    """The run's network mode is resolved ONCE and the SAME resolved
    value reaches both sides' Sandbox configs and the evidence.

    Planted bug this catches: per-side mode resolution — the
    resolver stub alternates, so a second call would hand side 2 a
    different mode (and the call count gives it away directly).
    """
    calls = []

    def alternating_resolver(requested):
        calls.append(requested)
        return ("isolated", "first resolution") if len(calls) == 1 else (
            "none+socket",
            "second resolution",
        )

    orch, out, _ = _stubbed_orchestrator(
        monkeypatch, tmp_path, app_server["loopback_url"],
        resolver=alternating_resolver,
    )
    summary = orch.measure_bug()
    assert summary["parent_ready"] is True
    assert summary["fix_ready"] is True
    assert calls == ["auto"]  # resolved exactly once, from the run config
    assert [sb.config.network_mode for sb in _FakeSandbox.instances] == [
        "isolated",
        "isolated",
    ]
    from proofdeploy.runpair import read_records

    evidence = [
        r for r in read_records(out) if r.get("record_type") == "evidence"
    ][0]
    assert evidence["provenance"]["network_mode"] == {
        "requested": "auto",
        "resolved": "isolated",
        "reason": "first resolution",
    }
    assert evidence["sandbox_evidence"]["fix_parent"]["app_network_mode"] == "isolated"
    assert evidence["sandbox_evidence"]["fix"]["app_network_mode"] == "isolated"


def test_mixed_mode_run_refused_with_no_score_record(
    monkeypatch, tmp_path, app_server
):
    """The sides report different app network modes: the run is
    REFUSED with the typed environment error and NO score record
    is written.

    Planted bug this catches: the mixed-mode refusal removed — the
    run would complete and a score record would exist.
    """
    from proofdeploy.orchestrator import MixedNetworkModeError
    from proofdeploy.runpair import read_records

    orch, out, _ = _stubbed_orchestrator(
        monkeypatch, tmp_path, app_server["loopback_url"]
    )
    _FakeSandbox.flip_second_app_mode = True
    with pytest.raises(MixedNetworkModeError):
        orch.measure_bug()
    records = read_records(out)
    assert [r for r in records if r.get("record_type") == "score"] == []


def test_write_grant_and_mode_in_evidence_on_both_sides(
    monkeypatch, tmp_path, app_server
):
    """The evidence records write_grant {provisioned, target_url,
    network_mode} per side, and app_network_mode per side.

    Planted bug this catches: write_grant not recorded — the key
    would be absent or empty on both sides.
    """
    from proofdeploy.runpair import read_records

    orch, out, _ = _stubbed_orchestrator(
        monkeypatch,
        tmp_path,
        app_server["loopback_url"],
        resolver=lambda requested: ("isolated", "explicit in test"),
    )
    orch.measure_bug()
    evidence = [
        r for r in read_records(out) if r.get("record_type") == "evidence"
    ][0]
    expected = {
        "provisioned": True,
        "target_url": app_server["loopback_url"],
        "network_mode": "isolated",
    }
    assert evidence["write_grants"]["fix_parent"] == expected
    assert evidence["write_grants"]["fix"] == expected
    assert evidence["sandbox_evidence"]["fix_parent"]["app_network_mode"] == "isolated"
    assert evidence["sandbox_evidence"]["fix"]["app_network_mode"] == "isolated"
    assert evidence["schema_version"] == "1.4.0"


def test_write_grant_not_invented_for_unready_side(
    monkeypatch, tmp_path, app_server
):
    """The fix side's install fails: no executor ever runs, and the
    recorded grant must NOT claim provisioned for it.

    Planted bug this catches: the grant recorded as set for a
    target the orchestrator never provisioned an executor for.
    """
    from proofdeploy.runpair import read_records

    orch, out, _ = _stubbed_orchestrator(
        monkeypatch, tmp_path, app_server["loopback_url"]
    )
    _FakeSandbox.fail_install_for = {1}  # the fix side's install fails
    summary = orch.measure_bug()
    assert summary["fix_ready"] is False
    evidence = [
        r for r in read_records(out) if r.get("record_type") == "evidence"
    ][0]
    assert evidence["write_grants"]["fix"]["provisioned"] is False
    assert evidence["write_grants"]["fix"]["target_url"] is None


def test_clean_run_records_write_grant(monkeypatch, tmp_path, app_server):
    """A clean (single-side) run records the same grant + mode for C."""
    from proofdeploy.orchestrator import MeasureConfig, Orchestrator
    from proofdeploy.runpair import read_records

    orch, out, info = _stubbed_orchestrator(
        monkeypatch,
        tmp_path,
        app_server["loopback_url"],
        resolver=lambda requested: ("isolated", "explicit in test"),
    )
    cfg = MeasureConfig(
        repo_dir=Path(info["repo"]),
        output_dir=out,
        workdir=tmp_path / "work-clean",
        no_skill=True,
        clean_id="wo73-mini-clean",
        rev_clean=info["fix"],
        model_runner=_canned_probes,
        public_contract_env_keys=["APP_ENV"],
        sandbox=True,
    )
    Orchestrator(cfg).measure_clean()
    evidence = [
        r for r in read_records(out) if r.get("record_type") == "evidence"
    ][0]
    assert evidence["write_grants"]["c"] == {
        "provisioned": True,
        "target_url": app_server["loopback_url"],
        "network_mode": "isolated",
    }
    assert evidence["sandbox_evidence"]["c"]["app_network_mode"] == "isolated"


# ---------------------------------------------------------------------------
# Schema 1.3.0: new fields land; older records still load.
# ---------------------------------------------------------------------------


def test_schema_130_and_old_records_still_load(tmp_path):
    from proofdeploy.probe import SCHEMA_VERSION
    from proofdeploy.runpair import build_evidence_record, read_records

    # WO-8.1 bumped the schema to 1.4.0 (REG-A lock fields); the
    # 1.3.0 write_grants behavior pinned here is unchanged.
    assert SCHEMA_VERSION == "1.4.0"
    record = build_evidence_record(run_kind="bug", bug_id="WO73-SCHEMA")
    assert record["schema_version"] == "1.4.0"
    # Callers that do not pass grants get an empty mapping, never a
    # missing key crash for readers.
    assert record["write_grants"] == {}

    grants = {
        "fix_parent": {
            "provisioned": True,
            "target_url": "http://10.89.1.2:8000",
            "network_mode": "isolated",
        },
        "fix": {
            "provisioned": True,
            "target_url": "http://10.89.1.2:8001",
            "network_mode": "isolated",
        },
    }
    record2 = build_evidence_record(
        run_kind="bug", bug_id="WO73-SCHEMA-2", write_grants=grants
    )
    assert record2["write_grants"] == grants

    # A 1.2.0-shaped record (no write_grants key at all) still loads.
    old = dict(record)
    old["schema_version"] = "1.2.0"
    old.pop("write_grants", None)
    runs = tmp_path / "runs.jsonl"
    runs.write_text(json.dumps(old) + "\n", encoding="utf-8")
    loaded = read_records(tmp_path)
    assert len(loaded) == 1
    assert loaded[0]["schema_version"] == "1.2.0"
