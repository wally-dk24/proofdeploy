"""Offline replay of recorded probe evidence (Addendum 36).

A replay re-evaluates every probe's assertions against the
responses recorded in an evidence record and derives the verdict
with the registered scoring rule. It is the product's ONE replay
implementation: it runs the executor's own verdict loop
(``evaluate_assertions``), so replay semantics can never drift
from live semantics, and it never echoes the stored per-probe
verdicts — tampering with a recorded response changes the replay
outcome.

Two kinds of probes are typed NOT REPLAYABLE (``NotReplayableError``),
never silently approximated:

- a probe with a ``db`` assertion — DB query results are not
  recorded, so the assertion cannot be re-decided offline;
- a probe whose recorded body was truncated
  (``http_body_truncated``, schema 1.2.0) — deciding from a cut
  body would invent evidence.
"""

from __future__ import annotations

from typing import Any

from proofdeploy.executor import (
    InconclusiveReason,
    ProbeResult,
    Verdict,
    evaluate_assertions,
)
from proofdeploy.runpair import RunPairVerdict, score_run_pair_detailed


class NotReplayableError(ValueError):
    """A recorded probe cannot be re-decided from its evidence."""


def replay_probe(probe: dict, result: dict) -> ProbeResult:
    """Re-decide ONE probe from its recorded response."""
    idx = result.get("probe_index", 0)
    assertions = probe.get("assert") or []
    for assertion in assertions:
        if assertion.get("type") == "db":
            raise NotReplayableError(
                f"probe {idx}: db assertions are not replayable "
                "(DB results are not recorded)"
            )
    if result.get("http_body_truncated"):
        raise NotReplayableError(
            f"probe {idx}: recorded body was truncated; "
            "refusing to decide from partial evidence"
        )
    if not result.get("sent", True):
        return ProbeResult(
            probe_index=idx,
            verdict=Verdict.INCONCLUSIVE,
            reason=InconclusiveReason.PROBE,
            sent=False,
        )
    verdict, reason, _details = evaluate_assertions(
        assertions,
        result.get("http_status"),
        result.get("http_headers") or {},
        result.get("http_body") or "",
        {},
    )
    return ProbeResult(
        probe_index=idx, verdict=verdict, reason=reason, sent=True
    )


def replay_side(evidence: dict, results_key: str) -> list[ProbeResult]:
    """Re-decide every probe of one side of an evidence record
    (``fix_parent_results`` / ``fix_results`` / ``c_results``)."""
    probes = evidence.get("parsed_probes") or []
    rebuilt: list[ProbeResult] = []
    for result in evidence.get(results_key) or []:
        idx = result.get("probe_index", 0)
        probe = probes[idx] if 0 <= idx < len(probes) else {}
        rebuilt.append(replay_probe(probe, result))
    return rebuilt


def replay_verdict(evidence: dict[str, Any]) -> str:
    """The bug-run verdict a replay of this evidence record
    produces, in the stored-verdict vocabulary:
    "candidate_catch", "no_catch", or "inconclusive"."""
    detailed = score_run_pair_detailed(
        replay_side(evidence, "fix_parent_results"),
        replay_side(evidence, "fix_results"),
    )
    if detailed.verdict == RunPairVerdict.CATCH:
        return "candidate_catch"
    return str(detailed.verdict.value)
