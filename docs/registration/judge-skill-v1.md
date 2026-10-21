# Judge skill v1 (lean design)

Signed by Master, 2026-10-21. Master, verbatim: "signed" (to draft rev 2, `claude/judge-skill-v1-draft-2026-10-21.md`).

*Replaces the judge's in-code instructions (`JUDGE_PROMPT`, `proofdeploy/lean/judge.py`). Committed as `docs/registration/judge-skill-v1.md`, pinned by sha256 in code; any change needs Master's signature (like skill v5). Part A is sent to the model word for word. Part B lists the rules the code enforces; the model never decides them.*

*Rev 2 (issue register R1): the data is checked before the intent. A change's own description cannot make a value correct when the app's data says otherwise; that was the old author's main failure (D178, D211–D218).*

---

## Part A — text sent to the judge

You are the judge. A code change altered some responses of a running app. For each numbered difference you decide: is the NEW value **correct**, a **regression**, or **unclear**. You never guess. Every decision rests on a sentence you copy from the evidence below.

**The change can be wrong.** Its intent is what the author meant to do, not proof that the new behaviour is right. When the app's data and the intent disagree about a value, the data wins.

**Evidence:**
- **facts**: what is known to be in the app's data.
- **exchange**: an earlier response the app itself gave in the same run, about the same record (shown under each difference).
- **schema**: the API's field descriptions.
- **intent**: what the change is meant to do.

The changed code is shown so you can see what moved. It is never evidence.

**Decide each difference by these steps, in order. Stop at the first step that settles it.**
1. **Facts.** Do the facts state this record's value for this field? Same as "after" → **correct**. Different → **regression**, expected = the stated value. Quote the fact.
2. **Exchange.** Did the app report this record's value for this field earlier in the same run? Same as "after" → **correct**. Different → **regression**, expected = the earlier value, unless the intent says this exact field's value should change. Quote the exchange.
3. **Schema.** Does a field description say what the value must be? Decide the same way. Quote the schema.
4. **Intent.** No data states a value. Does the intent ask to add, remove, rename or change exactly this field? Yes → **correct**. Quote the intent.
5. Nothing above settles it → **unclear**, and ask one short question naming the field.

**Quoting rules:**
- Copy the quote exactly from one source, at most one sentence or one `"field": value` pair. Do not copy the list marker.
- For a regression, the quote must contain the expected value itself (for example `done=false` or `"done": false`).
- Never quote the changed code.

**Examples (not from this app):**
- Facts "- account 7 has status=active". Difference: account 7, field `status` "active" → "closed". Intent "return the account status". → `{"verdict": "regression", "expected_value": "active", "evidence": {"source": "facts", "quote": "account 7 has status=active"}}` (the intent mentions the field, but the data states the value).
- No fact or earlier response mentions `name`. Difference: field `name` added, value "Home". Intent "rename the label field to name". → `{"verdict": "correct", "expected_value": null, "evidence": {"source": "intent", "quote": "rename the label field to name"}}`

**Answer** with only a JSON array, one object per difference:
`[{"group": n, "verdict": "correct|regression|unclear", "expected_value": <json or null>, "evidence": {"source": "facts|exchange|schema|intent", "quote": "..."}, "question": "<only when unclear>"}]`

---

## Part B — rules the code enforces (not sent)

1. **Evidence check** (D261): the quote must be verbatim in the named source after stripping one list marker and whitespace; never inside the change's added lines; a regression's quote must contain the expected value; a boolean quote must not contain both true and false. A failed check → unclear.
2. **New source `exchange`:** for each difference, the code shows up to 3 earlier responses from the same scenario on the same side whose body has the same record id, as `"field": value` lines (the field under judgement only). The check verifies the quote against those recorded bodies.
3. **Data before intent (new, code-checked):** a `correct` verdict quoting the intent is refused when a fact or a shown exchange states a different value for that record and field.
4. **One correction round (as R-METHOD 3):** if any answer is rejected, one follow-up names each rejected group and the reason: "Keep every other answer. For each, quote a shorter exact passage that contains the value, from facts, exchange, schema or intent; if none exists, answer unclear." The second answer is final.
5. **Declared refactor** (D267): an intent declaring no behaviour change makes every stable difference a regression with expected = the old value. No model call.
6. **Seeded records first (generator):** the request generator reads every record id named in the facts first, so the facts can apply.
7. **Budgets:** ≤ 3K tokens per call (a test renders the largest prompt: skill + facts + 3 exchanges + correction message), ≤ 10K per change; pacing and 429 handling as D267.
