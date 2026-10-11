"""Tests for the CLI surface (PRD FR-6)."""

from proofdeploy.cli import build_parser, main


def test_verify_flags_match_prd():
    args = build_parser().parse_args(
        [
            "verify",
            "--repo",
            "https://example.com/r.git",
            "--from",
            "main",
            "--to",
            "abc123",
            "--target-url",
            "http://localhost:5000",
            "--allow-remote-target",
            "--format",
            "json",
            "--output",
            "out.json",
            "--model",
            "some-model",
        ]
    )
    assert args.command == "verify"
    assert args.from_ref == "main"
    assert args.to_ref == "abc123"
    assert args.allow_remote_target is True
    assert args.format == "json"


def test_verify_not_implemented_returns_tool_error():
    # cmd_verify is a stub until ticket #55 and on: exit 2 = tool error.
    assert main(["verify"]) == 2


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
