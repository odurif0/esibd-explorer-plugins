"""A completed Open is not a verified shutdown, even before initComplete."""
from __future__ import annotations

import threading

import pytest

from test_dmmr_connection_retry import connection, finish_open  # noqa: F401
from test_dmmr_failed_open_shutdown import failed_open, request  # noqa: F401


@pytest.mark.parametrize('window', ['before_close_classification', 'after_connect'])
@pytest.mark.parametrize('verified_stop', [True, False])
def test_close_keeps_connected_backend_until_initializer_verifies_stop(
    failed_open, monkeypatch, window, verified_stop,
):
    hw, c = failed_open, failed_open.controller
    c.controllerParent.connect_timeout_s = 1.0
    c.initializing = True
    connected, proceed = threading.Event(), threading.Event()
    factory = hw.plugin._get_dmmr_driver_class()
    gates = []

    def driver_factory(**kwargs):
        device = factory(**kwargs)
        initialize = device.initialize

        def delayed_initialize(**kw):
            result = initialize(**kw)  # The real connect has returned successfully.
            connected.set()
            assert proceed.wait(3)
            return result

        def disable(name, value, **kw):
            assert value is False, 'An OFF/close request must never activate a gate'
            gates.append(('write', name))
            # An erroneous best-effort disable before the owning initializer
            # resumes is deliberately rejected, as in the original RO probe.
            return 0 if proceed.is_set() else -13

        def get_gate(name, **kw):
            gates.append(('read', name))
            return 0, not verified_stop

        device.initialize = delayed_initialize
        device.set_enable = lambda value, **kw: disable('enable', value, **kw)
        device.set_automatic_current = lambda value, **kw: disable('automatic', value, **kw)
        device.get_enable = lambda **kw: get_gate('enable', **kw)
        device.get_automatic_current = lambda **kw: get_gate('automatic', **kw)
        return device

    monkeypatch.setattr(hw.plugin, '_get_dmmr_driver_class', lambda: driver_factory)
    worker = threading.Thread(target=c.runInitialization)
    worker.start()
    original_close = c.closeCommunication
    try:
        assert hw.entered.wait(2)
        old = c.device

        def finish_connect():
            finish_open(hw)
            assert connected.wait(2)
            assert old.connected and not old._opening_in_progress and not old._open_failed
            assert c.initializing

        if window == 'before_close_classification':
            def close_after_connect(*args, **kwargs):
                finish_connect()  # After shutdown's test, before Close's first test.
                return original_close(*args, **kwargs)
            monkeypatch.setattr(c, 'closeCommunication', close_after_connect)
            request(hw, 'device_shutdown')
        else:
            finish_connect()
            request(hw, 'device_close')

        assert hw.calls == ['open'], 'Close must wait for verified OFF, not just successful Open'
        assert gates == [], 'The initializer still owns shutdown; no best-effort gate command'
        assert c.device is old and old._dll_port_claimed
        assert c.main_state == 'Shutdown unconfirmed' and c.controllerParent.isOn()
        assert c._initial_open_close_requested
        assert hw.successes == [] and not c.acquiring
        monkeypatch.setattr(c, 'closeCommunication', original_close)
        proceed.set()
        worker.join(3)
        assert not worker.is_alive()
        assert gates == [('write', 'automatic'), ('write', 'enable'),
                         ('read', 'automatic'), ('read', 'enable')]
        assert hw.successes == [] and not c.acquiring
        if verified_stop:
            assert c.device is None and not old._dll_port_claimed
            assert hw.calls == ['open', 'close']
            assert c.main_state == 'Disconnected' and not c.initialized
            assert not c.controllerParent.isOn()
        else:
            assert c.device is old and old._dll_port_claimed
            assert hw.calls == ['open']
            assert c.main_state == 'Shutdown unconfirmed' and c.initialized
            assert c.controllerParent.isOn()
    finally:
        monkeypatch.setattr(c, 'closeCommunication', original_close)
        hw.release.set()
        proceed.set()
        worker.join(3)
        assert not worker.is_alive()


@pytest.mark.parametrize('state', ['Disconnected', 'Connecting', 'ST_ON', 'OFF'])
@pytest.mark.parametrize('initialized', [True, False])
def test_plain_close_cannot_infer_verified_stop_from_display_or_completed_open(
    failed_open, monkeypatch, state, initialized,
):
    hw, c = failed_open, failed_open.controller
    hw.block, hw.powered = False, True
    old = hw.factory()
    assert old.connect(timeout_s=1.0)
    c.device, c.initialized, c.main_state = old, initialized, state
    c.initializing = False  # initComplete may still be queued, despite worker completion.
    c._initial_open_close_requested = False
    gate_commands = []
    old.set_enable = lambda *a, **kw: gate_commands.append(a) or -13
    base_closes = []
    monkeypatch.setattr(hw.plugin.DeviceController, 'closeCommunication',
                        lambda self: base_closes.append(True), raising=False)

    c.closeCommunication()

    assert hw.calls == ['open'] and not gate_commands and not base_closes
    assert c.device is old and old._dll_port_claimed
    assert c.main_state == 'Shutdown unconfirmed' and c.initialized
    assert c.controllerParent.isOn() and not c.acquiring
    assert c._initial_open_close_requested
    c.initComplete()  # A queued success may not re-enable acquisition after Close.
    assert c.device is old and hw.calls == ['open'] and hw.successes == []
    assert not c.acquiring


@pytest.mark.parametrize('initialized', [True, False])
def test_unconfirmed_stop_takes_priority_over_stale_forced_transport_state(
    failed_open, monkeypatch, initialized,
):
    failed_open.controller._forced_close_state = 'Communication lost'
    test_plain_close_cannot_infer_verified_stop_from_display_or_completed_open(
        failed_open, monkeypatch, 'Shutdown unconfirmed', initialized,
    )
