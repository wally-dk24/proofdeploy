"""Lean P3: request data shapes, the HTTP client and contract-driven auth.

Everything here is JSON-serializable and deterministic except wall-clock
``elapsed_ms``. No model is called; the only network use is the HTTP client
pointed at the app under test.

* ``Step`` / ``Scenario`` / ``Exchange``: what is sent and what came back.
  A scenario id is the sha256 (12 hex) of its steps and target operation, so
  the same content always has the same id. The coverage context of a step is
  ``<scenario id>:<step index>`` (``Scenario.request_id``).
* ``${name}`` values inside path params, query and body refer to a value
  captured by an earlier step (``capture: {name: json_path}``) and are
  resolved when the step is sent. In a login body, ``${NAME}`` that is not a
  captured value is read from the process environment at send time and never
  written into an exchange.
* ``AuthSpec`` is derived from the contract's ``auth:`` block (``mode: login``).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

REF_RE = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")
_PLACEHOLDER_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

# Response headers worth keeping in an exchange.
KEPT_HEADERS = ("content-type", "location", "www-authenticate", "allow", "retry-after")
MAX_BODY_CHARS = 20000

TOKEN_VAR = "token"


class UnresolvedReference(KeyError):
    """A ``${name}`` reference has no captured value."""


class UnsupportedAuth(ValueError):
    """The contract's auth mode has no login driver in the lean path."""


# --------------------------------------------------------------------------
# Data shapes
# --------------------------------------------------------------------------


@dataclass
class Step:
    method: str
    path: str
    path_params: dict[str, Any] = field(default_factory=dict)
    query: dict[str, Any] = field(default_factory=dict)
    body: Any = None
    content_type: str | None = None
    auth: bool = False
    capture: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Step:
        return cls(
            method=d["method"],
            path=d["path"],
            path_params=dict(d.get("path_params") or {}),
            query=dict(d.get("query") or {}),
            body=d.get("body"),
            content_type=d.get("content_type"),
            auth=bool(d.get("auth", False)),
            capture=dict(d.get("capture") or {}),
        )


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


@dataclass
class Scenario:
    steps: list[Step]
    target_operation: str
    id: str = ""
    # Id of the generated scenario this one was repaired from (400 repair).
    # Not part of the content hash.
    repaired_from: str | None = None

    def __post_init__(self) -> None:
        if not self.id:
            self.id = self.content_id()

    def content_id(self) -> str:
        payload = {
            "steps": [s.to_dict() for s in self.steps],
            "target": self.target_operation,
        }
        return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()[:12]

    def request_id(self, step_index: int) -> str:
        return f"{self.id}:{step_index}"

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "id": self.id,
            "steps": [s.to_dict() for s in self.steps],
            "target_operation": self.target_operation,
        }
        if self.repaired_from:
            d["repaired_from"] = self.repaired_from
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Scenario:
        return cls(
            steps=[Step.from_dict(s) for s in d["steps"]],
            target_operation=d["target_operation"],
            id=d.get("id", ""),
            repaired_from=d.get("repaired_from"),
        )


@dataclass
class Exchange:
    scenario_id: str
    step_index: int
    request: dict[str, Any]  # {method, url_path, body}
    status: int  # 0 = no HTTP response (transport error, unresolved reference)
    headers_subset: dict[str, str]
    body_json_or_text: Any
    elapsed_ms: float
    attempt: int = 0  # 0 = as generated, 1 = after 400 repair

    @property
    def request_id(self) -> str:
        return f"{self.scenario_id}:{self.step_index}"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["request_id"] = self.request_id
        return d


# --------------------------------------------------------------------------
# JSON paths and reference resolution
# --------------------------------------------------------------------------

_SEG_RE = re.compile(r"([^.\[\]]+)|\[(\d+)\]")


def json_path_get(obj: Any, path: str) -> tuple[bool, Any]:
    """Value at ``a.b[0].c`` (a leading ``$.`` is allowed); (found, value)."""
    p = path.strip()
    if p.startswith("$"):
        p = p[1:].lstrip(".")
    node = obj
    for m in _SEG_RE.finditer(p):
        key, idx = m.group(1), m.group(2)
        if idx is not None:
            if isinstance(node, list) and int(idx) < len(node):
                node = node[int(idx)]
            else:
                return False, None
        elif isinstance(node, dict) and key in node:
            node = node[key]
        else:
            return False, None
    return True, node


def resolve_refs(obj: Any, variables: dict[str, Any], *, strict: bool = True) -> Any:
    """Replace ``"${name}"`` values (whole-string) with captured values.

    A string that only contains a reference keeps the captured value's type;
    references embedded in a longer string are substituted textually. An
    unknown whole-string reference raises ``UnresolvedReference`` when
    ``strict``; otherwise it is left as is (a request body may carry
    ``${ENV_NAME}`` placeholders that are expanded from the environment at
    send time).
    """
    if isinstance(obj, str):
        m = REF_RE.match(obj)
        if m:
            if m.group(1) not in variables:
                if strict:
                    raise UnresolvedReference(m.group(1))
                return obj
            return variables[m.group(1)]
        if "${" in obj:

            def sub(mm: re.Match[str]) -> str:
                return str(variables[mm.group(1)]) if mm.group(1) in variables else mm.group(0)

            return _PLACEHOLDER_RE.sub(sub, obj)
        return obj
    if isinstance(obj, dict):
        return {k: resolve_refs(v, variables, strict=strict) for k, v in obj.items()}
    if isinstance(obj, list):
        return [resolve_refs(v, variables, strict=strict) for v in obj]
    return obj


def expand_env(obj: Any) -> Any:
    """Expand remaining ``${NAME}`` placeholders from the environment (send time only)."""
    if isinstance(obj, str):
        if "${" not in obj:
            return obj
        return _PLACEHOLDER_RE.sub(lambda m: os.environ.get(m.group(1), m.group(0)), obj)
    if isinstance(obj, dict):
        return {k: expand_env(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [expand_env(v) for v in obj]
    return obj


def fill_path(template: str, params: dict[str, Any]) -> str:
    """``/notes/{note_id}`` + ``{"note_id": 3}`` -> ``/notes/3`` (values URL-quoted)."""

    def sub(m: re.Match[str]) -> str:
        name = m.group(1)
        if name not in params:
            return m.group(0)
        return urllib.parse.quote(str(params[name]), safe="")

    return re.sub(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", sub, template)


def scrub_value(obj: Any, secret: str, label: str = "<redacted:token>") -> Any:
    """Replace ``secret`` inside a JSON-ish structure or text."""
    if not secret:
        return obj
    if isinstance(obj, str):
        return obj.replace(secret, label)
    if isinstance(obj, dict):
        return {k: scrub_value(v, secret, label) for k, v in obj.items()}
    if isinstance(obj, list):
        return [scrub_value(v, secret, label) for v in obj]
    return obj


# --------------------------------------------------------------------------
# HTTP client
# --------------------------------------------------------------------------


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def encode_body(body: Any, content_type: str | None) -> tuple[bytes | None, str | None]:
    """(bytes, content-type header value) for a step body."""
    if body is None:
        return None, content_type
    ct = (content_type or "application/json").split(";")[0].strip().lower()
    if ct == "application/x-www-form-urlencoded":
        pairs = body.items() if isinstance(body, dict) else []
        return urllib.parse.urlencode([(k, _form_value(v)) for k, v in pairs]).encode(), ct
    if ct == "multipart/form-data":
        boundary = "pdboundary" + uuid.uuid5(uuid.NAMESPACE_OID, _canonical(body)).hex[:16]
        parts: list[bytes] = []
        for k, v in body.items() if isinstance(body, dict) else []:
            parts.append(
                (
                    f"--{boundary}\r\nContent-Disposition: form-data; "
                    f'name="{k}"\r\n\r\n{_form_value(v)}\r\n'
                ).encode()
            )
        parts.append(f"--{boundary}--\r\n".encode())
        return b"".join(parts), f"multipart/form-data; boundary={boundary}"
    return json.dumps(body, ensure_ascii=False).encode("utf-8"), ct


def _form_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (dict, list)):
        return json.dumps(v)
    return str(v)


class HttpClient:
    """Minimal urllib client: no redirects, 4xx/5xx returned as data."""

    def __init__(self, base_url: str, *, timeout: float = 10.0) -> None:
        if urllib.parse.urlparse(base_url).scheme not in ("http", "https"):
            raise ValueError(f"base_url must be http(s), got {base_url!r}")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._opener = urllib.request.build_opener(_NoRedirect)

    def request(
        self,
        method: str,
        url_path: str,
        *,
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, str], str, float]:
        """(status, kept headers, body text, elapsed ms); status 0 on transport error."""
        req = urllib.request.Request(
            self.base_url + url_path, data=data, method=method.upper(), headers=headers or {}
        )
        t0 = time.perf_counter()
        status = 0
        hdrs: dict[str, str] = {}
        raw = b""
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                status, raw, hdrs = resp.status, resp.read(), _kept(resp.headers)
        except urllib.error.HTTPError as exc:
            status, hdrs = exc.code, _kept(exc.headers)
            try:
                raw = exc.read()
            except OSError:
                raw = b""
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raw = f"transport error: {type(exc).__name__}: {exc}".encode()
        elapsed = (time.perf_counter() - t0) * 1000.0
        return status, hdrs, raw.decode("utf-8", errors="replace"), round(elapsed, 2)


def _kept(headers: Any) -> dict[str, str]:
    out: dict[str, str] = {}
    for name in KEPT_HEADERS:
        val = headers.get(name)
        if val is not None:
            out[name] = str(val)
    return out


def parse_body(text: str) -> Any:
    """JSON value when the text is JSON, else the (truncated) text."""
    try:
        return json.loads(text)
    except ValueError:
        return text[:MAX_BODY_CHARS]


# --------------------------------------------------------------------------
# Auth from the contract
# --------------------------------------------------------------------------

_PRESENTATION_RE = re.compile(r"in the form '([A-Za-z][A-Za-z0-9_-]*) <token>'")
_SCHEME_WORD_RE = re.compile(r"\b(Bearer|Token|JWT|Basic|Ghost)\b")


def presentation_scheme(fixture: dict[str, str] | None) -> str:
    """The Authorization scheme the fixture's ``auth_mechanism`` names.

    Same rule as ``readiness.auth_proof.presentation_scheme`` (the fixed
    sentence "in the form '<Scheme> <token>'"), extended with a fallback for
    fixtures that name the scheme in prose ("Bearer token from POST /login");
    ``Bearer`` when nothing is named. Kept here so the lean path does not
    import the readiness harness.
    """
    mechanism = str((fixture or {}).get("auth_mechanism", ""))
    m = _PRESENTATION_RE.search(mechanism)
    if m:
        return m.group(1)
    w = _SCHEME_WORD_RE.search(mechanism)
    return w.group(1) if w else "Bearer"


@dataclass
class AuthSpec:
    login: Step
    scheme: str = "Bearer"

    def header(self, token: Any) -> str:
        return f"{self.scheme} {token}"


def auth_from_contract(contract: Any) -> AuthSpec | None:
    """AuthSpec for a loaded ``RepoContract`` (or a path to one); None without ``auth``.

    Only ``mode: login`` is driven here; other modes raise ``UnsupportedAuth``
    so the caller can decide, rather than silently running unauthenticated.
    """
    if isinstance(contract, (str, Path)):
        from proofdeploy.yml import load_contract

        contract = load_contract(contract)
    auth = getattr(contract, "auth", None)
    if not auth:
        return None
    mode = auth.get("mode")
    if mode != "login":
        raise UnsupportedAuth(f"auth mode {mode!r} is not supported by the lean generator")
    if auth.get("token_json_path"):
        capture = {TOKEN_VAR: str(auth["token_json_path"])}
    else:
        capture = {TOKEN_VAR: "header:" + str(auth["token_header"])}
    body = dict(auth["body"]) if auth.get("body") else None
    login = Step(
        method=str(auth.get("method") or "POST").upper(),
        path=str(auth["path"]),
        body=body,
        content_type="application/json" if body is not None else None,
        auth=False,
        capture=capture,
    )
    return AuthSpec(login=login, scheme=presentation_scheme(getattr(contract, "fixture", None)))


def capture_value(path: str, body: Any, headers: dict[str, str]) -> tuple[bool, Any]:
    """Apply a ``capture`` path: ``header:<Name>`` or a JSON path in the body."""
    if path.startswith("header:"):
        name = path[len("header:") :].strip().lower()
        for k, v in headers.items():
            if k.lower() == name:
                return True, v
        return False, None
    return json_path_get(body, path)
