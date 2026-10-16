"""Tests for the runner provisioner (WAL-58)."""

import json
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from proofdeploy.runner import (
    Provisioner,
    ProvisionResult,
    ProvisionStatus,
    _resolve_readiness,
    build_env,
    check_requirements_frozen,
    detect_runtime,
    find_lockfile,
    satisfies_range,
    select_package_manager,
    select_runtime_binary,
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


def _no_corepack(monkeypatch) -> None:
    """Make shutil.which report corepack as missing.

    select_package_manager routes through corepack when it is on PATH and
    packageManager pins a version, so tests asserting the plain command
    fail on machines with corepack installed. This keeps only the corepack
    lookup host-independent; all other binaries resolve normally.
    """
    real_which = shutil.which

    def fake_which(name, *args, **kwargs):
        if name == "corepack":
            return None
        return real_which(name, *args, **kwargs)

    monkeypatch.setattr(shutil, "which", fake_which)


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
    with pytest.raises(ValueError, match="not supported: no frozen lockfile"):
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
        ("poetry.lock", "poetry", ["poetry", "install", "--sync"]),
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


def test_select_yarn_berry_uses_immutable(tmp_path, monkeypatch):
    _no_corepack(monkeypatch)
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
    assert result.reason is not None and "not supported: no frozen lockfile" in result.reason
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
    # With every subprocess call failing, the effective-version re-probe
    # fails before install is reached. Either reason is BUILD_FAILED.
    assert result.reason is not None and (
        "missing binary" in result.reason or "not usable via step PATH" in result.reason
    )


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


# --- version range matching (finding 2) ---


@pytest.mark.parametrize(
    "version, spec, ecosystem, expected",
    [
        ("24.20.0", ">=18", "node", True),
        ("16.0.0", ">=18", "node", False),
        ("20.1.0", "^20.0.0", "node", True),
        ("21.0.0", "^20.0.0", "node", False),
        ("20.1.5", "~20.1.0", "node", True),
        ("20.5.3", "~20.1.0", "node", False),
        ("18.2.0", "18.x", "node", True),
        ("19.0.0", "18.x", "node", False),
        ("18.17.1", ">=16 <20", "node", True),
        ("3.12.3", ">=3.11", "python", True),
        ("3.10.0", ">=3.11", "python", False),
        ("3.12.3", ">=3.11,<3.13", "python", True),
        ("3.13.0", ">=3.11,<3.13", "python", False),
        ("3.11.9", "~=3.11", "python", True),
        ("4.0.0", "~=3.11", "python", False),
        ("3.11.2", "==3.11.*", "python", True),
        ("3.12.0", "==3.11.*", "python", False),
        ("24.20.0", "*", "node", True),
        ("24.20.0", "", "node", True),
        # npm bare partials are x-ranges (WO-8.4a): "22" is 22.x,
        # "22.2" is 22.2.x; a full x.y.z stays exact; the "*"
        # component spells the same wildcard as "x".
        ("22.23.3", "22", "node", True),
        ("23.0.0", "22", "node", False),
        ("22.2.9", "22.2", "node", True),
        ("22.3.0", "22.2", "node", False),
        ("22.23.3", "22.*", "node", True),
        ("22.14.0", "22.14.0", "node", True),
        ("22.23.3", "22.14.0", "node", False),
        # PEP 440 has no bare specifier: the Python side keeps the
        # exact reading of a bare version.
        ("3.12.15", "3.12", "python", False),
        ("3.12.0", "3.12", "python", True),
    ],
)
def test_satisfies_range(version, spec, ecosystem, expected):
    assert satisfies_range(version, spec, ecosystem) == expected


def test_select_runtime_binary_satisfies_declared_range(tmp_path):
    env = build_env()
    binary, actual, reason = select_runtime_binary("node", ">=18", env, [])
    assert reason is None
    assert binary is not None and actual is not None
    assert satisfies_range(actual, ">=18", "node")


def test_select_runtime_binary_unsatisfiable_range(tmp_path):
    env = build_env()
    binary, actual, reason = select_runtime_binary("node", ">=99", env, [])
    assert binary is None
    assert reason is not None and "no installed node satisfies" in reason


def test_provision_records_actual_runtime_version(tmp_path):
    recorded: list[str] = []
    prov = _provisioner_with_fake_exec(tmp_path, recorded)
    snap = _node_snapshot(tmp_path, package_json={"engines": {"node": ">=18"}})
    result = prov.provision(
        snapshot_dir=snap,
        contract=_contract(start=""),
        port=18090,
        expected_lockfile_sha256=verify_lockfile(snap, "node"),
    )
    assert result.status == ProvisionStatus.BUILD_FAILED  # missing start
    # Actual version recorded, not the declared range.
    assert result.runtime_version is not None
    assert result.runtime_version != ">=18"
    assert satisfies_range(result.runtime_version, ">=18", "node")


def test_provision_unsatisfiable_runtime_is_build_failed(tmp_path):
    prov = Provisioner(workdir=tmp_path)
    snap = _node_snapshot(tmp_path, package_json={"engines": {"node": ">=99"}})
    result = prov.provision(
        snapshot_dir=snap,
        contract=_contract(),
        port=18091,
        expected_lockfile_sha256=verify_lockfile(snap, "node"),
    )
    assert result.status == ProvisionStatus.BUILD_FAILED
    assert result.reason is not None and "no installed node satisfies" in result.reason


# --- lockfile preference via packageManager (finding 3) ---


def test_find_lockfile_prefers_package_manager(tmp_path):
    snap = _node_snapshot(
        tmp_path, package_json={"packageManager": "pnpm@9.1.0"}
    )
    (snap / "pnpm-lock.yaml").write_text("", encoding="utf-8")
    found = find_lockfile(snap, "node", preferred=["pnpm-lock.yaml"])
    assert found is not None
    assert found[0] == "pnpm-lock.yaml"


def test_provision_prefers_package_manager_lockfile(tmp_path, monkeypatch):
    _no_corepack(monkeypatch)
    recorded: list[str] = []
    prov = _provisioner_with_fake_exec(tmp_path, recorded)
    snap = _node_snapshot(
        tmp_path,
        lockfile="package-lock.json",
        package_json={"packageManager": "pnpm@9.1.0"},
    )
    (snap / "pnpm-lock.yaml").write_text("", encoding="utf-8")
    result = prov.provision(
        snapshot_dir=snap,
        contract=_contract(start=""),
        port=18092,
        expected_lockfile_sha256=find_lockfile(
            snap, "node", preferred=["pnpm-lock.yaml"]
        )[1],
    )
    assert result.package_manager == "pnpm"
    # WO-7 PR 2 + Addendum 35: a packageManager pin renders through
    # corepack at the exact pinned version — bootstrapped into the
    # checkout's .pd-tools (persisted) by the first step, then the
    # install runs through the persisted corepack binary. Plan
    # data, independent of the planning host's own corepack —
    # never a silent fall-back to whatever pnpm is on PATH.
    assert recorded[0].startswith("npm install --prefix ")
    assert "corepack@0.36.0" in recorded[0]
    assert ".pd-tools/node_modules/.bin/corepack pnpm@9.1.0 install" in (
        recorded[1]
    )
    assert "--frozen-lockfile" in recorded[1]


def test_provision_corepack_pinned_pnpm_manager_bin_first_on_path(
    tmp_path, monkeypatch
):
    """Plan 1.2(f), host executor: a corepack-pinned pnpm plan's
    contract steps run with the persisted corepack's manager bin
    dir (snap/.pd-tools/node_modules/.bin — the corepack package's
    own shims, which resolve the snapshot's packageManager pin)
    FIRST on PATH, so a step calling bare `pnpm` runs the plan's
    pinned version, never a PATH fallback."""
    _no_corepack(monkeypatch)
    recorded: list[str] = []
    envs: list[dict[str, str]] = []
    prov = Provisioner(workdir=tmp_path)

    def fake_exec(cmd, cwd, timeout, logs, env):
        recorded.append(" ".join(cmd))
        envs.append(dict(env))
        logs.append(f"$ {' '.join(cmd)} (mocked)")
        return True, None

    prov._exec = fake_exec  # type: ignore[method-assign]
    snap = _node_snapshot(
        tmp_path,
        lockfile="package-lock.json",
        package_json={
            "packageManager": "pnpm@9.1.0",
            "engines": {"node": ">=18"},
        },
    )
    (snap / "pnpm-lock.yaml").write_text("", encoding="utf-8")
    result = prov.provision(
        snapshot_dir=snap,
        contract=_contract(build="pnpm --version", start=""),
        port=18097,
        expected_lockfile_sha256=find_lockfile(
            snap, "node", preferred=["pnpm-lock.yaml"]
        )[1],
    )
    assert result.package_manager == "pnpm"
    assert result.status == ProvisionStatus.BUILD_FAILED  # missing start
    idx = recorded.index("pnpm --version")
    manager_bin = str(snap / ".pd-tools" / "node_modules" / ".bin")
    assert envs[idx]["PATH"].startswith(manager_bin + os.pathsep)


# --- frozen requirements.txt (finding 4) ---


def test_check_requirements_frozen_pinned_ok(tmp_path):
    # REG-A (WO-8.1): "frozen" means a COMPLETE lock — every spec
    # entry carries --hash (the pip-compile form, hashes on
    # continuation lines). An ==-only file is not complete; see
    # tests/test_wo81.py for the ==-only case.
    req = tmp_path / "requirements.txt"
    req.write_text(
        "# comment\n"
        "requests==2.32.3 \\\n"
        "    --hash=sha256:1111111111111111111111111111111111111111111111111111111111111111\n"
        "urllib3==2.2.0 ; python_version > '3.8' \\\n"
        "    --hash=sha256:2222222222222222222222222222222222222222222222222222222222222222\n",
        encoding="utf-8",
    )
    assert check_requirements_frozen(req) is None


def test_check_requirements_frozen_unpinned_fails(tmp_path):
    req = tmp_path / "requirements.txt"
    req.write_text("requests\n", encoding="utf-8")
    reason = check_requirements_frozen(req)
    assert reason is not None and "no --hash" in reason


def test_provision_unpinned_requirements_is_build_failed(tmp_path):
    # REG-A (WO-8.1): an unpinned requirements file goes through
    # lock generation, which is refused without the unit's cutoff —
    # a BUILD_FAILED, never a loose install.
    snap = _python_snapshot(tmp_path)  # body is "requests\n" (unpinned)
    prov = Provisioner(workdir=tmp_path)
    result = prov.provision(
        snapshot_dir=snap,
        contract=_contract(),
        port=18093,
        expected_lockfile_sha256=verify_lockfile(snap, "python"),
    )
    assert result.status == ProvisionStatus.BUILD_FAILED
    assert result.reason is not None and "cutoff" in result.reason


# --- fresh venv per run (finding 5) ---


def test_venvs_are_per_run(tmp_path):
    seen_envs: list[dict[str, str]] = []

    def fake_exec(cmd, cwd, timeout, logs, env):
        seen_envs.append(dict(env))
        logs.append(f"$ {' '.join(cmd)} (mocked)")
        # REG-A: the mocked resolver produces the generated lock
        # its --output-file names, like the real uv compile step.
        if "--output-file" in cmd:
            out = Path(cmd[cmd.index("--output-file") + 1])
            out.write_text(
                "requests==2.32.3 \\\n    --hash=sha256:abc\n",
                encoding="utf-8",
            )
        return True, None

    prov = Provisioner(workdir=tmp_path)
    prov._exec = fake_exec  # type: ignore[method-assign]
    snap = tmp_path / "snap"
    snap.mkdir()
    (snap / "pyproject.toml").write_text(
        '[project]\nname = "t"\nrequires-python = ">=3.10"\n', encoding="utf-8"
    )
    (snap / "requirements.txt").write_text("requests==2.32.3\n", encoding="utf-8")
    sha = verify_lockfile(snap, "python")
    r1 = prov.provision(
        snapshot_dir=snap, contract=_contract(start=""),
        port=18094, expected_lockfile_sha256=sha,
        lock_cutoff="2026-01-02T03:04:05Z",
    )
    r2 = prov.provision(
        snapshot_dir=snap, contract=_contract(start=""),
        port=18095, expected_lockfile_sha256=sha,
        lock_cutoff="2026-01-02T03:04:05Z",
    )
    assert r1.status == ProvisionStatus.BUILD_FAILED  # missing start
    assert r2.status == ProvisionStatus.BUILD_FAILED
    # The venv-creation call uses the probe env; all later calls (install,
    # steps) must run with a per-run venv bin dir first on PATH.
    venv_paths = [
        e["PATH"].split(os.pathsep)[0]
        for e in seen_envs
        if "venvs" in e["PATH"].split(os.pathsep)[0]
    ]
    assert venv_paths, "no step ran with a venv on PATH"
    assert all(p.endswith("bin") for p in venv_paths)
    assert len(set(venv_paths)) >= 2, "both runs must use distinct venvs"


# --- readiness resolution (finding 6) ---


def test_resolve_readiness_path_joins_port():
    assert _resolve_readiness("/health", 18096, []) == "http://127.0.0.1:18096/health"


def test_resolve_readiness_empty_falls_back():
    assert _resolve_readiness("", 18096, []) == "http://127.0.0.1:18096"


def test_resolve_readiness_full_url_warns_on_port_mismatch():
    logs: list[str] = []
    url = _resolve_readiness("http://127.0.0.1:9999/health", 18096, logs)
    assert url == "http://127.0.0.1:9999/health"
    assert any("port" in log for log in logs)


# --- target log capture (finding 7) ---


def test_start_failure_captures_target_log(tmp_path):
    recorded: list[str] = []
    prov = _provisioner_with_fake_exec(tmp_path, recorded)
    snap = _node_snapshot(tmp_path)
    result = prov.provision(
        snapshot_dir=snap,
        contract=_contract(start="definitely-not-a-real-binary-xyz --serve"),
        port=18097,
        expected_lockfile_sha256=verify_lockfile(snap, "node"),
    )
    assert result.status == ProvisionStatus.START_FAILED
    assert result.target_log_path is not None
    assert Path(result.target_log_path).is_file()
    assert "target_log_path" in result.to_dict()


# --- blocker fix: selected runtime must actually be used ---


def _mock_node_tree(tmp_path: Path, monkeypatch) -> Path:
    """Create fake node/node24 binaries with different versions.

    Returns a bin dir with:
    - `node` reporting v22.22.0 (the "default" that must NOT be used)
    - `node24` reporting v99.1.0 (uniquely satisfies `engines: >=99`;
      no real system binary reports v99)
    - `npm`/`npx` shims alongside node24
    Prepends the bin dir to PATH via monkeypatch.
    """
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    (bindir / "node").write_text(
        '#!/bin/sh\nif [ "$1" = "--version" ]; then echo "v22.22.0"; fi\n',
        encoding="utf-8",
    )
    (bindir / "node24").write_text(
        '#!/bin/sh\nif [ "$1" = "--version" ]; then echo "v99.1.0"; fi\n',
        encoding="utf-8",
    )
    for tool in ("npm", "npx"):
        (bindir / tool).write_text('#!/bin/sh\necho "mock"\n', encoding="utf-8")
    for f in bindir.iterdir():
        f.chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ.get("PATH", ""))
    return bindir


def test_selected_node_version_is_actually_used(tmp_path, monkeypatch):
    """Blocker: with engines >=99 and only mock node24 satisfying it, the
    recorded version must be v99.1.0 (from node24), proving the re-probe
    ran through the pinned PATH."""
    _mock_node_tree(tmp_path, monkeypatch)
    recorded: list[str] = []
    prov = _provisioner_with_fake_exec(tmp_path, recorded)
    snap = _node_snapshot(tmp_path, package_json={"engines": {"node": ">=99"}})
    result = prov.provision(
        snapshot_dir=snap,
        contract=_contract(start=""),  # missing start -> BUILD_FAILED after version probe
        port=18098,
        expected_lockfile_sha256=verify_lockfile(snap, "node"),
    )
    assert result.status == ProvisionStatus.BUILD_FAILED
    assert result.runtime_version == "v99.1.0", (
        f"expected v99.1.0 from mock node24, got {result.runtime_version!r}"
    )


def test_run_bin_pins_node_npm_npx(tmp_path, monkeypatch):
    """The per-run bin/ dir symlinks node/npm/npx to the selected install."""
    bindir = _mock_node_tree(tmp_path, monkeypatch)
    prov = Provisioner(workdir=tmp_path)
    logs: list[str] = []
    env = build_env()
    run_bin = prov._make_run_bin("node24", "testrun123", logs, env)
    assert run_bin is not None
    assert (run_bin / "node").is_symlink()
    assert (run_bin / "npm").is_symlink()
    # The node symlink resolves to our mock node24, not the default node.
    resolved = os.readlink(run_bin / "node")
    assert resolved == str(bindir / "node24"), f"node -> {resolved}"


def test_effective_version_comes_from_step_path(tmp_path, monkeypatch):
    """_effective_runtime_version resolves via the given PATH."""
    _mock_node_tree(tmp_path, monkeypatch)
    prov = Provisioner(workdir=tmp_path)
    logs: list[str] = []
    # Default PATH: mock node (v22) is first.
    env = build_env()
    assert prov._effective_runtime_version("node", env, logs) == "v22.22.0"
    # Pinned PATH: run bin with node24 (v99) first.
    run_bin = prov._make_run_bin("node24", "testrun456", logs, env)
    assert run_bin is not None
    pinned_env = build_env(prepend_path=str(run_bin))
    assert prov._effective_runtime_version("node", pinned_env, logs) == "v99.1.0"


def test_failed_provision_cleans_up_scratch_dirs(tmp_path):
    """Minor: venvs/<run_id> and runs/<run_id> are deleted on failure."""
    recorded: list[str] = []
    prov = Provisioner(workdir=tmp_path)

    def fake_exec_with_venv(cmd, cwd, timeout, logs, env):
        recorded.append(" ".join(cmd))
        # Actually create the venv dir so cleanup has something to delete.
        if cmd[:3] == ["python3", "-m", "venv"]:
            Path(cmd[3]).mkdir(parents=True, exist_ok=True)
        logs.append(f"$ {' '.join(cmd)} (mocked)")
        return True, None

    prov._exec = fake_exec_with_venv  # type: ignore[method-assign]
    snap = _python_snapshot(
        tmp_path, pyproject='[project]\nname = "t"\nrequires-python = ">=3.10"\n'
    )
    (snap / "requirements.txt").write_text("requests==2.32.3\n", encoding="utf-8")
    sha = verify_lockfile(snap, "python")
    result = prov.provision(
        snapshot_dir=snap,
        contract=_contract(start=""),  # missing start -> BUILD_FAILED after venv
        port=18099,
        expected_lockfile_sha256=sha,
    )
    assert result.status == ProvisionStatus.BUILD_FAILED
    assert result.run_id is not None
    venv_dir = tmp_path / "venvs" / result.run_id
    # The venv was created (mock made the dir), then cleaned up on failure.
    assert not venv_dir.exists(), f"venv not cleaned up: {venv_dir}"
    assert not (tmp_path / "runs" / result.run_id).exists(), "runs dir not cleaned up"


def test_cleanup_run_is_idempotent(tmp_path):
    prov = Provisioner(workdir=tmp_path)
    prov.cleanup_run("nonexistent-id", [])  # must not raise
    (tmp_path / "venvs" / "abc").mkdir(parents=True)
    (tmp_path / "runs" / "abc").mkdir(parents=True)
    logs: list[str] = []
    prov.cleanup_run("abc", logs)
    assert not (tmp_path / "venvs" / "abc").exists()
    assert not (tmp_path / "runs" / "abc").exists()
    assert len(logs) == 2


def test_cleanup_run_routes_userns_remapped_tree_through_unshare(
    tmp_path, monkeypatch
):
    """Plan 1.2(g) follow-up: a run dir holding foreign-owned entries
    (content written under the sandbox's userns mapping — on the host
    it belongs to the mapped runtime uid, in directories the host
    user does not own) cannot be removed by a host rmtree: unlink
    needs write on the containing directory. cleanup_run must route
    it through `podman unshare rm -rf`, the namespace coordinates of
    the ownership transfer. Simulated at the house seams: the
    ownership probe reports the remap, the host rmtree leaves the
    tree standing (its EPERMs are what ignore_errors used to
    swallow), and the fake podman's unshare rm removes it the way
    in-namespace root would."""
    prov = Provisioner(workdir=tmp_path)
    d = tmp_path / "venvs" / "abc"
    d.mkdir(parents=True)
    (d / "written-by-the-runtime-uid.txt").write_text("x\n", encoding="utf-8")

    monkeypatch.setattr(
        "proofdeploy.runner._tree_has_foreign_owner", lambda p: True
    )
    real_rmtree = shutil.rmtree
    monkeypatch.setattr(
        shutil, "rmtree", lambda p, **kw: None  # host EPERM, swallowed
    )
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        if "unshare" in cmd:
            real_rmtree(cmd[-1], ignore_errors=True)
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(f"unexpected subprocess call: {cmd}")

    monkeypatch.setattr(subprocess, "run", fake_run)
    logs: list[str] = []
    prov.cleanup_run("abc", logs)
    assert not d.exists(), "userns-remapped tree survived cleanup_run"
    unshare_calls = [c for c in calls if "unshare" in c]
    assert len(unshare_calls) == 1, calls
    call = unshare_calls[0]
    assert call[0] == "podman"
    assert call[call.index("unshare") + 1 : call.index("unshare") + 3] == [
        "rm",
        "-rf",
    ]
    assert call[-1] == str(d)
    assert logs == [f"cleaned up: {d}"]


def test_cleanup_run_logs_leftovers_when_removal_fails(tmp_path, monkeypatch):
    """Never silent: when neither the unshare route nor the host
    rmtree can remove a tree, cleanup_run enumerates what is left
    behind in the log instead of the old ignore_errors shrug."""
    prov = Provisioner(workdir=tmp_path)
    d = tmp_path / "venvs" / "abc"
    d.mkdir(parents=True)
    (d / "stuck.txt").write_text("x\n", encoding="utf-8")

    monkeypatch.setattr(
        "proofdeploy.runner._tree_has_foreign_owner", lambda p: True
    )
    monkeypatch.setattr(shutil, "rmtree", lambda p, **kw: None)

    def failing_run(cmd, **kwargs):
        return SimpleNamespace(
            returncode=1, stdout="", stderr="unshare blew up"
        )

    monkeypatch.setattr(subprocess, "run", failing_run)
    logs: list[str] = []
    prov.cleanup_run("abc", logs)
    assert d.exists()
    assert not any(line.startswith("cleaned up:") for line in logs), logs
    incomplete = [line for line in logs if line.startswith("cleanup incomplete:")]
    assert len(incomplete) == 1, logs
    assert str(d) in incomplete[0] and "stuck.txt" in incomplete[0], logs


def test_cleanup_run_plain_tree_never_touches_podman(tmp_path, monkeypatch):
    """The fallback: a tree owned by the invoking user (no remap)
    is removed by rmtree exactly as before, with no podman call."""
    prov = Provisioner(workdir=tmp_path)
    d = tmp_path / "venvs" / "abc"
    d.mkdir(parents=True)
    (d / "plain.txt").write_text("x\n", encoding="utf-8")
    calls: list[list[str]] = []

    def recording_run(cmd, **kwargs):
        calls.append(list(cmd))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", recording_run)
    logs: list[str] = []
    prov.cleanup_run("abc", logs)
    assert not d.exists()
    assert calls == [], calls
    assert logs == [f"cleaned up: {d}"]
