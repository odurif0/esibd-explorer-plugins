/******************************************************************************
//                                                                           //
//  Project: Software Interface for HV-PSU-2D Devices                        //
//                                                                           //
//  CGC Instruments, (c) 2009-2021, Version 1.00. All rights reserved.       //
//                                                                           //
//  Definition for COM-HVPSU2D DLL Routines                                  //
//                                                                           //
******************************************************************************/

#ifndef __COM_HVPSU2D_H__
#define __COM_HVPSU2D_H__


#ifdef __cplusplus
extern "C" {
#endif


#define COM_HVPSU2D_MAX_PORT 16 /* Maximum PortNumber */

/*
	The listed routines accept the number of the port PortNumber in the range 0..COM_HVPSU2D_MAX_PORT-1,
	thus maximum COM_HVPSU2D_MAX_PORT independent communication channels (ports) can be defined

	Each communication channel must be opened before the first usage. The Open routine assigns a specified COMx port to the channel.
	The ComNumber specifies the number of the port - 1 stands for COM1, 2 for COM2, etc.

	If necessary, the channel may be closed and assigned by the Open routine to a new COMx port.
	The channels are closed automatically at the end of the program, the main program does not need to take care about it.

	The return values of the routines are the following defines COM_HVPSU2D_ERR_XXX
		 0: routine completed without any error (COM_HVPSU2D_NO_ERR)
		<0: errors COM_HVPSU2D_ERR_XXX

	The last error code can be obtained by calling COM_HVPSU2D_State.
	The routine COM_HVPSU2D_ErrorMessage returns a pointer to a zero-terminated character string.

	If a communication error occurred, the error code can be red again by COM_HVPSU2D_IO_State,
	The routine COM_HVPSU2D_IO_ErrorMessage provides the corresponding error message.
*/

#define COM_HVPSU2D_NO_ERR                 0 /* No error occurred                           */

#define COM_HVPSU2D_ERR_PORT_RANGE        -1 /* PortNumber out of range                     */
#define COM_HVPSU2D_ERR_OPEN              -2 /* Error opening the port                      */
#define COM_HVPSU2D_ERR_CLOSE             -3 /* Error closing the port                      */
#define COM_HVPSU2D_ERR_PURGE             -4 /* Error purging the port                      */
#define COM_HVPSU2D_ERR_CONTROL           -5 /* Error setting the port control lines        */
#define COM_HVPSU2D_ERR_STATUS            -6 /* Error reading the port status lines         */
#define COM_HVPSU2D_ERR_COMMAND_SEND      -7 /* Error sending command                       */
#define COM_HVPSU2D_ERR_DATA_SEND         -8 /* Error sending data                          */
#define COM_HVPSU2D_ERR_TERM_SEND         -9 /* Error sending termination character         */
#define COM_HVPSU2D_ERR_COMMAND_RECEIVE  -10 /* Error receiving command                     */
#define COM_HVPSU2D_ERR_DATA_RECEIVE     -11 /* Error receiving data                        */
#define COM_HVPSU2D_ERR_TERM_RECEIVE     -12 /* Error receiving termination character       */
#define COM_HVPSU2D_ERR_COMMAND_WRONG    -13 /* Wrong command received                      */
#define COM_HVPSU2D_ERR_ARGUMENT_WRONG   -14 /* Wrong argument received                     */
#define COM_HVPSU2D_ERR_ARGUMENT         -15 /* Wrong argument passed to the function       */
#define COM_HVPSU2D_ERR_RATE             -16 /* Error setting the baud rate                 */

#define COM_HVPSU2D_ERR_NOT_CONNECTED   -100 /* Device not connected                        */
#define COM_HVPSU2D_ERR_NOT_READY       -101 /* Device not ready                            */
#define COM_HVPSU2D_ERR_READY           -102 /* Device state could not be set to not ready  */

#define COM_HVPSU2D_ERR_DEBUG_OPEN      -400 /* Error opening the file for debugging output */
#define COM_HVPSU2D_ERR_DEBUG_CLOSE     -401 /* Error closing the file for debugging output */


/* General */

WORD FAR _export COM_HVPSU2D_GetSWVersion(); /* Get the software version */

###int FAR _export COM_HVPSU2D_Open  (WORD PortNumber, WORD ComNumber); /* Open the port  */
###int FAR _export COM_HVPSU2D_Close (WORD PortNumber);                 /* Close the port */

###int FAR _export COM_HVPSU2D_SetBaudRate (WORD PortNumber, unsigned & BaudRate); /* Set the baud rate and return the set value */

int FAR _export COM_HVPSU2D_Purge          (WORD PortNumber);               /* Clear data buffers for the port                             */
int FAR _export COM_HVPSU2D_DevicePurge    (WORD PortNumber, BOOL & Empty); /* Clear output data buffer of the device                      */
int FAR _export COM_HVPSU2D_GetBufferState (WORD PortNumber, BOOL & Empty); /* Return true if the input data buffer of the device is empty */


/* Device control */

int FAR _export COM_HVPSU2D_SetInterlockEnable (WORD PortNumber, BOOL   ConOut, BOOL   ConBNC); /* Set interlock enable for the output and the BNC connectors */
int FAR _export COM_HVPSU2D_GetInterlockEnable (WORD PortNumber, BOOL & ConOut, BOOL & ConBNC); /* Get interlock enable for the output and the BNC connectors */

#define COM_HVPSU2D_STATE_ON             0x0000                           /* Device active              */
#define COM_HVPSU2D_STATE_ERROR          0x8000                           /* Error bit                  */
#define COM_HVPSU2D_STATE_ERR_VSUP       0x8001                           /* Voltage failure            */
#define COM_HVPSU2D_STATE_ERR_TEMP_LOW   0x8002                           /* Low-temperature failure    */
#define COM_HVPSU2D_STATE_ERR_TEMP_HIGH  0x8003                           /* High-temperature failure   */
#define COM_HVPSU2D_STATE_ERR_ILOCK      0x8004                           /* Interlock error            */
#define COM_HVPSU2D_STATE_ERR_PSU_DIS    0x8005                           /* PSUs disabled              */
###int FAR _export COM_HVPSU2D_GetMainState (WORD PortNumber, WORD & State); /* Get the main device status */

#define COM_HVPSU2D_DEVST_OK                  0      /* No error detected          */
#define COM_HVPSU2D_DEVST_VCPU_FAIL   (1UL << 0x00)  /* CPU supply voltage failed  */
#define COM_HVPSU2D_DEVST_VFAN_FAIL   (1UL << 0x01)  /* Fan supply voltage failed  */
#define COM_HVPSU2D_DEVST_VPSU0_FAIL  (1UL << 0x02)  /* PSU #0 failed              */
#define COM_HVPSU2D_DEVST_VPSU1_FAIL  (1UL << 0x03)  /* PSU #1 failed              */
#define COM_HVPSU2D_DEVST_FAN1_FAIL   (1UL << 0x08)  /* Fan #1 failed              */
#define COM_HVPSU2D_DEVST_FAN2_FAIL   (1UL << 0x09)  /* Fan #2 failed              */
#define COM_HVPSU2D_DEVST_FAN3_FAIL   (1UL << 0x0A)  /* Fan #3 failed              */
#define COM_HVPSU2D_DEVST_PSU_DIS     (1UL << 0x0F)  /* PSUs disabled              */
#define COM_HVPSU2D_DEVST_SEN1_HIGH   (1UL << 0x10)  /* Temperature sensor #1 hot  */
#define COM_HVPSU2D_DEVST_SEN2_HIGH   (1UL << 0x11)  /* Temperature sensor #2 hot  */
#define COM_HVPSU2D_DEVST_SEN3_HIGH   (1UL << 0x12)  /* Temperature sensor #3 hot  */
#define COM_HVPSU2D_DEVST_SEN1_LOW    (1UL << 0x18)  /* Temperature sensor #1 cold */
#define COM_HVPSU2D_DEVST_SEN2_LOW    (1UL << 0x19)  /* Temperature sensor #2 cold */
#define COM_HVPSU2D_DEVST_SEN3_LOW    (1UL << 0x1A)  /* Temperature sensor #3 cold */
###int FAR _export COM_HVPSU2D_GetDeviceState (WORD PortNumber, DWORD & DeviceState); /* Get the device status         */

###int FAR _export COM_HVPSU2D_GetHousekeeping (WORD PortNumber, double & VoltRect, double & Volt5V0, double & Volt3V3, double & TempCPU); /* Get the housekeeping data */

#define COM_HVPSU2D_SEN_NEG    0 /* number of the sensor of the negative PSU    */
#define COM_HVPSU2D_SEN_MID    1 /* number of the middle sensor (liquid coller) */
#define COM_HVPSU2D_SEN_POS    2 /* number of the sensor of the positive PSU    */
#define COM_HVPSU2D_SEN_COUNT  3 /* Number of implemented sensors               */
###int FAR _export COM_HVPSU2D_GetSensorData (WORD PortNumber, double Temperature [COM_HVPSU2D_SEN_COUNT]); /* Get sensor data               */

#define COM_HVPSU2D_FAN_COUNT       3                                                                                              /* Number of implemented fans */
#define COM_HVPSU2D_FAN_PWM_MAX  1000                                                                                              /* Maximum PWM value (100%)   */
###int FAR _export COM_HVPSU2D_GetFanData (WORD PortNumber, BOOL Enabled [COM_HVPSU2D_FAN_COUNT], BOOL Failed [COM_HVPSU2D_FAN_COUNT],
	WORD SetRPM [COM_HVPSU2D_FAN_COUNT], WORD MeasuredRPM [COM_HVPSU2D_FAN_COUNT], WORD PWM [COM_HVPSU2D_FAN_COUNT]);                /* Get fan data               */

###int FAR _export COM_HVPSU2D_GetLEDData (WORD PortNumber, BOOL & Red, BOOL & Green, BOOL & Blue);                                   /* Get LED data */


/* PSU Management */

#define COM_HVPSU2D_PSU_POS  0 /* number of the positive PSU */
#define COM_HVPSU2D_PSU_NEG  1 /* number of the negative PSU */
#define COM_HVPSU2D_PSU_NUM  2 /* total number of the PSUs   */

/* PSU monitoring */
###int FAR _export COM_HVPSU2D_GetADCHousekeeping (WORD PortNumber, unsigned PSU, double & VoltAVDD, double & VoltDVDD, double & VoltALDO, double & VoltDLDO, double & VoltRef, double & TempADC); /* Get ADC housekeeping data */
###int FAR _export COM_HVPSU2D_GetPSUHousekeeping (WORD PortNumber, unsigned PSU, double & Volt24Vp, double & Volt12Vp, double & Volt12Vn, double & VoltRef);                                      /* Get PSU housekeeping data */
###int FAR _export COM_HVPSU2D_GetPSUData         (WORD PortNumber, unsigned PSU, double & Voltage,  double & Current,  double & VoltDropout);                                                     /* Get measured PSU values   */

/* PSU control */
###int FAR _export COM_HVPSU2D_SetPSUOutputVoltage    (WORD PortNumber, unsigned PSU, double   Voltage);                           /* Set PSU output voltage             */
###int FAR _export COM_HVPSU2D_GetPSUOutputVoltage    (WORD PortNumber, unsigned PSU, double & Voltage);                           /* Get PSU output voltage             */
###int FAR _export COM_HVPSU2D_GetPSUSetOutputVoltage (WORD PortNumber, unsigned PSU, double & VoltageSet, double & VoltageLimit); /* Get PSU set & limit output voltage */

###int FAR _export COM_HVPSU2D_SetPSUOutputCurrent    (WORD PortNumber, unsigned PSU, double   Current);                           /* Set PSU output current             */
###int FAR _export COM_HVPSU2D_GetPSUOutputCurrent    (WORD PortNumber, unsigned PSU, double & Current);                           /* Get PSU output current             */
###int FAR _export COM_HVPSU2D_GetPSUSetOutputCurrent (WORD PortNumber, unsigned PSU, double & CurrentSet, double & CurrentLimit); /* Get PSU set & limit output current */

/* PSU configuration */
int FAR _export COM_HVPSU2D_SetPSUEnable (WORD PortNumber, BOOL   PSU0, BOOL   PSU1); /* Set PSU enable */
int FAR _export COM_HVPSU2D_GetPSUEnable (WORD PortNumber, BOOL & PSU0, BOOL & PSU1); /* Get PSU enable */

int FAR _export COM_HVPSU2D_HasPSUFullRange (WORD PortNumber, BOOL & PSU0, BOOL & PSU1); /* Get PSU range-switching implementation */
int FAR _export COM_HVPSU2D_SetPSUFullRange (WORD PortNumber, BOOL   PSU0, BOOL   PSU1); /* Set PSU full range                     */
int FAR _export COM_HVPSU2D_GetPSUFullRange (WORD PortNumber, BOOL & PSU0, BOOL & PSU1); /* Get PSU full range                     */

/* COM_HVPSU2D_SetPSUConfig bits */
#define COM_HVPSU2D_ST_ILIM_CTRL       (1UL <<  0) /* activate current limiter (signal I_limit)                                           */
#define COM_HVPSU2D_ST_LED_CTRL_R      (1UL <<  1) /* red   LED (negated output signal LED_Rn)                                            */
#define COM_HVPSU2D_ST_LED_CTRL_G      (1UL <<  2) /* green LED (negated output signal LED_Gn)                                            */
#define COM_HVPSU2D_ST_LED_CTRL_B      (1UL <<  3) /* blue  LED (negated output signal LED_Bn)                                            */
#define COM_HVPSU2D_ST_PSU0_ENB_CTRL   (1UL <<  4) /* enable PSU #0                                                                       */
#define COM_HVPSU2D_ST_PSU1_ENB_CTRL   (1UL <<  5) /* enable PSU #1                                                                       */
#define COM_HVPSU2D_ST_PSU0_FULL_CTRL  (1UL <<  6) /* full range of PSU #0 (signal PSU_Full_A)                                            */
#define COM_HVPSU2D_ST_PSU1_FULL_CTRL  (1UL <<  7) /* full range of PSU #1 (signal PSU_Full_B)                                            */
#define COM_HVPSU2D_ST_ILOCK_OUT_DIS   (1UL <<  8) /* disable interlock at output connector                                               */
#define COM_HVPSU2D_ST_ILOCK_BNC_DIS   (1UL <<  9) /* disable interlock at  BNC   connector                                               */
#define COM_HVPSU2D_ST_PSU_ENB_CTRL    (1UL << 10) /* enable PSUs                                                                         */
#define COM_HVPSU2D_ST_ILIM_ACT        (1UL << 12) /* state of the current limiter output (signal I_limit), R/O                           */
#define COM_HVPSU2D_ST_PSU0_FULL_ACT   (1UL << 13) /* state of the full range of PSU #0 output (signal PSU_Full_A), R/O                   */
#define COM_HVPSU2D_ST_PSU1_FULL_ACT   (1UL << 14) /* state of the full range of PSU #1 output (signal PSU_Full_B), R/O                   */
#define COM_HVPSU2D_ST_RES_N           (1UL << 15) /* state of the reset signal RESn, if 0, device is reset, R/O                          */
#define COM_HVPSU2D_ST_ILOCK_OUT_ACT   (1UL << 16) /* state of interlock at output connector, i.e. ST_ILOCK_CN  | ST_ILOCK_CN_DIS  active */
#define COM_HVPSU2D_ST_ILOCK_BNC_ACT   (1UL << 17) /* state of interlock at  BNC   connector, i.e. ST_ILOCK_BNC | ST_ILOCK_BNC_DIS active */
#define COM_HVPSU2D_ST_ILOCK_ACT       (1UL << 18) /* interlock state, i.e. ST_ILOCK_CN_ACT & ST_ILOCK_BNC_ACT active                     */
#define COM_HVPSU2D_ST_PSU_ENB_ACT     (1UL << 19) /* PSUs enabled, i.e. ST_PSU_ENB & ST_ILOCK active                                     */
#define COM_HVPSU2D_ST_PSU0_ENB_ACT    (1UL << 20) /* PSU #0 enabled, i.e. ST_PSU_ENB & ST_PSU_ENB_A active (output signal INT_A)         */
#define COM_HVPSU2D_ST_PSU1_ENB_ACT    (1UL << 21) /* PSU #1 enabled, i.e. ST_PSU_ENB & ST_PSU_ENB_B active (output signal INT_B)         */
#define COM_HVPSU2D_ST_ILOCK_OUT       (1UL << 22) /* interlock at output connector (input signal Interlock_CN)                           */
#define COM_HVPSU2D_ST_ILOCK_BNC       (1UL << 23) /* interlock at  BNC   connector (input signal Interlock_BNC)                          */
int FAR _export COM_HVPSU2D_GetPSUState (WORD PortNumber, DWORD & Status); /* Get PSU state */


/* Configuration management */

int FAR _export COM_HVPSU2D_GetDeviceEnable (WORD PortNumber, BOOL & Enable); /* Get the enable state of the device */
int FAR _export COM_HVPSU2D_SetDeviceEnable (WORD PortNumber, BOOL   Enable); /* Set the enable state of the device */

int FAR _export COM_HVPSU2D_ResetCurrentConfig (WORD PortNumber); /* Reset current configuration */

#define COM_HVPSU2D_MAX_CONFIG  168
int FAR _export COM_HVPSU2D_SaveCurrentConfig (WORD PortNumber, unsigned ConfigNumber); /* Save current configuration to NVM   */
int FAR _export COM_HVPSU2D_LoadCurrentConfig (WORD PortNumber, unsigned ConfigNumber); /* Load current configuration from NVM */

#define COM_HVPSU2D_CONFIG_NAME_SIZE  75
int FAR _export COM_HVPSU2D_GetConfigName  (WORD PortNumber, unsigned ConfigNumber,       char Name [COM_HVPSU2D_CONFIG_NAME_SIZE]); /* Get configuration name */
int FAR _export COM_HVPSU2D_SetConfigName  (WORD PortNumber, unsigned ConfigNumber, const char Name [COM_HVPSU2D_CONFIG_NAME_SIZE]); /* Set configuration name */

int FAR _export COM_HVPSU2D_GetConfigFlags (WORD PortNumber, unsigned ConfigNumber, bool & Active, bool & Valid); /* Get configuration flags */
int FAR _export COM_HVPSU2D_SetConfigFlags (WORD PortNumber, unsigned ConfigNumber, bool   Active, bool   Valid); /* Set configuration flags */

int FAR _export COM_HVPSU2D_GetConfigList  (WORD PortNumber, bool Active [COM_HVPSU2D_MAX_CONFIG], bool Valid [COM_HVPSU2D_MAX_CONFIG]); /* Get configuration list */


/* System */

###int FAR _export COM_HVPSU2D_Restart      (WORD PortNumber);                                                       /* Restart the controller                     */
###int FAR _export COM_HVPSU2D_GetCPUData   (WORD PortNumber, double & Load, double & Frequency);                    /* Get CPU load (0-1) and frequency (Hz)      */
int FAR _export COM_HVPSU2D_GetUptime    (WORD PortNumber, DWORD & Seconds, WORD & Milliseconds, DWORD & Optime); /* Get device uptime and operation time       */
int FAR _export COM_HVPSU2D_GetTotalTime (WORD PortNumber, DWORD & Uptime,                       DWORD & Optime); /* Get total device uptime and operation time */

int FAR _export COM_HVPSU2D_GetHWType    (WORD PortNumber, DWORD & HWType   ); /* Get the hardware type    */
int FAR _export COM_HVPSU2D_GetHWVersion (WORD PortNumber, WORD  & HWVersion); /* Get the hardware version */

int FAR _export COM_HVPSU2D_GetFWVersion (WORD PortNumber, WORD & Version);            /* Get the firmware version                                                             */
int FAR _export COM_HVPSU2D_GetFWDate    (WORD PortNumber, char * FAR DateString);     /* Get the firmware date, DateString should be at least 16 characters long              */
int FAR _export COM_HVPSU2D_GetProductID (WORD PortNumber, char * FAR Identification); /* Get the product identification, Identification should be at least 60 characters long */
int FAR _export COM_HVPSU2D_GetProductNo (WORD PortNumber, DWORD & Number);            /* Get the product number                                                               */


/* Communication port */

int          FAR _export COM_HVPSU2D_GetInterfaceState (WORD PortNumber); /* Get software interface state                                           */
const char * FAR _export COM_HVPSU2D_GetErrorMessage   (WORD PortNumber); /* Get the error message corresponding to the software interface state    */
int          FAR _export COM_HVPSU2D_GetIOState        (WORD PortNumber); /* Get serial port interface state                                        */
const char * FAR _export COM_HVPSU2D_GetIOErrorMessage (WORD PortNumber); /* Get the error message corresponding to the serial port interface state */

#ifdef __cplusplus
}
#endif


#endif//__COM_HVPSU2D_H__
