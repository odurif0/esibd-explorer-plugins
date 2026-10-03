"""OFF/Disconnect/Explorer close must retire an initial open, not shut down acquisition."""
from __future__ import annotations

from types import MethodType, SimpleNamespace
import importlib
import threading

import pytest

from test_dmmr_connection_retry import connection, finish_open  # noqa: F401


@pytest.fixture
def failed_open(connection, monkeypatch):
    hw = connection
    p, c, mod = hw.controller.controllerParent, hw.controller, hw.plugin
    p.controller = c
    p.useOnOffLogic, p.loading = True, False
    p.onAction = SimpleNamespace(state=False)
    p.isOn = lambda: p.onAction.state
    p._set_on_ui_state = lambda on: setattr(p.onAction, 'state', on)
    p.stopAcquisition, p.print = c.stopAcquisition, c.print
    for name in ('_sync_acquisition_controls', '_sync_local_on_action',
                 '_sync_toolbar_communication_controls', '_update_status_widgets'):
        setattr(p, name, lambda: None)
    for name in ('closeCommunication', 'shutdownCommunication', 'setOn'):
        setattr(p, name, MethodType(getattr(mod.DMMRDevice, name), p))
    for name in ('_restore_off_ui_state', '_restore_on_ui_state', '_update_state'):
        setattr(c, name, MethodType(getattr(mod.DMMRController, name), c))
    c.toggleOnFromThread = lambda **kwargs: c.toggleOn()
    hw.runtime_calls = []
    factory = hw.factory

    def tracked_factory(**kwargs):
        d = factory(**kwargs)
        for name in ('shutdown', 'get_state', 'get_device_state', 'get_voltage_state',
                     'get_temperature_state', 'set_enable', 'set_automatic_current'):
            original = getattr(d, name)

            def tracked(*args, _method=original, _name=name, **kw):
                hw.runtime_calls.append(_name)
                return _method(*args, **kw)

            setattr(d, name, tracked)
        return d

    monkeypatch.setattr(mod, '_get_dmmr_driver_class', lambda: tracked_factory)
    return hw


ACTIONS = ('controller_close', 'device_close', 'controller_shutdown',
           'device_shutdown', 'controller_off', 'device_off')


def request(hw, action):
    c, p = hw.controller, hw.controller.controllerParent
    if action.endswith('_off'):
        p.onAction.state = False
        if action == 'device_off':
            return p.setOn(on=False)
        assert c._begin_transition(False)
        return c.toggleOn()
    owner = p if action.startswith('device_') else c
    return getattr(owner, 'shutdownCommunication' if action.endswith('shutdown') else 'closeCommunication')()


def assert_pending(hw, old):
    c, p = hw.controller, hw.controller.controllerParent
    assert c.device is old and old._dll_port_claimed
    assert c.main_state == 'Connection pending'
    assert not c.initialized and not c.acquiring and not c.transitioning
    assert not p.isOn(), 'Pending initial connection is OFF; another ON must clean up before reconnecting'
    assert c._forced_close_state is None
    assert hw.runtime_calls == [], 'Initial-open cleanup must not query/disable an acquisition'


def assert_released(hw, old):
    c, p = hw.controller, hw.controller.controllerParent
    assert c.device is None and not old._dll_port_claimed
    assert c.main_state == 'Disconnected'
    assert not c.initialized and not c.acquiring and not c.transitioning
    assert not p.isOn()
    assert c._forced_close_state is None
    assert hw.runtime_calls == []
    assert hw.calls == ['open', 'close']


@pytest.mark.parametrize('action', ACTIONS)
@pytest.mark.parametrize('phase', ['pending_open', 'returned_open', 'close_error', 'pending_close'])
def test_failed_initial_open_shutdown_paths(failed_open, action, phase):
    hw, c = failed_open, failed_open.controller
    c.runInitialization()
    old = c.device
    if phase != 'pending_open':
        finish_open(hw)
    if phase == 'close_error':
        hw.close_status = -3
    if phase == 'pending_close':
        hw.block_close = True
    result = request(hw, action)
    if phase == 'returned_open':
        assert_released(hw, old)
        if action.endswith('shutdown') and action.startswith('controller'):
            assert result is True
        return
    assert_pending(hw, old)
    assert hw.calls == (['open'] if phase == 'pending_open' else ['open', 'close'])
    request(hw, action)
    assert_pending(hw, old)
    assert hw.calls == (['open'] if phase == 'pending_open' else ['open', 'close'])
    if phase == 'close_error':
        return  # A returned failed native Close cannot be replayed or called a success.
    if phase == 'pending_open':
        finish_open(hw)
    else:
        worker = old._pending_dll_call['thread']
        hw.close_release.set()
        worker.join(3)
        assert not worker.is_alive()
    request(hw, action)
    assert_released(hw, old)
    assert old._transport_poisoned, 'Retire the timed-out instance, never rearm it'


@pytest.mark.parametrize('action', ACTIONS)
def test_initial_open_cleanup_precedes_stale_shutdown_guard(failed_open, action):
    hw, c = failed_open, failed_open.controller
    c.runInitialization()
    old = c.device
    c.main_state = c._forced_close_state = 'Shutdown unconfirmed'
    c.initialized = True
    finish_open(hw)
    request(hw, action)
    assert_released(hw, old)


def test_unpoisoned_failed_open_never_queries_state_during_shutdown(failed_open):
    hw, c = failed_open, failed_open.controller
    hw.block, hw.close_status = False, -3
    c.runInitialization()
    old = c.device
    assert old._open_failed and not old._transport_poisoned
    request(hw, 'device_shutdown')
    assert_pending(hw, old)
    assert hw.calls == ['open', 'close', 'close']  # Explicit retry on a non-poisoned backend.


@pytest.mark.parametrize('action', ['device_close', 'device_shutdown'])
def test_close_while_open_still_running_then_error_return(failed_open, action):
    hw, c = failed_open, failed_open.controller
    p = c.controllerParent
    p.connect_timeout_s = 0.5
    c.initializing = True
    worker = threading.Thread(target=c.runInitialization)
    worker.start()
    try:
        assert hw.entered.wait(2)
        old = c.device
        request(hw, action)
        assert_pending(hw, old)
        assert hw.calls == ['open']
        hw.late_status = -5
        finish_open(hw)
        worker.join(3)
        assert not worker.is_alive()
        assert_released(hw, old)
        assert hw.successes == []
    finally:
        hw.release.set()
        worker.join(3)


@pytest.mark.parametrize('action', ['device_close', 'device_shutdown', 'device_off'])
@pytest.mark.parametrize('verified_stop', [True, False])
def test_close_during_open_cancels_later_activation(failed_open, action, verified_stop):
    hw, c = failed_open, failed_open.controller
    c.controllerParent.connect_timeout_s = 0.5
    c.initializing = True
    worker = threading.Thread(target=c.runInitialization)
    worker.start()
    try:
        assert hw.entered.wait(2)
        old = c.device
        gates = []
        def disable(name, value, **kwargs):
            assert value is False, 'An earlier OFF must prevent later activation'
            gates.append(('write', name, value))
            return 0
        def get_gate(name, **kwargs):
            gates.append(('read', name))
            return 0, not verified_stop
        old.set_enable = lambda value, **kw: disable('enable', value, **kw)
        old.set_automatic_current = lambda value, **kw: disable('automatic', value, **kw)
        old.get_enable = lambda **kw: get_gate('enable', **kw)
        old.get_automatic_current = lambda **kw: get_gate('automatic', **kw)
        request(hw, action)
        assert_pending(hw, old)
        assert gates == []
        finish_open(hw)  # Open succeeds before its timeout, unlike the late-return cases.
        worker.join(3)
        assert not worker.is_alive()
        assert hw.successes == [] and not c.acquiring
        assert gates == [('write', 'automatic', False), ('write', 'enable', False),
                         ('read', 'automatic'), ('read', 'enable')]
        if verified_stop:
            assert c.device is None and not c.initialized
            assert c.main_state == 'Disconnected' and not c.controllerParent.isOn()
            assert not old._dll_port_claimed and hw.calls == ['open', 'close']
        else:
            assert c.device is old and c.initialized
            assert c.main_state == 'Shutdown unconfirmed' and c.controllerParent.isOn()
            assert old._dll_port_claimed and hw.calls == ['open']
    finally:
        hw.release.set()
        worker.join(3)


def test_close_during_connect_verification_sends_no_parallel_gate_commands(failed_open, monkeypatch):
    hw, c = failed_open, failed_open.controller
    hw.block, hw.powered = False, True
    c.controllerParent.connect_timeout_s = 1.0
    c.initializing = True
    entered, release = threading.Event(), threading.Event()
    def set_baud(device, baud):
        entered.set()
        assert release.wait(3)
        return 0, baud
    cls = hw.plugin._DMMR_DRIVER_CLASS._PROCESS_CONTROLLER_CLASS
    monkeypatch.setattr(importlib.import_module(cls.__module__).DMMRBase, 'set_baud_rate', set_baud)
    worker = threading.Thread(target=c.runInitialization)
    worker.start()
    gates = []
    try:
        assert entered.wait(2)
        old = c.device
        assert old.connected and old._opening_in_progress
        def disable(value, **kwargs):
            gates.append(('disable', value))
            return 0
        def get_gate(**kwargs):
            gates.append(('read',))
            return 0, False
        old.set_enable = old.set_automatic_current = disable
        old.get_enable = old.get_automatic_current = get_gate
        request(hw, 'device_close')
        assert hw.calls == ['open'] and gates == []
        assert_pending(hw, old)
        release.set()
        worker.join(3)
        assert not worker.is_alive()
        assert gates == [('disable', False), ('disable', False), ('read',), ('read',)]
        assert hw.calls == ['open', 'close'] and hw.successes == []
        assert c.device is None and c.main_state == 'Disconnected'
        assert not c.controllerParent.isOn()
    finally:
        release.set()
        worker.join(3)


def test_late_init_complete_cannot_activate_after_initial_close_request(failed_open):
    hw, c = failed_open, failed_open.controller
    c.runInitialization()
    old = c.device
    request(hw, 'device_close')
    c.initComplete()  # An already queued Qt callback must respect the close request too.
    assert_pending(hw, old)
    assert hw.calls == ['open'] and hw.successes == []


def test_open_finishing_during_close_cannot_become_unverified_bare_disconnect(failed_open, monkeypatch):
    hw, c = failed_open, failed_open.controller
    c.controllerParent.connect_timeout_s = 1.0
    c.initializing = True
    connected, proceed = threading.Event(), threading.Event()
    factory = hw.plugin._get_dmmr_driver_class()
    gates = []
    def driver_factory(**kw):
        d = factory(**kw)
        initialize = d.initialize
        def delayed_initialize(**kwargs):
            result = initialize(**kwargs)
            connected.set()
            assert proceed.wait(3)
            return result
        d.initialize = delayed_initialize
        def disable(name, value, **kwargs):
            assert value is False
            gates.append(('write', name))
            return 0
        def get_gate(name, **kwargs):
            gates.append(('read', name))
            return 0, False
        d.set_enable = lambda value, **kw: disable('enable', value, **kw)
        d.set_automatic_current = lambda value, **kw: disable('automatic', value, **kw)
        d.get_enable = lambda **kw: get_gate('enable', **kw)
        d.get_automatic_current = lambda **kw: get_gate('automatic', **kw)
        return d
    monkeypatch.setattr(hw.plugin, '_get_dmmr_driver_class', lambda: driver_factory)
    worker = threading.Thread(target=c.runInitialization)
    worker.start()
    try:
        assert hw.entered.wait(2)
        old = c.device
        dispose = c._dispose_device
        def open_finishes_now(*, initial_open_only=False):
            if initial_open_only:
                finish_open(hw)
                assert connected.wait(2)
                assert not old._opening_in_progress and not old._open_failed
            return dispose(initial_open_only=initial_open_only)
        monkeypatch.setattr(c, '_dispose_device', open_finishes_now)
        request(hw, 'device_close')
        assert hw.calls == ['open'], 'Do not turn initial cleanup into a bare Close after connect succeeds'
        assert gates == []
        assert c.device is old and old._dll_port_claimed
        assert c.main_state == 'Shutdown unconfirmed', 'A completed connection needs its normal stop'
        proceed.set()
        worker.join(3)
        assert not worker.is_alive()
        assert gates == [('write', 'automatic'), ('write', 'enable'), ('read', 'automatic'), ('read', 'enable')]
        assert c.device is None and c.main_state == 'Disconnected'
        assert not c.controllerParent.isOn() and not c.acquiring
        assert hw.calls == ['open', 'close'] and hw.successes == []
    finally:
        hw.release.set()
        proceed.set()
        worker.join(3)


@pytest.mark.parametrize('action', ['controller_close', 'device_close', 'device_shutdown', 'device_off'])
@pytest.mark.parametrize('operation', ['get_module_current', 'set_enable', 'set_baud_rate'])
def test_post_open_timeout_still_forbids_initial_open_cleanup(failed_open, action, operation):
    hw, c = failed_open, failed_open.controller
    d = hw.factory()
    d.connected = operation != 'set_baud_rate'
    d._set_port_claimed(True)
    def read():
        hw.worker = threading.current_thread()
        hw.entered.set()
        assert hw.release.wait(5)
        return 0
    with pytest.raises(RuntimeError, match='timed out'):
        d._call_locked_with_timeout(read, 0.03, operation)
    finish_open(hw)
    c.device, c.initialized = d, True
    c.main_state = c._forced_close_state = 'Shutdown unconfirmed'
    request(hw, action)
    assert c.device is d and d._dll_port_claimed
    assert c.initialized and c.main_state == 'Shutdown unconfirmed'
    assert c.controllerParent.isOn()
    assert hw.calls == [], 'No Close after a post-open timeout, even after worker return'
