# DMMR Plugin

Reads DMMR module currents and monitors live picoammeter measurements.

The plugin is self-contained: it embeds the minimal private runtime it needs,
including the DMMR driver files and vendor DLL.

## Requirements

- ESIBD Explorer `1.0.1`
- Windows for real hardware communication
- No separate `ESIBD_core` installation is required for the plugin itself

## Activation

1. Open ESIBD Explorer.
2. Download the plugin bundle from the [Releases page](https://github.com/odurif0/esibd-explorer-plugins/releases)
   and extract it into your ESIBD Explorer `plugins` folder.
3. Set the Explorer `plugin path` to that `plugins` folder.
4. Restart ESIBD Explorer.
5. Enable the `DMMR` plugin in the Plugin Manager.

The plugin lazily loads its bundled local `vendor/runtime` package under a
private Python module namespace when communication is initialized. If that
bundled copy is missing, the plugin fails explicitly because the installation
is incomplete.

## Device Configuration

- `COM`: Windows COM port number used by the DMMR controller.
- `Baud rate`: serial speed passed to the DMMR driver.
- `Connect timeout (s)`: timeout used during initialization and shutdown.
- `Poll timeout (s)`: timeout used for periodic state and current reads.

Each real channel must be configured with:

- `Module`: DMMR module address from `0` to `7`
- `Range mode`: `Auto` (default) or fixed range index `0`–`4`. Choose it in the
  module card while OFF. ON reads the hardware, writes only changed settings,
  and verifies the result. A receive error allows one checked recovery;
  unverified settings abort startup.

The plugin auto-discovers installed modules, creates one channel per detected
module, reads live current measurements as channel monitors, and exposes a
global ON/OFF control that enables or disables DMMR acquisition. Switching OFF
verifies that acquisition is disabled, closes the port, and reports
`Disconnected`. The next ON reconnects automatically. If the port cannot be
closed, the failure remains visible and the next click retries OFF.

After a failed acquisition start, OFF is shown only when both acquisition gates read back
disabled. Otherwise, `Shutdown unconfirmed` is displayed and the button stays
ON so the next click retries OFF; this does not mean acquisition is running.
Only a confirmed `ST_ON` allows acquisition. Undocumented state codes keep their
numeric value and an `UNKNOWN_STATE` label, not an assumed overload diagnosis.

Use `Display` to show or hide a module's time trace. The color square next to
it opens the color picker; the choice is saved in the channel configuration.
Automatic Y scaling also handles constant picoamp signals without changing
manual zoom. Measurements and recordings remain in amperes.

Compact module cards use as many columns as the panel can fit, with
scrollbars when space is limited. Edit the label below the module number;
Enter or leaving the field saves it, Escape cancels the edit. Labels are
stored by module address without renaming recorded channels.

`Used` shows the range returned with the current, not the requested range.
No full-scale values are inferred from these indices. Currents stay uncorrected:
no offset subtraction or filtering of range transitions is applied.

Time, current and range histories share the same capacity. At the storage
limit, all three are thinned together. HDF5 exports keep the existing current
channels and add `DMMR/Measurement ranges/<channel>`, aligned with each current
and timestamp. Dataset attributes link the three series. Unknown ranges and
missing readings are NaN; old recordings load without inventing their ranges.
Full-history and visible-window exports remain aligned with Explorer 1.0.1.

The recording timer no longer repeats a polling result when acquisition is
slower than recording; it leaves a NaN gap instead. Identical currents from
separate polling cycles are kept. This does **not** establish fresh ADC
conversions: manual polling has no conversion timestamp, and the firmware's
ready-flag behavior still needs a hardware check. Recorded times are Explorer
recording times, not simultaneous conversion times for all modules.

## Startup Diagnostics

If connection fails with the DMMR powered off, power it on and retry. The plugin
keeps the old attempt until its opening call returns and port closure is confirmed,
then permits a new connection. OFF, Disconnect and Explorer closure clean up only
that failed opening, without sending acquisition commands. Until closure is
confirmed, the backend remains reserved and `Connection pending` keeps the button
ON for another OFF attempt; this does not mean acquisition started. An OFF request
before the connection worker starts prevents opening; during opening it cancels
subsequent activation. Only a fresh ON rearms startup. `Disconnected` requires
confirmed closure of any opened port. A persistently blocked DLL or unconfirmed closure can still require
restarting Explorer. This recovery never applies to a timeout after opening.

Each ON attempt captures the native DLL startup exchanges, including failed-start
cleanup, into `esibd explorer.log` under `[DMMR startup]` / `[DMMR native]`.
To investigate an error, try ON once and share that Explorer log. No additional
tool or setting is needed.

Startup has one receive-error recovery budget for mode, range and final-state
checks. If readbacks fail to communicate, one purge restores the link and baud
rate before rechecking settings. Configuration writes are never replayed.
A lost enable ACK still triggers OFF, not another enable. Shutdown has its own
single recovery attempt and confirms both OFF flags before closing the port.

Capture stops before continuous polling. The raw file in `dmmr/logs/` is reused
on each attempt and trimmed to 64 KiB after closing. A blocked DLL is never
closed concurrently: any partial capture is reported, and Explorer must be
restarted. An unavailable capture is explicitly reported, not silently omitted.

## Read Errors

Returned receive errors (`−10` to `−13`) are logged before recovery. The driver
checks the controller, acquisition mode and ranges, with at most one serial
purge if needed. The failed reading is missing data, never a reused value.
A second error before valid replies from all modules, or a fourth incident
within 60 seconds, stops acquisition. A blocked DLL is never retried or closed
concurrently. This checks new replies, not fresh ADC conversions.

Keep `dmmr/logs/dmmr_protocol_com*.jsonl*` with the Explorer log. The protocol
log records clear-on-read port diagnostics before another transaction; it
rotates at 1 MB with three backups. `−13` alone does not identify automatic mode.

## Optional Zero Check

The [zero-check notebook](../notebooks/dmmr_zero_check.ipynb) records all eight
modules in fixed range 0 for two 6-hour runs, with a 5-minute disconnected pause
and temperature/diagnostic logging. Close Explorer before running it.
The notebook uses the same bounded recovery for range setup and current reads,
and saves protocol logs per series. Range writes are never replayed after an error.
Update both the notebook and the complete `dmmr/` folder, including `vendor/`.
Instructions and limits are in the notebook. Keep the whole output folder.
This characterizes offset, noise and drift; it does not calibrate the input or
change gain, bias or NVM. Record the input connection and shielding. Acceptance
limits require the applicable range/integration-time specification or CGC's
criteria; none is invented from the measured scatter.

The notebook lives in `notebooks/`, outside the plugin release ZIP.

## Portability Note

To copy this plugin to another machine, keep the whole `dmmr/` directory
together, including the embedded `vendor/` subtree.
