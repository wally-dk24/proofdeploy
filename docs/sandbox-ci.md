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
file cannot be pushed from here. The file is committed on the branch
at `.github/workflows/sandbox-tests.yml`; Master can add it via the
web UI (copy the file contents into a new workflow file with the same
path).

## Unattended-run gate

The WO-5 brief requires the sandbox before any unattended run. The
sandbox enforces, fail-closed:

- Never `--net=host`: not for the app, and not for the install phase.
  The install runs untrusted install scripts on a bridge network
  (`pd-build`) with the proxy; when the host cannot create a bridge
  network, measured runs are refused.
- Never proxy variables in the app container environment.
- `require_userns=True` is the default: when the host cannot remap
  container root away from host root (`--uidmap`), the run is refused.
  The only way past is the explicit dev flag `allow_no_userns=True`,
  which falls back to `--user 65534` (nobody) and records the opt-out
  in the evidence record.

`assert_measurement_gate()` (in `proofdeploy.sandbox`) refuses a
measured run when podman is unavailable, userns remapping is
unavailable (without the explicit opt-out), or no bridge network can
be created for the install phase. The orchestrator calls it at the
start of every sandboxed measured run.

Every evidence record carries `sandbox_evidence`: the network mode
actually used, the userns mode actually used, whether the dev opt-out
was used, and the uid the app ran as.

## What the first CI run must show

Do not claim CI works until a real run log exists. The first real run
of `.github/workflows/sandbox-tests.yml` must show:

1. The sandbox tests ran (not skipped): look for the 5
   `test_sandbox_*` tests passing, which proves podman was present.
2. Which isolation modes the runner actually supports. The capability
   probe logs one of:
   - `userns remapping: --uidmap 0:1:65536` (userns works), and
     `isolated network: pd-isolated (--internal, no external route)`
     (netavark works); or
   - `WARNING: userns remapping unavailable ...` with
     `Explicit dev opt-out (allow_no_userns=True)` (the CI workflow
     passes the dev flag explicitly), and/or the `none+socket` bridge
     path.
3. Recent Ubuntu runners may restrict user namespaces; if the log shows
   the fallback path, measured runs on that runner would carry
   `userns_opt_out: true` in their records. That is honest and
   auditable, but not the full isolation story: prefer a runner (or a
   small cloud VM) where the probe shows `--uidmap` working before
   running measured units there.
