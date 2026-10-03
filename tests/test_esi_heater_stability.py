"""Offline qualification tests; the helper must never change heater control."""
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('_esi_stability_test', ROOT / 'esi/_heater_stability.py')
HELPER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = HELPER
SPEC.loader.exec_module(HELPER)
TemperatureStability = HELPER.TemperatureStability


def fill(tracker, *, start=0, count=61, target=50., temperature=None):
    for i in range(count):
        result = tracker.update(start+i, target if temperature is None else temperature(i), target)
    return result


def test_full_window_needed_and_aging_does_not_create_evidence():
    tracker = TemperatureStability()
    assert not fill(tracker, count=60)['stable']
    assert not tracker.status(60)['stable'], 'Only 59 s were observed'
    assert tracker.update(60, 50., 50.)['stable']
    assert tracker.status(62)['stable']
    assert tracker.status(62.01)['state'] == 'Unavailable'


@pytest.mark.parametrize('sign', [-1, 1])
def test_drift_in_band_still_rejected(sign):
    tracker = TemperatureStability()
    result = fill(tracker, temperature=lambda i: 50+sign*(i-30)*.15/60)
    assert result['max_error_c'] < .2
    assert result['slope_c_min'] == pytest.approx(sign*.15)
    assert not result['stable']


def test_irregular_timestamps_use_ols_not_sample_count():
    tracker = TemperatureStability(max_gap_s=3)
    times = list(range(0, 59, 2)) + [59, 61]
    for t in times:
        result = tracker.update(1e7+t, 50+(t-30)*.05/60, 50)
    assert result['stable']
    assert result['slope_c_min'] == pytest.approx(.05)
    assert result['span_s'] >= 60


def test_small_slope_does_not_hide_oscillation_outside_band():
    tracker = TemperatureStability()
    result = fill(tracker, temperature=lambda i: 50+(.3 if i % 2 else -.3))
    assert abs(result['slope_c_min']) < .1
    assert not result['stable']


@pytest.mark.parametrize('temperature', [49.8, 50.2])
def test_exact_temperature_band_edge_allowed(temperature):
    assert fill(TemperatureStability(), temperature=lambda _: temperature)['stable']


@pytest.mark.parametrize('bad', [float('nan'), float('inf'), None, True, '50'])
@pytest.mark.parametrize('field', ['temperature', 'target', 'timestamp'])
def test_invalid_values_clear_evidence(bad, field):
    tracker = TemperatureStability()
    assert fill(tracker)['stable']
    args = dict(timestamp=61., temperature=50., target=50.)
    args[field] = bad
    assert tracker.update(**args)['state'] == 'Unavailable'
    assert tracker.status(61)['samples'] == 0


@pytest.mark.parametrize('change', ['off', 'invalid', 'target', 'gap', 'duplicate', 'backwards', 'reset'])
def test_discontinuities_require_new_full_window(change):
    tracker = TemperatureStability()
    fill(tracker)
    if change == 'off':
        assert tracker.update(61, 50., 50., active=False)['state'] == 'OFF'
    elif change == 'invalid':
        tracker.update(61, 50., 50., valid=False)
    elif change == 'target':
        tracker.update(61, 50., 51.)
    elif change == 'gap':
        tracker.update(64, 50., 50.)
    elif change == 'duplicate':
        tracker.update(60, 50., 50.)
    elif change == 'backwards':
        tracker.update(59, 50., 50.)
    else:
        tracker.reset()
    assert not tracker.status(64)['stable']
    assert not fill(tracker, start=65, count=59)['stable']
    assert fill(tracker, start=124, count=2)['stable']


@pytest.mark.parametrize('parameter', ['window_s', 'tolerance_c', 'drift_c_min', 'max_gap_s'])
@pytest.mark.parametrize('bad', [0., -1., float('nan'), float('inf'), None, True])
def test_bad_parameters_rejected(parameter, bad):
    with pytest.raises(ValueError):
        TemperatureStability(**{parameter: bad})


def snapshot():
    return dict(main_state={'name': 'STATE_ON'}, device_state={'hex': '0x0'}, enabled=True,
                modules={}, interlock_state={'flags': []}, heat=dict(
                    monitor_temperature_c=50., target_temperature_c=50., monitor_current_a=2.,
                    heater_power_w=8., hardware_limits={'max_temperature_c': 175.}, valid=True,
                    interlock_state=0, active=True, module_active=True, module_gate_active=True,
                    device_gate_active=True, control_active=True))


@pytest.fixture
def controller():
    from test_esi_plugin_behavior import _load_plugin
    module = _load_plugin()
    clock = [0.]
    module.time = SimpleNamespace(monotonic=lambda: clock[0])
    parent = SimpleNamespace(interval=1000, poll_timeout_s=3, isOn=lambda: True)
    control = module.ESIController(parent)
    control._sync_status = lambda: None
    control.initialized = True
    control.device = SimpleNamespace(collect_diagnostics=lambda **kw: snapshot())
    return control, clock, module


def qualify(control, clock):
    for t in range(61):
        clock[0] = float(t)
        control.readNumbers()
    assert control.heat_stability_status()['stable']


def test_gui_uses_shared_qualifier_without_output_commands(controller):
    control, clock, _ = controller
    qualify(control, clock)
    assert control._heat_stability.__class__.__module__.startswith('_esibd_bundled_esi_stability_')
    clock[0] = 65
    assert control.heat_stability_status()['state'] == 'Unavailable'


@pytest.mark.parametrize('key', ['active', 'module_active', 'module_gate_active', 'device_gate_active', 'control_active'])
def test_missing_activation_never_qualifies(controller, key):
    control, clock, _ = controller
    data = snapshot()
    data['heat'].pop(key)
    for t in range(61):
        clock[0] = t
        control._apply_snapshot(data)
    assert not control.heat_stability_status()['stable']


@pytest.mark.parametrize('fault', ['0x1', '0x8000', 'unknown'])
def test_device_fault_blocks_qualification(controller, fault):
    control, clock, _ = controller
    qualify(control, clock)
    data = snapshot()
    data['device_state']['hex'] = fault
    clock[0] = 61
    control._apply_snapshot(data)
    assert control.heat_stability_status()['state'] == 'Unavailable'


def test_reset_and_late_poll_cannot_restore_old_stability(controller):
    control, clock, _ = controller
    qualify(control, clock)
    old_generation = control._heat_stability_generation
    control._reset_heat_stability()
    clock[0] = 61
    control._apply_snapshot(snapshot(), observed_at=61, stability_generation=old_generation)
    assert control.heat_stability_status()['samples'] == 0
    assert not control.heat_stability_status()['stable']


def test_poll_failure_clears_qualification(controller):
    control, clock, _ = controller
    qualify(control, clock)
    control.errorCount = 0
    control.print = lambda *args, **kwargs: None
    def failure(**kw):
        raise RuntimeError('missing sample')
    control.device.collect_diagnostics = failure
    clock[0] = 61
    control.readNumbers()
    assert control.heat_stability_status()['state'] == 'Unavailable'


def test_heater_command_resets_before_native_io_without_delaying_on(controller):
    control, clock, _ = controller
    qualify(control, clock)
    calls = []
    def target(value, **kwargs):
        assert not control.heat_stability_status()['stable']
        calls.append(('target', value))
    def enable(address, enabled, **kwargs):
        calls.append(('enable', address, enabled))
        return enabled
    control.device.set_heater_temperature = target
    control.device.set_output_active = enable
    channel = SimpleNamespace(is_current_channel=lambda: False, is_heat_channel=lambda: True,
                              module_address=lambda: 0, enabled=True, value=50., name='HEAT')
    control.applyValue(channel)
    assert calls == [('target', 50.), ('enable', 0, True)]
    assert clock[0] == 60, 'No dwell, sleep or polling introduced into ON'


@pytest.mark.parametrize('flag', ['active', 'valid'])
@pytest.mark.parametrize('unknown', [None, 1, 'true'])
def test_nonboolean_validity_is_not_qualification(flag, unknown):
    tracker = TemperatureStability()
    fill(tracker)
    assert tracker.update(61, 50., 50., **{flag: unknown})['state'] == 'Unavailable'


def test_displaying_stale_data_discards_old_window():
    tracker = TemperatureStability()
    fill(tracker)
    assert tracker.status(65)['state'] == 'Unavailable'
    tracker.max_gap_s = 20
    assert not tracker.status(66)['stable'], 'Changing cadence cannot revive expired evidence'


def test_poll_captures_command_generation_before_native_call(controller):
    control, clock, _ = controller
    qualify(control, clock)
    def read(**kwargs):
        control._reset_heat_stability()  # Concurrent command while this poll was in flight.
        return snapshot()
    control.device.collect_diagnostics = read
    clock[0] = 61
    control.readNumbers()
    assert control.heat_stability_status()['samples'] == 0


def test_cadence_change_requires_new_window(controller):
    control, clock, _ = controller
    qualify(control, clock)
    control.controllerParent.interval = 2000
    clock[0] = 61
    control.readNumbers()
    assert control.heat_stability_status()['samples'] == 1
    assert not control.heat_stability_status()['stable']


@pytest.mark.parametrize('enabled', [None, 1, 'true'])
def test_unknown_enable_is_not_verified_off_or_stable(controller, enabled):
    control, clock, _ = controller
    qualify(control, clock)
    data = snapshot()
    data['enabled'] = enabled
    clock[0] = 61
    control._apply_snapshot(data)
    assert control.heat_stability_status()['state'] == 'Unavailable'
