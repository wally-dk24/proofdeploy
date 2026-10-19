# Registration: Author Prompt v8 (O21 recipe v8, skill v5)

> Registered by Master, D242. Append-only: any change to the template or the
> builder requires a new registration before the next measured run. The signed
> spec is `docs/evaluation/o21/` (see its `SHA256SUMS`).

## 1. Author prompt template

- **Path:** `proofdeploy/author_prompt_v8.md`
- **SHA-256:** `09c55dca9d67f674850476a2f9fb36bc86185f1c1af68fbee9ab074240181f7c`
- **Header:** the rendered prompt up to the line `## Authoring skill` equals
  `docs/evaluation/o21/prompt-v8-header.md` (sha256 `52ae0da3...`), enforced by a test.
- **v7 template** (`proofdeploy/author_prompt_v7.md`, sha256
  `fa72a0e6c4a1fa9fb204986e08c8eef4b11fc13786f41389ecf2f42ea8555cf3`) is untouched.

## 2. Prompt builder

- **Path:** `proofdeploy/model_client.py`
- **SHA-256:** `1e063204c1201ae83cfa9fb9d33f046b6dcc2b99bea43d7d7d99f1fb0ff3e859`

## 3. Authoring skill v5

- **Skill author copy:** `docs/evaluation/skill/skill-author.md`
- **SHA-256 (`EXPECTED_SKILL_HASH`):** `75d52a774f505dafd0ccde84cf91c9a1b8c485b2e8465e106b78d6d43268310c`

## 4. Method

R-BUNDLE (route declarations, cap `max_route_bytes: 32768`) and R-METHOD
(evidence-cited assertions, one correction round) as specified in
`docs/evaluation/o21/o21-registrations-signed.md` (sha256 `9af08ac8...`).

Registered by Master, D242.

## 5. Template text (verbatim)

```
# Author prompt template v8 (frozen)

> This file is frozen. Any change creates a new version (e.g. `author_prompt_v9.md`)
> which must be registered by Master before any measured run. The SHA-256 of
> this file is recorded in every evidence record as `author_prompt_sha256`.
>
> Registered by Master, D242 (O21: prompt v8 + skill v5 + R-BUNDLE + R-METHOD).
> The Framing, Output contract and Caps line sections below reproduce the signed
> header `docs/evaluation/o21/prompt-v8-header.md`: a rendered prompt's text
> before the line `## Authoring skill` equals that file (a test enforces it).

> The `## Body` section below is the whole prompt skeleton: every section
> heading, its order, and every label comes from this file. The builder only
> substitutes the `[[...]]` placeholders with bundle content. No heading or
> label is injected from Python code.

## Framing

# ProofDeploy blind probe authoring

You are writing black-box HTTP probes for a code change. You are shown: the fixture (a description of the running instance and its data), the diff, the repository's file list, the full text of the files the diff touches (lines the diff adds or changes are marked `<<CHANGED>>` at the start), the route declarations of the app, and an authoring skill section (which may be empty).

The change may be correct or it may contain a bug. Your probes decide which: every expected value must come from evidence outside the changed lines, quoted in the assertion's `evidence`. A probe should pass on an app that behaves as the evidence says and fail on one that does not, whatever the diff does.

## Output contract

## Output format

Write any reasoning first, in plain prose. Then output each probe as its own fenced ```json block. Blocks contain JSON only: no comments, no `...`, no text outside the JSON.

This complete example shows every allowed form. Copy its shapes exactly:

```json
{
  "setup": [
    {"type": "http",
     "act": {"method": "POST", "path": "/items", "headers": {"Authorization": "Bearer ${AUTH_TOKEN}"}, "body": {"name": "probe item"}},
     "capture": {"ITEM_ID": {"from": "body", "json_path": "id"}}}
  ],
  "act": {"method": "GET", "path": "/items/${ITEM_ID}", "headers": {"Authorization": "Bearer ${AUTH_TOKEN}"}},
  "assert": [
    {"type": "status", "equals": 200, "evidence": {"source": "fixture", "quote": "<exact text from the fixture>"}},
    {"type": "body", "json_path": {"path": "name", "equals": "probe item"}, "evidence": {"source": "src/items.js", "quote": "<exact text from that file>"}},
    {"type": "body", "contains": "probe item", "evidence": {"source": "src/items.js", "quote": "<exact text>"}},
    {"type": "body", "matches": "^\\{", "evidence": {"source": "src/items.js", "quote": "<exact text>"}},
    {"type": "header", "name": "Content-Type", "contains": "json", "evidence": {"source": "src/items.js", "quote": "<exact text>"}},
    {"type": "db", "query": "SELECT id FROM items WHERE name = 'probe item'", "count": 1, "evidence": {"source": "src/items.js", "quote": "<exact text>"}}
  ]
}
```

Rules for each part:
- `act`: `method` and `path` are required; `method` is one of GET, HEAD, POST, PUT, PATCH, DELETE. `headers` (an object) and `body` (any JSON value) are optional.
- `setup`: a list of steps, possibly empty. Each step has `"type": "http"`, an `act` as above, and optionally `capture`. A `capture` maps a NAME to exactly `{"from": "body", "json_path": "<dotted path>"}`. Omit `capture` if you capture nothing; never write an empty one.
- Two different `json_path` shapes exist on purpose: inside `capture` it is a string (`"json_path": "id"`); inside an assertion it is an object (`"json_path": {"path": "id", "equals": 1}`).
- `assert`: a non-empty list. Every assertion has a `type` (status, body, header or db) and a top-level `evidence` object. `evidence` is never placed inside `json_path`.
- `status` `equals` and `db` `count` are integers. `db` queries are read-only SELECT statements.
- Placeholders such as `${AUTH_TOKEN}` and `${NAME}` appear only inside JSON strings. `${AUTH_TOKEN}` is the token the fixture describes; `${NAME}` is a value you captured earlier.
- Credentials: you may use credentials the fixture states, and you may create new users in setup with credentials you choose. Never invent credentials for other existing accounts, and never write a literal token you were not given.
- For a library (the fixture says so), a setup step may be `{"type": "harness_app", "language": "python" or "node", "source": "<a minimal app that mounts the library and serves HTTP>", "entrypoint": "<the source's file name, e.g. app.py or app.js>"}`. Its base URL replaces the target URL.

Rules for `evidence`:
- `source` is a file path shown in this prompt, or the literal string `"fixture"`. Only text shown in this prompt can be quoted; a file that is listed but not shown cannot.
- `quote` is copied exactly from that source (at most 300 characters). It must not be a line marked `<<CHANGED>>`, or any line the diff adds or changes, including changed tests.
- The quote must contain the expected value's text: the number for `status`, `count` and numeric `equals`; `true` or `false` for a boolean (any capitalisation); the string itself for `contains` and string `equals`; for `matches`, a string the pattern matches. For a true/false value, quote only the part about this record: a quote containing both `true` and `false` is refused.
- If no shown text outside the changed lines supports a value, do not assert that value.

The harness checks every assertion against these rules before anything runs. Errors are sent back to you once to fix; anything still wrong is dropped and counts against you.

## Caps

- `max_diff_files`: 20
- `max_file_bytes`: 100000
- `max_listed_files`: 200
- `max_route_bytes`: 32768

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

---

## Route declarations (selected mechanically; may be incomplete)

[[route_declarations]]

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

Caps: full text is shown for at most [[max_diff_files]] touched files, at most [[max_file_bytes]] bytes per file; at most [[max_listed_files]] files are listed; route declarations up to [[max_route_kb]] KB.

## Diff files omitted

... ([[remaining]] more touched files not shown in full: cap is [[max_diff_files]])

## Route shown above

### FILE: [[path]]

(shown above)

## Route files omitted

... ([[remaining]] more route declaration files not shown: cap is [[max_route_bytes]] bytes)

## No route declarations

(none found)

## Closing

Write your probes now: one fenced ```json block per probe.
```
