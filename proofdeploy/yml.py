"""Loader for the proofdeploy.yml repo contract (PRD FR-7).

Contract (frozen 2026-10-08, PR #17): build, start, readiness are required.
migrate is optional (database-backed apps only), seed is optional (only when
probes need pre-existing data), env is optional non-secret config.
`fixture` is optional (added 2026-10-09 for the blind-author fixture
description): a string-to-string mapping with the five template fields
(repo_name, seed_note, auth_mechanism, auth_scope, flags_note). Adding it
as optional keeps the frozen contract backward compatible.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

REQUIRED_KEYS = ("build", "start", "readiness")
OPTIONAL_KEYS = ("migrate", "seed")


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
    # Absent means no credential.
    auth: dict[str, str] | None = None

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
            if not isinstance(auth, dict):
                raise ValueError("proofdeploy.yml 'auth' must be a mapping")
            # Reject unknown keys (typos must fail, not silently pass).
            allowed_auth_keys = {
                "mode", "path", "method", "username_field",
                "password_field", "token_json_path",
            }
            unknown = set(auth) - allowed_auth_keys
            if unknown:
                raise ValueError(
                    f"proofdeploy.yml 'auth' has unknown keys: {sorted(unknown)}"
                )
            mode = auth.get("mode")
            if mode != "register":
                raise ValueError(
                    f"proofdeploy.yml 'auth.mode' must be 'register', got: {mode!r}"
                )
            for key in ("path", "method", "username_field", "password_field", "token_json_path"):
                val = auth.get(key)
                if not isinstance(val, str) or not val:
                    raise ValueError(
                        f"proofdeploy.yml 'auth.{key}' must be a non-empty string"
                    )
            auth = {k: str(v) for k, v in auth.items() if isinstance(v, str)}
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
        )


def load_contract(path: str | Path) -> RepoContract:
    """Load and validate proofdeploy.yml. Raises ValueError/FileNotFoundError."""
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValueError(f"proofdeploy.yml is not valid YAML: {exc}") from exc
    return RepoContract.from_dict(data)


def find_contract(repo_root: str | Path) -> Path | None:
    """Locate proofdeploy.yml at the repo root; None if absent."""
    p = Path(repo_root) / "proofdeploy.yml"
    return p if p.is_file() else None
