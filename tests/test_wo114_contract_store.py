"""WO-1.14 Part 1 (D103) — the contract store is the measured path's
contract source (REG-E).

The fixture audit (evidence/fixture-audit-2026-10-16) found the
measured path loading the contract from inside the measured
snapshot — a file no upstream tree contains, so every measured run
died with an untyped FileNotFoundError, no record, no model call —
and readiness contracts carrying ``fixture == {}``, so the author
text could never render from them either.

These tests pin the engine half of the fix:

- the measured path loads the unit's contract from the committed
  store (readiness/contracts/<owner>__<repo>.yml), by path, resolved
  against the ProofDeploy tree the run executes from — never from
  the upstream snapshot tree;
- ONE contract object serves the author bundle and every side;
- the contract is never written into the author's snapshot, and the
  bundle + leak check pass with the store-loaded contract;
- a missing store file, an unparseable contract, or an
  unrenderable fixture is a TYPED refusal WITH an evidence record
  (our-gap class), never an untyped traceback exit;
- --dry-run-before-model stops before the model call;
- readiness rows record the rendered author text's template name
  and sha256 per side (REG-E item 6).

The plants from the work order (restore the in-tree load; one side
loading a different file; the contract copied into the snapshot;
the missing-contract exception escaping untyped) each fail tests in
this file — the decoy test IS plant (a)'s catcher: its in-tree
contract is a different, broken contract that nothing may use.
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from proofdeploy.fixture import render_description, template_path
from proofdeploy.model_client import ModelResponse
from proofdeploy.orchestrator import (
    ContractRefusal,
    MeasureConfig,
    Orchestrator,
    OrchestratorError,
)
from proofdeploy.runpair import read_records
from proofdeploy.yml import load_contract
from readiness import provision as rp

sys.path.insert(0, str(Path(__file__).parent.parent / "conformance"))
import kit

REPO_ROOT = Path(__file__).resolve().parent.parent

APP_PY = '''
import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer

VALUE = {value}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            body, code = b"ok", 200
        elif self.path == "/value":
            body = json.dumps({{"value": VALUE}}).encode()
            code = 200
        else:
            body, code = b"not found", 404
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    port = int(os.environ["PORT"])
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()
'''

# The store contract for the mini unit: complete, with a fixture
# that renders under the no-credential template. Its repo_name is
# the marker the canned model asserts on: the author prompt must
# carry THIS text, from THIS file.
STORE_YML = """\
repo_name: store-mini-app
app:
  path: "."
build: "python -m py_compile app.py"
start: "python app.py"
readiness: "/health"
env:
  APP_ENV: "test"
fixture:
  repo_name: "store-mini-app"
  seed_note: "The app starts empty; GET /value reports the build's constant."
  auth_mechanism: "There are no user accounts and no login."
  flags_note: "There are no feature flags."
"""

# The decoy: the same repo carrying an IN-TREE proofdeploy.yml —
# different fixture text, and a build that fails. If anything on
# the measured path reads the contract from the snapshot tree, the
# run breaks (build) or the prompt names the decoy (fixture).
DECOY_YML = """\
repo_name: decoy-app
app:
  path: "."
build: "python -m py_compile no_such_decoy_file.py"
start: "python app.py"
readiness: "/health"
env:
  APP_ENV: "test"
fixture:
  repo_name: "decoy-app"
  seed_note: "Decoy seed note that must never reach the author."
  auth_mechanism: "There are no user accounts and no login."
  flags_note: "There are no feature flags."
"""


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=True,
    )
    return out.stdout.strip()


def make_mini_repo(tmp_path: Path, *, in_tree: str | None) -> dict:
    """base -> bug (VALUE 1->2) -> fix (VALUE back to 1).

    in_tree: None = no proofdeploy.yml anywhere in the tree (the
    upstream shape the audit found); "decoy" = an in-tree
    proofdeploy.yml that must be ignored; "full" = the legacy
    in-tree contract (verify/dev path shape)."""
    repo = tmp_path / "mini"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")

    def commit(value: int, message: str) -> str:
        (repo / "app.py").write_text(APP_PY.format(value=value))
        (repo / "requirements.txt").write_text("# no third-party deps\n")
        if in_tree == "decoy":
            (repo / "proofdeploy.yml").write_text(DECOY_YML)
        elif in_tree == "full":
            (repo / "proofdeploy.yml").write_text(STORE_YML)
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", message)
        return _git(repo, "rev-parse", "HEAD")

    base = commit(1, "base: value endpoint")
    bug = commit(2, "change the value constant")
    fix = commit(1, "restore the value constant")
    return {"repo": repo, "base": base, "bug": bug, "fix": fix}


def skill_path() -> Path:
    return REPO_ROOT / "docs" / "evaluation" / "skill" / "skill-author.md"


def canned_probes_store(prompt: str) -> ModelResponse:
    """A canned model reply whose probe catches VALUE != 1 — and
    which refuses to have been authored from any other fixture text
    than the store contract's."""
    assert "store-mini-app" in prompt
    assert "decoy-app" not in prompt
    return ModelResponse(
        content=(
            "```json\n"
            '{"setup": [], '
            '"act": {"method": "GET", "path": "/value"}, '
            '"assert": [{"type": "body", "json_path": '
            '{"path": "value", "equals": 1}}]}'
            "\n```\n"
        )
    )


def store_file(tmp_path: Path, text: str = STORE_YML) -> Path:
    path = tmp_path / "store" / "mini__mini.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def store_cfg(tmp_path: Path, info: dict, contract: Path, **over) -> MeasureConfig:
    cfg = MeasureConfig(
        repo_dir=Path(info["repo"]),
        output_dir=tmp_path / "records",
        workdir=tmp_path / "work",
        skill_path=skill_path(),
        bug_id="mini-bug",
        rev_bug_base=info["base"],
        rev_bug=info["bug"],
        rev_fix=info["fix"],
        model_runner=canned_probes_store,
        public_contract_env_keys=["APP_ENV"],
        allow_unsandboxed=True,
        use_contract_store=True,
        contract_path=contract,
    )
    for key, value in over.items():
        setattr(cfg, key, value)
    return cfg


# ----------------------------------------------------------------------
# The store contract drives the run
# ----------------------------------------------------------------------


def test_measure_bug_uses_store_contract_with_no_in_tree_contract(tmp_path):
    """The audit's shape: the upstream tree has NO proofdeploy.yml
    at all. The run still measures — from the store contract — and
    the evidence record names the store path and the file's sha256."""
    info = make_mini_repo(tmp_path, in_tree=None)
    contract = store_file(tmp_path)
    summary = Orchestrator(store_cfg(tmp_path, info, contract)).measure_bug()
    assert summary["verdict"] == "candidate_catch"
    assert summary["probe_count"] == 1

    records = read_records(tmp_path / "records")
    assert [r["record_type"] for r in records] == ["evidence", "score"]
    evidence = records[0]
    assert evidence["schema_version"] == "1.8.0"
    assert evidence["contract_source"] == {
        "store_path": str(contract),
        "sha256": hashlib.sha256(contract.read_bytes()).hexdigest(),
    }
    assert evidence["fixture"]["repo_name"] == "store-mini-app"


def test_measure_bug_ignores_in_tree_decoy_contract(tmp_path):
    """Plant (a)'s catcher: an in-tree proofdeploy.yml exists but
    is a different, broken contract. The store still wins — the
    decoy's build would fail and its fixture text would name the
    wrong app; the run catches the bug."""
    info = make_mini_repo(tmp_path, in_tree="decoy")
    contract = store_file(tmp_path)
    summary = Orchestrator(store_cfg(tmp_path, info, contract)).measure_bug()
    assert summary["verdict"] == "candidate_catch"
    evidence = read_records(tmp_path / "records")[0]
    assert evidence["fixture"]["repo_name"] == "store-mini-app"
    assert evidence["contract_source"]["sha256"] == hashlib.sha256(
        contract.read_bytes()
    ).hexdigest()


def test_measure_clean_uses_store_contract(tmp_path):
    info = make_mini_repo(tmp_path, in_tree=None)
    contract = store_file(tmp_path)
    cfg = MeasureConfig(
        repo_dir=Path(info["repo"]),
        output_dir=tmp_path / "records",
        workdir=tmp_path / "work",
        skill_path=skill_path(),
        clean_id="mini-clean",
        rev_clean=info["fix"],
        model_runner=canned_probes_store,
        public_contract_env_keys=["APP_ENV"],
        allow_unsandboxed=True,
        use_contract_store=True,
        contract_path=contract,
    )
    summary = Orchestrator(cfg).measure_clean()
    assert summary["run_kind"] == "clean"
    assert summary["false_alarm"] is False
    assert summary["c_ready"]
    evidence = read_records(tmp_path / "records")[0]
    assert evidence["contract_source"]["sha256"] == hashlib.sha256(
        contract.read_bytes()
    ).hexdigest()


def test_one_contract_object_serves_bundle_and_sides(tmp_path):
    """Plant (b)'s catcher: there is exactly one loaded contract
    object; the bundle accessor and the side accessor return that
    identical object, and the rendered text recorded at load is the
    engine's render of that same contract."""
    info = make_mini_repo(tmp_path, in_tree=None)
    contract = store_file(tmp_path)
    orch = Orchestrator(store_cfg(tmp_path, info, contract))
    loaded = orch._load_unit_contract()
    stub = SimpleNamespace(dir=tmp_path / "no-such-snapshot")
    assert orch._bundle_contract(stub) is loaded
    assert orch._side_contract(stub) is loaded
    assert orch._contract_source_record() == {
        "store_path": str(contract),
        "sha256": hashlib.sha256(contract.read_bytes()).hexdigest(),
    }
    rendered = render_description(
        template_path("fixture_template_nocredential.txt").read_text(
            encoding="utf-8"
        ),
        loaded.fixture,
    )
    assert orch._unit_contract_rendered_sha256 == hashlib.sha256(
        rendered.encode("utf-8")
    ).hexdigest()


def test_side_contract_identity_violation_raises(tmp_path, monkeypatch):
    """D123: the one-contract identity guard is an explicit
    raise, not a bare assert (which `python -O` would strip).
    Break the identity — the loader starts returning a DIFFERENT
    contract object (the decoy, loaded from its own file) after
    the unit contract was loaded — and _side_contract must raise
    OrchestratorError naming BOTH sources: the expected store
    path + sha256, and the offending contract's own build."""
    info = make_mini_repo(tmp_path, in_tree=None)
    contract = store_file(tmp_path)
    orch = Orchestrator(store_cfg(tmp_path, info, contract))
    orch._load_unit_contract()
    decoy_path = tmp_path / "decoy.yml"
    decoy_path.write_text(DECOY_YML, encoding="utf-8")
    decoy = load_contract(decoy_path)
    monkeypatch.setattr(
        Orchestrator, "_load_unit_contract", lambda self: decoy
    )
    stub = SimpleNamespace(dir=tmp_path / "no-such-snapshot")
    with pytest.raises(OrchestratorError) as excinfo:
        orch._side_contract(stub)
    message = str(excinfo.value)
    assert str(contract) in message
    assert hashlib.sha256(contract.read_bytes()).hexdigest() in message
    assert "no_such_decoy_file.py" in message


def test_contract_never_written_into_author_snapshot(tmp_path):
    """Plant (c)'s catcher: after a full measured run from a tree
    with no in-tree contract, no snapshot the run created — bundle
    or side — contains a proofdeploy.yml."""
    info = make_mini_repo(tmp_path, in_tree=None)
    contract = store_file(tmp_path)
    Orchestrator(store_cfg(tmp_path, info, contract)).measure_bug()
    work = tmp_path / "work" / "bug-mini-bug"
    snapshots = sorted(work.glob("snapshot*")) + sorted(
        (work / "author-bundle").glob("snapshot*")
    )
    assert snapshots, "the run produced snapshots to check"
    for snap in snapshots:
        assert not (snap / "proofdeploy.yml").exists(), snap


# ----------------------------------------------------------------------
# Typed refusals WITH records (item 4)
# ----------------------------------------------------------------------


def _refusal_run(tmp_path: Path, contract: Path):
    cfg = MeasureConfig(
        repo_dir=tmp_path / "unused",
        output_dir=tmp_path / "records",
        workdir=tmp_path / "work",
        skill_path=skill_path(),
        bug_id="mini-bug",
        rev_bug_base="base-sha",
        rev_bug="bug-sha",
        rev_fix="fix-sha",
        model_runner=canned_probes_store,
        allow_unsandboxed=True,
        use_contract_store=True,
        contract_path=contract,
    )
    return Orchestrator(cfg).measure_bug()


def _assert_refusal(summary, out: Path, cause: str) -> None:
    """Plant (d)'s catcher: the refusal RETURNED a summary (no
    exception escaped) and left a typed evidence + score pair."""
    assert summary["verdict"] == "inconclusive"
    assert summary["reason_class"] == "our gap"
    assert summary["cause"] == cause
    assert summary["measured_result"] is False
    records = read_records(out)
    assert [r["record_type"] for r in records] == ["evidence", "score"]
    evidence, score = records
    assert evidence["inconclusive_cause"] == cause
    assert evidence["measured_result"] is False
    assert "our gap" in evidence["inconclusive_error"]
    assert not evidence["inconclusive_error"].startswith("not supported:")
    assert score["verdict"] == "inconclusive"
    assert score["note"] == f"our gap: {cause}"


def test_missing_store_file_refuses_with_record(tmp_path):
    missing = tmp_path / "store" / "ghost__ghost.yml"
    summary = _refusal_run(tmp_path, missing)
    _assert_refusal(summary, tmp_path / "records", "contract_missing")
    evidence = read_records(tmp_path / "records")[0]
    assert evidence["contract_source"] == {
        "store_path": str(missing),
        "sha256": None,
    }


def test_unparseable_store_contract_refuses_with_record(tmp_path):
    bad = store_file(tmp_path, "repo_name: [unclosed\n")
    summary = _refusal_run(tmp_path, bad)
    _assert_refusal(summary, tmp_path / "records", "contract_invalid")
    evidence = read_records(tmp_path / "records")[0]
    assert evidence["contract_source"]["sha256"] == hashlib.sha256(
        bad.read_bytes()
    ).hexdigest()


def test_invalid_store_contract_refuses_with_record(tmp_path):
    # Parses as YAML, fails contract validation (no app path).
    bad = store_file(tmp_path, "repo_name: mini\nstart: 'python app.py'\n")
    summary = _refusal_run(tmp_path, bad)
    _assert_refusal(summary, tmp_path / "records", "contract_invalid")


def test_unrenderable_fixture_refuses_with_record(tmp_path):
    # A valid contract whose fixture mapping is empty: the render
    # raises "fixture mapping missing fields" — the audit's second
    # failure, now a typed refusal before any snapshot or model call.
    text = STORE_YML.split("fixture:")[0]
    unrenderable = store_file(tmp_path, text)
    summary = _refusal_run(tmp_path, unrenderable)
    _assert_refusal(summary, tmp_path / "records", "contract_unrenderable")
    evidence = read_records(tmp_path / "records")[0]
    assert "fixture mapping missing fields" in evidence["inconclusive_error"]


def test_slug_resolution_names_the_committed_store_file(
    tmp_path, monkeypatch
):
    """unit_repo resolves through the contract store's slug rule:
    directus/directus names readiness/contracts/
    directus__directus.yml in the store tree the run executes
    from. The store here is the test's OWN temporary tree,
    holding a fixture-less contract of the test's own authoring
    — so the run refuses as unrenderable, and the evidence
    record names that store file (its store-relative path and
    its sha256). The slug rule must not be exercised against a
    real repo's committed contract: a signed fixture landing in
    the real store (PR #129 added directus's) changes that file,
    not this test's premise."""
    store_root = tmp_path / "store-tree"
    committed = (
        store_root / "readiness" / "contracts" / "directus__directus.yml"
    )
    committed.parent.mkdir(parents=True)
    committed.write_text(STORE_YML.split("fixture:")[0], encoding="utf-8")
    monkeypatch.setattr(
        "proofdeploy.yml.proofdeploy_tree_root", lambda: store_root
    )
    monkeypatch.setattr(
        "proofdeploy.orchestrator.proofdeploy_tree_root",
        lambda: store_root,
    )
    cfg = MeasureConfig(
        repo_dir=tmp_path / "unused",
        output_dir=tmp_path / "records",
        workdir=tmp_path / "work",
        skill_path=skill_path(),
        bug_id="mini-bug",
        rev_bug_base="base-sha",
        rev_bug="bug-sha",
        rev_fix="fix-sha",
        model_runner=canned_probes_store,
        allow_unsandboxed=True,
        use_contract_store=True,
        unit_repo="directus/directus",
    )
    summary = Orchestrator(cfg).measure_bug()
    _assert_refusal(summary, tmp_path / "records", "contract_unrenderable")
    evidence = read_records(tmp_path / "records")[0]
    assert evidence["contract_source"]["store_path"] == (
        "readiness/contracts/directus__directus.yml"
    )
    assert evidence["contract_source"]["sha256"] == hashlib.sha256(
        committed.read_bytes()
    ).hexdigest()


def test_load_unit_contract_raises_typed_refusal():
    cfg = MeasureConfig(
        repo_dir=Path("/nonexistent"),
        output_dir=Path("/nonexistent/out"),
        workdir=Path("/nonexistent/work"),
        use_contract_store=True,
        unit_repo="nobody/no-such-repo",
    )
    with pytest.raises(ContractRefusal) as caught:
        Orchestrator(cfg)._load_unit_contract()
    assert caught.value.cause == "contract_missing"
    assert caught.value.store_path == (
        "readiness/contracts/nobody__no-such-repo.yml"
    )


# ----------------------------------------------------------------------
# Dry run (item 7 of the work order): stop before the model call
# ----------------------------------------------------------------------


def test_dry_run_stops_before_the_model_call(tmp_path):
    info = make_mini_repo(tmp_path, in_tree=None)
    contract = store_file(tmp_path)

    def exploding(prompt: str) -> ModelResponse:
        raise AssertionError("the model must not be called in a dry run")

    cfg = store_cfg(
        tmp_path, info, contract,
        model_runner=exploding, dry_run_before_model=True,
    )
    summary = Orchestrator(cfg).measure_bug()
    assert summary["dry_run"] is True
    assert summary["stopped_before_model_call"] is True
    assert summary["contract_source"]["sha256"] == hashlib.sha256(
        contract.read_bytes()
    ).hexdigest()
    bundle_desc = (
        tmp_path / "work" / "bug-mini-bug" / "author-bundle"
        / "fixture-description.txt"
    )
    assert summary["rendered_fixture_sha256"] == hashlib.sha256(
        bundle_desc.read_bytes()
    ).hexdigest()
    assert summary["bundle_manifest"]["fixture_template"]["file"] == (
        "fixture_template_nocredential.txt"
    )
    # Nothing was measured: the records repo holds no records.
    assert read_records(tmp_path / "records") == []


# The approved renders (D146/D147, signed): a golden unit's rendered
# author text must hash to exactly these.
GOLDEN_APPROVED_RENDER_SHA256 = {
    "node-express-sqlite": (
        "c73cfe01ff4c010d9e90b58494155f246bad7f75a5fa64dc572bba8ced4ca9df"
    ),
    "python-flask-sqlite": (
        "747581467b55d51800f3c7e6084837736675c27787a167ac7c7e176df3689e37"
    ),
}


@pytest.mark.parametrize("app", sorted(GOLDEN_APPROVED_RENDER_SHA256))
def test_golden_unit_dry_run_reaches_the_stop_from_the_store(
    tmp_path, monkeypatch, app
):
    """D154's final item: a non-dispatched local run of measure.yml's
    golden branch. The kit materializes the unit exactly as the
    workflow's golden step does; the orchestrator runs with
    --dry-run-before-model, no API key set and an exploding model
    runner — and must reach the dry-run stop with contract_source
    naming the committed golden store file (path + its sha256) and
    the rendered author text equal to the app's approved render."""
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    shas = kit.materialize_golden(app, tmp_path / "target")

    def exploding(prompt: str) -> ModelResponse:
        raise AssertionError("the model must not be called in a dry run")

    cfg = MeasureConfig(
        repo_dir=tmp_path / "target",
        output_dir=tmp_path / "records",
        workdir=tmp_path / "work",
        no_skill=True,
        bug_id=f"golden-{app}",
        rev_bug_base=shas["base"],
        rev_bug=shas["bug"],
        rev_fix=shas["fix"],
        model_runner=exploding,
        allow_unsandboxed=True,
        use_contract_store=True,
        unit_repo=f"golden/{app}",
        dry_run_before_model=True,
    )
    summary = Orchestrator(cfg).measure_bug()
    assert summary["dry_run"] is True
    assert summary["stopped_before_model_call"] is True
    store_file = (
        REPO_ROOT / "readiness" / "contracts" / f"golden__{app}.yml"
    )
    assert summary["contract_source"] == {
        "store_path": f"readiness/contracts/golden__{app}.yml",
        "sha256": hashlib.sha256(store_file.read_bytes()).hexdigest(),
    }
    assert summary["rendered_fixture_sha256"] == (
        GOLDEN_APPROVED_RENDER_SHA256[app]
    )
    # Nothing was measured: the records repo holds no records.
    assert read_records(tmp_path / "records") == []


def test_cli_measure_dry_run_flags_parse():
    from proofdeploy.cli import build_parser

    args = build_parser().parse_args(
        [
            "measure",
            "--repo", "/tmp/target",
            "--workdir", "/tmp/work",
            "--output-dir", "/tmp/out",
            "--bug-id", "b1",
            "--base", "a", "--bug", "b", "--fix", "c",
            "--unit-repo", "directus/directus",
            "--contract", "/tmp/store.yml",
            "--dry-run-before-model",
        ]
    )
    assert args.unit_repo == "directus/directus"
    assert args.contract == "/tmp/store.yml"
    assert args.dry_run_before_model is True


def test_cli_measure_dry_run_provision_flag_parses():
    """D222: --dry-run-provision parses onto the namespace the
    measure command reads (a dropped flag would silently rehearse
    nothing — cmd_measure reads it via getattr)."""
    from proofdeploy.cli import build_parser

    args = build_parser().parse_args(
        [
            "measure",
            "--repo", "/tmp/target",
            "--workdir", "/tmp/work",
            "--output-dir", "/tmp/out",
            "--bug-id", "b1",
            "--base", "a", "--bug", "b", "--fix", "c",
            "--dry-run-provision",
        ]
    )
    assert args.dry_run_provision is True
    plain = build_parser().parse_args(
        [
            "measure",
            "--repo", "/tmp/target",
            "--workdir", "/tmp/work",
            "--output-dir", "/tmp/out",
            "--bug-id", "b1",
            "--base", "a", "--bug", "b", "--fix", "c",
        ]
    )
    assert plain.dry_run_provision is False


# ----------------------------------------------------------------------
# Readiness render rows (REG-E item 6)
# ----------------------------------------------------------------------


def test_readiness_render_fields_record_our_gap_for_fixtureless_contract():
    # bottlepy/bottle's committed contract carries no fixture: the
    # render fails, the failure is recorded as our gap, and the
    # template choice is the engine's (no auth: -> no-credential).
    fields = rp.fixture_render_fields("bottlepy/bottle")
    assert fields["fixture_template"] == "fixture_template_nocredential.txt"
    assert fields["fixture_render_sha256"] is None
    assert "fixture mapping missing fields" in fields["fixture_render_error"]
    assert "(our gap)" in fields["fixture_render_error"]


def test_readiness_render_fields_success(monkeypatch, tmp_path):
    contract = tmp_path / "mini__mini.yml"
    contract.write_text(STORE_YML, encoding="utf-8")
    monkeypatch.setattr(rp, "contract_path", lambda repo: contract)
    fields = rp.fixture_render_fields("mini/mini")
    assert fields["fixture_template"] == "fixture_template_nocredential.txt"
    assert fields["fixture_render_error"] is None
    expected = render_description(
        template_path("fixture_template_nocredential.txt").read_text(
            encoding="utf-8"
        ),
        {
            "repo_name": "store-mini-app",
            "seed_note": (
                "The app starts empty; GET /value reports the "
                "build's constant."
            ),
            "auth_mechanism": "There are no user accounts and no login.",
            "flags_note": "There are no feature flags.",
        },
    )
    assert fields["fixture_render_sha256"] == hashlib.sha256(
        expected.encode("utf-8")
    ).hexdigest()


def test_readiness_rows_carry_render_fields():
    unit = {
        "unit_id": "HX",
        "kind": "heldout",
        "repo": "bottlepy/bottle",
        "sides": [],
    }
    row = rp.row_from_exception(
        unit, {"role": "fix", "sha": "a" * 40}, "a" * 40, 1, "isolated",
        ValueError("boom"),
    )
    assert row["fixture_template"] == "fixture_template_nocredential.txt"
    assert row["fixture_render_sha256"] is None
    assert "(our gap)" in row["fixture_render_error"]
    err_row = rp._harness_error_row(
        unit, {"role": "fix", "sha": "b" * 40}, 1, "isolated",
        RuntimeError("kaput"),
    )
    assert err_row["fixture_template"] == "fixture_template_nocredential.txt"
    assert "(our gap)" in err_row["fixture_render_error"]


# ----------------------------------------------------------------------
# D167/D171: golden smoke units record measured_result False
#
# A sandboxed run records measured_result True — that is what makes
# it a registered measurement. A golden unit (contract-store owner
# `golden`, D154) is a known-answer SMOKE unit (D152): it runs
# sandboxed through the real measured path, but its record must
# never be a measured result. Orchestrator._measured_result is the
# ONE helper every record site calls; D171 requires a test per
# record SITE, each asserting measured_result False AND the record
# kind — a test that lands in the wrong record proves nothing about
# the site a smoke run actually reaches:
#
#   (a) bug run, normal completion    -> the pair record
#   (b) clean run, normal completion  -> the clean record
#   (c) model failure (transport)     -> the model-failure record
#   (d) author output invalid         -> the author-invalid record
#   (e) orchestrator crash            -> the crash record
#
# The runs drive the real record path with a stub model (the O5/O6
# pattern: gates stubbed, a faked READY sandbox side whose target
# nothing listens on — probe results are errors on every side, and
# the normal-completion record is still written). The unit's
# contract resolves by the real slug rule from a temporary store
# tree; the golden runs and their non-golden control differ ONLY in
# the store owner.
#
# D171 history: the first version of the fake built its
# ProvisionResult with a `sandbox_evidence` keyword that dataclass
# does not take, so every "normal" run died inside the fake and
# landed in the CRASH record — the two golden tests passed for the
# wrong reason and only the crash site was really covered. The fake
# below builds the result the way the runner does (sandbox_evidence
# belongs on _Side, not on ProvisionResult); the normal-completion
# tests assert the record carries real probe results and NO crash
# cause, and the crash case is a deliberate fake of its own.
# ----------------------------------------------------------------------


def _prepare_sandboxed_store_run(
    tmp_path, monkeypatch, owner: str, *, crash: bool = False
):
    import proofdeploy.orchestrator as orch_mod
    from proofdeploy.executor import Executor
    from proofdeploy.orchestrator import _Side
    from proofdeploy.runner import ProvisionResult, ProvisionStatus

    monkeypatch.setattr(orch_mod, "assert_measurement_gate", lambda: {})
    # The harness gate has its own tests (Addendum 24/25); stub it so
    # this test does not depend on the working tree being clean.
    monkeypatch.setattr(
        orch_mod,
        "assert_harness_commit_gate",
        lambda: {
            "commit": "0" * 40,
            "source": "git",
            "dirty": False,
            "workflow_ref": None,
        },
    )
    store_root = tmp_path / f"store-{owner}"
    committed = store_root / "readiness" / "contracts" / f"{owner}__mini.yml"
    committed.parent.mkdir(parents=True)
    committed.write_text(STORE_YML, encoding="utf-8")
    monkeypatch.setattr(
        "proofdeploy.yml.proofdeploy_tree_root", lambda: store_root
    )
    monkeypatch.setattr(
        "proofdeploy.orchestrator.proofdeploy_tree_root",
        lambda: store_root,
    )
    info = make_mini_repo(tmp_path, in_tree=None)

    def prepare(orch: Orchestrator) -> None:
        contract = orch._load_unit_contract()

        def fake_provision_sandbox(rev, port, unit_dir, name):
            if crash:
                # D171 (e): provisioning itself explodes after
                # authoring — the run must land in the crash record.
                raise RuntimeError("fake sandbox provisioning exploded")
            return _Side(
                rev=rev,
                sha=rev,
                snapshot_dir=tmp_path / "snap",
                contract=contract,
                provision=ProvisionResult(
                    status=ProvisionStatus.READY,
                    target_url=f"http://127.0.0.1:{port}",
                    run_id="fake",
                ),
                sandbox_evidence={"image": "fake"},
            )

        orch._provision_side_sandbox = fake_provision_sandbox
        orch._make_executor = (
            lambda side: Executor(target_url=side.provision.target_url)
        )

    return info, prepare


def _sandboxed_store_cfg(tmp_path, info, owner: str, **over) -> MeasureConfig:
    cfg = MeasureConfig(
        repo_dir=Path(info["repo"]),
        output_dir=tmp_path / "records",
        workdir=tmp_path / "work",
        skill_path=skill_path(),
        bug_id="mini-bug",
        rev_bug_base=info["base"],
        rev_bug=info["bug"],
        rev_fix=info["fix"],
        model_runner=over.pop("model_runner", canned_probes_store),
        public_contract_env_keys=["APP_ENV"],
        sandbox=True,
        allow_unsandboxed=False,
        use_contract_store=True,
        unit_repo=f"{owner}/mini",
    )
    for key, value in over.items():
        setattr(cfg, key, value)
    return cfg


def _transport_failing_runner(prompt: str) -> ModelResponse:
    """D171 (c): the model call itself fails (transport) on every
    attempt — the orchestrator retries exactly once, then records
    the model-failure INCONCLUSIVE."""
    from proofdeploy.model_client import ModelCallError

    raise ModelCallError("connection reset by peer", cause="model_transport")


def _invalid_output_runner(prompt: str) -> ModelResponse:
    """D171 (d): the model answers, but its output holds no valid
    probe block at all."""
    assert "store-mini-app" in prompt
    assert "decoy-app" not in prompt
    return ModelResponse(
        content="I reviewed the diff and found nothing to probe.\n"
    )


def _store_evidence(tmp_path, owner: str):
    """The run's evidence record, with the D167 common ground
    asserted: sandboxed, and labelled by the store file it ran
    under (the flag and the label cannot disagree)."""
    evidence = read_records(tmp_path / "records")[0]
    assert evidence["sandboxed"] is True
    assert evidence["contract_source"]["store_path"] == (
        f"readiness/contracts/{owner}__mini.yml"
    )
    return evidence


def test_golden_bug_unit_normal_completion_records_measured_result_false(
    tmp_path, monkeypatch
):
    """D171 (a): a sandboxed golden BUG unit that completes NORMALLY
    — both sides READY, probes executed, the pair record written by
    the normal path (the site the smoke runs use). measured_result
    is False, and the record is the normal-completion record: real
    probe results on both sides, no crash cause."""
    info, prepare = _prepare_sandboxed_store_run(
        tmp_path, monkeypatch, "golden"
    )
    orch = Orchestrator(_sandboxed_store_cfg(tmp_path, info, "golden"))
    prepare(orch)
    summary = orch.measure_bug()
    # A crash or inconclusive summary names its cause; a normal
    # completion does not have one.
    assert "cause" not in summary
    evidence = _store_evidence(tmp_path, "golden")
    assert evidence["measured_result"] is False
    assert evidence["inconclusive_cause"] is None
    assert evidence["inconclusive_error"] is None
    assert evidence["crash_probe_results"] is None
    assert evidence["fix_parent_results"] and evidence["fix_results"]
    assert all("verdict" in r for r in evidence["fix_results"])


def test_golden_clean_unit_normal_completion_records_measured_result_false(
    tmp_path, monkeypatch
):
    """D171 (b): the same for a sandboxed golden CLEAN unit that
    completes normally — real probe results on C, no crash cause,
    measured_result False."""
    info, prepare = _prepare_sandboxed_store_run(
        tmp_path, monkeypatch, "golden"
    )
    cfg = _sandboxed_store_cfg(
        tmp_path, info, "golden",
        bug_id=None, rev_bug_base=None, rev_bug=None, rev_fix=None,
        clean_id="mini-clean", rev_clean=info["fix"],
    )
    orch = Orchestrator(cfg)
    prepare(orch)
    summary = orch.measure_clean()
    assert "cause" not in summary
    evidence = _store_evidence(tmp_path, "golden")
    assert evidence["measured_result"] is False
    assert evidence["inconclusive_cause"] is None
    assert evidence["inconclusive_error"] is None
    assert evidence["crash_probe_results"] is None
    assert evidence["c_results"]
    assert all("verdict" in r for r in evidence["c_results"])


def test_golden_bug_unit_model_failure_records_measured_result_false(
    tmp_path, monkeypatch
):
    """D171 (c): the model call fails (transport) on both attempts —
    the run lands in the model-failure INCONCLUSIVE record. A
    non-golden measured run records measured_result True there
    (Addendum 4: a model failure counts against the tool); a golden
    smoke unit never counts, so it records False."""
    info, prepare = _prepare_sandboxed_store_run(
        tmp_path, monkeypatch, "golden"
    )
    cfg = _sandboxed_store_cfg(
        tmp_path, info, "golden",
        model_runner=_transport_failing_runner,
    )
    orch = Orchestrator(cfg)
    prepare(orch)
    summary = orch.measure_bug()
    assert summary["verdict"] == "inconclusive"
    assert summary["cause"] == "model_transport"
    evidence = _store_evidence(tmp_path, "golden")
    assert evidence["measured_result"] is False
    attempts = evidence["model_call_attempts"]
    assert attempts is not None and len(attempts) == 2
    assert all("model_transport" in a for a in attempts)


def test_golden_bug_unit_author_output_invalid_records_measured_result_false(
    tmp_path, monkeypatch
):
    """D171 (d): the model answers with no valid probe block — the
    run lands in the author-output-invalid record (Addendum 11
    rule 6), and a golden unit records measured_result False."""
    info, prepare = _prepare_sandboxed_store_run(
        tmp_path, monkeypatch, "golden"
    )
    cfg = _sandboxed_store_cfg(
        tmp_path, info, "golden",
        model_runner=_invalid_output_runner,
    )
    orch = Orchestrator(cfg)
    prepare(orch)
    summary = orch.measure_bug()
    assert summary["cause"] == "author_output_invalid"
    evidence = _store_evidence(tmp_path, "golden")
    assert evidence["measured_result"] is False
    assert evidence["inconclusive_cause"] == "author_output_invalid"


def test_golden_bug_unit_crash_records_measured_result_false(
    tmp_path, monkeypatch
):
    """D171 (e): provisioning explodes after authoring — the run
    lands in the orchestrator-crash record (cause tool_failure),
    and a golden unit records measured_result False there too."""
    info, prepare = _prepare_sandboxed_store_run(
        tmp_path, monkeypatch, "golden", crash=True
    )
    orch = Orchestrator(_sandboxed_store_cfg(tmp_path, info, "golden"))
    prepare(orch)
    summary = orch.measure_bug()
    assert summary["verdict"] == "inconclusive"
    assert summary["cause"] == "tool_failure"
    evidence = _store_evidence(tmp_path, "golden")
    assert evidence["measured_result"] is False
    assert evidence["inconclusive_cause"] == "tool_failure"
    assert "fake sandbox provisioning exploded" in (
        evidence["inconclusive_error"] or ""
    )


def test_golden_clean_unit_crash_records_measured_result_false(
    tmp_path, monkeypatch
):
    """D171 (e), clean side: the same crash on a golden CLEAN unit
    lands in the crash record with measured_result False."""
    info, prepare = _prepare_sandboxed_store_run(
        tmp_path, monkeypatch, "golden", crash=True
    )
    cfg = _sandboxed_store_cfg(
        tmp_path, info, "golden",
        bug_id=None, rev_bug_base=None, rev_bug=None, rev_fix=None,
        clean_id="mini-clean", rev_clean=info["fix"],
    )
    orch = Orchestrator(cfg)
    prepare(orch)
    summary = orch.measure_clean()
    assert summary["verdict"] == "inconclusive"
    assert summary["cause"] == "tool_failure"
    evidence = _store_evidence(tmp_path, "golden")
    assert evidence["measured_result"] is False
    assert evidence["inconclusive_cause"] == "tool_failure"
    assert "fake sandbox provisioning exploded" in (
        evidence["inconclusive_error"] or ""
    )


def test_non_golden_sandboxed_unit_still_records_measured_true(
    tmp_path, monkeypatch
):
    """D167/D171 control: identical setup, non-golden store owner,
    NORMAL completion — a sandboxed unit still records
    measured_result True (it IS a registered measurement)."""
    info, prepare = _prepare_sandboxed_store_run(
        tmp_path, monkeypatch, "mini"
    )
    orch = Orchestrator(_sandboxed_store_cfg(tmp_path, info, "mini"))
    prepare(orch)
    summary = orch.measure_bug()
    assert "cause" not in summary
    evidence = _store_evidence(tmp_path, "mini")
    assert evidence["measured_result"] is True
    assert evidence["inconclusive_cause"] is None
    assert evidence["crash_probe_results"] is None
    assert evidence["fix_results"]


# ----------------------------------------------------------------------
# D220/D221/D222: the container env, the leak gate, and the
# provision rehearsal.
#
# Blind run 39149875917 came back INCONCLUSIVE (environment): the
# measured path built the sandbox containers' env from ONLY the
# leak gate's declared-public keys, so the paperless contract's
# non-public config (its secret key, DB engine, data dir, throttle)
# never reached the container and both sides failed Django's boot
# check — while readiness, which passes every contract key, had
# proved the same contract READY. One list carried two meanings.
# D220 separates them: on the contract-store path the containers
# receive the contract's FULL env (readiness parity); off the store
# the public-keys filter stands; the leak gate's exemption list is
# unchanged either way. D221 adds the redaction registration for
# every non-public value. D222 (replacing D221's item 3) adds the
# --dry-run-provision rehearsal: both sides provisioned, the unit's
# FIXED rehearsal probe set run through the real executor, a
# record marked dry_run="provision", no model call.
#
# Plants (each run by hand against this file; see the PR body):
#   P1 the public-keys filter restored on the store path -> the
#      store-env and readiness-parity tests FAIL;
#   P2 the leak exemption widened to every contract key -> the
#      leak-gate test FAILS;
#   P3 the sandbox redaction registration skipped -> the redaction
#      test FAILS;
#   P4 the rehearsal calling the model -> the rehearsal test FAILS
#      (its model runner raises on any call).
# ----------------------------------------------------------------------

D220_ENV = {
    "APP_ENV": "test",
    "APP_BACKEND_PATH": "acme.backends.PublicBackend",
    "APP_SECRET_KEY": "d220-throwaway-secret-value",
    "APP_DBENGINE": "sqlite",
    "APP_DATA_DIR": "/checkout/data",
    "APP_THROTTLE_RATE": "600/min",
}
D220_PUBLIC_KEYS = ["APP_ENV", "APP_BACKEND_PATH"]
D220_SECRET_VALUE = D220_ENV["APP_SECRET_KEY"]

D220_STORE_YML = """\
repo_name: store-mini-app
app:
  path: "."
build: "python -m py_compile app.py"
start: "python app.py"
readiness: "/health"
env:
  APP_ENV: "test"
  APP_BACKEND_PATH: "acme.backends.PublicBackend"
  APP_SECRET_KEY: "d220-throwaway-secret-value"
  APP_DBENGINE: "sqlite"
  APP_DATA_DIR: "/checkout/data"
  APP_THROTTLE_RATE: "600/min"
fixture:
  repo_name: "store-mini-app"
  seed_note: "The app starts empty; GET /value reports the build's constant."
  auth_mechanism: "There are no user accounts and no login."
  flags_note: "There are no feature flags."
"""


def _d220_caps():
    from proofdeploy.sandbox import SandboxCapabilities

    return SandboxCapabilities(
        podman=True,
        podman_version="podman version 4.9.3",
        isolated_network=False,
        build_network=True,
        userns_remap=True,
        reason="",
    )


def _d220_drive(monkeypatch, tmp_path, *, store: bool, echo_secret: bool = False):
    """Drive the REAL Orchestrator._provision_side_sandbox
    podman-free (the Addendum 18 pattern, as in
    test_provisioning_sandbox.py) with a six-key contract env of
    which only two keys are declared public. The fake install
    captures its env; the fake start captures its env and stops the
    side there. Returns (install_env, start_env, side)."""
    from proofdeploy import orchestrator as orch_mod
    from proofdeploy.provisioning import select_image
    from proofdeploy.sandbox import Sandbox, SandboxError
    from proofdeploy.yml import RepoContract

    snap = tmp_path / "snap"
    snap.mkdir()
    (snap / "requirements.txt").write_text(
        "six==1.17.0 \\\n"
        "    --hash=sha256:ff703d0fc3e08526949441584fca3fc209d20cf45a3f"
        "54c0fb84f79a80d0d95b1\n"
    )
    (snap / "app.py").write_text("VALUE = 1\n")

    row = select_image("python", None)
    assert row is not None
    monkeypatch.setattr(orch_mod, "_git", lambda *a: "fakesha1234567890abcdef")
    monkeypatch.setattr(
        orch_mod, "create_snapshot", lambda **kw: SimpleNamespace(dir=snap)
    )
    monkeypatch.setattr(orch_mod, "check_podman", lambda: (True, ""))
    monkeypatch.setattr(
        orch_mod,
        "check_sandbox_runtime",
        lambda *a: (row.version, row.digest, None),
    )
    monkeypatch.setattr(Sandbox, "_capabilities", lambda self: _d220_caps())

    captured: dict[str, dict] = {}

    def fake_install(self, checkout, steps, env, proxy_env, timeout=600):
        captured["install_env"] = dict(env)
        if echo_secret:
            # A -e KEY=VALUE command echo landing in the sandbox
            # log, carrying the raw non-public value verbatim.
            self.log.append(
                f"install env echo: APP_SECRET_KEY={env['APP_SECRET_KEY']}"
            )
        return [{"phase": s["phase"], "exit_code": 0} for s in steps]

    def fake_start(self, checkout, command, env, port, **kw):
        captured["start_env"] = dict(env)
        raise SandboxError("d220 test stops after capturing the app env")

    monkeypatch.setattr(Sandbox, "install_steps", fake_install)
    monkeypatch.setattr(Sandbox, "start", fake_start)

    if store:
        contract_file = tmp_path / "d220-store.yml"
        contract_file.write_text(D220_STORE_YML, encoding="utf-8")
        cfg = MeasureConfig(
            repo_dir=snap,
            output_dir=tmp_path / "out",
            workdir=tmp_path / "work",
            no_skill=True,
            public_contract_env_keys=list(D220_PUBLIC_KEYS),
            use_contract_store=True,
            contract_path=contract_file,
        )
    else:
        contract = RepoContract(
            build="",
            start="python app.py",
            readiness="/health",
            env=dict(D220_ENV),
        )
        monkeypatch.setattr(orch_mod, "load_contract", lambda p: contract)
        cfg = MeasureConfig(
            repo_dir=snap,
            output_dir=tmp_path / "out",
            workdir=tmp_path / "work",
            no_skill=True,
            public_contract_env_keys=list(D220_PUBLIC_KEYS),
        )
    orch = Orchestrator(cfg)
    unit_dir = tmp_path / "unit"
    unit_dir.mkdir(exist_ok=True)
    side = orch._provision_side_sandbox("fakerev", 18099, unit_dir, "d220")
    return captured["install_env"], captured["start_env"], side


def test_store_contract_provisions_full_env_to_install_and_app(
    monkeypatch, tmp_path
):
    """D220 (a): a store-contract side's install AND app containers
    receive the contract's full env — every key, with its value —
    even though only two keys are declared public. Plant P1
    (filter restored on the store path) fails this test."""
    from proofdeploy.runner import ProvisionStatus

    install_env, start_env, side = _d220_drive(
        monkeypatch, tmp_path, store=True
    )
    assert side.provision.status == ProvisionStatus.START_FAILED
    for env in (install_env, start_env):
        assert {k: env[k] for k in D220_ENV} == D220_ENV


def test_non_store_provisioning_still_filters_to_public_keys(
    monkeypatch, tmp_path
):
    """D220 (b): off the store (the verify/dev shape), provisioning
    still filters the container env to the declared public keys —
    the non-public keys never reach either container."""
    install_env, start_env, _side = _d220_drive(
        monkeypatch, tmp_path, store=False
    )
    for env in (install_env, start_env):
        assert {k for k in env if k in D220_ENV} == set(D220_PUBLIC_KEYS)
        assert env["APP_ENV"] == "test"
        assert "APP_SECRET_KEY" not in env


def test_non_public_contract_value_registered_for_sandbox_redaction(
    monkeypatch, tmp_path
):
    """D221 item 2: every non-public contract env value is
    registered with the sandbox redaction, exactly as the
    bootstrap values are — a log line echoing the value verbatim
    (a -e KEY=VALUE command echo) reaches the side's provision
    logs only in redacted form. Plant P3 (registration skipped)
    fails this test."""
    _install_env, _start_env, side = _d220_drive(
        monkeypatch, tmp_path, store=True, echo_secret=True
    )
    provision_logs = "\n".join(side.provision.logs)
    assert D220_SECRET_VALUE not in provision_logs
    # The echo line itself is present — redacted, not dropped.
    assert "install env echo: APP_SECRET_KEY=" in provision_logs


def test_readiness_and_measured_env_names_match_for_paperless(tmp_path):
    """D220 (c) PARITY: for the same store contract — the real
    paperless-ngx contract — readiness and the measured path
    produce the same container env NAMES: the six keys readiness
    proved READY with. Plant P1 fails this test."""
    readiness_names = rp.contract_env_keys("paperless-ngx/paperless-ngx")
    assert readiness_names == [
        "PAPERLESS_CACHE_BACKEND",
        "PAPERLESS_CHANNELS_BACKEND",
        "PAPERLESS_DATA_DIR",
        "PAPERLESS_DBENGINE",
        "PAPERLESS_SECRET_KEY",
        "PAPERLESS_TOKEN_THROTTLE_RATE",
    ]
    cfg = MeasureConfig(
        repo_dir=tmp_path,
        output_dir=tmp_path / "out",
        workdir=tmp_path / "work",
        no_skill=True,
        public_contract_env_keys=[
            "PAPERLESS_CHANNELS_BACKEND",
            "PAPERLESS_CACHE_BACKEND",
        ],
        use_contract_store=True,
        contract_path=(
            REPO_ROOT
            / "readiness"
            / "contracts"
            / "paperless-ngx__paperless-ngx.yml"
        ),
    )
    orch = Orchestrator(cfg)
    contract = orch._load_unit_contract()
    measured_names = sorted(orch._container_contract_env(contract))
    assert measured_names == readiness_names


def test_leak_gate_still_refuses_non_public_contract_value(
    monkeypatch, tmp_path
):
    """D220 (d): the leak gate is unchanged — only the declared
    public keys are exempt. A non-public contract value (the
    secret key's value) in the prompt OR in a bundle file is
    refused; a declared-public value is not. Plant P2 (exemption
    widened to all contract keys) fails this test."""
    from proofdeploy.leakcheck import SecretLeakError
    from proofdeploy.yml import RepoContract

    cfg = MeasureConfig(
        repo_dir=tmp_path,
        output_dir=tmp_path / "out",
        workdir=tmp_path / "work",
        no_skill=True,
        public_contract_env_keys=list(D220_PUBLIC_KEYS),
    )
    orch = Orchestrator(cfg)
    contract = RepoContract(
        build="", start="python app.py", readiness="/health",
        env=dict(D220_ENV),
    )
    snap = tmp_path / "snapshot"
    snap.mkdir()
    diff = tmp_path / "diff.patch"
    diff.write_text("benign diff text\n", encoding="utf-8")
    description = tmp_path / "description.txt"
    description.write_text("benign fixture text\n", encoding="utf-8")
    bundle = SimpleNamespace(
        diff=diff, description=description, skill=None, snapshot=snap
    )

    with pytest.raises(SecretLeakError):
        orch._leak_check_bundle(
            bundle, f"the prompt mentions {D220_SECRET_VALUE}", contract
        )
    diff.write_text(
        f"the diff mentions {D220_SECRET_VALUE}\n", encoding="utf-8"
    )
    with pytest.raises(SecretLeakError):
        orch._leak_check_bundle(bundle, "a clean prompt", contract)
    diff.write_text("benign diff text\n", encoding="utf-8")
    # Declared-public values are config the app itself prints:
    # exempt, exactly as before D220.
    orch._leak_check_bundle(
        bundle,
        "backend acme.backends.PublicBackend, env test",
        contract,
    )


def _d220_sandboxed_prepare(tmp_path, monkeypatch, owner="acme"):
    """The D171 sandboxed-store scaffolding, with the six-key
    contract: gates stubbed, the store resolved by the real slug
    rule from a temporary store tree, _provision_side_sandbox
    faked READY. Returns (info, prepare)."""
    import proofdeploy.orchestrator as orch_mod
    from proofdeploy.orchestrator import _Side
    from proofdeploy.runner import ProvisionResult, ProvisionStatus

    monkeypatch.setattr(orch_mod, "assert_measurement_gate", lambda: {})
    monkeypatch.setattr(
        orch_mod,
        "assert_harness_commit_gate",
        lambda: {
            "commit": "0" * 40,
            "source": "git",
            "dirty": False,
            "workflow_ref": None,
        },
    )
    # The capability probe has its own tests (test_sandbox.py);
    # stub it so the run's network-mode resolution does not shell
    # out to podman here (the test_sandbox.py pattern).
    monkeypatch.setattr(
        "proofdeploy.sandbox.check_sandbox_capabilities",
        lambda: _d220_caps(),
    )
    store_root = tmp_path / f"store-{owner}"
    committed = store_root / "readiness" / "contracts" / f"{owner}__mini.yml"
    committed.parent.mkdir(parents=True)
    committed.write_text(D220_STORE_YML, encoding="utf-8")
    monkeypatch.setattr(
        "proofdeploy.yml.proofdeploy_tree_root", lambda: store_root
    )
    monkeypatch.setattr(
        "proofdeploy.orchestrator.proofdeploy_tree_root",
        lambda: store_root,
    )
    info = make_mini_repo(tmp_path, in_tree=None)

    def prepare(orch: Orchestrator) -> None:
        contract = orch._load_unit_contract()

        def fake_provision_sandbox(rev, port, unit_dir, name):
            return _Side(
                rev=rev,
                sha=rev,
                snapshot_dir=tmp_path / "snap",
                contract=contract,
                provision=ProvisionResult(
                    status=ProvisionStatus.READY,
                    target_url=f"http://127.0.0.1:{port}",
                    run_id="fake",
                ),
                sandbox_evidence={"image": "fake"},
            )

        orch._provision_side_sandbox = fake_provision_sandbox

    return info, prepare


def _d220_sandboxed_cfg(tmp_path, info, owner="acme", **over):
    cfg = MeasureConfig(
        repo_dir=Path(info["repo"]),
        output_dir=tmp_path / "records",
        workdir=tmp_path / "work",
        skill_path=skill_path(),
        bug_id="mini-bug",
        rev_bug_base=info["base"],
        rev_bug=info["bug"],
        rev_fix=info["fix"],
        model_runner=over.pop("model_runner", canned_probes_store),
        public_contract_env_keys=list(D220_PUBLIC_KEYS),
        sandbox=True,
        allow_unsandboxed=False,
        use_contract_store=True,
        unit_repo=f"{owner}/mini",
    )
    for key, value in over.items():
        setattr(cfg, key, value)
    return cfg


def test_store_measure_record_carries_container_env_keys(
    tmp_path, monkeypatch
):
    """D220 (a, record half): a sandboxed store measure's evidence
    record names every contract env key in container_env_keys
    (names only), is stamped 1.8.0, and is not a dry run. The
    non-public VALUE appears nowhere in the written record."""
    info, prepare = _d220_sandboxed_prepare(tmp_path, monkeypatch)
    orch = Orchestrator(_d220_sandboxed_cfg(tmp_path, info))
    prepare(orch)
    summary = orch.measure_bug()
    assert "cause" not in summary
    evidence = read_records(tmp_path / "records")[0]
    assert evidence["schema_version"] == "1.8.0"
    assert evidence["container_env_keys"] == sorted(D220_ENV)
    assert evidence["dry_run"] is None
    assert evidence["contract_source"]["store_path"] == (
        "readiness/contracts/acme__mini.yml"
    )
    raw = (tmp_path / "records" / "runs.jsonl").read_text(encoding="utf-8")
    assert D220_SECRET_VALUE not in raw


def _exploding_runner(prompt: str) -> ModelResponse:
    raise AssertionError("the provision rehearsal called the model")


# The mini unit's fixed rehearsal set (written into the temporary
# store tree by the prepare below): the app's health endpoint and
# its value endpoint, through the real executor.
MINI_REHEARSAL = [
    {
        "setup": [],
        "act": {"method": "GET", "path": "/health"},
        "assert": [{"type": "status", "equals": 200}],
    },
    {
        "setup": [],
        "act": {"method": "GET", "path": "/value"},
        "assert": [
            {"type": "status", "equals": 200},
            {
                "type": "body",
                "json_path": {"path": "value", "equals": 1},
            },
        ],
    },
]


def _d222_prepare(tmp_path, monkeypatch, owner="acme"):
    """The D222 rehearsal scaffolding: the D220 sandboxed-store
    prepare, except the fake provision STARTS the mini app for
    real (the rehearsal runs its probes through the real executor
    against a live target) and the store tree also carries the
    unit's fixed rehearsal set. Returns (info, servers) — the
    caller terminates the servers."""
    import json as _json
    import os
    import urllib.request

    import proofdeploy.orchestrator as orch_mod
    from proofdeploy.orchestrator import _Side
    from proofdeploy.runner import ProvisionResult, ProvisionStatus

    monkeypatch.setattr(orch_mod, "assert_measurement_gate", lambda: {})
    monkeypatch.setattr(
        orch_mod,
        "assert_harness_commit_gate",
        lambda: {
            "commit": "0" * 40,
            "source": "git",
            "dirty": False,
            "workflow_ref": None,
        },
    )
    # The capability probe has its own tests (test_sandbox.py);
    # stub it so the run's network-mode resolution does not shell
    # out to podman here (the test_sandbox.py pattern).
    monkeypatch.setattr(
        "proofdeploy.sandbox.check_sandbox_capabilities",
        lambda: _d220_caps(),
    )
    store_root = tmp_path / f"store-{owner}"
    committed = store_root / "readiness" / "contracts" / f"{owner}__mini.yml"
    committed.parent.mkdir(parents=True)
    committed.write_text(D220_STORE_YML, encoding="utf-8")
    rehearsal = store_root / "readiness" / "rehearsal" / f"{owner}__mini.json"
    rehearsal.parent.mkdir(parents=True)
    rehearsal.write_text(_json.dumps(MINI_REHEARSAL), encoding="utf-8")
    monkeypatch.setattr(
        "proofdeploy.yml.proofdeploy_tree_root", lambda: store_root
    )
    monkeypatch.setattr(
        "proofdeploy.orchestrator.proofdeploy_tree_root",
        lambda: store_root,
    )
    info = make_mini_repo(tmp_path, in_tree=None)
    servers: list = []

    def prepare(orch: Orchestrator) -> None:
        contract = orch._load_unit_contract()

        def fake_provision_sandbox(rev, port, unit_dir, name):
            proc = subprocess.Popen(
                [sys.executable, "app.py"],
                cwd=info["repo"],
                env={**os.environ, "PORT": str(port)},
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                # The real runner starts targets in their own
                # session (stop_target kills the process GROUP).
                start_new_session=True,
            )
            servers.append(proc)
            url = f"http://127.0.0.1:{port}"
            for _ in range(100):
                try:
                    with urllib.request.urlopen(
                        f"{url}/health", timeout=1
                    ) as resp:
                        if resp.status == 200:
                            break
                except Exception:
                    import time

                    time.sleep(0.05)
            else:
                raise RuntimeError("mini app did not become ready")
            return _Side(
                rev=rev,
                sha=rev,
                snapshot_dir=tmp_path / "snap",
                contract=contract,
                provision=ProvisionResult(
                    status=ProvisionStatus.READY,
                    target_url=url,
                    run_id=f"fake-{name}",
                    proc=proc,
                ),
                sandbox_evidence={"image": "fake"},
            )

        orch._provision_side_sandbox = fake_provision_sandbox

    return info, prepare, servers


def test_dry_run_provision_rehearses_without_a_model_call(
    tmp_path, monkeypatch
):
    """D222 item 3: --dry-run-provision provisions BOTH sides
    through the measured path, runs the unit's FIXED rehearsal
    probe set through the REAL executor on both sides, writes a
    record marked dry_run="provision" carrying container_env_keys,
    per-side READY, and the per-probe rehearsal results in the
    per-side result fields — and never calls the model (the runner
    raises on any call: plant P4, the mode calling the model,
    fails this test by construction). No score record exists."""
    info, prepare, servers = _d222_prepare(tmp_path, monkeypatch)
    orch = Orchestrator(
        _d220_sandboxed_cfg(
            tmp_path, info,
            model_runner=_exploding_runner,
            dry_run_provision=True,
        )
    )
    prepare(orch)
    try:
        summary = orch.measure_bug()
    finally:
        for proc in servers:
            proc.terminate()
    assert summary["dry_run"] == "provision"
    assert summary["stopped_before_model_call"] is True
    assert summary["provision_rehearsal_ok"] is True
    assert summary["measured_result"] is False
    assert summary["container_env_keys"] == sorted(D220_ENV)
    for side_name in ("fix_parent", "fix"):
        assert summary["sides"][side_name]["ready"] is True
        assert summary["sides"][side_name]["all_passed"] is True
        assert summary["sides"][side_name]["probes"] == [
            {"index": 0, "verdict": "pass"},
            {"index": 1, "verdict": "pass"},
        ]

    records = read_records(tmp_path / "records")
    assert [r["record_type"] for r in records] == ["evidence"]
    evidence = records[0]
    assert evidence["dry_run"] == "provision"
    assert evidence["measured_result"] is False
    assert evidence["container_env_keys"] == sorted(D220_ENV)
    assert evidence["fix_parent_provision"]["status"] == "ready"
    assert evidence["fix_provision"]["status"] == "ready"
    # The per-probe rehearsal results ride the per-side result
    # fields, as full probe results from the real executor.
    for field in ("fix_parent_results", "fix_results"):
        assert [r["verdict"] for r in evidence[field]] == ["pass", "pass"]


def test_paperless_rehearsal_set_loads_through_the_probe_schema():
    """D222: the committed paperless rehearsal set
    (readiness/rehearsal/paperless-ngx__paperless-ngx.json, OUTSIDE
    the contract) loads through the REAL production probe parser —
    every probe validates — and carries exactly the ruled set:
    (a) authenticated GET /api/profile/ -> 200; (b) setup POST
    /api/tags/ capturing id, act GET /api/tags/${id}/ -> 200;
    (c) a db assertion over auth_user."""
    import json as _json

    from proofdeploy.probe import validate_probe

    path = (
        REPO_ROOT
        / "readiness"
        / "rehearsal"
        / "paperless-ngx__paperless-ngx.json"
    )
    probes = _json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(probes, list) and len(probes) == 3
    for probe in probes:
        validate_probe(probe)

    a, b, c = probes
    assert a["act"]["method"] == "GET"
    assert a["act"]["path"] == "/api/profile/"
    assert a["act"]["headers"]["Authorization"] == "Token ${AUTH_TOKEN}"
    assert a["assert"] == [{"type": "status", "equals": 200}]

    (step,) = b["setup"]
    assert step["type"] == "http"
    assert step["act"]["method"] == "POST"
    assert step["act"]["path"] == "/api/tags/"
    assert step["act"]["body"] == {"name": "pd-rehearsal"}
    assert step["capture"] == {"id": {"from": "body", "json_path": "id"}}
    assert b["act"]["method"] == "GET"
    assert b["act"]["path"] == "/api/tags/${id}/"
    assert b["assert"] == [{"type": "status", "equals": 200}]

    (db_assert,) = c["assert"]
    assert db_assert["type"] == "db"
    assert "auth_user" in db_assert["query"]
    assert db_assert["query"].startswith("SELECT COUNT(*) FROM auth_user")
    assert db_assert["count"] == 1
