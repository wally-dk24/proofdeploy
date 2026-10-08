# ProofDeploy

**Diff in, verified out.**

ProofDeploy is a CLI tool that reads your diff, authors executable probes for exactly what changed, runs them against a provisioned instance of the change, and renders per-claim verdicts. The pipeline stops answering "did it compile" and starts answering "does it work."

```
$ proofdeploy verify

Diff: main...HEAD (3 files, +142/-38)
Change model: 2 behavior-adds, 1 schema-change, 1 must-not-change
Provisioning: built in 47s, migrated disposable db, ready
Probes: 5 authored, 5 run

  ✓ POST /quotes accepts region — PASS (201, 84ms)
  ✓ Quotes.Region non-nullable — PASS (0 nulls)
  ✗ EU rate correct — FAIL (expected 4.20, got 3.90)

VERIFICATION FAILED — 1 claim broken
```

## The loop

**diff → probes → verdicts**

1. **Diff reader** — parses code, migration, and config diffs into a structured change model (any stack; one migration adapter per release, behind a common operation model).
2. **Probe author** — an LLM reads the change model and authors HTTP + database probes. Authoring time only; the probe set is frozen before anything executes.
3. **Provisioner + runner** — checks out the commit in isolation, builds it per `proofdeploy.yml`, starts it against a disposable database (migrations + seed applied), runs the probes, tears everything down.
4. **Verdict renderer** — PASS / FAIL / INCONCLUSIVE per claim, with the exact probe and evidence shown. Terminal output plus a self-contained HTML report.

## Design rules

- **The agent works at authoring time, then gets out of the way.** Execution is deterministic code. Same probes + same instance = same result.
- **Safety is isolation first:** disposable instance, disposable database, no credentials. DB probes run read-only by database enforcement (read-only role or always-rollback transaction).
- **Loud failures.** Any FAIL or INCONCLUSIVE fails the verification. No amber states, no silent passes.
- **Stateless.** No service database; run records are flat JSONL.
- **v1 runs advisory in CI** — posts verdicts, blocks nothing. It earns gating by being right.

## Repo contract

A repo under verification supplies `proofdeploy.yml` at its root:

```yaml
build: "dotnet build -c Release"
migrate: "dotnet ef database update"
seed: "./seed.sh"
start: "dotnet run --no-build"
readiness: "http://localhost:5000/health"
env:
  ASPNETCORE_ENVIRONMENT: Verification
```

## Status

**Working now:** the agent skill (authoring draft) — install it from the
marketplace below, point your coding session at a diff, and get back a change
model plus `probes.json`. CI (ruff, mypy, pytest, 80% coverage floor) and an
agent reviewer run on every PR; branch protection requires both.

**Under construction:** the deterministic runner (`proofdeploy verify
--probes`), the diff reader, the provisioner, and the verdict renderer.

**Proven before production:** the runner stays advisory in CI until the blind
evaluation earns it gating authority. FAIL or INCONCLUSIVE fails verification —
no amber states, ever.

## Claude Code marketplace

This repo is its own plugin marketplace. To install the `proofdeploy` skill in
Claude Code:

```
/plugin marketplace add wally-dk24/proofdeploy
/plugin install proofdeploy@wally-dk24
```

The plugin ships the skill (`plugins/proofdeploy/skills/`) and the
`/proofdeploy` slash command. Hooks that auto-verify after edits land with the
deterministic runner. The skill is also installable in any Agent Skills host:

```bash
npx skills add wally-dk24/proofdeploy --skill proofdeploy
```

## Examples

`examples/` holds small, honest, runnable samples of what exists today:

- `authoring-a-probe/` — a 1-line diff, its change model, and the two probes
  that catch the break.
- `validating-probes/` — a machine-readable draft of the probe schema and a
  stdlib-only validator.

CI keeps them honest: `tests/test_examples.py` fails if an example stops
validating.

## Development

Feature branches off `main`, PRs required, agent review on every PR.

```
git checkout -b feature/<name>
# ... work ...
git push -u origin feature/<name>
gh pr create
```
