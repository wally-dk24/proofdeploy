# Validating probes

The skill emits `probes.json`; the runner will validate it strictly before
executing anything. This example shows what "strictly" means at authoring
time, with a validator you can run yourself.

## The schema (draft)

`probe-schema.json` is a machine-readable draft of the v0 probe contract
(JSON Schema, draft 2020-12). It mirrors
`plugins/proofdeploy/skills/proofdeploy/references/probe-schema.md`.
Status: **draft** — frozen in WAL-57. Until then, treat it as the authoring
target.

## The validator

`validate.py` is stdlib-only (no dependencies). It enforces the structural
v0 rules: version, unique ids, `http`/`db` types, required fields, the
status-plus-assertion rule for HTTP probes, and the single-`SELECT` rule for
DB probes. The runner additionally parses SQL and enforces read-only at the
database level — a keyword scan here can't do that job, and doesn't pretend to.

```bash
# the worked example from authoring-a-probe: valid
python3 validate.py ../authoring-a-probe/probes.json

# the deliberately broken file: every problem listed, exit 1
python3 validate.py invalid-example.json
```
