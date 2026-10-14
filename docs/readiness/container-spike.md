# Container-path readiness spike

## Purpose

The provisioning readiness pass (WAL-112) provisions each
registered app through the native contract path — a contract's
build/start commands inside the sandbox. The pass's table showed
most sides refused long before anything ran: snapshot symlinks
refused, workspaces refused at plan, package managers the sandbox
does not carry. This spike asks the question that table raises,
early and cheaply:

**Can an app be provisioned from its own Dockerfile or compose
file instead — does the container path carry apps the native path
cannot reach?**

It is readiness-only and evidence-only. It does not change the
native provisioner (`readiness/provision.py`) or any measured
path (`proofdeploy/`), and a spike result is never a measured
result: rows are labelled `"container-spike"`, carry no
`measured_result`, and involve no model, no probes of the
measured kind, and no scoring.

## Safety bar

The same bar as the sandbox, verbatim from the work order:

- **Podman:** rootless and userns-remapped. No privileged mode,
  no host network, no socket mounts. Like the sandbox, the spike
  fails closed when a host cannot do userns remapping: no build
  and no run happen at all. Only the explicit dev flag
  `--allow-no-userns-fallback` proceeds without remapping (as
  `--user 65534`, nobody), and every row of such a run records
  `userns_fallback: true`; CI runners remap.
- **Build:** a Dockerfile's RUN steps are untrusted code, held
  to the same bar as the run: userns-remapped
  (`--userns-uid-map` / `--userns-gid-map` `0:1:65536`) and
  `--cap-drop=ALL`, so an escape during the build is not host
  root. Network is allowed during the build only, and only on
  the explicit rootless build network
  `slirp4netns:allow_host_loopback=false` — outbound NAT for
  package registries, no route back to services bound on the
  host's loopback — never podman's default and never the
  host's. The build context is the snapshot only. No secrets,
  no host mounts.
- **Run:**
  - `--net=none` with the existing socket bridge (a forwarder
    inside the container on a Unix socket bind-mounted at
    `/pd-sock`, a host-side TCP proxy in front of it — the
    pattern `proofdeploy/sandbox.py` uses for `none+socket`
    mode);
  - `--cap-drop=ALL` and `no-new-privileges`;
  - no secrets in the environment. The only env the spike ever
    passes is `PORT`, and only when the port source is `$PORT`
    itself; the image's own `ENV` is never overridden.
- A run records how podman was invoked: the run log
  (`spike-logs/spike-<repo>-run-a<attempt>.log`) carries the
  capability line (podman version, userns remapping), the podman
  base command, the run and build user args, and the build
  network mode; every podman invocation
  for a side is logged verbatim in that side's log.

Each safety property has a command-construction test in
`tests/test_container_spike.py`, and each test is plant-proven:
dropping `--net=none` from the run command, dropping
`--cap-drop=ALL` from the user args, dropping the userns remap
or the cap-drop from the build command, pointing the build at
the host network, or adding a host mount to the build command
makes the matching test fail. The build network's loopback
block is proven as a property, not just a flag: a sandbox test
(`test_build_network_blocks_host_loopback`, sandbox-tests CI)
runs a real rootless build whose RUN step tries to reach a
service bound on the host's loopback and must fail to reach it
while a control fetch succeeds; flipping the mode to
`allow_host_loopback=true` is the plant that makes it fail.

### Why the build network changed (WAL-118)

The build originally ran on the native install path's named
`pd-build` netavark bridge. The first real runs from main
(2026-10-14, runs `38647384356` / `38647391627`) showed a
userns-mapped `podman build` cannot join a named netavark
network on podman 4.9.3: every Directus side — all four, both
attempts — died at the Dockerfile's first RUN step with the
same typed reason, verbatim:

```
Error: building at STEP "RUN apk --no-cache add python3 py3-setuptools build-base": setup network: cannot use networks as rootless
```

The failure was the harness's, not the app's (the native path
avoids it because it builds via `podman run`, which can join
the bridge). The fix changed only the build's network: the
userns maps and `--cap-drop=ALL` are unchanged, and there is no
fallback — if the slirp mode is unavailable the build itself
fails and the side is typed `BUILD_FAILED` with the build's
error. The mode is recorded on every row (`build_network`) and
in the run header log.

## Selection (mechanical, decided before any run)

The ten registered repos were listed at their registered SHAs
and at HEAD for a root `Dockerfile` or `compose*.yml`. Every
qualifying repo, up to 3, in manifest order:

| repo | root Dockerfile | root compose | qualifies |
|---|---|---|---|
| vendure-ecommerce/vendure | — | `docker-compose.yml` at HEAD and all 6 registered SHAs | yes |
| directus/directus | `Dockerfile` at HEAD and all 4 registered SHAs | `docker-compose.yml` | yes |
| TryGhost/Ghost | — (see below) | — (see below) | no |
| nestjs/nest, fastify/fastify, honojs/hono, bottlepy/bottle, codeigniter4/CodeIgniter4, django/django, LMCache/LMCache | — | — | no |

Exactly two repos qualify: **vendure** and **directus**. Of
the two, only Directus can reach a real container start —
Vendure's sides type out before any build or run (below).

**Ghost near-miss, recorded exactly:** Ghost ships only variant
names — `Dockerfile.production` and `compose.dev*.yaml` files —
never an exact-name root `Dockerfile` and never a `compose*.yml`.
Under the selection rule as written (root `Dockerfile` or
`compose*.yml`), Ghost does **not** qualify. The rule was decided
before any run and is not bent after the fact; if the rule is to
change, that is a new decision, not this spike's.

Vendure qualifies on its compose file alone — and its root
`docker-compose.yml` is a multi-service development stack
(mariadb, mysql_8, mysql_5, postgres_12, postgres_16, and more),
so every Vendure side is expected to type out as
`multi-service not in spike` without any build or run. That is
the spike working as specified, and it is itself the evidence
for Vendure: its only container artifact is a dev stack, not an
app image. Directus qualifies on its root `Dockerfile`: a
multi-stage `node:${NODE_VERSION}-alpine` build (ARG
`NODE_VERSION=22`), `pnpm install --recursive --offline
--frozen-lockfile`, `EXPOSE 8055`.

## What a run does

For the dispatched repo, per registered side
(`readiness/manifest.json`):

1. Fetch the side's registered SHA (the native harness's clone
   and retrying fetch, reused by import) and materialize the
   tree with `git archive`.
2. Locate the container file: an exact-name root `Dockerfile`
   wins; otherwise a root compose file. Compose with more than
   one service: typed row, stop. Single-service compose must
   declare a `build` section.
3. Parse the Dockerfile: FROM stages with build-ARG resolution,
   the install RUN lines (including heredoc bodies), EXPOSE
   ports, ENV.
4. Build with podman (userns-remapped, `--cap-drop=ALL`, on
   the explicit rootless build network
   `slirp4netns:allow_host_loopback=false`; snapshot-only
   context; no secrets, no build args, no mounts). Record the
   resolved digest of every FROM image the build pulled.
5. Judge the install from the Dockerfile's own lines:
   `--frozen-lockfile`, `npm ci`, `--immutable`,
   `--require-hashes` are frozen; a bare install line is not.
   An unfrozen install is a **finding on the row**
   (`install_frozen: false` with the verbatim line), not a
   failed run.
6. Determine the port — never guess: EXPOSE (Dockerfile, then
   the built image's config) first, then a compose port mapping,
   then a numeric `PORT` the files themselves declare. No
   source: typed row, no run.
7. Run the image: `--net=none` + socket bridge, cap-drop ALL,
   no-new-privileges, userns args as above. The in-container
   forwarder is the sandbox's Python bridge when the image has
   python3, a Node equivalent when it has node; an image with
   neither types out as a start failure. The bridge starts beside
   the image's own ENTRYPOINT/CMD (read from the image config).
8. Probe the side's registered readiness path (from
   `readiness/contracts/<repo>.yml` — for Directus,
   `/server/ping`) over the bridge until the app answers HTTP
   (any status) or a bounded timeout passes.

### A note on symlinks in snapshots

Materialization extracts the `git archive` tar with tarfile's
`data` filter, which refuses absolute paths, `..` escapes and
links pointing outside the tree — but it keeps in-tree relative
symlinks. That keep-in-tree rule is the WO-8.4b rule,
**approved by Master 2026-10-14 (WO-8.4b); the measured path adopts
it only when WO-8.4b merges**. It is acceptable here
only because this spike is readiness-only and evidence-only.
The measured path is unchanged by this spike: it still refuses
links.

## What a row records

One row per (unit, role), in `rows-spike-attempt<N>.json`:

`kind` (`"container-spike"`), `repo`, `unit`, `role`,
`registered_sha`, `status` (`READY` / `BUILD_FAILED` /
`START_FAILED`), `typed_reason` (verbatim-style reason or null),
`from_digests` (every FROM image's resolved digest),
`install_frozen` (true / false / null when there is no install
line to judge), `install_evidence` (the verbatim install line),
`port_source` (`EXPOSE` / `compose` / `$PORT` / null), `port`,
`network_mode` (`none+socket`), `build_network`
(`slirp4netns:allow_host_loopback=false`), `userns_fallback`
(true only
when the run proceeded under the dev no-userns flag), `attempt`.

Per-side build and run logs land in `spike-logs/`, and the
workflow uploads rows + logs as one artifact. A failure in one
side never kills the others; the rows file is always written.

## How to dispatch

GitHub Actions → `readiness-container-spike` → Run workflow:
pick the repo and the attempt (2 = first pass plus a re-spike of
the non-READY sides, same attempt semantics as the readiness
workflow). Or:

```
gh workflow run readiness-container-spike.yml --ref <branch-or-main> \
  -f repo=directus/directus -f attempt=2
```

Directus builds are large (a pnpm monorepo build per side);
a full attempt-2 dispatch can take hours. That is expected —
do not cancel a run for slowness.

## Out of scope

- **Multi-service setups** — typed `multi-service not in
  spike`, never built or run.
- **Databases** — no service composition, no seeded data; if an
  app needs a database beside it, that is a multi-service shape
  and out.
- **Auth** — the probe is an unauthenticated readiness GET; no
  bootstrap, no credentials, no contract auth modes.
- **The native provisioner** — `readiness/provision.py` and
  everything under `proofdeploy/` are untouched; the spike
  imports helpers (clone/fetch, the sandbox bridge and
  capability probe, the contract loader) and changes none of
  them.
- **Image-only compose services and Dockerfile variants**
  (`Dockerfile.production`, `Dockerfile.dhi`, …) — the spike
  reads the exact-name root files the selection rule named.
