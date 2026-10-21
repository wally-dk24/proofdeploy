"""Lean round 5, WO-R5-O: hard checks flag only regressions (R9); run-time values are noise (R10).

Realistic shapes only; stub exchanges and fake run windows, no model, no network.
"""

from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from proofdeploy.lean import run as lean_run
from proofdeploy.lean.compare import (
    HardCheckNotes,
    RunData,
    compare_runs,
    find_hard_failures,
    parse_timestamp,
)
from proofdeploy.lean.http import Exchange, Scenario, Step

OK_JSON = (200, {"results": [{"id": 7, "title": "Weekly report"}], "count": 1})
SERVER_ERROR = (500, "Internal Server Error")
TIMEOUT = (0, "transport error: TimeoutError: timed out")
CREATED = (201, {"id": 7})


def _scenario() -> Scenario:
    return Scenario(
        [
            Step("POST", "/api/items/"),
            Step("GET", "/api/items/?q=report"),
        ],
        "GET /api/items/",
    )


def _runs(
    rows: list[list[tuple[int, Any]]],
    logs: list[str] | None = None,
    ct: str = "application/json",
) -> list[RunData]:
    sc = _scenario()
    out = []
    for i, (label, per) in enumerate(zip(("base", "base2", "head", "head2"), rows, strict=True)):
        run = RunData(label)
        exs = [
            Exchange(
                sc.id,
                k,
                {"method": sc.steps[k].method, "url_path": sc.steps[k].path, "body": None},
                status,
                {"content-type": ct},
                body,
                1.0,
            )
            for k, (status, body) in enumerate(per)
        ]
        run.add(sc, exs)
        run.server_log = (logs or ["", "", "", ""])[i]
        out.append(run)
    return out


def _four(old: tuple[int, Any], new: tuple[int, Any]) -> list[list[tuple[int, Any]]]:
    return [[CREATED, old], [CREATED, old], [CREATED, new], [CREATED, new]]


# -- R9 item 1: status-type failures need a healthy request on BOTH old runs ------------


def test_old_500_then_new_timeout_is_a_preexisting_failure_not_a_hard_failure() -> None:
    notes = HardCheckNotes()
    hard, flaky = find_hard_failures(*_runs(_four(SERVER_ERROR, TIMEOUT)), notes=notes)
    assert (hard, flaky) == ([], [])
    assert [(n["kind"], n["request_id"]) for n in notes.preexisting_failures] == [
        ("preexisting_failure", f"{_scenario().id}:1")
    ]


def test_old_500_then_new_500_is_a_preexisting_failure() -> None:
    notes = HardCheckNotes()
    assert find_hard_failures(*_runs(_four(SERVER_ERROR, SERVER_ERROR)), notes=notes) == ([], [])
    assert len(notes.preexisting_failures) == 1


def test_old_transport_error_then_new_5xx_is_preexisting() -> None:
    assert find_hard_failures(*_runs(_four(TIMEOUT, SERVER_ERROR))) == ([], [])


def test_old_200_then_new_500_is_still_a_hard_failure() -> None:
    notes = HardCheckNotes()
    hard, _ = find_hard_failures(*_runs(_four(OK_JSON, SERVER_ERROR)), notes=notes)
    assert [h.kind for h in hard] == ["5xx"] and notes.preexisting_failures == []


def test_old_200_then_new_timeout_is_a_hard_failure_and_records_the_timeout() -> None:
    notes = HardCheckNotes()
    hard, _ = find_hard_failures(*_runs(_four(OK_JSON, TIMEOUT)), notes=notes)
    assert [h.kind for h in hard] == ["timeout"]
    assert notes.timeout_seconds == 10.0 and "10s" in hard[0].detail
    assert notes.to_dict()["timeout_seconds"] == 10.0


def test_old_side_broken_in_one_run_only_is_not_healthy_on_both() -> None:
    rows: list[list[tuple[int, Any]]] = [
        [CREATED, SERVER_ERROR],
        [CREATED, OK_JSON],
        [CREATED, TIMEOUT],
        [CREATED, TIMEOUT],
    ]
    assert find_hard_failures(*_runs(rows)) == ([], [])


# -- R9 item 2: "not parseable JSON" needs new 2xx and old 2xx JSON ------------------------


def test_new_endpoint_old_405_then_new_400_text_is_not_a_failure() -> None:
    old = (405, {"detail": 'Method "POST" not allowed.'})
    new = (400, "Bad Request: missing field")
    assert find_hard_failures(*_runs(_four(old, new))) == ([], [])


def test_old_200_json_then_new_400_text_is_not_a_failure() -> None:
    assert find_hard_failures(*_runs(_four(OK_JSON, (400, "Bad Request")))) == ([], [])


def test_old_404_json_then_new_200_html_is_not_a_failure() -> None:
    assert find_hard_failures(*_runs(_four((404, {"detail": "Not found."}), (200, "<html>")))) == (
        [],
        [],
    )


def test_old_200_json_then_new_200_html_is_a_failure() -> None:
    hard, _ = find_hard_failures(*_runs(_four(OK_JSON, (200, "<!doctype html><title>Login"))))
    assert [h.kind for h in hard] == ["schema_violation"]


# -- R9 item 3: tracebacks explained by an excluded request ---------------------------------

_TB = (
    "Traceback (most recent call last):\n"
    '  File "/app/views.py", line 41, in get\n    return render(item)\n'
    "TypeError: 'NoneType' object is not subscriptable\n"
)


def _log(*, tb_after: str) -> str:
    """Two requests in the log; the traceback sits after the request named ``tb_after``."""
    first = "[INFO] POST /api/items/ 201\n"
    second = "[INFO] GET /api/items/?q=report 500\n"
    return (
        first
        + (_TB if tb_after == "first" else "")
        + second
        + (_TB if tb_after == "second" else "")
    )


def test_traceback_inside_an_excluded_requests_window_is_not_a_failure_and_is_recorded() -> None:
    log = _log(tb_after="second")
    notes = HardCheckNotes()
    runs = _runs(_four(SERVER_ERROR, SERVER_ERROR), ["", "", log, log])
    assert find_hard_failures(*runs, notes=notes) == ([], [])
    assert {e["request_id"] for e in notes.excluded_tracebacks} == {f"{_scenario().id}:1"}
    assert "TypeError" in notes.excluded_tracebacks[0]["signature"]


def test_traceback_outside_the_excluded_window_still_counts() -> None:
    log = _log(tb_after="first")  # belongs to POST /api/items/, which is healthy
    runs = _runs(_four(SERVER_ERROR, SERVER_ERROR), ["", "", log, log])
    hard, _ = find_hard_failures(*runs)
    assert [(h.kind, h.request_id) for h in hard] == [("new_exception", "server_log")]


def test_traceback_after_the_next_request_started_is_not_explained_by_the_excluded_one() -> None:
    log = "[INFO] POST /api/items/ 500\n[INFO] GET /api/items/?q=report 200\n" + _TB
    rows = [[SERVER_ERROR, OK_JSON]] * 2 + [[SERVER_ERROR, OK_JSON]] * 2
    notes = HardCheckNotes()
    hard, _ = find_hard_failures(*_runs(rows, ["", "", log, log]), notes=notes)
    assert [h.kind for h in hard] == ["new_exception"] and notes.excluded_tracebacks == []


def test_traceback_with_no_excluded_request_still_counts() -> None:
    log = _log(tb_after="second")
    hard, _ = find_hard_failures(*_runs(_four(OK_JSON, OK_JSON), ["", "", log, log]))
    assert [h.kind for h in hard] == ["new_exception"]


# -- R10 item 6: run-time values ------------------------------------------------------------

T0 = 1_760_000_000.0  # 2025-10-09T08:53:20Z
T1 = 1_760_000_600.0


def _start(i: int) -> float:
    return T0 + 1000 * (i // 2) + 100 * (i % 2)  # four distinct windows


def _starts() -> list[float]:
    return [_start(i) for i in range(4)]


def _timed(vals: list[Any]) -> list[RunData]:
    sc = _scenario()
    runs = []
    for i, (label, v) in enumerate(zip(("base", "base2", "head", "head2"), vals, strict=True)):
        run = RunData(label)
        body = {"id": 7, "date_joined": v}
        run.add(
            sc,
            [
                Exchange(
                    sc.id,
                    0,
                    {"method": "POST", "url_path": "/api/items/", "body": None},
                    201,
                    {"content-type": "application/json"},
                    {"id": 7},
                    1.0,
                ),
                Exchange(
                    sc.id,
                    1,
                    {"method": "GET", "url_path": "/api/items/?q=report", "body": None},
                    200,
                    {"content-type": "application/json"},
                    body,
                    1.0,
                ),
            ],
        )
        run.started_at = _start(i)
        run.finished_at = run.started_at + 30
        runs.append(run)
    return runs


def _iso(ts: float, *, z: bool = True) -> str:
    from datetime import datetime, timezone

    s = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.123456")
    return s + ("Z" if z else "")


def test_iso_timestamps_inside_their_own_run_windows_are_noise_run_time_value() -> None:
    runs = _timed([_iso(s + 10) for s in _starts()])
    cmp = compare_runs(*runs)
    assert cmp.differences == []
    items = [n for n in cmp.noise if n.field_path == "date_joined"]
    assert len(items) == 1 and items[0].detail == "run_time_value"
    assert set(cmp.run_windows) == {"base", "base2", "head", "head2"}
    assert cmp.run_windows["base"][0] < cmp.run_windows["base"][1]


def test_epoch_seconds_and_milliseconds_inside_the_windows_are_noise() -> None:
    starts = _starts()
    for scale in (1, 1000):
        runs = _timed([int((s + 12) * scale) for s in starts])
        cmp = compare_runs(*runs)
        assert cmp.differences == [], scale
        assert [n.detail for n in cmp.noise if n.field_path == "date_joined"] == ["run_time_value"]


def test_a_fixed_date_changed_by_the_code_is_still_a_difference() -> None:
    fixed_old, fixed_new = "2020-01-01T00:00:00Z", "2021-06-30T12:00:00Z"
    cmp = compare_runs(*_timed([fixed_old, fixed_old, fixed_new, fixed_new]))
    assert [(d.field_path, d.before, d.after) for d in cmp.differences] == [
        ("date_joined", fixed_old, fixed_new)
    ]


def test_a_value_outside_its_own_run_window_is_never_labelled_run_time_value() -> None:
    starts = _starts()
    old = "2020-01-01T00:00:00Z"
    # old fixed, new moves with run time: not all four in their windows
    cmp = compare_runs(*_timed([old, old, _iso(starts[2] + 5), _iso(starts[3] + 5)]))
    assert not [n for n in cmp.noise if n.detail == "run_time_value"]
    # head's value lies far outside head's own window (a run-time-looking date, not a run-time one)
    late = [_iso(starts[0] + 10), _iso(starts[1] + 10), _iso(starts[2] + 5000)]
    cmp = compare_runs(*_timed([*late, late[2]]))
    assert not [n for n in cmp.noise if n.detail == "run_time_value"]


def test_parse_timestamp_accepts_iso_and_epoch_only() -> None:
    assert parse_timestamp("2025-10-09T08:53:20Z") == T0
    assert parse_timestamp("2025-10-09 08:53:20+00:00") == T0
    assert parse_timestamp("2025-10-09T08:53:20") == T0  # naive = UTC
    assert parse_timestamp(int(T0)) == T0 and parse_timestamp(int(T0 * 1000)) == T0
    for not_time in (True, 7, 12.5, "Weekly report", "2025", "20251009", None, 3_000_000):
        assert parse_timestamp(not_time) is None


# -- R10 item 5: the clock in the sandbox ---------------------------------------------------


def _probe_with(monkeypatch: pytest.MonkeyPatch, present: set[str]) -> list[str]:
    seen: list[str] = []

    def fake_run(argv: list[str], **_kw: Any) -> Any:
        seen.append(argv[-1])
        return SimpleNamespace(returncode=0 if argv[-1] in present else 1)

    monkeypatch.setattr(subprocess, "run", fake_run)
    return seen


def test_faketime_probe_finds_the_library_wherever_the_image_keeps_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alpine = "/usr/lib/faketime/libfaketime.so.1"
    seen = _probe_with(monkeypatch, {alpine})
    assert lean_run.SandboxSide._probe_faketime("pd-c") == alpine
    assert seen[0] == lean_run.FAKETIME_LIB and seen[-1] == alpine
    _probe_with(monkeypatch, set())
    assert lean_run.SandboxSide._probe_faketime("pd-c") is None


def _side(tmp_path: Path, ready: Any) -> Any:
    db = tmp_path / "app.sqlite"
    sqlite3.connect(db).close()
    side = object.__new__(lean_run.SandboxSide)
    side.db = db
    side.work = tmp_path / "work"
    side.seed_db = side.work / "seed.sqlite"
    side._ready = ready
    side.hook = lean_run.SandboxHook(overlay={}, contract_env={})
    side._clock = {"used": False}
    return side


def test_prepare_preloads_the_probed_library_and_records_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    alpine = "/usr/lib/faketime/libfaketime.so.1"
    _probe_with(monkeypatch, {alpine})
    side = _side(tmp_path, SimpleNamespace(name="pd-c"))
    side.prepare()
    assert side.hook.clock_env == {"LD_PRELOAD": alpine, "FAKETIME": lean_run.FREEZE_TIME}
    assert side._clock["used"] is True and side._clock["lib"] == alpine


def test_prepare_without_the_library_says_what_was_probed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _probe_with(monkeypatch, set())
    side = _side(tmp_path, SimpleNamespace(name="pd-c"))
    side.prepare()
    assert side.hook.clock_env == {}
    assert side._clock["used"] is False and side._clock["probed"] == list(
        lean_run.FAKETIME_LIB_CANDIDATES
    )


def test_prepare_with_no_running_container_does_not_claim_the_image_lacks_it(
    tmp_path: Path,
) -> None:
    side = _side(tmp_path, None)
    side.prepare()
    assert "image" not in side._clock["reason"]
