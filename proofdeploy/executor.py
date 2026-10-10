"""Probe executor for the ProofDeploy runner (WAL-58, part 2 of 5).

Runs schema-validated probes against a live target and produces
deterministic verdicts. No model is involved in verdict decisions.

Fail-closed rules:
- Every probe is validated with ``validate_probe`` before any HTTP call.
  A schema-invalid probe is INCONCLUSIVE (reason ``probe``), not executed.
- Unknown setup types are INCONCLUSIVE (reason ``probe``), never skipped.
- ``harness_app`` setup steps are INCONCLUSIVE (reason ``environment``)
  until WO-5 can provision them.
- A setup HTTP step whose response is not 2xx makes the probe
  INCONCLUSIVE, with precise typing: 401/403 -> ``auth``, other 4xx ->
  ``probe``, 5xx -> ``environment``.
- Write methods (POST/PUT/DELETE/PATCH) are allowed only when the
  provisioner says the target was provisioned (``provisioned=True``);
  localhost alone is not sufficient. A write on a non-provisioned target
  is INCONCLUSIVE (reason ``probe``).
- Only GET and HEAD are allowed against remote (non-localhost) targets;
  anything else is INCONCLUSIVE (reason ``probe``).
- Redirects are never followed; a 3xx response is evaluated as-is.
- The HTTP client never executes code inside the target process.

Verdict precedence: FAIL beats INCONCLUSIVE beats PASS. A probe that both
fails an assertion and hits an inconclusive condition reports FAIL.
"""

from __future__ import annotations

import json
import re
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from proofdeploy.probe import ProbeRejected, validate_probe


class Verdict(Enum):
    PASS = "pass"
    FAIL = "fail"
    INCONCLUSIVE = "inconclusive"


class InconclusiveReason(Enum):
    """Typed reason for an INCONCLUSIVE verdict."""

    ENVIRONMENT = "environment"  # target/db/setup could not be evaluated
    AUTH = "auth"  # credential missing or expired (fixture/auth flow)
    DRIFT = "drift"  # target API shape drifted from what the probe expects
    PROBE = "probe"  # the probe itself is defective


# Only GET and HEAD may hit a remote (non-localhost) target.
SAFE_REMOTE_METHODS = frozenset({"GET", "HEAD"})

# Write methods are logged on every request (disposable targets).
WRITE_METHODS = frozenset({"POST", "PUT", "DELETE", "PATCH"})

_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow redirects; evaluate 3xx responses as-is."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


# Module-level opener that refuses to follow redirects.
_OPENER = urllib.request.build_opener(_NoRedirect)


@dataclass
class ProbeResult:
    """The outcome of running one probe."""

    probe_index: int
    verdict: Verdict
    reason: InconclusiveReason | None = None  # set when verdict is INCONCLUSIVE
    details: list[str] = field(default_factory=list)
    http_status: int | None = None
    http_headers: dict[str, str] = field(default_factory=dict)
    http_body: str = ""
    writes: list[str] = field(default_factory=list)  # every write, logged

    def to_dict(self) -> dict:
        return {
            "probe_index": self.probe_index,
            "verdict": self.verdict.value,
            "reason": self.reason.value if self.reason else None,
            "http_status": self.http_status,
            "http_headers": self.http_headers,
            "http_body": self.http_body[:2000],
            "writes": self.writes,
            "details": self.details,
        }


def _is_local_url(url: str) -> bool:
    """True for localhost targets (provisioned instances); False for remote."""
    try:
        host = urllib.parse.urlparse(url).hostname or ""
    except Exception:
        return False
    return host.lower() in _LOCAL_HOSTS


def _do_http(
    method: str,
    url: str,
    headers: dict[str, str] | None = None,
    body: Any = None,
    timeout: int = 30,
) -> tuple[int | None, dict[str, str], str, list[str]]:
    """Perform one HTTP request, never following redirects.

    Returns (status, headers, body, log lines). HTTP error statuses
    (including 3xx) are valid responses for assertions.
    """
    logs: list[str] = []
    data: bytes | None = None
    req_headers = dict(headers or {})
    if body is not None:
        if isinstance(body, (dict, list)):
            data = json.dumps(body).encode("utf-8")
            req_headers.setdefault("Content-Type", "application/json")
        elif isinstance(body, str):
            data = body.encode("utf-8")
        else:
            data = bytes(body)
    req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            raw_headers = {k.lower(): v for k, v in resp.headers.items()}
            raw_body = resp.read().decode("utf-8", errors="replace")
            return resp.status, raw_headers, raw_body, logs
    except urllib.error.HTTPError as e:
        # HTTP error statuses (4xx, 5xx) — and 3xx when the redirect
        # handler refuses to follow — are valid responses for assertions.
        try:
            raw_body = e.read().decode("utf-8", errors="replace")
        except Exception:
            raw_body = ""
        raw_headers = {k.lower(): v for k, v in (e.headers.items() if e.headers else [])}
        return e.code, raw_headers, raw_body, logs
    except Exception as e:
        logs.append(f"HTTP request failed: {e}")
        return None, {}, "", logs


def _do_db_query(db_path: str, query: str, timeout: int = 30) -> tuple[Any, list[str]]:
    """Run a read-only DB query. Returns (result, log lines)."""
    logs: list[str] = []
    # Fail closed: only SELECT (and read-only pragmas) allowed.
    stripped = query.strip().upper()
    if not (stripped.startswith("SELECT") or stripped.startswith("PRAGMA")):
        logs.append(f"rejected non-read-only query: {query[:80]}")
        return None, logs
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=timeout)
        try:
            cur = conn.execute(query)
            rows = cur.fetchall()
            return rows, logs
        finally:
            conn.close()
    except Exception as e:
        logs.append(f"DB query failed: {e}")
        return None, logs


def _get_json_path(body: str, path: str) -> tuple[Any, bool]:
    """Extract a value from a JSON body via dotted path. Returns (value, found)."""
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return None, False
    cur: Any = data
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return None, False
    return cur, True


def _check_assertion(
    assertion: dict[str, Any],
    status: int | None,
    headers: dict[str, str],
    body: str,
    db_result: Any,
) -> tuple[bool | None, str, InconclusiveReason | None]:
    """Check one assertion.

    Returns (passed, detail, inconclusive_reason):
    - (True, detail, None): assertion held.
    - (False, detail, None): assertion did not hold (FAIL).
    - (None, detail, reason): could not evaluate (INCONCLUSIVE with reason).
    """
    atype = assertion.get("type")

    if atype == "status":
        expected = assertion.get("equals")
        if status is None:
            return None, "status assertion: no HTTP response", InconclusiveReason.ENVIRONMENT
        ok = status == expected
        return ok, f"status: got {status}, expected {expected} -> {'PASS' if ok else 'FAIL'}", None

    if atype == "header":
        name = str(assertion.get("name", "")).lower()
        actual = headers.get(name)
        if actual is None:
            return False, f"header '{name}': missing -> FAIL", None
        if "equals" in assertion:
            ok = actual == assertion["equals"]
            return ok, f"header '{name}': got {actual!r} -> {'PASS' if ok else 'FAIL'}", None
        if "contains" in assertion:
            ok = assertion["contains"] in actual
            return ok, f"header '{name}' contains -> {'PASS' if ok else 'FAIL'}", None
        if "matches" in assertion:
            ok = re.search(assertion["matches"], actual) is not None
            return ok, f"header '{name}' matches -> {'PASS' if ok else 'FAIL'}", None
        # validate_probe requires a comparator; this is defense in depth.
        return None, f"header '{name}': no comparator", InconclusiveReason.PROBE

    if atype == "body":
        if "equals" in assertion:
            ok = body == assertion["equals"]
            return ok, f"body equals -> {'PASS' if ok else 'FAIL'}", None
        if "contains" in assertion:
            ok = assertion["contains"] in body
            return ok, f"body contains -> {'PASS' if ok else 'FAIL'}", None
        if "matches" in assertion:
            ok = re.search(assertion["matches"], body) is not None
            return ok, f"body matches -> {'PASS' if ok else 'FAIL'}", None
        if "json_path" in assertion:
            jp = assertion["json_path"]
            val, found = _get_json_path(body, jp.get("path", ""))
            if not found:
                # The API shape drifted from what the probe expects.
                return (
                    None,
                    f"body json_path '{jp.get('path')}': not found",
                    InconclusiveReason.DRIFT,
                )
            expected = jp.get("equals")
            ok = val == expected
            return ok, f"body json_path: got {val!r} -> {'PASS' if ok else 'FAIL'}", None
        return None, "body: no comparator", InconclusiveReason.PROBE

    if atype == "db":
        if db_result is None:
            return (
                None,
                "db assertion: query failed or was rejected",
                InconclusiveReason.ENVIRONMENT,
            )
        if "count" in assertion:
            ok = len(db_result) == assertion["count"]
            return ok, f"db count: got {len(db_result)} -> {'PASS' if ok else 'FAIL'}", None
        if "equals" in assertion:
            ok = db_result == assertion["equals"]
            return ok, f"db equals -> {'PASS' if ok else 'FAIL'}", None
        return None, "db: no comparator", InconclusiveReason.PROBE

    return None, f"unknown assertion type '{atype}'", InconclusiveReason.PROBE


def _check_remote_method(method: str, url: str) -> tuple[bool, str]:
    """Fail-closed policy: only GET/HEAD on remote targets.

    Returns (allowed, detail).
    """
    if _is_local_url(url):
        return True, ""
    if method.upper() in SAFE_REMOTE_METHODS:
        return True, ""
    return False, (
        f"method {method} not allowed on remote target "
        f"(only {sorted(SAFE_REMOTE_METHODS)}); failing closed"
    )


@dataclass
class Executor:
    """Runs validated probes against a live target."""

    target_url: str
    db_path: str | None = None
    http_timeout: int = 30
    # Fail-closed: harness_app setup steps are rejected unless the caller
    # explicitly opts in (dev-set library repos only).
    allow_harness_app: bool = False
    # Fail-closed: write methods (POST/PUT/DELETE/PATCH) are allowed only
    # when the target was provisioned by the provisioner. Localhost alone
    # is NOT sufficient.
    provisioned: bool = False

    def _log_write(
        self, method: str, url: str, status: int | None, writes: list[str], details: list[str]
    ) -> None:
        """Log every write operation (disposable provisioned instances)."""
        line = f"write: {method} {url} -> {status}"
        writes.append(line)
        details.append(line)

    def _check_write_allowed(self, method: str, where: str) -> tuple[bool, str]:
        """Fail-closed write gate: writes need a provisioned target.

        Returns (allowed, detail).
        """
        if method.upper() not in WRITE_METHODS:
            return True, ""
        if self.provisioned:
            return True, ""
        return False, (
            f"{where}: write method {method} requires a provisioned target; failing closed"
        )

    def run_setup(
        self, setup_steps: list[dict[str, Any]]
    ) -> tuple[list[str], InconclusiveReason | None, str | None, list[str]]:
        """Run setup steps. Returns (logs, failure_reason, failure_detail, writes).

        harness_app steps are handled by the provisioner, not here.
        A failed step is INCONCLUSIVE, not silently continued:
        - unknown step type -> reason PROBE
        - unreachable target or 5xx from a setup HTTP call -> reason ENVIRONMENT
        - 401/403 from a setup HTTP call -> reason AUTH
        - other 4xx from a setup HTTP call -> reason PROBE
        """
        logs: list[str] = []
        writes: list[str] = []
        for i, step in enumerate(setup_steps):
            stype = step.get("type")
            if stype == "harness_app":
                # harness_app provisioning is not available until WO-5.
                logs.append(f"setup[{i}]: harness_app provisioning not yet available (WO-5)")
                return (
                    logs,
                    InconclusiveReason.ENVIRONMENT,
                    f"setup[{i}]: harness_app provisioning not yet available (WO-5)",
                    writes,
                )
            if stype == "http":
                act = step.get("act", {})
                method = act.get("method", "GET")
                path = act.get("path", "/")
                url = self.target_url.rstrip("/") + path
                allowed, detail = _check_remote_method(method, url)
                if not allowed:
                    return logs, InconclusiveReason.PROBE, f"setup[{i}]: {detail}", writes
                allowed, detail = self._check_write_allowed(method, f"setup[{i}]")
                if not allowed:
                    return logs, InconclusiveReason.PROBE, detail, writes
                status, _, _, req_logs = _do_http(
                    method, url, act.get("headers"), act.get("body"), self.http_timeout
                )
                logs.extend(req_logs)
                if method.upper() in WRITE_METHODS:
                    self._log_write(method, url, status, writes, logs)
                else:
                    logs.append(f"setup[{i}]: {method} {path} -> {status}")
                if status is None:
                    return (
                        logs,
                        InconclusiveReason.ENVIRONMENT,
                        f"setup[{i}]: target unreachable during setup",
                        writes,
                    )
                if not (200 <= status < 300):
                    # Precise INCONCLUSIVE typing for setup failures:
                    # 401/403 -> AUTH (credential problem; admin may downgrade)
                    # other 4xx -> PROBE (the probe's request was wrong)
                    # 5xx -> ENVIRONMENT (target-side failure)
                    if status in (401, 403):
                        reason = InconclusiveReason.AUTH
                    elif 400 <= status < 500:
                        reason = InconclusiveReason.PROBE
                    else:
                        reason = InconclusiveReason.ENVIRONMENT
                    return (
                        logs,
                        reason,
                        f"setup[{i}]: setup call returned {status}",
                        writes,
                    )
                continue
            # Fail closed: unknown setup types are INCONCLUSIVE, never skipped.
            return (
                logs,
                InconclusiveReason.PROBE,
                f"setup[{i}]: unknown setup type '{stype}'",
                writes,
            )
        return logs, None, None, writes

    def run_probe(self, probe: dict[str, Any], index: int = 0) -> ProbeResult:
        """Run one probe and return its verdict.

        The probe is schema-validated before any HTTP call. Verdict
        precedence: FAIL first, then INCONCLUSIVE, then PASS.
        """
        details: list[str] = []
        writes: list[str] = []

        # 0. Schema validation before any HTTP call.
        try:
            validate_probe(probe, allow_harness_app=self.allow_harness_app)
        except ProbeRejected as e:
            details.append(f"probe rejected by schema: {e}")
            return ProbeResult(
                probe_index=index,
                verdict=Verdict.INCONCLUSIVE,
                reason=InconclusiveReason.PROBE,
                details=details,
                writes=writes,
            )

        # 1. Remote-method policy on the act, before setup runs.
        act = probe.get("act", {})
        method = act.get("method", "GET")
        path = act.get("path", "/")
        url = self.target_url.rstrip("/") + path
        allowed, detail = _check_remote_method(method, url)
        if not allowed:
            details.append(detail)
            return ProbeResult(
                probe_index=index,
                verdict=Verdict.INCONCLUSIVE,
                reason=InconclusiveReason.PROBE,
                details=details,
                writes=writes,
            )

        # 1b. Write gate on the act: writes need a provisioned target.
        allowed, detail = self._check_write_allowed(method, "act")
        if not allowed:
            details.append(detail)
            return ProbeResult(
                probe_index=index,
                verdict=Verdict.INCONCLUSIVE,
                reason=InconclusiveReason.PROBE,
                details=details,
                writes=writes,
            )

        # 2. Setup steps. A failed step is INCONCLUSIVE, not silently continued.
        setup_logs, setup_reason, setup_detail, setup_writes = self.run_setup(
            probe.get("setup", [])
        )
        details.extend(setup_logs)
        writes.extend(setup_writes)
        if setup_reason is not None:
            details.append(setup_detail or "setup failed")
            return ProbeResult(
                probe_index=index,
                verdict=Verdict.INCONCLUSIVE,
                reason=setup_reason,
                details=details,
                writes=writes,
            )

        # 3. The act: one HTTP request (redirects are never followed).
        status, headers, body, req_logs = _do_http(
            method, url, act.get("headers"), act.get("body"), self.http_timeout
        )
        details.extend(req_logs)
        if method.upper() in WRITE_METHODS:
            self._log_write(method, url, status, writes, details)
        else:
            details.append(f"act: {method} {path} -> {status}")

        if status is None:
            details.append("target unreachable")
            return ProbeResult(
                probe_index=index,
                verdict=Verdict.INCONCLUSIVE,
                reason=InconclusiveReason.ENVIRONMENT,
                details=details,
                writes=writes,
            )

        # 4. DB queries: one independent result per db assertion.
        assertions = probe.get("assert", [])
        db_results: dict[int, Any] = {}
        if any(a.get("type") == "db" for a in assertions):
            if self.db_path is None:
                details.append("db assertion but no db_path")
                return ProbeResult(
                    probe_index=index,
                    verdict=Verdict.INCONCLUSIVE,
                    reason=InconclusiveReason.ENVIRONMENT,
                    details=details,
                    http_status=status,
                    http_headers=headers,
                    http_body=body,
                    writes=writes,
                )
            for ai, a in enumerate(assertions):
                if a.get("type") != "db":
                    continue
                db_result, db_logs = _do_db_query(self.db_path, a.get("query", ""))
                details.extend(db_logs)
                db_results[ai] = db_result

        # 5. Evaluate assertions. Precedence: FAIL > INCONCLUSIVE > PASS.
        saw_fail = False
        saw_inconclusive = False
        inconclusive_reason: InconclusiveReason | None = None
        for ai, a in enumerate(assertions):
            passed, detail, ireason = _check_assertion(a, status, headers, body, db_results.get(ai))
            details.append(detail)
            if passed is None:
                saw_inconclusive = True
                if inconclusive_reason is None:
                    inconclusive_reason = ireason or InconclusiveReason.ENVIRONMENT
            elif not passed:
                saw_fail = True
            # Continue checking to log all assertion outcomes.

        if saw_fail:
            verdict, reason = Verdict.FAIL, None
        elif saw_inconclusive:
            verdict, reason = Verdict.INCONCLUSIVE, inconclusive_reason
        else:
            verdict, reason = Verdict.PASS, None

        return ProbeResult(
            probe_index=index,
            verdict=verdict,
            reason=reason,
            details=details,
            http_status=status,
            http_headers=headers,
            http_body=body,
            writes=writes,
        )

    def run_all(self, probes: list[dict[str, Any]]) -> list[ProbeResult]:
        """Run every probe in order."""
        return [self.run_probe(p, i) for i, p in enumerate(probes)]
