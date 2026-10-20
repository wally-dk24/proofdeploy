"""Shared stubs for the lean P4 tests (host and sandbox): a judge that answers from the prompt.

No real model is ever called: every test hands the pipeline one of these runners.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

from proofdeploy.model_client import ModelResponse

FACT_OPEN = "id 1 (done=false)"
FACT_DONE = "id 2 (done=true)"

_GROUP_RE = re.compile(
    r"^\[(\d+)\] (\w+) (\S+), field (\S+) \((\w+)\): before=(.*?) after=(.*?)$", re.MULTILINE
)


class PromptStub:
    """Answers every difference group by rule; records what it was asked."""

    def __init__(self, rule: Callable[[str, str, str, Any, Any], dict[str, Any] | None]):
        self.rule = rule
        self.prompts: list[str] = []

    def __call__(self, prompt: str) -> ModelResponse:
        self.prompts.append(prompt)
        answers = []
        for m in _GROUP_RE.finditer(prompt):
            n, method, tpl, field, kind, before, after = m.groups()
            a = self.rule(f"{method} {tpl}", field, kind, json.loads(before), json.loads(after))
            assert a is not None, f"stub has no rule for {method} {tpl} {field} {kind}"
            answers.append({"group": int(n), **a})
        usage = {"prompt_tokens": 500, "completion_tokens": 100, "total_tokens": 600}
        return ModelResponse(json.dumps(answers), "stop", usage)


def bug_rule(op: str, field: str, kind: str, before: Any, after: Any) -> dict[str, Any] | None:
    """The done flag must keep following the seed facts; a change away from them is a regression."""
    if op == "GET /notes/{note_id}" and field == "done" and kind == "changed":
        return {
            "verdict": "regression",
            "expected_value": before,
            "evidence": {"source": "facts", "quote": FACT_OPEN if before is False else FACT_DONE},
        }
    return None


def clean_rule(op: str, field: str, kind: str, before: Any, after: Any) -> dict[str, Any] | None:
    if field == "tag" and kind == "added_field":
        return {
            "verdict": "correct",
            "expected_value": None,
            "evidence": {"source": "intent", "quote": "add a tag field to note responses"},
        }
    if field == "done":
        quote = FACT_OPEN if after is False else FACT_DONE
        return {
            "verdict": "correct",
            "expected_value": None,
            "evidence": {"source": "facts", "quote": quote},
        }
    return None


class NoModel:
    """A runner that must never be called."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, prompt: str) -> ModelResponse:
        self.calls += 1
        raise AssertionError("the model must not be called here")
