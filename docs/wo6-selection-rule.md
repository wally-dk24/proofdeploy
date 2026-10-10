# WO-6 selection rule: app and bug for the blind real-app run

**Status: DRAFT for Master's sign-off. Do not run.** The selection result
goes in a later commit, after sign-off. The rule itself is not re-run and
the candidate list is not edited after sign-off.

## Why this rule exists

WO-6's blind real-app run must not be hand-picked: Wally knows every eval
bug, so any human choice of app or bug is suspect. This rule fixes the
candidate list and the selection criterion in advance, so the outcome is
mechanical.

## Candidate list (fixed)

From the eval set, excluding the rows that were NO-GO (they cannot run, so
they are not viable for a blind run — this is the eval set's own verdict,
not a new judgment):

| # | App      | Bug                  | Repo                                            |
|---|----------|----------------------|-------------------------------------------------|
| 1 | Directus | TFA                  | https://github.com/directus/directus            |
| 2 | Vendure  | productVariantCount  | https://github.com/vendure-ecommerce/vendure    |
| 3 | Vendure  | API-key escalation   | https://github.com/vendure-ecommerce/vendure    |

Ordered by (app name, bug id) ascending. Ghost is excluded: both Ghost rows
(gift-link) were NO-GO in the eval set — Owner fixture and JWT refresh
blockers — so no Ghost bug is runnable for the blind run.

## First-match criterion

The selection is the **first candidate in the list** whose repository is
publicly reachable, verified mechanically:

```
git ls-remote <repo-url> HEAD
```

exits 0. This is a reachability check only. No human looks at the
candidates, the bugs, or the repositories before the rule runs.

## Resolving revisions

The exact bug-introducing (B) and fix revisions for the selected (app, bug)
are resolved from the eval set records at run time. The rule selects the
(app, bug) pair; the SHAs are looked up, not chosen.

## After sign-off

1. Master signs off this rule (this document, unchanged).
2. The rule is run once, mechanically.
3. The selected (app, bug, B SHA, fix SHA) is recorded in a later commit.
4. The blind real-app run (WO-6) proceeds against that selection.
