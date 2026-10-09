"""Probe executor for the ProofDeploy runner (WAL-58, part 2 of 5).

Runs schema-validated probes against a live target and produces
deterministic verdicts. No model is involved in verdict decisions.

Verdicts:
- PASS: all assertions held.
- FAIL: at least one assertion did not hold.
- INCONCLUSIVE: the probe could not be evaluated (target unreachable,
  DB unavailable, assertion references missing data, API drift).

The executor only sends HTTP requests and read-only DB queries. It never
executes code inside the target process.
"""

from __future__ import annotations

import json
import re
import sqlite3
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Verdict(Enum):
    PASS = "pass"
    FAIL = "fail"
    INCONCLUSIVE = "inconclusive"


@dataclass
class ProbeResult:
    """The outcome of running one probe."""

    probe_index: int
    verdict: Verdict
    details: list[str] = field(default_factory=list)
    http_status: int | None = None
    http_headers: dict[str, str] = field(default_factory=dict)
    http_body: str = ""

    def to_dict(self) -> dict:
        return {
            "probe_index": self.probe_index,
            "verdict": self.verdict.value,
            "http_status": self.http_status,
            "http_headers": self.http_headers,
            "http_body": self.http_body[:2000],
            "details": self.details,
        }


def _do_http(
    method: str,
    url: str,
    headers: dict[str, str] | None = None,
    body: Any = None,
    timeout: int = 30,
) -> tuple[int | None, dict[str, str], str, list[str]]:
    """Perform one HTTP request. Returns (status, headers, body, log lines)."""
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
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw_headers = {k.lower(): v for k, v in resp.headers.items()}
            raw_body = resp.read().decode("utf-8", errors="replace")
            return resp.status, raw_headers, raw_body, logs
    except urllib.error.HTTPError as e:
        # HTTP error statuses are valid responses for assertions.
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
) -> tuple[bool | None, str]:
    """Check one assertion. Returns (passed|None for inconclusive, detail).

    True = held, False = did not hold, None = could not evaluate.
    """
    atype = assertion.get("type")

    if atype == "status":
        expected = assertion.get("equals")
        if status is None:
            return None, "status assertion: no HTTP response (inconclusive)"
        ok = status == expected
        return ok, f"status: got {status}, expected {expected} -> {'PASS' if ok else 'FAIL'}"

    if atype == "header":
        name = str(assertion.get("name", "")).lower()
        actual = headers.get(name)
        if actual is None:
            return False, f"header '{name}': missing -> FAIL"
        if "equals" in assertion:
            ok = actual == assertion["equals"]
            return ok, f"header '{name}': got {actual!r} -> {'PASS' if ok else 'FAIL'}"
        if "contains" in assertion:
            ok = assertion["contains"] in actual
            return ok, f"header '{name}' contains -> {'PASS' if ok else 'FAIL'}"
        if "matches" in assertion:
            ok = re.search(assertion["matches"], actual) is not None
            return ok, f"header '{name}' matches -> {'PASS' if ok else 'FAIL'}"
        return None, f"header '{name}': no comparator (inconclusive)"

    if atype == "body":
        if "equals" in assertion:
            ok = body == assertion["equals"]
            return ok, f"body equals -> {'PASS' if ok else 'FAIL'}"
        if "contains" in assertion:
            ok = assertion["contains"] in body
            return ok, f"body contains -> {'PASS' if ok else 'FAIL'}"
        if "matches" in assertion:
            ok = re.search(assertion["matches"], body) is not None
            return ok, f"body matches -> {'PASS' if ok else 'FAIL'}"
        if "json_path" in assertion:
            jp = assertion["json_path"]
            val, found = _get_json_path(body, jp.get("path", ""))
            if not found:
                return None, f"body json_path '{jp.get('path')}': not found (inconclusive)"
            expected = jp.get("equals")
            ok = val == expected
            return ok, f"body json_path: got {val!r} -> {'PASS' if ok else 'FAIL'}"
        return None, "body: no comparator (inconclusive)"

    if atype == "db":
        if db_result is None:
            return None, "db assertion: query failed or was rejected (inconclusive)"
        if "count" in assertion:
            ok = len(db_result) == assertion["count"]
            return ok, f"db count: got {len(db_result)} -> {'PASS' if ok else 'FAIL'}"
        if "equals" in assertion:
            ok = db_result == assertion["equals"]
            return ok, f"db equals -> {'PASS' if ok else 'FAIL'}"
        return None, "db: no comparator (inconclusive)"

    return None, f"unknown assertion type '{atype}' (inconclusive)"


@dataclass
class Executor:
    """Runs validated probes against a live target."""

    target_url: str
    db_path: str | None = None
    http_timeout: int = 30

    def run_setup(self, setup_steps: list[dict[str, Any]]) -> list[str]:
        """Run setup steps (HTTP calls). Returns log lines.

        harness_app steps are handled by the provisioner, not here.
        """
        logs: list[str] = []
        for i, step in enumerate(setup_steps):
            stype = step.get("type")
            if stype == "harness_app":
                logs.append(f"setup[{i}]: harness_app handled by provisioner, skipping")
                continue
            if stype == "http":
                act = step.get("act", {})
                method = act.get("method", "GET")
                path = act.get("path", "/")
                url = self.target_url.rstrip("/") + path
                status, _, _, req_logs = _do_http(
                    method, url, act.get("headers"), act.get("body"), self.http_timeout
                )
                logs.extend(req_logs)
                logs.append(f"setup[{i}]: {method} {path} -> {status}")
            else:
                logs.append(f"setup[{i}]: unknown type '{stype}', skipping")
        return logs

    def run_probe(self, probe: dict[str, Any], index: int = 0) -> ProbeResult:
        """Run one validated probe and return its verdict."""
        details: list[str] = []

        # 1. Setup steps.
        setup_logs = self.run_setup(probe.get("setup", []))
        details.extend(setup_logs)

        # 2. The act: one HTTP request.
        act = probe.get("act", {})
        method = act.get("method", "GET")
        path = act.get("path", "/")
        url = self.target_url.rstrip("/") + path
        status, headers, body, req_logs = _do_http(
            method, url, act.get("headers"), act.get("body"), self.http_timeout
        )
        details.extend(req_logs)
        details.append(f"act: {method} {path} -> {status}")

        if status is None:
            details.append("target unreachable: INCONCLUSIVE")
            return ProbeResult(
                probe_index=index,
                verdict=Verdict.INCONCLUSIVE,
                details=details,
            )

        # 3. DB queries for db assertions (run once, share across assertions).
        db_result: Any = None
        db_queries = [
            a.get("query") for a in probe.get("assert", []) if a.get("type") == "db"
        ]
        if db_queries:
            if self.db_path is None:
                details.append("db assertion but no db_path: INCONCLUSIVE")
                return ProbeResult(
                    probe_index=index,
                    verdict=Verdict.INCONCLUSIVE,
                    details=details,
                    http_status=status,
                    http_headers=headers,
                    http_body=body,
                )
            # All db assertions in one probe share the first query's result.
            # (Multi-query probes are a future extension.)
            db_result, db_logs = _do_db_query(self.db_path, db_queries[0])
            details.extend(db_logs)

        # 4. Evaluate assertions.
        verdict = Verdict.PASS
        for a in probe.get("assert", []):
            passed, detail = _check_assertion(a, status, headers, body, db_result)
            details.append(detail)
            if passed is None:
                verdict = Verdict.INCONCLUSIVE
            elif not passed:
                verdict = Verdict.FAIL
                # Continue checking to log all assertion outcomes.

        return ProbeResult(
            probe_index=index,
            verdict=verdict,
            details=details,
            http_status=status,
            http_headers=headers,
            http_body=body,
        )

    def run_all(self, probes: list[dict[str, Any]]) -> list[ProbeResult]:
        """Run every probe in order."""
        return [self.run_probe(p, i) for i, p in enumerate(probes)]
