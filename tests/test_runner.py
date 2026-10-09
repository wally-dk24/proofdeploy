"""Tests for the runner provisioner (WAL-58)."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from proofdeploy.runner import (
    Provisioner,
    ProvisionResult,
    ProvisionStatus,
    detect_runtime,
    verify_lockfile,
)


def _node_snapshot(tmp_path: Path) -> Path:
    snap = tmp_path / "snap"
    snap.mkdir()
    (snap / "package.json").write_text('{"name": "t"}\n', encoding="utf-8")
    (snap / "package-lock.json").write_text("{}\n", encoding="utf-8")
    return snap


def _python_snapshot(tmp_path: Path) -> Path:
    snap = tmp_path / "snap"
    snap.mkdir()
    (snap / "requirements.txt").write_text("requests\n", encoding="utf-8")
    return snap


def test_detect_runtime_node(tmp_path):
    assert detect_runtime(_node_snapshot(tmp_path)) == "node"


def test_detect_runtime_python(tmp_path):
    assert detect_runtime(_python_snapshot(tmp_path)) == "python"


def test_detect_runtime_unknown(tmp_path):
    snap = tmp_path / "snap"
    snap.mkdir()
    with pytest.raises(ValueError, match="cannot detect runtime"):
        detect_runtime(snap)


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


def test_provision_lockfile_mismatch(tmp_path):
    """A lockfile hash mismatch is BUILD_FAILED, not an exception."""
    snap = _node_snapshot(tmp_path)
    contract = SimpleNamespace(migrate=None, start="node server.js", readiness="/health")
    prov = Provisioner(workdir=tmp_path)
    result = prov.provision(
        snapshot_dir=snap,
        contract=contract,
        port=18080,
        expected_lockfile_sha256="0" * 64,
    )
    assert result.status == ProvisionStatus.BUILD_FAILED
    assert any("lockfile mismatch" in log for log in result.logs)


def test_provision_unknown_runtime(tmp_path):
    snap = tmp_path / "snap"
    snap.mkdir()
    contract = SimpleNamespace(migrate=None, start="x", readiness="/")
    prov = Provisioner(workdir=tmp_path)
    result = prov.provision(snapshot_dir=snap, contract=contract, port=18081)
    assert result.status == ProvisionStatus.BUILD_FAILED
    assert not result.ready


def test_provision_missing_start(tmp_path):
    snap = _node_snapshot(tmp_path)
    contract = SimpleNamespace(migrate=None, start="", readiness="/")
    prov = Provisioner(workdir=tmp_path)
    # Skip install by monkeypatching _run to succeed instantly
    import proofdeploy.runner as runner

    orig_run = runner._run

    def fake_run(cmd, cwd, timeout, logs):
        from subprocess import CompletedProcess

        logs.append(f"$ {' '.join(cmd)} (mocked)")
        return CompletedProcess(cmd, 0, stdout="", stderr="")

    runner._run = fake_run
    try:
        result = prov.provision(snapshot_dir=snap, contract=contract, port=18082)
    finally:
        runner._run = orig_run
    assert result.status == ProvisionStatus.BUILD_FAILED
    assert any("missing 'start'" in log for log in result.logs)


def test_provision_result_to_dict():
    r = ProvisionResult(status=ProvisionStatus.READY, target_url="http://127.0.0.1:1")
    d = r.to_dict()
    assert d["status"] == "ready"
    assert d["target_url"] == "http://127.0.0.1:1"
    assert r.ready
