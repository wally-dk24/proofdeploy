"""Auth proof harness (plan 1.11; D87).

Provisions registered sides through the SAME sandboxed runner path
as the readiness pass (``Orchestrator._provision_side_sandbox``),
then — while the side is still live — runs three executor probes
against the side's Admin API to prove the contract's declared auth
actually works end to end:

1. an authenticated request (the executor's registered credential
   provider mints a fresh token for it) — expected 200;
2. the same authenticated request again — expected 200, with a
   SECOND mint (one mint per request, counted from the provider's
   own detail lines);
3. the same path with no Authorization header — expected 401
   (auth is really enforced, not merely accepted).

The proof exists because a READY side proves build + start only:
the registered go/no-go rule for Ghost asks for build, start, seed
and auth with JWT refresh. The measured run needs the same wiring —
without the contract's seed + auth, the checkpoint's Ghost unit
could not have logged in either (D87).

Guarantees (mirroring readiness/provision.py):

* NO measured records, NO scores, NO model call. Output is a rows
  JSON labelled ``"auth-proof"`` plus per-side logs under pd-logs/.
* Engine reuse only: provisioning, the executor and the registered
  ``ghost_jwt`` provider are the engine's own; this module adds no
  auth logic of its own. The probes are plain executor probes whose
  ``${AUTH_TOKEN}`` header the provider fills per request.
* The harness commit for a side carries BOTH the repo's committed
  contract (as ``proofdeploy.yml``) and the contract seed's script
  (as ``ghost-seed.js`` at the tree root, the path the contract's
  ``seed:`` command names): tree delta is exactly those two files
  on the registered SHA. The plain readiness commit carries the
  contract only because its contract's seed — where one exists —
  names a script inside the app tree itself.
* Every string the harness emits (rows and dumped logs) is scrubbed
  BY VALUE with the side's own secret set: the seed-written Admin
  API key, the per-side generated bootstrap values, and every token
  the provider minted during the proof — plus a JWT-shape sweep
  for anything those values miss. Replacement counts per category
  are recorded on the row (``redactions``); the values themselves
  never leave the runner.
* A READY side is a live container: it is stopped in a ``finally``
  so no outcome can leak it.

Exit status: 0 when the harness ran and wrote its rows (a side
whose proof fails is data, not a harness failure); 2 when the
harness itself failed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import readiness.provision as rp
from proofdeploy.orchestrator import _free_port
from proofdeploy.sandbox import assert_measurement_gate

ROOT = Path(__file__).resolve().parent
SEEDS_DIR = ROOT / "seeds"

# The one endpoint the proof exercises: Ghost's Admin API site
# endpoint — the same endpoint the WO-8.3 sandbox proof used.
ADMIN_PATH = "/ghost/api/admin/site/"

# The contract seed's script, by the name the contract's `seed:`
# command invokes it and the seeds directory it is committed from.
SEED_SNAPSHOT_NAME = "ghost-seed.js"
SEED_SOURCE = SEEDS_DIR / SEED_SNAPSHOT_NAME

_MINT_DETAIL_PREFIX = "credential provider: minted ghost_jwt"
_JWT_SHAPE = re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")


def commit_contract_and_seed(
    clone: Path, registered_sha: str, contract: Path, seed_script: Path
) -> str:
    """Create the harness commit for one auth-proof side: parent =
    the registered SHA, tree delta = exactly ``+proofdeploy.yml``
    and ``+ghost-seed.js`` (the contract's seed script, committed
    from readiness/seeds/). Plumbing only (no checkout), so the
    target tree is untouched. Mirrors rp.commit_contract, which
    adds the contract alone."""
    blobs = {
        "proofdeploy.yml": rp._git(clone, "hash-object", "-w", str(contract)),
        SEED_SNAPSHOT_NAME: rp._git(
            clone, "hash-object", "-w", str(seed_script)
        ),
    }
    with tempfile.NamedTemporaryFile(delete=False) as tmp:
        index = tmp.name
    try:
        env = {"GIT_INDEX_FILE": index}
        rp._git(clone, "read-tree", registered_sha, env_extra=env)
        for name, blob in blobs.items():
            rp._git(
                clone, "update-index", "--add", "--cacheinfo",
                f"100644,{blob},{name}", env_extra=env,
            )
        tree = rp._git(clone, "write-tree", env_extra=env)
    finally:
        os.unlink(index)
    return rp._git(
        clone, "commit-tree", tree, "-p", registered_sha,
        "-m", "auth-proof: add proofdeploy.yml + ghost-seed.js (D87)",
        env_extra=rp._GIT_ENV,
    )


class Redactor:
    """Scrub a side's secret VALUES out of emitted text, counting
    replacements per category. Values are registered as the proof
    learns them (bootstrap values, the seed-written key, minted
    tokens); a JWT-shape sweep catches any token-shaped string the
    value set missed. Counts — never values — are what the row
    records."""

    def __init__(self) -> None:
        self._values: dict[str, list[str]] = {
            "admin_key": [],
            "bootstrap_value": [],
            "minted_jwt": [],
        }
        self.counts: dict[str, int] = {
            "admin_key": 0,
            "bootstrap_value": 0,
            "minted_jwt": 0,
            "jwt_shape": 0,
        }

    def add(self, category: str, value: Any) -> None:
        if isinstance(value, str) and len(value) >= 4:
            bucket = self._values[category]
            if value not in bucket:
                bucket.append(value)

    def add_many(self, category: str, values: Any) -> None:
        if isinstance(values, dict):
            values = values.values()
        for value in values or []:
            self.add(category, value)

    def scrub(self, text: str) -> str:
        for category, values in self._values.items():
            # Longest first: a value that contains another (the key
            # pair contains its secret half) must be replaced whole.
            for value in sorted(values, key=len, reverse=True):
                hits = text.count(value)
                if hits:
                    self.counts[category] += hits
                    text = text.replace(value, f"<redacted:{category}>")
        text, swept = _JWT_SHAPE.subn("<redacted:minted_jwt>", text)
        self.counts["jwt_shape"] += swept
        return text

    def scrub_obj(self, obj: Any) -> Any:
        if isinstance(obj, str):
            return self.scrub(obj)
        if isinstance(obj, dict):
            return {key: self.scrub_obj(value) for key, value in obj.items()}
        if isinstance(obj, list):
            return [self.scrub_obj(value) for value in obj]
        return obj


def _authenticated_probe() -> dict[str, Any]:
    return {
        "act": {
            "method": "GET",
            "path": ADMIN_PATH,
            "headers": {"Authorization": "Ghost ${AUTH_TOKEN}"},
        },
        "assert": [{"type": "status", "equals": 200}],
    }


def _unauthenticated_probe() -> dict[str, Any]:
    return {
        "act": {"method": "GET", "path": ADMIN_PATH},
        "assert": [{"type": "status", "equals": 401}],
    }


def _mint_count(details: list[str]) -> int:
    return sum(1 for d in details if d.startswith(_MINT_DETAIL_PREFIX))


def _seed_evidence(side: Any) -> dict[str, Any]:
    """What the seed left in the snapshot, read host-side: the key
    file's presence (never its content) and the SQLite files under
    the app's content data dir — the file the app side serves from,
    which the seed's temporary boot wrote (its own log line names
    it too, from inside the install container)."""
    snapshot = Path(side.snapshot_dir)
    key_file = snapshot / ".pd-auth" / "admin_key"
    data_dir = snapshot / "ghost" / "core" / "content" / "data"
    db_files: list[str] = []
    if data_dir.is_dir():
        for entry in sorted(data_dir.iterdir()):
            if ".db" in entry.name and entry.is_file():
                db_files.append(
                    f"ghost/core/content/data/{entry.name} "
                    f"({entry.stat().st_size} bytes)"
                )
    return {
        "key_file_present": key_file.is_file(),
        "key_file": ".pd-auth/admin_key",
        "sqlite_files": db_files,
    }


def _request_record(
    result: Any, *, authenticated: bool, redactor: Redactor
) -> dict[str, Any]:
    """One proof request as the row records it: method, path, the
    Authorization header's SHAPE (never its value), the HTTP
    status the executor observed, the verdict, and how many tokens
    the provider minted for this request."""
    return {
        "method": "GET",
        "path": ADMIN_PATH,
        "authorization": (
            "Ghost <redacted:minted_jwt>" if authenticated else None
        ),
        "http_status": result.http_status,
        "verdict": result.verdict.value,
        "reason": result.reason.value if result.reason else None,
        "mints": _mint_count(result.details),
        "details": [redactor.scrub(d) for d in result.details],
    }


def _register_result_secrets(redactor: Redactor, result: Any) -> None:
    redactor.add_many("minted_jwt", result.minted_tokens)
    redactor.add_many("bootstrap_value", result.bootstrap_values)
    for value in (result.captured_bindings or {}).values():
        if isinstance(value, list):
            redactor.add_many("bootstrap_value", value)
        else:
            redactor.add("bootstrap_value", value)


def auth_proof_side(
    orch: Any,
    clone: Path,
    unit: dict[str, Any],
    side_spec: dict[str, Any],
    contract: Path,
    attempt: int,
    workdir: Path,
    network_mode: str | None,
) -> dict[str, Any]:
    """Provision ONE side and, while it is live, run the three
    auth probes. Returns the side's auth-proof row. Never raises
    for a provisioning outcome (typed into the row, mirroring
    rp.provision_side); the raising paths are the harness's own
    (fetch / harness commit), which the caller types."""
    rp.fetch_sha(clone, str(side_spec["sha"]))
    harness_sha = commit_contract_and_seed(
        clone, str(side_spec["sha"]), contract, SEED_SOURCE
    )
    unit_dir = workdir / "units" / str(unit["unit_id"]) / f"a{attempt}"
    unit_dir.mkdir(parents=True, exist_ok=True)
    tag = rp._TAG_BY_ROLE[str(side_spec["role"])]
    redactor = Redactor()
    side: Any = None
    try:
        side = orch._provision_side_sandbox(
            harness_sha, _free_port(), unit_dir, tag
        )
        row = rp.row_from_provision(
            unit, side_spec, harness_sha, attempt, network_mode, side
        )
        row["kind"] = "auth-proof"
        logs = list(side.provision.logs)
        redactor.add_many(
            "bootstrap_value", getattr(side, "bootstrap_env", None)
        )
        redactor.add_many(
            "bootstrap_value", getattr(side, "generated_secrets", None)
        )
        if not side.provision.ready:
            row["seed"] = {"key_file_present": False, "sqlite_files": []}
            row["auth"] = {
                "endpoint": ADMIN_PATH,
                "status": "NOT_RUN",
                "reason": "side did not reach READY",
                "requests": [],
                "mint_count_after_two_authenticated": 0,
                "passed": False,
            }
        else:
            seed_evidence = _seed_evidence(side)
            key_path = Path(side.snapshot_dir) / ".pd-auth" / "admin_key"
            if key_path.is_file():
                redactor.add(
                    "admin_key", key_path.read_text(encoding="utf-8").strip()
                )
            executor = orch._make_executor(side)
            # The executor's credential config read the key host-side
            # after READY; its value joined the side's secret set.
            redactor.add_many(
                "bootstrap_value", getattr(side, "generated_secrets", None)
            )
            results = [
                executor.run_probe(_authenticated_probe(), index=0),
                executor.run_probe(_authenticated_probe(), index=1),
                executor.run_probe(_unauthenticated_probe(), index=2),
            ]
            for result in results:
                _register_result_secrets(redactor, result)
            records = [
                _request_record(results[0], authenticated=True, redactor=redactor),
                _request_record(results[1], authenticated=True, redactor=redactor),
                _request_record(
                    results[2], authenticated=False, redactor=redactor
                ),
            ]
            mint_total = records[0]["mints"] + records[1]["mints"]
            passed = (
                seed_evidence["key_file_present"]
                and records[0]["http_status"] == 200
                and records[1]["http_status"] == 200
                and records[2]["http_status"] == 401
                and mint_total == 2
                and all(r["verdict"] == "pass" for r in records)
            )
            row["seed"] = seed_evidence
            row["auth"] = {
                "endpoint": ADMIN_PATH,
                "status": "PROVEN" if passed else "FAILED",
                "reason": None if passed else "see requests",
                "requests": records,
                "mint_count_after_two_authenticated": mint_total,
                "passed": passed,
            }
            logs.append(
                "auth proof: "
                + "; ".join(
                    f"request {i + 1} -> {r['http_status']} "
                    f"(verdict {r['verdict']}, mints {r['mints']})"
                    for i, r in enumerate(records)
                )
                + f"; seed key file present: "
                f"{seed_evidence['key_file_present']}; "
                f"sqlite files: {seed_evidence['sqlite_files']}"
            )
    except (FileNotFoundError, ValueError) as exc:
        row = rp.row_from_exception(
            unit, side_spec, harness_sha, attempt, network_mode, exc
        )
        row["kind"] = "auth-proof"
        row["seed"] = {"key_file_present": False, "sqlite_files": []}
        row["auth"] = {
            "endpoint": ADMIN_PATH,
            "status": "NOT_RUN",
            "reason": f"provision raised: {exc}",
            "requests": [],
            "mint_count_after_two_authenticated": 0,
            "passed": False,
        }
        logs = [f"auth proof provision raised: {exc}"]
    finally:
        if side is not None and side.provision is not None:
            proc = side.provision.proc
            if proc is not None:
                proc.stop()
    scrubbed_row: dict[str, Any] = redactor.scrub_obj(row)
    row = scrubbed_row
    rp.dump_logs(
        f"authproof-{unit['unit_id']}-{tag}-a{attempt}",
        [redactor.scrub(entry) for entry in logs],
    )
    # Counts snapshot LAST, after the log dump's scrubbing too, so
    # the row's redaction counts cover everything the side emitted.
    row["redactions"] = dict(redactor.counts)
    return row


def auth_proof_repo(
    manifest: dict[str, Any],
    repo: str,
    workdir: Path,
    attempt: int,
    only: list[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    """Run the auth proof for the repo's units (or an ``only``
    subset of (unit_id, role) sides, for the one-side rehearsal).
    Run-level setup mirrors rp.provision_repo: the measurement
    gate, then ONE network mode resolved once for the whole run."""
    units = rp.units_for_repo(manifest, repo)
    if not units:
        raise ValueError(f"no units for repo {repo!r} in the manifest")
    if not SEED_SOURCE.is_file():
        raise ValueError(f"contract seed script missing: {SEED_SOURCE}")
    workdir = workdir.resolve()
    assert_measurement_gate()
    clone = rp.ensure_clone(workdir, repo)
    orch = rp.make_orchestrator(clone, workdir, rp.contract_env_keys(repo))
    orch._resolve_run_network_mode()
    network_mode = orch._run_network_mode
    contract = rp.contract_path(repo)
    rows: list[dict[str, Any]] = []

    def record_harness_error(
        unit: dict[str, Any], side_spec: dict[str, Any], exc: Exception
    ) -> dict[str, Any]:
        row = rp._harness_error_row(unit, side_spec, attempt, network_mode, exc)
        row["kind"] = "auth-proof"
        rp.dump_logs(
            f"authproof-{unit['unit_id']}-"
            f"{rp._TAG_BY_ROLE[str(side_spec['role'])]}-a{attempt}",
            [f"auth proof provision raised: {exc}"],
        )
        return row

    for unit in units:
        try:
            rp.fetch_sha(clone, rp.cutoff_sha(unit))
            orch._run_lock_cutoff = orch._commit_cutoff(rp.cutoff_sha(unit))
        except Exception as exc:
            for side_spec in unit["sides"]:
                if only is not None and (
                    unit["unit_id"], side_spec["role"]
                ) not in only:
                    continue
                row = record_harness_error(unit, side_spec, exc)
                rows.append(row)
                print(json.dumps(row), flush=True)
            continue
        for side_spec in unit["sides"]:
            if only is not None and (
                unit["unit_id"], side_spec["role"]
            ) not in only:
                continue
            try:
                row = auth_proof_side(
                    orch, clone, unit, side_spec, contract,
                    attempt, workdir, network_mode,
                )
            except Exception as exc:
                row = record_harness_error(unit, side_spec, exc)
            rows.append(row)
            print(json.dumps(row), flush=True)
    return {
        "kind": "auth-proof",
        "repo": repo,
        "attempt": attempt,
        "network_mode": network_mode,
        "rows": rows,
    }


def _parse_only(values: list[str] | None) -> list[tuple[str, str]] | None:
    if not values:
        return None
    only: list[tuple[str, str]] = []
    for value in values:
        unit_id, sep, role = value.partition(":")
        if not sep or not unit_id or not role:
            raise ValueError(f"--only expects <unit>:<role>, got {value!r}")
        only.append((unit_id, role))
    return only


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", required=True, help="owner/name from the manifest")
    ap.add_argument("--attempt", type=int, default=1)
    ap.add_argument("--out", type=Path, required=True, help="rows JSON output path")
    ap.add_argument("--workdir", type=Path, default=Path("readiness-work"))
    ap.add_argument("--manifest", type=Path, default=rp.DEFAULT_MANIFEST)
    ap.add_argument(
        "--only", action="append", default=None,
        help="<unit>:<role> side to prove (repeatable); default: all sides",
    )
    args = ap.parse_args(argv)
    manifest = rp.load_manifest(args.manifest)
    try:
        only = _parse_only(args.only)
        result = auth_proof_repo(
            manifest, args.repo, args.workdir, args.attempt, only=only
        )
    except (RuntimeError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"auth proof harness failed: {exc}", file=sys.stderr)
        return 2
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
