"""Run a real-Qt probe script in a fresh interpreter (offscreen, Explorer host Python)."""
from __future__ import annotations

import os
from pathlib import Path
import re
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


SECOND_RUN = []  # (probe, why its first run failed) of the probes that passed on their second run.
_FAILED = re.compile(r"^\w+(?:Error|Exception)\b.*$", re.MULTILINE)
_FRAME = re.compile(r'^  File "(.+?)", line (\d+)', re.MULTILINE)


def _launch(script, args, environment, timeout):
    """The finished probe, or the timeout error of one that had to be killed."""
    try:
        return subprocess.run([explorer_host.qt_python(), str(Path(script).resolve()), *map(str, args)],
                              env=environment, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as late:
        return late


def _failure(result) -> str:
    """What failed, when the probe may run a second time; '' when it passed, was skipped or crashed."""
    if isinstance(result, subprocess.TimeoutExpired):
        return f"no end after {result.timeout:g} s"
    if result.returncode != 1 or "Fatal Python error" in result.stderr:
        return ""  # 1 is an assertion or an exception; a signal or any other code is not run again.
    found = _FAILED.search(result.stderr)
    if not found:
        return "exit 1"
    frames = _FRAME.findall(result.stderr[:found.start()])
    where = f"{Path(frames[-1][0]).name}:{frames[-1][1]}: " if frames else ""
    return where + found.group(0)[:160]


def run_probe(script, *args, timeout: float, scale: str | None = None, env: dict | None = None,
              allow_traceback: bool = False) -> subprocess.CompletedProcess:
    """Run ``script`` (usually the calling test file) and fail with its output unless it exits 0.

    The probes run in real time, and a busy machine makes them late: on 2026-11-13, at a load of 18
    on 12 cores, 23 of 414 failed, on the published code as well, and passed when run again. A probe
    that fails an assertion or does not end is therefore run a second time, in the same state: every
    directory it was given empty is emptied again (the first run's files are kept beside it). A crash
    is never run again. The session ends with the list of the probes that needed their second run;
    ``ESIBD_PROBE_SECOND_RUN=0`` turns the second run off. ``timeout`` bounds a hang, with a floor
    of 60 s: it is not a speed requirement.
    """
    environment = {**os.environ, "QT_QPA_PLATFORM": "offscreen", "PYTHONFAULTHANDLER": "1", **(env or {})}
    if scale is not None:
        environment["QT_SCALE_FACTOR"] = scale
    timeout = max(timeout, 60)
    empty = [Path(arg) for arg in args if isinstance(arg, os.PathLike) and Path(arg).is_dir()
             and not any(Path(arg).iterdir())]
    result = _launch(script, args, environment, timeout)
    first = _failure(result) if os.environ.get("ESIBD_PROBE_SECOND_RUN") != "0" else ""
    if first:
        for directory in empty:
            directory.rename(directory.with_name(directory.name + "-first-run"))
            directory.mkdir()
        result = _launch(script, args, environment, timeout)
    if isinstance(result, subprocess.TimeoutExpired):
        raise result
    if result.returncode == SKIP:
        pytest.skip((result.stdout + result.stderr).strip() or "Real Qt/Explorer unavailable")
    assert result.returncode == 0, result.stdout[-6000:] + result.stderr[-6000:] + fatal_stack(result.stderr)
    if not allow_traceback:
        assert "Traceback" not in result.stderr, result.stderr[-6000:]
    if first:
        test = os.environ.get("PYTEST_CURRENT_TEST", Path(script).name).split(" ")[0]
        SECOND_RUN.append((test, first))
    return result
