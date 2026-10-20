"""WO-P2 (D258): lean step 2, impact via per-request coverage contexts.

Host-side only (no podman). Unit tests cover the diff parser, the impact
join and the Django settings overlay; the end-to-end test runs the golden
Flask app (base and bug revisions) under coverage.py with the injected
``pdcov`` startup and checks which request executes the changed line.
The end-to-end test needs network once (hash-pinned pip install of the
golden requirements) and skips ONLY when PyPI is unreachable.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "conformance"))
import kit

from proofdeploy.lean import coverage as lc
from proofdeploy.lean.coverage import ChangedLines, CoverageHarnessError, CoverageSession

# --------------------------------------------------------------------------
# ChangedLines.from_diff
# --------------------------------------------------------------------------

MODIFY = """\
diff --git a/app.py b/app.py
index 111..222 100644
--- a/app.py
+++ b/app.py
@@ -3,5 +3,5 @@ def f():
 a
 b
-old
+new
 c
 d
@@ -20,3 +20,4 @@ def g():
 x
-y1
-y2
+z1
+z2
+z3
 w
"""


def test_modify_hunks_added_and_removed_sides() -> None:
    c = ChangedLines.from_diff(MODIFY)
    assert c == {"app.py": {"added": {5, 21, 22, 23}, "removed": {5, 21, 22}}}


def test_pure_additions_and_deletions_use_their_own_side() -> None:
    diff = (
        "diff --git a/m.py b/m.py\n--- a/m.py\n+++ b/m.py\n"
        "@@ -2,0 +3,2 @@\n+n1\n+n2\n"
        "@@ -10,2 +12,0 @@\n-d1\n-d2\n"
    )
    assert ChangedLines.from_diff(diff) == {"m.py": {"added": {3, 4}, "removed": {10, 11}}}


def test_new_file_and_deleted_file() -> None:
    diff = (
        "diff --git a/new.py b/new.py\nnew file mode 100644\nindex 0..1\n"
        "--- /dev/null\n+++ b/new.py\n@@ -0,0 +1,2 @@\n+one\n+two\n"
        "diff --git a/gone.py b/gone.py\ndeleted file mode 100644\nindex 1..0\n"
        "--- a/gone.py\n+++ /dev/null\n@@ -1,3 +0,0 @@\n-a\n-b\n-c\n"
    )
    c = ChangedLines.from_diff(diff)
    assert c["new.py"] == {"added": {1, 2}, "removed": set()}
    assert c["gone.py"] == {"added": set(), "removed": {1, 2, 3}}


def test_rename_with_edit_keys_by_new_path_and_records_old() -> None:
    diff = (
        "diff --git a/old name.py b/new name.py\nsimilarity index 80%\n"
        "rename from old name.py\nrename to new name.py\nindex 1..2 100644\n"
        "--- a/old name.py\n+++ b/new name.py\n@@ -4,2 +4,2 @@\n k\n-before\n+after\n"
    )
    c = ChangedLines.from_diff(diff)
    assert c == {"new name.py": {"added": {5}, "removed": {5}}}
    assert c.renames == {"new name.py": "old name.py"}


def test_pure_rename_has_empty_entry_and_mapping() -> None:
    diff = "diff --git a/a.py b/b.py\nsimilarity index 100%\nrename from a.py\nrename to b.py\n"
    c = ChangedLines.from_diff(diff)
    assert c == {"b.py": {"added": set(), "removed": set()}}
    assert c.renames == {"b.py": "a.py"}


def test_no_newline_marker_does_not_shift_lines() -> None:
    diff = (
        "diff --git a/f.txt b/f.txt\n--- a/f.txt\n+++ b/f.txt\n"
        "@@ -1,2 +1,2 @@\n keep\n-last\n\\ No newline at end of file\n"
        "+last2\n\\ No newline at end of file\n"
        "diff --git a/g.txt b/g.txt\n--- a/g.txt\n+++ b/g.txt\n@@ -1 +1 @@\n-p\n+q\n"
    )
    c = ChangedLines.from_diff(diff)
    assert c["f.txt"] == {"added": {2}, "removed": {2}}
    assert c["g.txt"] == {"added": {1}, "removed": {1}}


def test_binary_files_are_listed_not_counted() -> None:
    diff = (
        "diff --git a/img.png b/img.png\nindex 1..2 100644\n"
        "Binary files a/img.png and b/img.png differ\n"
        "diff --git a/b.bin b/b.bin\nnew file mode 100644\nindex 0..1\n"
        "GIT binary patch\nliteral 3\nIcmZQz\n\nliteral 0\nHcmV?d00001\n\n"
        "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\n"
    )
    c = ChangedLines.from_diff(diff)
    assert set(c) == {"x.py"}
    assert c.binary_files == {"img.png", "b.bin"}


def test_removed_line_that_looks_like_a_header_is_content() -> None:
    # A removed line "-- sql comment" renders as "--- sql comment".
    diff = (
        "diff --git a/q.sql b/q.sql\n--- a/q.sql\n+++ b/q.sql\n"
        "@@ -1,2 +1,2 @@\n--- sql comment\n+++ new comment\n tail\n"
    )
    assert ChangedLines.from_diff(diff) == {"q.sql": {"added": {1}, "removed": {1}}}


def test_plain_unified_diff_without_git_headers() -> None:
    diff = (
        "--- a/p.py\t2026-01-01\n+++ b/p.py\t2026-01-02\n@@ -1 +1 @@\n-a\n+b\n"
        "--- a/r.py\n+++ b/r.py\n@@ -5 +5 @@\n-c\n+d\n"
    )
    c = ChangedLines.from_diff(diff)
    assert c == {"p.py": {"added": {1}, "removed": {1}}, "r.py": {"added": {5}, "removed": {5}}}


@pytest.mark.parametrize(
    "diff",
    [
        'diff --git "a/caf\\303\\251.py" "b/caf\\303\\251.py"\n--- "a/caf\\303\\251.py"\n',
        'diff --git a/x.py b/x.py\n--- "a/we\\"ird.py"\n+++ b/x.py\n',
        'diff --git a/a.py b/b.py\nrename from a.py\nrename to "b\\tq.py"\n',
    ],
)
def test_c_quoted_paths_fail_closed(diff: str) -> None:
    with pytest.raises(lc.DiffParseError, match="C-quoted"):
        ChangedLines.from_diff(diff)


def test_hunk_count_mismatch_and_combined_diffs_fail_closed() -> None:
    with pytest.raises(lc.DiffParseError):
        ChangedLines.from_diff("--- a/x\n+++ b/x\n@@ -1,2 +1,2 @@\n a\nzzz\n")
    with pytest.raises(lc.DiffParseError, match="combined"):
        ChangedLines.from_diff("diff --cc x\n--- a/x\n+++ b/x\n@@@ -1 -1 +1 @@@\n")


def test_real_git_diff_matches_parser(tmp_path: Path) -> None:
    """Cross-check against a real ``git diff`` at -U0 and -U3."""
    repo = tmp_path / "r"
    repo.mkdir()

    def git(*a: str) -> str:
        return subprocess.run(
            ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@x", *a],
            capture_output=True,
            text=True,
            check=True,
        ).stdout

    git("init", "-q")
    (repo / "f.py").write_text("".join(f"l{i}\n" for i in range(1, 11)))
    git("add", "-A")
    git("commit", "-qm", "a")
    (repo / "f.py").write_text(
        "".join(f"l{i}\n" for i in (1, 2, 3)) + "X\nY\n" + "".join(f"l{i}\n" for i in (6, 7, 8, 9))
    )
    git("add", "-A")
    git("commit", "-qm", "b")
    for unified in ("-U0", "-U3"):
        c = ChangedLines.from_diff(git("diff", unified, "HEAD~1", "HEAD"))
        assert c == {"f.py": {"added": {4, 5}, "removed": {4, 5, 10}}}


# --------------------------------------------------------------------------
# impact / unexercised
# --------------------------------------------------------------------------


def _changed(
    path: str = "app.py", added: set[int] | None = None, removed: set[int] | None = None
) -> ChangedLines:
    c = ChangedLines()
    c[path] = {"added": set(added or ()), "removed": set(removed or ())}
    return c


def test_impact_attributes_hits_to_requests_by_side() -> None:
    changed = _changed(added={10, 11}, removed={5})
    cov_old = {"r1": {"app.py": {5, 6}}, "r2": {"app.py": {1}}, "": {"app.py": {5}}}
    cov_new = {"r1": {"app.py": {10}}, "r2": {"app.py": {10, 11}}}
    got = lc.impact(changed, cov_old, cov_new)
    assert got == {
        "r1": {"hits_added": ["app.py:10"], "hits_removed": ["app.py:5"]},
        "r2": {"hits_added": ["app.py:10", "app.py:11"], "hits_removed": []},
    }


def test_removed_lines_are_judged_on_old_side_only() -> None:
    # Line 7 was removed. New-side coverage happens to execute line 7 (a
    # different statement now lives there); old-side never ran it.
    changed = _changed(removed={7})
    cov_old = {"r1": {"app.py": {1}}}
    cov_new = {"r1": {"app.py": {7}}}
    assert lc.impact(changed, cov_old, cov_new)["r1"]["hits_removed"] == []
    assert lc.unexercised(changed, cov_old, cov_new) == {"app.py": {"added": [], "removed": [7]}}


def test_added_lines_are_judged_on_new_side_only() -> None:
    changed = _changed(added={7})
    cov_old = {"r1": {"app.py": {7}}}
    cov_new = {"r1": {"app.py": {1}}}
    assert lc.unexercised(changed, cov_old, cov_new) == {"app.py": {"added": [7], "removed": []}}


def test_startup_context_does_not_count_as_a_request() -> None:
    changed = _changed(added={3})
    cov_new = {"": {"app.py": {3}}}
    assert lc.impact(changed, {}, cov_new) == {}
    assert lc.unexercised(changed, {}, cov_new) == {"app.py": {"added": [3], "removed": []}}


def test_rename_uses_old_path_on_old_side() -> None:
    changed = _changed("new.py", added={2}, removed={2})
    changed.renames["new.py"] = "old.py"
    cov_old = {"r": {"old.py": {2}}}
    cov_new = {"r": {"new.py": {2}}}
    assert lc.impact(changed, cov_old, cov_new)["r"] == {
        "hits_added": ["new.py:2"],
        "hits_removed": ["new.py:2"],
    }
    assert lc.unexercised(changed, cov_old, cov_new) == {}


# --------------------------------------------------------------------------
# injection files, session, settings overlay
# --------------------------------------------------------------------------


def test_install_pdcov_writes_rcfile_startup_and_coverage_copy(tmp_path: Path) -> None:
    pd = lc.install_pdcov(tmp_path / "pdcov", "/srv/app")
    rc = pd.rcfile.read_text()
    assert "parallel = true" in rc and "sigterm = true" in rc
    assert "relative_files = true" in rc and "source = /srv/app" in rc
    assert f"data_file = {tmp_path / 'pdcov' / 'data' / '.coverage'}" in rc
    assert "process_startup" in (pd.tool_dir / "pdcov_startup.py").read_text()
    assert (pd.lib_dir / "coverage" / "__init__.py").is_file()
    assert pd.env() == {"COVERAGE_PROCESS_START": str(pd.rcfile)}
    pth = lc.install_pth(tmp_path, pd)
    assert pth.name == "pdcov.pth" and pth.read_text().rstrip().endswith("import pdcov_startup")


def test_set_context_times_out_without_listener(tmp_path: Path) -> None:
    pd = lc.install_pdcov(tmp_path / "pdcov", tmp_path)
    s = CoverageSession(pd, ack_timeout=0.2)
    t0 = time.monotonic()
    with pytest.raises(CoverageHarnessError, match="did not acknowledge"):
        s.set_context("req-1")
    assert time.monotonic() - t0 < 2
    assert pd.context_file.read_text() == "req-1"


def test_set_context_returns_when_ack_matches(tmp_path: Path) -> None:
    pd = lc.install_pdcov(tmp_path / "pdcov", tmp_path)
    pd.ack_file.write_text("req-2")  # stands in for the in-app listener
    CoverageSession(pd, ack_timeout=0.5).set_context("req-2")
    with pytest.raises(CoverageHarnessError):
        CoverageSession(pd, ack_timeout=0.1).set_context("req-3")


def test_clean_app_env_drops_host_coverage_variables(tmp_path: Path) -> None:
    pd = lc.install_pdcov(tmp_path / "pdcov", tmp_path)
    host = {
        "PATH": "/bin",
        "HOME": "/h",
        "COVERAGE_FILE": "/x/.coverage.host",
        "COVERAGE_PROCESS_START": "/other/rc",
        "COV_CORE_SOURCE": "proofdeploy",
        "SECRET_TOKEN": "nope",
    }
    env = lc.clean_app_env(pd, {"PORT": "1"}, base=host)
    assert env == {
        "PATH": "/bin",
        "HOME": "/h",
        "PORT": "1",
        "COVERAGE_PROCESS_START": str(pd.rcfile),
    }
    assert "COVERAGE_PROCESS_START" not in lc.clean_app_env(pd, base=host, measured=False)


def test_missing_data_error_carries_exit_code_and_app_output(tmp_path: Path) -> None:
    pd = lc.install_pdcov(tmp_path / "pdcov", tmp_path)
    log = tmp_path / "app.log"
    log.write_text("Traceback: boom\n")
    proc = subprocess.Popen([sys.executable, "-c", "raise SystemExit(3)"])
    proc.wait()
    with pytest.raises(CoverageHarnessError) as ei:
        CoverageSession(pd).stop_and_collect(proc, log_path=log)
    assert "exit code: 3" in str(ei.value) and "Traceback: boom" in str(ei.value)


def test_collect_without_data_fails_closed(tmp_path: Path) -> None:
    pd = lc.install_pdcov(tmp_path / "pdcov", tmp_path)
    with pytest.raises(CoverageHarnessError, match="no coverage data"):
        CoverageSession(pd).stop_and_collect()


def test_django_settings_overlay(tmp_path: Path) -> None:
    (tmp_path / "fakeproj").mkdir()
    (tmp_path / "fakeproj" / "__init__.py").write_text("")
    (tmp_path / "fakeproj" / "settings.py").write_text(
        "DEBUG = False\nCELERY_TASK_ALWAYS_EAGER = False\nNAME = 'base'\n"
    )
    overlay_dir = tmp_path / "overlay"
    name = lc.write_django_settings_overlay(
        overlay_dir,
        "fakeproj.settings",
        {"CELERY_TASK_ALWAYS_EAGER": True, "EXTRA": {"a": [1, 2]}},
    )
    assert name == "pd_settings_overlay"
    src = (overlay_dir / "pd_settings_overlay.py").read_text()
    assert "from fakeproj.settings import *" in src
    out = subprocess.run(
        [
            sys.executable,
            "-c",
            "import os,importlib,json;m=importlib.import_module(os.environ['M']);"
            "print(json.dumps([m.DEBUG,m.CELERY_TASK_ALWAYS_EAGER,m.NAME,m.EXTRA]))",
        ],
        env={**os.environ, "M": name, "PYTHONPATH": f"{overlay_dir}{os.pathsep}{tmp_path}"},
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert json.loads(out) == [False, True, "base", {"a": [1, 2]}]


@pytest.mark.parametrize(
    ("base", "overrides"),
    [
        ("os; import evil", {}),
        ("fakeproj.settings", {"bad-name": 1}),
        ("fakeproj.settings", {"X": object()}),
        ("fakeproj.settings", {"X": float("nan")}),
    ],
)
def test_django_settings_overlay_rejects_unsafe_input(
    tmp_path: Path, base: str, overrides: dict[str, object]
) -> None:
    with pytest.raises(ValueError):
        lc.write_django_settings_overlay(tmp_path, base, overrides)


# --------------------------------------------------------------------------
# end to end on the golden Flask app
# --------------------------------------------------------------------------

APP = "python-flask-sqlite"


def _pypi_reachable() -> bool:
    try:
        urllib.request.urlopen("https://pypi.org/simple/flask/", timeout=15).close()
    except (urllib.error.URLError, OSError):
        return False
    return True


@pytest.fixture(scope="module")
def golden_env(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path | dict[str, str]]:
    root = tmp_path_factory.mktemp("lean_p2")
    repo = root / "repo"
    shas = kit.materialize_golden(APP, repo)
    reqs = repo / "requirements.txt"
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", shas["bug"]], check=True)
    if not _pypi_reachable():
        pytest.skip("PyPI unreachable: cannot create the hash-pinned golden venv (no network)")
    venv = root / "venv"
    subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True, timeout=120)
    py = venv / "bin" / "python"
    subprocess.run(
        [str(py), "-m", "pip", "install", "-q", "--require-hashes", "--no-input", "-r", str(reqs)],
        check=True,
        timeout=300,
    )
    return {"root": root, "repo": repo, "py": py, "shas": shas}  # type: ignore[dict-item]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _http(
    method: str, port: int, path: str, body: dict[str, str] | None = None, token: str | None = None
) -> tuple[int, dict[str, object]]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status, json.loads(resp.read())


def _run_revision(env: dict[str, object], rev: str, tool_dir: Path) -> lc.Coverage:
    root: Path = env["root"]  # type: ignore[assignment]
    repo: Path = env["repo"]  # type: ignore[assignment]
    py: Path = env["py"]  # type: ignore[assignment]
    shas: dict[str, str] = env["shas"]  # type: ignore[assignment]
    tree = root / f"tree-{rev}"
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "-q", "--detach", str(tree), shas[rev]],
        check=True,
    )
    pd = lc.install_pdcov(tool_dir, tree)
    lc.check_interpreter_compatible(py)
    lc.install_pth(lc.app_site_packages(py), pd)
    session = CoverageSession(pd)
    session.start()
    base_env = lc.clean_app_env(pd, {"APP_DB": str(tree / "app.db")}, measured=False)
    for step in ("migrate.py", "seed.py"):  # no coverage env: setup is not measured
        subprocess.run([str(py), step], cwd=tree, env=base_env, check=True, timeout=60)
    port = _free_port()
    app_env = lc.clean_app_env(pd, {"APP_DB": str(tree / "app.db"), "PORT": str(port)})
    log = tree / "app.log"
    logf = log.open("wb")
    proc = subprocess.Popen(
        [str(py), "app.py"], cwd=tree, env=app_env, stdout=logf, stderr=subprocess.STDOUT
    )
    try:
        for _ in range(100):
            try:
                _http("GET", port, "/health")
                break
            except OSError:
                time.sleep(0.1)
        else:
            raise AssertionError("app did not come up")
        session.set_context("ctx-health")
        assert _http("GET", port, "/health")[0] == 200
        session.set_context("ctx-login")
        _, login = _http("POST", port, "/login", {"username": "seeduser", "password": "seedpass"})
        token = str(login["token"])
        session.set_context("ctx-get1")
        status, note = _http("GET", port, "/notes/1", token=token)
        assert status == 200 and note["id"] == 1
        return session.stop_and_collect(proc, log_path=log)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_golden_flask_impact_end_to_end(
    golden_env: dict[str, object],
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # CI regression: pytest-cov / the CI shard export these; inherited by the
    # app, COVERAGE_FILE overrides the rcfile's data_file and the data went
    # missing ("no coverage data files"). Always set them here.
    monkeypatch.setenv("COVERAGE_FILE", str(tmp_path_factory.mktemp("stray") / ".coverage.host"))
    monkeypatch.setenv("COV_CORE_SOURCE", "proofdeploy")
    monkeypatch.setenv("COV_CORE_CONFIG", "x")
    t0 = time.perf_counter()
    repo: Path = golden_env["repo"]  # type: ignore[assignment]
    shas: dict[str, str] = golden_env["shas"]  # type: ignore[assignment]
    tool_dir = tmp_path_factory.mktemp("tools") / "pdcov"
    cov_old = _run_revision(golden_env, "base", tool_dir)
    cov_new = _run_revision(golden_env, "bug", tool_dir)
    diff = subprocess.run(
        ["git", "-C", str(repo), "diff", shas["base"], shas["bug"]],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    changed = ChangedLines.from_diff(diff)
    assert set(changed) == {"app.py"}
    (line,) = changed["app.py"]["added"]
    assert changed["app.py"]["removed"] == {line}

    for cov in (cov_old, cov_new):
        assert {"ctx-health", "ctx-login", "ctx-get1"} <= set(cov)
        assert "app.py" in cov["ctx-get1"]
    got = lc.impact(changed, cov_old, cov_new)
    here = f"app.py:{line}"
    assert got["ctx-get1"] == {"hits_added": [here], "hits_removed": [here]}
    assert got["ctx-health"] == {"hits_added": [], "hits_removed": []}
    assert got["ctx-login"] == {"hits_added": [], "hits_removed": []}
    assert lc.unexercised(changed, cov_old, cov_new) == {}
    print(f"golden e2e (two server runs): {time.perf_counter() - t0:.1f}s")
