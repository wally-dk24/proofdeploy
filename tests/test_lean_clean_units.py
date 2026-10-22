"""Lean P6a (D258): blind clean-unit selection, on a synthetic journal and repo."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "experiments" / "lean-poc" / "select_clean_units.py"
_spec = importlib.util.spec_from_file_location("select_clean_units", SCRIPT)
assert _spec is not None and _spec.loader is not None
sel = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = sel
_spec.loader.exec_module(sel)

SECRET_SUBJECT = "UNIQUE-SECRET-SUBJECT-TEXT"
SECRET_PATH = "uniquesecretdir"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def _commit(repo: Path, files: dict[str, str], n: int) -> str:
    for rel, text in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text + str(n), encoding="utf-8")
    _git(repo, "add", "-A")
    _git(
        repo,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@example.invalid",
        "commit",
        "-q",
        "-m",
        f"c{n} {SECRET_SUBJECT}",
    )
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture()
def world(tmp_path: Path) -> dict[str, object]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    src = f"{SECRET_PATH}/mod.py"
    shas = [
        _commit(repo, {"README.md": "r"}, 0),  # 0 root
        _commit(repo, {"docs/a.md": "d"}, 1),  # 1 docs only: no SOURCE
        _commit(repo, {src: "s", f"{SECRET_PATH}/test_mod.py": "t"}, 2),  # 2 SOURCE+TEST: pick 1
        _commit(repo, {src: "s"}, 3),  # 3 stands for B^: excluded (B's parent)
        _commit(repo, {src: "s"}, 4),  # 4 stands for B: excluded
        _commit(repo, {src: "s"}, 5),  # 5 failed_check e: not eligible
        _commit(repo, {"a.py": "x", "b.js": "y", "c.txt": "z"}, 6),  # 6 SOURCE=2 OTHER=1: pick 2
        _commit(repo, {"other.py": "x"}, 7),  # 7 fix^ (excluded by prefix)
        _commit(repo, {"third.py": "x"}, 8),  # 8 pick 3
        _commit(repo, {"fourth.py": "x"}, 9),  # 9 would be a 4th: never reached
    ]
    checks = ["a", "a", "a", "a", "a", "e", "a", "a", "a", "a"]
    rows = [
        {
            "sha": sha,
            "failed_check": chk,
            "committer_date": f"2026-08-{i + 1:02d}T00:00:00+00:00",
            "subject": SECRET_SUBJECT,
            "outcome": "rejected",
            "reason": "x",
        }
        for i, (sha, chk) in enumerate(zip(shas, checks, strict=True))
    ]
    journal = tmp_path / "walk.jsonl"
    journal.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return {"repo": repo, "shas": shas, "journal": journal}


def test_selects_first_three_eligible_in_walk_order(world: dict) -> None:
    shas = world["shas"]
    # Stand-ins: shas[4] plays B (its parent shas[3] is excluded with it);
    # shas[7] plays a fix-pair commit, excluded by prefix.
    wo6 = sel.load_wo6()
    repo = wo6.AppRepo(world["repo"])
    rows, jsha = sel.read_journal(world["journal"])
    assert jsha == hashlib.sha256(Path(world["journal"]).read_bytes()).hexdigest()
    assert all(set(r) == {"sha", "failed_check", "committer_date"} for r in rows)

    excludes = sel.resolve_excludes(repo, shas[4], (shas[7][:8],))
    assert excludes == {shas[3], shas[4], shas[7]}
    res = sel.select_clean_units(rows, repo, wo6, n=3, exclude=excludes)
    assert [u["sha"] for u in res["units"]] == [shas[2], shas[6], shas[8]]
    assert res["units"][0]["first_parent"] == shas[1]
    assert res["units"][1]["changed_class_counts"] == {"SOURCE": 2, "TEST": 0, "DOC": 0, "OTHER": 1}
    assert res["units"][1]["changed_source_paths"] == 2
    assert res["units"][0]["changed_class_counts"]["TEST"] == 1
    assert res["skipped_no_source_file"] == 2  # the README-only root and the docs-only commit
    assert res["skipped_excluded"] == 3  # shas[3], shas[4] and shas[7]
    assert res["journal_order_is_committer_date_ascending"] is True


def test_cli_output_is_blind(world: dict, tmp_path: Path, capsys, monkeypatch) -> None:
    shas = world["shas"]
    monkeypatch.setattr(sel, "B_SHA", shas[4])
    monkeypatch.setattr(sel, "FIX_PAIR_PREFIXES", (shas[7][:8],))
    out = tmp_path / "out.json"
    assert (
        sel.main(
            ["--journal", str(world["journal"]), "--repo", str(world["repo"]), "--out", str(out)]
        )
        == 0
    )
    printed = capsys.readouterr().out
    written = out.read_text(encoding="utf-8")
    for text in (printed, written):
        assert SECRET_SUBJECT not in text
        assert SECRET_PATH not in text
        assert "mod.py" not in text
    rec = json.loads(written)
    assert [u["sha"] for u in rec["units"]] == [shas[2], shas[6], shas[8]]
    assert rec["journal_sha256"] == hashlib.sha256(Path(world["journal"]).read_bytes()).hexdigest()
    assert "classify_path" in rec["rule"] and rec["measured_result"] is False


def test_too_few_units_fails(world: dict) -> None:
    wo6 = sel.load_wo6()
    repo = wo6.AppRepo(world["repo"])
    rows, _ = sel.read_journal(world["journal"])
    with pytest.raises(SystemExit):
        sel.select_clean_units(rows, repo, wo6, n=50, exclude=set())


# ---- WO-R5-U: --exclude-sha / --app-name, and the linkding units file ----------------------

PAPERLESS_RECORD = Path(__file__).parent.parent / "experiments/lean-poc/paperless-clean-units.json"


def test_default_excludes_are_the_paperless_set() -> None:
    assert sel.B_SHA == "aa67fd3aefa6489984fc15a5c32c1ad2b16ac23a"
    assert sel.FIX_PAIR_PREFIXES == ("14b6a41b", "d3710856")
    rec = json.loads(PAPERLESS_RECORD.read_text(encoding="utf-8"))
    assert sel.B_SHA in rec["excluded"]
    assert all(any(x.startswith(p) for x in rec["excluded"]) for p in sel.FIX_PAIR_PREFIXES)
    assert sel.DEFAULT_APP_NAME == "paperless-ngx/paperless-ngx"
    assert "app_name" not in rec  # the default is not recorded, so the record re-derives as is


def test_default_cli_record_has_the_paperless_shape(
    world: dict, tmp_path: Path, monkeypatch
) -> None:
    shas = world["shas"]
    monkeypatch.setattr(sel, "B_SHA", shas[4])
    monkeypatch.setattr(sel, "FIX_PAIR_PREFIXES", (shas[7][:8],))
    out = tmp_path / "out.json"
    argv = ["--journal", str(world["journal"]), "--repo", str(world["repo"]), "--out", str(out)]
    assert sel.main(argv) == 0
    rec = json.loads(out.read_text(encoding="utf-8"))
    assert set(rec) == set(json.loads(PAPERLESS_RECORD.read_text(encoding="utf-8")))
    assert rec["excluded"] == sorted([shas[3], shas[4], shas[7]])


def test_explicit_excludes_are_honoured_and_replace_the_default(
    world: dict, tmp_path: Path, monkeypatch
) -> None:
    shas = world["shas"]
    # The defaults would exclude shas[3], shas[4], shas[7]; the explicit set must replace them.
    monkeypatch.setattr(sel, "B_SHA", shas[4])
    monkeypatch.setattr(sel, "FIX_PAIR_PREFIXES", (shas[7][:8],))
    out = tmp_path / "out.json"
    argv = ["--journal", str(world["journal"]), "--repo", str(world["repo"]), "--out", str(out)]
    argv += ["--exclude-sha", shas[2], "--exclude-sha", shas[6], "--app-name", "o/linkding"]
    assert sel.main(argv) == 0
    rec = json.loads(out.read_text(encoding="utf-8"))
    assert rec["excluded"] == sorted([shas[2], shas[6]])
    assert rec["app_name"] == "o/linkding"
    assert [u["sha"] for u in rec["units"]] == [shas[3], shas[4], shas[7]]
    assert rec["skipped_excluded"] == 2


def test_exclude_sha_must_be_a_full_sha(world: dict, tmp_path: Path) -> None:
    argv = ["--journal", str(world["journal"]), "--repo", str(world["repo"])]
    argv += ["--out", str(tmp_path / "o.json"), "--exclude-sha", world["shas"][2][:8]]
    with pytest.raises(SystemExit):
        sel.main(argv)


ROOT = Path(__file__).parent.parent
RELAY_SELECTED = json.loads(
    (Path(__file__).parent / "fixtures" / "linkding_selected_shas.json").read_text(encoding="utf-8")
)


def _linkding() -> dict:
    return json.loads((ROOT / "experiments/lean-poc/linkding-units.json").read_text("utf-8"))


def test_linkding_bug_unit_equals_the_relay_records_selected_shas() -> None:
    (bug,) = _linkding()["bug_units"]
    assert {k: bug[k] for k in ("b_parent", "b", "fix_parent", "fix")} == RELAY_SELECTED
    assert bug["id"] == f"linkding-bug-{RELAY_SELECTED['fix'][:8]}"


def test_linkding_clean_units_are_the_committed_selection() -> None:
    rec = json.loads((ROOT / "experiments/lean-poc/linkding-clean-units.json").read_text("utf-8"))
    cleans = _linkding()["clean_units"]
    assert len(cleans) == 3
    assert [{k: u[k] for k in ("sha", "first_parent")} for u in rec["units"]] == [
        {k: u[k] for k in ("sha", "first_parent")} for u in cleans
    ]
    assert [u["changed_class_counts"] for u in rec["units"]] == [
        u["changed_class_counts"] for u in cleans
    ]
    assert set(rec["excluded"]) == set(RELAY_SELECTED.values())
    assert rec["app_name"] == "sissbruecker/linkding"
    assert not {u["sha"] for u in cleans} & set(RELAY_SELECTED.values())
    assert rec["measured_result"] is False


def test_linkding_group_loads_with_one_bug_and_three_clean_units() -> None:
    spec = importlib.util.spec_from_file_location(
        "lean_poc_gate_u", ROOT / "experiments/lean-poc/gate.py"
    )
    assert spec is not None and spec.loader is not None
    gate = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = gate
    spec.loader.exec_module(gate)
    units = gate.app_units(gate.load_app_group("linkding"))
    assert [u.kind for u in units] == ["bug", "clean", "clean", "clean"]
    assert units[0].id == "linkding-bug-71d62f48"
