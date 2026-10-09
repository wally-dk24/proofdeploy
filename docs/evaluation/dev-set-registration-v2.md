# ProofDeploy Dev Set Registration v2

**Date:** 2026-10-09
**Status:** APPROVED by reviewer. Supersedes dev set v1 (`docs/evaluation/dev-set-2026-10-09.md`).

## Registered file

- **File:** `docs/evaluation/dev-set-v2-2026-10-09.md`
- **SHA256:** `873a715714d9c8366e013d905ae5cb5df1284c72134906f510d6633c52ab6dda`
- **Merge commit:** `8aabb3babf5130167011c3e176d3dfb0d630cb28` (PR #30, merged 2026-10-09 16:11:54 EDT)

## Contents

- **TRAIN bugs:** 7 (T1–T7), all B^→B diffs verified against upstream, all approved by reviewer 2026-10-09.
- **TRAIN clean diffs:** 5 (C1–C5), all behavior-changing, 4+ weeks old, rule 8 clean, approved by reviewer 2026-10-09.
- **HELD-OUT:** mechanical selection rule with separate repos (nestjs/nest, koajs/koa, adonisjs/adonis, pallets/flask, fastapi/fastapi). Executed after skill freeze; reviewer judges eligibility.

## Supersession

Dev set v1 (`dev-set-2026-10-09.md`, SHA256 `de6c2aa77f9afcbe61f87f836daea5a03b8021156f52d707a3b30491b624f2c8`) is superseded. Its train bugs were selected by the author (contaminated), its clean diffs were not properly eligible, and it was rejected by the reviewer. This v2 registration replaces it in full. The v1 file remains on disk as history; it is not to be used.

## Standing rules recorded here

1. Every dev/eval entry ships with its verification commands and verbatim output.
2. Run pair: a catch means the probe FAILs on fix^ and PASSes on fix. INCONCLUSIVE if the API changed between B and fix^.
3. Skill v1 (drafted from the fabricated v1 list) is void. Skill v2 must be redrafted from the T1–T7 list in this registration.
4. For library repos in the dev set (Hono, Fastify, body-parser, Bottle): the author writes the harness app as a recorded setup step, hashed alongside the probes. Eval repos stay HTTP/DB-only. (Master approved option (b), 2026-10-09.)
5. Model for checkpoint/dev-set measurements: `openai/gpt-oss-120b` through Groq. Exactly one model per run. No best-of-N.
