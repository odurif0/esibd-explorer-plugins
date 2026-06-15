"""High-level CGC DMMR driver."""

from __future__ import annotations

import logging
import queue
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
from .dmmr_base import DMMRBase


class _DMMRController(DllPortClaimRegistryMixin, TimeoutSafeDllMixin, DMMRBase):
    """High-level CGC DMMR driver."""

    _INSTRUMENT_NAME = "DMMR"
    _active_connections_lock = threading.Lock()
    _active_connections: dict[int, dict[str, object]] = {}
    _EXPECTED_PRODUCT_TOKENS = ("DMMR",)
    _DEFAULT_IO_TIMEOUT_S = 5.0
    _COMPAT_OPTIONAL_STATUSES = frozenset(
        {
            DMMRBase.ERR_COMMAND_RECEIVE,
            DMMRBase.ERR_DATA_RECEIVE,
        }
    )

    def __init__(
        self,
        device_id: str,
        com: int,
        baudrate: int = 230400,
        logger: Optional[logging.Logger] = None,
        hk_thread: Optional[threading.Thread] = None,
        thread_lock: Optional[threading.Lock] = None,
        hk_interval_s: float = 5.0,
        dll_path: Optional[str] = None,
        log_dir: Optional[Path] = None,
        **kwargs,
    ):
        if kwargs:
            unexpected = ", ".join(sorted(kwargs))
            raise TypeError(f"Unexpected DMMR init kwargs: {unexpected}")

        self._validate_init_args(
            device_id=device_id,
            com=com,
            baudrate=baudrate,
            hk_interval_s=hk_interval_s,
        )

        self.device_id = device_id
        self.com = int(com)
        self.port_num = 0
        self.baudrate = int(baudrate)
        self.hk_interval_s = float(hk_interval_s)
        self.connected = False
        self._dll_port_claimed = False
        self._transport_poisoned = False
        self._transport_error = None
        self._optional_command_warnings: set[tuple[str, int]] = set()
        self._optional_command_support: dict[tuple[str, Optional[int]], bool] = {}

        self.hk_running = False
        self.hk_stop_event = threading.Event()
        self.external_thread = hk_thread is not None
        self.external_lock = thread_lock is not None

        self.thread_lock = thread_lock or threading.Lock()
        self.hk_lock = threading.Lock()
        self.hk_thread = hk_thread or threading.Thread(
            target=self._hk_worker,
            name=f"HK_{device_id}",
            daemon=True,
        )

        self.logger = build_device_logger(
            instrument_name=self._INSTRUMENT_NAME,
            device_id=device_id,
            logger=logger,
            log_dir=log_dir,
            source_file=__file__,
        )

        super().__init__(com=com, log=None, idn=device_id, dll_path=dll_path)

    @staticmethod
    def _validate_init_args(device_id, com, baudrate, hk_interval_s):
        if not isinstance(device_id, str):
            raise TypeError("DMMR device_id must be a string.")
        if not device_id.strip():
            raise ValueError("DMMR device_id must be a non-empty string.")

        if isinstance(com, bool) or not isinstance(com, int):
            raise TypeError("DMMR com must be an integer between 1 and 255.")
        if not 1 <= com <= 255:
            raise ValueError("DMMR com must be between 1 and 255.")

        if isinstance(baudrate, bool) or not isinstance(baudrate, int):
            raise TypeError("DMMR baudrate must be a positive integer.")
        if baudrate <= 0:
            raise ValueError("DMMR baudrate must be a positive integer.")

        if isinstance(hk_interval_s, bool) or not isinstance(hk_interval_s, (int, float)):
            raise TypeError("DMMR hk_interval_s must be a positive number.")
        if hk_interval_s <= 0:
            raise ValueError("DMMR hk_interval_s must be greater than 0.")

    def _resolve_io_timeout(self, timeout_s: Optional[float] = None) -> float:
        if timeout_s is None:
            return self._DEFAULT_IO_TIMEOUT_S
        timeout_s = float(timeout_s)
        if timeout_s <= 0:
            raise ValueError("DMMR timeout_s must be greater than 0.")
        return timeout_s

    def _on_transport_poisoned(self) -> None:
        self._set_port_claimed(True)

    def _require_connected(self):
        if not self.connected:
            raise RuntimeError("DMMR device is not connected.")

    def _raise_on_status(self, status: int, action: str):
        if status != self.NO_ERR:
            raise RuntimeError(f"DMMR {action} failed: {self.format_status(status)}")

    def _is_optional_command_failure(self, status: int) -> bool:
        return status in self._COMPAT_OPTIONAL_STATUSES

    def _warn_optional_command(self, action: str, status: int) -> None:
        warning_key = (action, int(status))
        if warning_key in self._optional_command_warnings:
            return
        self._optional_command_warnings.add(warning_key)
        self.logger.warning(
            f"DMMR {action} is unavailable on this controller: {self.format_status(status)}"
        )

    def _read_snapshot_method(
        self,
        method,
        action: str,
        *args,
        cache_address: Optional[int] = None,
    ):
        cache_key = (action, None if cache_address is None else int(cache_address))
        if self._optional_command_support.get(cache_key) is False:
            return None

        result = method(self, *args)
        status = result[0]
        if status == self.NO_ERR:
            self._optional_command_support[cache_key] = True
            return result[1:]

        if self._is_optional_command_failure(status):
            self._optional_command_support[cache_key] = False
            self._warn_optional_command(action, status)

        return None

    def _recover_optional_module_auto_range_status(
        self,
        address: int,
        requested_auto_range: bool,
        status: int,
        timeout_s: float,
    ) -> int:
        if not self._is_optional_command_failure(status):
            return status

        verify_timeout_s = max(0.2, min(float(timeout_s), 1.0))
        for _attempt in range(3):
            verify_status, _meas_range, auto_range = self._call_locked_with_timeout(
                DMMRBase.get_module_meas_range,
                verify_timeout_s,
                f"verify_set_module_auto_range[{int(address)}]",
                self,
                int(address),
            )
            if verify_status == self.NO_ERR:
                if bool(auto_range) == bool(requested_auto_range):
                    return self.NO_ERR
                return status
            if not self._is_optional_command_failure(verify_status):
                return status
            time.sleep(0.05)

        return status

    @staticmethod
    def _build_module_housekeeping_snapshot(values: tuple[float, ...]) -> dict:
        return {
            "volt_3v3_v": values[0],
            "temp_cpu_c": values[1],
            "volt_5v0_v": values[2],
            "volt_12v_v": values[3],
            "volt_3v3i_v": values[4],
            "temp_cpui_c": values[5],
            "volt_2v5i_v": values[6],
            "volt_36vn_v": values[7],
            "volt_20vp_v": values[8],
            "volt_20vn_v": values[9],
            "volt_15vp_v": values[10],
            "volt_15vn_v": values[11],
            "volt_1v8p_v": values[12],
            "volt_1v8n_v": values[13],
            "volt_vrefp_v": values[14],
            "volt_vrefn_v": values[15],
        }

    @staticmethod
    def _build_module_runtime_snapshot(
        uptime_values,
        operation_values,
    ) -> dict:
        runtime = {}
        if uptime_values is not None:
            runtime.update(
                {
                    "seconds": uptime_values[0],
                    "milliseconds": uptime_values[1],
                    "total_seconds": uptime_values[2],
                    "total_milliseconds": uptime_values[3],
                }
            )
        if operation_values is not None:
            runtime.update(
                {
                    "operation_seconds": operation_values[0],
                    "operation_milliseconds": operation_values[1],
                    "total_operation_seconds": operation_values[2],
                    "total_operation_milliseconds": operation_values[3],
                }
            )
        return runtime

    def _fallback_config_flags_list_unlocked(self) -> tuple[list[bool], list[bool]]:
        active_list = []
        valid_list = []
        for index in range(self.MAX_CONFIG):
            flag_status, active, valid = DMMRBase.get_config_flags(self, index)
            self._raise_on_status(flag_status, f"get_config_flags({index})")
            active_list.append(active)
            valid_list.append(valid)
        return active_list, valid_list

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

    def _rollback_connect_failure(
        self,
        close_port,
        timeout_s: float,
        reason: str,
    ) -> bool:
        try:
            close_status = self._call_locked_with_timeout(
                close_port, timeout_s, "close_port"
            )
        except Exception as exc:
            self.logger.warning(
                f"DMMR port rollback after {reason} also failed: {exc}"
            )
            return False

        if close_status != self.NO_ERR:
            self.logger.warning(
                f"DMMR port rollback after {reason} also failed: "
                f"{self.format_status(close_status)}"
            )
            return False

        return True

    def _warn_if_unexpected_product_id(self):
        try:
            status, product_id = self._call_locked(DMMRBase.get_product_id, self)
        except Exception as exc:
            self.logger.debug(f"Skipping DMMR identity probe after connect: {exc}")
            return

        if status != self.NO_ERR or not product_id:
            return

        normalized = product_id.upper()
        if any(token in normalized for token in self._EXPECTED_PRODUCT_TOKENS):
            return

        self.logger.warning(
            "Connected device does not look like a DMMR controller. "
            f"Reported product_id='{product_id}'. Check the COM port and use the "
            "matching driver for that instrument."
        )

    def _verify_device_type(self, timeout_s: float) -> int:
        status, device_type = self._call_locked_with_timeout(
            super().get_device_type,
            timeout_s,
            "get_device_type",
        )
        if status != self.NO_ERR:
            raise RuntimeError(
                f"DMMR get_device_type failed: {self.format_status(status)}"
            )
        if int(device_type) != int(self.DEVICE_TYPE):
            raise RuntimeError(
                "DMMR device type mismatch: "
                f"expected 0x{self.DEVICE_TYPE:04X}, got 0x{int(device_type):04X}"
            )
        return int(device_type)

    def connect(self, timeout_s: float = 5.0) -> bool:
        """Connect to the DMMR device."""
        try:
            if self.connected:
                self._set_port_claimed(True)
                self.logger.info(
                    f"DMMR device {self.device_id} is already connected; skipping open_port"
                )
                return True

            self._warn_on_other_process_ports()
            self.logger.info(
                f"Connecting to DMMR device {self.device_id} on COM{self.com}"
            )

            open_port = super().open_port
            set_baud_rate = super().set_baud_rate
            close_port = super().close_port

            status = self._call_locked_with_timeout(
                open_port,
                timeout_s,
                "open_port",
                self.com,
            )
            if status != self.NO_ERR:
                raise RuntimeError(
                    f"DMMR open_port failed: {self.format_status(status)}"
                )

            self.connected = True
            self._set_port_claimed(True)

            baud_status, actual_baud = self._call_locked_with_timeout(
                set_baud_rate,
                timeout_s,
                "set_baud_rate",
                self.baudrate,
            )
            if baud_status != self.NO_ERR:
                released = self._rollback_connect_failure(
                    close_port, timeout_s, "baud-rate failure"
                )
                self.connected = False
                self._set_port_claimed(not released)
                raise RuntimeError(
                    f"DMMR set_baud_rate failed: {self.format_status(baud_status)}"
                )

            try:
                self._verify_device_type(timeout_s)
            except Exception as exc:
                released = self._rollback_connect_failure(
                    close_port, timeout_s, "device-type mismatch"
                )
                self.connected = False
                self._set_port_claimed(not released)
                raise RuntimeError(str(exc)) from exc

            self._warn_if_unexpected_product_id()
            self.logger.info(
                f"Successfully connected to DMMR device {self.device_id} "
                f"(baud rate: {actual_baud})"
            )
            return True
        except Exception:
            self.connected = False
            raise

    def initialize(
        self,
        timeout_s: float = 5.0,
        *,
        persist_scan: bool = True,
    ) -> dict:
        """
        Connect to the controller, refresh the module scan, and return modules.

        By default, a detected module mismatch is acknowledged after the rescan
        and the current module population is stored as the new controller
        reference. Pass ``persist_scan=False`` to inspect the scan without
        updating the stored reference.
        """
        timeout_s = self._resolve_io_timeout(timeout_s)
        was_connected = self.connected
        self.logger.info(
            f"Initializing DMMR device {self.device_id} by rescanning modules"
        )

        try:
            self.connect(timeout_s=timeout_s)

            rescan_status = self.rescan_modules(timeout_s=timeout_s)
            self._raise_on_status(rescan_status, "rescan_modules")

            modules = self.scan_modules(timeout_s=timeout_s)
            mismatch_status, module_mismatch = self.get_scanned_module_state(
                timeout_s=timeout_s
            )
            self._raise_on_status(mismatch_status, "get_scanned_module_state")

            if module_mismatch:
                warning = (
                    "DMMR module scan does not match the saved controller "
                    "configuration."
                )
                if persist_scan:
                    persist_status = self.set_scanned_module_state(timeout_s=timeout_s)
                    self._raise_on_status(persist_status, "set_scanned_module_state")
                    self.logger.warning(
                        f"{warning} Saved the current scan as the new reference."
                    )
                else:
                    self.logger.warning(
                        f"{warning} If the detected hardware is correct, call "
                        "set_scanned_module_state() once to acknowledge it."
                    )

            if not modules:
                self.logger.warning("DMMR initialize detected no installed modules.")

            return modules
        except Exception:
            if was_connected or self.connected or self._transport_poisoned:
                self.disconnect()
            raise

    def _force_disconnect_poisoned_transport(self, timeout_s: float = 1.0) -> bool:
        """Best-effort close for an unusable inline transport.

        After a timed-out DLL call, the normal serialized close path cannot run
        because the transport lock is GUARANTEED to still be held by the blocked
        daemon thread (which cannot be interrupted while inside the native DLL
        call). A direct ``close_port`` is attempted in a short-lived helper
        thread so a fresh controller instance can reuse the COM port when the
        vendor DLL accepts the close.

        CAUTION: this unavoidably issues close_port concurrently with the still-
        blocked abandoned thread. The vendor DLL's per-port thread safety is not
        guaranteed; this is a least-bad recovery path, not a clean shutdown. The
        call is made idempotent so repeated transport failures do not spawn
        multiple competing close threads.
        """
        if getattr(self, "_force_disconnect_attempted", False):
            self.logger.warning(
                "DMMR force-close already attempted for this instance; not retrying "
                "to avoid concurrent close_port calls against the abandoned thread."
            )
            return False
        self._force_disconnect_attempted = True
        result_queue: queue.Queue[tuple[str, object]] = queue.Queue(maxsize=1)
        close_port = super().close_port

        def runner() -> None:
            try:
                result_queue.put(("result", close_port()))
            except Exception as exc:  # pragma: no cover - forwarded to caller
                result_queue.put(("error", exc))

        thread = threading.Thread(
            target=runner,
            name=f"DMMR_{self.device_id}_force_close",
            daemon=True,
        )
        thread.start()
        thread.join(timeout_s)

        if thread.is_alive():
            self.logger.warning(
                f"DMMR close_port did not return within {timeout_s:.1f}s after the "
                "transport became unusable. Leaving the DLL port claim active."
            )
            return False

        try:
            kind, payload = result_queue.get_nowait()
        except queue.Empty:
            self.logger.warning(
                "DMMR force-close worker exited without reporting a close_port result."
            )
            return False

        if kind == "error":
            self.logger.warning(
                f"Best-effort DMMR close_port after transport timeout failed: {payload}"
            )
            return False

        status = int(payload)
        if status in {self.NO_ERR, self.ERR_NOT_CONNECTED}:
            self.logger.warning(
                f"Released DMMR port for {self.device_id} after a transport timeout. "
                "Create a new controller instance before reconnecting."
            )
            return True

        self.logger.warning(
            "Best-effort DMMR close_port after transport timeout returned "
            f"{self.format_status(status)}"
        )
        return False

    def disconnect(self) -> bool:
        """Disconnect from the DMMR device."""
        self.stop_housekeeping()
        was_connected = self.connected

        try:
            if self._transport_poisoned:
                self.connected = False
                released = self._force_disconnect_poisoned_transport()
                self._set_port_claimed(not released)
                return released

            if not was_connected:
                self.connected = False
                if not self._dll_port_claimed:
                    self._set_port_claimed(False)
                return True

            self.logger.info(f"Disconnecting DMMR device {self.device_id}")
            status = self._call_locked(super().close_port)
            if status == self.NO_ERR:
                self.connected = False
                self._set_port_claimed(False)
                self.logger.info(
                    f"Successfully disconnected DMMR device {self.device_id}"
                )
                return True

            self.connected = was_connected
            self._set_port_claimed(True)
            self.logger.error(
                f"Failed to disconnect DMMR device {self.device_id}: "
                f"{self.format_status(status)}"
            )
            return False
        except Exception as exc:
            self.connected = was_connected
            self._set_port_claimed(True)
            self.logger.error(f"Disconnection error: {exc}")
            return False

    def _hk_worker(self):
        self.logger.info(f"Housekeeping worker started for {self.device_id}")

        while not self.hk_stop_event.is_set() and self.hk_running:
            try:
                if self.connected:
                    self.hk_monitor()
                    self.hk_stop_event.wait(timeout=self.hk_interval_s)
                else:
                    self.hk_stop_event.wait(timeout=1.0)
            except Exception as exc:
                self.logger.error(f"Housekeeping worker error: {exc}")
                self.hk_stop_event.wait(timeout=1.0)

        self.logger.info(f"Housekeeping worker stopped for {self.device_id}")

    def _hk_product_info(self):
        status, product_no = DMMRBase.get_product_no(self)
        if status == self.NO_ERR:
            self.logger.info(f"Product number: {product_no}")
        return status == self.NO_ERR

    def _hk_main_state(self):
        status, state_hex, state_name = DMMRBase.get_state(self)
        if status == self.NO_ERR:
            self.logger.info(f"Main state: {state_name} ({state_hex})")
        return status == self.NO_ERR

    def _hk_device_state(self):
        status, state_hex, state_names = DMMRBase.get_device_state(self)
        if status == self.NO_ERR:
            self.logger.info(f"Device state: {', '.join(state_names)} ({state_hex})")
        return status == self.NO_ERR

    def _hk_general_housekeeping(self):
        status, volt_12v, volt_5v0, volt_3v3, temp_cpu = DMMRBase.get_housekeeping(self)
        if status == self.NO_ERR:
            self.logger.info("get_housekeeping() results:")
            self.logger.info(f"  12V Supply: {volt_12v:.2f}V")
            self.logger.info(f"  5V Supply: {volt_5v0:.2f}V")
            self.logger.info(f"  3.3V Supply: {volt_3v3:.2f}V")
            self.logger.info(f"  CPU Temperature: {temp_cpu:.1f}degC")
        return status == self.NO_ERR

    def _hk_voltage_state(self):
        status, state_hex, state_names = DMMRBase.get_voltage_state(self)
        if status == self.NO_ERR:
            self.logger.info(f"Voltage state: {', '.join(state_names)} ({state_hex})")
        return status == self.NO_ERR

    def _hk_temperature_state(self):
        status, state_hex, state_names = DMMRBase.get_temperature_state(self)
        if status == self.NO_ERR:
            self.logger.info(
                f"Temperature state: {', '.join(state_names)} ({state_hex})"
            )
        return status == self.NO_ERR

    def _hk_base_state(self):
        status, state_hex, state_names = DMMRBase.get_base_state(self)
        if status == self.NO_ERR:
            self.logger.info(f"Base state: {', '.join(state_names)} ({state_hex})")
        return status == self.NO_ERR

    def _hk_base_temp(self):
        status, base_temp = DMMRBase.get_base_temp(self)
        if status == self.NO_ERR:
            self.logger.info(f"Base temperature: {base_temp:.1f}degC")
        return status == self.NO_ERR

    def _hk_fan_data(self):
        status, set_pwm, state_hex, state_names = DMMRBase.get_base_fan_pwm(self)
        if status == self.NO_ERR:
            self.logger.info(
                f"Fan PWM: {set_pwm}, State: {', '.join(state_names)} ({state_hex})"
            )
        rpm_status, rpm = DMMRBase.get_base_fan_rpm(self)
        if rpm_status == self.NO_ERR:
            self.logger.info(f"Fan RPM: {rpm:.0f}")
        return status == self.NO_ERR

    def _hk_led_data(self):
        status, red, green, blue = DMMRBase.get_base_led_data(self)
        if status == self.NO_ERR:
            self.logger.info(f"LED state: R={red}, G={green}, B={blue}")
        return status == self.NO_ERR

    def _hk_cpu_data(self):
        status, load, frequency = DMMRBase.get_cpu_data(self)
        if status == self.NO_ERR:
            self.logger.info(
                f"CPU: Load={load * 100:.1f}%, Frequency={frequency / 1e6:.1f}MHz"
            )
        return status == self.NO_ERR

    def _hk_module_presence(self):
        status, valid, max_module, presence_list = DMMRBase.get_module_presence(self)
        if status == self.NO_ERR:
            present_modules = [
                index
                for index, present in enumerate(presence_list[: self.MODULE_NUM])
                if present == self.MODULE_PRESENT
            ]
            self.logger.info(
                f"Modules present: {present_modules} (Max: {max_module}, Valid: {valid})"
            )
        return status == self.NO_ERR

    def hk_monitor(self):
        """Run one DMMR housekeeping batch under the shared transport lock."""
        try:
            # Housekeeping holds the transport lock for the whole batch, so it must
            # call the low-level DMMRBase methods directly and avoid wrappers that
            # would try to reacquire the same lock.
            with self.thread_lock:
                self._hk_product_info()
                self._hk_main_state()
                self._hk_device_state()
                self._hk_general_housekeeping()
                self._hk_voltage_state()
                self._hk_temperature_state()
                self._hk_base_state()
                self._hk_base_temp()
                self._hk_fan_data()
                self._hk_led_data()
                self._hk_cpu_data()
                self._hk_module_presence()
        except Exception as exc:
            self.logger.error(f"Housekeeping monitoring failed: {exc}")

    def start_housekeeping(self, interval_s: Optional[float] = None) -> bool:
        """Start housekeeping monitoring."""
        if not self.connected:
            self.logger.warning("Cannot start housekeeping: device not connected")
            return False

        if interval_s is not None:
            if isinstance(interval_s, bool) or not isinstance(interval_s, (int, float)):
                raise TypeError("DMMR interval_s must be a positive number.")
            if interval_s <= 0:
                raise ValueError("DMMR interval_s must be greater than 0.")

        with self.hk_lock:
            if self.hk_running:
                self.logger.warning("Housekeeping already running")
                return True

            try:
                if interval_s is not None:
                    self.hk_interval_s = float(interval_s)

                self.hk_stop_event.clear()
                self.hk_running = True

                if self.external_thread:
                    self.logger.info("Housekeeping enabled for external thread control")
                else:
                    if not self.hk_thread.is_alive():
                        self.hk_thread = threading.Thread(
                            target=self._hk_worker,
                            name=f"HK_{self.device_id}",
                            daemon=True,
                        )
                    self.hk_thread.start()
                    self.logger.info(
                        f"Housekeeping thread started with {self.hk_interval_s}s interval"
                    )
                return True
            except Exception as exc:
                self.logger.error(f"Failed to start housekeeping: {exc}")
                self.hk_running = False
                return False

    def stop_housekeeping(self) -> bool:
        """Stop housekeeping monitoring."""
        if not self.hk_running:
            return True

        with self.hk_lock:
            try:
                self.hk_running = False
                self.hk_stop_event.set()

                if not self.external_thread and self.hk_thread.is_alive():
                    self.hk_thread.join(timeout=2.0)
                    if self.hk_thread.is_alive():
                        self.logger.warning(
                            "Housekeeping thread did not stop cleanly"
                        )
                    else:
                        self.logger.info("Housekeeping thread stopped")
                else:
                    self.logger.info("Housekeeping monitoring disabled")
                return True
            except Exception as exc:
                self.logger.error(f"Failed to stop housekeeping: {exc}")
                return False

    def do_housekeeping_cycle(self) -> bool:
        """Run one housekeeping cycle, intended for externally managed threads."""
        if not self.hk_running:
            return False

        try:
            if self.connected:
                self.hk_monitor()
                return True
            self.logger.warning("Housekeeping cycle skipped: device not connected")
            return False
        except Exception as exc:
            self.logger.error(f"Housekeeping cycle error: {exc}")
            return False

    def _get_product_info_unlocked(self) -> dict:
        product_no_status, product_no = DMMRBase.get_product_no(self)
        self._raise_on_status(product_no_status, "get_product_no")
        product_id_status, product_id = DMMRBase.get_product_id(self)
        self._raise_on_status(product_id_status, "get_product_id")
        device_type_status, device_type = DMMRBase.get_device_type(self)
        self._raise_on_status(device_type_status, "get_device_type")
        fw_version_status, fw_version = DMMRBase.get_fw_version(self)
        self._raise_on_status(fw_version_status, "get_fw_version")
        fw_date_status, fw_date = DMMRBase.get_fw_date(self)
        self._raise_on_status(fw_date_status, "get_fw_date")
        hw_type_status, hw_type = DMMRBase.get_hw_type(self)
        self._raise_on_status(hw_type_status, "get_hw_type")
        hw_version_status, hw_version = DMMRBase.get_hw_version(self)
        self._raise_on_status(hw_version_status, "get_hw_version")
        manuf_date_status, manuf_year, manuf_week = DMMRBase.get_manuf_date(self)
        self._raise_on_status(manuf_date_status, "get_manuf_date")
        base_product_no_status, base_product_no = DMMRBase.get_base_product_no(self)
        self._raise_on_status(base_product_no_status, "get_base_product_no")
        base_manuf_status, base_manuf_year, base_manuf_week = DMMRBase.get_base_manuf_date(
            self
        )
        self._raise_on_status(base_manuf_status, "get_base_manuf_date")
        base_hw_type_status, base_hw_type = DMMRBase.get_base_hw_type(self)
        self._raise_on_status(base_hw_type_status, "get_base_hw_type")
        base_hw_version_status, base_hw_version = DMMRBase.get_base_hw_version(self)
        self._raise_on_status(base_hw_version_status, "get_base_hw_version")

        return {
            "product_no": product_no,
            "product_id": product_id,
            "device_type": device_type,
            "firmware": {
                "version": fw_version,
                "date": fw_date,
            },
            "hardware": {
                "type": hw_type,
                "version": hw_version,
            },
            "manufacturing": {
                "year": manuf_year,
                "calendar_week": manuf_week,
            },
            "base": {
                "product_no": base_product_no,
                "hardware": {
                    "type": base_hw_type,
                    "version": base_hw_version,
                },
                "manufacturing": {
                    "year": base_manuf_year,
                    "calendar_week": base_manuf_week,
                },
            },
        }

    def get_product_info(self, timeout_s: Optional[float] = None) -> dict:
        """Return stable controller and base metadata."""
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

    def _collect_module_snapshot_unlocked(self, address: int) -> dict:
        address = int(address)
        module = {"address": address}

        status, product_id = DMMRBase.get_module_product_id(self, address)
        if status == self.NO_ERR:
            module["product_id"] = product_id

        status, product_no = DMMRBase.get_module_product_no(self, address)
        if status == self.NO_ERR:
            module["product_no"] = product_no

        status, device_type = DMMRBase.get_module_device_type(self, address)
        if status == self.NO_ERR:
            module["device_type"] = device_type

        status, fw_version = DMMRBase.get_module_fw_version(self, address)
        if status == self.NO_ERR:
            module["firmware"] = {"version": fw_version}

        status, fw_date = DMMRBase.get_module_fw_date(self, address)
        if status == self.NO_ERR:
            module.setdefault("firmware", {})["date"] = fw_date

        status, hw_type = DMMRBase.get_module_hw_type(self, address)
        if status == self.NO_ERR:
            module["hardware"] = {"type": hw_type}

        status, hw_version = DMMRBase.get_module_hw_version(self, address)
        if status == self.NO_ERR:
            module.setdefault("hardware", {})["version"] = hw_version

        status, manuf_year, manuf_week = DMMRBase.get_module_manuf_date(self, address)
        if status == self.NO_ERR:
            module["manufacturing"] = {
                "year": manuf_year,
                "calendar_week": manuf_week,
            }

        uptime_values = self._read_snapshot_method(
            DMMRBase.get_module_uptime_int,
            "get_module_uptime_int",
            address,
            cache_address=address,
        )
        operation_values = self._read_snapshot_method(
            DMMRBase.get_module_optime_int,
            "get_module_optime_int",
            address,
            cache_address=address,
        )
        module_runtime = self._build_module_runtime_snapshot(
            uptime_values,
            operation_values,
        )
        if module_runtime:
            module["uptime"] = module_runtime

        status, cpu_load = DMMRBase.get_module_cpu_data(self, address)
        if status == self.NO_ERR:
            module["cpu"] = {"load": cpu_load}

        hk_result = DMMRBase.get_module_housekeeping(self, address)
        if hk_result[0] == self.NO_ERR:
            module["housekeeping"] = self._build_module_housekeeping_snapshot(
                hk_result[1:]
            )

        status, module_state = DMMRBase.get_module_state(self, address)
        if status == self.NO_ERR:
            module["state"] = module_state

        status, buffer_empty = DMMRBase.get_module_buffer_state(self, address)
        if status == self.NO_ERR:
            module["buffer"] = {"empty": buffer_empty}

        status, ready_flags = DMMRBase.get_module_ready_flags(self, address)
        if status == self.NO_ERR:
            module["ready_flags"] = {
                "raw": ready_flags,
                "measurement_current_ready": bool(ready_flags & self.MEAS_CUR_RDY),
                "measurement_housekeeping_ready": bool(
                    ready_flags & self.HK_MEAS_DATA_RDY
                ),
                "module_housekeeping_ready": bool(ready_flags & self.HK_MOD_DATA_RDY),
            }

        meas_range_values = self._read_snapshot_method(
            DMMRBase.get_module_meas_range,
            "get_module_meas_range",
            address,
            cache_address=address,
        )
        if meas_range_values is not None:
            module["measurement_range"] = {
                "range": meas_range_values[0],
                "auto_range": meas_range_values[1],
            }

        status, meas_current, current_range = DMMRBase.get_module_current(self, address)
        if status == self.NO_ERR:
            module["current"] = {
                "value": meas_current,
                "range": current_range,
            }

        status, scanned_product_no, saved_product_no, scanned_hw_type, saved_hw_type = (
            DMMRBase.get_scanned_module_params(self, address)
        )
        if status == self.NO_ERR:
            module["scanned_params"] = {
                "scanned_product_no": scanned_product_no,
                "saved_product_no": saved_product_no,
                "scanned_hw_type": scanned_hw_type,
                "saved_hw_type": saved_hw_type,
            }

        return module

    def _collect_housekeeping_unlocked(self) -> dict:
        main_status, main_state_hex, main_state_name = DMMRBase.get_state(self)
        self._raise_on_status(main_status, "get_state")
        device_state_status, device_state_hex, device_state_flags = DMMRBase.get_device_state(
            self
        )
        self._raise_on_status(device_state_status, "get_device_state")
        voltage_state_status, voltage_state_hex, voltage_state_flags = DMMRBase.get_voltage_state(
            self
        )
        self._raise_on_status(voltage_state_status, "get_voltage_state")
        temp_state_status, temp_state_hex, temp_state_flags = DMMRBase.get_temperature_state(
            self
        )
        self._raise_on_status(temp_state_status, "get_temperature_state")
        enable_status, device_enabled = DMMRBase.get_enable(self)
        self._raise_on_status(enable_status, "get_enable")
        automatic_current_status, automatic_current = DMMRBase.get_automatic_current(self)
        self._raise_on_status(automatic_current_status, "get_automatic_current")
        housekeeping_status, volt_12v, volt_5v0, volt_3v3, temp_cpu = DMMRBase.get_housekeeping(
            self
        )
        self._raise_on_status(housekeeping_status, "get_housekeeping")
        cpu_status, cpu_load, cpu_frequency = DMMRBase.get_cpu_data(self)
        self._raise_on_status(cpu_status, "get_cpu_data")
        uptime_status, uptime_s, uptime_ms, total_uptime_s, total_uptime_ms = (
            DMMRBase.get_uptime_int(self)
        )
        self._raise_on_status(uptime_status, "get_uptime_int")
        optime_status, operation_s, operation_ms, total_operation_s, total_operation_ms = (
            DMMRBase.get_optime_int(self)
        )
        self._raise_on_status(optime_status, "get_optime_int")
        base_state_status, base_state_hex, base_state_flags = DMMRBase.get_base_state(self)
        self._raise_on_status(base_state_status, "get_base_state")
        base_temp_status, base_temp_c = DMMRBase.get_base_temp(self)
        self._raise_on_status(base_temp_status, "get_base_temp")
        fan_status, fan_pwm, fan_state_hex, fan_state_flags = DMMRBase.get_base_fan_pwm(self)
        self._raise_on_status(fan_status, "get_base_fan_pwm")
        fan_rpm_status, fan_rpm = DMMRBase.get_base_fan_rpm(self)
        self._raise_on_status(fan_rpm_status, "get_base_fan_rpm")
        led_status, led_red, led_green, led_blue = DMMRBase.get_base_led_data(self)
        self._raise_on_status(led_status, "get_base_led_data")
        presence_status, presence_valid, max_module, presence_list = DMMRBase.get_module_presence(
            self
        )
        self._raise_on_status(presence_status, "get_module_presence")
        mismatch_status, module_mismatch = DMMRBase.get_scanned_module_state(self)
        self._raise_on_status(mismatch_status, "get_scanned_module_state")

        present_modules = [
            address
            for address, present in enumerate(presence_list[: self.MODULE_NUM])
            if present == self.MODULE_PRESENT
        ]
        modules = {
            address: self._collect_module_snapshot_unlocked(address)
            for address in present_modules
        }

        return {
            "device_enabled": device_enabled,
            "automatic_current": automatic_current,
            "main_state": {
                "hex": main_state_hex,
                "name": main_state_name,
            },
            "device_state": {
                "hex": device_state_hex,
                "flags": device_state_flags,
            },
            "voltage_state": {
                "hex": voltage_state_hex,
                "flags": voltage_state_flags,
            },
            "temperature_state": {
                "hex": temp_state_hex,
                "flags": temp_state_flags,
            },
            "housekeeping": {
                "volt_12v_v": volt_12v,
                "volt_5v0_v": volt_5v0,
                "volt_3v3_v": volt_3v3,
                "temp_cpu_c": temp_cpu,
            },
            "cpu": {
                "load": cpu_load,
                "frequency_hz": cpu_frequency,
            },
            "uptime": {
                "seconds": uptime_s,
                "milliseconds": uptime_ms,
                "operation_seconds": operation_s,
                "operation_milliseconds": operation_ms,
                "total_uptime_seconds": total_uptime_s,
                "total_uptime_milliseconds": total_uptime_ms,
                "total_operation_seconds": total_operation_s,
                "total_operation_milliseconds": total_operation_ms,
            },
            "base": {
                "state": {
                    "hex": base_state_hex,
                    "flags": base_state_flags,
                },
                "temperature_c": base_temp_c,
                "fan": {
                    "pwm": fan_pwm,
                    "rpm": fan_rpm,
                    "state": {
                        "hex": fan_state_hex,
                        "flags": fan_state_flags,
                    },
                },
                "led": {
                    "red": led_red,
                    "green": led_green,
                    "blue": led_blue,
                },
            },
            "module_presence": {
                "valid": presence_valid,
                "max_module": max_module,
                "present": present_modules,
                "raw": presence_list[: self.MODULE_NUM],
            },
            "scanned_module_state": {
                "module_mismatch": module_mismatch,
            },
            "modules": modules,
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

    def get_status(self) -> dict:
        """Return the current driver status."""
        return {
            "device_id": self.device_id,
            "com": self.com,
            "baudrate": self.baudrate,
            "connected": self.connected,
            "transport_poisoned": self._transport_poisoned,
            "hk_running": self.hk_running,
            "hk_interval_s": self.hk_interval_s,
            "external_thread": self.external_thread,
            "external_lock": self.external_lock,
        }

    def set_enable(self, enable, timeout_s: Optional[float] = None):
        """Enable or disable module measurement."""
        self.logger.info(f"Setting DMMR enable to {enable}")
        timeout_s = self._resolve_io_timeout(timeout_s)
        try:
            status = self._call_locked_with_timeout(
                super().set_enable,
                timeout_s,
                "set_enable",
                enable,
            )
            if status == self.NO_ERR:
                self.logger.info(f"DMMR enable set to {bool(enable)}")
            else:
                self.logger.error(
                    f"Failed to set DMMR enable: {self.format_status(status)}"
                )
            return status
        except Exception as exc:
            self.logger.error(f"Error setting DMMR enable: {exc}")
            raise

    def get_state(self, timeout_s: Optional[float] = None):
        """Get the DMMR main state."""
        timeout_s = self._resolve_io_timeout(timeout_s)
        status, state_hex, state_name = self._call_locked_with_timeout(
            super().get_state,
            timeout_s,
            "get_state",
        )
        if status == self.NO_ERR:
            self.logger.info(f"Main state: {state_name} ({state_hex})")
        else:
            self.logger.error(
                f"Failed to get main state: {self.format_status(status)}"
            )
        return status, state_hex, state_name

    def restart(self, timeout_s: Optional[float] = None):
        """Restart the DMMR controller."""
        self.logger.info("Restarting DMMR device")
        timeout_s = self._resolve_io_timeout(timeout_s)
        try:
            status = self._call_locked_with_timeout(
                super().restart,
                timeout_s,
                "restart",
            )
            if status == self.NO_ERR:
                self.logger.info("Device restart successful")
            else:
                self.logger.error(
                    f"Device restart failed: {self.format_status(status)}"
                )
            return status
        except Exception as exc:
            self.logger.error(f"Error restarting DMMR device: {exc}")
            raise

    def get_scanned_module_state(self, timeout_s: Optional[float] = None):
        timeout_s = self._resolve_io_timeout(timeout_s)
        return self._call_locked_with_timeout(
            super().get_scanned_module_state,
            timeout_s,
            "get_scanned_module_state",
        )

    def rescan_modules(self, timeout_s: Optional[float] = None):
        timeout_s = self._resolve_io_timeout(timeout_s)
        return self._call_locked_with_timeout(
            super().rescan_modules,
            timeout_s,
            "rescan_modules",
        )

    def set_scanned_module_state(self, timeout_s: Optional[float] = None):
        timeout_s = self._resolve_io_timeout(timeout_s)
        return self._call_locked_with_timeout(
            super().set_scanned_module_state,
            timeout_s,
            "set_scanned_module_state",
        )

    def _scan_present_modules_unlocked(
        self,
        presence_list: list[int],
        max_module: int,
    ) -> dict[int, dict]:
        modules = {}
        for address in range(min(int(max_module) + 1, self.MODULE_NUM)):
            if presence_list[address] != self.MODULE_PRESENT:
                continue

            module_info = {}

            fw_status, fw_version = DMMRBase.get_module_fw_version(self, address)
            if fw_status == self.NO_ERR:
                module_info["fw_version"] = fw_version

            prod_status, product_no = DMMRBase.get_module_product_no(self, address)
            if prod_status == self.NO_ERR:
                module_info["product_no"] = product_no

            hw_status, hw_type = DMMRBase.get_module_hw_type(self, address)
            if hw_status == self.NO_ERR:
                module_info["hw_type"] = hw_type

            hwv_status, hw_version = DMMRBase.get_module_hw_version(self, address)
            if hwv_status == self.NO_ERR:
                module_info["hw_version"] = hw_version

            state_status, state = DMMRBase.get_module_state(self, address)
            if state_status == self.NO_ERR:
                module_info["state"] = state

            modules[address] = module_info

        return modules

    def set_automatic_current(
        self,
        automatic_current: bool,
        timeout_s: Optional[float] = None,
    ):
        """Enable or disable automatic current acquisition."""
        timeout_s = self._resolve_io_timeout(timeout_s)
        return self._call_locked_with_timeout(
            super().set_automatic_current,
            timeout_s,
            "set_automatic_current",
            automatic_current,
        )

    def get_automatic_current(self, timeout_s: Optional[float] = None):
        """Return the state of automatic current acquisition."""
        timeout_s = self._resolve_io_timeout(timeout_s)
        return self._call_locked_with_timeout(
            super().get_automatic_current,
            timeout_s,
            "get_automatic_current",
        )

    def get_current(self, timeout_s: Optional[float] = None):
        """Return the next automatic current measurement frame."""
        timeout_s = self._resolve_io_timeout(timeout_s)
        return self._call_locked_with_timeout(
            super().get_current,
            timeout_s,
            "get_current",
        )

    def set_module_meas_range(
        self,
        address: int,
        meas_range: int,
        timeout_s: Optional[float] = None,
    ):
        """Set the measurement range for one DMMR module."""
        timeout_s = self._resolve_io_timeout(timeout_s)
        return self._call_locked_with_timeout(
            DMMRBase.set_module_meas_range,
            timeout_s,
            f"set_module_meas_range[{int(address)}]",
            self,
            int(address),
            int(meas_range),
        )

    def set_module_auto_range(
        self,
        address: int,
        auto_range: bool,
        timeout_s: Optional[float] = None,
    ):
        """Enable or disable automatic range switching for one DMMR module."""
        timeout_s = self._resolve_io_timeout(timeout_s)
        status = self._call_locked_with_timeout(
            DMMRBase.set_module_auto_range,
            timeout_s,
            f"set_module_auto_range[{int(address)}]",
            self,
            int(address),
            bool(auto_range),
        )
        return self._recover_optional_module_auto_range_status(
            int(address),
            bool(auto_range),
            status,
            timeout_s,
        )

    def get_module_meas_range(self, address: int, timeout_s: Optional[float] = None):
        """Return the measurement range state for one DMMR module."""
        timeout_s = self._resolve_io_timeout(timeout_s)
        return self._call_locked_with_timeout(
            DMMRBase.get_module_meas_range,
            timeout_s,
            f"get_module_meas_range[{int(address)}]",
            self,
            int(address),
        )

    def get_module_ready_flags(self, address: int, timeout_s: Optional[float] = None):
        """Return ready flags for one DMMR module."""
        timeout_s = self._resolve_io_timeout(timeout_s)
        return self._call_locked_with_timeout(
            DMMRBase.get_module_ready_flags,
            timeout_s,
            f"get_module_ready_flags[{int(address)}]",
            self,
            int(address),
        )

    def get_module_current(self, address: int, timeout_s: Optional[float] = None):
        """Return the current measurement for one DMMR module."""
        timeout_s = self._resolve_io_timeout(timeout_s)
        return self._call_locked_with_timeout(
            DMMRBase.get_module_current,
            timeout_s,
            f"get_module_current[{int(address)}]",
            self,
            int(address),
        )

    def scan_modules(self, timeout_s: Optional[float] = None):
        """Scan and summarize connected modules."""
        self.logger.info("Scanning for connected DMMR modules")
        self._require_connected()
        timeout_s = self._resolve_io_timeout(timeout_s)
        try:
            status, valid, max_module, presence_list = self._call_locked_with_timeout(
                DMMRBase.get_module_presence,
                timeout_s,
                "get_module_presence",
                self,
            )
            self._raise_on_status(status, "get_module_presence")
            if not valid:
                self.logger.warning(
                    "DMMR module presence flags are marked invalid; returning the current scan result."
                )

            modules = self._call_locked_with_timeout(
                self._scan_present_modules_unlocked,
                timeout_s,
                "scan_present_modules",
                presence_list,
                max_module,
            )
            if modules:
                self.logger.info(f"Found {len(modules)} modules:")
                for address, info in modules.items():
                    self.logger.info(
                        f"  Module {address}: Product {info.get('product_no', 'Unknown')}, "
                        f"FW {info.get('fw_version', 'Unknown')}, "
                        f"State {info.get('state', 'Unknown')}"
                    )
            else:
                self.logger.warning("No modules found")
            return modules
        except Exception as exc:
            self.logger.error(f"Error scanning modules: {exc}")
            raise

    def _get_module_info_unlocked(self, address: int) -> dict:
        return self._collect_module_snapshot_unlocked(address)

    def get_module_info(self, address: int, timeout_s: Optional[float] = None):
        """Return a structured snapshot for one DMMR module."""
        timeout_s = self._resolve_io_timeout(timeout_s)
        self.logger.info(f"Getting information for DMMR module {int(address)}")
        return self._call_locked_with_timeout(
            self._get_module_info_unlocked,
            timeout_s,
            f"get_module_info[{int(address)}]",
            int(address),
        )

    def _list_configs_unlocked(self, include_empty: bool = False) -> list[dict]:
        active_list, valid_list = self._get_config_flags_list_unlocked()

        configs = []
        for index, (active, valid) in enumerate(zip(active_list, valid_list)):
            if not include_empty and not (active or valid):
                continue
            name_status, name = DMMRBase.get_config_name(self, index)
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

    def _get_config_flags_list_unlocked(self) -> tuple[list[bool], list[bool]]:
        cache_key = ("get_config_list", None)
        if self._optional_command_support.get(cache_key) is False:
            return self._fallback_config_flags_list_unlocked()

        status, active_list, valid_list = DMMRBase.get_config_list(self)
        if status == self.NO_ERR:
            self._optional_command_support[cache_key] = True
            return active_list, valid_list
        if self._is_optional_command_failure(status):
            self._optional_command_support[cache_key] = False
            self._warn_optional_command("get_config_list", status)
            return self._fallback_config_flags_list_unlocked()
        self._raise_on_status(status, "get_config_list")
        return [], []

    def get_config_list(self, timeout_s: Optional[float] = None):
        """Return config activity/validity lists, with compatibility fallback."""
        active_list, valid_list = self._call_locked_with_timeout(
            self._get_config_flags_list_unlocked,
            self._resolve_batch_timeout(
                timeout_s,
                multiplier=3.0,
                additive=5.0,
                minimum=15.0,
                maximum=45.0,
            ),
            "get_config_list",
        )
        return self.NO_ERR, active_list, valid_list

    def list_configs(
        self,
        include_empty: bool = False,
        timeout_s: Optional[float] = None,
    ) -> list[dict]:
        """Return DMMR configurations with flags and names."""
        return self._call_locked_with_timeout(
            self._list_configs_unlocked,
            self._resolve_batch_timeout(
                timeout_s,
                multiplier=3.0,
                additive=5.0,
                minimum=15.0,
                maximum=45.0,
            ),
            "list_configs",
            include_empty,
        )

    def shutdown(
        self,
        *,
        disable_device: bool = True,
        disable_automatic_current: bool = True,
        timeout_s: Optional[float] = None,
    ) -> bool:
        """Disable acquisition and disconnect from the DMMR."""
        timeout_s = self._resolve_io_timeout(timeout_s)

        if self.connected and disable_automatic_current:
            status = self.set_automatic_current(False, timeout_s=timeout_s)
            if status == self.ERR_NOT_CONNECTED:
                self.connected = False
                return self.disconnect()
            self._raise_on_status(status, "set_automatic_current(False)")

        if self.connected and disable_device:
            status = self.set_enable(False, timeout_s=timeout_s)
            if status == self.ERR_NOT_CONNECTED:
                self.connected = False
                return self.disconnect()
            self._raise_on_status(status, "set_enable(False)")

        return self.disconnect()


class DMMR(ProcessIsolatedClientMixin):
    """Public DMMR client with process isolation on Windows."""

    _INSTRUMENT_NAME = "DMMR"
    _PROCESS_CONTROLLER_CLASS = _DMMRController
    _PROCESS_CONTROLLER_PATH = f"{__name__}:_DMMRController"
    _PROCESS_TIMEOUT_RULES = {
        "connect": (4.0, 5.0, 15.0),
        "initialize": (8.0, 5.0, 30.0),
    }
    _active_connections_lock = _DMMRController._active_connections_lock
    _active_connections = _DMMRController._active_connections

    def __init__(
        self,
        device_id: str,
        com: int,
        baudrate: int = 230400,
        process_backend: Optional[bool] = None,
        logger: Optional[logging.Logger] = None,
        hk_thread: Optional[threading.Thread] = None,
        thread_lock: Optional[threading.Lock] = None,
        hk_interval_s: float = 5.0,
        dll_path: Optional[str] = None,
        log_dir: Optional[Path] = None,
        **kwargs,
    ):
        if process_backend is not None and not isinstance(process_backend, bool):
            raise TypeError("DMMR process_backend must be a boolean when provided.")

        backend_kwargs = {
            "device_id": device_id,
            "com": com,
            "baudrate": baudrate,
            "logger": logger,
            "hk_thread": hk_thread,
            "thread_lock": thread_lock,
            "hk_interval_s": hk_interval_s,
            "dll_path": dll_path,
            "log_dir": log_dir,
            **kwargs,
        }
        self._initialize_process_backend(
            backend_kwargs=backend_kwargs,
            incompatible_objects={
                "logger": logger,
                "hk_thread": hk_thread,
                "thread_lock": thread_lock,
            },
            allow_process_backend=bool(process_backend),
            process_backend_disabled_reason=(
                "DMMR process isolation disabled by caller; using inline controller."
                if process_backend is False
                else ""
            ),
        )

    def _call_backend_method(self, method_name: str, *args, **kwargs):
        backend_mode = object.__getattribute__(self, "_backend_mode")
        if backend_mode == "inline":
            backend = object.__getattribute__(self, "_backend")
            return getattr(backend, method_name)(*args, **kwargs)
        return self._call_process_method(method_name, *args, **kwargs)

    def _resolve_module_addresses(
        self,
        address: Optional[int] = None,
        *,
        timeout_s: Optional[float] = None,
    ) -> list[int]:
        if address is not None:
            return [int(address)]
        modules = self.scan_modules(timeout_s=timeout_s)
        if isinstance(modules, dict):
            return sorted(int(module_address) for module_address in modules)
        return sorted(int(module_address) for module_address in (modules or []))

    def get_module_info(self, address: Optional[int] = None, **kwargs):
        """Return one module snapshot or all scanned module snapshots."""
        if address is not None:
            return self._call_backend_method("get_module_info", int(address), **kwargs)

        module_info = {}
        for module_address in self._resolve_module_addresses(
            timeout_s=kwargs.get("timeout_s")
        ):
            module_info[module_address] = self.get_module_info(module_address, **kwargs)
        return module_info

    def get_module_current(
        self,
        address: Optional[int] = None,
        *,
        timeout_s: Optional[float] = None,
    ):
        """Return one module current or all scanned module currents."""
        if address is not None:
            return self._call_backend_method(
                "get_module_current",
                int(address),
                timeout_s=timeout_s,
            )

        module_currents = {}
        for module_address in self._resolve_module_addresses(timeout_s=timeout_s):
            status, current, meas_range = self._call_backend_method(
                "get_module_current",
                module_address,
                timeout_s=timeout_s,
            )
            module_currents[module_address] = {
                "status": status,
                "current": current,
                "meas_range": meas_range,
            }
        return module_currents
