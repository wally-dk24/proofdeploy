"""Lean P6b (D258): the Stage 2 Phase 0 gate driver.

Runs the fixed gate units sequentially through the lean pipeline
(``proofdeploy.lean.run.lean_verify``) and writes

    <out>/gate-results.jsonl      one JSON line per unit, appended as each unit ends
    <out>/gate-summary.json       the pre-registered criteria, computed (see below)
    <out>/units/<unit-id>/        that unit's lean-verify output (manifest, record, exchanges, logs)

Units, in this order (26):

  a. golden python-flask-sqlite, SANDBOX: ``golden-bug`` (base -> bug) and
     ``golden-clean`` (intermediate -> clean); intent = the head commit's message.
  b. the 20 mutants of ``mutants.json`` on ``intermediate``, HOST mode (our own golden
     code; each row says ``mode: host``); intent "no behaviour change intended".
  c. ``paperless-bug`` (B^..B), SANDBOX, contract store paperless-ngx/paperless-ngx, OpenAPI
     path /api/schema/, ``CELERY_TASK_ALWAYS_EAGER=True``; intent = B's commit message. When
     the verdict is FAIL the flagged requests are re-run on fix^ and fix (two runs each, no
     judge) and ``confirmed`` records whether fix^ shows the flagged value and fix does not.
  d. the 3 paperless clean units of ``paperless-clean-units.json`` (first_parent -> sha),
     same options; intent = the unit's own commit message.

The model is called by the pipeline's judge only, through the pinned client (key from
``GROQ_API_KEY``). Nothing here is a measured result: every record says so.

BLINDNESS (paperless): this driver never prints diffs, file contents or commit messages.
Log lines carry ids, verdicts, causes and counts. The intent text reaches the judge prompt and
the record files only (artifacts). Pipeline log lines and anything the sandbox writes to
stdout/stderr during a paperless unit go to ``<unit>/console.log`` (an artifact), not the log.

The criteria in ``compute_summary`` are the pre-registered ones, verbatim in ``CRITERIA``.
Nothing is a threshold of this driver's own choosing.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest import mock

from proofdeploy.lean import pipeline
from proofdeploy.lean import run as lean_run
from proofdeploy.lean.compare import MISSING, RunData, flatten
from proofdeploy.lean.http import AuthSpec
from proofdeploy.lean.http import Scenario as HttpScenario
from proofdeploy.lean.mutants import GATE_APP, GATE_REVISION, _kit, gate_manifest, write_mutant_repo
from proofdeploy.lean.pipeline import LeanResult, Timings
from proofdeploy.lean.verdict import FAIL, INCONCLUSIVE, PASS
from proofdeploy.model_client import ModelRunner

HERE = Path(__file__).resolve().parent
MUTANTS_JSON = HERE / "mutants.json"
CLEAN_UNITS_JSON = HERE / "paperless-clean-units.json"

# -- the paperless bug unit (WO-6 selection), fixed -----------------------------------------
PAPERLESS_UNIT_REPO = "paperless-ngx/paperless-ngx"
PAPERLESS_BASE = "17dc482872cf62e12965f6207772c83d1deb651b"  # B^
PAPERLESS_BUG = "aa67fd3aefa6489984fc15a5c32c1ad2b16ac23a"  # B
PAPERLESS_FIX_PARENT = "14b6a41b88e75fb7d35aa84b33986870bfd1aa69"  # fix^
PAPERLESS_FIX = "d3710856992a91783783819217b43392aab73e00"  # fix
PAPERLESS_OPENAPI = "/api/schema/"
PAPERLESS_OVERLAY = ["CELERY_TASK_ALWAYS_EAGER=True"]

MUTANT_INTENT = "no behaviour change intended"
TOKEN_CAP = 10_000
OBSERVABLE_MIN_PERCENT = 70
PAPERLESS_CLEAN_MAX_FALSE_ALARMS = 1

GROUPS = ("all", "golden", "mutants", "paperless")

# The pre-registered criteria (claude/stage-2-phase-0-poc-plan-2026-10-20.md), verbatim.
CRITERIA: dict[str, str] = {
    "1": "golden-bug verdict FAIL; golden-clean verdict PASS.",
    "2": (
        "mutants: caught = verdict FAIL; observable = observable flag true; criterion >= 70% of "
        "observable mutants caught; unobservable mutants must never be PASS when they have "
        "unexercised executable changed lines (report any violation)."
    ),
    "3": (
        "paperless bug: a real verdict (any verdict whose cause is not an environment/provider/"
        "error cause); caught = FAIL AND confirmed - reported, not required."
    ),
    "4": (
        "false alarms: a FAIL verdict on a clean unit; 0 on golden-clean, <= 1 across the 3 "
        "paperless clean units. INCONCLUSIVE on clean units is reported as its own count (not an "
        "alarm in this PoC)."
    ),
    "5": (
        "cost: every unit <= 10,000 AI tokens; any provider rate-limit/transport failure is "
        "reported."
    ),
}


class GateError(Exception):
    """The gate cannot start (inputs inconsistent with the registered files)."""


# --------------------------------------------------------------------------
# The unit list
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Unit:
    id: str
    kind: str  # bug | clean | mutant
    group: str  # golden | mutants | paperless
    mode: str  # sandbox | host
    base: str  # golden/mutant: a revision name or ref of the golden repo; paperless: a sha
    head: str
    intent: str | None  # None = the head commit's message (the pipeline default)
    paperless: bool = False


def build_units(mutants: dict[str, Any], clean: dict[str, Any]) -> list[Unit]:
    """The fixed unit list: 2 golden, one per mutant, the paperless bug, the paperless cleans."""
    units = [
        Unit("golden-bug", "bug", "golden", "sandbox", "base", "bug", None),
        Unit("golden-clean", "clean", "golden", "sandbox", GATE_REVISION, "clean", None),
    ]
    for m in mutants["mutants"]:
        units.append(
            Unit(
                f"mutant-{m['id']}",
                "mutant",
                "mutants",
                "host",
                GATE_REVISION,
                str(m["branch"]),
                str(mutants.get("judge_intent") or MUTANT_INTENT),
            )
        )
    units.append(
        Unit(
            "paperless-bug",
            "bug",
            "paperless",
            "sandbox",
            PAPERLESS_BASE,
            PAPERLESS_BUG,
            None,
            True,
        )
    )
    for u in clean["units"]:
        units.append(
            Unit(
                f"paperless-clean-{str(u['sha'])[:12]}",
                "clean",
                "paperless",
                "sandbox",
                str(u["first_parent"]),
                str(u["sha"]),
                None,
                True,
            )
        )
    return units


def load_units() -> list[Unit]:
    mutants = json.loads(MUTANTS_JSON.read_text(encoding="utf-8"))
    clean = json.loads(CLEAN_UNITS_JSON.read_text(encoding="utf-8"))
    return build_units(mutants, clean)


def select_units(units: list[Unit], which: str) -> list[Unit]:
    if which not in GROUPS:
        raise GateError(f"units must be one of {GROUPS}, got {which!r}")
    return [u for u in units if which == "all" or u.group == which]


# --------------------------------------------------------------------------
# Flagged items and the confirm direction rule (pure)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Flagged:
    """One flagged field of one request, with the value B (head) produced."""

    request_id: str
    field_path: str
    head_value: Any


UNCONFIRMABLE = "$unconfirmable"


def flagged_from_verdict(
    verdict: dict[str, Any], head_exchanges: list[dict[str, Any]]
) -> list[Flagged]:
    """The flagged (request, field, head value) items of a FAIL verdict.

    A judged regression flags its group's field on each of the group's requests, with the
    group's ``after`` as B's value. A hard-oracle failure flags the request's ``$status``, B's
    value being the status B's head run got; a hard failure that names no request, or no head
    exchange, cannot be confirmed (an ``$unconfirmable`` item, which never confirms).
    """
    details = verdict.get("details") or {}
    out: list[Flagged] = []
    if verdict.get("cause") == "judged_regression":
        for r in details.get("regressions", []):
            for rid in r.get("request_ids", []):
                out.append(Flagged(str(rid), str(r["field_path"]), r.get("after")))
    elif verdict.get("cause") == "hard_oracle":
        status = {str(e.get("request_id")): e.get("status") for e in head_exchanges}
        for h in details.get("hard_failures", []):
            rid = str(h.get("request_id") or "")
            if rid in status:
                out.append(Flagged(rid, "$status", status[rid]))
            else:
                out.append(Flagged(rid, UNCONFIRMABLE, None))
    return out


def _canon(v: Any) -> str:
    return json.dumps(v, sort_keys=True, ensure_ascii=False, default=str)


def _same(a: Any, b: Any) -> bool:
    """Equal values; an absent field and an explicit null count as the same."""
    absent = (None, MISSING)
    if any(a is x or (isinstance(a, str) and a == MISSING) for x in absent) and any(
        b is x or (isinstance(b, str) and b == MISSING) for x in absent
    ):
        return True
    return _canon(a) == _canon(b)


def field_values(run: RunData, request_id: str, field_path: str) -> list[Any] | None:
    """Values of ``field_path`` in one request's response in one run (None: no such exchange).

    ``results[].done`` matches every ``results[i].done``; ``$status`` is the HTTP status. A field
    that is absent from the response gives ``[MISSING]``.
    """
    sid, _, step = request_id.rpartition(":")
    if not sid or not step.isdigit():
        return None
    ex = run.exchanges.get((sid, int(step), 0))
    if ex is None:
        return None
    if field_path == "$status":
        return [ex.status]
    flat = flatten(ex.body_json_or_text)
    values = [v for p, v in flat.items() if lean_display_path(p) == field_path]
    return values or [MISSING]


def lean_display_path(path: str) -> str:
    from proofdeploy.lean.compare import display_path

    return path if path.startswith("$") else display_path(path)


def _stable(a: list[Any] | None, b: list[Any] | None) -> bool:
    return a is not None and b is not None and sorted(map(_canon, a)) == sorted(map(_canon, b))


def confirm_flagged(
    flagged: list[Flagged],
    fix_parent_runs: tuple[RunData, RunData],
    fix_runs: tuple[RunData, RunData],
) -> dict[str, Any]:
    """``confirmed`` iff, for EVERY flagged field, fix^ still shows B's value and fix moved off it.

    Per flagged item: the field is read in both fix^ runs and both fix runs; the readings must be
    stable (a run pair that disagrees is noise, never a confirmation); every fix^ value must equal
    B's (head) value; every fix value must differ from it (the direction: the fix moves away from
    the flagged behaviour). No flagged items confirms nothing.
    """
    items: list[dict[str, Any]] = []
    for f in flagged:
        item: dict[str, Any] = {
            "request_id": f.request_id,
            "field_path": f.field_path,
            "head_value": f.head_value,
        }
        if f.field_path == UNCONFIRMABLE:
            item.update(ok=False, reason="not_confirmable")
            items.append(item)
            continue
        p1 = field_values(fix_parent_runs[0], f.request_id, f.field_path)
        p2 = field_values(fix_parent_runs[1], f.request_id, f.field_path)
        x1 = field_values(fix_runs[0], f.request_id, f.field_path)
        x2 = field_values(fix_runs[1], f.request_id, f.field_path)
        item.update(fix_parent_values=p1, fix_values=x1)
        if not (_stable(p1, p2) and _stable(x1, x2)):
            item.update(ok=False, reason="unstable_or_missing_exchange")
        elif p1 is None or x1 is None:  # pragma: no cover (covered by _stable)
            item.update(ok=False, reason="missing_exchange")
        elif not all(_same(v, f.head_value) for v in p1):
            item.update(ok=False, reason="fix_parent_differs_from_head")
        elif any(_same(v, f.head_value) for v in x1):
            item.update(ok=False, reason="fix_does_not_move_away")
        else:
            item.update(ok=True, reason="confirmed")
        items.append(item)
    return {"confirmed": bool(items) and all(i["ok"] for i in items), "items": items}


# --------------------------------------------------------------------------
# Result rows
# --------------------------------------------------------------------------


def _sha256_json(obj: Any) -> str:
    return hashlib.sha256(_canon(obj).encode("utf-8")).hexdigest()


def row_from_result(
    unit: Unit, result: LeanResult, code: int, *, wall_seconds: float, mode: str | None = None
) -> dict[str, Any]:
    """The per-unit record from a lean result (ids, verdicts and counts only)."""
    m = result.manifest
    v = result.verdict
    calls = list(result.record.calls) if result.record is not None else []
    compared = m.get("compared") or {}
    differences = int(compared.get("differences", 0))
    hard = len(m.get("hard_failures") or [])
    flaky = len(m.get("flaky_hard_failures") or [])
    impacted = m.get("impacted_operations_head") or {}
    reqs = sum(int(r.get("requests", 0)) for r in impacted.values())
    ok2xx = sum(int(r.get("2xx", 0)) for r in impacted.values())
    return {
        "unit": unit.id,
        "kind": unit.kind,
        "group": unit.group,
        "mode": mode or m.get("mode") or unit.mode,
        "measured_result": False,
        "exit_code": code,
        "verdict": v.status,
        "cause": v.cause,
        "env_cause": (v.details or {}).get("cause") if v.cause == "environment" else None,
        "observable": bool(differences > 0 or hard > 0 or flaky > 0),
        "observable_over_http": m.get("observable"),
        "differences": differences,
        "noise": len(m.get("noise") or []),
        "unexercised": len(m.get("unexercised_lines") or []),
        "executable_changed_lines": m.get("executable_changed_lines"),
        "hard_failures": hard,
        "flaky_hard_failures": flaky,
        "tokens": int(result.record.total_tokens) if result.record is not None else 0,
        "model_calls": len(calls),
        "model_attempts": sum(len(c.get("attempts") or []) for c in calls),
        "model_errors": [str(c.get("error_cause")) for c in calls if c.get("status") == "error"],
        "judge_raw_sha256": _sha256_json([c.get("raw_content", "") for c in calls]),
        "timings_seconds": m.get("timings_seconds") or {},
        "wall_seconds": round(wall_seconds, 1),
        "rate_2xx": round(ok2xx / reqs, 3) if reqs else None,
        "impacted_2xx": impacted,
        "confirmed": None,
        "error": None,
    }


def error_row(
    unit: Unit, error: str, *, wall_seconds: float, mode: str | None = None
) -> dict[str, Any]:
    return {
        "unit": unit.id,
        "kind": unit.kind,
        "group": unit.group,
        "mode": mode or unit.mode,
        "measured_result": False,
        "exit_code": None,
        "verdict": None,
        "cause": None,
        "env_cause": None,
        "observable": False,
        "observable_over_http": None,
        "differences": 0,
        "noise": 0,
        "unexercised": 0,
        "executable_changed_lines": None,
        "hard_failures": 0,
        "flaky_hard_failures": 0,
        "tokens": 0,
        "model_calls": 0,
        "model_attempts": 0,
        "model_errors": [],
        "judge_raw_sha256": _sha256_json([]),
        "timings_seconds": {},
        "wall_seconds": round(wall_seconds, 1),
        "rate_2xx": None,
        "impacted_2xx": {},
        "confirmed": None,
        "error": error,
    }


# --------------------------------------------------------------------------
# The pre-registered criteria (pure)
# --------------------------------------------------------------------------


def _by_id(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {r["unit"]: r for r in rows}


def _crit(status: str, **details: Any) -> dict[str, Any]:
    return {"status": status, **details}


def compute_summary(
    rows: list[dict[str, Any]], *, expected: list[str] | None = None
) -> dict[str, Any]:
    """The criteria over the result rows; ``expected`` is the selected unit ids (default: rows).

    A criterion whose units did not all run is ``NOT_RUN`` (a partial selection or an aborted
    gate never yields a PASS on part of the data).
    """
    by = _by_id(rows)
    expect = list(expected) if expected is not None else [r["unit"] for r in rows]
    have = set(by)

    def missing(ids: list[str]) -> list[str]:
        return [i for i in ids if i not in have]

    crit: dict[str, Any] = {}

    # 1. golden
    g_ids = [i for i in ("golden-bug", "golden-clean") if i in expect or i in have]
    if len(g_ids) < 2 or missing(g_ids):
        crit["1"] = _crit("NOT_RUN", missing=missing(["golden-bug", "golden-clean"]))
    else:
        bug, cl = by["golden-bug"], by["golden-clean"]
        ok = bug["verdict"] == FAIL and cl["verdict"] == PASS
        crit["1"] = _crit(
            "PASS" if ok else "FAIL",
            golden_bug=bug["verdict"],
            golden_bug_cause=bug["cause"],
            golden_clean=cl["verdict"],
            golden_clean_cause=cl["cause"],
        )

    # 2. mutants
    m_expected = [i for i in expect if i.startswith("mutant-")]
    m_rows = [r for r in rows if r["kind"] == "mutant"]
    if not m_expected or missing(m_expected):
        crit["2"] = _crit("NOT_RUN", missing=missing(m_expected) or ["mutant-*"])
    else:
        obs = [r for r in m_rows if r["observable"]]
        caught = [r for r in obs if r["verdict"] == FAIL]
        violations = [
            r["unit"]
            for r in m_rows
            if not r["observable"] and r["verdict"] == PASS and r["unexercised"] > 0
        ]
        # integer arithmetic: caught / observable >= 70 / 100
        ratio_ok = len(obs) > 0 and len(caught) * 100 >= OBSERVABLE_MIN_PERCENT * len(obs)
        crit["2"] = _crit(
            "PASS" if ratio_ok and not violations else "FAIL",
            mutants_run=len(m_rows),
            observable=len(obs),
            caught=len(caught),
            caught_percent=round(100 * len(caught) / len(obs), 1) if obs else None,
            ratio_criterion_met=ratio_ok,
            unobservable=sorted(r["unit"] for r in m_rows if not r["observable"]),
            uncaught_observable=sorted(r["unit"] for r in obs if r["verdict"] != FAIL),
            violations=violations,
            errors=sorted(r["unit"] for r in m_rows if r["error"]),
        )

    # 3. paperless bug (reported, not required)
    if "paperless-bug" not in have:
        crit["3"] = _crit("NOT_RUN", required=False, missing=["paperless-bug"])
    else:
        b = by["paperless-bug"]
        real = (
            not b["error"]
            and b["verdict"] in (PASS, FAIL, INCONCLUSIVE)
            and b["cause"] != "environment"
        )
        crit["3"] = _crit(
            "PASS" if real else "FAIL",
            required=False,
            real_verdict=real,
            verdict=b["verdict"],
            cause=b["cause"],
            confirmed=b["confirmed"],
            caught=b["verdict"] == FAIL and b["confirmed"] is True,
            error=bool(b["error"]),
        )

    # 4. false alarms
    p_clean_expected = [i for i in expect if i.startswith("paperless-clean-")]
    needed = ["golden-clean", *p_clean_expected]
    if "golden-clean" not in expect and "golden-clean" not in have:
        needed = p_clean_expected
    if not needed or missing(needed) or "golden-clean" not in have or not p_clean_expected:
        crit["4"] = _crit("NOT_RUN", missing=missing(["golden-clean", *p_clean_expected]))
    else:
        g_clean = by["golden-clean"]
        p_rows = [by[i] for i in p_clean_expected]
        g_alarms = 1 if g_clean["verdict"] == FAIL else 0
        p_alarms = sum(1 for r in p_rows if r["verdict"] == FAIL)
        crit["4"] = _crit(
            "PASS" if g_alarms == 0 and p_alarms <= PAPERLESS_CLEAN_MAX_FALSE_ALARMS else "FAIL",
            golden_clean_false_alarms=g_alarms,
            paperless_clean_false_alarms=p_alarms,
            paperless_clean_units=len(p_rows),
            inconclusive_clean_units=sum(
                1 for r in [g_clean, *p_rows] if r["verdict"] == INCONCLUSIVE
            ),
            inconclusive_by_cause={
                r["unit"]: r["cause"] for r in [g_clean, *p_rows] if r["verdict"] == INCONCLUSIVE
            },
            errors=sorted(r["unit"] for r in [g_clean, *p_rows] if r["error"]),
        )

    # 5. cost
    if not rows or missing(expect):
        crit["5"] = _crit("NOT_RUN", missing=missing(expect))
    else:
        over = sorted(r["unit"] for r in rows if r["tokens"] > TOKEN_CAP)
        provider = {r["unit"]: r["model_errors"] for r in rows if r["model_errors"]}
        crit["5"] = _crit(
            "PASS" if not over else "FAIL",
            max_tokens=max(r["tokens"] for r in rows),
            total_tokens=sum(r["tokens"] for r in rows),
            over_cap=over,
            provider_failures=provider,
            units_with_provider_failures=sorted(provider),
        )

    verdicts: dict[str, int] = {}
    for r in rows:
        key = r["verdict"] or "ERROR"
        verdicts[key] = verdicts.get(key, 0) + 1
    return {
        "measured_result": False,
        "units_expected": len(expect),
        "units_run": len(rows),
        "complete": not missing(expect),
        "verdict_counts": verdicts,
        "errors": sorted(r["unit"] for r in rows if r["error"]),
        "wall_seconds": round(sum(r["wall_seconds"] for r in rows), 1),
        "criteria_text": CRITERIA,
        "criteria": crit,
    }


# --------------------------------------------------------------------------
# Running
# --------------------------------------------------------------------------


@dataclass
class GateConfig:
    out: Path
    workdir: Path
    which: str = "all"
    paperless_repo: Path | None = None
    python: Path | None = None  # host-mode interpreter; built from the golden requirements if None
    golden_mode: str = "sandbox"  # tests only: "host" runs the golden units on the host too
    keep_work: bool = False
    runner: ModelRunner | None = None
    only: list[str] | None = None  # test seam: restrict the selection to these unit ids
    # test seams
    provision: Callable[..., Any] | None = None
    replay: Callable[..., RunData] | None = None


@dataclass
class _Repos:
    golden: Path | None = None
    golden_shas: dict[str, str] = field(default_factory=dict)


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True, timeout=300
    )
    return proc.stdout.strip()


def materialize_golden_repo(dest: Path) -> tuple[Path, dict[str, str]]:
    """The golden app's history plus one branch per mutant (the existing materializer)."""
    manifest, mutants = gate_manifest()
    registered = json.loads(MUTANTS_JSON.read_text(encoding="utf-8"))
    if [m.id for m in mutants] != [m["id"] for m in registered["mutants"]]:
        raise GateError("regenerated mutants do not match experiments/lean-poc/mutants.json")
    if manifest["base_source_sha256"] != registered["base_source_sha256"]:
        raise GateError("golden base source differs from the registered base_source_sha256")
    repo = write_mutant_repo(GATE_APP, GATE_REVISION, mutants, dest=dest)
    messages = _kit()._COMMIT_MESSAGES
    by_msg = {v: k for k, v in messages.items()}
    shas: dict[str, str] = {}
    branch = next(
        b
        for b in _git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads").split()
        if not b.startswith("mutant/")
    )
    for line in _git(repo, "log", "--format=%H%x09%s", branch).splitlines():
        sha, _, subject = line.partition("\t")
        if subject in by_msg:
            shas[by_msg[subject]] = sha
    if set(shas) != set(messages):
        raise GateError("could not map the golden revisions to commits")
    return repo, shas


def _rmtree(path: Path) -> None:
    """Remove a tree this driver created; rootless-podman-owned files need ``podman unshare``."""
    if not path.exists():
        return
    shutil.rmtree(path, ignore_errors=True)
    if path.exists():
        subprocess.run(["podman", "unshare", "rm", "-rf", str(path)], check=False, timeout=600)
        shutil.rmtree(path, ignore_errors=True)


@contextlib.contextmanager
def _capture_replays() -> Iterator[dict[str, Any]]:
    """Record the scenarios and auth the pipeline replays (the impact set), without changing it."""
    cap: dict[str, Any] = {"scenarios": None, "auth": None}
    orig = pipeline.replay_run

    def spy(driver: Any, label: str, scenarios: list[HttpScenario], auth: AuthSpec | None) -> Any:
        if cap["scenarios"] is None:
            cap["scenarios"] = list(scenarios)
            cap["auth"] = auth
        return orig(driver, label, scenarios, auth)

    with mock.patch.object(pipeline, "replay_run", spy):
        yield cap


def run_confirm(
    cfg: GateConfig,
    rc: lean_run.LeanRunConfig,
    scenarios: list[HttpScenario],
    auth: AuthSpec | None,
    flagged: list[Flagged],
) -> dict[str, Any]:
    """Re-run the flagged scenarios on fix^ and fix (two fresh runs each); NO judge, no model."""
    provision = cfg.provision or lean_run.provision_sandbox_sides
    replay = cfg.replay or pipeline.replay_run
    crc = lean_run.LeanRunConfig(
        repo=rc.repo,
        base=PAPERLESS_FIX_PARENT,
        head=PAPERLESS_FIX,
        output_dir=rc.output_dir / "confirm",
        unit_repo=rc.unit_repo,
        django_overlay=rc.django_overlay,
        openapi_path=rc.openapi_path,
        base_sha=pipeline.resolve_sha(rc.repo, PAPERLESS_FIX_PARENT),
        head_sha=pipeline.resolve_sha(rc.repo, PAPERLESS_FIX),
    )
    crc.output_dir.mkdir(parents=True, exist_ok=True)
    contract_file = lean_run._contract_file(crc)
    side_fp = side_fix = None
    try:
        side_fp, side_fix, _contract = provision(
            crc, contract_file, crc.output_dir / "work", Timings()
        )
        fp = (replay(side_fp, "fixp", scenarios, auth), replay(side_fp, "fixp2", scenarios, auth))
        fx = (replay(side_fix, "fix", scenarios, auth), replay(side_fix, "fix2", scenarios, auth))
    finally:
        for side in (side_fp, side_fix):
            if side is not None:
                with contextlib.suppress(Exception):
                    side.close()
    out = confirm_flagged(flagged, fp, fx)
    out.update(fix_parent=PAPERLESS_FIX_PARENT, fix=PAPERLESS_FIX, runs_each=2)
    return out


def _unit_rc(
    cfg: GateConfig, unit: Unit, repos: _Repos, out_dir: Path, mode: str
) -> lean_run.LeanRunConfig:
    if unit.paperless:
        assert cfg.paperless_repo is not None
        return lean_run.LeanRunConfig(
            repo=cfg.paperless_repo,
            base=unit.base,
            head=unit.head,
            output_dir=out_dir,
            unit_repo=PAPERLESS_UNIT_REPO,
            intent=unit.intent,
            no_sandbox=False,
            django_overlay=lean_run.parse_overlay_items(PAPERLESS_OVERLAY),
            openapi_path=PAPERLESS_OPENAPI,
        )
    assert repos.golden is not None
    base = repos.golden_shas.get(unit.base, unit.base)
    head = repos.golden_shas.get(unit.head, unit.head)
    return lean_run.LeanRunConfig(
        repo=repos.golden,
        base=base,
        head=head,
        output_dir=out_dir,
        intent=unit.intent,
        no_sandbox=(mode == "host"),
        python=cfg.python if mode == "host" else None,
    )


def run_unit(cfg: GateConfig, unit: Unit, repos: _Repos) -> dict[str, Any]:
    mode = cfg.golden_mode if unit.group == "golden" else unit.mode
    out_dir = cfg.out / "units" / unit.id
    out_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.perf_counter()
    console = (out_dir / "console.log").open("w", encoding="utf-8") if unit.paperless else None

    def log(msg: str) -> None:
        if console is not None:
            console.write(msg + "\n")
            console.flush()
        else:
            print(f"[{unit.id}] {msg}", flush=True)

    row: dict[str, Any]
    try:
        with contextlib.ExitStack() as stack:
            if console is not None:
                stack.enter_context(contextlib.redirect_stdout(console))
                stack.enter_context(contextlib.redirect_stderr(console))
            cap = stack.enter_context(_capture_replays())
            rc = _unit_rc(cfg, unit, repos, out_dir, mode)
            result, code = lean_run.lean_verify(rc, model_runner=cfg.runner, log=log)
            row = row_from_result(unit, result, code, wall_seconds=0.0, mode=mode)
            if unit.id == "paperless-bug" and result.verdict.status == FAIL:
                row["confirmed"] = False
                try:
                    flagged = flagged_from_verdict(
                        result.verdict.to_dict(), result.exchanges.get("head", [])
                    )
                    if not flagged or not cap["scenarios"]:
                        raise RuntimeError("no flagged items or no replayed scenarios to confirm")
                    conf = run_confirm(cfg, rc, cap["scenarios"], cap["auth"], flagged)
                    row["confirmed"] = bool(conf["confirmed"])
                    (out_dir / "confirm.json").write_text(
                        json.dumps(conf, indent=2, sort_keys=True, default=str) + "\n",
                        encoding="utf-8",
                    )
                except Exception as exc:
                    row["confirm_error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
        row["wall_seconds"] = round(time.perf_counter() - t0, 1)
    except Exception as exc:
        detail = f"{type(exc).__name__}: {str(exc)[:300]}"
        row = error_row(unit, detail, wall_seconds=time.perf_counter() - t0, mode=mode)
    finally:
        if console is not None:
            console.close()
        if not cfg.keep_work:
            _rmtree(out_dir / "work")
            _rmtree(out_dir / "confirm" / "work")
    return row


def log_line(row: dict[str, Any], paperless: bool) -> str:
    """Ids, verdicts, causes and counts only (an error shows its class; text off paperless)."""
    if row["error"]:
        err = str(row["error"])
        shown = err.split(":", 1)[0] if paperless else err
        return f"[{row['unit']}] ERROR {shown} t={row['wall_seconds']}s"
    return (
        f"[{row['unit']}] {row['verdict']} ({row['cause']}) mode={row['mode']} "
        f"observable={row['observable']} diffs={row['differences']} noise={row['noise']} "
        f"unexercised={row['unexercised']} tokens={row['tokens']} calls={row['model_calls']} "
        f"confirmed={row['confirmed']} t={row['wall_seconds']}s"
    )


def _write_summary(
    cfg: GateConfig, rows: list[dict[str, Any]], expected: list[str]
) -> dict[str, Any]:
    summary = compute_summary(rows, expected=expected)
    (cfg.out / "gate-summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return summary


def run_gate(cfg: GateConfig) -> dict[str, Any]:
    units = select_units(load_units(), cfg.which)
    if cfg.only is not None:
        units = [u for u in units if u.id in cfg.only]
    cfg.out.mkdir(parents=True, exist_ok=True)
    cfg.workdir.mkdir(parents=True, exist_ok=True)
    results = cfg.out / "gate-results.jsonl"
    results.write_text("", encoding="utf-8")
    expected = [u.id for u in units]
    repos = _Repos()
    rows: list[dict[str, Any]] = []
    prep_error: str | None = None
    if any(u.group in ("golden", "mutants") for u in units):
        try:
            repos.golden, repos.golden_shas = materialize_golden_repo(cfg.workdir / "golden-repo")
            if cfg.python is None and any(
                (cfg.golden_mode if u.group == "golden" else u.mode) == "host" for u in units
            ):
                from proofdeploy.lean import hostrun

                cfg.python = hostrun.make_venv(
                    cfg.workdir / "host-venv", repos.golden / "requirements.txt"
                )
        except Exception as exc:
            if isinstance(exc, GateError):
                raise
            prep_error = f"golden preparation failed: {type(exc).__name__}: {str(exc)[:300]}"
    for unit in units:
        if unit.paperless and cfg.paperless_repo is None:
            row = error_row(unit, "paperless repo not given (--paperless-repo)", wall_seconds=0.0)
        elif not unit.paperless and prep_error:
            row = error_row(unit, prep_error, wall_seconds=0.0)
        else:
            row = run_unit(cfg, unit, repos)
        rows.append(row)
        with results.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, sort_keys=True, default=str) + "\n")
        print(log_line(row, unit.paperless), flush=True)
        _write_summary(cfg, rows, expected)
    summary = _write_summary(cfg, rows, expected)
    for cid, c in summary["criteria"].items():
        print(f"criterion {cid}: {c['status']}", flush=True)
    return summary


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Lean P6b gate driver (Stage 2 Phase 0).")
    ap.add_argument("--units", choices=GROUPS, default="all")
    ap.add_argument("--out", type=Path, help="output dir (results, summary, unit outputs)")
    ap.add_argument("--workdir", type=Path, help="scratch dir (golden repo, host venv)")
    ap.add_argument("--paperless-repo", type=Path, help="a full clone of paperless-ngx")
    ap.add_argument("--list", action="store_true", help="print the selected unit ids and exit")
    args = ap.parse_args(argv)
    try:
        units = select_units(load_units(), args.units)
        if args.list:
            for u in units:
                print(u.id)
            return 0
        if args.out is None or args.workdir is None:
            print("gate: --out and --workdir are required", file=sys.stderr)
            return lean_run.EXIT_USAGE
        run_gate(
            GateConfig(
                out=args.out,
                workdir=args.workdir,
                which=args.units,
                paperless_repo=args.paperless_repo,
            )
        )
    except GateError as exc:
        print(f"gate: {exc}", file=sys.stderr)
        return lean_run.EXIT_USAGE
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
