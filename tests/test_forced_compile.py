"""Addendum 37, proof (b): the compile fallback actually works.

The express golden plus a golden-only .npmrc (build-from-source —
see conformance/golden/node-express-sqlite/variant-forced-compile/)
forces better-sqlite3 to COMPILE instead of taking its prebuild.
With npm_config_nodedir=/usr/local rendered into the install step
env (plan data), node-gyp compiles against the pinned image's own
headers: no nodejs.org download, no tarball extraction (the path
that dies with TAR_ENTRY_ERROR EPERM fchown in this sandbox), no
added capabilities. The provision must reach READY, the canned
probes must read the notes back out of SQLite, and the provision
logs — dumped to pd-logs/ like every other provision — must show
node-gyp's success line, `gyp info ok`.
"""
import os
import shutil
import socket
import subprocess
from pathlib import Path

import pytest

from conformance import kit
from proofdeploy.orchestrator import MeasureConfig, Orchestrator
from proofdeploy.probe import validate_probe

pytestmark = pytest.mark.skipif(
    not os.environ.get("PD_REQUIRE_SANDBOX"),
    reason="forced-compile proof runs in the sandbox-tests CI job",
)

APP = "node-express-sqlite"
OVERLAY = kit.GOLDEN_DIR / APP / "variant-forced-compile"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True, capture_output=True, text=True, timeout=120,
    ).stdout.strip()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def test_forced_compile_variant_ready_on_node24(tmp_path):
    src = kit.GOLDEN_DIR / APP / "revisions" / "fix"
    repo = tmp_path / "repo"
    shutil.copytree(src, repo)
    # The golden-only overlay: force the compile.
    shutil.copy2(OVERLAY / ".npmrc", repo / ".npmrc")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "conformance-kit")
    _git(repo, "config", "user.email", "kit@proofdeploy.invalid")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "forced-compile variant")
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
        rev, _free_port(), unit, "forced-compile"
    )
    provision = side.provision
    kit.dump_logs("forced-compile-node-express-sqlite-fix", provision.logs)
    try:
        assert provision.ready, "\n".join(provision.logs)
        plan = provision.install_plan
        assert plan is not None
        assert plan["selected_version"] == "24.21.0", plan
        # The nodedir env is IN THE RECORDED PLAN (Addendum 37:
        # plan data, recorded) on the install steps.
        for step in plan["steps"]:
            if step["phase"] == "install":
                assert step["env"].get("npm_config_nodedir") == (
                    "/usr/local"
                ), step
        joined = "\n".join(provision.logs)
        # node-gyp compiled and finished cleanly, without ever
        # downloading headers.
        assert "gyp info ok" in joined
        assert "gyp http" not in joined
        # The canned probes run against the compiled app: register,
        # login, write a note, read it back out of SQLite.
        probes = kit.load_probes(APP)
        parsed = [validate_probe(probes["bug_probes"][0])]
        executor = orch._make_executor(side)
        results = executor.run_all(parsed)
        assert results and all(
            r.verdict.value == "pass" for r in results
        ), [(r.verdict, r.details) for r in results]
    finally:
        if provision.proc is not None:
            provision.proc.stop()
