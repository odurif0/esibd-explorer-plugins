# AMPR_A Plugin

Drives AMPR_A high-voltage channels and monitors measured output voltages.

The plugin is self-contained: it bundles its native Rust worker, Python GUI
adapters and reference driver files, and the AMPR vendor DLL.

## Requirements

- ESIBD Explorer `1.0.2`
- Windows x86-64 for real vendor-DLL hardware communication
- No separate `ESIBD_core` installation is required for the plugin itself

## Activation

1. Open ESIBD Explorer.
2. Download the plugin bundle from the [Releases page](https://github.com/odurif0/esibd-explorer-plugins/releases)
   and extract it into your ESIBD Explorer `plugins` folder.
3. Set the Explorer `plugin path` to that `plugins` folder.
4. Restart ESIBD Explorer.
5. Enable the `AMPR_A` plugin in the Plugin Manager.

## Native Worker

Production communication runs in this plugin's separate native Rust process,
`native/esibd-ampr-worker.exe` (Windows x86-64). The supervisor selects
that exact plugin-local file from `native/manifest.json` and verifies its
SHA-256, family and protocol before use. Missing or mismatched files fail
startup; workers are not searched on PATH.

The Explorer GUI and thin facade adapters remain Python. The older Python
device implementation is retained as a reference for tests and notebooks,
not as a production fallback. No external or bundled private Python worker
interpreter, Explorer fork or separate `ESIBD_core` installation is required.
Each plugin instance owns its worker; no running worker is shared with siblings.

Worker logs are written under
`<Explorer data path>/logs/ampr_a/native/ampr/`, never inside the plugin.
The v0.5 native port has local mock-DLL, Wine and Explorer 1.0.2 software
validation. Real-hardware qualification remains pending; complete it before
routine experimental use. These checks do not certify physical shutdown.

## Device Configuration

- `COM`: Windows COM port number used by the AMPR controller.
- `Baud rate`: serial speed passed to the AMPR driver.
- `Connect timeout (s)`: timeout used during controller connection.

Each real channel must be configured with:

- `Module`: AMPR module address from `0` to `11`
- `CH`: channel number from `1` to `4`
- `Ramp (V/s)`: this channel's speed for global ON/OFF ramps, saved with its
  configuration; default **10 V/s**, or **0** for no ramp on this channel

The plugin reads measured voltages as channel monitors and applies channel
setpoints through the AMPR driver.

Validate a voltage edit with Enter, Tab, or a click outside the cell. Updates
from other channels do not validate an unfinished edit. An explicit ON action
uses the current field text even if its button does not remove keyboard focus.
Communication runs in the background; only the latest unsent request per
channel is kept. OFF cancels pending requests without submitting a new target.

While a channel is ON, an orange voltage cell means pending or awaiting
hardware readback. Red means an error or a different readback; the tooltip
gives details. Revalidate to retry. `Monitor` is the measured output voltage.
The first column, `Status`, keeps its ON/OFF button and shows voltage tracking:
green within 1% of the target, orange within 10%, red beyond (1 V reference
floor). During global ramp-down the reference is zero, not the saved target.
The coloured feedback is on `Status`, not `Monitor`; `Ramp (V/s)` is between
`Monitor` and `Min`. **OFF cells are neutral**, without changing plot colours.

Hardware voltages and `Status` are refreshed every second, including during
both ramps. Ramp commands and readings use the same serialized worker; slow
hardware responses can reduce the refresh rate. The Explorer acquisition
interval controls recording, not this monitoring cadence.

All channels ramp **in parallel**, each at its own speed; serial commands are
sent in turn. At 10 V/s, targets of 100 V and 200 V from zero take approximately
10 s and 20 s, not a shared duration or two sequential ramps. Speeds are captured
at the start of each transition. Older channel files without a speed inherit
the previous global ramp setting.

OFF during startup or ramp-up interrupts the ascent after the current hardware
call, then ramps down from the last accepted/read-back targets before verified
shutdown. It never raises a channel to its unfinished target before stopping.
A further click on ON/OFF (or another OFF) **during the ramp-down** skips the
rest of the descent: the verified shutdown then sets every channel to 0 V and
disables the PSU at once. Such a click never turns the outputs back on.

If the initial port opening fails, shutdown or closing communication cleans
up that opening without output commands. `Connection pending` stays visible
until its port cleanup or worker retirement is confirmed. This is port
cleanup, not confirmation of the HV output state. The backend and port
reservation are retained until cleanup or worker retirement; a new ON explicitly
reconnects afterwards. Failed-Open cleanup is not used for an operational timeout.
Crashes or hung DLL calls are isolated to this plugin's worker. OFF/close can
cancel, terminate and reap it without stopping other workers; a fresh explicit
ON then creates a new worker without an Explorer restart or replay of old writes.
Worker termination proves neither hardware OFF nor electrical discharge: use
the hardware interlock/front panel when shutdown cannot be verified.
If Open succeeds after a close request, normal verified shutdown runs instead
of completing initialization.

Once connected, a failed startup or ramp requires verified disable before
closing the port. If this fails, `Shutdown unconfirmed` stays visible and the next
click retries OFF; the button's ON state does not confirm an active output.
A confirmed OFF does not certify complete electrical discharge.

## Portability Note

To copy this plugin to another machine, keep the whole `ampr_a/` directory
together, including the bundled `vendor/` and `native/` subtrees.
