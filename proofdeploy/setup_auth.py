"""Setup capture and auth injection for the ProofDeploy runner (WAL-58, part 3 of 5).

Setup steps can capture named bindings from their HTTP responses (e.g. a
token for a user created in setup). Later steps — and the probe's act and
assertions — reference bindings as `${NAME}`.

`${AUTH_TOKEN}` is special: it is filled from the fixture's auth binding and
stays valid for the whole run.

Harness apps are allowed only on dev-set library repos. The executor never
sees them; the provisioner handles harness_app setup steps before this
module runs.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

# `${NAME}` placeholders in strings.
_BINDING_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def substitute_bindings(value: Any, bindings: dict[str, str]) -> Any:
    """Recursively replace `${NAME}` placeholders in strings.

    Unknown names are left as-is (the probe will likely fail its
    assertions, which is the correct behavior — not a crash).
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


@dataclass
class SetupContext:
    """Bindings available during a probe run.

    `auth_token` is the fixture's auth binding, valid for the whole run.
    `bindings` accumulates captures from setup steps in order.
    """

    auth_token: str | None = None
    bindings: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.auth_token is not None:
            self.bindings.setdefault("AUTH_TOKEN", self.auth_token)

    def add(self, new: dict[str, str]) -> None:
        """Add captured bindings (later captures overwrite earlier ones)."""
        self.bindings.update(new)
        # AUTH_TOKEN from the fixture always wins.
        if self.auth_token is not None:
            self.bindings["AUTH_TOKEN"] = self.auth_token

    def apply(self, probe: dict[str, Any]) -> dict[str, Any]:
        """Return a copy of the probe with all `${NAME}` placeholders filled."""
        import copy

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
            raise ValueError(
                f"setup[{i}]: harness_app is only allowed for dev-set library repos"
            )
