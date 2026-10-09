# ProofDeploy: Formal Control Registration v2

**Date:** 2026-10-09
**Status:** REGISTERED 2026-10-09 — v2 supersedes v1 (see supersession log below). 4 GO; C5 conditional on Ghost's Phase 2 gate, withdrawn per the rules if not met. No probes have been run against any control.
**Registration rule:** Formal registration waits until every control builds at its own commit. That condition is met for 4 of 5; C5 is conditional (see status).

---

## Supersession log

- **v1** (hash `3445b3ce22538bde7523a7d8d67eb08bc84819a0a1f04bfc36ef306e86138211`): registered 2026-10-09, then edited after registration. Superseded before any probe ran. Reason: "corrected before any run" — the reviewer found the file had been rewritten (7/9 correction, seed footnote fix, and addendum folded in after the hash). Nothing was compromised; no probes had run.
- **v2** (this file): the corrected registration. Self-contained, append-only from here.

---

## Registered controls (in evaluation order)

| # | Repo | Commit (full SHA) | Date | Behavior (one line) |
|---|---|---|---|---|
| C1 | vendure-ecommerce/vendure | `4cbbc790118786a17721954c3a4d739810bd2632` | 2026-09-05 | `OrderCalculator` removes promotions that apply no discount from `order.promotions` |
| C2 | vendure-ecommerce/vendure | `b19ab4dc76e4f8521e02258b8afa26f615142595` | 2026-09-05 | Empty search `sort: {}` falls back to relevance ordering instead of no ordering |
| C3 | directus/directus | `dc922b790a15949e8abfce9c1f4523d4e5f89492` | 2026-09-11 | `CommentsService` bulk updateMany/deleteMany now requires an authenticated user |
| C4 | directus/directus | `a5da59da94bb33c8d644df93a21403eca4c1a4e7` | 2026-09-08 | `FilesService.uploadOne` no longer lets client payloads overwrite system fields |
| C5 | TryGhost/Ghost | `5d482965b7f5ed05ffef956d0eaed8b8a51af0ef` | 2026-09-10 | Settings import/export no longer overwrites protected core settings |

## Backups (named order, unneeded)

1. Ghost `37e161932221fe12c1c626a7238c3669c22aec2a`
2. Directus `ea25ba63db11d4e25c0b1a4e5b8926a64dadbfee`

No control failed to build. Backups stay in reserve.

## Withdrawn (never eligible)

- Directus `962ce0bb1` (round 1) — withdrawn and logged: dated 2026-09-24, outside the Aug 28 – Sep 11 window. No probes were run against it.

## Failure and scoring rules (frozen 2026-10-09)

- Ghost drop means C5 is withdrawn and logged, not replaced.
- An unbuildable control yields to its named backup in order.
- Vendure has no backup; one surviving Vendure control preserves repo coverage.
- Author receives C^→C; probes run against C.
- Any FAIL or INCONCLUSIVE is a false alarm.
- Migration coverage: none.

## Build evidence (stage table 2026-10-09, Node 22)

| Commit | install | build | start | seed | auth (+lifetime) | wall | disk | verdict |
|---|---|---|---|---|---|---|---|---|
| `4cbbc7901` (C1) | PASS | PASS | PASS | PASS* | PASS, 1y | ~21m | 2.4G | GO |
| `b19ab4dc7` (C2) | PASS | PASS | PASS | PASS* | PASS, 1y | ~11m | 2.4G | GO |
| `dc922b790` (C3) | PASS | PASS | PASS | PASS | PASS, static | ~45–50m | 1.5G | GO |
| `a5da59da9` (C4) | PASS | PASS | PASS | PASS | PASS, static | ~45–50m | 1.4G | GO |
| `5d482965b7` (C5) | PASS | PARTIAL | PASS | PASS | NOT REACHED; JWT 5m | ~25m | 1.8G | CONDITIONAL (Ghost Phase 2 gate) |

\*Vendure seed: setup problem, not a repo defect (`populate-dev-server.ts` reads `../create/assets/products.csv`, created by `packages/create`'s own `copy-assets` build step; the stage-2 build skipped it). Corrected procedure confirmed at `44bad119b` (exit 0, 54/88/1); CSV byte-identical at C1/C2. The populate step itself will be re-verified at the runner's first provisioning in the Phase 2 acceptance test; if it fails there, the row is not GO.

Full stage table (4 bugs + 5 controls): `eval-build-check-report-2026-10-09-stage2.md`.

## Frozen template hash

`0994d1e367ddb3c679e750451dd67eab3a3d974fc1620b7f750216cdcdf0323b`

---

## Addendum 2026-10-09: approved per-repo description hashes

The reviewer approved the Vendure and Directus fixture descriptions. Their
rendered hashes are recorded here as a dated addendum. The Ghost description
remains held.

- Vendure: `6e739fca6cc4e975989a5d9ff15e3750c83a1d5365250369479b9d1cb1522954`
  (approved; condition: each commit uses its own `dev-config.ts`, unmodified)
- Directus: `55aa188ba1ab053a970d3438db505d70fc9ff7303d977e4271c1062e3fef9026`
  (approved after wording fix to "static Bearer token in the Authorization header")

---

## Addendum 2026-10-09: probe-authoring model pin

**Model:** `openai/gpt-oss-120b` (Groq API)
**Vendor-stated training cutoff:** June 2024 (OpenAI gpt-oss model card)
**Pin:** exact Groq model ID `openai/gpt-oss-120b`; no tools, no web access
**Decision:** Master, 2026-10-09. The model is a user preference in the
framework; this is the registered default for the Bottle blind dry run and
the checkpoint. Exactly one model per run (rule 5).

## Addendum 2026-10-09: wording fix

Under Backups: "No control failed to build" is corrected to "No control was
replaced" (C5's build was PARTIAL; it was not replaced).

---

## Addendum 2026-10-09: dev-set registration

**Dev list:** `docs/evaluation/dev-set-2026-10-09.md` (10 bugs: 6 TRAIN + 4 HELD-OUT; 10 clean diffs, split equally). Pre-registered before skill drafting. TRAIN half may be read for drafting; HELD-OUT half is measurement-only.

**Minimum scores (Master's decision, 2026-10-09):**
- False alarms (INCONCLUSIVE counts as one): at most **1 in 10** clean diffs.
- Catches: at least **3 in 10** bugs, strictly above the no-skill baseline.

These floors are fixed before any dev-set measurement. The checkpoint bar is unchanged.
