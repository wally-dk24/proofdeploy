#!/usr/bin/env python3
"""REG-C step 1+2+3+4 executed exactly as registered in
docs/registration/wo6-selection-rule.md (main a73898a):
- Item 1: GitHub repo search API, queries for language:JavaScript,
  language:TypeScript, language:Python; stars:>=1000 archived:false
  fork:false pushed:>=(snapshot-90d); sort stars desc; first 100 per
  query; de-duplicated.
- Item 2: exclusions (named repos + bare-name owner/name matching, ci).
- Item 3: mechanical filters at default-branch HEAD (file checks only).
- Item 4: order survivors by stars desc, ties by owner/name.
"""
import json
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

SNAPSHOT_DATE = "2026-10-14"
PUSHED_SINCE = "2026-07-16"  # 2026-10-14 minus 90 days
OUT = Path.home() / "workspace" / "wo6-scratch" / "regc-raw"
OUT.mkdir(parents=True, exist_ok=True)

TOKEN = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True).stdout.strip()


def api(url: str) -> dict:
    req = urllib.request.Request(url)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "proofdeploy-regc-runner")
    if TOKEN:
        req.add_header("Authorization", f"Bearer {TOKEN}")
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read().decode())


def main() -> None:
    # ---- item 1: snapshot queries
    pool: dict[str, dict] = {}
    for lang in ("JavaScript", "TypeScript", "Python"):
        q = (
            f"language:{lang} stars:>=1000 archived:false fork:false "
            f"pushed:>={PUSHED_SINCE}"
        )
        url = (
            "https://api.github.com/search/repositories?q="
            + urllib.parse.quote(q)
            + "&sort=stars&order=desc&per_page=100&page=1"
        )
        data = api(url)
        (OUT / f"search-{lang}.json").write_text(json.dumps(data, indent=1))
        print(f"query language:{lang}: total_count={data['total_count']} "
              f"returned={len(data['items'])}")
        for it in data["items"]:
            fn = it["full_name"]
            if fn not in pool:
                pool[fn] = {
                    "full_name": fn,
                    "stars": it["stargazers_count"],
                    "default_branch": it["default_branch"],
                    "queries": [lang],
                }
            else:
                pool[fn]["queries"].append(lang)
        time.sleep(2)
    print(f"pool after dedup: {len(pool)}")

    # ---- item 2: exclusions
    named = {
        "vendure-ecommerce/vendure", "directus/directus", "tryghost/ghost",
        "nestjs/nest", "koajs/koa", "adonisjs/core", "pallets/flask",
        "fastapi/fastapi", "codeigniter4/codeigniter4", "fastify/fastify",
        "django/django", "lmcache/lmcache", "bottlepy/bottle", "honojs/hono",
    }
    bare = {
        "ghost", "directus", "vendure", "nestjs", "koa", "adonisjs", "flask",
        "fastapi", "codeigniter4", "fastify", "django", "lmcache", "bottle",
        "hono", "body-parser", "express", "n8n", "strapi", "payload",
        "django-oscar", "keystone", "uptime-kuma", "pretix", "nocodb",
        "datasette", "apostrophe", "wagtail",
    }

    def excluded(fn: str) -> str | None:
        low = fn.lower()
        if low in named:
            return "named-exclusion"
        owner, name = low.split("/", 1)
        if owner in bare or name in bare:
            return "bare-name-exclusion"
        return None

    survivors = []
    excl_log = []
    for fn, rec in pool.items():
        why = excluded(fn)
        if why:
            excl_log.append((fn, rec["stars"], why))
        else:
            survivors.append(rec)
    print(f"after exclusions: {len(survivors)} (excluded {len(excl_log)})")
    (OUT / "excluded.json").write_text(json.dumps(excl_log, indent=1))

    # ---- item 3: mechanical filters at HEAD (tree file checks first)
    NODE_LOCK = {"package-lock.json", "pnpm-lock.yaml", "yarn.lock"}
    NODE_MONOREPO_FILES = {"pnpm-workspace.yaml", "lerna.json", "nx.json", "turbo.json"}
    PY_LOCK = {"uv.lock", "poetry.lock", "pdm.lock", "requirements.txt", "pyproject.toml"}
    SQLITE_DEPS = {"sqlite3", "better-sqlite3", "@libsql/client"}

    results = []
    for rec in sorted(survivors, key=lambda r: -r["stars"]):
        fn = rec["full_name"]
        entry = dict(rec)
        try:
            tree = api(
                f"https://api.github.com/repos/{fn}/git/trees/"
                f"{rec['default_branch']}?recursive=1"
            )
        except Exception as e:  # noqa: BLE001 - record and move on
            entry["filter"] = f"TREE-ERROR {e}"
            results.append(entry)
            continue
        paths = {t["path"] for t in tree.get("tree", [])}
        root = {p for p in paths if "/" not in p}
        entry["tree_truncated"] = tree.get("truncated", False)
        langs = set(rec["queries"])
        verdicts = []
        if langs & {"JavaScript", "TypeScript"}:
            v = []
            if "package.json" not in root:
                v.append("no root package.json")
            if root & NODE_MONOREPO_FILES:
                v.append(f"monorepo file at root: {sorted(root & NODE_MONOREPO_FILES)}")
            if not (root & NODE_LOCK):
                v.append("no root lockfile")
            entry["node_tree_drops"] = v
            verdicts.append(("node", v))
        if "Python" in langs:
            v = []
            if "manage.py" not in root:
                v.append("no root manage.py")
            if not (root & PY_LOCK):
                v.append("no root python lock/manifest")
            entry["python_tree_drops"] = v
            verdicts.append(("python", v))
        entry["stage"] = "tree-checked"
        results.append(entry)
        time.sleep(0.2)
    (OUT / "stage1-tree.json").write_text(json.dumps(results, indent=1))
    cand = [r for r in results
            if any(not v for _, v in
                   ([("node", r.get("node_tree_drops"))] if "node_tree_drops" in r else [])
                   + ([("python", r.get("python_tree_drops"))] if "python_tree_drops" in r else []))]
    print(f"tree-stage survivors (at least one stack passes tree checks): {len(cand)}")
    for r in cand:
        print("  TREE-SURVIVOR", r["full_name"], r["stars"])


if __name__ == "__main__":
    main()
