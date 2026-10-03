"""Execute the passive ESI notebook with a strict fake DLL, never hardware."""
from __future__ import annotations

import ast
import copy
import hashlib
import json
import math
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
NOTEBOOK = ROOT / "esi/esi_heater_readonly_probe.ipynb"


def notebook_code():
    notebook = json.loads(NOTEBOOK.read_text(encoding="utf-8"))
    cells = [c for c in notebook["cells"] if c["cell_type"] == "code"]
    assert notebook["nbformat"] == 4 and notebook["nbformat_minor"] == 5
    assert len(cells) == 1
    assert cells[0]["execution_count"] is None and cells[0]["outputs"] == []
    return "".join(cells[0]["source"])


@pytest.fixture
def ns():
    tree = ast.parse(notebook_code())
    assert isinstance(tree.body[-1], ast.Expr)
    assert ast.unparse(tree.body[-1]) == "main()"
    tree.body.pop()  # Load definitions, without opening a physical port.
    namespace = {}
    exec(compile(tree, str(NOTEBOOK), "exec"), namespace)
    return namespace


class FakeESI:
    """The controller can be ON while the independent heater gate is OFF."""

    def __init__(self):
        self.calls = []
        self.thread_ids = set()
        self.entered = threading.Event()
        self.release = threading.Event()
        self.block_method = None
        self.failures = {}
        self.values = {
            "get_sw_version": 0x0100,
            "open_port": 0, "close_port": 0,
            "get_dev_type": (0, 0x8ED6),
            "get_enable": (0, True),
            "get_main_state": (0, "0x0", "STATE_ON"),
            "get_device_state": (0, "0x0", ["DEVST_OK"]),
            "get_interlock_enable": (0, 5),
            "get_interlock_state": (0, "0x100", ["IS_HTCTRL_ILOCK1_CURR"]),
            "get_inputs": (0, True, False, True),
            "get_power_monitors": (0, True, True),
            "get_voltage_state": (0, "0x37", ["VS_3V3_OK"]),
            "get_temperature_state": (0, "0x0", []),
            "get_product_id": (0, "ESI-CTRL"),
            "get_product_no": (0, 123456),
            "get_fw_version": (0, 0x0100),
            "get_fw_date": (0, "2026-07-13"),
            "get_hw_type": (0, 1),
            "get_hw_version": (0, 0x0100),
            "get_config_values": (0, 1023, 53, 202),
            "get_current_config": (0, list(range(53))),
            "get_module_dev_type": (0, 0xDB1C),
            "get_module_product_id": (0, "HEAT-CTRL-2410"),
            "get_module_product_no": (0, 456789),
            "get_module_fw_version": (0, 0x0100),
            "get_module_hw_version": (0, 0x0100),
            "get_module_activation_state": (0, False),
            "get_module_state": (0, 0x8000),
            "get_module_led_data": (0, True, True, False),
            "get_module_data_ready_flags": (0, 1),
            "get_heat_ctrl_ilock_state": (0, 9),
            "get_heat_ctrl_hw_limits": (0, 24.0, 10.0, 200.0, 180.0),
            "get_heat_ctrl_voltage_limit": (0, 0.0),
            "get_heat_ctrl_current_limit": (0, 0.0),
            "get_heat_ctrl_power_limit": (0, 0.0),
            "get_heat_ctrl_heater_temperature": (0, 50.0),
            "get_heat_ctrl_output_voltage": (0, 8.0),
            "get_heat_ctrl_heater_power": (0, 20.0),
            "get_heat_ctrl_monitoring": (0, True, 0.0, 0.0, 0.0, 23.0),
            "get_heat_ctrl_housekeeping": (0, True, 3.3, 32.0, 5.0, 24.0, 30.0),
        }
        self.original_values = copy.deepcopy(self.values)

    def describe_error(self, status):
        return f"simulated status {status}"

    def __getattr__(self, name):
        if name not in self.values:
            raise AssertionError(f"Forbidden or unmodelled hardware call: {name}")

        def native(*args):
            self.calls.append((name, args))
            self.thread_ids.add(threading.get_ident())
            if name.startswith("get_module_"):
                assert args == (0,), "Only the heater module may be addressed"
            if name == self.block_method:
                self.entered.set()
                assert self.release.wait(3), "Test must release its fake native call"
            result = self.failures.get(name, self.values[name])
            if isinstance(result, BaseException):
                raise result
            return copy.deepcopy(result)
        return native


def run_probe(ns, tmp_path, device):
    probe = ns["PassiveProbe"](tmp_path / "report.json", 16, {"simulated": True})
    probe.run(lambda: device)
    return probe


def load_report(probe):
    # Disallow NaN/Infinity tokens; hardware non-finite readings must become null.
    return json.loads(probe.path.read_text(), parse_constant=lambda token: pytest.fail(token))


def test_real_notebook_is_one_clean_code_cell_and_passive(ns):
    tree = ast.parse(notebook_code())
    called = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert not {name for name in called if name.startswith(("set_", "purge", "restart", "rescan"))}
    assert "connect" not in called and "disconnect" not in called
    assert "get_io_state" not in ns["READ_FIELDS"]  # This getter clears its status.
    assert "get_comm_error" not in ns["READ_FIELDS"]


def test_on_controller_does_not_imply_heater_on_and_nothing_is_written(ns, tmp_path):
    device = FakeESI()
    probe = run_probe(ns, tmp_path, device)
    report = load_report(probe)
    assert report["outcome"] == "complete"
    assert report["port"] == "closed" and probe.reusable
    assert report["reads"]["controller/enable"]["data"] == {"enabled": True}
    assert report["reads"]["heater/activation_state"]["data"] == {"enabled": False}
    assert report["decoded"]["heater_state_bits"] == {
        "temperature_control_active": False, "module_active": False, "device_active": True,
    }
    assert report["decoded"]["interlocks_monitored"] == {
        "heater_1": True, "heater_2": False, "controller_front": True, "controller_rear": False,
    }
    assert report["decoded"]["heater_interlock_bits"] == {
        "interlock_1_current": True, "interlock_2_current": False,
        "interlock_1_last": False, "interlock_2_last": True,
    }
    assert report["reads"]["heater/led_data"]["data"] == {"red": True, "green": True, "blue": False}
    assert report["reads"]["heater/heater_power"]["data"] == {"target_heater_power_w": 20.0}
    assert report["reads"]["heater/monitoring"]["data"]["current_a"] == 0.0
    assert report["decoded"]["config_bytes_equal"] is True
    assert len(device.thread_ids) == 1
    assert device.values == device.original_values
    assert [n for n, _ in device.calls if not n.startswith("get_")] == ["open_port", "close_port"]
    assert all(c["state"] == "returned" for c in report["calls"])
    assert all(c["elapsed_s"] >= 0 for c in report["calls"])


@pytest.mark.parametrize("method", ["set_enable", "set_module_activation_state", "set_comspeed", "load_current_config", "purge", "get_io_state"])
def test_allowlist_rejects_output_and_state_clearing_calls(ns, tmp_path, method):
    device = FakeESI()
    probe = ns["PassiveProbe"](tmp_path / "r.json", 16)
    probe.device = device
    with pytest.raises(ValueError, match="Forbidden"):
        probe.call(method)
    assert device.calls == []


@pytest.mark.parametrize("result", [
    (0, False, 1., 2., 3., 99.),
    (0, True, 1., 2., math.nan, 25.),
    (0, True, 1., 2., 3., math.inf),
    (0, True, 1., 2., 3., -273.),
    (0, True, 1., 2., 3., 181.),
])
def test_invalid_monitoring_preserves_raw_evidence_not_a_measurement(ns, tmp_path, result):
    device = FakeESI()
    device.values["get_heat_ctrl_monitoring"] = result
    probe = run_probe(ns, tmp_path, device)
    report = load_report(probe)
    monitor = report["reads"]["heater/monitoring"]
    assert monitor["measurement_usable"] is False
    assert all(value is None for key, value in monitor["data"].items() if key != "valid")
    assert "raw_values" in monitor and monitor["status"] == 0
    assert report["outcome"] == "partial"


@pytest.mark.parametrize("method,result", [
    ("get_module_activation_state", (-101, True)),
    ("get_heat_ctrl_power_limit", (-14, 100.0)),
    ("get_module_led_data", AttributeError("optional export missing")),
])
def test_returned_unavailable_reads_are_not_decoded(ns, tmp_path, method, result):
    device = FakeESI()
    device.failures[method] = result
    probe = run_probe(ns, tmp_path, device)
    report = load_report(probe)
    record = next(c for c in report["calls"] if c["method"] == method)
    assert record["data"] is None
    assert report["outcome"] == "partial" and report["port"] == "closed"


@pytest.mark.parametrize("method,result", [
    ("get_dev_type", (0, 0xDEAD)),
    ("get_module_dev_type", (0, 0xBEEF)),
    ("get_module_dev_type", (-101, 0xDB1C)),
])
def test_wrong_or_unavailable_identity_never_enables_or_reads_heater(ns, tmp_path, method, result):
    device = FakeESI()
    device.failures[method] = result
    probe = run_probe(ns, tmp_path, device)
    assert load_report(probe)["outcome"] == "partial"
    assert not any(name.startswith("get_heat_ctrl_") for name, _ in device.calls)
    assert device.calls[-1][0] == "close_port"


def test_unknown_config_size_does_not_call_fixed_size_buffer(ns, tmp_path):
    device = FakeESI()
    device.values["get_config_values"] = (0, 1023, 60, 202)
    probe = run_probe(ns, tmp_path, device)
    assert not any(n == "get_current_config" for n, _ in device.calls)
    assert load_report(probe)["decoded"]["config_bytes_equal"] is None


@pytest.mark.parametrize("method", ["open_port", "get_heat_ctrl_monitoring", "close_port"])
def test_blocked_native_call_is_saved_and_never_followed_by_close_or_retry(ns, tmp_path, method):
    original_call = ns["PassiveProbe"].call

    def selected_deadline(self, name, *args, **kwargs):
        if name == method:
            key = "OPEN_TIMEOUT_S" if name in ("open_port", "close_port") else "READ_TIMEOUT_S"
            ns[key] = 0.02
        return original_call(self, name, *args, **kwargs)

    ns["PassiveProbe"].call = selected_deadline
    device = FakeESI()
    device.block_method = method
    probe = run_probe(ns, tmp_path, device)
    try:
        assert device.entered.is_set() and probe.thread.is_alive()
        pending = load_report(probe)
        assert pending["outcome"] == "aborted"
        assert pending["calls"][-1]["state"] == "running"
        assert pending["calls"][-1]["deadline_exceeded"] is True
        calls_at_timeout = list(device.calls)
    finally:
        device.release.set()
        probe.thread.join(3)
    assert not probe.thread.is_alive()
    assert device.calls == calls_at_timeout  # Even late Open cannot start another command.
    late = load_report(probe)
    assert late["calls"][-1]["returned_after_stop"] is True
    assert late["calls"][-1]["data"] is None
    assert not probe.reusable and late["requires_kernel_restart"]
    assert late["port"] == ("closed" if method == "close_port" else "open_attempted" if method == "open_port" else "open")


@pytest.mark.parametrize("status", [-7, -8, -9, -10, -11, -12])
def test_returned_transport_failure_is_not_followed_by_native_cleanup(ns, tmp_path, status):
    device = FakeESI()
    device.failures["get_heat_ctrl_monitoring"] = (status, True, 1., 2., 3., 80.)
    probe = run_probe(ns, tmp_path, device)
    report = load_report(probe)
    assert report["outcome"] == "aborted" and not probe.reusable
    assert device.calls[-1][0] == "get_heat_ctrl_monitoring"
    assert report["reads"]["heater/monitoring"]["data"] is None


@pytest.mark.parametrize("method", ["open_port", "get_heat_ctrl_monitoring", "close_port"])
def test_interrupt_inside_native_call_never_attempts_cleanup(ns, tmp_path, method):
    device = FakeESI()
    device.failures[method] = KeyboardInterrupt()
    probe = run_probe(ns, tmp_path, device)
    assert device.calls[-1][0] == method
    assert load_report(probe)["outcome"] == "aborted"
    assert not probe.reusable


def test_failed_open_cannot_close_a_possibly_unowned_dll_session(ns, tmp_path):
    device = FakeESI()
    device.failures["open_port"] = -2
    probe = run_probe(ns, tmp_path, device)
    assert [name for name, _ in device.calls] == ["get_sw_version", "open_port"]
    report = load_report(probe)
    assert not probe.reusable and report["outcome"] == "aborted"
    assert "port ownership" in report["reason"]
    assert report["calls"][-1]["status"] == -2


def test_unconfirmed_close_is_never_replayed(ns, tmp_path):
    device = FakeESI()
    device.failures["close_port"] = -3
    probe = run_probe(ns, tmp_path, device)
    assert not probe.reusable
    assert sum(name == "close_port" for name, _ in device.calls) == 1
    assert load_report(probe)["port"] == "close_attempted"


def test_report_must_be_writable_before_any_native_call(ns, tmp_path):
    device = FakeESI()
    parent = tmp_path / "file_not_directory"
    parent.write_text("occupied")
    probe = ns["PassiveProbe"](parent / "r.json", 16)
    with pytest.raises(OSError):
        probe.run(lambda: device)
    assert device.calls == [] and probe.thread is None


def test_rerun_guard_precedes_platform_loading_and_port_access(ns, tmp_path):
    old = ns["PassiveProbe"](tmp_path / "r.json", 16)
    old.reusable = False
    ns["_ESI_HEATER_READONLY_SESSION"] = old
    with pytest.raises(RuntimeError, match="Previous DLL session"):
        ns["main"]()


def test_non_windows_fails_before_loading_dll(ns):
    ns["sys"] = SimpleNamespace(platform="linux", version=sys.version)
    with pytest.raises(RuntimeError, match="64-bit Windows"):
        ns["main"]()


@pytest.mark.parametrize("port", [True, 0, 256, "COM16"])
def test_invalid_com_is_not_wrapped_into_a_different_port(ns, port):
    ns["sys"] = SimpleNamespace(platform="win32", version=sys.version)
    ns["COM_PORT"] = port
    with pytest.raises(ValueError, match="COM_PORT"):
        ns["main"]()


def test_main_finds_installed_bundle_and_runs_with_private_loader(ns, tmp_path, monkeypatch):
    plugin = tmp_path / "esi"
    vendor = plugin / "vendor/runtime/esi/vendor/x64"
    vendor.mkdir(parents=True)
    (plugin / "esi_plugin.py").write_text("# Fixture only\n")
    base_path = plugin / "vendor/runtime/esi/esi_base.py"
    base_path.write_text("# Fixture injected below; no native code\n")
    catalog = plugin / "vendor/runtime/error_codes.json"
    catalog.write_text('{}')
    dll = vendor / "COM-ESI-CTRL.dll"
    dll.write_bytes((ROOT / "esi/vendor/runtime/esi/vendor/x64/COM-ESI-CTRL.dll").read_bytes())
    device = FakeESI()
    factory_args = []

    def factory(**kwargs):
        factory_args.append(kwargs)
        return device

    module = SimpleNamespace(ESIBase=factory)
    imports = []

    def spec(name, path):
        imports.append((name, path))
        return SimpleNamespace(loader=SimpleNamespace(exec_module=lambda value: None))

    module_name = "_esibd_bundled_esi_heater_readonly_base"
    monkeypatch.setitem(sys.modules, module_name, None)  # Restore on fixture teardown.
    ns["sys"] = SimpleNamespace(platform="win32", version=sys.version, modules=sys.modules)
    ns["importlib"] = SimpleNamespace(util=SimpleNamespace(
        spec_from_file_location=spec, module_from_spec=lambda value: module,
    ))
    monkeypatch.chdir(tmp_path)  # Automatic discovery: cwd/esi, no manual paths.
    before_path = list(sys.path)
    ns["main"]()
    assert sys.path == before_path
    assert imports == [(module_name, base_path)]
    assert sys.modules[module_name] is module
    assert factory_args == [{"com": 16, "dll_path": dll, "error_codes_path": catalog}]
    reports = list((plugin / "logs/esi_heater_readonly").glob("*.json"))
    assert len(reports) == 1 and not list(plugin.rglob("*.tmp"))
    report = json.loads(reports[0].read_text())
    assert report["metadata"]["dll_sha256"] == hashlib.sha256(dll.read_bytes()).hexdigest()
    assert report["port"] == "closed" and report["outcome"] == "complete"


def test_current_call_is_persisted_before_entering_native_code(ns, tmp_path):
    device = FakeESI()
    probe = ns["PassiveProbe"](tmp_path / "r.json", 16)
    seen = []

    class CheckedDevice:
        def __getattr__(self, name):
            method = getattr(device, name)
            if name == "describe_error":
                return method

            def checked(*args):
                report = load_report(probe)
                assert report["calls"][-1]["method"] == name
                assert report["calls"][-1]["state"] == "running"
                seen.append(name)
                return method(*args)
            return checked

    probe.run(CheckedDevice)
    assert seen == [name for name, _ in device.calls]
    assert len(seen) > 30


def test_jupyter_interrupt_while_native_worker_is_blocked_does_not_close(ns, tmp_path):
    device = FakeESI()
    device.block_method = "get_heat_ctrl_monitoring"

    class InterruptThread(threading.Thread):
        interrupted = False

        def join(self, timeout=None):
            if not self.interrupted:
                assert device.entered.wait(3)
                self.interrupted = True
                raise KeyboardInterrupt()
            return super().join(timeout)

    ns["threading"] = SimpleNamespace(Thread=InterruptThread, RLock=threading.RLock)
    probe = run_probe(ns, tmp_path, device)
    try:
        assert probe.thread.is_alive() and probe.stop
        assert "User interrupt" in load_report(probe)["reason"]
        calls_at_interrupt = list(device.calls)
    finally:
        device.release.set()
        probe.thread.join(3)
    assert device.calls == calls_at_interrupt
    assert device.calls[-1][0] == "get_heat_ctrl_monitoring"
    assert not probe.reusable
