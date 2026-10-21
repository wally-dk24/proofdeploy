"""App index: the HTTP operations of an app plus known seed facts.

Step 1 of the lean path. An index is built from the app's own machinery
(a drf-spectacular / OpenAPI document, a Flask URL map, or the URL
resolver and serializers of a Django / Django REST Framework app) and is a pure
function of its input: the same input always gives the same JSON and the
same sha256. No model is called here.

Conventions
-----------
* Schemas are fully resolved (``$ref`` inlined, ``allOf`` merged). A
  ``$ref`` that points back into a schema already being resolved is cut
  with ``{"x-cycle": "<SchemaName>"}`` instead of recursing.
* ``field_descriptions`` maps a json path to its description. Response
  fields use bare paths (``results[].document_count``); request-body
  fields are prefixed ``request:`` (``request:name``); parameter
  descriptions are keyed ``param:<name>``.
* Operations are sorted by (path, method); params by (in, name). The
  order of ``seed_facts`` is the caller's order.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import urllib.parse
import urllib.request
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

# Methods indexed from an OpenAPI document. HEAD, OPTIONS and TRACE are
# transport plumbing, not behaviour to probe.
_OPENAPI_METHODS = ("get", "put", "post", "delete", "patch")

_MAX_FETCH_BYTES = 64 * 1024 * 1024


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------


@dataclass
class Operation:
    method: str
    path: str
    params: list[dict[str, Any]] = field(default_factory=list)
    request_schema: dict[str, Any] | None = None
    response_schema: dict[str, Any] | None = None
    summary: str = ""
    field_descriptions: dict[str, str] = field(default_factory=dict)
    source: str = ""
    # Media type of request_schema (e.g. application/json,
    # multipart/form-data); None when there is no request body.
    request_content_type: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["params"] = sorted(self.params, key=lambda p: (str(p.get("in")), str(p.get("name"))))
        d["field_descriptions"] = dict(sorted(self.field_descriptions.items()))
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Operation:
        return cls(
            method=d["method"],
            path=d["path"],
            params=list(d.get("params") or []),
            request_schema=d.get("request_schema"),
            response_schema=d.get("response_schema"),
            summary=d.get("summary") or "",
            field_descriptions=dict(d.get("field_descriptions") or {}),
            source=d.get("source") or "",
            request_content_type=d.get("request_content_type"),
        )


@dataclass
class AppIndex:
    app: str
    commit_sha: str
    operations: list[Operation] = field(default_factory=list)
    seed_facts: list[str] = field(default_factory=list)
    source_kind: str = ""

    def to_dict(self) -> dict[str, Any]:
        ops = sorted(self.operations, key=lambda o: (o.path, o.method))
        return {
            "app": self.app,
            "commit_sha": self.commit_sha,
            "operations": [o.to_dict() for o in ops],
            "seed_facts": list(self.seed_facts),
            "source_kind": self.source_kind,
        }

    def to_json(self, *, indent: int | None = 2) -> str:
        if indent is None:
            return json.dumps(
                self.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
            )
        return json.dumps(self.to_dict(), sort_keys=True, indent=indent, ensure_ascii=False)

    @classmethod
    def from_json(cls, text: str) -> AppIndex:
        d = json.loads(text)
        return cls(
            app=d["app"],
            commit_sha=d["commit_sha"],
            operations=[Operation.from_dict(o) for o in d.get("operations") or []],
            seed_facts=list(d.get("seed_facts") or []),
            source_kind=d.get("source_kind") or "",
        )

    def sha256(self) -> str:
        """Stable digest of the canonical (compact, key-sorted) JSON."""
        return hashlib.sha256(self.to_json(indent=None).encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# OpenAPI loader
# --------------------------------------------------------------------------

_SCHEMA_MAP_KEYS = ("properties", "patternProperties")
_SCHEMA_LIST_KEYS = ("oneOf", "anyOf", "prefixItems")
_SCHEMA_ONE_KEYS = ("items", "additionalProperties", "not")


def _lookup(doc: dict[str, Any], ref: str) -> Any:
    if not ref.startswith("#/"):
        raise ValueError(f"only in-document $ref is supported, got {ref!r}")
    node: Any = doc
    for raw in ref[2:].split("/"):
        key = urllib.parse.unquote(raw).replace("~1", "/").replace("~0", "~")
        if isinstance(node, dict) and key in node:
            node = node[key]
        else:
            raise ValueError(f"unresolvable $ref {ref!r}")
    return node


def _merge_schemas(parts: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for part in parts:
        for k, v in part.items():
            if k == "properties" and isinstance(v, dict):
                out.setdefault("properties", {}).update(v)
            elif k == "required" and isinstance(v, list):
                out["required"] = sorted(set(out.get("required", [])) | set(v))
            else:
                out[k] = v
    return out


def _resolve(doc: dict[str, Any], node: Any, stack: tuple[str, ...]) -> Any:
    """Inline ``$ref`` and merge ``allOf`` in a schema, with a cycle guard."""
    if not isinstance(node, dict):
        return copy.deepcopy(node)
    if "$ref" in node:
        ref = node["$ref"]
        if not isinstance(ref, str):
            raise ValueError(f"$ref must be a string, got {ref!r}")
        if ref in stack:
            return {"x-cycle": ref.rsplit("/", 1)[-1]}
        resolved = _resolve(doc, _lookup(doc, ref), (*stack, ref))
        siblings = {k: v for k, v in node.items() if k != "$ref"}
        if siblings:
            resolved = _merge_schemas([resolved, _resolve(doc, siblings, stack)])
        return resolved
    out: dict[str, Any] = {}
    for k, v in node.items():
        if k == "allOf":
            continue
        if k in _SCHEMA_MAP_KEYS and isinstance(v, dict):
            out[k] = {pk: _resolve(doc, pv, stack) for pk, pv in v.items()}
        elif k in _SCHEMA_LIST_KEYS and isinstance(v, list):
            out[k] = [_resolve(doc, i, stack) for i in v]
        elif k in _SCHEMA_ONE_KEYS and isinstance(v, dict):
            out[k] = _resolve(doc, v, stack)
        else:
            out[k] = copy.deepcopy(v)
    all_of = node.get("allOf")
    if isinstance(all_of, list):
        return _merge_schemas([_resolve(doc, p, stack) for p in all_of] + [out])
    return out


def _collect_descriptions(schema: Any, path: str, out: dict[str, str]) -> None:
    if not isinstance(schema, dict):
        return
    desc = schema.get("description")
    if isinstance(desc, str) and desc.strip() and path:
        out.setdefault(path, desc.strip())
    props = schema.get("properties")
    if isinstance(props, dict):
        for name, child in props.items():
            _collect_descriptions(child, f"{path}.{name}" if path else name, out)
    items = schema.get("items")
    if isinstance(items, dict):
        _collect_descriptions(items, f"{path}[]", out)
    addl = schema.get("additionalProperties")
    if isinstance(addl, dict):
        _collect_descriptions(addl, f"{path}.*" if path else "*", out)
    for key in ("oneOf", "anyOf"):
        for member in schema.get(key) or []:
            _collect_descriptions(member, path, out)


def _deref_object(doc: dict[str, Any], obj: Any) -> Any:
    """Follow a top-level ``$ref`` to a parameter/response/requestBody."""
    seen: set[str] = set()
    while isinstance(obj, dict) and "$ref" in obj:
        ref = obj["$ref"]
        if ref in seen:
            raise ValueError(f"cyclic $ref {ref!r}")
        seen.add(ref)
        obj = _lookup(doc, ref)
    return obj


def _pick_media(content: dict[str, Any], order: tuple[str, ...]) -> tuple[str, Any] | None:
    for want in order:
        if want in content:
            return want, content[want]
    for mt in sorted(content):
        if mt.split(";")[0].strip().endswith("json"):
            return mt, content[mt]
    if content:
        mt = sorted(content)[0]
        return mt, content[mt]
    return None


def _params(doc: dict[str, Any], path_item: dict[str, Any], op: dict[str, Any]) -> list[dict]:
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for raw in [*(path_item.get("parameters") or []), *(op.get("parameters") or [])]:
        p = _deref_object(doc, raw)
        if not isinstance(p, dict) or "name" not in p or "in" not in p:
            continue
        entry: dict[str, Any] = {
            "name": p["name"],
            "in": p["in"],
            "required": bool(p.get("required", p["in"] == "path")),
            "schema": _resolve(doc, p.get("schema") or {}, ()),
        }
        if isinstance(p.get("description"), str) and p["description"].strip():
            entry["description"] = p["description"].strip()
        merged[(p["in"], p["name"])] = entry
    return [merged[k] for k in sorted(merged)]


def _response_schema(doc: dict[str, Any], op: dict[str, Any]) -> dict[str, Any] | None:
    responses = op.get("responses") or {}
    codes = sorted(str(c) for c in responses if re.fullmatch(r"2\d\d", str(c)))
    for code in codes:
        resp = _deref_object(doc, responses.get(code, responses.get(int(code))))
        content = (resp or {}).get("content") or {}
        picked = _pick_media(content, ("application/json",))
        if picked and isinstance(picked[1], dict) and "schema" in picked[1]:
            resolved: dict[str, Any] = _resolve(doc, picked[1]["schema"], ())
            return resolved
    return None


def index_from_openapi(
    doc: dict[str, Any],
    *,
    app: str,
    commit_sha: str,
    seed_facts: Iterable[str] = (),
) -> AppIndex:
    """Build an :class:`AppIndex` from an OpenAPI 3.0/3.1 document."""
    version = str(doc.get("openapi", ""))
    if not version.startswith("3."):
        raise ValueError(f"expected an OpenAPI 3.x document, got openapi={version!r}")
    ops: list[Operation] = []
    for path, raw_item in (doc.get("paths") or {}).items():
        item = _deref_object(doc, raw_item)
        if not isinstance(item, dict):
            continue
        for method in _OPENAPI_METHODS:
            op = item.get(method)
            if not isinstance(op, dict):
                continue
            descs: dict[str, str] = {}
            params = _params(doc, item, op)
            for p in params:
                if "description" in p:
                    descs[f"param:{p['name']}"] = p["description"]
            req_schema = None
            req_type = None
            body = _deref_object(doc, op.get("requestBody"))
            if isinstance(body, dict):
                picked = _pick_media(
                    body.get("content") or {},
                    (
                        "application/json",
                        "multipart/form-data",
                        "application/x-www-form-urlencoded",
                    ),
                )
                if picked and isinstance(picked[1], dict) and "schema" in picked[1]:
                    req_type = picked[0]
                    req_schema = _resolve(doc, picked[1]["schema"], ())
                    req_descs: dict[str, str] = {}
                    _collect_descriptions(req_schema, "", req_descs)
                    descs.update({f"request:{k}": v for k, v in req_descs.items()})
            resp_schema = _response_schema(doc, op)
            _collect_descriptions(resp_schema, "", descs)
            summary = op.get("summary") or ""
            if not summary and isinstance(op.get("description"), str):
                summary = op["description"].strip().split("\n", 1)[0]
            if not summary:
                summary = op.get("operationId") or ""
            source = "openapi"
            if op.get("operationId"):
                source = f"openapi:{op['operationId']}"
            ops.append(
                Operation(
                    method=method.upper(),
                    path=path,
                    params=params,
                    request_schema=req_schema,
                    response_schema=resp_schema,
                    summary=summary,
                    field_descriptions=dict(sorted(descs.items())),
                    source=source,
                    request_content_type=req_type,
                )
            )
    ops.sort(key=lambda o: (o.path, o.method))
    return AppIndex(
        app=app,
        commit_sha=commit_sha,
        operations=ops,
        seed_facts=list(seed_facts),
        source_kind="openapi",
    )


def fetch_openapi(
    base_url: str,
    *,
    headers: dict[str, str] | None = None,
    path: str = "/api/schema/",
    timeout: float = 30,
) -> dict[str, Any]:
    """GET the app's OpenAPI document (JSON, or YAML via PyYAML)."""
    url = base_url.rstrip("/") + "/" + path.lstrip("/")
    if urllib.parse.urlparse(url).scheme not in ("http", "https"):
        raise ValueError(f"only http(s) URLs are fetched, got {url!r}")
    hdrs = {"Accept": "application/vnd.oai.openapi+json, application/json"}
    hdrs.update(headers or {})
    req = urllib.request.Request(url, headers=hdrs, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read(_MAX_FETCH_BYTES + 1)
    if len(raw) > _MAX_FETCH_BYTES:
        raise ValueError("OpenAPI document exceeds the size cap")
    text = raw.decode("utf-8")
    try:
        data = json.loads(text)
    except ValueError:
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise ValueError(f"{url} returned neither JSON nor YAML: {exc}") from exc
    if not isinstance(data, dict) or "paths" not in data:
        raise ValueError(f"{url} did not return an OpenAPI document")
    return data


# --------------------------------------------------------------------------
# Flask adapter
# --------------------------------------------------------------------------

# A python -c program. Usage: python -c "$FLASK_DUMP_SNIPPET" <module[:attr]>
# with the app's directory as cwd. Prints app.url_map rules as JSON on stdout
# (anything the import prints goes to stderr). HEAD, OPTIONS and static
# rules are excluded.
FLASK_DUMP_SNIPPET = r"""
import importlib, json, os, sys
real_out = sys.stdout
sys.stdout = sys.stderr
sys.path.insert(0, os.getcwd())
target = sys.argv[1]
modname, _, attr = target.partition(":")
obj = getattr(importlib.import_module(modname), attr or "app")
app = obj() if callable(obj) and not hasattr(obj, "url_map") else obj
rules = []
for r in app.url_map.iter_rules():
    methods = sorted(m for m in r.methods if m not in ("HEAD", "OPTIONS"))
    if r.endpoint == "static" or r.endpoint.endswith(".static") or not methods:
        continue
    rules.append({"rule": r.rule, "methods": methods, "endpoint": r.endpoint})
rules.sort(key=lambda x: (x["rule"], x["endpoint"]))
real_out.write(json.dumps(rules))
"""

_FLASK_CONVERTER = re.compile(r"<(?:(?P<conv>[a-zA-Z_]\w*)(?:\([^)]*\))?:)?(?P<name>[a-zA-Z_]\w*)>")
_FLASK_SCHEMAS: dict[str, dict[str, Any]] = {
    "int": {"type": "integer"},
    "float": {"type": "number"},
    "uuid": {"type": "string", "format": "uuid"},
}
_FLASK_SKIP_METHODS = ("HEAD", "OPTIONS")


def index_from_flask_url_map(
    rules: list[dict[str, Any]],
    *,
    app: str,
    commit_sha: str,
    seed_facts: Iterable[str] = (),
) -> AppIndex:
    """Build an :class:`AppIndex` from ``{rule, methods, endpoint}`` dumps.

    Flask declares no request or response schemas, so both are None.
    """
    ops: dict[tuple[str, str], Operation] = {}
    for r in rules:
        endpoint = str(r.get("endpoint", ""))
        if endpoint == "static" or endpoint.endswith(".static"):
            continue
        params: list[dict[str, Any]] = []

        def _sub(m: re.Match[str], params: list[dict[str, Any]] = params) -> str:
            schema = dict(_FLASK_SCHEMAS.get(m.group("conv") or "string", {"type": "string"}))
            params.append(
                {"name": m.group("name"), "in": "path", "required": True, "schema": schema}
            )
            return "{" + m.group("name") + "}"

        path = _FLASK_CONVERTER.sub(_sub, str(r["rule"]))
        params.sort(key=lambda p: p["name"])
        for method in r.get("methods") or []:
            m = str(method).upper()
            if m in _FLASK_SKIP_METHODS:
                continue
            ops[(path, m)] = Operation(
                method=m,
                path=path,
                params=copy.deepcopy(params),
                summary=endpoint,
                source=f"flask:{endpoint}",
            )
    return AppIndex(
        app=app,
        commit_sha=commit_sha,
        operations=sorted(ops.values(), key=lambda o: (o.path, o.method)),
        seed_facts=list(seed_facts),
        source_kind="flask",
    )


# --------------------------------------------------------------------------
# Django / Django REST Framework adapter
# --------------------------------------------------------------------------

# A python -c program. Usage: python -c "$DJANGO_DUMP_SNIPPET" [<settings module>]
# with the app's directory as cwd and the app's own environment (so
# ``DJANGO_SETTINGS_MODULE`` normally comes from there). It walks
# ``django.urls.get_resolver()`` recursively and prints, as JSON on stdout
# (anything the import prints goes to stderr), one record per URL pattern:
#   {"path": "/api/items/{pk}/", "view": "pkg.mod.Name", "kind": "drf" | "django",
#    "params": [{"name": "pk", "type": "int"}],
#    "methods": {"GET": {"action": "retrieve", "request": null, "content_type": null,
#                        "response": {...}}, "PUT": {...}}}
# For Django REST Framework views the serializer of each method is turned into
# a JSON schema.
# Plain Django views carry the route only. The admin site, static-serving views and
# format-suffix routes are left out (the admin site is a resolver with app_name
# ``admin``).
DJANGO_DUMP_SNIPPET = r"""
import json, os, re, sys
real_out = sys.stdout
sys.stdout = sys.stderr
sys.path.insert(0, os.getcwd())
if len(sys.argv) > 1 and sys.argv[1]:
    os.environ["DJANGO_SETTINGS_MODULE"] = sys.argv[1]
import django
django.setup()
from django.urls import URLResolver, get_resolver
try:
    from rest_framework import fields as F, relations as R, serializers as S
    from rest_framework.request import Request
    from rest_framework.test import APIRequestFactory
    from rest_framework.views import APIView
except Exception:
    APIView = None

OK_METHODS = ("get", "post", "put", "patch", "delete")
CONV = {"IntConverter": "int", "StringConverter": "str", "SlugConverter": "slug",
        "UUIDConverter": "uuid", "PathConverter": "path"}
SKIP_MODULES = ("django.contrib.admin", "django.views.static")


def rx_type(rx):
    if rx in ("[0-9]+", r"\d+"):
        return "int"
    if rx.startswith("[-a-zA-Z0-9_]"):
        return "slug"
    if "a-f" in rx and "{8}" in rx:
        return "uuid"
    if rx == ".+":
        return "path"
    return "str"


def route_to_tpl(route, converters):
    params = []

    out, last = [], 0
    for m in re.finditer(r"<(?:(\w+):)?(\w+)>", route):
        out.append(route[last:m.start()])
        name = m.group(2)
        if name == "format":
            return None
        conv = converters.get(name)
        kind = CONV.get(type(conv).__name__) if conv is not None else "str"
        if kind is None:
            kind = rx_type(getattr(conv, "regex", ""))
        params.append((name, kind))
        out.append("{" + name + "}")
        last = m.end()
    out.append(route[last:])
    return "".join(out), params


def regex_to_tpl(rx):
    out, params, i, n = [], [], 0, len(rx)
    while i < n:
        c = rx[i]
        if rx.startswith("(?P<", i):
            j = rx.index(">", i)
            name, depth, k = rx[i + 4:j], 1, j + 1
            while k < n and depth:
                ch = rx[k]
                if ch == "\\":
                    k += 2
                    continue
                if ch == "[":
                    k += 1
                    while k < n and rx[k] != "]":
                        k += 2 if rx[k] == "\\" else 1
                    k += 1
                    continue
                if ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                k += 1
            if name == "format":
                return None
            params.append((name, rx_type(rx[j + 1:k - 1])))
            out.append("{" + name + "}")
            i = k
            continue
        if c in "^$":
            i += 1
        elif rx.startswith(("\\Z", "\\A"), i):
            i += 2
        elif rx.startswith("/?", i):
            i += 2
        elif c == "\\" and i + 1 < n and not rx[i + 1].isalnum():
            out.append(rx[i + 1])
            i += 2
        elif c in "\\[](){}*+?|.":
            return None
        else:
            out.append(c)
            i += 1
    return "".join(out), params


def piece(p):
    pat = p.pattern
    if type(pat).__name__ == "RoutePattern":
        return route_to_tpl(str(pat), pat.converters)
    return regex_to_tpl(pat.regex.pattern)


def num(v):
    return float(v) if not isinstance(v, (int, float)) else v


def rel_schema(f):
    if isinstance(f, R.PrimaryKeyRelatedField):
        kind = "integer"
        model = getattr(getattr(f, "queryset", None), "model", None)
        if model is not None:
            pk = model._meta.pk
            if type(pk).__name__ == "UUIDField":
                return {"type": "string", "format": "uuid", "x-reference": True}
            if not pk.get_internal_type().endswith(("AutoField", "IntegerField")):
                kind = "string"
        return {"type": kind, "x-reference": True}
    if isinstance(f, R.HyperlinkedRelatedField):
        return {"type": "string", "format": "uri", "x-reference": True}
    return {"type": "string", "x-reference": True}


def fschema(f, mode, partial, depth):
    if isinstance(f, S.ListSerializer):
        return {"type": "array", "items": sschema(f.child, mode, partial, depth + 1)}
    if isinstance(f, S.BaseSerializer):
        return sschema(f, mode, partial, depth + 1)
    if isinstance(f, R.ManyRelatedField):
        return {"type": "array", "items": rel_schema(f.child_relation), "x-reference": True}
    if isinstance(f, R.RelatedField):
        return rel_schema(f)
    s = {}
    if isinstance(f, F.ListField):
        s = {"type": "array", "items": fschema(f.child, mode, partial, depth + 1)}
        if getattr(f, "min_length", None) is not None:
            s["minItems"] = f.min_length
        if getattr(f, "max_length", None) is not None:
            s["maxItems"] = f.max_length
        return s
    if isinstance(f, F.MultipleChoiceField):
        return {"type": "array", "items": {"enum": [k for k in f.choices]}}
    if isinstance(f, F.ChoiceField):
        keys = list(f.choices)
        kind = "integer" if keys and all(isinstance(k, int) for k in keys) else "string"
        return {"type": kind, "enum": keys if kind == "integer" else [str(k) for k in keys]}
    if isinstance(f, F.BooleanField):
        return {"type": "boolean"}
    if isinstance(f, F.IntegerField):
        s = {"type": "integer"}
    elif isinstance(f, (F.FloatField, F.DecimalField)):
        s = {"type": "number"}
    elif isinstance(f, F.EmailField):
        s = {"type": "string", "format": "email"}
    elif isinstance(f, F.URLField):
        s = {"type": "string", "format": "uri"}
    elif isinstance(f, F.UUIDField):
        s = {"type": "string", "format": "uuid"}
    elif isinstance(f, F.DateTimeField):
        s = {"type": "string", "format": "date-time"}
    elif isinstance(f, F.DateField):
        s = {"type": "string", "format": "date"}
    elif isinstance(f, F.TimeField):
        s = {"type": "string", "format": "time"}
    elif isinstance(f, F.FileField):
        s = {"type": "string", "format": "binary"}
    elif isinstance(f, F.DictField):
        child = fschema(f.child, mode, partial, depth + 1)
        return {"type": "object", "additionalProperties": child}
    elif isinstance(f, F.JSONField):
        return {}
    else:
        s = {"type": "string"}
    for attr, key in (("max_length", "maxLength"), ("min_length", "minLength")):
        if s["type"] == "string" and getattr(f, attr, None) is not None:
            s[key] = getattr(f, attr)
    for attr, key in (("max_value", "maximum"), ("min_value", "minimum")):
        if s["type"] in ("integer", "number") and getattr(f, attr, None) is not None:
            s[key] = num(getattr(f, attr))
    return s


def sschema(ser, mode, partial, depth):
    if depth > 4:
        return {"type": "object"}
    props, required = {}, []
    for name, f in ser.fields.items():
        if mode == "request" and (f.read_only or isinstance(f, F.HiddenField)):
            continue
        if mode == "response" and f.write_only:
            continue
        sch = fschema(f, mode, partial, depth)
        if f.allow_null:
            sch["nullable"] = True
        if mode == "response" and f.read_only:
            sch["readOnly"] = True
        if getattr(f, "help_text", None):
            sch["description"] = str(f.help_text)
        props[name] = sch
        if mode == "request" and f.required and not partial:
            required.append(name)
    out = {"type": "object", "properties": props}
    if required:
        out["required"] = sorted(required)
    return out


def serializer_for(cls, initkwargs, actions, action, method):
    if not hasattr(cls, "get_serializer_class"):
        return None, None
    view = None
    try:
        view = cls(**initkwargs)
        view.action = action
        view.action_map = actions or {}
        view.args, view.kwargs, view.format_kwarg = (), {}, None
        view.request = Request(APIRequestFactory().generic(method, "/"))
        return view.get_serializer_class(), view
    except Exception:
        return initkwargs.get("serializer_class") or getattr(cls, "serializer_class", None), view


def wrap_list(schema, view):
    if view is not None and getattr(view, "pagination_class", None) is not None:
        return {"type": "object", "properties": {
            "count": {"type": "integer"},
            "next": {"type": "string", "nullable": True},
            "previous": {"type": "string", "nullable": True},
            "results": {"type": "array", "items": schema}}}
    return {"type": "array", "items": schema}


def method_schemas(cls, initkwargs, actions, action, method):
    sc, view = serializer_for(cls, initkwargs, actions, action, method)
    if sc is None:
        return None, None, None
    partial = method == "PATCH" or action == "partial_update"
    try:
        req = sschema(sc(), "request", partial, 0) if method in ("POST", "PUT", "PATCH") else None
        resp = None
        if method != "DELETE":
            resp = sschema(sc(), "response", False, 0)
            if action == "list":
                resp = wrap_list(resp, view)
    except Exception:
        return None, None, None
    ctype = None
    if req is not None:
        ctype = "multipart/form-data" if '"binary"' in json.dumps(req) else "application/json"
    return req, ctype, resp


def refine_params(cls, initkwargs, params):
    model = getattr(getattr(cls, "queryset", None), "model", None)
    if model is None:
        return params
    name = initkwargs.get("lookup_url_kwarg") or getattr(cls, "lookup_url_kwarg", None) \
        or initkwargs.get("lookup_field") or getattr(cls, "lookup_field", None) or "pk"
    out = []
    for pname, kind in params:
        if pname == name and kind == "str":
            try:
                lf = getattr(cls, "lookup_field", "pk")
                fld = model._meta.pk if lf == "pk" else model._meta.get_field(lf)
                it = fld.get_internal_type()
                kind = "int" if it.endswith(("AutoField", "IntegerField")) else (
                    "uuid" if it == "UUIDField" else "str")
            except Exception:
                pass
        out.append((pname, kind))
    return out


routes = []


def emit(cb, tpl, params):
    cls = getattr(cb, "cls", None) or getattr(cb, "view_class", None)
    mod = getattr(cls, "__module__", None) or getattr(cb, "__module__", "") or ""
    if mod.startswith(SKIP_MODULES) or getattr(cb, "__module__", "").startswith(SKIP_MODULES):
        return
    actions = getattr(cb, "actions", None)
    initkwargs = dict(getattr(cb, "initkwargs", None) or getattr(cb, "view_initkwargs", None) or {})
    is_drf = APIView is not None and cls is not None and issubclass(cls, APIView)
    if actions:
        methods = {m.upper(): a for m, a in actions.items() if m.lower() in OK_METHODS}
    elif cls is not None:
        methods = {m.upper(): None for m in cls.http_method_names
                   if m in OK_METHODS and hasattr(cls, m)}
    else:
        methods = {"GET": None}
    if not methods:
        return
    name = (cls.__module__ + "." + cls.__qualname__) if cls is not None else (
        getattr(cb, "__module__", "") + "." + getattr(cb, "__qualname__", repr(cb)))
    if is_drf:
        params = refine_params(cls, initkwargs, params)
    rec = {"path": tpl, "view": name, "kind": "drf" if is_drf else "django",
           "params": [{"name": n, "type": t} for n, t in params], "methods": {}}
    for method in sorted(methods):
        entry = {"action": methods[method], "request": None, "content_type": None,
                 "response": None}
        if is_drf:
            req, ctype, resp = method_schemas(cls, initkwargs, actions, methods[method], method)
            entry.update(request=req, content_type=ctype, response=resp)
        rec["methods"][method] = entry
    routes.append(rec)


def walk(patterns, prefix, pparams):
    for p in patterns:
        if isinstance(p, URLResolver) and getattr(p, "app_name", None) == "admin":
            continue
        got = piece(p)
        if got is None:
            continue
        tpl, params = prefix + got[0], pparams + got[1]
        if isinstance(p, URLResolver):
            walk(p.url_patterns, tpl, params)
        else:
            emit(p.callback, "/" + tpl.lstrip("/"), params)


walk(get_resolver().url_patterns, "", [])
routes.sort(key=lambda r: (r["path"], r["view"]))
real_out.write(json.dumps(routes, default=str))
"""

_DJANGO_PARAM_SCHEMAS: dict[str, dict[str, Any]] = {
    "int": {"type": "integer"},
    "uuid": {"type": "string", "format": "uuid"},
    "slug": {"type": "string", "pattern": "^[-a-zA-Z0-9_]+$"},
    "path": {"type": "string"},
    "str": {"type": "string"},
}


def index_from_django_dump(
    routes: list[dict[str, Any]],
    *,
    app: str,
    commit_sha: str,
    seed_facts: Iterable[str] = (),
) -> AppIndex:
    """Build an :class:`AppIndex` from :data:`DJANGO_DUMP_SNIPPET` records.

    ``source_kind`` is ``django_drf`` when any route is a Django REST Framework
    view, else ``django``. Operations are keyed by (path, method); the first
    record wins. A plain Django view has a route and no body schema.
    """
    ops: dict[tuple[str, str], Operation] = {}
    drf = False
    for r in routes:
        path = str(r["path"])
        view = str(r.get("view", ""))
        is_drf = r.get("kind") == "drf"
        drf = drf or is_drf
        params = sorted(
            (
                {
                    "name": p["name"],
                    "in": "path",
                    "required": True,
                    "schema": dict(
                        _DJANGO_PARAM_SCHEMAS.get(str(p.get("type")), {"type": "string"})
                    ),
                }
                for p in r.get("params") or []
            ),
            key=lambda p: str(p["name"]),
        )
        for method, info in sorted((r.get("methods") or {}).items()):
            m = str(method).upper()
            if (path, m) in ops:
                continue
            info = info or {}
            req = info.get("request")
            resp = info.get("response")
            descs: dict[str, str] = {}
            if isinstance(req, dict):
                req_descs: dict[str, str] = {}
                _collect_descriptions(req, "", req_descs)
                descs.update({f"request:{k}": v for k, v in req_descs.items()})
            _collect_descriptions(resp, "", descs)
            action = info.get("action")
            ops[(path, m)] = Operation(
                method=m,
                path=path,
                params=copy.deepcopy(params),
                request_schema=req if isinstance(req, dict) else None,
                response_schema=resp if isinstance(resp, dict) else None,
                summary=f"{view}.{action}" if action else view,
                field_descriptions=dict(sorted(descs.items())),
                source=f"{'django_drf' if is_drf else 'django'}:{view}",
                request_content_type=(info.get("content_type") if isinstance(req, dict) else None),
            )
    return AppIndex(
        app=app,
        commit_sha=commit_sha,
        operations=sorted(ops.values(), key=lambda o: (o.path, o.method)),
        seed_facts=list(seed_facts),
        source_kind="django_drf" if drf else "django",
    )


# --------------------------------------------------------------------------
# Seed facts
# --------------------------------------------------------------------------

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


def seed_facts_from_contract(contract_path: str | Path) -> list[str]:
    """The fixture's ``seed_note`` split into sentences (read-only)."""
    from proofdeploy.yml import load_contract

    note = (load_contract(contract_path).fixture or {}).get("seed_note") or ""
    return [s.strip() for s in _SENTENCE_SPLIT.split(str(note).strip()) if s.strip()]


# --------------------------------------------------------------------------
# Cache
# --------------------------------------------------------------------------


def _safe(part: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", part).strip("._") or "_"


class IndexCache:
    """Directory cache keyed by (app, commit_sha, source_kind)."""

    def __init__(self, directory: str | Path) -> None:
        self.dir = Path(directory)

    def path_for(self, app: str, commit_sha: str, source_kind: str) -> Path:
        key = json.dumps([app, commit_sha, source_kind])
        tag = hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]
        name = f"{_safe(app)}__{_safe(commit_sha)[:40]}__{_safe(source_kind)}__{tag}.json"
        return self.dir / name

    def get(self, app: str, commit_sha: str, source_kind: str) -> AppIndex | None:
        p = self.path_for(app, commit_sha, source_kind)
        try:
            return AppIndex.from_json(p.read_text(encoding="utf-8"))
        except (OSError, ValueError, KeyError):
            return None

    def put(self, index: AppIndex) -> Path:
        p = self.path_for(index.app, index.commit_sha, index.source_kind)
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(index.to_json(), encoding="utf-8")
        tmp.replace(p)
        return p
