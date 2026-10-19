# R-SIZE — selection addendum: author-prompt size check (SIGNED; O22, D251, D253)

Applies to: the WO-6 selection rule (REG-C item 7 + Addendum 2 + D169 + R-SEL) and the held-out walk (Held-out Addenda 1–2), identically. Rule only; no scoring change; no change to what the author sees.

1. **New check (e), after (a)–(d).** For a commit that passes (a)–(d), the selection script builds the blind author's bundle and prompt exactly as the measured path does (same code, same registered contract, skill, template and caps), with no model call, and measures the prompt's length in characters (UTF-8 code points of the composed prompt).
2. **Budget:** the prompt must be at most **80,000 characters**. The commit fails (e) otherwise, with reason `e: author prompt too large (<n> chars > 80000)`, and the walk continues to the next commit. The size is recorded for every commit that reaches (e), pass or fail.
3. **Why this number (recorded, not part of the test):** the registered provider (Cerebras, gpt-oss-120b, current account tier) accepts at most 30,000 uncached tokens per minute and 65,000 tokens of context, so a single request above about 30,000 tokens can never be served. At a conservative 3.2 characters per token for code, 80,000 characters is about 25,000 tokens. Every prompt measured so far is under it: the first WO-6 blind run 33,518 chars (D205), paperless at the old B under v8 52,831 chars (D244), golden apps about 16,500 chars. The blind run 2 bug was 769,413 chars (D251).
4. **Change control:** the budget changes only by a new signed entry (for example, if the provider tier changes); it is never adjusted after seeing which commit a walk would select.
5. **Applies forward:** the WO-6 walk re-runs fresh under this rule (the script's sha256 changes, so the journal refuses to resume, D74); commits 1–11 re-evaluate the same way, and commit 11 (`b992f9fc`) fails (e). The held-out walk uses it from its first run.

Signed by Master, 2026-10-19: "Agreed. We haven’t been able to verify if tool works in blind run. So, yea, lets find something that fits and then we can optimize"
