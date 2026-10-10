# WO-6 Selection Rule (v5, redrafted 2026-10-10)

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
- had a commit in the last 90 days (active),
- is provisionable by the current runner (SQLite or no external services;
  no PostgreSQL, Redis, or other external services required).

The list is fixed; it does not change during selection. Ordered
deterministically by repository name (lexicographic, case-insensitive):

(No candidates currently meet all criteria. See "Provisioning proofs"
below for the full research record.)

## Provisioning proofs (2026-10-10)

Each candidate below was cloned at HEAD and checked against the
provisioner's hard requirements (frozen lockfile: `uv.lock`,
`poetry.lock`, or frozen `requirements.txt` for Python;
`package-lock.json`/`yarn.lock`/`pnpm-lock.yaml` for Node; SQLite or no
external services; runnable on the current runner's Node 24 / Python
3.12). None reached READY.

### Dropped candidates

1. `django-oscar/django-oscar` (Python, 7k+ stars, active) — DROPPED.
   No frozen lockfile. The repo uses `pyproject.toml` with version
   ranges (`django>=4.2,<6.2`); there is no `uv.lock`, `poetry.lock`,
   or frozen `requirements.txt` anywhere in the repo. The provisioner
   bails with BUILD_FAILED ("no frozen lockfile found"). The existing
   fixture (`docs/wo6-fixtures/django-oscar/proofdeploy.yml`) was
   written before the lockfile requirement was understood.

2. `keystonejs/keystone` (Node, 8k+ stars, active) — DROPPED. pnpm
   monorepo (`pnpm-lock.yaml` + `pnpm-workspace.yaml` at root; no
   standalone app with its own lockfile). The examples under
   `examples/` have no `package-lock.json` and depend on the workspace.
   Not provisionable by the runner's simple snapshot model.

### New candidates researched (none met all criteria)

- `louislam/uptime-kuma` (Node, 92k stars, active, SQLite default,
  `package-lock.json`) — DROPPED. Requires Node >= 26.2.0
  (`engines` in package.json); the runner has Node 24 only and the
  provisioner's `_NODE_BINARIES` list tops out at `node24`.
- `pretix/pretix` (Python, 2.5k stars, active, SQLite for dev) —
  DROPPED. Uses `pyproject.toml`; no frozen lockfile. Also documents
  Redis as required for production (runs without it only for dev).
- `nocodb/nocodb` (Node/TypeScript, 65k stars, active, SQLite default)
  — DROPPED. Complex monorepo; not a simple provisionable app.
- `simonw/datasette` (Python, 11k stars, active, SQLite-native) —
  DROPPED. Uses `pyproject.toml`; no frozen lockfile.
- `apostrophecms/apostrophe` (Node, 4.6k stars, active, SQLite
  supported) — DROPPED. The repo is the CMS framework, not a runnable
  app; the runnable demo (`apostrophecms/public-demo`) has 5 stars
  (< 1000).
- `wagtail/wagtail` (Python, 20k stars, active, SQLite for dev) —
  DROPPED. Framework/library, not a standalone runnable web app.

### Finding

No real open-source web app with >= 1000 stars, a frozen lockfile, and
SQLite-only provisioning was found that the current runner can
provision. Popular Python apps use `pyproject.toml` (not frozen);
popular Node apps are monorepos, need newer Node, or need external
services. The candidate list is therefore EMPTY. WO-6 selection cannot
proceed until either (a) the runner gains support for `pyproject.toml`
-based installs or newer Node, or (b) the criteria are revised. This
is reported honestly per the no-rushed-verdicts rule; no candidate was
forced onto the list.

## Generic fixtures (committed before selection)

For each candidate, `docs/wo6-fixtures/<name>/proofdeploy.yml` is committed
on this branch before the selection runs. Each fixture:

- uses the five registered template fields: `repo_name`, `seed_note`,
  `auth_mechanism`, `auth_scope`, `flags_note` (not free-form `description`);
- includes `readiness` (required by the loader; a `/path` joined to the
  assigned target URL);
- has NO `npm install` or `pip install` in `build` (the provisioner
  installs from the lockfile; `build`, `migrate`, `seed` are app commands
  only);
- uses the assigned `PORT` env var, not a hard-coded port;
- points at the real runnable app inside the repo (e.g. django-oscar's
  `sandbox/` project, not the repo root).

Each fixture is validated with the real loader (`load_contract`).

## Fixture proof (before sign-off)

Each candidate is provisioned **at its current HEAD** (never at any bug
commit) through the real provisioner. The provisioning log and the READY
status are recorded on this branch. Only candidates that reach READY stay
on the list. This looks at no bugs, so the blindness holds.

## Bug selection (mechanical)

For each candidate in order:

1. Find the default branch.
2. Take the first commit on the default branch with committer date
   strictly after 2026-07-01 (after the model's June 2024 cutoff, and
   recent enough that a fixture written for 2026 will build; this date
   is fixed) whose subject line matches `(?i)\bfix\b`.

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
   strictly after 2026-07-01 (after the model's June 2024 cutoff, and
   recent enough that a fixture written for 2026 will build; this date
   is fixed) whose subject line matches `(?i)\bfix\b`.
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
  "cutoff": "2026-07-01",
  "fallbacks": [
    {"candidate": "...", "fix": "...", "reason": "..."}
  ]
}
```
