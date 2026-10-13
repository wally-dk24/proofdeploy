# REG-A Addendum 1: build isolation for the root project

Signed by Master, 2026-10-13. Amends nothing in REG-A (`docs/registration/python-lock-rule.md`); it adds one rule. Rule only, no results.

1. Build isolation for the root project uses the unit's cutoff where the tool supports it (uv). Where a tool cannot (pdm), or builds the root with its own hash-pinned backend (poetry), the plan records a typed limitation instead. Committed locks are never regenerated.
2. The cutoff is the unit's own, as in REG-A item 2: the fix commit's committer time for a bug pair (the same value for fix^ and fix), and C's committer time for a clean diff. It is one value per unit, never per side.
3. The evidence records, per side, the build backend's name and the version the tool actually used, and the cutoff applied or the typed limitation.
4. Applies to every Python unit that installs through uv, poetry or pdm, from this addendum's merge onward. Units measured before the merge are not re-scored.
