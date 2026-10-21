"""Host access to paths inside a side's checkout or data directory (issue-register R4).

In the sandbox, a side's checkout and ``/pd-home`` belong to the userns-mapped
runtime uid after provisioning (``podman unshare chown``). The host user then
cannot always create, replace, remove, chmod or read entries there: round 2
crashed with ``PermissionError ... db.sqlite3.pd-restore``.

Every function here tries the plain host operation first and, on
``PermissionError``, retries the same operation inside the rootless user
namespace (``podman unshare <cmd>``), where the caller holds root over the
mapped uids. The command is an argv list (no shell). A second failure raises
:class:`ContainerPathError`, which names the operation and the path and never
carries file contents.

Route every host access to such a path through this module.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

_UNSHARE_TIMEOUT_SECONDS = 600


class ContainerPathError(OSError):
    """A container-owned path could not be accessed, even through ``podman unshare``."""


# Seams (tests replace these; production uses the real operations).
_copyfile = shutil.copyfile
_replace = os.replace
_rmtree = shutil.rmtree
_chmod = os.chmod


def _unlink(path: Path) -> None:
    path.unlink(missing_ok=True)


def _read(path: Path) -> bytes:
    return path.read_bytes()


def _run(argv: list[str]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        argv, capture_output=True, timeout=_UNSHARE_TIMEOUT_SECONDS, check=False, shell=False
    )


def unshare_argv(*inner: str) -> list[str]:
    """The full argv for one command run inside the user namespace."""
    from proofdeploy.sandbox import PODMAN

    return [*PODMAN, "unshare", *inner]


def _unshare(op: str, path: Path, *inner: str) -> bytes:
    """Run ``podman unshare <inner>``; raise ContainerPathError (path only) on any failure."""
    try:
        proc = _run(unshare_argv(*inner))
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ContainerPathError(
            f"{op} {path}: podman unshare did not run ({type(exc).__name__})"
        ) from None
    if proc.returncode != 0:
        raise ContainerPathError(f"{op} {path}: podman unshare exited {proc.returncode}")
    return proc.stdout or b""


def copy_into(src: Path, dest: Path) -> None:
    """Copy the file ``src`` to ``dest`` (a path inside a container-owned directory)."""
    try:
        _copyfile(src, dest)
    except PermissionError:
        _unshare("copy_into", dest, "cp", "--", str(src), str(dest))


def replace(src: Path, dest: Path) -> None:
    """Atomically move ``src`` over ``dest`` (both inside the same directory tree)."""
    try:
        _replace(src, dest)
    except PermissionError:
        _unshare("replace", dest, "mv", "-f", "--", str(src), str(dest))


def remove_tree(path: Path) -> None:
    """Remove a file or a directory tree; a missing path is not an error."""
    try:
        if path.is_dir() and not path.is_symlink():
            _rmtree(path)
        else:
            _unlink(path)
    except FileNotFoundError:
        return
    except PermissionError:
        _unshare("remove_tree", path, "rm", "-rf", "--", str(path))


def chmod(path: Path, mode: int) -> None:
    """Set the permission bits of ``path``."""
    try:
        _chmod(path, mode)
    except PermissionError:
        _unshare("chmod", path, "chmod", format(mode & 0o7777, "o"), "--", str(path))


def read_bytes(path: Path) -> bytes:
    """Read a whole file."""
    try:
        return _read(path)
    except PermissionError:
        return _unshare("read_bytes", path, "cat", "--", str(path))


__all__ = [
    "ContainerPathError",
    "chmod",
    "copy_into",
    "read_bytes",
    "remove_tree",
    "replace",
    "unshare_argv",
]
