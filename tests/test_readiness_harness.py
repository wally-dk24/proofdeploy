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

import readiness.provision as rp
from proofdeploy import orchestrator as orch_mod
from proofdeploy.orchestrator import MeasureConfig, Orchestrator
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
    assert len(units) == 27
    assert sum(len(u["sides"]) for u in units) == 42
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
    # No manager on the plan: fall back to the provision's.
    assert row["package_manager"] == "npm"
    assert row["last_error"] == "workspaces not yet supported"
    assert row["network_mode"] == "isolated"
    assert row["attempt"] == 2


def test_row_from_exception_types_runtime_detection() -> None:
    exc = ValueError(
        "cannot detect runtime in /snap: no package.json or pyproject.toml"
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
    assert (tmp_path / "pd-logs" / "readiness-T2-fix-parent.log").is_file()


def test_provision_side_types_a_raising_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def raising_method(
        self: Orchestrator, rev: str, port: int, unit_dir: Path, tag: str
    ) -> SimpleNamespace:
        raise ValueError(
            "cannot detect runtime in /snap: no package.json or pyproject.toml"
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
    # own env; Ghost's readiness contract has NO auth at all. The
    # one provider declaration is Directus's, from its approved
    # fixture (env NAMES only — values are generated per run).
    for path in sorted(rp.CONTRACTS_DIR.glob("*.yml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if path.name == "directus__directus.yml":
            assert data.get("auth", {}).get("mode") == "refresh"
        else:
            assert "auth" not in data, path.name


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
