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
| ESI | Implemented | Implemented | 61 controller tests; crash/blocked OFF/late Qt payloads | Not tested |
| PSU A/B/C/D/E | Implemented | Implemented | 52 controller tests; isolated recovery checks | Not tested |
| AMX A/B | Implemented | Implemented | 43 combined AMX/HD controller tests; 60 adapter tests | Not tested |
| AMX HD | Implemented | Implemented | Same combined suite; independent HD ABI and process | Not tested |
| AMPR A/B | Implemented | Implemented | 23 controller tests; isolated recovery checks | Not tested |
| DMMR | Implemented | Implemented | 38 controller tests; isolated recovery checks | Not tested |
| TPG366 | Implemented | Implemented | 17 Rust + 36 adapter/PTY tests; 30 Explorer 1.0.2 Qt cases | Not tested |
| MScan | Implemented | Implemented | 39 native tests; 36 paired executions; 138 NumPy plans; 48 adapter cases | Not tested |
| Transmission | Implemented | Implemented | 24 engine tests; 19 packaged lifecycle tests; 15 Explorer 1.0.2 Qt cases | Not tested |

The local target-host environment is isolated from the owner's installed Explorer.
Passing Python-reference tests do not validate native hardware communication.
DLL controller tests inject explicit mock SDK replies; PTY serial tests use a
pseudo-terminal; scan tests use simulated Explorer instruments. None opens a real
HV device. Windows workers for all nine families and Linux workers for TPG366,
MScan and Transmission have been rebuilt after the final Rust edits. Deployment
manifests all identify raw source data
`39875286a292d0d70d8620352a2193dff2a9c8413ece80d05f0d4b4bc5314922`.

The final all-feature Rust suite passes 302 tests, formatting/generated ABI checks
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

The Windows executables occupy 12,452,864 bytes across the 15 standalone folders
(roughly 0.62 to 1.16 MB per worker); the three Linux executables add 2,552,032
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
