# ruff: noqa: E501  (long embedded stub JSON / fixture lines)
"""R-BUNDLE (D242): mechanical route-declaration selection.

Synthetic Python and Node trees: scope (app.path), exclusions, the
name and content patterns, proximity order, the byte cap, "shown
above", the file-list guarantee, the manifest record and the leak and
answer-key checks over the new section.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from o21_helpers import routes_orch, routes_repo

from proofdeploy.model_client import build_author_prompt_full, route_selection_for
from proofdeploy.routemap import (
    is_doc_path,
    is_migration_path,
    is_test_path,
    select_route_files,
)

ROUTE_PY = "from django.urls import path\nurlpatterns = [path('a/', v)]\n"


def _tree(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return root


def _paths(sel, kind):
    return [e.path for e in sel.entries if e.kind == kind]


# ---------------------------------------------------------------------
# (g) Python tree
# ---------------------------------------------------------------------

PY_FILES = {
    # route declarations by name
    "backend/app/urls.py": ROUTE_PY,
    "backend/app/api/routes.py": "# routes\n",
    "backend/app/api/routers.py": "# routers\n",
    "backend/app/routing.py": "# routing\n",
    # by content
    "backend/app/main.py": "from flask import Flask\napp = Flask(__name__)\n@app.get('/x')\ndef x(): ...\n",
    "backend/app/svc.py": "    @bp.route('/y')\ndef y(): ...\n",
    "backend/app/mod.py": "r = APIRouter()\n",
    "backend/app/bp.py": "bp = Blueprint('b', __name__)\n",
    # not route declarations
    "backend/app/views.py": "def v(): ...\n",
    "backend/app/models.py": "# @app.get('/commented') is mid-line: x = '@app.get(1)'\nx = 1\n",
    "backend/app/notes.py": "def a(): return urls.py\n",
    # excluded classes and directories
    "backend/app/tests/urls.py": ROUTE_PY,
    "backend/app/test_routes.py": "@app.get('/t')\n",
    "backend/app/docs/urls.py": ROUTE_PY,
    "backend/app/vendor/lib/urls.py": ROUTE_PY,
    "backend/app/node_modules/x/routes.py": ROUTE_PY,
    "backend/app/dist/urls.py": ROUTE_PY,
    "backend/app/build/urls.py": ROUTE_PY,
    "backend/app/.venv/lib/site-packages/y/urls.py": ROUTE_PY,
    "backend/app/site-packages/urls.py": ROUTE_PY,
    "backend/app/migrations/urls.py": ROUTE_PY,
    "backend/app/migrate_urls.py": "@app.get('/m')\n",
    "backend/app/alembic/versions/routes.py": ROUTE_PY,
    # outside app.path
    "backend/other/urls.py": ROUTE_PY,
    "urls.py": ROUTE_PY,
}


def test_python_scope_exclusions_and_patterns(tmp_path):
    snap = _tree(tmp_path / "s", PY_FILES)
    sel = select_route_files(
        snap,
        app_path="backend/app",
        touched=["backend/app/views.py"],
        shown_touched=set(),
        max_route_bytes=32768,
    )
    assert sorted(_paths(sel, "included")) == [
        "backend/app/api/routers.py",
        "backend/app/api/routes.py",
        "backend/app/bp.py",
        "backend/app/main.py",
        "backend/app/mod.py",
        "backend/app/routing.py",
        "backend/app/svc.py",
        "backend/app/urls.py",
    ]
    assert not sel.truncated and not sel.omitted and not sel.shown_above


def test_whole_tree_when_no_app_path(tmp_path):
    snap = _tree(tmp_path / "s", PY_FILES)
    sel = select_route_files(
        snap, app_path=None, touched=[], shown_touched=set(), max_route_bytes=32768
    )
    assert "urls.py" in _paths(sel, "included")
    assert "backend/other/urls.py" in _paths(sel, "included")
    sel2 = select_route_files(
        snap, app_path=".", touched=[], shown_touched=set(), max_route_bytes=32768
    )
    assert _paths(sel2, "included") == _paths(sel, "included")


def test_order_is_proximity_then_path(tmp_path):
    snap = _tree(tmp_path / "s", PY_FILES)
    sel = select_route_files(
        snap,
        app_path="backend/app",
        touched=["backend/app/api/handlers.py"],
        shown_touched=set(),
        max_route_bytes=32768,
    )
    # api/* shares 3 leading segments with the touched file; the rest share 2.
    assert _paths(sel, "included") == [
        "backend/app/api/routers.py",
        "backend/app/api/routes.py",
        "backend/app/bp.py",
        "backend/app/main.py",
        "backend/app/mod.py",
        "backend/app/routing.py",
        "backend/app/svc.py",
        "backend/app/urls.py",
    ]
    # Another touched file changes the order: the proximity is to ANY touched file.
    sel = select_route_files(
        snap,
        app_path="backend/app",
        touched=["backend/app/mod.py"],
        shown_touched=set(),
        max_route_bytes=32768,
    )
    assert _paths(sel, "included")[0] == "backend/app/mod.py"  # the touched file itself: 3 segments
    sel = select_route_files(
        snap,
        app_path="backend/app",
        touched=["backend/app/api/handlers.py", "backend/app/mod.py"],
        shown_touched=set(),
        max_route_bytes=32768,
    )
    assert _paths(sel, "included") == [
        "backend/app/api/routers.py",  # 3 segments with handlers.py
        "backend/app/api/routes.py",
        "backend/app/mod.py",  # 3 segments with itself; ties by path
        "backend/app/bp.py",  # the rest share 2
        "backend/app/main.py",
        "backend/app/routing.py",
        "backend/app/svc.py",
        "backend/app/urls.py",
    ]


def test_cap_leaves_out_the_first_overflowing_file_and_every_later_one(tmp_path):
    files = {
        "app/a_urls.py": "x" * 10 + "\n@app.get('/a')\n",  # 26 bytes shown
        "app/b_urls.py": "y" * 40 + "\n@app.get('/b')\n",  # does not fit after a
        "app/c_urls.py": "@app.get('/c')\n",  # small: would fit, but follows the overflow
        "app/z/urls.py": "@app.get('/z')\n",
    }
    snap = _tree(tmp_path / "s", files)
    kw = dict(app_path="app", touched=["app/a_urls.py"], shown_touched=set())
    sel = select_route_files(snap, max_route_bytes=60, **kw)
    assert _paths(sel, "included") == ["app/a_urls.py"]
    assert _paths(sel, "omitted") == ["app/b_urls.py", "app/c_urls.py", "app/z/urls.py"]
    assert sel.truncated
    # the cap is a total: everything fits when it is high enough
    big = select_route_files(snap, max_route_bytes=10_000, **kw)
    assert not big.truncated and not big.omitted
    # exactly at the limit still fits
    one = select_route_files(
        snap, app_path="app/z", touched=[], shown_touched=set(), max_route_bytes=len("@app.get('/z')")
    )
    assert _paths(one, "included") == ["app/z/urls.py"] and not one.truncated


def test_shown_above_is_named_not_repeated_and_free_of_the_cap(tmp_path):
    snap = _tree(tmp_path / "s", PY_FILES)
    sel = select_route_files(
        snap,
        app_path="backend/app",
        touched=["backend/app/urls.py"],
        shown_touched={"backend/app/urls.py"},
        max_route_bytes=32768,
    )
    assert _paths(sel, "shown_above") == ["backend/app/urls.py"]
    assert "backend/app/urls.py" not in _paths(sel, "included")
    # A touched route file whose text is NOT shown (cap, binary, too large) is a normal entry.
    sel = select_route_files(
        snap,
        app_path="backend/app",
        touched=["backend/app/urls.py"],
        shown_touched=set(),
        max_route_bytes=32768,
    )
    assert "backend/app/urls.py" in _paths(sel, "included")
    assert not sel.shown_above


# ---------------------------------------------------------------------
# (g) Node tree
# ---------------------------------------------------------------------

NODE_FILES = {
    # by name
    "web/src/router.ts": "export const r = 1;\n",
    "web/src/users.routes.js": "module.exports = {};\n",
    "web/src/Routing.mjs": "export {};\n",
    "web/src/app.controller.ts": "export class C {}\n",
    "web/src/users.resolver.js": "exports.r = 1;\n",
    "web/src/schema.graphql": "type Query { a: Int }\n",
    "web/src/types.gql": "type T { a: Int }\n",
    # by content
    "web/src/server.cjs": "const app = require('x');\napp.get('/health', h);\n",
    "web/src/api.ts": "api.post(\"/items\", h);\n",
    "web/src/f.mts": "fastify.route({ url: '/f' });\nfastify.all(`/x`, h);\n",
    "web/src/r2.js": "router.delete(\n  '/d', h);\n",
    "web/src/s.js": "server.put(/regex/, h);\n",
    # not route declarations
    "web/src/util.ts": "const f = app.get(x);\nexport {};\n",
    "web/src/model.js": "obj.get('k');\n",
    "web/src/page.tsx": "app.get('/x', h);\n",  # .tsx is not a Node extension of the rule
    "web/src/readme.md": "app.get('/x', h)\n",
    # excluded
    "web/src/router.test.ts": "app.get('/t', h);\n",
    "web/src/__tests__/api.js": "app.get('/t', h);\n",
    "web/src/spec/routes.js": "app.get('/t', h);\n",
    "web/node_modules/express/lib/router.js": "router.get('/x');\n",
    "web/dist/routes.js": "app.get('/x', h);\n",
    "web/build/routes.js": "app.get('/x', h);\n",
    "web/vendor/routes.js": "app.get('/x', h);\n",
    "web/src/migrations/routes.js": "app.get('/x', h);\n",
    "web/docs/router.js": "app.get('/x', h);\n",
    # outside app.path
    "other/router.ts": "export {};\n",
}


def test_node_scope_exclusions_and_patterns(tmp_path):
    snap = _tree(tmp_path / "s", NODE_FILES)
    sel = select_route_files(
        snap,
        app_path="web",
        touched=["web/src/util.ts"],
        shown_touched=set(),
        max_route_bytes=32768,
    )
    assert sorted(_paths(sel, "included")) == [
        "web/src/Routing.mjs",
        "web/src/api.ts",
        "web/src/app.controller.ts",
        "web/src/f.mts",
        "web/src/r2.js",
        "web/src/router.ts",
        "web/src/s.js",
        "web/src/schema.graphql",
        "web/src/server.cjs",
        "web/src/types.gql",
        "web/src/users.resolver.js",
        "web/src/users.routes.js",
    ]


def test_selection_is_deterministic(tmp_path):
    snap = _tree(tmp_path / "s", NODE_FILES)
    kw = dict(
        app_path="web", touched=["web/src/util.ts"], shown_touched=set(), max_route_bytes=32768
    )
    assert select_route_files(snap, **kw) == select_route_files(snap, **kw)


# ---------------------------------------------------------------------
# the path classes are the D169 ones
# ---------------------------------------------------------------------


def _load_classify():
    script = Path(__file__).resolve().parents[1] / "scripts" / "wo6" / "regc_bug_select.py"
    spec = importlib.util.spec_from_file_location("regc_bug_select_for_routes", script)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclasses in the script need the module registered
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.modules.pop(spec.name, None)
    return mod.classify_path


_CORPUS = [
    "src/app/urls.py", "tests/urls.py", "src/tests/routes.py", "src/test_urls.py",
    "src/urls_test.py", "src/conftest.py", "src/a.test.ts", "src/a.spec.js", "src/e2e/r.js",
    "src/__tests__/r.js", "docs/urls.py", "src/doc/routes.py", "README.md", "src/guide.rst",
    "CHANGELOG.txt", "src/CONTRIBUTING.py", "src/router.ts", "src/models_testing/r.py",
    "src/unit_tests/r.py", "Spec/r.js", "src/TESTS/r.js",
]


@pytest.mark.parametrize("path", _CORPUS)
def test_test_and_doc_classes_match_the_d169_classifier(path):
    classify = _load_classify()
    klass = classify(path)
    assert is_test_path(path) == (klass == "TEST")
    # classify_path checks TEST first, so DOC is only meaningful when not TEST
    if klass != "TEST":
        assert is_doc_path(path) == (klass == "DOC")


def test_migration_paths_follow_r_sel_1():
    for p in ("a/migrations/x.py", "a/Migration/x.py", "a/migrate/x.py", "alembic/env.py",
              "a/ALEMBIC/x.py", "a/0001_migrate_users.py", "a/Migrations.py"):
        assert is_migration_path(p), p
    for p in ("a/mig/x.py", "a/migr/x.py", "a/urls.py", "a/migrants/x.py"):
        assert is_migration_path(p) == ("migrat" in p.rsplit("/", 1)[-1].lower()), p


# ---------------------------------------------------------------------
# the prompt section, the file list and the flags
# ---------------------------------------------------------------------

_DIFF = (
    "diff --git a/app/views.py b/app/views.py\n--- a/app/views.py\n+++ b/app/views.py\n"
    "@@ -1,1 +1,1 @@\n-v = 1\n+v = 2\n"
)


def _snap_with_many_files(tmp_path, extra: dict[str, str]) -> Path:
    files = {"app/views.py": "v = 2\n", **extra}
    for i in range(250):  # past the 200-file listing cap
        files[f"aaa/file{i:03d}.txt"] = "x\n"
    return _tree(tmp_path / "s", files)


def test_included_route_files_are_listed_whatever_the_cap(tmp_path):
    snap = _snap_with_many_files(tmp_path, {"zzz/urls.py": ROUTE_PY, "app/urls.py": ROUTE_PY})
    built = build_author_prompt_full(
        skill_text=None,
        fixture_description="Repository: demo",
        diff_text=_DIFF,
        snapshot_dir=snap,
        app_path=".",
    )
    listing = built.prompt.split("## Snapshot file list")[1].split("## Touched file contents")[0]
    assert built.truncation_flags["listed_files_truncated"] is True
    assert "zzz/urls.py" in listing.split("\n")  # sorts after the 200th file, still listed
    assert "app/urls.py" in listing.split("\n")
    # the "more files" marker counts what is really not listed: 253 files,
    # 200 listed by the cap plus the two route files beyond it
    n_listed = len([ln for ln in listing.split("\n") if ln.startswith(("aaa/", "app/", "zzz/"))])
    assert n_listed == 202
    assert f"... ({253 - n_listed} more files)" in listing


def test_route_section_in_the_prompt_and_flags(tmp_path):
    snap = _tree(
        tmp_path / "s",
        {
            "app/views.py": "v = 2\n",
            "app/urls.py": ROUTE_PY,
            "app/api/routes.py": "# r\n" + "x" * 100,
        },
    )
    built = build_author_prompt_full(
        skill_text=None,
        fixture_description="Repository: demo",
        diff_text=_DIFF,
        snapshot_dir=snap,
        app_path="app",
    )
    head = "## Route declarations (selected mechanically; may be incomplete)"
    section = built.prompt.split(head)[1].split("Write your probes now")[0]
    assert "### FILE: app/urls.py" in section and ROUTE_PY.rstrip() in section
    assert "### FILE: app/api/routes.py" in section
    assert built.truncation_flags["route_map_truncated"] is False
    assert built.sources["app/urls.py"].kind == "route"
    # the section comes after the touched files
    assert built.prompt.index(head) > built.prompt.index("## Touched file contents")
    # a tiny cap truncates and says so in the prompt
    from proofdeploy import model_client as mc

    tpl = Path(mc.__file__).with_name("author_prompt_v8.md").read_text()
    small = tmp_path / "tpl.md"
    small.write_text(tpl.replace("`max_route_bytes`: 32768", "`max_route_bytes`: 40"))
    built = build_author_prompt_full(
        skill_text=None,
        fixture_description="Repository: demo",
        diff_text=_DIFF,
        snapshot_dir=snap,
        app_path="app",
        template_path=small,
    )
    assert built.truncation_flags["route_map_truncated"] is True
    assert "more route declaration files not shown: cap is 40 bytes" in built.prompt
    assert "app/api/routes.py" not in built.sources


def test_none_found_text(tmp_path):
    snap = _tree(tmp_path / "s", {"app/views.py": "v = 2\n"})
    built = build_author_prompt_full(
        skill_text=None,
        fixture_description="Repository: demo",
        diff_text=_DIFF,
        snapshot_dir=snap,
        app_path="app",
    )
    assert "(none found)" in built.prompt


def test_shown_above_in_the_prompt(tmp_path):
    snap = _tree(tmp_path / "s", {"app/views.py": "v = 2\n", "app/urls.py": ROUTE_PY})
    diff = _DIFF.replace("views.py", "urls.py")
    built = build_author_prompt_full(
        skill_text=None,
        fixture_description="Repository: demo",
        diff_text=diff,
        snapshot_dir=snap,
        app_path="app",
    )
    section = built.prompt.split("## Route declarations")[1]
    assert "### FILE: app/urls.py\n\n(shown above)" in section
    assert section.count("urlpatterns") == 0  # not repeated


# ---------------------------------------------------------------------
# the manifest, the leak check and the answer-key check cover the section
# ---------------------------------------------------------------------

def test_manifest_records_the_route_map(tmp_path):
    info = routes_repo(tmp_path)
    orch, unit = routes_orch(tmp_path, info)
    bundle, manifest, prompt, *_ = orch._build_bundle(
        unit, unit_id="routes", rev_base=info["base"], rev_tip=info["bug"]
    )
    assert manifest["route_map"] == {
        "app_path": "app",
        "route_map_truncated": False,
        "included": ["app/urls.py"],  # not outside/urls.py: outside app.path
        "omitted": [],
        "shown_above": [],
    }
    assert "### FILE: app/urls.py" in prompt
    assert "outside/urls.py" in prompt.split("## Snapshot file list")[1]  # listed...
    assert "outside = 1" not in prompt  # ...but not shown


def test_leak_check_covers_the_route_section(tmp_path):
    """A secret in a route file reaches the author only through the new
    section; the existing leak check must catch it there."""
    from proofdeploy.leakcheck import SecretLeakError

    info = routes_repo(tmp_path, "# route file\nTOKEN = 'super-secret-env-value-123'\n")
    orch, unit = routes_orch(tmp_path, info)
    with pytest.raises(SecretLeakError, match="author prompt"):
        orch._build_bundle(unit, unit_id="routes", rev_base=info["base"], rev_tip=info["bug"])


def test_answer_key_check_covers_the_route_section(tmp_path):
    from proofdeploy.leakcheck import AnswerKeyLeakError

    info = routes_repo(tmp_path)
    orch, unit = routes_orch(tmp_path, info)
    bundle, manifest, prompt, _d, base, tip, _f, _s = orch._build_bundle(
        unit, unit_id="routes", rev_base=info["base"], rev_tip=info["bug"]
    )
    # untouched: passes
    orch._check_answer_key(bundle, prompt, base, tip, fix_rev=info["fix"])
    # 1. the route text in the prompt is not what the verified bundle gives
    tampered = prompt.replace("urlpatterns", "urlpatternz")
    assert tampered != prompt
    with pytest.raises(AnswerKeyLeakError, match="not built from the verified"):
        orch._check_answer_key(bundle, tampered, base, tip, fix_rev=info["fix"])
    # 2. the manifest's route_map disagrees with the selection from B
    import json

    m = json.loads(bundle.manifest.read_text())
    m["route_map"]["included"] = []
    bundle.manifest.write_text(json.dumps(m))
    with pytest.raises(AnswerKeyLeakError, match="route_map"):
        orch._check_answer_key(bundle, prompt, base, tip, fix_rev=info["fix"])
    m["route_map"]["included"] = ["app/urls.py"]
    bundle.manifest.write_text(json.dumps(m))
    # 3. the route file in the bundle's snapshot is not B's
    (bundle.snapshot / "app" / "urls.py").write_text("# changed\n")
    with pytest.raises(AnswerKeyLeakError, match="!= B version"):
        orch._check_answer_key(bundle, prompt, base, tip, fix_rev=info["fix"])


def test_route_selection_for_matches_what_the_builder_shows(tmp_path):
    snap = _tree(tmp_path / "s", {"app/views.py": "v = 2\n", "app/urls.py": ROUTE_PY})
    sel = route_selection_for(diff_text=_DIFF, snapshot_dir=snap, app_path="app")
    built = build_author_prompt_full(
        skill_text=None,
        fixture_description="Repository: demo",
        diff_text=_DIFF,
        snapshot_dir=snap,
        app_path="app",
    )
    assert sel == built.routes
