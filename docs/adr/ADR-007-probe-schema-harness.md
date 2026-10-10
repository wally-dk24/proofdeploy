# ADR-007: Strict Probe Schema Harness

**Date:** 2026-10-09
**Status:** Accepted

## Context

Skill registration v2 defines a language-neutral probe schema: probes test
externally observable behavior only (HTTP status/headers/bodies, read-only DB
state). The old Bottle prompt asked for Python code running inside the target
process, which the harness must reject.

## Decision

Implement `proofdeploy/probe.py` with:

1. **Strict schema validation** (`validate_probe`): every probe must match
   `{setup, act, assert}` exactly. Unknown fields fail closed.
2. **Code-probe rejection** (`_check_no_code_probe`): any string containing
   code markers (fenced python/js/php blocks, `eval(`, `import subprocess`,
   etc.) is rejected at every level of the probe.
3. **Read-only DB enforcement**: `db` assertions accept only SELECT/WITH queries.
4. **Harness-app setup** (option b, Master-approved 2026-10-09): library repos
   may include a `harness_app` setup step writing a minimal app mounting the
   library. The source is hashed (`harness_app_hash`) and recorded alongside
   probes.
5. **Harness version** (`HARNESS_VERSION = "1.0.0"`): recorded in every run
   manifest. Any schema change bumps the version.

## Consequences

- The author cannot smuggle code execution into probes.
- Library repos (Hono, Fastify, body-parser, Bottle) get a recorded,
  hashed harness app instead of inline code.
- Eval repos stay HTTP/DB-only (no `harness_app` steps).
- `ProbeSet.from_author_output` parses fenced ```json blocks and rejects the
  whole set on any single probe failure.
