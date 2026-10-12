"""WO-7 PR 2 / Addendum 33: a packageManager-pinned app on the
Node 26 row.

Corepack is not bundled from Node 25 onward (verified against the
pinned node:26.11.1 image: ``corepack: command not found``), so a
pinned install there needs the adapter's explicit corepack
bootstrap step. This test takes the pnpm golden, adds one variant
commit — the overlay in ``variant-node26/``: engines widened to
the 26 row, with the golden's own lockfile (both goldens pin
better-sqlite3 ^12.11.1, which ships prebuilt binaries for the
Node 24 and Node 26 ABIs; the original ^11.3.0 pin had no prebuild
for either row, and its node-gyp fallback cannot extract the Node
headers inside the sandbox's userns — tar fchown EPERM) —
and provisions it in the sandbox: READY, on
26.11.1, with the pinned manager version recorded in the plan and
the bootstrap step among the executed steps.

Runs in the sandbox-tests CI job; locally it skips without a
capable sandbox (in CI a skip fails the job, per
PD_REQUIRE_SANDBOX).
"""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "conformance"))
import kit

from proofdeploy.orchestrator import (
    MeasureConfig,
    Orchestrator,
    _free_port,
)
from proofdeploy.sandbox import sandbox_evidence

_EVIDENCE = sandbox_evidence()
requires_sandbox = pytest.mark.skipif(
    not (
        _EVIDENCE.get("podman")
        and _EVIDENCE.get("userns_remap")
        and _EVIDENCE.get("build_network")
    ),
    reason=(
        "sandbox unavailable: "
        + ", ".join(
            k for k in ("podman", "userns_remap", "build_network")
            if not _EVIDENCE.get(k)
        )
    ),
)

pytestmark = requires_sandbox


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=True, timeout=120,
    )
    return proc.stdout.strip()


def test_pnpm_golden_variant_provisions_on_node26(tmp_path):
    repo = tmp_path / "repo"
    shas = kit.materialize_golden("node-pnpm-sqlite", repo)
    # One variant commit on top of fix: the Node 26 overlay
    # (package.json + regenerated pnpm-lock.yaml).
    _git(repo, "checkout", "-q", shas["fix"])
    overlay = (
        Path(__file__).parent.parent
        / "conformance" / "golden" / "node-pnpm-sqlite" / "variant-node26"
    )
    for name in ("package.json", "pnpm-lock.yaml"):
        shutil.copyfile(overlay / name, repo / name)
    _git(repo, "add", "package.json", "pnpm-lock.yaml")
    _git(repo, "commit", "-q", "-m", "variant: node 26 row")
    rev = _git(repo, "rev-parse", "HEAD")
    print(f"node26 variant commit: {rev} (golden fix: {shas['fix']})")

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
    side = orch._provision_side_sandbox(rev, _free_port(), unit, "node26")
    provision = side.provision
    kit.dump_logs("node26-corepack-node-pnpm-sqlite-variant", provision.logs)
    try:
        assert provision.ready, "\n".join(provision.logs)
        plan = provision.install_plan
        assert plan is not None
        assert plan["selected_version"] == "26.11.1", plan
        assert plan["package_manager"] == "pnpm"
        assert plan["package_manager_version"] == "9.15.0"
        assert provision.runtime_version == "26.11.1"
        installs = [
            s for s in provision.executed_steps if s["phase"] == "install"
        ]
        assert len(installs) >= 2, provision.executed_steps
        # The persisted corepack bootstrap ran first (Addendum 35:
        # into the checkout's .pd-tools, never -g — each step is its
        # own --rm container), and the pinned install ran through
        # the persisted corepack binary.
        assert installs[0]["argv_rendered"] == [
            "npm", "install", "--prefix", "/checkout/.pd-tools",
            "--no-save", "--no-audit", "--no-fund", "corepack@0.36.0",
        ]
        assert installs[0]["exit_code"] == 0
        assert installs[1]["argv_rendered"] == [
            "/checkout/.pd-tools/node_modules/.bin/corepack",
            "pnpm@9.15.0", "install", "--frozen-lockfile",
        ]
        assert installs[1]["exit_code"] == 0
    finally:
        if provision.proc is not None:
            provision.proc.stop()
