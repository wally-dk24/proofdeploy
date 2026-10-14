# WO-8 Milestone: Lock Rules Made Real, and Credential Providers

**Date:** 2026-10-13
**Repository:** https://github.com/wally-dk24/proofdeploy
**Status:** Closed. Final merge: PR #61, `abb7dbf60953ee8e300ca67218824f19db7b54e9` (tree byte-identical to the accepted head `5c1564c`; merge check by the reviewer). This document is the consolidated record of the WO-8 arc.


---

## 1. What WO-8 delivered

WO-8 closed the two gaps the WO-5 milestone carried forward: the Python lock rule (REG-A) became enforced, recorded behavior instead of a registration text only, and ProofDeploy learned to authenticate against the apps it provisions through declared credential providers (WO-8.3), under the rule Master fixed in Addendum 43.

- **WO-8.1 — lock facts and the uv path (PR #59, merged `a310b45831e3ae01edec06bc582e64a204b69091`, WAL-97).** The run record carries the lock facts for the provisioned side, and the sandbox's uv bootstrap is hash-pinned in the repository (`proofdeploy/uv_bootstrap_requirements.txt`; wheel sha256 recorded in the work order's evidence), so a measured run's Python install path is reproducible rather than whatever the network serves that day.
- **WO-8.1b — the remaining lock managers in the sandbox (PR #64, merged `0f932170b65429e6af33ee1da5ed05ad2088d335`, WAL-100).** The sandbox's supported Python managers became `pip`, `uv`, `poetry`, and `pdm`; the poetry (2.5.1) and pdm (2.29.2) bootstraps are hash-pinned like uv's. Review: plants 8 of 8 caught (Addendum 54).
- **WO-8.1c — build-backend facts, per REG-A Addendum 1 (PR #66, merged `a1fe8df019d8a4435c4c27577fa8cdc8578188ef`, WAL-104).** Every record now carries, per side, the build backend's name and version and the unit cutoff applied — or the typed limitation where the tool cannot apply one (pdm) or the backend is itself hash-pinned (poetry). The rule text it implements is REG-A Addendum 1 (PR #68, merged `6208c3ac196b4595ff2e53918d33d3ce401b7187`, signed by Master 2026-10-13), an addendum to the original Python lock rule registration (PR #52, merged `159e562`). The parent rule document is unchanged by the addendum.
- **WO-8.2 — the no-credential fixture fix, REG-B in code (PR #60, merged `d9e1cb92f130ff2839bc996a2705943cccadfd35`, WAL-98).** The fixture template is selected by whether the contract declares a credential (`fixture.template_filename(declares_credential)`), and the leak check recomputes against the registered template hash (`fixture.REGISTERED_TEMPLATE_SHA256`), so a fixture that declares no credential is built from the no-credential template and the answer-key guard measures against the template actually used. Review: plants 9 of 9 caught (Addenda 51–52).
- **WO-8.3 — credential providers (PR #61, merged `abb7dbf60953ee8e300ca67218824f19db7b54e9`, WAL-99).** Section 2. The last WO-8 sub-order; its merge closes WO-8 (WAL-89).

Across every WO-8 merge, the pinned files never changed: `proofdeploy/model_client.py` at `55b7d93283d511b681f7af24dd78aafc0cc750d652ed965f94fe7a4ab1d28598`, `proofdeploy/author_prompt_v7.md` at `fa72a0e6c4a1fa9fb204986e08c8eef4b11fc13786f41389ecf2f42ea8555cf3`, `proofdeploy/fixture_template.txt` at `0994d1e367ddb3c679e750451dd67eab3a3d974fc1620b7f750216cdcdf0323b`, and `proofdeploy/fixture_template_nocredential.txt` at `6cb8edafa3211ccd3c275648bf3a2cc5ddadbbc876aa65d8105be5186b4188e9`, verified at every accepted head and (for #61) at the merge commit.

---

## 2. WO-8.3: credential providers

A contract may declare one auth provider in `proofdeploy.yml`; the executor obtains credentials from the disposable instance it provisioned and keeps them live for the probe run. Four modes: `register` (generate an identity and register it with the app), `login` (log in with bootstrap credentials and dig the token from the response at a declared JSON path), `refresh` (login, then renew through a refresh endpoint with `${REFRESH_TOKEN}` substitution as expiry nears), and `ghost_jwt` (sign a Ghost Admin API JWT from a key given directly or in a key file inside the checkout). `auth.bootstrap_env` records environment **names only**; unknown modes and keys are refused at load; `ttl_seconds` must be a positive integer.

### The rule it runs under (Addendum 43, Master's wording)

"No secrets in the container" means **customer secrets**. Per-run credentials ProofDeploy generates for a disposable instance may enter its install and app containers only if: (1) generated fresh per side and per run, random, never reused, never derived from a customer value; (2) used only against the disposable instance ProofDeploy provisioned; (3) redacted from every record and in the forbidden-values set for prompt and bundle; (4) never written to `proofdeploy.yml` or any committed file, recorded only as "generated" with env names; (5) customer secrets stay out of every container, unchanged. The wording is carried verbatim in `docs/adr/0009-orchestrator.md` and `docs/adr/0010-contract-credential-provider.md`.

Redaction is enforced, not asserted: generated values, provider-minted tokens, and captured values join the record's redaction set; the sandbox log itself is a scrubbing log bound to the registered secret values, after the first sandbox cycle caught generated values in plaintext in podman command echoes (fixed at `833c75a`, before the proofs were re-run).

### The Addendum 62 round (two blockers, both reproduced by the reviewer)

- **Auth-path host rewrite.** The validator accepted an auth path without a leading `/`; because the provider URL is built by concatenation and WO-7.3's write grants allow any host for a provisioned executor, a path like `@evil.example/x` would have sent the login POST and the generated credentials to a third party. Fixed in two layers: the validator requires every auth path to start with `/` (not `//`, no `@`, no backslash, no scheme), and the executor refuses — INCONCLUSIVE `auth`, before sending — any provider URL whose parsed host and port differ from the target's. Tests per mode assert both the refusal and that no request reaches a second listening server (24 validator cases, 3 executor second-listener tests; 30 failed / 1 passed before, 31 passed after).
- **`key_file` symlink escape.** The key-file check was lexical; a seed-planted symlink could make the host read a file outside the checkout and return its content as the key. Fixed: symlinks are refused in every path component, the resolved path must stay inside the snapshot, the file must be regular, and its size is capped at 65,536 bytes; failures surface as `key_error` (INCONCLUSIVE `auth`). Tests cover a symlinked file, a symlinked directory component, and an oversized file.
- The same round bumped `SCHEMA_VERSION` to **1.5.0** (section 3) and corrected the plant map: the grant-vs-non-provisioned-target plant is covered by the item-1 tests, not by the cross-side credentials test.

The reviewer's plants against the fixed head were all caught (symlink check off: 2 failed; executor host guard off: 3; scrub disabled: 1; deterministic generator: 2; redaction-set omission: 1), except one survivor he accepted as equivalent: removing the validator's `"@"` clause, which the remaining clauses already cover (a path like `/x@y` cannot change the host).

### The proofs, and what they do and do not prove

Two real-app sandbox proofs run in the provisions-apps shard, plus golden-contract proofs of every auth mode:

- **Directus v12.5.0** (upstream commit `4f77933bf9551cac6b289576078783e189b8f4e3`), `login` + `refresh`, SQLite: the side reaches READY and an authenticated `GET /users/me` returns 200; a driven near-expiry renewal takes the refresh endpoint and the token changes.
- **Ghost v6.69.0** (upstream commit `056f6bfa657f2175b924106fcb25c3fc245bb4e7`), `ghost_jwt`, SQLite: READY, and an authenticated `GET /ghost/api/admin/site/` returns 200.

**These proofs prove the auth providers, not adapter coverage.** Both apps install from their npm tarballs with a test-generated lockfile; nothing in them exercises the Node adapter against the apps' own repository layouts, workspace files, or lockfiles. The Ghost proof in particular takes the npm route deliberately: the tarball's `pnpm-workspace.yaml` trips the adapter's "workspaces not yet supported" refusal, and the two follow-on pnpm failure layers (`ERR_PNPM_LOCKFILE_CONFIG_MISMATCH`, then `ERR_PNPM_IGNORED_BUILDS` for better-sqlite3/re2/dtrace-provider) are recorded in the PR #61 finding note, accepted by the reviewer as written. That finding — ProofDeploy should align itself to the probed app's own dependency choices, generically, per Master's direction — is WO-8.4's subject, drafted by the reviewer after this merge; the true-workspace refusal stays.

One harness lesson from this merge's own CI: Directus access tokens carry `iat`/`exp` at second granularity with no session claim, so a refresh answered in the same second as the login returns a byte-identical access token (the refresh token rotates). The proof's rotation assertion failed twice on head `d9ef055` for exactly this reason — verified against a local directus@12.5.0 — and the harness now waits past the next second boundary before driving the refresh (head `5c1564c`, test-only, +13 lines). The provider was correct in both failed runs.

---

## 3. Schema changes

- **`schema_version` 1.4.0 → 1.5.0** (WO-8.3, PR #61). No field changes in this bump: it names the union of what the 1.4.0 line accumulated without a bump — the WO-8.1 lock facts, the WO-8.1c build-backend facts, and WO-8.3's `auth_bootstrap`. Records from 1.0.0 through 1.4.0 still load; a loader test guards the 1.4.0 shape.

---

## 4. Lessons

- Verify a check from the command's own exit and output. In the Addendum 62 round, a corrupted background delivery reported mypy clean when it was not; CI caught it, the misreport was disclosed to Master and the reviewer, and the fix round's claims were re-verified by direct runs and by the reviewer's own mypy run.
- A proof can race the proven system's clock. The Directus failure looked like a product regression, survived one rerun identically, and was neither: it was a one-second issuance granularity in the app, found by reproducing the app locally instead of re-running CI.
- Concurrency groups serialize what looks parallel. On successive pushes, the sandbox run for a new head waits pending behind the superseded head's run in the same workflow+branch group; the PR page shows no checks for a pending run, which reads as "sandbox didn't run" until job start times are checked.

---

## 5. Open items carried forward

- **WO-8.4** (reviewer to draft): generic alignment to the probed app's pnpm 10+ configuration — settings-only workspace files, build approvals — keeping the refusal for true workspaces.
- **WAL-110**: split the provisions-apps sandbox shard by family and hold a ~15-minute ceiling per shard; the shard's 12 tests now accumulate ~24 minutes of wall time.
- **WO-6** (WAL-90): the blind run, next in the MVP sequence now that WO-5 through WO-8 are closed.
