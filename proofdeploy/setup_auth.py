"""Setup capture and auth injection for the ProofDeploy runner (WAL-58, WO-3).

Setup steps can capture named bindings from their HTTP responses (e.g. a
token for a user created in setup). Later steps — and the probe's act and
assertions — reference bindings as `${NAME}`.

Placeholder rules (fail-closed):
- `${NAME}` must name a known binding: a contract `env` key, a fixture key,
  `AUTH_TOKEN`, or a capture declared by an earlier setup step. Anything
  else is INCONCLUSIVE (reason `probe`) — never sent literally.
- A declared capture that was never set (the capturing step failed to
  produce it, or the step order is wrong) is INCONCLUSIVE (reason `probe`).

`${AUTH_TOKEN}` precedence (highest first):
1. A token from the proactive credential provider (minted/refreshed before
   requests are sent).
2. The fixture's AUTH_TOKEN.
3. Setup-step captures. A setup step that captures AUTH_TOKEN is ignored
   when the fixture provides one — the fixture always wins over captures.

Credential provider (proactive, never reactive):
- The provider mints or refreshes the token BEFORE requests are sent, or
  when a provider-issued token nears expiry. It never looks at a response
  status, and it never re-sends a request. A 401 from the target is the
  probe's data (the assertions evaluate it); it is never a refresh signal.
- `ghost_jwt` mode mints a short-lived Ghost Admin API JWT per request
  from the instance's admin key (Ghost has no refresh endpoint). The
  key comes from a fixture binding or, declared in the contract
  (WO-8.3), from a checkout file the instance's own seed wrote.
- `login` mode (WO-8.3) POSTs the contract's declared login path with
  a body template whose placeholders resolve from the run's bindings —
  the channel through which the per-run bootstrap credentials
  (`auth.bootstrap_env`, generated fresh per side and per run) feed
  login and refresh.
- `refresh` mode calls a refresh endpoint, but only when there is no
  token or a provider-issued token nears expiry. Fixture tokens have no
  known expiry and are never refreshed proactively. With a login path
  declared, the first acquisition is a login; later ones refresh.
- `register` mode registers a disposable user on the target (declared in
  the contract, never a default): POSTs to a declared endpoint with
  generated credentials and extracts the token from the response. A
  provider failure is INCONCLUSIVE `auth`, never an exception.

harness_app setup steps are provisioned for real by the executor
(WO-5): the step's source is written to a file and started as a process
on a loopback port, which becomes the probe's target for the rest of the
run.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import hmac
import json
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Any


def generate_bootstrap_env(names: list[str]) -> dict[str, str]:
    """Generate fresh random values for declared bootstrap env NAMES.

    WO-8.3 (Addenda 43/44): the contract's `auth.bootstrap_env`
    declares NAMES only; ProofDeploy generates the values. Every call
    returns new values — fresh per side and per run, random, never
    reused, never derived from a customer value. The values are
    credentials for the disposable instance only: they are injected
    into that side's install and app containers, feed the side's
    login/refresh provider, and join the record's redaction set.
    They are never written to proofdeploy.yml or any committed file;
    the record names them only as "generated", with their env names.
    The prompt and the bundle are assembled and leak-checked before
    any value is generated (authoring precedes provisioning), so a
    generated value cannot appear in either — and the leak check
    refuses these values wherever they are supplied to it as extra
    secrets (the forbidden-values set).

    A name containing "EMAIL" gets an email-shaped value at
    example.com (the IANA documentation domain: syntactically a real
    address format — strict login validators accept it — but no
    mailbox exists there and nothing is ever sent to it); every other
    name gets 32 hex characters.
    """
    out: dict[str, str] = {}
    for name in names:
        if "EMAIL" in name.upper():
            out[name] = f"pd-{secrets.token_hex(6)}@example.com"
        else:
            out[name] = secrets.token_hex(16)
    return out

# `${NAME}` placeholders in strings.
_BINDING_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
# A value that is exactly one placeholder, e.g. "${ITEM_ID}". These
# substitute the typed binding value (ADR-0011); placeholders embedded in
# a larger string substitute the binding's text form.
_WHOLE_PLACEHOLDER_RE = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")


class PlaceholderError(ValueError):
    """Base class for `${NAME}` substitution failures (fail-closed)."""


class UnknownPlaceholder(PlaceholderError):
    """A `${NAME}` that names no known binding (not env, fixture, or capture)."""


class MissingCapture(PlaceholderError):
    """A `${NAME}` naming a declared capture that was never set."""


def substitute_strict(value: Any, bindings: dict[str, Any], known_names: set[str]) -> Any:
    """Recursively replace `${NAME}` placeholders, failing closed.

    - A name in `bindings` is substituted.
    - A name in `known_names` but not `bindings` raises MissingCapture
      (a declared capture that was never set).
    - Any other name raises UnknownPlaceholder (never sent literally).

    Typed substitution (ADR-0011): when the whole value is exactly one
    placeholder (e.g. `"${ITEM_ID}"`), the binding's JSON type is kept
    (an int capture stays an int). Placeholders embedded in a larger
    string (e.g. `"/items/${ITEM_ID}"`) substitute the text form.
    """
    if isinstance(value, str):
        whole = _WHOLE_PLACEHOLDER_RE.match(value)
        if whole is not None:
            name = whole.group(1)
            if name in bindings:
                return bindings[name]
            if name in known_names:
                raise MissingCapture(f"capture ${{{name}}} was declared but never set")
            raise UnknownPlaceholder(
                f"unknown placeholder ${{{name}}}: not a contract env key, "
                "fixture key, or declared capture"
            )

        def _repl(m: re.Match[str]) -> str:
            name = m.group(1)
            if name in bindings:
                return str(bindings[name])
            if name in known_names:
                raise MissingCapture(f"capture ${{{name}}} was declared but never set")
            raise UnknownPlaceholder(
                f"unknown placeholder ${{{name}}}: not a contract env key, "
                "fixture key, or declared capture"
            )

        return _BINDING_RE.sub(_repl, value)
    if isinstance(value, dict):
        return {k: substitute_strict(v, bindings, known_names) for k, v in value.items()}
    if isinstance(value, list):
        return [substitute_strict(v, bindings, known_names) for v in value]
    return value


def capture_bindings(
    response_body: str,
    response_headers: dict[str, str],
    capture: dict[str, dict[str, str]],
) -> dict[str, Any]:
    """Extract named bindings from an HTTP response.

    `capture` maps binding name -> {"from": "body"|"header", ...}:
    - body: {"from": "body", "json_path": "dotted.path"}
    - header: {"from": "header", "name": "x-token"}

    Returns the captured bindings (name -> value). Captures keep their
    JSON type (ADR-0011): an int id stays an int, so a later whole-value
    `${NAME}` substitution compares typed values. Missing values are
    skipped, not errors.
    """
    out: dict[str, Any] = {}
    for name, spec in capture.items():
        source = spec.get("from")
        if source == "header":
            val = response_headers.get(str(spec.get("name", "")).lower())
            if val is not None:
                out[name] = val
        elif source == "body":
            path = str(spec.get("json_path", ""))
            try:
                data = json.loads(response_body)
            except json.JSONDecodeError:
                continue
            cur: Any = data
            found = True
            for part in path.split("."):
                if isinstance(cur, dict) and part in cur:
                    cur = cur[part]
                else:
                    found = False
                    break
            if found and isinstance(cur, (str, int, float, bool)):
                out[name] = cur
    return out


def collect_capture_names(setup_steps: list[dict[str, Any]]) -> set[str]:
    """Collect every capture name declared by the setup steps."""
    names: set[str] = set()
    for step in setup_steps:
        capture = step.get("capture")
        if isinstance(capture, dict):
            names.update(capture.keys())
    return names


@dataclass
class SetupContext:
    """Bindings available during a probe run.

    `auth_token` is the fixture's auth binding. `bindings` accumulates
    contract env keys, fixture keys, and captures from setup steps in order.
    `known_names` holds every name a `${NAME}` may legally reference.
    """

    auth_token: str | None = None
    bindings: dict[str, Any] = field(default_factory=dict)
    known_names: set[str] = field(default_factory=set)
    # Set by the proactive credential provider; beats the fixture token.
    _provided_token: str | None = field(default=None, repr=False)
    # Unix timestamp when the provider-issued token expires (None when the
    # provider did not report an expiry, or no provider token is set).
    token_expires_at: float | None = field(default=None, repr=False)
    # Secrets accumulated during THIS probe run, for the record's
    # fail-closed redaction. run_captures holds setup-step captures
    # (populated by add()); run_minted holds provider-issued tokens
    # (populated by provide_token()). The executor copies these onto the
    # ProbeResult so build_evidence_record can collect them from the
    # results instead of trusting the caller to pass them in.
    run_captures: dict[str, Any] = field(default_factory=dict, repr=False)
    # Every DISTINCT value captured under each name during THIS probe
    # run. run_captures keeps only the latest value per name (that is
    # what substitution needs), but a name captured more than once —
    # e.g. a re-login — produced one secret per capture, and all of
    # them must reach the record's redaction (Addendum 23 R1).
    run_captures_all: dict[str, list[Any]] = field(default_factory=dict, repr=False)
    run_minted: list[str] = field(default_factory=list, repr=False)
    # Per-run bootstrap credential values in effect for this context
    # (WO-8.3), set by the executor from its `bootstrap_env`. Copied to
    # the ProbeResult at assembly so the values join the record's
    # redaction set (the record names them only as "generated").
    run_bootstrap: list[str] = field(default_factory=list, repr=False)
    # Set by a harness_app setup step (WO-5): subsequent setup steps and
    # the probe's act target the harness app instead of the provisioned
    # target. None when no harness_app step ran.
    target_override: str | None = None
    # Processes started by harness_app setup steps during this probe run.
    # The executor stops them when the probe run ends.
    harness_procs: list[Any] = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        if self.effective_token() is not None:
            self.bindings.setdefault("AUTH_TOKEN", self.effective_token())  # type: ignore[arg-type]
            self.known_names.add("AUTH_TOKEN")

    def effective_token(self) -> str | None:
        """The live auth token: provider token beats the fixture token."""
        return self._provided_token if self._provided_token is not None else self.auth_token

    def declare(self, names: set[str]) -> None:
        """Register names that `${NAME}` may reference (e.g. declared captures)."""
        self.known_names.update(names)

    def declare_from_setup(self, setup_steps: list[dict[str, Any]]) -> None:
        """Register every capture name declared by the setup steps."""
        self.declare(collect_capture_names(setup_steps))

    def add(self, new: dict[str, Any]) -> None:
        """Add captured bindings (later captures overwrite earlier ones).

        AUTH_TOKEN is special: the fixture's token (or a provider-issued
        one) always wins over a setup-step capture. Captures are also
        recorded in run_captures for the record's fail-closed redaction,
        and every distinct value per name in run_captures_all: an
        overwritten capture was still a live secret during this run.
        """
        self.bindings.update(new)
        self.run_captures.update(new)
        for _k, _v in new.items():
            _bucket = self.run_captures_all.setdefault(_k, [])
            if _v not in _bucket:
                _bucket.append(_v)
        token = self.effective_token()
        if token is not None:
            self.bindings["AUTH_TOKEN"] = token

    def provide_token(self, new_token: str, expires_in_seconds: float | None = None) -> None:
        """Install a token from the proactive credential provider.

        This is the one sanctioned override of the fixture token. When
        `expires_in_seconds` is given, the provider-issued token is
        refreshed proactively as it nears expiry. Minted tokens are
        recorded in run_minted for the record's fail-closed redaction.
        """
        self._provided_token = new_token
        self.bindings["AUTH_TOKEN"] = new_token
        self.known_names.add("AUTH_TOKEN")
        self.run_minted.append(new_token)
        self.token_expires_at = (
            time.time() + expires_in_seconds if expires_in_seconds is not None else None
        )

    def token_near_expiry(self, margin_seconds: float = 60) -> bool:
        """True when a provider-issued token nears expiry.

        Fixture tokens have no known expiry: they are never refreshed
        proactively. A 401 from the target is the probe's data, not a
        refresh signal.
        """
        if self.token_expires_at is None:
            return False
        return time.time() > self.token_expires_at - margin_seconds

    def apply_strict(self, value: Any) -> Any:
        """Substitute `${NAME}` placeholders, failing closed on bad names."""
        return substitute_strict(copy.deepcopy(value), self.bindings, self.known_names)


def _b64url(data: bytes) -> str:
    """Base64url-encode without padding (JWT encoding)."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def mint_ghost_jwt(admin_key: str, ttl_seconds: int = 300) -> str:
    """Mint a Ghost Admin API JWT from an "<id>:<secret>" admin key.

    Ghost has no refresh endpoint, so the provider mints a short-lived
    JWT per request instead. Pure stdlib (hmac + hashlib).

    Raises ValueError on a malformed key.
    """
    key_id, sep, secret_hex = admin_key.partition(":")
    if not sep or not key_id or not secret_hex:
        raise ValueError("ghost_jwt key must look like '<id>:<secret>'")
    try:
        secret = bytes.fromhex(secret_hex)
    except ValueError:
        raise ValueError("ghost_jwt key secret part is not hex") from None
    now = int(time.time())
    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT", "kid": key_id}).encode())
    payload = _b64url(json.dumps({"iat": now, "exp": now + ttl_seconds, "aud": "/admin/"}).encode())
    signing_input = f"{header}.{payload}".encode("ascii")
    sig = _b64url(hmac.new(secret, signing_input, hashlib.sha256).digest())
    return f"{header}.{payload}.{sig}"


@dataclass
class CredentialProvider:
    """Proactive credential provider.

    Mints or refreshes the auth token BEFORE requests are sent — never in
    reaction to a response. A 401 from the target is the probe's data (the
    assertions evaluate it); it is never a refresh signal, and no request
    is ever re-sent.

    Modes (from the `credential_config` mapping):
    - `ghost_jwt`: mint a short-lived Ghost Admin API JWT per request from
      the instance's admin key. Config: {"mode": "ghost_jwt",
      "key": "${ADMIN_KEY}", "ttl_seconds": 300} resolves the key from a
      binding; the WO-8.3 contract form declares `key_file` instead —
      the key is created by the instance during its own seed, written
      to a declared checkout file, and the orchestrator reads it after
      READY and passes it as `key_value` (a missing file arrives as
      `key_error` and fails INCONCLUSIVE `auth`, never a crash).
    - `login` (WO-8.3): POST the declared login path with the declared
      body template — its `${NAME}` placeholders resolve from the
      run's bindings, which is how the per-run bootstrap values feed
      it — and extract the token at `token_json_path`. Config:
      {"mode": "login", "path": "/auth/login", "method": "POST",
      "body": {"email": "${ADMIN_EMAIL}", "password": "${ADMIN_PASSWORD}"},
      "token_json_path": "data.access_token",
      "expires_in_json_path": "data.expires"}.
    - `refresh` (default): call a refresh endpoint, but only when there is
      no token or a provider-issued token nears expiry. Config:
      {"mode": "refresh", "path": "/auth/refresh", "method": "POST",
      "headers": {...}, "body": {...}, "token_json_path": "access_token",
      "expires_in_json_path": "expires_in"}. With `login_path`/
      `login_body` declared (WO-8.3), the first acquisition is a login
      (as `login` mode, at the login path); the response's refresh
      token is read at `refresh_token_json_path` and bound as
      `${REFRESH_TOKEN}` for the refresh body, and later acquisitions
      refresh near expiry.
    - `register`: register a disposable user on the target. The endpoint,
      body fields, and token field are declared in the contract (never a
      default). Config: {"mode": "register", "path": "/register",
      "method": "POST", "username_field": "username",
      "password_field": "password", "token_json_path": "token"}.
      A random username and password are generated per run; both are
      secrets for the record's redaction.
    """

    config: dict[str, Any]

    @property
    def mode(self) -> str:
        return str(self.config.get("mode", "refresh"))

    def token_needed(self, ctx: SetupContext) -> bool:
        """Whether to (re)acquire a credential before the next request."""
        if self.mode == "ghost_jwt":
            # Short-lived: mint fresh for every request.
            return True
        # Refresh mode: only when there is no token, or a provider-issued
        # token nears expiry. Fixture tokens have no known expiry and are
        # never refreshed proactively.
        return ctx.effective_token() is None or ctx.token_near_expiry()


def validate_harness_app_allowed(
    setup_steps: list[dict[str, Any]],
    allow_harness_app: bool,
) -> None:
    """Reject harness_app setup steps when not allowed.

    Raises ValueError if any step is a harness_app and `allow_harness_app`
    is False. Dev-set library repos pass True; eval repos pass False.
    """
    if allow_harness_app:
        return
    for i, step in enumerate(setup_steps):
        if step.get("type") == "harness_app":
            raise ValueError(f"setup[{i}]: harness_app is only allowed for dev-set library repos")
