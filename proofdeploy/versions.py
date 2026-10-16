"""The ONE reader-side schema-version check (plan 1.10 / WO-1.10).

The result contract (docs/contract/result-contract.md) always stated
the rule — "readers must refuse unknown versions" — but no read path
enforced it: readers accepted records by defaulting absent keys,
including records stamped with versions they did not know (the
contract's ambiguity 1). This module is the single gate every
reader uses: the JSONL record loader and the at-commit re-score read
(proofdeploy.runpair), offline replay (proofdeploy.replay), per-probe
result rebuilds (proofdeploy.executor.ProbeResult.from_dict), and the
verify output reader (proofdeploy.verify, whose outputs carry their
own verify_output_version under the same major discipline).

Rule: a version's MAJOR decides.

- Within a supported major, readers keep the long-standing behavior
  — absent keys default — so every record at 1.0.0-1.5.0 loads and
  re-scores byte-identically.
- An unknown major is refused with a typed error naming the version
  found and the supported majors: a major bump means existing fields
  may have changed meaning, which defaulting cannot absorb.
- A MISSING version also keeps today's behavior: pre-versioning
  artifacts (the legacy evaluation lines carry no schema_version at
  all) load as before.

This module imports nothing from the package, so every reader can
use it without an import cycle.
"""

from __future__ import annotations

from typing import Any

#: Schema majors this reader supports, for result-contract records
#: and for verify outputs alike.
SUPPORTED_SCHEMA_MAJORS = (1,)


class UnknownSchemaVersionError(ValueError):
    """Typed refusal: a record or output carries a schema major this
    reader does not support. The message names the version found and
    the supported majors, so the refusal is auditable, never silent."""


def check_schema_version(version: Any, *, kind: str = "record") -> None:
    """Refuse an unknown schema MAJOR; accept supported majors and a
    missing version (today's behavior for both). ``kind`` names what
    is being read, for the refusal message ("record", "evidence
    record", "per-probe result", "verify output")."""
    if version is None:
        return
    supported = False
    if isinstance(version, str):
        head, _, _ = version.partition(".")
        if head.isdigit() and int(head) in SUPPORTED_SCHEMA_MAJORS:
            supported = True
    if not supported:
        majors = ", ".join(str(m) for m in SUPPORTED_SCHEMA_MAJORS)
        raise UnknownSchemaVersionError(
            f"unsupported {kind} schema version {version!r}: this reader "
            f"supports major version(s) {majors}; a record or output "
            f"stamped with another major may have changed the meaning "
            f"of existing fields and is refused, not defaulted"
        )
