"""Probe schema harness (WAL-58).

Implements the strict probe schema from skill registration v2: probes test
externally observable behavior only — HTTP status, headers, bodies, and
read-only database state. The harness rejects any probe that does not match
the schema, including code probes that would run inside the target process.

Probe schema:
    {
        "setup": [...],   # optional; for library repos, may write a harness app
        "act": {
            "method": "GET" | "POST" | ...,
            "path": "/...",
            "headers": {...},   # optional
            "body": ...         # optional
        },
        "assert": [...]   # assertions on status, headers, body, DB state
    }

Harness version: 1.0.0
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

# Harness version, recorded in every run manifest. Bump on any schema change.
HARNESS_VERSION = "1.0.0"

# Record schema version, stamped on every record and every per-probe
# result (WO-5 addendum item 8). Bump when the record format changes.
SCHEMA_VERSION = "1.0.0"

# HTTP methods the schema allows. No CONNECT, TRACE, or custom verbs.
ALLOWED_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"})

# Assertion types the schema allows.
ALLOWED_ASSERT_TYPES = frozenset(
    {
        "status",  # {"type": "status", "equals": 200}
        "header",  # {"type": "header", "name": "...", "equals"|"contains"|"matches": ...}
        "body",  # {"type": "body", "equals"|"contains"|"matches"|"json_path": ...}
        "db",  # {"type": "db", "query": "SELECT ...", "equals"|"count": ...}
    }
)

# Setup step types. "harness_app" writes a minimal app mounting the library
# (library repos only); "http" performs an HTTP call to set up state.
ALLOWED_SETUP_TYPES = frozenset({"harness_app", "http"})

# Read-only SQL: SELECT and WITH ... SELECT only. Anything else is rejected.
_READONLY_SQL = re.compile(r"^\s*(SELECT\b|WITH\b)", re.IGNORECASE)


class ProbeRejected(Exception):
    """Raised when a probe does not match the strict schema."""


def _reject(reason: str) -> None:
    raise ProbeRejected(reason)


def _check_no_code_probe(value: Any, where: str) -> None:
    """Reject anything that looks like code meant to run in the target process.

    The schema allows only declarative HTTP/DB descriptions. Inline scripts,
    eval strings, and language-specific code blocks are rejected.
    """
    if isinstance(value, str):
        lowered = value.lower()
        for marker in (
            "```python",
            "```js",
            "```javascript",
            "```php",
            "```ts",
            "```typescript",
            "import subprocess",
            "import os",
            "eval(",
            "exec(",
            "__import__",
        ):
            if marker in lowered:
                _reject(f"{where}: code probe rejected (found {marker!r})")
    elif isinstance(value, dict):
        for k, v in value.items():
            _check_no_code_probe(v, f"{where}.{k}")
    elif isinstance(value, list):
        for i, v in enumerate(value):
            _check_no_code_probe(v, f"{where}[{i}]")


def _validate_act(act: Any) -> None:
    if not isinstance(act, dict):
        _reject("act must be an object")
    method = act.get("method")
    if method not in ALLOWED_METHODS:
        _reject(f"act.method must be one of {sorted(ALLOWED_METHODS)}, got {method!r}")
    path = act.get("path")
    if not isinstance(path, str) or not path.startswith("/"):
        _reject(f"act.path must be a string starting with '/', got {path!r}")
    headers = act.get("headers", {})
    if not isinstance(headers, dict):
        _reject("act.headers must be an object")
    for k, v in headers.items():
        if not isinstance(k, str) or not isinstance(v, str):
            _reject("act.headers keys and values must be strings")
    # body may be any JSON value; code-probe scan applies.
    _check_no_code_probe(act.get("body"), "act.body")
    _check_no_code_probe(headers, "act.headers")
    unknown = set(act) - {"method", "path", "headers", "body"}
    if unknown:
        _reject(f"act has unknown fields: {sorted(unknown)}")


def _validate_assertion(a: Any, i: int) -> None:
    where = f"assert[{i}]"
    if not isinstance(a, dict):
        _reject(f"{where} must be an object")
    atype = a.get("type")
    if atype not in ALLOWED_ASSERT_TYPES:
        _reject(f"{where}.type must be one of {sorted(ALLOWED_ASSERT_TYPES)}, got {atype!r}")
    if atype == "status":
        if "equals" not in a or not isinstance(a["equals"], int):
            _reject(f"{where}: status assertion needs integer 'equals'")
    elif atype == "header":
        if not isinstance(a.get("name"), str):
            _reject(f"{where}: header assertion needs string 'name'")
        if not any(k in a for k in ("equals", "contains", "matches")):
            _reject(f"{where}: header assertion needs equals|contains|matches")
    elif atype == "body":
        if not any(k in a for k in ("equals", "contains", "matches", "json_path")):
            _reject(f"{where}: body assertion needs equals|contains|matches|json_path")
    elif atype == "db":
        query = a.get("query")
        if not isinstance(query, str) or not _READONLY_SQL.match(query):
            _reject(f"{where}: db assertion needs a read-only SELECT query")
    _check_no_code_probe(a, where)


def _validate_setup_step(s: Any, i: int, allow_harness_app: bool = False) -> None:
    where = f"setup[{i}]"
    if not isinstance(s, dict):
        _reject(f"{where} must be an object")
    stype = s.get("type")
    if stype not in ALLOWED_SETUP_TYPES:
        _reject(f"{where}.type must be one of {sorted(ALLOWED_SETUP_TYPES)}, got {stype!r}")
    if stype == "harness_app":
        # Library repos only (dev-set): the author writes a minimal app mounting
        # the library. Eval repos must reject this step type.
        if not allow_harness_app:
            _reject(f"{where}: harness_app is only allowed for dev-set library repos")
        if not isinstance(s.get("language"), str):
            _reject(f"{where}: harness_app needs string 'language'")
        if not isinstance(s.get("source"), str) or not s["source"].strip():
            _reject(f"{where}: harness_app needs non-empty string 'source'")
        if not isinstance(s.get("entrypoint"), str):
            _reject(f"{where}: harness_app needs string 'entrypoint'")
        # Note: harness_app.source IS code by definition (a minimal app mounting
        # the library). The schema allowlist is fail-closed, so no substring
        # blocklist applies here — that check would reject legitimate imports.
    elif stype == "http":
        # An HTTP call to set up state (e.g. create a fixture record).
        if "act" not in s:
            _reject(f"{where}: http setup step needs 'act'")
        _validate_act(s["act"])
        if "capture" in s:
            _validate_capture(s["capture"], f"{where}.capture")
    unknown = set(s) - {"type", "language", "source", "entrypoint", "act", "note", "capture"}
    if unknown:
        _reject(f"{where} has unknown fields: {sorted(unknown)}")


def _validate_capture(capture: Any, where: str) -> None:
    """Validate a setup step's capture spec.

    Shape: {NAME: {"from": "body", "json_path": "dotted.path"}}
        or {NAME: {"from": "header", "name": "X-Token"}}
    """
    if not isinstance(capture, dict) or not capture:
        _reject(f"{where} must be a non-empty object")
    for name, spec in capture.items():
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            _reject(f"{where}: invalid capture name {name!r}")
        if not isinstance(spec, dict):
            _reject(f"{where}.{name} must be an object")
        source = spec.get("from")
        if source == "body":
            path = spec.get("json_path")
            if not isinstance(path, str) or not path:
                _reject(f"{where}.{name}: body capture needs non-empty 'json_path'")
            unknown = set(spec) - {"from", "json_path"}
        elif source == "header":
            hname = spec.get("name")
            if not isinstance(hname, str) or not hname:
                _reject(f"{where}.{name}: header capture needs non-empty 'name'")
            unknown = set(spec) - {"from", "name"}
        else:
            _reject(f"{where}.{name}: 'from' must be 'body' or 'header', got {source!r}")
            continue
        if unknown:
            _reject(f"{where}.{name} has unknown fields: {sorted(unknown)}")


def validate_probe(probe: Any, allow_harness_app: bool = False) -> dict[str, Any]:
    """Validate a single probe against the strict schema.

    Returns the probe unchanged on success. Raises ProbeRejected on any
    violation — wrong shape, unknown fields, non-HTTP/DB content, or code
    meant to run inside the target process.

    allow_harness_app defaults to False (fail-closed). Dev-set library repos
    opt in explicitly; eval repos must never set it.
    """
    if not isinstance(probe, dict):
        _reject("probe must be a JSON object")
    unknown_top = set(probe) - {"setup", "act", "assert"}
    if unknown_top:
        _reject(f"probe has unknown top-level fields: {sorted(unknown_top)}")
    if "act" not in probe:
        _reject("probe needs 'act'")
    if "assert" not in probe:
        _reject("probe needs 'assert'")

    setup = probe.get("setup", [])
    if not isinstance(setup, list):
        _reject("probe.setup must be a list")
    for i, s in enumerate(setup):
        _validate_setup_step(s, i, allow_harness_app=allow_harness_app)

    _validate_act(probe["act"])

    asserts = probe["assert"]
    if not isinstance(asserts, list) or not asserts:
        _reject("probe.assert must be a non-empty list")
    for i, a in enumerate(asserts):
        _validate_assertion(a, i)

    # Final sweep for code probes. harness_app.source is excluded: it is code
    # by definition (a minimal app), and the schema allowlist is fail-closed.
    scrubbed = json.loads(json.dumps(probe))
    for step in scrubbed.get("setup", []):
        if isinstance(step, dict) and step.get("type") == "harness_app":
            step["source"] = ""
    _check_no_code_probe(scrubbed, "probe")
    return probe  # type: ignore[no-any-return]


def parse_probes(text: str, allow_harness_app: bool = False) -> list[dict]:
    """Extract and validate probes from fenced JSON code blocks.

    The author outputs each probe as a fenced ```json block. Anything outside
    the blocks is ignored. Every block must parse as JSON and pass
    validate_probe; any failure rejects the whole set.

    allow_harness_app defaults to False (fail-closed). Dev-set library repos
    opt in explicitly.
    """
    blocks = re.findall(r"```json\s*\n(.*?)```", text, re.DOTALL)
    if not blocks:
        _reject("no fenced ```json blocks found in author output")
    probes = []
    for i, block in enumerate(blocks):
        try:
            probe = json.loads(block)
        except json.JSONDecodeError as e:
            _reject(f"block {i}: invalid JSON: {e}")
        try:
            validate_probe(probe, allow_harness_app=allow_harness_app)
        except ProbeRejected as e:
            _reject(f"block {i}: {e}")
        probes.append(probe)
    return probes


def harness_app_hash(source: str) -> str:
    """sha256 of a harness-app source string, recorded alongside probes."""
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


@dataclass
class ProbeSet:
    """A validated set of probes plus metadata for the run manifest."""

    probes: list[dict]
    harness_version: str = HARNESS_VERSION
    harness_app_hashes: list[str] = field(default_factory=list)

    @classmethod
    def from_author_output(cls, text: str, allow_harness_app: bool = False) -> ProbeSet:
        probes = parse_probes(text, allow_harness_app=allow_harness_app)
        hashes = []
        for p in probes:
            for step in p.get("setup", []):
                if step.get("type") == "harness_app":
                    hashes.append(harness_app_hash(step["source"]))
        return cls(probes=probes, harness_app_hashes=hashes)

    def manifest(self) -> dict:
        return {
            "harness_version": self.harness_version,
            "probe_count": len(self.probes),
            "harness_app_hashes": self.harness_app_hashes,
        }
