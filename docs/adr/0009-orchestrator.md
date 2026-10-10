# ADR 0009: Measured-Unit Orchestrator

**Date:** 2026-10-10
**Status:** Accepted (scope-corrected 2026-10-10 per the orchestrator's addendum)

## Context

WO-1 (provisioner), WO-2 (executor), WO-3 (setup/auth), and WO-4
(run-pair runner, scoring, judgment gate) are merged. WO-5 must wire
them into one command that runs a measured unit end to end: build the
blind author's bundle, call the registered model, validate the probes,
provision fix^ and fix (bugs) or C (clean diffs), execute, record, and
score.

The WO-5 brief (agreed text at `~/workspace/your_files/wally-brief-2026-10-10.md`,
plus the orchestrator's addendum) requires:

> **A leak check runs before every model call.** It fails the run if the bundle contains anything outside the allowlist: the fix SHA, any later commit's content, fix tests, eval files or a `.git` directory. The bundle manifest hash goes in the record.

> **Unattended runs:** rootless container mounting only the checkout, network cut after install, no secrets in the container environment. This must be in place before any unattended run.

> Put a `schema_version` on every record and every per-probe result, and document it next to the record format. (Addendum item 8.)

Out of WO-5: the blind real-app run and the Ghost go/no-go (WO-6); no CLI result contract, no exit codes.

Four design questions needed answers:

1. **How does the blind author see the snapshot?** The bundle is a
   directory on disk, but the registered model is a chat API. In the
   skill shape (ADR-0004) an agent reads the bundle files itself; the
   model client must serialize the bundle into the prompt.
2. **How is the author kept blind?** The brief requires a fail-closed
   ANSWER-KEY check (not just a secrets check): the bundle must hold
   only the allowlist. A separate "zero secret values" check runs as
   an extra.
3. **How is `harness_app` provisioned?** WO-3/WO-4 left it as
   INCONCLUSIVE (environment) "not yet available (WO-5)". Dev-set
   library repos need the author's harness app actually running.
4. **How are unattended runs sandboxed?** The app must run
   de-privileged, with only the checkout mounted and no external
   network after install.

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

Two checks run fail-closed BEFORE every model call:

1. **Answer-key check (required).** `check_bundle_answer_key` verifies
   the bundle holds only the allowlist. It raises `AnswerKeyLeakError`
   (refusing the run) if the bundle contains the fix SHA, the B->fix
   diff (later commit's content), a `.git` directory, or smuggled
   eval files; it also verifies the manifest's `source_sha` is B and
   its `diff_range` is B^..B. It returns the bundle manifest hash,
   which goes in the evidence record (`bundle_manifest_sha256`).
2. **Secret check (extra).** `collect_forbidden_values` gathers every
   fixture value whose key is not public (the five template fields are
   public by design), every contract env value whose key is not
   declared public, plus caller-supplied extra secrets.
   `assert_no_secrets` scans the prompt and every bundle file
   (including every snapshot file) and raises `SecretLeakError` on any
   occurrence. Values shorter than 8 characters are ignored to avoid
   false positives on ordinary words; the record's fail-closed
   redaction still covers every value regardless of length when the
   record is stored.

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

### Sandbox (`proofdeploy/sandbox.py`)

For unattended runs (`--sandbox`), the app is provisioned in a
de-privileged container:

- the workload runs as UID 65534 (nobody), with `CAP_DROP=ALL` and
  `no-new-privileges`; it never runs as root;
- only the checkout (the snapshot directory) is mounted, at
  `/checkout`; nothing else from the host enters;
- install runs WITH network (proxy + platform CA for pip/npm); the
  app runs WITHOUT any proxy variables, and direct egress is blocked
  in this environment, so the running app has no external network
  while the host executor still reaches it on 127.0.0.1;
- no secrets enter the container environment: only declared-public
  contract config is passed; auth tokens travel over HTTP.

### Schema version

Every record (evidence, score, judgment) and every per-probe result
carries `schema_version` (`"1.0.0"`, defined in `proofdeploy/probe.py`
as `SCHEMA_VERSION`). It is documented next to the record format in
`proofdeploy/runpair.py`. Bump it when the record format changes.

## Consequences

- `proofdeploy measure --repo ... --bug-id ... --base ... --bug ... --fix ...`
  (or `--clean-id`/`--clean`) is the one command for a measured unit;
  `--sandbox` enables the de-privileged container backend.
- `harness_app` is real: dev-set library repos can now run end to end.
- The prompt composition is a measurement choice and is recorded in
  the evidence record (`prompt`, `prompt_sha256`); changing it changes
  the measurement.
- The 8-character minimum in the secret check is a heuristic; short
  secrets are still redacted at record time, just not
  pre-authoring-checked.
- The answer-key check is the primary blindness guard; the secret
  check is defense in depth.

## Alternatives considered

- **Inlining the whole snapshot:** rejected; context-bounded and
  unnecessary. The diff-touched files are what the author must probe.
- **Authoring before vs after provisioning:** authoring first, so no
  live secret exists yet at model-call time. This makes the leak
  check meaningful rather than vacuous.
- **A generic `${AUTH_TOKEN}` seeding protocol in proofdeploy.yml:**
  rejected for WO-5; open registration plus runner-side registration
  covers the measured apps without a contract change.
