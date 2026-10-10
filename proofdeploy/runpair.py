"""Run pair and JSONL records for the ProofDeploy runner (WAL-58, WO-4).

A run pair executes the same probe set against two targets:
- fix^ (the parent of the fix): the bug should be present -> probes FAIL
- fix: the bug should be fixed -> probes PASS

Scoring implements the registered run-pair rule exactly
(docs/evaluation/scoring-rule-run-pair-2026-10-09.md, merged as PR #38):

- Bugs are scored per probe: a bug is caught if at least one probe
  FAILs on fix^ and PASSes on fix. The score record carries
  ``candidate_catch``: the indices of EVERY probe that went FAIL->PASS.
  Condition 3 of the registered rule ("targets behavior the fix actually
  changed") is a reviewer judgment: the final CATCH is a separate,
  append-only judgment record that cites the evidence and score records
  and holds the probe index, the judge, and the condition-3 decision.
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
- Every run writes an evidence record BEFORE any scoring. The evidence
  record holds the prompt, the raw model response, the parsed probes
  (verbatim) and their hash, the bundle manifest (skill hash or no-skill
  marker), the harness version, both provisioning results (logs, runtime
  versions, lockfile hashes), and the per-probe results with typed
  reasons and the recorded requests.
- Writing the evidence record makes a git commit; the commit SHA is
  recorded. Scoring refuses unless the evidence record is present in a
  commit. The score goes in a later commit that cites the evidence
  commit.
- Records are append-only: each record gets a unique id and is appended
  to a JSONL log. Repeats never overwrite.
- Records are secret-redacted by value: every fixture and contract-env
  secret value, every captured value, and every minted token is replaced
  wherever it appears. Pattern matching (JWT shape, "Bearer <token>")
  is a backstop. ``parsed_probes`` is stored verbatim so the stored hash
  still matches after redaction.
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
from proofdeploy.probe import HARNESS_VERSION

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
    (reason ``probe``) are a miss, not infrastructure.
    """
    return all(
        r.verdict == Verdict.INCONCLUSIVE and r.reason == InconclusiveReason.ENVIRONMENT
        for r in results
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
    # Clean-diff runs use these explicit fields (never the fix_* ones).
    c_sha: str | None = None,
    c_provision: dict[str, Any] | None = None,
    c_results: list[ProbeResult] | None = None,
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
    the prompt, the raw model response, the parsed probes (verbatim) and
    their hash, the bundle manifest (skill hash or no-skill marker), the
    harness version, both provisioning results (logs, runtime versions,
    lockfile hashes), and the per-probe results with typed reasons and
    the recorded requests.

    ``skill_sha256`` is None for no-skill baseline runs. For clean-diff
    runs (``run_kind="clean"``), the probes run against C only: use
    ``c_sha``/``c_provision``/``c_results``; the ``fix_*`` fields stay
    None.
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
        "c_sha": c_sha,
        "c_provision": c_provision or {},
        "c_results": [r.to_dict() for r in (c_results or [])],
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

    The record is secret-redacted before it is written, with
    ``parsed_probes`` kept verbatim. The commit is the enforcement of
    "evidence before score": scoring refuses unless the evidence record
    is present in a commit.
    """
    redacted = redact_record(record, secret_values)
    record_id, path = _append_record(redacted, output_dir)
    rel = _rel_log_path(repo, output_dir)
    _git(Path(repo), "add", rel)
    _git(Path(repo), "commit", "-m", f"evidence: run-pair record {record_id}")
    commit_sha = _git(Path(repo), "rev-parse", "HEAD")
    return record_id, path, commit_sha


def build_score_record(
    *,
    evidence_record_id: str,
    run_kind: str,
    bug_id: str | None = None,
    clean_id: str | None = None,
    verdict: RunPairVerdict | None = None,
    candidate_catch: list[int] | None = None,
    false_alarm: bool | None = None,
    scoring_version: str = DEFAULT_SCORING_VERSION,
    note: str | None = None,
) -> dict[str, Any]:
    """Build the score record for a run pair (after the evidence record).

    For bug runs: ``verdict`` plus ``candidate_catch`` (indices of every
    probe that went FAIL->PASS). For clean runs: ``false_alarm``.
    """
    return {
        "record_id": uuid.uuid4().hex,
        "timestamp": _utc_now(),
        "record_type": "score",
        "run_kind": run_kind,
        "bug_id": bug_id,
        "clean_id": clean_id,
        "evidence_record_id": evidence_record_id,
        "evidence_commit_sha": None,  # filled in by write_score_record
        "verdict": verdict.value if verdict is not None else None,
        "candidate_catch": candidate_catch or [],
        "false_alarm": false_alarm,
        "scoring_version": scoring_version,
        "note": note,
    }


def write_score_record(
    record: dict[str, Any],
    output_dir: str | Path,
    repo: str | Path,
    *,
    evidence_record_id: str,
    secret_values: Iterable[str] | None = None,
) -> tuple[str, Path]:
    """Append the score record. Refuses unless the evidence is committed.

    The evidence commit SHA is recorded in the score record; the caller
    commits the score in a later commit. Raises
    EvidenceNotCommittedError if the evidence record is not in any
    commit.
    """
    sha = evidence_commit_sha(evidence_record_id, repo, output_dir)
    if sha is None:
        raise EvidenceNotCommittedError(
            f"evidence record {evidence_record_id} is not in any commit; "
            "write and commit the evidence record before scoring"
        )
    record["evidence_commit_sha"] = sha
    redacted = redact_record(record, secret_values)
    return _append_record(redacted, output_dir)


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
    """
    return {
        "record_id": uuid.uuid4().hex,
        "timestamp": _utc_now(),
        "record_type": "judgment",
        "run_kind": run_kind,
        "bug_id": bug_id,
        "clean_id": clean_id,
        "evidence_record_id": evidence_record_id,
        "score_record_id": score_record_id,
        "evidence_commit_sha": evidence_commit_sha,
        "probe_index": probe_index,
        "judge": judge,
        "condition3_met": condition3_met,
        "verdict": "catch" if condition3_met else "no_catch",
        "note": note,
    }


def write_judgment_record(
    record: dict[str, Any],
    output_dir: str | Path,
    secret_values: Iterable[str] | None = None,
) -> tuple[str, Path]:
    """Append the judgment record. The reviewer commits it separately."""
    redacted = redact_record(record, secret_values)
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
