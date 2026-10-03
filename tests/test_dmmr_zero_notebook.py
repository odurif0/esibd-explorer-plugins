"""Run the standalone notebook against an eight-module DMMR, never real hardware."""
from __future__ import annotations

import json
import math
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
NOTEBOOK = ROOT / "notebooks/dmmr_zero_check.ipynb"


def assert_restored_ranges(device):
    for address, (saved_range, saved_auto) in device.original.items():
        actual_range, actual_auto = device.ranges[address]
        assert actual_auto is saved_auto
        if not saved_auto:
            assert actual_range == saved_range


@pytest.fixture
def ns():
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
        from test_dmmr_plugin_behavior import _load_module
        return _load_module()._get_dmmr_driver_class().CommandRecovery(self, **kwargs)

    def new_read_recovery(self, ranges, **kwargs):
        from test_dmmr_plugin_behavior import _load_module
        return _load_module()._get_dmmr_driver_class().ReadRecovery(self, ranges, **kwargs)

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
def rig(ns, tmp_path):
    clock = Clock()
    device = DMMR(clock, ns["CERTIFICATE"])
    cfg = dict(ns["PROTOCOL"], duration_s=2.0, bin_s=0.5, tail_s=1.0,
               stall_timeout_s=0.5, io_timeout_s=0.1)
    output = tmp_path / "run"

    def run():
        report = ns["run_zero_check"](device, output, cfg, clock=clock, sleep=clock.sleep)
        raw, summary, saved = ns["analyse_run"](output)
        return report, raw, summary, saved

    return device, cfg, output, run


def test_complete_eight_module_run_and_identity_mapping(ns, rig):
    device, cfg, output, run = rig
    report, raw, summary, saved = run()
    assert report["status"] == "complete"
    assert len(summary) == 8
    assert (summary.N >= 20).all()
    assert summary.N.sum() == raw.selected.sum()
    assert set(raw.loc[raw.selected == 1, "actual_range"]) == {0}
    assert set(summary["P/N"]) == set(ns["CERTIFICATE"])
    assert (raw.loc[raw.address == 0, "product_no"] == 132310).all()
    assert saved["conformity"] == "not_assessable_no_acceptance_limits"
    assert not device.connected and not device.enabled
    assert_restored_ranges(device)
    assert saved["cleanup"]["ranges_restored"] and saved["cleanup"]["shutdown_confirmed"]
    assert {p.name for p in output.glob("*.csv")} == {"raw.csv", "telemetry.csv"}
    assert (output / "native_startup.log").read_text().endswith("Simulated native capture closed\n")
    assert summary.loc[summary["P/N"] == 132303, "certificate_difference_pA"].isna().all()
    measured = summary.loc[summary["P/N"] != 132303, "certificate_difference_pA"]
    np.testing.assert_allclose(measured, 0.4, atol=0.001)
    assert "NaN" not in (output / "report.json").read_text()
    assert "Infinity" not in (output / "report.json").read_text()
    assert ("initialize", False) in device.calls
    assert len(saved["time_bins"]) == 4 * 8
    assert len(saved["tail_summary"]) == 8
    assert report["acquisition"]["duration_s"] >= cfg["duration_s"]
    # Native capture ends once, after starting the stream, before ongoing reads.
    capture_end = device.calls.index(("end_debug",))
    assert device.calls[capture_end - 1] == ("automatic", True)
    assert device.calls[capture_end + 1] == ("parse",)
    assert device.calls.count(("end_debug",)) == 2
    assert saved["cleanup_diagnostics"]["file"] == "native_cleanup.log"
    assert (output / "native_cleanup.log").is_file()
    assert device.calls[-1] == ("end_debug",)
    assert device.calls[-2] == ("disconnect",)


def test_full_twenty_minutes_not_200_points(ns, rig):
    _, cfg, _, run = rig
    cfg.update(ns["PROTOCOL"], duration_s=1200)
    report, raw, summary, saved = run()
    assert report["status"] == "complete"
    assert 1200 <= report["acquisition"]["duration_s"] < 1200.1
    assert (summary.N > 11000).all()
    selected = raw.loc[raw.selected == 1]
    assert selected.host_elapsed_s.min() < 0.1  # No hidden five-minute warmup.
    assert selected.host_elapsed_s.max() < 1200
    assert len(saved["time_bins"]) == 20 * 8
    assert len(saved["tail_summary"]) == 8
    assert all(row["host_start_s"] >= 900 for row in saved["tail_summary"])


@pytest.mark.parametrize('status', [-10, -11, -12, -13])
def test_initial_range_reply_loss_has_bounded_verified_recovery(rig, status):
    device, _, _, run = rig
    getter = device.get_module_meas_range
    lost = False

    def read_range(address, **kwargs):
        nonlocal lost
        if not lost:
            lost = True
            device.note("lost_range_reply", address)
            return status, 0, False
        return getter(address, **kwargs)

    device.get_module_meas_range = read_range
    report, *_ = run()
    assert report["status"] == "complete", report["error"]
    assert any(event["status"] == status for event in report["range_reads"])
    first = device.calls.index(("lost_range_reply", 0))
    assert device.calls[first + 1] == ("state",)
    assert device.calls[first + 2] == ("get_range", 0)


@pytest.mark.parametrize("status,expected_reads", [(-10, 2), (-11, 2), (-12, 2), (-13, 2), (-15, 1)])
def test_failed_range_reads_are_never_assumed_or_retried_indefinitely(rig, status, expected_reads):
    device, _, _, run = rig

    def bad_range(address, **kwargs):
        device.note("bad_range", address)
        return status, 0, False

    device.get_module_meas_range = bad_range
    report, raw, _, saved = run()
    assert report["status"] == "failed"
    assert f"module 0, read {expected_reads}" in report["error"]
    assert len(report["range_reads"]) == expected_reads
    assert raw.empty and not saved["tail_summary"]
    assert not any(call[0] in ("autorange", "range") for call in device.calls)
    assert report["cleanup"]["shutdown_confirmed"]
    assert not report["cleanup"]["range_restoration_needed"]
    first_close = device.calls.index(("end_debug",))
    disconnect = device.calls.index(("disconnect",))
    assert first_close < disconnect
    assert device.calls[-1] == ("end_debug",)


def test_range_recovery_requires_a_valid_controller_response(ns, rig):
    device, _, _, _ = rig
    device.get_module_meas_range = lambda *args, **kwargs: (-10, 0, False)
    device.get_state = lambda **kwargs: (-12, "0x0000", "ST_ON")
    events = []
    with pytest.raises(RuntimeError, match="controller state: status -12"):
        ns["read_range"](device, 0, 0.1, events, "initial range")
    assert len(events) == 1
    device.get_state = lambda **kwargs: (0, "0x8001", "ST_ERR_MODULE")
    with pytest.raises(RuntimeError, match="Unexpected controller state"):
        ns["read_range"](device, 0, 0.1, [], "initial range")


def test_no_changes_until_all_initial_ranges_are_known(rig):
    device, _, _, run = rig
    getter = device.get_module_meas_range

    def missing_fourth_range(address, **kwargs):
        return (-10, 0, False) if address == 3 else getter(address, **kwargs)

    device.get_module_meas_range = missing_fourth_range
    report, raw, *_ = run()
    assert report["status"] == "failed"
    assert set(report["initial_ranges"]) == {0, 1, 2}
    assert len([event for event in report["range_reads"] if event["address"] == 3]) == 2
    assert raw.empty
    assert not any(call[0] in ("autorange", "range") for call in device.calls)
    assert report["cleanup"]["shutdown_confirmed"]


@pytest.mark.parametrize("actual,auto", [(5, False), (0.0, False), (True, False), (0, 1)])
def test_invalid_initial_range_is_not_coerced_into_a_valid_snapshot(rig, actual, auto):
    device, _, _, run = rig
    device.get_module_meas_range = lambda *args, **kwargs: (0, actual, auto)
    report, raw, *_ = run()
    assert report["status"] == "failed"
    assert "invalid range response" in report["error"]
    assert raw.empty
    assert not any(call[0] in ("autorange", "range") for call in device.calls)


def test_wrong_readback_never_starts_the_measurement(rig):
    device, _, _, run = rig
    getter = device.get_module_meas_range
    injected = False

    def incorrect_once(address, **kwargs):
        nonlocal injected
        after_write = any(c[0] == 'range' for c in device.calls)
        result = getter(address, **kwargs)
        if after_write and not injected:
            injected = True
            return 0, 1, False
        return result

    device.get_module_meas_range = incorrect_once
    report, raw, *_ = run()
    assert report["status"] == "failed"
    assert "not confirmed" in report["error"]
    assert raw.empty
    assert not any(call == ("automatic", True) for call in device.calls)
    assert report["cleanup"]["ranges_restored"]
    assert report["cleanup"]["shutdown_confirmed"]


def test_poisoned_initial_read_makes_no_recovery_or_cleanup_calls(rig):
    device, _, _, run = rig

    def blocked(address, **kwargs):
        device.note("blocked_read", address)
        device._transport_poisoned = True
        device.connected = False
        raise RuntimeError("blocked DLL")

    device.get_module_meas_range = blocked
    report, raw, *_ = run()
    assert report["status"] == "failed"
    assert device.calls[-1] == ("blocked_read", 0)
    assert not report["cleanup"]["shutdown_confirmed"]
    assert raw.empty


@pytest.mark.parametrize("fault", ["poisoned", "interrupt", "bad_range", "nan", "timestamp_nan",
                                  "unknown_address", "status", "duplicate", "stalled", "parser",
                                  "wrong_fixed_range", "enable_ack_lost", "missing_stream_module"])
def test_faults_save_partial_data_without_inventing_samples(ns, rig, fault):
    device, _, output, run = rig
    device.fault = fault
    report, raw, _, saved = run()
    assert report["status"] in ("failed", "interrupted")
    assert report["error"]
    assert (output / "raw.csv").exists() and (output / "report.json").exists()
    assert (output / "native_startup.log").exists()
    assert not saved["tail_summary"]
    assert len(raw.loc[(raw.status != 0) & (raw.selected == 1)]) == 0
    if fault == "status":
        assert raw.current_A.isna().all(), "An error payload must not look like a measured current"
    if fault == "poisoned":
        assert device.calls[-1] == ("frame",)
        assert not report["cleanup"]["shutdown_confirmed"]
    else:
        assert report["cleanup"]["shutdown_confirmed"]
        assert not device.connected
    if fault in ("duplicate", "stalled", "missing_stream_module"):
        assert report["acquisition"]["duration_s"] < 1.0
    if fault == "duplicate":
        assert (raw.reason == "timestamp_not_increasing").any()
        assert (raw.loc[raw.selected == 1].groupby("address").size() == 1).all()


def test_equal_currents_are_kept_if_timestamps_are_new(rig):
    device, _, _, run = rig
    device.fault = "constant"
    report, _, summary, _ = run()
    assert report["status"] == "complete"
    assert (summary.N > 20).all()
    np.testing.assert_allclose(summary.mean_pA, 0.4, atol=1e-15)
    np.testing.assert_allclose(summary.std_fA, 0, atol=1e-12)


def test_failed_shutdown_is_not_reported_as_complete(ns, rig):
    device, cfg, output, run = rig
    device.shutdown_fails = True
    report, *_ = run()
    assert report["status"] == "cleanup_failed"
    assert device.connected
    with pytest.raises(RuntimeError, match="disconnected"):
        ns["run_zero_check"](device, output.parent / "retry", cfg)


def test_no_overwrite_and_invalid_configuration_never_touch_hardware(ns, rig):
    device, cfg, output, _ = rig
    output.mkdir()
    with pytest.raises(FileExistsError):
        ns["run_zero_check"](device, output, cfg)
    for key, value in [("duration_s", 0), ("duration_s", math.inf), ("duration_s", math.nan),
                       ("bin_s", -1), ("tail_s", 3), ("stall_timeout_s", 3),
                       ("range", True), ("range", 5), ("range", 0.5), ("io_timeout_s", True)]:
        with pytest.raises(ValueError):
            ns["run_zero_check"](device, output, dict(cfg, **{key: value}))
    assert device.calls == []


@pytest.mark.parametrize("kind", ["missing", "unknown", "duplicate"])
def test_no_assumed_mapping_for_unknown_module_population(rig, kind):
    device, _, _, run = rig
    if kind == "missing":
        del device.modules[7]
    elif kind == "unknown":
        device.modules[2]["product_no"] = 999999
    else:
        device.modules[2]["product_no"] = device.modules[0]["product_no"]
    report, raw, *_ = run()
    assert report["status"] == "failed"
    assert "Module identities" in report["error"]
    assert raw.empty
    assert not any(call[0] in ("autorange", "range") for call in device.calls)
    assert report["cleanup"]["shutdown_confirmed"]


def test_poisoned_restoration_never_closes_in_parallel(ns, rig):
    device, _, _, _ = rig
    device.connected = True

    def blocked(address, value, **kwargs):
        device.note("blocked_restore")
        device._transport_poisoned = True
        device.connected = False
        raise RuntimeError("blocked")

    device.set_module_meas_range = blocked
    result = ns["restore_and_shutdown"](device, {0: (2, False)}, 0.1)
    assert device.calls[-1] == ("blocked_restore",)
    assert not result["shutdown_confirmed"]
    assert not result["ranges_restored"]


def test_lost_write_ack_is_recovered_and_original_range_is_restored(rig):
    device, _, _, run = rig
    device.original[0] = device.ranges[0] = (2, False)
    setter = device.set_module_meas_range
    first = True

    def lose_ack(address, value, **kwargs):
        nonlocal first
        result = setter(address, value, **kwargs)
        if first:
            first = False
            return -12
        return result

    device.set_module_meas_range = lose_ack
    report, raw, *_ = run()
    assert report["status"] == "complete"
    assert not raw.empty
    assert report["cleanup"]["ranges_restored"]
    assert_restored_ranges(device)
    assert device.calls.count(('range', 0, 0)) == 1
    assert device.calls.count(('range', 0, 2)) == 1
    incident, = report['range_recoveries']
    assert incident['status'] == -12 and incident['verified']
    assert any(call == ("automatic", True) for call in device.calls)


def test_certificate_is_literal_and_never_a_tolerance(ns):
    table = ns["certificate_table"]
    assert len(table) == 40
    assert ns["CERTIFICATE"][132303][0][0] == -1.269036e-10
    assert ns["CERTIFICATE"][132303][0][4] == -8.585e-15
    assert ns["CERTIFICATE"][132310][0][0] == 5.343050e-13
    assert ns["CERTIFICATE"][132306][0][3] == 6.466600e-13
    assert ns["AMBIGUOUS_CERTIFICATE_PNS"] == {132303}
    assert ns["PROTOCOL"]["input_condition"] == "open_unshielded"
    assert ns["PROTOCOL"]["duration_s"] == 21600
    assert ns["PROTOCOL"]["series_count"] == 2
    assert ns["PROTOCOL"]["pause_s"] == 300
    assert ns["PROTOCOL"]["baseline_window_s"] == 1800
    assert ns["PROTOCOL"]["diagnostics_interval_s"] == 60
    assert ns["PROTOCOL"]["range"] == 0
    assert ns["COM_PORT"] == 15
    assert table.pdf_page.min() == 3 and table.pdf_page.max() == 10


def test_private_runtime_loader_does_not_modify_sys_path(ns):
    before = list(sys.path)
    cls = ns["load_driver"](ROOT / "dmmr")
    assert cls.__module__.startswith("_esibd_bundled_dmmr_zero_check.")
    assert sys.path == before


def test_statistics_preserve_known_drift_and_distinguish_short_term_dispersion(ns, rig):
    _, _, output, run = rig
    _, _, _, report = run()
    report["protocol"].update(duration_s=240, tail_s=120, bin_s=60)
    times = np.arange(7.5, 240, 15)
    values = (400 + 3 * times / 60) * 1e-15
    rows = []
    for address, info in report["modules"].items():
        for t, value in zip(times, values):
            rows.append(dict(phase="continuous", address=int(address), product_no=info["product_no"],
                             requested_range=0, actual_range=0, host_utc="", host_elapsed_s=t,
                             device_time_s=100000 + t, current_A=value, status=0, selected=1, reason="selected"))
    pd.DataFrame(rows).to_csv(output / "raw.csv", index=False)
    ns["save_report"](output, report)
    _, summary, saved = ns["analyse_run"](output)
    np.testing.assert_allclose(summary.mean_pA, 0.406, atol=1e-15)
    np.testing.assert_allclose(summary.drift_fA_min, 3, atol=1e-12)
    np.testing.assert_allclose(summary.std_fA, np.std(values, ddof=1) * 1e15, atol=1e-12)
    bins = pd.DataFrame(saved["time_bins"])
    np.testing.assert_allclose(bins.loc[bins.address == 0, "mean_pA"], [0.4015, 0.4045, 0.4075, 0.4105])
    assert bins.std_fA.max() < summary.std_fA.min()
    tail = pd.DataFrame(saved["tail_summary"])
    np.testing.assert_allclose(tail.mean_pA, 0.409, atol=1e-15)
    assert set(tail.N) == {8}
    assert tail.stability.str.contains("review").all()


def test_figure_and_results_cell_render_without_certificate_masquerading_as_data(ns, rig, monkeypatch):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    _, _, output, _ = rig
    # Execute the results cell against a full campaign rather than a single run.
    device = DMMR(Clock(), ns["CERTIFICATE"])
    campaign = output.parent / "campaign"
    cfg = dict(ns["PROTOCOL"], series_count=2, duration_s=5, bin_s=1, tail_s=2,
               stall_timeout_s=1, io_timeout_s=.1, pause_s=1, diagnostics_interval_s=2,
               baseline_window_s=2)
    ns["run_campaign"](device, campaign, cfg, clock=device.clock, sleep=device.clock.sleep)
    # The repository need not depend on Jupyter: only its display sink is faked.
    # Execute the exact cell, with real pandas analysis and Matplotlib figures.
    from types import ModuleType
    displayed = []
    display_module = ModuleType("IPython.display")
    display_module.display = displayed.append
    monkeypatch.setitem(sys.modules, "IPython.display", display_module)
    monkeypatch.setattr(plt, "show", lambda: None)
    ns["RUN_DIR"] = campaign
    notebook = json.loads(NOTEBOOK.read_text())
    result_cell = next(cell for cell in notebook["cells"] if cell["id"] == "results")
    exec("".join(result_cell["source"]), ns)
    assert len(displayed) == 7
    assert list(displayed[1].columns) == ["series", "P/N", "address", "range", "N", "mean_pA", "std_fA", "drift_fA_min"]
    assert list(displayed[2].columns) == list(displayed[1].columns)
    assert len(displayed[3]) == 32
    assert list(displayed[3].columns) == ['series', 'P/N', 'start_min', 'end_min', 'N', 'mean_pA', 'drift_fA_min']
    assert len(list(campaign.glob("*.png"))) == 3
    assert not any("display(certificate_table)" in "".join(cell["source"]) for cell in notebook["cells"])


def test_notebook_has_one_canonical_location_and_concise_english_text():
    assert not (ROOT / "dmmr/dmmr_zero_check.ipynb").exists()
    notebook = json.loads(NOTEBOOK.read_text())
    text = "\n".join("".join(cell["source"]) for cell in notebook["cells"])
    markdown = "\n".join("".join(cell["source"]) for cell in notebook["cells"] if cell["cell_type"] == "markdown")
    assert len(markdown.split()) < 550
    assert not re.search(r"[àâçèéêëîïôùûœ]", text, re.IGNORECASE)
    for cell in notebook["cells"]:
        if cell["id"] in ("implementation", "certificate-data"):
            assert cell["metadata"]["jupyter"]["source_hidden"]


@pytest.mark.parametrize("working_dir", [".", "notebooks", "dmmr"])
def test_driver_discovery_from_repository_and_notebook_locations(ns, monkeypatch, working_dir):
    monkeypatch.chdir(ROOT / working_dir)
    before = list(sys.path)
    plugin = ns["find_plugin_dir"]()
    assert plugin == ROOT / "dmmr"
    assert ns["load_driver"](plugin).__module__.startswith("_esibd_bundled_dmmr_zero_check.")
    assert sys.path == before


def test_driver_discovery_supports_installed_and_explicit_paths(ns, monkeypatch, tmp_path):
    home = tmp_path / "home"
    installed = home / "ESIBD Explorer/plugins/dmmr"
    installed.mkdir(parents=True)
    entrypoint = installed / "dmmr_plugin.py"
    entrypoint.write_text("# Test installation marker\n")
    work = tmp_path / "work/notebooks"
    work.mkdir(parents=True)
    monkeypatch.chdir(work)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    assert ns["find_plugin_dir"]() == installed
    assert ns["find_plugin_dir"](ROOT / "dmmr") == ROOT / "dmmr"
    entrypoint.unlink()
    with pytest.raises(FileNotFoundError, match="set PLUGIN_DIR"):
        ns["find_plugin_dir"]()


@pytest.mark.parametrize('outdated', [False, 'ReadRecovery', 'CommandRecovery'])
def test_hardware_cell_resolves_sibling_driver_without_opening_real_hardware(ns, monkeypatch, tmp_path, outdated):
    from types import SimpleNamespace

    monkeypatch.chdir(NOTEBOOK.parent)
    ns["os"] = SimpleNamespace(name="nt")
    ns["OUTPUT_ROOT"] = tmp_path / "logs"
    created, runs = [], []

    def factory(name, **kwargs):
        created.append((name, kwargs))
        return SimpleNamespace(connected=False)

    for name in ('ReadRecovery', 'CommandRecovery'):
        if name != outdated:
            setattr(factory, name, object)
    ns["load_driver"] = lambda path: factory
    ns["run_campaign"] = lambda device, directory, protocol, **kwargs: runs.append((device, directory, protocol, kwargs))
    notebook = json.loads(NOTEBOOK.read_text())
    cell = next(c for c in notebook["cells"] if c["id"] == "run")
    if outdated:
        with pytest.raises(RuntimeError, match='Update the complete dmmr/'):
            exec("".join(cell["source"]), ns)
        assert not created and not runs
        return
    exec("".join(cell["source"]), ns)
    assert ns["PLUGIN_DIR"] == ROOT / "dmmr"
    assert len(created) == len(runs) == 1
    assert created[0][1]["com"] == 15
    assert created[0][1]["process_backend"] is False
    assert runs[0][1].parent == ns["OUTPUT_ROOT"]
    provenance = runs[0][3]["provenance"]
    assert provenance["plugin_dir"] == str(ROOT / "dmmr")
    assert len(provenance["sha256"]) == 5
    assert 'vendor/runtime/dmmr/read_recovery.py' in provenance['sha256']


def test_old_derived_tables_are_rebuilt_in_english_without_changing_raw_data(ns, rig):
    _, _, output, run = rig
    _, _, expected, report = run()
    before = (output / "raw.csv").read_bytes()
    report["summary"] = [{"adresse": 0, "moyenne_pA": 999.0}]
    report["time_bins"] = [{"debut_s": 0, "moyenne_pA": 999.0}]
    report["tail_summary"] = [{"stabilite": "à examiner"}]
    ns["save_report"](output, report)
    _, actual, updated = ns["analyse_run"](output)
    pd.testing.assert_frame_equal(actual, expected)
    assert (output / "raw.csv").read_bytes() == before
    assert "mean_pA" in updated["time_bins"][0]
    assert "stability" in updated["tail_summary"][0]
