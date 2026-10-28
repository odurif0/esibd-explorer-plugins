# Rust Migration

Target: all 15 plugins retain their Explorer 1.0.2 Python entry points and GUI,
but run device communications and scan engines in independently supervised native
workers. No Explorer fork, private Python, sibling imports, shared running daemon,
or symlinks are required in the final distribution.

This is the software-validation ledger for v0.5, not a hardware qualification. Existing
Python implementations remain explicit reference/notebook implementations, not
production fallbacks. Hardware verification must be reported separately
from mock-DLL, simulated-instrument, Linux, and Wine tests.

## Work Items

| Scope | Implementation | Integration | Local validation | Hardware validation |
| --- | --- | --- | --- | --- |
| IPC, supervision, ABI, build | Implemented | Implemented | 36 real-process fault tests; all deployment targets built | Not applicable |
| ESI | Implemented | Implemented | 69 controller tests; crash/blocked OFF/late Qt payloads | Not tested |
| PSU A/B/C/D/E | Implemented | Implemented | 52 controller tests; isolated recovery checks | Not tested |
| AMX A/B | Implemented | Implemented | 43 combined AMX/HD controller tests; 60 adapter tests | Not tested |
| AMX HD | Implemented | Implemented | Same combined suite; independent HD ABI and process | Not tested |
| AMPR A/B | Implemented | Implemented | 29 controller tests; isolated recovery checks | Not tested |
| DMMR | Implemented | Implemented | 38 controller tests; isolated recovery checks | Not tested |
| TPG366 | Implemented | Implemented | 17 Rust + 36 adapter/PTY tests; 30 Explorer 1.0.2 Qt cases | Not tested |
| MScan | Implemented | Implemented | 43 native tests; 36 paired executions; 138 NumPy plans; 48 adapter cases | Not tested |
| Transmission | Implemented | Implemented | 25 engine tests; 19 packaged lifecycle tests; 15 Explorer 1.0.2 Qt cases | Not tested |

The local target-host environment is isolated from the owner's installed Explorer.
Passing Python-reference tests do not validate native hardware communication.
DLL controller tests inject explicit mock SDK replies; PTY serial tests use a
pseudo-terminal; scan tests use simulated Explorer instruments. None opens a real
HV device. Windows workers for all nine families and Linux workers for TPG366,
MScan and Transmission have been rebuilt after the final Rust edits. Deployment
manifests all identify raw source data
`1a62ffda5efd160944f07a5411b70644700e17a6ce96ccd14438952c3519f82f`
(v0.5.4 below; v0.5.5 changes no Rust source or worker).

## v0.5.5 Update (2026-10-28)

Python plugin changes only: the 15 workers, their manifests and the source archive are
those of v0.5.4. None is validated on hardware yet; `SOFTWARE_TEST_PLAN.md` lists each check.

- **PSU link:** with `Link CH1 to CH0` ticked, the `ON` box or the `Range` of either
  channel sets both channels in one transaction (a range change still waits for the
  measured discharge). The tick itself never switches an output or a range, which would
  restart CH0's ramp; it says when they differ.
- **PSU interlock loops:** the panel `Interlock` row adds `loop: output closed|open, BNC
  closed|open`, the raw inputs of `GetPSUState` (bits 22 and 23), also while `Interlock
  monitoring` is off, and a change is logged. In the 2026-10-22 logs, with monitoring off
  on both PSUs, the output loop was closed on PSU_D and open on PSU_E.
- **AMX HD interlock function:** `InterlockFunct` of the loaded config is read (never
  written) at connection and after each config load, shown in the `Loaded config` line
  and the status tooltip, and logged when it changes. OFF means the controller ignores
  the interlock input.
- **AMX documentation:** the output connector pins (1 and 4 ground, 2 RF2, 3 and 5
  interlock, 6 RF1), what each plugin can see of the interlock, and a shutdown advice that
  no longer suggests the plugin's interlock can confirm OFF.

## v0.5.4 Update (2026-10-26)

The lab runs of v0.5.3 (logs of 2026-10-26) led to these fixes and to requests from
the operator. None is validated on hardware yet; `SOFTWARE_TEST_PLAN.md` lists each check.

- **MScan data:** scans 011 and 013 were "completed" with every point NaN, because the
  DMMR records NaN at each recorder tick without a new reading and any NaN invalidated
  the point; a point is now the mean of its finite samples, and an infinite sample
  still invalidates it. Closing the plot during scan 012 was refused but dropped the
  axis labels, so every later plot failed and the scan was lost; the close is now
  refused without any change.
- **MScan plot and files:** every enabled DMMR module is recorded and plotted (the
  selected one stays the reference), the y axis is in pA, the x axis reads `PSU X (V)`,
  an Autoscale toggle follows the data, and a `PSU readback` row shows the driven rails.
  Scans are saved to `<Data path>/MScan/<date>/<date>_<time>[_<File name>]_PSU-<X>_<from>-<to>V.h5`
  and never overwrite a file; the Explorer session fields are no longer in the name.
- **MScan passes:** the same sweep is repeated N times in one file, in both modes. In
  Continuous each pass ends like a single sweep, then the PSU returns to the start and
  settles before the next timed sweep. Every pass is kept; the plot shows each pass
  faintly and their mean; `passes` and `points_per_pass` are recorded.
- **Transmission, MScan:** a channel that is inactive with no equation (AMPR `Manual`
  unticked, as every AMPR channel was in the lab) is operator-set and accepted;
  Transmission had refused to start on `Entry_IF1`. A channel driven by an equation is
  refused with the device to fix.
- **PSU:** the last values are proposed in a new session also when an `Operating config`
  is selected but not loaded (PSU_D/E had config 20 and showed zeros). `Link CH1 to CH0`
  makes CH1's `Vset` and `Ilim` follow CH0; outputs and ranges stay separate.
- **ESI:** `Close communication` clicked during the OFF check returns at once (it
  waited on the GUI thread and froze Explorer for 33 s). A single receive error
  (-10/-11/-12) on a measurement step of the check (ADC selection, ready flags or ADC
  read, never an output command) is purged and repeated once; one -10 had left an OFF
  unconfirmed. This is the second replay exception in `native/CONTRACT.md`.

All 15 deployed workers were rebuilt with Rust 1.98.1 (Windows linked with Zig 0.14.1;
Linux for TPG366, MScan and Transmission). The Rust suite passes 324 tests; Clippy
(`-D warnings`), formatting and the ABI/source/family/worker checks pass. The release
validation (`pytest -q --all-siblings` on Explorer 1.0.2) passed 5719 tests with 8
skipped and no failure; the MScan and documentation tests (342) were run again on the
final text. All nine Windows workers pass the constructor-only Wine checks (all six
vendor DLLs load) and the three Linux workers the Linux ones; no instrument is opened.
The archive `esibd-explorer-plugins-v0.5.4.zip` is built from the release commit and
passes `tests/test_release_archive_integrity.py`; the v0.5.4 tag message gives its file
count and SHA-256. Hardware validation is still pending.

## v0.5.3 Update (2026-10-23)

The first lab runs of v0.5.2 (logs of 2026-10-21 to 2026-10-23) led to these fixes.
None is validated on hardware yet; `SOFTWARE_TEST_PLAN.md` lists each check.

- **AMPR:** a poll no longer holds one lock across all 12 modules (about 31 ms per
  serial transaction), so an edit waits for one module and OFF never skips its
  ramp-down; connect scans once. The first command to module 0 after reconnecting,
  or after the PSU was enabled, sometimes got no reply within the DLL's 1.5 s and the
  start failed. After a receive error on a setpoint the worker purges the port and
  reads the setpoint back; it sends the same value once more only if it was not
  applied (the single exception to "no write is replayed", `native/CONTRACT.md`).
  Each lost reply is printed, and each connection keeps the DLL's own trace of its
  first 1000 calls (`ampr_startup_com<N>_<UTC>.log`) to find the cause.
- **PSU:** no false setpoint-readback mismatch right after an edit; after a shutdown
  the panel and table show the last values, sent only when the operator applies them.
- **DMMR:** Explorer's display-lag pauses, which silently raised its error count,
  are logged with the lagging device.
- **AMX A/B, AMX HD:** a frequency change keeps each pulser's duty cycle (or its
  width) and delay share, without rounding drift, in a write order that keeps every
  pulse inside the period; the operator view is an outputs table plus one row per
  used pulser (registers and sketch in Advanced); the output panel no longer reads
  constants by IPC on the GUI thread.
- **ESI:** the OFF check discards the first two conversions and first second after
  each ADC selection (the k × 110.7 V artefacts). HV1 really bleeds slowly through the
  module (247 V 10 s after an OFF from 1000 V, 10 V after 41 s, 2.8 V after 66 s), so
  the operator set the limit to 10 V (three settled rounds, 120 s). The button returns
  to OFF once the outputs are verified OFF; the check continues in the background
  and Explorer closing waits for it. Ramp targets are rounded to millivolts.
- **Transmission:** the spray current is read only from the ESI, the detector never;
  an ESI that is ON but not recording can be started from Transmission.
- **Native logs:** requests are buffered, routine reads summarised per minute, DLL
  statuses and outputs written in exit frames, 8 MB segments (eight kept); ESI
  data-ready flags no longer count as changes (43 MB/h before).
- **Development scripts** no longer run at import: a `tools/` folder copied into the
  plugin path had closed Explorer at startup.

All 15 deployed workers were rebuilt with Rust 1.98.1 (Windows linked with Zig 0.14.1;
Linux for TPG366, MScan and Transmission). The Rust suite passes 318 tests; Clippy
(`-D warnings`), formatting and the ABI/source/family/worker checks pass. The release
validation (`pytest -q --all-siblings` on Explorer 1.0.2) ran 5592 of 5701 tests (5584
passed, 8 skipped) before the session was stopped because other jobs had nearly
exhausted the system memory; the four files it had not reached (`test_tpg366_ui.py` and
`test_transmission_*.py`, 113 tests) then passed on their own, so every test passed or
was skipped once, with no failure. An earlier run under heavy CPU load from another job
failed three MScan real-Qt probes on their 45 s limit; they passed in this run. All nine
Windows workers pass the constructor-only Wine checks (all six vendor DLLs load) and the
three Linux workers the Linux ones; no instrument is opened. The archive
`esibd-explorer-plugins-v0.5.3.zip` is built from the release commit and passes
`tests/test_release_archive_integrity.py`; the v0.5.3 tag message gives its file count
and SHA-256. Hardware validation is still pending.

## v0.5.2 Update (2026-10-18)

Transmission now optimizes the current of a single detector, after Q4 or right
behind one of the four apertures (only the stages before it run), and each RF
amplitude is an AMX output whose two PSU rails move together. A `[spray]` guard
measures every averaging window again, without any move, until the ESI current
is in range (10–30 nA by default) and fluctuates by at most 20 %; after 120 s
the run stops as a soft interlock and restores the stage start. The guard is
implemented in the Python reference engine and in the Rust worker, with a
parity scenario.

The ESI runtime now logs the vendor serial latches (`GetIOState`,
`GetCommError` and their messages) after a failed DLL call, before the
recovery Purge clears them. The first version of that change declared the two
message exports neither in the ctypes signature table nor in the generated
native ABI; both are now declared and the ESI worker accepts the calls.

Because the shared worker source changed, all 15 deployed workers were rebuilt
with Rust 1.98.1 (Windows linked with Zig 0.14.1; Linux for TPG366, MScan and
Transmission). The Rust suite passes 307 tests; Clippy (`-D warnings`),
formatting and the ABI/source/family/worker checks pass. The release
validation (`ESIBD_REQUIRE_TARGET_HOST=1 pytest -q --all-siblings` on Explorer
1.0.2, with `ESIBD_RELEASE_ZIP` set to the v0.5.2 archive) passes **5598 tests,
8 skips, no failures** (1766.53 s). A first attempt failed 3 tests and errored
19 because the `/tmp` tmpfs was full; it is not counted. All nine Windows
workers pass the constructor-only Wine checks (all six vendor DLLs load) and
the three Linux workers the Linux ones; no instrument is opened. The archive
`esibd-explorer-plugins-v0.5.2.zip` holds 285 files and rebuilds
bit-identically from the release commit (entries carry the commit time); the
GitHub release gives its SHA-256. Hardware validation is still pending,
including Transmission and its spray guard.

## Transmission Stop Fix (2026-10-12)

Under load, a Stop could reach the queued `observe` request that reports an
Explorer action already performed. The worker refused every call cancelled
before it started, so the observation was lost: the run ended as `error`
(`RuntimeError: Operation cancelled`) without restoring the stage start. A
backend can now declare the calls it runs when cancelled; the Transmission
engine turns that cancellation into `stopped` and restores the stage start, as
for a Stop during the call. Device calls cancelled before they start are still
refused. Because `main.rs` is shared, all 15 deployed workers were rebuilt; the
Rust suite passes 307 tests and the Linux and Wine constructor-only smoke checks
pass again.

## PSU Fan-Data Fix (2026-10-12)

On the lab PC, every PSU worker of v0.5.1 died with exit `0xC0000374`
(`STATUS_HEAP_CORRUPTION`) shortly after connecting, at varying DLL calls.
The CGC header declares `COM_HVPSU2D_GetFanData(..., WORD PWM[3])`, but the
bundled `COM-HVPSU2D.dll` (SHA-256 `2c951f46...e9bd9e75`) stores `PWM/1000.0`
into three doubles: 24 bytes into a 6-byte heap buffer. The disassembly of every
other PSU getter the worker calls matches the header; the AMX, AMX HD, AMPR
and ESI `GetFanData` exports also match theirs. `tools/generate_native_abi.py`
now declares `PWM` as `double[3]` only for that DLL hash and refuses to
generate if the DLL or the declaration changes. The worker reports `pwm` as
a 0–1 duty fraction. The Python reference passed the same WORD buffer and
also overflowed, silently, inside Explorer's memory. Only the PSU worker
binaries changed; the other manifests record the new source hash.

## v0.5.1 Update (2026-10-11)

A line-by-line review of the worker sources and Python adapters found one
defect, present in both the Rust worker and the Python reference: ESI
`load_config` patched the volatile ramp step before forcing the outputs OFF.
A saved slot that enables HV made that patch refuse, and the error skipped the
final safe OFF, leaving the loaded HV live. OFF is now forced immediately after
the load, even when the load fails, before the patch and again after it. A
mock-DLL test reproduced the defect before the fix.

Minor fixes from the same review: DLL text is decoded as UTF-8 when valid and
otherwise as Windows-1252 (CGC strings are ANSI; `µ`/`°` were lost), AMX HD
rejects digital-I/O sources 80–127 and mapping engines above 3 before any DLL
call, and the supervisor encodes NumPy scalars (`numpy.bool_` was refused).

All Windows workers were rebuilt from scratch with Rust 1.98.1 and Zig 0.14.1;
the three Linux workers are bit-identical to v0.5. A rebuild in a clean
directory differs only in the 4-byte PE link timestamp. The v0.5 Windows
binaries were linked with a different Zig/runtime state (same application
strings, about 50 KB larger, different CRT/unwind imports) and cannot be
reproduced with this toolchain; they are superseded. Deployed workers pass the
constructor-only Wine/Linux smoke checks again. Hardware validation is still
pending.

The final all-feature Rust suite passes 305 tests, formatting/generated ABI checks
pass, and all-feature/all-target Clippy passes with `-D warnings`. Family-only
cross builds can report unused DLL helpers for non-DLL features and benign Zig
linker-option warnings; these are not hidden as a clean cross-build lint claim.
Transport smoke tests under Wine pass stdout isolation, blocked-call reaping,
per-export deadlines and independent-worker survival. All nine deployed Windows
family binaries pass constructor-only Wine checks, including loading all six
vendor DLLs and rejecting foreign families and the test backend. The three
deployed Linux binaries pass the same constructor/protocol checks. These checks
never open a COM/USB port or initialize an instrument; Wine is not physical
Windows qualification. Target-host and archive results are recorded separately
below.

## Local Validation

The dedicated environment uses Explorer 1.0.2 and Python 3.13.13; the owner's
installed Explorer is unchanged. Native products were built with Rust 1.98.1.
Checks run locally, with `ESIBD_REQUIRE_TARGET_HOST=1`; no GitHub CI is involved.

The fresh complete pre-publication suite for v0.5 passes **5466 tests, with six
skips and no failures or errors** (2032.48 s). It runs `pytest -q --all-siblings`
against Explorer 1.0.2, includes the complete Qt and full-application scenarios,
and selects the native candidate with `ESIBD_RELEASE_ZIP`. Its report is
`/tmp/esibd-v0.5-pre-release.xml`. The skips are the optional old Explorer/Python
host-launcher probes, not native production tests. This single clean run
supersedes the combined slow-test coverage described below. The README sync
regression also passes in a separate 25-case documentation/parity check.

Before publication, all 15 plugins also load individually from the candidate
archive through Explorer's real `dynamicImport` in fresh processes. Each has
only its own folder present, loads its private runtime without changing
`sys.path` or importing sibling slugs, and opens no instrument. The 302-test
Rust suite, Clippy, formatting, ABI/source/family checks and hardware-free
Windows/Linux worker smoke checks were rerun successfully.

The earlier all-sibling fast suite passes 4971 cases with one legacy host-worker
skip and 499 slow cases deselected (344.20 s). It uses the final candidate via
`ESIBD_RELEASE_ZIP` and includes the MScan session/completion guards and the real
facade constructor regression. The dedicated full-application crash test is
excluded from this command and covered by the three app tests below.
The final rerun completed after a server restart interrupted an earlier attempt;
an interrupted run is not counted as a pass.

The initial all-sibling slow run had 445 passes, 49 failures and five skips.
After fixes, targeted target-host reruns cover all 494 original non-skipped
cases successfully; this is combined coverage, not a claim that the original
26-minute command passed unchanged. The five skips concern the obsolete Python
worker/Explorer-fork launcher, not native-worker production paths. The 44 MScan
Qt scenarios passed before the last session/completion guards; six targeted
scenarios were repeated successfully after those guards. All 48 adapter cases
pass, including delayed completion, replaced sessions and expired commands.
The 12 device GUI-thread probes and Transmission's not-ready scenario also pass.
Three full Explorer-application tests pass, using isolated profiles and simulated
devices (including the two originally failed app cases). After the PSU logger
fix, 141 focused controller/recovery cases and six PSU Qt cases pass.

MScan integration checks exposed and corrected duplicate final notifications
(concurrent HDF5 saves), missing terminal-error logging and a missing hardware
setpoint confirmation in the GUI prewrite guard. An external 1 mV Vset change
now stops the scan before the next amplitude or restoration write, preserving
the already-applied output state rather than claiming HV shutdown.

Real-facade constructor checks also exposed a PSU entrypoint passing a Python
logger to a native backend that deliberately rejects shared Python objects.
The argument was removed from the canonical PSU and all four siblings. All
12 DLL entrypoints now pass their actual constructor arguments through their
real private facade, checking serialization, native-only selection and data-log
paths. Only process spawning is replaced in this check; it does not validate
Windows COM communication or permit inline DLL fallback.

Pre-publication packaging was checked in a disposable tracked-file snapshot,
without staging or committing the owner's work. The historical root v0.4.2 ZIP
remains unchanged. The owner subsequently requested commit, push, tag and release
v0.5, with physical instrument tests to follow. The release archive is built from
the committed plugin files, validated separately and supplied with its SHA-256.
Archive checks select one explicit `ESIBD_RELEASE_ZIP` path; the native candidate
is a test artifact, not a published or tagged release.

The final candidate is
`/tmp/esibd-native-psu-packaging.fDWCgX/esibd-explorer-plugins-v9.9.9.zip`,
SHA-256 `8722b6a020d2ed89cdeb5fd5e769d216481d1786cef7569893f6dc7f0c910059`.
It passes 40 explicit archive checks and contains exactly 285 files in the 15
plugin roots. Every archive entry is byte-identical to the current plugin file.
The ZIP is 36,204,860 bytes (36.20 MB); its expanded files total 56,792,621 bytes.
The version `9.9.9` identifies a disposable packaging test, not a release version.

## Required Gates

1. Capture current public APIs, state meanings, errors, snapshots, units, limits,
   and recovery contracts before porting a family.
2. Review native ABI declarations against vendor headers and existing ABI tests;
   distinguish one-byte C++ bool from four-byte Windows BOOL.
3. Bound IPC frames, queues, startup, requests, shutdown, and reaping. A dead or
   blocked worker must not block Explorer's GUI or another plugin's process.
4. Check request/session identities and retire late replies. An unexpected exit,
   malformed response, or deadline expiry poisons that connection; do not
   transparently retry a write or reload a DLL into Explorer.
5. Preserve OFF confirmation, discharge, interlocks, ramp limits, readback validity,
   and cancellation. Process termination is not proof that hardware outputs are OFF.
6. Compare against Python references and inject crashes, hangs, truncated frames,
   unexpected stdout, cancellation, missing DLLs/exports, and startup failures.
7. Keep canonical family copies synchronized and every deployed plugin folder
   self-contained. Native build-time reuse must not create deployment dependencies.
8. Rebuild and validate every Windows binary, corresponding source archive and
   local target-host suite. Physically qualify the instruments before routine
   experimental use. Publication for the owner's hardware tests must state that
   qualification is pending. Tests run locally only; there is no GitHub CI.

## Scope Boundaries

Workers contain no Qt, Explorer imports, or Python interpreter. Scan workers
interact with devices only through the Python adapter and Explorer's channel API.
Logs stay under Explorer's data directory. DLL workers serialize hardware calls;
parent-side supervision remains independent of a blocked native call.

## Deliberate Differences

- Transmission uses nalgebra/argmin and a PCG generator instead of SciPy L-BFGS-B
  and Sobol. Numerical trajectories and random draws are not bitwise compatible;
  paired tests validate bounded search results and safety behavior, not an
  identical optimizer trajectory. It limits 256 evaluations/stage, 4096 total,
  32 knobs, 64 stages, 128 channels and 4097 spectrum points.
- The GUI's local peak-selection helper, configuration models and simulated
  instrument are Python presentation/adaptation code. Native peak fitting and
  scan optimization are tested separately. No hardware SDK runs in that GUI.
- AMPR's cross-module GUI ramp/retarget coordination remains in its Python
  adapter; individual device commands and per-module ramps run in Rust.
- MScan returns bounded state snapshots. Large-history IPC overhead has not yet
  been benchmarked; do not infer throughput from small simulation tests.
- DLL calls have a per-export watchdog as well as a batch deadline. DMMR batches
  are capped explicitly instead of promising that every possible FIFO entry can
  individually consume the full I/O timeout.
- Idle housekeeping has its own child-side I/O watchdog. Fatal diagnostics cannot
  block process exit on a full stderr pipe. Parent log disk writes never hold
  state, request or close locks; queue overflow/forced termination can lose logs.

## Distribution

Every plugin contains its own small worker, manifest, supervisor, source archive
and notices. Family siblings are mechanical copies, not runtime imports. Vendor
DLLs remain Windows-only and retain their original distribution terms. MScan and
TPG366 preserve GPL-2.0-or-later; Transmission preserves MIT; third-party Rust
dependencies retain their own licenses. GPL source archives are built and checked
offline with vendored dependencies. There is no private Python payload.

The Windows executables occupy 11,701,248 bytes across the 15 standalone folders
(roughly 0.57 to 1.11 MB per worker); the three Linux executables add 2,552,032
bytes. Source/license archives are separate from runtime cost, and GPL vendored
source archives account for most of the distribution's additional size. Runtime
size is not the ZIP size or process memory usage.

The Windows target requires Windows 10 or later, x86-64
([Rust platform support](https://doc.rust-lang.org/rustc/platform-support.html)).
The deployed Linux executables require glibc 2.34 for MScan/TPG366 and 2.35 for
Transmission, as measured from their ELF symbol versions, plus system
`libgcc_s` and, for Transmission, `libm`. This distribution has no additional
Python interpreter or Rust runtime installation, but it is not independent of
the operating system's libraries.
