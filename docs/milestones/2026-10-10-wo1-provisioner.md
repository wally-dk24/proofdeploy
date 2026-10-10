# WO-1 Milestone: Provisioner

**Date:** 2026-10-10
**Branch:** `feat/runner-provisioner` (merged head `13bf622`, PR #34)
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
- **Fresh venv per run.** Created at `workdir/venvs/<run-id>/`, its `bin/` first on PATH for all steps, deleted on every failure path (READY runs keep theirs until the caller calls `cleanup_run`). uv gets `--python <selected>`; poetry gets `env use <selected>`.
- **Readiness joins the assigned port.** A `/path` readiness value is joined to the target URL; a full URL is used as-is with a logged warning on port mismatch.
- **Target output captured.** stdout/stderr go to `workdir/target-logs/<run-id>.log`; the tail is attached to failure records.
- **Scratch cleanup.** Venv and run-bin directories are removed on every failure path; READY runs are cleaned up by the caller via `cleanup_run`.

## Real-run evidence

All runs used real subprocesses (no mocks), on synthetic throwaway apps (no eval or held-out repos touched).

### Orchestrator's verification runs

The orchestrator ran these independently on a genuine second Node install (`/opt/node20`, v20.20.0) while the `node` on PATH was v22.22.0:

- **Node:** `engines: ">=20 <21"`: READY, recorded `v20.20.0`, target served `process.version` = `v20.20.0`. The blocker (selected version recorded but not used) is fixed.
- **Node:** `engines: ">=22 <23"`: READY end to end on v22.22.0.
- **Python:** READY; the target ran the per-run venv's python.
- **Tampered lockfile** (one byte changed): BUILD_FAILED, "lockfile mismatch: frozen lockfile changed".

### Wally's verification runs

Wally's own real runs (mock `node24` wrapper reporting `v99.1.0` and setting `MOCK_NODE_ID`, since no second real Node exists on this machine):

**Run 1 — `engines: ">=99"`, only mock `node24` satisfies:**

```
===================================================================
WO-1 BLOCKER REAL RUN 1: Node with engines >=99 (only mock node24 satisfies)
Date: 2026-10-10T01:20:11Z
Mock setup: fakebin/node reports v22.22.0, fakebin/node24 reports v99.1.0
            (wrappers exec real /usr/bin/node, set MOCK_NODE_ID)
===================================================================
lockfile sha256: b706b3644d43dd95...

=== RESULT: ready ===
runtime: node
runtime_version (recorded): v99.1.0
package_manager: npm
reason: None
run_id: c16c77171b26

=== TARGET REPORTS ===
mockNodeId: node24
execPath: /usr/bin/node
process.version: v24.20.0

cleanup done: venv/runs dirs for c16c77171b26 removed

=== RELEVANT LOG LINES ===
  runtime: node (declared range: >=99)
  runtime binary: node24 (v99.1.0) satisfies '>=99'
  run bin: /tmp/wo1-blocker/work/runs/c16c77171b26/bin (node -> /tmp/wo1-blocker/fakebin/node24)
  lockfile: package-lock.json sha256: b706b3644d43dd95...
  effective runtime: node -> /tmp/wo1-blocker/work/runs/c16c77171b26/bin/node (v99.1.0)
  runtime version in use: v99.1.0
  target ready

===================================================================
WO-1 BLOCKER REAL RUN 2: Tampered lockfile (one byte changed)
===================================================================
expected sha: b706b3644d43dd95...
actual sha:   6512136d42803337...
status: build_failed
reason: lockfile mismatch: frozen lockfile changed
```

The `mockNodeId: node24` line proves the target was launched through the selected binary via the pinned PATH, not the default `node`. (The wrapper reports `v99.1.0` for `--version` but execs the real `/usr/bin/node` for execution; the version-string mechanism is identical for real multi-version installs.)

**Earlier Wally runs** (from the first real-run pass, `>=18` range on the default install):

```
=== 1. Node app (real subprocesses) ===
  lockfile sha256: eb193883654dd026f1825f864e1e70567d015e02fa1a5ff176c4a7b38794f110
  status: ready
  runtime: node version: v24.20.0 pm: npm
  reason: None
  target_log: /tmp/wo1-real/work/target-logs/642b0e4f44a3.log
  [node] GET /health -> 200 'ok'
  target log tail: ['listening on 18091']
  [node] target stopped
  NODE OK
=== 2. Python app (real subprocesses, per-run venv) ===
  lockfile sha256: ad1f8ed10759652a0ba2793e0e78596ac690eda8b776190a1bb04e809f6ae90d
  status: ready
  runtime: python version: 3.12.3 pm: pip
  reason: None
  [python] GET /health -> 200 'ok'
  venvs created: ['10bcefe37662']
  [python] target stopped
  PYTHON OK
=== 3. Edited lockfile -> BUILD_FAILED ===
  status: build_failed
  reason: lockfile mismatch: frozen lockfile changed
  TAMPERED LOCKFILE OK
=== 4. Unsatisfiable engines range -> BUILD_FAILED ===
  status: build_failed
  reason: no installed node satisfies declared range '>=99' (found: node=v24.20.0, nodejs=v24.20.0)
  BAD RANGE OK
ALL REAL RUNS PASSED
```

## Tests

- **136 passed, 84.36% coverage** (floor 80%). Ruff and mypy clean.
- Includes a host-independence fix: two tests failed on machines with corepack on PATH; a `_no_corepack()` helper now mocks `shutil.which` for corepack only.

## Review history

WO-1 was first claimed complete at `12a3d8e`, then held open: the required real runs had not happened and six gaps remained (version recorded but not used, packageManager lockfile preference, unpinned requirements, shared venv, readiness/port mismatch, discarded target output). Follow-up `b704403` fixed all six; the orchestrator then reproduced a blocker (selected Node version never reached the build) and `5f165d8` fixed it by pinning the selected install first on PATH. The orchestrator verified the fix with a genuine second Node install and accepted WO-1.

## Note for WO-5

A READY run keeps its scratch directory until `cleanup_run(run_id)` is called. The orchestrator must call it for both fix^ and fix, on every exit path.
