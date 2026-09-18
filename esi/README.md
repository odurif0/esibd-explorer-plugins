# ESI Plugin

Controls the CGC ESI electrospray source with two HVPS-3kB modules and one
HEAT-CTRL-2410 heater. The plugin includes its private driver runtime, vendor
header, and 64-bit Windows DLL.

## Requirements

- ESIBD Explorer `1.0.1` on Windows for hardware communication.
- CGC ESI controller with HEAT-CTRL-2410 at address 0 and HVPS-3kB modules at
  addresses 1 and 2.
- Controller firmware `0x0100` dated July 13, 2026, with the matching July 14
  `COM-ESI-CTRL.dll` bundled by this plugin. Do not substitute the obsolete
  April DLL: its activation command times out with `-10` on the July firmware.

## Installation

1. Extract the plugin bundle into the ESIBD Explorer `plugins` folder.
2. Close any notebook or vendor utility using the ESI controller.
3. Enable the `ESI` plugin in the Plugin Manager.
4. Set the controller's Windows COM port and baud rate (default: `230400`).

No notebook or JSON report is required to enable or use the plugin.

Initialization checks the controller type (`0x8ED6`) and module inventory:
`ESI_HEAT` at address 0 (`0xDB1C`), and `ESI_HV1` / `ESI_HV2` at addresses 1 / 2
(`0x0A0D`). It rejects missing or mismatched modules and unexpected HV modules.
It zeros the HV and heater targets, verifies disable, and configures the
volatile HV maximum-step values described below. Initialization finishes with
HV and heater outputs OFF; output activation requires an operator command.

## Operation and safety

### HV outputs

Each HV module has one unsigned target magnitude, from 0 to 3000 V, shared by
its positive and negative connectors. The two modules are independent, but
**the two connectors of a module cannot be controlled separately**. Activating
that module also energizes its unused connector; keep it isolated and treat
it as live.

The `POS` / `NEG` selection changes only the voltage ADC being read. It does
not switch an output or change its polarity. The displayed voltage is the raw
ADC readback; negative targets are never passed to the vendor target API.

Apply a typed voltage with Enter, Tab, or a click outside the field. A button
that uses the target also reads the current field text; OFF stops without
first applying a new target. Acquisition refreshes leave edits untouched.
The heater's numeric target uses the same rule.

Initial activation sets and verifies the target while the module is in
standby, then activates it. Changes to an active target use the software ramp
(default: 500 V/s). Disabling an HV output requests zero immediately, then
verifies deactivation. A failed target change attempts the same rollback.

HV generation requires both the controller-wide `SetEnable` state and the
module's `SetModuleActivationState` state. The driver checks the target and
both activation states. The panel separates `HW target` from `HV control` and
`PWM set / measured`: an accepted target does not prove that regulation has
started. At a nonzero target, an idle control bit or zero PWM set value
indicates that it has not started.

The panel also shows the reported RGB LED color. Color alone is not a fault
diagnosis: CGC uses red/blue for positive/negative outputs, and red+green
(yellow) is normal at a zero target.

### HV current measurements

The cards display the measured current for each HV module. Two read-only
Explorer channels, `ESI_HV1_I` and `ESI_HV2_I`, provide live traces and recorded
currents in amperes. They are added automatically to existing configurations
without replacing the voltage and heater channels or their settings.

The vendor API reports one current per HV module, not separate currents for
the positive and negative connectors. Voltage, current, and temperature use
separate plot axes and their own units in HDF recordings. A missing or invalid
reading is recorded as `NaN`, never as the previous measurement.

These channels reuse the existing diagnostic reads; they send no output
commands and do not impose a current limit.

### Heater

`ESI_HEAT` controls target temperature in degrees Celsius and monitors measured
temperature. Advanced voltage, current, and power limits use `0` to retain the
hardware settings; positive overrides are checked against hardware limits.
Heating is blocked if the temperature readback is missing, non-finite, below
0 degC, or above the hardware maximum. A disconnected sensor can report an
out-of-range temperature even with zero heater power.

### ON / OFF

ON connects and starts operation according to the selected outputs. Review
their targets and activation states before switching ON.

Global OFF first disables the outputs, then displays `Stopping: checking HV`.
The port stays open while the driver checks both ADC polarities on both HV
modules. All four absolute voltages must be **at most 1 V for three consecutive
fresh measurement rounds** before the port closes. The check has a 60-second
deadline; a blocked DLL is handled by the transport watchdog.

The ADC selection is switched and checked for each polarity, old samples are
discarded, and the data-ready flags must clear then signal a new conversion.
The initial selections are restored unless the transport is unusable. Invalid
readings, unproven ADC freshness, a timeout, or a failed port closure leave
`Shutdown unconfirmed`; the controller is retained and the next click retries
OFF. The button stays ON during checking or uncertainty so OFF remains
accessible; this does not mean that an output is confirmed active.

The cards show both voltage readings and the current while checking. Current
readings must be valid, but there is **no current threshold** until its accuracy
and acceptable zero-current residual are established. After successful OFF,
the cards and buttons are neutral gray, the state is `Disconnected`, and the
next ON reconnects. Saved output selections are not changed.

**This 1 V software check does not certify safe access or complete discharge.**
It cannot verify disconnected loads or replace an independent voltage check.
If shutdown is unconfirmed, use the physical interlock/front panel and the
instrument's safety procedure. A blocked DLL requires an Explorer restart.

The cards wrap in narrow panels, with scrollbars when needed. The mouse wheel
does not edit setpoints.

## Configuration

The driver supports NVM slots `0..1022`; the plugin's `Operating config` setting
currently accepts `0..255`, or `-1` for no selection. `Available configs` lists
the slots reported by the controller; `Loaded config` shows the last loaded
slot. Selecting a number does not load it automatically.

While the device is ON, click `Load config` to load the selected slot into
volatile memory. The driver forces the outputs OFF before and after loading.
The plugin never saves NVM slots; use the vendor utility or the driver's
`save_config` method for that operation.

Initialization and configuration loading set both volatile `HVPSxMaxVoltStep`
fields to `10.008`. This permits regulation after loading an OFF configuration
whose maximum step is zero. The driver verifies all 53 configuration bytes
and checks that targets and activation states remain OFF. It does not save
these changes to NVM.

## Optional diagnostics and commissioning

Use these tools when investigating hardware, communication, or activation
problems. They are separate from normal plugin installation. Close Explorer
and other ESI applications first: the DLL allows only one active connection.
Run notebooks on the Windows controller PC and set `COM_PORT` to its actual
port (`16` is the notebook default).

### Inventory notebook

[`esi_hardware_probe.ipynb`](esi_hardware_probe.ipynb) identifies modules and
records diagnostics in a JSON report. It is **not read-only**: it switches
module communication with `set_enable`, writes zero HV and heater targets
(`0 V`, `0 degC`), then attempts to disable operation and close the port.
Previous targets and activation states are not restored.

Check the inventory, every `safe_zero_target` status, and cleanup results;
status `0` means the command succeeded. The expected controlled modules are
HEAT=0, HV1=1, and HV2=2. Keep the physical interlock available and verify HV
independently. The notebook's low-level DLL calls have no cancellable timeout;
if a call blocks, make the instrument safe before restarting the kernel.

### Activation notebook

[`esi_hv_activation_probe.ipynb`](esi_hv_activation_probe.ipynb) tests activation
commands and readbacks. Both `ARM_TEMP_CONFIG` and `ARM_NONZERO_TEST` default
to `False`. Even with these switches disarmed, it changes activation states,
zero targets, and ADC selections. It is not a passive inventory.

The zero-target test checks direct and PWM activation readbacks and both ADC
polarities, then restores the initial ADC selection. Require status `0` and
matching readbacks before proceeding with commissioning.

For a configuration test, first use `ARM_TEMP_CONFIG=True` with
`ARM_NONZERO_TEST=False`. This requires an initially verified OFF configuration,
changes only the selected module's `HVPSxMaxVoltStep` to `10.008`, verifies the
53-byte readback, and restores the original safe bytes. After a transient
`SetCurrentConfig` status `-11`, the notebook proceeds only if the immediate
readback matches exactly; a mismatch aborts the test and triggers restoration.

A nonzero test requires a separate, successful configuration-only run,
`ARM_NONZERO_TEST=True`, the confirmation `ARM 100 V`, and both external meters.
It stages `100 V` while the module is OFF, then activates it. The notebook:

- polls target, module state, PWM and the selected ADC with a 50 ms loop delay;
- allows 10 seconds to reach a `95 V` PWM trigger, then records a 20-second hold;
- aborts above the approved absolute `150 V` limit, including during the final
  ADC polarity reads; during continuous polling, it also aborts after more than
  1.5 seconds without a fresh valid ADC conversion;
- attempts to return enable, target and module activation to OFF, then waits
  for PWM and ADC readbacks below `1 V`, with a 60-second timeout, before
  requesting the external meter readings.

The trigger starts observation, not an accuracy verdict. Evaluate PWM, both
ADCs and both external meters rather than assuming the setpoint was reached.

For configuration diagnostics, the HV blocks begin at byte 17 with a 12-byte
stride: signed 32-bit millivolt target and maximum step, ADC selection, current
range, activation, and padding. Module 1's `10.008` maximum step occupies bytes
21-24 as `18 27 00 00`. Verify this layout at `0 V` before any nonzero test.

### Vendor utility

From the vendor package's top-level `Software` folder, use `ESI-Controller.exe`
with its adjacent 32-bit DLL to inspect identification and status. Replace
`16` with the actual COM port and investigate any communication error before
running further tests:

```bat
ESI-Controller.exe 16 -P -m -u -s -sd -si -sv -sf -st -sn -sp -se -ms1 -ma1 -ml1 -ms2 -ma2 -ml2 -t
```

To export the current manufacturer configuration without changing it:

```bat
ESI-Controller.exe 16 -xs ESI-current-before.cfg -t
```

## Portability

To copy this plugin to another machine, keep the whole `esi/` directory
together, including the embedded `vendor/` subtree.
