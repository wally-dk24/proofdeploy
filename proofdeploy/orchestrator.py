"""Measured-unit orchestrator (WO-5).

One command runs a measured unit end to end:

  (a) build the author bundle from the B^->B diff + B snapshot only,
      via the assembler in fixture.py;
  (b) call the registered model;
  (c) parse and validate the probes;
  (d) provision fix^ and fix (bugs) or C (clean diffs);
  (e) execute the probes;
  (f) write the evidence record (committed before scoring);
  (g) score, commit the score with commit_records.

Hard rules from the reviewer, enforced here, not by convention:

- Every score is committed with commit_records.
- cleanup_run is called for every provisioned instance, on every exit
  path (a READY run keeps its scratch dir until cleanup).
- The orchestrator NEVER writes judgment records. It stops at the
  score; judgments come only from the reviewer. The stored score
  verdict stays ``candidate_catch`` for bug runs.

The author is blind: the prompt is built from the bundle alone, and the
fail-closed leak check (proofdeploy.leakcheck) runs over the prompt and
every bundle file BEFORE the model is called. A single secret
occurrence aborts the run.

If provisioning fails on a side, the run is still recorded and scored:
the registered rule maps a non-READY side to infrastructure
INCONCLUSIVE. If the model produces no valid probes, the run aborts
with OrchestratorError and nothing is scored or committed: there is no
evidence to score.
"""

from __future__ import annotations

import json
import socket
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from proofdeploy.executor import Executor, HarnessAppConfig
from proofdeploy.fixture import (
    assemble_author_bundle,
    create_snapshot,
    template_hash,
)
from proofdeploy.leakcheck import (
    assert_no_secrets,
    check_bundle_answer_key,
    collect_forbidden_values,
)
from proofdeploy.model_client import (
    MODEL_ID,
    TEMPERATURE,
    ModelCallError,
    ModelResponse,
    ModelRunner,
    author_prompt_builder_sha256,
    author_prompt_sha256,
    build_author_prompt,
    call_model,
)
from proofdeploy.probe import ProbeRejected, ProbeSet
from proofdeploy.runner import (
    Provisioner,
    ProvisionResult,
    ProvisionStatus,
    find_lockfile,
)
from proofdeploy.runpair import (
    RunPairVerdict,
    build_evidence_record,
    commit_records,
    score_clean_diff,
    score_run_pair_detailed,
    write_evidence_record,
    write_score_record,
)
from proofdeploy.sandbox import IMAGES as SANDBOX_IMAGES
from proofdeploy.sandbox import (
    Sandbox,
    SandboxApp,
    SandboxConfig,
    assert_measurement_gate,
    check_podman,
)
from proofdeploy.yml import RepoContract, load_contract

# Fixture keys rendered into the author-facing description by design.
# Every other fixture value is secret-collected and leak-checked.
PUBLIC_FIXTURE_KEYS = ("repo_name", "seed_note", "auth_mechanism", "auth_scope", "flags_note")


class OrchestratorError(RuntimeError):
    """The measured unit could not run to a score."""


def _git(repo_dir: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo_dir), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise OrchestratorError(
            f"git {' '.join(args)} failed in {repo_dir}: {proc.stderr.strip()[:300]}"
        )
    return proc.stdout.strip()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@dataclass
class MeasureConfig:
    """One measured unit: a bug run or a clean-diff run."""

    repo_dir: Path
    output_dir: Path  # records live here; it is (or becomes) a git repo
    workdir: Path
    skill_path: Path | None = None
    # No-skill (baseline) arm: exactly one of skill_path / no_skill is set.
    no_skill: bool = False
    # Bug runs: rev_bug_base (B^), rev_bug (B), rev_fix (fix).
    # Clean runs: rev_clean (C); the fix_* revs stay None.
    bug_id: str | None = None
    clean_id: str | None = None
    rev_bug_base: str | None = None
    rev_bug: str | None = None
    rev_fix: str | None = None
    rev_clean: str | None = None
    allow_harness_app: bool = False
    model_runner: ModelRunner | None = None
    extra_secrets: list[str] = field(default_factory=list)
    # Contract env keys whose values are benign config, not secrets
    # (mirrors the record's public_contract_env_keys).
    public_contract_env_keys: list[str] = field(default_factory=list)
    # Sandbox unattended runs in a de-privileged container (WO-5 brief).
    # Sandboxed by default: untrusted code must never run on the host.
    # An unsandboxed run is allowed only through the explicit dev flag
    # ``allow_unsandboxed``; it is marked in the evidence record and
    # never counts as a measured result.
    sandbox: bool = True
    allow_unsandboxed: bool = False
    # Provenance for CI-driven measured runs (GitHub Actions run URL/ID,
    # runner image). Recorded verbatim in the evidence record.
    provenance_run_url: str | None = None
    provenance_run_id: str | None = None
    provenance_runner_image: str | None = None
    note: str = ""

    @property
    def run_kind(self) -> str:
        return "bug" if self.bug_id else "clean"


@dataclass
class _Side:
    """One provisioned side of a run pair."""

    rev: str
    sha: str
    snapshot_dir: Path
    contract: RepoContract
    provision: ProvisionResult
    executor: Executor | None = None
    # Isolation actually in effect for this side's sandbox app (network
    # mode, userns mode, uid). None when the sandbox was not used.
    sandbox_evidence: dict[str, object] | None = None


class Orchestrator:
    """Runs measured units end to end."""

    def __init__(self, cfg: MeasureConfig) -> None:
        self.cfg = cfg
        self.log: list[str] = []

    def _say(self, msg: str) -> None:
        self.log.append(msg)

    def _provenance(self, capability_probe: dict[str, object]) -> dict[str, object]:
        """Provenance for the evidence record: CI run identity (when
        provided via the CLI) plus the capability probe output from the
        measurement gate. Empty capability_probe means the gate did not
        run (unsandboxed dev run)."""
        return {
            "actions_run_url": self.cfg.provenance_run_url,
            "actions_run_id": self.cfg.provenance_run_id,
            "runner_image": self.cfg.provenance_runner_image,
            "capability_probe": capability_probe,
        }

    # ------------------------------------------------------------------
    # Bundle and authoring (steps a-c).
    # ------------------------------------------------------------------

    def _build_bundle(
        self,
        unit_dir: Path,
        *,
        unit_id: str,
        rev_base: str,
        rev_tip: str,
    ) -> tuple[Any, dict[str, Any], str, str, str, str, dict[str, Any]]:
        """Assemble the author bundle.

        Returns (bundle, manifest, prompt, raw_diff, base_sha, tip_sha,
        truncation_flags). The prompt is a pure function of the verified
        bundle plus the frozen template: no run metadata ("run kind",
        unit id) is included, so the orchestrator's knowledge cannot leak
        to the model through the prompt.
        Fail-closed: empty diff, bad template hash, or a secret in the
        prompt/bundle aborts before any model call. The answer-key check
        is a separate step (``_check_answer_key``) so tests can seed a
        tampered bundle and show the run refused.
        """
        cfg = self.cfg
        repo = cfg.repo_dir
        base_sha = _git(repo, "rev-parse", "--verify", f"{rev_base}^{{commit}}")
        tip_sha = _git(repo, "rev-parse", "--verify", f"{rev_tip}^{{commit}}")
        diff_text = _git(repo, "diff", f"{base_sha}..{tip_sha}", "--")
        if not diff_text.strip():
            raise OrchestratorError(
                f"empty diff {rev_base}..{rev_tip}: nothing to probe"
            )
        diff_path = unit_dir / "diff.patch"
        diff_path.write_text(diff_text, encoding="utf-8")

        snap_dir = unit_dir / "snapshot-tip"
        snapshot = create_snapshot(
            repo_dir=repo, commit_rev=tip_sha, output_dir=snap_dir
        )
        contract = load_contract(snapshot.dir / "proofdeploy.yml")

        bundle_dir = unit_dir / "author-bundle"
        bundle = assemble_author_bundle(
            diff_path=diff_path,
            snapshot_dir=snapshot.dir,
            yml_fixture=contract.fixture,
            output_dir=bundle_dir,
            expected_template_hash=template_hash(),
            source_sha=snapshot.sha,
            diff_range=f"{base_sha}..{tip_sha}",
            skill_path=cfg.skill_path,
            no_skill=cfg.no_skill,
        )
        manifest = json.loads(bundle.manifest.read_text(encoding="utf-8"))
        # Pass None for no-skill; the builder renders "(none)" itself.
        skill_text = (
            bundle.skill.read_text(encoding="utf-8") if bundle.skill else None
        )
        description = bundle.description.read_text(encoding="utf-8")
        prompt, truncation_flags = build_author_prompt(
            skill_text=skill_text,
            fixture_description=description,
            diff_text=diff_text,
            snapshot_dir=snapshot.dir,
        )
        self._say(f"bundle assembled at {bundle.root}")
        self._leak_check_bundle(bundle, prompt, contract)
        return bundle, manifest, prompt, diff_text, base_sha, tip_sha, truncation_flags

    def _check_answer_key(
        self,
        bundle: Any,
        prompt: str,
        base_sha: str,
        tip_sha: str,
        fix_rev: str | None,
    ) -> str:
        """Answer-key check (WO-5 brief): allowlist by equality.

        Fail the run if the bundle is not byte-equal to the allowlist
        recomputed from B (diff.patch, snapshot, skill, fixture
        description), if the prompt was not built from the verified
        files, or if the prompt backstop finds fix-SHA prefixes or the
        fix subject. Runs before any model call. Returns the bundle
        manifest hash for the record."""
        from proofdeploy.fixture import EXPECTED_SKILL_HASH

        repo = self.cfg.repo_dir
        fix_sha = (
            _git(repo, "rev-parse", "--verify", f"{fix_rev}^{{commit}}")
            if fix_rev
            else None
        )
        fix_subject = (
            _git(repo, "log", "-1", "--format=%s", fix_sha) if fix_sha else None
        )
        manifest_sha = check_bundle_answer_key(
            bundle.root,
            bundle.manifest,
            prompt,
            repo_dir=repo,
            bug_sha=tip_sha,
            bug_parent_sha=base_sha,
            fix_sha=fix_sha,
            fix_subject=fix_subject,
            expected_skill_hash=EXPECTED_SKILL_HASH,
            no_skill=self.cfg.no_skill,
        )
        self._say(f"answer-key check passed; bundle manifest {manifest_sha[:12]}")
        return manifest_sha

    def _record_inconclusive(
        self,
        cfg: MeasureConfig,
        unit_dir: Path,
        repo: Path,
        bundle: Any,
        manifest: dict[str, Any],
        prompt: str,
        base_sha: str,
        tip_sha: str,
        manifest_sha: str,
        cause: str,
        attempts: list[str],
        run_kind: str,
        use_sandbox: bool,
        capability_probe: dict[str, Any],
    ) -> dict[str, Any]:
        """Record an INCONCLUSIVE run (model call failed).

        Writes an evidence record (with no probes, but with both model
        call attempts recorded) and a score record with verdict
        INCONCLUSIVE, reason class "environment", and the cause
        ("model_overflow" or "model_transport").

        Per Master's rule (Addendum 4): a model failure in a measured run
        IS a measured result (measured_result=True). It counts against the
        tool. The counting consequence is stored as a separate field:
        - bug run: "counts_as_not_caught"
        - clean run: "counts_as_false_alarm"
        """
        from proofdeploy.model_client import (
            author_prompt_builder_sha256,
            author_prompt_sha256,
        )
        from proofdeploy.runpair import (
            build_evidence_record,
            commit_records,
            write_evidence_record,
            write_score_record,
        )

        # Minimal evidence: no probes were produced, but both model call
        # attempts are recorded. Per Master's rule, a model failure in a
        # sandboxed measured run IS a measured result (measured_result =
        # use_sandbox, matching the normal path). A dev run never counts.
        record = build_evidence_record(
            run_kind=run_kind,
            bug_id=cfg.bug_id if run_kind == "bug" else None,
            clean_id=cfg.clean_id if run_kind == "clean" else None,
            prompt=prompt,
            bundle_manifest=manifest,
            bundle_manifest_sha256=manifest_sha,
            author_prompt_sha256=author_prompt_sha256(),
            author_prompt_builder_sha256=author_prompt_builder_sha256(),
            skill_sha256=manifest.get("skill_sha256"),
            sandboxed=use_sandbox,
            measured_result=use_sandbox,
            provenance=self._provenance(capability_probe),
            model_call_attempts=attempts,
        )
        record_id, _, evidence_sha = write_evidence_record(
            record, repo, repo, secret_values=self.cfg.extra_secrets,
        )
        self._say(f"evidence committed: {evidence_sha[:12]}")
        # Score with INCONCLUSIVE verdict, reason class "environment",
        # and the cause. The counting consequence is a separate field.
        from proofdeploy.runpair import RunPairVerdict
        counting = (
            "counts_as_not_caught" if run_kind == "bug"
            else "counts_as_false_alarm"
        )
        score_path, _ = write_score_record(
            output_dir=repo,
            repo=repo,
            evidence_record_id=record_id,
            run_kind=run_kind,
            bug_id=cfg.bug_id if run_kind == "bug" else None,
            clean_id=cfg.clean_id if run_kind == "clean" else None,
            verdict=RunPairVerdict.INCONCLUSIVE,
            note=f"environment: {cause}",
            secret_values=self.cfg.extra_secrets,
        )
        score_sha = commit_records(
            repo, repo, f"score: {run_kind} {(cfg.bug_id or cfg.clean_id)} ({record_id[:8]})"
        )
        self._say(f"score committed: {score_sha[:12]}")
        return {
            "run_kind": run_kind,
            "bug_id": cfg.bug_id if run_kind == "bug" else None,
            "clean_id": cfg.clean_id if run_kind == "clean" else None,
            "evidence_record_id": record_id,
            "evidence_commit": evidence_sha,
            "score_commit": score_sha,
            "verdict": "inconclusive",
            "reason_class": "environment",
            "cause": cause,
            "counting": counting,
            "model_attempts": attempts,
        }

    def _record_orchestrator_crash(
        self,
        cfg: MeasureConfig,
        unit_dir: Path,
        repo: Path,
        bundle: Any,
        manifest: dict[str, Any],
        prompt: str,
        base_sha: str,
        tip_sha: str,
        manifest_sha: str,
        raw: ModelResponse,
        probeset: ProbeSet,
        run_kind: str,
        use_sandbox: bool,
        capability_probe: dict[str, Any],
        error: BaseException,
        runner_log: list[str] | None = None,
        provisioning: dict[str, Any] | None = None,
        probe_results: Any | None = None,
    ) -> dict[str, Any]:
        """Record an INCONCLUSIVE run after an orchestrator crash (O3).

        If the orchestrator fails after the model call (e.g., provisioning
        crash, probe execution crash), the prompt, raw response, parsed
        probes, runner log, and any provisioning/probe results gathered so
        far must not be lost. This commits an evidence record with verdict
        INCONCLUSIVE and a typed reason (cause, exception type and message),
        then the score as usual.

        Master's principle: state what failed, never lose it.
        """
        from proofdeploy.model_client import (
            author_prompt_builder_sha256,
            author_prompt_sha256,
        )
        from proofdeploy.runpair import (
            RunPairVerdict,
            build_evidence_record,
            commit_records,
            write_evidence_record,
            write_score_record,
        )

        cause = "orchestrator_crash"
        error_text = f"{type(error).__name__}: {error}"
        self._say(
            "orchestrator crashed after authoring; recording INCONCLUSIVE: "
            f"{error_text[:200]}"
        )
        record = build_evidence_record(
            run_kind=run_kind,
            bug_id=cfg.bug_id if run_kind == "bug" else None,
            clean_id=cfg.clean_id if run_kind == "clean" else None,
            prompt=prompt,
            raw_model_response=raw.content,
            parsed_probes=probeset.probes,
            bundle_manifest=manifest,
            bundle_manifest_sha256=manifest_sha,
            author_prompt_sha256=author_prompt_sha256(),
            author_prompt_builder_sha256=author_prompt_builder_sha256(),
            skill_sha256=manifest.get("skill_sha256"),
            sandboxed=use_sandbox,
            measured_result=use_sandbox,
            provenance=self._provenance(capability_probe),
            model_request=raw.request_record,
            # O3: keep the reason, the runner log, and everything gathered
            # before the crash.
            inconclusive_cause=cause,
            inconclusive_error=error_text,
            runner_log=list(runner_log) if runner_log else list(self.log),
            crash_provisioning=provisioning or {},
            crash_probe_results=probe_results,
        )
        record_id, _, evidence_sha = write_evidence_record(
            record, repo, repo, secret_values=self.cfg.extra_secrets,
        )
        self._say(f"evidence committed: {evidence_sha[:12]}")
        counting = (
            "counts_as_not_caught" if run_kind == "bug"
            else "counts_as_false_alarm"
        )
        score_path, _ = write_score_record(
            output_dir=repo,
            repo=repo,
            evidence_record_id=record_id,
            run_kind=run_kind,
            bug_id=cfg.bug_id if run_kind == "bug" else None,
            clean_id=cfg.clean_id if run_kind == "clean" else None,
            verdict=RunPairVerdict.INCONCLUSIVE,
            note="environment: orchestrator_crash",
            secret_values=self.cfg.extra_secrets,
        )
        score_sha = commit_records(
            repo, repo, f"score: {run_kind} {(cfg.bug_id or cfg.clean_id)} ({record_id[:8]})"
        )
        self._say(f"score committed: {score_sha[:12]}")
        return {
            "run_kind": run_kind,
            "bug_id": cfg.bug_id if run_kind == "bug" else None,
            "clean_id": cfg.clean_id if run_kind == "clean" else None,
            "evidence_record_id": record_id,
            "evidence_commit": evidence_sha,
            "score_commit": score_sha,
            "verdict": "inconclusive",
            "reason_class": "environment",
            "cause": "orchestrator_crash",
            "counting": counting,
        }

    def _leak_check_bundle(
        self, bundle: Any, prompt: str, contract: RepoContract
    ) -> None:
        """Fail-closed leak check over the prompt and every bundle file."""
        cfg = self.cfg
        forbidden = collect_forbidden_values(
            contract.fixture,
            contract.env,
            public_fixture_keys=PUBLIC_FIXTURE_KEYS,
            public_contract_env_keys=cfg.public_contract_env_keys,
            extra_secrets=cfg.extra_secrets,
        )
        assert_no_secrets(prompt, forbidden, where="author prompt")
        files = [bundle.diff, bundle.description]
        if bundle.skill is not None:
            files.append(bundle.skill)
        for rel in sorted(
            p.relative_to(bundle.snapshot).as_posix()
            for p in bundle.snapshot.rglob("*")
            if p.is_file() and not p.is_symlink()
        ):
            files.append(bundle.snapshot / rel)
        for f in files:
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            assert_no_secrets(text, forbidden, where=f"bundle file {f.name}")
        self._say("leak check passed: prompt and bundle carry no secret values")

    def _author_probes(self, prompt: str) -> tuple[ProbeSet, ModelResponse]:
        """Call the model once and parse its probes (steps b-c)."""
        raw = call_model(prompt, runner=self.cfg.model_runner)
        if not raw.content.strip():
            raise OrchestratorError("model returned empty content")
        try:
            probeset = ProbeSet.from_author_output(
                raw.content, allow_harness_app=self.cfg.allow_harness_app
            )
        except ProbeRejected as e:
            raise OrchestratorError(
                f"author output failed probe validation: {e}"
            ) from e
        if not probeset.probes:
            raise OrchestratorError("author produced zero probes")
        self._say(f"author produced {len(probeset.probes)} validated probes")
        return probeset, raw

    # ------------------------------------------------------------------
    # Provisioning and execution (steps d-e).
    # ------------------------------------------------------------------

    def _provision_side(
        self, runner: Provisioner, rev: str, port: int, unit_dir: Path, tag: str
    ) -> _Side:
        """Provision one side; never raises on build/start failure."""
        repo = self.cfg.repo_dir
        sha = _git(repo, "rev-parse", "--verify", f"{rev}^{{commit}}")
        snap_dir = unit_dir / f"snapshot-{tag}"
        snapshot = create_snapshot(repo_dir=repo, commit_rev=sha, output_dir=snap_dir)
        contract = load_contract(snapshot.dir / "proofdeploy.yml")
        found = find_lockfile(snapshot.dir, _detect_runtime(snapshot.dir))
        expected = found[1] if found else None
        provision = runner.provision(
            snapshot_dir=snapshot.dir,
            contract=contract,
            port=port,
            expected_lockfile_sha256=expected,
        )
        self._say(
            f"provision {tag} ({sha[:12]}): {provision.status.value} "
            f"-> {provision.target_url}"
        )
        return _Side(
            rev=rev,
            sha=sha,
            snapshot_dir=snapshot.dir,
            contract=contract,
            provision=provision,
        )

    def _provision_side_sandbox(
        self, rev: str, port: int, unit_dir: Path, tag: str
    ) -> _Side:
        """Provision one side in the sandbox; never raises on build/start failure.

        The app is installed with network, then run in a de-privileged
        container with no external network, only the checkout mounted,
        and no secrets in the container environment (WO-5 brief).
        """
        import os

        cfg = self.cfg
        repo = cfg.repo_dir
        sha = _git(repo, "rev-parse", "--verify", f"{rev}^{{commit}}")
        snap_dir = unit_dir / f"snapshot-{tag}"
        snapshot = create_snapshot(repo_dir=repo, commit_rev=sha, output_dir=snap_dir)
        contract = load_contract(snapshot.dir / "proofdeploy.yml")
        logs: list[str] = [f"sandbox provision {tag} ({sha[:12]})"]

        ok, msg = check_podman()
        if not ok:
            logs.append(f"sandbox unavailable: {msg}")
            provision = ProvisionResult(
                status=ProvisionStatus.BUILD_FAILED,
                reason=f"sandbox unavailable: {msg}",
                logs=logs,
            )
            return _Side(
                rev=rev, sha=sha, snapshot_dir=snapshot.dir,
                contract=contract, provision=provision,
            )

        runtime_name = _detect_runtime(snapshot.dir)
        image = SANDBOX_IMAGES.get(runtime_name, SANDBOX_IMAGES["python"])
        logs.append(f"sandbox image: {image} (runtime {runtime_name})")
        # Item 5: fail closed when the image's runtime doesn't satisfy the
        # repo's declared version (requires-python / engines). Record the
        # image digest and the runtime version in the evidence.
        from proofdeploy.runner import _requires_python_from_toml, satisfies_range

        _sandbox_requires_python = _requires_python_from_toml
        declared_range = None
        if runtime_name == "python":
            pyproject = snapshot.dir / "pyproject.toml"
            if pyproject.is_file():
                declared_range = _sandbox_requires_python(pyproject)
        elif runtime_name == "node":
            pkg = snapshot.dir / "package.json"
            if pkg.is_file():
                try:
                    import json
                    data = json.loads(pkg.read_text(encoding="utf-8"))
                    engines = data.get("engines") or {}
                    if isinstance(engines, dict):
                        declared_range = engines.get("node")
                except Exception:
                    pass
        # Get the image's runtime version and digest.
        image_version = None
        image_digest = None
        try:
            import subprocess as _sp
            ver_cmd = (
                ["python3", "--version"] if runtime_name == "python"
                else ["node", "--version"]
            )
            r = _sp.run(
                ["podman", "run", "--rm", image] + ver_cmd,
                capture_output=True, text=True, timeout=60,
            )
            out = (r.stdout or r.stderr or "").strip()
            # "Python 3.12.3" -> "3.12.3"; "v22.1.0" -> "22.1.0"
            image_version = out.split()[-1].lstrip("v") if out else None
            r2 = _sp.run(
                ["podman", "images", "--digests", "--format", "{{.Digest}}", image],
                capture_output=True, text=True, timeout=30,
            )
            image_digest = (r2.stdout or "").strip().split("\n")[0] or None
        except Exception as e:
            logs.append(f"sandbox runtime version check failed: {e}")
        logs.append(f"sandbox runtime version: {image_version} (digest {image_digest})")
        if declared_range and image_version:
            ecosystem = "python" if runtime_name == "python" else "node"
            if not satisfies_range(image_version, declared_range, ecosystem=ecosystem):
                logs.append(
                    f"sandbox runtime {image_version} does not satisfy "
                    f"declared {declared_range}: BUILD_FAILED"
                )
                provision = ProvisionResult(
                    status=ProvisionStatus.BUILD_FAILED,
                    reason=(
                        f"sandbox image runtime {image_version} does not satisfy "
                        f"declared version {declared_range}"
                    ),
                    logs=logs,
                    runtime=runtime_name,
                    runtime_version=image_version,
                )
                # Record the digest/version even on failure.
                provision.image_digest = image_digest
                return _Side(
                    rev=rev, sha=sha, snapshot_dir=snapshot.dir,
                    contract=contract, provision=provision,
                )
        sb = Sandbox(SandboxConfig(image=image))
        # Secret-free contract env only; never the full env.
        secret_free_env = {
            k: v for k, v in contract.env.items()
            if k in cfg.public_contract_env_keys
        }

        proxy_env = {k: v for k, v in os.environ.items() if "proxy" in k.lower()}
        install_cmds = []
        if contract.build:
            install_cmds.append(contract.build)
        # Python: install the lockfile into the checkout.
        lockfile = snapshot.dir / "requirements.txt"
        if lockfile.is_file():
            install_cmds.insert(0, "pip install --quiet -r requirements.txt")
        if contract.migrate:
            install_cmds.append(contract.migrate)
        if not sb.install(
            snapshot.dir, install_cmds, secret_free_env, proxy_env, timeout=600
        ):
            logs.extend(sb.log)
            provision = ProvisionResult(
                status=ProvisionStatus.BUILD_FAILED,
                reason="sandbox install failed",
                logs=logs,
            )
            return _Side(
                rev=rev, sha=sha, snapshot_dir=snapshot.dir,
                contract=contract, provision=provision,
            )
        logs.extend(sb.log)

        try:
            app = sb.start(
                snapshot.dir,
                contract.start,
                secret_free_env,
                port,
                name=f"pd-{tag}-{sha[:8]}",
            )
        except Exception as e:
            logs.append(f"sandbox start failed: {e}")
            provision = ProvisionResult(
                status=ProvisionStatus.START_FAILED,
                reason=str(e)[:200],
                logs=logs,
            )
            return _Side(
                rev=rev, sha=sha, snapshot_dir=snapshot.dir,
                contract=contract, provision=provision,
            )
        if not app.wait_ready(contract.readiness or "/health", timeout=120):
            logs.append("sandbox app not ready")
            app.stop()
            provision = ProvisionResult(
                status=ProvisionStatus.START_FAILED,
                reason="readiness timeout",
                logs=logs,
            )
            return _Side(
                rev=rev, sha=sha, snapshot_dir=snapshot.dir,
                contract=contract, provision=provision,
            )
        logs.append(f"sandbox app READY at {app.target_url}")
        provision = ProvisionResult(
            status=ProvisionStatus.READY,
            target_url=app.target_url,
            logs=logs,
            runtime=runtime_name,
            runtime_version=image_version,
            run_id=f"sandbox-{tag}-{sha[:8]}",
        )
        provision.image_digest = image_digest
        # Stash the SandboxApp for cleanup; the finally block stops it.
        provision.proc = app
        self._say(f"provision {tag} ({sha[:12]}): READY -> {app.target_url} (sandbox)")
        return _Side(
            rev=rev, sha=sha, snapshot_dir=snapshot.dir,
            contract=contract, provision=provision,
            sandbox_evidence=dict(app.sandbox_evidence),
        )

    def _credential_config(self, side: _Side) -> dict[str, Any] | None:
        """Parse the contract's declared credential provider, if any.

        The contract declares auth via the top-level `auth:` mapping,
        validated at load by `load_contract`. Absent means no credential:
        no token is used, `${AUTH_TOKEN}` is unknown, and a probe using
        it is INCONCLUSIVE `probe` (fail-closed, never a crash).

        Registration is one declared provider mode (`register`), never a
        default. Its requests are recorded and logged as writes by the
        executor; a provider failure is INCONCLUSIVE `auth`, never an
        exception.
        """
        auth = side.contract.auth
        if not auth:
            return None
        return dict(auth)

    def _make_executor(self, side: _Side) -> Executor:
        assert side.provision and side.provision.target_url
        # DB path comes from the contract's `database:` declaration, not a
        # hard-coded filename. None declared means the executor gets no DB
        # path and DB assertions are INCONCLUSIVE `probe`.
        db_path = None
        declared = side.contract.database
        if declared and declared.get("kind") == "sqlite":
            candidate = side.snapshot_dir / declared["path"]
            # Resolve relative to the snapshot dir (contract paths are
            # relative to the repo root, which the snapshot mirrors).
            if candidate.is_file():
                db_path = str(candidate)
        return Executor(
            target_url=side.provision.target_url,
            db_path=db_path,
            provisioned=True,
            contract_env=dict(side.contract.env),
            fixture=dict(side.contract.fixture),
            auth_token=None,
            credential_config=self._credential_config(side),
            allow_harness_app=self.cfg.allow_harness_app,
            harness=(
                HarnessAppConfig(workdir=self.cfg.workdir / "harness")
                if self.cfg.allow_harness_app
                else None
            ),
        )

    # ------------------------------------------------------------------
    # Records (steps f-g).
    # ------------------------------------------------------------------

    def _init_records_repo(self) -> Path:
        out = self.cfg.output_dir
        out.mkdir(parents=True, exist_ok=True)
        if not (out / ".git").exists():
            _git(out, "init", "-q")
            _git(out, "config", "user.name", "proofdeploy-orchestrator")
            _git(out, "config", "user.email", "orchestrator@proofdeploy.local")
        return out

    # ------------------------------------------------------------------
    # Public entry points.
    # ------------------------------------------------------------------

    def measure_bug(self) -> dict[str, Any]:
        """Run a full bug measured unit: bundle, author, provision, run, record, score."""
        cfg = self.cfg
        if not (cfg.bug_id and cfg.rev_bug_base and cfg.rev_bug and cfg.rev_fix):
            raise OrchestratorError("measure_bug needs bug_id, rev_bug_base, rev_bug, rev_fix")
        # Refuse the silent-host-run footgun: sandbox=False without the
        # explicit allow_unsandboxed flag is a programmer error, not a
        # dev opt-out. The only way to run unsandboxed is the explicit
        # flag.
        if not cfg.sandbox and not cfg.allow_unsandboxed:
            raise OrchestratorError(
                "sandbox=False without allow_unsandboxed=True refuses to run: "
                "untrusted code must not run on the host silently. Use "
                "allow_unsandboxed=True for an explicit dev opt-out."
            )
        use_sandbox = cfg.sandbox and not cfg.allow_unsandboxed
        capability_probe: dict[str, object] = {}
        if use_sandbox:
            # Fail-closed isolation gate: refuse the measured run unless
            # the host meets the sandbox requirements (userns remapping,
            # bridge network for the install phase).
            capability_probe = assert_measurement_gate()
            self._say("sandbox measurement gate passed")
        elif cfg.allow_unsandboxed:
            self._say(
                "WARNING: unsandboxed dev run (allow_unsandboxed=True): "
                "untrusted code runs on the host; this run is marked and "
                "never counts as a measured result"
            )
        unit_dir = cfg.workdir / f"bug-{cfg.bug_id}"
        unit_dir.mkdir(parents=True, exist_ok=True)
        repo = self._init_records_repo()

        bundle, manifest, prompt, _, base_sha, tip_sha, truncation_flags = (
            self._build_bundle(
                unit_dir,
                unit_id=cfg.bug_id,
                rev_base=cfg.rev_bug_base,
                rev_tip=cfg.rev_bug,
            )
        )
        manifest_sha = self._check_answer_key(
            bundle, prompt, base_sha, tip_sha, fix_rev=cfg.rev_fix
        )
        # Model call with retry per the registered rule:
        # - model_transport: exactly one retry, both attempts in evidence.
        # - model_overflow: no retry.
        attempts: list[str] = []
        try:
            probeset, raw = self._author_probes(prompt)
            attempts.append("attempt_1: success")
        except ModelCallError as e:
            attempts.append(f"attempt_1: {e.cause}: {e}")
            if e.cause == "model_transport":
                # Exactly one retry for transport failures.
                self._say("model transport failed; retrying once")
                try:
                    probeset, raw = self._author_probes(prompt)
                    attempts.append("attempt_2: success")
                except ModelCallError as e2:
                    attempts.append(f"attempt_2: {e2.cause}: {e2}")
                    self._say(f"model call failed twice; recording INCONCLUSIVE: {e2}")
                    return self._record_inconclusive(
                        cfg, unit_dir, repo, bundle, manifest, prompt,
                        base_sha, tip_sha, manifest_sha,
                        cause=e2.cause,
                        attempts=attempts,
                        run_kind="bug",
                        use_sandbox=use_sandbox,
                        capability_probe=capability_probe,
                    )
            else:
                # model_overflow: no retry.
                self._say(f"model overflow; recording INCONCLUSIVE (no retry): {e}")
                return self._record_inconclusive(
                    cfg, unit_dir, repo, bundle, manifest, prompt,
                    base_sha, tip_sha, manifest_sha,
                    cause=e.cause,
                    attempts=attempts,
                    run_kind="bug",
                    use_sandbox=use_sandbox,
                    capability_probe=capability_probe,
                )

        prov_workdir = cfg.workdir / "provisioner"
        prov_workdir.mkdir(parents=True, exist_ok=True)
        runner = Provisioner(workdir=prov_workdir)
        # CRITICAL: The run pair compares fix^ vs fix (the registered rule).
        # Do NOT use cfg.rev_bug (B, the bug-introducing commit) for the
        # "fix-parent" side. For real bugs, B can be years before fix^.
        fix_parent_rev = f"{cfg.rev_fix}^"
        # Resolve all four SHAs separately for the evidence record.
        repo_dir = self.cfg.repo_dir
        b_sha = _git(repo_dir, "rev-parse", "--verify", f"{cfg.rev_bug}^{{commit}}")
        b_parent_sha = _git(repo_dir, "rev-parse", "--verify", f"{cfg.rev_bug_base}^{{commit}}")
        fix_sha = _git(repo_dir, "rev-parse", "--verify", f"{cfg.rev_fix}^{{commit}}")
        fix_parent_sha = _git(repo_dir, "rev-parse", "--verify", f"{fix_parent_rev}^{{commit}}")
        sides: list[_Side] = []
        parent_results = None
        fix_results = None
        try:
            if use_sandbox:
                sides.append(
                    self._provision_side_sandbox(
                        fix_parent_rev, _free_port(), unit_dir, "fix-parent"
                    )
                )
                sides.append(
                    self._provision_side_sandbox(cfg.rev_fix, _free_port(), unit_dir, "fix")
                )
            else:
                sides.append(
                    self._provision_side(
                        runner, fix_parent_rev, _free_port(), unit_dir, "fix-parent"
                    )
                )
                sides.append(
                    self._provision_side(runner, cfg.rev_fix, _free_port(), unit_dir, "fix")
                )
            parent, fix = sides
            if parent.provision.ready and fix.provision.ready:
                # Auth comes from the contract's declared credential
                # provider (O1). Absent means no token; the executor
                # handles ${AUTH_TOKEN} as unknown (INCONCLUSIVE probe).
                parent.executor = self._make_executor(parent)
                fix.executor = self._make_executor(fix)
                try:
                    parent_results = [
                        parent.executor.run_probe(p, i)
                        for i, p in enumerate(probeset.probes)
                    ]
                    fix_results = [
                        fix.executor.run_probe(p, i)
                        for i, p in enumerate(probeset.probes)
                    ]
                finally:
                    parent.executor.close()
                    fix.executor.close()
            else:
                self._say("a side is not READY; scoring infrastructure-INCONCLUSIVE")

            record = build_evidence_record(
                run_kind="bug",
                bug_id=cfg.bug_id,
                fix_sha=fix_sha,
                fix_parent_sha=fix_parent_sha,
                b_sha=b_sha,
                b_parent_sha=b_parent_sha,
                prompt=prompt,
                raw_model_response=raw.content,
                parsed_probes=probeset.probes,
                bundle_manifest=manifest,
                bundle_manifest_sha256=manifest_sha,
                model_id=MODEL_ID,
                model_temperature=TEMPERATURE,
                model_attempts=1,
                model_request=raw.request_record,
                author_prompt_sha256=author_prompt_sha256(),
                author_prompt_builder_sha256=author_prompt_builder_sha256(),
                skill_sha256=manifest.get("skill_sha256"),
                truncation_flags=truncation_flags,
                sandbox_evidence={
                    "fix_parent": parent.sandbox_evidence or {},
                    "fix": fix.sandbox_evidence or {},
                },
                sandboxed=use_sandbox,
                measured_result=use_sandbox,
                provenance=self._provenance(capability_probe),
                fix_parent_provision=parent.provision.to_dict(),
                fix_provision=fix.provision.to_dict(),
                fix_parent_results=parent_results,
                fix_results=fix_results,
                runner_log=list(self.log),
                fixture=dict(parent.contract.fixture),
                contract_env=dict(parent.contract.env),
                public_fixture_keys=list(PUBLIC_FIXTURE_KEYS),
                public_contract_env_keys=list(self.cfg.public_contract_env_keys),
            )
            record_id, _, evidence_sha = write_evidence_record(
                record,
                repo,
                repo,
                secret_values=self.cfg.extra_secrets,
            )
            self._say(f"evidence committed: {evidence_sha[:12]}")
            score_path = write_score_record(
                repo,
                repo,
                evidence_record_id=record_id,
                run_kind="bug",
                bug_id=cfg.bug_id,
                secret_values=self.cfg.extra_secrets,
            )
            score_sha = commit_records(repo, repo, f"score: bug {cfg.bug_id} ({record_id[:8]})")
            self._say(f"score committed: {score_sha[:12]}")
            detailed = score_run_pair_detailed(parent_results, fix_results)
            # The stored score verdict mirrors write_score_record: it is
            # "candidate_catch", never "catch"; only a judgment makes it
            # a catch, and the orchestrator never writes judgments.
            stored_verdict = (
                "candidate_catch"
                if detailed.verdict == RunPairVerdict.CATCH
                else detailed.verdict.value
            )
            return {
                "run_kind": "bug",
                "bug_id": cfg.bug_id,
                "evidence_record_id": record_id,
                "evidence_commit": evidence_sha,
                "score_commit": score_sha,
                "score_path": str(score_path[1]),
                "verdict": stored_verdict,
                "candidate_catch": detailed.candidate_catch,
                "probe_count": len(probeset.probes),
                "parent_ready": parent.provision.ready,
                "fix_ready": fix.provision.ready,
            }
        except Exception as e:
            # O3: any failure after authoring commits an INCONCLUSIVE
            # record with a typed reason, then the score as usual.
            # Master's principle: state what failed, never lose it.
            # Gather everything available at crash time.
            crash_provisioning = {}
            try:
                for i, s in enumerate(sides):
                    if s.provision:
                        crash_provisioning[f"side_{i}"] = s.provision.to_dict()
            except Exception:
                pass
            return self._record_orchestrator_crash(
                cfg, unit_dir, repo, bundle, manifest, prompt,
                base_sha, tip_sha, manifest_sha, raw, probeset,
                run_kind="bug",
                use_sandbox=use_sandbox,
                capability_probe=capability_probe,
                error=e,
                runner_log=list(self.log),
                provisioning=crash_provisioning,
                probe_results={
                    "parent_results": [r.to_dict() for r in (parent_results or [])],
                    "fix_results": [r.to_dict() for r in (fix_results or [])],
                },
            )
        finally:
            for side in sides:
                if side.provision and side.provision.ready and side.provision.run_id:
                    proc = side.provision.proc
                    if isinstance(proc, SandboxApp):
                        proc.stop()
                        self._say(f"sandbox stop: {side.provision.run_id}")
                    else:
                        # O2: terminate and reap the target process on every
                        # exit path, then remove the scratch dirs.
                        runner.stop_target(proc, self.log)
                        runner.cleanup_run(side.provision.run_id, self.log)
                        self._say(f"cleanup_run: {side.provision.run_id}")

    def measure_clean(self) -> dict[str, Any]:
        """Run a clean-diff measured unit against C only."""
        cfg = self.cfg
        if not (cfg.clean_id and cfg.rev_clean):
            raise OrchestratorError("measure_clean needs clean_id and rev_clean")
        if not cfg.sandbox and not cfg.allow_unsandboxed:
            raise OrchestratorError(
                "sandbox=False without allow_unsandboxed=True refuses to run: "
                "untrusted code must not run on the host silently. Use "
                "allow_unsandboxed=True for an explicit dev opt-out."
            )
        use_sandbox = cfg.sandbox and not cfg.allow_unsandboxed
        capability_probe: dict[str, object] = {}
        if use_sandbox:
            # Fail-closed isolation gate: refuse the measured run unless
            # the host meets the sandbox requirements.
            capability_probe = assert_measurement_gate()
            self._say("sandbox measurement gate passed")
        elif cfg.allow_unsandboxed:
            self._say(
                "WARNING: unsandboxed dev run (allow_unsandboxed=True): "
                "untrusted code runs on the host; this run is marked and "
                "never counts as a measured result"
            )
        unit_dir = cfg.workdir / f"clean-{cfg.clean_id}"
        unit_dir.mkdir(parents=True, exist_ok=True)
        repo = self._init_records_repo()

        # The clean author bundle: C^->C diff + C snapshot.
        bundle, manifest, prompt, _, base_sha, tip_sha, truncation_flags = (
            self._build_bundle(
                unit_dir,
                unit_id=cfg.clean_id,
                rev_base=f"{cfg.rev_clean}^",
                rev_tip=cfg.rev_clean,
            )
        )
        manifest_sha = self._check_answer_key(
            bundle, prompt, base_sha, tip_sha, fix_rev=None
        )
        # Model call with retry per the registered rule:
        # - model_transport: exactly one retry, both attempts in evidence.
        # - model_overflow: no retry.
        attempts: list[str] = []
        try:
            probeset, raw = self._author_probes(prompt)
            attempts.append("attempt_1: success")
        except ModelCallError as e:
            attempts.append(f"attempt_1: {e.cause}: {e}")
            if e.cause == "model_transport":
                # Exactly one retry for transport failures.
                self._say("model transport failed; retrying once")
                try:
                    probeset, raw = self._author_probes(prompt)
                    attempts.append("attempt_2: success")
                except ModelCallError as e2:
                    attempts.append(f"attempt_2: {e2.cause}: {e2}")
                    self._say(f"model call failed twice; recording INCONCLUSIVE: {e2}")
                    return self._record_inconclusive(
                        cfg, unit_dir, repo, bundle, manifest, prompt,
                        base_sha, tip_sha, manifest_sha,
                        cause=e2.cause,
                        attempts=attempts,
                        run_kind="clean",
                        use_sandbox=use_sandbox,
                        capability_probe=capability_probe,
                    )
            else:
                # model_overflow: no retry.
                self._say(f"model overflow; recording INCONCLUSIVE (no retry): {e}")
                return self._record_inconclusive(
                    cfg, unit_dir, repo, bundle, manifest, prompt,
                    base_sha, tip_sha, manifest_sha,
                    cause=e.cause,
                    attempts=attempts,
                    run_kind="clean",
                    use_sandbox=use_sandbox,
                    capability_probe=capability_probe,
                )

        prov_workdir = cfg.workdir / "provisioner"
        prov_workdir.mkdir(parents=True, exist_ok=True)
        runner = Provisioner(workdir=prov_workdir)
        sides: list[_Side] = []
        c_results = None
        try:
            if use_sandbox:
                sides.append(
                    self._provision_side_sandbox(cfg.rev_clean, _free_port(), unit_dir, "clean")
                )
            else:
                sides.append(
                    self._provision_side(runner, cfg.rev_clean, _free_port(), unit_dir, "clean")
                )
            (side,) = sides
            if side.provision.ready:
                # Auth comes from the contract's declared credential
                # provider (O1). Absent means no token.
                side.executor = self._make_executor(side)
                try:
                    c_results = [
                        side.executor.run_probe(p, i)
                        for i, p in enumerate(probeset.probes)
                    ]
                finally:
                    side.executor.close()
            else:
                self._say("C is not READY; recording without results")

            record = build_evidence_record(
                run_kind="clean",
                clean_id=cfg.clean_id,
                c_sha=side.sha,
                c_provision=side.provision.to_dict(),
                c_results=c_results,
                prompt=prompt,
                raw_model_response=raw.content,
                parsed_probes=probeset.probes,
                bundle_manifest=manifest,
                bundle_manifest_sha256=manifest_sha,
                model_id=MODEL_ID,
                model_temperature=TEMPERATURE,
                model_attempts=1,
                model_request=raw.request_record,
                author_prompt_sha256=author_prompt_sha256(),
                author_prompt_builder_sha256=author_prompt_builder_sha256(),
                skill_sha256=manifest.get("skill_sha256"),
                truncation_flags=truncation_flags,
                sandbox_evidence={"c": side.sandbox_evidence or {}},
                sandboxed=use_sandbox,
                measured_result=use_sandbox,
                provenance=self._provenance(capability_probe),
                runner_log=list(self.log),
                fixture=dict(side.contract.fixture),
                contract_env=dict(side.contract.env),
                public_fixture_keys=list(PUBLIC_FIXTURE_KEYS),
                public_contract_env_keys=list(self.cfg.public_contract_env_keys),
            )
            record_id, _, evidence_sha = write_evidence_record(
                record, repo, repo, secret_values=self.cfg.extra_secrets
            )
            self._say(f"evidence committed: {evidence_sha[:12]}")
            score_path = write_score_record(
                repo,
                repo,
                evidence_record_id=record_id,
                run_kind="clean",
                clean_id=cfg.clean_id,
                secret_values=self.cfg.extra_secrets,
            )
            score_sha = commit_records(repo, repo, f"score: clean {cfg.clean_id} ({record_id[:8]})")
            self._say(f"score committed: {score_sha[:12]}")
            false_alarm = score_clean_diff(c_results)
            return {
                "run_kind": "clean",
                "clean_id": cfg.clean_id,
                "evidence_record_id": record_id,
                "evidence_commit": evidence_sha,
                "score_commit": score_sha,
                "score_path": str(score_path[1]),
                "false_alarm": false_alarm,
                "probe_count": len(probeset.probes),
                "c_ready": side.provision.ready,
            }
        except Exception as e:
            # O3: any failure after authoring commits an INCONCLUSIVE
            # record with a typed reason, then the score as usual.
            crash_provisioning = {}
            try:
                for i, s in enumerate(sides):
                    if s.provision:
                        crash_provisioning[f"side_{i}"] = s.provision.to_dict()
            except Exception:
                pass
            return self._record_orchestrator_crash(
                cfg, unit_dir, repo, bundle, manifest, prompt,
                base_sha, tip_sha, manifest_sha, raw, probeset,
                run_kind="clean",
                use_sandbox=use_sandbox,
                capability_probe=capability_probe,
                error=e,
                runner_log=list(self.log),
                provisioning=crash_provisioning,
                probe_results={
                    "c_results": [r.to_dict() for r in (c_results or [])],
                },
            )
        finally:
            for side in sides:
                if side.provision and side.provision.ready and side.provision.run_id:
                    proc = side.provision.proc
                    if isinstance(proc, SandboxApp):
                        proc.stop()
                        self._say(f"sandbox stop: {side.provision.run_id}")
                    else:
                        # O2: terminate and reap the target process on every
                        # exit path, then remove the scratch dirs.
                        runner.stop_target(proc, self.log)
                        runner.cleanup_run(side.provision.run_id, self.log)
                        self._say(f"cleanup_run: {side.provision.run_id}")


def _detect_runtime(snapshot_dir: Path) -> str:
    from proofdeploy.runner import detect_runtime

    runtime, _ = detect_runtime(snapshot_dir)
    return runtime
