---
description: Verify the current changes with ProofDeploy - author change-aware probes from the diff and check them against intended behavior.
---

# Verify with ProofDeploy

Run the proofdeploy skill's verification loop against the current working diff:

1. Get the diff under verification: `git diff` for uncommitted changes, or `git diff <base>...HEAD` for a branch. If the user named a commit range or PR, use that instead.
2. Follow the skill at `skills/proofdeploy/SKILL.md`: build the change model, author probes, write expected results from the intended behavior first.
3. Emit the authored probes as `probes.json` per `skills/proofdeploy/references/probe-schema.md` (schema v0, draft).

Current status: the deterministic runner (`proofdeploy verify --probes`) is under construction. Today this command produces the authored `probes.json` and a written verdict of what the probes *would* check. Do not invent an execution path or claim probes were run.

If there are no behavior-changing hunks in the diff, say so and stop - do not author probes that cannot fail.
