"""WO-7 PR 2: the conformance kit (cases 1–10 per adapter).

Drives the three golden apps (conformance/golden/) through the real
measured-run machinery in the sandbox with a CANNED author (fixed
probes, no model), plus direct sandbox provisions for the cases a
measured run cannot express (lockfile mismatch, forced start
failure, in-container network/env inspection).

These tests run in the sandbox-tests CI job. Locally they skip when
the sandbox cannot run; in CI a skip is a failure
(PD_REQUIRE_SANDBOX), so the kit cannot go green by skipping.

Fail-before: on the pre-PR-2 code the Node golden runs are
INCONCLUSIVE (the sandbox executor refused Node plans), the Python
provisions carry no executed_steps, and the kit itself does not
exist.
"""
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "conformance"))
import kit

from proofdeploy.orchestrator import (
    MeasureConfig,
    Orchestrator,
    _free_port,
)
from proofdeploy.runpair import read_records
from proofdeploy.sandbox import (
    PODMAN,
    sandbox_evidence,
)

_EVIDENCE = sandbox_evidence()
requires_sandbox = pytest.mark.skipif(
    not (
        _EVIDENCE.get("podman")
        and _EVIDENCE.get("userns_remap")
        and _EVIDENCE.get("build_network")
    ),
    reason=(
        "sandbox unavailable: "
        + ", ".join(
            k for k in ("podman", "userns_remap", "build_network")
            if not _EVIDENCE.get(k)
        )
    ),
)

pytestmark = requires_sandbox

APPS = list(kit.APPS)

# The frozen install markers each adapter must record (case 1):
# tokens that must appear in the recorded install argv.
FROZEN_INSTALL = {
    "node-express-sqlite": ["npm", "ci"],
    "node-pnpm-sqlite": ["/checkout/.pd-tools/node_modules/.bin/corepack",
                         "pnpm@9.15.0", "install", "--frozen-lockfile"],
    "python-flask-sqlite": ["install", "--require-hashes", "--no-deps"],
}

# The concrete install error each ecosystem prints when the lockfile
# disagrees with the manifest (case 2 evidence in the logs).
MISMATCH_MARKERS = {
    "node-express-sqlite": "EUSAGE",
    "node-pnpm-sqlite": "OUTDATED_LOCKFILE",
    "python-flask-sqlite": "DO NOT MATCH",
}


@pytest.fixture(scope="module", params=APPS, ids=APPS)
def golden(request, tmp_path_factory):
    """One golden app, fully measured: bug run + clean run."""
    app = request.param
    root = tmp_path_factory.mktemp(f"golden-{app}")
    repo = root / "repo"
    shas = kit.materialize_golden(app, repo)
    records = root / "records"
    records.mkdir()
    work = root / "work"
    work.mkdir()
    probes = kit.load_probes(app)

    bug_cfg = MeasureConfig(
        repo_dir=repo,
        output_dir=records,
        workdir=work,
        no_skill=True,
        bug_id=f"conformance-{app}",
        rev_bug_base=shas["base"],
        rev_bug=shas["bug"],
        rev_fix=shas["fix"],
        sandbox=True,
        model_runner=kit.canned_runner(probes["bug_probes"]),
    )
    bug_summary = Orchestrator(bug_cfg).measure_bug()

    clean_cfg = MeasureConfig(
        repo_dir=repo,
        output_dir=records,
        workdir=work,
        no_skill=True,
        clean_id=f"conformance-{app}",
        rev_clean=shas["clean"],
        sandbox=True,
        model_runner=kit.canned_runner(probes["clean_probes"]),
    )
    clean_summary = Orchestrator(clean_cfg).measure_clean()

    all_records = read_records(records)
    evidence = {
        r["run_kind"]: r
        for r in all_records
        if r.get("record_type") == "evidence"
    }
    # Addendum 35: every measured provision's full logs to pd-logs/
    # for the CI artifact (test evidence; records unchanged).
    for kind, ev in evidence.items():
        for key, side_name in (
            ("fix_parent_provision", "fix-parent"),
            ("fix_provision", "fix"),
            ("c_provision", "clean"),
        ):
            prov = ev.get(key)
            if prov:
                kit.dump_logs(
                    f"conformance-{app}-{kind}-{side_name}",
                    prov.get("logs") or [],
                )
    scores = {
        r["run_kind"]: r
        for r in all_records
        if r.get("record_type") == "score"
    }
    return SimpleNamespace(
        app=app,
        root=root,
        repo=repo,
        shas=shas,
        records_dir=records,
        workdir=work,
        bug=bug_summary,
        clean=clean_summary,
        evidence=evidence,
        scores=scores,
        canary=kit.canary_env_value(app),
    )


def _bug_provisions(g):
    ev = g.evidence["bug"]
    return ev["fix_parent_provision"], ev["fix_provision"]


# ---------------------------------------------------------------------------
# Cases 1, 3, 4 — from the bug run's committed provisions.
# ---------------------------------------------------------------------------


def test_case1_frozen_lockfile_honored(golden):
    """Case 1: the recorded install step is the adapter's frozen
    install for this app's lockfile, and it exited 0 on both sides.
    """
    want = FROZEN_INSTALL[golden.app]
    for provision in _bug_provisions(golden):
        steps = provision["executed_steps"]
        installs = [s for s in steps if s["phase"] == "install"]
        assert installs, (golden.app, steps)
        # Every install-phase step (a corepack bootstrap included)
        # ran in the sandbox and exited 0; the frozen install is the
        # step carrying the frozen form.
        for s in installs:
            assert s["exit_code"] == 0, (
                golden.app, s, "\n".join(provision["logs"]),
            )
            assert s["executor"] == "sandbox"
        install = next(
            s for s in installs
            if "--frozen-lockfile" in s["argv_rendered"]
            or "--require-hashes" in s["argv_rendered"]
            or s["argv_rendered"][:2] == ["npm", "ci"]
        )
        argv = install["argv_rendered"]
        for token in want:
            assert token in argv, (golden.app, argv)
        # The plan on the record is the adapter's, with the lockfile
        # hash the install consumed.
        assert provision["install_plan"]["persisted_dirs"]
        assert provision["lockfile_sha256"] == (
            provision["install_plan"]["lockfile_sha256"]
        )


def test_case3_declared_runtime_selected_and_reported(golden):
    """Case 3: the plan's selected version is the version the target
    reported inside the container, on both sides; the image is the
    pinned ref@digest."""
    for provision in _bug_provisions(golden):
        plan = provision["install_plan"]
        assert plan["selected_version"]
        assert provision["runtime_version"] == plan["selected_version"]
        assert "@sha256:" in plan["image_ref"]
        assert provision["image_digest"] == plan["image_digest"]


def test_case4_build_migrate_seed_run(golden):
    """Case 4: build, migrate and seed are recorded as executed steps
    with exit code 0 (and the seeded note is what case 7's probe
    reads, so the seed really landed)."""
    for provision in _bug_provisions(golden):
        by_phase = {
            s["phase"]: s for s in provision["executed_steps"]
        }
        for phase in ("build", "migrate", "seed"):
            assert phase in by_phase, (golden.app, sorted(by_phase))
            assert by_phase[phase]["exit_code"] == 0, (
                by_phase[phase], "\n".join(provision["logs"]),
            )


# ---------------------------------------------------------------------------
# Case 7 (bug pair) and case 8 (clean commit).
# ---------------------------------------------------------------------------


def test_case7_bug_pair_fails_at_parent_passes_at_fix(golden):
    """Case 7: FAIL at fix^, PASS at fix — a candidate catch — with
    both sides READY."""
    parent_prov, fix_prov = _bug_provisions(golden)
    assert golden.bug["parent_ready"], (
        golden.bug, "\n".join(parent_prov["logs"]),
    )
    assert golden.bug["fix_ready"], (
        golden.bug, "\n".join(fix_prov["logs"]),
    )
    assert golden.bug["verdict"] == "candidate_catch", golden.bug
    ev = golden.evidence["bug"]
    parent_verdicts = [r["verdict"] for r in ev["fix_parent_results"]]
    fix_verdicts = [r["verdict"] for r in ev["fix_results"]]
    assert "fail" in parent_verdicts, parent_verdicts
    assert fix_verdicts == ["pass"] * len(fix_verdicts), fix_verdicts


def test_case8_clean_commit_no_fail(golden):
    """Case 8: the clean behavior-changing commit produces no FAIL —
    no false alarm, the side READY."""
    c_prov = golden.evidence["clean"]["c_provision"]
    assert golden.clean["c_ready"], (
        golden.clean, "\n".join(c_prov["logs"]),
    )
    assert golden.clean["false_alarm"] is False, golden.clean
    ev = golden.evidence["clean"]
    verdicts = [r["verdict"] for r in ev["c_results"]]
    assert verdicts and all(v == "pass" for v in verdicts), verdicts


# ---------------------------------------------------------------------------
# Cases 9 and 10 — the committed records.
# ---------------------------------------------------------------------------


def test_case9_no_canary_in_records(golden, tmp_path):
    """Case 9: no committed record contains a REAL canary token or
    this app's canary env value. Positive controls first (Addendum
    33): a planted real token MUST be reported, and the golden
    apps' source-line form (the token prefix literal in code, which
    legitimately rides in the recorded prompt) must NOT be — a
    disabled or prefix-only scan fails one of these and turns the
    kit red."""
    planted = tmp_path / "planted"
    planted.mkdir()
    (planted / "runs.jsonl").write_text(
        '{"t": "pdcanary-0123456789abcdef"}\n', encoding="utf-8"
    )
    assert kit.scan_records_for_canary(planted, golden.canary) != []
    source_form = tmp_path / "source-form"
    source_form.mkdir()
    (source_form / "runs.jsonl").write_text(
        'token = "pdcanary-" + secrets.token_hex(8)\n', encoding="utf-8"
    )
    assert kit.scan_records_for_canary(source_form, golden.canary) == []
    violations = kit.scan_records_for_canary(golden.records_dir, golden.canary)
    assert violations == [], violations
    # The canary env value is not public, so it must not even appear
    # as a recorded contract env value.
    for ev in golden.evidence.values():
        assert golden.canary not in json.dumps(ev["contract_env"])


def test_case10_record_replays_to_same_verdict(golden):
    """Case 10 (Addendum 33): replaying the committed evidence —
    re-evaluating every probe's assertions against the RECORDED
    responses — reproduces the stored verdict. A tampered recorded
    response changes the replay outcome even though the stored
    per-probe verdicts are untouched, proving the replay re-checks
    the assertions instead of echoing the stored verdicts."""
    import copy

    evidence = golden.evidence["bug"]
    # Precondition (Addendum 35): a replay is only meaningful when
    # both sides actually provisioned — otherwise the failure is
    # the provisioning cascade, not a replay result, and must read
    # as such.
    for key in ("fix_parent_provision", "fix_provision"):
        prov = evidence[key]
        assert prov["status"] == "ready", (
            f"replay precondition: side not READY: {prov.get('reason')}"
        )
    stored = golden.scores["bug"]["verdict"]
    assert kit.replay_verdict(evidence) == stored
    tampered = copy.deepcopy(evidence)
    flipped = 0
    for result in tampered["fix_results"]:
        try:
            body = json.loads(result.get("http_body") or "")
        except json.JSONDecodeError:
            continue
        if isinstance(body, dict) and "done" in body:
            body["done"] = True  # the buggy value, on the fix side
            result["http_body"] = json.dumps(body)
            flipped += 1
    assert flipped >= 1
    # The stored verdicts in the tampered copy still claim the
    # catch; the replay must not.
    assert tampered["fix_results"][0]["verdict"] == "pass"
    assert kit.replay_verdict(tampered) != stored


# ---------------------------------------------------------------------------
# Cases 2, 5, 6 — direct sandbox provisions.
# ---------------------------------------------------------------------------


def _direct_orchestrator(g):
    cfg = MeasureConfig(
        repo_dir=g.repo,
        output_dir=g.records_dir,
        workdir=g.workdir,
        no_skill=True,
        sandbox=True,
    )
    return Orchestrator(cfg)


def test_case2_lockfile_mismatch_commit_refused(golden, tmp_path):
    """Case 2: the commit that changes a dependency without updating
    the lockfile fails the frozen install -> BUILD_FAILED, with the
    ecosystem's concrete mismatch error in the provision logs."""
    orch = _direct_orchestrator(golden)
    unit = tmp_path / "unit-mm"
    unit.mkdir()
    side = orch._provision_side_sandbox(
        golden.shas["lockmismatch"], _free_port(), unit, "mm"
    )
    provision = side.provision
    kit.dump_logs(
        f"conformance-{golden.app}-case2-lockmismatch", provision.logs
    )
    assert provision.status.value == "build_failed", (
        provision.reason, "\n".join(provision.logs),
    )
    marker = MISMATCH_MARKERS[golden.app]
    assert marker in "\n".join(provision.logs), marker
    failed_installs = [
        s for s in provision.executed_steps
        if s["phase"] == "install" and s["exit_code"] not in (0, None)
    ]
    assert failed_installs, provision.executed_steps


def test_case5_forced_start_failure_typed_with_error(golden, tmp_path):
    """Case 5: a start command that prints a concrete error and exits
    is a START_FAILED whose provision logs carry that error (never a
    bare 'not ready')."""
    # An ad-hoc commit on top of fix whose start prints the marker.
    repo = golden.repo
    subprocess.run(
        ["git", "-C", str(repo), "checkout", "-q", golden.shas["fix"]],
        check=True, capture_output=True, timeout=60,
    )
    yml_path = repo / "proofdeploy.yml"
    text = yml_path.read_text(encoding="utf-8")
    lines = [
        'start: "sh -c \'echo PD_START_MARKER_BOOM 1>&2; exit 1\'"'
        if line.startswith("start:") else line
        for line in text.splitlines()
    ]
    yml_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(repo), "add", "-A"], check=True,
        capture_output=True, timeout=60,
    )
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "-m", "broken start"],
        check=True, capture_output=True, timeout=60,
    )
    broken_sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True, timeout=60,
    ).stdout.strip()
    try:
        orch = _direct_orchestrator(golden)
        unit = tmp_path / "unit-broken"
        unit.mkdir()
        side = orch._provision_side_sandbox(
            broken_sha, _free_port(), unit, "broken"
        )
        provision = side.provision
        kit.dump_logs(
            f"conformance-{golden.app}-case5-broken-start", provision.logs
        )
        assert provision.status.value == "start_failed", (
            provision.reason, "\n".join(provision.logs),
        )
        assert "PD_START_MARKER_BOOM" in "\n".join(provision.logs)
        start = next(
            s for s in provision.executed_steps if s["phase"] == "start"
        )
        assert start["executor"] == "sandbox"
    finally:
        subprocess.run(
            ["git", "-C", str(repo), "checkout", "-q",
             golden.shas["lockmismatch"]],
            check=True, capture_output=True, timeout=60,
        )


def test_case6_network_cut_and_no_secret_in_container(golden, tmp_path):
    """Case 6: with the app READY in its container, (a) the canary
    env value (and the CANARY_SECRET name) is absent from the
    container environment, (b) an outbound HTTPS fetch from inside
    the container fails — the network is cut — while the executor's
    own path to the app still answers."""
    orch = _direct_orchestrator(golden)
    unit = tmp_path / "unit-net"
    unit.mkdir()
    side = orch._provision_side_sandbox(
        golden.shas["fix"], _free_port(), unit, "net"
    )
    provision = side.provision
    kit.dump_logs(f"conformance-{golden.app}-case6-fix", provision.logs)
    assert provision.ready, (
        provision.reason, "\n".join(provision.logs),
    )
    app = provision.proc
    try:
        env_out = subprocess.run(
            PODMAN + ["exec", app.name, "env"],
            capture_output=True, text=True, timeout=60,
        )
        assert env_out.returncode == 0, env_out.stderr
        assert golden.canary not in env_out.stdout
        assert "CANARY_SECRET" not in env_out.stdout
        if golden.app.startswith("node"):
            fetch = ["node", "-e",
                     "fetch('https://pypi.org').then("
                     "()=>process.exit(0)).catch(()=>process.exit(1))"]
        else:
            fetch = ["python3", "-c",
                     "import urllib.request;"
                     "urllib.request.urlopen('https://pypi.org', timeout=5)"]
        net = subprocess.run(
            PODMAN + ["exec", app.name, *fetch],
            capture_output=True, text=True, timeout=60,
        )
        assert net.returncode != 0, (
            "outbound fetch from the app container unexpectedly worked"
        )
        # The executor's path to the app still works.
        import urllib.request

        with urllib.request.urlopen(
            provision.target_url + "/health", timeout=10
        ) as resp:
            assert resp.status == 200
    finally:
        app.stop()
