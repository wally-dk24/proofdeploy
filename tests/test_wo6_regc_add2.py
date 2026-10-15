"""Offline tests for scripts/wo6/regc_add2_run.py (REG-C Addendum 2).

No live API: a FakeFetcher serves recorded-style responses built from
per-repo file maps, and the Fetcher's retry logic is exercised with an
injected raw_fetch. Each filter test pins one A2/A3 branch.
"""
from __future__ import annotations

import importlib.util
import io
import json
import sys
import tarfile
from pathlib import Path
from typing import Any

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "wo6" / "regc_add2_run.py"
_spec = importlib.util.spec_from_file_location("regc_add2_run", SCRIPT)
assert _spec is not None and _spec.loader is not None
mod = importlib.util.module_from_spec(_spec)
sys.modules["regc_add2_run"] = mod
_spec.loader.exec_module(mod)

SHA = "a" * 40
NO_SLEEP = lambda _s: None  # noqa: E731


def make_tarball(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for path, content in files.items():
            data = content.encode()
            info = tarfile.TarInfo(f"repo-{path}")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class FakeFetcher:
    """Duck-type of regc_add2_run.Fetcher backed by recorded fixtures.

    repos: {full_name: {"files": {path: str}, "branch": str}} where a
    file whose content parses as JSON is served as-is for package.json
    fetches and every file goes into the tarball fixture.
    search_pages: {language: [[item, ...], ...]} (one list per page).
    """

    def __init__(
        self,
        repos: dict[str, dict[str, Any]] | None = None,
        search_pages: dict[str, list[list[dict[str, Any]]]] | None = None,
    ) -> None:
        self.repos = repos or {}
        self.search_pages = search_pages or {}
        self.calls: list[str] = []

    # -- search ------------------------------------------------------
    def _search(self, url: str) -> dict[str, Any]:
        lang = next(lang for lang in mod.LANGUAGES if f"language%3A{lang}" in url
                    or f"language:{lang}" in url)
        page = int(url.rsplit("page=", 1)[1])
        pages = self.search_pages.get(lang, [])
        items = pages[page - 1] if page <= len(pages) else []
        return {"total_count": sum(len(p) for p in pages), "items": items}

    # -- dispatch ----------------------------------------------------
    def get_json(self, url: str) -> Any:
        self.calls.append(url)
        if "/search/repositories" in url:
            return self._search(url)
        if "/branches/" in url:
            return {"commit": {"sha": SHA}}
        if "/git/trees/" in url:
            fn = url.split("/repos/", 1)[1].split("/git/trees/")[0]
            files = self.repos[fn]["files"]
            return {
                "tree": [{"path": p, "type": "blob"} for p in sorted(files)],
                "truncated": False,
            }
        raise AssertionError(f"unexpected get_json URL: {url}")

    def get_text(self, url: str) -> str:
        self.calls.append(url)
        if "/contents/" in url:
            fn = url.split("/repos/", 1)[1].split("/contents/")[0]
            path = url.split("/contents/", 1)[1].split("?ref=")[0]
            files = self.repos[fn]["files"]
            if path not in files:
                raise mod.HarnessError(f"GET {url} -> 404")
            return files[path]
        raise AssertionError(f"unexpected get_text URL: {url}")

    def get_bytes(self, url: str) -> bytes:
        self.calls.append(url)
        if "/tarball/" in url:
            fn = url.split("/repos/", 1)[1].split("/tarball/")[0]
            return make_tarball(self.repos[fn]["files"])
        raise AssertionError(f"unexpected get_bytes URL: {url}")


def item(full_name: str, stars: int) -> dict[str, Any]:
    return {
        "full_name": full_name,
        "stargazers_count": stars,
        "default_branch": "main",
    }


def check(fetcher: FakeFetcher, full_name: str, stars: int, queries: list[str]) -> dict[str, Any]:
    rec = {"full_name": full_name, "stars": stars, "default_branch": "main", "queries": queries}
    return mod.check_repo(fetcher, rec)


# ---------------------------------------------------------------- A1 pool


def test_pagination_collects_up_to_1000_per_query() -> None:
    pages = [[item(f"express/r{i:04d}", 5000 - i) for i in range(p * 100, p * 100 + 100)]
             for p in range(10)]
    fetcher = FakeFetcher(search_pages={"JavaScript": pages})
    pool, counts = mod.collect_pool(fetcher, "2026-10-15", sleep=NO_SLEEP)
    assert counts["JavaScript"] == 1000
    assert len(pool) == 1000
    js_calls = [c for c in fetcher.calls
                if "/search/repositories" in c and "language%3AJavaScript" in c]
    assert len(js_calls) == 10  # never asks for page 11


def test_pagination_stops_early_on_short_page() -> None:
    pages = [
        [item(f"acme/app{i}", 3000 - i) for i in range(100)],
        [item(f"acme/app{i}", 3000 - i) for i in range(100, 137)],
    ]
    fetcher = FakeFetcher(search_pages={"JavaScript": pages})
    pool, counts = mod.collect_pool(fetcher, "2026-10-15", sleep=NO_SLEEP)
    assert counts["JavaScript"] == 137
    assert len(pool) == 137
    js_calls = [c for c in fetcher.calls
                if "/search/repositories" in c and "language%3AJavaScript" in c]
    assert len(js_calls) == 2


def test_pool_dedupes_across_queries() -> None:
    pages_js = [[item("acme/shared", 2000)]]
    pages_ts = [[item("acme/shared", 2000), item("acme/ts-only", 1500)]]
    fetcher = FakeFetcher(search_pages={"JavaScript": pages_js, "TypeScript": pages_ts})
    pool, counts = mod.collect_pool(fetcher, "2026-10-15", sleep=NO_SLEEP)
    assert counts == {"JavaScript": 1, "TypeScript": 2, "Python": 0}
    assert sorted(pool["acme/shared"]["queries"]) == ["JavaScript", "TypeScript"]


# ------------------------------------------------------------- item 2


def test_exclusion_named_and_bare_name_case_insensitive() -> None:
    assert mod.exclusion_reason("TryGhost/Ghost") == "named-exclusion"
    assert mod.exclusion_reason("nestjs/nest") == "named-exclusion"
    assert mod.exclusion_reason("someone/EXPRESS") == "bare-name-exclusion"
    assert mod.exclusion_reason("Wagtail/someapp") == "bare-name-exclusion"
    assert mod.exclusion_reason("acme/plainapp") is None


# ---------------------------------------------------------------- A2 Node


def test_node_root_app_passes() -> None:
    repos = {"acme/rootapp": {"files": {
        "package.json": json.dumps({
            "scripts": {"start": "node index.js"},
            "dependencies": {"better-sqlite3": "^11.0.0"},
        }),
        "package-lock.json": "{}",
    }}}
    entry = check(FakeFetcher(repos), "acme/rootapp", 2000, ["JavaScript"])
    assert entry["survivor"] is True
    assert entry["head_sha"] == SHA
    assert entry["stacks"]["node"] == {
        "passed": True, "app_path": ".", "drop_reasons": [],
    }


def test_node_single_workspace_app_passes() -> None:
    repos = {"acme/mono": {"files": {
        "package.json": json.dumps({"workspaces": ["apps/*"]}),
        "package-lock.json": "{}",
        "apps/web/package.json": json.dumps({
            "scripts": {"start": "node server.js"},
            "dependencies": {"sqlite3": "^5.1.0"},
        }),
        "apps/lib/package.json": json.dumps({"dependencies": {}}),
    }}}
    entry = check(FakeFetcher(repos), "acme/mono", 2000, ["TypeScript"])
    assert entry["survivor"] is True
    assert entry["stacks"]["node"]["app_path"] == "apps/web"


def test_node_workspace_zero_start_packages_excluded() -> None:
    repos = {"acme/mono": {"files": {
        "package.json": json.dumps({"name": "root"}),
        "pnpm-workspace.yaml": "packages: ['apps/*']",
        "pnpm-lock.yaml": "",
        "apps/web/package.json": json.dumps({"dependencies": {"sqlite3": "^5.1.0"}}),
    }}}
    entry = check(FakeFetcher(repos), "acme/mono", 2000, ["JavaScript"])
    assert entry["survivor"] is False
    assert "no workspace package with scripts.start" in (
        entry["stacks"]["node"]["drop_reasons"]
    )


def test_node_workspace_two_start_packages_ambiguous() -> None:
    repos = {"acme/mono": {"files": {
        "package.json": json.dumps({"workspaces": ["apps/*"]}),
        "yarn.lock": "",
        "apps/a/package.json": json.dumps({
            "scripts": {"start": "node a.js"}, "dependencies": {"sqlite3": "^5.1.0"},
        }),
        "apps/b/package.json": json.dumps({
            "scripts": {"start": "node b.js"}, "dependencies": {},
        }),
    }}}
    entry = check(FakeFetcher(repos), "acme/mono", 2000, ["JavaScript"])
    assert entry["survivor"] is False
    assert "ambiguous app" in entry["stacks"]["node"]["drop_reasons"]


def test_node_two_root_lockfiles_excluded() -> None:
    repos = {"acme/twolocks": {"files": {
        "package.json": json.dumps({
            "scripts": {"start": "node index.js"},
            "dependencies": {"sqlite3": "^5.1.0"},
        }),
        "package-lock.json": "{}",
        "yarn.lock": "",
    }}}
    entry = check(FakeFetcher(repos), "acme/twolocks", 2000, ["JavaScript"])
    assert entry["survivor"] is False
    reasons = entry["stacks"]["node"]["drop_reasons"]
    assert any(r.startswith("multiple root lockfiles") for r in reasons)


def test_node_bun_lock_counts_as_the_one_lockfile() -> None:
    repos = {"acme/bunapp": {"files": {
        "package.json": json.dumps({
            "scripts": {"start": "bun run index.ts"},
            "dependencies": {"@libsql/client": "^0.14.0"},
        }),
        "bun.lock": "",
    }}}
    entry = check(FakeFetcher(repos), "acme/bunapp", 2000, ["TypeScript"])
    assert entry["survivor"] is True
    assert entry["stacks"]["node"]["passed"] is True


def test_node_db_dep_only_in_app_package() -> None:
    repos = {"acme/mono": {"files": {
        "package.json": json.dumps({
            "workspaces": ["packages/*"], "dependencies": {"express": "^4.0.0"},
        }),
        "package-lock.json": "{}",
        "packages/api/package.json": json.dumps({
            "scripts": {"start": "node api.js"},
            "dependencies": {"better-sqlite3": "^11.0.0"},
        }),
    }}}
    entry = check(FakeFetcher(repos), "acme/mono", 2000, ["JavaScript"])
    assert entry["survivor"] is True
    assert entry["stacks"]["node"]["app_path"] == "packages/api"


def test_node_root_app_without_start_excluded() -> None:
    repos = {"acme/lib": {"files": {
        "package.json": json.dumps({"dependencies": {"sqlite3": "^5.1.0"}}),
        "package-lock.json": "{}",
    }}}
    entry = check(FakeFetcher(repos), "acme/lib", 2000, ["JavaScript"])
    assert entry["survivor"] is False
    assert "root package.json has no scripts.start" in (
        entry["stacks"]["node"]["drop_reasons"]
    )


# -------------------------------------------------------------- A3 Python


PY_FILES = {
    "manage.py": "",
    "config/settings.py": "DATABASES = {'default': {'ENGINE': 'django.db.backends.sqlite3'}}",
    "requirements.txt": "django\n",
}


def test_python_root_app_passes() -> None:
    repos = {"acme/dj": {"files": dict(PY_FILES)}}
    entry = check(FakeFetcher(repos), "acme/dj", 2000, ["Python"])
    assert entry["survivor"] is True
    assert entry["stacks"]["python"] == {
        "passed": True, "app_path": ".", "drop_reasons": [],
    }


def test_python_manage_py_at_depth_one() -> None:
    files = {
        "backend/manage.py": "",
        "backend/config/settings.py": "ENGINE = 'django.db.backends.sqlite3'",
        "backend/requirements.txt": "django\n",
    }
    repos = {"acme/dj": {"files": files}}
    entry = check(FakeFetcher(repos), "acme/dj", 2000, ["Python"])
    assert entry["survivor"] is True
    assert entry["stacks"]["python"]["app_path"] == "backend"


def test_python_two_manage_py_at_depth_one_ambiguous() -> None:
    files = {
        "one/manage.py": "",
        "two/manage.py": "",
        "one/settings.py": "ENGINE = 'django.db.backends.sqlite3'",
        "pyproject.toml": "[project]\nname='x'\n",
    }
    repos = {"acme/dj": {"files": files}}
    entry = check(FakeFetcher(repos), "acme/dj", 2000, ["Python"])
    assert entry["survivor"] is False
    assert "ambiguous app" in entry["stacks"]["python"]["drop_reasons"]


def test_python_missing_sqlite_string_excluded() -> None:
    files = {"manage.py": "", "requirements.txt": "django\n"}
    repos = {"acme/dj": {"files": files}}
    entry = check(FakeFetcher(repos), "acme/dj", 2000, ["Python"])
    assert entry["survivor"] is False
    assert "django.db.backends.sqlite3 not found in repository" in (
        entry["stacks"]["python"]["drop_reasons"]
    )


def test_python_lockfile_in_app_dir_counts() -> None:
    files = {
        "app/manage.py": "",
        "app/settings.py": "ENGINE = 'django.db.backends.sqlite3'",
        "app/uv.lock": "",
    }
    repos = {"acme/dj": {"files": files}}
    entry = check(FakeFetcher(repos), "acme/dj", 2000, ["Python"])
    assert entry["survivor"] is True


# ------------------------------------------------- harness / rate limits


def test_rate_limit_is_retried_then_succeeds() -> None:
    attempts: list[str] = []
    waits: list[float] = []

    def raw_fetch(url: str, headers: dict[str, str]) -> tuple[int, dict[str, str], bytes]:
        attempts.append(url)
        if len(attempts) == 1:
            return 403, {"Retry-After": "7"}, b"rate limited"
        return 200, {}, json.dumps({"ok": True}).encode()

    fetcher = mod.Fetcher(sleep=waits.append, raw_fetch=raw_fetch)
    assert fetcher.get_json("https://api.github.com/x") == {"ok": True}
    assert len(attempts) == 2
    assert waits == [7.0]


def test_rate_limit_exhaustion_is_harness_error_not_exclusion() -> None:
    def raw_fetch(url: str, headers: dict[str, str]) -> tuple[int, dict[str, str], bytes]:
        return 403, {"Retry-After": "0"}, b"rate limited"

    fetcher = mod.Fetcher(sleep=NO_SLEEP, raw_fetch=raw_fetch, max_retries=2)

    class FailingFetcher:
        def get_json(self, url: str) -> Any:
            return fetcher.get_json(url)

    rec = {"full_name": "acme/slow", "stars": 2000, "default_branch": "main",
           "queries": ["JavaScript"]}
    entry = mod.check_repo(FailingFetcher(), rec)  # type: ignore[arg-type]
    assert entry["survivor"] is False
    assert entry["excluded"] is None
    assert entry["error"].startswith("harness or environment:")


# --------------------------------------------------------------- item 4


def test_item4_order_stars_desc_then_owner_name() -> None:
    entries = [
        {"full_name": "zeta/app", "stars": 3000},
        {"full_name": "alpha/app", "stars": 3000},
        {"full_name": "mid/app", "stars": 5000},
    ]
    ordered = sorted(entries, key=mod.item4_key)
    assert [e["full_name"] for e in ordered] == ["mid/app", "alpha/app", "zeta/app"]


def test_run_end_to_end_writes_outputs(tmp_path: Path) -> None:
    repos = {"acme/rootapp": {"files": {
        "package.json": json.dumps({
            "scripts": {"start": "node index.js"},
            "dependencies": {"sqlite3": "^5.1.0"},
        }),
        "package-lock.json": "{}",
    }}}
    pages = [[item("acme/rootapp", 2000), item("someone/express", 9000)]]
    fetcher = FakeFetcher(repos=repos, search_pages={"JavaScript": pages})
    result = mod.run(fetcher, "2026-10-15", tmp_path, sleep=NO_SLEEP)
    assert result["query_counts"]["JavaScript"] == 2
    assert result["survivor_count"] == 1
    survivor = result["survivors"][0]
    assert survivor["full_name"] == "acme/rootapp"
    assert survivor["order"] == 1
    excluded = [e for e in result["entries"] if e.get("excluded")]
    assert excluded[0]["full_name"] == "someone/express"
    assert (tmp_path / "regc-add2-raw.json").exists()
    assert (tmp_path / "regc-add2-survivors.json").exists()
