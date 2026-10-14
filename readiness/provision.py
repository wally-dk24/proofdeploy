"""Provisioning readiness pass harness (Addendum 64/65, WAL-112).

Provisions every registered unit's sides through the REAL sandboxed
runner path — ``Orchestrator._provision_side_sandbox``, the same
method a measured run uses — and writes one machine-readable row per
side. There is no model call, no probe, and no scoring in this path.

Guarantees (Addendum 65):

* The harness writes NO measured records and NO scores. It never
  builds an evidence record and never touches the record machinery;
  its output is a rows JSON labelled ``"readiness"`` plus the
  per-side provision logs under ``pd-logs/``.
* Each row carries BOTH SHAs: the unit's registered SHA (from
  ``readiness/manifest.json``, the committed run spec) and the
  harness commit actually provisioned. The provisioned tree must
  contain ``proofdeploy.yml`` at its root and the loader reads it
  from inside the snapshot — there is no overlay — so the harness
  creates one commit per side whose parent is the registered SHA
  and whose tree delta is exactly ``+proofdeploy.yml`` (the repo's
  committed contract from ``readiness/contracts/``).
* One network mode per run, resolved once via the orchestrator's
  own ``_resolve_run_network_mode`` and recorded on every row.
* The REG-A lock cutoff for a unit comes from its fix commit
  (bug units) or its C commit (controls and cleans), exactly as
  ``measure_*`` sets it — never from the side's own commit.
* A READY side is a live container: it is stopped in a ``finally``
  so no outcome — row written, row failed, exception — can leak it.

Exit status: 0 when the harness ran and wrote its rows (a
non-READY side is data, not a harness failure); 2 when the harness
itself failed (measurement gate refused, manifest/clone errors).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from proofdeploy.orchestrator import MeasureConfig, Orchestrator, _free_port
from proofdeploy.sandbox import assert_measurement_gate
from proofdeploy.yml import load_contract

ROOT = Path(__file__).resolve().parent
DEFAULT_MANIFEST = ROOT / "manifest.json"
CONTRACTS_DIR = ROOT / "contracts"

# Roles whose commit provides the unit's REG-A lock cutoff: the fix
# commit for bug units; the single C commit for controls/cleans.
_TAG_BY_ROLE = {"fix^": "fix-parent", "fix": "fix", "C": "clean"}

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "proofdeploy-readiness",
    "GIT_AUTHOR_EMAIL": "readiness@proofdeploy.invalid",
    "GIT_COMMITTER_NAME": "proofdeploy-readiness",
    "GIT_COMMITTER_EMAIL": "readiness@proofdeploy.invalid",
}


def dump_logs(name: str, logs: list[str]) -> Path:
    """Write a side's logs to ``pd-logs/<name>.log``, the shape
    the sandbox workflows upload as artifacts. Mirrors
    conformance/kit.py's dump_logs exactly; kept local because
    conformance is a namespace package whose module mapping under
    mypy does not admit a cross-package import from checked code.
    """
    out = Path("pd-logs") / f"{name}.log"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(str(entry) for entry in logs), encoding="utf-8")
    return out


def repo_slug(repo: str) -> str:
    """``owner/name`` -> the contract/clone slug ``owner__name``."""
    return repo.replace("/", "__")


def contract_path(repo: str) -> Path:
    return CONTRACTS_DIR / f"{repo_slug(repo)}.yml"


def load_manifest(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def units_for_repo(manifest: dict[str, Any], repo: str) -> list[dict[str, Any]]:
    return [u for u in manifest["units"] if u["repo"] == repo]


def cutoff_sha(unit: dict[str, Any]) -> str:
    """The unit's REG-A cutoff commit: fix for bugs, C otherwise."""
    for side in unit["sides"]:
        if side["role"] in ("fix", "C"):
            return str(side["sha"])
    raise ValueError(f"unit {unit['unit_id']} has no fix/C side")


def _git(repo_dir: Path, *args: str, env_extra: dict[str, str] | None = None) -> str:
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run(
        ["git", "-C", str(repo_dir), *args],
        capture_output=True, text=True, check=True, timeout=900, env=env,
    )
    return proc.stdout.strip()


def ensure_clone(workdir: Path, repo: str) -> Path:
    """A checkout-less clone of the target: history is fetched per
    SHA, trees are never checked out, target content is never read
    by the harness itself (the snapshot step does the reading)."""
    clone = workdir / "repos" / repo_slug(repo)
    if not (clone / ".git").exists():
        clone.mkdir(parents=True)
        _git(clone, "init", "-q")
        _git(clone, "remote", "add", "origin", f"https://github.com/{repo}.git")
    return clone


def fetch_sha(clone: Path, sha: str) -> None:
    """Fetch one commit (with its full tree) if not already present."""
    proc = subprocess.run(
        ["git", "-C", str(clone), "cat-file", "-e", f"{sha}^{{commit}}"],
        capture_output=True, text=True, timeout=60,
    )
    if proc.returncode != 0:
        _git(clone, "fetch", "-q", "--depth", "1", "origin", sha)


def commit_contract(clone: Path, registered_sha: str, contract: Path) -> str:
    """Create the harness commit for one side: parent = the
    registered SHA, tree delta = exactly ``+proofdeploy.yml``.
    Plumbing only (no checkout), so the target tree is untouched."""
    blob = _git(clone, "hash-object", "-w", str(contract))
    with tempfile.NamedTemporaryFile(delete=False) as tmp:
        index = tmp.name
    try:
        env = {"GIT_INDEX_FILE": index}
        _git(clone, "read-tree", registered_sha, env_extra=env)
        _git(
            clone, "update-index", "--add", "--cacheinfo",
            f"100644,{blob},proofdeploy.yml", env_extra=env,
        )
        tree = _git(clone, "write-tree", env_extra=env)
    finally:
        os.unlink(index)
    return _git(
        clone, "commit-tree", tree, "-p", registered_sha,
        "-m", "readiness: add proofdeploy.yml (Addendum 64 pass)",
        env_extra=_GIT_ENV,
    )


def make_orchestrator(
    clone: Path, workdir: Path, public_env_keys: list[str]
) -> Orchestrator:
    cfg = MeasureConfig(
        repo_dir=clone,
        output_dir=workdir / "records",
        workdir=workdir / "work",
        no_skill=True,
        sandbox=True,
        public_contract_env_keys=public_env_keys,
    )
    return Orchestrator(cfg)


def contract_env_keys(repo: str) -> list[str]:
    """The repo contract's env names, to pass as the run's public
    (benign, non-secret) contract env keys — mirroring the tests'
    per-contract key lists. An unloadable contract yields no keys;
    provisioning then surfaces the loader's own error per side."""
    try:
        return sorted(load_contract(contract_path(repo)).env)
    except (FileNotFoundError, ValueError):
        return []


def _last_log_line(logs: list[str]) -> str | None:
    for entry in reversed(logs):
        text = str(entry).strip()
        if text:
            return text
    return None


def row_from_provision(
    unit: dict[str, Any],
    side_spec: dict[str, Any],
    harness_sha: str,
    attempt: int,
    fallback_network_mode: str | None,
    side: Any,
) -> dict[str, Any]:
    """Map a provisioned side onto its readiness row."""
    prov = side.provision
    plan: dict[str, Any] = prov.install_plan or {}
    status = "READY" if prov.ready else prov.status.value.upper()
    typed = plan.get("refusal") or prov.reason
    evidence = side.sandbox_evidence or {}
    network_mode = evidence.get("app_network_mode") or fallback_network_mode
    return {
        "kind": "readiness",
        "unit": unit["unit_id"],
        "repo": unit["repo"],
        "role": side_spec["role"],
        "registered_sha": side_spec["sha"],
        "harness_sha": harness_sha,
        "declared_runtime": plan.get("declared_range"),
        "selected_runtime": prov.runtime,
        "runtime_version": prov.runtime_version or plan.get("selected_version"),
        "package_manager": plan.get("package_manager") or prov.package_manager,
        "status": status,
        "typed_reason": typed,
        "last_error": None if prov.ready else _last_log_line(prov.logs),
        "install_plan_sha256": prov.install_plan_sha256,
        "network_mode": network_mode,
        "attempt": attempt,
    }


def row_from_exception(
    unit: dict[str, Any],
    side_spec: dict[str, Any],
    harness_sha: str,
    attempt: int,
    network_mode: str | None,
    exc: Exception,
) -> dict[str, Any]:
    """Type a raising provision path into a row (design obstacle
    O3): an undetected runtime is the typed ``runtime not
    supported``; anything else keeps the exception's own message as
    its typed reason, verbatim."""
    message = str(exc)
    if isinstance(exc, ValueError) and message.startswith("cannot detect runtime"):
        typed: str | None = "runtime not supported"
    else:
        typed = message
    return {
        "kind": "readiness",
        "unit": unit["unit_id"],
        "repo": unit["repo"],
        "role": side_spec["role"],
        "registered_sha": side_spec["sha"],
        "harness_sha": harness_sha,
        "declared_runtime": None,
        "selected_runtime": None,
        "runtime_version": None,
        "package_manager": None,
        "status": "BUILD_FAILED",
        "typed_reason": typed,
        "last_error": message,
        "install_plan_sha256": None,
        "network_mode": network_mode,
        "attempt": attempt,
    }


def provision_side(
    orch: Orchestrator,
    clone: Path,
    unit: dict[str, Any],
    side_spec: dict[str, Any],
    contract: Path,
    attempt: int,
    workdir: Path,
    network_mode: str | None,
) -> dict[str, Any]:
    """Provision ONE side through the measured path's own method
    and return its readiness row. Never raises for a build/start
    outcome; the raising paths are typed into the row."""
    fetch_sha(clone, str(side_spec["sha"]))
    harness_sha = commit_contract(clone, str(side_spec["sha"]), contract)
    unit_dir = workdir / "units" / str(unit["unit_id"])
    unit_dir.mkdir(parents=True, exist_ok=True)
    tag = _TAG_BY_ROLE[str(side_spec["role"])]
    side: Any = None
    try:
        side = orch._provision_side_sandbox(
            harness_sha, _free_port(), unit_dir, tag
        )
        row = row_from_provision(
            unit, side_spec, harness_sha, attempt, network_mode, side
        )
        logs = list(side.provision.logs)
    except (FileNotFoundError, ValueError) as exc:
        row = row_from_exception(
            unit, side_spec, harness_sha, attempt, network_mode, exc
        )
        logs = [f"readiness provision raised: {exc}"]
    finally:
        if side is not None and side.provision is not None:
            proc = side.provision.proc
            if proc is not None:
                proc.stop()
    dump_logs(f"readiness-{unit['unit_id']}-{tag}", logs)
    return row


def provision_repo(
    manifest: dict[str, Any],
    repo: str,
    workdir: Path,
    attempt: int,
    only: list[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    """Provision the repo's units (or an ``only`` subset of
    (unit_id, role) sides, for reruns). Run-level setup mirrors
    measure_*: the measurement gate, then ONE network mode resolved
    once for the whole run."""
    units = units_for_repo(manifest, repo)
    if not units:
        raise ValueError(f"no units for repo {repo!r} in the manifest")
    assert_measurement_gate()
    clone = ensure_clone(workdir, repo)
    orch = make_orchestrator(clone, workdir, contract_env_keys(repo))
    orch._resolve_run_network_mode()
    network_mode = orch._run_network_mode
    contract = contract_path(repo)
    rows: list[dict[str, Any]] = []
    for unit in units:
        fetch_sha(clone, cutoff_sha(unit))
        orch._run_lock_cutoff = orch._commit_cutoff(cutoff_sha(unit))
        for side_spec in unit["sides"]:
            if only is not None and (unit["unit_id"], side_spec["role"]) not in only:
                continue
            row = provision_side(
                orch, clone, unit, side_spec, contract,
                attempt, workdir, network_mode,
            )
            rows.append(row)
            print(json.dumps(row), flush=True)
    return {
        "kind": "readiness",
        "repo": repo,
        "attempt": attempt,
        "network_mode": network_mode,
        "rows": rows,
    }


def _rerun_targets(prior_path: Path) -> list[tuple[str, str]]:
    data = json.loads(prior_path.read_text(encoding="utf-8"))
    prior_rows = data["rows"] if isinstance(data, dict) else data
    return [
        (str(r["unit"]), str(r["role"]))
        for r in prior_rows
        if r["status"] != "READY"
    ]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", required=True, help="owner/name from the manifest")
    ap.add_argument("--attempt", type=int, default=1)
    ap.add_argument("--out", type=Path, required=True, help="rows JSON output path")
    ap.add_argument("--workdir", type=Path, default=Path("readiness-work"))
    ap.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    ap.add_argument(
        "--rerun-non-ready", type=Path, default=None,
        help="prior rows JSON; re-provision only its non-READY sides",
    )
    args = ap.parse_args(argv)
    manifest = load_manifest(args.manifest)
    only = (
        _rerun_targets(args.rerun_non_ready)
        if args.rerun_non_ready is not None
        else None
    )
    try:
        result = provision_repo(
            manifest, args.repo, args.workdir, args.attempt, only=only
        )
    except (RuntimeError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"readiness harness failed: {exc}", file=sys.stderr)
        return 2
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
