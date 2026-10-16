"""WO-7 PR 2, proof D: louislam/uptime-kuma at HEAD, READY in the sandbox.

uptime-kuma declares ``engines.node >= 26.2.0``, so provisioning it
exercises runtime selection against the node 26.11.1 row of
images.lock.json — a row the reviewer could not verify from their
host in PR 1 — and pulls the image by its pinned digest.

The test clones the repo (shallow), adds a proofdeploy.yml contract
as a local commit, and provisions that commit through the sandbox
executor — the same adapter-planned path as every measured run. It
asserts READY, the plan's selected version (26.11.1), and the version
the target reports inside the container. The contract's build step
is kuma's own ``npm run build``: the app refuses to start without
the built frontend (``dist/index.html``).

Runs in the sandbox-tests CI job; locally it skips without a capable
sandbox (in CI a skip fails the job, per PD_REQUIRE_SANDBOX).
"""
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

# The capability probe starts real containers (~20s), so it must
# NOT run at import/collection time (WO-CI-SPEED task 2): it runs
# lazily, on first use, and is cached for the process. The skipif
# condition below is a string — pytest's lazy form, evaluated at
# test setup in this module's globals — so collection never probes.
_EVIDENCE: dict[str, object] | None = None


def _evidence() -> dict[str, object]:
    """Sandbox capability evidence, probed once per process."""
    global _EVIDENCE
    if _EVIDENCE is None:
        _EVIDENCE = sandbox_evidence()
    return _EVIDENCE


def _sandbox_ready() -> bool:
    ev = _evidence()
    return bool(
        ev.get("podman") and ev.get("userns_remap") and ev.get("build_network")
    )


requires_sandbox = pytest.mark.skipif(
    "not _sandbox_ready()",
    reason=(
        "sandbox unavailable: needs podman, userns remapping, "
        "and a build network"
    ),
)

pytestmark = requires_sandbox

CONTRACT = """\
build: "npm run build"
start: "node --import=tsx server/server.js"
readiness: "/setup-database-info"
env: {}
fixture:
  repo_name: "uptime-kuma"
  seed_note: "no seed; the app initializes its own sqlite data dir on first start"
  auth_mechanism: "none declared for this provisioning proof"
  auth_scope: "none"
  flags_note: "no feature flags"
"""


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=True, timeout=300,
    )
    return proc.stdout.strip()


def test_uptime_kuma_head_ready_in_sandbox(tmp_path):
    repo = tmp_path / "uptime-kuma"
    subprocess.run(
        ["git", "clone", "-q", "--depth", "1",
         "https://github.com/louislam/uptime-kuma.git", str(repo)],
        check=True, capture_output=True, timeout=600,
    )
    upstream_head = _git(repo, "rev-parse", "HEAD")
    # Addendum 33: the upstream commit this proof provisioned, in
    # the job log (kuma moves; the SHA pins what was exercised).
    print(f"uptime-kuma upstream HEAD: {upstream_head}")
    (repo / "proofdeploy.yml").write_text(CONTRACT, encoding="utf-8")
    _git(repo, "config", "user.name", "conformance-kit")
    _git(repo, "config", "user.email", "kit@proofdeploy.invalid")
    _git(repo, "add", "proofdeploy.yml")
    _git(repo, "commit", "-q", "-m", "proofdeploy contract (local)")
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
    side = orch._provision_side_sandbox(rev, _free_port(), unit, "kuma")
    provision = side.provision
    kit.dump_logs("uptime-kuma-uptime-kuma-head", provision.logs)
    try:
        assert provision.ready, "\n".join(provision.logs)
        plan = provision.install_plan
        assert plan is not None
        # engines.node >= 26.2.0 selects the 26.11.1 pinned row, and
        # the version reported inside the container matches the plan.
        assert plan["adapter"] == "node"
        assert plan["selected_version"] == "26.11.1", plan
        assert provision.runtime_version == "26.11.1"
        assert provision.runtime == "node"
        assert "@sha256:" in plan["image_ref"]
        install = next(
            s for s in provision.executed_steps if s["phase"] == "install"
        )
        assert install["argv_rendered"][:2] == ["npm", "ci"]
        assert install["exit_code"] == 0
        # Provenance for the report: the upstream commit provisioned.
        assert upstream_head != rev  # rev adds only the contract commit
    finally:
        if provision.proc is not None:
            provision.proc.stop()
