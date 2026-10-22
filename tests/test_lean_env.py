"""Lean round 3, WO-R3-E (issue-register R4, R5, R6): environment guards.

* R4: ``hostfs`` (container-owned paths: plain first, ``podman unshare`` on PermissionError) and
  its use by ``restore_sqlite``, ``snapshot_sqlite``, the coverage data and control files.
* R6: odd-text and binary diffs (``\\x0b \\x0c \\x1c-\\x1e \\x85`` U+2028/2029, Latin-1, binary
  files) through the diff readers; only ``\\n`` splits lines.
* R5: the no-model rehearsal (``gate.py --rehearse``) and its workflow.

No podman, no network, no model: unshare is a recording fake that runs the inner command locally.
"""

from __future__ import annotations

import contextlib
import importlib.util
import inspect
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from coverage.data import CoverageData

from proofdeploy.lean import coverage as lc
from proofdeploy.lean import hostfs, pipeline
from proofdeploy.lean import run as lean_run
from proofdeploy.lean.coverage import ChangedLines, split_diff_lines
from proofdeploy.lean.generate import executable_view, parse_diff_text
from proofdeploy.sandbox import PODMAN

ROOT = Path(__file__).resolve().parent.parent
GATE_PATH = ROOT / "experiments" / "lean-poc" / "gate.py"
_spec = importlib.util.spec_from_file_location("lean_poc_gate_env", GATE_PATH)
assert _spec is not None and _spec.loader is not None
gate = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = gate
_spec.loader.exec_module(gate)

UNSHARE = [*PODMAN, "unshare"]


# --------------------------------------------------------------------------
# R4: hostfs
# --------------------------------------------------------------------------


class FakeUnshare:
    """Stands in for ``podman unshare``: records the argv and runs the inner command locally."""

    def __init__(self, rc: int = 0, raises: Exception | None = None) -> None:
        self.calls: list[list[str]] = []
        self.rc = rc
        self.raises = raises

    def __call__(self, argv: list[str]) -> subprocess.CompletedProcess[bytes]:
        self.calls.append(list(argv))
        if self.raises is not None:
            raise self.raises
        if self.rc != 0:
            return subprocess.CompletedProcess(argv, self.rc, b"", b"denied")
        assert argv[: len(UNSHARE)] == UNSHARE
        return subprocess.run(argv[len(UNSHARE) :], capture_output=True, check=False)


def _deny(*_a: Any, **_k: Any) -> None:
    raise PermissionError(13, "Permission denied")


@pytest.fixture
def unshare(monkeypatch: pytest.MonkeyPatch) -> FakeUnshare:
    fake = FakeUnshare()
    monkeypatch.setattr(hostfs, "_run", fake)
    return fake


@pytest.fixture
def denied(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every plain host operation on a container-owned path raises PermissionError."""
    monkeypatch.setattr(hostfs, "_copyfile", _deny)
    monkeypatch.setattr(hostfs, "_replace", _deny)
    monkeypatch.setattr(hostfs, "_rmtree", _deny)
    monkeypatch.setattr(hostfs, "_unlink", _deny)
    monkeypatch.setattr(hostfs, "_chmod", _deny)
    monkeypatch.setattr(hostfs, "_read", _deny)


def test_plain_operations_never_start_podman(tmp_path: Path, unshare: FakeUnshare) -> None:
    src, dest = tmp_path / "a", tmp_path / "b"
    src.write_bytes(b"x")
    hostfs.copy_into(src, dest)
    hostfs.chmod(dest, 0o640)
    hostfs.replace(dest, tmp_path / "c")
    assert hostfs.read_bytes(tmp_path / "c") == b"x"
    hostfs.remove_tree(tmp_path / "c")
    hostfs.remove_tree(tmp_path / "missing")
    assert not (tmp_path / "c").exists()
    assert unshare.calls == []


def test_copy_into_falls_back_to_podman_unshare_cp(
    tmp_path: Path, unshare: FakeUnshare, denied: None
) -> None:
    src, dest = tmp_path / "snap", tmp_path / "db.pd-restore"
    src.write_bytes(b"seed")
    hostfs.copy_into(src, dest)
    assert unshare.calls == [[*UNSHARE, "cp", "--", str(src), str(dest)]]
    assert dest.read_bytes() == b"seed"


def test_replace_chmod_read_and_remove_fall_back_with_exact_argv(
    tmp_path: Path, unshare: FakeUnshare, denied: None
) -> None:
    a, b, d = tmp_path / "a", tmp_path / "b", tmp_path / "tree"
    a.write_bytes(b"1")
    hostfs.replace(a, b)
    hostfs.chmod(b, 0o666)
    assert hostfs.read_bytes(b) == b"1"
    (d / "sub").mkdir(parents=True)
    (d / "sub" / "f").write_bytes(b"z")
    hostfs.remove_tree(d)
    assert unshare.calls == [
        [*UNSHARE, "mv", "-f", "--", str(a), str(b)],
        [*UNSHARE, "chmod", "666", "--", str(b)],
        [*UNSHARE, "cat", "--", str(b)],
        [*UNSHARE, "rm", "-rf", "--", str(d)],
    ]
    assert oct(b.stat().st_mode & 0o777) == "0o666" and not d.exists()


@pytest.mark.parametrize(
    "fake",
    [
        FakeUnshare(rc=1),
        FakeUnshare(raises=FileNotFoundError("podman")),
        FakeUnshare(raises=subprocess.TimeoutExpired("podman", 1)),
    ],
)
def test_second_failure_is_a_typed_error_naming_the_path_never_contents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, denied: None, fake: FakeUnshare
) -> None:
    monkeypatch.setattr(hostfs, "_run", fake)
    secret = tmp_path / "SECRET-LEAF"
    secret.write_bytes(b"TOP-SECRET-CONTENTS")
    with pytest.raises(hostfs.ContainerPathError) as ei:
        hostfs.read_bytes(secret)
    assert str(secret) in str(ei.value) and "TOP-SECRET-CONTENTS" not in str(ei.value)
    assert isinstance(ei.value, OSError)
    for call in (
        lambda: hostfs.copy_into(secret, tmp_path / "d"),
        lambda: hostfs.replace(secret, tmp_path / "d"),
        lambda: hostfs.chmod(secret, 0o600),
        lambda: hostfs.remove_tree(secret),
    ):
        with pytest.raises(hostfs.ContainerPathError):
            call()


def test_restore_sqlite_survives_a_container_owned_directory(
    tmp_path: Path, unshare: FakeUnshare, denied: None
) -> None:
    """The round-2 crash: the temp file cannot be written next to a mapped-uid database."""
    snap, target = tmp_path / "seed.sqlite", tmp_path / "data" / "db.sqlite3"
    target.parent.mkdir()
    snap.write_bytes(b"SEEDED")
    target.write_bytes(b"MUTATED-BY-THE-APP")
    for suffix in ("-wal", "-shm", "-journal"):
        target.with_name(target.name + suffix).write_bytes(b"stale")
    lean_run.restore_sqlite(snap, target)
    assert target.read_bytes() == b"SEEDED"
    assert oct(target.stat().st_mode & 0o777) == "0o666"
    assert not list(target.parent.glob("db.sqlite3-*")) and not list(target.parent.glob("*.pd-*"))
    verbs = [c[len(UNSHARE)] for c in unshare.calls]
    assert verbs == ["cp", "chmod", "mv", "rm", "rm", "rm"]


def test_snapshot_sqlite_reads_an_unreadable_database_through_hostfs(
    tmp_path: Path, unshare: FakeUnshare, monkeypatch: pytest.MonkeyPatch
) -> None:
    src, dest = tmp_path / "c" / "db.sqlite3", tmp_path / "w" / "seed.sqlite"
    src.parent.mkdir()
    src.write_bytes(b"not an sqlite file: the backup API fails, the copy fallback is used")
    monkeypatch.setattr(hostfs, "_read", _deny)
    lean_run.snapshot_sqlite(src, dest)
    assert dest.read_bytes() == src.read_bytes()
    assert unshare.calls == [[*UNSHARE, "cat", "--", str(src)]]


def test_snapshot_sqlite_backup_path_is_unchanged(tmp_path: Path, unshare: FakeUnshare) -> None:
    src, dest = tmp_path / "db.sqlite3", tmp_path / "out" / "seed.sqlite"
    with contextlib.closing(sqlite3.connect(src)) as c:
        c.execute("create table t(x)")
        c.execute("insert into t values (7)")
        c.commit()
    lean_run.snapshot_sqlite(src, dest)
    with contextlib.closing(sqlite3.connect(dest)) as c:
        assert c.execute("select x from t").fetchall() == [(7,)]
    assert unshare.calls == []


def test_coverage_runtime_state_is_reset_through_hostfs(
    tmp_path: Path, unshare: FakeUnshare, monkeypatch: pytest.MonkeyPatch
) -> None:
    pd = lc.PdCov(tmp_path / "pdcov")
    pd.data_dir.mkdir(parents=True)
    data = pd.data_dir / ".coverage.host.1.x"
    data.write_bytes(b"d")
    pd.ack_file.write_text("r1")
    real_unlink = Path.unlink

    def unlink(self: Path, missing_ok: bool = False) -> None:
        if self.name.startswith((".coverage", "context")):
            raise PermissionError(13, "mapped uid")
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", unlink)
    lc.reset_runtime_state(pd)
    assert not data.exists() and not pd.ack_file.exists()
    assert [*UNSHARE, "rm", "-rf", "--", str(data)] in unshare.calls
    assert [*UNSHARE, "rm", "-rf", "--", str(pd.ack_file)] in unshare.calls


def _write_coverage_part(data_dir: Path) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    cd = CoverageData(basename=str(data_dir / ".coverage.host.1.abc"), suffix=False)
    cd.set_context("r1")
    cd.add_lines({"app.py": {3, 4}})
    cd.write()


def test_collect_coverage_reads_unreadable_data_files_through_hostfs(
    tmp_path: Path, unshare: FakeUnshare, monkeypatch: pytest.MonkeyPatch
) -> None:
    d = tmp_path / "data"
    _write_coverage_part(d)
    plain = lc.collect_coverage(d, root_prefix="src")
    part = next(d.glob(".coverage.*"))
    monkeypatch.setattr(lc, "_host_can_open", lambda _p: False)
    monkeypatch.setattr(hostfs, "_read", _deny)
    via_hostfs = lc.collect_coverage(d, root_prefix="src")
    assert via_hostfs == plain == {"r1": {"src/app.py": {3, 4}}}
    assert unshare.calls == [[*UNSHARE, "cat", "--", str(part)]]


def test_control_file_writes_and_acks_go_through_hostfs(
    tmp_path: Path, unshare: FakeUnshare, monkeypatch: pytest.MonkeyPatch
) -> None:
    pd = lc.PdCov(tmp_path)
    real_write = Path.write_text

    def write_text(self: Path, data: str, *a: Any, **k: Any) -> int:
        if self.name == "context.host.tmp":
            raise PermissionError(13, "tool dir not writable")
        return real_write(self, data, *a, **k)

    monkeypatch.setattr(Path, "write_text", write_text)
    monkeypatch.setattr(hostfs, "_copyfile", _deny)  # the plain copy into it is denied too
    lc._write_atomic(pd.context_file, "req-1")
    assert pd.context_file.read_text() == "req-1"
    assert [c[len(UNSHARE)] for c in unshare.calls] == ["cp"]
    monkeypatch.setattr(hostfs, "_read", _deny)
    pd.ack_file.write_text("req-1")
    assert lc._read_ack(pd.ack_file) == "req-1"
    assert unshare.calls[-1] == [*UNSHARE, "cat", "--", str(pd.ack_file)]


# --------------------------------------------------------------------------
# R6: odd-text and binary diffs
# --------------------------------------------------------------------------

ODD = "x\x0c y\x1c z\x1d w\x1e v\x0b u\x85 t  s  r\r"


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-c", "core.quotepath=off", "-C", str(repo), *args],
        capture_output=True,
        check=True,
        timeout=60,
        text=True,
    )
    return proc.stdout.strip()


@pytest.fixture
def odd_repo(tmp_path: Path) -> tuple[Path, str, str]:
    """A synthetic repo (never a real app): base and head differ in odd text and binary files."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "commit.gpgsign", "false")
    base_lines = [f"line{i}" for i in range(1, 10)]
    (repo / "odd.py").write_bytes(("\n".join(base_lines) + "\n").encode())
    (repo / "latin1.txt").write_bytes(b"caf\xe9 one\nsecond\n")
    (repo / "bin.dat").write_bytes(bytes(range(256)) + b"\x00base")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    head_lines = list(base_lines)
    head_lines[2] = ODD  # line 3 holds every odd separator and a trailing CR
    head_lines.append("# a comment")  # added line 10: not executable
    (repo / "odd.py").write_bytes(("\n".join(head_lines) + "\n").encode("utf-8"))
    (repo / "latin1.txt").write_bytes(b"caf\xe9 two\nsecond\n")
    (repo / "bin.dat").write_bytes(bytes(range(255, -1, -1)) + b"\x00head")
    (repo / "new.bin").write_bytes(b"\x00\x01\x02new")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "head")
    return repo, base, _git(repo, "rev-parse", "HEAD")


def test_split_diff_lines_splits_on_newline_only() -> None:
    text = "a\x0cb\x1cc d\x85e\x0bf\r\ng\n"
    assert split_diff_lines(text) == ["a\x0cb\x1cc d\x85e\x0bf\r", "g"]
    assert split_diff_lines("") == [] and split_diff_lines("x") == ["x"]
    assert split_diff_lines("x\n\n") == ["x", ""]


def test_from_diff_counts_odd_separators_as_content() -> None:
    diff = (
        "diff --git a/odd.py b/odd.py\n"
        "--- a/odd.py\n+++ b/odd.py\n"
        "@@ -1,3 +1,3 @@\n"
        " one\n"
        "-two\x0c still two\n"
        "+TWO  still TWO\x85\n"
        " three\r\n"
    )
    cl = ChangedLines.from_diff(diff)
    assert cl["odd.py"] == {"added": {2}, "removed": {2}}


def test_from_diff_skips_binary_sections_including_the_git_binary_patch() -> None:
    diff = (
        "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x\n+y\n"
        "diff --git a/p.bin b/p.bin\nindex 1..2 100644\nGIT binary patch\nliteral 12\n"
        "zcmZQzU|?VZVBp~1Wn=&F\n--- a/evil.py\n+++ b/evil.py\nxcmZQz\n\nliteral 3\nxcmZQz\n\n"
        "diff --git a/q.bin b/q.bin\nindex 1..2 100644\nBinary files a/q.bin and b/q.bin differ\n"
        "diff --git a/b.py b/b.py\n--- a/b.py\n+++ b/b.py\n@@ -4 +4 @@\n-m\n+n\n"
    )
    cl = ChangedLines.from_diff(diff)
    assert set(cl) == {"a.py", "b.py"} and cl.binary_files == {"p.bin", "q.bin"}
    assert cl["b.py"] == {"added": {4}, "removed": {4}}


def test_git_diff_of_odd_and_binary_files_goes_through_from_diff(
    odd_repo: tuple[Path, str, str],
) -> None:
    repo, base, head = odd_repo
    raw = pipeline.diff_bytes_of(repo, base, head)
    text = pipeline.decode_diff(raw)  # the diff stage's own two calls
    assert text == pipeline.diff_text_of(repo, base, head)
    cl = ChangedLines.from_diff(text)
    assert cl["odd.py"] == {"added": {3, 10}, "removed": {3}}
    assert cl["latin1.txt"] == {"added": {1}, "removed": {1}}
    assert cl.binary_files == {"bin.dat", "new.bin"} and "bin.dat" not in cl
    # the same history with real binary patches (git diff --binary)
    binary = pipeline.decode_diff(
        pipeline.git_bytes(repo, "diff", "--no-color", "--binary", "-U3", base, head)
    )
    assert "GIT binary patch" in binary
    cl2 = ChangedLines.from_diff(binary)
    assert cl2["odd.py"] == cl["odd.py"] and cl2.binary_files == {"bin.dat", "new.bin"}


def test_parse_diff_text_and_executable_view_on_odd_and_binary_diffs(
    odd_repo: tuple[Path, str, str],
) -> None:
    repo, base, head = odd_repo
    text = pipeline.diff_text_of(repo, base, head)
    changed = ChangedLines.from_diff(text)
    dt = parse_diff_text(text)
    assert dt.text_of("odd.py", "added", 3) == ODD.replace("�", "�")
    assert dt.text_of("odd.py", "added", 10) == "# a comment"
    assert dt.text_of("odd.py", "removed", 3) == "line3"
    new_view = executable_view(changed, dt, "new")
    assert new_view["odd.py"]["added"] == {3}  # line 10 is a comment: not executable
    old_view = executable_view(changed, dt, "old")
    assert old_view["odd.py"]["removed"] == {3}
    assert new_view.binary_files == {"bin.dat", "new.bin"}


def test_other_git_output_readers_split_on_newline_only() -> None:
    """The gate's commit-subject reader and the pipeline's line readers keep their own rule."""
    src = GATE_PATH.read_text(encoding="utf-8")
    assert ".splitlines()" not in src
    assert "splitlines" not in inspect.getsource(lc.ChangedLines.from_diff)


# --------------------------------------------------------------------------
# R5: the no-model rehearsal
# --------------------------------------------------------------------------


class _Proof(BaseHTTPRequestHandler):
    def log_message(self, *_a: Any) -> None:
        return None

    def _send(self, status: int, body: dict[str, Any]) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        self._send(200, {"token": "tok-1"}) if self.path == "/login" else self._send(404, {})

    def do_GET(self) -> None:
        ok = self.headers.get("Authorization") == "Bearer tok-1"
        if self.path == "/notes/1":
            self._send(200, {"id": 1}) if ok else self._send(401, {})
        else:
            self._send(200, {})


@pytest.fixture
def proof_server() -> Iterator[str]:
    srv = HTTPServer(("127.0.0.1", 0), _Proof)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{srv.server_port}"
    finally:
        srv.shutdown()
        srv.server_close()


CONTRACT = SimpleNamespace(
    auth={
        "mode": "login",
        "path": "/login",
        "method": "POST",
        "body": {"username": "u", "password": "p"},
        "token_json_path": "token",
    },
    fixture={"repo_name": "python-flask-sqlite"},
    readiness="/health",
)


class FakeApp:
    def __init__(self, url: str) -> None:
        self.base_url = url
        self.session = None
        self.stopped = False

    def server_log(self) -> str:
        return ""

    def stop(self) -> None:
        self.stopped = True


class FakeSide:
    """A SideDriver double: counts starts; ``fail_start`` makes that start raise."""

    root_prefix = ""

    def __init__(self, label: str, url: str, fail_start: int | None = None) -> None:
        self.label, self.sha, self.url = label, label * 8, url
        self.fail_start = fail_start
        self.starts: list[bool] = []
        self.apps: list[FakeApp] = []
        self.closed = False

    def start(self, *, measured: bool) -> FakeApp:
        self.starts.append(measured)
        if self.fail_start == len(self.starts):
            raise lean_run.LeanEnvironmentError("app_not_ready", "SECRET-LOG-TAIL")
        app = FakeApp(self.url)
        self.apps.append(app)
        return app

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def unit_repo(tmp_path: Path) -> tuple[Path, Any]:
    repo = tmp_path / "golden"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "commit.gpgsign", "false")
    for i in (1, 2):
        (repo / "f.txt").write_text(f"v{i}\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", f"c{i}")
    unit = gate.Unit("golden-bug", "bug", "golden", "sandbox", "HEAD~1", "HEAD", None)
    return repo, unit


def _cfg(tmp_path: Path, provision: Any, **kw: Any) -> Any:
    return gate.GateConfig(
        out=tmp_path / "out", workdir=tmp_path / "work", which="golden", provision=provision, **kw
    )


def test_rehearsal_starts_each_side_three_times_and_sends_the_authenticated_request(
    tmp_path: Path,
    unit_repo: tuple[Path, Any],
    proof_server: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GROQ_API_KEY", raising=False)  # the rehearsal must run without the key
    repo, unit = unit_repo
    sides = {"base": FakeSide("base", proof_server), "head": FakeSide("head", proof_server)}

    def provision(rc: Any, contract_file: Any, work: Path, timings: Any) -> Any:
        assert rc.no_sandbox is False  # the real sandbox drivers, not the host ones
        return sides["base"], sides["head"], CONTRACT

    cfg = _cfg(tmp_path, provision)
    rows = gate.rehearse_unit(cfg, unit, gate._Repos(golden=repo))
    assert [r["side"] for r in rows] == ["base", "head"]
    for r in rows:
        assert r["ok"] is True and r["error"] is None and r["measured_result"] is False
        assert r["provision"] == "ok" and r["auth_proof"]["outcome"] == "2xx"
        assert [s["status"] for s in r["starts"]] == ["ok"] * 3
        assert r["request"] == {"path": "/notes/1", "status": 200}
        assert r["duration_seconds"] is not None and r["provision_seconds"] is not None
    for s in sides.values():
        assert s.starts == [False, False, False]  # three starts, all unmeasured
        assert all(a.stopped for a in s.apps) and s.closed


def test_rehearsal_failing_restart_fails_the_side_and_keeps_the_message_out_of_the_row(
    tmp_path: Path, unit_repo: tuple[Path, Any], proof_server: str
) -> None:
    repo, unit = unit_repo
    base = FakeSide("base", proof_server, fail_start=2)
    head = FakeSide("head", proof_server)
    cfg = _cfg(tmp_path, lambda *_a: (base, head, CONTRACT))
    rows = gate.rehearse_unit(cfg, unit, gate._Repos(golden=repo))
    assert rows[0]["ok"] is False and rows[1]["ok"] is True
    assert [s["status"] for s in rows[0]["starts"]] == [
        "ok",
        "error: LeanEnvironmentError: app_not_ready",
        "ok",
    ]
    assert "SECRET-LOG-TAIL" not in json.dumps(rows)


def test_rehearsal_provision_failure_marks_both_sides_and_stops_nothing_else(
    tmp_path: Path, unit_repo: tuple[Path, Any]
) -> None:
    repo, unit = unit_repo

    def boom(*_a: Any) -> Any:
        raise lean_run.LeanEnvironmentError("provisioning_failed", "SECRET-LOG-TAIL")

    rows = gate.rehearse_unit(_cfg(tmp_path, boom), unit, gate._Repos(golden=repo))
    assert [(r["provision"], r["ok"]) for r in rows] == [("failed", False)] * 2
    assert rows[0]["error"] == "LeanEnvironmentError: provisioning_failed"
    assert "SECRET-LOG-TAIL" not in json.dumps(rows)
    log = (tmp_path / "out/units/golden-bug/rehearse-console.log").read_text()
    assert "SECRET-LOG-TAIL" in log  # the artifact has it


def test_rehearsal_unauthenticated_proof_is_not_ok(
    tmp_path: Path, unit_repo: tuple[Path, Any], proof_server: str
) -> None:
    repo, unit = unit_repo
    bad = SimpleNamespace(**{**CONTRACT.__dict__, "auth": {**CONTRACT.auth, "path": "/nope"}})
    base, head = FakeSide("base", proof_server), FakeSide("head", proof_server)
    rows = gate.rehearse_unit(
        _cfg(tmp_path, lambda *_a: (base, head, bad)), unit, gate._Repos(golden=repo)
    )
    assert all(r["ok"] is False and r["auth_proof"]["outcome"] == "unproven" for r in rows)


class _SpyEnv(dict):  # type: ignore[type-arg]
    def __init__(self, data: Any) -> None:
        super().__init__(data)
        self.touched: list[str] = []

    def get(self, key: Any, default: Any = None) -> Any:
        self.touched.append(str(key))
        return super().get(key, default)

    def __getitem__(self, key: Any) -> Any:
        self.touched.append(str(key))
        return super().__getitem__(key)

    def __contains__(self, key: object) -> bool:
        self.touched.append(str(key))
        return super().__contains__(key)


def test_rehearsal_never_touches_the_model_key(
    tmp_path: Path,
    unit_repo: tuple[Path, Any],
    proof_server: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, unit = unit_repo
    spy = _SpyEnv({**os.environ, "GROQ_API_KEY": "sk-must-not-be-read"})
    monkeypatch.setattr(os, "environ", spy)
    monkeypatch.setattr(gate, "rehearsal_units", lambda _w: [unit])
    monkeypatch.setattr(gate, "materialize_golden_repo", lambda _d: (repo, {}))
    base, head = FakeSide("base", proof_server), FakeSide("head", proof_server)
    rows = gate.run_rehearsal(_cfg(tmp_path, lambda *_a: (base, head, CONTRACT)))
    assert [r["ok"] for r in rows] == [True, True]
    assert "GROQ_API_KEY" not in spy.touched
    for fn in (
        gate.run_rehearsal,
        gate.rehearse_unit,
        gate.rehearse_side,
        gate.rehearsal_units,
        gate.rehearsal_log_line,
    ):
        src = inspect.getsource(fn)
        assert "GROQ" not in src and "environ" not in src and "model_client" not in src
    on_disk = (tmp_path / "out" / "rehearsal.jsonl").read_text().splitlines()
    assert [json.loads(x)["side"] for x in on_disk] == ["base", "head"]
    assert "sk-must-not-be-read" not in "".join(on_disk)


def test_rehearsal_exit_code_is_1_when_any_side_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gate, "rehearsal_units", lambda _w: [])
    for rows, code in (
        ([{"ok": True}, {"ok": True}], 0),
        ([{"ok": True}, {"ok": False}], 1),
        ([], 1),
    ):
        monkeypatch.setattr(gate, "run_rehearsal", lambda _cfg, rows=rows: rows)
        argv = [
            "--rehearse",
            "--units",
            "golden",
            "--out",
            str(tmp_path),
            "--workdir",
            str(tmp_path),
        ]
        assert gate.main(argv) == code


def test_rehearsal_selects_sandbox_units_only_and_the_normal_mode_is_unchanged() -> None:
    golden = [u.id for u in gate.rehearsal_units("golden")]
    assert golden == ["golden-bug", "golden-clean"]
    with pytest.raises(gate.GateError):
        gate.rehearsal_units("mutants")
    assert gate.REHEARSE_STARTS == 3
    assert gate.GROUPS == ("all", "golden", "mutants", "paperless")


# -- the workflow ------------------------------------------------------------------------------

WORKFLOW = ROOT / ".github" / "workflows" / "lean-rehearse.yml"


def _wf() -> dict[str, Any]:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def test_rehearse_workflow_has_no_environment_no_secrets_and_least_permissions() -> None:
    data = _wf()
    text = WORKFLOW.read_text(encoding="utf-8")
    on = data.get("on", data.get(True))
    assert list(on) == ["workflow_dispatch"]
    units = on["workflow_dispatch"]["inputs"]["units"]
    assert units["type"] == "choice" and units["options"] == [
        "all",
        "golden",
        "paperless",
        "linkding",
    ]
    assert data["permissions"] == {"contents": "read"}
    assert "concurrency" not in data  # not in the model-calls group
    assert list(data["jobs"]) == ["rehearse"]
    job = data["jobs"]["rehearse"]
    assert "environment" not in job and job["timeout-minutes"] == 240
    code = re.sub(r"(?m)^\s*#.*$", "", text)
    assert "environment" not in code
    assert "secrets." not in text and "GROQ_API_KEY" not in re.sub(r"(?m)^\s*#.*$", "", text)
    assert "proofdeploy-model-calls" not in re.sub(r"(?m)^\s*#.*$", "", text)
    assert "contents: write" not in text and "git push" not in text


def test_rehearse_workflow_actions_are_pinned_like_lean_poc_and_inputs_use_env() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    uses = re.findall(r"uses:\s*(\S+\s+#\s*\S+)", text)
    poc = (ROOT / ".github/workflows/lean-poc.yml").read_text(encoding="utf-8")
    poc_uses = set(re.findall(r"uses:\s*(\S+\s+#\s*\S+)", poc))
    assert uses and set(uses) <= poc_uses
    for u in uses:
        assert re.fullmatch(r"[\w./-]+@[0-9a-f]{40}\s+#\s*\S+", u), u
    steps = _wf()["jobs"]["rehearse"]["steps"]
    for s in steps:
        assert "${{" not in s.get("run", ""), s.get("name")
    run = next(s for s in steps if s.get("name") == "Run the rehearsal")
    assert run["env"] == {"UNITS": "${{ inputs.units }}"}
    assert "experiments/lean-poc/gate.py" in run["run"] and "--rehearse" in run["run"]
    assert steps[-1]["if"] == "${{ always() }}" and steps[-1]["with"]["path"].startswith("/tmp/")


def test_remove_tree_of_a_missing_path_is_a_noop_without_podman(
    tmp_path: Path, unshare: FakeUnshare, monkeypatch: pytest.MonkeyPatch
) -> None:
    def gone(_p: Path) -> None:
        raise FileNotFoundError(2, "gone")

    monkeypatch.setattr(hostfs, "_unlink", gone)  # a race: the file vanishes before unlink
    hostfs.remove_tree(tmp_path / "missing")
    hostfs.remove_tree(tmp_path / "missing2")
    assert unshare.calls == []


def test_restore_sqlite_without_sidecar_files_never_starts_podman(
    tmp_path: Path, unshare: FakeUnshare
) -> None:
    snap, target = tmp_path / "seed.sqlite", tmp_path / "db.sqlite3"
    snap.write_bytes(b"SEEDED")
    target.write_bytes(b"old")
    lean_run.restore_sqlite(snap, target)
    assert target.read_bytes() == b"SEEDED" and unshare.calls == []


def test_judge_diff_readers_split_on_newline_only() -> None:
    from proofdeploy.lean import judge

    diff = "diff --git a/a.py b/a.py\n@@ -1 +1 @@\n-x\n+a\x0cb\u2028@@ c\x85d\n@@ -9 +9 @@\n+z\n"
    hunks = judge.split_hunks(diff)
    assert len(hunks) == 2 and "".join(hunks) == diff
    assert hunks[0].endswith("+a\x0cb\u2028@@ c\x85d\n")
    assert judge.added_lines_text(diff) == "a b @@ c d z"  # whitespace-normalized, one line
