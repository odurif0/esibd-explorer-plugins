"""Overview (logic without Qt): device order and lights, All ON / All OFF, channels followed by address, cards.

User request (2026-11-22): one page with the essential parameters, one card per chamber, and a general ON/OFF. v1
reads only; All ON / All OFF press each device's own ON button, one after the other. The fakes behave like the
device plugins as the Overview sees them: an ON button (onAction), a controller with its flags and state text.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
import re
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _module():
    spec = importlib.util.spec_from_file_location("_test_overview_runtime", ROOT / "overview" / "_runtime" / "_overview.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # A dataclass looks its module up while it is defined.
    spec.loader.exec_module(module)
    return module


ov = _module()


class Clock:
    now = 0.0


class Action:
    """Explorer's StateAction as a click sees it: trigger() toggles the state, then calls setOn(None)."""

    def __init__(self, device):
        self.device, self.state, self.enabled = device, False, True

    def trigger(self):
        if self.enabled:
            self.state = not self.state
            self.device.setOn(None)


class Controller:
    def __init__(self):
        self.initialized = self.initializing = self.transitioning = self.ramping = False
        self.main_state = "Disconnected"


class Device:
    """A device plugin: ON connects after ``start_s``, OFF ends after ``stop_s``; ``fail`` is the state of a failed ON."""

    useOnOffLogic = True

    def __init__(self, name, clock, presses, start_s=2.0, stop_s=1.0, fail="", ignore_off=0):
        self.name, self.clock, self.presses = name, clock, presses
        self.controller, self.onAction = Controller(), Action(self)
        self.start_s, self.stop_s, self.fail, self.ignore_off = start_s, stop_s, fail, ignore_off
        self.until = None
        self.recording = False
        self.channels = []

    def setOn(self, on=None):
        target = self.onAction.state if on is None else on
        self.presses.append((self.name, target, self.clock.now))
        if not target and self.ignore_off:  # DMMR during a transition: ignored, button restored.
            self.ignore_off -= 1
            self.onAction.state = True
            return
        if target:
            self.controller.initializing, self.controller.main_state = True, "Connecting"
        else:
            self.controller.transitioning = True
        self.until = (target, self.clock.now + (self.start_s if target else self.stop_s))

    def advance(self):
        if self.until is None or self.clock.now < self.until[1]:
            return
        target, self.until = self.until[0], None
        controller = self.controller
        controller.initializing = controller.transitioning = False
        if target and self.fail:
            controller.main_state = self.fail
        elif target:
            controller.initialized, controller.main_state = True, "ST_ON"
        else:
            controller.initialized, controller.main_state, self.recording = False, "Disconnected", False

    def toggleRecording(self, on=None, manual=True):
        self.recording = bool(on) and self.controller.initialized

    def getChannels(self):
        return self.channels


def lab(clock, presses, **special):
    names = ("ESI", "AMX_HD", "PSU_A", "PSU_B", "AMPR_A", "DMMR", "TPG366")
    return [Device(name, clock, presses, **special.get(name, {})) for name in names]


def run(sequence, devices, clock, limit_s=900.0, dt=0.25):
    while not sequence.finished and clock.now < limit_s:
        clock.now += dt
        for device in devices:
            device.advance()
        sequence.tick(clock.now)
    return sequence


# ---------------------------------------------------------------------------------------------------- devices
def test_devices_are_turned_on_in_beam_order_and_off_the_other_way():
    clock, presses = Clock(), []
    devices = lab(clock, presses)
    other = Device("Temperature", clock, presses)  # An Explorer device the Overview does not manage.
    no_button = Device("PSU_C", clock, presses)
    no_button.useOnOffLogic = False
    order_on = [d.name for d in ov.managed([*devices, other, no_button], True)]
    assert order_on == ["TPG366", "DMMR", "AMPR_A", "PSU_A", "PSU_B", "AMX_HD", "ESI"]
    assert [d.name for d in ov.managed(devices, False)] == ["ESI", "AMX_HD", "PSU_A", "PSU_B", "AMPR_A", "DMMR", "TPG366"]
    assert [ov.kind(n) for n in ("AMX_HD", "AMX_A", "PSU_E", "AMPR_B", "ESI", "TPG366", "DMMR", "MScan", "ESIX")] == [
        "amx", "amx", "psu", "ampr", "esi", "tpg366", "dmmr", None, None]


def test_lights_show_what_the_plugin_reports():
    clock, presses = Clock(), []
    device = Device("AMPR_A", clock, presses)
    assert ov.light(device) == ("off", "OFF")
    device.onAction.trigger()
    assert ov.light(device) == ("busy", "Starting")
    clock.now = 5
    device.advance()
    assert ov.light(device) == ("on", "ON")
    device.controller.ramping = True
    assert ov.light(device) == ("busy", "Ramping")
    device.controller.ramping = False
    for state in ("Communication lost", "Shutdown unconfirmed", "State error", "Connection pending", "ST_ERR_ILOCK"):
        device.controller.main_state = state
        assert ov.light(device) == ("fault", state)
    esi = Device("ESI", clock, presses)
    esi.controller.main_state = ov.ESI_STOPPING
    assert ov.light(esi) == ("busy", "Stopping: checking HV") and not ov.reached(esi, False)
    esi.controller._discharge_outputs_off = True  # Outputs verified OFF: the discharge check goes on.
    assert ov.light(esi) == ("busy", "OFF: HV discharging") and ov.reached(esi, False)


# ---------------------------------------------------------------------------------------------------- All ON
def test_all_on_presses_each_button_in_beam_order_once_the_previous_device_is_on():
    clock, presses, lines = Clock(), [], []
    devices = lab(clock, presses)
    devices[4].controller.initialized = True  # AMPR_A connected but OFF: pressed like the others.
    sequence = run(ov.Sequence(True, devices, clock.now, log=lines.append), devices, clock)
    assert sequence.ok and [p[:2] for p in presses] == [(n, True) for n in
                                                        ("TPG366", "DMMR", "AMPR_A", "PSU_A", "PSU_B", "AMX_HD", "ESI")]
    times = [p[2] for p in presses]
    assert all(later - earlier >= 2.0 for earlier, later in zip(times, times[1:]))  # Each waited for the previous.
    assert all(d.recording for d in devices)  # Recording started once ON (refused by a plugin until then).
    assert sequence.summary.startswith("All ON finished. turned ON: TPG366, DMMR, AMPR_A")
    assert lines[-1] == sequence.summary


def test_all_on_stops_at_the_first_failure_and_presses_nothing_after_it():
    clock, presses = Clock(), []
    devices = lab(clock, presses, AMPR_A={"fail": "Connection pending"})
    sequence = run(ov.Sequence(True, devices, clock.now), devices, clock)
    assert not sequence.ok
    assert [p[0] for p in presses] == ["TPG366", "DMMR", "AMPR_A"]
    states = {s.name: (s.state, s.note) for s in sequence.steps}
    assert states["AMPR_A"] == ("failed", "Connection pending")
    assert {states[n][0] for n in ("PSU_A", "PSU_B", "AMX_HD", "ESI")} == {"not started"}
    assert "Failed: AMPR_A: Connection pending." in sequence.summary


def test_all_on_skips_a_device_already_on_and_waits_for_a_busy_one():
    clock, presses = Clock(), []
    devices = lab(clock, presses)
    tpg, dmmr = devices[6], devices[5]
    tpg.onAction.state, tpg.controller.initialized = True, True
    dmmr.controller.transitioning = True  # Busy: pressing now would be ignored.
    sequence = ov.Sequence(True, devices, clock.now)
    for _ in range(8):
        clock.now += 0.25
        sequence.tick(clock.now)
    assert presses == [] and [step.name for step in sequence.pending()] == ["DMMR"]
    dmmr.controller.transitioning = False
    run(sequence, devices, clock)
    assert sequence.ok and presses[0][0] == "DMMR"
    assert sequence.steps[0].state == "skipped" and sequence.steps[0].note == "already ON"


def test_a_device_that_never_connects_fails_after_the_timeout():
    clock, presses = Clock(), []
    devices = [Device("AMPR_A", clock, presses, start_s=1e9), Device("PSU_A", clock, presses)]
    sequence = run(ov.Sequence(True, devices, clock.now), devices, clock)
    assert sequence.steps[0].state == "failed" and sequence.steps[0].note.startswith("not ON after 180 s (Starting)")
    assert 180 <= clock.now < 182 and [p[0] for p in presses] == ["AMPR_A"]


def test_an_on_button_that_goes_back_off_is_a_failure():
    clock, presses = Clock(), []
    device = Device("PSU_A", clock, presses)
    device.setOn = lambda on=None: setattr(device.onAction, "state", False)  # The plugin refuses the ON.
    sequence = run(ov.Sequence(True, [device], clock.now), [device], clock)
    assert sequence.steps[0].state == "failed" and sequence.steps[0].note == "its ON button went back OFF"


# ---------------------------------------------------------------------------------------------------- All OFF
def _all_on(devices):
    for device in devices:
        device.onAction.state = True
        device.controller.initialized, device.controller.main_state = True, "ST_ON"


def test_all_off_stops_scans_first_then_esi_rf_dc_and_the_readers_last():
    clock, presses, calls = Clock(), [], []
    devices = lab(clock, presses)
    _all_on(devices)
    running = {"MScan map": 3.0}

    def stop():
        calls.append(("stop", clock.now))

    def activity():
        return ", ".join(name for name, until in running.items() if clock.now < until)
    sequence = run(ov.Sequence(False, devices, clock.now, stop_activity=stop, activity=activity), devices, clock)
    assert sequence.ok and calls == [("stop", 0.25)]
    assert [p[:2] for p in presses] == [(n, False) for n in ("ESI", "AMX_HD", "PSU_A", "PSU_B", "AMPR_A", "DMMR", "TPG366")]
    supplies, readers = {p[2] for p in presses[:5]}, {p[2] for p in presses[5:]}
    assert len(supplies) == 1 and min(supplies) >= 3.0  # All at once (user, 2026-11-22), once the map had stopped.
    assert len(readers) == 1 and min(readers) >= min(supplies) + 1.0  # The readers once the supplies are OFF.
    assert all(not d.onAction.state for d in devices)


def test_all_off_goes_on_after_a_failure_and_presses_again_an_ignored_off():
    clock, presses = Clock(), []
    devices = lab(clock, presses, DMMR={"ignore_off": 1})
    _all_on(devices)
    ampr = devices[4]
    original = ampr.advance

    def unconfirmed():
        original()
        if not ampr.onAction.state and ampr.until is None:
            ampr.controller.main_state = "Shutdown unconfirmed"
    ampr.advance = unconfirmed
    sequence = run(ov.Sequence(False, devices, clock.now), devices, clock)
    states = {s.name: (s.state, s.note) for s in sequence.steps}
    assert states["AMPR_A"] == ("failed", "Shutdown unconfirmed")
    assert states["DMMR"][0] == states["TPG366"][0] == "done"
    assert [p[1] for p in presses if p[0] == "DMMR"] == [False, False]  # Ignored once (transition), pressed again.
    assert not sequence.ok and "Failed: AMPR_A: Shutdown unconfirmed." in sequence.summary


def test_a_hung_device_never_holds_the_others_at_all_off():
    clock, presses = Clock(), []
    devices = lab(clock, presses, ESI={"stop_s": 1e9})  # Stays in its OFF transition.
    _all_on(devices)
    sequence = run(ov.Sequence(False, devices, clock.now), devices, clock)
    states = {s.name: s.state for s in sequence.steps}
    assert states == {"ESI": "failed", "AMX_HD": "done", "PSU_A": "done", "PSU_B": "done", "AMPR_A": "done",
                      "DMMR": "done", "TPG366": "done"}
    off = {d.name: d for d in devices}
    pressed = {p[0]: p[2] for p in presses}
    assert len({pressed[n] for n in ("ESI", "AMX_HD", "PSU_A", "PSU_B", "AMPR_A")}) == 1 and pressed["ESI"] < 1
    assert pressed["DMMR"] > 180  # The readers watched until the ESI was given up.
    assert not any(off[n].onAction.state or off[n].controller.initialized for n in ("AMX_HD", "PSU_A", "AMPR_A"))
    assert sequence.steps[0].note.startswith("not OFF after 180 s")


def test_all_off_moves_on_while_the_esi_discharge_check_goes_on():
    clock, presses = Clock(), []
    esi, amx = Device("ESI", clock, presses), Device("AMX_HD", clock, presses)
    _all_on([esi, amx])

    def esi_off(on=None):
        presses.append(("ESI", esi.onAction.state, clock.now))
        esi.controller.main_state = ov.ESI_STOPPING
        esi.controller._discharge_outputs_off = True  # Outputs verified OFF; ~40 s of discharge check follow.
    esi.setOn = esi_off
    sequence = run(ov.Sequence(False, [esi, amx], clock.now), [esi, amx], clock)
    assert sequence.ok and [p[0] for p in presses] == ["ESI", "AMX_HD"] and presses[1][2] < 2
    assert ov.light(esi) == ("busy", "OFF: HV discharging")


def test_scans_still_running_after_5_s_do_not_hold_all_off():
    clock, presses, lines = Clock(), [], []
    devices = [Device("AMPR_A", clock, presses)]
    _all_on(devices)
    sequence = run(ov.Sequence(False, devices, clock.now, stop_activity=lambda: None, activity=lambda: "MScan scan",
                               log=lines.append), devices, clock)
    assert sequence.ok and 5 <= presses[0][2] < 6
    assert "All OFF: MScan scan still running after 5 s; the devices go OFF anyway." in lines


def test_all_off_cancels_a_running_all_on():
    clock, presses = Clock(), []
    devices = lab(clock, presses)
    sequence = ov.Sequence(True, devices, clock.now)
    for _ in range(12):  # TPG366 ON, DMMR being turned ON.
        clock.now += 0.25
        for device in devices:
            device.advance()
        sequence.tick(clock.now)
    sequence.cancel("All OFF pressed")
    assert sequence.finished and not sequence.ok
    assert {s.name: s.state for s in sequence.steps}["AMPR_A"] == "cancelled"
    assert "All ON cancelled (All OFF pressed)" in sequence.summary
    off = run(ov.Sequence(False, devices, clock.now), devices, clock)
    assert off.ok and not any(d.onAction.state for d in devices)


# ---------------------------------------------------------------------------------------------------- channels and cards
class Channel:
    def __init__(self, device, name, value=0.0, monitor=None, unit="V", module=None, number=None, **extra):
        self.device, self.name, self.value, self.monitor, self.unit = device, name, value, monitor, unit
        self.useMonitors = monitor is not None
        self.real = True
        if module is not None:
            self.module_address = lambda: module
        if number is not None:
            self.channel_number = lambda: number
        for key, item in extra.items():
            setattr(self, key, item)
        device.channels.append(self)

    def getDevice(self):
        return self.device


def connected_device(name, clock=None, **attributes):
    device = Device(name, clock or Clock(), [])
    _all_on([device])
    for key, value in attributes.items():
        setattr(device, key, value)
    return device


def test_a_renamed_channel_is_followed_by_its_output_and_never_replaced_by_its_former_name():
    ampr = connected_device("AMPR_A")
    q1 = Channel(ampr, "Q1_DC", 36.0, 36.0, module=3, number=1)
    resolver = ov.Resolver()
    resolver.index(ampr.channels)
    assert resolver.find("Q1_DC") is q1 and resolver.bindings == {"Q1_DC": "AMPR_A M3 CH1"} and resolver.changed
    q1.name = "Q1 DC (rod)"
    other = Channel(ampr, "Q1_DC", 5.0, 5.0, module=4, number=2)  # Another output given the former name.
    resolver.index(ampr.channels)
    assert resolver.find("Q1_DC") is q1
    assert ov.address(other) == "AMPR_A M4 CH2"
    tpg = connected_device("TPG366")
    gauge = Channel(tpg, "P4", 2.8e-3, unit="mbar", input=4)
    esi = connected_device("ESI")
    current = Channel(esi, "ESI_HV1_I", 3.2e-8, unit="A", module=1, function="HV current")
    assert (ov.address(gauge), ov.address(current)) == ("TPG366 input 4", "ESI M1 HV current")


def lab_board():
    clock = Clock()
    ampr = connected_device("AMPR_A", clock)
    names = {"Inlet_Capillary": 110.0, "Entry_IF1": 130.0, "Middle_IF1": 110.0, "Exit_IF1": 80.0, "Entry_IF2": 79.0,
             "Middle_IF2": 70.0, "Exit_IF2": 40.0, "Ap_IF_Q1": 40.0, "Q1_DC": 36.0, "Ap_Q1_Q2": 34.0,
             "Stub_Entry_Q2": 20.0, "Stub_Exit_Q2": -5.0, "Q2_DC_1": 70.02, "Q2_DC_2": -11.22, "Ap_Q2_Q3": 0.0,
             "Q3_DC": -8.0, "Ap_Q3_Q4": -10.0, "Stub_Entry_Q4": -12.0, "Q4_DC_1": 10.0, "Q4_DC_2": -30.0,
             "Stub_Exit_Q4": -40.0}
    for index, (name, value) in enumerate(names.items()):
        Channel(ampr, name, value, value, module=index // 4, number=index % 4 + 1)
    ampr.channels[2].value = 112.0  # Middle_IF1: setpoint not reached yet.
    psu = connected_device("PSU_A", clock)
    for number in (0, 1):
        Channel(psu, f"PSU_A_CH{number}", 194.73, 194.7, number=number, output_state="ON")
    psu_b = connected_device("PSU_B", clock)  # Q4's RF (AMX_HD CH0-CH1).
    for number in (0, 1):
        Channel(psu_b, f"PSU_B_CH{number}", 120.0, 119.95, number=number, output_state="ON")
    amx = connected_device("AMX_HD", clock, psu_ch23="PSU_A", psu_ch01="PSU_B", frequency_khz=500.0)
    dmmr = connected_device("DMMR", clock)
    Channel(dmmr, "DMMR_M03", 0.0, 4.12e-10, unit="A", module=3)
    tpg = connected_device("TPG366", clock)
    for number, value in enumerate((1.2, 3.4e-2, 1.1e-1, 2.8e-3, float("nan"), float("nan")), start=1):
        Channel(tpg, f"P{number}", value, unit="mbar", input=number, status="OK")
    esi = connected_device("ESI", clock)
    esi.controller.adc_readback = {1: {"state": "ok", "value": 998.4, "requested": "POS"}}
    heat = Channel(esi, "ESI_HEAT", 40.0, unit="°C", module=0, function="Temperature", enabled=False)
    hv = Channel(esi, "ESI_HV1", 1000.0, unit="V", module=1, function="HV voltage", enabled=True)
    spray = Channel(esi, "ESI_HV1_I", 3.23e-8, unit="A", module=1, function="HV current")
    for channel in (heat, hv, spray):
        channel.is_heat_channel = (lambda c=channel: c.module_address() == 0)
    spray.is_current_channel = lambda: True
    heat.is_current_channel = hv.is_current_channel = lambda: False
    devices = [ampr, psu, psu_b, amx, dmmr, tpg, esi]
    board = ov.Board()
    board.update(devices, [c for d in devices for c in d.channels])
    return board, {d.name: d for d in devices}


def texts(board, owner=None):
    return {view.title: (view.pressure, view.owner, [(r.label, r.text, r.small) for r in view.rows])
            for view in board.cards(owner)}


def test_the_cards_show_the_lab_beamline_as_the_devices_read_it():
    board, _ = lab_board()
    cards = texts(board)
    assert list(cards) == ["Source", "Funnel 1", "Funnel 2", "Q1", "Q2", "Q3", "Q4", "Detector"]  # IF1 → … → Q4
    assert cards["Source"] == ("", "", [("Spray", "32.3 nA", "window 30–60 nA"), ("HV1", "998.4 V", "ON · set 1000 V"),
                                        ("Heater", "40 °C", "set · deselected")])
    assert cards["Funnel 1"] == ("1.2e+0 mbar", "", [("Inlet", "110.0 V", ""), ("Entry", "130.0 V", ""),
                                                     ("Middle", "110.0 V", "set 112.0"), ("Exit", "80.0 V", "")])
    assert cards["Q2"] == ("2.8e-3 mbar", "", [("RF", "194.7 V", ""), ("f", "500.0 kHz", ""), ("x / U", "x 29.4  U 40.6", ""),
                                               ("Aperture", "34.0 V", ""), ("Stubs", "20 / -5 V", "")])
    # Q3 built like Q1, Q4 like Q2 (user, 2026-11-22); P5 and P6 read NaN here.
    assert cards["Q3"] == ("—", "", [("Aperture", "0.0 V", ""), ("DC", "-8.0 V", "")])
    assert cards["Q4"] == ("—", "", [("RF", "120.0 V", ""), ("f", "500.0 kHz", ""), ("x / U", "x -10.0  U 20.0", ""),
                                     ("Aperture", "-10.0 V", ""), ("Stubs", "-12 / -40 V", "")])
    assert board.cards()[6].rows[0].tooltip.startswith("AMX_HD CH0-CH1 → PSU_B: CH0 120.0 V")
    assert cards["Detector"] == ("", "", [("M03", "412 pA", "")])
    assert board.cards()[-1].rows[0].trend == pytest.approx(4.12e-10)
    assert texts(board, ("Q2", "MScan map"))["Q2"][1] == "MScan map"
    assert texts(board, ("Q4", "MScan map"))["Q2"][1] == "" and texts(board, ("Q4", "MScan map"))["Q4"][1] == "MScan map"


def test_a_card_never_shows_a_value_its_device_cannot_vouch_for():
    board, devices = lab_board()
    ampr = devices["AMPR_A"]
    ampr.onAction.state, ampr.controller.initialized, ampr.controller.main_state = False, False, "Disconnected"
    devices["PSU_A"].channels[0].output_state = "OFF"
    devices["AMX_HD"].onAction.state = False
    devices["DMMR"].channels[0].monitor = float("nan")
    spray = devices["ESI"].channels[2]
    spray.value = 12e-9
    cards = texts(board)
    assert cards["Funnel 1"][2][1] == ("Entry", "—", "set 130.0 · AMPR_A OFF")
    assert cards["Q2"][2][0] == ("RF", "—", "output OFF")
    assert cards["Q2"][2][1] == ("f", "—", "set 500.0 · AMX_HD OFF")
    assert cards["Q2"][2][2] == ("x / U", "—", "AMPR_A OFF")
    assert cards["Detector"][2][0] == ("M03", "—", "")
    assert cards["Source"][2][0] == ("Spray", "12.0 nA", "window 30–60 nA · below")
    assert board.cards()[0].rows[0].warn
    devices["TPG366"].controller.initialized = False
    assert texts(board)["Q1"][0] == "—"


def test_a_missing_plugin_or_channel_is_said_not_guessed():
    board, devices = lab_board()
    board.update([d for d in devices.values() if d.name not in ("AMX_HD", "TPG366")],
                 [c for d in devices.values() for c in d.channels if c.name != "Q1_DC"])
    q1 = board.cards()[3]
    assert q1.pressure == "" and q1.pressure_tip == "P3: TPG366 not loaded"
    assert (q1.rows[1].text, q1.rows[1].tooltip) == ("—", "Q1_DC: not found (plugin not loaded, or no such channel)")
    assert board.cards()[4].rows[0].tooltip.startswith("AMX_HD: not found")


def test_formats():
    assert [ov.current(v) for v in (3.23e-8, 4.12e-10, -1.5e-12, 1e-3, 0.0, None)] == [
        "32.3 nA", "412 pA", "-1.50 pA", "1.00 mA", "0 A", "—"]
    assert [ov.pressure(v) for v in (1.2, 3.4e-2, 9.96e-4, 1e-3, None)] == [
        "1.2e+0 mbar", "3.4e-2 mbar", "1.0e-3 mbar", "1.0e-3 mbar", "—"]
    assert (ov.volts(-5.0), ov.kilohertz(549.5), ov.celsius(40.2)) == ("-5.0 V", "549.5 kHz", "40 °C")


def test_a_layout_of_overview_json_is_checked():
    assert ov.check_layout([{"title": "Q4", "pressure": 6, "rows": [{"label": "RF", "rf": ["AMX_HD", "CH0-CH1"]}]}])
    for bad in ([], [{"rows": []}], [{"title": "Q4", "rows": [{"label": "RF"}]}], [{"title": "Q4", "rows": [{"rf": []}]}]):
        with pytest.raises(ValueError):
            ov.check_layout(bad)


def test_the_overview_never_writes_a_channel_nor_calls_a_device():
    """v1 reads only: no assignment to a channel, no controller call, no Ilim; only the ON buttons are pressed."""
    for path in (ROOT / "overview" / "overview_plugin.py", ROOT / "overview" / "_runtime" / "_overview.py"):
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"\.(value|monitor|enabled|output_state)\s*=[^=]", text), path
        assert not re.search(r"(current_limit|ilim|Ilim)", text), path
        assert not re.search(r"\.setOn\(|toggleOnFromThread|\.controller\.\w+\(", text), path
        assert not re.search(r"^\s*(from|import)\s+(ampr_[ab]|amx_[ab]|amx_hd|psu_[a-e]|esi|dmmr|tpg366|mscan|transmission|sync)\b", text,
                             re.M)
