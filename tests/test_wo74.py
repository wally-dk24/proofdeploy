"""WO-7.4 (Addendum 41): the sandbox capability probe must be reliable.

The defect (pre-existing, found by the reviewer on his host during
the WO-7.3 acceptance): ``check_sandbox_capabilities()`` made ONE
TCP connect right after ``podman run -d`` on a fresh ``--internal``
network. On a host where isolated networking works (a manual
container answers 200), the probe still reported isolated only 1
time in 4 — a startup race: the probe app is not listening yet when
the single connect is refused, and the refusal was read as "the host
cannot do isolated networks." With a POLLED connect the same host
was 3 of 3. Separately, a leftover ``pd-capability-probe`` network
(from a crashed earlier probe) made every later probe report
"already exists" -> unavailable until someone removed it by hand.

The WO-7.4 rules proven here:

- The probe POLLS the connect until a 10 s deadline (0.3 s
  interval): an app that starts listening a moment late still
  proves the capability.
- Each probe uses a UNIQUE network name, so a stale network left
  behind by an earlier probe can never poison this one.
- Cleanup (stop the probe container, remove the probe network)
  happens in a ``finally`` — even when the probe itself raises.
- When the capability is absent, the recorded reason carries the
  connect attempts and the last error, so a refusal can be told
  apart from a race that polling should have absorbed.

Fail-before: on the pre-WO-7.4 code the single connect is refused
twice-then-accepted -> not capable; the stale fixed-name network
fails the create; a raising probe leaks its network; and the
unreachable reason names neither attempts nor the last error.
"""
import json
import subprocess

import proofdeploy.sandbox as sb_mod


def _completed(cmd, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(cmd, returncode, stdout, stderr)


class _FakePodman:
    """Feature-keyed fake for subprocess.run in the capability probe.

    Keys on command SHAPE (network create / run -d / inspect / stop),
    never on the probe's names, so the same world runs against the
    pre- and post-fix probe. ``networks`` is the set of networks that
    exist; it can be seeded with stale leftovers.
    """

    def __init__(self, *, stale_networks=(), fail_isolated_run=None):
        self.calls = []
        self.networks = set(stale_networks)
        self.isolated_created = []  # --internal network names, in order
        self.run_attempts = []  # (container, network) per run -d
        self.container_net = {}
        self.fail_isolated_run = fail_isolated_run

    def __call__(self, cmd, **kwargs):
        cmd = [str(c) for c in cmd]
        self.calls.append(cmd)
        if "--version" in cmd:
            return _completed(cmd, 0, "podman version 4.9.3\n", "")
        if "network" in cmd and "create" in cmd:
            name = cmd[-1]
            if name in self.networks:
                return _completed(
                    cmd, 1, "",
                    f"Error: network with name {name} already exists",
                )
            self.networks.add(name)
            if "--internal" in cmd:
                self.isolated_created.append(name)
            return _completed(cmd, 0, name + "\n", "")
        if "network" in cmd and "rm" in cmd:
            self.networks.discard(cmd[-1])
            return _completed(cmd, 0, "", "")
        if "inspect" in cmd:
            name = cmd[cmd.index("inspect") + 1]
            net = self.container_net.get(name, "")
            payload = [{
                "NetworkSettings": {
                    "Networks": {net: {"IPAddress": "10.89.0.2"}}
                }
            }]
            return _completed(cmd, 0, json.dumps(payload), "")
        if "run" in cmd and "-d" in cmd:
            name = cmd[cmd.index("--name") + 1]
            net = cmd[cmd.index("--net") + 1]
            self.run_attempts.append((name, net))
            if self.fail_isolated_run is not None:
                raise self.fail_isolated_run
            self.container_net[name] = net
            return _completed(cmd, 0, "", "")
        # stop, the --uidmap probe, the build-network run: succeed.
        return _completed(cmd, 0, "", "")


class _FakeConn:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def close(self):
        pass


def _flaky_socket(failures):
    """socket.create_connection stub: refuse ``failures`` times, then
    connect. ``failures=None`` refuses forever."""
    state = {"calls": 0}

    def create_connection(address, timeout=None, *args, **kwargs):
        state["calls"] += 1
        if failures is None or state["calls"] <= failures:
            raise ConnectionRefusedError(111, "Connection refused")
        return _FakeConn()

    return create_connection, state


def _patch_probe(monkeypatch, fake_podman, create_connection):
    monkeypatch.setattr(sb_mod.shutil, "which", lambda _: "/usr/bin/podman")
    monkeypatch.setattr(sb_mod.subprocess, "run", fake_podman)
    monkeypatch.setattr(
        sb_mod.socket, "create_connection", create_connection
    )


def test_probe_polls_connect_until_app_listens(monkeypatch):
    """Refused twice, then accepted: the capability is real, and only
    polling sees it. The pre-fix single connect reported it absent."""
    podman = _FakePodman()
    connect, state = _flaky_socket(failures=2)
    _patch_probe(monkeypatch, podman, connect)
    caps = sb_mod.check_sandbox_capabilities()
    assert state["calls"] == 3
    assert caps.isolated_network is True


def test_probe_stale_network_name_still_probes(monkeypatch):
    """A leftover network carrying the legacy fixed probe name must
    not poison this probe: a unique name per probe routes around it."""
    podman = _FakePodman(stale_networks={"pd-capability-probe"})
    connect, _state = _flaky_socket(failures=0)
    _patch_probe(monkeypatch, podman, connect)
    caps = sb_mod.check_sandbox_capabilities()
    assert caps.isolated_network is True


def test_probe_cleans_up_on_exception(monkeypatch):
    """When the probe's container start raises, the probe network and
    container are still removed — no leftovers for the next probe."""
    podman = _FakePodman(
        fail_isolated_run=OSError("podman run exploded")
    )
    connect, _state = _flaky_socket(failures=0)
    _patch_probe(monkeypatch, podman, connect)
    caps = sb_mod.check_sandbox_capabilities()
    assert caps.isolated_network is False
    container, net = podman.run_attempts[-1]
    assert any(
        "network" in c and "rm" in c and net in c for c in podman.calls
    ), "probe network was not removed after the exception"
    assert any(
        "stop" in c and container in c for c in podman.calls
    ), "probe container was not stopped after the exception"


def test_probe_reason_records_attempts_and_last_error(monkeypatch):
    """A genuinely unreachable probe app: the reason must show the
    polling happened — the attempt count and the last error — so the
    refusal is evidence, not a shrug."""
    # Shrink the poll window when the knobs exist (post-fix code);
    # on the pre-fix code these are absent and the assertions below
    # fail on the reason text instead.
    monkeypatch.setattr(
        sb_mod, "_PROBE_CONNECT_DEADLINE_S", 0.35, raising=False
    )
    monkeypatch.setattr(
        sb_mod, "_PROBE_CONNECT_INTERVAL_S", 0.05, raising=False
    )
    podman = _FakePodman()
    connect, state = _flaky_socket(failures=None)
    _patch_probe(monkeypatch, podman, connect)
    caps = sb_mod.check_sandbox_capabilities()
    assert caps.isolated_network is False
    assert state["calls"] > 1, "the connect was not polled"
    assert "attempt" in caps.reason
    assert "Connection refused" in caps.reason


def test_probe_connect_attempt_is_time_boxed(monkeypatch):
    """A connect into a black hole (no RST — the attempt hangs until
    its timeout) must not eat the whole deadline in one attempt:
    each attempt is time-boxed, so the poll actually polls. (The
    first WO-7.4 cut set the attempt timeout to the remaining
    deadline and the GitHub focus run reported "1 connect attempts
    over 10 s, last error: timed out".)"""
    monkeypatch.setattr(
        sb_mod, "_PROBE_CONNECT_DEADLINE_S", 1.2, raising=False
    )
    monkeypatch.setattr(
        sb_mod, "_PROBE_CONNECT_INTERVAL_S", 0.05, raising=False
    )
    timeouts = []

    def hanging(address, timeout=None, *args, **kwargs):
        timeouts.append(timeout)
        raise TimeoutError("timed out")

    podman = _FakePodman()
    _patch_probe(monkeypatch, podman, hanging)
    caps = sb_mod.check_sandbox_capabilities()
    assert caps.isolated_network is False
    assert len(timeouts) > 1, "the hanging connect was not re-polled"
    assert all(t <= 1.0 for t in timeouts), (
        f"an attempt was given the whole deadline: {timeouts}"
    )
    assert "timed out" in caps.reason


def test_probe_uses_unique_network_name_per_probe(monkeypatch):
    """Two consecutive probes must not share a network name — a
    shared fixed name is exactly what let one crashed probe poison
    every later one."""
    podman = _FakePodman()
    connect, _state = _flaky_socket(failures=0)
    _patch_probe(monkeypatch, podman, connect)
    first = sb_mod.check_sandbox_capabilities()
    second = sb_mod.check_sandbox_capabilities()
    assert first.isolated_network is True
    assert second.isolated_network is True
    assert len(podman.isolated_created) == 2
    assert podman.isolated_created[0] != podman.isolated_created[1]
