# Probe schema v0 (draft)

Status: **draft**. This schema will be frozen as the `--probes` file contract
in WAL-57. Until then, treat it as the authoring target: the skill emits
`probes.json` in this shape, and the future runner validates strictly against
it. Malformed files will fail verification, never degrade silently.

## Document

```json
{
  "version": "0",
  "probes": [ "<probe>", "..." ]
}
```

- `version`: schema version the file was authored against. The runner rejects
  files whose version it does not understand.
- `probes`: non-empty array of probe objects. Order is execution order.

## Probe (common fields)

| Field         | Type   | Required | Notes                                                    |
|---------------|--------|----------|----------------------------------------------------------|
| `id`          | string | yes      | Unique within the file. Name the behavior, not `probe-1`. |
| `type`        | string | yes      | `"http"` or `"db"` in v1. Unknown types are rejected.     |
| `description` | string | yes      | One line: what behavior this probe guards.                |

## HTTP probe

```json
{
  "id": "gift-link-accept-activates-member",
  "type": "http",
  "description": "Accepting a gift-link invitation activates the member",
  "request": {
    "method": "POST",
    "path": "/api/invitations/accept",
    "headers": {"Content-Type": "application/json"},
    "body": {"token": "<gift-link-token>"}
  },
  "expect": {
    "status": 200,
    "json": {
      "member.status": "active"
    }
  }
}
```

| Field              | Type   | Required | Notes                                              |
|--------------------|--------|----------|----------------------------------------------------|
| `request.method`   | string | yes      | `GET`, `POST`, `PUT`, `PATCH`, `DELETE`, `HEAD`.    |
| `request.path`     | string | yes      | Path only, loopback host implied. No absolute URLs. |
| `request.headers`  | object | no       | String-to-string map. No `Authorization` with real secrets in v1. |
| `request.body`     | any    | no       | JSON-serializable. Omitted for GET/HEAD.            |
| `expect.status`    | number | yes      | Exact HTTP status code.                            |
| `expect.json`      | object | no       | Dotted-path → expected value assertions on a JSON body. |
| `expect.contains`  | string | no       | Substring that must appear in the raw body.         |

At least one of `expect.json` / `expect.contains` is required: a probe that
only checks the status code is a health check, and health checks are out of
scope.

## DB probe

```json
{
  "id": "member-row-active-after-accept",
  "type": "db",
  "description": "Member row shows active after gift-link accept",
  "query": "SELECT status FROM members WHERE id = ?",
  "params": ["<member-id>"],
  "expect": {
    "rows": [["active"]]
  }
}
```

| Field          | Type   | Required | Notes                                                        |
|----------------|--------|----------|--------------------------------------------------------------|
| `query`        | string | yes      | A single read-only `SELECT`. Anything else is rejected.       |
| `params`       | array  | no       | Positional parameters for `?` placeholders.                  |
| `expect.rows`  | array  | yes      | Exact expected rows, as arrays, in order. Empty array asserts no rows. |

Rules enforced by the runner (v1):

- The query must parse as a single `SELECT`. `INSERT`, `UPDATE`, `DELETE`,
  `DROP`, `ALTER`, `CREATE`, multiple statements, and stacked queries are
  rejected before execution.
- Execution uses a read-only role or an always-rollback transaction —
  enforcement is by the database, not by keyword filtering.
- Row comparison is exact. For volatile columns, select only the columns
  the change governs.

## Worked file

See `SKILL.md` "Worked example" for the three probes (two HTTP, one DB)
this schema encodes for the gift-link scenario.

## Changelog

- **v0** (2026-10-08): initial draft for skill authoring tests. Not frozen.
