"""AMPR must load saved channels before Explorer creates its ON/OFF action.

Run Explorer's actual Channel/Parameter widgets and configuration loader in a
subprocess. Only unrelated application services are stubbed, not isOn(), channel
construction, saved parameter restoration, colour updates, or Qt actions.
"""
from __future__ import annotations


import pytest
from support.qt import run_probe


@pytest.mark.parametrize("folder", ["ampr_a", "ampr_b"])
@pytest.mark.parametrize("legacy", [False, True])
def test_saved_channels_before_on_action(folder, legacy, tmp_path):
    run_probe(__file__, folder, str(int(legacy)), tmp_path, timeout=30)


@pytest.mark.parametrize("folder", ["ampr_a", "ampr_b"])
@pytest.mark.parametrize("state,active", [
    ("ready", True), ("unconfirmed", True), ("read_error", True),
    ("ramp_up", True), ("ramp_down", True),
    ("no_controller", False), ("no_action", False), ("action_none", False),
    ("no_initialized", False), ("not_initialized", False),
    ("no_state", False), ("empty_state", False), ("state_none", False),
    ("standby", False), ("disconnected", False), ("device_off", False),
    ("channel_off", False), ("virtual", False),
])


def test_colour_state_requires_initialized_controls(folder, state, active):
    from types import MethodType, SimpleNamespace
    from support.ampr import load_plugin

    module = load_plugin(f"{folder}/ampr_plugin.py", "ampr_colour_state")
    controller = SimpleNamespace(initialized=True, acquiring=True, main_state="ST_ON")
    parent = SimpleNamespace(controller=controller, onAction=SimpleNamespace(state=True))
    parent.isOn = MethodType(lambda self: self.onAction.state, parent)
    channel = module.AMPRChannel(parent)
    channel.enabled = channel.real = True
    channel.value = channel.monitor = 250.
    if state in {"ramp_up", "ramp_down"}:
        controller.ramping = controller.transitioning = True
        controller.acquiring = False
        controller.transition_target_on = parent.onAction.state = state == "ramp_up"
    elif state == "no_controller":
        del parent.controller
    elif state == "no_action":
        del parent.onAction
    elif state == "action_none":
        parent.onAction = None
    elif state == "no_initialized":
        del controller.initialized
    elif state == "not_initialized":
        controller.initialized = False
    elif state == "no_state":
        del controller.main_state
    elif state in {"standby", "disconnected", "empty_state", "state_none", "unconfirmed", "read_error"}:
        controller.main_state = {"standby": "ST_STBY", "disconnected": "Disconnected", "empty_state": "",
                                 "state_none": None, "unconfirmed": "Shutdown unconfirmed", "read_error": "State error"}[state]
    elif state == "device_off":
        parent.onAction.state = False
    elif state == "channel_off":
        channel.enabled = False
    elif state == "virtual":
        channel.real = False
    assert channel._output_enabled() is active
    if not active:
        assert channel._monitor_feedback_state() == "default"


