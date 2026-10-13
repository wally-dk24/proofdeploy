"""WO-8.1c: the root project's build backend — recorded,
constrained to the unit's cutoff where the tool allows it, and
typed as a limitation where it does not.

The gap (Addendum 54): when a unit installs with uv, poetry, or
pdm, the install step also builds the PROJECT ITSELF (``uv
sync``, ``poetry install``, ``pdm sync`` build the root
project), and the project's [build-system] requires resolve
fresh from PyPI inside build isolation — unpinned, unhashed,
and with NO date cutoff. Two runs of the same commit can build
with different backend versions. The pip path does not install
the root project, so it does not have this gap.

What this file proves, per pinned tool (verified empirically
against the pinned tools before the change was written —
2026-10-13, uv 0.13.0 / poetry 2.5.1 / pdm 2.29.2):

- uv: UV_EXCLUDE_NEWER on the sync step constrains ONLY the
  build resolution (a 2023-06-01 cutoff built the fixture with
  setuptools 67.8.0; unconstrained it built with 84.0.0; the
  frozen lock's own files installed either way). The plan puts
  the unit's cutoff there and records it as build_cutoff.
- pdm: no build-scoped cutoff exists. Its only date cutoff
  (strategy.exclude-newer) is resolution-wide and also filters
  the committed lock's own files at sync time (a locked package
  uploaded after the cutoff is refused), and it has no
  per-invocation form — so the plan records the typed
  limitation (PDM_BUILD_CUTOFF_LIMITATION) instead of breaking
  the frozen install.
- poetry: `poetry install` builds the root project itself with
  the poetry-core pinned by hash in the committed bootstrap
  file (the fixture's declared setuptools backend was never
  fetched; the built wheel is stamped "Generator: poetry-core
  2.5.0"). Nothing resolves at build time, so the plan records
  the backend name + pinned version outright. Poetry's
  ``install --sync`` is unchanged (kept per REG-A).

And for every manager: the side's record carries the backend
name + resolved version — the plan's fields, completed by the
executors reading the built wheel's Generator back from the
venv (build_backend_evidence), so the version recorded is the
one the tool ACTUALLY used, not the requirement string.

Offline behavior: the host E2E tests at the bottom run the real
pinned tools via the plans' own bootstrap (network) — they FAIL
rather than skip when the network is down: a green offline run
would prove nothing about installs (the WO-8.1b convention).
"""

from __future__ import annotations

import hashlib
import json
import shutil
import socket
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

FIXTURES = Path(__file__).parent / "fixtures" / "wo81b"

CUTOFF = "2026-01-02T03:04:05Z"

PINNED = {
    "uv": ("0.13.0", "uv.lock"),
    "poetry": ("2.5.1", "poetry.lock"),
    "pdm": ("2.29.2", "pdm.lock"),
}


def _contract():
    from proofdeploy.yml import RepoContract

    return RepoContract(
        build="python -m py_compile app.py",
        start="python app.py",
        readiness="/health",
        env={},
    )


def _plan(manager: str, lock_cutoff=CUTOFF):
    from proofdeploy.provisioning import plan_provisioning

    return plan_provisioning(
        FIXTURES / manager, _contract(), lock_cutoff=lock_cutoff
    )


def _sync_step(plan, manager: str):
    """The plan's frozen-install step for a manager (the one
    whose argv ends the frozen install, not the lock check)."""
    for step in plan.steps:
        if step.phase == "install" and step.argv is not None:
            if step.argv[1:2] == ["sync"] and manager in ("uv", "pdm"):
                return step
            if step.argv[1:2] == ["install"] and manager == "poetry":
                return step
    raise AssertionError(f"no frozen install step for {manager}")


# ---------------------------------------------------------------------------
# (a) The recorded backend: name + resolved version, per manager.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("manager", ["uv", "pdm"])
def test_declared_backend_recorded_on_plan(manager):
    """uv / pdm build the root with the project's declared
    PEP 517 backend; the plan records its module + distribution
    name. The version resolves fresh at build time, so the plan
    records None and the executors record the as-built version
    on the provision (see the E2E tests)."""
    plan = _plan(manager)
    assert plan.build_backend == "setuptools.build_meta"
    assert plan.build_backend_name == "setuptools"
    assert plan.build_backend_version is None


def test_poetry_backend_recorded_on_plan_with_pinned_version():
    """Poetry builds the root itself with its bundled
    poetry-core, whose version the committed bootstrap file
    pins by hash — so the plan records name AND version."""
    plan = _plan("poetry")
    assert plan.build_backend == "poetry.core.masonry.api"
    assert plan.build_backend_name == "poetry-core"
    assert plan.build_backend_version == "2.5.0"
    # The recorded version is the bootstrap file's own pin.
    bootstrap = (
        Path(__file__).parent.parent
        / "proofdeploy"
        / "poetry_bootstrap_requirements.txt"
    ).read_text(encoding="utf-8")
    assert "poetry-core==2.5.0" in bootstrap


def test_pep517_default_backend_when_undeclared(tmp_path):
    """No [build-system] in pyproject: PEP 517's default
    backend (setuptools.build_meta) is what builds the root,
    and that is what the plan records."""
    snap = tmp_path / "snap"
    shutil.copytree(FIXTURES / "uv", snap)
    text = (snap / "pyproject.toml").read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    kept, skipping = [], False
    for line in lines:
        if line.startswith("[build-system]"):
            skipping = True
            continue
        if skipping and line.startswith("["):
            skipping = False
        if not skipping:
            kept.append(line)
    (snap / "pyproject.toml").write_text(
        "".join(kept), encoding="utf-8"
    )
    from proofdeploy.provisioning import plan_provisioning

    plan = plan_provisioning(snap, _contract(), lock_cutoff=CUTOFF)
    assert plan.build_backend == "setuptools.build_meta"
    assert plan.build_backend_name == "setuptools"


def test_pip_plans_record_no_build_backend(tmp_path):
    """The pip paths never install the root project, so they
    carry no build-backend record at all: drop the fixture's
    lockfile and the same project plans as a GENERATED lock
    (pip), with every build field unset."""
    snap = tmp_path / "snap"
    shutil.copytree(FIXTURES / "uv", snap)
    (snap / "uv.lock").unlink()
    from proofdeploy.provisioning import plan_provisioning

    plan = plan_provisioning(snap, _contract(), lock_cutoff=CUTOFF)
    assert plan.lock_source == "generated"
    assert plan.build_backend is None
    assert plan.build_backend_name is None
    assert plan.build_backend_version is None
    assert plan.build_cutoff is None
    assert plan.build_limitations == []


# ---------------------------------------------------------------------------
# (b) The cutoff reaches the uv build step.
# ---------------------------------------------------------------------------


def test_uv_build_cutoff_on_sync_step():
    plan = _plan("uv", lock_cutoff=CUTOFF)
    assert plan.build_cutoff == CUTOFF
    assert plan.build_limitations == []
    sync = _sync_step(plan, "uv")
    assert sync.env["UV_EXCLUDE_NEWER"] == CUTOFF
    # The rest of the registered uv shape is untouched.
    assert sync.env["UV_PROJECT_ENVIRONMENT"] == "{venv}"
    assert sync.argv[1:3] == ["sync", "--frozen"]


def test_uv_build_cutoff_from_callable():
    """The orchestrator hands the adapter a lazy cutoff; a
    committed uv plan evaluates it for the build constraint."""
    plan = _plan("uv", lock_cutoff=lambda: CUTOFF)
    assert plan.build_cutoff == CUTOFF
    assert _sync_step(plan, "uv").env["UV_EXCLUDE_NEWER"] == CUTOFF


def test_uv_without_cutoff_records_limitation():
    plan = _plan("uv", lock_cutoff=None)
    assert plan.build_cutoff is None
    assert "UV_EXCLUDE_NEWER" not in _sync_step(plan, "uv").env
    assert len(plan.build_limitations) == 1
    assert plan.build_limitations[0].startswith(
        "build-backend-cutoff-unavailable:"
    )
    assert "no unit cutoff was available" in plan.build_limitations[0]


def test_uv_with_failing_cutoff_source_records_limitation():
    """The committed path's contract: planning never trips over
    the caller's cutoff source. A raising lazy cutoff degrades
    to the typed limitation, not an exception."""

    def _boom():
        raise RuntimeError("git is unavailable")

    plan = _plan("uv", lock_cutoff=_boom)
    assert plan.package_manager == "uv"
    assert plan.build_cutoff is None
    assert "UV_EXCLUDE_NEWER" not in _sync_step(plan, "uv").env
    assert len(plan.build_limitations) == 1
    assert "evaluating the unit cutoff failed (RuntimeError)" in (
        plan.build_limitations[0]
    )


# ---------------------------------------------------------------------------
# (c) The typed limitation for the tool that can't constrain.
# ---------------------------------------------------------------------------


def test_pdm_build_cutoff_is_a_typed_limitation():
    from proofdeploy.provisioning import PDM_BUILD_CUTOFF_LIMITATION

    plan = _plan("pdm", lock_cutoff=CUTOFF)
    assert plan.build_cutoff is None
    assert plan.build_limitations == [PDM_BUILD_CUTOFF_LIMITATION]
    limitation = plan.build_limitations[0]
    assert limitation.startswith("build-backend-cutoff-unavailable:")
    # The reason names the mechanism that makes it a limitation,
    # not a bare "unsupported".
    assert "strategy.exclude-newer" in limitation
    assert "committed lock's own files" in limitation
    # And the pdm steps carry no cutoff channel at all.
    for step in plan.steps:
        assert "EXCLUDE_NEWER" not in json.dumps(step.env)


def test_poetry_needs_no_limitation():
    """Poetry's root build resolves nothing from the index (the
    backend is the bootstrap-pinned poetry-core), so there is
    nothing to constrain and no limitation to record."""
    plan = _plan("poetry", lock_cutoff=CUTOFF)
    assert plan.build_limitations == []
    assert plan.build_cutoff is None


# ---------------------------------------------------------------------------
# The executor read-back: the version that ACTUALLY built the
# project, from the built wheel's Generator.
# ---------------------------------------------------------------------------


def _plan_stub(**kw):
    from proofdeploy.provisioning import InstallPlan

    base = {"adapter": "python", "runtime": "python"}
    base.update(kw)
    return InstallPlan(**base)


def _checkout_with_wheel(tmp_path, generator: str, name="wo81b-lock-app"):
    """A fake checkout: pyproject + a .venv whose site-packages
    hold the project's dist-info with the given WHEEL Generator."""
    checkout = tmp_path / "checkout"
    dist_info = (
        checkout
        / ".venv"
        / "lib"
        / "python3.12"
        / "site-packages"
        / "wo81b_lock_app-0.1.0.dist-info"
    )
    dist_info.mkdir(parents=True)
    (dist_info / "WHEEL").write_text(
        f"Wheel-Version: 1.0\nGenerator: {generator}\n",
        encoding="utf-8",
    )
    (checkout / "pyproject.toml").write_text(
        f'[project]\nname = "{name}"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )
    return checkout


def test_readback_setuptools_generator(tmp_path):
    from proofdeploy.provisioning import build_backend_evidence

    plan = _plan_stub(
        build_backend="setuptools.build_meta",
        build_backend_name="setuptools",
    )
    checkout = _checkout_with_wheel(tmp_path, "setuptools (84.0.0)")
    assert build_backend_evidence(plan, checkout) == {
        "name": "setuptools",
        "version": "84.0.0",
    }


def test_readback_poetry_core_generator(tmp_path):
    from proofdeploy.provisioning import build_backend_evidence

    plan = _plan_stub(
        build_backend="poetry.core.masonry.api",
        build_backend_name="poetry-core",
        build_backend_version="2.5.0",
    )
    checkout = _checkout_with_wheel(tmp_path, "poetry-core 2.5.0")
    assert build_backend_evidence(plan, checkout) == {
        "name": "poetry-core",
        "version": "2.5.0",
    }


def test_readback_falls_back_to_plan_pin(tmp_path):
    """No build artifact (poetry's package-mode = false fixture
    installs no root project): the record still carries the
    plan's pinned backend fact."""
    from proofdeploy.provisioning import build_backend_evidence

    plan = _plan_stub(
        build_backend="poetry.core.masonry.api",
        build_backend_name="poetry-core",
        build_backend_version="2.5.0",
    )
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / "pyproject.toml").write_text(
        '[project]\nname = "wo81b-lock-app"\n', encoding="utf-8"
    )
    assert build_backend_evidence(plan, checkout) == {
        "name": "poetry-core",
        "version": "2.5.0",
    }


def test_readback_none_without_artifact_or_pin(tmp_path):
    from proofdeploy.provisioning import build_backend_evidence

    plan = _plan_stub(
        build_backend="setuptools.build_meta",
        build_backend_name="setuptools",
    )
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    assert build_backend_evidence(plan, checkout) is None


def test_readback_ignores_other_projects(tmp_path):
    """A venv full of OTHER packages' dist-infos is not the
    root project's build record."""
    from proofdeploy.provisioning import build_backend_evidence

    plan = _plan_stub(
        build_backend="setuptools.build_meta",
        build_backend_name="setuptools",
    )
    checkout = _checkout_with_wheel(tmp_path, "setuptools (84.0.0)")
    (checkout / "pyproject.toml").write_text(
        '[project]\nname = "some-other-project"\n', encoding="utf-8"
    )
    assert build_backend_evidence(plan, checkout) is None


def test_readback_none_for_pip_plan(tmp_path):
    from proofdeploy.provisioning import build_backend_evidence

    plan = _plan_stub()  # a pip plan: no build backend recorded
    checkout = _checkout_with_wheel(tmp_path, "setuptools (84.0.0)")
    assert build_backend_evidence(plan, checkout) is None


# ---------------------------------------------------------------------------
# Sandbox seam: the cutoff reaches the uv build step through the
# orchestrator's own rendering, on the captured step descriptors.
# ---------------------------------------------------------------------------


class _CaptureInstall:
    """Fake Sandbox.install_steps: captures the step descriptors,
    reports every step as exit code 0 (the WO-8.1b scaffold)."""

    def __init__(self):
        self.descriptors: list[dict] = []

    @property
    def commands(self) -> list[str]:
        return [d["command"] for d in self.descriptors]

    def __call__(self, checkout, steps, env, proxy_env, timeout=600):
        self.descriptors.extend(steps)
        return [{"phase": s["phase"], "exit_code": 0} for s in steps]


def _fake_caps():
    from proofdeploy.sandbox import SandboxCapabilities

    return SandboxCapabilities(
        podman=True,
        podman_version="podman version 4.9.3",
        isolated_network=False,
        build_network=True,
        userns_remap=True,
        reason="",
    )


def test_sandbox_uv_sync_step_carries_the_run_cutoff(
    monkeypatch, tmp_path
):
    from proofdeploy import orchestrator as orch_mod
    from proofdeploy.orchestrator import MeasureConfig, Orchestrator
    from proofdeploy.provisioning import select_image
    from proofdeploy.sandbox import Sandbox, SandboxError
    from proofdeploy.yml import RepoContract

    snap = tmp_path / "snap"
    shutil.copytree(FIXTURES / "uv", snap)
    cap = _CaptureInstall()

    row = select_image("python", None)
    assert row is not None
    monkeypatch.setattr(
        orch_mod, "_git", lambda *a: "fakesha1234567890abcdef"
    )
    monkeypatch.setattr(
        orch_mod,
        "create_snapshot",
        lambda **kw: SimpleNamespace(dir=snap),
    )
    contract = RepoContract(
        build="", start="python app.py", readiness="/health", env={}
    )
    monkeypatch.setattr(orch_mod, "load_contract", lambda p: contract)
    monkeypatch.setattr(orch_mod, "check_podman", lambda: (True, ""))
    monkeypatch.setattr(
        orch_mod,
        "check_sandbox_runtime",
        lambda *a: (row.version, row.digest, None),
    )
    monkeypatch.setattr(Sandbox, "_capabilities", lambda self: _fake_caps())
    monkeypatch.setattr(Sandbox, "install_steps", cap)

    cfg = MeasureConfig(
        repo_dir=snap,
        output_dir=snap / "out",
        workdir=snap / "work",
        no_skill=True,
    )
    orch = Orchestrator(cfg)
    # A measured run sets the run's one cutoff before provisioning;
    # that is the value the uv build step must carry.
    orch._run_lock_cutoff = CUTOFF
    unit_dir = snap / "unit"
    unit_dir.mkdir(exist_ok=True)

    def _stop_after_install(self, *a, **k):
        raise SandboxError("test stops after install")

    monkeypatch.setattr(Sandbox, "start", _stop_after_install)
    side = orch._provision_side_sandbox("fakerev", 18097, unit_dir, "t")
    sync_desc = next(
        d
        for d in cap.descriptors
        if d["command"]
        == "/checkout/.pd-tools/bin/uv sync --frozen --python python3"
    )
    assert sync_desc["env"]["UV_EXCLUDE_NEWER"] == CUTOFF
    assert sync_desc["env"]["UV_PROJECT_ENVIRONMENT"] == "/checkout/.venv"
    plan = side.provision.install_plan
    assert plan is not None
    assert plan["build_cutoff"] == CUTOFF
    assert plan["build_backend"] == "setuptools.build_meta"


# ---------------------------------------------------------------------------
# Host executor E2E (real tools, via the plan's own bootstrap):
# the side's record carries the backend name + the version that
# ACTUALLY built the project.
# ---------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _provision_fixture(tmp_path, manager: str):
    from proofdeploy.runner import Provisioner
    from proofdeploy.yml import load_contract

    snap = tmp_path / manager
    shutil.copytree(FIXTURES / manager, snap)
    contract = load_contract(snap / "proofdeploy.yml")
    prov = Provisioner(workdir=tmp_path / "work")
    lock_name = PINNED[manager][1]
    expected = hashlib.sha256((snap / lock_name).read_bytes()).hexdigest()
    result = prov.provision(
        snap,
        contract,
        _free_port(),
        expected_lockfile_sha256=expected,
    )
    return prov, snap, result


@pytest.mark.parametrize("manager", ["uv", "pdm"])
def test_host_record_carries_as_built_backend(tmp_path, manager):
    from proofdeploy.runner import ProvisionStatus

    prov, snap, result = _provision_fixture(tmp_path, manager)
    try:
        assert result.status == ProvisionStatus.READY, "\n".join(
            result.logs
        )
        # (a) The recorded backend name + resolved version is
        # present in the side's record — read back from what the
        # tool actually used (the built wheel's Generator), so it
        # is the setuptools the build resolved, never the
        # declared ">=61" requirement string.
        record = result.to_dict()
        backend = record["build_backend"]
        assert backend is not None, "\n".join(result.logs)
        assert backend["name"] == "setuptools"
        assert backend["version"]
        assert backend["version"] != ">=61"
        assert "setuptools" not in backend["version"]
        # The app really runs from the provisioned venv.
        assert result.target_url is not None
        with urllib.request.urlopen(
            result.target_url + "/health", timeout=10
        ) as resp:
            assert json.loads(resp.read()) == {
                "ok": True,
                "six": "1.17.0",
            }
    finally:
        if result.proc is not None:
            result.proc.kill()
            result.proc.wait(timeout=10)
        prov.cleanup_run(result.run_id, [])


def test_host_record_carries_poetry_core_backend(tmp_path):
    from proofdeploy.runner import ProvisionStatus

    prov, snap, result = _provision_fixture(tmp_path, "poetry")
    try:
        assert result.status == ProvisionStatus.READY, "\n".join(
            result.logs
        )
        # The poetry fixture is package-mode = false (no root
        # build), so the record carries the plan's pinned fact:
        # poetry-core at the bootstrap-pinned version.
        record = result.to_dict()
        assert record["build_backend"] == {
            "name": "poetry-core",
            "version": "2.5.0",
        }
        plan = record["install_plan"]
        assert plan is not None
        assert plan["build_backend"] == "poetry.core.masonry.api"
        assert plan["build_backend_version"] == "2.5.0"
    finally:
        if result.proc is not None:
            result.proc.kill()
            result.proc.wait(timeout=10)
        prov.cleanup_run(result.run_id, [])
