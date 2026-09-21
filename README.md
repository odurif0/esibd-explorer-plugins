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
| `amx_a`  | Controls AMX_A timing and displays expected CH0–CH3 signals |
| `amx_b`  | Controls AMX_B timing and displays expected CH0–CH3 signals |
| `amx_hd` | Controls AMX HD frequency and timer modules |

## Quick Start

1. **Download the latest release** `esibd-explorer-plugins-v0.2.13.zip` from the
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

## Setpoint entry

Press Enter, Tab, or click outside a numeric field to apply its contents.
An action that uses the setpoint also reads the current field text, even if
it does not move keyboard focus. OFF stops without first applying a new target.
Acquisition refreshes do not replace text while you are editing it.

## ON / OFF

- **ON** connects and runs the configured startup sequence. Check each output's
  readback: ON does not necessarily enable every channel (for example, PSU
  standby configuration `-1` keeps the outputs disabled).
- **OFF** requests and verifies shutdown, then closes communication. Success is
  shown as **Disconnected**; the next ON reconnects.
- **Shutdown unconfirmed** means shutdown or port closure failed. The button
  remains ON so the next click retries OFF, and Explorer's closing warning stays
  active. This button state is not confirmation that outputs are enabled. If a
  DLL call is blocked, make the instrument safe locally and restart Explorer.

For HV devices, a verified disable is **not proof of complete electrical
discharge**. ESI additionally checks both HV polarities on each module against
its 1 V shutdown criterion before disconnecting (see `esi/README.md`). This
software check is not a safe-access certification. Use the device-specific
safety procedure before touching hardware.

## Requirements

- ESIBD Explorer `1.0.1` on Windows


## Running Tests

Development checks run from the repository root:

```bash
python3 -m pytest -q
ESIBD_RELEASE_ZIP=/path/to/esibd-explorer-plugins-vX.Y.Z.zip python3 -m pytest -q tests/test_release_archive_integrity.py
```

Tests remain in this repository's `tests/` directory. They are not part of plugin folders or release archives.
