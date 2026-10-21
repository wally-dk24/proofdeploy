# R-LEAN-SEL — Lean selection: the bug's change must reach the backend

Signed by Master, 2026-10-20 (D267, plan 0.4). Master, verbatim: "R lean sel is signed".

*Amends, for lean units only, REG-C item 7 (with Addendum 2), D169's path classes, R-SEL and R-SIZE. Rule only. Measured-path selections (REG-C, the held-out walk, the checkpoint) are unchanged.*

1. **Scope.** Selects the real-app bug unit of the lean proof of concept (Stage 2 Phase 0, gate round 2) and every later lean selection, until a registration replaces it.
2. **Walk.** As REG-C item 7 with D169 and R-SEL: the default branch's first-parent commits with committer date strictly after 2026-07-01T00:00:00Z, oldest first. Check (a): the subject matches `(?i)\bfix(e[sd])?\b`. Check (b): at least one changed SOURCE path that is not a migration path. Check (c): B is the most recent commit that last touched a line the fix removes or changes (blame at fix^), and B is contained in a release tag created before the fix.
3. **R-SIZE does not apply.** Its check (e) measured the old author prompt, which the lean path does not send.
4. **New check (L), after (c) and before (d), from changed-path lists only (no diff, test or message is opened).** A *backend source file* is a changed path of class SOURCE (D169) that is not a migration path (R-SEL 1) and whose extension belongs to the app's language: Python → `.py`. Other languages are not defined yet; a lean walk on such an app refuses until a registration defines them.
   - **(L1)** B's own change (B^..B, the change the lean gate tests) changes at least one backend source file.
   - **(L2)** The fix (fix^..fix) changes at least one backend source file.
   - A failure is recorded as `L1: no backend source in B` or `L2: no backend source in fix`.
5. **Check (d), extended.** B^ and B (the unit's two sides) and fix^ and the fix (the confirm sides) all reach READY, through the same GitHub-runner pair check, the B pair first.
6. **Stop.** If 200 commits are walked without a selection, no unit is selected; the walk stops and the reviewer reports to Master (no fallback app).
7. **Blindness and record.** As before: journals are read only through field-selecting scripts; only metadata (IDs, dates, check letters, counts) reaches reviews; the selection record is committed before the predictions; the gate reads the unit from `experiments/lean-poc/paperless-bug-unit.json`, written from that record.
8. **Clean units.** Unchanged: the D262 rule and its 3 units.
9. **Round 1.** The round-1 unit (fix `d3710856`, B `aa67fd3a`) stays published as round-1 evidence and is not re-run.
