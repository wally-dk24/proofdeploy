"""Git submodules at their pinned commits in every snapshot (plan 1.2(l), D46).

A snapshot created by :func:`proofdeploy.fixture.create_snapshot` is a
plain ``git archive`` of the side's commit: gitlink entries (mode
160000) arrive as EMPTY directories, because the archive carries no
submodule content. For a repo whose app needs its submodules — Ghost's
default theme ``source`` is one, and its absence makes the frontend
answer 500 — the snapshot must include each submodule's content at
exactly the commit the superproject records (Master's decision D46,
Option A: fetch the submodules; the contract's readiness path stays
unchanged).

This module materializes that content on the host, at snapshot time,
inside the two provisioning paths (host and sandbox). It never runs
anything from the repo: the only network operation is a ``git fetch``
of the recorded commit into a scratch bare repo OUTSIDE the checkout
(D36 — ProofDeploy's own state never lives in the app's tree). The
fetched tree is then extracted by plumbing (``ls-tree`` +
``cat-file``), byte-exact: a real submodule checkout — what D46
mirrors — contains the commit's FULL tree, while ``git archive``
would silently drop the files the tree's ``.gitattributes`` marks
``export-ignore`` (Ghost's Source theme drops its own
``pnpm-lock.yaml`` that way). Symlinks in the extracted tree are
validated against the WO-8.4b rule (ADR 0012) before any is created,
and no filter or hook from the repo is ever run: extraction executes
nothing from the submodule.

Safety (the URLs in ``.gitmodules`` are untrusted repo input):

* https only, enforced twice: the resolved URL and the recorded
  SHA are validated before any git process runs (R1 — a URL such
  as ``--upload-pack=…`` or ``-c…`` must never reach git's argv,
  where it would be parsed as an option; the fetch argv also ends
  its options with ``--end-of-options``), and git itself is
  restricted with
  ``-c protocol.allow=never -c protocol.https.allow=always``,
  which refuses ``file://``, ``ssh``, ``git://`` and ``ext::``
  before any transport is spawned;
* no credentials: ``GIT_TERMINAL_PROMPT=0``, an empty credential
  helper, and a hermetic config (system/global config files are not
  read, so a stray ``url.insteadOf`` rewrite cannot redirect the
  fetch);
* every argument is passed as argv, never through a shell;
* the submodule path must normalize to inside the checkout: ``..``
  components, absolute paths and symlinked parents are refused
  (ADR 0012);
* per-fetch limits, stated here: one fetch may take at most
  ``FETCH_TIMEOUT_SECONDS`` (300s), and one submodule's extracted
  tree may total at most ``MAX_TREE_BYTES`` (512 MiB). The size
  cap is summed from the tree listing and enforced BEFORE
  anything is written, and blobs are streamed to disk from one
  ``cat-file --batch`` process — never loaded whole into memory.
  Nested submodules are followed to ``MAX_DEPTH`` (5) levels
  below the side's own tree.

Failure typing (classes per the reviewer's R4 ruling, D48/D50):

* the recorded commit is unavailable — a URL refused by the R1
  validation or the protocol rules, a repository not found or
  private, or a commit the remote does not serve — raises
  :class:`SubmoduleUnavailableError`: a typed
  ``not supported: submodule <path> at <sha> unavailable from
  <url>: <detail>`` reason, where the detail is git's first error
  line for a fetch refusal (the H5 class: the readiness harness
  judges the side skipped with its reason published and never
  retries it);
* a submodule path refused by the path rules (absolute, ``..``,
  a symlinked parent) and a symlink escaping a fetched tree get
  the class ``create_snapshot`` already gives an ADR 0012
  refusal: a plain ``ValueError`` (fixture.py:359) or the
  ``SnapshotLinkError`` itself (fixture.py:157) — NEITHER
  carries the ``not supported:`` prefix;
* a tree over the size cap is OUR gap — the cap is our limit —
  and raises a plain ``ValueError`` whose message says so
  (``(our gap)``), never ``not supported:``;
* a relative URL whose superproject origin is missing or not an
  https URL (scp-style, a local path) is harness or environment
  — the origin is this checkout's configuration, not the
  repo's — and, like a network or other transient failure,
  raises :class:`SubmoduleFetchError`, the harness's existing
  retry class, never typed not-supported;
* there is never a silent fallback to an empty directory: a
  listed gitlink either materializes at its recorded commit or
  the side fails with one of the typings above.
"""

from __future__ import annotations

import os
import posixpath
import re
import subprocess
import urllib.parse
from pathlib import Path
from typing import Any, Protocol

from proofdeploy.fixture import SnapshotLinkError, _validate_snapshot_link

# Stated limits (work order 1.2(l) item 3): per-fetch wall clock,
# per-submodule extracted size, and submodule nesting depth below
# the side's own tree (the side's direct submodules are depth 1).
FETCH_TIMEOUT_SECONDS = 300
MAX_TREE_BYTES = 512 * 1024 * 1024
MAX_DEPTH = 5


class SubmoduleUnavailableError(ValueError):
    """A listed submodule cannot be materialized as committed.

    The message IS the typed reason (``not supported: ...``), in the
    same style as SnapshotLinkError: a ValueError whose text the
    readiness harness records verbatim as the side's typed reason.

    ``detail`` carries the refusal detail alone when the raising
    layer does not know the submodule's path (the fetch layer works
    per (url, sha)); materialize_snapshot_submodules re-raises with
    the full path-bearing message.
    """

    def __init__(self, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.detail = detail


class SubmoduleFetchError(RuntimeError):
    """A transient submodule fetch failure (network class).

    Deliberately NOT a ValueError: the readiness harness types
    ValueErrors into rows but lets other exceptions fall to its
    harness-error path, whose rows are retried on the next attempt —
    the existing retry this class must keep.
    """


def _typed(path: str, sha: str, url: str, detail: str) -> SubmoduleUnavailableError:
    return SubmoduleUnavailableError(
        f"not supported: submodule {path} at {sha} unavailable from {url}: {detail}",
        detail=detail,
    )


# ---------------------------------------------------------------------------
# Transport: how one (url, sha) becomes a local bare repo holding that
# commit. Production is https-only; tests inject a local transport.
# ---------------------------------------------------------------------------


class SubmoduleTransport(Protocol):
    """Fetch ``sha`` from ``url`` into a NEW bare repo under
    ``scratch_dir`` and return ``(repo_dir, tree_sha)``.

    Implementations raise SubmoduleUnavailableError (with ``detail``
    set) for a refusal or an unserved commit, SubmoduleFetchError
    for a transient failure.
    """

    def fetch(self, url: str, sha: str, scratch_dir: Path) -> tuple[Path, str]: ...


# stderr fragments that mean "the remote does not serve this" (typed),
# as opposed to a transient network failure (retried). Matched
# case-insensitively against the whole stderr of the fetch.
_TYPED_FETCH_MARKERS = (
    "not allowed",  # transport refused by the protocol rules
    "couldn't find remote ref",  # the SHA is not an advertised ref
    "not our ref",
    "repository not found",
    "does not allow request for unadvertised object",
    "could not read username",  # GitHub: no such (or private) repo, no creds sent
    "authentication failed",
    "does not exist",  # git's own wording for a missing repository
)


def _git_fetch(
    repo: Path,
    url: str,
    sha: str,
    *,
    restrict_protocols: bool,
) -> tuple[Path, str]:
    """The one fetch implementation. ``restrict_protocols`` is True
    for production (https only, hermetic config, no credentials);
    the test-only local transport passes False to reach its local
    fixture repos — production code never does."""
    repo.mkdir(parents=True, exist_ok=True)
    init = subprocess.run(
        ["git", "init", "-q", "--bare", str(repo)],
        capture_output=True, text=True, timeout=60,
    )
    if init.returncode != 0:
        raise SubmoduleFetchError(
            f"git init --bare failed for submodule fetch of {url}: "
            f"{init.stderr.strip()}"
        )
    argv = ["git"]
    env = dict(os.environ)
    if restrict_protocols:
        argv += [
            "-c", "protocol.allow=never",
            "-c", "protocol.https.allow=always",
            "-c", "credential.helper=",
        ]
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GIT_CONFIG_NOSYSTEM"] = "1"
        env["GIT_CONFIG_GLOBAL"] = os.devnull
    # --end-of-options (R1): the URL and SHA are untrusted repo
    # input; nothing after this marker can be parsed as an option,
    # whatever the URL string looks like.
    argv += [
        "-C", str(repo), "fetch", "-q", "--depth", "1", "--no-tags",
        "--end-of-options", url, sha,
    ]
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True,
            timeout=FETCH_TIMEOUT_SECONDS, env=env,
        )
    except subprocess.TimeoutExpired:
        raise SubmoduleFetchError(
            f"submodule fetch of {url} at {sha} timed out after "
            f"{FETCH_TIMEOUT_SECONDS}s"
        ) from None
    if proc.returncode != 0:
        stderr = proc.stderr.strip()
        first_line = stderr.splitlines()[0] if stderr else "git fetch failed"
        if any(m in stderr.lower() for m in _TYPED_FETCH_MARKERS):
            raise SubmoduleUnavailableError(
                f"not supported: submodule at {sha} unavailable "
                f"from {url}: {first_line}",
                detail=first_line,
            )
        raise SubmoduleFetchError(
            f"submodule fetch of {url} at {sha} failed: {first_line}"
        )
    # The fetch addressed the commit by SHA; object addressing is the
    # guarantee, and this asserts the object actually arrived.
    verify = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", f"{sha}^{{commit}}"],
        capture_output=True, text=True, timeout=60,
    )
    if verify.returncode != 0 or verify.stdout.strip() != sha:
        raise SubmoduleUnavailableError(
            f"not supported: submodule at {sha} unavailable from "
            f"{url}: the remote did not serve the recorded commit",
            detail="the remote did not serve the recorded commit",
        )
    tree = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", f"{sha}^{{tree}}"],
        capture_output=True, text=True, timeout=60,
    )
    if tree.returncode != 0:
        raise SubmoduleFetchError(
            f"cannot read the tree of fetched submodule commit {sha} "
            f"from {url}: {tree.stderr.strip()}"
        )
    return repo, tree.stdout.strip()


_SHA40_RE = re.compile(r"[0-9a-f]{40}")


def _validate_fetch_target(url: str, sha: str) -> None:
    """R1: refuse a hostile URL or SHA before any git process runs.

    The URL and SHA come straight from the repo under test
    (``.gitmodules`` and the tree's gitlink), so they are untrusted
    input to git's argv: a URL such as ``--upload-pack=…`` or
    ``-c…`` would otherwise be parsed as an option. After
    resolution the URL must be a plain ``https://`` URL — that
    anchor alone excludes a leading ``-`` — with no whitespace or
    control characters, and the SHA must be exactly 40 lowercase
    hex. Refusals are typed (the D46 class: non-https by the
    protocol rules means the recorded commit is unavailable) and
    carry ``detail`` so the materialize layer can re-raise with
    the submodule's path, exactly as for a fetch refusal.
    """
    if not url.startswith("https://") or any(
        ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in url
    ):
        raise SubmoduleUnavailableError(
            f"not supported: submodule URL refused before fetch: {url!r}",
            detail=f"submodule URL is not a clean https URL: {url!r}",
        )
    if _SHA40_RE.fullmatch(sha) is None:
        raise SubmoduleUnavailableError(
            f"not supported: submodule commit refused before fetch: {sha!r}",
            detail=(
                "recorded commit is not a 40-character lowercase "
                f"hex SHA: {sha!r}"
            ),
        )


class HttpsSubmoduleTransport:
    """Production transport: https only, no credentials, hermetic
    git config (see the module docstring). The fetch target is
    validated (R1) before any git process runs."""

    def fetch(self, url: str, sha: str, scratch_dir: Path) -> tuple[Path, str]:
        _validate_fetch_target(url, sha)
        return _git_fetch(scratch_dir / "repo", url, sha, restrict_protocols=True)


# ---------------------------------------------------------------------------
# Tree reading: gitlinks and .gitmodules, always from the side's own
# tree (the commit in its repo, the snapshot on disk), never from a
# working directory.
# ---------------------------------------------------------------------------


def _ls_tree_gitlinks(repo_dir: Path, tree_ish: str) -> dict[str, str]:
    """{path: commit} for every gitlink (mode 160000) in the tree,
    paths relative to the tree's root."""
    proc = subprocess.run(
        ["git", "-C", str(repo_dir), "ls-tree", "-r", "-z", tree_ish],
        capture_output=True, text=True, timeout=120,
    )
    if proc.returncode != 0:
        raise SubmoduleFetchError(
            f"git ls-tree {tree_ish} failed in {repo_dir}: "
            f"{proc.stderr.strip()}"
        )
    gitlinks: dict[str, str] = {}
    for entry in proc.stdout.split("\0"):
        if not entry:
            continue
        meta, _, path = entry.partition("\t")
        parts = meta.split(" ")
        if len(parts) == 3 and parts[0] == "160000":
            gitlinks[path] = parts[2]
    return gitlinks


def _read_gitmodules(tree_root: Path) -> list[dict[str, str]]:
    """The .gitmodules entries of one tree level, read from the
    materialized tree itself: [{name, path, url}] with url possibly
    empty. Parsed by git's own config reader (argv only)."""
    gitmodules = tree_root / ".gitmodules"
    if not gitmodules.is_file():
        return []
    proc = subprocess.run(
        [
            "git", "config", "--file", str(gitmodules), "--null",
            "--get-regexp", r"^submodule\..*\.(path|url)$",
        ],
        capture_output=True, text=True, timeout=60,
    )
    if proc.returncode not in (0, 1):  # 1 = no matches
        raise SubmoduleFetchError(
            f"cannot parse {gitmodules}: {proc.stderr.strip()}"
        )
    fields: dict[str, dict[str, str]] = {}
    for chunk in proc.stdout.split("\0"):
        if not chunk:
            continue
        key, _, value = chunk.partition("\n")
        head, _, field_name = key.rpartition(".")
        if not head.startswith("submodule."):
            continue
        name = head[len("submodule."):]
        fields.setdefault(name, {})[field_name] = value
    return [
        {"name": name, "path": f.get("path", ""), "url": f.get("url", "")}
        for name, f in sorted(fields.items())
    ]


def _resolve_url(raw_url: str, base_url: str | None, path: str) -> str:
    """Resolve a .gitmodules URL. Relative URLs (``./`` / ``../``)
    resolve against the superproject's own origin URL, exactly as
    git resolves them (Ghost's ``../../TryGhost/Source.git`` against
    ``https://github.com/TryGhost/Ghost.git``).

    A missing origin, or one that is not an https URL (scp-style
    ``git@host:path``, a local path), makes resolution impossible
    for a reason that lives in THIS checkout's configuration, not
    in the repo: harness or environment (the reviewer's R4 ruling),
    raised as SubmoduleFetchError — the harness retry class."""
    if raw_url.startswith(("./", "../")):
        if base_url is None or "://" not in base_url:
            raise SubmoduleFetchError(
                f"harness or environment: relative submodule URL "
                f"{raw_url!r} for submodule {path} cannot be resolved: "
                f"the superproject origin is missing or not an https "
                f"URL ({base_url!r}); the origin is this checkout's "
                f"configuration, not the repo's"
            )
        return urllib.parse.urljoin(base_url, raw_url)
    return raw_url


def _check_path_syntax(full_rel: str, raw_url: str) -> None:
    """Refuse an absolute or ``..``-carrying .gitmodules path,
    whether or not it pairs with a gitlink (see the caller). The
    refusal is a plain ValueError — the class create_snapshot
    gives an ADR 0012 refusal (fixture.py:359) — with NO
    ``not supported:`` prefix (the reviewer's R4 ruling)."""
    if posixpath.isabs(full_rel) or any(
        part == ".." for part in full_rel.split("/")
    ):
        raise ValueError(
            f"submodule path does not normalize inside the checkout: "
            f"{full_rel!r}"
        )


def _validate_path(snapshot_dir: Path, full_rel: str) -> str:
    """Validate one submodule path (posix, relative to the snapshot
    root) against ADR 0012 and return its normalized form: ``..``
    components or an absolute path, and any symlinked component on
    the way down (a link could carry the extraction outside the
    checkout). Refusals are plain ValueErrors — the class
    create_snapshot gives an ADR 0012 refusal (fixture.py:359) —
    with NO ``not supported:`` prefix (the reviewer's R4 ruling)."""
    if posixpath.isabs(full_rel) or any(
        part == ".." for part in full_rel.split("/")
    ):
        raise ValueError(
            f"submodule path does not normalize inside the checkout: "
            f"{full_rel!r}"
        )
    normalized = posixpath.normpath(full_rel)
    if normalized in ("", "."):
        raise ValueError(
            f"submodule path does not name a directory: {full_rel!r}"
        )
    parts = normalized.split("/")
    cursor = snapshot_dir
    walked: list[str] = []
    for part in parts[:-1]:
        cursor = cursor / part
        walked.append(part)
        if cursor.is_symlink():
            raise ValueError(
                f"submodule path has a symlinked parent: {'/'.join(walked)}"
            )
        if cursor.exists() and not cursor.is_dir():
            raise ValueError(
                f"submodule path's parent is not a directory: "
                f"{'/'.join(walked)}"
            )
    dest = snapshot_dir / normalized
    if dest.is_symlink():
        raise ValueError(
            f"submodule path is a symlink in the snapshot: {full_rel!r}"
        )
    if dest.exists() and not dest.is_dir():
        raise ValueError(
            "submodule path exists in the snapshot and is not a "
            f"directory: {full_rel!r}"
        )
    return normalized


def _extract_tree(
    repo: Path, sha: str, dest: Path, *, full_path: str, url: str
) -> None:
    """Extract the fetched commit's FULL tree into ``dest`` by
    plumbing: every blob byte-exact, executable bits preserved,
    gitlinks created as empty directories (the recursion fills the
    listed ones), symlinks deferred, validated against the WO-8.4b
    rule as one map, then created verbatim. Nothing is executed and
    no attribute filter is applied — the tree's bytes are the
    commit's bytes.

    The size cap is enforced BEFORE anything is written (R3): the
    blob sizes come from the ``ls-tree -l`` listing itself and are
    summed first; an over-cap tree is refused with nothing on
    disk. Blobs are then streamed to disk from ONE
    ``cat-file --batch`` process — each blob's bytes go from the
    pipe to the file in chunks, never held whole in memory.
    """
    proc = subprocess.run(
        ["git", "-C", str(repo), "ls-tree", "-r", "-l", "-z", sha],
        capture_output=True, timeout=120,
    )
    if proc.returncode != 0:
        raise SubmoduleFetchError(
            f"git ls-tree {sha} failed in {repo}: "
            f"{proc.stderr.decode(errors='replace').strip()}"
        )
    # (mode, oid, size, rel) per entry; gitlinks carry no blob.
    entries: list[tuple[str, str, int, str]] = []
    total = 0
    for entry in proc.stdout.split(b"\0"):
        if not entry:
            continue
        meta, _, raw_path = entry.partition(b"\t")
        parts = meta.split()
        if len(parts) != 4:
            raise SubmoduleFetchError(
                f"unparseable ls-tree entry for {sha}: {entry!r}"
            )
        mode = parts[0].decode()
        oid = parts[2].decode()
        rel = raw_path.decode("utf-8", errors="surrogateescape")
        # Tree paths are normalized by construction (git forbids
        # `..` components); refuse defensively rather than ever
        # writing outside dest. Plain ValueError: the ADR 0012
        # refusal class of create_snapshot (fixture.py:359).
        if posixpath.isabs(rel) or any(p == ".." for p in rel.split("/")):
            raise ValueError(
                f"submodule tree entry escapes the submodule "
                f"directory: {rel!r}"
            )
        if mode == "160000":
            entries.append((mode, oid, 0, rel))
            continue
        try:
            size = int(parts[3])
        except ValueError:
            raise SubmoduleFetchError(
                f"unparseable ls-tree size for {sha}: {entry!r}"
            ) from None
        total += size
        entries.append((mode, oid, size, rel))
    if total > MAX_TREE_BYTES:
        # The cap is OUR limit, so this is our gap (D48 R1/R3):
        # a plain ValueError whose message says so — it must NOT
        # start with "not supported:".
        raise ValueError(
            f"submodule {full_path}: its tree is {total} bytes, over "
            f"the {MAX_TREE_BYTES}-byte limit ProofDeploy sets for "
            f"one submodule (our gap)"
        )
    dest.mkdir(parents=True, exist_ok=True)
    links: dict[str, str] = {}
    batch = subprocess.Popen(
        ["git", "-C", str(repo), "cat-file", "--batch"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
    )
    done = False
    try:
        assert batch.stdin is not None and batch.stdout is not None
        for mode, oid, size, rel in entries:
            target = dest / rel
            if mode == "160000":
                target.mkdir(parents=True, exist_ok=True)
                continue
            # Request-response, one blob at a time: write the oid,
            # then drain exactly its response before asking for
            # the next, so neither pipe can fill and stall.
            batch.stdin.write(f"{oid}\n".encode())
            batch.stdin.flush()
            header = batch.stdout.readline().decode(errors="replace").split()
            if len(header) != 3 or header[1] != "blob" or int(header[2]) != size:
                raise SubmoduleFetchError(
                    f"git cat-file --batch cannot serve {oid} in "
                    f"{repo}: {' '.join(header)}"
                )
            if mode == "120000":
                # A link target is a path string by construction;
                # it is the one blob read whole.
                data = batch.stdout.read(size)
                batch.stdout.read(1)  # the response's trailing LF
                links[rel] = data.decode("utf-8", errors="surrogateescape")
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            remaining = size
            with target.open("wb") as fh:
                while remaining > 0:
                    chunk = batch.stdout.read(min(65536, remaining))
                    if not chunk:
                        raise SubmoduleFetchError(
                            f"git cat-file --batch stream ended "
                            f"early for {oid} in {repo}"
                        )
                    fh.write(chunk)
                    remaining -= len(chunk)
            batch.stdout.read(1)  # the response's trailing LF
            if mode == "100755":
                target.chmod(0o755)
        done = True
    finally:
        if batch.poll() is None:
            if done:
                assert batch.stdin is not None
                batch.stdin.close()
                batch.wait(timeout=60)
            else:
                batch.kill()
                batch.wait(timeout=60)
    if batch.returncode != 0:
        raise SubmoduleFetchError(
            f"git cat-file --batch exited {batch.returncode} in {repo}"
        )
    # All-or-nothing, exactly as in create_snapshot: validate the
    # whole symlink map before creating any link. A refusal is the
    # SnapshotLinkError itself — the class create_snapshot gives
    # an ADR 0012 refusal (fixture.py:157) — re-based to name the
    # path from the snapshot root.
    for rel in sorted(links):
        try:
            _validate_snapshot_link(links, rel)
        except SnapshotLinkError as exc:
            raise SnapshotLinkError(
                f"{full_path}/{exc.path}", exc.target, exc.reason
            ) from None
    for rel in sorted(links):
        link_dest = dest / rel
        link_dest.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(links[rel], link_dest)


# ---------------------------------------------------------------------------
# Materialization.
# ---------------------------------------------------------------------------


def materialize_snapshot_submodules(
    *,
    repo_dir: str | Path,
    sha: str,
    snapshot_dir: str | Path,
    scratch_dir: str | Path,
    transport: SubmoduleTransport | None = None,
    base_url: str | None = None,
) -> list[dict[str, Any]]:
    """Fill every listed gitlink of the side's tree with its
    recorded commit's content, in place under ``snapshot_dir``.

    ``repo_dir``/``sha`` name the side's own commit (its gitlinks
    come from that commit's tree; each level's ``.gitmodules`` is
    read from the materialized tree — never from a working
    directory). ``scratch_dir`` holds the fetch repos and must live
    OUTSIDE the snapshot (D36). ``base_url`` is the superproject's
    origin URL for resolving relative submodule URLs; by default
    the repo's own ``origin`` remote, read lazily and only when a
    relative URL appears.

    Returns the records for the readiness row / evidence record:
    ``[{path, url, commit, tree_sha}]`` sorted by path (nested
    submodules included, paths relative to the snapshot root); an
    empty list when the tree lists no gitlinks.
    """
    repo = Path(repo_dir)
    snap = Path(snapshot_dir)
    scratch = Path(scratch_dir)
    active_transport: SubmoduleTransport = transport or HttpsSubmoduleTransport()
    records: list[dict[str, Any]] = []
    counter = [0]
    origin_cache: list[str | None] = []

    def origin_url() -> str | None:
        if origin_cache:
            return origin_cache[0]
        if base_url is not None:
            origin_cache.append(base_url)
            return base_url
        proc = subprocess.run(
            ["git", "-C", str(repo), "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=60,
        )
        url = proc.stdout.strip() if proc.returncode == 0 else ""
        origin_cache.append(url or None)
        return origin_cache[0]

    def level(
        *,
        tree_repo: Path,
        tree_ish: str,
        tree_root: Path,
        prefix: str,
        level_base_url: str | None,
        depth: int,
        via: tuple[str, str, str] | None,
    ) -> None:
        all_entries = _read_gitmodules(tree_root)
        if not all_entries:
            # Nothing is listed at this level, so nothing is in
            # scope — gitlinks not listed in .gitmodules are never
            # fetched (see the module docstring).
            return
        # A hostile path in .gitmodules is refused even when it pairs
        # with no gitlink: extraction destinations come only from
        # gitlink paths (tree paths are normalized by construction),
        # so a `..` or absolute entry path can never be a destination
        # — but it is never silently passed over either.
        for e in all_entries:
            _check_path_syntax(f"{prefix}{e['path']}", e["url"])
        gitlinks = _ls_tree_gitlinks(tree_repo, tree_ish)
        if not gitlinks:
            return
        entries = [e for e in all_entries if e["path"] in gitlinks]
        if not entries:
            return
        if depth >= MAX_DEPTH:
            # This level's tree is a submodule nested MAX_DEPTH
            # deep; ITS listed gitlinks would nest one level more.
            assert via is not None  # only a parent submodule recurses
            raise _typed(
                via[0], via[1], via[2],
                f"nested submodules exceed the depth limit of {MAX_DEPTH}",
            )
        for entry in entries:
            rel_path = entry["path"]
            commit = gitlinks[rel_path]
            full_path = f"{prefix}{rel_path}"
            raw_url = entry["url"]
            if not raw_url:
                raise _typed(
                    full_path, commit, "(no url in .gitmodules)",
                    "submodule has no url in .gitmodules",
                )
            if raw_url.startswith(("./", "../")):
                base = level_base_url if level_base_url is not None else origin_url()
                url = _resolve_url(raw_url, base, full_path)
            else:
                url = raw_url
            normalized = _validate_path(snap, full_path)
            dest = snap / normalized
            counter[0] += 1
            fetch_dir = scratch / f"fetch-{counter[0]}"
            try:
                bare_repo, tree_sha = active_transport.fetch(url, commit, fetch_dir)
            except SubmoduleUnavailableError as exc:
                if exc.detail is not None:
                    raise _typed(full_path, commit, url, exc.detail) from None
                raise
            # _extract_tree enforces MAX_TREE_BYTES from the tree
            # listing before writing anything (R3).
            _extract_tree(bare_repo, commit, dest, full_path=full_path, url=url)
            records.append(
                {
                    "path": full_path,
                    "url": url,
                    "commit": commit,
                    "tree_sha": tree_sha,
                }
            )
            # Recurse: the submodule's own listed gitlinks, resolved
            # against THIS submodule's URL, one depth level down.
            level(
                tree_repo=bare_repo,
                tree_ish=commit,
                tree_root=dest,
                prefix=f"{full_path}/",
                level_base_url=url,
                depth=depth + 1,
                via=(full_path, commit, url),
            )

    level(
        tree_repo=repo,
        tree_ish=sha,
        tree_root=snap,
        prefix="",
        level_base_url=base_url,
        depth=0,
        via=None,
    )
    records.sort(key=lambda r: str(r["path"]))
    return records
