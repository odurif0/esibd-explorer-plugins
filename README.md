# ESIBD Explorer Plugins

Ready-to-use plugin bundle for [ESIBD Explorer](https://github.com/ioneater/ESIBD-Explorer).

13 device plugins and two standalone scan plugins (15 folders).

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
| `mscan`  | Scans quadrupole amplitude through AMX-linked PSU channels |
| `transmission` | Optimizes ion transmission through the beamline apertures, stage by stage |
| `tpg366` | Reads six Pfeiffer TPG 366 pressure inputs over USB |

## Quick Start

1. **Download the latest release** `esibd-explorer-plugins-v0.3.2.zip` from the
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
   ├── amx_hd/
   ├── mscan/
   ├── tpg366/
   └── transmission/
   ```

3. **Enable** the plugins you need in the Plugin Manager.

4. **Select the correct COM port** in the plugin settings for each device
   you want to control. That's it!

For amplitude scans, see [MScan](mscan/README.md). Its axis is in volts, without
m/z calibration. **Stopping a scan does not turn the HV outputs off.**

To optimize the transmission through the inlet, funnel and quadrupoles, see
[Transmission](transmission/README.md): describe the beamline once, choose the total
ion current or a mass selected by a quadrupole (peak picked on an MScan spectrum or a
quick sweep, in volts), choose the stages, then Optimize. It drives only the mapped
channels, in small verified steps, and can run on a built-in simulated beamline.

## Host fixes and hardware validation

`install_explorer_fixes.py` installs the targeted Explorer fixes for small-current
cursor labels, incomplete channel names, UTF-8 configuration reading and the order
of tabbed plugin docks (Explorer forgets a manual order at each restart; with the
fix it is saved at closing and reapplied once the plugins are loaded, and plugins
added since go last). Run it
with Explorer's Python environment after closing Explorer and its notebooks;
`--check` is read-only, and the original sources are backed up. Run it again after
every Explorer update: upgrading Explorer replaces the patched files (the fixes
are not yet in Explorer 1.0.2). It does not open
instruments. Software validation is separate from the physical checks listed in
[SOFTWARE_TEST_PLAN.md](SOFTWARE_TEST_PLAN.md).

## Setpoint entry

Press Enter, Tab, or click outside a numeric field to apply its contents.
An action that uses the setpoint also reads the current field text, even if
it does not move keyboard focus. OFF stops without first applying a new target.
Acquisition refreshes do not replace text while you are editing it.

## ON / OFF

For **TPG366**, ON/OFF starts or stops acquisition and the USB connection.
It never switches gauges or changes controller settings.

For the other device plugins:

- **ON** connects and runs the configured startup sequence. Check each output's
  readback: ON does not necessarily enable every channel (for example, PSU
  standby configuration `-1` keeps the outputs disabled).
- **OFF** requests and verifies shutdown, then closes communication. Success is
  shown as **Disconnected**; the next ON reconnects.
- **Shutdown unconfirmed** means shutdown or port closure failed. The button
  remains ON so the next click retries OFF, and Explorer's closing warning stays
  active. This button state is not confirmation that outputs are enabled. If a
  DLL call is blocked, make the instrument safe locally and restart Explorer.

A failed initial port opening uses **Connection pending** instead, with the
DMMR button OFF rather than falsely indicating initialized operation. OFF or
closing communication cleans up that opening without output commands. The
driver instance and port reservation remain until the native call has ended
and closure is confirmed. This confirms port closure, not the hardware output
state. If opening succeeds after a close request, the plugin runs its normal
verified shutdown instead of completing initialization.
A new ON explicitly reconnects after cleanup; a native call that never returns
or an unconfirmed closure can still require a restart.

While a device's port is open (from ON until its closure is confirmed,
including an unconfirmed shutdown), its plugin asks Windows not to sleep on
idle: a sleeping PC leaves the outputs in their last state with no software
supervision. `powercfg /requests` lists each connected device under SYSTEM
("ESIBD Explorer: …"). A manual sleep, a closed lid or the power button are not
prevented; disable sleep in the power plan of the lab PC as well.

### After an Explorer crash

A software crash does not interrupt an experiment: the instruments keep running.
Each device plugin records, while it is ON, a small file
`<Explorer config path>/<device>.session.json` (port, recording). OFF,
disconnection and a normal Explorer close remove it. When Explorer starts and
finds such a record left by a crashed Explorer for the same COM port, the plugin
reconnects by itself and **adopts the hardware state unchanged**: no startup
sequence, no forced OFF, no config load, no ramp and no re-applied setpoint. The
channel values are aligned to the setpoints the device reports, and recording
restarts if it was on. DMMR and TPG366 resume with their normal ON (measurement
only). A device that was OFF is not contacted. A normal start keeps every device
OFF; ESI outputs and PSU panels start as described in their READMEs.

If the instrument was powered off meanwhile, the resume ends like a failed ON
(a timed-out opening can lock the port until Explorer restarts).

For HV devices, a verified disable is **not proof of complete electrical
discharge**. ESI additionally checks both HV polarities on each module against
its 1 V shutdown criterion before disconnecting (see `esi/README.md`). This
software check is not a safe-access certification. Use the device-specific
safety procedure before touching hardware.

## Requirements

- ESIBD Explorer `1.0.2` on Windows


## Notebooks

All notebooks are in [`notebooks/`](notebooks/README.md): ESI and AMX HD hardware
probes, the DMMR zero check, ESI heater characterization and the
pressure–temperature protocol. Run them outside Explorer, with `notebooks/`
beside the plugin folders. Notebooks are not included in the plugin release ZIP.

## Running Tests

Development checks run from the repository root:

```bash
python3 -m pytest -q                     # default: behaviour tests on canonical copies (~15 min)
python3 -m pytest -q -m "not slow"       # without real Explorer/Qt subprocesses (~3 min)
python3 -m pytest -q --all-siblings      # release validation: every sibling copy too
ESIBD_RELEASE_ZIP=/path/to/esibd-explorer-plugins-vX.Y.Z.zip python3 -m pytest -q tests/test_release_archive_integrity.py
```

The real-Explorer tests use the installed `esibd-explorer` (or
`ESIBD_EXPLORER_SOURCE` / `ESIBD_QT_PYTHON`). They validate the release host only
when that is Explorer `1.0.2`; the session header shows the host and the summary
warns otherwise. Use a dedicated environment, and require it for release checks:

```bash
python3 -m venv .venv-explorer && .venv-explorer/bin/pip install -r requirements-dev.txt
ESIBD_REQUIRE_TARGET_HOST=1 .venv-explorer/bin/python -m pytest -q
```

Sibling plugins (`ampr_b`, `amx_b`, `psu_b`–`psu_e`) are copies of their
canonical plugin. Edit the canonical one, then run
`python3 tools/sync_family_siblings.py` (`--check` reports drift only). Because
parity tests guarantee identical copies, behaviour tests run on the canonical
copy unless `--all-siblings` is given. Tests run locally only (no CI);
use `--all-siblings` with Explorer 1.0.2 before a release.

Tests remain in this repository's `tests/` directory. They are not part of plugin folders or release archives.
