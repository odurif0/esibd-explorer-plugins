# Physical checks after the software update

The software was exercised with simulated instruments. This is not physical
qualification of a controller, load, sensor, calibration or protection limit.

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
  A shutdown/closure failure must remain unconfirmed, not become Disconnected.
  Software OFF is not a substitute for an independent physical safety check.
- **Sleep prevention (all device plugins):** with a device ON, run
  `powercfg /requests` in an administrator prompt: SYSTEM must list
  "ESIBD Explorer: <device> on COM<n> is connected" for each connected device,
  and the entry must disappear after a confirmed OFF. Leave the PC idle beyond
  its sleep delay with a device ON: it must stay awake.
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
- **PSU:** check the `Ramp step (V)` / `Ramp step interval (s)` defaults against
  the measured output slew before relying on them.
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
- **PSU_D:** check the selected module and reference/readback mapping at zero.
  Do not introduce an arbitrary software offset to match the plot. If disagreement
  remains, retain targets, native readbacks and independent meter readings.

## ESI heater / pressure–temperature

The approved heater characterization remains **50 W / 100 °C**, armed by default.
The pressure–temperature notebook is also **armed by default** and uses the separate
approved 175 °C protocol. Both share a per-COM lock, historical guards and explicit
operator recovery; they must not own the same instrument concurrently. Old kernels
and old software copies are not retrospectively protected by the new lock.

For PT175: baseline OFF 60 s; 30, 40, …, 170, 175 °C; applied limits at most
22 V / 10 A / 50 W and no more than fresh controller maxima. Each stage requires
60 s within ±0.2 °C with absolute OLS drift below 0.1 °C/min, then 300 s continuous
stable observation. First complete qualification must occur within 600 s of the
command; the absolute stage deadline is 900 s, never extended by resets. A gap of
more than 10 s between temperature observations (`MAX_SAMPLE_GAP_S`) restarts the
window; successful ESI reads occasionally take up to ~5 s on the real controller.

At 175 °C the effective band is **174.8–175.0 °C**. A hold is attempted, not
promised: the first observed temperature above 175 °C stops heating and leaves the
stage incomplete. This is a sampled software check, not a hardware thermal cutout.
Pressure is recorded continuously; thermal qualification does not prove pressure
equilibrium. Normal completion includes 900 s temperature/pressure observation
after verified heater OFF, not a declaration that the assembly is cold.

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
three-round discharge criterion or assuming an ABI/ADC fault. PT175 keeps every
discharge round in `metadata.json` (`discharge_observations`). During the
2026-10-05 run, routine diagnostics on the selected negative range read about
−0.06 V on both modules throughout, while the final check read 1324 V (HV1) and
110 V (HV2) on that range after reselecting it. `notebooks/esi_adc_probe.ipynb`
records every fresh conversion after each channel selection, with the raw
ready/overflow flags and a native call trace: run it together with an independent
HV measurement of both outputs.
