"""WO-P3 (D258): lean step 3, request generation and the coverage-guided loop.

Unit tests need no network beyond loopback (a fake app on ``http.server`` in a
thread). The end-to-end tests run the golden Flask app under coverage.py with
the injected ``pdcov`` startup, index it with the Flask adapter, and let
``cover_change`` find the requests that execute the changed lines. They need
network once (hash-pinned pip install of the golden requirements) and skip
ONLY when PyPI is unreachable. No model is called anywhere.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "conformance"))
import kit

from proofdeploy.lean import coverage as lc
from proofdeploy.lean import hostrun
from proofdeploy.lean.coverage import ChangedLines
from proofdeploy.lean.generate import (
    ScenarioGenerator,
    ScenarioRunner,
    body_variants,
    cover_change,
    error_fields,
    executable_view,
    generate_scenarios,
    mine_constants,
    parse_diff_text,
    rank_operations,
    repair_step,
    request_variants,
    seed_ids,
    seeded_records,
    value_for,
)
from proofdeploy.lean.http import (
    Exchange,
    HttpClient,
    Scenario,
    Step,
    UnsupportedAuth,
    auth_from_contract,
    json_path_get,
    presentation_scheme,
)
from proofdeploy.lean.index import (
    AppIndex,
    Operation,
    index_from_flask_url_map,
    index_from_openapi,
    seed_facts_from_contract,
)

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "lean" / "spectacular_mini.json"
GOLDEN_CONTRACT = ROOT / "readiness/contracts/golden__python-flask-sqlite.yml"


def _mini_index(seed_facts: list[str] | None = None) -> AppIndex:
    doc = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return index_from_openapi(doc, app="mini", commit_sha="0" * 40, seed_facts=seed_facts or [])


# --------------------------------------------------------------------------
# data shapes
# --------------------------------------------------------------------------


def test_scenario_id_is_a_stable_content_hash() -> None:
    s1 = Step("GET", "/a/{id}", path_params={"id": 1})
    s2 = Step("GET", "/a/{id}", path_params={"id": 1})
    a = Scenario([s1], "GET /a/{id}")
    b = Scenario([s2], "GET /a/{id}")
    c = Scenario([Step("GET", "/a/{id}", path_params={"id": 2})], "GET /a/{id}")
    assert a.id == b.id and len(a.id) == 12
    assert a.id != c.id
    assert a.request_id(3) == f"{a.id}:3"
    # JSON round trip keeps the id
    again = Scenario.from_dict(json.loads(json.dumps(a.to_dict())))
    assert again.id == a.id and again.steps == a.steps


def test_exchange_is_json_serializable() -> None:
    ex = Exchange(
        "abc", 1, {"method": "GET", "url_path": "/x", "body": None}, 200, {}, {"a": 1}, 1.5
    )
    d = json.loads(json.dumps(ex.to_dict()))
    assert d["request_id"] == "abc:1" and d["status"] == 200 and d["attempt"] == 0


def test_json_path_get() -> None:
    doc = {"a": {"b": [{"c": 5}]}, "token": "t"}
    assert json_path_get(doc, "a.b[0].c") == (True, 5)
    assert json_path_get(doc, "$.token") == (True, "t")
    assert json_path_get(doc, "a.x")[0] is False


# --------------------------------------------------------------------------
# body synthesis from schemas
# --------------------------------------------------------------------------

SCHEMA = {
    "type": "object",
    "required": ["title", "pages", "active", "kind", "when", "mail", "site"],
    "properties": {
        "title": {"type": "string"},
        "pages": {"type": "integer"},
        "active": {"type": "boolean"},
        "kind": {"type": "string", "enum": ["draft", "final"]},
        "when": {"type": "string", "format": "date-time"},
        "mail": {"type": "string", "format": "email"},
        "site": {"type": "string", "format": "uri"},
        "note": {"type": "string"},
        "id": {"type": "integer", "readOnly": True},
    },
}


def test_required_fields_get_type_correct_values() -> None:
    label, base = body_variants(SCHEMA)[0]
    assert label == "base"
    assert base == {
        "title": "pd-title",
        "pages": 1,
        "active": True,
        "kind": "draft",
        "when": "2024-01-01T00:00:00Z",
        "mail": "pd@example.com",
        "site": "https://example.com/pd",
    }


def test_optional_fields_one_variant_and_booleans_both_ways() -> None:
    variants = dict(body_variants(SCHEMA))
    assert "note" not in variants["base"]
    assert variants["full"]["note"] == "pd-note"
    assert "id" not in variants["full"]  # readOnly is never sent
    assert variants["bool:active=false"]["active"] is False
    assert variants["bool:active=false"]["note"] == "pd-note"


def test_value_for_respects_bounds_and_nested_types() -> None:
    assert value_for({"type": "integer", "minimum": 5}) == 5
    assert value_for({"type": "integer", "maximum": 0}) == 0
    assert value_for({"type": "string", "minLength": 12, "maxLength": 20}, "n") == "pd-n" + "x" * 8
    assert value_for({"type": "string", "maxLength": 3}, "name") == "pd-"
    assert value_for({"type": "array", "items": {"type": "integer"}}) == [1]
    assert value_for({"type": ["string", "null"]}, "q") == "pd-q"
    assert value_for({"anyOf": [{"type": "null"}, {"type": "boolean"}]}) is True
    nested = {"type": "object", "required": ["a"], "properties": {"a": {"type": "integer"}}}
    assert value_for(nested) == {"a": 1}
    assert value_for({"x-cycle": "Node"}) is None


def test_spectacular_fixture_bodies_follow_the_request_schemas() -> None:
    index = _mini_index()
    by_key = {(o.method, o.path): o for o in index.operations}
    tags = by_key[("POST", "/api/tags/")]
    variants = request_variants(tags)
    assert variants[0][0] == "base"
    assert variants[0][1] == {"name": "pd-name"}
    assert any(b == {"name": "pd-name", "color": "pd-color"} for _, b, _ in variants)
    doc = by_key[("POST", "/api/documents/post_document/")]
    (_, body, _) = request_variants(doc)[0]
    assert body == {"document": "pd-file"}
    for op in index.operations:  # every body is JSON-serializable
        json.dumps(request_variants(op))


# --------------------------------------------------------------------------
# id chaining (producer -> consumer) and seed ids
# --------------------------------------------------------------------------


def test_id_chaining_runs_the_collection_create_first() -> None:
    index = _mini_index()
    scenarios = ScenarioGenerator(index).for_operation(
        next(o for o in index.operations if o.path == "/api/tags/{id}/")
    )
    first = scenarios[0]
    assert [s.method for s in first.steps] == ["POST", "GET"]
    producer, target = first.steps
    assert producer.path == "/api/tags/" and producer.body == {"name": "pd-name"}
    assert producer.capture == {"id": "id"}
    assert target.path_params == {"id": "${id}"}
    assert first.target_operation == "GET /api/tags/{id}/"


def test_chaining_matches_other_param_names_and_nested_collections() -> None:
    ops = [
        Operation("POST", "/notes", response_schema={"properties": {"id": {}}}),
        Operation(
            "GET",
            "/notes/{note_id}",
            params=[
                {"name": "note_id", "in": "path", "required": True, "schema": {"type": "integer"}}
            ],
        ),
        Operation(
            "POST",
            "/notes/{note_id}/tags",
            params=[
                {"name": "note_id", "in": "path", "required": True, "schema": {"type": "integer"}}
            ],
            response_schema={"properties": {"pk": {}}},
        ),
        Operation(
            "GET",
            "/notes/{note_id}/tags/{pk}",
            params=[
                {"name": "note_id", "in": "path", "required": True, "schema": {"type": "integer"}},
                {"name": "pk", "in": "path", "required": True, "schema": {"type": "integer"}},
            ],
        ),
    ]
    gen = ScenarioGenerator(AppIndex("a", "c", ops))
    sc = gen.for_operation(ops[3])[0]
    assert [(s.method, s.path) for s in sc.steps] == [
        ("POST", "/notes"),
        ("POST", "/notes/{note_id}/tags"),
        ("GET", "/notes/{note_id}/tags/{pk}"),
    ]
    assert sc.steps[1].path_params == {"note_id": "${note_id}"}
    assert sc.steps[1].capture == {"pk": "pk"}
    assert sc.steps[2].path_params == {"note_id": "${note_id}", "pk": "${pk}"}


def test_seed_ids_are_parsed_and_used_as_variants() -> None:
    facts = ["seed user owns two notes after seed: id 1 (done=false) and id 2 (done=true)"]
    assert seed_ids(facts) == [1, 2]
    assert seed_ids(["ids 3, 4 and 5 exist", "no ids here"]) == [3, 4, 5]
    op = Operation(
        "GET",
        "/notes/{note_id}",
        params=[{"name": "note_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
    )
    gen = ScenarioGenerator(AppIndex("a", "c", [op], seed_facts=facts))
    scs = gen.for_operation(op)
    assert [s.steps[-1].path_params["note_id"] for s in scs] == [
        1,
        2,
        999999,
    ]  # chained == seed 1 without a producer


# --------------------------------------------------------------------------
# seeded records first (WO-R3-G, register row R2)
# --------------------------------------------------------------------------

_GOLDEN_RULES = [
    {"rule": "/health", "methods": ["GET"], "endpoint": "health"},
    {"rule": "/notes", "methods": ["GET", "POST"], "endpoint": "notes"},
    {"rule": "/notes/<int:note_id>", "methods": ["GET"], "endpoint": "get_note"},
]


def _golden_index(facts: list[str]) -> AppIndex:
    return index_from_flask_url_map(_GOLDEN_RULES, app="a", commit_sha="c", seed_facts=facts)


def _target_ids(scs: list[Scenario]) -> list[tuple[str, Any]]:
    return [(s.target_operation, list(s.steps[-1].path_params.values())) for s in scs]


def test_seeded_records_parse_the_named_patterns_mechanically() -> None:
    colls = ["notes", "items"]
    assert seeded_records(["id 1 and id=2", "#3", "ids 4, 5 and 6"], colls) == [
        (None, n) for n in (1, 2, 3, 4, 5, 6)
    ]
    assert seeded_records(["note 1", "note #2", "items 3 and 4", "item id 5"], colls) == [
        ("notes", 1),
        ("notes", 2),
        ("items", 3),
        ("items", 4),
        ("items", 5),
    ]
    assert seeded_records(["widget 5 exists", "gadget id 6", "step 7"], colls) == []
    assert seeded_records(["no digits here", "id abc"], colls) == []


def test_r2_golden_seed_fact_makes_the_first_scenarios_the_seeded_reads() -> None:
    """R2: with the golden seed fact the first scenarios are GET /notes/1 and GET /notes/2."""
    facts = seed_facts_from_contract(GOLDEN_CONTRACT)
    assert seeded_records(facts, ["notes"]) == [(None, 1), (None, 2)]
    scs = generate_scenarios(_golden_index(facts))
    head = [(s.steps[-1].method, s.steps[-1].path, s.steps[-1].path_params) for s in scs[:2]]
    assert head == [
        ("GET", "/notes/{note_id}", {"note_id": 1}),
        ("GET", "/notes/{note_id}", {"note_id": 2}),
    ]
    # with a login spec every seeded read is login + GET, still first
    auth = auth_from_contract(GOLDEN_CONTRACT)
    sa = generate_scenarios(_golden_index(facts), auth=auth)
    assert [st.path for st in sa[0].steps] == ["/login", "/notes/{note_id}"]
    assert sa[0].steps[-1].path_params == {"note_id": 1} and sa[0].steps[-1].auth is True
    assert sa[1].steps[-1].path_params == {"note_id": 2}


def test_no_seed_ids_leaves_the_scenario_list_unchanged() -> None:
    legacy = ScenarioGenerator(_golden_index([]))
    per_op = [legacy.for_operation(op) for op in rank_operations(legacy.index.operations, "")]
    expected: list[Scenario] = []
    for sc in [*[lst[0] for lst in per_op if lst], *[s for lst in per_op for s in lst[1:]]]:
        if sc.id not in {e.id for e in expected}:
            expected.append(sc)
    for facts in ([], ["seed user owns two notes"], ["done=false and done=true"]):
        got = generate_scenarios(_golden_index(facts))
        assert [s.id for s in got] == [s.id for s in expected]
        assert [s.to_dict() for s in got] == [s.to_dict() for s in expected]


def test_a_fact_for_a_collection_not_in_the_index_is_ignored() -> None:
    rules = [
        *_GOLDEN_RULES,
        {"rule": "/items/<int:item_id>", "methods": ["GET"], "endpoint": "get_item"},
    ]
    facts = ["widget 5 exists", "note 2 exists"]
    index = index_from_flask_url_map(rules, app="a", commit_sha="c", seed_facts=facts)
    scs = generate_scenarios(index)
    assert _target_ids(scs[:1]) == [("GET /notes/{note_id}", [2])]
    assert not any(5 in s.steps[-1].path_params.values() for s in scs)
    # "note 2" is typed: no items read uses id 2
    assert not any(s.steps[-1].path_params == {"item_id": 2} for s in scs)


def test_seeded_reads_are_counted_in_the_request_budget_and_join_the_impact_set() -> None:
    from proofdeploy.lean.generate import RunResult

    facts = ["notes: id 1 and id 2"]
    index = _golden_index(facts)
    auth = auth_from_contract(GOLDEN_CONTRACT)
    session = _FakeSession({})
    ran: list[Scenario] = []

    def run(sc: Scenario) -> Any:
        ran.append(sc)
        ex = [
            Exchange(
                sc.id, i, {"method": s.method, "url_path": s.path, "body": None}, 200, {}, {}, 0
            )
            for i, s in enumerate(sc.steps)
        ]
        return RunResult(ex, sc)

    changed = ChangedLines.from_diff(DIFF_GET_NOTE)
    res = cover_change(index, changed, run, session, 4, diff_text=DIFF_GET_NOTE, auth=auth)
    # login + GET = 2 requests each: exactly the first two seeded reads fit in 4
    assert res.requests_used == 4 and res.scenarios_run == 2
    assert [s.steps[-1].path_params for s in ran] == [{"note_id": 1}, {"note_id": 2}]
    # a seeded read that hits a changed line is in the impact set like any scenario
    hit = _FakeSession({"GET /notes/{note_id}": ("app.py", 99)})

    def run_hit(sc: Scenario) -> Any:
        hit.record(sc)
        return run(sc)

    res2 = cover_change(index, changed, run_hit, hit, 50, diff_text=DIFF_GET_NOTE, auth=auth)
    assert res2.covered and res2.scenarios_run == 1
    assert [s.steps[-1].path_params for s in res2.impact_set] == [{"note_id": 1}]


# --------------------------------------------------------------------------
# constant mining
# --------------------------------------------------------------------------

DIFF_PAGES = """\
diff --git a/lib.py b/lib.py
--- a/lib.py
+++ b/lib.py
@@ -10,4 +10,4 @@ def validate(pages, label):
     x = 1
-    if pages > 50:
+    if pages > 100:
         return label == "long"
     return None
"""


def test_constants_are_mined_from_added_and_removed_lines() -> None:
    got = mine_constants(DIFF_PAGES)
    ints = [c.value for c in got if c.kind == "int"]
    strs = [c.value for c in got if c.kind == "str"]
    assert ints == [100, 50]  # added first, then removed
    assert "long" not in strs  # a context line is not part of the change
    pages_const = next(c for c in got if c.value == 100)
    assert "page" in pages_const.names


def test_integer_constant_gives_boundary_variants_for_the_matching_field() -> None:
    schema = {
        "type": "object",
        "required": ["pages", "copies"],
        "properties": {"pages": {"type": "integer"}, "copies": {"type": "integer"}},
    }
    variants = body_variants(schema, mine_constants(DIFF_PAGES))
    pages_values = [b["pages"] for lbl, b in variants if lbl.startswith("mined:pages=")]
    assert pages_values == [99, 100, 101, 49, 50, 51]
    assert not any(lbl.startswith("mined:copies=") for lbl, _ in variants)  # name match wins
    # no diff -> no mined variants
    assert [lbl for lbl, _ in body_variants(schema)] == ["base"]


def test_mined_values_reach_query_params_and_scenarios() -> None:
    op = Operation(
        "GET",
        "/docs",
        params=[{"name": "pages", "in": "query", "required": False, "schema": {"type": "integer"}}],
    )
    gen = ScenarioGenerator(AppIndex("a", "c", [op]), diff_text=DIFF_PAGES)
    queries = [s.steps[-1].query for s in gen.for_operation(op)]
    assert {"pages": 99} in queries and {"pages": 100} in queries and {"pages": 101} in queries


# --------------------------------------------------------------------------
# diff text, executable view, ranking
# --------------------------------------------------------------------------

DIFF_GET_NOTE = """\
diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -98,5 +98,6 @@ def get_note(note_id):
         "title": row["title"],
-        "done": row['done'] == 1,
+        "done": row['done'] != 1,
+
     })
"""


def test_parse_diff_text_reads_texts_and_contexts() -> None:
    dt = parse_diff_text(DIFF_GET_NOTE)
    assert dt.contexts == ["def get_note(note_id):"]
    assert dt.text_of("app.py", "added", 99) == """        "done": row['done'] != 1,"""
    assert dt.text_of("app.py", "removed", 99) is not None


def test_executable_view_drops_blank_and_comment_lines() -> None:
    changed = ChangedLines.from_diff(DIFF_GET_NOTE)
    dt = parse_diff_text(DIFF_GET_NOTE)
    assert changed["app.py"]["added"] == {99, 100}
    assert executable_view(changed, dt, "new")["app.py"] == {"added": {99}, "removed": set()}
    assert executable_view(changed, dt, "old")["app.py"] == {"added": set(), "removed": {99}}
    with pytest.raises(ValueError):
        executable_view(changed, dt, "sideways")


def test_ranking_puts_operations_named_in_the_change_first() -> None:
    rules = [
        {"rule": "/health", "methods": ["GET"], "endpoint": "health"},
        {"rule": "/login", "methods": ["POST"], "endpoint": "login"},
        {"rule": "/notes", "methods": ["POST"], "endpoint": "create_note"},
        {"rule": "/notes/<int:note_id>", "methods": ["GET"], "endpoint": "get_note"},
    ]
    index = index_from_flask_url_map(rules, app="a", commit_sha="c")
    ranked = rank_operations(index.operations, DIFF_GET_NOTE)
    assert (ranked[0].method, ranked[0].path) == ("GET", "/notes/{note_id}")
    plain = rank_operations(index.operations, "")
    assert [o.path for o in plain] == sorted(o.path for o in plain)


# --------------------------------------------------------------------------
# contract auth
# --------------------------------------------------------------------------


def test_auth_from_contract_login_mode_and_scheme() -> None:
    auth = auth_from_contract(GOLDEN_CONTRACT)
    assert auth is not None
    login = auth.login
    assert (login.method, login.path) == ("POST", "/login")
    assert login.body == {"username": "seeduser", "password": "seedpass"}
    assert login.capture == {"token": "token"}
    assert auth.scheme == "Bearer" and auth.header("t") == "Bearer t"


def test_scheme_agrees_with_the_readiness_rule() -> None:
    import readiness.auth_proof as ap
    from proofdeploy.yml import load_contract

    checked = 0
    for path in sorted((ROOT / "readiness/contracts").glob("*.yml")):
        contract = load_contract(path)
        try:
            expected = ap.presentation_scheme(contract)
        except ValueError:
            continue  # the readiness rule needs its fixed sentence; ours has a fallback
        assert presentation_scheme(contract.fixture) == expected, path.name
        checked += 1
    assert checked > 0
    assert presentation_scheme({"auth_mechanism": "no scheme named"}) == "Bearer"
    assert presentation_scheme({"auth_mechanism": "x in the form 'Ghost <token>' y"}) == "Ghost"


def test_other_auth_modes_are_refused_not_ignored(tmp_path: Path) -> None:
    class C:
        auth = {"mode": "register", "path": "/r"}
        fixture: dict[str, str] = {}

    with pytest.raises(UnsupportedAuth):
        auth_from_contract(C())

    class NoAuth:
        auth = None

    assert auth_from_contract(NoAuth()) is None


def test_every_scenario_starts_with_login_when_the_contract_has_auth() -> None:
    auth = auth_from_contract(GOLDEN_CONTRACT)
    index = index_from_flask_url_map(
        [{"rule": "/notes/<int:note_id>", "methods": ["GET"], "endpoint": "get_note"}],
        app="a",
        commit_sha="c",
    )
    scs = generate_scenarios(index, auth=auth)
    assert scs[0].steps[0].path == "/login" and scs[0].steps[-1].auth is True
    noauth = [s for s in scs if not s.steps[0].path == "/login"]
    assert noauth and all(not s.steps[-1].auth for s in noauth)


# --------------------------------------------------------------------------
# 400 repair, with a fake server
# --------------------------------------------------------------------------


class _FakeApp(BaseHTTPRequestHandler):
    log: list[tuple[str, str, Any, str | None]] = []

    def log_message(self, *args: Any) -> None:
        pass

    def _reply(self, status: int, payload: Any) -> None:
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _body(self) -> Any:
        n = int(self.headers.get("Content-Length") or 0)
        try:
            return json.loads(self.rfile.read(n) or b"null")
        except ValueError:
            return None

    def do_GET(self) -> None:
        self.log.append(("GET", self.path, None, self.headers.get("Authorization")))
        if self.path == "/items/7":
            self._reply(200, {"id": 7})
        else:
            self._reply(404, {"detail": "not found"})

    def do_POST(self) -> None:
        body = self._body()
        self.log.append(("POST", self.path, body, self.headers.get("Authorization")))
        if self.path == "/login":
            self._reply(200, {"token": "secret-token-123"})
        elif self.path == "/items":
            if not isinstance(body, dict) or "title" not in body:
                self._reply(400, {"title": ["This field is required."]})
            elif not isinstance(body.get("count"), int):
                self._reply(400, {"count": ["A valid integer is required."]})
            else:
                self._reply(201, {"id": 7, "title": body["title"]})
        elif self.path == "/detail-style":
            if not isinstance(body, dict) or "name" not in body:
                self._reply(400, {"detail": "'name' is required"})
            else:
                self._reply(200, {"ok": True})
        else:
            self._reply(404, {})


@pytest.fixture()
def fake_app() -> Any:
    _FakeApp.log = []
    server = HTTPServer(("127.0.0.1", 0), _FakeApp)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", _FakeApp.log
    finally:
        server.shutdown()
        server.server_close()


def test_400_with_a_missing_field_is_repaired_once_and_retried(fake_app: Any) -> None:
    base_url, log = fake_app
    runner = ScenarioRunner(HttpClient(base_url))
    sc = Scenario([Step("POST", "/items", body={}, content_type="application/json")], "POST /items")
    result = runner(sc)
    assert [(e.status, e.attempt) for e in result.exchanges] == [(400, 0), (400, 1)]
    # the first repair added `title`; the server then wanted an integer `count`,
    # which is a second repair and the runner repairs only once per step
    assert log[1][2] == {"title": "pd-title"}


def test_400_repair_adds_every_field_named_in_one_response(fake_app: Any) -> None:
    base_url, log = fake_app
    runner = ScenarioRunner(HttpClient(base_url))
    sc = Scenario(
        [Step("POST", "/items", body={"title": "t"}, content_type="application/json")],
        "POST /items",
    )
    result = runner(sc)
    assert [(e.status, e.attempt) for e in result.exchanges] == [(400, 0), (201, 1)]
    assert log[-1][2] == {"title": "t", "count": 1}
    eff = result.scenario
    assert eff.repaired_from == sc.id and eff.id != sc.id
    assert eff.steps[0].body == {"title": "t", "count": 1}


def test_detail_string_error_is_repaired(fake_app: Any) -> None:
    base_url, _ = fake_app
    runner = ScenarioRunner(HttpClient(base_url))
    sc = Scenario([Step("POST", "/detail-style", body={})], "POST /detail-style")
    result = runner(sc)
    assert [(e.status, e.attempt) for e in result.exchanges] == [(400, 0), (200, 1)]


def test_repair_step_understands_the_named_error_shapes() -> None:
    step = Step("POST", "/x", body={"n": "text"})
    drf = repair_step(step, {"n": ["A valid integer is required."], "t": ["Required."]})
    assert drf is not None and drf.body == {"n": 1, "t": "pd-t"}
    pyd = repair_step(
        Step("POST", "/x", body={}),
        {"detail": [{"loc": ["body", "age"], "msg": "field required", "type": "missing"}]},
    )
    assert pyd is not None and pyd.body == {"age": "pd-age"}
    errs = repair_step(
        Step("POST", "/x", body={}), {"errors": [{"loc": ["email"], "msg": "not a valid email"}]}
    )
    assert errs is not None and errs.body == {"email": "pd@example.com"}
    typed = repair_step(
        Step("POST", "/x", body={"age": "old"}),
        {"age": ["wrong"]},
        {"properties": {"age": {"type": "integer"}}},
    )
    assert typed is not None and typed.body == {"age": 1}
    added = repair_step(step, {"unknown": ["bad"]})
    assert added is not None and added.body == {"n": "text", "unknown": "pd-unknown"}
    assert repair_step(step, "not json") is None
    assert repair_step(Step("POST", "/x", body=[1]), {"a": ["x"]}) is None
    assert error_fields({"error": "username and password required"}) == {
        "username": "username and password required",
        "password": "username and password required",
    }


def test_ids_chain_through_the_runner_and_the_token_is_never_recorded(
    fake_app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    base_url, log = fake_app
    monkeypatch.setenv("PD_TEST_PASSWORD", "from-the-host-env")  # must never be used
    login = Step(
        "POST",
        "/login",
        body={"username": "u", "password": "${PD_TEST_PASSWORD}"},
        content_type="application/json",
        capture={"token": "token"},
    )
    make = Step(
        "POST",
        "/items",
        body={"title": "t", "count": 1},
        content_type="application/json",
        auth=True,
        capture={"item_id": "id"},
    )
    get = Step("GET", "/items/{item_id}", path_params={"item_id": "${item_id}"}, auth=True)
    sc = Scenario([login, make, get], "GET /items/{item_id}")
    from proofdeploy.lean.http import AuthSpec

    runner = ScenarioRunner(
        HttpClient(base_url),
        auth=AuthSpec(login, "Token"),
        env={"PD_TEST_PASSWORD": "hunter2"},  # the side's own values, not the host env
    )
    result = runner(sc)
    assert [e.status for e in result.exchanges] == [200, 201, 200]
    assert log[-1][1] == "/items/7" and log[-1][3] == "Token secret-token-123"
    assert log[0][2]["password"] == "hunter2"  # expanded on the wire ...
    dumped = json.dumps([e.to_dict() for e in result.exchanges])
    assert "secret-token-123" not in dumped  # ... and never recorded
    assert "hunter2" not in dumped and "${PD_TEST_PASSWORD}" in dumped
    assert result.exchanges[2].request["url_path"] == "/items/7"
    assert result.exchanges[1].request_id == f"{sc.id}:1"


def test_failed_producer_aborts_the_scenario(fake_app: Any) -> None:
    base_url, log = fake_app
    make = Step("POST", "/nowhere", body={}, capture={"item_id": "id"})
    get = Step("GET", "/items/{item_id}", path_params={"item_id": "${item_id}"})
    result = ScenarioRunner(HttpClient(base_url))(Scenario([make, get], "GET /items/{item_id}"))
    assert result.aborted and len(result.exchanges) == 1
    assert all(entry[0] != "GET" for entry in log)


def test_unresolved_reference_is_a_status_0_exchange(fake_app: Any) -> None:
    base_url, _ = fake_app
    get = Step("GET", "/items/{item_id}", path_params={"item_id": "${item_id}"})
    result = ScenarioRunner(HttpClient(base_url))(Scenario([get], "GET /items/{item_id}"))
    assert result.exchanges[0].status == 0 and result.aborted


# --------------------------------------------------------------------------
# the loop, with fakes
# --------------------------------------------------------------------------


class _FakeSession:
    """Coverage grows when a scenario with a given target has run."""

    def __init__(self, hits_by_target: dict[str, tuple[str, int]]) -> None:
        self.hits_by_target = hits_by_target
        self.cov: lc.Coverage = {}
        self.snapshots = 0

    def record(self, sc: Scenario) -> None:
        hit = self.hits_by_target.get(sc.target_operation)
        if hit:
            self.cov.setdefault(sc.request_id(len(sc.steps) - 1), {}).setdefault(hit[0], set()).add(
                hit[1]
            )

    def snapshot(self, *, root_prefix: str = "") -> lc.Coverage:
        self.snapshots += 1
        return {k: {p: set(v) for p, v in f.items()} for k, f in self.cov.items()}


def _loop_fixture() -> tuple[AppIndex, ChangedLines]:
    rules = [
        {"rule": "/health", "methods": ["GET"], "endpoint": "health"},
        {"rule": "/notes", "methods": ["POST"], "endpoint": "create_note"},
        {"rule": "/notes/<int:note_id>", "methods": ["GET"], "endpoint": "get_note"},
    ]
    return (
        index_from_flask_url_map(rules, app="a", commit_sha="c"),
        ChangedLines.from_diff(DIFF_GET_NOTE),
    )


def test_loop_stops_when_covered_and_returns_the_impact_set() -> None:
    index, changed = _loop_fixture()
    session = _FakeSession({"GET /notes/{note_id}": ("app.py", 99)})
    ran: list[str] = []

    def run(sc: Scenario) -> Any:
        from proofdeploy.lean.generate import RunResult

        ran.append(sc.target_operation)
        session.record(sc)
        ex = [
            Exchange(
                sc.id, i, {"method": s.method, "url_path": s.path, "body": None}, 200, {}, {}, 0
            )
            for i, s in enumerate(sc.steps)
        ]
        return RunResult(ex, sc)

    res = cover_change(index, changed, run, session, 200, diff_text=DIFF_GET_NOTE)
    assert res.covered and res.unexercised == {}
    assert ran[0] == "GET /notes/{note_id}"  # ranked first; nothing else needed
    assert len(ran) == 1 and res.scenarios_run == 1
    assert [s.target_operation for s in res.impact_set] == ["GET /notes/{note_id}"]
    assert res.hits[res.impact_set[0].id]["hits_added"] == ["app.py:99"]
    assert res.requests_to_cover == res.requests_used == len(res.exchanges)
    json.dumps(res.stats())


def test_loop_stops_at_the_budget_and_reports_what_is_left() -> None:
    index, changed = _loop_fixture()
    session = _FakeSession({})  # nothing ever covers the line
    from proofdeploy.lean.generate import RunResult

    def run(sc: Scenario) -> Any:
        ex = [
            Exchange(sc.id, i, {"method": "GET", "url_path": "/", "body": None}, 200, {}, {}, 0)
            for i, _ in enumerate(sc.steps)
        ]
        return RunResult(ex, sc)

    scenarios = [Scenario([Step("GET", f"/p{i}")], "GET /health") for i in range(8)]
    res = cover_change(
        index, changed, run, session, 5, diff_text=DIFF_GET_NOTE, scenarios=scenarios
    )
    assert not res.covered and res.budget_exhausted
    assert res.requests_used == 5 and res.scenarios_run == 5
    assert res.unexercised == {"app.py": {"added": [99], "removed": []}}
    assert res.impact_set == [] and res.requests_to_cover is None
    # a scenario that cannot start inside the remaining budget is skipped
    big = Scenario([Step("GET", "/a"), Step("GET", "/b"), Step("GET", "/c")], "GET /health")
    small = Scenario([Step("GET", "/d")], "GET /health")
    res2 = cover_change(
        index, changed, run, _FakeSession({}), 2, diff_text=DIFF_GET_NOTE, scenarios=[big, small]
    )
    assert res2.scenarios_run == 1 and res2.requests_used == 1


def test_value_hint_runs_after_the_generated_scenarios_are_exhausted() -> None:
    index, changed = _loop_fixture()
    session = _FakeSession({})
    calls: list[tuple[str, str]] = []
    bodies: list[Any] = []
    from proofdeploy.lean.generate import RunResult

    def run(sc: Scenario) -> Any:
        bodies.append(sc.steps[-1].body)
        if sc.steps[-1].body == {"title": "magic"}:
            session.cov[sc.request_id(0)] = {"app.py": {99}}
        return RunResult(
            [Exchange(sc.id, 0, {"method": "POST", "url_path": "/", "body": None}, 200, {}, {}, 0)],
            sc,
        )

    def hint(op: Operation, snippet: str) -> dict[str, Any]:
        calls.append((f"{op.method} {op.path}", snippet))
        return {"title": "magic"} if op.method == "POST" else {}

    res = cover_change(index, changed, run, session, 200, diff_text=DIFF_GET_NOTE, value_hint=hint)
    assert calls and "app.py:99:" in calls[0][1] and "!= 1" in calls[0][1]
    assert {"title": "magic"} in bodies
    assert res.covered
    no_hint = cover_change(index, changed, run, _FakeSession({}), 200, diff_text=DIFF_GET_NOTE)
    assert not no_hint.covered


def test_flush_without_a_listener_times_out(tmp_path: Path) -> None:
    pd = lc.install_pdcov(tmp_path / "pdcov", tmp_path)
    session = lc.CoverageSession(pd, ack_timeout=0.1)
    with pytest.raises(lc.CoverageHarnessError, match="flush"):
        session.flush()


# --------------------------------------------------------------------------
# end to end on the golden Flask app
# --------------------------------------------------------------------------

APP = "python-flask-sqlite"
BUDGET_E2E = 20


@pytest.fixture(scope="module")
def golden(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    root = tmp_path_factory.mktemp("lean_p3")
    repo = root / "repo"
    shas = kit.materialize_golden(APP, repo)
    # HEAD ends on the lockmismatch revision (a deliberately wrong hash).
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", shas["bug"]], check=True)
    if not hostrun.pypi_reachable():
        pytest.skip("PyPI unreachable: cannot create the hash-pinned golden venv (no network)")
    py = hostrun.make_venv(root / "venv", repo / "requirements.txt")
    return {"root": root, "repo": repo, "py": py, "shas": shas}


def _diff(golden: dict[str, Any], a: str, b: str) -> str:
    return subprocess.run(
        ["git", "-C", str(golden["repo"]), "diff", golden["shas"][a], golden["shas"][b]],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def _cover(
    golden: dict[str, Any],
    rev: str,
    diff_text: str,
    tmp: Path,
    *,
    seed_facts: bool = True,
    repair: bool = True,
) -> Any:
    tree = hostrun.add_worktree(
        golden["repo"], golden["shas"][rev], golden["root"] / f"t-{rev}-{tmp.name}"
    )
    contract = tree / "proofdeploy.yml"
    facts = seed_facts_from_contract(contract) if seed_facts else []
    index = index_from_flask_url_map(
        hostrun.flask_url_map(golden["py"], tree),
        app=APP,
        commit_sha=golden["shas"][rev],
        seed_facts=facts,
    )
    auth = auth_from_contract(contract)
    changed = ChangedLines.from_diff(diff_text)
    with hostrun.running_app(golden["py"], tree, tmp / "pdcov") as app:
        runner = ScenarioRunner(
            HttpClient(app.base_url), auth=auth, session=app.session, index=index, repair=repair
        )
        result = cover_change(
            index, changed, runner, app.session, BUDGET_E2E, diff_text=diff_text, auth=auth
        )
        app.stop_and_collect()
    return result, changed


def _env(monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory) -> Path:
    # CI: pytest-cov exports these; the app must not inherit them.
    monkeypatch.setenv("COVERAGE_FILE", str(tmp_path_factory.mktemp("stray") / ".coverage.host"))
    monkeypatch.setenv("COV_CORE_SOURCE", "proofdeploy")
    monkeypatch.setenv("COV_CORE_CONFIG", "x")
    return tmp_path_factory.mktemp("tool")


def test_golden_bug_change_is_covered_by_the_note_lookup(
    golden: dict[str, Any],
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tmp = _env(monkeypatch, tmp_path_factory)
    diff = _diff(golden, "base", "bug")
    res, changed = _cover(golden, "bug", diff, tmp)
    (line,) = changed["app.py"]["added"]
    assert res.covered and res.unexercised == {}
    assert res.requests_to_cover is not None and res.requests_used <= BUDGET_E2E
    assert [s.target_operation for s in res.impact_set] == ["GET /notes/{note_id}"]
    sc = res.impact_set[0]
    assert res.hits[sc.id]["hits_added"] == [f"app.py:{line}"]
    # the covering scenario is the seeded read: it logs in and reads seeded note 1
    assert [(s.method, s.path) for s in sc.steps] == [
        ("POST", "/login"),
        ("GET", "/notes/{note_id}"),
    ]
    assert sc.steps[-1].path_params == {"note_id": 1}
    assert all(e.status in (200, 201) for e in res.exchanges[: res.requests_used])
    print(
        f"golden bug: covered in {res.requests_to_cover} requests, {res.scenarios_run} scenario(s)"
    )


def test_golden_bug_is_covered_by_chaining_alone_without_seed_facts(
    golden: dict[str, Any],
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tmp = _env(monkeypatch, tmp_path_factory)
    res, _ = _cover(golden, "bug", _diff(golden, "base", "bug"), tmp, seed_facts=False)
    assert res.covered and res.requests_used <= BUDGET_E2E
    assert [s.target_operation for s in res.impact_set] == ["GET /notes/{note_id}"]
    assert [s.method for s in res.impact_set[0].steps] == ["POST", "POST", "GET"]
    print(f"golden bug, no seed facts: covered in {res.requests_to_cover} requests")


def test_golden_clean_change_adds_a_tag_field_and_both_lines_are_covered(
    golden: dict[str, Any],
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tmp = _env(monkeypatch, tmp_path_factory)
    diff = _diff(golden, "intermediate", "clean")
    assert '+        "tag": "general",' in diff
    res, changed = _cover(golden, "clean", diff, tmp)
    assert len(changed["app.py"]["added"]) == 2  # the done line and the new tag line
    assert res.covered and res.unexercised == {}
    assert res.requests_used <= BUDGET_E2E
    assert [s.target_operation for s in res.impact_set] == ["GET /notes/{note_id}"]
    assert len(res.hits[res.impact_set[0].id]["hits_added"]) == 2
    print(f"golden clean: covered in {res.requests_to_cover} requests")


def _bug_app_parts(golden: dict[str, Any], tmp: Path) -> tuple[Path, Any, Any, ChangedLines, str]:
    tree = hostrun.add_worktree(
        golden["repo"], golden["shas"]["bug"], golden["root"] / f"t-snap-{tmp.name}"
    )
    contract = tree / "proofdeploy.yml"
    index = index_from_flask_url_map(
        hostrun.flask_url_map(golden["py"], tree),
        app=APP,
        commit_sha=golden["shas"]["bug"],
        seed_facts=seed_facts_from_contract(contract),
    )
    diff = _diff(golden, "base", "bug")
    return tree, index, auth_from_contract(contract), ChangedLines.from_diff(diff), diff


def test_snapshot_carries_coverage_while_the_app_is_still_running(
    golden: dict[str, Any],
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tmp = _env(monkeypatch, tmp_path_factory)
    tree, _, _, changed, _ = _bug_app_parts(golden, tmp)
    (line,) = changed["app.py"]["added"]
    client_login = Step(
        "POST",
        "/login",
        body={"username": "seeduser", "password": "seedpass"},
        content_type="application/json",
        capture={"token": "token"},
    )
    with hostrun.running_app(golden["py"], tree, tmp / "pdcov") as app:
        client = HttpClient(app.base_url)
        runner = ScenarioRunner(client, session=app.session)
        sc1 = Scenario(
            [client_login, Step("GET", "/notes/{n}", {"n": 1}, auth=True)], "GET /notes/1"
        )
        runner(sc1)
        first = app.session.snapshot()
        assert app.proc.poll() is None  # still running
        ctx1 = sc1.request_id(1)
        assert line in first[ctx1]["app.py"]
        assert sc1.request_id(0) in first and ctx1 in first
        sc2 = Scenario(
            [client_login, Step("GET", "/notes/{n}", {"n": 2}, auth=True)], "GET /notes/2"
        )
        runner(sc2)
        second = app.session.snapshot()
        assert line in second[ctx1]["app.py"]  # earlier context is kept
        assert line in second[sc2.request_id(1)]["app.py"]  # and the new one added
        assert sc2.request_id(1) not in first
        app.stop_and_collect()


def test_loop_stops_after_the_impacting_scenario_using_real_snapshots(
    golden: dict[str, Any],
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tmp = _env(monkeypatch, tmp_path_factory)
    tree, index, auth, changed, diff = _bug_app_parts(golden, tmp)
    impacting = generate_scenarios(index, diff_text=diff, auth=auth)[0]
    assert impacting.target_operation == "GET /notes/{note_id}"
    later = [Scenario([Step("GET", "/health", query={"v": i})], "GET /health") for i in range(4)]
    with hostrun.running_app(golden["py"], tree, tmp / "pdcov") as app:
        runner = ScenarioRunner(
            HttpClient(app.base_url), auth=auth, session=app.session, index=index
        )
        res = cover_change(
            index, changed, runner, app.session, 20, diff_text=diff, auth=auth,
            scenarios=[impacting, *later],
        )  # fmt: skip
        app.stop_and_collect()
    assert res.covered and res.scenarios_run == 1
    assert res.requests_used == len(impacting.steps) == 2  # login + seeded get
    ran = {e.scenario_id for e in res.exchanges}
    assert ran == {impacting.id} and not ran & {s.id for s in later}
    assert [s.id for s in res.impact_set] == [impacting.id]


# --------------------------------------------------------------------------
# WO-R4-G on the golden app: sign in once, aim, seed (registers R7, R2)
# --------------------------------------------------------------------------


def _line_diff(tree: Path, wanted: list[str]) -> str:
    """A diff that changes exactly the lines of ``app.py`` containing each wanted text."""
    lines = (tree / "app.py").read_text(encoding="utf-8").splitlines()
    out = ["diff --git a/app.py b/app.py", "--- a/app.py", "+++ b/app.py"]
    for text in wanted:
        (n,) = [i + 1 for i, ln in enumerate(lines) if text in ln]
        out += [f"@@ -{n},1 +{n},1 @@", f"-{lines[n - 1]}", f"+{lines[n - 1]}  # changed"]
    return "\n".join(out) + "\n"


def _golden_run(
    golden: dict[str, Any],
    tmp: Path,
    diff_for: Any,
    budget: int,
    *,
    schema: bool = False,
) -> tuple[Any, Any, Any]:
    tree, index, auth, _, _ = _bug_app_parts(golden, tmp)
    if schema:
        for op in index.operations:
            if (op.method, op.path) == ("POST", "/notes"):
                op.request_schema = {
                    "type": "object",
                    "required": ["title"],
                    "properties": {
                        "title": {"type": "string", "maxLength": 80},
                        "color": {"type": "string", "maxLength": 12},
                    },
                }
                op.request_content_type = "application/json"
    diff = diff_for(tree)
    changed = ChangedLines.from_diff(diff)
    with hostrun.running_app(golden["py"], tree, tmp / "pdcov") as app:
        runner = ScenarioRunner(
            HttpClient(app.base_url), auth=auth, session=app.session, index=index
        )
        res = cover_change(
            index, changed, runner, app.session, budget, diff_text=diff, auth=auth, side="new"
        )
        app.stop_and_collect()
    return res, runner, index


def test_golden_signs_in_once_for_the_whole_loop(
    golden: dict[str, Any],
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tmp = _env(monkeypatch, tmp_path_factory)

    def diff_for(tree: Path) -> str:
        return _line_diff(tree, ['"done": row[', "if __name__"])

    res, runner, _ = _golden_run(golden, tmp, diff_for, 40)
    assert res.scenarios_run >= 10 and not res.covered  # the last line never runs in a request
    sent = [
        e
        for e in res.exchanges
        if e.request["url_path"] == "/login" and e.request["body"]["password"] == "seedpass"
    ]
    # one sign-in for the loop, one more for the scenario aimed at the login operation itself
    assert res.logins == runner.logins == len(sent) and 1 <= res.logins <= 2
    assert res.scenarios_run > 5 * res.logins and res.stats()["logins"] == res.logins
    assert res.requests_used == len(res.exchanges)  # logins are in the count the budget sees
    print(f"golden: {res.scenarios_run} scenarios, {res.logins} login(s)")


def test_golden_aim_spends_the_remaining_budget_on_the_operation_reaching_the_change(
    golden: dict[str, Any],
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tmp = _env(monkeypatch, tmp_path_factory)

    def diff_for(tree: Path) -> str:
        # the done line (every read), the 404 line (a read of a missing note), and a line
        # no request reaches: the loop runs to the end of its budget
        return _line_diff(tree, ['"done": row[', '"not found"', "if __name__"])

    res, _, _ = _golden_run(golden, tmp, diff_for, 20)
    top = res.aim[0]
    assert top["operation"] == "GET /notes/{note_id}" and top["reached_a_changed_line"]
    by_op = res.requests_by_operation
    spent = by_op["GET /notes/{note_id}"]
    first_pass = 3  # login + the seeded read of note 1, and the seeded read of note 2
    assert spent - first_pass > top["budget_left"] / 2, (by_op, top)
    assert all(v <= spent for v in by_op.values())
    assert res.unexercised["app.py"]["added"] != []  # only the unreachable line is left
    assert len(res.unexercised["app.py"]["added"]) == 1
    print(f"golden aim: {by_op}, left at aim {top['budget_left']}")


def test_golden_seeding_makes_records_with_and_without_optional_fields(
    golden: dict[str, Any],
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tmp = _env(monkeypatch, tmp_path_factory)

    def diff_for(tree: Path) -> str:
        return _line_diff(tree, ['"done": row[', "if __name__"])

    res, _, _ = _golden_run(golden, tmp, diff_for, 60, schema=True)
    posts = [
        e
        for e in res.exchanges
        if e.request["url_path"] == "/notes" and e.request["method"] == "POST"
    ]
    bodies = [e.request["body"] for e in posts]
    assert {"title": bodies[0]["title"]} == bodies[0]  # first record: optional fields omitted
    assert any(set(b) == {"title", "color"} for b in bodies)  # one with every field set
    assert len(next(b for b in bodies if "color" in b)["title"]) == 80  # at maxLength
    assert all(e.status == 200 for e in posts[:3])
    # each record was read back, in the same scenario, before any probing
    reads = [e for e in res.exchanges if e.request["url_path"].startswith("/notes/")]
    assert [e.status for e in reads[:3]] == [200, 200, 200]
    assert res.seed_scenarios_run >= 3 and res.seed_requests <= 60 // 3
    first_ids = [e.scenario_id for e in res.exchanges[: res.seed_requests]]
    assert {s.id for s in res.impact_set[: res.seed_scenarios_run]} >= set(first_ids)
