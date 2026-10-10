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
1. A token from a successful credential refresh during this run.
2. The fixture's AUTH_TOKEN.
3. Setup-step captures. A setup step that captures AUTH_TOKEN is ignored
   when the fixture provides one — the fixture always wins over captures.

Harness apps are allowed only on dev-set library repos. The executor never
sees them; the provisioner handles harness_app setup steps before this
module runs.
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field
from typing import Any

# `${NAME}` placeholders in strings.
_BINDING_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class PlaceholderError(ValueError):
    """Base class for `${NAME}` substitution failures (fail-closed)."""


class UnknownPlaceholder(PlaceholderError):
    """A `${NAME}` that names no known binding (not env, fixture, or capture)."""


class MissingCapture(PlaceholderError):
    """A `${NAME}` naming a declared capture that was never set."""


def substitute_bindings(value: Any, bindings: dict[str, str]) -> Any:
    """Recursively replace `${NAME}` placeholders in strings (lenient).

    Unknown names are left as-is. This is the legacy behavior kept for
    backward compatibility; new code paths use `substitute_strict`, which
    fails closed instead.
    """
    if isinstance(value, str):

        def _repl(m: re.Match[str]) -> str:
            return bindings.get(m.group(1), m.group(0))

        return _BINDING_RE.sub(_repl, value)
    if isinstance(value, dict):
        return {k: substitute_bindings(v, bindings) for k, v in value.items()}
    if isinstance(value, list):
        return [substitute_bindings(v, bindings) for v in value]
    return value


def substitute_strict(value: Any, bindings: dict[str, str], known_names: set[str]) -> Any:
    """Recursively replace `${NAME}` placeholders, failing closed.

    - A name in `bindings` is substituted.
    - A name in `known_names` but not `bindings` raises MissingCapture
      (a declared capture that was never set).
    - Any other name raises UnknownPlaceholder (never sent literally).
    """
    if isinstance(value, str):

        def _repl(m: re.Match[str]) -> str:
            name = m.group(1)
            if name in bindings:
                return bindings[name]
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
) -> dict[str, str]:
    """Extract named bindings from an HTTP response.

    `capture` maps binding name -> {"from": "body"|"header", ...}:
    - body: {"from": "body", "json_path": "dotted.path"}
    - header: {"from": "header", "name": "x-token"}

    Returns the captured bindings (name -> string value). Missing values
    are skipped, not errors.
    """
    out: dict[str, str] = {}
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
                out[name] = str(cur)
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
    bindings: dict[str, str] = field(default_factory=dict)
    known_names: set[str] = field(default_factory=set)
    # Set by a successful credential refresh; beats the fixture token.
    _refreshed_token: str | None = field(default=None, repr=False)
    # True once the credential-refresh flow has run for this probe run
    # (the refresh is attempted at most once per run).
    refresh_attempted: bool = False

    def __post_init__(self) -> None:
        if self.effective_token() is not None:
            self.bindings.setdefault("AUTH_TOKEN", self.effective_token())  # type: ignore[arg-type]
            self.known_names.add("AUTH_TOKEN")

    def effective_token(self) -> str | None:
        """The live auth token: refreshed token beats the fixture token."""
        return self._refreshed_token if self._refreshed_token is not None else self.auth_token

    def declare(self, names: set[str]) -> None:
        """Register names that `${NAME}` may reference (e.g. declared captures)."""
        self.known_names.update(names)

    def declare_from_setup(self, setup_steps: list[dict[str, Any]]) -> None:
        """Register every capture name declared by the setup steps."""
        self.declare(collect_capture_names(setup_steps))

    def add(self, new: dict[str, str]) -> None:
        """Add captured bindings (later captures overwrite earlier ones).

        AUTH_TOKEN is special: the fixture's token (or a refreshed one)
        always wins over a setup-step capture.
        """
        self.bindings.update(new)
        token = self.effective_token()
        if token is not None:
            self.bindings["AUTH_TOKEN"] = token

    def refresh_token(self, new_token: str) -> None:
        """Install a token from a successful credential refresh.

        This is the one sanctioned override of the fixture token: the old
        token was rejected (401), so the refreshed one must take effect.
        """
        self._refreshed_token = new_token
        self.bindings["AUTH_TOKEN"] = new_token
        self.known_names.add("AUTH_TOKEN")

    def apply_strict(self, value: Any) -> Any:
        """Substitute `${NAME}` placeholders, failing closed on bad names."""
        return substitute_strict(copy.deepcopy(value), self.bindings, self.known_names)

    def apply(self, probe: dict[str, Any]) -> dict[str, Any]:
        """Return a copy of the probe with placeholders filled (lenient).

        Legacy behavior; new code paths use `apply_strict`.
        """
        result: dict[str, Any] = substitute_bindings(copy.deepcopy(probe), self.bindings)
        return result


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
