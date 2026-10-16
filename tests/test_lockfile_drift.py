"""Plan 1.2(j): a committed lockfile out of sync with its
manifest is a typed NOT-SUPPORTED class (D33, Master's option A
on the reviewer's O9; the D18 mechanism, one class over).

What this work order builds:

- ``runner._lockfile_drift_reason`` classifies a failed install
  step's own output as lockfile drift ONLY from the package
  manager's own frozen-install refusal, and renders the typed
  reason ``not supported: lockfile out of sync with its manifest
  (<manager>): <the manager's first refusal line, verbatim>``.
  ProofDeploy never re-implements version matching.
- Both executors use it: the host Provisioner at its two
  install-phase bail sites, the sandbox Orchestrator when the
  failed step is one of the plan's install steps (contract
  build/migrate/seed steps are never classified).
- The readiness harness maps the reason onto the row verbatim
  and never retries the side: ``_rerun_targets`` excludes rows
  whose typed reason is a not-supported class.

Evidence: every pattern was verified against a REAL run of the
pinned manager on a drifted project (one dependency bumped in
the manifest, lockfile unchanged) on 2026-10-15 — npm 10.9.4
(`npm ci`), pnpm 9.15.0 (`pnpm install --frozen-lockfile`),
yarn classic 1.22.22 (`yarn install --frozen-lockfile`), yarn
berry 4.18.1 (`yarn install --immutable`), bun 1.4.3 (the plan's
digest pin, sha256-verified against BUN_RELEASE_SHA256;
`bun install --frozen-lockfile`), uv 0.13.0 / pdm 2.29.2 /
poetry 2.5.1 (the bootstrap pins, installed BY HASH from the
committed bootstrap requirements files; `uv lock --check`,
`pdm lock --check`, `poetry install --sync`). The raw outputs
are committed under tests/fixtures/drift/; they are verbatim
for every line of the refusal itself — surrounding lines that
name machine-local paths (npm's debug-log path and stack
frames, poetry's venv-creation path) were trimmed at commit
time. The negative fixtures are real too: an npm ECONNREFUSED
registry failure and an npm ERESOLVE peer failure, neither of
which may ever be typed as drift.

Fail-before: on main there is no classifier — the same runs
are BUILD_FAILED with the generic "install failed: ..." /
"sandbox install failed: ..." reasons, and the readiness
harness re-runs the side on attempt 2.

The real-install E2E tests need network (registry access for
npm; the corepack bootstrap + pnpm download for pnpm); like
WO-8.1b's real-tool tests they fail rather than skip offline.
"""
import ast
import hashlib
import json
import re
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

import readiness.provision as rp
from proofdeploy.runner import (
    Provisioner,
    ProvisionStatus,
    _lockfile_drift_reason,
)

FIXTURES = Path(__file__).parent / "fixtures" / "drift"

# (capture file, manager, the refusal line the typed reason must
# embed verbatim — the first line of the manager's own output
# that IS the drift refusal).
DRIFT_CASES = [
    (
        "npm.txt",
        "npm",
        "npm error `npm ci` can only install packages when your "
        "package.json and package-lock.json or npm-shrinkwrap.json "
        "are in sync. Please update your lock file with "
        "`npm install` before continuing.",
    ),
    (
        "pnpm.txt",
        "pnpm",
        "ERR_PNPM_OUTDATED_LOCKFILE\u2009 Cannot install with "
        '"frozen-lockfile" because pnpm-lock.yaml is not up to '
        "date with <ROOT>/package.json",
    ),
    (
        "yarn-classic.txt",
        "yarn",
        "error Your lockfile needs to be updated, but yarn was "
        "run with `--frozen-lockfile`.",
    ),
    (
        "yarn-berry.txt",
        "yarn",
        "\u27a4 YN0028: \u2502 The lockfile would have been "
        "modified by this install, which is explicitly forbidden.",
    ),
    (
        "bun.txt",
        "bun",
        "error: lockfile had changes, but lockfile is frozen",
    ),
    (
        "uv.txt",
        "uv",
        "error: The lockfile at `uv.lock` needs to be updated, "
        "but `--check` was provided.",
    ),
    (
        "pdm.txt",
        "pdm",
        "WARNING: Lockfile doesn't satisfy the project "
        "requirements, packages may be outdated",
    ),
    (
        "poetry.txt",
        "poetry",
        "pyproject.toml changed significantly since poetry.lock "
        "was last generated. Run `poetry lock` to fix the "
        "lock file.",
    ),
]


def _capture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _typed(manager: str, line: str) -> str:
    return (
        "not supported: lockfile out of sync with its manifest "
        f"({manager}): {line}"
    )


# ---------------------------------------------------------------------------
# The classifier, over the captured real outputs.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fixture,manager,line",
    DRIFT_CASES,
    ids=[c[0] for c in DRIFT_CASES],
)
def test_classifier_types_each_managers_real_refusal(
    fixture: str, manager: str, line: str
) -> None:
    reason = _lockfile_drift_reason(manager, [_capture(fixture)])
    assert reason == _typed(manager, line)


def test_classifier_ignores_other_managers_output() -> None:
    # A manager's refusal only types under its own name: the
    # scan is keyed by the plan's package manager.
    assert _lockfile_drift_reason("npm", [_capture("pnpm.txt")]) is None
    assert _lockfile_drift_reason("pnpm", [_capture("npm.txt")]) is None
    assert _lockfile_drift_reason("yarn", [_capture("bun.txt")]) is None


def test_classifier_unknown_manager_and_no_output() -> None:
    assert _lockfile_drift_reason("pip", [_capture("uv.txt")]) is None
    assert _lockfile_drift_reason(None, [_capture("npm.txt")]) is None
    assert _lockfile_drift_reason("npm", []) is None
    assert _lockfile_drift_reason("npm", ["", "  "]) is None


def test_classifier_skips_podman_command_echo() -> None:
    # Addendum 38's rule, applied to the classifier too: refusal
    # text inside the echoed command line is not evidence.
    echoed = (
        "$ podman run --rm img bash -c 'npm ci # npm error "
        "lock file's x does not satisfy y'"
    )
    assert _lockfile_drift_reason("npm", [echoed]) is None


# ---------------------------------------------------------------------------
# No false typing: network / peer / generic failures stay
# BUILD_FAILED exactly as today.
# ---------------------------------------------------------------------------


def test_classifier_does_not_type_network_failure() -> None:
    assert _lockfile_drift_reason("npm", [_capture("npm-network.txt")]) is None


def test_classifier_does_not_type_peer_resolution_failure() -> None:
    assert _lockfile_drift_reason("npm", [_capture("npm-eresolve.txt")]) is None


def test_classifier_does_not_type_generic_error_lines() -> None:
    blob = "npm error code ELIFECYCLE\nnpm error errno 1\nerror: build failed"
    assert _lockfile_drift_reason("npm", [blob]) is None
    assert _lockfile_drift_reason("uv", ["error: network timeout"]) is None


# ---------------------------------------------------------------------------
# Host executor: REAL installs on small drifted goldens.
# ---------------------------------------------------------------------------


def _provision_fixture(tmp_path: Path, fixture: str, lockfile: str):
    src = FIXTURES / fixture
    snap = tmp_path / "snap"
    shutil.copytree(src, snap)
    contract = SimpleNamespace(
        build="node --check app.js",
        migrate=None,
        seed=None,
        start="node app.js",
        env={},
    )
    lock_sha = hashlib.sha256((snap / lockfile).read_bytes()).hexdigest()
    return Provisioner(workdir=tmp_path / "work").provision(
        snap, contract, 18131, expected_lockfile_sha256=lock_sha
    )


def test_host_npm_drifted_golden_is_typed_not_supported(tmp_path: Path) -> None:
    result = _provision_fixture(tmp_path, "npm-app", "package-lock.json")
    assert result.status == ProvisionStatus.BUILD_FAILED
    assert result.reason == _typed("npm", DRIFT_CASES[0][2])
    # The install really ran (and refused): npm's own EUSAGE
    # output is in the provision log, not a plan-level refusal.
    assert any("npm error code EUSAGE" in line for line in result.logs)


def test_host_pnpm_drifted_golden_is_typed_not_supported(
    tmp_path: Path,
) -> None:
    result = _provision_fixture(tmp_path, "pnpm-app", "pnpm-lock.yaml")
    assert result.status == ProvisionStatus.BUILD_FAILED
    assert result.reason is not None
    assert result.reason.startswith(
        "not supported: lockfile out of sync with its manifest (pnpm): "
    )
    assert "ERR_PNPM_OUTDATED_LOCKFILE" in result.reason
    assert any("ERR_PNPM_OUTDATED_LOCKFILE" in line for line in result.logs)


def test_host_network_failure_stays_generic_build_failed(
    tmp_path: Path,
) -> None:
    """The provision-level negative: an install step failing
    with a registry/network error keeps today's generic reason —
    it is retried where the harness retries, never typed."""
    recorded: list[str] = []
    prov = Provisioner(workdir=tmp_path / "work")

    def fake_exec(cmd, cwd, timeout, logs, env):
        recorded.append(" ".join(cmd))
        if cmd[:2] == ["npm", "ci"]:
            logs.extend(_capture("npm-network.txt").splitlines())
            return False, "command failed with code 1: npm ci"
        logs.append(f"$ {' '.join(cmd)} (mocked)")
        return True, None

    prov._exec = fake_exec  # type: ignore[method-assign]
    snap = tmp_path / "snap"
    snap.mkdir()
    (snap / "package.json").write_text('{"name": "t"}\n', encoding="utf-8")
    (snap / "package-lock.json").write_text("{}\n", encoding="utf-8")
    contract = SimpleNamespace(
        build=None,
        migrate=None,
        seed=None,
        start="node server.js",
        env={},
    )
    result = prov.provision(
        snap,
        contract,
        18132,
        expected_lockfile_sha256=hashlib.sha256(b"{}\n").hexdigest(),
    )
    assert recorded[0].startswith("npm ci")
    assert result.status == ProvisionStatus.BUILD_FAILED
    assert result.reason == "install failed: npm error code ECONNREFUSED"
    assert not (result.reason or "").startswith("not supported:")


# ---------------------------------------------------------------------------
# Sandbox executor: classification is install-phase only.
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


def _stub_sandbox(monkeypatch, snap: Path, contract, install_steps):
    from proofdeploy import orchestrator as orch_mod
    from proofdeploy.orchestrator import MeasureConfig, Orchestrator
    from proofdeploy.provisioning import select_image
    from proofdeploy.sandbox import Sandbox

    monkeypatch.setattr(orch_mod, "_git", lambda *a: "fakesha1234567890abcdef")
    monkeypatch.setattr(
        orch_mod,
        "create_snapshot",
        lambda **kw: SimpleNamespace(dir=snap),
    )
    monkeypatch.setattr(orch_mod, "load_contract", lambda p: contract)
    monkeypatch.setattr(orch_mod, "check_podman", lambda: (True, ""))

    def _runtime(*a):
        row = select_image(a[1] if len(a) > 1 else "python", None)
        assert row is not None
        return row.version, row.digest, None

    monkeypatch.setattr(orch_mod, "check_sandbox_runtime", _runtime)
    monkeypatch.setattr(Sandbox, "_capabilities", lambda self: _fake_caps())
    monkeypatch.setattr(Sandbox, "install_steps", install_steps)

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


def _npm_snapshot(tmp_path: Path):
    from proofdeploy.yml import RepoContract

    snap = tmp_path / "snap"
    snap.mkdir()
    (snap / "package.json").write_text(
        json.dumps({"name": "t"}) + "\n", encoding="utf-8"
    )
    (snap / "package-lock.json").write_text("{}\n", encoding="utf-8")
    (snap / "server.js").write_text("// app\n", encoding="utf-8")
    contract = RepoContract(
        build="node --check server.js",
        start="node server.js",
        readiness="/health",
        env={},
    )
    return snap, contract


def _failing_install(output: str, results: list[dict]):
    def fake(self, checkout, steps, env, proxy_env, timeout=600):
        self.log.append(output)
        self.failed_step_output = output
        return results

    return fake


def test_sandbox_install_drift_is_typed_not_supported(
    monkeypatch, tmp_path: Path
) -> None:
    snap, contract = _npm_snapshot(tmp_path)
    fake = _failing_install(
        _capture("npm.txt"), [{"phase": "install", "exit_code": 1}]
    )
    orch = _stub_sandbox(monkeypatch, snap, contract, fake)
    side = orch._provision_side_sandbox("fakerev", 18096, snap / "unit", "t")
    assert side.provision.status == ProvisionStatus.BUILD_FAILED
    assert side.provision.reason == _typed("npm", DRIFT_CASES[0][2])


def test_sandbox_contract_step_failure_is_not_classified(
    monkeypatch, tmp_path: Path
) -> None:
    """A contract build step whose output happens to carry the
    refusal text is the app's own failure: generic BUILD_FAILED,
    never the typed class (classification is install-phase
    only)."""
    snap, contract = _npm_snapshot(tmp_path)
    fake = _failing_install(
        _capture("npm.txt"),
        [
            {"phase": "install", "exit_code": 0},
            {"phase": "build", "exit_code": 1},
        ],
    )
    orch = _stub_sandbox(monkeypatch, snap, contract, fake)
    side = orch._provision_side_sandbox("fakerev", 18096, snap / "unit", "t")
    assert side.provision.status == ProvisionStatus.BUILD_FAILED
    assert side.provision.reason == (
        "sandbox install failed: npm error code EUSAGE"
    )


def test_sandbox_network_failure_is_not_classified(
    monkeypatch, tmp_path: Path
) -> None:
    snap, contract = _npm_snapshot(tmp_path)
    fake = _failing_install(
        _capture("npm-network.txt"), [{"phase": "install", "exit_code": 1}]
    )
    orch = _stub_sandbox(monkeypatch, snap, contract, fake)
    side = orch._provision_side_sandbox("fakerev", 18096, snap / "unit", "t")
    assert side.provision.status == ProvisionStatus.BUILD_FAILED
    assert side.provision.reason == (
        "sandbox install failed: npm error code ECONNREFUSED"
    )


# ---------------------------------------------------------------------------
# Readiness: the row carries the typed reason and the side is
# never retried.
# ---------------------------------------------------------------------------

_UNIT = {
    "unit_id": "T-drift",
    "repo": "example/drift",
    "class": "train-bug",
    "sides": [{"role": "fix", "sha": "b" * 40}],
}


def _fake_side(reason: str, status: ProvisionStatus):
    from proofdeploy.runner import ProvisionResult

    prov = ProvisionResult(
        status=status,
        reason=reason,
        runtime="node",
        runtime_version="22.11.0",
        package_manager="npm",
        install_plan={"package_manager": "npm"},
        install_plan_sha256="ab" * 32,
        logs=["install plan: ...", reason],
    )
    return SimpleNamespace(
        sha="h" * 40,
        provision=prov,
        sandbox_evidence={"app_network_mode": "none+socket"},
        contract=None,
    )


def test_readiness_row_carries_typed_reason_and_is_never_retried(
    tmp_path: Path,
) -> None:
    drift_reason = _typed("npm", DRIFT_CASES[0][2])
    side = _fake_side(drift_reason, ProvisionStatus.BUILD_FAILED)
    row = rp.row_from_provision(
        _UNIT, _UNIT["sides"][0], "h" * 40, 1, "isolated", side
    )
    assert row["status"] == "BUILD_FAILED"
    assert row["typed_reason"] == drift_reason

    generic_unit = {**_UNIT, "unit_id": "T-generic"}
    generic = rp.row_from_provision(
        generic_unit,
        _UNIT["sides"][0],
        "h" * 40,
        1,
        "isolated",
        _fake_side("install failed: npm error code ECONNREFUSED",
                   ProvisionStatus.BUILD_FAILED),
    )
    prior = tmp_path / "rows-attempt-1.json"
    prior.write_text(
        json.dumps({"rows": [row, generic]}), encoding="utf-8"
    )
    # The drift side is terminal (never a rerun target); the
    # generic failure still is.
    assert rp._rerun_targets(prior) == [("T-generic", "fix")]
    assert rp._is_not_supported_row(row)
    assert not rp._is_not_supported_row(generic)


# ---------------------------------------------------------------------------
# Plan 1.2(j) follow-up: D18's own refusal strings carry the
# registered ``not supported: `` prefix (D18's registered reason is
# literally "not supported: no frozen lockfile"), so H5/J1 judging
# and the harness's non-retry rule treat a no-lockfile side as the
# not-supported class it is — before this follow-up the emitted
# strings ("no frozen lockfile found ...") carried no prefix and
# the side was re-provisioned on attempt 2 like a transient
# failure.
# ---------------------------------------------------------------------------


def test_d18_no_lockfile_refusal_is_prefixed_and_never_retried(
    tmp_path: Path,
) -> None:
    from proofdeploy.provisioning import NodeAdapter

    snap = tmp_path / "snap"
    snap.mkdir()
    (snap / "package.json").write_text(
        '{"name": "t"}\n', encoding="utf-8"
    )
    contract = SimpleNamespace(
        build=None, migrate=None, seed=None, start="node .", env={}
    )
    plan = NodeAdapter().plan(snap, contract)
    assert plan.refusal == (
        "not supported: no frozen lockfile found in snapshot "
        "for runtime 'node'"
    )

    side = _fake_side(plan.refusal, ProvisionStatus.BUILD_FAILED)
    row = rp.row_from_provision(
        _UNIT, _UNIT["sides"][0], "h" * 40, 1, "isolated", side
    )
    assert row["status"] == "BUILD_FAILED"
    assert row["typed_reason"] == plan.refusal
    assert rp._is_not_supported_row(row)

    generic_unit = {**_UNIT, "unit_id": "T-generic"}
    generic = rp.row_from_provision(
        generic_unit,
        _UNIT["sides"][0],
        "h" * 40,
        1,
        "isolated",
        _fake_side(
            "install failed: npm error code ECONNREFUSED",
            ProvisionStatus.BUILD_FAILED,
        ),
    )
    prior = tmp_path / "rows-attempt-1.json"
    prior.write_text(
        json.dumps({"rows": [row, generic]}), encoding="utf-8"
    )
    # The D18 side is terminal (never a rerun target); the generic
    # failure still is.
    assert rp._rerun_targets(prior) == [("T-generic", "fix")]
    assert not rp._is_not_supported_row(generic)


# ------------------------------------------------------------------
# Part 2 of the 1.2(j) follow-up: the reviewer's typed-refusal
# ruling (reviewer, 2026-10-15) as a standing guard. The ruled
# classes live in docs/typed-refusals-inventory.md, "Ruling
# (reviewer, 2026-10-15)"; R1 keeps the mechanical test as
# typed_reason.startswith("not supported:") and allows the
# prefix ONLY on Master-approved not-supported classes (D18,
# D33, D50). This test reads the inventory's ruled classes and
# asserts, for every ruled row, that the string the code
# actually emits starts with the prefix iff the row is ruled
# "not supported" — both directions, so a prefix can never
# creep onto an our-gap or harness string (which would
# silently skip a side H5 must judge) and a ruled
# not-supported string can never lose it.

_INVENTORY = (
    Path(__file__).resolve().parent.parent
    / "docs"
    / "typed-refusals-inventory.md"
)

# Rows whose inventory entry is a mechanism or a cross-reference
# rather than its own emitted literal:
_REFUSAL_ROW_OVERRIDES: dict[int, dict[str, object]] = {
    # #36 emits the plan's image_refusal verbatim -> row 1's string.
    36: {"template_from": 1},
    # #40's reason is a §4 SandboxError text truncated to 200
    # chars; those texts are rows 43-55, each checked on its own.
    40: {"skip": True},
    # #57 is the row_from_exception passthrough mechanism; the
    # ruling classes its named instance, emitted by yml.py.
    57: {"template": "proofdeploy.yml missing required keys: {keys}"},
    # §7 (PR #109) rows whose inventory entry leads with the
    # refusal DETAIL: the emitted literal is the fetch layer's
    # own full form, which the materialize layer re-raises
    # inside #62's wrapper.
    63: {
        "template": (
            "not supported: submodule at {sha} unavailable "
            "from {url}: {first line}"
        )
    },
    64: {
        "template": (
            "not supported: submodule at {sha} unavailable "
            "from {url}: the remote did not serve the recorded commit"
        )
    },
    65: {"template": "not supported: submodule URL refused before fetch: {url!r}"},
    66: {
        "template": "not supported: submodule commit refused before fetch: {sha!r}"
    },
    # #67/#68 surface as #62's wrapper string with the detail
    # inlined by _typed; the wrapper is checked at #62.
    67: {"template_from": 62},
    68: {"template_from": 62},
    # #81 is the family of git-operation failure strings; its
    # first member stands for the family.
    81: {"template": "git init --bare failed for submodule fetch of {url}: {stderr}"},
}

_SITE_FILE_BY_BASENAME = {
    "provisioning.py": "proofdeploy/provisioning.py",
    "runner.py": "proofdeploy/runner.py",
    "orchestrator.py": "proofdeploy/orchestrator.py",
    "sandbox.py": "proofdeploy/sandbox.py",
    "yml.py": "proofdeploy/yml.py",
    "provision.py": "readiness/provision.py",
    "submodules.py": "proofdeploy/submodules.py",
    "fixture.py": "proofdeploy/fixture.py",
}


def _inventory_rows() -> tuple[dict[int, dict[str, object]], dict[int, str]]:
    text = _INVENTORY.read_text(encoding="utf-8")
    main, _, ruling = text.partition("## Ruling (reviewer, 2026-10-15)")
    assert ruling, "inventory has no Ruling section"
    rows: dict[int, dict[str, object]] = {}
    for line in main.splitlines():
        cells = [c.strip() for c in line.split("|")]
        if len(cells) < 5 or not cells[1].isdigit():
            continue
        span = re.search(r"`([^`]+)`", cells[2])
        if span is None:
            continue
        files: list[str] = []
        for name in re.findall(r"[\w./-]+\.py", cells[3]):
            rel = name if "/" in name else _SITE_FILE_BY_BASENAME[name]
            if rel not in files:
                files.append(rel)
        rows[int(cells[1])] = {"template": span.group(1), "files": files}
    ruled: dict[int, str] = {}
    for line in ruling.splitlines():
        m = re.match(
            r"\| (\d+) \| (not supported|our gap|harness or environment)",
            line,
        )
        if m:
            ruled[int(m.group(1))] = m.group(2)
    return rows, ruled


def _literal_part_lists(path: Path) -> list[list[str]]:
    """Every string literal in a source file, as its list of
    literal parts (one part for a plain string; the constant
    segments, in order, for an f-string)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    lists: list[list[str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value:
                lists.append([node.value])
        elif isinstance(node, ast.JoinedStr):
            # Empty segments (an f-string's edge padding around a
            # placeholder) carry no text; dropping them keeps the
            # first real segment as the literal's anchor.
            parts = [
                v.value
                for v in node.values
                if isinstance(v, ast.Constant)
                and isinstance(v.value, str)
                and v.value
            ]
            if parts:
                lists.append(parts)
    return lists


def _segments(template: str) -> list[str]:
    return [
        seg.replace("\\n", "\n") for seg in re.split(r"\{[^}]*\}", template)
    ]


def _prefix_compatible(a: str, b: str) -> bool:
    # "" is a prefix of everything, so it is compatible only
    # with itself; otherwise any short literal could anchor a
    # match on an empty segment.
    if not a or not b:
        return a == b
    return a.startswith(b) or b.startswith(a)


def _matches(template: str, parts: list[str]) -> bool:
    segments = _segments(template)
    # The lists may differ in length only by trailing empty
    # segments (a template or a literal ending at a placeholder).
    width = max(len(segments), len(parts))
    segs = segments + [""] * (width - len(segments))
    pts = parts + [""] * (width - len(parts))
    # The anchor segment must match exactly: a shorter literal
    # that merely prefixes it (e.g. the constant "workspaces")
    # is a different string, not this row's emission.
    if not segs[0] or segs[0] != pts[0]:
        return False
    return all(
        _prefix_compatible(seg, part)
        for seg, part in zip(segs, pts, strict=True)
    )


def test_typed_refusal_prefix_coverage() -> None:
    rows, ruled = _inventory_rows()
    assert set(rows) == set(ruled), (
        "inventory rows and ruling rows disagree: "
        f"unruled={sorted(set(rows) - set(ruled))} "
        f"unknown={sorted(set(ruled) - set(rows))}"
    )
    root = Path(__file__).resolve().parent.parent
    candidates_by_file: dict[str, list[list[str]]] = {}
    failures: list[str] = []
    for number in sorted(rows):
        info = rows[number]
        override = _REFUSAL_ROW_OVERRIDES.get(number, {})
        if override.get("skip"):
            continue
        if "template_from" in override:
            info = rows[int(override["template_from"])]  # type: ignore[arg-type]
        template = str(override.get("template", info["template"]))
        files = list(info["files"])  # type: ignore[arg-type]
        want_prefix = ruled[number] == "not supported"
        # Doc side: the documented string carries the prefix iff
        # the row is ruled not supported.
        if template.startswith("not supported: ") != want_prefix:
            failures.append(
                f"#{number}: inventory string {template!r} "
                f"{'carries' if template.startswith('not supported: ') else 'lacks'} "
                f"the prefix but is ruled {ruled[number]!r}"
            )
            continue
        # Code side: an emitted literal in the row's site file(s)
        # must correspond to the documented template, and every
        # corresponding literal must agree on the prefix.
        matched: list[bool] = []
        for rel in files:
            if rel not in candidates_by_file:
                candidates_by_file[rel] = _literal_part_lists(root / rel)
            for parts in candidates_by_file[rel]:
                if _matches(template, parts):
                    matched.append(parts[0].startswith("not supported: "))
        if not matched:
            failures.append(
                f"#{number}: no emitted literal in {files} matches "
                f"the inventory string {template!r}"
            )
        elif len(set(matched)) > 1:
            failures.append(
                f"#{number}: emitted literals disagree on the prefix "
                f"for {template!r}"
            )
        elif matched[0] != want_prefix:
            failures.append(
                f"#{number}: emitted string for {template!r} "
                f"{'carries' if matched[0] else 'lacks'} the prefix "
                f"but is ruled {ruled[number]!r}"
            )
    assert not failures, "\n".join(failures)
