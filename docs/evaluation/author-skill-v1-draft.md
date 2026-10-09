# Probe-Authoring Skill v1 (DRAFT)

**Status:** DRAFT — pending reviewer leak audit. Not registered. Must not be used for checkpoint runs until the audit passes and it is registered by hash.

**How to use this document:** Read it before opening the author bundle. It teaches how to probe, not what to find. Every numbered item carries a provenance tag: `[PROC]` (general procedure), `[DEV: T<n>]` (illustrated by TRAIN dev-set bug T<n>). An untagged item is a drafting error.

---

## 1. Your job

1. You are not verifying that the feature works. You are trying to find where it breaks. A probe that passes on both the buggy and fixed code tells you nothing. Every probe must be capable of failing. `[PROC]`

2. For each behavior the diff changes, write at least two probes: one for the expected case and one for an adversarial case (wrong actor, bad input, repeated action, interacting conditions). If you only write expected-case probes, you have not done the job. `[PROC]`

3. Only assert on what you can observe from outside: HTTP status codes, headers, response bodies, and read-only database state. Never assert on internals (variable values, call counts, log lines). `[PROC]`

---

## 2. Procedure

4. Read the diff first, before the snapshot. List each behavior it changes or adds, in your own words, one per line. If you cannot list them, re-read the diff. `[PROC]`

5. For each listed behavior, ask: which actors can reach it, in which states, with which inputs? An actor is anyone or anything that can invoke the code path: an authenticated user, an unauthenticated request, a scheduled job, another service. A state is what must already be true (an existing record, a flag set, a prior request completed). `[PROC]`

6. Compare the changed code path with the unchanged paths next to it. The diff shows what is new; the surrounding code shows what the old behavior was and what neighboring behaviors still do. A bug often lives in the interaction between the new path and an old one the diff did not touch. `[PROC]`

7. Trace each untrusted input from the boundary (HTTP request, query parameter, header, body field) to where it is used. Note every point where it is validated, normalized, or passed through unchanged. `[DEV: T2]`

8. When the diff adds a guard, check, or validation, probe both sides of it: an input that should pass the guard and an input that should be rejected by it. Then probe the boundary between them. `[PROC]`

9. Structure every probe as setup / act / assert, plus a negative case. Setup creates the preconditions. Act performs one operation. Assert checks the externally observable result. The negative case performs the same operation under adversarial conditions (different actor, malformed input, repeated invocation) and asserts the result differs appropriately. `[PROC]`

---

## 3. Adversarial questions

Ask these about every behavior the diff touches. They are generic; the answers come from the bundle, not from this list.

10. What happens when the operation is performed twice with the same inputs? Does the second invocation behave like the first, is it rejected, or does it produce a different result? `[DEV: T1]`

11. What happens with a lower-privileged actor than the one the feature was built for? What happens with no authentication at all? `[PROC]`

12. What happens with an empty, missing, or malformed input where the code expects a well-formed one? Does it reach the same handler, a different handler, or an error path? `[DEV: T4]`

13. When the diff introduces two or more new conditions, what happens when more than one applies at the same time? What happens when they disagree (one says allow, the other says deny; one matches, the other does not)? Probe the combinations, not just each condition alone. `[PROC]`

14. What happens when the same logical input is represented differently — different case, different encoding, different type (string vs number, object vs array, dict vs bytes)? Does the code normalize before comparing, or does the representation leak through? `[DEV: T3]`

15. What happens when a value of an unexpected type reaches the code — a dict where a string was expected, bytes where text was expected, a list where a scalar was expected? `[DEV: T5]`

16. Can the same entity or resource be reached by a second path the diff did not change (a different endpoint, a different query parameter, an admin interface, a bulk operation)? If so, probe the behavior through that path too. `[PROC]`

---

## 4. Worked examples

Each example is from the TRAIN dev set only, cited by commit SHA. Study the pattern, not the specific bug.

17. **Repeat the operation.** In Payload `6c0c4dc9b4ce1ac87b03fbb5dd7356b8559cbc4e` (fix for Stripe settlement replay, T1), the bug was that confirming the same order twice processed the payment twice; the fix added an idempotency guard. A probe that submits the same confirmation request twice and checks whether the second one is rejected would have caught it. Whenever a diff adds an operation that changes state (creates, updates, charges, sends), probe what happens when it is invoked twice with identical inputs. `[DEV: T1]`

18. **Vary the representation.** In Fastify `179619c8e99d54924309b9933402151ef9af37e0` (fix for header schema bypass, T3), the bug was that mixed-case header names slipped past validation; the fix normalized names recursively before checking. A probe that sends the same header in different cases (`Content-Type` vs `content-type` vs `CONTENT-TYPE`) and checks whether validation treats them identically would have caught it. Whenever a diff compares or validates a string, probe with case, encoding, and whitespace variations of the same logical value. `[DEV: T3]`

19. **Send the unexpected type.** In Django `ebcb13b327301f28cbc6cd5e4988a719f00575aa` (fix for GIS spatial lookup file write, T5), the bug was that dict and bytes values in spatial lookups reached code that treated them as file paths; the fix rejected unsafe types. A probe that submits a dict or bytes where the API documents a string, and checks whether it is rejected or mishandled, would have caught it. Whenever a diff processes a value, probe with a type the documentation does not promise. `[DEV: T5]`

---

## 5. Anti-patterns

20. Do not write only happy-path probes. "The feature returns the right answer for valid input" is the starting point, not the probe. For every happy-path probe, write the adversarial twin. `[PROC]`

21. Do not assert on things the diff did not change. If your probe would pass identically on the parent commit, it is not probing the diff. Each probe must exercise at least one behavior the diff added or modified. `[PROC]`

22. Do not invent domain rules. If the bundle does not say what the correct behavior is, derive it from the code and the snapshot: what does the changed path do, what do the neighboring paths do, and where do they disagree? Never import knowledge about the repository's domain that is not in the bundle. `[PROC]`

23. Do not stop at the first probe that passes. A passing probe confirms the feature exists; it does not confirm the feature is correct. Keep probing until you have covered the adversarial questions in section 3 for each behavior you listed in item 4. `[PROC]`
