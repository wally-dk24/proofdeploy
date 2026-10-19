# CLAUDE.md — working rules for AI agents in this repo

ProofDeploy gives developers and coding agents **evidence before "done"**: an AI writes black-box probes for a change, and ProofDeploy runs them on a sandboxed copy of the app before and after the change, then gives a deterministic, replayable verdict. Read `README.md` and `CONTRIBUTING.md` first. This file adds the rules an agent must follow.

## Who decides what
- **Master** (the human owner) decides scope, policy, registrations and scoring rules, spending, anything public, and the closing of unmerged PRs. He approves every model-calling run in the GitHub `measurement` environment. Agents never approve, bypass or work around that gate.
- **The reviewer** plans, reviews and merges. **The implementer** builds. They are never the same agent for the same change.
- The decision log (D-numbers) and the plan of record live in the owner's project, not in this repo. Work orders cite them; PR descriptions cite the work order's decision numbers.

## Hard rules (never)
- Never push to `main` except as the reviewer's merge step (the merge commit, after CI is green).
- Never commit secrets, tokens or keys; never print environment variables. The model key reaches code only as `GROQ_API_KEY` inside the `measurement` environment (the name is historical; it holds the Cerebras key).
- Never change a **pinned** file without a signed registration. The pins are asserted in code and docs; the current ones include:
  - `proofdeploy/model_client.py` (`a35238a5…`);
  - the author prompt template (`fa72a0e6…`);
  - the fixture template (`0994d1e3…`);
  - the skill author copy `docs/evaluation/skill/skill-author.md` (asserted by `EXPECTED_SKILL_HASH` in `proofdeploy/fixture.py`).

  Find every assertion with `grep -rn <hash prefix>` before touching any of them.
- Never change registered scoring, selection or fixture text in `docs/registration/` or `docs/evaluation/`, or the contracts in `readiness/contracts/`, unless the work order quotes the signed text verbatim.
- Never run model calls against held-out, checkpoint or real-app units unless a work order names the unit and Master approves the run. Golden units (`golden/<app>`) are for smoke tests and gates and always record `measured_result: false`.
- Never delete what you did not create (files, branches, directories on shared machines).
- Never report an identifier (SHA, run ID, count) you did not read back from git, the filesystem or GitHub. Correct any wrong number you reported, saying so.

## How to work
1. **Branch** from current `origin/main`: `feature/<topic>`, `fix/<topic>` or `experiment/<topic>`. Every dispatch or run is made from a ref that contains current main (state the merge-base).
2. **Set up:** `python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"`.
3. **Change only what the work order names.** If something else must change, stop and report it.
4. **Tests:**
   - Every behaviour change gets a test that fails without the change. Then **plant** the defect yourself (revert or weaken the fix) and show the test failing, with raw output, before restoring.
   - Run the affected test files, plus `ruff check`, `ruff format --check` and `mypy` on changed files.
5. **The test manifest:**
   - After adding or removing tests, run `python scripts/check_test_manifest.py --write` and commit the regenerated `tests/expected_host_nodeids.txt`.
   - `python scripts/check_test_manifest.py` must print OK. It also guards the CI shard matrices and the job budget.
   - A new test file must be assigned to a shard in `.github/workflows/ci.yml`.
6. **Workflows:**
   - Pin every action by full SHA, with a version comment.
   - Pass inputs through `env:`, never inline them in shell.
   - Give the least `permissions:` needed.
   - Model-calling workflows use `environment: measurement` and `concurrency: {group: proofdeploy-model-calls, cancel-in-progress: false, queue: max}`.
7. **Commits:** small and descriptive. The author line names the role (for example `builder-sonnet (D237)`). End with the co-author and session trailers your harness requires.
8. **Report back** to the reviewer in plain text:
   - branch, head SHA (from `git ls-remote`) and merge-base;
   - `git diff --stat origin/main...HEAD`;
   - commands run, with raw output (tests, plants, manifest check, lint);
   - anything uncertain or deviated from.

## Evidence for runs
- Predictions are committed before the first dispatch of anything they predict.
- Every readiness or measured run gets an evidence pack in the relay repo `wally-dk24/proofdeploy-relay` under `evidence/<run-id>/`, with the rows or records JSON, the decisive logs and the artifact sha256. Generated secrets are redacted by value.
- A run that fails for an environment or transport reason is fixed and repeated on the same unit, never silently replaced.

## Layout
| Path | Contents |
|---|---|
| `proofdeploy/` | The engine: orchestrator, executor, sandbox, model client, CLI (`verify`, `measure`) |
| `readiness/` | Provisioning harness, the contract store (`readiness/contracts/<owner>__<repo>.yml`), seeds, auth proofs, rehearsal probe sets (`readiness/rehearsal/`) |
| `docs/registration/`, `docs/evaluation/` | Signed registrations and evaluation texts (frozen) |
| `conformance/` | Golden apps and the conformance kit |
| `scripts/` | Selection walks (`scripts/wo6/`) and the manifest checker |
| `experiments/` | Reviewer-approved, non-measured experiments |
| `.github/workflows/` | `ci`, `sandbox-tests`, `readiness`, `measure`, `verify` and experiment workflows |
