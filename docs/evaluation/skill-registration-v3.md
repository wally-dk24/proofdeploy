# ProofDeploy Author Skill Registration v3

**Date:** 2026-10-17
**Status:** Signed by Master (D181), re-signed with section 1b added (D185). Supersedes skill registration v2 (`docs/evaluation/skill-registration-v2.md`) for the skill arm only.
**Plan line:** 1.7/1.8 author preparation (D179, D180). **Scope:** the skill text only. The author prompt (`fa72a0e6`), the model, temperature, attempts and the no-skill baseline prompt are unchanged.

## Why

Smoke runs V3 and M1 (D175, D178) showed the author taking a bug-introducing change as the definition of correct behaviour, even when the fixture stated contradicting seeded data. Smoke run M4 (D183) showed a schema slip (an assertion type that does not exist) turning a correct clean run into a false alarm. The v2 skill teaches adversarial inputs (items 1–23) but never asks whether the change itself is wrong.

## The change

Two new sections inserted after section 1 of the audited source `docs/evaluation/skill/skill.md`, in this order, verbatim below. Items are lettered (A1–A5, B1–B2) so the existing item numbers and cross-references stay unchanged. Nothing else in the skill changes. The author copy is regenerated with `scripts/generate_author_skill.py`.

```
## 1a. The change can be the bug

A1. You are reviewing a change before it ships. The diff shows what someone wrote, not what is correct. A change can introduce the very defect you are looking for, so never treat the diff's new behavior as the definition of correct.

A2. Before writing probes, establish what correct behavior is from sources outside the changed lines: the fixture's description of the instance and its seeded data, the names of the fields, functions and routes involved, the neighboring code paths the diff did not touch, and any docs, comments or tests in the snapshot.

A3. For each behavior the diff changes, ask whether the new behavior agrees with those sources. If the fixture states what a seeded record contains, a probe that reads that record should expect what the fixture states. If a name or the neighboring code says what a value means, the response should carry that meaning.

A4. When the diff's new behavior contradicts those sources, assert the behavior the sources establish, not the behavior the diff produces. Such a probe fails if the change is wrong and passes if it is right.

A5. When those sources do not settle what is correct, the diff is the only evidence of intent: probe it as written, and do not invent a correct behavior (see item 23).

## 1b. Check the format before you answer

B1. Every assertion must use one of the types the schema allows: status, header, body, db. To check a field in a JSON response body, use type body with a json_path object, for example {"type": "body", "json_path": {"path": "status", "equals": "active"}}. There is no assertion type named json_path.

B2. Before answering, re-read every probe against the schema. A probe that does not match is never sent, and it counts against you as if it had failed.
```

## Hashes

- **Audited source** (`docs/evaluation/skill/skill.md`): `74bcc6d5ac8e4222d81011c64c2fc14f3b43d1f6fafd59b5df723cb6df7e184a`
- **Author copy** (`docs/evaluation/skill/skill-author.md`): `e04cb8111c9767149abc371d0ee1b0781d01db9e2baab96f767224ee846f2099`
- **Generator:** `scripts/generate_author_skill.py` (deterministic)

## Test before use (D179 gate)

Four smoke-style runs on the golden apps only (`measured_result: false`; never held-out or checkpoint units), skill arm, predictions committed first, each approved by Master in GitHub:
- golden node-express-sqlite bug, golden python-flask-sqlite bug: expected caught;
- golden node-express-sqlite clean, golden python-flask-sqlite clean: expected no false alarm.

**Gate:** both bug units caught and no false alarm on either clean unit. Then this registration applies from the WO-6 blind run onward. At most 2 iterations; otherwise stop and replan with Master (D179, D180).

**Known limit, stated in advance:** passing on the golden apps is encouraging but not proof. The golden bugs are simple; the real measure stays the held-out dev set (1.7) under D95.

## Signature

Master (reviewer chat, 2026-10-17), verbatim: "So, I accept with what you posted with the skill". Signed (D181).
Re-signed with section 1b added (reviewer chat, 2026-10-17), verbatim: "Yea, lets make that update to skill as well to see if the results change". (D185)
