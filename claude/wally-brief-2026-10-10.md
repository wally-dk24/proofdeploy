# WO-5 Brief: Orchestrator (scope correction)

**Date:** 2026-10-10
**Source:** ProofDeploy orchestrator (Master's other agent), scope correction issued 2026-10-10.
**Status:** This file is the canonical WO-5 work order. Worker instructions for WO-5 must quote this file, not paraphrase it.

## Scope correction (quoted verbatim from the orchestrator)

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
