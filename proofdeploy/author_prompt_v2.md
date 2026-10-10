# Author prompt template v2 (frozen)

> This file is frozen. Any change creates a new version (e.g. `author_prompt_v3.md`)
> which must be registered by Master before any measured run. The SHA-256 of
> this file is recorded in every evidence record as `author_prompt_sha256`.

> The `## Body` section below is the whole prompt skeleton: every section
> heading, its order, and every label comes from this file. The builder only
> substitutes the `[[...]]` placeholders with bundle content. No heading or
> label is injected from Python code.

## Framing

# ProofDeploy blind probe authoring

You are authoring black-box probes for a code change. You see the diff, the repository snapshot at the changed commit, and a fixture description.

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
  - {"type": "harness_app", "language": "python",
    "source": "<a minimal app that mounts the library and serves HTTP>",
    "entrypoint": "app.py"}
    Only when the fixture says the target is a library. The harness app
    is started for you; its base URL replaces the target URL.
- Placeholders: ${AUTH_TOKEN} is your injected bearer token (use it in an
  "Authorization": "Bearer ${AUTH_TOKEN}" header when the fixture says
  authentication is by bearer token). ${NAME} references a captured value.
  Never write a literal token or password: you do not have any.
- "act": one HTTP request: {"method", "path", "headers", "body"}.
  Methods allowed: GET, HEAD, POST, PUT, PATCH, DELETE.
- "assert": non-empty list of assertions:
  - {"type": "status", "equals": 200}
  - {"type": "body", "contains": "text"} | {"matches": "regex"} |
    {"equals": ...} | {"json_path": {"path": "a.b", "equals": ...}}
  - {"type": "header", "name": "X-...", "equals"|"contains"|"matches": ...}
  - {"type": "db", "query": "SELECT ...", "count": N} (read-only SELECT only)
- Assert only externally observable behavior: status codes, headers,
  bodies, read-only database state. Every probe must exercise at least
  one behavior the diff changed: a probe that passes identically with
  and without the diff tells us nothing.

## Caps

- `max_diff_files`: 20
- `max_file_bytes`: 100000
- `max_listed_files`: 200

## Body

[[framing]]

[[contract]]

---

## Authoring skill

[[skill_text]]

---

## Fixture

[[fixture_description]]

---

## Diff (the change under test)

```diff
[[diff_text]]
```

---

## Snapshot file list

[[snapshot_file_list]]

---

## Touched file contents

[[touched_file_contents]]

[[closing]]

## File entry

### FILE: [[path]]

```
[[content]]
```

## Unavailable file

### FILE: [[path]]

(binary, missing, or too large: not inlined)

## More files

... ([[remaining]] more files)

## Closing

Write your probes now: one fenced ```json block per probe.
