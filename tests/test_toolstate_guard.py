"""Plan 1.2(n) (D77 Q6c): the no-in-tree-tool-state guard.

ProofDeploy's own tool state — the .pd-tools tree (the hash-pinned
Python lock managers, the corepack package, the digest-pinned bun)
and the app venv — lives in the per-side home (plan 1.2(k)'s
/pd-home; host: the snapshot's ``<snap>.pd-home`` sibling), NEVER
inside the app checkout it measures. Before 1.2(n) both trees were
rendered into the checkout, where a workspace scanner could
discover them (the 1.2(k) failure class) and a broad glob in the
app's own build could ingest them.

This guard enumerates the WHOLE plan matrix — every adapter,
every manager, both executors' renderings, plus the sandbox
contract-step wrapper — and asserts no plan string and no rendered
string places tool state in the tree:

- no ``{checkout}/.pd-tools`` / ``{checkout}/.venv`` in plan data;
- no ``/checkout/.pd-tools`` / ``/checkout/.venv`` in a sandbox
  rendering, and no ``<snap>/.pd-tools`` / ``<snap>/.venv`` in a
  host rendering;
- ``persisted_dirs`` entries are rooted (D77 Q3): tool state is
  ``pd_home:``-rooted, the app's own node_modules ``checkout:``.

Plant: leave one tenant at the old path (e.g. render the bun
bootstrap's tools dir as ``{checkout}/.pd-tools`` again) and this
guard fails — that direction is exercised in the PR that
introduced it.

The renderings go through the executors' OWN mapping functions
(``sandbox_placeholder_mapping`` / ``host_placeholder_mapping``,
D82): the guard holds no copy of either table, so a plant in an
executor's real mapping — sandbox or host — fails here.
"""
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from proofdeploy.orchestrator import (
    _sandbox_step_command,
    sandbox_placeholder_mapping,
)
from proofdeploy.provisioning import pip_flags, plan_provisioning, render_argv
from proofdeploy.runner import host_placeholder_mapping
from proofdeploy.sandbox import sandbox_home_dir

WO81B = Path(__file__).parent / "fixtures" / "wo81b"

HASHED_REQ = (
    "six==1.17.0 \\\n"
    "    --hash=sha256:ff703d0fc3e08526949441584fca3fc209d20cf45a3f54c0fb84f79a80d0d95b1\n"
)

CUTOFF = "2026-01-02T03:04:05Z"


def _contract(**kw):
    base = {
        "build": "python -m py_compile app.py",
        "migrate": None,
        "seed": None,
        "start": "python app.py",
        "env": {},
    }
    base.update(kw)
    return SimpleNamespace(**base)


def _node_contract(**kw):
    return _contract(build="npm run build", start="node server.js", **kw)


def _write(snap: Path, name: str, text: str) -> None:
    (snap / name).write_text(text, encoding="utf-8")


def _pip_snapshot(tmp_path: Path) -> Path:
    snap = tmp_path / "pip"
    snap.mkdir()
    _write(snap, "requirements.txt", HASHED_REQ)
    return snap


def _generated_snapshot(tmp_path: Path) -> Path:
    snap = tmp_path / "generated"
    snap.mkdir()
    _write(
        snap,
        "pyproject.toml",
        '[project]\nname = "t"\nrequires-python = ">=3.12,<3.13"\n'
        'dependencies = ["six==1.17.0"]\n',
    )
    return snap


def _manager_snapshot(tmp_path: Path, manager: str) -> Path:
    snap = tmp_path / manager
    shutil.copytree(WO81B / manager, snap)
    return snap


def _npm_snapshot(tmp_path: Path) -> Path:
    snap = tmp_path / "npm"
    snap.mkdir()
    _write(snap, "package.json", json.dumps({"name": "t"}))
    _write(snap, "package-lock.json", "{}")
    return snap


def _corepack_snapshot(tmp_path: Path) -> Path:
    snap = tmp_path / "corepack"
    snap.mkdir()
    _write(
        snap,
        "package.json",
        json.dumps({"name": "t", "packageManager": "pnpm@9.1.0"}),
    )
    _write(snap, "pnpm-lock.yaml", "lockfileVersion: '9.0'\n")
    return snap


def _bun_snapshot(tmp_path: Path) -> Path:
    snap = tmp_path / "bun"
    snap.mkdir()
    _write(
        snap,
        "package.json",
        json.dumps({"name": "t", "packageManager": "bun@1.3.14"}),
    )
    _write(snap, "bun.lock", "{}\n")
    return snap


def _workspace_snapshot(tmp_path: Path) -> Path:
    snap = tmp_path / "workspace"
    snap.mkdir()
    _write(
        snap,
        "package.json",
        json.dumps(
            {
                "name": "ws-root",
                "private": True,
                "packageManager": "pnpm@9.15.0",
            }
        ),
    )
    _write(snap, "pnpm-workspace.yaml", "packages:\n  - 'packages/*'\n")
    _write(snap, "pnpm-lock.yaml", "lockfileVersion: '9.0'\n")
    member = snap / "packages" / "app"
    member.mkdir(parents=True)
    _write(member, "package.json", json.dumps({"name": "packages-app"}))
    return snap


def _plans(tmp_path: Path):
    """The plan matrix: (label, snapshot, plan) for every
    adapter/manager combination that produces a plan."""
    cases = []
    snap = _pip_snapshot(tmp_path)
    cases.append(("python-pip", snap, plan_provisioning(snap, _contract())))
    snap = _generated_snapshot(tmp_path)
    cases.append(
        (
            "python-generated",
            snap,
            plan_provisioning(snap, _contract(), lock_cutoff=CUTOFF),
        )
    )
    for manager in ("uv", "poetry", "pdm"):
        snap = _manager_snapshot(tmp_path, manager)
        cases.append(
            (
                f"python-{manager}",
                snap,
                plan_provisioning(snap, _contract(), lock_cutoff=CUTOFF),
            )
        )
    snap = _npm_snapshot(tmp_path)
    cases.append(("node-npm", snap, plan_provisioning(snap, _node_contract())))
    snap = _corepack_snapshot(tmp_path)
    cases.append(
        ("node-corepack-pnpm", snap, plan_provisioning(snap, _node_contract()))
    )
    snap = _bun_snapshot(tmp_path)
    cases.append(("node-bun", snap, plan_provisioning(snap, _node_contract())))
    snap = _workspace_snapshot(tmp_path)
    cases.append(
        (
            "node-pnpm-workspace",
            snap,
            plan_provisioning(
                snap, _node_contract(app_path="packages/app")
            ),
        )
    )
    return cases


def _plan_strings(plan) -> list[str]:
    out: list[str] = list(plan.persisted_dirs)
    for step in plan.steps:
        out.extend(step.argv or [])
        if step.shell:
            out.append(step.shell)
        for key, value in step.env.items():
            out.append(key)
            out.append(value)
    return out


def _rendered_strings(plan, snap: Path) -> list[str]:
    """Every step argv/env rendered for BOTH executors, through
    the executors' own mapping functions (D82 — no copied tables
    here), plus the sandbox wrapper's rendering of every shell
    step."""
    home = sandbox_home_dir(snap)
    # Sandbox executor (orchestrator's own mapping).
    mappings = [sandbox_placeholder_mapping(plan)]
    if plan.runtime == "python":
        # Host executor: the managers' {venv} in the per-side home
        # sibling, and pip's per-run venv (a stand-in path of the
        # executor's workdir/venvs/<run_id> shape — outside the
        # tree by construction).
        mappings.append(
            host_placeholder_mapping("python3", snap, home / ".venv")
        )
        mappings.append(
            host_placeholder_mapping(
                "python3", snap, snap.parent / "venvs" / "run" / ".venv"
            )
        )
    else:
        mappings.append(host_placeholder_mapping("node", snap))
    out: list[str] = []
    for mapping in mappings:
        for step in plan.steps:
            if step.argv is not None:
                out.extend(
                    render_argv(
                        step.argv, mapping, pip_flags(plan, sandbox=True)
                    )
                )
            for value in step.env.values():
                out.append(render_argv([value], mapping, [])[0])
    for step in plan.steps:
        if step.shell:
            out.append(
                _sandbox_step_command(
                    step.shell,
                    plan.runtime,
                    plan.package_manager,
                    corepack_version=plan.corepack_version,
                )
            )
    return out


LABELS = [
    "python-pip",
    "python-generated",
    "python-uv",
    "python-poetry",
    "python-pdm",
    "node-npm",
    "node-corepack-pnpm",
    "node-bun",
    "node-pnpm-workspace",
]


@pytest.mark.parametrize("label", LABELS)
def test_no_in_tree_tool_state_anywhere(tmp_path, label):
    cases = {lbl: (snap, plan) for lbl, snap, plan in _plans(tmp_path)}
    snap, plan = cases[label]
    assert plan.refusal is None, (label, plan.refusal)
    # Plan data: no tenant addressed through the checkout.
    for text in _plan_strings(plan):
        assert "{checkout}/.pd-tools" not in text, (label, text)
        assert "{checkout}/.venv" not in text, (label, text)
        assert "/checkout/.pd-tools" not in text, (label, text)
        assert "/checkout/.venv" not in text, (label, text)
    # persisted_dirs: every entry rooted (D77 Q3); tool state and
    # the app venv are pd_home-rooted, never bare or checkout-rooted.
    for entry in plan.persisted_dirs:
        root, sep, rel = entry.partition(":")
        assert sep and root in ("checkout", "pd_home"), (label, entry)
        if ".pd-tools" in rel or rel == ".venv":
            assert root == "pd_home", (label, entry)
    # Renderings: no executor places tool state in the tree, and no
    # placeholder survives rendering.
    for text in _rendered_strings(plan, snap):
        assert "/checkout/.pd-tools" not in text, (label, text)
        assert "/checkout/.venv" not in text, (label, text)
        assert f"{snap}/.pd-tools" not in text, (label, text)
        assert f"{snap}/.venv" not in text, (label, text)
        assert "{pd_tools}" not in text, (label, text)
        assert "{venv}" not in text, (label, text)
        assert "{checkout}" not in text, (label, text)
