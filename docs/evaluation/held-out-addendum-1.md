# Registration: Held-out Addendum 1 — completing the held-out set to 10 bugs and 10 clean diffs

Registered: 2026-10-14. Amends the held-out selection registered in docs/evaluation/dev-set-registration-v2.md and recorded in docs/evaluation/held-out-set-complete-2026-10-09.md and docs/evaluation/held-out-clean-diffs-final-2026-10-09.md. Items not named here are unchanged.

Why. The dev-set floors (at most 1 false alarm in 10 clean diffs; at least 3 catches in 10 bugs and above the no-skill baseline) need 10 bugs and 10 clean diffs. The held-out set holds 4 and 4. Measuring on train units would favour the skill arm, because the skill was drafted from them. This addendum widens the held-out set by its own rule; no floor is lowered. No probe has been authored for, and no measurement has been made on, any held-out unit.

H1. Measurement set. The dev-set measurement uses held-out units only. The train set (T1–T7, train clean diffs) is unchanged and is not measured.

H2. Target. 10 held-out bugs and 10 held-out clean diffs. The existing 4 bugs (H1–H4) and 4 clean diffs count toward the target.

H3. Bugs: the same rule, continued. The same five repositories (nestjs/nest, koajs/koa, adonisjs/core, pallets/flask, fastapi/fastapi), the same order (advisories first, then strict ^fix( commits in SHA order), continuing from the position after the last candidate judged on 2026-10-09, under the same eligibility rule (rule 7, including no boundary or off-by-one bugs). Candidates are judged in order.

H4. Clean diffs: the same method, continued. Behavior-changing backend commits from the same five repositories, 4–6 weeks old at the date of this run, with no later change to any line they add or change (git blame --reverse, the method in held-out-clean-diffs-final-2026-10-09.md), taken in SHA order per weekly window, newest window first.

H5. Runnable in Stage 1. Before acceptance, each candidate's sides (fix^ and fix for a bug; C for a clean diff) are provisioned through the sandboxed readiness path, with no model, no probes and no scoring, on a main that includes the plan 1.2b (workspaces) and 1.2e (bun) changes; the run record names that SHA. A candidate whose sides do not reach READY for a typed reason in a not-supported class is skipped, and its typed reason is recorded and published with the results; the walk continues. A harness or environment failure is retried, not skipped.

H6. Order of work. The completed list (all 10 and 10, with every skipped candidate and its reason) is committed before any probe is authored for any held-out unit and before any measurement.

H7. Fallback (stated before any result). If the walk exhausts the five repositories before reaching 10 and 10, no unit is chosen by hand. Master decides between (i) widening the repository list by a further registered mechanical rule, and (ii) recording the smaller denominators, with the floors scaled proportionally and stated as such.

H8. Reporting. The repository spread of the final set is reported alongside the dev-set numbers. It is not tuned.

