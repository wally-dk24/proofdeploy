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

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        seen["prompt"] = kw.get("text")

        class P:
            returncode = 0
            stdout = json.dumps({"content": "resp", "finish_reason": "stop"})
            stderr = ""

        return P()

    monkeypatch.setattr(mc.subprocess, "run", fake_run)
    resp = call_model("the prompt")
    assert resp.content == "resp"
    cmd = seen["cmd"]
    assert "--model" in cmd and "openai/gpt-oss-120b" in cmd
    assert "--temperature" in cmd and "0.2" in cmd
    assert "--user" in cmd and cmd[cmd.index("--user") + 1] == "the prompt"
    # No seed, no tools flags: the CLI sends exactly model + temperature.
    assert "--seed" not in cmd
