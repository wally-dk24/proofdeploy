"""The judge: the AI only judges observed behaviour differences.

Step 4 of the lean path. Code gathers the differences (old==old', new==new'
stable runs), groups them, packs them into small prompts and asks the model
one question per group: is the NEW behaviour correct, a regression, or
unclear? Code then checks the model's quoted evidence mechanically. A
judgment whose evidence fails the check is demoted to ``unclear``; the AI
never decides the verdict (see ``verdict.py``).

The model is reached only through the PINNED ``proofdeploy.model_client``
(``call_model``); this module never edits it. Tests pass a stub ``runner``.

Conventions
-----------
* ``field_path`` is a json path in the response (``results[].done``); for a
  ``status_changed`` difference it is ``$status`` and before/after are the
  HTTP status codes. For ``added_field`` ``before`` is None; for
  ``removed_field`` ``after`` is None (the kind says the value is absent).
* A path template replaces id-like segments (digits, uuids) by ``{id}``
  unless a known template (from the app index) matches the path.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from proofdeploy.model_client import ModelCallError, ModelResponse, ModelRunner, call_model

DIFFERENCE_KINDS = ("changed", "added_field", "removed_field", "status_changed")
HARD_FAILURE_KINDS = ("crash", "5xx", "timeout", "schema_violation", "new_exception")
VERDICTS = ("correct", "regression", "unclear")
SOURCES = ("intent", "facts", "exchange", "schema")

DEFAULT_TOKEN_BUDGET = 10_000
DEFAULT_PROMPT_CAP = 3000

JUDGE_SKILL_PATH = (
    Path(__file__).resolve().parents[2] / "docs" / "registration" / "judge-skill-v1.md"
)
JUDGE_SKILL_SHA256 = "edbd35971dc3a9886082b92abf40c719aacf147c9632f57801649771bd557877"
_PART_A_HEADER = "## Part A \u2014 text sent to the judge\n\n"
_PART_A_END = "\n---\n"


class JudgeSkillError(Exception):
    """The registered judge skill is missing, altered or malformed: refuse to run."""


def load_part_a(path: Path | None = None) -> str:
    """Part A of the signed judge skill (D267, judge skill v1), word for word.

    The file's sha256 must equal ``JUDGE_SKILL_SHA256``; otherwise nothing is
    sent to the model.
    """
    p = path or JUDGE_SKILL_PATH
    try:
        raw = p.read_bytes()
    except OSError as exc:
        raise JudgeSkillError(f"judge skill unreadable: {p}: {exc}") from exc
    got = hashlib.sha256(raw).hexdigest()
    if got != JUDGE_SKILL_SHA256:
        raise JudgeSkillError(f"judge skill sha256 {got} != registered {JUDGE_SKILL_SHA256} ({p})")
    text = raw.decode("utf-8")
    start = text.find(_PART_A_HEADER)
    if start < 0:
        raise JudgeSkillError("judge skill has no Part A header")
    start += len(_PART_A_HEADER)
    end = text.find(_PART_A_END, start)
    if end < 0:
        raise JudgeSkillError("judge skill Part A has no end marker")
    return text[start:end].strip()


_part_a_cache: str | None = None


def judge_prompt() -> str:
    """Part A, loaded and verified once per process."""
    global _part_a_cache
    if _part_a_cache is None:
        _part_a_cache = load_part_a()
    return _part_a_cache


CORRECTION_SENTENCE = (
    "Keep every other answer. For each, quote a shorter exact passage that contains the "
    "value, from facts, exchange, schema or intent; if none exists, answer unclear."
)
_CORRECTION_HEAD = "\n\n---\n\nYour previous answer:\n\n"
CORRECTION_ANSWER_CHARS = 1200  # the largest first answer the budget guard allows for
_CORRECTION_LINE_CHARS = 60  # "group <n>: <reason>" per group, reserved
MAX_EXCHANGES = 3

DEFAULT_MIN_CALL_INTERVAL = 13.0
DEFAULT_429_RETRIES = 3
BACKOFF_429_SECONDS = (20.0, 40.0, 60.0)
ENV_MIN_CALL_INTERVAL = "PROOFDEPLOY_MIN_CALL_INTERVAL"
ENV_429_RETRIES = "PROOFDEPLOY_429_RETRIES"

_MAX_SHOWN_IDS = 5
_MAX_SHOWN_VALUE = 400
_MAX_SHOWN_BODY = 300


# --------------------------------------------------------------------------
# Input shapes
# --------------------------------------------------------------------------


@dataclass
class Difference:
    request_id: str
    method: str
    path: str
    request_body: Any
    field_path: str
    before: Any
    after: Any
    kind: str
    # Earlier exchanges of the same scenario and run about the same record:
    # {"method", "path", "field", "value"}. Old records have none.
    exchanges: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.kind not in DIFFERENCE_KINDS:
            raise ValueError(f"difference kind {self.kind!r} not in {DIFFERENCE_KINDS}")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Difference:
        return cls(**d)


@dataclass
class HardFailure:
    request_id: str
    kind: str
    detail: str

    def __post_init__(self) -> None:
        if self.kind not in HARD_FAILURE_KINDS:
            raise ValueError(f"hard failure kind {self.kind!r} not in {HARD_FAILURE_KINDS}")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> HardFailure:
        return cls(**d)


@dataclass
class JudgeContext:
    intent: str
    changed_hunks: str
    seed_facts: list[str] = field(default_factory=list)
    field_descriptions: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> JudgeContext:
        return cls(
            intent=d["intent"],
            changed_hunks=d["changed_hunks"],
            seed_facts=list(d.get("seed_facts") or []),
            field_descriptions=dict(d.get("field_descriptions") or {}),
        )


# --------------------------------------------------------------------------
# Grouping
# --------------------------------------------------------------------------

_ID_SEGMENT = re.compile(
    r"^(?:\d+|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})$"
)


def _canon(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def path_template(path: str, templates: tuple[str, ...] | list[str] = ()) -> str:
    """The route template for a concrete path (query string dropped)."""
    bare = path.split("?", 1)[0]
    segs = bare.split("/")
    for tpl in templates:
        tsegs = tpl.split("/")
        if len(tsegs) == len(segs) and all(
            t == s or (t.startswith("{") and t.endswith("}"))
            for t, s in zip(tsegs, segs, strict=True)
        ):
            return tpl
    return "/".join("{id}" if _ID_SEGMENT.match(s) else s for s in segs)


@dataclass
class Group:
    index: int  # 1-based, the number the model answers with
    method: str
    path_template: str
    field_path: str
    kind: str
    before: Any
    after: Any
    request_ids: list[str]
    sample_path: str
    sample_request_body: Any
    exchanges: list[dict[str, Any]] = field(default_factory=list)  # the first request's

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def group_differences(
    differences: list[Difference], templates: tuple[str, ...] | list[str] = ()
) -> list[Group]:
    """Identical (method, path template, field_path, before, after) across
    requests become one group; every request id is kept. First-seen order.

    The kind is part of the key too: ``null -> x`` as a changed field and as
    an added field are different facts.
    """
    groups: dict[tuple[str, ...], Group] = {}
    for d in differences:
        tpl = path_template(d.path, templates)
        key = (d.method.upper(), tpl, d.field_path, d.kind, _canon(d.before), _canon(d.after))
        g = groups.get(key)
        if g is None:
            g = Group(
                index=len(groups) + 1,
                method=d.method.upper(),
                path_template=tpl,
                field_path=d.field_path,
                kind=d.kind,
                before=d.before,
                after=d.after,
                request_ids=[],
                sample_path=d.path,
                sample_request_body=d.request_body,
                exchanges=list(d.exchanges),
            )
            groups[key] = g
        if d.request_id not in g.request_ids:
            g.request_ids.append(d.request_id)
    return list(groups.values())


# --------------------------------------------------------------------------
# Earlier exchanges (judge skill v1, Part B 2)
# --------------------------------------------------------------------------


def record_id_of(path: str) -> str | None:
    """The last numeric or uuid segment of a path (query dropped)."""
    for seg in reversed(path.split("?", 1)[0].split("/")):
        if _ID_SEGMENT.match(seg):
            return seg
    return None


def _objects_with_id(body: Any, rid: str) -> list[dict[str, Any]]:
    nodes = body if isinstance(body, list) else [body]
    return [n for n in nodes if isinstance(n, dict) and str(n.get("id")) == rid and "id" in n]


def attach_exchanges(differences: list[Difference], run_exchanges: list[dict[str, Any]]) -> None:
    """Fill each difference's ``exchanges`` from one run's exchanges (the head run).

    ``run_exchanges`` are ``Exchange.to_dict()`` rows in execution order. For a
    difference, up to ``MAX_EXCHANGES`` earlier exchanges (lower step number) of
    the SAME scenario whose JSON body, or an object inside a list body, has an
    ``id`` equal to the record id of the difference's path and carries the field
    under judgement. The nearest ones are kept, oldest first.
    """
    by_scenario: dict[str, list[dict[str, Any]]] = {}
    for ex in run_exchanges:
        by_scenario.setdefault(str(ex.get("scenario_id")), []).append(ex)
    for d in differences:
        d.exchanges = []
        scenario, _, step_s = d.request_id.rpartition(":")
        rid = record_id_of(d.path)
        name = field_name(d.field_path)
        if rid is None or not name or d.field_path.startswith("$") or not step_s.isdigit():
            continue
        step = int(step_s)
        found: list[dict[str, Any]] = []
        for ex in by_scenario.get(scenario, []):
            if int(ex.get("step_index", 0)) >= step:
                continue
            req = ex.get("request") or {}
            for obj in _objects_with_id(ex.get("body_json_or_text"), rid):
                if name in obj:
                    row = {
                        "method": str(req.get("method", "")).upper(),
                        "path": str(req.get("url_path", "")),
                        "field": name,
                        "value": obj[name],
                    }
                    if row not in found and len(_canon(row["value"])) <= _MAX_SHOWN_VALUE:
                        found.append(row)
        d.exchanges = found[-MAX_EXCHANGES:]


# --------------------------------------------------------------------------
# Names, sources, hunks
# --------------------------------------------------------------------------


def estimate_tokens(text: str) -> int:
    """ceil(chars / 3.2), in integer arithmetic."""
    return -(-len(text) * 10 // 32)


def field_name(field_path: str) -> str:
    """The last segment of a json path, without index brackets."""
    last = field_path.lstrip("$").split(".")[-1]
    last = re.sub(r"\[[^\]]*\]", "", last)
    return last.strip()


def _name_words(name: str) -> list[str]:
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name)
    words = [w for w in re.split(r"[^A-Za-z0-9]+", spaced) if w]
    if not words:
        return []
    long_words = [w for w in words if len(w) >= 3]
    return long_words or words


def _word_regex(word: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![A-Za-z0-9]){re.escape(word)}(?![A-Za-z0-9])", re.IGNORECASE)


def _name_regex(name: str) -> re.Pattern[str] | None:
    parts = [p for p in re.split(r"_", name) if p]
    if not parts:
        return None
    body = "[ _-]?".join(re.escape(p) for p in parts)
    return re.compile(rf"(?<![A-Za-z0-9]){body}(?![A-Za-z0-9])", re.IGNORECASE)


def split_hunks(changed_hunks: str) -> list[str]:
    """Split a diff into hunks at ``@@`` / ``diff --git`` lines."""
    hunks: list[list[str]] = []
    for line in changed_hunks.splitlines(keepends=True):
        if line.startswith("@@") or line.startswith("diff --git"):
            if line.startswith("@@") and hunks and not any(ln.startswith("@@") for ln in hunks[-1]):
                hunks[-1].append(line)  # file header + first hunk marker stay together
                continue
            hunks.append([line])
        elif hunks:
            hunks[-1].append(line)
        else:
            hunks.append([line])
    return ["".join(h) for h in hunks]


def _touch_names(groups: list[Group]) -> list[str]:
    names: list[str] = []
    for g in groups:
        fn = field_name(g.field_path)
        if fn:
            names.append(fn.lower())
        for seg in g.path_template.split("/"):
            if seg and not seg.startswith("{") and len(seg) >= 3:
                names.append(seg.lower())
    return sorted(set(names))


def trim_hunks(changed_hunks: str, groups: list[Group]) -> str:
    """Only the hunks that mention the groups' field or path names."""
    names = _touch_names(groups)
    kept = [h for h in split_hunks(changed_hunks) if any(n in h.lower() for n in names)]
    return "".join(kept)


def added_lines_text(changed_hunks: str) -> str:
    """The added lines of a diff, whitespace-normalized, joined by a space."""
    out = []
    for line in changed_hunks.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            out.append(" ".join(line[1:].split()))
    return " ".join(out)


# --------------------------------------------------------------------------
# Prompt and batching
# --------------------------------------------------------------------------


def build_correction_prompt(prompt: str, raw: str, rejected: list[tuple[int, str]]) -> str:
    """The one follow-up of a call (judge skill v1, Part B 4): the original
    prompt, the model's own answer, one line per rejected group, the signed
    sentence."""
    lines = "\n".join(f"group {n}: {reason}" for n, reason in rejected)
    return f"{prompt}{_CORRECTION_HEAD}{raw}\n\n{lines}\n{CORRECTION_SENTENCE}"


def correction_reserve_tokens(n_groups: int) -> int:
    """Tokens the correction message adds to a prompt of ``n_groups`` groups, for a
    first answer of ``CORRECTION_ANSWER_CHARS`` characters."""
    filler = (
        _CORRECTION_HEAD
        + "x" * CORRECTION_ANSWER_CHARS
        + "\n\n"
        + "x" * (_CORRECTION_LINE_CHARS * max(n_groups, 1))
        + "\n"
        + CORRECTION_SENTENCE
    )
    return estimate_tokens(filler)


def exchange_line(e: dict[str, Any]) -> str:
    """``"<field>": <json value>``: the one line an earlier exchange contributes."""
    return f"{json.dumps(str(e['field']), ensure_ascii=False)}: {_canon(e['value'])}"


def _shown(value: Any, limit: int) -> str:
    s = _canon(value)
    if len(s) > limit:
        return s[:limit] + "...[shown truncated]"
    return s


def _relevant_descriptions(ctx: JudgeContext, groups: list[Group]) -> dict[str, str]:
    wanted_paths = {g.field_path for g in groups}
    wanted_names = {field_name(g.field_path) for g in groups}
    out = {}
    for key, desc in sorted(ctx.field_descriptions.items()):
        if key in wanted_paths or field_name(key.split(":", 1)[-1]) in wanted_names:
            out[key] = desc
    return out


def render_prompt(ctx: JudgeContext, groups: list[Group], hunks: str, hunks_trimmed: bool) -> str:
    parts = [judge_prompt(), "INTENT:\n" + ctx.intent.strip()]
    if ctx.seed_facts:
        parts.append("KNOWN FACTS:\n" + "\n".join(f"- {f}" for f in ctx.seed_facts))
    descs = _relevant_descriptions(ctx, groups)
    if descs:
        parts.append("FIELD DESCRIPTIONS:\n" + "\n".join(f"{k}: {v}" for k, v in descs.items()))
    note = " (only hunks touching these fields or paths are shown)" if hunks_trimmed else ""
    parts.append(f"CHANGED CODE (not evidence){note}:\n{hunks.strip()}")
    lines = []
    for g in groups:
        ids = ", ".join(g.request_ids[:_MAX_SHOWN_IDS])
        more = len(g.request_ids) - _MAX_SHOWN_IDS
        if more > 0:
            ids += f" (+{more} more)"
        lines.append(
            f"[{g.index}] {g.method} {g.path_template}, field {g.field_path or '$'} "
            f"({g.kind}): before={_shown(g.before, _MAX_SHOWN_VALUE)} "
            f"after={_shown(g.after, _MAX_SHOWN_VALUE)}\n"
            f"    requests: {ids}; example: {g.method} {g.sample_path} "
            f"body={_shown(g.sample_request_body, _MAX_SHOWN_BODY)}"
            + "".join(
                f"\n    earlier in this run: {e['method']} {e['path']} \u2192 {exchange_line(e)}"
                for e in g.exchanges[:MAX_EXCHANGES]
            )
        )
    parts.append("DIFFERENCES:\n" + "\n".join(lines))
    return "\n\n".join(parts) + "\n"


@dataclass
class CallPlan:
    index: int
    group_ids: list[int]
    prompt: str
    hunks_trimmed: bool

    @property
    def prompt_sha256(self) -> str:
        return hashlib.sha256(self.prompt.encode("utf-8")).hexdigest()


def plan_calls(
    groups: list[Group], ctx: JudgeContext, prompt_cap: int = DEFAULT_PROMPT_CAP
) -> tuple[list[CallPlan], dict[int, str]]:
    """Greedy packing under the prompt cap.

    Returns the call plans and ``{group index: cause}`` for groups that
    cannot fit even alone with trimmed hunks (cause ``ai_budget``). Nothing
    is trimmed silently: a trimmed call is flagged on its plan.
    """
    plans: list[CallPlan] = []
    too_big: dict[int, str] = {}
    if declares_no_behaviour_change(ctx.intent) is not None:
        return plans, too_big  # code decides these groups; no call is planned
    batch: list[Group] = []

    def fits(gs: list[Group], hunks: str, trimmed: bool) -> bool:
        # Room is kept for the correction message, so the follow-up call also
        # stays under the cap (judge skill v1, Part B 7).
        used = estimate_tokens(render_prompt(ctx, gs, hunks, trimmed))
        return used + correction_reserve_tokens(len(gs)) <= prompt_cap

    def flush() -> None:
        nonlocal batch
        if batch:
            plans.append(
                CallPlan(
                    index=len(plans),
                    group_ids=[g.index for g in batch],
                    prompt=render_prompt(ctx, batch, ctx.changed_hunks, False),
                    hunks_trimmed=False,
                )
            )
            batch = []

    for g in groups:
        if fits(batch + [g], ctx.changed_hunks, False):
            batch.append(g)
            continue
        if batch:
            flush()
            if fits([g], ctx.changed_hunks, False):
                batch = [g]
                continue
        trimmed = trim_hunks(ctx.changed_hunks, [g])
        if fits([g], trimmed, True):
            plans.append(
                CallPlan(
                    index=len(plans),
                    group_ids=[g.index],
                    prompt=render_prompt(ctx, [g], trimmed, True),
                    hunks_trimmed=True,
                )
            )
        else:
            too_big[g.index] = "ai_budget"
    flush()
    return plans, too_big


# --------------------------------------------------------------------------
# Model call (pinned client) with one retry
# --------------------------------------------------------------------------


@dataclass
class CallResult:
    index: int
    status: str  # ok | error | skipped_budget
    raw_content: str = ""
    usage: dict[str, Any] | None = None
    tokens: int = 0
    tokens_estimated: bool = False
    error_cause: str | None = None
    attempts: list[str] = field(default_factory=list)
    waited_seconds: float = 0.0  # total time slept for pacing and 429 backoff
    # The one correction round (tokens, waits and attempts above include it).
    correction_used: bool = False  # a follow-up was sent
    correction_raw: str | None = None  # its answer; None when it failed
    correction_prompt_sha256: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _call_pinned(prompt: str, runner: ModelRunner | None) -> ModelResponse:
    """``call_model`` with the failure taxonomy normalized, as
    ``orchestrator._call_author_probes`` does: the pinned client raises a
    bare ``RuntimeError`` on an HTTP or transport failure."""
    try:
        return call_model(prompt, runner)
    except ModelCallError:
        raise
    except RuntimeError as exc:
        raise ModelCallError(str(exc), cause="model_transport") from exc


# --- pacing and 429 backoff -------------------------------------------------
# The clock and the sleep are module-level so tests can patch them.

_last_call_started: float | None = None


def _clock() -> float:
    return time.monotonic()


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def reset_pacer() -> None:
    """Forget the previous call's start (tests)."""
    global _last_call_started
    _last_call_started = None


def min_call_interval() -> float:
    raw = os.environ.get(ENV_MIN_CALL_INTERVAL)
    if raw is None or not raw.strip():
        return DEFAULT_MIN_CALL_INTERVAL
    try:
        value = float(raw)
    except ValueError:
        value = float("nan")
    if not value >= 0 or value == float("inf"):
        raise ValueError(f"{ENV_MIN_CALL_INTERVAL} must be a float >= 0, got {raw!r}")
    return value


def max_429_retries() -> int:
    raw = os.environ.get(ENV_429_RETRIES)
    if raw is None or not raw.strip():
        return DEFAULT_429_RETRIES
    try:
        value = int(raw.strip())
    except ValueError:
        raise ValueError(f"{ENV_429_RETRIES} must be an integer >= 0, got {raw!r}") from None
    if value < 0:
        raise ValueError(f"{ENV_429_RETRIES} must be an integer >= 0, got {raw!r}")
    return value


def _pace() -> float:
    """Wait until ``min_call_interval`` seconds have passed since the previous
    call started in this process, then mark this call as started. Returns the
    time slept."""
    global _last_call_started
    interval = min_call_interval()
    slept = 0.0
    now = _clock()
    if _last_call_started is not None:
        wait = _last_call_started + interval - now
        if wait > 0:
            _sleep(wait)
            slept = wait
            now = _clock()
    _last_call_started = now
    return slept


def http_status_of(message: str) -> int | None:
    m = re.search(r"HTTP (\d{3})", message)
    return int(m.group(1)) if m else None


def usage_tokens(usage: dict[str, Any] | None, prompt: str, content: str) -> tuple[int, bool]:
    """Tokens a call used: the response's usage if present, else the estimate."""
    if usage:
        total = usage.get("total_tokens")
        if isinstance(total, int) and not isinstance(total, bool):
            return total, False
        p, c = usage.get("prompt_tokens"), usage.get("completion_tokens")
        if isinstance(p, int) and isinstance(c, int):
            return p + c, False
    return estimate_tokens(prompt) + estimate_tokens(content), True


def execute_call(prompt: str, index: int, runner: ModelRunner | None) -> CallResult:
    """One model call, paced. A ``model_transport`` error is retried once; an
    HTTP 429 is retried up to ``max_429_retries`` times after 20, 40, 60 s,
    then ends as ``provider_limit``."""
    attempts: list[str] = []
    waited = 0.0
    retries_429 = max_429_retries()
    used_429 = 0
    transport_retried = False
    attempt = 0
    while True:
        attempt += 1
        waited += _pace()
        try:
            resp = _call_pinned(prompt, runner)
        except ModelCallError as exc:
            attempts.append(f"attempt_{attempt}: {exc.cause}: {str(exc)[:300]}")
            if exc.cause == "model_transport":
                if http_status_of(str(exc)) == 429:
                    if used_429 >= retries_429:
                        return CallResult(
                            index=index,
                            status="error",
                            error_cause="provider_limit",
                            attempts=attempts,
                            waited_seconds=waited,
                        )
                    delay = BACKOFF_429_SECONDS[min(used_429, len(BACKOFF_429_SECONDS) - 1)]
                    used_429 += 1
                    attempts.append(f"attempt_{attempt + 1}: waited {delay:.1f}s after 429")
                    _sleep(delay)
                    waited += delay
                    continue
                if not transport_retried:
                    transport_retried = True
                    continue
            return CallResult(
                index=index,
                status="error",
                error_cause=exc.cause,
                attempts=attempts,
                waited_seconds=waited,
            )
        attempts.append(f"attempt_{attempt}: success")
        tokens, est = usage_tokens(resp.usage, prompt, resp.content)
        return CallResult(
            index=index,
            status="ok",
            raw_content=resp.content,
            usage=resp.usage,
            tokens=tokens,
            tokens_estimated=est,
            attempts=attempts,
            waited_seconds=waited,
        )


# --------------------------------------------------------------------------
# Answers and the deterministic evidence check
# --------------------------------------------------------------------------


@dataclass
class Judgment:
    group: int
    verdict: str  # correct | regression | unclear | unjudged (final, after the check)
    model_verdict: str | None = None
    expected_value: Any = None
    evidence_source: str | None = None
    quote: str | None = None
    question: str = ""
    cause: str | None = None  # why a group is unclear / unjudged
    check: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class JudgedGroup:
    group: Group
    judgment: Judgment

    def to_dict(self) -> dict[str, Any]:
        return {"group": self.group.to_dict(), "judgment": self.judgment.to_dict()}


def parse_answers(content: str) -> list[dict[str, Any]] | None:
    """The JSON array in the model's reply, or None when there is none."""
    text = content.strip()
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except ValueError:
        return None
    if not isinstance(data, list) or not all(isinstance(x, dict) for x in data):
        return None
    return data


def _norm(s: str) -> str:
    return " ".join(s.split())


_LIST_MARKER = re.compile(r"^(?:[-*\u2022]|\d{1,3}[.)])\s+")


def _norm_quote(quote: str) -> str:
    """Per line: strip whitespace and ONE leading list marker (the prompt shows
    facts as ``- <fact>``), then collapse whitespace as ``_norm`` does."""
    lines = []
    for line in quote.splitlines() or [quote]:
        lines.append(_LIST_MARKER.sub("", line.lstrip(), count=1).strip())
    return _norm(" ".join(lines))


def _source_items(source: str, ctx: JudgeContext, g: Group) -> list[str]:
    if source == "exchange":
        return [_norm(exchange_line(e)) for e in g.exchanges[:MAX_EXCHANGES]]
    if source == "intent":
        return [_norm(ctx.intent)]
    if source == "facts":
        return [_norm(f) for f in ctx.seed_facts]
    if source == "schema":
        return [_norm(v) for v in ctx.field_descriptions.values()]
    return []


def _has_value(quote: str, value: Any) -> bool:
    if isinstance(value, bool):
        return re.search(rf"\b{'true' if value else 'false'}\b", quote, re.IGNORECASE) is not None
    if value is None:
        return False
    rendered = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return bool(rendered) and rendered in quote


_RECORD_MENTION = re.compile(r"(?i)(?:\bid\s*[=:#]?\s*|#)(\d+)")


def _fact_parts(fact: str, rid: str | None) -> list[str]:
    """The text of a fact that speaks about this record.

    A fact that names records (``id 1 (...) and id 2 (...)``) is split into
    clauses, each running from one record mention to the next; only the
    clauses for ``rid`` count (none: the fact does not count). A fact without
    record mentions, or a difference without a record id, is used whole.
    """
    mentions = list(_RECORD_MENTION.finditer(fact))
    if not mentions or rid is None:
        return [fact]
    out = []
    for i, m in enumerate(mentions):
        end = mentions[i + 1].start() if i + 1 < len(mentions) else len(fact)
        if m.group(1) == rid:
            out.append(fact[m.start() : end])
    return out


def _contradicted_by_data(g: Group, answer: dict[str, Any], ctx: JudgeContext) -> bool:
    """Data before intent (judge skill v1, Part B 3).

    True when a ``correct`` answer rests on the intent but the app's data says
    another value for this field. Mechanical, in two parts:

    * a fact counts (for a difference on a record, only the clauses of the fact
      about that record, see ``_fact_parts``) when it names the field
      (``_name_regex``) and contains the value the field had before (or the answer's ``expected_value``) that
      ``_has_value`` finds, while it does not contain ``after``;
    * a shown exchange counts when its value for the field differs from ``after``.

    A ``removed_field`` is exempt: the data necessarily shows the field that the
    intent asks to remove.
    """
    if g.kind == "removed_field":
        return False
    name_re = _name_regex(field_name(g.field_path))
    other = [v for v in (g.before, answer.get("expected_value")) if v is not None]
    rid = record_id_of(g.sample_path)
    if name_re is not None:
        for fact in ctx.seed_facts:
            for f in _fact_parts(_norm(fact), rid):
                if not name_re.search(f) or _has_value(f, g.after):
                    continue
                if any(_has_value(f, v) for v in other if _canon(v) != _canon(g.after)):
                    return True
    return any(_canon(e["value"]) != _canon(g.after) for e in g.exchanges[:MAX_EXCHANGES])


def check_evidence(
    g: Group, answer: dict[str, Any], ctx: JudgeContext
) -> tuple[bool, str | None, list[str]]:
    """The deterministic evidence check for a correct / regression answer.

    Returns (ok, reason, checks passed). Checked in order: source, quote
    verbatim in that source, not self-justifying (the quote must not be a
    substring of the changed hunks' added lines), then the verdict rules.
    """
    passed: list[str] = []
    ev = answer.get("evidence")
    if not isinstance(ev, dict):
        return False, "no_evidence", passed
    source, quote = ev.get("source"), ev.get("quote")
    if source not in SOURCES:
        return False, "bad_source", passed
    if not isinstance(quote, str):
        return False, "empty_quote", passed
    q = _norm_quote(quote)
    if not q:
        return False, "empty_quote", passed
    if not any(q in item for item in _source_items(source, ctx, g)):
        return False, "quote_not_in_source", passed
    passed.append("verbatim_in_source")
    if q in added_lines_text(ctx.changed_hunks):
        return False, "self_justifying", passed
    passed.append("not_self_justifying")

    verdict = answer.get("verdict")
    expected = answer.get("expected_value")
    name = field_name(g.field_path)
    name_re = _name_regex(name)
    has_name = bool(name_re and name_re.search(q))
    absent_after = g.kind == "removed_field"

    values = [expected, g.before, g.after] if verdict == "regression" else [g.before, g.after]
    is_bool = any(isinstance(v, bool) for v in values)
    if is_bool and re.search(r"\btrue\b", q, re.I) and re.search(r"\bfalse\b", q, re.I):
        return False, "ambiguous_boolean_quote", passed
    passed.append("not_ambiguous")

    if verdict == "regression":
        if expected is None:
            return False, "missing_expected_value", passed
        if not absent_after and _canon(expected) == _canon(g.after):
            return False, "expected_equals_after", passed
        passed.append("expected_differs_from_after")
        # A FAIL must rest on evidence of the correct value. Naming the field
        # alone is enough only for a removed field (the quote establishes that
        # the field should exist).
        if g.kind == "removed_field":
            if not (_has_value(q, expected) or has_name):
                return False, "quote_lacks_expected_value", passed
        elif not _has_value(q, expected):
            return False, "quote_lacks_expected_value", passed
        passed.append("quote_states_expected_value")
        return True, None, passed

    # verdict == "correct"
    has_after = (not absent_after) and _has_value(q, g.after)
    if not (has_after or has_name):
        return False, "quote_lacks_after_or_field", passed
    passed.append("quote_names_after_or_field")
    if source == "intent" and not has_name:
        words = _name_words(name)
        if not any(_word_regex(w).search(q) for w in words):
            return False, "intent_lacks_field_name", passed
        passed.append("intent_names_field")
    if source == "intent" and _contradicted_by_data(g, answer, ctx):
        return False, "intent_contradicted_by_data", passed
    passed.append("not_contradicted_by_data")
    return True, None, passed


_NO_CHANGE_PHRASES = (
    "no behaviour change",
    "no behavior change",
    "no functional change",
    "no behaviour changes",
    "no behavior changes",
    "no functional changes",
)
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")
DECLARED_RULE = "declared_no_behaviour_change"


def declares_no_behaviour_change(intent: str) -> str | None:
    """The first sentence of the intent that declares no behaviour change, or
    None. Only the exact phrases count; the word "refactor" alone does not."""
    for line in intent.splitlines():
        for sentence in _SENTENCE_SPLIT.split(line):
            text = sentence.strip()
            if text and any(p in _norm(text).lower() for p in _NO_CHANGE_PHRASES):
                return text
    return None


def _declared_judgment(g: Group, sentence: str) -> Judgment:
    """A stable base!=head difference under a declared refactor: the correct
    value is the base value. Decided by code; no model is asked."""
    check: dict[str, Any] = {
        "ok": True,
        "reason": None,
        "passed": [DECLARED_RULE],
        "rule": DECLARED_RULE,
    }
    expected = g.before
    if g.kind == "added_field":
        expected = None
        check["expected_absent"] = True
    return Judgment(
        group=g.index,
        verdict="regression",
        model_verdict=None,
        expected_value=expected,
        evidence_source="intent",
        quote=sentence,
        check=check,
    )


def _auto_question(g: Group, cause: str) -> str:
    return (
        f"Is the new value of {g.field_path or '$'} on {g.method} {g.path_template} "
        f"({_shown(g.after, 80)}, was {_shown(g.before, 80)}) intended? ({cause})"
    )


def _unclear(g: Group, cause: str, **kw: Any) -> Judgment:
    question = kw.pop("question", "") or _auto_question(g, cause)
    return Judgment(group=g.index, verdict="unclear", cause=cause, question=question, **kw)


def judge_answer(g: Group, answer: dict[str, Any] | None, ctx: JudgeContext) -> Judgment:
    """Turn one raw answer (or its absence) into a checked judgment."""
    if answer is None:
        return _unclear(g, "no_answer", check={"ok": False, "reason": "no_answer", "passed": []})
    mv = answer.get("verdict")
    if mv not in VERDICTS:
        return _unclear(
            g,
            "bad_verdict",
            model_verdict=str(mv),
            check={"ok": False, "reason": "bad_verdict", "passed": []},
        )
    raw_ev = answer.get("evidence")
    ev: dict[str, Any] = raw_ev if isinstance(raw_ev, dict) else {}
    base: dict[str, Any] = {
        "model_verdict": mv,
        "expected_value": answer.get("expected_value"),
        "evidence_source": ev.get("source") if isinstance(ev.get("source"), str) else None,
        "quote": ev.get("quote") if isinstance(ev.get("quote"), str) else None,
    }
    if mv == "unclear":
        q = answer.get("question")
        return _unclear(
            g,
            "model_unclear",
            question=q if isinstance(q, str) else "",
            check={"ok": True, "reason": None, "passed": []},
            **base,
        )
    ok, reason, passed = check_evidence(g, answer, ctx)
    check = {"ok": ok, "reason": reason, "passed": passed}
    if not ok:
        return _unclear(g, f"evidence_rejected:{reason}", check=check, **base)
    return Judgment(group=g.index, verdict=mv, check=check, **base)


class CorrectionMismatch(Exception):
    """A stored correction round does not follow from the stored first answer."""


def judge_call_answer(
    plan: CallPlan, by_index: dict[int, Group], ctx: JudgeContext, content: str
) -> dict[int, Judgment]:
    """The checked judgments of one call's groups from one raw answer."""
    parsed = parse_answers(content)
    if parsed is None:
        return {
            gi: _unclear(
                by_index[gi],
                "unparsed_answer",
                check={"ok": False, "reason": "unparsed_answer", "passed": []},
            )
            for gi in plan.group_ids
        }
    answers: dict[int, dict[str, Any]] = {}
    for a in parsed:
        n = a.get("group")
        if isinstance(n, int) and not isinstance(n, bool) and n in plan.group_ids:
            answers.setdefault(n, a)
    return {gi: judge_answer(by_index[gi], answers.get(gi), ctx) for gi in plan.group_ids}


def rejected_groups(plan: CallPlan, judgments: dict[int, Judgment]) -> list[tuple[int, str]]:
    """(group, reason) for every group the evidence check rejected, in call order."""
    out = []
    for gi in plan.group_ids:
        cause = judgments[gi].cause or ""
        if cause.startswith("evidence_rejected:"):
            out.append((gi, cause.split(":", 1)[1]))
    return out


def judge_from_calls(
    groups: list[Group],
    ctx: JudgeContext,
    plans: list[CallPlan],
    too_big: dict[int, str],
    results: list[CallResult],
) -> list[JudgedGroup]:
    """Pure: the judged groups from the plans and the calls' raw results.

    Used for the live run and for replay, so both derive identical output.
    """
    declared = declares_no_behaviour_change(ctx.intent)
    if declared is not None:
        return [JudgedGroup(g, _declared_judgment(g, declared)) for g in groups]
    by_index = {g.index: g for g in groups}
    judgments: dict[int, Judgment] = {}
    for gi, cause in too_big.items():
        judgments[gi] = _unclear(
            by_index[gi], cause, check={"ok": False, "reason": cause, "passed": []}
        )
    res_by_call = {r.index: r for r in results}
    for plan in plans:
        r = res_by_call.get(plan.index)
        failure: str | None = None
        if r is None or r.status == "skipped_budget":
            failure = "ai_budget"
        elif r.status == "error":
            failure = f"environment:{r.error_cause}"
        if failure or r is None:
            for gi in plan.group_ids:
                judgments[gi] = _unclear(
                    by_index[gi],
                    failure or "ai_budget",
                    check={"ok": False, "reason": failure, "passed": []},
                )
            continue
        first = judge_call_answer(plan, by_index, ctx, r.raw_content)
        if r.correction_raw is not None:
            rejected = rejected_groups(plan, first)
            expected_sha = hashlib.sha256(
                build_correction_prompt(plan.prompt, r.raw_content, rejected).encode("utf-8")
            ).hexdigest()
            if not rejected or r.correction_prompt_sha256 != expected_sha:
                raise CorrectionMismatch(
                    "the recorded correction does not follow from the first answer"
                )
            first = judge_call_answer(plan, by_index, ctx, r.correction_raw)
        judgments.update(first)
    return [JudgedGroup(g, judgments[g.index]) for g in groups]


@dataclass
class JudgeRun:
    groups: list[Group]
    plans: list[CallPlan]
    too_big: dict[int, str]
    results: list[CallResult]
    judged: list[JudgedGroup]

    @property
    def total_tokens(self) -> int:
        return sum(r.tokens for r in self.results)


def _correct(
    r: CallResult,
    plan: CallPlan,
    groups: list[Group],
    ctx: JudgeContext,
    runner: ModelRunner | None,
    headroom: int,
) -> None:
    """The one correction round of a call whose answer the check rejected in
    part. Updates ``r`` in place (its tokens include the follow-up).

    A failed or unaffordable follow-up leaves the first answer's judgments."""
    by_index = {g.index: g for g in groups}
    rejected = rejected_groups(plan, judge_call_answer(plan, by_index, ctx, r.raw_content))
    if not rejected:
        return
    prompt = build_correction_prompt(plan.prompt, r.raw_content, rejected)
    if estimate_tokens(prompt) > headroom:
        r.attempts.append("correction: skipped_budget")
        return
    r.correction_used = True
    r.correction_prompt_sha256 = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    c = execute_call(prompt, plan.index, runner)
    r.attempts += [f"correction {a}" for a in c.attempts]
    r.waited_seconds += c.waited_seconds
    if c.status == "ok":
        r.correction_raw = c.raw_content
        r.tokens += c.tokens
        r.tokens_estimated = r.tokens_estimated or c.tokens_estimated


def run_judge(
    differences: list[Difference],
    ctx: JudgeContext,
    *,
    runner: ModelRunner | None = None,
    token_budget: int = DEFAULT_TOKEN_BUDGET,
    prompt_cap: int = DEFAULT_PROMPT_CAP,
    templates: tuple[str, ...] | list[str] = (),
) -> JudgeRun:
    """Group, batch, call the pinned client, check evidence.

    The budget is checked before each call (tokens spent so far plus the
    prompt's estimate); once exceeded it stays exceeded and every remaining
    group is ``unclear`` with cause ``ai_budget``.
    """
    groups = group_differences(differences, templates)
    plans, too_big = plan_calls(groups, ctx, prompt_cap)
    results: list[CallResult] = []
    spent = 0
    exhausted = False
    for plan in plans:
        if not exhausted and spent + estimate_tokens(plan.prompt) > token_budget:
            exhausted = True
        if exhausted:
            results.append(CallResult(index=plan.index, status="skipped_budget"))
            continue
        r = execute_call(plan.prompt, plan.index, runner)
        results.append(r)
        if r.status == "ok":
            _correct(r, plan, groups, ctx, runner, token_budget - spent - r.tokens)
        spent += r.tokens
        if spent > token_budget:
            exhausted = True
    judged = judge_from_calls(groups, ctx, plans, too_big, results)
    return JudgeRun(groups, plans, too_big, results, judged)
