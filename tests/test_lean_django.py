"""Lean round 5: the Django / Django REST Framework adapter (mapping ladder step 2).

The subject is a SYNTHETIC project (``tests/fixtures/lean/drf_app``), run in this
interpreter: Django and djangorestframework are test (dev) dependencies only.
No real app is used and no model is called.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

from proofdeploy.lean import hostrun
from proofdeploy.lean.generate import ScenarioGenerator, ScenarioRunner
from proofdeploy.lean.http import Exchange, HttpClient
from proofdeploy.lean.index import (
    DJANGO_DUMP_SNIPPET,
    AppIndex,
    Operation,
    index_from_django_dump,
)
from proofdeploy.lean.pipeline import LeanConfig, LeanRefusal, acceptance_rate, build_index

APP_DIR = Path(__file__).resolve().parent / "fixtures" / "lean" / "drf_app"
SETTINGS = "tinyapp.settings"

EXPECTED_ROUTES = {
    "GET /api/",
    "GET /api/categories/",
    "POST /api/categories/",
    "DELETE /api/categories/{pk}/",
    "GET /api/categories/{pk}/",
    "PATCH /api/categories/{pk}/",
    "PUT /api/categories/{pk}/",
    "GET /api/items/",
    "POST /api/items/",
    "GET /api/items/stats/",
    "DELETE /api/items/{pk}/",
    "GET /api/items/{pk}/",
    "PATCH /api/items/{pk}/",
    "PUT /api/items/{pk}/",
    "POST /api/items/{pk}/archive/",
    "GET /health/",
    "GET /legacy/{num}/{slug}/",
    "GET /ping/",
    "POST /ping/",
    "GET /plain/{code}/",
}


def _clean_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("COV_CORE", "COVERAGE")) and k != "DJANGO_SETTINGS_MODULE"
    }
    env["PYTHONHASHSEED"] = "0"
    env.update(extra or {})
    return env


def _dump(
    settings: str | None = SETTINGS, *, env_settings: str | None = None
) -> list[dict[str, Any]]:
    argv = [sys.executable, "-c", DJANGO_DUMP_SNIPPET]
    if settings:
        argv.append(settings)
    extra = {"DJANGO_SETTINGS_MODULE": env_settings} if env_settings else None
    proc = subprocess.run(
        argv, cwd=APP_DIR, env=_clean_env(extra), capture_output=True, text=True, timeout=120
    )
    assert proc.returncode == 0, proc.stderr[-1500:]
    routes: list[dict[str, Any]] = json.loads(proc.stdout)
    return routes


@pytest.fixture(scope="module")
def routes() -> list[dict[str, Any]]:
    return _dump()


@pytest.fixture(scope="module")
def index(routes: list[dict[str, Any]]) -> AppIndex:
    return index_from_django_dump(routes, app="tiny", commit_sha="c1", seed_facts=[])


def _op(index: AppIndex, method: str, path: str) -> Operation:
    (op,) = [o for o in index.operations if o.method == method and o.path == path]
    return op


# --------------------------------------------------------------------------
# 1. the in-app dump and the routes
# --------------------------------------------------------------------------


def test_the_index_lists_exactly_the_routes_of_the_project(index: AppIndex) -> None:
    got = {f"{o.method} {o.path}" for o in index.operations}
    assert got == EXPECTED_ROUTES, sorted(got ^ EXPECTED_ROUTES)
    assert index.source_kind == "django_drf"


def test_router_actions_with_their_methods_are_indexed(index: AppIndex) -> None:
    archive = _op(index, "POST", "/api/items/{pk}/archive/")
    assert archive.source.startswith("django_drf:") and archive.summary.endswith(".archive")
    assert [o.method for o in index.operations if o.path == "/api/items/stats/"] == ["GET"]
    detail = sorted(o.method for o in index.operations if o.path == "/api/items/{pk}/")
    assert detail == ["DELETE", "GET", "PATCH", "PUT"]


def test_path_params_come_from_converters_regex_groups_and_the_model(index: AppIndex) -> None:
    def params(method: str, path: str) -> dict[str, dict[str, Any]]:
        return {p["name"]: p["schema"] for p in _op(index, method, path).params}

    assert params("GET", "/api/items/{pk}/") == {"pk": {"type": "integer"}}  # model AutoField
    assert params("GET", "/legacy/{num}/{slug}/") == {
        "num": {"type": "integer"},
        "slug": {"type": "string", "pattern": "^[-a-zA-Z0-9_]+$"},
    }
    assert params("GET", "/plain/{code}/") == {"code": {"type": "integer"}}  # re_path group
    assert all(p["in"] == "path" and p["required"] for o in index.operations for p in o.params)


def test_admin_and_format_suffix_routes_are_left_out(index: AppIndex) -> None:
    paths = [o.path for o in index.operations]
    assert not any(p.startswith("/admin") for p in paths)
    assert not any("format" in p or "\\" in p or "^" in p or "(" in p for p in paths)


def test_settings_come_from_the_app_environment_when_not_given() -> None:
    assert _dump(None, env_settings=SETTINGS) == _dump(SETTINGS)


def test_the_dump_is_deterministic(routes: list[dict[str, Any]]) -> None:
    again = _dump()
    a = index_from_django_dump(routes, app="tiny", commit_sha="c1")
    b = index_from_django_dump(again, app="tiny", commit_sha="c1")
    assert a.sha256() == b.sha256()


# --------------------------------------------------------------------------
# 2. serializer shapes
# --------------------------------------------------------------------------


def test_create_schema_from_the_serializer(index: AppIndex) -> None:
    op = _op(index, "POST", "/api/items/")
    s = op.request_schema
    assert s is not None and op.request_content_type == "application/json"
    props = s["properties"]
    # read-only fields never appear in a request body
    assert "id" not in props and "created" not in props
    assert s["required"] == ["title"]
    assert props["title"] == {"type": "string", "maxLength": 40}
    assert props["status"] == {"type": "string", "enum": ["draft", "live"]}
    assert props["quantity"] == {"type": "integer", "minimum": 1, "maximum": 99}
    assert props["labels"]["type"] == "array"
    assert props["labels"]["items"] == {"type": "string", "maxLength": 20}
    assert props["labels"]["description"] == "free tags"
    # nested serializer -> object with its own required list
    dims = props["dimensions"]
    assert dims["type"] == "object" and dims["required"] == ["width"]
    assert dims["properties"]["width"] == {"type": "integer", "minimum": 1, "maximum": 500}
    # PrimaryKeyRelatedField -> integer id marked as a reference; allow_null
    assert props["category_id"] == {"type": "integer", "x-reference": True, "nullable": True}
    assert op.field_descriptions["request:labels"] == "free tags"


def test_response_schema_keeps_read_only_and_wraps_lists(index: AppIndex) -> None:
    created = _op(index, "POST", "/api/items/").response_schema
    assert created is not None
    assert created["properties"]["id"] == {"type": "integer", "readOnly": True}
    assert created["properties"]["created"]["readOnly"] is True
    listed = _op(index, "GET", "/api/items/").response_schema
    assert listed is not None and listed["type"] == "array"
    assert listed["items"]["properties"]["title"]["maxLength"] == 40
    assert _op(index, "DELETE", "/api/items/{pk}/").response_schema is None


def test_methods_differ_in_required_and_action_serializers(index: AppIndex) -> None:
    put = _op(index, "PUT", "/api/items/{pk}/").request_schema
    patch = _op(index, "PATCH", "/api/items/{pk}/").request_schema
    assert put is not None and patch is not None
    assert put["required"] == ["title"]
    assert "required" not in patch  # partial update: nothing is required
    archive = _op(index, "POST", "/api/items/{pk}/archive/").request_schema
    assert archive == {
        "type": "object",
        "properties": {"reason": {"type": "string", "maxLength": 20}},
    }
    assert _op(index, "GET", "/api/items/{pk}/").request_schema is None


def test_a_plain_django_view_is_a_route_without_a_body(index: AppIndex) -> None:
    for method, path in (("GET", "/health/"), ("GET", "/legacy/{num}/{slug}/"), ("POST", "/ping/")):
        op = _op(index, method, path)
        assert op.request_schema is None and op.response_schema is None
        assert op.request_content_type is None and op.source.startswith("django:")


def test_a_project_without_drf_is_mapped_as_django() -> None:
    plain = [
        {
            "path": "/health/",
            "view": "app.views.health",
            "kind": "django",
            "params": [],
            "methods": {"GET": {"action": None, "request": None, "response": None}},
        }
    ]
    idx = index_from_django_dump(plain, app="a", commit_sha="c")
    assert idx.source_kind == "django" and [o.path for o in idx.operations] == ["/health/"]


# --------------------------------------------------------------------------
# 3. the mapping ladder and the manifest numbers
# --------------------------------------------------------------------------

OPENAPI_DOC = {
    "openapi": "3.0.3",
    "info": {"title": "t", "version": "1"},
    "paths": {"/things/": {"get": {"responses": {"200": {"description": "ok"}}}}},
}


class _Driver:
    label = "head"
    sha = "abc1234"
    root_prefix = ""

    def __init__(self, flask: list[dict[str, Any]] | None, django: list[dict[str, Any]] | None):
        self._flask, self._django = flask, django
        self.calls: list[str] = []

    def flask_rules(self, app: Any, target: str) -> list[dict[str, Any]] | None:
        self.calls.append("flask")
        return self._flask

    def django_routes(self, app: Any) -> list[dict[str, Any]] | None:
        self.calls.append("django")
        return self._django


class _Live:
    def __init__(self, base_url: str):
        self.base_url = base_url
        self.session = None


@pytest.fixture()
def openapi_server() -> Iterator[str]:
    class H(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path.startswith("/api/schema"):
                body = json.dumps(OPENAPI_DOC).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
            else:
                body = b"{}"
                self.send_response(404)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a: Any) -> None:
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture()
def dead_url() -> str:
    return f"http://127.0.0.1:{hostrun.free_port()}"


def _build(driver: _Driver, url: str) -> AppIndex:
    cfg = LeanConfig(repo=Path("."), base="b", head="h")
    return build_index(driver, _Live(url), cfg, app_name="x", seed_facts=[])  # type: ignore[arg-type]


FLASK_RULES = [{"rule": "/flask/", "methods": ["GET"], "endpoint": "f"}]


def test_published_openapi_wins_over_flask_and_django(
    openapi_server: str, routes: list[dict[str, Any]]
) -> None:
    driver = _Driver(FLASK_RULES, routes)
    idx = _build(driver, openapi_server)
    assert idx.source_kind == "openapi" and [o.path for o in idx.operations] == ["/things/"]
    assert driver.calls == []  # neither dump is even attempted


def test_flask_is_tried_before_django(dead_url: str, routes: list[dict[str, Any]]) -> None:
    driver = _Driver(FLASK_RULES, routes)
    idx = _build(driver, dead_url)
    assert idx.source_kind == "flask" and driver.calls == ["flask"]


def test_django_is_used_when_there_is_no_openapi_and_no_flask(
    dead_url: str, routes: list[dict[str, Any]]
) -> None:
    driver = _Driver(None, routes)
    idx = _build(driver, dead_url)
    assert idx.source_kind == "django_drf" and driver.calls == ["flask", "django"]
    assert {f"{o.method} {o.path}" for o in idx.operations} == EXPECTED_ROUTES


def test_nothing_mappable_is_cannot_map(dead_url: str) -> None:
    with pytest.raises(LeanRefusal) as exc:
        _build(_Driver(None, None), dead_url)
    assert exc.value.code == "cannot_map" and "Django" in str(exc.value)


def test_a_driver_without_a_django_dump_still_maps_or_refuses(dead_url: str) -> None:
    class Old:
        label, sha, root_prefix = "head", "a", ""

        def flask_rules(self, app: Any, target: str) -> None:
            return None

    with pytest.raises(LeanRefusal):
        _build(Old(), dead_url)  # type: ignore[arg-type]


def _ex(status: int) -> Exchange:
    return Exchange(
        scenario_id="s",
        step_index=0,
        request={"method": "GET", "url_path": "/x"},
        status=status,
        headers_subset={},
        body_json_or_text=None,
        elapsed_ms=1.0,
    )


def test_acceptance_rate_is_the_non_4xx_share_of_the_loop_requests() -> None:
    rows = [_ex(200), _ex(201), _ex(404), _ex(422), _ex(500), _ex(0)]
    assert acceptance_rate(rows) == {"requests": 6, "non_4xx": 4, "rate": 0.667}
    assert acceptance_rate([]) == {"requests": 0, "non_4xx": 0, "rate": None}


# --------------------------------------------------------------------------
# 4. the generator's seeding stage on the live project (host mode)
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def live_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    db = tmp_path_factory.mktemp("drfdb") / "app.sqlite"
    port = hostrun.free_port()
    log = tmp_path_factory.mktemp("drflog") / "app.log"
    with log.open("wb") as fh:
        proc = subprocess.Popen(
            [sys.executable, str(APP_DIR / "serve.py")],
            cwd=APP_DIR,
            env=_clean_env(
                {"PORT": str(port), "APP_DB": str(db), "DJANGO_SETTINGS_MODULE": SETTINGS}
            ),
            stdout=fh,
            stderr=subprocess.STDOUT,
        )
    try:
        for _ in range(150):
            try:
                hostrun.wait_http(port, "/health/", attempts=1)
                break
            except AssertionError:
                if proc.poll() is not None:
                    pytest.fail(f"synthetic app exited: {log.read_text()[-800:]}")
                time.sleep(0.2)
        else:
            pytest.fail("synthetic app did not come up")
        yield f"http://127.0.0.1:{port}"
    finally:
        proc.kill()
        proc.wait()


def test_seeding_stage_creates_are_accepted_with_the_dumped_shapes(
    index: AppIndex, live_url: str
) -> None:
    gen = ScenarioGenerator(index)
    runner = ScenarioRunner(HttpClient(live_url), index=index, repair=False)
    sent: list[Exchange] = []
    for sc in gen.seed_scenarios():
        sent += [
            e
            for e in runner(sc).exchanges
            if e.request.get("method") == "POST"
            and str(e.request.get("url_path")).startswith("/api/")
        ]
    items = [e for e in sent if e.request["url_path"] == "/api/items/"]
    cats = [e for e in sent if e.request["url_path"] == "/api/categories/"]
    assert cats and all(200 <= e.status < 300 for e in cats)
    ok = [e for e in items if 200 <= e.status < 300]
    # min and full creates (the update / delete flows create too) are accepted ...
    assert len(ok) >= 4, [(e.status, e.body_json_or_text) for e in items]
    # ... the only rejected shape is the stage's deliberate dangling reference (state "alt")
    for e in items:
        if e.status >= 300:
            assert e.status == 400 and set(e.body_json_or_text) == {"category_id"}, e
    bodies = [e.request["body"] for e in ok]
    assert any(set(b) == {"title"} for b in bodies)  # min: required fields only
    full = [b for b in bodies if "dimensions" in b]
    assert full and all(b["dimensions"]["width"] >= 1 for b in full)
    assert all("id" not in b and "created" not in b for b in bodies)  # read-only never sent


def test_the_synthetic_app_enforces_the_dumped_limits(live_url: str) -> None:
    """Fixture sanity: the app itself refuses what the dumped schema forbids."""
    status, _h, body, _ms = HttpClient(live_url).request(
        "POST",
        "/api/items/",
        data=json.dumps({"title": "x" * 41}).encode(),
        headers={"Content-Type": "application/json"},
    )
    assert status == 400 and "title" in body
