#!/usr/bin/env python3
"""REG-C item 3, content level, on stage-1 tree survivors:
- Node: root package.json must have scripts.start, no workspaces field,
  and one of sqlite3 / better-sqlite3 / @libsql/client in dependencies.
- Python: the string django.db.backends.sqlite3 must appear somewhere in
  the repo (mechanical grep of the HEAD tarball; file check only).
Item 4: final order = stars desc, ties by owner/name.
"""
import io
import json
import subprocess
import tarfile
import urllib.request
from pathlib import Path

OUT = Path.home() / "workspace" / "wo6-scratch" / "regc-raw"
TOKEN = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True).stdout.strip()
SQLITE_DEPS = {"sqlite3", "better-sqlite3", "@libsql/client"}


def get(url: str, accept: str = "application/vnd.github+json") -> bytes:
    req = urllib.request.Request(url)
    req.add_header("Accept", accept)
    req.add_header("User-Agent", "proofdeploy-regc-runner")
    if TOKEN:
        req.add_header("Authorization", f"Bearer {TOKEN}")
    with urllib.request.urlopen(req) as r:
        return r.read()


def main() -> None:
    results = json.loads((OUT / "stage1-tree.json").read_text())
    final = []
    for r in results:
        fn = r["full_name"]
        passes = []
        if "node_tree_drops" in r and not r["node_tree_drops"]:
            try:
                pkg = json.loads(get(
                    f"https://api.github.com/repos/{fn}/contents/package.json"
                    f"?ref={r['default_branch']}", "application/vnd.github.raw"))
                drops = []
                if "start" not in (pkg.get("scripts") or {}):
                    drops.append("no scripts.start")
                if "workspaces" in pkg:
                    drops.append("workspaces field present")
                deps = set((pkg.get("dependencies") or {}).keys())
                if not (deps & SQLITE_DEPS):
                    drops.append("no sqlite dep in dependencies")
                r["node_content_drops"] = drops
                if not drops:
                    passes.append("node")
            except Exception as e:  # noqa: BLE001
                r["node_content_drops"] = [f"ERROR {e}"]
        if "python_tree_drops" in r and not r["python_tree_drops"]:
            try:
                blob = get(f"https://api.github.com/repos/{fn}/tarball/{r['default_branch']}")
                found = False
                with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tf:
                    for m in tf.getmembers():
                        if m.isfile() and m.size < 2_000_000:
                            f = tf.extractfile(m)
                            if f and b"django.db.backends.sqlite3" in f.read():
                                found = True
                                break
                r["python_sqlite_string"] = found
                if found:
                    passes.append("python")
            except Exception as e:  # noqa: BLE001
                r["python_sqlite_string"] = f"ERROR {e}"
        r["passes"] = passes
        if passes:
            final.append(r)
    final.sort(key=lambda r: (-r["stars"], r["full_name"].lower()))
    (OUT / "stage2-final.json").write_text(json.dumps(final, indent=1))
    (OUT / "stage2-all.json").write_text(json.dumps(results, indent=1))
    print(f"FINAL SURVIVORS: {len(final)}")
    for r in final:
        print(f"{r['full_name']}\t{r['stars']}\t{','.join(r['passes'])}"
              f"\tqueries={','.join(r['queries'])}")


if __name__ == "__main__":
    main()
