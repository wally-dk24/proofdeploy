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
* Hard oracles (code decides, no model): a head 5xx where base was below
  500, a transport error or timeout on head only, a JSON body that no
  longer parses, a Python traceback in head's server log that base's
  logs do not have. Each must reproduce in both head runs, otherwise it is
  recorded as noise (flaky), not as a failure.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from proofdeploy.lean.http import (
    TOKEN_VAR,
    Exchange,
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

    def add(self, scenario: Scenario, exchanges: list[Exchange]) -> None:
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


def find_hard_failures(
    base: RunData,
    base2: RunData,
    head: RunData,
    head2: RunData,
) -> tuple[list[HardFailure], list[HardFailure]]:
    """Hard failures that appear only after the change: ``(stable, flaky)``.

    ``stable`` reproduce in both head runs; ``flaky`` appear in exactly one
    head run. Both need a base that is clean in BOTH base runs: a failure the
    base shows too varies independent of the change, so it is noise (and is
    left to the comparison). A head-only one-off is not noise; it is a
    regression signal that the verdict must not pass over.
    """
    failures: list[HardFailure] = []
    flaky: list[HardFailure] = []
    seen: dict[ExKey, None] = {}
    for run in (head, head2):
        for k in run.order:
            seen.setdefault(k, None)
    for key in seen:
        eb, eb2, eh, eh2 = (r.exchanges.get(key) for r in (base, base2, head, head2))
        rid = f"{key[0]}:{key[1]}"
        sb, sb2, sh, sh2 = (_status(e) for e in (eb, eb2, eh, eh2))
        base_ok = sb < 500 and sb2 < 500
        # 5xx on head where base was below 500 in both runs
        if base_ok and (sh >= 500 or sh2 >= 500):
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
        base_up = not _is_transport_error(eb) and not _is_transport_error(eb2)
        t_h, t_h2 = _is_transport_error(eh), _is_transport_error(eh2)
        if base_up and (t_h or t_h2):
            bad = eh if t_h else eh2
            text = str(bad.body_json_or_text if bad else "")
            kind = (
                "timeout" if "timed out" in text.lower() or "timeout" in text.lower() else "crash"
            )
            if t_h and t_h2:
                failures.append(HardFailure(rid, kind, f"{text[:200]} at {_where(eh)}"))
            else:
                flaky.append(
                    HardFailure(
                        rid, kind, f"in only one of two head runs: {text[:200]} at {_where(bad)}"
                    )
                )
            continue
        # a JSON body that no longer parses
        if (
            _is_json_container(_body(eb))
            and _is_json_container(_body(eb2))
            and eh is not None
            and eh2 is not None
            and sh < 500
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
    new_h = [s for s in traceback_signatures(head.server_log) if s not in base_sigs]
    new_h2 = [s for s in traceback_signatures(head2.server_log) if s not in base_sigs]
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
    "NoiseItem",
    "RunData",
    "compare_runs",
    "derive_captures",
    "display_path",
    "find_hard_failures",
    "flatten",
    "traceback_signatures",
    "unstable_list_paths",
]
