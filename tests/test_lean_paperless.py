"""Lean round 2, WO-R2-P (D258, D262, D266, D267 item 5): paperless observability.

Pure and fake-driver tests (a tiny HTTP server, temp git repos, stub drivers);
the golden host run at the end proves the loop's records on the real app.
No model is called anywhere and no secret value is ever asserted by value
into a committed file: the "generated" values below are made-up constants.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

import pytest
from lean_helpers import NoModel, PromptStub, bug_rule

# the golden-app fixtures of the pipeline tests (module-scoped venv, CI env, runners)
from test_lean_pipeline import ci_env, golden, run_golden  # noqa: F401

from proofdeploy.lean import pipeline
from proofdeploy.lean import run as lean_run
from proofdeploy.lean.coverage import ChangedLines
from proofdeploy.lean.generate import CoverResult, ScenarioRunner
from proofdeploy.lean.http import (
    AuthSpec,
    HttpClient,
    Scenario,
    auth_from_contract,
    expand_env,
    unresolved_placeholders,
)
from proofdeploy.lean.pathclass import (
    classify_path,
    is_backend_source,
    is_migration_path,
    paths_by_class,
)
from proofdeploy.lean.pipeline import (
    AUTH_PROOF_PATHS,
    LeanConfig,
    LeanRefusal,
    decode_diff,
    diff_bytes_of,
    diff_text_of,
    function_body_lines,
    not_observable_result,
    prove_auth,
    run_pipeline,
    side_bootstrap_env,
)
from proofdeploy.lean.verdict import INCONCLUSIVE

ROOT = Path(__file__).resolve().parent.parent

# Made-up stand-ins for the per-side generated bootstrap values (never real secrets).
GEN_USER = "gen-user-Qw3rTy9a"
GEN_PASS = "gen-pass-Zx8CvBn2"
TOKEN = "tok-from-the-fake-server"

PAPERLESS_AUTH = {
    "mode": "login",
    "path": "/api/token/",
    "method": "POST",
    "body": {"username": "${PAPERLESS_ADMIN_USER}", "password": "${PAPERLESS_ADMIN_PASSWORD}"},
    "token_json_path": "token",
    "bootstrap_env": ["PAPERLESS_ADMIN_USER", "PAPERLESS_ADMIN_PASSWORD"],
}
PAPERLESS_FIXTURE = {
    "repo_name": "paperless-ngx/paperless-ngx",
    "auth_mechanism": "token in the form 'Token <token>' in the Authorization header",
}
SIDE_ENV = {"PAPERLESS_ADMIN_USER": GEN_USER, "PAPERLESS_ADMIN_PASSWORD": GEN_PASS}


def _contract(auth: dict[str, Any] | None = None, repo: str | None = None) -> Any:
    fixture = dict(PAPERLESS_FIXTURE)
    if repo is not None:
        fixture["repo_name"] = repo
    return SimpleNamespace(auth=auth, fixture=fixture)


# --------------------------------------------------------------------------
# A fake paperless-like server: records what is sent
# --------------------------------------------------------------------------


class FakeServer:
    def __init__(self, *, profile_status: int | None = None, echo_user: bool = False) -> None:
        self.requests: list[dict[str, Any]] = []
        self.profile_status = profile_status
        self.echo_user = echo_user
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a: Any) -> None:
                return

            def _reply(self, code: int, body: Any) -> None:
                raw = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _record(self) -> dict[str, Any]:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"null") if n else None
                rec = {
                    "method": self.command,
                    "path": self.path,
                    "body": body,
                    "authorization": self.headers.get("Authorization"),
                }
                outer.requests.append(rec)
                return rec

            def do_POST(self) -> None:
                rec = self._record()
                body = rec["body"] or {}
                if self.path == "/api/token/":
                    if body.get("username") == GEN_USER and body.get("password") == GEN_PASS:
                        extra = {"user": body["username"]} if outer.echo_user else {}
                        self._reply(200, {"token": TOKEN, **extra})
                    else:
                        self._reply(400, {"non_field_errors": ["Unable to log in"]})
                else:
                    self._reply(404, {})

            def do_GET(self) -> None:
                rec = self._record()
                if self.path == "/api/profile/":
                    if outer.profile_status is not None:
                        self._reply(outer.profile_status, {"detail": "x"})
                    elif rec["authorization"] == f"Token {TOKEN}":
                        self._reply(200, {"username": "admin"})
                    else:
                        self._reply(
                            401, {"detail": "Authentication credentials were not provided."}
                        )
                else:
                    self._reply(404, {})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture()
def server() -> Any:
    s = FakeServer()
    yield s
    s.close()


class FakeLive:
    def __init__(self, url: str) -> None:
        self.base_url = url
        self.session = None
        self.stopped = False

    def server_log(self) -> str:
        return ""

    def stop(self) -> None:
        self.stopped = True


class FakeDriver:
    """A side driver whose app is the fake server; counts how often it was started."""

    def __init__(
        self,
        label: str,
        url: str | None,
        env: dict[str, str] | None = None,
        *,
        has_env: bool = True,
    ) -> None:
        self.label = label
        self.sha = label * 8
        self.root_prefix = ""
        self.url = url
        self.starts = 0
        self._env = dict(env or {})
        if not has_env:
            self.bootstrap_env = None  # type: ignore[assignment]

    def bootstrap_env(self) -> dict[str, str]:
        return dict(self._env)

    def start(self, *, measured: bool) -> FakeLive:
        self.starts += 1
        if self.url is None:
            raise AssertionError("no side may be started")
        return FakeLive(self.url)

    def flask_rules(self, app: Any, target: str) -> None:
        return None

    def describe(self) -> dict[str, Any]:
        return {}

    def close(self) -> None:
        return None


def _login_auth() -> AuthSpec:
    spec = auth_from_contract(_contract(PAPERLESS_AUTH))
    assert spec is not None
    return spec


# --------------------------------------------------------------------------
# Temp git repos
# --------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def _repo(
    tmp: Path, base_files: dict[str, bytes], head_files: dict[str, bytes], name: str = "r"
) -> tuple[Path, str, str]:
    repo = tmp / name
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "user.email", "t@invalid")
    for files, msg in ((base_files, "base"), (head_files, "head")):
        for rel, data in files.items():
            f = repo / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(data)
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", msg)
    return repo, _git(repo, "rev-parse", "HEAD~1"), _git(repo, "rev-parse", "HEAD")


def _cfg(repo: Path, base: str, head: str, **kw: Any) -> LeanConfig:
    return LeanConfig(repo=repo, base=base, head=head, **kw)


VIEWS_BASE = b"def view():\n    x = 1\n    return x\n"
VIEWS_HEAD = b"def view():\n    x = 2\n    return x\n"


# --------------------------------------------------------------------------
# Item 2: the round-1 root cause and the sign-in proof
# --------------------------------------------------------------------------


def test_expand_env_reads_only_the_given_side_env_never_the_host_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 1: ``expand_env`` read ``os.environ``; the generated values live only on the side."""
    monkeypatch.setenv("PAPERLESS_ADMIN_USER", "host-env-value")
    body = {"username": "${PAPERLESS_ADMIN_USER}", "n": [1, "x${PAPERLESS_ADMIN_USER}"]}
    assert expand_env(body, {}) == body  # the host value is never used
    assert expand_env(body, {"PAPERLESS_ADMIN_USER": "side-value"}) == {
        "username": "side-value",
        "n": [1, "xside-value"],
    }
    assert unresolved_placeholders(body) == ["PAPERLESS_ADMIN_USER"]
    assert unresolved_placeholders({"a": "plain"}) == []


def test_the_login_body_sent_carries_the_sides_generated_values(
    server: FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PAPERLESS_ADMIN_USER", "host-env-value")
    monkeypatch.setenv("PAPERLESS_ADMIN_PASSWORD", "host-env-pass")
    runner = ScenarioRunner(HttpClient(server.url), auth=_login_auth(), env=SIDE_ENV)
    res = runner(Scenario([_login_auth().login], "POST /api/token/"))
    assert server.requests[0]["body"] == {"username": GEN_USER, "password": GEN_PASS}
    assert res.exchanges[0].status == 200
    dumped = json.dumps([e.to_dict() for e in res.exchanges])
    assert GEN_USER not in dumped and GEN_PASS not in dumped and TOKEN not in dumped
    assert "${PAPERLESS_ADMIN_USER}" in dumped  # the recorded request keeps the placeholder


def test_an_unresolvable_placeholder_is_never_sent(
    server: FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PAPERLESS_ADMIN_USER", "host-env-value")
    monkeypatch.setenv("PAPERLESS_ADMIN_PASSWORD", "host-env-pass")
    runner = ScenarioRunner(HttpClient(server.url), auth=_login_auth(), env={})
    res = runner(Scenario([_login_auth().login], "POST /api/token/"))
    assert server.requests == []  # nothing on the wire
    assert res.exchanges[0].status == 0 and res.aborted
    assert "${PAPERLESS_ADMIN_USER}" in str(res.exchanges[0].body_json_or_text)


def test_a_response_that_echoes_a_generated_value_is_redacted_by_value() -> None:
    s = FakeServer(echo_user=True)
    try:
        runner = ScenarioRunner(HttpClient(s.url), auth=_login_auth(), env=SIDE_ENV)
        res = runner(Scenario([_login_auth().login], "POST /api/token/"))
    finally:
        s.close()
    dumped = json.dumps([e.to_dict() for e in res.exchanges])
    assert GEN_USER not in dumped and "<redacted:bootstrap>" in dumped


def test_proof_succeeds_when_the_side_signs_in_and_the_proof_path_is_2xx(
    server: FakeServer,
) -> None:
    driver = FakeDriver("head", server.url, SIDE_ENV)
    proof = prove_auth(driver, _login_auth(), "/api/profile/", side="head")
    assert proof["ok"] and proof["outcome"] == "2xx"
    assert proof["login_status"] == 200 and proof["proof_status"] == 200
    assert server.requests[1]["authorization"] == f"Token {TOKEN}"
    assert GEN_USER not in json.dumps(proof) and GEN_PASS not in json.dumps(proof)


def test_proof_fails_when_the_side_has_no_generated_values_and_sends_nothing(
    server: FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PAPERLESS_ADMIN_USER", GEN_USER)  # the host has them; the side does not
    monkeypatch.setenv("PAPERLESS_ADMIN_PASSWORD", GEN_PASS)
    driver = FakeDriver("head", server.url, has_env=False)
    proof = prove_auth(driver, _login_auth(), "/api/profile/", side="head")
    assert not proof["ok"] and proof["login_status"] == 0 and proof["proof_status"] is None
    assert server.requests == []


def test_proof_fails_when_the_login_is_rejected(server: FakeServer) -> None:
    driver = FakeDriver("base", server.url, {**SIDE_ENV, "PAPERLESS_ADMIN_PASSWORD": "wrong"})
    proof = prove_auth(driver, _login_auth(), "/api/profile/", side="base")
    assert not proof["ok"] and proof["login_status"] == 400 and proof["proof_status"] is None


def test_proof_fails_when_the_authenticated_get_is_401() -> None:
    s = FakeServer(profile_status=401)
    try:
        proof = prove_auth(
            FakeDriver("head", s.url, SIDE_ENV), _login_auth(), "/api/profile/", side="head"
        )
    finally:
        s.close()
    assert not proof["ok"] and proof["proof_status"] == 401


def test_a_5xx_on_the_proof_path_after_a_good_sign_in_is_not_an_unauthenticated_loop() -> None:
    s = FakeServer(profile_status=500)
    try:
        proof = prove_auth(
            FakeDriver("head", s.url, SIDE_ENV), _login_auth(), "/api/profile/", side="head"
        )
    finally:
        s.close()
    assert proof["ok"] and proof["outcome"] == "5xx_past_auth" and proof["proof_status"] == 500


def _pipeline_repo(tmp: Path) -> tuple[Path, str, str]:
    return _repo(tmp, {"app/views.py": VIEWS_BASE}, {"app/views.py": VIEWS_HEAD})


def test_unproven_auth_ends_inconclusive_auth_unproven_with_no_judge_call(
    tmp_path: Path, server: FakeServer
) -> None:
    repo, base, head = _pipeline_repo(tmp_path)
    cfg = _cfg(repo, base, head, unit_repo="paperless-ngx/paperless-ngx")
    bad = {**SIDE_ENV, "PAPERLESS_ADMIN_PASSWORD": "nope"}
    judge = NoModel()
    res = run_pipeline(
        cfg,
        _contract(PAPERLESS_AUTH),
        FakeDriver("base", server.url, bad),
        FakeDriver("head", server.url, bad),
        runner=judge,
    )
    v = res.verdict
    assert v.status == INCONCLUSIVE and v.cause == "auth_unproven"
    assert v.details == {"side": "head", "login_status": 400, "proof_status": None}
    assert judge.calls == 0 and res.record is None
    assert res.manifest["auth_proof"]["head"]["ok"] is False
    dumped = json.dumps(res.manifest)
    assert GEN_USER not in dumped and GEN_PASS not in dumped and TOKEN not in dumped


def test_a_unit_with_auth_but_no_proof_path_is_refused(tmp_path: Path) -> None:
    repo, base, head = _pipeline_repo(tmp_path)
    cfg = _cfg(repo, base, head, unit_repo="acme/unknown-app")
    head_driver = FakeDriver("head", None)
    with pytest.raises(LeanRefusal) as exc:
        run_pipeline(
            cfg, _contract(PAPERLESS_AUTH, repo="acme/unknown-app"), head_driver, head_driver
        )
    assert exc.value.code == "auth_proof_unspecified" and head_driver.starts == 0


def test_no_auth_declared_is_recorded_and_the_run_proceeds(tmp_path: Path) -> None:
    repo, base, head = _repo(
        tmp_path, {"app/views.py": b"# a\nx = 1\n"}, {"app/views.py": b"# b\nx = 1\n"}
    )
    d = FakeDriver("head", None)
    res = run_pipeline(_cfg(repo, base, head), _contract(None), d, d)
    assert res.manifest["auth_proof"] == "no_auth_declared"
    assert res.verdict.status == "PASS" and d.starts == 0


def test_proof_paths_match_the_readiness_auth_proof_registry() -> None:
    from readiness.auth_proof import PROOF_SPECS

    assert (
        AUTH_PROOF_PATHS["paperless-ngx/paperless-ngx"]
        == PROOF_SPECS["paperless-ngx/paperless-ngx"].path
    )
    assert AUTH_PROOF_PATHS["golden/python-flask-sqlite"] == "/notes/1"


def test_the_sandbox_side_hands_out_the_values_it_was_provisioned_with() -> None:
    side = lean_run.SandboxSide.__new__(lean_run.SandboxSide)
    side.side = SimpleNamespace(bootstrap_env=dict(SIDE_ENV))
    assert side_bootstrap_env(side) == SIDE_ENV
    side.side = SimpleNamespace(bootstrap_env={})
    assert side_bootstrap_env(side) == {}
    assert side_bootstrap_env(SimpleNamespace()) == {}  # a driver with no method has none


# --------------------------------------------------------------------------
# Item 3: load-time declarations
# --------------------------------------------------------------------------

MOD_BASE = (
    b'__version__ = "1.0"\n'  # 1
    b"\n"
    b"def view():\n"  # 3
    b"    x = 1\n"  # 4
    b"    return x\n"  # 5
)
MOD_HEAD = MOD_BASE.replace(b'"1.0"', b'"2.0"').replace(b"x = 1", b"x = 2")


class _LoadSession:
    def __init__(self, load: dict[str, set[int]]) -> None:
        self.load = load

    def snapshot(self, *, root_prefix: str = "") -> dict[str, dict[str, set[int]]]:
        return {"": self.load}


def _settle(tmp: Path, load_lines: set[int], unexercised: list[int], key: str = "added") -> Any:
    repo, base, head = _repo(tmp, {"pkg/mod.py": MOD_BASE}, {"pkg/mod.py": MOD_HEAD})
    cfg = _cfg(repo, base, head)
    diff_text = diff_text_of(repo, base, head)
    changed = ChangedLines.from_diff(diff_text)
    result = CoverResult(
        impact_set=[],
        hits={},
        unexercised={
            "pkg/mod.py": {
                "added": unexercised if key == "added" else [],
                "removed": unexercised if key == "removed" else [],
            }
        },
        requests_used=0,
        scenarios_run=0,
        covered=False,
        requests_to_cover=None,
        budget_requests=1,
    )
    return pipeline._settle_load_only(
        result,
        _LoadSession({"pkg/mod.py": load_lines}),  # type: ignore[arg-type]
        changed,
        diff_text,
        key,
        "",
        source_of=pipeline.source_reader(cfg, key),
    )


def test_a_module_level_assignment_that_ran_at_load_is_a_declaration_not_unexercised(
    tmp_path: Path,
) -> None:
    out = _settle(tmp_path, load_lines={1, 3}, unexercised=[1])
    assert out.load_only == ["pkg/mod.py:1"] and out.unexercised == {}


def test_a_changed_line_inside_a_view_that_no_request_hit_stays_unexercised(
    tmp_path: Path,
) -> None:
    out = _settle(tmp_path, load_lines={1, 3}, unexercised=[1, 4])
    assert out.load_only == ["pkg/mod.py:1"]
    assert out.unexercised == {"pkg/mod.py": {"added": [4], "removed": []}}


def test_a_function_body_line_is_never_load_time_even_if_it_ran_at_load(tmp_path: Path) -> None:
    out = _settle(tmp_path, load_lines={1, 3, 4}, unexercised=[4])  # called while importing
    assert out.load_only == [] and out.unexercised["pkg/mod.py"]["added"] == [4]


def test_a_module_level_line_that_did_not_run_at_load_is_unexercised(tmp_path: Path) -> None:
    out = _settle(tmp_path, load_lines={3}, unexercised=[1])
    assert out.load_only == [] and out.unexercised["pkg/mod.py"]["added"] == [1]


def test_removed_module_level_lines_are_read_from_the_base_file(tmp_path: Path) -> None:
    out = _settle(tmp_path, load_lines={1, 3}, unexercised=[1], key="removed")
    assert out.load_only == ["pkg/mod.py:1"]


def test_function_body_lines_cover_bodies_not_the_signature_or_decorators() -> None:
    src = (
        "import os\n"  # 1
        "@deco\n"  # 2
        "def f(a,\n"  # 3
        "      b=os.sep):\n"  # 4
        "    y = 1\n"  # 5
        "    def inner():\n"  # 6
        "        return y\n"  # 7
        "    return inner\n"  # 8
        "class C:\n"  # 9
        "    attr = 3\n"  # 10
        "    def m(self): return 1\n"  # 11
        "z = lambda: 1\n"  # 12
    )
    assert function_body_lines(src) == {5, 6, 7, 8}
    assert function_body_lines("def (:\n") is None


# --------------------------------------------------------------------------
# Item 4: diffs are read as bytes
# --------------------------------------------------------------------------


def test_a_latin1_file_never_crashes_the_diff_and_the_hash_is_over_the_raw_bytes(
    tmp_path: Path,
) -> None:
    latin = "# caf\xe9 au lait\n".encode("latin-1")  # comments only: no executable line
    repo, base, head = _repo(
        tmp_path, {"app/views.py": b"# x\n"}, {"app/views.py": latin, "app/legacy.py": latin}
    )
    raw = diff_bytes_of(repo, base, head)
    assert b"\xe9" in raw
    text = diff_text_of(repo, base, head)
    assert "caf\ufffd au lait" in text
    assert decode_diff(raw) == text
    d = FakeDriver("head", None)
    res = run_pipeline(_cfg(repo, base, head), _contract(None), d, d)
    assert res.manifest["inputs"]["diff_sha256"] == hashlib.sha256(raw).hexdigest()
    assert res.manifest["inputs"]["diff_sha256"] != hashlib.sha256(text.encode()).hexdigest()


def test_a_latin1_source_file_and_commit_message_never_crash_a_unit(tmp_path: Path) -> None:
    src = '# caf\xe9\n__version__ = "1"\n'.encode("latin-1")
    repo, base, head = _repo(tmp_path, {"pkg/mod.py": b"# x\n"}, {"pkg/mod.py": src})
    msg = tmp_path / "msg.txt"
    msg.write_bytes("fix caf\xe9\n".encode("latin-1"))
    _git(repo, "-c", "i18n.commitEncoding=latin1", "commit", "--amend", "-q", "-F", str(msg))
    head = _git(repo, "rev-parse", "HEAD")
    cfg = _cfg(repo, base, head)
    text = pipeline.source_reader(cfg, "added")("pkg/mod.py")
    assert text is not None and "__version__" in text
    assert function_body_lines(text) == set()
    intent, source = pipeline.intent_of(repo, head, None)
    assert source == "head_commit_message" and intent.startswith("fix caf")


# --------------------------------------------------------------------------
# Item 5: frontend-only changes typed up front
# --------------------------------------------------------------------------

PARITY_PATHS = [
    "src/paperless/views.py",
    "src/documents/models.py",
    "src/documents/migrations/0001_initial.py",
    "src/documents/Migrate.py",
    "alembic/versions/abc.py",
    "src/db/migration_helpers.py",
    "src-ui/src/app/x.ts",
    "src-ui/src/app/app.component.html",
    "src-ui/package.json",
    "src/locale/x.po",
    "src/locale/en/LC_MESSAGES/django.mo",
    "src/paperless_testing/x.py",
    "src/documents/tests/test_api.py",
    "tests/unit/foo.py",
    "src/Tests/helper.py",
    "pkg/models_test.py",
    "src/x/__tests__/a.js",
    "src/conftest.py",
    "src/test_things.py",
    "web/app.test.ts",
    "web/app.spec.tsx",
    "docs/x.py",
    "docs/guide.md",
    "README.md",
    "CHANGELOG",
    "LICENSE-3rd",
    "CONTRIBUTING.rst",
    "notes/design.adoc",
    ".github/workflows/x.yml",
    ".github/scripts/ci.py",
    "Dockerfile",
    "docker/compose/entry.sh",
    "docker-compose.yml",
    "requirements.txt",
    "requirements-dev.txt",
    "uv.lock",
    "poetry.lock",
    "Pipfile",
    "pyproject.toml",
    "package-lock.json",
    "pnpm-lock.yaml",
    "data.txt",
    "src/app/schema.json",
    "scripts/run.sh",
    "static/logo.svg",
    "Makefile",
    "src/app/style.css",
    "src/app/server.go",
    "src/app/Types.PYI",
    "setup.py",
]


def _regc() -> Any:
    script = ROOT / "scripts" / "wo6" / "regc_bug_select.py"
    spec = importlib.util.spec_from_file_location("regc_bug_select_for_pathclass", script)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclasses in the script resolve their module by name
    spec.loader.exec_module(mod)
    return mod


def test_the_path_classes_and_the_migration_rule_match_the_registered_selector() -> None:
    regc = _regc()
    assert len(PARITY_PATHS) >= 40
    seen = set()
    for p in PARITY_PATHS:
        assert classify_path(p) == regc.classify_path(p), p
        assert is_migration_path(p) == regc.is_migration_path(p), p
        seen.add(classify_path(p))
    assert seen == {"SOURCE", "TEST", "DOC", "OTHER"}
    assert classify_path("src-ui/src/app/x.ts") == "SOURCE"
    assert classify_path("src/locale/x.po") == "OTHER"
    assert classify_path("src/paperless_testing/x.py") == "TEST"  # the _testing suffix rule
    assert classify_path("docs/x.py") == "DOC"
    assert classify_path(".github/workflows/x.yml") == "OTHER"


def test_backend_source_needs_source_class_no_migration_and_a_language_extension() -> None:
    assert is_backend_source("src/documents/views.py", "python") is True
    assert is_backend_source("src-ui/src/app/x.ts", "python") is False
    assert is_backend_source("src/documents/migrations/0002_x.py", "python") is False
    assert is_backend_source("src/documents/tests/test_x.py", "python") is False
    assert is_backend_source("docs/x.py", "python") is False
    assert is_backend_source("src/locale/x.po", "python") is False
    assert is_backend_source("src/app/page.html", "python") is False
    assert is_backend_source("src/documents/views.py", "go") is None  # not defined yet
    assert paths_by_class(["a.py", "b.ts", "docs/c.md", "tests/d.py", "e.po"]) == {
        "SOURCE": 2,
        "TEST": 1,
        "DOC": 1,
        "OTHER": 1,
    }


FRONTEND_ONLY = (
    {"src-ui/src/app/x.ts": b"a\n", "src/locale/x.po": b"a\n", "docs/n.md": b"a\n"},
    {"src-ui/src/app/x.ts": b"b\n", "src/locale/x.po": b"b\n", "docs/n.md": b"b\n"},
)


def test_a_frontend_only_change_is_inconclusive_not_observable_with_no_sandbox_and_no_model(
    tmp_path: Path,
) -> None:
    repo, base, head = _repo(tmp_path, *FRONTEND_ONLY)
    d = FakeDriver("head", None)
    judge = NoModel()
    res = run_pipeline(_cfg(repo, base, head), _contract(None), d, d, runner=judge)
    assert res.verdict.status == INCONCLUSIVE and res.verdict.cause == "not_observable_over_http"
    assert res.verdict.details == {
        "reason": "no_backend_source_change",
        "paths_by_class": {"SOURCE": 1, "TEST": 0, "DOC": 1, "OTHER": 1},
    }
    assert d.starts == 0 and judge.calls == 0 and res.record is None


def test_a_migration_only_python_change_is_not_backend_source(tmp_path: Path) -> None:
    repo, base, head = _repo(
        tmp_path,
        {"src/documents/migrations/0002_a.py": b"x = 1\n"},
        {"src/documents/migrations/0002_a.py": b"x = 2\n"},
    )
    out = not_observable_result(_cfg(repo, base, head))
    assert out is not None and out.verdict.cause == "not_observable_over_http"


def test_a_backend_change_alongside_frontend_files_is_not_refused_up_front(tmp_path: Path) -> None:
    repo, base, head = _repo(
        tmp_path,
        {**FRONTEND_ONLY[0], "src/documents/views.py": VIEWS_BASE},
        {**FRONTEND_ONLY[1], "src/documents/views.py": VIEWS_HEAD},
    )
    assert not_observable_result(_cfg(repo, base, head)) is None


def test_an_undefined_language_proceeds_as_before(tmp_path: Path) -> None:
    repo, base, head = _repo(tmp_path, *FRONTEND_ONLY)
    assert not_observable_result(_cfg(repo, base, head, language="go")) is None


def test_lean_verify_types_a_frontend_only_change_before_provisioning(tmp_path: Path) -> None:
    repo, base, head = _repo(tmp_path, *FRONTEND_ONLY)
    rc = lean_run.LeanRunConfig(repo=repo, base=base, head=head, output_dir=tmp_path / "out")
    boom = AssertionError("provisioning must not run")
    with (
        mock.patch.object(lean_run, "provision_sandbox_sides", side_effect=boom),
        mock.patch.object(lean_run, "provision_host_sides", side_effect=boom),
    ):
        result, code = lean_run.lean_verify(rc)
    assert code == 2 and result.verdict.cause == "not_observable_over_http"
    written = json.loads((tmp_path / "out" / "manifest.json").read_text(encoding="utf-8"))
    assert written["verdict"]["details"]["reason"] == "no_backend_source_change"


# --------------------------------------------------------------------------
# Item 1: every loop request is recorded (golden app, host mode)
# --------------------------------------------------------------------------


def test_the_coverage_loop_requests_are_recorded_with_counts_and_the_auth_flag(
    golden: dict[str, Any],  # noqa: F811
    ci_env: None,  # noqa: F811
    tmp_path: Path,
) -> None:
    code, m = run_golden(golden, "base", "bug", tmp_path / "out", PromptStub(bug_rule))
    assert code == 1
    out = tmp_path / "out"
    rows = [
        json.loads(line)
        for line in (out / "exchanges.cover_head.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert rows and all(
        {"method", "path", "status", "scenario_id", "step", "auth_sent"} <= set(r) for r in rows
    )
    assert all(isinstance(r["auth_sent"], bool) for r in rows)
    assert any(r["auth_sent"] for r in rows) and any(
        not r["auth_sent"] for r in rows
    )  # login is bare
    loop = m["cover_loop"]["head"]
    assert loop["requests"] == len(rows) == m["coverage"]["head"]["requests_used"]
    assert loop["2xx"] + loop["4xx"] + loop["5xx"] + loop["no_response"] == loop["requests"]
    assert loop["with_auth"] == sum(1 for r in rows if r["auth_sent"]) > 0
    rates = m["impacted_operations_loop_head"]["GET /notes/{note_id}"]
    assert 0 < rates["2xx_rate"] <= 1 and rates["requests"] >= 1
    assert "impacted_operations_loop_base" in m
    base_ran = "skipped" not in m["coverage"]["base"]
    assert base_ran == (out / "exchanges.cover_base.jsonl").exists()
    if base_ran:  # the base side's loop is recorded the same way
        n_base = len((out / "exchanges.cover_base.jsonl").read_text(encoding="utf-8").splitlines())
        assert m["cover_loop"]["base"]["requests"] == n_base > 0
        assert m["auth_proof"]["base"]["outcome"] == "2xx"
    # the sign-in proof ran inside the side and was recorded
    assert m["auth_proof"]["head"]["outcome"] == "2xx" and m["auth_proof"]["head"]["ok"]
    blob = "".join(p.read_text(encoding="utf-8") for p in out.glob("exchanges.cover_*.jsonl"))
    assert "Authorization" not in blob  # the flag is a boolean, never the header
