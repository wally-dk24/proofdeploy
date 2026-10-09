# Probe-Authoring Skill

**How to use this document:** Read it before opening the author bundle. It teaches how to probe, not what to find. Every numbered item carries a provenance tag: `[PROC]` (general procedure), `[DEV: T<n>]` (illustrated by TRAIN dev-set bug T<n>). An untagged item is a drafting error.

---

## 1. Your job

1. You are not verifying that the feature works. Every probe must be able to fail if the behavior it checks were wrong. `[PROC]`

2. For each behavior the diff changes, write at least two probes: one for the expected case and one for an adversarial case (see section 3). If you only write expected-case probes, you have not done the job. `[PROC]`

3. Only assert on what you can observe from outside: HTTP status codes, headers, response bodies, and read-only database state. Never assert on internals (variable values, call counts, log lines). `[PROC]`

---

## 2. Procedure

4. Read the diff first, before the snapshot. List each behavior it changes or adds, in your own words, one per line. If you cannot list them, re-read the diff. `[PROC]`

5. For each listed behavior, ask: which actors can reach it, in which states, with which inputs? An actor is anyone or anything that can invoke the code path: an authenticated user, an unauthenticated request, a scheduled job, another service. A state is what must already be true (an existing record, a flag set, a prior request completed). `[PROC]`

6. Compare the changed code path with the unchanged paths next to it. The diff shows what is new; the surrounding code shows what the old behavior was and what neighboring behaviors still do. `[PROC]`

7. Trace each untrusted input from the boundary (HTTP request, query parameter, header, body field) to where it is used. Note every point where it is validated, normalized, escaped, or passed through unchanged. `[DEV: T1]`

8. When the diff adds a guard, check, or validation, probe both sides of it: an input that should pass the guard and an input that should be rejected by it. Then probe the boundary between them. `[PROC]`

9. Structure every probe as setup / act / assert, plus a negative case. Setup creates the preconditions. Act performs one operation. Assert checks the externally observable result. The negative case performs the same operation under adversarial conditions (different actor, malformed input) and asserts the result differs appropriately. `[PROC]`

---

## 3. Adversarial questions

Ask these about every behavior the diff touches. They are generic; the answers come from the bundle, not from this list.

10. For batch operations, what happens when one record in the batch carries a hostile value? `[DEV: T1]`

11. What happens with no authentication at all? `[DEV: T5]`

12. What happens with an empty, missing, or malformed input where the code expects a well-formed one? Does it reach the same handler, a different handler, or an error path? `[DEV: T3]` `[DEV: T7]`

13. When the diff introduces two or more new conditions, what happens when more than one applies at the same time? What happens when they disagree (one matches, the other does not)? Probe the combinations, not just each condition alone. `[DEV: T6]`

14. What happens when the same logical input is represented differently — different case, different encoding — or arrives as an unexpected type — a dict where a string was expected, bytes where text was expected, a list where a scalar was expected? Does the code normalize before comparing, or does the representation leak through? `[DEV: T2]` `[DEV: T4]`

15. Can the same request reach a different route or handler the diff did not change? If so, probe the behavior through that path too. `[DEV: T3]`

---

## 4. Worked examples

Each example is from the TRAIN dev set only, cited by commit SHA. Study the pattern, not the specific bug.

16. **Probe every input position in a batch.** In CodeIgniter4 `f5e463b9a3e986389ce285963e51a7f1fab6559f` (fix for deleteBatch() SQL injection, T1), the bug was that `deleteBatch()` with `where()` conditions substituted bind values raw into the DELETE SQL, ignoring the escape flag; the fix routes them through `convertWhereBindsForBatch()` which respects escaping. A probe that calls the batch operation with `where()` values containing SQL metacharacters, and checks whether the generated statement treats them as values or as code, would have caught it. Whenever a diff assembles a query or command from per-record inputs, probe each input position with a hostile value. `[DEV: T1]`

17. **Vary the representation.** In Fastify `179619c8e99d54924309b9933402151ef9af37e0` (fix for header schema bypass, T2), the bug was that mixed-case header names slipped past validation; the fix normalized names recursively before checking. A probe that sends the same header in different cases (`Content-Type` vs `content-type` vs `CONTENT-TYPE`) and checks whether validation treats them identically would have caught it. Whenever a diff compares or validates a string, probe with case, encoding, and whitespace variations of the same logical value. `[DEV: T2]`

18. **Send the unexpected type.** In Django `ebcb13b327301f28cbc6cd5e4988a719f00575aa` (fix for GIS spatial lookup file write, T4), the bug was that dict and bytes values in spatial lookups reached code that treated them as file paths; the fix rejected unsafe types. A probe that submits a dict or bytes where the API documents a string, and checks whether it is rejected or mishandled, would have caught it. Whenever a diff processes a value, probe with a type the documentation does not promise. `[DEV: T4]`

---

## 5. Anti-patterns

19. Do not write only happy-path probes. "The feature returns the right answer for valid input" is the starting point, not the probe. For every happy-path probe, write the adversarial twin. `[PROC]`

20. Do not assert on things the diff did not change. If your probe would pass identically on the parent commit, it is not probing the diff. Each probe must exercise at least one behavior the diff added or modified. `[PROC]`

21. Do not invent domain rules. If the bundle does not say what the correct behavior is, derive it from the code and the snapshot: what does the changed path do, what do the neighboring paths do, and where do they disagree? Never import knowledge about the repository's domain that is not in the bundle. `[PROC]`

22. Do not stop at the first probe that passes. A passing probe confirms the feature exists; it does not confirm the feature is correct. Keep probing until you have covered the adversarial questions in section 3 for each behavior you listed in item 4. `[PROC]`

23. If the bundle doesn't establish what the correct behavior is, don't assert it. An unfounded assertion counts against you just as a missed bug does. `[PROC]`

---
