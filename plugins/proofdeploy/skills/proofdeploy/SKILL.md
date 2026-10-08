---
name: proofdeploy
description: Author change-aware probes that verify behavior-changing code edits. Use after making backend code edits and before considering the work done, or when asked to verify a diff. Guides authoring a change model and probes from the diff; execution by a deterministic runner is planned but not built yet.
license: MIT
---

# ProofDeploy

> **Experimental — authoring only.** This skill authors probes from diffs.
> The deterministic runner is not built yet, so nothing here executes
> anything. Emit `probes.json` and stop. Do not present this skill as
> end-to-end verification until the runner lands.

ProofDeploy catches the breaks your test suite misses. The loop is
**diff → probes → verdicts**: from a code diff, author small targeted probes
(HTTP requests, read-only DB queries) with expected results, run them against
the changed code, and compare. Any FAIL or INCONCLUSIVE fails verification.

You are the author. A deterministic runner executes and judges. Never blur
that line: expected results come from the *intended* behavior, never from
running the code and copying what it does.

## When to use

- After behavior-changing backend edits, before calling the work done.
- When asked to verify a diff, a PR, or a commit range.
- After fixing a bug, to prove the fix holds (and nothing else broke).

## When NOT to use

- Pure refactors with no behavior surface (renames, formatting): say so and stop.
- Docs, comments, or config-only changes: report as informational, do not probe.
- Frontend-only changes: out of scope for v1 (HTTP + DB probes only).

## The loop

### 1. Build the change model

Read the diff. Categorize every hunk:

- **behavior**: changes what the code does (logic, queries, status codes, payloads)
- **refactor**: restructures without changing behavior
- **config**: env, flags, wiring
- **test**: test files themselves
- **docs**: comments, docs

Then name the **behavior surfaces**: the HTTP endpoints and DB reads whose
observable behavior could have changed. Only behavior hunks produce probes.
If there are no behavior surfaces, stop and say so — a probe that cannot fail
proves nothing.

### 2. Author probes

For each behavior surface, write 1–3 probes. Two types exist in v1:

- **http**: method, path, headers, body; assert status code and response shape.
- **db**: a read-only SELECT; assert the rows returned.

Rules (non-negotiable):

- DB probes are read-only. No INSERT/UPDATE/DELETE/DDL, ever.
- Targets are loopback only (`localhost`/`127.0.0.1`) unless the run explicitly
  allows otherwise. Never probe production.
- No secrets, tokens, or credentials inside probes.
- Probes must be deterministic: no wall-clock assertions, no random data,
  no ordering assumptions unless ORDER BY is present.
- Every probe targets behavior the diff actually touched. Do not write
  generic health checks — the health check already exists and already passes.

### 3. Write expected results FIRST

For each probe, state the expected result **from the intended behavior** —
the spec, the ticket, the PR description — before anything runs. This is the
load-bearing discipline. An expected result copied from observed output is
not verification; it is transcription.

### 4. Execute (runner — not built yet)

The deterministic runner is under construction. Today, emit the authored
probes as `probes.json` conforming to `references/probe-schema.md` (schema
v0, draft) and stop there. The intended execution interface will be:

```bash
proofdeploy verify --probes probes.json
```

That flag does not exist yet — do not run it, do not tell the user to run
it, and do not invent another execution path. When the runner lands, it
provisions a disposable instance and database, runs every probe, and compares
against the expected results byte-for-byte where it matters.

### 5. Read verdicts and fix the code, not the probes

- **PASS**: the surface behaves as intended.
- **FAIL**: the behavior changed in a way the expected result did not allow.
  Fix the code. Never "fix" a probe to match broken code.
- **INCONCLUSIVE**: the probe could not run or the result was ambiguous.
  Treat as FAIL: fix the probe or the environment, then re-run.

## Decision tree

```
diff in hand
 ├─ No behavior hunks? → Stop. Report "no behavior surfaces changed."
 ├─ Behavior in HTTP layer? → http probes: the touched endpoints,
 │   including the unhappy paths the diff altered (4xx/5xx, empty results)
 ├─ Behavior in DB reads? → db probes: the exact queries whose results
 │   the diff could change, with seed data the fixtures provide
 └─ Behavior in both? → Probe the seam: HTTP request, then DB query
    asserting the state the request should have produced
```

## Worked example (fictional)

Change: a patch to a fictional parcel-tracking service alters how released
parcels appear in the pickup queue. The diff touches the release handler and
the queue query.

Change model: 2 behavior hunks (handler logic, queue query); 1 refactor hunk
(variable rename — no probe).

Probes:

1. **http** `POST /api/parcels/{id}/release` →
   expect 200 and a parcel object with `status: "released"`.
2. **http** `GET /api/queue/pickup` after releasing →
   expect the queue list to contain the parcel with an assigned bay
   (not a null bay).
3. **db** `SELECT * FROM parcels WHERE id = ?` → expect `status = 'released'`.

The bug this pattern catches: the handler marked the parcel released but the
queue query returned a null bay — health check green, test suite green,
behavior broken. Probe 2 fails. That is the whole product.

## Common pitfalls

- ❌ Probing the fix instead of the behavior: author from the diff's *intent*,
  and where possible from the bug report, not from the patched code.
- ❌ Asserting on volatile fields (timestamps, generated IDs): assert on the
  fields the change governs.
- ❌ One giant probe per endpoint: small probes, one behavior each. A failing
  probe should name the break.
- ❌ Writing probes after running the code: expected results first, always.
- ❌ Treating INCONCLUSIVE as a pass: it is a fail until proven otherwise.

## Best practices

- Prefer the unhappy path: the bug usually lives in the error branch the
  author didn't re-check.
- Keep probes independent: no probe depends on another probe's side effects.
  Order them setup → act → assert explicitly instead.
- Name probes after the behavior: `parcel-release-assigns-bay`,
  not `probe-1`.
- When the diff is large, probe the riskiest surfaces first: data-loss,
  auth, and money-adjacent paths before cosmetic ones.

## Reference files

- **references/probe-schema.md** — the probes.json schema (v0 draft): probe
  types, fields, and expected-result encoding. This schema will become the
  `--probes` file contract once the runner (and its `--probes` flag) exists.
- **docs/adr/0004-injectable-probe-authoring.md** (repo root) — why authoring is injectable
  and the runner stays deterministic.
