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
  longer refused wholesale; a bun plan runs too (plan 1.2e: its
  digest-pinned bootstrap + frozen install, with .pd-tools/bin
  first on the contract steps' PATH).
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
import sys
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
    steps = _install_steps(plan)
    assert len(steps) == 2
    # Addendum 35: corepack is bootstrapped into the checkout
    # (.pd-tools, persisted) — never -g, which dies with the
    # install container — and the pinned install runs through the
    # persisted binary.
    assert steps[0].argv == [
        "npm", "install", "--prefix", "{checkout}/.pd-tools",
        "--no-save", "--no-audit", "--no-fund", "corepack@0.36.0",
    ]
    assert steps[1].argv == [
        "{checkout}/.pd-tools/node_modules/.bin/corepack",
        "pnpm@9.15.0", "install", "--frozen-lockfile",
    ]
    assert steps[1].env.get("COREPACK_ENABLE_DOWNLOAD_PROMPT") == "0"
    assert plan.corepack_version == "0.36.0"
    assert plan.persisted_dirs == ["node_modules", ".pd-tools"]


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
    steps = _install_steps(plan)
    assert len(steps) == 2
    step = steps[1]
    assert step.argv[:2] == [
        "{checkout}/.pd-tools/node_modules/.bin/corepack", "npm@10.9.0",
    ]
    assert "ci" in step.argv
    assert step.env.get("COREPACK_ENABLE_DOWNLOAD_PROMPT") == "0"
    assert plan.corepack_version == "0.36.0"


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
    # A berry pin renders through the persisted corepack too: the
    # frozen install is the LAST install-phase step (the bootstrap
    # precedes it).
    step = _install_steps(plan)[-1]
    assert step.argv[0] == "{checkout}/.pd-tools/node_modules/.bin/corepack"
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
    # D50 (Master, 2026-10-15): a not-supported class — the refusal
    # carries the literal prefix, remainder byte-identical.
    assert plan.refusal == (
        "not supported: packageManager pnpm@9.15.0 disagrees with the "
        "lockfile present in the snapshot (package-lock.json); "
        "refusing to install with a different package manager than "
        "the one pinned"
    )


def test_node_plan_workspaces_field_plans_the_member(tmp_path):
    """Plan 1.2b: a package.json `workspaces` declaration is planned,
    not refused (before 1.2b this asserted the typed refusal
    "workspaces not yet supported" — the behavior the plan line
    replaces). The single member is the app; the frozen install
    stays at the root."""
    snap = _node_snapshot(
        tmp_path,
        package_json={"name": "t", "workspaces": ["packages/*"]},
    )
    member = snap / "packages" / "app"
    member.mkdir(parents=True)
    (member / "package.json").write_text('{"name": "app"}\n', encoding="utf-8")
    plan = plan_provisioning(snap, _contract())
    assert plan.refusal is None
    assert plan.package_manager == "npm"
    assert plan.app_path == "packages/app"
    start = next(s for s in plan.steps if s.phase == "start")
    assert start.cwd == "packages/app"


def test_node_plan_pnpm_workspace_yaml_plans_the_member(tmp_path):
    """Plan 1.2b: a pnpm-workspace.yaml declaration is planned, not
    refused (the pre-1.2b assertion of "workspaces not yet
    supported" is the behavior the plan line replaces)."""
    snap = _node_snapshot(
        tmp_path,
        package_json={"name": "t"},
        lockfiles={"pnpm-lock.yaml": "lockfileVersion: '9.0'\n"},
        extra={"pnpm-workspace.yaml": "packages:\n  - 'packages/*'\n"},
    )
    member = snap / "packages" / "app"
    member.mkdir(parents=True)
    (member / "package.json").write_text('{"name": "app"}\n', encoding="utf-8")
    plan = plan_provisioning(snap, _contract())
    assert plan.refusal is None
    assert plan.package_manager == "pnpm"
    assert plan.app_path == "packages/app"


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


def test_sandbox_executor_runs_corepack_pinned_pnpm_plan(
    monkeypatch, tmp_path
):
    """Plan 1.2(f): a corepack-pinned pnpm plan's contract steps
    render with the persisted corepack's manager bin first on PATH,
    so a step calling bare `pnpm` runs the plan's pinned version
    (the corepack package's shim resolves the snapshot's
    packageManager pin) instead of dying with
    "env: 'pnpm': No such file or directory" — the Ghost failure
    this plan line fixes. The install phase is the plan's .pd-tools
    corepack bootstrap plus the frozen install through the
    persisted corepack binary, exactly as planned."""
    from proofdeploy.runner import ProvisionStatus
    from proofdeploy.sandbox import Sandbox
    from proofdeploy.yml import RepoContract

    snap = tmp_path / "snap"
    snap.mkdir()
    (snap / "package.json").write_text(
        json.dumps({
            "name": "t",
            "packageManager": "pnpm@9.15.0",
            "engines": {"node": ">=20"},
        }) + "\n"
    )
    (snap / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n")
    (snap / "server.js").write_text("// app\n")
    contract = RepoContract(
        build="pnpm --version",
        start="pnpm run start",
        readiness="/health",
        env={},
    )
    fake = _FakeInstallSteps()
    orch = _stub_sandbox(monkeypatch, snap, contract, fake)
    monkeypatch.setattr(Sandbox, "start", _stop_after_install)
    side = orch._provision_side_sandbox("fakerev", 18097, snap / "unit", "t")

    # Reached the start phase (stubbed to fail there): NOT refused.
    assert side.provision.status == ProvisionStatus.START_FAILED
    assert side.provision.package_manager == "pnpm"
    assert side.provision.install_plan is not None
    assert side.provision.install_plan["package_manager_version"] == "9.15.0"
    assert side.provision.install_plan["corepack_version"] == "0.36.0"
    installs = [d for d in fake.descriptors if d["phase"] == "install"]
    assert len(installs) == 2
    bootstrap, frozen = installs
    assert bootstrap["argv_rendered"] == [
        "npm", "install", "--prefix", "/checkout/.pd-tools",
        "--no-save", "--no-audit", "--no-fund", "corepack@0.36.0",
    ]
    assert frozen["argv_rendered"] == [
        "/checkout/.pd-tools/node_modules/.bin/corepack",
        "pnpm@9.15.0", "install", "--frozen-lockfile",
    ]
    build = next(d for d in fake.descriptors if d["phase"] == "build")
    assert build["shell"] == (
        "export PATH=/checkout/.pd-tools/node_modules/.bin:"
        "/checkout/node_modules/.bin:$PATH; pnpm --version"
    )
    start = next(
        e for e in side.provision.executed_steps if e["phase"] == "start"
    )
    assert start["shell"] == (
        "export PATH=/checkout/.pd-tools/node_modules/.bin:"
        "/checkout/node_modules/.bin:$PATH; pnpm run start"
    )


def test_sandbox_executor_unpinned_pnpm_plan_gets_no_manager_bin(
    monkeypatch, tmp_path
):
    """Plan 1.2(f) scope guard: an UNPINNED pnpm plan never
    bootstraps corepack, so there is no persisted manager bin to
    put on PATH — its contract steps keep the plain
    node_modules/.bin rendering."""
    from proofdeploy.runner import ProvisionStatus
    from proofdeploy.sandbox import Sandbox
    from proofdeploy.yml import RepoContract

    snap = tmp_path / "snap"
    snap.mkdir()
    (snap / "package.json").write_text(
        json.dumps({"name": "t", "engines": {"node": ">=20"}}) + "\n"
    )
    (snap / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n")
    (snap / "server.js").write_text("// app\n")
    contract = RepoContract(
        build="pnpm --version",
        start="node server.js",
        readiness="/health",
        env={},
    )
    fake = _FakeInstallSteps()
    orch = _stub_sandbox(monkeypatch, snap, contract, fake)
    monkeypatch.setattr(Sandbox, "start", _stop_after_install)
    side = orch._provision_side_sandbox("fakerev", 18098, snap / "unit", "t")

    assert side.provision.status == ProvisionStatus.START_FAILED
    assert side.provision.package_manager == "pnpm"
    assert side.provision.install_plan is not None
    assert side.provision.install_plan["corepack_version"] is None
    build = next(d for d in fake.descriptors if d["phase"] == "build")
    assert build["shell"] == (
        "export PATH=/checkout/node_modules/.bin:$PATH; pnpm --version"
    )


def test_sandbox_executor_runs_bun_plan(monkeypatch, tmp_path):
    """Plan 1.2e (D22): a bun plan runs in the sandbox (before
    1.2e this asserted the fail-closed refusal "not yet supported
    in the sandbox" — the behavior the plan line replaces). The
    install phase carries the plan's digest-pinned bootstrap and
    the frozen install through the persisted bun, and the
    contract steps render with .pd-tools/bin first on PATH."""
    from proofdeploy.runner import ProvisionStatus
    from proofdeploy.sandbox import Sandbox
    from proofdeploy.yml import RepoContract

    snap = tmp_path / "snap"
    snap.mkdir()
    (snap / "package.json").write_text(
        json.dumps({
            "name": "t",
            "packageManager": "bun@1.4.3",
            "engines": {"node": ">=20"},
        }) + "\n"
    )
    (snap / "bun.lock").write_text('{"lockfileVersion": 1}\n')
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
    side = orch._provision_side_sandbox("fakerev", 18094, snap / "unit", "t")

    # Reached the start phase (stubbed to fail there): the plan
    # was NOT refused.
    assert side.provision.status == ProvisionStatus.START_FAILED
    assert side.provision.package_manager == "bun"
    assert side.provision.install_plan is not None
    assert side.provision.install_plan["adapter"] == "node"
    assert side.provision.install_plan["bun_version"] == "1.4.3"
    installs = [d for d in fake.descriptors if d["phase"] == "install"]
    assert len(installs) == 2
    bootstrap, frozen = installs
    assert bootstrap["argv_rendered"][:2] == ["bash", "-c"]
    assert "bun-v1.4.3" in bootstrap["argv_rendered"][2]
    assert "sha256sum -c" in bootstrap["argv_rendered"][2]
    assert frozen["argv_rendered"] == [
        "/checkout/.pd-tools/bin/bun", "install", "--frozen-lockfile",
    ]
    build = next(d for d in fake.descriptors if d["phase"] == "build")
    assert build["shell"] == (
        "export PATH=/checkout/.pd-tools/bin:"
        "/checkout/node_modules/.bin:$PATH; node --check server.js"
    )


def test_sandbox_executor_refuses_ambiguous_workspace_plan(
    monkeypatch, tmp_path
):
    """Plan 1.2b: a workspace the planner refuses (here: two member
    apps and no contract app.path — the typed ambiguity refusal)
    still fails closed in the sandbox executor: BUILD_FAILED with
    the refusal as the reason, and no install step runs. (Before
    1.2b this fixture was a single root workspace refused as
    "workspaces not yet supported"; that shape is planned now, so
    the refused-plan subject is the ambiguous shape.)"""
    from proofdeploy.runner import ProvisionStatus
    from proofdeploy.yml import RepoContract

    snap = tmp_path / "snap"
    snap.mkdir()
    (snap / "package.json").write_text(
        json.dumps({"name": "t", "workspaces": ["packages/*"]}) + "\n"
    )
    (snap / "package-lock.json").write_text("{}\n")
    for name in ("app", "other"):
        member = snap / "packages" / name
        member.mkdir(parents=True)
        (member / "package.json").write_text(
            json.dumps({"name": name}) + "\n", encoding="utf-8"
        )
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
    assert side.provision.reason == "ambiguous app: 2 candidates"
    assert fake.descriptors == []


def test_sandbox_executor_quotes_workspace_member_path(
    monkeypatch, tmp_path
):
    """Plan 1.2b (reviewer fix): a workspace member's directory
    name is repo-controlled text, and the sandbox scopes its
    commands with a `cd` prefix built from it. An odd-but-legal
    name — a space and a $(...) fragment — still plans (quoting
    is the answer, not a name refusal); every issued sandbox
    command carries the path shlex-quoted as a single cd
    argument, while the executed records keep cwd as the raw
    relative path."""
    import shlex

    from proofdeploy.runner import ProvisionStatus
    from proofdeploy.sandbox import Sandbox
    from proofdeploy.yml import RepoContract

    member_rel = "apps/we ird;$(id)"
    snap = tmp_path / "snap"
    snap.mkdir()
    (snap / "package.json").write_text(
        json.dumps({"name": "t", "workspaces": ["apps/*"]}) + "\n"
    )
    (snap / "package-lock.json").write_text("{}\n")
    member = snap / "apps" / "we ird;$(id)"
    member.mkdir(parents=True)
    (member / "package.json").write_text(
        json.dumps({"name": "weird"}) + "\n", encoding="utf-8"
    )
    (member / "server.js").write_text("// app\n")
    contract = RepoContract(
        build="node --check server.js",
        start="node server.js",
        readiness="/health",
        env={},
    )
    fake = _FakeInstallSteps()
    orch = _stub_sandbox(monkeypatch, snap, contract, fake)
    monkeypatch.setattr(Sandbox, "start", _stop_after_install)
    side = orch._provision_side_sandbox("fakerev", 18096, snap / "unit", "t")

    # The plan accepted the odd name and scoped to that member.
    assert side.provision.status == ProvisionStatus.START_FAILED
    assert side.provision.install_plan is not None
    assert side.provision.install_plan["app_path"] == member_rel
    quoted = shlex.quote(f"/checkout/{member_rel}")
    # The scoped install-phase (build) command handed to the
    # sandbox carries the quoted path.
    build = next(d for d in fake.descriptors if d["phase"] == "build")
    assert build["command"].startswith(f"cd {quoted} && ")
    assert build["cwd"] == member_rel
    # The executed records: the start step's shell is the command
    # as issued (quoted cd prefix); every recorded cwd is the
    # raw relative path.
    executed = side.provision.executed_steps
    start_rec = next(e for e in executed if e["phase"] == "start")
    assert start_rec["shell"].startswith(f"cd {quoted} && ")
    assert start_rec["cwd"] == member_rel
    build_rec = next(e for e in executed if e["phase"] == "build")
    assert build_rec["cwd"] == member_rel


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


# ---------------------------------------------------------------------------
# Addendum 33: log capture (head + tail), secret-strip order in
# install_steps, corepack bootstrap on Node >= 25 rows, the canary
# scan's positive controls, and offline assertion replay (case 10).
# ---------------------------------------------------------------------------

sys.path.insert(0, str(Path(__file__).parent.parent / "conformance"))
import kit  # noqa: E402


def test_run_rc_keeps_head_and_tail_of_long_output(monkeypatch):
    """A failed step prints its real error FIRST, then pages of usage
    text: the old capture kept only the last 2000 chars and lost the
    error line entirely (the WO-7 PR 2 Node-install misdiagnosis).
    The capture must keep the head (first 40 lines) AND the tail,
    with an elision marker between them."""
    from proofdeploy import sandbox as sandbox_mod

    big = "npm error code EUSAGE\n" + "".join(
        f"filler line {i}\n" for i in range(5000)
    ) + "LASTLINE\n"

    def fake_run(args, capture_output, text, timeout):
        return SimpleNamespace(stdout=big, stderr="", returncode=1)

    monkeypatch.setattr(sandbox_mod.subprocess, "run", fake_run)
    logs: list[str] = []
    rc = sandbox_mod._run_rc(["podman", "run", "img"], 10, logs)
    assert rc == 1
    joined = "\n".join(logs)
    assert "npm error code EUSAGE" in joined
    assert "LASTLINE" in joined
    assert "lines elided" in joined


def test_install_steps_strips_secret_step_env(monkeypatch, tmp_path):
    """Secrets are stripped AFTER the per-step env is merged: a
    blocklisted key arriving via a step's own env must never reach
    the container's -e args, while legitimate step env survives."""
    from proofdeploy import sandbox as sandbox_mod
    from proofdeploy.sandbox import Sandbox, SandboxConfig

    captured: list[list[str]] = []

    def fake_run(args, timeout, logs):
        captured.append(list(args))
        return True, ""

    monkeypatch.setattr(sandbox_mod, "_run", fake_run)
    sb = Sandbox(SandboxConfig(image="img:tag", allow_no_userns=True))
    checkout = tmp_path / "co"
    checkout.mkdir()
    steps = [{
        "phase": "install",
        "command": "true",
        "env": {
            "GROQ_API_KEY": "shh-secret",
            "COREPACK_ENABLE_DOWNLOAD_PROMPT": "0",
        },
    }]
    results = sb.install_steps(checkout, steps, {}, {})
    assert results == [{"phase": "install", "exit_code": 0}]
    flat = [a for args in captured for a in args]
    assert not any("GROQ_API_KEY" in a for a in flat)
    assert "COREPACK_ENABLE_DOWNLOAD_PROMPT=0" in flat


def _install_steps(plan):
    return [s for s in plan.steps if s.phase == "install"]


def test_node26_pin_plan_uses_persisted_corepack(tmp_path):
    """The pinned corepack rendering is identical on every row
    (Addendum 35): a bootstrap step installs corepack@0.36.0 into
    the checkout's .pd-tools, and the install runs through the
    persisted binary — on the 26 row, whose image does not bundle
    corepack at all, exactly as on the 24 row."""
    snap = _node_snapshot(
        tmp_path,
        package_json={
            "name": "t",
            "packageManager": "pnpm@9.15.0",
            "engines": {"node": ">=26 <27"},
        },
        lockfiles={"pnpm-lock.yaml": "lockfileVersion: '9.0'\n"},
    )
    plan = plan_provisioning(snap, _contract())
    assert plan.refusal is None
    assert plan.selected_version == "26.11.1"
    assert plan.package_manager_version == "9.15.0"
    steps = _install_steps(plan)
    assert len(steps) == 2
    assert steps[0].argv == [
        "npm", "install", "--prefix", "{checkout}/.pd-tools",
        "--no-save", "--no-audit", "--no-fund", "corepack@0.36.0",
    ]
    assert steps[1].argv == [
        "{checkout}/.pd-tools/node_modules/.bin/corepack",
        "pnpm@9.15.0", "install", "--frozen-lockfile",
    ]
    assert plan.corepack_version == "0.36.0"
    assert ".pd-tools" in plan.persisted_dirs


def test_node24_pin_plan_same_persisted_corepack_no_global(tmp_path):
    """Same persisted rendering on the 24 row — and the -g mutant
    guard: no step may install globally, because a global install
    dies with the per-step --rm install container."""
    snap = _node_snapshot(
        tmp_path,
        package_json={
            "name": "t",
            "packageManager": "pnpm@9.15.0",
            "engines": {"node": ">=20 <25"},
        },
        lockfiles={"pnpm-lock.yaml": "lockfileVersion: '9.0'\n"},
    )
    plan = plan_provisioning(snap, _contract())
    assert plan.refusal is None
    assert plan.selected_version == "24.21.0"
    steps = _install_steps(plan)
    assert len(steps) == 2
    assert steps[0].argv == [
        "npm", "install", "--prefix", "{checkout}/.pd-tools",
        "--no-save", "--no-audit", "--no-fund", "corepack@0.36.0",
    ]
    assert steps[1].argv == [
        "{checkout}/.pd-tools/node_modules/.bin/corepack",
        "pnpm@9.15.0", "install", "--frozen-lockfile",
    ]
    for step in plan.steps:
        assert "-g" not in (step.argv or [])
        assert "--global" not in (step.argv or [])


def test_sandbox_install_failure_reason_names_first_error_line(
    monkeypatch, tmp_path
):
    """Addendum 33: the provision reason for a failed install names
    the install's first concrete error line — not just "sandbox
    install failed" — so the cause survives in the record."""
    from proofdeploy.runner import ProvisionStatus
    from proofdeploy.yml import RepoContract

    snap = tmp_path / "snap"
    snap.mkdir()
    (snap / "package.json").write_text(
        json.dumps({"name": "t"}) + "\n", encoding="utf-8"
    )
    (snap / "package-lock.json").write_text("{}\n", encoding="utf-8")
    (snap / "server.js").write_text("// app\n", encoding="utf-8")
    contract = RepoContract(
        build="node --check server.js",
        start="node server.js",
        readiness="/health",
        env={},
    )

    def failing_install(self, checkout, steps, env, proxy_env, timeout=600):
        blob = (
            "added 0 packages\n"
            "npm error code EUSAGE\n"
            "npm error some detail\n"
            "pages of usage text"
        )
        self.log.append(blob)
        # Addendum 38: model the real Sandbox.install_steps, which
        # captures the failed step's output (since Addendum 36).
        self.failed_step_output = blob
        return [{"phase": steps[0]["phase"], "exit_code": 1}]

    orch = _stub_sandbox(monkeypatch, snap, contract, failing_install)
    side = orch._provision_side_sandbox("fakerev", 18096, snap / "unit", "t")
    assert side.provision.status == ProvisionStatus.BUILD_FAILED
    assert side.provision.reason == (
        "sandbox install failed: npm error code EUSAGE"
    )


def test_first_error_line_markers_and_fallback():
    """Addendum 35: strong markers win, a generic Error: line is
    next, and an unmatched failure falls back to the last non-empty
    line of the output."""
    from proofdeploy.orchestrator import _first_error_line

    assert _first_error_line(
        ["noise\n  ERR_PNPM_OUTDATED_LOCKFILE x"]
    ) == "ERR_PNPM_OUTDATED_LOCKFILE x"
    assert _first_error_line(
        [".../better-sqlite3 install: gyp ERR! build error"]
    ) == ".../better-sqlite3 install: gyp ERR! build error"
    assert _first_error_line(
        ["npm error code EUSAGE", "Error: later generic"]
    ) == "npm error code EUSAGE"
    assert _first_error_line(
        ["Error: generic first", "nothing else"]
    ) == "Error: generic first"
    assert _first_error_line(
        ["plain output", "  ", "last nonempty line"]
    ) == "last nonempty line"
    assert _first_error_line(["", "  "]) is None


def test_first_error_line_skips_podman_echo():
    """Addendum 38: the `$ podman ...` command echo in the log is
    not scan evidence — a marker inside the echoed command must
    not be named, and the echo line must not serve as the
    fallback line."""
    from proofdeploy.orchestrator import _first_error_line

    assert _first_error_line(
        ["$ podman run --rm img bash -c 'npm error code EUSAGE'",
         "plain tail"]
    ) == "plain tail"
    assert _first_error_line(
        ["$ podman run --rm img true"]
    ) is None
    assert _first_error_line(
        ["$ podman run --rm img true", "Error: real one"]
    ) == "Error: real one"


def test_sandbox_install_failure_reason_without_step_output_names_the_step(
    monkeypatch, tmp_path
):
    """Addendum 38: with NO captured failed-step output, the
    provision reason does not scan the whole provision log — an
    unrelated line was named that way (the lockfile hash line in
    the tampered-hash run). It names the failed step itself:
    index, phase, exit code."""
    from proofdeploy.runner import ProvisionStatus
    from proofdeploy.yml import RepoContract

    snap = tmp_path / "snap"
    snap.mkdir()
    (snap / "package.json").write_text(
        json.dumps({"name": "t"}) + "\n", encoding="utf-8"
    )
    (snap / "package-lock.json").write_text("{}\n", encoding="utf-8")
    (snap / "server.js").write_text("// app\n", encoding="utf-8")
    contract = RepoContract(
        build="node --check server.js",
        start="node server.js",
        readiness="/health",
        env={},
    )

    def failing_install(self, checkout, steps, env, proxy_env, timeout=600):
        # No failed_step_output is captured; the provision log
        # carries an unrelated lockfile line the old whole-log
        # scan would have named.
        self.log.append(
            "lockfile: package-lock.json sha256: 3d021a12abcdef..."
        )
        return [{"phase": steps[0]["phase"], "exit_code": 7}]

    orch = _stub_sandbox(monkeypatch, snap, contract, failing_install)
    side = orch._provision_side_sandbox("fakerev", 18100, snap / "unit", "t")
    assert side.provision.status == ProvisionStatus.BUILD_FAILED
    assert side.provision.reason == (
        "sandbox install failed: install step 0 (install) exited 7"
    )


def test_sandbox_install_failure_reason_falls_back_to_last_line(
    monkeypatch, tmp_path
):
    """Addendum 35: when no known error marker appears in a failed
    install's output, the provision reason carries the output's
    last non-empty line instead of a bare 'sandbox install
    failed'."""
    from proofdeploy.runner import ProvisionStatus
    from proofdeploy.yml import RepoContract

    snap = tmp_path / "snap"
    snap.mkdir()
    (snap / "package.json").write_text(
        json.dumps({"name": "t"}) + "\n", encoding="utf-8"
    )
    (snap / "package-lock.json").write_text("{}\n", encoding="utf-8")
    (snap / "server.js").write_text("// app\n", encoding="utf-8")
    contract = RepoContract(
        build="node --check server.js",
        start="node server.js",
        readiness="/health",
        env={},
    )

    def failing_install(self, checkout, steps, env, proxy_env, timeout=600):
        blob = (
            "fetching packages\n"
            "some neutral output\n"
            "the final concrete line of the step"
        )
        self.log.append(blob)
        # Addendum 38: model the real Sandbox.install_steps, which
        # captures the failed step's output (since Addendum 36).
        self.failed_step_output = blob
        return [{"phase": steps[0]["phase"], "exit_code": 1}]

    orch = _stub_sandbox(monkeypatch, snap, contract, failing_install)
    side = orch._provision_side_sandbox("fakerev", 18097, snap / "unit", "t")
    assert side.provision.status == ProvisionStatus.BUILD_FAILED
    assert side.provision.reason == (
        "sandbox install failed: the final concrete line of the step"
    )


def test_sandbox_executed_steps_carry_the_real_failure_code(
    monkeypatch, tmp_path
):
    """Planted bug M9 (Addendum 36): executed_steps hard-coding
    exit_code 0. A stubbed install that fails a step with code 7
    must record 7 for that step — and the side is BUILD_FAILED."""
    from proofdeploy.runner import ProvisionStatus
    from proofdeploy.yml import RepoContract

    snap = tmp_path / "snap"
    snap.mkdir()
    (snap / "package.json").write_text(
        json.dumps({"name": "t"}) + "\n", encoding="utf-8"
    )
    (snap / "package-lock.json").write_text("{}\n", encoding="utf-8")
    (snap / "server.js").write_text("// app\n", encoding="utf-8")
    contract = RepoContract(
        build="node --check server.js",
        start="node server.js",
        readiness="/health",
        env={},
    )

    def failing_install(self, checkout, steps, env, proxy_env, timeout=600):
        return [{"phase": steps[0]["phase"], "exit_code": 7}]

    orch = _stub_sandbox(monkeypatch, snap, contract, failing_install)
    side = orch._provision_side_sandbox("fakerev", 18098, snap / "unit", "t")
    assert side.provision.status == ProvisionStatus.BUILD_FAILED
    install = next(
        e for e in side.provision.executed_steps if e["phase"] == "install"
    )
    assert install["exit_code"] == 7


def test_run_rc_keeps_head_and_tail_of_stderr(monkeypatch):
    """Planted bug M12 (Addendum 36): npm writes its errors to
    STDERR — a tail-only stderr capture loses the first error line
    behind the usage text even when stdout is captured whole. The
    head+tail capture applies to stderr specifically."""
    from proofdeploy import sandbox as sandbox_mod

    big_stderr = "npm error code EUSAGE\n" + "".join(
        f"filler line {i}\n" for i in range(5000)
    ) + "STDERR_LAST\n"

    def fake_run(args, capture_output, text, timeout):
        return SimpleNamespace(stdout="", stderr=big_stderr, returncode=1)

    monkeypatch.setattr(sandbox_mod.subprocess, "run", fake_run)
    logs: list[str] = []
    rc = sandbox_mod._run_rc(["podman", "run", "img"], 10, logs)
    assert rc == 1
    joined = "\n".join(logs)
    assert "npm error code EUSAGE" in joined
    assert "STDERR_LAST" in joined
    assert "lines elided" in joined


def test_run_rc_caps_total_output_for_giant_lines(monkeypatch):
    """Addendum 36: the capture caps TOTAL characters too — forty
    lines is few enough, but one 200k-char line must not come back
    whole."""
    from proofdeploy import sandbox as sandbox_mod

    big = "npm error code EUSAGE " + "x" * 200_000 + "\n"

    def fake_run(args, capture_output, text, timeout):
        return SimpleNamespace(stdout=big, stderr="", returncode=1)

    monkeypatch.setattr(sandbox_mod.subprocess, "run", fake_run)
    logs: list[str] = []
    rc = sandbox_mod._run_rc(["podman", "run", "img"], 10, logs)
    assert rc == 1
    joined = "\n".join(logs)
    assert "npm error code EUSAGE" in joined
    assert "lines elided" in joined
    assert len(joined) < 20_000


def test_install_failure_reason_scans_only_the_failed_step(
    monkeypatch, tmp_path
):
    """Addendum 36: the reason's error scan reads the FAILED STEP's
    own output — an error line from an earlier phase sharing the
    provision log must not be named."""
    from proofdeploy.runner import ProvisionStatus
    from proofdeploy.yml import RepoContract

    snap = tmp_path / "snap"
    snap.mkdir()
    (snap / "package.json").write_text(
        json.dumps({"name": "t"}) + "\n", encoding="utf-8"
    )
    (snap / "package-lock.json").write_text("{}\n", encoding="utf-8")
    (snap / "server.js").write_text("// app\n", encoding="utf-8")
    contract = RepoContract(
        build="node --check server.js",
        start="node server.js",
        readiness="/health",
        env={},
    )

    def failing_install(self, checkout, steps, env, proxy_env, timeout=600):
        self.log.append("npm error code EUSAGE from an earlier phase")
        self.failed_step_output = "compiling...\ngyp ERR! build error"
        return [{"phase": steps[0]["phase"], "exit_code": 1}]

    orch = _stub_sandbox(monkeypatch, snap, contract, failing_install)
    side = orch._provision_side_sandbox("fakerev", 18099, snap / "unit", "t")
    assert side.provision.status == ProvisionStatus.BUILD_FAILED
    assert side.provision.reason == (
        "sandbox install failed: gyp ERR! build error"
    )


def test_node_install_steps_carry_nodedir_env(tmp_path):
    """Addendum 37 (the reviewer will plant its removal): EVERY
    Node install step renders npm_config_nodedir=/usr/local in its
    env, so a native module with no prebuild compiles against the
    image's own headers — no download, no tarball extraction.
    Python install steps do NOT carry it."""
    import json as _json

    from proofdeploy.provisioning import plan_provisioning
    from proofdeploy.yml import RepoContract

    contract = RepoContract(
        build="node --check server.js",
        start="node server.js",
        readiness="/health",
        env={},
    )

    def snap(name, files):
        d = tmp_path / name
        d.mkdir()
        for fname, content in files.items():
            (d / fname).write_text(content, encoding="utf-8")
        return d

    pkg = _json.dumps({"name": "t"}) + "\n"
    cases = {
        "npm": {"package.json": pkg, "package-lock.json": "{}\n"},
        "pnpm": {
            "package.json": _json.dumps(
                {"name": "t", "packageManager": "pnpm@9.15.0"}
            ) + "\n",
            "pnpm-lock.yaml": "lockfileVersion: '9.0'\n",
        },
        "yarn-classic": {
            "package.json": pkg,
            "yarn.lock": "# yarn lockfile v1\n",
        },
        "yarn-berry": {
            "package.json": pkg,
            "yarn.lock": "__metadata:\n  version: 8\n",
        },
    }
    for name, files in cases.items():
        files = {**files, "server.js": "// app\n"}
        plan = plan_provisioning(snap(name, files), contract)
        assert plan.refusal is None, (name, plan.refusal)
        install = [s for s in plan.steps if s.phase == "install"]
        assert install, name
        for step in install:
            assert step.env.get("npm_config_nodedir") == "/usr/local", (
                name, step,
            )
    # Python: no nodedir anywhere.
    py = snap("py", {
        "requirements.txt": "flask==3.1.0\n",
        "app.py": "# app\n",
    })
    py_contract = RepoContract(
        build="", start="python app.py", readiness="/health", env={},
    )
    plan = plan_provisioning(py, py_contract)
    for step in plan.steps:
        assert "npm_config_nodedir" not in step.env


def test_canary_scan_positive_controls(tmp_path):
    """The kit scan fails on a REAL canary token or the canary env
    VALUE anywhere under the records dir, and stays silent on the
    golden apps' source-line form (the token prefix literal in code,
    which legitimately rides in the recorded prompt)."""
    records = tmp_path / "records"
    records.mkdir()
    (records / "runs.jsonl").write_text(
        'token = "pdcanary-" + secrets.token_hex(8)\n', encoding="utf-8"
    )
    assert kit.scan_records_for_canary(records, "pdcanary-env-zz42") == []
    (records / "runs.jsonl").write_text(
        '{"http_body": "{\\"token\\": \\"pdcanary-0123456789abcdef\\"}"}\n',
        encoding="utf-8",
    )
    assert kit.scan_records_for_canary(records, "pdcanary-env-zz42") != []
    (records / "runs.jsonl").write_text('{"clean": true}\n', encoding="utf-8")
    (records / "extra.json").write_text(
        '{"env": "pdcanary-env-zz42"}\n', encoding="utf-8"
    )
    assert kit.scan_records_for_canary(records, "pdcanary-env-zz42") != []


def _replay_evidence():
    probe = {
        "act": {"method": "GET", "path": "/notes/1"},
        "setup": [],
        "assert": [
            {"type": "status", "equals": 200},
            {"type": "body",
             "json_path": {"path": "done", "equals": False}},
        ],
    }
    parent = {
        "probe_index": 0, "verdict": "fail", "sent": True,
        "http_status": 200, "http_headers": {},
        "http_body": json.dumps({"done": True}), "reason": None,
        # Addendum 38: these model step-5-evaluated probes.
        "assertions_evaluated": True,
    }
    fix = {
        "probe_index": 0, "verdict": "pass", "sent": True,
        "http_status": 200, "http_headers": {},
        "http_body": json.dumps({"done": False}), "reason": None,
        "assertions_evaluated": True,
    }
    return {
        "record_type": "evidence", "run_kind": "bug",
        "parsed_probes": [probe],
        "fix_parent_results": [parent], "fix_results": [fix],
    }


def test_replay_rechecks_assertions_against_recorded_responses():
    """Case 10 (Addendum 33): replay re-evaluates each probe's
    assertions against the RECORDED response — it does not echo the
    stored verdicts. The untouched record replays to the stored
    verdict; a tampered recorded response changes the outcome even
    though the stored per-probe verdicts still claim the catch."""
    import copy

    evidence = _replay_evidence()
    assert kit.replay_verdict(evidence) == "candidate_catch"
    tampered = copy.deepcopy(evidence)
    tampered["fix_results"][0]["http_body"] = json.dumps({"done": True})
    # The stored verdicts are untouched by the tamper: a re-score
    # would still report the catch. The replay must not.
    assert tampered["fix_results"][0]["verdict"] == "pass"
    assert kit.replay_verdict(tampered) == "no_catch"


def test_sandbox_step_command_renders_export_form_for_every_branch():
    """Plan 1.2(m): every PATH-prepend branch of
    _sandbox_step_command renders `export PATH=<dirs>:$PATH; {cmd}`
    — corepack (pnpm/yarn), bun, plain node_modules and venv — with
    the contract command itself byte-untouched. The python-leading
    venv rewrite carries no PATH prepend and is unchanged."""
    from proofdeploy.orchestrator import _sandbox_step_command

    cmd = "cd packages/dev-server && bun run populate"
    corepack = (
        "export PATH=/checkout/.pd-tools/node_modules/.bin:"
        "/checkout/node_modules/.bin:$PATH; "
    )
    assert _sandbox_step_command(
        cmd, "node", "pnpm", corepack_version="0.36.0"
    ) == corepack + cmd
    assert _sandbox_step_command(
        cmd, "node", "yarn", corepack_version="0.36.0"
    ) == corepack + cmd
    assert _sandbox_step_command(cmd, "node", "bun") == (
        "export PATH=/checkout/.pd-tools/bin:"
        "/checkout/node_modules/.bin:$PATH; " + cmd
    )
    assert _sandbox_step_command(cmd, "node", "npm") == (
        "export PATH=/checkout/node_modules/.bin:$PATH; " + cmd
    )
    assert _sandbox_step_command(cmd, "node") == (
        "export PATH=/checkout/node_modules/.bin:$PATH; " + cmd
    )
    assert _sandbox_step_command("pytest -q", "python") == (
        "export PATH=/checkout/.venv/bin:$PATH; pytest -q"
    )
    assert _sandbox_step_command(
        "python manage.py migrate", "python"
    ) == "/checkout/.venv/bin/python manage.py migrate"


def test_sandbox_step_command_builtin_leading_command_runs(tmp_path):
    """Plan 1.2(m), defect 1: a contract command whose first token
    is a shell builtin must RUN. Vendure's seed/start begin with
    `cd ...`; under the old `env PATH=... {cmd}` rendering, env
    tried to exec the builtin and the step died with exit 127
    (`env: 'cd': No such file or directory`). The rendered command
    is executed exactly as the sandbox executes it (bash -c); the
    prepended /checkout dirs need not exist for the export to take
    effect, only the working directory does."""
    from proofdeploy.orchestrator import _sandbox_step_command

    (tmp_path / "sub").mkdir()
    for runtime, manager in (("node", "npm"), ("python", None)):
        rendered = _sandbox_step_command("cd sub && pwd", runtime, manager)
        result = subprocess.run(
            ["bash", "-c", rendered],
            cwd=tmp_path, capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == str(tmp_path.resolve() / "sub")


def test_sandbox_step_command_compound_command_keeps_prepended_path(
    tmp_path,
):
    """Plan 1.2(m), defect 2: in a compound contract command, the
    part after `&&` must still see the prepended dirs on PATH — that
    PATH is the lookup source for a tool named there (Vendure's
    `cd packages/dev-server && bun run populate`: the `bun` after
    `&&` resolves through it). printenv runs as the second simple
    command and reports the PATH in effect at exactly that point;
    under a rendering that sets PATH only on the first simple
    command (the `env` form, or a `PATH=... cd ...` prefix),
    printenv reports the caller's original PATH instead."""
    from proofdeploy.orchestrator import _sandbox_step_command

    (tmp_path / "sub").mkdir()
    rendered = _sandbox_step_command(
        "cd sub && printenv PATH", "node", "npm"
    )
    result = subprocess.run(
        ["bash", "-c", rendered],
        cwd=tmp_path, capture_output=True, text=True, timeout=30,
        env={"PATH": "/usr/bin:/bin"},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == (
        "/checkout/node_modules/.bin:/usr/bin:/bin"
    )
