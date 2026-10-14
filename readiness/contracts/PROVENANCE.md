# Contract provenance (Addendum 64/65)

One `proofdeploy.yml` per repo, identical across all of that repo's
commits. Authored ONLY from the repo's HEAD README + config files
(HEAD checkouts taken 2026-10-13), plus, for the two approved-fixture
repos, the registered fixture basis noted per contract. No target
commit's tree, bug, fix diff, or fix test was read at any point.
Every contract below was validated with the repo's own loader
(`proofdeploy.yml.load_contract`) before commit.

HEADs at authoring time: TryGhost/Ghost `d507bc2741fa`,
directus/directus `8e140f942508`, vendure-ecommerce/vendure
`983ae280ace8`, nestjs/nest `fa152aff7ecd`, django/django
`adeef108afa7`, LMCache/LMCache `96e69e09ea34`, bottlepy/bottle
`8ec5fe29420d`, codeigniter4/CodeIgniter4 `8e2db880c621`.

Per-field sources are quoted verbatim from the HEAD files named.
"Judgment call" flags are the authors' own, kept deliberately: the
readiness rows are the evidence, and a flagged synthetic field is
data, not a defect to hide.

## TryGhost/Ghost — held fixture basis, NO `auth:` (Addendum 65 (5))

- `build`: root `package.json` scripts — `"bootstrap": "pnpm install
  && bash tools/scripts/init-submodules.sh && ..."` and `"build":
  "pnpm nx run-many -t build"`. README.md: "Install dependencies
  and build the project (`pnpm run bootstrap` also runs the build)";
  "To run the Ghost Admin and Server locally".
- `migrate`: root `package.json` — `"migrate:db": "cd ghost &&
  ... knex-migrator migrate"`.
- `start`: root `package.json` — `"dev": "nx run
  ghost-monorepo:dev:host"`. The `server__host`/`server__port`
  prefix is Ghost's documented env-override configuration syntax
  for its server settings; `$PORT` is the runner-assigned port,
  expanded by the sandbox's `bash -c` start. Judgment call
  (author-flagged): `pnpm dev` is the monorepo's dev orchestration
  for the repo itself; production start is not a root script at
  HEAD (`"start"` is `None` in root `package.json`).
- `readiness`: `"/"` — the site's root answers unauthenticated;
  the loader-proven path class the WO-8.3 Ghost work used.
- No `env:`: the contract's earlier draft's env keys were not
  HEAD-documented; the port/host ride on the `start` prefix.

## directus/directus — approved fixture (registered hash `55aa188b…`)

Derived from the approved fixture description as realized in the
accepted WO-8.3 real-app proof contract
(`tests/test_wo83_sandbox.py`, `DIRECTUS_CONTRACT`), cross-checked
against HEAD; NOT derived from HEAD documentation, because the
package at HEAD is a monorepo whose npm entry the proof pinned.

- `build: "true"`, `start: "directus start"`, `migrate: "directus
  bootstrap"`, `readiness: "/server/ping"` — the WO-8.3 precedent
  verbatim.
- `env`: `DB_CLIENT: sqlite3`, `DB_FILENAME: /checkout/data.db` —
  precedent verbatim (non-secret config).
- `auth`: mode `refresh`, precedent verbatim — the approved
  fixture's own provider declaration. `bootstrap_env` names only
  (`ADMIN_EMAIL`, `ADMIN_PASSWORD`, `SECRET`); values are generated
  per run by the orchestrator. No credential value appears in the
  contract.

## vendure-ecommerce/vendure — approved fixture (registered hash `6e739fca…`)

Condition honored: each commit uses its own `dev-config.ts`,
unmodified — the contract declares no DB env and no config
override. Authored from HEAD README + `packages/dev-server`
config; the registered stage evidence names the same entry files
(`populate-dev-server.ts`, `packages/create`'s `copy-assets` build
step feeding the root build).

- `build`: root `package.json` — `"packageManager"` is unset and
  the tree carries `bun.lock`; scripts: `"build": "lerna run
  build"`. README.md documents the Bun-based monorepo flow.
  KNOWN CONSEQUENCE, deliberate: the sandbox refuses bun plans
  (`supported node managers: npm, pnpm, yarn — bun stays
  refused`), so HEAD-shaped Vendure trees type out at plan time;
  older registered trees plan by their own lockfiles. The
  readiness rows report this as-is; the contract is not bent
  toward a manager HEAD does not document.
- `migrate`: `packages/dev-server/package.json` scripts contain
  `"codegen"` (GraphQL schema codegen the dev-server imports at
  startup). Judgment call (author-flagged): the dev-server's own
  DB migrations run inside the app boot (`src/` migration runner),
  so no standalone DB-migration command exists at HEAD; codegen
  is the documented pre-start step placed in the migrate slot.
- `seed`: `packages/dev-server/populate-dev-server.ts` exists at
  HEAD; README.md: "the `populate-dev-server.ts` script in the
  `packages/dev-server` directory" populates "an initial set of
  products, customers and orders". Invoked with `bunx tsx`, the
  README's own runner for the dev-server entry files. (An earlier
  draft's `bun run populate` / `bun run start` named scripts that
  do NOT exist in `packages/dev-server/package.json` — caught in
  authoring review and corrected to the files themselves.)
- `start`: `packages/dev-server/index.ts` is the dev-server entry;
  README.md ("Start the server": `bunx tsx index.ts` run from the
  dev-server directory after building all packages).
- `readiness`: `"/"` — COORDINATOR AMENDMENT to the author's
  `/admin-api` draft, on the author's own evidence:
  `packages/dev-server/index.ts` mounts the landing page at the
  root ("Serve the Vendure logo & landing page from the root"),
  while a bare GET on the GraphQL `/admin-api` returns 400, and
  the runner's readiness check treats an HTTP error as not-ready.
  `/` proves the same process is serving HTTP, with a 200.
- No `env:`: HEAD's `packages/dev-server/dev-config.ts` reads
  `DB_*` variables with SQLite defaults; nothing needs declaring,
  and the approved condition forbids overriding the config.

## nestjs/nest — generic (HEAD README + config)

The framework repo ships no runnable root app (root
`package.json` has no `start` script); the contract targets the
repo's own canonical sample application.

- `build`: root `package.json` — `"build": "tsc -p
  tsconfig.json"` with the root `package-lock.json` (`npm ci`);
  then `sample/01-cats-app` installed and built via `--prefix`.
  `--legacy-peer-deps` is the repo's documented sample-install
  mode: root README.md's sample instructions use `npm install
  --legacy-peer-deps`, and the sample's own `postinstall` echoes
  that flag. Sample `package.json` — `"build": "nest build"`.
- `start`: `sample/01-cats-app/package.json` — `"start:prod":
  "node dist/main"`, run via `npm --prefix`.
- `readiness`: `"/cats"` — `sample/01-cats-app/src/cats/
  cats.controller.ts` (`@Controller('cats')`) serves the route
  unauthenticated; the sample's root controller also answers
  `/`. (The sample binds no port itself: its `main.ts` listens
  on `process.env.PORT ?? 3000`, and the runner assigns PORT.)

## django/django — generic (HEAD README + config)

- `build`: HEAD `pyproject.toml` packaging — `"python -m pip
  install ."`.
- `start`: `"django-admin"` — JUDGMENT CALL (author-flagged):
  Django-the-framework ships no project, no `manage.py`, and no
  documented runnable server entry at HEAD; `django-admin` is
  the framework's own documented CLI entry point
  (`django/__init__.py` / `pyproject.toml` `[project.scripts]`).
  It prints usage and exits, so Django sides are expected to type
  as start failures with that in the log — the faithful outcome
  for a framework repo, recorded rather than papered over with
  an invented project.
- `readiness`: `"/"`.

## LMCache/LMCache — generic (HEAD README + config)

- `build`: `"pip install -e . --no-build-isolation"` — HEAD
  README.md documents the source install; `--no-build-isolation`
  because HEAD's `setup.py` imports `torch` at build time.
- `start`: `"lmcache_server"` — `pyproject.toml`
  `[project.scripts]`: `"lmcache_server = "lmcache.v1.server.
  __main__:main"`. Author-flagged: that server speaks LMCache's
  own TCP protocol, not HTTP, so the readiness poll is expected
  to time out; it is HEAD's only documented server entry point
  and is used unchanged.
- `readiness`: `"/"`.

## bottlepy/bottle — generic (HEAD README + config)

- `build`: `"pip install ."` — HEAD `setup.py` / `pyproject.toml`
  packaging for the single-file framework.
- `start`: the README's own hello-world example
  (`README.rst`: the `route` / `run(host='localhost',
  port=8080)` snippet serving `<b>Hello {{name}}</b>!`), inlined
  as a `python -c` so the framework's documented example is what
  runs. Judgment call (author-flagged): Bottle ships no app; the
  example is the repo's canonical runnable form.
- `readiness`: `"/hello/world"` — the example's route, exercised
  with the README's own example name.

## codeigniter4/CodeIgniter4 — generic (HEAD README + config)

- `build`: `"composer install --no-interaction"` —
  `composer.json` at HEAD (`"name":
  "codeigniter4/codeigniter4"`, `"require": {"php": "^8.2",
  ...}`); README.md documents the Composer-based install.
- `start`: `"php spark serve --host=0.0.0.0 --port=$PORT"` — the
  framework's documented local-server command (HEAD user guide,
  `spark` at repo root); `$PORT` is runner-assigned and expanded
  by the sandbox's `bash -c` start.
- `readiness`: `"/"` — the stock app's welcome page answers
  unauthenticated.
- Expected outcome, recorded in advance (Addendum 64): PHP is not
  a sandbox-detected runtime, so these sides type as "runtime
  not supported" at plan time. The contract is faithful anyway.

## fastify/fastify — generic (HEAD README + config)

- `build`: root `package.json` — `"build:validation": "node
  build/build-error-serializer.js && node
  build/build-validation.js"`, fastify's own codegen/build step
  (there is no compile step and no umbrella `"build"` script;
  the lint suite was considered and rejected — the contract's
  build is the repo's build).
- `start`: `"node examples/simple.js"` — `examples/simple.js`
  exists at HEAD, builds the framework from the repo root
  (`require('../fastify')`), registers the `/` route returning
  `{hello: 'world'}`, and listens on port 3000 (the runner
  assigns the app port via its start binding; a fixed-port
  example is the repo's canonical runnable form — author-flagged
  judgment call, kept because every other example is a
  benchmark, TLS, or plugin variant of the same shape).
- `readiness`: `"/"` — the example's only route.

## honojs/hono — generic (HEAD README + config)

- `build`: root `package.json` — `"build": "pnpm run
  remove-dist && vp pack && pnpm run copy:package.cjs.json &&
  publint"`; the tree carries `pnpm-lock.yaml`. Expected plan
  outcome, recorded in advance: HEAD also carries
  `pnpm-workspace.yaml`, and the Node adapter refuses workspace
  trees, so Hono sides are expected to type out at plan time
  with that refusal — the contract is faithful anyway.
- `start`: `"node --experimental-strip-types
  examples/with-static.ts"` — hono is a runtime-agnostic
  framework core with no server binary and no `start` script;
  `examples/with-static.ts` at HEAD is a complete Hono app
  served through the repo's own `@hono/node-server` adapter,
  answering `/` with 200, and the examples import the framework
  from source, matching Node's type-stripping execution the
  repo's own tooling assumes. Judgment call (author-flagged):
  an earlier draft's `npm create hono@latest` was rejected in
  authoring review — it is an interactive scaffolder for a NEW
  project and never runs this repo.
- `readiness`: `"/"`.
