# Held-Out Set Complete — Milestone Document

**Date:** 2026-10-09
**Status:** Final — all 4 held-out bugs and 4 clean diffs accepted by reviewer
**Judge:** Master's other agent (reviewer)

## The Bugs

| ID | Fix | B | Behavior |
|----|-----|---|----------|
| H1 | `02ee6d44` (2026-09-14) | `701da565a` (2021-01-27) | ParseEnumPipe accepts numeric strings |
| H2 | `1d1dbf7b` (2026-09-14) | `7e3da2253` (2023-07-24) | Factory providers discovered by decorator |
| H3 | `21648746` (2026-09-23) | `bcc3fd38` (2020-12-11) | Transient middleware skips subsequent middleware |
| H4 | `1685a435` (2026-09-30) | `ddf3301d4` (2022-10-28) | +json content types on error replies (Express half) |

## The Clean Diffs

| SHA | Subject |
|-----|---------|
| `a9bf20b` | fix(core): Serialize errorCode for plain HttpException responses |
| `abce26ec` | fix(common): serialize errorCode in built-in exception responses |
| `45485b54` | fix(core): circular durable providers issue #17562 |
| `c921a60e` | fix(fastify): append headers instead of overwriting |

Judged by the reviewer using `git blame --reverse` per-line overlap. See
`held-out-clean-diffs-final-2026-10-09.md` for the full method and rulings.

## Selection Process

- **Rule:** 5 repos (nestjs/nest, koajs/koa, adonisjs/core, pallets/flask, fastapi/fastapi), advisories first then strict `^fix(` commits in SHA order, reviewer judges eligibility in order.
- **Rounds:** 4 H3 candidates proposed, 3 rejected before `21648746`. 2 H2 candidates rejected before `1d1dbf7b`.
- **Rejections:** GHSA-9c5c (B copied existing behavior), `037b0527` (unfocused B), `1c4d9c42` (B partly fixed), `05e9c6bd` (start crash), `1dcbc259` (start crash + lockfile), `1fbe1c53` (B partly fixed).

## Verification Standard

Every B was verified with:
1. **Before-B check:** B^ and B must differ on the bug input. For H1/H2 the feature did not exist before B (trivially different). For H3/H4 the bug input was run through the actual code path at each commit.
2. **Ancestry:** `git merge-base --is-ancestor B fix`.
3. **Tags:** `git tag --contains --sort=creatordate`.
4. **Fix cleanliness:** `--stat` checked for lockfile changes.
5. **Verbatim evidence:** all diffs pasted from git output, never retyped.

## Known Limitations

- **4/4 NestJS bugs.** The mechanical rule produced only NestJS. Not tuned; must be reported alongside dev-set numbers.
- **Old Bs.** H1 (2021), H3 (2020), H4 (2022) span years of API drift to their fixes. Expect INCONCLUSIVE where the harness cannot bridge the drift.
- **Age deviation:** the two extension-window clean diffs (~6.5 weeks old) slightly exceed the 4–6-week bound. Older is safer for "no later fix."

## Scoring Rules Registered

- **Start failure = INCONCLUSIVE** (Master's ruling, 2026-10-09): commit `4d4b71d`.
- **Run pair:** FAIL on fix^, PASS on fix. INCONCLUSIVE on API drift.

## What's Next

1. Merges (Master's authorization)
2. Registration v2 completion (harness version + assembler)
3. Runner (WAL-58) — the only blocker for measurement
4. No-skill baseline re-run
5. Dev-set measurement against X/Y floors
