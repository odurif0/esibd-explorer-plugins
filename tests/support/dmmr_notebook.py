"""DMMR zero-check notebook rig shared by the notebook tests."""
from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
NOTEBOOK = ROOT / "notebooks/dmmr_zero_check.ipynb"


def assert_restored_ranges(device):
    for address, (saved_range, saved_auto) in device.original.items():
        actual_range, actual_auto = device.ranges[address]
        assert actual_auto is saved_auto
        if not saved_auto:
            assert actual_range == saved_range


@pytest.fixture
def dmmr_notebook_ns():
    notebook = json.loads(NOTEBOOK.read_text(encoding="utf-8"))
    namespace = {}
    for cell in notebook["cells"]:
        if cell["cell_type"] != "code":
            continue
        source = "".join(cell["source"])
        compile(source, cell["id"], "exec")
        assert cell["outputs"] == []
        assert cell["execution_count"] is None
        if "definitions" in cell["metadata"]["tags"]:
            exec(source, namespace)
    return namespace


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class DMMR:
    NO_ERR = 0
    NO_DATA = 1

    def __init__(self, clock, certificate):
        self.clock = clock
        self.certificate = certificate
        self.connected = self.enabled = self.automatic = self._transport_poisoned = False
        # Deliberately not in certificate order: identify by product number.
        self.modules = {a: {"product_no": 132310 - a, "fw_version": "1-10"} for a in range(8)}
        self.ranges = {a: (a % 5, a % 2 == 1) for a in range(8)}
        self.original = dict(self.ranges)
        self.calls = []
        self.frames = 0
        self.fault = None
        self.duplicate_time = {}
        self.shutdown_fails = False
        self.frame_interval = 0.01
        self.diagnostic_delay = 0.0
        self.diagnostic_fault = None
        self.baudrate = self.port_baud = 230400
        self.protocol_events = []

    def new_command_recovery(self, **kwargs):
        from support.dmmr import load_plugin
        return load_plugin()._get_dmmr_driver_class().CommandRecovery(self, **kwargs)

    def new_read_recovery(self, ranges, **kwargs):
        from support.dmmr import load_plugin
        return load_plugin()._get_dmmr_driver_class().ReadRecovery(self, ranges, **kwargs)

    def record_protocol_event(self, event):
        self.protocol_events.append(json.loads(json.dumps(event)))

    def purge(self):
        self.note('purge')
        self.port_baud = 9600
        return 0

    def set_baud_rate(self, baud):
        self.note('baud', baud)
        self.port_baud = baud
        return 0, baud

    def note(self, name, *args):
        assert not self._transport_poisoned, "Call made after the transport was poisoned"
        if self.automatic:
            assert name in ("automatic", "frame", "parse", "begin_debug", "end_debug", "shutdown", "diagnostic", "state", "get_automatic", "get_enable", "get_range", "purge", "baud"), "Unexpected command during streaming"
        self.calls.append((name, *args))

    def begin_startup_diagnostics(self, **kwargs):
        self.note("begin_debug")
        return "Simulated native capture opened"

    def end_startup_diagnostics(self, **kwargs):
        self.note("end_debug")
        return "Simulated native capture closed"

    def connect(self, **kwargs):
        self.note("connect")
        self.connected = True
        return True

    def initialize(self, *, persist_scan, **kwargs):
        self.note("initialize", persist_scan)
        assert persist_scan is False
        return self.modules

    def get_product_info(self, **kwargs):
        self.note("identity")
        return {"product_no": 132302}

    def set_automatic_current(self, enabled, **kwargs):
        self.note("automatic", enabled)
        self.automatic = enabled
        return 0

    def get_automatic_current(self, **kwargs):
        self.note("get_automatic")
        return 0, self.automatic

    def set_enable(self, enabled, **kwargs):
        self.note("enable", enabled)
        self.enabled = enabled
        if self.fault == "enable_ack_lost" and enabled:
            return -12
        return 0

    def get_enable(self, **kwargs):
        self.note("get_enable")
        if self.shutdown_fails and not self.enabled:
            return 0, True  # OFF was requested, but the physical gate stays enabled.
        return 0, self.enabled

    def get_state(self, **kwargs):
        self.note("state")
        return 0, "0x0000", "ST_ON"

    def get_module_meas_range(self, address, **kwargs):
        self.note("get_range", address)
        return 0, *self.ranges[address]

    def set_module_auto_range(self, address, enabled, **kwargs):
        self.note("autorange", address, enabled)
        self.ranges[address] = (self.ranges[address][0], enabled)
        return 0

    def set_module_meas_range(self, address, value, **kwargs):
        self.note("range", address, value)
        self.ranges[address] = (value, self.ranges[address][1])
        return 0

    def _call_locked_with_timeout(self, method, timeout, label, *args):
        assert label == "check_auto_input" or label.startswith(("zero_check_get_", "read_recovery_", "range_setup_recovery_"))
        return method(*args)

    def __getattr__(self, name):
        fields = {
            "get_housekeeping": (12., 5., 3.3, 35.),
            "get_base_temp": (25.,), "get_base_fan_rpm": (1200.,),
            "get_base_fan_pwm": (100, "0x0000", []),
            "get_device_state": ("0x0000", ["DEVICE_OK"]),
            "get_voltage_state": ("0x0000", ["VOLTAGE_OK"]),
            "get_temperature_state": ("0x0000", ["TEMPERATURE_OK"]),
            "get_uptime_int": (123, 456, 123456, 789),
            "get_module_housekeeping": (3.3, 30., 5., 12., 3.3, 32., 2.5, -36., 20., -20., 15., -15., 1.8, -1.8, 2.5, -2.5),
            "get_module_state": (0,), "get_module_uptime_int": (12, 345, 6789, 123),
        }
        if name not in fields:
            raise AttributeError(name)

        def getter(*args):
            self.note("diagnostic", name, *args)
            self.clock.sleep(self.diagnostic_delay)
            if self.diagnostic_fault == "poisoned":
                self._transport_poisoned = True
                self.connected = False
                raise RuntimeError("blocked diagnostic DLL")
            if self.diagnostic_fault == "interrupt":
                raise KeyboardInterrupt()
            if self.diagnostic_fault == "status":
                return (-10, *([0.] * len(fields[name])))
            values = list(fields[name])
            if self.diagnostic_fault == "nan":
                values[0] = math.nan
            return 0, *values
        return getter

    def check_auto_input(self):
        self.note("parse")
        return (-200 if self.fault == "parser" and self.automatic else 0), 1

    def get_current(self, **kwargs):
        self.note("frame")
        # One empty FIFO observation between module batches, as on a live stream.
        if (self.automatic and self.frames and self.frames % 8 == 0
                and getattr(self, "last_empty_frame", None) != self.frames):
            self.last_empty_frame = self.frames
            return 1, 0, 0.0, 0, 0.0
        self.clock.sleep(self.frame_interval)
        if not self.automatic or self.fault == "stalled":
            return 1, 0, 0.0, 0, 0.0
        self.frames += 1
        address = (self.frames - 1) % 8
        actual, auto = self.ranges[address]
        if auto:
            actual = 0
        pn = self.modules[address]["product_no"]
        value = self.certificate[pn][0][actual] + 0.4e-12 + math.sin(self.frames) * 1e-15
        stamp = self.clock()
        if self.fault == "poisoned":
            self._transport_poisoned = True
            self.connected = False
            raise RuntimeError("simulated blocked DLL")
        if self.fault == "interrupt":
            raise KeyboardInterrupt()
        if self.fault == "bad_range":
            actual = 5
        if self.fault == "nan":
            value = math.nan
        if self.fault == "timestamp_nan":
            stamp = math.nan
        if self.fault == "unknown_address":
            address = 8
        if self.fault == "status":
            return -12, address, 666.0, actual, stamp
        if self.fault == "duplicate":
            stamp = self.duplicate_time.setdefault(address, stamp)
        if self.fault == "constant":
            value = 0.4e-12
        if self.fault == "wrong_fixed_range":
            actual = (actual + 1) % 5
        if self.fault == "missing_stream_module" and address == 7:
            return 1, 0, 0.0, 0, 0.0
        return 0, address, value, actual, stamp

    def disconnect(self):
        self.note("disconnect")
        self.connected = False
        return True

    def shutdown(self, **kwargs):
        self.note("shutdown")
        if self.shutdown_fails:
            raise RuntimeError("OFF not confirmed")
        self.enabled = self.automatic = self.connected = False
        return True


@pytest.fixture
def dmmr_notebook_rig(dmmr_notebook_ns, tmp_path):
    clock = Clock()
    device = DMMR(clock, dmmr_notebook_ns["CERTIFICATE"])
    cfg = dict(dmmr_notebook_ns["PROTOCOL"], duration_s=2.0, bin_s=0.5, tail_s=1.0,
               stall_timeout_s=0.5, io_timeout_s=0.1)
    output = tmp_path / "run"

    def run():
        report = dmmr_notebook_ns["run_zero_check"](device, output, cfg, clock=clock, sleep=clock.sleep)
        raw, summary, saved = dmmr_notebook_ns["analyse_run"](output)
        return report, raw, summary, saved

    return device, cfg, output, run
