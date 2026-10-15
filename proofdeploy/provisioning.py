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
verified, and seed runs. A committed ``uv.lock`` / ``poetry.lock``
/ ``pdm.lock`` plans the same way for both executors (WO-8.1b):
the checkout venv created from the selected interpreter, the
manager bootstrapped into ``.pd-tools`` by hash from its committed
bootstrap file at its pinned version (uv 0.13.0, poetry 2.5.1,
pdm 2.29.2), the tool's own lock-freshness check where its install
command does not refuse a stale lock itself, and the frozen
install (``uv sync --frozen``, ``poetry install --sync``,
``pdm sync``). A committed manager's install also builds the
project ITSELF, and that build's backend resolution is the
WO-8.1c concern: the plan records the build backend (name, and
the resolved version where the plan itself determines it) as
non-scoring evidence, constrains the build-isolation resolution
to the unit's cutoff where the tool allows it (uv:
``UV_EXCLUDE_NEWER`` on the sync step, which a ``--frozen`` sync
applies to the build resolution only, never to the lock's own
files), and records a typed limitation where it cannot (pdm —
see ``PDM_BUILD_CUTOFF_LIMITATION``). Poetry needs neither for
the ROOT project: it builds the root itself with the poetry-core
pinned by hash in its bootstrap file, so no backend resolution
happens for the root at install time. One level down, a locked
DEPENDENCY that ships only as an sdist (no compatible wheel in
the lock) is built by poetry and pdm alike with its build
backend resolved fresh from the index, unconstrained by the
unit cutoff — neither tool offers a build-scoped cutoff for
dependency builds — so both plans record that as a typed
limitation scoped to sdist dependency builds (see
``POETRY_SDIST_DEPENDENCY_LIMITATION`` /
``PDM_SDIST_DEPENDENCY_LIMITATION``); uv's sync-step cutoff
governs its dependency builds too, so uv records none. The
committed fixtures' locks are wheel-only (every locked package
carries a compatible wheel), so the case does not arise for
them. The pip paths never install the root
project, so they carry none of this. The executors complete the
evidence by reading the built project's dist-info back from the
checkout venv (``build_backend_evidence``): the backend name and
version that ACTUALLY built the project, as stamped by the tool
in the wheel's ``Generator``. The Node adapter
plans frozen installs from the lockfile for both executors: npm ci /
pnpm install --frozen-lockfile / yarn classic --frozen-lockfile or
berry --immutable, a packageManager pin rendered through corepack,
node_modules persisted inside the checkout. Monorepo workspaces
are planned (plan 1.2b, via ``plan_workspace``): the frozen
install runs at the workspace root and the contract steps are
scoped to the resolved app member; an ambiguous workspace
(several members, no contract ``app.path``), a workspace manager
without support, and packageManager/lockfile mismatches are
typed refusals (WO-7 PR 2).
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import platform
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

import yaml

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
# WO-8.1c adds one guarded exception: a committed UV plan also
# evaluates it, solely to constrain the build-isolation resolution
# (UV_EXCLUDE_NEWER on the sync step) — guarded, so a cutoff source
# that fails still never trips a committed plan; the plan records
# a typed build limitation instead (see _build_backend_plan).
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
    # Working directory RELATIVE to the checkout root (plan 1.2b):
    # None means the checkout root itself, where every step ran
    # before workspace support. Workspace plans scope the contract
    # steps (build/migrate/seed/start) to the app member's directory
    # this way; install steps always run at the workspace root.
    cwd: str | None = None


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
    # Addendum 46: the sha256 of the COMMITTED bootstrap
    # requirements file the resolver is installed from (the file
    # pins the resolver's wheel by hash; see
    # _uv_bootstrap_requirements).
    lock_resolver_bootstrap_sha256: str | None = None
    # WO-8.1b: for a committed uv.lock / poetry.lock / pdm.lock
    # plan, the sha256 of the COMMITTED bootstrap requirements
    # file the lock manager is installed from (the same pattern:
    # the file pins the manager's wheel(s) by hash; see
    # _manager_bootstrap_steps). The generated path keeps its own
    # field above; a plan carries at most one of the two.
    manager_bootstrap_sha256: str | None = None
    # WO-8.1c: the build of the root project itself, which a
    # committed manager's install performs (uv sync / poetry
    # install / pdm sync build the project; the pip paths never
    # install the root project, so they leave all of these unset).
    # Evidence only — non-scoring. build_backend is the backend
    # that builds the root under this plan, as its PEP 517 module:
    # for uv / pdm the [build-system] build-backend declared in
    # pyproject.toml (or the PEP 517 default when undeclared);
    # for poetry it is poetry.core.masonry.api, because poetry
    # builds the root project itself with its bundled poetry-core
    # (verified against the pinned poetry 2.5.1: installing the
    # fixture's root fetched no setuptools at all and stamped the
    # wheel "Generator: poetry-core 2.5.0"; the declared backend
    # governs other frontends, not `poetry install`).
    # build_backend_name is that backend's distribution name.
    # build_backend_version is the resolved version when the plan
    # itself determines it — poetry's, from the poetry-core pin in
    # the committed bootstrap file. For uv / pdm the version is
    # resolved fresh from the index at build time, so the field
    # stays None here and the executors record what ACTUALLY built
    # the project on the provision instead (build_backend_evidence
    # reads the built wheel's Generator back from the venv).
    # build_cutoff is the unit cutoff the build-isolation
    # resolution was constrained to, where the tool allows it
    # (uv only); build_limitations carries the typed limitation,
    # in registered wording, where it does not.
    build_backend: str | None = None
    build_backend_name: str | None = None
    build_backend_version: str | None = None
    build_cutoff: str | None = None
    build_limitations: list[str] = field(default_factory=list)
    generated_lockfile_name: str | None = None
    package_manager: str | None = None
    package_manager_version: str | None = None
    corepack_version: str | None = None
    # Plan 1.2e (D22): set on bun plans — the selected bun version
    # (the app's packageManager pin when declared, else the pin
    # table's default) and the sha256 of the pinned release
    # artifact the bootstrap step verifies before unpacking
    # (BUN_RELEASE_SHA256). The version is recorded even when the
    # app declared none: the plan/record always names the exact
    # bun that will run the frozen install.
    bun_version: str | None = None
    bun_sha256: str | None = None
    # Workspace facts (plan 1.2b; both None on a non-workspace plan).
    # workspace_root is the directory the frozen install runs in,
    # relative to the checkout root ("." — installs always run at
    # the root); app_path is the workspace member the contract steps
    # are scoped to ("." when the app is the root package itself).
    workspace_root: str | None = None
    app_path: str | None = None
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

    def plan_workspace(
        self,
        snapshot_dir: Path,
        contract: Any,
        lock_cutoff: LockCutoff = None,
    ) -> InstallPlan:
        """Build the install plan (or refusal) for a workspace /
        monorepo snapshot (plan 1.2b).

        The shape is ecosystem-shaped, never manager-shaped: the
        frozen install runs at the workspace root with the manager
        the adapter selects from the workspace root's own lockfile (a plan
        fact, recorded on the plan), and the contract's
        build/migrate/seed/start steps are scoped to the resolved
        app member — the contract's ``app.path`` when declared,
        else the single member, else the root; several members
        with no declaration is the typed refusal
        ``ambiguous app: <n> candidates``. A later manager plugs
        into this same method by joining the adapter's supported
        set (plan 1.2e does exactly that for bun) — the interface
        does not change.
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


def _contract_steps(
    contract: Any, cwd: str | None = None
) -> list[PlanStep]:
    """The contract's own steps, in order: build, migrate, seed, start.

    ``cwd`` (plan 1.2b) scopes every step to a directory relative
    to the checkout root — the workspace member being built; None
    keeps the historical behavior (steps run at the root).
    """
    steps: list[PlanStep] = []
    for phase in ("build", "migrate", "seed"):
        cmd = getattr(contract, phase, None)
        if cmd:
            steps.append(PlanStep(phase=phase, shell=str(cmd), cwd=cwd))
    start = getattr(contract, "start", None)
    if start:
        steps.append(PlanStep(phase="start", shell=str(start), cwd=cwd))
    return steps


# ---------------------------------------------------------------------------
# Workspace layout (plan 1.2b)
# ---------------------------------------------------------------------------


@dataclass
class WorkspaceLayout:
    """A detected monorepo workspace declaration in a snapshot.

    ``source`` names where the declaration was found
    (``pnpm-workspace.yaml`` or ``package.json``); ``members`` are
    the member package directories (relative to the root, sorted)
    that contain a package.json. The root package itself is never
    a member: it is the app only when no member is, or when the
    contract names ``"."`` explicitly.
    """

    source: str
    members: list[str]


def _workspace_globs(snapshot_dir: Path) -> tuple[str, list[str]] | None:
    """The declared workspace member globs, or None when undeclared.

    ``pnpm-workspace.yaml``'s ``packages:`` wins when the file
    exists (it is pnpm's own workspace file); otherwise
    package.json's ``workspaces`` (a list, or a mapping with a
    ``packages`` list). An unparseable declaration yields an empty
    glob list: the workspace is still declared (the caller's
    detection said so), it just names no members.
    """
    ws_file = snapshot_dir / "pnpm-workspace.yaml"
    if ws_file.is_file():
        globs: list[str] = []
        try:
            data = yaml.safe_load(ws_file.read_text(encoding="utf-8"))
        except (yaml.YAMLError, OSError):
            data = None
        if isinstance(data, dict):
            pkgs = data.get("packages")
            if isinstance(pkgs, list):
                globs = [g for g in pkgs if isinstance(g, str)]
        return "pnpm-workspace.yaml", globs
    pkg = snapshot_dir / "package.json"
    try:
        data = json.loads(pkg.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict) or "workspaces" not in data:
        return None
    ws = data.get("workspaces")
    globs = []
    if isinstance(ws, list):
        globs = [g for g in ws if isinstance(g, str)]
    elif isinstance(ws, dict):
        pkgs = ws.get("packages")
        if isinstance(pkgs, list):
            globs = [g for g in pkgs if isinstance(g, str)]
    return "package.json", globs


def _glob_member_dirs(root: Path, pattern: str) -> set[str]:
    """Directories under ``root`` matched by one workspace glob.

    Segments match with fnmatch; ``**`` spans any depth (including
    zero). Hidden directories and node_modules are never entered
    by ``**``: they cannot be workspace members, and node_modules
    may be a huge installed tree.
    """
    parts = [p for p in pattern.split("/") if p not in ("", ".")]
    if not parts:
        return set()
    found: set[str] = set()

    def walk(cur: Path, rest: list[str]) -> None:
        if not rest:
            rel = cur.relative_to(root).as_posix()
            if rel != ".":
                found.add(rel)
            return
        head, tail = rest[0], rest[1:]
        if head == "**":
            walk(cur, tail)  # zero segments
            for child in sorted(cur.iterdir()):
                if (
                    child.is_dir()
                    and not child.name.startswith(".")
                    and child.name != "node_modules"
                ):
                    walk(child, rest)  # one or more segments
            return
        if not cur.is_dir():
            return
        for child in sorted(cur.iterdir()):
            if child.is_dir() and fnmatch.fnmatchcase(child.name, head):
                walk(child, tail)

    if root.is_dir():
        walk(root, parts)
    return found


def detect_workspace(snapshot_dir: Path) -> WorkspaceLayout | None:
    """Detect a monorepo workspace declaration in a snapshot.

    Returns the layout (declaration source + member package
    dirs), or None when no workspace is declared. Members are
    the glob matches (with ``!`` exclusions applied) that
    contain a package.json, sorted.
    """
    found = _workspace_globs(snapshot_dir)
    if found is None:
        return None
    source, globs = found
    members: set[str] = set()
    exclusions: list[str] = []
    for glob in globs:
        if glob.startswith("!"):
            exclusions.append(glob[1:])
        else:
            members |= _glob_member_dirs(snapshot_dir, glob)
    for glob in exclusions:
        members -= _glob_member_dirs(snapshot_dir, glob)
    return WorkspaceLayout(
        source=source,
        members=sorted(
            m for m in members if (snapshot_dir / m / "package.json").is_file()
        ),
    )


def _resolve_workspace_app(
    layout: WorkspaceLayout, declared: str | None
) -> tuple[str | None, str | None]:
    """Resolve which workspace member is the app: (app_path, refusal).

    The contract's ``app.path`` wins when declared; it must name
    the root (``"."``) or a detected member — anything else is a
    typed refusal, never a guess. Undeclared: a single member is
    the app, no members means the root package itself is the app,
    and several members is the typed ambiguity refusal.
    """
    if declared is not None:
        app_path = str(Path(declared))
        if app_path == "." or app_path in layout.members:
            return app_path, None
        return None, (
            f"app.path {declared!r} is not a workspace package "
            "in this snapshot (workspace members: "
            f"{', '.join(layout.members) or 'none'})"
        )
    if len(layout.members) > 1:
        return None, f"ambiguous app: {len(layout.members)} candidates"
    if layout.members:
        return layout.members[0], None
    return ".", None


# REG-A (docs/registration/python-lock-rule.md): the registered
# resolver for generated Python locks, its exact pinned version,
# the registered index, the target platform, and the file the
# resolve step writes into the checkout. The generated lock is
# recorded in the evidence (text + sha256) by the executors.
UV_VERSION = "0.13.0"
LOCK_INDEX_URL = "https://pypi.org/simple"
LOCK_TARGET_PLATFORM = "x86_64-unknown-linux-gnu"
GENERATED_LOCKFILE_NAME = ".pd-generated-requirements.txt"

# Addendum 46: the resolver is pinned BY HASH, not by version
# alone. The committed bootstrap requirements file (kept next to
# this module) pins uv==0.13.0 with the sha256 of its manylinux
# x86_64 wheel; a generated plan materializes that file verbatim
# into the checkout's .pd-tools, installs from it with
# --require-hashes, and records the file's sha256 on the plan
# (lock_resolver_bootstrap_sha256).
UV_BOOTSTRAP_REQUIREMENTS_FILE = "uv_bootstrap_requirements.txt"
UV_BOOTSTRAP_CHECKOUT_PATH = ".pd-tools/uv-requirements.txt"

# Writes argv[1]'s file with argv[2]'s exact bytes: a plan step
# ([python, -c, THIS, <path>, <content>]) that materializes a
# committed file into the checkout — the install container mounts
# only the checkout, so a package file cannot be referenced in
# place on every executor.
_WRITE_FILE_CODE = (
    "import pathlib,sys; "
    "pathlib.Path(sys.argv[1]).write_bytes(sys.argv[2].encode())"
)


def _bootstrap_requirements(file_name: str) -> tuple[str, str]:
    """A committed bootstrap requirements file: (text, sha256)."""
    data = Path(__file__).with_name(file_name).read_bytes()
    return data.decode("utf-8"), hashlib.sha256(data).hexdigest()


def _uv_bootstrap_requirements() -> tuple[str, str]:
    """The committed resolver-bootstrap requirements: (text, sha256)."""
    return _bootstrap_requirements(UV_BOOTSTRAP_REQUIREMENTS_FILE)


# WO-8.1b: the committed-lock managers, their pinned versions, and
# their bootstrap files (committed next to this module, the
# Addendum 46 pattern). uv's pin is UV_VERSION — the REG-A
# resolver pin: the same hash-pinned wheel that resolves a
# generated lock also installs a committed uv.lock frozen. The
# poetry / pdm pins follow the resolver's rule (pin a version
# whose release is at least 14 days old when pinned): Poetry
# 2.5.1 was released 2026-09-20 and PDM 2.29.2 on 2026-09-17,
# pinned 2026-10-12. Neither is a single wheel, so their
# bootstrap files pin the manager's full dependency closure, each
# line hashed, installed --require-hashes --no-deps
# --only-binary :all:.
POETRY_VERSION = "2.5.1"
PDM_VERSION = "2.29.2"
POETRY_BOOTSTRAP_REQUIREMENTS_FILE = "poetry_bootstrap_requirements.txt"
PDM_BOOTSTRAP_REQUIREMENTS_FILE = "pdm_bootstrap_requirements.txt"
POETRY_BOOTSTRAP_CHECKOUT_PATH = ".pd-tools/poetry-requirements.txt"
PDM_BOOTSTRAP_CHECKOUT_PATH = ".pd-tools/pdm-requirements.txt"

_LOCK_MANAGERS: dict[str, tuple[str, str, str]] = {
    # manager -> (bootstrap file, its path inside the checkout,
    #             pinned version)
    "uv": (
        UV_BOOTSTRAP_REQUIREMENTS_FILE,
        UV_BOOTSTRAP_CHECKOUT_PATH,
        UV_VERSION,
    ),
    "poetry": (
        POETRY_BOOTSTRAP_REQUIREMENTS_FILE,
        POETRY_BOOTSTRAP_CHECKOUT_PATH,
        POETRY_VERSION,
    ),
    "pdm": (
        PDM_BOOTSTRAP_REQUIREMENTS_FILE,
        PDM_BOOTSTRAP_CHECKOUT_PATH,
        PDM_VERSION,
    ),
}

# Committed Python lockfiles (REG-A item 1) and their managers.
_COMMITTED_LOCK_MANAGERS = {
    "uv.lock": "uv",
    "poetry.lock": "poetry",
    "pdm.lock": "pdm",
}

# WO-8.1c: the typed limitation recorded on a committed pdm plan.
# pdm 2.29.2's only date cutoff is the resolution strategy
# `strategy.exclude-newer` (config file, or the project's own
# [tool.pdm.resolution]) and the `pdm lock --exclude-newer` flag;
# `pdm sync` has neither a flag nor an environment variable for
# it (the config item declares no env_var). The strategy is
# resolution-wide, not build-scoped — verified empirically
# against the pinned tool (2026-10-13): with the strategy set to
# 2023-06-01, `pdm sync` REFUSED the committed lock's own six
# 1.17.0 (uploaded 2024-12-04) with CandidateNotFound, because
# the installer fetches locked files through the same filtered
# finder. Constraining pdm's build that way would break REG-A
# item 1's frozen install, so the build-isolation resolution
# stays unconstrained and the plan says so, in this wording.
PDM_BUILD_CUTOFF_LIMITATION = (
    "build-backend-cutoff-unavailable: pdm 2.29.2 cannot "
    "constrain its build-isolation resolution to the unit "
    "cutoff — its only date cutoff (strategy.exclude-newer / "
    "pdm lock --exclude-newer) is a resolution-wide strategy "
    "that also filters the committed lock's own files when "
    "pdm sync fetches them, and it has no per-invocation form "
    "(no sync flag, no environment variable), so using it "
    "would break the frozen install (REG-A item 1)"
)

# WO-8.1c (reviewer's owed item 1, 2026-10-13): the typed
# limitation recorded on committed poetry and pdm plans for
# SDIST-ONLY LOCKED DEPENDENCIES. The root-project record does
# not cover this case: when a locked dependency ships only as
# an sdist (no compatible wheel in the lock), the installer
# must BUILD that dependency, and building it resolves the
# dependency's own build backend fresh from the index. Neither
# poetry 2.5.1 nor pdm 2.29.2 offers a build-scoped date cutoff
# for dependency builds (pdm's only cutoff is the
# resolution-wide strategy above; poetry has none), so that
# resolution is unconstrained by the unit cutoff. uv needs no
# such limitation: UV_EXCLUDE_NEWER sits on the sync step and
# governs every resolution that step performs, dependency
# builds included. The limitation is conditional in wording —
# it states what happens IF a lock carries an sdist-only
# dependency. The committed fixtures' locks are wheel-only
# (tests/test_wo81c.py proves every locked package in all
# three locks carries a compatible wheel), so the case does
# not arise for them.
POETRY_SDIST_DEPENDENCY_LIMITATION = (
    "sdist-dependency-build-unconstrained: if a locked "
    "dependency ships only as an sdist (no compatible wheel "
    "in the lock), poetry 2.5.1 builds it by resolving the "
    "dependency's build backend fresh from the index, "
    "unconstrained by the unit cutoff — poetry offers no "
    "build-scoped cutoff for dependency builds (the root "
    "project's own build is unaffected: poetry builds it "
    "with its bootstrap-pinned poetry-core)"
)

PDM_SDIST_DEPENDENCY_LIMITATION = (
    "sdist-dependency-build-unconstrained: if a locked "
    "dependency ships only as an sdist (no compatible wheel "
    "in the lock), pdm 2.29.2 builds it by resolving the "
    "dependency's build backend fresh from the index, "
    "unconstrained by the unit cutoff — pdm offers no "
    "build-scoped cutoff for dependency builds (its only "
    "date cutoff is the resolution-wide strategy in the "
    "build-backend-cutoff-unavailable limitation above)"
)

# PEP 517's default build backend, used when pyproject.toml
# declares no [build-system] build-backend.
PEP517_DEFAULT_BACKEND = "setuptools.build_meta"

# Build-backend module -> distribution name, for the common
# backends (the record names the distribution, the plan's steps
# never do). Unlisted modules fall back to a [build-system]
# requires entry naming the same root, then to the module root.
_BACKEND_DISTRIBUTIONS = {
    "setuptools.build_meta": "setuptools",
    "setuptools.build_meta:__legacy__": "setuptools",
    "poetry.core.masonry.api": "poetry-core",
    "pdm.backend": "pdm-backend",
    "hatchling.build": "hatchling",
    "flit_core.buildapi": "flit_core",
    "uv_build": "uv_build",
    "scikit_build_core.build": "scikit-build-core",
    "maturin": "maturin",
    "mesonpy": "meson-python",
}


def _toml_section(text: str, header: str) -> str | None:
    """The body of one ``[header]`` section of a TOML file, or
    None. Deliberately a light scan, not a TOML parser — the same
    approach as runner._requires_python_from_toml (the product
    runs on Python 3.10, which has no stdlib tomllib)."""
    m = re.search(
        rf"^\[{re.escape(header)}\][^\n]*$", text, re.MULTILINE
    )
    if m is None:
        return None
    body = text[m.end():]
    nxt = re.search(r"^\[", body, re.MULTILINE)
    if nxt is not None:
        body = body[: nxt.start()]
    return body


def _declared_build_system(
    pyproject: Path,
) -> tuple[str | None, list[str]]:
    """A pyproject's [build-system]: (build-backend, requires
    names). (None, []) when undeclared or unreadable — PEP 517
    then defaults the backend, and the caller applies that."""
    try:
        text = pyproject.read_text(encoding="utf-8")
    except OSError:
        return None, []
    section = _toml_section(text, "build-system")
    if section is None:
        return None, []
    backend = None
    m = re.search(
        r"^build-backend\s*=\s*[\"']([^\"']+)[\"']",
        section,
        re.MULTILINE,
    )
    if m:
        backend = m.group(1).strip() or None
    requires: list[str] = []
    rm = re.search(
        r"^requires\s*=\s*\[(.*?)\]", section, re.MULTILINE | re.DOTALL
    )
    if rm:
        for raw in re.findall(r"[\"']([^\"']+)[\"']", rm.group(1)):
            name = re.split(r"[<>=!~;\[ ]", raw.strip(), maxsplit=1)[0]
            if name:
                requires.append(name.strip())
    return backend, requires


def _backend_distribution_name(
    backend: str, requires: list[str]
) -> str:
    """The distribution name for a build-backend module."""
    if backend in _BACKEND_DISTRIBUTIONS:
        return _BACKEND_DISTRIBUTIONS[backend]
    root = re.split(r"[.:]", backend, maxsplit=1)[0]
    for name in requires:
        if name.replace("-", "_").lower() == root.replace("-", "_").lower():
            return name
        if name.replace("-", "_").lower().startswith(
            root.replace("-", "_").lower()
        ):
            return name
    return root


def _pinned_requirement_version(text: str, dist: str) -> str | None:
    """The version a committed bootstrap requirements file pins
    for `dist` (a ``dist==X`` line), or None."""
    m = re.search(
        rf"^{re.escape(dist)}==([^\s;\\]+)", text, re.MULTILINE
    )
    return m.group(1) if m else None


def _uv_build_cutoff_limitation(error_name: str | None) -> str:
    """Registered wording: a committed uv plan whose build could
    not be constrained to the unit cutoff, and why."""
    if error_name is not None:
        cause = (
            f"evaluating the unit cutoff failed ({error_name})"
        )
    else:
        cause = "no unit cutoff was available for this plan"
    return (
        "build-backend-cutoff-unavailable: "
        f"{cause}, so uv's build-isolation resolution is "
        "unconstrained (UV_EXCLUDE_NEWER is not set on the "
        "sync step)"
    )


def _build_backend_plan(
    snapshot_dir: Path,
    manager: str,
    lock_cutoff: LockCutoff,
) -> dict[str, Any]:
    """WO-8.1c: the plan's build-backend facts for a committed
    manager (the InstallPlan build_* fields), per manager:

    - poetry builds the root project itself with the poetry-core
      bundled in its hash-pinned bootstrap, so the backend and
      its version are plan facts and nothing resolves at build
      time — no cutoff applies and none is missing. A locked
      sdist-only DEPENDENCY is a different case (poetry builds
      it with a freshly resolved backend): the scoped typed
      limitation POETRY_SDIST_DEPENDENCY_LIMITATION.
    - pdm resolves the declared backend fresh inside build
      isolation and offers no build-scoped cutoff (see
      PDM_BUILD_CUTOFF_LIMITATION): the typed limitation. The
      same gap applies one level down to sdist-only locked
      dependencies: PDM_SDIST_DEPENDENCY_LIMITATION as well.
    - uv resolves the declared backend fresh too, but
      UV_EXCLUDE_NEWER constrains that resolution to the unit
      cutoff on the sync step (verified against uv 0.13.0: a
      2023-06-01 cutoff built the fixture with setuptools 67.8.0
      instead of the unconstrained 84.0.0, while the frozen
      lock's own files installed regardless of the cutoff).
      The cutoff source is evaluated guarded: a committed plan
      never trips over it (the LockCutoff contract); a failure
      or an absent cutoff records the typed limitation instead.
    """
    if manager == "poetry":
        bootstrap_text, _sha = _bootstrap_requirements(
            POETRY_BOOTSTRAP_REQUIREMENTS_FILE
        )
        return {
            "build_backend": "poetry.core.masonry.api",
            "build_backend_name": "poetry-core",
            "build_backend_version": _pinned_requirement_version(
                bootstrap_text, "poetry-core"
            ),
            "build_cutoff": None,
            "build_limitations": [
                POETRY_SDIST_DEPENDENCY_LIMITATION
            ],
        }
    declared_backend, requires = _declared_build_system(
        snapshot_dir / "pyproject.toml"
    )
    backend = declared_backend or PEP517_DEFAULT_BACKEND
    base: dict[str, Any] = {
        "build_backend": backend,
        "build_backend_name": _backend_distribution_name(
            backend, requires
        ),
        "build_backend_version": None,
    }
    if manager == "pdm":
        return {
            **base,
            "build_cutoff": None,
            "build_limitations": [
                PDM_BUILD_CUTOFF_LIMITATION,
                PDM_SDIST_DEPENDENCY_LIMITATION,
            ],
        }
    # uv: constrain the build to the unit's cutoff, when one is
    # available. The evaluation is the guarded exception to the
    # committed path's laziness (see the LockCutoff comment).
    cutoff: str | None = None
    error_name: str | None = None
    try:
        value = lock_cutoff() if callable(lock_cutoff) else lock_cutoff
        cutoff = value or None
    except Exception as e:  # the cutoff source must not trip the plan
        error_name = type(e).__name__
    if cutoff is not None:
        return {**base, "build_cutoff": cutoff, "build_limitations": []}
    return {
        **base,
        "build_cutoff": None,
        "build_limitations": [_uv_build_cutoff_limitation(error_name)],
    }


def _project_name_declared(pyproject: Path) -> str | None:
    """The project's declared name: PEP 621 [project] name, or
    Poetry's [tool.poetry] name. None when undeclared/unreadable."""
    try:
        text = pyproject.read_text(encoding="utf-8")
    except OSError:
        return None
    for header in ("project", "tool.poetry"):
        section = _toml_section(text, header)
        if section is None:
            continue
        m = re.search(
            r"^name\s*=\s*[\"']([^\"']+)[\"']", section, re.MULTILINE
        )
        if m:
            return m.group(1).strip() or None
    return None


def _parse_wheel_generator(text: str) -> tuple[str, str] | None:
    """A built wheel's WHEEL Generator: (name, version), in both
    stamped forms — ``setuptools (84.0.0)`` and ``poetry-core
    2.5.0``. The Generator names the tool that actually produced
    the wheel, at the version it actually ran."""
    m = re.search(r"^Generator:\s*(.+)$", text, re.MULTILINE)
    if m is None:
        return None
    value = m.group(1).strip()
    pm = re.match(r"^(.*?)\s*\(([^()]*)\)\s*$", value)
    if pm:
        name, version = pm.group(1).strip(), pm.group(2).strip()
    else:
        parts = value.rsplit(" ", 1)
        if len(parts) != 2:
            return None
        name, version = parts[0].strip(), parts[1].strip()
    if not name or not version:
        return None
    return name, version


def build_backend_evidence(
    plan: InstallPlan, checkout_dir: Path
) -> dict[str, str] | None:
    """WO-8.1c executor evidence: the build backend that
    ACTUALLY built the root project for this plan, as
    {"name": ..., "version": ...} — read back from the built
    project's dist-info WHEEL in the checkout's .venv (its
    Generator line is stamped by the tool that ran the build,
    at the version it resolved). Falls back to the plan's own
    pinned fact when no build artifact exists but the plan
    determines the backend (poetry: no root build happens for a
    package-mode = false project, yet the backend that WOULD
    build it is the bootstrap-pinned poetry-core). None when
    the plan builds no root project (the pip paths) or nothing
    can be determined. Evidence only: never raises, never
    affects the provision's outcome."""
    if plan.build_backend is None:
        return None
    found: tuple[str, str] | None = None
    try:
        project = _project_name_declared(checkout_dir / "pyproject.toml")
        if project:
            want = re.sub(r"[-_.]+", "_", project).lower()
            venv_lib = checkout_dir / ".venv" / "lib"
            for site_packages in sorted(
                venv_lib.glob("python*/site-packages")
            ):
                for dist_info in sorted(site_packages.glob("*.dist-info")):
                    stem = dist_info.name[: -len(".dist-info")]
                    have = re.sub(r"[-_.]+", "_", stem).lower()
                    if have != want and not have.startswith(want + "_"):
                        continue
                    wheel = dist_info / "WHEEL"
                    if not wheel.is_file():
                        continue
                    found = _parse_wheel_generator(
                        wheel.read_text(encoding="utf-8")
                    )
                    if found is not None:
                        break
                if found is not None:
                    break
    except Exception:
        found = None
    if found is not None:
        return {"name": found[0], "version": found[1]}
    if plan.build_backend_name and plan.build_backend_version:
        return {
            "name": plan.build_backend_name,
            "version": plan.build_backend_version,
        }
    return None


def _manager_bootstrap_steps(manager: str) -> list[PlanStep]:
    """Bootstrap a pinned lock manager into the checkout's
    .pd-tools, BY HASH from its committed bootstrap requirements
    file (the Addendum 46 resolver pattern, generalized): a tools
    venv, the file materialized verbatim into it, then a
    hash-checking binaries-only install. A wrong or substituted
    wheel fails the install step. The binary lands at
    {checkout}/.pd-tools/bin/<manager> on every executor."""
    file_name, checkout_path, _version = _LOCK_MANAGERS[manager]
    bootstrap_text, _sha256 = _bootstrap_requirements(file_name)
    return [
        PlanStep(
            phase="install",
            argv=["{runtime_bin}", "-m", "venv", "{checkout}/.pd-tools"],
        ),
        PlanStep(
            phase="install",
            argv=[
                "{runtime_bin}",
                "-c",
                _WRITE_FILE_CODE,
                f"{{checkout}}/{checkout_path}",
                bootstrap_text,
            ],
        ),
        PlanStep(
            phase="install",
            argv=[
                "{checkout}/.pd-tools/bin/pip",
                "install",
                "--require-hashes",
                "--no-deps",
                "--only-binary",
                ":all:",
                "-r",
                f"{{checkout}}/{checkout_path}",
            ],
        ),
    ]


def _manager_frozen_steps(
    manager: str, build_cutoff: str | None = None
) -> list[PlanStep]:
    """The frozen install of a committed lock, per manager
    (REG-A item 1), preceded — where the tool's install command
    does not itself refuse a stale lock — by the tool's own
    lock-freshness check (WO-8.1b item 4: a lock out of sync with
    pyproject.toml is BUILD_FAILED carrying the tool's error
    line, never a silent install of the wrong set).

    Verified against the pinned tools (2026-10-12):
    - uv 0.13.0: `uv sync --frozen` installs a stale lock
      silently (exit 0); `uv lock --check` exits 1 with
      "error: The lockfile at `uv.lock` needs to be updated...".
    - pdm 2.29.2: `pdm sync` warns but exits 0 on a stale lock;
      `pdm lock --check` exits 1.
    - poetry 2.5.1: `poetry install --sync` itself exits 1 on a
      stale lock ("pyproject.toml changed significantly since
      poetry.lock was last generated..."), so no separate check
      step is needed — or wanted: a second check would only
      duplicate the refusal.

    WO-8.1c: when the plan carries a build cutoff (uv only — see
    _build_backend_plan), the sync step's env gains
    UV_EXCLUDE_NEWER=<cutoff>: the one place the unit's cutoff
    can reach the build of the root project. On a --frozen sync
    it constrains ONLY the fresh resolution inside build
    isolation; the lock's own files install regardless (both
    verified against uv 0.13.0, 2026-10-13).
    """
    mgr_bin = f"{{checkout}}/.pd-tools/bin/{manager}"
    if manager == "uv":
        sync_env = {"UV_PROJECT_ENVIRONMENT": "{venv}"}
        if build_cutoff is not None:
            sync_env["UV_EXCLUDE_NEWER"] = build_cutoff
        return [
            PlanStep(phase="install", argv=[mgr_bin, "lock", "--check"]),
            PlanStep(
                phase="install",
                argv=[
                    mgr_bin,
                    "sync",
                    "--frozen",
                    # The interpreter the plan selected (the host
                    # executor used to append this itself; it is
                    # plan data now, so the sandbox gets the same
                    # pin).
                    "--python",
                    "{runtime_bin}",
                ],
                # The venv is the checkout's .venv (created by the
                # plan's venv step), named explicitly so the
                # install target cannot drift with the tool's
                # project discovery.
                env=sync_env,
            ),
        ]
    if manager == "poetry":
        return [
            PlanStep(
                phase="install",
                argv=[mgr_bin, "install", "--sync"],
                # In-project venvs: the plan's venv step has
                # already created {checkout}/.venv from the
                # selected interpreter, and poetry adopts an
                # existing in-project .venv; the env var states
                # the same intent in poetry's own configuration.
                env={"POETRY_VIRTUALENVS_IN_PROJECT": "true"},
            ),
        ]
    if manager == "pdm":
        return [
            PlanStep(phase="install", argv=[mgr_bin, "lock", "--check"]),
            PlanStep(phase="install", argv=[mgr_bin, "sync"]),
        ]
    raise ValueError(f"unsupported lock manager: {manager}")


class PythonAdapter:
    """Python: REG-A — complete committed locks (uv.lock /
    poetry.lock / pdm.lock / fully hashed requirements.txt) install
    frozen; anything else is resolved into a generated hashed lock
    by the pinned uv at the unit's cutoff, then installed frozen.

    A committed manager lock (uv/poetry/pdm) plans its full frozen
    install (WO-8.1b): checkout venv from the selected interpreter,
    the pinned manager bootstrapped by hash into .pd-tools, the
    freshness check where the tool needs one, the frozen install.
    """

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
            # WO-8.1b: a committed uv.lock / poetry.lock / pdm.lock
            # installs frozen IN BOTH EXECUTORS. The plan carries
            # the whole mechanism (never a bare manager binary off
            # PATH): the checkout venv created from the selected
            # interpreter (uv is pointed at it, poetry and pdm
            # adopt an existing in-project .venv), the manager
            # bootstrapped into .pd-tools BY HASH from its
            # committed bootstrap file at its pinned version, the
            # freshness check where the tool's install command
            # does not refuse a stale lock itself, the frozen
            # install, and the sandbox venv sanity check the pip
            # plans carry.
            manager = _COMMITTED_LOCK_MANAGERS[lockfile_name]
            _file, _path, manager_version = _LOCK_MANAGERS[manager]
            _text, manager_bootstrap_sha256 = _bootstrap_requirements(
                _file
            )
            # WO-8.1c: the root project's build — the backend
            # evidence, the cutoff constraint where the tool
            # allows it (its value rides the frozen steps), and
            # the typed limitation where it does not.
            build = _build_backend_plan(
                snapshot_dir, manager, lock_cutoff
            )
            steps = [
                PlanStep(
                    phase="venv",
                    argv=["{runtime_bin}", "-m", "venv", "{venv}"],
                ),
                *_manager_bootstrap_steps(manager),
                *_manager_frozen_steps(manager, build["build_cutoff"]),
                # Sandbox-only sanity check (Addendum 15, B2), as
                # on the pip paths.
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
                package_manager=manager,
                package_manager_version=manager_version,
                manager_bootstrap_sha256=manager_bootstrap_sha256,
                build_backend=build["build_backend"],
                build_backend_name=build["build_backend_name"],
                build_backend_version=build["build_backend_version"],
                build_cutoff=build["build_cutoff"],
                build_limitations=build["build_limitations"],
                steps=steps,
                persisted_dirs=[".venv", ".pd-tools"],
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
        bootstrap_text, bootstrap_sha256 = _uv_bootstrap_requirements()
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
            "lock_resolver_bootstrap_sha256": bootstrap_sha256,
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
            # Addendum 46: the resolver is installed BY HASH from
            # the committed bootstrap requirements file. The file
            # is materialized verbatim into .pd-tools first (the
            # install container mounts only the checkout), then
            # installed in hash-checking mode, binaries only: a
            # wrong or substituted wheel fails this step.
            PlanStep(
                phase="install",
                argv=[
                    "{runtime_bin}",
                    "-c",
                    _WRITE_FILE_CODE,
                    f"{{checkout}}/{UV_BOOTSTRAP_CHECKOUT_PATH}",
                    bootstrap_text,
                ],
            ),
            PlanStep(
                phase="install",
                argv=[
                    "{checkout}/.pd-tools/bin/pip",
                    "install",
                    "--require-hashes",
                    "--no-deps",
                    "--only-binary",
                    ":all:",
                    "-r",
                    f"{{checkout}}/{UV_BOOTSTRAP_CHECKOUT_PATH}",
                ],
            ),
            # REG-A item 2: resolve the declared dependencies with
            # the pinned uv, hashes generated, for the selected
            # Python version and linux/x86_64, against the
            # registered index, with the unit's one cutoff.
            # --no-header (Addendum 46): uv's header embeds the full
            # command, including --output-file <path>, so without it
            # the recorded sha256 of the generated lock depended on
            # the checkout path — identical resolutions at fix^ and
            # fix recorded different hashes. The command itself is
            # already recorded in executed_steps.
            PlanStep(
                phase="install",
                argv=[
                    uv_bin,
                    "pip",
                    "compile",
                    f"{{checkout}}/{source_name}",
                    "--generate-hashes",
                    "--no-header",
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

    def plan_workspace(
        self,
        snapshot_dir: Path,
        contract: Any,
        lock_cutoff: LockCutoff = None,
    ) -> InstallPlan:
        """Python workspaces: a typed refusal, for now (plan 1.2b).

        No Python workspace declaration is detected yet — the
        interface's workspace routing keys off declarations only
        the Node adapter's ecosystems make — so this is the
        interface's answer when a Python workspace plan is
        requested explicitly: refuse typed, never guess a member
        or a root to install.
        """
        del contract, lock_cutoff
        det = self.detect(snapshot_dir)
        declared = det.declared_range if det is not None else None
        return InstallPlan(
            adapter=self.name,
            runtime="python",
            declared_range=declared,
            **_image_fields("python", declared),
            refusal="workspaces not yet supported for runtime 'python'",
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

# Plan 1.2e (D22): bun is pinned BY DIGEST. Bun ships as a single
# binary in a zip on the oven-sh/bun GitHub releases page; this
# table is the committed pin — bun version -> machine architecture
# -> (release artifact, sha256 of that artifact) — resolved from
# the release's own SHASUMS256.txt and verified against the
# downloaded artifacts when the rows were added (ADR-0014). The
# bootstrap step downloads the artifact into the checkout's
# .pd-tools, verifies the sha256 before unpacking, and installs
# the binary at BUN_BIN: the Addendum 35 persisted-tools pattern,
# like the corepack bootstrap above and the Python managers'
# hash-pinned bootstrap requirements files. A bun version with no
# row here is REFUSED at plan time (typed) — there is no unpinned
# bun install, and no fallback to another manager.
BUN_RELEASE_SHA256: dict[str, dict[str, tuple[str, str]]] = {
    "1.4.3": {
        "x86_64": (
            "bun-linux-x64.zip",
            "3e730528d89775f03f87f05c06787c4df4099d0a37f3f252e7b829cb23816e18",
        ),
        "aarch64": (
            "bun-linux-aarch64.zip",
            "efa9813da5ed72423bf847f916e8d2c47c0d776add972354026a75e10da9aa21",
        ),
    },
    "1.3.14": {
        "x86_64": (
            "bun-linux-x64.zip",
            "951ee2aee855f08595aeec6225226a298d3fea83a3dcd6465c09cbccdf7e848f",
        ),
        "aarch64": (
            "bun-linux-aarch64.zip",
            "a27ffb63a8310375836e0d6f668ae17fa8d8d18b88c37c821c65331973a19a3b",
        ),
    },
    "1.2.19": {
        "x86_64": (
            "bun-linux-x64.zip",
            "c3d3c14e9a5ec83ff67d0acfe76e4315ad06da9f34f59fc7b13813782caf1f66",
        ),
        "aarch64": (
            "bun-linux-aarch64.zip",
            "fcfd471cdbd5a78fd4a390e29cccd2bb7004a49d352ba037af3b61d4fd5d0b83",
        ),
    },
}

# The bun version a bun.lock repo gets when its packageManager
# field does not pin one: the newest row of the pin table. The
# selected version is recorded on the plan either way (D22).
BUN_DEFAULT_VERSION = "1.4.3"
BUN_BIN = "{checkout}/.pd-tools/bin/bun"
_BUN_RELEASE_URL = (
    "https://github.com/oven-sh/bun/releases/download/"
    "bun-v{version}/{artifact}"
)


def _bun_bootstrap_step(
    version: str, artifact: str, sha256: str
) -> PlanStep:
    """The persisted bun bootstrap (plan 1.2e): one ``bash -c``
    argv step (the shape of the Python managers' ``-c`` code
    steps) that downloads the pinned release zip into the
    checkout's .pd-tools, verifies its sha256 against the
    committed pin, and unpacks the binary to BUN_BIN. Any failure
    (download, digest mismatch, unpack) fails the step: the
    frozen install never runs against an unverified bun.
    Non-interactive throughout (curl -f, unzip -o). When the
    sandbox's intercepting proxy is in play the plan's CA
    (SSL_CERT_FILE, set by the executor's install env) is handed
    to curl explicitly; on a plain network it rides the image's
    own trust store.
    """
    url = _BUN_RELEASE_URL.format(version=version, artifact=artifact)
    bindir = artifact.removesuffix(".zip")
    script = (
        "set -e; "
        "d={checkout}/.pd-tools; "
        "mkdir -p $d/bin $d/.bun-dist; "
        'cacert=""; '
        'if [ -n "${SSL_CERT_FILE:-}" ] && [ -f "$SSL_CERT_FILE" ]; '
        'then cacert="--cacert $SSL_CERT_FILE"; fi; '
        f"curl -fsSL $cacert {url} -o $d/bun.zip; "
        f'echo "{sha256}  $d/bun.zip" | sha256sum -c -; '
        "unzip -o -q $d/bun.zip -d $d/.bun-dist; "
        f"cp $d/.bun-dist/{bindir}/bun $d/bin/bun; "
        "chmod +x $d/bin/bun; "
        "rm -rf $d/bun.zip $d/.bun-dist; "
        "$d/bin/bun --version"
    )
    return PlanStep(
        phase="install",
        argv=["bash", "-c", script],
        env=dict(NODE_INSTALL_ENV),
    )


class NodeAdapter:
    """Node: frozen installs planned from the lockfile.

    Detection/planning rules (WO-7 PR 2):

    - ``package-lock.json`` -> ``npm ci``; ``pnpm-lock.yaml`` ->
      ``pnpm install --frozen-lockfile``; ``yarn.lock`` -> ``yarn
      install --frozen-lockfile`` (classic) or ``--immutable``
      (berry), via runner.select_package_manager.
    - ``bun.lock`` -> ``bun install --frozen-lockfile`` (plan
      1.2e, D22), run by a bun binary the plan itself bootstraps
      into the checkout's .pd-tools, pinned BY DIGEST
      (BUN_RELEASE_SHA256): the version comes from the app's
      ``packageManager`` pin when declared, else the pin table's
      default, and the selected version + artifact digest are
      recorded on the plan. A bun version with no digest pin is a
      typed refusal. ``bun.lockb`` (the legacy binary lockfile)
      is a typed refusal: only the text ``bun.lock`` is planned.
    - A ``packageManager`` pin renders the install through corepack
      at the exact pinned version (deterministically, as plan data —
      never dependent on the planning host's own corepack), with
      ``COREPACK_ENABLE_DOWNLOAD_PROMPT=0`` in the step env; the
      pinned version is recorded on the plan.
    - A ``packageManager`` that disagrees with the lockfile present
      (it pins pnpm, only package-lock.json exists) is a typed
      refusal — never a silent fall-back to the other manager.
    - Monorepo workspaces (a ``workspaces`` field in package.json,
      or a ``pnpm-workspace.yaml``) are planned by
      :meth:`plan_workspace` (plan 1.2b): the frozen install runs
      at the workspace root and the contract steps are scoped to
      the resolved app member. Ambiguity (several members, no
      contract ``app.path``) and unsupported workspace managers
      are typed refusals.
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
        # Monorepo workspaces (plan 1.2b): planned by the
        # workspace-aware method — frozen install at the root,
        # contract steps scoped to the resolved app member.
        if self._declares_workspaces(snapshot_dir):
            return self.plan_workspace(snapshot_dir, contract)
        return self._install_plan(snapshot_dir, base, contract, app_path=None)

    def plan_workspace(
        self,
        snapshot_dir: Path,
        contract: Any,
        lock_cutoff: LockCutoff = None,
    ) -> InstallPlan:
        """Plan a Node workspace (monorepo) snapshot (plan 1.2b).

        The app member is resolved first (contract ``app.path``,
        else the single member, else the root; several members
        with no declaration is the typed ambiguity refusal), then
        the plan is the root plan: the root's lockfile and
        ``packageManager`` pin select the manager exactly as for
        a single-package repo, the frozen install runs at the
        root, and the contract steps are scoped to the member.
        Managers without workspace support here (yarn) are a
        typed refusal naming the manager; bun joined the
        supported set under plan 1.2e, through this same method
        and with no interface change.
        """
        # Node never generates a lock; the cutoff is ignored (and
        # never evaluated), exactly as in plan().
        del lock_cutoff
        det = self.detect(snapshot_dir)
        assert det is not None, (
            "plan_workspace() called without a node detection"
        )
        image = _image_fields("node", det.declared_range)
        base: dict[str, Any] = {
            "adapter": self.name,
            "runtime": "node",
            "declared_range": det.declared_range,
            **image,
        }
        layout = detect_workspace(snapshot_dir)
        if layout is None:
            # Routed here on a declaration this module could not
            # resolve into a layout: refuse typed, never guess.
            return InstallPlan(**base, refusal="workspaces not yet supported")
        app_path, refusal = _resolve_workspace_app(
            layout, getattr(contract, "app_path", None)
        )
        if refusal is not None:
            return InstallPlan(**base, refusal=refusal)
        assert app_path is not None
        return self._install_plan(
            snapshot_dir, base, contract, app_path=app_path
        )

    def _install_plan(
        self,
        snapshot_dir: Path,
        base: dict[str, Any],
        contract: Any,
        *,
        app_path: str | None,
    ) -> InstallPlan:
        """The Node install plan from the root's lockfile.

        Shared by plan() (``app_path`` None: a single-package repo)
        and plan_workspace() (``app_path`` the resolved member, or
        ``"."`` for a root app): manager selection, the
        packageManager-mismatch and no-lockfile refusals, the
        corepack-pinned rendering and the frozen install step are
        identical; a workspace plan additionally scopes the
        contract steps to the member and records the workspace
        facts on the plan.
        """
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
        if lockfile_name == "bun.lockb":
            # Plan 1.2e (D22): the legacy BINARY bun lockfile is
            # refused, typed. Equivalence with the text bun.lock
            # cannot be proven from the plan's side (the format is
            # opaque and version-stamped by the bun that wrote it),
            # so there is no frozen install from it — never a
            # silent fall-back and never a generated lockfile.
            return InstallPlan(
                **base,
                refusal=(
                    "bun.lockb (the legacy binary bun lockfile) is "
                    "not supported: bun installs are planned from "
                    "bun.lock only — regenerate the lockfile with "
                    "a current bun and commit bun.lock"
                ),
            )
        manager, argv = select_package_manager(
            snapshot_dir, "node", lockfile_name
        )
        if app_path is not None and manager not in ("pnpm", "npm", "bun"):
            # Workspace support is implemented for pnpm and npm
            # (plan 1.2b) and bun (plan 1.2e); another manager's
            # workspace is a typed refusal naming it — never a
            # silent fall-back.
            return InstallPlan(
                **base,
                refusal=(
                    "workspaces not yet supported for package "
                    f"manager {manager!r}"
                ),
            )
        # Addendum 37: EVERY Node install step carries the nodedir
        # env (compile fallback against the image's own headers).
        step_env: dict[str, str] = dict(NODE_INSTALL_ENV)
        bun_version: str | None = None
        bun_sha256: str | None = None
        bun_bootstrap: PlanStep | None = None
        if manager == "bun":
            # Plan 1.2e (D22): bun is not on the pinned Node image
            # and is not corepack-distributed, so the plan
            # bootstraps it into the checkout's .pd-tools, pinned
            # by digest. The version aligns to the app: its own
            # packageManager pin when declared, else the pin
            # table's default. No digest pin for the selected
            # version is a typed refusal — bun is never installed
            # unpinned, and the frozen install never falls back
            # to another manager.
            bun_version = (
                pm_version if pm_name == "bun" else None
            ) or BUN_DEFAULT_VERSION
            row = BUN_RELEASE_SHA256.get(bun_version)
            if row is None:
                return InstallPlan(
                    **base,
                    refusal=(
                        f"no pinned digest for bun@{bun_version}: "
                        "bun is pinned by digest (BUN_RELEASE_SHA256) "
                        "and this version has no pin; refusing to "
                        "install an unpinned bun"
                    ),
                )
            release = row.get(platform.machine())
            if release is None:
                return InstallPlan(
                    **base,
                    refusal=(
                        f"no pinned bun build for architecture "
                        f"{platform.machine()!r} (bun@{bun_version})"
                    ),
                )
            artifact, bun_sha256 = release
            bun_bootstrap = _bun_bootstrap_step(
                bun_version, artifact, bun_sha256
            )
            argv = [BUN_BIN, "install", "--frozen-lockfile"]
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
            # -g. COREPACK_HOME rides on HOME (the sandbox's
            # per-side /pd-home since plan 1.2(k); the host's own
            # HOME on the host executor), so the downloaded manager
            # survives to later steps with the network cut.
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
        if bun_bootstrap is not None:
            # Plan 1.2e: the digest-pinned bun bootstrap precedes
            # the frozen install, in persisted (.pd-tools) form,
            # exactly like the corepack bootstrap above.
            steps.append(bun_bootstrap)
        steps.append(PlanStep(phase="install", argv=argv, env=step_env))
        # Plan 1.2b: a workspace plan scopes the contract steps to
        # the app member (cwd relative to the root; "." and None
        # both keep the steps at the root, as before).
        step_cwd = app_path if app_path not in (None, ".") else None
        steps.extend(_contract_steps(contract, cwd=step_cwd))
        persisted = ["node_modules"]
        if step_cwd is not None:
            # The member's own node_modules (pnpm links the
            # member's dependencies there) persists alongside the
            # root's.
            persisted.append(f"{step_cwd}/node_modules")
        if corepack_version or bun_bootstrap is not None:
            persisted.append(".pd-tools")
        return InstallPlan(
            **base,
            lockfile_name=lockfile_name,
            lockfile_sha256=lockfile_sha,
            package_manager=manager,
            package_manager_version=(
                pm_version if pm_name == manager else None
            ),
            corepack_version=corepack_version,
            bun_version=bun_version,
            bun_sha256=bun_sha256,
            workspace_root="." if app_path is not None else None,
            app_path=app_path,
            steps=steps,
            persisted_dirs=persisted,
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
