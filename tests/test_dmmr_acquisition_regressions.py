"""DMMR regressions for the Explorer lock/startup failures seen on COM15."""

import sys
import types

import numpy as np
import pytest



@pytest.mark.parametrize("swallow", [True, False], ids=["basic", "verbose"])
def test_hardware_exception_propagates_and_releases_host_lock(dmmr_acquisition_rig, swallow):
    controller = dmmr_acquisition_rig.controller
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
def test_lock_contention_stays_a_timeout_without_a_generator_error(dmmr_acquisition_rig, swallow):
    controller = dmmr_acquisition_rig.controller
    controller.lock.swallow = swallow
    assert controller.lock.acquire()
    try:
        with pytest.raises(TimeoutError, match="busy"):
            with controller._controller_lock_section("busy", timeout_s=0, log_timeout=False):
                pytest.fail("A contended lock must not enter the hardware section")
        assert controller.lock._lock.locked(), "Another caller's lock was released"
        assert controller.errorCount == 0
        assert dmmr_acquisition_rig.logs == []
    finally:
        controller.lock.release()


def test_failed_auto_range_aborts_startup_and_disables_measurement(dmmr_acquisition_rig):
    controller = dmmr_acquisition_rig.controller
    dmmr_acquisition_rig.device.auto_range_failure = 3
    controller.toggleOn()
    assert controller.acquiring is False
    assert dmmr_acquisition_rig.device.enabled is False
    assert dmmr_acquisition_rig.ui_states == [False]
    assert controller.errorCount == 1
    assert any("Failed to toggle DMMR acquisition" in message and "-12" in message
               for message in dmmr_acquisition_rig.logs)
    assert "DMMR acquisition enabled." not in dmmr_acquisition_rig.logs
    assert dmmr_acquisition_rig.device.calls.count(("enable", False)) == 1
    assert not controller.lock._lock.locked()


def test_startup_selects_manual_polling_before_configuring_module_ranges(dmmr_acquisition_rig):
    dmmr_acquisition_rig.controller.toggleOn()
    assert dmmr_acquisition_rig.controller.acquiring is True
    assert dmmr_acquisition_rig.device.calls.index(("automatic", False)) < dmmr_acquisition_rig.device.calls.index(("auto_range", 0, True))
    assert dmmr_acquisition_rig.device.calls.count(("automatic", False)) == 1


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
def test_acquisition_reuses_one_lock_for_state_modules_and_recovery(dmmr_acquisition_rig, monkeypatch, recover_polling_mode):
    controller = dmmr_acquisition_rig.controller
    # The old plugin inherits this loop; the corrected plugin must provide its
    # own loop that explicitly forwards ownership to the nested read sections.
    monkeypatch.setattr(dmmr_acquisition_rig.module.DeviceController, "runAcquisition", framework_acquisition, raising=False)
    controller.toggleOn()  # The recovery must use ranges actually verified at ON.
    assert controller.acquiring
    dmmr_acquisition_rig.device.calls.clear()
    controller.lock.attempts = 0
    dmmr_acquisition_rig.device.wrong_command_once = recover_polling_mode
    published = []

    def publish_once():
        published.append(dict(controller.values))
        if len(published) == 1 + int(recover_polling_mode):
            controller.acquiring = False

    controller.signalComm.updateValuesSignal = types.SimpleNamespace(emit=publish_once)
    controller.runAcquisition()
    assert published[-1] == {0: 1e-12, 3: 4e-12}
    if recover_polling_mode:
        assert all(np.isnan(value) for value in published[0].values())
        assert controller._read_recovery.last_incident['resumed'] is True
    assert controller.errorCount == 0
    assert controller.lock.attempts == 1 + int(recover_polling_mode)
    assert not controller.lock._lock.locked()
    assert not any("generator didn't yield" in message or 'mode was active' in message for message in dmmr_acquisition_rig.logs)
    assert ("automatic", False) not in dmmr_acquisition_rig.device.calls  # It was already manual.


def test_test_mode_never_reads_hardware(dmmr_acquisition_rig, monkeypatch):
    controller = dmmr_acquisition_rig.controller
    monkeypatch.setattr(dmmr_acquisition_rig.module, "getTestMode", lambda: True, raising=False)
    monkeypatch.setattr(sys.modules["esibd.core"], "getTestMode", lambda: True)
    monkeypatch.setattr(dmmr_acquisition_rig.module.DeviceController, "runAcquisition", framework_acquisition, raising=False)
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
    assert dmmr_acquisition_rig.device.calls == []
    assert not controller.lock._lock.locked()


def test_stopped_acquisition_does_not_read_remaining_modules(dmmr_acquisition_rig):
    controller = dmmr_acquisition_rig.controller
    controller.acquiring = True
    read_current = dmmr_acquisition_rig.device.get_module_current

    def stop_on_first_module(address, **kwargs):
        controller.acquiring = False
        return read_current(address, **kwargs)

    dmmr_acquisition_rig.device.get_module_current = stop_on_first_module
    controller.readNumbers()
    assert ("current", 0) in dmmr_acquisition_rig.device.calls
    assert ("current", 3) not in dmmr_acquisition_rig.device.calls
    assert all(np.isnan(value) for value in controller.values.values())


def test_busy_acquisition_invalidates_previous_sample_without_error_cascade(dmmr_acquisition_rig, monkeypatch):
    controller = dmmr_acquisition_rig.controller
    monkeypatch.setattr(dmmr_acquisition_rig.module.DeviceController, "runAcquisition", framework_acquisition, raising=False)
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
        assert dmmr_acquisition_rig.device.calls == []
        assert dmmr_acquisition_rig.logs == []
    finally:
        controller.lock.release()
