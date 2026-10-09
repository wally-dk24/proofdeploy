"""Tests for the fixture description template and bundle assembler."""

import hashlib
import json
import os
import subprocess

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
    assert (
        fixture.template_hash() == hashlib.sha256(fixture.template_path().read_bytes()).hexdigest()
    )


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
        "build: 'npm ci'\nstart: 'node dist/index.js'\nreadiness: 'http://localhost:3000/health'\n",
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
    expected_tree_hash = fixture._tree_hash(snap)
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
    # Finding 5: the manifest sits next to the bundle, never inside it.
    assert bundle.manifest.parent == out.parent
    assert bundle.manifest.name == "bundle.manifest.json"
    assert not (out / "manifest.json").exists()
    assert sorted(p.name for p in out.iterdir()) == [
        "diff.patch",
        "fixture-description.txt",
        "snapshot",
    ]
    manifest = json.loads(bundle.manifest.read_text(encoding="utf-8"))
    assert manifest["template_sha256"] == fixture.template_hash()
    assert manifest["allowlist"] == ["diff.patch", "snapshot", "fixture-description.txt"]
    assert set(manifest["files"]) == {"diff.patch", "fixture-description.txt"}
    # Finding 4: the snapshot tree itself is hashed, deterministically.
    assert manifest["snapshot_tree_sha256"] == fixture._tree_hash(bundle.snapshot)
    assert manifest["snapshot_tree_sha256"] == expected_tree_hash


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


def _assemble(tmp_path, snap=None, out_name="bundle", **kw):
    yml = _write_yml(tmp_path)
    diff = tmp_path / "b.patch"
    diff.write_text("diff --git a/x b/x\n", encoding="utf-8")
    args = {
        "diff_path": diff,
        "snapshot_dir": snap or _snapshot(tmp_path),
        "yml_fixture": load_contract(yml).fixture,
        "output_dir": tmp_path / out_name,
        "expected_template_hash": fixture.template_hash(),
    }
    args.update(kw)
    return fixture.assemble_author_bundle(**args)


def test_assemble_rejects_nonempty_output_dir(tmp_path):
    """Finding 1: a stray file in the output dir must not ride into the bundle."""
    out = tmp_path / "bundle"
    out.mkdir()
    (out / "probe-sketch.md").write_text("the answer\n", encoding="utf-8")
    with pytest.raises(ValueError, match="not empty"):
        _assemble(tmp_path)


def test_assemble_rejects_symlink_in_snapshot(tmp_path):
    """Finding 2: symlinks are refused outright (copytree would follow them)."""
    snap = _snapshot(tmp_path)
    (tmp_path / "secret").mkdir()
    (tmp_path / "secret" / "fix.patch").write_text("ANSWER\n", encoding="utf-8")
    (snap / "link").symlink_to(tmp_path / "secret", target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        _assemble(tmp_path, snap=snap)


def test_assemble_rejects_nested_git(tmp_path):
    """Finding 3: .git is refused at any depth, not just the top level."""
    snap = _snapshot(tmp_path)
    nested = snap / "sub" / "vendored"
    nested.mkdir(parents=True)
    (nested / ".git").mkdir()
    with pytest.raises(ValueError, match="must not contain .git"):
        _assemble(tmp_path, snap=snap)


def test_assemble_rejects_git_file_at_depth(tmp_path):
    """A `.git` *file* (submodule pointer) is also refused."""
    snap = _snapshot(tmp_path)
    (snap / "submod").mkdir()
    (snap / "submod" / ".git").write_text("gitdir: ../.git/modules/x\n", encoding="utf-8")
    with pytest.raises(ValueError, match="must not contain .git"):
        _assemble(tmp_path, snap=snap)


def test_manifest_records_source_sha_and_range_outside_bundle(tmp_path):
    """Finding 5: source SHAs live in the manifest, next to the bundle."""
    bundle = _assemble(tmp_path, source_sha="abc123", diff_range="abc123^..abc123")
    manifest = json.loads(bundle.manifest.read_text(encoding="utf-8"))
    assert manifest["source_sha"] == "abc123"
    assert manifest["diff_range"] == "abc123^..abc123"
    # ...and none of it reaches the bundle the author sees.
    assert "abc123" not in (bundle.root / "diff.patch").read_text(encoding="utf-8")
    assert "abc123" not in bundle.description.read_text(encoding="utf-8")
    assert sorted(p.name for p in bundle.root.iterdir()) == [
        "diff.patch",
        "fixture-description.txt",
        "snapshot",
    ]


def test_tree_hash_is_deterministic(tmp_path):
    snap = _snapshot(tmp_path)
    assert fixture._tree_hash(snap) == fixture._tree_hash(snap)
    (snap / "src" / "extra.js").write_text("x\n", encoding="utf-8")
    assert fixture._tree_hash(snap) != fixture._tree_hash(_snapshot(tmp_path.parent / "other"))


# --- Finding 6: snapshots are created by script via `git archive` ---


def _git_repo(tmp_path):
    """A tiny real git repo with one commit; returns (repo_dir, full_sha)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "src").mkdir()
    (repo / "src" / "app.js").write_text("console.log(1);\n", encoding="utf-8")
    (repo / "README.md").write_text("# hi\n", encoding="utf-8")
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "one"], cwd=repo, check=True, env=env)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()
    return repo, sha


def test_create_snapshot_happy_path(tmp_path):
    """Finding 6: `git archive <B>` into an empty dir; manifest gets the SHA."""
    repo, sha = _git_repo(tmp_path)
    snap = fixture.create_snapshot(repo_dir=repo, commit_rev=sha, output_dir=tmp_path / "snap")
    assert snap.sha == sha
    assert (snap.dir / "src" / "app.js").read_text(encoding="utf-8") == "console.log(1);\n"
    assert (snap.dir / "README.md").is_file()
    # git archive carries no history by construction.
    assert not (snap.dir / ".git").exists()
    assert list(snap.dir.rglob(".git")) == []


def test_create_snapshot_resolves_short_rev_to_full_sha(tmp_path):
    repo, sha = _git_repo(tmp_path)
    snap = fixture.create_snapshot(repo_dir=repo, commit_rev=sha[:7], output_dir=tmp_path / "snap")
    assert snap.sha == sha


def test_create_snapshot_rejects_unknown_rev(tmp_path):
    repo, _ = _git_repo(tmp_path)
    with pytest.raises(ValueError, match="cannot resolve commit"):
        fixture.create_snapshot(repo_dir=repo, commit_rev="deadbee", output_dir=tmp_path / "snap")


def test_create_snapshot_rejects_nonempty_output_dir(tmp_path):
    repo, sha = _git_repo(tmp_path)
    out = tmp_path / "snap"
    out.mkdir()
    (out / "stray").write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match="not empty"):
        fixture.create_snapshot(repo_dir=repo, commit_rev=sha, output_dir=out)


def test_create_snapshot_feeds_bundle_with_recorded_sha(tmp_path):
    """End to end: scripted snapshot -> bundle, manifest records source_sha."""
    repo, sha = _git_repo(tmp_path)
    snap = fixture.create_snapshot(repo_dir=repo, commit_rev=sha, output_dir=tmp_path / "snap")
    yml = _write_yml(tmp_path)
    diff = tmp_path / "b.patch"
    diff.write_text("diff --git a/x b/x\n", encoding="utf-8")
    bundle = fixture.assemble_author_bundle(
        diff_path=diff,
        snapshot_dir=snap.dir,
        yml_fixture=load_contract(yml).fixture,
        output_dir=tmp_path / "bundle",
        expected_template_hash=fixture.template_hash(),
        source_sha=snap.sha,
        diff_range=f"{sha}^..{sha}",
    )
    manifest = json.loads(bundle.manifest.read_text(encoding="utf-8"))
    assert manifest["source_sha"] == sha
    assert manifest["diff_range"] == f"{sha}^..{sha}"
