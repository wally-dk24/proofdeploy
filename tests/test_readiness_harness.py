"""Readiness harness tests (Addendum 64/65): podman-free.

The drift guarantee (Addendum 65 item 3) has two halves:

* BEHAVIORAL (the harness half): test_provision_side_routes_through
  monkeypatches Orchestrator._provision_side_sandbox and proves the
  harness's per-side function routes through it, stops the live
  container, and maps the row.
* SOURCE-LEVEL (the measured half):
  test_measured_path_uses_the_same_method asserts measure_bug /
  measure_clean name the same one-side method. Driving measure_*
  behaviorally here would require a model call and the records
  machinery — the invariant that must not drift is precisely
  "both paths provision through _provision_side_sandbox", and that
  is a source fact about orchestrator.py. If the measured path is
  ever refactored onto a different one-side method, this test
  fails and the harness must follow it.
"""

from __future__ import annotations

import inspect
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

import readiness.auth_proof as ap
import readiness.provision as rp
from proofdeploy import orchestrator as orch_mod
from proofdeploy.executor import ProbeResult, Verdict
from proofdeploy.orchestrator import MeasureConfig, Orchestrator
from proofdeploy.provisioning import NodeAdapter
from proofdeploy.runner import ProvisionResult, ProvisionStatus
from proofdeploy.yml import load_contract

BUG_UNIT: dict[str, Any] = {
    "unit_id": "T2",
    "repo": "fastify/fastify",
    "class": "train-bug",
    "sides": [
        {"role": "fix^", "sha": "a" * 40},
        {"role": "fix", "sha": "b" * 40},
    ],
}
CLEAN_UNIT: dict[str, Any] = {
    "unit_id": "train-C1",
    "repo": "django/django",
    "class": "train-clean",
    "sides": [{"role": "C", "sha": "c" * 40}],
}


class _FakeProc:
    def __init__(self) -> None:
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


def _fake_side(
    *,
    status: ProvisionStatus = ProvisionStatus.READY,
    reason: str | None = None,
    plan: dict[str, Any] | None = None,
    logs: list[str] | None = None,
    proc: Any = None,
) -> SimpleNamespace:
    prov = ProvisionResult(
        status=status,
        reason=reason,
        runtime="node",
        runtime_version="22.11.0",
        package_manager="npm",
        install_plan=plan,
        install_plan_sha256="ab" * 32,
        logs=logs if logs is not None else ["install plan: ...", "ready"],
        proc=proc,
    )
    return SimpleNamespace(
        sha="h" * 40,
        provision=prov,
        sandbox_evidence={"app_network_mode": "none+socket"},
        contract=None,
    )


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=True, timeout=60,
    )
    return proc.stdout.strip()


# --- manifest / cutoff helpers ---------------------------------------


def test_cutoff_sha_is_fix_for_bugs_and_c_for_cleans() -> None:
    assert rp.cutoff_sha(BUG_UNIT) == "b" * 40
    assert rp.cutoff_sha(CLEAN_UNIT) == "c" * 40


def test_manifest_shape() -> None:
    manifest = rp.load_manifest(rp.DEFAULT_MANIFEST)
    units = manifest["units"]
    assert len(units) == 28
    assert sum(len(u["sides"]) for u in units) == 43
    for unit in units:
        for side in unit["sides"]:
            assert len(side["sha"]) == 40
            int(side["sha"], 16)


def test_provision_repo_refuses_unknown_repo(tmp_path: Path) -> None:
    manifest = rp.load_manifest(rp.DEFAULT_MANIFEST)
    with pytest.raises(ValueError, match="no units for repo"):
        rp.provision_repo(manifest, "nobody/nothing", tmp_path, 1)


def test_rerun_targets_take_only_non_ready(tmp_path: Path) -> None:
    prior = tmp_path / "rows.json"
    prior.write_text(
        json.dumps(
            {
                "kind": "readiness",
                "rows": [
                    {"unit": "T2", "role": "fix", "status": "READY"},
                    {"unit": "T2", "role": "fix^", "status": "BUILD_FAILED"},
                    {"unit": "C1", "role": "C", "status": "START_FAILED"},
                ],
            }
        ),
        encoding="utf-8",
    )
    assert rp._rerun_targets(prior) == [("T2", "fix^"), ("C1", "C")]


# --- row mapping ------------------------------------------------------


def test_row_from_provision_ready() -> None:
    plan = {
        "declared_range": ">=22",
        "selected_version": "22.11.0",
        "package_manager": "pnpm",
        "refusal": None,
    }
    side = _fake_side(plan=plan)
    row = rp.row_from_provision(
        BUG_UNIT, BUG_UNIT["sides"][1], "h" * 40, 1, "isolated", side
    )
    assert row["kind"] == "readiness"
    assert row["status"] == "READY"
    assert row["registered_sha"] == "b" * 40
    assert row["harness_sha"] == "h" * 40
    assert row["declared_runtime"] == ">=22"
    assert row["selected_runtime"] == "node"
    assert row["runtime_version"] == "22.11.0"
    # The plan's manager wins over the provision's (design O5).
    assert row["package_manager"] == "pnpm"
    assert row["typed_reason"] is None
    assert row["last_error"] is None
    assert row["install_plan_sha256"] == "ab" * 32
    # The side's reported mode wins over the run fallback.
    assert row["network_mode"] == "none+socket"
    assert row["attempt"] == 1


def test_row_from_provision_carries_plan_workspace_facts() -> None:
    """Plan 1.2(k) trace fix: the row's app_path / workspace_root
    come from the plan, like every other plan fact on the row. A
    workspace plan that resolved the root app (Vendure's shape)
    shows "." on its row; before this fix the row carried no
    app_path field at all, so any reader saw a phantom None that
    no code had written and that contradicted the plan."""
    plan = {
        "declared_range": ">=20",
        "selected_version": "22.11.0",
        "package_manager": "bun",
        "app_path": ".",
        "workspace_root": ".",
        "refusal": None,
    }
    side = _fake_side(plan=plan)
    row = rp.row_from_provision(
        BUG_UNIT, BUG_UNIT["sides"][1], "h" * 40, 1, "isolated", side
    )
    assert row["app_path"] == "."
    assert row["workspace_root"] == "."


def test_row_from_provision_carries_declared_app_port() -> None:
    """Plan 1.2(o): the row records the contract's declared
    app.port, read from the side's own loaded contract (the
    snapshot's proofdeploy.yml). A contract declaring none — and
    a side with no contract at all — records None."""
    from proofdeploy.yml import RepoContract

    side = _fake_side(plan={})
    side.contract = RepoContract(
        build="b", start="s", readiness="/", app_port=3000
    )
    row = rp.row_from_provision(
        BUG_UNIT, BUG_UNIT["sides"][1], "h" * 40, 1, "isolated", side
    )
    assert row["app_port"] == 3000

    side_default = _fake_side(plan={})
    row_default = rp.row_from_provision(
        BUG_UNIT, BUG_UNIT["sides"][1], "h" * 40, 1, "isolated",
        side_default,
    )
    assert row_default["app_port"] is None


def test_exception_and_harness_rows_carry_app_port() -> None:
    """Plan 1.2(o): EVERY readiness row carries app_port — the
    exception and harness-error rows take the repo contract's
    declared value (threaded through by the harness), defaulting
    to None."""
    exc = ValueError("cannot detect runtime in /snap")
    row = rp.row_from_exception(
        BUG_UNIT, BUG_UNIT["sides"][0], "h" * 40, 1, None, exc,
        app_port=3000,
    )
    assert row["app_port"] == 3000
    row_none = rp.row_from_exception(
        BUG_UNIT, BUG_UNIT["sides"][0], "h" * 40, 1, None, exc
    )
    assert row_none["app_port"] is None

    hrow = rp._harness_error_row(
        BUG_UNIT, BUG_UNIT["sides"][0], 1, None,
        RuntimeError("fetch failed"), app_port=3000,
    )
    assert hrow["app_port"] == 3000


def test_contract_app_port_reads_the_repo_contract() -> None:
    """The harness's app_port source is the repo's committed
    contract in readiness/contracts/: nest declares 3000 (plan
    1.2(o), D98); bottle declares none."""
    assert rp.contract_app_port("nestjs/nest") == 3000
    assert rp.contract_app_port("bottlepy/bottle") is None


def test_row_from_provision_build_failed_plan_refusal() -> None:
    plan = {"declared_range": None, "refusal": "workspaces not yet supported"}
    side = _fake_side(
        status=ProvisionStatus.BUILD_FAILED,
        reason="workspaces not yet supported",
        plan=plan,
        logs=["install plan: ...", "workspaces not yet supported"],
    )
    side.sandbox_evidence = {}
    row = rp.row_from_provision(
        BUG_UNIT, BUG_UNIT["sides"][0], "h" * 40, 2, "isolated", side
    )
    assert row["status"] == "BUILD_FAILED"
    # The plan's refusal is the typed reason, verbatim.
    assert row["typed_reason"] == "workspaces not yet supported"
    # No manager on the plan: the row records None — the field
    # comes from the plan only, never the provision's default.
    assert row["package_manager"] is None
    assert row["last_error"] == "workspaces not yet supported"
    assert row["network_mode"] == "isolated"
    assert row["attempt"] == 2


def test_row_from_provision_records_the_refusal_that_fired(
    tmp_path: Path,
) -> None:
    # Directus shape (ten-repo re-run on c867b4b, WAL-119): a
    # workspaces repo whose declared Node range no pinned image
    # satisfies (the re-run's Directus declared the bare "22",
    # which the pre-WO-8.4a parser misread as exact 22.0.0; the
    # shape now declares "23", for which no image is pinned).
    # Since plan 1.2b the workspace side of the shape is an
    # AMBIGUOUS workspace — two member packages and no contract
    # app.path — whose plan refusal is "ambiguous app: 2
    # candidates" (before 1.2b it was "workspaces not yet
    # supported"; the both-refusals premise this test exists for
    # is unchanged). The real Node plan for such
    # a snapshot carries BOTH refusals — `refusal` (the ambiguity
    # refusal) and the sandbox-only `image_refusal` (the
    # range refusal) — and the sandbox executor checks
    # `image_refusal` before `plan.refusal` is ever consulted, so
    # the side stops on the range refusal: that is its provision
    # reason and the last line of its log. The row's typed reason
    # must be the refusal that fired, not the plan field that
    # never did (the re-run's Directus rows said "workspaces"
    # while every log — and the rows' own last_error — ended with
    # the range refusal).
    snap = tmp_path / "snapshot"
    snap.mkdir()
    (snap / "package.json").write_text(
        json.dumps(
            {
                "name": "directus-shaped",
                "engines": {"node": "23"},
                "workspaces": ["packages/*"],
            }
        ),
        encoding="utf-8",
    )
    (snap / "pnpm-workspace.yaml").write_text(
        "packages:\n  - 'packages/*'\n", encoding="utf-8"
    )
    for member in ("packages/a", "packages/b"):
        member_dir = snap / member
        member_dir.mkdir(parents=True)
        (member_dir / "package.json").write_text(
            json.dumps({"name": member.replace("/", "-")}),
            encoding="utf-8",
        )
    plan = NodeAdapter().plan(snap, contract=None)
    # The premise, from the real planner: both refusals on one plan.
    assert plan.refusal == "ambiguous app: 2 candidates"
    assert plan.image_refusal == (
        "declared range 23 not satisfiable by pinned images"
    )
    assert plan.lockfile_name is None
    side = _fake_side(
        status=ProvisionStatus.BUILD_FAILED,
        reason=plan.image_refusal,
        plan=plan.to_dict(),
        logs=[
            "install plan: sha256 ... (adapter: node)",
            str(plan.image_refusal),
        ],
    )
    side.provision.runtime_version = None
    side.sandbox_evidence = {}
    row = rp.row_from_provision(
        BUG_UNIT, BUG_UNIT["sides"][0], "h" * 40, 1, "none+socket", side
    )
    assert row["status"] == "BUILD_FAILED"
    assert row["typed_reason"] == plan.image_refusal
    assert row["last_error"] == plan.image_refusal


def test_provision_side_row_agrees_with_its_log_on_both_refusals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The same Directus shape driven through the harness's per-side
    # function: the row's typed reason must equal the last line of
    # the side's own dumped pd-log (the refusal that fired), even
    # though the attached plan also carries a `refusal` that never
    # fired in the sandbox.
    range_refusal = "declared range 23 not satisfiable by pinned images"
    plan = {
        "declared_range": "23",
        "refusal": "workspaces not yet supported",
        "image_refusal": range_refusal,
    }

    def fake_method(
        self: Orchestrator, rev: str, port: int, unit_dir: Path, tag: str
    ) -> SimpleNamespace:
        return _fake_side(
            status=ProvisionStatus.BUILD_FAILED,
            reason=range_refusal,
            plan=plan,
            logs=[
                "install plan: sha256 ... (adapter: node)",
                range_refusal,
            ],
            proc=_FakeProc(),
        )

    monkeypatch.setattr(Orchestrator, "_provision_side_sandbox", fake_method)
    monkeypatch.setattr(rp, "fetch_sha", lambda clone, sha: None)
    monkeypatch.setattr(rp, "commit_contract", lambda clone, sha, c: "h" * 40)
    monkeypatch.chdir(tmp_path)

    cfg = MeasureConfig(
        repo_dir=tmp_path,
        output_dir=tmp_path / "records",
        workdir=tmp_path / "work",
        no_skill=True,
        sandbox=True,
    )
    orch = Orchestrator(cfg)
    row = rp.provision_side(
        orch, tmp_path, BUG_UNIT, BUG_UNIT["sides"][0],
        Path("contract.yml"), 1, tmp_path / "work", "none+socket",
    )
    assert row["status"] == "BUILD_FAILED"
    assert row["typed_reason"] == range_refusal
    log_text = (
        tmp_path / "pd-logs" / "readiness-T2-fix-parent-a1.log"
    ).read_text(encoding="utf-8")
    assert log_text.splitlines()[-1] == row["typed_reason"]


def test_row_from_exception_types_runtime_detection() -> None:
    exc = ValueError(
        "cannot detect runtime in /snap: no package.json, "
        "pyproject.toml or requirements.txt"
    )
    row = rp.row_from_exception(
        BUG_UNIT, BUG_UNIT["sides"][0], "h" * 40, 1, "isolated", exc
    )
    assert row["status"] == "BUILD_FAILED"
    assert row["typed_reason"] == "runtime not supported"
    assert row["last_error"] == str(exc)
    assert row["install_plan_sha256"] is None


def test_row_from_exception_keeps_other_messages() -> None:
    exc = ValueError("proofdeploy.yml missing required keys: build")
    row = rp.row_from_exception(
        BUG_UNIT, BUG_UNIT["sides"][0], "h" * 40, 1, None, exc
    )
    assert row["typed_reason"] == "proofdeploy.yml missing required keys: build"


# --- the drift guarantee ----------------------------------------------


def test_provision_side_routes_through_orchestrator_method(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, str]] = []
    proc = _FakeProc()

    def fake_method(
        self: Orchestrator, rev: str, port: int, unit_dir: Path, tag: str
    ) -> SimpleNamespace:
        calls.append((rev, tag))
        return _fake_side(proc=proc)

    monkeypatch.setattr(Orchestrator, "_provision_side_sandbox", fake_method)
    monkeypatch.setattr(rp, "fetch_sha", lambda clone, sha: None)
    monkeypatch.setattr(rp, "commit_contract", lambda clone, sha, c: "h" * 40)
    monkeypatch.chdir(tmp_path)

    cfg = MeasureConfig(
        repo_dir=tmp_path,
        output_dir=tmp_path / "records",
        workdir=tmp_path / "work",
        no_skill=True,
        sandbox=True,
    )
    orch = Orchestrator(cfg)
    row = rp.provision_side(
        orch, tmp_path, BUG_UNIT, BUG_UNIT["sides"][0],
        Path("contract.yml"), 1, tmp_path / "work", "none+socket",
    )
    # Routed through the measured path's own one-side method, with
    # the harness commit as the rev.
    assert calls == [("h" * 40, "fix-parent")]
    assert row["status"] == "READY"
    assert row["registered_sha"] == "a" * 40
    assert row["harness_sha"] == "h" * 40
    # A READY side's live container is stopped, never leaked.
    assert proc.stopped
    # The side's logs are dumped for the artifact.
    assert (tmp_path / "pd-logs" / "readiness-T2-fix-parent-a1.log").is_file()


def test_provision_side_types_a_raising_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def raising_method(
        self: Orchestrator, rev: str, port: int, unit_dir: Path, tag: str
    ) -> SimpleNamespace:
        raise ValueError(
            "cannot detect runtime in /snap: no package.json, "
            "pyproject.toml or requirements.txt"
        )

    monkeypatch.setattr(Orchestrator, "_provision_side_sandbox", raising_method)
    monkeypatch.setattr(rp, "fetch_sha", lambda clone, sha: None)
    monkeypatch.setattr(rp, "commit_contract", lambda clone, sha, c: "h" * 40)
    monkeypatch.chdir(tmp_path)

    cfg = MeasureConfig(
        repo_dir=tmp_path,
        output_dir=tmp_path / "records",
        workdir=tmp_path / "work",
        no_skill=True,
        sandbox=True,
    )
    orch = Orchestrator(cfg)
    row = rp.provision_side(
        orch, tmp_path, BUG_UNIT, BUG_UNIT["sides"][0],
        Path("contract.yml"), 1, tmp_path / "work", None,
    )
    assert row["status"] == "BUILD_FAILED"
    assert row["typed_reason"] == "runtime not supported"


def test_measured_path_uses_the_same_method() -> None:
    # Source-level by design (see module docstring): the measured
    # entry points must name the same one-side provisioning method
    # the harness drives, or the two paths can drift apart.
    for entry in ("measure_bug", "measure_clean"):
        src = inspect.getsource(getattr(orch_mod.Orchestrator, entry))
        assert "_provision_side_sandbox(" in src, entry


def test_harness_writes_no_measured_records() -> None:
    # Structural guarantee, checked at the source: the harness
    # never touches the record machinery or any scorer input.
    src = Path(rp.__file__).read_text(encoding="utf-8")
    assert "build_evidence_record" not in src
    assert "measured_result" not in src
    assert "runpair" not in src


# --- the harness commit ------------------------------------------------


def test_commit_contract_delta_is_only_the_contract(tmp_path: Path) -> None:
    repo = tmp_path / "target"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "readiness-test")
    _git(repo, "config", "user.email", "test@proofdeploy.invalid")
    (repo / "app.py").write_text("print('hi')\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "registered")
    registered = _git(repo, "rev-parse", "HEAD")

    contract = tmp_path / "proofdeploy.yml"
    contract.write_text(
        'build: "true"\nstart: "python app.py"\nreadiness: "/"\n',
        encoding="utf-8",
    )
    harness_sha = rp.commit_contract(repo, registered, contract)
    assert _git(repo, "rev-parse", f"{harness_sha}^") == registered
    changed = _git(repo, "diff", "--name-only", registered, harness_sha)
    assert changed.splitlines() == ["proofdeploy.yml"]
    assert _git(repo, "show", f"{harness_sha}:proofdeploy.yml") == contract.read_text(
        encoding="utf-8"
    ).strip()


# --- committed contracts ------------------------------------------------


def test_manifest_and_contracts_cover_the_same_repos() -> None:
    manifest = rp.load_manifest(rp.DEFAULT_MANIFEST)
    repos = {u["repo"] for u in manifest["units"]}
    files = {p.stem for p in rp.CONTRACTS_DIR.glob("*.yml")}
    assert {rp.repo_slug(r) for r in repos} == files


def test_every_contract_validates() -> None:
    for path in sorted(rp.CONTRACTS_DIR.glob("*.yml")):
        contract = load_contract(path)
        assert contract.build and contract.start and contract.readiness


def test_contracts_carry_no_unapproved_credentials() -> None:
    # Addendum 65 (5): no credential beyond the approved fixtures'
    # own env. The approved provider declarations are Directus's,
    # from its approved fixture, and Ghost's (ghost_jwt, approved
    # D87: without it the checkpoint's Ghost unit could not have
    # authenticated in a measured run either). Both name env NAMES
    # only — values are generated per run. Vendure's is the third
    # (login with the D120 token_header, approved D120/D131): its
    # login body names upstream's published core-default superadmin
    # credentials — the same on every instance, not per-run secrets.
    # Paperless-ngx's is the fourth (login, approved D158): its login
    # body names only the per-run generated bootstrap env NAMES
    # (PAPERLESS_ADMIN_USER / PAPERLESS_ADMIN_PASSWORD) — values are
    # generated per run, never committed.
    for path in sorted(rp.CONTRACTS_DIR.glob("*.yml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if path.name == "directus__directus.yml":
            assert data.get("auth", {}).get("mode") == "refresh"
        elif path.name == "TryGhost__Ghost.yml":
            assert data.get("auth", {}).get("mode") == "ghost_jwt"
        elif path.name == "vendure-ecommerce__vendure.yml":
            assert data.get("auth", {}).get("mode") == "login"
        elif path.name == "paperless-ngx__paperless-ngx.yml":
            assert data.get("auth", {}).get("mode") == "login"
        else:
            assert "auth" not in data, path.name


def test_vendure_login_query_loads_with_plain_quotes() -> None:
    # PR #133 (reviewer): the committed Vendure login query sits in a
    # YAML single-quoted string, which does not process backslash
    # escapes — a \" in the source would load as a literal backslash,
    # the GraphQL parser rejects it ("Unexpected character: '\'"),
    # and every Vendure login would fail. Pin the LOADED string
    # character-for-character so the quoting cannot regress.
    contract = load_contract(rp.CONTRACTS_DIR / "vendure-ecommerce__vendure.yml")
    query = contract.auth["body"]["query"]
    assert "\\" not in query
    assert query == (
        'mutation { login(username: "superadmin", password: "superadmin") '
        "{ ... on CurrentUser { id identifier } } }"
    )


# --- the real-git path -------------------------------------------------
#
# The run-38575200339 failure (cutoff resolved before any fetch, in a
# clone that starts empty) was invisible to every test above because
# they stub the orchestrator or the fetch layer. This test stubs ONLY
# the sandbox call (Orchestrator._provision_side_sandbox) and the
# measurement gate, and runs the real provision_repo path — real
# fetches from a local file:// source, real cutoff resolution, real
# contract commits — from an empty clone.


def _commit_dated(repo: Path, text: str, date: str) -> str:
    (repo / "app.py").write_text(f'print("{text}")\n', encoding="utf-8")
    _git(repo, "add", "-A")
    env = dict(os.environ)
    env["GIT_AUTHOR_DATE"] = date
    env["GIT_COMMITTER_DATE"] = date
    proc = subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "-m", text],
        capture_output=True, text=True, check=True, timeout=60, env=env,
    )
    assert proc.returncode == 0
    return _git(repo, "rev-parse", "HEAD")


def _rfc3339_of(repo: Path, sha: str) -> str:
    epoch = int(_git(repo, "show", "-s", "--format=%ct", sha))
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def test_provision_repo_real_git_from_empty_clone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Source repository standing in for the GitHub remote: three
    # commits with distinct committer times, fetchable by SHA the
    # way GitHub allows.
    src = tmp_path / "src"
    src.mkdir()
    _git(src, "init", "-q")
    _git(src, "config", "user.name", "readiness-test")
    _git(src, "config", "user.email", "test@proofdeploy.invalid")
    _git(src, "config", "uploadpack.allowAnySHA1InWant", "true")
    fix_parent = _commit_dated(src, "v1", "2026-01-01T00:00:00+00:00")
    fix = _commit_dated(src, "v2", "2026-01-02T00:00:00+00:00")
    clean = _commit_dated(src, "v3", "2026-01-03T00:00:00+00:00")
    # Distinct times, so a cutoff equality below proves WHERE the
    # cutoff came from, not a coincidence of equal timestamps.
    assert len({_rfc3339_of(src, s) for s in (fix_parent, fix, clean)}) == 3

    repo = "example/widgets"
    workdir = tmp_path / "work"
    # The harness clone, exactly as ensure_clone leaves it before
    # the run — except its origin is the local source. EMPTY: the
    # .git directory exists, and it holds no objects at all.
    clone = workdir / "repos" / rp.repo_slug(repo)
    clone.mkdir(parents=True)
    _git(clone, "init", "-q")
    _git(clone, "remote", "add", "origin", src.as_uri())
    assert "count: 0" in _git(clone, "count-objects", "-v")

    contract = tmp_path / "widgets.yml"
    contract.write_text(
        'build: "true"\nstart: "python app.py"\nreadiness: "/"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(rp, "contract_path", lambda r: contract)
    # The measurement gate is the sandbox capability gate; there is
    # no capable sandbox in a host test by design (this suite is
    # podman-free). Everything else in the path is real.
    monkeypatch.setattr(rp, "assert_measurement_gate", lambda: None)

    seen: list[dict[str, Any]] = []

    def fake_method(
        self: Orchestrator, rev: str, port: int, unit_dir: Path, tag: str
    ) -> SimpleNamespace:
        seen.append(
            {"rev": rev, "tag": tag, "cutoff": self._run_lock_cutoff}
        )
        return _fake_side()

    monkeypatch.setattr(Orchestrator, "_provision_side_sandbox", fake_method)
    monkeypatch.chdir(tmp_path)  # pd-logs/ land in tmp

    manifest: dict[str, Any] = {
        "units": [
            {
                "unit_id": "T-REAL",
                "repo": repo,
                "class": "train-bug",
                "sides": [
                    {"role": "fix^", "sha": fix_parent},
                    {"role": "fix", "sha": fix},
                ],
            },
            {
                "unit_id": "C-REAL",
                "repo": repo,
                "class": "train-clean",
                "sides": [{"role": "C", "sha": clean}],
            },
        ]
    }
    result = rp.provision_repo(manifest, repo, workdir, 1)

    rows = result["rows"]
    assert [r["status"] for r in rows] == ["READY"] * 3
    assert [r["registered_sha"] for r in rows] == [fix_parent, fix, clean]
    assert [r["harness_sha"] for r in rows] == [s["rev"] for s in seen]
    assert result["network_mode"] in ("isolated", "none+socket")

    # Cutoff arithmetic, from the real git calls: during BOTH bug
    # sides the run cutoff is the FIX commit's committer time (never
    # the side's own — the fix^ side proves it); during the clean
    # side it is the C commit's.
    assert [s["cutoff"] for s in seen] == [
        _rfc3339_of(src, fix),
        _rfc3339_of(src, fix),
        _rfc3339_of(src, clean),
    ]

    # The sandbox call received harness commits, each built by the
    # real commit_contract (read-tree over the depth-1 fetch): the
    # parent is the registered SHA, the tree delta is exactly
    # +proofdeploy.yml, and the tree carries that side's own files.
    for call, registered, text in zip(
        seen, (fix_parent, fix, clean), ("v1", "v2", "v3"), strict=True
    ):
        harness = call["rev"]
        assert harness != registered
        assert _git(clone, "rev-parse", f"{harness}^") == registered
        changed = _git(clone, "diff", "--name-only", registered, harness)
        assert changed.splitlines() == ["proofdeploy.yml"]
        assert _git(clone, "show", f"{harness}:app.py") == f'print("{text}")'

    # The fetches were depth-1: the clone is shallow.
    assert (clone / ".git" / "shallow").is_file()
    # Fetching the same SHA twice is harmless (the clean unit above
    # already fetched its C commit twice — once as the cutoff, once
    # as the side); once more, directly, both times:
    rp.fetch_sha(clone, clean)
    rp.fetch_sha(clone, clean)


def test_provision_repo_attempt2_uses_attempt_scoped_dirs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Regression test for the Directus attempt-2 collision (run
    # 38587020785): the workflow's attempt-2 invocation shares the
    # workdir with attempt 1, so unit directories and pd-logs names
    # must be attempt-scoped, or the rerun dies on attempt 1's
    # snapshot directories and overwrites its logs. Runs the real
    # provision_repo twice against the same workdir — attempt 1,
    # then attempt 2 fed from attempt 1's rows exactly as the
    # workflow feeds it — with a non-READY side, so the rerun has
    # targets. Only the sandbox call and the gate are stubbed.
    src = tmp_path / "src"
    src.mkdir()
    _git(src, "init", "-q")
    _git(src, "config", "user.name", "readiness-test")
    _git(src, "config", "user.email", "test@proofdeploy.invalid")
    _git(src, "config", "uploadpack.allowAnySHA1InWant", "true")
    fix_parent = _commit_dated(src, "w1", "2026-02-01T00:00:00+00:00")
    fix = _commit_dated(src, "w2", "2026-02-02T00:00:00+00:00")

    repo = "example/widgets"
    workdir = tmp_path / "work"
    clone = workdir / "repos" / rp.repo_slug(repo)
    clone.mkdir(parents=True)
    _git(clone, "init", "-q")
    _git(clone, "remote", "add", "origin", src.as_uri())

    contract = tmp_path / "widgets.yml"
    contract.write_text(
        'build: "true"\nstart: "python app.py"\nreadiness: "/"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(rp, "contract_path", lambda r: contract)
    monkeypatch.setattr(rp, "assert_measurement_gate", lambda: None)

    calls: list[dict[str, Any]] = []

    def fake_method(
        self: Orchestrator, rev: str, port: int, unit_dir: Path, tag: str
    ) -> SimpleNamespace:
        calls.append({"rev": rev, "unit_dir": unit_dir, "tag": tag})
        return _fake_side(
            status=ProvisionStatus.BUILD_FAILED,
            reason="archive member is a link, refused: readme.md",
            plan={"refusal": "archive member is a link, refused: readme.md"},
            logs=["archive member is a link, refused: readme.md"],
        )

    monkeypatch.setattr(Orchestrator, "_provision_side_sandbox", fake_method)
    monkeypatch.chdir(tmp_path)  # pd-logs/ land in tmp

    manifest: dict[str, Any] = {
        "units": [
            {
                "unit_id": "T-REAL2",
                "repo": repo,
                "class": "train-bug",
                "sides": [
                    {"role": "fix^", "sha": fix_parent},
                    {"role": "fix", "sha": fix},
                ],
            }
        ]
    }
    result1 = rp.provision_repo(manifest, repo, workdir, 1)
    assert [r["status"] for r in result1["rows"]] == ["BUILD_FAILED"] * 2

    rows1_path = tmp_path / "rows-attempt1.json"
    rows1_path.write_text(json.dumps(result1), encoding="utf-8")
    targets = rp._rerun_targets(rows1_path)
    assert targets == [("T-REAL2", "fix^"), ("T-REAL2", "fix")]
    result2 = rp.provision_repo(manifest, repo, workdir, 2, only=targets)
    assert [r["attempt"] for r in result2["rows"]] == [2, 2]
    assert len(calls) == 4

    # The unit directory handed to the sandbox call is scoped by
    # attempt: attempt 1's two calls share .../a1, attempt 2's share
    # .../a2, and the two are different directories under the same
    # unit. A shared directory (the pre-fix shape) fails here — in
    # production it is what killed the rerun.
    dirs = [c["unit_dir"] for c in calls]
    assert dirs[0] == dirs[1] and dirs[2] == dirs[3]
    assert dirs[0] != dirs[2]
    assert dirs[0].name == "a1" and dirs[2].name == "a2"
    assert dirs[0].parent.name == "T-REAL2"
    assert dirs[0].parent == dirs[2].parent
    assert dirs[0].is_dir() and dirs[2].is_dir()

    # The pd-logs names are attempt-scoped too: both attempts' logs
    # survive side by side instead of the rerun overwriting them.
    names = sorted(p.name for p in (tmp_path / "pd-logs").glob("*.log"))
    assert names == [
        "readiness-T-REAL2-fix-a1.log",
        "readiness-T-REAL2-fix-a2.log",
        "readiness-T-REAL2-fix-parent-a1.log",
        "readiness-T-REAL2-fix-parent-a2.log",
    ]


def _source_with_commits(tmp_path: Path) -> tuple[Path, str, str]:
    """A local file:// source repo with two dated commits."""
    src = tmp_path / "src3"
    src.mkdir()
    _git(src, "init", "-q")
    _git(src, "config", "user.name", "readiness-test")
    _git(src, "config", "user.email", "test@proofdeploy.invalid")
    _git(src, "config", "uploadpack.allowAnySHA1InWant", "true")
    parent = _commit_dated(src, "s1", "2026-03-01T00:00:00+00:00")
    fix = _commit_dated(src, "s2", "2026-03-02T00:00:00+00:00")
    return src, parent, fix


def _seed_clone(workdir: Path, repo: str, src: Path) -> None:
    clone = workdir / "repos" / rp.repo_slug(repo)
    clone.mkdir(parents=True)
    _git(clone, "init", "-q")
    _git(clone, "remote", "add", "origin", src.as_uri())


def test_fetch_sha_retries_and_error_carries_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A fetch that cannot succeed must be retried (three attempts,
    # backoff between them) and the raised error must carry git's
    # own stderr — the Vendure run's exit-128 line carried neither,
    # so its cause was unknowable from the run's output.
    src, _parent, _fix = _source_with_commits(tmp_path)
    workdir = tmp_path / "work"
    repo = "example/widgets"
    _seed_clone(workdir, repo, src)
    clone = workdir / "repos" / rp.repo_slug(repo)
    sleeps: list[float] = []

    def record_sleep(seconds: float) -> None:
        # Only the fetch backoff sleeps are >= 1s; subprocess's own
        # wait-loop polls far below that and is not under test.
        if seconds >= 1.0:
            sleeps.append(seconds)

    monkeypatch.setattr("time.sleep", record_sleep)
    with pytest.raises(RuntimeError) as excinfo:
        rp.fetch_sha(clone, "e" * 40)
    assert "fatal" in str(excinfo.value)
    assert "e" * 40 in str(excinfo.value) or "ref" in str(excinfo.value)
    assert sleeps == [1.0, 2.0]


def test_provision_repo_relative_workdir_yields_absolute_unit_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The workflow passes --workdir readiness-work, a relative path.
    # Podman reads a relative mount source as a named volume, not a
    # bind mount (Bottle's install died on exactly that: the volume
    # name was the relative unit path, slashes and all). The harness
    # must resolve the workdir so the unit directory handed to the
    # sandbox call is absolute.
    src, parent, fix = _source_with_commits(tmp_path)
    repo = "example/widgets"
    monkeypatch.chdir(tmp_path)
    workdir = Path("relwork")
    _seed_clone(workdir, repo, src)
    contract = tmp_path / "widgets.yml"
    contract.write_text(
        'build: "true"\nstart: "python app.py"\nreadiness: "/"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(rp, "contract_path", lambda r: contract)
    monkeypatch.setattr(rp, "assert_measurement_gate", lambda: None)
    seen: list[Path] = []

    def fake_method(
        self: Orchestrator, rev: str, port: int, unit_dir: Path, tag: str
    ) -> SimpleNamespace:
        seen.append(unit_dir)
        return _fake_side()

    monkeypatch.setattr(Orchestrator, "_provision_side_sandbox", fake_method)
    manifest: dict[str, Any] = {
        "units": [
            {
                "unit_id": "T-ABS",
                "repo": repo,
                "class": "train-bug",
                "sides": [
                    {"role": "fix^", "sha": parent},
                    {"role": "fix", "sha": fix},
                ],
            }
        ]
    }
    result = rp.provision_repo(manifest, repo, workdir, 1)
    assert [r["status"] for r in result["rows"]] == ["READY", "READY"]
    assert seen and all(p.is_absolute() for p in seen)
    assert all(str(p).startswith(str(tmp_path)) for p in seen)


def test_provision_repo_unit_failure_isolated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # One unit's harness-level failure (here: its cutoff commit
    # cannot be fetched) must not kill the repo's run: the failed
    # unit gets a typed "harness error:" row per side, the next unit
    # still provisions, and provision_repo returns the full row set
    # (so main always writes the rows file). Vendure lost its whole
    # rows file — and two units — to exactly this.
    src, parent, fix = _source_with_commits(tmp_path)
    repo = "example/widgets"
    workdir = tmp_path / "work"
    _seed_clone(workdir, repo, src)
    contract = tmp_path / "widgets.yml"
    contract.write_text(
        'build: "true"\nstart: "python app.py"\nreadiness: "/"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(rp, "contract_path", lambda r: contract)
    monkeypatch.setattr(rp, "assert_measurement_gate", lambda: None)
    monkeypatch.setattr("time.sleep", lambda _s: None)

    def fake_method(
        self: Orchestrator, rev: str, port: int, unit_dir: Path, tag: str
    ) -> SimpleNamespace:
        return _fake_side()

    monkeypatch.setattr(Orchestrator, "_provision_side_sandbox", fake_method)
    monkeypatch.chdir(tmp_path)
    manifest: dict[str, Any] = {
        "units": [
            {
                "unit_id": "T-BROKEN",
                "repo": repo,
                "class": "checkpoint-bug",
                "sides": [
                    {"role": "fix^", "sha": "d" * 40},
                    {"role": "fix", "sha": "f" * 40},
                ],
            },
            {
                "unit_id": "T-GOOD",
                "repo": repo,
                "class": "train-bug",
                "sides": [
                    {"role": "fix^", "sha": parent},
                    {"role": "fix", "sha": fix},
                ],
            },
        ]
    }
    result = rp.provision_repo(manifest, repo, workdir, 1)
    rows = result["rows"]
    assert len(rows) == 4
    broken = [r for r in rows if r["unit"] == "T-BROKEN"]
    assert [r["status"] for r in broken] == ["BUILD_FAILED", "BUILD_FAILED"]
    assert all(
        str(r["typed_reason"]).startswith("harness error:") for r in broken
    )
    assert all("fatal" in str(r["typed_reason"]) for r in broken)
    good = [r for r in rows if r["unit"] == "T-GOOD"]
    assert [r["status"] for r in good] == ["READY", "READY"]


def test_row_package_manager_comes_from_plan_only() -> None:
    # A refused plan carries no package manager; the row must record
    # None, not the provision result's default (rows from the real
    # pass reported "pip" on refused node plans).
    plan = {"declared_range": None, "refusal": "workspaces not yet supported"}
    side = _fake_side(
        status=ProvisionStatus.BUILD_FAILED,
        reason="workspaces not yet supported",
        plan=plan,
    )
    assert side.provision.package_manager == "npm"  # the provision's default
    row = rp.row_from_provision(
        BUG_UNIT, BUG_UNIT["sides"][0], "h" * 40, 1, "isolated", side
    )
    assert row["package_manager"] is None
    plan_with = {"refusal": None, "package_manager": "bun"}
    side2 = _fake_side(plan=plan_with)
    row2 = rp.row_from_provision(
        BUG_UNIT, BUG_UNIT["sides"][0], "h" * 40, 1, "isolated", side2
    )
    assert row2["package_manager"] == "bun"


def test_provision_repo_mid_unit_failure_one_row_per_side(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A failure INSIDE the side loop (here commit_contract raising
    # on the second side, after the first side provisioned READY)
    # must write exactly one row per attempted side: the failed
    # side gets a harness-error row, the provisioned side keeps its
    # single READY row, and the next unit still provisions. The
    # first cut wrapped the whole unit in one try, so the except
    # re-wrote error rows for every side — one side showed both
    # READY and BUILD_FAILED (reviewer repro on PR #76).
    src, parent, fix = _source_with_commits(tmp_path)
    repo = "example/widgets"
    workdir = tmp_path / "work"
    _seed_clone(workdir, repo, src)
    contract = tmp_path / "widgets.yml"
    contract.write_text(
        'build: "true"\nstart: "python app.py"\nreadiness: "/"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(rp, "contract_path", lambda r: contract)
    monkeypatch.setattr(rp, "assert_measurement_gate", lambda: None)
    monkeypatch.setattr("time.sleep", lambda _s: None)
    real_commit_contract = rp.commit_contract
    fix_calls = {"n": 0}

    def flaky_commit_contract(
        clone: Path, registered_sha: str, contract_path_arg: Path
    ) -> str:
        # Raise only for T-MID's fix side (the first fix call;
        # T-NEXT shares the SHAs and must provision cleanly).
        if registered_sha == fix:
            fix_calls["n"] += 1
            if fix_calls["n"] == 1:
                raise RuntimeError("fatal: simulated contract failure")
        return real_commit_contract(clone, registered_sha, contract_path_arg)

    monkeypatch.setattr(rp, "commit_contract", flaky_commit_contract)

    def fake_method(
        self: Orchestrator, rev: str, port: int, unit_dir: Path, tag: str
    ) -> SimpleNamespace:
        return _fake_side()

    monkeypatch.setattr(Orchestrator, "_provision_side_sandbox", fake_method)
    monkeypatch.chdir(tmp_path)
    unit = {
        "unit_id": "T-MID",
        "repo": repo,
        "class": "train-bug",
        "sides": [
            {"role": "fix^", "sha": parent},
            {"role": "fix", "sha": fix},
        ],
    }
    manifest: dict[str, Any] = {
        "units": [
            unit,
            {
                "unit_id": "T-NEXT",
                "repo": repo,
                "class": "train-bug",
                "sides": [
                    {"role": "fix^", "sha": parent},
                    {"role": "fix", "sha": fix},
                ],
            },
        ]
    }
    result = rp.provision_repo(manifest, repo, workdir, 1)
    rows = result["rows"]
    mid = [(r["role"], r["status"]) for r in rows if r["unit"] == "T-MID"]
    assert mid == [("fix^", "READY"), ("fix", "BUILD_FAILED")]
    fix_row = next(
        r for r in rows if r["unit"] == "T-MID" and r["role"] == "fix"
    )
    assert str(fix_row["typed_reason"]).startswith("harness error:")
    assert "simulated contract failure" in str(fix_row["typed_reason"])
    # Ruling: no harness commit exists for such a side, so
    # harness_sha is None — the registered SHA stands in its own
    # field and is never duplicated into this one.
    assert fix_row["harness_sha"] is None
    nxt = [r["status"] for r in rows if r["unit"] == "T-NEXT"]
    assert nxt == ["READY", "READY"]
    assert len(rows) == 4


# --- plans 1.2h + 1.2i: the committed nest contract -----------------


def _nest_shaped_snapshot(tmp_path: Path) -> Path:
    """A nest-shaped workspace snapshot: root package.json with
    workspaces + a root package-lock.json, one member package."""
    snap = tmp_path / "snap"
    snap.mkdir()
    (snap / "package.json").write_text(
        json.dumps(
            {
                "name": "nest-root",
                "private": True,
                "workspaces": ["packages/*"],
                "engines": {"node": ">=20"},
            }
        ),
        encoding="utf-8",
    )
    (snap / "package-lock.json").write_text("{}", encoding="utf-8")
    member = snap / "packages" / "core"
    member.mkdir(parents=True)
    (member / "package.json").write_text(
        json.dumps({"name": "@nestjs/core"}), encoding="utf-8"
    )
    return snap


def test_nest_contract_plans_flags_and_regd(tmp_path: Path) -> None:
    """The committed nest contract, planned readiness-shaped:
    the adapter's frozen npm ci carries the contract's
    --legacy-peer-deps (plan 1.2h) and the sample install renders
    the unit's cutoff as --before (REG-D, plan 1.2i). This is the
    test both plants target: drop the flag rendering or the
    --before rendering and it fails."""
    contract = load_contract(rp.contract_path("nestjs/nest"))
    assert contract.app_path == "."
    assert contract.npm_flags == ["--legacy-peer-deps"]
    snap = _nest_shaped_snapshot(tmp_path)
    cutoff = "2025-02-03T04:05:06Z"
    plan = NodeAdapter().plan(snap, contract, lock_cutoff=cutoff)
    assert plan.refusal is None
    assert plan.package_manager == "npm"
    assert plan.npm_flags == ["--legacy-peer-deps"]
    install = next(s for s in plan.steps if s.phase == "install")
    assert install.argv is not None
    ci = install.argv.index("ci")
    assert install.argv[ci + 1] == "--legacy-peer-deps"
    build = next(s for s in plan.steps if s.phase == "build")
    assert build.shell is not None
    assert f"--before={cutoff}" in build.shell
    assert "{regd_cutoff}" not in build.shell
    assert "npm ci" not in build.shell
    assert plan.regd == {
        "sample_path": "sample/01-cats-app",
        "cutoff": cutoff,
        "command": (
            "npm --prefix sample/01-cats-app install "
            f"--legacy-peer-deps --before={cutoff}"
        ),
    }


def test_row_from_provision_carries_npm_flags_and_regd() -> None:
    plan = {
        "declared_range": ">=20",
        "package_manager": "npm",
        "npm_flags": ["--legacy-peer-deps"],
        "regd": {
            "sample_path": "sample/01-cats-app",
            "cutoff": "2025-02-03T04:05:06Z",
            "command": (
                "npm --prefix sample/01-cats-app install "
                "--legacy-peer-deps --before=2025-02-03T04:05:06Z"
            ),
        },
        "refusal": None,
    }
    side = _fake_side(plan=plan)
    row = rp.row_from_provision(
        BUG_UNIT, BUG_UNIT["sides"][1], "h" * 40, 1, "isolated", side
    )
    assert row["npm_flags"] == ["--legacy-peer-deps"]
    assert row["regd"]["applied"] is True
    assert row["regd"]["sample_path"] == "sample/01-cats-app"
    assert row["regd"]["cutoff"] == "2025-02-03T04:05:06Z"
    assert row["regd"]["command"].endswith(
        "--before=2025-02-03T04:05:06Z"
    )
    assert row["regd"]["tree_sha256"] is None


def test_row_from_provision_without_regd_marks_not_applied() -> None:
    plan = {"declared_range": ">=22", "package_manager": "pnpm"}
    side = _fake_side(plan=plan)
    row = rp.row_from_provision(
        BUG_UNIT, BUG_UNIT["sides"][1], "h" * 40, 1, "isolated", side
    )
    assert row["npm_flags"] == []
    assert row["regd"]["applied"] is False
    assert row["regd"]["cutoff"] is None


def test_capture_regd_tree_records_sha_and_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REG-D D4: the resolved tree is captured from the
    provisioned snapshot with a real `npm ls --all --json`; its
    sha256 lands on the row and the JSON rides pd-logs/."""
    import hashlib
    import shutil

    if shutil.which("npm") is None:
        pytest.skip("npm not available on this host")
    monkeypatch.chdir(tmp_path)
    snap = tmp_path / "snapshot"
    sample = snap / "sample" / "01-cats-app"
    dep = sample / "node_modules" / "left-pad"
    dep.mkdir(parents=True)
    (sample / "package.json").write_text(
        json.dumps(
            {
                "name": "sample",
                "version": "1.0.0",
                "dependencies": {"left-pad": "1.3.0"},
            }
        ),
        encoding="utf-8",
    )
    (dep / "package.json").write_text(
        json.dumps({"name": "left-pad", "version": "1.3.0"}),
        encoding="utf-8",
    )
    row = {
        "regd": rp._regd_row(
            {
                "sample_path": "sample/01-cats-app",
                "cutoff": "2025-02-03T04:05:06Z",
                "command": "npm --prefix sample/01-cats-app install",
            }
        )
    }
    side = SimpleNamespace(snapshot_dir=snap)
    rp._capture_regd_tree(BUG_UNIT, BUG_UNIT["sides"][1], 1, side, row)
    regd = row["regd"]
    assert regd["tree_error"] is None
    assert regd["tree_artifact"] == "pd-logs/regd-tree-T2-fix-a1.json"
    artifact = tmp_path / "pd-logs" / "regd-tree-T2-fix-a1.json"
    text = artifact.read_text(encoding="utf-8")
    assert regd["tree_sha256"] == hashlib.sha256(
        text.encode()
    ).hexdigest()
    tree = json.loads(text)
    assert tree["dependencies"]["left-pad"]["version"] == "1.3.0"
    # REG-D D4: the version of the npm that captured the tree is
    # recorded on the row.
    import subprocess

    expected_npm = subprocess.run(
        ["npm", "--version"], capture_output=True, text=True
    ).stdout.strip()
    assert regd["tree_npm_version"] == expected_npm


def _regd_captured_row(unit: str, role: str, artifact: str) -> dict:
    return {
        "unit": unit,
        "role": role,
        "regd": {
            "applied": True,
            "tree_sha256": "0" * 64,
            "tree_artifact": artifact,
        },
    }


def _write_tree(tmp_path: Path, name: str, tree: dict) -> str:
    out = tmp_path / "pd-logs" / name
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(tree), encoding="utf-8")
    return f"pd-logs/{name}"


def test_regd_unit_summaries_identical_trees(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REG-D D4 two-side report: both sides applied REG-D and
    captured the SAME resolved tree -> regd_trees_identical."""
    monkeypatch.chdir(tmp_path)
    tree = {
        "name": "sample",
        "version": "1.0.0",
        "dependencies": {
            "a": {"version": "1.0.0"},
            "b": {
                "version": "2.0.0",
                "dependencies": {"c": {"version": "3.0.0"}},
            },
        },
    }
    rows = [
        _regd_captured_row("U1", "fix^", _write_tree(tmp_path, "t1.json", tree)),
        _regd_captured_row("U1", "fix", _write_tree(tmp_path, "t2.json", tree)),
        # A unit with only one captured side gets no summary.
        _regd_captured_row("U2", "C", _write_tree(tmp_path, "t3.json", tree)),
    ]
    assert rp.regd_unit_summaries(rows) == [
        {"unit": "U1", "regd_trees_identical": True}
    ]


def test_regd_unit_summaries_differing_trees_list_the_diff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REG-D D4 two-side report: differing trees are reported
    with the name@version entries added and removed between the
    sides, computed from the two captured JSON trees (nested
    dependencies included)."""
    monkeypatch.chdir(tmp_path)
    first = {
        "dependencies": {
            "a": {"version": "1.0.0"},
            "b": {
                "version": "2.0.0",
                "dependencies": {"c": {"version": "3.0.0"}},
            },
        }
    }
    second = {
        "dependencies": {
            "a": {"version": "1.0.1"},
            "b": {
                "version": "2.0.0",
                "dependencies": {"d": {"version": "4.0.0"}},
            },
        }
    }
    rows = [
        _regd_captured_row(
            "U1", "fix^", _write_tree(tmp_path, "t1.json", first)
        ),
        _regd_captured_row(
            "U1", "fix", _write_tree(tmp_path, "t2.json", second)
        ),
    ]
    (summary,) = rp.regd_unit_summaries(rows)
    assert summary["unit"] == "U1"
    assert summary["regd_trees_identical"] is False
    assert summary["added"] == ["a@1.0.1", "d@4.0.0"]
    assert summary["removed"] == ["a@1.0.0", "c@3.0.0"]


def _regd_errored_row(unit: str, role: str, error: str) -> dict:
    return {
        "unit": unit,
        "role": role,
        "regd": {
            "applied": True,
            "tree_sha256": None,
            "tree_artifact": None,
            "tree_error": error,
        },
    }


def test_regd_unit_summaries_unreadable_trees_emit_null_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REG-D D4 two-side report: a unit where REG-D applied on
    both sides but a tree cannot be read back is emitted with
    regd_trees_identical null and the per-side tree_errors —
    never dropped silently. Covers all three unreadable shapes:
    the capture itself failed (tree_error on the row), the
    artifact is missing at read-back, and the artifact is
    unparseable. A unit where REG-D applied on one side only
    still gets no summary."""
    monkeypatch.chdir(tmp_path)
    tree = {"dependencies": {"a": {"version": "1.0.0"}}}
    good = _write_tree(tmp_path, "good.json", tree)
    broken = tmp_path / "pd-logs" / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    rows = [
        # U1: the fix side's capture failed; its row carries the
        # error and no artifact.
        _regd_captured_row("U1", "fix^", good),
        _regd_errored_row(
            "U1", "fix", "RuntimeError: npm ls exited 1 with no tree"
        ),
        # U2: the fix side's artifact was recorded at capture
        # time but is gone when the summary reads it back.
        _regd_captured_row("U2", "fix^", good),
        _regd_captured_row("U2", "fix", "pd-logs/missing.json"),
        # U3: the fix side's artifact is unparseable.
        _regd_captured_row("U3", "fix^", good),
        _regd_captured_row("U3", "fix", "pd-logs/broken.json"),
        # U4: REG-D applied on one side only -> no summary.
        _regd_captured_row("U4", "C", good),
    ]
    summaries = rp.regd_unit_summaries(rows)
    by_unit = {s["unit"]: s for s in summaries}
    assert set(by_unit) == {"U1", "U2", "U3"}
    assert by_unit["U1"] == {
        "unit": "U1",
        "regd_trees_identical": None,
        "tree_errors": {
            "fix": "RuntimeError: npm ls exited 1 with no tree"
        },
    }
    assert by_unit["U2"]["regd_trees_identical"] is None
    assert set(by_unit["U2"]["tree_errors"]) == {"fix"}
    assert by_unit["U2"]["tree_errors"]["fix"].startswith(
        "FileNotFoundError"
    )
    assert by_unit["U3"]["regd_trees_identical"] is None
    assert set(by_unit["U3"]["tree_errors"]) == {"fix"}
    assert by_unit["U3"]["tree_errors"]["fix"].startswith(
        "JSONDecodeError"
    )


def test_capture_regd_tree_failure_is_recorded_not_silent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    row = {
        "regd": rp._regd_row(
            {
                "sample_path": "sample/01-cats-app",
                "cutoff": "2025-02-03T04:05:06Z",
                "command": "npm --prefix sample/01-cats-app install",
            }
        )
    }
    # The snapshot has no sample directory at all: the capture
    # fails, and the failure is recorded on the row (D4 fields are
    # never silently absent).
    side = SimpleNamespace(snapshot_dir=tmp_path / "no-such-snapshot")
    rp._capture_regd_tree(BUG_UNIT, BUG_UNIT["sides"][1], 1, side, row)
    assert row["regd"]["tree_sha256"] is None
    assert row["regd"]["tree_artifact"] is None
    assert row["regd"]["tree_error"]


# --- auth proof harness (D87) ------------------------------------------

_FAKE_ADMIN_KEY = "0123456789abcdef01234567:" + "ab" * 32
_FAKE_JWT = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
    ".eyJzdWIiOiIxMjM0NTY3ODkwIn0.fake-signature_part"
)
_FAKE_BOOTSTRAP = "generated-bootstrap-password"

GHOST_UNIT: dict[str, Any] = {
    "unit_id": "checkpoint-bug-ghost-1",
    "repo": "TryGhost/Ghost",
    "class": "heldout-bug",
    "sides": [
        {"role": "fix^", "sha": "a" * 40},
        {"role": "fix", "sha": "b" * 40},
    ],
}


def test_redactor_scrubs_by_value_then_by_shape() -> None:
    redactor = ap.Redactor()
    redactor.add("key_file", _FAKE_ADMIN_KEY)
    redactor.add("bootstrap_value", _FAKE_BOOTSTRAP)
    out = redactor.scrub(
        f"key={_FAKE_ADMIN_KEY} pw={_FAKE_BOOTSTRAP} "
        f"tok={_FAKE_JWT} again={_FAKE_ADMIN_KEY}"
    )
    assert _FAKE_ADMIN_KEY not in out
    assert _FAKE_BOOTSTRAP not in out
    assert _FAKE_JWT not in out
    # The JWT was never registered as a value, so the shape sweep
    # is what caught it — the counts keep the two apart.
    assert redactor.counts == {
        "key_file": 2,
        "bootstrap_value": 1,
        "acquired_token": 0,
        "jwt_shape": 1,
    }


def test_parse_only() -> None:
    assert ap._parse_only(None) is None
    assert ap._parse_only(["checkpoint-bug-ghost-1:fix"]) == [
        ("checkpoint-bug-ghost-1", "fix")
    ]
    with pytest.raises(ValueError, match="--only expects"):
        ap._parse_only(["no-role-separator"])


def _one_commit_src(tmp_path: Path) -> tuple[Path, str]:
    src = tmp_path / "src"
    src.mkdir()
    _git(src, "init", "-q")
    _git(src, "config", "user.name", "auth-proof-test")
    _git(src, "config", "user.email", "test@proofdeploy.invalid")
    _git(src, "config", "uploadpack.allowAnySHA1InWant", "true")
    (src / "app.txt").write_text("v1\n", encoding="utf-8")
    _git(src, "add", "-A")
    _git(src, "commit", "-q", "-m", "base")
    return src, _git(src, "rev-parse", "HEAD")


def _empty_clone(tmp_path: Path, src: Path) -> Path:
    clone = tmp_path / "clone"
    clone.mkdir()
    _git(clone, "init", "-q")
    _git(clone, "remote", "add", "origin", src.as_uri())
    return clone


def test_commit_contract_and_seed_delta_is_exactly_two_files(
    tmp_path: Path,
) -> None:
    src, sha = _one_commit_src(tmp_path)
    clone = _empty_clone(tmp_path, src)
    rp.fetch_sha(clone, sha)
    harness = ap.commit_contract_and_seed(
        clone, sha, rp.contract_path("TryGhost/Ghost"), ap.SEED_SOURCE
    )
    assert _git(clone, "show", "-s", "--format=%P", harness) == sha
    changed = _git(
        clone, "diff-tree", "--no-commit-id", "--name-only", "-r", harness
    )
    assert changed.splitlines() == ["ghost-seed.js", "proofdeploy.yml"]
    assert _git(clone, "show", f"{harness}:ghost-seed.js") == (
        ap.SEED_SOURCE.read_text(encoding="utf-8").strip()
    )
    assert _git(clone, "show", f"{harness}:proofdeploy.yml") == (
        rp.contract_path("TryGhost/Ghost").read_text(encoding="utf-8").strip()
    )


class _FakeExecutor:
    """The executor's shape for the proof: an authenticated probe
    PASSes at 200 with one mint (and, to prove the scrub, a detail
    line carrying the minted token); the bare probe PASSes at 403
    (its assertion is the refusal)."""

    def __init__(self) -> None:
        self.probes: list[dict[str, Any]] = []

    def run_probe(self, probe: dict[str, Any], index: int = 0) -> ProbeResult:
        self.probes.append(probe)
        headers = probe["act"].get("headers") or {}
        if "Authorization" in headers:
            return ProbeResult(
                probe_index=index,
                verdict=Verdict.PASS,
                details=[
                    "credential provider: minted ghost_jwt (ttl 300s)",
                    "act: GET /ghost/api/admin/tags/ -> 200",
                    f"sent authorization: Ghost {_FAKE_JWT}",
                ],
                http_status=200,
                minted_tokens=[_FAKE_JWT],
            )
        return ProbeResult(
            probe_index=index,
            verdict=Verdict.PASS,
            details=["act: GET /ghost/api/admin/tags/ -> 403"],
            http_status=403,
        )


def _fake_auth_side(tmp_path: Path) -> SimpleNamespace:
    snapshot = tmp_path / "snapshot"
    (snapshot / ".pd-auth").mkdir(parents=True)
    (snapshot / ".pd-auth" / "admin_key").write_text(
        _FAKE_ADMIN_KEY + "\n", encoding="utf-8"
    )
    data_dir = snapshot / "ghost" / "core" / "content" / "data"
    data_dir.mkdir(parents=True)
    (data_dir / "ghost.db").write_bytes(b"sqlite-bytes")
    prov = ProvisionResult(
        status=ProvisionStatus.READY,
        reason=None,
        runtime="node",
        runtime_version="22.11.0",
        package_manager="pnpm",
        install_plan={},
        install_plan_sha256="ab" * 32,
        logs=[f"seed ran with ADMIN_PASSWORD={_FAKE_BOOTSTRAP}", "ready"],
        proc=_FakeProc(),
    )
    return SimpleNamespace(
        sha="h" * 40,
        provision=prov,
        sandbox_evidence={"app_network_mode": "none+socket"},
        contract=None,
        snapshot_dir=snapshot,
        bootstrap_env={
            "ADMIN_EMAIL": "boot@proofdeploy.invalid",
            "ADMIN_PASSWORD": _FAKE_BOOTSTRAP,
        },
        generated_secrets=[_FAKE_BOOTSTRAP, _FAKE_ADMIN_KEY],
    )


def test_auth_proof_side_proves_auth_through_the_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src, sha = _one_commit_src(tmp_path)
    clone = _empty_clone(tmp_path, src)
    side = _fake_auth_side(tmp_path)
    executor = _FakeExecutor()
    orch = SimpleNamespace(
        _provision_side_sandbox=(
            lambda rev, port, unit_dir, tag: side
        ),
        _make_executor=lambda s: executor,
    )
    monkeypatch.chdir(tmp_path)  # pd-logs/ land in tmp
    row = ap.auth_proof_side(
        orch,
        clone,
        GHOST_UNIT,
        {"role": "fix", "sha": sha},
        rp.contract_path("TryGhost/Ghost"),
        1,
        tmp_path / "work",
        "isolated",
    )
    assert row["kind"] == "auth-proof"
    assert row["status"] == "READY"
    assert row["registered_sha"] == sha
    # The seed's traces in the snapshot: key file present, and the
    # SQLite file the app side serves from is named.
    assert row["seed"]["key_file_present"] is True
    assert row["seed"]["key_file"] == ".pd-auth/admin_key"
    assert row["seed"]["sqlite_files"] == [
        "ghost/core/content/data/ghost.db (12 bytes)"
    ]
    # The proof itself: 200, 200 with a second mint, 403 bare.
    auth = row["auth"]
    assert auth["passed"] is True
    assert auth["status"] == "PROVEN"
    assert auth["endpoint"] == "/ghost/api/admin/tags/"
    assert [r["http_status"] for r in auth["requests"]] == [200, 200, 403]
    assert [r["acquisitions"] for r in auth["requests"]] == [1, 1, 0]
    assert [r["acquisition_kinds"] for r in auth["requests"]] == [
        {"mint": 1},
        {"mint": 1},
        {},
    ]
    assert auth["acquisitions_after_two_authenticated"] == 2
    assert auth["acquisition_kinds"] == {"mint": 2}
    assert auth["requests"][0]["authorization"] == (
        "Ghost <redacted:acquired_token>"
    )
    assert auth["requests"][2]["authorization"] is None
    # The probes the executor actually received: two carrying the
    # ${AUTH_TOKEN} header for the provider to fill, one bare.
    assert len(executor.probes) == 3
    assert executor.probes[0]["act"]["headers"]["Authorization"] == (
        "Ghost ${AUTH_TOKEN}"
    )
    assert "headers" not in executor.probes[2]["act"]
    # All three probes target the D91 endpoint, and the bare
    # probe's assertion is the 403 refusal.
    assert [p["act"]["path"] for p in executor.probes] == [
        "/ghost/api/admin/tags/"
    ] * 3
    assert executor.probes[2]["assert"] == [
        {"type": "status", "equals": 403}
    ]
    # No secret value reaches the row or the dumped log; the counts
    # say what was scrubbed (the JWT detail line twice, the
    # bootstrap password in the provision log once).
    blob = json.dumps(row)
    assert _FAKE_ADMIN_KEY not in blob
    assert _FAKE_JWT not in blob
    assert _FAKE_BOOTSTRAP not in blob
    assert row["redactions"]["acquired_token"] == 2
    assert row["redactions"]["bootstrap_value"] == 1
    log_text = (
        tmp_path / "pd-logs" / "authproof-checkpoint-bug-ghost-1-fix-a1.log"
    ).read_text(encoding="utf-8")
    assert _FAKE_BOOTSTRAP not in log_text
    assert _FAKE_ADMIN_KEY not in log_text
    # The live side was stopped.
    assert side.provision.proc.stopped is True


def _proof_record(
    http_status: int,
    *,
    verdict: str = "pass",
    acquisitions: int = 0,
    kinds: dict[str, int] | None = None,
    authorization: str | None = None,
    body: Any = None,
) -> dict[str, Any]:
    """One request record as _request_record shapes it, for
    driving the pure verdict directly (D106)."""
    return {
        "http_status": http_status,
        "verdict": verdict,
        "acquisitions": acquisitions,
        "acquisition_kinds": {} if kinds is None else kinds,
        "authorization": authorization,
        "body": body,
    }


_GHOST_SPEC = ap.spec_for_repo("TryGhost/Ghost")
_DIRECTUS_SPEC = ap.spec_for_repo("directus/directus")
_VENDURE_SPEC = ap.spec_for_repo("vendure-ecommerce/vendure")


def _minted(http_status: int, **kw: Any) -> dict[str, Any]:
    """An authenticated Ghost-shaped record: one mint."""
    return _proof_record(
        http_status,
        acquisitions=1,
        kinds={"mint": 1},
        authorization="Ghost <redacted:acquired_token>",
        **kw,
    )


def test_auth_proof_verdict_accepts_only_the_exact_proof() -> None:
    records = [
        _minted(200),
        _minted(200),
        _proof_record(403),
    ]
    assert ap._proof_passed(True, records, _GHOST_SPEC) is True
    # The seed's key file missing fails the proof on its own.
    assert ap._proof_passed(False, records, _GHOST_SPEC) is False


def test_auth_proof_verdict_accepts_the_directus_shape() -> None:
    # Directus (D113/D137): two authenticated 200s, each with its
    # own fresh login, and the bare request refused with 401. No
    # key file is declared, so the caller's seed conjunct is True.
    records = [
        _proof_record(
            200, acquisitions=1, kinds={"login": 1},
            authorization="Bearer <redacted:acquired_token>",
        ),
        _proof_record(
            200, acquisitions=1, kinds={"login": 1},
            authorization="Bearer <redacted:acquired_token>",
        ),
        _proof_record(401),
    ]
    assert ap._proof_passed(True, records, _DIRECTUS_SPEC) is True
    # The same records under Ghost's spec fail: the expected
    # unauthenticated status (and kind) are inputs, not constants.
    assert ap._proof_passed(True, records, _GHOST_SPEC) is False


def test_auth_proof_verdict_fails_when_unauthenticated_accepted() -> None:
    # The /site/ failure the proof exists to catch: the bare
    # request answers 200. Every leg's own verdict still reads
    # "pass" here, so only the status conjunct can fail it.
    records = [
        _minted(200),
        _minted(200),
        _proof_record(200),
    ]
    assert ap._proof_passed(True, records, _GHOST_SPEC) is False


def test_auth_proof_verdict_fails_on_acquisition_count_one() -> None:
    records = [
        _minted(200),
        _proof_record(200, authorization="Ghost <redacted:acquired_token>"),
        _proof_record(403),
    ]
    assert ap._proof_passed(True, records, _GHOST_SPEC) is False


def test_auth_proof_verdict_fails_on_acquisition_count_three() -> None:
    records = [
        _proof_record(
            200, acquisitions=2, kinds={"mint": 2},
            authorization="Ghost <redacted:acquired_token>",
        ),
        _minted(200),
        _proof_record(403),
    ]
    assert ap._proof_passed(True, records, _GHOST_SPEC) is False


def test_auth_proof_verdict_fails_on_authenticated_non_200() -> None:
    records = [
        _minted(500),
        _minted(200),
        _proof_record(403),
    ]
    assert ap._proof_passed(True, records, _GHOST_SPEC) is False
    records = [
        _minted(200),
        _minted(401),
        _proof_record(403),
    ]
    assert ap._proof_passed(True, records, _GHOST_SPEC) is False


def test_auth_proof_verdict_fails_on_split_acquisitions() -> None:
    # The summed-check hole (D137 reviewer): 2 acquisitions on leg
    # 1 and 0 on leg 2 totals two, but leg 2 was served by a token
    # it did not freshly acquire — each leg must show exactly one.
    records = [
        _proof_record(
            200, acquisitions=2, kinds={"login": 2},
            authorization="Bearer <redacted:acquired_token>",
        ),
        _proof_record(
            200, authorization="Bearer <redacted:acquired_token>"
        ),
        _proof_record(401),
    ]
    assert ap._proof_passed(True, records, _DIRECTUS_SPEC) is False
    # And the mirror image: 0 on leg 1, 2 on leg 2.
    records = [
        _proof_record(
            200, authorization="Bearer <redacted:acquired_token>"
        ),
        _proof_record(
            200, acquisitions=2, kinds={"login": 2},
            authorization="Bearer <redacted:acquired_token>",
        ),
        _proof_record(401),
    ]
    assert ap._proof_passed(True, records, _DIRECTUS_SPEC) is False


def test_auth_proof_verdict_fails_on_wrong_acquisition_kind() -> None:
    # The right count with the wrong kind: a refresh is not the
    # fresh login the Directus spec registers per leg.
    records = [
        _proof_record(
            200, acquisitions=1, kinds={"refresh": 1},
            authorization="Bearer <redacted:acquired_token>",
        ),
        _proof_record(
            200, acquisitions=1, kinds={"login": 1},
            authorization="Bearer <redacted:acquired_token>",
        ),
        _proof_record(401),
    ]
    assert ap._proof_passed(True, records, _DIRECTUS_SPEC) is False


def test_auth_proof_verdict_fails_when_bare_leg_acquires() -> None:
    # The bare leg must be credential-free: an acquisition on it
    # (the provider-configured executor's proactive ensure) or an
    # Authorization header shape both void the leg.
    records = [
        _proof_record(
            200, acquisitions=1, kinds={"login": 1},
            authorization="Bearer <redacted:acquired_token>",
        ),
        _proof_record(
            200, acquisitions=1, kinds={"login": 1},
            authorization="Bearer <redacted:acquired_token>",
        ),
        _proof_record(401, acquisitions=1, kinds={"login": 1}),
    ]
    assert ap._proof_passed(True, records, _DIRECTUS_SPEC) is False
    records[2] = _proof_record(
        401, authorization="Bearer <redacted:acquired_token>"
    )
    assert ap._proof_passed(True, records, _DIRECTUS_SPEC) is False


def _vendure_records(
    *,
    me_id: Any = "1",
    bare_body: Any = None,
) -> list[dict[str, Any]]:
    """Vendure-shaped records (D137): GraphQL answers refusals as
    HTTP 200 with an error body, so the spec's body checks carry
    what the status cannot."""
    if bare_body is None:
        bare_body = {
            "errors": [
                {
                    "message": "Forbidden",
                    "extensions": {"code": "FORBIDDEN"},
                }
            ]
        }
    authenticated = {
        "data": {"me": {"id": me_id, "identifier": "superadmin"}}
    }
    return [
        _proof_record(
            200, acquisitions=1, kinds={"login": 1},
            authorization="Bearer <redacted:acquired_token>",
            body=authenticated,
        ),
        _proof_record(
            200, acquisitions=1, kinds={"login": 1},
            authorization="Bearer <redacted:acquired_token>",
            body=authenticated,
        ),
        _proof_record(200, body=bare_body),
    ]


def test_auth_proof_verdict_accepts_the_vendure_shape() -> None:
    assert (
        ap._proof_passed(True, _vendure_records(), _VENDURE_SPEC)
        is True
    )


def test_auth_proof_verdict_fails_when_vendure_me_is_null() -> None:
    # A 200 whose data.me is null is the FORBIDDEN refusal wearing
    # a success status; only the body check can fail it.
    records = _vendure_records()
    records[0]["body"] = {"data": {"me": None}}
    assert ap._proof_passed(True, records, _VENDURE_SPEC) is False
    records = _vendure_records(me_id=None)
    assert ap._proof_passed(True, records, _VENDURE_SPEC) is False


def test_auth_proof_verdict_fails_when_vendure_bare_lacks_forbidden() -> None:
    # The bare leg at 200 without errors[0].extensions.code ==
    # "FORBIDDEN" is not the registered refusal.
    records = _vendure_records(bare_body={"data": {"me": None}})
    assert ap._proof_passed(True, records, _VENDURE_SPEC) is False
    records = _vendure_records(
        bare_body={
            "errors": [{"extensions": {"code": "INTERNAL_ERROR"}}]
        }
    )
    assert ap._proof_passed(True, records, _VENDURE_SPEC) is False


class _ScriptedExecutor:
    """The executor's shape with scripted per-leg outcomes:
    legs are (http_status, acquisitions) in call order. A leg's
    verdict follows its own assertion exactly as the real
    executor's does — PASS iff the observed status equals the
    asserted one — so a wrong status also fails that leg; the
    composed row must still read FAILED, never PROVEN (D106)."""

    def __init__(self, legs: list[tuple[int, int]]) -> None:
        self.legs = legs
        self.probes: list[dict[str, Any]] = []

    def run_probe(self, probe: dict[str, Any], index: int = 0) -> ProbeResult:
        self.probes.append(probe)
        status, acquisitions = self.legs[index]
        expected = probe["assert"][0]["equals"]
        details = [
            "credential provider: minted ghost_jwt (ttl 300s)"
        ] * acquisitions
        details.append(f"act: GET {probe['act']['path']} -> {status}")
        return ProbeResult(
            probe_index=index,
            verdict=Verdict.PASS if status == expected else Verdict.FAIL,
            details=details,
            http_status=status,
            minted_tokens=[_FAKE_JWT] * acquisitions,
        )


def _scripted_proof_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    legs: list[tuple[int, int]],
) -> dict[str, Any]:
    src, sha = _one_commit_src(tmp_path)
    clone = _empty_clone(tmp_path, src)
    side = _fake_auth_side(tmp_path)
    executor = _ScriptedExecutor(legs)
    orch = SimpleNamespace(
        _provision_side_sandbox=(lambda rev, port, unit_dir, tag: side),
        _make_executor=lambda s: executor,
    )
    monkeypatch.chdir(tmp_path)  # pd-logs/ land in tmp
    return ap.auth_proof_side(
        orch,
        clone,
        GHOST_UNIT,
        {"role": "fix", "sha": sha},
        rp.contract_path("TryGhost/Ghost"),
        1,
        tmp_path / "work",
        "isolated",
    )


def test_auth_proof_side_fails_when_unauthenticated_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = _scripted_proof_row(
        tmp_path, monkeypatch, [(200, 1), (200, 1), (200, 0)]
    )
    assert row["auth"]["requests"][2]["http_status"] == 200
    assert row["auth"]["passed"] is False
    assert row["auth"]["status"] == "FAILED"


def test_auth_proof_side_fails_on_acquisition_count_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = _scripted_proof_row(
        tmp_path, monkeypatch, [(200, 1), (200, 0), (403, 0)]
    )
    assert row["auth"]["acquisitions_after_two_authenticated"] == 1
    assert row["auth"]["passed"] is False
    assert row["auth"]["status"] == "FAILED"


def test_auth_proof_side_fails_on_acquisition_count_three(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = _scripted_proof_row(
        tmp_path, monkeypatch, [(200, 2), (200, 1), (403, 0)]
    )
    assert row["auth"]["acquisitions_after_two_authenticated"] == 3
    assert row["auth"]["passed"] is False
    assert row["auth"]["status"] == "FAILED"


def test_auth_proof_side_fails_on_authenticated_non_200(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = _scripted_proof_row(
        tmp_path, monkeypatch, [(500, 1), (200, 1), (403, 0)]
    )
    assert row["auth"]["requests"][0]["http_status"] == 500
    assert row["auth"]["passed"] is False
    assert row["auth"]["status"] == "FAILED"


# ----------------------------------------------------------------------
# The generic driver (D137): per-repo inputs are only the endpoint
# + expected unauthenticated status; the provider and the token
# presentation come from the repo's committed contract, and
# acquisitions are counted by kind from the provider's details.
# ----------------------------------------------------------------------


def test_proof_specs_registered_for_the_three_checkpoint_repos() -> None:
    ghost = ap.spec_for_repo("TryGhost/Ghost")
    assert (ghost.method, ghost.path, ghost.unauthenticated_status) == (
        "GET",
        "/ghost/api/admin/tags/",
        403,
    )
    directus = ap.spec_for_repo("directus/directus")
    assert (directus.method, directus.path, directus.unauthenticated_status) == (
        "GET",
        "/users/me",
        401,
    )
    vendure = ap.spec_for_repo("vendure-ecommerce/vendure")
    assert vendure.method == "POST"
    assert vendure.path == "/admin-api"
    assert vendure.body == {"query": "query { me { id identifier } }"}
    # The per-leg acquisition kinds each spec registers (D137).
    assert ghost.acquisition_kind == "mint"
    assert directus.acquisition_kind == "login"
    assert vendure.acquisition_kind == "login"
    # Vendure refuses as HTTP 200 + a FORBIDDEN GraphQL error
    # body, so its bare-leg expectation is 200 PLUS the body
    # check, and its authenticated legs carry the data.me.id
    # non-null check. Ghost and Directus refuse with real HTTP
    # statuses and register no body checks.
    assert vendure.unauthenticated_status == 200
    assert vendure.authenticated_body_path == "data.me.id"
    assert vendure.unauthenticated_body_path == (
        "errors[0].extensions.code"
    )
    assert vendure.unauthenticated_body_value == "FORBIDDEN"
    assert ghost.authenticated_body_path is None
    assert ghost.unauthenticated_body_path is None
    assert directus.authenticated_body_path is None
    assert directus.unauthenticated_body_path is None
    with pytest.raises(ValueError, match="no auth-proof spec"):
        ap.spec_for_repo("nestjs/nest")


def test_presentation_scheme_comes_from_the_committed_contract() -> None:
    from proofdeploy.yml import load_contract

    ghost = load_contract(rp.contract_path("TryGhost/Ghost"))
    assert ap.presentation_scheme(ghost) == "Ghost"
    directus = load_contract(rp.contract_path("directus/directus"))
    assert ap.presentation_scheme(directus) == "Bearer"


def test_seed_snapshot_files_follow_the_contract_seed_command() -> None:
    from proofdeploy.yml import load_contract

    ghost = load_contract(rp.contract_path("TryGhost/Ghost"))
    assert ap.seed_snapshot_files(ghost) == [ap.SEED_SOURCE]
    # Directus declares no seed script; Vendure's seed names an
    # in-tree command — neither adds a file to the harness commit.
    directus = load_contract(rp.contract_path("directus/directus"))
    assert ap.seed_snapshot_files(directus) == []
    vendure = load_contract(rp.contract_path("vendure-ecommerce/vendure"))
    assert ap.seed_snapshot_files(vendure) == []


def test_acquisition_kinds_counted_by_kind_from_provider_details() -> None:
    details = [
        "credential provider: minted ghost_jwt (ttl 300s)",
        "credential provider: POST /auth/login (login)",
        "credential provider: POST /auth/refresh",
        "credential provider: POST /register (register)",
        # Not acquisitions: outcomes and failures are not counted.
        "credential provider: token acquired",
        "credential provider failed: login endpoint returned 500",
        "act: GET /users/me -> 200",
    ]
    assert ap._acquisition_kinds(details) == {
        "mint": 1,
        "login": 1,
        "refresh": 1,
        "register": 1,
    }
    assert ap._acquisition_kinds([]) == {}


_FAKE_DIRECTUS_TOKEN = "directus-fake-token-0123456789"


class _LoginScriptedExecutor:
    """The executor's shape for a login-mode proof (Directus):
    each authenticated leg acquires by login (the measured path's
    fresh context per probe) and one detail line echoes the token,
    to prove the scrub; verdicts follow the legs' assertions."""

    def __init__(self, statuses: list[int]) -> None:
        self.statuses = statuses
        self.probes: list[dict[str, Any]] = []

    def run_probe(self, probe: dict[str, Any], index: int = 0) -> ProbeResult:
        self.probes.append(probe)
        status = self.statuses[index]
        expected = probe["assert"][0]["equals"]
        headers = probe["act"].get("headers") or {}
        details = [f"act: GET {probe['act']['path']} -> {status}"]
        minted: list[str] = []
        if "Authorization" in headers:
            details = [
                "credential provider: POST /auth/login (login)",
                "credential provider: token acquired",
                f"sent authorization: Bearer {_FAKE_DIRECTUS_TOKEN}",
            ] + details
            minted = [_FAKE_DIRECTUS_TOKEN]
        return ProbeResult(
            probe_index=index,
            verdict=Verdict.PASS if status == expected else Verdict.FAIL,
            details=details,
            http_status=status,
            minted_tokens=minted,
        )


def test_auth_proof_side_proves_directus_with_login_acquisitions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src, sha = _one_commit_src(tmp_path)
    clone = _empty_clone(tmp_path, src)
    side = _fake_auth_side(tmp_path)
    executor = _LoginScriptedExecutor([200, 200, 401])
    orch = SimpleNamespace(
        _provision_side_sandbox=(
            lambda rev, port, unit_dir, tag: side
        ),
        _make_executor=lambda s: executor,
    )
    monkeypatch.chdir(tmp_path)  # pd-logs/ land in tmp
    manifest = rp.load_manifest(rp.DEFAULT_MANIFEST)
    unit = next(
        u
        for u in rp.units_for_repo(manifest, "directus/directus")
        if u["unit_id"] == "checkpoint-bug-directus-1"
    )
    row = ap.auth_proof_side(
        orch,
        clone,
        unit,
        {"role": "fix", "sha": sha},
        rp.contract_path("directus/directus"),
        1,
        tmp_path / "work",
        "isolated",
    )
    auth = row["auth"]
    assert auth["passed"] is True
    assert auth["status"] == "PROVEN"
    assert auth["endpoint"] == "/users/me"
    assert [r["http_status"] for r in auth["requests"]] == [200, 200, 401]
    # Each authenticated request acquired by its own fresh login.
    assert [r["acquisition_kinds"] for r in auth["requests"]] == [
        {"login": 1},
        {"login": 1},
        {},
    ]
    assert auth["acquisitions_after_two_authenticated"] == 2
    assert auth["acquisition_kinds"] == {"login": 2}
    # Presentation read from the Directus contract: Bearer.
    assert auth["requests"][0]["authorization"] == (
        "Bearer <redacted:acquired_token>"
    )
    assert executor.probes[0]["act"]["headers"]["Authorization"] == (
        "Bearer ${AUTH_TOKEN}"
    )
    assert [p["act"]["path"] for p in executor.probes] == ["/users/me"] * 3
    assert executor.probes[2]["assert"] == [
        {"type": "status", "equals": 401}
    ]
    # No key file is declared by the Directus contract: no seed
    # conjunct, recorded as None.
    assert row["seed"]["key_file_present"] is None
    assert row["seed"]["key_file"] is None
    # The acquired token's value never reaches the row.
    assert _FAKE_DIRECTUS_TOKEN not in json.dumps(row)
    assert side.provision.proc.stopped is True
