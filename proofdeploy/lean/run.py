"""Lean P4: provisioning drivers and the ``lean-verify`` entry point.

Two kinds of side driver (see ``pipeline.SideDriver``):

* :class:`SandboxSide` (default): the side is provisioned by the measured
  path's own method, ``Orchestrator._provision_side_sandbox`` (rootless
  userns, network cut after install, only the checkout and the per-side
  ``/pd-home`` mounted). pdcov is injected through a ``Sandbox`` subclass
  (:func:`make_lean_sandbox`) that the orchestrator is handed for the
  duration of the provision; ``orchestrator.py`` itself is not changed.
* :class:`HostSide` (``--no-sandbox``): dev and golden tests only. The app
  runs on this machine through ``lean/hostrun``-style process handling.
  The run is marked unsandboxed and is never a measured result.

pdcov in the sandbox (from the P2 notes):

* the tool dir is ``/pd-home/.pd-tools/pdcov`` (the per-side home). It is
  staged on the host BEFORE the first install step, so the sandbox's
  ownership transfer (``podman unshare chown``) covers it; a last install
  step creates ``data/`` and makes the tool dir writable for the host
  user (mode 0777: the host writes the ``context``/``flush`` control files
  and reads the acks and data files across the userns boundary), and puts
  ``pdcov.pth`` into the app venv's purelib.
* ``COVERAGE_PROCESS_START`` is set only on the app containers that run
  measured; the install steps and the replays never see it.
* the coveragerc ``source`` is the app root as the app sees it
  (``/checkout`` or ``/checkout/<member>`` for a monorepo); ``root_prefix``
  maps coverage paths back to repo-relative paths.

Each replay run is a FRESH app (new container or process) on a database
restored from the snapshot taken right after seed.
"""

from __future__ import annotations

import ast
import contextlib
import hashlib
import json
import os
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
import traceback
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest import mock

from proofdeploy.lean import coverage as lc
from proofdeploy.lean import hostrun
from proofdeploy.lean.coverage import CoverageSession, PdCov
from proofdeploy.lean.index import FLASK_DUMP_SNIPPET
from proofdeploy.lean.pipeline import (
    LeanConfig,
    LeanEnvironmentError,
    LeanRefusal,
    LeanResult,
    SideDriver,
    Timings,
    dumps,
    environment_result,
    not_observable_result,
    resolve_sha,
    run_pipeline,
)
from proofdeploy.lean.verdict import FAIL, INCONCLUSIVE, PASS, Verdict
from proofdeploy.model_client import ModelRunner
from proofdeploy.yml import RepoContract, contract_store_path, load_contract

# Exit codes, as ``verify``.
EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_INCONCLUSIVE = 2
EXIT_NOT_SUPPORTED = 3
EXIT_USAGE = 4
EXIT_INTERNAL = 5

_VERDICT_EXIT = {PASS: EXIT_PASS, FAIL: EXIT_FAIL, INCONCLUSIVE: EXIT_INCONCLUSIVE}

CONTAINER_TOOL_DIR = lc.SANDBOX_TOOL_DIR
CONTAINER_OVERLAY_DIR = "/pd-home/.pd-tools/overlay"
APP_VENV_PYTHON = "/pd-home/.venv/bin/python"
NOT_PYTHON_EXIT = 92
FREEZE_TIME = "2020-01-01 00:00:00"
FAKETIME_LIB = "/usr/lib/x86_64-linux-gnu/faketime/libfaketime.so.1"
_MAX_LOG_BYTES = 4_000_000


class LeanUsageError(Exception):
    """Bad arguments or inputs (exit 4)."""


# --------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------


def parse_overlay_items(items: list[str]) -> dict[str, Any]:
    """``KEY=VALUE`` strings -> overrides; VALUE is a Python literal, else a string."""
    out: dict[str, Any] = {}
    for item in items:
        key, sep, raw = item.partition("=")
        if not sep or not key.strip():
            raise LeanUsageError(f"--django-overlay needs KEY=VALUE, got {item!r}")
        try:
            value = ast.literal_eval(raw)
        except (ValueError, SyntaxError):
            value = raw
        out[key.strip()] = value
    return out


def overlay_env(
    contract_env: dict[str, str],
    overrides: dict[str, Any],
    write_dir: Path,
    env_dir: str,
) -> dict[str, str]:
    """Env that points a Django app at the settings overlay; {} when not applicable.

    Applied only when the contract declares a Django app
    (``DJANGO_SETTINGS_MODULE`` in its env) and overrides were requested.
    ``write_dir`` is where the overlay module is written (host path);
    ``env_dir`` is the same directory as the app sees it.
    """
    base = contract_env.get("DJANGO_SETTINGS_MODULE")
    if not overrides or not base:
        return {}
    module = lc.write_django_settings_overlay(write_dir, base, overrides)
    existing = contract_env.get("PYTHONPATH")
    return {
        "DJANGO_SETTINGS_MODULE": module,
        "PYTHONPATH": env_dir + (os.pathsep + existing if existing else ""),
    }


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_text_capped(path: Path) -> str:
    try:
        data = path.read_bytes()[-_MAX_LOG_BYTES:]
    except OSError:
        return ""
    return data.decode("utf-8", errors="replace")


def snapshot_sqlite(src: Path, dest: Path) -> None:
    """Copy a sqlite database consistently (backup API; plain copy as fallback)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        with contextlib.closing(sqlite3.connect(f"file:{src}?mode=ro", uri=True)) as s:
            with contextlib.closing(sqlite3.connect(str(dest))) as d:
                s.backup(d)
    except sqlite3.Error:
        shutil.copyfile(src, dest)


def restore_sqlite(snapshot: Path, target: Path) -> None:
    """Put ``snapshot`` at ``target`` atomically, dropping stale sidecar files.

    A rename inside the directory works whoever owns ``target`` (in the
    sandbox the previous database file belongs to the userns-mapped uid),
    so the new file is made world-writable for the app user.
    """
    tmp = target.with_name(target.name + ".pd-restore")
    shutil.copyfile(snapshot, tmp)
    os.chmod(tmp, 0o666)
    os.replace(tmp, target)
    for suffix in ("-wal", "-shm", "-journal"):
        with contextlib.suppress(OSError):
            target.with_name(target.name + suffix).unlink()


def db_file_of(app_dir: Path, contract: RepoContract) -> Path:
    db = contract.database
    if not db or db.get("kind") != "sqlite" or not db.get("path"):
        raise LeanRefusal(
            "db_snapshot_unsupported",
            "the contract declares no sqlite database; the lean path restores a fresh "
            "database for every run and can only snapshot sqlite",
        )
    path = (app_dir / str(db["path"])).resolve()
    if not path.is_relative_to(app_dir.resolve()):
        raise LeanRefusal("db_snapshot_unsupported", "database.path leaves the app directory")
    return path


def wrap_clock_cmd(command: str) -> tuple[str, dict[str, Any]]:
    """Freeze the clock with libfaketime's ``faketime`` binary when it is installed."""
    exe = shutil.which("faketime")
    if exe is None:
        return command, {"used": False, "reason": "faketime not available"}
    return f"{shlex.quote(exe)} -f {shlex.quote(FREEZE_TIME)} bash -c {shlex.quote(command)}", {
        "used": True,
        "how": "faketime",
        "time": FREEZE_TIME,
    }


_DETERMINISTIC_ENV = {"PYTHONHASHSEED": "0", "TZ": "UTC"}


# --------------------------------------------------------------------------
# Host driver (--no-sandbox)
# --------------------------------------------------------------------------


@dataclass
class HostLive:
    base_url: str
    session: CoverageSession | None
    proc: subprocess.Popen[bytes]
    log: Path

    def server_log(self) -> str:
        return read_text_capped(self.log)

    def stop(self) -> None:
        if self.proc.poll() is None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(self.proc.pid, signal.SIGTERM)
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(self.proc.pid, signal.SIGKILL)
                self.proc.wait()


class HostSide:
    """One side on this machine: dev and golden use only (never a measured result)."""

    def __init__(
        self,
        *,
        label: str,
        sha: str,
        tree: Path,
        py: Path,
        contract: RepoContract,
        work: Path,
        overlay: dict[str, Any] | None = None,
    ) -> None:
        self.label = label
        self.sha = sha
        self.tree = tree
        self.py = py
        self.contract = contract
        self.work = work
        member = (contract.app_path or "").strip("/")
        member = "" if member == "." else member
        self.app_dir = tree / member if member else tree
        self.root_prefix = member
        self.tool_dir = work / "pdcov"
        self.pd = PdCov(self.tool_dir)
        self.seed_db = work / "seed.sqlite"
        self.db = db_file_of(self.app_dir, contract)
        self._env = {**contract.env}
        self._overlay = overlay_env(
            contract.env, overlay or {}, work / "overlay", str(work / "overlay")
        )
        self._starts = 0
        self._clock: dict[str, Any] = {"used": False}
        self._pth_ready = False

    def _base_env(self, extra: dict[str, str] | None = None, *, measured: bool) -> dict[str, str]:
        path = f"{self.py.parent}{os.pathsep}{os.environ.get('PATH', '')}"
        merged = {**self._env, **self._overlay, **_DETERMINISTIC_ENV, "PATH": path, **(extra or {})}
        return lc.clean_app_env(self.pd, merged, measured=measured)

    def _run_step(self, command: str | None, what: str) -> None:
        if not command:
            return
        proc = subprocess.run(
            ["bash", "-c", command],
            cwd=self.app_dir,
            env=self._base_env(measured=False),
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
        if proc.returncode != 0:
            tail = (proc.stdout + proc.stderr)[-600:]
            raise LeanEnvironmentError(
                "provisioning_failed", f"{self.label} {what} exited {proc.returncode}: {tail}"
            )

    def prepare(self) -> None:
        """build, migrate, seed (unmeasured), install pdcov, snapshot the seeded database."""
        try:
            lc.check_interpreter_compatible(self.py)
        except lc.CoverageHarnessError as exc:
            raise LeanEnvironmentError("pdcov_injection_failed", str(exc)) from exc
        lc.install_pdcov(self.tool_dir, self.app_dir)
        for what, cmd in (
            ("build", self.contract.build),
            ("migrate", self.contract.migrate),
            ("seed", self.contract.seed),
        ):
            self._run_step(cmd, what)
        if not self.db.is_file():
            raise LeanRefusal(
                "db_snapshot_unsupported",
                f"no sqlite file at {self.contract.database and self.contract.database['path']}"
                " after migrate and seed",
            )
        snapshot_sqlite(self.db, self.seed_db)

    def start(self, *, measured: bool) -> HostLive:
        restore_sqlite(self.seed_db, self.db)
        self._starts += 1
        port = hostrun.free_port()
        session: CoverageSession | None = None
        if measured:
            lc.install_pth(lc.app_site_packages(self.py), self.pd)
            session = CoverageSession(self.pd, ack_timeout=10.0)
            session.start()
        env = self._base_env({"PORT": str(port)}, measured=measured)
        cmd, self._clock = wrap_clock_cmd(self.contract.start)
        log = self.work / f"app-{self._starts}.log"
        with log.open("wb") as logf:
            proc = subprocess.Popen(
                ["bash", "-c", cmd],
                cwd=self.app_dir,
                env=env,
                stdout=logf,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        live = HostLive(f"http://127.0.0.1:{port}", session, proc, log)
        try:
            hostrun.wait_http(port, self.contract.readiness or "/health")
        except AssertionError as exc:
            tail = live.server_log()[-600:]
            live.stop()
            raise LeanEnvironmentError(
                "app_not_ready", f"{self.label}: {exc}; app output tail: {tail}"
            ) from exc
        return live

    def flask_rules(self, app: Any, target: str) -> list[dict[str, Any]] | None:
        try:
            return hostrun.flask_url_map(self.py, self.app_dir, target)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError):
            return None

    def describe(self) -> dict[str, Any]:
        return {
            "mode": "host",
            "sandboxed": False,
            "tree": str(self.tree),
            "db_snapshot_sha256": _sha256_file(self.seed_db) if self.seed_db.is_file() else None,
            "starts": self._starts,
            "frozen_clock": self._clock,
            "env": {"PYTHONHASHSEED": "0", "TZ": "UTC"},
        }

    def close(self) -> None:
        return None


def _ensure_python(tree: Path, venv: Path, given: Path | None) -> Path:
    if given is not None:
        return given
    req = tree / "requirements.txt"
    if not req.is_file():
        raise LeanRefusal(
            "host_mode_unsupported",
            "--no-sandbox builds a venv from a hash-pinned requirements.txt; none found "
            "(pass --python PATH for an interpreter that already has the app's dependencies)",
        )
    try:
        return hostrun.make_venv(venv, req)
    except subprocess.CalledProcessError as exc:
        raise LeanEnvironmentError("provisioning_failed", f"venv install failed: {exc}") from exc


def provision_host_sides(
    rc: LeanRunConfig, contract_file: Path | None, work: Path, timings: Timings
) -> tuple[HostSide, HostSide, RepoContract]:
    from proofdeploy.fixture import create_snapshot

    sides: dict[str, HostSide] = {}
    contracts: dict[str, RepoContract] = {}
    for label, sha in (("base", rc.base_sha), ("head", rc.head_sha)):
        with timings.stage(f"provision_{label}"):
            side_work = work / label
            side_work.mkdir(parents=True, exist_ok=True)
            tree = create_snapshot(
                repo_dir=rc.repo, commit_rev=sha, output_dir=side_work / "tree"
            ).dir
            try:
                contract = load_contract(contract_file or tree / "proofdeploy.yml")
            except (OSError, ValueError) as exc:
                raise LeanEnvironmentError("contract_missing", f"{label}: {exc}") from exc
            py = _ensure_python(tree, side_work / "venv", rc.python)
            side = HostSide(
                label=label,
                sha=sha,
                tree=tree,
                py=py,
                contract=contract,
                work=side_work,
                overlay=rc.django_overlay,
            )
            side.prepare()
            sides[label] = side
            contracts[label] = contract
    return sides["base"], sides["head"], contracts["head"]


# --------------------------------------------------------------------------
# Sandbox driver
# --------------------------------------------------------------------------


class SandboxHook:
    """State shared between one side's ``LeanSandbox`` and its driver."""

    def __init__(
        self,
        *,
        overlay: dict[str, Any],
        contract_env: dict[str, str] | None,
        tool_container: str = CONTAINER_TOOL_DIR,
    ) -> None:
        self.overlay = overlay
        self.contract_env = contract_env
        self.tool_container = tool_container
        self.host_tool: Path | None = None
        self.app_cwd = ""
        self.sandbox: Any = None
        self.start_args: dict[str, Any] | None = None
        self.overlay_env: dict[str, str] = {}
        self.pdcov_rc: int | None = None
        self.pdcov_output: str = ""
        self.clock_env: dict[str, str] = {}

    # -- staging (host side, before the first install container) ------------

    def stage(self, checkout: Path, steps: list[dict[str, Any]]) -> None:
        from proofdeploy.sandbox import sandbox_home_dir

        home = sandbox_home_dir(checkout)
        self.host_tool = home / ".pd-tools" / "pdcov"
        self.app_cwd = next((str(s["cwd"]) for s in steps if s.get("cwd")), "")
        source = "/checkout" + (f"/{self.app_cwd}" if self.app_cwd else "")
        tool = self.host_tool
        (tool / "data").mkdir(parents=True, exist_ok=True)
        (tool / "lib").mkdir(exist_ok=True)
        container = Path(self.tool_container)
        (tool / "coveragerc").write_text(lc.render_coveragerc(container, source), encoding="utf-8")
        shutil.copyfile(Path(lc.__file__).with_name("pdcov_startup.py"), tool / "pdcov_startup.py")
        dest = tool / "lib" / "coverage"
        if dest.exists():
            shutil.rmtree(dest)
        import coverage

        shutil.copytree(
            Path(coverage.__file__).resolve().parent,
            dest,
            ignore=shutil.ignore_patterns("__pycache__"),
        )
        (tool / "pdcov.pth.src").write_text(PdCov(container).pth_line(), encoding="utf-8")
        env = self.contract_env
        if env is None:
            try:
                env = load_contract(checkout / "proofdeploy.yml").env
            except (OSError, ValueError):
                env = {}
        self.overlay_env = overlay_env(
            env,
            self.overlay,
            home / ".pd-tools" / "overlay",
            CONTAINER_OVERLAY_DIR,
        )

    def setup_command(self) -> str:
        t = self.tool_container
        return "\n".join(
            [
                "set -eu",
                f"TOOL={shlex.quote(t)}",
                f"PY={shlex.quote(APP_VENV_PYTHON)}",
                f'if [ ! -x "$PY" ]; then echo "pdcov: no app interpreter at $PY '
                f'(not a Python app?)" >&2; exit {NOT_PYTHON_EXIT}; fi',
                'SP=$("$PY" -c \'import sysconfig; print(sysconfig.get_paths()["purelib"])\')',
                'mkdir -p "$TOOL/data"',
                'chmod 0777 "$TOOL" "$TOOL/data" 2>/dev/null || true',
                'chmod -R a+rX "$TOOL" 2>/dev/null || true',
                'cp "$TOOL/pdcov.pth.src" "$SP/pdcov.pth"',
                '"$PY" -c "import sys; sys.path.append(\'$TOOL/lib\'); import coverage; '
                "print('pdcov: coverage', coverage.__version__, "
                "'c-tracer', getattr(coverage.collector, 'CTracer', None) is not None)\"",
            ]
        )

    def finish(
        self, results: list[dict[str, Any]], n_orig: int, sandbox: Any
    ) -> list[dict[str, Any]]:
        """Drop pdcov's own step result; a failed pdcov step fails the install."""
        if len(results) != n_orig + 1:
            return results  # an earlier step failed; pdcov never ran
        mine = results[-1]
        self.pdcov_rc = int(mine["exit_code"])
        trimmed = results[:-1]
        if self.pdcov_rc != 0:
            self.pdcov_output = str(getattr(sandbox, "failed_step_output", "") or "")
            trimmed[-1] = {**trimmed[-1], "exit_code": self.pdcov_rc}
        return trimmed

    # -- app start environment ---------------------------------------------------

    def start_env(self, env: dict[str, str], measured: bool) -> dict[str, str]:
        out = dict(env)
        out.update(_DETERMINISTIC_ENV)
        out.update(self.overlay_env)
        out.update(self.clock_env)
        if measured:
            out["COVERAGE_PROCESS_START"] = f"{self.tool_container}/coveragerc"
        else:
            out.pop("COVERAGE_PROCESS_START", None)
        return out

    def restart(self, measured: bool, name: str) -> Any:
        from proofdeploy.sandbox import Sandbox

        a = self.start_args
        assert a is not None and self.sandbox is not None, "the side was never started"
        return Sandbox.start(
            self.sandbox,
            a["checkout"],
            a["command"],
            self.start_env(a["env"], measured),
            a["port"],
            name,
            app_port=a["app_port"],
        )


def make_lean_sandbox(hook: SandboxHook) -> type:
    """A ``Sandbox`` subclass that stages and activates pdcov (see the module docstring)."""
    from proofdeploy.sandbox import Sandbox

    class LeanSandbox(Sandbox):  # type: ignore[misc]
        def install_steps(
            self,
            checkout: Path,
            steps: list[dict],
            env: dict[str, str],
            proxy_env: dict[str, str],
            timeout: int = 600,
        ) -> list[dict]:
            hook.sandbox = self
            hook.stage(checkout, steps)
            extra = {"phase": "pdcov", "command": hook.setup_command(), "env": {}}
            results = super().install_steps(checkout, [*steps, extra], env, proxy_env, timeout)
            return hook.finish(results, len(steps), self)

        def start(  # type: ignore[no-untyped-def,override]
            self, checkout, command, env, port, name, *, app_port=None
        ):
            hook.sandbox = self
            hook.start_args = {
                "checkout": checkout,
                "command": command,
                "env": dict(env),
                "port": port,
                "app_port": app_port,
            }
            return super().start(
                checkout,
                command,
                hook.start_env(env, True),
                port,
                name,
                app_port=app_port,
            )

    return LeanSandbox


def container_log(name: str) -> str:
    from proofdeploy.sandbox import _podman_base

    try:
        proc = subprocess.run(
            _podman_base() + ["logs", name], capture_output=True, text=True, timeout=60, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return ((proc.stdout or "") + (proc.stderr or ""))[-_MAX_LOG_BYTES:]


@dataclass
class SandboxLive:
    app: Any  # SandboxApp
    base_url: str
    session: CoverageSession | None

    @property
    def container(self) -> str:
        return str(self.app.name)

    def server_log(self) -> str:
        return container_log(self.container)

    def stop(self) -> None:
        self.app.stop()


class SandboxSide:
    """A READY sandboxed side that can start fresh instances on the seed snapshot."""

    def __init__(
        self,
        *,
        label: str,
        side: Any,  # orchestrator._Side
        hook: SandboxHook,
        work: Path,
    ) -> None:
        self.label = label
        self.sha: str = side.sha
        self.side = side
        self.hook = hook
        self.contract: RepoContract = side.contract
        self.work = work
        self.root_prefix = hook.app_cwd.strip("/")
        assert hook.host_tool is not None
        self.pd = PdCov(hook.host_tool)
        self.snapshot_dir: Path = side.snapshot_dir
        self.app_dir = self.snapshot_dir / hook.app_cwd if hook.app_cwd else self.snapshot_dir
        self.seed_db = work / "seed.sqlite"
        self._ready: Any = side.provision.proc  # the app that provisioning left running
        self._current: SandboxLive | None = None
        self._starts = 0
        self.db = db_file_of(self.app_dir, self.contract)
        self._clock: dict[str, Any] = {"used": False, "reason": "libfaketime not in the image"}

    def prepare(self) -> None:
        """Snapshot the seeded database; probe for libfaketime in the image."""
        if not self.db.is_file():
            raise LeanRefusal(
                "db_snapshot_unsupported",
                f"no sqlite file at {self.contract.database and self.contract.database['path']} "
                "after the seed",
            )
        self.work.mkdir(parents=True, exist_ok=True)
        snapshot_sqlite(self.db, self.seed_db)
        if self._ready is not None and self._probe_faketime(str(self._ready.name)):
            self.hook.clock_env = {"LD_PRELOAD": FAKETIME_LIB, "FAKETIME": FREEZE_TIME}
            self._clock = {"used": True, "how": "libfaketime", "time": FREEZE_TIME}

    @staticmethod
    def _probe_faketime(container: str) -> bool:
        from proofdeploy.sandbox import _podman_base

        try:
            proc = subprocess.run(
                _podman_base() + ["exec", container, "test", "-e", FAKETIME_LIB],
                capture_output=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return proc.returncode == 0

    def _stop_current(self) -> None:
        if self._current is not None:
            self._current.stop()
            self._current = None

    def start(self, *, measured: bool) -> SandboxLive:
        self._stop_current()
        readiness = self.contract.readiness or "/health"
        if measured and self._ready is not None:
            app, self._ready = self._ready, None  # untouched since seed: adopt it
        else:
            if self._ready is not None:
                self._ready.stop()
                self._ready = None
            restore_sqlite(self.seed_db, self.db)
            self._starts += 1
            name = f"pd-lean-{self.label}-{self.sha[:8]}-{self._starts}-{int(time.time())}"
            app = self.hook.restart(measured, name)
            if not app.wait_ready(readiness, timeout=120):
                log = container_log(str(app.name))[-800:]
                app.stop()
                raise LeanEnvironmentError(
                    "app_not_ready", f"{self.label}: fresh instance not ready; log tail: {log}"
                )
        session: CoverageSession | None = None
        if measured:
            session = CoverageSession(self.pd, ack_timeout=10.0)
            session.start()
        live = SandboxLive(app=app, base_url=str(app.target_url), session=session)
        self._current = live
        return live

    def flask_rules(self, app: Any, target: str) -> list[dict[str, Any]] | None:
        from proofdeploy.sandbox import _podman_base

        cwd = "/checkout" + (f"/{self.hook.app_cwd}" if self.hook.app_cwd else "")
        argv = _podman_base() + [
            "exec",
            "-w",
            cwd,
            "-e",
            "COVERAGE_PROCESS_START=",
            str(app.container),
            APP_VENV_PYTHON,
            "-c",
            FLASK_DUMP_SNIPPET,
            target,
        ]
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=90, check=False)
            if proc.returncode != 0:
                return None
            rules = json.loads(proc.stdout)
        except (OSError, subprocess.TimeoutExpired, ValueError):
            return None
        return rules if isinstance(rules, list) else None

    def bootstrap_env(self) -> dict[str, str]:
        """The generated bootstrap values this side was provisioned with (secrets)."""
        return dict(getattr(self.side, "bootstrap_env", None) or {})

    def describe(self) -> dict[str, Any]:
        return {
            "mode": "sandbox",
            "sandboxed": True,
            "sha": self.sha,
            "sandbox_evidence": self.side.sandbox_evidence,
            "image_digest": getattr(self.side.provision, "image_digest", None),
            "db_snapshot_sha256": _sha256_file(self.seed_db) if self.seed_db.is_file() else None,
            "starts": self._starts,
            "frozen_clock": self._clock,
            "pdcov_setup_exit": self.hook.pdcov_rc,
            "app_cwd": self.hook.app_cwd,
            "env": {"PYTHONHASHSEED": "0", "TZ": "UTC"},
        }

    def close(self) -> None:
        self._stop_current()
        if self._ready is not None:
            with contextlib.suppress(Exception):
                self._ready.stop()
            self._ready = None


@contextmanager
def lean_sandbox_patched(sandbox_cls: type) -> Iterator[None]:
    """Hand the orchestrator the pdcov-aware Sandbox for the duration of one provision."""
    with mock.patch("proofdeploy.orchestrator.Sandbox", sandbox_cls):
        yield


def provision_sandbox_sides(
    rc: LeanRunConfig, contract_file: Path | None, work: Path, timings: Timings
) -> tuple[SandboxSide, SandboxSide, RepoContract]:
    from proofdeploy.orchestrator import MeasureConfig, Orchestrator, OrchestratorError, _free_port

    cfg = MeasureConfig(
        repo_dir=rc.repo,
        output_dir=work / "records",
        workdir=work / "orchestrator",
        no_skill=True,
        sandbox=True,
        use_contract_store=contract_file is not None,
        unit_repo=rc.unit_repo,
        contract_path=contract_file,
    )
    orch = Orchestrator(cfg)
    contract_env: dict[str, str] | None = None
    try:
        if contract_file is not None:
            contract_env = dict(orch._load_unit_contract().env)
        orch._resolve_run_network_mode()
    except Exception as exc:
        raise LeanEnvironmentError("provisioning_failed", f"orchestrator setup: {exc}") from exc
    # One lock-resolution cutoff for both sides, as the measured path does.
    orch._run_lock_cutoff = orch._commit_cutoff(rc.head_sha)
    unit_dir = work / "units"
    unit_dir.mkdir(parents=True, exist_ok=True)
    sides: dict[str, SandboxSide] = {}
    logs_dir = rc.output_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    try:
        for label, sha in (("base", rc.base_sha), ("head", rc.head_sha)):
            hook = SandboxHook(overlay=rc.django_overlay, contract_env=contract_env)
            with timings.stage(f"provision_{label}"):
                try:
                    with lean_sandbox_patched(make_lean_sandbox(hook)):
                        side = orch._provision_side_sandbox(sha, _free_port(), unit_dir, label)
                except (OrchestratorError, FileNotFoundError, ValueError, OSError) as exc:
                    raise LeanEnvironmentError(
                        "provisioning_failed", f"{label}: {type(exc).__name__}: {exc}"
                    ) from exc
            (logs_dir / f"provision-{label}.log").write_text(
                "\n".join(str(x) for x in side.provision.logs), encoding="utf-8"
            )
            if not side.provision.ready:
                if hook.pdcov_rc == NOT_PYTHON_EXIT:
                    raise LeanRefusal(
                        "runtime_not_python",
                        f"{label}: the lean path measures Python apps (no app venv found)",
                    )
                detail = hook.pdcov_output[-500:] if hook.pdcov_rc not in (None, 0) else ""
                cause = "pdcov_injection_failed" if detail else "provisioning_failed"
                raise LeanEnvironmentError(
                    cause,
                    f"{label}: {side.provision.status.value}: "
                    f"{side.provision.reason} {detail}".strip(),
                )
            try:
                ss = SandboxSide(label=label, side=side, hook=hook, work=work / "lean" / label)
                ss.prepare()
            except BaseException:
                if side.provision.proc is not None:
                    with contextlib.suppress(Exception):
                        side.provision.proc.stop()
                raise
            sides[label] = ss
    except BaseException:
        for s in sides.values():
            s.close()
        raise
    return sides["base"], sides["head"], sides["head"].contract


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


@dataclass
class LeanRunConfig:
    repo: Path
    base: str
    head: str
    output_dir: Path
    contract: Path | None = None
    unit_repo: str | None = None
    intent: str | None = None
    no_sandbox: bool = False
    django_overlay: dict[str, Any] = field(default_factory=dict)
    budget_tokens: int = 10_000
    budget_requests: int = 200
    openapi_path: str | None = None
    flask_target: str = "app"
    python: Path | None = None
    base_sha: str = ""
    head_sha: str = ""


def _contract_file(rc: LeanRunConfig) -> Path | None:
    if rc.contract is not None:
        return Path(rc.contract)
    if rc.unit_repo:
        try:
            return contract_store_path(rc.unit_repo)
        except ValueError as exc:
            raise LeanUsageError(str(exc)) from exc
    return None


def write_outputs(out: Path, result: LeanResult) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / "manifest.json").write_text(dumps(result.manifest), encoding="utf-8")
    if result.record is not None:
        (out / "lean-record.json").write_text(dumps(result.record.to_dict()), encoding="utf-8")
    if result.exchanges:
        (out / "exchanges.json").write_text(dumps(result.exchanges), encoding="utf-8")
    for name, rows in result.cover_exchanges.items():
        # one JSON object per line: the coverage loop's own requests, both sides' exchanges
        lines = "".join(
            json.dumps(r, sort_keys=True, ensure_ascii=False, default=str) + "\n" for r in rows
        )
        (out / f"exchanges.{name}.jsonl").write_text(lines, encoding="utf-8")


def lean_verify(
    rc: LeanRunConfig,
    *,
    model_runner: ModelRunner | None = None,
    log: Callable[[str], None] = lambda _m: None,
) -> tuple[LeanResult, int]:
    """Run the lean verdict; returns (result, exit code). Raises only ``LeanUsageError``."""
    rc.repo = Path(rc.repo)
    rc.output_dir = Path(rc.output_dir)
    if not (rc.repo / ".git").exists():
        raise LeanUsageError(f"--repo {rc.repo} is not a git checkout")
    try:
        rc.base_sha = resolve_sha(rc.repo, rc.base)
        rc.head_sha = resolve_sha(rc.repo, rc.head)
    except ValueError as exc:
        raise LeanUsageError(str(exc)) from exc
    contract_file = _contract_file(rc)
    cfg = LeanConfig(
        repo=rc.repo,
        base=rc.base_sha,
        head=rc.head_sha,
        intent=rc.intent,
        budget_tokens=rc.budget_tokens,
        budget_requests=rc.budget_requests,
        openapi_path=rc.openapi_path,
        flask_target=rc.flask_target,
        mode="host" if rc.no_sandbox else "sandbox",
        unit_repo=rc.unit_repo,
        inputs={
            "contract_file": str(contract_file) if contract_file else "head tree proofdeploy.yml",
            "contract_sha256": _sha256_file(contract_file)
            if contract_file and contract_file.is_file()
            else None,
            "django_overlay": {k: repr(v) for k, v in rc.django_overlay.items()},
        },
    )
    timings = Timings()
    early = not_observable_result(cfg, timings)
    if early is not None:  # typed up front: no sandbox, no model call
        write_outputs(rc.output_dir, early)
        return early, _VERDICT_EXIT[early.verdict.status]
    work = rc.output_dir / "work"
    work.mkdir(parents=True, exist_ok=True)
    drivers: list[SideDriver] = []
    try:
        try:
            bside: SideDriver
            hside: SideDriver
            if rc.no_sandbox:
                bside, hside, contract = provision_host_sides(rc, contract_file, work, timings)
            else:
                bside, hside, contract = provision_sandbox_sides(rc, contract_file, work, timings)
            drivers = [bside, hside]
            result = run_pipeline(
                cfg, contract, bside, hside, runner=model_runner, timings=timings, log=log
            )
        except LeanEnvironmentError as exc:
            result = environment_result(cfg, exc.cause, exc.message, timings)
        except lc.CoverageHarnessError as exc:
            result = environment_result(cfg, "coverage_harness", str(exc)[:500], timings)
        except LeanRefusal as exc:
            result = environment_result(cfg, exc.code, exc.message, timings)
            result.verdict = Verdict("NOT_SUPPORTED", exc.code, {"message": exc.message})
            result.manifest["verdict"] = result.verdict.to_dict()
            write_outputs(rc.output_dir, result)
            return result, EXIT_NOT_SUPPORTED
    finally:
        for d in drivers:
            with contextlib.suppress(Exception):
                d.close()
    write_outputs(rc.output_dir, result)
    return result, _VERDICT_EXIT[result.verdict.status]


def summary_text(result: LeanResult, code: int) -> str:
    v = result.manifest.get("verdict", {})
    lines = [f"verdict: {v.get('status')} ({v.get('cause')}) exit {code}"]
    m = result.manifest
    if m.get("timings_seconds"):
        lines.append(
            "timings: " + ", ".join(f"{k}={s:.1f}s" for k, s in m["timings_seconds"].items())
        )
    if "compared" in m:
        c = m["compared"]
        lines.append(
            f"compared {c['exchanges']} exchange(s), {c['fields']} field(s): "
            f"{c['differences']} difference(s), {len(m.get('noise_fields', []))} noise field(s)"
        )
    if m.get("hard_failures"):
        for h in m["hard_failures"]:
            lines.append(f"hard failure [{h['kind']}] {h['request_id']}: {h['detail']}")
    if v.get("status") == FAIL and v.get("details", {}).get("regressions"):
        for r in v["details"]["regressions"]:
            lines.append(
                f"regression: {r['method']} {r['path']} field {r['field_path']}: "
                f"expected {r['expected_value']!r}, got {r['after']!r}"
            )
    return "\n".join(lines) + "\n"


def cmd_lean_verify(args: Any, model_runner: ModelRunner | None = None) -> int:
    """CLI body for ``proofdeploy lean-verify``; exit codes as ``verify``."""
    try:
        overlay = parse_overlay_items(list(args.django_overlay or []))
        rc = LeanRunConfig(
            repo=Path(args.repo),
            base=args.base,
            head=args.head,
            output_dir=Path(args.output_dir),
            contract=Path(args.contract) if args.contract else None,
            unit_repo=args.unit_repo,
            intent=args.intent,
            no_sandbox=bool(args.no_sandbox),
            django_overlay=overlay,
            budget_tokens=int(args.budget_tokens),
            budget_requests=int(args.budget_requests),
            openapi_path=args.openapi_path,
            flask_target=args.flask_target,
            python=Path(args.python) if args.python else None,
        )
        if rc.budget_tokens <= 0 or rc.budget_requests <= 0:
            raise LeanUsageError("budgets must be positive")
        if rc.contract and rc.unit_repo:
            raise LeanUsageError("use --contract or --unit-repo, not both")
        if rc.python is not None and not rc.no_sandbox:
            raise LeanUsageError("--python is only meaningful with --no-sandbox")
        result, code = lean_verify(
            rc, model_runner=model_runner, log=lambda m: print(m, flush=True)
        )
    except LeanUsageError as exc:
        print(f"proofdeploy lean-verify: usage error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except Exception:
        traceback.print_exc()
        print("proofdeploy lean-verify: internal error (our gap); traceback above", file=sys.stderr)
        return EXIT_INTERNAL
    print(summary_text(result, code), end="")
    print(f"output: {rc.output_dir}")
    return code


__all__ = [
    "EXIT_FAIL",
    "EXIT_INCONCLUSIVE",
    "EXIT_INTERNAL",
    "EXIT_NOT_SUPPORTED",
    "EXIT_PASS",
    "EXIT_USAGE",
    "HostSide",
    "LeanRunConfig",
    "LeanUsageError",
    "SandboxHook",
    "SandboxSide",
    "cmd_lean_verify",
    "lean_verify",
    "make_lean_sandbox",
    "parse_overlay_items",
    "provision_host_sides",
    "provision_sandbox_sides",
]
