# Registration amendment: model provider (D206): SIGNED by Master 2026-10-18 (D207)

**Amends:** `docs/evaluation/dev-set-registration-v2.md` item 5, and every registration that names "openai/gpt-oss-120b through Groq" (skill registrations v1–v3 and control registration v2 inherit it).
**Why:** Groq's free tier caps requests at 8K tokens/minute, a real-app author request is about 9K (D205), and Groq has paused Developer-tier upgrades with no date. Master chose the same model on a different host.

## Item 5, amended text (verbatim)

> 5. Model for checkpoint/dev-set measurements: `gpt-oss-120b` (OpenAI's open-weight gpt-oss-120b) served by Cerebras through `https://api.cerebras.ai/v1/chat/completions`, model id string `gpt-oss-120b`. Temperature 0.2; no seed, no tools, no reasoning_effort sent (the provider default applies and is recorded). Exactly one model per run. No best-of-N. Every evidence record names the provider, the endpoint and the model id string. Every measured unit of Stage 1 (dev set, checkpoint, time machine, live bug) uses this provider; results obtained through another provider are never pooled with these.

## Stated in advance
- Same weights is not the same as identical outputs: serving stacks can differ in numerics and defaults. So the skill v3 golden gate (D179/D189: 2 golden bugs caught, 2 golden cleans with no false alarm) is re-run on Cerebras before any measured use. Earlier Groq-era runs (smoke, D152; gate, D189) stay on the record as Groq-era evidence and are not counted.
- The author prompt (v7, `fa72a0e6`), the skill (`e04cb811`), the fixture templates, the caps and the scoring are unchanged. The model_client pin changes (new hash recorded at merge).
- The secret keeps its name `GROQ_API_KEY`; the sandbox blocklist and the record redaction are keyed to that name. Renaming it later is one change that updates the blocklist and redaction together.

## Signature
Master (reviewer chat, 2026-10-18), verbatim: "Yes signed". Signed (D207).

## Implementation (PR #145): not part of the signed text

### Reason

The blind re-run `39082717051` (2026-10-17) died at the author call
with `HTTP Error 413: Payload Too Large`: the request (~9K tokens)
exceeds Groq's free-plan ceiling for this model (8K TPM / 200K TPD).
Groq has paused Developer tier upgrades, so the ceiling cannot be
lifted there. The same model is served by Cerebras; Master approved
the provider switch (O15/D206) and signed this amendment (D207).

### What changes

1. **Provider:** Groq → **Cerebras**. The record's provider field is
   `provider: "cerebras"`.
2. **Endpoint:** `https://api.cerebras.ai/v1/chat/completions`.
3. **Allowed hosts:** `["api.cerebras.ai"]` — the client refuses any
   other host before sending (fail closed).
4. **Model id:** `gpt-oss-120b` (Cerebras's id for the same weights;
   no `openai/` prefix).
5. **Model client pin:** `proofdeploy/model_client.py` SHA-256 changes
   from `55b7d93283d511b681f7af24dd78aafc0cc750d652ed965f94fe7a4ab1d28598`
   to `a35238a5a38b36ae691ab97861a064938787c5b14a49c380c009067a99f3785b`.
   The pin asserted in `docs/registration/wo5-prompt-v7.md` is updated
   in the same PR.
6. **Error body (folds in D205(a)):** on an HTTP failure, the raised
   error carries the HTTP status plus up to 500 characters of the
   response body, passed through the record secret-redaction path
   (by value for the env credential, plus the JWT/Bearer pattern
   backstops) before it can land in any record or log. Error text is
   provider-neutral ("model API call failed").

### What does not change

- **Temperature:** 0.2.
- **Request shape:** exactly one attempt; no seed, no tools, and no
  `reasoning_effort` is sent (the provider default applies). The
  record carries `seed: null`, `tools: null` and
  `reasoning_effort: null`, the endpoint, and the message hashes, as
  before.
- **Secret:** the credential is still read from the environment
  variable named `GROQ_API_KEY` (the sandbox blocklist and the CLI
  redaction are keyed to that name). The name is not renamed.
- **Prompt:** author prompt v7 (template SHA-256
  `fa72a0e6c4a1fa9fb204986e08c8eef4b11fc13786f41389ecf2f42ea8555cf3`), unchanged.
- **Skill:** skill v3, skill-author SHA-256
  `e04cb8111c9767149abc371d0ee1b0781d01db9e2baab96f767224ee846f2099`, unchanged.
- **Templates, caps and scoring:** unchanged.

### Sequencing (D206)

- No `measure.yml` or `verify.yml` dispatch from any ref until the
  PR carrying this amendment merges (current code would send the
  Cerebras key, stored as `GROQ_API_KEY`, to the old host).
- After the merge: the golden gate on Cerebras (4 runs; predictions
  committed first; Master approves each), then the blind re-run of
  the same selected bug with the D204 inputs. The predictions at
  relay `b5f78ef` stand.
