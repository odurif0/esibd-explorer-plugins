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
- Controller firmware `0x0100` dated July 13, 2026, with the matching July 14
  `COM-ESI-CTRL.dll` bundled by this plugin

## Activation

1. Extract the plugin bundle into the ESIBD Explorer `plugins` folder.
2. Keep ESIBD Explorer and all other ESI applications closed.
3. From the vendor package's top-level `Software` folder, run this read-only
   preflight with `ESI-Controller.exe` and its adjacent 32-bit DLL:

   ```bat
   ESI-Controller.exe 16 -P -m -u -s -sd -si -sv -sf -st -sn -sp -se -ms1 -ma1 -ml1 -ms2 -ma2 -ml2 -t
   ```

   Change `16` if needed. Do not continue unless the utility identifies the
   controller and both HV modules without communication errors.
4. Open `esi/esi_hardware_probe.ipynb` on the Windows controller PC. The
   validated installation uses `COM16`; update `COM_PORT` if Windows assigns a
   different port.
5. Run every cell and confirm that the generated JSON report maps `ESI_HEAT`
   to address 0, `ESI_HV1` to address 1, `ESI_HV2` to address 2, and reports
   address 3 as absent.
6. Confirm that every `safe_zero_target` operation in the report has status `0`
   before operating connected equipment.
7. Run `esi/esi_hv_activation_probe.ipynb` with `ARM_NONZERO_TEST = False` and
   require status `0` from the module activation command plus matching direct
   and PWM activation readbacks. The notebook also selects, verifies, and reads
   both voltage ADC polarities at a zero target before restoring the initial
   selection. A red+green (yellow) module LED is normal at a zero target and
   does not invalidate this gate test. Keep nonzero output disarmed except for
   the separately approved guarded `100 V` commissioning run described below.
8. Restart ESIBD Explorer. Enable the `ESI` plugin and configure the same COM
   port in its settings.

To inspect the active manufacturer configuration without changing it, close
ESIBD Explorer and run:

```bat
ESI-Controller.exe 16 -xs ESI-current-before.cfg -t
```

The activation notebook also records the 53-byte current configuration. The
release copy defaults both arm switches to `False`; commissioning requires an
explicit edit. First use
`ARM_TEMP_CONFIG=True, ARM_NONZERO_TEST=False`: after confirming that the
initial configuration is safely OFF, the notebook applies a temporary
configuration that changes only the selected module's
`HVPSxMaxVoltStep=10.008`, verifies that every target and gate remains OFF,
then restores the exact original safe bytes. A later run may also set
`ARM_NONZERO_TEST=True` to capture the `MS_CTRL_ACT` control bit, PWM
set/measured voltages, physical module LED RGB state, and both ADC polarities.
Low targets remain available and their actual PWM, ADC, and external readings
must be used instead of assuming they equal the setpoint. Nominal-range
commissioning stages `100 V` while the module is OFF, then activates it under
the exact confirmation `ARM 100 V`, with both external meters connected. It
polls target, module state, PWM, and the selected ADC every 50 ms, allows up to
10 seconds for the PWM measurement to reach `95 V`, then records a 20-second
hold. The `95 V` threshold starts the hold but is not an automatic accuracy
verdict. The PWM voltage remains guarded on every poll and during both final
ADC polarity selections, while the ADC permits at most 1.5 seconds between
fresh valid conversions, matching the measured firmware update cadence. The
run aborts above the operator-approved absolute `150 V` limit. Accuracy is
recorded without an automatic nominal-tolerance verdict. It automatically
returns global enable, target, and module activation to OFF, then polls until
both PWM and ADC readbacks fall below `1 V` or a 60-second discharge timeout
expires before asking for the external readings.

Initialization validates controller type `0x8ED6`, requires HV modules 1 and 2
to report type `0x0A0D`, requires the heater at address 0 to report type
`0xDB1C`, writes zero targets, and closes the module gate with
`SetModuleActivationState(False)` before closing the controller-wide gate with
`SetEnable(False)`. The operator-facing names
are `ESI_HV1` (address 1), `ESI_HV2` (address 2), and `ESI_HEAT` (address 0).
Turning the device or an output ON is always an explicit operator action.

Changes to an already active HV target use a configurable software ramp
(500 V/s by default). Initial activation instead loads and verifies the final
target while the module is in standby, then opens the module gate; OFF uses an
immediate zero followed by deactivation. If a target transition fails, the
shutdown path attempts that same immediate zero and deactivation.

The `ESI_HEAT` channel controls target temperature in degrees Celsius and
monitors measured heater temperature. Advanced voltage, current, and power
limit settings use `0` to retain the limits already configured in hardware.
Nonzero overrides are checked against the limits reported by the controller.
Heating is blocked when the temperature readback is missing, non-finite, below
0 degC, or above the hardware maximum. A disconnected sensor can report a high
out-of-range value even while heater power is zero.

## Voltage Safety

The HVPS-3kB software target range is 0 to 3000 V. Each module has one shared
unsigned target magnitude and physically distinct positive and negative
outputs. The published C API has no independent target or activation command
for either connector: `SetHVsupplyTargetOutputVoltage(Address, Voltage)` acts
on the complete module, while `SetHVsupplyMeasRanges(..., VoltNeg, ...)` only
selects an ADC measurement channel. The plugin therefore presents one honest
`+/-` output pair per module with one magnitude and one ON/OFF state. Negative
values are never passed to the vendor target function. The displayed voltage
is the raw ADC readback currently supplied by the firmware and is prefixed
`POS` or `NEG` according to the verified `VoltNeg` measurement selection.

The two connectors cannot be targeted, activated, or disabled independently.
It is possible to connect a load to only one polarity, but activating the module
still energizes the unused connector; it must remain isolated and be treated as
live. `SetHVsupplyMeasRanges(..., VoltNeg, ...)` selects only which connector is
measured. It does not switch either physical output. The two HV modules remain
independently controllable from each other, but each module always behaves as
one coupled +/- pair.

The operator panel distinguishes the target register (`HW target`) from the
internal regulator (`HV control` and `PWM set / measured`) and displays the
module's reported RGB LED color. A red LED is not treated as an error by color
alone because CGC also uses red/blue indicators for positive/negative outputs.
At a nonzero target, an idle control bit or a zero PWM set value means the
target register accepted the request but regulation did not start.

Output generation requires two independent gates: the controller-wide
`SetEnable(true)` state and each HV module's
`SetModuleActivationState(address, true)` HVC toggle. The plugin verifies the
target with `GetHVsupplyTargetOutputVoltage`, the global gate with `GetEnable`,
and the module toggle with the `ActivationState` returned by
`GetHVsupplyParamsPWM`. The July DLL also provides the matching direct
`GetModuleActivationState` readback. Any nonzero activation status is treated
as a failure. The obsolete April DLL used a different serial command for this
same API function and times out with `-10` against the July firmware. Disabled
module targets are set to zero before their HVC toggle is deactivated.

The supplied manufacturer configurations contain an additional
`HVPSxMaxVoltStep` value (`10.008` for active HV examples). The individual
target and activation APIs do not expose this value. After forcing every output
OFF during initialization, the plugin patches only the two volatile
`HVPSxMaxVoltStep` fields to `10.008`, verifies all 53 configuration bytes, and
rechecks both targets and gates before initialization succeeds. This enables HV
control after an OFF configuration whose maximum step is `0`. The plugin never
writes an NVM configuration slot.

The explicitly armed diagnostic notebook also calls `SetCurrentConfig` while
testing and restoring the same volatile configuration.
If that call returns the observed transient `-11` receive status, the notebook
does not assume success: it continues only when an immediate 53-byte readback
matches the requested temporary configuration exactly. Any mismatch aborts the
probe and triggers the safe restoration path.
The manufacturer utility serializes each HV configuration block from byte 17
with a 12-byte stride: target and maximum step are signed 32-bit millivolt
integers, followed by the voltage-ADC selection, current range, activation, and
one padding byte. Thus module 1 `HVPS1MaxVoltStep=10.008` occupies bytes 21-24
as `18 27 00 00` (decimal `10008`). Diagnostic builds must verify this exact
layout at `0 V` before any later nonzero run.

If a DLL call times out or shutdown cannot be confirmed, treat the HV state as
unknown and use the physical interlock or front panel before approaching the
source.

## Portability Note

To copy this plugin to another machine, keep the whole `esi/` directory
together, including the embedded `vendor/` subtree.
