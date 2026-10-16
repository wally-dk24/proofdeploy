#!/usr/bin/env python3
"""The test manifest: every collected test, by name, in a file.

Why this exists (WAL-109): the WAL-102 partition proof used to
hardcode per-shard test COUNTS in the ci.yml matrix (212 / 245 /
148, then 169, then 174...). Every added test changed a number
somewhere else, and forgetting it failed CI ten minutes into a
run — it bit PR #66 twice in one day. Counts were the wrong
granularity: a number cannot say WHICH test appeared or
vanished, and a rename that keeps the count is invisible to it.

The manifest replaces every hardcoded number and every embedded
file list with two generated files:

    tests/expected_host_nodeids.txt     (-m "not sandbox")
    tests/expected_sandbox_nodeids.txt  (-m "sandbox")

one nodeid per line, sorted. They are GENERATED, never edited
by hand:

    python scripts/check_test_manifest.py --write

after adding, removing, renaming, or moving any test. The same
script in check mode (what CI's lint job runs on every PR)
proves, from real pytest collection:

  * the host and sandbox collections equal the manifests
    exactly (any drift prints the added/removed nodeids);
  * host and sandbox partition the full collection (disjoint,
    union == full);
  * every manifest nodeid is selected by exactly one shard of
    its workflow's matrix (ci.yml host shards by files/nodeids,
    sandbox-tests.yml shards by files/nodeids) — a test no shard selects,
    or two shards select, is named here, fast, instead of
    running twice or not at all in a 30-minute job.
  * the two gate workflows' expanded job count stays within the
    per-push job budget (JOB_BUDGET_PER_PUSH below): PR #112's
    measurement showed 23 jobs per push against the GitHub Free
    plan's 20-concurrent-job cap, with queue time — not run time —
    dominating wall clock. WO-CI-SPEED task 5 (D53) re-packed the
    shards to 19; this check keeps a future matrix addition from
    silently re-creating the queue.

The shards keep their runtime proofs unchanged in kind: each
shard's JUnit verify asserts tests == its own collected count
and skipped == 0. The count now comes from these manifests —
read by `--counts` mode below — instead of a fresh collection
in every shard job. That is exactly as strong: the lint job
proves the manifests equal real collection on the same commit,
and if a shard's manifest-derived count ever disagreed with
what its real pytest run collects, the JUnit verify fails.
The anti-silent-drop proof that the hardcoded numbers used to
provide lives here, in the manifest diff — and it is strictly
stronger: it catches renames and swaps that keep every count
identical.

Why collection runs ONCE (gate-speed item 0): importing the
suite used to be expensive — several test modules ran a real
podman capability probe at import time (module-level `_CAPS` /
`_EVIDENCE` assignments and a module-level skipif in the
sandbox test files), ~20s per probe on a CI runner, so a single
`pytest --collect-only` cost ~110s while the remaining ~40
modules collect in ~0.5s each. WO-CI-SPEED task 2 made those
probes lazy (first use, never at import), so collection is now
fast — but the one-pass design stays: this script used to
collect 17 times (3 full selections + one per shard of both
matrices), and every shard job collected 2-4 more times for
its counts — ~50 collections per PR gate. Now the check makes
ONE collection pass: a throwaway pytest plugin records every
item's nodeid and whether it carries the `sandbox` marker
(the marker tests/conftest.py's pytest_itemcollected applies
during that same collection — the very marker pytest's own
-m selection consumes). The host/sandbox/full selections, the
manifest diffs, and the per-shard coverage proof are all
derived from that one real collection by set arithmetic:
a shard's selection is the universe nodeids whose file is in
the shard's `files`, plus its explicit `nodeids` — the same
semantics as passing those args to pytest, cross-checked by
every shard's JUnit verify against its real run.

Exit 0 = manifest and matrices match reality. Exit 1 = drift,
with the exact nodeids and the one command that repairs the
manifest. Shard ASSIGNMENT for a genuinely new test file stays
a human edit to the matrices' files/nodeids (rare, and the
check names the uncovered file when it is missing).
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CI_YML = ROOT / ".github" / "workflows" / "ci.yml"
SANDBOX_YML = ROOT / ".github" / "workflows" / "sandbox-tests.yml"
HOST_MANIFEST = ROOT / "tests" / "expected_host_nodeids.txt"
SANDBOX_MANIFEST = ROOT / "tests" / "expected_sandbox_nodeids.txt"

# The per-push job budget: the GitHub Free plan runs at most 20
# jobs concurrently, and every job past the cap queues behind
# finished ones (PR #112's measurement: at 23 jobs the late jobs'
# queue medians were 599–1047.5s — longer than most jobs run).
# Only the two gate workflows count: they are the ones that fire
# on every push and every pull request. (pr-agent.yml fires on
# pull-request events only and adds 1 job there; measure.yml,
# readiness*.yml and sandbox-focus.yml are manual dispatches.)
JOB_BUDGET_PER_PUSH = 20

HEADER = (
    "# GENERATED by scripts/check_test_manifest.py --write — "
    "do not edit by hand.\n"
    "# One collected test nodeid per line, sorted. Regenerate "
    "after adding,\n"
    "# removing, renaming, or moving any test.\n"
)

# Throwaway pytest plugin for the single collection pass: after
# collection (and after tests/conftest.py's pytest_itemcollected
# has applied the `sandbox` marker), dump every item's nodeid
# and marker flag as JSON lines to $PD_MANIFEST_DUMP.
_DUMP_PLUGIN = '''\
import json
import os


def pytest_collection_modifyitems(session, config, items):
    path = os.environ.get("PD_MANIFEST_DUMP")
    if not path:
        return
    with open(path, "w", encoding="utf-8") as fh:
        for item in items:
            fh.write(json.dumps({
                "nodeid": item.nodeid,
                "sandbox": item.get_closest_marker("sandbox") is not None,
            }) + "\\n")
'''


def collect_all() -> list[tuple[str, bool]]:
    """ONE pytest collection of the whole suite.

    Returns (nodeid, carries-the-sandbox-marker) for every
    collected item, exactly as pytest collects them — the marker
    flag is read in the same collection pass that pytest's own
    -m selection would consume.
    """
    with tempfile.TemporaryDirectory() as td:
        plugin = Path(td) / "pd_manifest_dump.py"
        plugin.write_text(_DUMP_PLUGIN, encoding="utf-8")
        dump = Path(td) / "dump.jsonl"
        env = dict(os.environ)
        env["PD_MANIFEST_DUMP"] = str(dump)
        prev = env.get("PYTHONPATH")
        env["PYTHONPATH"] = td if not prev else td + os.pathsep + prev
        cmd = [
            sys.executable, "-m", "pytest", "--collect-only", "-q",
            "-o", "addopts=", "-p", "no:cacheprovider",
            "-p", "pd_manifest_dump",
        ]
        out = subprocess.run(
            cmd, cwd=ROOT, capture_output=True, text=True,
            timeout=900, env=env,
        )
        if out.returncode not in (0, 5):  # 5 = no tests collected
            raise RuntimeError(
                f"collection failed: {' '.join(cmd)}\n{out.stderr}"
            )
        if not dump.exists():
            raise RuntimeError(
                "collection produced no manifest dump "
                f"(plugin did not run?): {' '.join(cmd)}\n{out.stderr}"
            )
        rows = [
            json.loads(ln)
            for ln in dump.read_text(encoding="utf-8").splitlines()
            if ln.strip()
        ]
    return sorted((r["nodeid"], bool(r["sandbox"])) for r in rows)


def read_manifest(path: Path) -> list[str]:
    if not path.exists():
        return []
    return sorted(
        ln.strip()
        for ln in path.read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.startswith("#")
    )


def write_manifest(path: Path, nodeids: list[str]) -> None:
    path.write_text(
        HEADER + "".join(f"{n}\n" for n in nodeids),
        encoding="utf-8",
    )


def matrix(yml_path: Path, job: str) -> list[dict]:
    data = yaml.safe_load(yml_path.read_text(encoding="utf-8"))
    return data["jobs"][job]["strategy"]["matrix"]["include"]


def shard_selection(entry: dict) -> list[str]:
    return (entry.get("files") or "").split() + (
        entry.get("nodeids") or ""
    ).split()


def selection_from_universe(
    tokens: list[str], universe: set[str]
) -> set[str]:
    """The nodeids pytest would select for these args, by set
    arithmetic over an already-collected universe: an explicit
    nodeid token selects itself; a path token selects every
    nodeid under that file (or directory)."""
    selected: set[str] = set()
    for token in tokens:
        if "::" in token:
            if token in universe:
                selected.add(token)
            continue
        prefix = token.rstrip("/")
        for nodeid in universe:
            path = nodeid.split("::", 1)[0]
            if path == prefix or path.startswith(prefix + "/"):
                selected.add(nodeid)
    return selected


def diff_problems(
    label: str, reality: list[str], manifest: list[str]
) -> list[str]:
    added = sorted(set(reality) - set(manifest))
    removed = sorted(set(manifest) - set(reality))
    problems = []
    if added:
        problems.append(
            f"{label}: {len(added)} collected test(s) missing "
            f"from the manifest: {added}"
        )
    if removed:
        problems.append(
            f"{label}: {len(removed)} manifest entr(ies) no "
            f"longer collect: {removed}"
        )
    return problems


def coverage_problems(
    label: str,
    entries: list[dict],
    marker: str,
    universe: list[str],
) -> list[str]:
    """Every universe nodeid selected by exactly one shard."""
    uset = set(universe)
    counts: dict[str, int] = {}
    for entry in entries:
        for nodeid in selection_from_universe(
            shard_selection(entry), uset
        ):
            counts[nodeid] = counts.get(nodeid, 0) + 1
    problems = []
    missing = sorted(set(universe) - set(counts))
    doubled = sorted(n for n, c in counts.items() if c > 1)
    stray = sorted(set(counts) - set(universe))
    if missing:
        problems.append(
            f"{label}: {len(missing)} test(s) selected by NO "
            f"shard: {missing} — assign their file to a shard "
            "(files/nodeids in the workflow matrix)"
        )
    if doubled:
        problems.append(
            f"{label}: {len(doubled)} test(s) selected by MORE "
            f"THAN ONE shard: {doubled}"
        )
    if stray:
        problems.append(
            f"{label}: shards select {len(stray)} nodeid(s) "
            f"outside the {marker!r} selection: {stray}"
        )
    return problems


def _matrix_expansion(
    workflow: str, job_name: str, matrix_def: object
) -> tuple[int, str | None]:
    """(expanded count, problem) for one job's strategy.matrix.

    Counts EXACTLY the shapes this counter can model, and refuses
    the rest with a problem naming the workflow and the job: a
    guard that undercounts silently is worse than no guard — it
    certifies a budget it never measured. (The first version of
    this counter counted any matrix containing `include` as
    len(include) alone and skipped non-list dimensions, so a
    dimensions+include matrix counted 1 where GitHub runs 5.)

    Modelled: no matrix (1 job); an `include`-only matrix (one
    job per entry, as both gate workflows use); pure list
    dimensions (the cartesian product); list dimensions with
    `exclude` (a combination is removed iff some exclude entry
    matches it on every key the entry names — enumerated
    exactly, so an entry naming a subset of the dimensions
    removes every combination it matches).

    Refused: dimensions combined with `include` (whether an
    entry merges into an existing combination or appends a new
    job depends on its keys in a way this counter does not
    model); a dimension that is not a list (a scalar, or a
    `${{ ... }}` expression whose value only GitHub knows at
    run time); a matrix that is not a mapping at all (an
    expression); `exclude` naming a key that is not a dimension.
    """
    label = f"{workflow} job {job_name!r}"
    advice = (
        "so the per-push job budget cannot be proven; write the "
        "matrix as literal lists, dimensions with `exclude`, or "
        "`include` entries alone (WO-CI-SPEED task 5)"
    )
    if matrix_def is None:
        return 1, None
    if not isinstance(matrix_def, dict):
        return 0, (
            f"{label}: strategy.matrix is not a mapping "
            f"({matrix_def!r}) — its expanded job count is "
            f"unknowable statically, {advice}"
        )
    if not matrix_def:
        return 1, None
    include = matrix_def.get("include")
    exclude = matrix_def.get("exclude")
    dims = {
        key: value
        for key, value in matrix_def.items()
        if key not in ("include", "exclude")
    }
    if include is not None and dims:
        return 0, (
            f"{label}: strategy.matrix combines dimension(s) "
            f"{sorted(dims)} with `include` — the expanded job "
            f"count cannot be computed exactly here, {advice}"
        )
    if include is not None:
        if not isinstance(include, list):
            return 0, (
                f"{label}: strategy.matrix `include` is not a "
                f"list ({include!r}), {advice}"
            )
        return len(include), None
    if not dims:
        return 0, (
            f"{label}: strategy.matrix has `exclude` but no "
            f"dimensions for it to subtract from, {advice}"
        )
    for key, value in dims.items():
        if not isinstance(value, list):
            return 0, (
                f"{label}: matrix dimension {key!r} is not a "
                f"list ({value!r}) — its expanded count is only "
                f"known to GitHub at run time, {advice}"
            )
    if exclude is None:
        count = 1
        for value in dims.values():
            count *= len(value)
        return count, None
    if not isinstance(exclude, list) or any(
        not isinstance(entry, dict) for entry in exclude
    ):
        return 0, (
            f"{label}: strategy.matrix `exclude` is not a list "
            f"of mappings ({exclude!r}), {advice}"
        )
    for entry in exclude:
        unknown = sorted(set(entry) - set(dims))
        if unknown:
            return 0, (
                f"{label}: `exclude` entry {entry!r} names "
                f"key(s) {unknown} that are not matrix "
                f"dimensions, {advice}"
            )
    keys = list(dims)
    kept = 0
    for combo in itertools.product(*(dims[key] for key in keys)):
        values = dict(zip(keys, combo, strict=True))
        if any(
            all(values[key] == value for key, value in entry.items())
            for entry in exclude
        ):
            continue
        kept += 1
    return kept, None


def expanded_job_count(yml_path: Path) -> tuple[int, list[str]]:
    """How many jobs one run of this workflow creates, and the
    budget problems for any job whose matrix shape cannot be
    counted exactly (see _matrix_expansion). An uncountable job
    contributes 0 to the count and one problem each, so the
    budget check fails closed instead of certifying a number
    it never measured."""
    data = yaml.safe_load(yml_path.read_text(encoding="utf-8"))
    jobs = data.get("jobs") if isinstance(data, dict) else None
    if not isinstance(jobs, dict):
        return 0, [
            f"{yml_path.name}: no `jobs` mapping — the expanded "
            "job count cannot be computed, so the per-push job "
            "budget cannot be proven (WO-CI-SPEED task 5)"
        ]
    total = 0
    problems: list[str] = []
    for job_name, job in jobs.items():
        strategy = job.get("strategy") if isinstance(job, dict) else None
        matrix_def = (
            strategy.get("matrix")
            if isinstance(strategy, dict)
            else None
        )
        count, problem = _matrix_expansion(
            yml_path.name, str(job_name), matrix_def
        )
        total += count
        if problem is not None:
            problems.append(problem)
    return total, problems


def job_budget_problems() -> list[str]:
    """The two gate workflows together must stay within the
    per-push job budget (see JOB_BUDGET_PER_PUSH). A matrix
    shape the counter cannot model is itself a problem: the
    budget is then unproven, which fails this check exactly
    as an over-budget count does."""
    ci, ci_problems = expanded_job_count(CI_YML)
    sandbox, sandbox_problems = expanded_job_count(SANDBOX_YML)
    problems = ci_problems + sandbox_problems
    if problems:
        return problems
    if ci + sandbox <= JOB_BUDGET_PER_PUSH:
        return []
    return [
        f"job budget exceeded: ci.yml ({ci}) + sandbox-tests.yml "
        f"({sandbox}) = {ci + sandbox} jobs per push > "
        f"{JOB_BUDGET_PER_PUSH} (the GitHub Free plan's "
        "concurrent-job cap; jobs past it queue behind finished "
        "ones) — merge shards or move a job out of the per-push "
        "workflows (WO-CI-SPEED task 5)"
    ]


def counts_mode(marker: str, tokens: list[str]) -> int:
    """--counts: a shard job's selection counts, read from the
    generated manifests — no collection in the shard job at all.

    The manifests are proven equal to real collection by the
    check mode in the ci workflow's lint job on the same commit;
    the two manifests must also be disjoint (the partition),
    which is asserted here on every call.
    """
    host = read_manifest(HOST_MANIFEST)
    sandbox = read_manifest(SANDBOX_MANIFEST)
    overlap = sorted(set(host) & set(sandbox))
    if overlap:
        print(
            "partition broken: "
            f"{len(overlap)} nodeid(s) in BOTH manifests: {overlap}"
        )
        return 1
    if marker == "sandbox":
        universe = sandbox
    elif marker == "not sandbox":
        universe = host
    else:
        print(f"--counts: unknown marker expression {marker!r}")
        return 2
    shard = selection_from_universe(tokens, set(universe))
    print(
        f"collection: full={len(host) + len(sandbox)} "
        f"host={len(host)} sandbox={len(sandbox)} "
        f"(from the generated manifests)"
    )
    print(f"shard_count={len(shard)}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--write", action="store_true",
        help="regenerate both manifests from real collection",
    )
    ap.add_argument(
        "--counts", metavar="MARKER", default=None,
        help="print a shard's selection counts for this marker "
        "expression ('sandbox' or 'not sandbox') from the "
        "manifests, without collecting; the shard's selection "
        "tokens follow after --",
    )
    ap.add_argument("selection", nargs=argparse.REMAINDER)
    args = ap.parse_args()

    if args.counts is not None:
        tokens = [t for t in args.selection if t != "--"]
        return counts_mode(args.counts, tokens)

    rows = collect_all()
    full = sorted(n for n, _ in rows)
    host = sorted(n for n, s in rows if not s)
    sandbox = sorted(n for n, s in rows if s)
    print(f"collection: full={len(full)} host={len(host)} "
          f"sandbox={len(sandbox)}")

    if args.write:
        write_manifest(HOST_MANIFEST, host)
        write_manifest(SANDBOX_MANIFEST, sandbox)
        print(f"--write: {HOST_MANIFEST.name} ({len(host)}), "
              f"{SANDBOX_MANIFEST.name} ({len(sandbox)})")
        print("Now re-run without --write: the matrix coverage "
              "checks still apply (a new test FILE may need a "
              "shard assignment).")
        # Fall through and verify coverage against the fresh
        # manifests, so --write is not done until CI would pass.

    problems: list[str] = []
    if sorted(host + sandbox) != full:
        problems.append(
            f"partition broken: host({len(host)}) + "
            f"sandbox({len(sandbox)}) != full({len(full)}) as "
            "nodeid sets"
        )
    problems += diff_problems(
        "host manifest", host, read_manifest(HOST_MANIFEST)
    )
    problems += diff_problems(
        "sandbox manifest", sandbox, read_manifest(SANDBOX_MANIFEST)
    )
    problems += coverage_problems(
        "ci.yml host matrix", matrix(CI_YML, "host"),
        "not sandbox", host,
    )
    problems += coverage_problems(
        "sandbox-tests.yml matrix", matrix(SANDBOX_YML, "sandbox"),
        "sandbox", sandbox,
    )
    problems += job_budget_problems()

    if problems:
        print("\nTEST MANIFEST MISMATCH:")
        for p in problems:
            print(f"  - {p}")
        print(
            "\nFix: python scripts/check_test_manifest.py "
            "--write regenerates the manifests. A test selected "
            "by no shard additionally needs its file assigned "
            "in the workflow matrix (files/nodeids) — a human "
            "call, named above."
        )
        return 1
    print("OK: manifests and shard matrices match the collected "
          "suite exactly")
    return 0


if __name__ == "__main__":
    sys.exit(main())
