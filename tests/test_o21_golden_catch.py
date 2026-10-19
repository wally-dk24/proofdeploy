"""R-METHOD end to end on the two golden bug units (D242).

The real prompt of each golden bug unit, a stub model (no model call),
the real evidence check, then the surviving probes run against the real
golden apps (fix^ and fix) and scored by the registered pair rule:

* a correct-direction answer (expected values quoted from the fixture)
  is accepted, runs, FAILs at fix^, PASSes at fix: candidate_catch;
* a wrong-direction answer (it asserts what the diff's changed line
  computes) is rejected by the check, in both rounds: nothing runs and
  the unit counts as not caught.

Live apps come from the same helpers as the golden fixture-truth tests
(`npm ci` and a hash-pinned venv, one install per app).
"""

from __future__ import annotations

import subprocess
import sys

import o21_helpers as h
import pytest
from test_golden_fixture_truth import GOLDEN, _running_golden

from proofdeploy.executor import Executor
from proofdeploy.runpair import RunPairVerdict, score_run_pair_detailed

APPS = ("node-express-sqlite", "python-flask-sqlite")
# the file each golden's diff changes, and the exact text of its changed line
CHANGED = {
    "node-express-sqlite": ("server.js", "done: note.done !== 1,"),
    "python-flask-sqlite": ("app.py", '"done": row[\'done\'] != 1,'),
}


def _note(note_id: int, done: bool, quote_source: str, quote: str) -> dict:
    return {
        "setup": [],
        "act": {
            "method": "GET",
            "path": f"/notes/{note_id}",
            "headers": {"Authorization": "Bearer ${AUTH_TOKEN}"},
        },
        "assert": [
            {
                "type": "body",
                "json_path": {"path": "done", "equals": done},
                "evidence": {"source": quote_source, "quote": quote},
            }
        ],
    }


def _correct_answer() -> str:
    """Expected values straight from the fixture's seed statement."""
    return h.probe_answer(
        _note(1, False, "fixture", "id 1 (done=false)"),
        _note(2, True, "fixture", "id 2 (done=true)"),
    )


def _wrong_answer(app: str) -> str:
    """What the diff's new line computes: note 1 done, note 2 not done,
    justified by the changed line itself."""
    src, line = CHANGED[app]
    return h.probe_answer(
        _note(1, True, src, line),
        _note(2, False, src, line),
    )


class Runner:
    def __init__(self, answer: str):
        self.answer, self.calls = answer, 0

    def __call__(self, prompt: str):
        self.calls += 1
        return h.response(self.answer)


# ---------------------------------------------------------------------
# live installs (one per app)
# ---------------------------------------------------------------------


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    """{app: (shims, installs)} for _running_golden."""
    out = {}
    # node-express: one npm ci from base; every revision links it
    tpl = tmp_path_factory.mktemp("o21-npm") / "app"
    import shutil

    shutil.copytree(GOLDEN / "node-express-sqlite" / "revisions" / "base", tpl)
    subprocess.run(
        ["npm", "ci", "--no-audit", "--no-fund"],
        cwd=tpl, check=True, capture_output=True, text=True, timeout=900,
    )
    shim = tmp_path_factory.mktemp("o21-pyshim")
    (shim / "python").symlink_to(sys.executable)
    out["node-express-sqlite"] = (
        {("node-express-sqlite", r): shim for r in ("fix-parent", "fix")},
        {"node-express-sqlite": [("node_modules", tpl / "node_modules")]},
    )
    # python-flask: a venv from the committed hash-pinned requirements
    venv = tmp_path_factory.mktemp("o21-pyvenv")
    subprocess.run(
        [sys.executable, "-m", "venv", "--copies", str(venv)],
        check=True, capture_output=True, text=True, timeout=300,
    )
    req = GOLDEN / "python-flask-sqlite" / "revisions" / "base" / "requirements.txt"
    subprocess.run(
        [str(venv / "bin" / "python"), "-m", "pip", "install", "--require-hashes", "-r", str(req)],
        check=True, capture_output=True, text=True, timeout=900,
    )
    out["python-flask-sqlite"] = (
        {("python-flask-sqlite", r): venv / "bin" for r in ("fix-parent", "fix")},
        {},
    )
    return out


def _run_side(app, revision, probes, tmp_path, live):
    shims, installs = live[app]
    results = []
    with _running_golden(app, revision, tmp_path, shims, installs) as (base, contract):
        ex = Executor(
            target_url=base, provisioned=True, credential_config=dict(contract.auth or {})
        )
        try:
            for i, p in enumerate(probes):
                results.append(ex.run_probe(p, i))
        finally:
            ex.close()
    return results


@pytest.mark.parametrize("app", APPS)
def test_i_correct_direction_answer_is_a_candidate_catch(app, tmp_path, live):
    g = h.golden_bundle(tmp_path / "bundle", app, model_runner=Runner(_correct_answer()))
    probeset, _raw = g.orch._author_probes(g.prompt, g.sources)
    ev = probeset.author_evidence
    assert ev["correction_used"] is False
    assert [c["result"] for c in ev["checks"]["r1"]] == ["ok", "ok"]
    assert len(probeset.probes) == 2 and not probeset.invalid_blocks
    parent = _run_side(app, "fix-parent", probeset.probes, tmp_path / "p", live)
    fixed = _run_side(app, "fix", probeset.probes, tmp_path / "f", live)
    score = score_run_pair_detailed(parent, fixed)
    assert [r.verdict.value for r in parent] == ["fail", "fail"], [r.details for r in parent]
    assert [r.verdict.value for r in fixed] == ["pass", "pass"], [r.details for r in fixed]
    assert score.verdict == RunPairVerdict.CATCH and score.candidate_catch == [0, 1]


@pytest.mark.parametrize("app", APPS)
def test_i_wrong_direction_answer_citing_the_changed_line_is_rejected(app, tmp_path):
    runner = Runner(_wrong_answer(app))
    g = h.golden_bundle(tmp_path / "bundle", app, model_runner=runner)
    probeset, _raw = g.orch._author_probes(g.prompt, g.sources)
    assert runner.calls == 2  # the answer and its one correction
    ev = probeset.author_evidence
    reasons = {c["result"] for c in ev["checks"]["r1"]} | {c["result"] for c in ev["checks"]["r2"]}
    assert reasons == {"evidence_self_justifying"}
    assert probeset.probes == [] and len(probeset.invalid_blocks) == 2
    # the unit counts as not caught: nothing is provisioned, nothing runs
    g.cfg.output_dir = tmp_path / "records"
    g.cfg.workdir = tmp_path / "measure-work"
    summary = g.orch.measure_bug()
    assert summary["verdict"] == "no_catch"
    assert summary["candidate_catch"] == []
    assert summary["cause"] == "author_output_invalid"
