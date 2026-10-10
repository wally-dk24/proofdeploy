# Scoring Rule Registration — Run-Pair Verdicts

**Date:** 2026-10-09
**Approved by:** Master
**Recommended by:** Reviewer (brief 2026-10-10, Part 1a)
**Status:** Registered — applies to all measured runs from this date

## Bugs: Per-Probe Catches

A bug counts as caught if **at least one probe** meets all three conditions:

1. It FAILs on fix^;
2. It PASSes on fix;
3. It targets behavior the fix actually changed (reviewer judges from the probe against the fix diff).

A sibling probe that is INCONCLUSIVE or FAILs on both sides does **not** cancel the catch. Every probe is reported.

**Rationale:** One flaky query or one probe hit by API drift should not erase a real catch. The bar defines a catch as a FAIL on behavior the fix changed — a property of one probe, not of the whole set.

## Whole-Side Failure: Pair INCONCLUSIVE

If fix^ or fix cannot build or start, or the target is unreachable for every probe, the pair is INCONCLUSIVE. There is nothing to compare. (Already registered: `4d4b71d`.)

## Clean Diffs: Strict

Probes run against C only. Any FAIL or INCONCLUSIVE is a false alarm.

## Both Arms

The same rule applies to the skill arm and the no-skill baseline arm.

## Probe Cap

No cap is registered at this time. Master may add one later.

## Product Verdicts (Separate)

Measurement scoring above is distinct from the product gate. For the product:
- Every probe is reported on its own; one INCONCLUSIVE never hides a FAIL.
- Every INCONCLUSIVE carries a typed reason: `environment`, `auth`, `drift`, or `probe`.
- Default gate is fail-closed: any FAIL or INCONCLUSIVE blocks.
