"""Secret leak checking for the blind probe author (WO-5).

The author is blind: it writes probes from the diff alone. Nothing the
author receives (the prompt, the bundle files) may carry secrets: fixture
keys, contract secrets, session tokens, minted tokens, or contract
secrets. The orchestrator runs these checks fail-closed BEFORE any model
call: a single occurrence aborts the run.

Short values (below ``MIN_SECRET_LENGTH``) are ignored: they collide with
ordinary words and would false-positive on every prompt. Real secrets
(tokens, keys, passwords) are far longer; the record's fail-closed
redaction (runpair.collect_record_secret_values) still redacts every
value regardless of length when the record is stored.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

# Values shorter than this are not checked: they collide with ordinary
# prose (e.g. "test", "admin") and would false-positive constantly.
MIN_SECRET_LENGTH = 8


class SecretLeakError(ValueError):
    """A secret value was found where only blind-author input may go."""


def _candidate_values(values: Iterable[Any]) -> list[str]:
    """Deduplicated string values long enough to check, preserving order."""
    seen: set[str] = set()
    out: list[str] = []
    for v in values:
        if not isinstance(v, str):
            continue
        if len(v) < MIN_SECRET_LENGTH:
            continue
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out


def find_secret_occurrences(
    text: str, secret_values: Iterable[Any]
) -> list[str]:
    """Return the secret values occurring in ``text`` (first-appearance order).

    Pure scan: no raising, no redaction. Short values are skipped per
    MIN_SECRET_LENGTH.
    """
    hits = [(text.index(v), v) for v in _candidate_values(secret_values) if v in text]
    hits.sort(key=lambda h: h[0])
    return [v for _, v in hits]


def assert_no_secrets(
    text: str, secret_values: Iterable[Any], *, where: str = "prompt"
) -> None:
    """Raise SecretLeakError if any secret value occurs in ``text``.

    Fail-closed: the caller must not send ``text`` to the model when this
    raises.
    """
    found = find_secret_occurrences(text, secret_values)
    if found:
        raise SecretLeakError(
            f"secret leak in {where}: {len(found)} secret value(s) present; "
            "refusing to send to the model"
        )


def collect_forbidden_values(
    fixture: dict[str, Any] | None,
    contract_env: dict[str, Any] | None,
    *,
    public_fixture_keys: Iterable[str] = (),
    public_contract_env_keys: Iterable[str] = (),
    extra_secrets: Iterable[Any] = (),
) -> list[str]:
    """Values that must never reach the blind author.

    Every fixture value whose key is not declared public, every contract
    env value whose key is not declared public, plus any caller-supplied
    extra secrets (captured values, minted tokens, seeded credentials).
    The five template fields are public by design: they are rendered into
    the fixture description the author is meant to read.
    """
    public_fx = set(public_fixture_keys)
    public_env = set(public_contract_env_keys)
    values: list[Any] = []
    for k, v in (fixture or {}).items():
        if k not in public_fx:
            values.append(v)
    for k, v in (contract_env or {}).items():
        if k not in public_env:
            values.append(v)
    values.extend(extra_secrets)
    return _candidate_values(values)
