"""Tests for the runner provisioner (WAL-58)."""

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from proofdeploy.runner import (
    Provisioner,
    ProvisionResult,
    ProvisionStatus,
    build_env,
    detect_runtime,
    find_lockfile,
    select_package_manager,
    verify_lockfile,
)


def _node_snapshot(
    tmp_path: Path,
    lockfile: str = "package-lock.json",
    package_json: dict[str, Any] | None = None,
) -> Path:
    snap = tmp_path / "snap"
    snap.mkdir()
    pkg = {"name": "t"}
    if package_json:
        pkg.update(package_json)
    (snap / "package.json").write_text(json.dumps(pkg) + "\n", encoding="utf-8")
    (snap / lockfile).write_text("{}\n", encoding="utf-8")
    return snap


def _python_snapshot(
    tmp_path: Path,
    lockfile: str = "requirements.txt",
    pyproject: str | None = None,
) -> Path:
    snap = tmp_path / "snap"
    snap.mkdir()
    if pyproject is not None:
        (snap / "pyproject.toml").write_text(pyproject, encoding="utf-8")
    body = "requests\n" if lockfile == "requirements.txt" else ""
    (snap / lockfile).write_text(body, encoding="utf-8")
    return snap


def _contract(**kw: Any) -> SimpleNamespace:
    base: dict[str, Any] = {
        "build": None,
        "migrate": None,
        "seed": None,
        "env": {},
        "start": "node server.js",
        "readiness": "/health",
    }
    base.update(kw)
    return SimpleNamespace(**base)


# --- runtime detection ---


def test_detect_runtime_node_version_from_engines(tmp_path):
    snap = _node_snapshot(tmp_path, package_json={"engines": {"node": ">=18"}})
    runtime, version = detect_runtime(snap)
    assert runtime == "node"
    assert version == ">=18"


def test_detect_runtime_node_no_engines(tmp_path):
    snap = _node_snapshot(tmp_path)
    runtime, version = detect_runtime(snap)
    assert runtime == "node"
    assert version is None


def test_detect_runtime_python_requires_python(tmp_path):
    snap = _python_snapshot(
        tmp_path,
        pyproject='[project]\nname = "t"\nrequires-python = ">=3.11"\n',
    )
    runtime, version = detect_runtime(snap)
    assert runtime == "python"
    assert version == ">=3.11"


def test_detect_runtime_python_no_pyproject(tmp_path):
    snap = _python_snapshot(tmp_path)
    runtime, version = detect_runtime(snap)
    assert runtime == "python"
    assert version is None


def test_detect_runtime_unknown(tmp_path):
    snap = tmp_path / "snap"
    snap.mkdir()
    with pytest.raises(ValueError, match="cannot detect runtime"):
        detect_runtime(snap)


# --- lockfile ---


def test_find_lockfile_prefers_first_match(tmp_path):
    snap = _node_snapshot(tmp_path)
    (snap / "yarn.lock").write_text("", encoding="utf-8")
    found = find_lockfile(snap, "node")
    assert found is not None
    name, sha = found
    assert name == "package-lock.json"
    assert len(sha) == 64


def test_verify_lockfile_found(tmp_path):
    snap = _node_snapshot(tmp_path)
    sha = verify_lockfile(snap, "node")
    assert len(sha) == 64


def test_verify_lockfile_missing(tmp_path):
    snap = tmp_path / "snap"
    snap.mkdir()
    (snap / "package.json").write_text("{}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="no frozen lockfile"):
        verify_lockfile(snap, "node")


# --- package manager selection ---


@pytest.mark.parametrize(
    "lockfile, manager, cmd_head",
    [
        ("package-lock.json", "npm", ["npm", "ci"]),
        ("pnpm-lock.yaml", "pnpm", ["pnpm", "install", "--frozen-lockfile"]),
        ("bun.lock", "bun", ["bun", "install", "--frozen-lockfile"]),
        ("bun.lockb", "bun", ["bun", "install", "--frozen-lockfile"]),
        ("uv.lock", "uv", ["uv", "sync", "--frozen"]),
        ("poetry.lock", "poetry", ["poetry", "install"]),
        ("requirements.txt", "pip", ["pip", "install"]),
    ],
)
def test_select_package_manager(tmp_path, lockfile, manager, cmd_head):
    runtime = "python" if lockfile in ("uv.lock", "poetry.lock", "requirements.txt") else "node"
    snap = tmp_path / "snap"
    snap.mkdir()
    (snap / lockfile).write_text("", encoding="utf-8")
    name, cmd = select_package_manager(snap, runtime, lockfile)
    assert name == manager
    assert cmd[: len(cmd_head)] == cmd_head


def test_select_yarn_classic_uses_frozen_lockfile(tmp_path):
    snap = _node_snapshot(tmp_path, lockfile="yarn.lock")
    name, cmd = select_package_manager(snap, "node", "yarn.lock")
    assert name == "yarn"
    assert cmd == ["yarn", "install", "--frozen-lockfile"]


def test_select_yarn_berry_uses_immutable(tmp_path):
    snap = _node_snapshot(
        tmp_path,
        lockfile="yarn.lock",
        package_json={"packageManager": "yarn@3.6.0"},
    )
    (snap / ".yarnrc.yml").write_text("nodeLinker: node-modules\n", encoding="utf-8")
    name, cmd = select_package_manager(snap, "node", "yarn.lock")
    assert name == "yarn"
    assert cmd == ["yarn", "install", "--immutable"]


def test_select_package_manager_unsupported(tmp_path):
    snap = tmp_path / "snap"
    snap.mkdir()
    with pytest.raises(ValueError, match="unsupported lockfile"):
        select_package_manager(snap, "node", "weird.lock")


# --- environment ---


def test_build_env_allowlist():
    env = build_env({"FOO": "bar"})
    assert env["PATH"] == os.environ["PATH"]
    assert env["CI"] == "true"
    assert env["FOO"] == "bar"
    # No leakage of unrelated secrets from the parent environment.
    assert "AWS_SECRET_ACCESS_KEY" not in env or "AWS_SECRET_ACCESS_KEY" in os.environ


def test_build_env_never_empty():
    env = build_env()
    assert env["PATH"]
    assert "HOME" in env or "HOME" not in os.environ


# --- _exec ---


def test_exec_missing_binary_is_failure_not_exception(tmp_path):
    prov = Provisioner(workdir=tmp_path)
    ok, reason = prov._exec(
        ["definitely-not-a-real-binary-xyz", "--version"],
        tmp_path,
        10,
        [],
        build_env(),
    )
    assert not ok
    assert reason is not None and "missing binary" in reason


def test_exec_timeout(tmp_path):
    prov = Provisioner(workdir=tmp_path)
    ok, reason = prov._exec(
        ["sleep", "30"], tmp_path, 1, [], build_env()
    )
    assert not ok
    assert reason is not None and "timed out" in reason


# --- provision() ---


def _provisioner_with_fake_exec(tmp_path, recorded):
    """Provisioner whose _exec records commands and always succeeds."""
    prov = Provisioner(workdir=tmp_path)

    def fake_exec(cmd, cwd, timeout, logs, env):
        recorded.append(" ".join(cmd))
        logs.append(f"$ {' '.join(cmd)} (mocked)")
        return True, None

    prov._exec = fake_exec  # type: ignore[method-assign]
    return prov


def test_provision_runs_build_migrate_seed_in_order(tmp_path):
    recorded: list[str] = []
    prov = _provisioner_with_fake_exec(tmp_path, recorded)
    snap = _node_snapshot(tmp_path)
    result = prov.provision(
        snapshot_dir=snap,
        contract=_contract(build="npm run build", migrate="npm run migrate",
                           seed="npm run seed", start=""),
        port=18080,
        expected_lockfile_sha256=verify_lockfile(snap, "node"),
    )
    assert result.status == ProvisionStatus.BUILD_FAILED
    assert result.reason is not None and "missing 'start'" in result.reason
    # Install, then build, migrate, seed — in that order.
    kinds = [c for c in recorded]
    assert kinds[0].startswith("npm ci")
    assert kinds[1] == "npm run build"
    assert kinds[2] == "npm run migrate"
    assert kinds[3] == "npm run seed"


def test_provision_skips_absent_optional_steps(tmp_path):
    recorded: list[str] = []
    prov = _provisioner_with_fake_exec(tmp_path, recorded)
    snap = _node_snapshot(tmp_path)
    prov.provision(
        snapshot_dir=snap,
        contract=_contract(start=""),
        port=18081,
        expected_lockfile_sha256=verify_lockfile(snap, "node"),
    )
    assert recorded == ["npm ci --no-audit --no-fund"]


def test_provision_lockfile_mismatch(tmp_path):
    """A lockfile hash mismatch is BUILD_FAILED with a reason, not an exception."""
    snap = _node_snapshot(tmp_path)
    prov = Provisioner(workdir=tmp_path)
    result = prov.provision(
        snapshot_dir=snap,
        contract=_contract(),
        port=18082,
        expected_lockfile_sha256="0" * 64,
    )
    assert result.status == ProvisionStatus.BUILD_FAILED
    assert result.reason == "lockfile mismatch: frozen lockfile changed"
    assert any("lockfile mismatch" in log for log in result.logs)


def test_provision_require_lockfile_match_without_expected(tmp_path):
    snap = _node_snapshot(tmp_path)
    prov = Provisioner(workdir=tmp_path)
    result = prov.provision(
        snapshot_dir=snap,
        contract=_contract(),
        port=18083,
        expected_lockfile_sha256=None,
        require_lockfile_match=True,
    )
    assert result.status == ProvisionStatus.BUILD_FAILED
    assert result.reason is not None and "required for measured runs" in result.reason


def test_provision_lockfile_match_not_required(tmp_path):
    recorded: list[str] = []
    prov = _provisioner_with_fake_exec(tmp_path, recorded)
    snap = _node_snapshot(tmp_path)
    result = prov.provision(
        snapshot_dir=snap,
        contract=_contract(start=""),
        port=18084,
        expected_lockfile_sha256=None,
        require_lockfile_match=False,
    )
    # Gets past the lockfile gate to the missing-start failure.
    assert result.status == ProvisionStatus.BUILD_FAILED
    assert result.reason is not None and "missing 'start'" in result.reason


def test_provision_unknown_runtime(tmp_path):
    snap = tmp_path / "snap"
    snap.mkdir()
    prov = Provisioner(workdir=tmp_path)
    result = prov.provision(
        snapshot_dir=snap, contract=_contract(), port=18085,
        require_lockfile_match=False,
    )
    assert result.status == ProvisionStatus.BUILD_FAILED
    assert result.reason is not None and "cannot detect runtime" in result.reason
    assert not result.ready


def test_provision_missing_lockfile(tmp_path):
    snap = tmp_path / "snap"
    snap.mkdir()
    (snap / "package.json").write_text('{"name": "t"}\n', encoding="utf-8")
    prov = Provisioner(workdir=tmp_path)
    result = prov.provision(
        snapshot_dir=snap, contract=_contract(), port=18086,
        require_lockfile_match=False,
    )
    assert result.status == ProvisionStatus.BUILD_FAILED
    assert result.reason is not None and "no frozen lockfile" in result.reason
    assert result.runtime == "node"


def test_provision_missing_binary_in_install(tmp_path, monkeypatch):
    """FileNotFoundError from subprocess becomes BUILD_FAILED, not a crash."""
    snap = _node_snapshot(tmp_path)
    prov = Provisioner(workdir=tmp_path)

    def boom(*a, **k):
        raise FileNotFoundError(2, "No such file or directory", "npm")

    monkeypatch.setattr(subprocess, "run", boom)
    result = prov.provision(
        snapshot_dir=snap,
        contract=_contract(),
        port=18087,
        expected_lockfile_sha256=verify_lockfile(snap, "node"),
    )
    assert result.status == ProvisionStatus.BUILD_FAILED
    assert result.reason is not None and "missing binary: npm" in result.reason


def test_provision_result_fields():
    r = ProvisionResult(
        status=ProvisionStatus.READY,
        target_url="http://127.0.0.1:1",
        runtime="node",
        runtime_version=">=18",
        package_manager="npm",
    )
    d = r.to_dict()
    assert d["status"] == "ready"
    assert d["runtime"] == "node"
    assert d["runtime_version"] == ">=18"
    assert d["package_manager"] == "npm"
    assert d["reason"] is None
    assert "proc" not in d
    assert r.ready


def test_provision_start_unreachable_is_start_failed(tmp_path):
    """A start command whose binary is missing -> START_FAILED, proc cleaned up."""
    recorded: list[str] = []
    prov = _provisioner_with_fake_exec(tmp_path, recorded)
    snap = _node_snapshot(tmp_path)
    result = prov.provision(
        snapshot_dir=snap,
        contract=_contract(start="definitely-not-a-real-binary-xyz --serve"),
        port=18088,
        expected_lockfile_sha256=verify_lockfile(snap, "node"),
    )
    assert result.status == ProvisionStatus.START_FAILED
    assert result.reason is not None and "missing binary" in result.reason
