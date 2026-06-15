# AMX HD Plugin

Drives the **AMX HD** (CGC `HV-AMX-CTRL-4EDH`, High-Definition) oscillator and
timer timing from ESIBD Explorer and monitors live timer readbacks.

This is the HD sibling of the `amx/` plugin. The HD variant is a **different
controller** than the normal AMX: it ships its own vendor DLL
(`COM-HVAMX4EDH.dll`), uses a stream-based API, exposes **timers** instead of
pulsers, has **500 configuration slots** (vs 126), an **8-value housekeeping**
readback, and a distinct state encoding (`STATE_ON = 0x0001`, with a new
`STATE_STANDBY = 0x0000`). See the plan/audit notes for the full normal-vs-HD
delta.

The plugin is self-contained: it embeds the minimal private runtime it needs,
including the AMX HD driver files and the vendor `COM-HVAMX4EDH.dll`.

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
5. Enable the `AMX_HD` plugin in the Plugin Manager.

## Device Configuration

- `COM`: Windows COM port number used by the AMX HD controller.
- `Baud rate`: serial speed passed to the AMX HD driver (default 230400).
- `Connect timeout (s)`: timeout used to establish the transport.
- `Startup timeout (s)`: timeout used for ON/OFF startup and shutdown sequences.
- `Poll timeout (s)`: timeout used for periodic housekeeping reads.
- `Operating config`: operating slot loaded on ON (0..499). Use `-1` only when
  you want to connect first and choose a config later before enabling the device.
- `Available configs`: live list of config slots reported by the connected HD.
- `Frequency (kHz)`: oscillator-0 frequency. The same value is exposed directly
  in the plugin toolbar for routine operation.

Toolbar notes:

- `Signal`: saved AMX HD config selector (operator-facing signal/routing shape).
- `Load now`: immediately loads the selected signal while the device is ON.
- `Freq`: oscillator frequency in kHz. Changes apply immediately while the
  device is ON and are reused on the next startup.
- channel rows: choose which timers are ON and set their pulse width in us.

Runtime timing notes:

- HD timer delay/width share the same register offsets as the normal AMX
  pulsers (delay `+3`, width `+2` ticks, 100 MHz / 10 ns tick on oscillator-0).
- HD per-switch **fine delays** (~11 ps) and switch source selection exist in
  the driver but are **not exposed in this v1 plugin** (parity with the normal
  AMX plugin). They can be added in a follow-up.

## AMX HD Configurations

AMX HD configuration slots are stored in the controller NVM (up to **500**
slots) and are specific to the actual hardware and firmware content of that
controller. Do not assume that an index seen on another unit, in an old
notebook, or on a normal AMX will exist on this HD controller.

Important points:

- `Operating config` is effectively required to bring the HD to `STATE_ON`
  (`0x0001`). Note: `0x0000` is `STATE_STANDBY` on HD (not ON as on the normal
  AMX).
- Set `Operating config = -1` only when you want the plugin to initialize
  communication first, inspect the available controller configs, and choose one
  before enabling the device.
- The signal shape/routing is intentionally not rebuilt from low-level switch
  controls in the plugin UI. It is chosen by loading a saved AMX HD config.
- The plugin `OFF` action disables or parks the controller with a confirmed
  shutdown path, then disconnects.
- Before choosing a config index, query the controller with the AMX HD wrapper
  notebook or `list_configs()`.

The plugin queries the timer count at runtime (`get_timer_count()`); it does
not hard-code a channel count. Each timer channel exposes:

- duty-cycle setpoint in percent
- timer delay in ticks
- channel ON/OFF state for whether the timer is actively applied
- measured duty-cycle monitor
- width and burst readbacks

## Process Backend

The embedded AMX HD runtime defaults to the inline controller path (consistent
with the normal AMX plugin), avoiding "worker timed out during worker startup"
warnings seen on some Explorer deployments. Process isolation remains available
for debugging or special cases by constructing the AMX HD runtime with
`process_backend=True`.

## Portability Note

To copy this plugin to another machine, keep the whole `amx_hd/` directory
together, including the embedded `vendor/` subtree and the
`COM-HVAMX4EDH.dll`.
