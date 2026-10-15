"""WO-7 PR 1: the provisioning interface, adapters, and the pinned
image table.

Provisioning used to live twice: the host Provisioner (runner.py) and
the sandbox path (Orchestrator._provision_side_sandbox) each carried
their own lockfile/install logic, and they diverged once (S1). These
tests pin the unified design:

- a ProvisioningAdapter plans (pure data, never runs a command);
- the InstallPlan is serializable and carries everything an executor
  needs, OR a typed refusal instead of a plan;
- images.lock.json is the one committed table of digest-pinned
  bookworm images; selection is the highest table version satisfying
  the declared range.

Every test here fails on the pre-WO-7 code (proofdeploy.provisioning
does not exist there) and passes on the branch.
"""
import hashlib
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from proofdeploy.provisioning import (
    InstallPlan,
    NodeAdapter,
    PythonAdapter,
    detect_workspace,
    get_adapter,
    load_images,
    plan_provisioning,
    select_image,
)


def _contract(**kw):
    base = {"build": None, "migrate": None, "seed": None,
            "start": "python app.py", "env": {}}
    base.update(kw)
    return SimpleNamespace(**base)


def _python_snapshot(tmp_path: Path, *, pyproject: str | None = None,
                     requirements: str | None = None) -> Path:
    snap = tmp_path / "snap"
    snap.mkdir()
    if pyproject is not None:
        (snap / "pyproject.toml").write_text(pyproject, encoding="utf-8")
    if requirements is not None:
        (snap / "requirements.txt").write_text(requirements,
                                               encoding="utf-8")
    return snap


HASHED_REQ = (
    "six==1.17.0 \\\n"
    "    --hash=sha256:ff703d0fc3e08526949441584fca3fc209d20cf45a3f54c0fb84f79a80d0d95b1\n"
)

PYPROJECT_312 = '[project]\nname = "t"\nrequires-python = ">=3.12,<3.13"\n'


# ---------------------------------------------------------------------------
# The pinned image table.
# ---------------------------------------------------------------------------


def test_images_table_is_committed_and_digest_pinned():
    rows = load_images()
    assert rows, "images.lock.json must not be empty"
    for row in rows:
        assert row.runtime in ("python", "node")
        # Full official bookworm images only (build tools for native
        # modules), replacing the old -slim / floating tags.
        assert row.ref.startswith("docker.io/library/")
        assert row.ref.endswith("-bookworm"), row.ref
        assert f":{row.version}-bookworm" in row.ref, row.ref
        # The digest is pinned, and the ref an executor runs carries it.
        assert row.digest.startswith("sha256:")
        assert len(row.digest) == len("sha256:") + 64
        assert row.pinned_ref == f"{row.ref}@{row.digest}"


def test_images_table_covers_declared_ranges_and_current_releases():
    rows = load_images()
    python_versions = [r.version for r in rows if r.runtime == "python"]
    node_versions = [r.version for r in rows if r.runtime == "node"]
    # The eval/dev/held-out sets declare Python lines 3.10-3.13 and
    # Node lines 18/20/22; the current releases (Python 3.14, Node 24+)
    # must be covered too.
    for line in ("3.10", "3.11", "3.12", "3.13", "3.14"):
        assert any(v.startswith(line + ".") for v in python_versions), line
    for line in ("18", "20", "22", "24"):
        assert any(v.startswith(line + ".") for v in node_versions), line


def test_select_image_picks_highest_satisfying_version():
    row = select_image("python", ">=3.12,<3.13")
    assert row is not None
    assert row.version.startswith("3.12.")
    # ">=3.11" must select the highest table row overall, not the
    # first satisfying one.
    row = select_image("python", ">=3.11")
    assert row is not None
    assert row.version == max(
        r.version for r in load_images() if r.runtime == "python"
    )
    row = select_image("node", "^22")
    assert row is not None
    assert row.version.startswith("22.")


def test_select_image_no_range_uses_default_line():
    # No declared range keeps the legacy default lines (the old
    # SANDBOX_IMAGES pins): Python 3.12, Node 22.
    row = select_image("python", None)
    assert row is not None and row.version.startswith("3.12.")
    row = select_image("node", None)
    assert row is not None and row.version.startswith("22.")


def test_select_image_unsatisfiable_returns_none():
    assert select_image("python", "==3.9.*") is None
    assert select_image("node", ">=99") is None


def test_select_image_bare_major_is_an_x_range():
    # WO-8.4a: in npm semver a bare "22" is the range 22.x, not the
    # exact version 22.0.0. Reading it as exact refused every
    # Directus side (engines {"node": "22"}) against the pinned
    # 22.23.3 image. The bare form must select the same row as the
    # spelled-out ranges for the same line.
    row22 = select_image("node", "22")
    assert row22 is not None
    assert row22.version == "22.23.3"  # the table's only 22 line row
    for spec in ("22.x", "22.*", "^22", ">=22 <23"):
        assert select_image("node", spec) == row22, spec
    # Not a 22 special case: another bare major selects its own line.
    row20 = select_image("node", "20")
    assert row20 is not None and row20.version.startswith("20.")


def test_select_image_bare_partial_and_exact_forms():
    # A bare minor partial is the matching minor range: "22.23"
    # means 22.23.x (satisfied by the pinned 22.23.3), while "22.2"
    # means 22.2.x, which no pinned row satisfies.
    row = select_image("node", "22.23")
    assert row is not None and row.version == "22.23.3"
    assert select_image("node", "22.2") is None
    # A full x.y.z stays exact: a different patch is not a match,
    # the pinned patch itself is.
    assert select_image("node", "22.14.0") is None
    row = select_image("node", "22.23.3")
    assert row is not None and row.version == "22.23.3"
    # A bare major with no pinned line stays unsatisfiable.
    assert select_image("node", "23") is None


# ---------------------------------------------------------------------------
# The Python adapter's plan.
# ---------------------------------------------------------------------------


def test_python_plan_for_hashed_requirements(tmp_path):
    snap = _python_snapshot(
        tmp_path, pyproject=PYPROJECT_312, requirements=HASHED_REQ
    )
    plan = plan_provisioning(snap, _contract(build="python -m py_compile app.py"))
    assert plan.refusal is None
    assert plan.adapter == "python"
    assert plan.runtime == "python"
    assert plan.declared_range == ">=3.12,<3.13"
    # Runtime selection: highest pinned image satisfying the range,
    # digest-pinned.
    row = select_image("python", ">=3.12,<3.13")
    assert row is not None
    assert plan.selected_version == row.version
    assert plan.image_ref == row.pinned_ref
    assert plan.image_digest == row.digest
    assert plan.image_refusal is None
    # Lockfile facts.
    assert plan.lockfile_name == "requirements.txt"
    assert plan.lockfile_sha256 == hashlib.sha256(
        HASHED_REQ.encode()
    ).hexdigest()
    assert plan.package_manager == "pip"
    # The venv lives in the checkout (persisted across install/start).
    assert plan.persisted_dirs == [".venv"]
    # Ordered steps: venv, install, then the contract steps, then start.
    phases = [s.phase for s in plan.steps]
    assert phases[0] == "venv"
    assert "install" in phases
    assert phases[-1] == "start"
    install = next(s for s in plan.steps if s.phase == "install")
    argv = " ".join(install.argv or [])
    assert "pip" in argv and "requirements.txt" in argv
    # The frozen-install facts are in the plan for the executors to
    # render: hashes present in the requirements file.
    assert plan.pip_require_hashes is True


def test_python_plan_without_range_or_hashes(tmp_path):
    # REG-A (WO-8.1): an ==-only requirements file is NOT a complete
    # lock — it goes through generation, which needs the unit's
    # cutoff. Without one the plan refuses (fail closed); with one
    # it is a generated plan installing the generated lock with
    # hashes required.
    snap = _python_snapshot(tmp_path, requirements="flask==3.1.0\n")
    plan = plan_provisioning(snap, _contract())
    assert plan.refusal is not None
    assert "cutoff" in plan.refusal
    plan = plan_provisioning(
        snap, _contract(), lock_cutoff="2026-01-02T03:04:05Z"
    )
    assert plan.refusal is None
    assert plan.declared_range is None
    assert plan.lock_source == "generated"
    # The install consumes the GENERATED lock (which carries hashes),
    # not the ==-only source file.
    assert plan.pip_require_hashes is True
    row = select_image("python", None)
    assert row is not None
    assert plan.selected_version == row.version


def test_python_plan_refuses_unfrozen_requirements(tmp_path):
    # REG-A (WO-8.1): an unpinned requirements line is a declared
    # dependency, resolved through generation — never installed
    # loose. Without the unit's cutoff the plan refuses.
    snap = _python_snapshot(tmp_path, requirements="flask>=3\n")
    plan = plan_provisioning(snap, _contract())
    assert plan.refusal is not None
    assert "cutoff" in plan.refusal
    plan = plan_provisioning(
        snap, _contract(), lock_cutoff="2026-01-02T03:04:05Z"
    )
    assert plan.refusal is None
    assert plan.lock_source == "generated"


def test_python_plan_refuses_missing_lockfile(tmp_path):
    # REG-A (WO-8.1): a bare pyproject.toml is not a lock either —
    # its declared dependencies go through generation (cutoff
    # required, as for a requirements file).
    snap = _python_snapshot(tmp_path, pyproject=PYPROJECT_312)
    plan = plan_provisioning(snap, _contract())
    assert plan.refusal is not None
    assert "cutoff" in plan.refusal
    plan = plan_provisioning(
        snap, _contract(), lock_cutoff="2026-01-02T03:04:05Z"
    )
    assert plan.refusal is None
    assert plan.lock_source == "generated"
    assert plan.lockfile_name == "pyproject.toml"


def test_python_plan_unsatisfiable_range_is_an_image_refusal(tmp_path):
    # The plan itself is buildable on a host, but no pinned image
    # satisfies the declared range: the sandbox must fail closed with
    # the registered wording.
    pyproject = '[project]\nname = "t"\nrequires-python = "==3.9.*"\n'
    snap = _python_snapshot(
        tmp_path, pyproject=pyproject, requirements="flask==3.1.0\n"
    )
    # REG-A: the ==-only requirements goes through generation, so
    # the plan needs the unit's cutoff to build at all.
    plan = plan_provisioning(
        snap, _contract(), lock_cutoff="2026-01-02T03:04:05Z"
    )
    assert plan.refusal is None
    assert plan.image_ref is None
    assert plan.image_digest is None
    assert plan.image_refusal == (
        "declared range ==3.9.* not satisfiable by pinned images"
    )


def test_python_plan_uv_lockfile_plans_uv(tmp_path):
    # uv.lock is a valid plan (the host executor runs it); refusing it
    # in the SANDBOX is the sandbox executor's job, not the plan's.
    snap = _python_snapshot(tmp_path, pyproject=PYPROJECT_312)
    (snap / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    plan = plan_provisioning(snap, _contract())
    assert plan.refusal is None
    assert plan.lockfile_name == "uv.lock"
    assert plan.package_manager == "uv"


def test_adapter_plan_never_runs_a_command(tmp_path, monkeypatch):
    # The adapter is pure: planning must not execute anything.
    def _boom(*a, **k):
        raise AssertionError("adapter executed a command")

    monkeypatch.setattr(subprocess, "run", _boom)
    monkeypatch.setattr(subprocess, "Popen", _boom)
    snap = _python_snapshot(
        tmp_path, pyproject=PYPROJECT_312, requirements=HASHED_REQ
    )
    plan = plan_provisioning(snap, _contract())
    assert plan.refusal is None


def test_plan_is_serializable_with_stable_sha256(tmp_path):
    snap = _python_snapshot(
        tmp_path, pyproject=PYPROJECT_312, requirements=HASHED_REQ
    )
    plan = plan_provisioning(snap, _contract())
    data = plan.to_dict()
    # Round-trips through JSON unchanged.
    assert json.loads(json.dumps(data)) == data
    # The recorded hash is over the plan data and is stable.
    assert plan.sha256() == plan_provisioning(snap, _contract()).sha256()
    assert plan.sha256() == hashlib.sha256(
        json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def test_detect_reports_runtime_and_reason(tmp_path):
    adapter = get_adapter("python")
    snap = _python_snapshot(
        tmp_path, pyproject=PYPROJECT_312, requirements=HASHED_REQ
    )
    det = adapter.detect(snap)
    assert det is not None
    assert det.runtime == "python"
    assert det.declared_range == ">=3.12,<3.13"
    assert det.reason  # the detection explains itself
    empty = tmp_path / "empty"
    empty.mkdir()
    assert adapter.detect(empty) is None


# ---------------------------------------------------------------------------
# The Node adapter's plan (host path; sandbox Node is PR 2).
# ---------------------------------------------------------------------------


def _node_snapshot(tmp_path: Path, *, package_json: dict,
                   lockfile: str = "package-lock.json") -> Path:
    snap = tmp_path / "snap"
    snap.mkdir()
    (snap / "package.json").write_text(
        json.dumps(package_json) + "\n", encoding="utf-8"
    )
    (snap / lockfile).write_text("{}\n", encoding="utf-8")
    return snap


def test_node_plan_npm_ci(tmp_path):
    snap = _node_snapshot(
        tmp_path,
        package_json={"name": "t", "engines": {"node": ">=20"}},
    )
    plan = plan_provisioning(snap, _contract(start="node server.js"))
    assert plan.refusal is None
    assert plan.adapter == "node"
    assert plan.runtime == "node"
    assert plan.declared_range == ">=20"
    assert plan.lockfile_name == "package-lock.json"
    assert plan.package_manager == "npm"
    install = next(s for s in plan.steps if s.phase == "install")
    assert install.argv is not None
    assert install.argv[:2] == ["npm", "ci"]
    # node_modules lives in the checkout.
    assert plan.persisted_dirs == ["node_modules"]


def test_node_plan_package_manager_version_recorded(tmp_path):
    # The pinned packageManager version is part of the plan (the
    # reviewer plants "packageManager ignored").
    snap = _node_snapshot(
        tmp_path,
        package_json={"name": "t", "packageManager": "pnpm@9.1.0"},
        lockfile="pnpm-lock.yaml",
    )
    plan = plan_provisioning(snap, _contract(start="node server.js"))
    assert plan.refusal is None
    assert plan.package_manager == "pnpm"
    assert plan.package_manager_version == "9.1.0"
    install = [s for s in plan.steps if s.phase == "install"]
    # Select the frozen step BY CONTENT (Addendum 37), never by
    # position: it is the step whose argv runs the pinned manager
    # frozen. (Position lied: since Addendum 35 the FIRST install
    # step is the .pd-tools corepack bootstrap.)
    frozen = next(
        s for s in install
        if f"pnpm@{plan.package_manager_version}" in (s.argv or [])
        and "--frozen-lockfile" in (s.argv or [])
    )
    argv = frozen.argv or []
    assert "--frozen-lockfile" in argv
    # The .pd-tools bootstrap step (the one installing a
    # `corepack@<version>` spec) PRECEDES the frozen step.
    bootstrap_idx = next(
        i for i, s in enumerate(install)
        if any(a.startswith("corepack@") for a in (s.argv or []))
    )
    assert bootstrap_idx < install.index(frozen)


def test_plan_provisioning_unknown_snapshot_raises(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ValueError):
        plan_provisioning(empty, _contract())


# ---------------------------------------------------------------------------
# Workspaces / monorepos (plan 1.2b): the frozen install runs at the
# workspace root; the contract steps are scoped to the resolved app
# member; ambiguity and unsupported managers are typed refusals.
# ---------------------------------------------------------------------------


def _workspace_snapshot(
    tmp_path: Path,
    *,
    members: tuple[str, ...] = ("packages/app",),
    manager: str = "pnpm",
    root_extra: dict | None = None,
    lockfile: bool = True,
) -> Path:
    """A Node workspace snapshot: root + member package dirs."""
    snap = tmp_path / "snap"
    snap.mkdir()
    root_pkg: dict = {
        "name": "ws-root",
        "private": True,
        "engines": {"node": ">=20 <25"},
    }
    if manager == "pnpm":
        root_pkg["packageManager"] = "pnpm@9.15.0"
        (snap / "pnpm-workspace.yaml").write_text(
            "packages:\n  - 'packages/*'\n", encoding="utf-8"
        )
        if lockfile:
            (snap / "pnpm-lock.yaml").write_text(
                "lockfileVersion: '9.0'\n", encoding="utf-8"
            )
    elif manager == "npm":
        root_pkg["workspaces"] = ["packages/*"]
        if lockfile:
            (snap / "package-lock.json").write_text("{}", encoding="utf-8")
    elif manager == "yarn":
        root_pkg["workspaces"] = ["packages/*"]
        if lockfile:
            (snap / "yarn.lock").write_text("# yarn lockfile v1\n",
                                            encoding="utf-8")
    if root_extra:
        root_pkg.update(root_extra)
    (snap / "package.json").write_text(json.dumps(root_pkg), encoding="utf-8")
    for member in members:
        d = snap / member
        d.mkdir(parents=True)
        (d / "package.json").write_text(
            json.dumps({"name": member.replace("/", "-")}),
            encoding="utf-8",
        )
    return snap


def _ws_contract(**kw):
    return _contract(
        build="npm run build", start="node server.js", **kw
    )


def test_detect_workspace_from_pnpm_yaml(tmp_path):
    snap = _workspace_snapshot(
        tmp_path, members=("packages/lib", "packages/app")
    )
    layout = detect_workspace(snap)
    assert layout is not None
    assert layout.source == "pnpm-workspace.yaml"
    assert layout.members == ["packages/app", "packages/lib"]


def test_detect_workspace_from_package_json_forms(tmp_path):
    snap = _workspace_snapshot(tmp_path, manager="npm")
    layout = detect_workspace(snap)
    assert layout is not None
    assert layout.source == "package.json"
    assert layout.members == ["packages/app"]
    # The mapping form ({packages: [...]}) declares the same globs.
    pkg = json.loads((snap / "package.json").read_text(encoding="utf-8"))
    pkg["workspaces"] = {"packages": ["packages/*"], "nohoist": []}
    (snap / "package.json").write_text(json.dumps(pkg), encoding="utf-8")
    layout = detect_workspace(snap)
    assert layout is not None
    assert layout.members == ["packages/app"]


def test_detect_workspace_applies_exclusions(tmp_path):
    snap = _workspace_snapshot(
        tmp_path, members=("packages/app", "packages/excluded")
    )
    (snap / "pnpm-workspace.yaml").write_text(
        "packages:\n  - 'packages/*'\n  - '!packages/excluded'\n",
        encoding="utf-8",
    )
    layout = detect_workspace(snap)
    assert layout is not None
    assert layout.members == ["packages/app"]


def test_pnpm_workspace_plan_scopes_contract_steps(tmp_path):
    snap = _workspace_snapshot(
        tmp_path, members=("packages/app", "packages/lib")
    )
    plan = NodeAdapter().plan(snap, _ws_contract(app_path="packages/app"))
    assert plan.refusal is None
    assert plan.workspace_root == "."
    assert plan.app_path == "packages/app"
    assert plan.package_manager == "pnpm"
    assert plan.lockfile_name == "pnpm-lock.yaml"
    installs = [s for s in plan.steps if s.phase == "install"]
    assert installs and all(s.cwd is None for s in installs)
    frozen = [
        s for s in installs if s.argv and "--frozen-lockfile" in s.argv
    ]
    assert frozen, [s.argv for s in installs]
    scoped = {s.phase: s.cwd for s in plan.steps if s.shell}
    assert scoped == {"build": "packages/app", "start": "packages/app"}
    assert "packages/app/node_modules" in plan.persisted_dirs
    assert "node_modules" in plan.persisted_dirs


def test_workspace_single_member_needs_no_declaration(tmp_path):
    snap = _workspace_snapshot(tmp_path, members=("packages/app",))
    plan = NodeAdapter().plan(snap, _ws_contract())
    assert plan.refusal is None
    assert plan.app_path == "packages/app"
    start = next(s for s in plan.steps if s.phase == "start")
    assert start.cwd == "packages/app"


def test_workspace_two_members_no_declaration_is_ambiguous(tmp_path):
    snap = _workspace_snapshot(
        tmp_path, members=("packages/app", "packages/other")
    )
    plan = NodeAdapter().plan(snap, _ws_contract())
    assert plan.refusal == "ambiguous app: 2 candidates"
    assert plan.lockfile_name is None
    assert plan.steps == []


def test_workspace_declared_path_must_be_a_member(tmp_path):
    snap = _workspace_snapshot(tmp_path, members=("packages/app",))
    plan = NodeAdapter().plan(snap, _ws_contract(app_path="packages/nope"))
    assert plan.refusal is not None
    assert plan.refusal.startswith(
        "app.path 'packages/nope' is not a workspace package"
    )


def test_workspace_no_members_scopes_to_root(tmp_path):
    snap = _workspace_snapshot(tmp_path, members=())
    (snap / "pnpm-workspace.yaml").write_text(
        "packages: []\n", encoding="utf-8"
    )
    plan = NodeAdapter().plan(snap, _ws_contract())
    assert plan.refusal is None
    assert plan.app_path == "."
    assert all(s.cwd is None for s in plan.steps)


def test_npm_workspace_plan(tmp_path):
    snap = _workspace_snapshot(tmp_path, manager="npm")
    plan = NodeAdapter().plan(snap, _ws_contract())
    assert plan.refusal is None
    assert plan.package_manager == "npm"
    assert plan.app_path == "packages/app"
    installs = [s for s in plan.steps if s.phase == "install"]
    assert any(
        s.argv and s.argv[:2] == ["npm", "ci"] for s in installs
    )
    start = next(s for s in plan.steps if s.phase == "start")
    assert start.cwd == "packages/app"


def test_yarn_workspace_is_a_typed_refusal(tmp_path):
    snap = _workspace_snapshot(tmp_path, manager="yarn")
    plan = NodeAdapter().plan(snap, _ws_contract())
    assert plan.refusal == (
        "workspaces not yet supported for package manager 'yarn'"
    )


def test_workspace_without_lockfile_refuses_at_root(tmp_path):
    snap = _workspace_snapshot(tmp_path, lockfile=False)
    plan = NodeAdapter().plan(snap, _ws_contract(app_path="packages/app"))
    assert plan.refusal == (
        "no frozen lockfile found in snapshot for runtime 'node'"
    )


def test_python_plan_workspace_is_a_typed_refusal(tmp_path):
    snap = _python_snapshot(
        tmp_path, pyproject='[project]\nname = "x"\nrequires-python = ">=3.12"\n'
    )
    plan = PythonAdapter().plan_workspace(snap, _contract())
    assert plan.refusal == (
        "workspaces not yet supported for runtime 'python'"
    )
    assert plan.steps == []


def test_plan_provisioning_routes_a_workspace_snapshot(tmp_path):
    snap = _workspace_snapshot(
        tmp_path, members=("packages/app", "packages/lib")
    )
    plan = plan_provisioning(snap, _ws_contract(app_path="packages/lib"))
    assert plan.refusal is None
    assert plan.app_path == "packages/lib"


def test_workspace_plan_round_trips_through_dict(tmp_path):
    snap = _workspace_snapshot(tmp_path)
    plan = NodeAdapter().plan(snap, _ws_contract())
    again = InstallPlan.from_dict(plan.to_dict())
    assert again.to_dict() == plan.to_dict()
    assert again.app_path == "packages/app"
    start = next(s for s in again.steps if s.phase == "start")
    assert start.cwd == "packages/app"


def test_non_workspace_plan_carries_no_workspace_fields(tmp_path):
    snap = _node_snapshot(
        tmp_path,
        package_json={"name": "plain", "engines": {"node": ">=20 <25"}},
    )
    (snap / "package-lock.json").write_text("{}", encoding="utf-8")
    plan = NodeAdapter().plan(snap, _ws_contract())
    assert plan.refusal is None
    assert plan.workspace_root is None
    assert plan.app_path is None
    assert all(s.cwd is None for s in plan.steps)


# ---------------------------------------------------------------------------
# install.npm_flags (plan 1.2h): the contract's allowlisted flags
# render into the frozen npm ci argv and are recorded on the plan.
# ---------------------------------------------------------------------------


def test_node_plan_npm_flags_render_into_ci_argv(tmp_path):
    snap = _node_snapshot(
        tmp_path,
        package_json={"name": "t", "engines": {"node": ">=20"}},
    )
    plan = plan_provisioning(
        snap,
        _contract(start="node server.js", npm_flags=["--legacy-peer-deps"]),
    )
    assert plan.refusal is None
    assert plan.package_manager == "npm"
    assert plan.npm_flags == ["--legacy-peer-deps"]
    install = next(s for s in plan.steps if s.phase == "install")
    assert install.argv == [
        "npm", "ci", "--legacy-peer-deps", "--no-audit", "--no-fund",
    ]


def test_node_plan_no_npm_flags_by_default(tmp_path):
    snap = _node_snapshot(
        tmp_path,
        package_json={"name": "t", "engines": {"node": ">=20"}},
    )
    plan = plan_provisioning(snap, _contract(start="node server.js"))
    assert plan.npm_flags == []
    install = next(s for s in plan.steps if s.phase == "install")
    assert install.argv is not None
    assert "--legacy-peer-deps" not in install.argv


def test_node_plan_npm_flags_refused_for_pnpm(tmp_path):
    # Declared npm flags on a pnpm plan are a typed refusal, never
    # a silent no-op.
    snap = _node_snapshot(
        tmp_path,
        package_json={"name": "t", "packageManager": "pnpm@9.1.0"},
        lockfile="pnpm-lock.yaml",
    )
    plan = plan_provisioning(
        snap,
        _contract(start="node server.js", npm_flags=["--legacy-peer-deps"]),
    )
    assert plan.refusal is not None
    assert "install.npm_flags applies to npm plans only" in plan.refusal
    assert plan.steps == []


# ---------------------------------------------------------------------------
# REG-D (plan 1.2i): the {regd_cutoff} placeholder in a contract
# step renders at plan time with the unit's REG-A lock cutoff.
# ---------------------------------------------------------------------------

REGD_BUILD = (
    "npm --prefix sample/01-cats-app install "
    "--legacy-peer-deps --before={regd_cutoff}"
)
REGD_CUTOFF = "2025-06-07T08:09:10Z"


def _regd_contract(**kw):
    return _contract(build=REGD_BUILD, start="node server.js", **kw)


def test_regd_plan_renders_cutoff_and_records_facts(tmp_path):
    snap = _node_snapshot(
        tmp_path,
        package_json={"name": "t", "engines": {"node": ">=20"}},
    )
    plan = plan_provisioning(
        snap, _regd_contract(), lock_cutoff=REGD_CUTOFF
    )
    assert plan.refusal is None
    build = next(s for s in plan.steps if s.phase == "build")
    assert build.shell == (
        "npm --prefix sample/01-cats-app install "
        f"--legacy-peer-deps --before={REGD_CUTOFF}"
    )
    assert plan.regd == {
        "sample_path": "sample/01-cats-app",
        "cutoff": REGD_CUTOFF,
        "command": (
            "npm --prefix sample/01-cats-app install "
            f"--legacy-peer-deps --before={REGD_CUTOFF}"
        ),
    }


def test_regd_both_sides_render_the_same_cutoff(tmp_path):
    # REG-D D3: both sides of a unit share the run's one cutoff,
    # so their rendered sample commands are identical. The cutoff
    # reaches the plan as the lazy callable the orchestrator
    # supplies for a measured unit.
    shells = []
    for name in ("parent", "fix"):
        side_dir = tmp_path / name
        side_dir.mkdir()
        snap = _node_snapshot(
            side_dir,
            package_json={"name": "t", "engines": {"node": ">=20"}},
        )
        plan = plan_provisioning(
            snap,
            _regd_contract(),
            lock_cutoff=lambda: REGD_CUTOFF,
        )
        assert plan.refusal is None
        assert plan.regd is not None
        assert plan.regd["cutoff"] == REGD_CUTOFF
        shells.append(
            next(s for s in plan.steps if s.phase == "build").shell
        )
    assert shells[0] == shells[1]
    assert "{regd_cutoff}" not in shells[0]


def test_regd_no_cutoff_is_a_typed_refusal(tmp_path):
    snap = _node_snapshot(
        tmp_path,
        package_json={"name": "t", "engines": {"node": ">=20"}},
    )
    plan = plan_provisioning(snap, _regd_contract())
    assert plan.refusal is not None
    assert plan.refusal.startswith("REG-D:")
    assert "undated sample install" in plan.refusal
    assert plan.steps == []
    assert plan.regd is None


def test_regd_non_iso_cutoff_is_a_typed_refusal(tmp_path):
    # The cutoff lands in a shell string: anything that is not an
    # ISO 8601 timestamp is refused, never rendered.
    snap = _node_snapshot(
        tmp_path,
        package_json={"name": "t", "engines": {"node": ">=20"}},
    )
    plan = plan_provisioning(
        snap, _regd_contract(), lock_cutoff="2025-06-07; rm -rf /"
    )
    assert plan.refusal is not None
    assert plan.refusal.startswith("REG-D:")
    assert plan.steps == []


def test_regd_absent_leaves_plan_untouched(tmp_path):
    snap = _node_snapshot(
        tmp_path,
        package_json={"name": "t", "engines": {"node": ">=20"}},
    )
    plan = plan_provisioning(snap, _contract(start="node server.js"))
    assert plan.regd is None
