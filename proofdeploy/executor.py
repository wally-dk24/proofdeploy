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
- ``${NAME}`` placeholders must name a known binding (contract ``env``
  key, fixture key, ``AUTH_TOKEN``, or a capture declared by a setup
  step). Unknown names are INCONCLUSIVE (reason ``probe``), never sent
  literally. A declared capture that was never set is also
  INCONCLUSIVE (reason ``probe``).
- Setup steps capture named bindings from their responses; later steps,
  the act, and the assertions reference them as ``${NAME}``.
- ``${AUTH_TOKEN}`` precedence: a provider-issued token beats the
  fixture's, which beats any setup-step capture.
- Proactive credential provider: when a ``credential_config`` exists, the
  runner mints or refreshes the token BEFORE requests are sent (or when a
  provider-issued token nears expiry). It never looks at a response
  status, and it never re-sends a request. A 401 from the target is the
  probe's data for the assertions, never a refresh signal. A provider
  failure is INCONCLUSIVE (reason ``auth``) before the request is sent.
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
from proofdeploy.setup_auth import (
    CredentialProvider,
    PlaceholderError,
    SetupContext,
    capture_bindings,
    mint_ghost_jwt,
)


class Verdict(Enum):
    PASS = "pass"
    FAIL = "fail"
    INCONCLUSIVE = "inconclusive"


class InconclusiveReason(Enum):
    """Typed reason for an INCONCLUSIVE verdict.

    Policy: ENVIRONMENT is the only reason class an admin may downgrade
    to "warn". AUTH and PROBE can never be downgraded.
    """

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
    # The act request actually sent (method, url, headers, body), for
    # replayability. None when no request was sent (fail-closed before it).
    request: dict[str, Any] | None = None

    def to_dict(self) -> dict:
        return {
            "probe_index": self.probe_index,
            "verdict": self.verdict.value,
            "reason": self.reason.value if self.reason else None,
            "request": self.request,
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
    # Contract `env` keys: known `${NAME}` bindings for every probe run.
    contract_env: dict[str, str] = field(default_factory=dict)
    # Fixture mapping (from proofdeploy.yml): all keys are known bindings.
    # The fixture's `auth_token` key (if present) becomes ${AUTH_TOKEN}.
    fixture: dict[str, str] = field(default_factory=dict)
    # Explicit auth token; wins over fixture["auth_token"] when both set.
    auth_token: str | None = None
    # Proactive credential provider config (None = no provider; the fixture
    # token, if any, is sent as-is and a 401 is the probe's data).
    # {"mode": "ghost_jwt", "key": "${ADMIN_KEY}", "ttl_seconds": 300} mints
    #   a short-lived Ghost Admin API JWT per request (Ghost has no refresh
    #   endpoint).
    # {"mode": "refresh", "path": "/auth/refresh", "method": "POST",
    #  "headers": {...}, "body": {...}, "token_json_path": "access_token",
    #  "expires_in_json_path": "expires_in"} calls the refresh endpoint,
    #   but only when there is no token or a provider-issued token nears
    #   expiry. The provider runs BEFORE requests are sent; it never reacts
    #   to a response and never re-sends a request.
    credential_config: dict[str, Any] | None = None

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

    def _make_context(self, setup_steps: list[dict[str, Any]]) -> SetupContext:
        """Build the SetupContext for one probe run.

        Known `${NAME}` bindings: contract env keys, fixture keys,
        AUTH_TOKEN (from the explicit token or the fixture), and every
        capture name declared by the setup steps.
        """
        token = self.auth_token
        if token is None and self.fixture.get("auth_token"):
            token = self.fixture["auth_token"]
        ctx = SetupContext(auth_token=token)
        ctx.bindings.update(self.contract_env)
        ctx.known_names.update(self.contract_env.keys())
        for k, v in self.fixture.items():
            if k != "auth_token":
                ctx.bindings.setdefault(k, v)
                ctx.known_names.add(k)
        ctx.declare_from_setup(setup_steps)
        # When a credential provider is configured, AUTH_TOKEN is a known
        # name from the start: the provider will mint it before each
        # request, so setup steps may reference ${AUTH_TOKEN} even when
        # no fixture token exists.
        if self.credential_config is not None:
            ctx.known_names.add("AUTH_TOKEN")
        return ctx

    def _ensure_credential(
        self, ctx: SetupContext, details: list[str], writes: list[str]
    ) -> InconclusiveReason | None:
        """Proactively ensure a valid credential before a request is sent.

        Never inspects any response status; never re-sends a request. A
        401 from the target is the probe's data for the assertions, not a
        refresh signal.

        Returns an InconclusiveReason when the provider fails (the caller
        reports INCONCLUSIVE before the request is sent), else None.
        """
        if self.credential_config is None:
            return None
        provider = CredentialProvider(self.credential_config)
        if not provider.token_needed(ctx):
            return None
        if provider.mode == "ghost_jwt":
            return self._provide_ghost_jwt(provider, ctx, details)
        if provider.mode == "refresh":
            return self._provide_refresh(provider, ctx, details, writes)
        details.append(f"credential provider failed: unknown mode '{provider.mode}'")
        return InconclusiveReason.AUTH

    def _provide_ghost_jwt(
        self, provider: CredentialProvider, ctx: SetupContext, details: list[str]
    ) -> InconclusiveReason | None:
        """Mint a short-lived Ghost Admin API JWT for this request."""
        cfg = provider.config
        try:
            raw_key = ctx.apply_strict(cfg.get("key", "${ADMIN_KEY}"))
        except PlaceholderError as e:
            details.append(f"credential provider failed: {e}")
            return InconclusiveReason.AUTH
        if not isinstance(raw_key, str) or not raw_key:
            details.append("credential provider failed: admin key did not resolve to a string")
            return InconclusiveReason.AUTH
        try:
            ttl = int(cfg.get("ttl_seconds", 300))
        except (TypeError, ValueError):
            details.append("credential provider failed: ttl_seconds is not an integer")
            return InconclusiveReason.AUTH
        try:
            token = mint_ghost_jwt(raw_key, ttl)
        except ValueError as e:
            details.append(f"credential provider failed: {e}")
            return InconclusiveReason.AUTH
        ctx.provide_token(token, expires_in_seconds=float(ttl))
        details.append(f"credential provider: minted ghost_jwt (ttl {ttl}s)")
        return None

    def _provide_refresh(
        self,
        provider: CredentialProvider,
        ctx: SetupContext,
        details: list[str],
        writes: list[str],
    ) -> InconclusiveReason | None:
        """Call the refresh endpoint proactively (no token or near expiry)."""
        cfg = provider.config
        path = cfg.get("path")
        token_path = cfg.get("token_json_path")
        if not isinstance(path, str) or not path.startswith("/"):
            details.append(
                "credential provider failed: credential_config needs 'path' starting with '/'"
            )
            return InconclusiveReason.AUTH
        if not isinstance(token_path, str) or not token_path:
            details.append("credential provider failed: credential_config needs 'token_json_path'")
            return InconclusiveReason.AUTH
        method = str(cfg.get("method", "POST")).upper()
        try:
            headers = ctx.apply_strict(cfg.get("headers") or {})
            body = ctx.apply_strict(cfg.get("body"))
        except PlaceholderError as e:
            details.append(f"credential provider failed: {e}")
            return InconclusiveReason.AUTH
        url = self.target_url.rstrip("/") + path
        allowed, detail = _check_remote_method(method, url)
        if not allowed:
            details.append(f"credential provider refused: {detail}")
            return InconclusiveReason.AUTH
        allowed, detail = self._check_write_allowed(method, "credential provider")
        if not allowed:
            details.append(f"credential provider refused: {detail}")
            return InconclusiveReason.AUTH
        details.append(f"credential provider: {method} {path}")
        status, _, resp_body, req_logs = _do_http(
            method, url, headers if isinstance(headers, dict) else None, body, self.http_timeout
        )
        details.extend(req_logs)
        if method.upper() in WRITE_METHODS:
            self._log_write(method, url, status, writes, details)
        if status is None or not (200 <= status < 300):
            details.append(f"credential provider failed: refresh endpoint returned {status}")
            return InconclusiveReason.AUTH
        try:
            data = json.loads(resp_body)
        except json.JSONDecodeError:
            details.append("credential provider failed: refresh response is not JSON")
            return InconclusiveReason.AUTH
        cur: Any = data
        for part in str(token_path).split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                cur = None
                break
        if not isinstance(cur, str) or not cur:
            details.append(
                f"credential provider failed: no token at '{token_path}' in refresh response"
            )
            return InconclusiveReason.AUTH
        expires_in: float | None = None
        expires_path = cfg.get("expires_in_json_path")
        if isinstance(expires_path, str) and expires_path:
            node: Any = data
            for part in expires_path.split("."):
                if isinstance(node, dict) and part in node:
                    node = node[part]
                else:
                    node = None
                    break
            if isinstance(node, (int, float)) and node > 0:
                expires_in = float(node)
        ctx.provide_token(cur, expires_in_seconds=expires_in)
        details.append("credential provider: token acquired")
        return None

    def run_setup(
        self, setup_steps: list[dict[str, Any]], ctx: SetupContext | None = None
    ) -> tuple[list[str], InconclusiveReason | None, str | None, list[str]]:
        """Run setup steps. Returns (logs, failure_reason, failure_detail, writes).

        harness_app steps are INCONCLUSIVE (environment) until WO-5.
        A failed step is INCONCLUSIVE, not silently continued:
        - unknown step type -> reason PROBE
        - unknown/missing ${NAME} placeholder -> reason PROBE (fail-closed)
        - unreachable target or 5xx from a setup HTTP call -> reason ENVIRONMENT
        - 401/403 from a setup HTTP call -> reason AUTH
        - other 4xx from a setup HTTP call -> reason PROBE

        Each http step's act is placeholder-substituted before the call, and
        its `capture` spec (if any) is extracted into `ctx` after a 2xx.
        When a credential provider is configured, it runs proactively
        before each request; a 401 response is never a refresh signal and
        no request is ever re-sent.
        """
        logs: list[str] = []
        writes: list[str] = []
        if ctx is None:
            ctx = self._make_context(setup_steps)
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
                raw_act = step.get("act", {})
                # Proactive credential provider: mint/refresh BEFORE the
                # request's placeholders are substituted, so a fresh token
                # lands in the request. A 401 response below is the step's
                # data, never a refresh signal; the request is never re-sent.
                provider_reason = self._ensure_credential(ctx, logs, writes)
                if provider_reason is not None:
                    return logs, provider_reason, "credential provider failed", writes
                try:
                    act = ctx.apply_strict(raw_act)
                except PlaceholderError as e:
                    return (
                        logs,
                        InconclusiveReason.PROBE,
                        f"setup[{i}]: {e}",
                        writes,
                    )
                method = act.get("method", "GET")
                path = act.get("path", "/")
                url = self.target_url.rstrip("/") + path
                allowed, detail = _check_remote_method(method, url)
                if not allowed:
                    return logs, InconclusiveReason.PROBE, f"setup[{i}]: {detail}", writes
                allowed, detail = self._check_write_allowed(method, f"setup[{i}]")
                if not allowed:
                    return logs, InconclusiveReason.PROBE, detail, writes
                status, headers, body, req_logs = _do_http(
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
                    # 401/403 -> AUTH (credential problem)
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
                # 2xx: extract declared captures for later steps.
                captured = capture_bindings(body, headers, step.get("capture") or {})
                if captured:
                    logs.append(f"setup[{i}]: captured {sorted(captured)}")
                    ctx.add(captured)
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

        # 1c. Build the setup/auth context: contract env, fixture keys,
        # AUTH_TOKEN, and declared capture names are known bindings.
        ctx = self._make_context(probe.get("setup", []))

        # 2. Setup steps. A failed step is INCONCLUSIVE, not silently continued.
        # Captures from setup steps land in ctx for the act below.
        setup_logs, setup_reason, setup_detail, setup_writes = self.run_setup(
            probe.get("setup", []), ctx
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

        # 2b. Proactive credential provider: mint/refresh BEFORE the act is
        # sent (and before its placeholders are substituted, so a fresh
        # token lands in the request). The act's response is never a
        # refresh signal and the act is never re-sent: a 401 is data for
        # the assertions below.
        provider_reason = self._ensure_credential(ctx, details, writes)
        if provider_reason is not None:
            details.append("credential provider failed")
            return ProbeResult(
                probe_index=index,
                verdict=Verdict.INCONCLUSIVE,
                reason=provider_reason,
                details=details,
                writes=writes,
            )

        # 2c. Substitute ${NAME} placeholders in the act, failing closed on
        # unknown names or declared-but-unset captures. This happens after
        # setup (so captures are available) and after the credential
        # provider (so a fresh token is substituted in).
        raw_act = probe.get("act", {})
        try:
            act = ctx.apply_strict(raw_act)
        except PlaceholderError as e:
            details.append(str(e))
            return ProbeResult(
                probe_index=index,
                verdict=Verdict.INCONCLUSIVE,
                reason=InconclusiveReason.PROBE,
                details=details,
                writes=writes,
            )
        method = act.get("method", "GET")
        path = act.get("path", "/")
        url = self.target_url.rstrip("/") + path

        # 3. The act: one HTTP request (redirects are never followed).
        # It is sent exactly once, whatever the response status.
        act_headers = act.get("headers") or {}
        act_body = act.get("body")
        status, headers, body, req_logs = _do_http(
            method, url, act_headers, act_body, self.http_timeout
        )
        details.extend(req_logs)
        if method.upper() in WRITE_METHODS:
            self._log_write(method, url, status, writes, details)
        else:
            details.append(f"act: {method} {path} -> {status}")
        # Record the request that was actually sent, for replayability.
        # (Redacted by value when the result is stored in a record.)
        sent_request: dict[str, Any] = {
            "method": method,
            "url": url,
            "headers": dict(act_headers),
            "body": act_body,
        }

        if status is None:
            details.append("target unreachable")
            return ProbeResult(
                probe_index=index,
                verdict=Verdict.INCONCLUSIVE,
                reason=InconclusiveReason.ENVIRONMENT,
                details=details,
                writes=writes,
                request=sent_request,
            )

        # 4. Substitute ${NAME} placeholders in the assertions, failing
        # closed on unknown names or declared-but-unset captures.
        try:
            assertions = ctx.apply_strict(probe.get("assert", []))
        except PlaceholderError as e:
            details.append(str(e))
            return ProbeResult(
                probe_index=index,
                verdict=Verdict.INCONCLUSIVE,
                reason=InconclusiveReason.PROBE,
                details=details,
                request=sent_request,
                http_status=status,
                http_headers=headers,
                http_body=body,
                writes=writes,
            )

        # 4. DB queries: one independent result per db assertion.
        db_results: dict[int, Any] = {}
        if any(a.get("type") == "db" for a in assertions):
            if self.db_path is None:
                details.append("db assertion but no db_path")
                return ProbeResult(
                    probe_index=index,
                    verdict=Verdict.INCONCLUSIVE,
                    reason=InconclusiveReason.ENVIRONMENT,
                    details=details,
                    request=sent_request,
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
            request=sent_request,
            http_status=status,
            http_headers=headers,
            http_body=body,
            writes=writes,
        )

    def run_all(self, probes: list[dict[str, Any]]) -> list[ProbeResult]:
        """Run every probe in order."""
        return [self.run_probe(p, i) for i, p in enumerate(probes)]
