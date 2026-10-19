# ruff: noqa: E501  (long embedded stub JSON / fixture lines)
"""WO-7 PR 1: both executors run the adapter's plan, and the evidence
records the plan.

Covers the reviewer's planted-bug list for PR 1:

- the sandbox path bypassing the adapter (a spy on plan_provisioning
  must fire when _provision_side_sandbox runs);
- the reported runtime not checked against the plan
  (check_sandbox_runtime with an expected version must fail closed);
- the digest not pinned (the sandbox must be handed the pinned
  ref@digest from the plan, never a floating tag);
- node_modules / the venv living outside the checkout (the plan's
  persisted dirs are checkout-relative).

Plus the evidence contract: a measured record carries the install
plan and its sha256, and the plan's lockfile hash matches the
provision's.

The podman-free tests stub only the container runtime (the Addendum
18 pattern). The evidence test runs a real unsandboxed (dev) measure
with a canned model. Every test fails on the pre-WO-7 code and passes
on the branch.
"""
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

from proofdeploy.orchestrator import check_sandbox_runtime

# ---------------------------------------------------------------------------
# Reported runtime vs the plan (planted bug: not checked).
# ---------------------------------------------------------------------------


class _FakeResult:
    def __init__(self, stdout):
        self.stdout = stdout
        self.stderr = ""


def _fake_runner(version_line: str):
    def fake_run(cmd, **kwargs):
        if "--version" in cmd:
            return _FakeResult(version_line)
        return _FakeResult("sha256:abc123\n")

    return fake_run


def test_reported_version_mismatch_with_plan_fails_closed():
    """The version the target reports inside the container is checked
    against the plan's selected version: a mismatch is BUILD_FAILED.

    Planted bug: drop the expected-version comparison -> failure is
    None -> FAIL.
    """
    version, digest, failure = check_sandbox_runtime(
        "docker.io/library/python:3.12.15-bookworm@sha256:5560",
        "python",
        ">=3.12,<3.13",
        "3.12.15",
        runner=_fake_runner("Python 3.12.3\n"),
    )
    assert version == "3.12.3"
    assert failure is not None
    assert "does not match" in failure
    assert "3.12.15" in failure


def test_reported_version_matching_plan_passes():
    version, digest, failure = check_sandbox_runtime(
        "docker.io/library/python:3.12.15-bookworm@sha256:5560",
        "python",
        ">=3.12,<3.13",
        "3.12.15",
        runner=_fake_runner("Python 3.12.15\n"),
    )
    assert version == "3.12.15"
    assert digest == "sha256:abc123"
    assert failure is None


# ---------------------------------------------------------------------------
# The sandbox path goes through the adapter and runs the pinned plan.
# ---------------------------------------------------------------------------


def _fake_caps():
    from proofdeploy.sandbox import SandboxCapabilities

    return SandboxCapabilities(
        podman=True,
        podman_version="podman version 4.9.3",
        isolated_network=False,
        build_network=True,
        userns_remap=True,
        reason="",
    )


def _stub_sandbox_side(monkeypatch, snap: Path, install):
    """Drive Orchestrator._provision_side_sandbox podman-free (the
    Addendum 18 scaffolding): git/snapshot/contract/podman/runtime
    checks stubbed, the sandbox install is the given fake."""
    from proofdeploy import orchestrator as orch_mod
    from proofdeploy.orchestrator import MeasureConfig, Orchestrator
    from proofdeploy.provisioning import select_image
    from proofdeploy.sandbox import Sandbox
    from proofdeploy.yml import RepoContract

    row = select_image("python", None)
    assert row is not None
    monkeypatch.setattr(orch_mod, "_git", lambda *a: "fakesha1234567890abcdef")
    monkeypatch.setattr(
        orch_mod,
        "create_snapshot",
        lambda **kw: SimpleNamespace(dir=snap),
    )
    contract = RepoContract(
        build="", start="python app.py", readiness="/health", env={}
    )
    monkeypatch.setattr(orch_mod, "load_contract", lambda p: contract)
    monkeypatch.setattr(orch_mod, "check_podman", lambda: (True, ""))
    monkeypatch.setattr(
        orch_mod,
        "check_sandbox_runtime",
        lambda *a: (row.version, row.digest, None),
    )
    monkeypatch.setattr(Sandbox, "_capabilities", lambda self: _fake_caps())
    if install is not None:
        monkeypatch.setattr(Sandbox, "install_steps", install)

    cfg = MeasureConfig(
        repo_dir=snap,
        output_dir=snap / "out",
        workdir=snap / "work",
        no_skill=True,
    )
    orch = Orchestrator(cfg)
    unit_dir = snap / "unit"
    unit_dir.mkdir(exist_ok=True)
    return orch, orch_mod


class _CaptureInstall:
    """Fake Sandbox.install_steps (WO-7 PR 2 signature): captures the
    step descriptors, reports every step as exit code 0."""

    def __init__(self):
        self.descriptors: list[dict] = []

    @property
    def commands(self) -> list[str]:
        return [d["command"] for d in self.descriptors]

    def __call__(self, checkout, steps, env, proxy_env, timeout=600):
        self.descriptors.extend(steps)
        return [
            {"phase": s["phase"], "exit_code": 0} for s in steps
        ]


def test_sandbox_path_plans_through_the_adapter(monkeypatch, tmp_path):
    """Planted bug: the sandbox path bypassing the adapter. The spy
    replaces plan_provisioning in the orchestrator's namespace; a
    sandbox provision that never calls it fails this test.
    """
    from proofdeploy import provisioning
    from proofdeploy.runner import ProvisionStatus
    from proofdeploy.sandbox import Sandbox, SandboxError

    snap = tmp_path / "snap"
    snap.mkdir()
    (snap / "requirements.txt").write_text(
        "six==1.17.0 \\\n"
        "    --hash=sha256:ff703d0fc3e08526949441584fca3fc209d20cf45a3f54c0fb84f79a80d0d95b1\n"
    )
    cap = _CaptureInstall()
    orch, orch_mod = _stub_sandbox_side(monkeypatch, snap, cap)

    calls = []
    real_plan = provisioning.plan_provisioning

    def spy(snapshot_dir, contract, lock_cutoff=None):
        calls.append(Path(snapshot_dir))
        return real_plan(snapshot_dir, contract, lock_cutoff=lock_cutoff)

    monkeypatch.setattr(orch_mod, "plan_provisioning", spy)

    def _stop_after_install(self, *a, **k):
        raise SandboxError("test stops after install")

    monkeypatch.setattr(Sandbox, "start", _stop_after_install)
    side = orch._provision_side_sandbox("fakerev", 18090, snap / "unit", "t")

    assert calls, "sandbox path never called the provisioning adapter"
    assert side.provision.status == ProvisionStatus.START_FAILED
    # The provision carries the plan and its hash.
    assert side.provision.install_plan is not None
    assert side.provision.install_plan["adapter"] == "python"
    assert side.provision.install_plan_sha256


def test_sandbox_executor_uses_pinned_image_from_plan(monkeypatch, tmp_path):
    """Planted bug: digest not pinned. The Sandbox the executor builds
    must be configured with the plan's ref@digest, never a bare tag.
    """
    from proofdeploy.provisioning import select_image
    from proofdeploy.sandbox import Sandbox, SandboxError

    snap = tmp_path / "snap"
    snap.mkdir()
    (snap / "requirements.txt").write_text(
        "six==1.17.0 \\\n"
        "    --hash=sha256:ff703d0fc3e08526949441584fca3fc209d20cf45a3f54c0fb84f79a80d0d95b1\n"
    )
    cap = _CaptureInstall()
    orch, _ = _stub_sandbox_side(monkeypatch, snap, cap)

    seen_images = []
    real_init = Sandbox.__init__

    def spy_init(self, config):
        seen_images.append(config.image)
        return real_init(self, config)

    monkeypatch.setattr(Sandbox, "__init__", spy_init)

    def _stop_after_install(self, *a, **k):
        raise SandboxError("test stops after install")

    monkeypatch.setattr(Sandbox, "start", _stop_after_install)
    orch._provision_side_sandbox("fakerev", 18091, snap / "unit", "t")

    row = select_image("python", None)
    assert row is not None
    assert seen_images, "executor never constructed a Sandbox"
    assert all("@" in img for img in seen_images), seen_images
    assert row.pinned_ref in seen_images


def test_sandbox_executor_runs_bun_plan(monkeypatch, tmp_path):
    """Plan 1.2e (D22): a bun plan runs in the sandbox (before
    1.2e this asserted the fail-closed refusal "not yet supported
    in the sandbox" with no install attempted — the behavior the
    plan line replaces). The install phase the executor issues is
    the plan's digest-pinned bun bootstrap followed by the frozen
    install through the persisted bun binary.
    """
    from proofdeploy.runner import ProvisionStatus
    from proofdeploy.sandbox import Sandbox, SandboxError

    snap = tmp_path / "snap"
    snap.mkdir()
    (snap / "package.json").write_text(
        '{"name": "t", "packageManager": "bun@1.4.3"}\n'
    )
    (snap / "bun.lock").write_text('{"lockfileVersion": 1}\n')
    cap = _CaptureInstall()
    orch, _ = _stub_sandbox_side(monkeypatch, snap, cap)

    def _stop_after_install(self, *a, **k):
        raise SandboxError("test stops after install")

    monkeypatch.setattr(Sandbox, "start", _stop_after_install)
    side = orch._provision_side_sandbox("fakerev", 18092, snap / "unit", "t")
    # Reached the start phase (stubbed to fail there): NOT refused.
    assert side.provision.status == ProvisionStatus.START_FAILED
    assert side.provision.package_manager == "bun"
    assert side.provision.install_plan is not None
    assert side.provision.install_plan["adapter"] == "node"
    assert side.provision.install_plan["bun_version"] == "1.4.3"
    installs = [d for d in cap.descriptors if d["phase"] == "install"]
    assert len(installs) == 2
    bootstrap, frozen = installs
    assert bootstrap["argv_rendered"][:2] == ["bash", "-c"]
    assert "sha256sum -c" in bootstrap["argv_rendered"][2]
    assert frozen["argv_rendered"] == [
        "/pd-home/.pd-tools/bin/bun", "install", "--frozen-lockfile",
    ]


# ---------------------------------------------------------------------------
# Plan 1.2(o) / D114: the ORCHESTRATOR's handoff of the contract's
# declared app.port into the sandbox start. The bridge tests in
# test_sandbox_addendum18 drive Sandbox.start directly and never see
# this handoff; the reviewer's plant (dropping
# `app_port=contract.app_port` from the orchestrator's start call)
# survived them. These drive the real provisioning path
# (Orchestrator._provision_side_sandbox) with a recording fake
# Sandbox.start and assert what actually arrives.
# ---------------------------------------------------------------------------


class _FakeStartedApp:
    """The SandboxApp surface the provisioning path touches after a
    successful start: readiness, name, URL, evidence, stop."""

    def __init__(self, name):
        self.name = name
        self.target_url = "http://127.0.0.1:9/fake-app"
        self.sandbox_evidence: dict = {}

    def wait_ready(self, path, timeout=120):
        return True

    def stop(self):
        pass


class _RecordingStart:
    """Fake Sandbox.start: records every call's app_port kwarg (with
    a sentinel default, so a handoff that drops the kwarg entirely
    records "UNSET" instead of raising) and returns a ready app."""

    def __init__(self):
        self.calls: list[dict] = []

    def __call__(self, checkout, command, env, port, name=None,
                 app_port="UNSET"):
        # Set on the Sandbox CLASS, this instance is not a descriptor:
        # sb.start(...) reaches __call__ with start's own arguments.
        self.calls.append({"port": port, "name": name, "app_port": app_port})
        return _FakeStartedApp(name)


def _drive_provision_with_recording_start(monkeypatch, tmp_path, app_port):
    """Run the real _provision_side_sandbox path (the Addendum 18
    scaffolding) with a contract declaring the given app.port and a
    recording Sandbox.start; the side comes back READY."""
    from proofdeploy.runner import ProvisionStatus
    from proofdeploy.sandbox import Sandbox
    from proofdeploy.yml import RepoContract

    snap = tmp_path / "snap"
    snap.mkdir()
    (snap / "requirements.txt").write_text(
        "six==1.17.0 \\\n"
        "    --hash=sha256:ff703d0fc3e08526949441584fca3fc209d20cf45a3f54c0fb84f79a80d0d95b1\n"
    )
    cap = _CaptureInstall()
    orch, orch_mod = _stub_sandbox_side(monkeypatch, snap, cap)
    contract = RepoContract(
        build="", start="python app.py", readiness="/health", env={},
        app_port=app_port,
    )
    monkeypatch.setattr(orch_mod, "load_contract", lambda p: contract)
    recorder = _RecordingStart()
    monkeypatch.setattr(Sandbox, "start", recorder)
    monkeypatch.setattr(Sandbox, "app_logs", lambda self, name: "")
    side = orch._provision_side_sandbox(
        "fakerev", 18093 if app_port is not None else 18094,
        snap / "unit", "t",
    )
    assert side.provision.status == ProvisionStatus.READY
    assert len(recorder.calls) == 1
    return recorder.calls[0]


def test_orchestrator_hands_declared_app_port_to_sandbox_start(
    monkeypatch, tmp_path
):
    """A contract declaring app.port: 3000 must arrive at the sandbox
    start call as app_port == 3000, through the real orchestrator
    provisioning path (readiness and measured runs share it).

    Planted bug (D114): the orchestrator's start call stops passing
    app_port=contract.app_port (or passes None) -> the recording fake
    sees None/"UNSET" -> FAIL.
    """
    call = _drive_provision_with_recording_start(monkeypatch, tmp_path, 3000)
    assert call["app_port"] == 3000


def test_orchestrator_hands_none_app_port_when_contract_declares_none(
    monkeypatch, tmp_path
):
    """A contract declaring no app.port must arrive as app_port is
    None — the default stays exactly as before, never an inferred
    port."""
    call = _drive_provision_with_recording_start(monkeypatch, tmp_path, None)
    assert call["app_port"] is None


# ---------------------------------------------------------------------------
# The evidence records the plan (host executor, real dev run).
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
  seed_note: "empty; create users with POST /register; GET /value reports v equal to 1 on a correct build"
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
            '"assert": [{"type": "body", "json_path": {"path": "v", "equals": 1}, "evidence": {"source": "fixture", "quote": "GET /value reports v equal to 1"}}]}'
            "\n```\n"
        ),
        finish_reason="stop",
    )


def test_evidence_record_carries_the_install_plan(tmp_path):
    """A measured run's evidence record carries each provision's
    install plan and the plan's sha256; the plan's lockfile hash is
    the provision's lockfile hash, and the recorded hash verifies
    against the recorded plan.
    """
    from proofdeploy.orchestrator import MeasureConfig, Orchestrator
    from proofdeploy.provisioning import InstallPlan

    info = _make_mini_repo(tmp_path)
    out = tmp_path / "records"
    cfg = MeasureConfig(
        repo_dir=Path(info["repo"]),
        output_dir=out,
        workdir=tmp_path / "work",
        no_skill=True,
        bug_id="mini-plan-evidence",
        rev_bug_base=info["base"],
        rev_bug=info["bug"],
        rev_fix=info["fix"],
        model_runner=_canned_probes,
        public_contract_env_keys=["APP_ENV"],
        allow_unsandboxed=True,
    )
    summary = Orchestrator(cfg).measure_bug()
    assert summary["parent_ready"] is True
    assert summary["fix_ready"] is True

    records = [
        json.loads(line)
        for line in (out / "runs.jsonl").read_text().splitlines()
        if line.strip()
    ]
    evidence = records[0]
    for key in ("fix_parent_provision", "fix_provision"):
        provision = evidence[key]
        plan = provision["install_plan"]
        assert plan is not None, f"{key} has no install plan in evidence"
        assert plan["adapter"] == "python"
        assert plan["runtime"] == "python"
        assert plan["lockfile_name"] == "requirements.txt"
        assert plan["lockfile_sha256"] == provision["lockfile_sha256"]
        recorded = InstallPlan.from_dict(plan)
        assert provision["install_plan_sha256"] == recorded.sha256()
