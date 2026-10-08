"""CLI entry point (PRD FR-6).

Exit codes: 0 = all claims PASS; 1 = any FAIL/INCONCLUSIVE; 2 = tool error.
"""

from __future__ import annotations

import argparse
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
    v.add_argument("--from", dest="from_ref", default=None, help="Base ref for the diff (default: merge-base with main).")
    v.add_argument("--to", dest="to_ref", default="HEAD", help="Target ref (default: HEAD).")
    v.add_argument("--target-url", default=None,
                   help="Probe this URL instead of provisioning (loopback only unless --allow-remote-target).")
    v.add_argument("--allow-remote-target", action="store_true",
                   help="Permit a non-loopback --target-url; forces all probes read-only, HTTP GET/HEAD only, no DB probes.")
    v.add_argument("--format", choices=["term", "html", "json"], default="term")
    v.add_argument("--output", default=None, help="Write the report here (default: stdout / ./proofdeploy-report.html).")
    v.add_argument("--model", default=None, help="Model for the probe author (default: $PROOFDEPLOY_MODEL).")
    return p


def cmd_verify(args: argparse.Namespace) -> int:
    # Stages land per tickets #55 (diff reader), #57 (probe author),
    # #58 (provisioner/runner), #59 (verdict renderer).
    raise NotImplementedError("verify is not implemented yet (ticket #55 and on)")


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
    return 2


if __name__ == "__main__":
    sys.exit(main())
