"""Real Qt OFF/Disconnect controls and Explorer's Device.close after a failed Open."""
from __future__ import annotations

import ast
from importlib.metadata import distribution
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize('scale', ['1', '1.5'])
@pytest.mark.parametrize('action', ['disconnect', 'local_off', 'global_off', 'host_close'])
def test_failed_open_close_controls(action, scale, tmp_path):
    script = Path(__file__).with_name('test_dmmr_connection_retry_ui.py')
    result = subprocess.run(
        [sys.executable, str(script), '-5', str(tmp_path), action],
        env={**os.environ, 'QT_QPA_PLATFORM': 'offscreen', 'QT_SCALE_FACTOR': scale},
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode == 77:
        pytest.skip('Real Qt or Explorer source unavailable')
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'Traceback' not in result.stderr, result.stderr


def host_device_close(parent):
    """Execute the real host close flow, with no record/export or outer dock to destroy."""
    source = os.environ.get('ESIBD_EXPLORER_SOURCE')
    path = (Path(source) / 'esibd/plugins.py' if source else
            Path(distribution('esibd-explorer').locate_file('esibd/plugins.py')))
    tree = ast.parse(path.read_text(encoding='utf-8'))
    device = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'Device')
    close = next(node for node in device.body if isinstance(node, ast.FunctionDef) and node.name == 'close')
    class Base:
        def close(self):
            return True
    ns = {'Base': Base}
    exec(compile('class HostDevice(Base):\n' + '\n'.join('    ' + line for line in ast.unparse(close).splitlines()),
                 str(path), 'exec'), ns)
    wrapper = ns['HostDevice']()
    wrapper.initialized = parent.controller.initialized
    wrapper.hasRecorded = False  # Open never succeeded: there is no acquired history.
    wrapper.closeCommunication = parent.closeCommunication
    return wrapper.close()
