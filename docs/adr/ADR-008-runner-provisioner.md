# ADR-008: Runner Provisioner

**Date:** 2026-10-09
**Status:** Accepted

## Context

WAL-58 requires a runner that provisions probe targets from `proofdeploy.yml`.
The provisioner is the first of five runner pieces (provisioner, executor,
setup/auth, run pair/records, acceptance test).

## Decision

`proofdeploy/runner.py` implements `Provisioner` with:

1. **Typed results, never crashes.** `ProvisionResult` carries `ProvisionStatus`
   (READY / BUILD_FAILED / START_FAILED). A target that cannot build or start
   yields INCONCLUSIVE at scoring time (per the start-failure rule), not an
   exception.
2. **Runtime detection** from `package.json` (node) or `pyproject.toml` /
   `requirements.txt` (python).
3. **Frozen lockfile verification.** Missing lockfile or hash mismatch against
   the expected value = BUILD_FAILED.
4. **Non-interactive installs** (`npm ci`, `pip install -r`) with `CI=true`.
5. **Readiness check** with timeout; a target that exits early or never becomes
   ready = START_FAILED.

## Consequences

- The scorer maps BUILD_FAILED / START_FAILED to INCONCLUSIVE without special
   casing.
- Lockfile drift between B and fix^ surfaces as INCONCLUSIVE, not a false
   catch.
- The provisioner does not run probes; that is the executor's job (next PR).
