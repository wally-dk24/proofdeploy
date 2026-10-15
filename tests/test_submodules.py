"""Plan 1.2(l) (D46): git submodules at their pinned commits in
every snapshot.

Unit tests run against local fixture repos through an injected
test-only transport (plain git fetch from a local path — production
code stays https-only). The refusal tests run against the PRODUCTION
transport: the protocol restriction lives in its git argv, so
``ext::`` / ``file://`` / ``ssh://`` are refused by git itself before
any transport spawns. One real test fetches Ghost's ``source`` theme
from GitHub at its recorded commit and verifies the extracted tree
byte-for-byte against an independently fetched copy.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import readiness.provision as rp
from proofdeploy import orchestrator as orch_mod
from proofdeploy import submodules as submodules_mod
from proofdeploy.fixture import SnapshotLinkError, create_snapshot
from proofdeploy.orchestrator import _Side
from proofdeploy.runner import ProvisionResult, ProvisionStatus
from proofdeploy.submodules import (
    HttpsSubmoduleTransport,
    SubmoduleFetchError,
    SubmoduleUnavailableError,
    _git_fetch,
    materialize_snapshot_submodules,
)

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "pd-test",
    "GIT_AUTHOR_EMAIL": "test@proofdeploy.invalid",
    "GIT_COMMITTER_NAME": "pd-test",
    "GIT_COMMITTER_EMAIL": "test@proofdeploy.invalid",
}


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=True, timeout=120,
        env={**os.environ, **_GIT_ENV},
    )
    return proc.stdout.strip()


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    return path


def _commit(
    repo: Path,
    files: dict[str, str],
    *,
    gitmodules: str | None = None,
    gitlinks: dict[str, str] | None = None,
    links: dict[str, str] | None = None,
) -> str:
    """Write files (+ optional .gitmodules, gitlink entries and
    symlinks) and commit; returns the commit SHA."""
    for rel, content in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        _git(repo, "add", rel)
    for rel, target in (links or {}).items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(target, p)
        _git(repo, "add", rel)
    if gitmodules is not None:
        (repo / ".gitmodules").write_text(gitmodules, encoding="utf-8")
        _git(repo, "add", ".gitmodules")
    for rel, sha in (gitlinks or {}).items():
        _git(repo, "update-index", "--add", "--cacheinfo", f"160000,{sha},{rel}")
    _git(repo, "commit", "-q", "-m", "fixture")
    return _git(repo, "rev-parse", "HEAD")


def _tree_sha(repo: Path, sha: str) -> str:
    return _git(repo, "rev-parse", f"{sha}^{{tree}}")


class _LocalTransport:
    """Test-only transport: the production fetch implementation
    without the protocol restriction, so fixtures can live at local
    paths. Injected explicitly by these tests; production code paths
    never see it."""

    def fetch(self, url: str, sha: str, scratch_dir: Path) -> tuple[Path, str]:
        return _git_fetch(scratch_dir / "repo", url, sha, restrict_protocols=False)


class _RecordingTransport:
    """Delegates to another transport and records every (url, sha)
    it was asked for, in order."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.calls: list[tuple[str, str]] = []

    def fetch(self, url: str, sha: str, scratch_dir: Path) -> tuple[Path, str]:
        self.calls.append((url, sha))
        return self.inner.fetch(url, sha, scratch_dir)


def _snapshot_of(repo: Path, sha: str, out: Path) -> Path:
    return create_snapshot(repo_dir=repo, commit_rev=sha, output_dir=out).dir


def _materialize(
    repo: Path, sha: str, snap: Path, tmp_path: Path, **kw: Any
) -> list[dict[str, Any]]:
    return materialize_snapshot_submodules(
        repo_dir=repo,
        sha=sha,
        snapshot_dir=snap,
        scratch_dir=tmp_path / "scratch",
        transport=kw.pop("transport", _LocalTransport()),
        **kw,
    )


# --- happy paths -------------------------------------------------------


def test_no_gitmodules_returns_empty(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "super")
    sha = _commit(repo, {"app.py": "print('hi')\n"})
    snap = _snapshot_of(repo, sha, tmp_path / "snap")
    assert _materialize(repo, sha, snap, tmp_path) == []
    assert (snap / "app.py").read_text(encoding="utf-8") == "print('hi')\n"


def test_gitlink_without_gitmodules_entry_is_not_fetched(
    tmp_path: Path,
) -> None:
    remote = _init_repo(tmp_path / "remote")
    remote_sha = _commit(remote, {"a.txt": "a\n"})
    repo = _init_repo(tmp_path / "super")
    sha = _commit(repo, {"app.py": "x\n"}, gitlinks={"vendored": remote_sha})
    snap = _snapshot_of(repo, sha, tmp_path / "snap")
    # The work order covers gitlinks LISTED in .gitmodules; an
    # unlisted gitlink is out of scope and stays an empty dir.
    assert _materialize(repo, sha, snap, tmp_path) == []
    assert list((snap / "vendored").iterdir()) == []


def test_materializes_listed_submodule_at_recorded_commit(
    tmp_path: Path,
) -> None:
    remote = _init_repo(tmp_path / "remote")
    remote_sha = _commit(
        remote, {"index.hbs": "<html></html>\n", "pkg.json": "{}\n"}
    )
    repo = _init_repo(tmp_path / "super")
    gitmodules = (
        '[submodule "source"]\n'
        "\tpath = themes/source\n"
        f"\turl = {remote}\n"
    )
    sha = _commit(
        repo, {"app.py": "x\n"}, gitmodules=gitmodules,
        gitlinks={"themes/source": remote_sha},
    )
    snap = _snapshot_of(repo, sha, tmp_path / "snap")
    assert list((snap / "themes" / "source").iterdir()) == []  # archive: empty
    records = _materialize(repo, sha, snap, tmp_path)
    assert records == [
        {
            "path": "themes/source",
            "url": str(remote),
            "commit": remote_sha,
            "tree_sha": _tree_sha(remote, remote_sha),
        }
    ]
    assert (snap / "themes" / "source" / "index.hbs").read_text(
        encoding="utf-8"
    ) == "<html></html>\n"
    assert (snap / "themes" / "source" / "pkg.json").exists()


def test_relative_url_resolves_against_superproject_origin(
    tmp_path: Path,
) -> None:
    remote = _init_repo(tmp_path / "remote")
    remote_sha = _commit(remote, {"a.txt": "a\n"})
    repo = _init_repo(tmp_path / "super")
    gitmodules = (
        '[submodule "source"]\n'
        "\tpath = themes/source\n"
        "\turl = ../../TryGhost/Source.git\n"
    )
    sha = _commit(
        repo, {"app.py": "x\n"}, gitmodules=gitmodules,
        gitlinks={"themes/source": remote_sha},
    )
    snap = _snapshot_of(repo, sha, tmp_path / "snap")

    class _AnyUrlLocalTransport:
        """Serves any URL from the fixture remote, so the test can
        assert the URL the resolver produced without fetching it."""

        def fetch(self, url: str, sha_: str, scratch_dir: Path) -> tuple[Path, str]:
            return _git_fetch(
                scratch_dir / "repo", str(remote), sha_,
                restrict_protocols=False,
            )

    recording = _RecordingTransport(_AnyUrlLocalTransport())
    records = _materialize(
        repo, sha, snap, tmp_path,
        transport=recording, base_url="https://github.com/TryGhost/Ghost.git",
    )
    assert recording.calls == [
        ("https://github.com/TryGhost/Source.git", remote_sha)
    ]
    assert records[0]["url"] == "https://github.com/TryGhost/Source.git"
    assert (snap / "themes" / "source" / "a.txt").exists()


def test_nested_submodule_materializes_recursively(tmp_path: Path) -> None:
    inner = _init_repo(tmp_path / "inner")
    inner_sha = _commit(inner, {"deep.txt": "deep\n"})
    middle = _init_repo(tmp_path / "middle")
    middle_modules = (
        '[submodule "inner"]\n'
        "\tpath = vendor/inner\n"
        f"\turl = {inner}\n"
    )
    middle_sha = _commit(
        middle, {"mid.txt": "mid\n"}, gitmodules=middle_modules,
        gitlinks={"vendor/inner": inner_sha},
    )
    repo = _init_repo(tmp_path / "super")
    gitmodules = (
        '[submodule "middle"]\n'
        "\tpath = libs/middle\n"
        f"\turl = {middle}\n"
    )
    sha = _commit(
        repo, {"app.py": "x\n"}, gitmodules=gitmodules,
        gitlinks={"libs/middle": middle_sha},
    )
    snap = _snapshot_of(repo, sha, tmp_path / "snap")
    records = _materialize(repo, sha, snap, tmp_path)
    assert [r["path"] for r in records] == ["libs/middle", "libs/middle/vendor/inner"]
    assert records[1]["commit"] == inner_sha
    assert records[1]["tree_sha"] == _tree_sha(inner, inner_sha)
    assert (snap / "libs" / "middle" / "mid.txt").exists()
    assert (snap / "libs" / "middle" / "vendor" / "inner" / "deep.txt").read_text(
        encoding="utf-8"
    ) == "deep\n"


# --- failure typing ----------------------------------------------------


def test_unserved_commit_is_typed_not_supported(tmp_path: Path) -> None:
    remote = _init_repo(tmp_path / "remote")
    _commit(remote, {"a.txt": "a\n"})
    elsewhere = _init_repo(tmp_path / "elsewhere")
    unserved_sha = _commit(elsewhere, {"b.txt": "b\n"})
    repo = _init_repo(tmp_path / "super")
    gitmodules = (
        '[submodule "source"]\n'
        "\tpath = themes/source\n"
        f"\turl = {remote}\n"
    )
    sha = _commit(
        repo, {"app.py": "x\n"}, gitmodules=gitmodules,
        gitlinks={"themes/source": unserved_sha},
    )
    snap = _snapshot_of(repo, sha, tmp_path / "snap")
    with pytest.raises(SubmoduleUnavailableError) as excinfo:
        _materialize(repo, sha, snap, tmp_path)
    message = str(excinfo.value)
    assert message.startswith(
        f"not supported: submodule themes/source at {unserved_sha} "
        f"unavailable from {remote}: "
    )


def test_transient_fetch_failure_is_not_typed(tmp_path: Path) -> None:
    remote = _init_repo(tmp_path / "remote")
    remote_sha = _commit(remote, {"a.txt": "a\n"})
    repo = _init_repo(tmp_path / "super")
    gitmodules = (
        '[submodule "source"]\n'
        "\tpath = themes/source\n"
        f"\turl = {remote}\n"
    )
    sha = _commit(
        repo, {"app.py": "x\n"}, gitmodules=gitmodules,
        gitlinks={"themes/source": remote_sha},
    )
    snap = _snapshot_of(repo, sha, tmp_path / "snap")

    class _BoomTransport:
        def fetch(self, url: str, sha_: str, scratch_dir: Path) -> tuple[Path, str]:
            raise SubmoduleFetchError("connection reset by peer")

    with pytest.raises(SubmoduleFetchError):
        _materialize(repo, sha, snap, tmp_path, transport=_BoomTransport())
    # The harness's retry classification rests on this: the
    # transient class is NOT a ValueError (typed rows are built
    # from ValueErrors; harness-error rows are retried).
    assert not issubclass(SubmoduleFetchError, ValueError)
    assert issubclass(SubmoduleUnavailableError, ValueError)


def _super_with_url(tmp_path: Path, url: str, gitlink_sha: str) -> tuple[Path, str, Path]:
    repo = _init_repo(tmp_path / "super")
    gitmodules = (
        '[submodule "source"]\n'
        "\tpath = themes/source\n"
        f"\turl = {url}\n"
    )
    sha = _commit(
        repo, {"app.py": "x\n"}, gitmodules=gitmodules,
        gitlinks={"themes/source": gitlink_sha},
    )
    snap = _snapshot_of(repo, sha, tmp_path / "snap")
    return repo, sha, snap


def test_production_transport_refuses_ext_url(tmp_path: Path) -> None:
    repo, sha, snap = _super_with_url(tmp_path, "ext::true", "a" * 40)
    with pytest.raises(SubmoduleUnavailableError) as excinfo:
        _materialize(
            repo, sha, snap, tmp_path, transport=HttpsSubmoduleTransport()
        )
    message = str(excinfo.value)
    assert message.startswith("not supported: submodule themes/source at ")
    # R1: refused by the pre-fetch URL validation, before git runs.
    assert "not a clean https URL" in message


def test_production_transport_refuses_file_url(tmp_path: Path) -> None:
    remote = _init_repo(tmp_path / "remote")
    remote_sha = _commit(remote, {"a.txt": "a\n"})
    repo, sha, snap = _super_with_url(tmp_path, f"file://{remote}", remote_sha)
    with pytest.raises(SubmoduleUnavailableError) as excinfo:
        _materialize(
            repo, sha, snap, tmp_path, transport=HttpsSubmoduleTransport()
        )
    assert "not a clean https URL" in str(excinfo.value)
    # Nothing was extracted through the refused transport.
    assert list((snap / "themes" / "source").iterdir()) == []


def test_production_transport_refuses_ssh_url(tmp_path: Path) -> None:
    repo, sha, snap = _super_with_url(
        tmp_path, "ssh://git@127.0.0.1:1/x.git", "a" * 40
    )
    with pytest.raises(SubmoduleUnavailableError) as excinfo:
        _materialize(
            repo, sha, snap, tmp_path, transport=HttpsSubmoduleTransport()
        )
    assert "not a clean https URL" in str(excinfo.value)


def test_parent_path_in_gitmodules_is_refused(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "super")
    gitmodules = (
        '[submodule "evil"]\n'
        "\tpath = ../escape\n"
        "\turl = https://example.com/escape.git\n"
    )
    sha = _commit(repo, {"app.py": "x\n"}, gitmodules=gitmodules)
    snap = _snapshot_of(repo, sha, tmp_path / "snap")
    with pytest.raises(ValueError) as excinfo:
        _materialize(repo, sha, snap, tmp_path)
    # R4: the ADR 0012 refusal class of create_snapshot (fixture.py:359)
    # — a plain ValueError, NOT the typed not-supported class.
    assert not isinstance(excinfo.value, SubmoduleUnavailableError)
    message = str(excinfo.value)
    assert not message.startswith("not supported:")
    assert message == (
        "submodule path does not normalize inside the checkout: '../escape'"
    )
    assert not (tmp_path / "escape").exists()


def test_symlinked_parent_is_refused(tmp_path: Path) -> None:
    remote = _init_repo(tmp_path / "remote")
    remote_sha = _commit(remote, {"a.txt": "a\n"})
    repo = _init_repo(tmp_path / "super")
    gitmodules = (
        '[submodule "source"]\n'
        "\tpath = themes/source\n"
        f"\turl = {remote}\n"
    )
    sha = _commit(
        repo, {"app.py": "x\n"}, gitmodules=gitmodules,
        gitlinks={"themes/source": remote_sha},
    )
    snap = _snapshot_of(repo, sha, tmp_path / "snap")
    # Replace the real themes/ dir with a symlink out of the tree.
    shutil.rmtree(snap / "themes")
    (snap / "themes").symlink_to(tmp_path / "remote", target_is_directory=True)
    with pytest.raises(ValueError) as excinfo:
        _materialize(repo, sha, snap, tmp_path)
    # R4: same ADR 0012 class as the parent-path refusal above.
    assert not isinstance(excinfo.value, SubmoduleUnavailableError)
    message = str(excinfo.value)
    assert not message.startswith("not supported:")
    assert "symlinked parent" in message


# --- R1: option injection — validate before any git process -----------


def _spy_on_subprocess(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Record every subprocess the submodules module tries to
    start; the spy raises, so a recorded call is itself a failure
    of the test that uses it."""
    calls: list[Any] = []

    def spy_run(*args: Any, **kwargs: Any) -> Any:
        calls.append(args)
        raise AssertionError("a subprocess started")

    monkeypatch.setattr(submodules_mod.subprocess, "run", spy_run)
    return calls


@pytest.mark.parametrize(
    "bad_url",
    [
        "--upload-pack=touch x",  # would be parsed as a git option
        "-cfoo=bar",  # likewise: a -c injection
        "https://a b",  # whitespace in the URL
        "http://example.com/x.git",  # not https
        "ext::true",
        "file:///tmp/x",
        "ssh://git@example.com/x.git",
    ],
)
def test_production_transport_refuses_hostile_urls_before_any_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad_url: str
) -> None:
    calls = _spy_on_subprocess(monkeypatch)
    with pytest.raises(SubmoduleUnavailableError) as excinfo:
        HttpsSubmoduleTransport().fetch(bad_url, "a" * 40, tmp_path / "scratch")
    assert calls == []  # refused before any git process ran
    assert "not a clean https URL" in (excinfo.value.detail or "")


@pytest.mark.parametrize(
    "bad_sha",
    [
        "A" * 40,  # uppercase hex is not lowercase hex
        "a" * 39,  # too short
        "a" * 41,  # too long
        "g" * 40,  # not hex at all
        "--upload-pack=touch x",  # an option, not a SHA
    ],
)
def test_production_transport_refuses_bad_shas_before_any_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad_sha: str
) -> None:
    calls = _spy_on_subprocess(monkeypatch)
    with pytest.raises(SubmoduleUnavailableError) as excinfo:
        HttpsSubmoduleTransport().fetch(
            "https://example.com/x.git", bad_sha, tmp_path / "scratch"
        )
    assert calls == []
    assert "40-character lowercase hex" in (excinfo.value.detail or "")


# --- R2: symlinks inside the fetched tree, through the real extract ----


def _super_for_remote(
    tmp_path: Path, remote: Path, remote_sha: str
) -> tuple[Path, str, Path]:
    repo = _init_repo(tmp_path / "super")
    gitmodules = (
        '[submodule "source"]\n'
        "\tpath = themes/source\n"
        f"\turl = {remote}\n"
    )
    sha = _commit(
        repo, {"app.py": "x\n"}, gitmodules=gitmodules,
        gitlinks={"themes/source": remote_sha},
    )
    snap = _snapshot_of(repo, sha, tmp_path / "snap")
    return repo, sha, snap


def test_extracted_tree_refuses_escaping_symlink(tmp_path: Path) -> None:
    remote = _init_repo(tmp_path / "remote")
    remote_sha = _commit(
        remote, {"a.txt": "a\n"}, links={"docs/escape": "../../../etc/passwd"}
    )
    repo, sha, snap = _super_for_remote(tmp_path, remote, remote_sha)
    with pytest.raises(SnapshotLinkError) as excinfo:
        _materialize(repo, sha, snap, tmp_path)
    # The very class create_snapshot raises (fixture.py:157), its
    # path re-based to the snapshot root; no not-supported prefix.
    message = str(excinfo.value)
    assert message == (
        "snapshot symlink refused (symlink-target-escapes-tree): "
        "themes/source/docs/escape -> ../../../etc/passwd"
    )
    dest = snap / "themes" / "source"
    assert not os.path.lexists(dest / "docs" / "escape")
    assert not [p for p in dest.rglob("*") if p.is_symlink()]


def test_extracted_tree_refuses_absolute_symlink(tmp_path: Path) -> None:
    remote = _init_repo(tmp_path / "remote")
    remote_sha = _commit(
        remote, {"a.txt": "a\n"}, links={"abs": "/etc/passwd"}
    )
    repo, sha, snap = _super_for_remote(tmp_path, remote, remote_sha)
    with pytest.raises(SnapshotLinkError) as excinfo:
        _materialize(repo, sha, snap, tmp_path)
    message = str(excinfo.value)
    assert message == (
        "snapshot symlink refused (symlink-target-absolute): "
        "themes/source/abs -> /etc/passwd"
    )
    dest = snap / "themes" / "source"
    assert not os.path.lexists(dest / "abs")
    assert not [p for p in dest.rglob("*") if p.is_symlink()]


def test_extracted_tree_creates_internal_relative_symlinks_verbatim(
    tmp_path: Path,
) -> None:
    remote = _init_repo(tmp_path / "remote")
    remote_sha = _commit(
        remote,
        {"real.txt": "data\n"},
        links={"alias.txt": "real.txt", "docs/inner": "../real.txt"},
    )
    repo, sha, snap = _super_for_remote(tmp_path, remote, remote_sha)
    records = _materialize(repo, sha, snap, tmp_path)
    assert [r["path"] for r in records] == ["themes/source"]
    dest = snap / "themes" / "source"
    assert os.readlink(dest / "alias.txt") == "real.txt"
    assert os.readlink(dest / "docs" / "inner") == "../real.txt"
    assert (dest / "real.txt").read_text(encoding="utf-8") == "data\n"


# --- R3: the size cap is our gap, enforced before writing --------------


def test_over_cap_tree_is_our_gap_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    remote = _init_repo(tmp_path / "remote")
    remote_sha = _commit(remote, {"big.txt": "x" * 64 + "\n"})
    repo, sha, snap = _super_for_remote(tmp_path, remote, remote_sha)
    monkeypatch.setattr(submodules_mod, "MAX_TREE_BYTES", 16)
    with pytest.raises(ValueError) as excinfo:
        _materialize(repo, sha, snap, tmp_path)
    # Our gap (D48 R1/R3): a plain ValueError, no not-supported
    # prefix — the cap is ProofDeploy's limit, not a repo property.
    assert not isinstance(excinfo.value, SubmoduleUnavailableError)
    message = str(excinfo.value)
    assert not message.startswith("not supported:")
    assert "(our gap)" in message
    # Refused from the listing, before anything was written: the
    # destination still holds only the archive's empty gitlink dir.
    dest = snap / "themes" / "source"
    assert [p for p in dest.rglob("*") if p.is_file()] == []


# --- R4: a relative URL with no / scp-style origin is harness-side -----


def _super_with_relative_url(tmp_path: Path, remote_sha: str) -> tuple[Path, str, Path]:
    repo = _init_repo(tmp_path / "super")
    gitmodules = (
        '[submodule "source"]\n'
        "\tpath = themes/source\n"
        "\turl = ../../TryGhost/Source.git\n"
    )
    sha = _commit(
        repo, {"app.py": "x\n"}, gitmodules=gitmodules,
        gitlinks={"themes/source": remote_sha},
    )
    snap = _snapshot_of(repo, sha, tmp_path / "snap")
    return repo, sha, snap


def test_relative_url_without_origin_is_harness_or_environment(
    tmp_path: Path,
) -> None:
    elsewhere = _init_repo(tmp_path / "elsewhere")
    remote_sha = _commit(elsewhere, {"a.txt": "a\n"})
    repo, sha, snap = _super_with_relative_url(tmp_path, remote_sha)
    # The fixture repo has no origin remote at all.
    with pytest.raises(SubmoduleFetchError) as excinfo:
        _materialize(repo, sha, snap, tmp_path)
    assert "harness or environment" in str(excinfo.value)


def test_relative_url_with_scp_style_origin_is_harness_or_environment(
    tmp_path: Path,
) -> None:
    elsewhere = _init_repo(tmp_path / "elsewhere")
    remote_sha = _commit(elsewhere, {"a.txt": "a\n"})
    repo, sha, snap = _super_with_relative_url(tmp_path, remote_sha)
    with pytest.raises(SubmoduleFetchError) as excinfo:
        _materialize(
            repo, sha, snap, tmp_path,
            base_url="git@github.com:TryGhost/Ghost.git",
        )
    assert "harness or environment" in str(excinfo.value)


# --- wiring: orchestrator wrapper + readiness row ----------------------


def test_sandbox_wrapper_attaches_records_to_the_provision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sentinel = [
        {
            "path": "themes/source",
            "url": "https://example.com/Source.git",
            "commit": "a" * 40,
            "tree_sha": "b" * 40,
        }
    ]
    calls: list[dict[str, Any]] = []

    def fake_materialize(**kw: Any) -> list[dict[str, Any]]:
        calls.append(kw)
        return list(sentinel)

    def fake_impl(
        self: Any, rev: str, port: int, unit_dir: Path, tag: str, **kw: Any
    ) -> _Side:
        return _Side(
            rev=rev,
            sha=kw["sha"],
            snapshot_dir=kw["snapshot"].dir,
            contract=None,  # type: ignore[arg-type]
            provision=ProvisionResult(status=ProvisionStatus.BUILD_FAILED),
        )

    monkeypatch.setattr(
        orch_mod, "materialize_snapshot_submodules", fake_materialize
    )
    monkeypatch.setattr(
        orch_mod.Orchestrator, "_provision_side_sandbox_impl", fake_impl
    )
    repo = _init_repo(tmp_path / "super")
    sha = _commit(repo, {"app.py": "x\n"})
    cfg = orch_mod.MeasureConfig(
        repo_dir=repo, output_dir=tmp_path / "records", workdir=tmp_path / "work"
    )
    orch = orch_mod.Orchestrator(cfg)
    side = orch._provision_side_sandbox(sha, 0, tmp_path / "unit", "fix")
    assert side.provision.submodules == sentinel
    assert len(calls) == 1
    # The fetch scratch lives OUTSIDE the snapshot (D36).
    assert calls[0]["snapshot_dir"] == tmp_path / "unit" / "snapshot-fix"
    assert calls[0]["scratch_dir"] == tmp_path / "unit" / "submodule-scratch-fix"
    assert calls[0]["sha"] == sha


def test_readiness_row_carries_submodules() -> None:
    plan = {
        "declared_range": None,
        "selected_version": None,
        "package_manager": None,
        "refusal": None,
    }
    prov = ProvisionResult(
        status=ProvisionStatus.READY,
        runtime="node",
        install_plan=plan,
        install_plan_sha256="ab" * 32,
        logs=["ready"],
    )
    prov.submodules = [
        {
            "path": "themes/source",
            "url": "https://github.com/TryGhost/Source.git",
            "commit": "c" * 40,
            "tree_sha": "d" * 40,
        }
    ]
    side = SimpleNamespace(
        provision=prov, sandbox_evidence={"app_network_mode": "isolated"}
    )
    unit = {"unit_id": "T9", "repo": "TryGhost/Ghost"}
    row = rp.row_from_provision(
        unit, {"role": "C", "sha": "e" * 40}, "f" * 40, 1, None, side
    )
    assert row["submodules"] == prov.submodules


# --- the one real test: Ghost's source theme from GitHub ---------------


GHOST_SOURCE_COMMIT = "219dc7dbe62bb6dfa8db9fd651528571eae6cfd3"
# The tree of that commit, read from GitHub during this plan line's
# development and pinned here like a digest: the test proves the
# extracted tree IS this tree, byte for byte.
GHOST_SOURCE_TREE = "805457d7f7e89099691e37e9e2ac1a37c672bb48"


def _blob_sha(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def test_real_ghost_source_theme_fetched_at_recorded_commit(
    tmp_path: Path,
) -> None:
    repo = _init_repo(tmp_path / "ghost-like")
    gitmodules = (
        '[submodule "source"]\n'
        "\tpath = ghost/core/content/themes/source\n"
        "\turl = ../../TryGhost/Source.git\n"
    )
    sha = _commit(
        repo,
        {"proofdeploy.yml": "version: 1\n"},
        gitmodules=gitmodules,
        gitlinks={
            "ghost/core/content/themes/source": GHOST_SOURCE_COMMIT,
        },
    )
    snap = _snapshot_of(repo, sha, tmp_path / "snap")
    theme_dir = snap / "ghost" / "core" / "content" / "themes" / "source"
    assert list(theme_dir.iterdir()) == []

    records = materialize_snapshot_submodules(
        repo_dir=repo,
        sha=sha,
        snapshot_dir=snap,
        scratch_dir=tmp_path / "scratch",
        base_url="https://github.com/TryGhost/Ghost.git",
    )
    assert records == [
        {
            "path": "ghost/core/content/themes/source",
            "url": "https://github.com/TryGhost/Source.git",
            "commit": GHOST_SOURCE_COMMIT,
            "tree_sha": GHOST_SOURCE_TREE,
        }
    ]
    # The two templates whose absence is gscan-fatal are present.
    assert (theme_dir / "index.hbs").is_file()
    assert (theme_dir / "post.hbs").is_file()

    # Independent verification: fetch the same commit into the
    # test's own bare repo and compare every tree entry against the
    # extracted files' git blob ids.
    verify_repo = tmp_path / "verify" / "repo"
    _git_fetch(verify_repo, "https://github.com/TryGhost/Source.git",
               GHOST_SOURCE_COMMIT, restrict_protocols=True)
    assert _tree_sha(verify_repo, GHOST_SOURCE_COMMIT) == GHOST_SOURCE_TREE
    listing = subprocess.run(
        ["git", "-C", str(verify_repo), "ls-tree", "-r", "-z",
         GHOST_SOURCE_COMMIT],
        capture_output=True, text=True, check=True, timeout=120,
    ).stdout
    expected_files: dict[str, str] = {}
    expected_links: dict[str, str] = {}
    for entry in listing.split("\0"):
        if not entry:
            continue
        meta, _, rel = entry.partition("\t")
        mode, _kind, oid = meta.split(" ")
        if mode == "120000":
            expected_links[rel] = oid
        else:
            expected_files[rel] = oid
    actual_files = {
        p.relative_to(theme_dir).as_posix()
        for p in theme_dir.rglob("*")
        if p.is_file() and not p.is_symlink()
    }
    actual_links = {
        p.relative_to(theme_dir).as_posix()
        for p in theme_dir.rglob("*")
        if p.is_symlink()
    }
    assert actual_files == set(expected_files)
    assert actual_links == set(expected_links)
    for rel, oid in expected_files.items():
        assert _blob_sha((theme_dir / rel).read_bytes()) == oid, rel
    for rel, oid in expected_links.items():
        target = os.readlink(theme_dir / rel)
        assert _blob_sha(target.encode()) == oid, rel
