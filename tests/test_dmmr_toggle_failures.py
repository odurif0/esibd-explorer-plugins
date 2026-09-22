"""A failed ON is not an OFF confirmation, including a lost enable ACK."""
from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from test_dmmr_plugin_behavior import _load_module


class FaultingDMMR:
    NO_ERR = 0

    def __init__(self):
        self.enabled = False
        self.automatic = False
        self.calls = []
        self.fail_start = "range"
        self.start_failed = False
        self.reject_cleanup = False
        self.auto_disable_raises = False
        self.readback = {}
        self.state_error = False
        self.connected = True
        self.disconnect_result = True
        self.disconnect_error = None
        self.port_closes = 0

    def disconnect(self):
        self.calls.append(("disconnect",))
        if self.disconnect_error is not None:
            raise self.disconnect_error
        if self.disconnect_result is True and self.connected:
            self.connected = False
            self.port_closes += 1
        return self.disconnect_result

    def set_enable(self, value, **kwargs):
        self.calls.append(("enable", value))
        if not value and self.reject_cleanup:
            return -13
        self.enabled = value
        if value and self.fail_start == "enable_ack":
            self.start_failed = True
            return -12  # Hardware changed, but the acknowledgement was lost.
        return 0

    def set_automatic_current(self, value, **kwargs):
        self.calls.append(("automatic", value))
        if self.auto_disable_raises:
            raise RuntimeError("automatic command failed")
        if self.start_failed and self.reject_cleanup:
            return -13
        self.automatic = value
        return 0

    def set_module_auto_range(self, address, value, **kwargs):
        self.calls.append(("range", address, value))
        if address == 5 and self.fail_start == "range":
            self.start_failed = True
            return -12
        return 0

    def get_module_meas_range(self, address, **kwargs):
        self.calls.append(("range_readback", address))
        return self.NO_ERR, 0, True

    def _get_gate(self, name, value):
        self.calls.append(("read", name))
        result = self.readback.get(name, (0, value))
        if isinstance(result, Exception):
            raise result
        return result

    def get_enable(self, **kwargs):
        return self._get_gate("enable", self.enabled)

    def get_automatic_current(self, **kwargs):
        return self._get_gate("automatic", self.automatic)

    def get_state(self, **kwargs):
        return (-13 if self.state_error else 0), "0x0000", "ST_ON"

    def format_status(self, status):
        return str(status)


@pytest.fixture
def rig():
    module = _load_module()
    states, logs = [], []
    parent = SimpleNamespace(
        connect_timeout_s=0.1, poll_timeout_s=0.1, on=True,
        getConfiguredModules=lambda: list(range(8)),
        getChannels=lambda: [SimpleNamespace(real=True, module_address=lambda: 5)],
    )
    parent.isOn = lambda: parent.on

    def set_ui(value):
        states.append(value)
        parent.on = value

    parent._set_on_ui_state = set_ui
    controller = module.DMMRController(parent)
    controller.device = FaultingDMMR()
    controller.initialized = True
    controller.detected_module_ids = list(range(8))
    controller.values = {5: 12e-12}
    controller.print = lambda message, **kwargs: logs.append(message)
    return SimpleNamespace(module=module, controller=controller, device=controller.device,
                           parent=parent, states=states, logs=logs)


def start(rig):
    assert rig.controller._begin_transition(True)
    rig.controller.toggleOn()
    assert rig.controller.transitioning is False
    assert rig.controller.transition_target_on is None


def assert_unconfirmed(rig):
    assert rig.parent.on is True, "Keep an OFF action, not another ON request"
    assert rig.controller.main_state == rig.module._DMMR_SHUTDOWN_UNCONFIRMED_STATE
    assert rig.parent.main_state == rig.controller.main_state
    assert rig.controller.acquiring is False
    assert np.isnan(rig.controller.values[5]), "Do not display the previous acquisition's sample"
    assert "DMMR acquisition enabled." not in rig.logs
    assert "DMMR acquisition disabled." not in rig.logs


def test_logged_range_error_with_failed_cleanup_stays_unconfirmed(rig):
    rig.device.reject_cleanup = True
    start(rig)
    assert_unconfirmed(rig)
    assert rig.device.enabled is True
    assert ("enable", False) in rig.device.calls
    assert any("set_module_auto_range(5, True) failed: -12" in msg for msg in rig.logs)
    # A generic ST_ON status says nothing about confirmed shutdown gates.
    for _ in range(3):
        rig.controller._update_state()
        rig.controller._sync_status_to_gui()
        assert_unconfirmed(rig)


@pytest.mark.parametrize("failure", ["range", "enable_ack"])
def test_successful_cleanup_verifies_both_gates_before_restoring_off(rig, failure):
    rig.device.fail_start = failure
    start(rig)
    assert rig.parent.on is False
    assert rig.device.enabled is False
    assert rig.states == [False]
    assert rig.controller.acquiring is False
    assert ("read", "enable") in rig.device.calls
    assert ("read", "automatic") in rig.device.calls
    assert rig.device.calls.index(("enable", False)) < rig.device.calls.index(("read", "enable"))
    assert np.isnan(rig.controller.values[5])


def test_lost_enable_ack_with_failed_cleanup_does_not_claim_off(rig):
    rig.device.fail_start = "enable_ack"
    rig.device.reject_cleanup = True
    start(rig)
    assert rig.device.enabled is True
    assert_unconfirmed(rig)


@pytest.mark.parametrize("gate", ["enable", "automatic"])
@pytest.mark.parametrize("readback", [(0, True), (0, None), (-13, False), RuntimeError("read failed")],
                         ids=["still_on", "unknown", "status_error", "exception"])
def test_cleanup_needs_positive_off_readbacks(rig, gate, readback):
    rig.device.readback[gate] = readback
    start(rig)
    assert_unconfirmed(rig)
    assert ("read", "automatic") in rig.device.calls
    assert ("read", "enable") in rig.device.calls


def test_following_off_request_can_recover_without_another_enable(rig):
    rig.device.reject_cleanup = True
    start(rig)
    assert_unconfirmed(rig)
    enable_count = rig.device.calls.count(("enable", True))
    rig.device.reject_cleanup = False
    rig.parent.on = False  # The following click requests OFF.
    rig.controller.toggleOn()
    assert rig.parent.on is False
    assert rig.device.enabled is False
    assert rig.controller.main_state != rig.module._DMMR_SHUTDOWN_UNCONFIRMED_STATE
    assert rig.device.calls.count(("enable", True)) == enable_count
    assert "DMMR acquisition disabled." in rig.logs


def test_manual_off_attempts_enable_false_even_when_automatic_disable_raises(rig):
    rig.device.enabled = True
    rig.device.auto_disable_raises = True
    rig.parent.on = False
    rig.controller.toggleOn()
    assert ("enable", False) in rig.device.calls
    assert_unconfirmed(rig)


@pytest.mark.parametrize("gate", ["enable", "automatic"])
def test_manual_off_is_not_confirmed_by_successful_setter_alone(rig, gate):
    rig.parent.on = False
    rig.device.readback[gate] = (0, True)
    rig.controller.toggleOn()
    assert_unconfirmed(rig)


def test_failed_final_status_read_cannot_announce_successful_start(rig):
    rig.device.fail_start = None
    rig.device.state_error = True
    start(rig)
    assert rig.parent.on is False  # Both OFF gates remain readable in this case.
    assert rig.device.enabled is False
    assert rig.controller.acquiring is False
    assert "DMMR acquisition enabled." not in rig.logs


def test_failed_acquisition_thread_start_also_verifies_shutdown(rig):
    rig.device.fail_start = None

    def failed_start():
        rig.controller.acquiring = True
        raise RuntimeError("can't start new thread")

    rig.controller.startAcquisition = failed_start
    start(rig)
    assert rig.parent.on is False
    assert rig.device.enabled is False
    assert rig.controller.acquiring is False
    assert "DMMR acquisition enabled." not in rig.logs
    assert any("can't start new thread" in msg for msg in rig.logs)


def test_cleanup_lock_timeout_preserves_unknown_state(rig):
    import threading

    class BusyLock:
        def acquire(self, **kwargs):
            return False

    rig.controller.lock = BusyLock()
    start(rig)
    assert_unconfirmed(rig)
    assert rig.device.calls == []
    rig.controller.lock = threading.Lock()
    rig.parent.on = False
    rig.controller.toggleOn()
    assert rig.parent.on is False
    assert "DMMR acquisition disabled." in rig.logs


def test_successful_start_still_acquires_normally(rig):
    rig.device.fail_start = None
    start(rig)
    assert rig.parent.on is True
    assert rig.controller.acquiring is True
    assert rig.device.enabled is True
    assert "DMMR acquisition enabled." in rig.logs
