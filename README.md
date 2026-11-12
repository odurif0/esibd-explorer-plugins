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
| `amx_hd` | Controls AMX_HD timing and displays expected CH0–CH3 signals |
| `mscan`  | Scans quadrupole amplitude through AMX-linked PSU channels |
| `transmission` | Optimizes ion transmission through the beamline, stage by stage, on the detector current |
| `tpg366` | Reads six Pfeiffer TPG 366 pressure inputs over USB |

## Quick Start

1. **Download the latest release** `esibd-explorer-plugins-v0.5.12.zip` from the
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

   Put only these plugin folders there, never a copy of this repository: Explorer
   imports every `.py` file of every folder in its plugin path, including `tools/`
   and `tests/`.

3. **Enable** the plugins you need in the Plugin Manager.

4. **Select the correct COM port** in the plugin settings for each device
   you want to control. That's it!

For amplitude scans and maps of the quadrupole, see [MScan](mscan/README.md). Its axis is the RF
amplitude in volts, or m/z. **Stopping a scan does not turn the HV outputs off.**

To optimize the transmission through the inlet, funnel and quadrupoles, see
[Transmission](transmission/README.md). It drives only the mapped channels, in small verified
steps, and measures only while the spray current is stable.

## ON / OFF

- **ON** connects and runs the device's startup sequence. Check each output's readback: ON does
  not necessarily enable every channel.
- **OFF** requests and verifies the shutdown, then closes communication (**Disconnected**).
- **Shutdown unconfirmed**: the shutdown could not be verified; communication stays open for
  another OFF.
- **Disconnected: shutdown unconfirmed**: the device's worker was released, which says nothing
  about the outputs. Make the instrument safe locally before reconnecting or touching it.

For TPG366, ON/OFF only starts or stops the pressure readings.

A verified disable is not proof of a complete electrical discharge. ESI checks both HV polarities
against 10 V before disconnecting (see [ESI](esi/README.md)); this is not a safe-access
certification.

Each device runs in its own process, so stopping or losing one plugin does not stop the others,
and a write is never replayed after a failure. While a device is connected, its plugin keeps
Windows from sleeping on idle; disable sleep in the power plan of the lab PC as well. Worker logs
are written to `<Explorer data path>/logs/<device>/`.

A numeric field is applied with Enter, Tab or a click outside it.

### After an Explorer crash

The instruments keep running. At the next start, each device that was ON reconnects by itself and
adopts the hardware state unchanged: no startup sequence, no forced OFF, no ramp. A device that was
OFF is not contacted, and a normal start keeps every device OFF.

## Requirements

- ESIBD Explorer `1.0.2` on Windows 10 or later (x86-64). No additional Python or Rust
  installation is needed: each plugin folder brings its own worker executable.
- Linux x86-64 workers are also supplied for TPG366, MScan and Transmission (glibc 2.35 or later).

## Notebooks

The hardware probes and characterization notebooks are in [`notebooks/`](notebooks/README.md).
They are not part of the release ZIP.

## Development

Tests run locally (no CI):

```bash
python3 -m venv .venv-explorer && .venv-explorer/bin/pip install -r requirements-dev.txt
.venv-explorer/bin/python -m pytest -q                    # canonical plugin copies
.venv-explorer/bin/python -m pytest -q -m "not slow"      # without the real Explorer/Qt probes
.venv-explorer/bin/python -m pytest -q --all-siblings     # release validation
```

`ampr_b`, `amx_b` and `psu_b`–`psu_e` are copies of `ampr_a`, `amx_a` and `psu_a`: edit the
canonical plugin, then run `python3 tools/sync_family_siblings.py`.

Native workers:

```bash
cargo test --locked --offline --manifest-path native/Cargo.toml --features test-backend
python3 tools/build_native_workers.py --target x86_64-pc-windows-gnu --deploy
python3 tools/build_native_workers.py --check
python3 tools/build_release.py X.Y.Z
```

Each plugin carries its native sources and third-party notices; see
[native/LICENSES.md](native/LICENSES.md).
