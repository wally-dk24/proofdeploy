"""Tests for the proofdeploy.yml contract loader (PRD FR-7)."""

import pytest

from proofdeploy.yml import find_contract, load_contract


def write(tmp_path, text):
    p = tmp_path / "proofdeploy.yml"
    p.write_text(text, encoding="utf-8")
    return p


VALID = """\
build: "dotnet build -c Release"
migrate: "dotnet ef database update"
seed: "./seed.sh"
start: "dotnet run --no-build"
readiness: "http://localhost:5000/health"
env:
  ASPNETCORE_ENVIRONMENT: Verification
"""


def test_load_valid(tmp_path):
    c = load_contract(write(tmp_path, VALID))
    assert c.build == "dotnet build -c Release"
    assert c.seed == "./seed.sh"
    assert c.env == {"ASPNETCORE_ENVIRONMENT": "Verification"}


MINIMAL = """\
build: "pip install -r requirements.txt"
start: "uvicorn main:app --host 127.0.0.1 --port 8000"
readiness: "http://localhost:8000/health"
"""


def test_minimal_file_without_migrate_seed_loads(tmp_path):
    """migrate/seed are optional (PR #17 contract): a minimal file must load."""
    c = load_contract(write(tmp_path, MINIMAL))
    assert c.build == "pip install -r requirements.txt"
    assert c.start == "uvicorn main:app --host 127.0.0.1 --port 8000"
    assert c.readiness == "http://localhost:8000/health"
    assert c.migrate is None
    assert c.seed is None
    assert c.env == {}


def test_missing_keys_rejected(tmp_path):
    with pytest.raises(ValueError, match="missing required keys"):
        load_contract(write(tmp_path, 'build: "x"\n'))


def test_env_must_be_string_map(tmp_path):
    bad = VALID.replace("Verification", "123")
    with pytest.raises(ValueError, match="'env' must be a mapping of strings"):
        load_contract(write(tmp_path, bad))


def test_not_a_mapping_rejected(tmp_path):
    with pytest.raises(ValueError, match="must be a mapping"):
        load_contract(write(tmp_path, "- just\n- a\n- list\n"))


def test_find_contract(tmp_path):
    assert find_contract(tmp_path) is None
    p = write(tmp_path, VALID)
    assert find_contract(tmp_path) == p


def test_database_unknown_keys_rejected(tmp_path):
    """Should-fix: unknown keys in `database:` are rejected (typos fail)."""
    bad = VALID + 'database:\n  kind: sqlite\n  path: ./app.db\n  bogus_key: x\n'
    with pytest.raises(ValueError, match="unknown keys"):
        load_contract(write(tmp_path, bad))


def test_database_absolute_path_rejected(tmp_path):
    """Should-fix: database.path must stay inside the checkout."""
    bad = VALID + 'database:\n  kind: sqlite\n  path: /etc/passwd\n'
    with pytest.raises(ValueError, match="inside the checkout"):
        load_contract(write(tmp_path, bad))


def test_database_dotdot_rejected(tmp_path):
    """Should-fix: database.path with `..` is rejected."""
    bad = VALID + 'database:\n  kind: sqlite\n  path: ../evil.db\n'
    with pytest.raises(ValueError, match="inside the checkout"):
        load_contract(write(tmp_path, bad))


def test_auth_unknown_keys_rejected(tmp_path):
    """Should-fix: unknown keys in `auth:` are rejected (typos fail)."""
    bad = (
        VALID
        + 'auth:\n  mode: register\n  path: /register\n  method: POST\n'
        + '  username_field: u\n  password_field: p\n  token_json_path: t\n'
        + '  token_path: typo\n'
    )
    with pytest.raises(ValueError, match="unknown keys"):
        load_contract(write(tmp_path, bad))


def test_database_valid_loads(tmp_path):
    """A valid database declaration loads."""
    good = VALID + 'database:\n  kind: sqlite\n  path: ./app.db\n'
    c = load_contract(write(tmp_path, good))
    assert c.database == {"kind": "sqlite", "path": "./app.db"}
