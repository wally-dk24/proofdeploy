# Scoring Addendum: Start Failure = INCONCLUSIVE

**Date:** 2026-10-09
**Type:** Append-only addendum to dev-set registration v2
**Authority:** Master's explicit ruling (2026-10-09)

## Rule

If a probe cannot obtain an HTTP response because the target fails to start
(crash at startup, bind failure, or any condition where the server process
does not reach a listening state), the result is **INCONCLUSIVE**, not a catch.

## Rationale

The runner spec defines a catch as a probe FAIL on fix^ (an HTTP response
that violates the asserted behavior). A start failure produces no HTTP
response at all. It cannot be scored as a behavioral catch because there is
no observed behavior to compare against the assertion.

## Consequence

Held-out H3 (Flask IPv6 SERVER_NAME, `05e9c6bd`) is removed from the held-out
set. At fix^, the bug manifests as a crash at startup (`int(':1]:5000')`
raises before the server listens). Under this rule, H3 cannot score a catch.
Its slot goes to the next candidate in v2 order per the selection rule.

## Registration

This addendum is append-only. It does not modify registration v1 or v2.
It takes effect for all runs on or after 2026-10-09.
