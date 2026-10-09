"""Timeout-safe high-level driver for the CGC ESI controller."""

from __future__ import annotations

import logging
import math
import struct
import threading
import time
from pathlib import Path
from typing import Optional

from .._driver_common import (
    DllPortClaimRegistryMixin,
    ProcessIsolatedClientMixin,
    TimeoutSafeDllMixin,
    build_device_logger,
)
from .esi_base import ESIBase


class _ESIController(DllPortClaimRegistryMixin, TimeoutSafeDllMixin, ESIBase):
    """Validated ESI controller with deterministic high-voltage shutdown."""

    _INSTRUMENT_NAME = "ESI"
    _DEFAULT_IO_TIMEOUT_S = 5.0
    HEAT_MODULE_ADDRESS = 0
    HV_MODULE_ADDRESSES = (1, 2)
    CONTROLLED_MODULE_ADDRESSES = (HEAT_MODULE_ADDRESS, *HV_MODULE_ADDRESSES)
    MAX_ABS_VOLTAGE_V = 3000.0
    # Operational shutdown check, not a touch-safe certification.
    DISCHARGE_LIMIT_V = 1.0
    DISCHARGE_SAMPLES = 3
    DISCHARGE_TIMEOUT_S = 60.0
    HEAT_MON_READY = 1 << 0  # CGC COM_ESI_CTRL_HTCTRL_MON_RDY
    HV_ADC_V_READY = 1 << 4
    HV_ADC_I_READY = 1 << 6
    HV_CONFIG_BASE_OFFSET = 17
    HV_CONFIG_STRIDE = 12
    HV_CONFIG_MAX_STEP_OFFSET = 4
    HV_CONFIG_ENABLE_OFFSET = 10
    DEFAULT_HV_MAX_VOLTAGE_STEP_V = 10.008
    # Heater safety contract the heater notebooks require (instead of a source
    # hash): limits/temperature validated before ON, direct activation readback,
    # cancellable heater reads. Keep it in later versions; bump it if the
    # contract changes incompatibly.
    HEATER_SAFETY_CONTRACT = 1
    _active_connections_lock = threading.Lock()
    _active_connections: dict[int, dict[str, object]] = {}

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
        self.port_num = 0  # The ESI DLL has one implicit native channel.
        self._dll_port_claimed = False
        self.connected = False
        self._transport_poisoned = False
        self._transport_error = None
        self._communication_error = None
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
            if (-14 <= status <= -7 or status == -100) and action not in {"close_port", "force_close_port"}:
                self._communication_error = f"{action}: {self.format_status(status)}"
            raise RuntimeError(f"ESI {action} failed: {self.format_status(status)}")

    def _call_locked_with_timeout(self, method, timeout_s, step_name, *args, **kwargs):
        def call():
            error = getattr(self, "_communication_error", None)
            if error is not None:
                # CGC prescribes Purge after communication errors. Serialize it with
                # all native I/O and never replay the failed output command.
                self.logger.warning(f"Restoring ESI communication with CGC Purge after {error}")
                status = ESIBase.purge(self)
                self._raise_on_status(status, "purge")
                self._communication_error = None
            return method(*args, **kwargs)

        return super()._call_locked_with_timeout(call, timeout_s, step_name)

    def connect(self, timeout_s: float = 5.0, *, preserve_outputs: bool = False) -> bool:
        """Connect, validate identity, inventory modules, and force HV OFF."""
        already_connected = self._connection_is_ready()
        timeout = self._resolve_timeout(timeout_s)
        if already_connected:
            return True
        self._hv_measurement_requests.clear()
        opened = False
        try:
            status = self._call_initial_open(
                ESIBase.open_port, lambda: ESIBase.close_port(self),
                timeout, self, self.com
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
            if not preserve_outputs:
                self._prepare_safe_inventory(timeout)
                self.force_safe_off(timeout_s=timeout)
            modules = self.discover_modules(timeout_s=timeout)
            self.logger.info(
                f"Connected on COM{self.com} at {actual_baud} baud; "
                f"modules={sorted(modules)}; "
                + ("existing outputs preserved" if preserve_outputs else "HV and heater outputs forced OFF")
            )
            return True
        except Exception:
            # The connect phase is over before rollback. Once identity has
            # been validated, disconnect must verify HV OFF/discharge too;
            # never discard an uncertain output state with a raw port close.
            self._finish_initial_open()
            if opened and not self._transport_poisoned and not preserve_outputs:
                try:
                    self.disconnect(timeout_s=timeout)
                except Exception as cleanup_error:
                    self.logger.error(f"ESI connect cleanup unconfirmed: {cleanup_error}")
            raise
        finally:
            self._finish_initial_open()

    def _prepare_safe_inventory(self, timeout: float) -> None:
        """Disable all outputs before inventory communication."""
        def prepare():
            status = ESIBase.set_enable(self, False)
            self._raise_on_status(status, "module disable before inventory")
            # Global OFF preserves module gates. Disarm every output before
            # reopening communication, including HV with stored nonzero targets.
            self._set_heat_module_active_unlocked(False)
            for address in self.HV_MODULE_ADDRESSES:
                status, read_status, active = self._set_hv_module_active_unlocked(address, False)
                self._raise_on_status(status, f"inventory HV{address} disable")
                self._raise_on_status(read_status, f"inventory HV{address} disable readback")
                if active:
                    raise RuntimeError(f"ESI inventory HV{address} remained active")
            self._raise_if_transport_poisoned()
            status = ESIBase.set_enable(self, True)
            self._raise_on_status(status, "module communication enable")

        self._call_locked_with_timeout(
            prepare, timeout * 10.0, "prepare_safe_inventory"
        )

    def force_safe_off(self, timeout_s: Optional[float] = None) -> bool:
        """Zero both HV targets and disable HV and heater activation."""
        self._require_connected()
        timeout = self._resolve_timeout(timeout_s)

        def safe_off_batch():
            failures = self._disable_heat_unlocked()
            for address in self.HV_MODULE_ADDRESSES:
                self._raise_if_transport_poisoned()
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
            self._raise_if_transport_poisoned()
            status = ESIBase.set_enable(self, False)
            if status != self.NO_ERR:
                failures.append(f"module disable: {self.format_status(status)}")
                return failures
            # Read back the global enable gate; retry once when the DLL
            # reports success but the controller is still enabled.
            readback_status, still_enabled = ESIBase.get_enable(self)
            if readback_status == self.NO_ERR and still_enabled:
                self._raise_if_transport_poisoned()
                status = ESIBase.set_enable(self, False)
                if status != self.NO_ERR:
                    failures.append(
                        f"module disable retry: {self.format_status(status)}"
                    )
                else:
                    readback_status, still_enabled = ESIBase.get_enable(self)
                    if readback_status != self.NO_ERR:
                        failures.append(
                            "global enable readback: "
                            f"{self.format_status(readback_status)}"
                        )
            elif readback_status != self.NO_ERR:
                failures.append(
                    f"global enable readback: {self.format_status(readback_status)}"
                )
            if still_enabled:
                failures.append(
                    "global enable remained ON after disable; outputs may be live"
                )
            return failures

        failures = self._call_locked_with_timeout(
            safe_off_batch, timeout * 8.0, "force_safe_off"
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
        for address, info in modules.items():
            if address not in self.CONTROLLED_MODULE_ADDRESSES and (
                info.get("device_type") == self.MODULE_HVPS_TYPE
            ):
                raise RuntimeError(
                    f"Unexpected HV module at address {address}; "
                    "refusing to connect with uncontrolled HV outputs."
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

    def _list_configs_unlocked(self, include_empty: bool = False) -> list:
        status, active_flags, valid_flags = ESIBase.get_config_list(self)
        self._raise_on_status(status, "get_config_list")
        configs: list = []
        for index in range(self.MAX_CONFIG):
            if not include_empty and not active_flags[index]:
                continue
            name_status, name = ESIBase.get_config_name(self, index)
            self._raise_on_status(name_status, f"get_config_name({index})")
            flag_status, active, valid = ESIBase.get_config_flags(self, index)
            self._raise_on_status(flag_status, f"get_config_flags({index})")
            configs.append(
                {
                    "index": index,
                    "name": name,
                    "active": active,
                    "valid": valid,
                }
            )
        return configs

    def list_configs(
        self, include_empty: bool = False, timeout_s: Optional[float] = None
    ) -> list:
        """Return ESI configuration slots with flags and names."""
        self._require_connected()
        timeout = self._resolve_timeout(timeout_s) * 3.0
        return self._call_locked_with_timeout(
            self._list_configs_unlocked, timeout, "list_configs", include_empty
        )

    def load_config(
        self, config_number: int, timeout_s: Optional[float] = None
    ) -> None:
        """Load one ESI configuration from controller NVM.

        Re-applies the volatile HV ramp-step patch afterwards because a
        saved OFF configuration has MaxVoltStep=0, which blocks HV control.
        """
        self._require_connected()
        if not isinstance(config_number, int) or not 0 <= config_number < self.MAX_CONFIG:
            raise ValueError(
                f"ESI config number must be 0..{self.MAX_CONFIG - 1}, "
                f"got {config_number}."
            )
        timeout = self._resolve_timeout(timeout_s)
        self.force_safe_off(timeout_s=timeout)
        status = self._call_locked_with_timeout(
            ESIBase.load_current_config,
            timeout,
            "load_current_config",
            self,
            config_number,
        )
        self._raise_on_status(status, f"load_current_config({config_number})")
        self.configure_hv_max_voltage_steps(
            self.DEFAULT_HV_MAX_VOLTAGE_STEP_V, timeout_s=timeout_s
        )
        self.force_safe_off(timeout_s=timeout)

    def save_config(
        self,
        config_number: int,
        *,
        name: Optional[str] = None,
        active: Optional[bool] = None,
        valid: Optional[bool] = None,
        timeout_s: Optional[float] = None,
    ) -> None:
        """Save the current ESI state into one NVM configuration slot."""
        self._require_connected()
        if not isinstance(config_number, int) or not 0 <= config_number < self.MAX_CONFIG:
            raise ValueError(
                f"ESI config number must be 0..{self.MAX_CONFIG - 1}, "
                f"got {config_number}."
            )
        timeout = self._resolve_timeout(timeout_s)
        status = self._call_locked_with_timeout(
            ESIBase.save_current_config_to_slot,
            timeout,
            "save_current_config_to_slot",
            self,
            config_number,
        )
        self._raise_on_status(status, f"save_current_config_to_slot({config_number})")
        if name is not None:
            name_status = self._call_locked_with_timeout(
                ESIBase.set_config_name,
                timeout,
                "set_config_name",
                self,
                config_number,
                name,
            )
            self._raise_on_status(name_status, f"set_config_name({config_number})")
        if active is not None or valid is not None:
            active_value = True if active is None else bool(active)
            valid_value = True if valid is None else bool(valid)
            flag_status = self._call_locked_with_timeout(
                ESIBase.set_config_flags,
                timeout,
                "set_config_flags",
                self,
                config_number,
                active_value,
                valid_value,
            )
            self._raise_on_status(flag_status, f"set_config_flags({config_number})")

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

    def select_hv_voltage_adc(
        self, address: int, *, negative: bool, timeout_s: Optional[float] = None
    ) -> None:
        """Point the voltage ADC at one connector and verify it; keep the current range.

        Measurement only: no output, target or activation command is sent.
        """
        self._require_connected()
        address = self._validate_hv_address(address)
        requested = bool(negative)
        timeout = self._resolve_timeout(timeout_s)

        def select_and_verify():
            status, _negative, high_current = ESIBase.get_hv_supply_meas_ranges(self, address)
            self._raise_on_status(status, f"get_measurement_ranges({address})")
            high_current = bool(high_current)
            status = ESIBase.set_hv_supply_meas_ranges(self, address, requested, high_current)
            self._raise_on_status(status, f"select_hv_voltage_adc({address})")
            status, observed_negative, observed_high = ESIBase.get_hv_supply_meas_ranges(self, address)
            self._raise_on_status(status, f"verify_hv_voltage_adc({address})")
            observed = bool(observed_negative), bool(observed_high)
            if observed != (requested, high_current):
                raise RuntimeError(
                    f"ESI module {address} voltage ADC selection verification failed: "
                    f"requested {(requested, high_current)}, controller reports {observed}"
                )
            self._hv_measurement_requests[address] = (requested, high_current, True)

        self._call_locked_with_timeout(
            select_and_verify, timeout * 3.0, f"select_hv_voltage_adc[{address}]"
        )

    def set_global_active(self, active: bool, timeout_s: Optional[float] = None, *, cancel_event=None) -> bool:
        self._require_connected()
        timeout = self._resolve_timeout(timeout_s)
        requested = bool(active)

        def set_and_verify():
            if requested and cancel_event is not None:
                # Stop may arrive after dispatch but before this worker starts.
                self._check_heat_read_permission(cancel_event)
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
        *,
        cancel_event=None,
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
            cancel_event,
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
        self, address: int, requested: bool, cancel_event=None
    ) -> tuple[int, int, bool]:
        self._raise_if_transport_poisoned()
        if requested:
            self._check_heat_read_permission(cancel_event)
        status = ESIBase.set_module_activation_state(
            self, address, requested
        )
        self._raise_if_transport_poisoned()
        if status != self.NO_ERR:
            return status, self.NO_ERR, not requested
        pwm = ESIBase.get_hv_supply_params_pwm(self, address)
        if pwm[0] != self.NO_ERR:
            self._raise_if_transport_poisoned()
            pwm = ESIBase.get_hv_supply_params_pwm(self, address)
        return status, pwm[0], bool(pwm[7])

    def _set_heat_module_active_unlocked(self, requested: bool) -> bool:
        """Command module 0 and require its direct activation readback.

        The CGC module command includes HEAT, not only the HV converters.
        No missing acknowledgement (including -10) is accepted for HEAT.
        """
        self._raise_if_transport_poisoned()
        status = ESIBase.set_module_activation_state(
            self, self.HEAT_MODULE_ADDRESS, requested
        )
        self._raise_on_status(status, "set_heat_module_active(0)")
        self._raise_if_transport_poisoned()
        status, observed = ESIBase.get_module_activation_state(
            self, self.HEAT_MODULE_ADDRESS
        )
        self._raise_on_status(status, "verify_heat_module_active(0)")
        if bool(observed) != requested:
            raise RuntimeError(
                "ESI heater module 0 activation verification failed: "
                f"requested {requested}, controller reports {bool(observed)}"
            )
        return bool(observed)

    def _set_heat_temperature_unlocked(self, target: float, maximum: float = 0.0) -> float:
        self._raise_if_transport_poisoned()
        status, applied = ESIBase.set_heat_ctrl_heater_temperature(self, target)
        self._raise_on_status(status, "set_heat_ctrl_heater_temperature")
        # The native in/out argument is the applied, potentially quantized value.
        if not math.isfinite(applied) or not 0 <= applied <= maximum:
            raise RuntimeError("ESI heater applied temperature is outside the allowed range")
        self._raise_if_transport_poisoned()
        status, observed = ESIBase.get_heat_ctrl_heater_temperature(self)
        self._raise_on_status(status, "verify_heat_ctrl_heater_temperature")
        if not math.isfinite(observed) or not math.isclose(
            observed, applied, rel_tol=1e-9, abs_tol=1e-6
        ):
            raise RuntimeError(
                "ESI heater temperature verification failed: "
                f"applied {applied:g} degC, controller reports {observed:g} degC"
            )
        return float(observed)

    def _disable_heat_unlocked(self) -> list[str]:
        """Disable the gate, then zero the target, retaining returned failures."""
        failures = []
        for operation in (
            lambda: self._set_heat_module_active_unlocked(False),
            lambda: self._set_heat_temperature_unlocked(0.0),
        ):
            self._raise_if_transport_poisoned()
            try:
                operation()
            except RuntimeError as exc:
                failures.append(str(exc))
        self._raise_if_transport_poisoned()
        return failures

    def _check_heat_read_permission(self, cancel_event=None) -> None:
        self._raise_if_transport_poisoned()
        if getattr(self, "_transport_poisoned", None) is not False:
            raise RuntimeError("ESI transport state is unknown; output operation refused")
        if cancel_event is not None and cancel_event.is_set():
            raise InterruptedError("ESI output operation cancelled")

    def _get_heat_monitoring_unlocked(self, timeout: float, cancel_event=None):
        """Wait for MON_RDY under the caller's lock, then read exactly once.

        No-data is not a sensor reading. Conversely, an invalid tuple after
        readiness is returned unchanged to the caller, never retried or cached.
        The readiness deadline does not replace the outer native-call timeout.
        """
        deadline = time.monotonic() + timeout
        while True:
            self._check_heat_read_permission(cancel_event)
            if time.monotonic() >= deadline:
                raise RuntimeError("ESI heater monitoring data not ready within the I/O timeout")
            status, flags = ESIBase.get_module_data_ready_flags(self, self.HEAT_MODULE_ADDRESS)
            self._check_heat_read_permission(cancel_event)
            self._raise_on_status(status, "get_heat_monitoring_ready(0)")
            if time.monotonic() >= deadline:
                raise RuntimeError("ESI heater monitoring data not ready within the I/O timeout")
            if int(flags) & self.HEAT_MON_READY:
                result = ESIBase.get_heat_ctrl_monitoring(self)
                self._check_heat_read_permission(cancel_event)
                return result
            delay = min(.1, max(0., deadline - time.monotonic()))
            if cancel_event is None:
                time.sleep(delay)
            else:
                cancel_event.wait(delay)

    def _validate_heat_operating_state_unlocked(
        self, target: Optional[float] = None, *, timeout: Optional[float] = None,
        cancel_event=None,
    ) -> float:
        self._check_heat_read_permission(cancel_event)
        configuration = self.get_heat_configuration_unlocked()
        maxima = configuration["hardware_limits"]
        for name, unit in (("voltage", "v"), ("current", "a"), ("power", "w")):
            value = configuration[f"{name}_limit_{unit}"]
            maximum = maxima[f"max_{name}_{unit}"]
            if not (math.isfinite(value) and math.isfinite(maximum)
                    and 0 < value <= maximum):
                raise RuntimeError(
                    f"ESI heater {name} limit is {value:g} {unit.upper()}; "
                    "configure a positive limit appropriate for the load "
                    f"and no greater than {maximum:g} {unit.upper()} before ON. "
                    "A plugin setting of zero retains the device setting."
                )
        maximum = maxima["max_temperature_c"]
        if target is None:
            target = configuration["target_temperature_c"]
        if not (math.isfinite(maximum) and maximum > 0
                and math.isfinite(target) and 0 <= target <= maximum):
            raise RuntimeError("ESI heater temperature target or hardware limit is invalid")
        self._raise_if_transport_poisoned()
        status, valid, _vout, _vmon, _imon, temperature = self._get_heat_monitoring_unlocked(
            self._resolve_timeout(timeout), cancel_event
        )
        self._raise_on_status(status, "get_heat_ctrl_monitoring before ON")
        if not valid or not math.isfinite(temperature) or not 0 <= temperature <= maximum:
            raise RuntimeError("ESI heater temperature sensor readback is invalid; ON refused")
        return float(maximum)

    def _enable_heat_unlocked(self, timeout: float, cancel_event=None) -> bool:
        self._validate_heat_operating_state_unlocked(timeout=timeout, cancel_event=cancel_event)
        self._check_heat_read_permission(cancel_event)
        return self._set_heat_module_active_unlocked(True)

    def _confirm_heat_active_unlocked(self, timeout: float, cancel_event=None) -> bool:
        """Confirm the asynchronous state transition, never replay ON.

        The I/O budget and 0.1 s polling cadence are software bounds, not
        measured firmware settling times. Only missing activation bits may
        wait; explicit faults, lost command gates and bad responses fail.
        """
        deadline = time.monotonic() + timeout
        last_state = None

        def permitted():
            self._check_heat_read_permission(cancel_event)
            if time.monotonic() >= deadline:
                observed = "unavailable" if last_state is None else hex(last_state)
                raise RuntimeError(
                    "ESI heater activation not confirmed within the I/O timeout; "
                    f"last module state={observed}"
                )

        def checked(method, *args):
            permitted()
            result = method(self, *args)
            self._check_heat_read_permission(cancel_event)
            self._raise_on_status(result[0], f"confirm_heat_activation/{method.__name__}")
            permitted()
            return result[1:]

        while True:
            try:
                (_, device_state, _, _, _, _, main_state, _, module_states) = checked(
                    ESIBase.get_complete_state
                )
                last_state = int(module_states[self.HEAT_MODULE_ADDRESS])
                device_state, main_state = int(device_state), int(main_state)
            except (TypeError, ValueError, IndexError) as exc:
                raise RuntimeError("ESI heater activation received an invalid complete state") from exc
            if device_state or self.MAIN_STATE.get(main_state) != "STATE_ON":
                raise RuntimeError(
                    "ESI heater activation blocked by controller fault/state: "
                    f"device={hex(device_state)}, main={hex(main_state)}, module={hex(last_state)}"
                )
            enabled, = checked(ESIBase.get_enable)
            module_active, = checked(ESIBase.get_module_activation_state, self.HEAT_MODULE_ADDRESS)
            if not enabled or not module_active:
                raise RuntimeError(
                    "ESI heater activation command readback lost: "
                    f"enabled={bool(enabled)}, module_active={bool(module_active)}, state={hex(last_state)}"
                )
            required = self.MS_CTRL_ACT | self.MS_MOD_ACT | self.MS_DEV_ACT
            permitted()
            if last_state & required == required:
                return True
            delay = min(.1, max(0., deadline - time.monotonic()))
            if cancel_event is None:
                time.sleep(delay)
            else:
                cancel_event.wait(delay)

    def set_output_active(
        self, address: int, active: bool, timeout_s: Optional[float] = None, *, cancel_event=None
    ) -> bool:
        """Verify the selected module gate before opening the shared gate.

        HEAT requires valid sensor/limits, command readbacks, and confirmed
        CTRL/MOD/DEV activation state within the existing I/O budget.
        Local OFF disables only that module and zeros its target; it does not
        interrupt other outputs or depend on valid heater operating limits.
        """
        self._require_connected()
        address = self._validate_controlled_address(address)
        timeout = self._resolve_timeout(timeout_s)
        if address == self.HEAT_MODULE_ADDRESS:
            if active:
                self._call_locked_with_timeout(
                    self._enable_heat_unlocked, timeout * 8.0, "enable_heat_output", timeout, cancel_event
                )
                self._check_heat_read_permission(cancel_event)
                self.set_global_active(True, timeout_s=timeout, cancel_event=cancel_event)
                self._call_locked_with_timeout(
                    self._confirm_heat_active_unlocked, timeout * 2.0,
                    "confirm_heat_activation", timeout, cancel_event,
                )
                self._check_heat_read_permission(cancel_event)
                return True
            failures = self._call_locked_with_timeout(
                self._disable_heat_unlocked, timeout * 4.0, "disable_heat_output"
            )
            if failures:
                raise RuntimeError("ESI heater OFF failed: " + "; ".join(failures))
            return False
        if active:
            self.set_hv_module_active(address, True, timeout_s=timeout, cancel_event=cancel_event)
            self.set_global_active(True, timeout_s=timeout, cancel_event=cancel_event)
            self._check_heat_read_permission(cancel_event)
            return True
        self.set_hv_module_target(address, 0.0, timeout_s=timeout)
        self.set_hv_module_active(address, False, timeout_s=timeout)
        return False

    def get_heat_configuration(self, timeout_s: Optional[float] = None) -> dict:
        """Read HEAT-CTRL-2410 hardware limits and configured setpoints."""
        self._require_connected()
        timeout = self._resolve_timeout(timeout_s)

        return self._call_locked_with_timeout(
            self.get_heat_configuration_unlocked, timeout * 5.0, "get_heat_configuration"
        )

    def configure_heat_limits(
        self,
        *,
        voltage_v: Optional[float] = None,
        current_a: Optional[float] = None,
        power_w: Optional[float] = None,
        timeout_s: Optional[float] = None,
        cancel_event=None,
    ) -> dict:
        """Apply selected heater limits after validating hardware maxima; honor Stop in the worker."""
        self._require_connected()
        timeout = self._resolve_timeout(timeout_s)
        requested = {
            "voltage_v": None if voltage_v is None else float(voltage_v),
            "current_a": None if current_a is None else float(current_a),
            "power_w": None if power_w is None else float(power_w),
        }

        def configure():
            self._check_heat_read_permission(cancel_event)
            status, max_voltage, max_current, max_power, _max_temperature = (
                ESIBase.get_heat_ctrl_hw_limits(self)
            )
            self._check_heat_read_permission(cancel_event)
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
                if not (math.isfinite(value) and math.isfinite(maxima[name])
                        and 0 < value <= maxima[name]):
                    raise ValueError(
                        f"ESI heat {name} must be greater than 0 and no more "
                        f"than the hardware maximum {maxima[name]:g}."
                    )

            applied = {}
            for name, value in requested.items():
                if value is None:
                    continue
                self._check_heat_read_permission(cancel_event)
                result = setters[name](self, value)
                self._check_heat_read_permission(cancel_event)
                self._raise_on_status(result[0], f"set_heat_{name}")
                actual = float(result[1])
                if not (math.isfinite(actual) and 0 < actual <= maxima[name]):
                    raise ValueError(f"ESI heat {name} returned an invalid applied limit: {actual}.")
                applied[name] = actual
            return applied

        return self._call_locked_with_timeout(
            configure, timeout * 4.0, "configure_heat_limits"
        )

    def set_heater_temperature(
        self, temperature_c: float, timeout_s: Optional[float] = None, *, cancel_event=None
    ) -> float:
        """Set heater target temperature within the reported hardware limit."""
        self._require_connected()
        timeout = self._resolve_timeout(timeout_s)
        target = float(temperature_c)

        def set_temperature():
            if target > 0:
                self._check_heat_read_permission(cancel_event)
            status, _max_v, _max_i, _max_p, max_temperature = (
                ESIBase.get_heat_ctrl_hw_limits(self)
            )
            self._raise_on_status(status, "get_heat_ctrl_hw_limits")
            if not (math.isfinite(max_temperature) and max_temperature > 0
                    and math.isfinite(target) and 0 <= target <= max_temperature):
                raise ValueError(
                    "ESI heater target must be between 0 and the hardware "
                    f"maximum {float(max_temperature):g} degC."
                )
            if target > 0:
                # A live setpoint change can heat before any subsequent ON call.
                self._validate_heat_operating_state_unlocked(target, timeout=timeout, cancel_event=cancel_event)
                self._check_heat_read_permission(cancel_event)
            return self._set_heat_temperature_unlocked(target, max_temperature)

        return self._call_locked_with_timeout(
            set_temperature, timeout * 9.0, "set_heater_temperature"
        )

    def collect_diagnostics(self, timeout_s: Optional[float] = None, *, cancel_event=None) -> dict:
        """Collect controller, HV, and heater state without changing outputs."""
        self._require_connected()
        timeout = self._resolve_timeout(timeout_s)

        def snapshot():
            self._raise_if_transport_poisoned()

            def checked(result, action):
                self._raise_if_transport_poisoned()
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
                        # Ready before this read: a new conversion, not a repeat of the last one.
                        "voltage_fresh": bool(
                            int(module_data_flags[address]) & self.HV_ADC_V_READY
                        ),
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
                self._get_heat_monitoring_unlocked(timeout, cancel_event), "get_heat_ctrl_monitoring"
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
            heat_module_active, = checked(
                ESIBase.get_module_activation_state(self, self.HEAT_MODULE_ADDRESS),
                "get_heat_module_active(0)",
            )
            heat_state = int(module_states[self.HEAT_MODULE_ADDRESS])
            heat_control_active = bool(heat_state & self.MS_CTRL_ACT)
            heat_module_gate = bool(heat_state & self.MS_MOD_ACT)
            heat_device_gate = bool(heat_state & self.MS_DEV_ACT)
            heat_active = bool(enabled and heat_module_active and heat_module_gate
                               and heat_device_gate and heat_control_active)
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
                    "active": heat_active,
                    "module_active": bool(heat_module_active),
                    "module_state": heat_state,
                    "control_active": heat_control_active,
                    "module_gate_active": heat_module_gate,
                    "device_gate_active": heat_device_gate,
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
        self._raise_if_transport_poisoned()

        def checked(result, action):
            self._raise_if_transport_poisoned()
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

    def _verify_hv_discharge(self, timeout: float, on_discharge=None) -> None:
        """Require three fresh low-voltage rounds on all four HV outputs.

        Never enable anything to obtain a reading. The ADC mux is restored,
        but an unresponsive/poisoned DLL is never called again for cleanup.
        A ready bit must clear after draining old data and then rise: repeatedly
        reading a cached zero (even with Valid=True) cannot prove discharge.
        """
        last_step = ""

        def verify():
            nonlocal last_step
            deadline = time.monotonic() + self.DISCHARGE_TIMEOUT_S
            saved_ranges = {}
            readings = {address: {"positive_v": math.nan, "negative_v": math.nan,
                                  "measured_a": math.nan}
                        for address in self.HV_MODULE_ADDRESSES}
            consecutive = 0
            ready_mask = self.HV_ADC_V_READY | self.HV_ADC_I_READY
            primary_error = None

            def checked(function, *args):
                nonlocal last_step
                self._raise_if_transport_poisoned()
                if time.monotonic() >= deadline:
                    raise RuntimeError(
                        "ESI discharge unconfirmed: fresh ADC voltages on both "
                        f"polarities did not remain <= {self.DISCHARGE_LIMIT_V:g} V "
                        f"for {self.DISCHARGE_SAMPLES} consecutive rounds within "
                        f"{self.DISCHARGE_TIMEOUT_S:g} s. Last readings: {readings}"
                    )
                last_step = f"{function.__name__}(module {args[0]})" if args else function.__name__
                result = function(self, *args)
                self._raise_if_transport_poisoned()
                if time.monotonic() >= deadline:
                    raise RuntimeError("ESI discharge unconfirmed: ADC response arrived after the discharge deadline")
                status = result[0] if isinstance(result, tuple) else result
                self._raise_on_status(status, last_step)
                return result[1:] if isinstance(result, tuple) else ()

            def notify():
                if on_discharge is not None:
                    on_discharge({"modules": {a: dict(v) for a, v in readings.items()},
                                  "consecutive": consecutive,
                                  "limit_v": self.DISCHARGE_LIMIT_V})

            try:
                for address in self.HV_MODULE_ADDRESSES:
                    saved_ranges[address] = checked(ESIBase.get_hv_supply_meas_ranges, address)
                while consecutive < self.DISCHARGE_SAMPLES:
                    for negative in (False, True):
                        for address, (_old_negative, high_current) in saved_ranges.items():
                            checked(ESIBase.set_hv_supply_meas_ranges, address, negative, high_current)
                            observed = checked(ESIBase.get_hv_supply_meas_ranges, address)
                            if observed != (negative, high_current):
                                raise RuntimeError(f"ESI ADC polarity selection failed for module {address}")
                            # A conversion buffered before the mux change is not
                            # evidence for the newly selected polarity.
                            checked(ESIBase.get_hv_supply_output_voltage, address)
                            checked(ESIBase.get_hv_supply_output_current, address)
                        armed = {address: 0 for address in saved_ranges}
                        pending = set(saved_ranges)
                        while pending:
                            for address in sorted(pending):
                                flags, = checked(ESIBase.get_module_data_ready_flags, address)
                                armed[address] |= ~flags & ready_mask
                                if flags & armed[address] & ready_mask == ready_mask:
                                    observed = checked(ESIBase.get_hv_supply_meas_ranges, address)
                                    if observed != (negative, saved_ranges[address][1]):
                                        raise RuntimeError(f"ESI ADC polarity changed on module {address}")
                                    valid_v, voltage = checked(ESIBase.get_hv_supply_output_voltage, address)
                                    valid_i, current = checked(ESIBase.get_hv_supply_output_current, address)
                                    if not (valid_v and valid_i and math.isfinite(voltage) and math.isfinite(current)):
                                        raise RuntimeError(f"ESI invalid ADC voltage/current on module {address}; discharge unconfirmed")
                                    readings[address]["negative_v" if negative else "positive_v"] = float(voltage)
                                    readings[address]["measured_a"] = float(current)
                                    pending.remove(address)
                                    notify()
                                else:
                                    # Drain a bit which has not been observed
                                    # clear. A stuck-ready flag can never pass.
                                    if flags & self.HV_ADC_V_READY and not armed[address] & self.HV_ADC_V_READY:
                                        checked(ESIBase.get_hv_supply_output_voltage, address)
                                    if flags & self.HV_ADC_I_READY and not armed[address] & self.HV_ADC_I_READY:
                                        checked(ESIBase.get_hv_supply_output_current, address)
                                    cleared, = checked(ESIBase.get_module_data_ready_flags, address)
                                    armed[address] |= ~cleared & ready_mask
                            if pending:
                                time.sleep(min(.1, max(0., deadline - time.monotonic())))
                    below_limit = all(abs(data[key]) <= self.DISCHARGE_LIMIT_V
                                      for data in readings.values()
                                      for key in ("positive_v", "negative_v"))
                    consecutive = consecutive + 1 if below_limit else 0
                    notify()
                self.logger.info(f"HV discharge verified: {readings}")
            except Exception as exc:
                primary_error = exc
                raise
            finally:
                self._hv_measurement_requests.clear()
                if not self._transport_poisoned and not getattr(self, "_communication_error", None):
                    for address, selection in saved_ranges.items():
                        try:
                            self._raise_if_transport_poisoned()
                            last_step = f"restore ADC selection({address})"
                            status = ESIBase.set_hv_supply_meas_ranges(self, address, *selection)
                            self._raise_if_transport_poisoned()
                            self._raise_on_status(status, f"restore ADC selection({address})")
                        except Exception as cleanup_error:
                            if primary_error is None:
                                raise
                            raise RuntimeError(f"{primary_error}; ADC restoration also failed: {cleanup_error}") from primary_error

        # Keep the DLL lock throughout the mux/ready/read sequence: a background
        # diagnostic must not consume the samples used for shutdown confirmation.
        try:
            self._call_locked_with_timeout(
                verify, self.DISCHARGE_TIMEOUT_S + timeout, "verify_hv_discharge"
            )
        except RuntimeError as exc:
            if self._transport_poisoned and last_step:
                raise RuntimeError(f"{exc}; last discharge operation: {last_step}") from exc
            raise

    def disconnect(self, timeout_s: Optional[float] = None, *, on_discharge=None) -> bool:
        timeout = self._resolve_timeout(timeout_s)
        initial_open = self._disconnect_failed_open(timeout)
        if initial_open is not None:
            return initial_open
        # A timeout after opening says nothing about the physical outputs.
        self._raise_if_transport_poisoned()
        if not self.connected and not self._dll_port_claimed:
            return True
        if self.connected:
            # Keep the owner and output uncertainty if any verification fails.
            self.force_safe_off(timeout_s=timeout)
            self.set_global_active(False, timeout_s=timeout)
            self._verify_hv_discharge(timeout, on_discharge=on_discharge)
        # A claimed but not yet connected port has not passed identity checks;
        # no output commands were issued and none may be sent to this device.
        status = self._call_locked_with_timeout(
            ESIBase.close_port, timeout, "close_port", self
        )
        self._raise_on_status(status, "close_port")
        self.connected = False
        self._set_port_claimed(False)
        return True

    def force_close_transport(self, timeout_s: Optional[float] = None) -> bool:
        """Release communication without claiming that HV/heater shutdown succeeded."""
        # Inline owners cannot safely close beside a timed-out native call.
        # Only a process-isolated owner can be terminated to release its port.
        self._raise_if_transport_poisoned()
        if not self._dll_port_claimed and not self.connected:
            return True
        timeout = self._resolve_timeout(timeout_s)
        # Close is local port cleanup, not an instrument command; do not precede it
        # with Purge, which could also fail on the broken communication stream.
        status = super()._call_locked_with_timeout(ESIBase.close_port, timeout, "force_close_port", self)
        self._raise_on_status(status, "force_close_port")
        self.connected = False
        self._set_port_claimed(False)
        return True


class ESI(ProcessIsolatedClientMixin):
    """Public ESI facade with optional worker-process isolation on Windows."""

    _INSTRUMENT_NAME = "ESI"
    _PROCESS_CONTROLLER_CLASS = _ESIController
    _PROCESS_CONTROLLER_PATH = f"{__package__}.esi:_ESIController"
    _PROCESS_TIMEOUT_RULES = {
        "connect": (30.0, 10.0, 90.0),
        "collect_identity": (25.0, 10.0, 90.0),
        "collect_diagnostics": (20.0, 5.0, 10.0),
        "get_heat_configuration": (8.0, 10.0, 45.0),
        "configure_heat_limits": (8.0, 10.0, 45.0),
        "configure_hv_max_voltage_steps": (8.0, 10.0, 45.0),
        "list_configs": (20.0, 10.0, 90.0),
        "load_config": (15.0, 10.0, 60.0),
        "save_config": (15.0, 10.0, 60.0),
        "set_heater_temperature": (10.0, 10.0, 30.0),
        "set_output_active": (12.0, 10.0, 45.0),
        "force_safe_off": (8.0, 10.0, 45.0),
        "disconnect": (12.0, _ESIController.DISCHARGE_TIMEOUT_S + 5.0, 10.0),
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
        worker_launcher=None,
        native_backend: bool = False,
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
        if native_backend:
            backend_kwargs["native_backend"] = True
            self._initialize_process_backend(backend_kwargs=backend_kwargs, incompatible_objects={})
            return
        if process_backend:
            from ._process import ESIProcessProxy

            if logger is not None or thread_lock is not None:
                raise ValueError("An isolated ESI worker cannot share a logger or thread lock")
            backend_kwargs.pop("logger")
            backend_kwargs.pop("thread_lock")
            # Failure to start isolation must not silently load the DLL in Explorer.
            proxy_options = {"worker_launcher": worker_launcher} if worker_launcher is not None else {}
            object.__setattr__(self, "_backend", ESIProcessProxy(backend_kwargs, **proxy_options))
            object.__setattr__(self, "_backend_mode", "process")
            object.__setattr__(self, "_process_backend_disabled_reason", "")
            return
        self._initialize_process_backend(
            backend_kwargs=backend_kwargs,
            incompatible_objects={"logger": logger, "thread_lock": thread_lock},
            allow_process_backend=bool(process_backend),
            process_backend_disabled_reason=(
                "ESI process isolation disabled by configuration; inline DLL calls "
                "cannot recover a COM port after a vendor call blocks."
            ),
        )

    def __getattr__(self, name):
        if object.__getattribute__(self, "_backend_mode") == "process":
            backend = object.__getattribute__(self, "_backend")
            if hasattr(backend, "session"):
                return super().__getattr__(name)
            if name in {"_open_failed", "_opening_in_progress", "_failed_open_released"}:
                return False  # Terminating the worker retires even a failed/blocked Open.
            if name == "_transport_poisoned":
                return backend.closed
        return super().__getattr__(name)

    def force_close_transport(self, timeout_s: Optional[float] = None) -> bool:
        backend = object.__getattribute__(self, "_backend")
        if object.__getattribute__(self, "_backend_mode") == "process":
            return backend.close()
        return backend.force_close_transport(timeout_s=timeout_s)

    def wait_for_idle(self, timeout_s: float = 1.) -> bool:
        if object.__getattribute__(self, "_backend_mode") == "process":
            return object.__getattribute__(self, "_backend").wait_for_idle(timeout_s)
        return True
