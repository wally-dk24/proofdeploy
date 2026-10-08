# ADR 0004: Injectable probe authoring

Date: 2026-10-08
Status: Accepted

## Context

The PRD assigns the LLM a single role: authoring-time work (change model,
probe authoring, expected-result reasoning). Execution and verdict comparison
are deterministic. The v1 CLI was sketched with a built-in probe author that
calls a model API directly.

Two integration shapes emerged that change who the author should be:

1. A `proofdeploy` agent skill (SKILL.md, open Agent Skills standard) for
   Claude Code, Codex, Cursor, Copilot, OpenCode, and other coding agents.
   In that shape the session's own model is the natural author: no API key
   to manage, no credential that could leak into a repo.
2. CI and standalone CLI use, where a configured model provider remains the
   practical author.

If the CLI hardwires "call model API, then execute", the skill shape needs a
fork or a second entrypoint, and the probe schema stays an internal detail.

## Decision

`proofdeploy verify` accepts pre-authored probes via `--probes <file>`
(a JSON document conforming to the published probe schema). The built-in
model-backed author becomes one author among many; the deterministic runner
never depends on who authored the probes.

Concretely:

- The probe schema (probe types, fields, expected-result encoding) is a
  public, versioned contract, documented and validated on ingest.
- `--probes` input is validated strictly: malformed or schema-violating
  files fail verification with a clear error, never silent degradation.
- The runner records the author identity (cli-builtin, skill session, file)
  in the JSONL record for auditability, but verdict logic is identical.
- The skill shape (post-checkpoint work) uses `--probes`: the agent authors,
  the CLI executes and renders verdicts.

## Rationale

- Keeps the PRD's LLM/deterministic seam clean: authoring is injectable,
  execution is fixed.
- One binary serves the CLI user, the CI job, and every agent skill: no
  per-agent forks of the runner.
- Removes the API key from the skill path entirely, satisfying the
  "model key is runtime-only, never committed" requirement by construction.
- The probe schema becomes reviewable on its own merits, which is what the
  blind evaluation checkpoint needs anyway.

## Consequences

- WAL-57 (probe author) must define and freeze the probe schema as a public
  contract, not an internal dataclass.
- `--probes` file validation needs its own tests (malformed JSON, unknown
  probe types, missing expected results).
- The skill itself is post-checkpoint work: it is not built until the blind
  evaluation proves verdicts catch real breaks.
