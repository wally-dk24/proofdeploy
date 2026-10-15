# Registration: Held-out Addendum 2 — existing held-out units meet the same runnable rule

Registered: 2026-10-15. Amends Held-out Addendum 1 (H5). Items not named here are unchanged.

**Why.** The eight existing held-out units (H1–H4 and four clean diffs, all nestjs/nest) were selected before the Stage 1 readiness path existed. On 2026-10-15 the reviewer found that at H1 (both sides) and at all four clean diffs, the committed `package-lock.json` does not satisfy `package.json`, so no frozen install can succeed. Addendum 1 H5 states how a not-supported *candidate* is handled, but not a unit already in the set. No probe has been authored for, and no measurement made on, any held-out unit.

**J1. Same rule for existing units.** Every existing held-out unit is judged by H5, exactly as a candidate: each side is provisioned through the sandboxed readiness path on the main the walk uses, with no model, no probes and no scoring. A unit with any side that does not reach READY for a typed reason in a not-supported class is removed from the set. A harness or environment failure is retried, not removed.

**J2. The class this addendum names.** "Not supported: lockfile out of sync with its manifest" is a not-supported class: the package manager's own frozen install (or its own lock-freshness check) refuses because the committed lockfile does not match the manifest. The manager's refusal is recorded verbatim. The repository's own install is never repaired, regenerated or resolved by date (rule 9; REG-D D1(c)).

**J3. Replacements by the walk only.** Removed units are replaced only by continuing the walk of Addendum 1 H3 and H4, in its registered order. No unit is chosen by hand. A removed unit is never re-admitted.

**J4. Target and fallback unchanged.** The target stays 10 bugs and 10 clean diffs (H2). If the walk is exhausted, H7 applies.

**J5. Order of work.** The judgment of the existing units is part of the completed list committed before any probe and before any measurement (H6).

**J6. Reporting.** Each removed unit is published with its typed reason alongside the results (H5, H8).
