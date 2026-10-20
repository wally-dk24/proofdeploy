"""Runs INSIDE the app interpreter (imported by ``pdcov.pth``).

Starts coverage.py for this process (``coverage.process_startup`` reads
``COVERAGE_PROCESS_START``) and a daemon thread that follows the control file
``context`` next to this file: whenever its content changes, the thread
switches coverage's dynamic context to that content and then writes the same
content to ``context.ack``, so the host knows the switch took effect before it
sends the next request.

This file is copied verbatim into the pdcov tool directory by
``proofdeploy.lean.coverage.install_pdcov``; it must stay stdlib + coverage
only, and it must never raise into the interpreter's startup.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

POLL_SECONDS = 0.02


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _follow(cov: object, control: Path, ack: Path) -> None:
    last: str | None = None
    while True:
        content = _read(control)
        if content is not None and content != last:
            try:
                cov.switch_context(content)  # type: ignore[attr-defined]
                _write_atomic(ack, content)
                last = content
            except Exception:  # never kill the app; the host times out
                pass
        time.sleep(POLL_SECONDS)


def start() -> None:
    if not os.environ.get("COVERAGE_PROCESS_START"):
        return
    try:
        import coverage

        cov = coverage.process_startup()
        if cov is None:
            cov = coverage.Coverage.current()
        if cov is None:
            return
        here = Path(__file__).resolve().parent
        thread = threading.Thread(
            target=_follow,
            args=(cov, here / "context", here / "context.ack"),
            name="pdcov-context",
            daemon=True,
        )
        thread.start()
    except Exception:  # startup hook must not break the interpreter
        return


start()
