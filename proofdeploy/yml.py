"""Loader for the proofdeploy.yml repo contract (PRD FR-7).

Contract (frozen 2026-10-08, PR #17): build, start, readiness are required.
migrate is optional (database-backed apps only), seed is optional (only when
probes need pre-existing data), env is optional non-secret config.
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
        return cls(
            build=data["build"],
            start=data["start"],
            readiness=data["readiness"],
            migrate=data.get("migrate"),
            seed=data.get("seed"),
            env=dict(env),
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
