# Sandbox CI: where the container tests run

The sandbox tests (`tests/test_sandbox.py`) use real containers. They
SKIP with a stated reason when no container runtime is present; they
are not host-dependent.

## Runner requirements

A runner for the sandbox tests needs:

- **podman** (rootful). The tests use `--storage-driver vfs
  --cgroup-manager=cgroupfs` (no systemd, no overlayfs).
- **Python 3.12+** with pytest.
- Network access to pull `docker.io/library/python:3.12-slim`
  (or a pre-pulled image).

The tests do NOT need:
- Isolated container networking (netavark). The sandbox falls back to
  `--net=none` + Unix-socket bridge when `podman network create
  --internal` cannot run a container.
- User-namespace remapping. The sandbox falls back to `--user 65534`
  (nobody) with `--cap-drop=ALL` and `no-new-privileges` when
  `--uidmap` is unavailable.

## What the tests prove

On a runner with podman, the tests verify from a real container:

1. `test_sandbox_install_and_run`: install works, app starts, executor
   reaches it.
2. `test_sandbox_app_is_not_root`: the app does not run as root.
3. `test_sandbox_app_has_no_external_network`: from inside the app
   container, with proxy variables set by the app itself, the proxy,
   host-loopback services, and an external IP (1.1.1.1:443) are all
   unreachable; the executor can still reach the app.
4. `test_sandbox_mounts_only_checkout`: only the checkout is mounted.

## Host capability reference

The sandbox probes capabilities at startup
(`proofdeploy.sandbox.check_sandbox_capabilities`):

| Capability | This dev host (2026-10-10) | Notes |
|---|---|---|
| podman (rootful) | Yes | vfs + cgroupfs |
| Isolated `--internal` network | No | `podman network create` succeeds, but running a container on it fails: netavark `setns: Operation not permitted`. The probe runs a real container; network creation alone is not proof. |
| Userns remapping (`--uidmap`) | No | `newuidmap` gets EPERM writing `uid_map` in this sandbox. |
| Rootless podman | No | Same newuidmap EPERM. |
| `podman exec` | No | `crun: setns mnt: Operation not permitted`. Tests that need in-container checks must have the app report via HTTP. |
| `--net=none` | Yes | Full network isolation; the Unix-socket bridge carries executor traffic. |

On a host where netavark and userns work, the sandbox uses the
`--internal` bridge and `--uidmap 0:1:65536` automatically
(`network_mode="auto"`). No code changes are needed; the capability
probe selects the strongest available isolation.

## GitHub Actions (for Master to add)

The repo's GitHub token lacks the `workflow` scope, so the workflow
file cannot be created from here. Master can add
`.github/workflows/sandbox.yml` in the web UI:

```yaml
name: sandbox
on: [push, pull_request]
jobs:
  sandbox:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Install podman
        run: sudo apt-get update && sudo apt-get install -y podman
      - name: Run sandbox tests
        run: python -m pytest tests/test_sandbox.py -v
```

`ubuntu-latest` runners have working netavark and userns, so the
`isolated` network mode and `--uidmap` paths get exercised there.

## Unattended-run gate

The WO-5 brief requires the sandbox (rootless container, checkout-only
mount, network cut after install, no secrets in env) before any
unattended run. The sandbox enforces:

- Never `--net=host` for the app (`network_mode="host"` is refused).
- Never proxy variables in the app container environment.
- `require_userns=True` fails closed when userns remapping is
  unavailable. The default (`False`) logs a prominent warning and
  uses `--user 65534`; do not run unattended on an untrusted host
  in that mode.

Sandbox capability evidence (`proofdeploy.sandbox.sandbox_evidence`)
is available for the record so a reviewer can see what isolation was
actually in effect.
