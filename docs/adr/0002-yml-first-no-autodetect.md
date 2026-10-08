# ADR 0002: Repo-supplied contract instead of auto-detection (v1)

Date: 2026-10-08
Status: Accepted

## Context

To provision a target, ProofDeploy must know how to build, migrate, seed,
start, and health-check an arbitrary repo. Fully automatic detection across
stacks is a research project of its own.

## Decision

v1 requires a small `proofdeploy.yml` at the repo root with `build`,
`migrate`, `seed`, `start`, `readiness`, and `env` commands. Auto-detection
becomes post-checkpoint polish.

## Rationale

- Getting arbitrary old commits of unfamiliar repos to build, start, and
  migrate is the hardest part of the project; auto-detecting all of it would
  eat the week.
- An explicit contract is debuggable: when provisioning fails, the failing
  command is visible, not a guess the tool made.
- This also resolves the "how does it know it's ready" question for v1.

## Consequences

- Adoption requires the repo author to write ~6 lines of YAML. Acceptable:
  CI already demands the same knowledge.
- The evaluation repos must each get a `proofdeploy.yml` plus a fixture plan
  before the probe author is built.
