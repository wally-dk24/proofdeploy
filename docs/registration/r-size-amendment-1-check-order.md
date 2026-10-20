# R-SIZE Amendment 1 — check order (SIGNED; D255)

Amends R-SIZE item 1 (docs/registration/r-size-author-prompt-size.md, signed 2026-10-19, D253), order only.

1. Check (e) runs after checks (a)–(c) and **before** check (d). A commit that fails (e) is rejected at (e) and check (d) (the readiness run) is not performed for it. A commit that passes (e) then goes to (d).
2. Nothing else changes: the budget (80,000 characters), the way the prompt is built and measured, the reason string, the recording of the size, the walk order and the rule that the first commit passing every check is selected. Because a commit must pass every check either way, the selected commit is the same as under the original order; only the recorded failing check of a commit that fails both (d) and (e) can differ.
3. Reason: under the original order every oversized candidate cost one readiness run (about 7.5 minutes) before being rejected; the 2026-10-19 walk spent about 80 minutes on 10 oversized candidates without a selection.

Signed by Master, 2026-10-20: "Signed"
