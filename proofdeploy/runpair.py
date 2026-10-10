"""Run pair and JSONL records for the ProofDeploy runner (WAL-58, WO-4).

A run pair executes the same probe set against two targets:
- fix^ (the parent of the fix): the bug should be present → probes FAIL
- fix: the bug should be fixed → probes PASS

Scoring implements the registered run-pair rule (docs/evaluation/
scoring-rule-run-pair-2026-10-09.md, merged as PR #38):

- Bugs are scored per probe: a bug is caught if at least one probe FAILs
  on fix^ and PASSes on fix. A sibling probe that is INCONCLUSIVE or
  FAILs on both sides does not cancel the catch. (Condition 3 of the
  registered rule — "targets behavior the fix actually changed" — is a
  reviewer judgment made from the probe against the fix diff; every probe
  is reported so the reviewer can judge.)
- If fix^ or fix cannot build or start, or the target is unreachable for
  every probe, the pair is INCONCLUSIVE. There is nothing to compare.
- Clean diffs are strict: probes run against C only, and any FAIL or
  INCONCLUSIVE is a false alarm.

The legacy all-probes-must-agree rule is kept behind the
``v1-all-probes`` scoring-version flag for reference. The registered
``v2-per-probe`` rule is the default.

Records:
- Every run writes an evidence record BEFORE any scoring. The evidence
  record holds the prompt, the raw model response, the parsed probes and
  their hash, the bundle manifest (skill hash or no-skill marker), the
  harness version, both provisioning results (logs, runtime versions,
  lockfile hashes), and the per-probe results with reasons.
- The score is written as a SEPARATE later record that references the
  evidence record by id. Evidence is committed before the score is.
- Records are append-only: each record gets a unique id and is appended
  to a JSONL log. Repeats never overwrite.
- Records are secret-redacted: fixture secrets (ADMIN_KEY, bearer tokens,
  minted JWTs) are redacted everywhere they appear, including in recorded
  request headers. Redaction runs on the whole record before it is
  written.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from proofdeploy.executor import ProbeResult, Verdict
from proofdeploy.probe import HARNESS_VERSION

# Scoring versions. v2 is the registered rule (2026-10-09, PR #38):
# per-probe catches. v1 is the legacy all-probes-must-agree rule,
# kept for reference behind the flag.
SCORING_VERSION_V1 = "v1-all-probes"
SCORING_VERSION_V2 = "v2-per-probe"
DEFAULT_SCORING_VERSION = SCORING_VERSION_V2

# Name of the append-only JSONL log inside the output directory.
RUNS_LOG_NAME = "runs.jsonl"


class RunPairVerdict(Enum):
    """Verdict for a fix^/fix run pair."""

    CATCH = "catch"  # at least one probe: FAIL on fix^, PASS on fix
    NO_CATCH = "no_catch"  # ran, but no probe caught the bug
    INCONCLUSIVE = "inconclusive"  # could not run (drift, start failure)


def _results_by_index(
    results: list[ProbeResult],
) -> dict[int, ProbeResult]:
    return {r.probe_index: r for r in results}


def _score_run_pair_v2(
    parent_results: list[ProbeResult] | None,
    fix_results: list[ProbeResult] | None,
) -> RunPairVerdict:
    """Registered per-probe scoring (PR #38).

    - Either side None (could not build/start) → INCONCLUSIVE.
    - Empty results → INCONCLUSIVE.
    - Every probe INCONCLUSIVE on either side (target unreachable for
      every probe) → INCONCLUSIVE. There is nothing to compare.
    - CATCH if at least one probe FAILs on fix^ and PASSes on fix
      (matched by probe_index). A sibling INCONCLUSIVE or
      non-discriminating probe does not cancel the catch.
    - Otherwise → NO_CATCH.
    """
    if parent_results is None or fix_results is None:
        return RunPairVerdict.INCONCLUSIVE
    if not parent_results or not fix_results:
        return RunPairVerdict.INCONCLUSIVE
    if all(r.verdict == Verdict.INCONCLUSIVE for r in parent_results):
        return RunPairVerdict.INCONCLUSIVE
    if all(r.verdict == Verdict.INCONCLUSIVE for r in fix_results):
        return RunPairVerdict.INCONCLUSIVE

    fix_by_index = _results_by_index(fix_results)
    for p in parent_results:
        f = fix_by_index.get(p.probe_index)
        if f is None:
            continue
        if p.verdict == Verdict.FAIL and f.verdict == Verdict.PASS:
            return RunPairVerdict.CATCH
    return RunPairVerdict.NO_CATCH


def _score_run_pair_v1(
    parent_results: list[ProbeResult] | None,
    fix_results: list[ProbeResult] | None,
) -> RunPairVerdict:
    """Legacy all-probes-must-agree scoring, kept for reference.

    Any INCONCLUSIVE probe poisons the pair; a catch needs a FAIL on
    fix^ and all probes PASSing on fix. Superseded by the registered
    v2 per-probe rule.
    """
    if parent_results is None or fix_results is None:
        return RunPairVerdict.INCONCLUSIVE
    if not parent_results or not fix_results:
        return RunPairVerdict.INCONCLUSIVE

    for r in list(parent_results) + list(fix_results):
        if r.verdict == Verdict.INCONCLUSIVE:
            return RunPairVerdict.INCONCLUSIVE

    parent_fail = any(r.verdict == Verdict.FAIL for r in parent_results)
    fix_pass = all(r.verdict == Verdict.PASS for r in fix_results)

    if parent_fail and fix_pass:
        return RunPairVerdict.CATCH
    return RunPairVerdict.NO_CATCH


def score_run_pair(
    parent_results: list[ProbeResult] | None,
    fix_results: list[ProbeResult] | None,
    scoring_version: str = DEFAULT_SCORING_VERSION,
) -> RunPairVerdict:
    """Score a fix^/fix run pair under the given scoring version.

    The default is the registered v2 per-probe rule. Pass
    ``SCORING_VERSION_V1`` for the legacy rule.
    """
    if scoring_version == SCORING_VERSION_V2:
        return _score_run_pair_v2(parent_results, fix_results)
    if scoring_version == SCORING_VERSION_V1:
        return _score_run_pair_v1(parent_results, fix_results)
    raise ValueError(f"unknown scoring version: {scoring_version!r}")


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
# Secret redaction
#
# Records must never leak fixture secrets: ADMIN_KEY, bearer tokens,
# minted JWTs — including in recorded request headers. Redaction runs on
# the whole record before it is written. It is deliberately
# over-eager: a redacted non-secret is a minor loss of detail, a leaked
# secret is a breach.
# ---------------------------------------------------------------------------

REDACTED = "***REDACTED***"
REDACTED_JWT = "***REDACTED_JWT***"

# Dict keys whose values are always treated as secrets (case-insensitive
# substring match on the key).
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


def _redact_string(s: str) -> str:
    s = _JWT_RE.sub(REDACTED_JWT, s)
    s = _BEARER_RE.sub(r"\1" + REDACTED, s)
    return s


def redact_secrets(obj: Any) -> Any:
    """Recursively redact fixture secrets from a record.

    Dict values under secret-named keys become "***REDACTED***";
    JWT-shaped strings become "***REDACTED_JWT***"; "Bearer <token>"
    occurrences keep the scheme and redact the token.
    """
    if isinstance(obj, dict):
        out: dict[Any, Any] = {}
        for k, v in obj.items():
            if isinstance(k, str) and _is_secret_key(k):
                out[k] = REDACTED
            else:
                out[k] = redact_secrets(v)
        return out
    if isinstance(obj, list):
        return [redact_secrets(v) for v in obj]
    if isinstance(obj, str):
        return _redact_string(obj)
    return obj


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
    prompt: str | None = None,
    raw_model_response: str | None = None,
    parsed_probes: list[dict[str, Any]] | None = None,
    bundle_manifest: dict[str, Any] | None = None,
    skill_sha256: str | None = None,
    harness_version: str = HARNESS_VERSION,
    scoring_version: str = DEFAULT_SCORING_VERSION,
    fix_parent_provision: dict[str, Any] | None = None,
    fix_provision: dict[str, Any] | None = None,
    fix_parent_results: list[ProbeResult] | None = None,
    fix_results: list[ProbeResult] | None = None,
    runner_log: list[str] | None = None,
) -> dict[str, Any]:
    """Build the evidence record for a run pair (before scoring).

    The record holds everything needed to replay and audit the run:
    the prompt, the raw model response, the parsed probes and their
    hash, the bundle manifest (skill hash or no-skill marker), the
    harness version, both provisioning results (logs, runtime versions,
    lockfile hashes), and the per-probe results with reasons.

    ``skill_sha256`` is None for no-skill baseline runs. ``fix_*``
    fields are None for clean-diff runs (``run_kind="clean"``), where
    the probes run against C only.
    """
    parsed = parsed_probes or []
    record: dict[str, Any] = {
        "record_id": uuid.uuid4().hex,
        "timestamp": _utc_now(),
        "record_type": "evidence",
        "run_kind": run_kind,
        "bug_id": bug_id,
        "clean_id": clean_id,
        "fix_sha": fix_sha,
        "fix_parent_sha": fix_parent_sha,
        "prompt": prompt,
        "prompt_sha256": hashlib.sha256((prompt or "").encode("utf-8")).hexdigest(),
        "raw_model_response": raw_model_response,
        "parsed_probes": parsed,
        "parsed_probes_sha256": _sha256_canonical(parsed),
        "bundle_manifest": bundle_manifest or {},
        "skill_sha256": skill_sha256,
        "harness_version": harness_version,
        "scoring_version": scoring_version,
        "fix_parent_provision": fix_parent_provision or {},
        "fix_provision": fix_provision or {},
        "fix_parent_results": [r.to_dict() for r in (fix_parent_results or [])],
        "fix_results": [r.to_dict() for r in (fix_results or [])],
        "runner_log": runner_log or [],
        "verdict": None,
    }
    return record


def write_evidence_record(record: dict[str, Any], output_dir: Path | str) -> tuple[str, Path]:
    """Append the evidence record to the JSONL log. Returns (record_id, path).

    The record is secret-redacted before it is written. This must be
    called — and the log committed — BEFORE any scoring.
    """
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / RUNS_LOG_NAME
    redacted = redact_secrets(record)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(redacted, separators=(",", ":")) + "\n")
    return str(record["record_id"]), path


def build_score_record(
    *,
    evidence_record_id: str,
    run_kind: str,
    bug_id: str | None = None,
    clean_id: str | None = None,
    verdict: RunPairVerdict | None = None,
    false_alarm: bool | None = None,
    scoring_version: str = DEFAULT_SCORING_VERSION,
    note: str | None = None,
) -> dict[str, Any]:
    """Build the score record for a run pair (after the evidence record).

    The score is a separate record that references the evidence record
    by id. It is written — and committed — after the evidence record.
    """
    return {
        "record_id": uuid.uuid4().hex,
        "timestamp": _utc_now(),
        "record_type": "score",
        "run_kind": run_kind,
        "bug_id": bug_id,
        "clean_id": clean_id,
        "evidence_record_id": evidence_record_id,
        "verdict": verdict.value if verdict is not None else None,
        "false_alarm": false_alarm,
        "scoring_version": scoring_version,
        "note": note,
    }


def write_score_record(record: dict[str, Any], output_dir: Path | str) -> tuple[str, Path]:
    """Append the score record to the JSONL log. Returns (record_id, path).

    Call this only after the evidence record has been written and
    committed.
    """
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / RUNS_LOG_NAME
    redacted = redact_secrets(record)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(redacted, separators=(",", ":")) + "\n")
    return str(record["record_id"]), path


def read_records(output_dir: Path | str) -> list[dict[str, Any]]:
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


# Kept for backward compatibility with the initial WO-4 branch: the old
# RunRecord dataclass and write_record helper. New code should use
# build_evidence_record / write_evidence_record / build_score_record /
# write_score_record.
@dataclass
class RunRecord:
    """One run-pair record (legacy shape)."""

    timestamp: str
    bug_id: str
    fix_sha: str
    fix_parent_sha: str
    prompt: str
    raw_model_response: str
    parsed_probes: list[dict[str, Any]]
    bundle_manifest: dict[str, Any]
    skill_sha256: str | None
    harness_version: str
    fix_parent_results: list[dict[str, Any]] = field(default_factory=list)
    fix_results: list[dict[str, Any]] = field(default_factory=list)
    runner_log: list[str] = field(default_factory=list)
    verdict: str = "inconclusive"

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "bug_id": self.bug_id,
            "fix_sha": self.fix_sha,
            "fix_parent_sha": self.fix_parent_sha,
            "prompt_sha256": hashlib.sha256(self.prompt.encode()).hexdigest(),
            "prompt": self.prompt,
            "raw_model_response": self.raw_model_response,
            "parsed_probes": self.parsed_probes,
            "bundle_manifest": self.bundle_manifest,
            "skill_sha256": self.skill_sha256,
            "harness_version": self.harness_version,
            "fix_parent_results": self.fix_parent_results,
            "fix_results": self.fix_results,
            "runner_log": self.runner_log,
            "verdict": self.verdict,
        }


def write_record(record: RunRecord, output_dir: Path) -> Path:
    """Write a legacy run record as JSONL (append-only). Returns the path."""
    evidence = build_evidence_record(
        run_kind="bug",
        bug_id=record.bug_id,
        fix_sha=record.fix_sha,
        fix_parent_sha=record.fix_parent_sha,
        prompt=record.prompt,
        raw_model_response=record.raw_model_response,
        parsed_probes=record.parsed_probes,
        bundle_manifest=record.bundle_manifest,
        skill_sha256=record.skill_sha256,
        harness_version=record.harness_version,
        runner_log=record.runner_log,
    )
    # Preserve any pre-serialized results the caller attached.
    evidence["fix_parent_results"] = record.fix_parent_results
    evidence["fix_results"] = record.fix_results
    evidence["verdict"] = record.verdict
    _, path = write_evidence_record(evidence, output_dir)
    return path


def _results_to_dicts(results: list[ProbeResult]) -> list[dict[str, Any]]:
    return [r.to_dict() for r in results]
