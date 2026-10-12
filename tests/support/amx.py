"""AMX / AMX HD plugin loading, register-level fakes and output snapshots shared by the AMX tests."""
from __future__ import annotations

import ast
from enum import Enum
import importlib
from importlib.metadata import distribution
from pathlib import Path
import re
from threading import Lock
from types import SimpleNamespace

import numpy as np

from support import explorer


def load_plugin(folder: str = "amx_a", module_name: str | None = None):
    """Import an AMX or AMX HD entry point against a fresh fake Explorer."""
    family = "amx_hd" if folder == "amx_hd" else "amx"
    return explorer.load_plugin(f"{folder}/{family}_plugin.py", module_name or f"{folder}_plugin_test",
                                runtimes=(family,))


def load_hd_controller_classes():
    """The private AMX HD runtime's controller and DLL base classes."""
    module = load_plugin("amx_hd")
    runtime_name = module._bundled_runtime_module_name(explorer.ROOT / "amx_hd")
    module._get_amx_driver_class()  # Loads the private runtime package.
    controller = importlib.import_module(f"{runtime_name}.amx_hd.amx_hd")
    base = importlib.import_module(f"{runtime_name}.amx_hd.amx_hd_base")
    return controller._AMXHDController, base.AMXHDBase


FOLDERS = ("amx_a", "amx_b", "amx_hd")
PRESETS = {
    79: (198, (98, 0, 0, 0)),
    12: (798, (298, 508, 0, 0)),
}


class RegisterDevice:
    CLOCK = DEF_CLOCK = 100e6
    OSC_OFFSET = PULSER_WIDTH_OFFSET = 2
    PULSER_DELAY_OFFSET = 3

    def __init__(self, count=4):
        self.connected = True
        self.enabled = False
        self.count = count
        self.calls = []
        self.fail_read = False
        self.period = 49998
        self.widths = [9998] * count
        self.config = -1

    def set_device_enabled(self, enabled, timeout_s=None):
        self.enabled = enabled
        self.calls.append(("enable", enabled))

    def load_config(self, index, timeout_s=None):
        self.config = index
        self.period, widths = PRESETS[index]
        self.widths = list(widths[:self.count])
        self.calls.append(("load", index))

    def get_status(self):
        return dict(memory_config=self.config, memory_config_name="Stored signal", memory_config_source="memory")

    def set_frequency_khz(self, frequency, timeout_s=None):
        self.period = round(self.CLOCK / (frequency * 1000) - self.OSC_OFFSET)
        self.calls.append(("frequency", frequency))

    def set_pulser_width_ticks(self, pulser, width, timeout_s=None):
        self.widths[pulser] = width
        self.calls.append(("width", pulser, width))

    def collect_state_snapshot(self, timeout_s=None):
        return dict(device_enabled=self.enabled, main_state={"name": "STATE_ON"},
                    device_state={"flags": []}, controller_state={"flags": []})

    def collect_housekeeping(self, timeout_s=None):
        self.calls.append(("read",))
        if self.fail_read:
            raise RuntimeError("timing read failed")
        return dict(self.collect_state_snapshot(), oscillator={"period": self.period},
                    pulsers=[dict(pulser=p, width_ticks=width, delay_ticks=7 if p == 0 else 0, burst=0)
                             for p, width in enumerate(self.widths)])

    @property
    def timing_writes(self):
        return [call for call in self.calls if call[0] in ("frequency", "width")]


def load_module(folder):
    return load_plugin(folder, f"config_load_{folder}")


def make_case(folder):
    module = load_module(folder)
    hd = folder == "amx_hd"
    cls = module.AMXHDDevice if hd else module.AMXDevice
    parent = cls.__new__(cls)
    parent.loading = False
    parent.frequency_khz = 2.
    parent.operating_config = 79
    parent.connect_timeout_s = parent.poll_timeout_s = parent.startup_timeout_s = 1.
    parent.isOn = lambda: True
    parent.print = lambda *args, **kwargs: None
    parent._sync_acquisition_controls = lambda: None
    parent.channels = [SimpleNamespace(
        real=True, enabled=True, active=False, equation="123", value=100., id=p,
        pulser_number=lambda p=p: p, getParameterByName=lambda _: None,
        setWidthText=lambda _: None, setDelayText=lambda _: None, setBurstText=lambda _: None,
        setDutyText=lambda _: None, setFreqText=lambda _: None,
    ) for p in range(2 if hd else 4)]
    parent.getChannels = lambda: parent.channels
    cls = module.AMXHDController if hd else module.AMXController
    controller = parent.controller = cls(parent)
    controller.lock = Lock()
    controller.errorCount = 0
    controller.print = parent.print
    controller.device = RegisterDevice(count=len(parent.channels))
    controller.initialized = True
    controller.available_configs = [dict(index=index, name="Stored signal", active=True, valid=True) for index in PRESETS]
    controller._restart_acquisition_after_transition = lambda: None
    return module, parent, controller


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


FLAGS = ["ST_ENABLE", "ST_ENB_TIMER", "ST_ENB_OSC0", "ST_ENB_SWITCH", "ST_ENB_PULS_GEN", "ST_CLRN"]
TIMER_OUT0, TIMER_RUN0, OSC0, INVERT = 4, 20, 36, 0x80
SWITCH_SYM = [(TIMER_OUT0, INVERT), (TIMER_OUT0 | INVERT, INVERT), (TIMER_OUT0, INVERT), (TIMER_OUT0 | INVERT, INVERT)]


def snapshot(timers=None, flags=FLAGS, enabled=True):
    """collect_housekeeping shape: timers {t: (delay, width, burst)}; 16 timers, 8 with burst."""
    timers = timers or {}
    return {
        "device_enabled": enabled, "main_state": {"name": "STATE_ON"},
        "controller_state": {"flags": list(flags)}, "oscillator": {"period": 9998},
        "pulsers": [dict(pulser=t, delay_ticks=timers.get(t, (0, 0, 0))[0], width_ticks=timers.get(t, (0, 0, 0))[1],
                         burst=timers.get(t, (0, 0, 0))[2] if t < 8 else None) for t in range(16)],
    }


def routing(switches, *, clocks=(3, 0, 0, 0), periods=(9998, 0, 0, 0), triggers=None, stops=None,
            delays=(0, 0, 0, 0)):
    triggers = {0: OSC0} if triggers is None else triggers
    stops = {t: 0 for t in triggers if t < 8} if stops is None else stops
    return {
        "clock_sources": list(clocks), "oscillator_periods": list(periods), "burst_timers": 8,
        "timer_triggers": dict(triggers), "timer_stops": dict(stops),
        "switches": [dict(switch=ch, trigger_source=trigger, enable_source=enable,
                          delay=dict(rise=delays[0], fall=delays[1]), fine_delay=dict(rise=delays[2], fall=delays[3]))
                     for ch, (trigger, enable) in enumerate(switches)],
    }


def config273():
    """Clock0=CLK100M, Oscillator0=9998, Timer00=7,4998,Osc0,Pos,0,0,Pos, SwitchSym, enables 0,Neg."""
    return snapshot({0: (7, 4998, 0)}), routing(SWITCH_SYM)


def register_sources(parent, *plugins):
    parent.pluginManager = SimpleNamespace(plugins=list(plugins))
    def channels():
        return [c for p in parent.pluginManager.plugins
                for c in getattr(p, "getChannels", lambda: [])() if hasattr(c, "name")]
    def lookup(name):
        return next((c for c in channels() if c.name.lower() == name.lower()), None)
    parent.pluginManager.DeviceManager = SimpleNamespace(channels=channels, getChannelByName=lookup)


def install_real_settings(module, parent, layout):
    """Exercise Explorer Parameter and makeSettingWrapper on real Qt combos."""
    import sys

    from PyQt6 import QtCore, QtGui, QtWidgets
    core = sys.modules["esibd.core"]
    root = Path(distribution("esibd-explorer").locate_file("esibd"))
    ns = dict(np=np, re=re, Path=Path, cast=lambda _, value: value,
              Channel=core.Channel, Setting=type("Setting", (), {}),
              Qt=QtCore.Qt, pyqtSignal=QtCore.pyqtSignal,
              CompactComboBox=QtWidgets.QComboBox, LedIndicator=QtWidgets.QLabel,
              PARAMETERTYPE=Enum("PARAMETERTYPE", "COMBO INTCOMBO FLOATCOMBO TEXT INT FLOAT EXP BOOL COLOR LABEL PATH"))
    for qt in (QtCore, QtGui, QtWidgets):
        ns.update({name: getattr(qt, name) for name in dir(qt) if name.startswith("Q")})
    source = ast.parse((root / "core.py").read_text())
    for name in ("ParameterWidget", "LabviewSpinBox", "LabviewDoubleSpinBox", "LabviewSciSpinBox", "CheckBox", "Parameter"):
        node = next(node for node in source.body if isinstance(node, ast.ClassDef) and node.name == name)
        exec(compile("from __future__ import annotations\n" + ast.unparse(node), str(root / "core.py"), "exec"), ns)
    source = ast.parse((root / "const.py").read_text())
    wrapper = next(node for node in source.body if isinstance(node, ast.FunctionDef) and node.name == "makeSettingWrapper")
    exec(compile("from __future__ import annotations\n" + ast.unparse(wrapper), str(root / "const.py"), "exec"), ns)
    module.Device.getDefaultSettings = lambda self: {
        f"{self.name}/Interval": {module.Parameter.VALUE: 1000},
        f"{self.name}/Max data points": {module.Parameter.VALUE: 100000},
    }
    parent.MAXDATAPOINTS = "Max data points"
    definitions = parent.getDefaultSettings()
    owner = SimpleNamespace(loading=True, print=parent.print, settings={})
    for key, attr in ((parent.PSU_CH01, "psu_ch01"), (parent.PSU_CH23, "psu_ch23")):
        full_name = f"{parent.name}/{key}"
        definition = definitions[full_name]
        parameter = ns["Parameter"](full_name, owner,
            parameterType=ns["PARAMETERTYPE"].COMBO,
            items=definition["items"], fixedItems=definition["fixedItems"],
            event=definition["event"], toolTip=definition["toolTip"])
        owner.settings[full_name] = parameter
        setattr(type(parent), attr, ns["makeSettingWrapper"](full_name, owner))
        parameter.value = definition["value"]
        layout.addWidget(QtWidgets.QLabel(key))
        layout.addWidget(parameter.combo)
    owner.loading = False
    return owner
