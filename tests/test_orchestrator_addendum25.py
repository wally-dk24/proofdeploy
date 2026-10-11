"""Addendum 25 — WO-5.2 round 3: the H9 survivor (tests only).

Round 2's orchestrator-level refusal test covered only ``measure_bug``:
it stubbed the harness gate to raise and showed a measured bug run is
refused before any record is written. The same call in
``measure_clean`` was untested, so removing it (mutant H9) survived —
the package's "called in both" was true in the code but proven only
for the bug path. This file adds the mirror test for a measured clean
run.
"""

import subprocess
from pathlib import Path

import pytest

import proofdeploy.orchestrator as orch_mod
from proofdeploy import runpair
from proofdeploy.model_client import ModelResponse
from proofdeploy.orchestrator import MeasureConfig, Orchestrator
from proofdeploy.runpair import RUNS_LOG_NAME

MINI_APP = '''\
import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer

VALUE = __VALUE__


class Handler(BaseHTTPRequestHandler):
    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._json(200, {"ok": True})
        elif self.path == "/value":
            self._json(200, {"v": VALUE})
        else:
            self._json(404, {"error": "not found"})

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()
'''

MINI_YML = """\
build: "python -m py_compile app.py"
start: "python app.py"
readiness: "/health"
env:
  APP_ENV: verification
fixture:
  repo_name: "mini-app"
  seed_note: "empty"
  auth_mechanism: "none"
  auth_scope: "none"
  flags_note: "no feature flags"
"""


def _git(repo: Path, *args: str) -> str:
    p = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=True,
    )
    return p.stdout.strip()


def _make_mini_repo(root: Path) -> dict[str, str]:
    """base (VALUE=1) -> bug (VALUE=2) -> fix (VALUE=1)."""
    repo = root / "mini-app"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "test")
    _git(repo, "config", "user.email", "test@test")
    (repo / "requirements.txt").write_text("# no third-party deps\n")
    (repo / "proofdeploy.yml").write_text(MINI_YML)
    shas = {}
    for tag, value in (("base", 1), ("bug", 2), ("fix", 1)):
        (repo / "app.py").write_text(MINI_APP.replace("__VALUE__", str(value)))
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", tag)
        shas[tag] = _git(repo, "rev-parse", "HEAD")
    return {"repo": str(repo), **shas}


def _skill_path() -> Path:
    return (
        Path(__file__).parent.parent
        / "docs"
        / "evaluation"
        / "skill"
        / "skill-author.md"
    )


def _canned_probes(prompt: str) -> ModelResponse:
    assert "mini-app" in prompt  # the author really saw the bundle
    probe = {
        "act": {"method": "GET", "path": "/value"},
        "assert": [{"type": "body", "json_path": {"path": "v", "equals": 1}}],
    }
    import json as _json

    return ModelResponse(
        content="```json\n" + _json.dumps(probe) + "\n```\n",
        finish_reason="stop",
    )


def test_measured_clean_run_refused_when_harness_gate_fails(tmp_path, monkeypatch):
    """H9: a measured (sandboxed) clean run must not start when the
    harness gate refuses — the mirror of the bug-path test."""

    def _refuse():
        raise runpair.CommitGateError("harness gate refused (test)")

    monkeypatch.setattr(orch_mod, "assert_harness_commit_gate", _refuse)
    info = _make_mini_repo(tmp_path)
    cfg = MeasureConfig(
        repo_dir=Path(info["repo"]),
        output_dir=tmp_path / "records",
        workdir=tmp_path / "work",
        skill_path=_skill_path(),
        clean_id="mini-harness-clean-refused",
        rev_clean=info["fix"],
        model_runner=_canned_probes,
        public_contract_env_keys=["APP_ENV"],
        allow_unsandboxed=False,
    )
    with pytest.raises(runpair.CommitGateError):
        Orchestrator(cfg).measure_clean()
    # Refused before anything ran: no records were committed.
    assert not (tmp_path / "records" / RUNS_LOG_NAME).exists()
