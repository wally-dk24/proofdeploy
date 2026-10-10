# WO-1 Milestone: Provisioner

**Date:** 2026-10-10
**Branch:** `feat/runner-provisioner` (merged commit `13bf622`)
**Accepted by:** Orchestrator (Master's other agent) on 2026-10-09
**Work order:** WO-1 from brief 2026-10-10

## What WO-1 delivered

The target provisioner (`proofdeploy/runner.py`) now meets all the review requirements:

- **Runtime version selection, actually used.** The declared range (`engines.node`, `requires-python`) is matched against installed binaries (`node`, `node24/22/20/18/16`, `python3.12/11/10/9/8`). The selected install is pinned first on PATH for every step (install, build, migrate, seed, start) via a per-run `bin/` directory of symlinks. The recorded version is re-probed through the step PATH, so it is the version the target really runs. An unsatisfiable range yields BUILD_FAILED naming the range and what was found.
- **Package manager per lockfile.** npm for `package-lock.json`, yarn (Berry `--immutable` / v1 `--frozen-lockfile`) for `yarn.lock`, pnpm `--frozen-lockfile`, bun `--frozen-lockfile`, uv `--frozen` for `uv.lock`, poetry for `poetry.lock`, pip into an isolated venv for `requirements.txt`.
- **`packageManager` honored.** When a repo declares e.g. `pnpm@9.1.0`, installs run through corepack with that version; when several lockfiles exist, the `packageManager`-declared one is preferred.
- **Contract steps in order.** `build`, then `migrate`, then `seed`, with contract `env` applied to all.
- **Subprocess env allowlist.** Only PATH and HOME are inherited; CI and runtime vars are set explicitly. A missing binary becomes BUILD_FAILED with a reason, never an exception.
- **Lockfile mismatch is BUILD_FAILED.** For every package manager. `expected_lockfile_sha256` is required for measured runs.
- **Unpinned `requirements.txt` is BUILD_FAILED.** Every package line must carry an `==` pin.
- **Fresh venv per run.** Created at `workdir/venvs/<run-id>/`, its `bin/` first on PATH for all steps, deleted on every exit path. uv gets `--python <selected>`; poetry gets `env use <selected>`.
- **Readiness joins the assigned port.** A `/path` readiness value is joined to the target URL; a full URL is used as-is with a logged warning on port mismatch.
- **Target output captured.** stdout/stderr go to `workdir/target-logs/<run-id>.log`; the tail is attached to failure records.
- **Scratch cleanup.** Venv and run-bin directories are removed on every exit path; on READY the run id is stashed for the caller to clean up.

## Real-run evidence

Verified with real subprocesses (no mocks), on synthetic throwaway apps (no eval or held-out repos touched):

- **Node:** `engines: ">=20 <21"` with `/opt/node20` (v20.20.0) present and default `node` v22.22.0: READY, recorded `v20.20.0`, target served `process.version` = `v20.20.0`. The blocker (selected version recorded but not used) is fixed.
- **Node:** `engines: ">=22 <23"`: READY end to end on v22.22.0.
- **Python:** READY; the target ran the per-run venv's python.
- **Tampered lockfile** (one byte changed): BUILD_FAILED, "lockfile mismatch: frozen lockfile changed".
- **Unsatisfiable range** (`engines: ">=99"`): BUILD_FAILED, "no installed node satisfies declared range".

## Tests

- **136 passed, 84.36% coverage** (floor 80%). Ruff and mypy clean.
- Includes a host-independence fix: two tests failed on machines with corepack on PATH; a `_no_corepack()` helper now mocks `shutil.which` for corepack only.

## Review history

WO-1 was first claimed complete at `12a3d8e`, then held open: the required real runs had not happened and six gaps remained (version recorded but not used, packageManager lockfile preference, unpinned requirements, shared venv, readiness/port mismatch, discarded target output). Follow-up `b704403` fixed all six; the orchestrator then reproduced a blocker (selected Node version never reached the build) and `5f165d8` fixed it by pinning the selected install first on PATH. The orchestrator verified the fix with a genuine second Node install and accepted WO-1.

## Note for WO-5

A READY run keeps its scratch directory until `cleanup_run(run_id)` is called. The orchestrator must call it for both fix^ and fix, on every exit path.
