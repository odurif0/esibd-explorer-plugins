"""AMX (power switch) low-level CGC driver."""

from __future__ import annotations

import ctypes
import json
import sys
from ctypes import wintypes
from pathlib import Path


class AMXPlatformError(RuntimeError):
    """Raised when the AMX driver is used on an unsupported platform."""


class AMXDllLoadError(RuntimeError):
    """Raised when the vendor AMX DLL cannot be loaded."""


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


class AMXBase:
    """Low-level CGC AMX driver backed by the vendor DLL."""

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
        0x8004: "STATE_ERR_FPGA_DIS",
    }

    DEVICE_STATE = {
        (1 << 0x00): "DEVST_VCPU_FAIL",
        (1 << 0x01): "DEVST_VSUP_FAIL",
        (1 << 0x08): "DEVST_FAN1_FAIL",
        (1 << 0x09): "DEVST_FAN2_FAIL",
        (1 << 0x0A): "DEVST_FAN3_FAIL",
        (1 << 0x0F): "DEVST_FPGA_DIS",
        (1 << 0x10): "DEVST_SEN1_HIGH",
        (1 << 0x11): "DEVST_SEN2_HIGH",
        (1 << 0x12): "DEVST_SEN3_HIGH",
        (1 << 0x18): "DEVST_SEN1_LOW",
        (1 << 0x19): "DEVST_SEN2_LOW",
        (1 << 0x1A): "DEVST_SEN3_LOW",
    }

    CONTROLLER_STATE = {
        (1 << 0): "ENB",
        (1 << 1): "ENB_OSC",
        (1 << 2): "ENB_PULSER",
        (1 << 3): "SW_TRIG",
        (1 << 4): "SW_PULSE",
        (1 << 5): "PREVENT_DIS",
        (1 << 6): "DIS_DITHER",
        (1 << 7): "NC",
        (1 << 8): "ENABLE",
        (1 << 9): "SW_TRIG_OUT",
        (1 << 10): "CLRN",
    }

    MAX_PORT = 16
    UINT32_MAX = (1 << 32) - 1
    CLOCK = 100e6
    OSC_OFFSET = 2
    SENSOR_NUM = 3
    FAN_NUM = 3
    FAN_PWM_MAX = 1000
    PULSER_NUM = 4
    PULSER_DELAY_OFFSET = 3
    PULSER_WIDTH_OFFSET = 2
    PULSER_BURST_NUM = 2
    SWITCH_NUM = 4
    SWITCH_DELAY_MAX = 1 << 4
    MAX_CONFIG = 126
    CONFIG_NAME_SIZE = 52
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
            raise AMXPlatformError(
                "CGC AMX is supported only on Windows because it depends on "
                "COM-HVAMX4ED.dll."
            )

        self.amx_dll_path = Path(dll_path) if dll_path is not None else (
            class_dir / "vendor" / "x64" / "COM-HVAMX4ED.dll"
        )
        try:
            self.amx_dll = ctypes.WinDLL(str(self.amx_dll_path))
        except OSError as exc:
            raise AMXDllLoadError(
                f"Unable to load CGC AMX DLL from '{self.amx_dll_path}': {exc}."
                f"{_format_dll_load_hint(self.amx_dll_path)}"
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

    def _validate_pulser(self, pulser_no: int) -> int:
        pulser_no = int(pulser_no)
        if not 0 <= pulser_no < self.PULSER_NUM:
            raise ValueError(
                f"Invalid pulser number: {pulser_no}. "
                f"Expected 0 <= pulser < {self.PULSER_NUM}."
            )
        return pulser_no

    def _validate_switch(self, switch_no: int) -> int:
        switch_no = int(switch_no)
        if not 0 <= switch_no < self.SWITCH_NUM:
            raise ValueError(
                f"Invalid switch number: {switch_no}. "
                f"Expected 0 <= switch < {self.SWITCH_NUM}."
            )
        return switch_no

    def _validate_burst_pulser(self, pulser_no: int) -> int:
        pulser_no = int(pulser_no)
        if not 0 <= pulser_no < self.PULSER_BURST_NUM:
            raise ValueError(
                f"Invalid burst-capable pulser number: {pulser_no}. "
                f"Expected 0 <= pulser < {self.PULSER_BURST_NUM}."
            )
        return pulser_no

    def _validate_uint32_register(self, value: int, name: str) -> int:
        value = int(value)
        if not 0 <= value <= self.UINT32_MAX:
            raise ValueError(
                f"Invalid {name}: {value}. "
                f"Expected 0 <= {name} <= {self.UINT32_MAX}."
            )
        return value

    def _validate_switch_delay(self, delay: int, name: str) -> int:
        delay = int(delay)
        if not 0 <= delay < self.SWITCH_DELAY_MAX:
            raise ValueError(
                f"Invalid {name}: {delay}. "
                f"Expected 0 <= {name} < {self.SWITCH_DELAY_MAX}."
            )
        return delay

    def _validate_config_number(self, config_number: int) -> int:
        config_number = int(config_number)
        if not 0 <= config_number < self.MAX_CONFIG:
            raise ValueError(
                f"Invalid AMX config number: {config_number}. "
                f"Expected 0 <= config < {self.MAX_CONFIG}."
            )
        return config_number

    def open_port(self, com_number: int, port_number: int | None = None) -> int:
        """Open the communication link to the AMX."""
        if port_number is None:
            port_number = self.port
        return self.amx_dll.COM_HVAMX4ED_Open(int(port_number), int(com_number))

    def close_port(self) -> int:
        """Close the communication link."""
        return self.amx_dll.COM_HVAMX4ED_Close(self.port)

    def set_baud_rate(self, baud_rate: int):
        """Set communication speed and return the negotiated baud rate."""
        baud_rate_ref = ctypes.c_uint(int(baud_rate))
        status = self.amx_dll.COM_HVAMX4ED_SetBaudRate(
            self.port, ctypes.byref(baud_rate_ref)
        )
        return status, baud_rate_ref.value

    def purge(self) -> int:
        """Clear data buffers for the port."""
        return self.amx_dll.COM_HVAMX4ED_Purge(self.port)

    def device_purge(self):
        """Clear the device output data buffer."""
        empty = self.WIN_BOOL()
        status = self.amx_dll.COM_HVAMX4ED_DevicePurge(
            self.port, ctypes.byref(empty)
        )
        return status, bool(empty.value)

    def get_buffer_state(self):
        """Return whether the device input buffer is empty."""
        empty = self.WIN_BOOL()
        status = self.amx_dll.COM_HVAMX4ED_GetBufferState(
            self.port, ctypes.byref(empty)
        )
        return status, bool(empty.value)

    def get_main_state(self):
        """Get the main AMX state."""
        state = ctypes.c_uint16()
        status = self.amx_dll.COM_HVAMX4ED_GetMainState(
            self.port, ctypes.byref(state)
        )
        state_value = state.value
        return (
            status,
            hex(state_value),
            self.MAIN_STATE.get(state_value, f"UNKNOWN_STATE_0x{state_value:04X}"),
        )

    def get_device_state(self):
        """Get the detailed AMX state bitfield."""
        device_state = ctypes.c_uint32()
        status = self.amx_dll.COM_HVAMX4ED_GetDeviceState(
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
        """Get the AMX housekeeping values."""
        volt_12v = ctypes.c_double()
        volt_5v0 = ctypes.c_double()
        volt_3v3 = ctypes.c_double()
        temp_cpu = ctypes.c_double()
        status = self.amx_dll.COM_HVAMX4ED_GetHousekeeping(
            self.port,
            ctypes.byref(volt_12v),
            ctypes.byref(volt_5v0),
            ctypes.byref(volt_3v3),
            ctypes.byref(temp_cpu),
        )
        return status, volt_12v.value, volt_5v0.value, volt_3v3.value, temp_cpu.value

    def get_sensor_data(self):
        """Get the three AMX temperature sensor readings."""
        temperatures = (ctypes.c_double * self.SENSOR_NUM)()
        status = self.amx_dll.COM_HVAMX4ED_GetSensorData(self.port, temperatures)
        return status, [temperatures[i] for i in range(self.SENSOR_NUM)]

    def get_fan_data(self):
        """Get the configured and measured fan values."""
        enabled = (self.WIN_BOOL * self.FAN_NUM)()
        failed = (self.WIN_BOOL * self.FAN_NUM)()
        set_rpm = (ctypes.c_uint16 * self.FAN_NUM)()
        measured_rpm = (ctypes.c_uint16 * self.FAN_NUM)()
        pwm = (ctypes.c_uint16 * self.FAN_NUM)()
        status = self.amx_dll.COM_HVAMX4ED_GetFanData(
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
        """Get the AMX controller LED state."""
        red = self.WIN_BOOL()
        green = self.WIN_BOOL()
        blue = self.WIN_BOOL()
        status = self.amx_dll.COM_HVAMX4ED_GetLEDData(
            self.port, ctypes.byref(red), ctypes.byref(green), ctypes.byref(blue)
        )
        return status, bool(red.value), bool(green.value), bool(blue.value)

    def get_device_enable(self):
        """Get the device enable state."""
        enable = self.WIN_BOOL()
        status = self.amx_dll.COM_HVAMX4ED_GetDeviceEnable(
            self.port, ctypes.byref(enable)
        )
        return status, bool(enable.value)

    def set_device_enable(self, enable: bool) -> int:
        """Set the device enable state."""
        return self.amx_dll.COM_HVAMX4ED_SetDeviceEnable(
            self.port, self.WIN_BOOL(int(bool(enable)))
        )

    def get_oscillator_period(self):
        """Get the oscillator period register."""
        period = ctypes.c_uint32()
        status = self.amx_dll.COM_HVAMX4ED_GetOscillatorPeriod(
            self.port, ctypes.byref(period)
        )
        return status, period.value

    def set_oscillator_period(self, period: int) -> int:
        """Set the oscillator period register."""
        period = self._validate_uint32_register(period, "period")
        return self.amx_dll.COM_HVAMX4ED_SetOscillatorPeriod(
            self.port, ctypes.c_uint32(period)
        )

    def get_pulser_delay(self, pulser_no: int):
        """Get one pulser delay register."""
        pulser_no = self._validate_pulser(pulser_no)
        delay = ctypes.c_uint32()
        status = self.amx_dll.COM_HVAMX4ED_GetPulserDelay(
            self.port, pulser_no, ctypes.byref(delay)
        )
        return status, delay.value

    def set_pulser_delay(self, pulser_no: int, delay: int) -> int:
        """Set one pulser delay register."""
        pulser_no = self._validate_pulser(pulser_no)
        delay = self._validate_uint32_register(delay, "delay")
        return self.amx_dll.COM_HVAMX4ED_SetPulserDelay(
            self.port, pulser_no, ctypes.c_uint32(delay)
        )

    def get_pulser_width(self, pulser_no: int):
        """Get one pulser width register."""
        pulser_no = self._validate_pulser(pulser_no)
        width = ctypes.c_uint32()
        status = self.amx_dll.COM_HVAMX4ED_GetPulserWidth(
            self.port, pulser_no, ctypes.byref(width)
        )
        return status, width.value

    def set_pulser_width(self, pulser_no: int, width: int) -> int:
        """Set one pulser width register."""
        pulser_no = self._validate_pulser(pulser_no)
        width = self._validate_uint32_register(width, "width")
        return self.amx_dll.COM_HVAMX4ED_SetPulserWidth(
            self.port, pulser_no, ctypes.c_uint32(width)
        )

    def get_pulser_burst(self, pulser_no: int):
        """Get the burst size for one burst-capable pulser."""
        pulser_no = self._validate_burst_pulser(pulser_no)
        burst = ctypes.c_uint32()
        status = self.amx_dll.COM_HVAMX4ED_GetPulserBurst(
            self.port, pulser_no, ctypes.byref(burst)
        )
        return status, burst.value

    def set_pulser_burst(self, pulser_no: int, burst: int) -> int:
        """Set the burst size for one burst-capable pulser."""
        pulser_no = self._validate_burst_pulser(pulser_no)
        burst = self._validate_uint32_register(burst, "burst")
        return self.amx_dll.COM_HVAMX4ED_SetPulserBurst(
            self.port, pulser_no, ctypes.c_uint32(burst)
        )

    def get_switch_trigger_config(self, switch_no: int):
        """Get one switch trigger configuration byte."""
        switch_no = self._validate_switch(switch_no)
        config = ctypes.c_ubyte()
        status = self.amx_dll.COM_HVAMX4ED_GetSwitchTriggerConfig(
            self.port, switch_no, ctypes.byref(config)
        )
        return status, config.value

    def get_switch_enable_config(self, switch_no: int):
        """Get one switch enable configuration byte."""
        switch_no = self._validate_switch(switch_no)
        config = ctypes.c_ubyte()
        status = self.amx_dll.COM_HVAMX4ED_GetSwitchEnableConfig(
            self.port, switch_no, ctypes.byref(config)
        )
        return status, config.value

    def get_switch_trigger_delay(self, switch_no: int):
        """Get the coarse trigger rise/fall delays of one switch."""
        switch_no = self._validate_switch(switch_no)
        rise_delay = ctypes.c_ubyte()
        fall_delay = ctypes.c_ubyte()
        status = self.amx_dll.COM_HVAMX4ED_GetSwitchTriggerDelay(
            self.port, switch_no, ctypes.byref(rise_delay), ctypes.byref(fall_delay)
        )
        return status, rise_delay.value, fall_delay.value

    def set_switch_trigger_delay(
        self, switch_no: int, rise_delay: int, fall_delay: int
    ) -> int:
        """Set the coarse trigger rise/fall delays of one switch."""
        switch_no = self._validate_switch(switch_no)
        rise_delay = self._validate_switch_delay(rise_delay, "rise_delay")
        fall_delay = self._validate_switch_delay(fall_delay, "fall_delay")
        return self.amx_dll.COM_HVAMX4ED_SetSwitchTriggerDelay(
            self.port,
            switch_no,
            ctypes.c_ubyte(rise_delay),
            ctypes.c_ubyte(fall_delay),
        )

    def get_switch_enable_delay(self, switch_no: int):
        """Get the coarse enable delay of one switch."""
        switch_no = self._validate_switch(switch_no)
        delay = ctypes.c_ubyte()
        status = self.amx_dll.COM_HVAMX4ED_GetSwitchEnableDelay(
            self.port, switch_no, ctypes.byref(delay)
        )
        return status, delay.value

    def set_switch_enable_delay(self, switch_no: int, delay: int) -> int:
        """Set the coarse enable delay of one switch."""
        switch_no = self._validate_switch(switch_no)
        delay = self._validate_switch_delay(delay, "delay")
        return self.amx_dll.COM_HVAMX4ED_SetSwitchEnableDelay(
            self.port, switch_no, ctypes.c_ubyte(delay)
        )

    def get_controller_state(self):
        """Get the combined AMX controller state/configuration bitfield."""
        state = ctypes.c_uint16()
        status = self.amx_dll.COM_HVAMX4ED_GetControllerState(
            self.port, ctypes.byref(state)
        )
        state_value = state.value
        active_states = []
        for flag, name in self.CONTROLLER_STATE.items():
            if state_value & flag:
                active_states.append(name)
        return status, hex(state_value), active_states

    def set_controller_config(self, config: int) -> int:
        """Set the controller configuration bitfield (Enb, EnbOsc, EnbPulser, ...)."""
        config = int(config) & 0xFFFF
        return self.amx_dll.COM_HVAMX4ED_SetControllerConfig(
            self.port, ctypes.c_uint16(config)
        )

    def save_current_config(self, config_number: int) -> int:
        """Save the current configuration to NVM."""
        config_number = self._validate_config_number(config_number)
        return self.amx_dll.COM_HVAMX4ED_SaveCurrentConfig(self.port, config_number)

    def load_current_config(self, config_number: int) -> int:
        """Load a configuration from NVM."""
        config_number = self._validate_config_number(config_number)
        return self.amx_dll.COM_HVAMX4ED_LoadCurrentConfig(self.port, config_number)

    def get_config_name(self, config_number: int):
        """Get an AMX configuration name."""
        config_number = self._validate_config_number(config_number)
        name = ctypes.create_string_buffer(self.CONFIG_NAME_SIZE)
        status = self.amx_dll.COM_HVAMX4ED_GetConfigName(self.port, config_number, name)
        return status, _decode_vendor_text(name.value)

    def get_config_flags(self, config_number: int):
        """Get the active/valid flags for one configuration."""
        config_number = self._validate_config_number(config_number)
        active = ctypes.c_bool()
        valid = ctypes.c_bool()
        status = self.amx_dll.COM_HVAMX4ED_GetConfigFlags(
            self.port, config_number, ctypes.byref(active), ctypes.byref(valid)
        )
        return status, active.value, valid.value

    def get_config_list(self):
        """Get the full AMX configuration list."""
        active = (ctypes.c_bool * self.MAX_CONFIG)()
        valid = (ctypes.c_bool * self.MAX_CONFIG)()
        status = self.amx_dll.COM_HVAMX4ED_GetConfigList(self.port, active, valid)
        return (
            status,
            [bool(active[i]) for i in range(self.MAX_CONFIG)],
            [bool(valid[i]) for i in range(self.MAX_CONFIG)],
        )

    def get_cpu_data(self):
        """Get the controller CPU load and frequency."""
        load = ctypes.c_double()
        frequency = ctypes.c_double()
        status = self.amx_dll.COM_HVAMX4ED_GetCPUData(
            self.port, ctypes.byref(load), ctypes.byref(frequency)
        )
        return status, load.value, frequency.value

    def get_uptime(self):
        """Get current uptime and operation time."""
        seconds = ctypes.c_uint32()
        milliseconds = ctypes.c_uint16()
        optime = ctypes.c_uint32()
        status = self.amx_dll.COM_HVAMX4ED_GetUptime(
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
        status = self.amx_dll.COM_HVAMX4ED_GetTotalTime(
            self.port, ctypes.byref(uptime), ctypes.byref(optime)
        )
        return status, uptime.value, optime.value

    def get_hw_type(self):
        """Get the hardware type."""
        hw_type = ctypes.c_uint16()
        status = self.amx_dll.COM_HVAMX4ED_GetHWType(self.port, ctypes.byref(hw_type))
        return status, hw_type.value

    def get_hw_version(self):
        """Get the hardware version."""
        hw_version = ctypes.c_uint16()
        status = self.amx_dll.COM_HVAMX4ED_GetHWVersion(
            self.port, ctypes.byref(hw_version)
        )
        return status, hw_version.value

    def get_fw_version(self):
        """Get the firmware version."""
        version = ctypes.c_uint16()
        status = self.amx_dll.COM_HVAMX4ED_GetFWVersion(self.port, ctypes.byref(version))
        return status, version.value

    def get_fw_date(self):
        """Get the firmware date string."""
        date_string = ctypes.create_string_buffer(self.FW_DATE_SIZE)
        status = self.amx_dll.COM_HVAMX4ED_GetFWDate(self.port, date_string)
        return status, _decode_vendor_text(date_string.value)

    def get_product_id(self):
        """Get the product identification string."""
        identification = ctypes.create_string_buffer(self.PRODUCT_ID_SIZE)
        status = self.amx_dll.COM_HVAMX4ED_GetProductID(self.port, identification)
        return status, _decode_vendor_text(identification.value)

    def get_product_no(self):
        """Get the product number."""
        number = ctypes.c_uint32()
        status = self.amx_dll.COM_HVAMX4ED_GetProductNo(
            self.port, ctypes.byref(number)
        )
        return status, number.value
