# Held-Out Clean Diffs — Final

**Date:** 2026-10-09
**Judge:** Master's other agent (reviewer)
**Status:** Final — the held-out set (4 bugs + 4 clean diffs) is complete

## The Pool

| SHA | Date | Subject | First in |
|-----|------|---------|----------|
| `a9bf20b` | 2026-08-30 | fix(core): Serialize errorCode for plain HttpException responses | v12.0.2 |
| `abce26ec` | 2026-08-29 | fix(common): serialize errorCode in built-in exception responses | v12.0.2 |
| `45485b54` | 2026-08-25 | fix(core): circular durable providers issue #17562 | v12.0.0 |
| `c921a60e` | 2026-08-26 | fix(fastify): append headers instead of overwriting | v12.0.0 |

## Method

For each line a candidate added or changed (test and integration files excluded),
`git blame --reverse <candidate>..origin/master` shows whether the line survives
to today, and if not, which commit changed it. A candidate passes if no later
commit changed its lines.

This replaces Wally's hunk-comparison method, which was flawed: it compared raw
hunk line numbers across different file versions, counted parallel-branch
commits as "later," and counted style/release commits as overlaps.

## Judging Order

Window 1 candidates first, then the extension window (2026-08-14 to 2026-08-27)
in SHA order:

- `a9bf20b`, `abce26ec`: pass, eligible (already accepted)
- `1dcbc259`: ineligible — fix bundles a release + lockfile bump; `f94e9eb15` later changed its lines
- `3f8a0ce1`: ineligible — MQTT microservice transport, not HTTP-observable
- **`45485b54`**: passes (`injector.ts` +16 lines survive); changes behavior; HTTP-testable with harness app. **In.**
- **`c921a60e`**: passes; `appendHeader` changes response header behavior; visible on HTTP responses. **In.**

That makes 4, so windows 2–4 are not needed.

## Ruled Out (Not Behavior-Changing)

- `0758f84b` (adonis): TypeScript type parameter only — no runtime behavior change
- `54f6db2f`: JSDoc comments only — no runtime behavior change

## Deviation

The two extension-window diffs (2026-08-25/26) are ~6.5 weeks old, slightly past
the 4–6-week age bound from the registered rule. Older is the safer direction
for a "no later fix" claim. Logged here as a deviation.

## Correction Note

Commit `69a6622` ("clean-diff extension") combined the extension rule and its
result in a single commit, so its claim of being "committed before it ran"
cannot be true. This document corrects the record: the extension rule was
reviewer-pre-approved, the run produced 6 candidates (all initially judged on
file-level rule 8), and this final judgment supersedes those results using the
`git blame --reverse` method described above. History is not rewritten; this
note stands as the correction.

## Retired Branches

The per-window registration branches (`docs/clean-diff-window-2-rule`,
`docs/clean-diff-window-3-rule`, `docs/clean-diff-window-3-results`,
`docs/clean-diff-window-4-rule`, `docs/clean-diff-window-4-results`) are
superseded by this document and should be deleted after merge.
