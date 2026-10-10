# WO-4 Milestone: Runner Run-Pair

**Date:** 2026-10-10
**Branch:** `feat/runner-runpair` (feature head `1806461`, merged as PR #42, merge commit `cf56125fc227100ebf2b4c46c382717aff36bea9`)
**Accepted by:** Orchestrator (Master's other agent) on 2026-10-10
**Work order:** WO-4 from brief 2026-10-10

## What WO-4 delivered

The run-pair runner (`proofdeploy/runpair.py` and supporting modules): fix^ vs fix run pairs and clean-diff runs (C), evidence records, scoring against the registered rule, and a validated judgment gate. (B^→B is only the author's diff, supplied to the model through the assembler; the measured run pair is fix^ vs fix.)

### Review history

- `d8a277a` — WO-4 initial review fixes: exact scoring, value redaction, commit-enforced records
- `c961ecc` — re-review fixes: fail-closed redaction, evidence-computed scores, validated judgments
- `1806461` — third review fixes: executor-carried secrets, judge allowlist (the accepted head)

The branch was based on `d2b2b9e`, one docs-only merge behind `main` (the WO-3 milestone, PR #41). The branch touched no docs, so it merged cleanly; branch protection required the head to be current, so `main` was merged into the feature branch as `ae9c02f` before the PR merge. The four accepted feature commits are byte-identical.

### Behaviors the orchestrator verified

At `c961ecc` and again at the accepted head:

1. **Scoring matches the registered rule.** `docs/evaluation/scoring-rule-run-pair-2026-10-09.md` (plus the start-failure addendum `025222d`) is the only rule; the unregistered `v1-all-probes` rule is gone.
2. **Probes stay verbatim and hash-valid.** Parsed probes are stored exactly as run; hashes validate.
3. **Evidence is committed before scoring.** `write_score_record` writes the score record but does not commit it; committing the score is the orchestrator's job (WO-5). The committed score is tied to the committed evidence.
4. **Clean diffs use explicit fields.** `c_sha`, `c_provision`, and `c_results` are explicit, never inferred from bug-run fields.
5. **Redaction needs no caller help.** Fixture, contract-env, captured, bearer, JWT, and minted values are redacted by value. Ordinary-named fixture and contract values are caught too.
6. **Scores are recomputed from committed evidence.** The scorer reads the committed record, not the live run objects.
7. **Verdict stays `candidate_catch` until judgment.** The score never claims a final catch; only a valid judgment produces one.
8. **Judgments on non-candidate probes are refused.**

### Third-review fixes (the accepted head)

1. **Executor-carried secrets.** `ProbeResult` carries captured values and minted tokens (without serializing helper fields), and `build_evidence_record` gathers them automatically. The required test: a captured setup token echoed by the act has zero plaintext occurrences in the record, with the caller passing no redaction values at all.
2. **Judge gate is a role allowlist.** Fixed role set; bug runs require the `reviewer` role and a non-empty judge; the judgment's evidence ID and commit must match the score's evidence.

### The judge-identity rule (design decision, read carefully)

The gate checks the **role**, not the judge **name**. A judge calling itself "Wally" with the `reviewer` role is accepted, and that is by design: a name typed into a record cannot prove who someone is. Who judged is proven by **who commits the judgment**. On measured runs only the reviewer writes judgments, and the orchestrator checks the commit author when it verifies. That rule is in the state doc.

Future packets must report what the code does. Do not claim "the Wally bypass is blocked" — the test suite documents that a `Wally`/`reviewer` judgment is accepted, and the orchestrator accepted this as the correct design.

## Real-run evidence

### Orchestrator's verification runs

The orchestrator re-ran both repros against live servers at the accepted head, with the caller passing no secrets. Nothing leaked: not the session token, either fixture key, or the minted Ghost token. All 253 tests passed in three full runs, and the flaky snapshot test (`test_create_snapshot_feeds_bundle_with_recorded_sha`, Plane WAL-74) did not recur in those three runs. WAL-74 stays open: the order-dependent failure was seen 1-in-2 earlier and is not proven fixed, only not observed.

### Wally's verification runs

Real throwaway-app records were generated under `c961ecc` and held as the packet source:

- `wo4-bug-runs.jsonl` — run-pair records (fix^ vs fix) with committed evidence, scores, and verdicts
- `wo4-clean-runs.jsonl` — clean-diff records with explicit `c_*` fields

The review packet (`wo4-review-packet.md`) drew its identifiers from those records. One warning carried forward: the packet's handwritten header cited the wrong `fix^` SHA; future packets must generate identifiers from record data and current test output, never from memory.

## Tests

- **253 tests pass** in three consecutive full runs; 86.57% coverage (floor 80%). Ruff check and mypy clean.
- The executor-carried-secrets test and the judge-allowlist tests are in the suite.

## Handoff to WO-5

The orchestrator's done-list for WO-5 (orchestrator, model client, leak check):

- commit every score with `commit_records`;
- call `cleanup_run` for every provisioned instance;
- **never write judgment records**: the orchestrator stops at the score, and judgments come only from the reviewer.
