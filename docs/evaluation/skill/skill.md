# Probe-Authoring Skill

**How to use this document:** Read all of it before opening the author bundle. It teaches how to decide what is correct and how to probe for it, not what bug to find. Every numbered item carries a provenance tag: `[PROC]` (general procedure), `[DEV: T<n>]` (illustrated by TRAIN dev-set bug T<n>). An untagged item is a drafting error.

---

## 0. The rule that decides every expected value

R1. The change you are reviewing is a claim, not a fact. It may be the bug. Never use the changed lines as evidence that the new behavior is correct. `[PROC]`

R2. Every expected value you assert, including status codes, must come from evidence outside the changed lines: the fixture's description of the instance and its data, unchanged tests, docs, comments, field and function names, or code the change did not touch. Tests the change adds or edits are part of the claim, not evidence. `[PROC]`

R3. Quote that evidence word for word in the assertion's `evidence` field. The harness checks every quote before anything runs: it must exist in the bundle, it must not come from a changed line (marked `<<CHANGED>>`), and it must contain the value you expect. An assertion that fails this check is sent back to you once to fix; if it is still wrong, it is dropped and counts against you. `[PROC]`

R4. If nothing outside the changed lines tells you what a value should be, do not assert that value. Probe other behavior around it that the evidence does settle. `[PROC]`

---

## 1. Procedure, in this order

1. **Facts first.** Before reading the diff, read the fixture and the unchanged code, tests and docs near the touched files. In your prose reasoning (before the JSON blocks), write down each fact you will rely on, with its exact quote and where it is. For example: *the fixture says "account 7 has balance 250"*. `[PROC]`

2. **Then the change.** Read the diff. List each behavior it changes or adds, one per line, in your own words. `[PROC]`

3. **Compare.** For each listed behavior, check it against your facts. If the change agrees with them, your probes expect that behavior. If the change contradicts them, your probes expect what the facts say, and they should fail against this change. If no fact covers it, apply R4. `[PROC]`

4. **Who and how.** For each behavior, ask which actors can reach it (an authenticated user, an unauthenticated request, a scheduled job, another service), in which states (an existing record, a flag set, a prior request completed) and with which inputs. Trace each untrusted input from the boundary (path, query, header, body) to where it is used, and note where it is validated, normalized, escaped or passed through unchanged. `[PROC]`

5. **Probe both sides.** When the change adds a guard, check or validation, probe an input that should pass it, an input that should be rejected, and the boundary between them. `[PROC]`

6. **Shape.** Every probe is setup / act / assert. Setup creates the preconditions; act performs one operation; assert checks only what is observable from outside: status codes, headers, response bodies, read-only database state. Never assert on internals (variables, call counts, log lines). `[PROC]`

7. **Pairs.** For each behavior, write at least two probes: the normal case and an adversarial case from section 2. A useful probe gives different results for the old and the new behavior; which result counts as a pass is decided only by the evidence, never by the diff. Each probe must exercise changed behavior, possibly reached through a route the change did not touch (item 13). `[PROC]`

---

## 2. Adversarial questions

Ask these about every behavior the change touches. The answers come from the bundle, not from this list.

8. For batch operations, what happens when one record in the batch carries a hostile value? `[PROC]`

9. What happens with no authentication at all, or as a different user? `[PROC]`

10. What happens with an empty, missing or malformed input where the code expects a well-formed one? Does it reach the same handler, a different handler or an error path? `[PROC]`

11. When the change introduces two or more conditions, what happens when more than one applies, or when they disagree? Probe the combinations, not just each condition alone. `[PROC]`

12. What happens when the same logical input is written differently (case, encoding, whitespace) or arrives as an unexpected type (a dict where a string was expected, bytes where text was expected, a list where a scalar was expected)? `[PROC]`

13. Can the same request reach a route or handler the change did not touch? If so, probe that path too. `[PROC]`

---

## 3. Worked examples

Study the pattern, not the specific bug. The first example is about deciding the expected value; the others are real bug fixes, cited by commit SHA.

14. **Expect what the evidence says, not what the change does.** Suppose the fixture says *"account 7 has balance 250"*, and the change edits how balances are read, introducing a bug. Expect balance 250 for account 7, with evidence `"account 7 has balance 250"` (source: fixture). Do not expect whatever number the new code would compute. If the new code returns something else, your probe fails, which is exactly the point. `[PROC]`

15. **Probe every input position in a batch.** In CodeIgniter4 `f5e463b9a3e986389ce285963e51a7f1fab6559f` (fix for deleteBatch() SQL injection, T1), `deleteBatch()` with `where()` conditions substituted bind values raw into the DELETE SQL, ignoring the escape flag; the fix routes them through `convertWhereBindsForBatch()`, which respects escaping. A probe that calls the batch operation with `where()` values containing SQL metacharacters, and checks whether they are treated as values or as code, would have caught it. Whenever a change assembles a query or command from per-record inputs, probe each input position with a hostile value. `[DEV: T1]`

16. **Vary the representation.** In Fastify `179619c8e99d54924309b9933402151ef9af37e0` (fix for header schema bypass, T2), mixed-case header names slipped past validation; the fix normalized names before checking. A probe that sends the same header as `Content-Type`, `content-type` and `CONTENT-TYPE` and checks that validation treats them alike would have caught it. `[DEV: T2]`

17. **Send the unexpected type.** In Django `ebcb13b327301f28cbc6cd5e4988a719f00575aa` (fix for GIS spatial lookup file write, T4), dict and bytes values in spatial lookups reached code that treated them as file paths; the fix rejected unsafe types. A probe that submits a dict or bytes where the API documents a string would have caught it. `[DEV: T4]`

---

## 4. Format (an invalid probe is never sent and counts against you)

18. Follow the output format at the top of this prompt exactly, copying the shapes in its complete example. The two most common slips: `evidence` goes at the top level of the assertion, never inside `json_path`; and `capture` is either omitted or maps a NAME to `{"from": "body", "json_path": "<dotted path>"}`, never an empty object. `[PROC]`

19. Quote only text shown in this prompt. For a true/false value, quote only the part about the record you are checking, not a sentence that contains both values. `[PROC]`

20. To check the value of a field in a JSON response, use a `json_path` assertion on that exact field, for example `{"type": "body", "json_path": {"path": "done", "equals": false}, ...}`. Do not use `body` `contains` or `matches` for a field's value: `contains "false"` also passes when a different field holds that text. `[PROC]`

---

## 5. Final check before you answer

21. For every assertion, status included: Is the expected value backed by a quote from outside the changed lines? Is the quote copied exactly? Does it contain the value, and only the value for this record? If not, fix it or remove the assertion (R4). `[PROC]`

22. For every probe: Does it match the format in section 4? Is every field value checked with `json_path` (item 20)? Would it fail if the behavior it checks were wrong? `[PROC]`

23. Do not write only happy-path probes, and do not stop at the first probe that passes. Cover section 2 for each behavior you listed in step 2. `[PROC]`
