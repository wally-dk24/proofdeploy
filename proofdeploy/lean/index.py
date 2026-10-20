"""App index: the HTTP operations of an app plus known seed facts.

Step 1 of the lean path. An index is built from the app's own machinery
(a drf-spectacular / OpenAPI document, or a Flask URL map) and is a pure
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
