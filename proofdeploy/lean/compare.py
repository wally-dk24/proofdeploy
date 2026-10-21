"""Lean P4, step 4: compare the four runs (base, base', head, head').

Pure functions over recorded exchanges; no I/O, no model.

* Every exchange is matched across runs by ``(scenario id, step index,
  attempt)``: the same request in the same order was sent to all four runs,
  each on a fresh database restored from the same snapshot.
* A value is NOISE if it differs between two runs of the SAME side
  (base != base' or head != head'). It is REAL if base == base',
  head == head' and base != head. Only REAL values become
  :class:`~proofdeploy.lean.judge.Difference` objects.
* Ids captured from an earlier step of the scenario (``capture``) are
  replaced by a position placeholder (``<cap:<step>:<name>>``) for the
  comparison only, so a run that chained a different id is not a
  difference. Reported ``before``/``after`` values stay the raw ones.
* Lists are compared order-insensitively where base vs base' (or head vs
  head') shows the order is not stable, and only then.
* Hard oracles (code decides, no model) flag only what the change made
  worse: a head 5xx, transport error or timeout where the same request was
  healthy (response received, below 500) in BOTH base runs; a body that no
  longer parses as JSON where base answered 2xx JSON and head answers 2xx; a
  Python traceback in head's server log that base's logs do not have and that
  no excluded request explains. A request already failing on base is recorded
  as ``preexisting_failure``, not a failure. Each must reproduce in both head
  runs, otherwise it is flaky (never PASS).
* Values that move with run time (ISO-8601 or epoch timestamps that lie inside
  the wall-clock window of their own run) are noise (``run_time_value``).
"""

from __future__ import annotations

import inspect
import json
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from proofdeploy.lean.http import (
    TOKEN_VAR,
    Exchange,
    HttpClient,
    Scenario,
    capture_value,
)
from proofdeploy.lean.judge import Difference, HardFailure

ExKey = tuple[str, int, int]  # (scenario id, step index, attempt)

MISSING = "<missing>"
RUN_LABELS = ("base", "base2", "head", "head2")


@dataclass
class RunData:
    """What one of the four runs produced."""

    label: str
    exchanges: dict[ExKey, Exchange] = field(default_factory=dict)
    order: list[ExKey] = field(default_factory=list)
    captures: dict[str, list[tuple[int, str, Any]]] = field(default_factory=dict)
    server_log: str = ""
    # Wall-clock window of this run (epoch seconds): created before the app starts,
    # extended each time a scenario's exchanges are added.
    started_at: float = field(default_factory=time.time)
    finished_at: float = 0.0

    def add(self, scenario: Scenario, exchanges: list[Exchange]) -> None:
        self.finished_at = max(self.finished_at, time.time())
        for ex in exchanges:
            key = (ex.scenario_id, ex.step_index, ex.attempt)
            self.exchanges[key] = ex
            self.order.append(key)
        self.captures[scenario.id] = derive_captures(scenario, exchanges)

    def request_count(self) -> int:
        return len(self.order)


def derive_captures(scenario: Scenario, exchanges: list[Exchange]) -> list[tuple[int, str, Any]]:
    """Values captured from a 2xx step, as ``(step index, name, value)``.

    The login token is never listed (it is scrubbed from the recorded
    exchange and differs on every run by design).
    """
    out: list[tuple[int, str, Any]] = []
    last: dict[int, Exchange] = {}
    for ex in exchanges:
        last[ex.step_index] = ex
    for idx, step in enumerate(scenario.steps):
        got = last.get(idx)
        if got is None or not step.capture or not 200 <= got.status < 300:
            continue
        for name, path in step.capture.items():
            if name == TOKEN_VAR:
                continue
            found, value = capture_value(path, got.body_json_or_text, got.headers_subset)
            if found:
                out.append((idx, name, value))
    return out


# --------------------------------------------------------------------------
# Normalization
# --------------------------------------------------------------------------


def _canon(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def placeholder_map(captures: list[tuple[int, str, Any]]) -> dict[tuple[str, str], str]:
    """``(type tag, text) -> placeholder`` for the captured scalar values."""
    out: dict[tuple[str, str], str] = {}
    for idx, name, value in captures:
        if isinstance(value, bool) or value is None:
            continue
        if isinstance(value, int):
            out.setdefault(("i", str(value)), f"<cap:{idx}:{name}>")
        elif isinstance(value, str) and value:
            out.setdefault(("s", value), f"<cap:{idx}:{name}>")
    return out


def cmp_transform(obj: Any, mapping: dict[tuple[str, str], str]) -> Any:
    """Replace whole-value matches of captured ids by their placeholder."""
    if not mapping:
        return obj
    if isinstance(obj, bool) or obj is None:
        return obj
    if isinstance(obj, int):
        return mapping.get(("i", str(obj)), obj)
    if isinstance(obj, str):
        return mapping.get(("s", obj), obj)
    if isinstance(obj, dict):
        return {k: cmp_transform(v, mapping) for k, v in obj.items()}
    if isinstance(obj, list):
        return [cmp_transform(v, mapping) for v in obj]
    return obj


def cmp_url_path(url_path: str, mapping: dict[tuple[str, str], str]) -> str:
    if not mapping:
        return url_path
    bare, sep, query = url_path.partition("?")
    segs = []
    for s in bare.split("/"):
        segs.append(mapping.get(("i", s)) or mapping.get(("s", s)) or s)
    return "/".join(segs) + sep + query


def _unstable_lists(a: Any, b: Any, path: str, out: set[str]) -> None:
    if isinstance(a, dict) and isinstance(b, dict):
        for k in a.keys() & b.keys():
            _unstable_lists(a[k], b[k], f"{path}.{k}" if path else str(k), out)
    elif isinstance(a, list) and isinstance(b, list):
        if a != b and sorted(map(_canon, a)) == sorted(map(_canon, b)):
            out.add(path)
            return
        if len(a) == len(b):
            for x, y in zip(a, b, strict=True):
                _unstable_lists(x, y, f"{path}[]", out)


def unstable_list_paths(a: Any, b: Any) -> set[str]:
    """Paths of lists whose ORDER differs between two runs but whose content does not."""
    out: set[str] = set()
    _unstable_lists(a, b, "", out)
    return out


def _sort_lists(
    obj: Any, unstable: set[str], mapping: dict[tuple[str, str], str], path: str
) -> Any:
    if isinstance(obj, dict):
        return {
            k: _sort_lists(v, unstable, mapping, f"{path}.{k}" if path else str(k))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        items = [_sort_lists(v, unstable, mapping, f"{path}[]") for v in obj]
        if path in unstable:
            items.sort(key=lambda el: _canon(cmp_transform(el, mapping)))
        return items
    return obj


_INDEX_RE = re.compile(r"\[\d+\]")


def flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    """JSON value -> ``{path: leaf}`` with ``a.b[0].c`` paths; empty containers are leaves."""
    out: dict[str, Any] = {}
    if isinstance(value, dict) and value:
        for k, v in value.items():
            out.update(flatten(v, f"{prefix}.{k}" if prefix else str(k)))
    elif isinstance(value, list) and value:
        for i, v in enumerate(value):
            out.update(flatten(v, f"{prefix}[{i}]"))
    else:
        out[prefix or "$body"] = value
    return out


def display_path(path: str) -> str:
    """``results[3].done`` -> ``results[].done`` (the judge's field_path form)."""
    return _INDEX_RE.sub("[]", path)


def _norm_ct(headers: dict[str, str]) -> str | None:
    ct = headers.get("content-type")
    if ct is None:
        return None
    return ct.split(";")[0].strip().lower()


# --------------------------------------------------------------------------
# Run-time values (R10)
# --------------------------------------------------------------------------

RUN_WINDOW_SLACK_S = 5.0  # clock reads before the first and after the last exchange
RUN_TIME_REASON = "run_time_value"
_EPOCH_S = (1e9, 1e11)  # 2001 .. 5138, in seconds
_EPOCH_MS = (1e12, 1e14)  # the same range in milliseconds


def parse_timestamp(value: Any) -> float | None:
    """Epoch seconds of an ISO-8601 string or an epoch number (s or ms), else None.

    A naive ISO time is read as UTC (the sandbox sets ``TZ=UTC``).
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        if _EPOCH_S[0] <= v < _EPOCH_S[1]:
            return v
        if _EPOCH_MS[0] <= v < _EPOCH_MS[1]:
            return v / 1000.0
        return None
    if isinstance(value, str) and len(value) >= 16 and value[:4].isdigit():
        try:
            dt = datetime.fromisoformat(value.strip())
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    return None


def run_window(run: RunData) -> tuple[float, float]:
    """The run's wall-clock window, widened by the slack."""
    end = run.finished_at if run.finished_at >= run.started_at else run.started_at
    return run.started_at - RUN_WINDOW_SLACK_S, end + RUN_WINDOW_SLACK_S


def is_run_time_value(values: list[Any], windows: list[tuple[float, float]]) -> bool:
    """True when every run's value is a timestamp inside that run's own window."""
    for v, (lo, hi) in zip(values, windows, strict=True):
        ts = parse_timestamp(v)
        if ts is None or not lo <= ts <= hi:
            return False
    return True


# --------------------------------------------------------------------------
# Comparison
# --------------------------------------------------------------------------


@dataclass
class NoiseItem:
    request_id: str
    field_path: str
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"request_id": self.request_id, "field_path": self.field_path, "detail": self.detail}


@dataclass
class Comparison:
    differences: list[Difference] = field(default_factory=list)
    noise: list[NoiseItem] = field(default_factory=list)
    compared_exchanges: int = 0
    compared_fields: int = 0
    # label -> [start, end] epoch seconds (with slack) used for ``run_time_value``
    run_windows: dict[str, list[float]] = field(default_factory=dict)

    def noise_fields(self) -> list[str]:
        return sorted({n.field_path for n in self.noise})


def _exchange_or_none(run: RunData, key: ExKey) -> Exchange | None:
    return run.exchanges.get(key)


def _status(ex: Exchange | None) -> int:
    return ex.status if ex is not None else 0


def _body(ex: Exchange | None) -> Any:
    return ex.body_json_or_text if ex is not None else None


def _mapping_for(run: RunData, key: ExKey) -> dict[tuple[str, str], str]:
    """Placeholders valid at this exchange: captures of EARLIER steps of its scenario."""
    caps = [c for c in run.captures.get(key[0], []) if c[0] < key[1]]
    return placeholder_map(caps)


def compare_runs(
    base: RunData,
    base2: RunData,
    head: RunData,
    head2: RunData,
    *,
    keys: Iterable[ExKey] | None = None,
) -> Comparison:
    """Differences and noise between the four runs."""
    result = Comparison()
    windows = [run_window(r) for r in (base, base2, head, head2)]
    result.run_windows = {
        r.label: [round(w[0], 3), round(w[1], 3)]
        for r, w in zip((base, base2, head, head2), windows, strict=True)
    }
    if keys is None:
        seen: dict[ExKey, None] = {}
        for run in (base, head, base2, head2):
            for k in run.order:
                seen.setdefault(k, None)
        keys = list(seen)
    for key in keys:
        runs = (base, base2, head, head2)
        exs = [_exchange_or_none(r, key) for r in runs]
        ref = next((e for e in (exs[2], exs[0], exs[3], exs[1]) if e is not None), None)
        if ref is None:
            continue
        result.compared_exchanges += 1
        request_id = ref.request_id
        method = str(ref.request.get("method", ""))
        # The path as sent on head (or whichever run has it): the judge groups by template.
        url_path = str((exs[2] or ref).request.get("url_path", ""))
        req_body = (exs[2] or ref).request.get("body")
        sb, sb2, sh, sh2 = (_status(e) for e in exs)

        def noisy(vals: list[Any]) -> bool:
            return bool(vals[0] != vals[1] or vals[2] != vals[3])

        # -- status
        if noisy([sb, sb2, sh, sh2]):
            result.noise.append(
                NoiseItem(request_id, "$status", f"base {sb}/{sb2} head {sh}/{sh2}")
            )
            continue
        if sb != sh:
            result.compared_fields += 1
            result.differences.append(
                Difference(
                    request_id, method, url_path, req_body, "$status", sb, sh, "status_changed"
                )
            )
            continue

        # -- bodies and the content-type header, under capture placeholders
        maps = [_mapping_for(r, key) for r in runs]
        cmp_bodies = [cmp_transform(_body(e), m) for e, m in zip(exs, maps, strict=True)]
        unstable = unstable_list_paths(cmp_bodies[0], cmp_bodies[1]) | unstable_list_paths(
            cmp_bodies[2], cmp_bodies[3]
        )
        raw_flats: list[dict[str, Any]] = []
        cmp_flats: list[dict[str, Any]] = []
        for e, m in zip(exs, maps, strict=True):
            sorted_raw = _sort_lists(_body(e), unstable, m, "")
            raw_flats.append(flatten(sorted_raw))
            cmp_flats.append(flatten(cmp_transform(sorted_raw, m)))
        paths = sorted(set().union(*cmp_flats))
        cts = [_norm_ct(e.headers_subset) if e is not None else None for e in exs]
        entries: list[tuple[str, list[Any], list[Any]]] = [
            (p, [f.get(p, MISSING) for f in cmp_flats], [f.get(p, MISSING) for f in raw_flats])
            for p in paths
        ]
        entries.append(
            (
                "$header.content-type",
                [c if c is not None else MISSING for c in cts],
                [c if c is not None else MISSING for c in cts],
            )
        )
        for path, vals, raws in entries:
            result.compared_fields += 1
            shown = path if path.startswith("$") else display_path(path)
            if (
                not path.startswith("$")
                and len({_canon(r) for r in raws}) > 1
                and is_run_time_value(raws, windows)
            ):
                result.noise.append(NoiseItem(request_id, shown, RUN_TIME_REASON))
                continue
            if noisy(vals):
                result.noise.append(NoiseItem(request_id, shown))
                continue
            if vals[0] == vals[2]:
                continue
            before, after = raws[0], raws[2]
            if before == MISSING:
                kind, before = "added_field", None
            elif after == MISSING:
                kind, after = "removed_field", None
            else:
                kind = "changed"
            result.differences.append(
                Difference(request_id, method, url_path, req_body, shown, before, after, kind)
            )
    return result


# --------------------------------------------------------------------------
# Hard oracles
# --------------------------------------------------------------------------

_TRACEBACK_RE = re.compile(
    r"Traceback \(most recent call last\):\n"
    r"(?:[ \t]+.*\n|\^+\n)*?(?P<exc>[A-Za-z_][\w.]*(?:: .*)?)$",
    re.MULTILINE,
)
_HEX_RE = re.compile(r"0x[0-9a-fA-F]+")


def traceback_signatures(log: str) -> list[str]:
    """The final exception line of every Python traceback in a server log."""
    return [_HEX_RE.sub("0x?", m.group("exc").strip()) for m in _TRACEBACK_RE.finditer(log)]


def _is_transport_error(ex: Exchange | None) -> bool:
    if ex is None or ex.status != 0:
        return False
    body = ex.body_json_or_text
    return isinstance(body, str) and body.startswith("transport error:")


def _is_json_container(body: Any) -> bool:
    return isinstance(body, (dict, list))


def _healthy(ex: Exchange | None) -> bool:
    """A response was received and it is below 500."""
    return ex is not None and 0 < ex.status < 500


def _is_2xx(ex: Exchange | None) -> bool:
    return ex is not None and 200 <= ex.status < 300


def _failed(ex: Exchange | None) -> bool:
    """A 5xx or a transport error / timeout."""
    return ex is not None and (ex.status >= 500 or _is_transport_error(ex))


def http_timeout_seconds() -> float:
    """The per-request timeout the replay client uses (recorded with every timeout)."""
    default = inspect.signature(HttpClient.__init__).parameters["timeout"].default
    return float(default)


@dataclass
class HardCheckNotes:
    """What the hard checks set aside (and the timeout used), for the manifest."""

    timeout_seconds: float = field(default_factory=http_timeout_seconds)
    preexisting_failures: list[dict[str, Any]] = field(default_factory=list)
    excluded_tracebacks: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "timeout_seconds": self.timeout_seconds,
            "preexisting_failure": self.preexisting_failures,
            "excluded_tracebacks": self.excluded_tracebacks,
        }


def _traceback_positions(log: str) -> list[tuple[int, str]]:
    return [
        (m.start(), _HEX_RE.sub("0x?", m.group("exc").strip())) for m in _TRACEBACK_RE.finditer(log)
    ]


def request_windows(run: RunData) -> dict[ExKey, tuple[int, int]]:
    """Where each request sits in the server log: ``key -> (start, next request's start)``.

    A request's start is the next occurrence, in order, of ``METHOD /path`` in the
    log. A request that cannot be found has no window (nothing is explained by it).
    """
    log = run.server_log
    found: list[tuple[ExKey, int]] = []
    pos = 0
    for key in run.order:
        ex = run.exchanges.get(key)
        if ex is None:
            continue
        method = str(ex.request.get("method", "")).upper()
        path = str(ex.request.get("url_path", ""))
        at = -1
        for needle in dict.fromkeys([f"{method} {path}", f"{method} {path.partition('?')[0]}"]):
            at = log.find(needle, pos)
            if at >= 0:
                break
        if at < 0:
            continue
        found.append((key, at))
        pos = at + 1
    out: dict[ExKey, tuple[int, int]] = {}
    for n, (key, at) in enumerate(found):
        end = found[n + 1][1] if n + 1 < len(found) else len(log) + 1
        out[key] = (at, end)
    return out


def find_hard_failures(
    base: RunData,
    base2: RunData,
    head: RunData,
    head2: RunData,
    *,
    notes: HardCheckNotes | None = None,
) -> tuple[list[HardFailure], list[HardFailure]]:
    """Hard failures that appear only after the change: ``(stable, flaky)``.

    ``stable`` reproduce in both head runs; ``flaky`` appear in exactly one
    head run. A failure counts only if the change made the request worse:

    * 5xx / transport error / timeout: the request must be healthy (response
      received, status below 500) in BOTH base runs; otherwise it is recorded in
      ``notes.preexisting_failures`` and is not a failure.
    * body not parseable as JSON: head answers 2xx where both base runs answered
      2xx with a JSON body (a 4xx with a text body is data).
    * new traceback in head's server log: not one that sits in the log window of
      a request excluded above (recorded in ``notes.excluded_tracebacks``).

    A head-only one-off is not noise; it is a regression signal that the verdict
    must not pass over.
    """
    if notes is None:
        notes = HardCheckNotes()
    failures: list[HardFailure] = []
    flaky: list[HardFailure] = []
    seen: dict[ExKey, None] = {}
    for run in (head, head2):
        for k in run.order:
            seen.setdefault(k, None)
    excluded: dict[str, set[ExKey]] = {head.label: set(), head2.label: set()}
    for key in seen:
        eb, eb2, eh, eh2 = (r.exchanges.get(key) for r in (base, base2, head, head2))
        rid = f"{key[0]}:{key[1]}"
        sb, sb2, sh, sh2 = (_status(e) for e in (eb, eb2, eh, eh2))
        base_healthy = _healthy(eb) and _healthy(eb2)
        if not base_healthy and (_failed(eh) or _failed(eh2)):
            notes.preexisting_failures.append(
                {
                    "request_id": rid,
                    "kind": "preexisting_failure",
                    "detail": f"base {sb}/{sb2}, head {sh}/{sh2} at {_where(eh or eh2)}",
                }
            )
            for run, e in ((head, eh), (head2, eh2)):
                if _failed(e):
                    excluded[run.label].add(key)
            continue
        # 5xx on head where base was healthy in both runs
        if base_healthy and (sh >= 500 or sh2 >= 500):
            if sh >= 500 and sh2 >= 500:
                failures.append(
                    HardFailure(rid, "5xx", f"head answered HTTP {sh} (base {sb}) at {_where(eh)}")
                )
            else:
                flaky.append(
                    HardFailure(
                        rid,
                        "5xx",
                        f"head answered HTTP {sh}/{sh2} in only one of two runs "
                        f"(base {sb}/{sb2}) at {_where(eh if sh >= 500 else eh2)}",
                    )
                )
            continue
        # connection error / timeout on head, none on base
        t_h, t_h2 = _is_transport_error(eh), _is_transport_error(eh2)
        if base_healthy and (t_h or t_h2):
            bad = eh if t_h else eh2
            text = str(bad.body_json_or_text if bad else "")
            kind = (
                "timeout" if "timed out" in text.lower() or "timeout" in text.lower() else "crash"
            )
            limit = f"(client timeout {notes.timeout_seconds:g}s) " if kind == "timeout" else ""
            if t_h and t_h2:
                failures.append(HardFailure(rid, kind, f"{limit}{text[:200]} at {_where(eh)}"))
            else:
                flaky.append(
                    HardFailure(
                        rid,
                        kind,
                        f"{limit}in only one of two head runs: {text[:200]} at {_where(bad)}",
                    )
                )
            continue
        # a JSON body that no longer parses: head 2xx where base answered 2xx JSON
        if (
            _is_2xx(eb)
            and _is_2xx(eb2)
            and _is_json_container(_body(eb))
            and _is_json_container(_body(eb2))
            and _is_2xx(eh)
            and _is_2xx(eh2)
            and eh is not None
            and eh2 is not None
            and isinstance(eh.body_json_or_text, str)
            and isinstance(eh2.body_json_or_text, str)
        ):
            failures.append(
                HardFailure(
                    rid,
                    "schema_violation",
                    f"response body is not parseable JSON on head (base's was) at {_where(eh)}: "
                    f"{eh.body_json_or_text[:120]!r}",
                )
            )
    base_sigs = set(traceback_signatures(base.server_log)) | set(
        traceback_signatures(base2.server_log)
    )

    def new_sigs(run: RunData) -> list[str]:
        located = request_windows(run) if excluded[run.label] else {}
        windows = {k: w for k, w in located.items() if k in excluded[run.label]}
        out: list[str] = []
        for pos, sig in _traceback_positions(run.server_log):
            if sig in base_sigs:
                continue
            owner = next((k for k, (lo, hi) in windows.items() if lo <= pos < hi), None)
            if owner is not None:
                notes.excluded_tracebacks.append(
                    {
                        "run": run.label,
                        "request_id": f"{owner[0]}:{owner[1]}",
                        "signature": sig,
                        "reason": "inside the log window of a request already excluded as "
                        "preexisting_failure",
                    }
                )
                continue
            out.append(sig)
        return out

    new_h = new_sigs(head)
    new_h2 = new_sigs(head2)
    for sig in dict.fromkeys([*new_h, *new_h2]):
        if sig in new_h and sig in new_h2:
            failures.append(
                HardFailure(
                    "server_log", "new_exception", f"new traceback in head's server log: {sig}"
                )
            )
        else:
            flaky.append(
                HardFailure(
                    "server_log",
                    "new_exception",
                    f"new traceback in only one of two head runs: {sig}",
                )
            )
    return failures, flaky


def _where(ex: Exchange | None) -> str:
    if ex is None:
        return "?"
    return f"{ex.request.get('method')} {ex.request.get('url_path')}"


__all__ = [
    "MISSING",
    "RUN_LABELS",
    "Comparison",
    "ExKey",
    "HardCheckNotes",
    "NoiseItem",
    "RunData",
    "compare_runs",
    "derive_captures",
    "display_path",
    "find_hard_failures",
    "flatten",
    "parse_timestamp",
    "request_windows",
    "traceback_signatures",
    "unstable_list_paths",
]
