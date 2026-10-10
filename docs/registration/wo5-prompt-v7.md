# WO-5 Registration: Author Prompt v7 and Model-Call Failure Rule

> This document registers the author prompt template and the prompt
> builder for WO-5 measured runs. It is append-only: any change to the
> template or the builder requires a new registration before the next
> measured run.

## 1. Author prompt template

- **Path:** `proofdeploy/author_prompt_v7.md`
- **SHA-256:** `fa72a0e6c4a1fa9fb204986e08c8eef4b11fc13786f41389ecf2f42ea8555cf3`
- **Status:** The independent auditor's verdict on rendered prompts is
  "OK to register."

The full verbatim text follows, so this document stands on its own:

```
# Author prompt template v7 (frozen)

> This file is frozen. Any change creates a new version (e.g. `author_prompt_v8.md`)
> which must be registered by Master before any measured run. The SHA-256 of
> this file is recorded in every evidence record as `author_prompt_sha256`.

> The `## Body` section below is the whole prompt skeleton: every section
> heading, its order, and every label comes from this file. The builder only
> substitutes the `[[...]]` placeholders with bundle content. No heading or
> label is injected from Python code.

## Framing

# ProofDeploy blind probe authoring

You are authoring black-box probes for a code change. You see the diff, a list of the repository's files at the changed commit, the full contents of the files the diff touches (subject to the caps below), a fixture description, and an authoring skill section (which may be empty).

## Output contract

Output contract — output each probe as a fenced ```json block, one block per probe, nothing
else inside the blocks. A probe is exactly:

{"setup": [...], "act": {"method": "...", "path": "/..."}, "assert": [...]}

- "setup": a list of steps. Step types:
  - {"type": "http", "act": {"method": "GET|POST|PUT|PATCH|DELETE|HEAD",
    "path": "/...", "headers": {...}, "body": ...},
    "capture": {"NAME": {"from": "body", "json_path": "dotted.path"}}}
    Use setup steps to create preconditions (users, records) and capture
    values (e.g. tokens) for later steps as ${NAME}.
  - {"type": "harness_app", "language": "python|node",
    "source": "<a minimal app that mounts the library and serves HTTP>",
    "entrypoint": "app.py"}
    Only when the fixture says the target is a library. The harness app
    is started for you; its base URL replaces the target URL.
- Placeholders: ${AUTH_TOKEN} is your injected bearer token (use it in an
  "Authorization": "Bearer ${AUTH_TOKEN}" header when the fixture says
  authentication is by bearer token). ${NAME} references a captured value.
  You may choose credentials for users you create in setup. Never guess credentials for pre-existing accounts, and never write a literal token you were not given.
- "act": one HTTP request: {"method", "path", "headers", "body"}.
  Methods allowed: GET, HEAD, POST, PUT, PATCH, DELETE.
- "assert": non-empty list of assertions:
  - {"type": "status", "equals": 200}
  - {"type": "body", "contains": "text"} | {"matches": "regex"} |
    {"equals": ...} | {"json_path": {"path": "a.b", "equals": ...}}
  - {"type": "header", "name": "X-...", "equals"|"contains"|"matches": ...}
  - {"type": "db", "query": "SELECT ...", "count": N} (read-only SELECT only)
- Assert only externally observable behavior: status codes, headers, bodies, read-only database state. Each probe should exercise behavior the diff changed and assert the behavior the change is intended to produce, as inferred from the diff, the code and any docs. A probe should pass on a correct implementation and fail on an incorrect one.

## Caps

- `max_diff_files`: 20
- `max_file_bytes`: 100000
- `max_listed_files`: 200

## Body

[[framing]]

[[contract]]

[[caps]]

---

## Authoring skill

[[skill_text]]

---

## Fixture

[[fixture_description]]

---

## Diff (the change under test)

[[diff_fence_open]]
[[diff_text]]
[[diff_fence_close]]

---

## Snapshot file list

[[snapshot_file_list]]

---

## Touched file contents

[[touched_file_contents]]

[[closing]]

## File entry

### FILE: [[path]]

[[file_fence_open]]
[[content]]
[[file_fence_close]]

## Unavailable file

### FILE: [[path]]

(binary, missing, or too large: not inlined)

## More files

... ([[remaining]] more files)

## Caps line

Caps: full contents shown for at most [[max_diff_files]] touched files, at most [[max_file_bytes]] bytes per file, at most [[max_listed_files]] files listed.

## Diff files omitted

... ([[remaining]] more touched files not shown in full: cap is [[max_diff_files]])

## Closing

Write your probes now: one fenced ```json block per probe.
```

## 2. Prompt builder

- **Path:** `proofdeploy/model_client.py`
- **SHA-256:** `55b7d93283d511b681f7af24dd78aafc0cc750d652ed965f94fe7a4ab1d28598`
  (at the commit that will merge; see below)

**Statement:** Any change to the template (`proofdeploy/author_prompt_v7.md`)
or the builder (`proofdeploy/model_client.py`) requires a new append-only
registration before the next measured run. The SHA-256 of both files is
recorded in every evidence record as `author_prompt_sha256` and
`author_prompt_builder_sha256`.

## 3. Model-call failure rule (Master-approved, 2026-10-10)

Master's principle: a result we're unsure of is stated as such. When
something external fails, say so plainly, and never guess pass or fail.

1. **Labelling.** A unit whose model call produces no completion is
   INCONCLUSIVE, reason class `environment`, with the cause recorded as
   `model_overflow` (the prompt exceeds the model's context) or
   `model_transport` (no response, or a transport error). It is never
   reported as PASS, FAIL, catch or no-catch, and the per-unit report
   states the cause in plain words.
2. **Retry.** `model_transport`: exactly one retry, with both attempts
   recorded in the evidence. `model_overflow`: no retry.
3. **Counting.** For the registered rates and the checkpoint bar, such a
   unit counts against the tool: a bug unit counts as not caught, and a
   clean-diff unit counts as a false alarm.
4. **Disclosure.** Every published rate states alongside it how many units
   were INCONCLUSIVE for external causes, by cause, so no external failure
   is hidden or mistaken for a measured result.
5. **ProofDeploy failure.** A unit that cannot finish because of a failure in ProofDeploy itself (any error in the tool after the model has answered, other than the causes in rules 1 and 6) is **INCONCLUSIVE**, reason class `environment`, cause `tool_failure`. Its evidence record keeps the error, the runner log and every result gathered before the failure. It is never reported as PASS, FAIL, catch or no-catch. It counts against the tool exactly as in rule 3, and is disclosed by cause as in rule 4. It can never be downgraded by any product policy.
6. **Invalid author output.** Every probe the author writes is validated on its own. A probe that fails validation is not sent; it is reported individually as **INCONCLUSIVE**, reason class `probe`, with its text and rejection reason recorded. Valid probes in the same output run and are scored as usual. A unit whose author output contains **no valid probe** (no probe blocks, unparseable output, or every probe invalid) has cause `author_output_invalid`. It counts against the tool exactly as in rule 3 (a bug unit counts as not caught; a clean-diff unit counts as a false alarm) and is disclosed by cause as in rule 4.
