# WO-5 Brief: canonical work order

**Date:** 2026-10-10
**Source:** Reviewer (via Master). Original brief: `~/workspace/your_files/wally-brief-2026-10-10.md`.
**Status:** This file is the canonical WO-5 work order in the repo. Worker instructions for WO-5 must quote this file, not paraphrase it.

---

## Original brief (quoted verbatim)

# Brief for Wally — scoring answer and runner work orders

2026-10-10 · From the reviewer, via Master

**How we work from here:** Wally builds. The reviewer orchestrates: sets the work orders, reviews each one against primary sources, and decides when it's done. Master decides policy and authorizes every merge.

Work through the orders in sequence. Send each one for review when its "done when" list is met, and don't start the next order that depends on it until it passes. The roadmap (`proofdeploy-roadmap.md`) gives the why; this brief gives the what.

---

## Part 1 — Answer to your scoring question

You asked: if one probe is INCONCLUSIVE while the others PASS or FAIL, does that void the whole fix^/fix pair, or are catches scored per probe?

There are two separate rules, and they should not be mixed.

### 1a. Measurement scoring (what the published numbers mean)

Recommended by the reviewer. **Master must approve and register it before any measured run.** Don't put it in scoring code that runs on real data until the registration is committed.

1. **Bugs: a catch is scored per probe.** A bug counts as caught if at least one probe meets all three conditions:
   - it FAILs on fix^;
   - it PASSes on fix;
   - it targets behavior the fix actually changed. The reviewer judges this from the probe against the fix diff.

   A sibling probe that is INCONCLUSIVE or FAILs on both sides doesn't cancel the catch. Every probe is still reported.
2. **A whole side failing makes the pair INCONCLUSIVE.** If fix^ or fix can't build or start, or the target is unreachable for every probe, there's nothing to compare. This is already registered (`4d4b71d`).
3. **Clean diffs stay strict.** Probes run against C only. Any FAIL or INCONCLUSIVE is a false alarm.
4. **Same rule for the skill and no-skill arms.**
5. **Optional, Master's call:** a cap on probes per diff (e.g. 10), registered at the same time. It limits "write many probes and hope one is lucky."

Why not let one INCONCLUSIVE spoil the pair:
- **It would measure infrastructure, not the author.** One flaky query or one probe hit by API drift would erase a real catch, and our old Bs make drift likely.
- **It isn't what the bar says.** The bar defines a catch as a FAIL on behavior the fix changed, which is a property of one probe, not of the whole set.

### 1b. Product verdict on a PR (what enterprise customers live with)

This is separate from measurement. **Only typed reasons get built now; the rest waits until after the checkpoint (rule 10).**

- **Report every probe on its own.** One INCONCLUSIVE never hides a FAIL, and never hides a pass on another probe.
- **Give every INCONCLUSIVE a typed reason.** **Build this now; it helps measurement too.** The reasons:
  - `environment`: build, start or reach failed;
  - `auth`: credential rejected or expired;
  - `drift`: the API changed between B and fix^;
  - `probe`: the probe itself was invalid.
- **Default gate: fail-closed.** Any FAIL or INCONCLUSIVE blocks. This is the registered product rule.
- **Later:**
  - an admin can downgrade environment-class INCONCLUSIVE to "warn", logged with who and when. FAIL can never be downgraded, and INCONCLUSIVE is never shown as a pass;
  - at most one recorded retry, environment class only, with both attempts in the record;
  - an audit trail per verdict: replay record, model, probe hash, and the policy in force.

---

## Part 2 — Working rules (unchanged, enforced)

- **Blind means blind.** The author gets only:
  - the B^→B diff;
  - a B snapshot with no git history;
  - the generic `proofdeploy.yml` and fixture description;
  - the registered skill copy, or nothing in the no-skill arm.

  It never sees the fix, later commits, eval files or the network. Wally and the reviewer are contaminated on every vetted bug and never write probes for them.
- **Every packet carries the verification commands and their verbatim output.** Regenerate it from fresh output; never hand-edit.
- **Branches:**
  - one branch per work order, based on `main` after Master's merges;
  - docs-only branches for registrations;
  - a registration commit contains the rule only; results go in a separate, later commit.
- **No merges without Master.**
- **No scoring rule lives only in code.** If code decides how something is counted, the rule is registered first.
- **Fail-closed means the safe value is the default.** Callers shouldn't have to remember to pass it.
- **Tests that mock subprocesses or servers don't prove the runner works.** Each piece needs at least one real run.

---

## Part 3 — Work orders

### WO-0 · Rebase onto `main` (after Master's merges)

Master merges, in order:
1. `docs/skill-registration-v2`
2. the start-failure addendum
3. `docs/held-out-clean-diffs-final` + the corrected milestone doc
4. `feat/assembler-skill-integration` (`99a3253`)
5. `feat/probe-schema-harness` (`518aa6d`)

**Done when:**
- every runner branch is rebased onto the new `main`;
- the stack no longer contains local merge commits of the docs branches;
- `probe.py` and the assembler's no-skill mode are present in the stack.

**Send:** `git log --oneline main..<branch>` for each branch, plus the test summary.

### WO-1 · Provisioner fixes

**Done when:**
- **Runtime version comes from the repo:** `engines` or `packageManager` (Node), `requires-python` (Python). The version is recorded in the run record, and a missing runtime is BUILD_FAILED with a reason.
- **The package manager matches the lockfile:**
  - `npm ci` for `package-lock.json`;
  - `yarn install --immutable` (or `--frozen-lockfile` for v1) for `yarn.lock`;
  - `pnpm install --frozen-lockfile` for `pnpm-lock.yaml`;
  - `bun install --frozen-lockfile` for `bun.lock` (Vendure C1/C2 need it).

  Python uses a lock-honoring tool (e.g. `uv sync --frozen`, `poetry install --no-update` or a hash-pinned requirements file) inside an isolated venv, never the host environment. An unrecognized or missing lockfile is BUILD_FAILED.
- **The contract's `build`, `migrate` and `seed` steps run, in that order,** and the contract's `env` is applied.
- **The subprocess environment is built correctly:** a minimal allowlist (`PATH`, `HOME`, the runtime's own variables, the contract `env`), not an empty dict. A missing binary becomes BUILD_FAILED, not an uncaught exception. It currently raises `FileNotFoundError: 'npm'`; this was reproduced.
- **Lockfile mismatch fails the build** for every package manager, and `expected_lockfile_sha256` is required for measured runs.
- **The process is cleaned up** on every exit path (killed and reaped).
- **Proven by real runs:** one real Node target and one real Python target provisioned through real subprocesses. Use small public repos from the dev set, not mocks.

**Send:** the code, the test summary, and the two real provisioning logs. Also deliberately change one lockfile byte and show the BUILD_FAILED output.

### WO-2 · Executor fixes

**Done when:**
- **Validation:** the executor calls `validate_probe` (with `allow_harness_app` set per repo class) before running anything. An invalid probe is INCONCLUSIVE with reason `probe`.
- **Unknown setup step types** are rejected, not skipped.
- **The verdict doesn't depend on assertion order.** Precedence is fixed and documented: any FAIL → FAIL. Otherwise any undecidable assertion → INCONCLUSIVE. Otherwise → PASS.
- **Setup steps are checked:** a setup step's HTTP failure (no response, or a status outside the step's expected range) is INCONCLUSIVE with reason `environment` or `probe`, and recorded.
- **Database assertions:** each one uses its own query and its own result. A probe that can't be evaluated is refused, never quietly checked against another query's result.
- **Redirects are not followed,** so a 3xx can be asserted.
- **Remote targets:** GET/HEAD only, enforced in code. Writes only on instances the runner provisioned, every write logged.
- **Typed INCONCLUSIVE reasons** (Part 1b) on every INCONCLUSIVE.

**Send:** the code, a test per point, and the test summary.

### WO-3 · Setup and auth fixes

**Done when:**
- **Unknown `${NAME}` placeholders are rejected** (INCONCLUSIVE, reason `probe`), never sent literally. This matches the existing validator.
- **Binding capture is wired into the executor:** values captured in setup are available to later steps and the act.
- **Captures are recorded:** a capture that finds nothing is INCONCLUSIVE with reason `probe`, recorded in the run record.
- **`${AUTH_TOKEN}` from the fixture always wins** and stays valid for the whole run. Credentials that need refreshing (Ghost's per-request JWT) go through a hook, which is required for Ghost's go/no-go.

**Send:** the code, the tests, and the test summary.

### WO-4 · Run pair and records

**Done when:**
- **Catch scoring follows Part 1a** (per-probe for bugs), behind a version flag. It is used for measured runs only after Master's registration is committed.
- **Clean-diff scoring is implemented:** probes run against C, and any FAIL or INCONCLUSIVE is a false alarm.
- **Records are repeat-safe:** unique per run (run id + timestamp), append-only, never overwritten. The 3 same-model repeats must all survive.
- **Each record holds everything the run used:**
  - prompt;
  - raw model response;
  - parsed probes and probe hash;
  - bundle manifest (skill hash or no-skill);
  - harness version;
  - provisioning logs for both sides, with runtime versions and lockfile hashes;
  - per-probe results with typed reasons.
- **Records are committed before scoring.** The record commit is in git before the score is computed, and the score is written in a separate commit.

**Send:** the code, the tests, and one example record.

### WO-5 · Orchestrator and model client

**Done when:**
- **One command runs a measured unit end to end:**
  1. build the author bundle from the B^→B diff + B snapshot only (assembler);
  2. call the model;
  3. parse and validate the probes;
  4. provision fix^ and fix (bugs) or C (clean diffs);
  5. execute the probes;
  6. write the record;
  7. score.
- **The model client is pinned to the registered model:** Groq `openai/gpt-oss-120b`, temperature 0.2, one attempt, no tools. The model and its settings are recorded.
- **A leak check runs before every model call.** It fails the run if the bundle contains anything outside the allowlist: the fix SHA, any later commit's content, fix tests, eval files or a `.git` directory. The bundle manifest hash goes in the record.
- **Unattended runs:** rootless container mounting only the checkout, network cut after install, no secrets in the container environment. This must be in place before any unattended run.

**Send:** the code and the leak-check tests. Also seed the fix diff into a test bundle deliberately and show the run refused.

### WO-6 · Real acceptance test (Ghost's deadline)

**Done when:**
- **A small app** (a real repo with a database and a login, not an in-process test server) **runs through WO-5 end to end:**
  - real provisioning;
  - a blind author (the registered model) writing probes from the diff alone;
  - real execution, a record and a score.
- **The app and its bug are chosen before the run,** recorded, and never vetted by Wally or the reviewer beforehand, so the author is truly blind.
- **Ghost's go/no-go is decided at this point, per the registered rules.** If Ghost isn't fully working (build, start, seed, auth with JWT refresh), drop bug #1 and withdraw C5.

**Send:** the JSONL record, the provisioning logs, and the Ghost decision with its evidence.

### After WO-6 (not part of this brief's build work)

1. Re-run the no-skill baseline with the corrected prompt.
2. Dev-set measurement on the held-out set (4 bugs + 4 clean diffs), skill vs no-skill, with 3 repeats, against Master's floors.
3. Master decides and registers whether the checkpoint runs with the skill or without.
4. Run the blind checkpoint and publish the result, pass or fail.

---

## Part 4 — Not now (rule 10)

None of these get built until the checkpoint has earned them:
- HTML reports or the diff reader (WAL-55);
- adapters for stacks beyond what the dev and eval sets need;
- admin policy, the audit UI or the retry policy;
- integrations (agent hook, GitHub Action);
- any public claims.

---

## Part 5 — Open decisions for Master

1. **Run-pair scoring rule (Part 1a):** approve and register, with or without the probe cap.
2. **Merges, in the order in WO-0.**

---

## Scope correction 2026-10-10 (quoted verbatim from the orchestrator)

**WO-5: scope correction. Build to `claude/wally-brief-2026-10-10.md`, quoted, not paraphrased.**

1. **The leak check guards against answer-key leaks.** Before every model call, fail the run if the author bundle contains anything outside the allowlist: the fix SHA, any later commit's content, fix tests, eval files, or a `.git` directory. Put the bundle manifest hash in the record. Your "zero secret values" check can stay as an extra.
2. **Required test:** seed the fix diff into a test bundle deliberately and show the run is refused.
3. **Sandbox for unattended runs:**
   - a rootless container that mounts only the checkout;
   - network cut after install;
   - no secrets in the container environment.
   It must be in place before any unattended run.
4. **One command per measured unit:** bundle (B^→B diff + B snapshot only), model call (registered config, recorded), parse and validate, provision fix^ and fix (or C), execute, record, score.
5. **Carried from WO-4:**
   - `commit_records` for every score;
   - `cleanup_run` on every exit path;
   - never write judgment records.
   `harness_app` provisioning goes real here.
6. **Out of WO-5:** the blind real-app run and the Ghost go/no-go are **WO-6**, after WO-5 is accepted.
7. **For WO-6, don't pick the app yourself.** Draft a mechanical selection rule: a fixed candidate list plus a deterministic first-match criterion. Put the rule alone in its own commit for Master to sign off before it runs, and the result in a later commit.
