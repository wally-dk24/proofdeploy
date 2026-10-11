"""Pytest session fixtures for ProofDeploy tests.

T5: fail the session if any target process started by the suite is still
alive at the end. This catches the O2 resource leak (target servers left
running after tests). Scoped to the suite's own processes: only PIDs whose
working directory is under pytest's tmp root, compared against a baseline
taken at session start.
"""
import os
import subprocess

import pytest


def _proc_cwd(pid: str) -> str | None:
    """Return a process's working directory, or None if unreadable."""
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        return None


def _target_procs(tmp_root: str, baseline: set[str]) -> list[str]:
    """Return PIDs of lingering target server processes started by the suite.

    Only processes whose cwd is under pytest's tmp root and that were not
    present at session start (baseline).
    """
    try:
        out = subprocess.run(
            ["ps", "-eo", "pid,args"],
            capture_output=True, text=True, timeout=10,
        ).stdout
    except Exception:
        return []
    pids = []
    for line in out.splitlines():
        # Match test target servers (mini-app app.py, acceptance app.py).
        # Avoid matching pytest itself or this helper.
        if "app.py" in line and "pytest" not in line and "conftest" not in line:
            parts = line.strip().split(None, 1)
            if parts and parts[0].isdigit():
                pid = parts[0]
                if pid in baseline:
                    continue
                cwd = _proc_cwd(pid)
                if cwd and cwd.startswith(tmp_root):
                    pids.append(pid)
    return pids


@pytest.fixture(scope="session", autouse=True)
def _no_lingering_targets(tmp_path_factory):
    """T5: fail if any suite-started target process survives the session."""
    # Baseline: PIDs of app.py processes before the suite runs.
    tmp_root = str(tmp_path_factory.getbasetemp())
    baseline: set[str] = set()
    try:
        out = subprocess.run(
            ["ps", "-eo", "pid,args"],
            capture_output=True, text=True, timeout=10,
        ).stdout
        for line in out.splitlines():
            if "app.py" in line:
                parts = line.strip().split(None, 1)
                if parts and parts[0].isdigit():
                    baseline.add(parts[0])
    except Exception:
        pass
    yield
    lingering = _target_procs(tmp_root, baseline)
    assert not lingering, (
        f"T5: {len(lingering)} suite target process(es) still alive after "
        f"test session: PIDs {lingering}. The O2 process-group cleanup is "
        f"incomplete."
    )
