"""Run pair and JSONL records for the ProofDeploy runner (WAL-58, WO-4).

A run pair executes the same probe set against two targets:
- fix^ (the parent of the fix): the bug should be present -> probes FAIL
- fix: the bug should be fixed -> probes PASS

Scoring implements the registered run-pair rule exactly
(docs/evaluation/scoring-rule-run-pair-2026-10-09.md, merged as PR #38):

- Bugs are scored per probe: a bug is caught if at least one probe
  FAILs on fix^ and PASSes on fix. The score record carries
  ``candidate_catch``: the indices of EVERY probe that went FAIL->PASS.
  The stored score verdict is ``candidate_catch``, never ``catch``.
  Condition 3 of the registered rule ("targets behavior the fix actually
  changed") is a reviewer judgment: the final CATCH is a separate,
  append-only judgment record that cites the evidence and score records
  and holds the probe index, the judge, the judge's role, and the
  condition-3 decision. Judgments are validated: the probe must be in
  the score's candidate_catch, condition3_met must be a bool, the
  judgment's evidence id and commit must match the score's, and the
  judge role is allowlisted (on measured runs only "reviewer" may
  judge, and the judge must not be empty).
- A sibling probe that is INCONCLUSIVE or FAILs on both sides does not
  cancel the catch.
- Pair INCONCLUSIVE only when a side's provisioning isn't READY (no
  results: cannot build/start), or when every probe on a side is
  INCONCLUSIVE with reason ``environment`` (target unreachable for
  every probe). Schema-invalid probes (reason ``probe``) are a miss
  (NO_CATCH), not infrastructure.
- Clean diffs are strict: probes run against C only, and any FAIL or
  INCONCLUSIVE is a false alarm.

The only scoring version is ``v2-per-probe`` (the registered rule).
Unknown versions raise.

Records:
- Every record (evidence, score, judgment) and every per-probe result
  carries ``schema_version`` (currently ``"1.0.0"``). Bump it when the
  record format changes; readers must refuse unknown versions.
- Every run writes an evidence record BEFORE any scoring. The evidence
  record holds the prompt, the raw model response, the parsed probes
  (verbatim) and their hash, the bundle manifest (skill hash or no-skill
  marker), the harness version, both provisioning results (logs, runtime
  versions, lockfile hashes), and the per-probe results with typed
  reasons and the recorded requests (act and setup steps).
- The evidence record also holds ``author_prompt_sha256``: the SHA-256
  of the frozen author-prompt template (``proofdeploy/author_prompt_v3.md``)
  that built the prompt, and ``model_request``: the exact model request
  as sent (model, temperature, per-message hashes, and the seed/tools
  fields as sent).
- Writing the evidence record makes a git commit; the commit SHA is
  recorded. Scoring reads the evidence record back AT that commit,
  rebuilds the per-probe results, and scores them: any caller-passed
  verdict, candidate_catch, false_alarm, bug_id, clean_id, or run_kind
  that disagrees with the evidence is refused. The score goes in a
  later commit that cites the evidence commit.
- Records are append-only: each record gets a unique id and is appended
  to a JSONL log. Repeats never overwrite.
- Records are secret-redacted by value, fail-closed: every fixture and
  contract-env value is secret unless its key is explicitly declared
  public in the record; captured values and minted tokens are always
  secret. Pattern matching (JWT shape, "Bearer <token>") is a backstop.
  Authorization headers keep their scheme ("Bearer ***REDACTED***").
  ``parsed_probes`` is stored VERBATIM and is never redacted: placeholders
  like ``Bearer ${USER_TOKEN}`` and capture specs are structure, not
  secrets, and the stored ``parsed_probes_sha256`` must still match.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from proofdeploy.executor import InconclusiveReason, ProbeResult, Verdict
from proofdeploy.probe import HARNESS_VERSION, SCHEMA_VERSION

# The only scoring version: the registered per-probe rule (2026-10-09,
# PR #38). There is no legacy version; unregistered rules must not be
# selectable.
SCORING_VERSION = "v2-per-probe"
DEFAULT_SCORING_VERSION = SCORING_VERSION

# Name of the append-only JSONL log inside the output directory.
RUNS_LOG_NAME = "runs.jsonl"


class RunPairVerdict(Enum):
    """Verdict for a fix^/fix run pair."""

    CATCH = "catch"  # at least one probe: FAIL on fix^, PASS on fix
    NO_CATCH = "no_catch"  # ran, but no probe caught the bug
    INCONCLUSIVE = "inconclusive"  # could not run (build/start failure)


@dataclass
class PairScore:
    """Detailed pair score: verdict plus every discriminating probe."""

    verdict: RunPairVerdict
    candidate_catch: list[int]  # probe indices with FAIL on fix^, PASS on fix


def _side_unreachable(results: list[ProbeResult]) -> bool:
    """True when the target was unreachable for every probe on this side.

    Registered text: "the target is unreachable for every probe". Only
    reason ``environment`` counts as unreachable. Schema-invalid probes
    (reason ``probe``) are a miss, not infrastructure. Results that were
    never sent (invalid author blocks, ``sent=False``) are excluded:
    they must not turn an infrastructure failure into an author miss,
    nor an author miss into infrastructure.
    """
    sent = [r for r in results if r.sent]
    return bool(sent) and all(
        r.verdict == Verdict.INCONCLUSIVE and r.reason == InconclusiveReason.ENVIRONMENT
        for r in sent
    )


def score_run_pair_detailed(
    parent_results: list[ProbeResult] | None,
    fix_results: list[ProbeResult] | None,
) -> PairScore:
    """Score a fix^/fix run pair under the registered rule (PR #38).

    - Either side None (cannot build/start) -> INCONCLUSIVE.
    - Empty results on either side -> INCONCLUSIVE (nothing to compare).
    - Every probe INCONCLUSIVE with reason ``environment`` on either
      side -> INCONCLUSIVE.
    - CATCH with ``candidate_catch`` = indices of EVERY probe that
      FAILs on fix^ and PASSes on fix (matched by probe_index). A
      sibling INCONCLUSIVE or non-discriminating probe does not cancel
      the catch.
    - Otherwise -> NO_CATCH (a miss).
    """
    if parent_results is None or fix_results is None:
        return PairScore(RunPairVerdict.INCONCLUSIVE, [])
    if not parent_results or not fix_results:
        return PairScore(RunPairVerdict.INCONCLUSIVE, [])
    if _side_unreachable(parent_results) or _side_unreachable(fix_results):
        return PairScore(RunPairVerdict.INCONCLUSIVE, [])

    fix_by_index = {r.probe_index: r for r in fix_results}
    candidates = [
        p.probe_index
        for p in parent_results
        if (f := fix_by_index.get(p.probe_index)) is not None
        and p.verdict == Verdict.FAIL
        and f.verdict == Verdict.PASS
    ]
    if candidates:
        return PairScore(RunPairVerdict.CATCH, candidates)
    return PairScore(RunPairVerdict.NO_CATCH, [])


def score_run_pair(
    parent_results: list[ProbeResult] | None,
    fix_results: list[ProbeResult] | None,
    scoring_version: str = DEFAULT_SCORING_VERSION,
) -> RunPairVerdict:
    """Score a fix^/fix run pair. Only the registered version is allowed."""
    if scoring_version != SCORING_VERSION:
        raise ValueError(f"unknown scoring version: {scoring_version!r}")
    return score_run_pair_detailed(parent_results, fix_results).verdict


def score_clean_diff(results: list[ProbeResult] | None) -> bool:
    """Strict clean-diff scoring. Returns True for a false alarm.

    Probes run against C only. Any FAIL or INCONCLUSIVE is a false
    alarm. A clean diff that could not run at all is INCONCLUSIVE,
    which is a false alarm under the strict rule.
    """
    if results is None or not results:
        return True
    return any(r.verdict in (Verdict.FAIL, Verdict.INCONCLUSIVE) for r in results)


# ---------------------------------------------------------------------------
# Secret redaction by value
#
# Records must never leak fixture secrets: ADMIN_KEY, bearer tokens,
# minted JWTs, captured values -- including in the prompt, runner logs,
# and recorded request headers. Redaction replaces every known secret
# VALUE wherever it appears in the record. Pattern matching (JWT shape,
# "Bearer <token>") is a backstop for values we never collected.
#
# ``parsed_probes`` is stored VERBATIM and is never redacted: placeholders
# like ``Bearer ${USER_TOKEN}`` and capture specs are structure, not
# secrets, and the stored ``parsed_probes_sha256`` must still match.
# ---------------------------------------------------------------------------

REDACTED = "***REDACTED***"
REDACTED_JWT = "***REDACTED_JWT***"

# Secret values shorter than this are skipped: replacing a 1-3 character
# substring everywhere would shred innocent text.
_MIN_SECRET_LEN = 4

# Dict keys whose values are always treated as secrets (case-insensitive
# substring match on the key). Backstop for values missed by value-based
# collection.
_SECRET_KEY_SUBSTRINGS = (
    "admin_key",
    "password",
    "passwd",
    "secret",
    "api_key",
    "apikey",
    "private_key",
    "bearer",
    "auth_token",
    "access_token",
    "refresh_token",
    "id_token",
    "client_secret",
)

# A minted JWT: three dot-separated base64url segments starting with eyJ.
_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")

# "Bearer <token>" in free text (details, logs, bodies).
_BEARER_RE = re.compile(r"(Bearer\s+)[A-Za-z0-9._~+/=-]+", re.IGNORECASE)


def _is_secret_key(key: str) -> bool:
    kl = key.lower()
    if kl in ("authorization", "proxy-authorization"):
        return True
    # Normalize separators so "X-Api-Key", "api_key" and "apikey" all match.
    flat = kl.replace("-", "").replace("_", "")
    if flat == "token" or flat.endswith("token"):
        return True
    return any(p in kl or p.replace("_", "") in flat for p in _SECRET_KEY_SUBSTRINGS)


def collect_secret_values(
    *,
    fixture: dict[str, Any] | None = None,
    contract_env: dict[str, Any] | None = None,
    captured: dict[str, Any] | None = None,
    minted_tokens: Iterable[str] | None = None,
) -> list[str]:
    """Collect every secret value that must be redacted from a record.

    - Fixture and contract-env values whose KEY looks secret.
    - Every captured binding value (tokens, session ids from setup).
    - Every minted token (credential provider output).
    Values shorter than _MIN_SECRET_LEN are skipped.
    """
    values: list[str] = []
    for mapping in (fixture or {}, contract_env or {}):
        for k, v in mapping.items():
            if isinstance(k, str) and _is_secret_key(k) and isinstance(v, str):
                values.append(v)
    for v in (captured or {}).values():
        if isinstance(v, str):
            values.append(v)
    for v in minted_tokens or []:
        if isinstance(v, str):
            values.append(v)
    seen: set[str] = set()
    out: list[str] = []
    for v in sorted(values, key=len, reverse=True):
        if len(v) >= _MIN_SECRET_LEN and v not in seen:
            seen.add(v)
            out.append(v)
    return out


def collect_record_secret_values(record: dict[str, Any]) -> list[str]:
    """Fail-closed secret collection from an evidence record.

    Every fixture and contract-env string VALUE is a secret unless its
    key is explicitly declared public in ``public_fixture_keys`` /
    ``public_contract_env_keys``. Key names are never trusted: a fixture
    key called ``STAFF_KEY`` or ``DIRECTUS_STATIC`` is redacted just like
    ``ADMIN_KEY``. Captured values and minted tokens are always secret.

    Non-string fixture/contract-env values (ints, bools) are kept as-is
    and are not secret-collected: replacing a number everywhere would
    shred innocent text (ports, status codes).
    """
    values: list[str] = []
    public_fixture = set(record.get("public_fixture_keys") or [])
    public_env = set(record.get("public_contract_env_keys") or [])
    fixture = record.get("fixture") or {}
    contract_env = record.get("contract_env") or {}
    if isinstance(fixture, dict):
        for k, v in fixture.items():
            if k not in public_fixture and isinstance(v, str):
                values.append(v)
    if isinstance(contract_env, dict):
        for k, v in contract_env.items():
            if k not in public_env and isinstance(v, str):
                values.append(v)
    captured = record.get("captured_values") or {}
    if isinstance(captured, dict):
        for v in captured.values():
            if isinstance(v, str):
                values.append(v)
    minted = record.get("minted_tokens") or []
    if isinstance(minted, list):
        for v in minted:
            if isinstance(v, str):
                values.append(v)
    seen: set[str] = set()
    out: list[str] = []
    for v in sorted(values, key=len, reverse=True):
        if len(v) >= _MIN_SECRET_LEN and v not in seen:
            seen.add(v)
            out.append(v)
    return out


class SecretRedactor:
    """Redact known secret values plus pattern backstops."""

    def __init__(self, secret_values: Iterable[str] | None = None):
        vals = [v for v in (secret_values or []) if v and len(v) >= _MIN_SECRET_LEN]
        self._values = sorted(set(vals), key=len, reverse=True)

    def _redact_string(self, s: str) -> str:
        for v in self._values:
            if v in s:
                s = s.replace(v, REDACTED)
        s = _JWT_RE.sub(REDACTED_JWT, s)
        s = _BEARER_RE.sub(r"\1" + REDACTED, s)
        return s

    def redact(self, obj: Any) -> Any:
        if isinstance(obj, dict):
            out: dict[Any, Any] = {}
            for k, v in obj.items():
                if isinstance(k, str) and _is_secret_key(k):
                    if isinstance(k, str) and k.lower() in ("authorization", "proxy-authorization"):
                        # Keep the auth scheme ("Bearer ***REDACTED***"):
                        # the scheme is structure, the credential is secret.
                        out[k] = self._redact_string(v) if isinstance(v, str) else REDACTED
                    else:
                        out[k] = REDACTED
                else:
                    out[k] = self.redact(v)
            return out
        if isinstance(obj, list):
            return [self.redact(v) for v in obj]
        if isinstance(obj, str):
            return self._redact_string(obj)
        return obj


def redact_secrets(obj: Any, secret_values: Iterable[str] | None = None) -> Any:
    """Redact secrets from ``obj``: by value, with pattern backstops."""
    return SecretRedactor(secret_values).redact(obj)


def redact_record(
    record: dict[str, Any], secret_values: Iterable[str] | None = None
) -> dict[str, Any]:
    """Redact a record for writing. ``parsed_probes`` is stored verbatim."""
    redactor = SecretRedactor(secret_values)
    out: dict[str, Any] = {}
    for k, v in record.items():
        if k == "parsed_probes":
            out[k] = v  # structure, not secrets; hash must still match
        else:
            out[k] = redactor.redact(v)
    return out


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_canonical(obj: Any) -> str:
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def build_evidence_record(
    *,
    run_kind: str,
    bug_id: str | None = None,
    clean_id: str | None = None,
    fix_sha: str | None = None,
    fix_parent_sha: str | None = None,
    # B (bug-introducing commit) and B^, resolved separately from fix^.
    # The run pair compares fix^ vs fix; B/B^ are recorded for provenance.
    b_sha: str | None = None,
    b_parent_sha: str | None = None,
    # Clean-diff runs use these explicit fields (never the fix_* ones).
    c_sha: str | None = None,
    c_provision: dict[str, Any] | None = None,
    c_results: list[ProbeResult] | None = None,
    prompt: str | None = None,
    raw_model_response: str | None = None,
    parsed_probes: list[dict[str, Any]] | None = None,
    # Addendum 11 rule 6: author blocks that failed validation, recorded
    # verbatim with their rejection reason. Entries: {"block_index",
    # "raw_text", "rejection_reason"}. Never sent as probes.
    invalid_probe_blocks: list[dict[str, Any]] | None = None,
    bundle_manifest: dict[str, Any] | None = None,
    bundle_manifest_sha256: str | None = None,
    skill_sha256: str | None = None,
    model_id: str | None = None,
    model_temperature: float | None = None,
    model_attempts: int | None = None,
    # The exact model request as sent (model, temperature, message
    # hashes, seed/tools as sent). None when the model was not called
    # through the in-repo client.
    model_request: dict[str, Any] | None = None,
    # SHA-256 of the frozen author-prompt template file
    # (proofdeploy/author_prompt_v4.md). The template is versioned and
    # must be registered before any measured run.
    author_prompt_sha256: str | None = None,
    # SHA-256 of the prompt builder module (proofdeploy/model_client.py).
    # Recorded next to author_prompt_sha256 so the exact rendering code
    # is pinned in every evidence record.
    author_prompt_builder_sha256: str | None = None,
    # Model call attempt log (for INCONCLUSIVE runs): list of strings like
    # "attempt_1: model_transport: ...", "attempt_2: success".
    model_call_attempts: list[str] | None = None,
    # Truncation flags from prompt construction: which caps applied
    # (diff_files_truncated, file_bytes_truncated, listed_files_truncated).
    truncation_flags: dict[str, Any] | None = None,
    # Isolation actually in effect for the sandbox apps (per side:
    # network mode, userns mode, uid the app ran as). Empty when the
    # sandbox was not used.
    sandbox_evidence: dict[str, Any] | None = None,
    # Whether the measured sides ran inside the sandbox. False only via
    # the explicit dev flag ``allow_unsandboxed``.
    sandboxed: bool = True,
    # Whether this record counts as a measured result. An unsandboxed
    # dev run is marked measured_result=False and must never be treated
    # as a measured result.
    measured_result: bool = True,
    # Provenance for CI-driven runs: Actions run URL/ID, runner image,
    # and the capability probe output. Empty when not provided.
    provenance: dict[str, Any] | None = None,
    harness_version: str = HARNESS_VERSION,
    scoring_version: str = DEFAULT_SCORING_VERSION,
    fix_parent_provision: dict[str, Any] | None = None,
    fix_provision: dict[str, Any] | None = None,
    fix_parent_results: list[ProbeResult] | None = None,
    fix_results: list[ProbeResult] | None = None,
    runner_log: list[str] | None = None,
    # Fixture and contract-env values used by the run. EVERY value is
    # treated as a secret for redaction unless its key is listed in the
    # matching public_*_keys (fail-closed). Non-string values are kept
    # as-is and are not secret-collected.
    fixture: dict[str, Any] | None = None,
    contract_env: dict[str, Any] | None = None,
    public_fixture_keys: list[str] | None = None,
    public_contract_env_keys: list[str] | None = None,
    # Every capture binding collected during the run (name -> value):
    # all values are secret-collected.
    captured_values: dict[str, str] | None = None,
    # Every token minted by the credential provider during the run:
    # all are secret-collected.
    minted_tokens: list[str] | None = None,
    # O3: crash record fields. When the orchestrator crashes after
    # authoring, these preserve the reason and everything gathered
    # before the crash.
    inconclusive_cause: str | None = None,
    inconclusive_error: str | None = None,
    crash_provisioning: dict[str, Any] | None = None,
    crash_probe_results: Any | None = None,
) -> dict[str, Any]:
    """Build the evidence record for a run pair (before scoring).

    The record holds everything needed to replay and audit the run:
    the prompt, the raw model response, the parsed probes (verbatim) and
    their hash, the bundle manifest (skill hash or no-skill marker), the
    harness version, both provisioning results (logs, runtime versions,
    lockfile hashes), and the per-probe results with typed reasons and
    the recorded requests (act and setup steps).

    ``skill_sha256`` is None for no-skill baseline runs. For clean-diff
    runs (``run_kind="clean"``), the probes run against C only: use
    ``c_sha``/``c_provision``/``c_results``; the ``fix_*`` fields stay
    None.
    """
    parsed = parsed_probes or []
    # Fail-closed secret collection from the probe results themselves.
    # The executor attaches each run's captured bindings and minted
    # tokens to the ProbeResult (never serialized); we collect them
    # here so a caller that forgets to pass captured_values cannot
    # leak a session token into the committed record.
    auto_captured: dict[str, str] = {}
    auto_minted: list[str] = []
    for _r in (fix_parent_results or []) + (fix_results or []) + (c_results or []):
        for _k, _v in _r.captured_bindings.items():
            auto_captured.setdefault(_k, _v)
        auto_minted.extend(_r.minted_tokens)
    # Caller-supplied values are explicit and win on name conflicts.
    merged_captured = {**auto_captured, **(captured_values or {})}
    _seen_minted = set(minted_tokens or [])
    merged_minted = list(minted_tokens or [])
    for _t in auto_minted:
        if _t not in _seen_minted:
            _seen_minted.add(_t)
            merged_minted.append(_t)
    record: dict[str, Any] = {
        "record_id": uuid.uuid4().hex,
        "timestamp": _utc_now(),
        "record_type": "evidence",
        "schema_version": SCHEMA_VERSION,
        "run_kind": run_kind,
        "bug_id": bug_id,
        "clean_id": clean_id,
        "fix_sha": fix_sha,
        "fix_parent_sha": fix_parent_sha,
        "b_sha": b_sha,
        "b_parent_sha": b_parent_sha,
        "c_sha": c_sha,
        "c_provision": c_provision or {},
        "c_results": [r.to_dict() for r in (c_results or [])],
        "prompt": prompt,
        "prompt_sha256": hashlib.sha256((prompt or "").encode("utf-8")).hexdigest(),
        "raw_model_response": raw_model_response,
        "parsed_probes": parsed,
        "parsed_probes_sha256": _sha256_canonical(parsed),
        "invalid_probe_blocks": invalid_probe_blocks or [],
        "bundle_manifest": bundle_manifest or {},
        "bundle_manifest_sha256": bundle_manifest_sha256,
        "skill_sha256": skill_sha256,
        "model_id": model_id,
        "model_temperature": model_temperature,
        "model_attempts": model_attempts,
        "model_call_attempts": model_call_attempts,
        "model_request": model_request or {},
        "author_prompt_sha256": author_prompt_sha256,
        "author_prompt_builder_sha256": author_prompt_builder_sha256,
        "truncation_flags": truncation_flags or {},
        "sandbox_evidence": sandbox_evidence or {},
        "sandboxed": sandboxed,
        "measured_result": measured_result,
        "provenance": provenance or {},
        "harness_version": harness_version,
        "scoring_version": scoring_version,
        "fix_parent_provision": fix_parent_provision or {},
        "fix_provision": fix_provision or {},
        "fix_parent_results": [r.to_dict() for r in (fix_parent_results or [])],
        "fix_results": [r.to_dict() for r in (fix_results or [])],
        "runner_log": runner_log or [],
        "fixture": fixture or {},
        "contract_env": contract_env or {},
        "public_fixture_keys": public_fixture_keys or [],
        "public_contract_env_keys": public_contract_env_keys or [],
        "captured_values": merged_captured,
        "minted_tokens": merged_minted,
        # O3: crash record fields (only set on orchestrator crashes).
        "inconclusive_cause": inconclusive_cause,
        "inconclusive_error": inconclusive_error,
        "crash_provisioning": crash_provisioning or {},
        "crash_probe_results": crash_probe_results,
        "verdict": None,
    }
    return record


class EvidenceNotCommittedError(RuntimeError):
    """Raised when scoring is attempted without committed evidence."""


def _git(repo: Path, *args: str) -> str:
    r = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr.strip()}")
    return r.stdout.strip()


def _rel_log_path(repo: str | Path, output_dir: str | Path) -> str:
    """Path of the JSONL log relative to the repo root."""
    return str(Path(output_dir).relative_to(Path(repo)) / RUNS_LOG_NAME)


def evidence_commit_sha(
    evidence_record_id: str, repo: str | Path, output_dir: str | Path
) -> str | None:
    """Return the commit SHA containing the evidence record, or None.

    The oldest commit touching the record id: the score record cites the
    evidence id too, so the newest match would be the score commit.
    """
    repo = Path(repo)
    rel = _rel_log_path(repo, output_dir)
    try:
        out = _git(
            repo,
            "log",
            "--all",
            "--reverse",
            "--format=%H",
            "-S",
            evidence_record_id,
            "--",
            rel,
        )
    except RuntimeError:
        return None  # e.g. no commits yet
    lines = [ln for ln in out.splitlines() if ln.strip()]
    return lines[0] if lines else None


def commit_records(
    repo: str | Path,
    output_dir: str | Path,
    message: str,
) -> str:
    """Commit the JSONL log (used for the score commit). Returns the SHA."""
    repo = Path(repo)
    rel = _rel_log_path(repo, output_dir)
    _git(repo, "add", rel)
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _append_record(record: dict[str, Any], output_dir: str | Path) -> tuple[str, Path]:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / RUNS_LOG_NAME
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, separators=(",", ":")) + "\n")
    return str(record["record_id"]), path


def write_evidence_record(
    record: dict[str, Any],
    output_dir: str | Path,
    repo: str | Path,
    secret_values: Iterable[str] | None = None,
) -> tuple[str, Path, str]:
    """Append the evidence record and commit it. Returns (record_id, path, commit_sha).

    Secret collection is fail-closed: every fixture and contract-env
    value in the record is treated as secret unless its key is declared
    public, plus every captured value and minted token. The optional
    ``secret_values`` adds extra values on top; the record is never
    trusted to be secret-free on its own.

    The record is secret-redacted before it is written, with
    ``parsed_probes`` kept verbatim. The commit is the enforcement of
    "evidence before score": scoring refuses unless the evidence record
    is present in a commit.
    """
    collected = collect_record_secret_values(record)
    extra = [v for v in (secret_values or []) if isinstance(v, str)]
    redacted = redact_record(record, collected + extra)
    record_id, path = _append_record(redacted, output_dir)
    rel = _rel_log_path(repo, output_dir)
    _git(Path(repo), "add", rel)
    _git(Path(repo), "commit", "-m", f"evidence: run-pair record {record_id}")
    commit_sha = _git(Path(repo), "rev-parse", "HEAD")
    return record_id, path, commit_sha


class ScoreMismatchError(ValueError):
    """A caller-supplied score claim disagrees with the committed evidence."""


def _read_record_at_commit(
    repo: str | Path, output_dir: str | Path, commit_sha: str, record_id: str
) -> dict[str, Any] | None:
    """Read one record from the JSONL log as it exists at a commit."""
    repo = Path(repo)
    rel = _rel_log_path(repo, output_dir)
    try:
        content = _git(repo, "show", f"{commit_sha}:{rel}")
    except RuntimeError:
        return None
    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict) and rec.get("record_id") == record_id:
            return rec
    return None


def _find_record_in_log(output_dir: str | Path, record_id: str) -> dict[str, Any] | None:
    """Find one record in the working-tree JSONL log."""
    for rec in read_records(output_dir):
        if rec.get("record_id") == record_id:
            return rec
    return None


def build_score_record(
    *,
    evidence_record_id: str,
    evidence_commit_sha: str,
    run_kind: str,
    bug_id: str | None = None,
    clean_id: str | None = None,
    # Stored verdict for a bug run: "candidate_catch" | "no_catch" |
    # "inconclusive". It is "candidate_catch", never "catch": only a
    # judgment makes it a catch. None for clean runs (they score
    # false_alarm instead).
    score_verdict: str | None = None,
    candidate_catch: list[int] | None = None,
    false_alarm: bool | None = None,
    scoring_version: str = DEFAULT_SCORING_VERSION,
    note: str | None = None,
) -> dict[str, Any]:
    """Build the score record for a run pair (after the evidence record).

    For bug runs: ``score_verdict`` plus ``candidate_catch`` (indices of
    every probe that went FAIL->PASS). For clean runs: ``false_alarm``.
    """
    return {
        "record_id": uuid.uuid4().hex,
        "timestamp": _utc_now(),
        "record_type": "score",
        "schema_version": SCHEMA_VERSION,
        "run_kind": run_kind,
        "bug_id": bug_id,
        "clean_id": clean_id,
        "evidence_record_id": evidence_record_id,
        "evidence_commit_sha": evidence_commit_sha,
        "verdict": score_verdict,
        "candidate_catch": candidate_catch or [],
        "false_alarm": false_alarm,
        "scoring_version": scoring_version,
        "note": note,
    }


def write_score_record(
    output_dir: str | Path,
    repo: str | Path,
    *,
    evidence_record_id: str,
    run_kind: str,
    bug_id: str | None = None,
    clean_id: str | None = None,
    # Caller claims: every one is validated against the committed
    # evidence and refused (ScoreMismatchError) on disagreement.
    verdict: RunPairVerdict | None = None,
    candidate_catch: list[int] | None = None,
    false_alarm: bool | None = None,
    note: str | None = None,
    secret_values: Iterable[str] | None = None,
) -> tuple[str, Path]:
    """Score the run pair from the COMMITTED evidence. Never trusts claims.

    1. Refuses (EvidenceNotCommittedError) unless the evidence record is
       in a commit.
    2. Reads the evidence record back at that commit, rebuilds the
       per-probe results, and scores them.
    3. Refuses (ScoreMismatchError) any caller-passed verdict,
       candidate_catch, false_alarm, bug_id, clean_id, or run_kind that
       does not match what the evidence shows.

    The stored verdict for a bug run is "candidate_catch", never
    "catch": only a judgment record makes it a catch. The caller commits
    the score in a later commit via commit_records().
    """
    sha = evidence_commit_sha(evidence_record_id, repo, output_dir)
    if sha is None:
        raise EvidenceNotCommittedError(
            f"evidence record {evidence_record_id} is not in any commit; "
            "write and commit the evidence record before scoring"
        )
    evidence = _read_record_at_commit(repo, output_dir, sha, evidence_record_id)
    if evidence is None or evidence.get("record_type") != "evidence":
        raise EvidenceNotCommittedError(
            f"evidence record {evidence_record_id} not found at commit {sha}"
        )
    if evidence.get("run_kind") != run_kind:
        raise ScoreMismatchError(
            f"run_kind {run_kind!r} does not match evidence {evidence.get('run_kind')!r}"
        )
    if bug_id is not None and evidence.get("bug_id") != bug_id:
        raise ScoreMismatchError(
            f"bug_id {bug_id!r} does not match evidence {evidence.get('bug_id')!r}"
        )
    if clean_id is not None and evidence.get("clean_id") != clean_id:
        raise ScoreMismatchError(
            f"clean_id {clean_id!r} does not match evidence {evidence.get('clean_id')!r}"
        )

    if run_kind == "clean":
        c_results = [ProbeResult.from_dict(d) for d in (evidence.get("c_results") or [])]
        expected_alarm = score_clean_diff(c_results)
        if false_alarm is not None and false_alarm != expected_alarm:
            raise ScoreMismatchError(
                f"false_alarm={false_alarm} does not match evidence ({expected_alarm})"
            )
        record = build_score_record(
            evidence_record_id=evidence_record_id,
            evidence_commit_sha=sha,
            run_kind="clean",
            clean_id=evidence.get("clean_id"),
            score_verdict=None,
            candidate_catch=[],
            false_alarm=expected_alarm,
            scoring_version=evidence.get("scoring_version", DEFAULT_SCORING_VERSION),
            note=note,
        )
    else:
        parent = [ProbeResult.from_dict(d) for d in (evidence.get("fix_parent_results") or [])]
        fix = [ProbeResult.from_dict(d) for d in (evidence.get("fix_results") or [])]
        detailed = score_run_pair_detailed(parent, fix)
        if verdict is not None and verdict != detailed.verdict:
            raise ScoreMismatchError(
                f"verdict {verdict.value!r} does not match evidence ({detailed.verdict.value!r})"
            )
        if candidate_catch is not None and list(candidate_catch) != detailed.candidate_catch:
            raise ScoreMismatchError(
                f"candidate_catch {candidate_catch} does not match "
                f"evidence ({detailed.candidate_catch})"
            )
        score_verdict = (
            "candidate_catch"
            if detailed.verdict == RunPairVerdict.CATCH
            else detailed.verdict.value
        )
        record = build_score_record(
            evidence_record_id=evidence_record_id,
            evidence_commit_sha=sha,
            run_kind=run_kind,
            bug_id=evidence.get("bug_id"),
            score_verdict=score_verdict,
            candidate_catch=detailed.candidate_catch,
            scoring_version=evidence.get("scoring_version", DEFAULT_SCORING_VERSION),
            note=note,
        )

    extra = [v for v in (secret_values or []) if isinstance(v, str)]
    redacted = redact_record(record, extra)
    return _append_record(redacted, output_dir)


class JudgmentRejectedError(ValueError):
    """A judgment record failed validation."""


# Fixed set of judge roles. Judgments are allowlisted: an unknown role
# is refused. On measured (bug) runs only "reviewer" may judge; the
# role is recorded so the provenance of every catch is auditable.
JUDGE_ROLES = frozenset({"reviewer", "implementer", "auditor"})


def build_judgment_record(
    *,
    evidence_record_id: str,
    score_record_id: str,
    evidence_commit_sha: str,
    run_kind: str,
    bug_id: str | None = None,
    clean_id: str | None = None,
    probe_index: int | None = None,
    judge: str | None = None,
    judge_role: str | None = None,
    condition3_met: bool | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    """Build the final CATCH judgment record (reviewer decision).

    Condition 3 of the registered rule ("targets behavior the fix
    actually changed") is a reviewer judgment made from the probe
    against the fix diff. This record is append-only and cites the
    evidence and score records (and the evidence commit). ``verdict``
    is "catch" only when the reviewer judges condition 3 met for the
    cited probe.

    ``condition3_met`` must be a bool and ``judge_role`` must be
    recorded; anything else raises. (Role allowlisting and the
    reviewer-only rule for measured runs are enforced in
    write_judgment_record.)
    """
    if not isinstance(condition3_met, bool):
        raise JudgmentRejectedError(f"condition3_met must be a bool, got {condition3_met!r}")
    if judge_role not in JUDGE_ROLES:
        raise JudgmentRejectedError(
            f"judge_role must be one of {sorted(JUDGE_ROLES)}, got {judge_role!r}"
        )
    return {
        "record_id": uuid.uuid4().hex,
        "timestamp": _utc_now(),
        "record_type": "judgment",
        "schema_version": SCHEMA_VERSION,
        "run_kind": run_kind,
        "bug_id": bug_id,
        "clean_id": clean_id,
        "evidence_record_id": evidence_record_id,
        "score_record_id": score_record_id,
        "evidence_commit_sha": evidence_commit_sha,
        "probe_index": probe_index,
        "judge": judge,
        "judge_role": judge_role,
        "condition3_met": condition3_met,
        "verdict": "catch" if condition3_met else "no_catch",
        "note": note,
    }


def write_judgment_record(
    record: dict[str, Any],
    output_dir: str | Path,
    repo: str | Path,
    *,
    score_record_id: str,
    secret_values: Iterable[str] | None = None,
) -> tuple[str, Path]:
    """Validate and append the judgment record.

    - The score record must exist in the log.
    - The judgment's evidence_record_id and evidence_commit_sha must
      match the score's (a judgment cannot be attached to a different
      evidence than the score it cites).
    - ``probe_index`` must be in the score's ``candidate_catch``.
    - ``condition3_met`` must be a bool.
    - ``judge_role`` must be in the fixed JUDGE_ROLES set. On measured
      (bug) runs the role must be ``reviewer`` and ``judge`` must not be
      empty: the implementer cannot judge its own catch.

    The reviewer commits the judgment separately. Raises
    JudgmentRejectedError on any violation.
    """
    score = _find_record_in_log(output_dir, score_record_id)
    if score is None or score.get("record_type") != "score":
        raise JudgmentRejectedError(f"score record {score_record_id} not found in log")
    if record.get("evidence_record_id") != score.get("evidence_record_id"):
        raise JudgmentRejectedError(
            f"evidence_record_id {record.get('evidence_record_id')!r} does not match "
            f"the score's {score.get('evidence_record_id')!r}"
        )
    if record.get("evidence_commit_sha") != score.get("evidence_commit_sha"):
        raise JudgmentRejectedError(
            f"evidence_commit_sha {record.get('evidence_commit_sha')!r} does not match "
            f"the score's {score.get('evidence_commit_sha')!r}"
        )
    candidates = score.get("candidate_catch") or []
    if record.get("probe_index") not in candidates:
        raise JudgmentRejectedError(
            f"probe_index {record.get('probe_index')!r} is not in the score's "
            f"candidate_catch {candidates}"
        )
    if not isinstance(record.get("condition3_met"), bool):
        raise JudgmentRejectedError(
            f"condition3_met must be a bool, got {record.get('condition3_met')!r}"
        )
    if record.get("judge_role") not in JUDGE_ROLES:
        raise JudgmentRejectedError(
            f"judge_role must be one of {sorted(JUDGE_ROLES)}, got {record.get('judge_role')!r}"
        )
    if score.get("run_kind") == "bug":
        if record.get("judge_role") != "reviewer":
            raise JudgmentRejectedError(
                f"bug runs must be judged by role 'reviewer', got {record.get('judge_role')!r}"
            )
        if not record.get("judge"):
            raise JudgmentRejectedError("judge must not be empty on bug runs")
    if record.get("run_kind") != score.get("run_kind"):
        raise JudgmentRejectedError(
            f"run_kind {record.get('run_kind')!r} does not match score {score.get('run_kind')!r}"
        )
    extra = [v for v in (secret_values or []) if isinstance(v, str)]
    redacted = redact_record(record, extra)
    return _append_record(redacted, output_dir)


def read_records(output_dir: str | Path) -> list[dict[str, Any]]:
    """Read all records from the JSONL log, in append order."""
    path = Path(output_dir) / RUNS_LOG_NAME
    if not path.is_file():
        return []
    records = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records
