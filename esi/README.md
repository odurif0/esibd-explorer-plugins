# ESI Plugin

Controls the CGC ESI electrospray source with two HVPS-3kB modules and one
HEAT-CTRL-2410 heater. The plugin includes its private driver runtime, vendor
header, and 64-bit Windows DLL.

## Requirements

- ESIBD Explorer `1.0.2` on Windows for hardware communication.
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
Reloading an INI configuration preserves channel names, module addresses and
output targets; the current channels remain read-only.

The vendor API reports one current per HV module, not separate currents for
the positive and negative connectors. Voltage, current, and temperature use
separate plot axes and their own units in HDF recordings. A missing or invalid
reading is recorded as `NaN`, never as the previous measurement.

These channels reuse the existing diagnostic reads; they send no output
commands and do not impose a current limit.

### Heater

The heater card provides `Temperature` (°C) and `Power limit` (W) inputs.
The default power ceiling is **50 W** for new settings; saved settings are retained.
Both inputs are bounded by fresh device maxima and disabled while those maxima
are unavailable. Controller maxima are not certified capillary or seal ratings.
Editing either field never enables heating. Live power changes leave voltage,
current and PID settings unchanged; the applied ceiling is independently read back
and shown as `Applied power limit`. Invalid temperature readback blocks a positive
power edit. A newer field edit replaces older queued edits; OFF cancels queued
changes. A native write already in progress cannot be undone by cancellation.

`ESI_HEAT` controls target temperature in degrees Celsius. ON writes and reads
back the target, then commands and verifies module 0 before opening the shared
gate. ON then polls the complete CTRL/MOD/DEV activation state within the I/O
budget instead of treating an incomplete transition as immediate failure. Faults,
lost command readbacks and Stop abort confirmation; ON is never replayed.
The native setter returns the applied temperature, which may be quantized;
an independent read verifies that value. The panel shows `ON` only when module
activation, controller gate and temperature-control state are confirmed. Otherwise it shows
`Unconfirmed`; OFF remains available even with an invalid sensor. `ON` confirms
controller states, not delivered heating power. `PID power target` is the CGC power
target, not a measured power.

The separate `Stability` line requires a full 60-second observation window within
±0.2 °C of the applied target and an absolute fitted drift below 0.1 °C/min.
`Stabilizing` does not delay ON or change the device's PID. New commands, OFF,
invalid data and acquisition gaps reset qualification; stale data show
`Unavailable`. The tooltip reports the observed span and drift. These are
experimental temperature criteria, not proof of pressure equilibrium or safety.

Local OFF disables and verifies module 0, then zeros and reads back its target,
without stopping the HV modules. Zero temperature alone is not proof of OFF.
Global OFF and configuration loading also require confirmed heater deactivation;
a failure prevents port closure and leaves shutdown unconfirmed.

Advanced voltage/current settings and the existing power setting use `0` to
retain the device limit (shown as `Keep device limit` for power), not to request
zero power. Installing the update does not apply the new default to hardware. ON and positive temperature writes require all three actual limits to be finite, positive and within
the reported hardware maxima; it never substitutes maxima for missing limits.
Choose limits suitable for the heater load. The fresh sensor reading must also
be valid, finite and within 0 degC to the hardware maximum. A disconnected
sensor can report an out-of-range value even with no heating power.
The target cannot exceed the temperature maximum reported by the device.

### ON / OFF

ON connects and starts operation according to the selected outputs. Review
their targets and activation states before switching ON.

If the initial port opening fails, OFF or closing communication cleans up
that opening without attempting an HV shutdown. The state stays
`Connection pending` while the native Open or Close call is unfinished or closure is not
confirmed; the backend and port reservation are retained. Confirmed closure
sets `Disconnected`, without certifying the HV output state; a new ON
explicitly reconnects. A call that never returns
or a failed closure may still require an Explorer restart. This cleanup does
not apply to a timeout during operation. If Open succeeds after a close
request, normal verified shutdown runs instead of completing initialization.

For an established connection, global OFF first disables the outputs, then
displays `Stopping: checking HV`.
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
instrument's safety procedure. A DLL blocked during operation requires an
Explorer restart.

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
Run notebooks on the Windows controller PC and set their COM port to the actual
port (`16` is the notebook default).

### Read-only heater notebook

[`esi_heater_readonly_probe.ipynb`](esi_heater_readonly_probe.ipynb) has one code
cell and uses the installed bundled DLL. It reads heater activation, interlocks,
limits, targets, monitoring, LED RGB and identification without output,
configuration or baud-setting commands. Use a fresh kernel; Close is attempted
only after a successful Open. Unavailable modules are not enabled to read them. Explorer shutdown requests OFF, so this is a post-shutdown snapshot, not a
reproduction of the preceding ON failure.

Send the JSON report saved under `logs/esi_heater_readonly/`, including partial
reports. A timeout or interrupt stops further DLL calls, with no concurrent or
late cleanup; make the instrument safe locally before restarting the kernel.
A confirmed communication Close is not an output-shutdown confirmation.

### Active heater characterization (up to 100 °C)

[`esi_heater_characterization.ipynb`](../notebooks/esi_heater_characterization.ipynb) is a
supervised test of the original CGC heater, with HV gates OFF and zero targets
verified. Use a fresh 64-bit Windows kernel and check `ESI_COM`.
**`ARM_HEATING=True` by default: running the cell requests real heating.**
Set it to `False` for a no-hardware check. Existing unfinished-run guards still
block activation. The SHA-256 of the runtime/DLL used is recorded with each run.

The fixed trial envelope is at most **22 V / 10 A / 50 W**, further bounded by
fresh hardware maxima and rounded down to verified native codes using the shared
pure `_heater_limits.py` helper. Applied echoes and independent readbacks must
agree before activation. These are agreed test
settings, not certified load ratings: 22 V / 50 W is present in the supplied CGC
Heat60 profile; 10 A uses the controller's nominal designation rather than
the profile's 12 A. No complete profile is loaded and power is never increased automatically.

Start below 30 °C. After a 60 s OFF baseline, test 30/40/50/60/70/80/90 °C.
Each stage must first reach ±1 °C within 300 s, then complete 60 continuous
seconds in that zone, with a fixed total limit of 360 s. An excursion resets
the observation, never extends this total deadline. The first successful
30 °C stage includes OFF for 60 s and a controlled restart at 30 °C; changed
limits block restart rather than being silently rewritten. The final 100 °C
stage has a 300 s deadline with no extension and stops at the first observed
temperature ≥100 °C, without a hold. These are experimental criteria, not thermal equilibrium
or a physical guarantee against overshoot. After confirmed heater OFF, observe
cooling for 180 s, then verify HV discharge and port closure; 180 s does not
establish that the heater is cold. Cleanup progress is printed once per phase
(waiting for OFF, cooling observation, discharge/closure); the final result and
any unconfirmed-shutdown warning remain visible.

Interrupt the kernel once to request Stop. The original instrument owner waits
for the active call before cleanup; a second Stop may shorten cooling observation,
not interrupt discharge verification. Poisoned or unknown transports forbid further
commands/Close and retain the unfinished-run guard: use physical safety controls,
never rerun or restart the kernel as a substitute for OFF.

Keep `notebooks/` beside `esi/`, or set `PLUGIN_DIR` to the ESI plugin folder.
CSV/JSON and `heater.png` remain under the plugin's `logs/esi_heater_characterization/`.
Do not move or delete existing logs or unfinished-run markers when updating the notebook.
`native_calls.jsonl` records the existing base-wrapper calls with arguments,
exact returned statuses/tuples (including `Valid=False` and tagged NaN/Infinity),
UTC/monotonic timestamps, exceptions, and every supplied discharge observation.
Tracing itself adds no instrument calls or retries. The matched runtime waits
for module-0 MON_RDY (bit 0) before one monitoring read, in diagnostics and before
positive targets/ON. Waiting uses the existing I/O budget (normally 5 s), not a
fixed settling delay. A datum invalid after readiness still refuses heating;
no previous temperature is substituted. Stop cancels pending heating waits and
commands, while OFF/cooling remain available. After ON, the runtime also waits
for confirmed CTRL/MOD/DEV activation, with cancellable polling waits of up to
0.1 s within its configured I/O budget. This is a software bound, not a measured settling
time; a fault or failure to converge still aborts. The HV discharge check is unchanged.
Logging has timing overhead and is not a raw serial capture.
The asynchronous writer reports capture errors and stays available for a late
native return after poison; forced process termination can lose pending records.
Characterization and pressure–temperature notebooks share `_experiment_guard.py`.
It acquires a per-user COM lock under `~/.esibd/esi_experiment_guards/` and the
historical heater lock, and checks both notebooks' known unfinished-run markers
before constructing any instrument. Changing output folders or notebook names
cannot bypass registered uncertainty. Other users, physical port aliases and
unknown legacy output folders are not covered: close Explorer and former kernels.
An unfinished run blocks heating until a separate operator-authorized restart.
In a fresh kernel, type the run-specific `RESTART …` phrase only after physically
making the equipment safe and terminating the former ESI/pressure-owning processes.
This declares both conditions; software does not verify another process's exit
or retroactively confirm HV discharge. ARM alone never acknowledges restart.
The original marker, report and available traces are archived with SHA-256 hashes
under the COM registry's `operator_restarts/`; original shutdown values are preserved.
The declaration and initial report are durable before the central claim and
compatibility markers are updated, then instrument construction may start.
These file updates are not a single atomic transaction: an interrupted transition
blocks heating rather than discarding uncertainty. With a valid central claim and
intact original evidence, missing compatibility markers are recorded and require
explicit operator recovery; they are never repaired automatically. Markers are
released only after confirmed shutdown, finished owners and a durable final report.
Missing/changed original evidence, preloaded runtimes, archive failures or a
competing owner abort the restart. OS-held ownership lasts throughout the new run;
no automatic retry follows a further failure.

Monitoring VoltOut, VoltHeat and CurrOut remain separate from voltage/power
**targets**. VoltHeat × CurrOut is a calculated indicator, not verified dissipated
power; averaging/PWM/synchronization are unspecified. There is no undocumented
U/I-to-limit alarm or claim of synchronous electrical sampling. Full electrical power/energy
characterization requires CGC clarification or suitable external instrumentation.

Electrical limits remain configured after OFF; initial zeros are not restored.
No Save/NVM command is issued, but power-cycle persistence is unknown. This test
**does not qualify 175 °C**, even if the retained positive limits subsequently
satisfy another notebook's preflight. Keep required cooling in service and physical
shutdown controls accessible throughout the experiment.

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

### Retired HV activation probe

The legacy low-level HV activation notebook is no longer shipped. Its discharge
check accepted one reading from one selected module instead of the driver's
three fresh rounds on both modules and both polarities. Do not run old copies.
The supported heater notebooks keep HV disabled and preserve an unconfirmed
shutdown; operator restart does not certify discharge. Nonzero HV commissioning
requires a separate approved protocol and resolution of the current readout issue.

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
