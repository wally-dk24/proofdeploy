"""Lean P4 (D258) sandbox suite: `proofdeploy lean-verify` end to end on a golden app.

Both sides are provisioned by the measured path's own sandbox provisioning
(rootless userns, network cut after install); pdcov is injected into the app
interpreter through the per-side ``/pd-home`` mount; the impact search and the
2x2 runs go through the real sandbox bridge. The judge is a STUB runner: no
model call is made and no key is read, so a verdict difference can only come
from the code under test.

Marked with the sandbox marker through the conftest keyword rule (the skip
reason names the sandbox); the skip becomes a failure under
PD_REQUIRE_SANDBOX=1, as everywhere in this suite.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from lean_helpers import FACT_OPEN, PromptStub, bug_rule

from conformance import kit
from proofdeploy.cli import build_parser
from proofdeploy.lean.run import cmd_lean_verify
from proofdeploy.lean.verdict import FAIL, LeanRecord, replay

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


pytestmark = pytest.mark.skipif(
    not _sandbox_ready(),
    reason="sandbox not ready on this host: "
    + json.dumps(_SANDBOX_CAPS.get("value"), sort_keys=True, default=str),
)

APP = "python-flask-sqlite"


def test_golden_bug_lean_verify_fails_in_the_sandbox_with_a_judged_regression(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "repo"
    shas = kit.materialize_golden(APP, repo)
    out = tmp_path / "out"
    args = build_parser().parse_args(
        [
            "lean-verify",
            "--repo",
            str(repo),
            "--base",
            shas["base"],
            "--head",
            shas["bug"],
            "--output-dir",
            str(out),
        ]
    )
    stub = PromptStub(bug_rule)
    code = cmd_lean_verify(args, model_runner=stub)
    printed = capsys.readouterr().out
    # per-stage timings go to the CI log
    print(printed)
    assert code == 1, printed

    m = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert m["verdict"]["status"] == FAIL and m["verdict"]["cause"] == "judged_regression"
    assert m["sandboxed"] is True and m["measured_result"] is False and m["mode"] == "sandbox"

    # the sandbox's isolation, as the measured path reports it
    for side in ("base", "head"):
        s = m["sides"][side]
        assert s["sandboxed"] is True
        assert s["pdcov_setup_exit"] == 0
        evidence = s["sandbox_evidence"]
        assert evidence.get("userns_mode") == "userns-remap"
        assert evidence.get("userns_opt_out") is False
        assert evidence.get("app_network_mode")
        assert s["db_snapshot_sha256"]

    # impact was measured through pdcov inside the container
    assert m["coverage"]["head"]["covered"] is True
    assert m["impact_scenarios"], "the bug's line must be reached by a request"
    assert m["unexercised_lines"] == []

    # four runs, each a fresh instance on a fresh database: same counts, nothing noisy
    runs = m["runs"]
    assert runs["base"] == runs["base2"] and runs["head"] == runs["head2"]
    assert runs["base"]["requests"] == runs["head"]["requests"] > 0
    assert m["noise_fields"] == [], m["noise"]
    assert m["hard_failures"] == []

    record = LeanRecord.from_dict(json.loads((out / "lean-record.json").read_text("utf-8")))
    done = [
        d
        for d in record.inputs["differences"]
        if d["field_path"] == "done" and d["path"].startswith("/notes/")
    ]
    assert any(d["before"] is False and d["after"] is True for d in done), done
    regs = record.verdict["details"]["regressions"]
    assert any(r["quote"] == FACT_OPEN for r in regs)
    assert replay(record).status == FAIL
    assert stub.prompts, "the judge ran on the real difference"

    # tool state stays outside the checked-out tree (plan 1.2(n)); no token in any output
    for snap in (out / "work").rglob("snapshot-*"):
        assert not (snap / ".pd-tools").exists() and not (snap / ".venv").exists()
    for f in out.glob("*.json"):
        assert "pdcanary-" not in f.read_text(encoding="utf-8"), f.name
    print("timings (s):", json.dumps(m["timings_seconds"], sort_keys=True))
