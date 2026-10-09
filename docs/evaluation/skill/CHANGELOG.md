## Diff vs v1 (item by item)

v1 was voided (drafted from a fabricated dev list). v1→v2 T-number mapping: v1 T1 (Payload replay) dropped; v1 T2 (Payload SQLi, fabricated) dropped; v1 T3 (Fastify header) is now T2; v1 T4 (Fastify URL) is now T3; v1 T5 (Django GIS) is now T4; v1 T6 (LMCache) is now T5; v1 T7 (Express) dropped; new T1 is CodeIgniter4 SQLi; new T6 is Bottle; new T7 is Hono.

- **Item 1:** reworded per reviewer fix 1 — "Every probe must be able to fail if the behavior it checks were wrong."
- **Item 2:** parenthetical "(wrong actor, bad input, repeated action, interacting conditions)" replaced with "(see section 3)" per reviewer.
- **Item 3:** unchanged.
- **Item 4:** unchanged.
- **Item 5:** unchanged.
- **Item 6:** third sentence ("A bug often lives in the interaction between the new path and an old one the diff did not touch.") deleted per reviewer fix 2; first two procedural sentences kept.
- **Item 7:** retagged `[DEV: T2]` → `[DEV: T1]`. v1's T2 was the fabricated Payload SQLi; the verified injection bug is now T1 (CodeIgniter4). Added "escaped" to the list of handling points, matching T1's escape-flag mechanism.
- **Item 8:** unchanged.
- **Item 9:** unchanged.
- **Item 10:** retagged `[DEV: T1]` (v1's T1 Payload replay was dropped; new T1 is the batch operation). Added the batch/hostile-record sentence, drawn from T1's deleteBatch shape.
- **Item 11:** "lower-privileged actor" clause removed per reviewer fix 3. Kept "no authentication at all", retagged `[DEV: T6]` → `[DEV: T5]`: the reviewer's T6 referred to v1 numbering where LMCache was T6; LMCache (unauthenticated /run_script) is now T5.
- **Item 12:** retagged `[DEV: T4]` → `[DEV: T3]` (v1's T4 Fastify URL bypass is now T3). Added `[DEV: T7]`: Hono's empty-segment match is also an empty input reaching the wrong handler instead of an error path.
- **Item 13:** "allow vs deny" removed per reviewer fix 4; kept "(one matches, the other does not)". Retagged `[PROC]` → `[DEV: Bottle]`: T6 Bottle is the interacting-conditions bug (If-None-Match present-but-non-matching vs If-Modified-Since precedence).
- **Items 14+15:** merged into one item per reviewer (overlapping). Kept both provenances: `[DEV: T2]` (v1's T3 Fastify header, now T2) and `[DEV: T4]` (Django GIS, number unchanged).
- **Item 15 (was 16):** limited to T4's shape per reviewer fix 5 — "a different route or handler reached by the same request". Retagged `[PROC]` → `[DEV: T4]`.
- **Item 16 (was 17):** rewritten from new T1 per reviewer fix 6 — order/payment/charge language removed. New example: CodeIgniter4 `f5e463b9` deleteBatch() SQL injection; pattern is "probe every input position in a batch".
- **Item 17 (was 18):** retagged `[DEV: T3]` → `[DEV: T2]`; SHA and text otherwise unchanged.
- **Item 18 (was 19):** retagged `[DEV: T5]` → `[DEV: T4]`; SHA and text otherwise unchanged.
- **Items 19–22 (was 20–23):** renumbered only; text unchanged.
- **Item 23:** NEW — counterweight per reviewer: "If the bundle doesn't establish what the correct behavior is, don't assert it. An unfounded assertion counts against you just as a missed bug does." `[PROC]`

### v2.1 fixes (reviewer audit round 2, 2026-10-09)

- **Item 1:** the two bug-implying sentences ("You are trying to find where it breaks." and "A probe that passes on both the buggy and fixed code tells you nothing.") are now actually deleted. The v2 draft had added the new sentence but left them in; the v2 changelog entry above incorrectly said "reworded".
- **Item 9:** "repeated invocation" removed from the negative-case list. Its source was the dropped Payload replay bug; no train bug supports it. The v2 entry above said "unchanged" — that was wrong.
- **Item 10:** the "performed twice" sentences deleted, same reason. The batch/hostile-record sentence stays `[DEV: T1]`.
- **Item 13:** retagged `[DEV: Bottle]` → `[DEV: T6]` to match dev-set numbering. The v2 entry above still says `[DEV: Bottle]`.
- **Item 15:** retagged `[DEV: T4]` → `[DEV: T3]`. The "different route or handler" shape is T3 (Fastify), not T4 (Django GIS); the v2 entry above still says `[DEV: T4]` due to v1→v2 renumbering confusion.
