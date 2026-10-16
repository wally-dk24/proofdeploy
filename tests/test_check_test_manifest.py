"""The job-budget guard in scripts/check_test_manifest.py must fail closed.

WO-CI-SPEED task 5 capped the two gate workflows at 20 expanded
jobs per push (the GitHub Free plan's concurrent-job cap). The
first counter modelled only the shapes today's workflows use
and silently miscounted the rest: a matrix combining dimensions
with an `include` entry counted 1 where GitHub runs 5, and a
dimension given as a `${{ ... }}` expression counted 1 where
the real number is unknowable statically. A guard that
undercounts gives false comfort, so the counter now counts
exactly the shapes it can model and returns a budget problem —
naming the workflow and the job — for every other shape. These
tests pin both directions for every shape.
"""

import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "check_test_manifest.py"


def _load_manifest_script():
    spec = importlib.util.spec_from_file_location(
        "check_test_manifest", SCRIPT
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


ctm = _load_manifest_script()


def _count(tmp_path: Path, body: str) -> tuple[int, list[str]]:
    path = tmp_path / "gate.yml"
    path.write_text(body, encoding="utf-8")
    return ctm.expanded_job_count(path)


def test_job_without_matrix_counts_once(tmp_path: Path) -> None:
    count, problems = _count(
        tmp_path,
        "jobs:\n"
        "  lint:\n"
        "    runs-on: ubuntu-latest\n"
        "  test:\n"
        "    runs-on: ubuntu-latest\n",
    )
    assert problems == []
    assert count == 2


def test_include_only_matrix_counts_entries(tmp_path: Path) -> None:
    count, problems = _count(
        tmp_path,
        "jobs:\n"
        "  host:\n"
        "    strategy:\n"
        "      matrix:\n"
        "        include:\n"
        "          - shard: a\n"
        "          - shard: b\n"
        "          - shard: c\n",
    )
    assert problems == []
    assert count == 3


def test_list_dimensions_multiply(tmp_path: Path) -> None:
    count, problems = _count(
        tmp_path,
        "jobs:\n"
        "  host:\n"
        "    strategy:\n"
        "      matrix:\n"
        "        os: [a, b]\n"
        "        py: [1, 2, 3]\n",
    )
    assert problems == []
    assert count == 6


def test_exclude_subtracts_exact_matching_combos(
    tmp_path: Path,
) -> None:
    count, problems = _count(
        tmp_path,
        "jobs:\n"
        "  host:\n"
        "    strategy:\n"
        "      matrix:\n"
        "        os: [a, b]\n"
        "        py: [1, 2]\n"
        "        exclude:\n"
        "          - os: a\n"
        "            py: 1\n",
    )
    assert problems == []
    assert count == 3


def test_exclude_with_partial_keys_subtracts_every_match(
    tmp_path: Path,
) -> None:
    count, problems = _count(
        tmp_path,
        "jobs:\n"
        "  host:\n"
        "    strategy:\n"
        "      matrix:\n"
        "        os: [a, b]\n"
        "        py: [1, 2]\n"
        "        exclude:\n"
        "          - os: a\n",
    )
    assert problems == []
    assert count == 2


def test_dimensions_with_include_is_a_budget_problem(
    tmp_path: Path,
) -> None:
    count, problems = _count(
        tmp_path,
        "jobs:\n"
        "  host:\n"
        "    strategy:\n"
        "      matrix:\n"
        "        os: [a, b]\n"
        "        py: [1, 2]\n"
        "        include:\n"
        "          - os: c\n"
        "            py: 3\n",
    )
    assert count == 0
    assert len(problems) == 1
    assert "gate.yml" in problems[0]
    assert "'host'" in problems[0]
    assert "include" in problems[0]


def test_expression_dimension_is_a_budget_problem(
    tmp_path: Path,
) -> None:
    count, problems = _count(
        tmp_path,
        "jobs:\n"
        "  host:\n"
        "    strategy:\n"
        "      matrix:\n"
        '        py: "${{ fromJSON(needs.setup.outputs.versions) }}"\n',
    )
    assert count == 0
    assert len(problems) == 1
    assert "'host'" in problems[0]
    assert "'py'" in problems[0]


def test_non_list_dimension_is_a_budget_problem(
    tmp_path: Path,
) -> None:
    count, problems = _count(
        tmp_path,
        "jobs:\n"
        "  host:\n"
        "    strategy:\n"
        "      matrix:\n"
        "        os: ubuntu-latest\n",
    )
    assert count == 0
    assert len(problems) == 1
    assert "'os'" in problems[0]


def test_expression_matrix_is_a_budget_problem_not_a_crash(
    tmp_path: Path,
) -> None:
    count, problems = _count(
        tmp_path,
        "jobs:\n"
        "  host:\n"
        "    strategy:\n"
        '      matrix: "${{ fromJSON(needs.setup.outputs.matrix) }}"\n',
    )
    assert count == 0
    assert len(problems) == 1
    assert "'host'" in problems[0]
    assert "not a mapping" in problems[0]


def test_real_gate_workflows_count_6_and_13() -> None:
    ci, ci_problems = ctm.expanded_job_count(ctm.CI_YML)
    sandbox, sandbox_problems = ctm.expanded_job_count(ctm.SANDBOX_YML)
    assert ci_problems == []
    assert sandbox_problems == []
    assert (ci, sandbox) == (6, 13)
