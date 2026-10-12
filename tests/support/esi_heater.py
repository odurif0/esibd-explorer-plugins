"""ESI heater rigs shared by the heater tests."""
from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from support.esi_driver import RUNTIME_NAME, make_controller, load_runtime


@pytest.fixture
def esi_heater_rig(monkeypatch):
    load_runtime()
    module = sys.modules[f"{RUNTIME_NAME}.esi.esi"]
    base = sys.modules[f"{RUNTIME_NAME}.esi.esi_base"].ESIBase
    driver = make_controller(module)
    driver.connected = True
    calls = []
    state = SimpleNamespace(
        enabled=False, active={0: False, 1: False, 2: False}, target=80.,
        limits={"voltage": 12., "current": 1., "power": 10.},
        maxima={"voltage": 22., "current": 12., "power": 180., "temperature": 175.},
        temperature=21.08, sensor_valid=True, command_status=0,
        read_status=0, stuck=False, target_status=0, target_stuck=False,
        global_status=0, state_override=None, target_calls=0,
    )

    def set_module(self, address, active):
        calls.append(("module", address, active))
        if address == 0 and state.command_status:
            return state.command_status
        if address != 0 or not state.stuck:
            state.active[address] = active
        return 0

    def get_module(self, address):
        calls.append(("read_module", address))
        return state.read_status if address == 0 else 0, state.active[address]

    def set_enable(self, active):
        calls.append(("enable", active))
        if state.global_status:
            return state.global_status
        state.enabled = active
        return 0

    def set_target(self, value):
        calls.append(("temperature", value))
        state.target_calls += 1
        if not state.target_status and not state.target_stuck:
            state.target = value
        # A successful echo is not independent target verification.
        return state.target_status, value

    def get_target(self):
        calls.append(("read_temperature",))
        return 0, state.target

    def complete(self):
        heat = (base.MS_CTRL_ACT if state.target >= 0 else 0)
        heat |= base.MS_MOD_ACT if state.active[0] else 0
        heat |= base.MS_DEV_ACT if state.enabled else 0
        if state.state_override is not None:
            heat = state.state_override
        return 0, 0, 0, 0, 0, 0, 0, 0, [0] * 5, [heat, 0, 0, 0, 0]

    monkeypatch.setattr(base, "set_module_activation_state", set_module)
    monkeypatch.setattr(base, "get_module_activation_state", get_module)
    monkeypatch.setattr(base, "set_enable", set_enable)
    monkeypatch.setattr(base, "get_enable", lambda self: (calls.append(("read_enable",)) or 0, state.enabled))
    monkeypatch.setattr(base, "set_heat_ctrl_heater_temperature", set_target)
    monkeypatch.setattr(base, "get_heat_ctrl_heater_temperature", get_target)
    monkeypatch.setattr(base, "get_heat_ctrl_hw_limits", lambda self: (0, *state.maxima.values()))
    for key in ("voltage", "current", "power"):
        monkeypatch.setattr(base, f"get_heat_ctrl_{key}_limit", lambda self, key=key: (0, state.limits[key]))
        monkeypatch.setattr(base, f"set_heat_ctrl_{key}_limit", lambda *args: pytest.fail("Activation must not choose electrical limits"))
    monkeypatch.setattr(base, "get_module_data_ready_flags", lambda self, address: (0, 1))
    monkeypatch.setattr(base, "get_heat_ctrl_monitoring", lambda self: (0, state.sensor_valid, 0., 0., 0., state.temperature))
    monkeypatch.setattr(base, "get_heat_ctrl_output_voltage", lambda self: (0, 0.))
    monkeypatch.setattr(base, "get_heat_ctrl_heater_power", lambda self: (0, 0.))
    monkeypatch.setattr(base, "get_heat_ctrl_ilock_state", lambda self: (0, 0))
    monkeypatch.setattr(base, "get_heat_ctrl_housekeeping", lambda self: (0, True, 3.3, 25., 5., 24., 25.))
    monkeypatch.setattr(base, "get_housekeeping", lambda self: (0, 24., 5., 3.3, 25., 25.))
    monkeypatch.setattr(base, "get_complete_state", complete)
    monkeypatch.setattr(base, "get_hv_supply_params_pwm", lambda self, address: (0, 1., .5, 0., 0., 0., 0., state.active[address], 0))
    monkeypatch.setattr(base, "get_hv_supply_target_output_voltage", lambda self, address: (0, 0.))
    monkeypatch.setattr(base, "set_hv_supply_target_output_voltage", lambda self, address, value: calls.append(("hv_target", address, value)) or 0)
    monkeypatch.setattr(base, "get_hv_supply_meas_ranges", lambda self, address: (0, False, False))
    monkeypatch.setattr(base, "get_hv_supply_output_voltage", lambda self, address: (0, True, 0.))
    monkeypatch.setattr(base, "get_hv_supply_output_current", lambda self, address: (0, True, 0.))
    monkeypatch.setattr(base, "get_module_led_data", lambda self, address: (0, True, True, False))
    monkeypatch.setattr(base, "close_port", lambda self: calls.append(("close",)) or 0)
    yield SimpleNamespace(driver=driver, state=state, calls=calls, module=module, base=base)
    type(driver)._active_connections.clear()


def snapshot():
    return dict(main_state={'name': 'STATE_ON'}, device_state={'hex': '0x0'}, enabled=True,
                modules={}, interlock_state={'flags': []}, heat=dict(
                    monitor_temperature_c=50., target_temperature_c=50., monitor_current_a=2.,
                    heater_power_w=8., hardware_limits={'max_temperature_c': 175.}, valid=True,
                    interlock_state=0, active=True, module_active=True, module_gate_active=True,
                    device_gate_active=True, control_active=True))
