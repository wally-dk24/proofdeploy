"""Leak checking for the blind probe author (WO-5).

Two checks run fail-closed BEFORE any model call:

1. Answer-key check (required by the WO-5 brief): an allowlist verified
   by EQUALITY, not a denylist. The bundle must be exactly:
   - root holds exactly the allowed files;
   - ``diff.patch`` byte-equal to ``git diff B^..B``;
   - the snapshot byte-equal to ``git archive B`` (file list and bytes,
     tree hash recomputed from B, never trusted from the manifest);
   - the skill hash equals ``EXPECTED_SKILL_HASH`` (or the skill is
     absent in the no-skill arm);
   - the fixture description byte-equal to the approved template
     rendered with B's fixture;
   - the prompt byte-equal to ``build_author_prompt`` re-derived from
     the verified files (the prompt is built only from verified files).
   Backstop on the final prompt: any fix-SHA prefix from 7 characters
   up, and the fix commit's subject line, refuse the run. There is no
   filename heuristic: a clean bundle containing ``retrieval.py`` passes.
2. Secret check (extra): nothing the author receives may carry secrets
   (fixture keys, contract secrets, session tokens, minted tokens).

Short values (below ``MIN_SECRET_LENGTH``) are ignored by the secret
check: they collide with ordinary words and would false-positive on
every prompt. Real secrets (tokens, keys, passwords) are far longer;
the record's fail-closed redaction
(runpair.collect_record_secret_values) still redacts every value
regardless of length when the record is stored.
"""

from __future__ import annotations

import hashlib
import subprocess
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import yaml

# Values shorter than this are not checked: they collide with ordinary
# prose (e.g. "test", "admin") and would false-positive constantly.
MIN_SECRET_LENGTH = 8

# Shortest fix-SHA prefix that triggers the prompt backstop. A 7-char
# prefix covers every longer prefix: if fix_sha[:12] is in the prompt,
# fix_sha[:7] is in it too.
_FIX_SHA_PREFIX_LEN = 7


class SecretLeakError(ValueError):
    """A secret value was found where only blind-author input may go."""


class AnswerKeyLeakError(ValueError):
    """The author bundle fails the allowlist-by-equality check: fail the run."""


def _git_bytes(repo_dir: Path, *args: str) -> bytes:
    proc = subprocess.run(
        ["git", "-C", str(repo_dir), *args],
        capture_output=True,
        timeout=60,
    )
    if proc.returncode != 0:
        raise AnswerKeyLeakError(
            f"answer-key check: git {' '.join(args)} failed: "
            f"{proc.stderr.decode('utf-8', 'replace')[:200]}"
        )
    return proc.stdout


def _git_text(repo_dir: Path, *args: str) -> str:
    return _git_bytes(repo_dir, *args).decode("utf-8")


def bundle_manifest_hash(manifest_path: Path) -> str:
    """SHA-256 of the bundle's manifest (goes in the record)."""
    return hashlib.sha256(Path(manifest_path).read_bytes()).hexdigest()


def check_bundle_answer_key(
    bundle_dir: Path,
    manifest_path: Path,
    prompt: str,
    *,
    repo_dir: Path,
    bug_sha: str,
    bug_parent_sha: str,
    fix_sha: str | None,
    fix_subject: str | None,
    expected_skill_hash: str,
    no_skill: bool = False,
) -> str:
    """Fail-closed allowlist-by-equality check on the author bundle.

    Every element of the bundle is verified by byte equality against
    values recomputed from the repo, never trusted from the manifest:

    - the bundle root holds exactly the allowed files;
    - ``diff.patch`` is byte-equal to ``git diff B^..B``;
    - the snapshot is byte-equal to ``git archive B`` (file list from
      ``git ls-tree``, bytes from ``git show``; symlinks and ``.git``
      refused);
    - the skill hash equals ``expected_skill_hash``, or the skill is
      absent in the no-skill arm;
    - the fixture description is byte-equal to the approved template
      rendered with B's fixture;
    - the prompt is byte-equal to ``build_author_prompt`` re-derived
      from the verified files.

    Backstop on the final prompt: any prefix of the fix SHA from 7
    characters up, or the fix commit's subject line, refuses the run.

    Returns the bundle manifest hash for the record.
    """
    from proofdeploy.model_client import build_author_prompt

    bundle_dir = Path(bundle_dir)
    manifest_path = Path(manifest_path)
    repo_dir = Path(repo_dir)

    # 1. The bundle root holds exactly the allowed files.
    allowed = {"diff.patch", "snapshot", "fixture-description.txt"}
    if not no_skill:
        allowed.add("skill-author.md")
    if not bundle_dir.is_dir():
        raise AnswerKeyLeakError("answer-key check: bundle dir missing")
    actual_root = {p.name for p in bundle_dir.iterdir()}
    if actual_root != allowed:
        raise AnswerKeyLeakError(
            "answer-key check: bundle root is not the allowlist: "
            f"extra={sorted(actual_root - allowed)} "
            f"missing={sorted(allowed - actual_root)}"
        )
    snapshot_dir = bundle_dir / "snapshot"
    if not snapshot_dir.is_dir():
        raise AnswerKeyLeakError("answer-key check: bundle snapshot is not a dir")
    for name in ("diff.patch", "fixture-description.txt"):
        if not (bundle_dir / name).is_file():
            raise AnswerKeyLeakError(f"answer-key check: {name} missing")
    # No .git anywhere in the bundle (root or snapshot).
    for p in bundle_dir.rglob(".git"):
        raise AnswerKeyLeakError(
            f"answer-key check: bundle contains a .git directory at {p}"
        )
    # No symlinks in the snapshot (they can point outside).
    for p in snapshot_dir.rglob("*"):
        if p.is_symlink():
            raise AnswerKeyLeakError(
                f"answer-key check: snapshot contains a symlink: {p.name}"
            )

    # 2. diff.patch is byte-equal to git diff B^..B.
    expected_diff = _git_text(repo_dir, "diff", f"{bug_parent_sha}..{bug_sha}", "--")
    actual_diff = (bundle_dir / "diff.patch").read_text(encoding="utf-8")
    # The assembler writes the stripped diff; compare stripped bytes.
    if actual_diff.strip() != expected_diff.strip():
        raise AnswerKeyLeakError(
            "answer-key check: diff.patch is not byte-equal to "
            f"git diff {bug_parent_sha[:7]}..{bug_sha[:7]}"
        )

    # 3. The snapshot is byte-equal to git archive B. The tree hash is
    #    recomputed from B (never trusted from the manifest); equality
    #    is established file-by-file: names from git ls-tree, bytes from
    #    git show.
    tree_hash = _git_text(repo_dir, "rev-parse", f"{bug_sha}^{{tree}}").strip()
    if not tree_hash:
        raise AnswerKeyLeakError("answer-key check: could not resolve B^{tree}")
    expected_files = set(
        _git_bytes(repo_dir, "ls-tree", "-r", "--name-only", "-z", bug_sha)
        .decode("utf-8")
        .split("\x00")
    )
    expected_files.discard("")
    actual_files = {
        p.relative_to(snapshot_dir).as_posix()
        for p in snapshot_dir.rglob("*")
        if p.is_file()
    }
    if actual_files != expected_files:
        raise AnswerKeyLeakError(
            "answer-key check: snapshot file list != git archive B: "
            f"extra={sorted(actual_files - expected_files)[:5]} "
            f"missing={sorted(expected_files - actual_files)[:5]}"
        )
    for rel in sorted(expected_files):
        expected_bytes = _git_bytes(repo_dir, "show", f"{bug_sha}:{rel}")
        actual_bytes = (snapshot_dir / rel).read_bytes()
        if actual_bytes != expected_bytes:
            raise AnswerKeyLeakError(
                f"answer-key check: snapshot file {rel} != B version "
                f"(tree {tree_hash[:12]})"
            )

    # 4. The skill hash equals EXPECTED_SKILL_HASH, or the skill is
    #    absent in the no-skill arm.
    skill_path = bundle_dir / "skill-author.md"
    if no_skill:
        if skill_path.exists():
            raise AnswerKeyLeakError(
                "answer-key check: skill present in no-skill arm"
            )
        skill_text = ""
    else:
        if not skill_path.is_file():
            raise AnswerKeyLeakError("answer-key check: skill-author.md missing")
        actual_skill_hash = hashlib.sha256(skill_path.read_bytes()).hexdigest()
        if actual_skill_hash != expected_skill_hash:
            raise AnswerKeyLeakError(
                "answer-key check: skill hash != EXPECTED_SKILL_HASH"
            )
        skill_text = skill_path.read_text(encoding="utf-8")

    # 5. The fixture description matches its approved hash: re-render
    #    from the approved frozen template and B's fixture, byte-compare.
    from proofdeploy.fixture import render_description, template_hash, template_path

    template_text = template_path().read_text(encoding="utf-8")
    if hashlib.sha256(template_text.encode("utf-8")).hexdigest() != template_hash():
        raise AnswerKeyLeakError(
            "answer-key check: fixture template is not the approved frozen template"
        )
    try:
        yml_text = _git_text(repo_dir, "show", f"{bug_sha}:proofdeploy.yml")
        fixture_fields = yaml.safe_load(yml_text).get("fixture") or {}
        expected_desc = render_description(template_text, fixture_fields)
    except Exception as e:
        raise AnswerKeyLeakError(
            f"answer-key check: cannot re-render fixture description from B: {e}"
        ) from e
    actual_desc = (bundle_dir / "fixture-description.txt").read_text(encoding="utf-8")
    if actual_desc != expected_desc:
        raise AnswerKeyLeakError(
            "answer-key check: fixture-description.txt != approved template "
            "rendered with B's fixture"
        )

    # 6. The prompt is built only from verified files: re-derive it from
    #    the verified bundle files and require byte equality.
    verified_prompt, _ = build_author_prompt(
        skill_text=skill_text,
        fixture_description=actual_desc,
        diff_text=actual_diff,
        snapshot_dir=snapshot_dir,
    )
    if prompt != verified_prompt:
        raise AnswerKeyLeakError(
            "answer-key check: prompt was not built from the verified bundle files"
        )

    # 7. Backstop on the final prompt: any fix-SHA prefix from 7
    #    characters up, or the fix commit's subject line, refuses.
    if fix_sha:
        for length in range(_FIX_SHA_PREFIX_LEN, len(fix_sha) + 1):
            if fix_sha[:length] in prompt:
                raise AnswerKeyLeakError(
                    f"answer-key check backstop: fix SHA prefix "
                    f"({length} chars) in prompt"
                )
    # The subject backstop needs a distinctive subject: a 3-letter word
    # like "fix" occurs in the framing text and is not a leak.
    if (
        fix_subject
        and len(fix_subject.strip()) >= 8
        and fix_subject.strip() in prompt
    ):
        raise AnswerKeyLeakError(
            "answer-key check backstop: fix commit subject in prompt"
        )

    # The manifest sits next to the bundle (never inside it); its hash
    # goes in the record. The manifest's own claims are not trusted.
    if not manifest_path.is_file():
        raise AnswerKeyLeakError("answer-key check: bundle has no manifest")
    return bundle_manifest_hash(manifest_path)


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
