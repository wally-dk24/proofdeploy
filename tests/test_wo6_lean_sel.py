"""Offline tests for R-LEAN-SEL in scripts/wo6/regc_bug_select.py and for
experiments/lean-poc/write_bug_unit.py (WO-R2-W; D267).

Temp git repos and fakes only: no network, no model, no workflow. Changed
paths are the only thing lean check L reads.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from test_wo6_bug_select import (
    FakeReady,
    SizeScript,
    add_tag,
    app_dict,
    commit,
    init_repo,
    side_outcome,
    ts,
)
from test_wo6_bug_select import mod as mod  # the loaded regc_bug_select

WRITER = Path(__file__).resolve().parents[1] / "experiments" / "lean-poc" / "write_bug_unit.py"
_wspec = importlib.util.spec_from_file_location("write_bug_unit", WRITER)
assert _wspec is not None and _wspec.loader is not None
writer = importlib.util.module_from_spec(_wspec)
sys.modules["write_bug_unit"] = writer
_wspec.loader.exec_module(writer)

SIGNED_SHA = "abc78a10fb5fa3af21b88ed2f43e58cbb8c374a0b1550c30ec8bc830f0aaa3ce"


def lean_history(
    tmp_path: Path,
    base: dict[str, str],
    b_files: dict[str, str],
    fix_files: dict[str, str],
) -> dict[str, str]:
    """base (Jan) -> B (Feb, tagged v1.0) -> mid (Aug 1) -> fix (Aug 15)."""
    repo = init_repo(tmp_path)
    s: dict[str, Any] = {"repo": repo}
    s["base"] = commit(repo, "Initial commit", ts(2026, 1, 10), base)
    s["b"] = commit(repo, "Introduce the thing", ts(2026, 2, 1), b_files)
    add_tag(repo, "v1.0", ts(2026, 2, 2))
    s["mid"] = commit(repo, "Add other module", ts(2026, 8, 1), {"src/other.py": "x = 1\n"})
    s["fix"] = commit(repo, "Fix the thing", ts(2026, 8, 15), fix_files)
    return s


def backend_history(tmp_path: Path) -> dict[str, Any]:
    return lean_history(
        tmp_path,
        {"src/app.py": "l1\nl2\nl3\n"},
        {"src/app.py": "l1\nl2-bug\nl3\n"},
        {"src/app.py": "l1\nl2-fixed\nl3\n"},
    )


def all_ready(s: dict[str, Any]) -> FakeReady:
    return FakeReady({s["base"], s["b"], s["mid"], s["fix"]})


def lean(s: dict[str, Any], ready: FakeReady, **kw: Any) -> dict[str, Any]:
    kw.setdefault("prompt_size", None)
    kw.setdefault("layout_files", [])
    return mod.select_fix(
        repo=mod.AppRepo(s["repo"]),
        app=app_dict(s["fix"]),
        ready_pair=ready,
        main_sha="f" * 40,
        script_sha256="s" * 64,
        rule="lean",
        language="python",
        **kw,
    )


def only_walked(record: dict[str, Any]) -> dict[str, Any]:
    fixes = [e for e in record["walked"] if e["sha"] and e["failed_check"] != "a"]
    assert len(fixes) == 1
    return fixes[0]


# ---------------------------------------------------------------------
# Registered text.
# ---------------------------------------------------------------------


def test_registered_text_sha256_is_the_signed_one() -> None:
    path = Path(__file__).resolve().parents[1] / "docs" / "registration" / "r-lean-sel.md"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == SIGNED_SHA
    assert mod.LEAN_SEL_SHA256 == SIGNED_SHA
    assert mod.lean_registered_sha256() == SIGNED_SHA


def test_altered_registered_text_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = tmp_path / "r-lean-sel.md"
    fake.write_text("not the signed text\n")
    monkeypatch.setattr(mod, "LEAN_SEL_PATH", fake)
    with pytest.raises(mod.HarnessError, match="not the signed"):
        mod.lean_registered_sha256()


# ---------------------------------------------------------------------
# The option surface.
# ---------------------------------------------------------------------


def run_main(args: list[str]) -> int:
    try:
        return int(mod.main(args))
    except SystemExit as exc:
        return int(exc.code or 0)


def test_rule_lean_requires_language(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    rc = run_main(["--rule", "lean", "--out", str(tmp_path / "r.json")])
    assert rc == 2
    assert "--rule lean requires --lean-language" in capsys.readouterr().err


@pytest.mark.parametrize("language", ["typescript", "ruby", "Python", ""])
def test_rule_lean_refuses_other_languages(
    language: str, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    rc = run_main(
        ["--rule", "lean", "--lean-language", language, "--out", str(tmp_path / "r.json")]
    )
    assert rc == 2
    assert "not defined yet (R-LEAN-SEL 4)" in capsys.readouterr().err


def test_lean_language_without_lean_rule_is_a_usage_error(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    rc = run_main(["--lean-language", "python", "--out", str(tmp_path / "r.json")])
    assert rc == 2
    assert "only valid with --rule lean" in capsys.readouterr().err


def test_select_fix_refuses_other_language(tmp_path: Path) -> None:
    s = backend_history(tmp_path)
    with pytest.raises(mod.HarnessError, match="not defined yet"):
        mod.select_fix(
            repo=mod.AppRepo(s["repo"]),
            app=app_dict(s["fix"]),
            ready_pair=all_ready(s),
            prompt_size=None,
            main_sha=None,
            script_sha256="s" * 64,
            rule="lean",
            language="go",
        )


# ---------------------------------------------------------------------
# Check L, from path lists.
# ---------------------------------------------------------------------


def test_changed_paths_z_reads_names_only_and_survives_odd_names(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    a = commit(repo, "one", ts(2026, 1, 1), {"src/a.py": "1\n"})
    b = commit(repo, "two", ts(2026, 1, 2), {'src/we ird/é"q.py': "2\n", "src/a.py": "3\n"})
    got = mod.AppRepo(repo).changed_paths_z(a, b)
    assert sorted(got) == ["src/a.py", 'src/we ird/é"q.py']


def test_backend_source_count_rules() -> None:
    paths = [
        "src/app.py",  # backend
        "src/app/migrations/0001_x.py",  # migration: excluded
        "src/tests/test_x.py",  # TEST
        "src-ui/x.ts",  # SOURCE but not python
        "src-ui/x.html",
        "docs/y.py",  # DOC
        "src/data.json",  # OTHER
    ]
    assert mod.backend_source_count(paths, "python") == 1
    counts = mod.lean_path_counts(paths, "python")
    assert counts == {"SOURCE": 4, "TEST": 1, "DOC": 1, "OTHER": 1, "backend_source": 1}


def test_lean_selects_backend_unit_and_records_it(tmp_path: Path) -> None:
    s = backend_history(tmp_path)
    ready = all_ready(s)
    rec = lean(s, ready)
    assert rec["outcome"] == "selected"
    sel = rec["selected"]
    assert sel["b_parent"] == s["base"]
    assert sel["b"] == s["b"]
    assert sel["fix_parent"] == s["mid"]
    assert sel["fix"] == s["fix"]
    assert sel["tag"] == "v1.0"
    assert sel["b_committer_date"].startswith("2026-02-01")
    assert sel["tag_created"].startswith("2026-02-02")
    assert rec["rule"] == "R-LEAN-SEL"
    assert rec["rule_sha256"] == SIGNED_SHA
    assert rec["language"] == "python"
    entry = only_walked(rec)
    assert entry["checks_run"] == ["a", "b", "c", "L", "L3", "d"]
    assert "R-SIZE" in entry["e"]
    assert "author_prompt_chars" not in entry
    assert entry["l_counts"]["b"]["backend_source"] == 1
    assert entry["l_counts"]["fix"]["backend_source"] == 1


def test_frontend_only_b_is_rejected_at_l1(tmp_path: Path) -> None:
    s = lean_history(
        tmp_path,
        {"src/app.py": "l1\n", "src/ui/a.ts": "t1\nt2\n"},
        {"src/ui/a.ts": "t1\nt2-bug\n", "src/ui/a.html": "<p>bug</p>\n"},
        {"src/ui/a.ts": "t1\nt2-fixed\n"},
    )
    ready = all_ready(s)
    rec = lean(s, ready)
    entry = only_walked(rec)
    assert rec["outcome"] == "no_unit_selected"
    assert entry["failed_check"] == "L1"
    assert entry["reason"] == "L1: no backend source in B"
    assert entry["l_counts"]["b"] == {
        "SOURCE": 2,
        "TEST": 0,
        "DOC": 0,
        "OTHER": 0,
        "backend_source": 0,
    }
    assert ready.calls == []  # no readiness dispatch for an L rejection
    assert rec["rejected_by_check"]["L1"] == 1


def test_backend_b_but_frontend_only_fix_is_rejected_at_l2(tmp_path: Path) -> None:
    s = lean_history(
        tmp_path,
        {"src/app.py": "l1\n", "src/ui/a.ts": "t1\nt2\n"},
        {"src/app.py": "l1-bug\n", "src/ui/a.ts": "t1\nt2-bug\n"},
        {"src/ui/a.ts": "t1\nt2-fixed\n"},
    )
    ready = all_ready(s)
    rec = lean(s, ready)
    entry = only_walked(rec)
    assert entry["failed_check"] == "L2"
    assert entry["reason"] == "L2: no backend source in fix"
    assert entry["l_counts"]["b"]["backend_source"] == 1
    assert entry["l_counts"]["fix"]["backend_source"] == 0
    assert ready.calls == []
    assert rec["rejected_by_check"]["L2"] == 1


def test_migration_only_b_is_rejected_at_l1(tmp_path: Path) -> None:
    s = lean_history(
        tmp_path,
        {"src/app/migrations/0001_a.py": "m1\nm2\n", "src/app.py": "a1\n"},
        {"src/app/migrations/0001_a.py": "m1\nm2-bug\n"},
        {"src/app/migrations/0001_a.py": "m1\nm2-fixed\n", "src/app.py": "a2\n"},
    )
    rec = lean(s, all_ready(s))
    entry = only_walked(rec)
    assert entry["failed_check"] == "L1"
    assert entry["l_counts"]["b"]["SOURCE"] == 1
    assert entry["l_counts"]["b"]["backend_source"] == 0


def test_journal_has_check_letters_and_counts_but_no_paths(tmp_path: Path) -> None:
    s = backend_history(tmp_path)
    jdir = tmp_path / "journal"
    journal, _r, _c = mod.Journal.open(
        jdir,
        script_sha256="s" * 64,
        main_sha=None,
        app_repo="paperless-ngx/paperless-ngx",
        app_head_sha=s["fix"],
    )
    lean(s, all_ready(s), journal=journal)
    text = (jdir / "walk.jsonl").read_text() + (jdir / "attempts.jsonl").read_text()
    assert "app.py" not in text and "other.py" not in text
    assert '"checks_run"' in text and '"l_counts"' in text
    pairs = [json.loads(ln)["pair"] for ln in (jdir / "attempts.jsonl").read_text().splitlines()]
    assert pairs[:2] == ["b", "b"] and set(pairs[2:]) == {"fix"}


# ---------------------------------------------------------------------
# R-SIZE: regc keeps it, lean never calls it.
# ---------------------------------------------------------------------


def test_regc_path_still_calls_size_check_and_never_l(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = backend_history(tmp_path)

    def boom(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("check L touched in regc mode")

    monkeypatch.setattr(mod.AppRepo, "changed_paths_z", boom)
    size = SizeScript([1234])
    rec = mod.select_fix(
        repo=mod.AppRepo(s["repo"]),
        app=app_dict(s["fix"]),
        ready_pair=all_ready(s),
        prompt_size=size,
        main_sha=None,
        script_sha256="s" * 64,
    )
    assert rec["outcome"] == "selected"
    assert size.calls == [(s["base"], s["b"])]
    assert rec["rule"].startswith("REG-C item 7")
    assert "rule_sha256" not in rec and "language" not in rec
    entry = only_walked(rec)
    assert "checks_run" not in entry and "l_counts" not in entry
    assert rec["rejected_by_check"] == {"a": 1, "b": 0, "c": 0, "d": 0, "e": 0, "selected": 1}


def test_lean_never_calls_the_size_check(tmp_path: Path) -> None:
    s = backend_history(tmp_path)

    def size_boom(_p: str, _b: str) -> int:
        raise AssertionError("R-SIZE check called in lean mode")

    rec = lean(s, all_ready(s), prompt_size=size_boom)
    assert rec["outcome"] == "selected"


# ---------------------------------------------------------------------
# Check (d): B pair first, then the fix pair.
# ---------------------------------------------------------------------


def test_d_checks_b_pair_before_fix_pair(tmp_path: Path) -> None:
    s = backend_history(tmp_path)
    ready = all_ready(s)
    lean(s, ready)
    assert ready.calls == [
        (s["base"], s["b"], ("fix^", "fix")),
        (s["mid"], s["fix"], ("fix^", "fix")),
    ]


def test_b_pair_not_ready_rejects_without_running_the_fix_pair(tmp_path: Path) -> None:
    s = backend_history(tmp_path)
    ready = FakeReady({s["base"], s["mid"], s["fix"]})  # B itself not READY
    rec = lean(s, ready)
    entry = only_walked(rec)
    assert entry["failed_check"] == "d"
    assert entry["reason"] == "B pair: B is not READY"
    assert entry["b_parent_ready"] is True and entry["b_ready"] is False
    assert ready.calls == [(s["base"], s["b"], ("fix^", "fix"))]
    assert "readiness" not in entry


def test_fix_pair_not_ready_rejects_after_b_pair(tmp_path: Path) -> None:
    s = backend_history(tmp_path)
    ready = FakeReady({s["base"], s["b"], s["fix"]})  # fix^ (mid) not READY
    rec = lean(s, ready)
    entry = only_walked(rec)
    assert entry["failed_check"] == "d"
    assert entry["reason"] == "fix pair: fix^ is not READY"
    assert len(ready.calls) == 2


def test_b_pair_environment_failure_retries_then_stops_naming_the_pair(tmp_path: Path) -> None:
    s = backend_history(tmp_path)
    env = side_outcome(typed_reason="github readiness: run failed")
    ready = FakeReady({s["base"], s["mid"], s["fix"]}, scripted={s["b"]: [env]})
    rec = lean(s, ready)
    assert rec["outcome"] == "stopped"
    assert rec["stopped"]["pair"] == "b"
    assert rec["stopped"]["retries_exhausted"] is True
    assert rec["stopped"]["sha"] == s["fix"]
    # 3 attempts total for the failing side, none for the fix pair.
    assert all(c[1] == s["b"] or c[0] == s["base"] for c in ready.calls)
    assert len(ready.calls) == 3


def test_b_pair_our_gap_stops_and_is_not_rejected(tmp_path: Path) -> None:
    s = backend_history(tmp_path)
    gap = side_outcome(typed_reason="x (our gap)")
    ready = FakeReady({s["base"]}, scripted={s["b"]: [gap]})
    rec = lean(s, ready)
    assert rec["outcome"] == "stopped"
    assert rec["stopped"]["class"] == "our_gap"
    assert rec["stopped"]["pair"] == "b"
    assert s["fix"] not in [e["sha"] for e in rec["walked"]]


def test_fix_pair_stop_names_the_fix_pair(tmp_path: Path) -> None:
    s = backend_history(tmp_path)
    gap = side_outcome(typed_reason="x (our gap)")
    ready = FakeReady({s["base"], s["b"], s["fix"]}, scripted={s["mid"]: [gap]})
    rec = lean(s, ready)
    assert rec["outcome"] == "stopped"
    assert rec["stopped"]["pair"] == "fix"


def test_workflow_run_ids_are_journaled_per_pair(tmp_path: Path) -> None:
    s = backend_history(tmp_path)
    inner = all_ready(s)

    def with_ids(p: str, f: str, roles: tuple[str, ...]) -> dict[str, dict[str, Any]]:
        out = inner(p, f, roles)
        for o in out.values():
            o["workflow_run_id"] = "RUN-" + f[:6]
        return out

    jdir = tmp_path / "j"
    journal, _r, _c = mod.Journal.open(
        jdir,
        script_sha256="s" * 64,
        main_sha=None,
        app_repo="paperless-ngx/paperless-ngx",
        app_head_sha=s["fix"],
    )
    lean(s, with_ids, journal=journal)  # type: ignore[arg-type]
    rows = [json.loads(ln) for ln in (jdir / "attempts.jsonl").read_text().splitlines()]
    assert {(r["pair"], r["workflow_run_id"]) for r in rows} == {
        ("b", "RUN-" + s["b"][:6]),
        ("fix", "RUN-" + s["fix"][:6]),
    }


# ---------------------------------------------------------------------
# 200 walked without a selection.
# ---------------------------------------------------------------------


def test_stop_at_200_walked_is_no_unit_selected_with_no_fallback(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    commit(repo, "Initial commit", ts(2026, 1, 10), {"src/app.py": "l1\nl2\n"})
    b = commit(repo, "Introduce the counter", ts(2026, 2, 1), {"src/app.py": "l1\nl2-bug\n"})
    add_tag(repo, "v1.0", ts(2026, 2, 2))
    head = b
    for i in range(200):
        head = commit(
            repo, f"Chore step {i}", ts(2026, 8, 1) + i * 60, {f"src/mod{i}.py": f"x = {i}\n"}
        )
    qualifying = commit(repo, "Fix the counter", ts(2026, 8, 20), {"src/app.py": "l1\nl2-fixed\n"})
    s = {"repo": repo, "fix": qualifying}
    rec = lean(s, FakeReady({head, qualifying, b}))
    assert rec["outcome"] == "no_unit_selected"
    assert rec["walked_count"] == 200
    assert rec["selected"] is None
    assert qualifying not in [e["sha"] for e in rec["walked"]]
    assert "no fallback" in rec["next"]
    assert rec["rule"] == "R-LEAN-SEL"


# ---------------------------------------------------------------------
# main(): lean wiring, no size builder.
# ---------------------------------------------------------------------


def test_main_lean_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "hist"
    root.mkdir()
    s = backend_history(root)
    survivors = tmp_path / "survivors.json"
    survivors.write_text(
        json.dumps(
            [
                {
                    "full_name": "o/r",
                    "head_sha": s["fix"],
                    "default_branch": "main",
                    "stacks": {"python": {"passed": True, "app_path": "src"}},
                }
            ]
        )
    )
    monkeypatch.setattr(mod, "_clone_app", lambda *a, **k: mod.AppRepo(s["repo"]))
    monkeypatch.setattr(mod, "_lean_layout_for_app", lambda _r, _a: ["app.py"])
    ready = all_ready(s)
    monkeypatch.setattr(mod, "make_github_pair_checker", lambda **k: ready)

    def no_builder(**_k: Any) -> Any:
        raise AssertionError("size builder constructed in lean mode")

    monkeypatch.setattr(mod, "make_author_prompt_size_builder", no_builder)
    out = tmp_path / "out" / "rec.json"
    rc = mod.main(
        [
            "--repo",
            "o/r",
            "--survivors",
            str(survivors),
            "--out",
            str(out),
            "--main-sha",
            "f" * 40,
            "--check-d",
            "github",
            "--rule",
            "lean",
            "--lean-language",
            "python",
        ]
    )
    text = capsys.readouterr().out
    assert rc == 0
    assert "tally: a=1 b=0 c=0 L1=0 L2=0 L3=0 d=0 selected=1" in text
    assert "Fix the" not in text
    rec = json.loads(out.read_text())
    assert rec["rule"] == "R-LEAN-SEL" and rec["language"] == "python"
    header = json.loads((out.with_name("rec-journal") / "header.json").read_text())
    assert header["rule"] == "R-LEAN-SEL" and header["language"] == "python"
    assert header["layout_files"] == ["app.py"]
    assert rec["layout_files"] == ["app.py"]


# ---------------------------------------------------------------------
# write_bug_unit.py.
# ---------------------------------------------------------------------

SHAS = {k: c * 40 for k, c in (("b_parent", "1"), ("b", "2"), ("fix_parent", "3"), ("fix", "4"))}


def write_record(path: Path, **over: Any) -> bytes:
    rec: dict[str, Any] = {
        "kind": "wo6-bug-select",
        "rule": "R-LEAN-SEL",
        "outcome": "selected",
        "app": {"repo": "paperless-ngx/paperless-ngx"},
        "selected": {**SHAS, "tag": "v1", "tag_created": "x", "b_committer_date": "y"},
        "walked": [{"sha": "9" * 40, "subject": "SECRET-SUBJECT-TEXT"}],
    }
    rec.update(over)
    data = (json.dumps(rec, indent=1) + "\n").encode()
    path.write_bytes(data)
    return data


def test_write_bug_unit_writes_exactly_seven_keys(tmp_path: Path) -> None:
    rp = tmp_path / "rec.json"
    data = write_record(rp)
    out = tmp_path / "unit.json"
    writer.write_unit(rp, "selections/rec.json", out)
    unit = json.loads(out.read_text())
    assert list(unit) == [
        "unit_repo",
        "b_parent",
        "b",
        "fix_parent",
        "fix",
        "selection_record",
        "selection_record_sha256",
    ]
    assert unit["unit_repo"] == "paperless-ngx/paperless-ngx"
    assert {k: unit[k] for k in SHAS} == SHAS
    assert unit["selection_record"] == "selections/rec.json"
    assert unit["selection_record_sha256"] == hashlib.sha256(data).hexdigest()
    assert "SECRET-SUBJECT-TEXT" not in out.read_text()


@pytest.mark.parametrize(
    "over",
    [
        {"rule": "REG-C item 7"},
        {"rule": None},
        {"outcome": "no_unit_selected"},
        {"outcome": "stopped"},
        {"selected": None},
        {"selected": {**SHAS, "b_parent": "not-a-sha"}},
    ],
)
def test_write_bug_unit_refuses(tmp_path: Path, over: dict[str, Any]) -> None:
    rp = tmp_path / "rec.json"
    write_record(rp, **over)
    out = tmp_path / "unit.json"
    with pytest.raises(writer.Refusal):
        writer.write_unit(rp, "rel/rec.json", out)
    assert not out.exists()


def test_write_bug_unit_main_never_prints_subjects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    rp = tmp_path / "rec.json"
    write_record(rp)
    monkeypatch.setattr(writer, "OUT_PATH", tmp_path / "paperless-bug-unit.json")
    assert writer.main([str(rp), "rel/rec.json"]) == 0
    cap = capsys.readouterr()
    assert "SECRET-SUBJECT-TEXT" not in cap.out + cap.err
    bad = tmp_path / "bad.json"
    write_record(bad, rule="REG-C")
    assert writer.main([str(bad), "rel/bad.json"]) == 2
    cap = capsys.readouterr()
    assert "SECRET-SUBJECT-TEXT" not in cap.out + cap.err
    assert writer.main([]) == 2


def test_write_bug_unit_default_output_path() -> None:
    assert writer.OUT_PATH.name == "paperless-bug-unit.json"
    assert writer.OUT_PATH.parent.name == "lean-poc"


def test_every_lean_walk_repo_with_a_contract_is_a_readiness_workflow_option() -> None:
    """Check (d) runs through readiness.yml pair mode: each walkable repo needs an option."""
    import yaml

    root = Path(__file__).resolve().parents[1]
    survivors = json.loads(
        (
            root / "docs/registration/wo6-regc-add2-closeout-2026-10-17/regc-add2-survivors.json"
        ).read_text(encoding="utf-8")
    )
    stems = {p.stem for p in (root / "readiness/contracts").glob("*.yml")}
    walkable = sorted(
        s["full_name"]
        for s in survivors
        if s.get("survivor") and s["full_name"].replace("/", "__") in stems
    )
    assert "sissbruecker/linkding" in walkable
    wf = yaml.safe_load((root / ".github/workflows/readiness.yml").read_text(encoding="utf-8"))
    options = wf[True]["workflow_dispatch"]["inputs"]["repo"]["options"]  # YAML "on" parses True
    assert [r for r in walkable if r not in options] == []


# ---------------------------------------------------------------------
# R-LEAN-SEL Amendment 1: check L3 (the unit must fit the layout).
# ---------------------------------------------------------------------

A1_SHA = "80961a5a45b9e0d905850780ac767b57f03942881f64ab3d3dfa0b82a7b63552"
ROOT = Path(__file__).resolve().parents[1]
LINKDING_SHAPE = {
    "pyproject.toml": '[project]\nname = "x"\nversion = "0"\nrequires-python = ">=3.13"\n',
    "uv.lock": "version = 1\n",
    "bookmarks/settings/prod.py": "DEBUG = False\n",
    "bookmarks/settings/__init__.py": "",
    "bookmarks/other.py": "x = 1\n",
}


def linkding_contract() -> Any:
    from proofdeploy.yml import load_contract

    return load_contract(ROOT / "readiness" / "contracts" / "sissbruecker__linkding.yml")


def layout_history(tmp_path: Path) -> dict[str, Any]:
    """Layout files from the first commit, then B, mid, fix."""
    repo = init_repo(tmp_path)
    s: dict[str, Any] = {"repo": repo}
    first = {k: v for k, v in LINKDING_SHAPE.items()}
    s["base"] = commit(repo, "Initial commit", ts(2026, 1, 10), first)
    s["b"] = commit(repo, "Introduce the thing", ts(2026, 2, 1), {"bookmarks/app.py": "l1\nbug\n"})
    add_tag(repo, "v1.0", ts(2026, 2, 2))
    s["mid"] = commit(repo, "Add other module", ts(2026, 8, 1), {"bookmarks/other.py": "x = 2\n"})
    s["fix"] = commit(repo, "Fix the thing", ts(2026, 8, 15), {"bookmarks/app.py": "l1\nfixed\n"})
    return s


def layout_lean(s: dict[str, Any], ready: FakeReady, layout: list[str]) -> dict[str, Any]:
    app = app_dict(s["fix"])
    app["app_path"] = "."
    return mod.select_fix(
        repo=mod.AppRepo(s["repo"]),
        app=app,
        ready_pair=ready,
        prompt_size=None,
        main_sha="f" * 40,
        script_sha256="s" * 64,
        rule="lean",
        language="python",
        layout_files=layout,
    )


def test_a1_registered_text_sha256_is_the_signed_one() -> None:
    path = ROOT / "docs" / "registration" / "r-lean-sel-a1.md"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == A1_SHA


def test_layout_files_of_a_linkding_shaped_tree(tmp_path: Path) -> None:
    s = layout_history(tmp_path)
    files = mod.lean_layout_files(mod.AppRepo(s["repo"]), s["fix"], linkding_contract(), ".")
    assert files == ["bookmarks/settings/prod.py", "pyproject.toml", "uv.lock"]


def test_layout_env_module_as_package_init_and_unresolved_ignored(tmp_path: Path) -> None:
    from proofdeploy.yml import load_contract

    s = layout_history(tmp_path)
    contract = load_contract(ROOT / "readiness" / "contracts" / "sissbruecker__linkding.yml")
    contract.env = {
        "A": "bookmarks.settings",  # package: bookmarks/settings/__init__.py
        "B": "no.such.module",  # resolves to nothing: ignored
        "C": "not a module",  # not dotted: ignored
        "D": "bookmarks.settings.prod",
    }
    files = mod.lean_layout_files(mod.AppRepo(s["repo"]), s["fix"], contract, ".")
    assert files == [
        "bookmarks/settings/__init__.py",
        "bookmarks/settings/prod.py",
        "pyproject.toml",
        "uv.lock",
    ]


def test_layout_refuses_when_the_plan_cannot_be_computed(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    sha = commit(repo, "Initial commit", ts(2026, 1, 10), {"README.md": "hi\n"})
    with pytest.raises(mod.HarnessError, match="layout files refused"):
        mod.lean_layout_files(mod.AppRepo(repo), sha, linkding_contract(), ".")


def test_lean_select_refuses_without_layout_files(tmp_path: Path) -> None:
    s = backend_history(tmp_path)
    with pytest.raises(mod.HarnessError, match="no layout files"):
        mod.select_fix(
            repo=mod.AppRepo(s["repo"]),
            app=app_dict(s["fix"]),
            ready_pair=all_ready(s),
            prompt_size=None,
            main_sha=None,
            script_sha256="s" * 64,
            rule="lean",
            language="python",
        )


LAYOUT = ["bookmarks/settings/prod.py", "pyproject.toml", "uv.lock"]


def test_l3_all_four_sides_with_the_files_proceeds_to_d(tmp_path: Path) -> None:
    s = layout_history(tmp_path)
    ready = FakeReady({s["base"], s["b"], s["mid"], s["fix"]})
    rec = layout_lean(s, ready, LAYOUT)
    entry = only_walked(rec)
    assert entry["outcome"] == "selected"
    assert entry["checks_run"] == ["a", "b", "c", "L", "L3", "d"]
    assert "l3_missing" not in entry
    assert len(ready.calls) == 2
    assert rec["layout_files"] == LAYOUT


def test_l3_b_pair_lacking_a_layout_file_is_rejected_without_dispatch(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    s: dict[str, Any] = {"repo": repo}
    old = {k: v for k, v in LINKDING_SHAPE.items() if k != "uv.lock"}
    s["base"] = commit(repo, "Initial commit", ts(2026, 1, 10), old)
    s["b"] = commit(repo, "Introduce the thing", ts(2026, 2, 1), {"bookmarks/app.py": "l1\nbug\n"})
    add_tag(repo, "v1.0", ts(2026, 2, 2))
    s["mid"] = commit(repo, "Add lock", ts(2026, 8, 1), {"uv.lock": "version = 1\n"})
    s["fix"] = commit(repo, "Fix the thing", ts(2026, 8, 15), {"bookmarks/app.py": "l1\nfixed\n"})
    ready = FakeReady({s["base"], s["b"], s["mid"], s["fix"]})
    rec = layout_lean(s, ready, LAYOUT)
    entry = only_walked(rec)
    assert entry["outcome"] == "rejected"
    assert entry["failed_check"] == "L3"
    assert entry["reason"] == "L3: predates the contract layout"
    assert entry["l3_missing"] == {"b_parent": ["uv.lock"], "b": ["uv.lock"]}
    assert ready.calls == []  # no check-(d) dispatch
    assert entry["checks_run"] == ["a", "b", "c", "L", "L3"]
    assert rec["rejected_by_check"]["L3"] == 1


def test_l3_header_identity_pins_the_list_and_a_different_list_refuses_resume(
    tmp_path: Path,
) -> None:
    jdir = tmp_path / "journal"
    ident = dict(script_sha256="s" * 64, main_sha=None, app_repo="o/r", app_head_sha="1" * 40)
    j, resumed, _ = mod.Journal.open(jdir, **ident, extra_identity={"layout_files": LAYOUT})
    assert not resumed and j.header["layout_files"] == LAYOUT
    mod.Journal.open(jdir, **ident, extra_identity={"layout_files": LAYOUT})
    with pytest.raises(mod.JournalMismatch, match="layout_files"):
        mod.Journal.open(jdir, **ident, extra_identity={"layout_files": ["pyproject.toml"]})
