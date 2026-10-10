# WO-6 Selection Rule (v4, redrafted 2026-10-10)

> This rule is mechanical. No human picks the app, the bug, or the base
> commit. The script and its output are committed. WO-6 is an acceptance
> test of the pipeline; the outcome is reported whatever it is.
>
> Ghost's go/no-go is separate: at WO-6, attempt Ghost per the registered
> rules (build, start, seed, auth via the `ghost_jwt` provider). It is not
> a selection criterion.

## Exclusion list

The following repositories are EXCLUDED because this project has already
examined bugs in them. A bug picked from any of these would not be blind:
Ghost, Directus, Vendure, NestJS, koa, adonisjs, Flask, FastAPI,
CodeIgniter4, Fastify, Django, LMCache, Bottle, Hono, body-parser,
Express, n8n, Strapi, Payload.

## Candidates

A fixed list of real open-source web applications, committed with this
rule. None are from the eval set, the held-out set, the dev set, or the
exclusion list above. Each candidate:

- is a web application with a database and a login,
- is Python or Node,
- has a lockfile (requirements.txt, package-lock.json, yarn.lock, etc.),
- has >= 1000 GitHub stars,
- had a commit in the last 90 days (active).

The list is fixed; it does not change during selection. Ordered
deterministically by repository name (lexicographic, case-insensitive):

1. `django-oscar/django-oscar` (Python, e-commerce)
2. `keystonejs/keystone` (Node, CMS)
3. `medusajs/medusa` (Node, e-commerce)
4. `saleor/saleor` (Python, e-commerce)

## Generic fixtures first (rule 2)

Before the selection script runs, each candidate's `proofdeploy.yml`
(contract: build, start, seed commands) and fixture description are
written and committed. These are generic — they describe how to run the
app, not any specific bug. They are written without reference to any bug
commit.

## Bug selection (mechanical)

For each candidate in order:

1. Find the default branch.
2. Take the first commit on the default branch with committer date
   strictly after 2024-07-01 (after the model's June 2024 cutoff; this
   date is fixed) whose subject line matches `(?i)\bfix\b`.
3. Exclude the commit if:
   - it is a merge commit (more than one parent),
   - it is a revert commit (subject matches `(?i)^revert\b`),
   - it touches only dependencies (`package.json`, `requirements.txt`,
     lockfiles, `*.lock`),
   - it touches only docs (`*.md`, `docs/`),
   - it touches only CI (`.github/`, `.gitlab-ci.yml`, etc.),
   - it touches only tests (`test/`, `tests/`, `*_test.py`, `*.test.js`, etc.),
   - it does not touch non-test source code.
4. The first commit passing these filters is the FIX commit.

## Base commit B (mechanical, SZZ)

1. If the FIX commit only adds lines (no deleted or modified lines),
   SZZ is undefined: skip this commit and take the next fix commit in
   order (go back to Bug selection step 2, continuing after this commit).
2. At FIX^, run `git blame` on each line the FIX commit deletes or
   modifies.
3. Take the most recent commit among the blamed lines. That is B, the
   bug-introducing commit.
4. If B is a root commit (no parent) or a merge commit, skip this FIX
   and take the next fix commit in order.
5. No human judgment. If blame is ambiguous (multiple commits tie for
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
  "fix": "...",
  "fix_parent": "...",
  "b": "...",
  "b_parent": "...",
  "cutoff": "2024-07-01",
  "fallbacks": [
    {"candidate": "...", "fix": "...", "reason": "..."}
  ]
}
```
