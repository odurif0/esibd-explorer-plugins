"""Parallel per-channel ramps, cancellable ON and preserved channel configuration."""
from types import SimpleNamespace

import pytest

from support.ampr import load_plugin
from support.ampr import ramp


@pytest.mark.parametrize('down', [False, True])
def test_every_channel_moves_from_the_first_tick_at_its_own_rate(ampr_ramp_rig, down):
    ramp(ampr_ramp_rig, down=down)
    by_channel = {i: [(t, v[i]) for t, v in ampr_ramp_rig.writes if i in v] for i in (1, 2, 3)}
    for i, start, end, rate, duration in [(1, 0., 100., 10., 10.), (2, 0., -200., 10., 20.),
                                         (3, 0., 50., 5., 10.)]:
        if down:
            start, end = end, start
        points = by_channel[i]
        assert points[0][0] == pytest.approx(.1)
        assert points[0][1] == pytest.approx(start + (1 if end > start else -1) * rate * .1)
        assert points[-1] == pytest.approx((duration, end))
        for t, value in points:
            assert abs(value - start) <= rate * t + 1e-8
        assert len(points) <= round(duration / .1) + 1, 'No writes to a finished channel'
    assert ampr_ramp_rig.clock.now == pytest.approx(20.), 'The channel durations must not be added'


def test_slow_serial_calls_count_towards_elapsed_ramp_time(ampr_ramp_rig):
    original = ampr_ramp_rig.controller.device.set_module_voltages
    def slow(*args):
        result = original(*args)
        ampr_ramp_rig.clock.now += .03
        return result
    ampr_ramp_rig.controller.device.set_module_voltages = slow
    ramp(ampr_ramp_rig)
    assert 20. <= ampr_ramp_rig.clock.now < 20.5
    for t, values in ampr_ramp_rig.writes:
        for number, value in values.items():
            assert abs(value) <= ampr_ramp_rig.channels[number - 1].ramp_rate_v_s * t + 1e-8


def test_zero_rate_is_immediate_for_only_that_channel(ampr_ramp_rig):
    ramp(ampr_ramp_rig, rates={(2, 1): 0., (2, 2): 10., (2, 3): 5.})
    first = next((t, v[1]) for t, v in ampr_ramp_rig.writes if 1 in v)
    assert first == (0., 100.)
    assert ampr_ramp_rig.clock.now == pytest.approx(20.)


@pytest.mark.parametrize('bad', [-1., float('nan'), float('inf')])
def test_invalid_rate_is_rejected_without_voltage_commands(ampr_ramp_rig, bad):
    with pytest.raises(ValueError, match='ramp rate'):
        ramp(ampr_ramp_rig, rates={(2, 1): bad, (2, 2): 10., (2, 3): 5.})
    assert not ampr_ramp_rig.writes


def test_off_is_not_ignored_while_on_transition_owns_the_worker(ampr_ramp_rig):
    assert ampr_ramp_rig.controller._begin_transition(True)
    ampr_ramp_rig.controller.toggleOnFromThread = lambda **kw: pytest.fail('Do not start a concurrent OFF worker')
    ampr_ramp_rig.module.AMPRDevice.setOn(ampr_ramp_rig.parent, on=False)
    assert not ampr_ramp_rig.on.state
    assert ampr_ramp_rig.controller._cancel_ramp
    assert ampr_ramp_rig.controller.transition_target_on is False
    assert ampr_ramp_rig.controller._setpoint_cancel.is_set()


def test_cancelled_ramp_must_not_reset_the_cancellation(ampr_ramp_rig):
    ampr_ramp_rig.controller._begin_transition(True)
    ampr_ramp_rig.module.AMPRDevice.setOn(ampr_ramp_rig.parent, on=False)
    with pytest.raises(ampr_ramp_rig.module._AMPRRampCancelled):
        ramp(ampr_ramp_rig)
    assert not ampr_ramp_rig.writes


def test_off_during_first_write_prevents_the_next_channels_from_rising(ampr_ramp_rig):
    ampr_ramp_rig.controller._begin_transition(True)
    original = ampr_ramp_rig.controller.device.set_module_voltages
    def off(*args):
        result = original(*args)
        ampr_ramp_rig.module.AMPRDevice.setOn(ampr_ramp_rig.parent, on=False)
        return result
    ampr_ramp_rig.controller.device.set_module_voltages = off
    with pytest.raises(ampr_ramp_rig.module._AMPRRampCancelled):
        ramp(ampr_ramp_rig)
    assert len(ampr_ramp_rig.writes) == 1
    assert len(ampr_ramp_rig.writes[0][1]) == 1


@pytest.mark.parametrize('when', ['initialize', 'ramp', 'completion'])
@pytest.mark.parametrize('shutdown_ok', [True, False])
def test_one_worker_handles_off_from_startup_through_ramp_completion(ampr_ramp_rig, when, shutdown_ok):
    c = ampr_ramp_rig.controller
    hw = c.device
    stops = []
    ampr_ramp_rig.module.DeviceController.toggleOn = lambda self: None
    c._refresh_module_scan = lambda: None
    c._apply_module_voltage_limits = lambda: None
    c._update_state = lambda **kwargs: setattr(c, 'main_state', 'ST_ON')
    c.startAcquisition = lambda: pytest.fail('Acquisition must not start after OFF')
    hw.close = lambda: None
    def stop():
        stops.append(True)
        if shutdown_ok:
            hw.connected = False
            hw._dll_port_claimed = False
        return shutdown_ok
    hw.shutdown = stop
    def off():
        ampr_ramp_rig.module.AMPRDevice.setOn(ampr_ramp_rig.parent, on=False)
    hw.initialize = lambda **kw: off() if when == 'initialize' else None
    if when == 'ramp':
        original = hw.set_module_voltages
        requested = False
        def write(*args):
            nonlocal requested
            result = original(*args)
            if not requested and any(value != 0. for value in args[1].values()):
                requested = True
                off()
            return result
        hw.set_module_voltages = write
    if when == 'completion':
        end = c._end_transition
        def finish():
            off()
            end()
        c._end_transition = finish
    assert c._begin_transition(True)
    c.toggleOn()
    assert stops == [True], 'Exactly one OFF worker, including the completion race'
    assert c.main_state == ('Disconnected' if shutdown_ok else 'Shutdown unconfirmed')
    assert not c.transitioning
    assert c._setpoint_cancel.is_set()
    assert c.initialized is (not shutdown_ok)
    assert (c.device is hw) is (not shutdown_ok)
    assert ampr_ramp_rig.on.state is (not shutdown_ok)
    if when == 'initialize':
        assert not ampr_ramp_rig.writes, 'OFF before initialize returns must prevent all ramp-up commands'
    if when == 'completion':
        assert ampr_ramp_rig.clock.now == pytest.approx(40.), 'OFF must descend in parallel, even at worker completion'
    if when == 'ramp':
        nonzero = [(t, values) for t, values in ampr_ramp_rig.writes if any(values.values())]
        assert len(nonzero) == 1, 'Never finish the upward frame after OFF'


def test_click_during_ramp_down_skips_the_rest_of_the_descent(ampr_ramp_rig):
    c = ampr_ramp_rig.controller
    assert c._begin_transition(False)
    original = c.device.set_module_voltages
    def write(*args):
        result = original(*args)
        if len(ampr_ramp_rig.writes) == 3:
            ampr_ramp_rig.on.state = True  # the operator clicks the (OFF) button again
            ampr_ramp_rig.module.AMPRDevice.setOn(ampr_ramp_rig.parent)
        return result
    c.device.set_module_voltages = write
    ramp(ampr_ramp_rig, down=True)
    assert len(ampr_ramp_rig.writes) == 3, 'no further descent step after the second click'
    assert ampr_ramp_rig.clock.now < 1.
    assert not ampr_ramp_rig.on.state, 'a click during the descent must never request ON'
    assert any('skipping the remaining descent' in message for message in ampr_ramp_rig.logs)


def test_second_off_during_descent_runs_the_verified_shutdown_at_once(ampr_ramp_rig):
    c, hw = ampr_ramp_rig.controller, ampr_ramp_rig.controller.device
    stops = []
    hw.close = lambda: None
    def stop():
        stops.append(ampr_ramp_rig.clock.now)
        hw.connected = False
        hw._dll_port_claimed = False
        return True
    hw.shutdown = stop
    rates = {(2, ch.channel_number()): ch.ramp_rate_v_s for ch in ampr_ramp_rig.channels}
    c._last_output_targets = {(2, ch.channel_number()): ch.value for ch in ampr_ramp_rig.channels}
    assert c._begin_transition(False)
    c.toggleOnFromThread = lambda **kw: pytest.fail('A further OFF must not start another worker')
    original = hw.set_module_voltages
    def write(*args):
        result = original(*args)
        if len(ampr_ramp_rig.writes) == 2:
            ampr_ramp_rig.module.AMPRDevice.setOn(ampr_ramp_rig.parent, on=False)
        return result
    hw.set_module_voltages = write
    c._ramp_down_and_shutdown(rates)
    assert len(stops) == 1 and stops[0] < 1., 'shutdown must not wait for the 20 s descent'
    assert len(ampr_ramp_rig.writes) == 2
    assert c.main_state == 'Disconnected'


def test_a_new_transition_clears_the_fast_off_request(ampr_ramp_rig):
    c = ampr_ramp_rig.controller
    assert c._begin_transition(False)
    assert c._request_fast_off()
    c.transitioning = False
    assert c._begin_transition(True)
    assert not c._abort_ramp_down
    assert not c._request_fast_off(), 'only an OFF transition can be accelerated'


def test_off_request_after_old_worker_exit_starts_new_off_worker(ampr_ramp_rig):
    # The busy check and _request_off are not atomic: simulate completion in between.
    c = ampr_ramp_rig.controller
    c._begin_transition(True)
    request = c._request_off
    def finish_then_request():
        c._end_transition()
        return request()
    c._request_off = finish_then_request
    calls = []
    c.toggleOnFromThread = lambda **kw: calls.append(c.transition_target_on)
    ampr_ramp_rig.module.AMPRDevice.setOn(ampr_ramp_rig.parent, on=False)
    assert calls == [False]
    assert not ampr_ramp_rig.on.state


def test_ramp_retarget_does_not_jump_to_the_new_value(ampr_ramp_rig):
    c = ampr_ramp_rig.controller
    ch = ampr_ramp_rig.channels[0]
    ch.VALUE = 'Value'
    ch.getParameterByName = lambda name: SimpleNamespace(displayDecimals=2)
    c._begin_transition(True)
    original = c.device.set_module_voltages
    changed = False
    def write(*args):
        nonlocal changed
        result = original(*args)
        if not changed and args[1].get(1) == 1.:
            changed = True
            ch.value = 150.
            c._queue_setpoint(ch)
        return result
    c.device.set_module_voltages = write
    ramp(ampr_ramp_rig)
    curve = [(t, v[1]) for t, v in ampr_ramp_rig.writes if 1 in v]
    assert curve[-1] == pytest.approx((15., 150.))
    assert all(abs(value) <= 10. * t + 1e-8 for t, value in curve)
    assert not c._pending_setpoints
    assert c._latest_setpoints[(2, 1)].state == 'confirmed'


def test_channel_column_defaults_to_previous_global_setting():
    module = load_plugin()
    channel = module.AMPRChannel(channelParent=SimpleNamespace(ramp_rate_v_s=7.5, useMonitors=True), tree=None)
    config = channel.getDefaultChannel()[channel.RAMP_RATE]
    assert config['value'] == 7.5
    assert config['minimum'] == 0.
    assert config['attr'] == 'ramp_rate_v_s'
    channel.setDisplayedParameters()
    assert channel.RAMP_RATE in channel.displayedParameters
    assert config.get('advanced', False) is False
    default_item = {'Ramp rate': 7.5, 'Module': '0', 'CH': '1', 'Enabled': True, 'Real': True}
    saved = [{'Name': 'My channel', 'Module': '2', 'CH': '1', 'Real': True,
              'Enabled': True, 'Ramp rate': 3.5}]
    items, _ = module._plan_channel_sync(current_items=saved, detected_modules=[2],
                                         device_name='AMPR_A', default_item=default_item)
    assert items[0]['Ramp rate'] == 3.5
    assert all(item['Ramp rate'] == 7.5 for item in items[1:])
