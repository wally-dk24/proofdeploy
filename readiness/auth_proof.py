"""Generic auth proof harness (plan 1.14; D87, generalized D137).

Provisions registered sides through the SAME sandboxed runner path
as the readiness pass (``Orchestrator._provision_side_sandbox``),
then — while the side is still live — runs three executor probes
against the side's API to prove the contract's declared auth
actually works end to end (REG-E item 4 shape):

1. an authenticated request — the executor's registered credential
   provider acquires a fresh credential for it — expected 200;
2. the same authenticated request again — expected 200, with its
   OWN fresh acquisition (the measured path builds a fresh context
   per probe, so each request acquires as the provider's mode
   dictates: a mint for ghost_jwt, a login for login/refresh
   modes), acquisitions counted BY KIND from the provider's own
   detail lines;
3. the same request with no credentials at all — run through a
   copy of the executor with the credential provider and any
   static token REMOVED, so the leg performs no acquisition and
   sends no Authorization header (through the provider-configured
   executor the provider's proactive ensure would still acquire,
   which is a credential event, not a bare request) — expected to
   be refused with the repo's registered status (auth is really
   enforced, not merely accepted).

The verdict is per-leg, never summed: each authenticated leg must
show exactly one acquisition of the repo's registered kind, and
the bare leg zero acquisitions. Where a repo's API answers a
refusal as HTTP 200 with an error body (GraphQL), the spec
additionally registers generic body checks — a JSON path that
must be non-null on the authenticated legs, and a JSON path that
must equal a registered value on the bare leg — evaluated over
the recorded response bodies; status alone proves nothing there.
Repos with real HTTP refusals register no body checks.

The driver is contract-driven (D137): the provider configuration
and the token presentation come from the repo's committed contract
(the presentation scheme is the one the contract's fixture text
names, in its fixed sentence "in the form '<Scheme> <token>'").
The only per-repo inputs are the proof endpoint, the expected
unauthenticated status, the expected acquisition kind and any
body checks, registered in PROOF_SPECS below — adding a repo is
a registry entry plus its contract, never new driver code.
This module adds no auth logic of its own: provisioning, the
executor and the registered providers are the engine's own, and
the probes are plain executor probes whose ``${AUTH_TOKEN}``
header the provider fills per request.

Guarantees (mirroring readiness/provision.py):

* NO measured records, NO scores, NO model call. Output is a rows
  JSON labelled ``"auth-proof"`` plus per-side logs under pd-logs/.
* The harness commit for a side carries the repo's committed
  contract (as ``proofdeploy.yml``) plus any readiness/seeds/ file
  the contract's ``seed:`` command names by bare filename at the
  tree root (Ghost's ghost-seed.js) — the tree delta is exactly
  those files on the registered SHA.
* Every string the harness emits (rows and dumped logs) is scrubbed
  BY VALUE with the side's own secret set: the seed-written key
  file's content (when the contract declares one), the per-side
  generated bootstrap values, and every token the provider
  acquired during the proof — plus a JWT-shape sweep for anything
  those values miss. Replacement counts per category are recorded
  on the row (``redactions``); the values themselves never leave
  the runner.
* A READY side is a live container: it is stopped in a ``finally``
  so no outcome can leak it.

Exit status: 0 when the harness ran and wrote its rows (a side
whose proof fails is data, not a harness failure); 2 when the
harness itself failed.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import readiness.provision as rp
from proofdeploy.orchestrator import _free_port
from proofdeploy.sandbox import assert_measurement_gate
from proofdeploy.yml import RepoContract, load_contract

ROOT = Path(__file__).resolve().parent
SEEDS_DIR = ROOT / "seeds"

# The contract seed's script for Ghost, by the name the contract's
# `seed:` command invokes it and the seeds directory it is
# committed from. Other repos' seeds name in-tree scripts (or no
# script at all) and add nothing to the harness commit; see
# seed_snapshot_files.
SEED_SNAPSHOT_NAME = "ghost-seed.js"
SEED_SOURCE = SEEDS_DIR / SEED_SNAPSHOT_NAME


@dataclass(frozen=True)
class ProofSpec:
    """The per-repo proof inputs (D137): how to call the proof
    endpoint, the status an unauthenticated request to it must be
    refused with, and the acquisition kind each authenticated leg
    must perform exactly once. Everything else — the provider,
    the token presentation — is read from the repo's committed
    contract.

    The optional body checks are generic (never repo-specific
    code): ``authenticated_body_path`` names a JSON path in the
    response body that must resolve non-null on both authenticated
    legs; ``unauthenticated_body_path`` / ``..._value`` name a
    JSON path on the bare leg that must equal the registered
    value. They exist for APIs that serve a refusal as HTTP 200
    with an error body, where the status alone proves nothing."""

    method: str
    path: str
    unauthenticated_status: int
    acquisition_kind: str
    body: dict[str, Any] | None = None
    authenticated_body_path: str | None = None
    unauthenticated_body_path: str | None = None
    unauthenticated_body_value: Any = None


# D91: Ghost's Admin API tags endpoint — its browse requires
# permission and the seeded integration's "Admin Integration" role
# covers it, so an authenticated read answers 200 while a request
# with no credentials is refused with 403 (authorizeAdminApi
# raises NoPermissionError). The site endpoint the WO-8.3 proof
# used cannot serve: its read is public by design.
PROOF_SPECS: dict[str, ProofSpec] = {
    "TryGhost/Ghost": ProofSpec(
        "GET", "/ghost/api/admin/tags/", 403, acquisition_kind="mint"
    ),
    # D113: Directus's current-user endpoint; no Authorization -> 401.
    "directus/directus": ProofSpec(
        "GET", "/users/me", 401, acquisition_kind="login"
    ),
    # D131: Vendure's admin API; the `me` query with the acquired
    # session token presented as Bearer. Vendure answers a request
    # with no token as HTTP 200 with a GraphQL error body —
    # ForbiddenError (code "FORBIDDEN") at the registered control
    # SHA, per the reviewer's source check — so the bare leg's
    # expectation is status 200 PLUS the body check, and each
    # authenticated leg must show a non-null data.me.id (a 200
    # with `me: null` is the same refusal wearing a success
    # status).
    "vendure-ecommerce/vendure": ProofSpec(
        "POST",
        "/admin-api",
        200,
        acquisition_kind="login",
        body={"query": "query { me { id identifier } }"},
        authenticated_body_path="data.me.id",
        unauthenticated_body_path="errors[0].extensions.code",
        unauthenticated_body_value="FORBIDDEN",
    ),
    # D158: paperless-ngx's user-self endpoint (ProfileView,
    # IsAuthenticated); a request with no Authorization header is
    # refused with a real HTTP 401, so no body checks.
    "paperless-ngx/paperless-ngx": ProofSpec(
        "GET", "/api/profile/", 401, acquisition_kind="login"
    ),
}


def spec_for_repo(repo: str) -> ProofSpec:
    """The registered proof spec for a repo; unregistered repos
    cannot be proven (fail closed, never a guessed endpoint)."""
    spec = PROOF_SPECS.get(repo)
    if spec is None:
        raise ValueError(f"no auth-proof spec registered for repo {repo!r}")
    return spec


_PRESENTATION_RE = re.compile(r"in the form '([A-Za-z][A-Za-z0-9_-]*) <token>'")


def presentation_scheme(contract: RepoContract) -> str:
    """The Authorization scheme the contract presents its token
    with ("Ghost", "Bearer", ...), read from the contract fixture's
    fixed auth_mechanism sentence — the contract is the source, so
    the driver carries no per-repo presentation constants."""
    mechanism = (contract.fixture or {}).get("auth_mechanism", "")
    match = _PRESENTATION_RE.search(mechanism)
    if match is None:
        raise ValueError(
            "contract fixture auth_mechanism does not name a presentation "
            f"scheme in the form '<Scheme> <token>': {mechanism!r}"
        )
    return match.group(1)


def seed_snapshot_files(contract: RepoContract) -> list[Path]:
    """The readiness/seeds/ files the contract's ``seed:`` command
    names by bare filename at the tree root (Ghost: ghost-seed.js).
    Only those join the harness commit next to the contract; a seed
    that names an in-tree script (a path with a separator) or no
    script adds nothing."""
    if not contract.seed:
        return []
    found: list[Path] = []
    for token in re.split(r"\s+", contract.seed):
        name = token.strip("\"'")
        if not name or "/" in name or "\\" in name:
            continue
        candidate = SEEDS_DIR / name
        if candidate.is_file() and candidate not in found:
            found.append(candidate)
    return found


def commit_contract_and_seeds(
    clone: Path,
    registered_sha: str,
    contract: Path,
    seed_files: list[Path],
) -> str:
    """Create the harness commit for one auth-proof side: parent =
    the registered SHA, tree delta = exactly ``+proofdeploy.yml``
    plus one entry per seed file (by bare filename). Plumbing only
    (no checkout), so the target tree is untouched. Mirrors
    rp.commit_contract, which adds the contract alone."""
    blobs = {
        "proofdeploy.yml": rp._git(clone, "hash-object", "-w", str(contract)),
    }
    for seed_file in seed_files:
        blobs[seed_file.name] = rp._git(
            clone, "hash-object", "-w", str(seed_file)
        )
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
    names = ", ".join(sorted(blobs))
    return rp._git(
        clone, "commit-tree", tree, "-p", registered_sha,
        "-m", f"auth-proof: add {names} (D137)",
        env_extra=rp._GIT_ENV,
    )


def commit_contract_and_seed(
    clone: Path, registered_sha: str, contract: Path, seed_script: Path
) -> str:
    """The single-seed-file form of commit_contract_and_seeds
    (Ghost's harness commit: proofdeploy.yml + ghost-seed.js)."""
    return commit_contract_and_seeds(
        clone, registered_sha, contract, [seed_script]
    )


class Redactor:
    """Scrub a side's secret VALUES out of emitted text, counting
    replacements per category. Values are registered as the proof
    learns them (bootstrap values, the seed-written key, acquired
    tokens); a JWT-shape sweep catches any token-shaped string the
    value set missed. Counts — never values — are what the row
    records."""

    def __init__(self) -> None:
        self._values: dict[str, list[str]] = {
            "key_file": [],
            "bootstrap_value": [],
            "acquired_token": [],
        }
        self.counts: dict[str, int] = {
            "key_file": 0,
            "bootstrap_value": 0,
            "acquired_token": 0,
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
        text, swept = _JWT_SHAPE.subn("<redacted:acquired_token>", text)
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


_JWT_SHAPE = re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")
_PROVIDER_DETAIL_PREFIX = "credential provider: "
_PROVIDER_CALL_RE = re.compile(
    r"^(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS) \S+$"
)


def _acquisition_kinds(details: list[str]) -> dict[str, int]:
    """Count the provider's acquisitions BY KIND from its own
    detail lines (D137): a mint ("minted ghost_jwt ..."), a login
    ("POST /auth/login (login)"), a registration ("... (register)"),
    or a refresh — the one provider call that carries no suffix,
    "POST /auth/refresh". Non-acquisition provider lines (failures,
    "token acquired") are not acquisitions and are not counted."""
    kinds: dict[str, int] = {}
    for detail in details:
        if not detail.startswith(_PROVIDER_DETAIL_PREFIX):
            continue
        rest = detail[len(_PROVIDER_DETAIL_PREFIX):]
        if rest.startswith("minted "):
            kind = "mint"
        elif rest.rstrip().endswith("(login)"):
            kind = "login"
        elif rest.rstrip().endswith("(register)"):
            kind = "register"
        elif _PROVIDER_CALL_RE.match(rest.strip()):
            kind = "refresh"
        else:
            continue
        kinds[kind] = kinds.get(kind, 0) + 1
    return kinds


def _authenticated_probe(spec: ProofSpec, scheme: str) -> dict[str, Any]:
    act: dict[str, Any] = {
        "method": spec.method,
        "path": spec.path,
        "headers": {"Authorization": f"{scheme} ${{AUTH_TOKEN}}"},
    }
    if spec.body is not None:
        act["body"] = spec.body
    return {"act": act, "assert": [{"type": "status", "equals": 200}]}


def _unauthenticated_probe(spec: ProofSpec) -> dict[str, Any]:
    act: dict[str, Any] = {"method": spec.method, "path": spec.path}
    if spec.body is not None:
        act["body"] = spec.body
    return {
        "act": act,
        "assert": [{"type": "status", "equals": spec.unauthenticated_status}],
    }


_PRUNED_SNAPSHOT_DIRS = {"node_modules", ".git", ".pnpm", ".venv", "__pycache__"}


def _snapshot_db_files(snapshot: Path) -> list[str]:
    """The SQLite files the seed/boot left in the snapshot, as
    "relative/path (N bytes)" — evidence the seed ran, read
    host-side. Dependency and VCS trees are pruned (never app
    data); the list is capped so a data-heavy snapshot stays
    bounded."""
    found: list[str] = []
    if not snapshot.is_dir():
        return found
    for dirpath, dirnames, filenames in os.walk(snapshot):
        dirnames[:] = sorted(
            d for d in dirnames if d not in _PRUNED_SNAPSHOT_DIRS
        )
        for name in sorted(filenames):
            if ".db" not in name:
                continue
            entry = Path(dirpath) / name
            if entry.is_file():
                found.append(
                    f"{entry.relative_to(snapshot).as_posix()} "
                    f"({entry.stat().st_size} bytes)"
                )
            if len(found) >= 20:
                return found
    return found


def _seed_evidence(side: Any, contract: RepoContract) -> dict[str, Any]:
    """What the seed left in the snapshot, read host-side: the
    presence (never the content) of the key file the contract's
    auth declares, when it declares one, and the SQLite files the
    boot/seed wrote. A contract with no key_file (login/refresh
    modes) reports key_file_present as None — no conjunct."""
    snapshot = Path(side.snapshot_dir)
    key_file: str | None = None
    if contract.auth:
        declared = contract.auth.get("key_file")
        if isinstance(declared, str) and declared:
            key_file = declared
    key_file_present: bool | None = None
    if key_file is not None:
        key_file_present = (snapshot / key_file).is_file()
    return {
        "key_file_present": key_file_present,
        "key_file": key_file,
        "sqlite_files": _snapshot_db_files(snapshot),
    }


_MISSING: Any = object()


def _json_path_value(body: Any, path: str) -> Any:
    """Resolve a generic JSON path ("data.me.id",
    "errors[0].extensions.code") over a parsed body; _MISSING
    when any step is absent or of the wrong shape. Dotted names
    walk mappings, [n] indexes sequences — nothing repo-specific."""
    current = body
    for segment in path.split("."):
        name, bracket, rest = segment.partition("[")
        if name:
            if not isinstance(current, dict) or name not in current:
                return _MISSING
            current = current[name]
        while bracket:
            index, close, rest = rest.partition("]")
            if not close or not index.isdigit():
                return _MISSING
            if not isinstance(current, list) or int(index) >= len(current):
                return _MISSING
            current = current[int(index)]
            bracket, rest = ("[", rest[1:]) if rest.startswith("[") else ("", rest)
    return current


def _record_body(result: Any, redactor: Redactor) -> Any:
    """The response body as the row records it: parsed JSON when
    the body parses (the body checks in _proof_passed walk it),
    else the scrubbed raw text, else None. Scrubbed by value like
    every other string the harness emits."""
    raw = getattr(result, "http_body", "") or ""
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except ValueError:
        return redactor.scrub(raw)
    return redactor.scrub_obj(parsed)


def _request_record(
    result: Any,
    *,
    authenticated: bool,
    redactor: Redactor,
    spec: ProofSpec,
    scheme: str,
) -> dict[str, Any]:
    """One proof request as the row records it: method, path, the
    Authorization header's SHAPE (never its value), the HTTP
    status the executor observed, the response body (scrubbed),
    the verdict, and the provider's acquisitions for this
    request, counted by kind."""
    kinds = _acquisition_kinds(result.details)
    return {
        "method": spec.method,
        "path": spec.path,
        "authorization": (
            f"{scheme} <redacted:acquired_token>" if authenticated else None
        ),
        "http_status": result.http_status,
        "body": _record_body(result, redactor),
        "verdict": result.verdict.value,
        "reason": result.reason.value if result.reason else None,
        "acquisitions": sum(kinds.values()),
        "acquisition_kinds": kinds,
        "details": [redactor.scrub(d) for d in result.details],
    }


def _register_result_secrets(redactor: Redactor, result: Any) -> None:
    redactor.add_many("acquired_token", result.minted_tokens)
    redactor.add_many("bootstrap_value", result.bootstrap_values)
    for value in (result.captured_bindings or {}).values():
        if isinstance(value, list):
            redactor.add_many("bootstrap_value", value)
        else:
            redactor.add("bootstrap_value", value)


def _bare_executor(executor: Any) -> Any:
    """A copy of the side's executor with NO credential provider
    and NO static token, for the unauthenticated leg: through the
    provider-configured executor the provider's proactive ensure
    would still acquire before the request — a credential event
    that would make the "bare" leg neither bare nor
    acquisition-free. The copy shares the target, the snapshot
    and the provisioned flag; only the credential wiring is
    removed."""
    bare = copy.copy(executor)
    bare.credential_config = None
    bare.auth_token = None
    return bare


def _proof_passed(
    seed_ok: bool,
    records: list[dict[str, Any]],
    spec: ProofSpec,
) -> bool:
    """The proof verdict, composed from the seed evidence and the
    three request records, PER LEG (never summed): the seed
    conjunct holds (a declared key file is present; no conjunct
    when the contract declares none); EACH authenticated leg was
    accepted (200) with exactly one fresh acquisition, of the
    spec's registered kind, and — when the spec registers an
    authenticated body path — that path resolves non-null in the
    leg's recorded body; the unauthenticated leg was refused with
    the expected status, performed ZERO acquisitions, sent no
    Authorization header, and — when the spec registers a bare-leg
    body check — its body's path equals the registered value;
    and every leg's own verdict is a pass. Pure over its inputs
    so every conjunct is testable (D106)."""

    def authenticated_ok(record: dict[str, Any]) -> bool:
        if record["http_status"] != 200 or record["verdict"] != "pass":
            return False
        if record["acquisitions"] != 1:
            return False
        if record["acquisition_kinds"] != {spec.acquisition_kind: 1}:
            return False
        if spec.authenticated_body_path is not None:
            value = _json_path_value(
                record.get("body"), spec.authenticated_body_path
            )
            if value is _MISSING or value is None:
                return False
        return True

    bare = records[2]
    bare_ok = (
        bare["http_status"] == spec.unauthenticated_status
        and bare["verdict"] == "pass"
        and bare["acquisitions"] == 0
        and not bare["acquisition_kinds"]
        and bare["authorization"] is None
    )
    if bare_ok and spec.unauthenticated_body_path is not None:
        value = _json_path_value(
            bare.get("body"), spec.unauthenticated_body_path
        )
        bare_ok = (
            value is not _MISSING
            and value == spec.unauthenticated_body_value
        )
    return (
        seed_ok
        and authenticated_ok(records[0])
        and authenticated_ok(records[1])
        and bare_ok
    )


def auth_proof_side(
    orch: Any,
    clone: Path,
    unit: dict[str, Any],
    side_spec: dict[str, Any],
    contract: Path,
    attempt: int,
    workdir: Path,
    network_mode: str | None,
    spec: ProofSpec | None = None,
) -> dict[str, Any]:
    """Provision ONE side and, while it is live, run the three
    auth probes. Returns the side's auth-proof row. Never raises
    for a provisioning outcome (typed into the row, mirroring
    rp.provision_side); the raising paths are the harness's own
    (fetch / harness commit / unregistered repo / unrenderable
    contract), which the caller types."""
    if spec is None:
        spec = spec_for_repo(str(unit.get("repo")))
    loaded = load_contract(contract)
    scheme = presentation_scheme(loaded)
    rp.fetch_sha(clone, str(side_spec["sha"]))
    harness_sha = commit_contract_and_seeds(
        clone, str(side_spec["sha"]), contract, seed_snapshot_files(loaded)
    )
    unit_dir = workdir / "units" / str(unit["unit_id"]) / f"a{attempt}"
    unit_dir.mkdir(parents=True, exist_ok=True)
    tag = rp._TAG_BY_ROLE[str(side_spec["role"])]
    redactor = Redactor()
    side: Any = None

    def not_run(reason: str) -> dict[str, Any]:
        return {
            "endpoint": spec.path,
            "method": spec.method,
            "status": "NOT_RUN",
            "reason": reason,
            "requests": [],
            "acquisitions_after_two_authenticated": 0,
            "acquisition_kinds": {},
            "passed": False,
        }

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
            row["seed"] = {
                "key_file_present": False,
                "key_file": loaded.auth.get("key_file") if loaded.auth else None,
                "sqlite_files": [],
            }
            row["auth"] = not_run("side did not reach READY")
        else:
            seed_evidence = _seed_evidence(side, loaded)
            if seed_evidence["key_file"] is not None:
                key_path = Path(side.snapshot_dir) / seed_evidence["key_file"]
                if key_path.is_file():
                    redactor.add(
                        "key_file",
                        key_path.read_text(encoding="utf-8").strip(),
                    )
            executor = orch._make_executor(side)
            # The executor's credential config read the key host-side
            # after READY; its value joined the side's secret set.
            redactor.add_many(
                "bootstrap_value", getattr(side, "generated_secrets", None)
            )
            results = [
                executor.run_probe(_authenticated_probe(spec, scheme), index=0),
                executor.run_probe(_authenticated_probe(spec, scheme), index=1),
                _bare_executor(executor).run_probe(
                    _unauthenticated_probe(spec), index=2
                ),
            ]
            for result in results:
                _register_result_secrets(redactor, result)
            records = [
                _request_record(
                    results[0], authenticated=True,
                    redactor=redactor, spec=spec, scheme=scheme,
                ),
                _request_record(
                    results[1], authenticated=True,
                    redactor=redactor, spec=spec, scheme=scheme,
                ),
                _request_record(
                    results[2], authenticated=False,
                    redactor=redactor, spec=spec, scheme=scheme,
                ),
            ]
            acquisition_total = (
                records[0]["acquisitions"] + records[1]["acquisitions"]
            )
            kinds_total: dict[str, int] = {}
            for record in records[:2]:
                for kind, count in record["acquisition_kinds"].items():
                    kinds_total[kind] = kinds_total.get(kind, 0) + count
            seed_ok = seed_evidence["key_file_present"] is not False
            passed = _proof_passed(seed_ok, records, spec)
            row["seed"] = seed_evidence
            row["auth"] = {
                "endpoint": spec.path,
                "method": spec.method,
                "status": "PROVEN" if passed else "FAILED",
                "reason": None if passed else "see requests",
                "requests": records,
                "acquisitions_after_two_authenticated": acquisition_total,
                "acquisition_kinds": kinds_total,
                "passed": passed,
            }
            logs.append(
                "auth proof: "
                + "; ".join(
                    f"request {i + 1} -> {r['http_status']} "
                    f"(verdict {r['verdict']}, acquisitions "
                    f"{r['acquisitions']} {r['acquisition_kinds']})"
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
        row["seed"] = {
            "key_file_present": False,
            "key_file": loaded.auth.get("key_file") if loaded.auth else None,
            "sqlite_files": [],
        }
        row["auth"] = not_run(f"provision raised: {exc}")
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
    spec = spec_for_repo(repo)
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
                    attempt, workdir, network_mode, spec,
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
