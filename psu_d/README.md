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
   Edit the fixed operator panel directly, then press `Apply now`.

If no startup config is selected, turning the PSU `ON` connects to the
controller and keeps the outputs `OFF` until the user applies manual values or
loads a config.

## Operator Panel

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
- apply the edited state immediately
- save the current live state into a controller config slot

## PSU Semantics

The PSU can be used either through stored configs or by setting values
manually. According to the vendor, both methods are valid; configs are simply
easier to reproduce.

`Vset` is the requested output voltage.

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

## Portability Note

To copy this plugin to another machine, keep the whole `psu_d/` directory
together, including the embedded `vendor/` subtree.
