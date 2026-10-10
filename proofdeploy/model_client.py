"""Blind probe-author model client (WO-5).

Registered measurement configuration (do not change per-run):
- model: ``openai/gpt-oss-120b`` through Groq
- temperature: 0.2
- exactly one attempt, no seed, no tools, no web

The client builds the author's prompt from the assembled bundle only:
the registered skill text, the fixture description, the B^->B diff, and
the snapshot (file list plus the full content of every file the diff
touches). The snapshot directory itself never leaves the machine; only
this prompt string is sent to the model.

The prompt carries no secrets by construction (the bundle is assembled
from the allowlist, and the fixture description contains only the five
public template fields), and the orchestrator additionally runs the
fail-closed leak check from ``proofdeploy.leakcheck`` over the prompt
and every bundle file BEFORE calling the model.

``call_model`` takes an injectable ``runner`` so tests can substitute a
canned response. The default runner shells out to the Groq skill CLI
(``~/workspace/skills/groq/bin/groq_chat.py``), which attaches the
stored credential and sets the User-Agent Groq's WAF requires.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Registered measurement configuration. These are constants, not
# parameters: a different model or temperature is a different
# measurement and must be registered, not passed at call time.
MODEL_ID = "openai/gpt-oss-120b"
TEMPERATURE = 0.2

# Prompt budget guards. The snapshot is summarized, not dumped: the
# model gets the full content of files the diff touches (those are the
# behaviors it must probe), plus the file list for orientation. Large
# repos need retrieval instead of inlining; that is future work, noted
# in ADR-0009.
_MAX_DIFF_FILES = 20
_MAX_FILE_BYTES = 100_000
_MAX_LISTED_FILES = 200

_DIFF_FILE_RE = re.compile(r"^\+\+\+ b/(.+)$", re.MULTILINE)


def diff_touched_files(diff_text: str) -> list[str]:
    """Relative paths the diff touches, in first-appearance order."""
    seen: set[str] = set()
    out: list[str] = []
    for m in _DIFF_FILE_RE.finditer(diff_text):
        p = m.group(1).strip()
        if p and p != "/dev/null" and p not in seen:
            seen.add(p)
            out.append(p)
    return out


def _read_text_capped(path: Path, cap: int = _MAX_FILE_BYTES) -> str | None:
    """File text, or None for missing/binary/oversized files."""
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if len(data) > cap or b"\x00" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


OUTPUT_CONTRACT = """\
## Output contract

Output each probe as a fenced ```json block, one block per probe, nothing
else inside the blocks. A probe is exactly:

{"setup": [...], "act": {"method": "...", "path": "/..."}, "assert": [...]}

- "setup": a list of steps. Step types:
  - {"type": "http", "act": {"method": "GET|POST|PUT|PATCH|DELETE|HEAD",
    "path": "/...", "headers": {...}, "body": ...},
    "capture": {"NAME": {"from": "body", "json_path": "dotted.path"}}}
    Use setup steps to create preconditions (users, records) and capture
    values (e.g. tokens) for later steps as ${NAME}.
  - {"type": "harness_app", "language": "python",
    "source": "<a minimal app that mounts the library and serves HTTP>",
    "entrypoint": "app.py"}
    Only when the fixture says the target is a library. The harness app
    is started for you; its base URL replaces the target URL.
- Placeholders: ${AUTH_TOKEN} is your injected bearer token (use it in an
  "Authorization": "Bearer ${AUTH_TOKEN}" header when the fixture says
  authentication is by bearer token). ${NAME} references a captured value.
  Never write a literal token or password: you do not have any.
- "act": one HTTP request: {"method", "path", "headers", "body"}.
  Methods allowed: GET, HEAD, POST, PUT, PATCH, DELETE.
- "assert": non-empty list of assertions:
  - {"type": "status", "equals": 200}
  - {"type": "body", "contains": "text"} | {"matches": "regex"} |
    {"equals": ...} | {"json_path": {"path": "a.b", "equals": ...}}
  - {"type": "header", "name": "X-...", "equals"|"contains"|"matches": ...}
  - {"type": "db", "query": "SELECT ...", "count": N} (read-only SELECT only)
- Assert only externally observable behavior: status codes, headers,
  bodies, read-only database state. Every probe must exercise at least
  one behavior the diff changed: a probe that passes identically with
  and without the diff tells us nothing.
"""


def build_author_prompt(
    *,
    skill_text: str,
    fixture_description: str,
    diff_text: str,
    snapshot_dir: str | Path,
    run_context: str = "",
) -> str:
    """Compose the blind author's prompt from bundle inputs only.

    Deterministic: same bundle in, same prompt out. The snapshot
    contributes its file list and the full content of every file the
    diff touches (capped); nothing else on the machine is read.
    """
    snap = Path(snapshot_dir)
    touched = diff_touched_files(diff_text)[:_MAX_DIFF_FILES]
    all_files = sorted(
        p.relative_to(snap).as_posix()
        for p in snap.rglob("*")
        if p.is_file() and not p.is_symlink()
    )
    listed = all_files[:_MAX_LISTED_FILES]

    parts = [
        "# ProofDeploy blind probe authoring",
        "",
        "You are authoring black-box probes for a code change. "
        "You see the diff, the repository snapshot at the changed commit, "
        "and a fixture description. You do not see the fix.",
        "",
    ]
    if run_context:
        parts += [run_context.strip(), ""]
    parts += [
        OUTPUT_CONTRACT,
        "---",
        "## Authoring skill",
        "",
        skill_text.rstrip(),
        "",
        "---",
        "## Fixture",
        "",
        fixture_description.rstrip(),
        "",
        "---",
        "## Diff (the change under test)",
        "",
        "```diff",
        diff_text.rstrip(),
        "```",
        "",
        "---",
        "## Snapshot file list",
        "",
        "\n".join(listed),
    ]
    if len(all_files) > _MAX_LISTED_FILES:
        parts.append(f"... ({len(all_files) - _MAX_LISTED_FILES} more files)")
    parts += ["", "---", "## Touched file contents", ""]
    for rel in touched:
        content = _read_text_capped(snap / rel)
        parts.append(f"### FILE: {rel}")
        parts.append("")
        if content is None:
            parts.append("(binary, missing, or too large: not inlined)")
        else:
            parts.append("```")
            parts.append(content.rstrip())
            parts.append("```")
        parts.append("")
    parts.append("Write your probes now: one fenced ```json block per probe.")
    return "\n".join(parts)


@dataclass
class ModelResponse:
    """What the model returned, with transport metadata."""

    content: str
    finish_reason: str | None = None
    usage: dict[str, Any] | None = None


ModelRunner = Callable[[str], ModelResponse]
"""A function taking the prompt and returning the model's response."""


def groq_runner(prompt: str) -> ModelResponse:
    """Default runner: the registered model via the Groq skill CLI.

    One attempt, temperature 0.2, no seed, no tools, no web: the CLI
    sends exactly the chat-completions call with those settings.
    Raises RuntimeError if the CLI fails.
    """
    cli = Path.home() / "workspace" / "skills" / "groq" / "bin" / "groq_chat.py"
    proc = subprocess.run(
        [
            "python3",
            str(cli),
            "--model",
            MODEL_ID,
            "--temperature",
            str(TEMPERATURE),
            "--user",
            prompt,
        ],
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"groq_chat.py failed (exit {proc.returncode}): "
            f"{proc.stderr.strip()[:500]}"
        )
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"groq_chat.py printed invalid JSON: {e}") from e
    return ModelResponse(
        content=data.get("content") or "",
        finish_reason=data.get("finish_reason"),
        usage=data.get("usage"),
    )


def call_model(prompt: str, runner: ModelRunner | None = None) -> ModelResponse:
    """Call the registered model exactly once with the prompt."""
    return (runner or groq_runner)(prompt)
