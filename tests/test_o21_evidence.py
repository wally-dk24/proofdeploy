# ruff: noqa: E501  (long embedded stub JSON / fixture lines)
"""R-METHOD (D242): the mechanical evidence check, the changed-line
markers and the probe-schema exemption for `evidence`.

Pure unit tests: no model, no app, no network.
"""

from __future__ import annotations

import json

import pytest

from proofdeploy.evidence import (
    CHANGED_MARKER,
    ShownSource,
    analyse_answer,
    changed_lines_by_file,
    check_assertion,
    correction_message,
    final_probeset,
)
from proofdeploy.model_client import build_author_prompt_full
from proofdeploy.probe import ProbeRejected, validate_probe

# ---------------------------------------------------------------------
# (b) changed-line markers: exact lines from the diff, never by string.
# ---------------------------------------------------------------------

# `done = false` appears on line 2 (untouched) and on line 5 (added).
_APP = "a = 1\ndone = false\nb = 2\nc = 3\ndone = false\nd = 4\n"
_DIFF = (
    "diff --git a/app.py b/app.py\n"
    "index 111..222 100644\n"
    "--- a/app.py\n"
    "+++ b/app.py\n"
    "@@ -3,3 +3,4 @@ b = 2\n"
    " b = 2\n"
    " c = 3\n"
    "+done = false\n"
    " d = 4\n"
)


def test_changed_lines_come_from_the_hunk_not_from_text():
    assert changed_lines_by_file(_DIFF) == {"app.py": frozenset({5})}


def test_changed_markers_on_exact_lines(tmp_path):
    """Only line 5 carries the marker, although line 2 has the same text."""
    snap = tmp_path / "snap"
    snap.mkdir()
    (snap / "app.py").write_text(_APP)
    built = build_author_prompt_full(
        skill_text=None,
        fixture_description="Repository: demo",
        diff_text=_DIFF,
        snapshot_dir=snap,
    )
    shown = built.sources["app.py"].shown_text.split("\n")
    assert shown == [
        "a = 1",
        "done = false",  # same text as the added line, but untouched
        "b = 2",
        "c = 3",
        CHANGED_MARKER + "done = false",
        "d = 4",
    ]
    assert CHANGED_MARKER + "done = false" in built.prompt
    assert built.prompt.count(CHANGED_MARKER + "done = false") == 1


def test_changed_lines_multi_hunk_new_file_and_deletions():
    diff = (
        "diff --git a/x.js b/x.js\n--- a/x.js\n+++ b/x.js\n"
        "@@ -1,4 +1,4 @@\n l1\n-old2\n+new2\n l3\n l4\n"
        "@@ -10,2 +10,3 @@\n l10\n+added11\n l11\n"
        "diff --git a/new.py b/new.py\nnew file mode 100644\n--- /dev/null\n+++ b/new.py\n"
        "@@ -0,0 +1,2 @@\n+p1\n+p2\n"
        "\\ No newline at end of file\n"
        "diff --git a/gone.py b/gone.py\n--- a/gone.py\n+++ /dev/null\n"
        "@@ -1,2 +0,0 @@\n-q1\n-q2\n"
    )
    got = changed_lines_by_file(diff)
    assert got["x.js"] == frozenset({2, 11})  # the deletion marks nothing
    assert got["new.py"] == frozenset({1, 2})
    assert "gone.py" not in got


def test_added_line_that_looks_like_a_header_is_still_a_line():
    """A '+++ ' inside a hunk is an added line, found by the hunk's counts."""
    diff = (
        "diff --git a/n.txt b/n.txt\n--- a/n.txt\n+++ b/n.txt\n"
        "@@ -1,1 +1,2 @@\n keep\n+++ not a header\n"
    )
    assert changed_lines_by_file(diff) == {"n.txt": frozenset({2})}


# ---------------------------------------------------------------------
# (c) every rejection reason.
# ---------------------------------------------------------------------

_FIXTURE = (
    "Repository: demo\n"
    "Seed data: note 1 is done=false and note 2 is done=true\n"
    "Seed user: note 1 has done false; the other note: done true\n"
    "Token scope: a regular user\n"
)
_SRC = (
    "def get(note):\n"
    "    # docs: a note starts with done == False\n"
    "    return {\n"
    "        'done': note.done != 1,\n"
    "        'title': note.title,\n"
    "    }\n"
    "MAX_ITEMS = 25\n"
)


def _sources() -> dict[str, ShownSource]:
    return {
        "fixture": ShownSource.from_text("fixture", "fixture", _FIXTURE.rstrip()),
        # line 4 is the one line the change adds
        "app.py": ShownSource.from_text("app.py", "touched", _SRC, frozenset({4})),
        "src/urls.py": ShownSource.from_text(
            "src/urls.py", "route", "path('items/', views.items)  # returns 200"
        ),
    }


def _a(quote, source="fixture", **kw):
    a = {"type": "body", "json_path": {"path": "done", "equals": False}}
    a.update(kw)
    a["evidence"] = {"source": source, "quote": quote}
    return a


_CASES = [
    # ok
    ("ok", _a("note 1 is done=false")),
    ("ok", _a("note 1 has done false", "fixture")),
    ("ok", _a("a note starts with done == False", "app.py")),  # Python False, any case
    # evidence_missing
    ("evidence_missing", {"type": "status", "equals": 200}),
    ("evidence_missing", {"type": "status", "equals": 200, "evidence": "fixture"}),
    ("evidence_missing", {"type": "status", "equals": 200, "evidence": {"source": "fixture"}}),
    ("evidence_missing", {"type": "status", "equals": 200, "evidence": {"quote": "Repository"}}),
    (
        "evidence_missing",
        {"type": "status", "equals": 200, "evidence": {"source": 3, "quote": "Repository"}},
    ),
    (
        "evidence_missing",
        {"type": "status", "equals": 200, "evidence": {"source": "fixture", "quote": 7}},
    ),
    (
        "evidence_missing",
        {"type": "status", "equals": 200, "evidence": {"source": "fixture", "quote": "  "}},
    ),
    # evidence_not_found
    ("evidence_not_found", _a("this text is nowhere")),
    ("evidence_not_found", _a("note 1 has done false", "app.py")),  # wrong source
    ("evidence_not_found", _a("path('items/'", "src/missing.py")),  # listed, not shown
    # evidence_self_justifying
    ("evidence_self_justifying", _a("'done': note.done != 1", "app.py", equals=1)),
    # a quote that spans an unchanged line and the changed one
    (
        "evidence_self_justifying",
        _a("return { 'done': note.done != 1,", "app.py", equals=1),
    ),
    # a quote that spans the changed line and the unchanged one after it
    (
        "evidence_self_justifying",
        _a("'done': note.done != 1, 'title': note.title,", "app.py", equals=1),
    ),
    # evidence_ambiguous: both values in a boolean's quote
    ("evidence_ambiguous", _a("note 1 is done=false and note 2 is done=true")),
    ("evidence_ambiguous", _a("note 1 has done false; the other note: done true")),
    # evidence_direction_mismatch
    ("evidence_direction_mismatch", _a("Token scope: a regular user")),
    ("evidence_direction_mismatch", _a("Repository: demo", "fixture", json_path={"path": "n", "equals": 7})),
    ("evidence_direction_mismatch", {"type": "body", "contains": "Zebra", "evidence": {"source": "fixture", "quote": "Repository: demo"}}),
    ("evidence_direction_mismatch", {"type": "db", "query": "SELECT 1", "count": 3, "evidence": {"source": "app.py", "quote": "MAX_ITEMS = 25"}}),
    ("evidence_direction_mismatch", {"type": "body", "matches": "^zz", "evidence": {"source": "fixture", "quote": "Repository: demo"}}),
]


@pytest.mark.parametrize("want,assertion", _CASES)
def test_each_rejection_reason(want, assertion):
    assert check_assertion(assertion, _sources()) == want


def test_rules_that_need_no_literal_and_literal_forms():
    s = _sources()
    ev = lambda src, q: {"source": src, "quote": q}  # noqa: E731
    # status: no digit needed, the quote must still be real and unchanged
    assert check_assertion({"type": "status", "equals": 404, "evidence": ev("fixture", "Token scope: a regular user")}, s) == "ok"
    assert check_assertion({"type": "status", "equals": 404, "evidence": ev("app.py", "'done': note.done != 1")}, s) == "evidence_self_justifying"
    # object and list values: no literal
    for v in ({"a": 1}, [1, 2], None):
        a = {"type": "body", "equals": v, "evidence": ev("fixture", "Repository: demo")}
        assert check_assertion(a, s) == "ok"
    # numbers as digits; db count needs the number
    assert check_assertion({"type": "db", "query": "SELECT 1", "count": 25, "evidence": ev("app.py", "MAX_ITEMS = 25")}, s) == "ok"
    assert check_assertion({"type": "body", "json_path": {"path": "n", "equals": 25}, "evidence": ev("app.py", "MAX_ITEMS = 25")}, s) == "ok"
    # strings, case-insensitive
    assert check_assertion({"type": "header", "name": "X", "contains": "REGULAR USER", "evidence": ev("fixture", "Token scope: a regular user")}, s) == "ok"
    # matches: a quoted string the pattern matches
    assert check_assertion({"type": "body", "matches": r"\d+$", "evidence": ev("app.py", "MAX_ITEMS = 25")}, s) == "ok"
    # whitespace-normalised matching
    assert check_assertion({"type": "status", "equals": 200, "evidence": ev("app.py", "def get(note):\n\n      # docs:")}, s) == "ok"
    # a route file is a source
    assert check_assertion({"type": "status", "equals": 200, "evidence": ev("src/urls.py", "returns 200")}, s) == "ok"


def test_quote_on_both_a_changed_and_an_unchanged_line_is_ok():
    sources = {
        "f.py": ShownSource.from_text("f.py", "touched", "x = False\ny = 1\nx = False\n", frozenset({3}))
    }
    a = {"type": "body", "json_path": {"path": "x", "equals": False}, "evidence": {"source": "f.py", "quote": "x = False"}}
    assert check_assertion(a, sources) == "ok"  # line 1 backs it, line 3 is irrelevant


def test_reason_order_is_the_registered_order():
    s = _sources()
    s["g.py"] = ShownSource.from_text(
        "g.py", "touched", "ok = True or False\nz = 1\nok = True or False\nw = 2\n", frozenset({1})
    )
    # not found comes before every later check, even if the quote would be ambiguous
    assert check_assertion(_a("nowhere true false", "fixture"), s) == "evidence_not_found"
    # on line 3 the same text is unchanged: found, not self-justifying, then ambiguous
    assert check_assertion(_a("ok = True or False", "g.py"), s) == "evidence_ambiguous"
    # with line 3 changed too, the quote is only on changed lines: self-justifying
    s["g.py"] = ShownSource.from_text(
        "g.py", "touched", "ok = True or False\nz = 1\nok = True or False\n", frozenset({1, 3})
    )
    assert check_assertion(_a("ok = True or False", "g.py"), s) == "evidence_self_justifying"
    # missing comes first of all
    assert check_assertion({"type": "status", "equals": 200}, s) == "evidence_missing"


def test_marker_text_in_a_quote_is_self_justifying_not_found():
    """A quote copied with the marker is on a changed line."""
    s = _sources()
    q = CHANGED_MARKER + "'done': note.done != 1,"
    assert check_assertion(_a(q, "app.py", equals=1), s) == "evidence_self_justifying"


# ---------------------------------------------------------------------
# the correction message is the O21 replay v2 text
# ---------------------------------------------------------------------

_V2_MESSAGE = (
    "Some assertions were rejected by the evidence check. Only these assertions are affected; "
    "keep every other assertion and probe exactly as it was.\n"
    "- probe 1, status assertion: evidence_missing\n"
    "- probe 2, body assertion: evidence_not_found\n"
    "Reasons: evidence_missing = no evidence object; evidence_not_found = the quote is not in the "
    "named source as shown; evidence_self_justifying = the quote comes from a changed line; "
    "evidence_direction_mismatch = the quote does not contain the expected value; evidence_ambiguous = "
    "the quote contains both true and false; invalid_json/empty_capture = format error.\n"
    "For each listed assertion, fix its evidence: quote a shorter exact passage, from outside the changed "
    "lines, that contains the expected value for this record only (for example the part about this one id, "
    "not a sentence about several). Remove an assertion only if no such passage exists.\n"
    "Return the complete corrected set of probes, in the same format. This is your only correction."
)


def _answer(*probes: dict) -> str:
    return "\n".join("```json\n" + json.dumps(p) + "\n```" for p in probes)


def _probe(*assertions: dict) -> dict:
    return {"setup": [], "act": {"method": "GET", "path": "/x"}, "assert": list(assertions)}


def test_correction_message_is_the_replay_v2_text():
    p1 = _probe({"type": "status", "equals": 200})
    p2 = _probe(_a("this text is nowhere"))
    analysis = analyse_answer(_answer(p1, p2), _sources())
    assert correction_message(analysis) == _V2_MESSAGE


def test_correction_message_none_when_everything_passes():
    ok = _probe(_a("a note starts with done == False", "app.py"))
    analysis = analyse_answer(_answer(ok), _sources())
    assert not analysis.needs_correction
    assert correction_message(analysis) is None


def test_schema_error_is_listed_in_the_correction_message():
    bad = '{"act": {"method": "GET", "path": "/x"}}'  # no assert
    text = "```json\n" + bad + "\n```\n" + _answer(_probe(_a("this text is nowhere")))
    analysis = analyse_answer(text, _sources())
    msg = correction_message(analysis)
    assert msg is not None
    assert "- probe 1: schema_error: block 0: probe needs 'assert'" in msg
    assert "- probe 2, body assertion: evidence_not_found" in msg


def test_final_probeset_drops_rejected_assertions_and_empty_probes():
    good = _a("a note starts with done == False", "app.py")
    bad = _a("this text is nowhere")
    keep = _probe(good, bad)  # one assertion survives
    gone = _probe(bad)  # none survives -> invalid block
    analysis = analyse_answer(_answer(keep, gone), _sources())
    ps = final_probeset(analysis)
    assert len(ps.probes) == 1
    assert ps.probes[0]["assert"] == [good]
    assert len(ps.invalid_blocks) == 1
    inv = ps.invalid_blocks[0]
    assert inv["block_index"] == 1
    assert json.loads(inv["raw_text"]) == gone
    assert "evidence_not_found" in inv["rejection_reason"]


# ---------------------------------------------------------------------
# (f) the code-content scan skips `evidence`
# ---------------------------------------------------------------------


def _with_evidence(quote: str, **extra) -> dict:
    a = {"type": "status", "equals": 200, "evidence": {"source": "app.py", "quote": quote}}
    a.update(extra)
    return _probe(a)


def test_evidence_quote_of_code_does_not_reject_the_probe():
    quote = "import os\nimport subprocess\nx = eval(y); exec(z); __import__('q')"
    validate_probe(_with_evidence(quote))  # must not raise


def test_code_scan_still_rejects_code_outside_evidence():
    with pytest.raises(ProbeRejected, match="code probe rejected"):
        validate_probe(_with_evidence("fine", **{"equals": 200, "note": "import os"}))
    bad = _probe({"type": "body", "contains": "import os", "evidence": {"source": "app.py", "quote": "x"}})
    with pytest.raises(ProbeRejected, match="code probe rejected"):
        validate_probe(bad)
    # and `evidence` nested elsewhere (inside json_path) is not exempt
    nested = _probe(
        {
            "type": "body",
            "json_path": {"path": "a", "equals": 1, "evidence": "import os"},
            "evidence": {"source": "app.py", "quote": "x"},
        }
    )
    with pytest.raises(ProbeRejected, match="code probe rejected"):
        validate_probe(nested)
