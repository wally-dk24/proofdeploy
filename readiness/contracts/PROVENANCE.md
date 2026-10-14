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
`983ae280ace8`, nestjs/nest `fa152aff7ecd`, fastify/fastify
`e222f53aa267`, honojs/hono `588ec13fa68e`, django/django
`adeef108afa7`, LMCache/LMCache `96e69e09ea34`, bottlepy/bottle
`b73bd1db5b7a` (author's bare-clone FETCH_HEAD; a parallel
checkout showed `8ec5fe29420d` — the README example the
contract quotes is identical in both), codeigniter4/
CodeIgniter4 `8e2db880c621`.

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
- `readiness`: `"/"` — HEAD's own dev healthcheck,
  `compose.dev.yaml`: a fetch of `http://localhost:2368`
  treated as healthy on any status <500 — an unauthenticated
  root fetch, matching the runner's any-non-error rule.
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
unmodified — the contract sets only the `DB` switch that
`dev-config.ts` itself reads (below); the file is never
overridden. Authored from HEAD `CONTRIBUTING.md` + README +
`packages/dev-server` config; the registered stage evidence
names the same seed file (`populate-dev-server.ts`,
`packages/create`'s `copy-assets` build step feeding the root
build).

- `build`: `CONTRIBUTING.md` (HEAD root): "[Bun](https://bun.sh/)
  — the canonical package manager for this repo", "Always use
  `bun install` inside this repo", steps 1–2: "`bun install`",
  "`bun run build`"; root `package.json`: `"build": "lerna run
  build"`, tree carries `bun.lock`. KNOWN CONSEQUENCE,
  deliberate: the sandbox refuses bun plans (supported node
  managers: npm, pnpm, yarn — bun stays refused), so HEAD-shaped
  Vendure trees type out at plan time; older registered trees
  plan by their own lockfiles. The readiness rows report this
  as-is; the contract is not bent toward a manager HEAD does
  not document.
- `seed`: `CONTRIBUTING.md` step 4 ("Populate test data"):
  "cd packages/dev-server" / "bun run populate";
  `packages/dev-server/package.json`: `"populate": "node -r
  ts-node/register -r dotenv/config populate-dev-server.ts"`;
  `packages/dev-server/README.md`: "Test data can be populated
  by running the `populate` script."
- `start`: `packages/dev-server/package.json`: `"start": "node
  ../cli/dist/cli.js start all"`, invoked from the dev-server
  directory per `CONTRIBUTING.md` step 5. Judgment call
  (author-flagged): the documented `bun run dev` runs a
  Portless/watcher supervisor unsuited to a fixed-port sandbox
  readiness check; the package's verbatim `start` script is
  used instead.
- `readiness`: `"/health"` — `packages/dev-server/scripts/
  dev-workflow.mjs` polls `` `${apiOrigin}/health` `` as the
  "API health endpoint", unauthenticated, accepting 2xx–3xx:
  Vendure's own readiness check at HEAD.
- `env`: `DB: sqlite` — `packages/dev-server/dev-config.ts`:
  `const dbType = process.env.DB || 'mysql';` — HEAD's default
  is MySQL (an external server), so one documented switch is
  required; the sqlite branch is a self-contained file database
  needing no credentials (`AGENTS.md`: "Switch DB with env
  var: `DB=postgres` or `DB=sqlite`"). Non-secret config only;
  the MySQL branch's hardcoded dev values were not copied.
- No `migrate`: no migrate script exists in
  `packages/dev-server/package.json`; migration is part of the
  start entry itself (`index.ts`: `runMigrations(devConfig)`,
  and `dev-config.ts` returns `synchronize: true`).

## nestjs/nest — generic (HEAD README + config)

The framework repo ships no runnable root app (root
`package.json` has no `start` script); the contract targets the
repo's own canonical sample application.

- `build`: root `package.json` — `"build": "tsc -b -v
  packages"` with the root `package-lock.json` (`npm ci`);
  then `sample/01-cats-app` installed and built via `--prefix`.
  `--legacy-peer-deps` is the repo's own sample-install mode:
  `tools/gulp/tasks/samples.ts` (the repo's `install:samples`
  runner) uses `'npm install --legacy-peer-deps'` per sample
  and itself invokes via `--prefix`. Sample `package.json` —
  `"build": "nest build"`.
- `start`: `sample/01-cats-app/package.json` — `"start:prod":
  "node dist/main"`, run via `npm --prefix` (chosen over
  `"start": "nest start"`: the build already compiles the
  sample to `dist/`). No root `app.controller.ts` exists in
  the sample; its only controller is the cats controller.
- `readiness`: `"/cats"` — `sample/01-cats-app/src/cats/
  cats.controller.ts` (`@Controller('cats')`, `@Get()`)
  serves it unauthenticated, proven by the sample's own HEAD
  e2e spec (`cats.e2e-spec.ts` expects 200 on `GET /cats`
  with no auth headers). Recorded tension: the sample's
  `main.ts` is `await app.listen(3000)` — a hardcoded port,
  not the runner-assigned one; the rows will show what that
  does, and no `env` was invented to hide it.

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
  publint"`, `"packageManager": "pnpm@12.6.0"`; the tree
  carries `pnpm-lock.yaml`. Expected plan outcome, recorded in
  advance: HEAD also carries `pnpm-workspace.yaml`, and the
  Node adapter refuses workspace trees, so Hono sides are
  expected to type out at plan time with that refusal — the
  contract is faithful anyway.
- `start`: `"npm create hono@latest"` — JUDGMENT CALL
  (author-flagged, verified at HEAD): no runnable app exists
  in this repo. The README snippet is a library export
  (`export default app`), `package.json` has no
  start/dev/serve script, and the full HEAD tree contains no
  `examples/` directory (verified by recursive `ls-tree`);
  the benchmarks tool is a Bun+bombardier harness, not an app.
  The start is therefore the README Quick Start's own command
  (`README.md`: `npm create hono@latest`). It scaffolds a NEW
  project rather than serving this repo, so a typed
  start/readiness failure is the expected, legitimate outcome;
  no server file was invented to disguise that.
- `readiness`: `"/"` — `README.md`'s only route:
  `app.get('/', (c) => c.text('Hono!'))`.

## Contract-fix pass (2026-10-14) — appended entries

Each entry: old value, new value, the HEAD file and lines the new
value comes from, and why. HEAD here is each repo's HEAD on
2026-10-14, re-read for this pass.

### bottlepy/bottle — HEAD `8ec5fe29420d14998c0d86885083983eaea8b839`

- `start`:
  - old: "python -c \"from bottle import route, run, template; index = route('/hello/<name>')(lambda name: template('<b>Hello {{name}}</b>!', name=name)); run(host='localhost', port=8080)\""
  - new: "python -c \"import os; from bottle import route, run, template; index = route('/hello/<name>')(lambda name: template('<b>Hello {{name}}</b>!', name=name)); run(host='localhost', port=int(os.environ['PORT']))\""
  - source: `README.rst:48-54` — the README's "Hello World" example
    (`from bottle import route, run, template` at :48, the
    `/hello/<name>` route at :50, `run(host='localhost', port=8080)`
    at :54).
  - why: the example is unchanged except the port, which now comes
    from the `PORT` environment variable the harness sets, so the
    listener binds the port the readiness check probes. The
    hardcoded 8080 made the app listen on a port nothing probed.

### nestjs/nest — HEAD `bb3d572080ba5c3a34b65fcc39ca598c324cb32f`

- `build`:
  - old: "npm ci && npm run build && npm --prefix sample/01-cats-app install --legacy-peer-deps && npm --prefix sample/01-cats-app run build"
  - new: "npm ci && npm run build && npm --prefix sample/34-hmr-esm install --legacy-peer-deps && npm --prefix sample/34-hmr-esm run build"
  - source: `sample/34-hmr-esm/package.json` — scripts `build`
    (`nest build`) and `start:prod` (`node dist/main`), the same
    script names the old sample's `package.json` carries.
  - why: the sample swap below; the build steps keep their shape.
- `start`:
  - old: "npm --prefix sample/01-cats-app run start:prod"
  - new: "npm --prefix sample/34-hmr-esm run start:prod"
  - source: `sample/34-hmr-esm/src/main.ts:11` —
    `await app.listen(process.env.PORT ?? 3000);` (the old sample's
    `sample/01-cats-app/src/main.ts:9` is
    `await app.listen(3000);`).
  - why: this sample reads `PORT` from the environment, so the
    listener binds the probed port; it serves the same cats
    controller shape (`sample/34-hmr-esm/src/cats/cats.controller.ts:10`,
    `@Controller('cats')`), so `readiness` stays `/cats`.

### django/django — HEAD `80c997fc23b08897d8611b5439c1877485a4676f`

JUDGMENT CALL (flagged): the registered entry point is a framework
command, not an app. This change generates a project with the
repo's own documented scaffolding inside the build step and starts
that project; the generated site routes only `admin/`, so the
readiness path `/` is answered by Django's default 404 response,
not a routed view. Recorded here so the call is visible.

- `build`:
  - old: "python -m pip install ."
  - new: "python -m pip install . && django-admin startproject readiness_site ."
  - source: `docs/intro/tutorial01.txt:47-52` — "run the following
    command to bootstrap a new Django project:
    `$ django-admin startproject mysite djangotutorial`"
    (also `docs/ref/django-admin.txt:1424-1427`).
  - why: a checkout of the framework has nothing to serve; the
    documented scaffolding creates the runnable project in place.
- `start`:
  - old: "django-admin"
  - new: "python manage.py runserver 0.0.0.0:$PORT --noreload"
  - source: `docs/intro/tutorial01.txt:110-115` — the documented
    dev-server command `$ python manage.py runserver`; the bind
    address carries the harness's `PORT`. The generated project's
    URLconf is `django/conf/project_template/project_name/urls.py-tpl:20-22`
    (`path('admin/', admin.site.urls)` only).
  - why: `django-admin` alone starts nothing; `runserver` is the
    repo's own way to serve the generated project over HTTP.

### LMCache/LMCache — HEAD `65320eaa8ebc5e5039ace88d8bb39a88f6859a63`

- `start`:
  - old: "lmcache_server"
  - new: "lmcache server --http-host 0.0.0.0 --http-port $PORT"
  - source: `pyproject.toml:47-50` registers both entry points;
    `lmcache_server` is `lmcache.v1.server.__main__:main`, a raw
    socket server (`lmcache/v1/server/__main__.py:30-32` binds a
    `socket.SOCK_STREAM` and parses a binary protocol — it cannot
    answer HTTP). The HTTP entry point is the `lmcache server`
    subcommand: `lmcache/cli/commands/server.py:2` ("launch the
    LMCache request server and HTTP API"), serving the FastAPI app
    in `lmcache/v1/multiprocess/http_server.py:180,246-253`; its
    `--http-host` / `--http-port` flags are defined at
    `lmcache/v1/multiprocess/config.py:705-735` (defaults
    127.0.0.1 / 8080 at :241-245).
  - why: the old entry point speaks a binary socket protocol, so
    no HTTP readiness check could ever pass against it; the new
    one is the repo's own HTTP API server, bound to the harness's
    `PORT` through its own flags.
- `readiness`:
  - old: "/"
  - new: "/healthcheck"
  - source: `lmcache/v1/multiprocess/http_apis/info_api.py:41-44` —
    `@router.get("/healthcheck")`, documented there as the
    "Health check endpoint for k8s liveness/readiness probes".
  - why: the repo's own documented readiness route replaces the
    root path.
