"""Timeout-safe high-level driver for the CGC ESI controller."""

from __future__ import annotations

import contextlib
import logging
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
    HV_MODULE_ADDRESSES = (2, 3)
    MAX_ABS_VOLTAGE_V = 3000.0
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
        allow_negative: bool = False,
        **kwargs,
    ):
        if kwargs:
            unexpected = ", ".join(sorted(kwargs))
            raise TypeError(f"Unexpected ESI init kwargs: {unexpected}")
        self._validate_init_args(device_id, com, baudrate)
        self.device_id = device_id
        self.com = int(com)
        self.baudrate = int(baudrate)
        self.allow_negative = bool(allow_negative)
        self.connected = False
        self._transport_poisoned = False
        self._transport_error = None
        self._module_inventory: dict[int, dict] = {}
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
                f"modules={sorted(modules)}; HV outputs forced OFF"
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
        """Disable global HV before reading and validating addressed modules."""
        def prepare():
            status = ESIBase.set_activation_state(self, False)
            self._raise_on_status(status, "global deactivate before inventory")
            status = ESIBase.set_enable(self, True)
            self._raise_on_status(status, "module communication enable")

        self._call_locked_with_timeout(
            prepare, timeout * 2.0, "prepare_safe_inventory"
        )

    def force_safe_off(self, timeout_s: Optional[float] = None) -> bool:
        """Zero both lab HV modules and disable all activation levels."""
        self._require_connected()
        timeout = self._resolve_timeout(timeout_s)

        def safe_off_batch():
            failures = []
            status = ESIBase.set_activation_state(self, False)
            if status != self.NO_ERR:
                failures.append(f"global deactivate: {self.format_status(status)}")
            status = ESIBase.set_enable(self, True)
            if status != self.NO_ERR:
                failures.append(f"module communication enable: {self.format_status(status)}")
            for address in self.HV_MODULE_ADDRESSES:
                for action, status in (
                    ("zero target", ESIBase.set_hv_supply_target_output_voltage(self, address, 0.0)),
                    ("deactivate", ESIBase.set_module_activation_state(self, address, False)),
                ):
                    if status != self.NO_ERR:
                        failures.append(f"module {address} {action}: {self.format_status(status)}")
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

        for address in self.HV_MODULE_ADDRESSES:
            info = modules.get(address)
            if info is None:
                raise RuntimeError(f"Required ESI HV module {address} was not detected.")
            if info.get("presence") != self.MODULE_PRESENT:
                raise RuntimeError(f"ESI module {address} is present but invalid.")
            if info.get("device_type") != self.MODULE_HVPS_TYPE:
                raise RuntimeError(
                    f"ESI module {address} type mismatch: expected "
                    f"0x{self.MODULE_HVPS_TYPE:04X}, got "
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

    def _validate_voltage(self, voltage: float) -> float:
        value = float(voltage)
        if abs(value) > self.MAX_ABS_VOLTAGE_V:
            raise ValueError(
                f"ESI target {value:g} V exceeds the absolute 3000 V hardware limit."
            )
        if value < 0 and not self.allow_negative:
            raise ValueError(
                "Negative ESI voltages are disabled; enable the advanced polarity "
                "setting only after confirming the installed module configuration."
            )
        return value

    def set_target_voltage(
        self, address: int, voltage: float, timeout_s: Optional[float] = None
    ) -> float:
        self._require_connected()
        address = self._validate_hv_address(address)
        value = self._validate_voltage(voltage)
        timeout = self._resolve_timeout(timeout_s)
        status = self._call_locked_with_timeout(
            ESIBase.set_hv_supply_target_output_voltage,
            timeout,
            f"set_target_voltage[{address}]",
            self,
            address,
            value,
        )
        self._raise_on_status(status, f"set_target_voltage({address})")
        return value

    def set_global_active(self, active: bool, timeout_s: Optional[float] = None) -> bool:
        self._require_connected()
        timeout = self._resolve_timeout(timeout_s)
        status = self._call_locked_with_timeout(
            ESIBase.set_activation_state,
            timeout,
            "set_global_active",
            self,
            bool(active),
        )
        self._raise_on_status(status, "set_global_active")
        return bool(active)

    def set_output_active(
        self, address: int, active: bool, timeout_s: Optional[float] = None
    ) -> bool:
        self._require_connected()
        address = self._validate_hv_address(address)
        timeout = self._resolve_timeout(timeout_s)
        status = self._call_locked_with_timeout(
            ESIBase.set_module_activation_state,
            timeout,
            f"set_output_active[{address}]",
            self,
            address,
            bool(active),
        )
        self._raise_on_status(status, f"set_output_active({address})")
        return bool(active)

    def collect_diagnostics(self, timeout_s: Optional[float] = None) -> dict:
        """Collect one controller and HV-module snapshot without changing state."""
        self._require_connected()
        timeout = self._resolve_timeout(timeout_s)

        def snapshot():
            def checked(result, action):
                self._raise_on_status(result[0], action)
                return result[1:]

            main_hex, main_name = checked(ESIBase.get_main_state(self), "get_main_state")
            device_hex, device_flags = checked(ESIBase.get_device_state(self), "get_device_state")
            voltage_hex, voltage_flags = checked(ESIBase.get_voltage_state(self), "get_voltage_state")
            interlock_hex, interlock_flags = checked(ESIBase.get_interlock_state(self), "get_interlock_state")
            enabled, = checked(ESIBase.get_enable(self), "get_enable")
            global_active, = checked(ESIBase.get_activation_state(self), "get_activation_state")
            housekeeping = checked(ESIBase.get_housekeeping(self), "get_housekeeping")
            modules = {}
            for address in self.HV_MODULE_ADDRESSES:
                active, = checked(
                    ESIBase.get_module_activation_state(self, address),
                    f"get_module_activation_state({address})",
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
                modules[address] = {
                    "active": bool(active),
                    "target_v": float(target),
                    "voltage_valid": bool(valid_v),
                    "measured_v": float(measured_v),
                    "current_valid": bool(valid_i),
                    "measured_a": float(measured_a),
                }
            return {
                "main_state": {"hex": main_hex, "name": main_name},
                "device_state": {"hex": device_hex, "flags": device_flags},
                "voltage_state": {"hex": voltage_hex, "flags": voltage_flags},
                "interlock_state": {"hex": interlock_hex, "flags": interlock_flags},
                "enabled": bool(enabled),
                "global_active": bool(global_active),
                "housekeeping": {
                    "volt_24v": housekeeping[0],
                    "volt_5v": housekeeping[1],
                    "volt_3v3": housekeeping[2],
                    "temp_cpu_c": housekeeping[3],
                    "temp_psu_c": housekeeping[4],
                },
                "modules": modules,
            }

        return self._call_locked_with_timeout(
            snapshot, timeout * 12.0, "collect_diagnostics"
        )

    def disconnect(self, timeout_s: Optional[float] = None) -> bool:
        if not self.connected:
            if not self._transport_poisoned:
                self._release_single_instance()
            return True
        timeout = self._resolve_timeout(timeout_s)
        safe = False
        try:
            self.force_safe_off(timeout_s=timeout)
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
        "force_safe_off": (8.0, 10.0, 45.0),
        "disconnect": (10.0, 10.0, 60.0),
    }
    NO_ERR = ESIBase.NO_ERR
    DEVICE_TYPE = ESIBase.DEVICE_TYPE
    MODULE_HVPS_TYPE = ESIBase.MODULE_HVPS_TYPE
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
        allow_negative: bool = False,
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
            "allow_negative": allow_negative,
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
