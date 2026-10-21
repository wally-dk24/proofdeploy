"""WO-R4-G (D267): sign in once, aim at the changed code, seed varied states.

No model is called and nothing leaves loopback: a small stateful HTTP app on
``http.server`` stands in for an application, and a stub session stands in for
coverage where the test needs to say which request reaches which changed line.
The golden-app end-to-end checks of the same items live in
``test_lean_generate.py`` (they share its hash-pinned golden venv).

Register rows proved here: R7 (a login per scenario; the budget it burns) and
R2 (the changed code is reached by aiming, not by a flat sweep).
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import pytest

from proofdeploy.lean import coverage as lc
from proofdeploy.lean.coverage import ChangedLines
from proofdeploy.lean.generate import (
    RunResult,
    ScenarioGenerator,
    ScenarioRunner,
    cover_change,
    rank_operations,
)
from proofdeploy.lean.http import AuthSpec, Exchange, HttpClient, Scenario, Step
from proofdeploy.lean.index import AppIndex, Operation

# --------------------------------------------------------------------------
# a small stateful app: customers, orders, a token login
# --------------------------------------------------------------------------


class _Shop(BaseHTTPRequestHandler):
    """Customers and orders in memory. ``state`` is replaced for every test."""

    state: dict[str, Any] = {}

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

    def _authorized(self) -> bool:
        st = self.state
        header = self.headers.get("Authorization", "")
        token = header[7:] if header.startswith("Bearer ") else ""
        st["seen"].append((self.command, self.path, token))
        if token == st["token"] and st["rejections"] == 0:
            return True
        if token == st["token"] and st["rejections"] > 0:
            st["rejections"] -= 1
            st["token"] = ""  # the server forgets the token: a 401 now, a new one at login
        self._reply(
            st["reject_status"], {"detail": "Authentication credentials were not provided."}
        )
        return False

    def _route(self) -> tuple[str, int | None]:
        parts = self.path.split("?")[0].strip("/").split("/")
        if len(parts) == 2 and parts[1].isdigit():
            return parts[0], int(parts[1])
        return parts[0], None

    def do_POST(self) -> None:
        st = self.state
        body = self._body()
        if self.path == "/login":
            if not isinstance(body, dict) or body.get("password") != "clerk-pass-17":
                self._reply(401, {"detail": "No active account found with the given credentials"})
                return
            st["logins"] += 1
            st["token"] = f"tok-{st['logins']:04d}"
            self._reply(200, {"token": st["token"]})
            return
        if not self._authorized():
            return
        coll, _ = self._route()
        if coll == "customers":
            if not isinstance(body, dict) or not body.get("name"):
                self._reply(400, {"name": ["This field is required."]})
                return
            rec = {"id": len(st["customers"]) + 1, **body}
            st["customers"][rec["id"]] = rec
            self._reply(201, rec)
        elif coll == "orders":
            if not isinstance(body, dict) or not body.get("sku"):
                self._reply(400, {"sku": ["This field is required."]})
                return
            cid = body.get("customer_id")
            if cid is not None and cid not in st["customers"]:
                self._reply(400, {"customer_id": [f'Invalid pk "{cid}" - object does not exist.']})
                return
            rec = {"id": len(st["orders"]) + 1, **body}
            st["orders"][rec["id"]] = rec
            self._reply(201, rec)
        else:
            self._reply(404, {"detail": "Not found."})

    def do_GET(self) -> None:
        st = self.state
        if not self._authorized():
            return
        coll, rid = self._route()
        table = st.get(coll)
        if table is None:
            self._reply(404, {"detail": "Not found."})
        elif rid is None:
            self._reply(200, {"count": len(table), "results": list(table.values())})
        elif rid in table:
            self._reply(200, table[rid])
        else:
            self._reply(404, {"detail": "Not found."})

    def do_PATCH(self) -> None:
        st = self.state
        body = self._body()
        if not self._authorized():
            return
        coll, rid = self._route()
        table = st.get(coll) or {}
        if rid in table and isinstance(body, dict):
            table[rid].update(body)
            self._reply(200, table[rid])
        else:
            self._reply(404, {"detail": "Not found."})

    def do_DELETE(self) -> None:
        st = self.state
        if not self._authorized():
            return
        coll, rid = self._route()
        table = st.get(coll) or {}
        if rid in table:
            del table[rid]
            self._reply(204, None)
        else:
            self._reply(404, {"detail": "Not found."})


@pytest.fixture()
def shop() -> Any:
    _Shop.state = {
        "logins": 0,
        "token": "",
        "rejections": 0,
        "reject_status": 401,
        "seen": [],
        "customers": {},
        "orders": {},
    }
    server = HTTPServer(("127.0.0.1", 0), _Shop)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", _Shop.state
    finally:
        server.shutdown()
        server.server_close()


LOGIN = Step(
    "POST",
    "/login",
    body={"username": "clerk", "password": "clerk-pass-17"},
    content_type="application/json",
    capture={"token": "token"},
)
AUTH = AuthSpec(LOGIN, "Bearer")


def _read(order_id: int, variant: int = 0) -> Scenario:
    """An authenticated read; ``variant`` only makes the scenario ids differ."""
    get = Step(
        "GET",
        "/orders/{order_id}",
        path_params={"order_id": order_id},
        query={"expand": f"v{variant}"},
        auth=True,
    )
    return Scenario([LOGIN, get], "GET /orders/{order_id}")


# --------------------------------------------------------------------------
# item 1: sign in once per side per run (register R7)
# --------------------------------------------------------------------------


def test_r7_fifty_authenticated_scenarios_sign_in_once(shop: Any) -> None:
    base_url, st = shop
    runner = ScenarioRunner(HttpClient(base_url), auth=AUTH)
    sent = 0
    for n in range(50):
        res = runner(_read(1 + n % 3, n))
        sent += len(res.exchanges)
        assert res.scenario.steps[0] == LOGIN  # the scenario keeps its login step ...
    assert st["logins"] == 1 and runner.logins == 1  # ... but only the first one is sent
    assert sent == 50 + 1  # 50 reads and one login: every request is counted
    seen = [entry for entry in st["seen"] if entry[0] == "GET"]
    assert len(seen) == 50 and {tok for _, _, tok in seen} == {"tok-0001"}


def test_r7_a_401_or_403_signs_in_again_once(shop: Any) -> None:
    base_url, st = shop
    for status in (401, 403):
        st.update(logins=0, token="", rejections=0, reject_status=status, seen=[])
        runner = ScenarioRunner(HttpClient(base_url), auth=AUTH)
        runner(_read(1, 0))
        assert runner.logins == 1
        st["rejections"] = 1  # the next authenticated request is refused and the token dies
        res = runner(_read(1, 1))
        assert res.exchanges[-1].status == status
        for n in range(2, 12):
            runner(_read(1, n))
        assert runner.logins == 2 and st["logins"] == 2  # re-login after the refusal, then quiet
        tokens = [tok for method, _, tok in st["seen"] if method == "GET"]
        assert tokens[:2] == ["tok-0001", "tok-0001"] and set(tokens[2:]) == {"tok-0002"}


def test_r7_a_request_without_credentials_does_not_drop_the_token(shop: Any) -> None:
    base_url, st = shop
    runner = ScenarioRunner(HttpClient(base_url), auth=AUTH)
    runner(_read(1, 0))
    anonymous = Scenario([Step("GET", "/orders/{order_id}", path_params={"order_id": 1})], "GET x")
    assert runner(anonymous).exchanges[0].status == 401  # no Authorization header was sent
    runner(_read(1, 1))
    assert st["logins"] == 1


def test_r7_a_scenario_aimed_at_the_login_operation_always_sends_it(shop: Any) -> None:
    base_url, st = shop
    runner = ScenarioRunner(HttpClient(base_url), auth=AUTH)
    runner(_read(1, 0))
    own = Scenario([LOGIN], "POST /login")
    assert len(runner(own).exchanges) == 1
    assert st["logins"] == 2 and runner.logins == 2


def test_r7_planned_requests_do_not_charge_a_login_that_will_be_skipped(shop: Any) -> None:
    base_url, _ = shop
    runner = ScenarioRunner(HttpClient(base_url), auth=AUTH)
    sc = _read(1, 0)
    assert runner.planned_requests(sc) == 2
    runner(sc)
    assert runner.planned_requests(_read(1, 1)) == 1


def _stub_run(session: _Reach) -> Any:
    def run(sc: Scenario) -> RunResult:
        ex = []
        for i, st in enumerate(sc.steps):
            session.touch(sc, i)
            ex.append(
                Exchange(
                    sc.id,
                    i,
                    {"method": st.method, "url_path": st.path, "body": st.body},
                    200,
                    {},
                    {},
                    0.0,
                )  # fmt: skip
            )
        return RunResult(ex, sc)

    return run


class _Reach:
    """Coverage by rule: ``rule(step) -> lines reached`` for the file ``billing.py``."""

    def __init__(self, rule: Any) -> None:
        self.rule = rule
        self.cov: lc.Coverage = {}

    def touch(self, sc: Scenario, idx: int) -> None:
        lines = self.rule(sc.steps[idx])
        if lines:
            self.cov.setdefault(sc.request_id(idx), {}).setdefault("billing.py", set()).update(
                lines
            )

    def snapshot(self, *, root_prefix: str = "") -> lc.Coverage:
        return {k: {p: set(v) for p, v in f.items()} for k, f in self.cov.items()}


def test_r7_logins_are_counted_in_the_loop_stats_and_the_budget(shop: Any) -> None:
    base_url, st = shop
    index = _shop_index()
    runner = ScenarioRunner(HttpClient(base_url), auth=AUTH, index=index)
    never = _Reach(lambda step: set())
    res = cover_change(
        index, ChangedLines.from_diff(DIFF), runner, never, 60, diff_text=DIFF, auth=AUTH
    )
    assert res.logins == st["logins"] == runner.logins
    assert 1 <= res.logins <= 3  # the seeding scenarios and probes shared one sign-in
    assert res.stats()["logins"] == res.logins
    assert res.requests_used == len(res.exchanges)
    posted = [e for e in res.exchanges if e.request["url_path"] == "/login"]
    assert len(posted) >= res.logins  # login requests are in the count the budget sees


# --------------------------------------------------------------------------
# an index shaped like a shop: customers, orders, payouts
# --------------------------------------------------------------------------

CUSTOMER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["name"],
    "properties": {
        "name": {"type": "string", "maxLength": 40},
        "email": {"type": "string", "format": "email"},
        "tier": {"type": "string", "enum": ["standard", "gold"]},
        "id": {"type": "integer", "readOnly": True},
    },
}
ORDER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["sku"],
    "properties": {
        "sku": {"type": "string", "maxLength": 24},
        "customer_id": {"type": "integer"},
        "quantity": {"type": "integer", "minimum": 1, "maximum": 99},
        "note": {"type": "string", "maxLength": 60},
        "gift": {"type": "boolean"},
        "id": {"type": "integer", "readOnly": True},
    },
}
ID_RESPONSE: dict[str, Any] = {"type": "object", "properties": {"id": {"type": "integer"}}}


def _op(method: str, path: str, schema: dict[str, Any] | None = None, **kw: Any) -> Operation:
    params = [
        {"name": seg[1:-1], "in": "path", "required": True, "schema": {"type": "integer"}}
        for seg in path.split("/")
        if seg.startswith("{")
    ]
    return Operation(
        method=method,
        path=path,
        params=params,
        request_schema=schema,
        response_schema=ID_RESPONSE,
        request_content_type="application/json" if schema else None,
        **kw,
    )


def _shop_index() -> AppIndex:
    ops = [
        _op("GET", "/customers"),
        _op("POST", "/customers", CUSTOMER_SCHEMA),
        _op("GET", "/customers/{customer_id}"),
        _op("DELETE", "/customers/{customer_id}"),
        _op("GET", "/orders"),
        _op("POST", "/orders", ORDER_SCHEMA),
        _op("GET", "/orders/{order_id}"),
        _op("PATCH", "/orders/{order_id}", ORDER_SCHEMA),
        _op("DELETE", "/orders/{order_id}"),
        _op("POST", "/login", {"type": "object", "properties": {"username": {"type": "string"}}}),
    ]
    return AppIndex(app="shop", commit_sha="0" * 40, operations=ops)


DIFF = """\
diff --git a/billing.py b/billing.py
--- a/billing.py
+++ b/billing.py
@@ -39,3 +39,5 @@ def settle_balance(ledger):
     ledger = load(ledger)
-    if ledger.total > 100:
+    if ledger.total >= 100:
+        ledger.flag = True
+        notify(ledger)
     return ledger
"""


# --------------------------------------------------------------------------
# item 3: varied states through the API (register R2)
# --------------------------------------------------------------------------


def test_r2_seed_shapes_cover_omitted_set_and_referenced_fields() -> None:
    gen = ScenarioGenerator(_shop_index(), auth=AUTH)
    seeds = gen.seed_scenarios()
    creates = [
        (sc, sc.steps[-2] if sc.steps[-1].method == "GET" else sc.steps[-1])
        for sc in seeds
        if sc.target_operation == "POST /orders"
    ]
    posts = [s for sc, _ in creates for s in sc.steps if s.method == "POST" and s.path == "/orders"]
    # the first three order scenarios are the three shapes
    minimal, full, alt = posts[0].body, posts[1].body, posts[2].body
    assert minimal == {"sku": minimal["sku"]}  # every optional field omitted
    assert set(full) == {"sku", "customer_id", "quantity", "note", "gift"}  # every field set
    assert len(full["note"]) == 60 and len(full["sku"]) == 24  # at maxLength
    assert "字" in full["note"] and full["quantity"] == 99
    assert set(alt) == {"sku", "customer_id", "note"}  # every second optional field
    assert len(alt["note"]) < 20 and alt != minimal  # and the plain values
    # all three differ and none repeats a unique-looking value of another
    assert len({minimal["sku"], full["sku"], alt["sku"]}) == 3


def test_r2_seed_references_use_an_existing_a_deleted_and_no_record() -> None:
    gen = ScenarioGenerator(_shop_index(), auth=AUTH)
    by_mode = [sc for sc in gen.seed_scenarios() if sc.target_operation == "POST /orders"][:3]
    minimal, full, alt = (sc.steps for sc in by_mode)
    assert not any(s.path == "/customers" for s in minimal)  # no reference at all
    assert "customer_id" not in next(s for s in minimal if s.path == "/orders").body
    kinds = [(s.method, s.path) for s in full if s.path.startswith("/customers")]
    assert kinds == [("POST", "/customers")]  # an existing customer, id captured
    create = next(s for s in full if s.path == "/orders" and s.method == "POST")
    assert create.body["customer_id"] == "${ref_customer_id}"
    kinds = [(s.method, s.path) for s in alt if s.path.startswith("/customers")]
    assert kinds == [("POST", "/customers"), ("DELETE", "/customers/{customer_id}")]
    create = next(s for s in alt if s.path == "/orders" and s.method == "POST")
    assert create.body["customer_id"] == "${ref_customer_id}"  # ... which is gone


def test_r2_seed_stages_run_creates_then_lists_then_updates_then_deletes() -> None:
    seeds = ScenarioGenerator(_shop_index(), auth=AUTH).seed_scenarios()

    def stage(sc: Scenario) -> str:
        names = [(s.method, s.path) for s in sc.steps if s.path != "/login"]
        if any(m == "PATCH" for m, _ in names):
            return "update"
        if ("DELETE", "/orders/{order_id}") in names:
            return "delete"
        return "list" if len(names) == 1 else "create"

    order = [stage(sc) for sc in seeds if sc.target_operation == "POST /orders"]
    assert order == ["create"] * 3 + ["list", "update", "delete"]
    assert [stage(sc) for sc in seeds].index("list") > len(seeds) // 3  # lists follow all creates
    upd = next(s for sc in seeds for s in sc.steps if s.method == "PATCH")
    assert len(upd.body) == 1  # one field changes
    assert upd.path_params == {"order_id": "${rec}"}


def test_r2_seeding_builds_records_with_and_without_optional_fields(shop: Any) -> None:
    base_url, st = shop
    index = _shop_index()
    runner = ScenarioRunner(HttpClient(base_url), auth=AUTH, index=index)
    for sc in ScenarioGenerator(index, auth=AUTH).seed_scenarios():
        runner(sc)
    assert st["logins"] == 1
    # Order 1..: the plain shape, the full shape and the one that points at a deleted customer
    orders = list(st["orders"].values())
    assert any(set(o) == {"id", "sku"} for o in orders)  # without optional fields
    assert any({"note", "quantity", "gift", "customer_id"} <= set(o) for o in orders)  # with
    full = next(o for o in orders if "note" in o)
    assert len(full["note"]) == 60 and full["customer_id"] in st["customers"]
    # the shape that references a deleted customer is refused by the app: kept as data, once
    attempts = [e for e in st["seen"] if e[0] == "POST" and e[1] == "/orders"]
    assert 3 <= len(attempts) <= 10


def test_r2_a_rejected_shape_is_data_not_retried_forever(shop: Any) -> None:
    base_url, st = shop
    index = _shop_index()
    gen = ScenarioGenerator(index, auth=AUTH)
    alt = [sc for sc in gen.seed_scenarios() if sc.target_operation == "POST /orders"][2]
    runner = ScenarioRunner(HttpClient(base_url), auth=AUTH, index=index)
    res = runner(alt)
    order_posts = [e for e in res.exchanges if e.request["url_path"] == "/orders"]
    assert order_posts[0].status == 400  # the deleted customer id is refused
    assert len(order_posts) <= 2  # at most the one repair attempt
    assert all(e.attempt <= 1 for e in res.exchanges)


def _covering_nothing() -> _Reach:
    return _Reach(lambda step: set())


def test_r2_the_loop_runs_seeding_first_and_replays_it(shop: Any) -> None:
    base_url, st = shop
    index = _shop_index()
    runner = ScenarioRunner(HttpClient(base_url), auth=AUTH, index=index)
    res = cover_change(
        index,
        ChangedLines.from_diff(DIFF),
        runner,
        _covering_nothing(),
        90,
        diff_text=DIFF,
        auth=AUTH,
    )
    seeds = ScenarioGenerator(index, auth=AUTH).seed_scenarios()
    assert res.seed_scenarios_run >= 3 and res.seed_requests <= 90 // 3
    first = [e.scenario_id for e in res.exchanges[: 3 * 3]]
    assert first[0] == seeds[0].id  # seeding comes before any probe
    # without hits, the impact set is exactly the seeding that ran: the replays rebuild its state
    ran = seeds[: res.seed_scenarios_run]
    # (a seed the app answered 400 joins as the scenario that was effectively sent)
    assert [s.repaired_from or s.id for s in res.impact_set] == [s.id for s in ran]
    assert any(set(o) == {"id", "sku"} for o in st["orders"].values())


def test_r2_explicit_scenarios_are_not_preceded_by_seeding(shop: Any) -> None:
    base_url, st = shop
    index = _shop_index()
    runner = ScenarioRunner(HttpClient(base_url), auth=AUTH, index=index)
    res = cover_change(
        index, ChangedLines.from_diff(DIFF), runner, _covering_nothing(), 30, diff_text=DIFF,
        auth=AUTH, scenarios=[_read(1)],
    )  # fmt: skip
    assert res.seed_scenarios_run == 0 and st["customers"] == {} and st["orders"] == {}


# --------------------------------------------------------------------------
# item 2: aim at the changed code (register R2)
# --------------------------------------------------------------------------

PAYOUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["account", "amount"],
    "properties": {
        "account": {"type": "string"},
        "amount": {"type": "integer", "minimum": -500000},
        "memo": {"type": "string"},
        "reference": {"type": "string"},
        "priority": {"type": "integer"},
        "currency": {"type": "string", "enum": ["EUR", "USD", "DKK"]},
    },
}


def _aim_index() -> AppIndex:
    ops = [
        _op("GET", "/health"),
        _op(
            "POST",
            "/ledgers",
            {"type": "object", "required": ["title"], "properties": {"title": {"type": "string"}}},
        ),
        _op("GET", "/ledgers/{ledger_id}"),
        _op("POST", "/payouts", PAYOUT_SCHEMA),
        _op("GET", "/reports"),
        _op("GET", "/audit-log"),
    ]
    return AppIndex(app="ledger", commit_sha="0" * 40, operations=ops)


def _payout_rule(step: Step) -> set[int]:
    """billing.py:40 on any payout; 41 only for a payout of zero or less; 42 never."""
    if step.method == "POST" and step.path == "/payouts":
        amount = step.body.get("amount") if isinstance(step.body, dict) else None
        return {40, 41} if isinstance(amount, int) and amount <= 0 else {40}
    return set()


def _run_aim(budget: int, rule: Any = _payout_rule) -> tuple[Any, list[str]]:
    session = _Reach(rule)
    order: list[str] = []
    inner = _stub_run(session)

    def run(sc: Scenario) -> RunResult:
        order.append(sc.id)
        return inner(sc)  # type: ignore[no-any-return]

    res = cover_change(
        _aim_index(), ChangedLines.from_diff(DIFF), run, session, budget, diff_text=DIFF, seed=False
    )
    return res, order


def test_r2_the_operation_reaching_the_change_gets_most_of_the_remaining_budget() -> None:
    index = _aim_index()
    # by name alone another operation would be first: the identifiers of the change are "ledger"
    assert rank_operations(index.operations, DIFF)[0].path.startswith("/ledgers")
    res, _ = _run_aim(150)
    assert res.unexercised == {"billing.py": {"added": [42], "removed": []}}  # 42 is unreachable
    top = res.aim[0]
    assert top["operation"] == "POST /payouts" and top["reached_a_changed_line"] is True
    assert top["changed_lines_reached"] == 1  # only line 40 after the first pass
    left = top["budget_left"]
    spent = res.requests_by_operation["POST /payouts"] - 1  # minus its one first-pass request
    assert spent > left / 2, (spent, left)
    others = sum(v for k, v in res.requests_by_operation.items() if k != "POST /payouts")
    assert res.requests_used == spent + 1 + others
    assert res.requests_by_operation["GET /audit-log"] == 1  # never revisited
    assert res.requests_used <= 150


def test_r2_boundary_inputs_find_the_line_the_first_request_missed() -> None:
    res, _ = _run_aim(150)
    bodies = [e.request["body"] for e in res.exchanges if e.request["url_path"] == "/payouts"]
    assert any(b.get("amount") == 0 for b in bodies) and any(b.get("amount") == -1 for b in bodies)
    assert any(b.get("memo") is None and "memo" in b for b in bodies)  # null for an optional field
    assert any(b.get("reference") == "" for b in bodies)  # empty string
    assert any(len(str(b.get("memo"))) >= 5000 for b in bodies)  # very long string
    assert any("字" in str(b.get("memo")) for b in bodies)  # unicode
    assert any(b.get("amount") == 100 for b in bodies)  # a constant mined from the changed lines
    assert any(b.get("currency") == "USD" for b in bodies)  # another enum member
    assert any(b.get("amount") == 0 and b.get("account") == "" for b in bodies)  # two at once


def test_r2_the_loop_stops_as_soon_as_every_changed_line_is_covered() -> None:
    only = lambda step: {40, 41, 42} if _payout_rule(step) >= {41} else _payout_rule(step)  # noqa: E731
    res, order = _run_aim(150, only)
    assert res.covered and res.requests_to_cover == res.requests_used
    assert res.requests_used < 40 and len(order) == res.scenarios_run
    last = [e for e in res.exchanges if e.scenario_id == order[-1]][-1]
    assert last.request["body"]["amount"] <= 0  # the scenario that covered it was the last one


def test_r2_the_same_inputs_give_the_same_scenarios_in_the_same_order() -> None:
    a, order_a = _run_aim(120)
    b, order_b = _run_aim(120)
    assert order_a == order_b and a.aim == b.aim
    assert [e.to_dict() | {"elapsed_ms": 0} for e in a.exchanges] == [
        e.to_dict() | {"elapsed_ms": 0} for e in b.exchanges
    ]


def test_r2_shares_halve_down_the_ranking() -> None:
    def rule(step: Step) -> set[int]:
        if step.method == "POST" and step.path == "/payouts":
            return {40, 41}
        if step.path.startswith("/ledgers"):
            return {40}
        return set()

    res, _ = _run_aim(150, rule)
    ops = [a["operation"] for a in res.aim[:2]]
    assert ops[0] == "POST /payouts" and ops[1].split()[1].startswith("/ledgers")
    quotas = [a["request_quota"] for a in res.aim[:2]]
    assert quotas[0] == res.aim[0]["budget_left"] // 2 and quotas[1] >= quotas[0] // 2 - 1
    assert res.aim[0]["changed_lines_reached"] > res.aim[1]["changed_lines_reached"]


def test_r2_with_no_operation_reaching_the_change_the_name_ranking_decides() -> None:
    res, _ = _run_aim(80, lambda step: set())
    assert res.aim and res.aim[0]["reached_a_changed_line"] is False
    assert res.aim[0]["operation"].split()[1].startswith("/ledgers")
    assert len(res.aim) == 3


def test_r2_chains_run_create_read_list_update_delete_on_one_record() -> None:
    gen = ScenarioGenerator(_shop_index(), auth=AUTH)
    create = next(o for o in gen.index.operations if o.method == "POST" and o.path == "/orders")
    life = gen.chain_scenarios(create)[0]
    plain = [(s.method, s.path) for s in life.steps if s.path.startswith("/orders")]
    assert plain == [
        ("POST", "/orders"),
        ("GET", "/orders/{order_id}"),
        ("GET", "/orders"),
        ("PATCH", "/orders/{order_id}"),
        ("DELETE", "/orders/{order_id}"),
        ("GET", "/orders/{order_id}"),
    ]
    assert all(s.path_params == {"order_id": "${rec}"} for s in life.steps if "{" in s.path)
    delete = next(o for o in gen.index.operations if o.method == "DELETE" and "orders" in o.path)
    tails = [[(s.method, s.path) for s in sc.steps[-2:]] for sc in gen.chain_scenarios(delete)]
    assert [("DELETE", "/orders/{order_id}"), ("GET", "/orders/{order_id}")] in tails
    assert [("DELETE", "/orders/{order_id}"), ("DELETE", "/orders/{order_id}")] in tails
