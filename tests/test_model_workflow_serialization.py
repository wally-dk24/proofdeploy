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
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"

MODEL_CONCURRENCY_GROUP = "proofdeploy-model-calls"

# ``gate.py --rehearse`` (lean-rehearse.yml, round 3 R5) never calls the model: the rehearsal
# code path has no judge and never reads the key (tests/test_lean_env.py asserts both). A
# workflow that names the key still counts, whatever it runs.
_MODEL_CLI = re.compile(
    r"proofdeploy(?:\.cli)?\s+(?:measure|verify|lean-verify)\b"
    r"|experiments/lean-poc/gate\.py(?![^\n]*--rehearse\b)"
)


def _can_call_model(text: str) -> bool:
    joined = re.sub(r"\\\n\s*", " ", text)  # a backslash-continued command is one command
    return "GROQ_API_KEY" in text or bool(_MODEL_CLI.search(joined))


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
    # The detector must not silently match nothing: verify, measure and
    # lean-poc (Lean P6b) are the workflows that hold GROQ_API_KEY today.
    detected = {
        path.name
        for path in sorted(WORKFLOWS.glob("*.yml"))
        if _can_call_model(path.read_text(encoding="utf-8"))
    }
    assert {"verify.yml", "measure.yml", "lean-poc.yml"} <= detected


_REHEARSE_STEP = """name: fake
on: workflow_dispatch
jobs:
  run:
    runs-on: ubuntu-latest
    steps:
      - run: |
          python experiments/lean-poc/gate.py \\
            --rehearse \\
            --out /tmp/o
"""


def test_a_no_key_rehearsal_invocation_is_not_a_model_workflow(tmp_path):
    _write_workflow(tmp_path, "rehearse.yml", _REHEARSE_STEP)
    assert _violations(tmp_path) == []


def test_a_rehearsal_workflow_that_names_the_key_is_still_a_model_workflow(tmp_path):
    body = _REHEARSE_STEP.replace(
        "--out /tmp/o", "--out /tmp/o\n        env:\n          GROQ_API_KEY: x"
    )
    _write_workflow(tmp_path, "rehearse.yml", body)
    assert len(_violations(tmp_path)) == 1


def test_the_gate_without_rehearse_is_still_a_model_workflow(tmp_path):
    _write_workflow(tmp_path, "gate.yml", _REHEARSE_STEP.replace("--rehearse", "--units all"))
    assert len(_violations(tmp_path)) == 1


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


# ----------------------------------------------------------------------
# D154: measure.yml's golden branch — workflow shape
#
# Golden units were refused on the measured path (contract_missing:
# the path loads only contract-store entries, and the golden apps
# had none). D154 adds the store entries (guarded in
# test_readiness_harness.py) and a golden branch in measure.yml:
# with unit_repo = golden/<app> the clone is skipped, the kit
# materializes the app at the commit being run, and the measure
# step receives the kit's revision SHAs plus --unit-repo. These
# tests pin the workflow's shape only; nothing here dispatches.
# ----------------------------------------------------------------------

MEASURE_YML = WORKFLOWS / "measure.yml"

GOLDEN_CHOICES = ["", "golden/node-express-sqlite", "golden/python-flask-sqlite"]


def _measure_workflow() -> dict[str, Any]:
    return yaml.safe_load(MEASURE_YML.read_text(encoding="utf-8"))


def _measure_steps() -> list[dict[str, Any]]:
    return _measure_workflow()["jobs"]["measure"]["steps"]


def _measure_step(name: str) -> dict[str, Any]:
    for step in _measure_steps():
        if step.get("name") == name:
            return step
    raise AssertionError(f"no step named {name!r}")


def test_unit_repo_input_is_a_choice_with_exactly_the_ruled_values():
    data = _measure_workflow()
    on = data.get("on", data.get(True))
    unit_repo = on["workflow_dispatch"]["inputs"]["unit_repo"]
    assert unit_repo["type"] == "choice"
    assert unit_repo["options"] == GOLDEN_CHOICES
    assert unit_repo["required"] is False
    assert unit_repo["default"] == ""


def test_inputs_reach_the_shell_only_via_env():
    # No ${{ inputs.* }} may be interpolated into a run script;
    # inputs reach the shell through step env only.
    for step in _measure_steps():
        run = step.get("run", "")
        assert "${{ inputs." not in run, step.get("name")
        assert "${{inputs." not in run, step.get("name")


def test_measure_environment_and_concurrency_are_unchanged():
    data = _measure_workflow()
    assert data["jobs"]["measure"]["environment"] == "measurement"
    concurrency = data["concurrency"]
    assert concurrency["group"] == MODEL_CONCURRENCY_GROUP
    assert concurrency["cancel-in-progress"] is False
    assert concurrency["queue"] == "max"


def test_measure_no_new_secret_and_every_action_is_sha_pinned():
    text = MEASURE_YML.read_text(encoding="utf-8")
    assert set(re.findall(r"secrets\.(\w+)", text)) == {"GROQ_API_KEY"}
    for uses in re.findall(r"uses:\s*(\S+)", text):
        assert re.fullmatch(r"[\w./-]+@[0-9a-f]{40}", uses), uses


def test_golden_branch_skips_the_clone_and_materializes():
    clone = _measure_step("Check out target repo")
    assert clone["if"] == "${{ !startsWith(inputs.unit_repo, 'golden/') }}"
    golden = _measure_step("Materialize golden unit")
    assert golden["if"] == "${{ startsWith(inputs.unit_repo, 'golden/') }}"
    assert golden["id"] == "golden"
    assert golden["env"]["UNIT_REPO"] == "${{ inputs.unit_repo }}"
    run = golden["run"]
    assert "from conformance.kit import materialize_golden" in run
    assert 'materialize_golden(app, Path("/tmp/target-repo"))' in run
    for name in ("base", "bug", "fix", "clean"):
        assert f'"{name}"' in run


def test_measure_step_passes_unit_repo_and_golden_shas_via_env():
    measure = _measure_step("Run measured unit")
    env = measure["env"]
    assert env["UNIT_REPO"] == "${{ inputs.unit_repo }}"
    for env_name, sha_name in (
        ("BASE", "base"),
        ("BUG", "bug"),
        ("FIX", "fix"),
        ("CLEAN", "clean"),
    ):
        assert env[env_name] == (f"${{{{ steps.golden.outputs.{sha_name} || inputs.{sha_name} }}}}")
    run = measure["run"]
    assert 'if [ -n "$UNIT_REPO" ]; then' in run
    assert 'UNIT_ARGS="--unit-repo $UNIT_REPO"' in run
    assert 'UNIT_ARGS=""' in run
    assert run.count("$UNIT_ARGS") == 2  # expanded in both invocations


def test_real_repo_path_commands_are_unchanged():
    # With unit_repo empty the golden branch is not taken: the clone
    # step's command and the measure invocations' argument lines are
    # exactly the pre-D154 ones (UNIT_ARGS expands to nothing).
    clone = _measure_step("Check out target repo")
    assert clone["env"]["TARGET_REPO"] == "${{ inputs.target_repo }}"
    assert 'git clone "$TARGET_REPO" /tmp/target-repo' in clone["run"]
    run = _measure_step("Run measured unit")["run"]
    for line in (
        "--repo /tmp/target-repo",
        '--bug-id "$BUG_ID"',
        '--base "$BASE"',
        '--bug "$BUG"',
        '--fix "$FIX"',
        '--clean-id "$CLEAN_ID"',
        '--clean "$CLEAN"',
    ):
        assert line in run, line


# ----------------------------------------------------------------------
# D203: measure.yml's public_env_keys input — workflow shape.
#
# The blind run on paperless stopped pre-measurement: the bundle
# leak check found two contract env VALUES inside the app's own
# pyproject.toml — Django backend class paths the contract copies
# from the app's own test config. The values are benign public
# config, but the run had no way to say so: --public-env-key
# existed in the CLI and measure.yml could not pass it. D203 adds
# the input, a fail-closed validation step ahead of the measure
# step, and the flag on both measure invocations. The validator
# itself is unit-tested in test_cli.py; these tests pin the
# workflow's shape only.
# ----------------------------------------------------------------------

VALIDATE_STEP = "Validate public env keys (D203, fail closed)"


def test_public_env_keys_input_shape():
    data = _measure_workflow()
    on = data.get("on", data.get(True))
    keys = on["workflow_dispatch"]["inputs"]["public_env_keys"]
    assert keys["type"] == "string"
    assert keys["required"] is False
    assert keys["default"] == ""


def test_public_env_keys_validation_step_runs_before_measure():
    names = [step.get("name") for step in _measure_steps()]
    assert names.index(VALIDATE_STEP) < names.index("Run measured unit")
    step = _measure_step(VALIDATE_STEP)
    # Skipped entirely when the input is empty: behaviour is then
    # exactly as before D203.
    assert step["if"] == "${{ inputs.public_env_keys != '' }}"
    env = step["env"]
    assert env["PUBLIC_ENV_KEYS"] == "${{ inputs.public_env_keys }}"
    assert env["UNIT_REPO"] == "${{ inputs.unit_repo }}"
    assert env["TARGET_REPO"] == "${{ inputs.target_repo }}"
    run = step["run"]
    assert "from proofdeploy.cli import" in run
    assert "contract_bootstrap_env_names" in run
    assert "validate_public_env_keys" in run
    # An unresolvable contract fails the job, never waves keys past.
    assert "sys.exit(1)" in run


def test_measure_step_passes_public_env_keys_on_both_invocations():
    measure = _measure_step("Run measured unit")
    assert measure["env"]["PUBLIC_ENV_KEYS"] == "${{ inputs.public_env_keys }}"
    run = measure["run"]
    assert 'PUBLIC_ENV_ARGS=""' in run
    assert 'PUBLIC_ENV_ARGS="$PUBLIC_ENV_ARGS --public-env-key $key"' in run
    assert "for key in $PUBLIC_ENV_KEYS; do" in run
    # Expanded in BOTH measure invocations (bug and clean).
    assert run.count("$PUBLIC_ENV_ARGS \\") == 2


# ----------------------------------------------------------------------
# D222: measure.yml's dry_run input — workflow shape.
#
# The provision rehearsal (--dry-run-provision) is dispatched
# through measure.yml so it runs under the same gates as a measured
# run: the measurement environment (Master's approval) and the
# proofdeploy-model-calls serialization group. The dry_run choice
# input (none / provision) reaches the measure step via env only,
# like every other input, and expands to --dry-run-provision on
# both measure invocations; "none" leaves the command line a plain
# measured run.
# ----------------------------------------------------------------------


def test_dry_run_input_shape():
    data = _measure_workflow()
    on = data.get("on", data.get(True))
    dry_run = on["workflow_dispatch"]["inputs"]["dry_run"]
    assert dry_run["type"] == "choice"
    assert dry_run["options"] == ["none", "provision"]
    assert dry_run["required"] is False
    assert dry_run["default"] == "none"


def test_measure_step_passes_dry_run_on_both_invocations():
    measure = _measure_step("Run measured unit")
    assert measure["env"]["DRY_RUN"] == "${{ inputs.dry_run }}"
    run = measure["run"]
    assert 'if [ "$DRY_RUN" = "provision" ]; then' in run
    assert 'DRY_RUN_ARGS="--dry-run-provision"' in run
    assert 'DRY_RUN_ARGS=""' in run
    # Expanded in BOTH measure invocations (bug and clean).
    assert run.count("$DRY_RUN_ARGS \\") == 2
