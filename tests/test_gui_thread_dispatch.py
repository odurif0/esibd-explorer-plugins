"""Exercise the real Qt event loop in a clean subprocess (no ESIBD stubs).

Only the GUI boundary functions are extracted from each entrypoint, so these
checks need PyQt6 but neither vendor DLLs nor an installed ESIBD application.
The parent mimics ESIBD's widget-backed settings, not SimpleNamespace fields.
"""

from __future__ import annotations

import ast
import logging
import os
import subprocess
import sys
import threading
import time
import types
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINTS = sorted(ROOT.glob("*/*plugin.py"))


@pytest.mark.parametrize("entrypoint", ENTRYPOINTS, ids=lambda p: p.parent.name)
def test_real_qt_dispatch_and_widget_backed_settings(entrypoint):
    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--probe", str(entrypoint)],
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode == 77:
        pytest.skip("PyQt6 is not installed; real Qt event-loop checks unavailable")
    assert result.returncode == 0, result.stdout + result.stderr


def probe(path):
    try:
        from PyQt6.QtCore import QThread, QTimer
        from PyQt6.QtWidgets import QApplication, QLabel
    except ImportError:
        return 77

    tree = ast.parse(path.read_text())
    names = {
        "_invoke_gui_callback", "_sync_status_to_gui", "_sync_status",
        "_restore_off_ui_state", "_restore_on_ui_state", "_set_on_ui_state",
        "_handle_transport_loss", "_stop_refresh_timer",
    }
    functions = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name in names]
    lock = next(
        n for n in tree.body if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "_GUI_DISPATCH_LOCK" for t in n.targets)
    )
    ns = {"Any": Any, "logging": logging, "RLock": threading.RLock, "__name__": "qt_probe"}
    exec(compile(ast.Module(body=[lock, *functions], type_ignores=[]), str(path), "exec"), ns)
    app = QApplication([])
    invoke = ns["_invoke_gui_callback"]
    calls = []
    errors = []

    def on_gui():
        assert QThread.currentThread() == app.thread(), "widget accessed outside GUI thread"

    def drain_until(predicate):
        deadline = time.monotonic() + 5
        while not predicate() and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.001)
        assert predicate(), "queued GUI callbacks were not delivered"

    def in_worker(fn):
        def run():
            try:
                fn()
            except BaseException as exc:
                errors.append(exc)
        thread = threading.Thread(target=run)
        thread.start()
        thread.join(5)
        assert not thread.is_alive(), "worker hung"
        assert not errors, repr(errors)

    # Simultaneous first callers must share a live dispatcher. Python threads
    # have no Qt event loop: an undecorated callable proxy loses these updates.
    barrier = threading.Barrier(8)
    def submit(index):
        try:
            barrier.wait(5)
            def callback():
                on_gui()
                calls.append(index)
            invoke(callback)
        except BaseException as exc:
            errors.append(exc)
    threads = [threading.Thread(target=submit, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
        assert not thread.is_alive()
    assert not errors, repr(errors)
    assert calls == [], "a callback ran before the GUI processed events"
    drain_until(lambda: len(calls) == 8)
    assert sorted(calls) == list(range(8))

    # Direct calls on the GUI thread still work, without event-loop latency.
    invoke(lambda: calls.append("direct"))
    assert calls[-1] == "direct"

    class Parent:
        def __init__(self):
            object.__setattr__(self, "labels", {})
            object.__setattr__(self, "writes", [])

        def __setattr__(self, name, value):
            # Setting.value in ESIBD calls widget setters synchronously.
            on_gui()
            label = self.labels.setdefault(name, QLabel())
            label.setText(str(value))
            self.writes.append(name)
            object.__setattr__(self, name, value)

    parent = Parent()
    controller = types.SimpleNamespace(
        controllerParent=parent, main_state="ST_ON", hardware_main_state="ST_ON",
        output_state_summary="ON", available_configs=[], available_configs_text="configs",
        loaded_state_text="Manual", loaded_config_text="Config 1", device_enabled_state="ON",
        detected_modules_text="modules", detected_modules="modules", device_state_summary="OK",
        interlock_state_summary="OK", voltage_state_summary="OK", temperature_state_summary="OK",
        interlock_state="OK", heat_status="OK",
    )
    sync = ns.get("_sync_status_to_gui", ns.get("_sync_status"))
    in_worker(lambda: sync(controller))
    assert parent.writes == [], "setting was written before dispatch"
    drain_until(lambda: "main_state" in parent.writes)
    assert parent.labels["main_state"].text() == "ST_ON"

    class Action:
        def __init__(self):
            self._state = None
        @property
        def state(self):
            return self._state
        @state.setter
        def state(self, value):
            on_gui()
            self._state = value

    object.__setattr__(parent, "onAction", Action())
    object.__setattr__(parent, "deviceOnAction", Action())
    for name in ("_sync_local_on_action", "_sync_toolbar_communication_controls", "_update_status_widgets"):
        object.__setattr__(parent, name, on_gui)
    in_worker(lambda: ns["_restore_off_ui_state"](controller))
    assert parent.onAction.state is None
    drain_until(lambda: parent.onAction.state is False)
    in_worker(lambda: ns["_restore_on_ui_state"](controller))
    drain_until(lambda: parent.onAction.state is True)
    in_worker(lambda: ns["_set_on_ui_state"](parent, False))
    drain_until(lambda: parent.deviceOnAction.state is False)
    assert parent.onAction.state is False

    if "_stop_refresh_timer" in ns:
        class Timer(QTimer):
            def stop(self):
                on_gui()
                super().stop()

        timer = Timer()
        timer.start(60_000)
        object.__setattr__(parent, "_refreshTimer", timer)
        object.__setattr__(parent, "_stop_refresh_timer", lambda: ns["_stop_refresh_timer"](parent))
        controller.print = lambda *args, **kwargs: None
        controller._cancel_output_commands = lambda: None
        controller._clear_transport_failures = lambda: None
        controller._dispose_device = lambda: None
        controller._sync_status_to_gui = lambda: sync(controller)
        ns["PRINT"] = types.SimpleNamespace(ERROR="error")
        ns["_PSU_COMMUNICATION_LOST_STATE"] = "Communication lost"
        in_worker(lambda: ns["_handle_transport_loss"](controller))
        assert timer.isActive(), "timer was accessed before GUI dispatch"
        drain_until(lambda: not timer.isActive())
        drain_until(lambda: parent.labels["main_state"].text() == "Communication lost")

    # A failed update must be logged, not escape a Qt slot and abort the host.
    def broken():
        raise RuntimeError("deliberately broken GUI callback")
    in_worker(lambda: invoke(broken))
    in_worker(lambda: invoke(lambda: calls.append("after-error")))
    drain_until(lambda: calls[-1] == "after-error")
    return 0


if __name__ == "__main__":
    assert sys.argv[1] == "--probe"
    raise SystemExit(probe(Path(sys.argv[2])))
