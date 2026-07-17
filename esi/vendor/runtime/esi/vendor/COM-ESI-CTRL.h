/******************************************************************************
//                                                                           //
//  Project: Software Interface for ESI-CTRL Devices                         //
//                                                                           //
//  CGC Instruments, (c) 2010-2026, Version 1-00. All rights reserved.       //
//                                                                           //
//  Definition for COM-ESI-CTRL DLL Routines                                 //
//                                                                           //
******************************************************************************/

#ifndef __COM_ESI_CTRL_H__
#define __COM_ESI_CTRL_H__

#ifdef __cplusplus
extern "C" {
#endif

/*
	The communication channel must be opened before the first usage.

	If necessary, the channel may be closed and reopened again.
	The channel is closed automatically at the end of the program, the main program does not need to take care about it.

	The return values of the routines are the following defines COM_ESI_CTRL_ERR_XXX
		 0: routine finished without any error (COM_ESI_CTRL_NOERR)
		<0: errors COM_ESI_CTRL_ERR_XXX

	The last error code can be obtained by calling COM_ESI_CTRL_State.
	The routine COM_ESI_CTRL_ErrorMessage returns a pointer to a zero-terminated character string or NULL in case of a failure.

	If a communication error occurred, the error code can be red by COM_ESI_CTRL_IO_State,
	The routine COM_ESI_CTRL_IO_ErrorMessage provides the corresponding error message.

	The last communication-port error code returned by the operating system can be obtained by COM_ESI_CTRL_GetCommError.
	The routine COM_ESI_CTRL_GetCommErrorMessage provides the corresponding error message.
*/

/****************
// Error codes //
****************/

#define COM_ESI_CTRL_NO_ERR              (   0) // No error occurred
#define COM_ESI_CTRL_ERR_OPEN            (  -2) // Error opening the port
#define COM_ESI_CTRL_ERR_CLOSE           (  -3) // Error closing the port
#define COM_ESI_CTRL_ERR_PURGE           (  -4) // Error purging the port
#define COM_ESI_CTRL_ERR_CONTROL         (  -5) // Error setting the port control lines
#define COM_ESI_CTRL_ERR_STATUS          (  -6) // Error reading the port status lines
#define COM_ESI_CTRL_ERR_COMMAND_SEND    (  -7) // Error sending command
#define COM_ESI_CTRL_ERR_DATA_SEND       (  -8) // Error sending data
#define COM_ESI_CTRL_ERR_TERM_SEND       (  -9) // Error sending termination character
#define COM_ESI_CTRL_ERR_COMMAND_RECEIVE ( -10) // Error receiving command
#define COM_ESI_CTRL_ERR_DATA_RECEIVE    ( -11) // Error receiving data
#define COM_ESI_CTRL_ERR_TERM_RECEIVE    ( -12) // Error receiving termination character
#define COM_ESI_CTRL_ERR_COMMAND_WRONG   ( -13) // Wrong command received
#define COM_ESI_CTRL_ERR_ARGUMENT_WRONG  ( -14) // Wrong argument received
#define COM_ESI_CTRL_ERR_ARGUMENT        ( -15) // Wrong argument passed to the function
#define COM_ESI_CTRL_ERR_RATE            ( -16) // Error setting the baud rate

#define COM_ESI_CTRL_ERR_NOT_CONNECTED   (-100) // Device not connected
#define COM_ESI_CTRL_ERR_NOT_READY       (-101) // Device not ready
#define COM_ESI_CTRL_ERR_READY           (-102) // Device state could not be set to not ready

#define COM_ESI_CTRL_ERR_BUFF_FULL       (-200) // Buffer for automatic notification data full

#define COM_ESI_CTRL_NO_DATA             (   1) // No new data is available
#define COM_ESI_CTRL_AUTO_MEAS_CUR       (   2) // Automatic current data received

WORD _export COM_ESI_CTRL_GetSWVersion(); // Get the COM-VOLTCONTROL12 software version


/******************
// Communication //
******************/

int _export COM_ESI_CTRL_Open (BYTE COMNumber); // Open communication port
int _export COM_ESI_CTRL_Close();               // Close communication port

int _export COM_ESI_CTRL_SetBaudRate (unsigned & BaudRate); // Set baud rate and return set value

int _export COM_ESI_CTRL_Purge();                       // Clear data buffers for the communication port
int _export COM_ESI_CTRL_GetBufferState (bool & Empty); // Return true if the input data buffer of the device is empty
int _export COM_ESI_CTRL_DevicePurge    (bool & Empty); // Clear output data buffer of the device, return value as COM_ESI_CTRL_Buffer_State

int _export COM_ESI_CTRL_GetAutoMask    (unsigned & CommandMask); // Get the mask of the last automatic notification data, can be used to check whether new data has been read during the last communication(s)
int _export COM_ESI_CTRL_CheckAutoInput (unsigned & CommandMask); // Check for new automatic notification data


/************
// General //
************/

int _export COM_ESI_CTRL_GetFwVersion ( WORD & FwVersion);                 // Get firmware version
#define COM_ESI_CTRL_DATA_STRING_SIZE  12                                  // DateString size for COM_ESI_CTRL_GetFwDate
int _export COM_ESI_CTRL_GetFwDate    ( char * DateString);                // Get firmware date
#define COM_ESI_CTRL_PRODUCT_ID_SIZE   81                                  // Identification size for COM_ESI_CTRL_GetProductID
int _export COM_ESI_CTRL_GetProductID ( char * Identification);            // Get product identification
int _export COM_ESI_CTRL_GetProductNo (DWORD & ProductNo);                 // Get product number
int _export COM_ESI_CTRL_GetManufDate ( WORD & Year, WORD & CalendarWeek); // Get manufacturing date
#define COM_ESI_CTRL_DEVICE_TYPE (0x8ED6)                                  // Expected device type
int _export COM_ESI_CTRL_GetDevType   ( WORD & DevType);                   // Get device type
int _export COM_ESI_CTRL_GetHwType    (DWORD & HwType);                    // Get hardware type
int _export COM_ESI_CTRL_GetHwVersion ( WORD & HwVersion);                 // Get hardware version

int _export COM_ESI_CTRL_GetUptimeInt (DWORD & Seconds, WORD & Milliseconds, DWORD & TotalSeconds, WORD & TotalMilliseconds); // Get current and total device uptimes as integer values
int _export COM_ESI_CTRL_GetOptimeInt (DWORD & Seconds, WORD & Milliseconds, DWORD & TotalSeconds, WORD & TotalMilliseconds); // Get current and total device operation times as integer values
int _export COM_ESI_CTRL_GetUptime    (double & Seconds,                     double & TotalSeconds                         ); // Get current and total device uptimes
int _export COM_ESI_CTRL_GetOptime    (double & Seconds,                     double & TotalSeconds                         ); // Get current and total device operation times
int _export COM_ESI_CTRL_GetCPUdata   (double & Load, double & Frequency);                                                    // Get CPU load (0-1 = 0-100%) and frequency (Hz)

int _export COM_ESI_CTRL_GetHousekeeping (double & Volt24V, double & Volt5V0, double & Volt3V3, double & TempCPU, double & TempPSU); // Get housekeeping data

int _export COM_ESI_CTRL_Restart(); // Restart the controller


/*******************
// ESI controller //
*******************/

int _export COM_ESI_CTRL_SetActivationState (bool   ActivationState); // Set device activation state
int _export COM_ESI_CTRL_GetActivationState (bool & ActivationState); // Get device activation state

// Controller's data-ready flags
#define COM_ESI_CTRL_HK_RDY  (1 << 0) // housekeeping data ready
#define COM_ESI_CTRL_HK_OFL  (1 << 1) // housekeeping data overflow
#define COM_ESI_CTRL_FAN_RDY (1 << 2) // fan data ready
#define COM_ESI_CTRL_FAN_OFL (1 << 3) // fan data overflow
int _export COM_ESI_CTRL_GetDataReadyFlags (BYTE & DataReadyFlags); // Get data-ready flags

// Controller status values:
#define COM_ESI_CTRL_ST_ON             (   0)                      // Modules are on
#define COM_ESI_CTRL_ST_ERROR          (1<<4)                      // General error
#define COM_ESI_CTRL_ST_ERR_MODULE     (COM_ESI_CTRL_ST_ERROR + 1) // DPA-1F-module error
#define COM_ESI_CTRL_ST_ERR_VSUP       (COM_ESI_CTRL_ST_ERROR + 2) // Supply-voltage error
#define COM_ESI_CTRL_ST_ERR_TEMP_LOW   (COM_ESI_CTRL_ST_ERROR + 3) // Low-temperature error
#define COM_ESI_CTRL_ST_ERR_TEMP_HIGH  (COM_ESI_CTRL_ST_ERROR + 4) // Overheating error
#define COM_ESI_CTRL_ST_ERR_ILOCK      (COM_ESI_CTRL_ST_ERROR + 5) // Interlock error
int _export COM_ESI_CTRL_GetState (WORD & State); // Get device state

// Controller's device state bits:
#define COM_ESI_CTRL_DS_ILOCK_FAIL      (1<<0)                // Interlock failure
#define COM_ESI_CTRL_DS_VOLT_FAIL       (1<<1)                // Supply voltages failure
#define COM_ESI_CTRL_DS_TEMP_FAIL       (1<<2)                // Temperature failure
#define COM_ESI_CTRL_DS_FAN_FAIL        (1<<3)                // Fan failure
#define COM_ESI_CTRL_DS_MODULE_FAIL     (1<<4)                // Module configuration failure
int _export COM_ESI_CTRL_GetDeviceState (BYTE & DeviceState); // Get device state
int _export COM_ESI_CTRL_SetEnable      (bool   Enable);      // Enable/disable modules
int _export COM_ESI_CTRL_GetEnable      (bool & Enable);      // Get module enable state

// Controller's voltage state bits:
#define COM_ESI_CTRL_VS_3V3_OK   (1<<0) // +3V3 rail voltage OK
#define COM_ESI_CTRL_VS_5V0_OK   (1<<1) // +5V0 rail voltage OK
#define COM_ESI_CTRL_VS_24V_OK   (1<<2) // +24V rail voltage OK
#define COM_ESI_CTRL_VS_LINE_OK  (1<<4) // Line voltage OK
#define COM_ESI_CTRL_VS_PSU_OK   (1<<5) // PSU output voltage OK
#define COM_ESI_CTRL_VS_SUPL_OK  (COM_ESI_CTRL_VS_3V3_OK  | COM_ESI_CTRL_VS_5V0_OK | COM_ESI_CTRL_VS_12V_OK) // Supply voltages OK
#define COM_ESI_CTRL_VS_OK       (COM_ESI_CTRL_VS_SUP_OK | COM_ESI_CTRL_VS_LINE_OK | COM_ESI_CTRL_VS_PSU_OK) // All supply voltages OK
int _export COM_ESI_CTRL_GetVoltageState (BYTE & VoltageState); // Get voltage state

#define COM_ESI_CTRL_FS_FAN_OK  (1<<0)                  // Fan OK
int _export COM_ESI_CTRL_GetFanState (BYTE & FanState); // Get fan state

// Controller's temperature state bits:
#define COM_ESI_CTRL_TS_TCPU_HIGH    (1<<0x0) //     CPU overheated
#define COM_ESI_CTRL_TS_TPSU_HIGH    (1<<0x1) //     PSU overheated
#define COM_ESI_CTRL_TS_TCPU_LOW     (1<<0x2) //     CPU too cold
#define COM_ESI_CTRL_TS_TPSU_LOW     (1<<0x3) //     PSU too cold
int _export COM_ESI_CTRL_GetTemperatureState (BYTE & TemperatureState); // Get temperature state

// Heat controller interlock bits
#define COM_ESI_CTRL_IS_HTCTRL_ILOCK1_CURR  (1<<0x8)
#define COM_ESI_CTRL_IS_HTCTRL_ILOCK2_CURR  (1<<0x9)
#define COM_ESI_CTRL_IS_HTCTRL_ILOCK1_LAST  (1<<0xA)
#define COM_ESI_CTRL_IS_HTCTRL_ILOCK2_LAST  (1<<0xB)
#define COM_ESI_CTRL_IS_HTCTRL_ILOCK1_ACT   (IS_HTCTRL_ILOCK1_CURR | IS_HTCTRL_ILOCK1_LAST)
#define COM_ESI_CTRL_IS_HTCTRL_ILOCK2_ACT   (IS_HTCTRL_ILOCK2_CURR | IS_HTCTRL_ILOCK2_LAST)
#define COM_ESI_CTRL_IS_HTCTRL_ILOCK_MASK   (IS_HTCTRL_ILOCK1_ACT  | IS_HTCTRL_ILOCK2_ACT )
#define COM_ESI_CTRL_IS_HTCTRL_ILOCK_ACT    (IS_HTCTRL_ILOCK_MASK)
// Controller interlock bits
#define COM_ESI_CTRL_IS_CTRL_ILOCK_FP_CURR  (1<<0xC)
#define COM_ESI_CTRL_IS_CTRL_ILOCK_RP_CURR  (1<<0xD)
#define COM_ESI_CTRL_IS_CTRL_ILOCK_FP_LAST  (1<<0xE)
#define COM_ESI_CTRL_IS_CTRL_ILOCK_RP_LAST  (1<<0xF)
#define COM_ESI_CTRL_IS_CTRL_ILOCK_FP_ACT   (IS_CTRL_ILOCK_FP_CURR | IS_CTRL_ILOCK_FP_LAST)
#define COM_ESI_CTRL_IS_CTRL_ILOCK_RP_ACT   (IS_CTRL_ILOCK_RP_CURR | IS_CTRL_ILOCK_RP_LAST)
#define COM_ESI_CTRL_IS_CTRL_ILOCK_MASK     (IS_CTRL_ILOCK_FP_ACT  | IS_CTRL_ILOCK_RP_ACT )
#define COM_ESI_CTRL_IS_CTRL_ILOCK_ACT      (IS_CTRL_ILOCK_MASK)
// Main interlock bits
#define COM_ESI_CTRL_IS_HTCTRL_ILOCK1       (1<<0x0)
#define COM_ESI_CTRL_IS_HTCTRL_ILOCK2       (1<<0x1)
#define COM_ESI_CTRL_IS_CTRL_ILOCK_FP       (1<<0x2)
#define COM_ESI_CTRL_IS_CTRL_ILOCK_RP       (1<<0x3)
#define COM_ESI_CTRL_IS_ILOCK_MASK          (IS_HTCTRL_ILOCK1 | IS_HTCTRL_ILOCK2 | IS_CTRL_ILOCK_FP | IS_CTRL_ILOCK_RP)
int _export COM_ESI_CTRL_GetInterlockState (WORD & InterlockState); // Get interlock state

// Interlock enable bits
#define COM_ESI_CTRL_IE_HTCTRL_ILOCK1  (COM_ESI_CTRL_IS_HTCTRL_ILOCK1)
#define COM_ESI_CTRL_IE_HTCTRL_ILOCK2  (COM_ESI_CTRL_IS_HTCTRL_ILOCK2)
#define COM_ESI_CTRL_IE_CTRL_ILOCK_FP  (COM_ESI_CTRL_IS_CTRL_ILOCK_FP)
#define COM_ESI_CTRL_IE_CTRL_ILOCK_RP  (COM_ESI_CTRL_IS_CTRL_ILOCK_RP)
#define COM_ESI_CTRL_IE_ILOCK_MASK     (IE_HTCTRL_ILOCK1 | IE_HTCTRL_ILOCK2 | IE_CTRL_ILOCK_FP | IE_CTRL_ILOCK_RP)
int _export COM_ESI_CTRL_GetInterlockEnable (BYTE & InterlockEnable); // Get interlock enable mask
int _export COM_ESI_CTRL_SetInterlockEnable (BYTE   InterlockEnable); // Set interlock enable mask

int _export COM_ESI_CTRL_GetInputs        (bool & InterlockFront, bool & InterlockRear, bool & FanEnable); // Get device input levels
int _export COM_ESI_CTRL_GetPowerMonitors (bool & ACin_OK,        bool & DCout_OK);                        // Get power monitor state

int _export COM_ESI_CTRL_GetLEDData (bool & Red, bool & Green, bool & Blue); // Get LED data

int _export COM_ESI_CTRL_GetFanData (bool & Failed, WORD & MaxRPM, WORD & SetRPM, WORD & MeasuredRPM, float & PWM); // Get fan data


/*******************
// Module service //
*******************/

#define COM_ESI_CTRL_MODULE_NUM          4  // Maximum module number
#define COM_ESI_CTRL_ADDR_HTCTRL         0  // Module HTCTRL-24-10 address
#define COM_ESI_CTRL_ADDR_BASE       (0x80) // Base-module address
#define COM_ESI_CTRL_ADDR_BROADCAST  (0xFF) // Broadcasting address

// Return values of COM_ESI_CTRL_GetModulePresence:
#define COM_ESI_CTRL_MODULE_NOT_FOUND  0                                                                                          // No module found
#define COM_ESI_CTRL_MODULE_PRESENT    1                                                                                          // Module with a proper type found
#define COM_ESI_CTRL_MODULE_INVALID    2                                                                                          // Module found but has an invalid type
#define COM_ESI_CTRL_PRESENCE_BASE   (COM_ESI_CTRL_MODULE_NUM)                                                                    // Index of the base-module in the presence flags
int _export COM_ESI_CTRL_GetModulePresence (bool & Valid, unsigned & MaxModule, BYTE ModulePresence [COM_ESI_CTRL_MODULE_NUM+1]); // Get device's maximum module number & module-presence flags
int _export COM_ESI_CTRL_UpdateModulePresence();                                                                                  // Update module-presence flags

int _export COM_ESI_CTRL_RescanModules();                  // Rescan address pins of all modules
int _export COM_ESI_CTRL_RescanModule  (unsigned Address); // Rescan address pins of the specified module

int _export COM_ESI_CTRL_RestartModule (unsigned Address); // Restart the specified module

int _export COM_ESI_CTRL_GetModuleBufferState (unsigned Address, bool & Empty); // Return true if the input data buffer of the specified module is empty
int _export COM_ESI_CTRL_ModulePurge          (unsigned Address, bool & Empty); // Clear output data buffer of the device, return value as COM_ESI_CTRL_GetBufferStateModule


int _export COM_ESI_CTRL_GetScannedModuleState  (bool & ModuleMismatch);                                             // Get the state of the module scan
int _export COM_ESI_CTRL_SetScannedModuleState();                                                                    // Reset the module mismatch, i.e save the current device configuration
int _export COM_ESI_CTRL_GetScannedModuleParams (unsigned Address, DWORD & ScannedProductNo, DWORD & SavedProductNo, // Get scanned & saved product number & hardware type of a module
/*       */                                                        DWORD & ScannedHwType,    DWORD & SavedHwType);

int _export COM_ESI_CTRL_GetModuleFwVersion (unsigned Address,  WORD & FwVersion);                 // Get module firmware version
int _export COM_ESI_CTRL_GetModuleFwDate    (unsigned Address,  char * DateString);                // Get module firmware date
int _export COM_ESI_CTRL_GetModuleProductID (unsigned Address,  char * Identification);            // Get module product identification
int _export COM_ESI_CTRL_GetModuleProductNo (unsigned Address, DWORD & ProductNo);                 // Get module product number
int _export COM_ESI_CTRL_GetModuleManufDate (unsigned Address,  WORD & Year, WORD & CalendarWeek); // Get module manufacturing date

#define COM_ESI_CTRL_MODULE_BASE_TYPE   (0x1C34)                                                   // Expected base device type
#define COM_ESI_CTRL_MODULE_HTCTRL_TYPE (0xDB1C)                                                   // Expected module HTCTRL-24-10 device type
#define COM_ESI_CTRL_MODULE_HVPS_TYPE   (0x0A0D)                                                   // Expected module HVPS-3kB device type
int _export COM_ESI_CTRL_GetModuleDevType   (unsigned Address,   WORD & DevType);                  // Get module device type
int _export COM_ESI_CTRL_GetModuleHwType    (unsigned Address,  DWORD & HwType);                   // Get module hardware type
int _export COM_ESI_CTRL_GetModuleHwVersion (unsigned Address,   WORD & HwVersion);                // Get module hardware version

int _export COM_ESI_CTRL_GetHVsupplyFpgaVersion (unsigned Address, DWORD & FpgaVersion);           // Get HV-PSU FPGA version

int _export COM_ESI_CTRL_GetModuleUptimeInt (unsigned Address, DWORD & Sec, WORD & Milisec, DWORD & TotalSec, WORD & TotalMilisec); // Get current and total module uptimes as integer values
int _export COM_ESI_CTRL_GetModuleOptimeInt (unsigned Address, DWORD & Sec, WORD & Milisec, DWORD & TotalSec, WORD & TotalMilisec); // Get current and total module operation times as integer values
int _export COM_ESI_CTRL_GetModuleUptime    (unsigned Address, double & Seconds,            double & TotalSeconds                ); // Get current and total module uptimes
int _export COM_ESI_CTRL_GetModuleOptime    (unsigned Address, double & Seconds,            double & TotalSeconds                ); // Get current and total module operation times
int _export COM_ESI_CTRL_GetModuleCPUdata   (unsigned Address, double & Load);                                                      // Get CPU load (0-1 = 0-100%)

int _export COM_ESI_CTRL_GetModuleLEDData (unsigned Address, bool & Red, bool & Green, bool & Blue); // Get module LED data

// Data-ready flags of the modules
#define COM_ESI_CTRL_MODULE_HK_RDY  (1 << 2) // Housekeeping data ready
#define COM_ESI_CTRL_MODULE_HK_OFL  (1 << 3) // Housekeeping data overflow

// Data-ready flags of the heat controller
#define COM_ESI_CTRL_HTCTRL_MON_RDY (1 << 0)                     // Monitoring data ready
#define COM_ESI_CTRL_HTCTRL_MON_OFL (1 << 1)                     // Monitoring data overflow
#define COM_ESI_CTRL_HTCTRL_HK_RDY  (COM_ESI_CTRL_MODULE_HK_RDY) // Housekeeping data ready
#define COM_ESI_CTRL_HTCTRL_HK_OFL  (COM_ESI_CTRL_MODULE_HK_OFL) // Housekeeping data overflow

// Data-ready flags of the HV-PSU
#define COM_ESI_CTRL_HVCTRL_PHS_RDY  (1 << 0)                     // Phase data ready
#define COM_ESI_CTRL_HVCTRL_PHS_OFL  (1 << 1)                     // Phase data overflow
#define COM_ESI_CTRL_HVCTRL_HK_RDY   (COM_ESI_CTRL_MODULE_HK_RDY) // Housekeeping data ready
#define COM_ESI_CTRL_HVCTRL_HK_OFL   (COM_ESI_CTRL_MODULE_HK_OFL) // Housekeeping data overflow
#define COM_ESI_CTRL_HVCTRL_ADCV_RDY (1 << 4)                     // Voltage data ready
#define COM_ESI_CTRL_HVCTRL_ADCV_OFL (1 << 5)                     // Voltage data overflow
#define COM_ESI_CTRL_HVCTRL_ADCI_RDY (1 << 6)                     // Current data ready
#define COM_ESI_CTRL_HVCTRL_ADCI_OFL (1 << 7)                     // Current data overflow

// Data-ready flags of the base module
#define COM_ESI_CTRL_BASE_HK_RDY  (COM_ESI_CTRL_MODULE_HK_RDY) // Housekeeping data ready
#define COM_ESI_CTRL_BASE_HK_OFL  (COM_ESI_CTRL_MODULE_HK_OFL) // Housekeeping data overflow

int _export COM_ESI_CTRL_GetModuleDataReadyFlags (unsigned Address, BYTE & DataReadyFlags); // Get data-ready flags of a module

int _export COM_ESI_CTRL_GetHeatCtrlHousekeeping (bool & Valid, double & Volt3V3, double & TempCPU, double & Volt5V0, double & Volt24V, double & TempPSU);                      // Get heat-controller housekeeping data

int _export COM_ESI_CTRL_GetHVsupplyHousekeeping (unsigned Address, bool & Valid, double & Volt3V3,  double & TempCPU,  double & Volt5V0, double & Volt24Vp, double & Volt22Vn, // Get HV-PSU housekeeping data
/*       */                                                                       double & Volt18Vp, double & Volt18Vn, double & Volt1V5, double & VoltAGnd);

int _export COM_ESI_CTRL_GetBaseHousekeeping (double & Volt3V3, double & TempCPU); // Get housekeeping data of base device

int _export COM_ESI_CTRL_GetHeatCtrlOutputVoltage (double & Voltage); // Get target heat-controller output voltage
int _export COM_ESI_CTRL_GetHeatCtrlHeaterPower   (double & Power);   // Get target heat-controller heater power

int _export COM_ESI_CTRL_SetHVsupplyMeasRanges    (unsigned Address, bool VoltNeg, bool CurrHigh); // Set HV-PSU measurement channels
int _export COM_ESI_CTRL_GetHVsupplyOutputVoltage (unsigned Address, bool & Valid, double & Voltage); // Get HV-PSU output voltage
int _export COM_ESI_CTRL_GetHVsupplyOutputCurrent (unsigned Address, bool & Valid, double & Current); // Get HV-PSU output current
int _export COM_ESI_CTRL_GetHVsupplyPhase         (unsigned Address, bool & Valid, double & Phase  ); // Get HV-PSU phase

int _export COM_ESI_CTRL_GetHVsupplyTargetOutputVoltage (unsigned Address, double & Voltage); // Get HV-PSU target output voltage
int _export COM_ESI_CTRL_SetHVsupplyTargetOutputVoltage (unsigned Address, double   Voltage); // Set HV-PSU target output voltage

int _export COM_ESI_CTRL_GetHVsupplyWidthPWM  (unsigned Address, double &  WidthPWM);                                                                // Get HV-PSU PWM width
int _export COM_ESI_CTRL_GetHVsupplyPeriodPWM (unsigned Address, double & PeriodPWM);                                                                // Get HV-PSU PWM period
int _export COM_ESI_CTRL_GetHVsupplyParamsPWM (unsigned Address, double & Period,  double & Width, double & PhaseGet, double & PhaseSet,             // Get HV-PSU PWM parameters
/*       */                                                      double & VoltSet, double & VoltMeas, bool & ActivationState, BYTE & DataReadyFlags);

int _export COM_ESI_CTRL_GetHeatCtrlHwLimits      (double & MaxVoltage, double & MaxCurrent, double & MaxPower, double & MaxTemperature); // Get heat-controller hardware limits
int _export COM_ESI_CTRL_GetHeatCtrlVoltageLimit  (double & MaxVoltage);                                                                  // Get heat-controller voltage limit
int _export COM_ESI_CTRL_SetHeatCtrlVoltageLimit  (double & MaxVoltage);                                                                  // Set heat-controller voltage limit, return the set value
int _export COM_ESI_CTRL_GetHeatCtrlCurrentLimit  (double & MaxCurrent);                                                                  // Get heat-controller current limit
int _export COM_ESI_CTRL_SetHeatCtrlCurrentLimit  (double & MaxCurrent);                                                                  // Set heat-controller current limit, return the set value
int _export COM_ESI_CTRL_GetHeatCtrlPowerLimit    (double & MaxPower  );                                                                  // Get heat-controller power limit
int _export COM_ESI_CTRL_SetHeatCtrlPowerLimit    (double & MaxPower  );                                                                  // Set heat-controller power limit, return the set value

int _export COM_ESI_CTRL_GetHeatCtrlHeaterTemperature (double & HeaterTemp); // Get target heat-controller heater temperature
int _export COM_ESI_CTRL_SetHeatCtrlHeaterTemperature (double & HeaterTemp); // Set target heat-controller heater temperature, return the set value

int _export COM_ESI_CTRL_GetHeatCtrlMonitoring (bool & Valid, double & VoltOut, double & VoltMon, double & CurrMon, double & TempMon); // Get heat-controller monitoring data

#define COM_ESI_CTRL_HTCTRL_IS_ILOCK1_CURR  (1 << 0)         // Interlock 1, current state
#define COM_ESI_CTRL_HTCTRL_IS_ILOCK2_CURR  (1 << 1)         // Interlock 2, current state
#define COM_ESI_CTRL_HTCTRL_IS_ILOCK1_LAST  (1 << 2)         // Interlock 1, last state
#define COM_ESI_CTRL_HTCTRL_IS_ILOCK2_LAST  (1 << 3)         // Interlock 2, last state
#define COM_ESI_CTRL_HTCTRL_IS_ILOCK1_ACT   (COM_ESI_CTRL_HTCTRL_IS_ILOCK1_CURR | COM_ESI_CTRL_HTCTRL_IS_ILOCK1_LAST)
#define COM_ESI_CTRL_HTCTRL_IS_ILOCK2_ACT   (COM_ESI_CTRL_HTCTRL_IS_ILOCK2_CURR | COM_ESI_CTRL_HTCTRL_IS_ILOCK2_LAST)
#define COM_ESI_CTRL_HTCTRL_IS_ILOCK_ACT    (COM_ESI_CTRL_HTCTRL_IS_ILOCK1_ACT  | COM_ESI_CTRL_HTCTRL_IS_ILOCK2_ACT )
int _export COM_ESI_CTRL_GetHeatCtrlIlockState (BYTE & IlockState); // Get interlock state of heat controller

// Module state bits:
#define COM_ESI_CTRL_MS_ACTIVE   (1<<0xF) // Device is active, i.e. output voltages can be nonzero
int _export COM_ESI_CTRL_GetModuleState (unsigned Address, WORD & ModuleState); // Get module state

int _export COM_ESI_CTRL_SetModuleActivationState (unsigned Address, bool   ActivationState); // Set module activation state
int _export COM_ESI_CTRL_GetModuleActivationState (unsigned Address, bool & ActivationState); // Get module activation state


/**********************
// Special functions //
**********************/

int _export COM_ESI_CTRL_GetCompleteState (BYTE & DataFlags, BYTE & DeviceState, BYTE & VoltageState, BYTE & TemperatureState, BYTE & FanState, WORD & InterlockState, WORD & State,  // Get complete device state
/**/                                       BYTE ModuleDataFlags [COM_ESI_CTRL_MODULE_NUM+1], WORD ModuleState [COM_ESI_CTRL_MODULE_NUM+1], BYTE & HeatCtrlInterlockState);


/*******************
// Error handling //
*******************/

int          _export COM_ESI_CTRL_GetInterfaceState();                     // Get software interface state
#ifdef _MSC_VER
_export const char * COM_ESI_CTRL_GetErrorMessage();                       // Get error message corresponding to the software interface state
_export const char * COM_ESI_CTRL_GetIOErrorMessage();                     // Get error message corresponding to the serial port interface state
#else
const char * _export COM_ESI_CTRL_GetErrorMessage();                       // Get error message corresponding to the software interface state
const char * _export COM_ESI_CTRL_GetIOErrorMessage();                     // Get error message corresponding to the serial port interface state
#endif
int          _export COM_ESI_CTRL_GetIOState          (int   & IOState);   // Get and clear last serial port interface state
#ifdef _MSC_VER
_export const char * COM_ESI_CTRL_GetIOStateMessage   (int     IOState);   // Get the error message corresponding to the specified interface state
#else
const char * _export COM_ESI_CTRL_GetIOStateMessage   (int     IOState);   // Get error message corresponding to the specified interface state
#endif
int          _export COM_ESI_CTRL_GetCommError        (DWORD & CommError); // Get and clear last communication-port error
#ifdef _MSC_VER
_export const char * COM_ESI_CTRL_GetCommErrorMessage (DWORD   CommError); // Get error message corresponding to the communication port error
#else
const char * _export COM_ESI_CTRL_GetCommErrorMessage (DWORD   CommError); // Get error message corresponding to the communication port error
#endif

#ifdef __cplusplus
}
#endif

#endif//__COM_ESI_CTRL_H__
