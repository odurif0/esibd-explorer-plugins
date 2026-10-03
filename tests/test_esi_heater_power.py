"""Explorer heater commands use fake devices only."""
from threading import Event
import pytest


@pytest.fixture
def control():
    from types import SimpleNamespace
    from copy import deepcopy
    from test_esi_plugin_behavior import _load_plugin
    module = _load_plugin()
    calls = []
    heat = SimpleNamespace(is_heat_channel=lambda: True, is_current_channel=lambda: False,
                           module_address=lambda: 0, name='HEAT', enabled=True)
    parent = SimpleNamespace(poll_timeout_s=.1, interval=1000., isOn=lambda: True,
                             getChannels=lambda: [heat], heat_power_limit_w=50.)
    controller = module.ESIController(parent)
    controller.initialized = True
    controller.errorCount = 0
    controller.main_state = 'STATE_ON'
    controller.heat_readback_valid = True
    controller._sync_status = lambda: None
    controller.print = lambda *args, **kwargs: None
    config = dict(hardware_limits=dict(max_power_w=180., max_temperature_c=175.),
                  power_limit_w=20., voltage_limit_v=22., current_limit_a=10.)

    class Device:
        def get_heat_configuration(self, **kwargs):
            calls.append(('read',))
            return deepcopy(config)

        def configure_heat_limits(self, **kwargs):
            assert set(kwargs) == {'power_w', 'timeout_s', 'cancel_event'}
            assert kwargs['cancel_event'] is controller._output_cancel
            calls.append(('power', kwargs['power_w']))
            config['power_limit_w'] = kwargs['power_w'] - .0001
            return {'power_w': config['power_limit_w']}

        def set_heater_temperature(self, value, **kwargs):
            calls.append(('temperature', value))
            return value

        def set_output_active(self, address, active, **kwargs):
            calls.append(('enable', active))
            return active

    controller.device = Device()
    return SimpleNamespace(module=module, controller=controller, parent=parent, heat=heat,
                           calls=calls, config=config)


def test_power_edit_only_sets_power_and_independently_verifies(control):
    c = control.controller
    c._apply_heat_power(50., c._output_cancel)
    assert control.calls == [('read',), ('power', 50.), ('read',)]
    assert c.heat_power_limit_w == 49.9999 and c.heat_power_error == ''
    # A later temperature command does not need to rewrite an already verified ceiling.
    control.calls.clear()
    c._apply_heat_power(50., c._output_cancel)
    assert control.calls == [('read',)]


def test_temperature_edit_never_enables(control):
    c = control.controller
    c._apply_heat_temperature(80., c._output_cancel)
    assert control.calls == [('temperature', 80.)]


@pytest.mark.parametrize('kind', ['power', 'temperature'])
def test_queued_edit_does_not_survive_stop_and_new_on(control, monkeypatch, kind):
    queued = []
    class Worker:
        def __init__(self, *, target, args, **kwargs):
            queued.append((target, args))
        def start(self):
            pass
    monkeypatch.setattr(control.module, 'Thread', Worker)
    c = control.controller
    getattr(c, 'applyHeatPowerFromThread' if kind == 'power' else 'applyHeatTemperatureFromThread')(50.)
    c._output_cancel.set()
    c._output_cancel = Event()
    target, args = queued.pop()
    target(*args)
    assert not control.calls


@pytest.mark.parametrize('kind', ['readback', 'voltage', 'current', 'max', 'echo'])
def test_power_discrepancy_is_visible_and_never_enables(control, monkeypatch, kind):
    original = control.controller.device.configure_heat_limits
    def corrupt(**kwargs):
        answer = original(**kwargs)
        if kind == 'readback':
            control.config['power_limit_w'] = 45.
        elif kind in ('voltage', 'current'):
            control.config[kind + '_limit_' + ('v' if kind == 'voltage' else 'a')] = 1.
        elif kind == 'max':
            control.config['hardware_limits']['max_power_w'] = 25.
        else:
            answer['power_w'] = 100.
        return answer
    monkeypatch.setattr(control.controller.device, 'configure_heat_limits', corrupt)
    c = control.controller
    c._apply_heat_power(50., c._output_cancel)
    assert 'unconfirmed' in c.heat_power_error
    assert ('enable', True) not in control.calls
    assert ('enable', False) in control.calls


@pytest.mark.parametrize('value', [float('nan'), float('inf'), -1., 181.])
def test_invalid_power_never_reaches_setter(control, value):
    c = control.controller
    c._apply_heat_power(value, c._output_cancel)
    assert not any(call[0] == 'power' for call in control.calls)
    assert c.heat_power_error


def test_legacy_zero_keeps_existing_device_limit(control):
    c = control.controller
    c._apply_heat_power(0., c._output_cancel)
    assert control.calls == [('read',)]
    assert c.heat_power_limit_w == 20.


def test_power_default_is_fifty_and_uses_existing_setting_event(control):
    m = control.module
    device = m.ESIDevice()
    setting = device.getDefaultSettings()[f'{device.name}/{device.HEAT_POWER_LIMIT}']
    assert setting['value'] == 50.
    assert setting['event'] == device._heat_power_limit_changed


@pytest.mark.parametrize('when', ['initial_read', 'setter_return', 'verification_read'])
def test_stop_during_power_edit_never_confirms_or_enables(control, monkeypatch, when):
    c = control.controller
    read = c.device.get_heat_configuration
    write = c.device.configure_heat_limits
    count = [0]
    def getter(**kwargs):
        result = read(**kwargs)
        count[0] += 1
        if (count[0] == 1 and when == 'initial_read') or (count[0] == 2 and when == 'verification_read'):
            c._output_cancel.set()
        return result
    def setter(**kwargs):
        result = write(**kwargs)
        if when == 'setter_return':
            c._output_cancel.set()
        return result
    monkeypatch.setattr(c.device, 'get_heat_configuration', getter)
    monkeypatch.setattr(c.device, 'configure_heat_limits', setter)
    c._apply_heat_power(50., c._output_cancel)
    assert 'unconfirmed' in c.heat_power_error
    assert ('enable', True) not in control.calls
    if when == 'initial_read':
        assert not any(call[0] == 'power' for call in control.calls)
    if when == 'setter_return':
        assert count[0] == 1


@pytest.mark.parametrize('state', ['Shutdown unconfirmed', 'Stopping', 'Connecting', 'Communication lost'])
@pytest.mark.parametrize('kind', ['power', 'temperature'])
def test_live_controls_refuse_unknown_or_transition_states(control, state, kind):
    c = control.controller
    c.main_state = state
    getattr(c, '_apply_heat_' + kind)(50., c._output_cancel)
    assert not control.calls


def test_on_checks_temperature_before_changing_live_power(control):
    c = control.controller
    with pytest.raises(ValueError, match='Temperature'):
        c._configure_heat_power_unlocked(50., c._output_cancel, target=176.)
    assert control.calls == [('read',)]


def test_channel_temperature_edit_uses_non_enabling_path(control):
    channel = control.module.ESIChannel.__new__(control.module.ESIChannel)
    channel.module, channel.real, channel.enabled = 0, True, True
    channel.value, channel.lastAppliedValue = 80., 30.
    channel.channelParent = control.parent
    control.parent.controller = control.controller
    control.controller.applyHeatTemperatureFromThread = lambda value: control.calls.append(('edit', value))
    channel.applyValue()
    assert control.calls == [('edit', 80.)]


def test_reset_drops_previous_power_limits_and_verification(control):
    import math
    c = control.controller
    c.heat_power_limit_w, c.heat_max_power_w, c.heat_max_temperature_c = 50., 180., 175.
    c.heat_limits_observed_at = 10.
    c._heat_power_command = (50., 50.)
    c.initializeValues(reset=True)
    assert all(math.isnan(x) for x in (c.heat_power_limit_w, c.heat_max_power_w, c.heat_max_temperature_c))
    assert c.heat_limits_observed_at == float('-inf')
    assert c._heat_power_command is None


@pytest.mark.parametrize('kind,last', [('power', 50.), ('power', 0.), ('temperature', 50.)])
def test_latest_queued_edit_wins_even_when_workers_are_reversed(control, monkeypatch, kind, last):
    queued = []
    class Worker:
        def __init__(self, *, target, args, **kwargs):
            queued.append((target, args))
        def start(self):
            pass
    monkeypatch.setattr(control.module, 'Thread', Worker)
    c = control.controller
    dispatch = getattr(c, 'applyHeatPowerFromThread' if kind == 'power' else 'applyHeatTemperatureFromThread')
    dispatch(90.)
    dispatch(last)
    for target, args in reversed(queued):
        target(*args)
    writes = [call for call in control.calls if call[0] == kind]
    assert writes == ([] if last == 0. else [(kind, last)])


@pytest.mark.parametrize('kind', ['power', 'temperature'])
@pytest.mark.parametrize('failed', [False, True])
def test_superseded_completion_does_not_clear_newer_error(control, monkeypatch, kind, failed):
    c = control.controller
    request = object()
    setattr(c, '_heat_' + kind + '_request', request)
    method = 'configure_heat_limits' if kind == 'power' else 'set_heater_temperature'
    native = getattr(c.device, method)
    logged = []
    c.print = lambda text, **kwargs: logged.append(text)
    def complete(*args, **kwargs):
        result = native(*args, **kwargs)
        setattr(c, '_heat_' + kind + '_request', object())
        setattr(c, 'heat_' + kind + '_error', 'Newer request error')
        if failed:
            raise RuntimeError('Native failure')
        return result
    monkeypatch.setattr(c.device, method, complete)
    getattr(c, '_apply_heat_' + kind)(90., c._output_cancel, request)
    assert getattr(c, 'heat_' + kind + '_error') == 'Newer request error'
    if failed:
        assert any('Native failure' in text for text in logged)
        assert ('enable', False) in control.calls
    else:
        assert not any(call[0] == 'enable' for call in control.calls)


@pytest.mark.parametrize('capture', ['before', 'during'])
def test_old_snapshot_cannot_replace_verified_power_or_refresh_its_age(control, monkeypatch, capture):
    import math
    from test_esi_heater_stability import snapshot
    c = control.controller
    data = snapshot()
    data['heat']['hardware_limits']['max_power_w'] = 180.
    data['heat']['power_limit_w'] = 20.
    c._apply_snapshot(data, observed_at=5.)
    generation = [c._heat_stability_generation]
    read = c.device.get_heat_configuration
    def getter(**kwargs):
        if capture == 'during':
            generation[0] = c._heat_stability_generation
        return read(**kwargs)
    monkeypatch.setattr(c.device, 'get_heat_configuration', getter)
    c._apply_heat_power(50., c._output_cancel)
    verified = c.heat_power_limit_w
    assert verified > 49.
    c._apply_snapshot(data, observed_at=99., stability_generation=generation[0])
    assert c.heat_power_limit_w == verified
    assert c.heat_limits_observed_at == 5.
    assert math.isnan(c.values[0]) and math.isnan(c.currents[0])
    data['heat']['power_limit_w'] = verified
    c._apply_snapshot(data, observed_at=100., stability_generation=c._heat_stability_generation)
    assert c.heat_limits_observed_at == 100.
    assert c.values[0] == 50.


@pytest.mark.parametrize('kind', ['power', 'temperature'])
@pytest.mark.parametrize('fallback_fails', [False, True])
def test_failed_edit_invalidates_on_and_reports_fallback(control, monkeypatch, kind, fallback_fails):
    from test_esi_heater_stability import snapshot
    c = control.controller
    c._apply_snapshot(snapshot(), observed_at=5.)
    generation = c._heat_stability_generation
    def failure(*args, **kwargs):
        raise RuntimeError('write failed')
    monkeypatch.setattr(c.device, 'configure_heat_limits' if kind == 'power' else 'set_heater_temperature', failure)
    if fallback_fails:
        def failed_off(*args, **kwargs):
            raise RuntimeError('OFF failed')
        monkeypatch.setattr(c.device, 'set_output_active', failed_off)
    getattr(c, '_apply_heat_' + kind)(50., c._output_cancel)
    assert not c.heat_activation
    assert c.heat_limits_observed_at == float('-inf')
    assert not c.heat_readback_valid
    error = getattr(c, 'heat_' + kind + '_error')
    assert ('OFF failed' if fallback_fails else 'forced OFF') in error
    c._apply_snapshot(snapshot(), observed_at=99., stability_generation=generation)
    assert not c.heat_activation
    assert c.heat_limits_observed_at == float('-inf')


def test_sensor_invalidation_during_configuration_read_blocks_power_setter(control, monkeypatch):
    c = control.controller
    read = c.device.get_heat_configuration
    def getter(**kwargs):
        result = read(**kwargs)
        c.heat_readback_valid = False
        return result
    monkeypatch.setattr(c.device, 'get_heat_configuration', getter)
    c._apply_heat_power(50., c._output_cancel)
    assert not any(call[0] == 'power' for call in control.calls)
    assert 'readback is invalid' in c.heat_power_error


def test_invalid_sensor_blocks_live_power_change(control):
    c = control.controller
    c.heat_readback_valid = False
    c._apply_heat_power(50., c._output_cancel)
    assert not any(call[0] == 'power' for call in control.calls)
    assert c.heat_power_error
