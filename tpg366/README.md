# TPG366 Plugin

USB pressure acquisition for the Pfeiffer MaxiGauge TPG 366, with six channels.
Requires ESIBD Explorer 1.0.1 and its existing `pyserial` dependency; no vendor DLL.

## Use

1. Copy the plugin into Explorer's plugin directory; keep the whole `tpg366/` directory
   together. Enable the `TPG366` plugin in the Plugin Manager.
2. Connect the controller's rear **USB-B** socket to the PC. Select its COM port
   in **Settings → TPG366**: both `21` and `COM21` select Windows port COM21.
   If Windows does not expose a COM port, install the FTDI virtual COM-port
   driver described in the operating manual.
3. Match **Baud rate** to the controller's USB setting (default: **9600**, 8N1).
4. Press **ON**. The six inputs appear as **P1–P6**; rename them as needed.
   The default interval is **1000 ms**, with all six inputs read per cycle.

The six pressure cards show scientific values in mbar, gauge type and exact
status. Edit their labels directly. **Read** excludes or includes a channel in
acquisition data; **Display** only hides or shows its curve. Neither switches a
physical gauge. Invalid, excluded and disconnected readings display a dash,
never the last pressure. Channel configuration and exports keep the same format.

**ON** connects and starts recording. **OFF** stops recording and closes USB.
It does **not** switch gauges, degas, change calibration, units, filters or relays.
Explorer's recording/pause control can pause storage while keeping live readings.
A failed port close leaves **Disconnect unconfirmed**; click OFF again to retry.

## Data

- Pressures are converted to **mbar**, regardless of whether the controller uses
  hPa/mbar, Torr, Pa or micron. Voltage units are rejected. `UNI` is read before
  and after each `PRX` packet; a detected unit change stops acquisition.
- Each six-channel packet is recorded once, timestamped on receipt by the PC.
  Curves use Explorer's logarithmic pressure display and standard data export.
- Underrange, overrange, sensor errors, switched-off or missing gauges, and
  identification errors appear separately in **Status**, with **NaN** data
  rather than zero or an old pressure. Other valid channels keep updating.
- USB/protocol errors invalidate current readings and stop acquisition.
  Previously recorded data is retained. ON reconnects after a confirmed close.
- Explorer's test mode generates explicitly labelled **simulation** data without
  opening a serial port. Disable test mode for real measurements.

## Protocol and validation

Based on Pfeiffer **BG 5511 BEN /A (2022-07)**, firmware reference **V010300**:
[communication protocol](https://www.idealvac.com/files/manuals/Pfeiffer_MaxiGauge_TPG366_Communication_Protocol.pdf.pdf)
and [operating manual](https://www.idealvac.com/files/manuals/Pfeiffer_MaxiGauge_TPG366_Operating_Instructions.pdf).

The driver uses read-only `AYT`, `TID`, `UNI` and `PRX` queries, validates ACK,
then sends ENQ without an appended CR. ETX resets only the communication parser
at connection. Protocol, shutdown races, histories, export and real Qt widgets
are tested with a simulated instrument. **Validation on a physical TPG 366 is
still required.**
