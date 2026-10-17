"""D101 item 3b: golden-app fixture descriptions must be literally true.

The rendered author text for every golden app says the seed
creates exactly one user — "seed user seeduser/seedpass owns
two notes after seed: id 1 (done=false) and id 2 (done=true)" —
and the template adds "No other users, roles, or credentials
exist beyond what the seed creates." The presented ${AUTH_TOKEN}
must therefore belong to that seed user, and the seeded rows
must be exactly the rows the text states.

Two untruths were fixed. First, the contracts declared
`mode: register`, so the provider registered a disposable
`runner-*` user per run and presented ITS token: a user the
text says does not exist, owning none of the seed data the
text describes. The contracts now declare `mode: login` with
the seed user's credentials, so the provider signs in as
seeduser and the text stays true as written. Second (D146):
the seeds themselves disagreed across revisions — base, bug
and intermediate inserted only note id 1, while clean,
fix-parent, fix and lockmismatch also inserted note id 2 — so
no single seed_note could be true on every revision. The
note-2 insert was added to the variant-A seeds; every revision
of every app now seeds exactly seeduser plus notes 1 and 2.
Third (D147): node-bun-sqlite's contracts declared
`repo_name: "node-pnpm-sqlite"` — a copy-paste slip from cloning
that app — so the rendered text opened "Repository:
node-pnpm-sqlite", a false statement about which repository the
author is looking at. The contracts now name their own app, and
a static test pins every contract's repo_name to its app
directory.

The live tests exercise the real provider path: the contract
is loaded by the real loader, the app is provisioned with the
contract's own migrate/seed/start commands, and the Executor's
credential provider (config = the contract's auth mapping,
exactly what Orchestrator._credential_config passes for
non-ghost modes) acquires the token. The identity assertion
is the app's own owner-scoped read: GET /notes/1 and
GET /notes/2 return 200 only for the token of the user who
owns those notes — the seed user. The seed-truth tests run a
real migrate+seed and compare the rows in the database with
the rows parsed out of the fixture text itself, so the text
and the data can never drift apart silently again.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.request
from contextlib import contextmanager
from pathlib import Path

import pytest

from proofdeploy.executor import Executor, Verdict
from proofdeploy.fixture import render_description
from proofdeploy.yml import load_contract

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "conformance" / "golden"
TEMPLATE = (ROOT / "proofdeploy" / "fixture_template.txt").read_text(encoding="utf-8")

APPS = (
    "node-express-sqlite",
    "python-flask-sqlite",
    "node-pnpm-sqlite",
    "node-pnpm-monorepo",
    "node-bun-sqlite",
)
REVISIONS = (
    "base",
    "bug",
    "clean",
    "fix",
    "fix-parent",
    "intermediate",
    "lockmismatch",
)

_SEED_TEXT_RE = re.compile(
    r"seed user (\S+)/(\S+) owns two notes after seed: "
    r"id 1 \(done=(true|false)\) and id 2 \(done=(true|false)\)"
)


def _contract(app: str, revision: str):
    return load_contract(GOLDEN / app / "revisions" / revision / "proofdeploy.yml")


def _described_seed(app: str, revision: str) -> tuple[str, str, list[tuple[int, int]]]:
    """The (username, password, [(note id, done), ...]) the fixture TEXT states."""
    note = _contract(app, revision).fixture["seed_note"]
    m = _SEED_TEXT_RE.search(note)
    assert m, f"{app}/{revision}: seed_note states no two-note seed: {note!r}"
    notes = [(1, 1 if m.group(3) == "true" else 0), (2, 1 if m.group(4) == "true" else 0)]
    return m.group(1), m.group(2), notes


def _described_seed_user(app: str, revision: str) -> tuple[str, str]:
    username, password, _notes = _described_seed(app, revision)
    return username, password


# ------------------------------------------------------------------
# Static truth: text, provider declaration, and per-revision identity
# ------------------------------------------------------------------


@pytest.mark.parametrize("app", APPS)
@pytest.mark.parametrize("revision", REVISIONS)
def test_provider_signs_in_as_the_described_seed_user(app, revision):
    username, password = _described_seed_user(app, revision)
    auth = _contract(app, revision).auth
    assert auth is not None
    assert auth["mode"] == "login"
    assert auth["path"] == "/login"
    assert auth["body"] == {"username": username, "password": password}
    assert auth["token_json_path"] == "token"


@pytest.mark.parametrize("app", APPS)
@pytest.mark.parametrize("revision", REVISIONS)
def test_fixture_repo_name_is_the_app_directory(app, revision):
    """D147: the rendered text names the repository it describes;
    the fixture's repo_name must be the app's own directory name."""
    assert _contract(app, revision).fixture["repo_name"] == app


@pytest.mark.parametrize("app", APPS)
def test_rendered_text_and_auth_identical_on_every_revision(app):
    renders = set()
    auths = set()
    for revision in REVISIONS:
        contract = _contract(app, revision)
        renders.add(render_description(TEMPLATE, contract.fixture))
        auths.add(repr(sorted((contract.auth or {}).items())))
    assert len(renders) == 1, f"{app}: rendered text differs across revisions"
    assert len(auths) == 1, f"{app}: auth config differs across revisions"
    # The one rendered text names the seed user whose token is presented.
    (render,) = renders
    assert (
        "seed user seeduser/seedpass owns two notes after seed: "
        "id 1 (done=false) and id 2 (done=true)"
    ) in render


# ------------------------------------------------------------------
# Provisioning machinery shared by the live and seed-truth tests
# ------------------------------------------------------------------


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(scope="session")
def _python_shim(tmp_path_factory) -> Path:
    """The contracts invoke `python`; make it resolve to this interpreter."""
    shim = tmp_path_factory.mktemp("pyshim")
    (shim / "python").symlink_to(sys.executable)
    return shim


# One dependency install per Node app, from its base revision, with
# the app's own pinned toolchain; every revision then symlinks the
# installed node_modules (all revisions of an app share one lock).
_INSTALL_COMMANDS = {
    "node-express-sqlite": ["npm", "ci", "--no-audit", "--no-fund"],
    "node-pnpm-sqlite": ["npx", "--yes", "pnpm@9.15.0", "install", "--frozen-lockfile"],
    "node-pnpm-monorepo": ["npx", "--yes", "pnpm@9.15.0", "install", "--frozen-lockfile"],
    "node-bun-sqlite": ["bun", "install", "--frozen-lockfile"],
}
_MODULE_LINKS = {
    "node-express-sqlite": ["node_modules"],
    "node-pnpm-sqlite": ["node_modules"],
    "node-pnpm-monorepo": ["node_modules", "packages/notes-app/node_modules"],
    "node-bun-sqlite": ["node_modules"],
}


@pytest.fixture(scope="session")
def _golden_installs(tmp_path_factory) -> dict[str, list[tuple[str, Path]]]:
    """{app: [(link path inside a revision copy, installed dir), ...]}."""
    installs: dict[str, list[tuple[str, Path]]] = {}
    for app, command in _INSTALL_COMMANDS.items():
        template = tmp_path_factory.mktemp(f"install-{app}") / "app"
        shutil.copytree(GOLDEN / app / "revisions" / "base", template)
        subprocess.run(
            command,
            cwd=template,
            check=True,
            capture_output=True,
            text=True,
            timeout=900,
        )
        installs[app] = [(rel, template / rel) for rel in _MODULE_LINKS[app]]
    return installs


@contextmanager
def _provisioned_golden(app: str, revision: str, tmp_path: Path, shim: Path,
                        installs: dict[str, list[tuple[str, Path]]]):
    """Copy one revision, link its deps, run the contract's migrate+seed.

    Yields (checkout dir, contract). The contract steps run in the
    app member directory (contract.app_path), as provisioning does.
    """
    src = GOLDEN / app / "revisions" / revision
    dst = tmp_path / f"{app}-{revision}"
    shutil.copytree(src, dst)
    for rel, target in installs.get(app, []):
        # Symlink, not copy: the app only reads node_modules, and a
        # copy per revision would multiply the suite's tmp footprint
        # for no behavioural difference.
        link = dst / rel
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(target)
    contract = load_contract(dst / "proofdeploy.yml")
    workdir = dst / contract.app_path if contract.app_path else dst
    assert contract.database is not None
    env = dict(
        os.environ,
        APP_DB=str(dst / contract.database["path"]),
        PATH=f"{shim}{os.pathsep}{os.environ['PATH']}",
    )
    for cmd in (contract.migrate, contract.seed):
        assert cmd
        r = subprocess.run(
            cmd, shell=True, cwd=workdir, env=env, capture_output=True, text=True,
            timeout=300,
        )
        assert r.returncode == 0, (cmd, r.stdout, r.stderr)
    yield dst, contract


@contextmanager
def _running_golden(app: str, revision: str, tmp_path: Path, shim: Path,
                    installs: dict[str, list[tuple[str, Path]]]):
    with _provisioned_golden(app, revision, tmp_path, shim, installs) as (dst, contract):
        workdir = dst / contract.app_path if contract.app_path else dst
        env = dict(
            os.environ,
            APP_DB=str(dst / contract.database["path"]),
            PATH=f"{shim}{os.pathsep}{os.environ['PATH']}",
        )
        port = _free_port()
        env["PORT"] = str(port)
        proc = subprocess.Popen(
            contract.start,
            shell=True,
            cwd=workdir,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        base = f"http://127.0.0.1:{port}"
        try:
            deadline = time.time() + 60
            while True:
                try:
                    with urllib.request.urlopen(base + contract.readiness, timeout=2) as resp:
                        if resp.status == 200:
                            break
                except Exception:
                    if time.time() > deadline:
                        raise RuntimeError(
                            f"{app}/{revision} never became ready"
                        ) from None
                    time.sleep(0.25)
            yield base, contract
        finally:
            # Kill the whole process group: the shell's child IS the
            # server, and terminating the shell alone would orphan it
            # (the conftest T5 guard fails the session on lingerers).
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(timeout=30)


# ------------------------------------------------------------------
# Seed truth: the seeded rows equal what the fixture text states
# ------------------------------------------------------------------


@pytest.mark.parametrize("app", APPS)
@pytest.mark.parametrize("revision", REVISIONS)
def test_seeded_rows_equal_what_seed_note_states(
    app, revision, tmp_path, _python_shim, _golden_installs
):
    username, password, expected_notes = _described_seed(app, revision)
    with _provisioned_golden(
        app, revision, tmp_path, _python_shim, _golden_installs
    ) as (dst, contract):
        db_path = dst / contract.database["path"]
        conn = sqlite3.connect(db_path)
        users = conn.execute("SELECT username, password FROM users").fetchall()
        assert users == [(username, password)], (
            f"{app}/{revision}: seed created users {users}, "
            f"text states exactly {username}/{password}"
        )
        uid = conn.execute(
            "SELECT id FROM users WHERE username = ?", (username,)
        ).fetchone()[0]
        notes = conn.execute(
            "SELECT id, done FROM notes ORDER BY id"
        ).fetchall()
        assert notes == expected_notes, (
            f"{app}/{revision}: seed created notes {notes}, "
            f"text states {expected_notes}"
        )
        owners = conn.execute("SELECT DISTINCT user_id FROM notes").fetchall()
        assert owners == [(uid,)], (
            f"{app}/{revision}: notes owned by {owners}, not only the seed user"
        )


# ------------------------------------------------------------------
# Live truth: the token the provider presents owns the seeded notes
# ------------------------------------------------------------------


def _note_probe(note_id: int):
    return {
        "act": {
            "method": "GET",
            "path": f"/notes/{note_id}",
            "headers": {"Authorization": "Bearer ${AUTH_TOKEN}"},
        },
        "assert": [{"type": "status", "equals": 200}],
    }


@pytest.mark.parametrize("app", APPS)
@pytest.mark.parametrize("revision", REVISIONS)
def test_presented_token_belongs_to_the_described_seed_user(
    app, revision, tmp_path, _python_shim, _golden_installs
):
    with _running_golden(
        app, revision, tmp_path, _python_shim, _golden_installs
    ) as (base, contract):
        # The executor's config is the contract's auth mapping — the
        # same content Orchestrator._credential_config passes (these
        # contracts declare no bootstrap_env, which is the only key
        # that method drops for non-ghost modes).
        assert "bootstrap_env" not in (contract.auth or {})
        ex = Executor(
            target_url=base,
            provisioned=True,
            credential_config=dict(contract.auth or {}),
        )
        for note_id in (1, 2):
            r = ex.run_probe(_note_probe(note_id))
            assert r.verdict == Verdict.PASS, r.details
            assert any("POST /login (login)" in d for d in r.details), r.details


@pytest.mark.parametrize("app", APPS)
def test_plant_register_mode_token_is_not_the_seed_users(
    app, tmp_path, _python_shim, _golden_installs
):
    """Plant: the pre-fix register provider presents a disposable
    user's token; that token must NOT be able to read note id 1."""
    with _running_golden(
        app, "fix", tmp_path, _python_shim, _golden_installs
    ) as (base, contract):
        register_cfg = {
            "mode": "register",
            "path": "/register",
            "method": "POST",
            "username_field": "username",
            "password_field": "password",
            "token_json_path": "token",
        }
        ex = Executor(target_url=base, provisioned=True, credential_config=register_cfg)
        r = ex.run_probe(_note_probe(1))
        assert r.verdict == Verdict.FAIL, r.details
