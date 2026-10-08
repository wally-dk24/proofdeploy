# Setting up proofdeploy.yml

`proofdeploy.yml` lives at your repo root. It is the contract between your
repo and the runner: what it needs to build, start, and probe your app.

| Field       | Required | What it is |
|-------------|----------|------------|
| `build`     | yes | One command that produces a runnable app |
| `start`     | yes | Launches the app in the foreground |
| `readiness` | yes | HTTP endpoint the runner polls until the app is up |
| `migrate`   | no  | Brings the disposable database to the change's schema (DB-backed apps only) |
| `seed`      | no  | Loads the minimum data probes need (only if probes need pre-existing data) |
| `env`       | no  | Non-secret environment for the run |

The minimal file is three lines: `build`, `start`, `readiness`. Add the rest
only if your app needs them.

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
