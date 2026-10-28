# Physical checks after the software update

The software was exercised with simulated instruments. This is not physical
qualification of a controller, load, sensor, calibration or protection limit.

## Native worker qualification

The Rust migration requires a new qualification on the Windows standalone
Explorer 1.0.2 executable. Wine DLL loading, mock SDK tests and Linux PTY tests
do not qualify Windows serial communication or physical output state.

- Install complete folders, including `native/manifest.json`, the worker,
  corresponding sources and notices. No private or system Python is required
  by a worker. Record the binary/DLL hashes, Explorer version and COM mappings.
- First test one device at zero with output gates disabled and independently
  verified safe. Check startup, cancellation while connecting, readbacks, OFF,
  discharge where applicable, and a fresh ON without restarting Explorer.
- With two devices safely connected, terminate only one worker in Task Manager.
  Its plugin must stop recording, invalidate readings and become reconnectable;
  the other plugin must keep communicating. A forced disconnection must finish
  without waiting for the failed device's SDK lock. Reconnecting must create a
  new worker; an old completion must not change the new session's controls.
- Worker termination is not hardware OFF. Check the physical outputs separately
  before reconnecting. A failed shutdown with healthy communication must remain
  available for an explicit OFF retry, not falsely become a successful shutdown.
- Check `<Data path>/logs/<device>/native/<family>/` and that no new log is written
  to the installed plugin folder. Keep all failure traces and physical readings.
- Repeat for AMPR A/B, AMX A/B, AMX HD, DMMR, ESI, PSU A/B/C/D/E and TPG366.
  Then qualify MScan and Transmission on simulated channels before approved,
  bounded hardware runs. Transmission's Rust optimizer is not numerically
  identical to the former SciPy implementation; compare measured outcomes,
  bounds, Stop/recovery and saved files rather than identical search paths.

## Install once

Close Explorer, CGC utilities and instrument-owning notebook kernels. Keep all
existing logs, results and unfinished-run markers. Install each complete plugin
folder, not just its entrypoint; keep `notebooks/` beside `esi/`, `dmmr/` and
`tpg366/`. Do not choose a new output folder to evade an unfinished run.

Run `install_explorer_fixes.py` with the Python environment used by Explorer.
It backs up and updates the host's cursor formatting, channel-name fallback and
UTF-8 readers. If an old HV activation notebook exists beside the installer,
it is archived byte-for-byte outside the plugin tree, not run. Incompatible host
sources are refused. Restart Explorer; no instrument is opened by the installer.

## Checks at zero / without heating

- **All device plugins:** verify COM assignments and channel/module mappings.
  Select zero targets and disabled output gates before connection; do not load
  an unreviewed startup profile. Check ON, OFF, reconnect and window closing.
  A shutdown failure with usable communication must remain unconfirmed and
  retryable. A reaped worker must show `Disconnected: shutdown unconfirmed`,
  never a claim that hardware shutdown succeeded.
  Software OFF is not a substitute for an independent physical safety check.
- **Sleep prevention (all device plugins):** with a device ON, run
  `powercfg /requests` in an administrator prompt: SYSTEM must list Explorer's
  native-worker sleep request. Test each device in isolation; its request must
  disappear after OFF or confirmed process reaping. With two connected devices,
  closing one must leave the other's request active. Leave the PC idle beyond
  its sleep delay with a device ON: it must stay awake. Reaping releases the
  sleep request but never confirms physical output shutdown.
- **TPG366:** match its real USB baud setting; compare all six statuses and units
  with the controller display. Test OFF/ON and reconnection after a controller
  power cycle. Inspect missing/off gauges without unplugging a powered gauge.
  Invalid channels must remain NaN/dashes while valid ones continue. Keep logs
  from any NAK/timeout; the reported intermittence is not physically explained yet.
  A NAK is now retransmitted ("answered NAK ... retransmitted" log); a persistent
  NAK or a timeout must cost one sample (NaN gap, "recovered" log), and
  three consecutive failures must still stop acquisition.
- **AMPR:** at a safe low voltage and slow ramp, press OFF, then click ON/OFF again
  during the descent: outputs must go to 0 V at once and the PSU be disabled.
- **PSU worker (fixed 2026-10-12):** connect each PSU with outputs OFF and leave it
  polling for several minutes: no "native worker released" message and no exit
  `3221226356` (`0xC0000374`) in `<Data path>/logs/psu_x/native/psu/`.
- **PSU Disable interlock (fixed 2026-10-12):** with one output enabled at a safe low
  voltage, no load, open the interlock loop, then click `Disable interlock`: both outputs
  must read OFF and stay at 0 V on a meter, the panel show `Off (not monitored)` and the
  state leave `ST_ERR_ILOCK`, without a Python error in the console (it raised on
  `PRINT.INFO` before, and left the outputs enabled).
- **PSU:** check the `Ramp step (V)` / `Ramp step interval (s)` defaults against
  the measured output slew before relying on them. Last values: with outputs OFF and
  `Operating config` `-1`, set Vset/Ilim, disconnect, power-cycle the PSU and
  reconnect: the panel must propose the previous values in italics and the PSU must
  keep its own until a field is validated or the output is turned ON.
- **Resume after a crash (all device plugins):** at safe low setpoints with outputs
  ON and recording, end the Explorer process from the Task Manager (a simulated
  crash), check with independent meters that the outputs stay unchanged, restart
  Explorer: each device must reconnect by itself with "Resumed after an Explorer
  crash … adopted", show the running values, restart recording, and no output may
  change (no ramp, no zeroing, no config load). Then close Explorer normally and
  restart it: nothing may reconnect by itself.
- **Explorer 1.0.2 data files:** save ESI and DMMR data (Ctrl+S and on closing)
  and reopen them; the ESI export failed with Explorer 1.0.1 before v0.3.0. Check that
  the DMMR history length now matches its configured storage.
- **DMMR:** verify all eight addresses and ranges, OFF/ON and a power cycle between
  connections. Save Explorer and `dmmr_protocol_com*.jsonl*` logs for receive errors.
  For the zero check, document open/grounded inputs and shielding, leave gain/bias
  unchanged, and keep the whole output folder. Compare offset/noise/drift only
  with the applicable specification or agreed CGC criteria. The five-minute
  disconnected pause changes temperature; it is not an isothermal reconnect.
- **MScan–DMMR:** verify that missing replies are missing samples, never held values.
  After recovery, a recent large gap must constrain Time step until replaced by
  finite replies; a window without a new valid reply must not advance the scan.
  First check mappings at zero. Nonzero PSU/HV trials require their own approved
  envelope; stopping MScan does not turn off HV outputs.
- **AMX HD outputs (2026-10-11):** with the PSUs at zero (or disconnected) and an
  oscilloscope on the logic/trigger monitor of each output, load a known config
  (for example CGC's `10kHz->SwitchSym`, Configuration273). CH0–CH3 must show
  10 kHz, `50 / 50` µs, CH1/CH3 opposite to CH0; compare frequency, dwell and
  relative timing with the scope. Load a static config (`Out0=Vpos`) and a Hi-Z
  config; then a PLL-tuned config, which must show Unknown and the Clock 0 notice.
  Associate PSU_x in Settings and check the displayed rail voltages against the
  PSU panel. Only afterwards select `AMX_HD` in MScan, at zero amplitude first.
- **AMX panel sketch (2026-10-13):** with the same scope set-up, on AMX HD and on
  AMX A/B (config 79: 500 kHz, 1 µs per level, CH0/CH2 together), compare the
  expected-switching sketch (Advanced) with the scope (edge order,
  Vpos/Vneg dwell); a static or Hi-Z config must give a flat or dashed row. Select another config while ON: `Load now` must turn amber until
  it is loaded. Switch Explorer to the light theme once: all panel text must stay
  readable.
- **AMX frequency keeps symmetry (2026-10-22):** same scope set-up, PSUs at zero.
  Load config 79 (500 kHz), then type 450 kHz: the scope must show 1.11 µs at each
  level (was 1.00 / 1.22 µs), the panel `450.4505 kHz (222 × 10 ns)`, `Vpos (%)` 50
  and P0 `Duty` 50.00 %; only the P0 timing row is shown, P1–P3 in Advanced.
  Type 600 then 500 kHz: back to 1.00 / 1.00 µs, P0 width
  register 98, delay register 7. Type 25 % in P0 `Duty`: 0.5 µs at Vpos. Set P0
  `Osc change keeps` to `width µs`, change the frequency: the width stays in µs.
  On AMX HD, Configuration273 at 9 kHz must stay 50 / 50 within one tick and the
  oscillator line must report the odd period.
- **MScan quadrupole offset (2026-10-08):** select the AMPR offset channel and a
  small coefficient; in a short stepped scan, check on an independent meter that
  the offset follows U = U/V × A at each point and returns to its initial value.
  Then check that a continuous scan's Time step leaves the AMPR time to confirm each
  target (otherwise it aborts, naming the offset).
- **AMPR responsiveness (2026-10-21):** with the 12-module AMPR_A, time ON (click to
  "Starting AMPR ramp-up", was ~18 s), an edit to its green confirmed Status (was
  2–7 s) and OFF after the ramp-down; the Monitor column must still refresh about
  once per second. OFF during a running poll must ramp down, never log "Could not
  acquire lock before AMPR ramp-down". Device/voltage/interlock state must show
  real values, not "Unknown"; opening the interlock while ON must log "AMPR left
  ST_ON" with the interlock state.
- **Native logs and GUI lag (2026-10-22):** after a session of several hours with
  AMPR, AMX, PSU and DMMR ON, each `logs/<plugin>/native/<family>/*.jsonl` must hold
  the whole session (a few MB, `summary` lines each minute, `status`/`values` on
  vendor calls). With the AMX panel open while an AMX connects to a powered-off
  unit, Explorer must stay responsive (no "operation queue timed out" traceback).
  A DMMR closed by Explorer's display-lag protection must log "Explorer display
  lag: …" naming the lagging device.
- **PSU last values after shutdown (2026-10-22):** in manual mode set CH0 to e.g.
  100 V / 1 mA with the output OFF, then OFF (shutdown) and ON again: the panel
  must show 100 V and 1 mA in italics and the channel table 100 V, while the PSU
  front panel still shows 0 V. Nothing changes on the PSU until a field is
  validated or the output turned ON; turning it ON ramps to 100 V.
- **PSU readback lag (2026-10-21):** on PSU_D/PSU_E with an output OFF, alternate a
  channel between 0 and 100 V a dozen times: no "setpoint readback … does not match"
  error and no outputs switched OFF.
- **PSU_D:** check the selected module and reference/readback mapping at zero.
  Do not introduce an arbitrary software offset to match the plot. If disagreement
  remains, retain targets, native readbacks and independent meter readings.

## ESI heater / pressure–temperature

The approved heater characterization remains **50 W / 100 °C**, armed by default.
The pressure–temperature notebook is also **armed by default** and uses the separate
approved protocol up to 170 °C, with a 175 °C safety stop. Both share a per-COM
lock, historical guards and explicit operator recovery; they must not own the same
instrument concurrently. Old kernels and old software copies are not
retrospectively protected by the new lock.

For the PT protocol: baseline OFF 60 s; 30, 40, …, 170 °C; applied limits at most
22 V / 10 A / 50 W and no more than fresh controller maxima. Each stage requires
60 s within ±0.2 °C with absolute OLS drift below 0.1 °C/min, then 300 s continuous
stable observation. First complete qualification must occur within 600 s of the
command; the absolute stage deadline is 900 s, never extended by resets. A gap of
more than 10 s between temperature observations (`MAX_SAMPLE_GAP_S`) restarts the
window; successful ESI reads occasionally take up to ~5 s on the real controller.

The first observed temperature above **175 °C** stops heating and leaves the stage
incomplete. The regulation always overshoots its setpoint by a few mK (1–5 mK on
every stage of the 2026-10-05 run), so each target must keep its ±0.2 °C band
below this stop; the notebook refuses any other program. A 175 °C stage stopped
that run at 175.001 °C, which is why the last stage is now 170 °C. This is a sampled
software check, not a hardware thermal cutout.
Pressure is recorded continuously; thermal qualification does not prove pressure
equilibrium. After verified heater OFF, temperature and all six pressures are
recorded until a valid measured temperature is ≤30 °C (`COOLING_TARGET_C`),
without a fixed cooling duration. The threshold-crossing sample must be saved;
a temperature above 30 °C after 900 s must not end recording. Stop, invalid
sensor readings and pressure faults must still preempt cooling and trigger
verified shutdown. Reaching the threshold does not declare the assembly safe to touch.
For this heater-only experiment, do not query HV voltage/current ADCs or require
HV discharge verification. Check HV gates disabled and targets zero throughout;
verify heater OFF and output disable before closing the ESI port. Record the
shutdown scope and `hv_discharge_verified=null`, never a fabricated HV discharge
confirmation. Simulated failing HV ADC calls must not affect acquisition or
closure. The driver's default disconnect must retain its HV discharge checks
for other callers; existing historical uncertainty markers remain authoritative.

**ESI start (fixed after 2026-10-07):** with HV1 selected at a nonzero target, close
Explorer, restart it and press ON: the panel must show every output OFF with the
target kept, and an independent meter must read no HV until HV1 is selected. Type a
new target and click an output selector before pressing Enter: the target must be
taken, without any error in the Explorer log.

**ESI Load config (fixed 2026-10-11):** before routine use, list the controller's
slots and check that none is saved with the global enable, a module gate or a
nonzero HV target. If such a slot exists, load it only with the HV outputs
disconnected and an independent meter on them: the outputs must end OFF (gates off,
targets zero) even when the log reports that the ramp-step patch was refused.

**ESI electrospray readback (2026-10-08):** set each card's `ADC connector` to the
electrospray's connector. With an independent HV probe on that connector, compare
`ADC readback` at 0, 500, 1000 and 2000 V (and with the spray running): note the
deviations, then switch POS/NEG on the card at a fixed target and check that the
probe does not move (measurement only) and how long `settling…` lasts before the
readings agree again. Unplug nothing and do not change the target during the switch.

Supervise the run, keep the PC awake and verify the supplied load/materials and
physical safeguards. Stop, invalid temperature, no valid pressure, dialogue errors
or uncertain output state stop the protocol. TPG366 is not a vacuum interlock.
Limits remain configured after stopping; neither notebook saves them to NVM.

**HV discharge is still unconfirmed on the real ESI.** Gates OFF and targets zero
have not explained the elevated native ADC readings. Preserve the guards and logs;
operator recovery acknowledges physical safety/closed old processes but does not
turn a false shutdown report into true. Nonzero HV commissioning is not authorized
by these heater protocols. Its next discriminating checks need independent meter
observations and the saved native ADC/range/readiness trace, without relaxing the
three-round discharge criterion or assuming an ABI/ADC fault. The PT notebook keeps every
discharge round in `metadata.json` (`discharge_observations`). During the
2026-10-05 run, routine diagnostics on the selected negative range read about
−0.06 V on both modules throughout, while the final check read 1324 V (HV1) and
110 V (HV2) on that range after reselecting it. `notebooks/esi_adc_probe.ipynb`
records every fresh conversion after each channel selection, with the raw
ready/overflow flags and a native call trace: run it together with an independent
HV measurement of both outputs.

**ESI OFF with settled readings (2026-10-22):** on 2026-10-22 the first conversions after
each ADC selection read k × 110.7 V (up to 1328 V, also on a never-enabled HV2) and the
check never confirmed; HV1 also really held about 280 V 8 s after its OFF from 1300 V.
The check now discards the first two conversions and the first second after each
selection (as the electrospray readback). With HV off for minutes, OFF must confirm in
about 6–8 s. If it still fails, the native log now holds every
discarded conversion (`values` of `GetHVsupplyOutputVoltage`): note how many follow a
selection before the readings agree, and compare with an independent HV probe.

**ESI non-blocking OFF at 10 V (2026-10-23):** the settled check worked, but HV1 really
decays slowly (247 V 10 s after OFF from 1000 V, 10 V after 41 s, 2.8 V after 66 s), so
the 1 V / 60 s check never confirmed; the operator chose 10 V. Right after HV1 at 1000 V,
press OFF: within about 3 s the button must be OFF and the badge `OFF: HV discharging`,
with readings every 10 s; ON must be refused meanwhile; `ESI HV discharge verified after
N s` must follow after about 45–50 s, then `Disconnected`. Repeat, closing Explorer during
the check: it must wait for the verdict, with no "unconfirmed" message. Check the
discharge time against an independent HV probe on the spray connector.

**AMPR first ON after OFF (2026-10-23):** the first command to module 0 after reconnecting,
or after the PSU was enabled, sometimes got no reply within 1.5 s (-10) and the start
failed; the second ON worked. Repeat OFF, wait one minute, ON, ten times. Each lost reply
must now be printed (`got no reply within …`) and the start must succeed; keep
`<Data path>/logs/ampr_a/` (native log and `ampr_startup_com*_*.log` DLL traces) of every
occurrence, to tell a busy controller (CTS inactive) from a reply lost on the line.

**MScan scans and files (2026-10-26):** scans 011 and 013 were "completed" but every point
was NaN (any NaN recorder tick invalidated a point), and closing the plot during scan 012 lost
it. With DMMR M00 and M02 enabled at 150 ms, run a stepped scan 0–150 V on PSU_D: points must
appear live in pA, both modules in the legend, x axis `PSU D (V)`, and `Samples at last point`
about `61 valid / 66 total`. Click the plot's close button during the scan: refused, the scan
continues and is saved. Zoom during the scan: the scale stays manual; click Autoscale: it follows
the data again. `PSU readback` must follow the PSU Monitor. The file must be
`<Data path>/MScan/<date>/<date_time>[_<File name>]_PSU-D_0-150V.h5`, with both modules.
Then set `Passes` to 3 on a short range: the status shows `pass k/3`, the plot each pass
faintly and the mean; the file holds 3 × the points and `passes = 3` in its setup. Repeat in
Continuous with `Passes` 2: after each sweep the PSU returns to the start, settles, then sweeps
again; no `time step missed` error between passes, and both passes are in the file.

**PSU link and last values (2026-10-26):** with `Operating config` 20 selected but not loaded,
set CH0 to e.g. 100 V / 1 mA with the output OFF, close Explorer, restart, connect: the panel
proposes 100 V and 1 mA in italics (it showed zeros because a selected config disabled the
proposal); `Load config` drops the proposal. Tick `Link CH1 to CH0`: CH1 receives CH0's Vset
and Ilim and its fields grey out; a value validated on CH0 reaches both channels. With the
link ticked, CH0's `ON` box switches both outputs OFF then both ON (one ramp), and so does
CH1's; a `Range` change on either channel switches both (after the discharge); untick it and
CH0's `ON` box and `Range` act on CH0 alone. The tick is kept after a restart and sends
nothing then.

**Transmission and MScan with AMPR channels (2026-10-26):** every AMPR channel had `Manual`
unticked with no equation, and Transmission refused `Entry_IF1`. A channel without equation
is now accepted; one driven by an equation is refused with `driven by its equation; tick
Manual`.

**ESI Close communication during the check (2026-10-26):** right after an ESI OFF, click
the ESI `Close communication` (square) button while the check runs: Explorer must stay
responsive and print that communication closes when the check ends.

**Interlock loops (2026-10-28):** the 2026-10-22 logs show both PSUs with monitoring off,
the output-connector loop closed on PSU_D and open on PSU_E, the BNC loop open on both.
With every output OFF, connect each PSU: its panel `Interlock` row reads `Off (not
monitored) · loop: output …, BNC …`. Unplug the AMX output cable fed by that PSU: if the
`output` loop changes and the log prints `PSU interlock loop at the output connector
opened`, the AMX connector's pins 3 and 5 reach that PSU; plug it back (`closed`). Connect
the AMX HD: the `Loaded config` line and the log give `interlock function ON|OFF` for the
loaded config (OFF expected with the CGC configs).

## Transmission optimizer

- **Simulation first:** tick **Simulation**, run the total ion current, then a selected
  mass (Pick on spectrum… → quick sweep of Q2 → click the 520 V peak); check the
  display, the saved `…_transmission.h5`, **Keep** and **Revert**. No device is touched.
- **Beamline:** in **Configure…**, map each setting, the detector (its current
  channel and position), the spray current and the gauges; the panel must show
  "✓ n settings · detector after Q4 · spray 10–30 nA · n gauges". Give each driven
  channel realistic Min/Max in its device plugin: the windows are clipped to them.
  For each RF amplitude choose its AMX output only: the two PSU rails linked to it
  must move together (Vpos = Vneg = A) and are never offered as separate settings.
  With the detector behind aperture 1–4, only the stages before it are offered.
- **Selected mass:** open a real MScan file in Pick on spectrum…; the filter must be
  recognised from the rails. Then make a short quick sweep around the peak at a safe
  amplitude: the filter must move in steps of the sweep spacing and return to its
  previous amplitude (also after Stop). Amplitudes are volts; m/z calibration stays
  in MScan.
- **Devices:** with the needed devices OFF (supplies at safe setpoints), the Devices
  line must list them; Optimize must propose to turn them ON, do nothing on Cancel, and
  on confirmation turn them ON through their own plugins, start the DMMR/TPG366
  recording, then start the run. Check the ramps and setpoints in each device plugin.
  With the DMMR interval at 1 s the Devices line must recommend 5–10 Hz; an ESI read
  slower than 1 Hz must be flagged too. The ESI is only read, never switched.
- **Dry run on hardware:** one stage, **Fine**, devices ON and current/pressure
  channels **recording**. Verify that each request appears as a small step on the
  device, that the readbacks settle before each measurement, and that Stop returns to
  the stage start.
- **Faults:** during a run, switch a driven device OFF and edit a driven setpoint by
  hand: the run must stop with "is OFF" / "changed outside the optimizer" and send no
  further command. On a PSU-driven element, lower Ilim just below the measured output
  current at a safe low voltage: the run must stop with "reached Ilim" (HV stays as is). Set a watched gauge maximum below the actual pressure: the soft
  interlock must stop and restore the stage start. The hardware interlock remains the
  protective one.
- **Spray:** during a run, let the spray leave its range (or fluctuate by more than
  20 %) for a few seconds: the status must show "Waiting for a stable spray", no
  setting may move, and the run must resume once the spray is back. Keep it unstable
  for more than 2 min: the run must stop with "No stable spray" and restore the stage
  start.
- **Criterion:** on one stage, compare the detector current (current panel) with the
  DMMR readings; a beam pushed off the detector must appear as "beam lost", never as
  an improvement. In mass mode, check on the before/after spectra that the peak stays
  at the picked amplitude.
- **Full run:** widen the windows progressively (Normal, then Wide); keep the HDF5
  files of each run and the logs (**Logs…**: `<Data path>/logs/transmission/`), one
  file per run, quick sweep and revert, with every step, window and decision. Check
  for `gui_stall` events: a blocked Explorer GUI also delays the device histories.
