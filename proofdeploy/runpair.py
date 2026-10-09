"""Run pair and JSONL records for the ProofDeploy runner (WAL-58, part 4 of 5).

The run pair executes the same probe set against two commits:
- fix^ (the parent of the fix): the bug should be present → probes FAIL
- fix: the bug should be fixed → probes PASS

A catch is FAIL on fix^ and PASS on fix. If the probes cannot run
(API drift, start failure), the result is INCONCLUSIVE.

Every run writes a JSONL record holding the prompt, the raw model
response, the parsed probes, the bundle manifest (skill hash or no-skill
marker), and the runner log. Records are written before any scoring, so
the evidence is committed independently of the verdict.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from proofdeploy.executor import ProbeResult, Verdict


class RunPairVerdict(Enum):
    """Verdict for a fix^/fix run pair."""

    CATCH = "catch"  # FAIL on fix^, PASS on fix
    NO_CATCH = "no_catch"  # anything else where both runs completed
    INCONCLUSIVE = "inconclusive"  # could not run (drift, start failure)


@dataclass
class RunRecord:
    """One JSONL record for a run pair."""

    timestamp: str
    bug_id: str
    fix_sha: str
    fix_parent_sha: str
    prompt: str
    raw_model_response: str
    parsed_probes: list[dict[str, Any]]
    bundle_manifest: dict[str, Any]
    skill_sha256: str | None  # None for no-skill baseline runs
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


def score_run_pair(
    parent_results: list[ProbeResult] | None,
    fix_results: list[ProbeResult] | None,
) -> RunPairVerdict:
    """Score a fix^/fix run pair.

    - Either side None (could not run) → INCONCLUSIVE.
    - Any INCONCLUSIVE probe on either side → INCONCLUSIVE.
    - FAIL on fix^ and PASS on fix (all probes) → CATCH.
    - Otherwise → NO_CATCH.
    """
    if parent_results is None or fix_results is None:
        return RunPairVerdict.INCONCLUSIVE
    if not parent_results or not fix_results:
        return RunPairVerdict.INCONCLUSIVE

    for r in list(parent_results) + list(fix_results):
        if r.verdict == Verdict.INCONCLUSIVE:
            return RunPairVerdict.INCONCLUSIVE

    parent_fail = any(r.verdict == Verdict.FAIL for r in parent_results)
    parent_pass = all(r.verdict == Verdict.PASS for r in parent_results)
    fix_pass = all(r.verdict == Verdict.PASS for r in fix_results)

    if parent_fail and fix_pass:
        return RunPairVerdict.CATCH
    if parent_pass and fix_pass:
        return RunPairVerdict.NO_CATCH
    # fix^ fails and fix fails, or mixed: no catch (bug not fixed or
    # probes don't discriminate).
    return RunPairVerdict.NO_CATCH


def write_record(record: RunRecord, output_dir: Path) -> Path:
    """Write a run record as JSONL. Returns the path written.

    The record is written before any scoring decision, so the evidence
    stands independently of the verdict.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{record.bug_id}-{record.fix_sha[:12]}.jsonl"
    with path.open("w", encoding="utf-8") as f:
        f.write(json.dumps(record.to_dict()) + "\n")
    return path


def _results_to_dicts(results: list[ProbeResult]) -> list[dict[str, Any]]:
    return [r.to_dict() for r in results]
