"""AMPR plugin loading and the parallel-ramp rig shared by the AMPR tests."""
from __future__ import annotations

import sys
import threading
from types import SimpleNamespace

import pytest

from support import explorer


def clear() -> None:
    """Forget the fake Explorer, the AMPR runtimes and the old shared runtime names tests guard against."""
    explorer.clear(runtimes=("ampr",))
    for name in list(sys.modules):
        if name == "cgc" or name.startswith(("cgc.", "esibd_ampr")):
            sys.modules.pop(name, None)


def load_plugin(path="ampr_a/ampr_plugin.py", module_name: str = "ampr_plugin_test"):
    """Import an AMPR entry point against a fresh fake Explorer."""
    clear()
    return explorer.load_plugin(path, module_name, runtimes=("ampr",))


@pytest.fixture(params=['ampr_a', 'ampr_b'])
def ampr_ramp_rig(request, monkeypatch):
    module = load_plugin(f'{request.param}/ampr_plugin.py', 'ampr_plugin_sync_test')
    clock = SimpleNamespace(now=0.)
    def sleep(seconds):
        clock.now += seconds
    # Do not patch the shared time module used by threading/pytest.
    module.time = SimpleNamespace(monotonic=lambda: clock.now, sleep=sleep)
    channels = [SimpleNamespace(real=True, enabled=True, value=target, ramp_rate_v_s=rate,
                    VALUE='Value', getParameterByName=lambda name: SimpleNamespace(displayDecimals=2),
                    module_address=lambda: 2, channel_number=lambda n=n: n)
                for n, target, rate in [(1, 100., 10.), (2, -200., 10.), (3, 50., 5.)]]
    on = SimpleNamespace(state=True)
    parent = SimpleNamespace(name='AMPR_A', isOn=lambda: on.state, onAction=on,
                             getChannels=lambda: channels, connect_timeout_s=5., ramp_rate_v_s=10.,
                             _sync_local_on_action=lambda: None, initialized=True, loading=False,
                             getConfiguredModules=lambda: [2])
    controller = module.AMPRController(parent)
    parent.controller = controller
    controller.initialized = True
    controller.errorCount = 0
    controller.lock = threading.Lock()
    controller._sync_status_to_gui = lambda: None
    controller.signalComm = SimpleNamespace(updateValuesSignal=SimpleNamespace(emit=lambda: None))
    logs, writes = [], []
    accepted = {ch.channel_number(): 0. for ch in channels}
    controller.print = parent.print = lambda message, **kwargs: logs.append(message)
    class Device:
        NO_ERR = 0
        connected = True
        def set_module_voltages(self, module_id, values):
            writes.append((clock.now, dict(values)))
            accepted.update(values)
            return dict.fromkeys(values, 0)
        def get_state(self):
            return (0, 0, 'ST_ON')
        def get_module_voltages(self, module_id):
            return {number: {'setpoint': value, 'measured': value}
                    for number, value in accepted.items()}
    controller.device = Device()
    return SimpleNamespace(module=module, controller=controller, parent=parent, channels=channels,
                           on=on, clock=clock, writes=writes, logs=logs)


def ramp(rig, *, down=False, rates=None):
    targets = {(2, ch.channel_number()): ch.value for ch in rig.channels}
    zero = dict.fromkeys(targets, 0.)
    rig.controller._ramp_target_voltages(start_targets=targets if down else zero,
        end_targets=zero if down else targets,
        rates_v_s=rates or {(2, ch.channel_number()): ch.ramp_rate_v_s for ch in rig.channels},
        label='down' if down else 'up')
