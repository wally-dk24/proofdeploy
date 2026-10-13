"""The author-skill generator's leak check must raise even under ``python -O``.

``scripts/generate_author_skill.py`` strips evaluation provenance from the
audited skill source before the author copy is written. Its final leak check
used to be an ``assert``, which ``python -O`` strips: the check would
silently pass and tagged content would reach the registered file. These
tests run the real script in a subprocess, with and without ``-O``, against
poisoned and clean sources in a throwaway tree.
"""

import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "generate_author_skill.py"

POISONED = "# Skill\n\nThis example is from the TRAIN dev set and must never ship.\n"
CLEAN = "# Skill\n\nEach item teaches a probing technique. Study the pattern.\n"


def _run_generator(
    tmp_path: Path, source: str, *flags: str
) -> subprocess.CompletedProcess[str]:
    tree = tmp_path / "tree"
    script_dir = tree / "scripts"
    skill_dir = tree / "docs" / "evaluation" / "skill"
    script_dir.mkdir(parents=True)
    skill_dir.mkdir(parents=True)
    shutil.copy(SCRIPT, script_dir / "generate_author_skill.py")
    (skill_dir / "skill.md").write_text(source)
    return subprocess.run(
        [sys.executable, *flags, str(script_dir / "generate_author_skill.py")],
        capture_output=True,
        text=True,
        timeout=60,
    )


def _dst(tmp_path: Path) -> Path:
    return tmp_path / "tree" / "docs" / "evaluation" / "skill" / "skill-author.md"


def test_leak_check_raises_under_optimized_python(tmp_path: Path) -> None:
    result = _run_generator(tmp_path, POISONED, "-O")
    assert result.returncode != 0
    assert "RuntimeError" in result.stderr
    assert "Leak:" in result.stderr
    assert not _dst(tmp_path).exists()


def test_leak_check_raises_without_optimization(tmp_path: Path) -> None:
    result = _run_generator(tmp_path, POISONED)
    assert result.returncode != 0
    assert "RuntimeError" in result.stderr
    assert "Leak:" in result.stderr
    assert not _dst(tmp_path).exists()


def test_clean_source_generates_under_optimized_python(tmp_path: Path) -> None:
    result = _run_generator(tmp_path, CLEAN, "-O")
    assert result.returncode == 0, result.stderr
    assert _dst(tmp_path).exists()
