"""Heater activation against a stateful instrument simulation, never hardware.

Electrical values below are test fixtures, NOT recommended capillary settings.
"""
from __future__ import annotations

import math
import sys
import threading
from types import SimpleNamespace

import pytest

from test_esi_driver_behavior import RUNTIME_NAME, _controller, _load_runtime


@pytest.fixture
def rig(monkeypatch):
    _load_runtime()
    module = sys.modules[f"{RUNTIME_NAME}.esi.esi"]
    base = sys.modules[f"{RUNTIME_NAME}.esi.esi_base"].ESIBase
    driver = _controller(module)
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


def test_on_commands_and_verifies_heater_module_before_shared_gate(rig):
    assert rig.driver.set_output_active(0, True, timeout_s=.2) is True
    assert rig.state.active[0] is True
    assert rig.state.enabled is True
    assert rig.calls.index(("module", 0, True)) < rig.calls.index(("read_module", 0)) < rig.calls.index(("enable", True))
    assert rig.state.limits == {"voltage": 12., "current": 1., "power": 10.}


def test_off_disables_the_module_without_stopping_the_other_outputs(rig):
    rig.state.enabled = True
    rig.state.active = {0: True, 1: True, 2: True}
    assert rig.driver.set_output_active(0, False, timeout_s=.2) is False
    assert rig.state.active == {0: False, 1: True, 2: True}
    assert rig.state.enabled is True
    assert rig.state.target == 0.
    assert ("read_module", 0) in rig.calls
    assert not any(call[0] in ("enable", "hv_target") for call in rig.calls)


@pytest.mark.parametrize("active", [False, True])
@pytest.mark.parametrize("failure", ["ack", "read", "mismatch"])
def test_no_ack_or_unconfirmed_module_state_never_succeeds(rig, active, failure):
    rig.state.active[0] = not active
    if failure == "ack":
        rig.state.command_status = -10
    elif failure == "read":
        rig.state.read_status = -10
    else:
        rig.state.stuck = True
    with pytest.raises(RuntimeError, match="heat|module 0"):
        rig.driver.set_output_active(0, active, timeout_s=.2)
    assert ("enable", True) not in rig.calls
    if failure == "ack":
        assert ("read_module", 0) not in rig.calls
    if not active:
        assert rig.state.target == 0., "A returned disable error must not skip target-zero fallback"


def test_safe_off_checks_heat_and_still_disables_hv_and_global_on_returned_error(rig):
    rig.state.enabled = True
    rig.state.active = {0: True, 1: True, 2: True}
    rig.state.command_status = -10
    with pytest.raises(RuntimeError, match="safe OFF.*(heat|module 0)"):
        rig.driver.force_safe_off(timeout_s=.2)
    assert rig.state.active == {0: True, 1: False, 2: False}
    assert rig.state.enabled is False
    assert rig.state.target == 0.
    assert rig.driver.connected is True


def test_disconnect_retains_port_when_heater_disable_is_unconfirmed(rig):
    rig.driver._set_port_claimed(True)
    rig.state.active[0] = True
    rig.state.stuck = True
    rig.driver._verify_hv_discharge = lambda *a, **kw: None
    with pytest.raises(RuntimeError, match="safe OFF.*(heat|module 0)"):
        rig.driver.disconnect(timeout_s=.2)
    assert ("close",) not in rig.calls
    assert rig.driver.connected is True
    assert rig.driver._dll_port_claimed is True


@pytest.mark.parametrize("key", ["voltage", "current", "power"])
@pytest.mark.parametrize("value", [0., -1., float("nan"), float("inf"), 999.])
def test_on_requires_explicit_valid_electrical_limits_without_choosing_them(rig, key, value):
    rig.state.limits[key] = value
    with pytest.raises((RuntimeError, ValueError), match="heat.*limit"):
        rig.driver.set_output_active(0, True, timeout_s=.2)
    assert ("module", 0, True) not in rig.calls
    assert ("enable", True) not in rig.calls
    if math.isnan(value):
        assert math.isnan(rig.state.limits[key])
    else:
        assert rig.state.limits[key] == value


@pytest.mark.parametrize("value,valid", [(521.975, True), (float("nan"), True), (21., False), (-1., True)])
def test_on_rechecks_sensor_in_the_driver_not_only_cached_gui_state(rig, value, valid):
    rig.state.temperature, rig.state.sensor_valid = value, valid
    with pytest.raises(RuntimeError, match="heat.*(sensor|temperature|readback)"):
        rig.driver.set_output_active(0, True, timeout_s=.2)
    assert ("module", 0, True) not in rig.calls


def test_off_does_not_depend_on_valid_limits_or_temperature(rig):
    rig.state.active[0] = True
    rig.state.sensor_valid = False
    rig.state.limits = dict.fromkeys(rig.state.limits, 0.)
    rig.state.maxima = dict.fromkeys(rig.state.maxima, float("nan"))
    assert rig.driver.set_output_active(0, False, timeout_s=.2) is False
    assert rig.state.active[0] is False


def test_heater_target_is_independently_read_back_before_activation(rig):
    rig.state.target_stuck = True
    with pytest.raises(RuntimeError, match="temperature.*verification|target.*verification"):
        rig.driver.set_heater_temperature(175., timeout_s=.2)
    assert ("module", 0, True) not in rig.calls


@pytest.mark.parametrize("enabled,module,state_bits,expected", [
    (True, False, 0x8100, False),  # Real post-OFF inventory shape, but nonzero target here.
    (True, True, 0xC100, True),
    (False, True, 0x4100, False),
    (True, True, 0x8100, False),   # Direct readback and module state disagree.
    (True, True, 0xC000, False),   # Module armed, temperature regulation inactive.
])
def test_diagnostics_use_hardware_gates_and_control_not_positive_target(rig, enabled, module, state_bits, expected):
    rig.state.enabled = enabled
    rig.state.active[0] = module
    rig.state.state_override = state_bits
    data = rig.driver.collect_diagnostics(timeout_s=.2)
    assert data["heat"]["active"] is expected
    assert data["heat"]["module_active"] is module
    assert data["heat"]["control_active"] is bool(state_bits & 0x100)
    assert data["heat"]["module_gate_active"] is bool(state_bits & 0x4000)
    assert data["heat"]["device_gate_active"] is bool(state_bits & 0x8000)


def test_zero_target_is_not_mistaken_for_module_off(rig):
    rig.state.target = 0.
    rig.state.enabled = True
    rig.state.active[0] = True
    assert rig.driver.collect_diagnostics(timeout_s=.2)["heat"]["active"] is True


@pytest.mark.parametrize("operation", ["on", "off", "global_off"])
def test_blocked_native_activation_does_not_trigger_global_enable_or_concurrent_close(rig, monkeypatch, operation):
    entered, release = threading.Event(), threading.Event()

    def blocked(self, address, active):
        rig.calls.append(("module", address, active))
        entered.set()
        release.wait(2)
        return 0

    monkeypatch.setattr(rig.base, "set_module_activation_state", blocked)
    try:
        with pytest.raises(RuntimeError, match="timed out"):
            if operation == "global_off":
                rig.driver.force_safe_off(timeout_s=.01)
            else:
                rig.driver.set_output_active(0, operation == "on", timeout_s=.01)
        assert entered.is_set()
        assert rig.driver._transport_poisoned is True
        with pytest.raises(Exception):
            rig.driver.disconnect(timeout_s=.01)
        assert ("enable", True) not in rig.calls
        assert ("close",) not in rig.calls
        before_return = list(rig.calls)
    finally:
        release.set()
        pending = getattr(rig.driver, "_pending_dll_call", None)
        if pending:
            pending["thread"].join(1)
            assert not pending["thread"].is_alive()
    assert rig.calls == before_return, "A late native return must not resume the command batch"


def test_inventory_enables_communication_only_after_heater_standby_is_verified(rig):
    rig.state.active[0] = True
    rig.driver._prepare_safe_inventory(.2)
    assert rig.state.active[0] is False
    assert rig.calls.index(("read_module", 0)) < rig.calls.index(("enable", True))


@pytest.mark.parametrize("failure", ["write", "readback"])
def test_bad_zero_target_does_not_skip_module_disable(rig, failure):
    rig.state.enabled = True
    rig.state.active[0] = True
    if failure == "write":
        rig.state.target_status = -11
    else:
        rig.state.target_stuck = True
    with pytest.raises(RuntimeError, match="heater OFF failed"):
        rig.driver.set_output_active(0, False, timeout_s=.2)
    assert rig.state.active[0] is False
    assert rig.state.enabled is True


@pytest.mark.parametrize("key", ["voltage", "current", "power", "temperature"])
@pytest.mark.parametrize("value", [float("nan"), float("inf"), 0., -1.])
def test_invalid_hardware_limits_cannot_authorize_heater_on(rig, key, value):
    rig.state.maxima[key] = value
    with pytest.raises(RuntimeError, match="heat.*limit"):
        rig.driver.set_output_active(0, True, timeout_s=.2)
    assert ("module", 0, True) not in rig.calls


@pytest.mark.parametrize("fault", ["sensor", "limits"])
def test_live_temperature_increase_is_validated_before_the_write(rig, fault):
    rig.state.active[0] = rig.state.enabled = True
    if fault == "sensor":
        rig.state.sensor_valid = False
    else:
        rig.state.limits["power"] = 0.
    with pytest.raises(RuntimeError, match="heater"):
        rig.driver.set_heater_temperature(175., timeout_s=.2)
    assert rig.state.target == 80.
    assert rig.state.target_calls == 0


def test_inventory_never_reenergizes_stored_nonzero_hv_targets(rig, monkeypatch):
    rig.state.active = {0: True, 1: True, 2: True}
    hv_targets = {1: 150., 2: 250.}  # Simulation only.
    exposures = []
    def enable(self, active):
        rig.calls.append(("enable", active))
        exposures.extend(address for address in hv_targets
                         if active and rig.state.active[address] and hv_targets[address])
        rig.state.enabled = active
        return 0
    monkeypatch.setattr(rig.base, "set_enable", enable)
    rig.driver._prepare_safe_inventory(.2)
    assert exposures == []
    assert not any(rig.state.active.values())


@pytest.mark.parametrize("address", [1, 2])
def test_inventory_does_not_open_global_gate_after_hv_disable_failure(rig, monkeypatch, address):
    original = rig.base.set_module_activation_state
    monkeypatch.setattr(rig.base, "set_module_activation_state",
                        lambda self, a, active: -10 if a == address else original(self, a, active))
    with pytest.raises(RuntimeError):
        rig.driver._prepare_safe_inventory(.2)
    assert ("enable", True) not in rig.calls


@pytest.mark.parametrize("address", [1, 2])
@pytest.mark.parametrize("failure", ["stuck", "read"])
def test_inventory_requires_verified_hv_standby(rig, monkeypatch, address, failure):
    original = rig.base.get_hv_supply_params_pwm
    def pwm(self, a):
        result = list(original(self, a))
        if a == address:
            result[0] = -10 if failure == "read" else 0
            result[7] = True
        return tuple(result)
    monkeypatch.setattr(rig.base, "get_hv_supply_params_pwm", pwm)
    with pytest.raises(RuntimeError):
        rig.driver._prepare_safe_inventory(.2)
    assert ("enable", True) not in rig.calls


@pytest.mark.parametrize("applied", [float("nan"), -1., 175.1])
def test_applied_temperature_must_stay_within_hardware_limits(rig, monkeypatch, applied):
    monkeypatch.setattr(rig.base, "set_heat_ctrl_heater_temperature", lambda self, target: (0, applied))
    with pytest.raises(RuntimeError, match="applied temperature"):
        rig.driver.set_heater_temperature(175., timeout_s=.2)
    assert ("module", 0, True) not in rig.calls


@pytest.mark.parametrize("disagreement", [False, True])
def test_temperature_uses_applied_value_with_independent_readback(rig, monkeypatch, disagreement):
    def quantized(self, requested):
        applied = 100.123  # Simulated native in/out rounding, not a hardware specification.
        rig.state.target = 99. if disagreement else applied
        return 0, applied
    monkeypatch.setattr(rig.base, "set_heat_ctrl_heater_temperature", quantized)
    if disagreement:
        with pytest.raises(RuntimeError, match="verification"):
            rig.driver.set_heater_temperature(100.1234, timeout_s=.2)
    else:
        assert rig.driver.set_heater_temperature(100.1234, timeout_s=.2) == 100.123


@pytest.mark.parametrize("operation,blocked_name", [
    *(('configuration', name) for name in (
        'get_heat_ctrl_hw_limits', 'get_heat_ctrl_voltage_limit',
        'get_heat_ctrl_current_limit', 'get_heat_ctrl_power_limit',
        'get_heat_ctrl_heater_temperature')),
    *(('configure', name) for name in (
        'get_heat_ctrl_hw_limits', 'set_heat_ctrl_voltage_limit',
        'set_heat_ctrl_current_limit', 'set_heat_ctrl_power_limit')),
    *(('diagnostics', name) for name in (
        'get_complete_state', 'get_enable', 'get_hv_supply_params_pwm',
        'get_heat_ctrl_monitoring', 'get_heat_ctrl_hw_limits')),
])
def test_configuration_and_diagnostics_never_resume_after_timeout(rig, monkeypatch, operation, blocked_name):
    entered, release = threading.Event(), threading.Event()
    observed = []
    for name in ('voltage', 'current', 'power'):
        monkeypatch.setattr(rig.base, f'set_heat_ctrl_{name}_limit', lambda self, value: (0, value))
    for name, original in list(vars(rig.base).items()):
        if not callable(original) or not name.startswith(('get_', 'set_', 'close_port')):
            continue
        def native(self, *args, _name=name, _original=original, **kwargs):
            observed.append(_name)
            if _name == blocked_name:
                entered.set()
                release.wait(3)
            return _original(self, *args, **kwargs)
        monkeypatch.setattr(rig.base, name, native)
    try:
        with pytest.raises(RuntimeError, match='timed out'):
            if operation == 'configuration':
                rig.driver.get_heat_configuration(timeout_s=.01)
            elif operation == 'configure':
                rig.driver.configure_heat_limits(voltage_v=12., current_a=1., power_w=10., timeout_s=.01)
            else:
                rig.driver.collect_diagnostics(timeout_s=.01)
        assert entered.is_set()
        assert rig.driver._transport_poisoned
        before = list(observed)
    finally:
        release.set()
        pending = getattr(rig.driver, '_pending_dll_call', None)
        if pending:
            pending['thread'].join(2)
            assert not pending['thread'].is_alive()
    assert observed == before, 'No native read or write may follow the late return'


def test_reported_175_degree_limit_is_inclusive_not_increased(rig):
    assert rig.driver.set_heater_temperature(175., timeout_s=.2) == 175.
    assert rig.driver.set_output_active(0, True, timeout_s=.2) is True
    with pytest.raises(ValueError, match="hardware maximum 175"):
        rig.driver.set_heater_temperature(175.1, timeout_s=.2)
    assert rig.state.target == 175.
