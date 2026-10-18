"""The ``proofdeploy verify`` engine (plan 1.10, WO-1.10 / D81).

Diff in, verdict out, for ONE change in a developer's own repo:

1. build the author bundle from the ``from``..``to`` diff plus a
   history-free snapshot at ``to`` (the orchestrator's bundle step,
   which runs the fail-closed leak check before anything else);
2. author probes with the PINNED model client;
3. provision ``to`` in the rootless sandbox (the orchestrator's
   measured-path sandbox provisioning — network cut after install,
   writes only on the disposable instance);
4. execute the probes on that instance;
5. write the evidence record (the result-contract record, redacted
   at write time exactly like a measured run's);
6. compute the verdict by the registered fail-closed rule and
   report it with a fixed exit code.

This module is a THIN WRAPPER over the existing engine: bundle,
leak check, model call, probe validation, provisioning, execution,
record building and record writing are the orchestrator's and
runpair's own code paths, shared with ``measure``. The only new
logic is the verify verdict rollup and the verify output shape,
both registered by WO-1.10. A verify run is never a measured
result (its evidence record carries ``measured_result=False``).

Verdict rule (fail-closed): any FAIL -> FAIL; else any
INCONCLUSIVE -> INCONCLUSIVE with that probe's typed reason; all
PASS -> PASS; no valid probe -> INCONCLUSIVE with reason ``probe``.
Every probe is reported individually, invalid author blocks
included (never sent, INCONCLUSIVE ``probe``). INCONCLUSIVE is
never shown as a pass: it has its own exit code.

Typed reasons and causes (D84): an INCONCLUSIVE output's
``typed_reason`` is ALWAYS one of the four registered reason
classes — ``environment`` / ``auth`` / ``drift`` / ``probe``, the
governance classes of the per-probe reason (only ``environment``
may ever be admin-relaxed to "warn"). The specific run-level cause
rides in the separate ``cause`` field, required (non-empty) on
INCONCLUSIVE and null otherwise: ``model_transport`` (class
environment), ``model_overflow`` (probe), ``author_output_invalid``
(probe), ``leak_check`` (probe — a refused author bundle; it never
surfaces as a typed reason and its class can never be relaxed),
``provisioning_failed`` (environment), ``probe_inconclusive``
(the winning probe's own class). A free-form run-level string is
never a typed reason.

Exit codes (registered, pinned by tests):

    0  PASS
    1  FAIL
    2  INCONCLUSIVE
    3  Not supported (typed)
    4  Usage / config error
    5  Internal error (our gap; traceback logged)

The machine-readable output is versioned
(``verify_output_version``) and schema'd at
``docs/contract/verify-output.schema.json``; readers of it pass
the shared reader-side version check (proofdeploy.versions), the
same gate that now guards result-contract records.
"""

from __future__ import annotations

import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

from proofdeploy import __version__
from proofdeploy.executor import ProbeResult, Verdict
from proofdeploy.leakcheck import SecretLeakError, bundle_manifest_hash
from proofdeploy.model_client import (
    MODEL_ID,
    TEMPERATURE,
    ModelCallError,
    ModelRunner,
    author_prompt_builder_sha256,
    author_prompt_sha256,
)
from proofdeploy.orchestrator import (
    PUBLIC_FIXTURE_KEYS,
    MeasureConfig,
    Orchestrator,
    OrchestratorError,
    _free_port,
    _invalid_block_results,
)
from proofdeploy.probe import SCHEMA_VERSION
from proofdeploy.runner import Provisioner
from proofdeploy.runpair import (
    build_evidence_record,
    redact_secrets,
    write_evidence_record,
)
from proofdeploy.sandbox import SandboxApp
from proofdeploy.versions import check_schema_version
from proofdeploy.yml import load_contract

#: Version of the verify output object (this module's format).
VERIFY_OUTPUT_VERSION = "1.0.0"

# Registered exit codes.
EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_INCONCLUSIVE = 2
EXIT_NOT_SUPPORTED = 3
EXIT_USAGE = 4
EXIT_INTERNAL = 5

_VERDICT_EXIT_CODES = {
    "PASS": EXIT_PASS,
    "FAIL": EXIT_FAIL,
    "INCONCLUSIVE": EXIT_INCONCLUSIVE,
}

#: Engine identity stamped on every verify output.
ENGINE = f"proofdeploy/{__version__}"

# Registered INCONCLUSIVE cause tokens (D84): the specific
# run-level cause an INCONCLUSIVE output carries in its ``cause``
# field, beside the four-class ``typed_reason``.
CAUSE_LEAK_CHECK = "leak_check"
CAUSE_MODEL_TRANSPORT = "model_transport"
CAUSE_MODEL_OVERFLOW = "model_overflow"
CAUSE_AUTHOR_OUTPUT_INVALID = "author_output_invalid"
CAUSE_PROVISIONING_FAILED = "provisioning_failed"
CAUSE_PROBE_INCONCLUSIVE = "probe_inconclusive"

#: The typed-reason class of a model failure, by cause (D84): a
#: transport failure is the environment's; an overflow is the
#: authoring side's (the bundle could not be authored from), class
#: probe. ModelCallError validates its cause, so this is total.
_MODEL_FAILURE_CLASSES = {
    CAUSE_MODEL_TRANSPORT: "environment",
    CAUSE_MODEL_OVERFLOW: "probe",
}

# Stage 1 typed refusals (exit 3). The exact strings are the
# contract: each names what is not supported and where it lands.
NOT_SUPPORTED_TARGET_URL = (
    "not supported yet: verifying an external target "
    "(Stage 1 verifies sandboxed instances only)"
)
NOT_SUPPORTED_HTML = "not supported yet: HTML verdict pages (Product v1)"
NOT_SUPPORTED_MODEL = "not supported yet: model selection (parked, O11)"
NOT_SUPPORTED_NO_CONTRACT = (
    "not supported yet: no proofdeploy.yml (init arrives in Product v1)"
)


def compute_verdict(results: list[ProbeResult]) -> tuple[str, str | None]:
    """The registered fail-closed rollup over one side's results.

    Returns (verdict, typed_reason). FAIL beats INCONCLUSIVE beats
    PASS; an INCONCLUSIVE verdict carries the FIRST inconclusive
    probe's typed reason; no results at all (no valid probe and no
    invalid block to report) is INCONCLUSIVE ``probe``.
    """
    for r in results:
        if r.verdict is Verdict.FAIL:
            return "FAIL", None
    for r in results:
        if r.verdict is Verdict.INCONCLUSIVE:
            return "INCONCLUSIVE", r.reason.value if r.reason else "environment"
    if not results:
        return "INCONCLUSIVE", "probe"
    return "PASS", None


def rollup_cause(results: list[ProbeResult]) -> str:
    """The run-level cause token for a rollup INCONCLUSIVE (D84).

    The winning (first) inconclusive probe decides: a probe that
    was never sent is an invalid author block — the engine's
    ``author_output_invalid``; a probe that ran and could not be
    decided is ``probe_inconclusive`` (its specifics live in that
    probe's own details, and its class is the verdict's typed
    reason). No results at all is the rollup's no-valid-probe case:
    ``author_output_invalid``.
    """
    for r in results:
        if r.verdict is Verdict.INCONCLUSIVE:
            return CAUSE_PROBE_INCONCLUSIVE if r.sent else CAUSE_AUTHOR_OUTPUT_INVALID
    return CAUSE_AUTHOR_OUTPUT_INVALID


def stop_output(
    *,
    exit_code: int,
    typed_reason: str,
    cause: str | None = None,
    detail: str | None = None,
    refs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A verify output for a run that stopped before a verdict: a
    refusal (exit 3), a usage/config error (exit 4), or an internal
    error (exit 5). Same shape as a verdict output, with ``verdict``
    null and no probes or record. The one stop that DOES carry a
    verdict — the leak refusal — passes verdict INCONCLUSIVE with
    a four-class typed reason and its ``cause`` (D84)."""
    return {
        "verify_output_version": VERIFY_OUTPUT_VERSION,
        "verdict": None,
        "exit_code": exit_code,
        "typed_reason": typed_reason,
        "cause": cause,
        "detail": detail,
        "probes": [],
        "refs": refs
        or {"repo": None, "from_ref": None, "to_ref": None,
            "from_sha": None, "to_sha": None},
        "model": MODEL_ID,
        "probe_set_sha256": None,
        "engine": ENGINE,
        "isolation_mode": None,
        "evidence_record": None,
        "record_schema_version": None,
        "timings": {},
    }


def load_verify_output(path: str | Path) -> dict[str, Any]:
    """Read a verify output file, through the shared reader-side
    version check: an output stamped with an unknown
    ``verify_output_version`` major is refused, never defaulted."""
    import json

    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"verify output at {path} is not a JSON object")
    check_schema_version(data.get("verify_output_version"), kind="verify output")
    return data


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, timeout=60,
    )


def _resolve_ref(repo: Path, ref: str) -> str | None:
    r = _git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}")
    return r.stdout.strip() if r.returncode == 0 else None


def _default_branch(repo: Path) -> str | None:
    """The repo's default branch: origin/HEAD when set, else a
    local main, else master, else None."""
    r = _git(repo, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    if r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip().removeprefix("origin/")
    for name in ("main", "master"):
        if _git(repo, "rev-parse", "--verify", f"{name}^{{commit}}").returncode == 0:
            return name
    return None


class _VerifyRun:
    """One verify run over the orchestrator's engine steps."""

    def __init__(
        self,
        *,
        repo_dir: Path,
        from_sha: str,
        to_sha: str,
        refs: dict[str, Any],
        output_dir: Path | None,
        workdir: Path | None,
        model_runner: ModelRunner | None,
        extra_secrets: list[str],
    ) -> None:
        self.repo_dir = repo_dir
        self.from_sha = from_sha
        self.to_sha = to_sha
        self.refs = refs
        self.unit_id = f"verify-{to_sha[:12]}-{uuid.uuid4().hex[:8]}"
        records_dir = output_dir or (repo_dir / ".proofdeploy" / "records")
        work_root = workdir or (repo_dir / ".proofdeploy" / "work")
        self.unit_dir = work_root / self.unit_id
        self.cfg = MeasureConfig(
            repo_dir=repo_dir,
            output_dir=records_dir,
            workdir=work_root,
            no_skill=True,
            model_runner=model_runner,
            extra_secrets=list(extra_secrets),
        )
        self.orch = Orchestrator(self.cfg)
        self.timings: dict[str, float] = {}
        self.attempts: list[str] = []

    # -- record writing ------------------------------------------------

    def _write_record(
        self,
        *,
        prompt: str | None,
        raw: Any,
        probeset: Any,
        manifest: dict[str, Any],
        manifest_sha: str,
        truncation_flags: dict[str, Any],
        side: Any,
        results: list[ProbeResult] | None,
        inconclusive_cause: str | None = None,
        inconclusive_error: str | None = None,
    ) -> tuple[str, Path, dict[str, Any]]:
        """Build and write the evidence record for this verify run.

        The single-side shape the result contract already defines:
        run_kind "clean" fields (c_sha/c_provision/c_results) hold
        the verified side; ``measured_result`` is False — a verify
        run never counts as a measured result.
        """
        orch = self.orch
        if side is not None:
            fixture = dict(side.contract.fixture)
            contract_env = dict(side.contract.env)
            c_provision = side.provision.to_dict()
            sandbox_evidence = {"verify": side.sandbox_evidence or {}}
            write_grants = {"verify": orch._write_grant(side)}
            auth_bootstrap = {"verify": orch._auth_bootstrap(side)}
            secret_values = orch._record_secret_values([side])
        else:
            # No side was provisioned (the run stopped at authoring):
            # the contract still comes from the bundle's snapshot, so
            # the record's redaction channels see the same values.
            try:
                contract = load_contract(
                    self.unit_dir / "snapshot-tip" / "proofdeploy.yml"
                )
                fixture = dict(contract.fixture)
                contract_env = dict(contract.env)
            except Exception:
                fixture, contract_env = {}, {}
            c_provision = {}
            sandbox_evidence = {}
            write_grants = {}
            auth_bootstrap = {}
            secret_values = orch._record_secret_values([])
        record = build_evidence_record(
            run_kind="clean",
            clean_id=self.unit_id,
            c_sha=self.to_sha,
            c_provision=c_provision,
            c_results=results,
            prompt=prompt,
            raw_model_response=raw.content if raw is not None else None,
            parsed_probes=probeset.probes if probeset is not None else [],
            invalid_probe_blocks=(
                probeset.invalid_blocks if probeset is not None else []
            ),
            bundle_manifest=manifest,
            bundle_manifest_sha256=manifest_sha,
            model_id=MODEL_ID,
            model_temperature=TEMPERATURE,
            model_attempts=max(1, len(self.attempts)),
            model_call_attempts=self.attempts or None,
            model_request=raw.request_record if raw is not None else {},
            model_finish_reason=(
                raw.finish_reason if raw is not None else None
            ),
            model_usage=raw.usage if raw is not None else None,
            author_prompt_sha256=author_prompt_sha256(),
            author_prompt_builder_sha256=author_prompt_builder_sha256(),
            skill_sha256=None,
            truncation_flags=truncation_flags,
            sandbox_evidence=sandbox_evidence,
            write_grants=write_grants,
            auth_bootstrap=auth_bootstrap,
            sandboxed=True,
            measured_result=False,
            provenance=self.orch._provenance({}),
            runner_log=list(self.orch.log),
            fixture=fixture,
            contract_env=contract_env,
            public_fixture_keys=list(PUBLIC_FIXTURE_KEYS),
            public_contract_env_keys=list(self.cfg.public_contract_env_keys),
            # D220: verify never uses the contract store, so its
            # helper yields the declared-public subset its containers
            # were filtered to; no side = no container = empty.
            container_env_keys=(
                sorted(orch._container_contract_env(side.contract))
                if side is not None
                else []
            ),
            inconclusive_cause=inconclusive_cause,
            inconclusive_error=inconclusive_error,
        )
        records_repo = self.orch._init_records_repo()
        record_id, path, _sha = write_evidence_record(
            record, records_repo, records_repo, secret_values=secret_values
        )
        return record_id, path, record

    # -- output assembly -------------------------------------------------

    def _output(
        self,
        *,
        verdict: str,
        typed_reason: str | None,
        results: list[ProbeResult],
        record_id: str | None,
        record_path: Path | None,
        probe_set_sha256: str | None,
        isolation_mode: str | None,
        cause: str | None = None,
        detail: str | None = None,
    ) -> dict[str, Any]:
        output: dict[str, Any] = {
            "verify_output_version": VERIFY_OUTPUT_VERSION,
            "verdict": verdict,
            "exit_code": _VERDICT_EXIT_CODES[verdict],
            "typed_reason": typed_reason,
            "cause": cause,
            "detail": detail,
            "probes": [r.to_dict() for r in results],
            "refs": self.refs,
            "model": MODEL_ID,
            "probe_set_sha256": probe_set_sha256,
            "engine": ENGINE,
            "isolation_mode": isolation_mode,
            "evidence_record": (
                {"path": str(record_path), "record_id": record_id}
                if record_id is not None and record_path is not None
                else None
            ),
            "record_schema_version": (
                SCHEMA_VERSION if record_id is not None else None
            ),
            "timings": dict(self.timings),
        }
        # No secret values in any output: redact by value, with the
        # same fail-closed pool the record uses (configured extras,
        # per-run generated credentials, every captured binding and
        # minted token the probes produced).
        values = list(self.cfg.extra_secrets)
        for r in results:
            values.extend(str(v) for v in r.captured_bindings.values())
            values.extend(r.minted_tokens)
            values.extend(r.bootstrap_values)
        redacted: dict[str, Any] = redact_secrets(output, values)
        return redacted

    # -- the run ---------------------------------------------------------

    def run(self) -> tuple[dict[str, Any], int]:
        orch = self.orch
        started = time.monotonic()
        self.unit_dir.mkdir(parents=True, exist_ok=True)

        # Steps 1-2: bundle + leak check (inside _build_bundle,
        # fail-closed before any model call).
        t0 = time.monotonic()
        try:
            bundle, manifest, prompt, _diff, _base, _tip, truncation_flags = (
                orch._build_bundle(
                    self.unit_dir,
                    unit_id=self.unit_id,
                    rev_base=self.from_sha,
                    rev_tip=self.to_sha,
                )
            )
        except SecretLeakError as exc:
            # A leak stop writes NO record: the bundle is tainted,
            # and persisting its prompt is exactly what the check
            # exists to prevent. The refusal is the evidence. Its
            # class is `probe` (D84): a refused author bundle, with
            # the specific cause in `cause` — never a typed reason,
            # and never a relaxable class.
            out = stop_output(
                exit_code=EXIT_INCONCLUSIVE,
                typed_reason="probe",
                cause=CAUSE_LEAK_CHECK,
                detail=(
                    "leak check refused the author bundle before any "
                    f"model call: {exc}"
                ),
                refs=self.refs,
            )
            out["verdict"] = "INCONCLUSIVE"
            return out, EXIT_INCONCLUSIVE
        except (OrchestratorError, ValueError) as exc:
            # Bundle-stage failures are the caller's inputs: an empty
            # diff, an unresolvable snapshot, an invalid contract.
            out = stop_output(
                exit_code=EXIT_USAGE,
                typed_reason="usage",
                detail=str(exc),
                refs=self.refs,
            )
            return out, EXIT_USAGE
        self.timings["bundle_s"] = round(time.monotonic() - t0, 3)
        manifest_sha = bundle_manifest_hash(bundle.manifest)

        # Step 3: author with the pinned client. Retry rule, as
        # registered for measured runs: a transport failure gets
        # exactly one retry; an overflow gets none.
        t0 = time.monotonic()
        probeset = None
        raw = None
        try:
            probeset, raw = self._call_author(prompt)
            self.attempts.append("attempt_1: success")
        except ModelCallError as exc:
            self.attempts.append(f"attempt_1: {exc.cause}: {exc}")
            if exc.cause == "model_transport":
                try:
                    probeset, raw = self._call_author(prompt)
                    self.attempts.append("attempt_2: success")
                except ModelCallError as exc2:
                    self.attempts.append(f"attempt_2: {exc2.cause}: {exc2}")
                    return self._model_failure(
                        prompt, manifest, manifest_sha, truncation_flags, exc2
                    )
            else:
                return self._model_failure(
                    prompt, manifest, manifest_sha, truncation_flags, exc
                )
        self.timings["author_s"] = round(time.monotonic() - t0, 3)
        assert probeset is not None and raw is not None

        # Step 4 (validation) already happened per-block inside the
        # author step. No valid probe: record and stop, INCONCLUSIVE
        # `probe` — provisioning never starts (Addendum 11 rule 6).
        if not probeset.probes:
            invalid_results = _invalid_block_results(probeset, start_index=0)
            record_id, path, record = self._write_record(
                prompt=prompt, raw=raw, probeset=probeset,
                manifest=manifest, manifest_sha=manifest_sha,
                truncation_flags=truncation_flags, side=None,
                results=invalid_results,
                # The record carries the engine's own cause for this
                # stop (orchestrator's author_output_invalid record).
                inconclusive_cause=CAUSE_AUTHOR_OUTPUT_INVALID,
                inconclusive_error=(
                    "author output contained no valid probe "
                    f"({len(probeset.invalid_blocks)} invalid block(s))"
                ),
            )
            self.timings["total_s"] = round(time.monotonic() - started, 3)
            out = self._output(
                verdict="INCONCLUSIVE", typed_reason="probe",
                cause=CAUSE_AUTHOR_OUTPUT_INVALID,
                results=invalid_results, record_id=record_id, record_path=path,
                probe_set_sha256=record["parsed_probes_sha256"],
                isolation_mode=None,
                detail="the author produced no valid probe",
            )
            return out, EXIT_INCONCLUSIVE

        # Steps 5-6: provision `to` in the rootless sandbox and
        # execute the probes on the disposable instance.
        orch._resolve_run_network_mode()
        results: list[ProbeResult] = []
        side = None
        t0 = time.monotonic()
        try:
            orch._run_lock_cutoff = orch._commit_cutoff(self.to_sha)
            side = orch._provision_side_sandbox(
                self.to_sha, _free_port(), self.unit_dir, "verify"
            )
            self.timings["provision_s"] = round(time.monotonic() - t0, 3)
            if side.provision.ready:
                t1 = time.monotonic()
                side.executor = orch._make_executor(side)
                try:
                    results = [
                        side.executor.run_probe(p, i)
                        for i, p in enumerate(probeset.probes)
                    ]
                finally:
                    side.executor.close()
                results = results + _invalid_block_results(
                    probeset, start_index=len(probeset.probes)
                )
                self.timings["execute_s"] = round(time.monotonic() - t1, 3)
        finally:
            orch._run_lock_cutoff = None
            if side is not None and side.provision and side.provision.run_id:
                proc = side.provision.proc
                if isinstance(proc, SandboxApp):
                    proc.stop()
                elif proc is not None:
                    runner = Provisioner(workdir=self.cfg.workdir / "provisioner")
                    runner.stop_target(proc, orch.log)
                    runner.cleanup_run(side.provision.run_id, orch.log)

        verdict: str
        typed_reason: str | None
        cause: str | None = None
        record_cause: str | None = None
        record_error: str | None = None
        if side is None or not side.provision.ready:
            verdict, typed_reason = "INCONCLUSIVE", "environment"
            cause = CAUSE_PROVISIONING_FAILED
            detail = (
                "the target could not be provisioned: "
                f"{side.provision.reason if side is not None else 'no side'}"
            )
            # The record carries the run-level cause too; the
            # provision reason prose is its error text.
            record_cause, record_error = cause, detail
        else:
            verdict, typed_reason = compute_verdict(results)
            if verdict == "INCONCLUSIVE":
                cause = rollup_cause(results)
            detail = None

        # Step 7-8: write the evidence record, then report.
        record_id, path, record = self._write_record(
            prompt=prompt, raw=raw, probeset=probeset,
            manifest=manifest, manifest_sha=manifest_sha,
            truncation_flags=truncation_flags, side=side,
            results=results,
            inconclusive_cause=record_cause,
            inconclusive_error=record_error,
        )
        self.timings["total_s"] = round(time.monotonic() - started, 3)
        isolation_mode = None
        if side is not None and side.sandbox_evidence:
            mode = side.sandbox_evidence.get("app_network_mode")
            isolation_mode = str(mode) if mode else "sandbox"
        elif side is not None:
            isolation_mode = "sandbox"
        out = self._output(
            verdict=verdict, typed_reason=typed_reason, cause=cause,
            results=results, record_id=record_id, record_path=path,
            probe_set_sha256=record["parsed_probes_sha256"],
            isolation_mode=isolation_mode, detail=detail,
        )
        return out, _VERDICT_EXIT_CODES[verdict]

    def _call_author(self, prompt: str) -> Any:
        """One author call through the orchestrator.

        The pinned client's own transport failure surfaces as a bare
        RuntimeError ("model API call failed: ..."); normalize it to
        the registered taxonomy — ModelCallError, cause
        model_transport — so the retry rule and the INCONCLUSIVE
        record see the same shape a measured run sees. An
        OrchestratorError (a RuntimeError subclass) is NOT a model
        failure and propagates untouched.
        """
        try:
            return self.orch._author_probes(prompt)
        except OrchestratorError:
            raise
        except ModelCallError:
            raise
        except RuntimeError as exc:
            raise ModelCallError(str(exc), cause="model_transport") from exc

    def _model_failure(
        self,
        prompt: str,
        manifest: dict[str, Any],
        manifest_sha: str,
        truncation_flags: dict[str, Any],
        exc: ModelCallError,
    ) -> tuple[dict[str, Any], int]:
        """The model call failed (after its one registered retry, if
        any): the evidence record carries the typed cause, and the
        verdict is INCONCLUSIVE whose typed reason is the cause's
        registered class (D84) — transport is environment, overflow
        is probe — with the specific cause in the output's ``cause``
        field."""
        record_id, path, record = self._write_record(
            prompt=prompt, raw=None, probeset=None,
            manifest=manifest, manifest_sha=manifest_sha,
            truncation_flags=truncation_flags, side=None, results=None,
            inconclusive_cause=exc.cause, inconclusive_error=str(exc),
        )
        out = self._output(
            verdict="INCONCLUSIVE",
            typed_reason=_MODEL_FAILURE_CLASSES[exc.cause],
            cause=exc.cause,
            results=[], record_id=record_id, record_path=path,
            probe_set_sha256=record["parsed_probes_sha256"],
            isolation_mode=None,
            detail=f"the model call failed: {exc}",
        )
        return out, EXIT_INCONCLUSIVE


def run_verify(
    *,
    repo_dir: Path,
    from_ref: str | None = None,
    to_ref: str = "HEAD",
    output_dir: Path | None = None,
    workdir: Path | None = None,
    model_runner: ModelRunner | None = None,
    extra_secrets: list[str] | None = None,
) -> tuple[dict[str, Any], int]:
    """Run one verify. Returns (output, exit_code).

    Expected stops (refusals, usage errors, leak/model/provisioning
    INCONCLUSIVEs) are encoded in the output; only a defect in
    ProofDeploy itself raises, for the CLI to report as exit 5.
    """
    refs: dict[str, Any] = {
        "repo": str(repo_dir),
        "from_ref": from_ref,
        "to_ref": to_ref,
        "from_sha": None,
        "to_sha": None,
    }
    if not repo_dir.is_dir():
        out = stop_output(
            exit_code=EXIT_USAGE, typed_reason="usage",
            detail=f"repo {repo_dir} is not a directory", refs=refs,
        )
        return out, EXIT_USAGE
    if _git(repo_dir, "rev-parse", "--git-dir").returncode != 0:
        out = stop_output(
            exit_code=EXIT_USAGE, typed_reason="usage",
            detail=f"repo {repo_dir} is not a git repository", refs=refs,
        )
        return out, EXIT_USAGE
    to_sha = _resolve_ref(repo_dir, to_ref)
    if to_sha is None:
        out = stop_output(
            exit_code=EXIT_USAGE, typed_reason="usage",
            detail=f"cannot resolve --to ref {to_ref!r}", refs=refs,
        )
        return out, EXIT_USAGE
    refs["to_sha"] = to_sha
    # The repo's own proofdeploy.yml at `to` is the contract; its
    # absence is a typed Stage 1 refusal, checked before any work.
    if _git(repo_dir, "cat-file", "-e", f"{to_sha}:proofdeploy.yml").returncode != 0:
        out = stop_output(
            exit_code=EXIT_NOT_SUPPORTED,
            typed_reason=NOT_SUPPORTED_NO_CONTRACT,
            refs=refs,
        )
        return out, EXIT_NOT_SUPPORTED
    if from_ref is not None:
        from_sha = _resolve_ref(repo_dir, from_ref)
        if from_sha is None:
            out = stop_output(
                exit_code=EXIT_USAGE, typed_reason="usage",
                detail=f"cannot resolve --from ref {from_ref!r}", refs=refs,
            )
            return out, EXIT_USAGE
    else:
        default = _default_branch(repo_dir)
        if default is None:
            out = stop_output(
                exit_code=EXIT_USAGE, typed_reason="usage",
                detail=(
                    "cannot determine the default branch for the "
                    "--from default (no origin/HEAD, no main/master)"
                ),
                refs=refs,
            )
            return out, EXIT_USAGE
        r = _git(repo_dir, "merge-base", default, to_sha)
        if r.returncode != 0:
            out = stop_output(
                exit_code=EXIT_USAGE, typed_reason="usage",
                detail=f"cannot compute merge-base with {default!r}",
                refs=refs,
            )
            return out, EXIT_USAGE
        from_sha = r.stdout.strip()
    refs["from_sha"] = from_sha

    run = _VerifyRun(
        repo_dir=repo_dir,
        from_sha=from_sha,
        to_sha=to_sha,
        refs=refs,
        output_dir=output_dir,
        workdir=workdir,
        model_runner=model_runner,
        extra_secrets=extra_secrets or [],
    )
    return run.run()
