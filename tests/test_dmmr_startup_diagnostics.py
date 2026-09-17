"""Native startup evidence reaches Explorer without extra serial commands."""
from __future__ import annotations

import builtins
import ctypes
import logging
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from test_dmmr_plugin_behavior import _load_module
from test_dmmr_toggle_failures import rig as toggle_rig, start


@pytest.fixture(name="rig")
def diagnostics_rig():
    return toggle_rig.__wrapped__()


DLL_PATH = Path(__file__).resolve().parents[1] / "dmmr/vendor/runtime/dmmr/vendor/x64/COM-DMMR-8.dll"


class NativeFunction:
    def __init__(self, implementation):
        self.implementation = implementation
        self.argtypes = self.restype = None

    def __call__(self, *args):
        for kind, value in zip(self.argtypes, args):
            kind.from_param(value)
        return self.implementation(*args)


@pytest.fixture
def native(tmp_path):
    cls = _load_module()._get_dmmr_driver_class()._PROCESS_CONTROLLER_CLASS
    driver = cls.__new__(cls)
    driver.com, driver.baudrate = 13, 230400
    driver.connected = True
    driver._transport_poisoned = False
    driver._transport_error = None
    driver.thread_lock = threading.Lock()
    driver.logger = logging.getLogger("dmmr-startup-test")
    driver._set_port_claimed = lambda value: None
    driver.dmmr_dll_path = DLL_PATH
    driver._startup_log_dir = tmp_path
    driver._startup_log_path = None
    calls = []
    state = SimpleNamespace(open_status=0, close_status=0, path=None)

    def open_log(filename):
        assert driver.thread_lock.locked()
        calls.append(("open", threading.get_ident()))
        state.path = Path(filename.decode("utf-8"))
        if state.open_status == 0:
            state.path.write_bytes(b"Opening debug file\r\n")
        return state.open_status

    def close_log():
        assert driver.thread_lock.locked()
        calls.append(("close", threading.get_ident()))
        # The actual DLL clears its FILE pointer even if fclose returns -401.
        return state.close_status

    driver.dll = SimpleNamespace(
        COM_DMMR_8_OpenDebugFile=NativeFunction(open_log),
        COM_DMMR_8_CloseDebugFile=NativeFunction(close_log),
    )
    driver._configure_dll_signatures()
    return SimpleNamespace(driver=driver, calls=calls, state=state, directory=tmp_path)


def test_optional_native_signatures_are_explicit(native):
    dll = native.driver.dll
    assert dll.COM_DMMR_8_OpenDebugFile.argtypes == [ctypes.c_char_p]
    assert dll.COM_DMMR_8_OpenDebugFile.restype is ctypes.c_int
    assert dll.COM_DMMR_8_CloseDebugFile.argtypes == []
    assert dll.COM_DMMR_8_CloseDebugFile.restype is ctypes.c_int


def test_native_trace_contains_identity_and_data_read(native):
    driver = native.driver
    header = driver.begin_startup_diagnostics(timeout_s=0.5)
    assert "COM13" in header and "230400" in header
    assert DLL_PATH.name in header and "e3bb4674" in header
    native.state.path.write_bytes(b"WriteCommand: 'g5Y'\r\nData read: '5Yx'\r\n")
    report = driver.end_startup_diagnostics(timeout_s=0.5)
    assert "Data read: '5Yx'" in report
    assert [call[0] for call in native.calls] == ["open", "close"]
    assert all(tid != threading.get_ident() for _, tid in native.calls)
    assert not driver.thread_lock.locked()
    assert driver.end_startup_diagnostics(timeout_s=0.5) == ""


def test_repeated_starts_reuse_one_bounded_file(native):
    driver = native.driver
    paths = set()
    for _ in range(4):
        driver.begin_startup_diagnostics(timeout_s=0.5)
        paths.add(native.state.path)
        native.state.path.write_bytes(b"x" * (driver._STARTUP_LOG_MAX_BYTES + 4000))
        report = driver.end_startup_diagnostics(timeout_s=0.5)
        assert "truncated" in report.lower()
        assert native.state.path.stat().st_size <= driver._STARTUP_LOG_MAX_BYTES
        assert len(report) < 5 * driver._STARTUP_LOG_MAX_BYTES
    assert len(paths) == 1
    assert list(native.directory.iterdir()) == list(paths)


def test_control_bytes_remain_visible_and_cannot_spoof_log_lines(native):
    driver = native.driver
    driver.begin_startup_diagnostics(timeout_s=0.5)
    native.state.path.write_bytes(b"Data read: '5Y\x00\xff\rZ'\r\n\x1b[31merror\n")
    report = driver.end_startup_diagnostics(timeout_s=0.5)
    assert r"5Y\x00\xff\x0dZ" in report
    assert r"\x1b[31merror" in report
    assert "\x00" not in report and "\x1b" not in report
    assert all(line.startswith("[DMMR native]") for line in report.splitlines())


@pytest.mark.parametrize("export", ["COM_DMMR_8_OpenDebugFile", "COM_DMMR_8_CloseDebugFile"])
def test_both_optional_exports_are_required(native, export):
    delattr(native.driver.dll, export)
    report = native.driver.begin_startup_diagnostics(timeout_s=0.5)
    assert "unavailable" in report.lower()
    assert native.calls == []
    assert native.driver.end_startup_diagnostics(timeout_s=0.5) == ""


def test_unverified_dll_does_not_use_undocumented_debug_api(native, tmp_path):
    other = tmp_path / "other.dll"
    other.write_bytes(b"a different DLL")
    native.driver.dmmr_dll_path = other
    report = native.driver.begin_startup_diagnostics(timeout_s=0.5)
    assert "unverified DLL" in report
    assert native.calls == []


def test_missing_optional_stdlib_module_does_not_break_device_control(native, monkeypatch):
    original_import = builtins.__import__

    def import_without_hashlib(name, *args, **kwargs):
        if name == "hashlib":
            raise ModuleNotFoundError("No module named 'hashlib'", name=name)
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_without_hashlib)
    report = native.driver.begin_startup_diagnostics(timeout_s=0.5)
    assert "unavailable" in report.lower() and "hashlib" in report
    assert native.driver._transport_poisoned is False
    assert native.calls == []


def test_unwritable_log_directory_is_not_a_hardware_error(native, tmp_path):
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("not a directory")
    native.driver._startup_log_dir = blocked
    assert "unavailable" in native.driver.begin_startup_diagnostics(timeout_s=0.5).lower()
    assert native.calls == []
    assert native.driver._transport_poisoned is False


def test_native_open_failure_is_reported_without_poisoning(native):
    native.state.open_status = -400
    report = native.driver.begin_startup_diagnostics(timeout_s=0.5)
    assert "-400" in report and "unavailable" in report.lower()
    assert native.driver.end_startup_diagnostics(timeout_s=0.5) == ""
    assert native.driver._transport_poisoned is False
    assert [name for name, _ in native.calls] == ["open"]


def test_native_close_failure_still_reports_captured_data(native):
    native.driver.begin_startup_diagnostics(timeout_s=0.5)
    native.state.close_status = -401
    report = native.driver.end_startup_diagnostics(timeout_s=0.5)
    assert "-401" in report and "Opening debug file" in report
    assert native.driver._transport_poisoned is False
    assert native.driver._startup_log_path is None


def test_poisoned_call_never_triggers_parallel_native_close(native):
    driver = native.driver
    driver.begin_startup_diagnostics(timeout_s=0.5)
    release, finished = threading.Event(), threading.Event()

    def blocked():
        try:
            release.wait(2)
        finally:
            finished.set()

    try:
        with pytest.raises(RuntimeError, match="timed out"):
            driver._call_locked_with_timeout(blocked, 0.02, "simulated blocked serial call")
        assert not finished.is_set()
        report = driver.end_startup_diagnostics(timeout_s=0.5)
        assert "not closed" in report.lower() and "restart Explorer" in report
        assert [name for name, _ in native.calls] == ["open"]
        assert driver.thread_lock.locked()
        # A second ON cannot truncate the active file or open another native log.
        assert "unavailable" in driver.begin_startup_diagnostics(timeout_s=0.1).lower()
        assert [name for name, _ in native.calls] == ["open"]
    finally:
        release.set()
        assert finished.wait(1)


def test_native_close_timeout_does_not_retry_or_trim_an_active_file(native):
    driver = native.driver
    driver.begin_startup_diagnostics(timeout_s=0.5)
    size = driver._STARTUP_LOG_MAX_BYTES + 20
    native.state.path.write_bytes(b"x" * size)
    release, finished = threading.Event(), threading.Event()
    calls = []

    def blocked_close():
        calls.append(threading.get_ident())
        try:
            release.wait(2)
        finally:
            finished.set()
        return 0

    driver.dll.COM_DMMR_8_CloseDebugFile.implementation = blocked_close
    try:
        report = driver.end_startup_diagnostics(timeout_s=0.02)
        assert "not closed" in report.lower()
        assert not finished.is_set()
        assert driver._transport_poisoned is True
        assert native.state.path.stat().st_size == size
        assert driver._startup_log_path is not None
        driver.end_startup_diagnostics(timeout_s=0.02)
        assert len(calls) == 1
    finally:
        release.set()
        assert finished.wait(1)


def test_close_worker_failure_keeps_capture_active_and_reports_the_trace(native, monkeypatch):
    driver = native.driver
    driver.begin_startup_diagnostics(timeout_s=0.5)

    def no_worker(*args, **kwargs):
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(driver, "_call_locked_with_timeout", no_worker)
    report = driver.end_startup_diagnostics(timeout_s=0.5)
    assert "can't start new thread" in report
    assert "Opening debug file" in report
    assert driver._startup_log_path is not None
    assert driver._transport_poisoned is False


def test_trimming_failure_does_not_hide_captured_bytes(native, monkeypatch):
    driver = native.driver
    driver.begin_startup_diagnostics(timeout_s=0.5)
    native.state.path.write_bytes(b"Data read: '5Yx'\n" + b"x" * driver._STARTUP_LOG_MAX_BYTES)
    real_open = Path.open

    def read_only(path, mode="r", *args, **kwargs):
        if mode == "r+b":
            raise PermissionError("cannot trim")
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", read_only)
    report = driver.end_startup_diagnostics(timeout_s=0.5)
    assert "cannot trim" in report and "Data read: '5Yx'" in report


def test_nested_begin_does_not_overwrite_active_evidence(native):
    driver = native.driver
    driver.begin_startup_diagnostics(timeout_s=0.5)
    native.state.path.write_bytes(b"Data read: '5Yx'\n")
    assert "already active" in driver.begin_startup_diagnostics(timeout_s=0.5)
    assert [name for name, _ in native.calls] == ["open"]
    assert "Data read: '5Yx'" in driver.end_startup_diagnostics(timeout_s=0.5)


def test_missing_native_file_is_a_diagnostic_warning(native):
    native.driver.begin_startup_diagnostics(timeout_s=0.5)
    native.state.path.unlink()
    report = native.driver.end_startup_diagnostics(timeout_s=0.5)
    assert "unavailable" in report.lower()
    assert [name for name, _ in native.calls] == ["open", "close"]


def attach_capture(rig):
    calls = rig.device.calls

    def begin(**kwargs):
        calls.append(("capture", "begin"))
        return "capture opened"

    def end(**kwargs):
        calls.append(("capture", "end"))
        return "[DMMR native] Data read: '5Yx'"

    rig.device.begin_startup_diagnostics = begin
    rig.device.end_startup_diagnostics = end


@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_failed_start_captures_first_error_and_cleanup_in_explorer_log(rig, cleanup_fails):
    attach_capture(rig)
    rig.device.reject_cleanup = cleanup_fails
    start(rig)
    calls = rig.device.calls
    assert calls[0] == ("capture", "begin")
    assert calls[-1] == ("capture", "end")
    assert calls.count(("range", 5, True)) == 1, "No diagnostic retry"
    assert ("enable", False) in calls
    assert any("Data read: '5Yx'" in message for message in rig.logs)
    assert rig.parent.on is cleanup_fails


def test_success_closes_trace_before_continuous_acquisition(rig):
    attach_capture(rig)
    rig.device.fail_start = None
    rig.controller.startAcquisition = lambda: rig.device.calls.append(("acquisition", "start"))
    start(rig)
    assert rig.device.calls[-2:] == [("capture", "end"), ("acquisition", "start")]
    assert "DMMR acquisition enabled." in rig.logs


@pytest.mark.parametrize("which", ["begin", "end"])
def test_python_logging_error_does_not_change_startup_outcome(rig, which):
    attach_capture(rig)
    rig.device.fail_start = None

    def broken(**kwargs):
        raise OSError("diagnostic file unavailable")

    setattr(rig.device, f"{which}_startup_diagnostics", broken)
    start(rig)
    assert "DMMR acquisition enabled." in rig.logs
    assert any("diagnostic file unavailable" in message for message in rig.logs)


@pytest.mark.parametrize("poisoned", [False, True])
def test_unclosed_capture_never_turns_into_continuous_logging(rig, poisoned):
    attach_capture(rig)
    rig.device.fail_start = None
    rig.controller.startAcquisition = lambda: pytest.fail("capture still open")

    def unclosed(**kwargs):
        rig.device._transport_poisoned = poisoned
        rig.device._startup_log_path = Path("still-open.log")
        if poisoned:
            rig.device.reject_cleanup = True
        return "native capture not closed"

    rig.device.end_startup_diagnostics = unclosed
    start(rig)
    assert "DMMR acquisition enabled." not in rig.logs
    assert ("enable", False) in rig.device.calls
    assert rig.controller.acquiring is False
    assert rig.parent.on is poisoned


def test_plain_off_does_not_start_capture(rig):
    attach_capture(rig)
    rig.parent.on = False
    rig.controller.toggleOn()
    assert not any(call[0] == "capture" for call in rig.device.calls)
