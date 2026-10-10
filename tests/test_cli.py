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
