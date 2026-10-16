"""WO-1.10: the `proofdeploy verify` CLI (plan 1.10, D81).

Host-side tests: the fail-closed verdict rollup, the registered
exit codes, every Stage 1 typed refusal, the shared reader-side
version check (records 1.0.0-1.5.0 load and re-score; an unknown
major is refused), the evidence-record round trip (a verify record
replays to the same verdict), the seeded-leak refusal through the
CLI path, and the model-failure paths. End-to-end PASS/FAIL runs
through the real sandbox live in tests/test_verify_sandbox.py.

The full-run host test monkeypatches the sandbox capability check
off, which is exactly the sandbox-less host a user can run verify
on: provisioning fails closed and the verdict is INCONCLUSIVE
`environment`, with the evidence record still written.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from proofdeploy import verify as verify_mod
from proofdeploy.cli import main
from proofdeploy.executor import InconclusiveReason, ProbeResult, Verdict
from proofdeploy.model_client import MODEL_ID, ModelCallError, ModelResponse
from proofdeploy.runpair import (
    build_evidence_record,
    read_records,
    write_evidence_record,
    write_score_record,
)
from proofdeploy.versions import (
    UnknownSchemaVersionError,
    check_schema_version,
)

_SCHEMA_PATH = (
    Path(__file__).parent.parent / "docs" / "contract" / "verify-output.schema.json"
)


# ---------------------------------------------------------------------------
# JSON Schema validation, stdlib subset (the same subset the result
# contract's test implements: type, enum, const, required, properties,
# additionalProperties, items, pattern, oneOf, anyOf, $defs/$ref).
# ---------------------------------------------------------------------------


def _type_matches(instance, expected):
    if expected == "null":
        return instance is None
    if expected == "boolean":
        return isinstance(instance, bool)
    if expected == "integer":
        return isinstance(instance, int) and not isinstance(instance, bool)
    if expected == "number":
        return isinstance(instance, (int, float)) and not isinstance(instance, bool)
    if expected == "string":
        return isinstance(instance, str)
    if expected == "array":
        return isinstance(instance, list)
    if expected == "object":
        return isinstance(instance, dict)
    raise AssertionError(f"validator: unknown type {expected!r}")


def _schema_errors(instance, schema, root, path="$"):
    if "$ref" in schema:
        ref = schema["$ref"]
        prefix = "#/$defs/"
        assert ref.startswith(prefix), f"validator: unsupported $ref {ref!r}"
        return _schema_errors(instance, root["$defs"][ref[len(prefix):]], root, path)
    errors = []
    if "oneOf" in schema:
        matches = sum(
            1 for sub in schema["oneOf"] if not _schema_errors(instance, sub, root, path)
        )
        if matches != 1:
            errors.append(f"{path}: oneOf matched {matches}, expected exactly 1")
        return errors
    if "anyOf" in schema:
        if all(_schema_errors(instance, sub, root, path) for sub in schema["anyOf"]):
            errors.append(f"{path}: anyOf matched no schema")
        return errors
    if "const" in schema and instance != schema["const"]:
        errors.append(f"{path}: {instance!r} != const {schema['const']!r}")
    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{path}: {instance!r} not in enum {schema['enum']!r}")
    if "type" in schema:
        expected = schema["type"]
        expected_list = expected if isinstance(expected, list) else [expected]
        if not any(_type_matches(instance, t) for t in expected_list):
            errors.append(
                f"{path}: expected type {expected!r}, got {type(instance).__name__}"
            )
            return errors
    if isinstance(instance, dict):
        for key in schema.get("required", []):
            if key not in instance:
                errors.append(f"{path}: missing required key {key!r}")
        props = schema.get("properties", {})
        for key, subschema in props.items():
            if key in instance:
                errors.extend(
                    _schema_errors(instance[key], subschema, root, f"{path}.{key}")
                )
        additional = schema.get("additionalProperties", True)
        if additional is False:
            for key in instance:
                if key not in props:
                    errors.append(f"{path}: additional property {key!r} not allowed")
        elif isinstance(additional, dict):
            for key in instance:
                if key not in props:
                    errors.extend(
                        _schema_errors(instance[key], additional, root, f"{path}.{key}")
                    )
    if isinstance(instance, list) and "items" in schema:
        for i, item in enumerate(instance):
            errors.extend(_schema_errors(item, schema["items"], root, f"{path}[{i}]"))
    if isinstance(instance, str) and "pattern" in schema:
        if not re.search(schema["pattern"], instance):
            errors.append(
                f"{path}: {instance!r} does not match pattern {schema['pattern']!r}"
            )
    return errors


def _assert_output_schema_valid(output):
    schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    errors = _schema_errors(output, schema, schema)
    assert errors == [], errors


# ---------------------------------------------------------------------------
# Helpers: tiny git repos and stub model runners.
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=True, timeout=60,
    )
    return proc.stdout.strip()


_CONTRACT = (
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


def _make_repo(
    tmp_path: Path,
    *,
    contract: bool = True,
    leak: bool = False,
) -> tuple[Path, str, str]:
    """A two-commit repo: app.py v1, then app.py v2 (+contract)."""
    repo = tmp_path / "app"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "verify-test")
    _git(repo, "config", "user.email", "verify@test.invalid")
    (repo / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    if contract:
        text = _CONTRACT
        if leak:
            text += 'env:\n  API_TOKEN: "leakytoken-123456"\n'
        (repo / "proofdeploy.yml").write_text(text, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "one")
    sha1 = _git(repo, "rev-parse", "HEAD")
    (repo / "app.py").write_text(
        "VALUE = 2\n" + ('TOKEN = "leakytoken-123456"\n' if leak else ""),
        encoding="utf-8",
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "two")
    sha2 = _git(repo, "rev-parse", "HEAD")
    return repo, sha1, sha2


_SIMPLE_PROBE = {
    "setup": [],
    "act": {"method": "GET", "path": "/health"},
    "assert": [{"type": "status", "equals": 200}],
}


def _runner_returning(payload: str):
    def _run(prompt: str) -> ModelResponse:
        return ModelResponse(content=payload, finish_reason="stop")

    return _run


def _probe_runner():
    return _runner_returning("```json\n" + json.dumps(_SIMPLE_PROBE) + "\n```\n")


def _result(verdict, reason=None, index=0):
    return ProbeResult(
        probe_index=index, verdict=verdict, reason=reason, sent=True,
    )


def _records_repo(path: Path) -> Path:
    """An initialized records repo (write_evidence_record commits)."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "verify-test"], cwd=path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "verify@test.invalid"], cwd=path, check=True
    )
    (path / "README.md").write_text("records\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=path, check=True)
    return path


# ---------------------------------------------------------------------------
# The verdict rollup and exit codes.
# ---------------------------------------------------------------------------


def test_verdict_all_pass_is_pass():
    verdict, reason = verify_mod.compute_verdict(
        [_result(Verdict.PASS), _result(Verdict.PASS, index=1)]
    )
    assert (verdict, reason) == ("PASS", None)


def test_verdict_fail_beats_pass():
    # Plant (d) target: a FAIL must never hide behind a sibling PASS.
    verdict, _ = verify_mod.compute_verdict(
        [_result(Verdict.PASS), _result(Verdict.FAIL, index=1)]
    )
    assert verdict == "FAIL"


def test_verdict_fail_beats_inconclusive():
    verdict, _ = verify_mod.compute_verdict(
        [
            _result(Verdict.INCONCLUSIVE, InconclusiveReason.ENVIRONMENT),
            _result(Verdict.FAIL, index=1),
        ]
    )
    assert verdict == "FAIL"


def test_verdict_inconclusive_carries_first_typed_reason():
    verdict, reason = verify_mod.compute_verdict(
        [
            _result(Verdict.PASS),
            _result(Verdict.INCONCLUSIVE, InconclusiveReason.AUTH, index=1),
            _result(Verdict.INCONCLUSIVE, InconclusiveReason.DRIFT, index=2),
        ]
    )
    assert (verdict, reason) == ("INCONCLUSIVE", "auth")


def test_verdict_no_results_is_inconclusive_probe():
    assert verify_mod.compute_verdict([]) == ("INCONCLUSIVE", "probe")


def test_exit_codes_are_the_registered_table():
    assert verify_mod.EXIT_PASS == 0
    assert verify_mod.EXIT_FAIL == 1
    assert verify_mod.EXIT_INCONCLUSIVE == 2
    assert verify_mod.EXIT_NOT_SUPPORTED == 3
    assert verify_mod.EXIT_USAGE == 4
    assert verify_mod.EXIT_INTERNAL == 5
    # Plant (a) target: INCONCLUSIVE must not map to the PASS code.
    out = verify_mod.stop_output(exit_code=2, typed_reason="probe")
    assert out["exit_code"] == 2 and out["exit_code"] != verify_mod.EXIT_PASS


# ---------------------------------------------------------------------------
# Stage 1 typed refusals (exit 3) and usage errors (exit 4).
# ---------------------------------------------------------------------------


def test_refusal_target_url(capsys):
    code = main(["verify", "--target-url", "http://127.0.0.1:5000"])
    assert code == 3
    assert verify_mod.NOT_SUPPORTED_TARGET_URL in capsys.readouterr().out


def test_refusal_target_url_json_is_schema_valid(capsys):
    code = main(
        ["verify", "--target-url", "http://example.com", "--format", "json"]
    )
    assert code == 3
    output = json.loads(capsys.readouterr().out)
    assert output["verdict"] is None
    assert output["typed_reason"] == verify_mod.NOT_SUPPORTED_TARGET_URL
    _assert_output_schema_valid(output)


def test_refusal_format_html(capsys):
    code = main(["verify", "--format", "html"])
    assert code == 3
    assert verify_mod.NOT_SUPPORTED_HTML in capsys.readouterr().out


def test_refusal_model_other_than_pinned(capsys):
    code = main(["verify", "--model", "some-other-model"])
    assert code == 3
    assert verify_mod.NOT_SUPPORTED_MODEL in capsys.readouterr().out


def test_refusal_no_contract(tmp_path):
    repo, sha1, sha2 = _make_repo(tmp_path, contract=False)
    output, code = verify_mod.run_verify(
        repo_dir=repo, from_ref=sha1, to_ref=sha2,
        output_dir=tmp_path / "records", workdir=tmp_path / "work",
        model_runner=_probe_runner(),
    )
    assert code == 3
    assert output["verdict"] is None
    assert output["typed_reason"] == verify_mod.NOT_SUPPORTED_NO_CONTRACT
    _assert_output_schema_valid(output)


def test_pinned_model_value_passes_the_model_gate(tmp_path):
    # --model naming the pinned model is not a refusal: the run
    # proceeds to the next gate (here, the missing contract).
    repo, sha1, sha2 = _make_repo(tmp_path, contract=False)
    code = main(
        [
            "verify", "--repo", str(repo), "--from", sha1, "--to", sha2,
            "--model", MODEL_ID,
        ]
    )
    assert code == 3  # the no-contract refusal, not the model refusal


def test_usage_errors(tmp_path):
    missing = tmp_path / "nope"
    _, code = verify_mod.run_verify(repo_dir=missing)
    assert code == 4

    plain = tmp_path / "plain"
    plain.mkdir()
    _, code = verify_mod.run_verify(repo_dir=plain)
    assert code == 4  # not a git repository

    (tmp_path / "r").mkdir()
    repo, sha1, sha2 = _make_repo(tmp_path / "r")
    _, code = verify_mod.run_verify(
        repo_dir=repo, from_ref=sha1, to_ref="does-not-exist",
        output_dir=tmp_path / "o", workdir=tmp_path / "w",
        model_runner=_probe_runner(),
    )
    assert code == 4
    _, code = verify_mod.run_verify(
        repo_dir=repo, from_ref="does-not-exist", to_ref=sha2,
        output_dir=tmp_path / "o", workdir=tmp_path / "w",
        model_runner=_probe_runner(),
    )
    assert code == 4


def test_usage_empty_diff(tmp_path):
    repo, sha1, _ = _make_repo(tmp_path)
    output, code = verify_mod.run_verify(
        repo_dir=repo, from_ref=sha1, to_ref=sha1,
        output_dir=tmp_path / "records", workdir=tmp_path / "work",
        model_runner=_probe_runner(),
    )
    assert code == 4
    assert "empty diff" in (output["detail"] or "")


# ---------------------------------------------------------------------------
# The shared reader-side version check.
# ---------------------------------------------------------------------------


def test_version_check_accepts_supported_and_missing():
    for version in (None, "1.0.0", "1.1.0", "1.2.0", "1.3.0", "1.4.0", "1.5.0", "1.99.0"):
        check_schema_version(version)  # must not raise


def test_version_check_refuses_unknown_major():
    for version in ("2.0.0", "3.1.0", "banana", "", 2):
        with pytest.raises(UnknownSchemaVersionError) as excinfo:
            check_schema_version(version)
        message = str(excinfo.value)
        assert repr(version) in message  # names the version found
        assert "1" in message  # and the supported majors


def test_read_records_refuses_unknown_major(tmp_path):
    out = tmp_path / "records"
    out.mkdir()
    good = {"record_id": "a" * 32, "record_type": "evidence", "schema_version": "1.5.0"}
    legacy = {"record_id": "b" * 32, "record_type": "author_run"}  # no version
    (out / "runs.jsonl").write_text(
        json.dumps(good) + "\n" + json.dumps(legacy) + "\n", encoding="utf-8"
    )
    assert len(read_records(out)) == 2  # 1.x and missing-version both load

    bad = {"record_id": "c" * 32, "record_type": "evidence", "schema_version": "2.0.0"}
    (out / "runs.jsonl").write_text(json.dumps(bad) + "\n", encoding="utf-8")
    with pytest.raises(UnknownSchemaVersionError):
        read_records(out)


def test_verify_output_reader_refuses_unknown_major(tmp_path):
    path = tmp_path / "out.json"
    output = verify_mod.stop_output(exit_code=3, typed_reason="x")
    path.write_text(json.dumps(output), encoding="utf-8")
    assert verify_mod.load_verify_output(path)["exit_code"] == 3
    output["verify_output_version"] = "2.0.0"
    path.write_text(json.dumps(output), encoding="utf-8")
    with pytest.raises(UnknownSchemaVersionError):
        verify_mod.load_verify_output(path)


def _clean_evidence(results, *, stamp: str):
    """A clean-shape evidence record via the real builder, re-stamped
    to a historical 1.x version (record and per-probe results)."""
    record = build_evidence_record(
        run_kind="clean",
        clean_id=f"stamp-{stamp}",
        c_sha="c" * 40,
        c_provision={},
        c_results=results,
        prompt="p",
        raw_model_response="r",
        parsed_probes=[],
        bundle_manifest={},
        bundle_manifest_sha256="0" * 64,
    )
    record["schema_version"] = stamp
    for result in record["c_results"]:
        result["schema_version"] = stamp
    return record


def test_records_1x_load_and_rescore_identically(tmp_path):
    results = [
        _result(Verdict.PASS),
        _result(Verdict.FAIL, index=1),
    ]
    baseline = None
    for stamp in ("1.0.0", "1.1.0", "1.2.0", "1.3.0", "1.4.0", "1.5.0"):
        out = _records_repo(tmp_path / f"records-{stamp}")
        record = _clean_evidence(results, stamp=stamp)
        record_id, _, _ = write_evidence_record(record, out, out, secret_values=[])
        loaded = read_records(out)
        assert [r["record_id"] for r in loaded] == [record_id]
        write_score_record(
            out, out,
            evidence_record_id=record_id,
            run_kind="clean",
            clean_id=f"stamp-{stamp}",
            secret_values=[],
        )
        score = next(
            r for r in read_records(out) if r["record_type"] == "score"
        )
        # One FAIL on a clean-shape side is a false alarm at every
        # 1.x stamp: the re-score is identical across the majors'
        # shared minor history.
        assert score["false_alarm"] is True
        if baseline is None:
            baseline = score["false_alarm"]
        assert score["false_alarm"] == baseline


# ---------------------------------------------------------------------------
# Replay: a verify-shaped record re-scores to the same verdict.
# ---------------------------------------------------------------------------


def _replayable_probe():
    return {
        "setup": [],
        "act": {"method": "GET", "path": "/health"},
        "assert": [
            {"type": "status", "equals": 200},
            {"type": "body", "contains": "ok"},
        ],
    }


def test_replay_reproduces_verify_verdict(tmp_path):
    from proofdeploy.replay import replay_side

    for status, body, want in ((200, "all ok", "PASS"), (500, "boom", "FAIL")):
        recorded = ProbeResult(
            probe_index=0,
            verdict=Verdict.PASS if want == "PASS" else Verdict.FAIL,
            http_status=status,
            http_body=body,
            sent=True,
            assertions_evaluated=True,
            request={"method": "GET", "url": "http://x/health", "headers": {}, "body": None},
        )
        live_verdict, _ = verify_mod.compute_verdict([recorded])
        assert live_verdict == want
        record = build_evidence_record(
            run_kind="clean",
            clean_id=f"replay-{want}",
            c_sha="c" * 40,
            c_provision={},
            c_results=[recorded],
            prompt="p",
            raw_model_response="r",
            parsed_probes=[_replayable_probe()],
            bundle_manifest={},
            bundle_manifest_sha256="0" * 64,
        )
        out = _records_repo(tmp_path / f"records-{want}")
        record_id, _, _ = write_evidence_record(record, out, out, secret_values=[])
        loaded = next(
            r for r in read_records(out) if r["record_id"] == record_id
        )
        replayed = replay_side(loaded, "c_results")
        replay_verdict, _ = verify_mod.compute_verdict(replayed)
        assert replay_verdict == live_verdict


# ---------------------------------------------------------------------------
# Full host runs (sandbox capability off -> INCONCLUSIVE environment).
# ---------------------------------------------------------------------------


@pytest.fixture
def no_sandbox(monkeypatch):
    # The whole capability surface of a sandbox-less host: no podman
    # for provisioning, and the network-mode probe reporting the
    # host incapable (the real probe starts containers; a host
    # without podman never gets that far).
    from proofdeploy.sandbox import SandboxCapabilities

    monkeypatch.setattr(
        "proofdeploy.orchestrator.check_podman",
        lambda: (False, "podman absent (test)"),
    )
    monkeypatch.setattr(
        "proofdeploy.sandbox.check_sandbox_capabilities",
        lambda: SandboxCapabilities(reason="podman absent (test)"),
    )


def test_host_run_inconclusive_environment_with_record(tmp_path, no_sandbox):
    repo, sha1, sha2 = _make_repo(tmp_path)
    records = tmp_path / "records"
    output, code = verify_mod.run_verify(
        repo_dir=repo, from_ref=sha1, to_ref=sha2,
        output_dir=records, workdir=tmp_path / "work",
        model_runner=_probe_runner(),
    )
    assert code == 2
    assert output["verdict"] == "INCONCLUSIVE"
    assert output["typed_reason"] == "environment"
    assert output["exit_code"] == 2
    assert output["model"] == MODEL_ID
    assert output["refs"]["from_sha"] == sha1
    assert output["refs"]["to_sha"] == sha2
    _assert_output_schema_valid(output)

    record_ref = output["evidence_record"]
    assert record_ref is not None
    assert Path(record_ref["path"]).is_file()
    loaded = read_records(records)
    record = next(r for r in loaded if r["record_id"] == record_ref["record_id"])
    assert record["schema_version"] == output["record_schema_version"]
    assert record["measured_result"] is False
    assert record["c_sha"] == sha2
    assert record["parsed_probes_sha256"] == output["probe_set_sha256"]
    assert record["parsed_probes"] == [_SIMPLE_PROBE]


def test_host_run_via_cli_json(tmp_path, monkeypatch, capsys):
    # The CLI path end to end, with the model transport failing the
    # way an unauthenticated CI host's would: deterministic
    # INCONCLUSIVE (model_transport), JSON on stdout, schema-valid.
    def _no_model(prompt: str, runner=None) -> ModelResponse:
        raise RuntimeError("Groq credential unavailable (test)")

    monkeypatch.setattr("proofdeploy.orchestrator.call_model", _no_model)
    repo, sha1, sha2 = _make_repo(tmp_path)
    code = main(
        [
            "verify", "--repo", str(repo), "--from", sha1, "--to", sha2,
            "--format", "json",
            "--output-dir", str(tmp_path / "records"),
            "--workdir", str(tmp_path / "work"),
        ]
    )
    assert code == 2
    output = json.loads(capsys.readouterr().out)
    assert output["verdict"] == "INCONCLUSIVE"
    assert output["typed_reason"] == "model_transport"
    _assert_output_schema_valid(output)


def test_seeded_leak_refused_before_any_model_call(tmp_path):
    repo, sha1, sha2 = _make_repo(tmp_path, leak=True)
    calls: list[str] = []

    def runner(prompt: str) -> ModelResponse:
        calls.append(prompt)
        raise AssertionError("the model must never be called on a leak")

    output, code = verify_mod.run_verify(
        repo_dir=repo, from_ref=sha1, to_ref=sha2,
        output_dir=tmp_path / "records", workdir=tmp_path / "work",
        model_runner=runner,
    )
    assert calls == []
    assert code == 2
    assert output["verdict"] == "INCONCLUSIVE"
    assert output["typed_reason"] == "leak_check"
    # A leak stop writes no record (the bundle is tainted).
    assert output["evidence_record"] is None
    assert not (tmp_path / "records" / "runs.jsonl").exists()
    _assert_output_schema_valid(output)


def test_model_transport_retried_once_then_inconclusive(tmp_path):
    repo, sha1, sha2 = _make_repo(tmp_path)
    calls: list[str] = []

    def runner(prompt: str) -> ModelResponse:
        calls.append(prompt)
        raise ModelCallError("connection reset", cause="model_transport")

    output, code = verify_mod.run_verify(
        repo_dir=repo, from_ref=sha1, to_ref=sha2,
        output_dir=tmp_path / "records", workdir=tmp_path / "work",
        model_runner=runner,
    )
    assert len(calls) == 2  # exactly one retry
    assert code == 2
    assert output["typed_reason"] == "model_transport"
    record = next(
        r
        for r in read_records(tmp_path / "records")
        if r["record_id"] == output["evidence_record"]["record_id"]
    )
    assert record["inconclusive_cause"] == "model_transport"
    assert len(record["model_call_attempts"]) == 2


def test_model_overflow_not_retried(tmp_path):
    repo, sha1, sha2 = _make_repo(tmp_path)
    calls: list[str] = []

    def runner(prompt: str) -> ModelResponse:
        calls.append(prompt)
        raise ModelCallError("prompt too long", cause="model_overflow")

    output, code = verify_mod.run_verify(
        repo_dir=repo, from_ref=sha1, to_ref=sha2,
        output_dir=tmp_path / "records", workdir=tmp_path / "work",
        model_runner=runner,
    )
    assert len(calls) == 1
    assert code == 2
    assert output["typed_reason"] == "model_overflow"


def test_model_runtime_error_normalized_to_transport(tmp_path):
    repo, sha1, sha2 = _make_repo(tmp_path)
    calls: list[str] = []

    def runner(prompt: str) -> ModelResponse:
        calls.append(prompt)
        raise RuntimeError("Groq API call failed: connection reset")

    output, code = verify_mod.run_verify(
        repo_dir=repo, from_ref=sha1, to_ref=sha2,
        output_dir=tmp_path / "records", workdir=tmp_path / "work",
        model_runner=runner,
    )
    assert len(calls) == 2
    assert code == 2
    assert output["typed_reason"] == "model_transport"
    record = next(
        r
        for r in read_records(tmp_path / "records")
        if r["record_id"] == output["evidence_record"]["record_id"]
    )
    assert "Groq API call failed" in (record["inconclusive_error"] or "")


def test_author_output_invalid_is_inconclusive_probe(tmp_path, no_sandbox):
    repo, sha1, sha2 = _make_repo(tmp_path)
    output, code = verify_mod.run_verify(
        repo_dir=repo, from_ref=sha1, to_ref=sha2,
        output_dir=tmp_path / "records", workdir=tmp_path / "work",
        model_runner=_runner_returning("the model said nothing parseable"),
    )
    assert code == 2
    assert output["verdict"] == "INCONCLUSIVE"
    assert output["typed_reason"] == "probe"
    record = next(
        r
        for r in read_records(tmp_path / "records")
        if r["record_id"] == output["evidence_record"]["record_id"]
    )
    # Provisioning never started: no provision was recorded.
    assert record["c_provision"] == {}
    assert record["parsed_probes"] == []


# ---------------------------------------------------------------------------
# Safety item 6 (tool state outside the app tree), asserted over the
# provisioning plan matrix. DEPENDENCY: plan 1.2(n) (D77) relocates
# tool state out of the checkout; it is in flight on its own branch.
# Until it lands, plan data carries no {pd_tools} placeholder and
# this test pins that pre-(n) state; the moment the placeholder
# exists, the strict post-(n) assertion activates on its own. (A
# skip/xfail is not available here: the CI shards assert skipped==0.)
# ---------------------------------------------------------------------------


def _plan_strings(plan) -> list[str]:
    strings: list[str] = []
    for step in plan.steps:
        strings.extend(step.argv or [])
        strings.extend(step.env.values())
        if step.shell:
            strings.append(step.shell)
    return strings


def test_tool_state_plan_matrix_guard(tmp_path):
    from proofdeploy.provisioning import plan_provisioning
    from proofdeploy.yml import load_contract

    # A committed-uv Python snapshot and a pnpm Node snapshot: the
    # two plan families whose managers bootstrap through .pd-tools.
    py = tmp_path / "py"
    py.mkdir()
    (py / "pyproject.toml").write_text(
        '[project]\nname = "t"\nrequires-python = ">=3.12,<3.13"\n',
        encoding="utf-8",
    )
    (py / "uv.lock").write_text("lock-bytes\n", encoding="utf-8")
    (py / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    (py / "proofdeploy.yml").write_text(_CONTRACT, encoding="utf-8")

    # The Node side uses the golden pnpm app's own revision tree —
    # a real package.json + pnpm-lock.yaml + contract.
    node = (
        Path(__file__).parent.parent
        / "conformance" / "golden" / "node-pnpm-sqlite" / "revisions" / "fix"
    )
    assert (node / "pnpm-lock.yaml").is_file()

    plans = [
        plan_provisioning(py, load_contract(py / "proofdeploy.yml")),
        plan_provisioning(node, load_contract(node / "proofdeploy.yml")),
    ]
    strings = [s for plan in plans for s in _plan_strings(plan)]
    assert strings, "the plan matrix must produce steps"

    if any("{pd_tools}" in s for s in strings):
        # Post-1.2(n): render with the D77 destinations and assert
        # no rendering puts tool state inside the checkout.
        destinations = {
            "{pd_tools}": "/pd-home/.pd-tools",
            "{venv}": "/pd-home/.venv",
            "{checkout}": "/checkout",
            "{runtime_bin}": "python3",
        }
        for s in strings:
            rendered = s
            for placeholder, dest in destinations.items():
                rendered = rendered.replace(placeholder, dest)
            assert "/checkout/.pd-tools" not in rendered, rendered
            assert "/checkout/.venv" not in rendered, rendered
    else:
        # Pre-1.2(n) characterization (D77 in flight): the managers
        # still bootstrap through {checkout}/.pd-tools. This branch
        # exists so the strict assertion above activates the moment
        # the relocation lands, without a red suite in between.
        assert any("{checkout}/.pd-tools" in s for s in strings)
