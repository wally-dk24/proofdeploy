# ADR-0011: Executor Comparison Semantics

**Date:** 2026-10-11
**Status:** Accepted

## Context

Acceptance run 38152175992 was the first run to put a model-written
assertion through the real executor. It exposed two defects in how the
executor compares values (Addendum 20):

1. **E1.** The author wrote `{"type": "body", "equals": {}}`. The app
   returned exactly `{}`. The executor compared the raw response text
   (`"{}\n"`) against the dict (`{}`) and reported FAIL. The registered
   prompt documents `{"equals": ...}` without limiting it to strings.
2. **E2.** The setup captured `id` (the number `1`); the executor stored
   the capture as the string `"1"`. The assertion
   `{"json_path": {"path": "id", "equals": "${ITEM_ID}"}}` compared
   `1 == "1"` and reported FAIL. The prompt says `${NAME}` "references
   a captured value".

Both defects make correct probes FAIL. On a clean diff that is a false
alarm caused by ProofDeploy; on a bug it can hide a catch. The earlier
rounds checked *that* verdicts were computed and recorded; this run
showed *how* values are compared had not been tested against the shapes
the prompt teaches the author to write.

## Decision

1. **`body equals` with a string expectation compares the raw response
   text exactly** (unchanged behavior).
2. **`body equals` with a non-string expectation parses the body as
   JSON and compares values.** A body that is not valid JSON is a FAIL
   with that detail (not INCONCLUSIVE: the assertion is well-formed,
   the response simply does not satisfy it).
3. **Captures keep their JSON type.** `capture_bindings` stores the
   parsed value (`int` stays `int`); only JSON scalars
   (`str`, `int`, `float`, `bool`) are capturable, as before.
4. **Whole-value `${NAME}` substitutes the typed value.**
   `{"equals": "${ITEM_ID}"}` with an int capture compares ints.
   Placeholders embedded in a larger string (e.g. `"/items/${ID}"`)
   substitute the text form, so URLs and headers keep working.
5. **No prompt change.** These semantics are consistent with the
   registered template v7, which documents `{"equals": ...}` without a
   type restriction and `${NAME}` as a reference to a captured value.

## Consequences

- The fail-closed placeholder rules are unchanged: unknown names and
  declared-but-unset captures still raise.
- Typed captures keep the redaction guarantee (Addendum 21): both
  secret collectors treat non-bool int/float captures as secrets in
  their text form, so a numeric session id or API key is redacted from
  the written record — including the `captured_values` leaf itself —
  exactly as a string capture is. (Fixture and contract-env numbers
  remain uncollected: they are configuration, not secrets.)
- Tests: `tests/test_executor_addendum20.py`, including a replay of
  acceptance run 38152175992's real probes and recorded responses
  (probe 0 PASSes both sides; probe 1 PASSes at fix^, FAILs at fix).
