"""Tests for change-model types (PRD FR-1)."""

from proofdeploy.change_model import ChangeModel, Claim, HunkKind, SchemaFact


def test_probeable_with_behavior_claim():
    m = ChangeModel(claims=[Claim(id="c1", kind=HunkKind.BEHAVIOR_ADD, statement="x")])
    assert m.probeable


def test_not_probeable_with_docs_only():
    m = ChangeModel(
        claims=[Claim(id="c1", kind=HunkKind.DOCS_ONLY, statement="x")],
        informational=["config key changed"],
    )
    assert not m.probeable


def test_config_change_is_informational_only():
    # v1: config diffs are reported, never probed (PRD v3)
    m = ChangeModel(claims=[Claim(id="c1", kind=HunkKind.CONFIG_CHANGE, statement="x")])
    assert not m.probeable


def test_schema_fact_shape():
    f = SchemaFact(operation="add_column", table="quotes", detail={"column": "region"})
    assert f.operation == "add_column"
    assert f.table == "quotes"
