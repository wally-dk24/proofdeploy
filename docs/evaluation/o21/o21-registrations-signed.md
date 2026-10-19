# O21 registrations: R-SEL, R-BUNDLE, R-METHOD (SIGNED by Master 2026-10-19, D242)

Signed together with skill registration v5 and prompt header v8 (`skill-v5-author.md` sha256 `75d52a77…`, `prompt-v8-header.md` sha256 `52ae0da3…`). Master, verbatim: "I approve your changes. And take this as I signed off." The slot-11 `json_path` line in skill v5 (item 20) was folded in on his instruction: "the skill file tightening you mentioned, can we fold this into this work and run?"

## R-SEL — Selection: exclude fixes that are only observable on an upgrade path
*Amends REG-C item 7 (with Addendum 2) and Held-out Addendum 1's walk; rule only; no unit's status changes (audit above).*

1. **Migration path.** A changed path is a *migration path* when, case-insensitively, any path segment is `migrations`, `migration`, `migrate` or `alembic`, or its file name contains `migrat`.
2. **New condition on check (b).** Check (b) (D169: at least one changed path is SOURCE) passes only if at least one SOURCE path that the fix changes is **not** a migration path. A fix whose SOURCE changes are all migration paths fails check (b), recorded as `b: upgrade-path only`.
3. **Scope.** Applies to every selection walk from its registration onward: the held-out walk (1.6), any future REG-C selection and any fresh checkpoint selection. Units already in a registered list are judged by the same test once, mechanically, and the result is published; per the audit, none fails it.
4. **Already-measured run.** The WO-6 run `39164959697` stands as published (`no_catch`, D229); it is not re-run and not replaced.
5. **Checkpoint.** The 4 registered checkpoint bugs are tested by this rule from their fix's changed-path list only (no diffs, tests or messages opened). If one fails, the reviewer brings Master a decision before any checkpoint run; the checkpoint is not changed silently.

## R-BUNDLE — Author input: the app's route declarations (prompt/bundle v8)
*Amends the registered author bundle (prompt v7, pin `fa72a0e6`). Together with header v8 and skill v5 (signed with this document) it makes prompt v8. The scoring is unchanged.*

1. **Which files.** In the author's snapshot (B's tree), within the unit contract's `app.path` subtree, excluding TEST and DOC paths (D169 classes) and any path with a segment `node_modules`, `vendor`, `dist`, `build`, `site-packages`, `.venv` or a migration path (R-SEL 1), a file is a *route declaration* when:
   - **Python** (`.py`): its name is `urls.py`, `routes.py`, `routers.py` or `routing.py`; or it contains a line matching `^\s*@\w+\.(route|get|post|put|patch|delete|api_route)\(` or `^\s*\w+\s*=\s*(APIRouter|Blueprint)\(`.
   - **Node** (`.js .mjs .cjs .ts .mts`): its file name contains `route`, `router` or `routing`, or ends in `.controller.ts/.js` or `.resolver.ts/.js`; or it contains a match of `\b(app|router|server|fastify|api)\.(get|post|put|patch|delete|all|route)\(\s*['"`/]`. Also `.graphql` / `.gql` schema files.
2. **Order and cap.** Files are ordered by the length of the longest common leading path (in segments) with any file B touches, longest first, then by path. Full texts are added in that order up to **32 KB in total**; the first file that would exceed the cap and every later file is left out, and a `route_map_truncated: true` flag plus the list of included and omitted paths goes into the bundle manifest. Included files are also always listed in the file list, whatever its 200-file truncation.
3. **Presentation.** One new bundle section, "Route declarations (selected mechanically; may be incomplete)", after the touched files. A route file that is already shown as a touched file is not repeated; it is named in the section as "shown above". Other prompt changes are those of prompt header v8 (signed with skill v5).
4. **Leak check.** Route files come from B's own tree, so the answer-key and leak checks apply to them unchanged.
5. **Stacks.** These patterns cover Stage 1 (Node and Python). Each later stack adds its patterns by its own registration.
6. **Pins.** The prompt-builder pin changes; its new sha256 is recorded in the registration when committed. Golden units and every later measured run use v8.

## R-METHOD — Evidence-cited assertions, checked by the harness, with one correction round
*Amends the author output contract and validation (prompt v8, with R-BUNDLE and the header v8 text signed with skill v5). The model, provider, temperature, scoring rules (Addendum 11 rule 6 and clean-diff counting) and every registered selection rule are unchanged. It applies to both author arms (skill and no-skill) and to `verify`.*

1. **Evidence on every assertion.** Every assertion (status, body, header, db) carries a top-level `evidence: {"source": <a file path shown in the prompt, or "fixture">, "quote": <exact text, at most 300 characters>}`.
2. **Mechanical check (deterministic, no model), per assertion, in this order:**
   - `evidence_missing`: no evidence object, or a missing or non-string source or quote;
   - `evidence_not_found`: the whitespace-normalised quote does not occur in the named source as shown in the prompt (the fixture text; the full text of a touched file; a route declaration file). Listed-but-unshown files cannot be quoted;
   - `evidence_self_justifying`: the quote occurs in the source only within (or across) lines the change under review adds or alters, including changed tests;
   - `evidence_ambiguous`: for a boolean expected value, the quote contains both `true` and `false` (as words, any capitalisation);
   - `evidence_direction_mismatch`: the quote does not contain the expected value's text. Booleans match `true`/`false` as words, any capitalisation; numbers as digits; strings case-insensitively; `matches` needs a quoted string the pattern matches; `db` `count` needs the number. Status codes and object or list values need no literal (the quote must still be real and not self-justifying).
3. **One correction round, same model.** If any assertion is rejected, or any block is invalid under the probe schema, the harness sends exactly one follow-up. Prompt = the original prompt + "\n\n---\n\nYour previous answer:\n\n" + the first answer + "\n\n" + a message listing each rejected assertion (probe number, assertion type, reason) or schema error, and stating: only these assertions are affected; keep every other assertion and probe exactly as it was; for each, quote a shorter exact passage from outside the changed lines that contains the expected value for this record only, and remove an assertion only if no such passage exists; return the complete corrected set; this is the only correction. The second answer is final.
4. **What runs.** In the final answer, a rejected assertion is removed. A probe left with no assertion, or failing the schema, is an invalid block, recorded verbatim and never sent, and counts exactly as an invalid block counts today (Addendum 11 rule 6). A transport failure of the correction call is retried once after 60 s. If it still fails, the first answer is used with its rejected assertions removed, and the failure is recorded.
5. **Record.** The evidence record adds: both raw answers, the correction prompt's sha256, `correction_used`, the per-assertion check results of both answers, and both answers' finish_reason and usage. The probe-schema scan for code content excludes `evidence` text (quotes of source code are expected). Record schema minor bump.
6. **Tested before signature:** the O21 replay v2 (D241), CAUGHT 10/11 with 11/11 right direction on the 11 slots that all missed under skill v3/v4.
