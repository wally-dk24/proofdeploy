"""Tests for proofdeploy.orchestrator (WO-5).

The model call is stubbed (injected runner); everything else is real:
bundle assembly from a real git repo, real provisioning of both sides,
real probe execution, real evidence/score records and commits, and real
cleanup_run on every exit path.
"""

import subprocess
from pathlib import Path

import pytest

from proofdeploy.leakcheck import SecretLeakError
from proofdeploy.model_client import ModelResponse
from proofdeploy.orchestrator import MeasureConfig, Orchestrator
from proofdeploy.runpair import RUNS_LOG_NAME, read_records

MINI_APP = '''\
import json
import os
import secrets
from http.server import BaseHTTPRequestHandler, HTTPServer

TOKENS = {}
VALUE = __VALUE__


class Handler(BaseHTTPRequestHandler):
    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b""
        return json.loads(raw.decode() or "{}")

    def do_GET(self):
        if self.path == "/health":
            self._json(200, {"ok": True})
        elif self.path == "/value":
            self._json(200, {"v": VALUE})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path == "/register":
            data = self._read_json()
            token = "tok-" + secrets.token_hex(8)
            TOKENS[token] = data.get("username")
            self._json(200, {"token": token})
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
  seed_note: "empty; create users with POST /register"
  auth_mechanism: "Bearer token in the Authorization header"
  auth_scope: "a regular user token"
  flags_note: "no feature flags"
"""


def _git(repo: Path, *args: str) -> str:
    p = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=True,
    )
    return p.stdout.strip()


def make_mini_repo(root: Path, build_cmd: str | None = None) -> dict[str, str]:
    """A tiny real app repo: base (VALUE=1) -> bug (VALUE=2) -> fix (VALUE=1)."""
    repo = root / "mini-app"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "test")
    _git(repo, "config", "user.email", "test@test")
    (repo / "requirements.txt").write_text("# no third-party deps\n")
    yml = MINI_YML
    if build_cmd is not None:
        yml = yml.replace(
            'build: "python -m py_compile app.py"',
            f'build: "{build_cmd}"',
        )
    (repo / "proofdeploy.yml").write_text(yml)
    shas = {}
    for tag, value in (("base", 1), ("bug", 2), ("fix", 1)):
        (repo / "app.py").write_text(MINI_APP.replace("__VALUE__", str(value)))
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", tag)
        shas[tag] = _git(repo, "rev-parse", "HEAD")
    return {"repo": str(repo), **shas}


def canned_probes(prompt: str) -> ModelResponse:
    assert "mini-app" in prompt  # the author really saw the bundle
    return ModelResponse(
        content=(
            "```json\n"
            '{"setup": [], '
            '"act": {"method": "GET", "path": "/value"}, '
            '"assert": [{"type": "body", "json_path": {"path": "v", "equals": 1}}]}'
            "\n```\n"
        ),
        finish_reason="stop",
    )


def skill_path() -> Path:
    return Path(__file__).parent.parent / "docs" / "evaluation" / "skill" / "skill-author.md"


def test_measure_bug_end_to_end(tmp_path):
    info = make_mini_repo(tmp_path)
    out = tmp_path / "records"
    work = tmp_path / "work"
    cfg = MeasureConfig(
        repo_dir=Path(info["repo"]),
        output_dir=out,
        workdir=work,
        skill_path=skill_path(),
        bug_id="mini",
        rev_bug_base=info["base"],
        rev_bug=info["bug"],
        rev_fix=info["fix"],
        model_runner=canned_probes,
        public_contract_env_keys=["APP_ENV"],
    )
    summary = Orchestrator(cfg).measure_bug()
    assert summary["verdict"] == "candidate_catch"
    assert summary["candidate_catch"] == [0]
    assert summary["probe_count"] == 1
    assert summary["parent_ready"] and summary["fix_ready"]

    # Records: evidence committed before score, score committed after.
    log = out / RUNS_LOG_NAME
    assert log.is_file()
    records = read_records(out)
    types = [r["record_type"] for r in records]
    assert types == ["evidence", "score"]
    assert not any(r["record_type"] == "judgment" for r in records)
    evidence, score = records
    assert evidence["prompt"] and evidence["raw_model_response"]
    assert evidence["fix_parent_sha"] == info["bug"]
    assert evidence["fix_sha"] == info["fix"]
    assert evidence["verdict"] is None
    # The stored score verdict stays candidate_catch: never catch.
    assert score["verdict"] == "candidate_catch"
    assert score["candidate_catch"] == [0]
    assert score["evidence_record_id"] == evidence["record_id"]
    # Both commits exist in the records repo.
    commits = _git(out, "log", "--format=%s").splitlines()
    assert any(c.startswith("evidence:") for c in commits)
    assert any(c.startswith("score:") for c in commits)

    # Cleanup: no provisioned scratch dirs survive.
    prov = work / "provisioner"
    for sub in ("venvs", "runs"):
        d = prov / sub
        assert not d.exists() or list(d.iterdir()) == []


def test_measure_bug_provisioning_failure_is_inconclusive(tmp_path):
    info = make_mini_repo(tmp_path, build_cmd="python -m py_compile missing.py")
    out = tmp_path / "records"
    work = tmp_path / "work"
    cfg = MeasureConfig(
        repo_dir=Path(info["repo"]),
        output_dir=out,
        workdir=work,
        skill_path=skill_path(),
        bug_id="mini-broken",
        rev_bug_base=info["base"],
        rev_bug=info["bug"],
        rev_fix=info["fix"],
        model_runner=canned_probes,
        public_contract_env_keys=["APP_ENV"],
    )
    summary = Orchestrator(cfg).measure_bug()
    assert summary["verdict"] == "inconclusive"
    assert not summary["parent_ready"]
    records = read_records(out)
    assert [r["record_type"] for r in records] == ["evidence", "score"]
    assert records[1]["verdict"] == "inconclusive"
    prov = work / "provisioner"
    for sub in ("venvs", "runs"):
        d = prov / sub
        assert not d.exists() or list(d.iterdir()) == []


def test_measure_bug_leak_check_fails_closed(tmp_path):
    info = make_mini_repo(tmp_path)
    repo = Path(info["repo"])
    # The fixture carries a secret and the repo snapshot contains it:
    # the leak check must refuse before any model call.
    yml = (repo / "proofdeploy.yml").read_text()
    yml = yml.replace(
        '  flags_note: "no feature flags"',
        '  flags_note: "no feature flags"\n  auth_token: "forbidden-secret-token-xyz"',
    )
    (repo / "proofdeploy.yml").write_text(yml)
    (repo / "leaky.txt").write_text("forbidden-secret-token-xyz\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "leak")
    leak_sha = _git(repo, "rev-parse", "HEAD")

    def must_not_run(prompt: str) -> ModelResponse:  # pragma: no cover
        raise AssertionError("model must not be called after a leak")

    cfg = MeasureConfig(
        repo_dir=repo,
        output_dir=tmp_path / "records",
        workdir=tmp_path / "work",
        skill_path=skill_path(),
        bug_id="mini-leak",
        rev_bug_base=info["bug"],
        rev_bug=leak_sha,
        rev_fix=info["fix"],
        model_runner=must_not_run,
    )
    with pytest.raises(SecretLeakError):
        Orchestrator(cfg).measure_bug()


def test_measure_clean_end_to_end(tmp_path):
    info = make_mini_repo(tmp_path)
    out = tmp_path / "records"
    work = tmp_path / "work"
    cfg = MeasureConfig(
        repo_dir=Path(info["repo"]),
        output_dir=out,
        workdir=work,
        skill_path=skill_path(),
        clean_id="mini-clean",
        rev_clean=info["fix"],
        model_runner=canned_probes,
        public_contract_env_keys=["APP_ENV"],
    )
    summary = Orchestrator(cfg).measure_clean()
    assert summary["run_kind"] == "clean"
    assert summary["false_alarm"] is False
    assert summary["c_ready"]
    records = read_records(out)
    assert [r["record_type"] for r in records] == ["evidence", "score"]
    assert records[0]["c_sha"] == info["fix"]
    assert records[0]["fix_sha"] is None
    prov = work / "provisioner"
    for sub in ("venvs", "runs"):
        d = prov / sub
        assert not d.exists() or list(d.iterdir()) == []


def test_measure_needs_revs():
    from proofdeploy.orchestrator import OrchestratorError

    cfg = MeasureConfig(
        repo_dir=Path("/tmp"),
        output_dir=Path("/tmp"),
        workdir=Path("/tmp"),
        skill_path=Path("/tmp"),
        bug_id="x",
    )
    with pytest.raises(OrchestratorError):
        Orchestrator(cfg).measure_bug()


def test_measure_bug_refuses_seeded_fix_diff(tmp_path, monkeypatch):
    """Required test (WO-5 brief): seed the fix diff into the bundle; the run is refused."""
    from proofdeploy.leakcheck import AnswerKeyLeakError

    info = make_mini_repo(tmp_path)
    out = tmp_path / "records"
    work = tmp_path / "work"
    cfg = MeasureConfig(
        repo_dir=Path(info["repo"]),
        output_dir=out,
        workdir=work,
        skill_path=skill_path(),
        bug_id="mini",
        rev_bug_base=info["base"],
        rev_bug=info["bug"],
        rev_fix=info["fix"],
        model_runner=canned_probes,
        public_contract_env_keys=["APP_ENV"],
    )
    orch = Orchestrator(cfg)
    orig_build = orch._build_bundle

    def tampered_build(unit_dir: Path, **kwargs):
        bundle, manifest, prompt, diff_text, base_sha, tip_sha, run_context = orig_build(
            unit_dir, **kwargs
        )
        # Deliberately seed the fix diff into the bundle's diff.patch.
        fix_diff = subprocess.run(
            ["git", "-C", info["repo"], "diff", f"{info['bug']}..{info['fix']}", "--"],
            capture_output=True, text=True, check=True,
        ).stdout
        (bundle.root / "diff.patch").write_text(fix_diff, encoding="utf-8")
        return bundle, manifest, prompt, diff_text, base_sha, tip_sha, run_context

    monkeypatch.setattr(orch, "_build_bundle", tampered_build)
    with pytest.raises(AnswerKeyLeakError, match="byte-equal"):
        orch.measure_bug()
    # The run is refused before any model call: no records committed.
    assert not (out / RUNS_LOG_NAME).exists()


def test_measure_bug_refuses_fix_sha_in_snapshot(tmp_path, monkeypatch):
    """The fix SHA smuggled into the snapshot also refuses the run."""
    from proofdeploy.leakcheck import AnswerKeyLeakError

    info = make_mini_repo(tmp_path)
    out = tmp_path / "records"
    work = tmp_path / "work"
    cfg = MeasureConfig(
        repo_dir=Path(info["repo"]),
        output_dir=out,
        workdir=work,
        skill_path=skill_path(),
        bug_id="mini",
        rev_bug_base=info["base"],
        rev_bug=info["bug"],
        rev_fix=info["fix"],
        model_runner=canned_probes,
        public_contract_env_keys=["APP_ENV"],
    )
    orch = Orchestrator(cfg)
    orig_build = orch._build_bundle

    def tampered_build(unit_dir: Path, **kwargs):
        bundle, manifest, prompt, diff_text, base_sha, tip_sha, run_context = orig_build(
            unit_dir, **kwargs
        )
        snap_file = bundle.root / "snapshot" / "app.py"
        snap_file.write_text(
            snap_file.read_text(encoding="utf-8") + f"\n# fix {info['fix']}\n",
            encoding="utf-8",
        )
        return bundle, manifest, prompt, diff_text, base_sha, tip_sha, run_context

    monkeypatch.setattr(orch, "_build_bundle", tampered_build)
    with pytest.raises(AnswerKeyLeakError, match="!= B version"):
        orch.measure_bug()
    assert not (out / RUNS_LOG_NAME).exists()
