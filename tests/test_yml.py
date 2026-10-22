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


# ---------------------------------------------------------------------------
# app.path (plan 1.2b): the workspace member to build and start.
# ---------------------------------------------------------------------------


def test_app_path_loads(tmp_path):
    c = load_contract(write(tmp_path, VALID + 'app:\n  path: "packages/web"\n'))
    assert c.app_path == "packages/web"


def test_app_absent_defaults_to_none(tmp_path):
    c = load_contract(write(tmp_path, VALID))
    assert c.app_path is None


def test_app_root_path_loads(tmp_path):
    c = load_contract(write(tmp_path, VALID + 'app:\n  path: "."\n'))
    assert c.app_path == "."


def test_app_path_is_normalized(tmp_path):
    c = load_contract(write(tmp_path, VALID + 'app:\n  path: "./packages/web/"\n'))
    assert c.app_path == "packages/web"


def test_app_must_be_a_mapping(tmp_path):
    with pytest.raises(ValueError, match="'app' must be a mapping"):
        load_contract(write(tmp_path, VALID + 'app: "packages/web"\n'))


def test_app_unknown_keys_rejected(tmp_path):
    bad = VALID + 'app:\n  path: "packages/web"\n  pat: "typo"\n'
    with pytest.raises(ValueError, match="'app' has unknown keys"):
        load_contract(write(tmp_path, bad))


def test_app_path_required(tmp_path):
    with pytest.raises(ValueError, match="'app.path' must be a non-empty string"):
        load_contract(write(tmp_path, VALID + "app:\n  path: ''\n"))
    with pytest.raises(ValueError, match="'app.path' must be a non-empty string"):
        load_contract(write(tmp_path, VALID + "app: {}\n"))


def test_app_path_must_stay_inside_checkout(tmp_path):
    with pytest.raises(ValueError, match="'app.path' must resolve inside"):
        load_contract(write(tmp_path, VALID + 'app:\n  path: "/abs/web"\n'))
    with pytest.raises(ValueError, match="'app.path' must resolve inside"):
        load_contract(write(tmp_path, VALID + 'app:\n  path: "../outside"\n'))


# ---------------------------------------------------------------------------
# app.port (plan 1.2(o)): the app's own fixed listen port, declared,
# never inferred. The bridge forwards to it when set; PORT is still
# exported as assigned.
# ---------------------------------------------------------------------------


def test_app_port_loads(tmp_path):
    text = VALID + 'app:\n  path: "."\n  port: 3000\n'
    c = load_contract(write(tmp_path, text))
    assert c.app_path == "."
    assert c.app_port == 3000


def test_app_port_absent_defaults_to_none(tmp_path):
    c = load_contract(write(tmp_path, VALID + 'app:\n  path: "."\n'))
    assert c.app_port is None
    c = load_contract(write(tmp_path, VALID))
    assert c.app_port is None


def test_app_port_bounds_load(tmp_path):
    for port in (1, 65535):
        text = VALID + f'app:\n  path: "."\n  port: {port}\n'
        c = load_contract(write(tmp_path, text))
        assert c.app_port == port


def test_app_port_out_of_range_rejected(tmp_path):
    for port in ("0", "65536", "-1"):
        bad = VALID + f'app:\n  path: "."\n  port: {port}\n'
        with pytest.raises(ValueError, match="'app.port' must be an integer"):
            load_contract(write(tmp_path, bad))


def test_app_port_non_integer_rejected(tmp_path):
    # A quoted port is a string, not an integer; a float and a bool
    # are not integers either (bool is an int subclass in Python —
    # it must not slip through).
    for port in ('"3000"', "3000.5", "true"):
        bad = VALID + f'app:\n  path: "."\n  port: {port}\n'
        with pytest.raises(ValueError, match="'app.port' must be an integer"):
            load_contract(write(tmp_path, bad))


def test_app_port_does_not_replace_path(tmp_path):
    # `port` is an addition to the mapping, not an alternative to
    # `path`: an app mapping still names its member directory.
    with pytest.raises(ValueError, match="'app.path' must be a non-empty string"):
        load_contract(write(tmp_path, VALID + "app:\n  port: 3000\n"))


# install.npm_flags (plan 1.2h): allowlisted flags for the frozen
# npm ci. Anything off the allowlist is refused at load.

def test_install_npm_flags_allowlisted_loads(tmp_path):
    text = VALID + 'install:\n  npm_flags: ["--legacy-peer-deps"]\n'
    c = load_contract(write(tmp_path, text))
    assert c.npm_flags == ["--legacy-peer-deps"]


def test_install_absent_defaults_to_no_flags(tmp_path):
    c = load_contract(write(tmp_path, VALID))
    assert c.npm_flags == []


def test_install_npm_flags_unknown_flag_rejected(tmp_path):
    bad = VALID + 'install:\n  npm_flags: ["--force"]\n'
    with pytest.raises(ValueError, match="unsupported flag '--force'"):
        load_contract(write(tmp_path, bad))


def test_install_npm_flags_unreviewed_flag_rejected(tmp_path):
    # Even a plausible npm flag is refused: the allowlist is the
    # contract, not npm's own flag set.
    bad = VALID + 'install:\n  npm_flags: ["--ignore-scripts"]\n'
    with pytest.raises(ValueError, match="unsupported flag"):
        load_contract(write(tmp_path, bad))


def test_install_must_be_a_mapping(tmp_path):
    with pytest.raises(ValueError, match="'install' must be a mapping"):
        load_contract(write(tmp_path, VALID + 'install: "--legacy-peer-deps"\n'))


def test_install_unknown_keys_rejected(tmp_path):
    bad = VALID + 'install:\n  npm_flags: ["--legacy-peer-deps"]\n  pnpm_flags: []\n'
    with pytest.raises(ValueError, match="'install' has unknown keys"):
        load_contract(write(tmp_path, bad))


def test_install_npm_flags_must_be_a_nonempty_list(tmp_path):
    with pytest.raises(ValueError, match="'install.npm_flags' must be a non-empty"):
        load_contract(write(tmp_path, VALID + 'install:\n  npm_flags: "--legacy-peer-deps"\n'))
    with pytest.raises(ValueError, match="'install.npm_flags' must be a non-empty"):
        load_contract(write(tmp_path, VALID + 'install:\n  npm_flags: []\n'))


def test_install_npm_flags_duplicates_rejected(tmp_path):
    bad = VALID + 'install:\n  npm_flags: ["--legacy-peer-deps", "--legacy-peer-deps"]\n'
    with pytest.raises(ValueError, match="repeats '--legacy-peer-deps'"):
        load_contract(write(tmp_path, bad))


# --- runtime (WO-R5-R) ---


def test_runtime_absent_defaults_to_none(tmp_path):
    assert load_contract(write(tmp_path, VALID)).runtime is None


@pytest.mark.parametrize("value", ["python", "node"])
def test_runtime_declared_loads(tmp_path, value):
    c = load_contract(write(tmp_path, VALID + f"runtime: {value}\n"))
    assert c.runtime == value


@pytest.mark.parametrize("value", ["ruby", "Python", '""', "3", "[python]"])
def test_runtime_invalid_value_rejected(tmp_path, value):
    with pytest.raises(ValueError, match="'runtime' must be one of"):
        load_contract(write(tmp_path, VALID + f"runtime: {value}\n"))


# --- runtime_line (WO-R5-V) ---


def test_runtime_line_absent_defaults_to_none(tmp_path):
    assert load_contract(write(tmp_path, VALID)).runtime_line is None


def test_runtime_line_declared_loads(tmp_path):
    c = load_contract(
        write(tmp_path, VALID + 'runtime: python\nruntime_line: "3.13"\n')
    )
    assert c.runtime_line == "3.13"


@pytest.mark.parametrize(
    "value", ['"3"', '"3.13.1"', "3.13", '"x.y"', '""', '"v3.13"', "[3.13]"]
)
def test_runtime_line_invalid_value_rejected(tmp_path, value):
    bad = VALID + f"runtime: python\nruntime_line: {value}\n"
    with pytest.raises(ValueError, match="'runtime_line' must be"):
        load_contract(write(tmp_path, bad))


def test_runtime_line_without_runtime_rejected(tmp_path):
    with pytest.raises(ValueError, match="requires 'runtime'"):
        load_contract(write(tmp_path, VALID + 'runtime_line: "3.13"\n'))
