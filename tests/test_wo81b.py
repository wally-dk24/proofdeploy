"""WO-8.1b: committed uv.lock / poetry.lock / pdm.lock install
frozen in the sandbox (and on the host executor).

The defect (pre-existing on main): REG-A item 1 calls a committed
uv.lock / poetry.lock / pdm.lock a COMPLETE lock that installs
frozen, and the host planner planned it — as one bare manager argv
(``uv sync --frozen`` off PATH, no bootstrap, no venv wiring) —
while the sandbox executor REFUSED every Python plan whose
manager was not pip ("not yet supported in the sandbox"). A repo
committing one of these locks (Flask is the standing example)
was BUILD_FAILED in every measured run.

What this work order builds:

- The plan carries the whole frozen install (one plan, both
  executors): the checkout venv created from the selected
  interpreter; the manager bootstrapped into /checkout/.pd-tools
  BY HASH from a committed bootstrap requirements file at ONE
  pinned version (uv 0.13.0 — the REG-A resolver pin; poetry
  2.5.1, PyPI 2026-09-20; pdm 2.29.2, PyPI 2026-09-17 — each the
  newest release at least 14 days old when pinned, 2026-10-12),
  the file's sha256 recorded on the plan
  (manager_bootstrap_sha256) and the version in
  package_manager_version; the frozen install into
  /checkout/.venv (``uv sync --frozen`` with
  UV_PROJECT_ENVIRONMENT + --python, ``poetry install --sync``
  into the in-project venv, ``pdm sync``); and the tool's own
  lock-freshness check where the install command does not refuse
  a stale lock itself (verified against the pinned tools:
  ``uv sync --frozen`` and ``pdm sync`` both exit 0 on a stale
  lock, so the plans run ``uv lock --check`` / ``pdm lock
  --check`` first; ``poetry install --sync`` exits 1 itself, so
  poetry gets no separate check).
- The sandbox manager gate grows to {pip, uv, poetry, pdm}.
- The host executor creates the checkout venv for these managers
  and puts it first on PATH for install/build/migrate/seed/start
  (previously only pip got a venv on PATH), renders step env
  through the plan mapping, and names a failed step's first error
  line in the reason — the sandbox's Addendum 33 surfacing — so a
  stale lock is BUILD_FAILED carrying the tool's line on both
  executors.

Fail-before: on main the plan for each lock is the single bare
argv (no bootstrap, no version, no check step), the sandbox
provision is BUILD_FAILED "not yet supported in the sandbox", and
the host E2E provisions below fail at the missing system manager.
The real-tool E2E tests need network (the plan's bootstrap
installs from PyPI); like WO-8.1's real-install tests they fail
rather than skip offline.
"""
import hashlib
import json
import shutil
import socket
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

from proofdeploy import provisioning
from proofdeploy.provisioning import plan_provisioning

FIXTURES = Path(__file__).parent / "fixtures" / "wo81b"
PKG_DIR = Path(provisioning.__file__).parent

# The pinned values, as literals: the tests pin the VALUES the
# work order registers, not whatever the code's constants say.
PINNED = {
    "uv": ("0.13.0", "uv_bootstrap_requirements.txt",
           ".pd-tools/uv-requirements.txt", "uv.lock"),
    "poetry": ("2.5.1", "poetry_bootstrap_requirements.txt",
               ".pd-tools/poetry-requirements.txt", "poetry.lock"),
    "pdm": ("2.29.2", "pdm_bootstrap_requirements.txt",
            ".pd-tools/pdm-requirements.txt", "pdm.lock"),
}

FROZEN_ARGV = {
    "uv": [
        "{checkout}/.pd-tools/bin/uv",
        "sync",
        "--frozen",
        "--python",
        "{runtime_bin}",
    ],
    "poetry": ["{checkout}/.pd-tools/bin/poetry", "install", "--sync"],
    "pdm": ["{checkout}/.pd-tools/bin/pdm", "sync"],
}

FROZEN_ENV = {
    "uv": {"UV_PROJECT_ENVIRONMENT": "{venv}"},
    "poetry": {"POETRY_VIRTUALENVS_IN_PROJECT": "true"},
    "pdm": {},
}

# Managers whose install command does not itself refuse a stale
# lock (see the module docstring) get the tool's own freshness
# check as the step right before the frozen install.
CHECK_ARGV = {
    "uv": ["{checkout}/.pd-tools/bin/uv", "lock", "--check"],
    "pdm": ["{checkout}/.pd-tools/bin/pdm", "lock", "--check"],
}


def _contract():
    return SimpleNamespace(
        build="python -m py_compile app.py",
        migrate=None,
        seed=None,
        start="python app.py",
        env={},
    )


def _install_steps(plan):
    return [
        s for s in plan.steps if s.phase == "install" and s.argv is not None
    ]


# ---------------------------------------------------------------------------
# The pins: exact versions in the code constants AND in the
# committed bootstrap files (a version edited to a range in either
# place is the unpinned-manager plant and must fail here).
# ---------------------------------------------------------------------------


def test_manager_versions_pinned_in_code():
    assert provisioning.UV_VERSION == "0.13.0"
    assert provisioning.POETRY_VERSION == "2.5.1"
    assert provisioning.PDM_VERSION == "2.29.2"


@pytest.mark.parametrize("manager", ["uv", "poetry", "pdm"])
def test_bootstrap_file_pins_exact_version_with_hashes(manager):
    version, file_name, _path, _lock = PINNED[manager]
    text = (PKG_DIR / file_name).read_text(encoding="utf-8")
    # The manager itself is pinned by exact version...
    assert f"{manager}=={version} \\" in text
    # ...and every requirement line in the file is an exact pin
    # whose entry carries at least one --hash (the file installs
    # with --require-hashes --no-deps --only-binary :all:).
    entries = []
    buf = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        buf = f"{buf} {line}".strip()
        if not line.endswith("\\"):
            entries.append(buf)
            buf = ""
    assert entries, file_name
    for entry in entries:
        assert "==" in entry.split()[0], entry
        assert "--hash=sha256:" in entry, entry


# ---------------------------------------------------------------------------
# Plan shape, per manager (from the REAL fixture checkouts: real
# pyproject.toml + a lock generated by the pinned tool itself).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("manager", ["uv", "poetry", "pdm"])
def test_committed_lock_plan_shape(manager, tmp_path):
    version, file_name, checkout_path, lockfile = PINNED[manager]
    # Plan from a COPY so planning can never write into the fixture.
    snap = tmp_path / "snap"
    shutil.copytree(FIXTURES / manager, snap)
    plan = plan_provisioning(
        snap, _contract(), lock_cutoff="2026-01-02T03:04:05Z"
    )
    assert plan.refusal is None
    assert plan.lockfile_name == lockfile
    # REG-A: a committed lock is never regenerated, even with a
    # cutoff on offer.
    assert plan.lock_source == "committed"
    assert plan.lock_cutoff is None
    assert plan.package_manager == manager
    assert plan.package_manager_version == version
    # The bootstrap file's sha256 is recorded on the plan, and it
    # is the sha256 of the committed file in the package.
    committed = (PKG_DIR / file_name).read_bytes()
    assert plan.manager_bootstrap_sha256 == hashlib.sha256(
        committed
    ).hexdigest()
    assert plan.persisted_dirs == [".venv", ".pd-tools"]

    steps = plan.steps
    # 1. The checkout venv, created from the selected interpreter.
    assert steps[0].phase == "venv"
    assert steps[0].argv == ["{runtime_bin}", "-m", "venv", "{venv}"]
    installs = _install_steps(plan)
    # 2. The manager bootstrap: tools venv, the committed file
    # materialized verbatim, the hash-checked install.
    assert installs[0].argv == [
        "{runtime_bin}", "-m", "venv", "{checkout}/.pd-tools",
    ]
    write_step = installs[1]
    assert write_step.argv[:3] == [
        "{runtime_bin}", "-c", provisioning._WRITE_FILE_CODE,
    ]
    assert write_step.argv[3] == f"{{checkout}}/{checkout_path}"
    assert write_step.argv[4] == committed.decode("utf-8")
    assert installs[2].argv == [
        "{checkout}/.pd-tools/bin/pip",
        "install",
        "--require-hashes",
        "--no-deps",
        "--only-binary",
        ":all:",
        "-r",
        f"{{checkout}}/{checkout_path}",
    ]
    # 3. The freshness check (where the tool needs one) directly
    # before the frozen install; the frozen install is LAST.
    frozen = installs[-1]
    assert frozen.argv == FROZEN_ARGV[manager], frozen.argv
    assert frozen.env == FROZEN_ENV[manager], frozen.env
    if manager in CHECK_ARGV:
        assert installs[-2].argv == CHECK_ARGV[manager]
    else:
        assert all(
            "--check" not in (s.argv or []) for s in installs
        ), installs
    # 4. The sandbox venv sanity check, as on the pip plans.
    verify = [s for s in steps if s.phase == "verify"]
    assert len(verify) == 1
    assert verify[0].executor == "sandbox"
    assert verify[0].argv is not None
    assert verify[0].argv[0] == "/checkout/.venv/bin/python"
    # 5. No generation machinery anywhere on a committed plan.
    assert all("compile" not in (s.argv or []) for s in steps)


# ---------------------------------------------------------------------------
# The sandbox seam (podman-free): the gate no longer refuses, and
# the rendered steps are the plan's — bootstrap, check, frozen
# install — with the step env rendered (/checkout/.venv, not the
# {venv} token).
# ---------------------------------------------------------------------------


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


class _CaptureInstall:
    """Fake Sandbox.install_steps: captures the step descriptors,
    reports every step as exit code 0."""

    def __init__(self):
        self.descriptors: list[dict] = []

    @property
    def commands(self) -> list[str]:
        return [d["command"] for d in self.descriptors]

    def __call__(self, checkout, steps, env, proxy_env, timeout=600):
        self.descriptors.extend(steps)
        return [{"phase": s["phase"], "exit_code": 0} for s in steps]


def _stub_sandbox_side(monkeypatch, snap: Path, install):
    """Drive Orchestrator._provision_side_sandbox podman-free (the
    Addendum 18 / WO-7 PR 1 scaffolding)."""
    from proofdeploy import orchestrator as orch_mod
    from proofdeploy.orchestrator import MeasureConfig, Orchestrator
    from proofdeploy.provisioning import select_image
    from proofdeploy.sandbox import Sandbox
    from proofdeploy.yml import RepoContract

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
    monkeypatch.setattr(Sandbox, "install_steps", install)

    cfg = MeasureConfig(
        repo_dir=snap,
        output_dir=snap / "out",
        workdir=snap / "work",
        no_skill=True,
    )
    orch = Orchestrator(cfg)
    unit_dir = snap / "unit"
    unit_dir.mkdir(exist_ok=True)
    return orch


RENDERED_FROZEN = {
    "uv": "/checkout/.pd-tools/bin/uv sync --frozen --python python3",
    "poetry": "/checkout/.pd-tools/bin/poetry install --sync",
    "pdm": "/checkout/.pd-tools/bin/pdm sync",
}


@pytest.mark.parametrize("manager", ["uv", "poetry", "pdm"])
def test_sandbox_runs_manager_plan(monkeypatch, tmp_path, manager):
    from proofdeploy.runner import ProvisionStatus
    from proofdeploy.sandbox import Sandbox, SandboxError

    snap = tmp_path / "snap"
    shutil.copytree(FIXTURES / manager, snap)
    cap = _CaptureInstall()
    orch = _stub_sandbox_side(monkeypatch, snap, cap)

    def _stop_after_install(self, *a, **k):
        raise SandboxError("test stops after install")

    monkeypatch.setattr(Sandbox, "start", _stop_after_install)
    side = orch._provision_side_sandbox(
        "fakerev", 18097, snap / "unit", "t"
    )
    logs = "\n".join(side.provision.logs)
    # Not refused: the install ran and the run reached app start.
    assert side.provision.reason != "not yet supported in the sandbox", logs
    assert side.provision.status == ProvisionStatus.START_FAILED, logs
    assert RENDERED_FROZEN[manager] in cap.commands, cap.commands
    # The bootstrap install ran, hash-checked, before the install.
    boot = [
        c for c in cap.commands
        if "/bin/pip install" in c and "--require-hashes" in c
    ]
    assert len(boot) == 1, cap.commands
    assert cap.commands.index(boot[0]) < cap.commands.index(
        RENDERED_FROZEN[manager]
    )
    if manager in CHECK_ARGV:
        check = RENDERED_FROZEN[manager].split(" sync")[0] + " lock --check"
        assert check in cap.commands, cap.commands
    # The uv step env was RENDERED: /checkout/.venv, not the token.
    if manager == "uv":
        sync_desc = next(
            d for d in cap.descriptors
            if d["command"] == RENDERED_FROZEN["uv"]
        )
        assert sync_desc["env"]["UV_PROJECT_ENVIRONMENT"] == (
            "/checkout/.venv"
        )
    # The plan on the provision records the manager facts.
    plan = side.provision.install_plan
    assert plan is not None
    assert plan["package_manager"] == manager
    assert plan["package_manager_version"] == PINNED[manager][0]
    assert plan["manager_bootstrap_sha256"]


def test_first_error_line_names_uv_lock_check_error():
    """The uv lock check's failure output ends with a hint line;
    the reason must name the error line, not the hint (the
    lowercase ``error:`` marker class)."""
    from proofdeploy.runner import _first_error_line

    out = (
        "Resolved 2 packages in 725ms\n"
        "error: The lockfile at `uv.lock` needs to be updated, "
        "but `--check` was provided.\n"
        "\n"
        "hint: To update the lockfile, run `uv lock`.\n"
    )
    assert _first_error_line([out]) == (
        "error: The lockfile at `uv.lock` needs to be updated, "
        "but `--check` was provided."
    )


# ---------------------------------------------------------------------------
# Host executor E2E (real tools, via the plan's own bootstrap):
# each fixture provisions READY on the host, the app answers from
# the checkout venv, and the mismatch fixture is BUILD_FAILED with
# the tool's first error line in the reason.
# ---------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _provision_fixture(tmp_path, variant: str):
    from proofdeploy.runner import Provisioner
    from proofdeploy.yml import load_contract

    snap = tmp_path / variant
    shutil.copytree(FIXTURES / variant, snap)
    contract = load_contract(snap / "proofdeploy.yml")
    prov = Provisioner(workdir=tmp_path / "work")
    # Measured-run semantics: the committed lock's hash is pinned.
    lock_name = PINNED[variant.removesuffix("-mismatch")][3]
    expected = hashlib.sha256((snap / lock_name).read_bytes()).hexdigest()
    result = prov.provision(
        snap,
        contract,
        _free_port(),
        expected_lockfile_sha256=expected,
    )
    return prov, snap, result


@pytest.mark.parametrize("manager", ["uv", "poetry", "pdm"])
def test_host_provisions_committed_lock_ready(tmp_path, manager):
    from proofdeploy.runner import ProvisionStatus

    prov, snap, result = _provision_fixture(tmp_path, manager)
    try:
        assert result.status == ProvisionStatus.READY, "\n".join(
            result.logs
        )
        # The manager was bootstrapped into the checkout, pinned.
        assert (snap / ".pd-tools" / "bin" / manager).exists()
        # The app runs from the checkout venv and its six is the
        # locked 1.17.0 (only installable from that venv).
        assert result.target_url is not None
        with urllib.request.urlopen(
            result.target_url + "/health", timeout=10
        ) as resp:
            body = json.loads(resp.read())
        assert body == {"ok": True, "six": "1.17.0"}
        assert (snap / ".venv" / "bin" / "python").exists()
        assert result.package_manager == manager
    finally:
        if result.proc is not None:
            result.proc.kill()
            result.proc.wait(timeout=10)
        prov.cleanup_run(result.run_id, [])


def test_host_mismatch_lock_is_build_failed_with_tool_line(tmp_path):
    from proofdeploy.runner import ProvisionStatus

    prov, snap, result = _provision_fixture(tmp_path, "uv-mismatch")
    try:
        assert result.status == ProvisionStatus.BUILD_FAILED, "\n".join(
            result.logs
        )
        # uv's own first error line, carried in the reason.
        assert result.reason is not None
        assert "error: The lockfile at `uv.lock` needs to be updated" in (
            result.reason
        ), result.reason
    finally:
        if result.proc is not None:
            result.proc.kill()
            result.proc.wait(timeout=10)
        prov.cleanup_run(result.run_id, [])
