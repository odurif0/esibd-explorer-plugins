/******************************************************************************
//                                                                           //
//  Project: Software Interface for HV-AMX-CTRL-4ED Devices                  //
//                                                                           //
//  CGC Instruments, (c) 2009-2021, Version 1.00. All rights reserved.       //
//                                                                           //
//  Definition for COM-HVAMX4ED DLL Routines                                 //
//                                                                           //
******************************************************************************/

#ifndef __COM_HVAMX4ED_H__
#define __COM_HVAMX4ED_H__


#ifdef __cplusplus
extern "C" {
#endif


#define COM_HVAMX4ED_MAX_PORT 16 /* Maximum PortNumber */

/*
	The listed routines accept the number of the port PortNumber in the range 0..COM_HVAMX4ED_MAX_PORT-1,
	thus maximum COM_HVAMX4ED_MAX_PORT independent communication channels (ports) can be defined

	Each communication channel must be opened before the first usage. The Open routine assigns a specified COMx port to the channel.
	The ComNumber specifies the number of the port - 1 stands for COM1, 2 for COM2, etc.

	If necessary, the channel may be closed and assigned by the Open routine to a new COMx port.
	The channels are closed automatically at the end of the program, the main program does not need to take care about it.

	The return values of the routines are the following defines COM_HVAMX4ED_ERR_XXX
		 0: routine finished without any error (COM_HVAMX4ED_NO_ERR)
		<0: errors COM_HVAMX4ED_ERR_XXX

	The last error code can be obtained by calling COM_HVAMX4ED_State.
	The routine COM_HVAMX4ED_ErrorMessage returns a pointer to a zero-terminated character string.

	If a communication error occurred, the error code can be red again by COM_HVAMX4ED_IO_State,
	The routine COM_HVAMX4ED_IO_ErrorMessage provides the corresponding error message.
*/

#define COM_HVAMX4ED_NO_ERR                 0 /* No error occurred                           */

#define COM_HVAMX4ED_ERR_PORT_RANGE        -1 /* PortNumber out of range                     */
#define COM_HVAMX4ED_ERR_OPEN              -2 /* Error opening the port                      */
#define COM_HVAMX4ED_ERR_CLOSE             -3 /* Error closing the port                      */
#define COM_HVAMX4ED_ERR_PURGE             -4 /* Error purging the port                      */
#define COM_HVAMX4ED_ERR_CONTROL           -5 /* Error setting the port control lines        */
#define COM_HVAMX4ED_ERR_STATUS            -6 /* Error reading the port status lines         */
#define COM_HVAMX4ED_ERR_COMMAND_SEND      -7 /* Error sending command                       */
#define COM_HVAMX4ED_ERR_DATA_SEND         -8 /* Error sending data                          */
#define COM_HVAMX4ED_ERR_TERM_SEND         -9 /* Error sending termination character         */
#define COM_HVAMX4ED_ERR_COMMAND_RECEIVE  -10 /* Error receiving command                     */
#define COM_HVAMX4ED_ERR_DATA_RECEIVE     -11 /* Error receiving data                        */
#define COM_HVAMX4ED_ERR_TERM_RECEIVE     -12 /* Error receiving termination character       */
#define COM_HVAMX4ED_ERR_COMMAND_WRONG    -13 /* Wrong command received                      */
#define COM_HVAMX4ED_ERR_ARGUMENT_WRONG   -14 /* Wrong argument received                     */
#define COM_HVAMX4ED_ERR_ARGUMENT         -15 /* Wrong argument passed to the function       */
#define COM_HVAMX4ED_ERR_RATE             -16 /* Error setting the baud rate                 */

#define COM_HVAMX4ED_ERR_NOT_CONNECTED   -100 /* Device not connected                        */
#define COM_HVAMX4ED_ERR_NOT_READY       -101 /* Device not ready                            */
#define COM_HVAMX4ED_ERR_READY           -102 /* Device state could not be set to not ready  */

#define COM_HVAMX4ED_ERR_DEBUG_OPEN      -400 /* Error opening the file for debugging output */
#define COM_HVAMX4ED_ERR_DEBUG_CLOSE     -401 /* Error closing the file for debugging output */


/* General */

WORD FAR _export COM_HVAMX4ED_GetSWVersion(); /* Get the software version */

int FAR _export COM_HVAMX4ED_Open  (WORD PortNumber, WORD ComNumber); /* Open the port  */
int FAR _export COM_HVAMX4ED_Close (WORD PortNumber);                 /* Close the port */

int FAR _export COM_HVAMX4ED_SetBaudRate (WORD PortNumber, unsigned & BaudRate); /* Set the baud rate and return the set value */

int FAR _export COM_HVAMX4ED_Purge          (WORD PortNumber);               /* Clear data buffers for the port                             */
int FAR _export COM_HVAMX4ED_DevicePurge    (WORD PortNumber, BOOL & Empty); /* Clear output data buffer of the device                      */
int FAR _export COM_HVAMX4ED_GetBufferState (WORD PortNumber, BOOL & Empty); /* Return true if the input data buffer of the device is empty */


/* Device control */

#define COM_HVAMX4ED_STATE_ON             0x0000                           /* Device active                                     */
#define COM_HVAMX4ED_STATE_ERROR          0x8000                           /* Error bit                                         */
#define COM_HVAMX4ED_STATE_ERR_VSUP       0x8001                           /* Voltage failure                                   */
#define COM_HVAMX4ED_STATE_ERR_TEMP_LOW   0x8002                           /* Low-temperature failure                           */
#define COM_HVAMX4ED_STATE_ERR_TEMP_HIGH  0x8003                           /* High-temperature failure                          */
#define COM_HVAMX4ED_STATE_ERR_FPGA_DIS   0x8004                           /* Internal failure (FPGA disabled or unresponsive)  */
int FAR _export COM_HVAMX4ED_GetMainState (WORD PortNumber, WORD & State); /* Get the main device status                        */

#define COM_HVAMX4ED_DEVST_OK                  0                                    /* No error detected             */
#define COM_HVAMX4ED_DEVST_VCPU_FAIL   (1UL << 0x00)                                /* CPU supply voltage failed     */
#define COM_HVAMX4ED_DEVST_VSUP_FAIL   (1UL << 0x01)                                /* Fan supply voltage failed     */
#define COM_HVAMX4ED_DEVST_FAN1_FAIL   (1UL << 0x08)                                /* Fan #1 failed                 */
#define COM_HVAMX4ED_DEVST_FAN2_FAIL   (1UL << 0x09)                                /* Fan #2 failed                 */
#define COM_HVAMX4ED_DEVST_FAN3_FAIL   (1UL << 0x0A)                                /* Fan #3 failed                 */
#define COM_HVAMX4ED_DEVST_FPGA_DIS    (1UL << 0x0F)                                /* FPGA disabled or unresponsive */
#define COM_HVAMX4ED_DEVST_SEN1_HIGH   (1UL << 0x10)                                /* Temperature sensor #1 hot     */
#define COM_HVAMX4ED_DEVST_SEN2_HIGH   (1UL << 0x11)                                /* Temperature sensor #2 hot     */
#define COM_HVAMX4ED_DEVST_SEN3_HIGH   (1UL << 0x12)                                /* Temperature sensor #3 hot     */
#define COM_HVAMX4ED_DEVST_SEN1_LOW    (1UL << 0x18)                                /* Temperature sensor #1 cold    */
#define COM_HVAMX4ED_DEVST_SEN2_LOW    (1UL << 0x19)                                /* Temperature sensor #2 cold    */
#define COM_HVAMX4ED_DEVST_SEN3_LOW    (1UL << 0x1A)                                /* Temperature sensor #3 cold    */
int FAR _export COM_HVAMX4ED_GetDeviceState (WORD PortNumber, DWORD & DeviceState); /* Get the device status         */

int FAR _export COM_HVAMX4ED_GetHousekeeping (WORD PortNumber, double & Volt12V, double & Volt5V0, double & Volt3V3, double & TempCPU); /* Get the housekeeping data */

#define COM_HVAMX4ED_SEN_COUNT       3                                                                     /* Number of implemented sensors */
int FAR _export COM_HVAMX4ED_GetSensorData (WORD PortNumber, double Temperature [COM_HVAMX4ED_SEN_COUNT]); /* Get sensor data               */

#define COM_HVAMX4ED_FAN_COUNT       3                                                                                              /* Number of implemented fans */
#define COM_HVAMX4ED_FAN_PWM_MAX  1000                                                                                              /* Maximum PWM value (100%)   */
int FAR _export COM_HVAMX4ED_GetFanData (WORD PortNumber, BOOL Enabled [COM_HVAMX4ED_FAN_COUNT], BOOL Failed [COM_HVAMX4ED_FAN_COUNT],
	WORD SetRPM [COM_HVAMX4ED_FAN_COUNT], WORD MeasuredRPM [COM_HVAMX4ED_FAN_COUNT], WORD PWM [COM_HVAMX4ED_FAN_COUNT]);              /* Get fan data               */

int FAR _export COM_HVAMX4ED_GetLEDData (WORD PortNumber, BOOL & Red, BOOL & Green, BOOL & Blue); /* Get LED data */


/* Pulser management */

#define COM_HVAMX4ED_CLOCK       (100E6)
#define COM_HVAMX4ED_OSC_OFFSET     2
int FAR _export COM_HVAMX4ED_GetOscillatorPeriod (WORD PortNumber, DWORD & Period); /* Get oscillator period */
int FAR _export COM_HVAMX4ED_SetOscillatorPeriod (WORD PortNumber, DWORD   Period); /* Set oscillator period */

#define COM_HVAMX4ED_PULSER_NUM           4
#define COM_HVAMX4ED_PULSER_DELAY_OFFSET  3
int FAR _export COM_HVAMX4ED_GetPulserDelay (WORD PortNumber, unsigned PulserNo, DWORD & Delay); /* Get pulse delay of specified pulser */
int FAR _export COM_HVAMX4ED_SetPulserDelay (WORD PortNumber, unsigned PulserNo, DWORD   Delay); /* Set pulse delay of specified pulser */
#define COM_HVAMX4ED_PULSER_WIDTH_OFFSET  2
int FAR _export COM_HVAMX4ED_GetPulserWidth (WORD PortNumber, unsigned PulserNo, DWORD & Width); /* Get pulse width of specified pulser */
int FAR _export COM_HVAMX4ED_SetPulserWidth (WORD PortNumber, unsigned PulserNo, DWORD   Width); /* Set pulse width of specified pulser */
#define COM_HVAMX4ED_PULSER_BURST_NUM     2
#define COM_HVAMX4ED_MAX_BURST   (1UL << 24)
int FAR _export COM_HVAMX4ED_GetPulserBurst (WORD PortNumber, unsigned PulserNo, DWORD & Burst); /* Get burst size of specified pulser */
int FAR _export COM_HVAMX4ED_SetPulserBurst (WORD PortNumber, unsigned PulserNo, DWORD   Burst); /* Set burst size of specified pulser */

#define COM_HVAMX4ED_CFG_LOG0         0 /* Logic 0, stop      */
#define COM_HVAMX4ED_CFG_SOFT_TRIG    1 /* Software trigger   */
#define COM_HVAMX4ED_CFG_OSC0         2 /* Oscillator 0       */
#define COM_HVAMX4ED_CFG_DIN0         3 /* Digital input 1    */
#define COM_HVAMX4ED_CFG_DIN1         4 /* Digital input 2    */
#define COM_HVAMX4ED_CFG_DIN2         5 /* Digital input 3    */
#define COM_HVAMX4ED_CFG_DIN3         6 /* Digital input 4    */
#define COM_HVAMX4ED_CFG_DIN4         7 /* Digital input 5    */
#define COM_HVAMX4ED_CFG_DIN5         8 /* Digital input 6    */
#define COM_HVAMX4ED_CFG_DIN6         9 /* Digital input 7    */
#define COM_HVAMX4ED_CFG_PULS_OUT0   10 /* Pulser 1 output    */
#define COM_HVAMX4ED_CFG_PULS_OUT1   11 /* Pulser 2 output    */
#define COM_HVAMX4ED_CFG_PULS_OUT2   12 /* Pulser 3 output    */
#define COM_HVAMX4ED_CFG_PULS_OUT3   13 /* Pulser 4 output    */
#define COM_HVAMX4ED_CFG_PULS_RUN0   14 /* Pulser 1 run       */
#define COM_HVAMX4ED_CFG_PULS_RUN1   15 /* Pulser 2 run       */
#define COM_HVAMX4ED_CFG_PULS_RUN2   16 /* Pulser 3 run       */
#define COM_HVAMX4ED_CFG_PULS_RUN3   17 /* Pulser 4 run       */
#define COM_HVAMX4ED_CFG_CLK2M       18 /* Timebase 2 MHz     */
#define COM_HVAMX4ED_CFG_CLK4M       19 /* Timebase 4 MHz     */
#define COM_HVAMX4ED_CFG_MASK        31 /* Configuration mask */
#define COM_HVAMX4ED_CFG_INVERT  (1<<5) /* Level negated      */

#define COM_HVAMX4ED_CONFIG_SIZE   6                                      /* Bit size of the configuration    */
#define COM_HVAMX4ED_CONFIG_MAX   (1UL << COM_HVAMX4ED_CONFIG_SIZE)       /* Maximum configuration            */
#define COM_HVAMX4ED_CONFIG_MASK  (COM_HVAMX4ED_CONFIG_MAX - 1)           /* Configuration bit mask           */
#define COM_HVAMX4ED_CONFIG_INV   (1UL << (COM_HVAMX4ED_CONFIG_SIZE - 1)) /* Bit Inv for inverting the signal */
#define COM_HVAMX4ED_SELECT_MASK  (COM_HVAMX4ED_CONFIG_INV - 1)           /* Signal-selection bit mask        */

#define COM_HVAMX4ED_PULSER_CFG_NUM   (COM_HVAMX4ED_PULSER_NUM + COM_HVAMX4ED_PULSER_BURST_NUM) /* Total number of pulser configurations          */
#define COM_HVAMX4ED_PULSER_INPUT_MAX  18                                                       /* Number of possible pulser configuration inputs */
/*
PulserCfgNo in following functions has the following assignments:
0                                     = trigger cfg. of pulser #0,
1                                     = stop    cfg. of pulser #0,
...
COM_HVAMX4ED_PULSER_BURST_NUM*2-1 (3) = stop    cfg. of pulser #(COM_HVAMX4ED_PULSER_BURST_NUM-1)=#1,
COM_HVAMX4ED_PULSER_BURST_NUM*2   (3) = trigger cfg. of pulser #COM_HVAMX4ED_PULSER_BURST_NUM=#2,
...
COM_HVAMX4ED_PULSER_CONFIG_MAX-1  (5) = trigger cfg. of pulser #(COM_HVAMX4ED_PULSER_NUM-1)=#3,
*/
int FAR _export COM_HVAMX4ED_GetPulserConfig (WORD PortNumber, unsigned PulserCfgNo, BYTE & Config); /* Get configuration of specified pulser */
int FAR _export COM_HVAMX4ED_SetPulserConfig (WORD PortNumber, unsigned PulserCfgNo, BYTE   Config); /* Set configuration of specified pulser */

#define COM_HVAMX4ED_SWITCH_NUM  4                                                                        /* Number of switches                            */
int FAR _export COM_HVAMX4ED_GetSwitchTriggerConfig (WORD PortNumber, unsigned SwitchNo, BYTE & Config ); /* Get configuration of specified switch trigger */
int FAR _export COM_HVAMX4ED_SetSwitchTriggerConfig (WORD PortNumber, unsigned SwitchNo, BYTE   Config ); /* Set configuration of specified switch trigger */
int FAR _export COM_HVAMX4ED_GetSwitchEnableConfig  (WORD PortNumber, unsigned SwitchNo, BYTE & Config ); /* Get configuration of specified switch enable  */
int FAR _export COM_HVAMX4ED_SetSwitchEnableConfig  (WORD PortNumber, unsigned SwitchNo, BYTE   Config ); /* Set configuration of specified switch enable  */

#define COM_HVAMX4ED_SWITCH_DELAY_SIZE   4                                                                                   /* Bit size of the switch delay           */
#define COM_HVAMX4ED_SWITCH_DELAY_MAX   (1UL << COM_HVAMX4ED_SWITCH_DELAY_SIZE)                                              /* Maximum switch delay                   */
#define COM_HVAMX4ED_SWITCH_DELAY_MASK  (COM_HVAMX4ED_SWITCH_DELAY_MAX - 1)                                                  /* Switch-delay bit mask                  */
int FAR _export COM_HVAMX4ED_GetSwitchTriggerDelay (WORD PortNumber, unsigned SwitchNo, BYTE & RiseDelay, BYTE & FallDelay); /* Get delays of specified switch trigger */
int FAR _export COM_HVAMX4ED_SetSwitchTriggerDelay (WORD PortNumber, unsigned SwitchNo, BYTE   RiseDelay, BYTE   FallDelay); /* Set delays of specified switch trigger */
int FAR _export COM_HVAMX4ED_GetSwitchEnableDelay  (WORD PortNumber, unsigned SwitchNo, BYTE & Delay);                       /* Get delay of specified switch enable   */
int FAR _export COM_HVAMX4ED_SetSwitchEnableDelay  (WORD PortNumber, unsigned SwitchNo, BYTE   Delay);                       /* Set delay of specified switch enable   */

#define COM_HVAMX4ED_MAPPING_SIZE   COM_HVAMX4ED_SWITCH_NUM                                                       /* Bit size of the switch mapping            */
#define COM_HVAMX4ED_MAPPING_MAX   (1UL << COM_HVAMX4ED_MAPPING_SIZE)                                             /* Maximum switch mapping                    */
#define COM_HVAMX4ED_MAPPING_MASK  (COM_HVAMX4ED_MAPPING_MAX - 1)                                                 /* Switch mapping mask                       */
#define COM_HVAMX4ED_MAPPING_NUM   (COM_HVAMX4ED_SWITCH_NUM + 1)                                                  /* Number of mappings                        */
int FAR _export COM_HVAMX4ED_GetSwitchTriggerMapping       (WORD PortNumber, unsigned MappingNo, BYTE & Mapping); /* Get specified switch trigger mapping      */
int FAR _export COM_HVAMX4ED_SetSwitchTriggerMapping       (WORD PortNumber, unsigned MappingNo, BYTE   Mapping); /* Set specified switch trigger mapping      */
int FAR _export COM_HVAMX4ED_GetSwitchEnableMapping        (WORD PortNumber, unsigned MappingNo, BYTE & Mapping); /* Get specified switch enable mapping       */
int FAR _export COM_HVAMX4ED_SetSwitchEnableMapping        (WORD PortNumber, unsigned MappingNo, BYTE   Mapping); /* Set specified switch enable mapping       */
int FAR _export COM_HVAMX4ED_GetSwitchTriggerMappingEnable (WORD PortNumber, bool & Enable);                      /* Get the switch trigger mapping enable bit */
int FAR _export COM_HVAMX4ED_SetSwitchTriggerMappingEnable (WORD PortNumber, bool   Enable);                      /* Set the switch trigger mapping enable bit */
int FAR _export COM_HVAMX4ED_GetSwitchEnableMappingEnable  (WORD PortNumber, bool & Enable);                      /* Get the switch enable mapping enable bit  */
int FAR _export COM_HVAMX4ED_SetSwitchEnableMappingEnable  (WORD PortNumber, bool   Enable);                      /* Set the switch enable mapping enable bit  */

#define COM_HVAMX4ED_DIO_NUM         7                                                                         /* Number of digital inputs/outputs               */
#define COM_HVAMX4ED_DIO_INPUT_MAX  20                                                                         /* Number of possible output configuration inputs */
int FAR _export COM_HVAMX4ED_GetInputConfig  (WORD PortNumber, BYTE & OutputEnable, BYTE & TerminationEnable); /* Get configuration of digital inputs/outputs    */
int FAR _export COM_HVAMX4ED_SetInputConfig  (WORD PortNumber, BYTE   OutputEnable, BYTE   TerminationEnable); /* Set configuration of digital inputs/outputs    */
int FAR _export COM_HVAMX4ED_GetOutputConfig (WORD PortNumber, unsigned OutputNo,   BYTE & Configuration);     /* Get configuration of specified output          */
int FAR _export COM_HVAMX4ED_SetOutputConfig (WORD PortNumber, unsigned OutputNo,   BYTE   Configuration);     /* Set configuration of specified output          */

/* State & configuration bits */
#define COM_HVAMX4ED_ENB         (1UL <<  0) /* Enables the switches, if COM_HVAMX4ED_PREVENT_DIS=0, 0 forces CLRn=0 => resets the oscillator and the pulse generators and stops the PSUs */
#define COM_HVAMX4ED_ENB_OSC     (1UL <<  1) /* 1 enables the oscillator                                                                                                                  */
#define COM_HVAMX4ED_ENB_PULSER  (1UL <<  2) /* 1 enables the pulse generators                                                                                                            */
#define COM_HVAMX4ED_SW_TRIG     (1UL <<  3) /* Software trigger                                                                                                                          */
#define COM_HVAMX4ED_SW_PULSE    (1UL <<  4) /* 1 creates a 1-CLK wide pulse at software trigger                                                                                          */
#define COM_HVAMX4ED_PREVENT_DIS (1UL <<  5) /* Disable the CLRn=0 => 1 prevents CLRn=0 when ST_ENABLE=0 => only the switches are disabled, not their PSUs                                */
#define COM_HVAMX4ED_DIS_DITHER  (1UL <<  6) /* 1 disable the dithering of the internal switching regulators                                                                              */
#define COM_HVAMX4ED_NC          (1UL <<  7) /* Bit not used                                                                                                                              */
/* COM_HVAMX4ED_GetState bits */
#define COM_HVAMX4ED_ENABLE      (1UL <<  8) /* Master enable, 0 forces CLRn=0 => resets the oscillator and the pulse generators */
#define COM_HVAMX4ED_SW_TRIG_OUT (1UL <<  9) /* Software-trigger engine output                                                   */
#define COM_HVAMX4ED_CLRN        (1UL << 10) /* Device enable output, i.e. clear, active at 0 (CLRn)                             */
int FAR _export COM_HVAMX4ED_GetControllerState  (WORD PortNumber, WORD & State); /* Get device state         */
int FAR _export COM_HVAMX4ED_SetControllerConfig (WORD PortNumber, BYTE  Config); /* Set device configuration */


/* Configuration management */

int FAR _export COM_HVAMX4ED_GetDeviceEnable (WORD PortNumber, BOOL & Enable); /* Get the enable state of the device */
int FAR _export COM_HVAMX4ED_SetDeviceEnable (WORD PortNumber, BOOL   Enable); /* Set the enable state of the device */

int FAR _export COM_HVAMX4ED_ResetCurrentConfig (WORD PortNumber); /* Reset current configuration */

#define COM_HVAMX4ED_MAX_CONFIG  126
int FAR _export COM_HVAMX4ED_SaveCurrentConfig (WORD PortNumber, unsigned ConfigNumber); /* Save current configuration to NVM   */
int FAR _export COM_HVAMX4ED_LoadCurrentConfig (WORD PortNumber, unsigned ConfigNumber); /* Load current configuration from NVM */

#define COM_HVAMX4ED_CONFIG_NAME_SIZE  52
int FAR _export COM_HVAMX4ED_GetConfigName  (WORD PortNumber, unsigned ConfigNumber,       char Name [COM_HVAMX4ED_CONFIG_NAME_SIZE]); /* Get configuration name */
int FAR _export COM_HVAMX4ED_SetConfigName  (WORD PortNumber, unsigned ConfigNumber, const char Name [COM_HVAMX4ED_CONFIG_NAME_SIZE]); /* Set configuration name */

int FAR _export COM_HVAMX4ED_GetConfigFlags (WORD PortNumber, unsigned ConfigNumber, bool & Active, bool & Valid); /* Get configuration flags */
int FAR _export COM_HVAMX4ED_SetConfigFlags (WORD PortNumber, unsigned ConfigNumber, bool   Active, bool   Valid); /* Set configuration flags */

int FAR _export COM_HVAMX4ED_GetConfigList  (WORD PortNumber, bool Active [COM_HVAMX4ED_MAX_CONFIG], bool Valid [COM_HVAMX4ED_MAX_CONFIG]); /* Get configuration list */


/* System */

int FAR _export COM_HVAMX4ED_Restart      (WORD PortNumber);                                                       /* Restart the controller                     */
int FAR _export COM_HVAMX4ED_GetCPUData   (WORD PortNumber, double & Load, double & Frequency);                    /* Get CPU load (0-1) and frequency (Hz)      */
int FAR _export COM_HVAMX4ED_GetUptime    (WORD PortNumber, DWORD & Seconds, WORD & Milliseconds, DWORD & Optime); /* Get device uptime and operation time       */
int FAR _export COM_HVAMX4ED_GetTotalTime (WORD PortNumber, DWORD & Uptime,                       DWORD & Optime); /* Get total device uptime and operation time */

int FAR _export COM_HVAMX4ED_GetHWType    (WORD PortNumber, WORD & HWType   ); /* Get the hardware type    */
int FAR _export COM_HVAMX4ED_GetHWVersion (WORD PortNumber, WORD & HWVersion); /* Get the hardware version */

int FAR _export COM_HVAMX4ED_GetFWVersion (WORD PortNumber, WORD & Version);            /* Get the firmware version                                                             */
int FAR _export COM_HVAMX4ED_GetFWDate    (WORD PortNumber, char * FAR DateString);     /* Get the firmware date, DateString should be at least 16 characters long              */
int FAR _export COM_HVAMX4ED_GetProductID (WORD PortNumber, char * FAR Identification); /* Get the product identification, Identification should be at least 60 characters long */
int FAR _export COM_HVAMX4ED_GetProductNo (WORD PortNumber, DWORD & Number);            /* Get the product number                                                               */


/* Communication port */

int          FAR _export COM_HVAMX4ED_GetInterfaceState (WORD PortNumber); /* Get software interface state                                           */
const char * FAR _export COM_HVAMX4ED_GetErrorMessage   (WORD PortNumber); /* Get the error message corresponding to the software interface state    */
int          FAR _export COM_HVAMX4ED_GetIOState        (WORD PortNumber); /* Get serial port interface state                                        */
const char * FAR _export COM_HVAMX4ED_GetIOErrorMessage (WORD PortNumber); /* Get the error message corresponding to the serial port interface state */

#ifdef __cplusplus
}
#endif


#endif//__COM_HVAMX4ED_H__
