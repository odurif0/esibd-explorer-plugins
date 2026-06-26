# ESIBD Explorer Plugins

Ready-to-use plugin bundle for [ESIBD Explorer](https://github.com/ioneater/ESIBD-Explorer).

One plugin for one device.

## Available Plugins

| Plugin   | Description |
|----------|-------------|
| `ampr_a` | Drives AMPR_A high-voltage channels and monitors output voltages |
| `ampr_b` | Drives AMPR_B high-voltage channels and monitors output voltages |
| `psu_a`  | Drives PSU_A outputs and monitors voltage/current readbacks |
| `psu_b`  | Drives PSU_B outputs and monitors voltage/current readbacks |
| `psu_c`  | Drives PSU_C outputs and monitors voltage/current readbacks |
| `psu_d`  | Drives PSU_D outputs and monitors voltage/current readbacks |
| `psu_e`  | Drives PSU_E outputs and monitors voltage/current readbacks |
| `dmmr`   | Reads DMMR module currents and monitors live picoammeter values |
| `amx`    | Drives AMX frequency and pulser timing and monitors pulser readbacks |
| `amx_a`  | Drives AMX_A frequency and pulser timing and monitors pulser readbacks |
| `amx_b`  | Drives AMX_B frequency and pulser timing and monitors pulser readbacks |
| `amx_hd` | Drives AMX HD (HV-AMX-CTRL-4EDH) frequency and timer timing and monitors timer readbacks |

## Quick Start

1. **Download the latest release** `esibd-explorer-plugins-v0.1.2.zip` from the
   [Releases page](https://github.com/odurif0/esibd-explorer-plugins/releases).

2. **Extract the zip** into your ESIBD Explorer `plugins` folder.
   The extracted directory structure should look like this:

   ```
   <ESIBD Explorer>/plugins/
   ├── ampr_a/
   ├── ampr_b/
   ├── psu_a/
   ├── psu_b/
   ├── psu_c/
   ├── psu_d/
   ├── psu_e/
   ├── dmmr/
   ├── amx/
   ├── amx_a/
   ├── amx_b/
   └── amx_hd/
   ```

3. **Enable** the plugins you need in the Plugin Manager.

4. **Select the correct COM port** in the plugin settings for each device
   you want to control. That's it!

## Requirements

- ESIBD Explorer `1.0.1` on Windows


## Running Tests

Tests validate plugin packaging, runtime integrity, and behavior contracts
against the ESIBD Explorer plugin API for development.
