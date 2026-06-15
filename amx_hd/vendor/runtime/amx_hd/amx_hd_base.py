"""AMX HD (High-Definition power switch) low-level CGC driver (HV-AMX-CTRL-4EDH)."""

from __future__ import annotations

import ctypes
import json
import sys
from ctypes import wintypes
from pathlib import Path


class AMXHDPlatformError(RuntimeError):
    """Raised when the AMX HD driver is used on an unsupported platform."""


class AMXHDDllLoadError(RuntimeError):
    """Raised when the vendor AMX HD DLL cannot be loaded."""


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


class AMXHDBase:
    """Low-level CGC AMX HD driver backed by the vendor DLL (COM-HVAMX4EDH)."""

    WIN_BOOL = wintypes.BOOL
    """SW_HR base device class wrapping the COM-HVAMX4EDH DLL."""

    # Error codes
    NO_ERR = 0
    ERR_STREAM_RANGE = -1
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
    ERR_CONFIG = -200
    ERR_CONFIG_EMPTY = -201
    ERR_DEBUG_OPEN = -400
    ERR_DEBUG_CLOSE = -401

    # Main state constants dictionary
    MAIN_STATE = {
        0x0000: 'STATE_STANDBY',
        0x0001: 'STATE_ON',
        0x8000: 'STATE_ERROR',
        0x8001: 'STATE_ERR_VSUP',
        0x8002: 'STATE_ERR_TEMP_LOW',
        0x8003: 'STATE_ERR_TEMP_HIGH',
    }

    # Device state constants dictionary (bit flags)
    DEVICE_STATE = {
        (1 << 0x00): 'DEVST_VCPU_FAIL',
        (1 << 0x01): 'DEVST_VSUP_FAIL',
        (1 << 0x04): 'DEVST_PLL0_FAIL',
        (1 << 0x05): 'DEVST_PLL1_FAIL',
        (1 << 0x06): 'DEVST_PLL2_FAIL',
        (1 << 0x07): 'DEVST_PLL3_FAIL',
        (1 << 0x08): 'DEVST_FAN1_FAIL',
        (1 << 0x09): 'DEVST_FAN2_FAIL',
        (1 << 0x0A): 'DEVST_FAN3_FAIL',
        (1 << 0x0F): 'DEVST_FPGA_DIS',
    }

    # Temperature state constants dictionary (bit flags)
    TEMPERATURE_STATE = {
        (1 << 0x00): 'TMPST_SEN1_HIGH',
        (1 << 0x01): 'TMPST_SEN2_HIGH',
        (1 << 0x02): 'TMPST_SEN3_HIGH',
        (1 << 0x08): 'TMPST_SEN1_LOW',
        (1 << 0x09): 'TMPST_SEN2_LOW',
        (1 << 0x0A): 'TMPST_SEN3_LOW',
    }

    # Controller state & configuration bits (GetState / SetConfig)
    CONTROLLER_STATE = {
        (1 << 0):  'ST_ENABLE',
        (1 << 1):  'ST_ENB_TIMER',
        (1 << 2):  'ST_ENB_OSC0',
        (1 << 3):  'ST_ENB_OSC1',
        (1 << 4):  'ST_ENB_OSC2',
        (1 << 5):  'ST_ENB_OSC3',
        (1 << 6):  'ST_SW_TRIG0',
        (1 << 7):  'ST_SW_PULSE0',
        (1 << 8):  'ST_SW_TRIG1',
        (1 << 9):  'ST_SW_PULSE1',
        (1 << 10): 'ST_DITHER',
        (1 << 11): 'ST_ENB_SWITCH',
    }

    # Extended state bits (GetState only, upper DWORD bits)
    EXTENDED_STATE = {
        (1 << 12): 'ST_ENB_PULS_GEN',
        (1 << 16): 'ST_CLRN',
        (1 << 17): 'ST_DEL_BUSY',
        (1 << 18): 'ST_ENB_PWM',
        (1 << 22): 'ST_SW_TRIG_OUT0',
        (1 << 23): 'ST_SW_TRIG_OUT1',
        (1 << 24): 'ST_DIO0',
        (1 << 25): 'ST_DIO1',
        (1 << 26): 'ST_DIO2',
        (1 << 27): 'ST_DIO3',
        (1 << 28): 'ST_DIO4',
        (1 << 29): 'ST_DIO5',
        (1 << 30): 'ST_DIO6',
        (1 << 31): 'ST_DIO7',
    }

    # Clock source constants
    CLOCK_SRC_NUM = 0x10
    CLOCK_SRC_MASK = 0x0F
    CLOCK_SRC_INVERT = (1 << 7)
    CLOCK_SOURCE_MASK = CLOCK_SRC_MASK | CLOCK_SRC_INVERT
    CLOCK_NUM = 4

    # PLL constants
    PLL_SRC_NUM = 0x10
    PLL_SRC_MASK = 0x0F
    PLL_SRC_INVERT = (1 << 7)
    PLL_SOURCE_MASK = PLL_SRC_MASK | PLL_SRC_INVERT
    PLL_NUM = 4
    PLL_REF_DIV_MIN = 1
    PLL_REF_DIV_MAX = 4095
    PLL_FB_DIV_MIN = 4
    PLL_FB_DIV_MAX = 16383
    PLL_FB_DIV_NA1 = 6
    PLL_FB_DIV_NA2 = 7
    PLL_FB_DIV_NA3 = 11
    PLL_POST_DIV1_MIN = 1
    PLL_POST_DIV1_MAX = 12
    PLL_POST_DIV2_MIN = 1
    PLL_POST_DIV2_MAX = 12
    PLL_POST_DIV3_1 = 0  # divider 1
    PLL_POST_DIV3_2 = 1  # divider 2
    PLL_POST_DIV3_4 = 2  # divider 4
    PLL_POST_DIV3_8 = 3  # divider 8
    PLL_POST_DIV3_MAX = 4

    # PLL charge-pump current
    PLL_CP_2u0 = 0   #  2.0 uA
    PLL_CP_4u5 = 1   #  4.5 uA
    PLL_CP_11u0 = 2  # 11.0 uA
    PLL_CP_22u5 = 3  # 22.5 uA
    PLL_CP_MAX = 4

    # PLL loop-filter resistor
    PLL_LR_400K = 0  # 400 kOhm
    PLL_LR_133K = 1  # 133 kOhm
    PLL_LR_30K = 2   #  30 kOhm
    PLL_LR_12K = 3   #  12 kOhm
    PLL_LR_MAX = 4

    # PLL loop-filter capacitor
    PLL_LC_185pF = 0  # 185 pF
    PLL_LC_500pF = 1  # 500 pF
    PLL_LC_MAX = 2

    # Divider constants
    DIV_CLOCK = 200e6
    DIV_OFFSET = 2
    DIV_NUM = 4

    # Counter constants
    CNT_CLOCK = 200e6
    CNT_PER_MIN = 2
    CNT_PER_MAX = 20
    CNT_NUM = 4

    # Oscillator constants
    DEF_CLOCK = 100e6
    OSC_OFFSET = 2

    # Timer constants
    TIMER_DELAY_OFFSET = 3
    TIMER_WIDTH_OFFSET = 2
    TIMER_MAX_BURST = (1 << 24)
    TIMER_MASK_BURST = TIMER_MAX_BURST - 1

    # Mapping engine constants
    MAPPING_INPUT_NUM = 4
    MAPPING_STATE_NUM = 7
    MAPPING_STATE_BITS = 0x0F

    # Switch constants
    SWITCH_NUM = 4
    SWITCH_DELAY_SIZE = 4
    SWITCH_DELAY_MAX = (1 << 4)
    SWITCH_DELAY_MASK = (1 << 4) - 1
    SWITCH_DELAY_SCALE = 5e-9
    SWITCH_DELAY_FINE_MAX = 0x200
    SWITCH_DELAY_FINE_SCALE = 11e-12

    # Digital I/O constants
    DIO_CFG_IN = 0
    DIO_CFG_OUT = 1
    DIO_CFG_IN_TERM = 2
    DIO_NUM = 8
    DIO_SRC_NUM = 0x50
    DIO_SRC_MASK = 0x7F
    DIO_SRC_INVERT = (1 << 7)
    DIO_SOURCE_MASK = DIO_SRC_MASK | DIO_SRC_INVERT

    # Signal constants
    SIGNAL_NUM = 0x40
    SIGNAL_MASK = 0x3F
    SIGNAL_INVERT = (1 << 7)
    SOURCE_MASK = SIGNAL_MASK | SIGNAL_INVERT
    SIGNAL_NUM_BYTE = (SIGNAL_NUM + 7) // 8

    # Sensor / Fan constants
    SEN_COUNT = 3
    FAN_COUNT = 3
    FAN_PWM_MAX = 1000

    # Configuration management constants
    MAX_CONFIG = 500
    CONFIG_NAME_SIZE = 193
    CONFIG_DATA_SIZE = 318
    DEFAULT_DATA_SIZE = 20

    # System identification constants
    CPU_ID = 0x5A3A
    DEVICE_TYPE = 0x0B21
    DATA_STRING_SIZE = 12
    PRODUCT_ID_SIZE = 81

    MAX_STREAM = 16

    def __init__(
        self,
        com: int,
        stream: int = 0,
        log=None,
        idn: str = "",
        dll_path: str | Path | None = None,
        error_codes_path: str | Path | None = None,
    ):
        class_dir = Path(__file__).resolve().parent

        if not sys.platform.startswith("win"):
            raise AMXHDPlatformError(
                "CGC AMX HD is supported only on Windows because it depends on "
                "COM-HVAMX4EDH.dll."
            )

        self.amx_hd_dll_path = Path(dll_path) if dll_path is not None else (
            class_dir / "vendor" / "x64" / "COM-HVAMX4EDH.dll"
        )
        try:
            self.amx_hd_dll = ctypes.WinDLL(str(self.amx_hd_dll_path))
        except OSError as exc:
            raise AMXHDDllLoadError(
                f"Unable to load CGC AMX HD DLL from '{self.amx_hd_dll_path}': {exc}."
                f"{_format_dll_load_hint(self.amx_hd_dll_path)}"
            ) from exc

        err_path = Path(error_codes_path) if error_codes_path is not None else (
            class_dir.parent / "error_codes.json"
        )
        with err_path.open("rb") as f:
            self.err_dict = json.load(f)

        self.com = int(com)
        self.stream = int(stream)
        self.log = log
        self.idn = idn

    def describe_error(self, status: int) -> str:
        """Return the vendor message for a driver status code."""
        return self.err_dict.get(str(status), "Unknown status code")

    def format_status(self, status: int) -> str:
        """Return a compact '<code> (<message>)' representation."""
        return f"{status} ({self.describe_error(status)})"

    # =========================================================================
    #     General
    # =========================================================================

    def get_sw_version(self):
        """
        Get the COM-HVAMX4EDH software (DLL) version.

        Returns
        -------
        int
            Software version.

        """
        self.amx_hd_dll.COM_HVAMX4EDH_GetSWVersion.restype = ctypes.c_uint16
        version = self.amx_hd_dll.COM_HVAMX4EDH_GetSWVersion()
        return version

    def open_port(self, com_number, stream_number=None):
        """
        Open communication link to device.

        Parameters
        ----------
        com_number : int
            COM port number (1 = COM1, 2 = COM2, etc.).
        stream_number : int, optional
            Stream number (default: self.stream).

        Returns
        -------
        int
            Status code.

        """
        if stream_number is None:
            stream_number = self.stream
        status = self.amx_hd_dll.COM_HVAMX4EDH_Open(stream_number, com_number)
        return status

    def close_port(self):
        """
        Close the communication link.

        Returns
        -------
        int
            Status code.

        """
        status = self.amx_hd_dll.COM_HVAMX4EDH_Close(self.stream)
        return status

    def set_comspeed(self, baud_rate):
        """
        Set communication speed.

        Parameters
        ----------
        baud_rate : int
            Baud rate value (usually set to max: 230400).

        Returns
        -------
        tuple
            (status, actual_baud_rate).

        """
        baud_rate_ref = ctypes.c_uint(baud_rate)
        status = self.amx_hd_dll.COM_HVAMX4EDH_SetBaudRate(
            self.stream, ctypes.byref(baud_rate_ref)
        )
        return status, baud_rate_ref.value

    def purge(self):
        """
        Clear data buffers for the stream.

        Returns
        -------
        int
            Status code.

        """
        status = self.amx_hd_dll.COM_HVAMX4EDH_Purge(self.stream)
        return status

    def device_purge(self):
        """
        Clear output data buffer of the device.

        Returns
        -------
        tuple
            (status, empty) where empty is True if buffer is empty.

        """
        empty = ctypes.c_bool()
        status = self.amx_hd_dll.COM_HVAMX4EDH_DevicePurge(
            self.stream, ctypes.byref(empty)
        )
        return status, empty.value

    def get_buffer_state(self):
        """
        Return True if the input data buffer of the device is empty.

        Returns
        -------
        tuple
            (status, empty).

        """
        empty = ctypes.c_bool()
        status = self.amx_hd_dll.COM_HVAMX4EDH_GetBufferState(
            self.stream, ctypes.byref(empty)
        )
        return status, empty.value

    # =========================================================================
    #     Device Control
    # =========================================================================

    def get_device_state(self):
        """
        Get the device status (main state, device state flags, temperature state flags).

        Returns
        -------
        tuple
            (status, main_state_hex, main_state_name, dev_state_hex,
             dev_state_names, temp_state_hex, temp_state_names).

        """
        main_state = ctypes.c_uint16()
        device_state = ctypes.c_uint16()
        temp_state = ctypes.c_uint16()
        status = self.amx_hd_dll.COM_HVAMX4EDH_GetDeviceState(
            self.stream,
            ctypes.byref(main_state),
            ctypes.byref(device_state),
            ctypes.byref(temp_state),
        )
        main_val = main_state.value
        main_name = self.MAIN_STATE.get(
            main_val, f'UNKNOWN_STATE_0x{main_val:04X}'
        )

        dev_val = device_state.value
        dev_names = []
        if dev_val == 0:
            dev_names.append('DEVST_OK')
        else:
            for flag, name in self.DEVICE_STATE.items():
                if dev_val & flag:
                    dev_names.append(name)

        temp_val = temp_state.value
        temp_names = []
        if temp_val == 0:
            temp_names.append('TMPST_OK')
        else:
            for flag, name in self.TEMPERATURE_STATE.items():
                if temp_val & flag:
                    temp_names.append(name)

        return (
            status,
            hex(main_val), main_name,
            hex(dev_val), dev_names,
            hex(temp_val), temp_names,
        )

    def get_housekeeping(self):
        """
        Get the housekeeping data.

        Returns
        -------
        tuple
            (status, volt_12v, volt_fans, volt_5v0, volt_3v3, volt_3v3p,
             volt_2v5p, volt_vc, temp_cpu).

        """
        volt_12v = ctypes.c_double()
        volt_fans = ctypes.c_double()
        volt_5v0 = ctypes.c_double()
        volt_3v3 = ctypes.c_double()
        volt_3v3p = ctypes.c_double()
        volt_2v5p = ctypes.c_double()
        volt_vc = ctypes.c_double()
        temp_cpu = ctypes.c_double()

        status = self.amx_hd_dll.COM_HVAMX4EDH_GetHousekeeping(
            self.stream,
            ctypes.byref(volt_12v),
            ctypes.byref(volt_fans),
            ctypes.byref(volt_5v0),
            ctypes.byref(volt_3v3),
            ctypes.byref(volt_3v3p),
            ctypes.byref(volt_2v5p),
            ctypes.byref(volt_vc),
            ctypes.byref(temp_cpu),
        )
        return (
            status,
            volt_12v.value, volt_fans.value, volt_5v0.value,
            volt_3v3.value, volt_3v3p.value, volt_2v5p.value,
            volt_vc.value, temp_cpu.value,
        )

    def get_sensor_data(self):
        """
        Get sensor data (3 temperature sensors).

        Returns
        -------
        tuple
            (status, temp0, temp1, temp2).

        """
        temperature = (ctypes.c_double * self.SEN_COUNT)()
        status = self.amx_hd_dll.COM_HVAMX4EDH_GetSensorData(self.stream, temperature)
        return status, temperature[0], temperature[1], temperature[2]

    def get_fan_speed(self, fan_number):
        """
        Get speed of a specific fan.

        Parameters
        ----------
        fan_number : int
            Fan number (0-2).

        Returns
        -------
        tuple
            (status, fan_rpm).

        """
        fan_rpm = ctypes.c_uint16()
        status = self.amx_hd_dll.COM_HVAMX4EDH_GetFanSpeed(
            self.stream, fan_number, ctypes.byref(fan_rpm)
        )
        return status, fan_rpm.value

    def get_fan_data(self):
        """
        Get fan data (3 fans).

        Returns
        -------
        tuple
            (status, enabled, failed, set_rpm, measured_rpm, pwm).

        """
        enabled = (ctypes.c_bool * self.FAN_COUNT)()
        failed = (ctypes.c_bool * self.FAN_COUNT)()
        set_rpm = (ctypes.c_uint16 * self.FAN_COUNT)()
        measured_rpm = (ctypes.c_uint16 * self.FAN_COUNT)()
        pwm = (ctypes.c_uint16 * self.FAN_COUNT)()

        status = self.amx_hd_dll.COM_HVAMX4EDH_GetFanData(
            self.stream, enabled, failed, set_rpm, measured_rpm, pwm
        )
        return (
            status,
            [enabled[i] for i in range(self.FAN_COUNT)],
            [failed[i] for i in range(self.FAN_COUNT)],
            [set_rpm[i] for i in range(self.FAN_COUNT)],
            [measured_rpm[i] for i in range(self.FAN_COUNT)],
            [pwm[i] for i in range(self.FAN_COUNT)],
        )

    def get_led_data(self):
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

        status = self.amx_hd_dll.COM_HVAMX4EDH_GetLEDData(
            self.stream, ctypes.byref(red), ctypes.byref(green), ctypes.byref(blue)
        )
        return status, red.value, green.value, blue.value

    # =========================================================================
    #     Clock System
    # =========================================================================

    def get_clock_count(self):
        """
        Get number of clocks (should return CLOCK_NUM=4).

        Returns
        -------
        tuple
            (status, count).

        """
        count = ctypes.c_uint()
        status = self.amx_hd_dll.COM_HVAMX4EDH_GetClockCount(
            self.stream, ctypes.byref(count)
        )
        return status, count.value

    def get_clock_source(self, clock):
        """
        Get signal source of specified clock.

        Parameters
        ----------
        clock : int
            Clock number (0 to CLOCK_NUM-1).

        Returns
        -------
        tuple
            (status, source).

        """
        source = ctypes.c_ubyte()
        status = self.amx_hd_dll.COM_HVAMX4EDH_GetClockSource(
            self.stream, clock, ctypes.byref(source)
        )
        return status, source.value

    def set_clock_source(self, clock, source):
        """
        Set signal source of specified clock.

        Parameters
        ----------
        clock : int
            Clock number (0 to CLOCK_NUM-1).
        source : int
            Source value (use CLOCK_SRC_MASK for selection, CLOCK_SRC_INVERT to negate).

        Returns
        -------
        int
            Status code.

        """
        status = self.amx_hd_dll.COM_HVAMX4EDH_SetClockSource(
            self.stream, clock, ctypes.c_ubyte(source)
        )
        return status

    # =========================================================================
    #     PLL System
    # =========================================================================

    def get_pll_count(self):
        """
        Get number of PLLs (should return PLL_NUM=4).

        Returns
        -------
        tuple
            (status, count).

        """
        count = ctypes.c_uint()
        status = self.amx_hd_dll.COM_HVAMX4EDH_GetPllCount(
            self.stream, ctypes.byref(count)
        )
        return status, count.value

    def get_pll_source(self, pll):
        """
        Get signal source of specified PLL.

        Parameters
        ----------
        pll : int
            PLL number (0 to PLL_NUM-1).

        Returns
        -------
        tuple
            (status, source).

        """
        source = ctypes.c_ubyte()
        status = self.amx_hd_dll.COM_HVAMX4EDH_GetPllSource(
            self.stream, pll, ctypes.byref(source)
        )
        return status, source.value

    def set_pll_source(self, pll, source):
        """
        Set signal source of specified PLL.

        Parameters
        ----------
        pll : int
            PLL number (0 to PLL_NUM-1).
        source : int
            Source value.

        Returns
        -------
        int
            Status code.

        """
        status = self.amx_hd_dll.COM_HVAMX4EDH_SetPllSource(
            self.stream, pll, ctypes.c_ubyte(source)
        )
        return status

    def get_pll_ref_div(self, pll):
        """
        Get reference divider of specified PLL.

        Parameters
        ----------
        pll : int
            PLL number (0 to PLL_NUM-1).

        Returns
        -------
        tuple
            (status, divider).

        """
        divider = ctypes.c_uint16()
        status = self.amx_hd_dll.COM_HVAMX4EDH_GetPllRefDiv(
            self.stream, pll, ctypes.byref(divider)
        )
        return status, divider.value

    def set_pll_ref_div(self, pll, divider):
        """
        Set reference divider of specified PLL.

        Parameters
        ----------
        pll : int
            PLL number (0 to PLL_NUM-1).
        divider : int
            Divider value (PLL_REF_DIV_MIN to PLL_REF_DIV_MAX).

        Returns
        -------
        int
            Status code.

        """
        status = self.amx_hd_dll.COM_HVAMX4EDH_SetPllRefDiv(
            self.stream, pll, ctypes.c_uint16(divider)
        )
        return status

    def get_pll_fdbk_div(self, pll):
        """
        Get feedback divider of specified PLL.

        Parameters
        ----------
        pll : int
            PLL number (0 to PLL_NUM-1).

        Returns
        -------
        tuple
            (status, divider).

        """
        divider = ctypes.c_uint16()
        status = self.amx_hd_dll.COM_HVAMX4EDH_GetPllFdbkDiv(
            self.stream, pll, ctypes.byref(divider)
        )
        return status, divider.value

    def set_pll_fdbk_div(self, pll, divider):
        """
        Set feedback divider of specified PLL.

        Parameters
        ----------
        pll : int
            PLL number (0 to PLL_NUM-1).
        divider : int
            Divider value (PLL_FB_DIV_MIN to PLL_FB_DIV_MAX, excluding NA values).

        Returns
        -------
        int
            Status code.

        """
        status = self.amx_hd_dll.COM_HVAMX4EDH_SetPllFdbkDiv(
            self.stream, pll, ctypes.c_uint16(divider)
        )
        return status

    def get_pll_post_div1(self, pll):
        """
        Get post divider #1 of specified PLL.

        Parameters
        ----------
        pll : int
            PLL number (0 to PLL_NUM-1).

        Returns
        -------
        tuple
            (status, divider).

        """
        divider = ctypes.c_ubyte()
        status = self.amx_hd_dll.COM_HVAMX4EDH_GetPllPostDiv1(
            self.stream, pll, ctypes.byref(divider)
        )
        return status, divider.value

    def set_pll_post_div1(self, pll, divider):
        """
        Set post divider #1 of specified PLL.

        Parameters
        ----------
        pll : int
            PLL number (0 to PLL_NUM-1).
        divider : int
            Divider value (PLL_POST_DIV1_MIN to PLL_POST_DIV1_MAX).

        Returns
        -------
        int
            Status code.

        """
        status = self.amx_hd_dll.COM_HVAMX4EDH_SetPllPostDiv1(
            self.stream, pll, ctypes.c_ubyte(divider)
        )
        return status

    def get_pll_post_div2(self, pll):
        """
        Get post divider #2 of specified PLL.

        Parameters
        ----------
        pll : int
            PLL number (0 to PLL_NUM-1).

        Returns
        -------
        tuple
            (status, divider).

        """
        divider = ctypes.c_ubyte()
        status = self.amx_hd_dll.COM_HVAMX4EDH_GetPllPostDiv2(
            self.stream, pll, ctypes.byref(divider)
        )
        return status, divider.value

    def set_pll_post_div2(self, pll, divider):
        """
        Set post divider #2 of specified PLL.

        Parameters
        ----------
        pll : int
            PLL number (0 to PLL_NUM-1).
        divider : int
            Divider value (PLL_POST_DIV2_MIN to PLL_POST_DIV2_MAX).

        Returns
        -------
        int
            Status code.

        """
        status = self.amx_hd_dll.COM_HVAMX4EDH_SetPllPostDiv2(
            self.stream, pll, ctypes.c_ubyte(divider)
        )
        return status

    def get_pll_post_div3(self, pll):
        """
        Get post divider #3 of specified PLL.

        Parameters
        ----------
        pll : int
            PLL number (0 to PLL_NUM-1).

        Returns
        -------
        tuple
            (status, divider) where divider is 0=1, 1=2, 2=4, 3=8.

        """
        divider = ctypes.c_ubyte()
        status = self.amx_hd_dll.COM_HVAMX4EDH_GetPllPostDiv3(
            self.stream, pll, ctypes.byref(divider)
        )
        return status, divider.value

    def set_pll_post_div3(self, pll, divider):
        """
        Set post divider #3 of specified PLL.

        Parameters
        ----------
        pll : int
            PLL number (0 to PLL_NUM-1).
        divider : int
            Divider selector (0=1, 1=2, 2=4, 3=8).

        Returns
        -------
        int
            Status code.

        """
        status = self.amx_hd_dll.COM_HVAMX4EDH_SetPllPostDiv3(
            self.stream, pll, ctypes.c_ubyte(divider)
        )
        return status

    def get_pll_power_down(self, pll):
        """
        Get power-down mode of specified PLL.

        Parameters
        ----------
        pll : int
            PLL number (0 to PLL_NUM-1).

        Returns
        -------
        tuple
            (status, power_down).

        """
        power_down = ctypes.c_bool()
        status = self.amx_hd_dll.COM_HVAMX4EDH_GetPllPowerDown(
            self.stream, pll, ctypes.byref(power_down)
        )
        return status, power_down.value

    def set_pll_power_down(self, pll, power_down):
        """
        Set power-down mode of specified PLL.

        Parameters
        ----------
        pll : int
            PLL number (0 to PLL_NUM-1).
        power_down : bool
            Power-down state.

        Returns
        -------
        int
            Status code.

        """
        status = self.amx_hd_dll.COM_HVAMX4EDH_SetPllPowerDown(
            self.stream, pll, ctypes.c_bool(power_down)
        )
        return status

    def get_pll_cp_current(self, pll):
        """
        Get charge-pump current of specified PLL.

        Parameters
        ----------
        pll : int
            PLL number (0 to PLL_NUM-1).

        Returns
        -------
        tuple
            (status, cp_current).

        """
        cp_current = ctypes.c_ubyte()
        status = self.amx_hd_dll.COM_HVAMX4EDH_GetPllCpCurrent(
            self.stream, pll, ctypes.byref(cp_current)
        )
        return status, cp_current.value

    def set_pll_cp_current(self, pll, cp_current):
        """
        Set charge-pump current of specified PLL.

        Parameters
        ----------
        pll : int
            PLL number (0 to PLL_NUM-1).
        cp_current : int
            Charge-pump current (0=2uA, 1=4.5uA, 2=11uA, 3=22.5uA).

        Returns
        -------
        int
            Status code.

        """
        status = self.amx_hd_dll.COM_HVAMX4EDH_SetPllCpCurrent(
            self.stream, pll, ctypes.c_ubyte(cp_current)
        )
        return status

    def get_pll_lf_resistor(self, pll):
        """
        Get loop-filter resistor of specified PLL.

        Parameters
        ----------
        pll : int
            PLL number (0 to PLL_NUM-1).

        Returns
        -------
        tuple
            (status, lf_resistor).

        """
        lf_resistor = ctypes.c_int()
        status = self.amx_hd_dll.COM_HVAMX4EDH_GetPllLfResistor(
            self.stream, pll, ctypes.byref(lf_resistor)
        )
        return status, lf_resistor.value

    def set_pll_lf_resistor(self, pll, lf_resistor):
        """
        Set loop-filter resistor of specified PLL.

        Parameters
        ----------
        pll : int
            PLL number (0 to PLL_NUM-1).
        lf_resistor : int
            Resistor selector (0=400k, 1=133k, 2=30k, 3=12k).

        Returns
        -------
        int
            Status code.

        """
        status = self.amx_hd_dll.COM_HVAMX4EDH_SetPllLfResistor(
            self.stream, pll, ctypes.c_int(lf_resistor)
        )
        return status

    def get_pll_lf_capacitor(self, pll):
        """
        Get loop-filter capacitor of specified PLL.

        Parameters
        ----------
        pll : int
            PLL number (0 to PLL_NUM-1).

        Returns
        -------
        tuple
            (status, lf_capacitor).

        """
        lf_capacitor = ctypes.c_int()
        status = self.amx_hd_dll.COM_HVAMX4EDH_GetPllLfCapacitor(
            self.stream, pll, ctypes.byref(lf_capacitor)
        )
        return status, lf_capacitor.value

    def set_pll_lf_capacitor(self, pll, lf_capacitor):
        """
        Set loop-filter capacitor of specified PLL.

        Parameters
        ----------
        pll : int
            PLL number (0 to PLL_NUM-1).
        lf_capacitor : int
            Capacitor selector (0=185pF, 1=500pF).

        Returns
        -------
        int
            Status code.

        """
        status = self.amx_hd_dll.COM_HVAMX4EDH_SetPllLfCapacitor(
            self.stream, pll, ctypes.c_int(lf_capacitor)
        )
        return status

    # =========================================================================
    #     Divider System
    # =========================================================================

    def get_divider_count(self):
        """
        Get number of dividers (should return DIV_NUM=4).

        Returns
        -------
        tuple
            (status, count).

        """
        count = ctypes.c_uint()
        status = self.amx_hd_dll.COM_HVAMX4EDH_GetDividerCount(
            self.stream, ctypes.byref(count)
        )
        return status, count.value

    def get_divider_period(self, divider):
        """
        Get period (dividing factor) of specified divider.

        Parameters
        ----------
        divider : int
            Divider number (0 to DIV_NUM-1).

        Returns
        -------
        tuple
            (status, period).

        """
        period = ctypes.c_ubyte()
        status = self.amx_hd_dll.COM_HVAMX4EDH_GetDividerPeriod(
            self.stream, divider, ctypes.byref(period)
        )
        return status, period.value

    def set_divider_period(self, divider, period):
        """
        Set period (dividing factor) of specified divider.

        Parameters
        ----------
        divider : int
            Divider number (0 to DIV_NUM-1).
        period : int
            Period value.

        Returns
        -------
        int
            Status code.

        """
        status = self.amx_hd_dll.COM_HVAMX4EDH_SetDividerPeriod(
            self.stream, divider, ctypes.c_ubyte(period)
        )
        return status

    # =========================================================================
    #     Counter System
    # =========================================================================

    def get_counter_count(self):
        """
        Get number of counters (should return CNT_NUM=4).

        Returns
        -------
        tuple
            (status, count).

        """
        count = ctypes.c_uint()
        status = self.amx_hd_dll.COM_HVAMX4EDH_GetCounterCount(
            self.stream, ctypes.byref(count)
        )
        return status, count.value

    def get_counter_period(self, counter):
        """
        Get period (dividing factor) of specified counter.

        Parameters
        ----------
        counter : int
            Counter number (0 to CNT_NUM-1).

        Returns
        -------
        tuple
            (status, period).

        """
        period = ctypes.c_ubyte()
        status = self.amx_hd_dll.COM_HVAMX4EDH_GetCounterPeriod(
            self.stream, counter, ctypes.byref(period)
        )
        return status, period.value

    def set_counter_period(self, counter, period):
        """
        Set period (dividing factor) of specified counter.

        Parameters
        ----------
        counter : int
            Counter number (0 to CNT_NUM-1).
        period : int
            Period value (CNT_PER_MIN to CNT_PER_MAX).

        Returns
        -------
        int
            Status code.

        """
        status = self.amx_hd_dll.COM_HVAMX4EDH_SetCounterPeriod(
            self.stream, counter, ctypes.c_ubyte(period)
        )
        return status

    # =========================================================================
    #     Oscillator Management
    # =========================================================================

    def get_oscillator_count(self):
        """
        Get number of implemented oscillators.

        Returns
        -------
        tuple
            (status, count).

        """
        count = ctypes.c_uint()
        status = self.amx_hd_dll.COM_HVAMX4EDH_GetOscillatorCount(
            self.stream, ctypes.byref(count)
        )
        return status, count.value

    def get_oscillator_period(self, oscillator):
        """
        Get oscillator period.

        Parameters
        ----------
        oscillator : int
            Oscillator number.

        Returns
        -------
        tuple
            (status, period).

        """
        period = ctypes.c_uint32()
        status = self.amx_hd_dll.COM_HVAMX4EDH_GetOscillatorPeriod(
            self.stream, oscillator, ctypes.byref(period)
        )
        return status, period.value

    def set_oscillator_period(self, oscillator, period):
        """
        Set oscillator period.

        Parameters
        ----------
        oscillator : int
            Oscillator number.
        period : int
            Oscillator period value.

        Returns
        -------
        int
            Status code.

        """
        status = self.amx_hd_dll.COM_HVAMX4EDH_SetOscillatorPeriod(
            self.stream, oscillator, ctypes.c_uint32(period)
        )
        return status

    # =========================================================================
    #     Timer Management
    # =========================================================================

    def get_timer_count(self):
        """
        Get number of implemented timers.

        Returns
        -------
        tuple
            (status, count).

        """
        count = ctypes.c_uint()
        status = self.amx_hd_dll.COM_HVAMX4EDH_GetTimerCount(
            self.stream, ctypes.byref(count)
        )
        return status, count.value

    def get_timer_delay(self, timer):
        """
        Get pulse delay of specified timer.

        Parameters
        ----------
        timer : int
            Timer number.

        Returns
        -------
        tuple
            (status, delay).

        """
        delay = ctypes.c_uint32()
        status = self.amx_hd_dll.COM_HVAMX4EDH_GetTimerDelay(
            self.stream, timer, ctypes.byref(delay)
        )
        return status, delay.value

    def set_timer_delay(self, timer, delay):
        """
        Set pulse delay of specified timer.

        Parameters
        ----------
        timer : int
            Timer number.
        delay : int
            Delay value.

        Returns
        -------
        int
            Status code.

        """
        status = self.amx_hd_dll.COM_HVAMX4EDH_SetTimerDelay(
            self.stream, timer, ctypes.c_uint32(delay)
        )
        return status

    def get_timer_width(self, timer):
        """
        Get pulse width of specified timer.

        Parameters
        ----------
        timer : int
            Timer number.

        Returns
        -------
        tuple
            (status, width).

        """
        width = ctypes.c_uint32()
        status = self.amx_hd_dll.COM_HVAMX4EDH_GetTimerWidth(
            self.stream, timer, ctypes.byref(width)
        )
        return status, width.value

    def set_timer_width(self, timer, width):
        """
        Set pulse width of specified timer.

        Parameters
        ----------
        timer : int
            Timer number.
        width : int
            Width value.

        Returns
        -------
        int
            Status code.

        """
        status = self.amx_hd_dll.COM_HVAMX4EDH_SetTimerWidth(
            self.stream, timer, ctypes.c_uint32(width)
        )
        return status

    def get_timer_trigger_source(self, timer):
        """
        Get trigger source of specified timer.

        Parameters
        ----------
        timer : int
            Timer number.

        Returns
        -------
        tuple
            (status, trigger_source).

        """
        trigger_source = ctypes.c_ubyte()
        status = self.amx_hd_dll.COM_HVAMX4EDH_GetTimerTriggerSource(
            self.stream, timer, ctypes.byref(trigger_source)
        )
        return status, trigger_source.value

    def set_timer_trigger_source(self, timer, trigger_source):
        """
        Set trigger source of specified timer.

        Parameters
        ----------
        timer : int
            Timer number.
        trigger_source : int
            Trigger source value.

        Returns
        -------
        int
            Status code.

        """
        status = self.amx_hd_dll.COM_HVAMX4EDH_SetTimerTriggerSource(
            self.stream, timer, ctypes.c_ubyte(trigger_source)
        )
        return status

    def get_timer_burst(self, timer):
        """
        Get burst size of specified timer.

        Parameters
        ----------
        timer : int
            Timer number.

        Returns
        -------
        tuple
            (status, burst).

        """
        burst = ctypes.c_uint32()
        status = self.amx_hd_dll.COM_HVAMX4EDH_GetTimerBurst(
            self.stream, timer, ctypes.byref(burst)
        )
        return status, burst.value

    def set_timer_burst(self, timer, burst):
        """
        Set burst size of specified timer.

        Parameters
        ----------
        timer : int
            Timer number.
        burst : int
            Burst size (max TIMER_MAX_BURST).

        Returns
        -------
        int
            Status code.

        """
        status = self.amx_hd_dll.COM_HVAMX4EDH_SetTimerBurst(
            self.stream, timer, ctypes.c_uint32(burst)
        )
        return status

    def get_timer_stop_source(self, timer):
        """
        Get stop source of specified timer.

        Parameters
        ----------
        timer : int
            Timer number.

        Returns
        -------
        tuple
            (status, stop_source).

        """
        stop_source = ctypes.c_ubyte()
        status = self.amx_hd_dll.COM_HVAMX4EDH_GetTimerStopSource(
            self.stream, timer, ctypes.byref(stop_source)
        )
        return status, stop_source.value

    def set_timer_stop_source(self, timer, stop_source):
        """
        Set stop source of specified timer.

        Parameters
        ----------
        timer : int
            Timer number.
        stop_source : int
            Stop source value.

        Returns
        -------
        int
            Status code.

        """
        status = self.amx_hd_dll.COM_HVAMX4EDH_SetTimerStopSource(
            self.stream, timer, ctypes.c_ubyte(stop_source)
        )
        return status

    # =========================================================================
    #     Mapping Engines
    # =========================================================================

    def get_mapping_engine_input_source(self, mapping_engine):
        """
        Get input signals of the specified mapping engine.

        Parameters
        ----------
        mapping_engine : int
            Mapping engine number.

        Returns
        -------
        tuple
            (status, input_sources) where input_sources is a list of
            MAPPING_INPUT_NUM values.

        """
        input_source = (ctypes.c_ubyte * self.MAPPING_INPUT_NUM)()
        status = self.amx_hd_dll.COM_HVAMX4EDH_GetMappingEngineInputSource(
            self.stream, mapping_engine, input_source
        )
        return status, [input_source[i] for i in range(self.MAPPING_INPUT_NUM)]

    def set_mapping_engine_input_source(self, mapping_engine, input_sources):
        """
        Set input signals of the specified mapping engine.

        Parameters
        ----------
        mapping_engine : int
            Mapping engine number.
        input_sources : list of int
            List of MAPPING_INPUT_NUM source values.

        Returns
        -------
        int
            Status code.

        """
        input_source = (ctypes.c_ubyte * self.MAPPING_INPUT_NUM)(
            *input_sources[:self.MAPPING_INPUT_NUM]
        )
        status = self.amx_hd_dll.COM_HVAMX4EDH_SetMappingEngineInputSource(
            self.stream, mapping_engine, input_source
        )
        return status

    def get_mapping_engine_output_value(self, mapping_engine):
        """
        Get the enable bit and output values of the specified mapping engine.

        Parameters
        ----------
        mapping_engine : int
            Mapping engine number.

        Returns
        -------
        tuple
            (status, enable, output_values) where output_values is a list of
            MAPPING_STATE_NUM values.

        """
        enable = ctypes.c_bool()
        output_value = (ctypes.c_ubyte * self.MAPPING_STATE_NUM)()
        status = self.amx_hd_dll.COM_HVAMX4EDH_GetMappingEngineOutputValue(
            self.stream, mapping_engine, ctypes.byref(enable), output_value
        )
        return (
            status,
            enable.value,
            [output_value[i] for i in range(self.MAPPING_STATE_NUM)],
        )

    def set_mapping_engine_output_value(self, mapping_engine, enable, output_values):
        """
        Set the enable bit and output values of the specified mapping engine.

        Parameters
        ----------
        mapping_engine : int
            Mapping engine number.
        enable : bool
            Enable bit.
        output_values : list of int
            List of MAPPING_STATE_NUM output values.

        Returns
        -------
        int
            Status code.

        """
        output_value = (ctypes.c_ubyte * self.MAPPING_STATE_NUM)(
            *output_values[:self.MAPPING_STATE_NUM]
        )
        status = self.amx_hd_dll.COM_HVAMX4EDH_SetMappingEngineOutputValue(
            self.stream, mapping_engine, ctypes.c_bool(enable), output_value
        )
        return status

    # =========================================================================
    #     Switch Management
    # =========================================================================

    def get_switch_trigger_source(self, switch_no):
        """
        Get source of specified switch trigger input.

        Parameters
        ----------
        switch_no : int
            Switch number (0 to SWITCH_NUM-1).

        Returns
        -------
        tuple
            (status, trigger_source).

        """
        trigger_source = ctypes.c_ubyte()
        status = self.amx_hd_dll.COM_HVAMX4EDH_GetSwitchTriggerSource(
            self.stream, switch_no, ctypes.byref(trigger_source)
        )
        return status, trigger_source.value

    def set_switch_trigger_source(self, switch_no, trigger_source):
        """
        Set source of specified switch trigger input.

        Parameters
        ----------
        switch_no : int
            Switch number (0 to SWITCH_NUM-1).
        trigger_source : int
            Trigger source value.

        Returns
        -------
        int
            Status code.

        """
        status = self.amx_hd_dll.COM_HVAMX4EDH_SetSwitchTriggerSource(
            self.stream, switch_no, ctypes.c_ubyte(trigger_source)
        )
        return status

    def get_switch_enable_source(self, switch_no):
        """
        Get source of specified switch enable input.

        Parameters
        ----------
        switch_no : int
            Switch number (0 to SWITCH_NUM-1).

        Returns
        -------
        tuple
            (status, enable_source).

        """
        enable_source = ctypes.c_ubyte()
        status = self.amx_hd_dll.COM_HVAMX4EDH_GetSwitchEnableSource(
            self.stream, switch_no, ctypes.byref(enable_source)
        )
        return status, enable_source.value

    def set_switch_enable_source(self, switch_no, enable_source):
        """
        Set source of specified switch enable input.

        Parameters
        ----------
        switch_no : int
            Switch number (0 to SWITCH_NUM-1).
        enable_source : int
            Enable source value.

        Returns
        -------
        int
            Status code.

        """
        status = self.amx_hd_dll.COM_HVAMX4EDH_SetSwitchEnableSource(
            self.stream, switch_no, ctypes.c_ubyte(enable_source)
        )
        return status

    def get_switch_delay(self, switch_no):
        """
        Get coarse delays of specified switch trigger.

        Parameters
        ----------
        switch_no : int
            Switch number (0 to SWITCH_NUM-1).

        Returns
        -------
        tuple
            (status, rise_delay, fall_delay).

        """
        rise_delay = ctypes.c_ubyte()
        fall_delay = ctypes.c_ubyte()
        status = self.amx_hd_dll.COM_HVAMX4EDH_GetSwitchDelay(
            self.stream, switch_no, ctypes.byref(rise_delay), ctypes.byref(fall_delay)
        )
        return status, rise_delay.value, fall_delay.value

    def set_switch_delay(self, switch_no, rise_delay, fall_delay):
        """
        Set coarse delays of specified switch trigger.

        Parameters
        ----------
        switch_no : int
            Switch number (0 to SWITCH_NUM-1).
        rise_delay : int
            Rise delay (0 to SWITCH_DELAY_MAX-1).
        fall_delay : int
            Fall delay (0 to SWITCH_DELAY_MAX-1).

        Returns
        -------
        int
            Status code.

        """
        status = self.amx_hd_dll.COM_HVAMX4EDH_SetSwitchDelay(
            self.stream, switch_no, ctypes.c_ubyte(rise_delay), ctypes.c_ubyte(fall_delay)
        )
        return status

    def get_switch_rise_delay_fine(self, switch_no):
        """
        Get fine rise delay of specified switch trigger.

        Parameters
        ----------
        switch_no : int
            Switch number (0 to SWITCH_NUM-1).

        Returns
        -------
        tuple
            (status, delay).

        """
        delay = ctypes.c_uint16()
        status = self.amx_hd_dll.COM_HVAMX4EDH_GetSwitchRiseDelayFine(
            self.stream, switch_no, ctypes.byref(delay)
        )
        return status, delay.value

    def set_switch_rise_delay_fine(self, switch_no, delay):
        """
        Set fine rise delay of specified switch trigger.

        Parameters
        ----------
        switch_no : int
            Switch number (0 to SWITCH_NUM-1).
        delay : int
            Fine delay value (0 to SWITCH_DELAY_FINE_MAX).

        Returns
        -------
        int
            Status code.

        """
        status = self.amx_hd_dll.COM_HVAMX4EDH_SetSwitchRiseDelayFine(
            self.stream, switch_no, ctypes.c_uint16(delay)
        )
        return status

    def get_switch_fall_delay_fine(self, switch_no):
        """
        Get fine fall delay of specified switch trigger.

        Parameters
        ----------
        switch_no : int
            Switch number (0 to SWITCH_NUM-1).

        Returns
        -------
        tuple
            (status, delay).

        """
        delay = ctypes.c_uint16()
        status = self.amx_hd_dll.COM_HVAMX4EDH_GetSwitchFallDelayFine(
            self.stream, switch_no, ctypes.byref(delay)
        )
        return status, delay.value

    def set_switch_fall_delay_fine(self, switch_no, delay):
        """
        Set fine fall delay of specified switch trigger.

        Parameters
        ----------
        switch_no : int
            Switch number (0 to SWITCH_NUM-1).
        delay : int
            Fine delay value (0 to SWITCH_DELAY_FINE_MAX).

        Returns
        -------
        int
            Status code.

        """
        status = self.amx_hd_dll.COM_HVAMX4EDH_SetSwitchFallDelayFine(
            self.stream, switch_no, ctypes.c_uint16(delay)
        )
        return status

    # =========================================================================
    #     Digital I/O
    # =========================================================================

    def get_digital_io_config(self, digital_io):
        """
        Get configuration of specified digital I/O.

        Parameters
        ----------
        digital_io : int
            Digital I/O number (0 to DIO_NUM-1).

        Returns
        -------
        tuple
            (status, config) where config is DIO_CFG_IN, DIO_CFG_OUT, or DIO_CFG_IN_TERM.

        """
        config = ctypes.c_ubyte()
        status = self.amx_hd_dll.COM_HVAMX4EDH_GetDigitalIOConfig(
            self.stream, digital_io, ctypes.byref(config)
        )
        return status, config.value

    def set_digital_io_config(self, digital_io, config):
        """
        Set configuration of specified digital I/O.

        Parameters
        ----------
        digital_io : int
            Digital I/O number (0 to DIO_NUM-1).
        config : int
            Configuration (DIO_CFG_IN=0, DIO_CFG_OUT=1, DIO_CFG_IN_TERM=2).

        Returns
        -------
        int
            Status code.

        """
        status = self.amx_hd_dll.COM_HVAMX4EDH_SetDigitalIOConfig(
            self.stream, digital_io, ctypes.c_ubyte(config)
        )
        return status

    def get_digital_io_signal_source(self, digital_io):
        """
        Get signal source of specified digital I/O.

        Parameters
        ----------
        digital_io : int
            Digital I/O number (0 to DIO_NUM-1).

        Returns
        -------
        tuple
            (status, signal_source).

        """
        signal_source = ctypes.c_ubyte()
        status = self.amx_hd_dll.COM_HVAMX4EDH_GetDigitalIOSignalSource(
            self.stream, digital_io, ctypes.byref(signal_source)
        )
        return status, signal_source.value

    def set_digital_io_signal_source(self, digital_io, signal_source):
        """
        Set signal source of specified digital I/O.

        Parameters
        ----------
        digital_io : int
            Digital I/O number (0 to DIO_NUM-1).
        signal_source : int
            Signal source value.

        Returns
        -------
        int
            Status code.

        """
        status = self.amx_hd_dll.COM_HVAMX4EDH_SetDigitalIOSignalSource(
            self.stream, digital_io, ctypes.c_ubyte(signal_source)
        )
        return status

    # =========================================================================
    #     Signal States
    # =========================================================================

    def get_signal_values(self):
        """
        Get signal states as individual boolean values.

        Returns
        -------
        tuple
            (status, signal_values) where signal_values is a list of
            SIGNAL_NUM boolean values.

        """
        signal_values = (ctypes.c_bool * self.SIGNAL_NUM)()
        status = self.amx_hd_dll.COM_HVAMX4EDH_GetSignalValues(
            self.stream, signal_values
        )
        return status, [signal_values[i] for i in range(self.SIGNAL_NUM)]

    def get_signals(self):
        """
        Get signal states as packed bytes.

        Returns
        -------
        tuple
            (status, signals) where signals is a list of SIGNAL_NUM_BYTE bytes.

        """
        signals = (ctypes.c_ubyte * self.SIGNAL_NUM_BYTE)()
        status = self.amx_hd_dll.COM_HVAMX4EDH_GetSignals(self.stream, signals)
        return status, [signals[i] for i in range(self.SIGNAL_NUM_BYTE)]

    # =========================================================================
    #     Controller State & Configuration
    # =========================================================================

    def get_state(self):
        """
        Get device state and configuration bits.

        Returns
        -------
        tuple
            (status, state, config, state_names) where state is a DWORD of
            combined state bits and config is a WORD of configuration bits.

        """
        state = ctypes.c_uint32()
        config = ctypes.c_uint16()
        status = self.amx_hd_dll.COM_HVAMX4EDH_GetState(
            self.stream, ctypes.byref(state), ctypes.byref(config)
        )
        state_value = state.value

        active_states = []
        all_bits = {**self.CONTROLLER_STATE, **self.EXTENDED_STATE}
        for flag, name in all_bits.items():
            if state_value & flag:
                active_states.append(name)

        return status, hex(state_value), config.value, active_states

    def set_config(self, config):
        """
        Set device configuration.

        Parameters
        ----------
        config : int
            Configuration word value.

        Returns
        -------
        int
            Status code.

        """
        status = self.amx_hd_dll.COM_HVAMX4EDH_SetConfig(
            self.stream, ctypes.c_uint16(config)
        )
        return status

    # =========================================================================
    #     Configuration Management
    # =========================================================================

    def get_device_enable(self):
        """
        Get the enable state of the device.

        Returns
        -------
        tuple
            (status, enable).

        """
        enable = ctypes.c_bool()
        status = self.amx_hd_dll.COM_HVAMX4EDH_GetDeviceEnable(
            self.stream, ctypes.byref(enable)
        )
        return status, enable.value

    def set_device_enable(self, enable):
        """
        Set the enable state of the device.

        Parameters
        ----------
        enable : bool
            Enable state.

        Returns
        -------
        int
            Status code.

        """
        status = self.amx_hd_dll.COM_HVAMX4EDH_SetDeviceEnable(
            self.stream, ctypes.c_bool(enable)
        )
        return status

    def get_interlock_funct(self):
        """
        Get the interlock functionality.

        Returns
        -------
        tuple
            (status, interlock_funct).

        """
        interlock_funct = ctypes.c_bool()
        status = self.amx_hd_dll.COM_HVAMX4EDH_GetInterlockFunct(
            self.stream, ctypes.byref(interlock_funct)
        )
        return status, interlock_funct.value

    def set_interlock_funct(self, interlock_funct):
        """
        Set the interlock functionality.

        Parameters
        ----------
        interlock_funct : bool
            Interlock functionality state.

        Returns
        -------
        int
            Status code.

        """
        status = self.amx_hd_dll.COM_HVAMX4EDH_SetInterlockFunct(
            self.stream, ctypes.c_bool(interlock_funct)
        )
        return status

    def get_current_config(self):
        """
        Get current configuration data.

        Returns
        -------
        tuple
            (status, data) where data is a list of CONFIG_DATA_SIZE bytes.

        """
        data = (ctypes.c_ubyte * self.CONFIG_DATA_SIZE)()
        status = self.amx_hd_dll.COM_HVAMX4EDH_GetCurrentConfig(self.stream, data)
        return status, [data[i] for i in range(self.CONFIG_DATA_SIZE)]

    def set_current_config(self, data):
        """
        Set current configuration data.

        Parameters
        ----------
        data : list of int
            Configuration data (CONFIG_DATA_SIZE bytes).

        Returns
        -------
        int
            Status code.

        """
        data_arr = (ctypes.c_ubyte * self.CONFIG_DATA_SIZE)(
            *data[:self.CONFIG_DATA_SIZE]
        )
        status = self.amx_hd_dll.COM_HVAMX4EDH_SetCurrentConfig(self.stream, data_arr)
        return status

    def get_defaults(self):
        """
        Get default delay data from NVM.

        Returns
        -------
        tuple
            (status, data) where data is a list of DEFAULT_DATA_SIZE bytes.

        """
        data = (ctypes.c_ubyte * self.DEFAULT_DATA_SIZE)()
        status = self.amx_hd_dll.COM_HVAMX4EDH_GetDefaults(self.stream, data)
        return status, [data[i] for i in range(self.DEFAULT_DATA_SIZE)]

    def set_defaults(self, data):
        """
        Set default delay data in NVM.

        Parameters
        ----------
        data : list of int
            Default data (DEFAULT_DATA_SIZE bytes).

        Returns
        -------
        int
            Status code.

        """
        data_arr = (ctypes.c_ubyte * self.DEFAULT_DATA_SIZE)(
            *data[:self.DEFAULT_DATA_SIZE]
        )
        status = self.amx_hd_dll.COM_HVAMX4EDH_SetDefaults(self.stream, data_arr)
        return status

    def save_defaults(self):
        """
        Save default delay data to NVM.

        Returns
        -------
        int
            Status code.

        """
        status = self.amx_hd_dll.COM_HVAMX4EDH_SaveDefaults(self.stream)
        return status

    def load_defaults(self):
        """
        Load default delay data from NVM.

        Returns
        -------
        int
            Status code.

        """
        status = self.amx_hd_dll.COM_HVAMX4EDH_LoadDefaults(self.stream)
        return status

    def get_config_list(self):
        """
        Get configuration list.

        Returns
        -------
        tuple
            (status, active_list, valid_list) where lists contain MAX_CONFIG
            boolean values.

        """
        active = (ctypes.c_bool * self.MAX_CONFIG)()
        valid = (ctypes.c_bool * self.MAX_CONFIG)()
        status = self.amx_hd_dll.COM_HVAMX4EDH_GetConfigList(self.stream, active, valid)
        return (
            status,
            [active[i] for i in range(self.MAX_CONFIG)],
            [valid[i] for i in range(self.MAX_CONFIG)],
        )

    def save_current_config(self, config_number):
        """
        Save current configuration to NVM.

        Parameters
        ----------
        config_number : int
            Configuration number (0 to MAX_CONFIG-1).

        Returns
        -------
        int
            Status code.

        """
        status = self.amx_hd_dll.COM_HVAMX4EDH_SaveCurrentConfig(
            self.stream, config_number
        )
        return status

    def load_current_config(self, config_number):
        """
        Load current configuration from NVM.

        Parameters
        ----------
        config_number : int
            Configuration number (0 to MAX_CONFIG-1).

        Returns
        -------
        int
            Status code.

        """
        status = self.amx_hd_dll.COM_HVAMX4EDH_LoadCurrentConfig(
            self.stream, config_number
        )
        return status

    def get_config_name(self, config_number):
        """
        Get configuration name from NVM.

        Parameters
        ----------
        config_number : int
            Configuration number (0 to MAX_CONFIG-1).

        Returns
        -------
        tuple
            (status, name).

        """
        name = ctypes.create_string_buffer(self.CONFIG_NAME_SIZE)
        status = self.amx_hd_dll.COM_HVAMX4EDH_GetConfigName(
            self.stream, config_number, name
        )
        return status, name.value.decode()

    def set_config_name(self, config_number, name):
        """
        Set configuration name in NVM.

        Parameters
        ----------
        config_number : int
            Configuration number (0 to MAX_CONFIG-1).
        name : str
            Configuration name (max CONFIG_NAME_SIZE-1 characters).

        Returns
        -------
        int
            Status code.

        """
        name_buffer = ctypes.create_string_buffer(
            name.encode(), self.CONFIG_NAME_SIZE
        )
        status = self.amx_hd_dll.COM_HVAMX4EDH_SetConfigName(
            self.stream, config_number, name_buffer
        )
        return status

    def get_config_data(self, config_number):
        """
        Get configuration data from NVM.

        Parameters
        ----------
        config_number : int
            Configuration number (0 to MAX_CONFIG-1).

        Returns
        -------
        tuple
            (status, data) where data is a list of CONFIG_DATA_SIZE bytes.

        """
        data = (ctypes.c_ubyte * self.CONFIG_DATA_SIZE)()
        status = self.amx_hd_dll.COM_HVAMX4EDH_GetConfigData(
            self.stream, config_number, data
        )
        return status, [data[i] for i in range(self.CONFIG_DATA_SIZE)]

    def set_config_data(self, config_number, data):
        """
        Set configuration data in NVM.

        Parameters
        ----------
        config_number : int
            Configuration number (0 to MAX_CONFIG-1).
        data : list of int
            Configuration data (CONFIG_DATA_SIZE bytes).

        Returns
        -------
        int
            Status code.

        """
        data_arr = (ctypes.c_ubyte * self.CONFIG_DATA_SIZE)(
            *data[:self.CONFIG_DATA_SIZE]
        )
        status = self.amx_hd_dll.COM_HVAMX4EDH_SetConfigData(
            self.stream, config_number, data_arr
        )
        return status

    def get_config_flags(self, config_number):
        """
        Get configuration flags from NVM.

        Parameters
        ----------
        config_number : int
            Configuration number (0 to MAX_CONFIG-1).

        Returns
        -------
        tuple
            (status, active, valid).

        """
        active = ctypes.c_bool()
        valid = ctypes.c_bool()
        status = self.amx_hd_dll.COM_HVAMX4EDH_GetConfigFlags(
            self.stream, config_number, ctypes.byref(active), ctypes.byref(valid)
        )
        return status, active.value, valid.value

    def set_config_flags(self, config_number, active, valid):
        """
        Set configuration flags in NVM.

        Parameters
        ----------
        config_number : int
            Configuration number (0 to MAX_CONFIG-1).
        active : bool
            Active flag.
        valid : bool
            Valid flag.

        Returns
        -------
        int
            Status code.

        """
        status = self.amx_hd_dll.COM_HVAMX4EDH_SetConfigFlags(
            self.stream, config_number, ctypes.c_bool(active), ctypes.c_bool(valid)
        )
        return status

    # =========================================================================
    #     System
    # =========================================================================

    def restart(self):
        """
        Restart the controller.

        Returns
        -------
        int
            Status code.

        """
        status = self.amx_hd_dll.COM_HVAMX4EDH_Restart(self.stream)
        return status

    def get_cpu_data(self):
        """
        Get CPU load (0-1) and frequency (Hz).

        Returns
        -------
        tuple
            (status, load, frequency).

        """
        load = ctypes.c_double()
        frequency = ctypes.c_double()
        status = self.amx_hd_dll.COM_HVAMX4EDH_GetCPUData(
            self.stream, ctypes.byref(load), ctypes.byref(frequency)
        )
        return status, load.value, frequency.value

    def get_uptime(self):
        """
        Get device uptime and operation time.

        Returns
        -------
        tuple
            (status, seconds, milliseconds, optime).

        """
        seconds = ctypes.c_uint32()
        milliseconds = ctypes.c_uint16()
        optime = ctypes.c_uint32()
        status = self.amx_hd_dll.COM_HVAMX4EDH_GetUptime(
            self.stream,
            ctypes.byref(seconds),
            ctypes.byref(milliseconds),
            ctypes.byref(optime),
        )
        return status, seconds.value, milliseconds.value, optime.value

    def get_total_time(self):
        """
        Get total device uptime and operation time.

        Returns
        -------
        tuple
            (status, uptime, optime).

        """
        uptime = ctypes.c_uint32()
        optime = ctypes.c_uint32()
        status = self.amx_hd_dll.COM_HVAMX4EDH_GetTotalTime(
            self.stream, ctypes.byref(uptime), ctypes.byref(optime)
        )
        return status, uptime.value, optime.value

    def get_fw_version(self):
        """
        Get the firmware version.

        Returns
        -------
        tuple
            (status, version).

        """
        version = ctypes.c_uint16()
        status = self.amx_hd_dll.COM_HVAMX4EDH_GetFwVersion(
            self.stream, ctypes.byref(version)
        )
        return status, version.value

    def get_fw_date(self):
        """
        Get the firmware date.

        Returns
        -------
        tuple
            (status, date_string).

        """
        date_string = ctypes.create_string_buffer(self.DATA_STRING_SIZE)
        status = self.amx_hd_dll.COM_HVAMX4EDH_GetFwDate(self.stream, date_string)
        return status, date_string.value.decode()

    def get_product_id(self):
        """
        Get the product identification.

        Returns
        -------
        tuple
            (status, identification).

        """
        identification = ctypes.create_string_buffer(self.PRODUCT_ID_SIZE)
        status = self.amx_hd_dll.COM_HVAMX4EDH_GetProductID(self.stream, identification)
        return status, identification.value.decode()

    def get_product_no(self):
        """
        Get the product number.

        Returns
        -------
        tuple
            (status, number).

        """
        number = ctypes.c_uint32()
        status = self.amx_hd_dll.COM_HVAMX4EDH_GetProductNo(
            self.stream, ctypes.byref(number)
        )
        return status, number.value

    def get_manuf_date(self):
        """
        Get manufacturing date.

        Returns
        -------
        tuple
            (status, year, calendar_week).

        """
        year = ctypes.c_uint16()
        calendar_week = ctypes.c_uint16()
        status = self.amx_hd_dll.COM_HVAMX4EDH_GetManufDate(
            self.stream, ctypes.byref(year), ctypes.byref(calendar_week)
        )
        return status, year.value, calendar_week.value

    def get_cpu_id(self):
        """
        Get CPU identification.

        Returns
        -------
        tuple
            (status, cpu_id).

        """
        cpu_id = ctypes.c_uint16()
        status = self.amx_hd_dll.COM_HVAMX4EDH_GetCPU_ID(
            self.stream, ctypes.byref(cpu_id)
        )
        return status, cpu_id.value

    def get_dev_type(self):
        """
        Get device type.

        Returns
        -------
        tuple
            (status, dev_type).

        """
        dev_type = ctypes.c_uint16()
        status = self.amx_hd_dll.COM_HVAMX4EDH_GetDevType(
            self.stream, ctypes.byref(dev_type)
        )
        return status, dev_type.value

    def get_hw_type(self):
        """
        Get the hardware type and the number of implemented modules.

        Returns
        -------
        tuple
            (status, hw_type, num_signal, num_register, num_burst_timer,
             num_timer, num_oscil, num_mapping, num_switch_dual_level,
             num_switch_tri_level).

        """
        hw_type = ctypes.c_uint32()
        num_signal = ctypes.c_uint16()
        num_register = ctypes.c_uint16()
        num_burst_timer = ctypes.c_uint16()
        num_timer = ctypes.c_uint16()
        num_oscil = ctypes.c_uint16()
        num_mapping = ctypes.c_uint16()
        num_switch_dual_level = ctypes.c_uint16()
        num_switch_tri_level = ctypes.c_uint16()
        status = self.amx_hd_dll.COM_HVAMX4EDH_GetHwType(
            self.stream,
            ctypes.byref(hw_type),
            ctypes.byref(num_signal),
            ctypes.byref(num_register),
            ctypes.byref(num_burst_timer),
            ctypes.byref(num_timer),
            ctypes.byref(num_oscil),
            ctypes.byref(num_mapping),
            ctypes.byref(num_switch_dual_level),
            ctypes.byref(num_switch_tri_level),
        )
        return (
            status,
            hw_type.value, num_signal.value, num_register.value,
            num_burst_timer.value, num_timer.value, num_oscil.value,
            num_mapping.value, num_switch_dual_level.value,
            num_switch_tri_level.value,
        )

    def get_hw_version(self):
        """
        Get the hardware versions.

        Returns
        -------
        tuple
            (status, hw_version, fpga_version).

        """
        hw_version = ctypes.c_uint16()
        fpga_version = ctypes.c_uint16()
        status = self.amx_hd_dll.COM_HVAMX4EDH_GetHwVersion(
            self.stream, ctypes.byref(hw_version), ctypes.byref(fpga_version)
        )
        return status, hw_version.value, fpga_version.value

    # =========================================================================
    #     Communication Port Status
    # =========================================================================

    def get_interface_state(self):
        """
        Get software interface state.

        Returns
        -------
        int
            Interface state code.

        """
        state = self.amx_hd_dll.COM_HVAMX4EDH_GetInterfaceState(self.stream)
        return state

    def get_error_message(self):
        """
        Get the error message corresponding to the software interface state.

        Returns
        -------
        str
            Error message.

        """
        self.amx_hd_dll.COM_HVAMX4EDH_GetErrorMessage.restype = ctypes.c_char_p
        msg_ptr = self.amx_hd_dll.COM_HVAMX4EDH_GetErrorMessage(self.stream)
        message = msg_ptr.decode() if msg_ptr else "No error"
        return message

    def get_io_state(self):
        """
        Get and clear last serial port interface state.

        Returns
        -------
        tuple
            (status, io_state).

        """
        io_state = ctypes.c_int()
        status = self.amx_hd_dll.COM_HVAMX4EDH_GetIOState(
            self.stream, ctypes.byref(io_state)
        )
        return status, io_state.value

    def get_io_error_message(self):
        """
        Get the error message corresponding to the serial port interface state.

        Returns
        -------
        str
            Error message.

        """
        self.amx_hd_dll.COM_HVAMX4EDH_GetIOErrorMessage.restype = ctypes.c_char_p
        msg_ptr = self.amx_hd_dll.COM_HVAMX4EDH_GetIOErrorMessage(self.stream)
        message = msg_ptr.decode() if msg_ptr else "No error"
        return message

    def get_io_state_message(self, io_state):
        """
        Get the error message corresponding to the specified interface state.

        Parameters
        ----------
        io_state : int
            IO state code.

        Returns
        -------
        str
            Error message.

        """
        self.amx_hd_dll.COM_HVAMX4EDH_GetIOStateMessage.restype = ctypes.c_char_p
        msg_ptr = self.amx_hd_dll.COM_HVAMX4EDH_GetIOStateMessage(ctypes.c_int(io_state))
        message = msg_ptr.decode() if msg_ptr else "No error"
        return message

    def get_comm_error(self):
        """
        Get and clear last communication-port error.

        Returns
        -------
        tuple
            (status, comm_error).

        """
        comm_error = ctypes.c_uint32()
        status = self.amx_hd_dll.COM_HVAMX4EDH_GetCommError(
            self.stream, ctypes.byref(comm_error)
        )
        return status, comm_error.value

    def get_comm_error_message(self, comm_error):
        """
        Get the error message corresponding to the communication port error.

        Parameters
        ----------
        comm_error : int
            Communication error code.

        Returns
        -------
        str
            Error message.

        """
        self.amx_hd_dll.COM_HVAMX4EDH_GetCommErrorMessage.restype = ctypes.c_char_p
        msg_ptr = self.amx_hd_dll.COM_HVAMX4EDH_GetCommErrorMessage(
            ctypes.c_uint32(comm_error)
        )
        message = msg_ptr.decode() if msg_ptr else "No error"
        return message
