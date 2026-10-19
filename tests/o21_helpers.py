"""Shared helpers for the O21 (prompt v8 / R-BUNDLE / R-METHOD) tests.

Not a test module: builds real author bundles for the golden bug
units, with no model and no provisioning.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from conformance import kit
from proofdeploy.model_client import ModelResponse
from proofdeploy.orchestrator import MeasureConfig, Orchestrator

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_AUTHOR = REPO_ROOT / "docs" / "evaluation" / "skill" / "skill-author.md"
SIGNED = REPO_ROOT / "docs" / "evaluation" / "o21"
HEADER = SIGNED / "prompt-v8-header.md"


def golden_bundle(
    tmp_path: Path, app: str, *, model_runner: Any = None, no_skill: bool = False
) -> SimpleNamespace:
    """The author bundle of the golden app's bug unit (B^ = base, B = bug)."""
    root = tmp_path / f"golden-{app}"
    repo = root / "repo"
    shas = kit.materialize_golden(app, repo)
    cfg = MeasureConfig(
        repo_dir=repo,
        output_dir=root / "records",
        workdir=root / "work",
        skill_path=None if no_skill else SKILL_AUTHOR,
        no_skill=no_skill,
        bug_id=f"golden-{app}",
        rev_bug_base=shas["base"],
        rev_bug=shas["bug"],
        rev_fix=shas["fix"],
        model_runner=model_runner,
        allow_unsandboxed=True,
    )
    unit = root / "work" / "unit"
    unit.mkdir(parents=True)
    orch = Orchestrator(cfg)
    bundle, manifest, prompt, diff, base, tip, flags, sources = orch._build_bundle(
        unit, unit_id=cfg.bug_id, rev_base=shas["base"], rev_tip=shas["bug"]
    )
    return SimpleNamespace(
        orch=orch, cfg=cfg, repo=repo, shas=shas, bundle=bundle, manifest=manifest,
        prompt=prompt, diff=diff, base=base, tip=tip, flags=flags, sources=sources,
    )


def header_text() -> str:
    return HEADER.read_text(encoding="utf-8")


def text_before_skill(prompt: str) -> str:
    """The prompt text before the line `## Authoring skill`."""
    m = re.search(r"^## Authoring skill$", prompt, re.MULTILINE)
    assert m, "no '## Authoring skill' line"
    return prompt[: m.start()]


def probe_answer(*probes: dict) -> str:
    """Author output: reasoning prose, then one fenced json block per probe."""
    return "My reasoning.\n" + "\n".join(
        "```json\n" + json.dumps(p) + "\n```" for p in probes
    )


def response(content: str, **kw: Any) -> ModelResponse:
    return ModelResponse(content=content, finish_reason="stop", **kw)


ROUTE_PY = "from django.urls import path\nurlpatterns = [path('a/', v)]\n"

_YML = """\
build: "true"
start: "true"
readiness: "/"
env:
  APP_TOKEN: "super-secret-env-value-123"
app:
  path: app
fixture:
  repo_name: "routes-demo"
  seed_note: "empty"
  auth_mechanism: "none"
  auth_scope: "none"
  flags_note: "no flags"
"""


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def routes_repo(root: Path, urls_text: str = ROUTE_PY) -> dict[str, str]:
    repo = root / "routes-app"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "user.email", "t@t")
    (repo / "proofdeploy.yml").write_text(_YML)
    (repo / "app").mkdir()
    (repo / "app" / "views.py").write_text("v = 1\n")
    (repo / "app" / "urls.py").write_text(urls_text)
    (repo / "outside").mkdir()
    (repo / "outside" / "urls.py").write_text("outside = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "app" / "views.py").write_text("v = 2\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "bug")
    bug = _git(repo, "rev-parse", "HEAD")
    (repo / "app" / "views.py").write_text("v = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "Repair the value")
    fix = _git(repo, "rev-parse", "HEAD")
    return {"repo": str(repo), "base": base, "bug": bug, "fix": fix}


def routes_orch(root: Path, info: dict[str, str], **kw):
    cfg = MeasureConfig(
        repo_dir=Path(info["repo"]),
        output_dir=root / "records",
        workdir=root / "work",
        skill_path=Path(__file__).parent.parent / "docs/evaluation/skill/skill-author.md",
        bug_id="routes",
        rev_bug_base=info["base"],
        rev_bug=info["bug"],
        rev_fix=info["fix"],
        allow_unsandboxed=True,
        **kw,
    )
    unit = root / "work" / "unit"
    unit.mkdir(parents=True)
    return Orchestrator(cfg), unit


