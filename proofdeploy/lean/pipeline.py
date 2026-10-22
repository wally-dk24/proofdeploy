"""Lean P4: the end-to-end flow for one change (base..head) of one app.

``run_pipeline`` is independent of HOW the sides are provisioned: it talks to
two :class:`SideDriver` objects (one per side) that can start a FRESH app
instance (fresh process, database restored from the snapshot taken right
after seed). ``proofdeploy.lean.run`` provides the sandbox and host drivers.

Stages (each timed):

1. ``diff``      base..head -> ChangedLines; intent; seed facts
2. ``index``     head (and base) app index: OpenAPI from the running side if
                 it serves one, else the Flask url_map, else the Django / DRF
                 resolver and serializers, else ``cannot_map``
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

import ast
import hashlib
import json
import re
import subprocess
import time
import urllib.error
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol

from proofdeploy.lean import coverage as lc
from proofdeploy.lean.compare import (
    RUN_LABELS,
    Comparison,
    HardCheckNotes,
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
    Exchange,
    HttpClient,
    Scenario,
    Step,
    UnsupportedAuth,
    auth_from_contract,
)
from proofdeploy.lean.index import (
    AppIndex,
    fetch_openapi,
    index_from_django_dump,
    index_from_flask_url_map,
    index_from_openapi,
)
from proofdeploy.lean.judge import (
    DEFAULT_TOKEN_BUDGET,
    HardFailure,
    JudgeContext,
    attach_exchanges,
    group_differences,
    group_hunk_labels,
    path_template,
)
from proofdeploy.lean.pathclass import BACKEND_EXTENSIONS, is_backend_source, paths_by_class
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
REQUEST_BUDGET_FREE_LINES = 100
REQUEST_BUDGET_CAP = 1000
REQUEST_BUDGET_RULE = (
    "200 if executable changed lines <= 100, else min(1000, 200 + 2 * lines); "
    "an explicit value wins"
)


def request_budget_for(explicit: int | None, lines: int) -> dict[str, Any]:
    """The per-side request budget of the impact search, and how it was chosen.

    An explicit value (CLI/config) wins. Otherwise the budget scales with the
    change: 200 for up to 100 executable changed lines, else ``min(1000, 200 + 2 * lines)``.
    """
    if explicit is not None:
        value, source = explicit, "explicit"
    else:
        if lines > REQUEST_BUDGET_FREE_LINES:
            value, source = min(REQUEST_BUDGET_CAP, DEFAULT_REQUEST_BUDGET + 2 * lines), "scaled"
        else:
            value, source = DEFAULT_REQUEST_BUDGET, "default"
    return {
        "value": value,
        "source": source,
        "executable_changed_lines": lines,
        "rule": REQUEST_BUDGET_RULE,
    }


_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


# --------------------------------------------------------------------------
# Typed outcomes
# --------------------------------------------------------------------------


class LeanRefusal(Exception):
    """The lean path does not support this unit (exit 3). ``code`` is the typed reason."""

    def __init__(
        self, code: str, message: str, attempts: list[dict[str, Any]] | None = None
    ) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        # ``mapping_attempts`` of a ``cannot_map``: one entry per tried step.
        self.attempts: list[dict[str, Any]] = list(attempts or [])


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

    # Optional: ``django_routes(app) -> list[dict] | None``, the records of
    # ``index.DJANGO_DUMP_SNIPPET`` run in the app's own interpreter and
    # environment (``DJANGO_SETTINGS_MODULE``); a driver without the method
    # cannot map a Django app.

    def describe(self) -> dict[str, Any]:
        """Facts for the run manifest (isolation, db snapshot, clock)."""
        ...

    # Optional: ``bootstrap_env() -> dict[str, str]``, the values this side was
    # provisioned with (generated per side and per run). They are secrets; a
    # driver without the method has none. Read with :func:`side_bootstrap_env`.

    def close(self) -> None: ...


@dataclass
class LeanConfig:
    repo: Path
    base: str
    head: str
    intent: str | None = None
    budget_tokens: int = DEFAULT_TOKEN_BUDGET
    budget_requests: int | None = None  # None: scaled with the change (request_budget_for)
    openapi_path: str | None = None
    flask_target: str = "app"
    mode: str = "sandbox"  # "sandbox" | "host"
    unit_repo: str | None = None  # the contract-store slug of the unit, when dispatched by one
    language: str = "python"  # the lean path measures Python apps (see pathclass)
    inputs: dict[str, Any] = field(default_factory=dict)  # extra manifest inputs


@dataclass
class LeanResult:
    verdict: Verdict
    record: LeanRecord | None
    manifest: dict[str, Any]
    exchanges: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    # The coverage loop's own exchanges, both sides' requests: "cover_head" / "cover_base".
    cover_exchanges: dict[str, list[dict[str, Any]]] = field(default_factory=dict)


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


def git_bytes(repo: Path, *args: str) -> bytes:
    """Raw stdout of a git command (never decoded here: a file need not be UTF-8)."""
    proc = subprocess.run(
        ["git", "-c", "core.quotepath=off", "-C", str(repo), *args],
        capture_output=True,
        check=False,
        timeout=120,
    )
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", errors="replace").strip()[:300]
        raise ValueError(f"git {' '.join(args)} failed: {err}")
    return proc.stdout


def git_out(repo: Path, *args: str) -> str:
    return git_bytes(repo, *args).decode("utf-8", errors="replace")


def resolve_sha(repo: Path, rev: str) -> str:
    return git_out(repo, "rev-parse", "--verify", f"{rev}^{{commit}}").strip()


def diff_bytes_of(repo: Path, base: str, head: str) -> bytes:
    return git_bytes(repo, "diff", "--no-color", "--no-ext-diff", "-U3", base, head)


def decode_diff(raw: bytes) -> str:
    """UTF-8 with ``errors="replace"``: a non-UTF-8 file can never crash a unit."""
    return raw.decode("utf-8", errors="replace")


def diff_text_of(repo: Path, base: str, head: str) -> str:
    return decode_diff(diff_bytes_of(repo, base, head))


def changed_paths_of(repo: Path, base: str, head: str) -> list[str]:
    """Every path the change touches (a rename lists both names), from git, not from hunks."""
    raw = git_bytes(repo, "diff", "--name-only", "--no-renames", "-z", base, head)
    return sorted(p.decode("utf-8", errors="replace") for p in raw.split(b"\0") if p)


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
    attempts: list[dict[str, Any]] | None = None,
) -> AppIndex:
    """OpenAPI from the running side, else its Flask url_map, else its Django / DRF
    resolver (in that order), else ``cannot_map``. ``index.source_kind`` is the
    ``mapped_by`` of the manifest: ``openapi`` | ``flask`` | ``django_drf`` | ``django``.

    ``attempts`` (when given) receives one entry per tried step with its outcome
    (``mapping_attempts`` of the manifest); a ``cannot_map`` carries the same list.
    """
    log: list[dict[str, Any]] = attempts if attempts is not None else []
    paths = (cfg.openapi_path,) if cfg.openapi_path else DEFAULT_OPENAPI_PATHS
    tried: list[str] = []
    for path in paths:
        try:
            doc = fetch_openapi(app.base_url, path=path, timeout=15)
            idx = index_from_openapi(
                doc, app=app_name, commit_sha=driver.sha, seed_facts=seed_facts
            )
            log.append({"step": "openapi", "paths_tried": [*tried, path], "outcome": "mapped"})
            return idx
        except (urllib.error.URLError, OSError, ValueError, KeyError) as exc:
            tried.append(f"{path}: {type(exc).__name__}")
    log.append({"step": "openapi", "paths_tried": tried, "outcome": "no OpenAPI document served"})
    rules = driver.flask_rules(app, cfg.flask_target)
    if rules:
        log.append({"step": "flask", "target": cfg.flask_target, "outcome": "mapped"})
        return index_from_flask_url_map(
            rules, app=app_name, commit_sha=driver.sha, seed_facts=seed_facts
        )
    log.append({"step": "flask", "target": cfg.flask_target, "outcome": "no url_map could be read"})
    dump = getattr(driver, "django_routes", None)
    routes = dump(app) if callable(dump) else None
    if routes:
        log.append({"step": "django", "outcome": "mapped"})
        return index_from_django_dump(
            routes, app=app_name, commit_sha=driver.sha, seed_facts=seed_facts
        )
    why = str(getattr(driver, "django_diagnostic", "") or "")
    if not callable(dump):
        why = "this driver cannot dump a Django resolver"
    log.append({"step": "django", "outcome": why or "no routes were dumped"})
    raise LeanRefusal(
        "cannot_map",
        f"the {driver.label} side serves no OpenAPI document (tried {', '.join(tried)}), "
        f"no Flask url_map could be read (target {cfg.flask_target!r}) and no Django URL "
        f"resolver could be read (django: {why or 'no routes were dumped'})",
        attempts=log,
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

    A request never runs a module-level statement (an ``import``, decorator,
    ``def``, ``class`` or plain assignment such as ``__version__ = ...``): the
    interpreter ran it while loading the app (context ``""``). Such a line is
    not reported as unexercised when it ran at load, but it does not count as
    reached over HTTP either (see ``run_pipeline``).
    """

    result: CoverResult | None
    skipped_reason: str | None = None
    unexercised: dict[str, dict[str, list[int]]] = field(default_factory=dict)
    load_only: list[str] = field(default_factory=list)
    # Request id -> the changed lines that request executed ("path:line"), read from the
    # coverage session while the app was still up. Not part of the manifest.
    request_hits: dict[str, dict[str, list[str]]] = field(default_factory=dict)

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


def side_bootstrap_env(driver: Any) -> dict[str, str]:
    """The generated bootstrap values this side was provisioned with ({} when it has none).

    Secrets: only ever used to fill a login body and to redact by value.
    """
    fn = getattr(driver, "bootstrap_env", None)
    return dict(fn()) if callable(fn) else {}


def source_reader(cfg: LeanConfig, key: str) -> Callable[[str], str | None]:
    """Read a file of the side's own tree from git (head for added, base for removed lines)."""
    rev = cfg.head if key == "added" else cfg.base

    def read(path: str) -> str | None:
        try:
            return git_out(cfg.repo, "show", f"{rev}:{path}")
        except ValueError:
            return None

    return read


def function_body_lines(source: str) -> set[int] | None:
    """Lines inside any function or method body of ``source`` (None when it does not parse).

    A body starts at its first statement; the ``def`` line, its decorators and a
    multi-line signature are evaluated when the module loads, so they are NOT
    body lines (a one-line ``def f(): ...`` is a module-level declaration).
    """
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return None
    inside: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            head_lines = {node.lineno, *(d.lineno for d in node.decorator_list)}
            end = node.end_lineno or node.lineno
            first = node.body[0].lineno
            inside |= {n for n in range(first, end + 1) if n not in head_lines}
    return inside


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
            HttpClient(app.base_url),
            auth=auth,
            session=app.session,
            index=index,
            repair=True,
            env=side_bootstrap_env(driver),
        )
        result = cover_change(
            index,
            changed,
            runner,
            app.session,
            cfg.budget_requests if cfg.budget_requests is not None else DEFAULT_REQUEST_BUDGET,
            diff_text=diff_text,
            auth=auth,
            side=side,
            root_prefix=driver.root_prefix,
        )
        impact = _settle_load_only(
            result,
            app.session,
            changed,
            diff_text,
            key,
            driver.root_prefix,
            source_of=source_reader(cfg, key),
        )
        impact.request_hits = _request_hits(app.session, view, side, driver.root_prefix)
    except lc.CoverageHarnessError as exc:
        raise LeanEnvironmentError("coverage_harness", str(exc)[:500]) from exc
    finally:
        app.stop()
    return impact


def _request_hits(
    session: CoverageSession, view: ChangedLines, side: str, root_prefix: str
) -> dict[str, dict[str, list[str]]]:
    """Per request id, the changed lines of this side's view it executed (none: left out)."""
    cov = session.snapshot(root_prefix=root_prefix)
    old, new = ({}, cov) if side == "new" else (cov, {})
    return {
        rid: h
        for rid, h in lc.impact(view, old, new).items()
        if h["hits_added"] or h["hits_removed"]
    }


def _settle_load_only(
    result: CoverResult,
    session: CoverageSession,
    changed: ChangedLines,
    diff_text: str,
    key: str,
    root_prefix: str,
    *,
    source_of: Callable[[str], str | None] | None = None,
) -> SideImpact:
    """Split P3's unexercised lines into truly unexercised and declarations run at load.

    A changed line that ran in the load context (``""``) and in no request
    context, and that belongs to a module-level statement (outside every
    function or method body of that side's file, per ``ast``), is a load-time
    declaration. A line inside a function body is never load-time. When the
    file cannot be read or parsed, only import/decorator/def/class lines count.
    """
    if not result.unexercised:
        return SideImpact(result, unexercised={})
    load = session.snapshot(root_prefix=root_prefix).get("", {})
    dt = parse_diff_text(diff_text)
    remaining: dict[str, dict[str, list[int]]] = {}
    load_only: list[str] = []
    for path, sides in sorted(result.unexercised.items()):
        cov_path = changed.renames.get(path, path) if key == "removed" else path
        body: set[int] | None = None
        if source_of is not None and cov_path.endswith(".py"):
            text = source_of(cov_path)
            body = function_body_lines(text) if text is not None else None
        left: list[int] = []
        for n in sides[key]:
            ran_at_load = n in load.get(cov_path, set())
            if body is not None:
                declaration = ran_at_load and n not in body
            else:
                declaration = ran_at_load and is_structural(dt.text_of(path, key, n))
            if declaration:
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
    data.side_started_at = getattr(driver, "provisioned_at", None)
    app = driver.start(measured=False)
    try:
        runner = ScenarioRunner(
            HttpClient(app.base_url),
            auth=auth,
            session=None,
            repair=False,
            env=side_bootstrap_env(driver),
        )
        for sc in scenarios:
            res = runner(sc)
            data.add(sc, res.exchanges)
    finally:
        try:
            data.server_log = app.server_log()
        finally:
            app.stop()
    return data


def settle_flaky(
    runs: dict[str, RunData],
    flaky: list[HardFailure],
    hard: list[HardFailure],
    notes: HardCheckNotes | None = None,
) -> tuple[list[HardFailure], list[HardFailure]]:
    """Use the third head run: a one-off that reproduces becomes a hard failure.

    The third run is compared with each earlier head run under the same rule
    (base clean in both runs); a flaky failure that comes back in either pair
    is reproduced. The rest stay flaky (INCONCLUSIVE, never PASS).
    """
    h3 = runs["head3"]

    def ident(h: HardFailure) -> tuple[str, str, str]:
        sig = h.detail.split(": ", 1)[-1] if h.request_id == "server_log" else ""
        return (h.request_id, h.kind, sig)

    again: set[tuple[str, str, str]] = set()
    for other in (runs["head"], runs["head2"]):
        stable, _ = find_hard_failures(runs["base"], runs["base2"], other, h3, notes=notes)
        again |= {ident(h) for h in stable}
    still: list[HardFailure] = []
    hard = list(hard)
    for f in flaky:
        if ident(f) in again:
            hard.append(
                HardFailure(
                    f.request_id,
                    f.kind,
                    f"reproduced in 2 of 3 head runs (base clean in both runs): {f.detail}",
                )
            )
        else:
            still.append(f)
    return still, hard


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


def loop_summary(exchanges: list[Exchange]) -> dict[str, Any]:
    """Counts of the coverage loop's own requests (login and producer steps included)."""
    return {
        "requests": len(exchanges),
        "2xx": sum(1 for e in exchanges if 200 <= e.status < 300),
        "4xx": sum(1 for e in exchanges if 400 <= e.status < 500),
        "5xx": sum(1 for e in exchanges if e.status >= 500),
        "no_response": sum(1 for e in exchanges if e.status == 0),
        "with_auth": sum(1 for e in exchanges if e.auth_sent),
    }


def acceptance_rate(exchanges: list[Exchange]) -> dict[str, Any]:
    """The non-4xx share of the loop's requests (a mapping that fits the app is accepted)."""
    n = len(exchanges)
    ok = sum(1 for e in exchanges if not 400 <= e.status < 500)
    return {"requests": n, "non_4xx": ok, "rate": round(ok / n, 3) if n else None}


def loop_operation_rates(
    impact_set: list[Scenario], exchanges: list[Exchange], templates: list[str]
) -> dict[str, dict[str, Any]]:
    """Per impacted operation: how many of the loop's requests to it got a 2xx.

    The loop sends each scenario under its generated id; a 400-repaired
    scenario is kept in the impact set under a new id (``repaired_from``), so
    both ids select its exchanges.
    """
    out: dict[str, dict[str, Any]] = {}
    for sc in impact_set:
        ids = {sc.id, sc.repaired_from}
        method, _, tpl = sc.target_operation.partition(" ")
        for ex in exchanges:
            if ex.scenario_id not in ids:
                continue
            ex_tpl = path_template(str(ex.request.get("url_path", "")), templates)
            if str(ex.request.get("method")) == method and ex_tpl == tpl:
                row = out.setdefault(sc.target_operation, {"requests": 0, "2xx": 0})
                row["requests"] += 1
                row["2xx"] += 1 if 200 <= ex.status < 300 else 0
    for row in out.values():
        row["2xx_rate"] = round(row["2xx"] / row["requests"], 3) if row["requests"] else 0.0
    return out


def cover_rows(exchanges: list[Exchange]) -> list[dict[str, Any]]:
    """The loop's exchanges as unit-output rows (token and bootstrap values already redacted)."""
    rows = []
    for e in exchanges:
        row = e.to_dict()
        row.update(
            method=e.request.get("method"),
            path=e.request.get("url_path"),
            scenario_id=e.scenario_id,
            step=e.step_index,
        )
        rows.append(row)
    return rows


# --------------------------------------------------------------------------
# Sign-in proof (inside the sandbox, before the impact loop)
# --------------------------------------------------------------------------

# The unit's proof path: an authenticated GET that must return 2xx once signed in.
# paperless-ngx's path equals ``readiness/auth_proof.py`` PROOF_SPECS (a test asserts
# it); the golden flask app's note 1 belongs to seeduser (D146).
AUTH_PROOF_PATHS: dict[str, str] = {
    "paperless-ngx/paperless-ngx": "/api/profile/",
    "golden/python-flask-sqlite": "/notes/1",
    "python-flask-sqlite": "/notes/1",
}


def unit_identity(cfg: LeanConfig, contract: Any) -> str | None:
    """The slug that selects the unit's proof path: the dispatched unit, else the fixture's repo."""
    if cfg.unit_repo:
        return cfg.unit_repo
    name = (getattr(contract, "fixture", None) or {}).get("repo_name")
    return str(name) if name else None


def auth_proof_path(cfg: LeanConfig, contract: Any) -> str:
    """The authenticated GET the sign-in proof sends: the contract's own ``auth.proof_path``
    first, else the in-code map for the unit (a unit with neither is a typed refusal)."""
    declared = (getattr(contract, "auth", None) or {}).get("proof_path")
    if declared:
        return str(declared)
    ident = unit_identity(cfg, contract)
    path = AUTH_PROOF_PATHS.get(ident or "")
    if path is None:
        raise LeanRefusal(
            "auth_proof_unspecified",
            f"the contract declares auth but unit {ident!r} has no registered proof path",
        )
    return path


def prove_auth(driver: SideDriver, auth: AuthSpec, proof_path: str, *, side: str) -> dict[str, Any]:
    """Sign in with the contract's login step, then one authenticated GET to the proof path.

    A static-token spec has no sign-in: the one request is the authenticated GET.

    Runs on a fresh unmeasured instance of the side, so the proof request leaves
    no trace in the coverage the impact loop reads. The record carries statuses
    and the path only (never a body, a token or a generated value).
    """
    env = side_bootstrap_env(driver)
    if auth.static_token_env is not None and not env.get(auth.static_token_env):
        # No generated token for this side: nothing is started and nothing is sent.
        return {
            "side": side,
            "ok": False,
            "outcome": "unproven",
            "proof_path": proof_path,
            "login_status": None,
            "proof_status": None,
            "proof_auth_sent": False,
            "reason": "static_token_missing",
        }
    app = driver.start(measured=False)
    try:
        runner = ScenarioRunner(
            HttpClient(app.base_url),
            auth=auth,
            session=None,
            repair=False,
            env=env,
        )
        proof = Step("GET", proof_path, auth=True)
        res = runner(Scenario([*auth.login_steps(), proof], f"GET {proof_path}"))
    finally:
        app.stop()
    if auth.is_static:  # no sign-in request: the proof request is the only exchange
        login_status: int | None = None
        proof_ex = res.exchanges[0] if res.exchanges else None
        login_ok = True
    else:
        login_status = res.exchanges[0].status if res.exchanges else 0
        proof_ex = res.exchanges[1] if len(res.exchanges) > 1 else None
        login_ok = 200 <= login_status < 300
    proof_status = proof_ex.status if proof_ex else None
    sent = bool(proof_ex is not None and proof_ex.auth_sent)
    if sent and proof_status is not None and 200 <= proof_status < 300:
        outcome = "2xx"
    elif sent and login_ok and proof_status is not None and proof_status >= 500:
        # Signed in, the token was presented, and the server got past authentication
        # and failed inside the proof endpoint itself: that is the change under test
        # breaking the endpoint (the hard oracle's business), not an unauthenticated loop.
        outcome = "5xx_past_auth"
    else:
        outcome = "unproven"
    return {
        "side": side,
        "ok": outcome != "unproven",
        "outcome": outcome,
        "proof_path": proof_path,
        "login_status": login_status,
        "proof_status": proof_status,
        "proof_auth_sent": sent,
    }


def _early_result(
    cfg: LeanConfig,
    verdict: Verdict,
    timings: Timings | None,
    extra: dict[str, Any] | None = None,
) -> LeanResult:
    manifest = {
        "kind": "lean-verify",
        "measured_result": False,
        "sandboxed": cfg.mode == "sandbox",
        "mode": cfg.mode,
        "inputs": {"repo": str(cfg.repo), "base": cfg.base, "head": cfg.head, **cfg.inputs},
        **(extra or {}),
        "timings_seconds": (timings.to_dict() if timings else {}),
        "verdict": verdict.to_dict(),
    }
    return LeanResult(verdict=verdict, record=None, manifest=manifest)


def not_observable_result(cfg: LeanConfig, timings: Timings | None = None) -> LeanResult | None:
    """INCONCLUSIVE ``not_observable_over_http`` when no changed path is backend source.

    Typed up front, before anything is provisioned and with no model call. A
    language whose backend extensions are not defined yet is never refused here.
    """
    paths = changed_paths_of(cfg.repo, cfg.base, cfg.head)
    if cfg.language not in BACKEND_EXTENSIONS or any(
        is_backend_source(p, cfg.language) for p in paths
    ):
        return None
    details = {"reason": "no_backend_source_change", "paths_by_class": paths_by_class(paths)}
    verdict = Verdict(INCONCLUSIVE, "not_observable_over_http", details)
    return _early_result(cfg, verdict, timings, {"observable": False, "changed_files": paths})


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

    # 0. a change with no backend source cannot be observed over HTTP -------------
    early = not_observable_result(cfg, timings)
    if early is not None:
        return early
    proof_path = auth_proof_path(cfg, contract) if auth is not None else None

    # 1. diff -------------------------------------------------------------
    with timings.stage("diff"):
        diff_raw = diff_bytes_of(cfg.repo, cfg.base, cfg.head)
        diff_text = decode_diff(diff_raw)
        changed = ChangedLines.from_diff(diff_text)
        intent, intent_source = intent_of(cfg.repo, cfg.head, cfg.intent)
        facts = seed_facts_of(getattr(contract, "fixture", None))
    app_name = cfg.repo.name

    # 2 + 3. index and impact: head first (added lines), then base (removed lines) --
    dt = parse_diff_text(diff_text)
    view_new = executable_view(changed, dt, "new")
    view_old = executable_view(changed, dt, "old")
    exec_lines = sum(len(s["added"]) for s in view_new.values()) + sum(
        len(s["removed"]) for s in view_old.values()
    )
    budget_rule = request_budget_for(cfg.budget_requests, exec_lines)
    cfg = replace(cfg, budget_requests=budget_rule["value"])
    index_head: AppIndex | None = None
    index_base: AppIndex | None = None
    attempts: dict[str, list[dict[str, Any]]] = {"head": [], "base": []}
    impact_head = SideImpact(None, "no executable added lines")
    impact_base = SideImpact(None, "no executable removed lines")

    # 1b. sign-in proven inside the sandbox, on each side that will run a loop ------
    auth_proof: Any = "no_auth_declared"
    if auth is not None:
        assert proof_path is not None
        auth_proof = {}
        for label, driver, side_view, key in (
            ("head", head, view_new, "added"),
            ("base", base, view_old, "removed"),
        ):
            if not _has_lines(side_view, key):
                auth_proof[label] = {"side": label, "skipped": f"no executable {key} lines"}
                continue
            with timings.stage(f"auth_proof_{label}"):
                auth_proof[label] = prove_auth(driver, auth, proof_path, side=label)
            log(f"auth proof {label}: {auth_proof[label]}")
            if not auth_proof[label]["ok"]:
                bad = auth_proof[label]
                verdict = Verdict(
                    INCONCLUSIVE,
                    "auth_unproven",
                    {
                        "side": label,
                        "login_status": bad["login_status"],
                        "proof_status": bad["proof_status"],
                    },
                )
                return _early_result(
                    cfg,
                    verdict,
                    timings,
                    {"auth_proof": auth_proof, "changed_files": sorted(changed)},
                )

    if _has_lines(view_new, "added"):
        live = None
        try:
            with timings.stage("index_head"):
                live = head.start(measured=True)
                index_head = build_index(
                    head, live, cfg, app_name=app_name, seed_facts=facts, attempts=attempts["head"]
                )
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
                    index_base = build_index(
                        base,
                        live,
                        cfg,
                        app_name=app_name,
                        seed_facts=facts,
                        attempts=attempts["base"],
                    )
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
        hard_notes = HardCheckNotes()
        hard, flaky_hard = find_hard_failures(
            runs["base"], runs["base2"], runs["head"], runs["head2"], notes=hard_notes
        )
    if flaky_hard:
        # A failure that appears after the change in only one of two head runs is a
        # regression signal, not noise. Run the head a third time to see whether it
        # reproduces: reproduced -> hard failure; not -> INCONCLUSIVE flaky_hard_failure.
        log(f"{len(flaky_hard)} head-only one-off failure(s): re-running head a third time")
        with timings.stage("run_head3"):
            runs["head3"] = replay_run(head, "head3", scenarios, auth)
        log(f"run head3: {runs['head3'].request_count()} request(s)")
        flaky_hard, hard = settle_flaky(runs, flaky_hard, hard, hard_notes)
    noise: list[NoiseItem] = list(comparison.noise)

    # 6. judge and decide ---------------------------------------------------------
    templates = sorted(
        {op.path for idx in (index_head, index_base) if idx for op in idx.operations}
    )
    cover_head_ex = impact_head.result.exchanges if impact_head.result else []
    cover_base_ex = impact_base.result.exchanges if impact_base.result else []
    descs: dict[str, str] = {}
    for idx in (index_head, index_base):
        if idx is not None:
            for op in idx.operations:
                for k, v in op.field_descriptions.items():
                    descs.setdefault(k, v)
    head_run = runs["head"]
    attach_exchanges(
        comparison.differences, [head_run.exchanges[k].to_dict() for k in head_run.order]
    )
    # The judge sees only the changed lines each request ran: head coverage for added
    # lines, base coverage for removed lines (keyed by the request ids of the differences).
    executed: dict[str, dict[str, list[str]]] = {}
    for d in comparison.differences:
        rid = d.request_id
        executed[rid] = {
            "hits_added": list(impact_head.request_hits.get(rid, {}).get("hits_added", [])),
            "hits_removed": list(impact_base.request_hits.get(rid, {}).get("hits_removed", [])),
        }
    ctx = JudgeContext(
        intent=intent,
        changed_hunks=diff_text,
        seed_facts=facts,
        field_descriptions=descs,
        executed=executed,
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
            flaky_hard_failures=flaky_hard,
        )
        replayed = replay(record)  # self-check: the stored outputs re-derive the verdict
    verdict = Verdict(
        replayed.status, replayed.cause, replayed.details
    )  # identical to the record's by construction
    hunks_shown = {
        str(g.index): group_hunk_labels(ctx, g)
        for g in group_differences(comparison.differences, templates)
    }

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
            "diff_sha256": hashlib.sha256(diff_raw).hexdigest(),
            "budget_tokens": cfg.budget_tokens,
            "budget_requests": cfg.budget_requests,
            "budget_requests_rule": budget_rule,
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
        "mapped_by": {
            "head": index_head.source_kind if index_head else None,
            "base": index_base.source_kind if index_base else None,
        },
        "mapping_attempts": attempts,
        "request_acceptance": {
            "head": acceptance_rate(cover_head_ex),
            "base": acceptance_rate(cover_base_ex),
        },
        "coverage": {"head": impact_head.stats(), "base": impact_base.stats()},
        "auth_proof": auth_proof,
        "cover_loop": {
            "head": loop_summary(cover_head_ex),
            "base": loop_summary(cover_base_ex),
        },
        "impacted_operations_loop_head": loop_operation_rates(
            impact_head.result.impact_set if impact_head.result else [], cover_head_ex, templates
        ),
        "impacted_operations_loop_base": loop_operation_rates(
            impact_base.result.impact_set if impact_base.result else [], cover_base_ex, templates
        ),
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
        "hard_check_notes": hard_notes.to_dict(),
        "run_windows": comparison.run_windows,
        "side_windows": comparison.side_windows,
        "hard_failures": [h.to_dict() for h in hard],
        "flaky_hard_failures": [h.to_dict() for h in flaky_hard],
        "judge": {
            "calls": len(record.calls),
            "total_tokens": record.total_tokens,
            "model_used": any(c.get("status") == "ok" for c in record.calls),
            "hunks_shown": hunks_shown,
        },
        "sides": {"base": base.describe(), "head": head.describe()},
        "timings_seconds": timings.to_dict(),
        "verdict": verdict.to_dict(),
    }
    exchanges = {
        label: [e.to_dict() for e in (run.exchanges[k] for k in run.order)]
        for label, run in runs.items()
    }
    cover_exchanges = {
        name: cover_rows(rows)
        for name, rows, impact in (
            ("cover_head", cover_head_ex, impact_head),
            ("cover_base", cover_base_ex, impact_base),
        )
        if impact.result is not None
    }
    return LeanResult(
        verdict=verdict,
        record=record,
        manifest=manifest,
        exchanges=exchanges,
        cover_exchanges=cover_exchanges,
    )


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
