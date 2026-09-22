"""Run the shipped notebook against an eight-module DMMR, never real hardware."""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
NOTEBOOK = ROOT / "dmmr/dmmr_zero_check.ipynb"


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

    def note(self, name, *args):
        assert not self._transport_poisoned, "Call made after the transport was poisoned"
        if self.automatic:
            assert name in ("automatic", "frame", "parse", "end_debug", "shutdown"), "Manual polling during streaming"
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

    def _call_locked_with_timeout(self, method, timeout, label):
        assert label == "check_auto_input"
        return method()

    def check_auto_input(self):
        self.note("parse")
        return (-200 if self.fault == "parser" and self.automatic else 0), 1

    def get_current(self, **kwargs):
        self.note("frame")
        self.clock.sleep(0.01)
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
    assert device.ranges == device.original
    assert saved["cleanup"]["ranges_restored"] and saved["cleanup"]["shutdown_confirmed"]
    assert list(output.glob("*.csv")) == [output / "raw.csv"]
    assert (output / "native_startup.log").read_text().endswith("Simulated native capture closed\n")
    assert summary.loc[summary["P/N"] == 132303, "ecart_au_certificat_pA"].isna().all()
    measured = summary.loc[summary["P/N"] != 132303, "ecart_au_certificat_pA"]
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
    assert device.calls.count(("end_debug",)) == 1


def test_full_fifteen_minutes_not_200_points(ns, rig):
    _, cfg, _, run = rig
    cfg.update(ns["PROTOCOL"])
    report, raw, summary, saved = run()
    assert report["status"] == "complete"
    assert 900 <= report["acquisition"]["duration_s"] < 900.1
    assert (summary.N > 11000).all()
    selected = raw.loc[raw.selected == 1]
    assert selected.host_elapsed_s.min() < 0.1  # No hidden five-minute warmup.
    assert selected.host_elapsed_s.max() < 900
    assert len(saved["time_bins"]) == 15 * 8
    assert len(saved["tail_summary"]) == 8
    assert all(row["debut_hote_s"] >= 600 for row in saved["tail_summary"])


def test_initial_range_reply_loss_has_bounded_verified_recovery(rig):
    device, _, _, run = rig
    getter = device.get_module_meas_range
    lost = False

    def read_range(address, **kwargs):
        nonlocal lost
        if not lost:
            lost = True
            device.note("lost_range_reply", address)
            return -10, 0, False
        return getter(address, **kwargs)

    device.get_module_meas_range = read_range
    report, *_ = run()
    assert report["status"] == "complete", report["error"]
    assert any(event["status"] == -10 for event in report["range_reads"])
    first = device.calls.index(("lost_range_reply", 0))
    assert device.calls[first + 1] == ("state",)
    assert device.calls[first + 2] == ("get_range", 0)


@pytest.mark.parametrize("status,expected_reads", [(-10, 2), (-11, 1), (-12, 1), (-13, 1), (-15, 1)])
def test_failed_range_reads_are_never_assumed_or_retried_indefinitely(rig, status, expected_reads):
    device, _, _, run = rig

    def bad_range(address, **kwargs):
        device.note("bad_range", address)
        return status, 0, False

    device.get_module_meas_range = bad_range
    report, raw, _, saved = run()
    assert report["status"] == "failed"
    assert f"module 0, lecture {expected_reads}" in report["error"]
    assert len(report["range_reads"]) == expected_reads
    assert raw.empty and not saved["tail_summary"]
    assert not any(call[0] in ("autorange", "range") for call in device.calls)
    assert report["cleanup"]["shutdown_confirmed"]
    assert not report["cleanup"]["range_restoration_needed"]
    assert device.calls.index(("end_debug",)) > device.calls.index(("shutdown",))


def test_range_recovery_requires_a_valid_controller_response(ns, rig):
    device, _, _, _ = rig
    device.get_module_meas_range = lambda *args, **kwargs: (-10, 0, False)
    device.get_state = lambda **kwargs: (-12, "0x0000", "ST_ON")
    events = []
    with pytest.raises(RuntimeError, match="controller state : statut -12"):
        ns["read_range"](device, 0, 0.1, events, "initial range")
    assert len(events) == 1
    device.get_state = lambda **kwargs: (0, "0x8001", "ST_ERR_MODULE")
    with pytest.raises(RuntimeError, match="État contrôleur inattendu"):
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
    assert "réponse de gamme invalide" in report["error"]
    assert raw.empty
    assert not any(call[0] in ("autorange", "range") for call in device.calls)


def test_wrong_readback_never_starts_the_measurement(rig):
    device, _, _, run = rig
    getter = device.get_module_meas_range
    reads = 0

    def incorrect_once(address, **kwargs):
        nonlocal reads
        reads += 1
        result = getter(address, **kwargs)
        return (0, 1, False) if reads == 9 else result

    device.get_module_meas_range = incorrect_once
    report, raw, *_ = run()
    assert report["status"] == "failed"
    assert "non confirmée" in report["error"]
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
    np.testing.assert_allclose(summary.moyenne_pA, 0.4, atol=1e-15)
    np.testing.assert_allclose(summary.ecart_type_fA, 0, atol=1e-12)


def test_failed_shutdown_is_not_reported_as_complete(ns, rig):
    device, cfg, output, run = rig
    device.shutdown_fails = True
    report, *_ = run()
    assert report["status"] == "cleanup_failed"
    assert device.connected
    with pytest.raises(RuntimeError, match="déconnectée"):
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
    assert "Identités" in report["error"]
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


def test_lost_write_ack_still_restores_the_potentially_changed_range(rig):
    device, _, _, run = rig
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
    assert report["status"] == "failed"
    assert raw.empty
    assert report["cleanup"]["ranges_restored"]
    assert device.ranges == device.original
    assert all(call[1] == 0 for call in device.calls if call[0] == "range")
    assert not any(call == ("automatic", True) for call in device.calls)


def test_certificate_is_literal_and_never_a_tolerance(ns):
    table = ns["certificate_table"]
    assert len(table) == 40
    assert ns["CERTIFICATE"][132303][0][0] == -1.269036e-10
    assert ns["CERTIFICATE"][132303][0][4] == -8.585e-15
    assert ns["CERTIFICATE"][132310][0][0] == 5.343050e-13
    assert ns["CERTIFICATE"][132306][0][3] == 6.466600e-13
    assert ns["AMBIGUOUS_CERTIFICATE_PNS"] == {132303}
    assert ns["PROTOCOL"]["input_condition"] == "open_unshielded"
    assert ns["PROTOCOL"]["duration_s"] == 900
    assert ns["PROTOCOL"]["range"] == 0
    assert ns["COM_PORT"] == 15
    assert table.page_PDF.min() == 3 and table.page_PDF.max() == 10


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
    np.testing.assert_allclose(summary.moyenne_pA, 0.406, atol=1e-15)
    np.testing.assert_allclose(summary.derive_fA_min, 3, atol=1e-12)
    np.testing.assert_allclose(summary.ecart_type_fA, np.std(values, ddof=1) * 1e15, atol=1e-12)
    bins = pd.DataFrame(saved["time_bins"])
    np.testing.assert_allclose(bins.loc[bins.adresse == 0, "moyenne_pA"], [0.4015, 0.4045, 0.4075, 0.4105])
    assert bins.ecart_type_fA.max() < summary.ecart_type_fA.min()
    tail = pd.DataFrame(saved["tail_summary"])
    np.testing.assert_allclose(tail.moyenne_pA, 0.409, atol=1e-15)
    assert set(tail.N) == {8}
    assert tail.stabilite.str.contains("examiner").all()


def test_figure_and_results_cell_render_without_certificate_masquerading_as_data(ns, rig, monkeypatch):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    _, _, output, run = rig
    _, raw, _, report = run()
    fig = ns["plot_run"](raw, report)
    fig.savefig(output / "time_series.png", dpi=100)
    assert len(fig.axes) == 8
    assert all("P/N" in ax.get_title() for ax in fig.axes)
    assert all(len(ax.lines) >= 2 for ax in fig.axes)
    plt.close(fig)
    # The repository need not depend on Jupyter: only its display sink is faked.
    # Execute the exact cell, with real pandas analysis and Matplotlib figures.
    from types import ModuleType
    displayed = []
    display_module = ModuleType("IPython.display")
    display_module.display = displayed.append
    monkeypatch.setitem(sys.modules, "IPython.display", display_module)
    monkeypatch.setattr(plt, "show", lambda: None)
    ns["RUN_DIR"] = output
    notebook = json.loads(NOTEBOOK.read_text())
    result_cell = next(cell for cell in notebook["cells"] if cell["id"] == "results")
    exec("".join(result_cell["source"]), ns)
    assert len(displayed) == 4
    assert not any("display(certificate_table)" in "".join(cell["source"]) for cell in notebook["cells"])
