"""Parallel per-channel ramps, cancellable ON and preserved channel configuration."""
from types import SimpleNamespace
import threading

import pytest

from test_ampr_plugin_channel_sync import _load_module


@pytest.fixture(params=['ampr_a', 'ampr_b'])
def rig(request, monkeypatch):
    import test_ampr_plugin_channel_sync as sync
    monkeypatch.setattr(sync, 'PLUGIN_PATH', sync.PLUGIN_PATH.parent.parent / request.param / 'ampr_plugin.py')
    module = _load_module()
    clock = SimpleNamespace(now=0.)
    def sleep(seconds):
        clock.now += seconds
    # Do not patch the shared time module used by threading/pytest.
    module.time = SimpleNamespace(monotonic=lambda: clock.now, sleep=sleep)
    channels = [SimpleNamespace(real=True, enabled=True, value=target, ramp_rate_v_s=rate,
                    module_address=lambda: 2, channel_number=lambda n=n: n)
                for n, target, rate in [(1, 100., 10.), (2, -200., 10.), (3, 50., 5.)]]
    on = SimpleNamespace(state=True)
    parent = SimpleNamespace(name='AMPR_A', isOn=lambda: on.state, onAction=on,
                             getChannels=lambda: channels, connect_timeout_s=5., ramp_rate_v_s=10.,
                             _sync_local_on_action=lambda: None, initialized=True, loading=False)
    controller = module.AMPRController(parent)
    parent.controller = controller
    controller.initialized = True
    controller.errorCount = 0
    controller.lock = threading.Lock()
    controller._sync_status_to_gui = lambda: None
    logs, writes = [], []
    controller.print = parent.print = lambda message, **kwargs: logs.append(message)
    class Device:
        NO_ERR = 0
        connected = True
        def set_module_voltages(self, module_id, values):
            writes.append((clock.now, dict(values)))
            return dict.fromkeys(values, 0)
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


@pytest.mark.parametrize('down', [False, True])
def test_every_channel_moves_from_the_first_tick_at_its_own_rate(rig, down):
    ramp(rig, down=down)
    by_channel = {i: [(t, v[i]) for t, v in rig.writes if i in v] for i in (1, 2, 3)}
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
    assert rig.clock.now == pytest.approx(20.), 'The channel durations must not be added'


def test_slow_serial_calls_count_towards_elapsed_ramp_time(rig):
    original = rig.controller.device.set_module_voltages
    def slow(*args):
        result = original(*args)
        rig.clock.now += .03
        return result
    rig.controller.device.set_module_voltages = slow
    ramp(rig)
    assert 20. <= rig.clock.now < 20.5
    for t, values in rig.writes:
        for number, value in values.items():
            assert abs(value) <= rig.channels[number - 1].ramp_rate_v_s * t + 1e-8


def test_zero_rate_is_immediate_for_only_that_channel(rig):
    ramp(rig, rates={(2, 1): 0., (2, 2): 10., (2, 3): 5.})
    first = next((t, v[1]) for t, v in rig.writes if 1 in v)
    assert first == (0., 100.)
    assert rig.clock.now == pytest.approx(20.)


@pytest.mark.parametrize('bad', [-1., float('nan'), float('inf')])
def test_invalid_rate_is_rejected_without_voltage_commands(rig, bad):
    with pytest.raises(ValueError, match='ramp rate'):
        ramp(rig, rates={(2, 1): bad, (2, 2): 10., (2, 3): 5.})
    assert not rig.writes


def test_off_is_not_ignored_while_on_transition_owns_the_worker(rig):
    assert rig.controller._begin_transition(True)
    rig.controller.toggleOnFromThread = lambda **kw: pytest.fail('Do not start a concurrent OFF worker')
    rig.module.AMPRDevice.setOn(rig.parent, on=False)
    assert not rig.on.state
    assert rig.controller._cancel_ramp
    assert rig.controller.transition_target_on is False
    assert rig.controller._setpoint_cancel.is_set()


def test_cancelled_ramp_must_not_reset_the_cancellation(rig):
    rig.controller._begin_transition(True)
    rig.module.AMPRDevice.setOn(rig.parent, on=False)
    with pytest.raises(rig.module._AMPRRampCancelled):
        ramp(rig)
    assert not rig.writes


def test_off_during_first_write_prevents_the_next_channels_from_rising(rig):
    rig.controller._begin_transition(True)
    original = rig.controller.device.set_module_voltages
    def off(*args):
        result = original(*args)
        rig.module.AMPRDevice.setOn(rig.parent, on=False)
        return result
    rig.controller.device.set_module_voltages = off
    with pytest.raises(rig.module._AMPRRampCancelled):
        ramp(rig)
    assert len(rig.writes) == 1
    assert len(rig.writes[0][1]) == 1


@pytest.mark.parametrize('when', ['initialize', 'ramp', 'completion'])
@pytest.mark.parametrize('shutdown_ok', [True, False])
def test_one_worker_handles_off_from_startup_through_ramp_completion(rig, when, shutdown_ok):
    c = rig.controller
    hw = c.device
    stops = []
    rig.module.DeviceController.toggleOn = lambda self: None
    c._refresh_module_scan = lambda: None
    c._apply_module_voltage_limits = lambda: None
    c._update_state = lambda: setattr(c, 'main_state', 'ST_ON')
    c.startAcquisition = lambda: pytest.fail('Acquisition must not start after OFF')
    hw.close = lambda: None
    def stop():
        stops.append(True)
        if shutdown_ok:
            hw.connected = False
        return shutdown_ok
    hw.shutdown = stop
    def off():
        rig.module.AMPRDevice.setOn(rig.parent, on=False)
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
    assert rig.on.state is (not shutdown_ok)
    if when == 'initialize':
        assert not rig.writes, 'OFF before initialize returns must prevent all ramp-up commands'
    if when == 'completion':
        assert rig.clock.now == pytest.approx(40.), 'OFF must descend in parallel, even at worker completion'
    if when == 'ramp':
        nonzero = [(t, values) for t, values in rig.writes if any(values.values())]
        assert len(nonzero) == 1, 'Never finish the upward frame after OFF'


def test_off_request_after_old_worker_exit_starts_new_off_worker(rig):
    # The busy check and _request_off are not atomic: simulate completion in between.
    c = rig.controller
    c._begin_transition(True)
    request = c._request_off
    def finish_then_request():
        c._end_transition()
        return request()
    c._request_off = finish_then_request
    calls = []
    c.toggleOnFromThread = lambda **kw: calls.append(c.transition_target_on)
    rig.module.AMPRDevice.setOn(rig.parent, on=False)
    assert calls == [False]
    assert not rig.on.state


def test_ramp_retarget_does_not_jump_to_the_new_value(rig):
    c = rig.controller
    ch = rig.channels[0]
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
    ramp(rig)
    curve = [(t, v[1]) for t, v in rig.writes if 1 in v]
    assert curve[-1] == pytest.approx((15., 150.))
    assert all(abs(value) <= 10. * t + 1e-8 for t, value in curve)
    assert not c._pending_setpoints
    assert c._latest_setpoints[(2, 1)].state == 'sent'


def test_channel_column_defaults_to_previous_global_setting():
    from test_ampr_plugin_packaging import _clear_test_modules, _install_esibd_stubs, _import_plugin_module
    _clear_test_modules()
    _install_esibd_stubs()
    module = _import_plugin_module()
    channel = module.AMPRChannel(channelParent=SimpleNamespace(ramp_rate_v_s=7.5), tree=None)
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
