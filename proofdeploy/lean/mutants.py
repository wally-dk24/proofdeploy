"""Deterministic AST mutation of a Python source file (Lean P6a, D258).

Gate units for the lean judge: mechanical mutants of a golden app, made
with no model and no judgement of whether a mutant is observable. A
mutant is the source with exactly ONE mutation site applied.

Operators (``Mutant.operator``)
-------------------------------
* ``flip_comparison``  == <-> !=, < <-> >=, > <-> <=  (single-operator compares)
* ``int_add_one`` / ``int_sub_one``  an integer literal +1 / -1 (bool excluded,
  results below zero skipped)
* ``negate_if``        ``if <test>`` becomes ``if not (<test>)``
* ``delete_statement`` a non-docstring statement in a function body is removed;
  when it is the only statement in its block it becomes ``pass``
* ``swap_dict_values`` two keys' values swapped in a dict literal inside a
  ``return`` statement
* ``flip_bool``        ``True`` <-> ``False``

Only code inside function bodies is mutated (module-level code and the
``__main__`` block never are), and functions named in ``exclude_functions``
are skipped together with everything nested in them.

Edits are made on the source text by position, so a mutant differs from the
original by that one site only (formatting elsewhere is untouched).

Determinism: sites are enumerated in source order; ``random.Random(seed)``
shuffles the site indices; candidates are taken in that order, skipping any
that fail ``compile()`` or duplicate an earlier mutant, until ``n`` are
collected. The same (source, seed, n, exclude_functions) always gives the
same mutants. Ids are the first 12 hex of sha256 of the mutated source.
"""

from __future__ import annotations

import argparse
import ast
import difflib
import hashlib
import json
import random
import re
import subprocess
import tempfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# One edit: byte span [start, end) in the UTF-8 source, and its replacement.
Edit = tuple[int, int, bytes]

_COMPARE_FLIP: dict[type[ast.cmpop], tuple[str, str]] = {
    ast.Eq: ("==", "!="),
    ast.NotEq: ("!=", "=="),
    ast.Lt: ("<", ">="),
    ast.GtE: (">=", "<"),
    ast.Gt: (">", "<="),
    ast.LtE: ("<=", ">"),
}
_CMP_TOKEN = re.compile(rb"==|!=|<=|>=|<|>")

COMMIT_MESSAGE = "refactor: tidy note handling"
JUDGE_INTENT = "no behaviour change intended"


@dataclass(frozen=True)
class Mutant:
    id: str
    operator: str
    lineno: int
    description: str
    mutated_source: str
    unified_diff: str


@dataclass(frozen=True)
class _Site:
    start: int  # sort key: byte offset of the first edit
    order: int  # tie-break among sites with the same start
    operator: str
    lineno: int
    description: str
    edits: tuple[Edit, ...]


class _Collector:
    def __init__(self, source: str, exclude: frozenset[str]) -> None:
        self.src = source.encode("utf-8")
        self.exclude = exclude
        self.sites: list[_Site] = []
        # Byte offset of the start of each (1-based) line.
        self.line_starts = [0]
        for line in self.src.splitlines(keepends=True):
            self.line_starts.append(self.line_starts[-1] + len(line))

    # -- positions ---------------------------------------------------------
    def _off(self, lineno: int, col: int) -> int:
        return self.line_starts[lineno - 1] + col

    def _span(self, node: ast.AST) -> tuple[int, int]:
        lineno: int = node.lineno  # type: ignore[attr-defined]
        end_lineno: int = node.end_lineno  # type: ignore[attr-defined]
        col: int = node.col_offset  # type: ignore[attr-defined]
        end_col: int = node.end_col_offset  # type: ignore[attr-defined]
        return self._off(lineno, col), self._off(end_lineno, end_col)

    def _text(self, node: ast.AST) -> bytes:
        a, b = self._span(node)
        return self.src[a:b]

    def _add(self, operator: str, node: ast.AST, description: str, edits: Iterable[Edit]) -> None:
        edit_list = tuple(edits)
        self.sites.append(
            _Site(
                start=min(e[0] for e in edit_list),
                order=len(self.sites),
                operator=operator,
                lineno=node.lineno,  # type: ignore[attr-defined]
                description=description,
                edits=edit_list,
            )
        )

    # -- traversal ---------------------------------------------------------
    def collect(self, tree: ast.Module) -> None:
        for node in tree.body:
            self._walk_outside(node)

    def _walk_outside(self, node: ast.AST) -> None:
        """Module / class level: only descend to find functions."""
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name not in self.exclude:
                self._function(node)
            return
        for child in ast.iter_child_nodes(node):
            self._walk_outside(child)

    def _function(self, fn: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self._block(fn.body, is_function_body=True)
        for stmt in fn.body:
            self._walk_inside(stmt)

    def _walk_inside(self, node: ast.AST) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name in self.exclude:
                return
            # A nested function: its own statements are candidates too.
            self._block(node.body, is_function_body=True)
            for stmt in node.body:
                self._walk_inside(stmt)
            return
        self._visit_node(node)
        for field in ("body", "orelse", "finalbody"):
            block = getattr(node, field, None)
            if isinstance(block, list) and block and isinstance(block[0], ast.stmt):
                self._block(block, is_function_body=False)
        for handler in getattr(node, "handlers", []):
            self._block(handler.body, is_function_body=False)
        for child in ast.iter_child_nodes(node):
            self._walk_inside(child)

    # -- operators ---------------------------------------------------------
    def _visit_node(self, node: ast.AST) -> None:
        if isinstance(node, ast.Compare) and len(node.ops) == 1:
            self._compare(node)
        elif isinstance(node, ast.Constant):
            self._constant(node)
        elif isinstance(node, ast.If):
            self._negate_if(node)
        elif isinstance(node, ast.Return) and node.value is not None:
            for sub in ast.walk(node.value):
                if isinstance(sub, ast.Dict):
                    self._swap_dict(sub)

    def _compare(self, node: ast.Compare) -> None:
        flip = _COMPARE_FLIP.get(type(node.ops[0]))
        if flip is None:
            return
        old, new = flip
        left_end = self._span(node.left)[1]
        right_start = self._span(node.comparators[0])[0]
        gap = self.src[left_end:right_start]
        m = _CMP_TOKEN.search(gap)
        if m is None or m.group(0).decode() != old:
            return
        a = left_end + m.start()
        b = left_end + m.end()
        self._add(
            "flip_comparison",
            node,
            f"flip {old} to {new} at line {node.lineno}",
            [(a, b, new.encode())],
        )

    def _constant(self, node: ast.Constant) -> None:
        value = node.value
        a, b = self._span(node)
        if isinstance(value, bool):
            new_bool = "False" if value else "True"
            self._add(
                "flip_bool",
                node,
                f"flip {value} to {new_bool} at line {node.lineno}",
                [(a, b, new_bool.encode())],
            )
        elif isinstance(value, int):
            for operator, delta in (("int_add_one", 1), ("int_sub_one", -1)):
                new = value + delta
                if new < 0:
                    continue
                self._add(
                    operator,
                    node,
                    f"change integer {value} to {new} at line {node.lineno}",
                    [(a, b, str(new).encode())],
                )

    def _negate_if(self, node: ast.If) -> None:
        a, b = self._span(node.test)
        text = self.src[a:b]
        self._add(
            "negate_if",
            node,
            f"negate if condition at line {node.lineno}",
            [(a, b, b"not (" + text + b")")],
        )

    def _swap_dict(self, node: ast.Dict) -> None:
        pairs = [(k, v) for k, v in zip(node.keys, node.values, strict=True) if k is not None]
        if len(pairs) != len(node.keys):
            return  # a ** unpack: leave the literal alone
        for i in range(len(pairs)):
            for j in range(i + 1, len(pairs)):
                (ki, vi), (kj, vj) = pairs[i], pairs[j]
                ti, tj = self._text(vi), self._text(vj)
                if ti == tj:
                    continue
                ai, bi = self._span(vi)
                aj, bj = self._span(vj)
                ni = self._text(ki).decode("utf-8")
                nj = self._text(kj).decode("utf-8")
                self._add(
                    "swap_dict_values",
                    ki,
                    f"swap values of keys {ni} and {nj} in returned dict at line {ki.lineno}",
                    [(ai, bi, tj), (aj, bj, ti)],
                )

    def _block(self, body: list[ast.stmt], *, is_function_body: bool) -> None:
        """Statement-deletion sites for one statement list."""
        for idx, stmt in enumerate(body):
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            if (
                idx == 0
                and is_function_body
                and isinstance(stmt, ast.Expr)
                and isinstance(stmt.value, ast.Constant)
                and isinstance(stmt.value.value, str)
            ):
                continue  # docstring
            start_line = stmt.lineno
            end_line = stmt.end_lineno or stmt.lineno
            line_a = self.line_starts[start_line - 1]
            line_b = self.line_starts[end_line]
            first_line = self.src[line_a : self.line_starts[start_line]]
            indent = first_line[: len(first_line) - len(first_line.lstrip())]
            if stmt.col_offset != len(indent):
                continue  # shares its line with something else
            only = len(body) == 1
            if only:
                repl = indent + b"pass\n"
                desc = f"replace statement with pass at line {start_line}"
            else:
                repl = b""
                desc = f"delete statement at line {start_line}"
            self._add("delete_statement", stmt, desc, [(line_a, line_b, repl)])


def _apply(source: bytes, edits: Sequence[Edit]) -> str:
    out = source
    for a, b, repl in sorted(edits, key=lambda e: e[0], reverse=True):
        out = out[:a] + repl + out[b:]
    return out.decode("utf-8")


def _compiles(code: str) -> bool:
    try:
        compile(code, "<mutant>", "exec")
    except (SyntaxError, ValueError):
        return False
    return True


def generate_mutants(
    source: str,
    *,
    seed: int,
    n: int,
    exclude_functions: Iterable[str] = (),
    filename: str = "app.py",
) -> list[Mutant]:
    """``n`` mutants of ``source``, deterministic in (source, seed, n, exclude).

    Raises ValueError when fewer than ``n`` distinct compiling mutants exist.
    """
    tree = ast.parse(source)
    collector = _Collector(source, frozenset(exclude_functions))
    collector.collect(tree)
    sites = sorted(collector.sites, key=lambda s: (s.start, s.order))
    rng = random.Random(seed)
    # sample without replacement: a seeded permutation of the site indices,
    # consumed in order; failures (no compile / duplicate / no-op) are skipped.
    order = rng.sample(range(len(sites)), len(sites))
    src_bytes = source.encode("utf-8")
    chosen: list[Mutant] = []
    seen: set[str] = set()
    for index in order:
        if len(chosen) == n:
            break
        site = sites[index]
        mutated = _apply(src_bytes, site.edits)
        if mutated == source or not _compiles(mutated):
            continue
        mutant_id = hashlib.sha256(mutated.encode("utf-8")).hexdigest()[:12]
        if mutant_id in seen:
            continue
        seen.add(mutant_id)
        diff = "".join(
            difflib.unified_diff(
                source.splitlines(keepends=True),
                mutated.splitlines(keepends=True),
                fromfile=f"a/{filename}",
                tofile=f"b/{filename}",
            )
        )
        chosen.append(
            Mutant(
                id=mutant_id,
                operator=site.operator,
                lineno=site.lineno,
                description=site.description,
                mutated_source=mutated,
                unified_diff=diff,
            )
        )
    if len(chosen) < n:
        raise ValueError(f"only {len(chosen)} distinct compiling mutants available, wanted {n}")
    return chosen


def _kit() -> Any:
    """The conformance kit, loaded by path (conformance/ is a fixture directory of the repo,
    not a package, so it is not imported by package name)."""
    import importlib.util
    import sys

    name = "pd_conformance_kit"
    if name in sys.modules:
        return sys.modules[name]
    path = Path(__file__).resolve().parents[2] / "conformance" / "kit.py"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    return proc.stdout.strip()


def write_mutant_repo(
    golden_app: str,
    base_revision: str,
    mutants: Sequence[Mutant],
    *,
    dest: Path | None = None,
    target: str = "app.py",
) -> Path:
    """Materialize the golden app's history and add one branch per mutant.

    Each ``mutant/<id>`` branch starts at the base revision's commit and adds
    ONE commit that rewrites ``target`` with the mutant's source. The commit
    message is the neutral ``COMMIT_MESSAGE``; the intent given to the judge
    (``JUDGE_INTENT``) is recorded in the manifest, not in the commit.
    """
    repo = Path(dest) if dest is not None else Path(tempfile.mkdtemp(prefix="pd-mutants-"))
    shas = _kit().materialize_golden(golden_app, repo)
    if base_revision not in shas:
        raise ValueError(f"unknown revision {base_revision!r}")
    base_sha = shas[base_revision]
    _git(repo, "config", "commit.gpgsign", "false")
    for m in mutants:
        _git(repo, "checkout", "-q", "-f", "-b", f"mutant/{m.id}", base_sha)
        (repo / target).write_text(m.mutated_source, encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", COMMIT_MESSAGE)
    _git(repo, "checkout", "-q", "-f", base_sha)
    return repo


def build_manifest(
    *,
    golden_app: str,
    base_revision: str,
    target: str,
    source: str,
    seed: int,
    n: int,
    exclude_functions: Sequence[str],
    mutants: Sequence[Mutant],
) -> dict[str, Any]:
    """The mutant manifest: ids, operator, line, description. No judgement of
    observability (that is exactly what the gate measures)."""
    return {
        "golden_app": golden_app,
        "base_revision": base_revision,
        "target": target,
        "base_source_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
        "seed": seed,
        "n": n,
        "exclude_functions": list(exclude_functions),
        "excluded_regions": ["__main__ block (module level is never mutated)"],
        "commit_message": COMMIT_MESSAGE,
        "judge_intent": JUDGE_INTENT,
        "mutants": [
            {
                "id": m.id,
                "branch": f"mutant/{m.id}",
                "operator": m.operator,
                "line": m.lineno,
                "description": m.description,
            }
            for m in mutants
        ],
    }


GATE_APP = "python-flask-sqlite"
GATE_REVISION = "fix"
GATE_TARGET = "app.py"
GATE_SEED = 20261020
GATE_N = 20
GATE_EXCLUDE = ("db", "_close")


def gate_manifest() -> tuple[dict[str, Any], list[Mutant]]:
    """The registered gate configuration applied to the committed golden source."""
    source = (_kit().GOLDEN_DIR / GATE_APP / "revisions" / GATE_REVISION / GATE_TARGET).read_text(
        encoding="utf-8"
    )
    mutants = generate_mutants(source, seed=GATE_SEED, n=GATE_N, exclude_functions=GATE_EXCLUDE)
    manifest = build_manifest(
        golden_app=GATE_APP,
        base_revision=GATE_REVISION,
        target=GATE_TARGET,
        source=source,
        seed=GATE_SEED,
        n=GATE_N,
        exclude_functions=GATE_EXCLUDE,
        mutants=mutants,
    )
    return manifest, mutants


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    ap.add_argument("--write-manifest", type=Path, help="write the gate manifest JSON here")
    ap.add_argument("--repo", type=Path, help="materialize the mutant repo at this new path")
    args = ap.parse_args(argv)
    manifest, mutants = gate_manifest()
    if args.write_manifest:
        args.write_manifest.parent.mkdir(parents=True, exist_ok=True)
        args.write_manifest.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    if args.repo:
        if args.repo.exists():
            raise SystemExit(f"{args.repo} exists; choose a new path")
        write_mutant_repo(GATE_APP, GATE_REVISION, mutants, dest=args.repo)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
