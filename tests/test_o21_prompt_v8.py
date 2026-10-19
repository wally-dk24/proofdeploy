"""Prompt v8 (D242): the signed header, the template pins, the shown
sources and the skill v5 author copy -- on real golden bundles.

No model is called.
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

import o21_helpers as h
import pytest

from proofdeploy import model_client as mc
from proofdeploy.fixture import EXPECTED_SKILL_HASH

GOLDENS = ("node-express-sqlite", "python-flask-sqlite")

_SIGNED_SKILL_SHA = "75d52a774f505dafd0ccde84cf91c9a1b8c485b2e8465e106b78d6d43268310c"
_SIGNED_HEADER_SHA = "52ae0da36255c37a4a5037daed45a84343d287147ccf69d0a37d10de3cb30f96"
_SIGNED_REGS_SHA = "9af08ac8c5bf61bc207bdb031eb550a21109562a80d5a630ce9376bb6269ea66"


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


# ---------------------------------------------------------------------
# (a) header equality, against a real golden bundle
# ---------------------------------------------------------------------


@pytest.mark.parametrize("app", GOLDENS)
def test_rendered_prompt_header_equals_the_signed_header(tmp_path, app):
    g = h.golden_bundle(tmp_path, app)
    before = h.text_before_skill(g.prompt)
    assert before.rstrip() == h.header_text().rstrip()
    # and the prompt continues with the skill section, then the bundle
    assert g.prompt[len(before):].startswith("## Authoring skill\n")


def test_header_equality_holds_for_the_no_skill_arm_too(tmp_path):
    g = h.golden_bundle(tmp_path, "node-express-sqlite", no_skill=True)
    assert h.text_before_skill(g.prompt).rstrip() == h.header_text().rstrip()
    assert "## Authoring skill\n\n(none)" in g.prompt


# ---------------------------------------------------------------------
# the signed files and the pins
# ---------------------------------------------------------------------


def test_signed_docs_are_byte_identical_to_the_signed_hashes():
    assert _sha(h.SIGNED / "skill-v5-author.md") == _SIGNED_SKILL_SHA
    assert _sha(h.SIGNED / "prompt-v8-header.md") == _SIGNED_HEADER_SHA
    assert _sha(h.SIGNED / "o21-registrations-signed.md") == _SIGNED_REGS_SHA
    sums = (h.SIGNED / "SHA256SUMS").read_text().split("\n")
    listed = {ln.split()[1]: ln.split()[0] for ln in sums if ln.strip()}
    assert listed == {
        "skill-v5-author.md": _SIGNED_SKILL_SHA,
        "prompt-v8-header.md": _SIGNED_HEADER_SHA,
        "o21-registrations-signed.md": _SIGNED_REGS_SHA,
    }


def test_skill_author_copy_is_the_signed_file_and_the_pinned_hash():
    assert EXPECTED_SKILL_HASH == _SIGNED_SKILL_SHA
    assert h.SKILL_AUTHOR.read_bytes() == (h.SIGNED / "skill-v5-author.md").read_bytes()


def test_generator_writes_the_signed_author_copy_from_the_audited_source(tmp_path):
    """scripts/generate_author_skill.py on skill.md gives the signed bytes."""
    tree = tmp_path / "tree"
    (tree / "scripts").mkdir(parents=True)
    (tree / "docs/evaluation/skill").mkdir(parents=True)
    gen = h.REPO_ROOT / "scripts" / "generate_author_skill.py"
    (tree / "scripts" / "generate_author_skill.py").write_bytes(gen.read_bytes())
    src = h.REPO_ROOT / "docs/evaluation/skill/skill.md"
    (tree / "docs/evaluation/skill/skill.md").write_bytes(src.read_bytes())
    subprocess.run(
        [sys.executable, str(tree / "scripts" / "generate_author_skill.py")],
        check=True, capture_output=True,
    )
    out = tree / "docs/evaluation/skill/skill-author.md"
    assert out.read_bytes() == (h.SIGNED / "skill-v5-author.md").read_bytes()


def test_audited_source_tags_every_item():
    import re

    text = (h.REPO_ROOT / "docs/evaluation/skill/skill.md").read_text()
    lines = [ln for ln in text.split("\n") if re.match(r"^(R\d|\d+)\. ", ln)]
    assert len(lines) == 27  # R1-R4 and items 1-23
    tags = {}
    for ln in lines:
        m = re.search(r"`\[(PROC|DEV: T\d+)\]`$", ln.rstrip())
        assert m, ln[:60]  # every item carries exactly one provenance tag at its end
        tags[ln.split(".")[0]] = m.group(1)
    assert {k: v for k, v in tags.items() if v != "PROC"} == {
        "15": "DEV: T1", "16": "DEV: T2", "17": "DEV: T4",
    }


def test_template_v8_is_pinned_and_v7_is_untouched():
    assert mc.PROMPT_TEMPLATE_FILENAME == "author_prompt_v8.md"
    v7 = Path(mc.__file__).with_name("author_prompt_v7.md")
    assert _sha(v7) == "fa72a0e6c4a1fa9fb204986e08c8eef4b11fc13786f41389ecf2f42ea8555cf3"
    reg = (h.REPO_ROOT / "docs/registration/author-prompt-v8.md").read_text()
    assert mc.author_prompt_sha256() in reg
    assert mc.author_prompt_builder_sha256() in reg
    assert EXPECTED_SKILL_HASH in reg
    assert "registered by Master, D242" in reg.lower() or "Registered by Master, D242" in reg


# ---------------------------------------------------------------------
# R-METHOD item "shown sources are exactly the prompt's text"
# ---------------------------------------------------------------------


@pytest.mark.parametrize("app", GOLDENS)
def test_each_shown_source_occurs_verbatim_in_the_rendered_prompt(tmp_path, app):
    g = h.golden_bundle(tmp_path, app)
    assert "fixture" in g.sources
    touched = [p for p, s in g.sources.items() if s.kind == "touched"]
    assert touched
    for path, src in g.sources.items():
        assert src.shown_text in g.prompt, path
    # the touched file is shown with its changed line marked, and only that line
    src = g.sources[touched[0]]
    marked = [ln for ln in src.shown_text.split("\n") if ln.startswith("<<CHANGED>> ")]
    assert len(marked) == 1
    assert len(src.changed) == 1


def test_route_files_are_shown_sources_too(tmp_path):
    """A route file that is not touched is shown in full and is a source."""
    info = h.routes_repo(tmp_path)
    orch, unit = h.routes_orch(tmp_path, info)
    _bundle, _manifest, prompt, _d, _b, _t, _f, sources = orch._build_bundle(
        unit, unit_id="routes", rev_base=info["base"], rev_tip=info["bug"]
    )
    assert sources["app/urls.py"].kind == "route"
    assert sources["app/urls.py"].shown_text in prompt
    assert {p for p, s in sources.items() if s.kind == "touched"} == {"app/views.py"}
