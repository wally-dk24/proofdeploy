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
| 8 | `not supported: packageManager {pm}@{version} disagrees with the lockfile present in the snapshot ({name}); refusing to install with a different package manager than the one pinned` | provisioning.py:1979 | **not supported** — the repo's own declarations conflict; as committed it cannot be installed frozen. Prefix added by the part-2 PR per the ruling (Master, D50); remainder byte-identical. |
| 9 | `not supported: no frozen lockfile found in snapshot for runtime 'node'` (D18) | provisioning.py:1857 | **not supported** — prefix added by this PR. This is D18's canonical case (fastify class). |
| 10 | `bun.lockb (legacy binary lockfile): ProofDeploy plans bun installs from bun.lock only (our gap) — regenerate the lockfile with a current bun and commit bun.lock` | provisioning.py:2009 | **our gap** — ruled our gap (bun itself installs frozen from `bun.lockb`; the limit is our planner's) and reworded by the part-2 PR so the text no longer contains "is not supported"; the regenerate-and-commit advice is preserved. |
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
| 56 | `runtime not supported` — `row_from_exception`'s mapping of runner.py:330's `cannot detect runtime in {dir}: no package.json, pyproject.toml or requirements.txt` | readiness/provision.py:283 | **our gap** — ruled our gap: detection is root-only (package.json → pyproject.toml → requirements.txt), so an app under `app.path` or a non-Node/Python repo lands here; that is our coverage. The underlying message was fixed by the part-2 PR to name `requirements.txt`, which detection also checks. The harness string itself carries no prefix and the side is re-provisioned on attempt 2. |
| 57 | (mechanism) `row_from_exception` passes any other exception's message through verbatim as `typed_reason`. Named instance: `proofdeploy.yml missing required keys: {keys}` | readiness/provision.py:285; proofdeploy/yml.py:101 | **our gap** — for the named instance: contract files are authored in this repo. (The passthrough itself is unclassifiable in general; each exception text rules on its own.) |
| 58 | `harness error: {message}` — fetch/contract-commit failures before a provision result exists | readiness/provision.py:333 | **harness or environment** — by construction; retried. |

## 6. REG-D dated sample resolution — `proofdeploy/provisioning.py` (PR #102)

Rows added by the part-2 PR, per the ruling: PR #102's REG-D
strings needed rows in this inventory once #102 landed. These
were classed directly by the ruling (no separate proposal).

| # | String (verbatim; `{…}` = interpolated) | Site | Ruled class |
|---|---|---|---|
| 59 | `REG-D: sample {path} has a lockfile ({name}); REG-D does not apply — the sample must install frozen` | provisioning.py:2171 | **our gap** — the correct behavior there is to install the sample frozen. Not urgent: no registered nest unit's sample has a lockfile. |
| 60 | `REG-D: the contract declares dated sample resolution ({token}) but no ISO 8601 UTC lock cutoff is available for this unit; refusing to run an undated sample install (REG-D D6)` | provisioning.py:557 | **harness or environment** — the analogue of #5: the run failed to supply the unit's cutoff. |
| 61 | `REG-D: the contract declares dated sample resolution ({token}) but the unit's lock cutoff could not be read ({error}); refusing to run an undated sample install (REG-D D6)` | provisioning.py:549 | **harness or environment** — same D6 family as #60. |

## 7. Submodule materialization — `proofdeploy/submodules.py` (plan 1.2(l), PR #109)

Every string the submodule step can raise, with the classes the
reviewer ruled in R4 of the #109 changes round (relay
2026-10-15T2215Z, D48/D50). Submodule URLs and contents come
from the repo under test, so they are untrusted input. Four
rulings shape the table:

- the recorded commit is unavailable → **not supported** (D46),
  carried by `SubmoduleUnavailableError`; the materialize layer
  re-raises every such refusal in the one wrapper form (#62), so
  the detail strings (#63–#68) are what varies;
- `.gitmodules` paths refused by the path rules, and links
  escaping a fetched tree, take **the class `create_snapshot`
  already gives an ADR 0012 refusal** — a plain `ValueError`
  (fixture.py:359, `archive member escapes snapshot dir:
  {member.name}`) or the `SnapshotLinkError` itself
  (fixture.py:157, message at fixture.py:164) — **neither
  carries the `not supported:` prefix**, so neither is
  mechanically terminal today: the side is retried and H5 must
  judge it by its string;
- a tree over the size cap is **our gap** (the cap is our
  limit); its string must not start with `not supported:`;
- a relative URL whose superproject origin is missing or
  scp-style, and transient fetch failures, are **harness or
  environment** (`SubmoduleFetchError`, a `RuntimeError` — the
  harness's retry class, never a typed row).

| # | String (verbatim; `{…}` = interpolated) | Site | Proposed class |
|---|---|---|---|
| 62 | `not supported: submodule {path} at {sha} unavailable from {url}: {detail}` — the wrapper every typed submodule refusal surfaces in; the detail is one of #63–#68 | submodules.py:137 (`_typed`); re-raise at :719 | **not supported** — carries the prefix (D46). |
| 63 | detail: git's first error line, when the fetch's stderr matches a "remote does not serve this" marker (transport refused by the protocol rules, `couldn't find remote ref`, repository not found, private repo with no credentials sent, …); the fetch layer's own form is `not supported: submodule at {sha} unavailable from {url}: {first line}` | submodules.py:228 | **not supported** — the recorded commit is unavailable (D46/R4). |
| 64 | detail: `the remote did not serve the recorded commit` (the fetch reported success but the object did not arrive) | submodules.py:243 | **not supported** (D46/R4). |
| 65 | detail: `submodule URL is not a clean https URL: {url!r}` — the R1 pre-fetch validation: after resolution the URL must match `^https://` with no whitespace or control characters and no leading `-`, refused before any git process runs (the fetch layer's own message is `not supported: submodule URL refused before fetch: {url!r}`) | submodules.py:281 | **not supported** — non-https by the protocol rules (R1/R4). |
| 66 | detail: `recorded commit is not a 40-character lowercase hex SHA: {sha!r}` — the R1 pre-fetch validation of the recorded SHA, likewise before any git process runs (fetch layer's own message: `not supported: submodule commit refused before fetch: {sha!r}`) | submodules.py:286 | **not supported** (R1/R4). |
| 67 | detail: `nested submodules exceed the depth limit of {MAX_DEPTH}` (MAX_DEPTH = 5) | submodules.py:692 | **not supported** — carries the prefix via #62. ⚠ Flagged for the reviewer: carried unchanged from the accepted build; the R4 ruling does not name this string. |
| 68 | `not supported: submodule {path} at {sha} unavailable from (no url in .gitmodules): submodule has no url in .gitmodules` (a listed gitlink whose `.gitmodules` entry has no URL) | submodules.py:702 | **not supported** — carries the prefix via #62. ⚠ Flagged for the reviewer: carried unchanged from the accepted build; the R4 ruling does not name this string. |
| 69 | `submodule path does not normalize inside the checkout: {path!r}` (absolute path or a `..` component in a `.gitmodules` path, paired with a gitlink or not) | submodules.py:402 / 419 | **ADR 0012 plain `ValueError`** — the class `create_snapshot` gives the same refusal (fixture.py:359). No `not supported:` prefix (R4). |
| 70 | `submodule path does not name a directory: {path!r}` | submodules.py:425 | **ADR 0012 plain `ValueError`** (as #69). |
| 71 | `submodule path has a symlinked parent: {walked}` | submodules.py:435 | **ADR 0012 plain `ValueError`** (as #69). |
| 72 | `submodule path's parent is not a directory: {walked}` | submodules.py:439 | **ADR 0012 plain `ValueError`** (as #69). |
| 73 | `submodule path is a symlink in the snapshot: {path!r}` | submodules.py:445 | **ADR 0012 plain `ValueError`** (as #69). |
| 74 | `submodule path exists in the snapshot and is not a directory: {path!r}` | submodules.py:449 | **ADR 0012 plain `ValueError`** (as #69). |
| 75 | `submodule tree entry escapes the submodule directory: {rel!r}` (defensive: an absolute or `..` entry name inside a fetched tree; git forbids these by construction) | submodules.py:503 | **ADR 0012 plain `ValueError`** (as #69). |
| 76 | `snapshot symlink refused ({reason}): {path} -> {target}` — a symlink inside a fetched tree that is absolute or escapes its tree (the WO-8.4b rule), validated as one map before any link is created; the `SnapshotLinkError` itself, re-based at the raise so `{path}` names the path from the snapshot root | submodules.py:597; class fixture.py:157, message fixture.py:164 | **`SnapshotLinkError`** — the class `create_snapshot` gives an ADR 0012 link refusal. No `not supported:` prefix (R4). |
| 77 | `submodule {full_path}: its tree is {total} bytes, over the {MAX_TREE_BYTES}-byte limit ProofDeploy sets for one submodule (our gap)` (MAX_TREE_BYTES = 512 MiB; the cap is summed from the tree listing and enforced before anything is written, R3) | submodules.py:522 | **our gap** — the cap is our limit (R3/R4); plain `ValueError`, no `not supported:` prefix. |
| 78 | `harness or environment: relative submodule URL {raw_url!r} for submodule {path} cannot be resolved: the superproject origin is missing or not an https URL ({base_url!r}); the origin is this checkout's configuration, not the repo's` | submodules.py:382 | **harness or environment** — the origin is our checkout's configuration, not the repo's (R4); `SubmoduleFetchError`, retried. |
| 79 | `submodule fetch of {url} at {sha} failed: {first error line}` — every fetch failure whose stderr matches no #63 marker (network and registry errors land here and stay untyped) | submodules.py:233 | **harness or environment** — retried by design. |
| 80 | `submodule fetch of {url} at {sha} timed out after {FETCH_TIMEOUT_SECONDS}s` (300 s) | submodules.py:220 | **harness or environment**. |
| 81 | The remaining `SubmoduleFetchError` strings, each naming the git operation that failed on this host (`git init --bare failed for submodule fetch of {url}: {stderr}` :192; `cannot read the tree of fetched submodule commit {sha} from {url}: {stderr}` :253; `git ls-tree … failed …` :320 / :479; `cannot parse {gitmodules}: {stderr}` :350; `unparseable ls-tree entry/size for {sha}: …` :492 / :513; `git cat-file --batch cannot serve {oid} …` / `… stream ended early …` / `… exited {code} …` :548 / :565 / :585) | submodules.py (as cited) | **harness or environment** — our git plumbing failed; retried, never a typed row. |

## Scope notes

- `readiness/container_spike.py` (plan 1.12, frozen) emits its
  own `typed_reason` strings (`multi-service not in spike`,
  `no root Dockerfile or compose file in snapshot`, …) in a
  separate harness whose rows do not feed H5/J1. Excluded here.
- PR #102's REG-D refusal strings (`REG-D: …`) landed with
  #102; their rows are §6 (#59–#61), added by the part-2 PR.
- Part 1 of the follow-up (PR #107) changed **only** the D18
  strings (#4, #9, #15, #19, #37): each gained the
  `not supported: ` prefix, remainder byte-identical. The ⚠
  rows were surfaced for the reviewer's ruling, not silently
  fixed; the ruling is recorded at the end of this document.
- Submodule materialization strings (plan 1.2(l)) are
  §7 (#62–#81), added by PR #109's changes round with
  the classes the reviewer ruled in R4 (relay
  2026-10-15T2215Z).
- Part 2 (this PR) applies that ruling: the Ruling section
  below, the §6 REG-D rows, the #8 prefix (D50), the #10
  rewording, and the #56 message fix. Site line numbers are
  this PR's tree for the rows it changed (#8, #10, #56,
  #59–#61) and the part-1 tree elsewhere.

## Ruling (reviewer, 2026-10-15)

Recorded by the part-2 PR from the reviewer's ruling (relay
2026-10-15T2050Z §3). This section governs: where a row's
proposed class above and its ruled class here differ, the
ruled class is the one in force.

**Governing rules:**

- **R1.** The mechanical test stays
  `typed_reason.startswith("not supported:")`. The prefix
  goes **only** on strings in a not-supported class Master has
  approved: D18, D33, and anything he approves now (D50 for
  #8).
- **R2.** Any string not ruled here, including future
  passthroughs (#57), defaults to **harness or environment**:
  retried, never skipped. Nothing becomes not-supported by
  sounding like it.
- **R3.** An **our gap** row never removes a unit. Under H5
  the unit stays pending until we fix the gap, then it's
  re-provisioned.

**Agreed as proposed:** 1, 4–7, 9, 11–26, 28–55, 58.

- On #1, an impossible range (no release could ever satisfy
  it) is the only exception. If one occurs, the reviewer
  reclassifies that row with evidence.
- On #21 and #24 to #26, agreed: these are retried.

**Changed by the ruling:** #2 → our gap (the contract is
ours); #3 → our gap (we declare `app.path` in our contracts;
REG-C selection's own "ambiguous app" exclusion is a
separate, registered mechanism and is unaffected); #10 → our
gap (bun itself installs frozen from `bun.lockb`; reworded by
this PR, see §1); #27 → our gap (agreed); #56 → our gap
(detection is root-only, so this is our coverage; the message
fix is in §5); #57 → R2 (the passthrough defaults to harness
or environment; the named `proofdeploy.yml missing required
keys` instance is our gap).

**Brought to Master and approved:** #8 → not supported
(Master, D50) — the repo's own toolchain refuses on the same
principle as D33. The prefix is applied by this PR, remainder
byte-identical.

**REG-D strings (PR #102, §6):** #59 → our gap; #60 and #61
→ harness or environment (#60 is the analogue of #5).

| # | Ruled class |
|---|---|
| 1 | our gap |
| 2 | our gap |
| 3 | our gap |
| 4 | not supported |
| 5 | harness or environment |
| 6 | our gap |
| 7 | our gap |
| 8 | not supported (Master, D50) |
| 9 | not supported |
| 10 | our gap |
| 11 | our gap |
| 12 | our gap |
| 13 | our gap |
| 14 | not supported |
| 15 | not supported |
| 16 | harness or environment |
| 17 | our gap |
| 18 | harness or environment |
| 19 | not supported |
| 20 | harness or environment |
| 21 | harness or environment |
| 22 | harness or environment |
| 23 | harness or environment |
| 24 | harness or environment |
| 25 | harness or environment |
| 26 | harness or environment |
| 27 | our gap |
| 28 | harness or environment |
| 29 | harness or environment |
| 30 | harness or environment |
| 31 | harness or environment |
| 32 | our gap |
| 33 | our gap |
| 34 | harness or environment |
| 35 | our gap |
| 36 | our gap |
| 37 | not supported |
| 38 | our gap |
| 39 | harness or environment |
| 40 | harness or environment |
| 41 | harness or environment |
| 42 | harness or environment |
| 43 | harness or environment |
| 44 | harness or environment |
| 45 | harness or environment |
| 46 | harness or environment |
| 47 | harness or environment |
| 48 | harness or environment |
| 49 | harness or environment |
| 50 | harness or environment |
| 51 | harness or environment |
| 52 | harness or environment |
| 53 | harness or environment |
| 54 | harness or environment |
| 55 | harness or environment |
| 56 | our gap |
| 57 | our gap |
| 58 | harness or environment |
| 59 | our gap |
| 60 | harness or environment |
| 61 | harness or environment |

**Submodule rows (§7, PR #109) — ruled in R4 of the #109
changes round (reviewer, relay 2026-10-15T2215Z):** #62–#68
are the D46 not-supported family. #69–#76 are ruled by R4 to
the ADR 0012 class `create_snapshot` already gives (a plain
`ValueError` / the `SnapshotLinkError` itself, §7) — a class
outside the three above; they are entered here as **harness
or environment** per R2's default (the mechanical treatment
they share: retried, never skipped, never terminal), with §7
carrying the class in force. The reviewer to confirm the
mapping at the #109 review. #77 is the R3/R4 our-gap row.

| # | Ruled class |
|---|---|
| 62 | not supported |
| 63 | not supported |
| 64 | not supported |
| 65 | not supported |
| 66 | not supported |
| 67 | not supported |
| 68 | not supported |
| 69 | harness or environment |
| 70 | harness or environment |
| 71 | harness or environment |
| 72 | harness or environment |
| 73 | harness or environment |
| 74 | harness or environment |
| 75 | harness or environment |
| 76 | harness or environment |
| 77 | our gap |
| 78 | harness or environment |
| 79 | harness or environment |
| 80 | harness or environment |
| 81 | harness or environment |

The prefix-coverage test
(`tests/test_lockfile_drift.py::test_typed_refusal_prefix_coverage`)
reads this table: every row ruled **not supported** must
correspond to an emitted string starting with
`not supported: `, and no other ruled row's string may start
with it.
