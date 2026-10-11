"""WO-7 PR 2: the Node adapter in the sandbox, and (Addendum 31) the
executed commands recorded per provision.

Three groups:

- Node adapter planning (pure, podman-free): frozen installs per
  lockfile (npm ci / pnpm --frozen-lockfile / yarn classic
  --frozen-lockfile / yarn berry --immutable), the packageManager pin
  rendered through corepack with the exact version recorded, a
  packageManager that disagrees with the lockfile present refused,
  and monorepo workspaces refused with the registered wording.
- The sandbox executor running a Node plan through the adapter
  (podman-free stubs, the Addendum 18 pattern): a Node plan is no
  longer refused wholesale; a bun plan still fails closed.
- Addendum 31: each provision result records, per executed step,
  {phase, executor, argv_rendered | shell, exit_code} — for BOTH
  executors rendering the same plan, and the sandbox's install argv
  carries --require-hashes and --no-deps (the host's does not carry
  --no-deps).

Every test fails on the pre-PR-2 code (no corepack rendering in the
plan, no mismatch/workspace refusals, Node refused by the sandbox
executor, no executed_steps on a provision) and passes on the branch.
"""
import hashlib
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from proofdeploy.provisioning import plan_provisioning, select_image

# ---------------------------------------------------------------------------
# Node adapter planning.
# ---------------------------------------------------------------------------


def _contract(**kw):
    base = {"build": None, "migrate": None, "seed": None,
            "start": "node server.js", "env": {}}
    base.update(kw)
    return SimpleNamespace(**base)


def _node_snapshot(tmp_path: Path, *, package_json: dict,
                   lockfiles: dict[str, str] | None = None,
                   extra: dict[str, str] | None = None) -> Path:
    snap = tmp_path / "snap"
    snap.mkdir()
    (snap / "package.json").write_text(
        json.dumps(package_json) + "\n", encoding="utf-8"
    )
    for name, text in (lockfiles or {"package-lock.json": "{}\n"}).items():
        (snap / name).write_text(text, encoding="utf-8")
    for name, text in (extra or {}).items():
        (snap / name).write_text(text, encoding="utf-8")
    return snap


def _install_step(plan):
    step = next(s for s in plan.steps if s.phase == "install")
    assert step.argv is not None
    return step


def test_node_plan_pnpm_pin_renders_corepack_with_exact_version(tmp_path):
    """Planted bug: packageManager ignored. A pinned packageManager
    renders the install through corepack at the exact pinned version,
    with the download prompt disabled, and the version is recorded.
    """
    snap = _node_snapshot(
        tmp_path,
        package_json={"name": "t", "packageManager": "pnpm@9.15.0"},
        lockfiles={"pnpm-lock.yaml": "lockfileVersion: '9.0'\n"},
    )
    plan = plan_provisioning(snap, _contract())
    assert plan.refusal is None
    assert plan.package_manager == "pnpm"
    assert plan.package_manager_version == "9.15.0"
    step = _install_step(plan)
    assert step.argv == [
        "corepack", "pnpm@9.15.0", "install", "--frozen-lockfile",
    ]
    assert step.env.get("COREPACK_ENABLE_DOWNLOAD_PROMPT") == "0"


def test_node_plan_pnpm_without_pin_uses_plain_frozen_install(tmp_path):
    snap = _node_snapshot(
        tmp_path,
        package_json={"name": "t"},
        lockfiles={"pnpm-lock.yaml": "lockfileVersion: '9.0'\n"},
    )
    plan = plan_provisioning(snap, _contract())
    assert plan.refusal is None
    assert plan.package_manager == "pnpm"
    assert plan.package_manager_version is None
    assert _install_step(plan).argv == ["pnpm", "install", "--frozen-lockfile"]


def test_node_plan_npm_pin_renders_corepack(tmp_path):
    snap = _node_snapshot(
        tmp_path,
        package_json={"name": "t", "packageManager": "npm@10.9.0"},
    )
    plan = plan_provisioning(snap, _contract())
    assert plan.refusal is None
    assert plan.package_manager == "npm"
    assert plan.package_manager_version == "10.9.0"
    step = _install_step(plan)
    assert step.argv[:2] == ["corepack", "npm@10.9.0"]
    assert "ci" in step.argv
    assert step.env.get("COREPACK_ENABLE_DOWNLOAD_PROMPT") == "0"


def test_node_plan_yarn_classic_vs_berry(tmp_path):
    classic = _node_snapshot(
        tmp_path / "c" if False else tmp_path,
        package_json={"name": "t"},
        lockfiles={"yarn.lock": "# yarn lockfile v1\n"},
    )
    plan = plan_provisioning(classic, _contract())
    assert plan.package_manager == "yarn"
    assert _install_step(plan).argv == ["yarn", "install", "--frozen-lockfile"]

    berry_dir = tmp_path / "berry"
    berry_dir.mkdir()
    snap = _node_snapshot(
        berry_dir,
        package_json={"name": "t", "packageManager": "yarn@4.5.0"},
        lockfiles={"yarn.lock": "# yarn lockfile (berry)\n"},
    )
    plan = plan_provisioning(snap, _contract())
    assert plan.package_manager == "yarn"
    step = _install_step(plan)
    assert "--immutable" in step.argv
    assert "--frozen-lockfile" not in step.argv


def test_node_plan_package_manager_lockfile_mismatch_refused(tmp_path):
    """Planted bug: lockfile mismatch accepted. packageManager pins
    pnpm but only an npm lockfile is present: the plan is a typed
    BUILD_FAILED refusal, never a silent fall-back to npm.
    """
    snap = _node_snapshot(
        tmp_path,
        package_json={"name": "t", "packageManager": "pnpm@9.15.0"},
        lockfiles={"package-lock.json": "{}\n"},
    )
    plan = plan_provisioning(snap, _contract())
    assert plan.refusal is not None
    assert "packageManager" in plan.refusal
    assert "pnpm" in plan.refusal


def test_node_plan_workspaces_field_refused(tmp_path):
    """Monorepo workspaces are refused with the registered wording."""
    snap = _node_snapshot(
        tmp_path,
        package_json={"name": "t", "workspaces": ["packages/*"]},
    )
    plan = plan_provisioning(snap, _contract())
    assert plan.refusal == "workspaces not yet supported"


def test_node_plan_pnpm_workspace_yaml_refused(tmp_path):
    snap = _node_snapshot(
        tmp_path,
        package_json={"name": "t"},
        lockfiles={"pnpm-lock.yaml": "lockfileVersion: '9.0'\n"},
        extra={"pnpm-workspace.yaml": "packages:\n  - 'packages/*'\n"},
    )
    plan = plan_provisioning(snap, _contract())
    assert plan.refusal == "workspaces not yet supported"


def test_node_plan_runtime_selection_from_engines(tmp_path):
    """Runtime selection is PR 1's rule, exercised on a Node range:
    >=26.2.0 (uptime-kuma's declaration) selects the 26.11.1 row,
    digest-pinned."""
    snap = _node_snapshot(
        tmp_path,
        package_json={"name": "t", "engines": {"node": ">=26.2.0"}},
    )
    plan = plan_provisioning(snap, _contract())
    assert plan.refusal is None
    assert plan.selected_version == "26.11.1"
    row = select_image("node", ">=26.2.0")
    assert row is not None
    assert plan.image_ref == row.pinned_ref
    assert "@sha256:" in (plan.image_ref or "")
    # node_modules persists inside the checkout (install -> app).
    assert plan.persisted_dirs == ["node_modules"]


# ---------------------------------------------------------------------------
# The sandbox executor and Node plans (podman-free stubs).
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


class _FakeInstallSteps:
    """Stands in for Sandbox.install_steps: captures the step
    descriptors and reports every step as exit code 0."""

    def __init__(self):
        self.descriptors: list[dict] = []

    def __call__(self, checkout, steps, env, proxy_env, timeout=600):
        self.descriptors = list(steps)
        return [
            {"phase": s["phase"], "exit_code": 0} for s in steps
        ]


def _stub_sandbox(monkeypatch, snap: Path, contract, install_steps):
    from proofdeploy import orchestrator as orch_mod
    from proofdeploy.orchestrator import MeasureConfig, Orchestrator
    from proofdeploy.provisioning import select_image
    from proofdeploy.sandbox import Sandbox

    monkeypatch.setattr(orch_mod, "_git", lambda *a: "fakesha1234567890abcdef")
    monkeypatch.setattr(
        orch_mod,
        "create_snapshot",
        lambda **kw: SimpleNamespace(dir=snap),
    )
    monkeypatch.setattr(orch_mod, "load_contract", lambda p: contract)
    monkeypatch.setattr(orch_mod, "check_podman", lambda: (True, ""))

    def _runtime(*a):
        row = select_image(a[1] if len(a) > 1 else "python", None)
        assert row is not None
        return row.version, row.digest, None

    monkeypatch.setattr(orch_mod, "check_sandbox_runtime", _runtime)
    monkeypatch.setattr(Sandbox, "_capabilities", lambda self: _fake_caps())
    if install_steps is not None:
        monkeypatch.setattr(Sandbox, "install_steps", install_steps)

    cfg = MeasureConfig(
        repo_dir=snap,
        output_dir=snap / "out",
        workdir=snap / "work",
        no_skill=True,
    )
    orch = Orchestrator(cfg)
    unit_dir = snap / "unit"
    unit_dir.mkdir(exist_ok=True)
    return orch


def _stop_after_install(self, *a, **k):
    from proofdeploy.sandbox import SandboxError

    raise SandboxError("test stops after install")


def test_sandbox_executor_runs_node_plan_through_adapter(
    monkeypatch, tmp_path
):
    """Planted bug: the sandbox path bypassing the adapter / refusing
    Node wholesale. A Node plan now reaches the install phase with
    the adapter's npm ci argv, and the provision records the plan's
    package manager (not a hardcoded 'pip').
    """
    from proofdeploy.runner import ProvisionStatus
    from proofdeploy.sandbox import Sandbox
    from proofdeploy.yml import RepoContract

    snap = tmp_path / "snap"
    snap.mkdir()
    (snap / "package.json").write_text(
        json.dumps({"name": "t", "engines": {"node": ">=20"}}) + "\n"
    )
    (snap / "package-lock.json").write_text("{}\n")
    (snap / "server.js").write_text("// app\n")
    contract = RepoContract(
        build="node --check server.js",
        start="node server.js",
        readiness="/health",
        env={},
    )
    fake = _FakeInstallSteps()
    orch = _stub_sandbox(monkeypatch, snap, contract, fake)
    monkeypatch.setattr(Sandbox, "start", _stop_after_install)
    side = orch._provision_side_sandbox("fakerev", 18093, snap / "unit", "t")

    # Reached the start phase (start was attempted and stubbed to
    # fail): the plan was NOT refused.
    assert side.provision.status == ProvisionStatus.START_FAILED
    assert side.provision.package_manager == "npm"
    assert side.provision.install_plan is not None
    assert side.provision.install_plan["adapter"] == "node"
    install = next(d for d in fake.descriptors if d["phase"] == "install")
    assert install["argv_rendered"] == ["npm", "ci", "--no-audit", "--no-fund"]
    # The executed steps are recorded on the provision (Addendum 31).
    executed = side.provision.executed_steps
    assert executed, "sandbox provision recorded no executed steps"
    rec = next(e for e in executed if e["phase"] == "install")
    assert rec["executor"] == "sandbox"
    assert rec["argv_rendered"] == ["npm", "ci", "--no-audit", "--no-fund"]
    assert rec["exit_code"] == 0


def test_sandbox_executor_refuses_bun_plan(monkeypatch, tmp_path):
    """bun is not one of the sandbox's Node managers: fail closed,
    no install runs."""
    from proofdeploy.runner import ProvisionStatus
    from proofdeploy.yml import RepoContract

    snap = tmp_path / "snap"
    snap.mkdir()
    (snap / "package.json").write_text(json.dumps({"name": "t"}) + "\n")
    (snap / "bun.lock").write_bytes(b"\x00bun")
    contract = RepoContract(
        build="node --check server.js",
        start="node server.js",
        readiness="/health",
        env={},
    )
    fake = _FakeInstallSteps()
    orch = _stub_sandbox(monkeypatch, snap, contract, fake)
    side = orch._provision_side_sandbox("fakerev", 18094, snap / "unit", "t")
    assert side.provision.status == ProvisionStatus.BUILD_FAILED
    assert side.provision.reason == "not yet supported in the sandbox"
    assert fake.descriptors == []


def test_sandbox_executor_refuses_workspaces_plan(monkeypatch, tmp_path):
    from proofdeploy.runner import ProvisionStatus
    from proofdeploy.yml import RepoContract

    snap = tmp_path / "snap"
    snap.mkdir()
    (snap / "package.json").write_text(
        json.dumps({"name": "t", "workspaces": ["packages/*"]}) + "\n"
    )
    (snap / "package-lock.json").write_text("{}\n")
    contract = RepoContract(
        build="node --check server.js",
        start="node server.js",
        readiness="/health",
        env={},
    )
    fake = _FakeInstallSteps()
    orch = _stub_sandbox(monkeypatch, snap, contract, fake)
    side = orch._provision_side_sandbox("fakerev", 18095, snap / "unit", "t")
    assert side.provision.status == ProvisionStatus.BUILD_FAILED
    assert side.provision.reason == "workspaces not yet supported"
    assert fake.descriptors == []


# ---------------------------------------------------------------------------
# Addendum 31: executed steps recorded by BOTH executors.
# ---------------------------------------------------------------------------

SIX_REQUIREMENTS = """\
six==1.17.0 \\
    --hash=sha256:4721f391ed90541fddacab5acf947aa0d3dc7d27b2e1e8eda2be8970586c3274 \\
    --hash=sha256:ff70335d468e7eb6ec65b95b99d3a2836546063f63acc5171de367e834932a81
"""

MINI_APP_WITH_DEP = '''\
import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer

import six

VALUE = 1
SIX_VERSION = six.__version__


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
            self._json(200, {"v": VALUE, "six": SIX_VERSION})
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
  seed_note: "empty"
  auth_mechanism: "Bearer token in the Authorization header"
  auth_scope: "a regular user token"
  flags_note: "no feature flags"
"""


def _python_snapshot_with_dep(tmp_path: Path) -> Path:
    snap = tmp_path / "snap"
    snap.mkdir()
    (snap / "app.py").write_text(MINI_APP_WITH_DEP)
    (snap / "requirements.txt").write_text(SIX_REQUIREMENTS)
    (snap / "proofdeploy.yml").write_text(MINI_YML)
    return snap


def test_host_executor_records_executed_steps(tmp_path):
    """Addendum 31 (host): a real host provision records every
    executed step — phase, executor, the rendered argv (install) /
    shell (contract steps), and the exit code. The host's pip
    rendering carries --require-hashes but NOT --no-deps (that flag
    is the sandbox's frozen-install rule).
    """
    from proofdeploy.orchestrator import _free_port
    from proofdeploy.runner import Provisioner
    from proofdeploy.yml import load_contract

    snap = _python_snapshot_with_dep(tmp_path)
    contract = load_contract(snap / "proofdeploy.yml")
    expected = hashlib.sha256(SIX_REQUIREMENTS.encode()).hexdigest()
    (tmp_path / "work").mkdir(parents=True, exist_ok=True)
    runner = Provisioner(workdir=tmp_path / "work")
    result = runner.provision(
        snap, contract, _free_port(),
        expected_lockfile_sha256=expected,
    )
    try:
        assert result.ready, result.reason
        steps = result.executed_steps
        phases = [s["phase"] for s in steps]
        assert phases == ["venv", "install", "build", "start"], phases
        assert all(s["executor"] == "host" for s in steps)
        install = next(s for s in steps if s["phase"] == "install")
        assert "--require-hashes" in install["argv_rendered"]
        assert "--no-deps" not in install["argv_rendered"]
        assert install["exit_code"] == 0
        build = next(s for s in steps if s["phase"] == "build")
        assert build["shell"] == "python -m py_compile app.py"
        assert build["exit_code"] == 0
        start = next(s for s in steps if s["phase"] == "start")
        assert start["shell"] == "python app.py"
        assert start["exit_code"] is None  # still running at READY
        # And the record form carries the steps.
        assert result.to_dict()["executed_steps"] == steps
    finally:
        if result.run_id:
            runner.stop_target(result.proc)
            runner.cleanup_run(result.run_id)


def test_both_executors_record_their_rendering_of_the_same_plan(
    monkeypatch, tmp_path
):
    """Addendum 31 (the registered test): the host and sandbox
    renderings of the SAME plan are both recorded on their provision
    results, and the sandbox's install argv contains --require-hashes
    and --no-deps.
    """
    from proofdeploy.orchestrator import _free_port
    from proofdeploy.runner import Provisioner
    from proofdeploy.sandbox import Sandbox
    from proofdeploy.yml import RepoContract, load_contract

    snap = _python_snapshot_with_dep(tmp_path)
    expected = hashlib.sha256(SIX_REQUIREMENTS.encode()).hexdigest()

    # Host rendering (real provision).
    contract = load_contract(snap / "proofdeploy.yml")
    (tmp_path / "work").mkdir(parents=True, exist_ok=True)
    runner = Provisioner(workdir=tmp_path / "work")
    host_result = runner.provision(
        snap, contract, _free_port(),
        expected_lockfile_sha256=expected,
    )
    try:
        assert host_result.ready, host_result.reason
    finally:
        if host_result.run_id:
            runner.stop_target(host_result.proc)
            runner.cleanup_run(host_result.run_id)

    # Sandbox rendering (container runtime stubbed; the rendering and
    # the recording are the real code paths).
    sb_contract = RepoContract(
        build="python -m py_compile app.py",
        start="python app.py",
        readiness="/health",
        env={},
    )
    fake = _FakeInstallSteps()
    orch = _stub_sandbox(monkeypatch, snap, sb_contract, fake)
    monkeypatch.setattr(Sandbox, "start", _stop_after_install)
    side = orch._provision_side_sandbox("fakerev", 18096, snap / "unit", "t")
    sandbox_steps = side.provision.executed_steps

    # Same plan on both sides.
    assert host_result.install_plan_sha256
    assert host_result.install_plan_sha256 == side.provision.install_plan_sha256

    host_install = next(
        s for s in host_result.executed_steps if s["phase"] == "install"
    )
    sb_install = next(s for s in sandbox_steps if s["phase"] == "install")
    assert host_install["executor"] == "host"
    assert sb_install["executor"] == "sandbox"
    assert "--require-hashes" in sb_install["argv_rendered"]
    assert "--no-deps" in sb_install["argv_rendered"]
    assert "--require-hashes" in host_install["argv_rendered"]
    assert "--no-deps" not in host_install["argv_rendered"]
    assert sb_install["exit_code"] == 0
    # The sandbox-only verify step is recorded too.
    assert any(s["phase"] == "verify" for s in sandbox_steps)


def test_executed_steps_survive_a_failed_step(tmp_path):
    """A step that fails is recorded with its real exit code, and the
    steps after it are not recorded (they never ran)."""
    from proofdeploy.orchestrator import _free_port
    from proofdeploy.runner import Provisioner, ProvisionStatus
    from proofdeploy.yml import load_contract

    snap = _python_snapshot_with_dep(tmp_path)
    yml = MINI_YML.replace(
        'build: "python -m py_compile app.py"',
        'build: "python -c \'import sys; sys.exit(3)\'"',
    )
    (snap / "proofdeploy.yml").write_text(yml)
    contract = load_contract(snap / "proofdeploy.yml")
    expected = hashlib.sha256(SIX_REQUIREMENTS.encode()).hexdigest()
    (tmp_path / "work").mkdir(parents=True, exist_ok=True)
    runner = Provisioner(workdir=tmp_path / "work")
    result = runner.provision(
        snap, contract, _free_port(),
        expected_lockfile_sha256=expected,
    )
    assert result.status == ProvisionStatus.BUILD_FAILED
    phases = [s["phase"] for s in result.executed_steps]
    assert phases == ["venv", "install", "build"], phases
    build = result.executed_steps[-1]
    assert build["exit_code"] == 3


def test_git_available_for_golden_kit():
    """The conformance kit materializes git histories; git must exist."""
    proc = subprocess.run(
        ["git", "--version"], capture_output=True, text=True, timeout=10
    )
    assert proc.returncode == 0


# ---------------------------------------------------------------------------
# Fixes proven by the first sandbox-CI run of this PR.
# ---------------------------------------------------------------------------


def test_range_with_spaced_operator_selects_image():
    """uptime-kuma declares engines.node '>= 26.2.0' — npm semver
    allows whitespace between the operator and the version. The
    parser must not lose the comparator to a bare '>=' token (the
    first CI run of this PR refused the plan: 'declared range
    >= 26.2.0 not satisfiable by pinned images').
    """
    from proofdeploy.provisioning import select_image
    from proofdeploy.runner import satisfies_range

    assert satisfies_range("26.11.1", ">= 26.2.0", ecosystem="node")
    assert not satisfies_range("24.21.0", ">= 26.2.0", ecosystem="node")
    row = select_image("node", ">= 26.2.0")
    assert row is not None and row.version == "26.11.1"
    # The golden apps' declaration form, spaced throughout.
    row = select_image("node", ">= 20 < 25")
    assert row is not None and row.version == "24.21.0"


def test_node_plan_with_kuma_engines_string(tmp_path):
    """Plan-level: a snapshot whose engines use the spaced form
    selects the 26.11.1 pinned image instead of refusing."""
    snap = _node_snapshot(
        tmp_path,
        package_json={"name": "kuma", "engines": {"node": ">= 26.2.0"}},
    )
    plan = plan_provisioning(snap, _contract())
    assert plan.image_refusal is None
    assert plan.selected_version == "26.11.1"


def _leak_check(tmp_path: Path, *, yml_text: str, other_files: dict):
    """Drive Orchestrator._leak_check_bundle podman-free with a fake
    bundle whose snapshot holds the given contract + files."""
    from proofdeploy.orchestrator import MeasureConfig, Orchestrator
    from proofdeploy.yml import RepoContract

    snap = tmp_path / "snapshot"
    snap.mkdir()
    (snap / "proofdeploy.yml").write_text(yml_text, encoding="utf-8")
    for name, text in other_files.items():
        (snap / name).write_text(text, encoding="utf-8")
    diff = tmp_path / "diff.patch"
    diff.write_text("diff --git a/x b/x\n", encoding="utf-8")
    desc = tmp_path / "description.txt"
    desc.write_text("fixture description\n", encoding="utf-8")
    bundle = SimpleNamespace(
        diff=diff, description=desc, skill=None, snapshot=snap
    )
    contract = RepoContract(
        build="x", start="x", readiness="/health",
        env={"CANARY_SECRET": "pdcanary-env-test123"},
        fixture={},
    )
    cfg = MeasureConfig(
        repo_dir=tmp_path, output_dir=tmp_path / "out",
        workdir=tmp_path / "work", no_skill=True,
    )
    orch = Orchestrator(cfg)
    return orch, bundle, contract


def test_leak_check_allows_contracts_own_env_declaration(tmp_path):
    """WO-7's golden contracts declare a canary env value in
    proofdeploy.yml. The bundle scan must not refuse the run over
    the declaration itself (the first CI run of this PR errored all
    30 conformance cases on exactly this)."""
    from proofdeploy.leakcheck import SecretLeakError  # noqa: F401

    orch, bundle, contract = _leak_check(
        tmp_path,
        yml_text='env:\n  CANARY_SECRET: "pdcanary-env-test123"\n',
        other_files={"server.js": "console.log('clean');\n"},
    )
    orch._leak_check_bundle(bundle, "a clean prompt", contract)


def test_leak_check_still_refuses_declared_value_in_other_files(tmp_path):
    """The exemption is scoped to the contract file: the same value
    in any other bundle file is still a leak, refused fail-closed."""
    from proofdeploy.leakcheck import SecretLeakError

    orch, bundle, contract = _leak_check(
        tmp_path,
        yml_text='env:\n  CANARY_SECRET: "pdcanary-env-test123"\n',
        other_files={"server.js": "const s = 'pdcanary-env-test123';\n"},
    )
    with pytest.raises(SecretLeakError):
        orch._leak_check_bundle(bundle, "a clean prompt", contract)


def test_leak_check_still_refuses_other_secrets_in_contract_file(tmp_path):
    """Only the contract's declared ENV values are exempted in the
    contract file: a different secret smuggled into proofdeploy.yml
    (here a non-public fixture value) still refuses the run."""
    from proofdeploy.leakcheck import SecretLeakError
    from proofdeploy.orchestrator import MeasureConfig, Orchestrator
    from proofdeploy.yml import RepoContract

    orch, bundle, _ = _leak_check(
        tmp_path,
        yml_text=(
            'fixture:\n  internal_note: "fixture-secret-abcdef"\n'
            'env:\n  CANARY_SECRET: "pdcanary-env-test123"\n'
        ),
        other_files={"server.js": "console.log('clean');\n"},
    )
    contract = RepoContract(
        build="x", start="x", readiness="/health",
        env={"CANARY_SECRET": "pdcanary-env-test123"},
        fixture={"internal_note": "fixture-secret-abcdef"},
    )
    cfg = MeasureConfig(
        repo_dir=tmp_path, output_dir=tmp_path / "out",
        workdir=tmp_path / "work", no_skill=True,
    )
    orch = Orchestrator(cfg)
    with pytest.raises(SecretLeakError):
        orch._leak_check_bundle(bundle, "a clean prompt", contract)
