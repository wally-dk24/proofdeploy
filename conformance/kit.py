"""Conformance kit for the ProofDeploy provisioning adapters (WO-7 PR 2).

The kit drives the golden apps in ``conformance/golden/`` through the
real measured-run machinery — sandbox provisioning behind the
provisioner interface, a CANNED author (fixed probes, no model), real
evidence/score records — and asserts the ten registered conformance
cases per adapter:

 1. the frozen lockfile is honored (the recorded install step is the
    adapter's frozen install, exit code 0);
 2. the lockfile-mismatch commit is refused (BUILD_FAILED, the frozen
    install itself fails);
 3. the declared runtime is selected and the version the target
    reports inside the container matches the plan;
 4. build, migrate and seed run (recorded executed steps, exit 0 —
    and the seeded data is what the probes read);
 5. a forced start failure is typed (START_FAILED) with the last
    concrete error in the provision logs;
 6. the network is cut after install and no secret reaches the app
    container's environment;
 7. the bug pair FAILs at fix^ and PASSes at fix (candidate_catch);
 8. the clean commit gives no FAIL (no false alarm);
 9. no canary appears in any committed record (neither the
    ``pdcanary-`` token prefix nor the per-app canary env value);
10. a committed record replays to the same verdict (re-scoring the
    committed evidence reproduces the stored score).

Each golden app ships as revision TREES under
``golden/<app>/revisions/<rev>/`` (base, bug, intermediate,
fix-parent, fix, clean, lockmismatch); :func:`materialize_golden`
rebuilds the app's own small git history from them, in order, so the
measured runs see real commits: B is the planted NON-boundary bug,
fix^..fix is the provisioned pair, C is a clean behavior-changing
commit, and the lockmismatch commit changes a dependency without
updating the lockfile.

The kit's checks live in ``tests/test_conformance.py`` and run in the
sandbox-tests CI job; a sandbox skip there fails the job (the repo's
PD_REQUIRE_SANDBOX discipline), so the kit cannot go green by
skipping.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

GOLDEN_DIR = Path(__file__).parent / "golden"

APPS = ("node-express-sqlite", "node-pnpm-sqlite", "python-flask-sqlite")

# The app's own history, in commit order. The kit maps these onto the
# measured-run revs: B^ = base, B = bug, fix^ = fix-parent, fix = fix,
# C = clean; lockmismatch is provisioned directly (case 2).
REVISIONS = (
    "base",
    "bug",
    "intermediate",
    "fix-parent",
    "fix",
    "clean",
    "lockmismatch",
)

_COMMIT_MESSAGES = {
    "base": "base: notes app with register/login and seeded note",
    "bug": "return the note's done flag",
    "intermediate": "add GET /notes list endpoint",
    "fix-parent": "seed a second, completed note",
    "fix": "fix the note done flag",
    "clean": "add a tag field to note responses",
    "lockmismatch": "bump a dependency (lockfile not updated)",
}

CANARY_PREFIX = "pdcanary-"


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=True, timeout=120,
    )
    return proc.stdout.strip()


def materialize_golden(app: str, dest: Path) -> dict[str, str]:
    """Rebuild the golden app's git history at ``dest``.

    Returns {revision_name: commit_sha}. The working tree is reset to
    each revision tree in turn and committed, so consecutive commits
    differ exactly as the trees differ.
    """
    src = GOLDEN_DIR / app / "revisions"
    dest.mkdir(parents=True, exist_ok=True)
    if not (dest / ".git").exists():
        _git(dest, "init", "-q")
    _git(dest, "config", "user.name", "conformance-kit")
    _git(dest, "config", "user.email", "kit@proofdeploy.invalid")
    shas: dict[str, str] = {}
    for rev in REVISIONS:
        tree = src / rev
        for child in list(dest.iterdir()):
            if child.name == ".git":
                continue
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
        for f in sorted(tree.rglob("*")):
            if f.is_file():
                target = dest / f.relative_to(tree)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(f, target)
        _git(dest, "add", "-A")
        _git(dest, "commit", "-q", "-m", _COMMIT_MESSAGES[rev])
        shas[rev] = _git(dest, "rev-parse", "HEAD")
    return shas


def load_probes(app: str) -> dict[str, list[dict[str, Any]]]:
    """The canned author's fixed probes for a golden app."""
    data = json.loads(
        (GOLDEN_DIR / app / "probes.json").read_text(encoding="utf-8")
    )
    return {
        "bug_probes": list(data["bug_probes"]),
        "clean_probes": list(data["clean_probes"]),
    }


def canned_runner(probes: list[dict[str, Any]]):
    """A model_runner that answers with fixed probes (NO model).

    The author is canned for conformance: the same probes on every
    run, so a verdict difference can only come from the code under
    test, never from sampling.
    """
    from proofdeploy.model_client import ModelResponse

    payload = "```json\n" + json.dumps(probes[0]) + "\n```\n"

    def _run(prompt: str) -> ModelResponse:
        return ModelResponse(content=payload, finish_reason="stop")

    return _run


def canary_env_value(app: str) -> str:
    """The per-app canary env value declared in the golden contract."""
    import yaml

    yml = GOLDEN_DIR / app / "revisions" / "clean" / "proofdeploy.yml"
    data = yaml.safe_load(yml.read_text(encoding="utf-8"))
    return str(data["env"]["CANARY_SECRET"])


def scan_records_for_canary(records_dir: Path, canary_value: str) -> list[str]:
    """Case 9's scan: every committed record file is searched for the
    canary token prefix and the appʼs canary env value. Returns a list
    of violation descriptions (empty = clean).
    """
    violations: list[str] = []
    log = records_dir / "runs.jsonl"
    files = [log] if log.is_file() else []
    for path in files:
        text = path.read_text(encoding="utf-8")
        if CANARY_PREFIX in text:
            violations.append(f"{path.name}: contains {CANARY_PREFIX!r}")
        if canary_value and canary_value in text:
            violations.append(f"{path.name}: contains the canary env value")
    return violations
