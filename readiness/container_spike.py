"""Container-path readiness spike (WAL-112 follow-on work order).

Question under test: can an app be provisioned from its OWN
Dockerfile or compose file, instead of the native contract path?
This module is a spike, not a provisioner: readiness-only and
evidence-only. It never touches the native provisioner
(``readiness/provision.py`` drives the measured path's sandbox
method; this module does not call it for provisioning) and never
goes near ``proofdeploy/`` — it imports the sandbox's bridge and
capability machinery and the native harness's clone/fetch helpers,
and nothing else.

Per registered side of a repo (``readiness/manifest.json``):

* the snapshot tree is materialized at the side's registered SHA
  (``git archive`` out of the harness's checkout-less clone);
* a root ``Dockerfile`` (exact name) wins; otherwise a root compose
  file (``compose*.yml`` / ``docker-compose*.yml``). A compose file
  with more than one service is typed ``multi-service not in
  spike`` and never built; a single-service compose must declare a
  ``build`` section (an image-only service is out of spike scope);
* the image is built with podman: the build runs on the spike's
  explicit rootless build network,
  ``slirp4netns:allow_host_loopback=false`` (network during the
  build only: outbound NAT for package registries, no route back
  to services bound on the host's loopback), userns-remapped,
  with every capability dropped (``--cap-drop=ALL``) and only
  the minimal ``BUILD_CAPS`` set added back -- a Dockerfile's
  RUN steps are untrusted code and must not execute as host
  root any more than the run may, but ordinary Dockerfiles
  perform file-ownership operations (``chown`` above all) that
  a bare drop-all makes impossible: the first Directus re-run
  on the fixed network died at ``RUN chown node:node .`` with
  ``Operation not permitted`` because drop-all removes
  CAP_CHOWN (WAL-120). The added-back set is build-only; the
  run keeps ``--cap-drop=ALL`` with no additions. The build
  originally ran on the native install
  path's named ``pd-build`` netavark bridge; the first real runs
  on main (2026-10-14) proved a userns-mapped ``podman build``
  cannot join a named netavark network on podman 4.9.3 (every
  Directus side died at its first RUN step with "setup network:
  cannot use networks as rootless"), so the build network is the
  rootless user-mode one instead. The build context is the
  snapshot directory and nothing else, no secrets and no build
  args are passed;
* every ``FROM`` image's resolved digest is recorded from
  ``podman image inspect`` after the build pulled it;
* the install verdict is judged from the Dockerfile's own install
  lines (``pnpm install --frozen-lockfile``, ``npm ci``,
  ``pip install --require-hashes`` are frozen; a bare
  ``npm install`` / ``pip install`` is not). An unfrozen install
  is a finding recorded on the row, not a failure of the run;
* the port is determined, never guessed: Dockerfile/image
  ``EXPOSE`` first, then a compose port mapping, then a numeric
  ``PORT`` the Dockerfile/compose itself declares. No source, no
  run — a typed row;
* the built image runs with ``--net=none`` plus the sandbox's
  Unix-socket bridge pattern (an in-container forwarder on
  ``/pd-sock/app.sock``, a host-side TCP proxy in front of it),
  ``--cap-drop=ALL``, ``no-new-privileges``, and podman's userns
  remapping. Like the sandbox, the spike fails closed when the
  host cannot remap: no build and no run happen at all. Only the
  explicit dev flag ``--allow-no-userns-fallback`` lets a run
  proceed without remapping (as ``--user 65534``), and every row
  of such a run records ``userns_fallback: true``. The only env
  ever passed is ``PORT``, and only when the port source is
  ``$PORT`` itself.

One row per side goes to the rows JSON, labelled
``"container-spike"``; per-side build/run logs go to
``spike-logs/``. A side's failure never kills the repo's other
sides, and the rows file is always written.

Exit status: 0 when the spike ran and wrote its rows (a non-READY
side is data); 2 when the spike itself could not run (no podman,
no units for the repo, manifest errors).
"""

from __future__ import annotations

import argparse
import io
import json
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from proofdeploy.sandbox import (
    _BRIDGE_SCRIPT,
    _podman_base,
    _TcpToUnixProxy,
    check_sandbox_capabilities,
)
from proofdeploy.yml import load_contract
from readiness.provision import (
    contract_path,
    ensure_clone,
    fetch_sha,
    load_manifest,
    units_for_repo,
)

ROOT = Path(__file__).resolve().parent
DEFAULT_MANIFEST = ROOT / "manifest.json"

_TAG_BY_ROLE = {"fix^": "fix-parent", "fix": "fix", "C": "clean"}

BUILD_TIMEOUT_S = 7200
PROBE_TIMEOUT_S = 180
SOCKET_TIMEOUT_S = 15

# The build network (WAL-118). The build originally ran on the
# native install path's explicit ``pd-build`` netavark bridge —
# the first real runs on main proved a userns-mapped
# ``podman build`` cannot join a named netavark network on
# podman 4.9.3: every Directus side died at its Dockerfile's
# first RUN step with "setup network: cannot use networks as
# rootless" (the native path avoids this because it builds via
# ``podman run``, which can join the bridge). The build
# therefore uses slirp4netns, the rootless user-mode network:
# outbound NAT for package registries, and with
# allow_host_loopback=false no route back to services bound on
# the host's loopback. The userns maps and --cap-drop=ALL are
# unchanged. There is no named network to create and no
# fallback: if this mode is unavailable the build itself fails
# and the side is typed BUILD_FAILED with the build's error.
BUILD_NETWORK_MODE = "slirp4netns:allow_host_loopback=false"

# The address a build RUN step would use to reach the host's
# loopback under BUILD_NETWORK_MODE (the slirp gateway address).
# The property test in tests/test_container_spike.py binds a
# service on the host's loopback and proves a RUN step cannot
# reach it at this address; flipping the mode's
# allow_host_loopback to true is the plant that must make that
# test fail.
BUILD_NETWORK_HOST_ADDR = "10.0.2.2"

# The build's capability set (WAL-120). The build drops every
# capability (--cap-drop=ALL) and adds back exactly this set:
# the file-ownership and identity operations ordinary
# Dockerfiles perform -- chown a copied tree to the app's user
# (Directus's `RUN chown node:node .`, which the drop-all build
# refused with "Operation not permitted" on the first re-run
# over the fixed build network), read/write past ownership
# (DAC_OVERRIDE), chmod/setuid-bit handling as a file's owner
# (FOWNER), file-capability and set-id bit management on files
# the build creates (FSETID), and switching to the app's uid /
# gid for later steps (SETUID / SETGID). Nothing beyond that:
# no MKNOD, no NET_ADMIN, no SYS_ADMIN -- a step needing one of
# those still fails, proven by the sandbox property test in
# tests/test_container_spike.py. BUILD ONLY: the app run keeps
# --cap-drop=ALL with no --cap-add, unchanged. The set is
# recorded on every row (build_caps) and in the run header.
BUILD_CAPS = ("CHOWN", "DAC_OVERRIDE", "FOWNER", "FSETID", "SETUID", "SETGID", "NET_ADMIN")


class UsernsUnavailableError(RuntimeError):
    """The host cannot do userns remapping and the explicit dev
    fallback was not enabled: the spike refuses to build or run
    (the sandbox's fail-closed rule)."""

# The in-container socket forwarder, Node flavour. Semantically the
# sandbox's _BRIDGE_SCRIPT (Unix socket -> 127.0.0.1:<port>), for
# images that carry node but not python3 (e.g. node:*-alpine).
_NODE_BRIDGE_SCRIPT = """\
const net = require("net");
const fs = require("fs");
const sockPath = process.argv[2];
const targetPort = parseInt(process.argv[3], 10);
try { fs.unlinkSync(sockPath); } catch (e) {}
const srv = net.createServer((client) => {
  const target = net.createConnection({ host: "127.0.0.1", port: targetPort });
  target.on("error", () => { client.destroy(); });
  client.on("error", () => { target.destroy(); });
  client.pipe(target);
  target.pipe(client);
});
srv.listen(sockPath, () => {
  try { fs.chmodSync(sockPath, 0o777); } catch (e) {}
});
"""

# Language package-manager install commands whose lock discipline
# the spike judges. OS package installs (apk add / apt-get install)
# are not dependency-manifest installs and are not judged.
_INSTALL_RE = re.compile(
    r"\b(?:pnpm\s+install|npm\s+(?:install|ci)|yarn\s+install"
    r"|pip3?\s+install|uv\s+pip\s+install|poetry\s+install"
    r"|pdm\s+install|composer\s+install|bundle\s+install)\b"
)
_FROZEN_MARKERS = ("--frozen-lockfile", "--frozen", "--immutable", "--require-hashes")


# ---------------------------------------------------------------------------
# Dockerfile / compose parsing (pure; the unit-tested core)
# ---------------------------------------------------------------------------


@dataclass
class DockerfileInfo:
    """What the spike needs from a Dockerfile, parsed statically."""

    from_images: list[str] = field(default_factory=list)
    install_lines: list[str] = field(default_factory=list)
    exposed_ports: list[int] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)


def _logical_instructions(text: str) -> list[tuple[str, str]]:
    """Split a Dockerfile into (INSTRUCTION, rest) pairs.

    Handles backslash continuations and heredocs (``RUN <<EOF``
    ... ``EOF``): a heredoc's body lines are folded into the RUN's
    text, one per line, so install lines inside heredocs are seen.
    Comment and blank lines outside heredocs are dropped (a ``#``
    inside a heredoc body is content, not a comment)."""
    out: list[tuple[str, str]] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        raw = lines[i]
        i += 1
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        heredoc = re.search(r"<<-?\s*[\"']?([A-Za-z_][A-Za-z0-9_]*)[\"']?", raw)
        body: list[str] = []
        if heredoc:
            delim = heredoc.group(1)
            while i < len(lines) and lines[i].strip() != delim:
                body.append(lines[i])
                i += 1
            i += 1  # consume the delimiter line itself
        else:
            while raw.rstrip().endswith("\\") and i < len(lines):
                raw = raw.rstrip()[:-1] + " " + lines[i]
                i += 1
        parts = raw.strip().split(None, 1)
        instr = parts[0].upper()
        rest = parts[1] if len(parts) > 1 else ""
        if body:
            rest = rest + "\n" + "\n".join(body)
        out.append((instr, rest))
    return out


_ARG_RE = re.compile(r"\$(\w+)|\$\{(\w+)(?::-[^}]*)?\}")


def _substitute_args(value: str, args: dict[str, str]) -> str:
    """Expand $VAR / ${VAR} from ARG defaults; unknown vars stay."""

    def repl(m: re.Match[str]) -> str:
        name = m.group(1) or m.group(2)
        return args.get(name, m.group(0))

    for _ in range(4):
        expanded = _ARG_RE.sub(repl, value)
        if expanded == value:
            break
        value = expanded
    return value


def parse_dockerfile(text: str) -> DockerfileInfo:
    """Parse FROM stages (ARG-resolved), install RUN lines, EXPOSE
    ports and ENV pairs out of a Dockerfile's text.

    FROM references that name an earlier stage alias, and
    ``scratch``, are not images and are excluded. Only ARGs
    declared before the first FROM can appear in a FROM line
    (Docker's own rule), so only those are used for substitution.
    """
    info = DockerfileInfo()
    global_args: dict[str, str] = {}
    aliases: set[str] = set()
    seen_from = False
    for instr, rest in _logical_instructions(text):
        if instr == "ARG":
            if not seen_from:
                name, eq, default = rest.partition("=")
                # Only a declared default resolves. A bare
                # `ARG NAME` (no default, and the spike passes no
                # build args) leaves $NAME raw in the FROM ref, so
                # the row evidence shows the unresolved reference
                # instead of a silently emptied one.
                if eq:
                    global_args[name.strip()] = default.strip()
            continue
        if instr == "FROM":
            seen_from = True
            tokens = rest.split()
            if not tokens:
                continue
            image = _substitute_args(tokens[0], global_args)
            upper = [t.upper() for t in tokens]
            if "AS" in upper:
                aliases.add(tokens[upper.index("AS") + 1])
            if image != "scratch" and image not in aliases:
                if image not in info.from_images:
                    info.from_images.append(image)
            continue
        if instr == "RUN":
            for line in rest.splitlines():
                candidate = line.strip()
                if _INSTALL_RE.search(candidate):
                    info.install_lines.append(candidate)
            continue
        if instr == "EXPOSE":
            for token in rest.split():
                port = _substitute_args(token, global_args).split("/")[0]
                if port.isdigit():
                    info.exposed_ports.append(int(port))
            continue
        if instr == "ENV":
            for token in shlex.split(rest):
                name, eq, value = token.partition("=")
                if eq:
                    info.env[name] = value
            continue
    return info


def install_verdict(info: DockerfileInfo) -> tuple[bool | None, str | None]:
    """(frozen, evidence) from the Dockerfile's own install lines.

    Frozen when every install line carries a frozen marker
    (``--frozen-lockfile`` / ``--frozen`` / ``--immutable`` /
    ``--require-hashes``) or is ``npm ci``. Any unfrozen install
    line makes the verdict False, with that verbatim line as the
    evidence. No install lines: (None, None) — nothing to judge.
    """
    if not info.install_lines:
        return None, None
    for line in info.install_lines:
        if any(marker in line for marker in _FROZEN_MARKERS):
            continue
        if re.search(r"\bnpm\s+ci\b", line):
            continue
        return False, line
    return True, info.install_lines[0]


@dataclass
class ComposeInfo:
    """The spike's view of a compose file: service names in file
    order, plus the first (and, for the spike, only usable)
    service's build/port/env facts when there is exactly one."""

    services: list[str] = field(default_factory=list)
    build_context: str | None = None
    build_dockerfile: str | None = None
    image: str | None = None
    port: int | None = None
    env_port: int | None = None


def parse_compose(text: str) -> ComposeInfo:
    """Parse a compose file with the YAML loader. Only facts the
    spike acts on are extracted; anything unparsable yields an
    empty service list (the caller types it)."""
    info = ComposeInfo()
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return info
    if not isinstance(data, dict):
        return info
    services = data.get("services")
    if not isinstance(services, dict):
        return info
    info.services = list(services.keys())
    if len(info.services) != 1:
        return info
    service = services[info.services[0]]
    if not isinstance(service, dict):
        return info
    info.image = service.get("image") if isinstance(service.get("image"), str) else None
    build = service.get("build")
    if isinstance(build, str):
        info.build_context = build
    elif isinstance(build, dict):
        if isinstance(build.get("context"), str):
            info.build_context = build["context"]
        if isinstance(build.get("dockerfile"), str):
            info.build_dockerfile = build["dockerfile"]
    ports = service.get("ports")
    if isinstance(ports, list):
        for entry in ports:
            port = _compose_entry_port(entry)
            if port is not None:
                info.port = port
                break
    env = service.get("environment")
    env_map: dict[str, str] = {}
    if isinstance(env, dict):
        env_map = {str(k): str(v) for k, v in env.items()}
    elif isinstance(env, list):
        for item in env:
            name, eq, value = str(item).partition("=")
            if eq:
                env_map[name] = value
    declared = env_map.get("PORT", "")
    if declared.isdigit():
        info.env_port = int(declared)
    return info


def _compose_entry_port(entry: Any) -> int | None:
    """The container-side port of one compose ``ports`` entry:
    the right-hand side of ``host:container``, or the number
    itself for a bare container port. Long-syntax dicts carry it
    as ``target``."""
    if isinstance(entry, int):
        return entry
    if isinstance(entry, dict):
        target = entry.get("target")
        return int(target) if isinstance(target, int) else None
    if isinstance(entry, str):
        tail = entry.split(":")[-1].split("/")[0]
        return int(tail) if tail.isdigit() else None
    return None


def determine_port(
    dockerfile_info: DockerfileInfo | None,
    image_exposed: list[int],
    compose: ComposeInfo | None,
) -> tuple[int | None, str | None]:
    """The app's port and its source, in the spike's fixed order:
    EXPOSE (Dockerfile text, then the built image's config), a
    compose port mapping, then a numeric PORT the files themselves
    declare (``$PORT``). Never guessed: no source, (None, None).
    """
    if dockerfile_info is not None and dockerfile_info.exposed_ports:
        return dockerfile_info.exposed_ports[0], "EXPOSE"
    if image_exposed:
        return image_exposed[0], "EXPOSE"
    if compose is not None and compose.port is not None:
        return compose.port, "compose"
    env_port: str | None = None
    if dockerfile_info is not None:
        env_port = dockerfile_info.env.get("PORT")
    if (env_port is None or not env_port.isdigit()) and compose is not None:
        if compose.env_port is not None:
            return compose.env_port, "$PORT"
    if env_port is not None and env_port.isdigit():
        return int(env_port), "$PORT"
    return None, None


def find_container_file(snapshot: Path) -> tuple[Path | None, Path | None]:
    """(dockerfile, compose) at the snapshot root. An exact-name
    ``Dockerfile`` takes precedence over any compose file."""
    dockerfile = snapshot / "Dockerfile"
    if dockerfile.is_file():
        return dockerfile, None
    candidates = sorted(snapshot.glob("compose*.yml")) + sorted(
        snapshot.glob("docker-compose*.yml")
    )
    for candidate in candidates:
        if candidate.is_file():
            return None, candidate
    return None, None


# ---------------------------------------------------------------------------
# Podman: command construction (pure) and execution (logged, verbatim)
# ---------------------------------------------------------------------------


def build_user_args_for(userns_remap: bool) -> list[str]:
    """The build's userns/cap args. A Dockerfile's RUN steps are
    untrusted code: with remapping available they execute as
    container root mapped away from host root (the build-form
    uid/gid maps). Either way every capability is dropped and
    only the minimal BUILD_CAPS set is added back -- the build
    alone gets that set; the run's args (user_args_for) add
    nothing back. Without remapping there is nothing to map --
    a run only reaches a build at all behind the dev fallback
    flag -- and the drop-all-plus-BUILD_CAPS posture still
    applies."""
    caps = ["--cap-drop=ALL", "--cap-add=" + ",".join(BUILD_CAPS)]
    if userns_remap:
        return ["--userns-uid-map", "0:1:65536",
                "--userns-gid-map", "0:1:65536", *caps]
    return caps


def build_command(
    base: list[str],
    tag: str,
    dockerfile: Path,
    context: Path,
    build_user_args: list[str],
) -> list[str]:
    """The build invocation. The context is the snapshot dir and
    nothing else is mounted or passed: no -v, no --secret, no
    --build-arg. The build runs on the explicit rootless build
    network (BUILD_NETWORK_MODE: slirp4netns with host loopback
    blocked) -- never on podman's default and never on the host
    network: RUN steps are untrusted code and must not land in
    the host namespace or on an unnamed default."""
    return [
        *base, "build", *build_user_args, "--network", BUILD_NETWORK_MODE,
        "-t", tag, "-f", str(dockerfile), str(context),
    ]


def run_env(port_source: str | None, port: int | None) -> dict[str, str]:
    """The ONLY env the spike ever passes to the app container:
    PORT, and only when the port source is $PORT itself. The
    image's own ENV (set by its Dockerfile) always applies and is
    never overridden here."""
    if port_source == "$PORT" and port is not None:
        return {"PORT": str(port)}
    return {}


def run_command(
    base: list[str],
    name: str,
    image: str,
    user_args: list[str],
    sock_dir: Path,
    env: dict[str, str],
    wrapped_command: str,
) -> list[str]:
    """The run invocation: --net=none (the socket bridge is the
    only way in), the sandbox's userns/cap args, the socket dir as
    the sole mount, and `sh -c` wrapping the image's own command
    with the bridge forwarder started beside it."""
    args = [*base, "run", "-d", "--name", name, "--net=none"]
    args += user_args
    args += ["-v", f"{sock_dir}:/pd-sock"]
    for key, value in env.items():
        args += ["-e", f"{key}={value}"]
    args += ["--entrypoint", "sh", image, "-c", wrapped_command]
    return args


def user_args_for(
    caps_userns_remap: bool, allow_no_userns_fallback: bool = False
) -> list[str]:
    """Mirror Sandbox._user_args, including its fail-closed rule.

    Userns remapping when the host can. When it cannot, the
    default is REFUSAL (UsernsUnavailableError): no build and no
    run happen, exactly as the sandbox refuses. Only the explicit
    dev flag (``--allow-no-userns-fallback``) permits the
    ``--user 65534`` (nobody) fallback, and the caller records
    that fallback on every row (``userns_fallback: true``)."""
    if caps_userns_remap:
        return ["--uidmap", "0:1:65536", "--cap-drop=ALL",
                "--security-opt=no-new-privileges"]
    if not allow_no_userns_fallback:
        raise UsernsUnavailableError(
            "userns remapping unavailable on this host; the spike "
            "refuses to build or run without it (explicit dev "
            "opt-out: --allow-no-userns-fallback)"
        )
    return ["--user", "65534", "--cap-drop=ALL",
            "--security-opt=no-new-privileges"]


@dataclass
class ProcResult:
    returncode: int
    stdout: str
    stderr: str


class Podman:
    """Every podman invocation the spike makes, logged verbatim
    (command line first, output after) into the side's log list."""

    def __init__(self, logs: list[str], base: list[str] | None = None) -> None:
        self.logs = logs
        self.base = list(base) if base is not None else _podman_base()

    def run(self, args: list[str], timeout: int) -> ProcResult:
        self.logs.append("$ " + " ".join(shlex.quote(a) for a in args))
        try:
            proc = subprocess.run(
                args, capture_output=True, text=True, timeout=timeout, check=False,
            )
        except subprocess.TimeoutExpired:
            self.logs.append(f"podman: timed out after {timeout}s")
            return ProcResult(124, "", f"timed out after {timeout}s")
        except OSError as exc:
            self.logs.append(f"podman: could not execute: {exc}")
            return ProcResult(127, "", str(exc))
        out = proc.stdout.strip()
        err = proc.stderr.strip()
        if out:
            self.logs.append(out)
        if err:
            self.logs.append(err)
        return ProcResult(proc.returncode, proc.stdout, proc.stderr)

    def build(
        self,
        tag: str,
        dockerfile: Path,
        context: Path,
        build_user_args: list[str],
    ) -> ProcResult:
        return self.run(
            build_command(self.base, tag, dockerfile, context, build_user_args),
            BUILD_TIMEOUT_S,
        )

    def image_digests(self, ref: str) -> list[str]:
        """The resolved RepoDigests of an image ref the build
        pulled; empty when the ref is unknown locally."""
        proc = self.run([*self.base, "image", "inspect", ref], 120)
        if proc.returncode != 0:
            return []
        try:
            data = json.loads(proc.stdout)
            digests = data[0].get("RepoDigests") or []
        except (ValueError, IndexError, AttributeError):
            return []
        return [str(d) for d in digests]

    def inspect_image(self, ref: str) -> dict[str, Any]:
        proc = self.run([*self.base, "image", "inspect", ref], 120)
        if proc.returncode != 0:
            return {}
        try:
            data = json.loads(proc.stdout)
            return dict(data[0]) if data else {}
        except (ValueError, IndexError, TypeError):
            return {}

    def runtime_available(self, image: str, binary: str) -> bool:
        proc = self.run(
            [*self.base, "run", "--rm", "--net=none",
             "--entrypoint", binary, image, "--version"],
            120,
        )
        return proc.returncode == 0

    def is_running(self, name: str) -> bool:
        proc = self.run([*self.base, "inspect", name], 60)
        if proc.returncode != 0:
            return False
        try:
            data = json.loads(proc.stdout)
            return bool(data[0]["State"]["Running"])
        except (ValueError, IndexError, KeyError, TypeError):
            return False

    def container_logs(self, name: str, tail: int = 100) -> str:
        proc = self.run([*self.base, "logs", "--tail", str(tail), name], 60)
        return (proc.stdout + proc.stderr).strip()

    def remove(self, name: str) -> None:
        self.run([*self.base, "rm", "-f", name], 60)


def dump_spike_logs(name: str, logs: list[str]) -> Path:
    """Write a side's logs to ``spike-logs/<name>.log`` (the
    native harness's dump_logs shape, under the spike's own dir).
    """
    out = Path("spike-logs") / f"{name}.log"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(str(entry) for entry in logs), encoding="utf-8")
    return out


def materialize_snapshot(clone: Path, sha: str, dest: Path) -> None:
    """Extract the registered commit's tree into ``dest`` via
    ``git archive``. The tarfile ``data`` filter refuses absolute
    paths, ``..`` escapes and links pointing outside the tree;
    such a tree fails the side with the filter's own error."""
    proc = subprocess.run(
        ["git", "-C", str(clone), "archive", sha],
        capture_output=True, timeout=900, check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"git archive {sha} failed: {proc.stderr.decode(errors='replace').strip()}"
        )
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(proc.stdout)) as archive:
        archive.extractall(dest, filter="data")


def _last_line(text: str) -> str | None:
    for line in reversed(text.splitlines()):
        if line.strip():
            return line.strip()
    return None


# ---------------------------------------------------------------------------
# The readiness probe over the socket bridge
# ---------------------------------------------------------------------------

Prober = Callable[
    [Path, str, float, Callable[[], bool], list[str]], tuple[int | None, bool]
]


def _wait_for_socket(sock_path: Path, timeout: float, logs: list[str]) -> str | None:
    """Mirror of the sandbox's bridge-socket wait: None once the
    socket accepts a connection, else the last error."""
    last_err = "socket file never appeared"
    deadline = time.time() + timeout
    while time.time() < deadline:
        if sock_path.exists():
            try:
                probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                probe.settimeout(2)
                try:
                    probe.connect(str(sock_path))
                    probe.close()
                    return None
                except OSError as exc:
                    last_err = f"socket present but connect failed: {exc}"
                finally:
                    probe.close()
            except OSError as exc:
                last_err = str(exc)
        time.sleep(0.5)
    logs.append(f"bridge socket never became ready within {timeout}s: {last_err}")
    return last_err


def probe_http(
    sock_path: Path,
    path: str,
    timeout_s: float,
    is_running: Callable[[], bool],
    logs: list[str],
) -> tuple[int | None, bool]:
    """Poll ``path`` over the bridge until the app answers HTTP
    (any status — answering is the readiness fact), the container
    stops, or the deadline passes. Returns (status, still
    running)."""
    proxy = _TcpToUnixProxy(sock_path)
    proxy.start()
    logs.append(f"spike probe: executor proxy at 127.0.0.1:{proxy.port}")
    deadline = time.time() + timeout_s
    status: int | None = None
    try:
        while time.time() < deadline:
            try:
                request = urllib.request.Request(
                    f"http://127.0.0.1:{proxy.port}{path}", method="GET"
                )
                with urllib.request.urlopen(request, timeout=2) as response:
                    status = int(response.status)
                    break
            except urllib.error.HTTPError as exc:
                status = int(exc.code)
                break
            except (urllib.error.URLError, OSError):
                if not is_running():
                    return None, False
                time.sleep(1.0)
    finally:
        proxy.stop()
    return status, True


# ---------------------------------------------------------------------------
# Per-side spike flow
# ---------------------------------------------------------------------------


def _row(
    unit: dict[str, Any],
    side_spec: dict[str, Any],
    attempt: int,
    status: str,
    typed_reason: str | None,
    from_digests: list[str] | None = None,
    install_frozen: bool | None = None,
    install_evidence: str | None = None,
    port_source: str | None = None,
    port: int | None = None,
    userns_fallback: bool = False,
) -> dict[str, Any]:
    return {
        "kind": "container-spike",
        "repo": unit["repo"],
        "unit": unit["unit_id"],
        "unit_id": unit["unit_id"],
        "role": side_spec["role"],
        "registered_sha": side_spec["sha"],
        "status": status,
        "typed_reason": typed_reason,
        "from_digests": from_digests or [],
        "install_frozen": install_frozen,
        "install_evidence": install_evidence,
        "port_source": port_source,
        "port": port,
        "network_mode": "none+socket",
        "build_network": BUILD_NETWORK_MODE,
        "build_caps": list(BUILD_CAPS),
        "userns_fallback": userns_fallback,
        "attempt": attempt,
    }


def spike_side_from_snapshot(
    podman: Podman,
    snapshot: Path,
    unit: dict[str, Any],
    side_spec: dict[str, Any],
    readiness_path: str,
    attempt: int,
    user_args: list[str],
    logs: list[str],
    prober: Prober = probe_http,
    probe_timeout_s: float = PROBE_TIMEOUT_S,
    socket_wait: Callable[[Path, float, list[str]], str | None] = _wait_for_socket,
    *,
    build_user_args: list[str],
    userns_fallback: bool = False,
) -> dict[str, Any]:
    """Spike one side whose snapshot tree is already on disk.
    Never raises for a build/start outcome; outcomes are typed
    into the row."""

    def row(status: str, typed_reason: str | None, **kwargs: Any) -> dict[str, Any]:
        return _row(unit, side_spec, attempt, status, typed_reason,
                    userns_fallback=userns_fallback, **kwargs)
    dockerfile_path, compose_path = find_container_file(snapshot)
    compose: ComposeInfo | None = None
    if dockerfile_path is None and compose_path is not None:
        compose = parse_compose(compose_path.read_text(encoding="utf-8"))
        logs.append(
            f"compose file: {compose_path.name}; services: {compose.services}"
        )
        if len(compose.services) > 1:
            return row("BUILD_FAILED",
                        "multi-service not in spike")
        if len(compose.services) == 0:
            return row("BUILD_FAILED",
                        "compose file declares no services")
        if compose.build_context is None:
            return row("BUILD_FAILED",
                        "compose service declares no build section; "
                        "image-only services are out of spike scope")
        context = (snapshot / compose.build_context).resolve()
        if not str(context).startswith(str(snapshot.resolve())):
            return row("BUILD_FAILED",
                        "compose build context escapes the snapshot")
        dockerfile_path = context / (compose.build_dockerfile or "Dockerfile")
        if not dockerfile_path.is_file():
            return row("BUILD_FAILED",
                        "compose build dockerfile not found in snapshot")
        build_context = context
    elif dockerfile_path is not None:
        build_context = snapshot
        logs.append(f"dockerfile: {dockerfile_path.name}")
    else:
        return row("BUILD_FAILED",
                    "no root Dockerfile or compose file in snapshot")

    info = parse_dockerfile(dockerfile_path.read_text(encoding="utf-8"))
    frozen, evidence = install_verdict(info)
    logs.append(f"FROM images: {info.from_images}")
    logs.append(f"install lines: {info.install_lines}")
    logs.append(f"install frozen: {frozen}; evidence: {evidence}")

    slug = f"{unit['unit_id']}-{side_spec['role']}-a{attempt}".lower()
    slug = re.sub(r"[^a-z0-9_.-]", "-", slug)
    tag = f"pd-spike-{slug}"
    built = podman.build(tag, dockerfile_path, build_context, build_user_args)
    digests: list[str] = []
    for image in info.from_images:
        for digest in podman.image_digests(image):
            if digest not in digests:
                digests.append(digest)
    logs.append(f"FROM digests: {digests}")
    if built.returncode != 0:
        tail = _last_line(built.stderr) or _last_line(built.stdout)
        typed = "container build failed" + (f": {tail[:300]}" if tail else "")
        return row("BUILD_FAILED", typed,
                    from_digests=digests, install_frozen=frozen,
                    install_evidence=evidence)

    image_info = podman.inspect_image(tag)
    config = image_info.get("Config") or {}
    exposed = []
    for key in (config.get("ExposedPorts") or {}):
        number = str(key).split("/")[0]
        if number.isdigit():
            exposed.append(int(number))
    port, port_source = determine_port(info, exposed, compose)
    logs.append(f"port: {port} (source: {port_source})")
    if port is None:
        return row("BUILD_FAILED",
                    "cannot determine container port: no EXPOSE in "
                    "Dockerfile or image config, no compose mapping, "
                    "no declared PORT",
                    from_digests=digests, install_frozen=frozen,
                    install_evidence=evidence)

    app_argv = list(config.get("Entrypoint") or []) + list(config.get("Cmd") or [])
    if not app_argv:
        return row("START_FAILED",
                    "image declares no ENTRYPOINT or CMD",
                    from_digests=digests, install_frozen=frozen,
                    install_evidence=evidence, port_source=port_source, port=port)

    if podman.runtime_available(tag, "python3"):
        bridge_file, bridge_cmd = ".pd-bridge.py", "python3"
        bridge_text = _BRIDGE_SCRIPT
    elif podman.runtime_available(tag, "node"):
        bridge_file, bridge_cmd = ".pd-bridge.cjs", "node"
        bridge_text = _NODE_BRIDGE_SCRIPT
    else:
        return row("START_FAILED",
                    "no bridge runtime (python3 or node) in image",
                    from_digests=digests, install_frozen=frozen,
                    install_evidence=evidence, port_source=port_source, port=port)

    name = f"pd-spike-{slug}"
    sock_dir = Path(tempfile.mkdtemp(prefix="pd-spike-", dir="/tmp"))
    sock_path = sock_dir / "app.sock"
    try:
        (sock_dir / bridge_file).write_text(bridge_text, encoding="utf-8")
        wrapped = (
            f"({bridge_cmd} /pd-sock/{bridge_file} /pd-sock/app.sock {port} &); "
            f"exec {shlex.join(app_argv)}"
        )
        env = run_env(port_source, port)
        started = podman.run(
            run_command(podman.base, name, tag, user_args, sock_dir, env, wrapped), 60
        )
        if started.returncode != 0:
            tail = _last_line(started.stderr) or _last_line(started.stdout)
            typed = "container start failed" + (f": {tail[:300]}" if tail else "")
            return row("START_FAILED", typed,
                        from_digests=digests, install_frozen=frozen,
                        install_evidence=evidence, port_source=port_source, port=port)
        sock_err = socket_wait(sock_path, SOCKET_TIMEOUT_S, logs)
        if sock_err is not None:
            logs.append("container log tail:\n" + podman.container_logs(name))
            return row("START_FAILED",
                        f"bridge socket never became ready: {sock_err}",
                        from_digests=digests, install_frozen=frozen,
                        install_evidence=evidence, port_source=port_source, port=port)
        logs.append(f"bridge socket ready: {sock_path}")
        status, running = prober(
            sock_path, readiness_path, probe_timeout_s,
            lambda: podman.is_running(name), logs,
        )
        if status is not None:
            logs.append(f"readiness: HTTP {status} at {readiness_path}")
            return row("READY", None,
                        from_digests=digests, install_frozen=frozen,
                        install_evidence=evidence, port_source=port_source, port=port)
        if not running:
            tail = _last_line(podman.container_logs(name))
            typed = "container exited before readiness" + (
                f": {tail[:300]}" if tail else ""
            )
            return row("START_FAILED", typed,
                        from_digests=digests, install_frozen=frozen,
                        install_evidence=evidence, port_source=port_source, port=port)
        logs.append("container log tail:\n" + podman.container_logs(name))
        return row("START_FAILED", "readiness timeout",
                    from_digests=digests, install_frozen=frozen,
                    install_evidence=evidence, port_source=port_source, port=port)
    finally:
        podman.remove(name)
        shutil.rmtree(sock_dir, ignore_errors=True)


def spike_side(
    podman: Podman,
    clone: Path,
    unit: dict[str, Any],
    side_spec: dict[str, Any],
    readiness_path: str,
    attempt: int,
    workdir: Path,
    user_args: list[str],
    build_user_args: list[str],
    userns_fallback: bool = False,
) -> dict[str, Any]:
    """Fetch + materialize one side's snapshot, then spike it.
    Exactly one row per side; harness-level surprises are typed
    in the native harness's ``harness error:`` style."""
    logs: list[str] = [f"podman base: {' '.join(podman.base)}"]
    tag = _TAG_BY_ROLE[str(side_spec["role"])]
    try:
        fetch_sha(clone, str(side_spec["sha"]))
        snapshot = (
            workdir / "units" / str(unit["unit_id"]) / f"a{attempt}"
            / f"snapshot-{tag}"
        )
        materialize_snapshot(clone, str(side_spec["sha"]), snapshot)
        side_podman = Podman(logs, podman.base)
        row = spike_side_from_snapshot(
            side_podman, snapshot, unit, side_spec, readiness_path,
            attempt, user_args, logs, build_user_args=build_user_args,
            userns_fallback=userns_fallback,
        )
    except Exception as exc:  # one side must not kill the others
        row = _row(unit, side_spec, attempt, "BUILD_FAILED",
                   f"harness error: {exc}", userns_fallback=userns_fallback)
        logs.append(f"spike raised: {exc}")
    dump_spike_logs(f"spike-{unit['unit_id']}-{tag}-a{attempt}", logs)
    return row


def spike_repo(
    manifest: dict[str, Any],
    repo: str,
    workdir: Path,
    attempt: int,
    only: list[tuple[str, str]] | None = None,
    allow_no_userns_fallback: bool = False,
) -> dict[str, Any]:
    """Spike every registered side of ``repo`` (or an ``only``
    subset of (unit_id, role) pairs, for reruns)."""
    units = units_for_repo(manifest, repo)
    if not units:
        raise ValueError(f"no units for repo {repo!r} in the manifest")
    workdir = workdir.resolve()
    caps = check_sandbox_capabilities()
    run_log: list[str] = [
        f"podman: {caps.podman} ({caps.podman_version}); "
        f"userns_remap: {caps.userns_remap}; reason: {caps.reason}",
        f"podman base: {' '.join(_podman_base())}",
    ]
    if not caps.podman:
        raise RuntimeError(f"podman not available: {caps.reason}")
    # Fail closed without userns remapping: user_args_for raises
    # UsernsUnavailableError, which propagates — no build and no
    # run happen, and main reports the refusal (exit 2).
    args_for_user = user_args_for(caps.userns_remap, allow_no_userns_fallback)
    build_args = build_user_args_for(caps.userns_remap)
    userns_fallback = not caps.userns_remap
    run_log.append(f"run user args: {args_for_user}")
    run_log.append(f"build user args: {build_args}")
    run_log.append(f"build network: {BUILD_NETWORK_MODE}")
    run_log.append(f"build caps: {','.join(BUILD_CAPS)}")
    if userns_fallback:
        run_log.append(
            "WARNING: userns remapping unavailable; the explicit dev "
            "flag --allow-no-userns-fallback allows the --user 65534 "
            "(nobody) fallback; every row records userns_fallback: true"
        )
    clone = ensure_clone(workdir, repo)
    readiness_path = load_contract(contract_path(repo)).readiness
    podman = Podman(run_log)
    rows: list[dict[str, Any]] = []
    for unit in units:
        for side_spec in unit["sides"]:
            if only is not None and (
                unit["unit_id"], side_spec["role"]
            ) not in only:
                continue
            row = spike_side(
                podman, clone, unit, side_spec, readiness_path,
                attempt, workdir, args_for_user, build_args,
                userns_fallback=userns_fallback,
            )
            rows.append(row)
            print(json.dumps(row), flush=True)
    dump_spike_logs(f"spike-{repo.replace('/', '-')}-run-a{attempt}", run_log)
    return {
        "kind": "container-spike",
        "repo": repo,
        "attempt": attempt,
        "network_mode": "none+socket",
        "rows": rows,
    }


def _rerun_targets(prior_path: Path) -> list[tuple[str, str]]:
    data = json.loads(prior_path.read_text(encoding="utf-8"))
    prior_rows = data["rows"] if isinstance(data, dict) else data
    return [
        (str(r["unit"]), str(r["role"]))
        for r in prior_rows
        if r["status"] != "READY"
    ]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", required=True, help="owner/name from the manifest")
    ap.add_argument("--attempt", type=int, default=1)
    ap.add_argument("--out", type=Path, required=True, help="rows JSON output path")
    ap.add_argument("--workdir", type=Path, default=Path("spike-work"))
    ap.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    ap.add_argument(
        "--rerun-non-ready", type=Path, default=None,
        help="prior rows JSON; re-spike only its non-READY sides",
    )
    ap.add_argument(
        "--allow-no-userns-fallback", action="store_true",
        help="DEV OVERRIDE: when the host cannot do userns "
        "remapping, proceed as --user 65534 (nobody) instead of "
        "refusing; every row then records userns_fallback: true. "
        "Never for measured runs.",
    )
    args = ap.parse_args(argv)
    manifest = load_manifest(args.manifest)
    only = (
        _rerun_targets(args.rerun_non_ready)
        if args.rerun_non_ready is not None
        else None
    )
    try:
        result = spike_repo(
            manifest, args.repo, args.workdir, args.attempt, only=only,
            allow_no_userns_fallback=args.allow_no_userns_fallback,
        )
    except (RuntimeError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"container spike failed: {exc}", file=sys.stderr)
        return 2
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
