"""Lean P4: the end-to-end flow for one change (base..head) of one app.

``run_pipeline`` is independent of HOW the sides are provisioned: it talks to
two :class:`SideDriver` objects (one per side) that can start a FRESH app
instance (fresh process, database restored from the snapshot taken right
after seed). ``proofdeploy.lean.run`` provides the sandbox and host drivers.

Stages (each timed):

1. ``diff``      base..head -> ChangedLines; intent; seed facts
2. ``index``     head (and base) app index: OpenAPI from the running side if
                 it serves one, else the Flask url_map, else ``cannot_map``
3. ``impact``    P3 ``cover_change`` on head (added lines) and on base
                 (removed lines); the impact scenario set is the union
4. ``runs``      the impact scenarios replayed on base, base', head, head',
                 each run on a fresh app and a fresh database, same order
5. ``compare``   noise vs real differences, hard oracles (compare.py)
6. ``judge``     P5 ``run_lean`` (judge + decide) and ``replay`` self-check

No model is called unless a REAL difference remains and no hard failure
decides first.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import time
import urllib.error
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from proofdeploy.lean import coverage as lc
from proofdeploy.lean.compare import (
    RUN_LABELS,
    Comparison,
    NoiseItem,
    RunData,
    compare_runs,
    find_hard_failures,
)
from proofdeploy.lean.coverage import ChangedLines, CoverageSession
from proofdeploy.lean.generate import (
    CoverResult,
    ScenarioRunner,
    cover_change,
    executable_view,
    parse_diff_text,
)
from proofdeploy.lean.http import (
    AuthSpec,
    HttpClient,
    Scenario,
    UnsupportedAuth,
    auth_from_contract,
)
from proofdeploy.lean.index import (
    AppIndex,
    fetch_openapi,
    index_from_flask_url_map,
    index_from_openapi,
)
from proofdeploy.lean.judge import (
    DEFAULT_TOKEN_BUDGET,
    JudgeContext,
    path_template,
)
from proofdeploy.lean.verdict import (
    INCONCLUSIVE,
    LeanRecord,
    Verdict,
    replay,
    run_lean,
)
from proofdeploy.model_client import ModelRunner

DEFAULT_OPENAPI_PATHS = ("/api/schema/", "/openapi.json")
DEFAULT_REQUEST_BUDGET = 200
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


# --------------------------------------------------------------------------
# Typed outcomes
# --------------------------------------------------------------------------


class LeanRefusal(Exception):
    """The lean path does not support this unit (exit 3). ``code`` is the typed reason."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class LeanEnvironmentError(Exception):
    """The environment failed (provisioning, injection, harness): INCONCLUSIVE environment."""

    def __init__(self, cause: str, message: str) -> None:
        super().__init__(f"{cause}: {message}")
        self.cause = cause
        self.message = message


# --------------------------------------------------------------------------
# Driver interface
# --------------------------------------------------------------------------


class LiveApp(Protocol):
    base_url: str
    session: CoverageSession | None

    def server_log(self) -> str: ...

    def stop(self) -> None: ...


class SideDriver(Protocol):
    """One provisioned side (base or head)."""

    label: str
    sha: str
    root_prefix: str

    def start(self, *, measured: bool) -> LiveApp:
        """A FRESH app instance on a database restored from the seed snapshot.

        ``measured=True`` runs it under pdcov (coverage per request context).
        """
        ...

    def flask_rules(self, app: LiveApp, target: str) -> list[dict[str, Any]] | None:
        """Flask ``url_map`` rules of the app, or None when this driver cannot dump them."""
        ...

    def describe(self) -> dict[str, Any]:
        """Facts for the run manifest (isolation, db snapshot, clock)."""
        ...

    def close(self) -> None: ...


@dataclass
class LeanConfig:
    repo: Path
    base: str
    head: str
    intent: str | None = None
    budget_tokens: int = DEFAULT_TOKEN_BUDGET
    budget_requests: int = DEFAULT_REQUEST_BUDGET
    openapi_path: str | None = None
    flask_target: str = "app"
    mode: str = "sandbox"  # "sandbox" | "host"
    inputs: dict[str, Any] = field(default_factory=dict)  # extra manifest inputs


@dataclass
class LeanResult:
    verdict: Verdict
    record: LeanRecord | None
    manifest: dict[str, Any]
    exchanges: dict[str, list[dict[str, Any]]] = field(default_factory=dict)


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------


class Timings:
    """Wall-clock seconds per named stage, in the order they ran."""

    def __init__(self, initial: dict[str, float] | None = None) -> None:
        self.stages: dict[str, float] = dict(initial or {})

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        t0 = time.perf_counter()
        try:
            yield
        finally:
            self.stages[name] = self.stages.get(name, 0.0) + round(time.perf_counter() - t0, 3)

    def to_dict(self) -> dict[str, float]:
        return dict(self.stages)


def git_out(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-c", "core.quotepath=off", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    if proc.returncode != 0:
        raise ValueError(f"git {' '.join(args)} failed: {proc.stderr.strip()[:300]}")
    return proc.stdout


def resolve_sha(repo: Path, rev: str) -> str:
    return git_out(repo, "rev-parse", "--verify", f"{rev}^{{commit}}").strip()


def diff_text_of(repo: Path, base: str, head: str) -> str:
    return git_out(repo, "diff", "--no-color", "--no-ext-diff", "-U3", base, head)


def intent_of(repo: Path, head: str, override: str | None) -> tuple[str, str]:
    """(intent text, source): the CLI text, else the head commit's subject and body."""
    if override is not None and override.strip():
        return override.strip(), "cli"
    msg = git_out(repo, "log", "-1", "--format=%s%n%n%b", head).strip()
    return msg, "head_commit_message"


def seed_facts_of(fixture: dict[str, str] | None) -> list[str]:
    note = str((fixture or {}).get("seed_note") or "").strip()
    return [s.strip() for s in _SENTENCE_SPLIT.split(note) if s.strip()]


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# Index
# --------------------------------------------------------------------------


def build_index(
    driver: SideDriver,
    app: LiveApp,
    cfg: LeanConfig,
    *,
    app_name: str,
    seed_facts: list[str],
) -> AppIndex:
    """OpenAPI from the running side, else its Flask url_map, else ``cannot_map``."""
    paths = (cfg.openapi_path,) if cfg.openapi_path else DEFAULT_OPENAPI_PATHS
    tried: list[str] = []
    for path in paths:
        try:
            doc = fetch_openapi(app.base_url, path=path, timeout=15)
            return index_from_openapi(
                doc, app=app_name, commit_sha=driver.sha, seed_facts=seed_facts
            )
        except (urllib.error.URLError, OSError, ValueError, KeyError) as exc:
            tried.append(f"{path}: {type(exc).__name__}")
    rules = driver.flask_rules(app, cfg.flask_target)
    if rules:
        return index_from_flask_url_map(
            rules, app=app_name, commit_sha=driver.sha, seed_facts=seed_facts
        )
    raise LeanRefusal(
        "cannot_map",
        f"the {driver.label} side serves no OpenAPI document (tried {', '.join(tried)}) and "
        f"no Flask url_map could be read (target {cfg.flask_target!r})",
    )


# --------------------------------------------------------------------------
# Impact
# --------------------------------------------------------------------------


_STRUCTURAL_RE = re.compile(r"^\s*(?:@|import\s|from\s+\S+\s+import\s|(?:async\s+)?def\s|class\s)")


def is_structural(text: str | None) -> bool:
    """A declaration line: import, decorator, ``def`` or ``class`` (run when the module loads)."""
    return text is not None and _STRUCTURAL_RE.match(text) is not None


@dataclass
class SideImpact:
    """One side's P3 result plus the changed lines only the module load executed.

    A request never runs an ``import``, decorator, ``def`` or ``class`` line:
    the interpreter ran it while loading the app (context ``""``). Such a
    line is not reported as unexercised when it ran at load, but it does not
    count as reached over HTTP either (see ``run_pipeline``).
    """

    result: CoverResult | None
    skipped_reason: str | None = None
    unexercised: dict[str, dict[str, list[int]]] = field(default_factory=dict)
    load_only: list[str] = field(default_factory=list)

    def stats(self) -> dict[str, Any]:
        if self.result is None:
            return {"skipped": self.skipped_reason}
        return {
            **self.result.stats(),
            "unexercised": self.unexercised,
            "load_only_declarations": self.load_only,
        }


def _has_lines(view: ChangedLines, key: str) -> bool:
    return any(sides[key] for sides in view.values())


def cover_side(
    driver: SideDriver,
    index: AppIndex | None,
    changed: ChangedLines,
    diff_text: str,
    auth: AuthSpec | None,
    cfg: LeanConfig,
    *,
    side: str,
    live: LiveApp | None = None,
) -> SideImpact:
    """Run P3's ``cover_change`` on one side: ``new`` = head/added, ``old`` = base/removed."""
    key = "added" if side == "new" else "removed"
    view = executable_view(changed, parse_diff_text(diff_text), side)
    if not _has_lines(view, key):
        if live is not None:
            live.stop()
        return SideImpact(None, f"no executable {key} lines")
    app = live if live is not None else driver.start(measured=True)
    try:
        assert app.session is not None, "measured app without a coverage session"
        assert index is not None
        runner = ScenarioRunner(
            HttpClient(app.base_url), auth=auth, session=app.session, index=index, repair=True
        )
        result = cover_change(
            index,
            changed,
            runner,
            app.session,
            cfg.budget_requests,
            diff_text=diff_text,
            auth=auth,
            side=side,
            root_prefix=driver.root_prefix,
        )
        impact = _settle_load_only(result, app.session, changed, diff_text, key, driver.root_prefix)
    except lc.CoverageHarnessError as exc:
        raise LeanEnvironmentError("coverage_harness", str(exc)[:500]) from exc
    finally:
        app.stop()
    return impact


def _settle_load_only(
    result: CoverResult,
    session: CoverageSession,
    changed: ChangedLines,
    diff_text: str,
    key: str,
    root_prefix: str,
) -> SideImpact:
    """Split P3's unexercised lines into truly unexercised and declarations run at load."""
    if not result.unexercised:
        return SideImpact(result, unexercised={})
    load = session.snapshot(root_prefix=root_prefix).get("", {})
    dt = parse_diff_text(diff_text)
    remaining: dict[str, dict[str, list[int]]] = {}
    load_only: list[str] = []
    for path, sides in sorted(result.unexercised.items()):
        cov_path = changed.renames.get(path, path) if key == "removed" else path
        left: list[int] = []
        for n in sides[key]:
            if n in load.get(cov_path, set()) and is_structural(dt.text_of(path, key, n)):
                load_only.append(f"{path}:{n}")
            else:
                left.append(n)
        if left:
            remaining[path] = {"added": [], "removed": []}
            remaining[path][key] = left
    return SideImpact(result, unexercised=remaining, load_only=load_only)


def _fmt_unexercised(unex: dict[str, dict[str, list[int]]], key: str, label: str) -> list[str]:
    return [f"{path}:{n} ({label})" for path, sides in sorted(unex.items()) for n in sides[key]]


# --------------------------------------------------------------------------
# The 2x2 runs
# --------------------------------------------------------------------------


def replay_run(
    driver: SideDriver, label: str, scenarios: list[Scenario], auth: AuthSpec | None
) -> RunData:
    """Replay the scenarios on a fresh app: same requests, same order, no repair."""
    data = RunData(label)
    app = driver.start(measured=False)
    try:
        runner = ScenarioRunner(HttpClient(app.base_url), auth=auth, session=None, repair=False)
        for sc in scenarios:
            res = runner(sc)
            data.add(sc, res.exchanges)
    finally:
        try:
            data.server_log = app.server_log()
        finally:
            app.stop()
    return data


def _run_summary(run: RunData) -> dict[str, Any]:
    statuses = [e.status for e in run.exchanges.values()]
    return {
        "requests": run.request_count(),
        "2xx": sum(1 for s in statuses if 200 <= s < 300),
        "4xx": sum(1 for s in statuses if 400 <= s < 500),
        "5xx": sum(1 for s in statuses if s >= 500),
        "no_response": sum(1 for s in statuses if s == 0),
    }


def impacted_2xx_rates(
    scenarios: list[Scenario], run: RunData, templates: list[str]
) -> dict[str, dict[str, Any]]:
    """Per impacted operation: how many of its requests got a 2xx on the head run."""
    out: dict[str, dict[str, Any]] = {}
    for sc in scenarios:
        op = sc.target_operation
        method, _, tpl = op.partition(" ")
        for key, ex in run.exchanges.items():
            if key[0] != sc.id:
                continue
            ex_tpl = path_template(str(ex.request.get("url_path", "")), templates)
            if str(ex.request.get("method")) == method and ex_tpl == tpl:
                row = out.setdefault(op, {"requests": 0, "2xx": 0})
                row["requests"] += 1
                row["2xx"] += 1 if 200 <= ex.status < 300 else 0
    for row in out.values():
        row["2xx_rate"] = round(row["2xx"] / row["requests"], 3) if row["requests"] else 0.0
    return out


# --------------------------------------------------------------------------
# The pipeline
# --------------------------------------------------------------------------


def run_pipeline(
    cfg: LeanConfig,
    contract: Any,
    base: SideDriver,
    head: SideDriver,
    *,
    runner: ModelRunner | None = None,
    timings: Timings | None = None,
    log: Callable[[str], None] = lambda _m: None,
) -> LeanResult:
    """Run the lean verdict for ``cfg.base..cfg.head``; see the module docstring."""
    timings = timings or Timings()
    try:
        auth = auth_from_contract(contract)
    except UnsupportedAuth as exc:
        raise LeanRefusal("auth_mode_unsupported", str(exc)) from exc

    # 1. diff -------------------------------------------------------------
    with timings.stage("diff"):
        diff_text = diff_text_of(cfg.repo, cfg.base, cfg.head)
        changed = ChangedLines.from_diff(diff_text)
        intent, intent_source = intent_of(cfg.repo, cfg.head, cfg.intent)
        facts = seed_facts_of(getattr(contract, "fixture", None))
    app_name = cfg.repo.name

    # 2 + 3. index and impact: head first (added lines), then base (removed lines) --
    dt = parse_diff_text(diff_text)
    view_new = executable_view(changed, dt, "new")
    view_old = executable_view(changed, dt, "old")
    index_head: AppIndex | None = None
    index_base: AppIndex | None = None
    impact_head = SideImpact(None, "no executable added lines")
    impact_base = SideImpact(None, "no executable removed lines")

    if _has_lines(view_new, "added"):
        live = None
        try:
            with timings.stage("index_head"):
                live = head.start(measured=True)
                index_head = build_index(head, live, cfg, app_name=app_name, seed_facts=facts)
        except BaseException:
            if live is not None:
                live.stop()
            raise
        log(f"head index: {len(index_head.operations)} operations ({index_head.source_kind})")
        with timings.stage("cover_head"):
            impact_head = cover_side(
                head, index_head, changed, diff_text, auth, cfg, side="new", live=live
            )
    if _has_lines(view_old, "removed"):
        live = None
        try:
            with timings.stage("index_base"):
                live = base.start(measured=True)
                try:
                    index_base = build_index(base, live, cfg, app_name=app_name, seed_facts=facts)
                except LeanRefusal:
                    if index_head is None:
                        raise
                    index_base = index_head  # base cannot be mapped: use head's operations
        except BaseException:
            if live is not None:
                live.stop()
            raise
        with timings.stage("cover_base"):
            impact_base = cover_side(
                base, index_base, changed, diff_text, auth, cfg, side="old", live=live
            )

    # union of the impact scenario sets (head first), by scenario id
    scenarios: list[Scenario] = []
    seen_ids: set[str] = set()
    for impact in (impact_head, impact_base):
        if impact.result is None:
            continue
        for sc in impact.result.impact_set:
            if sc.id not in seen_ids:
                seen_ids.add(sc.id)
                scenarios.append(sc)

    unexercised_lines: list[str] = []
    if impact_head.result is not None:
        unexercised_lines += _fmt_unexercised(impact_head.unexercised, "added", "added")
    if impact_base.result is not None:
        unexercised_lines += _fmt_unexercised(impact_base.unexercised, "removed", "removed")
    total_exec = sum(len(s["added"]) for s in view_new.values()) + sum(
        len(s["removed"]) for s in view_old.values()
    )
    load_only = len(impact_head.load_only) + len(impact_base.load_only)
    reached = total_exec - len(unexercised_lines) - load_only
    # Nothing to observe, or at least one changed line was reached by a request.
    # Declarations that only ran at module load do not make a change observable.
    observable = total_exec == 0 or reached > 0
    log(
        f"impact: {len(scenarios)} scenario(s), {total_exec} executable changed line(s), "
        f"{len(unexercised_lines)} unexercised"
    )

    # 4. the 2x2 runs ---------------------------------------------------------
    runs: dict[str, RunData] = {}
    if scenarios:
        for label in RUN_LABELS:
            driver = base if label.startswith("base") else head
            with timings.stage(f"run_{label}"):
                runs[label] = replay_run(driver, label, scenarios, auth)
            log(f"run {label}: {runs[label].request_count()} request(s)")
    else:
        for label in RUN_LABELS:
            runs[label] = RunData(label)

    # 5. compare ---------------------------------------------------------------
    with timings.stage("compare"):
        comparison: Comparison = compare_runs(
            runs["base"], runs["base2"], runs["head"], runs["head2"]
        )
        hard, flaky = find_hard_failures(runs["base"], runs["base2"], runs["head"], runs["head2"])
    noise: list[NoiseItem] = [*comparison.noise, *flaky]

    # 6. judge and decide ---------------------------------------------------------
    templates = sorted(
        {op.path for idx in (index_head, index_base) if idx for op in idx.operations}
    )
    descs: dict[str, str] = {}
    for idx in (index_head, index_base):
        if idx is not None:
            for op in idx.operations:
                for k, v in op.field_descriptions.items():
                    descs.setdefault(k, v)
    ctx = JudgeContext(
        intent=intent, changed_hunks=diff_text, seed_facts=facts, field_descriptions=descs
    )
    with timings.stage("judge"):
        record = run_lean(
            comparison.differences,
            hard,
            ctx,
            unexercised_lines,
            observable,
            runner=runner,
            token_budget=cfg.budget_tokens,
            templates=templates,
        )
        replayed = replay(record)  # self-check: the stored outputs re-derive the verdict
    verdict = Verdict(
        replayed.status, replayed.cause, replayed.details
    )  # identical to the record's by construction

    manifest = {
        "kind": "lean-verify",
        "measured_result": False,
        "sandboxed": cfg.mode == "sandbox",
        "mode": cfg.mode,
        "inputs": {
            "repo": str(cfg.repo),
            "base": cfg.base,
            "head": cfg.head,
            "intent_source": intent_source,
            "intent_sha256": _sha256_text(intent),
            "diff_sha256": _sha256_text(diff_text),
            "budget_tokens": cfg.budget_tokens,
            "budget_requests": cfg.budget_requests,
            "openapi_path": cfg.openapi_path,
            "flask_target": cfg.flask_target,
            **cfg.inputs,
        },
        "changed_files": sorted(changed),
        "executable_changed_lines": total_exec,
        "pass_basis": "no executable changed lines" if total_exec == 0 else None,
        "index": {
            "head": {"sha256": index_head.sha256(), "kind": index_head.source_kind}
            if index_head
            else None,
            "base": {"sha256": index_base.sha256(), "kind": index_base.source_kind}
            if index_base
            else None,
        },
        "coverage": {"head": impact_head.stats(), "base": impact_base.stats()},
        "impact_scenarios": [s.id for s in scenarios],
        "unexercised_lines": unexercised_lines,
        "observable": observable,
        "runs": {label: _run_summary(run) for label, run in runs.items()},
        "impacted_operations_head": impacted_2xx_rates(scenarios, runs["head"], templates),
        "compared": {
            "exchanges": comparison.compared_exchanges,
            "fields": comparison.compared_fields,
            "differences": len(comparison.differences),
        },
        "noise_fields": sorted({n.field_path for n in noise}),
        "noise": [n.to_dict() for n in noise],
        "hard_failures": [h.to_dict() for h in hard],
        "judge": {
            "calls": len(record.calls),
            "total_tokens": record.total_tokens,
            "model_used": any(c.get("status") == "ok" for c in record.calls),
        },
        "sides": {"base": base.describe(), "head": head.describe()},
        "timings_seconds": timings.to_dict(),
        "verdict": verdict.to_dict(),
    }
    exchanges = {
        label: [e.to_dict() for e in (run.exchanges[k] for k in run.order)]
        for label, run in runs.items()
    }
    return LeanResult(verdict=verdict, record=record, manifest=manifest, exchanges=exchanges)


def environment_result(
    cfg: LeanConfig, cause: str, message: str, timings: Timings | None = None
) -> LeanResult:
    """INCONCLUSIVE ``environment`` without a judge record (provisioning or harness failure)."""
    verdict = Verdict(INCONCLUSIVE, "environment", {"cause": cause, "message": message})
    manifest = {
        "kind": "lean-verify",
        "measured_result": False,
        "sandboxed": cfg.mode == "sandbox",
        "mode": cfg.mode,
        "inputs": {"repo": str(cfg.repo), "base": cfg.base, "head": cfg.head, **cfg.inputs},
        "timings_seconds": (timings.to_dict() if timings else {}),
        "verdict": verdict.to_dict(),
    }
    return LeanResult(verdict=verdict, record=None, manifest=manifest)


def dumps(obj: Any) -> str:
    return json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False, default=str) + "\n"
