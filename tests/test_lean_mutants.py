"""Lean P6a (D258): deterministic AST mutants. No model calls."""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

import pytest

from proofdeploy.lean import mutants as M
from proofdeploy.lean.mutants import Mutant, generate_mutants, write_mutant_repo

GOLDEN_SRC = (
    M._kit().GOLDEN_DIR / M.GATE_APP / "revisions" / M.GATE_REVISION / M.GATE_TARGET
).read_text(encoding="utf-8")


def _all(source: str, operator: str, **kw: object) -> list[Mutant]:
    """Every mutant of one operator (n large; ValueError means fewer exist)."""
    found: list[Mutant] = []
    n = 1
    while True:
        try:
            found = generate_mutants(source, seed=1, n=n, **kw)  # type: ignore[arg-type]
        except ValueError:
            break
        n += 1
    return [m for m in found if m.operator == operator]


def _one(source: str, operator: str) -> list[str]:
    return sorted(m.mutated_source for m in _all(source, operator))


def test_flip_comparison_each_pair() -> None:
    src = (
        "def f(a, b):\n"
        "    x = a == b\n"
        "    y = a != b\n"
        "    z = a < b\n"
        "    w = a >= b\n"
        "    u = a > b\n"
        "    v = a <= b\n"
        "    return x, y, z, w, u, v\n"
    )
    descs = sorted(m.description for m in _all(src, "flip_comparison"))
    assert descs == sorted(
        [
            "flip == to != at line 2",
            "flip != to == at line 3",
            "flip < to >= at line 4",
            "flip >= to < at line 5",
            "flip > to <= at line 6",
            "flip <= to > at line 7",
        ]
    )
    by_line = {
        m.lineno: m.mutated_source.splitlines()[m.lineno - 1] for m in _all(src, "flip_comparison")
    }
    assert by_line[2] == "    x = a != b"
    assert by_line[6] == "    u = a <= b"


def test_flip_comparison_ignores_chained_and_in() -> None:
    src = "def f(a):\n    return 1 < a < 3, a in (1,)\n"
    assert _all(src, "flip_comparison") == []


def test_int_plus_minus_one_and_bool_excluded() -> None:
    src = "def f():\n    a = 5\n    b = True\n    return a, b, 0\n"
    got = sorted(m.description for m in _all(src, "int_add_one") + _all(src, "int_sub_one"))
    # 0 - 1 would be negative: skipped; bool is not an integer site.
    assert got == [
        "change integer 0 to 1 at line 4",
        "change integer 5 to 4 at line 2",
        "change integer 5 to 6 at line 2",
    ]


def test_negate_if() -> None:
    src = "def f(a):\n    if a > 1:\n        return 1\n    return 2\n"
    [m] = _all(src, "negate_if")
    assert "    if not (a > 1):\n" in m.mutated_source
    assert m.description == "negate if condition at line 2"


def test_delete_statement_and_pass_replacement_and_docstring_kept() -> None:
    src = 'def f(a):\n    """doc"""\n    x = 1\n    if a:\n        y = 2\n    return x\n'
    ms = _all(src, "delete_statement")
    descs = sorted(m.description for m in ms)
    assert "delete statement at line 2" not in descs  # docstring never deleted
    assert "delete statement at line 3" in descs
    assert "replace statement with pass at line 5" in descs
    pass_m = next(m for m in ms if m.lineno == 5)
    assert "        pass\n" in pass_m.mutated_source
    ast.parse(pass_m.mutated_source)
    # a function whose only statement is a docstring-less single stmt becomes pass
    only = _all("def g():\n    return 1\n", "delete_statement")
    assert [m.mutated_source for m in only] == ["def g():\n    pass\n"]


def test_swap_dict_values_in_return_only() -> None:
    src = "def f(a, b):\n    unused = {'p': a, 'q': b}\n    return {'x': a, 'y': b, 'z': a}\n"
    ms = _all(src, "swap_dict_values")
    assert sorted(m.description for m in ms) == [
        "swap values of keys 'x' and 'y' in returned dict at line 3",
        "swap values of keys 'y' and 'z' in returned dict at line 3",
    ]  # x/z hold identical value text: a no-op swap is not a site
    xy = next(m for m in ms if "'x' and 'y'" in m.description)
    assert xy.mutated_source.splitlines()[2] == "    return {'x': b, 'y': a, 'z': a}"
    assert "'p': a, 'q': b" in xy.mutated_source  # non-returned dict untouched


def test_flip_bool() -> None:
    src = "def f():\n    a = True\n    return False\n"
    assert sorted(m.description for m in _all(src, "flip_bool")) == [
        "flip False to True at line 3",
        "flip True to False at line 2",
    ]


def test_module_level_and_main_block_never_mutated() -> None:
    src = (
        "LIMIT = 10\n"
        "FLAG = True\n"
        "def f():\n"
        "    return 3\n"
        "if __name__ == '__main__':\n"
        "    print(f() == 3, 7)\n"
    )
    for m in _all(src, "int_add_one") + _all(src, "flip_comparison") + _all(src, "flip_bool"):
        assert m.lineno == 4


def test_exclude_functions_untouched_incl_nested() -> None:
    src = (
        "def keep(a):\n    return a + 1\n"
        "def skip(a):\n    def inner():\n        return 41\n    return a == 2\n"
    )
    ms = generate_mutants(src, seed=3, n=3, exclude_functions=("skip",))
    assert all(m.lineno <= 2 for m in ms)
    for m in ms:
        assert "def skip(a):\n    def inner():\n        return 41\n    return a == 2\n" in (
            m.mutated_source
        )


def test_golden_excluded_functions_untouched() -> None:
    ms = generate_mutants(
        GOLDEN_SRC, seed=M.GATE_SEED, n=M.GATE_N, exclude_functions=M.GATE_EXCLUDE
    )
    block_db = GOLDEN_SRC[GOLDEN_SRC.index("def db():") : GOLDEN_SRC.index("@app.get")]
    block_close = GOLDEN_SRC[
        GOLDEN_SRC.index("def _close(exc):") : GOLDEN_SRC.index("def issue_token")
    ]
    main_block = GOLDEN_SRC[GOLDEN_SRC.index('if __name__ == "__main__":') :]
    for m in ms:
        assert block_close in m.mutated_source
        assert main_block in m.mutated_source
    assert 'def db():\n    if "db" not in g:' in "".join(m.mutated_source for m in ms[:1])
    assert block_db  # sanity: the slice exists


def test_determinism_same_seed_same_ids() -> None:
    a = generate_mutants(GOLDEN_SRC, seed=M.GATE_SEED, n=M.GATE_N, exclude_functions=M.GATE_EXCLUDE)
    b = generate_mutants(GOLDEN_SRC, seed=M.GATE_SEED, n=M.GATE_N, exclude_functions=M.GATE_EXCLUDE)
    c = generate_mutants(
        GOLDEN_SRC, seed=M.GATE_SEED + 1, n=M.GATE_N, exclude_functions=M.GATE_EXCLUDE
    )
    assert [m.id for m in a] == [m.id for m in b]
    assert [m.id for m in a] != [m.id for m in c]


def test_gate_mutants_compile_distinct_and_ids_stable() -> None:
    import hashlib

    ms = generate_mutants(
        GOLDEN_SRC, seed=M.GATE_SEED, n=M.GATE_N, exclude_functions=M.GATE_EXCLUDE
    )
    assert len(ms) == 20
    assert len({m.id for m in ms}) == 20
    for m in ms:
        compile(m.mutated_source, "<m>", "exec")
        assert m.mutated_source != GOLDEN_SRC
        assert m.id == hashlib.sha256(m.mutated_source.encode()).hexdigest()[:12]
        assert m.unified_diff.startswith("--- a/app.py\n+++ b/app.py\n")


def test_not_enough_sites_raises() -> None:
    with pytest.raises(ValueError):
        generate_mutants("def f():\n    return 1\n", seed=1, n=50)


def test_committed_manifest_matches_generator() -> None:
    import json

    committed = json.loads(
        (Path(__file__).parent.parent / "experiments/lean-poc/mutants.json").read_text()
    )
    manifest, _ = M.gate_manifest()
    assert committed == manifest
    assert all(
        set(row) == {"id", "branch", "operator", "line", "description"}
        for row in committed["mutants"]
    )


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout


def test_materialized_branches_differ_from_base_by_one_mutation(tmp_path: Path) -> None:
    ms = generate_mutants(
        GOLDEN_SRC, seed=M.GATE_SEED, n=M.GATE_N, exclude_functions=M.GATE_EXCLUDE
    )
    repo = write_mutant_repo(M.GATE_APP, M.GATE_REVISION, ms, dest=tmp_path / "repo")
    base = _git(repo, "rev-parse", "HEAD").strip()
    assert _git(repo, "show", f"{base}:app.py") == GOLDEN_SRC
    branches = _git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/mutant/").split()
    assert sorted(branches) == sorted(f"mutant/{m.id}" for m in ms)
    for m in ms:
        br = f"mutant/{m.id}"
        assert _git(repo, "rev-parse", f"{br}^").strip() == base
        assert _git(repo, "log", "-1", "--format=%s", br).strip() == "refactor: tidy note handling"
        assert _git(repo, "diff", "--name-only", base, br).split() == ["app.py"]
        assert _git(repo, "show", f"{br}:app.py") == m.mutated_source
        # exactly one mutation: the changed-line count is that of the mutant's own diff
        stat = _git(repo, "diff", "--numstat", base, br).split()
        own = [
            ln
            for ln in m.unified_diff.splitlines()
            if ln[:1] in "+-" and ln[:3] not in ("+++", "---")
        ]
        assert int(stat[0]) + int(stat[1]) == len(own)
        assert int(stat[0]) + int(stat[1]) <= 2 or m.operator in (
            "delete_statement",
            "swap_dict_values",
        )
