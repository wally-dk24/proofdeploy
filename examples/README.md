# Examples

Small, honest, runnable examples of what ProofDeploy does today.

ProofDeploy's loop is **diff → probes → verdicts**. The deterministic runner
(`proofdeploy verify`) is still under construction, so these examples cover
the part that exists now: **authoring** — turning a diff into a change model
and a `probes.json` file — plus validating that file.

| Example | What it teaches |
|---------|-----------------|
| `authoring-a-probe/` | A 1-line diff, the change model, and the two probes that catch the break |
| `validating-probes/` | A machine-readable draft of the probe schema and a stdlib-only validator |
| `proofdeploy-yml/` | The repo contract across three stacks: Node.js, Python, dotnet |

Each example has its own README. `tests/test_examples.py` keeps them honest:
CI fails if an example `probes.json` stops validating.
