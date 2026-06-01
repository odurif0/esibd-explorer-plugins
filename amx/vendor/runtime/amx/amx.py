"""High-level CGC AMX driver."""

from __future__ import annotations

import contextlib
import logging
import threading
from pathlib import Path
from typing import Optional

from .._driver_common import (
    DllPortClaimRegistryMixin,
    ProcessIsolatedClientMixin,
    TimeoutSafeDllMixin,
    build_device_logger,
)
from .amx_base import AMXBase

class _AMXController(DllPortClaimRegistryMixin, TimeoutSafeDllMixin, AMXBase):
    """
    High-level CGC AMX driver.

    The preferred workflow is:
    1. initialize with a known standby and/or operating configuration
    2. adjust frequency, duty cycle or delays only
    3. shutdown when the sequence is complete
    """

    _INSTRUMENT_NAME = "AMX"
    _active_connections_lock = threading.Lock()
    _active_connections: dict[int, dict[str, object]] = {}
    PULSER_LABELS = {
        0: "pulser_0",
        1: "pulser_1",
        2: "pulser_2",
        3: "pulser_3",
    }
    _DEFAULT_IO_TIMEOUT_S = 5.0
    _EXPECTED_PRODUCT_TOKENS = ("AMX", "AMX-CTRL")

    def __init__(
        self,
        device_id: str,
        com: int,
        port: int = 0,
        baudrate: int = 230400,
        process_backend: bool | None = None,
        logger: Optional[logging.Logger] = None,
        thread_lock: Optional[threading.Lock] = None,
        dll_path: Optional[str] = None,
        log_dir: Optional[Path] = None,
        **kwargs,
    ):
        if kwargs:
            unexpected = ", ".join(sorted(kwargs))
            raise TypeError(f"Unexpected AMX init kwargs: {unexpected}")

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
        self.loaded_config_number: Optional[int] = None
        self.loaded_config_name: str = ""
        self.loaded_config_source: str = ""

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
            raise RuntimeError("AMX device is not connected.")

    def _raise_on_status(self, status: int, action: str):
        if status != self.NO_ERR:
            raise RuntimeError(f"AMX {action} failed: {self.format_status(status)}")

    def _resolve_io_timeout(self, timeout_s: Optional[float] = None) -> float:
        if timeout_s is None:
            return self._DEFAULT_IO_TIMEOUT_S
        timeout_s = float(timeout_s)
        if timeout_s <= 0:
            raise ValueError("AMX timeout_s must be greater than 0.")
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

    @staticmethod
    def _call_with_optional_timeout(method, *args, timeout_s: Optional[float] = None):
        if timeout_s is None:
            return method(*args)
        return method(*args, timeout_s=timeout_s)

    @staticmethod
    def _append_shutdown_error(
        errors: list[tuple[str, BaseException]],
        step: str,
        exc: BaseException,
    ) -> None:
        errors.append((step, exc))

    def _raise_shutdown_errors(self, errors: list[tuple[str, BaseException]]) -> None:
        if not errors:
            return
        details = "; ".join(f"{step}: {exc}" for step, exc in errors)
        raise RuntimeError(
            f"AMX shutdown completed with {len(errors)} error(s): {details}"
        ) from errors[0][1]

    def _warn_if_unexpected_product_id(self, timeout_s: Optional[float] = None):
        try:
            if timeout_s is None:
                status, product_id = self._call_locked(AMXBase.get_product_id, self)
            else:
                status, product_id = self._call_locked_with_timeout(
                    AMXBase.get_product_id,
                    self._resolve_io_timeout(timeout_s),
                    "get_product_id",
                    self,
                )
        except Exception as exc:
            if self._transport_poisoned:
                raise
            self.logger.debug(f"Skipping AMX identity probe after connect: {exc}")
            return

        try:
            status_value = int(status)
        except (TypeError, ValueError):
            return
        if status_value != self.NO_ERR:
            raise RuntimeError(
                "Connected device did not respond to the AMX identity probe: "
                f"get_product_id returned {self.format_status(status_value)}. "
                "Check the COM port and use the matching plugin for that instrument."
            )
        if not product_id:
            return

        normalized = product_id.upper()
        if any(token in normalized for token in self._EXPECTED_PRODUCT_TOKENS):
            return

        self.logger.warning(
            "Connected device does not look like an AMX controller. "
            f"Reported product_id='{product_id}'. Check the COM port and use the "
            "matching driver for that instrument."
        )

    def connect(self, timeout_s: float = 5.0) -> bool:
        """Connect to the AMX device."""
        try:
            if self.connected:
                self._set_port_claimed(True)
                self.logger.info(
                    f"AMX device {self.device_id} is already connected; skipping open_port"
                )
                return True

            timeout_s = self._resolve_io_timeout(timeout_s)
            self._warn_on_other_process_ports()
            self.logger.info(
                f"Connecting to AMX device {self.device_id} "
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
                    f"AMX open_port failed: {self.format_status(status)}"
                )

            self._set_port_claimed(True)
            baud_status, actual_baud = self._call_locked_with_timeout(
                set_baud_rate, timeout_s, "set_baud_rate", self.baudrate
            )
            if baud_status == self.NO_ERR:
                self.connected = True
                try:
                    self._warn_if_unexpected_product_id(timeout_s=timeout_s)
                except Exception:
                    if not self._transport_poisoned:
                        with contextlib.suppress(Exception):
                            close_status = self._call_locked_with_timeout(
                                close_port,
                                timeout_s,
                                "close_port",
                            )
                            if close_status == self.NO_ERR:
                                self._set_port_claimed(False)
                    self.connected = False
                    raise
                self.logger.info(
                    f"Successfully connected to AMX device {self.device_id} "
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
                    "AMX port rollback after baud-rate failure also failed: "
                    f"{self.format_status(close_status)}"
                )
            else:
                self._set_port_claimed(False)
            raise RuntimeError(
                f"AMX set_baud_rate failed: {self.format_status(baud_status)}"
            )
        except Exception:
            self.connected = False
            raise

    def _cleanup_initialize_failure(self, timeout_s: float) -> None:
        """Best-effort cleanup after a failed AMX initialize() sequence."""
        if self._transport_poisoned:
            self.disconnect(timeout_s=timeout_s)
            return

        try:
            self.shutdown(timeout_s=timeout_s)
        except Exception as exc:  # noqa: BLE001
            self.logger.error(
                "AMX initialize cleanup failed after startup error: "
                f"{exc}"
            )

    def _set_loaded_config_state(
        self,
        config_number: Optional[int],
        *,
        config_name: Optional[str] = None,
        source: str = "",
    ) -> None:
        self.loaded_config_number = (
            None if config_number is None else int(config_number)
        )
        self.loaded_config_name = str(config_name or "").strip()
        self.loaded_config_source = str(source or "").strip()

    def _loaded_config_status(self) -> dict[str, object]:
        return {
            "memory_config": self.loaded_config_number,
            "memory_config_name": self.loaded_config_name or None,
            "memory_config_source": self.loaded_config_source or None,
        }

    def _remember_loaded_config(
        self,
        config_number: int,
        *,
        timeout_s: Optional[float] = None,
        config_name: Optional[str] = None,
        source: str = "explicit",
    ) -> None:
        resolved_name = str(config_name or "").strip()
        if not resolved_name and self.connected:
            try:
                io_timeout_s = self._resolve_io_timeout(timeout_s)
                status, resolved_name = self._call_locked_with_timeout(
                    AMXBase.get_config_name,
                    io_timeout_s,
                    f"get_config_name[{config_number}]",
                    self,
                    config_number,
                )
                self._raise_on_status(status, f"get_config_name({config_number})")
            except Exception as exc:  # noqa: BLE001
                self.logger.debug(
                    "Could not resolve AMX config name for loaded slot "
                    f"{config_number}: {exc}"
                )
                resolved_name = ""

        self._set_loaded_config_state(
            config_number,
            config_name=resolved_name,
            source=source,
        )

    def _find_auto_standby_config(
        self,
        timeout_s: Optional[float] = None,
    ) -> Optional[dict[str, object]]:
        try:
            configs = self.list_configs(timeout_s=timeout_s)
        except Exception as exc:  # noqa: BLE001
            self.logger.debug(
                "Could not inspect AMX config list during initialize(): "
                f"{exc}"
            )
            return None

        exact_matches = [
            config
            for config in configs
            if bool(config.get("valid"))
            and str(config.get("name", "")).strip().lower() == "standby"
        ]
        if exact_matches:
            return exact_matches[0]

        partial_matches = [
            config
            for config in configs
            if bool(config.get("valid"))
            and "standby" in str(config.get("name", "")).strip().lower()
        ]
        if len(partial_matches) == 1:
            return partial_matches[0]
        if len(partial_matches) > 1:
            self.logger.warning(
                "AMX initialize() found multiple valid standby-like configs; "
                "skipping automatic memory load. Choose a slot explicitly."
            )
        return None

    def initialize(
        self,
        timeout_s: float = 5.0,
        *,
        standby_config: Optional[int] = None,
        operating_config: Optional[int] = None,
        require_standby_device_disabled: bool = True,
    ) -> dict:
        """
        Run the routine AMX startup sequence.

        initialize() always establishes the transport with connect(). Standby
        and operating configurations are optional. When neither is provided,
        initialize() will try to auto-load a valid configuration named
        ``Standby`` into controller memory. When a standby configuration is
        available, it is checked to confirm that the AMX device-enable flag
        stays cleared before any operating configuration is applied.
        """
        timeout_s = self._resolve_io_timeout(timeout_s)
        was_connected = self.connected
        selected_standby_name = ""
        standby_source = "standby"
        self.logger.info(
            f"Initializing AMX device {self.device_id}"
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

        initialization_state = {}
        try:
            self.connect(timeout_s=timeout_s)

            if standby_config is None and operating_config is None:
                auto_standby = self._find_auto_standby_config(timeout_s=timeout_s)
                if auto_standby is not None:
                    standby_config = int(auto_standby["index"])
                    selected_standby_name = str(
                        auto_standby.get("name", "")
                    ).strip()
                    standby_source = "auto-standby"

            if standby_config is not None:
                self.load_config(standby_config, timeout_s=timeout_s)
                self._set_loaded_config_state(
                    standby_config,
                    config_name=selected_standby_name or self.loaded_config_name,
                    source=standby_source,
                )
                device_enabled = self.get_device_enabled(timeout_s=timeout_s)
                initialization_state.update(
                    {
                        "standby_config": int(standby_config),
                        "device_enabled": device_enabled,
                    }
                )

                if require_standby_device_disabled and device_enabled:
                    raise RuntimeError(
                        "AMX standby configuration left the device enabled. "
                        "Refusing to continue initialization."
                    )

            if operating_config is not None:
                self.load_config(operating_config, timeout_s=timeout_s)
                self._set_loaded_config_state(
                    operating_config,
                    config_name=self.loaded_config_name,
                    source="operating",
                )
                initialization_state["operating_config"] = int(operating_config)

            if self.loaded_config_number is not None:
                initialization_state.update(self._loaded_config_status())
            return initialization_state
        except Exception:
            if was_connected or self.connected or self._transport_poisoned:
                self._cleanup_initialize_failure(timeout_s)
            raise

    def disconnect(self, timeout_s: Optional[float] = None) -> bool:
        """Disconnect from the AMX device."""
        was_connected = self.connected
        timeout_s = self._resolve_io_timeout(timeout_s)

        try:
            if self._transport_poisoned:
                self._set_port_claimed(True)
                self.logger.warning(
                    f"Skipping AMX close_port for {self.device_id} because the "
                    "transport is marked unusable."
                )
                return False

            if not was_connected:
                if not self._dll_port_claimed:
                    self._set_port_claimed(False)
                self._set_loaded_config_state(None)
                return True

            self.logger.info(f"Disconnecting AMX device {self.device_id}")
            status = self._call_locked_with_timeout(
                super().close_port,
                timeout_s,
                "close_port",
            )
            if status == self.NO_ERR:
                self.connected = False
                self._set_port_claimed(False)
                self._set_loaded_config_state(None)
                self.logger.info(
                    f"Successfully disconnected AMX device {self.device_id}"
                )
                return True

            self._set_port_claimed(True)
            self.logger.error(
                f"Failed to disconnect AMX device {self.device_id}: "
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
            **self._loaded_config_status(),
        }

    def _list_configs_unlocked(self, include_empty: bool = False) -> list[dict]:
        """Collect config metadata while the caller holds self.thread_lock."""
        assert self.thread_lock.locked(), "caller must hold self.thread_lock"
        status, active_list, valid_list = AMXBase.get_config_list(self)
        self._raise_on_status(status, "get_config_list")

        configs = []
        for index, (active, valid) in enumerate(zip(active_list, valid_list)):
            if not include_empty and not (active or valid):
                continue
            name_status, name = AMXBase.get_config_name(self, index)
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
        """Return AMX configurations with flags and names."""
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
        """Load and apply one AMX configuration stored in controller NVM."""
        self._require_connected()
        self.logger.info(f"Loading AMX config {config_number}")
        timeout_s = self._resolve_io_timeout(timeout_s)
        status = self._call_locked_with_timeout(
            AMXBase.load_current_config,
            timeout_s,
            "load_current_config",
            self,
            config_number,
        )
        self._raise_on_status(status, f"load_current_config({config_number})")
        self._remember_loaded_config(config_number, timeout_s=timeout_s)

    def set_device_enabled(
        self,
        enable: bool,
        timeout_s: Optional[float] = None,
    ) -> None:
        """Set the AMX device enable flag."""
        self._require_connected()
        timeout_s = self._resolve_io_timeout(timeout_s)
        status = self._call_locked_with_timeout(
            AMXBase.set_device_enable,
            timeout_s,
            "set_device_enable",
            self,
            enable,
        )
        self._raise_on_status(status, f"set_device_enable({enable})")

    def get_device_enabled(self, timeout_s: Optional[float] = None) -> bool:
        """Return the AMX device enable flag."""
        self._require_connected()
        timeout_s = self._resolve_io_timeout(timeout_s)
        status, enabled = self._call_locked_with_timeout(
            AMXBase.get_device_enable,
            timeout_s,
            "get_device_enable",
            self,
        )
        self._raise_on_status(status, "get_device_enable")
        return enabled

    def get_frequency_hz(self, timeout_s: Optional[float] = None) -> float:
        """Return the oscillator frequency in hertz."""
        self._require_connected()
        timeout_s = self._resolve_io_timeout(timeout_s)
        status, period = self._call_locked_with_timeout(
            AMXBase.get_oscillator_period,
            timeout_s,
            "get_oscillator_period",
            self,
        )
        self._raise_on_status(status, "get_oscillator_period")
        return self.CLOCK / (period + self.OSC_OFFSET)

    def get_frequency_khz(self, timeout_s: Optional[float] = None) -> float:
        """Return the oscillator frequency in kilohertz."""
        return self.get_frequency_hz(timeout_s=timeout_s) / 1000.0

    def set_frequency_hz(
        self,
        frequency_hz: float,
        timeout_s: Optional[float] = None,
    ) -> None:
        """Set the oscillator frequency in hertz."""
        if frequency_hz <= 0:
            raise ValueError("frequency_hz must be > 0")
        period = round((self.CLOCK / float(frequency_hz)) - self.OSC_OFFSET)
        if period < 1 or period > 0xFFFFFFFF:
            raise ValueError(
                f"frequency_hz={frequency_hz} results in an invalid "
                f"oscillator period register: {period}"
            )
        self._require_connected()
        timeout_s = self._resolve_io_timeout(timeout_s)
        status = self._call_locked_with_timeout(
            AMXBase.set_oscillator_period,
            timeout_s,
            "set_oscillator_period",
            self,
            period,
        )
        self._raise_on_status(status, f"set_oscillator_period({period})")

    def set_frequency_khz(
        self,
        frequency_khz: float,
        timeout_s: Optional[float] = None,
    ) -> None:
        """Set the oscillator frequency in kilohertz."""
        self.set_frequency_hz(float(frequency_khz) * 1000.0, timeout_s=timeout_s)

    def get_pulser_delay_ticks(
        self,
        pulser_no: int,
        timeout_s: Optional[float] = None,
    ) -> int:
        """Return one pulser delay register."""
        self._require_connected()
        timeout_s = self._resolve_io_timeout(timeout_s)
        status, delay = self._call_locked_with_timeout(
            AMXBase.get_pulser_delay,
            timeout_s,
            f"get_pulser_delay[{pulser_no}]",
            self,
            pulser_no,
        )
        self._raise_on_status(status, f"get_pulser_delay({pulser_no})")
        return delay

    def set_pulser_delay_ticks(
        self,
        pulser_no: int,
        delay: int,
        timeout_s: Optional[float] = None,
    ) -> None:
        """Set one pulser delay register.  0 stops the pulser."""
        if int(delay) < 0:
            raise ValueError("delay must be >= 0")
        self._require_connected()
        timeout_s = self._resolve_io_timeout(timeout_s)
        status = self._call_locked_with_timeout(
            AMXBase.set_pulser_delay,
            timeout_s,
            f"set_pulser_delay[{pulser_no}]",
            self,
            pulser_no,
            delay,
        )
        self._raise_on_status(status, f"set_pulser_delay({pulser_no}, {delay})")

    def get_pulser_delay_seconds(
        self,
        pulser_no: int,
        timeout_s: Optional[float] = None,
    ) -> float:
        """Return one pulser delay in seconds."""
        delay = self.get_pulser_delay_ticks(pulser_no, timeout_s=timeout_s)
        return (delay + self.PULSER_DELAY_OFFSET) / self.CLOCK

    def set_pulser_width_ticks(
        self,
        pulser_no: int,
        width: int,
        timeout_s: Optional[float] = None,
    ) -> None:
        """Set one pulser width register.  0 stops the pulser."""
        if int(width) < 0:
            raise ValueError("width must be >= 0")
        self._require_connected()
        timeout_s = self._resolve_io_timeout(timeout_s)
        status = self._call_locked_with_timeout(
            AMXBase.set_pulser_width,
            timeout_s,
            f"set_pulser_width[{pulser_no}]",
            self,
            pulser_no,
            width,
        )
        self._raise_on_status(status, f"set_pulser_width({pulser_no}, {width})")

    def get_pulser_width_ticks(
        self,
        pulser_no: int,
        timeout_s: Optional[float] = None,
    ) -> int:
        """Return one pulser width register."""
        self._require_connected()
        timeout_s = self._resolve_io_timeout(timeout_s)
        status, width = self._call_locked_with_timeout(
            AMXBase.get_pulser_width,
            timeout_s,
            f"get_pulser_width[{pulser_no}]",
            self,
            pulser_no,
        )
        self._raise_on_status(status, f"get_pulser_width({pulser_no})")
        return width

    def get_pulser_width_seconds(
        self,
        pulser_no: int,
        timeout_s: Optional[float] = None,
    ) -> float:
        """Return one pulser width in seconds."""
        width = self.get_pulser_width_ticks(pulser_no, timeout_s=timeout_s)
        return (width + self.PULSER_WIDTH_OFFSET) / self.CLOCK

    def _set_pulser_duty_cycle_unlocked(self, pulser_no: int, duty_cycle: float) -> None:
        """Compute and set pulser width while holding self.thread_lock."""
        assert self.thread_lock.locked(), "caller must hold self.thread_lock"

        status, period = AMXBase.get_oscillator_period(self)
        self._raise_on_status(status, "get_oscillator_period")

        total_ticks = period + self.OSC_OFFSET
        width_register = round(total_ticks * duty_cycle - self.PULSER_WIDTH_OFFSET)
        if width_register < 1:
            raise ValueError(
                f"duty_cycle={duty_cycle} produces an invalid width register: "
                f"{width_register}"
            )

        status = AMXBase.set_pulser_width(self, pulser_no, width_register)
        self._raise_on_status(
            status, f"set_pulser_width({pulser_no}, {width_register})"
        )

    def set_pulser_duty_cycle(
        self,
        pulser_no: int,
        duty_cycle: float,
        timeout_s: Optional[float] = None,
    ) -> None:
        """Set one pulser duty cycle using the current oscillator period."""
        if not 0 < duty_cycle <= 1:
            raise ValueError("duty_cycle must satisfy 0 < duty_cycle <= 1")

        self._require_connected()
        timeout_s = self._resolve_io_timeout(timeout_s)
        self._call_locked_with_timeout(
            self._set_pulser_duty_cycle_unlocked,
            timeout_s,
            f"set_pulser_duty_cycle[{pulser_no}]",
            pulser_no,
            float(duty_cycle),
        )

    def set_switch_trigger_delay(
        self,
        switch_no: int,
        rise_delay: int,
        fall_delay: int,
        timeout_s: Optional[float] = None,
    ) -> None:
        """Set one switch coarse trigger rise/fall delays."""
        self._require_connected()
        timeout_s = self._resolve_io_timeout(timeout_s)
        status = self._call_locked_with_timeout(
            AMXBase.set_switch_trigger_delay,
            timeout_s,
            f"set_switch_trigger_delay[{switch_no}]",
            self,
            switch_no,
            rise_delay,
            fall_delay,
        )
        self._raise_on_status(
            status,
            f"set_switch_trigger_delay({switch_no}, {rise_delay}, {fall_delay})",
        )

    def get_switch_trigger_delay(
        self,
        switch_no: int,
        timeout_s: Optional[float] = None,
    ) -> tuple[int, int]:
        """Return one switch coarse trigger rise/fall delays."""
        self._require_connected()
        timeout_s = self._resolve_io_timeout(timeout_s)
        status, rise_delay, fall_delay = self._call_locked_with_timeout(
            AMXBase.get_switch_trigger_delay,
            timeout_s,
            f"get_switch_trigger_delay[{switch_no}]",
            self,
            switch_no,
        )
        self._raise_on_status(status, f"get_switch_trigger_delay({switch_no})")
        return rise_delay, fall_delay

    def set_switch_enable_delay(
        self,
        switch_no: int,
        delay: int,
        timeout_s: Optional[float] = None,
    ) -> None:
        """Set one switch coarse enable delay."""
        self._require_connected()
        timeout_s = self._resolve_io_timeout(timeout_s)
        status = self._call_locked_with_timeout(
            AMXBase.set_switch_enable_delay,
            timeout_s,
            f"set_switch_enable_delay[{switch_no}]",
            self,
            switch_no,
            delay,
        )
        self._raise_on_status(
            status, f"set_switch_enable_delay({switch_no}, {delay})"
        )

    def get_switch_enable_delay(
        self,
        switch_no: int,
        timeout_s: Optional[float] = None,
    ) -> int:
        """Return one switch coarse enable delay."""
        self._require_connected()
        timeout_s = self._resolve_io_timeout(timeout_s)
        status, delay = self._call_locked_with_timeout(
            AMXBase.get_switch_enable_delay,
            timeout_s,
            f"get_switch_enable_delay[{switch_no}]",
            self,
            switch_no,
        )
        self._raise_on_status(status, f"get_switch_enable_delay({switch_no})")
        return delay

    def _get_product_info_unlocked(self) -> dict:
        product_no_status, product_no = AMXBase.get_product_no(self)
        self._raise_on_status(product_no_status, "get_product_no")
        product_id_status, product_id = AMXBase.get_product_id(self)
        self._raise_on_status(product_id_status, "get_product_id")
        fw_version_status, fw_version = AMXBase.get_fw_version(self)
        self._raise_on_status(fw_version_status, "get_fw_version")
        fw_date_status, fw_date = AMXBase.get_fw_date(self)
        self._raise_on_status(fw_date_status, "get_fw_date")
        hw_type_status, hw_type = AMXBase.get_hw_type(self)
        self._raise_on_status(hw_type_status, "get_hw_type")
        hw_version_status, hw_version = AMXBase.get_hw_version(self)
        self._raise_on_status(hw_version_status, "get_hw_version")
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
        main_status, main_state_hex, main_state_name = AMXBase.get_main_state(self)
        self._raise_on_status(main_status, "get_main_state")
        device_state_status, device_state_hex, device_state_flags = AMXBase.get_device_state(self)
        self._raise_on_status(device_state_status, "get_device_state")
        controller_state_status, controller_state_hex, controller_state_flags = AMXBase.get_controller_state(self)
        self._raise_on_status(controller_state_status, "get_controller_state")
        device_enabled_status, device_enabled = AMXBase.get_device_enable(self)
        self._raise_on_status(device_enabled_status, "get_device_enable")
        housekeeping_status, volt_12v, volt_5v0, volt_3v3, temp_cpu = AMXBase.get_housekeeping(self)
        self._raise_on_status(housekeeping_status, "get_housekeeping")
        sensor_status, temperatures = AMXBase.get_sensor_data(self)
        self._raise_on_status(sensor_status, "get_sensor_data")
        fan_status, fan_enabled, fan_failed, fan_set_rpm, fan_measured_rpm, fan_pwm = AMXBase.get_fan_data(self)
        self._raise_on_status(fan_status, "get_fan_data")
        led_status, led_red, led_green, led_blue = AMXBase.get_led_data(self)
        self._raise_on_status(led_status, "get_led_data")
        cpu_status, cpu_load, cpu_frequency = AMXBase.get_cpu_data(self)
        self._raise_on_status(cpu_status, "get_cpu_data")
        uptime_status, uptime_s, uptime_ms, operation_s = AMXBase.get_uptime(self)
        self._raise_on_status(uptime_status, "get_uptime")
        total_time_status, total_uptime_s, total_operation_s = AMXBase.get_total_time(self)
        self._raise_on_status(total_time_status, "get_total_time")
        oscillator_status, oscillator_period = AMXBase.get_oscillator_period(self)
        self._raise_on_status(oscillator_status, "get_oscillator_period")

        pulsers = []
        for pulser in range(self.PULSER_NUM):
            delay_status, delay_ticks = AMXBase.get_pulser_delay(self, pulser)
            self._raise_on_status(delay_status, f"get_pulser_delay({pulser})")
            width_status, width_ticks = AMXBase.get_pulser_width(self, pulser)
            self._raise_on_status(width_status, f"get_pulser_width({pulser})")
            burst = None
            if pulser < self.PULSER_BURST_NUM:
                burst_status, burst = AMXBase.get_pulser_burst(self, pulser)
                self._raise_on_status(burst_status, f"get_pulser_burst({pulser})")
            pulsers.append(
                {
                    "pulser": pulser,
                    "label": self.PULSER_LABELS.get(pulser, str(pulser)),
                    "delay_ticks": delay_ticks,
                    "width_ticks": width_ticks,
                    "burst": burst,
                }
            )

        switches = []
        for switch in range(self.SWITCH_NUM):
            trig_cfg_status, trigger_config = AMXBase.get_switch_trigger_config(self, switch)
            self._raise_on_status(trig_cfg_status, f"get_switch_trigger_config({switch})")
            enb_cfg_status, enable_config = AMXBase.get_switch_enable_config(self, switch)
            self._raise_on_status(enb_cfg_status, f"get_switch_enable_config({switch})")
            trig_delay_status, rise_delay, fall_delay = AMXBase.get_switch_trigger_delay(self, switch)
            self._raise_on_status(trig_delay_status, f"get_switch_trigger_delay({switch})")
            enb_delay_status, enable_delay = AMXBase.get_switch_enable_delay(self, switch)
            self._raise_on_status(enb_delay_status, f"get_switch_enable_delay({switch})")
            switches.append(
                {
                    "switch": switch,
                    "trigger_config": trigger_config,
                    "enable_config": enable_config,
                    "trigger_delay": {
                        "rise": rise_delay,
                        "fall": fall_delay,
                    },
                    "enable_delay": enable_delay,
                }
            )

        oscillator_frequency_hz = self.CLOCK / (oscillator_period + self.OSC_OFFSET)
        return {
            "device_enabled": device_enabled,
            "main_state": {
                "hex": main_state_hex,
                "name": main_state_name,
            },
            "device_state": {
                "hex": device_state_hex,
                "flags": device_state_flags,
            },
            "controller_state": {
                "hex": controller_state_hex,
                "flags": controller_state_flags,
            },
            "housekeeping": {
                "volt_12v_v": volt_12v,
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
            "oscillator": {
                "period": oscillator_period,
                "frequency_hz": oscillator_frequency_hz,
            },
            "pulsers": pulsers,
            "switches": switches,
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

    def shutdown(
        self,
        *,
        standby_config: int | None = None,
        disable_device: bool = True,
        timeout_s: Optional[float] = None,
    ) -> bool:
        """Disable the AMX or load an explicit standby config, then disconnect."""
        errors: list[tuple[str, BaseException]] = []
        if standby_config is not None and disable_device:
            raise ValueError(
                "standby_config cannot be combined with disable_device. Either "
                "load an explicit standby config or request an explicit shutdown "
                "sequence."
            )

        standby_loaded = False
        if self.connected and standby_config is not None:
            try:
                self._call_with_optional_timeout(
                    self.load_config,
                    standby_config,
                    timeout_s=timeout_s,
                )
                standby_loaded = True
            except Exception as exc:  # noqa: BLE001
                self._append_shutdown_error(
                    errors, f"load_config({standby_config})", exc
                )
        should_disable_device = disable_device or (
            standby_config is not None and not standby_loaded
        )
        if self.connected and should_disable_device:
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
                RuntimeError("AMX disconnect failed during shutdown."),
            )

        self._raise_shutdown_errors(errors)
        return disconnected


class AMX(ProcessIsolatedClientMixin):
    """Public AMX client with process isolation on Windows."""

    _INSTRUMENT_NAME = "AMX"
    _PROCESS_CONTROLLER_CLASS = _AMXController
    _PROCESS_CONTROLLER_PATH = f"{__name__}:_AMXController"
    _PROCESS_TIMEOUT_RULES = {
        "connect": (4.0, 5.0, 15.0),
        "initialize": (8.0, 5.0, 30.0),
    }
    _active_connections_lock = _AMXController._active_connections_lock
    _active_connections = _AMXController._active_connections

    def __init__(
        self,
        device_id: str,
        com: int,
        port: int = 0,
        baudrate: int = 230400,
        process_backend: bool | None = None,
        logger: Optional[logging.Logger] = None,
        thread_lock: Optional[threading.Lock] = None,
        dll_path: Optional[str] = None,
        log_dir: Optional[Path] = None,
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
            allow_process_backend=bool(process_backend),
            process_backend_disabled_reason=(
                "AMX process isolation disabled by caller; using inline controller."
                if process_backend is False
                else ""
            ),
        )
