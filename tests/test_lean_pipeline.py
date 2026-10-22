"""Lean P4 (D258): the end-to-end lean verdict, in host mode.

The golden Flask app runs on this machine (``--no-sandbox``: marked, never a
measured result) under pdcov, in the CI-like environment (coverage env vars
set by pytest-cov must not reach the app). The judge is a STUB runner that
answers from the prompt; no real model call is made anywhere, and the model
key is never read.

Covers: the golden bug (FAIL via a judged regression) and the clean change
(PASS), a planted noise field (classified NOISE, never a Difference), a
planted 500 (FAIL through the hard oracle with zero model calls), a
comment-only change, changed code that no request reaches (never PASS), the
unit-level comparison rules, and the sandbox plumbing that does not need
podman (the Sandbox subclass that stages and activates pdcov).
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from lean_helpers import FACT_OPEN, NoModel, PromptStub, bug_rule, clean_rule

from conformance import kit
from proofdeploy.cli import build_parser, main
from proofdeploy.lean import hostrun
from proofdeploy.lean import run as lean_run
from proofdeploy.lean.compare import (
    RunData,
    compare_runs,
    find_hard_failures,
    traceback_signatures,
)
from proofdeploy.lean.http import Exchange, Scenario, Step
from proofdeploy.lean.verdict import FAIL, INCONCLUSIVE, PASS, LeanRecord, replay
from proofdeploy.model_client import ModelResponse

APP = "python-flask-sqlite"
ROOT = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------
# The golden repo, a shared venv and variant repos
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def golden(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    root = tmp_path_factory.mktemp("lean_p4")
    repo = root / "repo"
    shas = kit.materialize_golden(APP, repo)
    # HEAD ends on the lockmismatch revision (a deliberately wrong hash).
    _git(repo, "checkout", "-q", shas["bug"])
    if not hostrun.pypi_reachable():
        pytest.skip("PyPI unreachable: cannot create the hash-pinned golden venv (no network)")
    py = hostrun.make_venv(root / "venv", repo / "requirements.txt")
    return {"root": root, "repo": repo, "py": py, "shas": shas}


@pytest.fixture()
def ci_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # CI: pytest-cov exports these; the app must not inherit them.
    monkeypatch.setenv("COVERAGE_FILE", str(tmp_path / ".coverage.host"))
    monkeypatch.setenv("COV_CORE_SOURCE", "proofdeploy")
    monkeypatch.setenv("COV_CORE_CONFIG", "x")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def variant_repo(
    tmp: Path, name: str, edit: Callable[[str], str], message: str = "change the note lookup"
) -> tuple[Path, str, str]:
    """A repo with the golden base commit and one commit that edits app.py."""
    repo = tmp / name
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "user.email", "t@invalid")
    src = kit.GOLDEN_DIR / APP / "revisions" / "base"
    for f in src.iterdir():
        shutil.copyfile(f, repo / f.name)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    app = repo / "app.py"
    app.write_text(edit(app.read_text(encoding="utf-8")), encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return repo, base, _git(repo, "rev-parse", "HEAD")


def run_cli(
    golden: dict[str, Any],
    repo: Path,
    base: str,
    head: str,
    out: Path,
    runner: Callable[[str], ModelResponse] | None,
    *extra: str,
) -> tuple[int, dict[str, Any]]:
    args = build_parser().parse_args(
        [
            "lean-verify",
            "--repo",
            str(repo),
            "--base",
            base,
            "--head",
            head,
            "--no-sandbox",
            "--python",
            str(golden["py"]),
            "--output-dir",
            str(out),
            *extra,
        ]
    )
    code = lean_run.cmd_lean_verify(args, model_runner=runner)
    if code == 5:
        pytest.fail("lean-verify hit an internal error (exit 5); the traceback is in stderr")
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    return code, manifest


def run_golden(
    golden: dict[str, Any],
    a: str,
    b: str,
    out: Path,
    runner: Callable[[str], ModelResponse] | None,
    *extra: str,
) -> tuple[int, dict[str, Any]]:
    shas = golden["shas"]
    return run_cli(golden, golden["repo"], shas[a], shas[b], out, runner, *extra)


# --------------------------------------------------------------------------
# Golden end to end
# --------------------------------------------------------------------------


def test_golden_bug_is_a_judged_regression_and_fails(
    golden: dict[str, Any], ci_env: None, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    stub = PromptStub(bug_rule)
    code, m = run_golden(golden, "base", "bug", tmp_path / "out", stub)
    out = capsys.readouterr().out
    assert code == 1, out
    assert m["verdict"]["status"] == FAIL and m["verdict"]["cause"] == "judged_regression"
    record = LeanRecord.from_dict(
        json.loads((tmp_path / "out" / "lean-record.json").read_text(encoding="utf-8"))
    )
    diffs = record.inputs["differences"]
    done = [d for d in diffs if d["field_path"] == "done" and d["path"].startswith("/notes/")]
    assert any(d["before"] is False and d["after"] is True for d in done), done
    assert all(d["method"] == "GET" for d in done)
    regs = record.verdict["details"]["regressions"]
    assert any(r["quote"] == FACT_OPEN and r["after"] is True for r in regs)
    assert replay(record).status == FAIL  # the stored outputs re-derive the verdict
    # fresh database per run: nothing is noise on the golden app
    assert m["noise_fields"] == [], m["noise"]
    assert m["runs"]["base"] == m["runs"]["base2"] and m["runs"]["head"] == m["runs"]["head2"]
    assert m["runs"]["base"]["requests"] == m["runs"]["head"]["requests"] > 0
    assert m["sandboxed"] is False and m["measured_result"] is False
    assert re.fullmatch(r"[0-9a-f]{64}", m["sides"]["head"]["db_snapshot_sha256"])
    assert m["observable"] is True and m["unexercised_lines"] == []
    assert m["impacted_operations_head"]["GET /notes/{note_id}"]["2xx_rate"] == 1.0
    assert m["coverage"]["head"]["covered"] is True
    assert m["mapped_by"] == {"head": "flask", "base": "flask"}
    for side in ("head", "base"):
        steps = [(a["step"], a["outcome"]) for a in m["mapping_attempts"][side]]
        assert steps[-1] == ("flask", "mapped") and steps[0][0] == "openapi", steps
    for side in ("head", "base"):
        acc = m["request_acceptance"][side]
        assert acc["requests"] == m["cover_loop"][side]["requests"] > 0
        assert acc["non_4xx"] == acc["requests"] - m["cover_loop"][side]["4xx"]
        assert acc["rate"] == round(acc["non_4xx"] / acc["requests"], 3)
    for stage in ("diff", "index_head", "cover_head", "run_base", "run_base2", "run_head2"):
        assert stage in m["timings_seconds"], m["timings_seconds"]
    assert "verdict: FAIL (judged_regression)" in out
    assert stub.prompts and FACT_OPEN in stub.prompts[0]


def test_golden_seeded_reads_are_identical_across_the_four_runs(
    golden: dict[str, Any], ci_env: None, tmp_path: Path
) -> None:
    """Seeded records come first (R2): the same seeded note is read in all four runs.

    Fresh database per run: the seed made notes 1 and 2, so ``GET /notes/1`` answers
    the same body in every run and no producer ``POST /notes`` is needed.
    """
    stub = PromptStub(bug_rule)
    run_golden(golden, "base", "bug", tmp_path / "out", stub)
    runs = json.loads((tmp_path / "out" / "exchanges.json").read_text(encoding="utf-8"))
    read = {
        label: [
            e["request"]["url_path"]
            for e in exs
            if e["request"]["method"] == "GET" and e["request"]["url_path"].startswith("/notes/")
        ]
        for label, exs in runs.items()
    }
    assert all(v == read["base"] for v in read.values()), read
    assert read["base"] and read["base"][0] == "/notes/1"  # seed made notes 1 and 2
    assert not any(
        e["request"]["method"] == "POST" and e["request"]["url_path"] == "/notes"
        for exs in runs.values()
        for e in exs
    )


def test_golden_clean_change_passes_with_the_added_tag_judged_correct(
    golden: dict[str, Any], ci_env: None, tmp_path: Path
) -> None:
    stub = PromptStub(clean_rule)
    code, m = run_golden(golden, "intermediate", "clean", tmp_path / "out", stub)
    assert code == 0, m["verdict"]
    assert m["verdict"]["status"] == PASS
    record = json.loads((tmp_path / "out" / "lean-record.json").read_text(encoding="utf-8"))
    kinds = {(d["field_path"], d["kind"]) for d in record["inputs"]["differences"]}
    assert ("tag", "added_field") in kinds
    tag = next(g for g in record["groups"] if g["group"]["field_path"] == "tag")
    assert tag["judgment"]["verdict"] == "correct"
    assert tag["judgment"]["evidence_source"] == "intent"
    assert m["judge"]["calls"] >= 1 and m["judge"]["total_tokens"] > 0


def test_planted_noise_field_is_classified_noise_not_a_difference(
    golden: dict[str, Any], ci_env: None, tmp_path: Path
) -> None:
    def edit(src: str) -> str:
        src = src.replace("import secrets\n", "import secrets\nimport time\n")
        return src.replace(
            "        \"done\": row['done'] == 1,\n",
            '        "done": row[\'done\'] == 1,\n        "ts": time.time(),\n',
        )

    repo, base, head = variant_repo(tmp_path, "noise", edit, "stamp each note with the time")
    judge = NoModel()
    code, m = run_cli(golden, repo, base, head, tmp_path / "out", judge)
    assert code == 0, m["verdict"]
    assert m["verdict"]["status"] == PASS
    assert "ts" in m["noise_fields"], m["noise"]
    assert m["compared"]["differences"] == 0
    assert judge.calls == 0 and m["judge"]["calls"] == 0  # no difference: no model, no key
    record = json.loads((tmp_path / "out" / "lean-record.json").read_text(encoding="utf-8"))
    assert record["inputs"]["differences"] == []


def test_planted_500_on_head_fails_by_hard_oracle_with_zero_model_calls(
    golden: dict[str, Any], ci_env: None, tmp_path: Path
) -> None:
    def edit(src: str) -> str:
        return src.replace(
            "\"done\": row['done'] == 1,", "\"done\": 1 // (row['done'] - row['done']),"
        )

    repo, base, head = variant_repo(tmp_path, "boom", edit, "compute the done flag")
    judge = NoModel()
    code, m = run_cli(golden, repo, base, head, tmp_path / "out", judge)
    assert code == 1
    assert m["verdict"]["status"] == FAIL and m["verdict"]["cause"] == "hard_oracle"
    kinds = {h["kind"] for h in m["hard_failures"]}
    assert "5xx" in kinds and "new_exception" in kinds, m["hard_failures"]
    assert any("ZeroDivisionError" in h["detail"] for h in m["hard_failures"])
    assert judge.calls == 0 and m["judge"]["calls"] == 0


def _inject_500_in(labels: set[str]) -> Any:
    """Wrap ``replay_run`` so that the named runs see a 500 on their last exchange."""
    from proofdeploy.lean import pipeline

    real = pipeline.replay_run

    def wrapper(driver: Any, label: str, scenarios: Any, auth: Any) -> Any:
        data = real(driver, label, scenarios, auth)
        if label in labels:
            ex = data.exchanges[data.order[-1]]
            ex.status = 500
            ex.body_json_or_text = "Internal Server Error"
        return data

    return mock.patch.object(pipeline, "replay_run", wrapper)


def test_head_500_in_one_run_only_is_never_a_pass_when_the_third_run_is_clean(
    golden: dict[str, Any], ci_env: None, tmp_path: Path
) -> None:
    with _inject_500_in({"head"}):
        code, m = run_golden(
            golden, "intermediate", "clean", tmp_path / "out", PromptStub(clean_rule)
        )
    assert m["verdict"]["status"] == INCONCLUSIVE and code == 2, m["verdict"]
    assert m["verdict"]["cause"] == "flaky_hard_failure"
    assert [h["kind"] for h in m["flaky_hard_failures"]] == ["5xx"]
    assert m["hard_failures"] == [] and "head3" in m["runs"]  # the third run happened


def test_head_500_in_one_run_that_reproduces_on_the_third_run_fails_by_hard_oracle(
    golden: dict[str, Any], ci_env: None, tmp_path: Path
) -> None:
    judge = NoModel()
    with _inject_500_in({"head", "head3"}):
        code, m = run_golden(golden, "intermediate", "clean", tmp_path / "out", judge)
    assert code == 1 and m["verdict"]["status"] == FAIL, m["verdict"]
    assert m["verdict"]["cause"] == "hard_oracle"
    assert [h["kind"] for h in m["hard_failures"]] == ["5xx"]
    assert "reproduced in 2 of 3 head runs" in m["hard_failures"][0]["detail"]
    assert m["flaky_hard_failures"] == [] and judge.calls == 0


def test_a_clean_run_never_starts_a_third_head_run(
    golden: dict[str, Any], ci_env: None, tmp_path: Path
) -> None:
    code, m = run_golden(golden, "intermediate", "clean", tmp_path / "out", PromptStub(clean_rule))
    assert code == 0 and "head3" not in m["runs"] and m["flaky_hard_failures"] == []


def test_comment_only_change_has_no_executable_lines_and_passes_without_running(
    golden: dict[str, Any], ci_env: None, tmp_path: Path
) -> None:
    repo, base, head = variant_repo(
        tmp_path,
        "comment",
        lambda s: s.replace("def get_note(note_id):", "# look the note up\ndef get_note(note_id):"),
        "document the lookup",
    )
    judge = NoModel()
    code, m = run_cli(golden, repo, base, head, tmp_path / "out", judge)
    # P5 decide: nothing executable changed, nothing unexercised, no difference -> PASS.
    assert m["executable_changed_lines"] == 0
    assert code == 0 and m["verdict"] == {
        "status": PASS,
        "cause": "no_regression_found",
        "details": {},
    }
    assert m["pass_basis"] == "no executable changed lines"
    assert m["impact_scenarios"] == [] and all(r["requests"] == 0 for r in m["runs"].values())
    assert judge.calls == 0


def test_changed_code_no_request_reaches_is_never_a_pass(
    golden: dict[str, Any], ci_env: None, tmp_path: Path
) -> None:
    dead = "\n\ndef never_called():\n    return 41 + 1\n"
    repo, base, head = variant_repo(
        tmp_path,
        "dead",
        lambda s: s.replace("\n\nif __name__", dead + "\n\nif __name__"),
        "add a helper",
    )
    code, m = run_cli(golden, repo, base, head, tmp_path / "out", NoModel())
    assert m["verdict"]["status"] == INCONCLUSIVE
    assert m["verdict"]["cause"] == "not_observable_over_http"  # nothing changed was reached
    assert code == 2
    assert m["observable"] is False
    assert any(line.endswith("(added)") for line in m["unexercised_lines"])


def test_partly_exercised_change_is_inconclusive_change_not_exercised(
    golden: dict[str, Any], ci_env: None, tmp_path: Path
) -> None:
    def edit(src: str) -> str:
        src = src.replace(
            "\n\nif __name__", "\n\ndef never_called():\n    return 41 + 1\n\n\nif __name__"
        )
        return src.replace(
            "\"done\": row['done'] == 1,", '"done": row[\'done\'] == 1,\n        "tag": "general",'
        )

    repo, base, head = variant_repo(tmp_path, "part", edit, "add a tag field to note responses")
    stub = PromptStub(clean_rule)
    code, m = run_cli(golden, repo, base, head, tmp_path / "out", stub)
    assert (
        m["verdict"]["status"] == INCONCLUSIVE and m["verdict"]["cause"] == "change_not_exercised"
    )
    assert code == 2 and m["observable"] is True
    assert m["unexercised_lines"], "the dead helper's body was never run"


def test_a_declaration_that_only_ran_at_load_does_not_make_a_change_observable(
    golden: dict[str, Any], ci_env: None, tmp_path: Path
) -> None:
    """A signature change runs once, at import: nothing a request ran changed, so never a pass."""
    repo, base, head = variant_repo(
        tmp_path,
        "sig",
        lambda s: s.replace("def get_note(note_id):", "def get_note(note_id, extra=None):"),
        "accept an extra argument",
    )
    code, m = run_cli(golden, repo, base, head, tmp_path / "out", NoModel())
    assert code == 2 and m["verdict"]["cause"] == "not_observable_over_http"
    assert m["coverage"]["head"]["load_only_declarations"] == ["app.py:88"]
    assert m["unexercised_lines"] == []


def test_declarations_are_recognised_by_their_text() -> None:
    from proofdeploy.lean.pipeline import is_structural

    for text in ("import time", "from a import b", "@app.get('/x')", "def f(x):", "async def f():",
                 "class A:", "    def method(self):"):  # fmt: skip
        assert is_structural(text), text
    for text in ("x = 1", "return f(x)", "LIMIT = 10", "importance = 3", "if x:", None):
        assert not is_structural(text), text


# --------------------------------------------------------------------------
# CLI exit codes
# --------------------------------------------------------------------------


def test_usage_errors_exit_4(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert (
        main(
            [
                "lean-verify",
                "--repo",
                str(tmp_path),
                "--base",
                "a",
                "--head",
                "b",
                "--output-dir",
                str(tmp_path / "o"),
            ]
        )
        == 4
    )
    assert "not a git checkout" in capsys.readouterr().err
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q")
    assert (
        main(
            [
                "lean-verify",
                "--repo",
                str(repo),
                "--base",
                "nope",
                "--head",
                "HEAD",
                "--output-dir",
                str(tmp_path / "o"),
            ]
        )
        == 4
    )
    assert (
        main(
            [
                "lean-verify",
                "--repo",
                str(repo),
                "--base",
                "a",
                "--head",
                "b",
                "--contract",
                "c.yml",
                "--unit-repo",
                "o/r",
                "--output-dir",
                str(tmp_path / "o"),
            ]
        )
        == 4
    )
    assert (
        main(
            [
                "lean-verify",
                "--repo",
                str(repo),
                "--base",
                "a",
                "--head",
                "b",
                "--django-overlay",
                "NOEQUALS",
                "--output-dir",
                str(tmp_path / "o"),
            ]
        )
        == 4
    )


def test_no_database_declared_is_a_typed_refusal_exit_3(
    golden: dict[str, Any], ci_env: None, tmp_path: Path
) -> None:
    contract = tmp_path / "nodb.yml"
    text = (kit.GOLDEN_DIR / APP / "revisions" / "base" / "proofdeploy.yml").read_text("utf-8")
    contract.write_text(re.sub(r"database:\n  kind: sqlite\n  path: ./app.db\n", "", text))
    code, m = run_golden(
        golden, "base", "bug", tmp_path / "out", NoModel(), "--contract", str(contract)
    )
    assert code == 3
    assert m["verdict"]["status"] == "NOT_SUPPORTED"
    assert m["verdict"]["cause"] == "db_snapshot_unsupported"


def test_an_app_that_cannot_be_mapped_is_a_typed_refusal_exit_3(
    golden: dict[str, Any], ci_env: None, tmp_path: Path
) -> None:
    code, m = run_golden(
        golden, "base", "bug", tmp_path / "out", NoModel(), "--flask-target", "no_such_module"
    )
    assert code == 3 and m["verdict"]["cause"] == "cannot_map"
    steps = {a["step"]: a for a in m["mapping_attempts"]}
    assert list(steps) == ["openapi", "flask", "django"]
    # the Django dump ran in the golden (Flask-only) interpreter and failed: the reason is kept
    assert steps["django"]["outcome"].startswith("exit 1: "), steps["django"]
    assert "django: exit 1" in m["verdict"]["details"]["message"]


# --------------------------------------------------------------------------
# Comparison rules (pure)
# --------------------------------------------------------------------------


def _scenario(*, capture: bool = False) -> Scenario:
    steps = [Step("POST", "/notes", capture={"note_id": "id"} if capture else {}, auth=True)]
    steps.append(Step("GET", "/notes/{note_id}", path_params={"note_id": "${note_id}"}))
    return Scenario(steps, "GET /notes/{note_id}")


def _ex(
    sc: Scenario,
    step: int,
    status: int,
    body: Any,
    *,
    url: str | None = None,
    ct: str = "application/json",
) -> Exchange:
    method = sc.steps[step].method
    path = url or sc.steps[step].path
    return Exchange(
        sc.id,
        step,
        {"method": method, "url_path": path, "body": None},
        status,
        {"content-type": ct},
        body,
        1.0,
    )


def _runs(
    sc: Scenario, per_run: list[list[tuple[int, Any]]], logs: list[str] | None = None
) -> list[RunData]:
    out = []
    for i, (label, rows) in enumerate(
        zip(("base", "base2", "head", "head2"), per_run, strict=True)
    ):
        r = RunData(label)
        r.add(sc, [_ex(sc, k, s, b) for k, (s, b) in enumerate(rows)])
        r.server_log = (logs or ["", "", "", ""])[i]
        out.append(r)
    return out


def test_noise_is_base_vs_base2_or_head_vs_head2_and_real_is_the_rest() -> None:
    sc = _scenario()
    created = (201, {"id": 3})
    runs = _runs(
        sc,
        [
            [created, (200, {"id": 3, "done": False, "ts": 1, "v": 1})],
            [created, (200, {"id": 3, "done": False, "ts": 2, "v": 1})],
            [created, (200, {"id": 3, "done": True, "ts": 3, "v": 2, "tag": "x"})],
            [created, (200, {"id": 3, "done": True, "ts": 4, "v": 2, "tag": "x"})],
        ],
    )
    cmp = compare_runs(*runs)
    got = {(d.field_path, d.kind, d.before, d.after) for d in cmp.differences}
    assert got == {
        ("done", "changed", False, True),
        ("v", "changed", 1, 2),
        ("tag", "added_field", None, "x"),
    }
    assert cmp.noise_fields() == ["ts"]


def test_a_field_noisy_on_one_side_only_is_noise_not_a_difference() -> None:
    sc = _scenario()
    runs = _runs(
        sc,
        [
            [(201, {"id": 3}), (200, {"n": 1})],
            [(201, {"id": 3}), (200, {"n": 1})],
            [(201, {"id": 3}), (200, {"n": 5})],
            [(201, {"id": 3}), (200, {"n": 6})],
        ],
    )
    cmp = compare_runs(*runs)
    assert cmp.differences == [] and cmp.noise_fields() == ["n"]


def test_status_change_and_status_noise() -> None:
    sc = _scenario()
    changed = compare_runs(
        *_runs(sc, [[(201, {}), (200, {"a": 1})]] * 2 + [[(201, {}), (404, {"error": "x"})]] * 2)
    )
    assert [(d.field_path, d.kind, d.before, d.after) for d in changed.differences] == [
        ("$status", "status_changed", 200, 404)
    ]
    flaky = compare_runs(
        *_runs(
            sc,
            [
                [(201, {}), (200, {})],
                [(201, {}), (200, {})],
                [(201, {}), (200, {})],
                [(201, {}), (503, {})],
            ],
        )
    )
    assert flaky.differences == [] and flaky.noise_fields() == ["$status"]


def test_lists_are_order_insensitive_only_where_the_order_is_unstable() -> None:
    sc = _scenario()
    a, b = {"notes": [{"id": 1}, {"id": 2}]}, {"notes": [{"id": 2}, {"id": 1}]}
    unstable = compare_runs(*_runs(sc, [[(201, {}), (200, a)], [(201, {}), (200, b)]] * 2))
    assert unstable.differences == [] and unstable.noise == []
    # order differs base vs head only (stable inside each side): a real difference
    stable = compare_runs(*_runs(sc, [[(201, {}), (200, a)]] * 2 + [[(201, {}), (200, b)]] * 2))
    assert stable.differences and all(d.kind == "changed" for d in stable.differences)
    assert {d.field_path for d in stable.differences} == {"notes[].id"}


def test_chained_ids_are_normalized_by_position() -> None:
    sc = _scenario(capture=True)
    # each run's database handed out a different id; the GET echoes the id it was given
    runs = _runs(
        sc,
        [
            [(201, {"id": 3}), (200, {"id": 3, "done": False})],
            [(201, {"id": 7}), (200, {"id": 7, "done": False})],
            [(201, {"id": 4}), (200, {"id": 4, "done": False})],
            [(201, {"id": 9}), (200, {"id": 9, "done": False})],
        ],
    )
    cmp = compare_runs(*runs)
    # step 0's own id differs inside each side (noise); step 1 is the same by position
    assert cmp.differences == []
    assert all(n.request_id.endswith(":0") for n in cmp.noise)
    # a head that returns a DIFFERENT id than the one it created is a real difference
    wrong = _runs(
        sc,
        [[(201, {"id": 3}), (200, {"id": 3})]] * 2 + [[(201, {"id": 3}), (200, {"id": 99})]] * 2,
    )
    d = compare_runs(*wrong).differences
    assert [(x.field_path, x.before, x.after) for x in d] == [("id", 3, 99)]


def test_content_type_is_compared() -> None:
    sc = _scenario()

    base = _runs(sc, [[(200, {"a": 1})]] * 2 + [[(200, {"a": 1})]] * 2)
    for r in base[2:]:
        for ex in r.exchanges.values():
            ex.headers_subset["content-type"] = "text/html; charset=utf-8"
    d = compare_runs(*base).differences
    assert [(x.field_path, x.before, x.after) for x in d] == [
        ("$header.content-type", "application/json", "text/html")
    ]


def test_hard_failures_need_head_only_and_must_reproduce() -> None:
    sc = _scenario()
    ok = [(201, {}), (200, {"a": 1})]
    boom = [(201, {}), (500, "Internal Server Error")]
    stable = _runs(sc, [ok, ok, boom, boom])
    hard, flaky = find_hard_failures(*stable)
    assert [h.kind for h in hard] == ["5xx"] and flaky == []
    once = _runs(sc, [ok, ok, boom, ok])
    hard, flaky = find_hard_failures(*once)
    assert hard == [] and [(h.kind, h.request_id) for h in flaky] == [("5xx", f"{sc.id}:1")]
    # the 5xx in the SECOND head run is the same signal
    assert [h.kind for h in find_hard_failures(*_runs(sc, [ok, ok, ok, boom]))[1]] == ["5xx"]
    both = _runs(sc, [boom, ok, boom, boom])  # base also 5xx once: base is not clean
    assert find_hard_failures(*both) == ([], [])
    # base flaky too (500/200 on base, 500/200 on head): genuine noise, neither list
    assert find_hard_failures(*_runs(sc, [boom, ok, boom, ok])) == ([], [])
    assert find_hard_failures(*_runs(sc, [boom, ok, ok, boom])) == ([], [])
    # JSON that stops parsing
    text = _runs(sc, [ok, ok, [(201, {}), (200, "<html>oops")], [(201, {}), (200, "<html>oops")]])
    assert [h.kind for h in find_hard_failures(*text)[0]] == ["schema_violation"]
    # a transport error on head only
    dead = (0, "transport error: ConnectionRefusedError: [Errno 111]")
    crashed = _runs(sc, [ok, ok, [(201, {}), dead], [(201, {}), dead]])
    assert [h.kind for h in find_hard_failures(*crashed)[0]] == ["crash"]
    slow = (0, "transport error: TimeoutError: timed out")
    assert [
        h.kind
        for h in find_hard_failures(*_runs(sc, [ok, ok, [(201, {}), slow], [(201, {}), slow]]))[0]
    ] == ["timeout"]


def test_new_tracebacks_in_head_logs_are_hard_failures() -> None:
    sc = _scenario()
    ok = [(201, {}), (200, {})]
    tb = (
        "127.0.0.1 - GET /x\nTraceback (most recent call last):\n"
        '  File "app.py", line 3, in f\n    1/0\nZeroDivisionError: division by zero\n'
    )
    old = tb.replace("ZeroDivisionError: division by zero", "KeyError: 'a'")
    assert traceback_signatures(tb) == ["ZeroDivisionError: division by zero"]
    hard, _ = find_hard_failures(*_runs(sc, [ok] * 4, ["", "", tb, tb]))
    assert [(h.kind, h.request_id) for h in hard] == [("new_exception", "server_log")]
    # the same traceback on base is not new
    assert find_hard_failures(*_runs(sc, [ok] * 4, [old, "", old, old]))[0] == []
    # only in one head run: a head-only one-off, kept apart from noise
    hard, flaky = find_hard_failures(*_runs(sc, [ok] * 4, ["", "", tb, ""]))
    assert hard == [] and [(h.request_id, h.kind) for h in flaky] == [
        ("server_log", "new_exception")
    ]
    assert "ZeroDivisionError" in flaky[0].detail
    # base flaky too (the traceback appears once on base): not new, so not a signal at all
    assert find_hard_failures(*_runs(sc, [ok] * 4, [tb, "", tb, ""])) == ([], [])


def test_transport_error_in_one_head_run_only_is_a_flaky_hard_failure() -> None:
    sc = _scenario()
    ok = [(201, {}), (200, {})]
    dead = [(201, {}), (0, "transport error: ConnectionRefusedError: [Errno 111]")]
    hard, flaky = find_hard_failures(*_runs(sc, [ok, ok, dead, ok]))
    assert hard == [] and [h.kind for h in flaky] == ["crash"]
    assert find_hard_failures(*_runs(sc, [dead, ok, dead, ok])) == ([], [])  # base not clean


def _diff_free_ctx() -> Any:
    from proofdeploy.lean.judge import JudgeContext

    return JudgeContext(intent="", changed_hunks="", seed_facts=[], field_descriptions={})


def test_a_flaky_hard_failure_is_never_a_pass_but_a_real_failure_still_fails() -> None:
    from proofdeploy.lean.judge import HardFailure
    from proofdeploy.lean.verdict import decide, run_lean

    flaky = [HardFailure("s:1", "5xx", "head answered HTTP 500 in only one of two runs")]
    v = decide([], [], [], True, flaky)
    assert (v.status, v.cause) == (INCONCLUSIVE, "flaky_hard_failure")
    assert v.details["flaky_hard_failures"][0]["request_id"] == "s:1"
    # no flaky failure: the old PASS stays
    assert decide([], [], [], True, []).status == PASS
    assert decide([], [], [], True).status == PASS
    # a stable hard failure outranks it
    hard = [HardFailure("s:1", "5xx", "x")]
    assert decide(hard, [], [], True, flaky).cause == "hard_oracle"
    # through run_lean and replay: recorded, replays to the same verdict, zero model calls
    model = NoModel()
    rec = run_lean([], [], _diff_free_ctx(), [], True, runner=model, flaky_hard_failures=flaky)
    assert (rec.verdict["status"], rec.verdict["cause"]) == (INCONCLUSIVE, "flaky_hard_failure")
    assert rec.inputs["flaky_hard_failures"][0]["kind"] == "5xx"
    assert replay(rec).cause == "flaky_hard_failure" and model.calls == 0


# --------------------------------------------------------------------------
# Plumbing: database restore, overlay, clock, the pdcov-aware Sandbox
# --------------------------------------------------------------------------


def test_restore_sqlite_replaces_the_file_and_drops_sidecars(tmp_path: Path) -> None:
    db = tmp_path / "app.db"
    for path, rows in ((tmp_path / "seed.sqlite", 1), (db, 5)):
        con = sqlite3.connect(path)
        con.execute("create table t (x)")
        con.executemany("insert into t values (?)", [(i,) for i in range(rows)])
        con.commit()
        con.close()
    (tmp_path / "app.db-wal").write_text("stale")
    lean_run.restore_sqlite(tmp_path / "seed.sqlite", db)
    assert not (tmp_path / "app.db-wal").exists()
    con = sqlite3.connect(db)
    assert con.execute("select count(*) from t").fetchone() == (1,)
    con.close()
    assert db.stat().st_mode & 0o666 == 0o666  # the app user (another uid) must write it
    snap = tmp_path / "snap.sqlite"
    lean_run.snapshot_sqlite(db, snap)
    assert sqlite3.connect(snap).execute("select count(*) from t").fetchone() == (1,)


def test_django_overlay_flags_and_env(tmp_path: Path) -> None:
    got = lean_run.parse_overlay_items(["CELERY_TASK_ALWAYS_EAGER=True", "RATE=3", "NAME=abc"])
    assert got == {"CELERY_TASK_ALWAYS_EAGER": True, "RATE": 3, "NAME": "abc"}
    env = lean_run.overlay_env(
        {"DJANGO_SETTINGS_MODULE": "proj.settings"}, got, tmp_path / "ov", "/pd-home/ov"
    )
    assert env["DJANGO_SETTINGS_MODULE"] == "pd_settings_overlay"
    assert env["PYTHONPATH"] == "/pd-home/ov"
    text = (tmp_path / "ov" / "pd_settings_overlay.py").read_text(encoding="utf-8")
    assert "from proj.settings import *" in text and "CELERY_TASK_ALWAYS_EAGER = True" in text
    # not a Django app (or no overrides): nothing is applied
    assert lean_run.overlay_env({}, got, tmp_path / "ov2", "/x") == {}
    assert lean_run.overlay_env({"DJANGO_SETTINGS_MODULE": "p.s"}, {}, tmp_path / "ov3", "/x") == {}
    with pytest.raises(lean_run.LeanUsageError):
        lean_run.parse_overlay_items(["NOEQUALS"])


def test_clock_is_frozen_only_when_faketime_exists(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    cmd, info = lean_run.wrap_clock_cmd("python app.py")
    assert cmd == "python app.py" and info["used"] is False
    fake = tmp_path / "faketime"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)
    cmd, info = lean_run.wrap_clock_cmd("python app.py")
    assert info["used"] is True and "faketime" in cmd and "python app.py" in cmd


def test_lean_sandbox_stages_pdcov_appends_the_step_and_activates_it_for_measured_starts(
    tmp_path: Path,
) -> None:
    from proofdeploy.sandbox import Sandbox, SandboxConfig, sandbox_home_dir

    hook = lean_run.SandboxHook(
        overlay={"CELERY_TASK_ALWAYS_EAGER": True},
        contract_env={"DJANGO_SETTINGS_MODULE": "proj.settings"},
    )
    cls = lean_run.make_lean_sandbox(hook)
    checkout = tmp_path / "snap"
    checkout.mkdir()
    seen: dict[str, Any] = {}

    def fake_install(self, checkout, steps, env, proxy_env, timeout=600):  # type: ignore[no-untyped-def]
        seen["steps"] = steps
        return [{"phase": s["phase"], "exit_code": 0} for s in steps]

    def fake_start(self, checkout, command, env, port, name, *, app_port=None):  # type: ignore[no-untyped-def]
        seen.setdefault("starts", []).append((name, dict(env)))
        return "app"

    with (
        mock.patch.object(Sandbox, "install_steps", fake_install),
        mock.patch.object(Sandbox, "start", fake_start),
    ):
        sb = cls(SandboxConfig(image="img"))
        steps = [{"phase": "install", "command": "true", "cwd": "packages/web"}]
        results = sb.install_steps(checkout, steps, {}, {})
        # pdcov's own step ran last and is not reported to the orchestrator
        assert results == [{"phase": "install", "exit_code": 0}]
        assert [s["phase"] for s in seen["steps"]] == ["install", "pdcov"]
        tool = sandbox_home_dir(checkout) / ".pd-tools" / "pdcov"
        rc = (tool / "coveragerc").read_text(encoding="utf-8")
        assert "data_file = /pd-home/.pd-tools/pdcov/data/.coverage" in rc
        assert "source = /checkout/packages/web" in rc  # the app root as the app sees it
        assert (tool / "pdcov_startup.py").is_file() and (tool / "lib" / "coverage").is_dir()
        assert "/pd-home/.pd-tools/pdcov/lib" in (tool / "pdcov.pth.src").read_text("utf-8")
        assert hook.app_cwd == "packages/web"
        cmd = hook.setup_command()
        assert "/pd-home/.venv/bin/python" in cmd and "pdcov.pth" in cmd and "chmod 0777" in cmd
        # the first start (the one provisioning keeps) is measured, deterministic, overlaid
        sb.start(checkout, "python app.py", {"A": "1"}, 8000, "n1", app_port=None)
        _, env = seen["starts"][0]
        assert env["COVERAGE_PROCESS_START"] == "/pd-home/.pd-tools/pdcov/coveragerc"
        assert env["PYTHONHASHSEED"] == "0" and env["TZ"] == "UTC" and env["A"] == "1"
        assert env["DJANGO_SETTINGS_MODULE"] == "pd_settings_overlay"
        assert env["PYTHONPATH"] == lean_run.CONTAINER_OVERLAY_DIR
        # replays restart the same command UNMEASURED
        hook.restart(False, "n2")
        name, env2 = seen["starts"][1]
        assert name == "n2" and "COVERAGE_PROCESS_START" not in env2
        assert env2["PYTHONHASHSEED"] == "0"
        hook.restart(True, "n3")
        assert "COVERAGE_PROCESS_START" in seen["starts"][2][1]


def test_a_failed_pdcov_step_fails_the_install_and_keeps_its_output(tmp_path: Path) -> None:
    from proofdeploy.sandbox import Sandbox, SandboxConfig

    hook = lean_run.SandboxHook(overlay={}, contract_env={})
    cls = lean_run.make_lean_sandbox(hook)
    checkout = tmp_path / "snap"
    checkout.mkdir()

    def fake_install(self, checkout, steps, env, proxy_env, timeout=600):  # type: ignore[no-untyped-def]
        self.failed_step_output = "pdcov: no app interpreter at /pd-home/.venv/bin/python"
        return [
            *({"phase": s["phase"], "exit_code": 0} for s in steps[:-1]),
            {"phase": "pdcov", "exit_code": lean_run.NOT_PYTHON_EXIT},
        ]

    with mock.patch.object(Sandbox, "install_steps", fake_install):
        sb = cls(SandboxConfig(image="img"))
        results = sb.install_steps(checkout, [{"phase": "install", "command": "npm ci"}], {}, {})
    assert results == [{"phase": "install", "exit_code": lean_run.NOT_PYTHON_EXIT}]
    assert hook.pdcov_rc == lean_run.NOT_PYTHON_EXIT
    assert "no app interpreter" in hook.pdcov_output
    # an earlier failing step: pdcov never ran, the results pass through untouched
    hook2 = lean_run.SandboxHook(overlay={}, contract_env={})
    assert hook2.finish([{"phase": "install", "exit_code": 1}], 2, None) == [
        {"phase": "install", "exit_code": 1}
    ]
    assert hook2.pdcov_rc is None


def test_the_record_never_carries_the_model_key(
    golden: dict[str, Any], ci_env: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "csk-test-key-not-a-real-secret-0123456789"
    monkeypatch.setenv("GROQ_API_KEY", secret)
    stub = PromptStub(clean_rule)
    run_golden(golden, "intermediate", "clean", tmp_path / "out", stub)
    for f in (tmp_path / "out").glob("*.json"):
        assert secret not in f.read_text(encoding="utf-8")


def test_manifest_carries_hard_check_notes_run_windows_and_side_windows(
    golden: dict[str, Any], ci_env: None, tmp_path: Path
) -> None:
    code, m = run_golden(golden, "intermediate", "clean", tmp_path / "out", PromptStub(clean_rule))
    assert code == 0, m["verdict"]
    assert set(m["hard_check_notes"]) == {
        "timeout_seconds",
        "preexisting_failure",
        "excluded_tracebacks",
    }
    assert m["hard_check_notes"]["timeout_seconds"] == 10.0
    assert set(m["run_windows"]) == {"base", "base2", "head", "head2"}
    assert set(m["side_windows"]) == {"base", "head"}
    for side in ("base", "head"):
        lo, hi = m["side_windows"][side]
        assert lo < hi
        # the side's lifetime starts at its provisioning, before its first replay run begins
        assert m["sides"][side]["provisioned_at"] is not None
        assert lo < min(m["run_windows"][r][0] for r in (side, side + "2"))
