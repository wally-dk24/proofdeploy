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
  commits or diffs are ever fetched).
- Item 4, unchanged: survivors sorted by stars descending, ties by
  owner/name.

Running this script against the live API IS the A5 single run; it is
reviewed before that happens. The token, if any, comes from the
GITHUB_TOKEN environment variable (read-only, per REG-C item 1) and is
never written to any output. Rate limits are handled by waiting and
retrying; a repo whose checks still fail after retries is recorded as
"harness or environment: <error>", never as excluded.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path
from typing import Any

API = "https://api.github.com"
USER_AGENT = "proofdeploy-regc-add2-runner"
LANGUAGES = ("JavaScript", "TypeScript", "Python")
PER_PAGE = 100
MAX_PAGES = 10  # A1: 10 pages x 100 = the first 1000 results per query.

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
    offline tests; the default implementation uses urllib. A retryable
    failure (403/429 rate limiting, 5xx, network error) is retried after
    the server-provided wait (Retry-After, else X-RateLimit-Reset, else
    exponential backoff); after max_retries the call raises HarnessError.
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

    def _urllib_fetch(
        self, url: str, headers: dict[str, str]
    ) -> tuple[int, dict[str, str], bytes]:
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req) as r:
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
            if status == 200:
                return body
            if status == 404:
                raise HarnessError(f"GET {url} -> 404")
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
) -> tuple[dict[str, dict[str, Any]], dict[str, int]]:
    """REG-C item 1 as amended by A1. Returns (pool, per-query counts)."""
    pool: dict[str, dict[str, Any]] = {}
    counts: dict[str, int] = {}
    for language in LANGUAGES:
        q = urllib.parse.quote(search_query(language, snapshot_date))
        returned = 0
        for page in range(1, MAX_PAGES + 1):
            url = (
                f"{API}/search/repositories?q={q}"
                f"&sort=stars&order=desc&per_page={PER_PAGE}&page={page}"
            )
            data = fetcher.get_json(url)
            items = data.get("items", [])
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
    return pool, counts


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


def fetch_file_text(fetcher: Fetcher, full_name: str, path: str, sha: str) -> str | None:
    """A file's raw content at the SHA, or None when it does not exist."""
    url = (
        f"{API}/repos/{full_name}/contents/{urllib.parse.quote(path, safe='/')}"
        f"?ref={sha}"
    )
    try:
        return fetcher.get_text(url)
    except HarnessError as e:
        if "404" in str(e):
            return None
        raise


def fetch_package_json(
    fetcher: Fetcher, full_name: str, path: str, sha: str
) -> dict[str, Any] | None:
    text = fetch_file_text(fetcher, full_name, path, sha)
    if text is None:
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def repo_contains_string(fetcher: Fetcher, full_name: str, sha: str, needle: bytes) -> bool:
    """True when the needle appears in any file of the repo at the SHA.

    One tarball fetch for the exact SHA, scanned member by member (the
    mechanism scripts/wo6/regc_stage2.py used for the same check).
    """
    blob = fetcher.get_bytes(f"{API}/repos/{full_name}/tarball/{sha}")
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tf:
        for member in tf.getmembers():
            if member.isfile() and member.size <= TARBALL_MEMBER_CAP:
                f = tf.extractfile(member)
                if f is not None and needle in f.read():
                    return True
    return False


def _root_files(paths: set[str]) -> set[str]:
    return {p for p in paths if "/" not in p}


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


def node_filter(
    fetcher: Fetcher, full_name: str, sha: str, paths: set[str]
) -> tuple[bool, str | None, list[str]]:
    """A2. Returns (passed, app.path, typed drop reasons)."""
    reasons: list[str] = []
    root = _root_files(paths)
    root_pkg = (
        fetch_package_json(fetcher, full_name, "package.json", sha)
        if "package.json" in root
        else None
    )
    workspace_root = (root_pkg is not None and "workspaces" in root_pkg) or bool(
        root & set(NODE_MONOREPO_FILES)
    )
    app_pkg: dict[str, Any] | None = None
    app_path: str | None = None
    if not workspace_root:
        app_path = "."
        if root_pkg is None:
            reasons.append("no root package.json")
        elif not _has_start(root_pkg):
            reasons.append("root package.json has no scripts.start")
        else:
            app_pkg = root_pkg
    else:
        start_pkgs: list[tuple[str, dict[str, Any]]] = []
        candidates = sorted(
            p
            for p in paths
            if p.endswith("/package.json") and "node_modules" not in p.split("/")
        )
        for pkg_path in candidates:
            pkg = fetch_package_json(fetcher, full_name, pkg_path, sha)
            if _has_start(pkg):
                start_pkgs.append((pkg_path.rsplit("/", 1)[0], pkg))
        if not start_pkgs:
            reasons.append("no workspace package with scripts.start")
        elif len(start_pkgs) > 1:
            reasons.append("ambiguous app")
        else:
            app_path, app_pkg = start_pkgs[0]
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
    return (not reasons), (app_path if not reasons else None), reasons


def python_filter(
    fetcher: Fetcher, full_name: str, sha: str, paths: set[str]
) -> tuple[bool, str | None, list[str]]:
    """A3. Returns (passed, app.path, typed drop reasons)."""
    reasons: list[str] = []
    root = _root_files(paths)
    depth_one_dirs = sorted(
        {p.split("/", 1)[0] for p in paths if p.count("/") == 1 and p.endswith("/manage.py")}
    )
    app_path: str | None = None
    if "manage.py" in root:
        app_path = "."
    elif len(depth_one_dirs) == 1:
        app_path = depth_one_dirs[0]
    elif len(depth_one_dirs) > 1:
        reasons.append("ambiguous app")
    else:
        reasons.append("no manage.py at root or at depth one")
    if not repo_contains_string(fetcher, full_name, sha, SQLITE_BACKEND):
        reasons.append("django.db.backends.sqlite3 not found in repository")
    lock_dirs = [app_path] if app_path is not None else []
    if "." not in lock_dirs:
        lock_dirs.append(".")
    has_lock = any(
        (f"{d}/{f}" if d != "." else f) in paths for d in lock_dirs for f in PY_LOCKFILES
    )
    if not has_lock:
        reasons.append(
            "no lockfile (uv.lock, poetry.lock, pdm.lock, requirements.txt "
            "or pyproject.toml) in app directory or root"
        )
    return (not reasons), (app_path if not reasons else None), reasons


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
        verdicts: dict[str, Any] = {}
        langs = set(rec["queries"])
        if langs & {"JavaScript", "TypeScript"}:
            passed, app_path, reasons = node_filter(fetcher, rec["full_name"], sha, paths)
            verdicts["node"] = {
                "passed": passed,
                "app_path": app_path,
                "drop_reasons": reasons,
            }
        if "Python" in langs:
            passed, app_path, reasons = python_filter(fetcher, rec["full_name"], sha, paths)
            verdicts["python"] = {
                "passed": passed,
                "app_path": app_path,
                "drop_reasons": reasons,
            }
        entry["stacks"] = verdicts
        entry["survivor"] = any(v["passed"] for v in verdicts.values())
    except HarnessError as e:
        entry["error"] = f"harness or environment: {e}"
        entry["survivor"] = False
    return entry


def item4_key(entry: dict[str, Any]) -> tuple[int, str]:
    """REG-C item 4: stars descending, ties by owner/name."""
    return (-int(entry["stars"]), str(entry["full_name"]).lower())


def run(
    fetcher: Fetcher,
    snapshot_date: str,
    out_dir: Path,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    pool, counts = collect_pool(fetcher, snapshot_date, sleep=sleep)
    entries = [check_repo(fetcher, rec) for rec in pool.values()]
    entries.sort(key=item4_key)
    survivors = [e for e in entries if e.get("survivor")]
    for i, e in enumerate(survivors):
        e["order"] = i + 1
    result: dict[str, Any] = {
        "rule": "REG-C as amended by Addendum 2 (A1-A3); items 2 and 4 unchanged",
        "snapshot_date": snapshot_date,
        "pushed_since": pushed_since(snapshot_date),
        "query_counts": counts,
        "pool_size": len(pool),
        "survivor_count": len(survivors),
        "entries": entries,
        "survivors": survivors,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "regc-add2-raw.json").write_text(json.dumps(result, indent=1))
    (out_dir / "regc-add2-survivors.json").write_text(json.dumps(survivors, indent=1))
    return result


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--snapshot-date", default=date.today().isoformat())
    ap.add_argument(
        "--out",
        type=Path,
        default=Path.home() / "workspace" / "wo6-scratch" / "regc-add2-raw",
    )
    args = ap.parse_args()
    token = os.environ.get("GITHUB_TOKEN") or None
    fetcher = Fetcher(token=token)
    result = run(fetcher, args.snapshot_date, args.out)
    print(f"snapshot date: {result['snapshot_date']} (pushed_since {result['pushed_since']})")
    for lang in LANGUAGES:
        print(f"query language:{lang}: returned={result['query_counts'][lang]}")
    print(f"pool after dedup: {result['pool_size']}")
    print(f"SURVIVORS: {result['survivor_count']}")
    for e in result["survivors"]:
        stacks = ",".join(k for k, v in e["stacks"].items() if v["passed"])
        apps = ",".join(
            f"{k}={v['app_path']}" for k, v in e["stacks"].items() if v["passed"]
        )
        print(f"{e['order']}. {e['full_name']}\t{e['stars']}\t{stacks}\t{apps}")


if __name__ == "__main__":
    main()
