# Setting up proofdeploy.yml

`proofdeploy.yml` lives at your repo root. It is the contract between your
repo and the runner: six fields, no secrets.

| Field       | What it is | Example |
|-------------|------------|---------|
| `build`     | One command that produces a runnable app | `npm ci && npm run build` |
| `migrate`   | Brings the disposable database to the change's schema | `alembic upgrade head` |
| `seed`      | Loads the minimum data probes need | `python scripts/seed.py` |
| `start`     | Launches the app in the foreground | `uvicorn app.main:app --port 8000` |
| `readiness` | HTTP endpoint the runner polls until the app is up | `http://localhost:8000/health` |
| `env`       | Non-secret environment for the run | `APP_ENV: verification` |

**Secrets rule.** `env` carries configuration, never credentials. The runner
provisions a disposable database per run and injects the connection details
itself. If a value looks like a password, token, or key, it does not belong
here — the run is rejected before anything starts.

## Per-stack examples

- `nodejs.yml` — Express + PostgreSQL + Prisma
- `python.yml` — FastAPI + PostgreSQL + Alembic
- `dotnet.yml` — ASP.NET Core + SQL Server + EF Core

Same six fields, every stack. That is the whole point: the runner learns one
contract and verifies any repo that speaks it.

## Status

These are the declared target of the runner (WAL-58), which is still under
construction. They parse and validate today (`tests/test_examples.py`
checks every file in this directory); they execute when the runner lands.
