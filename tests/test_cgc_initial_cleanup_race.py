"""An initial cleanup must not become a bare Close if Open finishes meanwhile.

ESI verifies shutdown inside its runtime's normal disconnect; DMMR covers this
race in its own retry tests. Here the ten other controllers must leave a newly
connected backend to the initializer's verified shutdown path.
"""
import threading

import pytest

from test_cgc_initial_connection_recovery import SPECS, rig  # noqa: F401
from test_cgc_plugin_connection_recovery import plugin  # noqa: F401


INITIAL_CLEANUP_SPECS = [spec for spec in SPECS if spec[1] != "esi"]


@pytest.mark.parametrize("rig", INITIAL_CLEANUP_SPECS, indirect=True,
                         ids=[spec[0] for spec in INITIAL_CLEANUP_SPECS])
@pytest.mark.parametrize(("action", "completion_point"), [
    ("close", "cleanup"), ("shutdown", "cleanup"),
    ("close", "classification"), ("shutdown", "classification"),
    ("close", "before_close"),
])
@pytest.mark.parametrize("shutdown_confirmed", [True, False])
def test_successful_open_during_cleanup_requires_verified_shutdown(
    plugin, rig, monkeypatch, action, completion_point, shutdown_confirmed,
):
    c, hw = plugin.controller, rig.hw
    plugin.parent.connect_timeout_s = 2.0
    hw.open_release.clear()
    finished, proceed = threading.Event(), threading.Event()
    worker = threading.Thread(target=c.initializeCommunication, daemon=True)
    worker.start()
    try:
        assert hw.open_entered.wait(2)
        old = c.device
        finish = old._finish_initial_open

        def hold_finished_connect():
            finish()
            finished.set()
            assert proceed.wait(5)

        monkeypatch.setattr(old, "_finish_initial_open", hold_finished_connect)
        dispose = c._dispose_device
        classify = c._initial_open_incomplete

        def finish_connect():
            hw.open_release.set()
            assert finished.wait(2)
            assert old.connected and not old._opening_in_progress and not old._open_failed

        def finish_before_disposal(*args, **kwargs):
            # The outer path classified an unfinished Open; finish it before
            # disposal decides whether a raw disconnect is still permissible.
            finish_connect()
            return dispose(*args, **kwargs)

        def finish_after_classification(*args, **kwargs):
            incomplete = classify(*args, **kwargs)
            if incomplete:
                finish_connect()
            return incomplete

        if completion_point == "cleanup":
            monkeypatch.setattr(c, "_dispose_device", finish_before_disposal)
        elif completion_point == "classification":
            monkeypatch.setattr(c, "_initial_open_incomplete", finish_after_classification)
        else:
            # The native connection is ready but Explorer's initializer has not
            # yet consumed the pending close or published initialization success.
            finish_connect()
        if action == "shutdown":
            assert c.shutdownCommunication() is False
        else:
            c.closeCommunication()
        if rig.family == "psu":
            assert c._output_cancel.is_set()
        else:
            assert c._initial_open_close_requested
        assert c.device is old and old.connected and old._dll_port_claimed
        assert c.main_state == "Shutdown unconfirmed"
        assert hw.count("close") == hw.count("dispose") == 0
        assert not plugin.successes

        # Restore only the interleaving hook, keeping all native simulation
        # installed until the initializer and its pending stop have finished.
        monkeypatch.setattr(c, "_dispose_device", dispose)
        monkeypatch.setattr(c, "_initial_open_incomplete", classify)
        shutdown_calls = []

        def verified_shutdown():
            shutdown_calls.append(old)
            hw.invoke("verified_shutdown", old)
            if shutdown_confirmed:
                assert rig.disconnect(old) is True
            c.closeCommunication(final_state=("Disconnected" if shutdown_confirmed
                                              else "Shutdown unconfirmed"))
            return shutdown_confirmed

        monkeypatch.setattr(c, "shutdownCommunication", verified_shutdown)
        proceed.set()
        worker.join(3)
        assert not worker.is_alive()
        assert shutdown_calls == [old]
        assert not plugin.successes and len(plugin.created) == 1
        assert not c.initializing
        if shutdown_confirmed:
            assert c.device is None and not c.initialized
            assert c.main_state == "Disconnected"
            operations = [name for name, _ in hw.operations]
            assert operations.index("verified_shutdown") < operations.index("close")
            assert hw.count("close") == hw.count("dispose") == 1
        else:
            assert c.device is old and c.initialized and old._dll_port_claimed
            assert c.main_state == "Shutdown unconfirmed"
            assert hw.count("close") == hw.count("dispose") == 0
    finally:
        proceed.set()
        hw.open_release.set()
        worker.join(3)
        assert not worker.is_alive()


@pytest.mark.parametrize("rig", INITIAL_CLEANUP_SPECS, indirect=True,
                         ids=[spec[0] for spec in INITIAL_CLEANUP_SPECS])
@pytest.mark.parametrize(("state", "forced_state"), [
    ("Initializing", None), ("Disconnected", None),
    ("Shutdown unconfirmed", None), ("Shutdown unconfirmed", "Communication lost"),
])
def test_plain_close_keeps_backend_without_verified_stop(
    plugin, rig, monkeypatch, state, forced_state,
):
    c, hw = plugin.controller, rig.hw
    hw.open_release.set()
    old = rig.new()
    assert old.connect(timeout_s=1.0)
    c.device, c.initialized, c.initializing = old, False, False
    c.main_state, c._forced_close_state = state, forced_state
    before = list(hw.operations)
    base_closes = []
    monkeypatch.setattr(plugin.module.DeviceController, "closeCommunication",
                        lambda self: base_closes.append(True), raising=False)

    c.closeCommunication()

    assert hw.operations == before and not base_closes
    assert c.device is old and old._dll_port_claimed
    assert c.main_state == "Shutdown unconfirmed" and c.initialized
    assert not c.acquiring
    if rig.family == "psu":
        assert c._output_cancel.is_set()
    else:
        assert c._initial_open_close_requested
    c.initComplete()  # A queued success must not undo the Close request.
    assert hw.operations == before and c.device is old and not plugin.successes
