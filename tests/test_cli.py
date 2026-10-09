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
