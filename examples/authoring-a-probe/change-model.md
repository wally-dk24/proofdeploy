# Change model for `sample.diff`

One hunk, one file. Following the skill's decision tree:

## Hunks

| Hunk | Category | Rationale |
|------|----------|-----------|
| `discount_rate`: `>=` → `>` | **behavior-change** | The boundary condition moved. Orders of exactly 10 items now get 0% instead of 15%. |

No other hunks. No migration, no config, no schema change.

## Must-not-change

- The discount rates themselves (0.15 / 0.0) — untouched by the diff, and the
  probe for `quantity: 9` guards them.
- The function signature and return type.

## What the probes must show

The intended behavior (from the docstring: "15% at 10+ items"):

1. `quantity = 10` → `0.15`. Against the changed code this returns `0.0`,
   so the probe **fails** — the break is caught.
2. `quantity = 9` → `0.0`. Unchanged behavior; guards against over-correction
   (e.g. someone "fixing" it to `>= 9`).

Two probes, both HTTP (the service exposes `POST /api/orders/quote`), both
with expected results written from the docstring — not from the code.
See `probes.json`.
