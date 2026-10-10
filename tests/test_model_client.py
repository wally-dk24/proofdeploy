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
        run_context="Run kind: bug.",
    )
    assert build_author_prompt(**kw) == build_author_prompt(**kw)


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
    # Groq's WAF requires a real User-Agent.
    assert seen["ua"] == "wally-groq-cli/1.0"
    assert seen["cred_applied"]
    # The exact request is recorded for evidence.
    rec = resp.request_record
    assert rec["model"] == "openai/gpt-oss-120b"
    assert rec["temperature"] == 0.2
    assert rec["seed"] is None
    assert rec["tools"] is None
    assert len(rec["message_hashes"]) == 1 and len(rec["message_hashes"][0]) == 64
