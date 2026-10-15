# Registration: REG-D — dated dependency resolution for framework sample apps without a lockfile

Registered: 2026-10-15. Narrow exception to the frozen-lockfile rule (rule 9) for one situation only. Items not named here leave every registered rule unchanged.

Why. Some framework repositories are measured by building a sample application that ships inside the repository (the harness-app path, D13). When that sample ships no lockfile, its dependencies would resolve to whatever the registry serves on the day of the run: the two sides of a unit could resolve differently, and a later re-run could not reproduce either. The first case is nestjs/nest sample/01-cats-app, which has no lockfile at any registered SHA; all eight held-out units depend on it.

D1. Scope. Applies only when all hold: (a) the unit's contract builds a sample application that lives in the repository and is not the package under test; (b) that sample has no lockfile at the side's SHA; (c) the repository's own install is frozen from its own lockfile. It never applies to an application under test or to a repository's own dependencies; a Node app with no lockfile stays "not supported: no frozen lockfile" (D18).

D2. Dated resolution. The sample's install runs as npm install --before=<cutoff> (with any flags the contract already declares), where <cutoff> is the unit's lock cutoff as defined for REG-A — the fix commit's committer date for a bug unit (used for both fix^ and fix), the C commit's committer date for a clean diff — as an ISO 8601 UTC timestamp. No lockfile is generated, committed or written back to the repository.

D3. Identical on both sides. Both sides of a unit use the same cutoff and the same command.

D4. Recorded. The run record states, per side: that REG-D applied, the sample path, the cutoff, the exact command, and the resolved dependency tree (npm ls --all --json, or its sha256 with the file kept as an artifact). A unit whose two sides resolve to different trees is reported as such, with the difference.

D5. Network. As for every install: network during the install phase only, then cut; no customer secrets.

D6. Known limit, stated. Dated resolution reproduces only while the registry still serves the versions it resolved. A failure to resolve is a typed BUILD_FAILED naming REG-D, never a silent fall-back to an undated install.
