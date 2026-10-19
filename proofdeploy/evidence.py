"""Evidence-cited assertions, checked by the harness (R-METHOD, D242).

Every assertion the author writes carries a top-level ``evidence``
object, ``{"source": <a path shown in the prompt, or "fixture">,
"quote": <exact text>}``. Before anything runs, the harness checks each
quote MECHANICALLY (no model), against the text the prompt actually
showed:

1. ``evidence_missing``: no evidence object, or a missing or non-string
   source or quote;
2. ``evidence_not_found``: the whitespace-normalised quote does not
   occur in the named source as shown in the prompt (the fixture text;
   the full text of a touched file; a route declaration file). A file
   that is listed but not shown cannot be quoted;
3. ``evidence_self_justifying``: the quote occurs in the source only
   within (or across) lines the change under review adds or alters;
4. ``evidence_ambiguous``: for a boolean expected value, the quote
   contains both ``true`` and ``false`` (as words, any case);
5. ``evidence_direction_mismatch``: the quote does not contain the
   expected value's text. Booleans match ``true``/``false`` as words,
   any case; numbers as digits; strings case-insensitively;
   ``matches`` needs a quoted string the pattern matches; ``db``
   ``count`` needs the number. Status codes and object or list values
   need no literal (the quote must still be real and not
   self-justifying).

The first failing check names the rejection. One correction round is
sent for rejected assertions and schema-invalid blocks (see
``Orchestrator._author_probes``); in the final answer a rejected
assertion is removed, and a probe left with no assertion is an invalid
block.

This module also owns the SHOWN SOURCES: the exact texts the prompt
displays (the fixture, each touched file with its changed lines marked,
each included route file). The prompt builder renders from them, so
what the check matches against is, by construction, what the author
saw.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

from proofdeploy.probe import ParsedBlock, ProbeSet, parse_probe_blocks

# Prefix of every line the diff adds or changes, in the touched-file
# contents (R-METHOD / header v8): the marker, then one space, then the
# original line.
CHANGED_MARKER = "<<CHANGED>> "

# Stands for a changed line when the text outside the changed lines is
# built: a private-use character, so a quote that spans a changed line
# cannot match across the gap.
_GAP = ""

REASONS = (
    "evidence_missing",
    "evidence_not_found",
    "evidence_self_justifying",
    "evidence_ambiguous",
    "evidence_direction_mismatch",
)


def norm(s: str) -> str:
    """Whitespace-normalise: runs of whitespace become one space."""
    return re.sub(r"\s+", " ", s).strip()


# ---------------------------------------------------------------------
# Changed lines, from the diff's hunk headers.
# ---------------------------------------------------------------------

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def changed_lines_by_file(diff_text: str) -> dict[str, frozenset[int]]:
    """New-file line numbers the diff adds or changes, per path.

    Computed from the hunk headers and the hunk lines (each ``+`` line
    in a hunk is a line of the post-change file; the counts in the
    header say where a hunk ends), never by matching text. Paths are
    the ``+++ b/<path>`` names, the same as ``diff_touched_files``.
    """
    out: dict[str, set[int]] = {}
    lines = diff_text.split("\n")
    i = 0
    current: str | None = None
    while i < len(lines):
        line = lines[i]
        if line.startswith("diff --git "):
            current = None
            i += 1
            continue
        if line.startswith("+++ "):
            name = line[4:].split("\t", 1)[0].strip()
            if name == "/dev/null":
                current = None
            elif name.startswith("b/"):
                current = name[2:]
                out.setdefault(current, set())
            i += 1
            continue
        m = _HUNK_RE.match(line)
        if m is None:
            i += 1
            continue
        old_left = int(m.group(2)) if m.group(2) is not None else 1
        new_left = int(m.group(4)) if m.group(4) is not None else 1
        new_ln = int(m.group(3))
        i += 1
        while i < len(lines) and (old_left > 0 or new_left > 0):
            hl = lines[i]
            if hl.startswith("\\"):  # "\ No newline at end of file"
                i += 1
                continue
            if hl.startswith("+"):
                if current is not None:
                    out[current].add(new_ln)
                new_ln += 1
                new_left -= 1
            elif hl.startswith("-"):
                old_left -= 1
            else:  # context line (a bare empty line stands for " ")
                old_left -= 1
                new_left -= 1
                new_ln += 1
            i += 1
    return {k: frozenset(v) for k, v in out.items()}


# ---------------------------------------------------------------------
# Shown sources.
# ---------------------------------------------------------------------


@dataclass(frozen=True)
class ShownSource:
    """One text the prompt shows, as a quote source.

    ``path`` is the name an assertion's evidence ``source`` must give:
    ``"fixture"`` or the file path. ``lines`` are the source's lines
    exactly as the file has them; ``changed`` are the 1-based numbers
    of the lines the change adds or alters (empty for the fixture and
    for route files). ``shown_text`` is exactly what the prompt
    contains for this source.
    """

    path: str
    kind: str  # "fixture" | "touched" | "route"
    lines: tuple[str, ...]
    changed: frozenset[int] = field(default_factory=frozenset)

    @classmethod
    def from_text(
        cls, path: str, kind: str, text: str, changed: frozenset[int] = frozenset()
    ) -> ShownSource:
        lines = text.split("\n")
        if lines and lines[-1] == "":
            lines.pop()  # the newline that ends the last line
        return cls(path, kind, tuple(lines), frozenset(changed))

    @property
    def shown_text(self) -> str:
        """The text in the prompt: changed lines prefixed with the
        marker, trailing whitespace stripped."""
        marked = [
            (CHANGED_MARKER + ln) if (n + 1) in self.changed else ln
            for n, ln in enumerate(self.lines)
        ]
        return "\n".join(marked).rstrip()

    @property
    def plain_text(self) -> str:
        """The same text without the markers."""
        return "\n".join(self.lines).rstrip()

    @property
    def unchanged_text(self) -> str:
        """The text with every changed line replaced by a gap, so no
        quote can match inside, or across, a changed line."""
        kept = [
            _GAP if (n + 1) in self.changed else ln for n, ln in enumerate(self.lines)
        ]
        return "\n".join(kept)

    def occurs(self, nq: str) -> bool:
        return nq in norm(self.plain_text) or nq in norm(self.shown_text)

    def occurs_outside_changes(self, nq: str) -> bool:
        return nq in norm(self.unchanged_text)


# ---------------------------------------------------------------------
# The per-assertion checks.
# ---------------------------------------------------------------------

_Expected = tuple[str, Any]  # (kind, value); kind in none|bool|num|str|matches


def _general(v: Any) -> _Expected:
    if isinstance(v, bool):
        return ("bool", v)
    if isinstance(v, (int, float)):
        return ("num", v)
    if isinstance(v, str):
        return ("str", v)
    return ("none", None)  # objects, lists, null: no literal


def _expected(a: dict[str, Any]) -> _Expected:
    """The expected value the comparator of this assertion carries.

    The comparator precedence is the executor's: header equals >
    contains > matches; body equals > contains > matches > json_path.
    """
    t = a.get("type")
    if t == "status":
        return ("none", None)
    if t == "db":
        if "count" in a:
            return ("num", a["count"])
        if "equals" in a:
            return _general(a["equals"])
        return ("none", None)
    if t == "body" and "equals" not in a and "contains" not in a and "matches" not in a:
        jp = a.get("json_path")
        if isinstance(jp, dict) and "equals" in jp:
            return _general(jp["equals"])
        return ("none", None)
    if "equals" in a:
        return _general(a["equals"])
    if "contains" in a:
        return _general(a["contains"])
    if "matches" in a and isinstance(a["matches"], str):
        return ("matches", a["matches"])
    return ("none", None)


def _bool_words(quote: str) -> tuple[bool, bool]:
    q = quote.lower()
    return (
        re.search(r"\btrue\b", q) is not None,
        re.search(r"\bfalse\b", q) is not None,
    )


def check_assertion(a: Any, sources: dict[str, ShownSource]) -> str:
    """Check one assertion's evidence. Returns ``"ok"`` or the first
    failing reason, in the registered order."""
    ev = a.get("evidence") if isinstance(a, dict) else None
    if not isinstance(ev, dict):
        return "evidence_missing"
    src, quote = ev.get("source"), ev.get("quote")
    if not isinstance(src, str) or not isinstance(quote, str):
        return "evidence_missing"
    nq = norm(quote)
    if not src.strip() or not nq:
        return "evidence_missing"
    source = sources.get(src)
    if source is None or not source.occurs(nq):
        return "evidence_not_found"
    if not source.occurs_outside_changes(nq):
        return "evidence_self_justifying"
    kind, value = _expected(a)
    q = nq.lower()
    if kind == "bool":
        has_true, has_false = _bool_words(nq)
        if has_true and has_false:
            return "evidence_ambiguous"
        if not (has_true if value else has_false):
            return "evidence_direction_mismatch"
        return "ok"
    if kind == "num":
        return "ok" if str(value) in nq else "evidence_direction_mismatch"
    if kind == "str":
        return "ok" if norm(value).lower() in q else "evidence_direction_mismatch"
    if kind == "matches":
        try:
            hit = re.search(value, quote) is not None or re.search(value, nq) is not None
        except re.error:
            hit = False
        return "ok" if hit else "evidence_direction_mismatch"
    return "ok"


# ---------------------------------------------------------------------
# One author answer, analysed.
# ---------------------------------------------------------------------


def _schema_kind(reason: str) -> str:
    if "invalid JSON" in reason:
        return "invalid_json"
    if "capture" in reason and "non-empty" in reason:
        return "empty_capture"
    return "schema_error"


@dataclass
class AnswerAnalysis:
    """An author answer, parsed per block and checked per assertion."""

    text: str
    blocks: list[ParsedBlock]
    # Per assertion of each valid block: {"probe": N (1-based block
    # number), "assertion": j (0-based), "type": ..., "result": "ok" |
    # a rejection reason}.
    checks: list[dict[str, Any]]

    @property
    def rejected(self) -> list[dict[str, Any]]:
        return [c for c in self.checks if c["result"] != "ok"]

    @property
    def schema_errors(self) -> list[ParsedBlock]:
        return [b for b in self.blocks if b.probe is None]

    @property
    def needs_correction(self) -> bool:
        """A rejected assertion, or a block invalid under the probe
        schema. An answer with no fenced block at all has nothing to
        correct (it is an invalid answer, recorded as such)."""
        return bool(self.rejected or self.schema_errors)


def analyse_answer(
    text: str, sources: dict[str, ShownSource], allow_harness_app: bool = False
) -> AnswerAnalysis:
    blocks = parse_probe_blocks(text, allow_harness_app=allow_harness_app)
    checks: list[dict[str, Any]] = []
    for b in blocks:
        if b.probe is None:
            continue
        for j, a in enumerate(b.probe.get("assert", [])):
            checks.append(
                {
                    "probe": b.index + 1,
                    "assertion": j,
                    "type": a.get("type") if isinstance(a, dict) else None,
                    "result": check_assertion(a, sources),
                }
            )
    return AnswerAnalysis(text=text, blocks=blocks, checks=checks)


def correction_message(analysis: AnswerAnalysis) -> str | None:
    """The one follow-up the harness sends (the O21 replay v2 text,
    ``check.correction_message``); lists each rejected assertion and
    each schema error. None when there is nothing to correct."""
    lines: list[str] = []
    for b in analysis.blocks:
        if b.probe is not None:
            continue
        reason = b.rejection_reason or ""
        lines.append(f"- probe {b.index + 1}: {_schema_kind(reason)}: {reason}")
    for c in analysis.rejected:
        lines.append(f"- probe {c['probe']}, {c['type']} assertion: {c['result']}")
    if not lines:
        return None
    return (
        "Some assertions were rejected by the evidence check. Only these assertions are "
        "affected; "
        "keep every other assertion and probe exactly as it was.\n" + "\n".join(lines) +
        "\nReasons: evidence_missing = no evidence object; evidence_not_found = the quote is "
        "not in the "
        "named source as shown; evidence_self_justifying = the quote comes from a changed "
        "line; "
        "evidence_direction_mismatch = the quote does not contain the expected value; "
        "evidence_ambiguous = "
        "the quote contains both true and false; invalid_json/empty_capture = format error.\n"
        "For each listed assertion, fix its evidence: quote a shorter exact passage, from "
        "outside the changed "
        "lines, that contains the expected value for this record only (for example the part "
        "about this one id, "
        "not a sentence about several). Remove an assertion only if no such passage exists.\n"
        "Return the complete corrected set of probes, in the same format. This is your only "
        "correction."
    )


def correction_prompt(prompt: str, first_answer: str, message: str) -> str:
    """The follow-up prompt: the original prompt, the first answer, the
    message (R-METHOD item 3)."""
    return prompt + "\n\n---\n\nYour previous answer:\n\n" + first_answer + "\n\n" + message


def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def final_probeset(analysis: AnswerAnalysis) -> ProbeSet:
    """The probes that run from this (final) answer.

    A rejected assertion is removed; a probe left with no assertion is
    an invalid block (recorded verbatim, never sent), as is a block
    that failed the probe schema. An answer without any fenced block
    yields the usual single invalid block.
    """
    from proofdeploy.probe import HARNESS_VERSION, harness_app_hash

    if not analysis.text.strip():
        return ProbeSet(
            probes=[],
            invalid_blocks=[
                {
                    "block_index": -1,
                    "raw_text": "",
                    "rejection_reason": "model returned empty content",
                }
            ],
        )
    if not analysis.blocks:
        return ProbeSet(
            probes=[],
            invalid_blocks=[
                {
                    "block_index": -1,
                    "raw_text": analysis.text,
                    "rejection_reason": "no fenced ```json blocks found in author output",
                }
            ],
        )
    by_probe: dict[int, list[dict[str, Any]]] = {}
    for c in analysis.checks:
        by_probe.setdefault(c["probe"], []).append(c)
    probes: list[dict[str, Any]] = []
    invalid: list[dict[str, Any]] = []
    for b in analysis.blocks:
        if b.probe is None:
            invalid.append(
                {
                    "block_index": b.index,
                    "raw_text": b.raw_text,
                    "rejection_reason": b.rejection_reason,
                }
            )
            continue
        results = by_probe.get(b.index + 1, [])
        kept = [
            a
            for a, c in zip(b.probe.get("assert", []), results, strict=True)
            if c["result"] == "ok"
        ]
        if not kept:
            reasons = sorted({c["result"] for c in results})
            invalid.append(
                {
                    "block_index": b.index,
                    "raw_text": b.raw_text,
                    "rejection_reason": (
                        f"block {b.index}: every assertion was rejected by the evidence "
                        f"check ({', '.join(reasons)})"
                    ),
                }
            )
            continue
        probe = dict(b.probe)
        probe["assert"] = kept
        probes.append(probe)
    hashes = [
        harness_app_hash(step["source"])
        for p in probes
        for step in p.get("setup", [])
        if step.get("type") == "harness_app"
    ]
    return ProbeSet(
        probes=probes,
        invalid_blocks=invalid,
        harness_version=HARNESS_VERSION,
        harness_app_hashes=hashes,
    )
