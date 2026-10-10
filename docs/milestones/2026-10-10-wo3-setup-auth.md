# WO-3 Milestone: Setup and Auth

**Date:** 2026-10-10
**Branch:** `feat/runner-setup-auth` (feature head `b096089`, merged as PR #40, merge commit `d2b2b9e`)
**Accepted by:** Orchestrator (Master's other agent) on 2026-10-09
**Work order:** WO-3 from brief 2026-10-10

## What WO-3 delivered

Setup capture, placeholder substitution, and credential handling for the probe executor (`proofdeploy/setup_auth.py`, wired into `proofdeploy/executor.py`).

### Original findings (5)

1. **Unknown placeholders rejected.** `${NAME}` must name a known binding: a contract `env` key, a fixture key, `AUTH_TOKEN`, or a capture declared by an earlier setup step. Anything else raises `UnknownPlaceholder` and the probe is INCONCLUSIVE (reason `probe`) — never sent literally. (Previously unknown placeholders went out literally and could pass.)
2. **Captures wired into later steps.** Setup `http` steps accept a `capture` spec (`{"TOKEN": {"from": "body", "json_path": "token"}}` or `{"from": "header", "name": "X-Token"}`). After a 2xx response, `run_setup` extracts captures into the `SetupContext`; later setup steps, the act, and the assertions all substitute through it. The probe schema (`probe.py`) validates the `capture` field.
3. **Missing capture is INCONCLUSIVE (`probe`).** A `${NAME}` naming a declared capture that was never set (the capturing step failed, or the step order is wrong) raises `MissingCapture`.
4. **Fixture `${AUTH_TOKEN}` always wins.** Precedence, highest first: a refreshed/provider token, then the fixture's token, then setup-step captures. A setup step that captures `AUTH_TOKEN` cannot override the fixture's — `SetupContext.add` re-asserts the effective token after every capture.
5. **Credential provider (was: refresh hook).** See the blocker fix below — the original "refresh on 401" design was replaced.

### Blocker fix: proactive credential provider

The first WO-3 implementation refreshed the token when the probe's own request got a 401, then re-sent the request. The orchestrator reproduced three failures against a live server:

- A correct "access denied" check came back INCONCLUSIVE (the 401 it asserted was swallowed by the refresh).
- Another correct check came back as a false **FAIL**.
- A POST request was sent twice, but only one write was logged.

A 401 from the target is often the very thing being measured — this would have corrupted the access-control probes the eval set depends on (Directus bug #2, Vendure bug #4, control C3).

**The fix:** a `CredentialProvider` that mints or refreshes tokens **before** requests are sent, never in reaction to a response. A 401 from the target is the probe's data for the assertions — never a refresh signal — and no request is ever re-sent.

- **`ghost_jwt` mode:** mints a short-lived Ghost Admin API JWT per request from the fixture's admin key, via pure-stdlib HMAC-SHA256. Ghost has no refresh endpoint.
- **`refresh` mode:** calls the refresh endpoint, but only when there is no token or a provider-issued token nears expiry. Fixture tokens have no known expiry and are never refreshed proactively.
- **Provider failure** is INCONCLUSIVE (reason `auth`) before the request is sent.
- **Every provider request** goes in the record, and is logged as a write when it uses a write method.

### Setup AUTH_TOKEN fix

After the blocker fix, setup steps still rejected a provider-issued `${AUTH_TOKEN}`: `apply_strict` ran before `_ensure_credential`, and `AUTH_TOKEN` wasn't a known name until the provider had run. The act did it in the right order. Fixed so that:

- when `credential_config` is set, `AUTH_TOKEN` is declared known from the start;
- in setup, the provider runs before substitution, as the act does — each request carries the token minted for it.

### Cleanups folded in

- **Removed fail-open paths.** `substitute_bindings` and `SetupContext.apply` (lenient substitution) were deleted; only `substitute_strict`/`apply_strict` remain.
- **Stale docstring fixed.** `setup_auth.py` no longer says the provisioner handles `harness_app`; those steps are INCONCLUSIVE (`environment`) until WO-5.
- **Misplaced comment fixed.** The "only `environment` may be downgraded to warn" policy note moved from the 5xx setup-typing line to the `InconclusiveReason` enum definition.
- **Flaky test fixed.** `test_create_snapshot_feeds_bundle_with_recorded_sha` mutated the global `EXPECTED_SKILL_HASH` without restoring it (order-dependent failure ~1 in 3 runs). Fixed with try/finally. Plane ticket WAL #74.

## Real-run evidence

### Orchestrator's verification runs

The orchestrator re-ran every live check on the fixed code against a live server:

- Ghost probes can now use the minted token in both their setup steps and the main request.
- Each request gets its own fresh token, and each one passed an independent signature check the orchestrator wrote.
- The earlier fixes still hold: "access denied" checks pass, nothing is sent twice, and a failed credential fetch stops before the request goes out.

### Wally's verification runs

The three required cases, against a live local HTTP server:

- a probe asserting 401 with no auth gives **PASS**;
- a probe asserting 401 with an expired fixture token gives **PASS**;
- a POST that gets a 401 is sent **exactly once** (server-side hit counter confirms; the write is logged once).

Plus new provider tests: Ghost JWT mint verified against an independent signature check, proactive refresh before the act, and refresh failure giving INCONCLUSIVE (`auth`) with the act never sent.

## Tests

- **204 tests pass** (195 existing + 9 new for the provider and the setup-token fix), 84.83% coverage (floor 80%). Ruff check + format clean, mypy clean.

## Review history

- `e5bebf4` — WO-3 initial (5 findings: placeholders, captures, AUTH_TOKEN precedence, refresh hook)
- `e2b0d0a` — blocker fix: proactive credential provider replaces 401 refresh-and-retry
- `b096089` — setup steps accept provider-issued `${AUTH_TOKEN}`
- Merged as PR #40 (merge commit `d2b2b9e`), accepted by orchestrator on 2026-10-09
