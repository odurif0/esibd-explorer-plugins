"""Shared helpers for high-level CGC instrument drivers."""

from __future__ import annotations

import functools
import logging
import queue
import sys
import threading
import time
import warnings
import weakref
from datetime import datetime
from pathlib import Path
from typing import Optional

from ._controller_process import ControllerProcessProxy

RUNTIME_IS_WINDOWS = sys.platform.startswith("win")


class DeviceLoggerAdapter(logging.LoggerAdapter):
    """Prefix log messages with the device identifier."""

    def process(self, msg, kwargs):
        return f"{self.extra['device_id']} - {msg}", kwargs


def build_device_logger(
    *,
    instrument_name: str,
    device_id: str,
    logger: Optional[logging.Logger],
    log_dir: Optional[Path],
    source_file: str,
):
    """Return an injected logger adapter or create a file-backed device logger."""
    if logger is not None:
        return DeviceLoggerAdapter(logger, {"device_id": device_id})

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    logger_name = f"{instrument_name}_{device_id}_{timestamp}"
    device_logger = logging.getLogger(logger_name)

    if not device_logger.handlers:
        root_log_dir = (
            Path(log_dir)
            if log_dir is not None
            else Path(source_file).resolve().parents[3] / "logs"
        )
        root_log_dir.mkdir(parents=True, exist_ok=True)
        log_file = root_log_dir / f"{instrument_name.lower()}_{device_id}_{timestamp}.log"
        handler = logging.FileHandler(log_file)
        formatter = logging.Formatter(
            f"%(asctime)s - {device_id} - %(levelname)s - %(message)s"
        )
        handler.setFormatter(formatter)
        device_logger.addHandler(handler)
        device_logger.setLevel(logging.INFO)

    return device_logger


def supports_process_backend(*shared_objects) -> bool:
    """Return True when the current runtime can isolate the controller process."""
    return RUNTIME_IS_WINDOWS and all(value is None for value in shared_objects)


class TimeoutSafeDllMixin:
    """Shared timeout-safe serialization for vendor DLL calls."""

    _INSTRUMENT_NAME = "CGC"

    def _on_transport_poisoned(self) -> None:
        """Hook for instrument-specific cleanup after a timed-out DLL call."""

    def _raise_if_transport_poisoned(self):
        if self._transport_poisoned:
            detail = self._transport_error or "unknown transport failure"
            raise RuntimeError(
                f"{self._INSTRUMENT_NAME} transport is unusable after a timed-out DLL call. "
                f"{detail} Recreate the {self._INSTRUMENT_NAME} instance before retrying."
            )

    def _poison_transport(self, step_name: str):
        self._transport_poisoned = True
        self._transport_error = (
            f"Timed out during '{step_name}'. "
            "The device may be powered off or unresponsive."
        )
        self.connected = False
        self._on_transport_poisoned()

    def _call_locked_with_timeout(self, method, timeout_s, step_name, *args, **kwargs):
        self._raise_if_transport_poisoned()
        lock_deadline = time.monotonic() + timeout_s
        while True:
            remaining = lock_deadline - time.monotonic()
            if remaining <= 0:
                self._raise_if_transport_poisoned()
                raise RuntimeError(
                    f"{self._INSTRUMENT_NAME} transport lock timed out during '{step_name}'. "
                    "A previous DLL call may still be blocked."
                )
            if self.thread_lock.acquire(timeout=min(0.1, remaining)):
                break
            self._raise_if_transport_poisoned()

        result_queue = queue.Queue(maxsize=1)
        release_lock = True

        def runner():
            try:
                result_queue.put(("result", method(*args, **kwargs)))
            except Exception as exc:  # pragma: no cover - forwarded to caller
                result_queue.put(("error", exc))

        thread = threading.Thread(target=runner, daemon=True)
        thread.start()
        thread.join(timeout_s)

        try:
            if thread.is_alive():
                self._poison_transport(step_name)
                release_lock = False
                raise RuntimeError(
                    f"{self._INSTRUMENT_NAME} DLL call timed out during '{step_name}'. "
                    "The device may be powered off or unresponsive. "
                    f"The {self._INSTRUMENT_NAME} instance is now marked unusable."
                )

            kind, payload = result_queue.get()
            if kind == "error":
                raise payload
            return payload
        finally:
            if release_lock:
                self.thread_lock.release()

    def _call_locked(self, method, *args, **kwargs):
        self._raise_if_transport_poisoned()
        while True:
            if self.thread_lock.acquire(timeout=0.1):
                try:
                    self._raise_if_transport_poisoned()
                    return method(*args, **kwargs)
                finally:
                    self.thread_lock.release()
            self._raise_if_transport_poisoned()


class DllPortClaimRegistryMixin:
    """Track DLL port claims across instances in the same Python process."""

    _INSTRUMENT_NAME = "CGC"
    _active_connections_lock = threading.Lock()
    _active_connections: dict[int, dict[str, object]] = {}

    @classmethod
    def _purge_stale_connections(cls):
        stale = []
        for instance_id, entry in cls._active_connections.items():
            instance = entry["ref"]()
            if instance is None or not instance._dll_port_claimed:
                stale.append(instance_id)
        for instance_id in stale:
            cls._active_connections.pop(instance_id, None)

    def _register_connected_instance(self):
        cls = type(self)
        with cls._active_connections_lock:
            cls._purge_stale_connections()
            cls._active_connections[id(self)] = {
                "ref": weakref.ref(self),
                "device_id": self.device_id,
                "com": self.com,
                "port": self.port_num,
            }

    def _unregister_connected_instance(self):
        cls = type(self)
        with cls._active_connections_lock:
            cls._active_connections.pop(id(self), None)
            cls._purge_stale_connections()

    def _set_port_claimed(self, claimed: bool):
        self._dll_port_claimed = bool(claimed)
        if self._dll_port_claimed:
            self._register_connected_instance()
        else:
            self._unregister_connected_instance()

    def _warn_on_other_process_ports(self):
        cls = type(self)
        with cls._active_connections_lock:
            cls._purge_stale_connections()
            others = [
                entry
                for instance_id, entry in cls._active_connections.items()
                if instance_id != id(self)
            ]

        if not others:
            return

        same_port = [entry for entry in others if entry["port"] == self.port_num]
        if same_port:
            other_devices = ", ".join(
                f"{entry['device_id']}@COM{entry['com']}" for entry in same_port
            )
            self.logger.warning(
                f"Another {self._INSTRUMENT_NAME} instance in this process already claims "
                f"the same DLL port {self.port_num}: {other_devices}. Reusing the same "
                "DLL port can reassign or close the shared vendor channel unexpectedly."
            )
            return

        all_ports = sorted({entry["port"] for entry in others} | {self.port_num})
        other_devices = ", ".join(
            f"{entry['device_id']}@COM{entry['com']}/port{entry['port']}"
            for entry in others
        )
        self.logger.warning(
            f"Multiple {self._INSTRUMENT_NAME} instances in this process currently claim "
            f"DLL ports {all_ports}: {other_devices}. The vendor DLL exposes "
            "independent ports, but keep one active instance per port in each workflow."
        )


class ProcessIsolatedClientMixin:
    """Shared public facade for process-isolated high-level controllers."""

    _INSTRUMENT_NAME = "CGC"
    _PROCESS_CONTROLLER_CLASS = None
    _PROCESS_CONTROLLER_PATH = ""
    _PROCESS_ATTR_TIMEOUT_S = 5.0
    _PROCESS_STARTUP_TIMEOUT_S = 30.0
    _PROCESS_CALL_TIMEOUT_S = 30.0
    _PROCESS_TIMEOUT_RULES = {
        "connect": (4.0, 5.0, 15.0),
    }
    _LOCAL_ATTRS = frozenset(
        {
            "_backend",
            "_backend_mode",
            "_process_backend_disabled_reason",
        }
    )

    def _initialize_process_backend(
        self,
        *,
        backend_kwargs: dict[str, object],
        incompatible_objects: dict[str, object],
        allow_process_backend: bool = True,
        process_backend_disabled_reason: str = "",
    ) -> None:
        object.__setattr__(self, "_process_backend_disabled_reason", "")

        if allow_process_backend and supports_process_backend(*incompatible_objects.values()):
            process_kwargs = dict(backend_kwargs)
            if "logger" in process_kwargs:
                process_kwargs["logger"] = None
            try:
                backend = ControllerProcessProxy(
                    self._PROCESS_CONTROLLER_PATH,
                    process_kwargs,
                    label=f"{self._INSTRUMENT_NAME} {backend_kwargs['device_id']}",
                    startup_timeout_s=self._PROCESS_STARTUP_TIMEOUT_S,
                )
            except Exception as exc:
                object.__setattr__(
                    self,
                    "_process_backend_disabled_reason",
                    f"{self._INSTRUMENT_NAME} process isolation startup failed; "
                    f"falling back to inline controller: {exc}",
                )
                warnings.warn(
                    self._process_backend_disabled_reason,
                    RuntimeWarning,
                    stacklevel=2,
                )
            else:
                object.__setattr__(self, "_backend_mode", "process")
                object.__setattr__(self, "_backend", backend)
                return

        if not allow_process_backend:
            reason = process_backend_disabled_reason.strip()
            if reason:
                object.__setattr__(
                    self,
                    "_process_backend_disabled_reason",
                    reason,
                )
        elif RUNTIME_IS_WINDOWS and any(
            value is not None for value in incompatible_objects.values()
        ):
            incompatible = [
                name
                for name, value in incompatible_objects.items()
                if value is not None
            ]
            object.__setattr__(
                self,
                "_process_backend_disabled_reason",
                f"{self._INSTRUMENT_NAME} process isolation is disabled because "
                f"{', '.join(incompatible)} cannot be shared with the worker process.",
            )
            warnings.warn(
                self._process_backend_disabled_reason,
                RuntimeWarning,
                stacklevel=2,
            )

        object.__setattr__(self, "_backend_mode", "inline")
        object.__setattr__(self, "_backend", self._PROCESS_CONTROLLER_CLASS(**backend_kwargs))

    def __getattr__(self, name):
        backend = object.__getattribute__(self, "_backend")
        backend_mode = object.__getattribute__(self, "_backend_mode")

        if backend_mode == "inline":
            return getattr(backend, name)

        controller_attr = getattr(self._PROCESS_CONTROLLER_CLASS, name, None)
        if callable(controller_attr) and not name.startswith("_"):
            return functools.partial(self._call_process_method, name)

        return backend.get_attribute(name, timeout_s=self._PROCESS_ATTR_TIMEOUT_S)

    def __setattr__(self, name, value):
        if name in self._LOCAL_ATTRS or "_backend" not in self.__dict__:
            object.__setattr__(self, name, value)
            return

        backend = object.__getattribute__(self, "_backend")
        backend_mode = object.__getattribute__(self, "_backend_mode")

        if backend_mode == "inline":
            setattr(backend, name, value)
            return

        backend.set_attribute(name, value, timeout_s=self._PROCESS_ATTR_TIMEOUT_S)

    def _call_process_method(self, method_name, *args, **kwargs):
        backend = object.__getattribute__(self, "_backend")
        return backend.call_method(
            method_name,
            *args,
            rpc_timeout_s=self._rpc_timeout_for(method_name, kwargs),
            **kwargs,
        )

    @classmethod
    def _rpc_timeout_for(cls, method_name: str, kwargs) -> float:
        timeout_s = kwargs.get("timeout_s")
        if not isinstance(timeout_s, (int, float)) or timeout_s <= 0:
            return cls._PROCESS_CALL_TIMEOUT_S

        multiplier, additive, minimum = cls._PROCESS_TIMEOUT_RULES.get(
            method_name,
            (4.0, 0.0, cls._PROCESS_CALL_TIMEOUT_S),
        )
        return max(float(minimum), float(timeout_s) * float(multiplier) + float(additive))

    def close(self):
        """Release worker-process resources when process isolation is enabled."""
        if "_backend_mode" not in self.__dict__ or "_backend" not in self.__dict__:
            return
        if object.__getattribute__(self, "_backend_mode") == "process":
            object.__getattribute__(self, "_backend").close()

    def __del__(self):  # pragma: no cover - best effort cleanup
        try:
            self.close()
        except Exception:
            pass
