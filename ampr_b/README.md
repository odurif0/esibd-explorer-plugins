# AMPR_B Plugin

Drives AMPR_B high-voltage channels and monitors measured output voltages.

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
5. Enable the `AMPR_B` plugin in the Plugin Manager.

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

The plugin reads measured voltages as channel monitors and applies channel
setpoints through the AMPR driver.

Validate a voltage edit with Enter, Tab, or a click outside the cell. Typing
does not send intermediate digits. Communication runs in the background;
only the latest unsent value per channel is kept. OFF/disconnection cancels
pending commands.

An orange voltage cell means pending or awaiting hardware readback. Red means
an error or a different setpoint readback; the tooltip gives details. Revalidate
the value to retry. The cell returns to normal after matching the readback at
the displayed precision. `Monitor` remains the measured output voltage.

## Portability Note

To copy this plugin to another machine, keep the whole `ampr_b/` directory
together, including the embedded `vendor/` subtree.
