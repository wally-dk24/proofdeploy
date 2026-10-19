# ruff: noqa: E501  (long embedded stub JSON / fixture lines)
"""Addendum 23 — WO-5.2 R1 at the orchestrator level.

A full measured run (real provisioning of both sides, real probe
execution, real records) against an app with login: each probe's setup
logs in and captures the session token under the same name. Because
fix^ and fix are separate app instances, each side mints its OWN token
for each probe — four distinct session values under one capture name.
Before the R1 fix only the first value was collected for redaction, so
the other three were committed to the records branch in plaintext.
"""

import json
import subprocess
from pathlib import Path

from proofdeploy.model_client import ModelResponse
from proofdeploy.orchestrator import MeasureConfig, Orchestrator
from proofdeploy.runpair import REDACTED, RUNS_LOG_NAME, read_records

LOGIN_APP = '''\
import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer

VALUE = __VALUE__


def _session(user):
    return "sess-%d-%s-0123456789abcdef" % (VALUE, user)


class Handler(BaseHTTPRequestHandler):
    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b""
        return json.loads(raw.decode() or "{}")

    def do_GET(self):
        if self.path == "/health":
            self._json(200, {"ok": True})
        elif self.path.startswith("/me/"):
            self._json(200, {"v": VALUE, "session_echo": self.path[len("/me/"):]})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path == "/login":
            data = self._read_json()
            self._json(200, {"session": _session(data.get("user", "anon"))})
        else:
            self._json(404, {"error": "not found"})

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()
'''

LOGIN_YML = """\
build: "python -m py_compile app.py"
start: "python app.py"
readiness: "/health"
env:
  APP_ENV: verification
fixture:
  repo_name: "mini-login"
  seed_note: "empty; log in with POST /login; GET /me/ echoes the session; v equal to 1 on a correct build"
  auth_mechanism: "session token returned by POST /login"
  auth_scope: "a regular user session"
  flags_note: "no feature flags"
"""

# fix^ runs the bug commit (VALUE=2), fix runs VALUE=1: each side's
# login mints that side's token for each probe's user.
EXPECTED_TOKENS = {
    "sess-2-probe0-0123456789abcdef",
    "sess-2-probe1-0123456789abcdef",
    "sess-1-probe0-0123456789abcdef",
    "sess-1-probe1-0123456789abcdef",
}


def _git(repo: Path, *args: str) -> str:
    p = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=True,
    )
    return p.stdout.strip()


def _make_login_repo(root: Path) -> dict[str, str]:
    """base (VALUE=1) -> bug (VALUE=2) -> fix (VALUE=1), with login."""
    repo = root / "mini-login"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "test")
    _git(repo, "config", "user.email", "test@test")
    (repo / "requirements.txt").write_text("# no third-party deps\n")
    (repo / "proofdeploy.yml").write_text(LOGIN_YML)
    shas = {}
    for tag, value in (("base", 1), ("bug", 2), ("fix", 1)):
        (repo / "app.py").write_text(LOGIN_APP.replace("__VALUE__", str(value)))
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", tag)
        shas[tag] = _git(repo, "rev-parse", "HEAD")
    return {"repo": str(repo), **shas}


def _probe_block(user: str) -> str:
    probe = {
        "setup": [
            {
                "type": "http",
                "act": {"method": "POST", "path": "/login", "body": {"user": user}},
                "capture": {"SESSION": {"from": "body", "json_path": "session"}},
            }
        ],
        "act": {"method": "GET", "path": "/me/${SESSION}"},
        "assert": [
            {
                "type": "status",
                "equals": 200,
                "evidence": {"source": "fixture", "quote": "Repository: mini-login"},
            },
            {
                "type": "body",
                "json_path": {"path": "v", "equals": 1},
                "evidence": {
                    "source": "fixture",
                    "quote": "v equal to 1 on a correct build",
                },
            },
        ],
    }
    return "```json\n" + json.dumps(probe) + "\n```\n"


def _canned_login_probes(prompt: str) -> ModelResponse:
    assert "mini-login" in prompt  # the author really saw the bundle
    return ModelResponse(
        content=_probe_block("probe0") + _probe_block("probe1"),
        finish_reason="stop",
    )


def test_measure_with_login_tokens_on_both_sides_redacts_every_token(tmp_path):
    info = _make_login_repo(tmp_path)
    out = tmp_path / "records"
    work = tmp_path / "work"
    skill = (
        Path(__file__).parent.parent
        / "docs"
        / "evaluation"
        / "skill"
        / "skill-author.md"
    )
    cfg = MeasureConfig(
        repo_dir=Path(info["repo"]),
        output_dir=out,
        workdir=work,
        skill_path=skill,
        bug_id="mini-login",
        rev_bug_base=info["base"],
        rev_bug=info["bug"],
        rev_fix=info["fix"],
        model_runner=_canned_login_probes,
        public_contract_env_keys=["APP_ENV"],
        allow_unsandboxed=True,
    )
    summary = Orchestrator(cfg).measure_bug()
    assert summary["parent_ready"] and summary["fix_ready"]
    assert summary["probe_count"] == 2

    blob = (out / RUNS_LOG_NAME).read_text(encoding="utf-8")
    for token in EXPECTED_TOKENS:
        assert token not in blob, "a login token leaked into the committed records"

    evidence = read_records(out)[0]
    captured = evidence["captured_values"]["SESSION"]
    # All four values reached the record, and every one was redacted.
    assert isinstance(captured, list)
    assert len(captured) == 4
    assert set(captured) == {REDACTED}
