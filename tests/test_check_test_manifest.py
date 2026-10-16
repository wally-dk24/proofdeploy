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

A second section below guards the D109 (plan 1.13) docs/evaluation
NON-INERT lists in the same two workflows.
"""

import importlib.util
import re
import sys
from pathlib import Path

import yaml

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


# ------------------------------------------------------------------
# D109 (plan 1.13): the docs/evaluation NON-INERT lists.
#
# WAL-101 originally treated every docs/evaluation change as heavy:
# registrations, addenda and pure rule documents each burned a full
# CI cycle. D109 replaced the blanket glob in both gate workflows'
# changes jobs with an explicit list of only the docs/evaluation
# files that tests or code actually reference; every other such file
# is inert. The tests below derive the referenced set by scanning
# the source trees — never a copied constant — and require both
# workflows' parsed lists to equal it and each other.
#
# References take two shapes: a literal path string, and a Path-join
# segment sequence ("docs" / "evaluation" / ...), single- or
# multi-line. A referenced path that exists as a file must be
# listed. One that names a directory is discharged by the file
# references under it (the skill directory is laid out in temp
# trees by the generator tests) and must have at least one. A
# reference to a path that exists as neither fails here, so a
# dangling reference cannot pass silently.

_EVAL_LITERAL_RE = re.compile(r"docs/evaluation/[A-Za-z0-9._\-/]+")
_EVAL_JOIN_RE = re.compile(
    r"[\"']docs[\"']\s*/\s*[\"']evaluation[\"']"
    r"(?:\s*/\s*[\"'][^\"']+[\"'])+"
)
_EVAL_TREES = ("tests", "proofdeploy", "scripts")


def _referenced_evaluation_paths() -> tuple[set[str], set[str]]:
    files: set[str] = set()
    dirs: set[str] = set()
    for tree in _EVAL_TREES:
        for path in sorted((REPO / tree).rglob("*")):
            if (
                not path.is_file()
                or "__pycache__" in path.parts
                or path.suffix == ".pyc"
            ):
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            candidates = [m.group(0) for m in _EVAL_LITERAL_RE.finditer(text)]
            candidates += [
                "/".join(re.findall(r"[\"']([^\"']+)[\"']", m.group(0)))
                for m in _EVAL_JOIN_RE.finditer(text)
            ]
            for candidate in candidates:
                relative = candidate.rstrip("/")
                target = REPO / relative
                if target.is_file():
                    files.add(relative)
                elif target.is_dir():
                    dirs.add(relative)
                else:
                    raise AssertionError(
                        f"{path}: references {relative}, which exists "
                        "as neither a file nor a directory in the repo"
                    )
    return files, dirs


def _noninert_evaluation_list(yml_path: Path) -> list[str]:
    data = yaml.safe_load(yml_path.read_text(encoding="utf-8"))
    decide = next(
        step for step in data["jobs"]["changes"]["steps"] if step.get("id") == "decide"
    )
    script = decide["run"]
    assert "docs/evaluation/*)" not in script, (
        f"{yml_path.name}: the blanket docs/evaluation glob is back; "
        "D109 allows only the explicit NON-INERT list"
    )
    arms = [
        line
        for line in script.splitlines()
        if "NON-INERT (test input)" in line and ") echo" in line
    ]
    assert len(arms) == 1, (
        f"{yml_path.name}: expected exactly one NON-INERT arm in the "
        f"changes job, found {len(arms)}"
    )
    head = arms[0].split(") echo", 1)[0].strip()
    entries = [entry.strip() for entry in head.split("|")]
    assert entries and all(
        entry.startswith("docs/evaluation/") for entry in entries
    ), f"{yml_path.name}: malformed NON-INERT list: {entries}"
    return entries


def test_evaluation_noninert_lists_equal_referenced_files() -> None:
    files, dirs = _referenced_evaluation_paths()
    assert files, "derivation found no referenced docs/evaluation files"
    for directory in dirs:
        assert any(name.startswith(directory + "/") for name in files), (
            f"referenced directory {directory} has no referenced file "
            "under it; the NON-INERT lists cannot discharge it"
        )
    ci_list = _noninert_evaluation_list(ctm.CI_YML)
    sandbox_list = _noninert_evaluation_list(ctm.SANDBOX_YML)
    assert ci_list == sandbox_list, (
        "the two gate workflows' NON-INERT lists differ: "
        f"ci={ci_list} sandbox-tests={sandbox_list}"
    )
    assert set(ci_list) == files, (
        "NON-INERT lists must equal the referenced set exactly; "
        f"listed={sorted(ci_list)} referenced={sorted(files)}"
    )


def test_evaluation_reference_derivation_finds_registered_skill() -> None:
    # Vacuity anchor: the sha256-checked registered skill file is
    # read by host tests in this repo (its hash is pinned in
    # proofdeploy/fixture.py), so a derivation that misses it is
    # broken, not merely strict.
    files, _dirs = _referenced_evaluation_paths()
    skill = (
        (REPO / "docs" / "evaluation" / "skill" / "skill-author.md")
        .relative_to(REPO)
        .as_posix()
    )
    assert skill in files
