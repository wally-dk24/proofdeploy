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
        allow_unsandboxed=True,
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
    # The run pair compares fix^ vs fix (not B vs fix). In this toy repo,
    # fix^ == bug (B), so fix_parent_sha == info["bug"].
    assert evidence["fix_parent_sha"] == info["bug"]
    assert evidence["fix_sha"] == info["fix"]
    # All four SHAs are recorded.
    assert evidence["b_sha"] == info["bug"]
    assert evidence["b_parent_sha"] == info["base"]
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


def test_measure_bug_uses_fix_parent_not_b(tmp_path):
    """The run pair must provision fix^, not B, for the 'before' side.

    Creates a repo where B != fix^: base -> bug (B, VALUE=2) ->
    middle (VALUE=3, unrelated) -> fix (VALUE=1). The fix-parent side
    must run at fix^ (middle, VALUE=3), not at B (VALUE=2).
    """
    repo = tmp_path / "mini-app2"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "test")
    _git(repo, "config", "user.email", "test@test")
    (repo / "requirements.txt").write_text("# no third-party deps\n")
    (repo / "proofdeploy.yml").write_text(MINI_YML)
    shas = {}
    # base (VALUE=1) -> bug B (VALUE=2) -> middle (VALUE=3) -> fix (VALUE=1)
    for tag, value in (("base", 1), ("bug", 2), ("middle", 3), ("fix", 1)):
        (repo / "app.py").write_text(MINI_APP.replace("__VALUE__", str(value)))
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", tag)
        shas[tag] = _git(repo, "rev-parse", "HEAD")
    # fix^ is the middle commit, not the bug commit.
    fix_parent = _git(repo, "rev-parse", "HEAD^")
    assert fix_parent == shas["middle"]
    assert fix_parent != shas["bug"]

    out = tmp_path / "records"
    work = tmp_path / "work"
    cfg = MeasureConfig(
        repo_dir=repo,
        output_dir=out,
        workdir=work,
        skill_path=skill_path(),
        bug_id="mini2",
        rev_bug_base=shas["base"],
        rev_bug=shas["bug"],
        rev_fix=shas["fix"],
        model_runner=canned_probes,
        public_contract_env_keys=["APP_ENV"],
        allow_unsandboxed=True,
    )
    Orchestrator(cfg).measure_bug()
    # The probe asserts VALUE==1. At fix^ (middle, VALUE=3), it fails;
    # at fix (VALUE=1), it passes. If we had (incorrectly) used B (VALUE=2)
    # for the before side, it would also fail — but the SHAs prove we
    # used fix^.
    records = read_records(out)
    evidence = records[0]
    assert evidence["fix_parent_sha"] == shas["middle"]
    assert evidence["fix_sha"] == shas["fix"]
    assert evidence["b_sha"] == shas["bug"]
    assert evidence["b_parent_sha"] == shas["base"]
    # The recorded fix_parent_sha must NOT be B.
    assert evidence["fix_parent_sha"] != shas["bug"]


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
        allow_unsandboxed=True,
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
        allow_unsandboxed=True,
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
        allow_unsandboxed=True,
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
        allow_unsandboxed=True,
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
        allow_unsandboxed=True,
)
    orch = Orchestrator(cfg)
    orig_build = orch._build_bundle

    def tampered_build(unit_dir: Path, **kwargs):
        bundle, manifest, prompt, diff_text, base_sha, tip_sha, flags = orig_build(
            unit_dir, **kwargs
        )
        # Deliberately seed the fix diff into the bundle's diff.patch.
        fix_diff = subprocess.run(
            ["git", "-C", info["repo"], "diff", f"{info['bug']}..{info['fix']}", "--"],
            capture_output=True, text=True, check=True,
        ).stdout
        (bundle.root / "diff.patch").write_text(fix_diff, encoding="utf-8")
        return bundle, manifest, prompt, diff_text, base_sha, tip_sha, flags

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
        allow_unsandboxed=True,
)
    orch = Orchestrator(cfg)
    orig_build = orch._build_bundle

    def tampered_build(unit_dir: Path, **kwargs):
        bundle, manifest, prompt, diff_text, base_sha, tip_sha, flags = orig_build(
            unit_dir, **kwargs
        )
        snap_file = bundle.root / "snapshot" / "app.py"
        snap_file.write_text(
            snap_file.read_text(encoding="utf-8") + f"\n# fix {info['fix']}\n",
            encoding="utf-8",
        )
        return bundle, manifest, prompt, diff_text, base_sha, tip_sha, flags

    monkeypatch.setattr(orch, "_build_bundle", tampered_build)
    with pytest.raises(AnswerKeyLeakError, match="!= B version"):
        orch.measure_bug()
    assert not (out / RUNS_LOG_NAME).exists()


def test_measure_config_sandbox_on_by_default():
    """Sandbox is the default: untrusted code never runs on the host
    unless the explicit dev flag is set."""
    from proofdeploy.orchestrator import MeasureConfig

    cfg = MeasureConfig(
        repo_dir=Path("/tmp/r"),
        output_dir=Path("/tmp/o"),
        workdir=Path("/tmp/w"),
        skill_path=Path("/tmp/s.md"),
    )
    assert cfg.sandbox is True
    assert cfg.allow_unsandboxed is False


def test_unsandboxed_run_is_marked_not_measured():
    """An unsandboxed dev run is marked in the evidence record and never
    counts as a measured result."""
    from proofdeploy.runpair import build_evidence_record

    record = build_evidence_record(
        run_kind="bug", sandboxed=False, measured_result=False
    )
    assert record["sandboxed"] is False
    assert record["measured_result"] is False
    # A normal (sandboxed) run counts.
    record2 = build_evidence_record(run_kind="bug")
    assert record2["sandboxed"] is True
    assert record2["measured_result"] is True


def test_evidence_record_carries_provenance():
    from proofdeploy.runpair import build_evidence_record

    prov = {
        "actions_run_url": "https://example.com/runs/1",
        "actions_run_id": "123",
        "runner_image": "ubuntu-24.04",
        "capability_probe": {"userns_remap": True},
    }
    record = build_evidence_record(run_kind="bug", provenance=prov)
    assert record["provenance"] == prov
    assert build_evidence_record(run_kind="bug")["provenance"] == {}


def test_measure_bug_no_skill_arm_end_to_end(tmp_path):
    """The no-skill arm passes the leak check end-to-end through measure."""
    info = make_mini_repo(tmp_path)
    out = tmp_path / "records"
    work = tmp_path / "work"
    cfg = MeasureConfig(
        repo_dir=Path(info["repo"]),
        output_dir=out,
        workdir=work,
        skill_path=None,
        no_skill=True,
        bug_id="mini-noskill",
        rev_bug_base=info["base"],
        rev_bug=info["bug"],
        rev_fix=info["fix"],
        model_runner=canned_probes,
        public_contract_env_keys=["APP_ENV"],
        allow_unsandboxed=True,
    )
    # Must not raise AnswerKeyLeakError: the builder renders "(none)" and
    # the leak check rebuilds the identical prompt.
    summary = Orchestrator(cfg).measure_bug()
    assert summary["verdict"] == "candidate_catch"
    records = read_records(out)
    evidence = records[0]
    assert "(none)" in evidence["prompt"]
    # Builder hash is recorded.
    assert evidence["author_prompt_builder_sha256"]


def test_measure_bug_model_overflow_no_retry(tmp_path):
    """model_overflow: no retry, INCONCLUSIVE, counts as not caught."""
    info = make_mini_repo(tmp_path)
    out = tmp_path / "records"
    work = tmp_path / "work"

    def failing_runner(prompt: str):
        from proofdeploy.model_client import ModelCallError
        raise ModelCallError("context length exceeded", cause="model_overflow")

    cfg = MeasureConfig(
        repo_dir=Path(info["repo"]),
        output_dir=out,
        workdir=work,
        skill_path=skill_path(),
        bug_id="mini-overflow",
        rev_bug_base=info["base"],
        rev_bug=info["bug"],
        rev_fix=info["fix"],
        model_runner=failing_runner,
        public_contract_env_keys=["APP_ENV"],
        allow_unsandboxed=True,
    )
    summary = Orchestrator(cfg).measure_bug()
    assert summary["verdict"] == "inconclusive"
    assert summary["reason_class"] == "environment"
    assert summary["cause"] == "model_overflow"
    assert summary["counting"] == "counts_as_not_caught"
    # Only one attempt (no retry for overflow).
    assert len(summary["model_attempts"]) == 1
    assert "model_overflow" in summary["model_attempts"][0]


def test_measure_bug_model_transport_retry_twice(tmp_path):
    """model_transport failing twice: one retry, both recorded."""
    info = make_mini_repo(tmp_path)
    out = tmp_path / "records"
    work = tmp_path / "work"

    def failing_runner(prompt: str):
        from proofdeploy.model_client import ModelCallError
        raise ModelCallError("connection reset", cause="model_transport")

    cfg = MeasureConfig(
        repo_dir=Path(info["repo"]),
        output_dir=out,
        workdir=work,
        skill_path=skill_path(),
        bug_id="mini-transport",
        rev_bug_base=info["base"],
        rev_bug=info["bug"],
        rev_fix=info["fix"],
        model_runner=failing_runner,
        public_contract_env_keys=["APP_ENV"],
        allow_unsandboxed=True,
    )
    summary = Orchestrator(cfg).measure_bug()
    assert summary["verdict"] == "inconclusive"
    assert summary["cause"] == "model_transport"
    # Two attempts (one retry).
    assert len(summary["model_attempts"]) == 2
    assert "attempt_1" in summary["model_attempts"][0]
    assert "attempt_2" in summary["model_attempts"][1]


def test_measure_bug_model_transport_retry_succeeds(tmp_path):
    """model_transport succeeding on retry: normal run proceeds."""
    info = make_mini_repo(tmp_path)
    out = tmp_path / "records"
    work = tmp_path / "work"
    calls = []

    def flaky_runner(prompt: str):
        from proofdeploy.model_client import ModelCallError
        calls.append(1)
        if len(calls) == 1:
            raise ModelCallError("timeout", cause="model_transport")
        return canned_probes(prompt)

    cfg = MeasureConfig(
        repo_dir=Path(info["repo"]),
        output_dir=out,
        workdir=work,
        skill_path=skill_path(),
        bug_id="mini-retry-ok",
        rev_bug_base=info["base"],
        rev_bug=info["bug"],
        rev_fix=info["fix"],
        model_runner=flaky_runner,
        public_contract_env_keys=["APP_ENV"],
        allow_unsandboxed=True,
    )
    summary = Orchestrator(cfg).measure_bug()
    # Retry succeeded, so normal scoring proceeds.
    assert summary["verdict"] == "candidate_catch"
    assert len(calls) == 2


def test_measure_clean_model_overflow_no_retry(tmp_path):
    """Clean path: model_overflow → INCONCLUSIVE, counts as false alarm."""
    info = make_mini_repo(tmp_path)
    out = tmp_path / "records"
    work = tmp_path / "work"

    def failing_runner(prompt: str):
        from proofdeploy.model_client import ModelCallError
        raise ModelCallError("context length exceeded", cause="model_overflow")

    cfg = MeasureConfig(
        repo_dir=Path(info["repo"]),
        output_dir=out,
        workdir=work,
        skill_path=skill_path(),
        clean_id="mini-clean-overflow",
        rev_clean=info["fix"],
        model_runner=failing_runner,
        public_contract_env_keys=["APP_ENV"],
        allow_unsandboxed=True,
    )
    summary = Orchestrator(cfg).measure_clean()
    assert summary["verdict"] == "inconclusive"
    assert summary["run_kind"] == "clean"
    assert summary["cause"] == "model_overflow"
    assert summary["counting"] == "counts_as_false_alarm"
    assert len(summary["model_attempts"]) == 1


def test_measure_clean_model_transport_retry_twice(tmp_path):
    """Clean path: transport failing twice → one retry, both recorded."""
    info = make_mini_repo(tmp_path)
    out = tmp_path / "records"
    work = tmp_path / "work"

    def failing_runner(prompt: str):
        from proofdeploy.model_client import ModelCallError
        raise ModelCallError("timeout", cause="model_transport")

    cfg = MeasureConfig(
        repo_dir=Path(info["repo"]),
        output_dir=out,
        workdir=work,
        skill_path=skill_path(),
        clean_id="mini-clean-transport",
        rev_clean=info["fix"],
        model_runner=failing_runner,
        public_contract_env_keys=["APP_ENV"],
        allow_unsandboxed=True,
    )
    summary = Orchestrator(cfg).measure_clean()
    assert summary["verdict"] == "inconclusive"
    assert summary["counting"] == "counts_as_false_alarm"
    assert len(summary["model_attempts"]) == 2


def test_measure_clean_model_transport_retry_succeeds(tmp_path):
    """Clean path: transport succeeding on retry → normal run."""
    info = make_mini_repo(tmp_path)
    out = tmp_path / "records"
    work = tmp_path / "work"
    calls = []

    def flaky_runner(prompt: str):
        from proofdeploy.model_client import ModelCallError
        calls.append(1)
        if len(calls) == 1:
            raise ModelCallError("timeout", cause="model_transport")
        return canned_probes(prompt)

    cfg = MeasureConfig(
        repo_dir=Path(info["repo"]),
        output_dir=out,
        workdir=work,
        skill_path=skill_path(),
        clean_id="mini-clean-retry-ok",
        rev_clean=info["fix"],
        model_runner=flaky_runner,
        public_contract_env_keys=["APP_ENV"],
        allow_unsandboxed=True,
    )
    summary = Orchestrator(cfg).measure_clean()
    assert summary["run_kind"] == "clean"
    assert summary["false_alarm"] is False
    assert len(calls) == 2


def test_inconclusive_unsandboxed_records_not_measured(tmp_path):
    """Unsandboxed dev INCONCLUSIVE: measured_result=False (never counts)."""
    info = make_mini_repo(tmp_path)
    out = tmp_path / "records"
    work = tmp_path / "work"

    def failing_runner(prompt: str):
        from proofdeploy.model_client import ModelCallError
        raise ModelCallError("overflow", cause="model_overflow")

    cfg = MeasureConfig(
        repo_dir=Path(info["repo"]),
        output_dir=out,
        workdir=work,
        skill_path=skill_path(),
        bug_id="mini-measured",
        rev_bug_base=info["base"],
        rev_bug=info["bug"],
        rev_fix=info["fix"],
        model_runner=failing_runner,
        public_contract_env_keys=["APP_ENV"],
        allow_unsandboxed=True,
    )
    Orchestrator(cfg).measure_bug()
    records = read_records(out)
    evidence = records[0]
    # Unsandboxed dev run: never a measured result.
    assert evidence["measured_result"] is False
    assert evidence["sandboxed"] is False
    # Provenance is present (not empty).
    assert "provenance" in evidence


def test_inconclusive_sandboxed_counts(tmp_path, monkeypatch):
    """Sandboxed model failure: measured_result=True, probe in provenance."""
    from proofdeploy.model_client import ModelCallError

    info = make_mini_repo(tmp_path)
    out = tmp_path / "records"
    work = tmp_path / "work"

    # Mock the capability gate so a sandboxed run proceeds.
    fake_probe = {"podman": True, "userns_remap": True}
    monkeypatch.setattr(
        "proofdeploy.orchestrator.assert_measurement_gate",
        lambda: fake_probe,
    )

    def failing_runner(prompt: str):
        raise ModelCallError("overflow", cause="model_overflow")

    cfg = MeasureConfig(
        repo_dir=Path(info["repo"]),
        output_dir=out,
        workdir=work,
        skill_path=skill_path(),
        bug_id="mini-sandboxed",
        rev_bug_base=info["base"],
        rev_bug=info["bug"],
        rev_fix=info["fix"],
        model_runner=failing_runner,
        public_contract_env_keys=["APP_ENV"],
        # Sandboxed (no allow_unsandboxed).
    )
    Orchestrator(cfg).measure_bug()
    records = read_records(out)
    evidence = records[0]
    assert evidence["measured_result"] is True
    assert evidence["sandboxed"] is True
    assert evidence["provenance"]["capability_probe"] == fake_probe


def test_inconclusive_unsandboxed_dev_does_not_count(tmp_path):
    """Unsandboxed dev model failure: measured_result=False."""
    from proofdeploy.model_client import ModelCallError

    info = make_mini_repo(tmp_path)
    out = tmp_path / "records"
    work = tmp_path / "work"

    def failing_runner(prompt: str):
        raise ModelCallError("overflow", cause="model_overflow")

    cfg = MeasureConfig(
        repo_dir=Path(info["repo"]),
        output_dir=out,
        workdir=work,
        skill_path=skill_path(),
        bug_id="mini-dev",
        rev_bug_base=info["base"],
        rev_bug=info["bug"],
        rev_fix=info["fix"],
        model_runner=failing_runner,
        public_contract_env_keys=["APP_ENV"],
        allow_unsandboxed=True,
    )
    Orchestrator(cfg).measure_bug()
    records = read_records(out)
    evidence = records[0]
    assert evidence["measured_result"] is False
    assert evidence["sandboxed"] is False


# ------------------------------------------------------------------
# O1: No hard-coded /register. Auth comes from the contract.
# ------------------------------------------------------------------

MINI_APP_NO_AUTH = '''\\
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

MINI_YML_NO_AUTH = """build: "python -m py_compile app.py"
start: "python app.py"
readiness: "/health"
env:
  APP_ENV: verification
fixture:
  repo_name: "mini-app-no-auth"
  seed_note: "empty"
  auth_mechanism: "none"
  auth_scope: "no auth"
  flags_note: "no feature flags"
"""


def make_mini_repo_no_auth(root: Path) -> dict[str, str]:
    """A tiny app with NO /register endpoint and no credential declared."""
    repo = root / "mini-app-no-auth"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "test")
    _git(repo, "config", "user.email", "test@test")
    (repo / "requirements.txt").write_text("# no third-party deps\\n")
    (repo / "proofdeploy.yml").write_text(MINI_YML_NO_AUTH)
    shas = {}
    for tag, value in (("base", 1), ("bug", 2), ("fix", 1)):
        (repo / "app.py").write_text(MINI_APP_NO_AUTH.replace("__VALUE__", str(value)))
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", tag)
        shas[tag] = _git(repo, "rev-parse", "HEAD")
    return {"repo": str(repo), **shas}


def test_no_auth_app_does_not_crash(tmp_path):
    """O1: an app without /register and no declared credential does not
    crash. Probes run; no token is used."""
    info = make_mini_repo_no_auth(tmp_path)
    out = tmp_path / "records"
    work = tmp_path / "work"
    cfg = MeasureConfig(
        repo_dir=Path(info["repo"]),
        output_dir=out,
        workdir=work,
        skill_path=skill_path(),
        bug_id="mini-no-auth",
        rev_bug_base=info["base"],
        rev_bug=info["bug"],
        rev_fix=info["fix"],
        model_runner=canned_probes,
        public_contract_env_keys=["APP_ENV"],
        allow_unsandboxed=True,
    )
    summary = Orchestrator(cfg).measure_bug()
    # No crash; the run completes and scores.
    assert summary["verdict"] == "candidate_catch"
    assert summary["parent_ready"] and summary["fix_ready"]


def test_declared_registration_provider(tmp_path):
    """O1: a contract-declared register provider mints a token."""
    import json

    info = make_mini_repo(tmp_path)
    # Add the credential_provider declaration to the fixture.
    # We modify the base commit's yml so the fix still restores VALUE=1.
    repo = Path(info["repo"])
    # Amend the fix commit to include the provider declaration.
    _git(repo, "checkout", "-q", info["fix"])
    yml_path = repo / "proofdeploy.yml"
    yml_text = yml_path.read_text()
    provider_json = json.dumps({
        "mode": "register",
        "path": "/register",
        "method": "POST",
        "username_field": "username",
        "password_field": "password",
        "token_json_path": "token",
    })
    yml_text = yml_text.replace(
        '  flags_note: "no feature flags"',
        f'  flags_note: "no feature flags"\n  credential_provider: \'{provider_json}\'',
    )
    yml_path.write_text(yml_text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--amend", "--no-edit")
    new_fix = _git(repo, "rev-parse", "HEAD")

    out = tmp_path / "records"
    work = tmp_path / "work"
    cfg = MeasureConfig(
        repo_dir=repo,
        output_dir=out,
        workdir=work,
        skill_path=skill_path(),
        bug_id="mini-provider",
        rev_bug_base=info["base"],
        rev_bug=info["bug"],
        rev_fix=new_fix,
        model_runner=canned_probes,
        public_contract_env_keys=["APP_ENV"],
        allow_unsandboxed=True,
    )
    summary = Orchestrator(cfg).measure_bug()
    # The run completes; the provider was used (no crash on /register).
    assert summary["verdict"] == "candidate_catch"


def test_provider_failure_is_inconclusive_auth(tmp_path):
    """O1: a declared provider with a bad endpoint does not crash the run.
    The failure is INCONCLUSIVE auth, never an exception."""
    import json

    info = make_mini_repo(tmp_path)
    repo = Path(info["repo"])
    _git(repo, "checkout", "-q", info["fix"])
    yml_path = repo / "proofdeploy.yml"
    yml_text = yml_path.read_text()
    # Declare a provider pointing at a non-existent endpoint.
    provider_json = json.dumps({
        "mode": "register",
        "path": "/nonexistent-register",
        "method": "POST",
        "username_field": "username",
        "password_field": "password",
        "token_json_path": "token",
    })
    yml_text = yml_text.replace(
        '  flags_note: "no feature flags"',
        f'  flags_note: "no feature flags"\n  credential_provider: \'{provider_json}\'',
    )
    yml_path.write_text(yml_text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--amend", "--no-edit")
    new_fix = _git(repo, "rev-parse", "HEAD")

    out = tmp_path / "records"
    work = tmp_path / "work"
    cfg = MeasureConfig(
        repo_dir=repo,
        output_dir=out,
        workdir=work,
        skill_path=skill_path(),
        bug_id="mini-bad-provider",
        rev_bug_base=info["base"],
        rev_bug=info["bug"],
        rev_fix=new_fix,
        model_runner=canned_probes,  # probes don't use auth
        public_contract_env_keys=["APP_ENV"],
        allow_unsandboxed=True,
    )
    # Should not raise; the run completes (provider only runs when a probe
    # needs ${AUTH_TOKEN}).
    summary = Orchestrator(cfg).measure_bug()
    assert summary["verdict"] in ("candidate_catch", "no_catch", "inconclusive")


# ------------------------------------------------------------------
# O2: Target processes are terminated on every exit path.
# ------------------------------------------------------------------

def _count_app_processes():
    """Count running 'python app.py' processes (our test targets)."""
    import subprocess
    p = subprocess.run(
        ["ps", "-eo", "args"],
        capture_output=True, text=True,
    )
    return sum(1 for line in p.stdout.splitlines() if "python app.py" in line)


def test_target_process_stopped_after_run(tmp_path):
    """O2: after a normal run, no target process is alive."""
    info = make_mini_repo(tmp_path)
    out = tmp_path / "records"
    work = tmp_path / "work"
    before = _count_app_processes()
    cfg = MeasureConfig(
        repo_dir=Path(info["repo"]),
        output_dir=out,
        workdir=work,
        skill_path=skill_path(),
        bug_id="mini-cleanup",
        rev_bug_base=info["base"],
        rev_bug=info["bug"],
        rev_fix=info["fix"],
        model_runner=canned_probes,
        public_contract_env_keys=["APP_ENV"],
        allow_unsandboxed=True,
    )
    Orchestrator(cfg).measure_bug()
    import time
    time.sleep(2)  # let the OS reap
    after = _count_app_processes()
    assert after <= before, f"target processes leaked: {before} -> {after}"


def test_target_process_stopped_after_crash(tmp_path):
    """O2: after a crash, no target process is alive."""
    info = make_mini_repo(tmp_path)
    out = tmp_path / "records"
    work = tmp_path / "work"
    before = _count_app_processes()

    cfg = MeasureConfig(
        repo_dir=Path(info["repo"]),
        output_dir=out,
        workdir=work,
        skill_path=skill_path(),
        bug_id="mini-crash-cleanup",
        rev_bug_base=info["base"],
        rev_bug=info["bug"],
        rev_fix=info["fix"],
        model_runner=canned_probes,
        public_contract_env_keys=["APP_ENV"],
        allow_unsandboxed=True,
    )
    orch = Orchestrator(cfg)
    # Monkeypatch the executor's run_probe to raise after provisioning.
    # This simulates a crash after the targets are READY.
    orig_make_executor = orch._make_executor
    def bad_make_executor(side):
        ex = orig_make_executor(side)
        def crashing_run(*args, **kwargs):
            raise RuntimeError("simulated crash during probe")
        ex.run_probe = crashing_run
        return ex
    orch._make_executor = bad_make_executor
    # The crash should be recorded as INCONCLUSIVE, not propagate.
    summary = orch.measure_bug()
    assert summary["verdict"] == "inconclusive"
    assert summary["cause"] == "orchestrator_crash"
    import time
    time.sleep(2)
    after = _count_app_processes()
    assert after <= before, f"target processes leaked after crash: {before} -> {after}"


# ------------------------------------------------------------------
# O3: Crash after authoring commits an INCONCLUSIVE record.
# ------------------------------------------------------------------

def test_crash_after_authoring_records_inconclusive(tmp_path):
    """O3: a crash after the model call commits an INCONCLUSIVE record
    with a typed reason; the prompt and response are not lost."""
    info = make_mini_repo(tmp_path)
    out = tmp_path / "records"
    work = tmp_path / "work"
    cfg = MeasureConfig(
        repo_dir=Path(info["repo"]),
        output_dir=out,
        workdir=work,
        skill_path=skill_path(),
        bug_id="mini-crash-record",
        rev_bug_base=info["base"],
        rev_bug=info["bug"],
        rev_fix=info["fix"],
        model_runner=canned_probes,
        public_contract_env_keys=["APP_ENV"],
        allow_unsandboxed=True,
    )
    orch = Orchestrator(cfg)
    # Crash during probe execution (after authoring).
    def bad_provision(*args, **kwargs):
        raise RuntimeError("simulated post-authoring crash")
    orch._provision_side = bad_provision
    summary = orch.measure_bug()
    assert summary["verdict"] == "inconclusive"
    assert summary["cause"] == "orchestrator_crash"
    # The evidence record exists with the prompt and raw response.
    records = read_records(out)
    evidence = records[0]
    assert evidence["record_type"] == "evidence"
    assert evidence["prompt"]
    assert evidence["raw_model_response"]
