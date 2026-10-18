# Registration Amendment: Model Provider — Groq → Cerebras

**Date:** 2026-10-18
**Ruling:** D206 (reviewer); signed by Master in chat (D207, 2026-10-18).
**Amends:** the model/provider terms of the WO-5 registration
(`docs/registration/wo5-prompt-v7.md`) and of every registration that
names the author model as `openai/gpt-oss-120b` through Groq. All
other terms of those registrations are unchanged.

## Reason

The blind re-run `39082717051` (2026-10-17) died at the author call
with `HTTP Error 413: Payload Too Large`: the request (~9K tokens)
exceeds Groq's free-plan ceiling for this model (8K TPM / 200K TPD).
Groq has paused Developer tier upgrades, so the ceiling cannot be
lifted there. The same model is served by Cerebras; Master approved
the provider switch (O15/D206) and signed this amendment (D207).

## What changes

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

## What does not change

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

## Sequencing (D206)

- No `measure.yml` or `verify.yml` dispatch from any ref until the
  PR carrying this amendment merges (current code would send the
  Cerebras key, stored as `GROQ_API_KEY`, to the old host).
- After the merge: the golden gate on Cerebras (4 runs; predictions
  committed first; Master approves each), then the blind re-run of
  the same selected bug with the D204 inputs. The predictions at
  relay `b5f78ef` stand.

Signed: Master (D207, 2026-10-18)
