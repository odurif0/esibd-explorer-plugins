"""ESI controller base device class for CGC ESI-CTRL."""

import ctypes
import json
import sys
from pathlib import Path


class ESIPlatformError(RuntimeError):
    """Raised when the ESI driver is used on an unsupported platform."""


class ESIDllLoadError(RuntimeError):
    """Raised when the vendor ESI DLL cannot be loaded."""


class ESIBase:
    """ESI base device class wrapping the COM-ESI-CTRL DLL.

    Note
    ----
    Unlike other CGC DLLs (PSU/SW/AMPR) this DLL is single-instance: the
    exported routines do not take a port argument. Only one ESI-CTRL device
    can be opened per process.
    """

    # Error codes (from COM-ESI-CTRL.h)
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

    # Expected device / module type IDs
    DEVICE_TYPE = 0x8ED6
    MODULE_BASE_TYPE = 0x1C34
    MODULE_HTCTRL_TYPE = 0xDB1C
    MODULE_HVPS_TYPE = 0x0A0D

    # Module addressing
    MODULE_NUM = 4
    ADDR_HTCTRL = 0
    ADDR_BASE = 0x80
    ADDR_BROADCAST = 0xFF
    PRESENCE_BASE = MODULE_NUM  # index of base module in presence array

    # Module presence return values
    MODULE_NOT_FOUND = 0
    MODULE_PRESENT = 1
    MODULE_INVALID = 2

    # Controller main state values
    MAIN_STATE = {
        0x0000: 'STATE_ON',
        0x0010: 'STATE_ERROR',
        0x0011: 'STATE_ERR_MODULE',
        0x0012: 'STATE_ERR_VSUP',
        0x0013: 'STATE_ERR_TEMP_LOW',
        0x0014: 'STATE_ERR_TEMP_HIGH',
        0x0015: 'STATE_ERR_ILOCK',
    }

    # Device state bit flags
    DEVICE_STATE = {
        (1 << 0): 'DS_ILOCK_FAIL',
        (1 << 1): 'DS_VOLT_FAIL',
        (1 << 2): 'DS_TEMP_FAIL',
        (1 << 3): 'DS_FAN_FAIL',
        (1 << 4): 'DS_MODULE_FAIL',
    }

    # Voltage state bit flags
    VOLTAGE_STATE = {
        (1 << 0): 'VS_3V3_OK',
        (1 << 1): 'VS_5V0_OK',
        (1 << 2): 'VS_24V_OK',
        (1 << 4): 'VS_LINE_OK',
        (1 << 5): 'VS_PSU_OK',
    }

    # Fan state bit flags
    FAN_STATE = {
        (1 << 0): 'FS_FAN_OK',
    }

    # Temperature state bit flags
    TEMPERATURE_STATE = {
        (1 << 0): 'TS_TCPU_HIGH',
        (1 << 1): 'TS_TPSU_HIGH',
        (1 << 2): 'TS_TCPU_LOW',
        (1 << 3): 'TS_TPSU_LOW',
    }

    # Interlock state bit flags
    INTERLOCK_STATE = {
        (1 << 0x0): 'IS_HTCTRL_ILOCK1',
        (1 << 0x1): 'IS_HTCTRL_ILOCK2',
        (1 << 0x2): 'IS_CTRL_ILOCK_FP',
        (1 << 0x3): 'IS_CTRL_ILOCK_RP',
        (1 << 0x8): 'IS_HTCTRL_ILOCK1_CURR',
        (1 << 0x9): 'IS_HTCTRL_ILOCK2_CURR',
        (1 << 0xA): 'IS_HTCTRL_ILOCK1_LAST',
        (1 << 0xB): 'IS_HTCTRL_ILOCK2_LAST',
        (1 << 0xC): 'IS_CTRL_ILOCK_FP_CURR',
        (1 << 0xD): 'IS_CTRL_ILOCK_RP_CURR',
        (1 << 0xE): 'IS_CTRL_ILOCK_FP_LAST',
        (1 << 0xF): 'IS_CTRL_ILOCK_RP_LAST',
    }

    # Data-ready flags for the controller
    HK_RDY = 1 << 0
    HK_OFL = 1 << 1
    FAN_RDY = 1 << 2
    FAN_OFL = 1 << 3

    # Module state bits
    MS_ACTIVE = 1 << 0xF

    # String sizes
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
        Initialization.

        Parameters
        ----------
        com : int
            COM port hardware side (1 = COM1, etc.).
        log : logfile, optional
            Logging instance where information is logged.
        idn : str, optional
            String to distinguish between same devices.
        """
        if not sys.platform.startswith("win"):
            raise ESIPlatformError(
                "CGC ESI is supported only on Windows because it depends on "
                "COM-ESI-CTRL.dll."
            )

        class_dir = Path(__file__).resolve().parent
        self.esi_dll_path = Path(dll_path) if dll_path is not None else (
            class_dir / "vendor" / "x64" / "COM-ESI-CTRL.dll"
        )
        try:
            self.esi_dll = ctypes.WinDLL(str(self.esi_dll_path))
        except OSError as exc:
            raise ESIDllLoadError(
                f"Unable to load CGC ESI DLL from '{self.esi_dll_path}'."
            ) from exc
        self._configure_hv_dll_signatures()

        self.err_path = Path(error_codes_path) if error_codes_path is not None else (
            class_dir.parent / "error_codes.json"
        )
        with self.err_path.open(encoding="utf-8") as stream:
            self.err_dict = json.load(stream)

        self.com = com
        self.log = log
        self.idn = idn

    def _configure_hv_dll_signatures(self) -> None:
        """Declare the HV ABI exactly as published in COM-ESI-CTRL.h."""
        bool_ptr = ctypes.POINTER(ctypes.c_bool)
        double_ptr = ctypes.POINTER(ctypes.c_double)
        byte_ptr = ctypes.POINTER(ctypes.c_ubyte)
        signatures = {
            "COM_ESI_CTRL_SetEnable": ([ctypes.c_bool], ctypes.c_int),
            "COM_ESI_CTRL_GetEnable": ([bool_ptr], ctypes.c_int),
            "COM_ESI_CTRL_SetModuleActivationState": (
                [ctypes.c_uint, ctypes.c_bool],
                ctypes.c_int,
            ),
            "COM_ESI_CTRL_GetModuleActivationState": (
                [ctypes.c_uint, bool_ptr],
                ctypes.c_int,
            ),
            "COM_ESI_CTRL_GetModuleLEDData": (
                [ctypes.c_uint, bool_ptr, bool_ptr, bool_ptr],
                ctypes.c_int,
            ),
            "COM_ESI_CTRL_SetHVsupplyMeasRanges": (
                [ctypes.c_uint, ctypes.c_bool, ctypes.c_bool],
                ctypes.c_int,
            ),
            "COM_ESI_CTRL_GetHVsupplyOutputVoltage": (
                [ctypes.c_uint, bool_ptr, double_ptr],
                ctypes.c_int,
            ),
            "COM_ESI_CTRL_GetHVsupplyOutputCurrent": (
                [ctypes.c_uint, bool_ptr, double_ptr],
                ctypes.c_int,
            ),
            "COM_ESI_CTRL_GetHVsupplyPhase": (
                [ctypes.c_uint, bool_ptr, double_ptr],
                ctypes.c_int,
            ),
            "COM_ESI_CTRL_GetHVsupplyTargetOutputVoltage": (
                [ctypes.c_uint, double_ptr],
                ctypes.c_int,
            ),
            "COM_ESI_CTRL_SetHVsupplyTargetOutputVoltage": (
                [ctypes.c_uint, ctypes.c_double],
                ctypes.c_int,
            ),
            "COM_ESI_CTRL_GetHVsupplyParamsPWM": (
                [
                    ctypes.c_uint,
                    double_ptr,
                    double_ptr,
                    double_ptr,
                    double_ptr,
                    double_ptr,
                    double_ptr,
                    bool_ptr,
                    byte_ptr,
                ],
                ctypes.c_int,
            ),
        }
        for name, (argtypes, restype) in signatures.items():
            function = getattr(self.esi_dll, name)
            function.argtypes = argtypes
            function.restype = restype

    def describe_error(self, status: int) -> str:
        """Return the vendor message for a driver status code."""
        return self.err_dict.get(str(status), "Unknown status code")

    def format_status(self, status: int) -> str:
        """Return a compact status representation."""
        return f"{status} ({self.describe_error(status)})"

    # =========================================================================
    #     Communication
    # =========================================================================

    def get_sw_version(self):
        """Get DLL software version."""
        self.esi_dll.COM_ESI_CTRL_GetSWVersion.restype = ctypes.c_uint16
        return self.esi_dll.COM_ESI_CTRL_GetSWVersion()

    def open_port(self, com_number=None):
        """Open communication port."""
        if com_number is None:
            com_number = self.com
        return self.esi_dll.COM_ESI_CTRL_Open(ctypes.c_ubyte(com_number))

    def close_port(self):
        """Close communication port."""
        return self.esi_dll.COM_ESI_CTRL_Close()

    def set_comspeed(self, baud_rate):
        """Set baud rate. Returns (status, actual_baud_rate)."""
        baud_ref = ctypes.c_uint(baud_rate)
        status = self.esi_dll.COM_ESI_CTRL_SetBaudRate(ctypes.byref(baud_ref))
        return status, baud_ref.value

    def purge(self):
        """Clear data buffers for the communication port."""
        return self.esi_dll.COM_ESI_CTRL_Purge()

    def get_buffer_state(self):
        """Return (status, empty) for the device input buffer."""
        empty = ctypes.c_bool()
        status = self.esi_dll.COM_ESI_CTRL_GetBufferState(ctypes.byref(empty))
        return status, empty.value

    def device_purge(self):
        """Clear output data buffer of the device."""
        empty = ctypes.c_bool()
        status = self.esi_dll.COM_ESI_CTRL_DevicePurge(ctypes.byref(empty))
        return status, empty.value

    def get_auto_mask(self):
        """Get mask of the last automatic notification data."""
        mask = ctypes.c_uint()
        status = self.esi_dll.COM_ESI_CTRL_GetAutoMask(ctypes.byref(mask))
        return status, mask.value

    def check_auto_input(self):
        """Check for new automatic notification data."""
        mask = ctypes.c_uint()
        status = self.esi_dll.COM_ESI_CTRL_CheckAutoInput(ctypes.byref(mask))
        return status, mask.value

    # =========================================================================
    #     General
    # =========================================================================

    def get_fw_version(self):
        """Get firmware version."""
        fw = ctypes.c_uint16()
        status = self.esi_dll.COM_ESI_CTRL_GetFwVersion(ctypes.byref(fw))
        return status, fw.value

    def get_fw_date(self):
        """Get firmware date as string."""
        buf = ctypes.create_string_buffer(self.DATA_STRING_SIZE)
        status = self.esi_dll.COM_ESI_CTRL_GetFwDate(buf)
        return status, buf.value.decode(errors="replace")

    def get_product_id(self):
        """Get product identification string."""
        buf = ctypes.create_string_buffer(self.PRODUCT_ID_SIZE)
        status = self.esi_dll.COM_ESI_CTRL_GetProductID(buf)
        return status, buf.value.decode(errors="replace")

    def get_product_no(self):
        """Get product number."""
        pn = ctypes.c_uint32()
        status = self.esi_dll.COM_ESI_CTRL_GetProductNo(ctypes.byref(pn))
        return status, pn.value

    def get_manuf_date(self):
        """Get manufacturing date (year, calendar week)."""
        year = ctypes.c_uint16()
        week = ctypes.c_uint16()
        status = self.esi_dll.COM_ESI_CTRL_GetManufDate(
            ctypes.byref(year), ctypes.byref(week)
        )
        return status, year.value, week.value

    def get_dev_type(self):
        """Get device type."""
        dt = ctypes.c_uint16()
        status = self.esi_dll.COM_ESI_CTRL_GetDevType(ctypes.byref(dt))
        return status, dt.value

    def get_hw_type(self):
        """Get hardware type."""
        ht = ctypes.c_uint32()
        status = self.esi_dll.COM_ESI_CTRL_GetHwType(ctypes.byref(ht))
        return status, ht.value

    def get_hw_version(self):
        """Get hardware version."""
        hv = ctypes.c_uint16()
        status = self.esi_dll.COM_ESI_CTRL_GetHwVersion(ctypes.byref(hv))
        return status, hv.value

    def get_uptime(self):
        """Get device uptime (seconds, total_seconds)."""
        sec = ctypes.c_double()
        total = ctypes.c_double()
        status = self.esi_dll.COM_ESI_CTRL_GetUptime(
            ctypes.byref(sec), ctypes.byref(total)
        )
        return status, sec.value, total.value

    def get_optime(self):
        """Get device operation time (seconds, total_seconds)."""
        sec = ctypes.c_double()
        total = ctypes.c_double()
        status = self.esi_dll.COM_ESI_CTRL_GetOptime(
            ctypes.byref(sec), ctypes.byref(total)
        )
        return status, sec.value, total.value

    def get_cpu_data(self):
        """Get CPU load (0-1) and frequency (Hz)."""
        load = ctypes.c_double()
        freq = ctypes.c_double()
        status = self.esi_dll.COM_ESI_CTRL_GetCPUdata(
            ctypes.byref(load), ctypes.byref(freq)
        )
        return status, load.value, freq.value

    def get_housekeeping(self):
        """Get controller housekeeping data.

        Returns (status, volt_24v, volt_5v0, volt_3v3, temp_cpu, temp_psu).
        """
        v24 = ctypes.c_double()
        v5 = ctypes.c_double()
        v3 = ctypes.c_double()
        tcpu = ctypes.c_double()
        tpsu = ctypes.c_double()
        status = self.esi_dll.COM_ESI_CTRL_GetHousekeeping(
            ctypes.byref(v24), ctypes.byref(v5), ctypes.byref(v3),
            ctypes.byref(tcpu), ctypes.byref(tpsu),
        )
        return status, v24.value, v5.value, v3.value, tcpu.value, tpsu.value

    def restart(self):
        """Restart the controller."""
        return self.esi_dll.COM_ESI_CTRL_Restart()

    # =========================================================================
    #     ESI controller
    # =========================================================================

    def set_activation_state(self, activation_state):
        """Set device activation state."""
        return self.esi_dll.COM_ESI_CTRL_SetActivationState(
            ctypes.c_bool(activation_state)
        )

    def get_activation_state(self):
        """Get device activation state."""
        st = ctypes.c_bool()
        status = self.esi_dll.COM_ESI_CTRL_GetActivationState(ctypes.byref(st))
        return status, st.value

    def get_data_ready_flags(self):
        """Get data-ready flags."""
        flags = ctypes.c_ubyte()
        status = self.esi_dll.COM_ESI_CTRL_GetDataReadyFlags(ctypes.byref(flags))
        return status, flags.value

    def get_main_state(self):
        """Get main device state. Returns (status, state_hex, state_name)."""
        state = ctypes.c_uint16()
        status = self.esi_dll.COM_ESI_CTRL_GetState(ctypes.byref(state))
        sv = state.value
        name = self.MAIN_STATE.get(sv, f"UNKNOWN_STATE_0x{sv:04X}")
        return status, hex(sv), name

    def get_device_state(self):
        """Get device state. Returns (status, state_hex, active_flag_names)."""
        ds = ctypes.c_ubyte()
        status = self.esi_dll.COM_ESI_CTRL_GetDeviceState(ctypes.byref(ds))
        sv = ds.value
        active = [n for f, n in self.DEVICE_STATE.items() if sv & f] or ['DEVST_OK']
        return status, hex(sv), active

    def set_enable(self, enable):
        """Enable/disable modules."""
        return self.esi_dll.COM_ESI_CTRL_SetEnable(ctypes.c_bool(enable))

    def get_enable(self):
        """Get module enable state."""
        en = ctypes.c_bool()
        status = self.esi_dll.COM_ESI_CTRL_GetEnable(ctypes.byref(en))
        return status, en.value

    def get_voltage_state(self):
        """Get voltage state. Returns (status, state_hex, active_flag_names)."""
        vs = ctypes.c_ubyte()
        status = self.esi_dll.COM_ESI_CTRL_GetVoltageState(ctypes.byref(vs))
        sv = vs.value
        active = [n for f, n in self.VOLTAGE_STATE.items() if sv & f]
        return status, hex(sv), active

    def get_fan_state(self):
        """Get fan state. Returns (status, state_hex, active_flag_names)."""
        fs = ctypes.c_ubyte()
        status = self.esi_dll.COM_ESI_CTRL_GetFanState(ctypes.byref(fs))
        sv = fs.value
        active = [n for f, n in self.FAN_STATE.items() if sv & f]
        return status, hex(sv), active

    def get_temperature_state(self):
        """Get temperature state."""
        ts = ctypes.c_ubyte()
        status = self.esi_dll.COM_ESI_CTRL_GetTemperatureState(ctypes.byref(ts))
        sv = ts.value
        active = [n for f, n in self.TEMPERATURE_STATE.items() if sv & f]
        return status, hex(sv), active

    def get_interlock_state(self):
        """Get interlock state."""
        ils = ctypes.c_uint16()
        status = self.esi_dll.COM_ESI_CTRL_GetInterlockState(ctypes.byref(ils))
        sv = ils.value
        active = [n for f, n in self.INTERLOCK_STATE.items() if sv & f]
        return status, hex(sv), active

    def get_interlock_enable(self):
        """Get interlock enable mask."""
        ie = ctypes.c_ubyte()
        status = self.esi_dll.COM_ESI_CTRL_GetInterlockEnable(ctypes.byref(ie))
        return status, ie.value

    def set_interlock_enable(self, interlock_enable):
        """Set interlock enable mask."""
        return self.esi_dll.COM_ESI_CTRL_SetInterlockEnable(
            ctypes.c_ubyte(interlock_enable)
        )

    def get_inputs(self):
        """Get device input levels. Returns (status, ilock_front, ilock_rear, fan_enable)."""
        front = ctypes.c_bool()
        rear = ctypes.c_bool()
        fan_en = ctypes.c_bool()
        status = self.esi_dll.COM_ESI_CTRL_GetInputs(
            ctypes.byref(front), ctypes.byref(rear), ctypes.byref(fan_en)
        )
        return status, front.value, rear.value, fan_en.value

    def get_power_monitors(self):
        """Get power monitor state. Returns (status, ac_in_ok, dc_out_ok)."""
        ac = ctypes.c_bool()
        dc = ctypes.c_bool()
        status = self.esi_dll.COM_ESI_CTRL_GetPowerMonitors(
            ctypes.byref(ac), ctypes.byref(dc)
        )
        return status, ac.value, dc.value

    def get_led_data(self):
        """Get LED data. Returns (status, red, green, blue)."""
        r = ctypes.c_bool()
        g = ctypes.c_bool()
        b = ctypes.c_bool()
        status = self.esi_dll.COM_ESI_CTRL_GetLEDData(
            ctypes.byref(r), ctypes.byref(g), ctypes.byref(b)
        )
        return status, r.value, g.value, b.value

    def get_fan_data(self):
        """Get fan data. Returns (status, failed, max_rpm, set_rpm, meas_rpm, pwm)."""
        failed = ctypes.c_bool()
        max_rpm = ctypes.c_uint16()
        set_rpm = ctypes.c_uint16()
        meas_rpm = ctypes.c_uint16()
        pwm = ctypes.c_float()
        status = self.esi_dll.COM_ESI_CTRL_GetFanData(
            ctypes.byref(failed), ctypes.byref(max_rpm),
            ctypes.byref(set_rpm), ctypes.byref(meas_rpm), ctypes.byref(pwm),
        )
        return status, failed.value, max_rpm.value, set_rpm.value, meas_rpm.value, pwm.value

    # =========================================================================
    #     Module service
    # =========================================================================

    def get_module_presence(self):
        """Get module presence flags.

        Returns (status, valid, max_module, presence_list) where presence_list
        has length MODULE_NUM+1 (last entry is the base module).
        """
        valid = ctypes.c_bool()
        max_mod = ctypes.c_uint()
        arr = (ctypes.c_ubyte * (self.MODULE_NUM + 1))()
        status = self.esi_dll.COM_ESI_CTRL_GetModulePresence(
            ctypes.byref(valid), ctypes.byref(max_mod), arr
        )
        return status, valid.value, max_mod.value, list(arr)

    def update_module_presence(self):
        """Update module-presence flags."""
        return self.esi_dll.COM_ESI_CTRL_UpdateModulePresence()

    def rescan_modules(self):
        """Rescan address pins of all modules."""
        return self.esi_dll.COM_ESI_CTRL_RescanModules()

    def rescan_module(self, address):
        """Rescan address pins of the specified module."""
        return self.esi_dll.COM_ESI_CTRL_RescanModule(ctypes.c_uint(address))

    def restart_module(self, address):
        """Restart the specified module."""
        return self.esi_dll.COM_ESI_CTRL_RestartModule(ctypes.c_uint(address))

    def get_module_dev_type(self, address):
        """Get module device type."""
        dt = ctypes.c_uint16()
        status = self.esi_dll.COM_ESI_CTRL_GetModuleDevType(
            ctypes.c_uint(address), ctypes.byref(dt)
        )
        return status, dt.value

    def get_module_fw_version(self, address):
        """Get module firmware version."""
        fw = ctypes.c_uint16()
        status = self.esi_dll.COM_ESI_CTRL_GetModuleFwVersion(
            ctypes.c_uint(address), ctypes.byref(fw)
        )
        return status, fw.value

    def get_module_product_no(self, address):
        """Get module product number."""
        pn = ctypes.c_uint32()
        status = self.esi_dll.COM_ESI_CTRL_GetModuleProductNo(
            ctypes.c_uint(address), ctypes.byref(pn)
        )
        return status, pn.value

    def get_module_product_id(self, address):
        """Get module product identification string."""
        buf = ctypes.create_string_buffer(self.PRODUCT_ID_SIZE)
        status = self.esi_dll.COM_ESI_CTRL_GetModuleProductID(
            ctypes.c_uint(address), buf
        )
        return status, buf.value.decode(errors="replace")

    def get_module_hw_type(self, address):
        """Get module hardware type."""
        hw_type = ctypes.c_uint32()
        status = self.esi_dll.COM_ESI_CTRL_GetModuleHwType(
            ctypes.c_uint(address), ctypes.byref(hw_type)
        )
        return status, hw_type.value

    def get_module_hw_version(self, address):
        """Get module hardware version."""
        version = ctypes.c_uint16()
        status = self.esi_dll.COM_ESI_CTRL_GetModuleHwVersion(
            ctypes.c_uint(address), ctypes.byref(version)
        )
        return status, version.value

    def get_hv_supply_fpga_version(self, address):
        """Get one HV module's FPGA version."""
        version = ctypes.c_uint32()
        status = self.esi_dll.COM_ESI_CTRL_GetHVsupplyFpgaVersion(
            ctypes.c_uint(address), ctypes.byref(version)
        )
        return status, version.value

    def get_module_data_ready_flags(self, address):
        """Get data-ready flags of a module."""
        flags = ctypes.c_ubyte()
        status = self.esi_dll.COM_ESI_CTRL_GetModuleDataReadyFlags(
            ctypes.c_uint(address), ctypes.byref(flags)
        )
        return status, flags.value

    def get_module_led_data(self, address):
        """Get module LED data. Returns (status, red, green, blue)."""
        red = ctypes.c_bool()
        green = ctypes.c_bool()
        blue = ctypes.c_bool()
        status = self.esi_dll.COM_ESI_CTRL_GetModuleLEDData(
            ctypes.c_uint(address),
            ctypes.byref(red),
            ctypes.byref(green),
            ctypes.byref(blue),
        )
        return status, red.value, green.value, blue.value

    def get_module_state(self, address):
        """Get module state word."""
        ms = ctypes.c_uint16()
        status = self.esi_dll.COM_ESI_CTRL_GetModuleState(
            ctypes.c_uint(address), ctypes.byref(ms)
        )
        return status, ms.value

    def set_module_activation_state(self, address, activation_state):
        """Set module activation state."""
        return self.esi_dll.COM_ESI_CTRL_SetModuleActivationState(
            ctypes.c_uint(address), ctypes.c_bool(activation_state)
        )

    def get_module_activation_state(self, address):
        """Get module activation state."""
        st = ctypes.c_bool()
        status = self.esi_dll.COM_ESI_CTRL_GetModuleActivationState(
            ctypes.c_uint(address), ctypes.byref(st)
        )
        return status, st.value

    def get_base_housekeeping(self):
        """Get base-module housekeeping. Returns (status, volt_3v3, temp_cpu)."""
        v3 = ctypes.c_double()
        tcpu = ctypes.c_double()
        status = self.esi_dll.COM_ESI_CTRL_GetBaseHousekeeping(
            ctypes.byref(v3), ctypes.byref(tcpu)
        )
        return status, v3.value, tcpu.value

    def get_heat_ctrl_housekeeping(self):
        """Get heat-controller housekeeping.

        Returns (status, valid, volt_3v3, temp_cpu, volt_5v0, volt_24v, temp_psu).
        """
        valid = ctypes.c_bool()
        v3 = ctypes.c_double()
        tcpu = ctypes.c_double()
        v5 = ctypes.c_double()
        v24 = ctypes.c_double()
        tpsu = ctypes.c_double()
        status = self.esi_dll.COM_ESI_CTRL_GetHeatCtrlHousekeeping(
            ctypes.byref(valid), ctypes.byref(v3), ctypes.byref(tcpu),
            ctypes.byref(v5), ctypes.byref(v24), ctypes.byref(tpsu),
        )
        return (
            status, valid.value, v3.value, tcpu.value,
            v5.value, v24.value, tpsu.value,
        )

    def get_hv_supply_housekeeping(self, address):
        """Get HV-PSU housekeeping data.

        Returns (status, valid, volt_3v3, temp_cpu, volt_5v0, volt_24vp,
        volt_22vn, volt_18vp, volt_18vn, volt_1v5, volt_agnd).
        """
        valid = ctypes.c_bool()
        v3 = ctypes.c_double()
        tcpu = ctypes.c_double()
        v5 = ctypes.c_double()
        v24p = ctypes.c_double()
        v22n = ctypes.c_double()
        v18p = ctypes.c_double()
        v18n = ctypes.c_double()
        v1_5 = ctypes.c_double()
        vagnd = ctypes.c_double()
        status = self.esi_dll.COM_ESI_CTRL_GetHVsupplyHousekeeping(
            ctypes.c_uint(address), ctypes.byref(valid),
            ctypes.byref(v3), ctypes.byref(tcpu), ctypes.byref(v5),
            ctypes.byref(v24p), ctypes.byref(v22n),
            ctypes.byref(v18p), ctypes.byref(v18n),
            ctypes.byref(v1_5), ctypes.byref(vagnd),
        )
        return (
            status, valid.value, v3.value, tcpu.value, v5.value,
            v24p.value, v22n.value, v18p.value, v18n.value,
            v1_5.value, vagnd.value,
        )

    # -- HV supply control --

    def set_hv_supply_meas_ranges(self, address, volt_neg, curr_high):
        """Set HV-PSU measurement channels."""
        return self.esi_dll.COM_ESI_CTRL_SetHVsupplyMeasRanges(
            ctypes.c_uint(address),
            ctypes.c_bool(volt_neg), ctypes.c_bool(curr_high),
        )

    def get_hv_supply_output_voltage(self, address):
        """Get HV-PSU output voltage. Returns (status, valid, voltage)."""
        valid = ctypes.c_bool()
        v = ctypes.c_double()
        status = self.esi_dll.COM_ESI_CTRL_GetHVsupplyOutputVoltage(
            ctypes.c_uint(address), ctypes.byref(valid), ctypes.byref(v)
        )
        return status, valid.value, v.value

    def get_hv_supply_output_current(self, address):
        """Get HV-PSU output current. Returns (status, valid, current)."""
        valid = ctypes.c_bool()
        i = ctypes.c_double()
        status = self.esi_dll.COM_ESI_CTRL_GetHVsupplyOutputCurrent(
            ctypes.c_uint(address), ctypes.byref(valid), ctypes.byref(i)
        )
        return status, valid.value, i.value

    def get_hv_supply_phase(self, address):
        """Get HV-PSU phase. Returns (status, valid, phase)."""
        valid = ctypes.c_bool()
        p = ctypes.c_double()
        status = self.esi_dll.COM_ESI_CTRL_GetHVsupplyPhase(
            ctypes.c_uint(address), ctypes.byref(valid), ctypes.byref(p)
        )
        return status, valid.value, p.value

    def get_hv_supply_target_output_voltage(self, address):
        """Get HV-PSU target output voltage."""
        v = ctypes.c_double()
        status = self.esi_dll.COM_ESI_CTRL_GetHVsupplyTargetOutputVoltage(
            ctypes.c_uint(address), ctypes.byref(v)
        )
        return status, v.value

    def set_hv_supply_target_output_voltage(self, address, voltage):
        """Set HV-PSU target output voltage."""
        return self.esi_dll.COM_ESI_CTRL_SetHVsupplyTargetOutputVoltage(
            ctypes.c_uint(address), ctypes.c_double(voltage)
        )

    def get_hv_supply_params_pwm(self, address):
        """Get HV-PSU PWM parameters.

        Returns (status, period, width, phase_get, phase_set, volt_set,
        volt_meas, activation_state, data_ready_flags).
        """
        period = ctypes.c_double()
        width = ctypes.c_double()
        phase_get = ctypes.c_double()
        phase_set = ctypes.c_double()
        volt_set = ctypes.c_double()
        volt_meas = ctypes.c_double()
        activation = ctypes.c_bool()
        drf = ctypes.c_ubyte()
        status = self.esi_dll.COM_ESI_CTRL_GetHVsupplyParamsPWM(
            ctypes.c_uint(address),
            ctypes.byref(period), ctypes.byref(width),
            ctypes.byref(phase_get), ctypes.byref(phase_set),
            ctypes.byref(volt_set), ctypes.byref(volt_meas),
            ctypes.byref(activation), ctypes.byref(drf),
        )
        return (
            status, period.value, width.value,
            phase_get.value, phase_set.value,
            volt_set.value, volt_meas.value,
            activation.value, drf.value,
        )

    # -- Heat controller --

    def get_heat_ctrl_hw_limits(self):
        """Get heat-controller hardware limits.

        Returns (status, max_voltage, max_current, max_power, max_temperature).
        """
        v = ctypes.c_double()
        i = ctypes.c_double()
        p = ctypes.c_double()
        t = ctypes.c_double()
        status = self.esi_dll.COM_ESI_CTRL_GetHeatCtrlHwLimits(
            ctypes.byref(v), ctypes.byref(i), ctypes.byref(p), ctypes.byref(t)
        )
        return status, v.value, i.value, p.value, t.value

    def get_heat_ctrl_voltage_limit(self):
        v = ctypes.c_double()
        status = self.esi_dll.COM_ESI_CTRL_GetHeatCtrlVoltageLimit(ctypes.byref(v))
        return status, v.value

    def set_heat_ctrl_voltage_limit(self, max_voltage):
        v = ctypes.c_double(max_voltage)
        status = self.esi_dll.COM_ESI_CTRL_SetHeatCtrlVoltageLimit(ctypes.byref(v))
        return status, v.value

    def get_heat_ctrl_current_limit(self):
        i = ctypes.c_double()
        status = self.esi_dll.COM_ESI_CTRL_GetHeatCtrlCurrentLimit(ctypes.byref(i))
        return status, i.value

    def set_heat_ctrl_current_limit(self, max_current):
        i = ctypes.c_double(max_current)
        status = self.esi_dll.COM_ESI_CTRL_SetHeatCtrlCurrentLimit(ctypes.byref(i))
        return status, i.value

    def get_heat_ctrl_power_limit(self):
        p = ctypes.c_double()
        status = self.esi_dll.COM_ESI_CTRL_GetHeatCtrlPowerLimit(ctypes.byref(p))
        return status, p.value

    def set_heat_ctrl_power_limit(self, max_power):
        p = ctypes.c_double(max_power)
        status = self.esi_dll.COM_ESI_CTRL_SetHeatCtrlPowerLimit(ctypes.byref(p))
        return status, p.value

    def get_heat_ctrl_heater_temperature(self):
        t = ctypes.c_double()
        status = self.esi_dll.COM_ESI_CTRL_GetHeatCtrlHeaterTemperature(ctypes.byref(t))
        return status, t.value

    def set_heat_ctrl_heater_temperature(self, heater_temp):
        t = ctypes.c_double(heater_temp)
        status = self.esi_dll.COM_ESI_CTRL_SetHeatCtrlHeaterTemperature(ctypes.byref(t))
        return status, t.value

    def get_heat_ctrl_output_voltage(self):
        v = ctypes.c_double()
        status = self.esi_dll.COM_ESI_CTRL_GetHeatCtrlOutputVoltage(ctypes.byref(v))
        return status, v.value

    def get_heat_ctrl_heater_power(self):
        p = ctypes.c_double()
        status = self.esi_dll.COM_ESI_CTRL_GetHeatCtrlHeaterPower(ctypes.byref(p))
        return status, p.value

    def get_heat_ctrl_monitoring(self):
        """Get heat-controller monitoring data.

        Returns (status, valid, volt_out, volt_mon, curr_mon, temp_mon).
        """
        valid = ctypes.c_bool()
        vout = ctypes.c_double()
        vmon = ctypes.c_double()
        imon = ctypes.c_double()
        tmon = ctypes.c_double()
        status = self.esi_dll.COM_ESI_CTRL_GetHeatCtrlMonitoring(
            ctypes.byref(valid), ctypes.byref(vout),
            ctypes.byref(vmon), ctypes.byref(imon), ctypes.byref(tmon),
        )
        return status, valid.value, vout.value, vmon.value, imon.value, tmon.value

    def get_heat_ctrl_ilock_state(self):
        ils = ctypes.c_ubyte()
        status = self.esi_dll.COM_ESI_CTRL_GetHeatCtrlIlockState(ctypes.byref(ils))
        return status, ils.value

    # =========================================================================
    #     Special functions
    # =========================================================================

    def get_complete_state(self):
        """Get complete device state.

        Returns (status, data_flags, device_state, voltage_state,
        temperature_state, fan_state, interlock_state, state,
        module_data_flags_list, module_state_list, heat_ctrl_interlock_state).
        """
        data_flags = ctypes.c_ubyte()
        dev_state = ctypes.c_ubyte()
        volt_state = ctypes.c_ubyte()
        temp_state = ctypes.c_ubyte()
        fan_state = ctypes.c_ubyte()
        ilock_state = ctypes.c_uint16()
        state = ctypes.c_uint16()
        mod_data_flags = (ctypes.c_ubyte * (self.MODULE_NUM + 1))()
        mod_state = (ctypes.c_uint16 * (self.MODULE_NUM + 1))()
        heat_ilock = ctypes.c_ubyte()
        status = self.esi_dll.COM_ESI_CTRL_GetCompleteState(
            ctypes.byref(data_flags), ctypes.byref(dev_state),
            ctypes.byref(volt_state), ctypes.byref(temp_state),
            ctypes.byref(fan_state), ctypes.byref(ilock_state),
            ctypes.byref(state), mod_data_flags, mod_state,
            ctypes.byref(heat_ilock),
        )
        return (
            status, data_flags.value, dev_state.value, volt_state.value,
            temp_state.value, fan_state.value, ilock_state.value,
            state.value, list(mod_data_flags), list(mod_state), heat_ilock.value,
        )

    # =========================================================================
    #     Error handling
    # =========================================================================

    def get_interface_state(self):
        """Get software interface state."""
        return self.esi_dll.COM_ESI_CTRL_GetInterfaceState()

    def get_error_message(self):
        """Get error message for the software interface state."""
        self.esi_dll.COM_ESI_CTRL_GetErrorMessage.restype = ctypes.c_char_p
        msg = self.esi_dll.COM_ESI_CTRL_GetErrorMessage()
        return msg.decode(errors="replace") if msg else ""

    def get_io_error_message(self):
        """Get error message for the serial port interface state."""
        self.esi_dll.COM_ESI_CTRL_GetIOErrorMessage.restype = ctypes.c_char_p
        msg = self.esi_dll.COM_ESI_CTRL_GetIOErrorMessage()
        return msg.decode(errors="replace") if msg else ""

    def get_io_state(self):
        """Get and clear last serial port interface state."""
        io = ctypes.c_int()
        status = self.esi_dll.COM_ESI_CTRL_GetIOState(ctypes.byref(io))
        return status, io.value

    def get_comm_error(self):
        """Get and clear last communication-port error."""
        err = ctypes.c_uint32()
        status = self.esi_dll.COM_ESI_CTRL_GetCommError(ctypes.byref(err))
        return status, err.value
