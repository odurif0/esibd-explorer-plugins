# AMX Plugin

Drives AMX frequency and pulser timing from ESIBD Explorer and monitors live
pulser readbacks.

The plugin is self-contained: it embeds the minimal private runtime it needs,
including the AMX driver files and vendor DLL.

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
5. Enable the `AMX` plugin in the Plugin Manager.

## Device Configuration

- `COM`: Windows COM port number used by the AMX controller.
- `Baud rate`: serial speed passed to the AMX driver.
- `Connect timeout (s)`: timeout used to establish the transport.
- `Startup timeout (s)`: timeout used for ON/OFF startup and shutdown sequences.
- `Poll timeout (s)`: timeout used for periodic housekeeping reads.
- `Operating config`: operating slot loaded on ON. Use `-1` only when you
  want to connect first and choose a config later before enabling the AMX.
- `Available configs`: live list of config slots reported by the connected AMX.
- `Frequency (kHz)`: oscillator frequency. The same value is exposed directly
  in the plugin toolbar for routine operation.

Toolbar notes:

- `Signal`: saved AMX config selector. This is the operator-facing signal or
  routing shape.
- `Load now`: immediately loads the selected signal while the AMX is ON.
- `Freq`: oscillator frequency in kHz. Changes apply immediately while the AMX
  is ON and are reused on the next startup.
- channel rows: choose which pulsers are ON and set their pulse width in us.

Runtime timing notes:

- switch trigger `rise` and `fall` delays are coarse AMX controller ticks in
  the range `0..15`
- switch enable delay is also limited to `0..15`
- values outside that hardware range are rejected by the AMX wrapper on purpose

## AMX Configurations

AMX configuration slots are stored in the controller NVM and are specific to
the actual hardware and firmware content of that controller. Do not assume that
an index seen on another AMX, in an old notebook, or in a previous experiment
will exist on the current unit.

Important points:

- `Operating config` is effectively required to bring the AMX to `STATE_ON`.
- Set `Operating config = -1` only when you want the plugin to initialize
  communication first, inspect the available controller configs, and choose one
  before enabling the AMX.
- The signal shape/routing is intentionally not rebuilt from low-level switch
  controls in the plugin UI. It is chosen by loading a saved AMX config.
- The plugin `OFF` action disables or parks the controller with a confirmed
  shutdown path, then disconnects. There is no separate shutdown-config selector.
- Before choosing a config index, query the controller with the AMX wrapper
  notebook or `cgc.amx.AMX.list_configs()`.
- In the Python wrapper, `cgc.amx.AMX.initialize()` can be called without an
  explicit slot number. It will connect first and, when the controller exposes
  a valid config named `Standby`, auto-load that config into runtime memory.
- Do not hard-code slot `40`. On the AMX tested on April 14, 2026, slot `40`
  was not valid and `load_current_config(40)` failed with `-10`.

Example observed on one AMX controller on April 14, 2026:

- `0`: `Standby`
- `9`: `Static:Out0-3=Hi-Z`
- `10`: `Static:Out0-3=Vneg`
- `11`: `Static:Out0-3=Vpos`
- `19`: `Static:DIO0-DIO6=log0`
- `20`: `Static:DIO0-DIO6=log1`
- `21`: `1kHz->DIO0-DIO6`
- `29`: `DIO0->2xSwitchSym,DIO0->DIO1,1kHz->DIO2`
- `30`: `DIO0->Switch0-3,DIO0->DIO1,1kHz->DIO2`
- `39`: `1kHz->SwitchSym+DIO0,Osc->DIO1`
- `44`: `1kHz->SwitchSym(0/90deg)+DIO0/1,Osc->DIO2`
- `49`: `10kHz->SwitchSym+DIO0,Osc->DIO1`
- `59`: `100kHz->SwitchSym+DIO0,Osc->DIO1`
- `69`: `250kHz->SwitchSym+DIO0,Osc->DIO1`
- `74`: `400kHz->SwitchSym+DIO0,Osc->DIO1`
- `79`: `500kHz->SwitchSym+DIO0,Osc->DIO1`
- `89`: `1MHz->SwitchSym+DIO0,Osc->DIO1`
- `99`: `0.84MHz->SwitchSym+DIO0,Osc->DIO1`
- `101`: `1.2MHz->SwitchSym+DIO0,Osc->DIO1`
- `102`: `1.5MHz->SwitchSym+DIO0,Osc->DIO1`
- `103`: `2MHz->SwitchSym+DIO0,Osc->DIO1`
- `104`: `3MHz->SwitchSym+DIO0,Osc->DIO1`
- `105`: `4MHz->SwitchSym+DIO0,Osc->DIO1`
- `106`: `5MHz->SwitchSym+DIO0,Osc->DIO1`
- `109`: `5000x1MHz->SwitchSym+DIO0,Pause->Vneg+DIO1`

The plugin keeps a fixed 4-channel pulser layout matching the AMX hardware.
Each channel exposes:

- duty-cycle setpoint in percent
- pulser delay in ticks
- channel ON/OFF state for whether the pulser is actively applied
- measured duty-cycle monitor
- width and burst readbacks

Switch topology and routing remain managed by the saved AMX controller
configurations. The plugin focuses on the runtime timing adjustments typically
changed between experiments.

## Process Backend

The embedded AMX runtime now defaults to the inline controller path. This
avoids the repeated "worker timed out during worker startup" warnings seen on
some Explorer deployments. Process isolation remains available for debugging or
special cases by constructing the AMX runtime with `process_backend=True`.

## Portability Note

To copy this plugin to another machine, keep the whole `amx/` directory
together, including the embedded `vendor/` subtree.
