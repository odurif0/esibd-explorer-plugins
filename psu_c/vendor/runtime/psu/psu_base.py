"""PSU (Power Supply Unit) low-level CGC driver."""

from __future__ import annotations

import ctypes
import json
import sys
from ctypes import wintypes
from pathlib import Path


class PSUPlatformError(RuntimeError):
    """Raised when the PSU driver is used on an unsupported platform."""


class PSUDllLoadError(RuntimeError):
    """Raised when the vendor PSU DLL cannot be loaded."""


def _python_is_64bit() -> bool:
    return ctypes.sizeof(ctypes.c_void_p) >= 8


def _format_dll_load_hint(dll_path: Path) -> str:
    if dll_path.parent.name.lower() == "x64" and not _python_is_64bit():
        return (
            " The bundled DLL is 64-bit, but this Python interpreter is 32-bit. "
            "Use 64-bit Python on Windows or pass a compatible 32-bit dll_path."
        )
    return ""


def _decode_vendor_text(value: bytes) -> str:
    return value.decode(errors="replace")


class PSUBase:
    """Low-level CGC PSU driver backed by the vendor DLL."""

    WIN_BOOL = wintypes.BOOL

    NO_ERR = 0
    ERR_PORT_RANGE = -1
    ERR_OPEN = -2
    ERR_CLOSE = -3
    ERR_PURGE = -4
    ERR_CONTROL = -5
    ERR_STATUS = -6
    ERR_COMMAND_SEND = -7
    ERR_DATA_SEND = -8
    ERR_TERM_SEND = -9
    ERR_COMMAND_RECEIVE = -10
    ERR_DATA_RECEIVE = -11
    ERR_TERM_RECEIVE = -12
    ERR_COMMAND_WRONG = -13
    ERR_ARGUMENT_WRONG = -14
    ERR_ARGUMENT = -15
    ERR_RATE = -16
    ERR_NOT_CONNECTED = -100
    ERR_NOT_READY = -101
    ERR_READY = -102
    ERR_DEBUG_OPEN = -400
    ERR_DEBUG_CLOSE = -401

    MAIN_STATE = {
        0x0000: "STATE_ON",
        0x8000: "STATE_ERROR",
        0x8001: "STATE_ERR_VSUP",
        0x8002: "STATE_ERR_TEMP_LOW",
        0x8003: "STATE_ERR_TEMP_HIGH",
        0x8004: "STATE_ERR_ILOCK",
        0x8005: "STATE_ERR_PSU_DIS",
    }

    DEVICE_STATE = {
        (1 << 0x00): "DEVST_VCPU_FAIL",
        (1 << 0x01): "DEVST_VFAN_FAIL",
        (1 << 0x02): "DEVST_VPSU0_FAIL",
        (1 << 0x03): "DEVST_VPSU1_FAIL",
        (1 << 0x08): "DEVST_FAN1_FAIL",
        (1 << 0x09): "DEVST_FAN2_FAIL",
        (1 << 0x0A): "DEVST_FAN3_FAIL",
        (1 << 0x0F): "DEVST_PSU_DIS",
        (1 << 0x10): "DEVST_SEN1_HIGH",
        (1 << 0x11): "DEVST_SEN2_HIGH",
        (1 << 0x12): "DEVST_SEN3_HIGH",
        (1 << 0x18): "DEVST_SEN1_LOW",
        (1 << 0x19): "DEVST_SEN2_LOW",
        (1 << 0x1A): "DEVST_SEN3_LOW",
    }

    PSU_STATE_PSU0_ENB_CTRL = 1 << 4
    PSU_STATE_PSU1_ENB_CTRL = 1 << 5
    PSU_STATE_PSU0_FULL_CTRL = 1 << 6
    PSU_STATE_PSU1_FULL_CTRL = 1 << 7
    PSU_STATE_ILIM_ACT = 1 << 12
    PSU_STATE_PSU0_FULL_ACT = 1 << 13
    PSU_STATE_PSU1_FULL_ACT = 1 << 14
    PSU_STATE_PSU0_ENB_ACT = 1 << 20
    PSU_STATE_PSU1_ENB_ACT = 1 << 21

    PSU_POS = 0
    PSU_NEG = 1
    PSU_NUM = 2
    SENSOR_NUM = 3
    FAN_NUM = 3
    MAX_PORT = 16
    MAX_CONFIG = 168
    CONFIG_NAME_SIZE = 75
    FW_DATE_SIZE = 16
    PRODUCT_ID_SIZE = 60

    def __init__(
        self,
        com: int,
        port: int = 0,
        log=None,
        idn: str = "",
        dll_path: str | Path | None = None,
        error_codes_path: str | Path | None = None,
    ):
        class_dir = Path(__file__).resolve().parent

        if not sys.platform.startswith("win"):
            raise PSUPlatformError(
                "CGC PSU is supported only on Windows because it depends on "
                "COM-HVPSU2D.dll."
            )

        self.psu_dll_path = Path(dll_path) if dll_path is not None else (
            class_dir / "vendor" / "x64" / "COM-HVPSU2D.dll"
        )
        try:
            self.psu_dll = ctypes.WinDLL(str(self.psu_dll_path))
        except OSError as exc:
            raise PSUDllLoadError(
                f"Unable to load CGC PSU DLL from '{self.psu_dll_path}': {exc}."
                f"{_format_dll_load_hint(self.psu_dll_path)}"
            ) from exc

        err_path = Path(error_codes_path) if error_codes_path is not None else (
            class_dir.parent / "error_codes.json"
        )
        with err_path.open("rb") as f:
            self.err_dict = json.load(f)

        self.com = int(com)
        self.port = int(port)
        self.log = log
        self.idn = idn

    def describe_error(self, status: int) -> str:
        """Return the vendor message for a driver status code."""
        return self.err_dict.get(str(status), "Unknown status code")

    def format_status(self, status: int) -> str:
        """Return a compact '<code> (<message>)' representation."""
        return f"{status} ({self.describe_error(status)})"

    def _validate_channel(self, channel: int) -> int:
        channel = int(channel)
        if channel not in (self.PSU_POS, self.PSU_NEG):
            raise ValueError(
                f"Invalid PSU channel: {channel}. Expected 0 or 1."
            )
        return channel

    def _validate_config_number(self, config_number: int) -> int:
        config_number = int(config_number)
        if not 0 <= config_number < self.MAX_CONFIG:
            raise ValueError(
                f"Invalid PSU config number: {config_number}. "
                f"Expected 0 <= config < {self.MAX_CONFIG}."
            )
        return config_number

    def open_port(self, com_number: int, port_number: int | None = None) -> int:
        """Open the communication link to the PSU."""
        if port_number is None:
            port_number = self.port
        status = self.psu_dll.COM_HVPSU2D_Open(int(port_number), int(com_number))
        return status

    def close_port(self) -> int:
        """Close the communication link."""
        return self.psu_dll.COM_HVPSU2D_Close(self.port)

    def set_baud_rate(self, baud_rate: int):
        """Set communication speed and return the negotiated baud rate."""
        baud_rate_ref = ctypes.c_uint32(int(baud_rate))
        status = self.psu_dll.COM_HVPSU2D_SetBaudRate(
            self.port, ctypes.byref(baud_rate_ref)
        )
        return status, baud_rate_ref.value

    def purge(self) -> int:
        """Clear data buffers for the port."""
        return self.psu_dll.COM_HVPSU2D_Purge(self.port)

    def device_purge(self):
        """Clear the device output data buffer."""
        empty = self.WIN_BOOL()
        status = self.psu_dll.COM_HVPSU2D_DevicePurge(
            self.port, ctypes.byref(empty)
        )
        return status, bool(empty.value)

    def get_buffer_state(self):
        """Return whether the device input buffer is empty."""
        empty = self.WIN_BOOL()
        status = self.psu_dll.COM_HVPSU2D_GetBufferState(
            self.port, ctypes.byref(empty)
        )
        return status, bool(empty.value)

    def get_main_state(self):
        """Get the main PSU state."""
        state = ctypes.c_uint16()
        status = self.psu_dll.COM_HVPSU2D_GetMainState(
            self.port, ctypes.byref(state)
        )
        state_value = state.value
        return (
            status,
            hex(state_value),
            self.MAIN_STATE.get(state_value, f"UNKNOWN_STATE_0x{state_value:04X}"),
        )

    def get_device_state(self):
        """Get the detailed PSU state bitfield."""
        device_state = ctypes.c_uint32()
        status = self.psu_dll.COM_HVPSU2D_GetDeviceState(
            self.port, ctypes.byref(device_state)
        )
        state_value = device_state.value
        active_states = []
        if state_value == 0:
            active_states.append("DEVST_OK")
        else:
            for flag, name in self.DEVICE_STATE.items():
                if state_value & flag:
                    active_states.append(name)
        return status, hex(state_value), active_states

    def get_housekeeping(self):
        """Get the PSU housekeeping values."""
        volt_rect = ctypes.c_double()
        volt_5v0 = ctypes.c_double()
        volt_3v3 = ctypes.c_double()
        temp_cpu = ctypes.c_double()
        status = self.psu_dll.COM_HVPSU2D_GetHousekeeping(
            self.port,
            ctypes.byref(volt_rect),
            ctypes.byref(volt_5v0),
            ctypes.byref(volt_3v3),
            ctypes.byref(temp_cpu),
        )
        return status, volt_rect.value, volt_5v0.value, volt_3v3.value, temp_cpu.value

    def get_sensor_data(self):
        """Get the three PSU temperature sensor readings."""
        temperatures = (ctypes.c_double * self.SENSOR_NUM)()
        status = self.psu_dll.COM_HVPSU2D_GetSensorData(self.port, temperatures)
        return status, [temperatures[i] for i in range(self.SENSOR_NUM)]

    def get_fan_data(self):
        """Get the configured and measured fan values."""
        enabled = (self.WIN_BOOL * self.FAN_NUM)()
        failed = (self.WIN_BOOL * self.FAN_NUM)()
        set_rpm = (ctypes.c_uint16 * self.FAN_NUM)()
        measured_rpm = (ctypes.c_uint16 * self.FAN_NUM)()
        pwm = (ctypes.c_uint16 * self.FAN_NUM)()
        status = self.psu_dll.COM_HVPSU2D_GetFanData(
            self.port, enabled, failed, set_rpm, measured_rpm, pwm
        )
        return (
            status,
            [bool(enabled[i]) for i in range(self.FAN_NUM)],
            [bool(failed[i]) for i in range(self.FAN_NUM)],
            [int(set_rpm[i]) for i in range(self.FAN_NUM)],
            [int(measured_rpm[i]) for i in range(self.FAN_NUM)],
            [int(pwm[i]) for i in range(self.FAN_NUM)],
        )

    def get_led_data(self):
        """Get the PSU controller LED state."""
        red = self.WIN_BOOL()
        green = self.WIN_BOOL()
        blue = self.WIN_BOOL()
        status = self.psu_dll.COM_HVPSU2D_GetLEDData(
            self.port, ctypes.byref(red), ctypes.byref(green), ctypes.byref(blue)
        )
        return status, bool(red.value), bool(green.value), bool(blue.value)

    def get_adc_housekeeping(self, channel: int):
        """Get ADC housekeeping data for one PSU channel."""
        channel = self._validate_channel(channel)
        volt_avdd = ctypes.c_double()
        volt_dvdd = ctypes.c_double()
        volt_aldo = ctypes.c_double()
        volt_dldo = ctypes.c_double()
        volt_ref = ctypes.c_double()
        temp_adc = ctypes.c_double()
        status = self.psu_dll.COM_HVPSU2D_GetADCHousekeeping(
            self.port,
            channel,
            ctypes.byref(volt_avdd),
            ctypes.byref(volt_dvdd),
            ctypes.byref(volt_aldo),
            ctypes.byref(volt_dldo),
            ctypes.byref(volt_ref),
            ctypes.byref(temp_adc),
        )
        return (
            status,
            volt_avdd.value,
            volt_dvdd.value,
            volt_aldo.value,
            volt_dldo.value,
            volt_ref.value,
            temp_adc.value,
        )

    def get_psu_housekeeping(self, channel: int):
        """Get rail housekeeping data for one PSU channel."""
        channel = self._validate_channel(channel)
        volt_24vp = ctypes.c_double()
        volt_12vp = ctypes.c_double()
        volt_12vn = ctypes.c_double()
        volt_ref = ctypes.c_double()
        status = self.psu_dll.COM_HVPSU2D_GetPSUHousekeeping(
            self.port,
            channel,
            ctypes.byref(volt_24vp),
            ctypes.byref(volt_12vp),
            ctypes.byref(volt_12vn),
            ctypes.byref(volt_ref),
        )
        return status, volt_24vp.value, volt_12vp.value, volt_12vn.value, volt_ref.value

    def get_psu_data(self, channel: int):
        """Get measured output data for one PSU channel."""
        channel = self._validate_channel(channel)
        voltage = ctypes.c_double()
        current = ctypes.c_double()
        volt_dropout = ctypes.c_double()
        status = self.psu_dll.COM_HVPSU2D_GetPSUData(
            self.port,
            channel,
            ctypes.byref(voltage),
            ctypes.byref(current),
            ctypes.byref(volt_dropout),
        )
        return status, voltage.value, current.value, volt_dropout.value

    def get_device_enable(self):
        """Get the device enable state."""
        enable = self.WIN_BOOL()
        status = self.psu_dll.COM_HVPSU2D_GetDeviceEnable(
            self.port, ctypes.byref(enable)
        )
        return status, bool(enable.value)

    def set_device_enable(self, enable: bool) -> int:
        """Set the device enable state."""
        return self.psu_dll.COM_HVPSU2D_SetDeviceEnable(
            self.port, self.WIN_BOOL(int(bool(enable)))
        )

    def get_interlock_enable(self):
        """Get the interlock enable state for output and BNC connectors."""
        connector_output = self.WIN_BOOL()
        connector_bnc = self.WIN_BOOL()
        status = self.psu_dll.COM_HVPSU2D_GetInterlockEnable(
            self.port,
            ctypes.byref(connector_output),
            ctypes.byref(connector_bnc),
        )
        return status, bool(connector_output.value), bool(connector_bnc.value)

    def set_interlock_enable(self, connector_output: bool, connector_bnc: bool) -> int:
        """Set the interlock enable state for output and BNC connectors."""
        return self.psu_dll.COM_HVPSU2D_SetInterlockEnable(
            self.port,
            self.WIN_BOOL(int(bool(connector_output))),
            self.WIN_BOOL(int(bool(connector_bnc))),
        )

    def get_psu_enable(self):
        """Get the channel enable flags."""
        psu0 = self.WIN_BOOL()
        psu1 = self.WIN_BOOL()
        status = self.psu_dll.COM_HVPSU2D_GetPSUEnable(
            self.port, ctypes.byref(psu0), ctypes.byref(psu1)
        )
        return status, bool(psu0.value), bool(psu1.value)

    def set_psu_enable(self, psu0: bool, psu1: bool) -> int:
        """Set the channel enable flags."""
        return self.psu_dll.COM_HVPSU2D_SetPSUEnable(
            self.port,
            self.WIN_BOOL(int(bool(psu0))),
            self.WIN_BOOL(int(bool(psu1))),
        )

    def has_psu_full_range(self):
        """Return whether range switching is implemented for each channel."""
        psu0 = self.WIN_BOOL()
        psu1 = self.WIN_BOOL()
        status = self.psu_dll.COM_HVPSU2D_HasPSUFullRange(
            self.port, ctypes.byref(psu0), ctypes.byref(psu1)
        )
        return status, bool(psu0.value), bool(psu1.value)

    def set_psu_full_range(self, psu0: bool, psu1: bool) -> int:
        """Set the full-range state for both PSU channels."""
        return self.psu_dll.COM_HVPSU2D_SetPSUFullRange(
            self.port,
            self.WIN_BOOL(int(bool(psu0))),
            self.WIN_BOOL(int(bool(psu1))),
        )

    def get_psu_full_range(self):
        """Return the full-range state for both PSU channels."""
        psu0 = self.WIN_BOOL()
        psu1 = self.WIN_BOOL()
        status = self.psu_dll.COM_HVPSU2D_GetPSUFullRange(
            self.port, ctypes.byref(psu0), ctypes.byref(psu1)
        )
        return status, bool(psu0.value), bool(psu1.value)

    def get_psu_state(self):
        """Get the raw PSU state bitfield."""
        state = ctypes.c_uint32()
        status = self.psu_dll.COM_HVPSU2D_GetPSUState(self.port, ctypes.byref(state))
        return status, state.value

    def get_psu_output_voltage(self, channel: int):
        """Get the output voltage of one PSU channel in volts."""
        channel = self._validate_channel(channel)
        voltage = ctypes.c_double()
        status = self.psu_dll.COM_HVPSU2D_GetPSUOutputVoltage(
            self.port, channel, ctypes.byref(voltage)
        )
        return status, voltage.value

    def set_psu_output_voltage(self, channel: int, voltage_v: float) -> int:
        """Set the output voltage of one PSU channel in volts."""
        channel = self._validate_channel(channel)
        return self.psu_dll.COM_HVPSU2D_SetPSUOutputVoltage(
            self.port, channel, ctypes.c_double(float(voltage_v))
        )

    def get_psu_set_output_voltage(self, channel: int):
        """Get the requested and limit output voltages in volts."""
        channel = self._validate_channel(channel)
        voltage_set = ctypes.c_double()
        voltage_limit = ctypes.c_double()
        status = self.psu_dll.COM_HVPSU2D_GetPSUSetOutputVoltage(
            self.port,
            channel,
            ctypes.byref(voltage_set),
            ctypes.byref(voltage_limit),
        )
        return status, voltage_set.value, voltage_limit.value

    def get_psu_output_current(self, channel: int):
        """Get the output current of one PSU channel in amperes."""
        channel = self._validate_channel(channel)
        current = ctypes.c_double()
        status = self.psu_dll.COM_HVPSU2D_GetPSUOutputCurrent(
            self.port, channel, ctypes.byref(current)
        )
        return status, current.value

    def set_psu_output_current(self, channel: int, current_a: float) -> int:
        """Set the output current of one PSU channel in amperes."""
        channel = self._validate_channel(channel)
        return self.psu_dll.COM_HVPSU2D_SetPSUOutputCurrent(
            self.port, channel, ctypes.c_double(float(current_a))
        )

    def get_psu_set_output_current(self, channel: int):
        """Get the requested and limit output currents in amperes."""
        channel = self._validate_channel(channel)
        current_set = ctypes.c_double()
        current_limit = ctypes.c_double()
        status = self.psu_dll.COM_HVPSU2D_GetPSUSetOutputCurrent(
            self.port,
            channel,
            ctypes.byref(current_set),
            ctypes.byref(current_limit),
        )
        return status, current_set.value, current_limit.value

    def reset_current_config(self) -> int:
        """Reset the current configuration."""
        return self.psu_dll.COM_HVPSU2D_ResetCurrentConfig(self.port)

    def save_current_config(self, config_number: int) -> int:
        """Save the current configuration to NVM."""
        config_number = self._validate_config_number(config_number)
        return self.psu_dll.COM_HVPSU2D_SaveCurrentConfig(self.port, config_number)

    def load_current_config(self, config_number: int) -> int:
        """Load a configuration from NVM."""
        config_number = self._validate_config_number(config_number)
        return self.psu_dll.COM_HVPSU2D_LoadCurrentConfig(self.port, config_number)

    def get_config_name(self, config_number: int):
        """Get a PSU configuration name."""
        config_number = self._validate_config_number(config_number)
        name = ctypes.create_string_buffer(self.CONFIG_NAME_SIZE)
        status = self.psu_dll.COM_HVPSU2D_GetConfigName(
            self.port, config_number, name
        )
        return status, _decode_vendor_text(name.value)

    def set_config_name(self, config_number: int, name: str) -> int:
        """Set a PSU configuration name."""
        config_number = self._validate_config_number(config_number)
        encoded = str(name or "").encode("ascii", errors="replace")
        buffer = ctypes.create_string_buffer(self.CONFIG_NAME_SIZE)
        max_bytes = max(self.CONFIG_NAME_SIZE - 1, 0)
        buffer.value = encoded[:max_bytes]
        return self.psu_dll.COM_HVPSU2D_SetConfigName(
            self.port,
            config_number,
            buffer,
        )

    def get_config_flags(self, config_number: int):
        """Get the active/valid flags for one configuration."""
        config_number = self._validate_config_number(config_number)
        active = ctypes.c_bool()
        valid = ctypes.c_bool()
        status = self.psu_dll.COM_HVPSU2D_GetConfigFlags(
            self.port, config_number, ctypes.byref(active), ctypes.byref(valid)
        )
        return status, active.value, valid.value

    def set_config_flags(self, config_number: int, active: bool, valid: bool) -> int:
        """Set the active/valid flags for one configuration."""
        config_number = self._validate_config_number(config_number)
        return self.psu_dll.COM_HVPSU2D_SetConfigFlags(
            self.port,
            config_number,
            bool(active),
            bool(valid),
        )

    def get_config_list(self):
        """Get the full PSU configuration list."""
        active = (ctypes.c_bool * self.MAX_CONFIG)()
        valid = (ctypes.c_bool * self.MAX_CONFIG)()
        status = self.psu_dll.COM_HVPSU2D_GetConfigList(self.port, active, valid)
        return (
            status,
            [bool(active[i]) for i in range(self.MAX_CONFIG)],
            [bool(valid[i]) for i in range(self.MAX_CONFIG)],
        )

    def get_cpu_data(self):
        """Get the controller CPU load and frequency."""
        load = ctypes.c_double()
        frequency = ctypes.c_double()
        status = self.psu_dll.COM_HVPSU2D_GetCPUData(
            self.port, ctypes.byref(load), ctypes.byref(frequency)
        )
        return status, load.value, frequency.value

    def get_uptime(self):
        """Get current uptime and operation time."""
        seconds = ctypes.c_uint32()
        milliseconds = ctypes.c_uint16()
        optime = ctypes.c_uint32()
        status = self.psu_dll.COM_HVPSU2D_GetUptime(
            self.port,
            ctypes.byref(seconds),
            ctypes.byref(milliseconds),
            ctypes.byref(optime),
        )
        return status, seconds.value, milliseconds.value, optime.value

    def get_total_time(self):
        """Get total uptime and total operation time."""
        uptime = ctypes.c_uint32()
        optime = ctypes.c_uint32()
        status = self.psu_dll.COM_HVPSU2D_GetTotalTime(
            self.port, ctypes.byref(uptime), ctypes.byref(optime)
        )
        return status, uptime.value, optime.value

    def get_hw_type(self):
        """Get the hardware type."""
        hw_type = ctypes.c_uint32()
        status = self.psu_dll.COM_HVPSU2D_GetHWType(self.port, ctypes.byref(hw_type))
        return status, hw_type.value

    def get_hw_version(self):
        """Get the hardware version."""
        hw_version = ctypes.c_uint16()
        status = self.psu_dll.COM_HVPSU2D_GetHWVersion(
            self.port, ctypes.byref(hw_version)
        )
        return status, hw_version.value

    def get_fw_version(self):
        """Get the firmware version."""
        version = ctypes.c_uint16()
        status = self.psu_dll.COM_HVPSU2D_GetFWVersion(self.port, ctypes.byref(version))
        return status, version.value

    def get_fw_date(self):
        """Get the firmware date string."""
        date_string = ctypes.create_string_buffer(self.FW_DATE_SIZE)
        status = self.psu_dll.COM_HVPSU2D_GetFWDate(self.port, date_string)
        return status, _decode_vendor_text(date_string.value)

    def get_product_id(self):
        """Get the product identification string."""
        identification = ctypes.create_string_buffer(self.PRODUCT_ID_SIZE)
        status = self.psu_dll.COM_HVPSU2D_GetProductID(self.port, identification)
        return status, _decode_vendor_text(identification.value)

    def get_product_no(self):
        """Get the product number."""
        number = ctypes.c_uint32()
        status = self.psu_dll.COM_HVPSU2D_GetProductNo(self.port, ctypes.byref(number))
        return status, number.value
