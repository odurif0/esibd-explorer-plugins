# Transmission Plugin

Optimizes the ion transmission through the beamline (inlet, ion funnel, quadrupoles
Q1–Q4 and the apertures between their chambers), stage by stage, with a
noise-tolerant surrogate model and verified A/B decisions. It optimizes either the
total ion current or one mass selected by a quadrupole.

Enable the `Transmission` plugin in Explorer's Plugin Manager. When installing or
updating, keep the whole `transmission/` directory: the entrypoint loads its private
runtime from `_runtime/` (`_engine.py`, the optimizer; `_beamline.py`, which turns
the simple settings into a configuration; `_simulator.py`, a simulated beamline;
`_log.py`, the logs). The
plugin imports no other plugin: it drives the channels of the device plugins only
through Explorer, like a user edit, so each device keeps its own ramps, limits and
OFF logic. MIT license (see `LICENSE`).

## Use

The panel has three steps, then **▶ Optimize**.

1. **Beamline** — **Configure…** once: which channel drives each setting (inlet
   voltage; funnel RF amplitude, DC gradient and exit; offset and RF amplitude of
   Q1–Q4), which picoammeter channel measures each aperture (funnel → Q1, Q1 → Q2,
   Q2 → Q3, Q3 → Q4) and the collector after Q4, and optionally which gauges to
   watch with their pressure range. An RF amplitude set by two PSU rails, as in
   MScan (Vpos = Vneg = A, unsigned magnitudes), uses both channels with gain +1.
   Unmapped settings and apertures are simply skipped.
2. **Target** — **Total ion current**, or **Selected mass**: choose the quadrupole
   used as the filter (usually Q2 or Q4; any quadrupole can be used), then
   **Pick on spectrum…** and click the peak, either on a saved MScan file (the
   filter is recognised from MScan's rails) or on a quick sweep of the filter made
   here. The peak position and its half-width are filter amplitudes in volts, as in
   MScan. **The m/z calibration belongs to MScan**; this plugin never converts.
3. **Stages** — tick the stages to run (Inlet + funnel, Q1, …, Q4, Final
   refinement) and the exploration: **Fine**, **Normal** or **Wide** windows.

The devices driving the settings must be ON, and those measuring the currents and
pressures ON **and recording**. The **Devices** line of step 1 shows each device the
optimization needs (✓ ready, or OFF, not recording, transition in progress, not
available). When one is not ready, **Optimize** (panel or toolbar), **Turn ON…** or a
quick sweep lists what is missing and proposes to turn the devices ON and start their
recording: nothing happens without confirmation. Turning ON works as each device's own
power button, so a supply then applies the setpoints set in its plugin. The
optimization or the sweep starts once every device is ready (at most 5 min; **■ Stop**
cancels the wait and leaves the devices as they are). Transmission never turns the ESI
ON, and leaves a device in transition alone; a channel whose plugin is not loaded is
named with its plugin (enable it in the Plugin Manager, then restart Explorer). A PSU
output gate that is OFF is reported (**output off**) but never switched from here:
turn it ON in the PSU panel. A gate that goes OFF during a run stops it without any
further command.

During the run the panel shows the stage, the
measurement count, the best gain so far and an upper bound of the time left; **■ Stop**
returns to the start of the stage in progress. At the end, one line sums it up (for
example `Collector: 210 pA → 820 pA (×3.9). 5/6 stage(s) improved`): **Keep** the
new settings or **Revert** to the settings before the run, step by step.

**Simulation** runs the same steps on a built-in simulated beamline (three species,
a Q2 mass filter, noise and drift) without touching any device. In Explorer's test mode,
Transmission always simulates (the box is then ticked and locked). **Advanced…** opens
the full configuration (below) for anything the simple settings do not cover.

The choices are kept in Explorer's configuration folder (`Transmission.beamline.json`,
`Transmission.state.json` and, for the advanced mode, `Transmission.toml`).

## Philosophy

- **Start from what works.** Each stage starts from the current setpoints and only
  explores a window around them, clipped to the channel Min/Max of the device plugin.
- **Small verified steps.** Every move is split into steps no larger than the setting's
  `max_step`; each step waits until the readback reaches the setpoint.
- **Beam order.** Stages follow the beam (funnel, Q1, …, Q4); the final stage refines
  the coupled offsets together with finer windows.
- **Adopt only what is proven.** A new setting is kept only if paired A/B measurements
  against the stage start show a significant gain; otherwise the stage start is restored.

Default windows (Normal; Fine × 0.4, Wide × 2.5): inlet ±20 V in 2 V steps, DC
gradient and offsets ±10 V in 1 V steps, RF amplitudes ±15 % in 2 % steps of their
start value. The final refinement uses at most the Fine windows.

## Criterion

For each stage, the criterion is the **transmitted current**: the sum of the currents
measured *after* the stage aperture (following apertures and the final collector).
From the second stage on it is **normalized by the incoming flux** (the sum of all
collectors), which compensates spray fluctuations. Ions lost on the quadrupole rods are
never measured, so a better ratio could hide fewer ions: a normalized gain is adopted
only if the A/B pairs also show more ions after the aperture.

A low current on an aperture is ambiguous: ions may pass through it, or no ions may
arrive at all. The **charge bookkeeping** resolves it: the flux reaching the aperture
is the aperture current plus the downstream current. When it drops below
`lost_fraction` (20 %) of its value at the stage start, or below the noise
(`min_current`), the point is **beam lost**: invalid, and counted as zero by the
model. It can never be taken for good transmission.

### Selected mass

The filter quadrupole is set to the picked peak and its RF amplitude is not optimized
(it selects the mass). For the stages up to the filter and for the final refinement,
the criterion is the **height of the peak**: a short sweep of the filter amplitude
(5 points over ± the half-width) around the tracked peak centre, fitted by a
parabola. The peak is measured only on the currents both behind the filter and after
the stage aperture: ions of the selected mass landing on the stage's own exit
aperture are not transmitted by it. A peak that moves in amplitude when an upstream
setting changes is therefore never mistaken for a change of transmission; its centre
is tracked within ± twice the half-width of the picked position. The stages after the
filter maximize the (already filtered) current. A spectrum around the peak is recorded
before and after the run.

## Method (one stage)

1. Read the start setpoints; bounds = window ∩ channel Min/Max.
2. Measure the start twice (reference flux and noise).
3. Probe each setting by ±1 `max_step` (local slope).
4. Search with the selected strategy until the stage budget or convergence; every
   `reference_every` measurements the best point is measured again (drift tracking).
5. **A/B verification:** alternate best and start `verify_pairs` times (alternating
   order cancels drift). The gain is the mean paired difference; it is adopted only if
   positive and larger than `significance` standard errors.
6. Each measurement: stepwise move, readback settling, `settle_s`, then the mean and
   standard error of the samples recorded during `average_s` after the move (Explorer's
   timestamped channel histories: the monitor for DMMR/PSU, the value for TPG366). A
   window is measured again, never filled, when a channel has no valid sample (an
   acquisition or GUI stall) or a criterion current has fewer than two (no noise
   estimate); three such windows in a row stop the run.

Explorer timestamps device samples on its GUI thread, when it records them. While that
thread is busy, recordings wait and then run in a burst, much closer together than the
device interval. Two rules keep this out of the measurement:

- **No redraw during a window.** Before a window opens, the optimizer waits for a
  redraw in progress to finish. The plugin postpones its own redraws until the window
  closes, and redraws at most every 3 s (always at a stage change and at the end).
- **A burst is one observation.** Samples closer together than a quarter of the device
  interval count once, with the freshest value; only the remaining samples give the
  mean and the standard error. DMMR, TPG366 and ESI already record each reading once
  (a repeated reading is NaN); this rule covers any other device.

The A/B verdict uses the spread of the paired differences, not the window standard
errors; these weight the points of the Gaussian-process model and of the peak fit.

The budget is 12 measurements per setting + 16 (8 + 16 for a selected mass, where each
measurement is a 5-point peak sweep). With the default timings (1 s settling, 2 s
averaging), the Inlet + funnel stage takes about 4 min for the total ion current and
about 15 min for a selected mass.

## Algorithms

- `bayesian` (default): local Bayesian optimization in a trust region. A Gaussian
  process (Matérn 5/2, one length scale per setting) models the criterion with the
  measured standard error of each point plus a learned noise floor, and time as an
  extra dimension for slow source drift. The next point maximizes
  `mean + 2 × uncertainty` inside a region centred on the best *predicted* point and
  measured in `max_step` units: it starts at ±2 steps, doubles after 3 successes
  (up to ±8) and halves after repeated failures; below ±0.5 step the stage converges.
- `coordinate`: the classic manual method, automated: one setting at a time, a
  five-point line and a quadratic fit, with a shrinking step. Robust and easy to read.

## Safety

- The **soft interlocks** only stop the optimization: a watched pressure outside its
  range, or any monitored current above `max_current` (a discharge), returns step by
  step to the stage start. **The protective interlock belongs in the hardware** (for
  example TPG366 relays wired to the CGC interlock inputs).
- A device switched OFF, a channel disabled, a setpoint changed outside the optimizer,
  a readback that never settles or missing data stop the run **without any further
  command**. So do, as in MScan, a device plugin busy with its initialization, an ON/OFF
  transition or ramp, or an HV configuration loading, and a PSU output whose current
  reached its limit Ilim (short, discharge or overload; Transmission does not switch HV
  OFF).
- Before each setpoint, the optimizer waits until the device plugin has finished
  applying the previous one (PSU apply worker), at most `settle_timeout_s`.
- Windows are clipped to each channel's Min/Max and, for a PSU, to its hardware voltage
  limit and to 0 V (unsigned magnitudes).
- **ESI channels are never driven**: their HV discharge is not confirmed and a
  temperature setpoint starts the heater. They are not offered in Configure… and the
  advanced configuration refuses them; they can still be measured.
- **Stop** returns step by step to the start of the stage in progress; completed stages
  keep their verdict. **Revert** returns every channel driven by the last run, the
  filter included, to its value before that run.
- A quick sweep moves only the filter amplitude, in steps of the sweep spacing, then
  returns it to its previous value; Stop returns it at once.

## Advanced configuration

**Advanced…** edits `Transmission.toml` with **Check** (rules and Explorer channels),
**From the simple settings**, **Import…** and **Export…**; tick *Use this
configuration* to run it instead of the panel settings. Example:

```toml
polarity = 1              # 1: collected ions read as positive currents
strategy = "bayesian"     # or "coordinate"
settle_s = 1.0            # wait after the readbacks reach their setpoints (s)
average_s = 2.0           # averaging window (s)
readback_tolerance = 0.5  # |readback - setpoint| accepted as settled (V)
max_current = 1e-6        # A: larger |current| = discharge (soft interlock)

[pressure]                # soft interlock only, [minimum, maximum] in mbar
P_funnel = [2.0, 6.0]

[target]                  # optional: a mass selected by a quadrupole
filter = "Q2"
channels = { "PSU_B Vpos" = 1.0, "PSU_B Vneg" = 1.0 }  # MScan rails: Vpos = Vneg = A
center = 520.0            # peak amplitude A (V)
width = 30.0              # half-width (V): the peak sweep spans ± width
measure = ["DMMR A3", "DMMR A4", "DMMR Collector"]  # currents behind the filter
# points = 5, track = 2 * width, max_step = width / 2, spectrum_points = 21, spectrum_span = 3.0

[[stage]]
name = "Funnel to Q1"
aperture = "DMMR A1"                         # aperture closing this stage
downstream = ["DMMR A2", "DMMR Collector"]   # their sum is maximized
normalize_by = []                            # optional incoming-flux channels
peak = true                                  # criterion = height of the [target] peak
# budget = 40                                # measurements

[[stage.knob]]
name = "Funnel exit"
channels = { "PSU_A CH0" = 1.0 }  # channel = start + gain * delta
window = [-20.0, 20.0]            # delta range around the start
max_step = 2.0                    # largest change per move
# relative = true                 # window and max_step as fractions of the start value
```

A knob may drive several channels: symmetric rails use gains `+1` and `-1` (or `+1`
and `+1` for unsigned magnitudes), a common offset uses gain `1` on each channel. A
channel may appear again in a later stage (for example a final global refinement); a
stage never drives the filter channels. Other settings: `settle_timeout_s`,
`min_current`, `lost_fraction`, `verify_pairs`, `significance`, `reference_every`,
`poll_s`, `seed`.

## Display and data

The display shows the criterion relative to each stage start (lost-beam points as grey
crosses, verification points as stars), the selected peak before/after (or the setting
positions of the current stage), the current balance (on the aperture, after it, and
reaching it) and the pressures.

Each run is saved in the session folder (`…_transmission.h5`): the configuration, the
beamline description and the result (JSON, with the spectra and the before/after
currents) as attributes, and for each stage the measurement index, time, kind
(`initial`, `probe`, `explore`, `reference`, `verify`), validity and reason, criterion
with standard error, incident and transmitted flux, setting displacements, setpoints, the currents and
pressures (mean, standard error, samples) and, for a selected mass, each peak sweep.
Explorer's own settings and device configuration are saved in the same file.

## Logs

For troubleshooting and for tuning the method later, the plugin writes JSON Lines logs
(one JSON object per line, written at once, so they survive a crash or a sleep) in
Explorer's data folder, never inside the plugin: **`<Data path>/logs/transmission/`**
(by default `~/ESIBD Explorer/data/logs/transmission/`). **Logs…** opens the folder;
copy it together with the session data.

- `runs/transmission_<date-time>_<kind>.jsonl`: one complete file per optimization
  (`run`, or `simulation`), quick sweep (`sweep`) and revert (`revert`). The newest 200
  files are kept, within 500 MB (a full run takes about 1 to 5 MB). The HDF5 file of a
  run names its log (`log_file` attribute) and the log names the HDF5 file.
- `transmission_session.jsonl`: GUI actions and outcomes across sessions (plugin start,
  beamline saved, MScan file opened, peak selected, advanced configuration, devices not
  ready / turned ON / declined / ready / timed out, start refused, run start and end,
  Stop, Keep, Revert), rotated at 1 MB with 5 backups.

Each line has `t` (Unix time), `seq` and `event`; non-finite numbers are `null`. A run log
contains:

| Event | Content |
| --- | --- |
| `header` | plugin and Explorer versions, Python, platform, SHA-256 of the plugin code (provenance), panel state, beamline, full configuration, notes, HDF5 file, and every channel used (device, ON, recording, value, readback, Min/Max, unit, flags) |
| `run_start` / `run_end` | strategy, options, stages, target; status, reason, duration, counters, final setpoints and the traceback of an error |
| `stage_start` / `stage_done` | windows, start, steps, budget, currents used; verdict, gains, duration, final strategy state |
| `step` | every setpoint step sent, with the settling time, polls and readbacks |
| `measure` | every averaging window: mean, standard error and samples per channel, attempt, channels with too few samples, delay before the closing sample, and per channel the samples recorded, valid, collapsed from a burst and repeated, the device interval, the largest gap without a sample; Qt-thread latency |
| `evaluation` | every measured point: displacements, setpoints, currents, pressures, criterion, incident and transmitted flux, validity and reason, peak sweep and fit |
| `ask` / `tell` | the strategy state: Gaussian-process length scales, noise, prediction and uncertainty of the chosen point, trust-region radius, fit time (or the coordinate-search axis and scale) |
| `verify` | the A/B pairs, gains, verdict and the "fewer ions" guard |
| `sweep`, `snapshot`, `restore`, `revert_*` | spectra, before/after currents, returns to a stage start or to the settings before the run |
| `gui_stall`, `gui_slow`, `plot_slow` | Explorer's Qt thread blocked (> 0.6 s, and whether a window was open), a channel request delayed (> 0.5 s), a slow redraw |

Read a log with `json.loads` line by line, or `pandas.read_json(path, lines=True)`.
Logging never stops the optimizer: if the folder cannot be written, the run continues
and Explorer's console says why.

## Limits

- The optimum is **local**, around the start: deliberate, for safety.
- An unstable spray degrades every method; normalization helps.
- Couplings between stages call for the final refinement.
- The simulator is a test bench, not a physical model; validate on the instrument.
- No m/z: the target is a filter amplitude in volts. Convert with MScan's calibration.
