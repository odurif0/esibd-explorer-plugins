# ESIBD Explorer Plugins

Ready-to-use plugin bundle for [ESIBD Explorer](https://github.com/ioneater/ESIBD-Explorer).

One plugin for one device.

## Available Plugins

| Plugin   | Description |
|----------|-------------|
| `ampr_a` | Controls AMPR_A high-voltage modules |
| `ampr_b` | Controls AMPR_B high-voltage modules |
| `psu_a`  | Controls PSU_A power-supply modules |
| `psu_b`  | Controls PSU_B power-supply modules |
| `psu_c`  | Controls PSU_C power-supply modules |
| `psu_d`  | Controls PSU_D power-supply modules |
| `psu_e`  | Controls PSU_E power-supply modules |
| `dmmr`   | Monitors DMMR picoammeter modules |
| `esi`    | Controls ESI heater and paired +/- HVPS-3kB modules |
| `amx_a`  | Controls AMX_A frequency and pulser modules |
| `amx_b`  | Controls AMX_B frequency and pulser modules |
| `amx_hd` | Controls AMX HD frequency and timer modules |

## Quick Start

1. **Download the latest release** `esibd-explorer-plugins-v0.2.1.zip` from the
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
   ├── amx_a/
   ├── amx_b/
   └── amx_hd/
   ```

3. **Enable** the plugins you need in the Plugin Manager.

4. **Select the correct COM port** in the plugin settings for each device
   you want to control. That's it!

When upgrading from a bundle that contained the legacy `amx/` plugin, remove
that folder before extracting the new bundle. Extracting an archive does not
delete old plugin folders; the supported standard-AMX identities are now
`AMX_A` (`amx_a/`) and `AMX_B` (`amx_b/`).

The ESI controller requires an additional read-only hardware inventory before
the plugin is enabled. Follow [`esi/README.md`](esi/README.md) and run the
bundled `esi_hardware_probe.ipynb` first.

## Requirements

- ESIBD Explorer `1.0.1` on Windows


## Running Tests

Development checks run from the repository root:

```bash
python3 -m pytest -q
ESIBD_RELEASE_ZIP=/path/to/esibd-explorer-plugins-vX.Y.Z.zip python3 -m pytest -q tests/test_release_archive_integrity.py
```

Tests remain in this repository's `tests/` directory. They are not part of plugin folders or release archives.
