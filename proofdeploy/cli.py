"""CLI entry point (PRD FR-6).

``verify`` exit codes (plan 1.10, registered): 0 = PASS; 1 = FAIL;
2 = INCONCLUSIVE; 3 = not supported (typed); 4 = usage/config
error; 5 = internal error (our gap; traceback logged).
``measure`` keeps its own codes: 0 = ran; 1 = measure failed;
2 = tool error.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections.abc import Iterable
from typing import Any

from proofdeploy import __version__

# ----------------------------------------------------------------------
# D203: public env keys (measure.yml's `public_env_keys` input).
#
# A contract env value is a forbidden value for the bundle leak
# check unless its key is declared public (--public-env-key). That
# declaration is only ever legitimate for benign config the app
# itself prints in its own files (e.g. Django backend class paths);
# declared for a credential, it would disarm the leak check for a
# real secret. The workflow therefore validates every dispatched key
# with validate_public_env_keys BEFORE the measure step, failing the
# job on any refusal. The CLI flag itself stays a plain repeatable
# option; the gate lives here so it is unit-tested, not only shell.
# ----------------------------------------------------------------------

PUBLIC_ENV_KEY_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")

# Anything named like a credential can never be declared public:
# a secret/password/passwd/credential mention anywhere in the name,
# or a name ending in a token / api-key / _key suffix.
PUBLIC_ENV_KEY_REFUSAL_RE = re.compile(
    r"secret|password|passwd|credential|token$|api_?key|_key$",
    re.IGNORECASE,
)


def validate_public_env_keys(
    keys: Iterable[str],
    *,
    bootstrap_env_names: Iterable[str] = (),
) -> list[str]:
    """Validate keys proposed for --public-env-key (D203).

    Returns the keys, in order, when every one is acceptable.
    Raises ValueError on the first refusal: the key is not a
    well-formed env name (^[A-Z][A-Z0-9_]*$ — this refuses lowercase
    names and empty tokens), it is named like a credential, or it
    is one of the contract's auth.bootstrap_env names (whose values
    are generated per run and are secrets by construction).
    """
    bootstrap = set(bootstrap_env_names)
    accepted: list[str] = []
    for key in keys:
        if not PUBLIC_ENV_KEY_NAME_RE.match(key):
            raise ValueError(
                f"public env key {key!r} is not a valid env name "
                "(^[A-Z][A-Z0-9_]*$)"
            )
        if PUBLIC_ENV_KEY_REFUSAL_RE.search(key):
            raise ValueError(
                f"public env key {key!r} is named like a credential "
                "and can never be declared public"
            )
        if key in bootstrap:
            raise ValueError(
                f"public env key {key!r} is declared in the "
                "contract's auth.bootstrap_env; its value is a "
                "generated credential and can never be public"
            )
        accepted.append(key)
    return accepted


def _slug_from_repo_url(url: str) -> str | None:
    """The "owner/repo" a GitHub URL names, parsed the way the
    orchestrator's _repo_slug parses a clone's origin URL; None
    when the URL names no GitHub owner/repo."""
    marker = "github.com"
    idx = url.find(marker)
    if idx < 0:
        return None
    rest = url[idx + len(marker):].lstrip(":/")
    if rest.endswith(".git"):
        rest = rest[:-4]
    parts = rest.split("/")
    if len(parts) != 2 or not all(parts):
        return None
    return f"{parts[0]}/{parts[1]}"


def contract_bootstrap_env_names(
    unit_repo: str | None,
    target_repo: str | None,
) -> list[str] | None:
    """The dispatched unit contract's auth.bootstrap_env names.

    The contract resolves exactly as the measured path resolves it
    (REG-E): the unit_repo slug when one is dispatched, else the
    owner/repo parsed from the target repo URL (the D202 path), and
    the committed contract-store file for that slug. None when no
    contract can be named or loaded — the caller fails closed.
    """
    from proofdeploy.yml import contract_store_path, load_contract

    slug = unit_repo or (
        _slug_from_repo_url(target_repo) if target_repo else None
    )
    if slug is None:
        return None
    try:
        path = contract_store_path(slug)
        contract = load_contract(path)
    except (ValueError, FileNotFoundError, OSError):
        return None
    auth = contract.auth or {}
    names = auth.get("bootstrap_env")
    if not isinstance(names, list):
        return []
    return [str(n) for n in names]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="proofdeploy",
        description="Diff in, verified out.",
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    v = sub.add_parser(
        "verify",
        help="Verify a diff by probing a sandboxed instance of the target ref.",
    )
    v.add_argument(
        "--repo",
        default=None,
        help="Git checkout to verify (default: current directory).",
    )
    v.add_argument(
        "--from",
        dest="from_ref",
        default=None,
        help="Base ref for the diff (default: merge-base with the default branch).",
    )
    v.add_argument("--to", dest="to_ref", default="HEAD", help="Target ref (default: HEAD).")
    v.add_argument(
        "--target-url",
        default=None,
        help=(
            "Not supported in Stage 1: verify always provisions its own "
            "sandboxed instance; passing this is a typed refusal (exit 3)."
        ),
    )
    v.add_argument("--format", choices=["term", "html", "json"], default="term")
    v.add_argument(
        "--output",
        default=None,
        help="Write the report here (default: stdout).",
    )
    v.add_argument(
        "--output-dir",
        default=None,
        help="Evidence records dir (default: <repo>/.proofdeploy/records).",
    )
    v.add_argument(
        "--workdir",
        default=None,
        help="Scratch dir for bundles and snapshots (default: <repo>/.proofdeploy/work).",
    )
    v.add_argument(
        "--model",
        default=None,
        help="Probe-author model (default: the pinned model; another value refuses, exit 3).",
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
    m.add_argument(
        "--unit-repo",
        default=None,
        help=(
            "The measured unit's repo as owner/repo (WO-1.14, REG-E): "
            "its contract is loaded from the committed contract store "
            "(readiness/contracts/<owner>__<repo>.yml in the "
            "ProofDeploy tree), never from the target repo's tree. "
            "Default: derived from the target repo's origin remote."
        ),
    )
    m.add_argument(
        "--contract",
        default=None,
        help=(
            "Explicit contract store file to load for this unit "
            "(dev/tests; wins over --unit-repo). The file is still a "
            "store contract: it is loaded by path and its sha256 is "
            "recorded, and it is never read from the target tree."
        ),
    )
    m.add_argument(
        "--dry-run-before-model",
        action="store_true",
        help=(
            "Assemble and answer-key check the author bundle, then "
            "stop immediately before the model call: no spend, no "
            "provisioning, no records. Prints the rendered fixture "
            "sha256 and the bundle manifest."
        ),
    )
    return p


def _render_verify_output(output: dict, fmt: str) -> str:
    """Render a verify output object in the requested format."""
    import json

    if fmt == "json":
        return json.dumps(output, indent=2) + "\n"
    lines = []
    verdict = output.get("verdict")
    if verdict is None:
        lines.append(
            f"proofdeploy verify: {output.get('typed_reason')} "
            f"(exit {output.get('exit_code')})"
        )
    else:
        lines.append(f"verdict: {verdict} (exit {output.get('exit_code')})")
        if output.get("typed_reason"):
            lines.append(f"typed reason: {output['typed_reason']}")
        if output.get("cause"):
            lines.append(f"cause: {output['cause']}")
    refs = output.get("refs") or {}
    if refs.get("from_sha") or refs.get("to_sha"):
        lines.append(
            f"refs: {refs.get('repo')} "
            f"{(refs.get('from_sha') or '?')[:12]}..{(refs.get('to_sha') or '?')[:12]}"
        )
    probes = output.get("probes") or []
    if probes:
        counts: dict[str, int] = {}
        for p in probes:
            counts[p["verdict"]] = counts.get(p["verdict"], 0) + 1
        summary = ", ".join(f"{n} {v}" for v, n in sorted(counts.items()))
        lines.append(f"probes: {len(probes)} ({summary})")
        for p in probes:
            reason = f" ({p['reason']})" if p.get("reason") else ""
            lines.append(f"  [{p['probe_index']}] {p['verdict']}{reason}")
    record = output.get("evidence_record")
    if record:
        lines.append(
            f"evidence record: {record['path']} (record {record['record_id']})"
        )
    if output.get("detail"):
        lines.append(f"detail: {output['detail']}")
    return "\n".join(lines) + "\n"


def _emit_verify_output(output: dict, args: argparse.Namespace) -> None:
    text = _render_verify_output(output, args.format)
    if args.output:
        from pathlib import Path

        Path(args.output).write_text(text, encoding="utf-8")
    else:
        print(text, end="")


def cmd_verify(
    args: argparse.Namespace, model_runner: Any = None
) -> int:
    """Run one verify (plan 1.10). ``model_runner`` is the test seam:
    the pinned model client is used when it is None."""
    import traceback
    from pathlib import Path

    from proofdeploy import verify as verify_mod
    from proofdeploy.model_client import MODEL_ID

    fmt = args.format
    # Stage 1 typed refusals that need no engine work (exit 3).
    stop = None
    if args.target_url:
        stop = verify_mod.stop_output(
            exit_code=verify_mod.EXIT_NOT_SUPPORTED,
            typed_reason=verify_mod.NOT_SUPPORTED_TARGET_URL,
        )
    elif fmt == "html":
        stop = verify_mod.stop_output(
            exit_code=verify_mod.EXIT_NOT_SUPPORTED,
            typed_reason=verify_mod.NOT_SUPPORTED_HTML,
        )
    elif args.model and args.model != MODEL_ID:
        stop = verify_mod.stop_output(
            exit_code=verify_mod.EXIT_NOT_SUPPORTED,
            typed_reason=verify_mod.NOT_SUPPORTED_MODEL,
        )
    if stop is not None:
        _emit_verify_output(stop, args)
        return verify_mod.EXIT_NOT_SUPPORTED

    repo = Path(args.repo) if args.repo else Path.cwd()
    # The model key (when provided via env) joins the redaction pool:
    # it never enters a prompt or a container, and no output may
    # carry it (defense in depth, as in cmd_measure).
    extra_secrets = [v for v in [os.environ.get("GROQ_API_KEY")] if v]
    try:
        output, code = verify_mod.run_verify(
            repo_dir=repo,
            from_ref=args.from_ref,
            to_ref=args.to_ref,
            output_dir=Path(args.output_dir) if args.output_dir else None,
            workdir=Path(args.workdir) if args.workdir else None,
            model_runner=model_runner,
            extra_secrets=extra_secrets,
        )
    except Exception:
        # Internal error (exit 5): our gap. The traceback is logged,
        # never swallowed; the output stays machine-readable.
        traceback.print_exc()
        output = verify_mod.stop_output(
            exit_code=verify_mod.EXIT_INTERNAL,
            typed_reason=(
                "internal error: verify failed inside ProofDeploy "
                "(our gap); traceback logged"
            ),
        )
        _emit_verify_output(output, args)
        return verify_mod.EXIT_INTERNAL
    _emit_verify_output(output, args)
    return code


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
        # WO-1.14 (REG-E): a measured run ALWAYS loads its contract
        # from the committed contract store. getattr keeps older
        # argument namespaces (tests) working.
        use_contract_store=True,
        unit_repo=getattr(args, "unit_repo", None),
        contract_path=(
            Path(args.contract) if getattr(args, "contract", None) else None
        ),
        dry_run_before_model=bool(
            getattr(args, "dry_run_before_model", False)
        ),
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
        # cmd_verify maps every outcome onto the registered exit
        # codes itself (including internal errors, as exit 5).
        return cmd_verify(args)
    if args.command == "measure":
        try:
            return cmd_measure(args)
        except Exception as exc:  # loud failures, never silent
            print(f"proofdeploy: error: {exc}", file=sys.stderr)
            return 2
    return 2


if __name__ == "__main__":
    sys.exit(main())
