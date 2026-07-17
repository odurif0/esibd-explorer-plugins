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
| `esi`    | Controls ESI HVPS-3kB modules 2 and 3 and monitors voltage/current readbacks and interlocks |
| `amx`    | Drives AMX frequency and pulser timing and monitors pulser readbacks |
| `amx_a`  | Drives AMX_A frequency and pulser timing and monitors pulser readbacks |
| `amx_b`  | Drives AMX_B frequency and pulser timing and monitors pulser readbacks |
| `amx_hd` | Drives AMX HD (HV-AMX-CTRL-4EDH) frequency and timer timing and monitors timer readbacks |

## Quick Start

1. **Download the latest release** `esibd-explorer-plugins-v0.2.zip` from the
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
   ├── esi/
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

Development checks run from the repository root:

```bash
python3 -m pytest -q
ESIBD_RELEASE_ZIP=/path/to/esibd-explorer-plugins-vX.Y.Z.zip python3 -m pytest -q tests/test_release_archive_integrity.py
```

Tests remain in this repository's `tests/` directory. They are not part of plugin folders or release archives.
