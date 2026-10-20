"""Lean P1 (D258): the app index. No model calls, no network beyond loopback."""

from __future__ import annotations

import copy
import json
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
import yaml

from proofdeploy.lean.index import (
    FLASK_DUMP_SNIPPET,
    AppIndex,
    IndexCache,
    fetch_openapi,
    index_from_flask_url_map,
    index_from_openapi,
    seed_facts_from_contract,
)

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "lean" / "spectacular_mini.json"
GOLDEN_DIR = ROOT / "conformance/golden/python-flask-sqlite/revisions/intermediate"
GOLDEN_CONTRACT = ROOT / "readiness/contracts/golden__python-flask-sqlite.yml"

GOLDEN_ROUTES = [
    ("GET", "/health"),
    ("GET", "/notes"),
    ("GET", "/notes/{note_id}"),
    ("POST", "/login"),
    ("POST", "/notes"),
    ("POST", "/register"),
]


def _doc() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _index(doc: dict | None = None, sha: str = "abc123") -> AppIndex:
    return index_from_openapi(
        doc or _doc(), app="mini/docs", commit_sha=sha, seed_facts=["tag 1 exists"]
    )


def _op(idx: AppIndex, method: str, path: str):
    (op,) = [o for o in idx.operations if o.method == method and o.path == path]
    return op


# ---------------------------------------------------------------- OpenAPI


def test_operations_listed_sorted():
    idx = _index()
    keys = [(o.path, o.method) for o in idx.operations]
    assert keys == sorted(keys)
    assert ("/api/tags/", "GET") in keys and ("/api/tags/", "POST") in keys
    assert idx.source_kind == "openapi"
    assert idx.seed_facts == ["tag 1 exists"]


def test_paginated_list_resolves_ref_and_flattens_descriptions():
    op = _op(_index(), "GET", "/api/tags/")
    props = op.response_schema["properties"]
    assert set(props) == {"count", "next", "previous", "results"}
    # $ref inlined, not left as a pointer
    assert props["results"]["items"]["properties"]["document_count"]["type"] == "integer"
    assert "$ref" not in json.dumps(op.response_schema)
    assert (
        op.field_descriptions["results[].document_count"] == "Number of documents carrying this tag"
    )
    assert op.field_descriptions["results[].name"] == "Display name of the tag"
    assert op.field_descriptions["param:page"].startswith("A page number")
    assert [p["name"] for p in op.params] == ["name__icontains", "page", "page_size"]
    assert all(p["in"] == "query" and p["required"] is False for p in op.params)


def test_ref_cycle_is_cut_not_recursed():
    op = _op(_index(), "GET", "/api/nodes/{id}/")
    children = op.response_schema["properties"]["children"]
    assert children["items"] == {"x-cycle": "Node"}
    assert op.field_descriptions["name"] == "Node name"


def test_allof_merges_properties_required_and_sibling_description():
    op = _op(_index(), "GET", "/api/tags/{id}/")
    schema = op.response_schema
    assert {"id", "name", "color", "document_count", "owner"} <= set(schema["properties"])
    assert schema["required"] == ["id", "name", "owner"]
    assert "allOf" not in json.dumps(schema)
    owner = schema["properties"]["owner"]
    assert owner["properties"]["username"]["type"] == "string"
    assert owner["nullable"] is True
    assert op.field_descriptions["owner"] == "The user who owns the tag"
    assert op.field_descriptions["owner.username"] == "Login name"


def test_request_body_prefers_json_over_multipart():
    op = _op(_index(), "POST", "/api/tags/")
    assert op.request_content_type == "application/json"
    assert op.request_schema["required"] == ["name"]
    assert op.field_descriptions["request:name"] == "Display name of the tag"
    assert op.response_schema["properties"]["id"]["type"] == "integer"  # 201 picked


def test_multipart_body_and_no_response_body():
    op = _op(_index(), "POST", "/api/documents/post_document/")
    assert op.request_content_type == "multipart/form-data"
    assert op.request_schema["properties"]["document"]["format"] == "binary"
    assert op.field_descriptions["request:document"] == "The file to ingest"
    assert op.response_schema is None
    assert op.summary == "Upload a document"


def test_path_params_from_path_item_and_operation():
    idx = _index()
    tag = _op(idx, "GET", "/api/tags/{id}/")
    assert tag.params == [
        {
            "name": "id",
            "in": "path",
            "required": True,
            "schema": {"type": "integer"},
            "description": "A unique integer value identifying this tag.",
        }
    ]
    node = _op(idx, "GET", "/api/nodes/{id}/")
    assert node.params[0]["in"] == "path" and node.params[0]["required"] is True


def test_non_openapi3_rejected():
    with pytest.raises(ValueError, match="OpenAPI 3"):
        index_from_openapi({"swagger": "2.0", "paths": {}}, app="a", commit_sha="c")


def test_unresolvable_ref_raises():
    doc = _doc()
    doc["components"]["schemas"]["Node"]["properties"]["name"] = {
        "$ref": "#/components/schemas/Missing"
    }
    with pytest.raises(ValueError, match="unresolvable"):
        _index(doc)


# ---------------------------------------------------------------- determinism


def test_same_input_same_sha_and_roundtrip():
    a, b = _index(), _index()
    assert a.sha256() == b.sha256()
    assert a.to_json() == b.to_json()
    again = AppIndex.from_json(a.to_json())
    assert again == a and again.sha256() == a.sha256()


def test_sha_independent_of_input_key_order_but_sensitive_to_content():
    doc = _doc()
    shuffled = copy.deepcopy(doc)
    shuffled["paths"] = dict(reversed(list(doc["paths"].items())))
    shuffled["components"]["schemas"] = dict(reversed(list(doc["components"]["schemas"].items())))
    assert _index(shuffled).sha256() == _index(doc).sha256()
    assert _index(sha="other").sha256() != _index().sha256()
    changed = copy.deepcopy(doc)
    changed["components"]["schemas"]["Tag"]["properties"]["name"]["description"] = "x"
    assert _index(changed).sha256() != _index().sha256()


# ---------------------------------------------------------------- fetch


@pytest.fixture
def server(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    seen: dict = {}
    state = {"body": b"", "ctype": "application/json"}

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            seen["path"] = self.path
            seen["accept"] = self.headers.get("Accept")
            seen["auth"] = self.headers.get("Authorization")
            self.send_response(200)
            self.send_header("Content-Type", state["ctype"])
            self.end_headers()
            self.wfile.write(state["body"])

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", state, seen
    srv.shutdown()
    srv.server_close()


def test_fetch_openapi_json(server):
    base, state, seen = server
    state["body"] = FIXTURE.read_bytes()
    doc = fetch_openapi(base + "/", headers={"Authorization": "Token t0k"})
    assert doc == _doc()
    assert seen["path"] == "/api/schema/"
    assert seen["accept"] == "application/vnd.oai.openapi+json, application/json"
    assert seen["auth"] == "Token t0k"


def test_fetch_openapi_yaml_custom_path(server):
    base, state, seen = server
    state["body"] = yaml.safe_dump(_doc()).encode()
    state["ctype"] = "application/vnd.oai.openapi"
    doc = fetch_openapi(base, path="schema.yml")
    assert seen["path"] == "/schema.yml"
    assert _index(doc).sha256() == _index().sha256()


def test_fetch_openapi_rejects_non_openapi_and_bad_scheme(server):
    base, state, _ = server
    state["body"] = b'{"hello": 1}'
    with pytest.raises(ValueError, match="did not return"):
        fetch_openapi(base)
    with pytest.raises(ValueError, match="http"):
        fetch_openapi("file:///etc")


# ---------------------------------------------------------------- Flask adapter


def test_flask_adapter_synthetic_filters_and_converters():
    rules = [
        {
            "rule": "/static/<path:filename>",
            "methods": ["GET", "HEAD", "OPTIONS"],
            "endpoint": "static",
        },
        {"rule": "/a/<int:id>", "methods": ["GET", "HEAD", "OPTIONS", "PUT"], "endpoint": "a"},
        {
            "rule": "/b/<name>/<uuid:u>/<float:f>",
            "methods": ["OPTIONS", "POST"],
            "endpoint": "bp.b",
        },
        {"rule": "/only-head", "methods": ["HEAD", "OPTIONS"], "endpoint": "h"},
        {"rule": "/bp/static/<p>", "methods": ["GET"], "endpoint": "bp.static"},
    ]
    idx = index_from_flask_url_map(rules, app="x/y", commit_sha="c", seed_facts=["s"])
    assert [(o.method, o.path) for o in idx.operations] == [
        ("GET", "/a/{id}"),
        ("PUT", "/a/{id}"),
        ("POST", "/b/{name}/{u}/{f}"),
    ]
    a = idx.operations[0]
    assert a.params == [
        {"name": "id", "in": "path", "required": True, "schema": {"type": "integer"}}
    ]
    b = idx.operations[2]
    assert {p["name"]: p["schema"] for p in b.params} == {
        "f": {"type": "number"},
        "name": {"type": "string"},
        "u": {"type": "string", "format": "uuid"},
    }
    assert a.request_schema is None and a.response_schema is None
    assert idx.source_kind == "flask" and idx.seed_facts == ["s"]


def _python_with_flask(tmp_path_factory) -> str:
    """An interpreter that can import the golden app's Flask.

    Builds a venv from the golden requirements with --require-hashes (the
    same hash-pinned install the golden itself uses).
    """
    venv = tmp_path_factory.mktemp("flaskvenv") / "v"
    subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True, capture_output=True)
    py = str(venv / "bin" / "python")
    r = subprocess.run(
        [
            py,
            "-m",
            "pip",
            "install",
            "-q",
            "--require-hashes",
            "-r",
            str(GOLDEN_DIR / "requirements.txt"),
        ],
        capture_output=True,
        text=True,
        timeout=300,
    )
    if r.returncode != 0:
        shutil.rmtree(venv, ignore_errors=True)
        pytest.skip(f"cannot install golden requirements (no network?): {r.stderr[-300:]}")
    return py


@pytest.fixture(scope="module")
def golden_dump(tmp_path_factory):
    py = _python_with_flask(tmp_path_factory)
    r = subprocess.run(
        [py, "-c", FLASK_DUMP_SNIPPET, "app:app"],
        cwd=GOLDEN_DIR,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_flask_dump_snippet_excludes_head_options_static(golden_dump):
    methods = {m for r in golden_dump for m in r["methods"]}
    assert methods == {"GET", "POST"}
    assert not any(r["endpoint"] == "static" or "/static" in r["rule"] for r in golden_dump)


def test_flask_adapter_on_golden_app(golden_dump):
    idx = index_from_flask_url_map(golden_dump, app="golden/python-flask-sqlite", commit_sha="g1")
    assert sorted((o.method, o.path) for o in idx.operations) == GOLDEN_ROUTES
    note = _op(idx, "GET", "/notes/{note_id}")
    assert note.params == [
        {"name": "note_id", "in": "path", "required": True, "schema": {"type": "integer"}}
    ]
    assert all(o.request_schema is None and o.response_schema is None for o in idx.operations)
    assert (
        idx.sha256()
        == index_from_flask_url_map(
            list(reversed(golden_dump)), app="golden/python-flask-sqlite", commit_sha="g1"
        ).sha256()
    )


# ---------------------------------------------------------------- seed facts + cache


def test_seed_facts_from_golden_contract():
    facts = seed_facts_from_contract(GOLDEN_CONTRACT)
    assert any("id 1 (done=false)" in f for f in facts)
    assert any("id 2 (done=true)" in f for f in facts)


def test_seed_facts_split_into_sentences(tmp_path):
    p = tmp_path / "c.yml"
    p.write_text(
        'build: "true"\nstart: "true"\nreadiness: "/h"\nfixture:\n'
        '  repo_name: r\n  seed_note: "One thing. Two things! Three?"\n'
        "  auth_mechanism: m\n  auth_scope: s\n  flags_note: f\n",
        encoding="utf-8",
    )
    assert seed_facts_from_contract(p) == ["One thing.", "Two things!", "Three?"]


def test_cache_round_trip(tmp_path):
    cache = IndexCache(tmp_path / "c")
    idx = _index()
    assert cache.get(idx.app, idx.commit_sha, idx.source_kind) is None
    path = cache.put(idx)
    assert path.parent == tmp_path / "c" and path.exists()
    got = cache.get(idx.app, idx.commit_sha, idx.source_kind)
    assert got == idx and got.sha256() == idx.sha256()
    # key components are all significant
    assert cache.get(idx.app, "other", idx.source_kind) is None
    assert cache.get(idx.app, idx.commit_sha, "flask") is None
    assert cache.get("other/app", idx.commit_sha, idx.source_kind) is None
    path.write_text("{not json", encoding="utf-8")
    assert cache.get(idx.app, idx.commit_sha, idx.source_kind) is None
