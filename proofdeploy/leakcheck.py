"""Leak checking for the blind probe author (WO-5).

Two checks run fail-closed BEFORE any model call:

1. Answer-key check (required by the WO-5 brief): the author bundle must
   contain only the allowlist: the B^->B diff, the B snapshot, the
   fixture description, the skill, and the manifest. It must NOT contain
   the fix SHA, any later commit's content, fix tests, eval files, or a
   .git directory. A single violation aborts the run.
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
import json
import subprocess
from collections.abc import Iterable
from pathlib import Path
from typing import Any

# Values shorter than this are not checked: they collide with ordinary
# prose (e.g. "test", "admin") and would false-positive constantly.
MIN_SECRET_LENGTH = 8


class SecretLeakError(ValueError):
    """A secret value was found where only blind-author input may go."""


class AnswerKeyLeakError(ValueError):
    """The author bundle contains answer-key material: fail the run."""


def _bundle_files(bundle_dir: Path) -> list[Path]:
    """All regular files under the bundle directory."""
    return [p for p in bundle_dir.rglob("*") if p.is_file()]


def _read_text(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="strict")
    except (UnicodeDecodeError, OSError):
        return ""


def bundle_manifest_hash(manifest_path: Path) -> str:
    """SHA-256 of the bundle's manifest (goes in the record)."""
    return hashlib.sha256(Path(manifest_path).read_bytes()).hexdigest()


def check_bundle_answer_key(
    bundle_dir: Path,
    manifest_path: Path,
    *,
    bug_sha: str,
    bug_parent_sha: str,
    fix_sha: str | None,
    repo_dir: Path,
) -> str:
    """Fail-closed answer-key check on the assembled author bundle.

    The bundle may contain only the allowlist: the B^->B diff, the B
    snapshot, the fixture description, the skill, and the manifest. Fail
    the run (raise AnswerKeyLeakError) if the bundle contains:
    - the fix SHA;
    - any later commit's content (the B->fix diff);
    - fix tests or eval files smuggled in as bundle files;
    - a .git directory.

    Also verifies the bundle's provenance: the manifest's source_sha
    must be B and its diff_range must be B^..B.

    Returns the bundle manifest hash for the record.
    """
    bundle_dir = Path(bundle_dir)
    manifest_path = Path(manifest_path)

    # 1. No .git directory anywhere in the bundle.
    for p in bundle_dir.rglob(".git"):
        raise AnswerKeyLeakError(
            f"answer-key leak: bundle contains a .git directory at {p}"
        )

    # 2. Provenance: the manifest must describe B^->B, not the fix.
    if not manifest_path.is_file():
        raise AnswerKeyLeakError("answer-key leak: bundle has no manifest")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        raise AnswerKeyLeakError(f"answer-key leak: unreadable manifest: {e}") from e
    if manifest.get("source_sha") != bug_sha:
        raise AnswerKeyLeakError(
            "answer-key leak: manifest source_sha is not B "
            f"({manifest.get('source_sha')!r} != {bug_sha[:12]}...)"
        )
    expected_range = f"{bug_parent_sha}..{bug_sha}"
    if manifest.get("diff_range") != expected_range:
        raise AnswerKeyLeakError(
            "answer-key leak: manifest diff_range is not B^..B "
            f"({manifest.get('diff_range')!r})"
        )

    files = _bundle_files(bundle_dir)

    # 3. The fix SHA must not appear in any bundle file.
    if fix_sha:
        for p in files:
            if fix_sha in _read_text(p):
                raise AnswerKeyLeakError(
                    f"answer-key leak: fix SHA appears in bundle file {p.name}"
                )
        # 4. Later commit's content: the B->fix diff must not be in the bundle.
        proc = subprocess.run(
            ["git", "-C", str(repo_dir), "diff", f"{bug_sha}..{fix_sha}"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            fix_diff = proc.stdout
            for p in files:
                content = _read_text(p)
                if fix_diff in content:
                    raise AnswerKeyLeakError(
                        f"answer-key leak: B->fix diff appears in bundle file {p.name}"
                    )
            # The bundle's own diff.patch must be the B^->B diff, not the fix.
            diff_patch = bundle_dir / "diff.patch"
            if diff_patch.is_file():
                patch_text = _read_text(diff_patch)
                if patch_text.strip() == fix_diff.strip():
                    raise AnswerKeyLeakError(
                        "answer-key leak: bundle diff.patch is the fix diff, not B^->B"
                    )

    # 5. Fix tests / eval files: no test or eval paths smuggled into the
    #    snapshot that are not part of the B tree. The snapshot is built by
    #    git archive from B, so by construction it holds only B's files;
    #    here we refuse obvious smuggling: files whose names suggest they
    #    were added for the fix or the evaluation.
    snapshot_dir = bundle_dir / "snapshot"
    if snapshot_dir.is_dir():
        for p in snapshot_dir.rglob("*"):
            if not p.is_file():
                continue
            name = p.name.lower()
            if "eval" in name and "evaluation" not in str(p.parent).lower():
                # 'eval' in a filename inside the target snapshot is a
                # red flag for evaluation material.
                raise AnswerKeyLeakError(
                    f"answer-key leak: suspicious eval file in snapshot: {p.name}"
                )

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
