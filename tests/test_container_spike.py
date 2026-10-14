"""Container-spike tests: podman-free.

The parsing core (Dockerfile FROM/ARG/EXPOSE/install lines,
compose services, port determination) is pure and tested
directly. The command constructors are pure lists and tested as
data — the safety properties of the spike (``--net=none``,
``--cap-drop=ALL``, ``no-new-privileges``, snapshot-only build
context, no secret env) are assertions on those lists. The
per-side flow is driven through a FakePodman that records every
command it is handed, so a flow test can also assert the safety
shape of the commands the flow actually built.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import readiness.container_spike as cs

DIRECTUS_DOCKERFILE = """\
# syntax=docker/dockerfile:1.4
ARG NODE_VERSION=22
FROM node:${NODE_VERSION}-alpine AS builder
RUN apk --no-cache add python3 py3-setuptools build-base
WORKDIR /directus
COPY package.json .
COPY pnpm-lock.yaml .
RUN pnpm fetch
COPY --chown=node:node . .
RUN <<EOF
\tset -ex
\tpnpm install --recursive --offline --frozen-lockfile
\tnpm_config_workspace_concurrency=2 pnpm run build
EOF
FROM node:${NODE_VERSION}-alpine AS runtime
USER node
WORKDIR /directus
ENV DB_CLIENT="sqlite3" NODE_ENV="production"
EXPOSE 8055
CMD ["node", "docker-entrypoint.cjs"]
"""

VENDURE_COMPOSE = """\
name: vendure-monorepo
services:
    mariadb:
        image: 'mariadb:latest'
    mysql_8:
        image: mysql:8.0
    mysql_5:
        image: mysql:5.7
    postgres_12:
        image: postgres:12.3
    postgres_16:
        image: postgres:16
    postgres_15:
        image: postgres:15
    elasticsearch:
        image: docker.elastic.co/elasticsearch/elasticsearch:7.10.1
    redis:
        image: redis:7
"""

UNIT: dict[str, Any] = {"unit_id": "C3", "repo": "directus/directus"}
SIDE: dict[str, Any] = {"role": "C", "sha": "dc922b790a15949e8abfce9c1f4523d4e5f89492"}
USER_ARGS = cs.user_args_for(True)
BUILD_USER_ARGS = cs.build_user_args_for(True)


class FakePodman:
    """Records commands; answers like a cooperative podman."""

    def __init__(
        self,
        *,
        build_rc: int = 0,
        digests: list[str] | None = None,
        config: dict[str, Any] | None = None,
        runtimes: tuple[str, ...] = ("node",),
        start_rc: int = 0,
        running: bool = True,
        logs_text: str = "",
    ) -> None:
        self.base = ["podman", "--storage-driver", "vfs"]
        self.logs: list[str] = []
        self.build_rc = build_rc
        self.digests = digests if digests is not None else ["docker.io/library/node@sha256:abc123"]
        self.config = config if config is not None else {
            "ExposedPorts": {"8055/tcp": {}},
            "Entrypoint": None,
            "Cmd": ["node", "docker-entrypoint.cjs"],
            "Env": ["DB_CLIENT=sqlite3"],
        }
        self.runtimes = runtimes
        self.start_rc = start_rc
        self.running = running
        self.logs_text = logs_text
        self.built: list[list[str]] = []
        self.started: list[list[str]] = []
        self.removed: list[str] = []

    def ensure_build_network(self) -> None:
        return None

    def build(
        self, tag: str, dockerfile: Path, context: Path, build_user_args: list[str]
    ) -> cs.ProcResult:
        cmd = cs.build_command(self.base, tag, dockerfile, context, build_user_args)
        self.built.append(cmd)
        err = "" if self.build_rc == 0 else "step 9 failed\nerror building image\n"
        return cs.ProcResult(self.build_rc, "", err)

    def image_digests(self, ref: str) -> list[str]:
        return list(self.digests)

    def inspect_image(self, ref: str) -> dict[str, Any]:
        return {"Config": self.config}

    def runtime_available(self, image: str, binary: str) -> bool:
        return binary in self.runtimes

    def run(self, args: list[str], timeout: int) -> cs.ProcResult:
        self.started.append(args)
        err = "" if self.start_rc == 0 else "run failed badly\n"
        return cs.ProcResult(self.start_rc, "", err)

    def is_running(self, name: str) -> bool:
        return self.running

    def container_logs(self, name: str, tail: int = 100) -> str:
        return self.logs_text

    def remove(self, name: str) -> None:
        self.removed.append(name)


def _ready_prober(sock_path, path, timeout_s, is_running, logs):  # type: ignore[no-untyped-def]
    return 200, True


def _socket_ok(sock_path, timeout, logs):  # type: ignore[no-untyped-def]
    return None


def _snapshot(tmp_path: Path, files: dict[str, str]) -> Path:
    snap = tmp_path / "snapshot"
    snap.mkdir()
    for name, text in files.items():
        target = snap / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return snap


# ---------------------------------------------------------------------------
# Dockerfile parsing
# ---------------------------------------------------------------------------


def test_parse_dockerfile_directus_shape() -> None:
    info = cs.parse_dockerfile(DIRECTUS_DOCKERFILE)
    assert info.from_images == ["node:22-alpine"]
    assert info.install_lines == ["pnpm install --recursive --offline --frozen-lockfile"]
    assert info.exposed_ports == [8055]
    assert info.env["DB_CLIENT"] == "sqlite3"


def test_from_alias_and_scratch_are_not_images() -> None:
    info = cs.parse_dockerfile(
        "FROM scratch AS base\n"
        "FROM golang:1.22 AS build\n"
        "FROM build AS final\n"
        "FROM alpine:3.20\n"
    )
    assert info.from_images == ["golang:1.22", "alpine:3.20"]


def test_from_arg_without_default_stays_raw() -> None:
    info = cs.parse_dockerfile("ARG TAG\nFROM example/app:$TAG\n")
    assert info.from_images == ["example/app:$TAG"]


def test_expose_multiple_ports_and_suffixes() -> None:
    info = cs.parse_dockerfile("FROM x\nEXPOSE 80/tcp 443/udp\n")
    assert info.exposed_ports == [80, 443]


def test_run_continuation_install_line() -> None:
    info = cs.parse_dockerfile(
        "FROM x\nRUN pip install --require-hashes \\\n    -r requirements.txt\n"
    )
    assert len(info.install_lines) == 1
    assert " ".join(info.install_lines[0].split()) == (
        "pip install --require-hashes -r requirements.txt"
    )


def test_install_verdict_frozen_forms() -> None:
    for line in (
        "pnpm install --recursive --offline --frozen-lockfile",
        "npm ci --ignore-scripts",
        "yarn install --immutable",
        "pip install --require-hashes -r requirements.txt",
    ):
        frozen, evidence = cs.install_verdict(cs.DockerfileInfo(install_lines=[line]))
        assert frozen is True, line
        assert evidence == line


def test_install_verdict_unfrozen_forms() -> None:
    for line in ("npm install", "pip install flask", "pnpm install", "composer install"):
        frozen, evidence = cs.install_verdict(cs.DockerfileInfo(install_lines=[line]))
        assert frozen is False, line
        assert evidence == line


def test_install_verdict_none_without_install_lines() -> None:
    info = cs.parse_dockerfile("FROM x\nRUN apk --no-cache add python3\nRUN echo hi\n")
    assert info.install_lines == []
    assert cs.install_verdict(info) == (None, None)


def test_install_verdict_one_unfrozen_line_wins() -> None:
    info = cs.DockerfileInfo(
        install_lines=["pnpm install --frozen-lockfile", "pip install flask"]
    )
    frozen, evidence = cs.install_verdict(info)
    assert frozen is False
    assert evidence == "pip install flask"


# ---------------------------------------------------------------------------
# Compose parsing
# ---------------------------------------------------------------------------


def test_compose_vendure_shape_is_multi_service() -> None:
    info = cs.parse_compose(VENDURE_COMPOSE)
    assert len(info.services) == 8
    assert "mariadb" in info.services
    assert "postgres_16" in info.services


def test_compose_single_service_forms() -> None:
    info = cs.parse_compose(
        "services:\n  app:\n    build: ./app\n    ports:\n      - '8080:80'\n"
    )
    assert info.services == ["app"]
    assert info.build_context == "./app"
    assert info.port == 80


def test_compose_single_service_dict_build_and_env_port() -> None:
    info = cs.parse_compose(
        "services:\n"
        "  app:\n"
        "    build:\n"
        "      context: app\n"
        "      dockerfile: Dockerfile.dev\n"
        "    environment:\n"
        "      PORT: '9000'\n"
    )
    assert info.build_context == "app"
    assert info.build_dockerfile == "Dockerfile.dev"
    assert info.env_port == 9000


def test_compose_invalid_yaml_declares_no_services() -> None:
    assert cs.parse_compose("services: [unclosed\n").services == []
    assert cs.parse_compose("not a compose file").services == []


# ---------------------------------------------------------------------------
# Port determination
# ---------------------------------------------------------------------------


def test_determine_port_expose_beats_compose() -> None:
    dockerfile = cs.DockerfileInfo(exposed_ports=[8055])
    compose = cs.ComposeInfo(services=["app"], port=9999)
    assert cs.determine_port(dockerfile, [], compose) == (8055, "EXPOSE")


def test_determine_port_image_exposed_fallback() -> None:
    assert cs.determine_port(cs.DockerfileInfo(), [3000], None) == (3000, "EXPOSE")


def test_determine_port_compose_mapping() -> None:
    compose = cs.ComposeInfo(services=["app"], port=8081)
    assert cs.determine_port(None, [], compose) == (8081, "compose")


def test_determine_port_declared_env() -> None:
    dockerfile = cs.DockerfileInfo(env={"PORT": "4567"})
    assert cs.determine_port(dockerfile, [], None) == (4567, "$PORT")


def test_determine_port_never_guessed() -> None:
    assert cs.determine_port(cs.DockerfileInfo(), [], None) == (None, None)
    assert cs.determine_port(None, [], cs.ComposeInfo(services=["a"])) == (None, None)


# ---------------------------------------------------------------------------
# Command construction (the safety properties, as data)
# ---------------------------------------------------------------------------


def test_build_command_context_is_snapshot_only(tmp_path: Path) -> None:
    dockerfile = tmp_path / "Dockerfile"
    cmd = cs.build_command(["podman"], "pd-spike-x", dockerfile, tmp_path, BUILD_USER_ARGS)
    assert cmd == [
        "podman", "build",
        "--userns-uid-map", "0:1:65536", "--userns-gid-map", "0:1:65536",
        "--cap-drop=ALL", "--network", "pd-build",
        "-t", "pd-spike-x", "-f", str(dockerfile), str(tmp_path),
    ]
    assert "-v" not in cmd
    assert "--secret" not in " ".join(cmd)
    assert "--build-arg" not in cmd


def test_build_command_safety_shape(tmp_path: Path) -> None:
    # The build executes untrusted Dockerfile RUN steps: remapped
    # away from host root, capabilities dropped, and on the
    # explicit pd-build bridge network (the native install path's
    # network) — never podman's default, never the host's.
    cmd = cs.build_command(
        ["podman"], "pd-spike-x", tmp_path / "Dockerfile", tmp_path, BUILD_USER_ARGS
    )
    assert "--userns-uid-map" in cmd and "0:1:65536" in cmd
    assert "--userns-gid-map" in cmd
    assert "--cap-drop=ALL" in cmd
    assert cmd[cmd.index("--network") + 1] == "pd-build"
    assert "--net=host" not in cmd and "host" not in cmd
    assert "--privileged" not in cmd


def test_build_user_args_modes() -> None:
    remap = cs.build_user_args_for(True)
    assert "--userns-uid-map" in remap and "--userns-gid-map" in remap
    assert "0:1:65536" in remap and "--cap-drop=ALL" in remap
    no_remap = cs.build_user_args_for(False)
    assert "--userns-uid-map" not in no_remap
    assert "--cap-drop=ALL" in no_remap  # dropped even in fallback mode


def test_run_command_safety_shape(tmp_path: Path) -> None:
    cmd = cs.run_command(
        ["podman"], "pd-spike-x", "img", USER_ARGS, tmp_path, {}, "exec app"
    )
    assert "--net=none" in cmd
    assert "--cap-drop=ALL" in cmd
    assert "--security-opt=no-new-privileges" in cmd
    assert "--uidmap" in cmd and "0:1:65536" in cmd
    assert "--privileged" not in cmd
    assert "--network" not in cmd and "--net=host" not in cmd
    assert cmd.count("-v") == 1  # the socket dir is the only mount
    assert "-e" not in cmd  # no env passed when the source is not $PORT


def test_run_command_env_is_only_port(tmp_path: Path) -> None:
    env = cs.run_env("$PORT", 8080)
    assert env == {"PORT": "8080"}
    assert cs.run_env("EXPOSE", 8055) == {}
    assert cs.run_env("compose", 80) == {}
    cmd = cs.run_command(["podman"], "n", "img", USER_ARGS, tmp_path, env, "exec app")
    assert cmd.count("-e") == 1
    assert "PORT=8080" in cmd


def test_user_args_both_modes_carry_cap_drop() -> None:
    remap = cs.user_args_for(True)
    assert "--uidmap" in remap and "--cap-drop=ALL" in remap
    assert "--security-opt=no-new-privileges" in remap
    fallback = cs.user_args_for(False, allow_no_userns_fallback=True)
    assert "--user" in fallback and "65534" in fallback
    assert "--cap-drop=ALL" in fallback
    assert "--security-opt=no-new-privileges" in fallback


def test_userns_refused_by_default_without_remap() -> None:
    # Fail closed, like the sandbox: no remap and no explicit dev
    # flag is a refusal, not a silent fallback.
    with pytest.raises(cs.UsernsUnavailableError, match="refuses to build or run"):
        cs.user_args_for(False)
    with pytest.raises(cs.UsernsUnavailableError):
        cs.user_args_for(False, allow_no_userns_fallback=False)


def test_spike_repo_refuses_without_userns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    caps = SimpleNamespace(
        podman=True, podman_version="test", userns_remap=False,
        reason="userns probe failed: test double",
    )
    monkeypatch.setattr(cs, "check_sandbox_capabilities", lambda: caps)
    manifest = {"units": [dict(UNIT, sides=[SIDE])]}
    with pytest.raises(cs.UsernsUnavailableError):
        cs.spike_repo(manifest, "directus/directus", tmp_path, 1)


# ---------------------------------------------------------------------------
# Per-side flow (FakePodman)
# ---------------------------------------------------------------------------


def test_side_directus_ready(tmp_path: Path) -> None:
    snap = _snapshot(tmp_path, {"Dockerfile": DIRECTUS_DOCKERFILE})
    fake = FakePodman()
    logs: list[str] = []
    row = cs.spike_side_from_snapshot(
        fake, snap, UNIT, SIDE, "/server/ping", 2, USER_ARGS, logs,  # type: ignore[arg-type]
        prober=_ready_prober,
        socket_wait=_socket_ok,
        build_user_args=BUILD_USER_ARGS,
    )
    assert row["kind"] == "container-spike"
    assert row["status"] == "READY"
    assert row["typed_reason"] is None
    assert row["port"] == 8055
    assert row["port_source"] == "EXPOSE"
    assert row["install_frozen"] is True
    assert row["install_evidence"] == "pnpm install --recursive --offline --frozen-lockfile"
    assert row["from_digests"] == ["docker.io/library/node@sha256:abc123"]
    assert row["network_mode"] == "none+socket"
    assert row["userns_fallback"] is False
    assert row["attempt"] == 2
    assert len(fake.built) == 1
    started = fake.started[-1]
    assert "--net=none" in started
    assert "--cap-drop=ALL" in started
    wrapped = started[-1]
    assert "/pd-sock/.pd-bridge.cjs /pd-sock/app.sock 8055" in wrapped
    assert "exec node docker-entrypoint.cjs" in wrapped
    assert fake.removed == ["pd-spike-c3-c-a2"]


def test_side_userns_fallback_recorded_on_row(tmp_path: Path) -> None:
    # Behind the explicit dev flag the side still runs (as nobody),
    # and the row says so: userns_fallback is on EVERY row of such
    # a run, this READY one included.
    snap = _snapshot(tmp_path, {"Dockerfile": DIRECTUS_DOCKERFILE})
    fake = FakePodman()
    row = cs.spike_side_from_snapshot(
        fake, snap, UNIT, SIDE, "/server/ping", 1,
        cs.user_args_for(False, allow_no_userns_fallback=True), [],  # type: ignore[arg-type]
        prober=_ready_prober,
        socket_wait=_socket_ok,
        build_user_args=cs.build_user_args_for(False),
        userns_fallback=True,
    )
    assert row["status"] == "READY"
    assert row["userns_fallback"] is True
    started = fake.started[-1]
    assert "--user" in started and "65534" in started


def test_side_vendure_multi_service_typed_and_never_built(tmp_path: Path) -> None:
    snap = _snapshot(tmp_path, {"docker-compose.yml": VENDURE_COMPOSE})
    fake = FakePodman()
    unit = {"unit_id": "C1", "repo": "vendure-ecommerce/vendure"}
    side = {"role": "C", "sha": "4cbbc790118786a17721954c3a4d739810bd2632"}
    row = cs.spike_side_from_snapshot(
        fake, snap, unit, side, "/health", 2, USER_ARGS, [],  # type: ignore[arg-type]
        prober=_ready_prober,
        build_user_args=BUILD_USER_ARGS,
    )
    assert row["status"] == "BUILD_FAILED"
    assert row["typed_reason"] == "multi-service not in spike"
    assert row["from_digests"] == []
    assert fake.built == []
    assert fake.started == []


def test_side_no_container_file(tmp_path: Path) -> None:
    snap = _snapshot(tmp_path, {"README.md": "nothing here\n"})
    row = cs.spike_side_from_snapshot(
        FakePodman(), snap, UNIT, SIDE, "/", 1, USER_ARGS, [],  # type: ignore[arg-type]
        prober=_ready_prober,
        build_user_args=BUILD_USER_ARGS,
    )
    assert row["status"] == "BUILD_FAILED"
    assert row["typed_reason"] == "no root Dockerfile or compose file in snapshot"


def test_side_dockerfile_beats_compose_at_root(tmp_path: Path) -> None:
    snap = _snapshot(
        tmp_path, {"Dockerfile": DIRECTUS_DOCKERFILE, "docker-compose.yml": VENDURE_COMPOSE}
    )
    fake = FakePodman()
    row = cs.spike_side_from_snapshot(
        fake, snap, UNIT, SIDE, "/server/ping", 1, USER_ARGS, [],  # type: ignore[arg-type]
        prober=_ready_prober,
        socket_wait=_socket_ok,
        build_user_args=BUILD_USER_ARGS,
    )
    assert row["status"] == "READY"
    assert len(fake.built) == 1


def test_side_build_failure_records_digests_and_tail(tmp_path: Path) -> None:
    snap = _snapshot(tmp_path, {"Dockerfile": DIRECTUS_DOCKERFILE})
    fake = FakePodman(build_rc=1)
    row = cs.spike_side_from_snapshot(
        fake, snap, UNIT, SIDE, "/server/ping", 1, USER_ARGS, [],  # type: ignore[arg-type]
        prober=_ready_prober,
        build_user_args=BUILD_USER_ARGS,
    )
    assert row["status"] == "BUILD_FAILED"
    assert row["typed_reason"] == "container build failed: error building image"
    assert row["from_digests"] == ["docker.io/library/node@sha256:abc123"]
    assert row["install_frozen"] is True


def test_side_port_undetermined_is_typed_not_guessed(tmp_path: Path) -> None:
    snap = _snapshot(tmp_path, {"Dockerfile": "FROM python:3.12\nRUN pip install flask\n"})
    fake = FakePodman(config={"ExposedPorts": {}, "Entrypoint": None, "Cmd": ["python", "app.py"]})
    row = cs.spike_side_from_snapshot(
        fake, snap, UNIT, SIDE, "/", 1, USER_ARGS, [],  # type: ignore[arg-type]
        prober=_ready_prober,
        build_user_args=BUILD_USER_ARGS,
    )
    assert row["status"] == "BUILD_FAILED"
    assert "cannot determine container port" in str(row["typed_reason"])
    assert row["port"] is None
    assert row["install_frozen"] is False
    assert row["install_evidence"] == "pip install flask"


def test_side_readiness_timeout(tmp_path: Path) -> None:
    snap = _snapshot(tmp_path, {"Dockerfile": DIRECTUS_DOCKERFILE})
    fake = FakePodman()

    def timeout_prober(sock_path, path, timeout_s, is_running, logs):  # type: ignore[no-untyped-def]
        return None, True

    row = cs.spike_side_from_snapshot(
        fake, snap, UNIT, SIDE, "/server/ping", 1, USER_ARGS, [],  # type: ignore[arg-type]
        prober=timeout_prober,
        socket_wait=_socket_ok,
        build_user_args=BUILD_USER_ARGS,
    )
    assert row["status"] == "START_FAILED"
    assert row["typed_reason"] == "readiness timeout"


def test_side_container_exited_carries_log_tail(tmp_path: Path) -> None:
    snap = _snapshot(tmp_path, {"Dockerfile": DIRECTUS_DOCKERFILE})
    fake = FakePodman(logs_text="booting\nkaboom: database gone\n")

    def exited_prober(sock_path, path, timeout_s, is_running, logs):  # type: ignore[no-untyped-def]
        return None, False

    row = cs.spike_side_from_snapshot(
        fake, snap, UNIT, SIDE, "/server/ping", 1, USER_ARGS, [],  # type: ignore[arg-type]
        prober=exited_prober,
        socket_wait=_socket_ok,
        build_user_args=BUILD_USER_ARGS,
    )
    assert row["status"] == "START_FAILED"
    assert row["typed_reason"] == "container exited before readiness: kaboom: database gone"


def test_side_image_without_cmd(tmp_path: Path) -> None:
    snap = _snapshot(tmp_path, {"Dockerfile": DIRECTUS_DOCKERFILE})
    fake = FakePodman(config={"ExposedPorts": {"8055/tcp": {}}, "Entrypoint": None, "Cmd": None})
    row = cs.spike_side_from_snapshot(
        fake, snap, UNIT, SIDE, "/server/ping", 1, USER_ARGS, [],  # type: ignore[arg-type]
        prober=_ready_prober,
        build_user_args=BUILD_USER_ARGS,
    )
    assert row["status"] == "START_FAILED"
    assert row["typed_reason"] == "image declares no ENTRYPOINT or CMD"


def test_side_no_bridge_runtime(tmp_path: Path) -> None:
    snap = _snapshot(tmp_path, {"Dockerfile": DIRECTUS_DOCKERFILE})
    fake = FakePodman(runtimes=())
    row = cs.spike_side_from_snapshot(
        fake, snap, UNIT, SIDE, "/server/ping", 1, USER_ARGS, [],  # type: ignore[arg-type]
        prober=_ready_prober,
        build_user_args=BUILD_USER_ARGS,
    )
    assert row["status"] == "START_FAILED"
    assert row["typed_reason"] == "no bridge runtime (python3 or node) in image"


def test_side_compose_single_service_uses_compose_port(tmp_path: Path) -> None:
    snap = _snapshot(
        tmp_path,
        {
            "compose.yml": (
                "services:\n  app:\n    build:\n      context: app\n"
                "    ports:\n      - '9999:8081'\n"
            ),
            "app/Dockerfile": "FROM python:3.12\nRUN pip install --require-hashes -r r.txt\n",
        },
    )
    fake = FakePodman(
        config={"ExposedPorts": {}, "Entrypoint": None, "Cmd": ["python", "app.py"]},
        runtimes=("python3",),
    )
    row = cs.spike_side_from_snapshot(
        fake, snap, UNIT, SIDE, "/", 1, USER_ARGS, [],  # type: ignore[arg-type]
        prober=_ready_prober,
        socket_wait=_socket_ok,
        build_user_args=BUILD_USER_ARGS,
    )
    assert row["status"] == "READY"
    assert row["port"] == 8081
    assert row["port_source"] == "compose"
    assert fake.built and fake.built[0][-1].endswith("app")


def test_side_compose_image_only_is_out_of_scope(tmp_path: Path) -> None:
    snap = _snapshot(
        tmp_path,
        {"compose.yml": "services:\n  app:\n    image: example/app:1\n"},
    )
    row = cs.spike_side_from_snapshot(
        FakePodman(), snap, UNIT, SIDE, "/", 1, USER_ARGS, [],  # type: ignore[arg-type]
        prober=_ready_prober,
        build_user_args=BUILD_USER_ARGS,
    )
    assert row["status"] == "BUILD_FAILED"
    assert "no build section" in str(row["typed_reason"])


def test_spike_side_fetch_failure_is_a_harness_error_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    def boom(clone: Path, sha: str) -> None:
        raise RuntimeError("git fetch failed: exit 128")

    monkeypatch.setattr(cs, "fetch_sha", boom)
    podman = cs.Podman([], ["podman"])
    row = cs.spike_side(
        podman, tmp_path, UNIT, SIDE, "/", 1, tmp_path, USER_ARGS, BUILD_USER_ARGS
    )
    assert row["status"] == "BUILD_FAILED"
    assert row["typed_reason"] == "harness error: git fetch failed: exit 128"
    assert (tmp_path / "spike-logs" / "spike-C3-clean-a1.log").is_file()


def test_materialize_snapshot_from_real_git(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t"}
    import os

    full_env = {**os.environ, **env}
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, env=full_env)
    subprocess.run(["git", "add", "."], cwd=repo, check=True, env=full_env)
    subprocess.run(["git", "commit", "-qm", "x"], cwd=repo, check=True, env=full_env)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True,
        check=True, env=full_env,
    ).stdout.strip()
    dest = tmp_path / "snap"
    cs.materialize_snapshot(repo, sha, dest)
    assert (dest / "Dockerfile").read_text(encoding="utf-8") == "FROM scratch\n"


def test_rerun_targets_only_non_ready(tmp_path: Path) -> None:
    path = tmp_path / "rows.json"
    path.write_text(
        json.dumps({"rows": [
            {"unit": "C3", "role": "C", "status": "READY"},
            {"unit": "C4", "role": "C", "status": "BUILD_FAILED"},
        ]}),
        encoding="utf-8",
    )
    assert cs._rerun_targets(path) == [("C4", "C")]


def test_main_unknown_repo_exits_2(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"units": []}), encoding="utf-8")
    rc = cs.main([
        "--repo", "nope/nope", "--out", str(tmp_path / "rows.json"),
        "--workdir", str(tmp_path / "work"), "--manifest", str(manifest),
    ])
    assert rc == 2
