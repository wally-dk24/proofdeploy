# Conformance kit (WO-7 PR 2)

The kit proves the provisioning adapters behave identically across
executors and ecosystems, using three **golden apps** and a **canned
author** (fixed probes — no model, so verdicts can only move when
the code under test moves).

## Golden apps

Each app is the same tiny notes service — register/login returning a
token, an authenticated note endpoint, migrations, a seed — in a
different stack:

| app | stack | lockfile | runtime declaration |
|---|---|---|---|
| `golden/node-express-sqlite` | Express + better-sqlite3 | `package-lock.json` → `npm ci` | `engines.node: >=20 <25` → node 24.21.0 |
| `golden/node-pnpm-sqlite` | Express + better-sqlite3 | `pnpm-lock.yaml` → corepack `pnpm@9.15.0 install --frozen-lockfile` | `packageManager: pnpm@9.15.0`; `engines.node: >=20 <25` |
| `golden/python-flask-sqlite` | Flask + stdlib sqlite3 | fully hashed `requirements.txt` | `requires-python: >=3.13,<3.14` → python 3.13.16 |

`golden/node-pnpm-sqlite/variant-node26/` holds a two-file overlay
(package.json + regenerated pnpm-lock.yaml) used by
`tests/test_node26_corepack.py`: engines widened to the Node 26
row and better-sqlite3 bumped to ^13.0.3 (11.10.0's C++ predates
Node 26's V8), proving a packageManager-pinned app provisions on
the 26 row — where corepack is not bundled and the adapter
bootstraps it explicitly.

Each app ships as revision **trees** under `golden/<app>/revisions/`
(`base`, `bug`, `intermediate`, `fix-parent`, `fix`, `clean`,
`lockmismatch`). `kit.materialize_golden()` rebuilds the app's own
git history from the trees in that order:

- `bug` plants a NON-boundary bug (the note's `done` flag is
  inverted when read back);
- `fix` removes it; the measured pair is `fix-parent` vs `fix`;
- `clean` is a behavior-changing commit that must not trip the
  probes (adds a `tag` field to note responses);
- `lockmismatch` changes a dependency without updating the lockfile
  (express `^5.0.0` against a 4.x lockfile; flask `3.0.3` against
  3.1.0's hashes) and must be refused by the frozen install.

## Canaries

Every token the golden apps issue starts with `pdcanary-`, and each
app's contract declares a secret env value `CANARY_SECRET:
pdcanary-env-<suffix>` (a fixed suffix per app, committed in the
fixture contract). The kit **fails** if any committed record contains
`pdcanary-` or the canary env value, or if the canary value appears
in the app container's environment.

## The ten cases (per adapter)

1. Frozen lockfile honored (the recorded install step is the
   adapter's frozen install, exit 0).
2. Lockfile-mismatch commit → BUILD_FAILED (the frozen install
   itself fails; the ecosystem's mismatch error is in the logs).
3. Declared runtime selected; the version reported inside the
   container matches the plan.
4. Build, migrate, seed run (recorded executed steps, exit 0).
5. Forced start failure is typed START_FAILED with the last concrete
   error in the provision logs.
6. Network cut after install; no secret in the container env.
7. Bug pair FAIL at fix^ / PASS at fix (candidate_catch).
8. Clean commit gives no FAIL (no false alarm).
9. No canary in any committed record.
10. A committed record replays to the same verdict (re-scoring the
    committed evidence reproduces the stored score).

Cases are implemented in `tests/test_conformance.py` on top of
`conformance/kit.py`, and run in the **sandbox-tests** CI job — a
skip there fails the job (`PD_REQUIRE_SANDBOX=1`), so the kit cannot
go green by skipping.
