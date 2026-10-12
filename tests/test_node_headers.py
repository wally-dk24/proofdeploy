"""Addendum 37, proof (a): the pinned Node images ship the exact
runtime's headers in /usr/local/include/node — the reason
npm_config_nodedir=/usr/local makes the compile fallback work
with no download. Checked in the sandbox, against the pinned
ref@digest rows from images.lock.json (Node 24 and Node 26).
"""
import json
import os
from pathlib import Path

import pytest

from proofdeploy.sandbox import _run_rc

pytestmark = pytest.mark.skipif(
    not os.environ.get("PD_REQUIRE_SANDBOX"),
    reason="sandbox headers proof runs in the sandbox-tests CI job",
)

LOCK = (
    Path(__file__).resolve().parent.parent
    / "proofdeploy" / "images.lock.json"
)


def _row(version: str) -> dict:
    lock = json.loads(LOCK.read_text(encoding="utf-8"))
    for row in lock["images"]:
        if row["runtime"] == "node" and row["version"] == version:
            return row
    raise AssertionError(f"node {version} missing from images.lock.json")


@pytest.mark.parametrize(
    "version,major", [("24.21.0", "24"), ("26.11.1", "26")]
)
def test_pinned_node_image_ships_its_headers(version, major):
    row = _row(version)
    ref = f"{row['ref']}@{row['digest']}"
    logs: list[str] = []
    rc = _run_rc(
        [
            "podman", "--storage-driver", "vfs",
            "--cgroup-manager=cgroupfs", "run", "--rm", ref,
            "sh", "-c",
            "ls /usr/local/include/node/node_version.h && "
            "grep -m1 '#define NODE_MAJOR_VERSION' "
            "/usr/local/include/node/node_version.h",
        ],
        300,
        logs,
    )
    joined = "\n".join(logs)
    assert rc == 0, joined
    assert "/usr/local/include/node/node_version.h" in joined
    assert f"#define NODE_MAJOR_VERSION {major}" in joined
