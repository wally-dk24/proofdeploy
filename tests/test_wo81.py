"""WO-8.1 (REG-A): the registered Python lock rule in code.

The rule (docs/registration/python-lock-rule.md items 1-6):

1. A COMPLETE committed lock installs frozen: uv.lock
   (``uv sync --frozen``), poetry.lock (``poetry install --sync``),
   pdm.lock (``pdm sync``), or a requirements file where every spec
   entry carries ``--hash`` (pip ``--require-hashes --no-deps``).
2. Otherwise the runner GENERATES a lock: the declared
   dependencies (pyproject.toml, or the requirements file's pins as
   constraints) are resolved with uv 0.13.0 — pinned, bootstrapped
   into the per-side home's .pd-tools — ``--generate-hashes``, for the
   selected Python version and linux/x86_64, against
   https://pypi.org/simple, with ``--exclude-newer`` set to ONE
   cutoff per unit (the fix commit's committer time for a bug pair,
   the SAME for fix^ and fix; C's committer time for a clean diff).
   The generated lock installs ``--require-hashes --no-deps``.
3. Generation and install happen in the sandbox install container.
4. The evidence records the lock source (committed/generated), the
   generated lock text + sha256, the resolver + version, the
   cutoff, and the index URL.
5. A resolution or install failure is BUILD_FAILED (typed
   environment); the pair is INCONCLUSIVE under the start-failure
   rule.

The WAL-76 gap this closes: on main, ``check_requirements_frozen``
accepted any ``==``-pinned requirements file as a lock, and the
file installed with ``--no-deps`` — missing transitive deps
surfaced only as a start failure. An ``==``-only file is NOT a
complete lock under REG-A; it goes through generation.

Fail-before: on the pre-WO-8.1 code the ``==``-only file checks out
as "frozen", plans carry no lock_source / lock_cutoff, pdm.lock is
not a recognized lockfile, poetry plans lack ``--sync``, records
are schema 1.3.0, and the orchestrator never resolves a cutoff.
"""
import hashlib
import json
import os
import subprocess
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from proofdeploy import executor as executor_mod
from proofdeploy.provisioning import (
    pip_flags,
    plan_provisioning,
    render_argv,
)
from proofdeploy.runner import (
    Provisioner,
    ProvisionStatus,
    check_requirements_frozen,
    find_lockfile,
)

# The registered REG-A values, as literals: the tests pin the
# VALUES the rule registers, not whatever the code's constants say.
GENERATED_LOCKFILE_NAME = ".pd-generated-requirements.txt"
CUTOFF = "2026-01-02T03:04:05Z"

HASHED_REQ = (
    "six==1.17.0 \\\n"
    "    --hash=sha256:ff703d0fc3e08526949441584fca3fc209d20cf45a3f54c0fb84f79a80d0d95b1\n"
)

PYPROJECT_312 = '[project]\nname = "t"\nrequires-python = ">=3.12,<3.13"\n'


def _contract(**kw):
    base = {"build": None, "migrate": None, "seed": None,
            "start": "python app.py", "env": {}}
    base.update(kw)
    return SimpleNamespace(**base)


def _python_snapshot(tmp_path: Path, *, pyproject: str | None = None,
                     requirements: str | None = None,
                     extra: dict[str, str] | None = None) -> Path:
    snap = tmp_path / "snap"
    snap.mkdir()
    if pyproject is not None:
        (snap / "pyproject.toml").write_text(pyproject, encoding="utf-8")
    if requirements is not None:
        (snap / "requirements.txt").write_text(requirements, encoding="utf-8")
    for name, text in (extra or {}).items():
        (snap / name).write_text(text, encoding="utf-8")
    return snap


def _steps(plan, phase="install"):
    return [s for s in plan.steps if s.phase == phase and s.argv is not None]


def _resolve_step(plan):
    for s in _steps(plan):
        if "compile" in (s.argv or []):
            return s
    raise AssertionError(f"no uv compile step in {[s.argv for s in plan.steps]}")


# ---------------------------------------------------------------------------
# Item 1 classification: what counts as a complete lock (WAL-76).
# ---------------------------------------------------------------------------


def test_eq_only_requirements_is_not_a_complete_lock(tmp_path):
    """THE WAL-76 test: an ==-pinned requirements file without
    hashes is NOT a complete lock. On main, check_requirements_frozen
    returned None here (accepted) and the file installed --no-deps.

    Planted bug this catches: ``==``-only treated as complete (the
    old == check restored)."""
    req = tmp_path / "requirements.txt"
    req.write_text("flask==3.1.0\nwerkzeug==3.1.3\n", encoding="utf-8")
    reason = check_requirements_frozen(req)
    assert reason is not None
    assert "no --hash" in reason


def test_hashed_requirements_is_a_complete_lock(tmp_path):
    req = tmp_path / "requirements.txt"
    req.write_text(
        "# comment\n"
        "flask==3.1.0 \\\n"
        "    --hash=sha256:aaaa \\\n"
        "    --hash=sha256:bbbb\n"
        "werkzeug==3.1.3 \\\n"
        "    --hash=sha256:cccc\n",
        encoding="utf-8",
    )
    assert check_requirements_frozen(req) is None


def test_partially_hashed_requirements_is_not_complete(tmp_path):
    req = tmp_path / "requirements.txt"
    req.write_text(
        "flask==3.1.0 \\\n    --hash=sha256:aaaa\nwerkzeug==3.1.3\n",
        encoding="utf-8",
    )
    reason = check_requirements_frozen(req)
    assert reason is not None and "line 3" in reason


def test_unpinned_requirements_is_not_complete(tmp_path):
    req = tmp_path / "requirements.txt"
    req.write_text("flask>=3\n", encoding="utf-8")
    assert check_requirements_frozen(req) is not None


def test_comment_only_requirements_is_trivially_complete(tmp_path):
    req = tmp_path / "requirements.txt"
    req.write_text("# no third-party deps\n", encoding="utf-8")
    assert check_requirements_frozen(req) is None


def test_option_lines_are_not_spec_entries(tmp_path):
    req = tmp_path / "requirements.txt"
    req.write_text(
        "--index-url https://pypi.org/simple\n"
        "flask==3.1.0 \\\n    --hash=sha256:aaaa\n",
        encoding="utf-8",
    )
    assert check_requirements_frozen(req) is None


# ---------------------------------------------------------------------------
# Item 1: committed locks install frozen — and are never regenerated.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "lockfile,manager,argv",
    [
        (
            "uv.lock",
            "uv",
            [
                "{pd_tools}/bin/uv",
                "sync",
                "--frozen",
                "--python",
                "{runtime_bin}",
            ],
        ),
        (
            "poetry.lock",
            "poetry",
            ["{pd_tools}/bin/poetry", "install", "--sync"],
        ),
        ("pdm.lock", "pdm", ["{pd_tools}/bin/pdm", "sync"]),
    ],
)
def test_committed_lockfile_plans_frozen(tmp_path, lockfile, manager, argv):
    snap = _python_snapshot(
        tmp_path, pyproject=PYPROJECT_312, extra={lockfile: "lock-bytes\n"}
    )
    # Even WITH a cutoff available, a committed lock is never
    # regenerated (no resolver steps, no cutoff on the plan).
    plan = plan_provisioning(snap, _contract(), lock_cutoff=CUTOFF)
    assert plan.refusal is None
    assert plan.lockfile_name == lockfile
    assert plan.lock_source == "committed"
    assert plan.lock_cutoff is None
    assert plan.package_manager == manager
    installs = _steps(plan)
    # WO-8.1b: the frozen install is the LAST install step, run by
    # the manager the plan bootstrapped into {pd_tools} by hash —
    # never a bare manager binary off PATH. (Pre-WO-8.1b the plan
    # was the single bare argv and the sandbox refused it.)
    assert installs[-1].argv == argv
    assert any(
        "--require-hashes" in (s.argv or []) for s in installs
    ), installs
    assert find_lockfile(snap, "python") is not None
    assert find_lockfile(snap, "python")[0] == lockfile


def test_hashed_requirements_plans_committed_pip(tmp_path):
    snap = _python_snapshot(
        tmp_path, pyproject=PYPROJECT_312, requirements=HASHED_REQ
    )
    plan = plan_provisioning(snap, _contract(), lock_cutoff=CUTOFF)
    assert plan.refusal is None
    assert plan.lock_source == "committed"
    assert plan.lockfile_name == "requirements.txt"
    assert plan.package_manager == "pip"
    assert plan.pip_require_hashes is True
    # No generation machinery on a committed plan.
    assert all("compile" not in (s.argv or []) for s in plan.steps)
    assert all("{pd_tools}" not in " ".join(s.argv or []) for s in plan.steps)


# ---------------------------------------------------------------------------
# Item 2: the generated lock — plan shape and every flag.
# ---------------------------------------------------------------------------


def test_generated_plan_carries_every_registered_flag(tmp_path):
    """Planted bugs this catches: --exclude-newer dropped (the
    resolve argv loses the flag or the cutoff value); the resolver
    bootstrap weakened (installed by bare version instead of by
    hash from the committed file, or as a global install);
    --no-header dropped (the generated lock's hash becomes
    path-dependent); --no-deps dropped (the sandbox rendering of
    the install step loses it)."""
    snap = _python_snapshot(
        tmp_path, pyproject=PYPROJECT_312, requirements="flask==3.1.0\n"
    )
    plan = plan_provisioning(snap, _contract(), lock_cutoff=CUTOFF)
    assert plan.refusal is None
    assert plan.lock_source == "generated"
    assert plan.lock_resolver == "uv"
    assert plan.lock_resolver_version == "0.13.0"
    assert plan.lock_cutoff == CUTOFF
    assert plan.lock_index_url == "https://pypi.org/simple"
    assert plan.generated_lockfile_name == GENERATED_LOCKFILE_NAME
    # The code's constants are the registered values.
    from proofdeploy.provisioning import LOCK_INDEX_URL, UV_VERSION

    assert UV_VERSION == "0.13.0"
    assert LOCK_INDEX_URL == "https://pypi.org/simple"
    assert plan.lockfile_name == "requirements.txt"
    assert plan.lockfile_sha256 == hashlib.sha256(
        b"flask==3.1.0\n"
    ).hexdigest()
    assert plan.persisted_dirs == ["pd_home:.venv", "pd_home:.pd-tools"]

    installs = _steps(plan)
    # Order: tools venv, bootstrap writer, hash-checked bootstrap
    # install, resolve, frozen install.
    joined = [" ".join(s.argv or []) for s in installs]
    assert "{pd_tools}" in joined[0] and "venv" in joined[0]
    # Addendum 46: the resolver is installed BY HASH from the
    # committed bootstrap requirements file — never a bare version
    # install, never a global install. The writer step materializes
    # the committed file into {pd_tools}; the bootstrap installs
    # from it in hash-checking mode. (tests/test_wo81_addendum46.py
    # pins the file's contents and the recorded sha256.)
    bootstrap = installs[2].argv or []
    assert bootstrap[0] == "{pd_tools}/bin/pip"
    assert "--require-hashes" in bootstrap
    assert "--no-deps" in bootstrap
    assert "--only-binary" in bootstrap and ":all:" in bootstrap
    assert bootstrap[bootstrap.index("-r") + 1] == (
        "{pd_tools}/uv-requirements.txt"
    )
    assert "-g" not in bootstrap and "--global" not in bootstrap
    assert plan.lock_resolver_bootstrap_sha256 is not None

    resolve = _resolve_step(plan)
    argv = resolve.argv or []
    assert argv[0] == "{pd_tools}/bin/uv"
    assert argv[1:3] == ["pip", "compile"]
    assert "{checkout}/requirements.txt" in argv
    assert "--generate-hashes" in argv
    # Addendum 46: no header — the generated lock's recorded sha256
    # must not depend on the checkout path.
    assert "--no-header" in argv
    assert argv[argv.index("--exclude-newer") + 1] == CUTOFF
    assert argv[argv.index("--python-version") + 1] == plan.selected_version
    assert argv[argv.index("--python-platform") + 1] == (
        "x86_64-unknown-linux-gnu"
    )
    assert argv[argv.index("--index-url") + 1] == "https://pypi.org/simple"
    assert argv[argv.index("--output-file") + 1] == (
        f"{{checkout}}/{GENERATED_LOCKFILE_NAME}"
    )
    # The install consumes the GENERATED lock, frozen, both flags
    # in the sandbox rendering.
    install = installs[-1]
    rendered = render_argv(
        install.argv or [],
        {"{venv}": "/pd-home/.venv", "{pd_tools}": "/pd-home/.pd-tools", "{checkout}": "/checkout"},
        pip_flags(plan, sandbox=True),
    )
    assert "--require-hashes" in rendered
    assert "--no-deps" in rendered
    assert f"/checkout/{GENERATED_LOCKFILE_NAME}" in rendered
    assert rendered[0] == "/pd-home/.venv/bin/pip"


def test_generated_plan_from_pyproject_only(tmp_path):
    snap = _python_snapshot(tmp_path, pyproject=PYPROJECT_312)
    plan = plan_provisioning(snap, _contract(), lock_cutoff=CUTOFF)
    assert plan.refusal is None
    assert plan.lock_source == "generated"
    assert plan.lockfile_name == "pyproject.toml"
    resolve = _resolve_step(plan)
    assert "{checkout}/pyproject.toml" in (resolve.argv or [])


def test_generation_without_cutoff_is_a_typed_refusal(tmp_path):
    snap = tmp_path / "snap"
    snap.mkdir()
    (snap / "requirements.txt").write_text("flask==3.1.0\n", encoding="utf-8")
    plan = plan_provisioning(snap, _contract())
    assert plan.refusal is not None
    assert "cutoff" in plan.refusal
    # The refusal still records the lock facts for the evidence.
    assert plan.lock_source == "generated"
    assert plan.lock_resolver_version == "0.13.0"


def test_generated_plan_round_trips_through_dict(tmp_path):
    snap = _python_snapshot(tmp_path, requirements="flask==3.1.0\n")
    plan = plan_provisioning(snap, _contract(), lock_cutoff=CUTOFF)
    from proofdeploy.provisioning import InstallPlan

    data = plan.to_dict()
    assert json.loads(json.dumps(data)) == data
    again = InstallPlan.from_dict(data)
    assert again.lock_source == "generated"
    assert again.lock_cutoff == CUTOFF
    assert again.sha256() == plan.sha256()
    # A pre-WO-8.1 plan dict (no lock keys) still loads, defaults None.
    old = {k: v for k, v in data.items() if not k.startswith("lock_")}
    old.pop("generated_lockfile_name", None)
    legacy = InstallPlan.from_dict(old)
    assert legacy.lock_source is None
    assert legacy.lock_cutoff is None


# ---------------------------------------------------------------------------
# Items 2+4+5 at the orchestrator: one cutoff per unit, recorded;
# a resolution failure is BUILD_FAILED and the pair INCONCLUSIVE.
# (Stubbed-sandbox harness, the Addendum 18 / WO-7.3 pattern.)
# ---------------------------------------------------------------------------

MINI_APP = '''\
import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer

VALUE = __VALUE__


class Handler(BaseHTTPRequestHandler):
    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._json(200, {"ok": True})
        elif self.path == "/value":
            self._json(200, {"v": VALUE})
        else:
            self._json(404, {"error": "not found"})

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()
'''

MINI_YML = """\
build: "python -m py_compile app.py"
start: "python app.py"
readiness: "/health"
env:
  APP_ENV: verification
fixture:
  repo_name: "mini-app"
  seed_note: "empty; create users with POST /register"
  auth_mechanism: "Bearer token in the Authorization header"
  auth_scope: "a regular user token"
  flags_note: "no feature flags"
"""

GENERATED_LOCK_TEXT = (
    "# generated by the fake resolver\n"
    "flask==3.1.0 \\\n"
    "    --hash=sha256:0123456789abcdef0123456789abcdef"
    "0123456789abcdef0123456789abcdef\n"
)

# Explicit, DISTINCT committer times per commit: fix (2026-01-02)
# differs from fix^ (2025-07-01), so a per-side cutoff plant hands
# fix^ the wrong value and the pair test catches it.
_COMMIT_DATES = {
    "base": "2025-06-01T00:00:05 +0000",
    "bug": "2025-07-01T00:00:05 +0000",
    "fix": "2026-01-02T03:04:05 +0000",
    "clean": "2026-02-03T04:05:06 +0000",
}
FIX_CUTOFF = "2026-01-02T03:04:05Z"
CLEAN_CUTOFF = "2026-02-03T04:05:06Z"


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            body, code = {"ok": True}, 200
        elif self.path == "/value":
            body, code = {"v": 1}, 200
        else:
            body, code = {"error": "not found"}, 404
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):
        pass


@pytest.fixture()
def app_server():
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{port}"
    srv.shutdown()
    srv.server_close()


@pytest.fixture(autouse=True)
def _proxy_free_opener(monkeypatch):
    opener = urllib.request.build_opener(
        executor_mod._NoRedirect, urllib.request.ProxyHandler({})
    )
    monkeypatch.setattr(executor_mod, "_OPENER", opener)


def _git(repo: Path, *args: str, env: dict | None = None) -> str:
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    p = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=True, env=full_env,
    )
    return p.stdout.strip()


def _make_lock_repo(root: Path) -> dict[str, str]:
    """A mini repo whose requirements.txt is ==-only (NOT a complete
    lock): every measured side must go through generation."""
    repo = root / "mini-app"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "test")
    _git(repo, "config", "user.email", "test@test")
    (repo / "requirements.txt").write_text("flask==3.1.0\n", encoding="utf-8")
    (repo / "proofdeploy.yml").write_text(MINI_YML, encoding="utf-8")
    shas = {}
    for tag, value in (
        ("base", 1), ("bug", 2), ("fix", 1), ("clean", 1),
    ):
        app_text = MINI_APP.replace("__VALUE__", str(value))
        if tag == "clean":
            app_text += "\n# clean change: no behavior difference\n"
        (repo / "app.py").write_text(app_text)
        _git(repo, "add", "-A")
        date = _COMMIT_DATES[tag]
        _git(
            repo, "commit", "-q", "-m", tag,
            env={"GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date},
        )
        shas[tag] = _git(repo, "rev-parse", "HEAD")
    return {"repo": str(repo), **shas}


def _canned_probes(prompt: str):
    from proofdeploy.model_client import ModelResponse

    assert "mini-app" in prompt
    return ModelResponse(
        content=(
            "```json\n"
            '{"setup": [], '
            '"act": {"method": "GET", "path": "/value"}, '
            '"assert": [{"type": "body", "json_path": {"path": "v", "equals": 1}}]}'
            "\n```\n"
        ),
        finish_reason="stop",
    )


class _FakeSandbox:
    """Stands in for proofdeploy.sandbox.Sandbox. Install steps
    succeed — and the step whose argv carries ``compile`` writes
    the generated lock into the checkout, like the real resolver —
    unless ``fail_resolve`` makes the resolve step exit 1."""

    instances: list = []
    app_url: str = ""
    fail_resolve: bool = False

    def __init__(self, config):
        self.config = config
        self.log: list[str] = []
        self.failed_step_output = None
        self.index = len(_FakeSandbox.instances)
        _FakeSandbox.instances.append(self)

    def install_steps(self, checkout, steps, env, proxy_env, timeout=600):
        results = []
        for s in steps:
            argv = s.get("argv_rendered") or []
            rc = 0
            if "compile" in argv:
                if _FakeSandbox.fail_resolve:
                    rc = 1
                    self.failed_step_output = (
                        "error: No solution found when resolving "
                        "dependencies (fake resolver)"
                    )
                else:
                    (Path(checkout) / GENERATED_LOCKFILE_NAME).write_text(
                        GENERATED_LOCK_TEXT, encoding="utf-8"
                    )
            results.append({"phase": s["phase"], "exit_code": rc})
            if rc != 0:
                break
        return results

    def start(self, checkout, command, env, port, name, app_port=None):
        from proofdeploy.sandbox import SandboxApp

        app = SandboxApp(name=name, sandbox=self, target_url=_FakeSandbox.app_url)
        app.sandbox_evidence = {
            "app_network_mode": self.config.network_mode,
            "install_network": "bridge:pd-build",
            "userns_mode": "userns-remap",
            "userns_opt_out": False,
            "app_uid": 0,
            "install_ca_used": False,
        }
        return app

    def app_logs(self, name, tail_lines=100):
        return ""

    def stop(self, name):
        pass

    def isolation_evidence_best_effort(self):
        return {"app_network_mode": self.config.network_mode}


def _stubbed_orchestrator(monkeypatch, tmp_path, app_url):
    from proofdeploy import orchestrator as orch_mod
    from proofdeploy.orchestrator import MeasureConfig, Orchestrator

    monkeypatch.setattr(orch_mod, "assert_measurement_gate", lambda: {})
    monkeypatch.setattr(
        orch_mod,
        "assert_harness_commit_gate",
        lambda: {
            "commit": "0" * 40,
            "source": "git",
            "dirty": False,
            "workflow_ref": None,
        },
    )
    monkeypatch.setattr(orch_mod, "check_podman", lambda: (True, ""))
    monkeypatch.setattr(
        orch_mod, "check_sandbox_runtime", lambda *a: ("3.12.15", "sha256:fake", None)
    )
    _FakeSandbox.instances = []
    _FakeSandbox.app_url = app_url
    _FakeSandbox.fail_resolve = False
    monkeypatch.setattr(orch_mod, "Sandbox", _FakeSandbox)
    monkeypatch.setattr(
        orch_mod, "resolve_network_mode",
        lambda requested: ("none+socket", "stubbed capability reason"),
        raising=False,
    )

    info = _make_lock_repo(tmp_path)
    out = tmp_path / "records"
    work = tmp_path / "work"
    cfg = MeasureConfig(
        repo_dir=Path(info["repo"]),
        output_dir=out,
        workdir=work,
        no_skill=True,
        bug_id="wo81-mini",
        rev_bug_base=info["base"],
        rev_bug=info["bug"],
        rev_fix=info["fix"],
        model_runner=_canned_probes,
        public_contract_env_keys=["APP_ENV"],
        sandbox=True,
    )
    return Orchestrator(cfg), out, info


def _evidence(out, kind="bug"):
    from proofdeploy.runpair import read_records

    records = read_records(out)
    ev = [
        r for r in records
        if r.get("record_type") == "evidence" and r.get("run_kind") == kind
    ]
    assert ev, [r.get("record_type") for r in records]
    return records, ev[0]


def test_same_cutoff_on_both_sides_and_lock_evidence(
    monkeypatch, tmp_path, app_server
):
    """The bug pair resolves with ONE cutoff — the FIX commit's
    committer time — on BOTH sides, and the evidence records the
    lock source, the generated lock text + sha256, the resolver +
    version, the cutoff and the index on both sides.

    Planted bug this catches: a per-side cutoff — fix^'s committer
    time (2025-07-01) differs from fix's (2026-01-02), so resolving
    per side hands the parent side the wrong value."""
    orch, out, _ = _stubbed_orchestrator(monkeypatch, tmp_path, app_server)
    summary = orch.measure_bug()
    assert summary["parent_ready"] is True, summary
    assert summary["fix_ready"] is True, summary
    _, ev = _evidence(out)
    want_sha = hashlib.sha256(GENERATED_LOCK_TEXT.encode()).hexdigest()
    for key in ("fix_parent_provision", "fix_provision"):
        prov = ev[key]
        plan = prov["install_plan"]
        assert plan["lock_source"] == "generated", plan
        assert plan["lock_cutoff"] == FIX_CUTOFF, plan
        assert plan["lock_resolver"] == "uv"
        assert plan["lock_resolver_version"] == "0.13.0"
        assert plan["lock_index_url"] == "https://pypi.org/simple"
        assert prov["generated_lock"]["text"] == GENERATED_LOCK_TEXT
        assert prov["generated_lock"]["sha256"] == want_sha
        # The resolve step that actually ran carried this cutoff.
        resolve = next(
            s for s in prov["executed_steps"]
            if "compile" in (s.get("argv_rendered") or [])
        )
        argv = resolve["argv_rendered"]
        assert argv[argv.index("--exclude-newer") + 1] == FIX_CUTOFF
        assert resolve["exit_code"] == 0


def test_clean_cutoff_is_cs_committer_time(monkeypatch, tmp_path, app_server):
    """A clean diff resolves with C's committer time."""
    from proofdeploy.orchestrator import MeasureConfig, Orchestrator

    orch, out, info = _stubbed_orchestrator(
        monkeypatch, tmp_path, app_server
    )
    cfg = MeasureConfig(
        repo_dir=Path(info["repo"]),
        output_dir=out,
        workdir=tmp_path / "work-clean",
        no_skill=True,
        clean_id="wo81-mini-clean",
        rev_clean=info["clean"],
        model_runner=_canned_probes,
        public_contract_env_keys=["APP_ENV"],
        sandbox=True,
    )
    Orchestrator(cfg).measure_clean()
    _, ev = _evidence(out, kind="clean")
    plan = ev["c_provision"]["install_plan"]
    assert plan["lock_source"] == "generated"
    assert plan["lock_cutoff"] == CLEAN_CUTOFF
    assert ev["c_provision"]["generated_lock"]["sha256"] == (
        hashlib.sha256(GENERATED_LOCK_TEXT.encode()).hexdigest()
    )


def test_resolution_failure_is_build_failed_pair_inconclusive(
    monkeypatch, tmp_path, app_server
):
    """Item 5: the resolver exits non-zero -> both sides
    BUILD_FAILED (typed environment), the run records INCONCLUSIVE,
    never a silent pass or a scored verdict."""
    orch, out, _ = _stubbed_orchestrator(monkeypatch, tmp_path, app_server)
    _FakeSandbox.fail_resolve = True
    summary = orch.measure_bug()
    assert summary["parent_ready"] is False
    assert summary["fix_ready"] is False
    records, ev = _evidence(out)
    for key in ("fix_parent_provision", "fix_provision"):
        prov = ev[key]
        assert prov["status"] == "build_failed", prov
        assert "No solution found" in (prov["reason"] or ""), prov["reason"]
        assert prov["generated_lock"] is None
    scores = [r for r in records if r.get("record_type") == "score"]
    assert scores and scores[0]["verdict"] == "inconclusive", scores


def test_host_side_provision_passes_the_run_cutoff(monkeypatch, tmp_path):
    """The host executor path gets the same one cutoff: the run's
    value when measure_* set it, else the side's own commit time
    (a direct single-side provision is its own unit)."""
    from proofdeploy.orchestrator import MeasureConfig, Orchestrator
    from proofdeploy.runner import ProvisionResult

    info = _make_lock_repo(tmp_path)
    captured: dict = {}

    class _Runner:
        def provision(self, **kw):
            captured.update(kw)
            return ProvisionResult(status=ProvisionStatus.BUILD_FAILED,
                                   reason="stub runner")

    cfg = MeasureConfig(
        repo_dir=Path(info["repo"]),
        output_dir=tmp_path / "records",
        workdir=tmp_path / "work",
        no_skill=True,
        sandbox=False,
        allow_unsandboxed=True,
    )
    orch = Orchestrator(cfg)
    unit = tmp_path / "unit"
    unit.mkdir()
    orch._provision_side(_Runner(), info["fix"], 18999, unit, "fix")
    # No run cutoff: a LAZY fallback the adapter evaluates only if
    # it plans a generated lock; it yields the side's own time (a
    # lone side is its own unit).
    fallback = captured["lock_cutoff"]
    assert callable(fallback)
    assert fallback() == FIX_CUTOFF
    orch._run_lock_cutoff = "2020-01-01T00:00:00Z"
    orch._provision_side(_Runner(), info["bug"], 18999, unit, "fix-parent")
    assert captured["lock_cutoff"] == "2020-01-01T00:00:00Z"  # run's wins


def test_run_cutoff_cleared_after_measure_bug(monkeypatch, tmp_path, app_server):
    """Addendum 46 should-fix: the run cutoff must not leak out of
    measure_*. After measure_bug returns, a direct single-side
    provision falls back to the SIDE'S OWN commit time — not the
    finished run's fix cutoff."""
    from proofdeploy.runner import ProvisionResult

    orch, out, info = _stubbed_orchestrator(monkeypatch, tmp_path, app_server)
    summary = orch.measure_bug()
    assert summary["parent_ready"] is True, summary
    assert orch._run_lock_cutoff is None

    captured: dict = {}

    class _Runner:
        def provision(self, **kw):
            captured.update(kw)
            return ProvisionResult(status=ProvisionStatus.BUILD_FAILED,
                                   reason="stub runner")

    unit = tmp_path / "unit2"
    unit.mkdir()
    orch._provision_side(_Runner(), info["bug"], 18999, unit, "fix-parent")
    fallback = captured["lock_cutoff"]
    assert callable(fallback)
    # The bug commit's own committer time (2025-07-01) — a stale
    # leak would hand back the finished run's FIX_CUTOFF string
    # (2026-01-02) instead.
    assert fallback() == "2025-07-01T00:00:05Z"


def test_committed_lock_never_evaluates_the_cutoff(tmp_path):
    """A lazy cutoff is evaluated ONLY for a generated lock: a
    committed-lock plan (and a Node plan) must not touch the
    caller's cutoff source at all — planning stays pure."""

    def boom():
        raise AssertionError("cutoff evaluated for a committed lock")

    snap = _python_snapshot(
        tmp_path, pyproject=PYPROJECT_312, requirements=HASHED_REQ
    )
    plan = plan_provisioning(snap, _contract(), lock_cutoff=boom)
    assert plan.lock_source == "committed"
    # A generated plan DOES evaluate it, exactly for the cutoff.
    snap2 = tmp_path / "snap2"
    snap2.mkdir()
    (snap2 / "requirements.txt").write_text(
        "flask==3.1.0\n", encoding="utf-8"
    )
    plan2 = plan_provisioning(snap2, _contract(), lock_cutoff=lambda: CUTOFF)
    assert plan2.lock_cutoff == CUTOFF


# ---------------------------------------------------------------------------
# Host executor: the generated lock is captured into the provision.
# ---------------------------------------------------------------------------


def test_host_provision_captures_generated_lock(tmp_path):
    snap = tmp_path / "snap"
    snap.mkdir()
    (snap / "requirements.txt").write_text("requests==2.32.3\n", encoding="utf-8")

    def fake_exec(cmd, cwd, timeout, logs, env):
        logs.append(f"$ {' '.join(cmd)} (mocked)")
        if "--output-file" in cmd:
            out = Path(cmd[cmd.index("--output-file") + 1])
            out.write_text(GENERATED_LOCK_TEXT, encoding="utf-8")
        return True, None

    prov = Provisioner(workdir=tmp_path)
    prov._exec = fake_exec  # type: ignore[method-assign]
    result = prov.provision(
        snapshot_dir=snap,
        contract=_contract(start=""),
        port=18097,
        lock_cutoff=CUTOFF,
    )
    # No start command: the run bails AFTER install — with the
    # generated lock already captured into the provision.
    assert result.status == ProvisionStatus.BUILD_FAILED
    assert result.generated_lock is not None
    assert result.generated_lock["text"] == GENERATED_LOCK_TEXT
    assert result.generated_lock["sha256"] == hashlib.sha256(
        GENERATED_LOCK_TEXT.encode()
    ).hexdigest()
    assert result.install_plan is not None
    assert result.install_plan["lock_source"] == "generated"
    assert result.install_plan["lock_cutoff"] == CUTOFF


def test_host_provision_missing_generated_lock_fails(tmp_path):
    """A generated plan whose resolver produced no file is a
    BUILD_FAILED, never a silent pass."""
    snap = tmp_path / "snap"
    snap.mkdir()
    (snap / "requirements.txt").write_text("requests==2.32.3\n", encoding="utf-8")

    def fake_exec(cmd, cwd, timeout, logs, env):
        logs.append(f"$ {' '.join(cmd)} (mocked)")
        return True, None  # the resolver "succeeds" but writes nothing

    prov = Provisioner(workdir=tmp_path)
    prov._exec = fake_exec  # type: ignore[method-assign]
    result = prov.provision(
        snapshot_dir=snap,
        contract=_contract(start="python app.py"),
        port=18098,
        lock_cutoff=CUTOFF,
    )
    assert result.status == ProvisionStatus.BUILD_FAILED
    assert result.reason is not None
    assert "generated lock missing" in result.reason
    assert result.generated_lock is None


# ---------------------------------------------------------------------------
# Schema 1.7.0: D211 adds model_finish_reason / model_usage; older
# records still load.
# ---------------------------------------------------------------------------


def test_schema_140_and_old_records_still_load(tmp_path):
    from proofdeploy.probe import SCHEMA_VERSION
    from proofdeploy.runpair import build_evidence_record, read_records

    assert SCHEMA_VERSION == "1.7.0"
    record = build_evidence_record(run_kind="bug", bug_id="WO81-SCHEMA")
    assert record["schema_version"] == "1.7.0"
    # A 1.3.0-shaped record (provisions without lock fields) loads.
    old = dict(record)
    old["schema_version"] = "1.3.0"
    runs = tmp_path / "runs.jsonl"
    runs.write_text(json.dumps(old) + "\n", encoding="utf-8")
    loaded = read_records(tmp_path)
    assert len(loaded) == 1
    assert loaded[0]["schema_version"] == "1.3.0"
