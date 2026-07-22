"""Timeout-safe high-level driver for the CGC ESI controller."""

from __future__ import annotations

import contextlib
import logging
import math
import struct
import threading
from pathlib import Path
from typing import Optional

from .._driver_common import (
    ProcessIsolatedClientMixin,
    TimeoutSafeDllMixin,
    build_device_logger,
)
from .esi_base import ESIBase


class _ESIController(TimeoutSafeDllMixin, ESIBase):
    """Validated ESI controller with deterministic high-voltage shutdown."""

    _INSTRUMENT_NAME = "ESI"
    _DEFAULT_IO_TIMEOUT_S = 5.0
    HEAT_MODULE_ADDRESS = 0
    HV_MODULE_ADDRESSES = (1, 2)
    CONTROLLED_MODULE_ADDRESSES = (HEAT_MODULE_ADDRESS, *HV_MODULE_ADDRESSES)
    MAX_ABS_VOLTAGE_V = 3000.0
    HV_CONFIG_BASE_OFFSET = 17
    HV_CONFIG_STRIDE = 12
    HV_CONFIG_MAX_STEP_OFFSET = 4
    HV_CONFIG_ENABLE_OFFSET = 10
    DEFAULT_HV_MAX_VOLTAGE_STEP_V = 10.008
    _instance_lock = threading.Lock()
    _connected_instance: Optional["_ESIController"] = None

    def __init__(
        self,
        device_id: str,
        com: int,
        baudrate: int = 230400,
        logger: Optional[logging.Logger] = None,
        thread_lock: Optional[threading.Lock] = None,
        dll_path: Optional[str] = None,
        log_dir: Optional[Path] = None,
        **kwargs,
    ):
        if kwargs:
            unexpected = ", ".join(sorted(kwargs))
            raise TypeError(f"Unexpected ESI init kwargs: {unexpected}")
        self._validate_init_args(device_id, com, baudrate)
        self.device_id = device_id
        self.com = int(com)
        self.baudrate = int(baudrate)
        self.connected = False
        self._transport_poisoned = False
        self._transport_error = None
        self._module_inventory: dict[int, dict] = {}
        self._hv_measurement_requests: dict[int, tuple[bool, bool, bool]] = {}
        self.thread_lock = thread_lock or threading.Lock()
        self.logger = build_device_logger(
            instrument_name=self._INSTRUMENT_NAME,
            device_id=device_id,
            logger=logger,
            log_dir=log_dir,
            source_file=__file__,
        )
        super().__init__(com=com, idn=device_id, dll_path=dll_path)

    @staticmethod
    def _validate_init_args(device_id, com, baudrate):
        if not isinstance(device_id, str) or not device_id.strip():
            raise ValueError("ESI device_id must be a non-empty string.")
        if isinstance(com, bool) or not isinstance(com, int) or not 1 <= com <= 255:
            raise ValueError("ESI com must be an integer between 1 and 255.")
        if isinstance(baudrate, bool) or not isinstance(baudrate, int) or baudrate <= 0:
            raise ValueError("ESI baudrate must be a positive integer.")

    def _resolve_timeout(self, timeout_s: Optional[float]) -> float:
        timeout = self._DEFAULT_IO_TIMEOUT_S if timeout_s is None else float(timeout_s)
        if timeout <= 0:
            raise ValueError("ESI timeout_s must be greater than 0.")
        return timeout

    def _require_connected(self):
        if not self.connected:
            raise RuntimeError("ESI device is not connected.")

    def _raise_on_status(self, status: int, action: str):
        if status != self.NO_ERR:
            raise RuntimeError(f"ESI {action} failed: {self.format_status(status)}")

    def _claim_single_instance(self):
        cls = type(self)
        with cls._instance_lock:
            holder = cls._connected_instance
            if holder is not None and holder is not self:
                raise RuntimeError(
                    "ESI-CTRL DLL is single-instance per process; "
                    f"'{holder.device_id}' already owns its implicit channel."
                )
            cls._connected_instance = self

    def _release_single_instance(self):
        cls = type(self)
        with cls._instance_lock:
            if cls._connected_instance is self:
                cls._connected_instance = None

    def _on_transport_poisoned(self) -> None:
        # A timed-out DLL thread may still own the implicit COM channel.
        # Keep the single-instance claim until this process exits.
        self.connected = False

    def connect(self, timeout_s: float = 5.0) -> bool:
        """Connect, validate identity, inventory modules, and force HV OFF."""
        timeout = self._resolve_timeout(timeout_s)
        if self.connected:
            return True
        self._hv_measurement_requests.clear()
        self._claim_single_instance()
        opened = False
        try:
            status = self._call_locked_with_timeout(
                ESIBase.open_port, timeout, "open_port", self, self.com
            )
            self._raise_on_status(status, "open_port")
            opened = True

            status, actual_baud = self._call_locked_with_timeout(
                ESIBase.set_comspeed, timeout, "set_comspeed", self, self.baudrate
            )
            self._raise_on_status(status, "set_comspeed")

            status, device_type = self._call_locked_with_timeout(
                ESIBase.get_dev_type, timeout, "get_dev_type", self
            )
            self._raise_on_status(status, "get_dev_type")
            if int(device_type) != self.DEVICE_TYPE:
                raise RuntimeError(
                    "ESI device type mismatch: "
                    f"expected 0x{self.DEVICE_TYPE:04X}, got 0x{int(device_type):04X}."
                )

            self.connected = True
            self._prepare_safe_inventory(timeout)
            modules = self.discover_modules(timeout_s=timeout)
            self.force_safe_off(timeout_s=timeout)
            self.logger.info(
                f"Connected on COM{self.com} at {actual_baud} baud; "
                f"modules={sorted(modules)}; HV and heater outputs forced OFF"
            )
            return True
        except Exception:
            self.connected = False
            if opened and not self._transport_poisoned:
                with contextlib.suppress(Exception):
                    self._call_locked_with_timeout(
                        ESIBase.close_port, timeout, "close_port_rollback", self
                    )
            if not self._transport_poisoned:
                self._release_single_instance()
            raise

    def _prepare_safe_inventory(self, timeout: float) -> None:
        """Disable all outputs before inventory communication."""
        def prepare():
            status = ESIBase.set_enable(self, False)
            self._raise_on_status(status, "module disable before inventory")
            status = ESIBase.set_enable(self, True)
            self._raise_on_status(status, "module communication enable")

        self._call_locked_with_timeout(
            prepare, timeout * 2.0, "prepare_safe_inventory"
        )

    def force_safe_off(self, timeout_s: Optional[float] = None) -> bool:
        """Zero both HV targets and disable HV and heater activation."""
        self._require_connected()
        timeout = self._resolve_timeout(timeout_s)

        def safe_off_batch():
            failures = []
            status, _temperature = ESIBase.set_heat_ctrl_heater_temperature(self, 0.0)
            if status != self.NO_ERR:
                failures.append(f"heat target zero: {self.format_status(status)}")
            for address in self.HV_MODULE_ADDRESSES:
                status = ESIBase.set_hv_supply_target_output_voltage(self, address, 0.0)
                if status != self.NO_ERR:
                    failures.append(
                        f"module {address} zero target: {self.format_status(status)}"
                    )
                activation_status, read_status, active = (
                    self._set_hv_module_active_unlocked(address, False)
                )
                if activation_status != self.NO_ERR:
                    failures.append(
                        f"module {address} deactivate: "
                        f"{self.format_status(activation_status)}"
                    )
                elif read_status != self.NO_ERR:
                    failures.append(
                        f"module {address} verify deactivation: "
                        f"{self.format_status(read_status)}"
                    )
                elif active:
                    failures.append(
                        f"module {address} remained active after deactivation"
                    )
            status = ESIBase.set_enable(self, False)
            if status != self.NO_ERR:
                failures.append(f"module disable: {self.format_status(status)}")
            return failures

        failures = self._call_locked_with_timeout(
            safe_off_batch, timeout * 6.0, "force_safe_off"
        )
        if failures:
            raise RuntimeError("ESI safe OFF failed: " + "; ".join(failures))
        return True

    def discover_modules(self, timeout_s: Optional[float] = None) -> dict[int, dict]:
        """Return validated identity data for every present module."""
        self._require_connected()
        timeout = self._resolve_timeout(timeout_s)
        update_status = self._call_locked_with_timeout(
            ESIBase.update_module_presence, timeout, "update_module_presence", self
        )
        self._raise_on_status(update_status, "update_module_presence")
        status, valid, max_module, presence = self._call_locked_with_timeout(
            ESIBase.get_module_presence, timeout, "get_module_presence", self
        )
        self._raise_on_status(status, "get_module_presence")
        if not valid:
            raise RuntimeError("ESI module inventory is not valid.")

        modules = {}
        for address in range(self.MODULE_NUM):
            presence_state = int(presence[address])
            if presence_state == self.MODULE_NOT_FOUND:
                continue
            info = {"address": address, "presence": presence_state}
            if presence_state == self.MODULE_PRESENT:
                status, device_type = self._call_locked_with_timeout(
                    ESIBase.get_module_dev_type,
                    timeout,
                    f"get_module_dev_type[{address}]",
                    self,
                    address,
                )
                self._raise_on_status(status, f"get_module_dev_type({address})")
                info["device_type"] = int(device_type)
            modules[address] = info

        expected_types = {
            self.HEAT_MODULE_ADDRESS: self.MODULE_HTCTRL_TYPE,
            **{address: self.MODULE_HVPS_TYPE for address in self.HV_MODULE_ADDRESSES},
        }
        for address, expected_type in expected_types.items():
            info = modules.get(address)
            if info is None:
                module_kind = "heat" if address == self.HEAT_MODULE_ADDRESS else "HV"
                raise RuntimeError(
                    f"Required ESI {module_kind} module {address} was not detected."
                )
            if info.get("presence") != self.MODULE_PRESENT:
                raise RuntimeError(f"ESI module {address} is present but invalid.")
            if info.get("device_type") != expected_type:
                raise RuntimeError(
                    f"ESI module {address} type mismatch: expected "
                    f"0x{expected_type:04X}, got "
                    f"0x{int(info.get('device_type', 0)):04X}."
                )
        self._module_inventory = modules
        return modules

    def collect_identity(self, timeout_s: Optional[float] = None) -> dict:
        """Return controller and module identity, retaining optional probe errors."""
        self._require_connected()
        timeout = self._resolve_timeout(timeout_s)

        def identity_batch():
            def probe(method, *args):
                try:
                    result = method(self, *args)
                except Exception as exc:
                    return {"error": str(exc)}
                status, *values = result
                if status != self.NO_ERR:
                    return {"error": self.format_status(status)}
                return values[0] if len(values) == 1 else values

            controller = {
                "product_id": probe(ESIBase.get_product_id),
                "product_no": probe(ESIBase.get_product_no),
                "firmware_version": probe(ESIBase.get_fw_version),
                "firmware_date": probe(ESIBase.get_fw_date),
                "hardware_type": probe(ESIBase.get_hw_type),
                "hardware_version": probe(ESIBase.get_hw_version),
                "device_type": probe(ESIBase.get_dev_type),
                "dll_version": ESIBase.get_sw_version(self),
            }
            modules = {}
            for address in sorted(self._module_inventory):
                modules[address] = {
                    "product_id": probe(ESIBase.get_module_product_id, address),
                    "product_no": probe(ESIBase.get_module_product_no, address),
                    "device_type": probe(ESIBase.get_module_dev_type, address),
                    "hardware_type": probe(ESIBase.get_module_hw_type, address),
                    "hardware_version": probe(ESIBase.get_module_hw_version, address),
                    "firmware_version": probe(ESIBase.get_module_fw_version, address),
                }
                if modules[address]["device_type"] == self.MODULE_HVPS_TYPE:
                    modules[address]["fpga_version"] = probe(
                        ESIBase.get_hv_supply_fpga_version, address
                    )
            return {"controller": controller, "modules": modules}

        return self._call_locked_with_timeout(
            identity_batch, timeout * 20.0, "collect_identity"
        )

    def _validate_hv_address(self, address: int) -> int:
        if isinstance(address, bool) or not isinstance(address, int):
            raise TypeError("ESI HV module address must be an integer.")
        if address not in self.HV_MODULE_ADDRESSES:
            raise ValueError(f"ESI HV module address must be one of {self.HV_MODULE_ADDRESSES}.")
        return address

    def _validate_controlled_address(self, address: int) -> int:
        if isinstance(address, bool) or not isinstance(address, int):
            raise TypeError("ESI module address must be an integer.")
        if address not in self.CONTROLLED_MODULE_ADDRESSES:
            raise ValueError(
                "ESI module address must be one of "
                f"{self.CONTROLLED_MODULE_ADDRESSES}."
            )
        return address

    def _validate_voltage(self, voltage: float) -> float:
        value = float(voltage)
        if not math.isfinite(value):
            raise ValueError("ESI target voltage must be finite.")
        if not 0.0 <= value <= self.MAX_ABS_VOLTAGE_V:
            raise ValueError(
                "ESI target voltage must be between 0 and 3000 V. Each HV module "
                "drives both physical connectors from one unsigned magnitude."
            )
        return value

    def configure_hv_max_voltage_steps(
        self,
        max_step_v: float = DEFAULT_HV_MAX_VOLTAGE_STEP_V,
        timeout_s: Optional[float] = None,
    ) -> dict[int, float]:
        """Configure both volatile HV ramp steps while all outputs are OFF."""
        self._require_connected()
        value = float(max_step_v)
        if not math.isfinite(value) or not 0.0 < value <= self.MAX_ABS_VOLTAGE_V:
            raise ValueError(
                "ESI HV maximum voltage step must be greater than 0 and no more "
                "than 3000 V."
            )
        raw_value = round(value * 1000.0)
        if not math.isclose(raw_value / 1000.0, value, rel_tol=0.0, abs_tol=1e-9):
            raise ValueError("ESI HV maximum voltage step must use millivolt precision.")
        timeout = self._resolve_timeout(timeout_s)

        def configure():
            status, current = ESIBase.get_current_config(self)
            self._raise_on_status(status, "get_current_config")
            if len(current) != self.CONFIG_DATA_SIZE:
                raise RuntimeError(
                    "ESI current configuration has unexpected size "
                    f"{len(current)}; expected {self.CONFIG_DATA_SIZE} bytes"
                )

            unsafe = []
            if current[0]:
                unsafe.append("global enable is ON")
            for address in self.HV_MODULE_ADDRESSES:
                offset = self.HV_CONFIG_BASE_OFFSET + (
                    address - 1
                ) * self.HV_CONFIG_STRIDE
                target_mv = struct.unpack_from("<i", bytes(current), offset)[0]
                if target_mv != 0:
                    unsafe.append(f"module {address} target is {target_mv / 1000.0:g} V")
                if current[offset + self.HV_CONFIG_ENABLE_OFFSET]:
                    unsafe.append(f"module {address} gate is enabled")
            if unsafe:
                raise RuntimeError(
                    "Refusing volatile HV configuration while outputs are not "
                    "safely OFF: " + "; ".join(unsafe)
                )

            requested = bytearray(current)
            for address in self.HV_MODULE_ADDRESSES:
                offset = (
                    self.HV_CONFIG_BASE_OFFSET
                    + (address - 1) * self.HV_CONFIG_STRIDE
                    + self.HV_CONFIG_MAX_STEP_OFFSET
                )
                struct.pack_into("<i", requested, offset, raw_value)

            if requested != bytes(current):
                status = ESIBase.set_current_config(self, requested)
                self._raise_on_status(status, "set_current_config")

            status, observed = ESIBase.get_current_config(self)
            self._raise_on_status(status, "verify_current_config")
            if len(observed) != self.CONFIG_DATA_SIZE:
                raise RuntimeError(
                    "ESI verified configuration has unexpected size "
                    f"{len(observed)}; expected {self.CONFIG_DATA_SIZE} bytes"
                )
            if bytes(observed) != bytes(requested):
                changed = [
                    index
                    for index, (expected, actual) in enumerate(
                        zip(requested, observed, strict=True)
                    )
                    if expected != actual
                ]
                raise RuntimeError(
                    "ESI volatile HV configuration verification failed at byte "
                    f"offsets {changed}"
                )

            status, enabled = ESIBase.get_enable(self)
            self._raise_on_status(status, "verify_global_off_after_config")
            if enabled:
                raise RuntimeError("ESI global gate opened during HV configuration")

            applied = {}
            for address in self.HV_MODULE_ADDRESSES:
                status, target = ESIBase.get_hv_supply_target_output_voltage(
                    self, address
                )
                self._raise_on_status(
                    status, f"verify_hv_target_after_config({address})"
                )
                pwm = ESIBase.get_hv_supply_params_pwm(self, address)
                self._raise_on_status(
                    pwm[0], f"verify_hv_gate_after_config({address})"
                )
                if float(target) != 0.0 or bool(pwm[7]):
                    raise RuntimeError(
                        f"ESI module {address} left safe OFF during HV configuration"
                    )
                offset = (
                    self.HV_CONFIG_BASE_OFFSET
                    + (address - 1) * self.HV_CONFIG_STRIDE
                    + self.HV_CONFIG_MAX_STEP_OFFSET
                )
                applied[address] = (
                    struct.unpack_from("<i", bytes(observed), offset)[0] / 1000.0
                )
            return applied

        return self._call_locked_with_timeout(
            configure, timeout * 8.0, "configure_hv_max_voltage_steps"
        )

    def set_hv_module_target(
        self, address: int, voltage: float, timeout_s: Optional[float] = None
    ) -> float:
        """Set and verify the unsigned target shared by both HV outputs."""
        self._require_connected()
        address = self._validate_hv_address(address)
        value = self._validate_voltage(voltage)
        timeout = self._resolve_timeout(timeout_s)

        def set_and_verify():
            status = ESIBase.set_hv_supply_target_output_voltage(
                self, address, value
            )
            if status != self.NO_ERR:
                return status, self.NO_ERR, 0.0
            read_status, applied = ESIBase.get_hv_supply_target_output_voltage(
                self, address
            )
            return status, read_status, float(applied)

        status, read_status, applied = self._call_locked_with_timeout(
            set_and_verify,
            timeout * 2.0,
            f"set_hv_module_target[{address}]",
        )
        self._raise_on_status(status, f"set_hv_module_target({address})")
        self._raise_on_status(read_status, f"verify_hv_module_target({address})")
        if not math.isclose(applied, value, rel_tol=1e-9, abs_tol=1e-6):
            raise RuntimeError(
                f"ESI module {address} target verification failed: requested "
                f"{value:g} V, controller reports {applied:g} V"
            )
        return applied

    def set_target_voltage(
        self, address: int, voltage: float, timeout_s: Optional[float] = None
    ) -> float:
        """Compatibility alias for the module-level HV target."""
        return self.set_hv_module_target(address, voltage, timeout_s=timeout_s)

    def select_hv_measurement(
        self,
        address: int,
        *,
        negative: bool,
        high_current: bool = False,
        timeout_s: Optional[float] = None,
    ) -> bool:
        """Select and verify the HV ADC channels."""
        self._require_connected()
        address = self._validate_hv_address(address)
        requested = bool(negative), bool(high_current)
        previous = self._hv_measurement_requests.get(address)
        if previous is not None and previous[:2] == requested:
            return previous[2]
        timeout = self._resolve_timeout(timeout_s)

        def select_and_verify():
            status = ESIBase.set_hv_supply_meas_ranges(
                self,
                address,
                *requested,
            )
            if status != self.NO_ERR:
                return status, self.NO_ERR, False, False
            readback_status, negative, high_current = (
                ESIBase.get_hv_supply_meas_ranges(self, address)
            )
            return status, readback_status, bool(negative), bool(high_current)

        status, readback_status, negative, high_current = (
            self._call_locked_with_timeout(
                select_and_verify,
                timeout * 2.0,
                f"select_hv_measurement[{address}]",
            )
        )
        self._raise_on_status(status, f"select_hv_measurement({address})")
        self._raise_on_status(
            readback_status,
            f"verify_hv_measurement_selection({address})",
        )
        observed = negative, high_current
        if observed != requested:
            raise RuntimeError(
                f"ESI module {address} measurement selection verification failed: "
                f"requested {requested}, controller reports {observed}"
            )
        self._hv_measurement_requests[address] = (*requested, True)
        return True

    def set_global_active(self, active: bool, timeout_s: Optional[float] = None) -> bool:
        self._require_connected()
        timeout = self._resolve_timeout(timeout_s)
        requested = bool(active)

        def set_and_verify():
            status = ESIBase.set_enable(self, requested)
            if status != self.NO_ERR:
                return status, self.NO_ERR, not requested
            read_status, enabled = ESIBase.get_enable(self)
            return status, read_status, bool(enabled)

        status, read_status, enabled = self._call_locked_with_timeout(
            set_and_verify,
            timeout * 2.0,
            "set_global_active",
        )
        self._raise_on_status(status, "set_global_active")
        self._raise_on_status(read_status, "verify_global_active")
        if enabled != requested:
            raise RuntimeError(
                "ESI global output enable verification failed: requested "
                f"{requested}, controller reports {enabled}"
            )
        return enabled

    def get_hv_module_active(
        self, address: int, timeout_s: Optional[float] = None
    ) -> bool:
        """Read the HV converter activation bit from the working PWM status API."""
        self._require_connected()
        address = self._validate_hv_address(address)
        timeout = self._resolve_timeout(timeout_s)
        result = self._call_locked_with_timeout(
            ESIBase.get_hv_supply_params_pwm,
            timeout,
            f"get_hv_module_active[{address}]",
            self,
            address,
        )
        self._raise_on_status(result[0], f"get_hv_module_active({address})")
        return bool(result[7])

    def set_hv_module_active(
        self,
        address: int,
        active: bool,
        timeout_s: Optional[float] = None,
    ) -> bool:
        """Set the module HVC toggle and verify it through PWM status."""
        self._require_connected()
        address = self._validate_hv_address(address)
        requested = bool(active)
        timeout = self._resolve_timeout(timeout_s)

        status, read_status, observed = self._call_locked_with_timeout(
            self._set_hv_module_active_unlocked,
            timeout * 4.0,
            f"set_hv_module_active[{address}]",
            address,
            requested,
        )
        self._raise_on_status(status, f"set_hv_module_active({address})")
        self._raise_on_status(
            read_status,
            f"verify_hv_module_active({address})",
        )
        if observed != requested:
            raise RuntimeError(
                f"ESI module {address} activation verification failed: "
                f"requested {requested}, PWM status reports {observed}"
            )
        return observed

    def _set_hv_module_active_unlocked(
        self, address: int, requested: bool
    ) -> tuple[int, int, bool]:
        status = ESIBase.set_module_activation_state(
            self, address, requested
        )
        if status != self.NO_ERR:
            return status, self.NO_ERR, not requested
        pwm = ESIBase.get_hv_supply_params_pwm(self, address)
        if pwm[0] != self.NO_ERR:
            pwm = ESIBase.get_hv_supply_params_pwm(self, address)
        return status, pwm[0], bool(pwm[7])

    def set_output_active(
        self, address: int, active: bool, timeout_s: Optional[float] = None
    ) -> bool:
        """Control one module's HVC toggle and the controller-wide gate.

        HV disable always zeros the target before requesting module standby.
        HV enable verifies the module toggle before opening the global gate.
        """
        self._require_connected()
        address = self._validate_controlled_address(address)
        timeout = self._resolve_timeout(timeout_s)
        if address == self.HEAT_MODULE_ADDRESS:
            if active:
                self.set_global_active(True, timeout_s=timeout)
                return True
            result = self._call_locked_with_timeout(
                ESIBase.set_heat_ctrl_heater_temperature,
                timeout,
                "disable_heat_output",
                self,
                0.0,
            )
            self._raise_on_status(result[0], "disable_heat_output")
            return False
        if active:
            self.set_hv_module_active(address, True, timeout_s=timeout)
            self.set_global_active(True, timeout_s=timeout)
            return True
        self.set_hv_module_target(address, 0.0, timeout_s=timeout)
        self.set_hv_module_active(address, False, timeout_s=timeout)
        return False

    def get_heat_configuration(self, timeout_s: Optional[float] = None) -> dict:
        """Read HEAT-CTRL-2410 hardware limits and configured setpoints."""
        self._require_connected()
        timeout = self._resolve_timeout(timeout_s)

        def configuration():
            def checked(result, action):
                self._raise_on_status(result[0], action)
                return result[1:]

            max_voltage, max_current, max_power, max_temperature = checked(
                ESIBase.get_heat_ctrl_hw_limits(self), "get_heat_ctrl_hw_limits"
            )
            voltage_limit, = checked(
                ESIBase.get_heat_ctrl_voltage_limit(self), "get_heat_ctrl_voltage_limit"
            )
            current_limit, = checked(
                ESIBase.get_heat_ctrl_current_limit(self), "get_heat_ctrl_current_limit"
            )
            power_limit, = checked(
                ESIBase.get_heat_ctrl_power_limit(self), "get_heat_ctrl_power_limit"
            )
            target_temperature, = checked(
                ESIBase.get_heat_ctrl_heater_temperature(self),
                "get_heat_ctrl_heater_temperature",
            )
            return {
                "hardware_limits": {
                    "max_voltage_v": float(max_voltage),
                    "max_current_a": float(max_current),
                    "max_power_w": float(max_power),
                    "max_temperature_c": float(max_temperature),
                },
                "voltage_limit_v": float(voltage_limit),
                "current_limit_a": float(current_limit),
                "power_limit_w": float(power_limit),
                "target_temperature_c": float(target_temperature),
            }

        return self._call_locked_with_timeout(
            configuration, timeout * 5.0, "get_heat_configuration"
        )

    def configure_heat_limits(
        self,
        *,
        voltage_v: Optional[float] = None,
        current_a: Optional[float] = None,
        power_w: Optional[float] = None,
        timeout_s: Optional[float] = None,
    ) -> dict:
        """Apply selected heater limits after validating hardware maxima."""
        self._require_connected()
        timeout = self._resolve_timeout(timeout_s)
        requested = {
            "voltage_v": None if voltage_v is None else float(voltage_v),
            "current_a": None if current_a is None else float(current_a),
            "power_w": None if power_w is None else float(power_w),
        }

        def configure():
            status, max_voltage, max_current, max_power, _max_temperature = (
                ESIBase.get_heat_ctrl_hw_limits(self)
            )
            self._raise_on_status(status, "get_heat_ctrl_hw_limits")
            maxima = {
                "voltage_v": float(max_voltage),
                "current_a": float(max_current),
                "power_w": float(max_power),
            }
            setters = {
                "voltage_v": ESIBase.set_heat_ctrl_voltage_limit,
                "current_a": ESIBase.set_heat_ctrl_current_limit,
                "power_w": ESIBase.set_heat_ctrl_power_limit,
            }
            for name, value in requested.items():
                if value is None:
                    continue
                if not 0 < value <= maxima[name]:
                    raise ValueError(
                        f"ESI heat {name} must be greater than 0 and no more "
                        f"than the hardware maximum {maxima[name]:g}."
                    )

            applied = {}
            for name, value in requested.items():
                if value is None:
                    continue
                result = setters[name](self, value)
                self._raise_on_status(result[0], f"set_heat_{name}")
                applied[name] = float(result[1])
            return applied

        return self._call_locked_with_timeout(
            configure, timeout * 4.0, "configure_heat_limits"
        )

    def set_heater_temperature(
        self, temperature_c: float, timeout_s: Optional[float] = None
    ) -> float:
        """Set heater target temperature within the reported hardware limit."""
        self._require_connected()
        timeout = self._resolve_timeout(timeout_s)
        target = float(temperature_c)

        def set_temperature():
            status, _max_v, _max_i, _max_p, max_temperature = (
                ESIBase.get_heat_ctrl_hw_limits(self)
            )
            self._raise_on_status(status, "get_heat_ctrl_hw_limits")
            if not 0 <= target <= float(max_temperature):
                raise ValueError(
                    "ESI heater target must be between 0 and the hardware "
                    f"maximum {float(max_temperature):g} degC."
                )
            status, applied = ESIBase.set_heat_ctrl_heater_temperature(self, target)
            self._raise_on_status(status, "set_heat_ctrl_heater_temperature")
            return float(applied)

        return self._call_locked_with_timeout(
            set_temperature, timeout * 2.0, "set_heater_temperature"
        )

    def collect_diagnostics(self, timeout_s: Optional[float] = None) -> dict:
        """Collect controller, HV, and heater state without changing outputs."""
        self._require_connected()
        timeout = self._resolve_timeout(timeout_s)

        def snapshot():
            def checked(result, action):
                self._raise_on_status(result[0], action)
                return result[1:]

            (
                data_flags,
                device_state,
                voltage_state,
                temperature_state,
                fan_state,
                interlock_state,
                main_state,
                module_data_flags,
                module_states,
            ) = checked(ESIBase.get_complete_state(self), "get_complete_state")
            main_hex = hex(int(main_state))
            main_name = self.MAIN_STATE.get(
                int(main_state), f"UNKNOWN_STATE_0x{int(main_state):04X}"
            )
            device_hex = hex(int(device_state))
            device_flags = [
                name
                for flag, name in self.DEVICE_STATE.items()
                if int(device_state) & flag
            ] or ["DEVST_OK"]
            voltage_hex = hex(int(voltage_state))
            voltage_flags = [
                name
                for flag, name in self.VOLTAGE_STATE.items()
                if int(voltage_state) & flag
            ]
            temperature_hex = hex(int(temperature_state))
            temperature_flags = [
                name
                for flag, name in self.TEMPERATURE_STATE.items()
                if int(temperature_state) & flag
            ]
            fan_hex = hex(int(fan_state))
            fan_flags = [
                name
                for flag, name in self.FAN_STATE.items()
                if int(fan_state) & flag
            ]
            interlock_hex = hex(int(interlock_state))
            interlock_flags = [
                name
                for flag, name in self.INTERLOCK_STATE.items()
                if int(interlock_state) & flag
            ]
            enabled, = checked(ESIBase.get_enable(self), "get_enable")
            housekeeping = checked(ESIBase.get_housekeeping(self), "get_housekeeping")
            modules = {}
            for address in self.HV_MODULE_ADDRESSES:
                module_state = int(module_states[address])
                voltage_negative, current_high = checked(
                    ESIBase.get_hv_supply_meas_ranges(self, address),
                    f"get_measurement_ranges({address})",
                )
                target, = checked(
                    ESIBase.get_hv_supply_target_output_voltage(self, address),
                    f"get_target({address})",
                )
                valid_v, measured_v = checked(
                    ESIBase.get_hv_supply_output_voltage(self, address),
                    f"get_voltage({address})",
                )
                valid_i, measured_a = checked(
                    ESIBase.get_hv_supply_output_current(self, address),
                    f"get_current({address})",
                )
                pwm = checked(
                    ESIBase.get_hv_supply_params_pwm(self, address),
                    f"get_pwm({address})",
                )
                led_red, led_green, led_blue = checked(
                    ESIBase.get_module_led_data(self, address),
                    f"get_module_led({address})",
                )
                module_active = bool(pwm[6])
                modules[address] = {
                    "active": bool(
                        enabled and module_active and float(target) != 0.0
                    ),
                    "module_active": module_active,
                    "module_state": module_state,
                    "control_active": bool(module_state & self.MS_CTRL_ACT),
                    "module_gate_active": bool(module_state & self.MS_MOD_ACT),
                    "device_gate_active": bool(module_state & self.MS_DEV_ACT),
                    "data_ready_flags": int(module_data_flags[address]),
                    "measurement": {
                        "voltage_polarity": (
                            "negative" if voltage_negative else "positive"
                        ),
                        "negative_voltage": bool(voltage_negative),
                        "high_current_range": bool(current_high),
                    },
                    "target_v": float(target),
                    "voltage_valid": bool(valid_v),
                    "measured_v": float(measured_v),
                    "current_valid": bool(valid_i),
                    "measured_a": float(measured_a),
                    "led": {
                        "red": bool(led_red),
                        "green": bool(led_green),
                        "blue": bool(led_blue),
                    },
                    "pwm": {
                        "period_s": float(pwm[0]),
                        "width_s": float(pwm[1]),
                        "phase_measured_s": float(pwm[2]),
                        "phase_set_s": float(pwm[3]),
                        "voltage_set_v": float(pwm[4]),
                        "voltage_measured_v": float(pwm[5]),
                        "data_ready_flags": int(pwm[7]),
                    },
                }
            heat_valid, heat_vout, heat_vmon, heat_imon, heat_tmon = checked(
                ESIBase.get_heat_ctrl_monitoring(self), "get_heat_ctrl_monitoring"
            )
            heat_output_voltage, = checked(
                ESIBase.get_heat_ctrl_output_voltage(self),
                "get_heat_ctrl_output_voltage",
            )
            heat_power, = checked(
                ESIBase.get_heat_ctrl_heater_power(self), "get_heat_ctrl_heater_power"
            )
            heat_interlock, = checked(
                ESIBase.get_heat_ctrl_ilock_state(self), "get_heat_ctrl_ilock_state"
            )
            heat_hk = checked(
                ESIBase.get_heat_ctrl_housekeeping(self),
                "get_heat_ctrl_housekeeping",
            )
            heat_configuration = self.get_heat_configuration_unlocked()
            heat_active = bool(
                enabled and heat_configuration["target_temperature_c"] > 0.0
            )
            return {
                "main_state": {"hex": main_hex, "name": main_name},
                "data_ready_flags": int(data_flags),
                "device_state": {"hex": device_hex, "flags": device_flags},
                "voltage_state": {"hex": voltage_hex, "flags": voltage_flags},
                "temperature_state": {
                    "hex": temperature_hex,
                    "flags": temperature_flags,
                },
                "fan_state": {"hex": fan_hex, "flags": fan_flags},
                "interlock_state": {"hex": interlock_hex, "flags": interlock_flags},
                "enabled": bool(enabled),
                "global_active": bool(
                    enabled
                    and (
                        heat_active
                        or any(module["active"] for module in modules.values())
                    )
                ),
                "housekeeping": {
                    "volt_24v": housekeeping[0],
                    "volt_5v": housekeeping[1],
                    "volt_3v3": housekeeping[2],
                    "temp_cpu_c": housekeeping[3],
                    "temp_psu_c": housekeeping[4],
                },
                "modules": modules,
                "heat": {
                    "active": bool(heat_active),
                    "valid": bool(heat_valid),
                    "output_voltage_v": float(heat_output_voltage),
                    "heater_power_w": float(heat_power),
                    "monitor_output_v": float(heat_vout),
                    "monitor_voltage_v": float(heat_vmon),
                    "monitor_current_a": float(heat_imon),
                    "monitor_temperature_c": float(heat_tmon),
                    "interlock_state": int(heat_interlock),
                    "housekeeping": {
                        "valid": bool(heat_hk[0]),
                        "volt_3v3": float(heat_hk[1]),
                        "temp_cpu_c": float(heat_hk[2]),
                        "volt_5v": float(heat_hk[3]),
                        "volt_24v": float(heat_hk[4]),
                        "temp_psu_c": float(heat_hk[5]),
                    },
                    **heat_configuration,
                },
            }

        return self._call_locked_with_timeout(
            snapshot, timeout * 20.0, "collect_diagnostics"
        )

    def get_heat_configuration_unlocked(self) -> dict:
        """Read heat configuration while the caller already owns the DLL lock."""
        def checked(result, action):
            self._raise_on_status(result[0], action)
            return result[1:]

        max_voltage, max_current, max_power, max_temperature = checked(
            ESIBase.get_heat_ctrl_hw_limits(self), "get_heat_ctrl_hw_limits"
        )
        voltage_limit, = checked(
            ESIBase.get_heat_ctrl_voltage_limit(self), "get_heat_ctrl_voltage_limit"
        )
        current_limit, = checked(
            ESIBase.get_heat_ctrl_current_limit(self), "get_heat_ctrl_current_limit"
        )
        power_limit, = checked(
            ESIBase.get_heat_ctrl_power_limit(self), "get_heat_ctrl_power_limit"
        )
        target_temperature, = checked(
            ESIBase.get_heat_ctrl_heater_temperature(self),
            "get_heat_ctrl_heater_temperature",
        )
        return {
            "hardware_limits": {
                "max_voltage_v": float(max_voltage),
                "max_current_a": float(max_current),
                "max_power_w": float(max_power),
                "max_temperature_c": float(max_temperature),
            },
            "voltage_limit_v": float(voltage_limit),
            "current_limit_a": float(current_limit),
            "power_limit_w": float(power_limit),
            "target_temperature_c": float(target_temperature),
        }

    def disconnect(self, timeout_s: Optional[float] = None) -> bool:
        if not self.connected:
            if not self._transport_poisoned:
                self._release_single_instance()
            return True
        timeout = self._resolve_timeout(timeout_s)
        safe = False
        try:
            self.force_safe_off(timeout_s=timeout)
            self.set_global_active(False, timeout_s=timeout)
            safe = True
        finally:
            if not self._transport_poisoned:
                status = self._call_locked_with_timeout(
                    ESIBase.close_port, timeout, "close_port", self
                )
                self.connected = False
                self._release_single_instance()
                self._raise_on_status(status, "close_port")
        if not safe:
            raise RuntimeError("ESI shutdown could not be confirmed.")
        return True


class ESI(ProcessIsolatedClientMixin):
    """Public ESI facade with optional worker-process isolation on Windows."""

    _INSTRUMENT_NAME = "ESI"
    _PROCESS_CONTROLLER_CLASS = _ESIController
    _PROCESS_CONTROLLER_PATH = f"{__package__}.esi:_ESIController"
    _PROCESS_TIMEOUT_RULES = {
        "connect": (30.0, 10.0, 90.0),
        "collect_identity": (25.0, 10.0, 90.0),
        "collect_diagnostics": (15.0, 10.0, 60.0),
        "get_heat_configuration": (8.0, 10.0, 45.0),
        "configure_heat_limits": (8.0, 10.0, 45.0),
        "configure_hv_max_voltage_steps": (8.0, 10.0, 45.0),
        "set_heater_temperature": (4.0, 10.0, 30.0),
        "force_safe_off": (8.0, 10.0, 45.0),
        "disconnect": (10.0, 10.0, 60.0),
    }
    NO_ERR = ESIBase.NO_ERR
    DEVICE_TYPE = ESIBase.DEVICE_TYPE
    MODULE_HVPS_TYPE = ESIBase.MODULE_HVPS_TYPE
    MODULE_HTCTRL_TYPE = ESIBase.MODULE_HTCTRL_TYPE
    HEAT_MODULE_ADDRESS = _ESIController.HEAT_MODULE_ADDRESS
    HV_MODULE_ADDRESSES = _ESIController.HV_MODULE_ADDRESSES
    MAX_ABS_VOLTAGE_V = _ESIController.MAX_ABS_VOLTAGE_V

    def __init__(
        self,
        device_id: str,
        com: int,
        baudrate: int = 230400,
        logger: Optional[logging.Logger] = None,
        thread_lock: Optional[threading.Lock] = None,
        dll_path: Optional[str] = None,
        log_dir: Optional[Path] = None,
        process_backend: bool = False,
    ):
        backend_kwargs = {
            "device_id": device_id,
            "com": com,
            "baudrate": baudrate,
            "logger": logger,
            "thread_lock": thread_lock,
            "dll_path": dll_path,
            "log_dir": log_dir,
        }
        self._initialize_process_backend(
            backend_kwargs=backend_kwargs,
            incompatible_objects={"logger": logger, "thread_lock": thread_lock},
            allow_process_backend=bool(process_backend),
            process_backend_disabled_reason=(
                "ESI process isolation disabled by configuration; inline DLL calls "
                "cannot recover a COM port after a vendor call blocks."
            ),
        )
