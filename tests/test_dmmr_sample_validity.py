"""A failed read is missing data, never another sample of the last value."""

import math
import threading
import types

import pytest

from test_dmmr_plugin_behavior import _load_module


@pytest.mark.parametrize("failure", ["status", "exception", "lock"])
def test_missing_dmmr_sample_becomes_nan_without_losing_other_channels(failure):
    module = _load_module()
    channels = [types.SimpleNamespace(real=True, module_address=lambda n=n: n) for n in (1, 2)]
    parent = types.SimpleNamespace(
        getChannels=lambda: channels, isOn=lambda: True, poll_timeout_s=0.1,
        getConfiguredModules=lambda: [1, 2],
    )
    controller = module.DMMRController(parent)
    controller.lock = threading.Lock()
    controller.initialized = controller.acquiring = True
    controller._update_state = lambda: None
    controller.print = lambda *args, **kwargs: None
    failing = False
    calls = []

    def get_current(address, **kwargs):
        calls.append(address)
        if failing and address == 1:
            if failure == "exception":
                raise ValueError("read failed")
            if failure == "lock":
                raise TimeoutError("controller busy")
            return 123, 12e-12, 0
        return 0, address * 12e-12, 0

    controller.device = types.SimpleNamespace(NO_ERR=0, get_module_current=get_current)
    controller.readNumbers()
    assert controller.values == {1: 12e-12, 2: 24e-12}
    failing = True
    for _ in range(100):
        controller.readNumbers()
        assert math.isnan(controller.values[1])
        assert controller.values[2] == 24e-12
    failing = False
    controller.readNumbers()
    assert controller.values == {1: 12e-12, 2: 24e-12}
    assert len(calls) == 204


def test_dmmr_state_poll_failure_cannot_leave_old_samples():
    module = _load_module()
    channel = types.SimpleNamespace(real=True, module_address=lambda: 1)
    parent = types.SimpleNamespace(getChannels=lambda: [channel], isOn=lambda: True)
    controller = module.DMMRController(parent)
    controller.initialized = controller.acquiring = True
    controller.device = object()
    controller.values = {1: 12e-12}
    controller._update_state = lambda: setattr(controller, "acquiring", False)
    controller.readNumbers()
    assert math.isnan(controller.values[1])
