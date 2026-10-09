# Native Worker Contract

The main agent owns shared files, the worker executable, ABI generation, Python
supervision, integration, build, and release plumbing. Family contributors own
only their named Rust modules/tests and any explicitly assigned adapter files.
Do not edit Cargo.toml, lib.rs, error.rs, context.rs, ffi.rs, or another family.
Send dependency requirements to the integrating agent.

## Rust Backend Interface

Each DLL family supplies `pub struct Controller` with:

```rust
pub fn new(config: &serde_json::Value, dll: Box<dyn crate::ffi::Dll>)
    -> crate::error::Result<Self>;
```

It implements `crate::Backend`:

```rust
fn call(&mut self, method: &str, args: &[serde_json::Value],
        kwargs: &serde_json::Value, ctx: &crate::context::Context)
    -> crate::error::Result<serde_json::Value>;
fn get_attribute(&self, name: &str) -> crate::error::Result<serde_json::Value>;
fn set_attribute(&mut self, name: &str, value: serde_json::Value)
    -> crate::error::Result<()>;
```

Only public operations used by the existing facade are dispatched, by an explicit
match. Internal helper methods do not become RPC operations. Input validation and
state contracts are preserved. Missing supported methods must be reported as
migration gaps, not replaced by successful no-op stubs.

`crate::ffi::Dll::call(&mut self, symbol: &str, args: &[Value]) -> Result<NativeReply>`
calls a whitelisted vendor export. `NativeReply` has `status: i64` (the actual
return value) and `values: Vec<Value>` (pointer arguments only, in argument order).
Scalar arguments use normal JSON numbers/booleans. A pointer argument uses its
initial scalar value, or an array of the required fixed length. A char buffer uses
`{"capacity": 202, "text": ""}` and returns its NUL-terminated string.
Fixed vendor buffer lengths must be correct even on errors. A returned C bool is
represented as JSON bool, without constructing invalid Rust bool values.

Examples:

```rust
let reply = dll.call("COM_ESI_CTRL_GetState", &[serde_json::json!(0)])?;
// reply.status is the status code, reply.values[0] is the state word.
let reply = dll.call("COM_HVPSU2D_GetMainState", &[serde_json::json!(port), serde_json::json!(0)])?;
let reply = dll.call("COM_ESI_CTRL_GetConfigName", &[
    serde_json::json!(number), serde_json::json!({"capacity": 202, "text": ""})])?;
```

`crate::error::Error` provides `new(kind, message)`, `argument(message)`,
`runtime(message)`, `unsupported(message)`, `status(code, operation)`.
Kinds use Python exception names (ValueError, RuntimeError, TimeoutError, etc.).
`crate::context::Context` supplies `check_cancelled()`, `is_cancelled()`,
`remaining()`, `sleep(Duration)`, and `progress(Value)`, all bounded/cancellable.
Wrap every vendor invocation in `native_call(symbol, timeout, closure)`. It emits
entry/exit telemetry for the independent parent watchdog and rejects late returns.
An I/O timeout poisons the transport: do not send another command on that stream.
Use `ctx.cleanup(Duration)` for deliberately uncancelled, bounded safety cleanup;
it preserves request identity, observer and sequence numbers. A fresh unrelated
Context would hide cleanup calls from the watchdog. Cancellation never reverses
a write already accepted by the device.
Unit tests use `Context::test(timeout_seconds)`.

Use `crate::codec::float(f64)` for non-finite telemetry and
`crate::codec::tuple(Vec<Value>)` when a Python tuple must be preserved. Non-string
map keys use `{"$map": [[key, value], ...]}`. Bytes use `{"$bytes": [0, 1, ...]}`.

Scans may supply their own `pub struct Controller` constructor without a DLL.
An action/observation state machine is preferred over calling Explorer directly;
the Python adapter executes actions through Explorer and returns observations.

## Process Protocol

Length-prefixed UTF-8 JSON, four-byte big-endian length, maximum 16 MiB. The first
parent request is `{version:1, id:0, op:"init", family:"esi", config:{...}}`.
Replies identify version, request id, kind, success/value or typed error. Supported
ops are call/getattr/setattr/close/cancel; only one hardware call runs at a time.
Cancellation can be received while hardware runs. A lost parent pipe terminates
the worker, even when the hardware thread is blocked. Native DLL stdout must not
corrupt the protocol channel. Logs go to stderr, drained by the parent.

The deployment binary accepts exactly its compiled family; `test-backend` is
reserved for explicit fault fixtures and must never be shipped. IDs are monotonic
unsigned 64-bit integers. Late replies are retired, duplicate/unsolicited replies
and malformed frames poison the session. No hardware write is replayed after
failure. JSON NaN/Infinity tokens are forbidden; nonfinite readings use tagged
values. Request and output queues are bounded (8 and 64).

After each executor reply, a `state` map contains only cached lifecycle attributes:
`connected`, `_dll_port_claimed`, `_open_failed`, `_opening_in_progress`,
`_failed_open_released`, `_failed_open_cleanup_outcome`, `_transport_poisoned` and
`_transport_error`. Reading these in the GUI is local and must not wait for an
SDK lock or another RPC. An idle housekeeping failure that poisons the backend
exits the worker instead of leaving a falsely active GUI connection.
An independent child-side watchdog also observes idle housekeeping DLL calls;
it exits with a best-effort, bounded diagnostic even when stderr is undrained.

`native_io` frames contain request `id`, `sequence`, export `symbol`, `phase`
(`enter`/`exit`), and `timeout_s`/`elapsed_s`. The parent enforces that deadline
even inside a long batch. Calls also have an overall request deadline; a batch
budget does not allow an individual export to block for that entire period.
Cancellation Events and progress callbacks are Python-local arguments, not JSON
payloads. Progress executes on the calling thread, never the pipe-reader thread.

## Supervision And GUI

Workers are selected by an x86-64 OS target in a plugin-local manifest. Check a
regular, nonsymlink binary and SHA-256 before launch. Missing files, wrong family
or invalid responses fail closed. Never search PATH, launch Python or load the
vendor DLL in Explorer as a fallback. A plugin's process/session is independent
of every other plugin, including family siblings.

Startup defaults to 30 s. Ordinary I/O defaults to 5 s; explicit budgets are
validated in the 1 ms to 1 h interval. Cooperative cancellation gets a bounded
grace period; close then uses terminate/wait and kill/wait (1 s each). Forced
controller disconnect bypasses its I/O lock and skips the cooperative grace.
Process reaping and pending-caller wakeup must complete before reconnecting.

Healthy communication with an unconfirmed OFF keeps its owner available for
an OFF retry. Dead, timed-out or explicitly force-closed native workers are reaped,
acquisition stops, readings become invalid and the connection button becomes OFF.
Show `Disconnected: shutdown unconfirmed`, not an output-shutdown success. An old
initializer, command, progress callback or GUI callback cannot activate a new
session. No process termination is proof of hardware OFF, discharge or coldness.

DLL entry/exit, progress and transport records are JSON Lines under the supplied
Explorer data log directory's `native/<family>/`, capped at 8 MiB per session.
The stderr tail is bounded to 8 KiB. Existing sessions are not deleted. Scan
experiment logs keep their explicit data paths. No worker log belongs in a plugin
installation folder.
Log filesystem writes run on a separate daemon behind a bounded queue. A slow
or failed log disk must not hold the protocol/state/close locks or delay process
termination. Queue overflow drops records and reports the count when possible;
close makes only a bounded flush attempt. A forced exit can lose pending records.

## Build And Sources

Build locally with Cargo `--locked --offline --release --no-default-features`
and one family feature. ABI declarations and the embedded error catalog are
generated/checked from supplied vendor headers/catalogs. Bool widths and fixed
buffers are explicit; DMMR debug exports require the known DLL hash.

The plugin manifest records binary target/features/size/SHA-256, Cargo.lock hash,
and source-data hash. `source.zip` and `THIRD_PARTY_NOTICES.txt` have their own
hash/size records. The archive records every source file's digest, retains raw
build provenance and license terms, and contains no deployed worker executable or
private Python. Preserved upstream crate payloads can include platform import
libraries required by Cargo's Windows builds.
MScan/TPG366 GPL corresponding sources include vendored dependencies and an
archive-specific family manifest; tests rebuild offline from an empty Cargo home
and compare active dependency versions with the original locked build. Preserve
MIT/GPL/MPL/ISC/Apache and vendor terms per component, not a blanket license.

Deployment checks, canonical sync, temporary tracked-file release archives and
Explorer 1.0.2 tests run locally. Keep physical Windows-instrument qualification
separate from mock SDK, PTY, simulation and Wine checks. No publication is implied.
