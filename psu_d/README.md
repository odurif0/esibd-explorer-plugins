# PSU_D Plugin

Runs the PSU from ESIBD Explorer and monitors live voltage/current readbacks.

The plugin is self-contained: it embeds the minimal private runtime it needs,
including the PSU driver files and vendor DLL.

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
5. Enable the `PSU_D` plugin in the Plugin Manager.

## Device Configuration

- `COM`: Windows COM port number used by the PSU controller.
- `Baud rate`: serial speed passed to the PSU driver.
- `Connect timeout (s)`: timeout used to establish the transport.
- `Startup timeout (s)`: timeout used for ON/OFF startup and shutdown sequences.
- `Poll timeout (s)`: timeout used for periodic housekeeping reads.
- `Operating config`: PSU config exposed directly in the plugin toolbar.
  Use `-1` to connect with outputs kept OFF until manual values are applied.
- `Shutdown config`: advanced slot loaded on OFF. Use `-1` to use software shutdown.
- `Available configs`: live list of config slots reported by the connected PSU.

Toolbar notes:

- `Config`: main PSU config selector shown directly in the toolbar.
- `Load now`: immediately reloads the selected PSU config while the PSU is ON.
- device summary: compact CH0/CH1 ON/OFF + measured voltage/current readbacks.
- `HV outputs`: CH0/CH1 enable readback, not the measured voltage value.
- `Device flags`: low-level PSU state flags reported by the controller.

The plugin now shows a fixed 2-channel operator panel matching the physical PSU
outputs instead of a generic channel table.

Two usage styles are supported:

1. `Config-based`
   Use stored PSU configs. This is the simplest and most reproducible workflow.
2. `Manual`
   Edit the fixed operator panel directly; validate each numeric field to apply it.

If no startup config is selected, turning the PSU `ON` connects to the
controller and keeps the outputs `OFF` until the user applies manual values or
loads a config.

## Operator Panel

Channel cards wrap to fit narrower panels. Scrollbars keep all controls
accessible when space is limited; the mouse wheel does not edit setpoints.

Each PSU channel card exposes:

- output ON/OFF readback
- configured voltage setpoint readback
- configured current limit readback (`Ilim`)
- measured voltage readback (`Vget`)
- measured current readback (`Iget`)
- full-range state (`Full` / `Half`)
- `Display` checkbox used by Explorer plots

The manual panel lets the operator:

- enable/disable each output
- choose full-range mode when supported by the controller
- set `Vset`
- set `Ilim`
- copy the live controller state into the edit panel
- validate numeric edits with Enter, Tab, or a click outside the field;
  intermediate digits are not applied
- save the current live state into a controller config slot

Validating `Vset` or `Ilim` sends only that setting for that channel; other
fields are not reapplied. An enabled channel moves from its current hardware
setpoint to the new voltage without being switched OFF or passing through zero;
the other channel is left unchanged. Larger changes are stepped in either direction.
Reconfiguration retains the disable/verification sequence, and range switching
still requires measured discharge. A failed check triggers output-disable recovery.

## PSU Semantics

The PSU can be used either through stored configs or by setting values
manually. According to the vendor, both methods are valid; configs are simply
easier to reproduce.

`Vset` is the requested output voltage magnitude. CH0 is the positive rail,
CH1 the negative rail; `Vset` and `Vget` remain unsigned magnitudes in the PSU
panel. These polarities follow `PSU_POS = 0` / `PSU_NEG = 1` in the
[CGC PSU controller interface](https://www.cgc-instruments.com/data/Products/Power-Supplies/19PSU/Modules/PSU-CTRL-2D_Files/PSU-CTRL-2D_1-00_2.pdf), pp. 60–65.

`Ilim` is the configured current setting. In normal constant-voltage operation,
it behaves like a current limit/compliance and should be set slightly above the
expected load current. If the load reaches that limit, the PSU can enter
current-limited behavior.

A config name such as `10 V / 1 A` therefore means:

- `10 V` voltage setpoint
- `1 A` configured current limit/current setting

`Full` / `Half` range reflects the rectifier mode reported by the PSU. Full
range allows the maximum voltage. Half range lowers the voltage capability and
allows higher current.

Below the channel cards, a compact `Diagnostics` section summarizes ADC
temperature and dropout voltage. Detailed rail voltages remain in the
diagnostics tooltip instead of the main operator view.

## Explorer Channels and AMX

Each physical output is an Explorer `Channel`: `value` is the Vset request and
`monitor` is the measured Vget, both in V (unsigned magnitudes). The panel, UCM,
scans and equations share these fields. `Enabled` selects the Explorer channel;
it is independent of the HV output gate. A voltage edit never enables a gate.
Channel controls and equations are available in Advanced.

Measurements are published on the GUI thread and recorded through the normal
Explorer history. Missing, disabled, faulty or stale readings become NaN;
`Readback status` explains why. Expiry is two polling intervals, at least 2 s,
even when recording is stopped. Existing channel names are preserved.
Malformed automatic channel defaults (inactive, no equation, 0–0 V limits)
are repaired on loading; intentional limits and equations are preserved.

AMX A/B resolve these channels through `DeviceManager.getChannelByName()` using
the PSU associated with each output pair in their Settings. They display CH0
as Vpos and CH1 as Vneg, relative to the PSU reference (external offset excluded).
Linking an AMX performs no extra hardware reads and issues no PSU commands.

## Portability Note

To copy this plugin to another machine, keep the whole `psu_d/` directory
together, including the embedded `vendor/` subtree.
