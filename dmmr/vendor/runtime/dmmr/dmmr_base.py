"""DMMR (picoammeter) low-level CGC driver."""

from __future__ import annotations

import ctypes
import json
import sys
from pathlib import Path


class DMMRPlatformError(RuntimeError):
    """Raised when the DMMR driver is used on an unsupported platform."""


class DMMRDllLoadError(RuntimeError):
    """Raised when the vendor DMMR DLL cannot be loaded."""


class DMMRBase:
    """Low-level CGC DMMR driver backed by the vendor DLL."""

    # Error codes (from COM-DMMR-8.h)
    NO_ERR = 0
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
    ERR_BUFF_FULL = -200

    NO_DATA = 1
    AUTO_MEAS_CUR = 2

    # Controller status values (from COM-DMMR-8.h)
    MAIN_STATE = {
        0: 'ST_ON',                # Modules are on
        1: 'ST_OVERLOAD',          # HV PSUs overloaded
        2: 'ST_STBY',              # HV PSUs are stand-by
        0x8000: 'ST_ERROR',        # General error
        0x8001: 'ST_ERR_MODULE',   # DPA-1F-module error
        0x8002: 'ST_ERR_VSUP',     # Supply-voltage error
    }

    # Device state bits (from COM-DMMR-8.h)
    DEVICE_STATE = {
        (1 << 0x0): 'DS_PSU_ENB',      # PSUs enabled
        (1 << 0x8): 'DS_VOLT_FAIL',    # Supply voltages failure
        (1 << 0xA): 'DS_FAN_FAIL',     # Fan failure
        (1 << 0xC): 'DS_MODULE_FAIL',  # Module configuration failure
        (1 << 0xE): 'DS_HV_STOP',      # HV PSUs were turned off
    }

    # Voltage state bits (from COM-DMMR-8.h)
    VOLTAGE_STATE = {
        (1 << 0x0): 'VS_3V3_OK',  # +3V3 rail voltage OK
        (1 << 0x1): 'VS_5V0_OK',  # +5V0 rail voltage OK
        (1 << 0x2): 'VS_12V_OK',  # +12V rail voltage OK
    }

    # Temperature state bits (from COM-DMMR-8.h)
    TEMPERATURE_STATE = {
        (1 << 0x4): 'TS_TCPU_HIGH',  # CPU overheated
        (1 << 0xC): 'TS_TCPU_LOW',   # CPU too cold
    }

    # Base device status bits (from COM-DMMR-8.h)
    BASE_STATE = {
        (1 << 0x0): 'BS_MRES',        # device reset
        (1 << 0x1): 'BS_DIS_OVRD',    # override device disable from controller
        (1 << 0x2): 'BS_DIS_CTRL',    # control device disable if DisOvrd=1
        (1 << 0x3): 'BS_ILOCK_OVRD',  # override interlock input
        (1 << 0x4): 'BS_ILOCK_CTRL',  # control interlock output if IlockOvrd=1
        (1 << 0x5): 'BS_DITH_OVRD',   # override dithering setting
        (1 << 0x6): 'BS_DITH_CTRL',   # control dithering output if DithOvrd=1
        (1 << 0x7): 'BS_ENB',         # module enable output
        (1 << 0x8): 'BS_DISABLE',     # device disable from controller
        (1 << 0x9): 'BS_ENABLE',      # enable output
        (1 << 0xA): 'BS_ILOCK_IN',    # interlock input (BNC at front)
        (1 << 0xB): 'BS_ILOCK_OUT',   # interlock output
        (1 << 0xC): 'BS_DITH_IN',     # dithering enable (switch at front)
        (1 << 0xD): 'BS_DITH_OUT',    # dithering output
        (1 << 0xE): 'BS_ON_TEMP',     # operating temperature > minimum
        (1 << 0xF): 'BS_OFF_TEMP',    # operating temperature > maximum
    }

    # Fan constants (from COM-DMMR-8.h)
    FAN_PWM_MAX = (0x9F << 1) + 1  # Maximum PWM value (100%)
    FAN_STATE = {
        (1 << 9):  'FAN_OK',        # Fan runs properly or is stopped
        (1 << 10): 'FAN_PWM_DIS',   # Fan PWM control is disabled
        (1 << 11): 'FAN_T_LOW',     # Fan control: low-temperature limit reached
        (1 << 12): 'FAN_INSTAL',    # Fan is installed
        (1 << 13): 'FAN_DIS',       # Fan is disabled via the front switch
        (1 << 14): 'FAN_OVRD_DIS',  # Override fan disable
        (1 << 15): 'FAN_CTRL_EXT',  # External fan control enabled
    }

    # Module constants
    MODULE_NUM = 8            # Maximum module number
    ADDR_BROADCAST = 0xFF     # Broadcasting address

    # Module presence return values
    MODULE_NOT_FOUND = 0      # No module found
    MODULE_PRESENT = 1        # Module with proper type found
    MODULE_INVALID = 2        # Module found but has invalid type

    # Device / module types
    DEVICE_TYPE = 0xAC38      # Expected device type
    MODULE_TYPE = 0xC41E      # Expected module device type

    # Measurement range
    MEAS_RANGE_NUM = 5        # number of measurement ranges

    # Data-ready flags
    MEAS_CUR_RDY = (1 << 0)       # current data ready
    HK_MEAS_DATA_RDY = (1 << 1)   # housekeeping data from measurement block ready
    HK_MOD_DATA_RDY = (1 << 2)    # housekeeping data from module ready

    # Configuration constants
    MAX_REG = 0x60 - 3
    MAX_CONFIG = 500
    CONFIG_NAME_SIZE = 0x89
    DATA_STRING_SIZE = 12
    PRODUCT_ID_SIZE = 81

    def __init__(
        self,
        com,
        log=None,
        idn="",
        dll_path: str | Path | None = None,
        error_codes_path: str | Path | None = None,
    ):
        """
        Initialize the low-level DMMR driver.

        Parameters
        ----------
        com : int
            Hardware COM port number.
        log : logfile, optional
            Logging instance.
        idn : string, optional
            Optional identifier suffix.

        """
        class_dir = Path(__file__).resolve().parent

        if not sys.platform.startswith("win"):
            raise DMMRPlatformError(
                "CGC DMMR is supported only on Windows because it depends on "
                "COM-DMMR-8.dll."
            )

        self.dmmr_dll_path = Path(dll_path) if dll_path is not None else (
            class_dir / "vendor" / "x64" / "COM-DMMR-8.dll"
        )
        try:
            self.dll = ctypes.WinDLL(str(self.dmmr_dll_path))
        except OSError as exc:
            raise DMMRDllLoadError(
                f"Unable to load CGC DMMR DLL from '{self.dmmr_dll_path}'."
            ) from exc

        err_path = Path(error_codes_path) if error_codes_path is not None else (
            class_dir.parent / "error_codes.json"
        )
        with err_path.open("rb") as f:
            self.err_dict = json.load(f)

        self.com = com
        self.log = log
        self.idn = idn

    def describe_error(self, status: int) -> str:
        """Return the vendor message for a driver status code."""
        return self.err_dict.get(str(status), "Unknown status code")

    def format_status(self, status: int) -> str:
        """Return a compact '<code> (<message>)' representation."""
        return f"{status} ({self.describe_error(status)})"

    @staticmethod
    def _format_state_hex(state_value: int) -> str:
        return f"0x{int(state_value):04X}"

    def _decode_state_name(self, state_value: int, mapping: dict[int, str]) -> str:
        state_value = int(state_value)
        return mapping.get(state_value, f"UNKNOWN_STATE_{self._format_state_hex(state_value)}")

    def _decode_state_flags(
        self,
        state_value: int,
        mapping: dict[int, str],
        *,
        ok_name: str | None = None,
    ) -> list[str]:
        state_value = int(state_value)
        if state_value == 0 and ok_name is not None:
            return [ok_name]
        return [name for flag, name in mapping.items() if state_value & flag]

    def _validate_config_number(self, config_number):
        if isinstance(config_number, bool) or not isinstance(config_number, int):
            raise TypeError("config_number must be an integer.")
        if not 0 <= int(config_number) < self.MAX_CONFIG:
            raise ValueError(
                f"config_number must be between 0 and {self.MAX_CONFIG - 1}."
            )
        return int(config_number)

    def _validate_config_data(self, config_data, *, field_name: str):
        if isinstance(config_data, (str, bytes)):
            raise TypeError(f"{field_name} must be an iterable of integers.")
        try:
            values = list(config_data)
        except TypeError as exc:
            raise TypeError(f"{field_name} must be an iterable of integers.") from exc

        if len(values) != self.MAX_REG:
            raise ValueError(
                f"{field_name} must contain exactly {self.MAX_REG} register values."
            )

        normalized = []
        for index, value in enumerate(values):
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(
                    f"{field_name}[{index}] must be an integer register value."
                )
            if not 0 <= int(value) <= 0xFFFFFFFF:
                raise ValueError(
                    f"{field_name}[{index}] must be between 0 and 0xFFFFFFFF."
                )
            normalized.append(int(value))

        return normalized

    def _validate_config_name(self, name: str) -> bytes:
        if not isinstance(name, str):
            raise TypeError("name must be a string.")
        encoded = name.encode()
        if len(encoded) >= self.CONFIG_NAME_SIZE:
            raise ValueError(
                f"name must be shorter than {self.CONFIG_NAME_SIZE} bytes."
            )
        return encoded

    # =========================================================================
    #     Software version
    # =========================================================================

    def get_sw_version(self):
        """
        Get the COM-DMMR-8 software version.

        Returns
        -------
        int
            Software version.

        """
        version = self.dll.COM_DMMR_8_GetSWVersion()
        return version

    # =========================================================================
    #     Communication
    # =========================================================================

    def open_port(self, com_number):
        """
        Open communication port.

        Parameters
        ----------
        com_number : int
            COM port number.

        Returns
        -------
        int
            Status code.

        """
        com_number = int(com_number)
        if not 1 <= com_number <= 255:
            raise ValueError(
                f"DMMR COM port number must be 1..255, got {com_number}."
            )
        status = self.dll.COM_DMMR_8_Open(ctypes.c_ubyte(com_number))
        return status

    def close_port(self):
        """
        Close communication port.

        Returns
        -------
        int
            Status code.

        """
        status = self.dll.COM_DMMR_8_Close()
        return status

    def set_baud_rate(self, baud_rate):
        """
        Set baud rate and return set value.

        Parameters
        ----------
        baud_rate : int
            Baud rate (usually set to max: 230400).

        Returns
        -------
        tuple
            (status, actual_baud_rate).

        """
        baud_rate_ref = ctypes.c_uint(baud_rate)
        status = self.dll.COM_DMMR_8_SetBaudRate(ctypes.byref(baud_rate_ref))
        return status, baud_rate_ref.value

    def purge(self):
        """
        Clear data buffers for the communication port.

        Returns
        -------
        int
            Status code.

        """
        status = self.dll.COM_DMMR_8_Purge()
        return status

    def get_buffer_state(self):
        """
        Return true if the input data buffer of the device is empty.

        Returns
        -------
        tuple
            (status, empty).

        """
        empty = ctypes.c_bool()
        status = self.dll.COM_DMMR_8_GetBufferState(ctypes.byref(empty))
        return status, empty.value

    def device_purge(self):
        """
        Clear output data buffer of the device.

        Returns
        -------
        tuple
            (status, empty) where empty is True if buffer is empty.

        """
        empty = ctypes.c_bool()
        status = self.dll.COM_DMMR_8_DevicePurge(ctypes.byref(empty))
        return status, empty.value

    def get_auto_mask(self):
        """
        Get the mask of the last automatic notification data.

        Returns
        -------
        tuple
            (status, command_mask).

        """
        command_mask = ctypes.c_uint()
        status = self.dll.COM_DMMR_8_GetAutoMask(ctypes.byref(command_mask))
        return status, command_mask.value

    def check_auto_input(self):
        """
        Check for new automatic notification data.

        Returns
        -------
        tuple
            (status, command_mask).

        """
        command_mask = ctypes.c_uint()
        status = self.dll.COM_DMMR_8_CheckAutoInput(ctypes.byref(command_mask))
        return status, command_mask.value

    # =========================================================================
    #     General device information
    # =========================================================================

    def get_fw_version(self):
        """
        Get firmware version.

        Returns
        -------
        tuple
            (status, version).

        """
        version = ctypes.c_ushort()
        status = self.dll.COM_DMMR_8_GetFwVersion(ctypes.byref(version))
        return status, version.value

    def get_fw_date(self):
        """
        Get firmware date.

        Returns
        -------
        tuple
            (status, date_string).

        """
        date_string = ctypes.create_string_buffer(self.DATA_STRING_SIZE)
        status = self.dll.COM_DMMR_8_GetFwDate(date_string)
        return status, date_string.value.decode()

    def get_product_id(self):
        """
        Get product identification.

        Returns
        -------
        tuple
            (status, identification).

        """
        identification = ctypes.create_string_buffer(self.PRODUCT_ID_SIZE)
        status = self.dll.COM_DMMR_8_GetProductID(identification)
        return status, identification.value.decode()

    def get_product_no(self):
        """
        Get product number.

        Returns
        -------
        tuple
            (status, number).

        """
        number = ctypes.c_uint32()
        status = self.dll.COM_DMMR_8_GetProductNo(ctypes.byref(number))
        return status, number.value

    def get_manuf_date(self):
        """
        Get manufacturing date.

        Returns
        -------
        tuple
            (status, year, calendar_week).

        """
        year = ctypes.c_ushort()
        calendar_week = ctypes.c_ushort()
        status = self.dll.COM_DMMR_8_GetManufDate(
            ctypes.byref(year), ctypes.byref(calendar_week))
        return status, year.value, calendar_week.value

    def get_device_type(self):
        """
        Get device type.

        Returns
        -------
        tuple
            (status, device_type).

        """
        device_type = ctypes.c_ushort()
        status = self.dll.COM_DMMR_8_GetDevType(ctypes.byref(device_type))
        return status, device_type.value

    def get_hw_type(self):
        """
        Get hardware type.

        Returns
        -------
        tuple
            (status, hw_type).

        """
        hw_type = ctypes.c_uint32()
        status = self.dll.COM_DMMR_8_GetHwType(ctypes.byref(hw_type))
        return status, hw_type.value

    def get_hw_version(self):
        """
        Get hardware version.

        Returns
        -------
        tuple
            (status, hw_version).

        """
        hw_version = ctypes.c_ushort()
        status = self.dll.COM_DMMR_8_GetHwVersion(ctypes.byref(hw_version))
        return status, hw_version.value

    def get_uptime_int(self):
        """
        Get current and total device uptimes as integer values.

        Returns
        -------
        tuple
            (status, seconds, milliseconds, total_seconds, total_milliseconds).

        """
        sec = ctypes.c_uint32()
        ms = ctypes.c_ushort()
        total_sec = ctypes.c_uint32()
        total_ms = ctypes.c_ushort()
        status = self.dll.COM_DMMR_8_GetUptimeInt(
            ctypes.byref(sec), ctypes.byref(ms),
            ctypes.byref(total_sec), ctypes.byref(total_ms))
        return status, sec.value, ms.value, total_sec.value, total_ms.value

    def get_optime_int(self):
        """
        Get current and total device operation times as integer values.

        Returns
        -------
        tuple
            (status, seconds, milliseconds, total_seconds, total_milliseconds).

        """
        sec = ctypes.c_uint32()
        ms = ctypes.c_ushort()
        total_sec = ctypes.c_uint32()
        total_ms = ctypes.c_ushort()
        status = self.dll.COM_DMMR_8_GetOptimeInt(
            ctypes.byref(sec), ctypes.byref(ms),
            ctypes.byref(total_sec), ctypes.byref(total_ms))
        return status, sec.value, ms.value, total_sec.value, total_ms.value

    def get_uptime(self):
        """
        Get current and total device uptimes.

        Returns
        -------
        tuple
            (status, seconds, total_seconds).

        """
        sec = ctypes.c_double()
        total_sec = ctypes.c_double()
        status = self.dll.COM_DMMR_8_GetUptime(
            ctypes.byref(sec), ctypes.byref(total_sec))
        return status, sec.value, total_sec.value

    def get_optime(self):
        """
        Get current and total device operation times.

        Returns
        -------
        tuple
            (status, seconds, total_seconds).

        """
        sec = ctypes.c_double()
        total_sec = ctypes.c_double()
        status = self.dll.COM_DMMR_8_GetOptime(
            ctypes.byref(sec), ctypes.byref(total_sec))
        return status, sec.value, total_sec.value

    def get_cpu_data(self):
        """
        Get CPU load (0-1 = 0-100%) and frequency (Hz).

        Returns
        -------
        tuple
            (status, load, frequency).

        """
        load = ctypes.c_double()
        frequency = ctypes.c_double()
        status = self.dll.COM_DMMR_8_GetCPUdata(
            ctypes.byref(load), ctypes.byref(frequency))
        return status, load.value, frequency.value

    def get_housekeeping(self):
        """
        Get housekeeping data.

        Returns
        -------
        tuple
            (status, volt_12v, volt_5v0, volt_3v3, temp_cpu).

        """
        volt_12v = ctypes.c_double()
        volt_5v0 = ctypes.c_double()
        volt_3v3 = ctypes.c_double()
        temp_cpu = ctypes.c_double()
        status = self.dll.COM_DMMR_8_GetHousekeeping(
            ctypes.byref(volt_12v), ctypes.byref(volt_5v0),
            ctypes.byref(volt_3v3), ctypes.byref(temp_cpu))
        return status, volt_12v.value, volt_5v0.value, volt_3v3.value, temp_cpu.value

    def restart(self):
        """
        Restart the controller.

        Returns
        -------
        int
            Status code.

        """
        status = self.dll.COM_DMMR_8_Restart()
        return status

    # =========================================================================
    #     DMMR-8 controller
    # =========================================================================

    def get_state(self):
        """
        Get device main state.

        Returns
        -------
        tuple
            (status, state_hex, state_name).

        """
        state = ctypes.c_ushort()
        status = self.dll.COM_DMMR_8_GetState(ctypes.byref(state))
        state_value = state.value
        state_name = self._decode_state_name(state_value, self.MAIN_STATE)
        return status, self._format_state_hex(state_value), state_name

    def get_device_state(self):
        """
        Get device state.

        Returns
        -------
        tuple
            (status, state_hex, state_names).

        """
        device_state = ctypes.c_ushort()
        status = self.dll.COM_DMMR_8_GetDeviceState(ctypes.byref(device_state))
        state_value = device_state.value
        active_states = self._decode_state_flags(
            state_value,
            self.DEVICE_STATE,
            ok_name="DEVICE_OK",
        )
        return status, self._format_state_hex(state_value), active_states

    def set_enable(self, enable):
        """
        Enable/disable modules.

        Parameters
        ----------
        enable : bool
            Enable state.

        Returns
        -------
        int
            Status code.

        """
        status = self.dll.COM_DMMR_8_SetEnable(ctypes.c_bool(enable))
        return status

    def get_enable(self):
        """
        Get module enable state.

        Returns
        -------
        tuple
            (status, enable).

        """
        enable = ctypes.c_bool()
        status = self.dll.COM_DMMR_8_GetEnable(ctypes.byref(enable))
        return status, enable.value

    def get_voltage_state(self):
        """
        Get voltage state.

        Returns
        -------
        tuple
            (status, state_hex, state_names).

        """
        voltage_state = ctypes.c_ushort()
        status = self.dll.COM_DMMR_8_GetVoltageState(ctypes.byref(voltage_state))
        state_value = voltage_state.value
        active_states = self._decode_state_flags(
            state_value,
            self.VOLTAGE_STATE,
            ok_name="VOLTAGE_OK",
        )
        return status, self._format_state_hex(state_value), active_states

    def get_temperature_state(self):
        """
        Get temperature state.

        Returns
        -------
        tuple
            (status, state_hex, state_names).

        """
        temperature_state = ctypes.c_ushort()
        status = self.dll.COM_DMMR_8_GetTemperatureState(ctypes.byref(temperature_state))
        state_value = temperature_state.value
        active_states = self._decode_state_flags(
            state_value,
            self.TEMPERATURE_STATE,
            ok_name="TEMPERATURE_OK",
        )
        return status, self._format_state_hex(state_value), active_states

    # =========================================================================
    #     DMMR-8 base device
    # =========================================================================

    def restart_base(self):
        """
        Restart the base device.

        Returns
        -------
        int
            Status code.

        """
        status = self.dll.COM_DMMR_8_RestartBase()
        return status

    def get_base_product_no(self):
        """
        Get product number of base device.

        Returns
        -------
        tuple
            (status, product_no).

        """
        product_no = ctypes.c_uint32()
        status = self.dll.COM_DMMR_8_GetBaseProductNo(ctypes.byref(product_no))
        return status, product_no.value

    def get_base_manuf_date(self):
        """
        Get manufacturing date of base device.

        Returns
        -------
        tuple
            (status, year, calendar_week).

        """
        year = ctypes.c_ushort()
        calendar_week = ctypes.c_ushort()
        status = self.dll.COM_DMMR_8_GetBaseManufDate(
            ctypes.byref(year), ctypes.byref(calendar_week))
        return status, year.value, calendar_week.value

    def get_base_hw_version(self):
        """
        Get hardware version of base device.

        Returns
        -------
        tuple
            (status, hw_version).

        """
        hw_version = ctypes.c_ushort()
        status = self.dll.COM_DMMR_8_GetBaseHwVersion(ctypes.byref(hw_version))
        return status, hw_version.value

    def get_base_hw_type(self):
        """
        Get hardware type of base device.

        Returns
        -------
        tuple
            (status, hw_type).

        """
        hw_type = ctypes.c_ushort()
        status = self.dll.COM_DMMR_8_GetBaseHwType(ctypes.byref(hw_type))
        return status, hw_type.value

    def get_base_state(self):
        """
        Get state of base device.

        Returns
        -------
        tuple
            (status, state_hex, state_names).

        """
        base_state = ctypes.c_ushort()
        status = self.dll.COM_DMMR_8_GetBaseState(ctypes.byref(base_state))
        state_value = base_state.value
        active_states = self._decode_state_flags(
            state_value,
            self.BASE_STATE,
            ok_name="BASE_OK",
        )
        return status, self._format_state_hex(state_value), active_states

    def get_base_led_data(self):
        """
        Get LED data.

        Returns
        -------
        tuple
            (status, red, green, blue).

        """
        red = ctypes.c_bool()
        green = ctypes.c_bool()
        blue = ctypes.c_bool()
        status = self.dll.COM_DMMR_8_GetBaseLEDData(
            ctypes.byref(red), ctypes.byref(green), ctypes.byref(blue))
        return status, red.value, green.value, blue.value

    def get_base_fan_pwm(self):
        """
        Get fan's PWM value and state.

        Returns
        -------
        tuple
            (status, set_pwm, state_hex, state_names).

        """
        set_pwm = ctypes.c_ushort()
        state = ctypes.c_ushort()
        status = self.dll.COM_DMMR_8_GetBaseFanPWM(
            ctypes.byref(set_pwm), ctypes.byref(state))
        state_value = state.value
        active_states = self._decode_state_flags(state_value, self.FAN_STATE)
        return status, set_pwm.value, self._format_state_hex(state_value), active_states

    def get_base_fan_rpm(self):
        """
        Get fan's RPM.

        Returns
        -------
        tuple
            (status, rpm).

        """
        rpm = ctypes.c_double()
        status = self.dll.COM_DMMR_8_GetBaseFanRPM(ctypes.byref(rpm))
        return status, rpm.value

    def get_base_temp(self):
        """
        Get base temperature.

        Returns
        -------
        tuple
            (status, base_temp).

        """
        base_temp = ctypes.c_double()
        status = self.dll.COM_DMMR_8_GetBaseTemp(ctypes.byref(base_temp))
        return status, base_temp.value

    # =========================================================================
    #     DPA-1F module service
    # =========================================================================

    def get_module_presence(self):
        """
        Get device's maximum module number & module-presence flags.

        Returns
        -------
        tuple
            (status, valid, max_module, module_presence_list).

        """
        valid = ctypes.c_bool()
        max_module = ctypes.c_uint()
        module_presence = (ctypes.c_ubyte * self.MODULE_NUM)()

        status = self.dll.COM_DMMR_8_GetModulePresence(
            ctypes.byref(valid), ctypes.byref(max_module), module_presence)

        presence_list = [module_presence[i] for i in range(self.MODULE_NUM)]
        return status, valid.value, max_module.value, presence_list

    def update_module_presence(self):
        """
        Update module-presence flags.

        Returns
        -------
        int
            Status code.

        """
        status = self.dll.COM_DMMR_8_UpdateModulePresence()
        return status

    def rescan_modules(self):
        """
        Rescan address pins of all modules.

        Returns
        -------
        int
            Status code.

        """
        status = self.dll.COM_DMMR_8_RescanModules()
        return status

    def rescan_module(self, address):
        """
        Rescan address pins of the specified module.

        Parameters
        ----------
        address : int
            Module address.

        Returns
        -------
        int
            Status code.

        """
        status = self.dll.COM_DMMR_8_RescanModule(ctypes.c_uint(address))
        return status

    def restart_module(self, address):
        """
        Restart the specified module.

        Parameters
        ----------
        address : int
            Module address.

        Returns
        -------
        int
            Status code.

        """
        status = self.dll.COM_DMMR_8_RestartModule(ctypes.c_uint(address))
        return status

    def get_module_buffer_state(self, address):
        """
        Return true if the input data buffer of the specified module is empty.

        Parameters
        ----------
        address : int
            Module address.

        Returns
        -------
        tuple
            (status, empty).

        """
        empty = ctypes.c_bool()
        status = self.dll.COM_DMMR_8_GetModuleBufferState(
            ctypes.c_uint(address), ctypes.byref(empty))
        return status, empty.value

    def module_purge(self, address):
        """
        Clear output data buffer of the specified module.

        Parameters
        ----------
        address : int
            Module address.

        Returns
        -------
        tuple
            (status, empty).

        """
        empty = ctypes.c_bool()
        status = self.dll.COM_DMMR_8_ModulePurge(
            ctypes.c_uint(address), ctypes.byref(empty))
        return status, empty.value

    def get_scanned_module_state(self):
        """
        Get the state of the module scan.

        Returns
        -------
        tuple
            (status, module_mismatch).

        """
        module_mismatch = ctypes.c_bool()
        status = self.dll.COM_DMMR_8_GetScannedModuleState(
            ctypes.byref(module_mismatch))
        return status, module_mismatch.value

    def set_scanned_module_state(self):
        """
        Reset the module mismatch, i.e. save the current device configuration.

        Returns
        -------
        int
            Status code.

        """
        status = self.dll.COM_DMMR_8_SetScannedModuleState()
        return status

    def get_scanned_module_params(self, address):
        """
        Get scanned & saved product number & hardware type of a module.

        Parameters
        ----------
        address : int
            Module address.

        Returns
        -------
        tuple
            (status, scanned_product_no, saved_product_no, scanned_hw_type, saved_hw_type).

        """
        scanned_product_no = ctypes.c_uint32()
        saved_product_no = ctypes.c_uint32()
        scanned_hw_type = ctypes.c_uint32()
        saved_hw_type = ctypes.c_uint32()
        status = self.dll.COM_DMMR_8_GetScannedModuleParams(
            ctypes.c_uint(address),
            ctypes.byref(scanned_product_no), ctypes.byref(saved_product_no),
            ctypes.byref(scanned_hw_type), ctypes.byref(saved_hw_type))
        return (status, scanned_product_no.value, saved_product_no.value,
                scanned_hw_type.value, saved_hw_type.value)

    def get_module_fw_version(self, address):
        """
        Get module firmware version.

        Parameters
        ----------
        address : int
            Module address.

        Returns
        -------
        tuple
            (status, fw_version).

        """
        fw_version = ctypes.c_ushort()
        status = self.dll.COM_DMMR_8_GetModuleFwVersion(
            ctypes.c_uint(address), ctypes.byref(fw_version))
        return status, fw_version.value

    def get_module_fw_date(self, address):
        """
        Get module firmware date.

        Parameters
        ----------
        address : int
            Module address.

        Returns
        -------
        tuple
            (status, date_string).

        """
        date_string = ctypes.create_string_buffer(self.DATA_STRING_SIZE)
        status = self.dll.COM_DMMR_8_GetModuleFwDate(
            ctypes.c_uint(address), date_string)
        return status, date_string.value.decode()

    def get_module_product_id(self, address):
        """
        Get module product identification.

        Parameters
        ----------
        address : int
            Module address.

        Returns
        -------
        tuple
            (status, identification).

        """
        identification = ctypes.create_string_buffer(self.PRODUCT_ID_SIZE)
        status = self.dll.COM_DMMR_8_GetModuleProductID(
            ctypes.c_uint(address), identification)
        return status, identification.value.decode()

    def get_module_product_no(self, address):
        """
        Get module product number.

        Parameters
        ----------
        address : int
            Module address.

        Returns
        -------
        tuple
            (status, product_no).

        """
        product_no = ctypes.c_uint32()
        status = self.dll.COM_DMMR_8_GetModuleProductNo(
            ctypes.c_uint(address), ctypes.byref(product_no))
        return status, product_no.value

    def get_module_manuf_date(self, address):
        """
        Get module manufacturing date.

        Parameters
        ----------
        address : int
            Module address.

        Returns
        -------
        tuple
            (status, year, calendar_week).

        """
        year = ctypes.c_ushort()
        calendar_week = ctypes.c_ushort()
        status = self.dll.COM_DMMR_8_GetModuleManufDate(
            ctypes.c_uint(address),
            ctypes.byref(year), ctypes.byref(calendar_week))
        return status, year.value, calendar_week.value

    def get_module_device_type(self, address):
        """
        Get module device type.

        Parameters
        ----------
        address : int
            Module address.

        Returns
        -------
        tuple
            (status, dev_type).

        """
        dev_type = ctypes.c_ushort()
        status = self.dll.COM_DMMR_8_GetModuleDevType(
            ctypes.c_uint(address), ctypes.byref(dev_type))
        return status, dev_type.value

    def get_module_hw_type(self, address):
        """
        Get module hardware type.

        Parameters
        ----------
        address : int
            Module address.

        Returns
        -------
        tuple
            (status, hw_type).

        """
        hw_type = ctypes.c_uint32()
        status = self.dll.COM_DMMR_8_GetModuleHwType(
            ctypes.c_uint(address), ctypes.byref(hw_type))
        return status, hw_type.value

    def get_module_hw_version(self, address):
        """
        Get module hardware version.

        Parameters
        ----------
        address : int
            Module address.

        Returns
        -------
        tuple
            (status, hw_version).

        """
        hw_version = ctypes.c_ushort()
        status = self.dll.COM_DMMR_8_GetModuleHwVersion(
            ctypes.c_uint(address), ctypes.byref(hw_version))
        return status, hw_version.value

    def get_module_uptime_int(self, address):
        """
        Get current and total module uptimes as integer values.

        Parameters
        ----------
        address : int
            Module address.

        Returns
        -------
        tuple
            (status, seconds, milliseconds, total_seconds, total_milliseconds).

        """
        sec = ctypes.c_uint32()
        ms = ctypes.c_ushort()
        total_sec = ctypes.c_uint32()
        total_ms = ctypes.c_ushort()
        status = self.dll.COM_DMMR_8_GetModuleUptimeInt(
            ctypes.c_uint(address),
            ctypes.byref(sec), ctypes.byref(ms),
            ctypes.byref(total_sec), ctypes.byref(total_ms))
        return status, sec.value, ms.value, total_sec.value, total_ms.value

    def get_module_optime_int(self, address):
        """
        Get current and total module operation times as integer values.

        Parameters
        ----------
        address : int
            Module address.

        Returns
        -------
        tuple
            (status, seconds, milliseconds, total_seconds, total_milliseconds).

        """
        sec = ctypes.c_uint32()
        ms = ctypes.c_ushort()
        total_sec = ctypes.c_uint32()
        total_ms = ctypes.c_ushort()
        status = self.dll.COM_DMMR_8_GetModuleOptimeInt(
            ctypes.c_uint(address),
            ctypes.byref(sec), ctypes.byref(ms),
            ctypes.byref(total_sec), ctypes.byref(total_ms))
        return status, sec.value, ms.value, total_sec.value, total_ms.value

    def get_module_uptime(self, address):
        """
        Get current and total module uptimes.

        Parameters
        ----------
        address : int
            Module address.

        Returns
        -------
        tuple
            (status, seconds, total_seconds).

        """
        sec = ctypes.c_double()
        total_sec = ctypes.c_double()
        status = self.dll.COM_DMMR_8_GetModuleUptime(
            ctypes.c_uint(address),
            ctypes.byref(sec), ctypes.byref(total_sec))
        return status, sec.value, total_sec.value

    def get_module_optime(self, address):
        """
        Get current and total module operation times.

        Parameters
        ----------
        address : int
            Module address.

        Returns
        -------
        tuple
            (status, seconds, total_seconds).

        """
        sec = ctypes.c_double()
        total_sec = ctypes.c_double()
        status = self.dll.COM_DMMR_8_GetModuleOptime(
            ctypes.c_uint(address),
            ctypes.byref(sec), ctypes.byref(total_sec))
        return status, sec.value, total_sec.value

    def get_module_cpu_data(self, address):
        """
        Get module CPU load (0-1 = 0-100%).

        Parameters
        ----------
        address : int
            Module address.

        Returns
        -------
        tuple
            (status, load).

        """
        load = ctypes.c_double()
        status = self.dll.COM_DMMR_8_GetModuleCPUdata(
            ctypes.c_uint(address), ctypes.byref(load))
        return status, load.value

    def get_module_housekeeping(self, address):
        """
        Get module housekeeping data.

        Parameters
        ----------
        address : int
            Module address.

        Returns
        -------
        tuple
            (status, volt_3v3, temp_cpu, volt_5v0, volt_12v,
             volt_3v3i, temp_cpui, volt_2v5i, volt_36vn,
             volt_20vp, volt_20vn, volt_15vp, volt_15vn,
             volt_1v8p, volt_1v8n, volt_vrefp, volt_vrefn).

        """
        volt_3v3 = ctypes.c_double()
        temp_cpu = ctypes.c_double()
        volt_5v0 = ctypes.c_double()
        volt_12v = ctypes.c_double()
        volt_3v3i = ctypes.c_double()
        temp_cpui = ctypes.c_double()
        volt_2v5i = ctypes.c_double()
        volt_36vn = ctypes.c_double()
        volt_20vp = ctypes.c_double()
        volt_20vn = ctypes.c_double()
        volt_15vp = ctypes.c_double()
        volt_15vn = ctypes.c_double()
        volt_1v8p = ctypes.c_double()
        volt_1v8n = ctypes.c_double()
        volt_vrefp = ctypes.c_double()
        volt_vrefn = ctypes.c_double()

        status = self.dll.COM_DMMR_8_GetModuleHousekeeping(
            ctypes.c_uint(address),
            ctypes.byref(volt_3v3), ctypes.byref(temp_cpu),
            ctypes.byref(volt_5v0), ctypes.byref(volt_12v),
            ctypes.byref(volt_3v3i), ctypes.byref(temp_cpui),
            ctypes.byref(volt_2v5i), ctypes.byref(volt_36vn),
            ctypes.byref(volt_20vp), ctypes.byref(volt_20vn),
            ctypes.byref(volt_15vp), ctypes.byref(volt_15vn),
            ctypes.byref(volt_1v8p), ctypes.byref(volt_1v8n),
            ctypes.byref(volt_vrefp), ctypes.byref(volt_vrefn))

        return (status, volt_3v3.value, temp_cpu.value, volt_5v0.value, volt_12v.value,
                volt_3v3i.value, temp_cpui.value, volt_2v5i.value, volt_36vn.value,
                volt_20vp.value, volt_20vn.value, volt_15vp.value, volt_15vn.value,
                volt_1v8p.value, volt_1v8n.value, volt_vrefp.value, volt_vrefn.value)

    def get_module_state(self, address):
        """
        Get module state.

        Parameters
        ----------
        address : int
            Module address.

        Returns
        -------
        tuple
            (status, state).

        """
        state = ctypes.c_ushort()
        status = self.dll.COM_DMMR_8_GetModuleState(
            ctypes.c_uint(address), ctypes.byref(state))
        return status, state.value

    def set_module_meas_range(self, address, meas_range):
        """
        Set measurement range.

        Parameters
        ----------
        address : int
            Module address.
        meas_range : int
            Measurement range (0 to MEAS_RANGE_NUM-1).

        Returns
        -------
        int
            Status code.

        """
        status = self.dll.COM_DMMR_8_SetModuleMeasRange(
            ctypes.c_uint(address), ctypes.c_uint(meas_range))
        return status

    def set_module_auto_range(self, address, auto_range):
        """
        Set automatic range switching.

        Parameters
        ----------
        address : int
            Module address.
        auto_range : bool
            Enable automatic range switching.

        Returns
        -------
        int
            Status code.

        """
        status = self.dll.COM_DMMR_8_SetModuleAutoRange(
            ctypes.c_uint(address), ctypes.c_bool(auto_range))
        return status

    def get_module_meas_range(self, address):
        """
        Get measurement range & automatic range switching.

        Parameters
        ----------
        address : int
            Module address.

        Returns
        -------
        tuple
            (status, meas_range, auto_range).

        """
        meas_range = ctypes.c_uint()
        auto_range = ctypes.c_bool()
        status = self.dll.COM_DMMR_8_GetModuleMeasRange(
            ctypes.c_uint(address),
            ctypes.byref(meas_range), ctypes.byref(auto_range))
        return status, meas_range.value, auto_range.value

    def get_module_ready_flags(self, address):
        """
        Get data-ready flags.

        Parameters
        ----------
        address : int
            Module address.

        Returns
        -------
        tuple
            (status, ready_flags).

        """
        ready_flags = ctypes.c_ubyte()
        status = self.dll.COM_DMMR_8_GetModuleReadyFlags(
            ctypes.c_uint(address), ctypes.byref(ready_flags))
        return status, ready_flags.value

    def get_module_current(self, address):
        """
        Get current measured by module & used measurement range.

        Parameters
        ----------
        address : int
            Module address.

        Returns
        -------
        tuple
            (status, meas_current, meas_range).

        """
        meas_current = ctypes.c_double()
        meas_range = ctypes.c_uint()
        status = self.dll.COM_DMMR_8_GetModuleCurent(
            ctypes.c_uint(address),
            ctypes.byref(meas_current), ctypes.byref(meas_range))
        return status, meas_current.value, meas_range.value

    def set_automatic_current(self, automatic_current):
        """
        Turn on or off automatic current measurement.

        Parameters
        ----------
        automatic_current : bool
            Enable automatic current measurement.

        Returns
        -------
        int
            Status code.

        """
        status = self.dll.COM_DMMR_8_SetAutomaticCurent(
            ctypes.c_bool(automatic_current))
        return status

    def get_automatic_current(self):
        """
        Get state of automatic current measurement.

        Returns
        -------
        tuple
            (status, automatic_current).

        """
        automatic_current = ctypes.c_bool()
        status = self.dll.COM_DMMR_8_GetAutomaticCurent(
            ctypes.byref(automatic_current))
        return status, automatic_current.value

    def get_current(self):
        """
        Check if new current data is available.

        Returns module address, current value, used measurement range
        and time stamp of the data.

        Returns
        -------
        tuple
            (status, address, meas_current, meas_range, time).

        """
        address = ctypes.c_uint()
        meas_current = ctypes.c_double()
        meas_range = ctypes.c_uint()
        time = ctypes.c_double()
        status = self.dll.COM_DMMR_8_GetCurent(
            ctypes.byref(address), ctypes.byref(meas_current),
            ctypes.byref(meas_range), ctypes.byref(time))
        return status, address.value, meas_current.value, meas_range.value, time.value

    # =========================================================================
    #     Configuration management
    # =========================================================================

    def get_current_config(self):
        """
        Get current configuration.

        Returns
        -------
        tuple
            (status, config_array).

        """
        config = (ctypes.c_uint32 * self.MAX_REG)()
        status = self.dll.COM_DMMR_8_GetCurrentConfig(config)
        return status, [config[i] for i in range(self.MAX_REG)]

    def set_current_config(self, config_data):
        """
        Set current configuration.

        Parameters
        ----------
        config_data : list
            Configuration data array of length MAX_REG.

        Returns
        -------
        int
            Status code.

        """
        normalized = self._validate_config_data(
            config_data,
            field_name="config_data",
        )
        config = (ctypes.c_uint32 * self.MAX_REG)(*normalized)
        status = self.dll.COM_DMMR_8_SetCurrentConfig(config)
        return status

    def get_config_list(self):
        """
        Get configuration list.

        Returns
        -------
        tuple
            (status, active_list, valid_list).

        """
        active = (ctypes.c_bool * self.MAX_CONFIG)()
        valid = (ctypes.c_bool * self.MAX_CONFIG)()
        status = self.dll.COM_DMMR_8_GetConfigList(active, valid)
        return (status,
                [active[i] for i in range(self.MAX_CONFIG)],
                [valid[i] for i in range(self.MAX_CONFIG)])

    def save_current_config(self, config_number):
        """
        Save current configuration to NVM.

        Parameters
        ----------
        config_number : int
            Configuration slot number.

        Returns
        -------
        int
            Status code.

        """
        config_number = self._validate_config_number(config_number)
        status = self.dll.COM_DMMR_8_SaveCurrentConfig(ctypes.c_ushort(config_number))
        return status

    def load_current_config(self, config_number):
        """
        Load current configuration from NVM.

        Parameters
        ----------
        config_number : int
            Configuration slot number.

        Returns
        -------
        int
            Status code.

        """
        config_number = self._validate_config_number(config_number)
        status = self.dll.COM_DMMR_8_LoadCurrentConfig(ctypes.c_ushort(config_number))
        return status

    def get_config_name(self, config_number):
        """
        Get configuration name.

        Parameters
        ----------
        config_number : int
            Configuration slot number.

        Returns
        -------
        tuple
            (status, name).

        """
        config_number = self._validate_config_number(config_number)
        name = ctypes.create_string_buffer(self.CONFIG_NAME_SIZE)
        status = self.dll.COM_DMMR_8_GetConfigName(
            ctypes.c_ushort(config_number), name)
        return status, name.value.decode()

    def set_config_name(self, config_number, name):
        """
        Set configuration name.

        Parameters
        ----------
        config_number : int
            Configuration slot number.
        name : str
            Configuration name.

        Returns
        -------
        int
            Status code.

        """
        config_number = self._validate_config_number(config_number)
        name_buf = ctypes.create_string_buffer(
            self._validate_config_name(name), self.CONFIG_NAME_SIZE)
        status = self.dll.COM_DMMR_8_SetConfigName(
            ctypes.c_ushort(config_number), name_buf)
        return status

    def get_config_data(self, config_number):
        """
        Get configuration data.

        Parameters
        ----------
        config_number : int
            Configuration slot number.

        Returns
        -------
        tuple
            (status, config_array).

        """
        config_number = self._validate_config_number(config_number)
        config = (ctypes.c_uint32 * self.MAX_REG)()
        status = self.dll.COM_DMMR_8_GetConfigData(
            ctypes.c_ushort(config_number), config)
        return status, [config[i] for i in range(self.MAX_REG)]

    def set_config_data(self, config_number, config_data):
        """
        Set configuration data.

        Parameters
        ----------
        config_number : int
            Configuration slot number.
        config_data : list
            Configuration data array of length MAX_REG.

        Returns
        -------
        int
            Status code.

        """
        config_number = self._validate_config_number(config_number)
        normalized = self._validate_config_data(
            config_data,
            field_name="config_data",
        )
        config = (ctypes.c_uint32 * self.MAX_REG)(*normalized)
        status = self.dll.COM_DMMR_8_SetConfigData(
            ctypes.c_ushort(config_number), config)
        return status

    def get_config_flags(self, config_number):
        """
        Get configuration flags.

        Parameters
        ----------
        config_number : int
            Configuration slot number.

        Returns
        -------
        tuple
            (status, active, valid).

        """
        config_number = self._validate_config_number(config_number)
        active = ctypes.c_bool()
        valid = ctypes.c_bool()
        status = self.dll.COM_DMMR_8_GetConfigFlags(
            ctypes.c_ushort(config_number),
            ctypes.byref(active), ctypes.byref(valid))
        return status, active.value, valid.value

    def set_config_flags(self, config_number, active, valid):
        """
        Set configuration flags.

        Parameters
        ----------
        config_number : int
            Configuration slot number.
        active : bool
            Active flag.
        valid : bool
            Valid flag.

        Returns
        -------
        int
            Status code.

        """
        config_number = self._validate_config_number(config_number)
        status = self.dll.COM_DMMR_8_SetConfigFlags(
            ctypes.c_ushort(config_number),
            ctypes.c_bool(active), ctypes.c_bool(valid))
        return status

    # =========================================================================
    #     Error handling
    # =========================================================================

    def get_interface_state(self):
        """
        Get software interface state.

        Returns
        -------
        int
            Interface state code.

        """
        return self.dll.COM_DMMR_8_GetInterfaceState()

    def get_error_message(self):
        """
        Get error message corresponding to the software interface state.

        Returns
        -------
        str
            Error message string.

        """
        self.dll.COM_DMMR_8_GetErrorMessage.restype = ctypes.c_char_p
        msg = self.dll.COM_DMMR_8_GetErrorMessage()
        return msg.decode() if msg else ""

    def get_io_error_message(self):
        """
        Get error message corresponding to the serial port interface state.

        Returns
        -------
        str
            IO error message string.

        """
        self.dll.COM_DMMR_8_GetIOErrorMessage.restype = ctypes.c_char_p
        msg = self.dll.COM_DMMR_8_GetIOErrorMessage()
        return msg.decode() if msg else ""

    def get_io_state(self):
        """
        Get and clear last serial port interface state.

        Returns
        -------
        tuple
            (status, io_state).

        """
        io_state = ctypes.c_int()
        status = self.dll.COM_DMMR_8_GetIOState(ctypes.byref(io_state))
        return status, io_state.value

    def get_io_state_message(self, io_state):
        """
        Get error message corresponding to the specified interface state.

        Parameters
        ----------
        io_state : int
            IO state code.

        Returns
        -------
        str
            IO state message string.

        """
        self.dll.COM_DMMR_8_GetIOStateMessage.restype = ctypes.c_char_p
        msg = self.dll.COM_DMMR_8_GetIOStateMessage(ctypes.c_int(io_state))
        return msg.decode() if msg else ""

    def get_comm_error(self):
        """
        Get and clear last communication-port error.

        Returns
        -------
        tuple
            (status, comm_error).

        """
        comm_error = ctypes.c_uint32()
        status = self.dll.COM_DMMR_8_GetCommError(ctypes.byref(comm_error))
        return status, comm_error.value

    def get_comm_error_message(self, comm_error):
        """
        Get error message corresponding to the communication port error.

        Parameters
        ----------
        comm_error : int
            Communication error code.

        Returns
        -------
        str
            Communication error message string.

        """
        self.dll.COM_DMMR_8_GetCommErrorMessage.restype = ctypes.c_char_p
        msg = self.dll.COM_DMMR_8_GetCommErrorMessage(ctypes.c_uint32(comm_error))
        return msg.decode() if msg else ""

    # =========================================================================
    #     Convenience methods
    # =========================================================================

    def scan_all_modules(self):
        """
        Scan for all connected modules and return their information.

        Returns
        -------
        dict
            Dictionary with module addresses as keys and module info as values.

        """
        modules = {}
        status, valid, max_module, presence_list = self.get_module_presence()

        if status != self.NO_ERR:
            return modules

        for addr in range(min(max_module + 1, self.MODULE_NUM)):
            if presence_list[addr] == self.MODULE_PRESENT:
                module_info = {}

                fw_status, fw_version = self.get_module_fw_version(addr)
                if fw_status == self.NO_ERR:
                    module_info['fw_version'] = fw_version

                prod_status, product_no = self.get_module_product_no(addr)
                if prod_status == self.NO_ERR:
                    module_info['product_no'] = product_no

                hw_status, hw_type = self.get_module_hw_type(addr)
                if hw_status == self.NO_ERR:
                    module_info['hw_type'] = hw_type

                hwv_status, hw_version = self.get_module_hw_version(addr)
                if hwv_status == self.NO_ERR:
                    module_info['hw_version'] = hw_version

                state_status, state = self.get_module_state(addr)
                if state_status == self.NO_ERR:
                    module_info['state'] = state

                modules[addr] = module_info

        return modules
