# ProofDeploy Author Skill Registration v2

**Date:** 2026-10-09
**Status:** Supersedes skill registration v1 (`docs/evaluation/skill-registration-v1.md`).

## What changed from v1

1. **System prompt corrected.** v1 recorded the old Bottle prompt ("a Python web framework… self-contained Python script using the WSGI interface"). That prompt asks for Python code running inside the target process, which the harness rejects, which skill item 3 forbids, and which is wrong for every TS/PHP repo and the whole eval set. Replaced with the language-neutral prompt below.
2. **Approval wording corrected.** v1 said "APPROVED by reviewer". The reviewer passed the skill *content* at commit `0b3606b`; the v1 registration itself was not reviewed. This v2 registration corrects that overclaim.
3. **Generator fixed.** The v1 generator left empty `` after stripping tags and left "T1"/"T2"/"T4" in the worked examples. Fixed in `scripts/generate_author_skill.py`; the author copy is re-hashed below. The audited source `skill.md` is unchanged (hash `11576fb2...` still matches).
4. **Harness version:** still owed (probe-schema harness not yet implemented).
5. **Assembler change:** still owed (skill allowlist + manifest integration).
6. **No-skill baseline:** must be re-run with the corrected prompt below (minus the skill). The Bottle baseline used the old prompt; the Y floor ("above the no-skill baseline") requires the same prompt.

## Hashes

- **Audited source** (`docs/evaluation/skill/skill.md`): `11576fb29003ad28b58480c6ee9a508622597cc46b48fcb6d1e86bf5af4d7739` (unchanged)
- **Author copy** (`docs/evaluation/skill/skill-author.md`): `e42f12d71ea9f5ceee0f6d13b553f7cb13426954053db684ae4e2a6a8d73f5f9`
- **Generator:** `scripts/generate_author_skill.py` (deterministic)

## Model

- **Model:** `openai/gpt-oss-120b` through Groq
- **Cutoff:** June 2024 (vendor-stated)
- **Temperature:** 0.2
- **Seed:** not supported by Groq API
- **Attempts:** 1 (exactly one run per bundle; no best-of-N)
- **Tools:** none. **Web:** none.

## System prompt (corrected)

```
You are a black-box probe author. You will receive a code diff and a repository snapshot. Your task is to write probes that verify the behavior introduced or changed by the diff.

Rules:
- Probes must test externally observable behavior only: HTTP status codes, headers, response bodies, and read-only database state.
- Each probe is a JSON object matching the probe schema: { "setup": [...], "act": { "method": ..., "path": ..., "headers": {...}, "body": ... }, "assert": [...] }.
- For library repos, you may include a setup step that writes a minimal harness app mounting the library. The harness app is hashed alongside your probes.
- Each probe must be able to fail if the behavior it checks were wrong.
- Output each probe as a fenced JSON code block. Do not include explanations outside the code blocks.
```

When the skill is used, `skill-author.md` (hash `e42f12d7...`) is prepended to this prompt.

The no-skill baseline uses this prompt without the skill.

## Harness

- **Version:** owed (probe-schema harness not yet implemented).
- **Probe schema:** strict HTTP/read-only DB probes. The harness rejects non-schema probes.
- **Run pair:** FAIL on fix^, PASS on fix. INCONCLUSIVE if the API changed between B and fix^.

## Bundle integration

- **Allowlist:** `skill-author.md` (hash `e42f12d7...`) — owed: assembler change.
- **Manifest:** skill hash recorded per run — owed: assembler change.

## Library repos

Option (b): the author writes the harness app as a recorded setup step, hashed alongside the probes. Dev-set library repos only (Hono, Fastify, body-parser, Bottle). Eval repos stay HTTP/DB-only. (Master approved, 2026-10-09.)

## Skill freeze

The skill content is frozen as of the v1 registration merge (`cac6886f`, 2026-10-09 16:23 EDT). The held-out candidates became visible after this point. Any change to the skill's *content* requires a new held-out set chosen by a new rule. The generator fix in this v2 does not change content (audited source hash unchanged).
