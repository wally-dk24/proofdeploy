"""Lean step 3, GENERATE: request scenarios that reach the changed lines.

No model is called. From an :class:`~proofdeploy.lean.index.AppIndex` (the
app's operations plus seed facts) and the change (``ChangedLines`` plus the
diff text) this module

1. builds one or more request SCENARIOS per operation
   (login -> producers -> target), see :class:`ScenarioGenerator`:
   * path params come from a producer step (POST on the parent collection,
     capture ``id``/``pk``/the param name from its response) or from ids named
     in the seed facts;
   * request bodies are synthesized from the request schema (required fields
     with type-correct values, booleans both ways, enums first value, valid
     formats, optional fields in one extra variant);
   * integers/strings found in the ADDED and REMOVED diff lines add variants
     (``n-1``, ``n``, ``n+1`` for integers);
   * auth: a login step from the contract's ``auth`` block comes first;
2. executes them (:class:`ScenarioRunner`), repairing a 400/422 once from the
   error body (DRF, ``detail``/``error`` strings, ``loc`` lists);
3. loops until the changed lines are covered or the request budget ends
   (:func:`cover_change`), reading coverage after every scenario.

The only model hook is ``value_hint``: an optional callable that is given an
operation and the uncovered diff text and may return extra body fields. It is
an interface only; nothing here implements it with a model.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from proofdeploy.lean import coverage as lc
from proofdeploy.lean.coverage import ChangedLines, Coverage, split_diff_lines
from proofdeploy.lean.http import (
    TOKEN_VAR,
    AuthSpec,
    Exchange,
    HttpClient,
    Scenario,
    Step,
    UnresolvedReference,
    capture_value,
    encode_body,
    expand_env,
    fill_path,
    parse_body,
    resolve_refs,
    scrub_value,
    unresolved_placeholders,
)
from proofdeploy.lean.index import AppIndex, Operation

WRITE_METHODS = ("POST", "PUT", "PATCH")
MISSING_ID = 999999
MAX_PRODUCER_DEPTH = 3


def op_key(op: Operation) -> str:
    return f"{op.method} {op.path}"


# --------------------------------------------------------------------------
# Diff text: line texts and hunk contexts
# --------------------------------------------------------------------------

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@ ?(.*)$")


@dataclass
class DiffText:
    """Texts of the changed lines and the hunk-header contexts of a diff."""

    # (path, "added"|"removed", line number, text without the +/- marker)
    lines: list[tuple[str, str, int, str]] = field(default_factory=list)
    contexts: list[str] = field(default_factory=list)

    def text_of(self, path: str, side: str, lineno: int) -> str | None:
        for p, s, n, t in self.lines:
            if p == path and s == side and n == lineno:
                return t
        return None

    def all_text(self) -> str:
        return "\n".join(t for _, _, _, t in self.lines)


def parse_diff_text(diff_text: str) -> DiffText:
    """Lenient reader of a unified diff: line texts, hunk contexts.

    Line numbering follows the hunk headers exactly like
    ``ChangedLines.from_diff`` (which is the strict parser and runs first in
    real use); this one never raises.
    """
    out = DiffText()
    old_path: str | None = None
    new_path: str | None = None
    old_ln = new_ln = old_rem = new_rem = 0
    for line in split_diff_lines(diff_text):
        if old_rem > 0 or new_rem > 0:
            tag = line[:1]
            path = new_path if new_path is not None else old_path
            if line.startswith("\\"):
                continue
            if tag == "-" and old_rem > 0 and path is not None:
                out.lines.append((path, "removed", old_ln, line[1:]))
                old_ln += 1
                old_rem -= 1
                continue
            if tag == "+" and new_rem > 0 and path is not None:
                out.lines.append((path, "added", new_ln, line[1:]))
                new_ln += 1
                new_rem -= 1
                continue
            if old_rem > 0 and new_rem > 0:
                old_ln += 1
                new_ln += 1
                old_rem -= 1
                new_rem -= 1
                continue
            old_rem = new_rem = 0
        if line.startswith("diff --git "):
            old_path = new_path = None
            continue
        if line.startswith("--- "):
            raw = line[4:].split("\t", 1)[0]
            old_path = None if raw == "/dev/null" else re.sub(r"^[ab]/", "", raw)
            continue
        if line.startswith("+++ "):
            raw = line[4:].split("\t", 1)[0]
            new_path = None if raw == "/dev/null" else re.sub(r"^[ab]/", "", raw)
            continue
        m = _HUNK_RE.match(line)
        if m:
            old_ln = int(m.group(1))
            old_rem = int(m.group(2)) if m.group(2) is not None else 1
            new_ln = int(m.group(3))
            new_rem = int(m.group(4)) if m.group(4) is not None else 1
            ctx = m.group(5).strip()
            if ctx:
                out.contexts.append(ctx)
    return out


_BRACKETS_ONLY = re.compile(r"^[\s()\[\]{},;]*$")
_NO_EXEC = {"else:", "try:", "finally:"}


def is_non_executable(text: str) -> bool:
    """Blank, comment-only, bracket-only or bare ``else:``/``try:`` lines never run."""
    s = text.strip()
    return s == "" or s.startswith("#") or bool(_BRACKETS_ONLY.match(s)) or s in _NO_EXEC


def executable_view(changed: ChangedLines, dt: DiffText, side: str) -> ChangedLines:
    """The part of ``changed`` one side's run can be asked to cover.

    ``side="new"`` keeps ADDED lines (judged on the new-side run), ``"old"``
    keeps REMOVED lines. Lines whose diff text is non-executable are dropped.
    """
    if side not in ("new", "old"):
        raise ValueError("side must be 'new' or 'old'")
    key = "added" if side == "new" else "removed"
    texts = {(p, s, n): t for p, s, n, t in dt.lines}
    view = ChangedLines()
    view.renames = dict(changed.renames)
    view.binary_files = set(changed.binary_files)
    for path, sides in changed.items():
        keep = {n for n in sides[key] if not is_non_executable(texts.get((path, key, n), "x"))}
        view[path] = {
            "added": keep if key == "added" else set(),
            "removed": keep if key == "removed" else set(),
        }
    return view


# --------------------------------------------------------------------------
# Constant mining
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class MinedConstant:
    kind: str  # "int" | "str"
    value: int | str
    names: tuple[str, ...]  # lowercase word tokens on the same diff line


_STR_RE = re.compile(r"\"([^\"\\\n]{1,40})\"|'([^'\\\n]{1,40})'")
_INT_RE = re.compile(r"(?<![\w.\)\]])(-?)(\d+)(?![\w.])")
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*")
MAX_MINED_INTS = 4
MAX_MINED_STRS = 4


def _word_tokens(text: str) -> set[str]:
    out: set[str] = set()
    for w in re.findall(r"[A-Za-z][A-Za-z0-9_]*", text):
        for part in w.split("_"):
            for piece in re.findall(r"[A-Z]?[a-z0-9]+|[A-Z]+(?![a-z])", part):
                out.add(_stem(piece.lower()))
    return out


def _stem(tok: str) -> str:
    return tok[:-1] if len(tok) > 3 and tok.endswith("s") and not tok.endswith("ss") else tok


def mine_constants(diff_text: str) -> list[MinedConstant]:
    """Integer and string literals from the ADDED then REMOVED diff lines.

    Order of first appearance, de-duplicated by (kind, value); at most
    ``MAX_MINED_INTS`` integers and ``MAX_MINED_STRS`` strings. ``names`` are
    the word tokens of the line the literal came from (used to prefer fields
    of the same name).
    """
    dt = parse_diff_text(diff_text)
    ordered = [t for _, s, _, t in dt.lines if s == "added"] + [
        t for _, s, _, t in dt.lines if s == "removed"
    ]
    out: list[MinedConstant] = []
    seen: set[tuple[str, int | str]] = set()
    n_int = n_str = 0
    for text in ordered:
        if is_non_executable(text):
            continue
        names = tuple(sorted(_word_tokens(text)))
        for m in _STR_RE.finditer(text):
            val = m.group(1) if m.group(1) is not None else m.group(2)
            if "%" in val or "{" in val or ("str", val) in seen or n_str >= MAX_MINED_STRS:
                continue
            seen.add(("str", val))
            n_str += 1
            out.append(MinedConstant("str", val, names))
        blanked = _STR_RE.sub('""', text)
        for m in _INT_RE.finditer(blanked):
            ival = int(m.group(2)) * (-1 if m.group(1) else 1)
            if abs(ival) > 10**12 or ("int", ival) in seen or n_int >= MAX_MINED_INTS:
                continue
            seen.add(("int", ival))
            n_int += 1
            out.append(MinedConstant("int", ival, names))
    return out


# --------------------------------------------------------------------------
# Body synthesis from schemas
# --------------------------------------------------------------------------

_FORMAT_VALUES = {
    "date-time": "2024-01-01T00:00:00Z",
    "date": "2024-01-01",
    "time": "00:00:00",
    "email": "pd@example.com",
    "idn-email": "pd@example.com",
    "uri": "https://example.com/pd",
    "url": "https://example.com/pd",
    "uri-reference": "https://example.com/pd",
    "uuid": "00000000-0000-4000-8000-000000000001",
    "hostname": "example.com",
    "ipv4": "127.0.0.1",
    "ipv6": "::1",
    "binary": "pd-file",
    "byte": "cGQ=",
    "password": "pd-password",
}


def _schema_type(schema: dict[str, Any]) -> str | None:
    t = schema.get("type")
    if isinstance(t, list):
        non_null = [x for x in t if x != "null"]
        return str(non_null[0]) if non_null else "null"
    if isinstance(t, str):
        return t
    if "properties" in schema:
        return "object"
    if "items" in schema:
        return "array"
    return None


def value_for(schema: dict[str, Any] | None, name: str = "value", *, _depth: int = 0) -> Any:
    """A type-correct value for a (resolved) JSON schema."""
    if not isinstance(schema, dict) or _depth > 6:
        return f"pd-{name}"
    if "x-cycle" in schema:
        return None
    if "const" in schema:
        return schema["const"]
    if isinstance(schema.get("enum"), list) and schema["enum"]:
        return schema["enum"][0]
    for key in ("oneOf", "anyOf"):
        members = [m for m in schema.get(key) or [] if isinstance(m, dict)]
        non_null = [m for m in members if m.get("type") != "null"]
        if non_null:
            return value_for(non_null[0], name, _depth=_depth + 1)
    t = _schema_type(schema)
    if t == "string":
        fmt = str(schema.get("format") or "")
        if fmt in _FORMAT_VALUES:
            return _FORMAT_VALUES[fmt]
        s = f"pd-{name}"
        min_len = schema.get("minLength")
        if isinstance(min_len, int) and len(s) < min_len:
            s = s + "x" * (min_len - len(s))
        max_len = schema.get("maxLength")
        if isinstance(max_len, int) and max_len > 0 and len(s) > max_len:
            s = s[:max_len]
        return s
    if t in ("integer", "number"):
        v = 1
        lo = schema.get("minimum")
        if isinstance(lo, (int, float)) and not isinstance(lo, bool) and v < lo:
            v = int(lo) if float(lo).is_integer() else int(lo) + 1
        if schema.get("exclusiveMinimum") is True and isinstance(lo, (int, float)):
            v = int(lo) + 1
        elif isinstance(schema.get("exclusiveMinimum"), (int, float)) and not isinstance(
            schema.get("exclusiveMinimum"), bool
        ):
            v = max(v, int(schema["exclusiveMinimum"]) + 1)
        hi = schema.get("maximum")
        if isinstance(hi, (int, float)) and not isinstance(hi, bool) and v > hi:
            v = int(hi)
        return v
    if t == "boolean":
        return True
    if t == "array":
        return [value_for(schema.get("items"), name, _depth=_depth + 1)]
    if t == "object":
        props = schema.get("properties") or {}
        req = schema.get("required") or []
        return {
            k: value_for(props.get(k), k, _depth=_depth + 1)
            for k in sorted(req)
            if not (isinstance(props.get(k), dict) and props[k].get("readOnly"))
        }
    if t == "null":
        return None
    return f"pd-{name}"


def _writable_props(schema: dict[str, Any]) -> dict[str, dict[str, Any]]:
    props = schema.get("properties") or {}
    return {
        k: v
        for k, v in sorted(props.items())
        if isinstance(v, dict) and not v.get("readOnly") and "x-cycle" not in v
    }


def _required_names(schema: dict[str, Any]) -> list[str]:
    return sorted(str(r) for r in schema.get("required") or [])


def _field_matches(field_name: str, const: MinedConstant) -> bool:
    return bool(_word_tokens(field_name) & set(const.names) - {"id"})


def _mined_field_values(
    fields: dict[str, dict[str, Any]], mined: Sequence[MinedConstant]
) -> list[tuple[str, Any]]:
    """(field, value) pairs for the mined constants, boundaries for integers."""
    out: list[tuple[str, Any]] = []
    for c in mined:
        want = "int" if c.kind == "int" else "str"
        typed = []
        for k, sch in fields.items():
            t = _schema_type(sch)
            if want == "int" and t in ("integer", "number"):
                typed.append(k)
            elif want == "str" and t == "string":
                enum = sch.get("enum")
                if enum is None or c.value in enum:
                    typed.append(k)
        named = [k for k in typed if _field_matches(k, c)]
        for k in named or typed:
            if c.kind == "int":
                n = int(c.value)
                out.extend((k, v) for v in (n - 1, n, n + 1))
            else:
                out.append((k, c.value))
    seen: set[str] = set()
    uniq: list[tuple[str, Any]] = []
    for k, v in out:
        key = json.dumps([k, v])
        if key not in seen:
            seen.add(key)
            uniq.append((k, v))
    return uniq


def body_variants(
    schema: dict[str, Any] | None, mined: Sequence[MinedConstant] = ()
) -> list[tuple[str, Any]]:
    """Request bodies for a schema as ``(label, body)``; the first is ``base``.

    ``base``: required fields only. Then one variant per mined boundary value,
    ``full`` (every writable field), and a ``bool:<field>=false`` variant per
    boolean field.
    """
    if schema is None:
        return [("base", None)]
    if _schema_type(schema) != "object":
        return [("base", value_for(schema, "value"))]
    props = _writable_props(schema)
    required = _required_names(schema)
    base: dict[str, Any] = {
        k: (value_for(props[k], k) if k in props else f"pd-{k}")
        for k in required
        if k in props or k not in (schema.get("properties") or {})
    }
    out: list[tuple[str, Any]] = [("base", base)]
    for k, v in _mined_field_values(props, mined):
        out.append((f"mined:{k}={v}", {**base, k: v}))
    full = dict(base)
    for k, sch in props.items():
        if k not in full:
            full[k] = value_for(sch, k)
    if full != base:
        out.append(("full", full))
    for k, sch in props.items():
        if _schema_type(sch) == "boolean" and k in full:
            out.append((f"bool:{k}=false", {**full, k: False}))
    seen: set[str] = set()
    uniq: list[tuple[str, Any]] = []
    for label, body in out:
        key = json.dumps(body, sort_keys=True)
        if key not in seen:
            seen.add(key)
            uniq.append((label, body))
    return uniq


def _query_params(op: Operation) -> dict[str, dict[str, Any]]:
    return {
        str(p["name"]): (p.get("schema") or {})
        for p in sorted(op.params, key=lambda p: str(p.get("name")))
        if p.get("in") == "query"
    }


def request_variants(
    op: Operation, mined: Sequence[MinedConstant] = ()
) -> list[tuple[str, Any, dict[str, Any]]]:
    """``(label, body, query)`` variants for one operation."""
    q_all = _query_params(op)
    q_req_names = {
        str(p["name"]) for p in op.params if p.get("in") == "query" and p.get("required")
    }
    q_req = {k: value_for(v, k) for k, v in q_all.items() if k in q_req_names}
    q_full = {k: value_for(v, k) for k, v in q_all.items()}
    if op.request_schema is None and op.method in WRITE_METHODS:
        bodies: list[tuple[str, Any]] = [("base", {})]
    else:
        bodies = body_variants(op.request_schema, mined)
    out: list[tuple[str, Any, dict[str, Any]]] = []
    for label, body in bodies:
        wide = label == "full" or label.startswith("bool:")
        out.append((label, body, q_full if wide else q_req))
    base_body = bodies[0][1]
    for k, v in _mined_field_values(q_all, mined):
        out.append((f"mined-query:{k}={v}", base_body, {**q_req, k: v}))
    if q_all and q_full != q_req:
        out.append(("query-full", base_body, q_full))
    seen: set[str] = set()
    uniq: list[tuple[str, Any, dict[str, Any]]] = []
    for label, body, query in out:
        key = json.dumps([body, query], sort_keys=True, default=str)
        if key not in seen:
            seen.add(key)
            uniq.append((label, body, query))
    return uniq


# --------------------------------------------------------------------------
# Seed facts
# --------------------------------------------------------------------------

_SEED_IDS_RE = re.compile(r"\bids?\s*[:=#]?\s*(\d+(?:\s*(?:,|and|&)\s*\d+)*)", re.IGNORECASE)


def seed_ids(seed_facts: Iterable[str]) -> list[int]:
    """Integer ids named in seed sentences (``id 1``, ``ids 1 and 2``), in order."""
    out: list[int] = []
    for fact in seed_facts:
        for m in _SEED_IDS_RE.finditer(fact):
            for n in re.findall(r"\d+", m.group(1)):
                if int(n) not in out:
                    out.append(int(n))
    return out


# --------------------------------------------------------------------------
# Ranking
# --------------------------------------------------------------------------

_RANK_STOP = {
    "get", "post", "put", "patch", "delete", "list", "create", "retrieve", "update", "id",
    "pk", "def", "return", "self", "if", "else", "elif", "none", "true", "false", "not",
    "and", "or", "in", "is", "for", "app", "api", "request", "data", "json", "jsonify",
    "error", "import", "from", "class", "the", "db", "g", "str", "int", "len", "with",
    "as", "try", "except", "raise", "pass", "lambda", "await", "async", "const", "let",
    "var", "function", "new", "this", "res", "req", "status", "length",
}  # fmt: skip


def _op_tokens(op: Operation) -> list[str]:
    toks: list[str] = []
    for seg in op.path.split("/"):
        toks.extend(_word_tokens(seg.strip("{}")))
    for p in op.params:
        toks.extend(_word_tokens(str(p.get("name", ""))))
    toks.extend(_word_tokens(_endpoint_name(op)))
    for schema in (op.request_schema, op.response_schema):
        if isinstance(schema, dict):
            toks.extend(_schema_names(schema))
    return [t for t in toks if t not in _RANK_STOP]


def _endpoint_name(op: Operation) -> str:
    return op.source.split(":", 1)[1] if ":" in op.source else ""


def _schema_names(schema: dict[str, Any], depth: int = 0) -> list[str]:
    out: list[str] = []
    if depth > 3:
        return out
    for k, v in (schema.get("properties") or {}).items():
        out.extend(_word_tokens(str(k)))
        if isinstance(v, dict):
            out.extend(_schema_names(v, depth + 1))
    if isinstance(schema.get("items"), dict):
        out.extend(_schema_names(schema["items"], depth + 1))
    return out


def rank_operations(ops: Sequence[Operation], diff_text: str = "") -> list[Operation]:
    """Operations that mention identifiers from the changed lines first.

    The score counts how many of an operation's tokens (path segments, param
    names, handler/operationId name, schema property names; stop-words
    removed) occur among the identifiers of the changed lines and of the
    hunk-header contexts git prints (the enclosing function), plus a bonus
    when the handler name equals an enclosing function. Ties keep
    (path, method) order, so the ranking is deterministic.
    """
    dt = parse_diff_text(diff_text)
    diff_tokens = {t for t in _word_tokens(dt.all_text() + "\n" + "\n".join(dt.contexts))}
    diff_tokens -= _RANK_STOP
    ctx_names = {m for c in dt.contexts for m in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", c)}

    def score(op: Operation) -> int:
        s = sum(1 for t in _op_tokens(op) if t in diff_tokens)
        ep = _endpoint_name(op)
        if ep and ep.split(".")[-1] in ctx_names:
            s += 5
        return s

    return sorted(ops, key=lambda o: (-score(o), o.path, o.method))


# --------------------------------------------------------------------------
# Scenario generation
# --------------------------------------------------------------------------


def _norm(path: str) -> str:
    return "/" + path.strip("/")


def _param_schemas(op: Operation) -> dict[str, dict[str, Any]]:
    return {
        str(p["name"]): (p.get("schema") or {})
        for p in sorted(op.params, key=lambda p: str(p.get("name")))
        if p.get("in") == "path"
    }


def _default_param(name: str, schema: dict[str, Any], int_value: int | None) -> Any:
    t = _schema_type(schema)
    if t in ("integer", "number"):
        return int_value if int_value is not None else 1
    if schema.get("format") == "uuid":
        return value_for(schema, name)
    return f"pd-{name}"


class ScenarioGenerator:
    """Builds scenarios for operations of one index. See the module docstring."""

    def __init__(
        self,
        index: AppIndex,
        *,
        diff_text: str = "",
        auth: AuthSpec | None = None,
        max_variants_per_op: int = 24,
    ) -> None:
        self.index = index
        self.auth = auth
        self.max_variants = max_variants_per_op
        self.mined = mine_constants(diff_text) if diff_text else []
        self.seed_ids = seed_ids(index.seed_facts)
        self._by_path: dict[str, list[Operation]] = {}
        for op in index.operations:
            self._by_path.setdefault(_norm(op.path), []).append(op)

    # -- producers ----------------------------------------------------------

    def _producer_for(self, op: Operation, param: str) -> Operation | None:
        segs = op.path.strip("/").split("/")
        marker = "{" + param + "}"
        if marker not in segs:
            return None
        coll = _norm("/".join(segs[: segs.index(marker)]))
        for cand in self._by_path.get(coll, []):
            if cand.method == "POST" and cand is not op:
                return cand
        return None

    @staticmethod
    def _capture_path(producer: Operation, param: str) -> str:
        resp = producer.response_schema or {}
        props = resp.get("properties") if isinstance(resp, dict) else None
        if isinstance(props, dict):
            for cand in (param, "id", "pk", "uuid", "slug"):
                if cand in props:
                    return cand
        return "id"

    def _chain(self, op: Operation, depth: int = 0) -> tuple[list[Step], dict[str, Any]] | None:
        """Producer steps (dependencies first) and path param refs for ``op``.

        None when ``op`` has no path params or no param has a producer.
        """
        if depth >= MAX_PRODUCER_DEPTH:
            return None
        params = _param_schemas(op)
        if not params:
            return None
        steps: list[Step] = []
        values: dict[str, Any] = {}
        used = False
        fallback = self.seed_ids[0] if self.seed_ids else None
        for name, schema in params.items():
            producer = self._producer_for(op, name)
            if producer is None:
                values[name] = _default_param(name, schema, fallback)
                continue
            sub = self._chain(producer, depth + 1)
            sub_steps, sub_values = sub if sub else ([], {})
            steps.extend(s for s in sub_steps if s not in steps)
            steps.append(
                self._producer_step(producer, name, sub_values, authed=self.auth is not None)
            )
            values[name] = "${" + name + "}"
            used = True
        return (steps, values) if used else None

    def _producer_step(
        self, producer: Operation, param: str, path_params: dict[str, Any], *, authed: bool
    ) -> Step:
        variants = request_variants(producer)
        _, body, query = variants[0]
        return Step(
            method="POST",
            path=producer.path,
            path_params=dict(path_params),
            query=query,
            body=body,
            content_type=(producer.request_content_type or "application/json")
            if body is not None
            else None,
            auth=authed,
            capture={param: self._capture_path(producer, param)},
        )

    # -- scenarios ----------------------------------------------------------

    def _target(
        self,
        op: Operation,
        body: Any,
        query: dict[str, Any],
        path_params: dict[str, Any],
        *,
        authed: bool,
    ) -> Step:
        return Step(
            method=op.method,
            path=op.path,
            path_params=dict(path_params),
            query=dict(query),
            body=body,
            content_type=(op.request_content_type or "application/json")
            if body is not None
            else None,
            auth=authed,
        )

    def _make(self, op: Operation, pre: list[Step], target: Step, *, login: bool) -> Scenario:
        steps: list[Step] = []
        if login and self.auth is not None:
            steps.append(self.auth.login)
        steps.extend(pre)
        steps.append(target)
        return Scenario(steps=steps, target_operation=op_key(op))

    def _is_login_op(self, op: Operation) -> bool:
        a = self.auth
        return (
            a is not None and op.method == a.login.method and _norm(op.path) == _norm(a.login.path)
        )

    def for_operation(
        self, op: Operation, *, body_override: dict[str, Any] | None = None
    ) -> list[Scenario]:
        """Scenarios for one operation, most promising first."""
        if self._is_login_op(op) and self.auth is not None:
            out = [Scenario(steps=[self.auth.login], target_operation=op_key(op))]
            body = self.auth.login.body
            if isinstance(body, dict):
                bad = Step(
                    **{
                        **self.auth.login.to_dict(),
                        "body": {k: "pd-wrong" for k in body},
                        "capture": {},
                    }
                )
                out.append(Scenario(steps=[bad], target_operation=op_key(op)))
            return out
        authed = self.auth is not None
        params = _param_schemas(op)
        variants = request_variants(op, self.mined)
        if body_override is not None:
            base_body = variants[0][1]
            merged = (
                {**base_body, **body_override} if isinstance(base_body, dict) else body_override
            )
            variants = [("hint", merged, variants[0][2])]
        chain = self._chain(op)
        pre, ids = chain if chain else ([], None)
        if ids is None:
            fb = self.seed_ids[0] if self.seed_ids else None
            ids = {n: _default_param(n, s, fb) for n, s in params.items()}

        chained: list[tuple[str, Scenario]] = []
        for label, body, query in variants:
            chained.append(
                (
                    label,
                    self._make(
                        op, pre, self._target(op, body, query, ids, authed=authed), login=True
                    ),
                )
            )
        first_label, first_body, first_query = variants[0]
        mined_sc = [s for lbl, s in chained[1:] if lbl.startswith("mined")]
        other_sc = [s for lbl, s in chained[1:] if not lbl.startswith("mined")]
        ordered: list[Scenario] = [chained[0][1], *mined_sc]
        if params:
            for sid in self.seed_ids[:2]:
                seed_ids_map = {
                    n: _default_param(n, s, sid)
                    if _schema_type(s) in ("integer", "number")
                    else _default_param(n, s, None)
                    for n, s in params.items()
                }
                ordered.append(
                    self._make(
                        op,
                        [],
                        self._target(op, first_body, first_query, seed_ids_map, authed=authed),
                        login=True,
                    )
                )
        ordered.extend(other_sc)
        if self.auth is not None:
            fb = self.seed_ids[0] if self.seed_ids else None
            plain = {n: _default_param(n, s, fb) for n, s in params.items()}
            ordered.append(
                self._make(
                    op,
                    [],
                    self._target(op, first_body, first_query, plain, authed=False),
                    login=False,
                )
            )
        if params:
            missing = {n: _default_param(n, s, MISSING_ID) for n, s in params.items()}
            ordered.append(
                self._make(
                    op,
                    [],
                    self._target(op, first_body, first_query, missing, authed=authed),
                    login=True,
                )
            )
        seen: set[str] = set()
        uniq: list[Scenario] = []
        for sc in ordered:
            if sc.id not in seen:
                seen.add(sc.id)
                uniq.append(sc)
        return uniq[: self.max_variants]

    def generate(self, ops: Sequence[Operation] | None = None) -> list[Scenario]:
        """All scenarios, ordered: the first scenario of each ranked op, then the rest."""
        per_op = [
            self.for_operation(op) for op in (ops if ops is not None else self.index.operations)
        ]
        first = [lst[0] for lst in per_op if lst]
        rest = [s for lst in per_op for s in lst[1:]]
        seen: set[str] = set()
        out: list[Scenario] = []
        for sc in [*first, *rest]:
            if sc.id not in seen:
                seen.add(sc.id)
                out.append(sc)
        return out


def generate_scenarios(
    index: AppIndex,
    *,
    diff_text: str = "",
    auth: AuthSpec | None = None,
    max_variants_per_op: int = 24,
) -> list[Scenario]:
    """Scenarios for every operation, ranked by the changed identifiers."""
    gen = ScenarioGenerator(
        index, diff_text=diff_text, auth=auth, max_variants_per_op=max_variants_per_op
    )
    return gen.generate(rank_operations(index.operations, diff_text))


# --------------------------------------------------------------------------
# 400 repair
# --------------------------------------------------------------------------

_REPAIR_STATUSES = (400, 422)
_MSG_KEYS = ("detail", "error", "message", "msg")
_NOT_FIELDS = {"detail", "error", "errors", "message", "msg", "non_field_errors", "status", "code"}
_MSG_STOP = {
    "a", "an", "and", "are", "be", "bad", "body", "but", "can", "cannot", "data", "empty",
    "error", "field", "fields", "for", "has", "have", "in", "invalid", "is", "it", "json",
    "missing", "must", "need", "needed", "no", "not", "of", "on", "or", "payload", "please",
    "provide", "provided", "request", "required", "requires", "should", "the", "this", "to",
    "valid", "value", "was", "were", "with", "you", "your",
}  # fmt: skip
_REQUIRED_HINT = re.compile(
    r"requir|missing|must|provide|need|blank|null|empty|cannot be|invalid", re.I
)


def _flatten_msg(v: Any) -> str:
    if isinstance(v, str):
        return v
    if isinstance(v, list):
        return " ".join(_flatten_msg(x) for x in v)
    if isinstance(v, dict):
        return " ".join(_flatten_msg(x) for x in v.values())
    return str(v)


def error_fields(resp: Any, known: Iterable[str] = ()) -> dict[str, str]:
    """Fields an error body names, mapped to the message(s) about them.

    Understood shapes: DRF ``{field: [msg]}``; ``{"errors": {field: [msg]}}``;
    ``{"errors"|"detail": [{"loc": [..., field], "msg": ..., "type": ...}]}``;
    ``{"detail"|"error"|"message": "text"}`` where the text names fields
    (quoted names first, else bare words that are not stop-words).
    """
    out: dict[str, str] = {}
    if not isinstance(resp, dict):
        return out
    for key in ("errors", "detail"):
        val = resp.get(key)
        if isinstance(val, list):
            for item in val:
                if isinstance(item, dict) and isinstance(item.get("loc"), list):
                    names = [x for x in item["loc"] if isinstance(x, str)]
                    names = [n for n in names if n not in ("body", "query", "path")]
                    if names:
                        msg = f"{item.get('msg', '')} {item.get('type', '')}".strip()
                        out[names[-1]] = msg
        elif isinstance(val, dict) and key == "errors":
            for k, v in val.items():
                out[str(k)] = _flatten_msg(v)
    if out:
        return out
    for k, v in resp.items():
        if k not in _NOT_FIELDS and isinstance(v, (list, str, dict)) and _flatten_msg(v):
            out[str(k)] = _flatten_msg(v)
    if out:
        return out
    known_set = set(known)
    for key in _MSG_KEYS:
        text = resp.get(key)
        if not isinstance(text, str) or not _REQUIRED_HINT.search(text):
            continue
        quoted = re.findall(r"[`'\"]([A-Za-z_][A-Za-z0-9_]*)[`'\"]", text)
        words = [
            w
            for w in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", text)
            if w.lower() not in _MSG_STOP and w.lower() == w
        ]
        names = quoted or [w for w in words if w in known_set] or words[:6]
        for n in names:
            out.setdefault(n, text)
        if out:
            return out
    return out


def _fix_value(
    name: str, msg: str, current: Any, present: bool, prop: dict[str, Any] | None
) -> Any:
    """New value for ``name`` given the error message; ``current`` if nothing better."""
    m = msg.lower()
    if prop and prop.get("type") is not None and (not present or _type_mismatch(prop, current)):
        return value_for(prop, name)
    if "integer" in m or "number" in m or "numeric" in m or "int_" in m or "int " in m:
        return 1
    if "boolean" in m or "bool" in m:
        return True
    if "email" in m:
        return "pd@example.com"
    if "url" in m or "uri" in m:
        return "https://example.com/pd"
    if "datetime" in m or "date-time" in m or "date time" in m:
        return "2024-01-01T00:00:00Z"
    if "date" in m:
        return "2024-01-01"
    if "uuid" in m:
        return "00000000-0000-4000-8000-000000000001"
    if "list" in m or "array" in m:
        return ["pd-item"]
    if not present:
        return f"pd-{name}"
    if isinstance(current, str):
        return 1
    return f"pd-{name}"


def _type_mismatch(prop: dict[str, Any], value: Any) -> bool:
    t = _schema_type(prop)
    if t == "string":
        return not isinstance(value, str)
    if t == "integer":
        return not isinstance(value, int) or isinstance(value, bool)
    if t == "number":
        return not isinstance(value, (int, float)) or isinstance(value, bool)
    if t == "boolean":
        return not isinstance(value, bool)
    if t == "array":
        return not isinstance(value, list)
    return False


def repair_step(step: Step, response: Any, schema: dict[str, Any] | None = None) -> Step | None:
    """One repair of a step that got 400/422: add or adjust the named fields.

    Returns a modified copy of the step, or None when the response names no
    field this function can change.
    """
    body = step.body if isinstance(step.body, dict) else ({} if step.body is None else None)
    if body is None:
        return None
    props = (schema or {}).get("properties") or {}
    fields = error_fields(response, known=props.keys())
    if not fields:
        return None
    new_body = dict(body)
    changed = False
    for name, msg in fields.items():
        present = name in new_body
        val = _fix_value(name, msg, new_body.get(name), present, props.get(name))
        if not present or val != new_body[name]:
            new_body[name] = val
            changed = True
    if not changed:
        return None
    return Step(
        method=step.method,
        path=step.path,
        path_params=dict(step.path_params),
        query=dict(step.query),
        body=new_body,
        content_type=step.content_type or "application/json",
        auth=step.auth,
        capture=dict(step.capture),
    )


# --------------------------------------------------------------------------
# Execution
# --------------------------------------------------------------------------


class ContextSetter(Protocol):
    def set_context(self, request_id: str) -> None: ...


@dataclass
class RunResult:
    exchanges: list[Exchange]
    scenario: Scenario  # the scenario as effectively sent (after any repair)
    aborted: bool = False


class ScenarioRunner:
    """Executes scenarios against a running app; callable as ``run_scenario``.

    ``session`` (a ``CoverageSession``) gets ``set_context(<scenario>:<step>)``
    before every step. A step answered 400/422 is repaired once
    (``repair=True``) and retried in the same coverage context; both exchanges
    are kept, the second with ``attempt=1``. A step that must capture a value
    and cannot ends the scenario (``aborted``).
    """

    def __init__(
        self,
        client: HttpClient,
        *,
        auth: AuthSpec | None = None,
        session: ContextSetter | None = None,
        index: AppIndex | None = None,
        repair: bool = True,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self.client = client
        # The side's own generated bootstrap values: the ONLY source of ``${NAME}``
        # placeholders in a body (never the host environment). Secrets: redacted
        # by value from everything recorded.
        self.env: dict[str, str] = dict(env or {})
        self._secrets = sorted({v for v in self.env.values() if v}, key=len, reverse=True)
        self.scheme = auth.scheme if auth else "Bearer"
        self.session = session
        self.repair = repair
        self._ops = {op_key(o): o for o in (index.operations if index else [])}

    def _schema_for(self, step: Step) -> dict[str, Any] | None:
        op = self._ops.get(f"{step.method} {step.path}")
        return op.request_schema if op else None

    def _send(
        self, scenario_id: str, idx: int, step: Step, variables: dict[str, Any], attempt: int
    ) -> tuple[Exchange, Any, dict[str, str]]:
        """(exchange, live response body, live headers). The exchange is scrubbed."""
        try:
            params = resolve_refs(step.path_params, variables)
            query = resolve_refs(step.query, variables)
            recorded_body = resolve_refs(step.body, variables, strict=False)
            url_path = fill_path(step.path, params)
        except UnresolvedReference as exc:
            req = {"method": step.method, "url_path": step.path, "body": step.body}
            msg = f"unresolved reference ${{{exc.args[0]}}}"
            return Exchange(scenario_id, idx, req, 0, {}, msg, 0.0, attempt), None, {}
        if query:
            url_path += "?" + _urlencode(query)
        headers: dict[str, str] = {"Accept": "application/json"}
        if step.auth and TOKEN_VAR in variables:
            headers["Authorization"] = f"{self.scheme} {variables[TOKEN_VAR]}"
        sent_body = expand_env(recorded_body, self.env)
        left = unresolved_placeholders(sent_body)
        if left:
            # Never send an unexpanded placeholder: nothing goes on the wire.
            req = {"method": step.method, "url_path": url_path, "body": recorded_body}
            msg = "unresolved placeholder " + ", ".join("${" + n + "}" for n in left)
            return Exchange(scenario_id, idx, req, 0, {}, msg, 0.0, attempt), None, {}
        data, ctype = encode_body(sent_body, step.content_type)
        if ctype:
            headers["Content-Type"] = ctype
        status, kept, text, elapsed = self.client.request(
            step.method, url_path, data=data, headers=headers
        )
        live = parse_body(text)
        shown = live
        tok = variables.get(TOKEN_VAR)
        if TOKEN_VAR in step.capture:
            ok, got = capture_value(step.capture[TOKEN_VAR], live, kept)
            tok = got if ok else tok
        if isinstance(tok, str) and tok:
            shown = scrub_value(live, tok)
        req = {"method": step.method, "url_path": url_path, "body": recorded_body}
        for secret in self._secrets:
            shown = scrub_value(shown, secret, "<redacted:bootstrap>")
            req = scrub_value(req, secret, "<redacted:bootstrap>")
        return (
            Exchange(
                scenario_id,
                idx,
                req,
                status,
                kept,
                shown,
                elapsed,
                attempt,
                auth_sent="Authorization" in headers,
            ),
            live,
            kept,
        )

    def __call__(self, scenario: Scenario) -> RunResult:
        variables: dict[str, Any] = {}
        exchanges: list[Exchange] = []
        effective: list[Step] = []
        aborted = False
        for idx, step in enumerate(scenario.steps):
            if self.session is not None:
                self.session.set_context(scenario.request_id(idx))
            current = step
            ex, live, kept = self._send(scenario.id, idx, current, variables, 0)
            exchanges.append(ex)
            if self.repair and ex.status in _REPAIR_STATUSES:
                fixed = repair_step(current, live, self._schema_for(current))
                if fixed is not None:
                    ex2, live2, kept2 = self._send(scenario.id, idx, fixed, variables, 1)
                    exchanges.append(ex2)
                    if ex2.status not in _REPAIR_STATUSES:
                        current, ex, live, kept = fixed, ex2, live2, kept2
            effective.append(current)
            if current.capture:
                got: dict[str, Any] = {}
                if 200 <= ex.status < 300:
                    for name, path in current.capture.items():
                        found, value = capture_value(path, live, kept)
                        if found:
                            got[name] = value
                if len(got) != len(current.capture):
                    aborted = True
                    break
                variables.update(got)
            elif ex.status == 0:
                aborted = True
                break
        eff = Scenario(steps=effective, target_operation=scenario.target_operation)
        if eff.id != scenario.id:
            eff.repaired_from = scenario.id
        return RunResult(exchanges=exchanges, scenario=eff, aborted=aborted)


def _urlencode(query: dict[str, Any]) -> str:
    import urllib.parse

    pairs: list[tuple[str, str]] = []
    for k in sorted(query):
        v = query[k]
        if isinstance(v, bool):
            v = "true" if v else "false"
        pairs.append((k, str(v)))
    return urllib.parse.urlencode(pairs)


# --------------------------------------------------------------------------
# Coverage-guided loop
# --------------------------------------------------------------------------


class SnapshotSession(Protocol):
    def snapshot(self, *, root_prefix: str = "") -> Coverage: ...


ValueHint = Callable[[Operation, str], dict[str, Any]]


@dataclass
class CoverResult:
    impact_set: list[Scenario]
    hits: dict[str, dict[str, list[str]]]  # scenario id -> hits_added / hits_removed
    unexercised: dict[str, dict[str, list[int]]]
    requests_used: int
    scenarios_run: int
    covered: bool
    requests_to_cover: int | None
    budget_requests: int
    exchanges: list[Exchange] = field(default_factory=list)
    aborted_scenarios: list[str] = field(default_factory=list)

    @property
    def budget_exhausted(self) -> bool:
        return not self.covered and self.requests_used >= self.budget_requests

    def stats(self) -> dict[str, Any]:
        return {
            "covered": self.covered,
            "requests_used": self.requests_used,
            "requests_to_cover": self.requests_to_cover,
            "scenarios_run": self.scenarios_run,
            "impact_scenarios": [s.id for s in self.impact_set],
            "unexercised": self.unexercised,
            "budget_requests": self.budget_requests,
            "budget_exhausted": self.budget_exhausted,
        }


def _snippet(dt: DiffText, side: str, unex: dict[str, dict[str, list[int]]]) -> str:
    key = "added" if side == "new" else "removed"
    out = []
    for path, sides in sorted(unex.items()):
        for n in sides[key]:
            text = dt.text_of(path, key, n)
            out.append(f"{path}:{n}: {text if text is not None else ''}")
    return "\n".join(out)


def cover_change(
    index: AppIndex,
    changed: ChangedLines,
    run_scenario: Callable[[Scenario], RunResult],
    coverage_session: SnapshotSession,
    budget_requests: int = 200,
    *,
    diff_text: str = "",
    auth: AuthSpec | None = None,
    scenarios: Sequence[Scenario] | None = None,
    value_hint: ValueHint | None = None,
    side: str = "new",
    root_prefix: str = "",
    max_variants_per_op: int = 24,
) -> CoverResult:
    """Run scenarios until the changed lines are covered or the budget ends.

    ``side="new"`` (default) is for a run on the NEW tree and tries to cover
    the ADDED lines; ``"old"`` is for the OLD tree and the REMOVED lines.
    Scenarios come from :func:`generate_scenarios` unless given; they run in
    that order (operations that mention identifiers of the change first).
    After each scenario coverage is read with ``coverage_session.snapshot()``.
    When every generated scenario has run and lines are still uncovered,
    ``value_hint(operation, uncovered_snippet)`` (if given) may return extra
    body fields for the top-ranked operations; those scenarios run next.

    Requests are counted per exchange (login, producers, retries). A scenario
    is skipped when it cannot start inside the remaining budget; a repair
    retry may overshoot the budget by at most that scenario's steps.
    """
    dt = parse_diff_text(diff_text)
    view = executable_view(changed, dt, side)
    gen = ScenarioGenerator(
        index, diff_text=diff_text, auth=auth, max_variants_per_op=max_variants_per_op
    )
    ranked = rank_operations(index.operations, diff_text)
    queue: list[Scenario] = list(scenarios) if scenarios is not None else gen.generate(ranked)

    def read() -> tuple[Coverage, dict[str, dict[str, list[int]]]]:
        cov = coverage_session.snapshot(root_prefix=root_prefix)
        old, new = ({}, cov) if side == "new" else (cov, {})
        return cov, lc.unexercised(view, old, new)

    used = 0
    ran: list[Scenario] = []
    effective: dict[str, Scenario] = {}
    exchanges: list[Exchange] = []
    aborted: list[str] = []
    cov: Coverage = {}
    unex: dict[str, dict[str, list[int]]] = lc.unexercised(view, {}, {})
    to_cover: int | None = 0 if not unex else None
    hint_done = False
    i = 0
    while unex:
        if i >= len(queue):
            if value_hint is None or hint_done:
                break
            hint_done = True
            snippet = _snippet(dt, side, unex)
            for op in ranked[:3]:
                extra = value_hint(op, snippet)
                if extra:
                    queue.extend(gen.for_operation(op, body_override=extra)[:1])
            continue
        sc = queue[i]
        i += 1
        if used + len(sc.steps) > budget_requests:
            continue
        result = run_scenario(sc)
        used += len(result.exchanges)
        exchanges.extend(result.exchanges)
        ran.append(sc)
        effective[sc.id] = result.scenario
        if result.aborted:
            aborted.append(sc.id)
        cov, unex = read()
        if not unex:
            to_cover = used
        if used >= budget_requests:
            break
    hits: dict[str, dict[str, list[str]]] = {}
    impact_set: list[Scenario] = []
    if ran:
        old, new = ({}, cov) if side == "new" else (cov, {})
        per_request = lc.impact(view, old, new)
        for sc in ran:
            agg: dict[str, list[str]] = {"hits_added": [], "hits_removed": []}
            for rid, h in per_request.items():
                if rid.split(":", 1)[0] == sc.id:
                    for k in agg:
                        agg[k] = sorted(set(agg[k]) | set(h[k]))
            if agg["hits_added"] or agg["hits_removed"]:
                hits[sc.id] = agg
                impact_set.append(effective[sc.id])
    return CoverResult(
        impact_set=impact_set,
        hits=hits,
        unexercised=unex,
        requests_used=used,
        scenarios_run=len(ran),
        covered=not unex,
        requests_to_cover=to_cover,
        budget_requests=budget_requests,
        exchanges=exchanges,
        aborted_scenarios=aborted,
    )
