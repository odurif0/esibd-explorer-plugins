"""ESI OFF must prove low, fresh ADC voltages before closing the port."""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from test_esi_driver_behavior import _controller, _load_runtime, RUNTIME_NAME


class Clock:
    now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


@pytest.fixture
def rig(monkeypatch):
    import sys
    _load_runtime()
    module = sys.modules[f"{RUNTIME_NAME}.esi.esi"]
    base = sys.modules[f"{RUNTIME_NAME}.esi.esi_base"].ESIBase
    driver = _controller(module)
    driver.connected = True
    type(driver)._connected_instance = driver
    clock = Clock()
    monkeypatch.setattr(module, "time", clock, raising=False)
    calls = []
    ranges = {1: (True, True), 2: (False, False)}
    last_read = {(a, kind): -1.0 for a in (1, 2) for kind in ("v", "i")}
    counts = {(a, n): 0 for a in (1, 2) for n in (False, True)}
    values = {(a, n): [(-1 if n else 1) * .5] for a, n in counts}
    state = SimpleNamespace(sticky=False, never_ready=False, invalid=None,
                            mismatch=False, voltage_status=0, close_status=0)

    def disable(**kwargs):
        calls.append(("disable",))
        return True

    def select(self, address, negative, high):
        calls.append(("select", address, negative, high))
        ranges[address] = bool(negative), bool(high)
        return 0

    def selection(self, address):
        negative, high = ranges[address]
        return 0, not negative if state.mismatch else negative, high

    def flags(self, address):
        calls.append(("flags", address))
        if state.never_ready:
            return 0, 0
        if state.sticky:
            return 0, 0x50
        return 0, sum(bit for kind, bit in (("v", 0x10), ("i", 0x40))
                      if clock.now - last_read[address, kind] >= .05)

    def voltage(self, address):
        negative, _ = ranges[address]
        fresh = clock.now - last_read[address, "v"] >= .05
        last_read[address, "v"] = clock.now
        key = address, negative
        count = counts[key]
        plan = values[key]
        value = plan[min(count, len(plan) - 1)]
        if fresh:
            counts[key] += 1
        # Only the ready/clear handshake can distinguish old from fresh samples.
        calls.append(("voltage", address, negative, fresh, value))
        return state.voltage_status, state.invalid != "voltage", value

    def current(self, address):
        calls.append(("current", address))
        last_read[address, "i"] = clock.now
        return 0, state.invalid != "current", -2e-9

    def close(self):
        calls.append(("close",))
        return state.close_status

    driver.force_safe_off = disable
    driver.set_global_active = lambda active, **kw: calls.append(("gate", active)) or active
    monkeypatch.setattr(base, "set_hv_supply_meas_ranges", select)
    monkeypatch.setattr(base, "get_hv_supply_meas_ranges", selection)
    monkeypatch.setattr(base, "get_module_data_ready_flags", flags)
    monkeypatch.setattr(base, "get_hv_supply_output_voltage", voltage)
    monkeypatch.setattr(base, "get_hv_supply_output_current", current)
    monkeypatch.setattr(base, "close_port", close)
    yield SimpleNamespace(driver=driver, module=module, base=base, clock=clock,
                          calls=calls, ranges=ranges, values=values, counts=counts, state=state)
    type(driver)._connected_instance = None


def test_disconnect_checks_four_outputs_after_disable_and_before_close(rig):
    reports = []
    original = dict(rig.ranges)
    assert rig.driver.disconnect(timeout_s=.5, on_discharge=reports.append) is True
    assert rig.calls[0] == ("disable",)
    assert rig.calls[-1] == ("close",)
    assert all(call != ("gate", True) for call in rig.calls)
    assert rig.ranges == original
    final = reports[-1]
    assert final["consecutive"] == 3
    assert final["limit_v"] == 1.0
    for address in (1, 2):
        assert final["modules"][address]["positive_v"] == .5
        assert final["modules"][address]["negative_v"] == -.5
        assert final["modules"][address]["measured_a"] == -2e-9
    assert not rig.driver.connected
    assert type(rig.driver)._connected_instance is None


def test_exact_voltage_boundary_is_inclusive(rig):
    for address, negative in rig.values:
        rig.values[address, negative] = [-1. if negative else 1.]
    assert rig.driver.disconnect(timeout_s=.5)


def test_default_disconnect_cannot_bypass_discharge(rig):
    rig.values[2, True] = [-100.0]
    with pytest.raises(RuntimeError, match="discharge|Discharge"):
        rig.driver.disconnect(timeout_s=.5)
    assert ("close",) not in rig.calls
    assert rig.driver.connected
    assert type(rig.driver)._connected_instance is rig.driver


@pytest.mark.parametrize("address,negative", [(1, False), (1, True), (2, False), (2, True)])
def test_one_high_polarity_blocks_off(rig, address, negative):
    rig.values[address, negative] = [-1.01 if negative else 1.01]
    with pytest.raises(RuntimeError, match="discharge|Discharge"):
        rig.driver.disconnect(timeout_s=.5)
    assert ("close",) not in rig.calls


def test_above_threshold_resets_consecutive_confirmations(rig):
    reports = []
    injected = False

    def progress(report):
        nonlocal injected
        reports.append(report)
        if report["consecutive"] == 1 and not injected:
            rig.values[2, True] = [12.]
            injected = True
        elif injected and report["consecutive"] == 0:
            rig.values[2, True] = [.5]

    assert rig.driver.disconnect(timeout_s=.5, on_discharge=progress)
    counts = [r["consecutive"] for r in reports]
    assert any(a > 0 and b == 0 for a, b in zip(counts, counts[1:])), counts
    assert reports[-1]["consecutive"] == 3


@pytest.mark.parametrize("reason", ["sticky", "never_ready"])
def test_cached_zero_or_missing_adc_updates_never_confirm_off(rig, reason):
    setattr(rig.state, reason, True)
    for key in rig.values:
        rig.values[key] = [0.0]
    with pytest.raises(RuntimeError, match="discharge|fresh|ADC"):
        rig.driver.disconnect(timeout_s=.5)
    assert ("close",) not in rig.calls
    assert rig.driver.connected


@pytest.mark.parametrize("invalid", ["voltage", "current"])
def test_invalid_measurements_never_confirm_off(rig, invalid):
    rig.state.invalid = invalid
    with pytest.raises(RuntimeError, match="invalid|Invalid|ADC"):
        rig.driver.disconnect(timeout_s=.5)
    assert ("close",) not in rig.calls


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_voltages_never_confirm_off(rig, value):
    rig.values[1, False] = [value]
    with pytest.raises(RuntimeError, match="invalid|finite|ADC"):
        rig.driver.disconnect(timeout_s=.5)
    assert ("close",) not in rig.calls


def test_wrong_adc_polarity_cannot_count_as_the_other_output(rig):
    rig.state.mismatch = True
    with pytest.raises(RuntimeError, match="selection|polarity"):
        rig.driver.disconnect(timeout_s=.5)
    assert ("close",) not in rig.calls


def test_adc_command_error_retains_port(rig):
    rig.state.voltage_status = -12
    with pytest.raises(RuntimeError):
        rig.driver.disconnect(timeout_s=.5)
    assert ("close",) not in rig.calls
    assert rig.driver.connected


def test_current_has_no_arbitrary_threshold(rig, monkeypatch):
    # Use the real fake's flag reset, only replace its measured value.
    old = rig.base.get_hv_supply_output_current
    monkeypatch.setattr(rig.base, "get_hv_supply_output_current",
                        lambda self, addr: (*old(self, addr)[:2], .02))
    assert rig.driver.disconnect(timeout_s=.5)


@pytest.mark.parametrize("current", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_current_cannot_confirm_off(rig, monkeypatch, current):
    old = rig.base.get_hv_supply_output_current
    monkeypatch.setattr(rig.base, "get_hv_supply_output_current",
                        lambda self, addr: (*old(self, addr)[:2], current))
    with pytest.raises(RuntimeError, match="invalid ADC"):
        rig.driver.disconnect(timeout_s=.5)
    assert ("close",) not in rig.calls


def test_final_sample_arriving_after_deadline_is_rejected(rig, monkeypatch):
    old = rig.base.get_hv_supply_output_current
    count = 0

    def slow_final_current(self, address):
        nonlocal count
        count += 1
        result = old(self, address)
        # Drain + new read, two modules, two polarities, three rounds.
        if count == 24:
            rig.clock.sleep(rig.driver.DISCHARGE_TIMEOUT_S)
        return result

    monkeypatch.setattr(rig.base, "get_hv_supply_output_current", slow_final_current)
    with pytest.raises(RuntimeError, match="deadline"):
        rig.driver.disconnect(timeout_s=.5)
    assert count == 24
    assert ("close",) not in rig.calls


def test_close_failure_after_discharge_keeps_connection_for_retry(rig):
    rig.state.close_status = -12
    with pytest.raises(RuntimeError, match="close_port"):
        rig.driver.disconnect(timeout_s=.5)
    assert rig.driver.connected
    assert type(rig.driver)._connected_instance is rig.driver
    rig.state.close_status = 0
    assert rig.driver.disconnect(timeout_s=.5)


def test_failed_disable_never_starts_adc_check_or_closes(rig):
    def fail(**kwargs):
        raise RuntimeError("disable unconfirmed")
    rig.driver.force_safe_off = fail
    with pytest.raises(RuntimeError, match="disable unconfirmed"):
        rig.driver.disconnect(timeout_s=.5)
    assert rig.calls == []


def test_timeout_while_restoring_mux_stops_remaining_cleanup(rig, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    old = rig.base.set_hv_supply_meas_ranges
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

    rig.driver.DISCHARGE_TIMEOUT_S = .03
    monkeypatch.setattr(rig.base, "set_hv_supply_meas_ranges", blocked_restore)
    try:
        with pytest.raises(RuntimeError, match="timed out"):
            rig.driver.disconnect(timeout_s=.03)
        assert entered.is_set()
        assert rig.driver._transport_poisoned
        assert ("close",) not in rig.calls
    finally:
        release.set()
        for worker in workers:
            worker.join(2)
            assert not worker.is_alive()
    assert count == 3, "A timeout during cleanup must prevent further DLL calls"
    assert ("close",) not in rig.calls


def test_poisoned_adc_call_does_not_restore_mux_or_close_concurrently(rig, monkeypatch):
    monkeypatch.setattr(rig.module, "time", time)
    monkeypatch.setattr(rig.driver, "DISCHARGE_TIMEOUT_S", .03, raising=False)
    entered, release = threading.Event(), threading.Event()

    def blocked(self, address):
        entered.set()
        release.wait(2)
        return 0, True, 0.0

    monkeypatch.setattr(rig.base, "get_hv_supply_output_voltage", blocked)
    try:
        with pytest.raises(RuntimeError, match="timed out"):
            rig.driver.disconnect(timeout_s=.03)
        assert entered.is_set()
        assert rig.driver._transport_poisoned
        before = list(rig.calls)
        assert ("close",) not in before
    finally:
        release.set()
    time.sleep(.1)
    assert rig.calls == before, "No further DLL calls after the poisoned worker returns"
    assert type(rig.driver)._connected_instance is rig.driver
