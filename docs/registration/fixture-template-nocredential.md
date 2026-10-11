# Registration: Fixture description for repos with no declared credential (REG-B)

Registered: 2026-10-11.

Problem. The frozen template (proofdeploy/fixture_template.txt, sha256 0994d1e3...0323b) always renders "Authentication: {auth_mechanism}. Your token: ${AUTH_TOKEN}." For a repo whose contract declares no credential, that tells the blind author it holds a token it doesn't have; any probe using it is INCONCLUSIVE probe, counted against the tool. All three WO-5 acceptance runs received that line. Most dev-set and all held-out repos have no declared credential.

Rule:

1. proofdeploy/fixture_template.txt stays byte-identical (sha256 0994d1e3...0323b). It is used when the repo's proofdeploy.yml declares a credential (a top-level auth: mapping).

2. A second frozen template, proofdeploy/fixture_template_nocredential.txt, is used when the contract declares no credential. Its body is exactly:

```
Repository: {repo_name}
Seed data: {seed_note}
Authentication: {auth_mechanism}. You are given no token or credentials.
No users, roles, or credentials exist beyond what the seed creates.
Feature flags: {flags_note}.
You may create users, roles, data, or change settings in setup steps before your probes run.
```

It has no ${AUTH_TOKEN} and no {auth_scope}; auth_scope is not required when this template is used.

3. The choice between the two is mechanical (the presence of auth:), made before any model call. The bundle manifest records which template was used and its sha256; the answer-key check verifies it.

4. Both templates' sha256 values are recorded in the registration commit. Any change to either invalidates prior runs that used it.

5. Applies to every unit from its merge onward, before the dev-set measurement. Runs made before the merge (the WO-5 acceptance runs, the Bottle re-run) keep their records and aren't re-scored.

## Template sha256 values (recorded per item 4)

- proofdeploy/fixture_template.txt: `0994d1e367ddb3c679e750451dd67eab3a3d974fc1620b7f750216cdcdf0323b`
- proofdeploy/fixture_template_nocredential.txt: `6cb8edafa3211ccd3c275648bf3a2cc5ddadbbc876aa65d8105be5186b4188e9`
