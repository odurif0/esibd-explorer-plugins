# TPG366 Plugin

USB pressure acquisition for the Pfeiffer MaxiGauge TPG 366, with six channels.
Requires ESIBD Explorer 1.0.2 and its existing `pyserial` dependency; no vendor DLL.

## Native Worker

Production USB communication runs in an isolated native Rust worker on Windows
x86-64 (`native/esibd-tpg366-worker.exe`) or Linux x86-64
(`native/esibd-tpg366-worker`). The supervisor selects that exact plugin-local
binary from `native/manifest.json` and verifies its SHA-256, family and protocol.
Missing or mismatched files fail startup; no worker is searched on PATH.
The Explorer GUI and data/exception adapter remain Python; the Python ASCII
protocol implementation is retained as a test/notebook reference, not a
production fallback. No external or private Python worker interpreter or
Explorer fork is required.

OFF/close cancels communication and can terminate and reap an unresponsive
worker. A crash or hang is isolated to this plugin; other workers keep running.
After retirement, only an explicit ON reconnects, without restarting Explorer
or replaying old requests. Port closure or worker exit does not mean that the
physical gauges are OFF; this plugin never sends gauge-OFF commands.
Worker logs are under
`<Explorer data path>/logs/tpg366/native/tpg366/`, never inside the plugin.
The v0.5 native port has local mock-serial, Wine and Explorer 1.0.2 software
validation. Real-hardware qualification remains pending; complete it before
routine experimental use. Software checks do not qualify the physical gauges.

## Use

1. Copy the plugin into Explorer's plugin directory; keep the whole `tpg366/` directory
   together, including `_runtime/` and `native/`.
   Enable the `TPG366` plugin in the Plugin Manager.
2. Connect the controller's rear **USB-B** socket to the PC. Select its COM port
   in **Settings → TPG366**: both `21` and `COM21` select Windows port COM21.
   On Linux, use the USB serial device path (for example `/dev/ttyUSB0`) with
   permission to access it. If Windows does not expose a COM port, install the
   FTDI virtual COM-port driver described in the operating manual.
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
  hPa/mbar, Torr, Pa or micron. Voltage units are rejected. `UNI` is read at
  connection and after each `PRX` packet (two transactions per poll); a unit
  differing from the previous reading discards the frame and stops acquisition.
- Each six-channel packet is recorded once, timestamped on receipt by the PC.
  Curves use Explorer's logarithmic pressure display and standard data export.
- Underrange, overrange, sensor errors, switched-off or missing gauges, and
  identification errors appear separately in **Status**, with **NaN** data
  rather than zero or an old pressure. Other valid channels keep updating.
- A command answered with NAK is retransmitted, as the TPG 366 protocol specifies
  (at most twice; each NAK is logged). A persistent NAK, a timeout or a corrupt
  frame costs that sample only: readings show NaN,
  the lost packet is recorded as a gap, and the plugin resynchronizes with the
  same read-only handshake as ON (ETX, `AYT`, `TID`; same controller required).
  Three consecutive failed transactions, a unit change, an unsupported unit or
  an open/close error stop acquisition. Previously recorded data is retained.
  ON reconnects after a confirmed close. The pressure–temperature notebook keeps
  its stricter rule: the first pressure read error stops its heating protocol.
- Explorer's test mode generates explicitly labelled **simulation** data without
  opening a serial port. Disable test mode for real measurements.

## Protocol and validation

Based on Pfeiffer **BG 5511 BEN /A (2022-07)**, firmware reference **V010300**:
[communication protocol](https://www.idealvac.com/files/manuals/Pfeiffer_MaxiGauge_TPG366_Communication_Protocol.pdf.pdf)
and [operating manual](https://www.idealvac.com/files/manuals/Pfeiffer_MaxiGauge_TPG366_Operating_Instructions.pdf).

The driver uses read-only `AYT`, `TID`, `UNI` and `PRX` queries, validates ACK,
then sends ENQ without an appended CR. ETX resets only the communication parser
at connection; the driver then waits 0.2 s before `AYT`, because ETX clears the
controller's input buffer and could also clear a command sent right behind it. Protocol, shutdown races, histories, export and real Qt widgets
are tested with a simulated instrument. **Validation on a physical TPG 366 is
still required.**
