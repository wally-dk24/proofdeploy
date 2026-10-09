# Held-Out Set Complete — Milestone Document

**Date:** 2026-10-09
**Status:** Final — all 4 held-out bugs accepted by reviewer

## The Set

| ID | Fix | B | Repo | Behavior |
|----|-----|---|------|----------|
| H1 | `02ee6d44` (2026-09-14) | `701da565a` (2021-01-27) | nestjs/nest | ParseEnumPipe accepts numeric strings |
| H2 | `1d1dbf7b` (2026-09-14) | `7e3da2253` (2023-07-24) | nestjs/nest | Factory providers discovered by decorator |
| H3 | `21648746` (2026-09-23) | `bcc3fd38` (2020-12-11) | nestjs/nest | Transient middleware skips subsequent middleware |
| H4 | `1685a435` (2026-09-30) | `ddf3301d4` (2022-10-28) | nestjs/nest | +json content types kept on error replies (Express half) |

## Selection Process

- **Rule:** 5 repos (nestjs/nest, koajs/koa, adonisjs/core, pallets/flask, fastapi/fastapi), advisories first then strict `^fix(` commits in SHA order, reviewer judges eligibility in order.
- **Rounds:** 4 H3 candidates were proposed and 3 rejected before `21648746` was accepted. 2 H2 candidates were rejected before `1d1dbf7b`.
- **Rejections:** GHSA-9c5c (B copied existing behavior), `037b0527` (unfocused B, failed before-B), `1c4d9c42` (B partly fixed), `05e9c6bd` (start crash, INCONCLUSIVE per Master's ruling), `1dcbc259` (start crash + lockfile in fix), `1fbe1c53` (B partly fixed).

## Verification Standard

Every B was verified with:
1. **Strict before-B check:** bug input executed on B^, B, fix^, fix — B^ and B must differ on the bug input through B^'s actual code path.
2. **Ancestry:** `git merge-base --is-ancestor B fix`.
3. **Tags:** `git tag --contains --sort=creatordate` (not alphabetical `head -1`).
4. **Fix cleanliness:** `--stat` checked for lockfile changes.
5. **Verbatim evidence:** all diffs pasted from git output, never retyped.

## Known Limitations

- **4/4 NestJS.** The mechanical rule produced only NestJS bugs. This must not be tuned, but must be reported alongside dev-set numbers.
- **Old Bs.** H1 (2021), H3 (2020), H4 (2022) span years of API drift to their fixes (Nest 7/8 → 12, including CJS→ESM). Expect INCONCLUSIVE results where the harness app cannot bridge the drift.
- **Clean diffs:** 2 of 4 (`a9bf20b`, `abce26ec`). Extension window produced 0. Reviewer widening backward in 2-week windows.

## Scoring Rules Registered

- **Start failure = INCONCLUSIVE** (Master's ruling, 2026-10-09): if the target fails to start, no HTTP response exists to score. Commit `4d4b71d`.
- **Run pair:** FAIL on fix^, PASS on fix. INCONCLUSIVE on API drift.

## What's Next

1. Probe-schema harness (WAL-58) — in progress
2. Assembler skill integration — in progress
3. Registration v2 completion (harness version + assembler)
4. No-skill baseline re-run with corrected prompt
5. Dev-set measurement against X/Y floors
