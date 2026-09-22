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
  module card while OFF. ON applies the saved choice and verifies its readback;
  a failed write or readback aborts startup and triggers verified shutdown.

The plugin auto-discovers installed modules, creates one channel per detected
module, reads live current measurements as channel monitors, and exposes a
global ON/OFF control that enables or disables DMMR acquisition. Switching OFF
verifies that acquisition is disabled, closes the port, and reports
`Disconnected`. The next ON reconnects automatically. If the port cannot be
closed, the failure remains visible and the next click retries OFF.

After a failed start, OFF is shown only when both acquisition gates read back
disabled. Otherwise, `Shutdown unconfirmed` is displayed and the button stays
ON so the next click retries OFF; this does not mean acquisition is running.

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

The recording timer no longer repeats a polling result when acquisition is
slower than recording; it leaves a NaN gap instead. Identical currents from
separate polling cycles are kept. This does **not** establish fresh ADC
conversions: manual polling has no conversion timestamp, and the firmware's
ready-flag behavior still needs a hardware check. Recorded times are Explorer
recording times, not simultaneous conversion times for all modules.

## Startup Diagnostics

Each ON attempt captures the native DLL startup exchanges, including failed-start
cleanup, into `esibd explorer.log` under `[DMMR startup]` / `[DMMR native]`.
To investigate an error, try ON once and share that Explorer log. No additional
tool or setting is needed; retries and serial timeouts are unchanged.

Capture stops before continuous polling. The raw file in `dmmr/logs/` is reused
on each attempt and trimmed to 64 KiB after closing. A blocked DLL is never
closed concurrently: any partial capture is reported, and Explorer must be
restarted. An unavailable capture is explicitly reported, not silently omitted.

## Optional Zero Check

[`dmmr_zero_check.ipynb`](dmmr_zero_check.ipynb) runs outside Explorer: close
Explorer first and check the notebook's COM port (default COM15). It records
all eight modules for 15 minutes in fixed range 0, without offset correction,
and reports time traces, per-minute means and standard deviations. A completed
run also reports the final five minutes, without assuming they are stable.

Keep `raw.csv`, `report.json` and `native_startup.log` from the run directory.
Open inputs can pick up interference; this check does not certify conformance
or test gain and linearity without a reference current source.

## Portability Note

To copy this plugin to another machine, keep the whole `dmmr/` directory
together, including the embedded `vendor/` subtree.
