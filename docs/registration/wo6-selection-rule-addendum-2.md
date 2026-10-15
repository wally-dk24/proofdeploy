# Registration: REG-C Addendum 2 — WO-6 candidate pool and filters

Registered: 2026-10-14. Amends docs/registration/wo6-selection-rule.md (REG-C, 2026-10-11, with the item-2 bare-name ruling). Items not named here are unchanged.

Why. The run on 2026-10-14 (docs/registration/wo6-regc-run-2026-10-14.md) produced zero survivors from a 300-repository pool. The filters encoded the runner's capability on 2026-10-11 (one app at the repository root). The runner now supports single-app workspaces (plan 1.2b) and bun lockfiles (plan 1.2e). This addendum widens the pool and aligns the filters to that capability. No bar is lowered. No bug, fix, diff or issue of any repository was inspected in writing it.

A1. Pool (replaces "the first 100 results per language query" in item 1). The first 1000 results per language query (the GitHub search API maximum), same qualifiers, sort and de-duplication. If the API returns fewer, all returned results are used and the count is recorded.

A2. Node filter (replaces the Node line of item 3). All at the default branch's HEAD:
1. App package. If the root package.json has no workspaces field and the root has none of pnpm-workspace.yaml, lerna.json, nx.json, turbo.json, the app package is the root and it must have scripts.start. Otherwise the app package is the single workspace package whose package.json has scripts.start; zero such packages: excluded; more than one: excluded as "ambiguous app". The app package's directory is recorded as app.path.
2. Lockfile: exactly one root lockfile among package-lock.json, pnpm-lock.yaml, yarn.lock, bun.lock.
3. Database: sqlite3, better-sqlite3 or @libsql/client in dependencies of the root package.json or of the app package's package.json.

A3. Python filter (replaces the Python line of item 3). All at HEAD:
1. App directory: a manage.py at the root, or exactly one manage.py exactly one directory below the root; that directory is the app directory, recorded as app.path. More than one manage.py at depth one and none at the root: excluded as "ambiguous app".
2. Database: the string django.db.backends.sqlite3 somewhere in the repository.
3. Lockfile: one of uv.lock, poetry.lock, pdm.lock, requirements.txt or pyproject.toml in the app directory or at the root (REG-A covers unlocked ones).

A4. Timing. The run under this addendum happens only after the plan 1.2b (workspaces) and 1.2e (bun) changes are merged on main. The run record names the main SHA it ran from.

A5. Script. The query script, as run, is committed in the same PR as the run's raw ordered output, under scripts/wo6/, with the raw API outputs when under 1 MB. One run only.

A6. Fallback (stated before any result). If this run also yields zero survivors, no candidate is chosen by hand. Master decides between (i) a further registered addendum admitting apps with no database requirement, and (ii) deferring WO-6's real-app run until Postgres support (plan 2.2).

Items 4–10 of REG-C apply unchanged to any survivor.
