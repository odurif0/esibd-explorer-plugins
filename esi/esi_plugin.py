"""Control the CGC ESI source and monitor its two HV supply modules."""

from __future__ import annotations

import contextlib
import hashlib
import importlib
import importlib.util
import sys
import time
from pathlib import Path
from typing import Any, cast

import numpy as np

from esibd.core import (
    PARAMETERTYPE,
    PLUGINTYPE,
    PRINT,
    Channel,
    DeviceController,
    Parameter,
    parameterDict,
)
from esibd.plugins import Device, Plugin


_RUNTIME_PREFIX = "_esibd_bundled_esi_runtime"
_ESI_DRIVER_CLASS: type[Any] | None = None
_ESI_MAX_VOLTAGE = 3000.0
_ESI_MODULES = (2, 3)
_ESI_COMMUNICATION_LOST = "Communication lost"


def _coerce_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
        return default
    return default if value is None else bool(value)


def _runtime_module_name(plugin_dir: Path) -> str:
    digest = hashlib.sha256(str(plugin_dir.resolve()).encode()).hexdigest()[:12]
    return f"{_RUNTIME_PREFIX}_{digest}"


def _load_runtime_package(name: str, runtime_dir: Path) -> None:
    if name in sys.modules:
        return
    spec = importlib.util.spec_from_file_location(
        name,
        runtime_dir / "__init__.py",
        submodule_search_locations=[str(runtime_dir)],
    )
    if spec is None or spec.loader is None:
        raise ModuleNotFoundError("Could not create the bundled ESI runtime package.")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(name, None)
        raise


def _get_esi_driver_class() -> type[Any]:
    """Load the ESI driver lazily from this plugin's private runtime."""
    global _ESI_DRIVER_CLASS
    if _ESI_DRIVER_CLASS is not None:
        return _ESI_DRIVER_CLASS
    plugin_dir = Path(__file__).resolve().parent
    runtime_dir = plugin_dir / "vendor" / "runtime"
    if not (runtime_dir / "__init__.py").is_file():
        raise ModuleNotFoundError(
            "Bundled ESI runtime not found in vendor/runtime; installation is incomplete."
        )
    runtime_name = _runtime_module_name(plugin_dir)
    _load_runtime_package(runtime_name, runtime_dir)
    module = importlib.import_module(f"{runtime_name}.esi")
    _ESI_DRIVER_CLASS = cast(type[Any], module.ESI)
    return _ESI_DRIVER_CLASS


def _fixed_channel_items(device_name: str) -> list[dict[str, Any]]:
    """Return the stable two-channel ESI layout used by configuration sync."""
    return [
        {
            "Name": f"{device_name}_HV{address}",
            "Module": address,
            "Enabled": False,
            "Active": True,
            "Real": True,
            "Value": 0.0,
            "Min": 0.0,
            "Max": _ESI_MAX_VOLTAGE,
            "Display": True,
        }
        for address in _ESI_MODULES
    ]


def providePlugins() -> "list[type[Plugin]]":
    return [ESIDevice]


class ESIDevice(Device):
    """Two-channel electrospray high-voltage controller."""

    documentation = (
        "Controls CGC ESI HV modules 2 and 3 and monitors voltage, current, "
        "interlocks, controller health, and module presence."
    )
    name = "ESI"
    version = "0.1.0"
    supportedVersion = "1.0.1"
    pluginType = PLUGINTYPE.INPUTDEVICE
    unit = "V"
    useMonitors = True
    useOnOffLogic = True
    iconFile = "esi.png"
    channels: "list[ESIChannel]"

    COM = "COM"
    BAUDRATE = "Baud rate"
    CONNECT_TIMEOUT = "Connect timeout (s)"
    POLL_TIMEOUT = "Poll timeout (s)"
    RAMP_RATE = "Ramp rate (V/s)"
    ALLOW_NEGATIVE = "Allow negative voltage"
    STATE = "State"
    INTERLOCK = "Interlock"
    MODULES = "Modules"

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.channelType = ESIChannel

    def initGUI(self) -> None:
        super().initGUI()
        self.controller = ESIController(controllerParent=self)

    def getChannels(self) -> "list[ESIChannel]":
        return cast("list[ESIChannel]", super().getChannels())

    def getDefaultSettings(self) -> dict[str, dict]:
        settings = super().getDefaultSettings()
        settings[f"{self.name}/{self.COM}"] = parameterDict(
            value=14,
            minimum=1,
            maximum=255,
            toolTip="Windows COM port number used by the ESI controller.",
            parameterType=PARAMETERTYPE.INT,
            attr="com",
        )
        settings[f"{self.name}/{self.BAUDRATE}"] = parameterDict(
            value=230400,
            minimum=1,
            maximum=1_000_000,
            toolTip="Vendor controller baud rate.",
            parameterType=PARAMETERTYPE.INT,
            attr="baudrate",
        )
        settings[f"{self.name}/{self.CONNECT_TIMEOUT}"] = parameterDict(
            value=5.0,
            minimum=1.0,
            maximum=30.0,
            toolTip="Timeout for connection, identity validation, and safe OFF.",
            parameterType=PARAMETERTYPE.FLOAT,
            attr="connect_timeout_s",
        )
        settings[f"{self.name}/{self.POLL_TIMEOUT}"] = parameterDict(
            value=3.0,
            minimum=0.5,
            maximum=30.0,
            toolTip="Timeout for one diagnostic and readback snapshot.",
            parameterType=PARAMETERTYPE.FLOAT,
            attr="poll_timeout_s",
        )
        settings[f"{self.name}/{self.RAMP_RATE}"] = parameterDict(
            value=500.0,
            minimum=0.0,
            maximum=_ESI_MAX_VOLTAGE,
            toolTip=(
                "Software ramp rate for normal target changes and ON/OFF "
                "transitions. Set to 0 for an immediate change."
            ),
            parameterType=PARAMETERTYPE.FLOAT,
            attr="ramp_rate_v_s",
        )
        settings[f"{self.name}/{self.ALLOW_NEGATIVE}"] = parameterDict(
            value=False,
            toolTip=(
                "Expert setting. Permit negative targets down to -3000 V only after "
                "confirming the installed HVPS-3kB polarity."
            ),
            parameterType=PARAMETERTYPE.BOOL,
            attr="allow_negative",
            advanced=True,
        )
        for label, attr, tooltip in (
            (self.STATE, "main_state", "Latest ESI controller state."),
            (self.INTERLOCK, "interlock_state", "Latest ESI interlock flags."),
            (self.MODULES, "detected_modules", "Detected module addresses and types."),
        ):
            settings[f"{self.name}/{label}"] = parameterDict(
                value="Disconnected" if label == self.STATE else "n/a",
                toolTip=tooltip,
                parameterType=PARAMETERTYPE.LABEL,
                attr=attr,
                indicator=True,
                internal=True,
                restore=False,
            )
        settings[f"{self.name}/Interval"][Parameter.VALUE] = 1000
        settings[f"{self.name}/{self.MAXDATAPOINTS}"][Parameter.VALUE] = 100000
        return settings

    def ensureFixedChannels(self) -> None:
        """Replace only the generic bootstrap layout with modules 2 and 3."""
        channels = self.getChannels()
        existing_modules = [getattr(channel, "module", None) for channel in channels]
        if existing_modules == list(_ESI_MODULES):
            return
        if channels and not all(
            str(getattr(channel, "name", "")).startswith(self.name)
            for channel in channels
        ):
            self.print(
                "Keeping the existing ESI channel configuration; expected module "
                "addresses are 2 and 3.",
                flag=PRINT.WARNING,
            )
            return
        update = getattr(self, "updateChannelConfig", None)
        custom_file = getattr(self, "customConfigFile", None)
        if not callable(update) or not callable(custom_file):
            return
        update(_fixed_channel_items(self.name), custom_file(self.confINI))

    def closeCommunication(self) -> None:
        controller = getattr(self, "controller", None)
        if controller is not None:
            controller.shutdownCommunication()
        super().closeCommunication()


class ESIChannel(Channel):
    """One HVPS-3kB module at ESI address 2 or 3."""

    MODULE = "Module"
    channelParent: ESIDevice

    def getDefaultChannel(self) -> dict[str, dict]:
        self.module: int
        channel = super().getDefaultChannel()
        channel[self.VALUE][Parameter.HEADER] = "Voltage (V)"
        channel[self.VALUE][Parameter.MIN] = 0.0
        channel[self.VALUE][Parameter.MAX] = _ESI_MAX_VOLTAGE
        channel[self.ENABLED][Parameter.HEADER] = "HV On"
        channel[self.MODULE] = parameterDict(
            value=2,
            minimum=2,
            maximum=3,
            toolTip="Fixed CGC ESI HV module address (2 or 3).",
            parameterType=PARAMETERTYPE.INT,
            attr="module",
            header="Module",
            advanced=False,
        )
        return channel

    def setDisplayedParameters(self) -> None:
        super().setDisplayedParameters()
        self.displayedParameters.append(self.MODULE)

    def module_address(self) -> int:
        return int(self.module)


class ESIController(DeviceController):
    """ESIBD bridge for the timeout-safe ESI runtime."""

    controllerParent: ESIDevice

    def __init__(self, controllerParent) -> None:
        super().__init__(controllerParent=controllerParent)
        self.device: Any | None = None
        self.values: dict[int, float] | None = None
        self.currents: dict[int, float] = {}
        self.initialized = False
        self.main_state = "Disconnected"
        self.identity: dict[str, Any] = {}

    def runInitialization(self) -> None:
        self.initialized = False
        self._dispose_device()
        try:
            driver = _get_esi_driver_class()
            self.device = driver(
                device_id=f"esi_com{int(self.controllerParent.com)}",
                com=int(self.controllerParent.com),
                baudrate=int(self.controllerParent.baudrate),
                allow_negative=_coerce_bool(self.controllerParent.allow_negative),
                process_backend=True,
            )
            self.device.connect(timeout_s=float(self.controllerParent.connect_timeout_s))
            self.identity = self.device.collect_identity(
                timeout_s=float(self.controllerParent.poll_timeout_s)
            )
            snapshot = self.device.collect_diagnostics(
                timeout_s=float(self.controllerParent.poll_timeout_s)
            )
            self._apply_snapshot(snapshot)
            self.signalComm.initCompleteSignal.emit()
        except Exception as exc:
            self.print(f"ESI initialization failed: {exc}", flag=PRINT.ERROR)
            self._dispose_device()
        finally:
            self.initializing = False

    def initComplete(self) -> None:
        self.controllerParent.ensureFixedChannels()
        self.initializeValues(reset=True)
        self.initialized = self.device is not None
        with contextlib.suppress(AttributeError):
            super().initComplete()
        if self.initialized:
            self.print(
                "ESI initialized with all HV outputs forced OFF. "
                "Use the explicit ON controls to energize modules 2 and 3."
            )

    def initializeValues(self, reset: bool = False) -> None:
        if self.values is None or reset:
            self.values = {address: np.nan for address in _ESI_MODULES}
            self.currents = {address: np.nan for address in _ESI_MODULES}

    def readNumbers(self) -> None:
        if self.device is None or not self.initialized:
            self.initializeValues(reset=True)
            return
        try:
            snapshot = self.device.collect_diagnostics(
                timeout_s=float(self.controllerParent.poll_timeout_s)
            )
            self._apply_snapshot(snapshot)
        except Exception as exc:
            self.errorCount += 1
            self.main_state = _ESI_COMMUNICATION_LOST
            self._sync_status()
            self.initializeValues(reset=True)
            self.print(
                "ESI readback failed; the transport state is unknown and HV may "
                f"remain energized: {exc}",
                flag=PRINT.ERROR,
            )

    def fakeNumbers(self) -> None:
        self.initializeValues(reset=True)

    def applyValue(self, channel: ESIChannel) -> None:
        if self.device is None or not self.initialized or not self.controllerParent.isOn():
            return
        target = float(channel.value if channel.enabled else 0.0)
        try:
            current = self.values.get(channel.module_address(), 0.0) if self.values else 0.0
            if not np.isfinite(current):
                current = 0.0
            self._ramp_target(channel.module_address(), float(current), target)
        except Exception as exc:
            self.errorCount += 1
            self.print(
                f"ESI rejected {target:g} V for module {channel.module_address()}: {exc}",
                flag=PRINT.ERROR,
            )

    def updateValues(self) -> None:
        if self.values is None:
            return
        for channel in self.controllerParent.getChannels():
            channel.monitor = (
                self.values.get(channel.module_address(), np.nan)
                if channel.enabled and channel.real
                else np.nan
            )

    def toggleOn(self) -> None:
        super().toggleOn()
        if self.device is None:
            return
        timeout = float(self.controllerParent.connect_timeout_s)
        try:
            if self.controllerParent.isOn():
                # Start from zero before activation; only enabled channels are energized.
                for address in _ESI_MODULES:
                    self.device.set_target_voltage(address, 0.0, timeout_s=timeout)
                self.device.set_global_active(True, timeout_s=timeout)
                for channel in self.controllerParent.getChannels():
                    self.device.set_output_active(
                        channel.module_address(), bool(channel.enabled), timeout_s=timeout
                    )
                for channel in self.controllerParent.getChannels():
                    self.applyValue(channel)
            else:
                for channel in self.controllerParent.getChannels():
                    address = channel.module_address()
                    current = self.values.get(address, channel.value) if self.values else channel.value
                    if not np.isfinite(current):
                        current = channel.value
                    self._ramp_target(address, float(current), 0.0)
                self.device.force_safe_off(timeout_s=timeout)
        except Exception as exc:
            self.errorCount += 1
            self.print(
                "ESI ON/OFF transition failed. HV state is not confirmed; verify "
                f"the hardware and interlock before approaching the source: {exc}",
                flag=PRINT.ERROR,
            )

    def _ramp_target(self, address: int, start: float, target: float) -> None:
        """Apply one normal setpoint transition in bounded 100 ms steps."""
        device = self.device
        if device is None:
            return
        rate = max(0.0, float(getattr(self.controllerParent, "ramp_rate_v_s", 0.0)))
        delta = float(target) - float(start)
        if rate == 0.0 or delta == 0.0:
            device.set_target_voltage(
                address,
                float(target),
                timeout_s=float(self.controllerParent.poll_timeout_s),
            )
            return

        step_interval_s = 0.1
        steps = max(1, int(np.ceil(abs(delta) / (rate * step_interval_s))))
        for step in range(1, steps + 1):
            value = float(start) + delta * step / steps
            device.set_target_voltage(
                address,
                value,
                timeout_s=float(self.controllerParent.poll_timeout_s),
            )
            if step < steps:
                time.sleep(step_interval_s)

    def shutdownCommunication(self) -> bool:
        device = self.device
        if device is None:
            return True
        confirmed = False
        try:
            device.disconnect(timeout_s=float(self.controllerParent.connect_timeout_s))
            confirmed = True
        except Exception as exc:
            self.print(
                "ESI shutdown could not be confirmed. HV may remain energized; "
                f"use the hardware interlock/front panel: {exc}",
                flag=PRINT.ERROR,
            )
        finally:
            self._dispose_device()
            self.initialized = False
            self.main_state = "Disconnected" if confirmed else "Shutdown unconfirmed"
            self._sync_status()
        return confirmed

    def closeCommunication(self) -> None:
        with contextlib.suppress(AttributeError):
            super().closeCommunication()
        self.shutdownCommunication()

    def _apply_snapshot(self, snapshot: dict[str, Any]) -> None:
        self.main_state = str(snapshot["main_state"]["name"])
        self.values = {}
        self.currents = {}
        for address, module in snapshot["modules"].items():
            address = int(address)
            self.values[address] = (
                float(module["measured_v"]) if module["voltage_valid"] else np.nan
            )
            self.currents[address] = (
                float(module["measured_a"]) if module["current_valid"] else np.nan
            )
        self.controllerParent.main_state = self.main_state
        flags = snapshot["interlock_state"]["flags"]
        self.controllerParent.interlock_state = ", ".join(flags) if flags else "OK"
        module_identity = self.identity.get("modules", {})
        labels = []
        for address in _ESI_MODULES:
            info = module_identity.get(address, module_identity.get(str(address), {}))
            product_id = info.get("product_id") if isinstance(info, dict) else None
            label = product_id if isinstance(product_id, str) and product_id else "HVPS-3kB"
            labels.append(f"{address}: {label}")
        self.controllerParent.detected_modules = ", ".join(labels)
        self._sync_status()

    def _sync_status(self) -> None:
        self.controllerParent.main_state = self.main_state

    def _dispose_device(self) -> None:
        device = self.device
        self.device = None
        if device is not None:
            with contextlib.suppress(Exception):
                device.close()
