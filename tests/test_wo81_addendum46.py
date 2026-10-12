"""WO-8.1 fix round (Addendum 46): the completeness check's
allowlist, the path-independent generated-lock hash, and the
hash-pinned resolver bootstrap.

The reviewer's findings, reproduced against the WO-8.1 branch:

1. ``check_requirements_frozen`` called three kinds of INCOMPLETE
   file complete: an include-only file (``-r base.txt`` with the
   include unhashed), an editable (``-e .``), and a pin whose
   ``--hash`` appears only inside a comment. Each then failed as
   BUILD_FAILED under ``--require-hashes`` where REG-A item 1
   sends it to generation. The fix is an allowlist: only the
   global pip options are exempt; ``-r``/``-c`` includes are
   followed (recursively, cycle-guarded) inside the checkout;
   editables and every other option are not complete; an inline
   comment is stripped before looking for ``--hash``.
2. The generated lock's recorded sha256 depended on the checkout
   path: uv's header embeds the full command, including
   ``--output-file <path>``, and the host executor renders a
   per-run ``{checkout}``, so fix^ and fix recorded different
   hashes for the same resolution. Fix: ``--no-header``.
3. The resolver bootstrap installed ``uv==0.13.0`` by version
   only. Fix: install from a COMMITTED requirements file pinning
   the manylinux x86_64 wheel BY HASH (PyPI file
   uv-0.13.0-py3-none-manylinux_2_17_x86_64.manylinux2014_x86_64.whl,
   sha256 52c7da0f...16ed), with ``--require-hashes --no-deps
   --only-binary :all:``; the plan records that file's sha256.

Fail-before: on the pre-round code every completeness case below
returns "complete" (None), the compile argv has no --no-header,
the bootstrap names a bare ``uv==0.13.0``, the plan records no
bootstrap hash, and the two checkout paths record two different
generated-lock hashes.
"""
import hashlib
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from proofdeploy import provisioning
from proofdeploy.provisioning import plan_provisioning, render_argv
from proofdeploy.runner import (
    Provisioner,
    ProvisionStatus,
    check_requirements_frozen,
)

CUTOFF = "2026-01-02T03:04:05Z"
PYPROJECT_312 = '[project]\nname = "t"\nrequires-python = ">=3.12,<3.13"\n'

# The uv 0.13.0 manylinux x86_64 wheel on PyPI (the file whose
# sha256 starts 52c7da0f, per Addendum 46; full digest verified
# against https://pypi.org/pypi/uv/0.13.0/json).
UV_WHEEL_SHA256 = (
    "52c7da0fd9056d9551124777ec77de53f0df7017daf8f98a19e947b7837416ed"
)

HASHED_REQ = (
    "six==1.17.0 \\\n"
    "    --hash=sha256:ff703d0fc3e08526949441584fca3fc209d20cf45a3f54c0fb84f79a80d0d95b1\n"
)


def _contract(**kw):
    base = {"build": None, "migrate": None, "seed": None,
            "start": "python app.py", "env": {}}
    base.update(kw)
    return SimpleNamespace(**base)


def _write(root: Path, rel: str, text: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def _install_steps(plan):
    return [s for s in plan.steps if s.phase == "install" and s.argv is not None]


def _generated_plan(tmp_path: Path):
    snap = tmp_path / "snap"
    snap.mkdir()
    (snap / "pyproject.toml").write_text(PYPROJECT_312, encoding="utf-8")
    (snap / "requirements.txt").write_text(
        "flask==3.1.0\n", encoding="utf-8"
    )
    plan = plan_provisioning(snap, _contract(), lock_cutoff=CUTOFF)
    assert plan.refusal is None
    assert plan.lock_source == "generated"
    return plan


# ---------------------------------------------------------------------------
# 1. The completeness check: allowlist + followed includes.
# ---------------------------------------------------------------------------


def test_include_only_file_with_unhashed_include_is_not_complete(tmp_path):
    """Reviewer's case 1: the top file's only content is an
    include; the included file is unhashed. The pair is NOT a
    complete lock — the include is followed and checked."""
    req = _write(tmp_path, "requirements.txt", "-r requirements/base.txt\n")
    _write(tmp_path, "requirements/base.txt", "flask==3.1.0\n")
    reason = check_requirements_frozen(req)
    assert reason is not None
    assert "no --hash" in reason


def test_editable_is_not_complete(tmp_path):
    """Reviewer's case 2: an editable install pins nothing."""
    req = _write(tmp_path, "requirements.txt", "-e .\n")
    reason = check_requirements_frozen(req)
    assert reason is not None
    assert "not a complete lock" in reason


def test_hash_only_inside_a_comment_is_not_complete(tmp_path):
    """Reviewer's case 3: ``--hash`` inside an inline comment is
    not a hash. The comment is stripped before the check."""
    req = _write(
        tmp_path, "requirements.txt", "flask==3.0.0  # --hash=sha256:abc\n"
    )
    reason = check_requirements_frozen(req)
    assert reason is not None
    assert "no --hash" in reason


def test_fully_hashed_include_tree_stays_complete(tmp_path):
    """The control: an include tree where every spec carries a
    hash IS complete (global options exempt, includes followed)."""
    req = _write(
        tmp_path,
        "requirements.txt",
        "--index-url https://pypi.org/simple\n-r requirements/base.txt\n",
    )
    _write(tmp_path, "requirements/base.txt", HASHED_REQ)
    assert check_requirements_frozen(req) is None


def test_include_outside_the_checkout_is_not_complete(tmp_path):
    snap = tmp_path / "snap"
    snap.mkdir()
    req = _write(snap, "requirements.txt", "-r ../outside.txt\n")
    _write(tmp_path, "outside.txt", HASHED_REQ)
    reason = check_requirements_frozen(req)
    assert reason is not None
    assert "outside the checkout" in reason


def test_url_include_is_not_complete(tmp_path):
    req = _write(
        tmp_path, "requirements.txt", "-r https://example.com/req.txt\n"
    )
    reason = check_requirements_frozen(req)
    assert reason is not None
    assert "not a complete lock" in reason


def test_missing_include_is_not_complete(tmp_path):
    req = _write(
        tmp_path, "requirements.txt", "-r requirements/missing.txt\n"
    )
    reason = check_requirements_frozen(req)
    assert reason is not None
    assert "does not exist" in reason


def test_include_cycle_terminates_and_stays_complete(tmp_path):
    """A includes B, B includes A: the cycle guard keeps the check
    finite, and a fully hashed cycle is complete."""
    req = _write(
        tmp_path, "requirements.txt", "-r requirements/a.txt\n"
    )
    _write(
        tmp_path, "requirements/a.txt", "-r ../requirements.txt\n" + HASHED_REQ
    )
    assert check_requirements_frozen(req) is None


def test_unlisted_option_is_not_complete(tmp_path):
    """Only the allowlisted global options are exempt; anything
    else (here --no-index) means the file alone is not the lock."""
    req = _write(tmp_path, "requirements.txt", "--no-index\n" + HASHED_REQ)
    reason = check_requirements_frozen(req)
    assert reason is not None
    assert "not a complete lock" in reason


def test_unhashed_constraint_include_is_not_complete(tmp_path):
    """-c/--constraint includes are followed the same way as -r."""
    req = _write(
        tmp_path, "requirements.txt", "-c constraints.txt\n" + HASHED_REQ
    )
    _write(tmp_path, "constraints.txt", "flask==3.1.0\n")
    reason = check_requirements_frozen(req)
    assert reason is not None
    assert "no --hash" in reason


# ---------------------------------------------------------------------------
# 2. --no-header: the generated lock's hash is path-independent.
# ---------------------------------------------------------------------------


def test_generated_compile_step_carries_no_header(tmp_path):
    plan = _generated_plan(tmp_path)
    resolve = next(
        s for s in _install_steps(plan) if "compile" in (s.argv or [])
    )
    assert "--no-header" in (resolve.argv or [])


GENERATED_LOCK_BODY = (
    "flask==3.1.0 \\\n"
    "    --hash=sha256:0123456789abcdef0123456789abcdef"
    "0123456789abcdef0123456789abcdef\n"
)


def test_generated_lock_sha256_is_checkout_path_independent(tmp_path):
    """Same inputs at two checkout paths record the SAME sha256.

    The fake resolver emulates uv faithfully: WITHOUT --no-header
    it writes the autogenerated header embedding the full
    --output-file path (the reviewer's real-run finding: identical
    inputs at different paths gave different hashes); with it, the
    file is the bare resolution. Pre-fix, the plan carries no
    --no-header, so the two sides record different hashes."""

    def fake_exec(cmd, cwd, timeout, logs, env):
        logs.append(f"$ {' '.join(cmd)} (mocked)")
        if "compile" in cmd:
            out = Path(cmd[cmd.index("--output-file") + 1])
            if "--no-header" in cmd:
                out.write_text(GENERATED_LOCK_BODY, encoding="utf-8")
            else:
                out.write_text(
                    "# This file was autogenerated by uv via the "
                    "following command:\n"
                    f"#    uv pip compile --output-file {out}\n"
                    + GENERATED_LOCK_BODY,
                    encoding="utf-8",
                )
        return True, None

    results = []
    for i, port in enumerate((18101, 18102)):
        snap = tmp_path / f"snap{i}"
        snap.mkdir()
        (snap / "requirements.txt").write_text(
            "requests==2.32.3\n", encoding="utf-8"
        )
        prov = Provisioner(workdir=tmp_path / f"w{i}")
        prov._exec = fake_exec  # type: ignore[method-assign]
        result = prov.provision(
            snapshot_dir=snap,
            contract=_contract(start=""),
            port=port,
            lock_cutoff=CUTOFF,
        )
        assert result.status == ProvisionStatus.BUILD_FAILED  # no start
        assert result.generated_lock is not None
        results.append(result.generated_lock)
    want = hashlib.sha256(GENERATED_LOCK_BODY.encode()).hexdigest()
    assert results[0]["sha256"] == want
    assert results[1]["sha256"] == want


# ---------------------------------------------------------------------------
# 3b. The resolver bootstrap is pinned BY HASH, from a committed file.
# ---------------------------------------------------------------------------


def _bootstrap_file() -> Path:
    return Path(provisioning.__file__).with_name(
        "uv_bootstrap_requirements.txt"
    )


def test_generated_bootstrap_is_hash_pinned_and_recorded(tmp_path):
    plan = _generated_plan(tmp_path)
    installs = _install_steps(plan)
    # No step installs the resolver by bare version any more.
    for s in installs:
        assert "uv==0.13.0" not in (s.argv or [])
    # Step order: tools venv, the writer that materializes the
    # committed bootstrap file into .pd-tools, the hash-checked
    # install from it, then the resolve.
    writer, boot = installs[1], installs[2]
    assert (writer.argv or [])[1] == "-c"
    assert (writer.argv or [])[3] == (
        "{checkout}/.pd-tools/uv-requirements.txt"
    )
    committed = _bootstrap_file()
    text = committed.read_text(encoding="utf-8")
    assert (writer.argv or [])[4] == text
    assert "uv==0.13.0" in text
    assert f"--hash=sha256:{UV_WHEEL_SHA256}" in text
    argv = boot.argv or []
    assert argv[0] == "{checkout}/.pd-tools/bin/pip"
    assert "--require-hashes" in argv
    assert "--no-deps" in argv
    assert "--only-binary" in argv and ":all:" in argv
    assert argv[argv.index("-r") + 1] == (
        "{checkout}/.pd-tools/uv-requirements.txt"
    )
    # The plan records the committed file's sha256.
    assert plan.lock_resolver_bootstrap_sha256 == hashlib.sha256(
        committed.read_bytes()
    ).hexdigest()


def _host_mapping(checkout: Path) -> dict:
    return {
        "{runtime_bin}": sys.executable,
        "{checkout}": str(checkout),
        "{venv}": str(checkout / ".venv"),
    }


def _run_step_for_real(step, checkout: Path) -> subprocess.CompletedProcess:
    cmd = render_argv(step.argv or [], _host_mapping(checkout), [])
    return subprocess.run(
        cmd, cwd=checkout, capture_output=True, text=True, timeout=600,
    )


def _run_bootstrap_for_real(plan, checkout: Path):
    """Run the plan's first three install steps (tools venv,
    bootstrap-file writer, hash-checked uv install) rendered for
    this host. Returns (completed_processes, first_failure)."""
    installs = _install_steps(plan)
    # The round's step shape: installs[1] is the writer step.
    assert (installs[1].argv or [])[1] == "-c"
    checkout.mkdir(parents=True, exist_ok=True)
    procs = []
    for step in installs[:3]:
        proc = _run_step_for_real(step, checkout)
        procs.append(proc)
        if proc.returncode != 0:
            return procs, proc
    return procs, None


def _real_generated_plan(tmp_path: Path, name: str):
    snap = tmp_path / name
    snap.mkdir()
    (snap / "requirements.txt").write_text(
        "flask==3.1.0\n", encoding="utf-8"
    )
    plan = plan_provisioning(snap, _contract(), lock_cutoff=CUTOFF)
    assert plan.lock_source == "generated"
    return plan


def test_bootstrap_install_for_real_installs_pinned_uv(tmp_path):
    """The rendered bootstrap steps, run for real against PyPI,
    install exactly uv 0.13.0 into .pd-tools."""
    plan = _real_generated_plan(tmp_path, "snap-good")
    checkout = tmp_path / "co-good"
    _, failure = _run_bootstrap_for_real(plan, checkout)
    assert failure is None, (
        failure.stdout + failure.stderr if failure else ""
    )
    uv_bin = checkout / ".pd-tools" / "bin" / "uv"
    assert uv_bin.is_file()
    out = subprocess.run(
        [str(uv_bin), "--version"],
        capture_output=True, text=True, timeout=60,
    )
    assert out.returncode == 0
    assert "0.13.0" in out.stdout


def test_wrong_bootstrap_hash_fails_the_install_step(tmp_path):
    """The control: a planted wrong hash in the materialized
    bootstrap file makes pip refuse the install (hash-checking
    mode), so the bootstrap step fails instead of installing an
    unverified uv."""
    plan = _real_generated_plan(tmp_path, "snap-bad")
    installs = _install_steps(plan)
    assert (installs[1].argv or [])[1] == "-c"  # the writer step
    checkout = tmp_path / "co-bad"
    checkout.mkdir()
    # Tools venv + writer run for real; the file lands in .pd-tools.
    for step in installs[:2]:
        proc = _run_step_for_real(step, checkout)
        assert proc.returncode == 0, proc.stdout + proc.stderr
    req_file = checkout / ".pd-tools" / "uv-requirements.txt"
    text = req_file.read_text(encoding="utf-8")
    assert UV_WHEEL_SHA256 in text
    # The plant: corrupt the one hash the install will check.
    req_file.write_text(
        text.replace(UV_WHEEL_SHA256, "0" * 64), encoding="utf-8"
    )
    proc = _run_step_for_real(installs[2], checkout)
    assert proc.returncode != 0
    assert "DO NOT MATCH THE HASHES" in (proc.stdout + proc.stderr)
    assert not (checkout / ".pd-tools" / "bin" / "uv").exists()
