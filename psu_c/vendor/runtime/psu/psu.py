"""High-level CGC PSU driver."""

from __future__ import annotations

import logging
import math
import threading
from pathlib import Path
from typing import Optional

from .._driver_common import (
    DllPortClaimRegistryMixin,
    ProcessIsolatedClientMixin,
    TimeoutSafeDllMixin,
    build_device_logger,
)
from .psu_base import PSUBase

class _PSUController(DllPortClaimRegistryMixin, TimeoutSafeDllMixin, PSUBase):
    """
    High-level CGC PSU driver.

    The public API is intentionally config-centric:
    load a known configuration first, then optionally adjust voltages and
    current limits at runtime.
    """

    _INSTRUMENT_NAME = "PSU"
    _active_connections_lock = threading.Lock()
    _active_connections: dict[int, dict[str, object]] = {}
    CHANNEL_LABELS = {
        PSUBase.PSU_POS: "positive",
        PSUBase.PSU_NEG: "negative",
    }
    _DEFAULT_IO_TIMEOUT_S = 5.0
    _COMPAT_OPTIONAL_STATUSES = frozenset(
        {
            PSUBase.ERR_COMMAND_RECEIVE,
            PSUBase.ERR_DATA_RECEIVE,
            PSUBase.ERR_COMMAND_WRONG,
            PSUBase.ERR_ARGUMENT_WRONG,
        }
    )
    _EXPECTED_PRODUCT_TOKENS = ("PSU", "PSU-CTRL")

    def __init__(
        self,
        device_id: str,
        com: int,
        port: int = 0,
        baudrate: int = 230400,
        logger: Optional[logging.Logger] = None,
        thread_lock: Optional[threading.Lock] = None,
        dll_path: Optional[str] = None,
        log_dir: Optional[Path] = None,
        **kwargs,
    ):
        if kwargs:
            unexpected = ", ".join(sorted(kwargs))
            raise TypeError(f"Unexpected PSU init kwargs: {unexpected}")

        self.device_id = device_id
        self.com = int(com)
        self.port_num = int(port)
        self.baudrate = int(baudrate)
        if self.baudrate <= 0:
            raise ValueError("baudrate must be > 0")
        self.connected = False
        self._dll_port_claimed = False
        self._transport_poisoned = False
        self._transport_error = None

        self.thread_lock = thread_lock or threading.Lock()

        self.logger = build_device_logger(
            instrument_name=self._INSTRUMENT_NAME,
            device_id=device_id,
            logger=logger,
            log_dir=log_dir,
            source_file=__file__,
        )

        super().__init__(com=com, port=port, log=None, idn=device_id, dll_path=dll_path)

    def _on_transport_poisoned(self) -> None:
        self._set_port_claimed(True)

    def _require_connected(self):
        if not self.connected:
            raise RuntimeError("PSU device is not connected.")

    def _raise_on_status(self, status: int, action: str):
        if status != self.NO_ERR:
            raise RuntimeError(f"PSU {action} failed: {self.format_status(status)}")

    def _resolve_io_timeout(self, timeout_s: Optional[float] = None) -> float:
        if timeout_s is None:
            return self._DEFAULT_IO_TIMEOUT_S
        timeout_s = float(timeout_s)
        if timeout_s <= 0:
            raise ValueError("PSU timeout_s must be greater than 0.")
        return timeout_s

    def _resolve_batch_timeout(
        self,
        timeout_s: Optional[float],
        *,
        multiplier: float,
        additive: float = 0.0,
        minimum: Optional[float] = None,
        maximum: Optional[float] = None,
    ) -> float:
        batch_timeout = (
            self._resolve_io_timeout(timeout_s) * float(multiplier)
        ) + float(additive)
        if minimum is not None:
            batch_timeout = max(batch_timeout, float(minimum))
        if maximum is not None:
            batch_timeout = min(batch_timeout, float(maximum))
        return batch_timeout

    def _coerce_finite_setpoint(self, value: float, quantity: str) -> float:
        try:
            numeric_value = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"PSU {quantity} must be a real number, got {value!r}."
            ) from exc
        if not math.isfinite(numeric_value):
            raise ValueError(
                f"PSU {quantity} must be finite, got {numeric_value!r}."
            )
        return numeric_value

    @staticmethod
    def _call_with_optional_timeout(method, *args, timeout_s: Optional[float] = None):
        if timeout_s is None:
            return method(*args)
        return method(*args, timeout_s=timeout_s)

    def _append_shutdown_error(
        self,
        errors: list[tuple[str, BaseException]],
        step: str,
        exc: BaseException,
    ) -> None:
        self.logger.error(f"PSU shutdown step failed during {step}: {exc}")
        errors.append((step, exc))

    def _raise_shutdown_errors(self, errors: list[tuple[str, BaseException]]) -> None:
        if not errors:
            return
        details = "; ".join(f"{step}: {exc}" for step, exc in errors)
        raise RuntimeError(
            f"PSU shutdown completed with {len(errors)} error(s): {details}"
        ) from errors[0][1]

    def _warn_if_unexpected_product_id(self, timeout_s: Optional[float] = None):
        try:
            if timeout_s is None:
                status, product_id = self._call_locked(PSUBase.get_product_id, self)
            else:
                status, product_id = self._call_locked_with_timeout(
                    PSUBase.get_product_id,
                    self._resolve_io_timeout(timeout_s),
                    "get_product_id",
                    self,
                )
        except Exception as exc:
            if self._transport_poisoned:
                raise
            self.logger.debug(f"Skipping PSU identity probe after connect: {exc}")
            return

        if status != self.NO_ERR or not product_id:
            return

        normalized = product_id.upper()
        if any(token in normalized for token in self._EXPECTED_PRODUCT_TOKENS):
            return

        self.logger.warning(
            "Connected device does not look like a PSU controller. "
            f"Reported product_id='{product_id}'. Check the COM port and use the "
            "matching driver for that instrument."
        )

    def connect(self, timeout_s: float = 5.0) -> bool:
        """Connect to the PSU device."""
        try:
            if self.connected:
                self._set_port_claimed(True)
                self.logger.info(
                    f"PSU device {self.device_id} is already connected; skipping open_port"
                )
                return True

            timeout_s = self._resolve_io_timeout(timeout_s)
            self._warn_on_other_process_ports()
            self.logger.info(
                f"Connecting to PSU device {self.device_id} "
                f"on COM{self.com}, port {self.port_num}"
            )

            open_port = super().open_port
            set_baud_rate = super().set_baud_rate
            close_port = super().close_port

            status = self._call_locked_with_timeout(
                open_port, timeout_s, "open_port", self.com, self.port_num
            )
            if status != self.NO_ERR:
                raise RuntimeError(
                    f"PSU open_port failed: {self.format_status(status)}"
                )

            self._set_port_claimed(True)
            baud_status, actual_baud = self._call_locked_with_timeout(
                set_baud_rate, timeout_s, "set_baud_rate", self.baudrate
            )
            if baud_status == self.NO_ERR:
                self.connected = True
                self._warn_if_unexpected_product_id(timeout_s=timeout_s)
                self.logger.info(
                    f"Successfully connected to PSU device {self.device_id} "
                    f"(baud rate: {actual_baud})"
                )
                return True

            self.logger.error(
                f"Failed to set baud rate: {self.format_status(baud_status)}"
            )
            close_status = self._call_locked_with_timeout(
                close_port, timeout_s, "close_port"
            )
            if close_status != self.NO_ERR:
                self.logger.warning(
                    "PSU port rollback after baud-rate failure also failed: "
                    f"{self.format_status(close_status)}"
                )
            else:
                self._set_port_claimed(False)
            raise RuntimeError(
                f"PSU set_baud_rate failed: {self.format_status(baud_status)}"
            )
        except Exception:
            self.connected = False
            raise

    def _cleanup_initialize_failure(self, timeout_s: float) -> None:
        """Best-effort cleanup after a failed PSU initialize() sequence."""
        if self._transport_poisoned:
            self.disconnect(timeout_s=timeout_s)
            return

        try:
            self.shutdown(timeout_s=timeout_s)
        except Exception as exc:  # noqa: BLE001
            self.logger.error(
                "PSU initialize cleanup failed after startup error: "
                f"{exc}"
            )

    def initialize(
        self,
        timeout_s: float = 5.0,
        *,
        standby_config: Optional[int] = None,
        operating_config: Optional[int] = None,
        require_standby_outputs_disabled: bool = True,
    ) -> dict:
        """
        Run the routine PSU startup sequence.

        The sequence connects to the controller, optionally loads a standby
        configuration, reads back the device and output enable state when a
        standby stage is requested, and optionally loads an operating
        configuration. When a standby configuration is requested, initialize()
        verifies that both HV outputs are disabled before it continues. If a
        saved standby configuration brings one of the outputs up, initialize()
        forces both outputs back OFF, verifies the readback, and only then
        proceeds or raises.
        """
        timeout_s = self._resolve_io_timeout(timeout_s)
        was_connected = self.connected
        self.logger.info(
            f"Initializing PSU device {self.device_id}"
            + (
                f" with standby config {standby_config}"
                if standby_config is not None
                else ""
            )
            + (
                f" and operating config {operating_config}"
                if operating_config is not None
                else ""
            )
        )

        try:
            self.connect(timeout_s=timeout_s)
            initialization_state = {}
            if standby_config is not None:
                self.load_config(standby_config, timeout_s=timeout_s)
                device_enabled = self.get_device_enabled(timeout_s=timeout_s)
                output_enabled = self.get_output_enabled(timeout_s=timeout_s)
                initialization_state = {
                    "standby_config": int(standby_config),
                    "device_enabled": device_enabled,
                    "output_enabled": output_enabled,
                }

                if require_standby_outputs_disabled and output_enabled != (False, False):
                    initialization_state["standby_output_enabled_before_recovery"] = output_enabled
                    self.logger.warning(
                        "PSU standby configuration %s left outputs enabled %s; "
                        "forcing both outputs OFF before continuing.",
                        standby_config,
                        output_enabled,
                    )
                    self.set_output_enabled(False, False, timeout_s=timeout_s)
                    output_enabled = self.get_output_enabled(timeout_s=timeout_s)
                    initialization_state["output_enabled"] = output_enabled
                    initialization_state["standby_outputs_recovered"] = (
                        output_enabled == (False, False)
                    )
                    if output_enabled != (False, False):
                        raise RuntimeError(
                            "PSU standby configuration left outputs enabled even after "
                            f"forcing them OFF: {output_enabled}. Refusing to continue "
                            "initialization."
                        )

            if operating_config is not None:
                self.load_config(operating_config, timeout_s=timeout_s)
                initialization_state["operating_config"] = int(operating_config)

            return initialization_state
        except Exception:
            if was_connected or self.connected or self._transport_poisoned:
                self._cleanup_initialize_failure(timeout_s)
            raise

    def disconnect(self, timeout_s: Optional[float] = None) -> bool:
        """Disconnect from the PSU device."""
        was_connected = self.connected
        timeout_s = self._resolve_io_timeout(timeout_s)

        try:
            if self._transport_poisoned:
                self._set_port_claimed(True)
                self.logger.warning(
                    f"Skipping PSU close_port for {self.device_id} because the "
                    "transport is marked unusable."
                )
                return False

            if not was_connected:
                if not self._dll_port_claimed:
                    self._set_port_claimed(False)
                return True

            self.logger.info(f"Disconnecting PSU device {self.device_id}")
            status = self._call_locked_with_timeout(
                super().close_port,
                timeout_s,
                "close_port",
            )
            if status == self.NO_ERR:
                self.connected = False
                self._set_port_claimed(False)
                self.logger.info(
                    f"Successfully disconnected PSU device {self.device_id}"
                )
                return True

            self._set_port_claimed(True)
            self.logger.error(
                f"Failed to disconnect PSU device {self.device_id}: "
                f"{self.format_status(status)}"
            )
            return False
        except Exception as exc:
            self._set_port_claimed(True)
            self.logger.error(f"Disconnection error: {exc}")
            return False

    def get_status(self) -> dict:
        """Return the current driver status."""
        return {
            "device_id": self.device_id,
            "com": self.com,
            "port": self.port_num,
            "baudrate": self.baudrate,
            "connected": self.connected,
            "transport_poisoned": self._transport_poisoned,
        }

    def _is_optional_command_failure(self, status: int) -> bool:
        return status in self._COMPAT_OPTIONAL_STATUSES

    def _read_optional_metadata(self, method, action: str):
        status, value = method(self)
        if status == self.NO_ERR:
            return value
        if self._is_optional_command_failure(status):
            self.logger.warning(
                f"PSU {action} is unavailable on this controller: "
                f"{self.format_status(status)}"
            )
            return None
        self._raise_on_status(status, action)

    def _normalize_fixed_length_values(
        self,
        values,
        *,
        expected_len: int,
        fill_value,
        label: str,
    ) -> list:
        try:
            normalized = list(values or [])
        except TypeError:
            normalized = [] if values is None else [values]

        if len(normalized) < expected_len:
            self.logger.warning(
                f"PSU {label} returned {len(normalized)} item(s); expected "
                f"{expected_len}. Padding missing values."
            )
            normalized.extend([fill_value] * (expected_len - len(normalized)))
        elif len(normalized) > expected_len:
            self.logger.warning(
                f"PSU {label} returned {len(normalized)} item(s); expected "
                f"{expected_len}. Ignoring extra values."
            )
            normalized = normalized[:expected_len]
        return normalized

    def _get_output_enabled_unlocked(self) -> tuple[bool, bool]:
        status, psu0, psu1 = PSUBase.get_psu_enable(self)
        if status == self.NO_ERR:
            return psu0, psu1
        if self._is_optional_command_failure(status):
            state_status, state = PSUBase.get_psu_state(self)
            self._raise_on_status(state_status, "get_psu_state")
            self.logger.warning(
                "PSU get_psu_enable is unavailable on this controller; "
                "falling back to get_psu_state control bits."
            )
            return (
                bool(state & self.PSU_STATE_PSU0_ENB_CTRL),
                bool(state & self.PSU_STATE_PSU1_ENB_CTRL),
            )
        self._raise_on_status(status, "get_psu_enable")
        return False, False

    def _get_config_flags_list_unlocked(self) -> tuple[list[bool], list[bool]]:
        status, active_list, valid_list = PSUBase.get_config_list(self)
        if status == self.NO_ERR:
            return active_list, valid_list
        if self._is_optional_command_failure(status):
            self.logger.warning(
                "PSU get_config_list is unavailable on this controller; "
                "falling back to per-config get_config_flags."
            )
            active_list = []
            valid_list = []
            for index in range(self.MAX_CONFIG):
                flag_status, active, valid = PSUBase.get_config_flags(self, index)
                self._raise_on_status(flag_status, f"get_config_flags({index})")
                active_list.append(active)
                valid_list.append(valid)
            return active_list, valid_list
        self._raise_on_status(status, "get_config_list")
        return [], []

    def _list_configs_unlocked(self, include_empty: bool = False) -> list[dict]:
        """Collect config metadata while the caller holds self.thread_lock."""
        assert self.thread_lock.locked(), "caller must hold self.thread_lock"
        active_list, valid_list = self._get_config_flags_list_unlocked()

        configs = []
        for index, (active, valid) in enumerate(zip(active_list, valid_list)):
            if not include_empty and not (active or valid):
                continue
            name_status, name = PSUBase.get_config_name(self, index)
            self._raise_on_status(name_status, f"get_config_name({index})")
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
        self,
        include_empty: bool = False,
        timeout_s: Optional[float] = None,
    ) -> list[dict]:
        """Return PSU configurations with flags and names."""
        self._require_connected()
        return self._call_locked_with_timeout(
            self._list_configs_unlocked,
            self._resolve_batch_timeout(
                timeout_s,
                multiplier=2.0,
                additive=5.0,
                minimum=10.0,
                maximum=30.0,
            ),
            "list_configs",
            include_empty,
        )

    def load_config(self, config_number: int, timeout_s: Optional[float] = None) -> None:
        """Load and apply one PSU configuration stored in controller NVM."""
        self._require_connected()
        self.logger.info(f"Loading PSU config {config_number}")
        timeout_s = self._resolve_io_timeout(timeout_s)
        status = self._call_locked_with_timeout(
            PSUBase.load_current_config,
            timeout_s,
            "load_current_config",
            self,
            config_number,
        )
        self._raise_on_status(status, f"load_current_config({config_number})")

    def save_config(
        self,
        config_number: int,
        *,
        name: str | None = None,
        active: bool | None = None,
        valid: bool | None = None,
        timeout_s: Optional[float] = None,
    ) -> None:
        """Save the current PSU state into one configuration slot."""
        self._require_connected()
        self.logger.info(f"Saving PSU config {config_number}")
        timeout_s = self._resolve_io_timeout(timeout_s)
        status = self._call_locked_with_timeout(
            PSUBase.save_current_config,
            timeout_s,
            "save_current_config",
            self,
            config_number,
        )
        self._raise_on_status(status, f"save_current_config({config_number})")
        if name is not None:
            self.set_config_name(config_number, name, timeout_s=timeout_s)
        if active is not None or valid is not None:
            active_value = True if active is None else bool(active)
            valid_value = True if valid is None else bool(valid)
            self.set_config_flags(
                config_number,
                active=active_value,
                valid=valid_value,
                timeout_s=timeout_s,
            )

    def set_config_name(
        self,
        config_number: int,
        name: str,
        timeout_s: Optional[float] = None,
    ) -> None:
        """Set the human-readable name of one PSU configuration slot."""
        self._require_connected()
        timeout_s = self._resolve_io_timeout(timeout_s)
        status = self._call_locked_with_timeout(
            PSUBase.set_config_name,
            timeout_s,
            "set_config_name",
            self,
            config_number,
            name,
        )
        self._raise_on_status(status, f"set_config_name({config_number}, {name!r})")

    def set_config_flags(
        self,
        config_number: int,
        *,
        active: bool,
        valid: bool,
        timeout_s: Optional[float] = None,
    ) -> None:
        """Set the active/valid flags of one PSU configuration slot."""
        self._require_connected()
        timeout_s = self._resolve_io_timeout(timeout_s)
        status = self._call_locked_with_timeout(
            PSUBase.set_config_flags,
            timeout_s,
            "set_config_flags",
            self,
            config_number,
            active,
            valid,
        )
        self._raise_on_status(
            status,
            f"set_config_flags({config_number}, active={active}, valid={valid})",
        )

    def set_device_enabled(
        self,
        enable: bool,
        timeout_s: Optional[float] = None,
    ) -> None:
        """Set the PSU device enable flag."""
        self._require_connected()
        timeout_s = self._resolve_io_timeout(timeout_s)
        status = self._call_locked_with_timeout(
            PSUBase.set_device_enable,
            timeout_s,
            "set_device_enable",
            self,
            enable,
        )
        self._raise_on_status(status, f"set_device_enable({enable})")

    def get_device_enabled(self, timeout_s: Optional[float] = None) -> bool:
        """Return the PSU device enable flag."""
        self._require_connected()
        timeout_s = self._resolve_io_timeout(timeout_s)
        status, enabled = self._call_locked_with_timeout(
            PSUBase.get_device_enable,
            timeout_s,
            "get_device_enable",
            self,
        )
        self._raise_on_status(status, "get_device_enable")
        return enabled

    def set_output_enabled(
        self,
        psu0: bool,
        psu1: bool,
        timeout_s: Optional[float] = None,
    ) -> None:
        """Set the two PSU channel enable flags."""
        self._require_connected()
        timeout_s = self._resolve_io_timeout(timeout_s)
        status = self._call_locked_with_timeout(
            PSUBase.set_psu_enable,
            timeout_s,
            "set_psu_enable",
            self,
            psu0,
            psu1,
        )
        self._raise_on_status(status, f"set_psu_enable({psu0}, {psu1})")

    def get_output_enabled(self, timeout_s: Optional[float] = None) -> tuple[bool, bool]:
        """Return the two PSU channel enable flags."""
        self._require_connected()
        timeout_s = self._resolve_io_timeout(timeout_s)
        return self._call_locked_with_timeout(
            self._get_output_enabled_unlocked,
            timeout_s,
            "get_output_enabled",
        )

    def _get_output_full_range_unlocked(self) -> tuple[bool, bool]:
        status, psu0, psu1 = PSUBase.get_psu_full_range(self)
        if status == self.NO_ERR:
            return psu0, psu1
        if self._is_optional_command_failure(status):
            state_status, state = PSUBase.get_psu_state(self)
            self._raise_on_status(state_status, "get_psu_state")
            self.logger.warning(
                "PSU get_psu_full_range is unavailable on this controller; "
                "falling back to get_psu_state range bits."
            )
            return (
                bool(state & self.PSU_STATE_PSU0_FULL_ACT),
                bool(state & self.PSU_STATE_PSU1_FULL_ACT),
            )
        self._raise_on_status(status, "get_psu_full_range")
        return False, False

    def set_output_full_range(
        self,
        psu0: bool,
        psu1: bool,
        timeout_s: Optional[float] = None,
    ) -> None:
        """Set the full-range state for the two PSU channels."""
        self._require_connected()
        timeout_s = self._resolve_io_timeout(timeout_s)
        status = self._call_locked_with_timeout(
            PSUBase.set_psu_full_range,
            timeout_s,
            "set_psu_full_range",
            self,
            psu0,
            psu1,
        )
        self._raise_on_status(status, f"set_psu_full_range({psu0}, {psu1})")

    def get_output_full_range(
        self,
        timeout_s: Optional[float] = None,
    ) -> tuple[bool, bool]:
        """Return the full-range state for the two PSU channels."""
        self._require_connected()
        timeout_s = self._resolve_io_timeout(timeout_s)
        return self._call_locked_with_timeout(
            self._get_output_full_range_unlocked,
            timeout_s,
            "get_output_full_range",
        )

    def set_interlock_enabled(
        self,
        connector_output: bool,
        connector_bnc: bool,
        timeout_s: Optional[float] = None,
    ) -> None:
        """Set interlock enable flags for the output and BNC connectors."""
        self._require_connected()
        timeout_s = self._resolve_io_timeout(timeout_s)
        status = self._call_locked_with_timeout(
            PSUBase.set_interlock_enable,
            timeout_s,
            "set_interlock_enable",
            self,
            connector_output,
            connector_bnc,
        )
        self._raise_on_status(
            status,
            f"set_interlock_enable({connector_output}, {connector_bnc})",
        )

    def get_interlock_enabled(
        self,
        timeout_s: Optional[float] = None,
    ) -> tuple[bool, bool]:
        """Return interlock enable flags for the output and BNC connectors."""
        self._require_connected()
        timeout_s = self._resolve_io_timeout(timeout_s)
        status, connector_output, connector_bnc = self._call_locked_with_timeout(
            PSUBase.get_interlock_enable,
            timeout_s,
            "get_interlock_enable",
            self,
        )
        self._raise_on_status(status, "get_interlock_enable")
        return connector_output, connector_bnc

    def set_channel_voltage(
        self,
        channel: int,
        voltage_v: float,
        timeout_s: Optional[float] = None,
    ) -> None:
        """Set one PSU channel output voltage in volts."""
        self._require_connected()
        voltage_v = self._coerce_finite_setpoint(voltage_v, "voltage")
        timeout_s = self._resolve_io_timeout(timeout_s)
        status = self._call_locked_with_timeout(
            PSUBase.set_psu_output_voltage,
            timeout_s,
            f"set_psu_output_voltage[{channel}]",
            self,
            channel,
            voltage_v,
        )
        self._raise_on_status(status, f"set_psu_output_voltage({channel}, {voltage_v})")

    def get_channel_voltage(
        self,
        channel: int,
        timeout_s: Optional[float] = None,
    ) -> float:
        """Return one PSU channel output voltage in volts."""
        self._require_connected()
        timeout_s = self._resolve_io_timeout(timeout_s)
        status, voltage = self._call_locked_with_timeout(
            PSUBase.get_psu_output_voltage,
            timeout_s,
            f"get_psu_output_voltage[{channel}]",
            self,
            channel,
        )
        self._raise_on_status(status, f"get_psu_output_voltage({channel})")
        return voltage

    def get_channel_voltage_limits(
        self,
        channel: int,
        timeout_s: Optional[float] = None,
    ) -> tuple[float, float]:
        """Return the requested and limit voltages for one channel."""
        self._require_connected()
        timeout_s = self._resolve_io_timeout(timeout_s)
        status, setpoint, limit = self._call_locked_with_timeout(
            PSUBase.get_psu_set_output_voltage,
            timeout_s,
            f"get_psu_set_output_voltage[{channel}]",
            self,
            channel,
        )
        self._raise_on_status(status, f"get_psu_set_output_voltage({channel})")
        return setpoint, limit

    def set_channel_current(
        self,
        channel: int,
        current_a: float,
        timeout_s: Optional[float] = None,
    ) -> None:
        """Set one PSU channel output current in amperes."""
        self._require_connected()
        current_a = self._coerce_finite_setpoint(current_a, "current")
        timeout_s = self._resolve_io_timeout(timeout_s)
        status = self._call_locked_with_timeout(
            PSUBase.set_psu_output_current,
            timeout_s,
            f"set_psu_output_current[{channel}]",
            self,
            channel,
            current_a,
        )
        self._raise_on_status(status, f"set_psu_output_current({channel}, {current_a})")

    def get_channel_current(
        self,
        channel: int,
        timeout_s: Optional[float] = None,
    ) -> float:
        """Return one PSU channel output current in amperes."""
        self._require_connected()
        timeout_s = self._resolve_io_timeout(timeout_s)
        status, current = self._call_locked_with_timeout(
            PSUBase.get_psu_output_current,
            timeout_s,
            f"get_psu_output_current[{channel}]",
            self,
            channel,
        )
        self._raise_on_status(status, f"get_psu_output_current({channel})")
        return current

    def get_channel_current_limits(
        self,
        channel: int,
        timeout_s: Optional[float] = None,
    ) -> tuple[float, float]:
        """Return the requested and limit currents for one channel."""
        self._require_connected()
        timeout_s = self._resolve_io_timeout(timeout_s)
        status, setpoint, limit = self._call_locked_with_timeout(
            PSUBase.get_psu_set_output_current,
            timeout_s,
            f"get_psu_set_output_current[{channel}]",
            self,
            channel,
        )
        self._raise_on_status(status, f"get_psu_set_output_current({channel})")
        return setpoint, limit

    def _get_product_info_unlocked(self) -> dict:
        product_no_status, product_no = PSUBase.get_product_no(self)
        self._raise_on_status(product_no_status, "get_product_no")
        product_id = self._read_optional_metadata(PSUBase.get_product_id, "get_product_id")
        fw_version = self._read_optional_metadata(PSUBase.get_fw_version, "get_fw_version")
        fw_date = self._read_optional_metadata(PSUBase.get_fw_date, "get_fw_date")
        hw_type = self._read_optional_metadata(PSUBase.get_hw_type, "get_hw_type")
        hw_version = self._read_optional_metadata(PSUBase.get_hw_version, "get_hw_version")
        return {
            "product_no": product_no,
            "product_id": product_id,
            "firmware": {
                "version": fw_version,
                "date": fw_date,
            },
            "hardware": {
                "type": hw_type,
                "version": hw_version,
            },
        }

    def get_product_info(self, timeout_s: Optional[float] = None) -> dict:
        """Return stable product, firmware and hardware metadata."""
        self._require_connected()
        return self._call_locked_with_timeout(
            self._get_product_info_unlocked,
            self._resolve_batch_timeout(
                timeout_s,
                multiplier=2.0,
                additive=5.0,
                minimum=10.0,
                maximum=20.0,
            ),
            "get_product_info",
        )

    def _collect_housekeeping_unlocked(self) -> dict:
        main_status, main_state_hex, main_state_name = PSUBase.get_main_state(self)
        self._raise_on_status(main_status, "get_main_state")
        device_state_status, device_state_hex, device_state_flags = PSUBase.get_device_state(self)
        self._raise_on_status(device_state_status, "get_device_state")
        psu_state_status, psu_state = PSUBase.get_psu_state(self)
        self._raise_on_status(psu_state_status, "get_psu_state")
        device_enabled_status, device_enabled = PSUBase.get_device_enable(self)
        self._raise_on_status(device_enabled_status, "get_device_enable")
        output_enabled_raw = self._get_output_enabled_unlocked()
        full_range_supported_status, full_range0_supported, full_range1_supported = (
            PSUBase.has_psu_full_range(self)
        )
        if full_range_supported_status == self.NO_ERR:
            full_range_supported = (full_range0_supported, full_range1_supported)
        elif self._is_optional_command_failure(full_range_supported_status):
            full_range_supported = (False, False)
        else:
            self._raise_on_status(full_range_supported_status, "has_psu_full_range")
            full_range_supported = (False, False)
        full_range_raw = self._get_output_full_range_unlocked()
        housekeeping_status, volt_rect, volt_5v0, volt_3v3, temp_cpu = PSUBase.get_housekeeping(self)
        self._raise_on_status(housekeeping_status, "get_housekeeping")
        sensor_status, temperatures = PSUBase.get_sensor_data(self)
        self._raise_on_status(sensor_status, "get_sensor_data")
        fan_status, fan_enabled, fan_failed, fan_set_rpm, fan_measured_rpm, fan_pwm = PSUBase.get_fan_data(self)
        self._raise_on_status(fan_status, "get_fan_data")
        led_status, led_red, led_green, led_blue = PSUBase.get_led_data(self)
        self._raise_on_status(led_status, "get_led_data")
        cpu_status, cpu_load, cpu_frequency = PSUBase.get_cpu_data(self)
        self._raise_on_status(cpu_status, "get_cpu_data")
        uptime_status, uptime_s, uptime_ms, operation_s = PSUBase.get_uptime(self)
        self._raise_on_status(uptime_status, "get_uptime")
        total_time_status, total_uptime_s, total_operation_s = PSUBase.get_total_time(self)
        self._raise_on_status(total_time_status, "get_total_time")

        output_enabled = tuple(
            bool(value)
            for value in self._normalize_fixed_length_values(
                output_enabled_raw,
                expected_len=self.PSU_NUM,
                fill_value=False,
                label="output enabled state",
            )
        )
        full_range = tuple(
            bool(value)
            for value in self._normalize_fixed_length_values(
                full_range_raw,
                expected_len=self.PSU_NUM,
                fill_value=False,
                label="full range state",
            )
        )
        full_range_supported = tuple(
            bool(value)
            for value in self._normalize_fixed_length_values(
                full_range_supported,
                expected_len=self.PSU_NUM,
                fill_value=False,
                label="full range support state",
            )
        )
        temperatures = self._normalize_fixed_length_values(
            temperatures,
            expected_len=self.SENSOR_NUM,
            fill_value=math.nan,
            label="sensor data",
        )
        fan_enabled = [
            bool(value)
            for value in self._normalize_fixed_length_values(
                fan_enabled,
                expected_len=self.FAN_NUM,
                fill_value=False,
                label="fan enabled state",
            )
        ]
        fan_failed = [
            bool(value)
            for value in self._normalize_fixed_length_values(
                fan_failed,
                expected_len=self.FAN_NUM,
                fill_value=False,
                label="fan failure state",
            )
        ]
        fan_set_rpm = [
            int(value)
            for value in self._normalize_fixed_length_values(
                fan_set_rpm,
                expected_len=self.FAN_NUM,
                fill_value=0,
                label="fan setpoints",
            )
        ]
        fan_measured_rpm = [
            int(value)
            for value in self._normalize_fixed_length_values(
                fan_measured_rpm,
                expected_len=self.FAN_NUM,
                fill_value=0,
                label="fan readbacks",
            )
        ]
        fan_pwm = [
            int(value)
            for value in self._normalize_fixed_length_values(
                fan_pwm,
                expected_len=self.FAN_NUM,
                fill_value=0,
                label="fan PWM values",
            )
        ]
        channels = []
        for channel in range(self.PSU_NUM):
            measured_status, measured_voltage, measured_current, dropout_voltage = PSUBase.get_psu_data(self, channel)
            self._raise_on_status(measured_status, f"get_psu_data({channel})")
            voltage_status, set_voltage, limit_voltage = PSUBase.get_psu_set_output_voltage(self, channel)
            self._raise_on_status(voltage_status, f"get_psu_set_output_voltage({channel})")
            current_status, set_current, limit_current = PSUBase.get_psu_set_output_current(self, channel)
            self._raise_on_status(current_status, f"get_psu_set_output_current({channel})")
            adc_status, volt_avdd, volt_dvdd, volt_aldo, volt_dldo, volt_ref, temp_adc = PSUBase.get_adc_housekeeping(self, channel)
            self._raise_on_status(adc_status, f"get_adc_housekeeping({channel})")
            rail_status, volt_24vp, volt_12vp, volt_12vn, rail_ref = PSUBase.get_psu_housekeeping(self, channel)
            self._raise_on_status(rail_status, f"get_psu_housekeeping({channel})")
            channels.append(
                {
                    "channel": channel,
                    "label": self.CHANNEL_LABELS.get(channel, str(channel)),
                    "enabled": output_enabled[channel],
                    "full_range": {
                        "supported": full_range_supported[channel],
                        "enabled": full_range[channel],
                    },
                    "voltage": {
                        "measured_v": measured_voltage,
                        "set_v": set_voltage,
                        "limit_v": limit_voltage,
                    },
                    "current": {
                        "measured_a": measured_current,
                        "set_a": set_current,
                        "limit_a": limit_current,
                    },
                    "dropout_v": dropout_voltage,
                    "adc": {
                        "volt_avdd_v": volt_avdd,
                        "volt_dvdd_v": volt_dvdd,
                        "volt_aldo_v": volt_aldo,
                        "volt_dldo_v": volt_dldo,
                        "volt_ref_v": volt_ref,
                        "temp_adc_c": temp_adc,
                    },
                    "rails": {
                        "volt_24vp_v": volt_24vp,
                        "volt_12vp_v": volt_12vp,
                        "volt_12vn_v": volt_12vn,
                        "volt_ref_v": rail_ref,
                    },
                }
            )

        return {
            "device_enabled": device_enabled,
            "output_enabled": output_enabled,
            "main_state": {
                "hex": main_state_hex,
                "name": main_state_name,
            },
            "device_state": {
                "hex": device_state_hex,
                "flags": device_state_flags,
            },
            "psu_state": {
                "hex": hex(psu_state),
                "current_limit_active": bool(psu_state & self.PSU_STATE_ILIM_ACT),
            },
            "housekeeping": {
                "volt_rect_v": volt_rect,
                "volt_5v0_v": volt_5v0,
                "volt_3v3_v": volt_3v3,
                "temp_cpu_c": temp_cpu,
            },
            "sensors_c": temperatures,
            "fans": [
                {
                    "fan": index,
                    "enabled": fan_enabled[index],
                    "failed": fan_failed[index],
                    "set_rpm": fan_set_rpm[index],
                    "measured_rpm": fan_measured_rpm[index],
                    "pwm": fan_pwm[index],
                }
                for index in range(self.FAN_NUM)
            ],
            "led": {
                "red": led_red,
                "green": led_green,
                "blue": led_blue,
            },
            "cpu": {
                "load": cpu_load,
                "frequency_hz": cpu_frequency,
            },
            "uptime": {
                "seconds": uptime_s,
                "milliseconds": uptime_ms,
                "operation_seconds": operation_s,
                "total_uptime_seconds": total_uptime_s,
                "total_operation_seconds": total_operation_s,
            },
            "channels": channels,
        }

    def collect_housekeeping(self, timeout_s: Optional[float] = None) -> dict:
        """Return a structured runtime snapshot suitable for monitoring."""
        self._require_connected()
        return self._call_locked_with_timeout(
            self._collect_housekeeping_unlocked,
            self._resolve_batch_timeout(
                timeout_s,
                multiplier=3.0,
                additive=10.0,
                minimum=20.0,
                maximum=60.0,
            ),
            "collect_housekeeping",
        )

    def _zero_output_setpoints(
        self,
        timeout_s: Optional[float] = None,
    ) -> list[tuple[str, BaseException]]:
        """Drive both channel current and voltage setpoints to zero."""
        errors: list[tuple[str, BaseException]] = []
        for channel in range(self.PSU_NUM):
            try:
                self._call_with_optional_timeout(
                    self.set_channel_current,
                    channel,
                    0.0,
                    timeout_s=timeout_s,
                )
            except Exception as exc:  # noqa: BLE001
                self._append_shutdown_error(
                    errors, f"set_channel_current({channel}, 0.0)", exc
                )
        for channel in range(self.PSU_NUM):
            try:
                self._call_with_optional_timeout(
                    self.set_channel_voltage,
                    channel,
                    0.0,
                    timeout_s=timeout_s,
                )
            except Exception as exc:  # noqa: BLE001
                self._append_shutdown_error(
                    errors, f"set_channel_voltage({channel}, 0.0)", exc
                )
        return errors

    def shutdown(
        self,
        *,
        standby_config: int | None = None,
        disable_outputs: bool = True,
        disable_device: bool = True,
        timeout_s: Optional[float] = None,
    ) -> bool:
        """Disable the PSU or load an explicit standby config, then disconnect."""
        errors: list[tuple[str, BaseException]] = []
        if standby_config is not None and (disable_outputs or disable_device):
            raise ValueError(
                "standby_config cannot be combined with disable_outputs or "
                "disable_device. Either load an explicit standby config or "
                "request an explicit shutdown sequence."
            )

        if self.connected and standby_config is not None:
            try:
                self._call_with_optional_timeout(
                    self.load_config,
                    standby_config,
                    timeout_s=timeout_s,
                )
            except Exception as exc:  # noqa: BLE001
                self._append_shutdown_error(
                    errors, f"load_config({standby_config})", exc
                )
        if self.connected and (disable_outputs or disable_device):
            errors.extend(self._zero_output_setpoints(timeout_s=timeout_s))
        if self.connected and disable_outputs:
            try:
                self._call_with_optional_timeout(
                    self.set_output_enabled,
                    False,
                    False,
                    timeout_s=timeout_s,
                )
            except Exception as exc:  # noqa: BLE001
                self._append_shutdown_error(
                    errors, "set_output_enabled(False, False)", exc
                )
        if self.connected and disable_device:
            try:
                self._call_with_optional_timeout(
                    self.set_device_enabled,
                    False,
                    timeout_s=timeout_s,
                )
            except Exception as exc:  # noqa: BLE001
                self._append_shutdown_error(errors, "set_device_enabled(False)", exc)

        disconnected = self._call_with_optional_timeout(
            self.disconnect,
            timeout_s=timeout_s,
        )
        if not disconnected:
            self._append_shutdown_error(
                errors,
                "disconnect()",
                RuntimeError("PSU disconnect failed during shutdown."),
            )

        self._raise_shutdown_errors(errors)
        return disconnected


class PSU(ProcessIsolatedClientMixin):
    """Public PSU client with process isolation on Windows."""

    _INSTRUMENT_NAME = "PSU"
    _PROCESS_CONTROLLER_CLASS = _PSUController
    _PROCESS_CONTROLLER_PATH = f"{__name__}:_PSUController"
    _PROCESS_TIMEOUT_RULES = {
        "connect": (4.0, 5.0, 15.0),
        "initialize": (8.0, 5.0, 30.0),
    }
    _active_connections_lock = _PSUController._active_connections_lock
    _active_connections = _PSUController._active_connections

    def __init__(
        self,
        device_id: str,
        com: int,
        port: int = 0,
        baudrate: int = 230400,
        logger: Optional[logging.Logger] = None,
        thread_lock: Optional[threading.Lock] = None,
        dll_path: Optional[str] = None,
        log_dir: Optional[Path] = None,
        allow_process_backend: bool = True,
        process_backend_disabled_reason: str = "",
        **kwargs,
    ):
        backend_kwargs = {
            "device_id": device_id,
            "com": com,
            "port": port,
            "baudrate": baudrate,
            "logger": logger,
            "thread_lock": thread_lock,
            "dll_path": dll_path,
            "log_dir": log_dir,
            **kwargs,
        }
        self._initialize_process_backend(
            backend_kwargs=backend_kwargs,
            incompatible_objects={
                "logger": logger,
                "thread_lock": thread_lock,
            },
            allow_process_backend=allow_process_backend,
            process_backend_disabled_reason=process_backend_disabled_reason,
        )
