"""WO-1.10 sandbox suite: `proofdeploy verify` end to end (plan 1.10).

Verify runs on the golden apps with a STUB model (the conformance
kit's canned runner returning the app's own canned bug probe), so a
verdict difference can only come from the code under test:

- a good change (fix-parent..fix) verifies PASS, exit 0;
- a planted-defect change (base..bug) verifies FAIL, exit 1;
- an unprovisionable change (clean..lockmismatch) is INCONCLUSIVE
  `environment`, exit 2 — never a pass, never a crash.

The records are asserted as sandbox evidence: sandboxed, carrying
the side's isolation evidence (rootless userns-remap in effect, the
app network mode recorded — the network is cut after install), no
canary value anywhere, and the developer's own repo tree untouched
by the run (writes happen only on the disposable instance).

Marked with the sandbox marker through the conftest keyword rule
(the skip reason names the sandbox); the skip becomes a failure
under PD_REQUIRE_SANDBOX=1, as everywhere in this suite.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from conformance import kit
from proofdeploy.cli import build_parser, cmd_verify
from proofdeploy.runpair import read_records

_SANDBOX_CAPS: dict = {}


def _sandbox_ready() -> bool:
    if "value" not in _SANDBOX_CAPS:
        try:
            from proofdeploy.sandbox import sandbox_evidence

            _SANDBOX_CAPS["value"] = sandbox_evidence()
        except Exception as exc:  # any probe failure = not ready here
            _SANDBOX_CAPS["value"] = {"error": str(exc)}
    ev = _SANDBOX_CAPS["value"]
    return bool(
        ev.get("podman")
        and ev.get("userns_remap")
        and ev.get("build_network")
        and not ev.get("error")
    )


def _ready_str() -> str:
    return json.dumps(_SANDBOX_CAPS.get("value"), sort_keys=True, default=str)


pytestmark = pytest.mark.skipif(
    not _sandbox_ready(),
    reason="sandbox not ready on this host: " + _ready_str(),
)


def _tree_digest(root: Path) -> dict[str, str]:
    """rel path -> sha256 for every file under root except .git."""
    out: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or ".git" in path.parts:
            continue
        out[str(path.relative_to(root))] = hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
    return out


def _run_verify(tmp_path: Path, app: str, from_rev: str, to_rev: str):
    repo = tmp_path / "repo"
    shas = kit.materialize_golden(app, repo)
    before = _tree_digest(repo)
    probes = kit.load_probes(app)
    out_dir = tmp_path / "records"
    workdir = tmp_path / "work"
    output_path = tmp_path / "verify-output.json"
    args = build_parser().parse_args(
        [
            "verify",
            "--repo", str(repo),
            "--from", shas[from_rev],
            "--to", shas[to_rev],
            "--format", "json",
            "--output", str(output_path),
            "--output-dir", str(out_dir),
            "--workdir", str(workdir),
        ]
    )
    code = cmd_verify(args, model_runner=kit.canned_runner(probes["bug_probes"]))
    output = json.loads(output_path.read_text(encoding="utf-8"))
    assert _tree_digest(repo) == before, "verify wrote into the developer's repo"
    return code, output, out_dir, workdir


def _record_for(output, out_dir: Path) -> dict:
    ref = output["evidence_record"]
    assert ref is not None
    records = read_records(out_dir)
    return next(r for r in records if r["record_id"] == ref["record_id"])


def _assert_sandbox_evidence(record: dict) -> None:
    assert record["sandboxed"] is True
    assert record["measured_result"] is False
    evidence = (record.get("sandbox_evidence") or {}).get("verify") or {}
    assert evidence, "the record must carry the side's sandbox evidence"
    # Safety items 1-2 as the sandbox reports them: the rootless
    # userns-remap mode in effect (caps dropped + no-new-privileges
    # are properties of the sandbox's container arguments, asserted
    # at that level in tests/test_container_spike.py), and the app's
    # network mode recorded (isolated/none: cut after install).
    assert evidence.get("userns_mode") == "userns-remap"
    assert evidence.get("userns_opt_out") is False
    assert evidence.get("app_network_mode")


def _assert_tool_state_outside_tree(workdir: Path) -> None:
    """Safety item 6, conditional on plan 1.2(n) (D77, in flight).

    Once the provisioning plans carry the {pd_tools} placeholder,
    the verify snapshot tree must contain no tool state. Before
    (n) lands there is nothing further to assert here — the host
    suite's plan-matrix guard pins the pre-(n) state and activates
    the strict assertion when the placeholder appears.
    """
    snapshots = list(workdir.glob("*/snapshot-verify"))
    assert snapshots, "the verify snapshot must exist under the workdir"
    snapshot = snapshots[0]
    from proofdeploy.provisioning import plan_provisioning
    from proofdeploy.yml import load_contract

    plan = plan_provisioning(snapshot, load_contract(snapshot / "proofdeploy.yml"))
    strings: list[str] = []
    for step in plan.steps:
        strings.extend(step.argv or [])
        strings.extend(step.env.values())
        if step.shell:
            strings.append(step.shell)
    if any("{pd_tools}" in s for s in strings):
        assert not (snapshot / ".pd-tools").exists()
        assert not (snapshot / ".venv").exists()


def _assert_clean_records(output, out_dir: Path, app: str) -> dict:
    record = _record_for(output, out_dir)
    _assert_sandbox_evidence(record)
    # Safety item 3's record half: no canary value in any record.
    assert kit.scan_records_for_canary(out_dir, kit.canary_env_value(app)) == []
    return record


def test_verify_sandbox_node_good_change_passes(tmp_path):
    code, output, out_dir, workdir = _run_verify(
        tmp_path, "node-express-sqlite", "fix-parent", "fix"
    )
    assert code == 0
    assert output["verdict"] == "PASS"
    assert output["exit_code"] == 0
    record = _assert_clean_records(output, out_dir, "node-express-sqlite")
    # Records store the Verdict enum's value ("pass"/"fail"); the
    # verify OUTPUT renders the name ("PASS"/"FAIL"), asserted above.
    assert [r["verdict"] for r in record["c_results"]] == ["pass"]
    _assert_tool_state_outside_tree(workdir)


def test_verify_sandbox_node_planted_defect_fails(tmp_path):
    code, output, out_dir, workdir = _run_verify(
        tmp_path, "node-express-sqlite", "base", "bug"
    )
    assert code == 1
    assert output["verdict"] == "FAIL"
    assert output["exit_code"] == 1
    record = _assert_clean_records(output, out_dir, "node-express-sqlite")
    assert [r["verdict"] for r in record["c_results"]] == ["fail"]
    _assert_tool_state_outside_tree(workdir)


def test_verify_sandbox_python_good_change_passes(tmp_path):
    code, output, out_dir, workdir = _run_verify(
        tmp_path, "python-flask-sqlite", "fix-parent", "fix"
    )
    assert code == 0
    assert output["verdict"] == "PASS"
    record = _assert_clean_records(output, out_dir, "python-flask-sqlite")
    assert [r["verdict"] for r in record["c_results"]] == ["pass"]
    _assert_tool_state_outside_tree(workdir)


def test_verify_sandbox_python_planted_defect_fails(tmp_path):
    code, output, out_dir, workdir = _run_verify(
        tmp_path, "python-flask-sqlite", "base", "bug"
    )
    assert code == 1
    assert output["verdict"] == "FAIL"
    record = _assert_clean_records(output, out_dir, "python-flask-sqlite")
    assert [r["verdict"] for r in record["c_results"]] == ["fail"]
    _assert_tool_state_outside_tree(workdir)


def test_verify_sandbox_unprovisionable_change_is_inconclusive(tmp_path):
    code, output, out_dir, workdir = _run_verify(
        tmp_path, "node-express-sqlite", "clean", "lockmismatch"
    )
    assert code == 2
    assert output["verdict"] == "INCONCLUSIVE"
    assert output["typed_reason"] == "environment"
    assert output["cause"] == "provisioning_failed"
    record = _record_for(output, out_dir)
    assert record["sandboxed"] is True
    assert record["c_results"] == []
    assert kit.scan_records_for_canary(
        out_dir, kit.canary_env_value("node-express-sqlite")
    ) == []
