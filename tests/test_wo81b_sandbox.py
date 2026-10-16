"""WO-8.1b sandbox variants: the python golden fixtures with a
committed uv.lock / poetry.lock / pdm.lock reach READY in the
sandbox, and the stale-lock variant is BUILD_FAILED with the
tool's first error line in the reason.

Each fixture (tests/fixtures/wo81b/<manager>/) is the same stdlib
app whose only dependency is six==1.17.0, with a lock generated
by the REAL pinned manager (uv 0.13.0, poetry 2.5.1, pdm 2.29.2)
at fixture-creation time. The provision runs the plan end to end
in the sandbox install container: the app venv in the home, the manager
bootstrapped into /pd-home/.pd-tools BY HASH from its committed
bootstrap requirements file, the freshness check (uv/pdm), the
frozen install into /pd-home/.venv, then the app on the venv.

Runs in the sandbox-tests CI job (PD_REQUIRE_SANDBOX), like the
WO-8.1 generated-lock variant. Fail-before: on main every one of
these provisions is BUILD_FAILED "not yet supported in the
sandbox" before any install step runs.
"""
import hashlib
import os
import shutil
import socket
import subprocess
from pathlib import Path

import pytest

from proofdeploy.orchestrator import MeasureConfig, Orchestrator

pytestmark = pytest.mark.skipif(
    not os.environ.get("PD_REQUIRE_SANDBOX"),
    reason="manager-lock variants run in the sandbox-tests CI job",
)

FIXTURES = Path(__file__).parent / "fixtures" / "wo81b"

PINNED = {
    "uv": ("0.13.0", "uv.lock"),
    "poetry": ("2.5.1", "poetry.lock"),
    "pdm": ("2.29.2", "pdm.lock"),
}

FROZEN_TOKENS = {
    "uv": ("sync", "--frozen"),
    "poetry": ("install", "--sync"),
    "pdm": ("sync",),
}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True, capture_output=True, text=True, timeout=120,
    ).stdout.strip()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _provision_variant(tmp_path: Path, variant: str):
    repo = tmp_path / "repo"
    shutil.copytree(FIXTURES / variant, repo)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "wo81b-variant")
    _git(repo, "config", "user.email", "wo81b@proofdeploy.invalid")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", f"wo81b {variant} variant")
    rev = _git(repo, "rev-parse", "HEAD")

    cfg = MeasureConfig(
        repo_dir=repo,
        output_dir=tmp_path / "records",
        workdir=tmp_path / "work",
        no_skill=True,
        sandbox=True,
    )
    orch = Orchestrator(cfg)
    unit = tmp_path / "unit"
    unit.mkdir()
    side = orch._provision_side_sandbox(
        rev, _free_port(), unit, f"wo81b-{variant}"
    )
    return side.provision


@pytest.mark.parametrize("manager", ["uv", "poetry", "pdm"])
def test_manager_lock_variant_ready_in_sandbox(tmp_path, manager):
    from proofdeploy import provisioning

    provision = _provision_variant(tmp_path, manager)
    try:
        assert provision.ready, "\n".join(provision.logs)
        plan = provision.install_plan
        assert plan is not None
        assert plan["lock_source"] == "committed"
        assert plan["lockfile_name"] == PINNED[manager][1]
        assert plan["package_manager"] == manager
        assert plan["package_manager_version"] == PINNED[manager][0]
        # The recorded bootstrap sha256 is the committed file's.
        file_name = provisioning._LOCK_MANAGERS[manager][0]
        committed = (
            Path(provisioning.__file__).parent / file_name
        ).read_bytes()
        assert plan["manager_bootstrap_sha256"] == hashlib.sha256(
            committed
        ).hexdigest()
        # Every install step ran clean, and the last one is the
        # manager's frozen install from the bootstrapped binary.
        installs = [
            s for s in provision.executed_steps
            if s["phase"] == "install"
        ]
        assert installs and all(
            s["exit_code"] == 0 for s in installs
        ), installs
        frozen = installs[-1]["argv_rendered"]
        assert frozen[0] == f"/pd-home/.pd-tools/bin/{manager}"
        for token in FROZEN_TOKENS[manager]:
            assert token in frozen, frozen
        # The bootstrap install was hash-checked.
        assert any(
            "--require-hashes" in (s.get("argv_rendered") or [])
            for s in installs
        ), installs
    finally:
        if provision.proc is not None:
            provision.proc.stop()


def test_stale_uv_lock_variant_build_failed_in_sandbox(tmp_path):
    from proofdeploy.runner import ProvisionStatus

    provision = _provision_variant(tmp_path, "uv-mismatch")
    try:
        assert provision.status == ProvisionStatus.BUILD_FAILED, (
            "\n".join(provision.logs)
        )
        # uv's own first error line, carried in the reason — not
        # the trailing hint line, not a bare "install failed".
        assert provision.reason is not None
        assert (
            "error: The lockfile at `uv.lock` needs to be updated"
            in provision.reason
        ), provision.reason
    finally:
        if provision.proc is not None:
            provision.proc.stop()
