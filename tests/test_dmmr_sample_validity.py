"""A failed read is missing data, never another sample of the last value."""

import math
import types


from support.dmmr import load_plugin


def test_dmmr_state_poll_failure_cannot_leave_old_samples():
    module = load_plugin()
    channel = types.SimpleNamespace(real=True, module_address=lambda: 1)
    parent = types.SimpleNamespace(getChannels=lambda: [channel], isOn=lambda: True)
    controller = module.DMMRController(parent)
    controller.initialized = controller.acquiring = True
    controller.device = object()
    controller.values = {1: 12e-12}
    controller._update_state = lambda **kwargs: setattr(controller, "acquiring", False)
    controller.readNumbers()
    assert math.isnan(controller.values[1])
