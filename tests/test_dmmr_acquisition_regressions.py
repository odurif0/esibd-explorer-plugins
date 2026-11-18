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


def test_startup_selects_manual_polling_before_configuring_module_ranges(dmmr_acquisition_rig):
    from support.dmmr import FaultingDMMR
    rig = dmmr_acquisition_rig
    worker = rig.controller.device = FaultingDMMR()
    worker.fail_start = None
    rig.controller.toggleOn()
    assert rig.controller.acquiring is True
    first_range = next(i for i, call in enumerate(worker.calls) if call[0] == "range")
    assert worker.calls.index(("automatic", False)) < first_range
    assert worker.calls.count(("automatic", False)) == 1


def test_one_acquisition_cycle_takes_the_controller_lock_once(dmmr_native_acquisition_rig):
    """The loop owns the non-reentrant lock and forwards it to the state read and the poll."""
    rig = dmmr_native_acquisition_rig
    controller = rig.controller
    controller.lock.attempts = 0
    published = []

    def publish_once():
        published.append(dict(controller.values))
        controller.acquiring = False

    controller.signalComm.updateValuesSignal = types.SimpleNamespace(emit=publish_once)
    controller.runAcquisition()
    assert published == [{0: 1e-12, 3: 1e-12}]
    assert controller.errorCount == 0
    assert controller.lock.attempts == 1
    assert not controller.lock._lock.locked()
