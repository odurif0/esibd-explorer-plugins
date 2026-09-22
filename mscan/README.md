# MScan Plugin

Standalone fork of Explorer's `MassSpec` (`msScan`), using the existing Scan
interface, plotting and HDF5 format. The original plugin remains unchanged.
Enable the `MScan` plugin in the Plugin Manager. Requires Explorer 1.0.1 and the
PSU A–E / AMX A–B plugins from the same release bundle. No DLL or bundled runtime.

## Use

1. Configure the PSU associations in the AMX settings and establish the waveform.
2. Turn on the required PSU rails and AMX. Start recording the detector's channel.
3. Select the AMX and **AMX connectors** wired to the quadrupole. **Driven PSU**
   shows which supply rails will follow A; **Fixed frequency** comes from the AMX.
4. Set **Amplitude from / to / step (V)**.
5. Choose one detector channel under **Measured signal**, directly from Explorer's
   available channels. Set **Settling (ms)** and **Integration (ms)**, then start.

The visible **Scan axis** is amplitude in volts; **Measured signal** is the detector
readout. Only the selected signal is acquired, not the other dropdown choices.
A missing or ambiguous channel blocks starting; it is never silently replaced.
Acquisition settings are locked until the scan has finished.

Amplitude **A** means **Vpos = +A, Vneg = −A**, relative to the PSU reference.
Both PSU channel setpoints are positive magnitudes. Any other outputs sharing
these supplies are affected too. External common-mode offsets are not included.
The AMX frequency, duty cycle and routing remain unchanged. Channels controlled
by equations cannot simultaneously be controlled by MScan.

This version records **current versus amplitude**, not a calibrated m/z axis.
The operator must establish mass-filter selectivity and calibrate the mass axis;
a 50% symmetric waveform can give a transmission curve rather than mass peaks.
The historical MZCalculator is deliberately not applied to an axis in volts.

## Acquisition and stopping

- Uses `Channel.value` for setpoints and `Channel.monitor` for PSU rechecks.
  It never initializes a device, enables HV, changes ranges or limits, or calls a DLL.
- Settling / Large-step settling are continuous periods **after** PSU commands
  finish and both measured rails reach Voltage tolerance. Large step (V) sets
  the threshold for the longer wait. Settle timeout and Voltage tolerance are
  available in Advanced. A timeout aborts the scan.
- Only detector samples timestamped inside the averaging window contribute.
  The detector must keep acquiring and recording. No samples, invalid samples,
  or unacquired points remain NaN, never invented zeroes or a mean that hides NaN.
- Loss of voltage validity during acquisition, a changed waveform/association,
  manual intervention, OFF or a changed device aborts the point.
- Normal completion restores each original PSU setpoint and verifies settling.
  **Stop/error holds the last requested values; it does not restore or turn HV off.**
  Use the device OFF controls for shutdown. Pending scan callbacks are cancelled.
- The scan steps never exceed From/To; a nonaligned endpoint is not added.
  The two PSU writes are coordinated in software, not electrically simultaneous.

The `.mscan.h5` file retains planned amplitudes, raw detector averages, sample
counts, NaN for missing data, point status, actual acquisition windows, PSU rail
rechecks and the AMX waveform/source snapshot. It also uses Explorer's normal
configuration archive. There is no background subtraction or hidden calibration.
Existing settings keep their selected detector; the old `Display` key remains
compatible on disk. Old files with several recorded signals can still be opened
and their curves selected below the plot.

To copy this plugin to another machine, keep the whole `mscan/` directory together.

## Origin and license

Forked from `esibd/scans/ms/ms.py` in `ioneater/ESIBD-Explorer`, commit `9945145`.
Copyright (C) 2021–2026 Tim Esser. Modifications by ESIBD Explorer Plugins
contributors. Distributed under GPL-2.0-or-later; the original LICENSE and icon
are included. No changes to the installed Explorer or its historical scan.
