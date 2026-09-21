# AMX_B Plugin

Drives AMX timing from ESIBD Explorer and shows the expected signals on
CH0–CH3 from controller-register readbacks.

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
5. Enable the `AMX_B` plugin in the Plugin Manager.

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

- `Signal`: saved AMX configuration selector (routing and timing).
- `Load now`: immediately loads the selected signal while the AMX is ON.
- `Osc`: internal oscillator frequency in kHz, not necessarily each output's
  frequency. Validate typed changes with Enter, Tab,
  or a click outside the field. They apply while ON, until the next config load.
- `Advanced`: pulser enable/width controls, equations and channel metadata.

ON and `Load now` load the selected configuration's saved timing and update
frequency, widths and enable requests from the hardware readback. Previous
setpoints and unfinished edits are replaced, not sent back to the device.
Channels return to manual mode; equation text is retained for explicit reuse.

## Operator Panel

The default view is a compact CH0–CH3 table: signal type, frequency, rail
selection, time at Vpos/Vneg, and timing relative to the first periodic output.
For the original config 79, all four outputs switch at 500 kHz with 1 µs at
each level: CH0/CH2 together, CH1/CH3 opposite. P1–P3 being stopped does not
mean that CH1–CH3 are disabled. Stopping P0 can leave outputs at a fixed rail.

These are **expected dual-level switch signals**, not measured waveforms.
Vpos/Vneg voltage values are **unknown**: the AMX API reports internal supply
rails, not the external HV amplitudes. On trilevel hardware, each pair CH0/1
or CH2/3 controls one physical output; the dual-level voltage interpretation
does not apply. See the [CGC controller manual](https://www.cgc-instruments.com/en/Products/Switches/19AMX/Modules/AMX-CTRL-4ED), pp. 14–15 and 20–27.

The view uses live trigger/enable selections, pulser sources, controller flags
and mapping-enable readbacks, never the config name or slot number. External
signals, bursts, chained triggers, active/missing mapping and potentially
skipped triggers remain explicitly unknown rather than inheriting oscillator
frequency. Nonzero or unread switch edge delays suppress exact dwell/phase
claims. Failed reads, writes awaiting readback, transitions and disconnection
invalidate the displayed signal. Hi-Z does not mean 0 V or a discharged output.

`Advanced` contains P0–P3 controls and the channel table within the same
scrollable panel. Cards separate requests from register readbacks; a green
`REGISTERS APPLIED` confirms matching registers, not a physical waveform.
Typed widths wait for Enter, Tab or focus-out; scrolling never edits them.
Equation-controlled widths are read-only in the cards. The switch delay
register range remains `0..15`; values outside it are rejected.

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

The Advanced view and recorded data keep the fixed P0–P3 pulser layout.
Each pulser exposes:

- requested pulse width in microseconds
- duty request calculated from the width and oscillator frequency
- pulser delay in ticks
- channel enable request used when the global AMX state is ON
- duty calculated from controller period/width register readbacks
- width, delay, and burst register readbacks

Switch topology and routing remain managed by the saved AMX controller
configurations. The plugin focuses on the runtime timing adjustments typically
changed between experiments.

## Process Backend

The bundled runtime runs inside Explorer. Process isolation is disabled:
a spawned interpreter cannot import the private bundled modules. After a DLL
timeout, the connection cannot be reused. Switch the instrument OFF using its
hardware controls before restarting Explorer.

## Portability Note

To copy this plugin to another machine, keep the whole `amx_b/` directory
together, including the embedded `vendor/` subtree.
