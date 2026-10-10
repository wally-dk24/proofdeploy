"""Tests for proofdeploy.leakcheck (WO-5)."""

import json
import subprocess
from pathlib import Path

import pytest

from proofdeploy.leakcheck import (
    MIN_SECRET_LENGTH,
    AnswerKeyLeakError,
    SecretLeakError,
    assert_no_secrets,
    bundle_manifest_hash,
    check_bundle_answer_key,
    collect_forbidden_values,
    find_secret_occurrences,
)


def test_short_values_ignored():
    assert find_secret_occurrences("the admin user", ["admin"]) == []


def test_finds_secret_value():
    secret = "sk-live-abcdef1234567890"
    assert find_secret_occurrences(f"token={secret}!", [secret]) == [secret]


def test_deduplicates_and_orders():
    s1 = "token-alpha-12345678"
    s2 = "token-beta-123456789"
    text = f"{s2} then {s1} then {s2}"
    assert find_secret_occurrences(text, [s1, s2, s1]) == [s2, s1]


def test_non_string_values_skipped():
    assert find_secret_occurrences("12345", [12345, None, b"bytes"]) == []


def test_assert_no_secrets_passes_clean():
    assert_no_secrets("hello world", ["super-secret-value-123"], where="prompt")


def test_assert_no_secrets_raises():
    secret = "super-secret-value-123"
    with pytest.raises(SecretLeakError):
        assert_no_secrets(f"prompt with {secret} inside", [secret], where="prompt")


def test_min_secret_length_boundary():
    assert MIN_SECRET_LENGTH == 8
    assert find_secret_occurrences("abcdefgh", ["abcdefgh"]) == ["abcdefgh"]
    assert find_secret_occurrences("abcdefg", ["abcdefg"]) == []


def test_collect_forbidden_values_respects_public_keys():
    fixture = {
        "repo_name": "demo",
        "auth_token": "fixture-secret-token-xyz",
    }
    env = {"APP_ENV": "verification", "DB_PASSWORD": "db-secret-pw-123"}
    forbidden = collect_forbidden_values(
        fixture,
        env,
        public_fixture_keys=["repo_name"],
        public_contract_env_keys=["APP_ENV"],
        extra_secrets=["minted-jwt-value-abc"],
    )
    assert "fixture-secret-token-xyz" in forbidden
    assert "db-secret-pw-123" in forbidden
    assert "minted-jwt-value-abc" in forbidden
    assert "demo" not in forbidden
    assert "verification" not in forbidden


# ---------------------------------------------------------------------------
# Answer-key check (WO-5 brief, required).
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    p = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=True,
    )
    return p.stdout.strip()


def _answer_key_repo(root: Path) -> dict[str, str]:
    """Repo with base -> bug (B) -> fix commits for answer-key tests."""
    repo = root / "ak-repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "test")
    _git(repo, "config", "user.email", "test@test")
    (repo / "app.py").write_text("VALUE = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "app.py").write_text("VALUE = 2\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "bug")
    bug = _git(repo, "rev-parse", "HEAD")
    (repo / "app.py").write_text("VALUE = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "fix")
    fix = _git(repo, "rev-parse", "HEAD")
    return {"repo": str(repo), "base": base, "bug": bug, "fix": fix}


def _answer_key_bundle(root: Path, info: dict[str, str]) -> tuple[Path, Path]:
    """A minimal valid bundle dir + manifest for the answer-key check."""
    bundle = root / "bundle"
    snap = bundle / "snapshot"
    snap.mkdir(parents=True)
    (snap / "app.py").write_text("VALUE = 2\n")
    diff = _git(Path(info["repo"]), "diff", f"{info['base']}..{info['bug']}", "--")
    (bundle / "diff.patch").write_text(diff)
    (bundle / "fixture-description.txt").write_text("Repository: ak\n")
    (bundle / "skill-author.md").write_text("# skill\n")
    manifest = {
        "source_sha": info["bug"],
        "diff_range": f"{info['base']}..{info['bug']}",
    }
    manifest_path = root / "bundle.manifest.json"
    manifest_path.write_text(json.dumps(manifest))
    return bundle, manifest_path


def _check_kwargs(bundle: Path, manifest: Path, info: dict[str, str]) -> dict:
    return {
        "bug_sha": info["bug"],
        "bug_parent_sha": info["base"],
        "fix_sha": info["fix"],
        "repo_dir": Path(info["repo"]),
    }


def test_answer_key_clean_bundle_passes(tmp_path):
    info = _answer_key_repo(tmp_path)
    bundle, manifest = _answer_key_bundle(tmp_path, info)
    sha = check_bundle_answer_key(bundle, manifest, **_check_kwargs(bundle, manifest, info))
    assert sha == bundle_manifest_hash(manifest)
    assert len(sha) == 64


def test_answer_key_refuses_fix_sha_in_bundle(tmp_path):
    info = _answer_key_repo(tmp_path)
    bundle, manifest = _answer_key_bundle(tmp_path, info)
    # Seed the fix SHA into a snapshot file.
    (bundle / "snapshot" / "app.py").write_text(f"VALUE = 2\n# {info['fix']}\n")
    with pytest.raises(AnswerKeyLeakError, match="fix SHA"):
        check_bundle_answer_key(bundle, manifest, **_check_kwargs(bundle, manifest, info))


def test_answer_key_refuses_seeded_fix_diff(tmp_path):
    """The required test: seed the fix diff into the bundle; the run is refused."""
    info = _answer_key_repo(tmp_path)
    bundle, manifest = _answer_key_bundle(tmp_path, info)
    # Deliberately replace the B^->B diff with the B->fix diff.
    fix_diff = _git(
        Path(info["repo"]), "diff", f"{info['bug']}..{info['fix']}", "--"
    )
    (bundle / "diff.patch").write_text(fix_diff)
    with pytest.raises(AnswerKeyLeakError, match="fix diff"):
        check_bundle_answer_key(bundle, manifest, **_check_kwargs(bundle, manifest, info))


def test_answer_key_refuses_git_directory(tmp_path):
    info = _answer_key_repo(tmp_path)
    bundle, manifest = _answer_key_bundle(tmp_path, info)
    (bundle / ".git" / "objects").mkdir(parents=True)
    with pytest.raises(AnswerKeyLeakError, match=".git"):
        check_bundle_answer_key(bundle, manifest, **_check_kwargs(bundle, manifest, info))


def test_answer_key_refuses_wrong_source_sha(tmp_path):
    info = _answer_key_repo(tmp_path)
    bundle, manifest = _answer_key_bundle(tmp_path, info)
    bad = json.loads(manifest.read_text())
    bad["source_sha"] = info["fix"]
    manifest.write_text(json.dumps(bad))
    with pytest.raises(AnswerKeyLeakError, match="source_sha"):
        check_bundle_answer_key(bundle, manifest, **_check_kwargs(bundle, manifest, info))
