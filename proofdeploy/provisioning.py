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

The Python adapter implements the registered Python lock rule
(REG-A, docs/registration/python-lock-rule.md; WO-8.1): a COMPLETE
committed lock — ``uv.lock``, ``poetry.lock``, ``pdm.lock``, or a
requirements file where every spec entry carries ``--hash`` —
installs frozen; anything else (an ``==``-only or unpinned
requirements file, or a bare ``pyproject.toml``) goes through lock
GENERATION: the pinned resolver (uv 0.13.0, bootstrapped into the
checkout's ``.pd-tools``) compiles the declared dependencies with
``--generate-hashes`` against the registered index, with
``--exclude-newer`` set to the unit's one cutoff (the fix commit's
committer time for a bug pair, C's for a clean diff — supplied by
the caller as ``lock_cutoff``), and the generated lock installs
with ``pip install --require-hashes --no-deps``. Generation without
a cutoff is a typed refusal: a lock resolved without the unit's
cutoff is not the registered artifact. The plan records the lock
source (committed / generated) and, for a generated lock, the
resolver and its version, the cutoff, and the index URL; the
executors read the generated lock back into the evidence. The
hashed-requirements flow is otherwise unchanged: venv in the
checkout at ``/checkout/.venv`` (the sandbox executor renders it
there; the host executor renders the same step at its per-run venv
dir), ``PIP_NO_CACHE_DIR=1``, the venv first on PATH for
build/migrate/seed/start, the lockfile sha256 recorded and
verified, seed runs, and unsupported lockfiles refused by the
sandbox executor with BUILD_FAILED "not yet supported in the
sandbox". The Node adapter
plans frozen installs from the lockfile for both executors: npm ci /
pnpm install --frozen-lockfile / yarn classic --frozen-lockfile or
berry --immutable, a packageManager pin rendered through corepack,
node_modules persisted inside the checkout; workspaces and
packageManager/lockfile mismatches are typed refusals (WO-7 PR 2).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
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

# A REG-A resolution cutoff as handed to an adapter: the timestamp
# itself, a zero-arg callable producing it (evaluated LAZILY — only
# when a generated lock is actually planned, so planning a
# committed lock or a Node plan never pays for, or trips over, the
# caller's cutoff source), or None (no cutoff available).
LockCutoff = str | Callable[[], str | None] | None

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
    # REG-A lock facts (Python; None for Node plans). lock_source is
    # "committed" (a complete lock in the repo, installed frozen) or
    # "generated" (the runner resolved the declared dependencies —
    # see lock_resolver / lock_resolver_version / lock_cutoff /
    # lock_index_url, and generated_lockfile_name for the file the
    # resolve step writes into the checkout). For a generated plan,
    # lockfile_name / lockfile_sha256 name the DECLARED source file
    # (requirements.txt or pyproject.toml) the lock was generated
    # from; the generated lock's own text + sha256 are captured by
    # the executors into the provision's generated_lock.
    lock_source: str | None = None
    lock_resolver: str | None = None
    lock_resolver_version: str | None = None
    lock_cutoff: str | None = None
    lock_index_url: str | None = None
    generated_lockfile_name: str | None = None
    package_manager: str | None = None
    package_manager_version: str | None = None
    corepack_version: str | None = None
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

    def plan(
        self,
        snapshot_dir: Path,
        contract: Any,
        lock_cutoff: LockCutoff = None,
    ) -> InstallPlan:
        """Build the install plan (or refusal) for this snapshot.

        ``lock_cutoff`` is the unit's REG-A resolution cutoff (an
        RFC 3339 timestamp) for a generated Python lock — or a
        zero-arg callable producing it, evaluated lazily and only
        when a generated lock is planned. Adapters that never
        generate a lock ignore it.
        """
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


# REG-A (docs/registration/python-lock-rule.md): the registered
# resolver for generated Python locks, its exact pinned version,
# the registered index, the target platform, and the file the
# resolve step writes into the checkout. The generated lock is
# recorded in the evidence (text + sha256) by the executors.
UV_VERSION = "0.13.0"
LOCK_INDEX_URL = "https://pypi.org/simple"
LOCK_TARGET_PLATFORM = "x86_64-unknown-linux-gnu"
GENERATED_LOCKFILE_NAME = ".pd-generated-requirements.txt"


class PythonAdapter:
    """Python: REG-A — complete committed locks (uv.lock /
    poetry.lock / pdm.lock / fully hashed requirements.txt) install
    frozen; anything else is resolved into a generated hashed lock
    by the pinned uv at the unit's cutoff, then installed frozen."""

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

    def plan(
        self,
        snapshot_dir: Path,
        contract: Any,
        lock_cutoff: LockCutoff = None,
    ) -> InstallPlan:
        det = self.detect(snapshot_dir)
        assert det is not None, "plan() called without a python detection"
        image = _image_fields("python", det.declared_range)
        found = find_lockfile(snapshot_dir, "python")
        # REG-A item 1: a COMPLETE committed lock installs frozen —
        # uv.lock / poetry.lock / pdm.lock by construction, or a
        # requirements file where every spec entry carries --hash
        # (check_requirements_frozen is now exactly that check).
        if found is not None and found[0] != "requirements.txt":
            lockfile_name, lockfile_sha = found
            manager, argv = select_package_manager(
                snapshot_dir, "python", lockfile_name
            )
            return InstallPlan(
                adapter=self.name,
                runtime="python",
                declared_range=det.declared_range,
                lockfile_name=lockfile_name,
                lockfile_sha256=lockfile_sha,
                lock_source="committed",
                **image,
                package_manager=manager,
                steps=[
                    PlanStep(phase="install", argv=argv),
                    *_contract_steps(contract),
                ],
                persisted_dirs=[".venv"] if manager == "uv" else [],
            )
        if found is not None and check_requirements_frozen(
            snapshot_dir / "requirements.txt"
        ) is None:
            lockfile_name, lockfile_sha = found
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
                # Sandbox-only sanity check (Addendum 15, B2): the
                # venv must actually work before any contract step
                # relies on it; py_compile would not notice a
                # broken venv.
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
                adapter=self.name,
                runtime="python",
                declared_range=det.declared_range,
                lockfile_name=lockfile_name,
                lockfile_sha256=lockfile_sha,
                lock_source="committed",
                **image,
                package_manager="pip",
                pip_require_hashes="--hash" in req_text,
                steps=steps,
                persisted_dirs=[".venv"],
            )
        # REG-A item 2: no complete lock — GENERATE one. The declared
        # source is the requirements file when one is present (its
        # pins are the constraints) or pyproject.toml otherwise.
        if (snapshot_dir / "requirements.txt").is_file():
            source_name = "requirements.txt"
        elif (snapshot_dir / "pyproject.toml").is_file():
            source_name = "pyproject.toml"
        else:  # pragma: no cover - detect() guarantees one exists
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
        source_sha = hashlib.sha256(
            (snapshot_dir / source_name).read_bytes()
        ).hexdigest()
        # The cutoff is evaluated only NOW, when a generated lock
        # is actually being planned (see LockCutoff): planning a
        # committed lock above never touched the caller's source.
        cutoff = lock_cutoff() if callable(lock_cutoff) else lock_cutoff
        generated_base: dict[str, Any] = {
            "adapter": self.name,
            "runtime": "python",
            "declared_range": det.declared_range,
            "lockfile_name": source_name,
            "lockfile_sha256": source_sha,
            "lock_source": "generated",
            "lock_resolver": "uv",
            "lock_resolver_version": UV_VERSION,
            "lock_index_url": LOCK_INDEX_URL,
            "generated_lockfile_name": GENERATED_LOCKFILE_NAME,
            **image,
        }
        if cutoff is None:
            # Fail closed: a lock generated without the unit's one
            # cutoff is not the registered artifact, and there is no
            # honest default to invent here.
            return InstallPlan(
                **generated_base,
                refusal=(
                    "python lock generation requires the unit's "
                    "resolution cutoff (the fix commit's committer "
                    "time for a bug pair, C's for a clean diff); "
                    "none was provided"
                ),
            )
        python_version = (
            image["selected_version"] or DEFAULT_IMAGE_LINES["python"]
        )
        uv_bin = "{checkout}/.pd-tools/bin/uv"
        steps = [
            PlanStep(
                phase="venv",
                argv=["{runtime_bin}", "-m", "venv", "{venv}"],
            ),
            # The pinned resolver, bootstrapped into the checkout's
            # .pd-tools (persisted across the sandbox's per-step
            # containers — the corepack pattern, Addendum 35; never
            # a global install). A dedicated venv keeps the layout
            # identical on every pip: <prefix>/bin/uv.
            PlanStep(
                phase="install",
                argv=[
                    "{runtime_bin}", "-m", "venv",
                    "{checkout}/.pd-tools",
                ],
            ),
            PlanStep(
                phase="install",
                argv=[
                    "{checkout}/.pd-tools/bin/pip",
                    "install",
                    f"uv=={UV_VERSION}",
                ],
            ),
            # REG-A item 2: resolve the declared dependencies with
            # the pinned uv, hashes generated, for the selected
            # Python version and linux/x86_64, against the
            # registered index, with the unit's one cutoff.
            PlanStep(
                phase="install",
                argv=[
                    uv_bin,
                    "pip",
                    "compile",
                    f"{{checkout}}/{source_name}",
                    "--generate-hashes",
                    "--exclude-newer",
                    cutoff,
                    "--python-version",
                    python_version,
                    "--python-platform",
                    LOCK_TARGET_PLATFORM,
                    "--index-url",
                    LOCK_INDEX_URL,
                    "--output-file",
                    f"{{checkout}}/{GENERATED_LOCKFILE_NAME}",
                ],
                env={"UV_NATIVE_TLS": "true"},
            ),
            # REG-A item 2: install the generated lock frozen.
            PlanStep(
                phase="install",
                argv=[
                    "{venv}/bin/pip",
                    "install",
                    PIP_FLAGS_TOKEN,
                    "-r",
                    f"{{checkout}}/{GENERATED_LOCKFILE_NAME}",
                ],
            ),
            # Sandbox-only sanity check (Addendum 15, B2), as on the
            # committed path.
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
            **generated_base,
            lock_cutoff=cutoff,
            package_manager="pip",
            pip_require_hashes=True,
            steps=steps,
            persisted_dirs=[".venv", ".pd-tools"],
        )


# The pinned corepack a packageManager-pinned Node plan bootstraps
# into the checkout's .pd-tools (Addendum 35). Persisted form on
# every row: each sandbox step is its own --rm container and only
# the checkout survives between steps, so a global (-g) install
# would die with the install container — and corepack is not even
# bundled from Node 25 onward (verified against the pinned
# node:26.11.1 image: `corepack: command not found`).
COREPACK_BOOTSTRAP_VERSION = "0.36.0"
COREPACK_BIN = "{checkout}/.pd-tools/node_modules/.bin/corepack"

# Addendum 37: every Node install step renders with this env. The
# official Node images ship the exact runtime's headers in
# /usr/local/include/node, so node-gyp compiles a native module
# that has no prebuild against the LOCAL headers — no download
# from nodejs.org and no tarball extraction. (The download path
# dies in the sandbox install container: node-gyp's tar extraction
# hits `TAR_ENTRY_ERROR EPERM: operation not permitted, fchown`
# under the userns mapping + --cap-drop=ALL.) npm and pnpm both
# honor npm_config_nodedir; it is plan data, so it is recorded.
NODE_INSTALL_ENV = {"npm_config_nodedir": "/usr/local"}


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
    - Every install step's env carries
      ``npm_config_nodedir=/usr/local`` (Addendum 37): the pinned
      images ship the runtime's own headers there, so a native
      module without a prebuild compiles locally — no nodejs.org
      download, no tarball extraction.
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

    def plan(
        self,
        snapshot_dir: Path,
        contract: Any,
        lock_cutoff: LockCutoff = None,
    ) -> InstallPlan:
        # Node never generates a lock (REG-A is Python-only); the
        # cutoff is accepted for the Protocol and ignored (and, being
        # possibly a lazy callable, never evaluated here).
        del lock_cutoff
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
        # Addendum 37: EVERY Node install step carries the nodedir
        # env (compile fallback against the image's own headers).
        step_env: dict[str, str] = dict(NODE_INSTALL_ENV)
        pinned = bool(
            pm_name == manager
            and pm_version
            and manager in ("pnpm", "yarn", "npm")
        )
        if pinned:
            # The pin renders through corepack, as plan data: strip
            # whatever prefix select_package_manager chose (plain
            # binary, or corepack when the planning host has it) and
            # prepend the deterministic persisted-corepack form.
            frozen_args = list(argv)
            corepack_prefix = [COREPACK_BIN, f"{manager}@{pm_version}"]
            if frozen_args[:2] == [
                "corepack", f"{manager}@{pm_version}",
            ] or frozen_args[:2] == corepack_prefix:
                frozen_args = frozen_args[2:]
            elif frozen_args[:1] == [manager]:
                frozen_args = frozen_args[1:]
            argv = [*corepack_prefix, *frozen_args]
            step_env["COREPACK_ENABLE_DOWNLOAD_PROMPT"] = "0"
        steps: list[PlanStep] = []
        corepack_version: str | None = None
        if pinned:
            # Addendum 35: the bootstrap is part of the pinned
            # rendering on EVERY row, in persisted form — corepack
            # is installed into the checkout's .pd-tools (which
            # persists across the per-step --rm containers), never
            # -g. COREPACK_HOME rides on HOME (inside the checkout
            # in the sandbox), so the downloaded manager survives
            # to later steps with the network cut.
            corepack_version = COREPACK_BOOTSTRAP_VERSION
            steps.append(
                PlanStep(
                    phase="install",
                    argv=[
                        "npm", "install", "--prefix",
                        "{checkout}/.pd-tools",
                        "--no-save", "--no-audit", "--no-fund",
                        f"corepack@{COREPACK_BOOTSTRAP_VERSION}",
                    ],
                    env=dict(NODE_INSTALL_ENV),
                )
            )
        steps.append(PlanStep(phase="install", argv=argv, env=step_env))
        steps.extend(_contract_steps(contract))
        return InstallPlan(
            **base,
            lockfile_name=lockfile_name,
            lockfile_sha256=lockfile_sha,
            package_manager=manager,
            package_manager_version=(
                pm_version if pm_name == manager else None
            ),
            corepack_version=corepack_version,
            steps=steps,
            persisted_dirs=(
                ["node_modules", ".pd-tools"]
                if corepack_version
                else ["node_modules"]
            ),
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


def plan_provisioning(
    snapshot_dir: Path,
    contract: Any,
    lock_cutoff: LockCutoff = None,
) -> InstallPlan:
    """Plan provisioning for a snapshot through its adapter.

    ``lock_cutoff`` is the unit's REG-A resolution cutoff for a
    generated Python lock (see the Protocol). Raises ValueError
    (detect_runtime's message) when no adapter detects a runtime in
    the snapshot.
    """
    snap = Path(snapshot_dir)
    for name in _ADAPTER_ORDER:
        adapter = _ADAPTERS[name]
        if adapter.detect(snap) is not None:
            return adapter.plan(snap, contract, lock_cutoff=lock_cutoff)
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
