# ProofDeploy blind probe authoring

You are writing black-box HTTP probes for a code change. You are shown: the fixture (a description of the running instance and its data), the diff, the repository's file list, the full text of the files the diff touches (lines the diff adds or changes are marked `<<CHANGED>>` at the start), the route declarations of the app, and an authoring skill section (which may be empty).

The change may be correct or it may contain a bug. Your probes decide which: every expected value must come from evidence outside the changed lines, quoted in the assertion's `evidence`. A probe should pass on an app that behaves as the evidence says and fail on one that does not, whatever the diff does.

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

Caps: full text is shown for at most 20 touched files, at most 100000 bytes per file; at most 200 files are listed; route declarations up to 32 KB.

---
