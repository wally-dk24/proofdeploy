# The versioned result contract — record format at `SCHEMA_VERSION` 1.5.0

Plan line 1.4(a) (D23). This document is the prose contract for the
records ProofDeploy writes for every measured run. The machine-readable
form is [`result-record.schema.json`](result-record.schema.json) beside
it; the validation test in `tests/test_runpair.py`
(`test_result_contract_*`) proves the committed records and the records
the code's own builders produce conform to it.

Everything here describes what the code **does today** at
`SCHEMA_VERSION = "1.5.0"` (`proofdeploy/probe.py`), as implemented by
`proofdeploy/runpair.py` (record builders, redaction, log writes) and
`proofdeploy/executor.py` (per-probe results). Where the code is
ambiguous, this document says so under
[Ambiguities](#ambiguities-flagged-not-resolved) instead of inventing
semantics. Nothing in this contract changes any behavior.

## Scope

- **In scope:** the three record types appended to `runs.jsonl` —
  `evidence`, `score`, `judgment` — and the per-probe result objects
  embedded in evidence records.
- **Out of scope:** the probe schema itself (a separate contract,
  `proofdeploy/probe.py`); provisioning result objects embedded in
  evidence records (the provisioning contract); the in-memory run
  summaries the orchestrator returns to its caller (they carry keys like
  `verdict`/`counting` but are never written as records); readiness and
  container-spike rows (different harnesses, different files); and the
  legacy `docs/evaluation/bottle-rerun-2026-10-09.jsonl` artifact, whose
  `author_run`/`run_summary` lines predate this format, carry no
  `schema_version`, and are produced by no current code.

## Storage and lifecycle

- Records are JSON objects, one per line, appended to `runs.jsonl` in
  the run's output directory (compact separators, UTF-8). The log is
  **append-only**: every record gets a unique `record_id`
  (`uuid.uuid4().hex`, 32 lowercase hex chars) and repeats never
  overwrite.
- Writing is also a git protocol. The **evidence record is written and
  committed before any scoring**. Scoring reads the evidence back *at
  that commit* and writes the **score record**, which the caller commits
  in a **later commit** that cites the evidence commit. A **judgment
  record**, when one is made, is appended after the score it cites and
  committed separately by the reviewer.
- Every record is **secret-redacted at write time**
  (`redact_record`, see [Redaction](#redaction-guarantee)) before it is
  appended.

## Common fields

Every record carries:

| Field | Type | Meaning |
|---|---|---|
| `record_id` | string (32 hex) | Unique id minted by the builder. |
| `timestamp` | string | UTC ISO-8601, `datetime.now(timezone.utc).isoformat()`. |
| `record_type` | string | `"evidence"`, `"score"`, or `"judgment"`. |
| `schema_version` | string | The record format version; `"1.5.0"` at this contract. Also stamped on every per-probe result. |
| `run_kind` | string | `"bug"` or `"clean"`. |
| `bug_id` | string \| null | Set on bug runs, null on clean runs. |
| `clean_id` | string \| null | Set on clean runs, null on bug runs. |

## Verdicts and typed reasons

### Per-probe verdicts (`proofdeploy/executor.py`)

A per-probe result's `verdict` is one of `pass`, `fail`, `inconclusive`.
An inconclusive verdict always carries a typed `reason`
(`InconclusiveReason`); `reason` is null otherwise:

| Reason | Meaning |
|---|---|
| `environment` | The target, database, or setup could not be evaluated. The only reason class an admin may downgrade to "warn". |
| `auth` | A credential was missing or expired (fixture/auth flow). Never downgradable. |
| `drift` | The target API shape drifted from what the probe expects. |
| `probe` | The probe itself is defective. Never downgradable. |

### Pair scoring (score record)

Scoring implements the registered run-pair rule; the only scoring
version is `v2-per-probe` (`scoring_version`), and unknown versions
raise.

- **Bug runs** score `verdict` as `candidate_catch`, `no_catch`, or
  `inconclusive`, plus `candidate_catch`: the indices of **every** probe
  that went FAIL on fix^ and PASS on fix. The stored verdict is
  `candidate_catch`, **never `catch`** — only a judgment record makes a
  catch.
- A pair is INCONCLUSIVE only when a side's provisioning is not READY
  (no results), or when every probe on a side is INCONCLUSIVE with
  reason `environment`. Schema-invalid probes (reason `probe`) are a
  miss (`no_catch`), not infrastructure. A sibling probe that is
  INCONCLUSIVE, or FAILs on both sides, does not cancel a catch.
- **Clean runs** are strict — probes run against C only — and score
  `false_alarm` (bool) with `verdict` null: any FAIL or INCONCLUSIVE on
  a clean diff is a false alarm.

### Judgments

A judgment record is the reviewer's condition-3 decision ("targets
behavior the fix actually changed"), made from the probe against the
fix diff. Its `verdict` is `catch` when `condition3_met` is true for the
cited probe, else `no_catch`. Validation at write time enforces:

- the cited `probe_index` is in the score's `candidate_catch`;
- the judgment's `evidence_record_id` and `evidence_commit_sha` match
  the score's;
- `condition3_met` is a bool;
- `judge_role` is in the fixed allowlist `reviewer`, `implementer`,
  `auditor` — and on measured (bug) runs the role must be `reviewer`
  and `judge` must be non-empty: the implementer cannot judge its own
  catch.

## The evidence record

Built by `build_evidence_record()`; 55 keys, **all always present** —
fields that do not apply to a run are null or empty, never absent.
`verdict` is always **null** on an evidence record: verdicts live on
score and judgment records.

### Run naming — the exact commits under test

| Field | Meaning |
|---|---|
| `fix_sha` / `fix_parent_sha` | The fix commit in the target repo, and its parent (fix^). The run pair executes the same probes against fix^ first (bug should be present → probes FAIL), then fix (bug should be fixed → probes PASS). |
| `b_sha` / `b_parent_sha` | The bug-introducing commit B and its parent, resolved separately from fix^ and recorded for provenance; the pair comparison does not use them. |
| `c_sha` | The clean-diff commit C. Clean runs use the `c_*` fields and never the `fix_*` ones. |

### Authoring

| Field | Meaning |
|---|---|
| `prompt` / `prompt_sha256` | The exact prompt sent, and its SHA-256 (hex). Changing prompt composition changes the measurement. |
| `raw_model_response` | The model's raw response, verbatim. |
| `parsed_probes` | The parsed probes. Stored **verbatim and never redacted** (see [Redaction](#redaction-guarantee)). |
| `parsed_probes_sha256` | SHA-256 of the canonical JSON of `parsed_probes`; must still match the stored probes. |
| `invalid_probe_blocks` | Author blocks that failed validation, recorded verbatim as `{block_index, raw_text, rejection_reason}`. Never sent as probes. |
| `bundle_manifest` / `bundle_manifest_sha256` | The author bundle manifest (skill hash or no-skill marker) and its hash. |
| `skill_sha256` | The skill's hash; null for no-skill baseline runs. |
| `model_id`, `model_temperature`, `model_attempts`, `model_call_attempts` | The model and its call accounting; `model_call_attempts` logs each attempt's outcome for INCONCLUSIVE runs. |
| `model_request` | The exact model request as sent: model, temperature, per-message hashes, and the seed/tools fields as sent. |
| `author_prompt_sha256` | SHA-256 of the frozen author-prompt template that built the prompt. |
| `author_prompt_builder_sha256` | SHA-256 of the prompt builder module (`proofdeploy/model_client.py`), pinning the exact rendering code beside the template hash. |
| `truncation_flags` | Which prompt-construction caps applied (diff files, file bytes, listed files). |

### Provisioning and results

| Field | Meaning |
|---|---|
| `fix_parent_provision`, `fix_provision`, `c_provision` | The provisioning result per side (logs, runtime versions, lockfile hashes, and — at 1.5.0 — the Python lock facts and build-backend facts the provisioner records). Their inner shape is the provisioning contract, not this one. |
| `fix_parent_results`, `fix_results`, `c_results` | The per-probe results per side (below). |
| `runner_log` | The runner's log lines. |
| `sandbox_evidence` | Isolation actually in effect per side (network mode, userns mode, uid the app ran as); empty when the sandbox was not used. |
| `write_grants` | Per side `{provisioned, target_url, network_mode}`: writes were allowed on a side iff its executor was provisioned. Non-scoring audit data. |
| `sandboxed` | Whether the measured sides ran inside the sandbox; false only via the explicit dev flag. |
| `measured_result` | Whether this record counts as a measured result. An unsandboxed dev run is marked false and must never be treated as one. |

### Secrets-bearing fields (stored redacted)

| Field | Meaning |
|---|---|
| `fixture`, `contract_env` | The fixture and contract-env values used by the run. |
| `public_fixture_keys`, `public_contract_env_keys` | The keys explicitly declared public; every other fixture/env value is treated as a secret for redaction (fail-closed). |
| `captured_values` | Values captured during the run. A name bound once maps to its value; a name bound more than once (several probes, both sides, a re-login) maps to the **list of its distinct values**, so no value is dropped before redaction. Captures keep their JSON type; numeric captures are collected in text form. |
| `minted_tokens` | Every token minted by the credential provider during the run. |
| `auth_bootstrap` | Per-side auth bootstrap summary: `{"fix": {"generated": ["ADMIN_EMAIL", "ADMIN_PASSWORD"]}}` — env **names** recorded as `"generated"`, never their values (see [Credentials](#credential-handling-rule-9--d8)). |

### Provenance

`provenance` merges caller provenance (Actions run URL/ID, runner
image, capability probe output) with a computed `harness` object that
is always present and never caller-supplied, so every record path (bug,
clean, crash, inconclusive) carries it:

| Field | Meaning |
|---|---|
| `provenance.harness.commit` | `git rev-parse HEAD` of the ProofDeploy checkout that produced the record — taken **only** when the package's parent directory is that repo's toplevel. No environment variable supplies or overrides it (`GITHUB_SHA` is only a cross-check for the measurement gate, never a source). Null when no commit is proven. |
| `provenance.harness.source` | `"git"` when the commit came from git, else null. |
| `provenance.harness.dirty` | True when any tracked file differs from HEAD; untracked files do not count. |
| `provenance.harness.workflow_ref` | `GITHUB_WORKFLOW_REF`, or `GITHUB_REF` when absent; null outside GitHub Actions. |
| `harness_version` | `HARNESS_VERSION` — the **probe-schema** harness version, a different version from `schema_version`. |

### Crash records

When the orchestrator crashes after authoring, the evidence record is
still written, with `inconclusive_cause` (the typed cause —
`tool_failure` for a failure in ProofDeploy itself, reason class
`environment`), `inconclusive_error`, `crash_provisioning`, and
`crash_probe_results` preserving everything gathered before the crash.
The cause is stated, never lost; the run then scores INCONCLUSIVE as
usual.

## The per-probe result

Serialized by `ProbeResult.to_dict()`; 14 keys, all always present, and
each carries its own `schema_version`.

| Field | Meaning |
|---|---|
| `probe_index` | Index into `parsed_probes`. |
| `verdict` / `reason` | As [above](#verdicts-and-typed-reasons). |
| `request` | The act request actually sent — `{method, url, headers, body}` — or null when no request was sent (fail-closed before it). |
| `setup_requests` | Every setup-step request actually sent, in order, same shape; null when the executor left it unset. |
| `http_status` | The response status (3xx is evaluated as-is; redirects are never followed), or null. |
| `http_headers` | The response headers (names lowercased). |
| `http_body` | The response body, **capped at 2000 characters**. |
| `http_body_truncated` | True when the body hit the cap; a truncated body is not replayable. |
| `writes` | Every write request made, logged (method, URL, status). |
| `details` | Human-readable evaluation log lines. |
| `sent` | Whether the probe was actually sent to the target. Invalid author blocks are never sent but are still reported individually as INCONCLUSIVE `probe`. |
| `assertions_evaluated` | Whether assertion evaluation actually ran. Every early return leaves it false: an offline replay must **not** re-decide such a probe — replay carries the recorded verdict and reason instead of inventing one. |

## The score record

14 keys, all always present: the common fields plus
`evidence_record_id`, `evidence_commit_sha` (the git commit carrying
the cited evidence record), `verdict`, `candidate_catch`,
`false_alarm`, `scoring_version`, and `note`.

Scoring never trusts the caller: `write_score_record` refuses
(`EvidenceNotCommittedError`) unless the evidence record is in a
commit, reads it back at that commit, rebuilds the per-probe results,
and refuses (`ScoreMismatchError`) any caller-passed verdict,
`candidate_catch`, `false_alarm`, `bug_id`, `clean_id`, or `run_kind`
that disagrees with the evidence.

## The judgment record

16 keys, all always present: the common fields plus
`evidence_record_id`, `score_record_id`, `evidence_commit_sha`,
`probe_index`, `judge`, `judge_role`, `condition3_met`, `verdict`
(`catch` / `no_catch`), and `note`. The validation rules are listed
under [Judgments](#judgments).

## Replay fields

A record is replayable because the evidence record holds, together:

- the probe set verbatim (`parsed_probes` + `parsed_probes_sha256`) and
  the invalid blocks with their rejection reasons;
- the full authoring context: `prompt` + `prompt_sha256`,
  `raw_model_response`, `model_request`, `author_prompt_sha256`,
  `author_prompt_builder_sha256`, `bundle_manifest` (+ hash),
  `skill_sha256`, `truncation_flags`;
- per side: the provisioning result (logs, runtime versions, lockfile
  hashes) and every per-probe result, each with its recorded act and
  setup requests, response status/headers/body, and the
  `sent` / `assertions_evaluated` / `http_body_truncated` flags that
  tell replay when it may re-decide and when it must carry the recorded
  verdict;
- the exact commits under test (fix/fix^/B/B^/C) and the exact
  ProofDeploy commit (`provenance.harness`);
- and the evidence is read back **at its git commit**
  (`evidence_commit_sha`), so scoring and replay act on the committed
  bytes, not on a caller's copy.

Offline replay itself is `proofdeploy/replay.py`; this contract only
fixes the fields it consumes.

## Redaction guarantee

Records are redacted **by value, fail-closed**, before they are
appended (`redact_record` / `SecretRedactor` in
`proofdeploy/runpair.py`):

- **Secret collection.** Every fixture and contract-env value is
  secret unless its key is explicitly declared public in the record
  (`public_fixture_keys` / `public_contract_env_keys`). Captured values
  and minted tokens are always secret. Per-run bootstrap credential
  values join the same pool (below). Values shorter than 4 characters
  are not collected.
- **By-value replacement.** Any occurrence of a collected value, in
  any string anywhere in the record, is replaced with
  `***REDACTED***`. A numeric leaf whose text form is a collected
  secret (a typed capture) becomes the marker too — no plaintext form
  survives anywhere.
- **Key-name backstop.** Values under keys whose names match the
  secret-key patterns (e.g. `password`, `secret`, `api_key`,
  `private_key`, anything ending in `token`, case- and
  separator-insensitive) are replaced wholesale, even if the value was
  never collected.
- **Pattern backstops.** JWT-shaped strings become
  `***REDACTED_JWT***`; `Bearer <token>` becomes
  `Bearer ***REDACTED***`. Authorization headers (`Authorization`,
  `Proxy-Authorization`) keep their scheme — the scheme is structure,
  the credential is secret.
- **The one exception.** `parsed_probes` is stored **verbatim and is
  never redacted**: placeholders like `Bearer ${USER_TOKEN}` and
  capture specs are structure, not secrets, and the stored
  `parsed_probes_sha256` must still match.

## Credential handling (rule 9 / D8)

Rule 9 means **customer secrets**: they stay out of every container and
every record. The credentials ProofDeploy itself generates per run are
handled under D8's five conditions; in record terms:

- bootstrap credential **values** are collected into the redaction pool
  exactly like minted tokens, so a bootstrap value or read admin key is
  redacted from every record field — provision logs included — even on
  paths where no probe ran to carry it;
- the record names them **only as `"generated"`**: `auth_bootstrap`
  holds, per side, the sorted env **names** (`{"generated": [...]}`),
  never their values (Addendum 43 condition 4);
- nothing about them is ever written to a committed file in any other
  form: in the record they appear as redaction markers or as the
  `"generated"` name list, nowhere else.

## Versioning and bump rules

- `SCHEMA_VERSION` lives in `proofdeploy/probe.py` and is stamped on
  every record and every per-probe result. **Bump it when the record
  format changes.** `HARNESS_VERSION` (probe schema) and
  `SCORING_VERSION` (scoring rule) are separate versions with their own
  bump rules; neither is the record version.
- A reader may assume a record stamped `1.5.0` has exactly the field
  sets in the JSON Schema: builders emit fixed key sets, and fields
  that do not apply are null/empty rather than absent.
- **History** (from the comments at the constant; all changes were
  non-scoring field additions unless noted):

  | Version | Change |
  |---|---|
  | 1.0.0 | The original format (scalar `captured_values`, no harness provenance). |
  | 1.1.0 | `captured_values` may hold a list of values per name (Addendum 23 R1); evidence records carry `provenance.harness` (Addendum 24). |
  | 1.2.0 | Per-probe results carry `http_body_truncated` (Addendum 36). |
  | 1.3.0 | Evidence records carry `write_grants` and the resolved network mode in provenance (WO-7.3 / Addendum 40). |
  | 1.4.0 | Two additions under one minor: the Python lock facts in each side's provision (WO-8.1 / REG-A) and `auth_bootstrap` (WO-8.3). |
  | 1.5.0 | **No field changes** (Addendum 62): the bump separates the field sets that accumulated under 1.4.0 (lock facts, build-backend facts, `auth_bootstrap`), so one version number never covers two sets again. |

- **Reader compatibility, as the code behaves today:** readers default
  absent keys (`ProbeResult.from_dict` defaults `sent` to true and
  `assertions_evaluated` to false, and the loaders default the rest),
  so records at 1.0.0–1.4.0 still load and re-score. A format change
  that alters the **meaning** of an existing field, rather than adding
  fields with defaults, is the kind of change this defaulting cannot
  absorb — it needs a bump and, per the registered-rule discipline, a
  registration decision, not a silent edit.

## Ambiguities flagged, not resolved

These are places where the code or the older documents do not pin a
single reading. This contract documents them; resolving them is future
work, not this PR.

1. **"Readers must refuse unknown versions" is stated but not
   implemented.** The `runpair.py` module docstring says readers must
   refuse unknown versions; no read path in the code checks
   `schema_version` at all — readers accept records by defaulting
   absent keys, including versions they do not know. The docstring
   states the intended rule; the code does not enforce it.
2. **Older documents name stale versions.** The `runpair.py` module
   docstring says `schema_version` is "currently `1.3.0`" and
   ADR-0009's schema-version section says `"1.0.0"`. The constant in
   `proofdeploy/probe.py` is authoritative; both texts lag it.
3. **`crash_probe_results` has no pinned shape.** The builder parameter
   is typed `Any` and the crash path passes whatever per-probe results
   were gathered before the crash; the schema therefore leaves the
   field's type unconstrained.
4. **Free-form embedded objects.** The inner shapes of the provisioning
   results, `bundle_manifest`, `model_request`, `fixture`,
   `contract_env`, `captured_values`, `sandbox_evidence`,
   `write_grants`, and `truncation_flags` belong to other contracts
   (provisioning, the author bundle, the fixture format); this contract
   fixes only that they are JSON objects. `model_request`'s contents
   are described by its builder comment ("model, temperature,
   per-message hashes, seed/tools as sent"), not by a schema.

## The schema and its test

- [`result-record.schema.json`](result-record.schema.json) — JSON
  Schema (draft 2020-12) for the three record types at 1.5.0. Where a
  builder guarantees a value by construction, the schema pins it:
  `record_id` is 32 lowercase hex chars (uuid4 hex);
  `prompt_sha256`/`parsed_probes_sha256` are 64-hex digests computed
  inline by the builder; fixed key sets are `additionalProperties:
  false`. Free-form embedded objects are typed as objects and no more.
- The repo's dependency set has **no JSON Schema library** (runtime:
  `pyyaml` only), so the validation test implements the subset of JSON
  Schema the schema file uses — `type`, `enum`, `const`, `required`,
  `properties`, `additionalProperties`, `items`, `pattern`, `oneOf`,
  `anyOf`, `$defs`/`$ref` — with the standard library only. All other
  keywords in the schema file are annotations the validator ignores.
  If the schema grows beyond that subset, the test's validator must
  grow with it (or the dependency decision must be revisited).
- `tests/test_runpair.py::test_result_contract_*` validates: records
  produced by the real builders (bug, clean, and crash evidence; bug
  and clean scores; judgments) after the same redaction pass the write
  path applies; every committed record in the repo that declares this
  format (a `record_type` in the family and a `schema_version`); and
  that the schema's version constant equals the code's
  `SCHEMA_VERSION`.
