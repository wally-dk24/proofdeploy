"""Lean: classify changed paths up front (D169 classes, R-SEL item 1).

``classify_path`` and ``is_migration_path`` reproduce, rule for rule, the
registered functions of ``scripts/wo6/regc_bug_select.py`` (D169 Q2 file
classes TEST, DOC, OTHER, SOURCE; R-SEL item 1, signed D242). A parity test
runs both over a fixed list of paths. The lean path uses them to type a
frontend-only (or docs/tests/locale-only) change before any sandbox is
provisioned: nothing a backend HTTP request could observe.

``is_backend_source(path, language)`` is true for a SOURCE-class,
non-migration path whose extension belongs to the app language. Only
``python`` is defined; every other language returns ``None`` ("not defined
yet"), which callers treat as "proceed as before".
"""

from __future__ import annotations

from collections.abc import Iterable

_TEST_SEGMENTS = {"test", "tests", "testing", "__tests__", "spec", "specs", "e2e"}
_TEST_SEGMENT_SUFFIXES = ("_test", "_tests", "_testing")
_DOC_SEGMENTS = {"doc", "docs"}
_DOC_EXTENSIONS = {".md", ".rst", ".adoc"}
_DOC_BASENAME_PREFIXES = ("README", "CHANGELOG", "LICENSE", "AUTHORS", "CONTRIBUTING")
_TRANSLATION_EXTENSIONS = {".po", ".pot", ".mo", ".xlf", ".xliff"}
_TRANSLATION_SEGMENTS = {"locale", "locales", "i18n"}
_LOCK_BASENAMES = {"pyproject.toml", "uv.lock", "poetry.lock", "package.json", "pnpm-lock.yaml"}
SOURCE_EXTENSIONS = {
    ".py", ".pyi", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx",
    ".vue", ".svelte", ".html", ".htm", ".css", ".scss", ".sass",
    ".less", ".go", ".java", ".kt", ".rb", ".php", ".rs", ".cs",
}  # fmt: skip

CLASSES = ("SOURCE", "TEST", "DOC", "OTHER")

# Backend source extensions per app language; a language not listed is "not defined yet".
BACKEND_EXTENSIONS: dict[str, frozenset[str]] = {"python": frozenset({".py"})}


def _extension(basename: str) -> str:
    """The basename's final extension, dot included, lowercased; "" without a dot."""
    if "." not in basename:
        return ""
    return "." + basename.rsplit(".", 1)[1].lower()


def _is_test_path(segments: list[str], basename: str) -> bool:
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


def _is_doc_path(segments: list[str], basename: str) -> bool:
    if any(seg in _DOC_SEGMENTS for seg in segments):
        return True
    if _extension(basename) in _DOC_EXTENSIONS:
        return True
    return basename.startswith(_DOC_BASENAME_PREFIXES)


def _is_other_path(segments: list[str], basename: str) -> bool:
    ext = _extension(basename)
    if ext in _TRANSLATION_EXTENSIONS:
        return True
    if any(seg in _TRANSLATION_SEGMENTS for seg in segments):
        return True
    if (
        (basename.startswith("requirements") and basename.endswith(".txt"))
        or basename in _LOCK_BASENAMES
        or basename.startswith("Pipfile")
        or (basename.endswith("-lock.json") and len(basename) > len("-lock.json"))
        or basename.endswith(".lock")
    ):
        return True
    if (
        segments[0] == ".github"
        or basename.startswith("Dockerfile")
        or basename.startswith("docker-compose")
        or any(seg == "docker" for seg in segments)
    ):
        return True
    if basename.endswith(".txt"):
        return True
    return ext not in SOURCE_EXTENSIONS


def classify_path(path: str) -> str:
    """D169 Q2: exactly one of TEST, DOC, OTHER, SOURCE per path."""
    segments = path.split("/")
    basename = segments[-1]
    if _is_test_path(segments, basename):
        return "TEST"
    if _is_doc_path(segments, basename):
        return "DOC"
    if _is_other_path(segments, basename):
        return "OTHER"
    return "SOURCE"


MIGRATION_SEGMENTS = frozenset({"migrations", "migration", "migrate", "alembic"})
MIGRATION_NAME_SUBSTRING = "migrat"


def is_migration_path(path: str) -> bool:
    """R-SEL item 1 (signed D242): a migration path (case-insensitive segment or name rule)."""
    segments = path.lower().split("/")
    if any(seg in MIGRATION_SEGMENTS for seg in segments):
        return True
    return MIGRATION_NAME_SUBSTRING in segments[-1]


def is_backend_source(path: str, language: str) -> bool | None:
    """Is ``path`` backend source for ``language``? ``None`` when the language is not defined."""
    exts = BACKEND_EXTENSIONS.get(language)
    if exts is None:
        return None
    if classify_path(path) != "SOURCE" or is_migration_path(path):
        return False
    return _extension(path.split("/")[-1]) in exts


def paths_by_class(paths: Iterable[str]) -> dict[str, int]:
    """Counts per class (always all four keys) for the unit's changed paths."""
    counts = dict.fromkeys(CLASSES, 0)
    for p in paths:
        counts[classify_path(p)] += 1
    return counts
