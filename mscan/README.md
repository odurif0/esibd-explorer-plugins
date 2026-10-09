# MScan Plugin

Amplitude scan for an AMX/PSU quadrupole, with current measured by a DMMR module.
Enable the `MScan` plugin in Explorer's Plugin Manager. Its panel is titled
**msScan — AMX/PSU**, distinct from the built-in **msScan** that expects
`AMP_Q1`/`AMP_Q2`. Those historical channels are not used here.

Requires Explorer 1.0.2 and the PSU A–E, AMX A–B, DMMR and (for the quadrupole
offset) AMPR A–B plugins from the same bundle. The plugin uses Explorer's plotting, channel services and HDF5 format,
but has its own interface and scan protocol, bundled Python adapter and native
Rust scan worker. MScan has no vendor DLL of its own.

## Native Worker

Production planning and the scan state machine run in an isolated native Rust
worker: `native/esibd-mscan-worker.exe` on Windows x86-64 or
`native/esibd-mscan-worker` on Linux x86-64. The supervisor selects that exact
plugin-local binary from `native/manifest.json` and verifies its SHA-256, family
and protocol. Missing or mismatched files fail startup; workers are not searched
on PATH. The DLL-backed device plugins still require Windows x86-64 for real
hardware communication.

The Python GUI and `_runtime/_native_scan.py` adapter execute acknowledged
actions and collect observations only through Explorer channels. The worker
does not import device plugins or call their DLLs. The original Python scan
implementation is retained as an explicit reference for tests, never a
production fallback. No external or private Python worker interpreter or
Explorer fork is required.

Stop/Explorer close cancels pending actions and can terminate and reap an
unresponsive scan worker; a crash or hang does not stop other workers. A later
explicit Start creates a fresh worker without restarting Explorer or replaying
old commands. Worker exit proves neither restored voltages nor hardware OFF;
Stop/error retains the voltage behavior described below. Worker logs are under
`<Explorer data path>/logs/mscan/native/mscan/`, never inside the plugin.
The v0.5 native port has local mock-channel, Wine and Explorer 1.0.2 software
validation. Real-hardware qualification remains pending; complete it before
routine experimental use. These checks do not certify physical shutdown.

## Use

1. Configure the PSU associations in AMX Settings and establish the waveform.
   Turn ON the required PSU rails and AMX. Start DMMR acquisition and recording.
2. Choose **AMX** and one pair of **AMX connectors**: **CH0-CH1** or **CH2-CH3**.
   **Driven PSU** lists the supply channels swept together; shared outputs are
   identified too. **Fixed frequency** comes from the AMX output readback, not
   an assumed oscillator frequency. Per-edge compensation delays are allowed
   and retained in the scan metadata; their exact phase is not inferred.
   Changing them during a scan aborts it. Update AMX and MScan together.
3. Select **DMMR module**, shown as its physical number and channel name.
   Choose the module wired to the ion collector after the quadrupole.
   Only this module's current is acquired; no placeholder `Detector` is used.
   **DMMR interval (ms)** edits the live DMMR acquisition/recording setting,
   shared by **all** its modules. It does not change the ADC configuration.
4. Set **Amplitude from / to (V)** within **Allowed amplitude (V)**.
   The voltage range and **PSU Ilim** are grouped together above the scan settings.
   **After completion** shows each channel's return voltage and live measured
   current, e.g. `PSU_D_CH0: +4 V, Iget 32 mA`.
5. Optional: **Quadrupole offset (AMPR)** selects the AMPR channel (module and
   channel) applying the quadrupole offset; **None** (default) leaves it alone.
   During the scan it is set to U = **Offset coefficient (U/V)** × A at every
   amplitude (default 0.2, negative allowed), together with the PSU rails.
   U is the DC offset and V the RF amplitude, here the scanned amplitude A.
   **Offset readback** shows the AMPR Monitor and setpoint; during a scan, the
   scan target and whether the AMPR confirmed it. See *Quadrupole offset* below.
6. Choose **Scan mode**: **Step by step** (default), with **Amplitude step (V)**,
   or **Continuous**, with **Sweep rate (V/s)** and **Time step (s)**.
   Set the timing fields shown for that mode, described below.
7. When **Status** shows **Ready to scan**, click **Start scan** in this panel.
   It becomes **Stop scan**. Explorer's general acquisition button starts device
   recording, not the amplitude scan.

## Quadrupole offset

The offset channel is an ordinary AMPR channel: switch it ON, in manual (not
equation) control, with its AMPR ON. Start is refused if U/V × the
requested amplitude span leaves the channel's Min/Max or the AMPR module rating,
if its initial value could not be restored there, or if its current setpoint is
not confirmed by the AMPR. At each amplitude, the rails are commanded first, then
the offset (rounded to the AMPR channel's display precision). No point is
acquired (and, in continuous mode, no next command is sent) until the AMPR has
confirmed **that** target by its hardware setpoint readback and its Monitor is
within **Voltage tolerance**; a small offset step is not "confirmed" by tolerance
alone. An offset edited or failing outside the scan, an AMPR OFF or a switch to
equation control aborts the scan. Normal completion restores the initial offset
with the PSU setpoints; Stop/error holds the last value. The HDF5 file keeps the
coefficient, channel and initial value in the setup, and per point the commanded
offset (`offset_target`) and its Monitor (`offset_v`); continuous scans also keep
them in the raw observations.

There is no Input/Output table or generic channel selector to configure.
The **msScan** settings list belongs to this scan; Explorer's global
**Settings → Acquisition** still controls all devices, not just MScan.

## Timing and data

- **Step by step**: wait for the PSU commands to finish and hold all rails within
  tolerance for **Settling time**, then average new DMMR samples during
  **Measurement per point**. Repeat at each amplitude; settle again on return.
- **Continuous**: enter a positive **Sweep rate (V/s)** and **Time step (s)**.
  The command increment is calculated as **Sweep rate × Time step**; direction
  follows From/To. For example, 5 V/s and 0.2 s give increments of 1 V.
  Changing Time step preserves the requested speed, not the old voltage step.
  **Amplitude step** is hidden and ignored in this mode, but retained for stepped
  scans. **Initial/final settling** applies at the first amplitude and on return,
  not between scan points. New amplitudes are requested at Time step intervals;
  DMMR samples are averaged over each actual interval, **including transitions**.
  The last amplitude also gets a full interval before returning.
- This continuous mode is **PC-timed increments, not a hardware analogue ramp**.
  PSU rail writes are sequential. PC jitter can lengthen an interval, never
  shorten the next one to catch up. A full missed interval, or a PSU target not
  reached by the next deadline, aborts without sending the next command.
  The next command is also refused if the current interval has no new valid
  DMMR sample, or the detector cadence has become incompatible with Time step.
  The interrupted point stays NaN; the scan does not continue without data.
- **DMMR data interval** uses valid samples from the selected module, not empty
  recorder ticks or changes in the current's numeric value. It shows the typical
  interval and the **minimum Time step**: the larger of the configured DMMR
  interval and the largest of the last 20 valid-sample intervals, rounded up to
  milliseconds. At least two recent valid samples are required. A shorter Time
  step blocks Start before any PSU command; the setting is never silently changed.
  This is an observed software cadence, not an ADC conversion rate or hardware
  synchronization. **Samples at last point** shows finite/total samples used.
  Allow several samples per measurement/command interval.
- **DMMR interval (ms)** uses Explorer's existing DMMR setting, with the same
  allowed range (normally 100–10000 ms; default 1000 ms). Enter or leave the field
  to apply it; Start also commits pending text before validation.
  Edits are also visible and saved in global Settings, apply to all
  DMMR modules, and remain in effect after the scan; no automatic restoration.
  Existing history is preserved. After a cadence change, continuous Start waits
  for at least two new valid samples to evaluate the new interval. The control
  is locked during a scan; an external interval change aborts the scan.
- 100 ms is a **software setting minimum**, not a hardware conversion limit.
  Serial exchanges and reading other modules add time; use **DMMR data interval**
  for the observed cadence. Sweep rate and Time step never adjust DMMR silently.
  The [DMM-1PA's specified 10 Hz bandwidth](https://www.cgc-instruments.com/en/Products/Experiment-Control/DMMR/DMM-1PA)
  is an analogue response specification, not a maximum sample rate. The maximum
  ADC conversion rate remains unconfirmed.
- **Minimum duration** is a lower bound. Step by step: all settling and
  measurement windows plus final settling. Continuous: one time step per
  amplitude plus initial/final settling. Approach/return, communication,
  scheduling and waiting for the last DMMR sample add time. **Continuous is not
  inherently faster**: duration depends on voltage step and timing.
- Only samples timestamped inside the measurement window contribute. No
  samples, invalid samples or unacquired points remain NaN, never invented
  zeroes or averages hiding invalid data. No background subtraction is applied.

Amplitude **A** means **Vpos = +A, Vneg = −A**, relative to the PSU reference,
not peak-to-peak voltage. Both PSU setpoints are positive magnitudes.
Other outputs sharing these supplies are affected; external offsets are not
included. The AMX frequency, duty cycle and routing stay unchanged.

The result is **current versus amplitude**, not a calibrated m/z axis.
In continuous mode, the axis explicitly says **Commanded amplitude A (V)**:
there is no claim of simultaneous voltage/current measurement or interpolation
of voltage through a transition. Check peak positions and widths against
stepped acquisition on the real setup before relying on a continuous spectrum.
Mass-filter selectivity and calibration must be established experimentally;
a symmetric 50% waveform can give a transmission curve rather than mass peaks.

## Limits and stopping

**Allowed amplitude** is read-only: it intersects channel Min/Max with the
PSU-reported voltage limits for its current range. There is no fixed 350 V cap
in MScan. Its tooltip gives both bounds for each rail. Choose your sweep with
**Amplitude from/to**; software channel limits can restrict it, never override
the PSU ceiling. **After completion** shows the signed initial setpoints, restored only
after a normal finish, even if outside the sweep. Beside each return voltage,
**Iget** is the PSU current measured now, not Ilim or a prediction of current
after the return. It refreshes during the scan without changing the saved return
target; missing or stale current reads as unavailable, never zero. **PSU Ilim**
remains a separate, unchanged limit alongside **Allowed amplitude**.
Missing/stale limits,
incompatible endpoints or an invalid return voltage block Start before any
command. Limits, source identity and configuration are rechecked during the scan.
The scan steps never exceed From/To; a nonaligned endpoint is not added.

Quantized PSU setpoint echoes are observations, not new commands. MScan tracks
request revisions, accepts the first PSU-verified echo for each command, and
still aborts on a new external request or a later hardware setpoint change.
Voltage-regulation tolerance and the PSU driver's write checks are unchanged.
Update MScan and PSU A–E together; old PSU plugins without request tracking
block Start before any command.

Ilim must be valid and remain unchanged. Missing current data, **Iget ≥ Ilim**
or loss of voltage regulation after reaching a target aborts the scan; the
interrupted point remains invalid. Continuous acquisition allows the expected
voltage transition, but both rails must be confirmed within tolerance before the
next deadline. MScan never raises Ilim. The PSU limits current electronically,
with capacity dependent on voltage and temperature
([CGC](https://www.cgc-instruments.com/en/Products/Power-Supplies/19PSU/19PSU-350-2DSW)).
Software sampling cannot rule out fast transients or certify safety.
PSU output current is not the detector's ion current.

**Stop/error holds the last requested voltages; it neither restores them nor
turns HV OFF.** Use device OFF controls for shutdown. Pending scan callbacks
are cancelled. Normal completion restores the original setpoints and checks
settling. Settle timeout and voltage tolerance are available in Advanced.

MScan uses standard channels without initializing devices, enabling HV, changing
ranges/limits or calling DLLs. Equations cannot control the same PSU channels
during a scan. Rail updates are coordinated, not electrically simultaneous.
Controls are locked until acquisition and saving finish.

## Files and upgrades

The `.mscan.h5` file retains planned amplitudes, current averages, sample counts,
NaN gaps, acquisition windows, point status, PSU voltage/current readbacks,
limits, return setpoints, AMX waveform and DMMR module identity. Continuous scans
also retain the detector cadence used for preflight, requested sweep speed,
computed voltage step and configured DMMR interval. Device
configurations are also archived using Explorer's normal mechanism. Continuous
files additionally store **Validation/Continuous**: raw DMMR current/timestamps,
GUI command dispatch times, and timestamped PSU voltage/current/target snapshots
(including unavailable readbacks). These are PC/Explorer timestamps, **not
synchronized ADC times**. Point readbacks are observations at the end of each
command interval, not voltages assigned to every detector sample. Available raw
data from interrupted windows are kept without presenting an incomplete point
as acquired. Explorer may discard old history already copied into the scan;
loss of not-yet-copied samples aborts acquisition.

Old settings without **Scan mode** load as **Step by step**. An unknown mode
blocks Start rather than silently choosing a different protocol. Old continuous
settings derive Sweep rate from the saved Amplitude step / Time step, preserving
the planned amplitudes. Loading settings or data never applies a saved DMMR
interval to the device; that field always reflects the current live setting.
Old MScan settings are converted: `Display` becomes the selected DMMR module,
`Average` becomes the measurement duration in seconds, and the greater of
`Wait`/`Wait long` becomes the single settling duration. A missing detector is
never silently replaced. The former **CH0-CH3** selection blocks Start until
one physical pair is explicitly selected; it never silently changes the wiring.
Old data files, including multiple saved signals,
remain viewable; select their curves below the plot. Importing an old settings
file does not rewrite it. Notes are metadata, not a scan control.
INI settings are read and written as UTF-8, including files with a Windows BOM;
module names and saved selections do not depend on the Windows locale.

To copy this plugin to another machine, keep the whole `mscan/` directory together,
including `_runtime/` and `native/`.

## Origin and license

Originally forked from `esibd/scans/ms/ms.py` in `ioneater/ESIBD-Explorer`, commit
`9945145`; the interface and acquisition protocol have since been rewritten.
Copyright (C) 2021–2026 Tim Esser. Modifications by ESIBD Explorer Plugins
contributors. GPL-2.0-or-later; the original LICENSE and icon are included.
No changes to the installed Explorer or its historical scan.
