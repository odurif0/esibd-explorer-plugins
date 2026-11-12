# MScan Plugin

Amplitude scan for an AMX/PSU quadrupole, with the currents of every enabled DMMR module.
Enable the `MScan` plugin in Explorer's Plugin Manager. Its panel is titled
**msScan — AMX/PSU**, distinct from the built-in **msScan** that expects
`AMP_Q1`/`AMP_Q2`. Those historical channels are not used here.

Requires Explorer 1.0.2 and the PSU A–E, AMX A–B or AMX HD, DMMR and (for the quadrupole
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

## Panel

**Status** is the first row, next to **Start scan**: it says **Ready to scan** or why a
scan cannot start, and a greyed **Start scan** shows the same reason in its tooltip. The
other rows are in four groups, in the order a scan is prepared:

- **Setup**: AMX, connectors, the PSU they drive, AMX frequency, the recorded DMMR modules
  and interval, file name, and **Notes**: free text (sample, dilution, conditions) saved with
  each scan and kept for the next ones until changed.
- **Amplitude scan**: allowed range and PSU Ilim, mode, amplitudes, timing, duration.
- **Mass filter**: the AMPR channels of the two rod pairs, k, δ, Minimum U, axis potential
  x, and a **Preview** of what the scan will apply.
- **m/z**: the field radius r0, the RF attenuation factor, the plot abscissa (A or m/z) and the m/z
  range of the scan.
- **Live readback**: PSU rails, rod pair channels, values restored after completion, samples
  at the last point.

A folded group stays folded when settings are reloaded. Groups and displayed names are
display only: settings files and HDF5 scans keep the earlier keys, so files written before
and after this layout read the same. The mass filter rows keep these keys:

| Displayed | Key in INI and `MScan/Settings` |
|---|---|
| Rod pair 1 (AMPR) | `Quadrupole offset` |
| Rod pair 2 (AMPR) | `Quadrupole offset pair 2` |
| U/V ratio k | `Offset coefficient` |
| Resolution δ (V) | `Resolution offset` |
| Minimum U (V) | `Minimum U` |
| Axis potential x (V) | `Fixed offset` |
| Live readback → Rod pairs (AMPR) | `Offset readback` |

## Use

1. Configure the PSU associations in AMX Settings and establish the waveform.
   Turn ON the required PSU rails and AMX. Start DMMR acquisition and recording.
2. In **Setup**, choose **AMX** (`AMX_A`, `AMX_B` or `AMX_HD`) and one pair of **AMX
   connectors**: **CH0-CH1** or **CH2-CH3**.
   **Driven PSU** lists the supply channels swept together; shared outputs are
   identified too. **AMX frequency** comes from the AMX output readback, not
   an assumed oscillator frequency; MScan never changes it. Per-edge compensation delays are allowed
   and retained in the scan metadata; their exact phase is not inferred.
   Changing them during a scan aborts it. Update AMX and MScan together.
3. **DMMR modules** lists the modules recorded: every **enabled** current module of the
   DMMR, by module number, each in its own column and plot line. No module is chosen
   (2026-11-03: a chosen reference module was a trap; that day the peaks were on M03
   while M04 was the reference). Enable or disable modules in the DMMR plugin.
   The point windows follow the DMMR time axis, shared by all modules: a tick counts when
   at least one recorded module has a new reading. A module without a reading in a window
   gives NaN for that point only and never stops the scan; a point is **invalid detector
   data** only when no module read anything. Start is refused when no module is enabled,
   when two modules share a name, or when the DMMR does not acquire and record.
   **DMMR interval (ms)** edits the live DMMR acquisition/recording setting,
   shared by **all** its modules. It does not change the ADC configuration.
4. In **Amplitude scan**, set **Amplitude from / to (V)** within **Allowed amplitude (V)**.
   The voltage range and **PSU Ilim** are the first two rows of the group. **Allowed
   amplitude** also shows what the PSU can reach now, when that is lower: see *PSU headroom* below.
   **After completion** (Live readback) shows each channel's return voltage and live measured
   current, e.g. `PSU_D_CH0: +4 V, Iget 32 mA`.
5. Choose **Scan mode**: **Step by step** (default), with **Amplitude step (V)**,
   or **Continuous**, with **Sweep rate (V/s)** and **Time step (s)**.
   Set the timing fields shown for that mode, described below.
6. Optional, in **Mass filter**: **Rod pair 1 (AMPR)** and **Rod pair 2 (AMPR)** select the
   AMPR channels (module and channel) of the two rod pairs; **None** (default)
   leaves them alone, and only the RF amplitude is swept. During the scan, at every amplitude
   and together with the PSU rails, pair 1 is set to x + U and pair 2 to x − U, with
   U = max(**Minimum U**, **k** × A − **δ**) and x the **Axis potential**.
   k defaults to 0.2 (negative swaps the pairs, 0 is RF only), δ and x to 0 V and
   Minimum U to 0.5 V. Pair 2 alone is refused; pair 1 alone works as before.
   **Preview** shows, before Start, U and both pair potentials at Amplitude from and to,
   says when the filter is RF only or when one pair is not driven, warns when k is above the
   apex of an ideal square wave, and estimates m/Δm when k is near it and δ is 0.
   **Rod pairs (AMPR)** in Live readback shows each channel's AMPR Monitor and setpoint;
   during a scan, its target and whether the AMPR confirmed it. See *Mass filter* below.
7. When **Status** shows **Ready to scan**, click **Start scan** in this panel.
   It becomes **Stop scan**. Explorer's general acquisition button starts device
   recording, not the amplitude scan.

## Mass filter

Each rod pair channel is an ordinary AMPR channel: switch it ON, in manual (not
equation) control, with its AMPR ON. Start is refused if its target over the requested
amplitude span leaves that channel's Min/Max or the AMPR module rating,
if its initial value could not be restored there, or if its current setpoint is
not confirmed by the AMPR. At each amplitude, the rails are commanded first, then
the rod pairs (rounded to the AMPR channel's display precision). No point is
acquired (and, in continuous mode, no next command is sent) until the AMPR has
confirmed **that** target by its hardware setpoint readback and its Monitor is
within **Voltage tolerance**; a small step is not "confirmed" by tolerance
alone. A rod pair edited or failing outside the scan, an AMPR OFF or a switch to
equation control aborts the scan; a settling timeout names the channel not ready, with
its Monitor and target. Normal completion restores each initial value with the PSU
setpoints; Stop/error holds the last values. The HDF5 file keeps k (`coefficient`), x
(`fixed_v`), δ, Minimum U and each pair's channel, sign and initial value in the setup, and per
point the commanded potentials (`offset_target`) and their Monitors (`offset_v`), one column per
pair; continuous scans also keep them in the raw observations.

Only U, of opposite sign on the two pairs, filters masses. The **axis potential x** is the
potential of the quadrupole axis, so it sets the ion energy: keep it at the value the rods hold
outside a scan. k = U/V
sets the line in the stability diagram: for an ideal 50 % square wave nothing passes above
about 0.215, and the resolution m/Δm is about 0.157 / (0.2153 − k), so about 10 at 0.20
and 30 at 0.21. These values assume that the amplitude on the rods equals A: raise k until
the signal disappears at k = c to measure the ratio, c / 0.2153. With δ = 0, peaks widen
in proportion to A; with k at that limit, they keep about the same width, about 6.4 × δ in
A. δ must stay small: it is not the axis potential, and adding it with the wrong sign closes the
filter. As on commercial instruments, set k and δ on a light and a heavy known ion: the straight
line only approaches the ideal curve between them.

Rod potentials, scan law, parameters and their physical effect are described in
[docs/mscan_quadrupole.pdf](../docs/mscan_quadrupole.pdf). k = U/V is not the α of textbook
quadrupole papers, where α is the Mathieu parameter a and U/V = α/(2q).

**Minimum U** keeps the DC from reaching zero: at zero DC every heavy ion would pass, so a spectrum
would start with a wide plateau. Commercial instruments do the same ("Zero Blast Suppression" in
the SRS RGA manual). Peaks whose tip lies below (Minimum U + δ) / k are not filtered reliably,
and a small signal can remain at the start of a scan, where ions are not filtered as in theory.

Explorer's own msScan has no such formula: it sweeps one channel, and any DC follows
through channel equations, without confirmation. MScan refuses a channel driven by an equation.

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
  scans. **Initial/final settling** applies at the first amplitude of each pass and
  on return, not between scan points. New amplitudes are requested at Time step intervals;
  DMMR samples are averaged over each actual interval, **including transitions**.
  The last amplitude also gets a full interval before returning.
- This continuous mode is **PC-timed increments, not a hardware analogue ramp**.
  PSU rail writes are sequential. PC jitter can lengthen an interval, never
  shorten the next one to catch up. A full missed interval, or a PSU target not
  reached by the next deadline, aborts without sending the next command.
  The next command is also refused if the current interval has no new valid
  DMMR sample, or the DMMR cadence has become incompatible with Time step.
  The interrupted point stays NaN; the scan does not continue without data.
- **DMMR data interval** uses valid DMMR ticks (at least one recorded module with a new
  reading), not empty recorder ticks or changes in the current's numeric value. It shows the typical
  interval and the **minimum Time step**: the larger of the configured DMMR
  interval and the largest of the last 20 valid-sample intervals, rounded up to
  milliseconds. At least two recent valid samples are required. A shorter Time
  step blocks Start before any PSU command; the setting is never silently changed.
  This is an observed software cadence, not an ADC conversion rate or hardware
  synchronization. **Samples at last point** shows valid/total samples of each module.
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
  amplitude plus one settling per pass and the final settling. Approach/return, communication,
  scheduling and waiting for the last DMMR sample add time. **Continuous is not
  inherently faster**: duration depends on voltage step and timing.
- MScan reads the recent DMMR history only (the last minute at least, more when a point window or
  the DMMR interval needs it). Until 2026-11-03 it converted and sent the whole history to its
  worker at every 50 ms poll: about 120 ms of Python and 2.5 MB of JSON for 100 000 records, which
  saturated Explorer's GUI thread over a day; recordings then bunched up into empty readings (25 to
  55 % of the ticks that day) and repeated timestamps.
- Explorer stamps each DMMR record with the PC clock. A record whose stamp is not later than every
  earlier one (a clock adjustment, or two records within the clock resolution) is skipped with its
  values; **DMMR data interval** says how many. On 2026-11-03 one such record made MScan refuse
  every later scan (`Invalid or nonmonotonic DMMR timestamps`), even after restarting the DMMR
  plugin, which keeps its history. Each scan file lists the records skipped during the scan in
  Validation `dmmr_skipped`, as [stamp, delay behind the latest earlier stamp]: 0 s means two
  records within the clock resolution, a negative delay a clock stepped back (an empty list: none).
- Only samples timestamped inside the measurement window contribute. The DMMR
  records NaN at each recorder tick without a new reading; those are skipped and
  the point is the mean of the readings present, with **valid / total** samples
  saved per point (2026-10-26 lab: about 5 of 66 samples per point were such
  ticks, and requiring every sample finite left every point NaN). A window with
  no reading, an infinite reading or an unacquired point stays NaN, never an
  invented zero. No background subtraction is applied.

Amplitude **A** means **Vpos = +A, Vneg = −A**, relative to the PSU reference,
not peak-to-peak voltage. Both PSU setpoints are positive magnitudes.
Other outputs sharing these supplies are affected; external offsets are not
included. The AMX frequency, duty cycle and routing stay unchanged.

The measured axis is **amplitude**; the m/z is computed from it (see *m/z* below). The plot
shows every recorded module in **pA** with a legend (files keep amperes); the
x axis names the driven supply, e.g. **PSU D (V)**. In continuous mode it says
**PSU D, commanded (V)**:
there is no claim of simultaneous voltage/current measurement or interpolation
of voltage through a transition. Check peak positions and widths against
stepped acquisition on the real setup before relying on a continuous spectrum.
Mass-filter selectivity and calibration must be established experimentally;
a symmetric 50% waveform can give a transmission curve rather than mass peaks.

## m/z

**m/z range** shows the m/z at Amplitude from and to, and **Plot axis** = **m/z** draws the
spectrum against it (user request 2026-11-03). A map line whose axis is x, the entry stub or the
frequency is always plotted against that axis: its m/z is not a function of it. For an ideal
50 % square wave:

    m/z [Th] = 4 e A / (q r0² (2π f)²) / u ≈ 2.444 · A / q · (4 mm / r0)² · (500 kHz / f)²

- **f** is the AMX output frequency, read from the AMX (and saved with each scan).
- **q** is the centre of the stable band on the scan line a = 2 (U/A) q, with U = max(Minimum U,
  k × A − δ) at each point, as the rods are driven; 0.536 at k = 0.2 and δ = 0. Without rod pairs
  or with k = 0 (RF only) there are no peaks: the axis is the **low-mass cut-off** (q = 0.712).
  With one driven pair the DC between the pairs is unknown and no m/z is given; above the apex
  (about 0.2153) nothing is transmitted.
- **RF attenuation factor** is the RF amplitude actually seen by the rods divided by A (1: no loss).
  It enters q and U/V, so the filter closes at k = 0.2153 × factor; the preview and Start warn
  above it. To measure it, raise k on a peak until the signal disappears: factor = that k / 0.2153.
- **r0 (mm)** is the field radius, from the axis to the inner surface of a rod. m/z varies as
  1/r0², so 1 % on r0 is 2 % on m/z. Calibrate on a known peak by setting r0 to
  r0 × √(m/z shown / m/z known); one peak fixes the scale at every amplitude, frequency and k.

**Defaults, calibrated on 2026-11-03** (Q2, AMX HD, 400–500 kHz) with the Agilent ESI tuning mix
G2421A: the M03 peaks are its ions at m/z 622, 922, 1522 and 2122, and they fall on the same m/z at
500, 450 and 400 kHz. The band widths at k = 0.20 and the transmission at k = 0.21 give an RF
attenuation factor of **0.978**; r0 then fits the 15 peaks with **2.625 ± 0.003 mm**, and each
lands within 0.6 % of its mass. That r0 is effective: 5 % above the nominal 2.5 mm of Q2 (5 mm
between opposite rods), so the nominal value would read m/z about 10 % too high. The real
waveform, round rods and the rod spacing can each account for that; it is not measured. Measure
both again for another quadrupole, AMX or frequency range.
Resolution measured that day (m/Δm at half height): 12–16 at k = 0.20, 35–55 at k = 0.21.
A scan is converted with its own settings, saved in the file, not with the panel's current ones.
The file keeps the swept quantity as the axis, and an amplitude line adds `Validation/mz` (NaN
where not transmitted or unknown), with the formula and the values used in its `Definition`
attribute. A map line that sweeps x, the entry stub or the frequency does not sweep A, so it gets
no `Validation/mz`: its axis, the recorded f and `Validation/setup` say what m/z that line had.
MScan gives m/z, not the mass: that needs the charge.

## Limits and stopping

### PSU headroom

The PSU regulates its output below an internal supply; the difference is its **dropout**,
which the PSU plugin reads at every poll. The internal supply falls as the RF load draws
more current, and once the dropout is gone the output stays below its setpoint. On
2026-11-03, PSU_A in **Half range** went from 256 V at 0 mA to about 248.5 V at 150 mA: the
output was exact with 2.7 V of dropout, 0.3 V low with 0.9 V and 2 V low with 0.05 V, and
a 50–300 V scan waited 30 s at 250 V before stopping on a generic settling timeout. MScan
now handles this in three places:

- **Before Start**: output + dropout is the most the rail can reach, since the load only
  grows with A. Start is refused when **Amplitude to** is above it minus 2 V, with the rail,
  its range and what to do (switch the PSU to **Full range**, or lower Amplitude to). The
  load still to come lowers it further (4.3 V for a 206 V rise that day, about 2 %): above
  it minus (2 V + 3 % of the rise from the present output), **Status** adds a warning, e.g.
  `Warning: PSU_A_CH0: about 252 V reachable now (Half range), less under load: Full range
  recommended.`, also written to the log at Start. Start stays allowed. MScan never switches
  the range itself: it is a configuration change of a live HV supply, for the operator.
  **Allowed amplitude** shows this value when it is the lower limit, and **PSU rails**
  shows each rail's headroom and range.
- **During the scan**: a rail out of tolerance with less than 1 V of dropout, on two
  different PSU readings in a row, stops the scan at once with the cause, e.g.
  `PSU_A_CH0 cannot reach 250 V: PSU_A has no headroom left (dropout 0.05 V at 248.56 V,
  146 mA, Half range). Switch PSU_A to Full range or lower Amplitude to.`
- **In the file**: `Validation/rail_dropout` keeps the lowest dropout of each rail over
  each point window, and `Validation/low_headroom` flags points measured with less than
  2 V, where the RF amplitude may not have been regulated. The log lists them at the end.

Full range raises the internal supply, and the heat the PSU must dissipate; the PSU lowers
its current limit when it heats up. A PSU that does not report its dropout is not judged.

### Allowed amplitude, limits and stopping

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

## Plot, readback and files

- **Passes**: the same sweep is repeated this many times in one file, in both modes.
  Step by step: each pass returns to the start like any other step (commanded, settled,
  measured). Continuous: each pass ends like a single sweep (last window closed and
  drained), then the PSU returns to the first amplitude without a timed deadline and
  settles for `Initial/final settling` before the next timed sweep; detector samples
  between passes stay in the raw arrays but belong to no point. Every point of every
  pass is kept, the plot shows each pass faintly and the mean of the passes per module
  (`(mean of N)` in the legend), and the duration estimate includes every pass.
  `Validation/setup` records `passes` and `points_per_pass`; the arrays hold the passes
  one after the other.
- **Autoscale** (title bar of the plot): the view follows the data, also during a
  scan. Zooming or panning switches to manual scale; a click follows the data again.
  **Data to Clipboard** copies the amplitude and each current in pA.
- **PSU rails** (Live readback) shows each driven rail live: measured voltage (Monitor) and the
  setpoint confirmed by the PSU.
- Each scan is saved to `<Data path>/MScan/<YYYY-MM-DD>/` as
  `<date>_<time>[_<File name>]_PSU-<X>_<from>-<to>V.h5`, for example
  `2026-10-26_14-46-27_essai1_PSU-D_0-150V.h5`. **File name** is optional; letters,
  digits, `-` and `_` are kept. Nothing comes from Explorer's session fields
  (substrate, molecule, session type, measurement number), and an existing file is
  never overwritten (`_2` is added).
- Explorer refuses to close the plot while a scan runs. On 2026-10-26 that refused
  close still broke every later plot update, the last one before saving included,
  and the scan was lost. The plot now changes nothing on a refused close, and a
  plot fault never prevents the save.

## Map of Q2 (v0.5.10)

A **map** runs a plan file line by line, with no operator, to find, prove and record the best
scan of the quadrupole (`docs/quadrupole_optimization_plan.md`). Every line is an ordinary MScan
scan, started and saved like one from Start, with all of its checks; the decisions belong to the
MScan worker (`mscan_map`, no device access). Group **Map** of the panel:

- **Map plan**: the plan file (TOML). Templates in `plans/`: `q2_check_2h.toml` (stopping cut and
  the P1 ladder up to the R_target region, about 2 h), `q2_check_6h.toml` (P1 and P5 ladders, about
  6 h) and `q2_map.toml` (the full map, every key with its default: f 250–640 kHz, about 45–55 h,
  Ilim at least 330 mA; P9 at 900 kHz and P10 at 200 kHz were dropped on 2026-11-12).
  Change a copy. Until 2026-11-04 the first two were named after their planned 1 h and 4 h.
- **Map status**: stage, line, point and what the map waits for; the same in `status.json`. A
  finished map shows when and after how many lines (`stopped at 14:35 after 0 lines: …`).
- Buttons, between Map plan and Map status (named buttons of the group, not toolbar icons, since
  2026-11-12: on 2026-11-11 the play icon of Start scan was taken for the start of the 2 h plan):
  **Check** (asks for the plan file, opened on the loaded one, and checks it against the
  instrument: what to change before Start, and the time of the lines the Q2 simulator decides, each
  timed like on the instrument), **Simulate** (the loaded plan against the simulator, no hardware),
  **Start map / Stop map** and **Resume** (a stopped map, from its folder). Simulate and
  Start map use the loaded plan without asking, and their tooltips name it; they ask for a file only
  when no plan is loaded. To run another plan after a map, Check it first. A running map keeps the
  plan it started with. The toolbar keeps Start scan alone.

Before Start map, prepare the instrument as for a scan: AMX HD ON with its RF config (50 % duty,
100 MHz clock), PSU in Full range with an Ilim above what Check asks (set it on the PSU yourself:
the RF current follows from f and A, and Ilim is the operator's safeguard, which MScan reads but never
changes), AMPR ON with `Q1_DC`,
`Ap_Q1_Q2`, `Stub_Entry_Q2`, `Stub_Exit_Q2` and both rod pairs in manual control, DMMR recording,
TPG366 acquiring, ESI spraying from the syringe. Select the rod pairs in **Mass filter** as for a
scan. Start scan is locked from the click on Start map until the map ends: its first engine
call can take about 30 s on the worker's first launch after an install (an antivirus check), and
on 2026-11-11 a Start scan clicked during that wait stopped the map at its first engine call. A
second click on Start map during the start stops the map once it has started. From Start map on,
nothing waits for a human:

- **Lines.** The stopping cut along x (it fixes the ion energy E = e·(x_stop − x)); at each design
  point a ladder in k up to the closing, two repeats, a verification line at the computed (k*, δ*)
  and a cut along the entry stub; then the outer search in (f, E), an out-of-sample check, a local
  check and the best scan, repeated. A reference window on 922 every five lines corrects the drift.
- **Settings a line does not sweep** are set first and confirmed: the AMX frequency (through the
  AMX HD plugin, which keeps the duty cycle), `Q1_DC`, the entry stub (x + 0.42·(Ap − x)) and
  `Stub_Exit_Q2` (x − 15 V). MScan's own settings (k, δ, x, timing, file name, notes) are set for
  each line and return to the operator's values at the end.
- **New line axes** of the MScan worker: x (stopping cut), the entry stub and the AMX frequency
  (frequency lines at fixed A and U), besides A. An amplitude line spans the best-range ions it
  holds ± `a_margin` (20 %) of their amplitude, inside `a_range_v` (P1: 50–248 V of 20–348 V).
  Amplitude lines use the measurement-driven step:
  coarse (A/(2·R_max)) where the selected module (`detector`, M03) is flat, every fine point (A/(8·R_max))
  where it changes by more than 3σ of the point means and 10 % of the local height; the points
  never measured are `skipped`. σ is never less than 50 fA, the noise between neighbouring points
  of a flat M03 signal (2026-11-04; with 1 fA, half of the fine grid was measured).
- **Time.** A line takes 15 s plus its points at `point_s + settle_s + point_overhead_s` each
  (0.8 + 0.3 + 0.9 = 2.0 s; 2026-11-04: 2.02 s and 15.5 s). Once its lines hold 100 points, the map
  times its own: the estimates and the time left follow the instrument.
- **Faults.** A failed line (spray outside ±40 % of its value during the last reference line that
  found its peak, or at Start before the first one, a worker or detector fault,
  no new point for 5 min, or a line longer than all the points of its axis would take at
  `point_s + settle_s + 2 s` each) is measured again. A line slower than its estimate is not a
  fault (2026-11-04: three good lines were stopped at twice an estimate that counted 1.1 s per
  point). The same line failing
  three times, or three failed lines within one hour, stop the map. A line a limit forbids (PSU headroom,
  Ilim, AMPR limits) closes that setting and the map goes on. Before each line the spray must be in
  its window; the map waits up to 30 min for it.
- **Safe stop** (Stop, a fault, the end): the AMX frequency, `Q1_DC`, the stubs and MScan's settings
  return to Start. The PSU rails and the rod pairs stay as the last line left them: restored at the
  end of a completed line, held after a line fault, as for any MScan scan. Ranges, Ilim and the ESI
  are never touched. The PC is kept from idle sleep for the whole map.
- **Files.** Each line is a normal file in `<Data path>/MScan/<date>/` named
  `<date_time>_<map>-L<line>-<role>_PSU-X_<span>.h5`, with the line request in `Validation`
  (`map_line`). The map folder `<Data path>/MScan/maps/<date_time>_<name>/` holds `plan.toml`,
  `journal.json` (the whole state; `.bak` is the previous one), `status.json`, `map.log`, `map.h5`
  (every line: axis, signal, sigma, spray, request and analysis), and at the end `report.json`
  (laws, points, search, checks, best scan), `figures.png` and `best_settings.ini` (load it to
  repeat the best scan). `figures.png` also draws the stopping cut and the reference peak against
  the ESI current, so a map stopped before its ladders still shows what it measured.
- **Resume** continues from the last finished line of the journal, after a safe stop, an Explorer
  crash or a power cut; a line cut in the middle is measured again.

## Files and upgrades

The scan file retains planned amplitudes, current averages, sample counts,
NaN gaps, acquisition windows, point status, PSU voltage/current readbacks,
limits, return setpoints, AMX waveform and DMMR module identity. **Validation**
lists the recorded modules (`modules`, JSON, in column order); `samples` and `finite_samples`
have one column per module, and `rail_dropout`/`low_headroom` keep the PSU headroom. Files
written before 2026-11-03 name a reference module (`reference_channel`) and the others
(`extra_channels`) instead. Since v0.5.9 a scan also keeps:

- **Validation/Context/ESI/…** and **Context/TPG366/…**: each enabled, real channel of the ESI
  (spray currents, HV, heater) and of the TPG366 (pressures), one value per point with its
  `Unit`. MScan reads what each plugin shows (the measured value: the monitor for the ESI) at
  every poll and averages it over the point window. These devices need not be recording in
  Explorer; one that is not acquiring gives NaN, and `Status` says so before Start. Recorded
  only, never used to correct the currents.
- **Validation/Samples**: every DMMR record behind the point means: `time`, `point` (its point
  index), `current` and `range` (DMMR measurement range), one column per module. NaN: no new
  reading, or range unknown.
- In `Validation/setup` (JSON): `rf` (`frequency_hz`, `duty_percent` per selected AMX output,
  null when unknown), `software` (plugin version, SHA-256 of `mscan_plugin.py`,
  `_runtime/_native_scan.py` and of the MScan worker in `native/manifest.json`), `notes` and
  `context` (device, channel and unit of each context column).
- **Notes** in the `MScan` attribute `notes`.

Continuous scans
also retain the detector cadence used for preflight, requested sweep speed,
computed voltage step and configured DMMR interval. Device
configurations are also archived using Explorer's normal mechanism. Continuous
files additionally store **Validation/Continuous**: raw DMMR timestamps and currents
(`detector_current`, one column per recorded module),
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
Old MScan settings are converted: `Average` becomes the measurement duration in seconds,
and the greater of `Wait`/`Wait long` becomes the single settling duration. A saved
`Display` or `DMMR module` selection is ignored: every enabled module is recorded. The former **CH0-CH3** selection blocks Start until
one physical pair is explicitly selected; it never silently changes the wiring.
Old data files, including multiple saved signals,
remain viewable; every saved signal is plotted. Importing an old settings
file does not rewrite it. Notes are metadata, not a scan control: a legacy `[Notes]` entry
is kept as such, and **Notes** in Setup (key `Scan notes`) replaces it in new files when set.
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
