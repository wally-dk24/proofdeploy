"""Loader for the proofdeploy.yml repo contract (PRD FR-7).

Contract (frozen 2026-10-08, PR #17): build, start, readiness are required.
migrate is optional (database-backed apps only), seed is optional (only when
probes need pre-existing data), env is optional non-secret config.
`fixture` is optional (added 2026-10-09 for the blind-author fixture
description): a string-to-string mapping with the five template fields
(repo_name, seed_note, auth_mechanism, auth_scope, flags_note). Adding it
as optional keeps the frozen contract backward compatible.

`auth` (optional) declares the credential provider (ADR-0010; extended
WO-8.3): one of the modes `register`, `login`, `refresh`, `ghost_jwt`,
each with its own key set, plus the shared optional `bootstrap_env` —
a list of env NAMES for which ProofDeploy generates a fresh random
value per side and per run (Addendum 43/44). The generated values are
never written into the contract or any committed file; the contract
declares only the names.

`app` (optional, added 2026-10-14 for monorepo/workspace support,
plan 1.2b) names the workspace member to build and start: a mapping
with the key `path` — the package directory inside the checkout
("." for the workspace root itself) — and, since plan 1.2(o), the
optional key `port`. The provisioning adapter installs frozen from
the workspace root and runs the contract's build/migrate/seed/start
scoped to that directory. A workspace with several candidate apps
and no `app.path` is a typed refusal ("ambiguous app: <n>
candidates"), never a guess.

`app.port` (optional, plan 1.2(o)) is the app's own fixed listen
port: an integer in 1–65535, DECLARED by the contract and never
inferred from the app's code or config. Some apps bind a hardcoded
port and ignore the runner-assigned `$PORT` (nestjs/nest's sample
app calls `app.listen(3000)`); for those, the readiness bridge
forwards to the declared port instead of the assigned one. `PORT`
is still exported to the app environment exactly as before, and
when `app.port` is absent the bridge behaviour is unchanged.

`install` (optional, added 2026-10-15, plan 1.2h) declares how the
adapter's own frozen install is invoked: a mapping with the single
key `npm_flags` — extra flags rendered into the frozen `npm ci`
argv. The flags exist to align the install with the repo's own
install mode (nestjs/nest's CI installs with `--legacy-peer-deps`);
they can never unfreeze anything (`npm ci` still requires an exact
lock match), so the set is ALLOWLISTED at load: today the only
allowlisted flag is `--legacy-peer-deps`, and any other flag is a
typed refusal here, at load time, before anything runs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, overload

import yaml

REQUIRED_KEYS = ("build", "start", "readiness")
OPTIONAL_KEYS = ("migrate", "seed")

# A bootstrap env name must be a valid environment variable name (and a
# valid ${NAME} placeholder name, so providers can reference it).
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# The allowlist for `install.npm_flags` (plan 1.2h): flags the
# adapter may render into the frozen `npm ci` argv. Deliberately a
# single entry — a flag joins this set only by a reviewed change,
# never by a contract asking for it.
_NPM_FLAGS_ALLOWLIST = frozenset({"--legacy-peer-deps"})

# Per-mode key sets for the `auth:` mapping (WO-8.3). Unknown keys are
# refused (typos must fail, not silently pass). `bootstrap_env` is the
# one shared key, allowed in every mode.
_AUTH_MODE_KEYS: dict[str, set[str]] = {
    "register": {
        "mode", "path", "method", "username_field", "password_field",
        "token_json_path",
    },
    "login": {
        "mode", "path", "method", "body", "token_json_path", "token_header",
        "expires_in_json_path",
    },
    "refresh": {
        "mode", "path", "method", "body", "headers", "token_json_path",
        "refresh_token_json_path", "expires_in_json_path",
        "login_path", "login_method", "login_body",
    },
    "ghost_jwt": {"mode", "key", "key_file", "ttl_seconds"},
}
_AUTH_SHARED_KEYS = {"bootstrap_env"}


@dataclass
class RepoContract:
    build: str
    start: str
    readiness: str
    migrate: str | None = None
    seed: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    fixture: dict[str, str] = field(default_factory=dict)
    # Database declaration (optional): e.g. {"kind": "sqlite", "path": "./app.db"}.
    # When declared, the executor uses this path for DB assertions. When absent,
    # DB assertions are INCONCLUSIVE `probe` (fail-closed).
    database: dict[str, str] | None = None
    # Auth declaration (optional): e.g. {"mode": "register", "path": "/register",
    # "method": "POST", "username_field": "username", "password_field": "password",
    # "token_json_path": "token"}. Validated at load, before anything runs.
    # Absent means no credential. Values are mostly strings; `body`,
    # `headers` and `login_body` are string mappings and `bootstrap_env`
    # is a list of env names (WO-8.3), so the mapping is dict[str, Any].
    auth: dict[str, Any] | None = None
    # Workspace member to build and start (optional, plan 1.2b): the
    # `app.path` declaration, normalized ("packages/web", or "." for
    # the workspace root). Absent means the adapter resolves the app
    # from the workspace layout itself (single member, or the root)
    # or refuses an ambiguous workspace with a typed refusal.
    app_path: str | None = None
    # The app's own fixed listen port (optional, plan 1.2(o)): the
    # contract's `app.port`, validated at load (an int in 1–65535,
    # declared, never inferred). When set, the sandbox bridge
    # targets this port; the assigned `PORT` is still exported to
    # the app environment. Absent means the bridge targets the
    # assigned port, exactly as before.
    app_port: int | None = None
    # Extra flags for the adapter's frozen `npm ci` (optional, plan
    # 1.2h): the contract's `install.npm_flags`, validated against
    # the allowlist at load (see _validate_install). Empty when the
    # contract declares no `install` mapping. The Node adapter
    # renders these into the install argv and records them on the
    # plan; they never apply to another manager's install.
    npm_flags: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict) -> RepoContract:
        if not isinstance(data, dict):
            raise ValueError(f"proofdeploy.yml must be a mapping, got: {type(data).__name__}")

        missing = [k for k in REQUIRED_KEYS if not data.get(k)]
        if missing:
            raise ValueError(f"proofdeploy.yml missing required keys: {', '.join(missing)}")
        env = data.get("env") or {}
        if not isinstance(env, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in env.items()
        ):
            raise ValueError("proofdeploy.yml 'env' must be a mapping of strings")
        fixture = data.get("fixture") or {}
        if not isinstance(fixture, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in fixture.items()
        ):
            raise ValueError("proofdeploy.yml 'fixture' must be a mapping of strings")
        database = data.get("database")
        if database is not None:
            if not isinstance(database, dict):
                raise ValueError("proofdeploy.yml 'database' must be a mapping")
            # Reject unknown keys (typos must fail, not silently pass).
            allowed_db_keys = {"kind", "path"}
            unknown = set(database) - allowed_db_keys
            if unknown:
                raise ValueError(
                    f"proofdeploy.yml 'database' has unknown keys: {sorted(unknown)}"
                )
            kind = database.get("kind")
            path = database.get("path")
            if kind != "sqlite":
                raise ValueError(
                    f"proofdeploy.yml 'database.kind' must be 'sqlite', got: {kind!r}"
                )
            if not isinstance(path, str) or not path:
                raise ValueError("proofdeploy.yml 'database.path' must be a non-empty string")
            # The contract comes from untrusted repos: the path must stay
            # inside the checkout. No absolute paths, no `..` components.
            if path.startswith("/") or ".." in Path(path).parts:
                raise ValueError(
                    "proofdeploy.yml 'database.path' must resolve inside "
                    f"the checkout, got: {path!r}"
                )
            database = {"kind": kind, "path": path}
        auth = data.get("auth")
        if auth is not None:
            auth = _validate_auth(auth)
        app_path, app_port = _validate_app(data.get("app"))
        npm_flags = _validate_install(data.get("install"))
        return cls(
            build=data["build"],
            start=data["start"],
            readiness=data["readiness"],
            migrate=data.get("migrate"),
            seed=data.get("seed"),
            env=dict(env),
            fixture=dict(fixture),
            database=database,
            auth=auth,
            app_path=app_path,
            app_port=app_port,
            npm_flags=npm_flags,
        )


def _validate_app(app: Any) -> tuple[str | None, int | None]:
    """Validate the contract's `app:` mapping (plans 1.2b + 1.2(o)).

    `path` is the workspace member directory to build and start,
    declared so a multi-app workspace names its app instead of the
    adapter guessing. `port` (plan 1.2(o)) is the app's own fixed
    listen port, declared for apps that bind a hardcoded port and
    ignore the assigned `$PORT` — it is never inferred. Unknown
    keys are refused (the same rule as the `database` and `auth`
    mappings: typos must fail, not silently pass). The path must
    stay inside the checkout (same rule as `database.path` — the
    contract comes from untrusted repos). The port must be an
    integer in 1–65535: a string, a float, a bool, or an
    out-of-range value is refused here, at load, before anything
    runs. Returns (normalized path, port): the path is "." for the
    workspace root itself; both are None when `app` is absent, and
    the port alone is None when undeclared.
    """
    if app is None:
        return None, None
    if not isinstance(app, dict):
        raise ValueError("proofdeploy.yml 'app' must be a mapping")
    unknown = set(app) - {"path", "port"}
    if unknown:
        raise ValueError(
            f"proofdeploy.yml 'app' has unknown keys: {sorted(unknown)}"
        )
    path = app.get("path")
    if not isinstance(path, str) or not path:
        raise ValueError("proofdeploy.yml 'app.path' must be a non-empty string")
    if path.startswith("/") or ".." in Path(path).parts:
        raise ValueError(
            "proofdeploy.yml 'app.path' must resolve inside the "
            f"checkout, got: {path!r}"
        )
    port = app.get("port")
    if port is not None:
        if (
            not isinstance(port, int)
            or isinstance(port, bool)
            or not 1 <= port <= 65535
        ):
            raise ValueError(
                "proofdeploy.yml 'app.port' must be an integer "
                f"between 1 and 65535, got: {port!r}"
            )
    return str(Path(path)), port


def _validate_install(install: Any) -> list[str]:
    """Validate the contract's `install:` mapping (plan 1.2h).

    The one key is `npm_flags`: extra flags for the adapter's
    frozen `npm ci`. Unknown keys are refused (the same rule as
    the `app`/`database`/`auth` mappings: typos must fail, not
    silently pass), and every flag must be on the allowlist
    (_NPM_FLAGS_ALLOWLIST) — an unlisted flag is a typed refusal
    at load, before anything runs, because a contract is untrusted
    input and the frozen install's argv is ours to control.
    Returns the flags in declared order, or [] when `install` is
    absent.
    """
    if install is None:
        return []
    if not isinstance(install, dict):
        raise ValueError("proofdeploy.yml 'install' must be a mapping")
    unknown = set(install) - {"npm_flags"}
    if unknown:
        raise ValueError(
            f"proofdeploy.yml 'install' has unknown keys: {sorted(unknown)}"
        )
    flags = install.get("npm_flags")
    if flags is None:
        return []
    if (
        not isinstance(flags, list)
        or not flags
        or not all(isinstance(f, str) for f in flags)
    ):
        raise ValueError(
            "proofdeploy.yml 'install.npm_flags' must be a non-empty "
            "list of flag strings"
        )
    out: list[str] = []
    for flag in flags:
        if flag not in _NPM_FLAGS_ALLOWLIST:
            raise ValueError(
                f"proofdeploy.yml 'install.npm_flags' has unsupported "
                f"flag {flag!r}: the allowlist is "
                f"{sorted(_NPM_FLAGS_ALLOWLIST)} (plan 1.2h)"
            )
        if flag in out:
            raise ValueError(
                f"proofdeploy.yml 'install.npm_flags' repeats {flag!r}"
            )
        out.append(flag)
    return out


@overload
def _auth_str(auth: dict, key: str, *, required: Literal[True] = True) -> str: ...


@overload
def _auth_str(auth: dict, key: str, *, required: Literal[False]) -> str | None: ...


def _auth_str(auth: dict, key: str, *, required: bool = True) -> str | None:
    """One non-empty string field of the auth mapping (or None)."""
    val = auth.get(key)
    if val is None:
        if required:
            raise ValueError(f"proofdeploy.yml 'auth.{key}' must be a non-empty string")
        return None
    if not isinstance(val, str) or not val:
        raise ValueError(f"proofdeploy.yml 'auth.{key}' must be a non-empty string")
    return val


def _auth_path(auth: dict, key: str, *, required: bool = True) -> str | None:
    """One auth endpoint path, constrained to the target (Addendum 62).

    The credential provider builds its URL by concatenating this path
    onto the target URL, and the executor's write gate allows any
    host for a provisioned target (WO-7.3) — so the path itself must
    be a plain absolute path on the target. Anything that could
    rewrite the URL's authority is refused at load: no leading '@'
    (userinfo), no '//' (scheme-relative host), no '@' anywhere, no
    backslash, no scheme.
    """
    if required:
        val: str | None = _auth_str(auth, key)
    else:
        val = _auth_str(auth, key, required=False)
    if val is None:
        return None
    if (
        not val.startswith("/")
        or val.startswith("//")
        or "@" in val
        or "\\" in val
        or "://" in val
    ):
        raise ValueError(
            f"proofdeploy.yml 'auth.{key}' must be an absolute path on "
            "the target (starts with '/', no '//', '@', '\\' or scheme), "
            f"got: {val!r}"
        )
    return val


def _auth_str_map(auth: dict, key: str) -> dict[str, str] | None:
    """One optional string-to-string mapping field of the auth mapping.

    Used for request body/header templates: values are `${NAME}`
    placeholder templates resolved by the credential provider at run
    time, never literal secrets in the contract.
    """
    val = auth.get(key)
    if val is None:
        return None
    if not isinstance(val, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in val.items()
    ):
        raise ValueError(
            f"proofdeploy.yml 'auth.{key}' must be a mapping of strings"
        )
    return dict(val)


def _validate_bootstrap_env(auth: dict) -> list[str] | None:
    """Validate `auth.bootstrap_env`: a list of env NAMES (WO-8.3).

    Only names are declared — ProofDeploy generates the values fresh
    per side and per run (Addendum 43). A value written here would be
    a committed credential, so anything but a list of valid,
    duplicate-free env names is refused.
    """
    val = auth.get("bootstrap_env")
    if val is None:
        return None
    if not isinstance(val, list) or not val:
        raise ValueError(
            "proofdeploy.yml 'auth.bootstrap_env' must be a non-empty list of env names"
        )
    names: list[str] = []
    for name in val:
        if not isinstance(name, str) or not _ENV_NAME_RE.match(name):
            raise ValueError(
                "proofdeploy.yml 'auth.bootstrap_env' entries must be valid "
                f"env names, got: {name!r}"
            )
        if name in names:
            raise ValueError(
                f"proofdeploy.yml 'auth.bootstrap_env' repeats {name!r}"
            )
        names.append(name)
    return names


def _validate_auth(auth: Any) -> dict[str, Any]:
    """Validate the contract's `auth:` mapping (ADR-0010, WO-8.3).

    One declared provider mode, validated at load before anything
    runs — unknown modes and unknown keys are refused. Returns the
    normalized mapping (string fields as strings, `body`/`headers`/
    `login_body` as string mappings, `bootstrap_env` as a list of
    names, `ttl_seconds` as an int).
    """
    if not isinstance(auth, dict):
        raise ValueError("proofdeploy.yml 'auth' must be a mapping")
    mode = auth.get("mode")
    if mode not in _AUTH_MODE_KEYS:
        raise ValueError(
            "proofdeploy.yml 'auth.mode' must be one of "
            f"{sorted(_AUTH_MODE_KEYS)}, got: {mode!r}"
        )
    allowed = _AUTH_MODE_KEYS[mode] | _AUTH_SHARED_KEYS
    unknown = set(auth) - allowed
    if unknown:
        raise ValueError(
            f"proofdeploy.yml 'auth' has unknown keys for mode {mode!r}: "
            f"{sorted(unknown)}"
        )
    out: dict[str, Any] = {"mode": mode}
    bootstrap = _validate_bootstrap_env(auth)
    if bootstrap is not None:
        out["bootstrap_env"] = bootstrap
    if mode == "register":
        out["path"] = _auth_path(auth, "path")
        for key in ("method", "username_field", "password_field", "token_json_path"):
            out[key] = _auth_str(auth, key)
    elif mode == "login":
        out["path"] = _auth_path(auth, "path")
        # D120: the login response carries the token in exactly one
        # place — the JSON body at `token_json_path`, or a response
        # header named by `token_header`. Both or neither is refused.
        token_json_path = _auth_str(auth, "token_json_path", required=False)
        token_header = _auth_str(auth, "token_header", required=False)
        if (token_json_path is None) == (token_header is None):
            raise ValueError(
                "proofdeploy.yml 'auth' login mode needs exactly one of "
                "'token_json_path' or 'token_header'"
            )
        if token_json_path is not None:
            out["token_json_path"] = token_json_path
        if token_header is not None:
            out["token_header"] = token_header
        method = _auth_str(auth, "method", required=False)
        if method is not None:
            out["method"] = method
        body = _auth_str_map(auth, "body")
        if body is not None:
            out["body"] = body
        expires = _auth_str(auth, "expires_in_json_path", required=False)
        if expires is not None:
            out["expires_in_json_path"] = expires
    elif mode == "refresh":
        out["path"] = _auth_path(auth, "path")
        out["token_json_path"] = _auth_str(auth, "token_json_path")
        method = _auth_str(auth, "method", required=False)
        if method is not None:
            out["method"] = method
        for key in ("body", "headers", "login_body"):
            mapping = _auth_str_map(auth, key)
            if mapping is not None:
                out[key] = mapping
        for key in ("refresh_token_json_path", "expires_in_json_path", "login_method"):
            val = _auth_str(auth, key, required=False)
            if val is not None:
                out[key] = val
        login_path = _auth_path(auth, "login_path", required=False)
        if login_path is not None:
            out["login_path"] = login_path
    elif mode == "ghost_jwt":
        key_tpl = _auth_str(auth, "key", required=False)
        key_file = _auth_str(auth, "key_file", required=False)
        if (key_tpl is None) == (key_file is None):
            raise ValueError(
                "proofdeploy.yml 'auth' ghost_jwt needs exactly one of "
                "'key' (a ${NAME} placeholder) or 'key_file' (a checkout file)"
            )
        if key_tpl is not None:
            out["key"] = key_tpl
        if key_file is not None:
            # The key file is written by the instance's own seed into
            # the checkout: the path must stay inside it (same rule as
            # database.path — the contract comes from untrusted repos).
            if key_file.startswith("/") or ".." in Path(key_file).parts:
                raise ValueError(
                    "proofdeploy.yml 'auth.key_file' must resolve inside "
                    f"the checkout, got: {key_file!r}"
                )
            out["key_file"] = key_file
        ttl = auth.get("ttl_seconds")
        if ttl is not None:
            if not isinstance(ttl, int) or isinstance(ttl, bool) or ttl <= 0:
                raise ValueError(
                    "proofdeploy.yml 'auth.ttl_seconds' must be a positive integer"
                )
            out["ttl_seconds"] = ttl
    return out


def load_contract(path: str | Path) -> RepoContract:
    """Load and validate proofdeploy.yml. Raises ValueError/FileNotFoundError."""
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValueError(f"proofdeploy.yml is not valid YAML: {exc}") from exc
    return RepoContract.from_dict(data)


def proofdeploy_tree_root() -> Path:
    """The root of the ProofDeploy tree this code executes from.

    Measured runs execute from a checkout of the ProofDeploy repo
    (CI checks it out and installs it editable), so the package's
    parent directory is that tree's root — the tree of the main SHA
    the run was dispatched from (REG-E item 1).
    """
    return Path(__file__).resolve().parent.parent


def contract_store_path(
    repo_slug: str, *, tree_root: Path | None = None
) -> Path:
    """The committed contract-store file for a measured unit's repo.

    REG-E item 1 (WO-1.14): a measured unit's contract is its repo's
    file in the committed contract store, ``readiness/contracts/
    <owner>__<repo>.yml``, resolved against the ProofDeploy tree the
    run executes from — never a file inside the measured repo's own
    tree. ``repo_slug`` is ``owner/repo``; anything else is refused
    here, before a path is built from it (the slug becomes a file
    name, so it must carry no separators or traversal).
    """
    owner, sep, name = repo_slug.partition("/")
    if (
        not sep
        or not owner
        or not name
        or "/" in name
        or owner in (".", "..")
        or name in (".", "..")
    ):
        raise ValueError(
            f"repo slug must be 'owner/repo', got: {repo_slug!r}"
        )
    root = tree_root if tree_root is not None else proofdeploy_tree_root()
    return root / "readiness" / "contracts" / f"{owner}__{name}.yml"


def find_contract(repo_root: str | Path) -> Path | None:
    """Locate proofdeploy.yml at the repo root; None if absent."""
    p = Path(repo_root) / "proofdeploy.yml"
    return p if p.is_file() else None
