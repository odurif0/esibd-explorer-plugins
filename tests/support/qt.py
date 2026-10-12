"""Run a real-Qt probe script in a fresh interpreter (offscreen, Explorer host Python)."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest

import explorer_host

SKIP = 77  # A probe exits 77 when real PyQt6 or the Explorer sources are unavailable.


def fatal_stack(stderr: str) -> str:
    """The faulthandler stacks of a crash, which its trailing list of extension modules pushes out of the tail."""
    start = stderr.find("Fatal Python error")
    if start < 0:
        return ""
    end = stderr.find("\nExtension modules:", start)
    return "\n--- faulthandler ---\n" + stderr[start:end if end >= 0 else None][:20000]


def dispose_windows(app) -> None:
    """Delete the probe's parentless windows before interpreter teardown, without running the event loop.

    Child popups go with their parents: a toolbar keeps a raw pointer to its extension menu, a
    top-level popup, and deleting that menu on its own made a later action removal crash
    (SIGSEGV in QToolBarLayout::takeAt, 2026-10-12). Only deferred deletions are delivered:
    the plugins were never closed, and their timers would fire on the deleted widgets.
    """
    from PyQt6.QtCore import QCoreApplication, QEvent
    for widget in app.topLevelWidgets():
        if widget.parentWidget() is None:
            widget.hide()
            widget.deleteLater()
    for _ in range(2):  # Destructors may post further deletions.
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def run_probe(script, *args, timeout: float, scale: str | None = None, env: dict | None = None,
              allow_traceback: bool = False) -> subprocess.CompletedProcess:
    """Run ``script`` (usually the calling test file) and fail with its output unless it exits 0."""
    environment = {**os.environ, "QT_QPA_PLATFORM": "offscreen", "PYTHONFAULTHANDLER": "1", **(env or {})}
    if scale is not None:
        environment["QT_SCALE_FACTOR"] = scale
    result = subprocess.run([explorer_host.qt_python(), str(Path(script).resolve()), *map(str, args)], env=environment,
                            capture_output=True, text=True, timeout=timeout)
    if result.returncode == SKIP:
        pytest.skip((result.stdout + result.stderr).strip() or "Real Qt/Explorer unavailable")
    assert result.returncode == 0, result.stdout[-6000:] + result.stderr[-6000:] + fatal_stack(result.stderr)
    if not allow_traceback:
        assert "Traceback" not in result.stderr, result.stderr[-6000:]
    return result
