"""D101 item 3a: every model-calling workflow is serialized.

verify.yml and measure.yml call the pinned model with GROQ_API_KEY
from the `measurement` environment. Two model-calling runs at once
would interleave their evidence and compete for the same gated key,
so all model-calling workflows share ONE concurrency group with
cancel-in-progress: false and queue: max — runs are queued up to
100, sequential, and never cancelled mid-flight. queue: max is
required (D159): GitHub's default keeps one running + ONE pending
run per group and cancels the older pending run when another
arrives, which cancel-in-progress: false does not prevent.

The guard is textual + structural: a workflow "can call the model"
when its text references GROQ_API_KEY or invokes the measure/verify
CLI (the model client paths). Every such workflow must declare the
exact shared group at top level. ci.yml / sandbox-tests.yml carry
their own per-ref concurrency for test shards and do not call the
model (no key is ever in their environment); they are out of scope.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"

MODEL_CONCURRENCY_GROUP = "proofdeploy-model-calls"

_MODEL_CLI = re.compile(r"proofdeploy(?:\.cli)?\s+(?:measure|verify)\b")


def _can_call_model(text: str) -> bool:
    return "GROQ_API_KEY" in text or bool(_MODEL_CLI.search(text))


def _violations(workflows_dir: Path) -> list[str]:
    problems: list[str] = []
    for path in sorted(workflows_dir.glob("*.yml")):
        text = path.read_text(encoding="utf-8")
        if not _can_call_model(text):
            continue
        data = yaml.safe_load(text)
        concurrency = data.get("concurrency") if isinstance(data, dict) else None
        if not isinstance(concurrency, dict):
            problems.append(
                f"{path.name}: model-calling workflow declares no "
                f"top-level concurrency group (expected "
                f"{MODEL_CONCURRENCY_GROUP!r})"
            )
            continue
        if concurrency.get("group") != MODEL_CONCURRENCY_GROUP:
            problems.append(
                f"{path.name}: concurrency group "
                f"{concurrency.get('group')!r} != {MODEL_CONCURRENCY_GROUP!r}"
            )
        if concurrency.get("cancel-in-progress") is not False:
            problems.append(
                f"{path.name}: cancel-in-progress is "
                f"{concurrency.get('cancel-in-progress')!r}, expected False"
            )
        if concurrency.get("queue") != "max":
            problems.append(
                f"{path.name}: concurrency queue is "
                f"{concurrency.get('queue')!r}, expected 'max' "
                f"(queued up to 100, sequential)"
            )
    return problems


def test_every_model_calling_workflow_shares_the_serialization_group():
    assert _violations(WORKFLOWS) == []


def test_the_known_model_workflows_are_detected():
    # The detector must not silently match nothing: verify and
    # measure are the two workflows that hold GROQ_API_KEY today.
    detected = {
        path.name
        for path in sorted(WORKFLOWS.glob("*.yml"))
        if _can_call_model(path.read_text(encoding="utf-8"))
    }
    assert {"verify.yml", "measure.yml"} <= detected


def _write_workflow(dir_: Path, name: str, body: str) -> None:
    (dir_ / name).write_text(body, encoding="utf-8")


_MODEL_STEP = """name: fake
on: workflow_dispatch
jobs:
  run:
    runs-on: ubuntu-latest
    steps:
      - run: proofdeploy measure --repo x
        env:
          GROQ_API_KEY: ${{ secrets.GROQ_API_KEY }}
"""


def test_guard_fails_when_a_model_workflow_lacks_the_group(tmp_path):
    _write_workflow(tmp_path, "fake.yml", _MODEL_STEP)
    problems = _violations(tmp_path)
    assert len(problems) == 1
    assert "no top-level concurrency group" in problems[0]


def test_guard_fails_when_cancel_in_progress_is_true(tmp_path):
    body = _MODEL_STEP.replace(
        "jobs:",
        "concurrency:\n"
        f"  group: {MODEL_CONCURRENCY_GROUP}\n"
        "  cancel-in-progress: true\n"
        "  queue: max\n\n"
        "jobs:",
        1,
    )
    _write_workflow(tmp_path, "fake.yml", body)
    problems = _violations(tmp_path)
    assert len(problems) == 1
    assert "cancel-in-progress" in problems[0]


def test_guard_fails_on_a_different_group(tmp_path):
    body = _MODEL_STEP.replace(
        "jobs:",
        "concurrency:\n  group: some-other-group\n  cancel-in-progress: false\n"
        "  queue: max\n\njobs:",
        1,
    )
    _write_workflow(tmp_path, "fake.yml", body)
    problems = _violations(tmp_path)
    assert len(problems) == 1
    assert "some-other-group" in problems[0]


def test_guard_fails_when_queue_is_not_max(tmp_path):
    # D159: a concurrency block without queue: max must be reported —
    # GitHub would keep only one pending run and cancel the older
    # pending one when another arrives.
    body = _MODEL_STEP.replace(
        "jobs:",
        f"concurrency:\n  group: {MODEL_CONCURRENCY_GROUP}\n  cancel-in-progress: false\n\njobs:",
        1,
    )
    _write_workflow(tmp_path, "fake.yml", body)
    problems = _violations(tmp_path)
    assert len(problems) == 1
    assert "queue" in problems[0]
    assert "'max'" in problems[0]


def test_guard_ignores_a_non_model_workflow_without_the_group(tmp_path):
    _write_workflow(
        tmp_path,
        "plain.yml",
        "name: plain\non: workflow_dispatch\njobs:\n  run:\n"
        "    runs-on: ubuntu-latest\n    steps:\n      - run: echo hi\n",
    )
    assert _violations(tmp_path) == []
