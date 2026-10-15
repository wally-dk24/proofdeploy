"""Offline tests for scripts/wo6/regc_add2_run.py (REG-C Addendum 2).

No live API: a FakeFetcher serves recorded-style responses built from
per-repo file maps, and the Fetcher's retry logic is exercised with an
injected raw_fetch. Each filter test pins one A2/A3 branch; the
changes-round tests pin the reviewer's F1-F6 findings on PR #108.
"""
from __future__ import annotations

import importlib.util
import io
import json
import sys
import tarfile
import urllib.parse
from pathlib import Path
from typing import Any

import pytest

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

    repos: {full_name: {"files": {path: str}, "branch": str, ...}} where
    a file whose content parses as JSON is served as-is for package.json
    fetches and every file goes into the tarball fixture. Optional repo
    keys: "truncated" (the recursive tree reports truncated),
    "tree_paths" (the paths the recursive tree returns — a subset of
    the files when modelling truncation; contents listings always see
    the full file map), "fail_checks" (the branch fetch raises a
    HarnessError), "fail_listings" (contents listings raise a
    HarnessError). There is deliberately no `authenticated` attribute:
    run() must record False for a fetcher without one.
    search_pages: {language: [page, ...]} where a page is either a list
    of items or {"items": [...], "total_count": int,
    "incomplete_results": bool}.
    """

    def __init__(
        self,
        repos: dict[str, dict[str, Any]] | None = None,
        search_pages: dict[str, list[Any]] | None = None,
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
        raw = pages[page - 1] if page <= len(pages) else []
        if isinstance(raw, dict):
            return {
                "total_count": raw.get("total_count", len(raw.get("items", []))),
                "incomplete_results": raw.get("incomplete_results", False),
                "items": raw.get("items", []),
            }
        total = sum(
            len(p) if isinstance(p, list)
            else p.get("total_count", len(p.get("items", [])))
            for p in pages
        )
        return {"total_count": total, "incomplete_results": False, "items": raw}

    # -- dispatch ----------------------------------------------------
    def get_json(self, url: str) -> Any:
        self.calls.append(url)
        if "/search/repositories" in url:
            return self._search(url)
        if "/branches/" in url:
            fn = url.split("/repos/", 1)[1].split("/branches/")[0]
            if self.repos.get(fn, {}).get("fail_checks"):
                raise mod.HarnessError("simulated check failure")
            return {"commit": {"sha": SHA}}
        if "/git/trees/" in url:
            fn = url.split("/repos/", 1)[1].split("/git/trees/")[0]
            repo = self.repos[fn]
            tree_paths = repo.get("tree_paths", sorted(repo["files"]))
            return {
                "tree": [{"path": p, "type": "blob"} for p in tree_paths],
                "truncated": bool(repo.get("truncated", False)),
            }
        if "/contents" in url:
            fn = url.split("/repos/", 1)[1].split("/contents")[0]
            repo = self.repos[fn]
            if repo.get("fail_listings"):
                raise mod.HarnessError("simulated listing failure")
            rest = url.split("/contents", 1)[1]
            path = urllib.parse.unquote(rest.lstrip("/").split("?ref=")[0])
            files = repo["files"]
            if path and path in files:
                return {
                    "type": "file",
                    "path": path,
                    "name": path.rsplit("/", 1)[-1],
                }
            prefix = f"{path}/" if path else ""
            entries: list[dict[str, str]] = []
            seen: set[str] = set()
            for p in sorted(files):
                if not p.startswith(prefix):
                    continue
                head, _, tail = p[len(prefix):].partition("/")
                if head in seen:
                    continue
                seen.add(head)
                entries.append({
                    "name": head,
                    "path": prefix + head,
                    "type": "dir" if tail else "file",
                })
            if path and not entries:
                raise mod.NotFound(f"GET {url} -> 404")
            return entries
        raise AssertionError(f"unexpected get_json URL: {url}")

    def get_text(self, url: str) -> str:
        self.calls.append(url)
        if "/contents/" in url:
            fn = url.split("/repos/", 1)[1].split("/contents/")[0]
            path = url.split("/contents/", 1)[1].split("?ref=")[0]
            files = self.repos[fn]["files"]
            if path not in files:
                raise mod.NotFound(f"GET {url} -> 404")
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
    pool, counts, _meta = mod.collect_pool(fetcher, "2026-10-15", sleep=NO_SLEEP)
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
    pool, counts, _meta = mod.collect_pool(fetcher, "2026-10-15", sleep=NO_SLEEP)
    assert counts["JavaScript"] == 137
    assert len(pool) == 137
    js_calls = [c for c in fetcher.calls
                if "/search/repositories" in c and "language%3AJavaScript" in c]
    assert len(js_calls) == 2


def test_pool_dedupes_across_queries() -> None:
    pages_js = [[item("acme/shared", 2000)]]
    pages_ts = [[item("acme/shared", 2000), item("acme/ts-only", 1500)]]
    fetcher = FakeFetcher(search_pages={"JavaScript": pages_js, "TypeScript": pages_ts})
    pool, counts, _meta = mod.collect_pool(fetcher, "2026-10-15", sleep=NO_SLEEP)
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


# ------------------------------------------------------------- F1 members


def test_node_nested_non_member_package_does_not_count() -> None:
    # examples/demo has scripts.start but is not a declared workspace
    # member; it must not make the repo "ambiguous app" (F1).
    repos = {"acme/mono": {"files": {
        "package.json": json.dumps({"workspaces": ["apps/*"]}),
        "package-lock.json": "{}",
        "apps/web/package.json": json.dumps({
            "scripts": {"start": "node server.js"},
            "dependencies": {"sqlite3": "^5.1.0"},
        }),
        "examples/demo/package.json": json.dumps({
            "scripts": {"start": "node demo.js"}, "dependencies": {},
        }),
    }}}
    entry = check(FakeFetcher(repos), "acme/mono", 2000, ["JavaScript"])
    assert entry["survivor"] is True
    node = entry["stacks"]["node"]
    assert node["app_path"] == "apps/web"
    assert node["workspace_patterns"] == ["apps/*"]
    assert node["workspace_members"] == ["apps/web"]


def test_node_object_form_workspaces() -> None:
    repos = {"acme/mono": {"files": {
        "package.json": json.dumps({"workspaces": {"packages": ["apps/*"]}}),
        "package-lock.json": "{}",
        "apps/web/package.json": json.dumps({
            "scripts": {"start": "node server.js"},
            "dependencies": {"sqlite3": "^5.1.0"},
        }),
    }}}
    entry = check(FakeFetcher(repos), "acme/mono", 2000, ["JavaScript"])
    assert entry["survivor"] is True
    assert entry["stacks"]["node"]["workspace_members"] == ["apps/web"]


def test_node_pnpm_workspace_negation() -> None:
    repos = {"acme/mono": {"files": {
        "package.json": json.dumps({"name": "root"}),
        "pnpm-workspace.yaml": "packages:\n  - 'apps/*'\n  - '!apps/skip'\n",
        "pnpm-lock.yaml": "",
        "apps/web/package.json": json.dumps({
            "scripts": {"start": "node server.js"},
            "dependencies": {"sqlite3": "^5.1.0"},
        }),
        "apps/skip/package.json": json.dumps({
            "scripts": {"start": "node skip.js"}, "dependencies": {},
        }),
    }}}
    entry = check(FakeFetcher(repos), "acme/mono", 2000, ["JavaScript"])
    assert entry["survivor"] is True
    node = entry["stacks"]["node"]
    assert node["app_path"] == "apps/web"
    assert node["workspace_patterns"] == ["apps/*", "!apps/skip"]
    assert node["workspace_members"] == ["apps/web"]


def test_node_lerna_default_packages() -> None:
    repos = {"acme/mono": {"files": {
        "package.json": json.dumps({"name": "root"}),
        "lerna.json": json.dumps({"version": "1.0.0"}),
        "package-lock.json": "{}",
        "packages/api/package.json": json.dumps({
            "scripts": {"start": "node api.js"},
            "dependencies": {"better-sqlite3": "^11.0.0"},
        }),
    }}}
    entry = check(FakeFetcher(repos), "acme/mono", 2000, ["JavaScript"])
    assert entry["survivor"] is True
    node = entry["stacks"]["node"]
    assert node["app_path"] == "packages/api"
    assert node["workspace_patterns"] == ["packages/*"]
    assert node["workspace_members"] == ["packages/api"]


def test_node_nx_only_marker_no_declared_members() -> None:
    # nx.json declares no members; with no patterns anywhere the nested
    # app is not a workspace package (F1).
    repos = {"acme/mono": {"files": {
        "package.json": json.dumps({"name": "root"}),
        "nx.json": "{}",
        "package-lock.json": "{}",
        "apps/web/package.json": json.dumps({
            "scripts": {"start": "node server.js"},
            "dependencies": {"sqlite3": "^5.1.0"},
        }),
    }}}
    entry = check(FakeFetcher(repos), "acme/mono", 2000, ["JavaScript"])
    assert entry["survivor"] is False
    node = entry["stacks"]["node"]
    assert "no declared workspace members" in node["drop_reasons"]
    assert node["workspace_patterns"] == []
    assert node["workspace_members"] == []


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
        "sqlite_scan_skipped_members": 0,
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


# ---------------------------------------------------- F2 truncated tree


def test_truncated_tree_node_root_app_passes_via_fallback() -> None:
    # The recursive tree is truncated and never lists the lockfile;
    # the root listing (F2 fallback) shows it, so no false exclusion.
    repos = {"acme/rootapp": {
        "files": {
            "package.json": json.dumps({
                "scripts": {"start": "node index.js"},
                "dependencies": {"better-sqlite3": "^11.0.0"},
            }),
            "package-lock.json": "{}",
        },
        "tree_paths": ["package.json"],
        "truncated": True,
    }}
    entry = check(FakeFetcher(repos), "acme/rootapp", 2000, ["JavaScript"])
    assert entry["tree_truncated"] is True
    assert entry["survivor"] is True
    assert entry["stacks"]["node"]["app_path"] == "."


def test_truncated_tree_python_depth_one_passes_via_fallback() -> None:
    # The truncated tree shows none of the backend files; listings of
    # the root and of backend/ decide A3 (F2).
    repos = {"acme/dj": {
        "files": {
            "backend/manage.py": "",
            "backend/settings.py": "ENGINE = 'django.db.backends.sqlite3'",
            "backend/requirements.txt": "django\n",
        },
        "tree_paths": ["LICENSE"],
        "truncated": True,
    }}
    entry = check(FakeFetcher(repos), "acme/dj", 2000, ["Python"])
    assert entry["tree_truncated"] is True
    assert entry["survivor"] is True
    assert entry["stacks"]["python"]["app_path"] == "backend"


def test_truncated_tree_workspace_member_via_fallback() -> None:
    # The truncated tree lacks the member's package.json; the matched
    # workspace dir's listing decides (F1 + F2 together).
    repos = {"acme/mono": {
        "files": {
            "package.json": json.dumps({"workspaces": ["apps/*"]}),
            "package-lock.json": "{}",
            "apps/web/package.json": json.dumps({
                "scripts": {"start": "node server.js"},
                "dependencies": {"sqlite3": "^5.1.0"},
            }),
        },
        "tree_paths": ["package.json", "package-lock.json", "apps/web/index.js"],
        "truncated": True,
    }}
    entry = check(FakeFetcher(repos), "acme/mono", 2000, ["JavaScript"])
    assert entry["survivor"] is True
    assert entry["stacks"]["node"]["workspace_members"] == ["apps/web"]


def test_truncated_tree_listing_failure_is_harness_error() -> None:
    # Truncated tree and the fallback listings cannot be fetched: the
    # repo is undecidable -> harness error, never excluded (F2).
    repos = {"acme/big": {
        "files": {
            "package.json": json.dumps({
                "scripts": {"start": "node index.js"},
                "dependencies": {"sqlite3": "^5.1.0"},
            }),
            "package-lock.json": "{}",
        },
        "tree_paths": ["package.json"],
        "truncated": True,
        "fail_listings": True,
    }}
    entry = check(FakeFetcher(repos), "acme/big", 2000, ["JavaScript"])
    assert entry["survivor"] is False
    assert entry["excluded"] is None
    assert entry["error"] == "harness or environment: tree truncated"


# ------------------------------------------------------- F3 typed 404


def test_notfound_is_a_typed_harness_error() -> None:
    def raw_fetch(url: str, headers: dict[str, str]) -> tuple[int, dict[str, str], bytes]:
        return 404, {}, b"not found"

    fetcher = mod.Fetcher(sleep=NO_SLEEP, raw_fetch=raw_fetch)
    assert issubclass(mod.NotFound, mod.HarnessError)
    with pytest.raises(mod.NotFound):
        fetcher.get_bytes("https://api.github.com/repos/acme/x/contents/a?ref=s")
    assert mod.fetch_file_text(fetcher, "acme/x", "a", "s") is None


def test_error_text_containing_404_is_not_treated_as_absent() -> None:
    # A rate-limit failure on a path named 404.html: the old code's
    # `"404" in str(e)` match would have called the file "absent" (F3).
    def raw_fetch(url: str, headers: dict[str, str]) -> tuple[int, dict[str, str], bytes]:
        return 403, {"Retry-After": "0"}, b"rate limited"

    fetcher = mod.Fetcher(sleep=NO_SLEEP, raw_fetch=raw_fetch, max_retries=1)
    with pytest.raises(mod.HarnessError) as excinfo:
        mod.fetch_file_text(fetcher, "acme/x", "404.html", "s")
    assert not isinstance(excinfo.value, mod.NotFound)
    assert "404" in str(excinfo.value)


# ------------------------------------------- F4 search completeness


def test_search_pages_recorded_and_incomplete_flagged() -> None:
    pages = [{"items": [item("acme/a", 3000)], "total_count": 42,
              "incomplete_results": True}]
    fetcher = FakeFetcher(search_pages={"Python": pages})
    pool, counts, meta = mod.collect_pool(fetcher, "2026-10-15", sleep=NO_SLEEP)
    assert counts["Python"] == 1
    page = meta["Python"]["pages"][0]
    assert page == {"page": 1, "returned": 1, "total_count": 42,
                    "incomplete_results": True}
    assert meta["Python"]["incomplete_results"] is True
    assert meta["JavaScript"]["incomplete_results"] is False


# ------------------------------------------------- F5 no silent drops


def test_unparseable_root_package_json_typed_reason() -> None:
    repos = {"acme/broken": {"files": {
        "package.json": "{not json",
        "package-lock.json": "{}",
    }}}
    entry = check(FakeFetcher(repos), "acme/broken", 2000, ["JavaScript"])
    assert entry["survivor"] is False
    reasons = entry["stacks"]["node"]["drop_reasons"]
    assert "package.json unparseable at package.json" in reasons
    assert "no root package.json" not in reasons


def test_unparseable_member_package_json_typed_reason() -> None:
    repos = {"acme/mono": {"files": {
        "package.json": json.dumps({"workspaces": ["apps/*"]}),
        "package-lock.json": "{}",
        "apps/web/package.json": "{oops",
    }}}
    entry = check(FakeFetcher(repos), "acme/mono", 2000, ["JavaScript"])
    assert entry["survivor"] is False
    assert "package.json unparseable at apps/web/package.json" in (
        entry["stacks"]["node"]["drop_reasons"]
    )


def test_sqlite_scan_skipped_members_recorded_and_in_reason() -> None:
    big = "x" * (mod.TARBALL_MEMBER_CAP + 1)
    files = {
        "manage.py": "",
        "requirements.txt": "django\n",
        "assets/big.bin": big,
    }
    repos = {"acme/dj": {"files": files}}
    entry = check(FakeFetcher(repos), "acme/dj", 2000, ["Python"])
    assert entry["survivor"] is False
    py = entry["stacks"]["python"]
    assert py["sqlite_scan_skipped_members"] == 1
    assert any(
        "not found in repository" in r and "skipped over the 4 MB scan cap" in r
        for r in py["drop_reasons"]
    )


def test_sqlite_scan_finds_string_despite_skipped_member() -> None:
    big = "x" * (mod.TARBALL_MEMBER_CAP + 1)
    files = {
        "manage.py": "",
        "requirements.txt": "django\n",
        # The oversized member precedes the settings file in the
        # tarball, so the scan skips it before the string is found.
        "assets/big.bin": big,
        "settings.py": "ENGINE = 'django.db.backends.sqlite3'",
    }
    repos = {"acme/dj": {"files": files}}
    entry = check(FakeFetcher(repos), "acme/dj", 2000, ["Python"])
    assert entry["survivor"] is True
    assert entry["stacks"]["python"]["sqlite_scan_skipped_members"] == 1


def test_both_stacks_passed_flag() -> None:
    repos = {"acme/full": {"files": {
        "package.json": json.dumps({
            "scripts": {"start": "node index.js"},
            "dependencies": {"sqlite3": "^5.1.0"},
        }),
        "package-lock.json": "{}",
        "manage.py": "",
        "settings.py": "ENGINE = 'django.db.backends.sqlite3'",
        "requirements.txt": "django\n",
    }}}
    entry = check(FakeFetcher(repos), "acme/full", 2000, ["JavaScript", "Python"])
    assert entry["survivor"] is True
    assert entry["stacks"]["node"]["passed"] is True
    assert entry["stacks"]["python"]["passed"] is True
    assert entry["both_stacks_passed"] is True


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
    assert result["authenticated"] is False
    assert "search_incomplete" not in result
    survivor = result["survivors"][0]
    assert survivor["full_name"] == "acme/rootapp"
    assert survivor["order"] == 1
    excluded = [e for e in result["entries"] if e.get("excluded")]
    assert excluded[0]["full_name"] == "someone/express"
    assert (tmp_path / "regc-add2-raw.json").exists()
    assert (tmp_path / "regc-add2-survivors.json").exists()


# ------------------------------------------------------- F6 run summary


def test_run_records_error_count_and_errored_repos(tmp_path: Path) -> None:
    repos = {
        "acme/good": {"files": {
            "package.json": json.dumps({
                "scripts": {"start": "node index.js"},
                "dependencies": {"sqlite3": "^5.1.0"},
            }),
            "package-lock.json": "{}",
        }},
        "acme/flaky": {"files": {}, "fail_checks": True},
    }
    pages = [[item("acme/flaky", 9000), item("acme/good", 2000)]]
    fetcher = FakeFetcher(repos=repos, search_pages={"JavaScript": pages})
    result = mod.run(fetcher, "2026-10-15", tmp_path, sleep=NO_SLEEP)
    assert result["error_count"] == 1
    assert result["errored_repos"] == [{"full_name": "acme/flaky", "stars": 9000}]
    assert result["survivor_count"] == 1
    errored = [e for e in result["entries"] if "error" in e]
    assert errored[0]["full_name"] == "acme/flaky"
    assert errored[0]["error"].startswith("harness or environment:")
