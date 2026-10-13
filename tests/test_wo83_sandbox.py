"""WO-8.3 sandbox proofs: auth providers end to end, plus the two
real-app proofs (Addendum 44 items 5-6).

Golden app (tests/fixtures/wo83_auth_app, provisioned through the
real sandbox path):

- login mode: bootstrap credentials generated per side, injected
  into the install + app containers, login via the same values.
- refresh mode: login, then refresh near expiry.
- ghost_jwt: the admin key is created by the app's own seed, written
  to the declared .pd-auth/admin_key, read host-side after READY.
- a missing key file -> INCONCLUSIVE auth, never a crash.
- credentials differ per side; the written evidence record contains
  none of the generated values (and no app-minted canary tokens).

Real apps (no model, no probes beyond one authenticated GET):

- Directus 12.5.0 (release tag v12.5.0, commit
  4f77933bf9551cac6b289576078783e189b8f4e3) via the published npm
  package: login + refresh (SQLite), READY in the sandbox, one
  authenticated GET -> 200, recorded and redacted. The wrapper
  project declares engines.node ^22: the plan's max-satisfying
  selection would otherwise pick a newer line whose V8 the
  isolated-vm 5.0.3 dependency cannot build against (and ships no
  prebuild for); Node 22 is a Directus-supported line with
  prebuilds.
- Ghost 6.69.0 (release tag v6.69.0, commit
  056f6bfa657f2175b924106fcb25c3fc245bb4e7): ghost_jwt (SQLite),
  READY in the sandbox, one authenticated GET -> 200, recorded and
  redacted. The proof vendors the published npm release tarball as
  the project tree (the Ghost-CLI layout): the ghost package's
  `file:components/*.tgz` self-references only resolve when ghost
  is the root project — npm cannot install it as a dependency
  (ENOENT on its own components), which the local experiment for
  this work order confirmed twice.

Runs in the sandbox-tests CI job; locally it skips without a capable
sandbox (in CI a skip fails the job, per PD_REQUIRE_SANDBOX).
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "conformance"))
import kit

from proofdeploy.executor import InconclusiveReason, Verdict
from proofdeploy.orchestrator import MeasureConfig, Orchestrator, _free_port
from proofdeploy.runpair import build_evidence_record, write_evidence_record
from proofdeploy.sandbox import sandbox_evidence

_EVIDENCE = sandbox_evidence()
requires_sandbox = pytest.mark.skipif(
    not (
        _EVIDENCE.get("podman")
        and _EVIDENCE.get("userns_remap")
        and _EVIDENCE.get("build_network")
    ),
    reason=(
        "sandbox unavailable: "
        + ", ".join(
            k for k in ("podman", "userns_remap", "build_network")
            if not _EVIDENCE.get(k)
        )
    ),
)

pytestmark = requires_sandbox

FIXTURE_APP = Path(__file__).parent / "fixtures" / "wo83_auth_app"
CANARY_RE = re.compile(r"pdcanary-[0-9a-f]{16}")

DIRECTUS_VERSION = "12.5.0"
DIRECTUS_TAG = "v12.5.0"
DIRECTUS_SHA = "4f77933bf9551cac6b289576078783e189b8f4e3"
GHOST_VERSION = "6.69.0"
GHOST_TAG = "v6.69.0"
GHOST_SHA = "056f6bfa657f2175b924106fcb25c3fc245bb4e7"


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=True, timeout=300,
    )
    return proc.stdout.strip()


def _make_repo(tmp_path: Path, name: str, files: dict[str, str]) -> tuple[Path, str]:
    repo = tmp_path / name
    repo.mkdir(parents=True)
    for rel, content in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "wo83-test")
    _git(repo, "config", "user.email", "wo83@proofdeploy.invalid")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "wo83 proof fixture")
    return repo, _git(repo, "rev-parse", "HEAD")


def _orch(tmp_path: Path, repo: Path, public_env: list[str]) -> Orchestrator:
    cfg = MeasureConfig(
        repo_dir=repo,
        output_dir=tmp_path / "records",
        workdir=tmp_path / "work",
        no_skill=True,
        sandbox=True,
        public_contract_env_keys=public_env,
    )
    return Orchestrator(cfg)


def _provision(orch: Orchestrator, tmp_path: Path, rev: str, tag: str):
    unit = tmp_path / f"unit-{tag}"
    unit.mkdir(exist_ok=True)
    side = orch._provision_side_sandbox(rev, _free_port(), unit, tag)
    kit.dump_logs(f"wo83-{tag}", side.provision.logs)
    return side


def _stop(side) -> None:
    if side.provision is not None and side.provision.proc is not None:
        side.provision.proc.stop()


def _write_record(tmp_path: Path, orch: Orchestrator, side, results) -> str:
    """Write an evidence record for a side + probe results and return
    the committed record text (what an auditor would read)."""
    record = build_evidence_record(
        run_kind="clean",
        clean_id="wo83-proof",
        auth_bootstrap={"c": orch._auth_bootstrap(side)},
        c_results=results,
        runner_log=list(side.provision.logs if side.provision else []),
    )
    repo = tmp_path / "records-repo"
    repo.mkdir(exist_ok=True)
    if not (repo / ".git").exists():
        _git(repo, "init", "-q")
        _git(repo, "config", "user.name", "wo83-test")
        _git(repo, "config", "user.email", "wo83@proofdeploy.invalid")
    write_evidence_record(
        record, repo, repo, secret_values=orch._record_secret_values([side])
    )
    return (repo / "runs.jsonl").read_text(encoding="utf-8")


def _assert_no_secrets(text: str, values: list[str]) -> None:
    for value in values:
        assert value, "expected a non-empty secret value to check"
        assert value not in text
    assert "***REDACTED***" in text or not values


def _golden_files(contract: str) -> dict[str, str]:
    files = {
        name: (FIXTURE_APP / name).read_text(encoding="utf-8")
        for name in (
            "app.py",
            "migrate.py",
            "seed.py",
            "requirements.txt",
            "pyproject.toml",
        )
    }
    files["proofdeploy.yml"] = contract
    return files


GOLDEN_BASE = """\
build: "true"
start: "python app.py"
readiness: "/health"
migrate: "python migrate.py"
seed: "python seed.py"
"""

LOGIN_CONTRACT = (
    GOLDEN_BASE
    + """\
env:
  APP_DB: app.db
auth:
  mode: login
  path: /login
  method: POST
  body:
    email: "${ADMIN_EMAIL}"
    password: "${ADMIN_PASSWORD}"
  token_json_path: access_token
  bootstrap_env: [ADMIN_EMAIL, ADMIN_PASSWORD]
"""
)

REFRESH_CONTRACT = (
    GOLDEN_BASE
    + """\
env:
  APP_DB: app.db
  TOKEN_TTL_SECONDS: "59"
auth:
  mode: refresh
  path: /auth/refresh
  method: POST
  body:
    refresh_token: "${REFRESH_TOKEN}"
  token_json_path: access_token
  refresh_token_json_path: refresh_token
  expires_in_json_path: expires_in
  login_path: /login
  login_body:
    email: "${ADMIN_EMAIL}"
    password: "${ADMIN_PASSWORD}"
  bootstrap_env: [ADMIN_EMAIL, ADMIN_PASSWORD]
"""
)

GHOST_CONTRACT = (
    GOLDEN_BASE
    + """\
env:
  APP_DB: app.db
auth:
  mode: ghost_jwt
  key_file: .pd-auth/admin_key
  ttl_seconds: 300
  bootstrap_env: [ADMIN_EMAIL, ADMIN_PASSWORD]
"""
)

GHOST_MISSING_KEY_CONTRACT = (
    GOLDEN_BASE
    + """\
env:
  APP_DB: app.db
auth:
  mode: ghost_jwt
  key_file: .pd-auth/never_written
  bootstrap_env: [ADMIN_EMAIL, ADMIN_PASSWORD]
"""
)


def _me_probe() -> dict:
    return {
        "act": {
            "method": "GET",
            "path": "/me",
            "headers": {"Authorization": "Bearer ${AUTH_TOKEN}"},
        },
        "assert": [{"type": "status", "equals": 200}],
    }


def test_golden_login_mode_in_sandbox(tmp_path):
    repo, rev = _make_repo(tmp_path, "golden-login", _golden_files(LOGIN_CONTRACT))
    orch = _orch(tmp_path, repo, ["APP_DB"])
    side = _provision(orch, tmp_path, rev, "golden-login")
    try:
        assert side.provision.ready, "\n".join(side.provision.logs)
        # Generated for this side: names in the log, values nowhere.
        joined = "\n".join(side.provision.logs)
        assert "auth bootstrap: generated ADMIN_EMAIL, ADMIN_PASSWORD" in joined
        assert side.bootstrap_env
        for value in side.bootstrap_env.values():
            assert value not in joined
        executor = orch._make_executor(side)
        result = executor.run_probe(_me_probe())
        assert result.verdict == Verdict.PASS, result.details
        assert any("(login)" in d for d in result.details)
        committed = _write_record(tmp_path, orch, side, [result])
        _assert_no_secrets(committed, list(side.bootstrap_env.values()))
        assert not CANARY_RE.search(committed)
        parsed = json.loads(committed)
        assert parsed["auth_bootstrap"] == {
            "c": {"generated": ["ADMIN_EMAIL", "ADMIN_PASSWORD"]}
        }
    finally:
        _stop(side)


def test_golden_refresh_mode_in_sandbox(tmp_path):
    repo, rev = _make_repo(
        tmp_path, "golden-refresh", _golden_files(REFRESH_CONTRACT)
    )
    orch = _orch(tmp_path, repo, ["APP_DB", "TOKEN_TTL_SECONDS"])
    side = _provision(orch, tmp_path, rev, "golden-refresh")
    try:
        assert side.provision.ready, "\n".join(side.provision.logs)
        executor = orch._make_executor(side)
        probe = {
            "setup": [
                {
                    "type": "http",
                    "act": {
                        "method": "GET",
                        "path": "/me",
                        "headers": {"Authorization": "Bearer ${AUTH_TOKEN}"},
                    },
                }
            ],
            "act": {
                "method": "GET",
                "path": "/me",
                "headers": {"Authorization": "Bearer ${AUTH_TOKEN}"},
            },
            "assert": [{"type": "status", "equals": 200}],
        }
        result = executor.run_probe(probe)
        assert result.verdict == Verdict.PASS, result.details
        # First acquisition logged in; the near-expiry second one hit
        # the refresh endpoint.
        assert any("(login)" in d for d in result.details), result.details
        assert any(
            d == "credential provider: POST /auth/refresh" for d in result.details
        ), result.details
        committed = _write_record(tmp_path, orch, side, [result])
        _assert_no_secrets(committed, list(side.bootstrap_env.values()))
        assert not CANARY_RE.search(committed)
    finally:
        _stop(side)


def test_golden_ghost_jwt_from_seed_key_file_in_sandbox(tmp_path):
    repo, rev = _make_repo(tmp_path, "golden-ghost", _golden_files(GHOST_CONTRACT))
    orch = _orch(tmp_path, repo, ["APP_DB"])
    side = _provision(orch, tmp_path, rev, "golden-ghost")
    try:
        assert side.provision.ready, "\n".join(side.provision.logs)
        # The instance's seed wrote the declared key file.
        key_path = side.snapshot_dir / ".pd-auth" / "admin_key"
        assert key_path.is_file()
        key_value = key_path.read_text(encoding="utf-8").strip()
        executor = orch._make_executor(side)
        # Reading the config registered the key for redaction.
        assert key_value in side.generated_secrets
        probe = {
            "act": {
                "method": "GET",
                "path": "/admin/site",
                "headers": {"Authorization": "Ghost ${AUTH_TOKEN}"},
            },
            "assert": [{"type": "status", "equals": 200}],
        }
        result = executor.run_probe(probe)
        assert result.verdict == Verdict.PASS, result.details
        committed = _write_record(tmp_path, orch, side, [result])
        _assert_no_secrets(
            committed, list(side.bootstrap_env.values()) + [key_value]
        )
        assert not CANARY_RE.search(committed)
    finally:
        _stop(side)


def test_golden_ghost_missing_key_file_inconclusive_in_sandbox(tmp_path):
    repo, rev = _make_repo(
        tmp_path, "golden-ghost-missing", _golden_files(GHOST_MISSING_KEY_CONTRACT)
    )
    orch = _orch(tmp_path, repo, ["APP_DB"])
    side = _provision(orch, tmp_path, rev, "golden-ghost-missing")
    try:
        assert side.provision.ready, "\n".join(side.provision.logs)
        executor = orch._make_executor(side)
        probe = {
            "act": {
                "method": "GET",
                "path": "/admin/site",
                "headers": {"Authorization": "Ghost ${AUTH_TOKEN}"},
            },
            "assert": [{"type": "status", "equals": 200}],
        }
        result = executor.run_probe(probe)  # never a crash
        assert result.verdict == Verdict.INCONCLUSIVE
        assert result.reason == InconclusiveReason.AUTH
        assert any("key file not found" in d for d in result.details)
    finally:
        _stop(side)


def test_golden_bootstrap_values_differ_per_side_in_sandbox(tmp_path):
    repo, rev = _make_repo(tmp_path, "golden-sides", _golden_files(LOGIN_CONTRACT))
    orch = _orch(tmp_path, repo, ["APP_DB"])
    side_a = _provision(orch, tmp_path, rev, "side-a")
    side_b = _provision(orch, tmp_path, rev, "side-b")
    try:
        assert side_a.provision.ready, "\n".join(side_a.provision.logs)
        assert side_b.provision.ready, "\n".join(side_b.provision.logs)
        # Fresh per side: no generated value is shared between sides.
        assert side_a.bootstrap_env
        assert set(side_a.bootstrap_env.values()).isdisjoint(
            side_b.bootstrap_env.values()
        )
        ex_a = orch._make_executor(side_a)
        assert ex_a.run_probe(_me_probe()).verdict == Verdict.PASS
        ex_b = orch._make_executor(side_b)
        assert ex_b.run_probe(_me_probe()).verdict == Verdict.PASS
    finally:
        _stop(side_a)
        _stop(side_b)


# ----------------------------------------------------------------------
# Real-app proofs (Addendum 44 item 6): no model, no probes beyond
# the one authenticated GET each.
# ----------------------------------------------------------------------

DIRECTUS_CONTRACT = """\
build: "true"
start: "directus start"
readiness: "/server/ping"
migrate: "directus bootstrap"
env:
  DB_CLIENT: sqlite3
  DB_FILENAME: /checkout/data.db
auth:
  mode: refresh
  path: /auth/refresh
  method: POST
  body:
    refresh_token: "${REFRESH_TOKEN}"
  token_json_path: data.access_token
  refresh_token_json_path: data.refresh_token
  expires_in_json_path: data.expires
  login_path: /auth/login
  login_body:
    email: "${ADMIN_EMAIL}"
    password: "${ADMIN_PASSWORD}"
  bootstrap_env: [ADMIN_EMAIL, ADMIN_PASSWORD, SECRET]
"""


def _npm_lockfile(repo: Path, *, legacy_peer_deps: bool = False) -> None:
    cmd = ["npm", "install", "--package-lock-only", "--ignore-scripts",
           "--no-audit", "--no-fund"]
    if legacy_peer_deps:
        # Ghost's own tree does not resolve under npm's default peer
        # rules (it pins knex 2.4.2 while its bookshelf 1.2.0 declares
        # peer knex <0.22 — Ghost installs with yarn, which treats
        # peers as advisory). Generation uses the flag, and the proof
        # repo carries an .npmrc with legacy-peer-deps=true so the
        # adapter's plain `npm ci` accepts the frozen lockfile (both
        # halves verified against npm locally for this work order).
        cmd.append("--legacy-peer-deps")
    subprocess.run(cmd, cwd=repo, check=True, capture_output=True, timeout=900)


def test_directus_login_refresh_proof_in_sandbox(tmp_path):
    print(
        f"directus release: tag {DIRECTUS_TAG} commit {DIRECTUS_SHA} "
        f"(npm directus@{DIRECTUS_VERSION})"
    )
    repo_dir = tmp_path / "directus-proof"
    repo_dir.mkdir()
    (repo_dir / "package.json").write_text(
        json.dumps(
            {
                "name": "pd-proof-directus",
                "private": True,
                "version": "1.0.0",
                # Node 22 line: see the module docstring (isolated-vm
                # 5.0.3 prebuilds exist for Node 22; the plan selects
                # the max satisfying image row).
                "engines": {"node": "^22.0.0"},
                "dependencies": {"directus": DIRECTUS_VERSION},
            }
        ),
        encoding="utf-8",
    )
    _npm_lockfile(repo_dir)
    assert (repo_dir / "package-lock.json").is_file()
    repo, rev = _make_repo(
        tmp_path,
        "directus-proof-repo",
        {
            "package.json": (repo_dir / "package.json").read_text("utf-8"),
            "package-lock.json": (repo_dir / "package-lock.json").read_text("utf-8"),
            "proofdeploy.yml": DIRECTUS_CONTRACT,
        },
    )
    orch = _orch(tmp_path, repo, ["DB_CLIENT", "DB_FILENAME"])
    side = _provision(orch, tmp_path, rev, "directus")
    try:
        assert side.provision.ready, "\n".join(side.provision.logs)
        executor = orch._make_executor(side)
        probe = {
            "act": {
                "method": "GET",
                "path": "/users/me",
                "headers": {"Authorization": "Bearer ${AUTH_TOKEN}"},
            },
            "assert": [{"type": "status", "equals": 200}],
        }
        result = executor.run_probe(probe)
        assert result.verdict == Verdict.PASS, result.details
        assert any("(login)" in d for d in result.details), result.details

        # Refresh leg: drive the provider directly. The login above
        # happened inside the probe's own context; here a fresh
        # context logs in, then its expiry is forced to the past AS
        # A TEST HARNESS ACTION (Directus reports expires in
        # milliseconds; the provider's unit is seconds) so the
        # near-expiry branch must take the refresh endpoint.
        details: list[str] = []
        writes: list[str] = []
        ctx = executor._make_context([])
        assert executor._ensure_credential(ctx, details, writes) is None
        first_token = ctx._provided_token
        assert first_token
        refresh_token = ctx.bindings.get("REFRESH_TOKEN")
        assert refresh_token
        ctx.token_expires_at = 0.0
        # Directus access tokens are JWTs whose claims carry iat/exp
        # at SECOND granularity and no session claim, so a refresh
        # answered within the same wall-clock second as the login
        # legitimately returns a byte-identical access token (the
        # refresh token still rotates) — verified against a local
        # directus@12.5.0: an immediate refresh returned the login
        # token unchanged; a refresh after crossing a second
        # boundary returned a different one. Wait past the next
        # second boundary so the refreshed token must carry a later
        # iat and the rotation assertion below tests the provider,
        # not the clock phase.
        time.sleep(1.1 - (time.time() % 1))
        assert executor._ensure_credential(ctx, details, writes) is None
        assert any(
            d == "credential provider: POST /auth/refresh" for d in details
        ), details
        assert ctx._provided_token and ctx._provided_token != first_token

        committed = _write_record(tmp_path, orch, side, [result])
        secret_values = list(side.bootstrap_env.values()) + [
            first_token,
            refresh_token,
            ctx._provided_token,
        ]
        _assert_no_secrets(committed, secret_values)
        parsed = json.loads(committed)
        assert parsed["auth_bootstrap"] == {
            "c": {"generated": ["ADMIN_EMAIL", "ADMIN_PASSWORD", "SECRET"]}
        }
    finally:
        _stop(side)


GHOST_SEED_JS = r"""
// Contract seed for the Ghost proof: boot a temporary Ghost inside
// the install container against the checkout's SQLite database, run
// the instance's own setup with the per-run bootstrap credentials,
// create an Admin API key through the Admin API, and write the
// <id>:<secret> key to the contract's declared key file. The app
// container then starts on the same database; ProofDeploy's
// host-side ghost_jwt provider reads the key file after READY.
const { spawn } = require('child_process');
const fs = require('fs');
const http = require('http');
const path = require('path');

const PORT = 3999;
const BASE = `http://127.0.0.1:${PORT}`;

function request(method, urlPath, body, headers) {
  return new Promise((resolve, reject) => {
    const data = body ? JSON.stringify(body) : null;
    const req = http.request(
      BASE + urlPath,
      {
        method,
        headers: {
          'Content-Type': 'application/json',
          ...(data ? { 'Content-Length': Buffer.byteLength(data) } : {}),
          ...(headers || {}),
        },
      },
      (res) => {
        const chunks = [];
        res.on('data', (c) => chunks.push(c));
        res.on('end', () =>
          resolve({
            status: res.statusCode,
            headers: res.headers,
            text: Buffer.concat(chunks).toString('utf8'),
          })
        );
      }
    );
    req.on('error', reject);
    if (data) req.write(data);
    req.end();
  });
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function waitUp(child) {
  // "Up" means the app answers like a running Ghost, not the 503
  // maintenance page it serves for every route during boot
  // (migrations). A fresh (not yet set up) Ghost answers the site
  // endpoint 200; after setup it answers 401 without auth. Both
  // mean the API is live; 503 means still booting.
  for (let i = 0; i < 150; i++) {
    if (child.exitCode !== null) {
      throw new Error(`temporary Ghost exited early with ${child.exitCode}`);
    }
    try {
      const res = await request('GET', '/ghost/api/admin/site/');
      if (res.status && res.status !== 503) return;
    } catch (e) {
      /* not up yet */
    }
    await sleep(2000);
  }
  throw new Error('temporary Ghost did not come up in 300 s');
}

async function main() {
  const { ADMIN_EMAIL: email, ADMIN_PASSWORD: password } = process.env;
  if (!email || !password) {
    throw new Error('ADMIN_EMAIL / ADMIN_PASSWORD missing from seed env');
  }
  const env = {
    ...process.env,
    server__host: '127.0.0.1',
    server__port: String(PORT),
  };
  const child = spawn(process.execPath, ['index.js'], {
    env,
    stdio: 'inherit',
  });
  try {
    await waitUp(child);
    const setup = await request('POST', '/ghost/api/admin/authentication/setup/', {
      setup: [{ name: 'ProofDeploy', email, password, blogTitle: 'ProofDeploy' }],
    });
    if (setup.status !== 201) {
      throw new Error(`setup failed: ${setup.status} ${setup.text.slice(0, 300)}`);
    }
    const session = await request('POST', '/ghost/api/admin/session/', {
      username: email,
      password,
    });
    if (session.status !== 201 && session.status !== 200) {
      throw new Error(
        `session failed: ${session.status} ${session.text.slice(0, 300)}`
      );
    }
    const cookie = (session.headers['set-cookie'] || [])
      .map((c) => c.split(';')[0])
      .join('; ');
    const created = await request(
      'POST',
      '/ghost/api/admin/integrations/?include=api_keys',
      { integrations: [{ name: 'proofdeploy' }] },
      { Cookie: cookie }
    );
    if (created.status !== 201) {
      throw new Error(
        `integration failed: ${created.status} ${created.text.slice(0, 300)}`
      );
    }
    const integration = JSON.parse(created.text).integrations[0];
    const adminKey = (integration.api_keys || []).find((k) => k.type === 'admin');
    if (!adminKey) {
      throw new Error('no admin api key in integration response');
    }
    // The integrations API returns an admin key's secret as the full
    // "<key id>:<secret>" pair (the form Ghost's own admin shows for
    // copying); write it as returned. If a future Ghost returns the
    // bare secret, pair it with the key id here.
    const keyPair = adminKey.secret.includes(':')
      ? adminKey.secret
      : `${adminKey.id}:${adminKey.secret}`;
    fs.mkdirSync('.pd-auth', { recursive: true });
    fs.writeFileSync(path.join('.pd-auth', 'admin_key'), `${keyPair}\n`);
    console.log('ghost seed: setup complete, admin key written');
  } finally {
    child.kill('SIGTERM');
  }
}

main().then(
  () => process.exit(0),
  (err) => {
    console.error('ghost seed failed:', err.message);
    process.exit(1);
  }
);
"""

GHOST_APP_CONTRACT = """\
build: "true"
start: "server__port=$PORT node index.js"
readiness: "/"
seed: "node seed-ghost.js"
env:
  url: "http://localhost"
  database__client: sqlite3
  database__connection__filename: /checkout/ghost.db
  server__host: "0.0.0.0"
  NODE_ENV: production
auth:
  mode: ghost_jwt
  key_file: .pd-auth/admin_key
  ttl_seconds: 300
  bootstrap_env: [ADMIN_EMAIL, ADMIN_PASSWORD]
"""


def test_ghost_ghost_jwt_proof_in_sandbox(tmp_path):
    print(
        f"ghost release: tag {GHOST_TAG} commit {GHOST_SHA} "
        f"(npm ghost@{GHOST_VERSION})"
    )
    # Vendor the published release tarball as the project tree (the
    # Ghost-CLI layout; see the module docstring for why installing
    # ghost as a dependency cannot work).
    repo = tmp_path / "ghost-proof-repo"
    repo.mkdir()
    tarball = tmp_path / f"ghost-{GHOST_VERSION}.tgz"
    subprocess.run(
        ["curl", "-fsSL", "-o", str(tarball),
         f"https://registry.npmjs.org/ghost/-/ghost-{GHOST_VERSION}.tgz"],
        check=True, capture_output=True, timeout=600,
    )
    subprocess.run(
        ["tar", "-xzf", str(tarball), "-C", str(repo), "--strip-components=1"],
        check=True, capture_output=True, timeout=300,
    )
    assert (repo / "package.json").is_file()
    assert (repo / "index.js").is_file()
    # Vendor the tree the way Ghost-CLI installs Ghost in production:
    # with npm. The published tarball is the monorepo's root package
    # plus its pnpm scaffolding, and Ghost's pnpm path cannot be
    # provisioned by the adapter (each step verified on CI or locally
    # during this work order):
    # - the tarball ships pnpm-workspace.yaml (catalog + overrides
    #   for building Ghost from source); the adapter refuses
    #   workspace trees on principle -> plan refusal "workspaces not
    #   yet supported" (first CI run of this proof);
    # - with the file removed, the SHIPPED pnpm-lock.yaml no longer
    #   matches the tree's config (its overrides live in that file)
    #   -> ERR_PNPM_LOCKFILE_CONFIG_MISMATCH (second CI run);
    # - a regenerated pnpm lockfile installs until pnpm 12's build
    #   approvals: ERR_PNPM_IGNORED_BUILDS for better-sqlite3, re2,
    #   dtrace-provider — and pnpm 12 takes approvals ONLY from
    #   pnpm-workspace.yaml (the package.json pnpm field is ignored;
    #   verified locally). Approving builds needs the one file the
    #   adapter refuses.
    # So the proof presents the tree as the single npm package it is
    # installed as in production: the pnpm scaffolding and pin are
    # removed (nothing else in the tree is touched), and the lockfile
    # is generated with npm, as for Directus. Verified locally in the
    # pinned node image: lockfile generation, npm ci (native bindings
    # for better-sqlite3 and re2 built), and Ghost booting to a 200
    # on the contract env.
    (repo / "pnpm-workspace.yaml").unlink(missing_ok=True)
    (repo / "pnpm-lock.yaml").unlink(missing_ok=True)
    pkg_path = repo / "package.json"
    pkg = json.loads(pkg_path.read_text(encoding="utf-8"))
    assert pkg.pop("packageManager", "").startswith("pnpm@")
    pkg_path.write_text(json.dumps(pkg, indent=2) + "\n", encoding="utf-8")
    _npm_lockfile(repo, legacy_peer_deps=True)
    assert (repo / "package-lock.json").is_file()
    (repo / ".npmrc").write_text("legacy-peer-deps=true\n", encoding="utf-8")
    (repo / "seed-ghost.js").write_text(GHOST_SEED_JS, encoding="utf-8")
    (repo / "proofdeploy.yml").write_text(GHOST_APP_CONTRACT, encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "wo83-test")
    _git(repo, "config", "user.email", "wo83@proofdeploy.invalid")
    # -f: the release tree's own .gitignore must not drop shipped
    # files from the snapshot (snapshots carry tracked files only).
    _git(repo, "add", "-A", "-f")
    _git(repo, "commit", "-q", "-m", "ghost release tree + proofdeploy contract")
    rev = _git(repo, "rev-parse", "HEAD")
    orch = _orch(
        tmp_path,
        repo,
        [
            "url",
            "database__client",
            "database__connection__filename",
            "server__host",
            "NODE_ENV",
        ],
    )
    side = _provision(orch, tmp_path, rev, "ghost")
    try:
        assert side.provision.ready, "\n".join(side.provision.logs)
        key_path = side.snapshot_dir / ".pd-auth" / "admin_key"
        assert key_path.is_file(), "seed did not write the admin key file"
        key_value = key_path.read_text(encoding="utf-8").strip()
        executor = orch._make_executor(side)
        assert key_value in side.generated_secrets
        probe = {
            "act": {
                "method": "GET",
                "path": "/ghost/api/admin/site/",
                "headers": {"Authorization": "Ghost ${AUTH_TOKEN}"},
            },
            "assert": [{"type": "status", "equals": 200}],
        }
        result = executor.run_probe(probe)
        assert result.verdict == Verdict.PASS, result.details
        committed = _write_record(tmp_path, orch, side, [result])
        _assert_no_secrets(
            committed, list(side.bootstrap_env.values()) + [key_value]
        )
    finally:
        _stop(side)
