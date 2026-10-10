"""CLI entry point (PRD FR-6).

Exit codes: 0 = all claims PASS; 1 = any FAIL/INCONCLUSIVE; 2 = tool error.
"""

from __future__ import annotations

import argparse
import os
import sys

from proofdeploy import __version__


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="proofdeploy",
        description="Diff in, verified out.",
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    v = sub.add_parser("verify", help="Verify a diff by probing a provisioned instance.")
    v.add_argument("--repo", default=None, help="Repo URL to clone (default: current directory).")
    v.add_argument(
        "--from",
        dest="from_ref",
        default=None,
        help="Base ref for the diff (default: merge-base with main).",
    )
    v.add_argument("--to", dest="to_ref", default="HEAD", help="Target ref (default: HEAD).")
    v.add_argument(
        "--target-url",
        default=None,
        help="Probe this URL instead of provisioning (loopback only unless --allow-remote-target).",
    )
    v.add_argument(
        "--allow-remote-target",
        action="store_true",
        help=(
            "Permit a non-loopback --target-url; forces all probes read-only, "
            "HTTP GET/HEAD only, no DB probes."
        ),
    )
    v.add_argument("--format", choices=["term", "html", "json"], default="term")
    v.add_argument(
        "--output",
        default=None,
        help="Write the report here (default: stdout / ./proofdeploy-report.html).",
    )
    v.add_argument(
        "--model", default=None, help="Model for the probe author (default: $PROOFDEPLOY_MODEL)."
    )

    m = sub.add_parser(
        "measure",
        help="Run one measured unit end to end (WO-5 orchestrator).",
    )
    m.add_argument("--repo", required=True, help="App repo dir (must be a git checkout).")
    m.add_argument(
        "--workdir",
        required=True,
        help="Scratch dir for bundles, snapshots, and provisioning.",
    )
    m.add_argument(
        "--output-dir",
        required=True,
        help="Records dir (becomes a git repo for evidence/score commits).",
    )
    m.add_argument(
        "--skill",
        default=None,
        help="Registered author skill copy (hash-verified at bundle time).",
    )
    m.add_argument(
        "--no-skill",
        action="store_true",
        help="Run the no-skill (baseline) arm: no skill file is bundled.",
    )
    m.add_argument("--bug-id", default=None, help="Bug run id (requires --base/--bug/--fix).")
    m.add_argument("--base", default=None, help="B^: parent of the bug-introducing commit.")
    m.add_argument("--bug", default=None, help="B: the bug-introducing commit.")
    m.add_argument("--fix", default=None, help="The fix commit (fix^ must be B).")
    m.add_argument("--clean-id", default=None, help="Clean-diff run id (requires --clean).")
    m.add_argument("--clean", default=None, help="C: the clean-diff commit.")
    m.add_argument(
        "--allow-harness-app",
        action="store_true",
        help="Allow harness_app setup steps (dev-set library repos only).",
    )
    m.add_argument(
        "--public-env-key",
        action="append",
        default=[],
        metavar="KEY",
        help=(
            "Contract env key whose value is benign config, not a secret "
            "(repeatable; mirrors the record's public_contract_env_keys)."
        ),
    )
    m.add_argument(
        "--no-sandbox",
        action="store_true",
        help=(
            "DEV ONLY: run the app on the host without the sandbox "
            "container. The run is marked unsandboxed in the evidence "
            "record and never counts as a measured result."
        ),
    )
    m.add_argument(
        "--provenance-run-url",
        default=None,
        help="CI run URL recorded in the evidence provenance.",
    )
    m.add_argument(
        "--provenance-run-id",
        default=None,
        help="CI run ID recorded in the evidence provenance.",
    )
    m.add_argument(
        "--provenance-runner-image",
        default=None,
        help="CI runner image recorded in the evidence provenance.",
    )
    m.add_argument("--note", default="", help="Free-text note stored in the run log.")
    return p


def cmd_verify(args: argparse.Namespace) -> int:
    # Stages land per tickets #55 (diff reader), #57 (probe author),
    # #58 (provisioner/runner), #59 (verdict renderer).
    raise NotImplementedError("verify is not implemented yet (ticket #55 and on)")


def cmd_measure(args: argparse.Namespace) -> int:
    """Run one measured unit end to end (WO-5)."""
    from pathlib import Path

    from proofdeploy.orchestrator import MeasureConfig, Orchestrator, OrchestratorError

    if args.bug_id and args.clean_id:
        print("proofdeploy: --bug-id and --clean-id are mutually exclusive", file=sys.stderr)
        return 2
    if args.bug_id:
        missing = [n for n in ("base", "bug", "fix") if not getattr(args, n)]
        if missing:
            print(
                f"proofdeploy: --bug-id needs --base/--bug/--fix (missing: {missing})",
                file=sys.stderr,
            )
            return 2
    elif args.clean_id:
        if not args.clean:
            print("proofdeploy: --clean-id needs --clean", file=sys.stderr)
            return 2
    else:
        print("proofdeploy: need --bug-id or --clean-id", file=sys.stderr)
        return 2
    # Exactly one of --skill / --no-skill.
    if bool(args.skill) == bool(args.no_skill):
        print(
            "proofdeploy: need exactly one of --skill <path> or --no-skill",
            file=sys.stderr,
        )
        return 2
    # The Groq key (when provided via env for CI) is redacted by value
    # in every record. It never enters a prompt or a container; this is
    # defense in depth so no accidental serialization can leak it.
    extra_secrets = [v for v in [os.environ.get("GROQ_API_KEY")] if v]
    cfg = MeasureConfig(
        repo_dir=Path(args.repo),
        output_dir=Path(args.output_dir),
        workdir=Path(args.workdir),
        skill_path=Path(args.skill) if args.skill else None,
        no_skill=args.no_skill,
        bug_id=args.bug_id,
        clean_id=args.clean_id,
        rev_bug_base=args.base,
        rev_bug=args.bug,
        rev_fix=args.fix,
        rev_clean=args.clean,
        allow_harness_app=args.allow_harness_app,
        public_contract_env_keys=list(args.public_env_key),
        extra_secrets=extra_secrets,
        allow_unsandboxed=args.no_sandbox,
        provenance_run_url=args.provenance_run_url,
        provenance_run_id=args.provenance_run_id,
        provenance_runner_image=args.provenance_runner_image,
        note=args.note,
    )
    orch = Orchestrator(cfg)
    try:
        summary = orch.measure_bug() if cfg.bug_id else orch.measure_clean()
    except OrchestratorError as exc:
        print(f"proofdeploy: measure failed: {exc}", file=sys.stderr)
        return 1
    import json

    print(json.dumps(summary, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "verify":
        try:
            return cmd_verify(args)
        except NotImplementedError as exc:
            print(f"proofdeploy: {exc}", file=sys.stderr)
            return 2
        except Exception as exc:  # loud failures, never silent
            print(f"proofdeploy: error: {exc}", file=sys.stderr)
            return 2
    if args.command == "measure":
        try:
            return cmd_measure(args)
        except Exception as exc:  # loud failures, never silent
            print(f"proofdeploy: error: {exc}", file=sys.stderr)
            return 2
    return 2


if __name__ == "__main__":
    sys.exit(main())
