# Security Policy

## Supported versions

Only the latest `main` is supported during the pre-1.0 build.

## Reporting a vulnerability

Open a GitHub issue with the `security` label, or email the maintainer via the
address on the GitHub profile. Do not open a public issue with exploit details.

## Security design

- ProofDeploy never touches production: it provisions disposable instances and
  disposable databases, and takes no production credentials.
- Database probes run read-only by database enforcement (read-only role or
  always-rollback transaction).
- `--target-url` accepts loopback addresses only unless `--allow-remote-target`
  is passed, which forces read-only, HTTP GET/HEAD-only, no DB probes.
- API keys (e.g. for the probe author's model) are read from the environment
  at runtime and never committed to the repo.
