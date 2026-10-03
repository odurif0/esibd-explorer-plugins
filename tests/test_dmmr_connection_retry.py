"""Failed native opens must be retained until their serial handle is released."""
import importlib
import logging
import threading
import types

import pytest

from test_dmmr_plugin_behavior import _load_module


@pytest.fixture
def connection(monkeypatch, tmp_path):
    plugin = _load_module()
    cls = plugin._get_dmmr_driver_class()._PROCESS_CONTROLLER_CLASS
    base = importlib.import_module(cls.__module__).DMMRBase
    hw = types.SimpleNamespace(
        calls=[], opened=False, powered=False, release=threading.Event(),
        entered=threading.Event(), worker=None, late_status=0, block=True,
        close_status=0, close_release=threading.Event(), block_close=False,
        drivers=[],
    )

    def native_open(self, com):
        hw.calls.append('open')
        if hw.opened:
            return -2
        hw.opened = True
        hw.worker = threading.current_thread()
        hw.entered.set()
        if hw.block:
            assert hw.release.wait(5), 'test did not release simulated open'
            return hw.late_status
        return 0 if hw.powered else -5

    def native_close(self):
        # Closing is permitted only after the native open AND its wrapper exit.
        assert hw.worker is None or not hw.worker.is_alive()
        assert self.thread_lock.locked()
        hw.calls.append('close')
        if hw.block_close:
            assert hw.close_release.wait(5), 'test did not release simulated close'
        if hw.close_status == 0:
            hw.opened = False
        return hw.close_status

    monkeypatch.setattr(base, 'open_port', native_open)
    monkeypatch.setattr(base, 'close_port', native_close)
    monkeypatch.setattr(base, 'set_baud_rate', lambda self, rate: (0, rate))
    monkeypatch.setattr(base, 'get_io_state', lambda self: (0, 0))
    monkeypatch.setattr(base, 'get_comm_error', lambda self: (0, 0))

    def factory(**kwargs):
        driver = cls.__new__(cls)
        driver.device_id = 'retry-test'
        driver.com, driver.port_num, driver.baudrate = 15, 0, 230400
        driver.connected = driver._dll_port_claimed = driver._transport_poisoned = False
        driver._transport_error = None
        driver.err_dict = {}
        driver.thread_lock = threading.Lock()
        driver.logger = logging.getLogger('dmmr-retry-test')
        driver._startup_log_dir = tmp_path
        driver.stop_housekeeping = lambda: None
        driver._resolve_io_timeout = lambda timeout_s=None: 0.03 if timeout_s is None else timeout_s
        driver._warn_on_other_process_ports = lambda: None
        driver._verify_device_type = lambda timeout: driver.DEVICE_TYPE
        driver._warn_if_unexpected_product_id = lambda timeout: None
        driver.close = lambda: None  # Public facade cleanup, not a native close.
        def initialize(timeout_s):
            driver.connect(timeout_s=timeout_s)
            return {address: {} for address in range(8)}
        driver.initialize = initialize
        hw.drivers.append(driver)
        return driver

    monkeypatch.setattr(plugin, '_get_dmmr_driver_class', lambda: factory)
    parent = types.SimpleNamespace(
        name='DMMR', com=15, baudrate=230400, connect_timeout_s=0.03,
        poll_timeout_s=0.03, getChannels=lambda: [], isOn=lambda: False,
    )
    controller = plugin.DMMRController(parent)
    messages, successes = [], []
    controller.print = lambda message, **kwargs: messages.append(message)
    controller._restore_off_ui_state = lambda: None
    controller._sync_status_to_gui = lambda: None
    controller._update_state = lambda: True
    controller.signalComm = types.SimpleNamespace(
        initCompleteSignal=types.SimpleNamespace(emit=lambda: successes.append(True)),
    )
    hw.plugin = plugin
    hw.controller, hw.factory, hw.messages, hw.successes = controller, factory, messages, successes
    yield hw
    hw.release.set()
    hw.close_release.set()
    if hw.worker is not None:
        hw.worker.join(3)
    for driver in hw.drivers:
        pending = getattr(driver, '_pending_dll_call', None)
        if pending is not None:
            pending['thread'].join(3)
        driver._dll_port_claimed = False  # Drop this test's simulated handle only.
    with cls._active_connections_lock:
        cls._purge_stale_connections()


def finish_open(hw):
    hw.release.set()
    assert hw.entered.wait(2)
    hw.worker.join(3)
    assert not hw.worker.is_alive()
    hw.block = False
    hw.powered = True


@pytest.mark.parametrize('late_status', [0, -5])
def test_power_off_then_on_reconnects_only_after_old_open_finishes(connection, late_status, caplog):
    caplog.set_level(logging.INFO, logger='dmmr-retry-test')
    hw = connection
    hw.late_status = late_status
    hw.controller.runInitialization()
    old = hw.drivers[0]
    assert hw.controller.device is old, 'do not discard the still-running open'
    assert old._dll_port_claimed
    assert hw.successes == []
    hw.controller.runInitialization()
    assert hw.controller.device is old
    assert hw.calls == ['open'], 'no new open/close while the first DLL call is active'
    assert len(hw.drivers) == 1
    finish_open(hw)
    assert f'Initial open_port returned {late_status} after ' in caplog.text
    hw.controller.runInitialization()
    assert hw.calls == ['open', 'close', 'open']
    assert len(hw.drivers) == 2
    assert hw.controller.device is hw.drivers[1]
    assert hw.controller.device.connected
    assert hw.successes == [True]
    assert old._transport_poisoned, 'the abandoned instance must never be rearmed'
    assert not old._dll_port_claimed
    assert 'DMMR failed opening finished; serial-port closure confirmed.' in caplog.text
    assert 'hardware stop is not confirmed' in caplog.text
    assert 'high-voltage outputs' not in caplog.text


def test_handshake_error_also_releases_the_partially_open_port(connection):
    hw = connection
    hw.block = False
    hw.controller.runInitialization()
    assert hw.calls == ['open', 'close']
    assert not hw.opened
    hw.powered = True
    hw.controller.runInitialization()
    assert hw.calls == ['open', 'close', 'open']
    assert hw.successes == [True]


def test_failed_close_keeps_backend_and_blocks_new_open(connection):
    hw = connection
    hw.controller.runInitialization()
    old = hw.drivers[0]
    finish_open(hw)
    hw.close_status = -3
    hw.controller.runInitialization()
    assert hw.controller.device is old
    assert old._dll_port_claimed
    assert len(hw.drivers) == 1
    assert hw.calls.count('open') == 1
    assert hw.successes == []
    assert '-3' in hw.messages[-1]


def test_dispose_cannot_erase_a_failed_disconnect(connection):
    hw = connection
    driver = hw.factory()
    driver.connected = driver._dll_port_claimed = True
    driver.disconnect = lambda: False
    hw.controller.device = driver
    hw.controller.initialized = True
    hw.controller._dispose_device()
    assert hw.controller.device is driver
    assert driver._dll_port_claimed
    assert hw.controller.initialized


def test_late_measurement_return_does_not_authorize_a_close(connection):
    hw = connection
    driver = hw.factory()
    driver.connected = driver._dll_port_claimed = True
    def read():
        hw.worker = threading.current_thread()
        hw.entered.set()
        assert hw.release.wait(5)
        return 0
    with pytest.raises(RuntimeError, match='timed out'):
        driver._call_locked_with_timeout(read, 0.03, 'get_module_current')
    finish_open(hw)
    hw.controller.device = driver
    hw.controller.initialized = True
    hw.controller._dispose_device()
    assert hw.controller.device is driver
    assert hw.calls == []
    assert driver._dll_port_claimed


@pytest.mark.parametrize('blocked_open', [False, True])
def test_delayed_close_is_awaited_once_without_reopening(connection, blocked_open):
    hw = connection
    hw.block, hw.block_close = blocked_open, True
    hw.controller.runInitialization()
    old = hw.drivers[0]
    if blocked_open:
        finish_open(hw)
        hw.controller.runInitialization()
    else:
        hw.powered = True
    assert hw.calls == ['open', 'close']
    closing = old._pending_dll_call['thread']
    assert closing.is_alive()
    hw.controller.runInitialization()
    assert hw.calls == ['open', 'close']
    assert hw.controller.device is old and old._dll_port_claimed
    hw.close_release.set()
    closing.join(3)
    assert not closing.is_alive()
    hw.controller.runInitialization()
    assert hw.calls == ['open', 'close', 'open']
    assert hw.successes == [True]
    assert old._transport_poisoned and old._failed_open_released


def test_native_open_not_entered_does_not_require_native_close(connection):
    hw = connection
    driver = hw.factory()
    driver.thread_lock.acquire()
    try:
        with pytest.raises(RuntimeError, match='transport lock timed out'):
            driver.connect(timeout_s=0.03)
        assert hw.calls == []
        assert driver.disconnect() is True
        assert not driver._dll_port_claimed
        assert not driver._transport_poisoned
    finally:
        driver.thread_lock.release()
    hw.block, hw.powered = False, True
    driver.connect(timeout_s=0.03)
    assert hw.calls == ['open']
    assert driver.connected


def test_claim_survives_attempted_release_and_refuses_competing_open(connection):
    hw = connection
    hw.controller.runInitialization()
    old = hw.controller.device
    old._set_port_claimed(False)
    assert old._dll_port_claimed
    other = hw.factory()
    with pytest.raises(RuntimeError, match='still reserved'):
        other.connect(timeout_s=0.03)
    assert not other._dll_port_claimed
    assert hw.calls == ['open']
    finish_open(hw)
    other.connect(timeout_s=0.03)
    assert hw.calls == ['open', 'close', 'open']
    assert other.connected and other._dll_port_claimed
    assert old.disconnect() is True
    old._set_port_claimed(True)
    assert not old._dll_port_claimed, 'a retired owner cannot reclaim the new connection'
    assert hw.calls == ['open', 'close', 'open']


def test_registry_retains_orphaned_open_until_a_new_connection_can_retire_it(connection):
    import gc
    import weakref
    hw = connection
    hw.controller.runInitialization()
    reference = weakref.ref(hw.controller.device)
    finish_open(hw)
    hw.controller.device = None
    hw.drivers.clear()
    gc.collect()
    assert reference() is not None, 'Python GC must not abandon a native serial handle'
    old = reference()
    hw.drivers.append(old)
    new = hw.factory()
    new.connect(timeout_s=0.03)
    assert hw.calls == ['open', 'close', 'open']
    assert old._failed_open_released and not old._dll_port_claimed


def test_connected_false_with_a_claim_is_not_a_successful_disconnect(connection):
    hw = connection
    driver = hw.factory()
    driver._set_port_claimed(True)
    hw.opened = True
    hw.close_status = -3
    assert driver.disconnect() is False
    assert driver._dll_port_claimed and not driver.connected
    assert hw.calls == ['close']


def test_close_communication_preserves_a_pending_open(connection):
    hw = connection
    hw.controller.runInitialization()
    old = hw.controller.device
    hw.controller.closeCommunication()
    assert hw.controller.device is old
    assert old._dll_port_claimed
    assert hw.calls == ['open']
    assert hw.controller.main_state == 'Connection pending'
    finish_open(hw)
    hw.controller.closeCommunication(final_state='Disconnected')
    assert hw.controller.device is None
    assert hw.controller.main_state == 'Disconnected'
    assert hw.calls == ['open', 'close']


def test_failed_state_verification_does_not_emit_initialization_success(connection):
    hw = connection
    hw.block, hw.powered = False, True
    hw.controller._update_state = lambda: False
    hw.controller.runInitialization()
    assert hw.successes == []
    assert hw.controller.device is None
    assert hw.calls == ['open', 'close']


def test_failed_normal_disposal_cannot_claim_disconnection(connection):
    hw = connection
    driver = hw.factory()
    driver.connected = True
    driver._set_port_claimed(True)
    driver.disconnect = lambda: False
    hw.controller.device = driver
    hw.controller.initialized = True
    hw.controller._attempt_device_disable = lambda: None
    hw.controller.closeCommunication(final_state='Disconnected')
    assert hw.controller.device is driver
    assert hw.controller.initialized
    assert hw.controller.main_state == 'Shutdown unconfirmed'


def _connect_in_thread(driver, results):
    try:
        results.append(driver.connect(timeout_s=1))
    except BaseException as exc:
        results.append(exc)


def test_overlapping_connect_cannot_report_ready_during_verification(connection):
    hw = connection
    hw.block, hw.powered = False, True
    driver = hw.factory()
    verifying, release = threading.Event(), threading.Event()
    results = []

    def verify(timeout):
        verifying.set()
        assert release.wait(3)
        return driver.DEVICE_TYPE

    driver._verify_device_type = verify
    worker = threading.Thread(target=_connect_in_thread, args=(driver, results))
    worker.start()
    try:
        assert verifying.wait(2)
        assert driver.connected  # Native handle exists; connect is not done.
        with pytest.raises(RuntimeError, match='in progress'):
            driver.connect(timeout_s=1)
        assert hw.calls == ['open']
    finally:
        release.set()
        worker.join(3)
    assert results == [True]
    assert driver.connected and driver._dll_port_claimed


def test_reentrant_connect_does_not_release_outer_setup_guard(connection):
    hw = connection
    hw.block, hw.powered = False, True
    driver = hw.factory()

    def verify(timeout):
        with pytest.raises(RuntimeError, match='in progress'):
            driver.connect(timeout_s=1)
        assert driver._opening_in_progress
        assert driver._opening_caller is threading.current_thread()
        assert driver.disconnect() is False
        return driver.DEVICE_TYPE

    driver._verify_device_type = verify
    assert driver.connect(timeout_s=1) is True
    assert hw.calls == ['open']
    assert driver.connected and not driver._opening_in_progress


def test_late_competing_refusal_cannot_erase_successful_connection(connection):
    hw = connection
    hw.block, hw.powered = False, True
    driver = hw.factory()
    original_open = driver._call_initial_open
    waiting, resume = threading.Event(), threading.Event()
    rejected, raise_refusal = threading.Event(), threading.Event()
    verifying, release_verify = threading.Event(), threading.Event()
    winner_results, loser_results = [], []

    def delayed_open(*args):
        if threading.current_thread().name != 'losing-connect':
            return original_open(*args)
        waiting.set()
        assert resume.wait(3)
        try:
            return original_open(*args)
        except RuntimeError:
            rejected.set()
            assert raise_refusal.wait(3)
            raise

    def verify(timeout):
        verifying.set()
        assert release_verify.wait(3)
        return driver.DEVICE_TYPE

    driver._call_initial_open = delayed_open
    driver._verify_device_type = verify
    loser = threading.Thread(target=_connect_in_thread, args=(driver, loser_results),
                             name='losing-connect')
    winner = threading.Thread(target=_connect_in_thread, args=(driver, winner_results))
    loser.start()
    try:
        assert waiting.wait(2)
        winner.start()
        assert verifying.wait(2)
        resume.set()
        assert rejected.wait(2)
        release_verify.set()
        winner.join(3)
        assert winner_results == [True]
        assert driver.connected
        raise_refusal.set()
        loser.join(3)
        assert len(loser_results) == 1 and isinstance(loser_results[0], RuntimeError)
        assert driver.connected, 'a caller that did not open must not reset the winner'
        assert driver._dll_port_claimed
        assert hw.calls == ['open']
    finally:
        resume.set()
        release_verify.set()
        raise_refusal.set()
        loser.join(3)
        if winner.ident is not None:
            winner.join(3)


def test_connection_readiness_uses_the_reservation_lock(connection, monkeypatch):
    driver = connection.factory()
    cls = type(driver)
    original_getattribute = cls.__getattribute__
    reads = []

    def checked_getattribute(self, name):
        if self is driver and name in ('connected', '_opening_in_progress'):
            assert cls._active_connections_lock.locked()
            reads.append(name)
        return original_getattribute(self, name)

    monkeypatch.setattr(cls, '__getattribute__', checked_getattribute)
    assert driver._connection_is_ready() is False
    assert reads == ['_opening_in_progress', 'connected']
    driver._opening_in_progress = True
    with pytest.raises(RuntimeError, match='in progress'):
        driver._connection_is_ready()
