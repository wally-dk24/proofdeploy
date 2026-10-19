"""Route-declaration selection for the author bundle (R-BUNDLE, D242).

The author sees, besides the files the diff touches, the app's route
declarations: the files that say which URLs the app serves. They are
selected MECHANICALLY from the snapshot of B (never by a model, never by
the diff's content), by the rules of R-BUNDLE items 1-3:

1. Which files: inside the unit contract's ``app.path`` subtree, not a
   TEST or DOC path (D169 classes), not under ``node_modules``,
   ``vendor``, ``dist``, ``build``, ``site-packages`` or ``.venv``, not
   a migration path (R-SEL 1); and a route declaration by the Python or
   Node patterns below.
2. Order and cap: longest common leading path (in segments) with any
   file B touches first, then by path; full texts are added up to
   ``max_route_bytes`` in total; the first file that would exceed the
   cap and every later one is left out and ``route_map_truncated`` is
   set.
3. A route file already shown as a touched file is named, not repeated
   ("shown above").

This module is pure: it reads the snapshot and returns the selection.
The prompt builder renders it and the bundle assembler records it in
the manifest; the answer-key check recomputes it and compares.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# --- path classes (D169 Q2, TEST and DOC only) -------------------------
# A copy of the TEST and DOC rules of scripts/wo6/regc_bug_select.py's
# classify_path (that script is not part of the package). A test pins
# the two to the same answers on a path corpus.
_TEST_SEGMENTS = {"test", "tests", "testing", "__tests__", "spec", "specs", "e2e"}
_TEST_SEGMENT_SUFFIXES = ("_test", "_tests", "_testing")
_DOC_SEGMENTS = {"doc", "docs"}
_DOC_EXTENSIONS = {".md", ".rst", ".adoc"}
_DOC_BASENAME_PREFIXES = ("README", "CHANGELOG", "LICENSE", "AUTHORS", "CONTRIBUTING")


def _extension(basename: str) -> str:
    if "." not in basename:
        return ""
    return "." + basename.rsplit(".", 1)[1].lower()


def is_test_path(path: str) -> bool:
    segments = path.split("/")
    basename = segments[-1]
    for seg in segments:
        low = seg.lower()
        if low in _TEST_SEGMENTS or low.endswith(_TEST_SEGMENT_SUFFIXES):
            return True
    if basename == "conftest.py":
        return True
    if basename.startswith("test_") and basename.endswith(".py"):
        return True
    if basename.endswith("_test.py"):
        return True
    parts = basename.split(".")
    return len(parts) >= 3 and parts[-2] in ("test", "spec")


def is_doc_path(path: str) -> bool:
    segments = path.split("/")
    basename = segments[-1]
    if any(seg in _DOC_SEGMENTS for seg in segments):
        return True
    if _extension(basename) in _DOC_EXTENSIONS:
        return True
    return basename.startswith(_DOC_BASENAME_PREFIXES)


# --- R-SEL 1: migration paths ------------------------------------------
_MIGRATION_SEGMENTS = {"migrations", "migration", "migrate", "alembic"}


def is_migration_path(path: str) -> bool:
    """R-SEL 1: any segment is migrations/migration/migrate/alembic
    (case-insensitive), or the file name contains ``migrat``."""
    segments = path.split("/")
    if any(seg.lower() in _MIGRATION_SEGMENTS for seg in segments):
        return True
    return "migrat" in segments[-1].lower()


# --- R-BUNDLE item 1 ----------------------------------------------------
_EXCLUDED_SEGMENTS = {"node_modules", "vendor", "dist", "build", "site-packages", ".venv"}

_PY_NAMES = {"urls.py", "routes.py", "routers.py", "routing.py"}
_PY_LINE_RES = (
    re.compile(r"^\s*@\w+\.(route|get|post|put|patch|delete|api_route)\(", re.MULTILINE),
    re.compile(r"^\s*\w+\s*=\s*(APIRouter|Blueprint)\(", re.MULTILINE),
)
_NODE_EXTS = {".js", ".mjs", ".cjs", ".ts", ".mts"}
_NODE_SUFFIXES = (".controller.ts", ".controller.js", ".resolver.ts", ".resolver.js")
_NODE_CALL_RE = re.compile(
    r"\b(app|router|server|fastify|api)\.(get|post|put|patch|delete|all|route)\(\s*['\"`/]"
)
_SCHEMA_EXTS = {".graphql", ".gql"}

# A file larger than this is not scanned for content patterns (only its
# name is considered). Far above the 32 KB the section can ever hold.
_MAX_SCAN_BYTES = 2_000_000


def _excluded(rel: str) -> bool:
    segments = rel.split("/")
    if any(seg.lower() in _EXCLUDED_SEGMENTS for seg in segments):
        return True
    if is_test_path(rel) or is_doc_path(rel):
        return True
    return is_migration_path(rel)


def _is_route_declaration(rel: str, text: str | None) -> bool:
    """R-BUNDLE item 1 for one candidate file (already in scope)."""
    base = rel.rsplit("/", 1)[-1]
    ext = _extension(base)
    low = base.lower()
    if ext == ".py":
        if base in _PY_NAMES:
            return True
        return text is not None and any(r.search(text) for r in _PY_LINE_RES)
    if ext in _NODE_EXTS:
        if "route" in low or "router" in low or "routing" in low:
            return True
        if low.endswith(_NODE_SUFFIXES):
            return True
        return text is not None and _NODE_CALL_RE.search(text) is not None
    return ext in _SCHEMA_EXTS


@dataclass(frozen=True)
class RouteEntry:
    """One selected route declaration file.

    ``kind`` is ``included`` (full text shown in the route section),
    ``shown_above`` (already a touched file shown in full; named only)
    or ``omitted`` (left out by the cap)."""

    path: str
    kind: str
    text: str | None = None


@dataclass
class RouteSelection:
    """The mechanical route-declaration selection for one bundle."""

    app_path: str
    entries: list[RouteEntry] = field(default_factory=list)
    truncated: bool = False

    @property
    def included(self) -> list[RouteEntry]:
        return [e for e in self.entries if e.kind == "included"]

    @property
    def shown_above(self) -> list[RouteEntry]:
        return [e for e in self.entries if e.kind == "shown_above"]

    @property
    def omitted(self) -> list[RouteEntry]:
        return [e for e in self.entries if e.kind == "omitted"]

    def manifest(self) -> dict[str, Any]:
        """The record of the selection (bundle manifest ``route_map``)."""
        return {
            "app_path": self.app_path,
            "route_map_truncated": self.truncated,
            "included": [e.path for e in self.included],
            "omitted": [e.path for e in self.omitted],
            "shown_above": [e.path for e in self.shown_above],
        }


def _common_prefix_len(a: list[str], b: list[str]) -> int:
    n = 0
    for x, y in zip(a, b, strict=False):
        if x != y:
            break
        n += 1
    return n


def normalize_app_path(app_path: str | None) -> str:
    """The contract's app.path as a snapshot-relative prefix ('.' = all)."""
    if not app_path:
        return "."
    p = app_path.strip().replace("\\", "/").strip("/")
    if p in ("", "."):
        return "."
    while p.startswith("./"):
        p = p[2:]
    return p or "."


def select_route_files(
    snapshot_dir: str | Path,
    *,
    app_path: str | None,
    touched: list[str],
    shown_touched: set[str],
    max_route_bytes: int,
) -> RouteSelection:
    """Select the route declaration files of the snapshot (R-BUNDLE).

    ``touched``: every path the diff touches (for the proximity order).
    ``shown_touched``: the touched paths whose full text the prompt
    actually shows (those a route file may be "shown above" of).
    Deterministic: same snapshot and inputs, same selection.
    """
    snap = Path(snapshot_dir)
    app = normalize_app_path(app_path)
    candidates: list[tuple[str, str | None]] = []
    for p in sorted(snap.rglob("*")):
        if not p.is_file() or p.is_symlink():
            continue
        rel = p.relative_to(snap).as_posix()
        if app != "." and not (rel == app or rel.startswith(app + "/")):
            continue
        if _excluded(rel):
            continue
        base = rel.rsplit("/", 1)[-1]
        ext = _extension(base)
        if ext not in _NODE_EXTS and ext not in _SCHEMA_EXTS and ext != ".py":
            continue
        try:
            data = p.read_bytes()
        except OSError:
            continue
        if b"\x00" in data:
            continue
        text: str | None
        if len(data) > _MAX_SCAN_BYTES:
            text = None
        else:
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                continue
        if _is_route_declaration(rel, text):
            # text None: too large to ever fit; it is still a route
            # declaration and is reported as omitted, never silently
            # dropped.
            candidates.append((rel, text))

    touched_segments = [t.split("/") for t in touched]

    def proximity(rel: str) -> int:
        segs = rel.split("/")
        return max((_common_prefix_len(segs, t) for t in touched_segments), default=0)

    candidates.sort(key=lambda c: (-proximity(c[0]), c[0]))

    sel = RouteSelection(app_path=app)
    total = 0
    cut = False
    for rel, text in candidates:
        if rel in shown_touched:
            sel.entries.append(RouteEntry(rel, "shown_above"))
            continue
        if cut:
            sel.entries.append(RouteEntry(rel, "omitted"))
            continue
        shown = text.rstrip() if text is not None else ""
        size = len(shown.encode("utf-8"))
        if text is None or total + size > max_route_bytes:
            cut = True
            sel.truncated = True
            sel.entries.append(RouteEntry(rel, "omitted"))
            continue
        total += size
        sel.entries.append(RouteEntry(rel, "included", shown))
    return sel
