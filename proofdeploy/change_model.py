"""Change-model types (PRD FR-1). The structured output of the diff reader."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class HunkKind(str, Enum):
    BEHAVIOR_ADD = "behavior-add"
    BEHAVIOR_CHANGE = "behavior-change"
    SCHEMA_CHANGE = "schema-change"
    CONFIG_CHANGE = "config-change"  # informational only in v1, never probed
    TEST_ONLY = "test-only"
    DOCS_ONLY = "docs-only"


@dataclass
class Claim:
    """A behavioral claim extracted from the diff: what changed, or what must not have."""
    id: str
    kind: HunkKind
    statement: str
    must_not_change: bool = False
    files: list[str] = field(default_factory=list)


@dataclass
class SchemaFact:
    """A schema-change fact from a migration diff (framework adapters map to these)."""
    operation: str  # add_column | alter_column | drop_column | create_index | drop_index | seed_data | ...
    table: str
    detail: dict = field(default_factory=dict)


@dataclass
class ChangeModel:
    claims: list[Claim] = field(default_factory=list)
    schema_facts: list[SchemaFact] = field(default_factory=list)
    informational: list[str] = field(default_factory=list)  # config changes, v1: reported, not probed

    @property
    def probeable(self) -> bool:
        return any(
            c.kind in (HunkKind.BEHAVIOR_ADD, HunkKind.BEHAVIOR_CHANGE, HunkKind.SCHEMA_CHANGE)
            for c in self.claims
        )
