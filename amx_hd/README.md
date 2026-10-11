# AMX HD Plugin

Drives the **AMX HD** (CGC `HV-AMX-CTRL-4EDH`) oscillator and timer timing from
ESIBD Explorer, monitors live timer readbacks and shows the expected signal on
each output CH0–CH3, with the voltages of the PSUs wired to them. "HD" is only this plugin's name:
CGC's `EDH` means options E + D + **H, "High Frequency Resolution"** (2 digital
oscillators with 10 Hz resolution, 16 pulse generators, timing resolution < 0.1 ns,
jitter < 0.5 ns; [CGC 19AMX options](https://www.cgc-instruments.com/en/Products/Switches/19AMX/Preconfigured/Options)).

This is the standalone HD variant alongside `amx_a/` and `amx_b/`. It is
a **different controller** than the normal AMX: it ships its own vendor DLL
(`COM-HVAMX4EDH.dll`), uses a stream-based API, exposes **timers** instead of
pulsers, has **500 configuration slots** (vs 126), an **8-value housekeeping**
readback, and a distinct state encoding (`STATE_ON = 0x0001`, with a new
`STATE_STANDBY = 0x0000`). See the plan/audit notes for the full normal-vs-HD
delta.

The plugin is self-contained: it bundles its native Rust worker, Python GUI
adapters and reference driver files, and the vendor `COM-HVAMX4EDH.dll`.

## Requirements

- ESIBD Explorer `1.0.2`
- Windows x86-64 for real vendor-DLL hardware communication
- No separate `ESIBD_core` installation is required for the plugin itself

## Activation

1. Open ESIBD Explorer.
2. Download the plugin bundle from the [Releases page](https://github.com/odurif0/esibd-explorer-plugins/releases)
   and extract it into your ESIBD Explorer `plugins` folder.
3. Set the Explorer `plugin path` to that `plugins` folder.
4. Restart ESIBD Explorer.
5. Enable the `AMX_HD` plugin in the Plugin Manager.

## Native Worker

Production communication runs in this plugin's separate native Rust process,
`native/esibd-amx_hd-worker.exe` (Windows x86-64). The supervisor selects
that exact plugin-local file from `native/manifest.json` and verifies its
SHA-256, family and protocol before use. Missing or mismatched files fail
startup; workers are not searched on PATH.

The Explorer GUI and thin facade adapters remain Python. The older Python
device implementation is retained as a reference for tests and notebooks,
not as a production fallback. No external or bundled private Python worker
interpreter, Explorer fork or separate `ESIBD_core` installation is required.
Each plugin instance owns its worker; no running worker is shared with siblings.

Worker logs are written under
`<Explorer data path>/logs/amx_hd/native/amx_hd/`, never inside the plugin.
The v0.5 native port has local mock-DLL, Wine and Explorer 1.0.2 software
validation. Real-hardware qualification remains pending; complete it before
routine experimental use. These checks do not certify physical shutdown.

## Device Configuration

- `COM`: Windows COM port number used by the AMX HD controller.
- `Baud rate`: serial speed passed to the AMX HD driver (default 230400).
- `Connect timeout (s)`: timeout used to establish the transport.
- `Startup timeout (s)`: timeout used for ON/OFF startup and shutdown sequences.
- `Poll timeout (s)`: timeout used for periodic housekeeping reads.
- `Operating config`: operating slot loaded on ON (0..499). Use `-1` only when
  you want to connect first and choose a config later before enabling the device.
- `Available configs`: live list of config slots reported by the connected HD.
- `Frequency (kHz)`: oscillator-0 frequency. The same value is exposed directly
  in the plugin toolbar for routine operation.
- `PSU for CH0-CH1`, `PSU for CH2-CH3`: PSU plugin physically feeding each output
  pair (PSU CH0 (+) = Vpos, CH1 (−) = Vneg). Read-only association used for the
  displayed rail voltages and by MScan; it never connects or controls the PSU.

Toolbar notes:

- `Signal`: saved AMX HD configuration selector (routing and timing).
- `Load now`: immediately loads the selected signal while the device is ON.
- `Freq`: oscillator frequency in kHz. Validate typed changes with Enter, Tab,
  or a click outside the field. They apply while ON, until the next config load.
- channel rows: choose which timers are ON and set their pulse width in us.
  Typed widths use the same validation; intermediate digits are not sent.

ON and `Load now` load the selected configuration's saved timing and update
frequency, widths and enable requests from the hardware readback. Previous
setpoints and unfinished edits are replaced, not sent back to the device.
Channels return to manual mode; equation text is retained for explicit reuse.

Runtime timing notes:

- HD timer delay/width share the same register offsets as the normal AMX
  pulsers (delay `+3`, width `+2` ticks, 100 MHz / 10 ns tick on oscillator-0).
- The `Freq` setting writes oscillator 0 only, and the µs/duty conversions assume
  a 100 MHz clock. When Clock 0 is not `CLK100M` (for example a PLL-tuned config),
  the panel says so; read the CH0–CH3 table for the decoded timing.
- Per-switch coarse (5 ns) and **fine delays** (~11 ps) and the switch sources are
  read and displayed, never written by this plugin.

## Operator Panel

The default view shows the four outputs CH0–CH3, decoded from the live
controller registers, never from a config name or slot number:

| Column | Meaning |
| --- | --- |
| Signal | `Periodic`, `Static`, `Hi-Z`, `Burst`, `External`, `Software`, `Gated`, `Unknown` or `Not enabled` |
| Frequency | Output frequency computed from the oscillator period and its clock |
| Levels | `Vneg ↔ Vpos`, `Vpos`, `Vneg` or `Hi-Z`; measured PSU voltages replace the rail names when a PSU is associated |
| Time (µs) Vpos / Vneg | Exact dwell, only when all switch edge delays are zero |
| Relative timing | Same / opposite / delay in µs relative to the first decoded output |
| PSU | Associated PSU plugin |

Decoding uses CGC's 4EDH signal table (64 sources: logic 0, two software
triggers, `Enb`, the 16 timer outputs, the 16 timer running states, four
oscillators, the 16 mapping-engine outputs and eight digital inputs), as listed by
CGC's `COM-HVAMX4EDH-Control` 1-20 software and used in its configuration files
(`SwitchTrigger0=TimerOut00,Pos`). Enable low gives Hi-Z, enable high with the
trigger low/high gives Vneg/Vpos. Oscillator *k* runs on Clock *k* and timer *t*
on Clock ((*t* mod 8) div 2); periods are register + 2 ticks, widths + 2 and
delays + 3. Mapping engines, bursts, running-state signals, chained, software or
external triggers, a stop source and PLL/counter/divider clocks are never
flattened into a frequency: those outputs are reported as such, with the reason
in the tooltip. Hi-Z never implies 0 V.

The routing registers are read only while the controller is ON (`STATE_ON`),
right after each ON/config load and then every 5 s; timing edits invalidate the
table until the next readback. **Advanced** shows the timer cards (requested
enable and width, register readbacks, trigger and clock, `REGISTERS APPLIED`
when the width and oscillator-0 registers match the request) and the channel
table. Viewing the panel never writes to the controller.

The table states the expected signal from registers; it is not a waveform
measurement. Check it once with an oscilloscope on each output before relying on
it (see `SOFTWARE_TEST_PLAN.md`).

## AMX HD Configurations

AMX HD configuration slots are stored in the controller NVM (up to **500**
slots) and are specific to the actual hardware and firmware content of that
controller. Do not assume that an index seen on another unit, in an old
notebook, or on a normal AMX will exist on this HD controller.

Important points:

- `Operating config` is effectively required to bring the HD to `STATE_ON`
  (`0x0001`). Note: `0x0000` is `STATE_STANDBY` on HD (not ON as on the normal
  AMX).
- Set `Operating config = -1` only when you want the plugin to initialize
  communication first, inspect the available controller configs, and choose one
  before enabling the device.
- The signal shape/routing is intentionally not rebuilt from low-level switch
  controls in the plugin UI. It is chosen by loading a saved AMX HD config.
- The plugin `OFF` action disables or parks the controller with a confirmed
  shutdown path, then disconnects.
- Before choosing a config index, query the controller with the
  [AMX HD hardware probe](../notebooks/amx_hd_hardware_probe.ipynb) or `list_configs()`.

The channels cover timers 0–3; the housekeeping snapshot reads every timer the
controller reports (`get_timer_count()`), and the CH0–CH3 table decodes whichever
timers the outputs actually use. Each timer channel exposes:

- duty-cycle setpoint in percent
- timer delay in ticks
- channel ON/OFF state for whether the timer is actively applied
- measured duty-cycle monitor
- width and burst readbacks

## Connection Failures

A failed or timed-out connection is never reused. A returned failed initial
Open is cleaned up without output commands; `Connection pending` remains until
port cleanup or worker retirement is confirmed. `Disconnected` confirms
communication closure, not hardware OFF or HV discharge.

OFF/close cancels startup. If Open succeeds after a close request, a normal
verified shutdown follows. Only an explicit ON rearms startup; cancelled
requests and old writes are never replayed automatically.

Crashes and hung DLL calls are isolated to this plugin's worker. OFF/close can
cancel, terminate and reap an unresponsive child without stopping other workers.
After retirement, a fresh explicit ON starts a new worker without requiring an
Explorer restart. Process termination does not prove outputs are OFF or
discharged: use the hardware interlock/front panel when shutdown cannot be
verified, and treat the displayed uncertainty separately from port ownership.

## Portability Note

To copy this plugin to another machine, keep the whole `amx_hd/` directory
together, including the bundled `vendor/` and `native/` subtrees and the
`COM-HVAMX4EDH.dll`.
