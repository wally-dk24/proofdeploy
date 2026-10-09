# ProofDeploy: Formal Control Registration

**Date:** 2026-10-09
**Status:** REGISTERED 2026-10-09 — all 5 controls built at their own commits on Node 22 (stage table: 9/9 GO). Registration is dated and hashed. No probes have been run against any control.
**Registration rule:** Formal registration waits until every control builds at its own commit. That condition is now met (see stage table below).

---

## Registered controls (in evaluation order)

| # | Repo | Commit (full SHA) | Date | Behavior (one line) |
|---|---|---|---|---|
| C1 | vendure-ecommerce/vendure | `4cbbc790118786a17721954c3a4d739810bd2632` | 2026-09-05 | `OrderCalculator` removes promotions that apply no discount from `order.promotions` |
| C2 | vendure-ecommerce/vendure | `b19ab4dc76e4f8521e02258b8afa26f615142595` | 2026-09-05 | Empty search `sort: {}` falls back to relevance ordering instead of no ordering |
| C3 | directus/directus | `dc922b790a15949e8abfce9c1f4523d4e5f89492` | (in window) | (see controls-2026-10-09-round2.md for behavior + later-touch evidence) |
| C4 | directus/directus | `a5da59da94bb33c8d644df93a21403eca4c1a4e7` | (in window) | (see controls-2026-10-09-round2.md for behavior + later-touch evidence) |
| C5 | TryGhost/Ghost | `5d482965b7f5ed05ffef956d0eaed8b8a51af0ef` | (in window) | (see controls-2026-10-09-round2.md for behavior + later-touch evidence) |

Full behavior descriptions, later-touch evidence, and probe notes: `controls-2026-10-09-round2.md`.

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
| `5d482965b7` (C5) | PASS | PARTIAL | PASS | PASS | NOT REACHED; JWT 5m | ~25m | 1.8G | GO |

\*Vendure seed: genuine repo defect (`populate-dev-server.ts` references nonexistent `../create/assets/products.csv`); symlink workaround → populate exit 0. Does not touch eval targets.

Full stage table (4 bugs + 5 controls): `eval-build-check-report-2026-10-09-stage2.md`.

## Frozen fixture-description hash

`0994d1e367ddb3c679e750451dd67eab3a3d974fc1620b7f750216cdcdf0323b`

---

## Registration hash

Computed over this file's canonical content (controls, backups, rules, build verdicts, fixture hash):

`3445b3ce22538bde7523a7d8d67eb08bc84819a0a1f04bfc36ef306e86138211`
