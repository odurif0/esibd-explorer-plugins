"""ESI OFF must prove low, fresh ADC voltages before closing the port."""
from __future__ import annotations

import threading
import time

import pytest



def test_disconnect_checks_four_outputs_after_disable_and_before_close(esi_discharge_rig):
    reports = []
    original = dict(esi_discharge_rig.ranges)
    assert esi_discharge_rig.driver.disconnect(timeout_s=.5, on_discharge=reports.append) is True
    assert esi_discharge_rig.calls[0] == ("disable",)
    assert esi_discharge_rig.calls[-1] == ("close",)
    assert all(call != ("gate", True) for call in esi_discharge_rig.calls)
    assert esi_discharge_rig.ranges == original
    final = reports[-1]
    assert final["consecutive"] == 3
    assert final["limit_v"] == 1.0
    for address in (1, 2):
        assert final["modules"][address]["positive_v"] == .5
        assert final["modules"][address]["negative_v"] == -.5
        assert final["modules"][address]["measured_a"] == -2e-9
    assert not esi_discharge_rig.driver.connected
    assert not type(esi_discharge_rig.driver)._active_connections


def test_exact_voltage_boundary_is_inclusive(esi_discharge_rig):
    for address, negative in esi_discharge_rig.values:
        esi_discharge_rig.values[address, negative] = [-1. if negative else 1.]
    assert esi_discharge_rig.driver.disconnect(timeout_s=.5)


def test_default_disconnect_cannot_bypass_discharge(esi_discharge_rig):
    esi_discharge_rig.values[2, True] = [-100.0]
    with pytest.raises(RuntimeError, match="discharge|Discharge"):
        esi_discharge_rig.driver.disconnect(timeout_s=.5)
    assert ("close",) not in esi_discharge_rig.calls
    assert esi_discharge_rig.driver.connected
    assert type(esi_discharge_rig.driver)._active_connections[id(esi_discharge_rig.driver)]["owner"] is esi_discharge_rig.driver


@pytest.mark.parametrize("address,negative", [(1, False), (1, True), (2, False), (2, True)])
def test_one_high_polarity_blocks_off(esi_discharge_rig, address, negative):
    esi_discharge_rig.values[address, negative] = [-1.01 if negative else 1.01]
    with pytest.raises(RuntimeError, match="discharge|Discharge"):
        esi_discharge_rig.driver.disconnect(timeout_s=.5)
    assert ("close",) not in esi_discharge_rig.calls


def test_above_threshold_resets_consecutive_confirmations(esi_discharge_rig):
    reports = []
    injected = False

    def progress(report):
        nonlocal injected
        reports.append(report)
        if report["consecutive"] == 1 and not injected:
            esi_discharge_rig.values[2, True] = [12.]
            injected = True
        elif injected and report["consecutive"] == 0:
            esi_discharge_rig.values[2, True] = [.5]

    assert esi_discharge_rig.driver.disconnect(timeout_s=.5, on_discharge=progress)
    counts = [r["consecutive"] for r in reports]
    assert any(a > 0 and b == 0 for a, b in zip(counts, counts[1:])), counts
    assert reports[-1]["consecutive"] == 3


@pytest.mark.parametrize("reason", ["sticky", "never_ready"])
def test_cached_zero_or_missing_adc_updates_never_confirm_off(esi_discharge_rig, reason):
    setattr(esi_discharge_rig.state, reason, True)
    for key in esi_discharge_rig.values:
        esi_discharge_rig.values[key] = [0.0]
    with pytest.raises(RuntimeError, match="discharge|fresh|ADC"):
        esi_discharge_rig.driver.disconnect(timeout_s=.5)
    assert ("close",) not in esi_discharge_rig.calls
    assert esi_discharge_rig.driver.connected


@pytest.mark.parametrize("invalid", ["voltage", "current"])
def test_invalid_measurements_never_confirm_off(esi_discharge_rig, invalid):
    esi_discharge_rig.state.invalid = invalid
    with pytest.raises(RuntimeError, match="invalid|Invalid|ADC"):
        esi_discharge_rig.driver.disconnect(timeout_s=.5)
    assert ("close",) not in esi_discharge_rig.calls


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_voltages_never_confirm_off(esi_discharge_rig, value):
    esi_discharge_rig.values[1, False] = [value]
    with pytest.raises(RuntimeError, match="invalid|finite|ADC"):
        esi_discharge_rig.driver.disconnect(timeout_s=.5)
    assert ("close",) not in esi_discharge_rig.calls


def test_wrong_adc_polarity_cannot_count_as_the_other_output(esi_discharge_rig):
    esi_discharge_rig.state.mismatch = True
    with pytest.raises(RuntimeError, match="selection|polarity"):
        esi_discharge_rig.driver.disconnect(timeout_s=.5)
    assert ("close",) not in esi_discharge_rig.calls


def test_adc_command_error_retains_port(esi_discharge_rig):
    esi_discharge_rig.state.voltage_status = -12
    with pytest.raises(RuntimeError):
        esi_discharge_rig.driver.disconnect(timeout_s=.5)
    assert ("close",) not in esi_discharge_rig.calls
    assert esi_discharge_rig.driver.connected


@pytest.mark.parametrize("status", [-10, -12, -13])
def test_protocol_error_preserves_first_failure_without_unpurged_adc_cleanup(esi_discharge_rig, status):
    esi_discharge_rig.state.voltage_status = status
    with pytest.raises(RuntimeError) as error:
        esi_discharge_rig.driver.disconnect(timeout_s=.5)
    assert f"{status}" in str(error.value)
    assert "module 1" in str(error.value)
    assert "restore ADC selection" not in str(error.value)
    assert [call for call in esi_discharge_rig.calls if call[0] == "select"] == [("select", 1, False, True)]
    assert ("close",) not in esi_discharge_rig.calls
    assert esi_discharge_rig.driver._communication_error is not None
    assert esi_discharge_rig.driver.connected


def test_failed_adc_cleanup_cannot_hide_original_invalid_discharge(esi_discharge_rig, monkeypatch):
    esi_discharge_rig.state.invalid = "voltage"
    original = esi_discharge_rig.base.set_hv_supply_meas_ranges
    count = 0

    def select(self, *args):
        nonlocal count
        count += 1
        return -13 if count == 3 else original(self, *args)

    monkeypatch.setattr(esi_discharge_rig.base, "set_hv_supply_meas_ranges", select)
    with pytest.raises(RuntimeError) as error:
        esi_discharge_rig.driver.disconnect(timeout_s=.5)
    assert "invalid ADC voltage/current" in str(error.value)
    assert "ADC restoration also failed" in str(error.value)
    assert "restore ADC selection(1) failed: -13" in str(error.value)
    assert count == 3 and ("close",) not in esi_discharge_rig.calls


def test_current_has_no_arbitrary_threshold(esi_discharge_rig, monkeypatch):
    # Use the real fake's flag reset, only replace its measured value.
    old = esi_discharge_rig.base.get_hv_supply_output_current
    monkeypatch.setattr(esi_discharge_rig.base, "get_hv_supply_output_current",
                        lambda self, addr: (*old(self, addr)[:2], .02))
    assert esi_discharge_rig.driver.disconnect(timeout_s=.5)


@pytest.mark.parametrize("current", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_current_cannot_confirm_off(esi_discharge_rig, monkeypatch, current):
    old = esi_discharge_rig.base.get_hv_supply_output_current
    monkeypatch.setattr(esi_discharge_rig.base, "get_hv_supply_output_current",
                        lambda self, addr: (*old(self, addr)[:2], current))
    with pytest.raises(RuntimeError, match="invalid ADC"):
        esi_discharge_rig.driver.disconnect(timeout_s=.5)
    assert ("close",) not in esi_discharge_rig.calls


def test_final_sample_arriving_after_deadline_is_rejected(esi_discharge_rig, monkeypatch):
    old = esi_discharge_rig.base.get_hv_supply_output_current
    count = 0

    def slow_final_current(self, address):
        nonlocal count
        count += 1
        result = old(self, address)
        # Drain + new read, two modules, two polarities, three rounds.
        if count == 24:
            esi_discharge_rig.clock.sleep(esi_discharge_rig.driver.DISCHARGE_TIMEOUT_S)
        return result

    monkeypatch.setattr(esi_discharge_rig.base, "get_hv_supply_output_current", slow_final_current)
    with pytest.raises(RuntimeError, match="deadline"):
        esi_discharge_rig.driver.disconnect(timeout_s=.5)
    assert count == 24
    assert ("close",) not in esi_discharge_rig.calls


def test_close_failure_after_discharge_keeps_connection_for_retry(esi_discharge_rig):
    esi_discharge_rig.state.close_status = -12
    with pytest.raises(RuntimeError, match="close_port"):
        esi_discharge_rig.driver.disconnect(timeout_s=.5)
    assert esi_discharge_rig.driver.connected
    assert type(esi_discharge_rig.driver)._active_connections[id(esi_discharge_rig.driver)]["owner"] is esi_discharge_rig.driver
    esi_discharge_rig.state.close_status = 0
    assert esi_discharge_rig.driver.disconnect(timeout_s=.5)


def test_failed_disable_never_starts_adc_check_or_closes(esi_discharge_rig):
    def fail(**kwargs):
        raise RuntimeError("disable unconfirmed")
    esi_discharge_rig.driver.force_safe_off = fail
    with pytest.raises(RuntimeError, match="disable unconfirmed"):
        esi_discharge_rig.driver.disconnect(timeout_s=.5)
    assert esi_discharge_rig.calls == []


def test_timeout_while_restoring_mux_stops_remaining_cleanup(esi_discharge_rig, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    old = esi_discharge_rig.base.set_hv_supply_meas_ranges
    count = 0
    workers = []

    def blocked_restore(self, address, negative, high_current):
        nonlocal count
        count += 1
        result = old(self, address, negative, high_current)
        if count == 3:  # two initial selections, then first cleanup selection
            workers.append(threading.current_thread())
            entered.set()
            assert release.wait(2)
        return result

    esi_discharge_rig.driver.DISCHARGE_TIMEOUT_S = .03
    monkeypatch.setattr(esi_discharge_rig.base, "set_hv_supply_meas_ranges", blocked_restore)
    try:
        with pytest.raises(RuntimeError, match="timed out") as error:
            esi_discharge_rig.driver.disconnect(timeout_s=.03)
        assert "last discharge operation: restore ADC selection(1)" in str(error.value)
        assert entered.is_set()
        assert esi_discharge_rig.driver._transport_poisoned
        assert ("close",) not in esi_discharge_rig.calls
    finally:
        release.set()
        for worker in workers:
            worker.join(2)
            assert not worker.is_alive()
    assert count == 3, "A timeout during cleanup must prevent further DLL calls"
    assert ("close",) not in esi_discharge_rig.calls


def test_poisoned_adc_call_does_not_restore_mux_or_close_concurrently(esi_discharge_rig, monkeypatch):
    monkeypatch.setattr(esi_discharge_rig.module, "time", time)
    monkeypatch.setattr(esi_discharge_rig.driver, "DISCHARGE_TIMEOUT_S", .03, raising=False)
    entered, release = threading.Event(), threading.Event()

    def blocked(self, address):
        entered.set()
        release.wait(2)
        return 0, True, 0.0

    monkeypatch.setattr(esi_discharge_rig.base, "get_hv_supply_output_voltage", blocked)
    try:
        with pytest.raises(RuntimeError, match="timed out"):
            esi_discharge_rig.driver.disconnect(timeout_s=.03)
        assert entered.is_set()
        assert esi_discharge_rig.driver._transport_poisoned
        before = list(esi_discharge_rig.calls)
        assert ("close",) not in before
    finally:
        release.set()
    time.sleep(.1)
    assert esi_discharge_rig.calls == before, "No further DLL calls after the poisoned worker returns"
    assert type(esi_discharge_rig.driver)._active_connections[id(esi_discharge_rig.driver)]["owner"] is esi_discharge_rig.driver
