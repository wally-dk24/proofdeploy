# Registration: Python dependency lock rule (REG-A)

Registered: 2026-10-11. Closes WAL-76.

1. Complete lock present. A repo's Python dependencies are locked if it commits uv.lock, poetry.lock or pdm.lock, or a requirements file where every line carries --hash. These install frozen (uv sync --frozen, poetry install --no-update / --sync, pdm sync, or pip install --require-hashes --no-deps). A mismatch or a missing transitive pin is BUILD_FAILED.

2. Otherwise, a generated lock. The runner resolves the declared dependencies (pyproject.toml, or the requirements file's pins as constraints) with uv 0.13.0, --generate-hashes, for the Python version selected under rule 9 and linux/x86_64, against https://pypi.org/simple, with --exclude-newer set to one cutoff per unit: the fix commit's committer time for a bug pair (the same cutoff for fix^ and fix) and C's committer time for a clean diff. It installs with --require-hashes --no-deps.

3. Where it runs. Generation and install happen in the sandbox's install container (network on); the app container then runs with the network cut, as for every unit.

4. Recorded. The evidence records the lock source (committed or generated), the generated lock text and its sha256, the resolver and its version, the cutoff, and the index URL.

5. Failures. A resolution or install failure is BUILD_FAILED, typed environment, and the pair is INCONCLUSIVE under the start-failure rule.

6. Applies to every Python unit (dev set, held-out, eval, WO-6) from its merge onward. Units measured before the merge aren't re-scored.
