# WO-2 Milestone: Executor

**Date:** 2026-10-10
**Branch:** `feat/runner-executor` (head `95ce427`, awaiting Master's merge)
**Accepted by:** Orchestrator (Master's other agent) on 2026-10-09
**Work order:** WO-2 from brief 2026-10-10

## What WO-2 delivered

The probe executor (`proofdeploy/executor.py`) now meets all the review requirements.

### Original findings (8)

1. **`validate_probe` before execution.** Every probe is schema-validated before any HTTP call. An invalid probe is INCONCLUSIVE with reason `probe`; nothing is executed.
2. **Fail closed on unknown setup types.** Unknown `setup.type` is INCONCLUSIVE with reason `probe`, never skipped. Both the schema gate and `run_setup` fail closed.
3. **Verdict precedence: FAIL > INCONCLUSIVE > PASS.** The executor tracks `saw_fail` and `saw_inconclusive` separately; a probe that both fails an assertion and hits an INCONCLUSIVE condition reports FAIL.
4. **Setup outcomes checked.** A failed setup step (unreachable target, non-2xx response) is INCONCLUSIVE with a typed reason, and the act never runs. Previously it was silently continued.
5. **Independent DB result per assertion.** Each `db` assertion runs its own query. The old code shared the first query's result across all db assertions.
6. **Redirects never followed.** A custom `HTTPRedirectHandler` refuses redirects; a 3xx response is evaluated as-is.
7. **GET/HEAD only on remote targets.** Non-localhost targets reject other methods with INCONCLUSIVE (reason `probe`) before any request is sent.
8. **Every write logged.** POST/PUT/DELETE/PATCH requests are recorded on `ProbeResult.writes` (method, URL, status), serialized in `to_dict()`.

**Typed INCONCLUSIVE reasons:** new `InconclusiveReason` enum with `environment`, `auth`, `drift`, `probe`. The `reason` field is on `ProbeResult` and serialized in `to_dict()`.

### Follow-up findings (3)

9. **Non-2xx setup is INCONCLUSIVE.** Any setup HTTP response that is not 2xx makes the probe INCONCLUSIVE; the act never runs.
10. **Writes gated on the provisioned flag.** Write methods are allowed only when `provisioned=True`. Localhost alone is not sufficient. A write on a non-provisioned target is INCONCLUSIVE (reason `probe`), refused before any HTTP call.
11. **`harness_app` is INCONCLUSIVE until WO-5.** `run_setup` no longer skips `harness_app` steps silently. They return INCONCLUSIVE (reason `environment`) with the message "harness_app provisioning not yet available (WO-5)".

### 4xx typing fix

12. **Precise INCONCLUSIVE typing for setup HTTP failures.** A 4xx setup response used to be typed `environment`; that is the one reason class an admin is allowed to downgrade to "warn", so a broken setup request could have been waved through by policy. Now:
    - 401, 403 -> `auth` (credential problem)
    - other 4xx -> `probe` (the probe's request was wrong)
    - 5xx and unreachable -> `environment` (unchanged)

## Real-run evidence

### Orchestrator's verification runs

The orchestrator ran every case on `b7f2988` against a live server:

| Case | `provisioned=False` | `provisioned=True` |
|---|---|---|
| 302 asserted (no redirect followed) | PASS | PASS |
| FAIL plus an undecidable assertion | FAIL | FAIL |
| Setup POST returns 400 | INCONCLUSIVE (`probe`) | INCONCLUSIVE (`environment`) |
| `harness_app` step | INCONCLUSIVE (`environment`) | INCONCLUSIVE (`environment`) |
| POST to localhost | INCONCLUSIVE (`probe`); default constructor too | PASS, write logged |

The 400-on-setup case the orchestrator had reproduced in an earlier round (then typed `environment`) is now correctly typed `probe` for a non-provisioned target and `auth`-typed 401/403 are covered by the new tests.

### Wally's verification runs

Wally's own real runs against a live local HTTP server (verbatim):

```
server: http://127.0.0.1:40963

=== RUN 1: passing probe (provisioned target) ===
verdict: pass
reason: None
http_status: 200
  act: GET /health -> 200
  status: got 200, expected 200 -> PASS
  body json_path: got 'ok' -> PASS

=== RUN 2: non-2xx setup (503) -> INCONCLUSIVE ===
verdict: inconclusive
reason: environment
  setup[0]: GET /flaky-setup -> 503
  setup[0]: setup call returned 503

=== RUN 3: write on non-provisioned target -> INCONCLUSIVE ===
verdict: inconclusive
reason: probe
  act: write method POST requires a provisioned target; failing closed

DONE
```

## Tests

- **33 executor tests** (28 original + 5 new for precise setup-status typing: 401 -> auth, 403 -> auth, 400 -> probe, 404 -> probe, 500 -> environment). Test server gains `/bad` (400), `/denied` (401), `/forbidden` (403) endpoints.
- **Full suite: 169 passed, 85.62% coverage** (floor 80%). Ruff check + format clean, mypy clean.

## Review history

- `ce1e434` — WO-2 initial (8 findings fixed)
- `b7f2988` — follow-up (rebase onto WO-1-merged main, 3 findings fixed)
- `95ce427` — precise 4xx typing for setup HTTP failures
- Accepted by orchestrator on 2026-10-09
