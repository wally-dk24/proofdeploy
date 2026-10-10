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
live in the frozen template ``proofdeploy/author_prompt_v3.md``. Its
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
import os
import re
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from proofdeploy import __version__

# Registered measurement configuration. These are constants, not
# parameters: a different model or temperature is a different
# measurement and must be registered, not passed at call time.
MODEL_ID = "openai/gpt-oss-120b"
TEMPERATURE = 0.2

# Frozen author-prompt template. The file is versioned; its hash is in
# every evidence record.
PROMPT_TEMPLATE_FILENAME = "author_prompt_v7.md"

_GROQ_API = "https://api.groq.com/openai/v1/chat/completions"
_GROQ_ALLOWED_HOSTS = ["api.groq.com"]
# Groq's edge WAF 403s requests without a real User-Agent (2026-10-08).
# Derived from the package version, never hardcoded.
_GROQ_USER_AGENT = f"proofdeploy/{__version__}"

_DIFF_FILE_RE = re.compile(r"^\+\+\+ b/(.+)$", re.MULTILINE)


def prompt_template_path() -> Path:
    """Path to the frozen author-prompt template."""
    return Path(__file__).with_name(PROMPT_TEMPLATE_FILENAME)


def author_prompt_sha256(path: str | Path | None = None) -> str:
    """SHA-256 of the frozen prompt template bytes (goes in the record)."""
    p = Path(path) if path else prompt_template_path()
    return hashlib.sha256(p.read_bytes()).hexdigest()


def author_prompt_builder_sha256() -> str:
    """SHA-256 of the prompt builder module itself (goes in the record)."""
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


# Top-level template sections, in order. A ``## <name>`` line starts a
# new section only when <name> is in this set; any other ``## `` line is
# content of the current section. That is what lets the ``## Body``
# skeleton contain literal ``## `` prompt headings.
_TEMPLATE_SECTIONS = (
    "Framing",
    "Output contract",
    "Caps",
    "Body",
    "File entry",
    "Unavailable file",
    "More files",
    "Caps line",
    "Diff files omitted",
    "Closing",
)

# Placeholders the builder substitutes with bundle content. The
# ``[[...]]`` syntax is used because the template contains literal JSON
# braces (the output contract), which would collide with str.format.
_PLACEHOLDERS = (
    "framing",
    "contract",
    "closing",
    "caps",
    "skill_text",
    "fixture_description",
    "diff_text",
    "max_diff_files",
    "max_file_bytes",
    "max_listed_files",
    "remaining",
    "diff_fence_open",
    "diff_fence_close",
    "snapshot_file_list",
    "touched_file_contents",
)


def _parse_template(text: str) -> dict[str, Any]:
    """Split the template into its sections.

    Fail-closed: every section in ``_TEMPLATE_SECTIONS`` must be
    present, exactly once, and no ``[[placeholder]]`` may be left
    unsubstituted by the builder (checked at build time).
    """
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in text.splitlines():
        if line.startswith("## ") and line[3:].strip() in _TEMPLATE_SECTIONS:
            current = line[3:].strip()
            if current in sections:
                raise ValueError(f"prompt template has duplicate section: {current}")
            sections[current] = []
        elif current is not None:
            sections[current].append(line)
    missing = [s for s in _TEMPLATE_SECTIONS if s not in sections]
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
    # Every placeholder used in the Body must be a known one; an unknown
    # [[...]] would silently survive substitution.
    for m in re.finditer(r"\[\[([a-z_]+)\]\]", "\n".join(sections["Body"])):
        if m.group(1) not in _PLACEHOLDERS:
            raise ValueError(
                f"prompt template Body uses unknown placeholder: [[{m.group(1)}]]"
            )
    return {
        "framing": "\n".join(sections["Framing"]).strip(),
        "contract": "\n".join(sections["Output contract"]).strip(),
        "body": "\n".join(sections["Body"]).strip(),
        "file_entry": "\n".join(sections["File entry"]).strip(),
        "unavailable_file": "\n".join(sections["Unavailable file"]).strip(),
        "more_files": "\n".join(sections["More files"]).strip(),
        "caps_line": "\n".join(sections["Caps line"]).strip(),
        "diff_files_omitted": "\n".join(sections["Diff files omitted"]).strip(),
        "closing": "\n".join(sections["Closing"]).strip(),
        "caps": caps,
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


def _fence_for(content: str, info: str = "") -> tuple[str, str]:
    """Code fence longer than any backtick run in the content.

    Prevents the content from accidentally closing the fence. Returns
    (opening, closing): the opening fence carries the info string
    (e.g. "diff"), the closing is the bare backtick run.
    Fence length = max(3, longest backtick run + 1).
    """
    max_run = 0
    run = 0
    for ch in content:
        if ch == "`":
            run += 1
            max_run = max(max_run, run)
        else:
            run = 0
    # Fence must be strictly longer than the longest backtick run,
    # and at least 3 (a valid Markdown code fence).
    fence = "`" * max(3, max_run + 1)
    opening = f"{fence}{info}" if info else fence
    return opening, fence


def build_author_prompt(
    *,
    skill_text: str | None,
    fixture_description: str,
    diff_text: str,
    snapshot_dir: str | Path,
    template_path: str | Path | None = None,
) -> tuple[str, dict[str, object]]:
    """Compose the blind author's prompt from bundle inputs only.

    The builder is the single source for the no-skill rendering: a None
    or empty skill_text is rendered as "(none)". Callers pass None/empty
    for "no skill"; they do not pre-render "(none)" themselves.

    Framing, output contract, and truncation caps come from the frozen
    template file. Deterministic: same bundle in, same prompt out. The
    snapshot contributes its file list and the full content of every
    file the diff touches (capped); nothing else on the machine is read.

    The prompt is a pure function of the verified bundle plus the
    frozen template. There is deliberately no channel for run metadata
    (no "run kind", no unit id): anything outside the bundle would let
    the orchestrator's knowledge leak to the model.

    Returns (prompt, truncation_flags). The flags record which caps
    applied (diff_files_truncated, file_bytes_truncated, listed_files_truncated);
    the caller records them in the evidence record.
    """
    tpl = load_prompt_template(template_path)
    caps = tpl["caps"]
    snap = Path(snapshot_dir)
    truncation_flags: dict[str, object] = {
        "diff_files_truncated": False,
        "file_bytes_truncated": False,
        "listed_files_truncated": False,
    }

    # Touched files in diff order; cap with explicit marker.
    all_touched = diff_touched_files(diff_text)
    if len(all_touched) > caps["max_diff_files"]:
        truncation_flags["diff_files_truncated"] = True
    touched = all_touched[: caps["max_diff_files"]]

    # File list sorted by path; cap with explicit marker.
    all_files = sorted(
        p.relative_to(snap).as_posix()
        for p in snap.rglob("*")
        if p.is_file() and not p.is_symlink()
    )
    if len(all_files) > caps["max_listed_files"]:
        truncation_flags["listed_files_truncated"] = True
    listed = all_files[: caps["max_listed_files"]]
    file_list = "\n".join(listed)
    if truncation_flags["listed_files_truncated"]:
        remaining = len(all_files) - caps["max_listed_files"]
        file_list += "\n" + tpl["more_files"].replace("[[remaining]]", str(remaining))

    # Touched file contents; cap bytes with explicit marker.
    touched_parts: list[str] = []
    for rel in touched:
        content = _read_text_capped(snap / rel, caps["max_file_bytes"])
        if content is None:
            # Check if it's too large (vs missing/binary) for the marker.
            try:
                size = (snap / rel).stat().st_size
                if size > caps["max_file_bytes"]:
                    truncation_flags["file_bytes_truncated"] = True
            except OSError:
                pass
            entry = tpl["unavailable_file"].replace("[[path]]", rel)
        else:
            file_open, file_close = _fence_for(content)
            entry = (
                tpl["file_entry"]
                .replace("[[path]]", rel)
                .replace("[[file_fence_open]]", file_open)
                .replace("[[file_fence_close]]", file_close)
                .replace("[[content]]", content.rstrip())
            )
        touched_parts.append(entry)
    touched_text = "\n\n".join(touched_parts)
    # Explicit marker when the diff-files cap truncated the list.
    # Rendered from the template's "Diff files omitted" section.
    if truncation_flags["diff_files_truncated"]:
        omitted = len(all_touched) - caps["max_diff_files"]
        marker = tpl["diff_files_omitted"]
        marker = marker.replace("[[remaining]]", str(omitted))
        marker = marker.replace("[[max_diff_files]]", str(caps["max_diff_files"]))
        touched_text += "\n\n" + marker

    # Diff fence adapts to backtick runs in the diff.
    diff_open, diff_close = _fence_for(diff_text, "diff")

    # Single-pass substitution: build the value map, then substitute each
    # [[placeholder]] in the template exactly once. Values are not re-scanned,
    # so content containing [[...]] (e.g. in a diff) is not expanded.
    # Single source for no-skill rendering: None/empty -> "(none)".
    rendered_skill = "(none)" if not skill_text or not skill_text.strip() else skill_text.rstrip()
    values = {
        "framing": tpl["framing"],
        "contract": tpl["contract"],
        "closing": tpl["closing"],
        # Caps line rendered from the template's "Caps line" section.
        # Uses "Caps:" (the approved framing says "caps").
        "caps": (
            tpl["caps_line"]
            .replace("[[max_diff_files]]", str(caps["max_diff_files"]))
            .replace("[[max_file_bytes]]", str(caps["max_file_bytes"]))
            .replace("[[max_listed_files]]", str(caps["max_listed_files"]))
        ),
        "skill_text": rendered_skill,
        "fixture_description": fixture_description.rstrip(),
        "diff_fence_open": diff_open,
        "diff_fence_close": diff_close,
        "diff_text": diff_text.rstrip(),
        "snapshot_file_list": file_list,
        "touched_file_contents": touched_text,
    }
    # Fail-closed: every [[...]] in the template must be a known placeholder.
    # Check the template, not the output: values may legitimately contain
    # [[...]] (e.g. a diff mentioning a placeholder name).
    template_placeholders = set(re.findall(r"\[\[([a-z_]+)\]\]", tpl["body"]))
    unknown = template_placeholders - set(values.keys())
    if unknown:
        raise ValueError(
            f"prompt template Body uses unknown placeholders: {sorted(unknown)}"
        )
    def _sub(m: re.Match[str]) -> str:
        v: str = values[m.group(1)]
        return v
    prompt = re.sub(r"\[\[([a-z_]+)\]\]", _sub, tpl["body"])
    return prompt, truncation_flags


def _message_hashes(messages: list[dict[str, Any]]) -> list[str]:
    """SHA-256 of each message's canonical JSON (for the record)."""
    return [
        hashlib.sha256(
            json.dumps(m, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        for m in messages
    ]


class ModelCallError(Exception):
    """The model call itself failed (context overflow, transport error).

    This is distinct from a model that returned unparsable content
    (which is a probe-validation matter, handled per-probe). Only
    ModelCallError (and subclasses) are caught by the orchestrator's
    INCONCLUSIVE path; our own parse/validation bugs must propagate.

    The ``cause`` is one of:
    - "model_overflow": the prompt exceeds the model's context; no retry.
    - "model_transport": no response, or a transport error; exactly one retry.
    """


    def __init__(self, message: str, cause: str):
        super().__init__(message)
        if cause not in ("model_overflow", "model_transport"):
            raise ValueError(f"unknown ModelCallError cause: {cause}")
        self.cause = cause


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


def _apply_credential(req: urllib.request.Request) -> str:
    """Attach the Groq credential to the request; return its source.

    Primary path is the authd surrogate (the same mechanism as the Groq
    skill): the raw key never exists in this process. On hosts without
    authd (e.g. GitHub Actions), fall back to the ``GROQ_API_KEY``
    environment variable. The key value is never logged and never
    enters the request record; only the source is recorded.
    """
    try:
        _credential_surrogate()(req)
        return "authd-surrogate"
    except Exception:
        pass
    key = os.environ.get("GROQ_API_KEY", "")
    if not key:
        raise RuntimeError(
            "Groq credential unavailable: no authd surrogate and "
            "GROQ_API_KEY is not set"
        )
    req.add_header("Authorization", f"Bearer {key}")
    return "env"


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
    request_record["credential_source"] = _apply_credential(req)
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
