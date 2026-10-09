"""Tests for the fixture description template and bundle assembler."""

import hashlib
import json

import pytest

from proofdeploy import fixture
from proofdeploy.yml import load_contract

TEMPLATE = """\
# a comment the author never sees
Repository: {repo_name}
Seed data: {seed_note}
Authentication: {auth_mechanism}. Your token: ${AUTH_TOKEN}.
Token scope: {auth_scope}. No other users, roles, or credentials exist beyond what the seed creates.
Feature flags: {flags_note}.
You may create users, roles, data, or change settings in setup steps before your probes run.
"""

FIELDS = {
    "repo_name": "Vendure",
    "seed_note": "the project's built-in sample data, unmodified",
    "auth_mechanism": "Bearer token in the Authorization header",
    "auth_scope": "super-administrator",
    "flags_note": "all flags at their defaults",
}

EXPECTED = (
    "Repository: Vendure\n"
    "Seed data: the project's built-in sample data, unmodified\n"
    "Authentication: Bearer token in the Authorization header. Your token: ${AUTH_TOKEN}.\n"
    "Token scope: super-administrator. "
    "No other users, roles, or credentials exist beyond what the seed creates.\n"
    "Feature flags: all flags at their defaults.\n"
    "You may create users, roles, data, or change settings in setup steps before your probes run.\n"
)


def test_render_fills_fields_and_strips_comments():
    assert fixture.render_description(TEMPLATE, FIELDS) == EXPECTED


def test_render_preserves_auth_token_placeholder():
    out = fixture.render_description(TEMPLATE, FIELDS)
    assert "${AUTH_TOKEN}" in out
    assert "\x00" not in out  # sentinel never leaks


def test_render_missing_field_fails_closed():
    fields = dict(FIELDS)
    del fields["seed_note"]
    with pytest.raises(ValueError, match="missing fields"):
        fixture.render_description(TEMPLATE, fields)


def test_render_non_string_field_fails_closed():
    fields = dict(FIELDS)
    fields["repo_name"] = 42
    with pytest.raises(ValueError, match="must be a string"):
        fixture.render_description(TEMPLATE, fields)


def test_render_unknown_placeholder_fails_closed():
    with pytest.raises(ValueError, match="unknown \\{placeholder\\}"):
        fixture.render_description("Hello {mystery} ${AUTH_TOKEN}.", FIELDS)


def test_template_file_renders_with_fixture_fields():
    text = fixture.template_path().read_text(encoding="utf-8")
    out = fixture.render_description(text, FIELDS)
    assert out.startswith("Repository: Vendure\n")
    assert "${AUTH_TOKEN}" in out


def test_template_hash_is_stable():
    assert fixture.template_hash() == hashlib.sha256(
        fixture.template_path().read_bytes()
    ).hexdigest()


def _write_yml(tmp_path):
    p = tmp_path / "proofdeploy.yml"
    p.write_text(
        "build: 'npm ci'\n"
        "start: 'node dist/index.js'\n"
        "readiness: 'http://localhost:3000/health'\n"
        "fixture:\n"
        "  repo_name: Vendure\n"
        "  seed_note: the project's built-in sample data, unmodified\n"
        "  auth_mechanism: Bearer token in the Authorization header\n"
        "  auth_scope: super-administrator\n"
        "  flags_note: all flags at their defaults\n",
        encoding="utf-8",
    )
    return p


def test_yml_loads_optional_fixture_mapping(tmp_path):
    c = load_contract(_write_yml(tmp_path))
    assert c.fixture["repo_name"] == "Vendure"
    assert len(c.fixture) == 5


def test_yml_without_fixture_still_loads(tmp_path):
    p = tmp_path / "proofdeploy.yml"
    p.write_text(
        "build: 'npm ci'\nstart: 'node dist/index.js'\n"
        "readiness: 'http://localhost:3000/health'\n",
        encoding="utf-8",
    )
    assert load_contract(p).fixture == {}


def test_yml_rejects_non_string_fixture_values(tmp_path):
    p = tmp_path / "proofdeploy.yml"
    p.write_text(
        "build: 'x'\nstart: 'y'\nreadiness: 'z'\nfixture:\n  repo_name: 42\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="'fixture' must be a mapping of strings"):
        load_contract(p)


def _snapshot(tmp_path):
    snap = tmp_path / "snap"
    (snap / "src").mkdir(parents=True)
    (snap / "src" / "app.js").write_text("console.log(1);\n", encoding="utf-8")
    return snap


def test_assemble_bundle_happy_path(tmp_path):
    yml = _write_yml(tmp_path)
    diff = tmp_path / "b.patch"
    diff.write_text("diff --git a/x b/x\n", encoding="utf-8")
    snap = _snapshot(tmp_path)
    out = tmp_path / "bundle"

    c = load_contract(yml)
    bundle = fixture.assemble_author_bundle(
        diff_path=diff,
        snapshot_dir=snap,
        yml_fixture=c.fixture,
        output_dir=out,
        expected_template_hash=fixture.template_hash(),
    )
    assert bundle.description.read_text(encoding="utf-8") == EXPECTED
    assert (bundle.snapshot / "src" / "app.js").is_file()
    assert not (bundle.snapshot / ".git").exists()
    manifest = json.loads(bundle.manifest.read_text(encoding="utf-8"))
    assert manifest["template_sha256"] == fixture.template_hash()
    assert manifest["allowlist"] == ["diff.patch", "snapshot", "fixture-description.txt"]
    assert set(manifest["files"]) == {"diff.patch", "fixture-description.txt"}


def test_assemble_rejects_template_hash_mismatch(tmp_path):
    yml = _write_yml(tmp_path)
    diff = tmp_path / "b.patch"
    diff.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match="template hash mismatch"):
        fixture.assemble_author_bundle(
            diff_path=diff,
            snapshot_dir=_snapshot(tmp_path),
            yml_fixture=load_contract(yml).fixture,
            output_dir=tmp_path / "bundle",
            expected_template_hash="0" * 64,
        )


def test_assemble_rejects_snapshot_with_git(tmp_path):
    yml = _write_yml(tmp_path)
    diff = tmp_path / "b.patch"
    diff.write_text("x", encoding="utf-8")
    snap = _snapshot(tmp_path)
    (snap / ".git").mkdir()
    with pytest.raises(ValueError, match="must not contain .git"):
        fixture.assemble_author_bundle(
            diff_path=diff,
            snapshot_dir=snap,
            yml_fixture=load_contract(yml).fixture,
            output_dir=tmp_path / "bundle",
            expected_template_hash=fixture.template_hash(),
        )


def test_assemble_rejects_missing_diff(tmp_path):
    yml = _write_yml(tmp_path)
    with pytest.raises(ValueError, match="diff not found"):
        fixture.assemble_author_bundle(
            diff_path=tmp_path / "nope.patch",
            snapshot_dir=_snapshot(tmp_path),
            yml_fixture=load_contract(yml).fixture,
            output_dir=tmp_path / "bundle",
            expected_template_hash=fixture.template_hash(),
        )
