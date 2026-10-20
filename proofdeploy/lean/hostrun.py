"""Host-side driver: run an app tree under pdcov on this machine.

Used by the lean tests (and usable for experiments) to start a checked-out
app, with the coverage injection of ``proofdeploy.lean.coverage``, without a
sandbox. Not part of the measured path; it only starts processes the caller
names, with the clean allow-listed environment of ``clean_app_env``.
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from proofdeploy.lean import coverage as lc
from proofdeploy.lean.coverage import CoverageSession, PdCov
from proofdeploy.lean.index import FLASK_DUMP_SNIPPET


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def pypi_reachable() -> bool:
    try:
        urllib.request.urlopen("https://pypi.org/simple/flask/", timeout=15).close()
    except (urllib.error.URLError, OSError):
        return False
    return True


def make_venv(venv: Path, requirements: Path) -> Path:
    """Create ``venv`` and install a hash-pinned requirements file; return its python."""
    subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True, timeout=120)
    py = venv / "bin" / "python"
    subprocess.run(
        [
            str(py),
            "-m",
            "pip",
            "install",
            "-q",
            "--require-hashes",
            "--no-input",
            "-r",
            str(requirements),
        ],
        check=True,
        timeout=300,
    )
    return py


def add_worktree(repo: Path, sha: str, tree: Path) -> Path:
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "-q", "--detach", str(tree), sha],
        check=True,
    )
    return tree


def wait_http(port: int, path: str = "/health", *, attempts: int = 100) -> None:
    for _ in range(attempts):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5).close()
            return
        except urllib.error.HTTPError:
            return  # the server answered
        except OSError:
            time.sleep(0.1)
    raise AssertionError(f"app did not come up on port {port}")


def flask_url_map(py: Path, tree: Path, target: str = "app") -> list[dict[str, object]]:
    """The Flask app's URL map as ``index_from_flask_url_map`` input."""
    proc = subprocess.run(
        [str(py), "-c", FLASK_DUMP_SNIPPET, target],
        cwd=tree,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    rules: list[dict[str, object]] = json.loads(proc.stdout)
    return rules


@dataclass
class RunningApp:
    tree: Path
    port: int
    proc: subprocess.Popen[bytes]
    session: CoverageSession
    pd: PdCov
    log: Path

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def stop_and_collect(self, *, root_prefix: str = "") -> lc.Coverage:
        return self.session.stop_and_collect(self.proc, root_prefix=root_prefix, log_path=self.log)


@contextmanager
def running_app(
    py: Path,
    tree: Path,
    tool_dir: Path,
    *,
    setup: Sequence[Sequence[str]] = (("migrate.py",), ("seed.py",)),
    start: Sequence[str] = ("app.py",),
    env: Mapping[str, str] | None = None,
    health_path: str = "/health",
) -> Iterator[RunningApp]:
    """Install pdcov for ``tree``, run setup scripts (unmeasured), start the app (measured).

    ``env`` is passed to the app on top of the allow-listed host variables;
    ``APP_DB`` and ``PORT`` are set here (the golden apps' convention). The
    app is killed on exit if it is still running; call
    ``RunningApp.stop_and_collect()`` before leaving the block to get coverage.
    """
    pd = lc.install_pdcov(tool_dir, tree)
    lc.check_interpreter_compatible(py)
    lc.install_pth(lc.app_site_packages(py), pd)
    session = CoverageSession(pd)
    session.start()
    port = free_port()
    extra = {"APP_DB": str(tree / "app.db"), **(env or {})}
    setup_env = lc.clean_app_env(pd, extra, measured=False)
    for argv in setup:  # setup is not measured
        subprocess.run([str(py), *argv], cwd=tree, env=setup_env, check=True, timeout=60)
    app_env = lc.clean_app_env(pd, {**extra, "PORT": str(port)})
    log = tree / "app.log"
    with log.open("wb") as logf:
        proc = subprocess.Popen(
            [str(py), *start], cwd=tree, env=app_env, stdout=logf, stderr=subprocess.STDOUT
        )
        try:
            wait_http(port, health_path)
            yield RunningApp(tree, port, proc, session, pd, log)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
