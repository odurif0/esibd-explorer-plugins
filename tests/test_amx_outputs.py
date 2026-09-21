"""Physical-output summaries must follow readback routing, not P numbers or names."""
import importlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_amx_config_load import load_module, make_case

FOLDERS = ("amx_a", "amx_b")


def config79_snapshot():
    # CGC Config-4D-All.cfg [Configuration80] (zero-based API slot 79).
    # Only P0 runs, yet all four switch channels are driven.
    return {
        "device_enabled": True, "main_state": {"name": "STATE_ON"},
        "controller_state": {"flags": ["ENB", "ENB_OSC", "ENB_PULSER", "ENABLE", "CLRN"]},
        "oscillator": {"period": 198},
        "pulsers": [dict(pulser=p, width_ticks=98 if p == 0 else 0,
                         delay_ticks=7 if p == 0 else 0, burst=0 if p < 2 else None,
                         trigger_config=2 if p == 0 else 0, stop_config=0 if p < 2 else None)
                    for p in range(4)],
        "switches": [dict(switch=ch, trigger_config=10 if ch % 2 == 0 else 42,
                          enable_config=32, trigger_delay={"rise": 0, "fall": 0}, enable_delay=0)
                     for ch in range(4)],
        "switch_mapping": {"trigger_enabled": False, "enable_enabled": False},
    }


@pytest.fixture(params=FOLDERS)
def module(request):
    return load_module(request.param)


def test_config79_outputs_not_pulsers(module):
    rows = module._amx_output_rows(config79_snapshot())
    assert [r["channel"] for r in rows] == ["CH0", "CH1", "CH2", "CH3"]
    assert all(r["state"] == "Periodic" and r["frequency"] == "500 kHz" for r in rows)
    assert all(r["levels"] == "Vneg ↔ Vpos" and r["dwell"] == "1 / 1" for r in rows)
    assert [r["relation"] for r in rows] == ["Reference", "Opposite to CH0", "Same as CH0", "Opposite to CH0"]


def test_changed_config_not_guessed_from_slot_name_or_gui_values(module):
    snapshot = config79_snapshot()
    snapshot.update(memory_config=79, memory_config_name="500kHz->SwitchSym")
    snapshot["oscillator"]["period"] = 798
    snapshot["pulsers"][0]["width_ticks"] = 298
    snapshot["switches"][2]["trigger_config"] = 32
    snapshot["switches"][3]["enable_config"] = 0
    rows = module._amx_output_rows(snapshot)
    assert rows[0]["frequency"] == "125 kHz"
    assert rows[0]["dwell"] == "3 / 5"
    assert rows[1]["dwell"] == "5 / 3"
    assert rows[1]["relation"] == "Opposite to CH0"  # NOT an assumed 180° shift
    assert rows[2]["state"] == "Static" and rows[2]["levels"] == "Vpos"
    assert rows[3]["state"] == "Hi-Z" and rows[3]["frequency"] == "—"
    assert "0 V" in rows[3]["detail"]


@pytest.mark.parametrize("stopped", ("width_ticks", "delay_ticks", "trigger_config"))
def test_stopping_p0_does_not_mean_zero_voltage_or_disabled_outputs(module, stopped):
    snapshot = config79_snapshot()
    snapshot["pulsers"][0][stopped] = 0
    rows = module._amx_output_rows(snapshot)
    assert all(r["state"] == "Static" and r["frequency"] == "—" for r in rows)
    assert [r["levels"] for r in rows] == ["Vneg", "Vpos", "Vneg", "Vpos"]


@pytest.mark.parametrize("field", ("trigger_enabled", "enable_enabled"))
@pytest.mark.parametrize("value", (True, None))
def test_active_or_missing_mapping_is_not_assumed_direct(module, field, value):
    snapshot = config79_snapshot()
    snapshot["switch_mapping"][field] = value
    rows = module._amx_output_rows(snapshot)
    assert all(r["state"] == "Unknown" and r["frequency"] == "Unknown" for r in rows)
    assert all("mapping" in r["detail"] for r in rows)


@pytest.mark.parametrize("case,expected", [
    ("external_pulser", "External"), ("external_switch", "External"),
    ("external_enable", "Gated"), ("burst", "Burst"),
    ("chained", "Unknown"), ("run", "Unknown"), ("software", "Software"),
    ("skip_triggers", "Unknown"), ("missing_trigger", "Unknown"),
    ("missing_burst", "Unknown"), ("invalid_source", "Unknown"),
    ("invalid_period", "Unknown"), ("missing_flags", "Unknown"),
])
def test_non_simple_signals_never_inherit_oscillator_frequency(module, case, expected):
    snapshot = config79_snapshot()
    p, s = snapshot["pulsers"][0], snapshot["switches"][0]
    if case == "external_pulser": p["trigger_config"] = 3
    elif case == "external_switch": s["trigger_config"] = 3
    elif case == "external_enable": s["enable_config"] = 3
    elif case == "burst": p["burst"] = 5
    elif case == "chained": p["trigger_config"] = 11
    elif case == "run": s["trigger_config"] = 14
    elif case == "software": s["trigger_config"] = 1
    elif case == "skip_triggers": p["width_ticks"] = 998
    elif case == "missing_trigger": p.pop("trigger_config")
    elif case == "missing_burst": p.pop("burst")
    elif case == "invalid_source": s["trigger_config"] = 255
    elif case == "invalid_period": snapshot["oscillator"]["period"] = 0
    elif case == "missing_flags": snapshot.pop("controller_state")
    row = module._amx_output_rows(snapshot)[0]
    assert row["state"] == expected
    assert row["frequency"] == "Unknown"
    assert row["dwell"] == "Unknown" and row["relation"] == "—"


def test_different_pulsers_and_edge_delays(module):
    snapshot = config79_snapshot()
    snapshot["pulsers"][1] = dict(snapshot["pulsers"][0], pulser=1, delay_ticks=57)
    snapshot["switches"][1]["trigger_config"] = 11
    rows = module._amx_output_rows(snapshot)
    assert rows[1]["frequency"] == "500 kHz"
    assert rows[1]["relation"] == "+0.5 µs vs CH0"
    snapshot["switches"][1]["trigger_delay"]["rise"] = 1
    rows = module._amx_output_rows(snapshot)
    assert rows[1]["frequency"] == "500 kHz"
    assert rows[1]["dwell"] == "Delay adjusted" and rows[1]["relation"] == "—"


def test_direct_oscillator_is_not_a_50_percent_square_wave(module):
    snapshot = config79_snapshot()
    snapshot["switches"][0]["trigger_config"] = 2
    row = module._amx_output_rows(snapshot)[0]
    assert row["frequency"] == "500 kHz" and row["dwell"] == "0.01 / 1.99"


@pytest.mark.parametrize("flag", ("ENB", "ENB_OSC", "ENB_PULSER", "CLRN"))
def test_disabled_controller_generators_not_shown_running(module, flag):
    snapshot = config79_snapshot()
    snapshot["controller_state"]["flags"].remove(flag)
    rows = module._amx_output_rows(snapshot)
    assert all(r["state"] == ("Hi-Z" if flag == "ENB" else "Static") for r in rows)
    assert all(r["frequency"] == "—" for r in rows)


@pytest.mark.parametrize("folder", FOLDERS)
def test_output_readback_invalidated_on_write_transition_and_poll_failure(folder):
    _, _, controller = make_case(folder)
    controller._apply_snapshot(config79_snapshot())
    assert controller.output_rows[0]["frequency"] == "500 kHz"
    controller.applyValue(controller.controllerParent.channels[0])
    assert controller.output_rows is None
    controller._apply_snapshot(config79_snapshot())
    controller.applyGlobalSettings()
    assert controller.output_rows is None
    controller._apply_snapshot(config79_snapshot())
    controller._begin_transition(True)
    assert controller.output_rows is None
    controller._end_transition()
    controller._apply_snapshot(config79_snapshot())
    controller._poll_error("simulated read error", exc=None)
    assert controller.output_rows is None


@pytest.mark.parametrize("folder", FOLDERS)
@pytest.mark.parametrize("exports", (True, False))
def test_live_runtime_collects_routing_under_housekeeping_read(folder, exports, monkeypatch):
    module = load_module(folder)
    module._get_amx_driver_class()
    namespace = module._bundled_runtime_module_name(Path(module.__file__).parent)
    runtime = importlib.import_module(f"{namespace}.amx.amx")
    cls, base = runtime._AMXController, runtime.AMXBase
    hw = cls.__new__(cls)
    hw.port = 0
    snapshot = config79_snapshot()
    values = {
        "get_main_state": (0, 0, "STATE_ON"), "get_device_state": (0, 0, []),
        "get_controller_state": (0, 0, snapshot["controller_state"]["flags"]),
        "get_device_enable": (0, True), "get_housekeeping": (0, 12., 5., 3.3, 40.),
        "get_sensor_data": (0, [25.] * 3),
        "get_fan_data": (0, [True] * 3, [False] * 3, [500] * 3, [500] * 3, [50] * 3),
        "get_led_data": (0, False, True, False), "get_cpu_data": (0, 1., 100e6),
        "get_uptime": (0, 1, 0, 1), "get_total_time": (0, 1, 1), "get_oscillator_period": (0, 198),
        "get_switch_trigger_delay": (0, 0, 0), "get_switch_enable_delay": (0, 0),
        "get_switch_enable_config": (0, 32), "get_pulser_burst": (0, 0),
    }
    for name, result in values.items():
        monkeypatch.setattr(base, name, lambda *args, result=result: result)
    monkeypatch.setattr(base, "get_pulser_width", lambda self, p: (0, snapshot["pulsers"][p]["width_ticks"]))
    monkeypatch.setattr(base, "get_pulser_delay", lambda self, p: (0, snapshot["pulsers"][p]["delay_ticks"]))
    monkeypatch.setattr(base, "get_switch_trigger_config", lambda self, ch: (0, 10 if ch % 2 == 0 else 42))
    calls = []
    def get_config(port, index, ptr):
        calls.append(index)
        ptr._obj.value = 2 if index == 0 else 0
        return 0
    def get_mapping(port, ptr):
        ptr._obj.value = False
        return 0
    hw.amx_dll = SimpleNamespace(**({
        "COM_HVAMX4ED_GetPulserConfig": get_config,
        "COM_HVAMX4ED_GetSwitchTriggerMappingEnable": get_mapping,
        "COM_HVAMX4ED_GetSwitchEnableMappingEnable": get_mapping,
    } if exports else {}))
    actual = hw._collect_housekeeping_unlocked()
    rows = module._amx_output_rows(actual)
    if exports:
        assert calls == [0, 1, 2, 3, 4, 5]
        assert actual["switch_mapping"] == snapshot["switch_mapping"]
        assert [{k: v for k, v in p.items() if k != "label"} for p in actual["pulsers"]] == snapshot["pulsers"]
        assert all(r["frequency"] == "500 kHz" for r in rows)
    else:
        assert calls == []
        assert all(r["frequency"] == "Unknown" for r in rows)
