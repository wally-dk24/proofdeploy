# ADR 0001: Python as the implementation language

Date: 2026-10-08
Status: Accepted

## Context

ProofDeploy needs a CLI that shells out to git, builds containers, runs HTTP
probes, and orchestrates disposable infrastructure. Candidates: Python, Go,
TypeScript/Node.

## Decision

Python, stdlib-first, with minimal dependencies (PyYAML for the repo contract).

## Rationale

- Fastest iteration speed for the one-week build.
- Rich ecosystem for HTTP, subprocess orchestration, and LLM clients.
- The standing Docker Hub rule covers distribution: every shipped tool gets a
  container image, so the "single binary" advantage of Go is not decisive.
- Type safety via mypy (`disallow_untyped_defs`) and linting via ruff close
  the usual Python rigor gap.

## Consequences

- Distribution is via pip and container image, not a static binary.
- If startup time or binary distribution becomes a real constraint later,
  revisit (unlikely for a CI/dev-machine tool).
