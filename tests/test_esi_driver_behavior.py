"""Safety and validation checks for the bundled ESI driver."""

from __future__ import annotations

import ctypes
import hashlib
import importlib.util
import json
import logging
import sys
import threading
import types
from pathlib import Path

import pytest


RUNTIME_DIR = Path(__file__).resolve().parents[1] / "esi" / "vendor" / "runtime"
RUNTIME_NAME = "_esi_driver_test_runtime"
VENDOR_DIR = RUNTIME_DIR / "esi" / "vendor"
ACTIVATION_NOTEBOOK = Path(__file__).resolve().parents[1] / "esi" / "esi_hv_activation_probe.ipynb"
HARDWARE_NOTEBOOK = Path(__file__).resolve().parents[1] / "esi" / "esi_hardware_probe.ipynb"


def _load_runtime():
    for name in tuple(sys.modules):
        if name == RUNTIME_NAME or name.startswith(f"{RUNTIME_NAME}."):
            sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(
        RUNTIME_NAME,
        RUNTIME_DIR / "__init__.py",
        submodule_search_locations=[str(RUNTIME_DIR)],
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[RUNTIME_NAME] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def driver_modules():
    runtime = _load_runtime()
    driver_module = sys.modules[f"{RUNTIME_NAME}.esi.esi"]
    base_module = sys.modules[f"{RUNTIME_NAME}.esi.esi_base"]
    driver_module._ESIController._connected_instance = None
    yield runtime, driver_module, base_module
    driver_module._ESIController._connected_instance = None


def _controller(driver_module):
    controller = object.__new__(driver_module._ESIController)
    controller.device_id = "test_esi"
    controller.com = 14
    controller.baudrate = 230400
    controller.connected = False
    controller._transport_poisoned = False
    controller._transport_error = None
    controller._module_inventory = {}
    controller._hv_measurement_requests = {}
    controller.thread_lock = threading.Lock()
    controller.logger = logging.getLogger("test_esi_driver")
    controller.err_dict = {"-15": "Wrong argument"}
    return controller


def test_connect_validates_identity_and_forces_known_off_state(
    driver_modules,
    monkeypatch,
):
    _runtime, driver_module, base_module = driver_modules
    controller = _controller(driver_module)
    calls = []
    module_active = {1: True, 2: True}

    monkeypatch.setattr(base_module.ESIBase, "open_port", lambda self, com: calls.append(("open", com)) or 0)
    monkeypatch.setattr(base_module.ESIBase, "set_comspeed", lambda self, baud: calls.append(("baud", baud)) or (0, baud))
    monkeypatch.setattr(base_module.ESIBase, "get_dev_type", lambda self: calls.append(("device_type",)) or (0, self.DEVICE_TYPE))
    monkeypatch.setattr(base_module.ESIBase, "set_enable", lambda self, state: calls.append(("enable", state)) or 0)
    monkeypatch.setattr(
        base_module.ESIBase,
        "set_heat_ctrl_heater_temperature",
        lambda self, value: calls.append(("heat_target", value)) or (0, value),
    )
    monkeypatch.setattr(base_module.ESIBase, "set_hv_supply_target_output_voltage", lambda self, address, value: calls.append(("target", address, value)) or 0)
    monkeypatch.setattr(
        base_module.ESIBase,
        "set_module_activation_state",
        lambda self, address, state: (
            calls.append(("module", address, state)),
            module_active.__setitem__(address, state),
            self.NO_ERR,
        )[-1],
    )
    monkeypatch.setattr(
        base_module.ESIBase,
        "get_hv_supply_params_pwm",
        lambda self, address: (
            calls.append(("pwm", address)) or 0,
            1.0,
            0.5,
            0.0,
            0.0,
            0.0,
            0.0,
            module_active[address],
            0,
        ),
    )
    monkeypatch.setattr(base_module.ESIBase, "update_module_presence", lambda self: 0)
    monkeypatch.setattr(
        base_module.ESIBase,
        "get_module_presence",
        lambda self: (0, True, 3, [1, 1, 1, 0, 1]),
    )
    monkeypatch.setattr(
        base_module.ESIBase,
        "get_module_dev_type",
        lambda self, address: (
            0,
            self.MODULE_HTCTRL_TYPE if address == 0 else self.MODULE_HVPS_TYPE,
        ),
    )

    assert controller.connect(timeout_s=0.5) is True
    assert calls[:3] == [("open", 14), ("baud", 230400), ("device_type",)]
    assert calls[3:] == [
        ("enable", False),
        ("enable", True),
        ("heat_target", 0.0),
        ("target", 1, 0.0),
        ("module", 1, False),
        ("pwm", 1),
        ("target", 2, 0.0),
        ("module", 2, False),
        ("pwm", 2),
        ("enable", False),
    ]


def test_connect_rejects_wrong_controller_type(driver_modules, monkeypatch):
    _runtime, driver_module, base_module = driver_modules
    controller = _controller(driver_module)
    closed = []
    monkeypatch.setattr(base_module.ESIBase, "open_port", lambda self, com: 0)
    monkeypatch.setattr(base_module.ESIBase, "set_comspeed", lambda self, baud: (0, baud))
    monkeypatch.setattr(base_module.ESIBase, "get_dev_type", lambda self: (0, 0xFFFF))
    monkeypatch.setattr(base_module.ESIBase, "close_port", lambda self: closed.append(True) or 0)

    with pytest.raises(RuntimeError, match="device type mismatch"):
        controller.connect(timeout_s=0.5)

    assert closed == [True]
    assert controller.connected is False
    assert driver_module._ESIController._connected_instance is None


def test_discovery_requires_both_expected_hv_modules(driver_modules, monkeypatch):
    _runtime, driver_module, base_module = driver_modules
    controller = _controller(driver_module)
    controller.connected = True
    monkeypatch.setattr(base_module.ESIBase, "update_module_presence", lambda self: 0)
    monkeypatch.setattr(
        base_module.ESIBase,
        "get_module_presence",
        lambda self: (0, True, 2, [1, 1, 0, 0, 1]),
    )
    monkeypatch.setattr(
        base_module.ESIBase,
        "get_module_dev_type",
        lambda self, address: (
            0,
            self.MODULE_HTCTRL_TYPE if address == 0 else self.MODULE_HVPS_TYPE,
        ),
    )

    with pytest.raises(RuntimeError, match="module 2 was not detected"):
        controller.discover_modules(timeout_s=0.5)


def test_voltage_guard_enforces_unsigned_3kv_vendor_range(driver_modules):
    _runtime, driver_module, _base_module = driver_modules
    controller = _controller(driver_module)

    assert controller._validate_voltage(3000) == 3000
    with pytest.raises(ValueError, match="between 0 and 3000 V"):
        controller._validate_voltage(3000.1)
    with pytest.raises(ValueError, match="finite"):
        controller._validate_voltage(float("nan"))
    with pytest.raises(ValueError, match="finite"):
        controller._validate_voltage(float("inf"))
    with pytest.raises(ValueError, match="between 0 and 3000 V"):
        controller._validate_voltage(-1)


def test_hv_ctypes_signatures_match_vendor_header(driver_modules):
    _runtime, _driver_module, base_module = driver_modules

    class FakeFunction:
        argtypes = None
        restype = None

    names = (
        "COM_ESI_CTRL_SetEnable",
        "COM_ESI_CTRL_GetEnable",
        "COM_ESI_CTRL_SetModuleActivationState",
        "COM_ESI_CTRL_GetModuleActivationState",
        "COM_ESI_CTRL_GetModuleLEDData",
        "COM_ESI_CTRL_GetBaseHousekeeping",
        "COM_ESI_CTRL_GetHVsupplyMeasRanges",
        "COM_ESI_CTRL_SetHVsupplyMeasRanges",
        "COM_ESI_CTRL_GetHVsupplyOutputVoltage",
        "COM_ESI_CTRL_GetHVsupplyOutputCurrent",
        "COM_ESI_CTRL_GetHVsupplyPhase",
        "COM_ESI_CTRL_GetHVsupplyTargetOutputVoltage",
        "COM_ESI_CTRL_SetHVsupplyTargetOutputVoltage",
        "COM_ESI_CTRL_GetHVsupplyParamsPWM",
        "COM_ESI_CTRL_GetCompleteState",
        "COM_ESI_CTRL_GetConfigValues",
        "COM_ESI_CTRL_GetCurrentConfig",
        "COM_ESI_CTRL_SetCurrentConfig",
    )
    base = object.__new__(base_module.ESIBase)
    base.esi_dll = types.SimpleNamespace(
        **{name: FakeFunction() for name in names}
    )

    base._configure_hv_dll_signatures()

    assert base.esi_dll.COM_ESI_CTRL_SetHVsupplyTargetOutputVoltage.argtypes == [
        ctypes.c_uint,
        ctypes.c_double,
    ]
    assert base.esi_dll.COM_ESI_CTRL_SetHVsupplyMeasRanges.argtypes == [
        ctypes.c_uint,
        ctypes.c_bool,
        ctypes.c_bool,
    ]
    assert base.esi_dll.COM_ESI_CTRL_GetHVsupplyMeasRanges.argtypes == [
        ctypes.c_uint,
        ctypes.POINTER(ctypes.c_bool),
        ctypes.POINTER(ctypes.c_bool),
    ]
    assert base.esi_dll.COM_ESI_CTRL_GetBaseHousekeeping.argtypes == [
        ctypes.POINTER(ctypes.c_bool),
        ctypes.POINTER(ctypes.c_double),
        ctypes.POINTER(ctypes.c_double),
    ]
    assert base.esi_dll.COM_ESI_CTRL_GetHVsupplyOutputVoltage.argtypes == [
        ctypes.c_uint,
        ctypes.POINTER(ctypes.c_bool),
        ctypes.POINTER(ctypes.c_double),
    ]
    assert base.esi_dll.COM_ESI_CTRL_GetModuleLEDData.argtypes == [
        ctypes.c_uint,
        ctypes.POINTER(ctypes.c_bool),
        ctypes.POINTER(ctypes.c_bool),
        ctypes.POINTER(ctypes.c_bool),
    ]
    assert base.esi_dll.COM_ESI_CTRL_GetCompleteState.argtypes == [
        ctypes.POINTER(ctypes.c_ubyte),
        ctypes.POINTER(ctypes.c_ubyte),
        ctypes.POINTER(ctypes.c_ubyte),
        ctypes.POINTER(ctypes.c_ubyte),
        ctypes.POINTER(ctypes.c_ubyte),
        ctypes.POINTER(ctypes.c_uint16),
        ctypes.POINTER(ctypes.c_uint16),
        ctypes.POINTER(ctypes.c_ubyte),
        ctypes.POINTER(ctypes.c_uint16),
    ]
    assert base.esi_dll.COM_ESI_CTRL_GetConfigValues.argtypes == [
        ctypes.POINTER(ctypes.c_uint),
        ctypes.POINTER(ctypes.c_uint),
        ctypes.POINTER(ctypes.c_uint),
    ]
    assert base.esi_dll.COM_ESI_CTRL_GetCurrentConfig.argtypes == [
        ctypes.POINTER(ctypes.c_ubyte),
    ]
    assert base.esi_dll.COM_ESI_CTRL_SetCurrentConfig.argtypes == [
        ctypes.POINTER(ctypes.c_ubyte),
    ]
    assert all(getattr(base.esi_dll, name).restype is ctypes.c_int for name in names)


def test_low_voltage_reaches_vendor_api_without_scaling(driver_modules):
    _runtime, _driver_module, base_module = driver_modules
    base = object.__new__(base_module.ESIBase)
    calls = []

    def set_target(address, voltage):
        calls.append((address.value, voltage.value))
        return 0

    base.esi_dll = types.SimpleNamespace(
        COM_ESI_CTRL_SetHVsupplyTargetOutputVoltage=set_target,
    )

    assert base.set_hv_supply_target_output_voltage(1, 1.0) == 0
    assert base.set_hv_supply_target_output_voltage(2, 30.0) == 0
    assert calls == [(1, 1.0), (2, 30.0)]


def test_bundled_vendor_files_are_the_july_2026_revision():
    dll_path = VENDOR_DIR / "x64" / "COM-ESI-CTRL.dll"
    header = (VENDOR_DIR / "COM-ESI-CTRL.h").read_text(encoding="utf-8")

    assert hashlib.sha256(dll_path.read_bytes()).hexdigest() == (
        "991637c4dab5ed6d2801543696b462c038c9d7504caef0f86d9a2ba208232456"
    )
    assert "COM_ESI_CTRL_GetHVsupplyMeasRanges" in header
    assert "COM_ESI_CTRL_GetCompleteState" in header
    assert "COM_ESI_CTRL_SetActivationState (" not in header
    assert "COM_ESI_CTRL_GetActivationState (" not in header


def test_updated_base_and_complete_state_abis(driver_modules):
    _runtime, _driver_module, base_module = driver_modules
    base = object.__new__(base_module.ESIBase)
    base.MODULE_NUM = 4

    def get_base_housekeeping(valid, voltage, temperature):
        valid._obj.value = True
        voltage._obj.value = 3.31
        temperature._obj.value = 42.5
        return 0

    def get_complete_state(
        data_flags,
        device_state,
        voltage_state,
        temperature_state,
        fan_state,
        interlock_state,
        state,
        module_data_flags,
        module_states,
    ):
        data_flags._obj.value = 0x05
        device_state._obj.value = 0x00
        voltage_state._obj.value = 0x37
        temperature_state._obj.value = 0x00
        fan_state._obj.value = 0x0F
        interlock_state._obj.value = 0xF00C
        state._obj.value = 0x0001
        module_data_flags[:] = [1, 2, 3, 4, 5]
        module_states[:] = [0x100, 0x200, 0x300, 0x400, 0x500]
        return 0

    base.esi_dll = types.SimpleNamespace(
        COM_ESI_CTRL_GetBaseHousekeeping=get_base_housekeeping,
        COM_ESI_CTRL_GetCompleteState=get_complete_state,
    )

    assert base.get_base_housekeeping() == (0, True, 3.31, 42.5)
    assert base.get_complete_state() == (
        0,
        0x05,
        0x00,
        0x37,
        0x00,
        0x0F,
        0xF00C,
        0x0001,
        [1, 2, 3, 4, 5],
        [0x100, 0x200, 0x300, 0x400, 0x500],
    )


def test_current_configuration_api(driver_modules):
    _runtime, _driver_module, base_module = driver_modules
    base = object.__new__(base_module.ESIBase)

    def get_config_values(max_config, data_size, name_size):
        max_config._obj.value = 1023
        data_size._obj.value = 53
        name_size._obj.value = 202
        return 0

    def get_current_config(config):
        config[:] = range(53)
        return 0

    applied = []

    def set_current_config(config):
        applied.append(list(config))
        return 0

    base.esi_dll = types.SimpleNamespace(
        COM_ESI_CTRL_GetConfigValues=get_config_values,
        COM_ESI_CTRL_GetCurrentConfig=get_current_config,
        COM_ESI_CTRL_SetCurrentConfig=set_current_config,
    )

    assert base.get_config_values() == (0, 1023, 53, 202)
    assert base.get_current_config() == (0, list(range(53)))
    assert base.set_current_config(range(53)) == 0
    assert applied == [list(range(53))]
    with pytest.raises(ValueError, match="53 bytes"):
        base.set_current_config(range(52))


def test_volatile_hv_step_configuration_changes_only_both_step_fields(
    driver_modules,
    monkeypatch,
):
    _runtime, driver_module, base_module = driver_modules
    controller = _controller(driver_module)
    controller.connected = True
    current = [0] * 53
    writes = []

    monkeypatch.setattr(
        base_module.ESIBase,
        "get_current_config",
        lambda _self: (0, list(current)),
    )

    def set_current_config(_self, requested):
        writes.append(list(requested))
        current[:] = requested
        return 0

    monkeypatch.setattr(
        base_module.ESIBase,
        "set_current_config",
        set_current_config,
    )
    monkeypatch.setattr(base_module.ESIBase, "get_enable", lambda _self: (0, False))
    monkeypatch.setattr(
        base_module.ESIBase,
        "get_hv_supply_target_output_voltage",
        lambda _self, _address: (0, 0.0),
    )
    monkeypatch.setattr(
        base_module.ESIBase,
        "get_hv_supply_params_pwm",
        lambda _self, _address: (
            0,
            5e-6,
            0.0,
            0.0,
            1.5e-6,
            0.0,
            0.0,
            False,
            0,
        ),
    )

    applied = controller.configure_hv_max_voltage_steps(10.008, timeout_s=0.5)

    assert applied == {1: 10.008, 2: 10.008}
    assert len(writes) == 1
    changed_offsets = [
        index for index, value in enumerate(writes[0]) if value != 0
    ]
    assert changed_offsets == [21, 22, 33, 34]
    assert writes[0][21:25] == [0x18, 0x27, 0x00, 0x00]
    assert writes[0][33:37] == [0x18, 0x27, 0x00, 0x00]


def test_volatile_hv_step_configuration_requires_safe_off(
    driver_modules,
    monkeypatch,
):
    _runtime, driver_module, base_module = driver_modules
    controller = _controller(driver_module)
    controller.connected = True
    current = [0] * 53
    current[0] = 1
    monkeypatch.setattr(
        base_module.ESIBase,
        "get_current_config",
        lambda _self: (0, current),
    )
    monkeypatch.setattr(
        base_module.ESIBase,
        "set_current_config",
        lambda _self, _requested: (_ for _ in ()).throw(
            AssertionError("unsafe configuration must not be written")
        ),
    )

    with pytest.raises(RuntimeError, match="global enable is ON"):
        controller.configure_hv_max_voltage_steps(timeout_s=0.5)


def test_activation_notebook_uses_vendor_fixed_point_configuration_layout():
    notebook = json.loads(ACTIVATION_NOTEBOOK.read_text(encoding="utf-8"))
    setup_source = "".join(notebook["cells"][1]["source"])
    probe_source = "".join(notebook["cells"][2]["source"])
    notebook_source = "".join(
        "".join(cell.get("source", [])) for cell in notebook["cells"]
    )
    namespace = {
        "DRIVER_FILE": RUNTIME_DIR / "esi" / "esi_base.py",
        "DLL_FILE": VENDOR_DIR / "x64" / "COM-ESI-CTRL.dll",
        "ERROR_CODES_FILE": RUNTIME_DIR / "error_codes.json",
        "MAX_VOLTAGE_STEP": 10.008,
        "MAX_VOLTAGE_STEP_RAW": 10008,
    }
    exec(probe_source, namespace)

    assert "ARM_TEMP_CONFIG = False" in setup_source
    assert "ARM_NONZERO_TEST = False" in setup_source
    assert "TEST_VOLTAGE = 100.0" in setup_source
    assert "0.0 <= float(TEST_VOLTAGE) <= 3000.0" in notebook_source
    assert "between 0 and 10 V" not in notebook_source
    assert "lab_admin" not in notebook_source
    assert "Path.home() / 'ESIBD Explorer' / 'plugins' / 'esi'" in setup_source
    assert "NONZERO_RISE_TIMEOUT_SECONDS = 10.0" in setup_source
    assert "NONZERO_HOLD_SECONDS = 20.0" in setup_source
    assert "NONZERO_HOLD_START_V = 95.0" in setup_source
    assert "NONZERO_OBSERVE_SECONDS" not in notebook_source
    assert "NONZERO_POLL_SECONDS = 0.05" in setup_source
    assert "NONZERO_ABS_LIMIT_V = 150.0" in setup_source
    assert "NONZERO_ADC_GRACE_SECONDS = 1.5" in setup_source
    assert "DISCHARGE_LIMIT_V = 1.0" in setup_source
    assert "DISCHARGE_TIMEOUT_SECONDS = 60.0" in setup_source
    assert "'nominal_evaluation': 'measurement_only'" in notebook_source
    assert "(('positive', False), ('negative', True))" in notebook_source
    assert "report['steps'][f'zero_{polarity}_adc']" in notebook_source
    assert "verify_measurement_ranges_after_zero_adc" in notebook_source
    assert "zero_active_current_configuration" in notebook_source
    assert "expected_confirmation = f'ARM {TEST_VOLTAGE:g} V'" in notebook_source
    assert "nonzero_global_off_after_observation" in notebook_source
    assert "nonzero_safe_state_after_observation" in notebook_source
    assert "nonzero_guard_samples" in notebook_source
    assert "abort_nonzero(guard_reason)" in notebook_source
    assert "nonzero_config['selected_max_step_bytes']" in notebook_source
    assert "nonzero_staged_target_state" in notebook_source
    assert "nonzero_activate_after_target" in notebook_source
    assert "last_valid_adc_elapsed = None" in notebook_source
    assert "adc_silence_seconds > NONZERO_ADC_GRACE_SECONDS" in notebook_source
    assert "No fresh valid ADC conversion" in notebook_source
    assert "hold_started_elapsed = None" in notebook_source
    assert "abs(pwm_measured_v) >= NONZERO_HOLD_START_V" in notebook_source
    assert "hold_elapsed_seconds >= NONZERO_HOLD_SECONDS" in notebook_source
    assert "'completed': True" in notebook_source
    assert "int(module_state_values[0]) & int(device.MS_ACTIVE)" in notebook_source
    assert "safety_limit_v=NONZERO_ABS_LIMIT_V" in notebook_source
    assert "abort_callback=abort_nonzero" in notebook_source
    assert "guard_{polarity}_adc_settle_pwm" in notebook_source
    assert "PWM activation was lost during" in notebook_source
    assert "report['cleanup']['discharge']" in notebook_source
    assert "def wait_for_discharge():" in notebook_source
    assert "if not report['cleanup']['discharge']['confirmed']:" in notebook_source
    assert notebook_source.index("'nonzero_guard_global_off'") < (
        notebook_source.index("'nonzero_guard_zero_target'")
    ) < notebook_source.index("'nonzero_guard_standby'")
    assert notebook_source.index("guard_started = time.monotonic()") < (
        notebook_source.index("nonzero_state = read_hv_state")
    )
    assert notebook_source.index("nonzero_global_off_after_observation") < (
        notebook_source.index("'positive_voltage_v': input(")
    )
    assert notebook_source.index(
        "report['cleanup']['discharge'] = wait_for_discharge()"
    ) < notebook_source.index("'positive_voltage_v': input(")

    class FakeAdcDevice:
        NO_ERR = 0

        def __init__(self):
            self.pwm_measured_v = 100.0

        @staticmethod
        def format_status(status):
            return str(status)

        def set_hv_supply_meas_ranges(self, _address, _negative, _current_high):
            return self.NO_ERR

        def get_hv_supply_meas_ranges(self, _address):
            return self.NO_ERR, False, False

        def get_hv_supply_params_pwm(self, _address):
            return (
                self.NO_ERR,
                5e-6,
                15e-9,
                0.0,
                0.0,
                100.0,
                self.pwm_measured_v,
                True,
                0,
            )

    fake_adc_device = FakeAdcDevice()
    namespace["SETTLE_SECONDS"] = 0.001
    namespace["NONZERO_POLL_SECONDS"] = 0.0001
    namespace["read_hv_state"] = lambda _device, _address: {
        "measured_voltage": {"values": [True, 100.0]},
        "pwm_voltage_measured_v": 100.0,
    }
    guarded_adc = namespace["select_and_read_adc"](
        fake_adc_device,
        1,
        False,
        False,
        safety_limit_v=150.0,
    )

    assert guarded_adc["settle_guard_samples"]
    assert all(
        sample["pwm_measured_v"] == 100.0
        for sample in guarded_adc["settle_guard_samples"]
    )

    fake_adc_device.pwm_measured_v = 151.0
    abort_reasons = []

    def abort_adc(reason):
        abort_reasons.append(reason)
        raise RuntimeError(reason)

    with pytest.raises(RuntimeError, match="exceeds 150 V"):
        namespace["select_and_read_adc"](
            fake_adc_device,
            1,
            False,
            False,
            safety_limit_v=150.0,
            abort_callback=abort_adc,
        )

    assert abort_reasons and "151 V exceeds 150 V" in abort_reasons[0]

    hardware_notebook = json.loads(HARDWARE_NOTEBOOK.read_text(encoding="utf-8"))
    hardware_source = "".join(
        "".join(cell.get("source", [])) for cell in hardware_notebook["cells"]
    )
    assert "lab_admin" not in hardware_source
    assert "REPO_ROOT" not in hardware_source
    assert "Path.home() / 'ESIBD Explorer' / 'plugins' / 'esi'" in hardware_source
    assert "report_path = (\n            PLUGIN_DIR /" in hardware_source

    raw = [0] * 53
    raw[11:15] = (273150).to_bytes(4, byteorder="little", signed=True)
    decoded = namespace["decode_current_config"](raw)

    assert decoded["heat"]["target_temperature_c"] == 0.0
    assert namespace["safe_config_violations"](decoded) == []

    temporary = namespace["build_temporary_hv_config"](raw, 1, 10.008)
    changed_offsets = [
        index
        for index, (before, after) in enumerate(zip(raw, temporary, strict=True))
        if before != after
    ]
    temporary_decoded = namespace["decode_current_config"](temporary)

    assert changed_offsets == [21, 22]
    assert temporary[21:25] == [0x18, 0x27, 0x00, 0x00]
    assert temporary_decoded["hv"][1]["max_voltage_step_v"] == 10.008
    assert namespace["temporary_config_violations"](
        temporary_decoded, 1, 10.008
    ) == []

    old_misaligned_patch = bytearray(raw)
    old_misaligned_patch[24:28] = bytes([0x18, 0x01, 0x00, 0x00])
    old_misaligned_decoded = namespace["decode_current_config"](
        old_misaligned_patch
    )

    assert old_misaligned_decoded["hv"][1]["max_voltage_step_v"] == 402653.184
    assert old_misaligned_decoded["hv"][1]["negative_adc"] is True

    active = bytearray(temporary)
    active[0] = 1
    active[17:21] = (10000).to_bytes(4, byteorder="little", signed=True)
    active[27] = 1
    active_decoded = namespace["decode_current_config"](active)

    assert active_decoded["device_enabled"] is True
    assert active_decoded["hv"][1] == {
        "target_voltage_v": 10.0,
        "max_voltage_step_v": 10.008,
        "negative_adc": False,
        "high_current_range": False,
        "enabled": True,
    }


def test_diagnostics_use_complete_state_snapshot(driver_modules, monkeypatch):
    _runtime, driver_module, base_module = driver_modules
    controller = _controller(driver_module)
    controller.connected = True
    complete_calls = []

    monkeypatch.setattr(
        base_module.ESIBase,
        "get_complete_state",
        lambda _self: (
            complete_calls.append(True) or 0,
            0x05,
            0x00,
            0x37,
            0x00,
            0x0F,
            0xF00C,
            0x0001,
            [0x04, 0x10, 0x50, 0x00, 0x04],
            [0x0100, 0x4100, 0xC100, 0x0000, 0x8000],
        ),
    )
    for obsolete_getter in (
        "get_main_state",
        "get_device_state",
        "get_voltage_state",
        "get_interlock_state",
    ):
        monkeypatch.setattr(
            base_module.ESIBase,
            obsolete_getter,
            lambda _self, name=obsolete_getter: (_ for _ in ()).throw(
                AssertionError(f"{name} should be read through GetCompleteState")
            ),
        )
    monkeypatch.setattr(base_module.ESIBase, "get_enable", lambda _self: (0, True))
    monkeypatch.setattr(
        base_module.ESIBase,
        "get_housekeeping",
        lambda _self: (0, 24.0, 5.0, 3.3, 40.0, 41.0),
    )
    monkeypatch.setattr(
        base_module.ESIBase,
        "get_hv_supply_target_output_voltage",
        lambda _self, _address: (0, 0.0),
    )
    monkeypatch.setattr(
        base_module.ESIBase,
        "get_hv_supply_meas_ranges",
        lambda _self, address: (0, address == 2, address == 1),
    )
    monkeypatch.setattr(
        base_module.ESIBase,
        "get_hv_supply_output_voltage",
        lambda _self, _address: (0, True, 0.0),
    )
    monkeypatch.setattr(
        base_module.ESIBase,
        "get_hv_supply_output_current",
        lambda _self, _address: (0, True, 0.0),
    )
    monkeypatch.setattr(
        base_module.ESIBase,
        "get_hv_supply_params_pwm",
        lambda _self, _address: (0, 1.0, 0.5, 0.0, 0.0, 0.0, 0.0, False, 0x50),
    )
    monkeypatch.setattr(
        base_module.ESIBase,
        "get_module_led_data",
        lambda _self, address: (0, address == 1, True, False),
    )
    monkeypatch.setattr(
        base_module.ESIBase,
        "get_heat_ctrl_monitoring",
        lambda _self: (0, True, 0.0, 0.0, 0.0, 25.0),
    )
    monkeypatch.setattr(
        base_module.ESIBase,
        "get_heat_ctrl_output_voltage",
        lambda _self: (0, 0.0),
    )
    monkeypatch.setattr(
        base_module.ESIBase,
        "get_heat_ctrl_heater_power",
        lambda _self: (0, 0.0),
    )
    monkeypatch.setattr(
        base_module.ESIBase,
        "get_heat_ctrl_ilock_state",
        lambda _self: (0, 0),
    )
    monkeypatch.setattr(
        base_module.ESIBase,
        "get_heat_ctrl_housekeeping",
        lambda _self: (0, True, 3.3, 40.0, 5.0, 24.0, 41.0),
    )
    monkeypatch.setattr(
        controller,
        "get_heat_configuration_unlocked",
        lambda: {
            "hardware_limits": {
                "max_voltage_v": 22.0,
                "max_current_a": 12.0,
                "max_power_w": 180.0,
                "max_temperature_c": 175.0,
            },
            "voltage_limit_v": 22.0,
            "current_limit_a": 12.0,
            "power_limit_w": 180.0,
            "target_temperature_c": 0.0,
        },
    )

    snapshot = controller.collect_diagnostics(timeout_s=0.5)

    assert complete_calls == [True]
    assert snapshot["main_state"] == {"hex": "0x1", "name": "STATE_STANDBY"}
    assert snapshot["data_ready_flags"] == 0x05
    assert snapshot["device_state"]["flags"] == ["DEVST_OK"]
    assert snapshot["temperature_state"] == {"hex": "0x0", "flags": []}
    assert snapshot["modules"][1]["module_state"] == 0x4100
    assert snapshot["modules"][1]["control_active"] is True
    assert snapshot["modules"][1]["module_gate_active"] is True
    assert snapshot["modules"][1]["device_gate_active"] is False
    assert snapshot["modules"][1]["led"] == {
        "red": True,
        "green": True,
        "blue": False,
    }
    assert snapshot["modules"][1]["measurement"] == {
        "voltage_polarity": "positive",
        "negative_voltage": False,
        "high_current_range": True,
    }
    assert snapshot["modules"][2]["measurement"] == {
        "voltage_polarity": "negative",
        "negative_voltage": True,
        "high_current_range": False,
    }
    assert snapshot["modules"][1]["pwm"]["period_s"] == 1.0
    assert "period_us" not in snapshot["modules"][1]["pwm"]
    assert snapshot["modules"][2]["data_ready_flags"] == 0x50


def test_module_target_api_is_unsigned_and_keeps_compatibility_alias(
    driver_modules,
    monkeypatch,
):
    _runtime, driver_module, base_module = driver_modules
    controller = _controller(driver_module)
    controller.connected = True
    calls = []
    targets = {}

    def set_target(_self, address, value):
        calls.append(("set", address, value))
        targets[address] = value
        return 0

    monkeypatch.setattr(
        base_module.ESIBase,
        "set_hv_supply_target_output_voltage",
        set_target,
    )
    monkeypatch.setattr(
        base_module.ESIBase,
        "get_hv_supply_target_output_voltage",
        lambda _self, address: (
            calls.append(("get", address)) or 0,
            targets[address],
        ),
    )

    assert controller.set_hv_module_target(1, 125.0, timeout_s=0.5) == 125.0
    assert controller.set_target_voltage(2, 250.0, timeout_s=0.5) == 250.0

    assert calls == [
        ("set", 1, 125.0),
        ("get", 1),
        ("set", 2, 250.0),
        ("get", 2),
    ]


def test_module_target_rejects_unconfirmed_value(driver_modules, monkeypatch):
    _runtime, driver_module, base_module = driver_modules
    controller = _controller(driver_module)
    controller.connected = True
    monkeypatch.setattr(
        base_module.ESIBase,
        "set_hv_supply_target_output_voltage",
        lambda _self, _address, _value: 0,
    )
    monkeypatch.setattr(
        base_module.ESIBase,
        "get_hv_supply_target_output_voltage",
        lambda _self, _address: (0, 0.0),
    )

    with pytest.raises(RuntimeError, match="target verification failed"):
        controller.set_hv_module_target(1, 10.0, timeout_s=0.5)


def test_global_enable_is_written_then_verified(driver_modules, monkeypatch):
    _runtime, driver_module, base_module = driver_modules
    controller = _controller(driver_module)
    controller.connected = True
    calls = []
    state = {"enabled": False}

    def set_enable(_self, enabled):
        calls.append(("set", enabled))
        state["enabled"] = enabled
        return 0

    monkeypatch.setattr(base_module.ESIBase, "set_enable", set_enable)
    monkeypatch.setattr(
        base_module.ESIBase,
        "get_enable",
        lambda _self: (calls.append(("get",)) or 0, state["enabled"]),
    )

    assert controller.set_global_active(True, timeout_s=0.5) is True
    assert calls == [("set", True), ("get",)]


def test_global_enable_rejects_unconfirmed_state(driver_modules, monkeypatch):
    _runtime, driver_module, base_module = driver_modules
    controller = _controller(driver_module)
    controller.connected = True
    monkeypatch.setattr(
        base_module.ESIBase,
        "set_enable",
        lambda _self, _enabled: 0,
    )
    monkeypatch.setattr(
        base_module.ESIBase,
        "get_enable",
        lambda _self: (0, False),
    )

    with pytest.raises(RuntimeError, match="enable verification failed"):
        controller.set_global_active(True, timeout_s=0.5)


def test_hv_measurement_selector_uses_physical_polarity_and_current_range(
    driver_modules,
    monkeypatch,
):
    _runtime, driver_module, base_module = driver_modules
    controller = _controller(driver_module)
    controller.connected = True
    calls = []
    selected_ranges = {}

    def set_ranges(_self, address, negative, high_current):
        calls.append(("set", address, negative, high_current))
        selected_ranges[address] = (negative, high_current)
        return 0

    monkeypatch.setattr(
        base_module.ESIBase,
        "set_hv_supply_meas_ranges",
        set_ranges,
    )
    monkeypatch.setattr(
        base_module.ESIBase,
        "get_hv_supply_meas_ranges",
        lambda _self, address: (
            calls.append(("get", address)) or 0,
            *selected_ranges[address],
        ),
    )

    selected = controller.select_hv_measurement(
        2,
        negative=True,
        high_current=False,
        timeout_s=0.5,
    )
    selected_again = controller.select_hv_measurement(
        2,
        negative=True,
        high_current=False,
        timeout_s=0.5,
    )

    assert selected is True
    assert selected_again is True
    assert calls == [("set", 2, True, False), ("get", 2)]


def test_hv_measurement_selector_rejects_unconfirmed_selection(
    driver_modules,
    monkeypatch,
):
    _runtime, driver_module, base_module = driver_modules
    controller = _controller(driver_module)
    controller.connected = True
    monkeypatch.setattr(
        base_module.ESIBase,
        "set_hv_supply_meas_ranges",
        lambda _self, _address, _negative, _high_current: 0,
    )
    monkeypatch.setattr(
        base_module.ESIBase,
        "get_hv_supply_meas_ranges",
        lambda _self, _address: (0, False, False),
    )

    with pytest.raises(RuntimeError, match="selection verification failed"):
        controller.select_hv_measurement(1, negative=True, timeout_s=0.5)

    assert controller._hv_measurement_requests == {}


def test_hv_measurement_selector_rejects_readback_failure(
    driver_modules,
    monkeypatch,
):
    _runtime, driver_module, base_module = driver_modules
    controller = _controller(driver_module)
    controller.connected = True
    monkeypatch.setattr(
        base_module.ESIBase,
        "set_hv_supply_meas_ranges",
        lambda _self, _address, _negative, _high_current: 0,
    )
    monkeypatch.setattr(
        base_module.ESIBase,
        "get_hv_supply_meas_ranges",
        lambda self, _address: (self.ERR_COMMAND_RECEIVE, False, False),
    )

    with pytest.raises(
        RuntimeError,
        match="verify_hv_measurement_selection",
    ):
        controller.select_hv_measurement(1, negative=False, timeout_s=0.5)

    assert controller._hv_measurement_requests == {}


def test_hv_measurement_selector_keeps_other_failures_blocking(
    driver_modules,
    monkeypatch,
):
    _runtime, driver_module, base_module = driver_modules
    controller = _controller(driver_module)
    controller.connected = True
    monkeypatch.setattr(
        base_module.ESIBase,
        "set_hv_supply_meas_ranges",
        lambda self, address, negative, high_current: self.ERR_COMMAND_WRONG,
    )

    with pytest.raises(RuntimeError, match=r"select_hv_measurement\(1\) failed"):
        controller.select_hv_measurement(1, negative=False, timeout_s=0.5)

    assert controller._hv_measurement_requests == {}


def test_only_lab_hv_addresses_are_commandable(driver_modules):
    _runtime, driver_module, _base_module = driver_modules
    controller = _controller(driver_module)

    assert controller._validate_hv_address(1) == 1
    assert controller._validate_hv_address(2) == 2
    with pytest.raises(ValueError, match="must be one of"):
        controller._validate_hv_address(3)

    assert controller._validate_controlled_address(0) == 0


def test_heat_output_disable_uses_zero_target_not_module_activation(
    driver_modules,
    monkeypatch,
):
    _runtime, driver_module, base_module = driver_modules
    controller = _controller(driver_module)
    controller.connected = True
    calls = []
    monkeypatch.setattr(
        base_module.ESIBase,
        "set_enable",
        lambda _self, state: calls.append(("enable", state)) or 0,
    )
    monkeypatch.setattr(
        base_module.ESIBase,
        "get_enable",
        lambda _self: (calls.append(("get_enable",)) or 0, True),
    )
    monkeypatch.setattr(
        base_module.ESIBase,
        "set_heat_ctrl_heater_temperature",
        lambda self, value: calls.append(("temperature", value)) or (0, value),
    )
    monkeypatch.setattr(
        base_module.ESIBase,
        "set_module_activation_state",
        lambda self, address, active: (_ for _ in ()).throw(
            AssertionError("Heat must not use module activation")
        ),
    )

    assert controller.set_output_active(0, True, timeout_s=0.5) is True
    assert controller.set_output_active(0, False, timeout_s=0.5) is False
    assert calls == [
        ("enable", True),
        ("get_enable",),
        ("temperature", 0.0),
    ]


def test_hv_output_state_uses_module_toggle_and_global_gate(
    driver_modules,
    monkeypatch,
):
    _runtime, driver_module, base_module = driver_modules
    controller = _controller(driver_module)
    controller.connected = True
    calls = []
    module_state = {1: False}
    monkeypatch.setattr(
        base_module.ESIBase,
        "set_enable",
        lambda _self, state: calls.append(("enable", state)) or 0,
    )
    monkeypatch.setattr(
        base_module.ESIBase,
        "get_enable",
        lambda _self: (calls.append(("get_enable",)) or 0, True),
    )
    monkeypatch.setattr(
        base_module.ESIBase,
        "set_hv_supply_target_output_voltage",
        lambda _self, address, value: calls.append(("target", address, value)) or 0,
    )
    monkeypatch.setattr(
        base_module.ESIBase,
        "get_hv_supply_target_output_voltage",
        lambda _self, address: (calls.append(("get_target", address)) or 0, 0.0),
    )
    monkeypatch.setattr(
        base_module.ESIBase,
        "set_module_activation_state",
        lambda _self, address, active: (
            calls.append(("module", address, active)),
            module_state.__setitem__(address, active),
            0,
        )[-1],
    )
    monkeypatch.setattr(
        base_module.ESIBase,
        "get_hv_supply_params_pwm",
        lambda _self, address: (
            calls.append(("pwm", address)) or 0,
            1.0,
            0.5,
            0.0,
            0.0,
            0.0,
            0.0,
            module_state[address],
            0,
        ),
    )

    assert controller.set_output_active(1, True, timeout_s=0.5) is True
    assert controller.set_output_active(1, False, timeout_s=0.5) is False
    assert calls == [
        ("module", 1, True),
        ("pwm", 1),
        ("enable", True),
        ("get_enable",),
        ("target", 1, 0.0),
        ("get_target", 1),
        ("module", 1, False),
        ("pwm", 1),
    ]


def test_module_activation_rejects_missing_ack_without_polling(
    driver_modules,
    monkeypatch,
):
    _runtime, driver_module, base_module = driver_modules
    controller = _controller(driver_module)
    controller.connected = True
    calls = []
    monkeypatch.setattr(
        base_module.ESIBase,
        "set_module_activation_state",
        lambda _self, address, active: calls.append(
            ("module", address, active)
        ) or controller.ERR_COMMAND_RECEIVE,
    )
    monkeypatch.setattr(
        base_module.ESIBase,
        "get_hv_supply_params_pwm",
        lambda _self, address: calls.append(("pwm", address)),
    )

    with pytest.raises(RuntimeError, match=r"set_hv_module_active\(1\) failed"):
        controller.set_hv_module_active(1, True, timeout_s=0.5)

    assert calls == [("module", 1, True)]


def test_module_activation_rejects_unconfirmed_pwm_state(
    driver_modules,
    monkeypatch,
):
    _runtime, driver_module, base_module = driver_modules
    controller = _controller(driver_module)
    controller.connected = True
    monkeypatch.setattr(
        base_module.ESIBase,
        "set_module_activation_state",
        lambda _self, _address, _active: 0,
    )
    monkeypatch.setattr(
        base_module.ESIBase,
        "get_hv_supply_params_pwm",
        lambda _self, _address: (
            0,
            1.0,
            0.5,
            0.0,
            0.0,
            0.0,
            0.0,
            False,
            0,
        ),
    )

    with pytest.raises(RuntimeError, match="activation verification failed"):
        controller.set_hv_module_active(1, True, timeout_s=0.5)


def test_discovery_rejects_wrong_heat_controller_type(driver_modules, monkeypatch):
    _runtime, driver_module, base_module = driver_modules
    controller = _controller(driver_module)
    controller.connected = True
    monkeypatch.setattr(base_module.ESIBase, "update_module_presence", lambda self: 0)
    monkeypatch.setattr(
        base_module.ESIBase,
        "get_module_presence",
        lambda self: (0, True, 3, [1, 1, 1, 0, 1]),
    )
    monkeypatch.setattr(
        base_module.ESIBase,
        "get_module_dev_type",
        lambda self, address: (0, self.MODULE_HVPS_TYPE),
    )

    with pytest.raises(RuntimeError, match="module 0 type mismatch"):
        controller.discover_modules(timeout_s=0.5)


def test_heater_temperature_is_limited_by_hardware(driver_modules, monkeypatch):
    _runtime, driver_module, base_module = driver_modules
    controller = _controller(driver_module)
    controller.connected = True
    applied = []
    monkeypatch.setattr(
        base_module.ESIBase,
        "get_heat_ctrl_hw_limits",
        lambda self: (0, 24.0, 10.0, 200.0, 180.0),
    )
    monkeypatch.setattr(
        base_module.ESIBase,
        "set_heat_ctrl_heater_temperature",
        lambda self, target: applied.append(target) or (0, target),
    )

    assert controller.set_heater_temperature(125.0, timeout_s=0.5) == 125.0
    assert applied == [125.0]
    with pytest.raises(ValueError, match="hardware maximum 180"):
        controller.set_heater_temperature(181.0, timeout_s=0.5)


def test_heat_limit_overrides_validate_reported_maxima(driver_modules, monkeypatch):
    _runtime, driver_module, base_module = driver_modules
    controller = _controller(driver_module)
    controller.connected = True
    applied = []
    monkeypatch.setattr(
        base_module.ESIBase,
        "get_heat_ctrl_hw_limits",
        lambda self: (0, 24.0, 8.0, 150.0, 180.0),
    )
    monkeypatch.setattr(
        base_module.ESIBase,
        "set_heat_ctrl_voltage_limit",
        lambda self, value: applied.append(("voltage", value)) or (0, value),
    )
    monkeypatch.setattr(
        base_module.ESIBase,
        "set_heat_ctrl_current_limit",
        lambda self, value: applied.append(("current", value)) or (0, value),
    )
    monkeypatch.setattr(
        base_module.ESIBase,
        "set_heat_ctrl_power_limit",
        lambda self, value: applied.append(("power", value)) or (0, value),
    )

    assert controller.configure_heat_limits(
        voltage_v=20.0,
        power_w=120.0,
        timeout_s=0.5,
    ) == {"voltage_v": 20.0, "power_w": 120.0}
    assert applied == [("voltage", 20.0), ("power", 120.0)]
    with pytest.raises(ValueError, match="hardware maximum 24"):
        controller.configure_heat_limits(voltage_v=25.0, timeout_s=0.5)
    applied.clear()
    with pytest.raises(ValueError, match="hardware maximum 8"):
        controller.configure_heat_limits(
            voltage_v=20.0,
            current_a=9.0,
            timeout_s=0.5,
        )
    assert applied == []


def test_second_inline_controller_is_rejected(driver_modules):
    _runtime, driver_module, _base_module = driver_modules
    first = _controller(driver_module)
    second = _controller(driver_module)
    first.device_id = "first"
    second.device_id = "second"
    first._claim_single_instance()

    with pytest.raises(RuntimeError, match="single-instance"):
        second._claim_single_instance()


def test_process_rpc_budgets_cover_batched_dll_operations(driver_modules):
    runtime, _driver_module, _base_module = driver_modules
    driver = runtime.ESI

    assert driver._rpc_timeout_for("connect", {"timeout_s": 5.0}) == 160.0
    assert driver._rpc_timeout_for("collect_identity", {"timeout_s": 3.0}) == 90.0
    assert driver._rpc_timeout_for("collect_diagnostics", {"timeout_s": 3.0}) == 60.0
    assert driver._rpc_timeout_for("force_safe_off", {"timeout_s": 5.0}) == 50.0
    assert driver._rpc_timeout_for("disconnect", {"timeout_s": 5.0}) == 60.0
    assert driver._rpc_timeout_for("get_heat_configuration", {"timeout_s": 3.0}) == 45.0
