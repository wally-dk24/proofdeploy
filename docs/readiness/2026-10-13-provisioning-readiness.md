# Provisioning readiness pass — report

The run: `.github/workflows/readiness.yml` on `main` at `833fa3baf2cf59b2a680696b487f8fef2852a1d8`, dispatched once per registered repo at attempt 2 in a single concurrent batch. The harness provisions each registered (unit, side) through the product's plan, snapshot and sandbox path — no model, no probes, no scoring, no secrets; it never sets `measured_result`. A side is READY only when the product reports it built, started, and answering its readiness check.

This pass is complete: **all 42 registered sides have a row in attempt 1 and in attempt 2, and attempt 2 mirrors attempt 1 on (status, typed_reason) for every side.** Every run concluded `success`, and there are no harness-error rows. (Two earlier batches inform the harness's history — the first Directus dispatches that validated attempt isolation, and a nine-repo batch in which Vendure's run died mid-loop before its bug-side rows were written; the harness defects those exposed were fixed in PR #76. This report rests only on the batch below.)

## Cause classes

Verdict classes were settled when this report's form was fixed (Addendum 66). Their causes, and the mapping used below:

- **tool coverage gap** — the product does not cover this case yet. Five sub-causes appear in this run: workspaces refused at plan; a package manager the sandbox does not support; a snapshot containing a symlink (links are refused); no frozen lockfile in the snapshot; a runtime the product does not support.
- **no runnable app** — the registered entry point is not an application.
- **start failure** — the app installed and started, but never answered its readiness check, for a reason not established to be a contract defect.
- **contract defect (ours)** — our own registered contract was wrong for the app, so the run measured our error, not the app's behavior.
- **harness defect** — the harness itself failed (a row whose reason begins `harness error:`).
- **product defect** — the product itself misbehaved. Reserved; no rows in this run.

Mapping (row -> class): a refused plan naming workspaces, an unsupported package manager, a refused snapshot link, a missing frozen lockfile, or an unsupported runtime -> tool coverage gap, sub-cause as named; `django-admin is not a runnable app.` -> no runnable app; a `START_FAILED` row whose cause is established as our contract's error -> contract defect (ours); any other `START_FAILED` row -> start failure; a row whose reason begins `harness error:` -> harness defect. Checkpoint-bug rows are stated as coming from the orchestrator's record; no new field was added to any row.

## Results by cause

| class | sides |
|---|---|
| tool coverage gap: symlinks refused | 18 |
| tool coverage gap: workspaces | 12 |
| tool coverage gap: no frozen lockfile | 5 |
| tool coverage gap: package manager not supported in the sandbox | 3 |
| contract defect (ours) | 2 |
| tool coverage gap: runtime not supported | 2 |
| start failure: readiness not answered | 0 |
| no runnable app | 0 |
| harness defect | 0 |
| product defect | 0 |
| **total** | **42** |

**READY: 0 of 42 sides.**

## READY rate per repo

| repo | READY | sides | rate |
|---|---|---|---|
| directus/directus | 0 | 4 | 0% |
| nestjs/nest | 0 | 12 | 0% |
| fastify/fastify | 0 | 5 | 0% |
| honojs/hono | 0 | 3 | 0% |
| bottlepy/bottle | 0 | 2 | 0% |
| codeigniter4/CodeIgniter4 | 0 | 2 | 0% |
| vendure-ecommerce/vendure | 0 | 6 | 0% |
| TryGhost/Ghost | 0 | 3 | 0% |
| django/django | 0 | 3 | 0% |
| LMCache/LMCache | 0 | 2 | 0% |

## Runs

| repo | run | conclusion | sides |
|---|---|---|---|
| directus/directus | [38615852849](https://github.com/wally-dk24/proofdeploy/actions/runs/38615852849) | success | 4 |
| nestjs/nest | [38615865081](https://github.com/wally-dk24/proofdeploy/actions/runs/38615865081) | success | 12 |
| fastify/fastify | [38615877972](https://github.com/wally-dk24/proofdeploy/actions/runs/38615877972) | success | 5 |
| honojs/hono | [38615890791](https://github.com/wally-dk24/proofdeploy/actions/runs/38615890791) | success | 3 |
| bottlepy/bottle | [38615903114](https://github.com/wally-dk24/proofdeploy/actions/runs/38615903114) | success | 2 |
| codeigniter4/CodeIgniter4 | [38615915203](https://github.com/wally-dk24/proofdeploy/actions/runs/38615915203) | success | 2 |
| vendure-ecommerce/vendure | [38615928165](https://github.com/wally-dk24/proofdeploy/actions/runs/38615928165) | success | 6 |
| TryGhost/Ghost | [38615940418](https://github.com/wally-dk24/proofdeploy/actions/runs/38615940418) | success | 3 |
| django/django | [38615952118](https://github.com/wally-dk24/proofdeploy/actions/runs/38615952118) | success | 3 |
| LMCache/LMCache | [38615964346](https://github.com/wally-dk24/proofdeploy/actions/runs/38615964346) | success | 2 |

Artifacts: each run's artifact holds `rows-attempt1.json`, `rows-attempt2.json`, and `pd-logs/` (one install/start log per side per attempt).

## Every side

One row per (unit, side), attempt 1. Attempt 2 is identical on (status, typed_reason) for all 42 rows.

| repo | unit | role | status | typed reason (verbatim) | cause class |
|---|---|---|---|---|---|
| directus/directus | C3 | C | BUILD_FAILED | archive member is a link, refused: readme.md | tool coverage gap: symlinks refused |
| directus/directus | C4 | C | BUILD_FAILED | archive member is a link, refused: readme.md | tool coverage gap: symlinks refused |
| directus/directus | checkpoint-bug-directus-1 | fix^ | BUILD_FAILED | archive member is a link, refused: readme.md | tool coverage gap: symlinks refused |
| directus/directus | checkpoint-bug-directus-1 | fix | BUILD_FAILED | archive member is a link, refused: readme.md | tool coverage gap: symlinks refused |
| nestjs/nest | H1 | fix^ | BUILD_FAILED | workspaces not yet supported | tool coverage gap: workspaces |
| nestjs/nest | H1 | fix | BUILD_FAILED | workspaces not yet supported | tool coverage gap: workspaces |
| nestjs/nest | H2 | fix^ | BUILD_FAILED | workspaces not yet supported | tool coverage gap: workspaces |
| nestjs/nest | H2 | fix | BUILD_FAILED | workspaces not yet supported | tool coverage gap: workspaces |
| nestjs/nest | H3 | fix^ | BUILD_FAILED | workspaces not yet supported | tool coverage gap: workspaces |
| nestjs/nest | H3 | fix | BUILD_FAILED | workspaces not yet supported | tool coverage gap: workspaces |
| nestjs/nest | H4 | fix^ | BUILD_FAILED | workspaces not yet supported | tool coverage gap: workspaces |
| nestjs/nest | H4 | fix | BUILD_FAILED | workspaces not yet supported | tool coverage gap: workspaces |
| nestjs/nest | heldout-clean-1 | C | BUILD_FAILED | workspaces not yet supported | tool coverage gap: workspaces |
| nestjs/nest | heldout-clean-2 | C | BUILD_FAILED | workspaces not yet supported | tool coverage gap: workspaces |
| nestjs/nest | heldout-clean-3 | C | BUILD_FAILED | workspaces not yet supported | tool coverage gap: workspaces |
| nestjs/nest | heldout-clean-4 | C | BUILD_FAILED | workspaces not yet supported | tool coverage gap: workspaces |
| fastify/fastify | T2 | fix^ | BUILD_FAILED | no frozen lockfile found in snapshot for runtime 'node' | tool coverage gap: no frozen lockfile |
| fastify/fastify | T2 | fix | BUILD_FAILED | no frozen lockfile found in snapshot for runtime 'node' | tool coverage gap: no frozen lockfile |
| fastify/fastify | T3 | fix^ | BUILD_FAILED | no frozen lockfile found in snapshot for runtime 'node' | tool coverage gap: no frozen lockfile |
| fastify/fastify | T3 | fix | BUILD_FAILED | no frozen lockfile found in snapshot for runtime 'node' | tool coverage gap: no frozen lockfile |
| fastify/fastify | train-C3 | C | BUILD_FAILED | no frozen lockfile found in snapshot for runtime 'node' | tool coverage gap: no frozen lockfile |
| honojs/hono | T7 | fix^ | BUILD_FAILED | not yet supported in the sandbox | tool coverage gap: package manager not supported in the sandbox |
| honojs/hono | T7 | fix | BUILD_FAILED | not yet supported in the sandbox | tool coverage gap: package manager not supported in the sandbox |
| honojs/hono | train-C2 | C | BUILD_FAILED | not yet supported in the sandbox | tool coverage gap: package manager not supported in the sandbox |
| bottlepy/bottle | T6 | fix^ | START_FAILED | readiness timeout | contract defect (ours) |
| bottlepy/bottle | T6 | fix | START_FAILED | readiness timeout | contract defect (ours) |
| codeigniter4/CodeIgniter4 | T1 | fix^ | BUILD_FAILED | runtime not supported | tool coverage gap: runtime not supported |
| codeigniter4/CodeIgniter4 | T1 | fix | BUILD_FAILED | runtime not supported | tool coverage gap: runtime not supported |
| vendure-ecommerce/vendure | C1 | C | BUILD_FAILED | archive member is a link, refused: CLAUDE.md | tool coverage gap: symlinks refused |
| vendure-ecommerce/vendure | C2 | C | BUILD_FAILED | archive member is a link, refused: CLAUDE.md | tool coverage gap: symlinks refused |
| vendure-ecommerce/vendure | checkpoint-bug-vendure-1 | fix^ | BUILD_FAILED | archive member is a link, refused: CLAUDE.md | tool coverage gap: symlinks refused |
| vendure-ecommerce/vendure | checkpoint-bug-vendure-1 | fix | BUILD_FAILED | archive member is a link, refused: CLAUDE.md | tool coverage gap: symlinks refused |
| vendure-ecommerce/vendure | checkpoint-bug-vendure-2 | fix^ | BUILD_FAILED | archive member is a link, refused: CLAUDE.md | tool coverage gap: symlinks refused |
| vendure-ecommerce/vendure | checkpoint-bug-vendure-2 | fix | BUILD_FAILED | archive member is a link, refused: CLAUDE.md | tool coverage gap: symlinks refused |
| TryGhost/Ghost | C5 | C | BUILD_FAILED | archive member is a link, refused: .claude/skills/add-admin-api-endpoint | tool coverage gap: symlinks refused |
| TryGhost/Ghost | checkpoint-bug-ghost-1 | fix^ | BUILD_FAILED | archive member is a link, refused: .claude/skills/add-admin-api-endpoint | tool coverage gap: symlinks refused |
| TryGhost/Ghost | checkpoint-bug-ghost-1 | fix | BUILD_FAILED | archive member is a link, refused: .claude/skills/add-admin-api-endpoint | tool coverage gap: symlinks refused |
| django/django | T4 | fix^ | BUILD_FAILED | archive member is a link, refused: docs/_theme/djangodocs-epub/static/docicons-behindscenes.png | tool coverage gap: symlinks refused |
| django/django | T4 | fix | BUILD_FAILED | archive member is a link, refused: docs/_theme/djangodocs-epub/static/docicons-behindscenes.png | tool coverage gap: symlinks refused |
| django/django | train-C1 | C | BUILD_FAILED | archive member is a link, refused: docs/_theme/djangodocs-epub/static/docicons-behindscenes.png | tool coverage gap: symlinks refused |
| LMCache/LMCache | T5 | fix^ | BUILD_FAILED | archive member is a link, refused: docs/design/tools/controller_benchmark/README.md | tool coverage gap: symlinks refused |
| LMCache/LMCache | T5 | fix | BUILD_FAILED | archive member is a link, refused: docs/design/tools/controller_benchmark/README.md | tool coverage gap: symlinks refused |

## Notes on individual sides

- **Bottle (T6, both sides) — contract defect (ours), cause established.** The harness assigns the app a port and bridges the readiness probe to it; our contract's start hardcoded a different port, so the app listened where nothing probed. From the fix side's attempt-1 pd-log (`readiness-T6-fix-a1.log`), verbatim — the container is given `PORT=55185` and the bridge forwards to 55185, while the contract's own start binds 8080:

```
$ podman --storage-driver vfs --cgroup-manager=cgroupfs run -d --name pd-fix-a859256a --net=none --uidmap 0:1:65536 --cap-drop=ALL --security-opt=no-new-privileges -v /home/runner/work/proofdeploy/proofdeploy/readiness-work/units/T6/a1/snapshot-fix:/checkout -w /checkout -v /tmp/pd-87dqp0c1:/pd-sock -e HOME=/checkout/.sandbox-home -e PORT=55185 docker.io/library/python:3.14.8-bookworm@sha256:7ee7e4d4fb42c3ad45b8fdc473b64ec69c3cf6e80ae5d52f6fa55f77fac29027 bash -c (python3 /checkout/.pd-bridge.py /pd-sock/app.sock 55185 &); /checkout/.venv/bin/python -c "from bottle import route, run, template; index = route('/hello/<name>')(lambda name: template('<b>Hello {{name}}</b>!', name=name)); run(host='localhost', port=8080)"
Listening on http://localhost:8080/
sandbox app not ready
```

  Both sides record `START_FAILED`, typed reason `readiness timeout`, last error `sandbox app not ready`. The failure is our contract's, not the app's: the app started and listened, on the wrong port by our instruction.
- **Bottle runtime finding.** The plan selected Python 3.14.8 from the tree's declared `>=3.9`, while the registered baseline note for this fix says it needs Python 3.9–3.11. The selected runtime is outside the range the registration records for the fix; carried here as a finding, separate from the port defect above.
- **Hono's plan selects bun** for its units, which the sandbox does not support — its rows are the package-manager sub-cause of the coverage gap, not the workspaces refusal its pnpm sibling repos hit.
- **Django's train controls** were expected to record the `django-admin` no-runnable-app line; in this run all three Django sides refused earlier, at the snapshot (symlinks under `docs/_theme/`), so the no-runnable-app class has no rows in this pass.
- **Checkpoint-bug sides** (Ghost, Directus, Vendure units) are provisioned from the bug-fix commits in the orchestrator's record, as registered in Addendum 65; the registered files hold no checkpoint SHAs.
- Every row records network mode `none+socket`. No row carries a `measured_result`.

## Latent contract defects

Not observed in this run; contract-fix PR in progress. These defects sit in the registered contracts themselves and would surface only on a run that gets each repo past its earlier refusal:

- **fastify** — the contract's example listens on port 3000; the harness assigns `PORT`, so the listener and the probe would miss each other exactly as Bottle's did.
- **nest** — the contract's sample listens on port 3000 (same defect).
- **django** — the contract's start is `django-admin`, which runs no server at all.
- **hono** — the contract's start is a networked scaffolder (`npm create hono@latest`), not a server from the repo.
- **LMCache** — the contract's server (`lmcache_server`) speaks a binary socket protocol, not HTTP, so no HTTP readiness check could pass against it.

## Registered-set note

The manifest registers 27 units / 42 sides. Its train-control list names three clean-diff controls (django C1, hono C2, fastify C3); a registered file's claim of five is a known shortfall, left as-is for a separate ruling — nothing was substituted in this run.
