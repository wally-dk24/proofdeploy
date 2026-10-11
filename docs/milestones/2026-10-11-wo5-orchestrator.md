# WO-5 Milestone: The One-Command Measured Run, Proven End to End

**Date:** 2026-10-11
**Repository:** https://github.com/wally-dk24/proofdeploy
**Status:** Closed. Final merge: PR #50, `5a2f99f`. This document is the consolidated record of the WO-5 arc.

---

## 1. What WO-5 delivered

WO-5 built the one-command measured run: a single command takes a target repository, a base commit, a bug commit, and a fix commit, and produces a durable measurement record.

- **The orchestrator.** `proofdeploy measure` runs the whole unit: provisions both sides of the pair (the parent of the fix, and the fix), authors probes with a model, executes the probes against both sides through the sandbox bridge, scores the pair, and writes the records.
- **The model client.** The model call lives in the repository (`proofdeploy/model_client.py`), and the exact request is recorded: endpoint, model, temperature, message hashes. The model client is pinned by hash: `55b7d93283d511b681f7af24dd78aafc0cc750d652ed965f94fe7a4ab1d28598`.
- **The sandboxed measured path.** Provisioning runs in a sandbox (podman, `--net=none` with a Unix-socket bridge for probe traffic, a separate build network for installs) with a fail-closed measurement gate: if the capability probe fails the gate, the run refuses to measure rather than falling back to unsandboxed execution. Every record carries `sandbox_evidence`.
- **Answer-key and leak checks.** The probe bundle is checked by allowlist-by-equality: the bundle may contain only what the allowlist permits, and any content matching the fix (the seeded fix, later commits' content, fix tests, eval files, or `.git`) fails the run. A seeded-fix refusal test proves the check refuses a bundle that carries the answer key. The bundle manifest hash is in the record. Records are secret-redacted by value before they are committed.
- **The record format.** Every record carries `schema_version` and per-probe results. Evidence is committed before the score that cites it. Scoring is `v2-per-probe`. Section 4 records the schema change made during WO-5.2.
- **Failure-cause rules 5 and 6** (registered in `docs/registration/wo5-prompt-v7.md`, PR #46, `db1e251`). Rule 5: a failure in ProofDeploy itself after the model has answered is INCONCLUSIVE, reason class `environment`, cause `tool_failure`; the evidence record keeps the error, the runner log, and every result gathered before the failure. Rule 6: every probe is validated on its own; an invalid probe is not sent and is reported individually as INCONCLUSIVE, reason class `probe`; a unit with no valid probe has cause `author_output_invalid`.
- **Author prompt template v7.** The probe-authoring prompt is a registered artifact, pinned by hash: `fa72a0e6c4a1fa9fb204986e08c8eef4b11fc13786f41389ecf2f42ea8555cf3`. Registration landed through PR #44 (`a11456b`), PR #45 (`10ae057`), and PR #46 (`db1e251`).

Measured runs execute on GitHub Actions via `measure.yml`. The API key lives in the GitHub `measurement` environment with Master as required reviewer; it never enters the container environment, never appears in a prompt, and is redacted by value in every record. Records are pushed to a `records/run-<run-id>` branch, evidence commit first, then the score commit. The records of every acceptance run are append-only history and were never modified.

The standing order for the whole arc, fixed by the reviewer after run 1: reviewer verifies the fix, Master merges, reviewer checks the merge commit, only then the next step.

---

## 2. The three acceptance runs

All three runs used the same inputs: target repo `https://github.com/wally-dk24/proofdeploy-acceptance.git`, arm `no-skill`, `bug_id` `acceptance-001`, base `34f5575ee7f5ef0e00ef223944b9a1d3db3d0b91`, bug `545bf356eda04cfe21b70b7ab5792ec6f69e9995`, fix^ `64c0af77c9ef0916e518d3f475d0fcf7f7be2fd9`, fix `e59b0201651a8e00725c17bf5c810f119d07ab9a`.

### Run 1: 38103425572

- Run: https://github.com/wally-dk24/proofdeploy/actions/runs/38103425572
- Records: branch `records/run-38103425572`, evidence commit `c5c72a6` before score commit `c11475e`.
- Code under test: `main` at `854f537` (PR #47).
- **Verdict: INCONCLUSIVE.** The run executed end to end at the workflow level: the first real model call through the sandboxed pipeline worked, the leak and answer-key checks passed, the correct SHAs and fix^ were used, provenance and pinned hashes were recorded, one valid probe and one malformed probe block were recorded (the invalid block was not sent), and no secret appeared in the records. But both app sides were `START_FAILED` with a readiness timeout.
- **Cause:** dependencies installed during provisioning were not present when the app started (defect S1, section 3). The acceptance-run socket path is 64 characters, so the bridge defect found later did not contribute to this run.
- One process note: the `measurement` environment did not pause for approval during this run, and whether the required-reviewer setting is enforced remains unverified.

### Run 2: 38152175992

- Run: https://github.com/wally-dk24/proofdeploy/actions/runs/38152175992
- Records: branch `records/run-38152175992`, evidence commit `4ac2276` before score commit `5481d30`.
- Code under test: `main` at `04c05b1` (PR #48).
- **Verdict: the pipeline proven end to end; score `no_catch`.** The reviewer verified the records: `scoring_version` `v2-per-probe`, `schema_version` 1.0.0; SHAs B^ `34f5575`, B `545bf35`, fix^ `64c0af7`, fix `e59b020`; `sandboxed=true`, `measured_result=true`; model `openai/gpt-oss-120b` at temperature 0.2, one attempt, the request recorded; template and builder hashes equal to the registered ones; `skill_sha256` null for the no-skill arm; provenance with the run ID, runner image `ubuntu24-20261004.327.1`, and the capability probe; sandbox evidence on both sides (`none+socket`, install on `bridge:pd-build`, `userns-remap`); both sides READY on Python 3.12.15 with the frozen hashed install; leak and answer-key checks passed; no API key in the records; 2 valid probes, both sent on both sides.
- **Cause of the remaining defect:** two of the four FAILs were executor defects, E1 and E2 (section 3). With correct comparisons the verdicts would have been: probe 0 PASS on both sides; probe 1 PASS at fix^ and FAIL at fix. The score stays `no_catch` either way, because the author asserted the buggy behavior as intended. Catch or miss was never the acceptance criterion.

### Run 3: 38159181077

- Run: https://github.com/wally-dk24/proofdeploy/actions/runs/38159181077
- Records: branch `records/run-38159181077`.
- Code under test: `main` at `eb2f083` (PR #49).
- **Verdict: records verified; score `no_catch`, correct under the registered rule.** The inputs were byte-identical to run 2 (same prompt, bundle, lockfile, image digest, and SHAs); fix^ was provisioned correctly; the run was sandboxed; the registered hashes matched; no API key and no fix SHA appeared in the prompt. The two comparison defects fixed in WO-5.1 produced correct verdicts in a real run. The `no_catch` is correct because the author asserted the bug's `200 {}` for a missing item as the intended behavior, the expected reading for this app.
- **Cause of the remaining work:** checking the records turned up defects R1 and R2 (section 3). No fourth acceptance run was required: the acceptance app cannot exercise R1, and R2 is checked in the next measured record.

---

## 3. Defects found and fixed

### Fix-up rounds 1 to 6 (PR #47, merged `854f537`)

Six review rounds against a planted-bug bar; across rounds 4 to 6 the reviewer reported 42 of 42 planted bugs killed. The accepted head was `3befc40` on branch `fix/wo5-acceptance-preflight`.

- **O1, contract auth.** The credential flow relied on a hard-coded registration path. Replaced with a typed top-level `auth:` contract in `proofdeploy.yml`, validated at load (ADR-0010). In the same round the hard-coded SQLite file name became a declared `database:` contract field (kind and path), also validated at load.
- **O2, target process cleanup.** Stopping a target killed the wrapper process only; child servers survived through shell wrappers and kept the port. Targets now run in their own process group (`start_new_session=True`) and are terminated through `os.killpg`; the regression test goes through the real provisioner and asserts the port no longer answers.
- **O3, crash records.** An orchestrator crash after the model call lost the context. Crash records now record INCONCLUSIVE with the cause, the exception type and message, the runner log, and the provisioning and probe results gathered before the crash (failure-cause rule 5).
- **T1 to T5, Addendum 7 test hardening.** T1: a sandboxed end-to-end orchestrator test. T5: the conftest process-leak check scoped to suite processes against a baseline, `server_close()` on all test HTTP servers, and `-W error::ResourceWarning` in the CI invocation. The round also added CLI measure-path tests, leak-check tamper-refusal tests (tampered skill copy, tampered fixture description), and orchestrator record assertions for sandboxed and unsandboxed runs. Two further items landed in these rounds: **L4**, a skill-arm tampered-copy test against the real skill with altered bytes in the bundle; and **O5/O6**, `measured_result` assertions on the crash and clean paths (false on unsandboxed, true on sandboxed).

### Addendum 15: sandbox dependency defects (PR #48, merged `04c05b1`)

- **S1 (blocker).** Dependencies installed at provisioning were gone when the app started. The sandbox now installs under verified lockfile and frozen-install rules: Python venv inside the checkout at `/checkout/.venv`; install with `--require-hashes --no-deps`; venv first on `PATH` for build, migrate, seed, and start; `lockfile_sha256` recorded and verified; `seed` runs. Unsupported ecosystems such as Node fail closed with BUILD_FAILED: "not yet supported in the sandbox". `PIP_NO_CACHE_DIR=1` is set.
- **S2.** App-container stdout and stderr tails are captured into the provision logs on every outcome.
- **S3.** Failed provisions preserve `runtime`, `runtime_version`, `image_digest`, and `sandbox_evidence`.
- **S4.** A sandboxed end-to-end test with a real third-party dependency from a hash-pinned `requirements.txt`, both sides READY, asserting the lockfile hash, probe results, score, and the app log on a forced start failure. It was shown failing on `854f537` and passing after the fix.

### The bridge defect: AF_UNIX path too long (PR #48, merged `04c05b1`)

Round 7 landed S1 to S3 at `424392d`, but the S4 happy path still failed: the `none+socket` bridge never created its socket. GitHub runners report `isolated_network: false`, so every measured run depends on this bridge (Addendum 16). Round 8 attempted the diagnosis through CI iteration; after six consecutive failed `sandbox-tests` runs, Master ordered a stop and freeze. The reviewer reproduced the failure locally (podman 4.9.3, the same version and uid remap as the GitHub runners) and found the root cause (Addendum 17):

- **Root cause:** `OSError: AF_UNIX path too long`. The bridge worked and created the socket within 0.5 seconds, but the host-side path to `.pd-app.sock` inside the checkout was 114 characters locally and 121 characters in CI; Linux limits AF_UNIX paths to 107 bytes, so every connect failed. `_wait_for_socket` swallowed the `OSError` and reported a false "bridge script failed to start".
- **Fix:** the socket lives in a short per-run directory (`/tmp/pd-XXXXXX/app.sock`), mounted into the container at `/pd-sock`; provisioning fails closed if the socket path exceeds 100 bytes; the socket directory is cleaned up on every exit path; `_wait_for_socket` records the last connect error and failure messages report it; readiness distinguishes bridge-down from app-down. Addendum 16 item B3 also landed: `PD_REQUIRE_SANDBOX=1` in the workflow, a conftest hook that turns sandbox skips into failures, and a CI step that re-runs the key sandboxed tests and fails on any skip. Landed at `e45de3c`; CI run 38140492909 passed with the full suite at 406 passed and zero sandbox skips.
- **Round 9 (Addendum 18), tests only.** A mutation run on the new code killed 2 of 6 planted bugs; the four survivors (SB3 to SB6) and a real bridge regression test became required tests. The earlier regression test was tautological (it called `mkdtemp` itself, never touched `Sandbox`, and passed on the broken code); it was replaced with a podman-free test driving the real `_start_none_socket` with a checkout path over 107 bytes. SB3: a non-`requirements.txt` lockfile gives BUILD_FAILED "not yet supported in the sandbox". SB4: the concrete connect error reaches the failure message. SB5: the socket directory is removed after `stop()` and after a failed start. SB6: a hash-pinned install uses `--require-hashes`, and a tampered hash fails the install. Round 9 landed at `9cb5a22` (CI run 38147237302 passed); the reviewer's re-check confirmed 6 of 6 planted bugs killed and the full suite at 412 passed, 0 skipped, 0 failed on their host.

### WO-5.1: executor comparison defects (PR #49, merged `eb2f083`)

- **E1.** `body equals` compared raw text against a JSON value: `_check_assertion` did `body == assertion["equals"]`, so `{"type": "body", "equals": {}}` FAILed against a body of exactly `{}`. Fixed: with a non-string expected value, the body is parsed as JSON and values are compared; a body that is not valid JSON gives FAIL with that detail; a string expected value compares the raw text exactly.
- **E2.** Captured values lost their JSON type: setup captured `id` as the number `1`, `${ITEM_ID}` substituted the string `"1"`, and `1 == "1"` FAILed on both sides. Fixed: captures keep their JSON type; a value that is exactly `${NAME}` substitutes the typed value, and a placeholder inside a larger string substitutes its text form.
- ADR-0011 records the comparison semantics, consistent with the registered prompt; no prompt change. A replay test feeds run 2's probes and recorded responses through the fixed executor: probe 0 PASSes on both sides; probe 1 PASSes at fix^ and FAILs at fix. The reviewer's mutation run on the comparison code: 5 of 5 planted bugs caught (X1 to X5). Landed at `5d15146`.
- **B1, the redaction regression.** E2's typed captures broke redaction: `collect_secret_values` and `collect_record_secret_values` collected captured values only `if isinstance(v, str)`, so a numeric session ID, OTP, or API key captured during setup was skipped and could appear in plaintext in the evidence record, including in `captured_values` itself. The reviewer reproduced it against a real local server (plaintext on the branch, redacted on `main`). Fixed per Addendum 21: both collectors treat every non-bool `int`/`float` capture as a secret via its text form (`str(v)` for int, `json.dumps(v)` for float), subject to the existing `_MIN_SECRET_LEN`; bools are skipped; the written record contains no plaintext form of a numeric capture anywhere, with the `captured_values` leaf becoming the redaction marker; `parsed_probes` stays verbatim. ADR-0011 gained the line that typed captures keep the redaction guarantee. Addendum 22 accepted WO-5.1: 7 of 7 planted bugs in the fix caught (N1 to N7), 12 of 12 across both WO-5.1 rounds, full suite 428 passed and 0 skipped on the reviewer's host. Landed at `9d6065b`; PR #49 merged as `eb2f083`, its tree byte-identical to the accepted head.

### WO-5.2: record redaction and harness provenance (PR #50, merged `5a2f99f`)

- **R1, repeated capture names leaked (Addendum 23).** When the same capture name appeared in several probes, or on both fix^ and fix, only the first value was redacted; later values were written to the record in plaintext. This is the normal case for any app with login, because each side gets its own session, so the fix side's token always leaked. Root cause: `build_evidence_record` merged per-probe `captured_bindings` into a single name-to-value dict with first-wins semantics, and the redaction set is derived from that merged dict; the same defect existed within a single run, where `SetupContext.run_captures` kept only the latest value of a name captured twice. The defect dates from WO-4. Run 3 was clean only because the acceptance app has no secrets and its IDs are too short to count as secrets. Fixed in round 1 (landed at `4403d54`): the merge accumulates every distinct value per name, both collectors flatten the multi-value form, and an orchestrator test with real login tokens on both sides proves none of the 4 tokens appears in the written records. The reviewer's repro went from 3 of 4 tokens in plaintext on `main` to 0 of 4.
- **R2, records did not name the harness (Addendum 23, completed in Addendum 24).** Round 1 recorded a `proofdeploy_commit` field, but the resolution trusted environment overrides and any enclosing git checkout, could silently be empty, and carried no dirty flag. Round 2 (landed at `13d5635` and `8644bb8`) replaced it with a `provenance.harness` object `{commit, source, dirty, workflow_ref}` computed inside `build_evidence_record`, so every record path (bug, clean, crash) carries it and callers cannot supply or override it. The commit comes from `git rev-parse HEAD` only when the package's parent directory is the repo's toplevel (`git rev-parse --show-toplevel`); a venv inside another checkout records no commit. The `PROOFDEPLOY_COMMIT` override was dropped entirely. `GITHUB_SHA` is a cross-check only, never a source. `dirty` is true when any tracked file differs from `HEAD`. `workflow_ref` is `GITHUB_WORKFLOW_REF`, else `GITHUB_REF`. A measured run refuses to start (fail closed) when the commit is unknown, the tree is dirty, or the cross-check fails; unmeasured dev runs record whatever is true and continue. CI run 38162810076 on the round 2 head: 450 passed, 0 skipped.
- **Round 3 (Addendum 25), tests only.** The reviewer accepted the code but three planted bugs survived: H1 (GITHUB_SHA used as the commit source when the package is not at its own toplevel, exactly the customer-CI case), H6 (caller-supplied provenance with a `harness` key overriding the real one), and H9 (gate call removed from `measure_clean`, untested because the refusal test covered only `measure_bug`). Tests for all three landed, plus stubs so the three pre-existing sandboxed record tests no longer fail on a dirty working tree. Landed at `f1e1d4a` (tests only, 3 files, +300 lines); CI run 38166388787: 454 passed, 0 skipped. Addendum 26 accepted WO-5.2: across its three rounds, every planted bug was caught (M1 to M8, H1 to H10).
- **Merge.** PR #50 merged as `5a2f99f`. Addendum 27 verified the merge: parents `eb2f083` and `f1e1d4a`, tree byte-identical to the accepted `f1e1d4a`, pinned files unchanged.

Across all fix branches, the pinned files never changed: `proofdeploy/model_client.py` at `55b7d93283d511b681f7af24dd78aafc0cc750d652ed965f94fe7a4ab1d28598` and `proofdeploy/author_prompt_v7.md` at `fa72a0e6c4a1fa9fb204986e08c8eef4b11fc13786f41389ecf2f42ea8555cf3`, verified at every accepted head and every merge commit.

---

## 4. Schema changes

- **`schema_version` 1.0.0 to 1.1.0** (WO-5.2 round 2). Two record-format changes required the bump: `captured_values` leaves can now hold a list of distinct values for a repeated capture name (R1), and every record carries the `provenance.harness` object (R2).
- **Backward compatibility is tested:** a record in the 1.0.0 shape (scalar `captured_values`, no harness object) still loads and still re-scores.

---

## 5. Lessons

One line each, recorded from the reviewer's notes across the arc:

- Test apps must exercise real installs: run 1 failed because the acceptance path never installed dependencies the way a real run does.
- A readiness check must carry its last error: the swallowed `OSError` turned a one-line diagnosis (socket path too long) into hours of guessing.
- A test that needs special infrastructure must be shown to have run in CI, not just to exist: the silently skipped sandboxed test hid both S1 and the bridge defect.
- Test the executor with the shapes the prompt teaches, not only the shapes our own tests use: E1 and E2 survived until a model-written assertion went through the real executor.
- Test redaction with repeated capture names on both sides: first-wins collection leaked every later value, including the fix side's token.
- Test environment checks in the hostile layout: a commit resolver that is correct in its own checkout records a customer's commit when the package sits inside the customer's repo.
- Claims need tests behind them: "redaction unaffected" was asserted in a package, was wrong, and pointed at the guard that caused the regression.
- Raw output only: hand-edited CI lines and an unverified "fails on the old code" claim both appeared in reports during this arc and both were caught; verification evidence is pasted verbatim, in both directions.
- When attempts stop converging, stop: after 3 to 4 non-converging `sandbox-tests` failures, freeze, package the evidence, and bring in fresh eyes, instead of burning more CI runs.

---

## 6. Open items carried forward

- **REG-B, the no-credential fixture text.** Carried forward unresolved at closure.
- **REG-A, the Python lock rule** (Addendum 5 item C; WAL-76). Requirements files without hashes currently install with `--no-deps` only, so transitive dependencies are neither pinned nor installed; until the lock rule lands, measured repos must carry fully hashed requirements, as the acceptance app does.
- **Node in the sandbox (WO-7).** Node still fails closed in the sandbox ("not yet supported in the sandbox"); Node support belongs to WO-7.
- One non-blocking note from Addendum 22: a float capture is matched by its `json.dumps` text, so an echo rendered in a different form (`1e+20` against `100000000000000000000`) is not caught by value. Integer IDs and tokens, the realistic case, are exact.
- Process note from run 1: the GitHub `measurement` environment did not pause for approval, and whether required-reviewer enforcement is active remains unverified.
