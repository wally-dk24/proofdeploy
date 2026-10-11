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
    snap = _python_snapshot(tmp_path, requirements="flask==3.1.0\n")
    plan = plan_provisioning(snap, _contract())
    assert plan.refusal is None
    assert plan.declared_range is None
    assert plan.pip_require_hashes is False
    row = select_image("python", None)
    assert row is not None
    assert plan.selected_version == row.version


def test_python_plan_refuses_unfrozen_requirements(tmp_path):
    # Planted-bug guard (unfrozen install): a requirements line that is
    # not pinned with '==' must refuse the plan, never install loose.
    snap = _python_snapshot(tmp_path, requirements="flask>=3\n")
    plan = plan_provisioning(snap, _contract())
    assert plan.refusal is not None
    assert "not pinned" in plan.refusal


def test_python_plan_refuses_missing_lockfile(tmp_path):
    snap = _python_snapshot(tmp_path, pyproject=PYPROJECT_312)
    plan = plan_provisioning(snap, _contract())
    assert plan.refusal is not None
    assert "no frozen lockfile" in plan.refusal


def test_python_plan_unsatisfiable_range_is_an_image_refusal(tmp_path):
    # The plan itself is buildable on a host, but no pinned image
    # satisfies the declared range: the sandbox must fail closed with
    # the registered wording.
    pyproject = '[project]\nname = "t"\nrequires-python = "==3.9.*"\n'
    snap = _python_snapshot(
        tmp_path, pyproject=pyproject, requirements="flask==3.1.0\n"
    )
    plan = plan_provisioning(snap, _contract())
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
    install = next(s for s in plan.steps if s.phase == "install")
    argv = install.argv or []
    assert "--frozen-lockfile" in argv


def test_plan_provisioning_unknown_snapshot_raises(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ValueError):
        plan_provisioning(empty, _contract())
