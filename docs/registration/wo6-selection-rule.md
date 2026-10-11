# Registration: WO-6 candidate query and bug selection (REG-C)

Registered: 2026-10-11.

Purpose. WO-6 is the blind acceptance test on a real app. Nobody picks the app or the bug. A committed script does, and its raw output goes in a later commit.

1. Snapshot. One run of the committed query script, on a date recorded in its output. The GitHub repository search API, unauthenticated or with a read-only token, queried for: Node: language:JavaScript and language:TypeScript; Python: language:Python; stars:>=1000 archived:false fork:false pushed:>=<snapshot date minus 90 days>; sorted by stars descending, the first 100 results per language query, de-duplicated.

2. Exclusions. vendure-ecommerce/vendure (docs/evaluation/control-registration-v2.md), directus/directus (docs/evaluation/control-registration-v2.md), TryGhost/Ghost (docs/evaluation/control-registration-v2.md), nestjs/nest (docs/evaluation/held-out-set-complete-2026-10-09.md), koajs/koa (docs/evaluation/held-out-set-complete-2026-10-09.md), adonisjs/core (docs/evaluation/held-out-set-complete-2026-10-09.md), pallets/flask (docs/evaluation/held-out-set-complete-2026-10-09.md), fastapi/fastapi (docs/evaluation/held-out-set-complete-2026-10-09.md), codeigniter4/CodeIgniter4 (docs/evaluation/dev-set-v2-2026-10-09.md), fastify/fastify (docs/evaluation/dev-set-v2-2026-10-09.md), django/django (docs/evaluation/dev-set-v2-2026-10-09.md), LMCache/LMCache (docs/evaluation/dev-set-v2-2026-10-09.md), bottlepy/bottle (docs/evaluation/dev-set-v2-2026-10-09.md), honojs/hono (docs/evaluation/dev-set-v2-2026-10-09.md), and this list: Ghost, Directus, Vendure, NestJS, koa, adonisjs, Flask, FastAPI, CodeIgniter4, Fastify, Django, LMCache, Bottle, Hono, body-parser, Express, n8n, Strapi, Payload, django-oscar, keystone, uptime-kuma, pretix, nocodb, datasette, apostrophe, wagtail (the last eight were looked at for WO-6 on 2026-10-10). A bare name excludes every repository whose owner or name (the parts before and after the slash) equals it, case-insensitively.

3. Mechanical filters at the default branch's HEAD (file checks only; no human reads the repo):
   Node: a root package.json with scripts.start; no workspaces field and no pnpm-workspace.yaml, lerna.json, nx.json or turbo.json at the root; a root package-lock.json, pnpm-lock.yaml or yarn.lock; and sqlite3, better-sqlite3 or @libsql/client in dependencies.
   Python: a root manage.py; the string django.db.backends.sqlite3 somewhere in the repo; and a root uv.lock, poetry.lock, pdm.lock, requirements.txt or pyproject.toml (REG-A covers unlocked ones).

4. Order. Survivors sorted by stars descending, ties by owner/name. The ordered list is committed before any provisioning.

5. Fixtures. For each survivor in order, Wally writes a generic proofdeploy.yml from the repo's README and config at HEAD only (never a fix, an issue or a diff), committed before its provisioning proof.

6. Provisioning proofs at HEAD, in order, through the real sandboxed runner, until 3 candidates reach READY. Logs are committed. The first READY candidate is the WO-6 app. No bug inspection or diff viewing before the run.

7. Bug selection in that app (script, not a person). Walk the default branch's commits in committer-date order, starting strictly after 2026-07-01. The first commit that passes all of these is the fix: subject matches (?i)\bfix(e[sd])?\b; changes at least one non-test, non-doc source file; B = the most recent commit that last touched any line the fix removes or changes (git blame at fix^), B must exist, B != fix^ is allowed, and B must be contained in a release tag created before the fix (git tag --contains), which is the "shipped" test; fix^ and the fix both reach READY. If 200 commits are walked without a pass, the next READY candidate is used.

8. Then the blind author runs under the registered rules, with no-skill arm and the registered prompt. The result is reported whatever it is.

9. Ghost's go/no-go stays separate: attempt Ghost under the registered rules at WO-6, not as a selection criterion.

10. Honest gaps: the "testable over HTTP" and "not a boundary bug" eligibility checks (rule 7) can't be made mechanical. For WO-6, an acceptance test, they're not applied. If the selected bug isn't HTTP-testable, the run reports INCONCLUSIVE with its reason, and that is the result.
