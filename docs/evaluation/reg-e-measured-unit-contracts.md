# REG-E: contract and fixture for measured units

Signed by Master 2026-10-16 ("I sign REG-E as drafted."); decision D105.

1. **Source.** For every measured unit (checkpoint bug and control, held-out bug and clean, and the units of WO-6's selected app), the contract is its repo's file in the committed contract store (`readiness/contracts/`) at the main SHA the run is dispatched from. It is never read from the repo's own tree. Its sha256 is recorded in every record, and one contract serves the author bundle and every side of the unit.
2. **Content.** The `fixture:` describes only the running instance: seed data, accounts, credentials, token scope and feature-flag state. It names no feature area and nothing about any bug (rule 2). Every statement, including the fixed sentences the frozen template adds, is literally true for the unit's own seed and auth provider at every side's SHA.
3. **Verification.** Before commit, the reviewer checks each statement against upstream source at every side's SHA and reads the text as rendered by the real renderer (sha256 recorded). A fresh agent that has not seen the bug list independently checks the rendered text for accuracy and neutrality.
4. **Auth proof.** A contract that declares `auth:` is proven on every side before any measured run: an authenticated request is accepted twice, with a fresh credential where the provider mints one, and an unauthenticated request is refused.
5. **Approval.** Checkpoint fixtures are signed off by Master. Held-out and WO-6 fixtures are approved by the reviewer under this rule, with the independent check, and reported to Master. Each is committed before any measured run of its unit; a change after a measured run invalidates that run.
6. **Readiness.** Readiness renders the author text for every side and records its sha256; a render failure is our gap.
7. **Applies** from its merge to every unit not yet measured. It supersedes the 2026-10-09 approved fixture hashes (Vendure `6e739fca…`, Directus `55aa188b…`), which stay in the record, marked superseded.
