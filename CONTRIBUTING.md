# Contributing to ProofDeploy

## Workflow

- **No direct pushes to `main`.** Everything goes through pull requests from
  `feature/<name>` branches. Branch protection enforces this.
- Every PR gets an automated agent review plus CI (tests, lint, typecheck).
- Keep PRs small and focused. One concern per PR.

## Development setup

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest -q
```

## Standards

- `ruff check` and `ruff format --check` must pass.
- `mypy` must pass (`disallow_untyped_defs` is on — type all functions).
- Tests must pass with ≥80% coverage (`pytest` enforces the gate).
- No secrets in the repo, ever. API keys come from the environment at runtime.

## Design rules (from the PRD)

- The LLM works at authoring time only. Execution is deterministic code.
- Safety is isolation first: disposable instance, disposable database, no credentials.
- Loud failures: any FAIL or INCONCLUSIVE fails the verification. No amber states.
