# ADR-0010: Contract-Declared Credential Provider

**Date:** 2026-10-10
**Status:** Accepted

## Context

The WO-5 orchestrator hard-coded a `POST /register` on every READY target,
expecting `{"token": ...}`. This crashed on any app without open
registration — which includes the WO-5 acceptance app and every eval and
held-out repo. The registration was also a write outside the probes, and
its failure was an exception, not a typed INCONCLUSIVE.

Auth must come from the contract, declared per repo, through WO-3's
credential providers. No default registration.

## Decision

1. **No default credential.** The orchestrator never POSTs `/register`
   unless the contract declares it. If the contract declares no
   credential:
   - No token is used.
   - `${AUTH_TOKEN}` is an unknown placeholder.
   - A probe using it is INCONCLUSIVE `probe` (fail-closed, never a crash).

2. **Registration as a declared provider.** The contract declares a
   top-level, typed `auth:` mapping (validated at load by
   `load_contract`, before anything runs). The `register` mode:
   ```yaml
   auth:
     mode: register
     path: /register
     method: POST
     username_field: username
     password_field: password
     token_json_path: token
   ```
   - A random username and password are generated per run.
   - The request is recorded and logged as a write (it's a POST).
   - Both the password and the token are secrets for the record's
     fail-closed redaction.
   - A provider failure is INCONCLUSIVE `auth` in the evidence record,
     never an exception.

3. **Provider modes.** WO-3's `CredentialProvider` gains a `register` mode
   alongside `ghost_jwt` and `refresh`. The executor's `_ensure_credential`
   dispatches to `_provide_register`, which follows the same fail-closed
   pattern: validate config, check write allowed, log the request, extract
   the token, or return `InconclusiveReason.AUTH`.

## Consequences

- Apps without auth work without crashing. The fixture template's
  "Your token: ${AUTH_TOKEN}" line is misleading for no-auth apps; a
  separate append-only amendment will make the token line conditional.
- The contract's `fixture` mapping carries the provider config as a JSON
  string. It is not rendered into the author-facing description (only the
  five template fields are), and it is leak-checked.
- Tests: no-auth app (no crash), declared registration provider (works),
  provider failure (INCONCLUSIVE auth).
