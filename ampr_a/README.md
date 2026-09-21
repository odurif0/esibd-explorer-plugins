# AMPR_A Plugin

Drives AMPR_A high-voltage channels and monitors measured output voltages.

The plugin is self-contained: it embeds the minimal private runtime it needs,
including the AMPR driver files and vendor DLL.

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
5. Enable the `AMPR_A` plugin in the Plugin Manager.

The plugin lazily loads its bundled local `vendor/runtime` package under a
private Python module namespace when communication is initialized. If that
bundled copy is missing, the plugin fails explicitly because the installation
is incomplete.

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

After a failed startup or ramp, the plugin verifies disable before closing
the port. If this fails, `Shutdown unconfirmed` stays visible and the next
click retries OFF; the button's ON state does not confirm an active output.
A confirmed OFF does not certify complete electrical discharge.

## Portability Note

To copy this plugin to another machine, keep the whole `ampr_a/` directory
together, including the embedded `vendor/` subtree.
