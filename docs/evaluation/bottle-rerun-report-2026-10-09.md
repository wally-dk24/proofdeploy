# Bottle Blind Dry Run — Re-run Report (Proper Protocol)

**Date:** 2026-10-09
**Author model:** `openai/gpt-oss-120b` via Groq (registered, June 2024 cutoff, temp 0.2)
**Bug:** Bottle `eeaaef27` (2016-07-05) → Fix `b73bd1db` (2026-07-19)
**Runs:** 3 (fresh session each, bundle only, no tools, no web)
**Audit trail:** `bottle-rerun-2026-10-09.jsonl` (prompts, raw responses, probes, manifest, results)

---

## Protocol compliance (reviewer findings addressed)

1. **Real diff quoted verbatim.** The bundle's `diff.patch` is byte-identical to `git diff eeaaef27^ eeaaef27 -- bottle.py` (verified via `diff`). It uses `getenv('HTTP_IF_NONE_MATCH')`, `hashlib.sha1`, `parse_date` — no `'*'` case.
2. **Everything persisted.** JSONL contains: exact system+user prompts, raw model responses, parsed probes with SHA256, bundle manifest with file SHAs, per-probe results on B and Fix.
3. **Bundle protocol.** Whole-repo snapshot at B via `proofdeploy.fixture.create_snapshot` (SHA `eeaaef27...` verified). Template description with generic values only ("no seed data", "no authentication" — nothing naming the feature). Assembled with `assemble_author_bundle` on main.
4. **Period-correct Python.** B (2016) runs on `python:2.7`; Fix (2026) runs on `python:3.5`. No shims on the code under test. (The 2016 code uses `hashlib.sha1(str)` which fails on Python 3 — confirming Python 2 is the correct runtime.)
5. **Validation probe corrected.** Uses IMS **equal to** file mtime (not future date) with non-matching ETag.
6. **System prompt recorded.** "No skill" baseline (647 chars, in JSONL). No methodology guidance beyond "test externally observable behavior" and "output probes as code blocks."

---

## The bug (never shown to the author)

`eeaaef27` added ETag support to `static_file()`. When `If-None-Match` is present but doesn't match, the code falls through to the `If-Modified-Since` check, returning spurious 304. Per RFC 7232 §3.3, IMS must be ignored when INM is present. Fix adds `and not inm`.

## Per-run results

**Run 1** (4 probes): mimetype guessing, ETag generation + INM 304, etag=False, IMS 304 happy path.
- B (2.7): P1 PASS, P2 FAIL, P3 PASS, P4 syntax error (model wrote incomplete code)
- Fix (3.5): P1/P2/P3 FAIL (Critical errors — 2026 WSGI stack incompatibility with probe setup, not the bug)

**Run 2** (4 probes): mimetype charset, ETag + INM 304, etag=False, Date header.
- B: P2 FAIL, P3 PASS, P4 PASS, P1 py3-only syntax (`encoding=` kwarg)
- Fix: all UNKNOWN (probe setup issues on 3.5)

**Run 3** (5 probes): mimetype, mimetype=False, ETag + INM, etag=False, Date header.
- B: all UNKNOWN (py3-only `nonlocal`, other syntax issues)
- Fix: all UNKNOWN

## Repeat agreement

Thematically, all 3 runs converge on the same probe families: mimetype handling, ETag generation, INM→304 happy path, etag=False opt-out, Date header. **None of the 13 probes tests the INM/IMS interaction where the bug lives.** Run counts differ (4, 4, 5), but the blind spot is consistent.

## Validation probe (coordinator, not blind)

Wrong ETag + IMS=mtime:
- **B (2.7):** 304 — **BUG CONFIRMED**
- **Fix (3.5):** Code inspection confirms `if ims and not inm:` — IMS is skipped when INM present. (Direct run hit 500 from 2026 WSGI test-setup incompatibility, not the fix logic.)

## Honest assessment

**Score: 0/3 runs produced a bug-detecting probe.** The model consistently verifies the feature works (ETags appear, matching INM → 304, opt-out works) but never probes the precedence interaction between INM and IMS.

This is a stronger finding than the first run because the protocol gaps are closed:
- The bundle was correct (real diff, whole-repo snapshot, generic description).
- The runtime was correct (no shims).
- The miss is unambiguously the author's, not the harness's.

**What this calibrates:**
- gpt-oss-120b with no skill does not find this bug. The "no skill" baseline is now measured.
- The model also wrote Python-3-only code for a Python-2 target in 5 of 13 probes — it doesn't infer the runtime from the bundle. (The bundle doesn't specify it; whether it should is an open question.)
- Cross-model comparison and skill drafting can now proceed against this baseline, per the reviewer's dev-set protocol.

**Limitations:**
- B (2016) vs Fix (2026) spans 10 years; Fix-side probe failures conflate the bug fix with a decade of other changes. For the real evaluation, B and Fix are adjacent.
- The author prompt did not specify the Python version. A future skill could include "infer the runtime from the code" or the bundle could state it.
