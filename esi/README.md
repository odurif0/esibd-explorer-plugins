# ESI Plugin

Controls the CGC ESI electrospray source, two HVPS-3kB modules, and the
HEAT-CTRL-2410 heater module.

The plugin is self-contained and embeds its private driver runtime, vendor
header, and 64-bit Windows DLL.

## Requirements

- ESIBD Explorer `1.0.1`
- Windows for real hardware communication
- CGC ESI controller with HEAT-CTRL-2410 at address 0 and HVPS-3kB modules at
  addresses 1 and 2

## Activation

1. Extract the plugin bundle into the ESIBD Explorer `plugins` folder.
2. Keep ESIBD Explorer and the vendor control application closed.
3. Open `esi/esi_hardware_probe.ipynb` on the Windows controller PC. The
   validated installation uses `COM16`; update `COM_PORT` if Windows assigns a
   different port.
4. Run every cell and confirm that the generated JSON report maps `ESI_HEAT`
   to address 0, `ESI_HV1` to address 1, `ESI_HV2` to address 2, and reports
   address 3 as absent.
5. Confirm that every `safe_zero_target` operation in the report has status `0`
   before operating connected equipment.
6. Restart ESIBD Explorer. Enable the `ESI` plugin and configure the same COM
   port in its settings.

Initialization validates controller type `0x8ED6`, requires HV modules 1 and 2
to report type `0x0A0D`, requires the heater at address 0 to report type
`0xDB1C`, writes zero targets, and closes the module gate with
`SetEnable(False)` after initialization. The operator-facing names
are `ESI_HV1` (address 1), `ESI_HV2` (address 2), and `ESI_HEAT` (address 0).
Turning the device or an output ON is always an explicit operator action.

Normal HV target changes and ON/OFF transitions use a configurable software
ramp (500 V/s by default). If a ramp fails, the shutdown path attempts an
immediate zero and deactivation, then closes the module gate.

The `ESI_HEAT` channel controls target temperature in degrees Celsius and
monitors measured heater temperature. Advanced voltage, current, and power
limit settings use `0` to retain the limits already configured in hardware.
Nonzero overrides are checked against the limits reported by the controller.
Heating is blocked when the temperature readback is missing, non-finite, below
0 degC, or above the hardware maximum. A disconnected sensor can report a high
out-of-range value even while heater power is zero.

The module-activation read and write functions return vendor status `-10` on
the validated controller firmware and are therefore not used. Effective output
state is controlled by the module gate and zero/nonzero targets.

## Voltage Safety

The HVPS-3kB software target range is 0 to 3000 V. Each module has one shared
unsigned target magnitude and physically distinct positive and negative
outputs. Selecting `+` or `-` configures the corresponding voltage measurement
channel before applying the shared magnitude; negative values are never passed
to the vendor target function.

If a DLL call times out or shutdown cannot be confirmed, treat the HV state as
unknown and use the physical interlock or front panel before approaching the
source.

## Portability Note

To copy this plugin to another machine, keep the whole `esi/` directory
together, including the embedded `vendor/` subtree.
