# Overview Plugin

Shows the beamline on one page, one card per chamber in beam order, and turns every device ON or
OFF with one button. It only reads: it never changes a setpoint.

Enable the `Overview` plugin in Explorer's Plugin Manager. When installing or updating,
keep the whole `overview/` directory, including `_runtime/`: the entrypoint loads `_runtime/_overview.py`
privately. The plugin imports no other plugin.

## Page

- **All OFF** and **All ON** at the top, with the progress of the running sequence.
- **Devices**: one light per device plugin, as its plugin reports it: green ON, grey OFF, orange
  while starting, stopping, ramping or while the ESI HV discharges, red when its plugin cannot
  confirm its state (communication lost, shutdown unconfirmed, error). Click a device to open its tab.
- **Cards**, in beam order: Source, Funnel 1, Funnel 2, Q1, Q2, Q3, Q4, Detector. Each chamber shows
  its pressure (TPG366 P1–P6) and the values read back by the devices. A setpoint not reached yet is shown in small
  (`set 112.0`). A value the device cannot vouch for (device OFF, output OFF, invalid reading)
  shows `—`, never 0 or an old value.
  - Source: spray current with its window (30–60 nA), HV1 read on the electrospray connector, heater target.
  - Q1 and Q3 (built alike): entry aperture and DC.
  - Q2 and Q4 (built alike): RF amplitude (the two PSU rails of the AMX output: AMX_HD CH2-CH3 for Q2,
    CH0-CH1 for Q4), frequency, x and U of the rod pair, exit aperture, stubs. The title reads
    `Q2 · MScan map` while MScan scans that quadrupole.
  - A channel not created yet (e.g. `Q3_DC`) shows `—`; its tooltip names it.
  - Detector: current, with the last 30 minutes.
- **Footer**: MScan, Transmission and Sync activity, then the last event.

## All ON

After a confirmation, each device's own ON button is pressed, in beam order: TPG366, DMMR, AMPR,
PSU, AMX, ESI. The next device waits until the previous one is ON, and the sequence stops at the
first device that fails. Each device starts exactly as with its own button; the ESI starts with
its HV and heater deselected.

## All OFF

No confirmation. Scans, maps and Transmission stop first (5 s at most; a map can be resumed with
Map…). Then the OFF buttons of every supply are pressed at once: ESI, AMX, PSU, AMPR. DMMR and
TPG366 go OFF once the supplies are OFF, so the descent is watched to the end. A device that fails
or hangs never holds the others; it is reported. The ESI counts as OFF once its outputs are
verified OFF; its HV discharge check goes on (orange light). All OFF also cancels a running All ON.

All OFF is not an emergency stop: the hardware interlocks remain the protection.

## Files

- `<Explorer config path>/Overview.json`: the output address of each channel named on the cards,
  so that renaming a channel in its plugin changes nothing. It may also hold a `cards` list that
  replaces the default layout.
- `<Explorer data path>/logs/overview/overview.log`: device state changes and the steps of All ON / All OFF.
