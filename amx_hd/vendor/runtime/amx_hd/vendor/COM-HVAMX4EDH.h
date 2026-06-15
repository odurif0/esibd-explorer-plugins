/******************************************************************************
//                                                                           //
//  Project: Software Interface for HV-AMX-CTRL-4EDH Devices                 //
//                                                                           //
//  CGC Instruments, (c) 2009-2025, Version 1.20. All rights reserved.       //
//                                                                           //
//  Definition for COM-HVAMX4EDH DLL Routines                                //
//                                                                           //
******************************************************************************/

#ifndef __COM_HVAMX4EDH_H__
#define __COM_HVAMX4EDH_H__

#ifdef __cplusplus
extern "C" {
#endif

#define COM_HVAMX4EDH_MAX_STREAM 16 // Maximum StreamNumber
/*
	The listed routines accept the number of the stream StreamNumber in the range 0..COM_HVAMX4EDH_MAX_STREAM-1,
	thus maximum COM_HVAMX4EDH_MAX_STREAM independent communication streams (channels) can be defined

	Each communication stream must be opened before the first usage. The Open routine assigns a specified COMx port to the stream.
	The ComNumber specifies the number of the port - 1 stands for COM1, 2 for COM2, etc.
	The maximum allowable ComNumber is 255 corresponding to COM255.

	If necessary, the stream may be closed and assigned by the Open routine to a new COMx port.
	The streams are closed automatically at the end of the program, the main program does not need to take care about it.

	The return values of the routines are the following defines COM_HVAMX4EDH_ERR_XXX
		 0: routine completed without any error (COM_HVAMX4EDH_NO_ERR)
		<0: errors COM_HVAMX4EDH_ERR_XXX

	The last error code can be obtained by calling COM_HVAMX4EDH_State.
	The routine COM_HVAMX4EDH_ErrorMessage returns a pointer to a zero-terminated character string.

	If a communication error occurred, the error code can be red by COM_HVAMX4EDH_IO_State,
	The routine COM_HVAMX4EDH_IO_ErrorMessage provides the corresponding error message.

	The last communication-port error code returned by the operating system can be obtained by COM_HVAMX4EDH_GetCommError.
	The routine COM_HVAMX4EDH_GetCommErrorMessage provides the corresponding error message.
*/

/****************
// Error codes //
****************/

#define COM_HVAMX4EDH_NO_ERR                 0 // No error occurred

#define COM_HVAMX4EDH_ERR_STREAM_RANGE      -1 // StreamNumber out of range
#define COM_HVAMX4EDH_ERR_OPEN              -2 // Error opening the port
#define COM_HVAMX4EDH_ERR_CLOSE             -3 // Error closing the port
#define COM_HVAMX4EDH_ERR_PURGE             -4 // Error purging the port
#define COM_HVAMX4EDH_ERR_CONTROL           -5 // Error setting the port control lines
#define COM_HVAMX4EDH_ERR_STATUS            -6 // Error reading the port status lines
#define COM_HVAMX4EDH_ERR_COMMAND_SEND      -7 // Error sending command
#define COM_HVAMX4EDH_ERR_DATA_SEND         -8 // Error sending data
#define COM_HVAMX4EDH_ERR_TERM_SEND         -9 // Error sending termination character
#define COM_HVAMX4EDH_ERR_COMMAND_RECEIVE  -10 // Error receiving command
#define COM_HVAMX4EDH_ERR_DATA_RECEIVE     -11 // Error receiving data
#define COM_HVAMX4EDH_ERR_TERM_RECEIVE     -12 // Error receiving termination character
#define COM_HVAMX4EDH_ERR_COMMAND_WRONG    -13 // Wrong command received
#define COM_HVAMX4EDH_ERR_ARGUMENT_WRONG   -14 // Wrong argument received
#define COM_HVAMX4EDH_ERR_ARGUMENT         -15 // Wrong argument passed to the function
#define COM_HVAMX4EDH_ERR_RATE             -16 // Error setting the baud rate

#define COM_HVAMX4EDH_ERR_NOT_CONNECTED   -100 // Device not connected
#define COM_HVAMX4EDH_ERR_NOT_READY       -101 // Device not ready
#define COM_HVAMX4EDH_ERR_READY           -102 // Device state could not be set to not ready

#define COM_HVAMX4EDH_ERR_CONFIG          -200 // Configuration could not be loaded or saved, it may be invalid
#define COM_HVAMX4EDH_ERR_CONFIG_EMPTY    -201 // Configuration is empty

#define COM_HVAMX4EDH_ERR_DEBUG_OPEN      -400 // Error opening the file for debugging output
#define COM_HVAMX4EDH_ERR_DEBUG_CLOSE     -401 // Error closing the file for debugging output

WORD _export COM_HVAMX4EDH_GetSWVersion(); // Get the software version


/******************
// Communication //
******************/

int _export COM_HVAMX4EDH_Open  (WORD StreamNumber, WORD ComNumber); // Open the stream
int _export COM_HVAMX4EDH_Close (WORD StreamNumber);                 // Close the stream

int _export COM_HVAMX4EDH_SetBaudRate (WORD StreamNumber, unsigned & BaudRate); // Set the baud rate and return the set value

int _export COM_HVAMX4EDH_Purge          (WORD StreamNumber);               // Clear data buffers for the stream
int _export COM_HVAMX4EDH_DevicePurge    (WORD StreamNumber, BOOL & Empty); // Clear output data buffer of the device
int _export COM_HVAMX4EDH_GetBufferState (WORD StreamNumber, BOOL & Empty); // Return true if the input data buffer of the device is empty


/*******************
// Device control //
*******************/

// Bits of main device state:
#define COM_HVAMX4EDH_STATE_STANDBY        0x0000  // Device stand-by
#define COM_HVAMX4EDH_STATE_ON             0x0001  // Device active
#define COM_HVAMX4EDH_STATE_ERROR          0x8000  // Error bit
#define COM_HVAMX4EDH_STATE_ERR_VSUP       0x8001  // Voltage failure
#define COM_HVAMX4EDH_STATE_ERR_TEMP_LOW   0x8002  // Low-temperature failure
#define COM_HVAMX4EDH_STATE_ERR_TEMP_HIGH  0x8003  // High-temperature failure
//int _export COM_HVAMX4EDH_GetMainState (WORD StreamNumber, WORD & State); // Get the main device status
// Bits of main device state:
#define COM_HVAMX4EDH_DEVST_OK                  0      // No error detected
#define COM_HVAMX4EDH_DEVST_VCPU_FAIL   (1UL << 0x00)  // CPU supply voltage failed
#define COM_HVAMX4EDH_DEVST_VSUP_FAIL   (1UL << 0x01)  // Fan supply voltage failed
#define COM_HVAMX4EDH_DEVST_PLL0_FAIL   (1UL << 0x04)  // PLL #0 failed
#define COM_HVAMX4EDH_DEVST_PLL1_FAIL   (1UL << 0x05)  // PLL #1 failed
#define COM_HVAMX4EDH_DEVST_PLL2_FAIL   (1UL << 0x06)  // PLL #2 failed
#define COM_HVAMX4EDH_DEVST_PLL3_FAIL   (1UL << 0x07)  // PLL #3 failed
#define COM_HVAMX4EDH_DEVST_FAN1_FAIL   (1UL << 0x08)  // Fan #1 failed
#define COM_HVAMX4EDH_DEVST_FAN2_FAIL   (1UL << 0x09)  // Fan #2 failed
#define COM_HVAMX4EDH_DEVST_FAN3_FAIL   (1UL << 0x0A)  // Fan #3 failed
#define COM_HVAMX4EDH_DEVST_FPGA_DIS    (1UL << 0x0F)  // FPGA disabled
// Bits of temperature state:
#define COM_HVAMX4EDH_TMPST_SEN1_HIGH   (1UL << 0x00)  // Temperature sensor #1 hot
#define COM_HVAMX4EDH_TMPST_SEN2_HIGH   (1UL << 0x01)  // Temperature sensor #2 hot
#define COM_HVAMX4EDH_TMPST_SEN3_HIGH   (1UL << 0x02)  // Temperature sensor #3 hot
#define COM_HVAMX4EDH_TMPST_SEN1_LOW    (1UL << 0x08)  // Temperature sensor #1 cold
#define COM_HVAMX4EDH_TMPST_SEN2_LOW    (1UL << 0x09)  // Temperature sensor #2 cold
#define COM_HVAMX4EDH_TMPST_SEN3_LOW    (1UL << 0x0A)  // Temperature sensor #3 cold
//int _export COM_HVAMX4EDH_GetDeviceState (WORD StreamNumber, DWORD & DeviceState); // Get the device status
int _export COM_HVAMX4EDH_GetDeviceState (WORD StreamNumber, WORD & MainState, WORD & DeviceState, WORD & TemperatureState); // Get the device status

int _export COM_HVAMX4EDH_GetHousekeeping (WORD StreamNumber, double & Volt12V, double & VoltFans, double & Volt5V0, double & Volt3V3, double & Volt3V3p, double & Volt2V5p, double & VoltVc, double & TempCPU); // Get the housekeeping data

#define COM_HVAMX4EDH_SEN_COUNT       3  // Number of implemented sensors
int _export COM_HVAMX4EDH_GetSensorData (WORD StreamNumber, double Temperature [COM_HVAMX4EDH_SEN_COUNT]); // Get sensor data

#define COM_HVAMX4EDH_FAN_COUNT       3  // Number of implemented fans
#define COM_HVAMX4EDH_FAN_PWM_MAX  1000  // Maximum PWM value (100%)
int _export COM_HVAMX4EDH_GetFanSpeed (WORD PortNumber, WORD FanNumber, WORD & FanRPM);                                                  // Get fan speed
int _export COM_HVAMX4EDH_GetFanData  (WORD StreamNumber, BOOL Enabled [COM_HVAMX4EDH_FAN_COUNT], BOOL Failed [COM_HVAMX4EDH_FAN_COUNT],
	WORD SetRPM [COM_HVAMX4EDH_FAN_COUNT], WORD MeasuredRPM [COM_HVAMX4EDH_FAN_COUNT], WORD PWM [COM_HVAMX4EDH_FAN_COUNT]);                // Get fan data

int _export COM_HVAMX4EDH_GetLEDData (WORD StreamNumber, BOOL & Red, BOOL & Green, BOOL & Blue);                                         // Get LED data


/**********************
// Pulser management //
**********************/

// Clock System
#define COM_HVAMX4EDH_CLOCK_SRC_NUM        0x10 // Clock source number
#define COM_HVAMX4EDH_CLOCK_SRC_MASK       0x0F // Clock source selection mask
#define COM_HVAMX4EDH_CLOCK_SRC_INVERT   (1<<7) // Clock source negated
#define COM_HVAMX4EDH_CLOCK_SOURCE_MASK  (COM_HVAMX4EDH_CLOCK_SRC_MASK | COM_HVAMX4EDH_CLOCK_SRC_INVERT)
#define COM_HVAMX4EDH_CLOCK_NUM               4
int _export COM_HVAMX4EDH_GetClockCount  (WORD StreamNumber, unsigned & Count);              // Get number of clocks, should return COM_HVAMX4EDH_CLOCK_NUM
int _export COM_HVAMX4EDH_GetClockSource (WORD StreamNumber, unsigned Clock, BYTE & Source); // Get signal source of specified clock
int _export COM_HVAMX4EDH_SetClockSource (WORD StreamNumber, unsigned Clock, BYTE   Source); // Set signal source of specified clock

#define COM_HVAMX4EDH_PLL_SRC_NUM        0x10 // PLL source number
#define COM_HVAMX4EDH_PLL_SRC_MASK       0x0F // PLL source selection mask
#define COM_HVAMX4EDH_PLL_SRC_INVERT   (1<<7) // PLL source negated
#define COM_HVAMX4EDH_PLL_SOURCE_MASK  (COM_HVAMX4EDH_PLL_SRC_MASK | COM_HVAMX4EDH_PLL_SRC_INVERT)
#define COM_HVAMX4EDH_PLL_NUM     4
int _export COM_HVAMX4EDH_GetPllCount     (WORD StreamNumber, unsigned & Count);               // Get number of PLLs, should return COM_HVAMX4EDH_PllNUM
int _export COM_HVAMX4EDH_GetPllSource    (WORD StreamNumber, unsigned PLL, BYTE &    Source); // Get signal source of specified PLL
int _export COM_HVAMX4EDH_SetPllSource    (WORD StreamNumber, unsigned PLL, BYTE      Source); // Set signal source of specified PLL
#define COM_HVAMX4EDH_PLL_REF_DIV_MIN     1
#define COM_HVAMX4EDH_PLL_REF_DIV_MAX  4095
int _export COM_HVAMX4EDH_GetPllRefDiv    (WORD StreamNumber, unsigned PLL, WORD &   Divider); // Get reference divider of specified PLL
int _export COM_HVAMX4EDH_SetPllRefDiv    (WORD StreamNumber, unsigned PLL, WORD     Divider); // Set reference divider of specified PLL
#define COM_HVAMX4EDH_PLL_FB_DIV_MIN      4
#define COM_HVAMX4EDH_PLL_FB_DIV_MAX  16383
// not allowed values:
#define COM_HVAMX4EDH_PLL_FB_DIV_NA1      6
#define COM_HVAMX4EDH_PLL_FB_DIV_NA2      7
#define COM_HVAMX4EDH_PLL_FB_DIV_NA3     11
int _export COM_HVAMX4EDH_GetPllFdbkDiv   (WORD StreamNumber, unsigned PLL, WORD &   Divider); // Get feedback divider of specified PLL
int _export COM_HVAMX4EDH_SetPllFdbkDiv   (WORD StreamNumber, unsigned PLL, WORD     Divider); // Set feedback divider of specified PLL
#define COM_HVAMX4EDH_PLL_POST_DIV1_MIN   1
#define COM_HVAMX4EDH_PLL_POST_DIV1_MAX  12
int _export COM_HVAMX4EDH_GetPllPostDiv1  (WORD StreamNumber, unsigned PLL, BYTE &   Divider); // Get post divider #1 of specified PLL
int _export COM_HVAMX4EDH_SetPllPostDiv1  (WORD StreamNumber, unsigned PLL, BYTE     Divider); // Set post divider #1 of specified PLL
#define COM_HVAMX4EDH_PLL_POST_DIV2_MIN   1
#define COM_HVAMX4EDH_PLL_POST_DIV2_MAX  12
int _export COM_HVAMX4EDH_GetPllPostDiv2  (WORD StreamNumber, unsigned PLL, BYTE &   Divider); // Get post divider #2 of specified PLL
int _export COM_HVAMX4EDH_SetPllPostDiv2  (WORD StreamNumber, unsigned PLL, BYTE     Divider); // Set post divider #2 of specified PLL
#define COM_HVAMX4EDH_PLL_POST_DIV3_1     0 // [00] 1
#define COM_HVAMX4EDH_PLL_POST_DIV3_2     1 // [01] 2
#define COM_HVAMX4EDH_PLL_POST_DIV3_4     2 // [10] 4
#define COM_HVAMX4EDH_PLL_POST_DIV3_8     3 // [11] 8
#define COM_HVAMX4EDH_PLL_POST_DIV3_MAX   4
int _export COM_HVAMX4EDH_GetPllPostDiv3  (WORD StreamNumber, unsigned PLL, BYTE &   Divider); // Get post divider #3 of specified PLL
int _export COM_HVAMX4EDH_SetPllPostDiv3  (WORD StreamNumber, unsigned PLL, BYTE     Divider); // Set post divider #3 of specified PLL
int _export COM_HVAMX4EDH_GetPllPowerDown (WORD StreamNumber, unsigned PLL, bool & PowerDown); // Get power-down mode of specified PLL
int _export COM_HVAMX4EDH_SetPllPowerDown (WORD StreamNumber, unsigned PLL, bool   PowerDown); // Set power-down mode of specified PLL
// PLL charge-pump current:
#define COM_HVAMX4EDH_PLL_CP_2u0    0  //  2.0uA
#define COM_HVAMX4EDH_PLL_CP_4u5    1  //  4.5uA
#define COM_HVAMX4EDH_PLL_CP_11u0   2  // 11.0uA
#define COM_HVAMX4EDH_PLL_CP_22u5   3  // 22.5uA
#define COM_HVAMX4EDH_PLL_CP_MAX    4
#define COM_HVAMX4EDH_PLL_CP_ERR  (-1) // Invalid response
int _export COM_HVAMX4EDH_GetPllCpCurrent (WORD StreamNumber, unsigned PLL, BYTE & CpCurrent); // Get charge-pump current of specified PLL
int _export COM_HVAMX4EDH_SetPllCpCurrent (WORD StreamNumber, unsigned PLL, BYTE   CpCurrent); // Set charge-pump current of specified PLL
// PLL loop-filter resistor select:
#define COM_HVAMX4EDH_PLL_LR_400K  0  // 400kOhm
#define COM_HVAMX4EDH_PLL_LR_133K  1  // 133kOhm
#define COM_HVAMX4EDH_PLL_LR_30K   2  //  30kOhm
#define COM_HVAMX4EDH_PLL_LR_12K   3  //  12kOhm
#define COM_HVAMX4EDH_PLL_LR_MAX   4
#define COM_HVAMX4EDH_PLL_LR_ERR (-1) // Invalid response
int _export COM_HVAMX4EDH_GetPllLfResistor (WORD StreamNumber, unsigned PLL, int & LfResistor); // Get loop-filter resistor of specified PLL
int _export COM_HVAMX4EDH_SetPllLfResistor (WORD StreamNumber, unsigned PLL, int   LfResistor); // Set loop-filter resistor of specified PLL
// PLL loop-filter capacitor select:
#define COM_HVAMX4EDH_PLL_LC_185pF  0  // 185pF
#define COM_HVAMX4EDH_PLL_LC_500pF  1  // 500pF
#define COM_HVAMX4EDH_PLL_LC_MAX    2
#define COM_HVAMX4EDH_PLL_LC_ERR  (-1) // Invalid response
int _export COM_HVAMX4EDH_GetPllLfCapacitor (WORD StreamNumber, unsigned PLL, int & LfCapacitor); // Get loop-filter capacitor of specified PLL
int _export COM_HVAMX4EDH_SetPllLfCapacitor (WORD StreamNumber, unsigned PLL, int   LfCapacitor); // Set loop-filter capacitor of specified PLL

#define COM_HVAMX4EDH_DIV_CLOCK   (200E6)
#define COM_HVAMX4EDH_DIV_OFFSET  2
#define COM_HVAMX4EDH_DIV_NUM     4
int _export COM_HVAMX4EDH_GetDividerCount  (WORD StreamNumber, unsigned & Count);                // Get number of dividers, should return COM_HVAMX4EDH_DIV_NUM
int _export COM_HVAMX4EDH_GetDividerPeriod (WORD StreamNumber, unsigned Divider, BYTE & Period); // Get period = dividing factor of specified divider
int _export COM_HVAMX4EDH_SetDividerPeriod (WORD StreamNumber, unsigned Divider, BYTE   Period); // Set period = dividing factor of specified divider

#define COM_HVAMX4EDH_CNT_CLOCK   (200E6)
#define COM_HVAMX4EDH_CNT_PER_MIN    2
#define COM_HVAMX4EDH_CNT_PER_MAX   20
#define COM_HVAMX4EDH_CNT_NUM        4
int _export COM_HVAMX4EDH_GetCounterCount  (WORD StreamNumber, unsigned & Count);                // Get number of counters, should return COM_HVAMX4EDH_CNT_NUM
int _export COM_HVAMX4EDH_GetCounterPeriod (WORD StreamNumber, unsigned Counter, BYTE & Period); // Get period = dividing factor of specified counter
int _export COM_HVAMX4EDH_SetCounterPeriod (WORD StreamNumber, unsigned Counter, BYTE   Period); // Set period = dividing factor of specified counter


// Oscillators
#define COM_HVAMX4EDH_DEF_CLOCK   (100E6)
#define COM_HVAMX4EDH_OSC_OFFSET     2
int _export COM_HVAMX4EDH_GetOscillatorCount  (WORD StreamNumber, unsigned & OscillatorCount);          // Get number of implemented oscillators
int _export COM_HVAMX4EDH_GetOscillatorPeriod (WORD StreamNumber, unsigned Oscillator, DWORD & Period); // Get oscillator period
int _export COM_HVAMX4EDH_SetOscillatorPeriod (WORD StreamNumber, unsigned Oscillator, DWORD   Period); // Set oscillator period


// Timers
#define COM_HVAMX4EDH_TIMER_DELAY_OFFSET  3
int _export COM_HVAMX4EDH_GetTimerCount (WORD StreamNumber, unsigned & TimerCount);         // Get number of implemented timers
int _export COM_HVAMX4EDH_GetTimerDelay (WORD StreamNumber, unsigned Timer, DWORD & Delay); // Get pulse delay of specified timer
int _export COM_HVAMX4EDH_SetTimerDelay (WORD StreamNumber, unsigned Timer, DWORD   Delay); // Set pulse delay of specified timer
#define COM_HVAMX4EDH_TIMER_WIDTH_OFFSET  2
int _export COM_HVAMX4EDH_GetTimerWidth (WORD StreamNumber, unsigned Timer, DWORD & Width); // Get pulse width of specified timer
int _export COM_HVAMX4EDH_SetTimerWidth (WORD StreamNumber, unsigned Timer, DWORD   Width); // Set pulse width of specified timer

int _export COM_HVAMX4EDH_GetTimerTriggerSource (WORD StreamNumber, unsigned Timer, BYTE & TriggerSource); // Get trigger source of specified timer
int _export COM_HVAMX4EDH_SetTimerTriggerSource (WORD StreamNumber, unsigned Timer, BYTE   TriggerSource); // Set trigger source of specified timer

#define COM_HVAMX4EDH_TIMER_MAX_BURST   (1UL << 24)
#define COM_HVAMX4EDH_TIMER_MASK_BURST  (COM_HVAMX4EDH_TIMER_MAX_BURST - 1)
int _export COM_HVAMX4EDH_GetTimerBurst      (WORD StreamNumber, unsigned Timer, DWORD & Burst);      // Get burst size of specified timer
int _export COM_HVAMX4EDH_SetTimerBurst      (WORD StreamNumber, unsigned Timer, DWORD   Burst);      // Set burst size of specified timer
int _export COM_HVAMX4EDH_GetTimerStopSource (WORD StreamNumber, unsigned Timer,  BYTE & StopSource); // Get stop source of specified timer
int _export COM_HVAMX4EDH_SetTimerStopSource (WORD StreamNumber, unsigned Timer,  BYTE   StopSource); // Set stop source of specified timer


// Mapping Engines
#define COM_HVAMX4EDH_MAPPING_INPUT_NUM        4  // Number of mapping-engine inputs, i.e. bit size of the mapping engine
#define COM_HVAMX4EDH_MAPPING_STATE_NUM        7  // State number of the mapping engine
#define COM_HVAMX4EDH_MAPPING_STATE_BITS    0x0F  // State bits of the mapping engine
int _export COM_HVAMX4EDH_GetMappingEngineInputSource (WORD StreamNumber, unsigned MappingEngine,                BYTE       InputSource [COM_HVAMX4EDH_MAPPING_INPUT_NUM]); // Get input signals of the specified mapping engine
int _export COM_HVAMX4EDH_SetMappingEngineInputSource (WORD StreamNumber, unsigned MappingEngine,                BYTE const InputSource [COM_HVAMX4EDH_MAPPING_INPUT_NUM]); // Set input signals of the specified mapping engine
int _export COM_HVAMX4EDH_GetMappingEngineOutputValue (WORD StreamNumber, unsigned MappingEngine, bool & Enable, BYTE       OutputValue [COM_HVAMX4EDH_MAPPING_STATE_NUM]); // Get the enable bit & the output values of the specified mapping engine
int _export COM_HVAMX4EDH_SetMappingEngineOutputValue (WORD StreamNumber, unsigned MappingEngine, bool   Enable, BYTE const OutputValue [COM_HVAMX4EDH_MAPPING_STATE_NUM]); // Set the enable bit & the output values of the specified mapping engine


// Switch control
#define COM_HVAMX4EDH_SWITCH_NUM  4  // Maximum number of switch channels, use COM_HVAMX4EDH_GetHwType to obtain the real number
int _export COM_HVAMX4EDH_GetSwitchTriggerSource (WORD StreamNumber, unsigned Switch, BYTE & TriggerSource); // Get source of specified switch trigger input
int _export COM_HVAMX4EDH_SetSwitchTriggerSource (WORD StreamNumber, unsigned Switch, BYTE   TriggerSource); // Set source of specified switch trigger input
int _export COM_HVAMX4EDH_GetSwitchEnableSource  (WORD StreamNumber, unsigned Switch, BYTE & EnableSource);  // Get source of specified switch enable input
int _export COM_HVAMX4EDH_SetSwitchEnableSource  (WORD StreamNumber, unsigned Switch, BYTE   EnableSource);  // Set source of specified switch enable input
/*
#define COM_HVAMX4EDH_SWITCH_DELAY_SIZE   4                                                                               // Bit size of the switch delay
#define COM_HVAMX4EDH_SWITCH_DELAY_MAX   (1UL << COM_HVAMX4EDH_SWITCH_DELAY_SIZE)                                         // Maximum switch delay
#define COM_HVAMX4EDH_SWITCH_DELAY_MASK  (COM_HVAMX4EDH_SWITCH_DELAY_MAX - 1)                                             // Switch-delay bit mask
int _export COM_HVAMX4EDH_GetSwitchTriggerDelay (WORD StreamNumber, unsigned Switch, BYTE & RiseDelay, BYTE & FallDelay); // Get delays of specified switch trigger
int _export COM_HVAMX4EDH_SetSwitchTriggerDelay (WORD StreamNumber, unsigned Switch, BYTE   RiseDelay, BYTE   FallDelay); // Set delays of specified switch trigger
int _export COM_HVAMX4EDH_GetSwitchEnableDelay  (WORD StreamNumber, unsigned Switch, BYTE & Delay);                       // Get delay of specified switch enable
int _export COM_HVAMX4EDH_SetSwitchEnableDelay  (WORD StreamNumber, unsigned Switch, BYTE   Delay);                       // Set delay of specified switch enable
*/
#define COM_HVAMX4EDH_SWITCH_DELAY_SIZE   4                                                                        // Bit size of the switch delay
#define COM_HVAMX4EDH_SWITCH_DELAY_MAX   (1UL << COM_HVAMX4EDH_SWITCH_DELAY_SIZE)                                  // Maximum switch delay
#define COM_HVAMX4EDH_SWITCH_DELAY_MASK  (COM_HVAMX4EDH_SWITCH_DELAY_MAX - 1)                                      // Switch-delay bit mask
#define COM_HVAMX4EDH_SWITCH_DELAY_SCALE (5E-9)
int _export COM_HVAMX4EDH_GetSwitchDelay (WORD StreamNumber, unsigned Switch, BYTE & RiseDelay, BYTE & FallDelay); // Get coarse delays of specified switch trigger
int _export COM_HVAMX4EDH_SetSwitchDelay (WORD StreamNumber, unsigned Switch, BYTE   RiseDelay, BYTE   FallDelay); // Set coarse delays of specified switch trigger
#define COM_HVAMX4EDH_SWITCH_DELAY_FINE_MAX   (0x200)
#define COM_HVAMX4EDH_SWITCH_DELAY_FINE_SCALE (11E-12)
int _export COM_HVAMX4EDH_GetSwitchRiseDelayFine (WORD StreamNumber, unsigned Switch, WORD & Delay); // Get fine rise delay of specified switch trigger
int _export COM_HVAMX4EDH_GetSwitchFallDelayFine (WORD StreamNumber, unsigned Switch, WORD & Delay); // Get fine fall delay of specified switch trigger
int _export COM_HVAMX4EDH_SetSwitchRiseDelayFine (WORD StreamNumber, unsigned Switch, WORD   Delay); // Set fine rise delay of specified switch trigger
int _export COM_HVAMX4EDH_SetSwitchFallDelayFine (WORD StreamNumber, unsigned Switch, WORD   Delay); // Set fine fall delay of specified switch trigger
/*
#define COM_HVAMX4EDH_MAPPING_SIZE   COM_HVAMX4EDH_SWITCH_NUM                                                    // Bit size of the switch mapping
#define COM_HVAMX4EDH_MAPPING_MAX   (1UL << COM_HVAMX4EDH_MAPPING_SIZE)                                          // Maximum switch mapping
#define COM_HVAMX4EDH_MAPPING_MASK  (COM_HVAMX4EDH_MAPPING_MAX - 1)                                              // Switch mapping mask
#define COM_HVAMX4EDH_MAPPING_NUM   (COM_HVAMX4EDH_SWITCH_NUM + 1)                                               // Number of mappings
int _export COM_HVAMX4EDH_GetSwitchTriggerMapping       (WORD StreamNumber, unsigned MappingNo, BYTE & Mapping); // Get specified switch trigger mapping
int _export COM_HVAMX4EDH_SetSwitchTriggerMapping       (WORD StreamNumber, unsigned MappingNo, BYTE   Mapping); // Set specified switch trigger mapping
int _export COM_HVAMX4EDH_GetSwitchEnableMapping        (WORD StreamNumber, unsigned MappingNo, BYTE & Mapping); // Get specified switch enable mapping
int _export COM_HVAMX4EDH_SetSwitchEnableMapping        (WORD StreamNumber, unsigned MappingNo, BYTE   Mapping); // Set specified switch enable mapping
int _export COM_HVAMX4EDH_GetSwitchTriggerMappingEnable (WORD StreamNumber, bool & Enable);                      // Get the switch trigger mapping enable bit
int _export COM_HVAMX4EDH_SetSwitchTriggerMappingEnable (WORD StreamNumber, bool   Enable);                      // Set the switch trigger mapping enable bit
int _export COM_HVAMX4EDH_GetSwitchEnableMappingEnable  (WORD StreamNumber, bool & Enable);                      // Get the switch enable mapping enable bit
int _export COM_HVAMX4EDH_SetSwitchEnableMappingEnable  (WORD StreamNumber, bool   Enable);                      // Set the switch enable mapping enable bit
*/

// Digital I/Os
#define COM_HVAMX4EDH_DIO_CFG_IN       0  // Digital I/O configuration: input
#define COM_HVAMX4EDH_DIO_CFG_OUT      1  // Digital I/O configuration: output
#define COM_HVAMX4EDH_DIO_CFG_IN_TERM  2  // Digital I/O configuration: input with termination
//#define COM_HVAMX4EDH_DIO_CFGS         3  // Number of digital I/O configurations
//#define COM_HVAMX4EDH_DIO_CFG_MASK  0x03  // Digital I/O configuration mask
#define COM_HVAMX4EDH_DIO_NUM  8          // Number of digital I/Os
int _export COM_HVAMX4EDH_GetDigitalIOConfig       (WORD StreamNumber, unsigned DigitalIO, BYTE & Config);       // Get configuration of specified digital I/O
int _export COM_HVAMX4EDH_SetDigitalIOConfig       (WORD StreamNumber, unsigned DigitalIO, BYTE   Config);       // Set configuration of specified digital I/O
#define COM_HVAMX4EDH_DIO_SRC_NUM        0x50 // Digital I/O source number
#define COM_HVAMX4EDH_DIO_SRC_MASK       0x7F // Digital I/O source selection mask
#define COM_HVAMX4EDH_DIO_SRC_INVERT   (1<<7) // Digital I/O source negated
#define COM_HVAMX4EDH_DIO_SOURCE_MASK  (COM_HVAMX4EDH_DIO_SRC_MASK | COM_HVAMX4EDH_DIO_SRC_INVERT)
int _export COM_HVAMX4EDH_GetDigitalIOSignalSource (WORD StreamNumber, unsigned DigitalIO, BYTE & SignalSource); // Get signal source of specified digital I/O
int _export COM_HVAMX4EDH_SetDigitalIOSignalSource (WORD StreamNumber, unsigned DigitalIO, BYTE   SignalSource); // Set signal source of specified digital I/O


// Signal states
#define COM_HVAMX4EDH_SIGNAL_NUM        0x40 // Signal number
#define COM_HVAMX4EDH_SIGNAL_MASK       0x3F // Signal selection mask
#define COM_HVAMX4EDH_SIGNAL_INVERT   (1<<7) // Level negated
#define COM_HVAMX4EDH_SOURCE_MASK     (COM_HVAMX4EDH_SIGNAL_MASK | COM_HVAMX4EDH_SIGNAL_INVERT)
int _export COM_HVAMX4EDH_GetSignalValues (WORD StreamNumber, bool SignalValues [COM_HVAMX4EDH_SIGNAL_NUM]); // Get signal states
#define COM_HVAMX4EDH_SIGNAL_NUM_BYTE ((COM_HVAMX4EDH_SIGNAL_NUM + 7) / 8)                                   // Signal byte number
int _export COM_HVAMX4EDH_GetSignals      (WORD StreamNumber, BYTE Signals [COM_HVAMX4EDH_SIGNAL_NUM_BYTE]); // Get signal states
/*
// State & configuration bits:
#define COM_HVAMX4EDH_ENB         (1UL <<  0) // Enables the switches, if COM_HVAMX4EDH_PREVENT_DIS=0, 0 forces CLRn=0 => resets the oscillator and the pulse generators and stops the PSUs
#define COM_HVAMX4EDH_ENB_OSC     (1UL <<  1) // 1 enables the oscillator
#define COM_HVAMX4EDH_ENB_PULSER  (1UL <<  2) // 1 enables the pulse generators
#define COM_HVAMX4EDH_SW_TRIG     (1UL <<  3) // Software trigger
#define COM_HVAMX4EDH_SW_PULSE    (1UL <<  4) // 1 creates a 1-CLK wide pulse at software trigger
#define COM_HVAMX4EDH_PREVENT_DIS (1UL <<  5) // Disable the CLRn=0 => 1 prevents CLRn=0 when ST_ENABLE=0 => only the switches are disabled, not their PSUs
#define COM_HVAMX4EDH_DIS_DITHER  (1UL <<  6) // 1 disable the dithering of the internal switching regulators
#define COM_HVAMX4EDH_NC          (1UL <<  7) // Bit not used
// COM_HVAMX4EDH_GetState bits:
#define COM_HVAMX4EDH_ENABLE      (1UL <<  8) // Master enable, 0 forces CLRn=0 => resets the oscillator and the pulse generators
#define COM_HVAMX4EDH_SW_TRIG_OUT (1UL <<  9) // Software-trigger engine output
#define COM_HVAMX4EDH_CLRN        (1UL << 10) // Device enable output, i.e. clear, active at 0 (CLRn)
*/
// State & configuration bits - COM_HVAMX4EDH_GetState/COM_HVAMX4EDH_SetConfig:
#define COM_HVAMX4EDH_ST_ENABLE        (1UL <<  0) // 1 enables the device, is controlled internally, can be set by COM_HVAMX4EDH_SetDeviceEnable only
#define COM_HVAMX4EDH_ST_ENB_TIMER     (1UL <<  1) // 1 enables the timers
#define COM_HVAMX4EDH_ST_ENB_OSC0      (1UL <<  2) // 1 enables the oscillator #0
#define COM_HVAMX4EDH_ST_ENB_OSC1      (1UL <<  3) // 1 enables the oscillator #1
#define COM_HVAMX4EDH_ST_ENB_OSC2      (1UL <<  4) // 1 enables the oscillator #2
#define COM_HVAMX4EDH_ST_ENB_OSC3      (1UL <<  5) // 1 enables the oscillator #3
#define COM_HVAMX4EDH_ST_ENB_OSC  (COM_HVAMX4EDH_ST_ENB_OSC0 | COM_HVAMX4EDH_ST_ENB_OSC1 | COM_HVAMX4EDH_ST_ENB_OSC2 | COM_HVAMX4EDH_ST_ENB_OSC3)
#define COM_HVAMX4EDH_ST_SW_TRIG0      (1UL <<  6) // Software trigger #0
#define COM_HVAMX4EDH_ST_SW_PULSE0     (1UL <<  7) // 1 generates a pulse at the software trigger SW_TrigOut0, SW_Trig0 will be inverted for 1CLK
#define COM_HVAMX4EDH_ST_SW_TRIG1      (1UL <<  8) // Software trigger #1
#define COM_HVAMX4EDH_ST_SW_PULSE1     (1UL <<  9) // 1 generates a pulse at the software trigger SW_TrigOut1, SW_Trig1 will be inverted for 1CLK
#define COM_HVAMX4EDH_ST_SW_TRIG  (COM_HVAMX4EDH_ST_SW_TRIG0  | COM_HVAMX4EDH_ST_SW_TRIG1 )
#define COM_HVAMX4EDH_ST_SW_PULSE (COM_HVAMX4EDH_ST_SW_PULSE0 | COM_HVAMX4EDH_ST_SW_PULSE1)
#define COM_HVAMX4EDH_ST_SW0      (COM_HVAMX4EDH_ST_SW_TRIG0  | COM_HVAMX4EDH_ST_SW_PULSE0)
#define COM_HVAMX4EDH_ST_SW1      (COM_HVAMX4EDH_ST_SW_TRIG1  | COM_HVAMX4EDH_ST_SW_PULSE1)
#define COM_HVAMX4EDH_ST_SW       (COM_HVAMX4EDH_ST_SW0       | COM_HVAMX4EDH_ST_SW1      )
#define COM_HVAMX4EDH_ST_DITHER        (1UL << 10) // 1 enables dithering for the auxiliary PSUs
#define COM_HVAMX4EDH_ST_ENB_SWITCH    (1UL << 11) // 1 enables the switch
// State bits - COM_HVAMX4EDH_GetState only:
#define COM_HVAMX4EDH_ST_ENB_PULS_GEN  (1UL << 12) // 1 enables the pulse generator
#define COM_HVAMX4EDH_ST_CLRN          (1UL << 16) // Device clear, active at 0
#define COM_HVAMX4EDH_ST_DEL_BUSY      (1UL << 17) // State of the delay busy signal
#define COM_HVAMX4EDH_ST_ENB_PWM       (1UL << 18) // State of the PWM enable
#define COM_HVAMX4EDH_ST_SW_TRIG_OUT0  (1UL << 22) // Software-trigger engine output 0
#define COM_HVAMX4EDH_ST_SW_TRIG_OUT1  (1UL << 23) // Software-trigger engine output 1
#define COM_HVAMX4EDH_ST_SW_TRIG_OUT  (COM_HVAMX4EDH_ST_SW_TRIG_OUT0 | COM_HVAMX4EDH_ST_SW_TRIG_OUT1)
#define COM_HVAMX4EDH_ST_DIO0          (1UL << 24) // Signal level on DIO0
#define COM_HVAMX4EDH_ST_DIO1          (1UL << 25) // Signal level on DIO1
#define COM_HVAMX4EDH_ST_DIO2          (1UL << 26) // Signal level on DIO2
#define COM_HVAMX4EDH_ST_DIO3          (1UL << 27) // Signal level on DIO3
#define COM_HVAMX4EDH_ST_DIO4          (1UL << 28) // Signal level on DIO4
#define COM_HVAMX4EDH_ST_DIO5          (1UL << 29) // Signal level on DIO5
#define COM_HVAMX4EDH_ST_DIO6          (1UL << 30) // Signal level on DIO6
#define COM_HVAMX4EDH_ST_DIO7          (1UL << 31) // Signal level on DIO7
#define COM_HVAMX4EDH_ST_DIO  (COM_HVAMX4EDH_ST_DIO0 | COM_HVAMX4EDH_ST_DIO1 | COM_HVAMX4EDH_ST_DIO2 | COM_HVAMX4EDH_ST_DIO3 | COM_HVAMX4EDH_ST_DIO4 | COM_HVAMX4EDH_ST_DIO5 | COM_HVAMX4EDH_ST_DIO6 | COM_HVAMX4EDH_ST_DIO7)
int _export COM_HVAMX4EDH_GetState  (WORD StreamNumber, DWORD & State, WORD & Config); // Get device state & configuration bits
int _export COM_HVAMX4EDH_SetConfig (WORD StreamNumber,                WORD   Config); // Set device configuration


/*****************************
// Configuration management //
*****************************/

int _export COM_HVAMX4EDH_GetDeviceEnable (WORD StreamNumber, BOOL & Enable); // Get the enable state of the device
int _export COM_HVAMX4EDH_SetDeviceEnable (WORD StreamNumber, BOOL   Enable); // Set the enable state of the device

int _export COM_HVAMX4EDH_GetInterlockFunct (WORD StreamNumber, BOOL & InterlockFunct); // Get the interlock functionality
int _export COM_HVAMX4EDH_SetInterlockFunct (WORD StreamNumber, BOOL   InterlockFunct); // Set the interlock functionality

//int _export COM_HVAMX4EDH_ResetCurrentConfig (WORD StreamNumber); // Reset current configuration

#define COM_HVAMX4EDH_CONFIG_DATA_SIZE  318
int _export COM_HVAMX4EDH_GetCurrentConfig (WORD StreamNumber,       BYTE Data [COM_HVAMX4EDH_CONFIG_DATA_SIZE]); // Get current configuration
int _export COM_HVAMX4EDH_SetCurrentConfig (WORD StreamNumber, const BYTE Data [COM_HVAMX4EDH_CONFIG_DATA_SIZE]); // Set current configuration

#define COM_HVAMX4EDH_DEFAULT_DATA_SIZE  20
int _export COM_HVAMX4EDH_GetDefaults      (WORD StreamNumber,       BYTE Data [COM_HVAMX4EDH_DEFAULT_DATA_SIZE]); // Get default delay data from NVM
int _export COM_HVAMX4EDH_SetDefaults      (WORD StreamNumber, const BYTE Data [COM_HVAMX4EDH_DEFAULT_DATA_SIZE]); // Set default delay data in NVM

#define COM_HVAMX4EDH_MAX_CONFIG  500
int _export COM_HVAMX4EDH_GetConfigList (WORD StreamNumber, bool Active [COM_HVAMX4EDH_MAX_CONFIG], bool Valid [COM_HVAMX4EDH_MAX_CONFIG]); // Get configuration list

int _export COM_HVAMX4EDH_SaveCurrentConfig (WORD StreamNumber, unsigned ConfigNumber); // Save current configuration to NVM
int _export COM_HVAMX4EDH_LoadCurrentConfig (WORD StreamNumber, unsigned ConfigNumber); // Load current configuration from NVM

int _export COM_HVAMX4EDH_SaveDefaults (WORD StreamNumber); // Save default delay data to NVM
int _export COM_HVAMX4EDH_LoadDefaults (WORD StreamNumber); // Load default delay data from NVM

#define COM_HVAMX4EDH_CONFIG_NAME_SIZE  193                                                                                            // Allowed size of the configuration name in NVM
int _export COM_HVAMX4EDH_GetConfigName  (WORD StreamNumber, unsigned ConfigNumber,       char Name [COM_HVAMX4EDH_CONFIG_NAME_SIZE]); // Get configuration name from NVM
int _export COM_HVAMX4EDH_SetConfigName  (WORD StreamNumber, unsigned ConfigNumber, const char Name [COM_HVAMX4EDH_CONFIG_NAME_SIZE]); // Set configuration name in NVM

int _export COM_HVAMX4EDH_GetConfigData  (WORD StreamNumber, unsigned ConfigNumber,       BYTE Data [COM_HVAMX4EDH_CONFIG_DATA_SIZE]); // Get configuration data from NVM
int _export COM_HVAMX4EDH_SetConfigData  (WORD StreamNumber, unsigned ConfigNumber, const BYTE Data [COM_HVAMX4EDH_CONFIG_DATA_SIZE]); // Set configuration data in NVM

int _export COM_HVAMX4EDH_GetConfigFlags (WORD StreamNumber, unsigned ConfigNumber, bool & Active, bool & Valid); // Get configuration flags from NVM
int _export COM_HVAMX4EDH_SetConfigFlags (WORD StreamNumber, unsigned ConfigNumber, bool   Active, bool   Valid); // Set configuration flags in NVM


/***********
// System //
***********/

int _export COM_HVAMX4EDH_Restart      (WORD StreamNumber);                                                       // Restart the controller
int _export COM_HVAMX4EDH_GetCPUData   (WORD StreamNumber, double & Load, double & Frequency);                    // Get CPU load (0-1) and frequency (Hz)
int _export COM_HVAMX4EDH_GetUptime    (WORD StreamNumber, DWORD & Seconds, WORD & Milliseconds, DWORD & Optime); // Get device uptime and operation time
int _export COM_HVAMX4EDH_GetTotalTime (WORD StreamNumber, DWORD & Uptime,                       DWORD & Optime); // Get total device uptime and operation time

int _export COM_HVAMX4EDH_GetFwVersion (WORD StreamNumber, WORD & FwVersion);                                     // Get the firmware version
#define COM_HVAMX4EDH_DATA_STRING_SIZE  12
int _export COM_HVAMX4EDH_GetFwDate    (WORD StreamNumber, char * FwDateString);                                  // Get the firmware date, DateString should be at least COM_HVAMX4EDH_DATA_STRING_SIZE characters long
#define COM_HVAMX4EDH_PRODUCT_ID_SIZE   81
int _export COM_HVAMX4EDH_GetProductID (WORD StreamNumber, char * Identification);                                // Get the product identification, Identification should be at least COM_HVAMX4EDH_PRODUCT_ID_SIZE characters long
int _export COM_HVAMX4EDH_GetProductNo (WORD StreamNumber, DWORD & Number);                                       // Get the product number
int _export COM_HVAMX4EDH_GetManufDate (WORD StreamNumber, WORD & Year, WORD & CalendarWeek);                     // Get manufacturing date
#define COM_HVAMX4EDH_CPU_ID (0x5A3A)                                                                             // Expected CPU identification
int _export COM_HVAMX4EDH_GetCPU_ID    (WORD StreamNumber, WORD & CPU_ID);                                        // Get CPU identification
#define COM_HVAMX4EDH_DEVICE_TYPE (0x0B21)                                                                        // Expected device type
int _export COM_HVAMX4EDH_GetDevType   (WORD StreamNumber, WORD & DevType);                                       // Get device type
int _export COM_HVAMX4EDH_GetHwType    (WORD StreamNumber, DWORD & HwType, WORD & NumSignal, WORD & NumRegister,  // Get the hardware type and the number of implemented modules
	WORD & NumBurstTimer, WORD & NumTimer, WORD & NumOscil, WORD & NumMapping,
	WORD & NumSwitchDualLevel, WORD & NumSwitchTriLevel);
int _export COM_HVAMX4EDH_GetHwVersion (WORD StreamNumber, WORD & HwVersion, WORD & FPGA_Version);                // Get the hardware versions


/***********************
// Communication port //
***********************/

int          _export COM_HVAMX4EDH_GetInterfaceState   (WORD StreamNumber);                    // Get software interface state
#ifdef _MSC_VER
_export const char * COM_HVAMX4EDH_GetErrorMessage     (WORD StreamNumber);                    // Get the error message corresponding to the software interface state
_export const char * COM_HVAMX4EDH_GetIOErrorMessage   (WORD StreamNumber);                    // Get the error message corresponding to the serial port interface state
#else
const char * _export COM_HVAMX4EDH_GetErrorMessage     (WORD StreamNumber);                    // Get the error message corresponding to the software interface state
const char * _export COM_HVAMX4EDH_GetIOErrorMessage   (WORD StreamNumber);                    // Get the error message corresponding to the serial port interface state
#endif
int          _export COM_HVAMX4EDH_GetIOState          (WORD StreamNumber, int   & IOState);   // Get and clear last serial port interface state
#ifdef _MSC_VER
_export const char * COM_HVAMX4EDH_GetIOStateMessage   (                   int     IOState);   // Get the error message corresponding to the specified interface state
#else
const char * _export COM_HVAMX4EDH_GetIOStateMessage   (                   int     IOState);   // Get the error message corresponding to the specified interface state
#endif
int          _export COM_HVAMX4EDH_GetCommError        (WORD StreamNumber, DWORD & CommError); // Get and clear last communication-port error
#ifdef _MSC_VER
_export const char * COM_HVAMX4EDH_GetCommErrorMessage (                   DWORD   CommError); // Get the error message corresponding to the communication port error
#else
const char * _export COM_HVAMX4EDH_GetCommErrorMessage (                   DWORD   CommError); // Get the error message corresponding to the communication port error
#endif

#ifdef __cplusplus
}
#endif

#endif//__COM_HVAMX4EDH_H__
