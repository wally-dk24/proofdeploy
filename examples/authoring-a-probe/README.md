# Authoring a probe — worked example

**Scenario.** Someone changed a boundary condition in `shop/pricing.py`
(`sample.diff`): the bulk discount now applies at `> 10` instead of `>= 10`.
The docstring still says "15% at 10+ items."

**The question the skill asks:** what behavior does this diff put at risk,
and what would prove it still holds?

## The loop on this diff

1. **Change model** (`change-model.md`): one hunk, categorized
   `behavior-change`. The rates themselves are `must-not-change`.
2. **Probes** (`probes.json`): two HTTP probes, expected results written from
   the docstring — never from the changed code.
3. **Verdicts (when the runner lands):** probe 1 (`quantity: 10` → `0.15`)
   fails against the changed code, which returns `0.0`. The break is caught.
   Probe 2 (`quantity: 9` → `0.0`) passes and guards the threshold from below.

## Try it

Author these yourself following `plugins/proofdeploy/skills/proofdeploy/SKILL.md`
(the plugin is unlisted from the marketplace until the runner exists), then
compare. Then validate the emitted file:

```bash
python3 ../validating-probes/validate.py probes.json
```
