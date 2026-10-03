"""An OFF/close between host scheduling and worker entry must cancel that ON."""
from __future__ import annotations

import ast
from importlib.metadata import distribution
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from test_dmmr_connection_retry import connection  # noqa: F401
from test_dmmr_failed_open_shutdown import ACTIONS, failed_open, request  # noqa: F401
from test_dmmr_failed_open_shutdown_ui import host_device_close


def hold_host_initialization(hw, monkeypatch):
    """Use the real Explorer initializer, holding only Thread.start for the test."""
    source = os.environ.get('ESIBD_EXPLORER_SOURCE')
    path = (Path(source) / 'esibd/core.py' if source else
            Path(distribution('esibd-explorer').locate_file('esibd/core.py')))
    tree = ast.parse(path.read_text(encoding='utf-8'))
    controller = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'DeviceController')
    method = next(n for n in controller.body if isinstance(n, ast.FunctionDef) and n.name == 'initializeCommunication')
    queued = []

    class HeldThread:
        def __init__(self, *, target, name):
            self.target, self.name = target, name
        def start(self):
            queued.append(self.target)

    ns = {'Thread': HeldThread, 'getTestMode': lambda: False, 'PRINT': SimpleNamespace(DEBUG=None)}
    exec(compile(ast.Module(body=[method], type_ignores=[]), str(path), 'exec'), ns)
    base = hw.plugin.DMMRController.__mro__[1]
    monkeypatch.setattr(base, 'initializeCommunication', ns['initializeCommunication'], raising=False)
    hw.controller.acquisitionThread = None
    return queued


def close_before_worker(hw, action):
    if action == 'host_close':
        host_device_close(hw.controller.controllerParent)
    else:
        request(hw, action)


@pytest.mark.parametrize('action', (*ACTIONS, 'host_close'))
@pytest.mark.parametrize('duplicate_on', [False, True])
def test_close_before_worker_cancels_open(failed_open, monkeypatch, action, duplicate_on):
    hw, c = failed_open, failed_open.controller
    p = c.controllerParent
    hw.block, hw.powered = False, True
    c._update_state = lambda: True
    queued = hold_host_initialization(hw, monkeypatch)
    p.onAction.state = True
    c.initializeCommunication()
    assert c.initializing and c.device is None and len(queued) == 1
    close_before_worker(hw, action)
    if duplicate_on:
        c.initializeCommunication()  # Not a new request while the old worker is still queued.
    assert len(queued) == 1
    queued.pop()()
    assert hw.calls == [], 'Cancelled queued startup must never reach native Open or Close'
    assert hw.drivers == [] and hw.runtime_calls == [] and hw.successes == []
    assert c.device is None and not c.initializing and not c.initialized and not c.acquiring
    assert c.main_state == 'Disconnected' and not p.isOn()
    c.initComplete()  # A queued success callback must not undo the cancellation either.
    assert c.device is None and not c.initialized and not c.acquiring and not p.isOn()
    assert hw.calls == [] and hw.runtime_calls == []


@pytest.mark.parametrize('action', ['device_off', 'device_close', 'host_close'])
def test_only_a_fresh_explicit_on_rearms_cancelled_initialization(failed_open, monkeypatch, action):
    hw, c = failed_open, failed_open.controller
    hw.block, hw.powered = False, True
    c._update_state = lambda: True
    queued = hold_host_initialization(hw, monkeypatch)
    c.controllerParent.onAction.state = True
    c.initializeCommunication()
    close_before_worker(hw, action)
    queued.pop()()
    assert hw.calls == [] and hw.successes == []
    c.controllerParent.onAction.state = True
    c.initializeCommunication()
    assert c.initializing and len(queued) == 1
    queued.pop()()
    assert hw.calls == ['open'] and hw.successes == [True]
    assert c.device.connected and c.device._dll_port_claimed
    assert not c.initializing


@pytest.mark.parametrize('scale', ['1', '1.5'])
@pytest.mark.parametrize('action', ['local_off', 'global_off', 'host_close'])
def test_real_qt_close_before_worker(action, scale, tmp_path):
    script = Path(__file__).with_name('test_dmmr_connection_retry_ui.py')
    result = subprocess.run(
        [sys.executable, str(script), '-5', str(tmp_path), 'queued_' + action],
        env={**os.environ, 'QT_QPA_PLATFORM': 'offscreen', 'QT_SCALE_FACTOR': scale},
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode == 77:
        pytest.skip('Real Qt or Explorer source unavailable')
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'Traceback' not in result.stderr, result.stderr
