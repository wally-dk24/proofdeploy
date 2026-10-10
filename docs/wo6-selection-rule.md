# WO-6 Selection Rule (v2, redrafted 2026-10-10)

> This rule is mechanical. No human picks the app, the bug, or the base
> commit. The script and its output are committed. WO-6 is an acceptance
> test of the pipeline; the outcome is reported whatever it is.
>
> Ghost's go/no-go is separate: at WO-6, attempt Ghost per the registered
> rules (build, start, seed, auth via the `ghost_jwt` provider). It is not
> a selection criterion.

## Candidates

A fixed list of real open-source web applications. None are from the
eval set, the held-out set, or the dev set. Each candidate:

- is a web application with a database and a login,
- is Python or Node,
- has a lockfile (requirements.txt, package-lock.json, yarn.lock, etc.),
- has >= 1000 GitHub stars,
- had a commit in the last 90 days (active).

The list is committed with this rule, ordered deterministically by
repository name (lexicographic, case-insensitive). The list is fixed at
commit time; it does not change during selection.

Proposed initial list (to be finalized at commit time by the script
querying the GitHub API against the criteria above):

1. (to be populated by script; e.g. `django-oscar/django-oscar`,
   `vendure-ecommerce/vendure` is EXCLUDED as eval,
   etc.)

## Bug selection (mechanical)

For each candidate in order:

1. Find the default branch.
2. Take the first commit on the default branch with committer date
   strictly after 2024-07-01 (after the model's June 2024 cutoff; this
   date is fixed) whose subject line matches `(?i)\bfix\b`.
3. Exclude the commit if:
   - it touches only dependencies (`package.json`, `requirements.txt`,
     lockfiles, `*.lock`),
   - it touches only docs (`*.md`, `docs/`),
   - it touches only CI (`.github/`, `.gitlab-ci.yml`, etc.),
   - it touches only tests (`test/`, `tests/`, `*_test.py`, `*.test.js`, etc.),
   - it does not touch non-test source code.
4. The first commit passing these filters is the FIX commit.

## Base commit B (mechanical, SZZ)

1. At FIX^, run `git blame` on each line the FIX commit deletes or
   modifies.
2. Take the most recent commit among the blamed lines. That is B, the
   bug-introducing commit.
3. No human judgment. If blame is ambiguous (multiple commits tie for
   most recent), take the lexicographically smallest SHA.

## Fallback (pre-registered)

If provisioning fails at FIX^ or at FIX (the app does not build, start,
or seed per the contract), record the failure (candidate, commit SHAs,
log excerpt) in the WO-6 output, and move to the next candidate in
order. Do not retry, do not pick a different bug in the same candidate.

## Blindness

Nobody looks at the candidate list, the bug commits, or the diffs before
the WO-6 run. The selection script is run once; its output (selected
candidate, FIX SHA, B SHA, fallback records if any) is committed. The
WO-6 measured run uses exactly that output.

## Output

The script writes `wo6-selection.json`:
```json
{
  "candidates": ["owner/repo", ...],
  "selected": "owner/repo",
  "fix_sha": "...",
  "base_sha": "...",
  "cutoff": "2024-07-01",
  "fallbacks": [
    {"candidate": "...", "fix_sha": "...", "reason": "..."}
  ]
}
```
