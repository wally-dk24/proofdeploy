#!/usr/bin/env python3
"""REG-C Addendum 2 (docs/registration/wo6-selection-rule-addendum-2.md)
candidate selection, implementing REG-C (wo6-selection-rule.md) as amended:

- Item 1 as amended by A1: GitHub repository search API, queries for
  language:JavaScript, language:TypeScript, language:Python;
  stars:>=1000 archived:false fork:false pushed:>=(snapshot date - 90
  days); sorted by stars descending; up to the first 1000 results per
  language query (10 pages x 100, the API maximum); de-duplicated; the
  count returned per query is recorded.
- Item 2, unchanged: exclusions (named repos + bare-name owner/name
  matching, case-insensitive).
- Item 3 as amended by A2 (Node) and A3 (Python): mechanical filters at
  the default branch's HEAD SHA (file checks only; no issues, PRs,
  commits or diffs are ever fetched). Per the reviewer's F1 ruling on
  PR #108, A2's workspace packages are the members declared by the
  repo's own workspace configuration (root package.json `workspaces`,
  pnpm-workspace.yaml `packages`, lerna.json `packages`), never every
  nested package.json. Per F2, a truncated recursive tree is decided
  from non-recursive per-directory listings, and an undecidable repo
  is a harness failure, never an exclusion.
- Item 4, unchanged: survivors sorted by stars descending, ties by
  owner/name.

Running this script against the live API IS the A5 single run; it is
reviewed before that happens. The token, if any, comes from the
GITHUB_TOKEN environment variable (read-only, per REG-C item 1) and is
never written to any output; the record carries only
`authenticated: true/false`. Rate limits are handled by waiting and
retrying; a repo whose checks still fail after retries is recorded as
"harness or environment: <error>", never as excluded.

D74 (2026-10-16) changes only the fetch layer and run mechanics; the
selection rules A1-A6 above are unchanged. Every request now carries
a timeout, and `http.client.HTTPException` (including `IncompleteRead`,
a mid-body connection break) is retried exactly like `OSError`; an
exception left after retries for one repo becomes that repo's
"harness or environment: <error>" row, never a dead run. A run with
`--journal <dir>` journals as it goes: the pool and search metadata
are written before any screening, one line is appended per screened
repo, and the final outputs are built only from the journal. A death
resumes the same journal; the journal header carries this script's
sha256, the main SHA and the snapshot date, and a resume with any of
them different refuses. One journal is one run (A5).

D79 (2026-10-16) adds one rule on top: an errored repo is retried,
never silently dropped. After the full pass, every entry with an
error is re-screened in up to two further passes (three attempts per
repo at most). Each attempt is journaled as its own line carrying an
`attempt` number; the latest line per repo wins when the outputs are
built, and a repo still errored after its passes stays in
`errored_repos` with every attempt's message. The run prints ERRORED
next to SURVIVORS; while it is non-zero the survivor list is not
final and Phase C does not start.
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import http.client
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

API = "https://api.github.com"
USER_AGENT = "proofdeploy-regc-add2-runner"
LANGUAGES = ("JavaScript", "TypeScript", "Python")
PER_PAGE = 100
MAX_PAGES = 10  # A1: 10 pages x 100 = the first 1000 results per query.
REQUEST_TIMEOUT = 120.0  # D74: every request gets a timeout (seconds).
MAX_SCREEN_ATTEMPTS = 3  # D79: first pass + up to 2 retry passes per repo.

NODE_LOCKFILES = ("package-lock.json", "pnpm-lock.yaml", "yarn.lock", "bun.lock")
NODE_MONOREPO_FILES = ("pnpm-workspace.yaml", "lerna.json", "nx.json", "turbo.json")
PY_LOCKFILES = ("uv.lock", "poetry.lock", "pdm.lock", "requirements.txt", "pyproject.toml")
SQLITE_DEPS = ("sqlite3", "better-sqlite3", "@libsql/client")
SQLITE_BACKEND = b"django.db.backends.sqlite3"
TARBALL_MEMBER_CAP = 4_000_000  # skip individual members larger than this in the string scan

# REG-C item 2, verbatim from the registered rule (and scripts/wo6/regc_run.py).
NAMED_EXCLUSIONS = {
    "vendure-ecommerce/vendure", "directus/directus", "tryghost/ghost",
    "nestjs/nest", "koajs/koa", "adonisjs/core", "pallets/flask",
    "fastapi/fastapi", "codeigniter4/codeigniter4", "fastify/fastify",
    "django/django", "lmcache/lmcache", "bottlepy/bottle", "honojs/hono",
}
BARE_NAME_EXCLUSIONS = {
    "ghost", "directus", "vendure", "nestjs", "koa", "adonisjs", "flask",
    "fastapi", "codeigniter4", "fastify", "django", "lmcache", "bottle",
    "hono", "body-parser", "express", "n8n", "strapi", "payload",
    "django-oscar", "keystone", "uptime-kuma", "pretix", "nocodb",
    "datasette", "apostrophe", "wagtail",
}


class HarnessError(Exception):
    """A check could not be completed (network / rate limit / API error).

    Recorded on the repo as 'harness or environment: <error>'; the repo
    is never excluded on the basis of a harness failure.
    """


class NotFound(HarnessError):
    """The API answered 404: the path does not exist at that SHA (F3)."""


def pushed_since(snapshot_date: str) -> str:
    """REG-C item 1: pushed:>=(snapshot date minus 90 days)."""
    d = date.fromisoformat(snapshot_date)
    return (d - timedelta(days=90)).isoformat()


def search_query(language: str, snapshot_date: str) -> str:
    return (
        f"language:{language} stars:>=1000 archived:false fork:false "
        f"pushed:>={pushed_since(snapshot_date)}"
    )


def exclusion_reason(full_name: str) -> str | None:
    """REG-C item 2: named repos, then the bare-name rule (ci)."""
    low = full_name.lower()
    if low in NAMED_EXCLUSIONS:
        return "named-exclusion"
    owner, _, name = low.partition("/")
    if owner in BARE_NAME_EXCLUSIONS or name in BARE_NAME_EXCLUSIONS:
        return "bare-name-exclusion"
    return None


class Fetcher:
    """GitHub API GETs with rate-limit waiting and retries.

    raw_fetch(url, headers) -> (status, headers, body) is injectable for
    offline tests; the default implementation uses urllib with a
    per-request timeout (D74). A retryable failure (403/429 rate
    limiting, 5xx, network error, or an http.client.HTTPException such
    as IncompleteRead) is retried after the server-provided wait
    (Retry-After, else X-RateLimit-Reset, else exponential backoff);
    after max_retries the call raises HarnessError. A 404 raises
    NotFound immediately (F3: typed, never substring-matched).
    """

    def __init__(
        self,
        token: str | None = None,
        sleep: Callable[[float], None] = time.sleep,
        max_retries: int = 5,
        raw_fetch: Callable[[str, dict[str, str]], tuple[int, dict[str, str], bytes]] | None = None,
    ) -> None:
        self._token = token
        self._sleep = sleep
        self._max_retries = max_retries
        self._raw_fetch = raw_fetch or self._urllib_fetch

    @property
    def authenticated(self) -> bool:
        """True when a token is in use (the token itself is never recorded)."""
        return self._token is not None

    def _urllib_fetch(
        self, url: str, headers: dict[str, str]
    ) -> tuple[int, dict[str, str], bytes]:
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as r:
                return r.status, dict(r.headers.items()), r.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers.items()) if e.headers else {}, e.read() or b""

    def _wait_seconds(self, headers: dict[str, str], attempt: int) -> float:
        retry_after = headers.get("Retry-After") or headers.get("retry-after")
        if retry_after:
            try:
                return float(retry_after)
            except ValueError:
                pass
        reset = headers.get("X-RateLimit-Reset") or headers.get("x-ratelimit-reset")
        if reset:
            try:
                return max(0.0, float(reset) - time.time()) + 1.0
            except ValueError:
                pass
        return float(min(60 * (2**attempt), 900))

    def get_bytes(self, url: str) -> bytes:
        headers = {"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        last = "no attempt made"
        for attempt in range(self._max_retries + 1):
            try:
                status, resp_headers, body = self._raw_fetch(url, headers)
            except OSError as e:
                status, resp_headers, body = 0, {}, b""
                last = f"network error: {e}"
            except http.client.HTTPException as e:
                # D74: a mid-body break (IncompleteRead) or any other
                # http.client failure is retried exactly like OSError.
                status, resp_headers, body = 0, {}, b""
                last = f"http error: {e}"
            if status == 200:
                return body
            if status == 404:
                raise NotFound(f"GET {url} -> 404")
            retryable = status in (0, 403, 429) or 500 <= status < 600
            last = f"GET {url} -> HTTP {status}"
            if not retryable:
                raise HarnessError(last)
            if attempt < self._max_retries:
                self._sleep(self._wait_seconds(resp_headers, attempt))
        raise HarnessError(f"{last} (after {self._max_retries} retries)")

    def get_json(self, url: str) -> Any:
        return json.loads(self.get_bytes(url).decode("utf-8"))

    def get_text(self, url: str) -> str:
        return self.get_bytes(url).decode("utf-8", errors="replace")


def collect_pool(
    fetcher: Fetcher, snapshot_date: str, sleep: Callable[[float], None] = time.sleep
) -> tuple[dict[str, dict[str, Any]], dict[str, int], dict[str, Any]]:
    """REG-C item 1 as amended by A1.

    Returns (pool, per-query counts, per-language search metadata).
    The metadata records every page's returned count, total_count and
    incomplete_results (F4).
    """
    pool: dict[str, dict[str, Any]] = {}
    counts: dict[str, int] = {}
    search_meta: dict[str, Any] = {}
    for language in LANGUAGES:
        q = urllib.parse.quote(search_query(language, snapshot_date))
        returned = 0
        pages_meta: list[dict[str, Any]] = []
        lang_incomplete = False
        for page in range(1, MAX_PAGES + 1):
            url = (
                f"{API}/search/repositories?q={q}"
                f"&sort=stars&order=desc&per_page={PER_PAGE}&page={page}"
            )
            data = fetcher.get_json(url)
            items = data.get("items", [])
            incomplete = bool(data.get("incomplete_results", False))
            lang_incomplete = lang_incomplete or incomplete
            pages_meta.append({
                "page": page,
                "returned": len(items),
                "total_count": data.get("total_count"),
                "incomplete_results": incomplete,
            })
            returned += len(items)
            for it in items:
                fn = it["full_name"]
                if fn not in pool:
                    pool[fn] = {
                        "full_name": fn,
                        "stars": it["stargazers_count"],
                        "default_branch": it["default_branch"],
                        "queries": [language],
                    }
                elif language not in pool[fn]["queries"]:
                    pool[fn]["queries"].append(language)
            if len(items) < PER_PAGE:
                break
            sleep(2)
        counts[language] = returned
        search_meta[language] = {
            "pages": pages_meta,
            "incomplete_results": lang_incomplete,
        }
    return pool, counts, search_meta


def head_sha(fetcher: Fetcher, full_name: str, default_branch: str) -> str:
    """The default branch's HEAD SHA, via the branch endpoint (metadata
    only; no commits are listed or fetched)."""
    branch = urllib.parse.quote(default_branch, safe="")
    data = fetcher.get_json(f"{API}/repos/{full_name}/branches/{branch}")
    return str(data["commit"]["sha"])


def repo_paths(fetcher: Fetcher, full_name: str, sha: str) -> tuple[set[str], bool]:
    """All blob paths at the given SHA, plus the tree's truncated flag."""
    data = fetcher.get_json(f"{API}/repos/{full_name}/git/trees/{sha}?recursive=1")
    paths = {t["path"] for t in data.get("tree", []) if t.get("type") == "blob"}
    return paths, bool(data.get("truncated", False))


class TreeIndex:
    """Path oracle for the item-3 filters, over one repo at one SHA.

    Complete mode answers from the recursive tree's blob set. When the
    recursive tree is truncated (F2), membership answers stay sound:
    a path present in the truncated set exists, and anything else is
    decided from a non-recursive contents-API listing of its directory
    (root, depth-one dirs, matched workspace dirs — fetched on demand
    and cached). A listing that cannot be fetched at all raises
    HarnessError("tree truncated"): the repo is recorded as a harness
    failure, never excluded on missing data.
    """

    def __init__(
        self,
        fetcher: Fetcher,
        full_name: str,
        sha: str,
        paths: set[str],
        truncated: bool,
    ) -> None:
        self._fetcher = fetcher
        self._full_name = full_name
        self._sha = sha
        self._paths = paths
        self._truncated = truncated
        self._dirs: set[str] | None = None
        self._listings: dict[str, tuple[frozenset[str], frozenset[str]]] = {}

    def _all_dirs(self) -> set[str]:
        if self._dirs is None:
            dirs: set[str] = set()
            for p in self._paths:
                parts = p.split("/")[:-1]
                for i in range(1, len(parts) + 1):
                    dirs.add("/".join(parts[:i]))
            self._dirs = dirs
        return self._dirs

    def _listing(self, dir_path: str) -> tuple[frozenset[str], frozenset[str]]:
        """(file paths, subdir paths) directly inside dir_path ('' = root)."""
        cached = self._listings.get(dir_path)
        if cached is not None:
            return cached
        quoted = urllib.parse.quote(dir_path, safe="/")
        url = f"{API}/repos/{self._full_name}/contents"
        if dir_path:
            url += f"/{quoted}"
        url += f"?ref={self._sha}"
        try:
            data = self._fetcher.get_json(url)
        except NotFound:
            result: tuple[frozenset[str], frozenset[str]] = (frozenset(), frozenset())
        except HarnessError as e:
            raise HarnessError("tree truncated") from e
        else:
            if isinstance(data, list):
                files = frozenset(
                    str(e["path"]) for e in data
                    if isinstance(e, dict) and e.get("type") in ("file", "symlink")
                )
                dirs = frozenset(
                    str(e["path"]) for e in data
                    if isinstance(e, dict) and e.get("type") == "dir"
                )
                result = (files, dirs)
            else:  # the path is a file, not a directory: no children
                result = (frozenset(), frozenset())
        self._listings[dir_path] = result
        return result

    def root_files(self) -> set[str]:
        if not self._truncated:
            return {p for p in self._paths if "/" not in p}
        files, _ = self._listing("")
        return set(files)

    def has_file(self, path: str) -> bool:
        if path in self._paths:
            return True
        if not self._truncated:
            return False
        parent = path.rsplit("/", 1)[0] if "/" in path else ""
        files, _ = self._listing(parent)
        return path in files

    def child_dirs(self, dir_path: str) -> set[str]:
        if self._truncated:
            _, dirs = self._listing(dir_path)
            return set(dirs)
        return {
            d for d in self._all_dirs()
            if (d.rsplit("/", 1)[0] if "/" in d else "") == dir_path
        }

    def match_dirs(self, patterns: list[str]) -> list[str]:
        """Directories matched by workspace patterns (F1).

        Positive patterns union; `!` patterns subtract. `*` matches
        one path segment, `**` any number (including zero). Expansion
        never descends into or matches a `node_modules` directory
        (G1), mirroring npm/pnpm workspace-glob resolution.
        """
        matched: set[str] = set()
        for pat in patterns:
            if not pat.startswith("!"):
                matched |= self._expand_pattern(pat)
        for pat in patterns:
            if pat.startswith("!"):
                matched -= self._expand_pattern(pat[1:])
        return sorted(matched)

    def _glob_children(self, dir_path: str) -> set[str]:
        """Children visible to workspace-glob expansion (G1).

        npm and pnpm ignore `node_modules` when resolving workspace
        globs, so expansion never descends into or matches a
        `node_modules` directory, at any depth, in `*` or `**`.
        """
        return {
            d for d in self.child_dirs(dir_path)
            if d.rsplit("/", 1)[-1] != "node_modules"
        }

    def _expand_pattern(self, pattern: str) -> set[str]:
        pat = pattern.strip()
        if pat.startswith("./"):
            pat = pat[2:]
        segs = [s for s in pat.strip("/").split("/") if s not in ("", ".")]
        current = {""}
        for seg in segs:
            nxt: set[str] = set()
            if seg == "**":
                seen = set(current)
                frontier = list(current)
                while frontier:
                    d = frontier.pop()
                    for child in self._glob_children(d):
                        if child not in seen:
                            seen.add(child)
                            frontier.append(child)
                nxt = seen
            else:
                for d in current:
                    for child in self._glob_children(d):
                        if fnmatch.fnmatchcase(child.rsplit("/", 1)[-1], seg):
                            nxt.add(child)
            current = nxt
            if not current:
                break
        return current


def fetch_file_text(fetcher: Fetcher, full_name: str, path: str, sha: str) -> str | None:
    """A file's raw content at the SHA, or None when it does not exist.

    "Does not exist" is decided by the typed NotFound (F3), never by
    matching "404" in an error message.
    """
    url = (
        f"{API}/repos/{full_name}/contents/{urllib.parse.quote(path, safe='/')}"
        f"?ref={sha}"
    )
    try:
        return fetcher.get_text(url)
    except NotFound:
        return None


def fetch_package_json(
    fetcher: Fetcher, full_name: str, path: str, sha: str
) -> tuple[dict[str, Any] | None, bool]:
    """(parsed package.json or None, unparseable flag) (F5).

    (None, False) = the file does not exist at the SHA. (None, True) =
    it exists but is not a parseable JSON object; callers give it its
    own typed reason instead of treating it as absent.
    """
    text = fetch_file_text(fetcher, full_name, path, sha)
    if text is None:
        return None, False
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None, True
    if not isinstance(parsed, dict):
        return None, True
    return parsed, False


def repo_contains_string(
    fetcher: Fetcher, full_name: str, sha: str, needle: bytes
) -> tuple[bool, int]:
    """(found, members skipped over the size cap) for a tarball scan.

    One tarball fetch for the exact SHA, scanned member by member (the
    mechanism scripts/wo6/regc_stage2.py used for the same check).
    Members larger than TARBALL_MEMBER_CAP are skipped; the skip count
    is returned so a not-found verdict can say so (F5).
    """
    blob = fetcher.get_bytes(f"{API}/repos/{full_name}/tarball/{sha}")
    skipped = 0
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tf:
        for member in tf.getmembers():
            if not member.isfile():
                continue
            if member.size > TARBALL_MEMBER_CAP:
                skipped += 1
                continue
            f = tf.extractfile(member)
            if f is not None and needle in f.read():
                return True, skipped
    return False, skipped


def _dependencies(pkg: dict[str, Any] | None) -> set[str]:
    if not pkg:
        return set()
    deps = pkg.get("dependencies")
    return set(deps.keys()) if isinstance(deps, dict) else set()


def _has_start(pkg: dict[str, Any] | None) -> bool:
    if not pkg:
        return False
    scripts = pkg.get("scripts")
    return isinstance(scripts, dict) and "start" in scripts


def _parse_pnpm_workspace(text: str) -> list[str]:
    """The `packages:` list of a pnpm-workspace.yaml (F1).

    Minimal line parser (the script stays stdlib-only); handles the
    block form (one `- pattern` per line) and the flow form
    (`packages: ['apps/*']`).
    """
    lines = text.splitlines()
    for i, line in enumerate(lines):
        m = re.match(r"^packages\s*:\s*(.*)$", line)
        if not m:
            continue
        rest = m.group(1).strip()
        if rest.startswith("["):
            inner = rest.strip().strip("[]")
            return [p.strip().strip("'\"") for p in inner.split(",") if p.strip()]
        if rest:
            return [rest.strip().strip("'\"")]
        patterns: list[str] = []
        for follow in lines[i + 1:]:
            item = re.match(r"^\s*-\s+(.+?)\s*$", follow)
            if not item:
                if follow.strip():
                    break
                continue
            val = item.group(1)
            if val[:1] in ("'", '"'):
                end = val.find(val[0], 1)
                val = val[1:end] if end > 0 else val.strip("'\"")
            else:
                val = val.split(" #", 1)[0].strip()
            if val:
                patterns.append(val)
        return patterns
    return []


def _declared_workspace_patterns(
    fetcher: Fetcher,
    full_name: str,
    sha: str,
    root_pkg: dict[str, Any] | None,
    root_files: set[str],
) -> list[str]:
    """The repo's declared workspace patterns (F1 ruling), unioned from
    root package.json `workspaces` (array, or the object form's
    `packages`), pnpm-workspace.yaml `packages`, and lerna.json
    `packages` (lerna's default `packages/*` when the file declares
    none). nx.json / turbo.json declare no members and contribute
    nothing."""
    patterns: list[str] = []
    if root_pkg is not None:
        ws = root_pkg.get("workspaces")
        if isinstance(ws, list):
            patterns.extend(p for p in ws if isinstance(p, str))
        elif isinstance(ws, dict):
            pkgs = ws.get("packages")
            if isinstance(pkgs, list):
                patterns.extend(p for p in pkgs if isinstance(p, str))
    if "pnpm-workspace.yaml" in root_files:
        text = fetch_file_text(fetcher, full_name, "pnpm-workspace.yaml", sha)
        if text is not None:
            patterns.extend(_parse_pnpm_workspace(text))
    if "lerna.json" in root_files:
        declared: list[str] = []
        text = fetch_file_text(fetcher, full_name, "lerna.json", sha)
        if text is not None:
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                data = None
            if isinstance(data, dict) and isinstance(data.get("packages"), list):
                declared = [p for p in data["packages"] if isinstance(p, str)]
        patterns.extend(declared if declared else ["packages/*"])
    return patterns


def node_filter(
    fetcher: Fetcher, full_name: str, sha: str, tree: TreeIndex
) -> tuple[bool, str | None, list[str], dict[str, Any]]:
    """A2. Returns (passed, app.path, typed drop reasons, extra record).

    The extra record carries the declared workspace patterns and the
    matched members in workspace mode (F1).
    """
    reasons: list[str] = []
    info: dict[str, Any] = {}
    root = tree.root_files()
    root_pkg: dict[str, Any] | None = None
    root_unparseable = False
    if "package.json" in root:
        root_pkg, root_unparseable = fetch_package_json(
            fetcher, full_name, "package.json", sha
        )
        if root_unparseable:
            reasons.append("package.json unparseable at package.json")
    workspace_root = (root_pkg is not None and "workspaces" in root_pkg) or bool(
        root & set(NODE_MONOREPO_FILES)
    )
    app_pkg: dict[str, Any] | None = None
    app_path: str | None = None
    if not workspace_root:
        if root_pkg is None and not root_unparseable:
            reasons.append("no root package.json")
        elif not _has_start(root_pkg):
            if not root_unparseable:
                reasons.append("root package.json has no scripts.start")
        else:
            app_path = "."
            app_pkg = root_pkg
    else:
        patterns = _declared_workspace_patterns(
            fetcher, full_name, sha, root_pkg, root
        )
        info["workspace_patterns"] = patterns
        members: list[str] = []
        if not patterns:
            # Markers exist (e.g. nx.json / turbo.json only) but no
            # workspace members are declared anywhere (F1).
            reasons.append("no declared workspace members")
        else:
            members = [
                d for d in tree.match_dirs(patterns)
                if tree.has_file(f"{d}/package.json")
            ]
            start_pkgs: list[tuple[str, dict[str, Any]]] = []
            for d in members:
                pkg, unparseable = fetch_package_json(
                    fetcher, full_name, f"{d}/package.json", sha
                )
                if unparseable:
                    reasons.append(f"package.json unparseable at {d}/package.json")
                if _has_start(pkg):
                    start_pkgs.append((d, pkg))
            if len(start_pkgs) > 1:
                reasons.append("ambiguous app")
            elif len(start_pkgs) == 1:
                app_path, app_pkg = start_pkgs[0]
            else:
                reasons.append("no workspace package with scripts.start")
        info["workspace_members"] = members
    locks = [f for f in NODE_LOCKFILES if f in root]
    if not locks:
        reasons.append("no root lockfile")
    elif len(locks) > 1:
        reasons.append(f"multiple root lockfiles: {', '.join(locks)}")
    deps = _dependencies(root_pkg) | _dependencies(app_pkg)
    if not (deps & set(SQLITE_DEPS)):
        reasons.append(
            "no sqlite dependency (sqlite3, better-sqlite3, @libsql/client) "
            "in root or app package.json dependencies"
        )
    return (not reasons), (app_path if not reasons else None), reasons, info


def python_filter(
    fetcher: Fetcher, full_name: str, sha: str, tree: TreeIndex
) -> tuple[bool, str | None, list[str], dict[str, Any]]:
    """A3. Returns (passed, app.path, typed drop reasons, extra record).

    The extra record carries the sqlite tarball scan's skipped-member
    count (F5).
    """
    reasons: list[str] = []
    info: dict[str, Any] = {}
    root = tree.root_files()
    app_path: str | None = None
    if "manage.py" in root:
        app_path = "."
    else:
        depth_one = sorted(
            d for d in tree.child_dirs("") if tree.has_file(f"{d}/manage.py")
        )
        if len(depth_one) == 1:
            app_path = depth_one[0]
        elif len(depth_one) > 1:
            reasons.append("ambiguous app")
        else:
            reasons.append("no manage.py at root or at depth one")
    found, skipped = repo_contains_string(fetcher, full_name, sha, SQLITE_BACKEND)
    info["sqlite_scan_skipped_members"] = skipped
    if not found:
        if skipped:
            reasons.append(
                "django.db.backends.sqlite3 not found in repository "
                f"({skipped} tarball members skipped over the 4 MB scan cap)"
            )
        else:
            reasons.append("django.db.backends.sqlite3 not found in repository")
    lock_dirs = [app_path] if app_path is not None else []
    if "." not in lock_dirs:
        lock_dirs.append(".")
    has_lock = any(
        tree.has_file(f"{d}/{f}" if d != "." else f)
        for d in lock_dirs for f in PY_LOCKFILES
    )
    if not has_lock:
        reasons.append(
            "no lockfile (uv.lock, poetry.lock, pdm.lock, requirements.txt "
            "or pyproject.toml) in app directory or root"
        )
    return (not reasons), (app_path if not reasons else None), reasons, info


def check_repo(
    fetcher: Fetcher, rec: dict[str, Any]
) -> dict[str, Any]:
    """Item 2 exclusions, then item 3 (A2/A3) filters at HEAD."""
    entry: dict[str, Any] = dict(rec)
    why = exclusion_reason(rec["full_name"])
    if why is not None:
        entry["excluded"] = why
        entry["survivor"] = False
        return entry
    entry["excluded"] = None
    try:
        sha = head_sha(fetcher, rec["full_name"], rec["default_branch"])
        entry["head_sha"] = sha
        paths, truncated = repo_paths(fetcher, rec["full_name"], sha)
        entry["tree_truncated"] = truncated
        tree = TreeIndex(fetcher, rec["full_name"], sha, paths, truncated)
        verdicts: dict[str, Any] = {}
        langs = set(rec["queries"])
        if langs & {"JavaScript", "TypeScript"}:
            passed, app_path, reasons, info = node_filter(
                fetcher, rec["full_name"], sha, tree
            )
            verdicts["node"] = {
                "passed": passed,
                "app_path": app_path,
                "drop_reasons": reasons,
                **info,
            }
        if "Python" in langs:
            passed, app_path, reasons, info = python_filter(
                fetcher, rec["full_name"], sha, tree
            )
            verdicts["python"] = {
                "passed": passed,
                "app_path": app_path,
                "drop_reasons": reasons,
                **info,
            }
        entry["stacks"] = verdicts
        entry["survivor"] = any(v["passed"] for v in verdicts.values())
        if len(verdicts) == 2 and all(v["passed"] for v in verdicts.values()):
            entry["both_stacks_passed"] = True  # F5: ruled on before provisioning
    except HarnessError as e:
        entry["error"] = f"harness or environment: {e}"
        entry["survivor"] = False
    except Exception as e:
        # D74: any other failure left after the fetcher's retries (a
        # corrupt tarball, a malformed response body, ...) is recorded
        # the same way; a single repo never kills the run. A hard kill
        # (KeyboardInterrupt / SystemExit) is a BaseException and still
        # ends the process, to be resumed from the journal.
        entry["error"] = f"harness or environment: {type(e).__name__}: {e}"
        entry["survivor"] = False
    return entry


def item4_key(entry: dict[str, Any]) -> tuple[int, str]:
    """REG-C item 4: stars descending, ties by owner/name."""
    return (-int(entry["stars"]), str(entry["full_name"]).lower())


def _attempt_entry(entry: dict[str, Any], attempt: int) -> dict[str, Any]:
    """One screening attempt, stamped with its attempt number (D79).

    The stamp is journal bookkeeping: it is stripped from the entry
    the outputs are built from (see _winners_and_error_history).
    """
    stamped = dict(entry)
    stamped["attempt"] = attempt
    return stamped


def _retry_errored(
    screen: Callable[[dict[str, Any]], dict[str, Any]],
    pool_list: list[dict[str, Any]],
    history: dict[str, list[dict[str, Any]]],
    record: Callable[[dict[str, Any]], None],
) -> None:
    """D79: re-screen every still-errored repo, up to
    MAX_SCREEN_ATTEMPTS attempts per repo in total.

    Each re-screen is recorded as a new attempt before it joins the
    history. A repo that already used its attempts keeps them — a
    session that died mid-pass recorded the attempts it completed, so
    a resume continues the pass instead of repeating it.
    """
    while True:
        pending = [
            rec for rec in pool_list
            if "error" in history[rec["full_name"]][-1]
            and len(history[rec["full_name"]]) < MAX_SCREEN_ATTEMPTS
        ]
        if not pending:
            return
        for rec in pending:
            name = rec["full_name"]
            entry = _attempt_entry(screen(rec), len(history[name]) + 1)
            record(entry)
            history[name].append(entry)


def _winners_and_error_history(
    pool_list: list[dict[str, Any]],
    history: dict[str, list[dict[str, Any]]],
) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
    """The latest attempt per repo wins (D79), in pool order; a repo
    whose latest attempt is still an error keeps every attempt's
    message, in attempt order."""
    winners: list[dict[str, Any]] = []
    error_history: dict[str, list[str]] = {}
    for rec in pool_list:
        lines = history[rec["full_name"]]
        winner = dict(lines[-1])
        winner.pop("attempt", None)
        winners.append(winner)
        if "error" in lines[-1]:
            error_history[rec["full_name"]] = [
                str(line["error"]) for line in lines if "error" in line
            ]
    return winners, error_history


class JournalMismatch(Exception):
    """A resume was attempted against a journal whose header names a
    different script, main SHA or snapshot date. The resume refuses:
    one journal is one run (A5), under one identity."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _script_sha256() -> str:
    return hashlib.sha256(Path(__file__).resolve().read_bytes()).hexdigest()


def _detect_main_sha() -> str | None:
    """HEAD of the checkout containing this script (the run record
    names it); None when it cannot be determined."""
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).resolve().parents[2],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    sha = proc.stdout.strip()
    return sha if proc.returncode == 0 and sha else None


class Journal:
    """The D74 run journal: one directory is one run (A5).

    header.json identifies the run (script sha256, main SHA, snapshot
    date). pool.json — the collected pool in order, the per-query
    counts and the search metadata — is written before any screening.
    entries.jsonl gains one line per screened repo, as it completes,
    plus one further line per retry attempt of an errored repo (D79).
    events.jsonl records every start and resume (UTC time and the
    previous exit cause) and the exit of a completed session. The
    final outputs are built only from these files, so a process death
    loses nothing already screened and a resume re-screens nothing
    already recorded.
    """

    def __init__(self, dir_path: Path, header: dict[str, Any]) -> None:
        self.dir = dir_path
        self.header = header

    @classmethod
    def open(
        cls,
        dir_path: Path,
        *,
        script_sha256: str,
        main_sha: str,
        snapshot_date: str,
    ) -> tuple[Journal, bool, str | None]:
        """(journal, resumed, previous exit cause).

        A directory without a header starts a new run. A directory
        with one resumes it — after verifying the header names this
        exact script, main SHA and snapshot date; any difference
        raises JournalMismatch before anything is written.
        """
        header_path = dir_path / "header.json"
        if not header_path.exists():
            dir_path.mkdir(parents=True, exist_ok=True)
            journal = cls(dir_path, {
                "format": 1,
                "script_sha256": script_sha256,
                "main_sha": main_sha,
                "snapshot_date": snapshot_date,
            })
            journal._write_atomic("header.json", journal.header)
            return journal, False, None
        header = json.loads(header_path.read_text())
        for field, expected in (
            ("script_sha256", script_sha256),
            ("main_sha", main_sha),
            ("snapshot_date", snapshot_date),
        ):
            if header.get(field) != expected:
                raise JournalMismatch(
                    f"journal {field} is {header.get(field)!r}, "
                    f"this run has {expected!r}; refusing to resume"
                )
        journal = cls(dir_path, header)
        return journal, True, journal._previous_exit_cause()

    def _previous_exit_cause(self) -> str:
        events = self._read_events()
        if events and events[-1].get("type") == "exit":
            return str(events[-1].get("cause", "completed"))
        if events:
            return "not recorded: the previous session ended without an exit record"
        return "not recorded: no prior session events"

    def _read_events(self) -> list[dict[str, Any]]:
        path = self.dir / "events.jsonl"
        if not path.exists():
            return []
        events: list[dict[str, Any]] = []
        for line in path.read_text().splitlines():
            if line.strip():
                events.append(json.loads(line))
        return events

    def _write_atomic(self, name: str, payload: dict[str, Any]) -> None:
        tmp = self.dir / f".{name}.tmp"
        tmp.write_text(json.dumps(payload, indent=1))
        os.replace(tmp, self.dir / name)

    def _append_line(self, name: str, payload: dict[str, Any]) -> None:
        with (self.dir / name).open("a") as f:
            f.write(json.dumps(payload) + "\n")
            f.flush()
            os.fsync(f.fileno())

    def record_start(self, *, resumed: bool, previous_exit_cause: str | None) -> None:
        self._append_line("events.jsonl", {
            "type": "start",
            "at": _utc_now(),
            "resumed": resumed,
            "previous_exit_cause": previous_exit_cause,
        })

    def record_exit(self, cause: str) -> None:
        self._append_line("events.jsonl", {
            "type": "exit",
            "at": _utc_now(),
            "cause": cause,
        })

    def write_pool(
        self,
        pool: list[dict[str, Any]],
        counts: dict[str, int],
        search_meta: dict[str, Any],
    ) -> None:
        self._write_atomic("pool.json", {
            "pool": pool,
            "query_counts": counts,
            "search_meta": search_meta,
        })

    def read_pool(
        self,
    ) -> tuple[list[dict[str, Any]], dict[str, int], dict[str, Any]] | None:
        path = self.dir / "pool.json"
        if not path.exists():
            return None
        stored = json.loads(path.read_text())
        return stored["pool"], stored["query_counts"], stored["search_meta"]

    def append_entry(self, entry: dict[str, Any]) -> None:
        self._append_line("entries.jsonl", entry)

    def read_entries(self) -> list[dict[str, Any]]:
        """Screened-repo entries in completion order.

        A torn final line (a death mid-append) is dropped: that repo
        was never durably recorded and is re-screened on resume. A
        corrupt line anywhere else raises.
        """
        path = self.dir / "entries.jsonl"
        if not path.exists():
            return []
        lines = [ln for ln in path.read_text().splitlines() if ln.strip()]
        entries: list[dict[str, Any]] = []
        for i, line in enumerate(lines):
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                if i == len(lines) - 1:
                    break
                raise
        return entries


def _finalize(
    entries: list[dict[str, Any]],
    *,
    snapshot_date: str,
    counts: dict[str, int],
    search_meta: dict[str, Any],
    pool_size: int,
    authenticated: bool,
    out_dir: Path,
    error_history: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    """Item 4 ordering and the accepted output files, from entries in
    pool order (the same objects appear in entries and survivors, so
    the assigned order shows in both, as the format has always been).
    A still-errored repo is listed with every attempt's message (D79).
    """
    entries = sorted(entries, key=item4_key)
    survivors = [e for e in entries if e.get("survivor")]
    for i, e in enumerate(survivors):
        e["order"] = i + 1
    history_map = error_history or {}
    errored = [
        {
            "full_name": e["full_name"],
            "stars": e["stars"],
            "errors": history_map.get(
                str(e["full_name"]), [str(e["error"])]
            ),
        }
        for e in entries if "error" in e
    ]
    result: dict[str, Any] = {
        "rule": "REG-C as amended by Addendum 2 (A1-A3); items 2 and 4 unchanged",
        "snapshot_date": snapshot_date,
        "pushed_since": pushed_since(snapshot_date),
        "authenticated": authenticated,
        "query_counts": counts,
        "search_pages": search_meta,
        "pool_size": pool_size,
        "survivor_count": len(survivors),
        "error_count": len(errored),
        "errored_repos": errored,
        "entries": entries,
        "survivors": survivors,
    }
    if any(m["incomplete_results"] for m in search_meta.values()):
        result["search_incomplete"] = True  # F4: ruled on before provisioning
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "regc-add2-raw.json").write_text(json.dumps(result, indent=1))
    (out_dir / "regc-add2-survivors.json").write_text(json.dumps(survivors, indent=1))
    return result


def run(
    fetcher: Fetcher,
    snapshot_date: str,
    out_dir: Path,
    sleep: Callable[[float], None] = time.sleep,
    *,
    journal_dir: Path | None = None,
    script_sha256: str | None = None,
    main_sha: str | None = None,
) -> dict[str, Any]:
    authenticated = bool(getattr(fetcher, "authenticated", False))
    if journal_dir is None:
        pool, counts, search_meta = collect_pool(fetcher, snapshot_date, sleep=sleep)
        pool_list = list(pool.values())
        history = {
            rec["full_name"]: [_attempt_entry(check_repo(fetcher, rec), 1)]
            for rec in pool_list
        }
        _retry_errored(
            lambda rec: check_repo(fetcher, rec),
            pool_list,
            history,
            lambda entry: None,
        )
        entries, error_history = _winners_and_error_history(pool_list, history)
        return _finalize(
            entries,
            snapshot_date=snapshot_date,
            counts=counts,
            search_meta=search_meta,
            pool_size=len(pool_list),
            authenticated=authenticated,
            out_dir=out_dir,
            error_history=error_history,
        )
    # D74 journaled run: the pool and every screened entry reach disk
    # before the outputs are built, and the outputs are built only
    # from the journal.
    journal, resumed, previous_cause = Journal.open(
        journal_dir,
        script_sha256=(
            script_sha256 if script_sha256 is not None else _script_sha256()
        ),
        main_sha=main_sha if main_sha is not None else (_detect_main_sha() or ""),
        snapshot_date=snapshot_date,
    )
    journal.record_start(resumed=resumed, previous_exit_cause=previous_cause)
    stored = journal.read_pool()
    if stored is None:
        pool, counts, search_meta = collect_pool(fetcher, snapshot_date, sleep=sleep)
        journal.write_pool(list(pool.values()), counts, search_meta)
        stored = journal.read_pool()
    assert stored is not None  # write_pool just landed (or was already there)
    pool_list, counts, search_meta = stored
    history = {}
    for line in journal.read_entries():
        history.setdefault(str(line["full_name"]), []).append(line)
    for rec in pool_list:
        if rec["full_name"] in history:
            continue  # recorded in a previous session; never re-screened
        entry = _attempt_entry(check_repo(fetcher, rec), 1)
        journal.append_entry(entry)
        history[rec["full_name"]] = [entry]
    _retry_errored(
        lambda rec: check_repo(fetcher, rec),
        pool_list,
        history,
        journal.append_entry,
    )
    entries, error_history = _winners_and_error_history(pool_list, history)
    result = _finalize(
        entries,
        snapshot_date=snapshot_date,
        counts=counts,
        search_meta=search_meta,
        pool_size=len(pool_list),
        authenticated=authenticated,
        out_dir=out_dir,
        error_history=error_history,
    )
    journal.record_exit("completed")
    return result


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--snapshot-date", default=date.today().isoformat())
    ap.add_argument(
        "--out",
        type=Path,
        default=Path.home() / "workspace" / "wo6-scratch" / "regc-add2-raw",
    )
    ap.add_argument(
        "--journal",
        type=Path,
        default=None,
        help="run journal directory (D74); defaults to <out>-journal",
    )
    ap.add_argument(
        "--main-sha",
        default=None,
        help="the main SHA this run is from (recorded in the journal "
        "header); defaults to the checkout's HEAD",
    )
    args = ap.parse_args()
    token = os.environ.get("GITHUB_TOKEN") or None
    fetcher = Fetcher(token=token)
    journal_dir = (
        args.journal
        if args.journal is not None
        else args.out.parent / f"{args.out.name}-journal"
    )
    main_sha = args.main_sha or _detect_main_sha()
    if not main_sha:
        print(
            "error: the main SHA could not be determined; pass --main-sha",
            file=sys.stderr,
        )
        raise SystemExit(2)
    resumed = (journal_dir / "header.json").exists()
    try:
        result = run(
            fetcher,
            args.snapshot_date,
            args.out,
            journal_dir=journal_dir,
            main_sha=main_sha,
        )
    except JournalMismatch as err:
        print(f"error: {err}", file=sys.stderr)
        raise SystemExit(2) from err
    print(f"journal: {journal_dir} ({'resumed' if resumed else 'new run'})")
    print(f"snapshot date: {result['snapshot_date']} (pushed_since {result['pushed_since']})")
    for lang in LANGUAGES:
        print(f"query language:{lang}: returned={result['query_counts'][lang]}")
    print(f"pool after dedup: {result['pool_size']}")
    print(f"SURVIVORS: {result['survivor_count']}")
    print(f"ERRORED: {result['error_count']}")
    for e in result["survivors"]:
        stacks = ",".join(k for k, v in e["stacks"].items() if v["passed"])
        apps = ",".join(
            f"{k}={v['app_path']}" for k, v in e["stacks"].items() if v["passed"]
        )
        print(f"{e['order']}. {e['full_name']}\t{e['stars']}\t{stacks}\t{apps}")


if __name__ == "__main__":
    main()
