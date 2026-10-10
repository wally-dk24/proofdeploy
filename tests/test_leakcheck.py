"""Tests for proofdeploy.leakcheck (WO-5)."""

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



# ---------------------------------------------------------------------------
# Allowlist-by-equality answer-key check (orchestrator review, 2026-10-10).
# ---------------------------------------------------------------------------


_YML = """\
build: "true"
start: "true"
readiness: "/health"
fixture:
  repo_name: "ak"
  seed_note: "seed"
  auth_mechanism: "none"
  auth_scope: "none"
  flags_note: "none"
env: {}
"""


def _eq_repo(root: Path) -> dict[str, str]:
    """Repo with base (B^) -> bug (B) -> fix, plus proofdeploy.yml at B."""
    from proofdeploy.fixture import EXPECTED_SKILL_HASH  # noqa: F401

    repo = root / "eq-repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "test")
    _git(repo, "config", "user.email", "test@test")
    (repo / "app.py").write_text("VALUE = 1\n")
    (repo / "proofdeploy.yml").write_text(_YML)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "app.py").write_text("VALUE = 2\n")
    (repo / "retrieval.py").write_text("# clean helper, name contains eval\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "bug")
    bug = _git(repo, "rev-parse", "HEAD")
    (repo / "app.py").write_text("VALUE = 1\n")
    (repo / "test_fix.py").write_text("def test_fix():\n    assert True\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "Fix app value handling")
    fix = _git(repo, "rev-parse", "HEAD")
    return {"repo": str(repo), "base": base, "bug": bug, "fix": fix}


def _eq_bundle(root: Path, info: dict[str, str]) -> dict:
    """Build a real bundle (B^->B diff + B snapshot) and its prompt."""
    from proofdeploy.fixture import (
        assemble_author_bundle,
        template_hash,
    )
    from proofdeploy.model_client import build_author_prompt

    repo = Path(info["repo"])
    work = root / "eq-work"
    work.mkdir()
    # B^->B diff.
    diff_text = _git(repo, "diff", f"{info['base']}..{info['bug']}", "--")
    diff_path = work / "diff.patch"
    diff_path.write_text(diff_text)
    # B snapshot via git archive.
    snap_src = work / "snap-src"
    snap_src.mkdir()
    proc = subprocess.run(
        ["git", "-C", str(repo), "archive", info["bug"]],
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["tar", "-x", "-C", str(snap_src)],
        input=proc.stdout,
        capture_output=True,
        check=True,
    )
    # Skill file with a known hash (passed as expected).
    skill_path = work / "skill.md"
    skill_path.write_text("# test skill\n")
    import hashlib

    skill_hash = hashlib.sha256(skill_path.read_bytes()).hexdigest()
    bundle_dir = work / "bundle"
    assemble_author_bundle(
        diff_path=diff_path,
        snapshot_dir=snap_src,
        yml_fixture={
            "repo_name": "ak",
            "seed_note": "seed",
            "auth_mechanism": "none",
            "auth_scope": "none",
            "flags_note": "none",
        },
        output_dir=bundle_dir,
        expected_template_hash=template_hash(),
        source_sha=info["bug"],
        diff_range=f"{info['base']}..{info['bug']}",
        skill_path=skill_path,
        no_skill=True,
    )
    run_context = "Run kind: bug. Unit id: eq."
    prompt = build_author_prompt(
        skill_text="",
        fixture_description=(bundle_dir / "fixture-description.txt").read_text(),
        diff_text=diff_text,
        snapshot_dir=bundle_dir / "snapshot",
        run_context=run_context,
    )
    return {
        "bundle": bundle_dir,
        "manifest": Path(str(bundle_dir) + ".manifest.json"),
        "prompt": prompt,
        "run_context": run_context,
        "skill_hash": skill_hash,
        "info": info,
        "no_skill": True,
    }


def _eq_check_kwargs(ctx: dict) -> dict:
    info = ctx["info"]
    repo = Path(info["repo"])
    fix_subject = _git(repo, "log", "-1", "--format=%s", info["fix"])
    return {
        "repo_dir": repo,
        "bug_sha": info["bug"],
        "bug_parent_sha": info["base"],
        "fix_sha": info["fix"],
        "fix_subject": fix_subject,
        "expected_skill_hash": ctx["skill_hash"],
        "no_skill": ctx.get("no_skill", False),
        "run_context": ctx["run_context"],
    }


def test_allowlist_clean_bundle_with_retrieval_passes(tmp_path):
    """A clean bundle containing retrieval.py must pass (no filename heuristic)."""
    info = _eq_repo(tmp_path)
    ctx = _eq_bundle(tmp_path, info)
    # retrieval.py is in the B snapshot; the bundle is valid.
    assert (ctx["bundle"] / "snapshot" / "retrieval.py").is_file()
    sha = check_bundle_answer_key(
        ctx["bundle"], ctx["manifest"], ctx["prompt"], **_eq_check_kwargs(ctx)
    )
    assert sha == bundle_manifest_hash(ctx["manifest"])


def test_allowlist_refuses_fix_diff_appended(tmp_path):
    info = _eq_repo(tmp_path)
    ctx = _eq_bundle(tmp_path, info)
    repo = Path(info["repo"])
    fix_diff = _git(repo, "diff", f"{info['bug']}..{info['fix']}", "--")
    with open(ctx["bundle"] / "diff.patch", "a") as f:
        f.write("\n" + fix_diff)
    with pytest.raises(AnswerKeyLeakError, match="byte-equal"):
        check_bundle_answer_key(
            ctx["bundle"], ctx["manifest"], ctx["prompt"], **_eq_check_kwargs(ctx)
        )


def test_allowlist_refuses_fix_hunk_in_extra_file(tmp_path):
    info = _eq_repo(tmp_path)
    ctx = _eq_bundle(tmp_path, info)
    repo = Path(info["repo"])
    fix_diff = _git(repo, "diff", f"{info['bug']}..{info['fix']}", "--")
    # One fix hunk smuggled as an extra snapshot file.
    (ctx["bundle"] / "snapshot" / "extra.py").write_text(fix_diff)
    with pytest.raises(AnswerKeyLeakError, match="file list"):
        check_bundle_answer_key(
            ctx["bundle"], ctx["manifest"], ctx["prompt"], **_eq_check_kwargs(ctx)
        )


def test_allowlist_refuses_snapshot_file_replaced_by_fix(tmp_path):
    info = _eq_repo(tmp_path)
    ctx = _eq_bundle(tmp_path, info)
    # Replace app.py with its fix version.
    (ctx["bundle"] / "snapshot" / "app.py").write_text("VALUE = 1\n")
    with pytest.raises(AnswerKeyLeakError, match="!= B version"):
        check_bundle_answer_key(
            ctx["bundle"], ctx["manifest"], ctx["prompt"], **_eq_check_kwargs(ctx)
        )


def test_allowlist_refuses_fix_test_file_in_snapshot(tmp_path):
    info = _eq_repo(tmp_path)
    ctx = _eq_bundle(tmp_path, info)
    # The fix's test file smuggled into the snapshot.
    (ctx["bundle"] / "snapshot" / "test_fix.py").write_text(
        "def test_fix():\n    assert True\n"
    )
    with pytest.raises(AnswerKeyLeakError, match="file list"):
        check_bundle_answer_key(
            ctx["bundle"], ctx["manifest"], ctx["prompt"], **_eq_check_kwargs(ctx)
        )


def test_allowlist_backstop_refuses_fix_sha_prefix_in_prompt(tmp_path):
    from proofdeploy.model_client import build_author_prompt

    info = _eq_repo(tmp_path)
    ctx = _eq_bundle(tmp_path, info)
    # A 12-char fix SHA prefix smuggled via run_context (prompt-only channel).
    bad_context = ctx["run_context"] + f" ref {info['fix'][:12]}"
    bad_prompt = build_author_prompt(
        skill_text="",
        fixture_description=(ctx["bundle"] / "fixture-description.txt").read_text(),
        diff_text=(ctx["bundle"] / "diff.patch").read_text(),
        snapshot_dir=ctx["bundle"] / "snapshot",
        run_context=bad_context,
    )
    kwargs = _eq_check_kwargs(ctx)
    kwargs["run_context"] = bad_context
    with pytest.raises(AnswerKeyLeakError, match="backstop.*fix SHA prefix"):
        check_bundle_answer_key(
            ctx["bundle"], ctx["manifest"], bad_prompt, **kwargs
        )


def test_allowlist_backstop_refuses_fix_subject_in_prompt(tmp_path):
    from proofdeploy.model_client import build_author_prompt

    info = _eq_repo(tmp_path)
    ctx = _eq_bundle(tmp_path, info)
    repo = Path(info["repo"])
    subject = _git(repo, "log", "-1", "--format=%s", info["fix"])
    bad_context = ctx["run_context"] + f" note: {subject}"
    bad_prompt = build_author_prompt(
        skill_text="",
        fixture_description=(ctx["bundle"] / "fixture-description.txt").read_text(),
        diff_text=(ctx["bundle"] / "diff.patch").read_text(),
        snapshot_dir=ctx["bundle"] / "snapshot",
        run_context=bad_context,
    )
    kwargs = _eq_check_kwargs(ctx)
    kwargs["run_context"] = bad_context
    with pytest.raises(AnswerKeyLeakError, match="backstop.*subject"):
        check_bundle_answer_key(
            ctx["bundle"], ctx["manifest"], bad_prompt, **kwargs
        )


def test_allowlist_refuses_extra_file_at_bundle_root(tmp_path):
    info = _eq_repo(tmp_path)
    ctx = _eq_bundle(tmp_path, info)
    (ctx["bundle"] / "evil.txt").write_text("smuggled")
    with pytest.raises(AnswerKeyLeakError, match="not the allowlist"):
        check_bundle_answer_key(
            ctx["bundle"], ctx["manifest"], ctx["prompt"], **_eq_check_kwargs(ctx)
        )
