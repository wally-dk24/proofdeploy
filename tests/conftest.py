"""Pytest session fixtures for ProofDeploy tests.

T5: fail the session if any target process started by the suite is still
alive at the end. This catches the O2 resource leak (target servers left
running after tests). Scoped to the suite's own processes: only PIDs whose
working directory is under pytest's tmp root, compared against a baseline
taken at session start.

B3 (Addendum 16): when PD_REQUIRE_SANDBOX=1 (set in the sandbox-tests
CI job), sandbox tests must not skip. A skipped sandbox test fails the
job instead. This catches the T1 miss: the sandboxed end-to-end test
was silently skipping in CI due to a broken import, hiding real bugs.
"""
import os
import subprocess
from pathlib import Path

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


# The suite's one definition of "sandbox-gated": a skipif whose reason
# names the sandbox capabilities. Used by the B3 hook below (a sandbox
# skip becomes a failure under PD_REQUIRE_SANDBOX=1) and by the WAL-102
# collection hook (the same tests get the `sandbox` marker, so CI can
# select them exactly).
_SANDBOX_SKIP_KEYWORDS = ("podman", "sandbox", "bridge", "userns", "container")


def _sandbox_skip_reason(marker) -> str | None:
    """Return the marker's reason if it is a sandbox-capability skipif."""
    reason = marker.kwargs.get("reason", "")
    if any(kw in reason.lower() for kw in _SANDBOX_SKIP_KEYWORDS):
        return reason
    return None


def pytest_itemcollected(item):
    """WAL-102: tag sandbox-gated tests with the `sandbox` marker.

    A test is sandbox-gated iff it carries a skipif whose reason names
    the sandbox capabilities — keyed on the marks a test actually
    carries, never on its file name: podman-free sandbox unit tests
    (test_provisioning_sandbox.py, test_sandbox_addendum18.py) carry
    no such skipif and stay in the host selection, while sandbox tests
    living in mixed files (test_orchestrator.py) are selected
    correctly. The ci job runs -m "not sandbox"; the sandbox-tests job
    runs -m sandbox with PD_REQUIRE_SANDBOX=1. Every test is in
    exactly one selection.
    """
    for marker in item.iter_markers(name="skipif"):
        if _sandbox_skip_reason(marker) is not None:
            item.add_marker(pytest.mark.sandbox)
            return


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item):
    """B3: with PD_REQUIRE_SANDBOX=1, sandbox skips become failures.

    Runs before pytest's own skip handling (tryfirst). If a test has a
    skipif marker with a true condition and a sandbox-related reason,
    fail instead of skipping. This ensures the sandbox-tests CI job
    actually exercises the sandbox, rather than silently skipping.
    """
    if os.environ.get("PD_REQUIRE_SANDBOX") != "1":
        return
    for marker in item.iter_markers(name="skipif"):
        condition = marker.args[0] if marker.args else False
        # The condition was evaluated at import; a truthy value means
        # the test would skip.
        would_skip = bool(condition) if isinstance(condition, bool) else False
        if not would_skip:
            continue
        reason = _sandbox_skip_reason(marker)
        if reason is not None:
            pytest.fail(
                f"B3: test {item.nodeid} would skip with "
                f"PD_REQUIRE_SANDBOX=1 (reason: {reason}). "
                f"Sandbox tests must run in the sandbox-tests job; "
                f"a skip here hides real bugs."
            )


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


def _bridge_sock_dirs() -> set[str]:
    """Host paths of per-run bridge socket dirs (/tmp/pd-*)."""
    try:
        return {str(p) for p in Path("/tmp").glob("pd-*")}
    except OSError:
        return set()


@pytest.fixture(scope="session", autouse=True)
def _no_leftover_bridge_sockets():
    """SB5 (Addendum 18): fail if the suite leaves /tmp/pd-* behind.

    The bridge socket lives in a short per-run dir under /tmp; every
    exit path (stop(), failed start) must remove it. The reviewer found
    13 such dirs piled up with cleanup disabled, which the old T5
    process check could not see.
    """
    before = _bridge_sock_dirs()
    yield
    leftover = _bridge_sock_dirs() - before
    assert not leftover, (
        f"SB5: {len(leftover)} bridge socket dir(s) left behind after "
        f"the test session: {sorted(leftover)}. The per-run socket dir "
        "must be removed on every exit path."
    )
