# ADR 0009: Measured-Unit Orchestrator

**Date:** 2026-10-10
**Status:** Accepted

## Context

WO-1 (provisioner), WO-2 (executor), WO-3 (setup/auth), and WO-4
(run-pair runner, scoring, judgment gate) are merged. WO-5 must wire
them into one command that runs a measured unit end to end: build the
blind author's bundle, call the registered model, validate the probes,
provision both sides, execute, record, and score.

Three design questions needed answers:

1. **How does the blind author see the snapshot?** The bundle is a
   directory on disk, but the registered model is a chat API. In the
   skill shape (ADR-0004) an agent reads the bundle files itself; the
   model client must serialize the bundle into the prompt.
2. **How is the author kept blind?** The task requires a fail-closed
   leak check: no fixture keys, session tokens, minted tokens, or
   contract secrets in the bundle or prompt.
3. **How is `harness_app` provisioned?** WO-3/WO-4 left it as
   INCONCLUSIVE (environment) "not yet available (WO-5)". Dev-set
   library repos need the author's harness app actually running.

## Decision

### Orchestrator (`proofdeploy/orchestrator.py`)

`Orchestrator.measure_bug()` / `.measure_clean()` run the seven
steps in order: (a) assemble the author bundle from the B^->B diff +
B snapshot via `fixture.assemble_author_bundle`; (b) call the model
once; (c) parse and validate probes with `probe.ProbeSet`;
(d) provision fix^ and fix (or C) with `runner.Provisioner`;
(e) execute the probes with `executor.Executor`; (f) write the
evidence record and commit it; (g) score from the committed evidence
and commit the score with `runpair.commit_records`.

Hard rules, enforced in code rather than by convention:

- `cleanup_run` is called for every READY provisioned instance in a
  `finally` block covering every exit path.
- The orchestrator never writes judgment records. The stored score
  verdict stays `candidate_catch`; only a reviewer judgment (written
  elsewhere) can make it a catch.
- A non-READY side is still recorded and scored: the registered rule
  maps it to infrastructure INCONCLUSIVE. There is always an evidence
  record before a score.
- If the model produces no valid probes, the run aborts with
  `OrchestratorError` and nothing is scored or committed: there is no
  evidence to score.

### Model client (`proofdeploy/model_client.py`)

The registered configuration is fixed in code, not parameters: model
`openai/gpt-oss-120b` via Groq, temperature 0.2, exactly one attempt,
no seed, no tools, no web. `call_model` takes an injectable runner so
tests can substitute a canned response; the default runner shells out
to the Groq skill CLI, which attaches the stored credential and sets
the User-Agent Groq's WAF requires.

The prompt is built deterministically from the bundle alone: the
output contract (fenced ```json probe schema), the registered skill
text, the fixture description, the diff, the snapshot file list, and
the full content of every file the diff touches (capped). Snapshot
files the diff does not touch are listed but not inlined. Large repos
will need retrieval instead of inlining; that is future work and is
not attempted here.

### Leak check (`proofdeploy/leakcheck.py`)

`collect_forbidden_values` gathers every fixture value whose key is
not public (the five template fields are public by design), every
contract env value whose key is not declared public, plus
caller-supplied extra secrets. `assert_no_secrets` scans the prompt
and every bundle file (including every snapshot file) and raises
`SecretLeakError` on any occurrence, before any model call. Values
shorter than 8 characters are ignored to avoid false positives on
ordinary words; the record's fail-closed redaction still covers every
value regardless of length when the record is stored.

### Harness app (executor)

`Executor` gains an optional `HarnessAppConfig` (workdir,
interpreters, extra env, cwd, startup timeout). A `harness_app` setup
step writes the author's `source` to a file, starts it on a free
loopback port, waits for the port to accept connections, and sets
`SetupContext.target_override`: the rest of that probe run (later
setup steps and the act) targets the harness app. The process is
stopped when the probe run ends, on every exit path, and via
`Executor.close()`.

Only `python` and `node` are provisioned; anything else is
INCONCLUSIVE (reason `probe`). Without a harness config the step is
INCONCLUSIVE (reason `environment`). A process that exits early or
never listens is INCONCLUSIVE (reason `environment`) with the harness
log tail in the details.

The orchestrator creates the runner's disposable credential itself:
after provisioning it registers a random user through the app's own
API and passes the token as the executor's auth token, which is what
`${AUTH_TOKEN}` resolves to. Password and token join the record's
secret collection and never enter the bundle or prompt (authoring
happens before provisioning, so they do not exist yet at author
time).

## Consequences

- `proofdeploy measure --repo ... --bug-id ... --base ... --bug ... --fix ...`
  (or `--clean-id`/`--clean`) is the one command for a measured unit.
- `harness_app` is real: dev-set library repos can now run end to end.
- The prompt composition is a measurement choice and is recorded in
  the evidence record (`prompt`, `prompt_sha256`); changing it changes
  the measurement.
- The 8-character minimum in the leak check is a heuristic; short
  secrets are still redacted at record time, just not
  pre-authoring-checked.

## Alternatives considered

- **Inlining the whole snapshot:** rejected; context-bounded and
  unnecessary. The diff-touched files are what the author must probe.
- **Authoring before vs after provisioning:** authoring first, so no
  live secret exists yet at model-call time. This makes the leak
  check meaningful rather than vacuous.
- **A generic `${AUTH_TOKEN}` seeding protocol in proofdeploy.yml:**
  rejected for WO-5; open registration plus runner-side registration
  covers the measured apps without a contract change.
