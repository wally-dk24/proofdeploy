"""Guard (D265): every third-party import in proofdeploy/ is covered by a
declared RUNTIME dependency. Dev extras mask a missing one: the lean-poc
gate failed at startup on ``import coverage`` while CI (``.[dev]``) passed.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "proofdeploy"

# distribution name (normalised, lowercase) -> top-level import names.
DIST_TO_IMPORTS: dict[str, set[str]] = {
    "pyyaml": {"yaml"},
    "coverage": {"coverage"},
}

# Modules provided by the host environment, not by pip: (module, file) pairs.
# model_client.py is pinned (CLAUDE.md), so this import cannot be changed.
ENVIRONMENT_PROVIDED: set[tuple[str, str]] = {
    ("dynamic_credentials", "proofdeploy/model_client.py"),
}


def _runtime_dists() -> set[str]:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r"^dependencies\s*=\s*\[(.*?)\]", text, re.S | re.M)
    assert m, "no [project] dependencies list found in pyproject.toml"
    names = set()
    for spec in re.findall(r'"([^"]+)"', m.group(1)):
        name = re.split(r"[<>=!~\[; ]", spec, maxsplit=1)[0]
        names.add(name.lower().replace("_", "-"))
    return names


def _is_type_checking(test: ast.expr) -> bool:
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


def _imports(tree: ast.AST) -> list[str]:
    found: list[str] = []

    def visit(node: ast.AST) -> None:
        if isinstance(node, ast.If) and _is_type_checking(node.test):
            for stmt in node.orelse:
                visit(stmt)
            return
        if isinstance(node, ast.Import):
            found.extend(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.append(node.module.split(".")[0])
        for child in ast.iter_child_nodes(node):
            visit(child)

    visit(tree)
    return found


def test_every_third_party_import_is_a_runtime_dependency() -> None:
    covered: set[str] = set()
    for dist in _runtime_dists():
        assert dist in DIST_TO_IMPORTS, (
            f"runtime dependency {dist!r} has no entry in DIST_TO_IMPORTS; "
            "add its import name(s) to tests/test_runtime_dependencies.py"
        )
        covered |= DIST_TO_IMPORTS[dist]
    problems = []
    for path in sorted(PKG.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for mod in _imports(tree):
            if mod == "proofdeploy" or mod in sys.stdlib_module_names:
                continue
            if (mod, path.relative_to(ROOT).as_posix()) in ENVIRONMENT_PROVIDED:
                continue
            if mod not in covered:
                problems.append(
                    f"module {mod!r} imported in {path.relative_to(ROOT)} is not "
                    "provided by any declared runtime dependency in pyproject.toml"
                )
    assert not problems, "\n".join(problems)


def test_lean_coverage_dependency_pinned_exactly() -> None:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r"^dependencies\s*=\s*\[(.*?)\]", text, re.S | re.M)
    assert m and '"coverage==7.16.2"' in m.group(1)
