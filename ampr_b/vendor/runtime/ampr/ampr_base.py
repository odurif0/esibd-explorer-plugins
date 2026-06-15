"""AMPR (Amplifier) base device class for CGC."""

import ctypes
import json
import math
import sys
from pathlib import Path


class AMPRPlatformError(RuntimeError):
    """Raised when the AMPR driver is used on an unsupported platform."""


class AMPRDllLoadError(RuntimeError):
    """Raised when the vendor AMPR DLL cannot be loaded."""


class AMPRBase:
    """Low-level CGC AMPR driver backed by the vendor DLL."""
    
    # Error codes (from COM-AMPR-12.h)
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
    
    # Controller status values (from COM-AMPR-12.h)
    MAIN_STATE = {
        0: 'ST_ON',              # PSUs are on
        1: 'ST_OVERLOAD',        # HV PSUs overloaded
        2: 'ST_STBY',            # HV PSUs are stand-by
        0x8000: 'ST_ERROR',      # General error
        0x8001: 'ST_ERR_MODULE', # PSU-module error
        0x8002: 'ST_ERR_VSUP',   # Supply-voltage error
        0x8003: 'ST_ERR_TEMP_LOW', # Low-temperature error
        0x8004: 'ST_ERR_TEMP_HIGH', # Overheating error
        0x8005: 'ST_ERR_ILOCK',   # Interlock error
        0x8006: 'ST_ERR_PSU_DIS', # Error due to disabled PSUs
        0x8007: 'ST_ERR_HV_PSU'   # HV could not reach nominal value
    }
    
    # Device state bits (from COM-AMPR-12.h)
    DEVICE_STATE = {
        (1 << 0x0): 'DS_PSU_ENB',     # PSUs enabled
        (1 << 0x8): 'DS_VOLT_FAIL',   # Supply voltages failure
        (1 << 0x9): 'DS_HV_FAIL',     # High voltages failure
        (1 << 0xA): 'DS_FAN_FAIL',    # Fan failure
        (1 << 0xB): 'DS_ILOCK_FAIL',  # Interlock failure
        (1 << 0xC): 'DS_MODULE_FAIL', # Module configuration failure
        (1 << 0xD): 'DS_RATING_FAIL', # Module rating failure
        (1 << 0xE): 'DS_HV_STOP'      # HV PSUs were turned off
    }
    
    # Voltage state bits (from COM-AMPR-12.h)
    VOLTAGE_STATE = {
        (1 << 0x0): 'VS_3V3_OK',   # +3V3 rail voltage OK
        (1 << 0x1): 'VS_5V0_OK',   # +5V0 rail voltage OK
        (1 << 0x2): 'VS_12V_OK',   # +12V rail voltage OK
        (1 << 0x3): 'VS_LINE_ON',  # Line voltage OK
        (1 << 0x4): 'VS_12VP_OK',  # +12Va rail voltage OK
        (1 << 0x5): 'VS_12VN_OK',  # -12Va rail voltage OK
        (1 << 0x6): 'VS_HVP_OK',   # Positive high voltage OK
        (1 << 0x7): 'VS_HVN_OK',   # Negative high voltage OK
        (1 << 0x8): 'VS_HVP_NZ',   # Positive high voltage non-zero
        (1 << 0x9): 'VS_HVN_NZ',   # Negative high voltage non-zero
        (1 << 0xF): 'VS_ICL_ON'    # ICL active, i.e. shorted
    }
    
    # Temperature state bits (from COM-AMPR-12.h)
    TEMPERATURE_STATE = {
        (1 << 0x0): 'TS_HVPPSU_HIGH',  # +HV PSU overheated
        (1 << 0x1): 'TS_HVNPSU_HIGH',  # -HV PSU overheated
        (1 << 0x2): 'TS_AVPSU_HIGH',   # AV PSU overheated
        (1 << 0x3): 'TS_TADC_HIGH',    # ADC overheated
        (1 << 0x4): 'TS_TCPU_HIGH',    # CPU overheated
        (1 << 0x8): 'TS_HVPPSU_LOW',   # +HV PSU too cold
        (1 << 0x9): 'TS_HVNPSU_LOW',   # -HV PSU too cold
        (1 << 0xA): 'TS_AVPSU_LOW',    # AV PSU too cold
        (1 << 0xB): 'TS_TADC_LOW',     # ADC too cold
        (1 << 0xC): 'TS_TCPU_LOW'      # CPU too cold
    }
    
    # Interlock state bits (from COM-AMPR-12.h)
    INTERLOCK_STATE = {
        (1 << 0x0): 'SI_ILOCK_FRONT_ENB',   # Front interlock enable
        (1 << 0x1): 'SI_ILOCK_REAR_ENB',    # Rear interlock enable
        (1 << 0x2): 'SI_ILOCK_FRONT_INV',   # Front interlock invert
        (1 << 0x3): 'SI_ILOCK_REAR_INV',    # Rear interlock invert
        (1 << 0x8): 'SI_ILOCK_FRONT',       # Front interlock level
        (1 << 0x9): 'SI_ILOCK_REAR',        # Rear interlock level
        (1 << 0xA): 'SI_ILOCK_FRONT_LAST',  # Last front interlock level
        (1 << 0xB): 'SI_ILOCK_REAR_LAST',   # Last rear interlock level
        (1 << 0xF): 'SI_ILOCK_ENB'          # Interlock state
    }
    
    # Module constants
    MODULE_NUM = 12          # Maximum module number
    CHANNEL_NUM = 4          # Channels per module
    MAX_ABS_MODULE_VOLTAGE = 1000.0  # Voltage rating per AMPR module channel
    ADDR_BASE = 0x80        # Base-module address
    ADDR_BROADCAST = 0xFF   # Broadcasting address
    
    # Module presence return values
    MODULE_NOT_FOUND = 0    # No module found
    MODULE_PRESENT = 1      # Module with proper type found
    MODULE_INVALID = 2      # Module found but has invalid type
    
    # Fan constants
    FAN_PWM_MAX = 10000     # Maximum PWM value (100%)
    
    # Device type
    DEVICE_TYPE = 0xA3D8    # Expected device type

    def __init__(self, com, log=None, idn="", dll_path=None, error_codes_path=None):
        """
        Initialize the low-level AMPR driver.

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
            raise AMPRPlatformError(
                "CGC AMPR is supported only on Windows because it depends on COM-AMPR-12.dll."
            )

        self.ampr_dll_path = Path(dll_path) if dll_path is not None else (
            class_dir / "vendor" / "x64" / "COM-AMPR-12.dll"
        )
        try:
            self.ampr_dll = ctypes.WinDLL(str(self.ampr_dll_path))
        except OSError as exc:
            raise AMPRDllLoadError(
                f"Unable to load CGC AMPR DLL from '{self.ampr_dll_path}'."
            ) from exc

        err_path = Path(error_codes_path) if error_codes_path is not None else (
            class_dir.parent / "error_codes.json"
        )
        with err_path.open("rb") as f:
            self.err_dict = json.load(f)

        self.com = com
        self.log = log
        self.idn = idn

    def describe_error(self, status):
        """Return the vendor message for a driver status code."""
        return self.err_dict.get(str(status), "Unknown status code")

    def format_status(self, status):
        """Return a compact '<code> (<message>)' representation."""
        return f"{status} ({self.describe_error(status)})"

    def open_port(self, com_number):
        """
        Open the communication link to the device.

        Parameters
        ----------
        com_number : int
            COM port number.

        Returns
        -------
        int
            Status code.

        """
        status = self.ampr_dll.COM_AMPR_12_Open(ctypes.c_ubyte(com_number))
        return status

    def close_port(self):
        """
        Close the communication link.

        Returns
        -------
        int
            Status code.

        """
        status = self.ampr_dll.COM_AMPR_12_Close()
        return status

    def set_baud_rate(self, baud_rate):
        """
        Set communication speed.

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
        status = self.ampr_dll.COM_AMPR_12_SetBaudRate(ctypes.byref(baud_rate_ref))
        return status, baud_rate_ref.value

    def purge(self):
        """
        Clear data buffers for the port.

        Returns
        -------
        int
            Status code.

        """
        status = self.ampr_dll.COM_AMPR_12_Purge()
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
        status = self.ampr_dll.COM_AMPR_12_DevicePurge(ctypes.byref(empty))
        return status, empty.value

    def get_buffer_state(self):
        """
        Return true if the input data buffer of the device is empty.

        Returns
        -------
        tuple
            (status, empty) where empty is True if buffer is empty.

        """
        empty = ctypes.c_bool()
        status = self.ampr_dll.COM_AMPR_12_GetBufferState(ctypes.byref(empty))
        return status, empty.value

    # General device information methods
    
    def get_sw_version(self):
        """
        Get the COM-AMPR-12 software version.

        Returns
        -------
        int
            Software version.

        """
        version = self.ampr_dll.COM_AMPR_12_GetSWVersion()
        return version

    def get_fw_version(self):
        """
        Get the firmware version.

        Returns
        -------
        tuple
            (status, version).

        """
        version = ctypes.c_ushort()
        status = self.ampr_dll.COM_AMPR_12_GetFwVersion(ctypes.byref(version))
        return status, version.value

    def get_fw_date(self):
        """
        Get the firmware date.

        Returns
        -------
        tuple
            (status, date_string).

        """
        date_string = ctypes.create_string_buffer(12)
        status = self.ampr_dll.COM_AMPR_12_GetFwDate(date_string)
        return status, date_string.value.decode()

    def get_product_id(self):
        """
        Get the product identification.

        Returns
        -------
        tuple
            (status, identification).

        """
        identification = ctypes.create_string_buffer(81)
        status = self.ampr_dll.COM_AMPR_12_GetProductID(identification)
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
        status = self.ampr_dll.COM_AMPR_12_GetProductNo(ctypes.byref(number))
        return status, number.value

    def get_manuf_date(self):
        """
        Get the manufacturing date.

        Returns
        -------
        tuple
            (status, year, calendar_week).

        """
        year = ctypes.c_ushort()
        calendar_week = ctypes.c_ushort()
        status = self.ampr_dll.COM_AMPR_12_GetManufDate(ctypes.byref(year), ctypes.byref(calendar_week))
        return status, year.value, calendar_week.value

    def get_device_type(self):
        """
        Get the device type.

        Returns
        -------
        tuple
            (status, device_type).

        """
        device_type = ctypes.c_ushort()
        status = self.ampr_dll.COM_AMPR_12_GetDevType(ctypes.byref(device_type))
        return status, device_type.value

    def get_hw_type(self):
        """
        Get the hardware type.

        Returns
        -------
        tuple
            (status, hw_type).

        """
        hw_type = ctypes.c_uint32()
        status = self.ampr_dll.COM_AMPR_12_GetHwType(ctypes.byref(hw_type))
        return status, hw_type.value

    def get_hw_version(self):
        """
        Get the hardware version.

        Returns
        -------
        tuple
            (status, hw_version).

        """
        hw_version = ctypes.c_ushort()
        status = self.ampr_dll.COM_AMPR_12_GetHwVersion(ctypes.byref(hw_version))
        return status, hw_version.value

    def get_uptime(self):
        """
        Get current and total device uptimes.

        Returns
        -------
        tuple
            (status, sec, millisec, total_sec, total_millisec).

        """
        sec = ctypes.c_uint32()
        millisec = ctypes.c_ushort()
        total_sec = ctypes.c_uint32()
        total_millisec = ctypes.c_ushort()
        
        status = self.ampr_dll.COM_AMPR_12_GetUptime(
            ctypes.byref(sec), ctypes.byref(millisec), 
            ctypes.byref(total_sec), ctypes.byref(total_millisec))
        
        return status, sec.value, millisec.value, total_sec.value, total_millisec.value

    def get_optime(self):
        """
        Get current and total device operation times.

        Returns
        -------
        tuple
            (status, sec, millisec, total_sec, total_millisec).

        """
        sec = ctypes.c_uint32()
        millisec = ctypes.c_ushort()
        total_sec = ctypes.c_uint32()
        total_millisec = ctypes.c_ushort()
        
        status = self.ampr_dll.COM_AMPR_12_GetOptime(
            ctypes.byref(sec), ctypes.byref(millisec), 
            ctypes.byref(total_sec), ctypes.byref(total_millisec))
        
        return status, sec.value, millisec.value, total_sec.value, total_millisec.value

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
        status = self.ampr_dll.COM_AMPR_12_GetCPUdata(ctypes.byref(load), ctypes.byref(frequency))
        return status, load.value, frequency.value

    def get_housekeeping(self):
        """
        Get the housekeeping data.

        Returns
        -------
        tuple
            (status, volt_12v, volt_5v0, volt_3v3, volt_agnd, volt_12vp, volt_12vn,
             volt_hvp, volt_hvn, temp_cpu, temp_adc, temp_av, temp_hvp, temp_hvn, line_freq).

        """
        volt_12v = ctypes.c_double()
        volt_5v0 = ctypes.c_double()
        volt_3v3 = ctypes.c_double()
        volt_agnd = ctypes.c_double()
        volt_12vp = ctypes.c_double()
        volt_12vn = ctypes.c_double()
        volt_hvp = ctypes.c_double()
        volt_hvn = ctypes.c_double()
        temp_cpu = ctypes.c_double()
        temp_adc = ctypes.c_double()
        temp_av = ctypes.c_double()
        temp_hvp = ctypes.c_double()
        temp_hvn = ctypes.c_double()
        line_freq = ctypes.c_double()
        
        status = self.ampr_dll.COM_AMPR_12_GetHousekeeping(
            ctypes.byref(volt_12v), ctypes.byref(volt_5v0), ctypes.byref(volt_3v3),
            ctypes.byref(volt_agnd), ctypes.byref(volt_12vp), ctypes.byref(volt_12vn),
            ctypes.byref(volt_hvp), ctypes.byref(volt_hvn), ctypes.byref(temp_cpu),
            ctypes.byref(temp_adc), ctypes.byref(temp_av), ctypes.byref(temp_hvp),
            ctypes.byref(temp_hvn), ctypes.byref(line_freq))
        
        return (status, volt_12v.value, volt_5v0.value, volt_3v3.value, volt_agnd.value,
                volt_12vp.value, volt_12vn.value, volt_hvp.value, volt_hvn.value,
                temp_cpu.value, temp_adc.value, temp_av.value, temp_hvp.value,
                temp_hvn.value, line_freq.value)

    def restart(self):
        """
        Restart the controller.

        Returns
        -------
        int
            Status code.

        """
        status = self.ampr_dll.COM_AMPR_12_Restart()
        return status

    def _read_state_word(self, dll_call):
        """Read a 16-bit state value from the DLL."""
        state = ctypes.c_ushort()
        status = dll_call(ctypes.byref(state))
        return status, state.value

    def _decode_enum_state(self, state_value, mapping):
        """Decode an enum-style state value."""
        return hex(state_value), mapping.get(state_value, f'UNKNOWN_STATE_0x{state_value:04X}')

    def _decode_flag_state(self, state_value, mapping, ok_label=None):
        """Decode a bitmask state value."""
        active_states = []
        if state_value == 0 and ok_label is not None:
            active_states.append(ok_label)
        else:
            for flag, name in mapping.items():
                if state_value & flag:
                    active_states.append(name)
        return hex(state_value), active_states

    def _get_enum_state(self, dll_call, mapping):
        """Read and decode a DLL enum-style state value."""
        status, state_value = self._read_state_word(dll_call)
        if status != self.NO_ERR:
            return status, None, None

        state_hex, state_name = self._decode_enum_state(state_value, mapping)
        return status, state_hex, state_name

    def _get_flag_state(self, dll_call, mapping, ok_label=None):
        """Read and decode a DLL bitmask state value."""
        status, state_value = self._read_state_word(dll_call)
        if status != self.NO_ERR:
            return status, None, None

        state_hex, active_states = self._decode_flag_state(state_value, mapping, ok_label)
        return status, state_hex, active_states

    # AMPR Controller methods
    
    def get_state(self):
        """
        Get device main state.

        Returns
        -------
        tuple
            (status, state_hex, state_name).

        """
        return self._get_enum_state(self.ampr_dll.COM_AMPR_12_GetState, self.MAIN_STATE)

    def get_device_state(self):
        """
        Get device state.

        Returns
        -------
        tuple
            (status, state_hex, state_names).

        """
        return self._get_flag_state(
            self.ampr_dll.COM_AMPR_12_GetDeviceState,
            self.DEVICE_STATE,
            ok_label='DEVICE_OK',
        )

    def enable_psu(self, enable):
        """
        Set PSUs-enable bit in device state.

        Parameters
        ----------
        enable : bool
            Enable state.

        Returns
        -------
        tuple
            (status, enable_value).

        """
        enable_ref = ctypes.c_bool(enable)
        status = self.ampr_dll.COM_AMPR_12_EnablePSU(ctypes.byref(enable_ref))
        return status, enable_ref.value

    def get_voltage_state(self):
        """
        Get voltage state.

        Returns
        -------
        tuple
            (status, state_hex, state_names).

        """
        return self._get_flag_state(
            self.ampr_dll.COM_AMPR_12_GetVoltageState,
            self.VOLTAGE_STATE,
            ok_label='VOLTAGE_OK',
        )

    def get_temperature_state(self):
        """
        Get temperature state.

        Returns
        -------
        tuple
            (status, state_hex, state_names).

        """
        return self._get_flag_state(
            self.ampr_dll.COM_AMPR_12_GetTemperatureState,
            self.TEMPERATURE_STATE,
            ok_label='TEMPERATURE_OK',
        )

    def get_interlock_state(self):
        """
        Get interlock state.

        Returns
        -------
        tuple
            (status, state_hex, state_names).

        """
        return self._get_flag_state(
            self.ampr_dll.COM_AMPR_12_GetInterlockState,
            self.INTERLOCK_STATE,
        )

    def set_interlock_state(self, interlock_control):
        """
        Set interlock control bits.

        Parameters
        ----------
        interlock_control : int
            Interlock control byte.

        Returns
        -------
        int
            Status code.

        """
        status = self.ampr_dll.COM_AMPR_12_SetInterlockState(ctypes.c_ubyte(interlock_control))
        return status

    def get_inputs(self):
        """
        Get instantaneous device input levels.

        Returns
        -------
        tuple
            (status, interlock_front, interlock_rear, input_sync).

        """
        interlock_front = ctypes.c_bool()
        interlock_rear = ctypes.c_bool()
        input_sync = ctypes.c_bool()
        
        status = self.ampr_dll.COM_AMPR_12_GetInputs(
            ctypes.byref(interlock_front), ctypes.byref(interlock_rear), ctypes.byref(input_sync))
        
        return status, interlock_front.value, interlock_rear.value, input_sync.value

    def get_sync_control(self):
        """
        Get device Sync control.

        Returns
        -------
        tuple
            (status, external, invert, level).

        """
        external = ctypes.c_bool()
        invert = ctypes.c_bool()
        level = ctypes.c_bool()
        
        status = self.ampr_dll.COM_AMPR_12_GetSyncControl(
            ctypes.byref(external), ctypes.byref(invert), ctypes.byref(level))
        
        return status, external.value, invert.value, level.value

    def set_sync_control(self, external, invert, level):
        """
        Set device Sync control.

        Parameters
        ----------
        external : bool
            External sync.
        invert : bool
            Invert sync.
        level : bool
            Sync level.

        Returns
        -------
        int
            Status code.

        """
        status = self.ampr_dll.COM_AMPR_12_SetSyncControl(
            ctypes.c_bool(external), ctypes.c_bool(invert), ctypes.c_bool(level))
        return status

    def get_fan_data(self):
        """
        Get fan data.

        Returns
        -------
        tuple
            (status, failed, max_rpm, set_rpm, measured_rpm, pwm).

        """
        failed = ctypes.c_bool()
        max_rpm = ctypes.c_ushort()
        set_rpm = ctypes.c_ushort()
        measured_rpm = ctypes.c_ushort()
        pwm = ctypes.c_ushort()
        
        status = self.ampr_dll.COM_AMPR_12_GetFanData(
            ctypes.byref(failed), ctypes.byref(max_rpm), ctypes.byref(set_rpm),
            ctypes.byref(measured_rpm), ctypes.byref(pwm))
        
        return status, failed.value, max_rpm.value, set_rpm.value, measured_rpm.value, pwm.value

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
        
        status = self.ampr_dll.COM_AMPR_12_GetLEDData(
            ctypes.byref(red), ctypes.byref(green), ctypes.byref(blue))
        
        return status, red.value, green.value, blue.value

    # Module service methods
    
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
        module_presence = (ctypes.c_ubyte * (self.MODULE_NUM + 1))()
        
        status = self.ampr_dll.COM_AMPR_12_GetModulePresence(
            ctypes.byref(valid), ctypes.byref(max_module), module_presence)
        
        presence_list = [module_presence[i] for i in range(self.MODULE_NUM + 1)]
        return status, valid.value, max_module.value, presence_list

    def update_module_presence(self):
        """
        Update module-presence flags.

        Returns
        -------
        int
            Status code.

        """
        status = self.ampr_dll.COM_AMPR_12_UpdateModulePresence()
        return status

    def rescan_modules(self):
        """
        Rescan address pins of all modules.

        Returns
        -------
        int
            Status code.

        """
        status = self.ampr_dll.COM_AMPR_12_RescanModules()
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
        status = self.ampr_dll.COM_AMPR_12_RescanModule(ctypes.c_uint(address))
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
        status = self.ampr_dll.COM_AMPR_12_RestartModule(ctypes.c_uint(address))
        return status

    def get_scanned_module_state(self):
        """
        Get the state of the module scan.

        Returns
        -------
        tuple
            (status, module_mismatch, rating_failure).

        """
        module_mismatch = ctypes.c_bool()
        rating_failure = ctypes.c_bool()
        
        status = self.ampr_dll.COM_AMPR_12_GetScannedModuleState(
            ctypes.byref(module_mismatch), ctypes.byref(rating_failure))
        
        return status, module_mismatch.value, rating_failure.value

    def set_scanned_module_state(self):
        """
        Reset the module mismatch, i.e save the current device configuration.

        Returns
        -------
        int
            Status code.

        """
        status = self.ampr_dll.COM_AMPR_12_SetScannedModuleState()
        return status

    def get_scanned_module_params(self, address):
        """
        Get scanned module parameters.

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
        
        status = self.ampr_dll.COM_AMPR_12_GetScannedModuleParams(
            ctypes.c_uint(address), ctypes.byref(scanned_product_no), 
            ctypes.byref(saved_product_no), ctypes.byref(scanned_hw_type), 
            ctypes.byref(saved_hw_type))
        
        return (status, scanned_product_no.value, saved_product_no.value,
                scanned_hw_type.value, saved_hw_type.value)

    def get_module_fw_version(self, address):
        """
        Get the module firmware version.

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
        status = self.ampr_dll.COM_AMPR_12_GetModuleFwVersion(
            ctypes.c_uint(address), ctypes.byref(fw_version))
        return status, fw_version.value

    def get_module_product_id(self, address):
        """
        Get the module product identification string.

        Parameters
        ----------
        address : int
            Module address.

        Returns
        -------
        tuple
            (status, identification).

        """
        identification = ctypes.create_string_buffer(81)
        status = self.ampr_dll.COM_AMPR_12_GetModuleProductID(
            ctypes.c_uint(address), identification
        )
        return status, identification.value.decode()

    def get_module_product_no(self, address):
        """
        Get the module product number.

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
        status = self.ampr_dll.COM_AMPR_12_GetModuleProductNo(
            ctypes.c_uint(address), ctypes.byref(product_no))
        return status, product_no.value

    def get_module_hw_type(self, address):
        """
        Get the module hardware type.

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
        status = self.ampr_dll.COM_AMPR_12_GetModuleHwType(
            ctypes.c_uint(address), ctypes.byref(hw_type))
        return status, hw_type.value

    def get_module_hw_version(self, address):
        """
        Get the module hardware version.

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
        status = self.ampr_dll.COM_AMPR_12_GetModuleHwVersion(
            ctypes.c_uint(address), ctypes.byref(hw_version))
        return status, hw_version.value

    def get_module_state(self, address):
        """
        Get the module state.

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
        status = self.ampr_dll.COM_AMPR_12_GetModuleState(
            ctypes.c_uint(address), ctypes.byref(state))
        return status, state.value

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
            (status, volt_3v3, temp_cpu, volt_5v0, volt_12vp, volt_12vn,
             volt_1v8p, volt_1v8n). The seven output doubles match the order
             declared by COM-AMPR-12.h GetModuleHousekeeping (Address then
             Volt3V3, TempCPU, Volt5V0, Volt12Vp, Volt12Vn, Volt1V8p, Volt1V8n).

        """
        volt_3v3 = ctypes.c_double()
        temp_cpu = ctypes.c_double()
        volt_5v0 = ctypes.c_double()
        volt_12vp = ctypes.c_double()
        volt_12vn = ctypes.c_double()
        volt_1v8p = ctypes.c_double()
        volt_1v8n = ctypes.c_double()

        status = self.ampr_dll.COM_AMPR_12_GetModuleHousekeeping(
            ctypes.c_uint(address), ctypes.byref(volt_3v3), ctypes.byref(temp_cpu),
            ctypes.byref(volt_5v0), ctypes.byref(volt_12vp), ctypes.byref(volt_12vn),
            ctypes.byref(volt_1v8p), ctypes.byref(volt_1v8n))

        return (status, volt_3v3.value, temp_cpu.value, volt_5v0.value,
                volt_12vp.value, volt_12vn.value, volt_1v8p.value, volt_1v8n.value)

    # Voltage control methods for modules
    
    def set_module_voltage(self, address, channel, voltage):
        """
        Set module output voltage.

        Parameters
        ----------
        address : int
            Module address (0-11).
        channel : int
            Channel number (1-4).
        voltage : float
            Voltage to set.

        Returns
        -------
        int
            Status code.

        """
        if not 1 <= channel <= self.CHANNEL_NUM:
            return self.ERR_ARGUMENT
        try:
            voltage = float(voltage)
        except (TypeError, ValueError):
            return self.ERR_ARGUMENT
        if not math.isfinite(voltage) or abs(voltage) > self.MAX_ABS_MODULE_VOLTAGE:
            return self.ERR_ARGUMENT

        status = self.ampr_dll.COM_AMPR_12_SetModuleOutputVoltage(
            ctypes.c_uint(address), ctypes.c_uint(channel - 1), ctypes.c_double(voltage))
        return status

    def get_module_voltage_setpoint(self, address, channel):
        """
        Get module voltage setpoint.

        Parameters
        ----------
        address : int
            Module address (0-11).
        channel : int
            Channel number (1-4).

        Returns
        -------
        tuple
            (status, voltage).

        """
        if not 1 <= channel <= self.CHANNEL_NUM:
            return self.ERR_ARGUMENT, None

        voltage = ctypes.c_double()
        status = self.ampr_dll.COM_AMPR_12_GetModuleOutputVoltage(
            ctypes.c_uint(address), ctypes.c_uint(channel - 1), ctypes.byref(voltage))
        return status, voltage.value

    def get_module_voltage_measured(self, address, channel):
        """
        Get module measured voltage.

        Parameters
        ----------
        address : int
            Module address (0-11).
        channel : int
            Channel number (1-4).

        Returns
        -------
        tuple
            (status, voltage).

        """
        if not 1 <= channel <= self.CHANNEL_NUM:
            return self.ERR_ARGUMENT, None

        status, voltages = self.get_all_module_voltage_measured(address)
        if status != self.NO_ERR:
            return status, None

        return status, voltages[channel - 1]

    def get_all_module_voltage_measured(self, address):
        """
        Get all measured voltages for one module in a single DLL call.

        Parameters
        ----------
        address : int
            Module address (0-11).

        Returns
        -------
        tuple
            (status, voltages) where voltages is a list of the 4 channel values.

        """
        voltages = (ctypes.c_double * self.CHANNEL_NUM)()
        status = self.ampr_dll.COM_AMPR_12_GetMeasuredModuleOutputVoltages(
            ctypes.c_uint(address), voltages)
        if status != self.NO_ERR:
            return status, None

        return status, [float(voltage) for voltage in voltages]
    
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
        
        # `presence_list` includes one extra entry at index 12 for the base module.
        # Only addresses 0..11 are pilotable amplifier modules.
        max_address = min(max_module, self.MODULE_NUM - 1)
        for addr in range(max_address + 1):
            if presence_list[addr] == self.MODULE_PRESENT:
                module_info = {}
                
                # Get module firmware version
                fw_status, fw_version = self.get_module_fw_version(addr)
                if fw_status == self.NO_ERR:
                    module_info['fw_version'] = fw_version
                
                # Get module product number
                prod_status, product_no = self.get_module_product_no(addr)
                if prod_status == self.NO_ERR:
                    module_info['product_no'] = product_no

                # Get module product identification
                product_id_status, product_id = self.get_module_product_id(addr)
                if product_id_status == self.NO_ERR:
                    module_info['product_id'] = product_id

                # Get module hardware info
                hw_status, hw_type = self.get_module_hw_type(addr)
                if hw_status == self.NO_ERR:
                    module_info['hw_type'] = hw_type
                
                hwv_status, hw_version = self.get_module_hw_version(addr)
                if hwv_status == self.NO_ERR:
                    module_info['hw_version'] = hw_version
                
                # Get module state
                state_status, state = self.get_module_state(addr)
                if state_status == self.NO_ERR:
                    module_info['state'] = state
                
                modules[addr] = module_info
        
        return modules

    def get_all_module_voltages(self, address):
        """
        Get all channel voltages for a specific module.

        Parameters
        ----------
        address : int
            Module address.

        Returns
        -------
        dict
            Dictionary with channel numbers as keys and (setpoint, measured) tuples as values.

        """
        voltages = {}
        meas_status, measured_voltages = self.get_all_module_voltage_measured(address)
        
        for channel in range(1, self.CHANNEL_NUM + 1):
            # Get setpoint
            set_status, setpoint = self.get_module_voltage_setpoint(address, channel)
            measured = None
            if meas_status == self.NO_ERR:
                measured = measured_voltages[channel - 1]
            
            if set_status == self.NO_ERR and meas_status == self.NO_ERR:
                voltages[channel] = {
                    'setpoint': setpoint,
                    'measured': measured
                }
            elif set_status == self.NO_ERR:
                voltages[channel] = {
                    'setpoint': setpoint,
                    'measured': None
                }
            elif meas_status == self.NO_ERR:
                voltages[channel] = {
                    'setpoint': None,
                    'measured': measured
                }
        
        return voltages

    def set_all_module_voltages(self, address, voltages):
        """
        Set voltages for all channels of a module.

        Parameters
        ----------
        address : int
            Module address.
        voltages : list or dict
            If list: voltages for channels 1-4
            If dict: {channel: voltage} mapping

        Returns
        -------
        dict
            Dictionary with channel numbers as keys and status codes as values.

        """
        results = {}
        
        if isinstance(voltages, list):
            # List format: [ch1, ch2, ch3, ch4]
            for i, voltage in enumerate(voltages[: self.CHANNEL_NUM]):
                channel = i + 1
                if voltage is not None:
                    status = self.set_module_voltage(address, channel, voltage)
                    results[channel] = status
        elif isinstance(voltages, dict):
            # Dictionary format: {channel: voltage}
            for channel, voltage in voltages.items():
                if 1 <= channel <= self.CHANNEL_NUM and voltage is not None:
                    status = self.set_module_voltage(address, channel, voltage)
                    results[channel] = status
        
        return results
