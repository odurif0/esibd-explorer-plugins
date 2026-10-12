"""AMX HD CH0–CH3 summaries follow live 4EDH routing, never config names or timer numbers.

Ground truth: CGC's COM-HVAMX4EDH-Control 1-20 signal tables and its 4EDH test
configuration file (config_swA.cfg), whose slot names state the intended output,
e.g. "[Configuration273] Test Switch (Default Delay): 10kHz->SwitchSym+DIO0".
"""
from types import SimpleNamespace

import pytest

from support.amx import load_module, make_case
from support.amx import INVERT, OSC0, SWITCH_SYM, TIMER_OUT0, TIMER_RUN0, config273, routing, snapshot

STATIC_FLAGS = ["ST_ENABLE", "ST_ENB_SWITCH", "ST_CLRN"]  # Configurations 100-121: generators off.


@pytest.fixture(scope="module")
def hd():
    return load_module("amx_hd")


def test_signal_table_is_cgcs_4edh_table(hd):
    names = [name for name, _ in hd._HD_SIGNALS]
    assert len(names) == 64
    assert names[:4] == ["0", "SoftTrig0", "SoftTrig1", "Enb"]
    assert names[4] == "TimerOut00" and names[19] == "TimerOut15"
    assert names[20] == "TimerRun00" and names[35] == "TimerRun15"
    assert names[36:40] == ["Osc0", "Osc1", "Osc2", "Osc3"]
    assert names[40] == "Mapping0-Out0" and names[55] == "Mapping3-Out3"
    assert names[56] == "Din0" and names[63] == "Din7"
    assert hd._HD_CLOCK_SOURCES[hd._HD_CLOCK_100M] == "CLK100M"
    assert hd._hd_signal_text(TIMER_OUT0 | INVERT) == "inverted TimerOut00"
    assert hd._hd_signal_text(INVERT) == "1"
    assert hd._hd_signal_text(0x40) == "unknown" and hd._hd_signal_text(None) == "unknown"
    assert [hd._hd_timer_clock(t) for t in range(16)] == [0, 0, 1, 1, 2, 2, 3, 3] * 2


def test_config273_10khz_switch_sym(hd):
    rows = hd._amx_hd_output_rows(*config273())
    assert [r["channel"] for r in rows] == ["CH0", "CH1", "CH2", "CH3"]
    assert all(r["state"] == "Periodic" and r["frequency"] == "10 kHz" for r in rows)
    assert all(r["levels"] == "Vneg ↔ Vpos" and r["dwell"] == "50 / 50" for r in rows)
    assert [r["relation"] for r in rows] == ["Reference", "Opposite to CH0", "Same as CH0", "Opposite to CH0"]
    assert rows[0]["waveform"]["trigger_delay"] == {"rise": 0, "fall": 0}
    assert rows[0]["waveform"]["fine_delay"] == {"rise": 0, "fall": 0}
    assert "trigger = TimerOut00; enable = 1" in rows[0]["detail"]
    assert "trigger = inverted TimerOut00" in rows[1]["detail"]


def test_config272_quadrature_is_reported_from_registers(hd):
    # "1kHz->SwitchSym(0/90deg)": Timer00=7,49998 and Timer01=25001,49998, both on Osc0.
    rows = hd._amx_hd_output_rows(
        snapshot({0: (7, 49998, 0), 1: (25001, 49998, 0)}),
        routing([(TIMER_OUT0, INVERT), (TIMER_OUT0 | INVERT, INVERT), (5, INVERT), (5 | INVERT, INVERT)],
                periods=(99998, 0, 0, 0), triggers={0: OSC0, 1: OSC0}),
    )
    assert all(r["frequency"] == "1 kHz" and r["dwell"] == "500 / 500" for r in rows)
    assert [r["relation"] for r in rows] == [
        "Reference", "Opposite to CH0", "+249.94 µs vs CH0", "+749.94 µs vs CH0"]


@pytest.mark.parametrize(("name", "switch0", "expected"), [
    ("[Configuration110] Out0=Vneg", (0, INVERT), ("Static", "Vneg")),
    ("[Configuration111] Out0=Vpos", (INVERT, INVERT), ("Static", "Vpos")),
    ("[Configuration100] Out0-3=Hi-Z", (0, 0), ("Hi-Z", "Hi-Z")),
])


def test_static_test_configurations(hd, name, switch0, expected):
    rows = hd._amx_hd_output_rows(snapshot(flags=STATIC_FLAGS), routing([switch0] + [(0, 0)] * 3, triggers={}))
    assert (rows[0]["state"], rows[0]["levels"]) == expected, name
    assert rows[0]["frequency"] == "—"
    assert all(r["state"] == "Hi-Z" and "Hi-Z does not imply 0 V" in r["detail"] for r in rows[1:])


def test_config290_burst_gated_by_running_signal_is_not_a_frequency(hd):
    # "10x1MHz->SwitchSym, Pause->Vneg (Frep=100Hz)": Timer00=48,48,Osc0,Pos,9; enables TimerRun00.
    state = snapshot({0: (48, 48, 9)})
    gated = routing([(TIMER_OUT0, TIMER_RUN0), (TIMER_OUT0 | INVERT, TIMER_RUN0)] * 2, periods=(999998, 0, 0, 0))
    rows = hd._amx_hd_output_rows(state, gated)
    assert all(r["state"] == "Gated" and r["frequency"] == "Unknown" for r in rows)
    assert "Timer #0 running" in rows[0]["detail"]
    always = routing(SWITCH_SYM, periods=(999998, 0, 0, 0))
    row = hd._amx_hd_output_rows(state, always)[0]
    assert row["state"] == "Burst" and "bursts of 10 pulses" in row["detail"]


def test_config298_pll_clock_is_not_assumed_100mhz(hd):
    # "HF cont.+Tuning": Clock0=PLL0 (10..100 MHz), Oscillator0=18, Timer00=1,8,Osc0.
    rows = hd._amx_hd_output_rows(snapshot({0: (1, 8, 0)}), routing(SWITCH_SYM, clocks=(4, 0, 0, 0), periods=(18, 0, 0, 0)))
    assert all(r["state"] == "Unknown" and r["frequency"] == "Unknown" for r in rows)
    assert "Clock 0 uses PLL0" in rows[0]["detail"]


@pytest.mark.parametrize(("clock1", "expected"), [(3, "Periodic"), (0, "Unknown"), (3 | INVERT, "Unknown")])
def test_timer_clock_must_match_its_trigger_oscillator_clock(hd, clock1, expected):
    # Timer 2 runs on Clock1 (CGC "Test Timer2,3 Burst" pairs it with Clock1/Osc1).
    state = snapshot({2: (7, 4998, 0)})
    row = hd._amx_hd_output_rows(state, routing([(6, INVERT)] * 4, clocks=(3, clock1, 0, 0), triggers={2: OSC0}))[0]
    assert row["state"] == expected
    if expected == "Periodic":
        assert row["frequency"] == "10 kHz"


@pytest.mark.parametrize(("case", "expected"), [
    ("external_switch", "External"), ("external_timer", "External"), ("external_enable", "Gated"),
    ("chained_timer", "Unknown"), ("software", "Software"), ("mapping", "Unknown"),
    ("stop_source", "Unknown"), ("missing_stop", "Unknown"), ("missing_burst", "Unknown"),
    ("missing_trigger", "Unknown"), ("invalid_source", "Unknown"), ("invalid_period", "Unknown"),
    ("missing_flags", "Unknown"), ("missing_switch", "Unknown"), ("skip_triggers", "Unknown"),
    ("not_enabled", "Not enabled"),
])


def test_non_simple_signals_never_inherit_an_oscillator_frequency(hd, case, expected):
    state, route = config273()
    timer, switch = state["pulsers"][0], route["switches"][0]
    if case == "external_switch": switch["trigger_source"] = 56
    elif case == "external_timer": route["timer_triggers"][0] = 57
    elif case == "external_enable": switch["enable_source"] = 56
    elif case == "chained_timer": route["timer_triggers"][0] = TIMER_RUN0 + 10
    elif case == "software": switch["trigger_source"] = 1
    elif case == "mapping": switch["trigger_source"] = 40
    elif case == "stop_source": route["timer_stops"][0] = 58
    elif case == "missing_stop": route["timer_stops"].pop(0)
    elif case == "missing_burst": timer["burst"] = None
    elif case == "missing_trigger": route["timer_triggers"].pop(0)
    elif case == "invalid_source": switch["trigger_source"] = 0x40
    elif case == "invalid_period": route["oscillator_periods"][0] = 0
    elif case == "missing_flags": state.pop("controller_state")
    elif case == "missing_switch": route["switches"].pop(0)
    elif case == "skip_triggers": timer["width_ticks"] = 9990
    elif case == "not_enabled": state["device_enabled"] = False
    row = hd._amx_hd_output_rows(state, route)[0]
    assert row["state"] == expected
    assert row["frequency"] in ("Unknown", "—") and row["relation"] == "—"
    assert "timing" not in row and "waveform" not in row
    if case == "mapping":
        assert "Mapping engine #0 output #0" in row["detail"]


@pytest.mark.parametrize("flag", ("ST_ENB_SWITCH", "ST_ENB_TIMER", "ST_ENB_OSC0", "ST_ENB_PULS_GEN", "ST_CLRN"))
def test_disabled_generators_are_not_shown_running(hd, flag):
    state, route = config273()
    state["controller_state"]["flags"].remove(flag)
    rows = hd._amx_hd_output_rows(state, route)
    assert all(r["state"] == ("Hi-Z" if flag == "ST_ENB_SWITCH" else "Static") for r in rows)
    assert all(r["frequency"] == "—" for r in rows)


@pytest.mark.parametrize(("pulse_gen", "expected"), [(True, "Vpos"), (False, "Vneg")])
def test_enb_signal_follows_the_pulse_generator_enable(hd, pulse_gen, expected):
    # CGC "Test Pulse Generator Enable/Disable: Enb->DIO7".
    flags = STATIC_FLAGS + (["ST_ENB_PULS_GEN"] if pulse_gen else [])
    row = hd._amx_hd_output_rows(snapshot(flags=flags), routing([(3, INVERT)] * 4, triggers={}))[0]
    assert row["levels"] == expected


@pytest.mark.parametrize("delays", ((1, 0, 0, 0), (0, 0, 500, 0), (0, 0, 0, 1)))
def test_edge_delays_keep_frequency_but_not_an_exact_dwell(hd, delays):
    state, _ = config273()
    rows = hd._amx_hd_output_rows(state, routing(SWITCH_SYM, delays=delays))
    assert all(r["frequency"] == "10 kHz" and r["dwell"] == "Delay adjusted" for r in rows)
    assert all("timing" not in r and r["relation"] == "—" for r in rows)
    assert rows[0]["waveform"]["fine_delay"]["rise"] == delays[2]


@pytest.mark.parametrize("delays", ((16, 0, 0, 0), (0, 0, 512, 0), (None, 0, 0, 0), (0, 0, 0, None)))
def test_invalid_or_missing_edge_delays_publish_no_waveform(hd, delays):
    state, _ = config273()
    row = hd._amx_hd_output_rows(state, routing(SWITCH_SYM, delays=delays))[0]
    assert row["state"] == "Periodic" and row["dwell"] == "Delay adjusted"
    assert "waveform" not in row and "timing" not in row


class RoutingDevice:
    """Read-only HD getters with vendor (status, value...) tuples."""

    def __init__(self, switches=SWITCH_SYM, statuses=None, missing=()):
        self.switches, self.statuses, self.missing = switches, statuses or {}, set(missing)
        self.calls = []

    def __getattr__(self, name):
        if not name.startswith("get_") or name in self.missing:
            raise AttributeError(name)

        def getter(*args):
            self.calls.append((name, *args))
            status = self.statuses.get((name, *args), 0)
            if name == "get_hw_type":
                return (status, 0x1234, 64, 300, 8, 16, 4, 4, 4, 0)
            if name == "get_switch_delay":
                return (status, 0, 0)
            if name == "get_switch_trigger_source":
                return (status, self.switches[args[0]][0])
            if name == "get_switch_enable_source":
                return (status, self.switches[args[0]][1])
            if name == "get_clock_source":
                return (status, 3 if args[0] == 0 else 0)
            if name == "get_oscillator_period":
                return (status, 9998 if args[0] == 0 else 0)
            if name == "get_timer_trigger_source":
                return (status, OSC0)
            return (status, 0)

        return getter


def test_routing_is_read_only_and_limited_to_referenced_timers(hd):
    _, parent, controller = make_case("amx_hd")
    device = RoutingDevice(switches=[(TIMER_OUT0, INVERT), (TIMER_OUT0 + 10, INVERT),
                                     (TIMER_RUN0 + 6, INVERT), (TIMER_OUT0 | INVERT, INVERT)])
    route = controller._collect_hd_routing(device, snapshot())
    assert all(call[0].startswith("get_") for call in device.calls)
    # Channel timers 0-1 (make_case) plus TimerOut10 and TimerRun06 from the switches.
    assert sorted(route["timer_triggers"]) == [0, 1, 6, 10]
    assert sorted(route["timer_stops"]) == [0, 1, 6]  # Only the 8 burst timers have a stop source.
    assert route["clock_sources"] == [3, 0, 0, 0] and route["burst_timers"] == 8
    controller._collect_hd_routing(device, snapshot())
    assert [call[0] for call in device.calls].count("get_hw_type") == 1


def test_routing_vendor_errors_and_missing_getters_stay_unknown(hd):
    _, _, controller = make_case("amx_hd")
    device = RoutingDevice(statuses={("get_clock_source", 0): -10}, missing={"get_switch_rise_delay_fine"})
    route = controller._collect_hd_routing(device, snapshot())
    assert route["clock_sources"][0] is None
    assert route["switches"][0]["fine_delay"]["rise"] is None
    rows = hd._amx_hd_output_rows(snapshot({0: (7, 4998, 0)}), route)
    assert all(r["state"] == "Unknown" and "Clock 0 source" in r["detail"] for r in rows)


def test_routing_transport_errors_propagate(hd):
    _, _, controller = make_case("amx_hd")

    class Broken(RoutingDevice):
        def __getattr__(self, name):
            def fail(*args):
                raise RuntimeError("AMX native transport is unusable")
            return fail

    with pytest.raises(RuntimeError, match="unusable"):
        controller._collect_hd_routing(Broken(), snapshot())


def test_poll_reads_routing_only_while_running_and_at_its_cadence(hd, monkeypatch):
    _, parent, controller = make_case("amx_hd")
    device = RoutingDevice()
    clock = SimpleNamespace(now=100.)
    monkeypatch.setattr(hd.time, "monotonic", lambda: clock.now)
    stopped = dict(snapshot(), device_enabled=False)
    controller._refresh_hd_routing(device, stopped)
    assert controller.hd_routing is None and device.calls == []
    controller._refresh_hd_routing(device, snapshot())
    first = len(device.calls)
    assert first and controller.hd_routing is not None
    clock.now += hd._HD_ROUTING_REFRESH_S - 0.1
    controller._refresh_hd_routing(device, snapshot())
    assert len(device.calls) == first
    clock.now += 0.2
    controller._refresh_hd_routing(device, snapshot())
    assert len(device.calls) > first
    controller._refresh_hd_routing(device, stopped)
    assert controller.hd_routing is None


def test_snapshot_publishes_rows_only_with_routing(hd):
    _, parent, controller = make_case("amx_hd")
    state, route = config273()
    controller._apply_snapshot(state)
    assert controller.output_rows is None
    controller.hd_routing = route
    controller._apply_snapshot(state)
    assert [r["frequency"] for r in controller.output_rows] == ["10 kHz"] * 4
    assert controller.frequency_readback_khz == pytest.approx(10.)


def test_output_rows_invalidated_on_write_transition_poll_failure_and_dispose(hd):
    _, parent, controller = make_case("amx_hd")
    state, route = config273()

    def publish():
        controller.hd_routing = route
        controller._apply_snapshot(state)
        assert controller.output_rows is not None

    publish()
    controller.applyValue(parent.channels[0])
    assert controller.output_rows is None
    publish()
    controller.applyGlobalSettings()
    assert controller.output_rows is None
    publish()
    controller._begin_transition(True)
    assert controller.output_rows is None and controller.hd_routing is None
    controller._end_transition()
    publish()
    controller._poll_error("simulated read error", exc=None)
    assert controller.output_rows is None and controller.hd_routing is None
    publish()
    controller._dispose_device()
    assert controller.output_rows is None and controller.hd_routing is None


def test_mscan_offers_and_accepts_amx_hd(mscan_rig, hd):
    assert "AMX_HD" in mscan_rig.scan.AMX_PLUGINS
    mscan_rig.amx.name = mscan_rig.scan.amx_name = "AMX_HD"
    state, _ = config273()
    mscan_rig.amx.controller.output_rows = hd._amx_hd_output_rows(state, routing(SWITCH_SYM, delays=(3, 7, 120, 0)))
    mscan_rig.scan._plan = mscan_rig.scan._preflight()
    assert not mscan_rig.psus[0].writes  # preflight is read-only
    mscan_rig.scan.runScan(lambda: True)
    assert mscan_rig.scan._validation["status"] == "completed", mscan_rig.scan._validation
    rows = mscan_rig.scan._plan["metadata"]["waveform"]
    assert rows[0]["waveform"]["fine_delay"] == {"rise": 120, "fall": 0}


def test_mscan_refuses_an_undecoded_amx_hd_output(mscan_rig, hd):
    mscan_rig.amx.name = mscan_rig.scan.amx_name = "AMX_HD"
    state, route = config273()
    route["switches"][0]["trigger_source"] = 40  # mapping engine output
    mscan_rig.amx.controller.output_rows = hd._amx_hd_output_rows(state, route)
    with pytest.raises(mscan_rig.module.ScanError, match="AMX_HD CH0: Unknown") as error:
        mscan_rig.scan._prepare()
    assert "Mapping engine" in str(error.value)
    assert not mscan_rig.psus[0].writes
