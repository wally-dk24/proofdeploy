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

The author prompt's framing text, output contract, and truncation caps
live in the frozen template ``proofdeploy/author_prompt_v1.md``. Its
SHA-256 (``author_prompt_sha256()``) is recorded in every evidence
record; any change to the template is a new version that must be
re-registered before any measured run.

The prompt carries no secrets by construction (the bundle is assembled
from the allowlist, and the fixture description contains only the five
public template fields), and the orchestrator additionally runs the
fail-closed leak check from ``proofdeploy.leakcheck`` over the prompt
and every bundle file BEFORE calling the model.

``call_model`` takes an injectable ``runner`` so tests can substitute a
canned response. The default runner calls Groq's chat-completions API
directly over HTTPS (stdlib urllib) with the stored credential
surrogate; it does not shell out.
"""

from __future__ import annotations

import hashlib
import json
import re
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Registered measurement configuration. These are constants, not
# parameters: a different model or temperature is a different
# measurement and must be registered, not passed at call time.
MODEL_ID = "openai/gpt-oss-120b"
TEMPERATURE = 0.2

# Frozen author-prompt template. The file is versioned; its hash is in
# every evidence record.
PROMPT_TEMPLATE_FILENAME = "author_prompt_v1.md"

_GROQ_API = "https://api.groq.com/openai/v1/chat/completions"
_GROQ_ALLOWED_HOSTS = ["api.groq.com"]
# Groq's edge WAF 403s requests without a real User-Agent (2026-10-08).
_GROQ_USER_AGENT = "wally-groq-cli/1.0"

_DIFF_FILE_RE = re.compile(r"^\+\+\+ b/(.+)$", re.MULTILINE)


def prompt_template_path() -> Path:
    """Path to the frozen author-prompt template."""
    return Path(__file__).with_name(PROMPT_TEMPLATE_FILENAME)


def author_prompt_sha256(path: str | Path | None = None) -> str:
    """SHA-256 of the frozen prompt template bytes (goes in the record)."""
    p = Path(path) if path else prompt_template_path()
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _parse_template(text: str) -> dict[str, Any]:
    """Split the template into framing, contract, caps, closing.

    Sections are ``## Framing``, ``## Output contract``, ``## Caps``,
    ``## Closing``. Fail-closed: every section must be present.
    """
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in text.splitlines():
        if line.startswith("## "):
            current = line[3:].strip()
            sections[current] = []
        elif current is not None:
            sections[current].append(line)
    required = ("Framing", "Output contract", "Caps", "Closing")
    missing = [s for s in required if s not in sections]
    if missing:
        raise ValueError(
            f"prompt template missing sections: {', '.join(missing)}"
        )
    caps: dict[str, int] = {}
    for line in sections["Caps"]:
        m = re.match(r"\s*-\s*`([^`]+)`\s*:\s*(\d+)", line)
        if m:
            caps[m.group(1)] = int(m.group(2))
    for key in ("max_diff_files", "max_file_bytes", "max_listed_files"):
        if key not in caps:
            raise ValueError(f"prompt template caps missing {key}")
    return {
        "framing": "\n".join(sections["Framing"]).strip(),
        "contract": "\n".join(sections["Output contract"]).strip(),
        "caps": caps,
        "closing": "\n".join(sections["Closing"]).strip(),
    }


def load_prompt_template(path: str | Path | None = None) -> dict[str, Any]:
    """Parse the frozen template (fail-closed on any structural change)."""
    p = Path(path) if path else prompt_template_path()
    return _parse_template(p.read_text(encoding="utf-8"))


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


def _read_text_capped(path: Path, cap: int) -> str | None:
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


def build_author_prompt(
    *,
    skill_text: str,
    fixture_description: str,
    diff_text: str,
    snapshot_dir: str | Path,
    run_context: str = "",
    template_path: str | Path | None = None,
) -> str:
    """Compose the blind author's prompt from bundle inputs only.

    Framing, output contract, and truncation caps come from the frozen
    template file. Deterministic: same bundle in, same prompt out. The
    snapshot contributes its file list and the full content of every
    file the diff touches (capped); nothing else on the machine is read.
    """
    tpl = load_prompt_template(template_path)
    caps = tpl["caps"]
    snap = Path(snapshot_dir)
    touched = diff_touched_files(diff_text)[: caps["max_diff_files"]]
    all_files = sorted(
        p.relative_to(snap).as_posix()
        for p in snap.rglob("*")
        if p.is_file() and not p.is_symlink()
    )
    listed = all_files[: caps["max_listed_files"]]

    parts = [tpl["framing"], ""]
    if run_context:
        parts += [run_context.strip(), ""]
    parts += [
        tpl["contract"],
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
    if len(all_files) > caps["max_listed_files"]:
        parts.append(f"... ({len(all_files) - caps['max_listed_files']} more files)")
    parts += ["", "---", "## Touched file contents", ""]
    for rel in touched:
        content = _read_text_capped(snap / rel, caps["max_file_bytes"])
        parts.append(f"### FILE: {rel}")
        parts.append("")
        if content is None:
            parts.append("(binary, missing, or too large: not inlined)")
        else:
            parts.append("```")
            parts.append(content.rstrip())
            parts.append("```")
        parts.append("")
    parts.append(tpl["closing"])
    return "\n".join(parts)


def _message_hashes(messages: list[dict[str, Any]]) -> list[str]:
    """SHA-256 of each message's canonical JSON (for the record)."""
    return [
        hashlib.sha256(
            json.dumps(m, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        for m in messages
    ]


@dataclass
class ModelResponse:
    """What the model returned, with transport metadata."""

    content: str
    finish_reason: str | None = None
    usage: dict[str, Any] | None = None
    # The exact request as sent, for the evidence record.
    request_record: dict[str, Any] = field(default_factory=dict)


ModelRunner = Callable[[str], ModelResponse]
"""A function taking the prompt and returning the model's response."""


def _credential_surrogate() -> Callable[[urllib.request.Request], None]:
    """The stored-credential injector (same mechanism as the Groq skill)."""
    import sys

    sys.path.insert(0, "/opt/hatch/skills/skill-creator/bin")
    from dynamic_credentials import add_surrogate_to_request  # type: ignore[import-not-found]

    def apply(req: urllib.request.Request) -> None:
        add_surrogate_to_request(
            req, "custom.groq", allowed_hosts=_GROQ_ALLOWED_HOSTS
        )

    return apply


def groq_runner(prompt: str) -> ModelResponse:
    """Default runner: the registered model via Groq's API, in-repo.

    One attempt, temperature 0.2, no seed, no tools, no web. The HTTPS
    call is made directly (stdlib urllib); nothing is shelled out.
    Raises RuntimeError if the call fails.
    """
    messages = [{"role": "user", "content": prompt}]
    body: dict[str, Any] = {
        "model": MODEL_ID,
        "messages": messages,
        "temperature": TEMPERATURE,
        # seed and tools are deliberately ABSENT: the registered config
        # sends neither. Their absence is recorded, not assumed.
    }
    request_record: dict[str, Any] = {
        "model": MODEL_ID,
        "temperature": TEMPERATURE,
        "seed": None,  # not sent
        "tools": None,  # not sent
        "message_hashes": _message_hashes(messages),
        "endpoint": _GROQ_API,
    }
    req = urllib.request.Request(
        _GROQ_API,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "User-Agent": _GROQ_USER_AGENT,
        },
    )
    _credential_surrogate()(req)
    try:
        with urllib.request.urlopen(req, timeout=240) as resp:
            out = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        raise RuntimeError(f"Groq API call failed: {e}") from e
    try:
        choice = out["choices"][0]
        msg = choice["message"]
    except (KeyError, IndexError, TypeError) as e:
        raise RuntimeError(f"Groq API returned unexpected shape: {e}") from e
    return ModelResponse(
        content=msg.get("content") or "",
        finish_reason=choice.get("finish_reason"),
        usage=out.get("usage"),
        request_record=request_record,
    )


def call_model(prompt: str, runner: ModelRunner | None = None) -> ModelResponse:
    """Call the registered model exactly once with the prompt."""
    return (runner or groq_runner)(prompt)
