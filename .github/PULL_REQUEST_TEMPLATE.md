## What

## Why

## PRD section / ticket

## Test plan

- [ ] `pytest -q` passes (≥80% coverage)
- [ ] `ruff check` and `ruff format --check` pass
- [ ] `mypy` passes

## Safety check (if it touches provisioning, probes, or execution)

- [ ] No new network/credential surface
- [ ] No destructive operations expressible
- [ ] Failures are loud, never silent
