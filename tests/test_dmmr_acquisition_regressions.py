"""DMMR regressions for the Explorer lock/startup failures seen on COM15."""

import contextlib
import sys
import threading
import types

import numpy as np
import pytest

from test_dmmr_plugin_behavior import _load_module


class ExplorerStyleLock:
    """Explorer 1.0.1 exposes a raw lock AND a log-level-dependent context.

    Its BASIC/DEBUG context swallows exceptions. Its VERBOSE/TRACE context
    propagates them but does not release the lock on that path. Expired waits
    are immediate here so regressions do not spend a second per nested lock.
    """

    def __init__(self, parent, *, swallow=True):
        self._lock = threading.Lock()
        self.parent = parent
        self.swallow = swallow
        self.attempts = 0
        self.context_entries = 0

    def acquire(self, blocking=True, timeout=-1):
        self.attempts += 1
        return self._lock.acquire(blocking=False)

    def release(self):
        self._lock.release()

    @contextlib.contextmanager
    def acquire_timeout(self, timeout, timeoutMessage="", already_acquired=False):
        self.context_entries += 1
        acquired = already_acquired or self.acquire(timeout=timeout)
        if not self.swallow:
            yield acquired
            if acquired and not already_acquired:
                self.release()
            return
        try:
            yield acquired
        except Exception:
            self.parent.errorCount += 1
        finally:
            if acquired and not already_acquired:
                self.release()


class Device:
    NO_ERR = 0
    ERR_COMMAND_WRONG = -13

    def __init__(self):
        self.calls = []
        self.enabled = False
        self.auto_range_failure = None
        self.wrong_command_once = False

    def set_enable(self, enabled, **kwargs):
        self.calls.append(("enable", enabled))
        self.enabled = enabled
        return self.NO_ERR

    def set_automatic_current(self, enabled, **kwargs):
        self.calls.append(("automatic", enabled))
        return self.NO_ERR

    def set_module_auto_range(self, address, enabled, **kwargs):
        self.calls.append(("auto_range", address, enabled))
        return -12 if address == self.auto_range_failure else self.NO_ERR

    def get_state(self, **kwargs):
        self.calls.append(("state",))
        return self.NO_ERR, "0x0000", "ST_OVERLOAD"

    def get_module_current(self, address, **kwargs):
        self.calls.append(("current", address))
        if self.wrong_command_once:
            self.wrong_command_once = False
            return self.ERR_COMMAND_WRONG, np.nan, 0
        return self.NO_ERR, (address + 1) * 1e-12, 0

    def format_status(self, status):
        return str(status)


@pytest.fixture
def rig():
    module = _load_module()
    channels = [types.SimpleNamespace(real=True, module_address=lambda address=address: address)
                for address in (0, 3)]
    ui_states = []
    parent = types.SimpleNamespace(
        connect_timeout_s=5.0, poll_timeout_s=2.0, interval=0,
        isOn=lambda: True, getChannels=lambda: channels,
        getConfiguredModules=lambda: [0, 3],
        _set_on_ui_state=ui_states.append,
    )
    controller = module.DMMRController(parent)
    controller.initialized = True
    controller.detected_module_ids = [0, 3]
    controller.lock = ExplorerStyleLock(controller)
    controller.device = Device()
    logs = []
    controller.print = lambda message, flag=None: logs.append(message)
    return types.SimpleNamespace(module=module, controller=controller,
                                 device=controller.device, logs=logs, ui_states=ui_states)


@pytest.mark.parametrize("swallow", [True, False], ids=["basic", "verbose"])
def test_hardware_exception_propagates_and_releases_host_lock(rig, swallow):
    controller = rig.controller
    controller.lock.swallow = swallow
    failure = RuntimeError("set_module_auto_range(3, True) failed: -12")
    with pytest.raises(RuntimeError) as caught:
        with controller._controller_lock_section("busy"):
            raise failure
    assert caught.value is failure
    assert not controller.lock._lock.locked()
    assert controller.errorCount == 0
    assert controller.lock.context_entries == 0


@pytest.mark.parametrize("swallow", [True, False], ids=["basic", "verbose"])
def test_lock_contention_stays_a_timeout_without_a_generator_error(rig, swallow):
    controller = rig.controller
    controller.lock.swallow = swallow
    assert controller.lock.acquire()
    try:
        with pytest.raises(TimeoutError, match="busy"):
            with controller._controller_lock_section("busy", timeout_s=0, log_timeout=False):
                pytest.fail("A contended lock must not enter the hardware section")
        assert controller.lock._lock.locked(), "Another caller's lock was released"
        assert controller.errorCount == 0
        assert rig.logs == []
    finally:
        controller.lock.release()


def test_failed_auto_range_aborts_startup_and_disables_measurement(rig):
    controller = rig.controller
    rig.device.auto_range_failure = 3
    controller.toggleOn()
    assert controller.acquiring is False
    assert rig.device.enabled is False
    assert rig.ui_states == [False]
    assert controller.errorCount == 1
    assert any("Failed to toggle DMMR acquisition" in message and "-12" in message
               for message in rig.logs)
    assert "DMMR acquisition enabled." not in rig.logs
    assert rig.device.calls.count(("enable", False)) == 1
    assert not controller.lock._lock.locked()


def test_startup_selects_manual_polling_before_configuring_module_ranges(rig):
    rig.controller.toggleOn()
    assert rig.controller.acquiring is True
    assert rig.device.calls.index(("automatic", False)) < rig.device.calls.index(("auto_range", 0, True))
    assert rig.device.calls.count(("automatic", False)) == 1


def framework_acquisition(controller):
    """The inherited Explorer loop already owns the non-reentrant lock."""
    from esibd.core import getTestMode

    while controller.acquiring:
        with controller.lock.acquire_timeout(1) as acquired:
            if acquired:
                if getTestMode():
                    controller.fakeNumbers()
                else:
                    controller.readNumbers()
                controller.signalComm.updateValuesSignal.emit()


@pytest.mark.parametrize("recover_polling_mode", [False, True], ids=["normal", "recover-mode"])
def test_acquisition_reuses_one_lock_for_state_modules_and_recovery(rig, monkeypatch, recover_polling_mode):
    controller = rig.controller
    # The old plugin inherits this loop; the corrected plugin must provide its
    # own loop that explicitly forwards ownership to the nested read sections.
    monkeypatch.setattr(rig.module.DeviceController, "runAcquisition", framework_acquisition, raising=False)
    rig.device.wrong_command_once = recover_polling_mode
    controller.acquiring = True
    published = []

    def publish_once():
        published.append(dict(controller.values))
        controller.acquiring = False

    controller.signalComm.updateValuesSignal = types.SimpleNamespace(emit=publish_once)
    controller.runAcquisition()
    assert published == [{0: 1e-12, 3: 4e-12}]
    assert controller.errorCount == 0
    assert controller.lock.attempts == 1
    assert not controller.lock._lock.locked()
    assert not any("generator didn't yield" in message for message in rig.logs)
    assert (("automatic", False) in rig.device.calls) is recover_polling_mode


def test_test_mode_never_reads_hardware(rig, monkeypatch):
    controller = rig.controller
    monkeypatch.setattr(rig.module, "getTestMode", lambda: True, raising=False)
    monkeypatch.setattr(sys.modules["esibd.core"], "getTestMode", lambda: True)
    monkeypatch.setattr(rig.module.DeviceController, "runAcquisition", framework_acquisition, raising=False)
    controller.acquiring = True
    controller.values = {0: 12e-12, 3: 25e-12}
    published = []

    def publish_once():
        published.append(dict(controller.values))
        controller.acquiring = False

    controller.signalComm.updateValuesSignal = types.SimpleNamespace(emit=publish_once)
    controller.runAcquisition()
    assert len(published) == 1
    assert all(np.isnan(value) for value in published[0].values())
    assert rig.device.calls == []
    assert not controller.lock._lock.locked()


def test_stopped_acquisition_does_not_read_remaining_modules(rig):
    controller = rig.controller
    controller.acquiring = True
    read_current = rig.device.get_module_current

    def stop_on_first_module(address, **kwargs):
        controller.acquiring = False
        return read_current(address, **kwargs)

    rig.device.get_module_current = stop_on_first_module
    controller.readNumbers()
    assert ("current", 0) in rig.device.calls
    assert ("current", 3) not in rig.device.calls
    assert all(np.isnan(value) for value in controller.values.values())


def test_busy_acquisition_invalidates_previous_sample_without_error_cascade(rig, monkeypatch):
    controller = rig.controller
    monkeypatch.setattr(rig.module.DeviceController, "runAcquisition", framework_acquisition, raising=False)
    controller.acquiring = True
    controller.values = {0: 12e-12, 3: 25e-12}
    published = []

    def publish_once():
        published.append(dict(controller.values))
        controller.acquiring = False

    controller.signalComm.updateValuesSignal = types.SimpleNamespace(emit=publish_once)
    # Bound the old inherited loop too, which otherwise spins forever without
    # emitting an update when the outer lock is busy.
    original_acquire = controller.lock.acquire

    def acquire_once(**kwargs):
        controller.acquiring = False
        return original_acquire(**kwargs)

    assert controller.lock.acquire()
    monkeypatch.setattr(controller.lock, "acquire", acquire_once)
    try:
        controller.runAcquisition()
        assert len(published) == 1
        assert all(np.isnan(value) for value in published[0].values())
        assert controller.errorCount == 0
        assert rig.device.calls == []
        assert rig.logs == []
    finally:
        controller.lock.release()
