"""Pytest session fixtures for ProofDeploy tests.

T5: fail the session if any target process started by the suite is still
alive at the end. This catches the O2 resource leak (target servers left
running after tests).
"""
import subprocess

import pytest


def _target_procs():
    """Return PIDs of lingering target server processes."""
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
                pids.append(parts[0])
    return pids


@pytest.fixture(scope="session", autouse=True)
def _no_lingering_targets():
    """T5: fail if any target process survives the test session."""
    yield
    lingering = _target_procs()
    assert not lingering, (
        f"T5: {len(lingering)} target process(es) still alive after test session: "
        f"PIDs {lingering}. The O2 process-group cleanup is incomplete."
    )
