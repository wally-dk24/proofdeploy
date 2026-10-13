"""Tests for REG-B: no-credential fixture template selection (WO-8.2).

The choice between the two frozen templates is mechanical (Addendum 44;
docs/registration/fixture-template-nocredential.md item 3): a contract
whose proofdeploy.yml declares a top-level `auth:` mapping uses
fixture_template.txt; any other contract uses
fixture_template_nocredential.txt. The selection happens before any
model call, the bundle manifest records `fixture_template: {file,
sha256}`, and the answer-key check recomputes the selection and refuses
a mismatch.
"""

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from proofdeploy import fixture
from proofdeploy.leakcheck import AnswerKeyLeakError, check_bundle_answer_key
from proofdeploy.yml import load_contract

# Registered sha256 values (registration doc, item 4). The tests pin the
# shipped files and the recorded manifest values to these constants.
CREDENTIAL_TEMPLATE_SHA256 = (
    "0994d1e367ddb3c679e750451dd67eab3a3d974fc1620b7f750216cdcdf0323b"
)
NO_CREDENTIAL_TEMPLATE_SHA256 = (
    "6cb8edafa3211ccd3c275648bf3a2cc5ddadbbc876aa65d8105be5186b4188e9"
)

FIELDS = {
    "repo_name": "ak",
    "seed_note": "seed",
    "auth_mechanism": "none",
    "auth_scope": "none",
    "flags_note": "none",
}

EXPECTED_CREDENTIAL_DESC = (
    "Repository: ak\n"
    "Seed data: seed\n"
    "Authentication: none. Your token: ${AUTH_TOKEN}.\n"
    "Token scope: none. "
    "No other users, roles, or credentials exist beyond what the seed creates.\n"
    "Feature flags: none.\n"
    "You may create users, roles, data, or change settings in setup steps "
    "before your probes run.\n"
)

EXPECTED_NO_CREDENTIAL_DESC = (
    "Repository: ak\n"
    "Seed data: seed\n"
    "Authentication: none. You are given no token or credentials.\n"
    "No users, roles, or credentials exist beyond what the seed creates.\n"
    "Feature flags: none.\n"
    "You may create users, roles, data, or change settings in setup steps "
    "before your probes run.\n"
)


# ---------------------------------------------------------------------------
# The mechanical selection itself.
# ---------------------------------------------------------------------------


def test_selection_filenames():
    assert fixture.template_filename(True) == "fixture_template.txt"
    assert fixture.template_filename(False) == "fixture_template_nocredential.txt"


def test_both_templates_pinned_to_registered_sha256():
    cred = fixture.template_path("fixture_template.txt")
    nocred = fixture.template_path("fixture_template_nocredential.txt")
    assert hashlib.sha256(cred.read_bytes()).hexdigest() == CREDENTIAL_TEMPLATE_SHA256
    assert hashlib.sha256(nocred.read_bytes()).hexdigest() == NO_CREDENTIAL_TEMPLATE_SHA256
    assert fixture.REGISTERED_TEMPLATE_SHA256 == {
        "fixture_template.txt": CREDENTIAL_TEMPLATE_SHA256,
        "fixture_template_nocredential.txt": NO_CREDENTIAL_TEMPLATE_SHA256,
    }


def test_render_no_credential_template_without_auth_scope():
    """The no-credential template has no {auth_scope}; it is not required."""
    text = fixture.template_path(
        "fixture_template_nocredential.txt"
    ).read_text(encoding="utf-8")
    fields = {k: v for k, v in FIELDS.items() if k != "auth_scope"}
    assert fixture.render_description(text, fields) == EXPECTED_NO_CREDENTIAL_DESC


def test_render_no_credential_template_byte_for_byte_with_all_fields():
    """Extra fixture fields (incl. auth_scope) do not change the output."""
    text = fixture.template_path(
        "fixture_template_nocredential.txt"
    ).read_text(encoding="utf-8")
    assert fixture.render_description(text, FIELDS) == EXPECTED_NO_CREDENTIAL_DESC


def test_render_credential_template_byte_for_byte():
    text = fixture.template_path("fixture_template.txt").read_text(encoding="utf-8")
    assert fixture.render_description(text, FIELDS) == EXPECTED_CREDENTIAL_DESC


def test_render_credential_template_still_requires_auth_scope():
    text = fixture.template_path("fixture_template.txt").read_text(encoding="utf-8")
    fields = {k: v for k, v in FIELDS.items() if k != "auth_scope"}
    with pytest.raises(ValueError, match="missing fields: auth_scope"):
        fixture.render_description(text, fields)


# ---------------------------------------------------------------------------
# Bundle assembly records the selection in the manifest.
# ---------------------------------------------------------------------------


def _snapshot(tmp_path):
    snap = tmp_path / "snap"
    (snap / "src").mkdir(parents=True)
    (snap / "src" / "app.js").write_text("console.log(1);\n", encoding="utf-8")
    return snap


def _assemble(tmp_path, *, declares_credential, fields=None):
    diff = tmp_path / "b.patch"
    diff.write_text("diff --git a/x b/x\n", encoding="utf-8")
    name = fixture.template_filename(declares_credential)
    tpath = fixture.template_path(name)
    return fixture.assemble_author_bundle(
        diff_path=diff,
        snapshot_dir=_snapshot(tmp_path),
        yml_fixture=dict(FIELDS if fields is None else fields),
        output_dir=tmp_path / "bundle",
        expected_template_hash=fixture.template_hash(tpath),
        template_file=tpath,
        no_skill=True,
    )


def test_assemble_records_fixture_template_credential(tmp_path):
    bundle = _assemble(tmp_path, declares_credential=True)
    assert bundle.description.read_text(encoding="utf-8") == EXPECTED_CREDENTIAL_DESC
    manifest = json.loads(bundle.manifest.read_text(encoding="utf-8"))
    assert manifest["fixture_template"] == {
        "file": "fixture_template.txt",
        "sha256": CREDENTIAL_TEMPLATE_SHA256,
    }
    assert manifest["template_sha256"] == CREDENTIAL_TEMPLATE_SHA256


def test_assemble_records_fixture_template_no_credential(tmp_path):
    fields = {k: v for k, v in FIELDS.items() if k != "auth_scope"}
    bundle = _assemble(tmp_path, declares_credential=False, fields=fields)
    assert bundle.description.read_text(encoding="utf-8") == EXPECTED_NO_CREDENTIAL_DESC
    manifest = json.loads(bundle.manifest.read_text(encoding="utf-8"))
    assert manifest["fixture_template"] == {
        "file": "fixture_template_nocredential.txt",
        "sha256": NO_CREDENTIAL_TEMPLATE_SHA256,
    }
    assert manifest["template_sha256"] == NO_CREDENTIAL_TEMPLATE_SHA256


# ---------------------------------------------------------------------------
# The answer-key check recomputes the selection and refuses a mismatch.
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    p = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=True,
    )
    return p.stdout.strip()


_YML_NO_AUTH = """\
build: "true"
start: "true"
readiness: "/health"
fixture:
  repo_name: "ak"
  seed_note: "seed"
  auth_mechanism: "none"
  auth_scope: "none"
  flags_note: "none"
env: {}
"""

_YML_WITH_AUTH = """\
build: "true"
start: "true"
readiness: "/health"
fixture:
  repo_name: "ak"
  seed_note: "seed"
  auth_mechanism: "none"
  auth_scope: "none"
  flags_note: "none"
auth:
  mode: "register"
  path: "/register"
  method: "POST"
  username_field: "username"
  password_field: "password"
  token_json_path: "token"
env: {}
"""


def _repo(root: Path, yml: str) -> dict[str, str]:
    """Repo with base (B^) -> bug (B) -> fix, proofdeploy.yml at every rev."""
    repo = root / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "test")
    _git(repo, "config", "user.email", "test@test")
    (repo / "app.py").write_text("VALUE = 1\n")
    (repo / "proofdeploy.yml").write_text(yml)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "app.py").write_text("VALUE = 2\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "bug")
    bug = _git(repo, "rev-parse", "HEAD")
    (repo / "app.py").write_text("VALUE = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "Fix app value handling")
    fix = _git(repo, "rev-parse", "HEAD")
    return {"repo": str(repo), "base": base, "bug": bug, "fix": fix}


def _bundle(root: Path, info: dict[str, str], *, force_template: str | None = None):
    """Assemble a bundle the way the orchestrator does: the contract is
    loaded from B's snapshot and the template selected mechanically from
    it (unless force_template overrides, to seed an inverted selection).
    """
    from proofdeploy.model_client import build_author_prompt

    repo = Path(info["repo"])
    work = root / "work"
    work.mkdir(exist_ok=True)
    diff_text = _git(repo, "diff", f"{info['base']}..{info['bug']}", "--")
    diff_path = work / "diff.patch"
    diff_path.write_text(diff_text)
    snap_src = work / "snap-src"
    snap_src.mkdir(exist_ok=True)
    proc = subprocess.run(
        ["git", "-C", str(repo), "archive", info["bug"]],
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["tar", "-x", "-C", str(snap_src)],
        input=proc.stdout,
        capture_output=True,
        check=True,
    )
    contract = load_contract(snap_src / "proofdeploy.yml")
    name = force_template or fixture.template_filename(contract.auth is not None)
    tpath = fixture.template_path(name)
    bundle_dir = work / "bundle"
    bundle = fixture.assemble_author_bundle(
        diff_path=diff_path,
        snapshot_dir=snap_src,
        yml_fixture=contract.fixture,
        output_dir=bundle_dir,
        expected_template_hash=fixture.template_hash(tpath),
        template_file=tpath,
        source_sha=info["bug"],
        diff_range=f"{info['base']}..{info['bug']}",
        no_skill=True,
    )
    prompt, _ = build_author_prompt(
        skill_text=None,
        fixture_description=bundle.description.read_text(encoding="utf-8"),
        diff_text=diff_text,
        snapshot_dir=bundle_dir / "snapshot",
    )
    return {"bundle": bundle_dir, "manifest": bundle.manifest, "prompt": prompt}


def _check_kwargs(info: dict[str, str]) -> dict:
    repo = Path(info["repo"])
    fix_subject = _git(repo, "log", "-1", "--format=%s", info["fix"])
    return {
        "repo_dir": repo,
        "bug_sha": info["bug"],
        "bug_parent_sha": info["base"],
        "fix_sha": info["fix"],
        "fix_subject": fix_subject,
        "expected_skill_hash": "unused-in-no-skill-arm",
        "no_skill": True,
    }


def test_answer_key_no_auth_contract_uses_no_credential_template(tmp_path):
    info = _repo(tmp_path, _YML_NO_AUTH)
    ctx = _bundle(tmp_path, info)
    desc = (ctx["bundle"] / "fixture-description.txt").read_text(encoding="utf-8")
    assert desc == EXPECTED_NO_CREDENTIAL_DESC
    manifest = json.loads(ctx["manifest"].read_text(encoding="utf-8"))
    assert manifest["fixture_template"] == {
        "file": "fixture_template_nocredential.txt",
        "sha256": NO_CREDENTIAL_TEMPLATE_SHA256,
    }
    check_bundle_answer_key(
        ctx["bundle"], ctx["manifest"], ctx["prompt"], **_check_kwargs(info)
    )


def test_answer_key_auth_contract_uses_credential_template(tmp_path):
    info = _repo(tmp_path, _YML_WITH_AUTH)
    ctx = _bundle(tmp_path, info)
    desc = (ctx["bundle"] / "fixture-description.txt").read_text(encoding="utf-8")
    assert desc == EXPECTED_CREDENTIAL_DESC
    manifest = json.loads(ctx["manifest"].read_text(encoding="utf-8"))
    assert manifest["fixture_template"] == {
        "file": "fixture_template.txt",
        "sha256": CREDENTIAL_TEMPLATE_SHA256,
    }
    check_bundle_answer_key(
        ctx["bundle"], ctx["manifest"], ctx["prompt"], **_check_kwargs(info)
    )


def test_answer_key_refuses_manifest_fixture_template_tamper(tmp_path):
    info = _repo(tmp_path, _YML_NO_AUTH)
    ctx = _bundle(tmp_path, info)
    manifest = json.loads(ctx["manifest"].read_text(encoding="utf-8"))
    manifest["fixture_template"] = {
        "file": "fixture_template.txt",
        "sha256": CREDENTIAL_TEMPLATE_SHA256,
    }
    ctx["manifest"].write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(AnswerKeyLeakError, match="fixture_template"):
        check_bundle_answer_key(
            ctx["bundle"], ctx["manifest"], ctx["prompt"], **_check_kwargs(info)
        )


def test_answer_key_refuses_manifest_fixture_template_missing(tmp_path):
    info = _repo(tmp_path, _YML_NO_AUTH)
    ctx = _bundle(tmp_path, info)
    manifest = json.loads(ctx["manifest"].read_text(encoding="utf-8"))
    del manifest["fixture_template"]
    ctx["manifest"].write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(AnswerKeyLeakError, match="fixture_template"):
        check_bundle_answer_key(
            ctx["bundle"], ctx["manifest"], ctx["prompt"], **_check_kwargs(info)
        )


def test_answer_key_refuses_inverted_selection(tmp_path):
    """A no-auth contract whose bundle was rendered from the credential
    template (selection inverted) must be refused by the recomputation."""
    info = _repo(tmp_path, _YML_NO_AUTH)
    ctx = _bundle(tmp_path, info, force_template="fixture_template.txt")
    with pytest.raises(AnswerKeyLeakError):
        check_bundle_answer_key(
            ctx["bundle"], ctx["manifest"], ctx["prompt"], **_check_kwargs(info)
        )


# ---------------------------------------------------------------------------
# The registered-hash comparison itself, and the assembler's fail-closed
# hash (Addendum 48). Everywhere else the template is hashed against
# itself or against a manifest that was written from the same file, so a
# tampered template file is self-consistent; only the comparison against
# REGISTERED_TEMPLATE_SHA256 refuses it. These tests point the selection
# at a one-byte-tampered copy so that comparison is the only thing that
# can fire.
# ---------------------------------------------------------------------------


def _tampered_template_copy(tmp_path, name):
    """A copy of a frozen template with exactly one byte changed."""
    pristine = fixture.template_path(name).read_bytes()
    tampered = pristine.replace(b"Repository:", b"Repositorx:", 1)
    assert tampered != pristine
    assert len(tampered) == len(pristine)
    path = tmp_path / name
    path.write_bytes(tampered)
    return path


def _patch_template_path(monkeypatch, module, name, path):
    """Make module.template_path resolve `name` to the tampered copy."""
    real = module.template_path

    def fake(filename=None):
        if filename == name:
            return path
        return real(filename)

    monkeypatch.setattr(module, "template_path", fake)


def test_answer_key_refuses_tampered_template_file(tmp_path, monkeypatch):
    """Plant D's target: the registered-sha256 comparison inside
    check_bundle_answer_key. The bundle and manifest are assembled from
    the same tampered copy, so the manifest comparison and the
    re-render comparison both agree; only the registered-hash
    comparison can refuse."""
    info = _repo(tmp_path, _YML_NO_AUTH)
    name = "fixture_template_nocredential.txt"
    tampered = _tampered_template_copy(tmp_path, name)
    _patch_template_path(monkeypatch, fixture, name, tampered)
    ctx = _bundle(tmp_path, info)
    with pytest.raises(AnswerKeyLeakError, match="approved frozen template"):
        check_bundle_answer_key(
            ctx["bundle"], ctx["manifest"], ctx["prompt"], **_check_kwargs(info)
        )


def test_build_bundle_refuses_tampered_template(tmp_path, monkeypatch):
    """The orchestrator hands the assembler the REGISTERED hash, not a
    hash of the file itself, so assembly refuses a tampered template
    copy before any bundle is written."""
    from proofdeploy import orchestrator as orchestrator_mod
    from proofdeploy.orchestrator import MeasureConfig, Orchestrator

    info = _repo(tmp_path, _YML_NO_AUTH)
    name = "fixture_template_nocredential.txt"
    tampered = _tampered_template_copy(tmp_path, name)
    _patch_template_path(monkeypatch, orchestrator_mod, name, tampered)
    cfg = MeasureConfig(
        repo_dir=Path(info["repo"]),
        output_dir=tmp_path / "records",
        workdir=tmp_path / "work",
        no_skill=True,
        bug_id="regb-tampered",
        rev_bug_base=info["base"],
        rev_bug=info["bug"],
        rev_fix=info["fix"],
    )
    unit_dir = tmp_path / "work" / "unit"
    unit_dir.mkdir(parents=True)
    with pytest.raises(ValueError, match="template hash mismatch"):
        Orchestrator(cfg)._build_bundle(
            unit_dir,
            unit_id="regb-tampered",
            rev_base=info["base"],
            rev_tip=info["bug"],
        )
