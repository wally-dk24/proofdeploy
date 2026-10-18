"""Tests for the CLI surface (PRD FR-6)."""

import pytest

from proofdeploy.cli import (
    build_parser,
    contract_bootstrap_env_names,
    main,
    validate_public_env_keys,
)


def test_verify_flags_match_prd():
    args = build_parser().parse_args(
        [
            "verify",
            "--repo",
            "/tmp/some-repo",
            "--from",
            "main",
            "--to",
            "abc123",
            "--target-url",
            "http://localhost:5000",
            "--format",
            "json",
            "--output",
            "out.json",
            "--output-dir",
            "records",
            "--workdir",
            "work",
            "--model",
            "some-model",
        ]
    )
    assert args.command == "verify"
    assert args.from_ref == "main"
    assert args.to_ref == "abc123"
    assert args.target_url == "http://localhost:5000"
    # The stub's remote-target escape hatch is gone (plan 1.10):
    # Stage 1 verify provisions its own sandboxed instance only.
    assert not hasattr(args, "allow_remote_target")
    assert args.format == "json"
    assert args.output_dir == "records"
    assert args.workdir == "work"


def test_verify_without_contract_is_not_supported(tmp_path, monkeypatch):
    # Plan 1.10: a repo with no proofdeploy.yml is a typed
    # not-supported (exit 3), never a crash. Full CLI behavior is
    # covered in tests/test_verify.py.
    import subprocess

    repo = tmp_path / "app"
    repo.mkdir()
    for git_args in (
        ["init", "-q"],
        ["config", "user.name", "t"],
        ["config", "user.email", "t@t.invalid"],
    ):
        subprocess.run(["git", "-C", str(repo), *git_args], check=True)
    (repo / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "one"], check=True)
    monkeypatch.chdir(repo)
    assert main(["verify"]) == 3


def _measure_args(*extra):
    return build_parser().parse_args(
        [
            "measure",
            "--repo",
            "/tmp/r",
            "--workdir",
            "/tmp/w",
            "--output-dir",
            "/tmp/o",
            "--skill",
            "/tmp/s.md",
            "--bug-id",
            "b1",
            "--base",
            "a",
            "--bug",
            "b",
            "--fix",
            "c",
            *extra,
        ]
    )


def test_measure_is_sandboxed_by_default():
    args = _measure_args()
    assert args.no_sandbox is False


def test_measure_no_sandbox_is_explicit_dev_flag():
    args = _measure_args("--no-sandbox")
    assert args.no_sandbox is True


def test_measure_provenance_flags():
    args = _measure_args(
        "--provenance-run-url",
        "https://example.com/runs/1",
        "--provenance-run-id",
        "123",
        "--provenance-runner-image",
        "ubuntu-24.04",
    )
    assert args.provenance_run_url == "https://example.com/runs/1"
    assert args.provenance_run_id == "123"
    assert args.provenance_runner_image == "ubuntu-24.04"


def test_cmd_measure_bug_drives_orchestrator(tmp_path, monkeypatch, capsys):
    """T3: cmd_measure drives the orchestrator through the argument parser
    with a canned model and prints the summary SHAs.
    """
    import argparse

    from proofdeploy.cli import cmd_measure

    # Build a minimal args namespace like the parser would.
    args = argparse.Namespace(
        bug_id="cli-test-001",
        clean_id=None,
        base="base-sha",
        bug="bug-sha",
        fix="fix-sha",
        clean=None,
        skill=None,
        no_skill=True,
        repo=str(tmp_path / "repo"),
        output_dir=str(tmp_path / "out"),
        workdir=str(tmp_path / "work"),
        allow_harness_app=False,
        public_env_key=[],
        no_sandbox=True,
        provenance_run_url=None,
        provenance_run_id=None,
        provenance_runner_image=None,
        note=None,
    )
    # Mock the orchestrator to avoid real provisioning.
    import proofdeploy.orchestrator as orch_mod

    called = {}

    class FakeOrch:
        def __init__(self, cfg):
            called["cfg"] = cfg

        def measure_bug(self):
            return {
                "run_kind": "bug",
                "bug_id": "cli-test-001",
                "evidence_commit": "abc123",
                "score_commit": "def456",
            }

    monkeypatch.setattr(orch_mod, "Orchestrator", FakeOrch)
    rc = cmd_measure(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "abc123" in out  # evidence SHA printed
    assert "def456" in out  # score SHA printed
    assert called["cfg"].bug_id == "cli-test-001"
    assert called["cfg"].no_skill is True


# ----------------------------------------------------------------------
# D203: public env key validation (measure.yml's public_env_keys).
#
# A contract env key may be declared public (--public-env-key) only
# when its value is benign config the app itself prints in its own
# files. Declared for a credential, the declaration would disarm
# the bundle leak check for a real secret, so the workflow runs
# validate_public_env_keys before the measure step and fails the
# job on any refusal. These tests pin the ruled accept/refuse set.
# ----------------------------------------------------------------------

PAPERLESS_BOOTSTRAP = ["PAPERLESS_ADMIN_USER", "PAPERLESS_ADMIN_PASSWORD"]


def test_public_env_keys_accept_the_paperless_backend_keys():
    # The two keys D203 names: Django backend class paths copied
    # from paperless's own pyproject test config — public config,
    # not secrets.
    assert validate_public_env_keys(
        ["PAPERLESS_CHANNELS_BACKEND", "PAPERLESS_CACHE_BACKEND"],
        bootstrap_env_names=PAPERLESS_BOOTSTRAP,
    ) == ["PAPERLESS_CHANNELS_BACKEND", "PAPERLESS_CACHE_BACKEND"]


def test_public_env_keys_accept_an_empty_list():
    assert validate_public_env_keys([]) == []


@pytest.mark.parametrize(
    "key",
    [
        "PAPERLESS_SECRET_KEY",  # the one real secret in the contract
        "PAPERLESS_ADMIN_PASSWORD",  # a generated bootstrap credential
        "MY_API_KEY",
        "SERVICE_TOKEN",
        "DB_PASSWD",
        "APP_CREDENTIAL_ID",
    ],
)
def test_public_env_keys_refuse_credential_shaped_names(key):
    with pytest.raises(ValueError):
        validate_public_env_keys([key], bootstrap_env_names=[])


def test_public_env_keys_refuse_a_lowercase_key():
    with pytest.raises(ValueError):
        validate_public_env_keys(["paperless_cache_backend"])


def test_public_env_keys_refuse_an_empty_token():
    with pytest.raises(ValueError):
        validate_public_env_keys([""])


def test_public_env_keys_refuse_a_bootstrap_name():
    # PAPERLESS_ADMIN_USER matches no credential pattern; only the
    # contract's auth.bootstrap_env listing refuses it — its value
    # is a generated per-run credential.
    with pytest.raises(ValueError):
        validate_public_env_keys(
            ["PAPERLESS_ADMIN_USER"],
            bootstrap_env_names=PAPERLESS_BOOTSTRAP,
        )


def test_contract_bootstrap_env_names_resolves_the_store_entry():
    # By slug (a dispatched unit_repo) and by target URL (the D202
    # empty-unit_repo path) — the same committed paperless entry.
    assert contract_bootstrap_env_names(
        "paperless-ngx/paperless-ngx", None
    ) == PAPERLESS_BOOTSTRAP
    assert contract_bootstrap_env_names(
        None, "https://github.com/paperless-ngx/paperless-ngx"
    ) == PAPERLESS_BOOTSTRAP
    assert contract_bootstrap_env_names(
        None, "https://github.com/paperless-ngx/paperless-ngx.git"
    ) == PAPERLESS_BOOTSTRAP


def test_contract_bootstrap_env_names_unresolvable_is_none():
    assert contract_bootstrap_env_names("no-such-owner/no-such-repo", None) is None
    assert contract_bootstrap_env_names(None, "https://example.com/a/b") is None
    assert contract_bootstrap_env_names(None, None) is None
