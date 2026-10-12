"""ESI discharge rig shared by the discharge tests."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from support.esi_driver import RUNTIME_NAME, make_controller, load_runtime


class Clock:
    now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


@pytest.fixture
def esi_discharge_rig(monkeypatch):
    import sys
    load_runtime()
    module = sys.modules[f"{RUNTIME_NAME}.esi.esi"]
    base = sys.modules[f"{RUNTIME_NAME}.esi.esi_base"].ESIBase
    driver = make_controller(module)
    driver.connected = True
    driver._set_port_claimed(True)
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
    type(driver)._active_connections.clear()
