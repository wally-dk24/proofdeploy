# Typed-refusal inventory — every refusal string the provisioning path can emit

Plan line: **1.2(j) follow-up** (reviewer's work order, relay
2026-10-15T1805Z §2(b)). Compiled on main `a4c6879` plus this PR's
changes; line numbers are this PR's tree.

Purpose: the readiness harness's H5/J1 judging and its attempt-2
non-retry rule (`readiness/provision.py::_is_not_supported_row`)
key on the row's `typed_reason`. The reviewer rules on the
proposed class of every string below **before** the 25-side
re-run, because H5/J1 depend on them.

## The three proposed classes

- **not supported** — a property of the repo as committed: the
  side cannot be provisioned as committed. H5 skips it, with the
  reason published; the harness never retries it.
- **harness or environment** — a failure of the run or the host,
  not of the repo: retried (attempt 2), never skipped.
- **our gap** — a ProofDeploy support boundary (a table we have
  not extended, a manager/runtime the executor cannot run yet):
  we fix it, never skip the side for it. Retried today; the class
  exists so the ruling can keep these out of the not-supported
  set even where the string sounds like one.

## How the mechanical test works today

`_is_not_supported_row` is exactly
`typed_reason.startswith("not supported:")`. Only strings
carrying that prefix are mechanically terminal today. Rows whose
proposed class is *not supported* but whose string does **not**
carry the prefix are flagged ⚠ — today they are re-provisioned
on attempt 2 and H5 must judge them by their string, not by the
prefix rule, until the reviewer rules.

## 1. Plan refusals — `proofdeploy/provisioning.py`

Emitted as `InstallPlan.refusal` (or `image_refusal`), copied
verbatim into the provision reason by both executors and onto
the readiness row.

| # | String (verbatim; `{…}` = interpolated) | Site | Proposed class |
|---|---|---|---|
| 1 | `declared range {declared_range} not satisfiable by pinned images` (`image_refusal`) | provisioning.py:438 | **our gap** — satisfiability is defined by our pinned-image table. Edge for the ruling: a range no release could ever satisfy (e.g. node `>=99`) is a repo property wearing the same string. |
| 2 | `app.path {declared!r} is not a workspace package in this snapshot (workspace members: {members or 'none'})` | provisioning.py:616 | **not supported** ⚠ — the contract names an app the snapshot does not contain; as committed, there is nothing to provision. No prefix today. |
| 3 | `ambiguous app: {n} candidates` | provisioning.py:621 | **not supported** ⚠ — several workspace members, no `app.path` declared. Resolvable by a contract declaration; as committed, ambiguous. No prefix today. |
| 4 | `not supported: no frozen lockfile found in snapshot for runtime 'python'` (D18) | provisioning.py:1371 | **not supported** — prefix added by this PR. (Fallback site: neither `requirements.txt` nor `pyproject.toml` in the snapshot.) |
| 5 | `python lock generation requires the unit's resolution cutoff (the fix commit's committer time for a bug pair, C's for a clean diff); none was provided` | provisioning.py:1404 | **harness or environment** — the run failed to supply the unit's cutoff; nothing about the repo. |
| 6 | `workspaces not yet supported for runtime 'python'` | provisioning.py:1548 | **our gap** — Python workspace planning is unbuilt (plan 1.2b interface answer). |
| 7 | `workspaces not yet supported` | provisioning.py:1798 | **our gap** — a Node workspace declaration our detector could not resolve into a layout. |
| 8 | `packageManager {pm}@{version} disagrees with the lockfile present in the snapshot ({name}); refusing to install with a different package manager than the one pinned` | provisioning.py:1843 | **not supported** ⚠ — the repo's own declarations conflict; as committed it cannot be installed frozen. No prefix today. |
| 9 | `not supported: no frozen lockfile found in snapshot for runtime 'node'` (D18) | provisioning.py:1857 | **not supported** — prefix added by this PR. This is D18's canonical case (fastify class). |
| 10 | `bun.lockb (the legacy binary bun lockfile) is not supported: bun installs are planned from bun.lock only — regenerate the lockfile with a current bun and commit bun.lock` | provisioning.py:1872 | **not supported** ⚠ — the words "is not supported:" sit mid-string, so `startswith("not supported:")` does **not** match it today: the side is retried and is not mechanically terminal. |
| 11 | `workspaces not yet supported for package manager {manager!r}` | provisioning.py:1889 | **our gap** — workspace support exists for pnpm/npm/bun; another manager's workspace is unbuilt. |
| 12 | `no pinned digest for bun@{version}: bun is pinned by digest (BUN_RELEASE_SHA256) and this version has no pin; refusing to install an unpinned bun` | provisioning.py:1917 | **our gap** — our pin table lacks the version (the work order's own example); fixed by adding the row, as PR #105 did for bun 1.2.20. |
| 13 | `no pinned bun build for architecture {arch!r} (bun@{version})` | provisioning.py:1928 | **our gap** — our pin table lacks the architecture row for a pinned version. |

## 2. Host executor — `proofdeploy/runner.py`

`Provisioner` bail reasons; they become the provision reason and
hence the row's `typed_reason` on host-tier runs.

| # | String | Site | Proposed class |
|---|---|---|---|
| 14 | `not supported: lockfile out of sync with its manifest ({manager}): {manager's first refusal line, verbatim}` (plan 1.2(j), D33) | runner.py:270 (`_lockfile_drift_reason`) | **not supported** — carries the prefix; classified only from the manager's own frozen-install refusal. |
| 15 | `not supported: no frozen lockfile found in {snapshot_dir} for runtime {runtime!r}` (`verify_lockfile` ValueError) | runner.py:672 | **not supported** — D18 string on the exception path; prefix added by this PR. |
| 16 | `no {runtime} binary found on PATH` (`select_runtime_binary`) | runner.py:607 | **harness or environment** — the host lacks the runtime entirely. |
| 17 | `no installed {runtime} satisfies declared range {declared_range!r} (found: {probed})` | runner.py:611 | **our gap** — host-path analogue of #1: the installed set is ours. |
| 18 | `could not pin selected node runtime {runtime_bin!r}` | runner.py:1401 | **harness or environment** — staging the selected runtime onto the step PATH failed on this host. |
| 19 | `not supported: no frozen lockfile found in {snap} for runtime {runtime!r}` (host bail when the plan has no lockfile) | runner.py:1415 | **not supported** — D18; prefix added by this PR. |
| 20 | `lockfile hash required for measured runs (expected_lockfile_sha256 is None)` | runner.py:1448 | **harness or environment** — run configuration, not the repo. |
| 21 | `lockfile mismatch: frozen lockfile changed` | runner.py:1458 | **harness or environment** — the snapshot disagrees with the run's registered expectation; an integrity/config mismatch, not a support property of the repo. |
| 22 | `could not create isolated venv` / `could not create isolated venv: {reason}` | runner.py:1513 / 1544 | **harness or environment**. |
| 23 | `selected {runtime} runtime {runtime_bin!r} not usable via step PATH` | runner.py:1594 | **harness or environment**. |
| 24 | `install step failed: {first error line}` / `install failed: {first error line}` — every non-drift install failure; the tail is the manager's/app's own output (registry and network errors land here and stay untyped, per 1.2(j)'s negative tests) | runner.py:1692 / 1721 | **harness or environment** — retried by design. |
| 25 | `generated lock missing after install: {name}` | runner.py:1743 | **harness or environment** — our REG-A generated-lock bookkeeping check firing after a nominally successful install. |
| 26 | `contract step '{step_name}' failed: {reason}` | runner.py:1793 | **harness or environment** — the app's own contract step failed; genuine app evidence, retried per harness policy. |
| 27 | `contract missing 'start' command` | runner.py:1810 | **our gap** — contracts are authored in this repo; a missing start is our authoring gap, not a repo property. |
| 28 | `missing binary for start command: {filename}` | runner.py:1885 | **harness or environment**. |
| 29 | `start failed: {error}` | runner.py:1887 | **harness or environment**. |
| 30 | `readiness check timed out` | runner.py:1917 | **harness or environment** — the app may be slow, broken, or the host loaded; never a skip. |

## 3. Sandbox executor — `proofdeploy/orchestrator.py`

| # | String | Site | Proposed class |
|---|---|---|---|
| 31 | `sandbox runtime version check failed: {error}` | orchestrator.py:219 | **harness or environment** — the probe of our own pinned image failed to run. |
| 32 | `sandbox image runtime {version} does not match plan selected version {expected}` | orchestrator.py:222 | **our gap** — our pinned image and our plan disagree. |
| 33 | `sandbox image runtime {version} does not satisfy declared version {range}` | orchestrator.py:229 | **our gap** — sandbox analogue of #1/#17: the image row is ours. |
| 34 | `sandbox unavailable: {message}` | orchestrator.py:1203 | **harness or environment** — wraps the sandbox capability/mode failures of §4. |
| 35 | `not yet supported in the sandbox` (detected runtime is neither python nor node) | orchestrator.py:1236 | **our gap** — the sandbox executor supports two runtimes; more are future adapters. |
| 36 | plan `image_refusal` verbatim | orchestrator.py:1252 | → #1 (**our gap**). |
| 37 | `not supported: no frozen lockfile found in snapshot for runtime {runtime!r}` (sandbox bail when the plan has no lockfile; log twin at :1337 without "in snapshot") | orchestrator.py:1343 | **not supported** — D18; prefix added by this PR. The plan's own refusal (#4/#9) is preferred when present; this is the fallback text. |
| 38 | `not yet supported in the sandbox` (plan's package manager outside the sandbox executor's supported set for the runtime) | orchestrator.py:1367 | **our gap** — executor coverage, not a repo property. |
| 39 | `sandbox install failed: {first error line}` / `sandbox install failed: install step {i} ({phase}) exited {code}` — every non-drift sandbox install failure (drift is classified to #14 first, install-phase only) | orchestrator.py:1558 / 1571 | **harness or environment**. |
| 40 | START_FAILED reason = the `SandboxError` text of §4, truncated to 200 chars | orchestrator.py:1651 | **harness or environment**. |
| 41 | `bridge socket missing` | orchestrator.py:1673 | **harness or environment** — our bridge died, not the app. |
| 42 | `readiness timeout` | orchestrator.py:1676 | **harness or environment** — as #30. |

## 4. Sandbox refusals — `proofdeploy/sandbox.py` (`SandboxError`)

Raised before or during a sandbox run; they surface as #34/#40
reasons. All are host/run conditions by construction.

| # | String | Site | Proposed class |
|---|---|---|---|
| 43 | `network_mode=host is refused: the app must not share the host network` | sandbox.py:419 | **harness or environment** — run configuration refused by policy. |
| 44 | `network_mode=isolated requested but the host cannot create isolated networks: {reason}` | sandbox.py:436 | **harness or environment**. |
| 45 | `unknown network_mode {requested!r}` | sandbox.py:440 | **harness or environment**. |
| 46 | `could not create isolated network: {reason}` | sandbox.py:798 | **harness or environment**. |
| 47 | `could not create build network for the install phase: {reason}; measured runs are refused without an isolated install network (the install must not use --net=host)` | sandbox.py:826 | **harness or environment**. |
| 48 | `userns remapping unavailable on this host ({reason}); require_userns=True refuses to run (explicit dev opt-out: allow_no_userns=True)` | sandbox.py:854 | **harness or environment**. |
| 49 | `sandbox start failed: {reason}` | sandbox.py:1179 / 1254 | **harness or environment**. |
| 50 | `sandbox start: no container IP on isolated network` | sandbox.py:1186 | **harness or environment**. |
| 51 | `bridge socket path too long: {path}` | sandbox.py:1214 | **harness or environment**. |
| 52 | `sandbox start: bridge socket {name} never became ready: {error}. Container log tail:\n{tail}` | sandbox.py:1264 | **harness or environment**. |
| 53 | `measured run refused: no container runtime ({reason})` | sandbox.py:1457 | **harness or environment**. |
| 54 | `measured run refused: userns remapping unavailable on this host ({reason}); require_userns=True is the default (explicit dev opt-out: allow_no_userns=True)` | sandbox.py:1461 | **harness or environment**. |
| 55 | `measured run refused: no bridge network for the install phase ({reason}); the install must not use --net=host` | sandbox.py:1467 | **harness or environment**. |

## 5. Readiness harness — `readiness/provision.py`

| # | String | Site | Proposed class |
|---|---|---|---|
| 56 | `runtime not supported` — `row_from_exception`'s mapping of runner.py:330's `cannot detect runtime in {dir}: no package.json or pyproject.toml` | readiness/provision.py:283 | **not supported** ⚠ — a repo with no detectable runtime manifest is a repo property, but the string does not carry the `not supported:` prefix, so the mechanical test does not recognize it today: the side is re-provisioned on attempt 2 (and fails identically). |
| 57 | (mechanism) `row_from_exception` passes any other exception's message through verbatim as `typed_reason`. Named instance: `proofdeploy.yml missing required keys: {keys}` | readiness/provision.py:285; proofdeploy/yml.py:101 | **our gap** — for the named instance: contract files are authored in this repo. (The passthrough itself is unclassifiable in general; each exception text rules on its own.) |
| 58 | `harness error: {message}` — fetch/contract-commit failures before a provision result exists | readiness/provision.py:333 | **harness or environment** — by construction; retried. |

## Scope notes

- `readiness/container_spike.py` (plan 1.12, frozen) emits its
  own `typed_reason` strings (`multi-service not in spike`,
  `no root Dockerfile or compose file in snapshot`, …) in a
  separate harness whose rows do not feed H5/J1. Excluded here.
- PR #102's REG-D refusal strings (`REG-D: …`) are not on this
  main yet; they need rows in this inventory when #102 lands.
- This PR changes **only** the D18 strings (#4, #9, #15, #19,
  #37): each gained the `not supported: ` prefix, remainder
  byte-identical. No other string above was edited; the ⚠ rows
  are surfaced for the reviewer's ruling, not silently fixed.
