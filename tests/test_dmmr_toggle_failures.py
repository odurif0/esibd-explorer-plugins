"""A failed ON is not an OFF confirmation, including a lost enable ACK."""
from __future__ import annotations


import numpy as np
import pytest

from support.dmmr import start


def assert_unconfirmed(rig):
    assert rig.parent.on is True, "Keep an OFF action, not another ON request"
    assert rig.controller.main_state == rig.module._DMMR_SHUTDOWN_UNCONFIRMED_STATE
    assert rig.parent.main_state == rig.controller.main_state
    assert rig.controller.acquiring is False
    assert np.isnan(rig.controller.values[5]), "Do not display the previous acquisition's sample"
    assert "DMMR acquisition enabled." not in rig.logs
    assert "DMMR acquisition disabled." not in rig.logs


def test_logged_range_error_with_failed_cleanup_stays_unconfirmed(dmmr_toggle_rig):
    dmmr_toggle_rig.device.reject_cleanup = True
    start(dmmr_toggle_rig)
    assert_unconfirmed(dmmr_toggle_rig)
    assert dmmr_toggle_rig.device.enabled is True
    assert ("enable", False) in dmmr_toggle_rig.device.calls
    assert any("set_module_auto_range(5, True) failed: -12" in msg for msg in dmmr_toggle_rig.logs)
    # A generic ST_ON status says nothing about confirmed shutdown gates.
    for _ in range(3):
        dmmr_toggle_rig.controller._update_state()
        dmmr_toggle_rig.controller._sync_status_to_gui()
        assert_unconfirmed(dmmr_toggle_rig)


@pytest.mark.parametrize("failure", ["range", "enable_ack"])
def test_successful_cleanup_verifies_both_gates_before_restoring_off(dmmr_toggle_rig, failure):
    dmmr_toggle_rig.device.fail_start = failure
    start(dmmr_toggle_rig)
    assert dmmr_toggle_rig.parent.on is False
    assert dmmr_toggle_rig.device.enabled is False
    assert dmmr_toggle_rig.states == [False]
    assert dmmr_toggle_rig.controller.acquiring is False
    assert ("read", "enable") in dmmr_toggle_rig.device.calls
    assert ("read", "automatic") in dmmr_toggle_rig.device.calls
    assert dmmr_toggle_rig.device.calls.index(("enable", False)) < dmmr_toggle_rig.device.calls.index(("read", "enable"))
    assert np.isnan(dmmr_toggle_rig.controller.values[5])


def test_lost_enable_ack_with_failed_cleanup_does_not_claim_off(dmmr_toggle_rig):
    dmmr_toggle_rig.device.fail_start = "enable_ack"
    dmmr_toggle_rig.device.reject_cleanup = True
    start(dmmr_toggle_rig)
    assert dmmr_toggle_rig.device.enabled is True
    assert_unconfirmed(dmmr_toggle_rig)


@pytest.mark.parametrize("gate", ["enable", "automatic"])
@pytest.mark.parametrize("readback", [(0, True), (0, None), (-13, False), RuntimeError("read failed")],
                         ids=["still_on", "unknown", "status_error", "exception"])


def test_cleanup_needs_positive_off_readbacks(dmmr_toggle_rig, gate, readback):
    dmmr_toggle_rig.device.readback[gate] = readback
    start(dmmr_toggle_rig)
    assert_unconfirmed(dmmr_toggle_rig)
    assert ("read", "automatic") in dmmr_toggle_rig.device.calls
    assert ("read", "enable") in dmmr_toggle_rig.device.calls


def test_following_off_request_can_recover_without_another_enable(dmmr_toggle_rig):
    dmmr_toggle_rig.device.reject_cleanup = True
    start(dmmr_toggle_rig)
    assert_unconfirmed(dmmr_toggle_rig)
    enable_count = dmmr_toggle_rig.device.calls.count(("enable", True))
    dmmr_toggle_rig.device.reject_cleanup = False
    dmmr_toggle_rig.parent.on = False  # The following click requests OFF.
    dmmr_toggle_rig.controller.toggleOn()
    assert dmmr_toggle_rig.parent.on is False
    assert dmmr_toggle_rig.device.enabled is False
    assert dmmr_toggle_rig.controller.main_state != dmmr_toggle_rig.module._DMMR_SHUTDOWN_UNCONFIRMED_STATE
    assert dmmr_toggle_rig.device.calls.count(("enable", True)) == enable_count
    assert "DMMR acquisition disabled." in dmmr_toggle_rig.logs


def test_manual_off_attempts_enable_false_even_when_automatic_disable_raises(dmmr_toggle_rig):
    dmmr_toggle_rig.device.enabled = True
    dmmr_toggle_rig.device.auto_disable_raises = True
    dmmr_toggle_rig.parent.on = False
    dmmr_toggle_rig.controller.toggleOn()
    assert ("enable", False) in dmmr_toggle_rig.device.calls
    assert_unconfirmed(dmmr_toggle_rig)


@pytest.mark.parametrize("gate", ["enable", "automatic"])
def test_manual_off_is_not_confirmed_by_successful_setter_alone(dmmr_toggle_rig, gate):
    dmmr_toggle_rig.parent.on = False
    dmmr_toggle_rig.device.readback[gate] = (0, True)
    dmmr_toggle_rig.controller.toggleOn()
    assert_unconfirmed(dmmr_toggle_rig)


def test_failed_final_status_read_cannot_announce_successful_start(dmmr_toggle_rig):
    dmmr_toggle_rig.device.fail_start = None
    dmmr_toggle_rig.device.state_error = True
    start(dmmr_toggle_rig)
    assert dmmr_toggle_rig.parent.on is False  # Both OFF gates remain readable in this case.
    assert dmmr_toggle_rig.device.enabled is False
    assert dmmr_toggle_rig.controller.acquiring is False
    assert "DMMR acquisition enabled." not in dmmr_toggle_rig.logs


def test_failed_acquisition_thread_start_also_verifies_shutdown(dmmr_toggle_rig):
    dmmr_toggle_rig.device.fail_start = None

    def failed_start():
        dmmr_toggle_rig.controller.acquiring = True
        raise RuntimeError("can't start new thread")

    dmmr_toggle_rig.controller.startAcquisition = failed_start
    start(dmmr_toggle_rig)
    assert dmmr_toggle_rig.parent.on is False
    assert dmmr_toggle_rig.device.enabled is False
    assert dmmr_toggle_rig.controller.acquiring is False
    assert "DMMR acquisition enabled." not in dmmr_toggle_rig.logs
    assert any("can't start new thread" in msg for msg in dmmr_toggle_rig.logs)


def test_cleanup_lock_timeout_preserves_unknown_state(dmmr_toggle_rig):
    import threading

    class BusyLock:
        def acquire(self, **kwargs):
            return False

    dmmr_toggle_rig.controller.lock = BusyLock()
    start(dmmr_toggle_rig)
    assert_unconfirmed(dmmr_toggle_rig)
    assert dmmr_toggle_rig.device.calls == []
    dmmr_toggle_rig.controller.lock = threading.Lock()
    dmmr_toggle_rig.parent.on = False
    dmmr_toggle_rig.controller.toggleOn()
    assert dmmr_toggle_rig.parent.on is False
    assert "DMMR acquisition disabled." in dmmr_toggle_rig.logs


def test_successful_start_still_acquires_normally(dmmr_toggle_rig):
    dmmr_toggle_rig.device.fail_start = None
    start(dmmr_toggle_rig)
    assert dmmr_toggle_rig.parent.on is True
    assert dmmr_toggle_rig.controller.acquiring is True
    assert dmmr_toggle_rig.device.enabled is True
    assert "DMMR acquisition enabled." in dmmr_toggle_rig.logs
