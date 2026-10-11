"""Provisioning adapters (WO-7): one interface, one source of truth.

Provisioning used to live twice: the host Provisioner (runner.py)
handled Node and Python, and the sandbox path in the orchestrator
carried its own lockfile/install logic for hashed requirements.txt
only. They diverged once (Addendum 15, S1). This module is the seam
that unifies them:

- A :class:`ProvisioningAdapter` DETECTS a runtime in a snapshot and
  PLANS the provisioning. Planning is pure data work: an adapter
  NEVER runs a command.
- The plan (:class:`InstallPlan`) is serializable data: runtime and
  declared range, the selected runtime version, the pinned image
  (ref + digest from ``images.lock.json``), the lockfile name and
  sha256, the package manager and its exact pinned version, the
  ordered steps (venv/install/verify/build/migrate/seed/start, each an
  argv list or a contract shell string, plus env), and the dirs that
  must persist inside the checkout — OR a typed refusal (a
  BUILD_FAILED reason) instead of a plan.
- Two executors run the same plan: the host Provisioner (dev,
  unsandboxed) and the orchestrator's sandbox path (measured). The
  evidence record carries the plan and its sha256.

Runtime selection for the sandbox comes from the committed table in
``images.lock.json`` (full official ``-bookworm`` images, digest
pinned): the highest table version satisfying the declared range
(``engines.node`` / ``requires-python``) wins. No satisfying row is a
typed refusal: "declared range R not satisfiable by pinned images".
The table changes only by a committed change.

The Python adapter carries today's hashed-requirements behavior
unchanged: venv in the checkout at ``/checkout/.venv`` (the sandbox
executor renders it there; the host executor renders the same step at
its per-run venv dir), ``pip install --require-hashes --no-deps``
(sandbox rendering), ``PIP_NO_CACHE_DIR=1``, the venv first on PATH
for build/migrate/seed/start, the lockfile sha256 recorded and
verified, seed runs, and unsupported lockfiles refused by the sandbox
executor with BUILD_FAILED "not yet supported in the sandbox".
Unlocked repos stay refused until WO-8 (REG-A). The Node adapter
plans frozen installs from the lockfile for both executors: npm ci /
pnpm install --frozen-lockfile / yarn classic --frozen-lockfile or
berry --immutable, a packageManager pin rendered through corepack,
node_modules persisted inside the checkout; workspaces and
packageManager/lockfile mismatches are typed refusals (WO-7 PR 2).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

from proofdeploy.runner import (
    _PM_LOCKFILES,
    LOCKFILES,
    _parse_version,
    check_requirements_frozen,
    detect_runtime,
    find_lockfile,
    read_package_manager,
    satisfies_range,
    select_package_manager,
)

# Lines used when a repo declares no version range: the runtimes the
# pre-WO-7 sandbox images pinned (python:3.12-slim, node:22).
DEFAULT_IMAGE_LINES = {"python": "3.12", "node": "22"}

# Placeholder tokens in plan step argv, rendered by each executor:
#   {runtime_bin}  the runtime executable (host: the selected binary;
#                  sandbox: python3 in the pinned image)
#   {venv}         the venv dir (host: per-run workdir venv; sandbox:
#                  /checkout/.venv)
#   {checkout}     the checkout root (host: snapshot dir; sandbox:
#                  /checkout)
#   {pip_flags}    expands to the executor's pip flag list (or nothing)
PIP_FLAGS_TOKEN = "{pip_flags}"


@dataclass(frozen=True)
class ImageRow:
    """One pinned image from images.lock.json."""

    runtime: str
    version: str
    ref: str
    digest: str

    @property
    def pinned_ref(self) -> str:
        """The ref executors pull: exact tag pinned by digest."""
        return f"{self.ref}@{self.digest}"


_IMAGES_CACHE: list[ImageRow] | None = None


def load_images() -> list[ImageRow]:
    """Load the committed pinned-image table (images.lock.json)."""
    global _IMAGES_CACHE
    if _IMAGES_CACHE is None:
        path = Path(__file__).with_name("images.lock.json")
        doc = json.loads(path.read_text(encoding="utf-8"))
        _IMAGES_CACHE = [
            ImageRow(
                runtime=str(r["runtime"]),
                version=str(r["version"]),
                ref=str(r["ref"]),
                digest=str(r["digest"]),
            )
            for r in doc["images"]
        ]
    return list(_IMAGES_CACHE)


def select_image(runtime: str, declared_range: str | None) -> ImageRow | None:
    """Select the pinned image for a runtime + declared range.

    The highest table version satisfying the range wins. With no
    declared range, the runtime's default line (DEFAULT_IMAGE_LINES)
    is used. Returns None when no row satisfies the range.
    """
    rows = [r for r in load_images() if r.runtime == runtime]
    if not rows:
        return None
    if declared_range:
        satisfying = [
            r for r in rows
            if satisfies_range(r.version, declared_range, runtime)
        ]
        if not satisfying:
            return None
        return max(satisfying, key=lambda r: _parse_version(r.version))
    line = DEFAULT_IMAGE_LINES.get(runtime)
    if line:
        line_rows = [r for r in rows if r.version.startswith(line + ".")]
        if line_rows:
            return max(line_rows, key=lambda r: _parse_version(r.version))
    return max(rows, key=lambda r: _parse_version(r.version))


@dataclass
class Detection:
    """An adapter's positive detection of a runtime in a snapshot."""

    runtime: str
    declared_range: str | None
    reason: str


@dataclass
class PlanStep:
    """One ordered step of an install plan.

    Exactly one of `argv` / `shell` is set: `argv` for steps the plan
    itself defines (venv creation, the frozen install, the venv
    sanity check), `shell` for the contract's own commands
    (build/migrate/seed/start), which each executor renders in its
    environment. `executor` restricts a step to one executor
    ("sandbox" / "host"); None means both run it.
    """

    phase: str  # venv | install | verify | build | migrate | seed | start
    argv: list[str] | None = None
    shell: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    executor: str | None = None


@dataclass
class InstallPlan:
    """Pure, serializable provisioning plan (or a typed refusal).

    When `refusal` is set, the plan is a BUILD_FAILED refusal with
    that reason and no steps run. `image_refusal` is a sandbox-only
    refusal: the plan is executable on a host, but no pinned image
    satisfies the declared range, so the sandbox executor fails
    closed with that reason.
    """

    adapter: str
    runtime: str
    declared_range: str | None = None
    selected_version: str | None = None
    image_ref: str | None = None  # pinned ref@digest from the table
    image_digest: str | None = None
    image_refusal: str | None = None
    lockfile_name: str | None = None
    lockfile_sha256: str | None = None
    package_manager: str | None = None
    package_manager_version: str | None = None
    pip_require_hashes: bool = False
    steps: list[PlanStep] = field(default_factory=list)
    persisted_dirs: list[str] = field(default_factory=list)
    refusal: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> InstallPlan:
        steps = [PlanStep(**s) for s in data.get("steps", [])]
        rest = {k: v for k, v in data.items() if k != "steps"}
        return cls(steps=steps, **rest)

    def sha256(self) -> str:
        """sha256 of the canonical JSON of this plan (as recorded)."""
        canonical = json.dumps(
            self.to_dict(), sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(canonical.encode()).hexdigest()


class ProvisioningAdapter(Protocol):
    """Plans provisioning for one runtime ecosystem. Never executes."""

    name: str

    def detect(self, snapshot_dir: Path) -> Detection | None:
        """Detect this adapter's runtime in the snapshot, or None."""
        ...

    def plan(self, snapshot_dir: Path, contract: Any) -> InstallPlan:
        """Build the install plan (or refusal) for this snapshot."""
        ...


def _image_fields(
    runtime: str, declared_range: str | None
) -> dict[str, Any]:
    """The plan's image selection: pinned row, or a sandbox refusal."""
    row = select_image(runtime, declared_range)
    if row is not None:
        return {
            "selected_version": row.version,
            "image_ref": row.pinned_ref,
            "image_digest": row.digest,
            "image_refusal": None,
        }
    refusal = None
    if declared_range:
        refusal = (
            f"declared range {declared_range} not satisfiable "
            "by pinned images"
        )
    return {
        "selected_version": None,
        "image_ref": None,
        "image_digest": None,
        "image_refusal": refusal,
    }


def _contract_steps(contract: Any) -> list[PlanStep]:
    """The contract's own steps, in order: build, migrate, seed, start."""
    steps: list[PlanStep] = []
    for phase in ("build", "migrate", "seed"):
        cmd = getattr(contract, phase, None)
        if cmd:
            steps.append(PlanStep(phase=phase, shell=str(cmd)))
    start = getattr(contract, "start", None)
    if start:
        steps.append(PlanStep(phase="start", shell=str(start)))
    return steps


class PythonAdapter:
    """Python: uv.lock / poetry.lock plans, and the hashed
    requirements.txt flow (venv + pip --require-hashes) unchanged."""

    name = "python"

    def detect(self, snapshot_dir: Path) -> Detection | None:
        if (snapshot_dir / "package.json").is_file():
            return None  # Node wins, exactly as detect_runtime orders it
        if not (
            (snapshot_dir / "pyproject.toml").is_file()
            or (snapshot_dir / "requirements.txt").is_file()
        ):
            return None
        _, declared = detect_runtime(snapshot_dir)
        source = (
            "pyproject.toml"
            if (snapshot_dir / "pyproject.toml").is_file()
            else "requirements.txt"
        )
        return Detection(
            runtime="python",
            declared_range=declared,
            reason=f"python detected from {source}",
        )

    def plan(self, snapshot_dir: Path, contract: Any) -> InstallPlan:
        det = self.detect(snapshot_dir)
        assert det is not None, "plan() called without a python detection"
        image = _image_fields("python", det.declared_range)
        found = find_lockfile(snapshot_dir, "python")
        if found is None:
            return InstallPlan(
                adapter=self.name,
                runtime="python",
                declared_range=det.declared_range,
                **image,
                refusal=(
                    "no frozen lockfile found in snapshot "
                    "for runtime 'python'"
                ),
            )
        lockfile_name, lockfile_sha = found
        base: dict[str, Any] = {
            "adapter": self.name,
            "runtime": "python",
            "declared_range": det.declared_range,
            "lockfile_name": lockfile_name,
            "lockfile_sha256": lockfile_sha,
            **image,
        }
        if lockfile_name != "requirements.txt":
            manager, argv = select_package_manager(
                snapshot_dir, "python", lockfile_name
            )
            return InstallPlan(
                **base,
                package_manager=manager,
                steps=[
                    PlanStep(phase="install", argv=argv),
                    *_contract_steps(contract),
                ],
                persisted_dirs=[".venv"] if manager == "uv" else [],
            )
        not_frozen = check_requirements_frozen(
            snapshot_dir / "requirements.txt"
        )
        if not_frozen is not None:
            return InstallPlan(**base, refusal=not_frozen)
        req_text = (snapshot_dir / "requirements.txt").read_text(
            encoding="utf-8"
        )
        steps = [
            PlanStep(
                phase="venv",
                argv=["{runtime_bin}", "-m", "venv", "{venv}"],
            ),
            PlanStep(
                phase="install",
                argv=[
                    "{venv}/bin/pip",
                    "install",
                    PIP_FLAGS_TOKEN,
                    "-r",
                    "{checkout}/requirements.txt",
                ],
            ),
            # Sandbox-only sanity check (Addendum 15, B2): the venv
            # must actually work before any contract step relies on
            # it; py_compile would not notice a broken venv.
            PlanStep(
                phase="verify",
                argv=[
                    "/checkout/.venv/bin/python",
                    "-c",
                    "import sys; print(sys.prefix)",
                ],
                executor="sandbox",
            ),
            *_contract_steps(contract),
        ]
        return InstallPlan(
            **base,
            package_manager="pip",
            pip_require_hashes="--hash" in req_text,
            steps=steps,
            persisted_dirs=[".venv"],
        )


class NodeAdapter:
    """Node: frozen installs planned from the lockfile.

    Detection/planning rules (WO-7 PR 2):

    - ``package-lock.json`` -> ``npm ci``; ``pnpm-lock.yaml`` ->
      ``pnpm install --frozen-lockfile``; ``yarn.lock`` -> ``yarn
      install --frozen-lockfile`` (classic) or ``--immutable``
      (berry), via runner.select_package_manager.
    - A ``packageManager`` pin renders the install through corepack
      at the exact pinned version (deterministically, as plan data —
      never dependent on the planning host's own corepack), with
      ``COREPACK_ENABLE_DOWNLOAD_PROMPT=0`` in the step env; the
      pinned version is recorded on the plan.
    - A ``packageManager`` that disagrees with the lockfile present
      (it pins pnpm, only package-lock.json exists) is a typed
      refusal — never a silent fall-back to the other manager.
    - Monorepo workspaces (a ``workspaces`` field in package.json,
      or a ``pnpm-workspace.yaml``) are refused with the registered
      wording: "workspaces not yet supported".
    - ``node_modules`` lives in the checkout (persisted_dirs), so it
      survives from the install container to the app container.
    """

    name = "node"

    def detect(self, snapshot_dir: Path) -> Detection | None:
        if not (snapshot_dir / "package.json").is_file():
            return None
        _, declared = detect_runtime(snapshot_dir)
        return Detection(
            runtime="node",
            declared_range=declared,
            reason="node detected from package.json",
        )

    def plan(self, snapshot_dir: Path, contract: Any) -> InstallPlan:
        det = self.detect(snapshot_dir)
        assert det is not None, "plan() called without a node detection"
        image = _image_fields("node", det.declared_range)
        base: dict[str, Any] = {
            "adapter": self.name,
            "runtime": "node",
            "declared_range": det.declared_range,
            **image,
        }
        # Monorepo workspaces: refused, with the registered wording.
        if self._declares_workspaces(snapshot_dir):
            return InstallPlan(**base, refusal="workspaces not yet supported")
        pm_name, pm_version = read_package_manager(snapshot_dir)
        # A packageManager that disagrees with the lockfile present
        # is a typed refusal. Checked against the lockfiles actually
        # in the snapshot, before any preference is applied.
        if pm_name and pm_name in _PM_LOCKFILES:
            present = [
                name
                for name in LOCKFILES["node"]
                if (snapshot_dir / name).is_file()
            ]
            expected = _PM_LOCKFILES[pm_name]
            if present and not any(name in expected for name in present):
                return InstallPlan(
                    **base,
                    refusal=(
                        f"packageManager {pm_name}@{pm_version} disagrees "
                        f"with the lockfile present in the snapshot "
                        f"({present[0]}); refusing to install with a "
                        "different package manager than the one pinned"
                    ),
                )
        preferred: list[str] | None = None
        if pm_name and pm_name in _PM_LOCKFILES:
            preferred = list(_PM_LOCKFILES[pm_name])
        found = find_lockfile(snapshot_dir, "node", preferred)
        if found is None:
            return InstallPlan(
                **base,
                refusal=(
                    "no frozen lockfile found in snapshot "
                    "for runtime 'node'"
                ),
            )
        lockfile_name, lockfile_sha = found
        manager, argv = select_package_manager(
            snapshot_dir, "node", lockfile_name
        )
        step_env: dict[str, str] = {}
        if pm_name == manager and pm_version and manager in (
            "pnpm", "yarn", "npm",
        ):
            # The pin renders through corepack, as plan data: strip
            # whatever prefix select_package_manager chose (plain
            # binary, or corepack when the planning host has it) and
            # prepend the deterministic corepack form.
            frozen_args = list(argv)
            corepack_prefix = ["corepack", f"{manager}@{pm_version}"]
            if frozen_args[:2] == corepack_prefix:
                frozen_args = frozen_args[2:]
            elif frozen_args[:1] == [manager]:
                frozen_args = frozen_args[1:]
            argv = [*corepack_prefix, *frozen_args]
            step_env = {"COREPACK_ENABLE_DOWNLOAD_PROMPT": "0"}
        return InstallPlan(
            **base,
            lockfile_name=lockfile_name,
            lockfile_sha256=lockfile_sha,
            package_manager=manager,
            package_manager_version=(
                pm_version if pm_name == manager else None
            ),
            steps=[
                PlanStep(phase="install", argv=argv, env=step_env),
                *_contract_steps(contract),
            ],
            persisted_dirs=["node_modules"],
        )

    @staticmethod
    def _declares_workspaces(snapshot_dir: Path) -> bool:
        """True for monorepo workspace declarations (refused)."""
        if (snapshot_dir / "pnpm-workspace.yaml").is_file():
            return True
        pkg = snapshot_dir / "package.json"
        try:
            data = json.loads(pkg.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return False
        return isinstance(data, dict) and "workspaces" in data


_ADAPTERS: dict[str, ProvisioningAdapter] = {
    "python": PythonAdapter(),
    "node": NodeAdapter(),
}

# Detection order matches detect_runtime's precedence (Node first).
_ADAPTER_ORDER = ("node", "python")


def get_adapter(name: str) -> ProvisioningAdapter:
    return _ADAPTERS[name]


def plan_provisioning(snapshot_dir: Path, contract: Any) -> InstallPlan:
    """Plan provisioning for a snapshot through its adapter.

    Raises ValueError (detect_runtime's message) when no adapter
    detects a runtime in the snapshot.
    """
    snap = Path(snapshot_dir)
    for name in _ADAPTER_ORDER:
        adapter = _ADAPTERS[name]
        if adapter.detect(snap) is not None:
            return adapter.plan(snap, contract)
    detect_runtime(snap)  # raises ValueError with the canonical message
    raise AssertionError("unreachable: detect_runtime did not raise")


def pip_flags(plan: InstallPlan, *, sandbox: bool) -> list[str]:
    """Render the pip install flags for one executor.

    Both executors pass --require-hashes when the requirements file
    carries hashes. --no-deps is the sandbox's frozen-install rule
    (the measured path installs exactly the hashed set); the host
    executor keeps its historical flag set.
    """
    flags: list[str] = []
    if plan.pip_require_hashes:
        flags.append("--require-hashes")
    if sandbox:
        flags.append("--no-deps")
    return flags


def render_argv(
    argv: list[str],
    mapping: dict[str, str],
    flags: list[str],
) -> list[str]:
    """Render a plan step's argv for one executor.

    Substitutes the placeholder tokens (see the module docstring);
    the {pip_flags} element expands to `flags` (or disappears).
    """
    out: list[str] = []
    for arg in argv:
        if arg == PIP_FLAGS_TOKEN:
            out.extend(flags)
            continue
        for token, value in mapping.items():
            arg = arg.replace(token, value)
        out.append(arg)
    return out
