"""Tests for proofdeploy.model_client (WO-5)."""

import json

from proofdeploy.model_client import (
    MODEL_ID,
    TEMPERATURE,
    ModelResponse,
    build_author_prompt,
    call_model,
    diff_touched_files,
)


def test_registered_model_config():
    # The registered measurement configuration is fixed in code.
    assert MODEL_ID == "openai/gpt-oss-120b"
    assert TEMPERATURE == 0.2


def test_diff_touched_files():
    diff = (
        "diff --git a/app.py b/app.py\n"
        "index 111..222 100644\n"
        "--- a/app.py\n"
        "+++ b/app.py\n"
        "@@ -1 +1 @@\n"
        "-x\n"
        "+y\n"
        "diff --git a/notes.md b/notes.md\n"
        "--- /dev/null\n"
        "+++ b/notes.md\n"
    )
    assert diff_touched_files(diff) == ["app.py", "notes.md"]


def test_build_author_prompt_deterministic(tmp_path):
    snap = tmp_path / "snap"
    snap.mkdir()
    (snap / "app.py").write_text("print('hi')\n")
    (snap / "other.py").write_text("print('other')\n")
    diff = "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-print('hi')\n+print('bye')\n"
    kw = dict(
        skill_text="SKILL",
        fixture_description="Repository: demo",
        diff_text=diff,
        snapshot_dir=snap,
    )
    assert build_author_prompt(**kw) == build_author_prompt(**kw)


def test_build_author_prompt_has_no_run_context_parameter():
    """The run_context channel is removed: the prompt is a pure function
    of the verified bundle plus the frozen template. A run_context-style
    hint has nowhere to go."""
    import inspect

    from proofdeploy.leakcheck import check_bundle_answer_key

    assert "run_context" not in inspect.signature(build_author_prompt).parameters
    assert "run_context" not in inspect.signature(check_bundle_answer_key).parameters
    # Passing it is a TypeError, not a silent extra.
    import pytest

    with pytest.raises(TypeError):
        build_author_prompt(
            skill_text="s",
            fixture_description="f",
            diff_text="d",
            snapshot_dir=".",
            run_context="Run kind: bug.",
        )


def test_prompt_carries_no_run_metadata(tmp_path):
    """No 'Run kind: bug|clean' or unit id may appear in the prompt."""
    snap = tmp_path / "snap"
    snap.mkdir()
    (snap / "app.py").write_text("x = 1\n")
    prompt = build_author_prompt(
        skill_text="SKILL",
        fixture_description="Repository: demo",
        diff_text="--- a/app.py\n+++ b/app.py\n",
        snapshot_dir=snap,
    )
    assert "Run kind:" not in prompt
    assert "Unit id:" not in prompt


def test_empty_bundle_yields_only_template_text(tmp_path):
    """With an empty bundle, every heading/label in the prompt comes
    from the frozen template file: no Python-injected headings."""
    from proofdeploy.model_client import prompt_template_path

    snap = tmp_path / "snap"
    snap.mkdir()
    prompt = build_author_prompt(
        skill_text="",
        fixture_description="",
        diff_text="",
        snapshot_dir=snap,
    )
    template_text = prompt_template_path().read_text(encoding="utf-8")
    template_headings = {
        line.strip()
        for line in template_text.splitlines()
        if line.startswith("## ") or line.startswith("### ")
    }
    prompt_headings = [
        line.strip()
        for line in prompt.splitlines()
        if line.startswith("## ") or line.startswith("### ")
    ]
    assert prompt_headings, "prompt should still have template headings"
    for heading in prompt_headings:
        assert heading in template_headings, f"non-template heading: {heading!r}"


def test_template_covers_diff_label(tmp_path):
    """The '## Diff (the change under test)' label lives in the template,
    not in Python code."""
    from proofdeploy.model_client import prompt_template_path

    template_text = prompt_template_path().read_text(encoding="utf-8")
    assert "## Diff (the change under test)" in template_text
    snap = tmp_path / "snap"
    snap.mkdir()
    (snap / "app.py").write_text("x = 1\n")
    prompt = build_author_prompt(
        skill_text="",
        fixture_description="",
        diff_text="--- a/app.py\n+++ b/app.py\n",
        snapshot_dir=snap,
    )
    assert "## Diff (the change under test)" in prompt


def test_build_author_prompt_contains_bundle_only(tmp_path):
    snap = tmp_path / "snap"
    snap.mkdir()
    (snap / "app.py").write_text("SECRET_MARKER = 1\n")
    (snap / "untouched.py").write_text("x = 1\n")
    diff = "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-SECRET_MARKER = 0\n+SECRET_MARKER = 1\n"
    prompt = build_author_prompt(
        skill_text="SKILL TEXT",
        fixture_description="Repository: demo",
        diff_text=diff,
        snapshot_dir=snap,
    )
    assert "SKILL TEXT" in prompt
    assert "Repository: demo" in prompt
    assert "SECRET_MARKER = 1" in prompt  # touched file content inlined
    assert "untouched.py" in prompt  # file list mentions it
    assert "x = 1" not in prompt  # untouched content not inlined
    assert "Output contract" in prompt


def test_call_model_uses_injected_runner():
    def fake_runner(prompt: str) -> ModelResponse:
        assert "hello" in prompt
        return ModelResponse(content="```json\n{}\n```", finish_reason="stop")

    resp = call_model("hello", runner=fake_runner)
    assert resp.content.startswith("```json")
    assert resp.finish_reason == "stop"


def test_call_model_default_runner_is_groq(monkeypatch):
    import proofdeploy.model_client as mc

    seen: dict = {}

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps(
                {
                    "choices": [
                        {
                            "message": {"content": "resp"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"total_tokens": 10},
                }
            ).encode()

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["body"] = json.loads(req.data.decode())
        seen["ua"] = req.get_header("User-agent")
        return FakeResp()

    def fake_cred(req):
        seen["cred_applied"] = True

    monkeypatch.setattr(mc.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(mc, "_credential_surrogate", lambda: fake_cred)
    resp = call_model("the prompt")
    assert resp.content == "resp"
    assert resp.finish_reason == "stop"
    assert seen["url"] == "https://api.groq.com/openai/v1/chat/completions"
    body = seen["body"]
    assert body["model"] == "openai/gpt-oss-120b"
    assert body["temperature"] == 0.2
    assert body["messages"] == [{"role": "user", "content": "the prompt"}]
    # No seed, no tools: the request sends exactly model + messages + temperature.
    assert "seed" not in body
    assert "tools" not in body
    # Groq's WAF requires a real User-Agent, derived from the package version.
    from proofdeploy import __version__

    assert seen["ua"] == f"proofdeploy/{__version__}"
    assert seen["cred_applied"]
    # The exact request is recorded for evidence.
    rec = resp.request_record
    assert rec["model"] == "openai/gpt-oss-120b"
    assert rec["temperature"] == 0.2
    assert rec["seed"] is None
    assert rec["tools"] is None
    assert len(rec["message_hashes"]) == 1 and len(rec["message_hashes"][0]) == 64


def test_credential_env_fallback(monkeypatch):
    """Without authd, the Groq key comes from GROQ_API_KEY; the key value
    never enters the request record, only the source is recorded."""
    import urllib.request

    import proofdeploy.model_client as mc

    def no_authd():
        raise RuntimeError("no authd here")

    monkeypatch.setattr(mc, "_credential_surrogate", no_authd)
    monkeypatch.setenv("GROQ_API_KEY", "sk-test-key-123")
    req = urllib.request.Request("https://api.groq.com/openai/v1/chat/completions")
    assert mc._apply_credential(req) == "env"
    assert req.get_header("Authorization") == "Bearer sk-test-key-123"


def test_credential_unavailable_raises(monkeypatch):
    import urllib.request

    import proofdeploy.model_client as mc

    def no_authd():
        raise RuntimeError("no authd here")

    monkeypatch.setattr(mc, "_credential_surrogate", no_authd)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    req = urllib.request.Request("https://api.groq.com/openai/v1/chat/completions")
    try:
        mc._apply_credential(req)
    except RuntimeError as e:
        assert "GROQ_API_KEY" in str(e)
    else:
        raise AssertionError("expected RuntimeError")


def test_template_v3_hash_and_content():
    """The v3 template is the registered file: hash matches, the
    harness example shows python|node, and the assertion sentence is
    Master's verbatim wording."""
    import hashlib
    import re
    from pathlib import Path

    import proofdeploy.model_client as mc

    assert mc.PROMPT_TEMPLATE_FILENAME == "author_prompt_v3.md"
    path = Path(mc.__file__).with_name("author_prompt_v3.md")
    text = path.read_text(encoding="utf-8")
    assert hashlib.sha256(text.encode("utf-8")).hexdigest() == mc.author_prompt_sha256()
    assert '"language": "python|node"' in text
    flat = re.sub(r"\s+", " ", text)
    want = (
        "Every probe must exercise at least one behavior the diff changed, and "
        "assert how a correct implementation of the change should behave, not "
        "merely what the code currently does. A probe that passes identically "
        "with and without the diff tells us nothing."
    )
    assert want in flat
