# ADR-0013: Workspace / Monorepo Provisioning (Plan 1.2b, D16)

**Date:** 2026-10-14
**Status:** Accepted

## Context

The provisioning adapters refused every monorepo workspace with the
typed refusal "workspaces not yet supported". In the 2026-10-14
ten-repo readiness re-run that refusal was the single largest
blocker (25 of 82 side-attempts: Directus, Vendure, Ghost, nest).
Master's decision D16 puts monorepo/workspace support in Stage 1,
built stack-generic. The contract needs a way to say WHICH package
in the workspace is the app, and the adapter interface needs a
workspace-aware planning method that does not bake in one
ecosystem — plan 1.2e (bun as a Node package manager) must plug
into the same method next.

## Decision

1. **Contract:** `proofdeploy.yml` gains an optional `app` mapping
   whose single key is `path` — the workspace member directory to
   build and start (`.` for the root package). Validated in
   `proofdeploy/yml.py` with the same rules as the other mappings:
   unknown keys refused, and the path must resolve inside the
   checkout. Stored on the contract as `app_path`.
2. **Interface:** `ProvisioningAdapter` gains `plan_workspace()`.
   Its shape is ecosystem-shaped, never manager-shaped: the frozen
   install runs at the workspace root with the manager the adapter
   selects from the root's own lockfile (a plan fact, recorded on
   the plan), and the contract's build/migrate/seed/start steps
   are scoped to the resolved app member via a new `PlanStep.cwd`
   (relative to the checkout root; `None` keeps every pre-1.2b
   plan byte-identical in behavior). The plan records
   `workspace_root` and `app_path` as evidence.
3. **App resolution:** the contract's `app.path` wins and must
   name the root or a detected member (anything else is a typed
   refusal naming the path). Undeclared: exactly one member is the
   app; no members means the root package is the app; several
   members is the typed refusal `ambiguous app: <n> candidates`.
   Nothing is ever guessed.
4. **Detection:** workspace members come from
   `pnpm-workspace.yaml`'s `packages:` globs when that file exists
   (pnpm's own precedence), else package.json's `workspaces` (list
   or `{packages: [...]}` form). Globs support `*`, `**`, literals
   and `!` exclusions; a member is a matched directory containing
   a package.json.
5. **Implementations:** Node implements pnpm and npm workspaces
   (the frozen install and pin/lockfile-mismatch machinery are the
   root plan's, unchanged — rule 9 untouched). Another manager's
   workspace (yarn, bun) is a typed refusal naming the manager;
   plan 1.2e adds bun to the supported set inside the same method.
   Python's `plan_workspace` returns the typed refusal
   `workspaces not yet supported for runtime 'python'`.
6. **Executors:** both executors honor `PlanStep.cwd` — the
   sandbox prefixes the rendered command with
   `cd /checkout/<cwd> && ` (the container workdir stays the
   checkout root) and the host runs the step with that working
   directory; the executed-step records carry the `cwd` either
   way. Non-workspace plans render exactly as before.

## Consequences

- The blanket "workspaces not yet supported" refusal is gone where
  detection succeeds; typed refusals remain for ambiguous
  workspaces, non-member `app.path` values, unsupported workspace
  managers, and Python workspaces.
- One existing test's fixture changes shape (WAL-119's
  both-refusals premise): the Directus-shaped snapshot now takes
  the ambiguous-workspace shape (two members, no `app.path`,
  unsatisfiable declared range), so the real plan still carries
  both refusals and the executor still fires the image refusal
  first. The premise and the row assertions are unchanged.
- Conformance gains a fourth golden app, `node-pnpm-monorepo`
  (shared library + app, the planted bug in the library), adding
  10 sandbox cases in two new CI shards.
