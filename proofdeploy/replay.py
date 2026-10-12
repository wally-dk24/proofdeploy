"""Offline replay of recorded probe evidence (Addendum 36).

A replay re-evaluates every probe's assertions against the
responses recorded in an evidence record and derives the verdict
with the registered scoring rule. It is the product's ONE replay
implementation: it runs the executor's own verdict loop
(``evaluate_assertions``), so replay semantics can never drift
from live semantics, and it never echoes the stored per-probe
verdicts — tampering with a recorded response changes the replay
outcome.

Replay honesty (Addendum 38): a replay may only re-decide what
the record can actually support.

- A result whose live run returned BEFORE the assertions were
  evaluated (``assertions_evaluated`` false or absent — every
  early return in ``run_probe`` is INCONCLUSIVE and never
  evaluates) is NOT re-decided: replay carries the recorded
  INCONCLUSIVE verdict and reason, labeled not re-decided. It
  never invents a PASS or a FAIL — evaluating a no-response
  record's header/body assertions against the empty recorded
  response fabricated FAILs (a catch that never happened).
- Four kinds of probes are typed NOT REPLAYABLE
  (``NotReplayableError``), never silently approximated:

  - a probe with a ``db`` assertion — DB query results are not
    recorded, so the assertion cannot be re-decided offline;
  - a probe whose recorded body was truncated
    (``http_body_truncated``, schema 1.2.0) — deciding from a cut
    body would invent evidence;
  - an assertion containing a ``${...}`` capture substitution —
    live, the value came from a setup capture; captures are not
    recorded, so the literal comparison would invent a verdict;
  - a body or header assertion against a redacted value — the
    recorded body/header holds ``***REDACTED***`` /
    ``***REDACTED_JWT***`` where the real value was, so the
    assertion cannot be re-checked against what the app sent.
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


# The record redaction markers (proofdeploy.runpair.REDACTED /
# REDACTED_JWT). A recorded value holding one of these is not the
# value the app sent.
_REDACTED_MARKERS = ("***REDACTED***", "***REDACTED_JWT***")


def _strings(value: Any) -> list[str]:
    """Every string inside a (possibly nested) assertion value."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        out: list[str] = []
        for v in value.values():
            out.extend(_strings(v))
        return out
    if isinstance(value, (list, tuple)):
        out = []
        for v in value:
            out.extend(_strings(v))
        return out
    return []


def _has_marker(text: str) -> bool:
    return any(marker in text for marker in _REDACTED_MARKERS)


def _asserts_against_redacted(assertion: dict, result: dict) -> bool:
    """True when this body/header assertion would be decided
    against a redacted value: the recorded body (or the asserted
    header's recorded value) holds a redaction marker, or the
    assertion's own expected value does."""
    atype = assertion.get("type")
    if atype not in ("body", "header"):
        return False
    if any(_has_marker(s) for s in _strings(assertion)):
        return True
    if atype == "body":
        return _has_marker(str(result.get("http_body") or ""))
    name = str(assertion.get("name", "")).lower()
    value = (result.get("http_headers") or {}).get(name)
    return isinstance(value, str) and _has_marker(value)


def _recorded_reason(result: dict) -> InconclusiveReason:
    """The reason recorded for a not-evaluated result. Every live
    early return records one; ENVIRONMENT is the fallback for a
    record that somehow lacks it."""
    raw = result.get("reason")
    if isinstance(raw, str):
        try:
            return InconclusiveReason(raw)
        except ValueError:
            pass
    return InconclusiveReason.ENVIRONMENT


def replay_probe(probe: dict, result: dict) -> ProbeResult:
    """Re-decide ONE probe from its recorded response."""
    idx = result.get("probe_index", 0)
    assertions = probe.get("assert") or []
    if not result.get("assertions_evaluated", False):
        # The live run returned before step 5 (or the record
        # predates the field): the assertions were never
        # evaluated, so there is nothing to re-decide. Carry the
        # recorded verdict and reason, labeled not re-decided —
        # never PASS, never FAIL.
        reason = _recorded_reason(result)
        return ProbeResult(
            probe_index=idx,
            verdict=Verdict.INCONCLUSIVE,
            reason=reason,
            details=[
                "not re-decided: the live run did not evaluate "
                "this probe's assertions (recorded reason: "
                f"{reason.value})"
            ],
            sent=bool(result.get("sent", True)),
        )
    for assertion in assertions:
        if assertion.get("type") == "db":
            raise NotReplayableError(
                f"probe {idx}: db assertions are not replayable "
                "(DB results are not recorded)"
            )
        if any("${" in s for s in _strings(assertion)):
            raise NotReplayableError(
                f"probe {idx}: an assertion contains a ${{...}} "
                "capture substitution; captures are not recorded, "
                "so the assertion cannot be re-decided"
            )
        if _asserts_against_redacted(assertion, result):
            raise NotReplayableError(
                f"probe {idx}: a body/header assertion targets a "
                "redacted value in the recorded response; it "
                "cannot be re-asserted"
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
    eval_verdict, eval_reason, _details = evaluate_assertions(
        assertions,
        result.get("http_status"),
        result.get("http_headers") or {},
        result.get("http_body") or "",
        {},
    )
    return ProbeResult(
        probe_index=idx, verdict=eval_verdict, reason=eval_reason, sent=True
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
