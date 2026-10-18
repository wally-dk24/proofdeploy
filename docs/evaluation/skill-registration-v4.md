# ProofDeploy Author Skill Registration v4

**Date:** 2026-10-18
**Status:** Signed by Master (D216). Supersedes skill registration v3 (`docs/evaluation/skill-registration-v3.md`) for the skill arm only.
**Plan line:** 1.5(d) author preparation (D212 outcome; D215, the last skill iteration under D180). **Scope:** the skill text only. The author prompt (`fa72a0e6`), the model, temperature, attempts and the no-skill baseline prompt are unchanged.

## Why

The D212 repeat study on Cerebras (10 runs, skill v3, golden bug units) landed both units in the 2-3/5 band: node-express-sqlite 3/5 catches, python-flask-sqlite 2/5. Every miss was the same shape -- a full polarity inversion: the author asserted the diff's (buggy) values against the fixture's stated seeded data, so its probes passed on the buggy side and failed on the fixed side. Every catch asserted the fixture's values. There were 0 invalid blocks across all 10 runs: the failure is the direction of the assertions, not their format. Section 1a already says to assert what the sources establish (A4); the runs show the author does not reliably do it for values the fixture states about the instance itself.

## The change

Two items appended to the audited source `docs/evaluation/skill/skill.md`, verbatim below: A6 as the last item of section 1a (after A5), and B3 as the last item of section 1b (after B2). Nothing else in the skill changes; the skill.md diff is +4/-0 (two blank lines, two items). The author copy is regenerated with `scripts/generate_author_skill.py`.

```
A6. The fixture's statements about the instance are facts; the diff is a claim. When a probe reads a record or setting the fixture describes, its expected values come from the fixture, never from what the changed code would return.

B3. Before answering, check the direction of every assertion. For each expected value, name its source: the fixture, a name, untouched code, a doc or test, or only the diff. If a value comes only from the diff and one of the other sources says otherwise, change it to what that source says.
```

## Hashes

- **Audited source** (`docs/evaluation/skill/skill.md`): `64d1a4953e6519843aea7b823648b06428821f79033bee0f6bbb8a572eb22f63`
- **Author copy** (`docs/evaluation/skill/skill-author.md`): `ff601eb15b09e6e0bf8f48b2037f0ab8b2a1b0c90ac022156f76d0169380b36a`
- **Generator:** `scripts/generate_author_skill.py` (deterministic)

## Test before use (D215 gate)

Gate predictions are committed to the relay before any dispatch. The gate rule:

16 runs, skill v4, from the accepted PR head, measured_result false, label skill v4 gate (D215): 5 × golden node-express-sqlite bug, 5 × golden python-flask-sqlite bug, 3 × golden node-express-sqlite clean, 3 × golden python-flask-sqlite clean. Pass = ≥4/5 catches on EACH bug unit AND 0 false alarms in the 6 cleans (an invalid block on a clean = false alarm). Fail → stop; replan with Master (no further skill iteration). An environment/transport INCONCLUSIVE is re-run once in the same slot. Reviewer prediction: about 4/5 per bug unit (band 3–5); 0 false alarms most likely.

**Known limit, stated in advance:** passing on the golden apps is encouraging but not proof. The golden bugs are simple; the real measure stays the held-out dev set (1.7) under D95. This is the last skill iteration (D180): if the gate fails, there is no v5 -- the work replans with Master.

## Signature

Master (reviewer chat, 2026-10-18), verbatim: "Agreed, Im ok with updating the skill". Signed (D216).
