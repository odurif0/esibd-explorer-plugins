"""Heater activation against a stateful instrument simulation, never hardware.

Electrical values below are test fixtures, NOT recommended capillary settings.
"""
from __future__ import annotations

import math
import threading

import pytest



def test_on_commands_and_verifies_heater_module_before_shared_gate(esi_heater_rig):
    assert esi_heater_rig.driver.set_output_active(0, True, timeout_s=.2) is True
    assert esi_heater_rig.state.active[0] is True
    assert esi_heater_rig.state.enabled is True
    assert esi_heater_rig.calls.index(("module", 0, True)) < esi_heater_rig.calls.index(("read_module", 0)) < esi_heater_rig.calls.index(("enable", True))
    assert esi_heater_rig.state.limits == {"voltage": 12., "current": 1., "power": 10.}


def test_off_disables_the_module_without_stopping_the_other_outputs(esi_heater_rig):
    esi_heater_rig.state.enabled = True
    esi_heater_rig.state.active = {0: True, 1: True, 2: True}
    assert esi_heater_rig.driver.set_output_active(0, False, timeout_s=.2) is False
    assert esi_heater_rig.state.active == {0: False, 1: True, 2: True}
    assert esi_heater_rig.state.enabled is True
    assert esi_heater_rig.state.target == 0.
    assert ("read_module", 0) in esi_heater_rig.calls
    assert not any(call[0] in ("enable", "hv_target") for call in esi_heater_rig.calls)


@pytest.mark.parametrize("active", [False, True])
@pytest.mark.parametrize("failure", ["ack", "read", "mismatch"])
def test_no_ack_or_unconfirmed_module_state_never_succeeds(esi_heater_rig, active, failure):
    esi_heater_rig.state.active[0] = not active
    if failure == "ack":
        esi_heater_rig.state.command_status = -10
    elif failure == "read":
        esi_heater_rig.state.read_status = -10
    else:
        esi_heater_rig.state.stuck = True
    with pytest.raises(RuntimeError, match="heat|module 0"):
        esi_heater_rig.driver.set_output_active(0, active, timeout_s=.2)
    assert ("enable", True) not in esi_heater_rig.calls
    if failure == "ack":
        assert ("read_module", 0) not in esi_heater_rig.calls
    if not active:
        assert esi_heater_rig.state.target == 0., "A returned disable error must not skip target-zero fallback"


def test_safe_off_checks_heat_and_still_disables_hv_and_global_on_returned_error(esi_heater_rig):
    esi_heater_rig.state.enabled = True
    esi_heater_rig.state.active = {0: True, 1: True, 2: True}
    esi_heater_rig.state.command_status = -10
    with pytest.raises(RuntimeError, match="safe OFF.*(heat|module 0)"):
        esi_heater_rig.driver.force_safe_off(timeout_s=.2)
    assert esi_heater_rig.state.active == {0: True, 1: False, 2: False}
    assert esi_heater_rig.state.enabled is False
    assert esi_heater_rig.state.target == 0.
    assert esi_heater_rig.driver.connected is True


def test_disconnect_retains_port_when_heater_disable_is_unconfirmed(esi_heater_rig):
    esi_heater_rig.driver._set_port_claimed(True)
    esi_heater_rig.state.active[0] = True
    esi_heater_rig.state.stuck = True
    esi_heater_rig.driver._verify_hv_discharge = lambda *a, **kw: None
    with pytest.raises(RuntimeError, match="safe OFF.*(heat|module 0)"):
        esi_heater_rig.driver.disconnect(timeout_s=.2)
    assert ("close",) not in esi_heater_rig.calls
    assert esi_heater_rig.driver.connected is True
    assert esi_heater_rig.driver._dll_port_claimed is True


@pytest.mark.parametrize("key", ["voltage", "current", "power"])
@pytest.mark.parametrize("value", [0., -1., float("nan"), float("inf"), 999.])
def test_on_requires_explicit_valid_electrical_limits_without_choosing_them(esi_heater_rig, key, value):
    esi_heater_rig.state.limits[key] = value
    with pytest.raises((RuntimeError, ValueError), match="heat.*limit"):
        esi_heater_rig.driver.set_output_active(0, True, timeout_s=.2)
    assert ("module", 0, True) not in esi_heater_rig.calls
    assert ("enable", True) not in esi_heater_rig.calls
    if math.isnan(value):
        assert math.isnan(esi_heater_rig.state.limits[key])
    else:
        assert esi_heater_rig.state.limits[key] == value


@pytest.mark.parametrize("value,valid", [(521.975, True), (float("nan"), True), (21., False), (-1., True)])
def test_on_rechecks_sensor_in_the_driver_not_only_cached_gui_state(esi_heater_rig, value, valid):
    esi_heater_rig.state.temperature, esi_heater_rig.state.sensor_valid = value, valid
    with pytest.raises(RuntimeError, match="heat.*(sensor|temperature|readback)"):
        esi_heater_rig.driver.set_output_active(0, True, timeout_s=.2)
    assert ("module", 0, True) not in esi_heater_rig.calls


def test_off_does_not_depend_on_valid_limits_or_temperature(esi_heater_rig):
    esi_heater_rig.state.active[0] = True
    esi_heater_rig.state.sensor_valid = False
    esi_heater_rig.state.limits = dict.fromkeys(esi_heater_rig.state.limits, 0.)
    esi_heater_rig.state.maxima = dict.fromkeys(esi_heater_rig.state.maxima, float("nan"))
    assert esi_heater_rig.driver.set_output_active(0, False, timeout_s=.2) is False
    assert esi_heater_rig.state.active[0] is False


def test_heater_target_is_independently_read_back_before_activation(esi_heater_rig):
    esi_heater_rig.state.target_stuck = True
    with pytest.raises(RuntimeError, match="temperature.*verification|target.*verification"):
        esi_heater_rig.driver.set_heater_temperature(175., timeout_s=.2)
    assert ("module", 0, True) not in esi_heater_rig.calls


@pytest.mark.parametrize("enabled,module,state_bits,expected", [
    (True, False, 0x8100, False),  # Real post-OFF inventory shape, but nonzero target here.
    (True, True, 0xC100, True),
    (False, True, 0x4100, False),
    (True, True, 0x8100, False),   # Direct readback and module state disagree.
    (True, True, 0xC000, False),   # Module armed, temperature regulation inactive.
])


def test_diagnostics_use_hardware_gates_and_control_not_positive_target(esi_heater_rig, enabled, module, state_bits, expected):
    esi_heater_rig.state.enabled = enabled
    esi_heater_rig.state.active[0] = module
    esi_heater_rig.state.state_override = state_bits
    data = esi_heater_rig.driver.collect_diagnostics(timeout_s=.2)
    assert data["heat"]["active"] is expected
    assert data["heat"]["module_active"] is module
    assert data["heat"]["control_active"] is bool(state_bits & 0x100)
    assert data["heat"]["module_gate_active"] is bool(state_bits & 0x4000)
    assert data["heat"]["device_gate_active"] is bool(state_bits & 0x8000)


def test_zero_target_is_not_mistaken_for_module_off(esi_heater_rig):
    esi_heater_rig.state.target = 0.
    esi_heater_rig.state.enabled = True
    esi_heater_rig.state.active[0] = True
    assert esi_heater_rig.driver.collect_diagnostics(timeout_s=.2)["heat"]["active"] is True


@pytest.mark.parametrize("operation", ["on", "off", "global_off"])
def test_blocked_native_activation_does_not_trigger_global_enable_or_concurrent_close(esi_heater_rig, monkeypatch, operation):
    entered, release = threading.Event(), threading.Event()

    def blocked(self, address, active):
        esi_heater_rig.calls.append(("module", address, active))
        entered.set()
        release.wait(2)
        return 0

    monkeypatch.setattr(esi_heater_rig.base, "set_module_activation_state", blocked)
    try:
        with pytest.raises(RuntimeError, match="timed out"):
            if operation == "global_off":
                esi_heater_rig.driver.force_safe_off(timeout_s=.01)
            else:
                esi_heater_rig.driver.set_output_active(0, operation == "on", timeout_s=.01)
        assert entered.is_set()
        assert esi_heater_rig.driver._transport_poisoned is True
        with pytest.raises(Exception):
            esi_heater_rig.driver.disconnect(timeout_s=.01)
        assert ("enable", True) not in esi_heater_rig.calls
        assert ("close",) not in esi_heater_rig.calls
        before_return = list(esi_heater_rig.calls)
    finally:
        release.set()
        pending = getattr(esi_heater_rig.driver, "_pending_dll_call", None)
        if pending:
            pending["thread"].join(1)
            assert not pending["thread"].is_alive()
    assert esi_heater_rig.calls == before_return, "A late native return must not resume the command batch"


def test_inventory_enables_communication_only_after_heater_standby_is_verified(esi_heater_rig):
    esi_heater_rig.state.active[0] = True
    esi_heater_rig.driver._prepare_safe_inventory(.2)
    assert esi_heater_rig.state.active[0] is False
    assert esi_heater_rig.calls.index(("read_module", 0)) < esi_heater_rig.calls.index(("enable", True))


@pytest.mark.parametrize("failure", ["write", "readback"])
def test_bad_zero_target_does_not_skip_module_disable(esi_heater_rig, failure):
    esi_heater_rig.state.enabled = True
    esi_heater_rig.state.active[0] = True
    if failure == "write":
        esi_heater_rig.state.target_status = -11
    else:
        esi_heater_rig.state.target_stuck = True
    with pytest.raises(RuntimeError, match="heater OFF failed"):
        esi_heater_rig.driver.set_output_active(0, False, timeout_s=.2)
    assert esi_heater_rig.state.active[0] is False
    assert esi_heater_rig.state.enabled is True


@pytest.mark.parametrize("key", ["voltage", "current", "power", "temperature"])
@pytest.mark.parametrize("value", [float("nan"), float("inf"), 0., -1.])
def test_invalid_hardware_limits_cannot_authorize_heater_on(esi_heater_rig, key, value):
    esi_heater_rig.state.maxima[key] = value
    with pytest.raises(RuntimeError, match="heat.*limit"):
        esi_heater_rig.driver.set_output_active(0, True, timeout_s=.2)
    assert ("module", 0, True) not in esi_heater_rig.calls


@pytest.mark.parametrize("fault", ["sensor", "limits"])
def test_live_temperature_increase_is_validated_before_the_write(esi_heater_rig, fault):
    esi_heater_rig.state.active[0] = esi_heater_rig.state.enabled = True
    if fault == "sensor":
        esi_heater_rig.state.sensor_valid = False
    else:
        esi_heater_rig.state.limits["power"] = 0.
    with pytest.raises(RuntimeError, match="heater"):
        esi_heater_rig.driver.set_heater_temperature(175., timeout_s=.2)
    assert esi_heater_rig.state.target == 80.
    assert esi_heater_rig.state.target_calls == 0


def test_inventory_never_reenergizes_stored_nonzero_hv_targets(esi_heater_rig, monkeypatch):
    esi_heater_rig.state.active = {0: True, 1: True, 2: True}
    hv_targets = {1: 150., 2: 250.}  # Simulation only.
    exposures = []
    def enable(self, active):
        esi_heater_rig.calls.append(("enable", active))
        exposures.extend(address for address in hv_targets
                         if active and esi_heater_rig.state.active[address] and hv_targets[address])
        esi_heater_rig.state.enabled = active
        return 0
    monkeypatch.setattr(esi_heater_rig.base, "set_enable", enable)
    esi_heater_rig.driver._prepare_safe_inventory(.2)
    assert exposures == []
    assert not any(esi_heater_rig.state.active.values())


@pytest.mark.parametrize("address", [1, 2])
def test_inventory_does_not_open_global_gate_after_hv_disable_failure(esi_heater_rig, monkeypatch, address):
    original = esi_heater_rig.base.set_module_activation_state
    monkeypatch.setattr(esi_heater_rig.base, "set_module_activation_state",
                        lambda self, a, active: -10 if a == address else original(self, a, active))
    with pytest.raises(RuntimeError):
        esi_heater_rig.driver._prepare_safe_inventory(.2)
    assert ("enable", True) not in esi_heater_rig.calls


@pytest.mark.parametrize("address", [1, 2])
@pytest.mark.parametrize("failure", ["stuck", "read"])
def test_inventory_requires_verified_hv_standby(esi_heater_rig, monkeypatch, address, failure):
    original = esi_heater_rig.base.get_hv_supply_params_pwm
    def pwm(self, a):
        result = list(original(self, a))
        if a == address:
            result[0] = -10 if failure == "read" else 0
            result[7] = True
        return tuple(result)
    monkeypatch.setattr(esi_heater_rig.base, "get_hv_supply_params_pwm", pwm)
    with pytest.raises(RuntimeError):
        esi_heater_rig.driver._prepare_safe_inventory(.2)
    assert ("enable", True) not in esi_heater_rig.calls


@pytest.mark.parametrize("applied", [float("nan"), -1., 175.1])
def test_applied_temperature_must_stay_within_hardware_limits(esi_heater_rig, monkeypatch, applied):
    monkeypatch.setattr(esi_heater_rig.base, "set_heat_ctrl_heater_temperature", lambda self, target: (0, applied))
    with pytest.raises(RuntimeError, match="applied temperature"):
        esi_heater_rig.driver.set_heater_temperature(175., timeout_s=.2)
    assert ("module", 0, True) not in esi_heater_rig.calls


@pytest.mark.parametrize("disagreement", [False, True])
def test_temperature_uses_applied_value_with_independent_readback(esi_heater_rig, monkeypatch, disagreement):
    def quantized(self, requested):
        applied = 100.123  # Simulated native in/out rounding, not a hardware specification.
        esi_heater_rig.state.target = 99. if disagreement else applied
        return 0, applied
    monkeypatch.setattr(esi_heater_rig.base, "set_heat_ctrl_heater_temperature", quantized)
    if disagreement:
        with pytest.raises(RuntimeError, match="verification"):
            esi_heater_rig.driver.set_heater_temperature(100.1234, timeout_s=.2)
    else:
        assert esi_heater_rig.driver.set_heater_temperature(100.1234, timeout_s=.2) == 100.123


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


def test_configuration_and_diagnostics_never_resume_after_timeout(esi_heater_rig, monkeypatch, operation, blocked_name):
    entered, release = threading.Event(), threading.Event()
    observed = []
    for name in ('voltage', 'current', 'power'):
        monkeypatch.setattr(esi_heater_rig.base, f'set_heat_ctrl_{name}_limit', lambda self, value: (0, value))
    for name, original in list(vars(esi_heater_rig.base).items()):
        if not callable(original) or not name.startswith(('get_', 'set_', 'close_port')):
            continue
        def native(self, *args, _name=name, _original=original, **kwargs):
            observed.append(_name)
            if _name == blocked_name:
                entered.set()
                release.wait(3)
            return _original(self, *args, **kwargs)
        monkeypatch.setattr(esi_heater_rig.base, name, native)
    try:
        with pytest.raises(RuntimeError, match='timed out'):
            if operation == 'configuration':
                esi_heater_rig.driver.get_heat_configuration(timeout_s=.01)
            elif operation == 'configure':
                esi_heater_rig.driver.configure_heat_limits(voltage_v=12., current_a=1., power_w=10., timeout_s=.01)
            else:
                esi_heater_rig.driver.collect_diagnostics(timeout_s=.01)
        assert entered.is_set()
        assert esi_heater_rig.driver._transport_poisoned
        before = list(observed)
    finally:
        release.set()
        pending = getattr(esi_heater_rig.driver, '_pending_dll_call', None)
        if pending:
            pending['thread'].join(2)
            assert not pending['thread'].is_alive()
    assert observed == before, 'No native read or write may follow the late return'


def test_reported_175_degree_limit_is_inclusive_not_increased(esi_heater_rig):
    assert esi_heater_rig.driver.set_heater_temperature(175., timeout_s=.2) == 175.
    assert esi_heater_rig.driver.set_output_active(0, True, timeout_s=.2) is True
    with pytest.raises(ValueError, match="hardware maximum 175"):
        esi_heater_rig.driver.set_heater_temperature(175.1, timeout_s=.2)
    assert esi_heater_rig.state.target == 175.
