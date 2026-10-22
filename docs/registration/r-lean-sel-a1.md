# R-LEAN-SEL Amendment 1 — the unit must fit the contract's layout

Signed by Master, 2026-10-21. Master, verbatim: "Signed" (to `claude/registration-R-LEAN-SEL-A1-draft-2026-10-22.md`).

*Amends R-LEAN-SEL (signed 2026-10-20) for lean walks only. Rule only.*

**New check (L3), after (L2) and before (d), from tree paths only (no diff, test or message is opened).** Every *layout file* exists in the trees of B^, B, fix^ and the fix. The layout files are the paths, relative to the app path, that the store contract's provisioning depends on at the contract's check SHA:
1. the manifest and lockfile the provisioning plan uses (linkding: `pyproject.toml`, `uv.lock`);
2. the source file of every Python module the contract's `env` names (linkding: `DJANGO_SETTINGS_MODULE=bookmarks.settings.prod` → `bookmarks/settings/prod.py`).

The list is computed by code from the contract and the check SHA, written into the walk's journal header before the walk starts, and identical for every commit. A failure is recorded as `L3: predates the contract layout` and the walk moves on (the commit is rejected, not stopped).

**Effect, disclosed.** For linkding, only bugs whose B is on or after 2025-08-23 can be selected. Selection stays blind and mechanical. Older bugs would need a contract per layout era (not planned; a scope decision for later).

**The stopped commit.** `02a0dc0d` is re-evaluated under L3. Its journal keeps the stop and both attempt rounds (round 1: environment, launched from a non-git directory so `gh` could not dispatch; round 2: readiness run `39717331888`).

*Rationale (from the draft): the linkding walk at main `b4b3b09a` stopped at check (d) on fix `02a0dc0d` (2026-08-07); both bug-pair sides (B^ `6775633b`, B `98b9a9c1`, 2024-01-27) failed with `ModuleNotFoundError: No module named 'bookmarks.settings'`; the signed store contract (`94d74b00…`) fits the layout from 2025-08-23 (`b90ae1b` added `pyproject.toml` and `uv.lock`; `bookmarks/settings/` added 2025-03-09 in `2d3bd13`). One contract per repo is a ProofDeploy limit, so under D48/D177 it must not reject candidates without a rule fixed in advance.*
