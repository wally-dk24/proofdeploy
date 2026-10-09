#!/usr/bin/env python3
"""Generate the author-facing skill copy from the audited source.

The audited source (skill.md) carries provenance tags ([PROC], [DEV: Tn])
and TRAIN dev-set wording for the reviewer's leak audit. The author must
not see that this is part of an evaluation, so this script strips the
tags and rewords the framing. The generated copy (skill-author.md) is
what gets hashed and registered.

Usage: python3 scripts/generate_author_skill.py
"""
import re
from pathlib import Path

HERE = Path(__file__).parent.parent
SRC = HERE / "docs/evaluation/skill/skill.md"
DST = HERE / "docs/evaluation/skill/skill-author.md"

def main():
    content = SRC.read_text()

    # Strip provenance tags: [PROC], [DEV: T1] ... [DEV: T7]
    content = re.sub(r' ?\[(?:PROC|DEV: T\d+)\]', '', content)
    content = re.sub(r' +(\n)', r'\1', content)

    # Reword framing that reveals the evaluation
    content = content.replace(
        'Every numbered item carries a provenance tag: `` (general procedure), `[DEV: T<n>]` (illustrated by TRAIN dev-set bug T<n>). An untagged item is a drafting error.',
        'Each item teaches a probing technique. Study the pattern, not the specific example.'
    )
    # Fallback in case the tag-strip already altered the line
    content = content.replace(
        'Every numbered item carries a provenance tag.',
        'Each item teaches a probing technique.'
    )
    content = content.replace(
        'Each example is from the TRAIN dev set only, cited by commit SHA. Study the pattern, not the specific bug.',
        'Each example is from a real bug fix, cited by commit SHA. Study the pattern, not the specific bug.'
    )

    # Verify no evaluation-revealing language remains
    for pattern in [r'\[DEV:', 'TRAIN', r'T<n>']:
        assert not re.search(pattern, content), f"Leak: {pattern} still present"

    DST.write_text(content)
    print(f"Wrote {DST}")

if __name__ == "__main__":
    main()
