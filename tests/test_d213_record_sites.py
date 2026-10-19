# ruff: noqa: E501  (long embedded stub JSON / fixture lines)
"""D213 (follow-up to D211, D57 cleanup): per-site tests for the
``model_finish_reason`` / ``model_usage`` record fields, D171 pattern.

D211 put the two fields on every evidence record (schema 1.7.0;
1.8.0 since D220). Its own tests drove ONE site (``measure_bug``'s
clean completion). D213 requires a test per record SITE, pinning:

- a clean completion records the response's finish reason (``stop``)
  and its usage object, exactly as returned;
- a crash / failure record records the representation that site
  actually produces: the fields ride along from the response when a
  response exists (crash-after-authoring, author-output-invalid),
  and are present-but-null when no response exists (the model call
  itself failed).

Sites covered (one test each):
  S1  orchestrator.measure_bug — clean completion record
  S2  orchestrator.measure_clean — clean completion record
  S3  orchestrator._record_author_output_invalid
  S4  orchestrator._record_orchestrator_crash (crash after authoring)
  S5  orchestrator._record_inconclusive (model call failed)
  S6  verify._write_record — with a model response
  S7  verify._write_record — model call failed (no response)

The model call is stubbed (injected runner) everywhere; everything
else is real, as in tests/test_orchestrator.py.
"""

import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from proofdeploy.model_client import ModelCallError, ModelResponse
from proofdeploy.orchestrator import (
    MeasureConfig,
    Orchestrator,
    OrchestratorError,
)
from proofdeploy.runpair import read_records
from proofdeploy.verify import run_verify

MINI_APP = '''\
import json
import os
import secrets
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace

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
  seed_note: "empty; create users with POST /register; GET /value reports v equal to 1 on a correct build"
  auth_mechanism: "Bearer token in the Authorization header"
  auth_scope: "a regular user token"
  flags_note: "no feature flags"
"""

_USAGE = {"prompt_tokens": 111, "completion_tokens": 22, "total_tokens": 133}

_PROBE_CONTENT = (
    "```json\n"
    '{"setup": [], '
    '"act": {"method": "GET", "path": "/value"}, '
    '"assert": [{"type": "body", "json_path": {"path": "v", "equals": 1}, "evidence": {"source": "fixture", "quote": "GET /value reports v equal to 1"}}]}'
    "\n```\n"
)


def _git(repo: Path, *args: str) -> str:
    p = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=True,
    )
    return p.stdout.strip()


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "d213-test")
    _git(repo, "config", "user.email", "d213@test.invalid")


def make_mini_repo(root: Path) -> dict[str, str]:
    """A tiny real app repo: base (VALUE=1) -> bug (VALUE=2) -> fix (VALUE=1)."""
    repo = root / "mini-app"
    _init_repo(repo)
    (repo / "requirements.txt").write_text("# no third-party deps\n")
    (repo / "proofdeploy.yml").write_text(MINI_YML)
    shas = {}
    for tag, value in (("base", 1), ("bug", 2), ("fix", 1)):
        (repo / "app.py").write_text(MINI_APP.replace("__VALUE__", str(value)))
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", tag)
        shas[tag] = _git(repo, "rev-parse", "HEAD")
    return {"repo": str(repo), **shas}


def skill_path() -> Path:
    return (
        Path(__file__).parent.parent
        / "docs" / "evaluation" / "skill" / "skill-author.md"
    )


def _stop_with_usage(prompt: str) -> ModelResponse:
    assert "mini-app" in prompt
    return ModelResponse(
        content=_PROBE_CONTENT, finish_reason="stop", usage=dict(_USAGE)
    )


def _no_block_with_usage(prompt: str) -> ModelResponse:
    assert "mini-app" in prompt
    return ModelResponse(
        content="I could not find anything worth probing here.",
        finish_reason="stop",
        usage=dict(_USAGE),
    )


def _transport_fail(prompt: str) -> ModelResponse:
    raise ModelCallError("timeout (test)", cause="model_transport")


def _bug_cfg(
    tmp_path: Path,
    info: dict[str, str],
    runner: Callable[[str], ModelResponse],
    bug_id: str = "mini-d213",
) -> tuple[MeasureConfig, Path]:
    out = tmp_path / "records"
    cfg = MeasureConfig(
        repo_dir=Path(info["repo"]),
        output_dir=out,
        workdir=tmp_path / "work",
        skill_path=skill_path(),
        bug_id=bug_id,
        rev_bug_base=info["base"],
        rev_bug=info["bug"],
        rev_fix=info["fix"],
        model_runner=runner,
        public_contract_env_keys=["APP_ENV"],
        allow_unsandboxed=True,
    )
    return cfg, out


def _evidence(out: Path) -> dict[str, Any]:
    records = read_records(out)
    return next(r for r in records if r["record_type"] == "evidence")


def _assert_stop_and_usage(evidence: dict[str, Any]) -> None:
    assert evidence["schema_version"] == "1.9.0"
    assert evidence["model_finish_reason"] == "stop"
    assert evidence["model_usage"] == _USAGE


def _assert_null_fields(evidence: dict[str, Any]) -> None:
    assert evidence["schema_version"] == "1.9.0"
    assert "model_finish_reason" in evidence
    assert evidence["model_finish_reason"] is None
    assert "model_usage" in evidence
    assert evidence["model_usage"] is None


# ---------------------------------------------------------------------------
# S1/S2: clean completions.
# ---------------------------------------------------------------------------


def test_s1_measure_bug_clean_completion_records_stop_and_usage(
    tmp_path: Path,
) -> None:
    """Site: measure_bug's completion record (D211 site)."""
    info = make_mini_repo(tmp_path)
    cfg, out = _bug_cfg(tmp_path, info, _stop_with_usage)
    summary = Orchestrator(cfg).measure_bug()
    assert summary["verdict"] == "candidate_catch"
    _assert_stop_and_usage(_evidence(out))


def test_s2_measure_clean_clean_completion_records_stop_and_usage(
    tmp_path: Path,
) -> None:
    """Site: measure_clean's completion record (D211 site)."""
    info = make_mini_repo(tmp_path)
    out = tmp_path / "records"
    cfg = MeasureConfig(
        repo_dir=Path(info["repo"]),
        output_dir=out,
        workdir=tmp_path / "work",
        skill_path=skill_path(),
        clean_id="mini-d213-clean",
        rev_clean=info["fix"],
        model_runner=_stop_with_usage,
        public_contract_env_keys=["APP_ENV"],
        allow_unsandboxed=True,
    )
    summary = Orchestrator(cfg).measure_clean()
    assert summary["run_kind"] == "clean"
    assert summary["false_alarm"] is False
    _assert_stop_and_usage(_evidence(out))


# ---------------------------------------------------------------------------
# S3/S4: a response exists; its fields ride into the failure record.
# ---------------------------------------------------------------------------


def test_s3_author_output_invalid_records_stop_and_usage(
    tmp_path: Path,
) -> None:
    """Site: _record_author_output_invalid — the model completed, its
    output held no valid probe; finish reason + usage still recorded."""
    info = make_mini_repo(tmp_path)
    cfg, out = _bug_cfg(tmp_path, info, _no_block_with_usage)
    summary = Orchestrator(cfg).measure_bug()
    assert summary["verdict"] == "no_catch"
    assert summary["cause"] == "author_output_invalid"
    evidence = _evidence(out)
    assert evidence["inconclusive_cause"] == "author_output_invalid"
    _assert_stop_and_usage(evidence)


def test_s4_orchestrator_crash_records_stop_and_usage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Site: _record_orchestrator_crash — a crash AFTER authoring; the
    crash record (cause tool_failure) keeps the response's finish
    reason + usage."""
    info = make_mini_repo(tmp_path)
    cfg, out = _bug_cfg(tmp_path, info, _stop_with_usage)
    orch = Orchestrator(cfg)
    orig_make_executor = orch._make_executor

    def bad_make_executor(side: Any) -> Any:
        ex = orig_make_executor(side)

        def crashing_run(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("simulated crash during probe (test)")

        monkeypatch.setattr(ex, "run_probe", crashing_run)
        return ex

    monkeypatch.setattr(orch, "_make_executor", bad_make_executor)
    summary = orch.measure_bug()
    assert summary["verdict"] == "inconclusive"
    assert summary["cause"] == "tool_failure"
    evidence = _evidence(out)
    assert evidence["inconclusive_cause"] == "tool_failure"
    _assert_stop_and_usage(evidence)


# ---------------------------------------------------------------------------
# S5: no response exists (the model call itself failed).
# ---------------------------------------------------------------------------


def test_s5_model_call_failure_records_null_finish_and_usage(
    tmp_path: Path,
) -> None:
    """Site: _record_inconclusive — transport failure on both attempts;
    the fields are present on the record and null."""
    info = make_mini_repo(tmp_path)
    cfg, out = _bug_cfg(tmp_path, info, _transport_fail)
    summary = Orchestrator(cfg).measure_bug()
    assert summary["verdict"] == "inconclusive"
    assert summary["cause"] == "model_transport"
    _assert_null_fields(_evidence(out))


# ---------------------------------------------------------------------------
# D251: the pinned client's bare RuntimeError is normalized on the
# measured path (one retry, then an INCONCLUSIVE record).
# ---------------------------------------------------------------------------

_HTTP_429 = "model API call failed: HTTP 429: rate limited (test)"


def _http_429_always(prompt: str) -> ModelResponse:
    raise RuntimeError(_HTTP_429)


def _http_429_once_then_ok(calls: list[str]) -> Callable[[str], ModelResponse]:

    def runner(prompt: str) -> ModelResponse:
        calls.append(prompt)
        if len(calls) == 1:
            raise RuntimeError(_HTTP_429)
        return _stop_with_usage(prompt)

    return runner


def _clean_cfg(
    tmp_path: Path, info: dict[str, str], runner: Callable[[str], ModelResponse]
) -> tuple[MeasureConfig, Path]:
    out = tmp_path / "records"
    cfg = MeasureConfig(
        repo_dir=Path(info["repo"]),
        output_dir=out,
        workdir=tmp_path / "work",
        skill_path=skill_path(),
        clean_id="mini-d251-clean",
        rev_clean=info["fix"],
        model_runner=runner,
        public_contract_env_keys=["APP_ENV"],
        allow_unsandboxed=True,
    )
    return cfg, out


def _assert_two_failed_attempts(evidence: dict[str, Any]) -> None:
    attempts = evidence["model_call_attempts"]
    assert len(attempts) == 2
    assert attempts[0].startswith("attempt_1: model_transport:")
    assert attempts[1].startswith("attempt_2: model_transport:")
    assert "HTTP 429" in attempts[0]


def test_d251_bug_bare_runtimeerror_records_inconclusive_after_one_retry(
    tmp_path: Path,
) -> None:
    info = make_mini_repo(tmp_path)
    cfg, out = _bug_cfg(tmp_path, info, _http_429_always)
    summary = Orchestrator(cfg).measure_bug()
    assert summary["verdict"] == "inconclusive"
    assert summary["cause"] == "model_transport"
    _assert_two_failed_attempts(_evidence(out))


def test_d251_clean_bare_runtimeerror_records_inconclusive_after_one_retry(
    tmp_path: Path,
) -> None:
    info = make_mini_repo(tmp_path)
    cfg, out = _clean_cfg(tmp_path, info, _http_429_always)
    summary = Orchestrator(cfg).measure_clean()
    assert summary["verdict"] == "inconclusive"
    assert summary["cause"] == "model_transport"
    _assert_two_failed_attempts(_evidence(out))


def test_d251_bug_transient_runtimeerror_then_success_proceeds(
    tmp_path: Path,
) -> None:
    info = make_mini_repo(tmp_path)
    calls: list[str] = []
    cfg, _ = _bug_cfg(tmp_path, info, _http_429_once_then_ok(calls))
    summary = Orchestrator(cfg).measure_bug()
    assert summary["verdict"] == "candidate_catch"
    # Exactly one retry. (The completion record does not carry the
    # attempts list; only the INCONCLUSIVE record does.)
    assert len(calls) == 2


def test_d251_clean_transient_runtimeerror_then_success_proceeds(
    tmp_path: Path,
) -> None:
    info = make_mini_repo(tmp_path)
    calls: list[str] = []
    cfg, _ = _clean_cfg(tmp_path, info, _http_429_once_then_ok(calls))
    summary = Orchestrator(cfg).measure_clean()
    assert summary["run_kind"] == "clean"
    assert summary["false_alarm"] is False
    # Exactly one retry. (The completion record does not carry the
    # attempts list; only the INCONCLUSIVE record does.)
    assert len(calls) == 2


def test_d251_orchestrator_error_and_model_call_error_propagate_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    info = make_mini_repo(tmp_path)
    cfg, _ = _bug_cfg(tmp_path, info, _stop_with_usage)
    orch = Orchestrator(cfg)

    def raise_orch(prompt: str, sources: Any) -> Any:
        raise OrchestratorError("internal (test)")

    monkeypatch.setattr(orch, "_author_probes", raise_orch)
    with pytest.raises(OrchestratorError, match="internal"):
        orch._call_author_probes("p", {})

    def raise_overflow(prompt: str, sources: Any) -> Any:
        raise ModelCallError("too big (test)", cause="model_overflow")

    monkeypatch.setattr(orch, "_author_probes", raise_overflow)
    with pytest.raises(ModelCallError) as ei:
        orch._call_author_probes("p", {})
    assert ei.value.cause == "model_overflow"


# ---------------------------------------------------------------------------
# S6/S7: the verify path's record site (_write_record).
# ---------------------------------------------------------------------------

_VERIFY_CONTRACT = (
    'build: "python -m py_compile app.py"\n'
    'start: "python app.py"\n'
    'readiness: "/health"\n'
    "fixture:\n"
    '  repo_name: "verify-test-app"\n'
    '  seed_note: "no seed data"\n'
    '  auth_mechanism: "no auth"\n'
    '  auth_scope: "no auth"\n'
    '  flags_note: "no feature flags"\n'
)

_VERIFY_PROBE = {
    "setup": [],
    "act": {"method": "GET", "path": "/health"},
    "assert": [
        {
            "type": "status",
            "equals": 200,
            "evidence": {"source": "fixture", "quote": "no seed data"},
        }
    ],
}


def _make_verify_repo(root: Path) -> tuple[Path, str, str]:
    """A two-commit repo for verify: app v1, then app v2 (+contract)."""
    repo = root / "verify-app"
    _init_repo(repo)
    (repo / "app.py").write_text(MINI_APP.replace("__VALUE__", "1"))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "one")
    sha1 = _git(repo, "rev-parse", "HEAD")
    (repo / "app.py").write_text(MINI_APP.replace("__VALUE__", "2"))
    (repo / "proofdeploy.yml").write_text(_VERIFY_CONTRACT)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "two")
    sha2 = _git(repo, "rev-parse", "HEAD")
    return repo, sha1, sha2


def _no_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    """The sandbox-less host surface, as in tests/test_verify.py."""
    from proofdeploy.sandbox import SandboxCapabilities

    monkeypatch.setattr(
        "proofdeploy.orchestrator.check_podman",
        lambda: (False, "podman absent (test)"),
    )
    monkeypatch.setattr(
        "proofdeploy.sandbox.check_sandbox_capabilities",
        lambda: SandboxCapabilities(reason="podman absent (test)"),
    )


def _verify_record(
    tmp_path: Path, output: dict[str, Any]
) -> dict[str, Any]:
    record_ref = output["evidence_record"]
    assert record_ref is not None
    loaded = read_records(tmp_path / "records")
    return next(r for r in loaded if r["record_id"] == record_ref["record_id"])


def test_s6_verify_record_with_response_records_stop_and_usage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Site: verify._write_record with a model response — the record
    carries the response's finish reason + usage."""
    _no_sandbox(monkeypatch)
    repo, sha1, sha2 = _make_verify_repo(tmp_path)

    def runner(prompt: str) -> ModelResponse:
        return ModelResponse(
            content="```json\n" + json.dumps(_VERIFY_PROBE) + "\n```\n",
            finish_reason="stop",
            usage=dict(_USAGE),
        )

    output, _code = run_verify(
        repo_dir=repo, from_ref=sha1, to_ref=sha2,
        output_dir=tmp_path / "records", workdir=tmp_path / "work",
        model_runner=runner,
    )
    _assert_stop_and_usage(_verify_record(tmp_path, output))


def test_s7_verify_record_without_response_records_nulls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Site: verify._write_record via _model_failure — the model call
    failed, no response exists; the fields are present and null."""
    def _no_model(prompt: str, runner: Any = None) -> ModelResponse:
        raise RuntimeError("model credential unavailable (test)")

    monkeypatch.setattr("proofdeploy.orchestrator.call_model", _no_model)
    repo, sha1, sha2 = _make_verify_repo(tmp_path)
    output, code = run_verify(
        repo_dir=repo, from_ref=sha1, to_ref=sha2,
        output_dir=tmp_path / "records", workdir=tmp_path / "work",
        model_runner=lambda prompt: ModelResponse(content="unused"),
    )
    assert code == 2
    assert output["verdict"] == "INCONCLUSIVE"
    assert output["cause"] == "model_transport"
    _assert_null_fields(_verify_record(tmp_path, output))
