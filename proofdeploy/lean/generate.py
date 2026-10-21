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
   (:func:`cover_change`), reading coverage after every scenario: a seeding
   stage first (records in varied states built through the API), one request
   per operation, then the remaining budget aimed at the operations whose
   requests executed the most changed lines. The runner signs in once and
   reuses the token until a 401/403.

The only model hook is ``value_hint``: an optional callable that is given an
operation and the uncovered diff text and may return extra body fields. It is
an interface only; nothing here implements it with a model.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, TypeGuard

from proofdeploy.lean import coverage as lc
from proofdeploy.lean.coverage import ChangedLines, Coverage, split_diff_lines
from proofdeploy.lean.http import (
    TOKEN_VAR,
    AuthSpec,
    Exchange,
    HttpClient,
    MissingStaticToken,
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


_ID_LIST = r"\d+(?:\s*(?:,|and|&)\s*\d+)*"
_RECORD_RE = re.compile(
    r"(?:\b(?P<w>[A-Za-z][A-Za-z_-]*)\s+)?(?:\bids?\s*[:=#]?\s*|#\s*)(?P<n>" + _ID_LIST + r")"
    r"|\b(?P<w2>[A-Za-z][A-Za-z_-]*)\s+(?P<n2>" + _ID_LIST + r")(?![\w.])",
    re.IGNORECASE,
)
# Words that may stand before ``id <n>`` without naming a collection ("seed id 1").
_ID_FILLER = {
    "a", "an", "and", "as", "at", "both", "by", "for", "has", "have", "id", "ids", "is", "are",
    "of", "or", "record", "records", "row", "rows", "seed", "seeded", "the", "to", "with",
}  # fmt: skip


def _flat(word: str) -> str:
    return re.sub(r"[^a-z0-9]", "", word.lower())


def _singulars(coll: str) -> set[str]:
    c = _flat(coll)
    out = {c}
    if c.endswith("ies") and len(c) > 3:
        out.add(c[:-3] + "y")
    if c.endswith("es") and len(c) > 2:
        out.add(c[:-2])
    if c.endswith("s") and len(c) > 1:
        out.add(c[:-1])
    return out


def seeded_records(
    seed_facts: Iterable[str], collections: Iterable[str] = ()
) -> list[tuple[str | None, int]]:
    """Record ids named in seed sentences as ``(collection | None, id)``, in order.

    ``id <n>``, ``id=<n>``, ``#<n>`` name an id for any collection (None).
    ``<word> <n>`` (also ``<word> id <n>``, ``<word> #<n>``) names an id for the
    collection of ``collections`` whose singular or plural form is ``<word>``; a
    word that matches none of them is ignored. Only integers; no ids, empty list.
    """
    colls = list(dict.fromkeys(collections))
    out: list[tuple[str | None, int]] = []
    for fact in seed_facts:
        for m in _RECORD_RE.finditer(fact):
            word = m.group("w") or m.group("w2")
            nums = [int(n) for n in re.findall(r"\d+", m.group("n") or m.group("n2"))]
            target: str | None = None
            if word and word.lower() not in _ID_FILLER:
                hits = [c for c in colls if _flat(word) in _singulars(c) or _flat(word) == _flat(c)]
                if not hits:
                    continue
                target = hits[0]
            elif m.group("w2"):
                continue  # "<filler> <n>" is not an id mention
            for n in nums:
                if (target, n) not in out:
                    out.append((target, n))
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


# --------------------------------------------------------------------------
# Seed values and edge values (WO-R4-G items 2 and 3)
# --------------------------------------------------------------------------

SEED_DEFAULT_STRING = 255
SEED_MAX_STRING = 10000
LONG_STRING = 5000
INT_MAX = 2**31 - 1
UNICODE_TEXT = "ünï字é"
AIM_INPUT_CAP = 48
AIM_PATH_CAP = 12
_REF_NAME_RE = re.compile(r"^(?P<stem>.+?)(?:_ids?|_pks?|Ids?|Pks?)$")


def _tag_number(tag: str) -> int:
    digits = re.sub(r"\D", "", tag)
    return int(digits) if digits else 0


def _numeric(v: Any) -> TypeGuard[int | float]:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _seed_value(schema: Any, name: str, tag: str, mode: str, _depth: int = 0) -> Any:
    """A value for one field of a seed record.

    ``mode``: ``min`` (plain), ``full`` (strings at maxLength or 255 with unicode,
    numbers at their maximum) or ``alt`` (booleans false, numbers at the low end,
    last enum member). ``tag`` keeps unique-looking fields apart between records.
    """
    if not isinstance(schema, dict) or _depth > 3:
        return f"pd-{name}-{tag}"
    if "x-cycle" in schema:
        return None
    if "const" in schema:
        return schema["const"]
    enum = schema.get("enum")
    if isinstance(enum, list) and enum:
        return enum[-1] if mode == "alt" else enum[0]
    for key in ("oneOf", "anyOf"):
        non_null = [
            m for m in schema.get(key) or [] if isinstance(m, dict) and m.get("type") != "null"
        ]
        if non_null:
            return _seed_value(non_null[0], name, tag, mode, _depth + 1)
    t = _schema_type(schema)
    if t == "string":
        fmt = str(schema.get("format") or "")
        if fmt in ("email", "idn-email"):
            return f"pd-{tag}@example.com"
        if fmt in ("uri", "url", "uri-reference"):
            return f"https://example.com/pd-{tag}"
        if fmt == "uuid":
            return f"00000000-0000-4000-8000-{_tag_number(tag):012d}"
        if fmt in _FORMAT_VALUES:
            return _FORMAT_VALUES[fmt]
        lo, hi = schema.get("minLength"), schema.get("maxLength")
        if mode == "full":
            n = hi if isinstance(hi, int) and 0 < hi <= SEED_MAX_STRING else SEED_DEFAULT_STRING
            if isinstance(lo, int) and n < lo:
                n = lo
            text = UNICODE_TEXT + f"-{name}-{tag}-" + UNICODE_TEXT * (n // len(UNICODE_TEXT) + 1)
            return text[:n]
        s = f"pd-{name}-{tag}"
        if isinstance(lo, int) and len(s) < lo:
            s += "x" * (lo - len(s))
        if isinstance(hi, int) and hi > 0 and len(s) > hi:
            s = s[:hi]
        return s
    if t in ("integer", "number"):
        v = value_for(schema, name)
        top = schema.get("maximum")
        if mode == "full" and _numeric(top) and abs(top) <= INT_MAX:
            return int(top)
        if (
            mode == "alt"
            and schema.get("minimum") is None
            and schema.get("exclusiveMinimum") is None
        ):
            return 0
        return v
    if t == "boolean":
        return mode != "alt"
    if t == "array":
        return [_seed_value(schema.get("items"), name, tag, mode, _depth + 1)]
    if t == "object":
        return value_for(schema, name)
    if t == "null":
        return None
    return f"pd-{name}-{tag}"


def _changed_value(schema: dict[str, Any], name: str, current: Any) -> Any:
    """A different, valid-looking value for one field (the update step)."""
    t = _schema_type(schema)
    if t == "boolean":
        return not current if isinstance(current, bool) else False
    if t in ("integer", "number"):
        top = schema.get("maximum")
        if isinstance(current, int) and not isinstance(current, bool):
            return current - 1 if _numeric(top) and current >= top else current + 1
        return 2
    s = f"pd-{name}-upd"
    hi = schema.get("maxLength")
    if isinstance(hi, int) and hi > 0:
        s = s[:hi]
    if s == current:
        s = s[:-1] + ("z" if not s.endswith("z") else "y")
    return s


def _edge_values(schema: dict[str, Any], optional: bool) -> list[tuple[str, Any]]:
    """Boundary inputs for one field: numbers 0/-1/max, empty/long/unicode strings, null."""
    out: list[tuple[str, Any]] = []
    t = _schema_type(schema)
    enum = schema.get("enum")
    if isinstance(enum, list) and enum:
        out.extend((f"enum={v}", v) for v in enum[1:])
    elif t in ("integer", "number"):
        top = schema.get("maximum")
        out.extend([("0", 0), ("-1", -1), ("max", int(top) if _numeric(top) else INT_MAX)])
    elif t == "string":
        out.extend([("empty", ""), ("long", "a" * LONG_STRING), ("unicode", UNICODE_TEXT)])
    elif t == "array":
        out.append(("empty-list", []))
    if optional:
        out.append(("null", None))
    seen: set[str] = set()
    uniq: list[tuple[str, Any]] = []
    for label, v in out:
        key = json.dumps(v, sort_keys=True, default=str)
        if key not in seen:
            seen.add(key)
            uniq.append((label, v))
    return uniq


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
        self._coll_cache: dict[str, Operation] | None = None
        self._chain_n = 0
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
            steps.extend(self.auth.login_steps())
        steps.extend(pre)
        steps.append(target)
        return Scenario(steps=steps, target_operation=op_key(op))

    def _is_login_op(self, op: Operation) -> bool:
        a = self.auth
        return (
            a is not None
            and not a.is_static
            and op.method == a.login.method
            and _norm(op.path) == _norm(a.login.path)
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

    def seeded_reads(self, ops: Sequence[Operation]) -> list[Scenario]:
        """One read of every seeded record id on every GET op with exactly one path param."""
        colls = [c for c in (self._collection_of(op) for op in self.index.operations) if c]
        records = seeded_records(self.index.seed_facts, colls)
        if not records:
            return []
        authed = self.auth is not None
        out: list[Scenario] = []
        for op in ops:
            params = _param_schemas(op)
            if op.method != "GET" or len(params) != 1 or self._is_login_op(op):
                continue
            (name, schema), coll = next(iter(params.items())), self._collection_of(op)
            if _schema_type(schema) not in ("integer", "number", None):
                continue
            _, body, query = request_variants(op, self.mined)[0]
            for target, n in records:
                if target is not None and target != coll:
                    continue
                out.append(
                    self._make(
                        op, [], self._target(op, body, query, {name: n}, authed=authed), login=True
                    )
                )
        return out

    @staticmethod
    def _collection_of(op: Operation) -> str | None:
        """The path segment just before the op's single ``{param}`` (None if none/several)."""
        segs = op.path.strip("/").split("/")
        marks = [i for i, sg in enumerate(segs) if sg.startswith("{") and sg.endswith("}")]
        if len(marks) != 1 or marks[0] == 0:
            return None
        return segs[marks[0] - 1]

    def generate(self, ops: Sequence[Operation] | None = None) -> list[Scenario]:
        """All scenarios, ordered: seeded record reads, the first scenario of each ranked op,
        then the rest."""
        use = list(ops if ops is not None else self.index.operations)
        per_op = [self.for_operation(op) for op in use]
        seeded = self.seeded_reads(use)
        first = [lst[0] for lst in per_op if lst]
        rest = [s for lst in per_op for s in lst[1:]]
        seen: set[str] = set()
        out: list[Scenario] = []
        for sc in [*seeded, *first, *rest]:
            if sc.id not in seen:
                seen.add(sc.id)
                out.append(sc)
        return out

    # -- seeding stage (varied states through the API) ---------------------------

    def _wrap(self, target: Operation, steps: Sequence[Step]) -> Scenario:
        head = self.auth.login_steps() if self.auth is not None else []
        return Scenario(steps=[*head, *steps], target_operation=op_key(target))

    def _is_create_op(self, op: Operation) -> bool:
        schema = op.request_schema
        return (
            op.method == "POST"
            and not self._is_login_op(op)
            and isinstance(schema, dict)
            and _schema_type(schema) == "object"
            and bool(schema.get("properties"))
            and "json" in (op.request_content_type or "application/json")
        )

    def _collections(self) -> dict[str, Operation]:
        """Singular and plural names of top-level collections -> their create operation."""
        if self._coll_cache is None:
            out: dict[str, Operation] = {}
            for op in sorted(self.index.operations, key=lambda o: (o.path, o.method)):
                if not self._is_create_op(op) or _param_schemas(op):
                    continue
                segs = [s for s in op.path.strip("/").split("/") if s]
                if segs:
                    for name in _singulars(segs[-1]):
                        out.setdefault(name, op)
            self._coll_cache = out
        return self._coll_cache

    def _ref_fields(self, op: Operation) -> dict[str, tuple[Operation, bool]]:
        """Writable fields that name another record (``<thing>_id``, ``<thing>Ids``), with
        the create operation of that collection and whether the field is a list."""
        out: dict[str, tuple[Operation, bool]] = {}
        colls = self._collections()
        for k, sch in _writable_props(op.request_schema or {}).items():
            m = _REF_NAME_RE.match(k)
            t = _schema_type(sch)
            if not m or t not in ("integer", "number", "string", "array"):
                continue
            target = colls.get(_flat(m.group("stem")))
            if target is not None:
                out[k] = (target, t == "array")
        return out

    def _item_ops(self, create_op: Operation) -> dict[str, tuple[Operation, str]]:
        """Operations on ``<collection>/{param}`` by method, with the name of that param."""
        coll = _norm(create_op.path)
        out: dict[str, tuple[Operation, str]] = {}
        for o in self.index.operations:
            segs = o.path.strip("/").split("/")
            if len(segs) < 2 or not (segs[-1].startswith("{") and segs[-1].endswith("}")):
                continue
            if _norm("/".join(segs[:-1])) == coll:
                out.setdefault(o.method, (o, segs[-1][1:-1]))
        return out

    def _seed_body(
        self, op: Operation, mode: str, tag: str, refs: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        """``min``: required fields only; ``full``: every writable field; ``alt``: required
        plus every second optional field, with the low-end values."""
        schema = op.request_schema or {}
        props = _writable_props(schema)
        required = set(_required_names(schema))
        names = sorted(props)
        optional = [k for k in names if k not in required]
        if mode == "min":
            use = [k for k in names if k in required]
        elif mode == "full":
            use = names
        else:
            use = [k for k in names if k in required or optional.index(k) % 2 == 0]
        body: dict[str, Any] = {}
        for k in use:
            body[k] = refs[k] if refs and k in refs else _seed_value(props[k], k, tag, mode)
        for k in sorted(required):
            if k not in (schema.get("properties") or {}):
                body[k] = f"pd-{k}-{tag}"
        return body

    def _referent_steps(
        self, target: Operation, var: str, kind: str, tag: str
    ) -> tuple[list[Step], Any]:
        """Steps that make a record of ``target`` (and delete it for ``kind="deleted"``);
        the value to put in the referring field."""
        authed = self.auth is not None
        create = Step(
            "POST",
            target.path,
            query=request_variants(target)[0][2],
            body=self._seed_body(target, "min", tag),
            content_type=target.request_content_type or "application/json",
            auth=authed,
            capture={var: self._capture_path(target, "id")},
        )
        if kind != "deleted":
            return [create], "${" + var + "}"
        info = self._item_ops(target).get("DELETE")
        if info is None:
            return [], MISSING_ID  # nothing can delete it: an id that never existed
        op_d, pname = info
        gone = Step("DELETE", op_d.path, path_params={pname: "${" + var + "}"}, auth=authed)
        return [create, gone], "${" + var + "}"

    def _parent_context(self, op: Operation) -> tuple[list[Step], dict[str, Any]]:
        """Producer steps for the path params of ``op`` and their values."""
        chain = self._chain(op)
        if chain:
            return chain
        fb = self.seed_ids[0] if self.seed_ids else None
        return [], {n: _default_param(n, s, fb) for n, s in _param_schemas(op).items()}

    def _create_context(
        self, op: Operation, mode: str, tag: str
    ) -> tuple[list[Step], dict[str, Any], Step]:
        """Steps up to and including the create of one record (capturing ``rec``), the path
        params of the collection, and the create step."""
        authed = self.auth is not None
        pre, ids = self._parent_context(op)
        steps = list(pre)
        refs: dict[str, Any] = {}
        required = set(_required_names(op.request_schema or {}))
        kind = "deleted" if mode == "alt" else "existing"
        for k, (target, is_list) in self._ref_fields(op).items():
            if mode == "min" and k not in required:
                continue
            var = "ref_" + re.sub(r"\W", "_", k)
            more, value = self._referent_steps(target, var, kind, f"{tag}{mode[0]}")
            steps.extend(more)
            refs[k] = [value] if is_list else value
        create = Step(
            "POST",
            op.path,
            path_params=dict(ids),
            query=request_variants(op)[0][2],
            body=self._seed_body(op, mode, tag, refs),
            content_type=op.request_content_type or "application/json",
            auth=authed,
            capture={"rec": self._capture_path(op, "id")},
        )
        steps.append(create)
        return steps, ids, create

    def _item_step(
        self, create_op: Operation, method: str, ids: Mapping[str, Any], body: Any = None
    ) -> Step | None:
        info = self._item_ops(create_op).get(method)
        if info is None:
            return None
        o, pname = info
        others = set(_param_schemas(o)) - {pname}
        if not others <= set(ids):
            return None
        _, own_body, query = request_variants(o)[0]
        sent = body if body is not None else own_body
        return Step(
            method,
            o.path,
            path_params={**{n: ids[n] for n in others}, pname: "${rec}"},
            query=query,
            body=sent,
            content_type=(o.request_content_type or "application/json")
            if sent is not None
            else None,
            auth=self.auth is not None,
        )

    def _list_step(self, create_op: Operation, ids: Mapping[str, Any]) -> Step | None:
        for o in self._by_path.get(_norm(create_op.path), []):
            if o.method == "GET" and set(_param_schemas(o)) <= set(ids):
                return Step(
                    "GET",
                    o.path,
                    path_params={n: ids[n] for n in _param_schemas(o)},
                    query=request_variants(o)[0][2],
                    auth=self.auth is not None,
                )
        return None

    def _update_step(
        self, create_op: Operation, ids: Mapping[str, Any], created: Any
    ) -> Step | None:
        info = self._item_ops(create_op).get("PATCH") or self._item_ops(create_op).get("PUT")
        if info is None:
            return None
        o, _ = info
        schema = (
            o.request_schema if isinstance(o.request_schema, dict) else create_op.request_schema
        )
        props = _writable_props(schema or {})
        skip = set(self._ref_fields(create_op))
        field_name = next(
            (
                k
                for k in sorted(props)
                if k not in skip
                and not props[k].get("enum")
                and _schema_type(props[k]) in ("string", "integer", "boolean")
            ),
            None,
        )
        if field_name is None:
            return None
        base = dict(created) if isinstance(created, dict) else {}
        new = _changed_value(props[field_name], field_name, base.get(field_name))
        body = {field_name: new} if o.method == "PATCH" else {**base, field_name: new}
        return self._item_step(create_op, o.method, ids, body)

    def seed_scenarios(self, ops: Sequence[Operation] | None = None) -> list[Scenario]:
        """The seeding stage: records in varied states, built through the API.

        For every create operation (POST with a JSON object body schema) three
        records, each followed by a read of it: ``min`` (optional fields
        omitted), ``full`` (every optional field set, strings at maxLength or
        255 with unicode, references to an existing record) and ``alt`` (every
        second optional field, low-end values, references to a DELETED record).
        Then, per collection, a list, an update of one field and a delete (with
        a read after it). Each scenario is self-contained (it creates what it
        refers to), so it replays identically on every side and run. A shape
        the app rejects ends that scenario as data. Stage order: all creates,
        all lists, all updates, all deletes.
        """
        creates = [
            o for o in (ops if ops is not None else self.index.operations) if self._is_create_op(o)
        ]
        n = 0
        stage_create: list[Scenario] = []
        stage_list: list[Scenario] = []
        stage_update: list[Scenario] = []
        stage_delete: list[Scenario] = []
        for op in creates:
            for mode in ("min", "full", "alt"):
                n += 1
                steps, ids, _ = self._create_context(op, mode, f"s{n}")
                read = self._item_step(op, "GET", ids)
                stage_create.append(self._wrap(op, [*steps, *([read] if read else [])]))
        for op in creates:
            pre, pids = self._parent_context(op)
            lst = self._list_step(op, pids)
            if lst is not None:
                stage_list.append(self._wrap(op, [*pre, lst]))
            n += 1
            steps, ids, created = self._create_context(op, "full", f"s{n}")
            upd = self._update_step(op, ids, created.body)
            if upd is not None:
                read = self._item_step(op, "GET", ids)
                stage_update.append(self._wrap(op, [*steps, upd, *([read] if read else [])]))
            n += 1
            steps, ids, _ = self._create_context(op, "min", f"s{n}")
            gone = self._item_step(op, "DELETE", ids)
            if gone is not None:
                after = self._item_step(op, "GET", ids)
                stage_delete.append(self._wrap(op, [*steps, gone, *([after] if after else [])]))
        seen: set[str] = set()
        out: list[Scenario] = []
        for sc in [*stage_create, *stage_list, *stage_update, *stage_delete]:
            if sc.id not in seen:
                seen.add(sc.id)
                out.append(sc)
        return out

    # -- aimed variants (item 2) -------------------------------------------------

    def input_variants(self, op: Operation) -> list[tuple[str, Any, dict[str, Any]]]:
        """Boundary inputs for the fields of one operation as ``(label, body, query)``.

        Numbers 0/-1/max, empty, very long and unicode strings, null for optional
        fields, other enum members. Fields named like a mined constant come first.
        After the single-field variants: every field at the same edge at once, then
        pairs of fields.
        """
        _, base_body, base_query = request_variants(op)[0]
        schema = op.request_schema if isinstance(op.request_schema, dict) else {}
        required = set(_required_names(schema))
        q_all = _query_params(op)
        q_required = {
            str(p["name"]) for p in op.params if p.get("in") == "query" and p.get("required")
        }
        fields: list[tuple[str, str, dict[str, Any], bool]] = []
        if isinstance(base_body, dict):
            fields += [
                ("body", k, sch, k not in required) for k, sch in _writable_props(schema).items()
            ]
        fields += [("query", k, sch, k not in q_required) for k, sch in q_all.items()]

        def matched(item: tuple[str, str, dict[str, Any], bool]) -> bool:
            return any(_field_matches(item[1], c) for c in self.mined)

        fields = [f for f in fields if matched(f)] + [f for f in fields if not matched(f)]
        singles: list[tuple[str, str, str, Any]] = []  # (where, field, label, value)
        for where, k, sch, optional in fields:
            singles.extend((where, k, label, v) for label, v in _edge_values(sch, optional))

        def variant(
            picked: Sequence[tuple[str, str, str, Any]],
        ) -> tuple[str, Any, dict[str, Any]]:
            body = dict(base_body) if isinstance(base_body, dict) else base_body
            query = dict(base_query)
            for where, k, _, v in picked:
                if where == "body" and isinstance(body, dict):
                    body[k] = v
                elif where == "query":
                    query[k] = v
            label = "+".join(f"{k}={lbl}" for _, k, lbl, _ in picked)
            return f"edge:{label}", body, query

        out = [variant([one]) for one in singles[:AIM_INPUT_CAP]]
        # every field at the same edge at once, then pairs of different fields
        for label in dict.fromkeys(lbl for _, _, lbl, _ in singles):
            same = [one for one in singles if one[2] == label]
            if len({k for _, k, _, _ in same}) > 1:
                out.append(variant(same))
        pairs = 0
        for a in range(len(singles)):
            for b in range(a + 1, len(singles)):
                if singles[a][1] != singles[b][1] and pairs < AIM_INPUT_CAP:
                    out.append(variant([singles[a], singles[b]]))
                    pairs += 1
        return out

    def _is_producer_op(self, op: Operation) -> bool:
        """A POST that makes a record: with or without a body schema (no schema sends ``{}``)."""
        return op.method == "POST" and not self._is_login_op(op)

    def _collection_create_of(self, op: Operation) -> Operation | None:
        if self._is_producer_op(op) and not _param_schemas(op) or self._is_create_op(op):
            return op
        segs = op.path.strip("/").split("/")
        if len(segs) > 1 and segs[-1].startswith("{") and segs[-1].endswith("}"):
            where = _norm("/".join(segs[:-1]))
        elif op.method == "GET":
            where = _norm(op.path)
        else:
            return None
        return next((c for c in self._by_path.get(where, []) if self._is_producer_op(c)), None)

    def path_variants(self, op: Operation) -> list[tuple[str, dict[str, Any]]]:
        """Boundary values for the path params of ``op`` (0, -1, max, empty, long, unicode)."""
        params = _param_schemas(op)
        fb = self.seed_ids[0] if self.seed_ids else None
        base = {n: _default_param(n, s, fb) for n, s in params.items()}
        out: list[tuple[str, dict[str, Any]]] = []
        for name, sch in params.items():
            for label, v in _edge_values(sch or {"type": "integer"}, False):
                out.append((f"path:{name}={label}", {**base, name: v}))
        return out[:AIM_PATH_CAP]

    def chain_scenarios(self, op: Operation) -> list[Scenario]:
        """Create -> read/list/update/delete sequences on one record, ending on ``op``'s kind."""
        coll = self._collection_create_of(op)
        if coll is None:
            return []
        self._chain_n += 1
        out: list[Scenario] = []
        info = self._item_ops(coll)
        is_item = any(o is op for o, _ in info.values())

        def build(
            mode: str, wanted: Callable[[Mapping[str, Any], Step], list[Step | None]]
        ) -> None:
            self._chain_n += 1
            steps, ids, created = self._create_context(coll, mode, f"c{self._chain_n}")
            tail = [s for s in wanted(ids, created) if s is not None]
            if tail:
                out.append(
                    Scenario(
                        steps=[*self._wrap(op, steps).steps, *tail], target_operation=op_key(op)
                    )
                )

        if op is coll:
            build(
                "full",
                lambda ids, c: [
                    self._item_step(coll, "GET", ids),
                    self._list_step(coll, ids),
                    self._update_step(coll, ids, c.body),
                    self._item_step(coll, "DELETE", ids),
                    self._item_step(coll, "GET", ids),
                ],
            )
            build(
                "min",
                lambda ids, c: [
                    self._update_step(coll, ids, c.body),
                    self._item_step(coll, "GET", ids),
                ],
            )
        elif is_item:
            own = lambda ids: self._item_step(coll, op.method, ids)  # noqa: E731
            if op.method == "GET":
                build("full", lambda ids, c: [self._update_step(coll, ids, c.body), own(ids)])
                build("min", lambda ids, c: [self._item_step(coll, "DELETE", ids), own(ids)])
            else:
                build("full", lambda ids, c: [own(ids), self._item_step(coll, "GET", ids)])
                build("min", lambda ids, c: [own(ids), own(ids)])
        else:
            build(
                "min",
                lambda ids, c: [
                    Step(**{**c.to_dict(), "capture": {}}),
                    self._list_step(coll, ids),
                ],
            )
        return out

    def aimed_scenarios(self, op: Operation) -> list[Scenario]:
        """Everything worth sending at ``op`` after its first request, most varied first:
        the generated variants, boundary inputs and record chains, interleaved."""
        chain = self._chain(op)
        pre, ids = chain if chain else ([], None)
        params = _param_schemas(op)
        if ids is None:
            fb = self.seed_ids[0] if self.seed_ids else None
            ids = {n: _default_param(n, s, fb) for n, s in params.items()}
        authed = self.auth is not None
        groups = [
            self.for_operation(op)[1:],
            [
                self._make(op, pre, self._target(op, b, q, ids, authed=authed), login=True)
                for _, b, q in self.input_variants(op)
            ],
            [
                self._make(
                    op,
                    [],
                    self._target(op, *request_variants(op)[0][1:], pids, authed=authed),
                    login=True,
                )
                for _, pids in self.path_variants(op)
            ],
            self.chain_scenarios(op),
        ]
        seen: set[str] = set()
        out: list[Scenario] = []
        for i in range(max(len(g) for g in groups)):
            for g in groups:
                if i < len(g) and g[i].id not in seen:
                    seen.add(g[i].id)
                    out.append(g[i])
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
_AUTH_REJECTED = (401, 403)
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

    Sign in once: the contract's login step runs the first time a scenario
    needs it; its token is cached and every later scenario skips its own copy
    of the login step (the step stays in the scenario, no request is sent) until
    an authenticated request is answered 401/403, which drops the token so the
    next scenario signs in again. ``logins`` counts the login requests sent.
    A scenario whose target IS the login operation always sends its login.
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
        self._auth = auth
        self._token: Any = None
        self._static = bool(auth is not None and auth.is_static)
        if auth is not None and auth.static_token_env is not None:
            # the token IS the side's generated value; no sign-in request exists for it
            value = self.env.get(auth.static_token_env)
            if not value:
                raise MissingStaticToken(f"no generated value for {auth.static_token_env}")
            self._token = value
        self.logins = 0
        self.session = session
        self.repair = repair
        self._ops = {op_key(o): o for o in (index.operations if index else [])}

    def _reuses_login(self, step: Step, scenario: Scenario) -> bool:
        """True when ``step`` is the contract login and a cached token makes it unnecessary."""
        a = self._auth
        if a is None or self._token is None or step != a.login:
            return False
        if self._static:
            return True  # a static spec has no login to send, whatever the scenario targets
        return scenario.target_operation != f"{a.login.method} {a.login.path}"

    def planned_requests(self, scenario: Scenario) -> int:
        """Requests the scenario needs right now (its steps minus a login that is cached)."""
        return sum(1 for st in scenario.steps if not self._reuses_login(st, scenario))

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
        if self._token is not None:
            variables[TOKEN_VAR] = self._token
        exchanges: list[Exchange] = []
        effective: list[Step] = []
        aborted = False
        for idx, step in enumerate(scenario.steps):
            if self._reuses_login(step, scenario):
                effective.append(step)
                continue
            is_login = self._auth is not None and step == self._auth.login
            if self.session is not None:
                self.session.set_context(scenario.request_id(idx))
            current = step
            ex, live, kept = self._send(scenario.id, idx, current, variables, 0)
            exchanges.append(ex)
            if is_login:
                self.logins += 1
            if self.repair and ex.status in _REPAIR_STATUSES:
                fixed = repair_step(current, live, self._schema_for(current))
                if fixed is not None:
                    ex2, live2, kept2 = self._send(scenario.id, idx, fixed, variables, 1)
                    exchanges.append(ex2)
                    if ex2.status not in _REPAIR_STATUSES:
                        current, ex, live, kept = fixed, ex2, live2, kept2
            effective.append(current)
            if ex.auth_sent and ex.status in _AUTH_REJECTED and not self._static:
                self._token = None  # the next scenario signs in again (a static token stays)
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
                if is_login:
                    self._token = variables.get(TOKEN_VAR)
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
    logins: int = 0  # login requests sent in this loop (they count in requests_used)
    seed_scenarios_run: int = 0
    seed_requests: int = 0
    # Requests spent per target operation (all scenarios, seeding included).
    requests_by_operation: dict[str, int] = field(default_factory=dict)
    # The aiming pass: operations ranked by changed lines their requests executed,
    # each with its request quota and the scenarios queued for it.
    aim: list[dict[str, Any]] = field(default_factory=list)

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
            "logins": self.logins,
            "seed_scenarios_run": self.seed_scenarios_run,
            "seed_requests": self.seed_requests,
            "requests_by_operation": dict(sorted(self.requests_by_operation.items())),
            "aim": self.aim,
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
    seed: bool = True,
) -> CoverResult:
    """Run scenarios until the changed lines are covered or the budget ends.

    ``side="new"`` (default) is for a run on the NEW tree and tries to cover
    the ADDED lines; ``"old"`` is for the OLD tree and the REMOVED lines.
    Unless ``scenarios`` is given, the stages are:

    1. SEEDING (``seed=True``): :meth:`ScenarioGenerator.seed_scenarios`, at most
       a third of the budget. The seeding scenarios that ran always join the
       impact set (first), because the 2x2 replays must rebuild their state.
    2. FIRST PASS: the seeded reads and one scenario per operation, in ranked order.
    3. AIM: operations are ranked by the number of changed executable lines their
       requests executed (per-request coverage contexts). The remaining budget
       goes to the top ones, half to the first, half of the rest to the second,
       and so on: boundary inputs, mined constants and create/read/list/update/
       delete chains on one record (:meth:`ScenarioGenerator.aimed_scenarios`).
       With no operation reaching a changed line, the three best by identifier
       match get the same shares.
    4. REST: every other variant, in ranked order, then ``value_hint`` (if given)
       for the top-ranked operations.

    After each scenario coverage is read with ``coverage_session.snapshot()``
    and the loop stops as soon as every changed line is covered. Nothing is
    random: the same inputs give the same scenarios in the same order.

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
    auto = scenarios is None
    planned = getattr(run_scenario, "planned_requests", None)

    def need_of(sc: Scenario) -> int:
        return int(planned(sc)) if callable(planned) else len(sc.steps)

    seeds = gen.seed_scenarios(ranked) if auto and seed else []
    seed_set = {s.id for s in seeds}
    seed_cap = budget_requests // 3
    per_op: dict[str, list[Scenario]] = {}
    phase = 3
    if scenarios is not None:
        queue: list[Scenario] = list(scenarios)
    else:
        per_op = {op_key(o): gen.for_operation(o) for o in ranked}
        firsts = [per_op[op_key(o)][0] for o in ranked if per_op[op_key(o)]]
        queue = []
        for sc in [*seeds, *gen.seeded_reads(ranked), *firsts]:
            if sc.id not in {q.id for q in queue}:
                queue.append(sc)
        phase = 1

    def read() -> tuple[Coverage, dict[str, dict[str, list[int]]]]:
        cov = coverage_session.snapshot(root_prefix=root_prefix)
        old, new = ({}, cov) if side == "new" else (cov, {})
        return cov, lc.unexercised(view, old, new)

    used = 0
    ran: list[Scenario] = []
    effective: dict[str, Scenario] = {}
    exchanges: list[Exchange] = []
    aborted: list[str] = []
    by_op: dict[str, int] = {}
    step_ops: dict[str, str] = {}  # request id -> "METHOD path template" of the step
    aim_info: list[dict[str, Any]] = []
    seeds_ran: list[Scenario] = []
    seed_used = 0
    logins_before = int(getattr(run_scenario, "logins", 0))
    cov: Coverage = {}
    unex: dict[str, dict[str, list[int]]] = lc.unexercised(view, {}, {})
    to_cover: int | None = 0 if not unex else None
    hint_done = False

    def aim_batch() -> list[Scenario]:
        old, new = ({}, cov) if side == "new" else (cov, {})
        reached: dict[str, set[str]] = {}
        for rid, h in lc.impact(view, old, new).items():
            key = step_ops.get(rid)
            if key:
                reached.setdefault(key, set()).update(h["hits_added"], h["hits_removed"])
        pos = {op_key(o): n for n, o in enumerate(ranked)}
        cands = [o for o in ranked if reached.get(op_key(o))]
        cands.sort(key=lambda o: (-len(reached[op_key(o)]), pos[op_key(o)]))
        reaching = bool(cands)
        if not cands:
            cands = ranked[:3]
        left = max(budget_requests - used, 0)
        queued = {q.id for q in queue}
        batch: list[Scenario] = []
        for k, op in enumerate(cands):
            quota = left >> (k + 1) if k < len(cands) - 1 else left >> (len(cands) - 1)
            spent = count = 0
            for sc in gen.aimed_scenarios(op):
                if sc.id in queued:
                    continue
                need = need_of(sc)
                if spent + need > quota:
                    continue
                batch.append(sc)
                queued.add(sc.id)
                spent += need
                count += 1
            aim_info.append(
                {
                    "operation": op_key(op),
                    "changed_lines_reached": len(reached.get(op_key(op), ())),
                    "reached_a_changed_line": reaching,
                    "request_quota": quota,
                    "scenarios_queued": count,
                    "budget_left": left,
                }
            )
        return batch

    i = 0
    while unex:
        if i >= len(queue):
            if phase == 1:
                phase = 2
                queue.extend(aim_batch())
                continue
            if phase == 2:
                phase = 3
                queued_now = {q.id for q in queue}
                for o in ranked:
                    for sc in per_op.get(op_key(o), [])[1:]:
                        if sc.id not in queued_now:
                            queued_now.add(sc.id)
                            queue.append(sc)
                continue
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
        need = need_of(sc)
        if used + need > budget_requests:
            continue
        if sc.id in seed_set and seed_used + need > seed_cap:
            continue
        result = run_scenario(sc)
        n_ex = len(result.exchanges)
        used += n_ex
        by_op[sc.target_operation] = by_op.get(sc.target_operation, 0) + n_ex
        for idx, st in enumerate(sc.steps):
            step_ops[sc.request_id(idx)] = f"{st.method} {st.path}"
        if sc.id in seed_set:
            seed_used += n_ex
            seeds_ran.append(result.scenario)
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
    impact_set: list[Scenario] = list(seeds_ran)
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
                if sc.id not in seed_set:
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
        logins=int(getattr(run_scenario, "logins", 0)) - logins_before,
        seed_scenarios_run=len(seeds_ran),
        seed_requests=seed_used,
        requests_by_operation=by_op,
        aim=aim_info,
    )
