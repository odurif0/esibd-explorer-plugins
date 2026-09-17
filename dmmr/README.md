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

The plugin auto-discovers installed modules, creates one channel per detected
module, reads live current measurements as channel monitors, and exposes a
global ON/OFF control that enables or disables DMMR acquisition.

Use `Display` to show or hide a module's time trace. The color square next to
it opens the color picker; the choice is saved in the channel configuration.
Automatic Y scaling also handles constant picoamp signals without changing
manual zoom. Measurements and recordings remain in amperes.

Compact module cards use as many columns as the panel can fit, with
scrollbars when space is limited. Edit the label below the module number;
Enter or leaving the field saves it, Escape cancels the edit. Labels are
stored by module address without renaming recorded channels.

Time and current histories share the same capacity, including before module
discovery. At the storage limit, older values and timestamps are thinned
together so each retained measurement keeps its original time.

## Portability Note

To copy this plugin to another machine, keep the whole `dmmr/` directory
together, including the embedded `vendor/` subtree.
