"""Private, bounded transport for a plugin-local native worker (no DLL in host)."""
from __future__ import annotations

import builtins
from collections import deque
import hashlib
import json
import math
import numbers
import os
from pathlib import Path
import platform
import queue
import struct
import subprocess
import sys
import threading
import time
import uuid

PROTOCOL = 1
MAX_FRAME = 16 * 1024 * 1024
MAX_LOG_BYTES = 8 * 1024 * 1024
LIFECYCLE_ATTRIBUTES = frozenset({"connected", "_dll_port_claimed", "_open_failed", "_opening_in_progress",
                                "_failed_open_released", "_failed_open_cleanup_outcome",
                                "_transport_poisoned", "_transport_error"})


def encode(value):
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, numbers.Integral):
        return int(value)
    if isinstance(value, numbers.Real):
        value = float(value)
        return value if math.isfinite(value) else {"$float": "nan" if math.isnan(value) else "inf" if value > 0 else "-inf"}
    if isinstance(value, tuple):
        return {"$tuple": [encode(v) for v in value]}
    if isinstance(value, (bytes, bytearray)):
        return {"$bytes": list(value)}
    if isinstance(value, list):
        return [encode(v) for v in value]
    if isinstance(value, dict):
        if all(isinstance(k, str) for k in value) and not any(k.startswith("$") for k in value):
            return {k: encode(v) for k, v in value.items()}
        return {"$map": [[encode(k), encode(v)] for k, v in value.items()]}
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"Cannot send {type(value).__name__} to native worker")


def decode(value):
    if isinstance(value, list):
        return [decode(v) for v in value]
    if not isinstance(value, dict):
        return value
    if len(value) == 1:
        if "$tuple" in value:
            return tuple(decode(v) for v in value["$tuple"])
        if "$bytes" in value:
            return bytes(value["$bytes"])
        if "$float" in value:
            return {"nan": math.nan, "inf": math.inf, "-inf": -math.inf}[value["$float"]]
        if "$map" in value:
            return {decode(k): decode(v) for k, v in value["$map"]}
    return {k: decode(v) for k, v in value.items()}


def _read_exact(stream, count):
    data = bytearray()
    while len(data) < count:
        chunk = stream.read(count - len(data))
        if not chunk:
            raise EOFError("Native worker pipe closed")
        data.extend(chunk)
    return bytes(data)


def receive(stream):
    length, = struct.unpack("!I", _read_exact(stream, 4))
    if not 0 < length <= MAX_FRAME:
        raise ValueError("Native worker frame outside size limit")
    def invalid_constant(value):
        raise ValueError(f"Non-JSON native worker number: {value}")
    value = json.loads(_read_exact(stream, length), parse_constant=invalid_constant)
    if (not isinstance(value, dict) or type(value.get("version")) is not int or value.get("version") != PROTOCOL
            or type(value.get("id")) is not int or not 0 <= value["id"] < 2 ** 64):
        raise ValueError("Invalid native worker protocol/version/request id")
    return value


def executable(plugin_root, family):
    root = Path(plugin_root).resolve()
    directory = root / "native"
    if directory.is_symlink() or not directory.is_dir():
        raise FileNotFoundError(f"Missing or non-regular native worker directory: {directory}")
    manifest_path = directory / "manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise FileNotFoundError(f"Missing native worker manifest: {manifest_path}. Restore the complete plugin folder.")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (not isinstance(manifest, dict) or type(manifest.get("protocol")) is not int
            or manifest.get("protocol") != PROTOCOL or manifest.get("family") != family):
        raise RuntimeError("Native worker manifest family/protocol mismatch")
    machine = platform.machine().lower()
    if machine not in {"amd64", "x86_64"}:
        raise RuntimeError(f"No native worker for {machine}; Windows devices require x86-64")
    target = "windows-x86_64" if sys.platform == "win32" else "linux-x86_64" if sys.platform.startswith("linux") else ""
    targets = manifest.get("targets")
    if not isinstance(targets, dict):
        raise RuntimeError("Invalid native worker target manifest")
    spec = targets.get(target)
    if not isinstance(spec, dict):
        raise RuntimeError(f"No native worker bundled for {sys.platform}/{machine}")
    name = spec.get("file", "")
    if not isinstance(name, str) or not name or Path(name).name != name or "\\" in name:
        raise RuntimeError("Invalid native worker filename")
    path = directory / name
    if path.is_symlink() or not path.is_file() or path.resolve().parent != directory.resolve():
        raise FileNotFoundError(f"Missing or non-regular native worker: {path}")
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest() if hasattr(hashlib, "file_digest") else hashlib.sha256(stream.read()).hexdigest()
    if digest != spec.get("sha256"):
        raise RuntimeError(f"Native worker integrity check failed: {path}")
    return path


class _Pending:
    def __init__(self):
        self.event = threading.Event()
        self.response = None
        self.failure = None
        self.value = None
        self.progress = deque(maxlen=32)
        self.io_deadline = None
        self.io_timeout = None
        self.io_symbol = None
        self.io_sequence = -1
        self.timeout_failure = None


class NativeWorkerProxy:
    """One serialized controller with independently cancellable bounded teardown."""

    def __init__(self, plugin_root, family, config, *, startup_timeout_s=30., log_dir=None,
                 command=None, stop_grace_s=1., awake_request=None):
        # command injection is used by local fault tests, never by a runtime setting.
        self.family = str(family)
        self.session = uuid.uuid4().hex
        self._request_lock = threading.Lock()
        self._state_lock = threading.RLock()
        self._send_lock = threading.Lock()
        self._close_lock = threading.Lock()
        self._closing = threading.Event()
        self._log_lock = threading.Lock()
        self._logs = queue.Queue(maxsize=128)
        self._logs_stop = threading.Event()
        self._log_thread = None
        self._log_dropped = 0
        self._log_write_error = None
        self._pending = {}
        self._sequence = -1
        self._closed = False
        self._closed_reason = "Native worker is closed"
        self._stderr_tail = bytearray()
        self._active = None
        self._active_method = None
        self._lifecycle = {"connected": False, "_dll_port_claimed": False, "_open_failed": False,
                           "_opening_in_progress": False, "_failed_open_released": False,
                           "_failed_open_cleanup_outcome": None, "_transport_poisoned": False,
                           "_transport_error": None}
        self._stop_grace_s = max(0., min(float(stop_grace_s), 5.))
        self._writes = queue.Queue(maxsize=8)
        self._log_path = None
        self._log_bytes = 0
        self._awake_request = awake_request
        self._threads = []
        if log_dir is not None:
            base = Path(log_dir)
            if not base.is_absolute():
                raise ValueError("Native logs require an absolute Explorer data-directory path")
            directory = base / "native" / self.family
            self._log_path = directory / f"{time.strftime('%Y%m%d-%H%M%S')}-{self.session[:8]}.jsonl"
        self._command = list(command) if command is not None else [str(executable(plugin_root, self.family)), "--family", self.family]
        environment = os.environ.copy()
        for key in ("PYTHONHOME", "PYTHONPATH"):
            environment.pop(key, None)
        self._process = subprocess.Popen(self._command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE, bufsize=0, env=environment,
                                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), close_fds=True)
        try:
            if self._awake_request is not None:
                self._awake_request.hold(True)
            for target, name in ((self._write_frames, "writer"), (self._read_replies, "reader"),
                                 (self._read_stderr, "stderr"), (self._write_logs, "logs")):
                thread = threading.Thread(target=target, daemon=True, name=f"{family}-native-{name}")
                thread.start()
                self._threads.append(thread)
                if name == "logs":
                    self._log_thread = thread
            self._log("start", command=self._command)
            self.identity = self._request("init", startup_timeout_s, family=self.family, config=encode(config))
            if (not isinstance(self.identity, dict) or self.identity.get("family") != self.family
                    or type(self.identity.get("protocol")) is not int or self.identity.get("protocol") != PROTOCOL):
                raise RuntimeError("Native worker identity mismatch")
        except BaseException:
            self.close(grace_s=0)
            raise

    @property
    def closed(self):
        return self._closed or self._process.poll() is not None

    def lifecycle_attribute(self, name):
        if name not in LIFECYCLE_ATTRIBUTES:
            raise AttributeError(name)
        with self._state_lock:
            if name == "_transport_poisoned":
                return self.closed or self._lifecycle[name]
            if name == "_transport_error" and self.closed:
                return self._closed_reason
            if name == "connected" and self.closed:
                return False
            if name == "_dll_port_claimed" and self._process.poll() is not None:
                return False
            if name == "_opening_in_progress":
                return not self.closed and self._active_method in {"connect", "initialize"}
            return self._lifecycle[name]

    def _log(self, kind, **fields):
        if self._log_path is None or self._logs_stop.is_set():
            return
        record = {"time": time.time(), "session": self.session, "family": self.family, "kind": kind, **fields}
        try:
            with self._log_lock:
                if self._log_bytes >= MAX_LOG_BYTES:
                    return
                if self._log_dropped:
                    record["dropped_records"] = self._log_dropped
                line = json.dumps(record, allow_nan=False) + "\n"
                length = len(line.encode("utf-8"))
                if self._log_bytes + length > MAX_LOG_BYTES:
                    line = json.dumps({"time": time.time(), "session": self.session, "kind": "log_truncated", "limit_bytes": MAX_LOG_BYTES}) + "\n"
                try:
                    self._logs.put_nowait((line, None))
                except queue.Full:
                    self._log_dropped += 1
                    return
                self._log_dropped = 0
                self._log_bytes = (MAX_LOG_BYTES if self._log_bytes + length > MAX_LOG_BYTES
                                   else self._log_bytes + length)
        except (OSError, TypeError, ValueError):
            pass

    def _write_logs(self):
        while True:
            try:
                line, completion = self._logs.get(timeout=.1)
            except queue.Empty:
                if self._logs_stop.is_set():
                    return
                continue
            try:
                if line is not None:
                    self._log_path.parent.mkdir(parents=True, exist_ok=True)
                    with self._log_path.open("a", encoding="utf-8") as stream:
                        stream.write(line)
            except (OSError, ValueError) as exc:
                self._log_write_error = str(exc)
            finally:
                if completion is not None:
                    completion.set()

    def _flush_logs(self, timeout_s=.2):
        if self._log_thread is None or not self._log_thread.is_alive():
            return True
        complete = threading.Event()
        try:
            self._logs.put_nowait((None, complete))
        except queue.Full:
            return False
        return complete.wait(max(0., timeout_s))

    def _failure_message(self, message):
        with self._state_lock:
            stderr = bytes(self._stderr_tail).decode("utf-8", errors="replace").strip()
        return f"{self.family} native worker transport is unusable: {message}; exit={self._process.poll()}; stderr={stderr or '(empty)'}. Output safety is not confirmed."

    def _fail(self, message):
        with self._state_lock:
            if not self._closed:
                self._closed = True
                self._closed_reason = str(message)
            for pending in self._pending.values():
                if pending.failure is None:
                    pending.failure = str(message)
                pending.event.set()

    def _enqueue(self, op, **fields):
        with self._send_lock, self._state_lock:
            if self.closed:
                raise RuntimeError(self._failure_message(self._closed_reason))
            self._sequence += 1
            request_id = self._sequence
            payload = json.dumps({"version": PROTOCOL, "id": request_id, "op": op, **fields}, allow_nan=False, separators=(",", ":")).encode("utf-8")
            if not 0 < len(payload) <= MAX_FRAME:
                raise ValueError("Native worker request exceeds frame size limit")
            pending = _Pending()
            self._pending[request_id] = pending
            try:
                self._writes.put_nowait(struct.pack("!I", len(payload)) + payload)
            except queue.Full:
                self._pending.pop(request_id)
                raise RuntimeError("Native worker write queue exhausted") from None
            return request_id, pending

    def _write_frames(self):
        try:
            while True:
                payload = self._writes.get()
                if payload is None:
                    return
                view = memoryview(payload)
                while view:
                    written = os.write(self._process.stdin.fileno(), view)
                    if written <= 0:
                        raise BrokenPipeError("Native worker write returned no bytes")
                    view = view[written:]
        except (OSError, ValueError) as exc:
            self._fail(self._failure_message(str(exc)))
            if not self._closing.is_set():
                self.close(grace_s=0)

    def _read_replies(self):
        try:
            while True:
                response = receive(self._process.stdout)
                with self._state_lock:
                    if response["id"] > self._sequence:
                        raise ValueError("Native reply identifies an unsent request")
                    pending = self._pending.get(response["id"])
                    if pending is None:
                        continue  # Retired/cancel acknowledgements cannot satisfy another call.
                    if response.get("kind") == "native_io":
                        sequence, symbol = response.get("sequence"), response.get("symbol")
                        if type(sequence) is not int or sequence < 0 or not isinstance(symbol, str) or not symbol:
                            raise ValueError("Invalid native I/O identity")
                        if response.get("phase") == "enter":
                            limit = response.get("timeout_s")
                            if (pending.io_deadline is not None or sequence <= pending.io_sequence
                                    or type(limit) not in (int, float) or not math.isfinite(limit) or not 0 < limit <= 3600.):
                                raise ValueError("Invalid native I/O start/deadline")
                            pending.io_deadline = time.monotonic() + limit
                            pending.io_timeout, pending.io_symbol, pending.io_sequence = limit, symbol, sequence
                        elif response.get("phase") == "exit":
                            elapsed = response.get("elapsed_s")
                            if (pending.io_deadline is None or sequence != pending.io_sequence or symbol != pending.io_symbol
                                    or type(elapsed) not in (int, float) or not math.isfinite(elapsed) or elapsed < 0):
                                raise ValueError("Invalid native I/O completion")
                            if elapsed >= pending.io_timeout:
                                pending.timeout_failure = f"{symbol} exceeded its native I/O deadline ({pending.io_timeout:g}s)"
                            pending.io_deadline = None
                        else:
                            raise ValueError("Invalid native I/O phase")
                        self._log("native_io", request=response["id"], **{k: v for k, v in response.items()
                                                                          if k not in {"id", "kind", "version"}})
                        continue
                    if response.get("kind") == "progress":
                        if "value" not in response:
                            raise ValueError("Missing native progress value")
                        pending.progress.append(decode(response["value"]))
                        self._log("progress", request=response["id"], value=response.get("value"))
                        continue
                    if response.get("kind") != "reply" or type(response.get("success")) is not bool:
                        raise ValueError("Invalid native worker reply")
                    if pending.io_deadline is not None:
                        raise ValueError("Native reply arrived before DLL I/O completion")
                    if pending.event.is_set():
                        raise ValueError("Duplicate native worker reply")
                    state = response.get("state", {})
                    if not isinstance(state, dict) or not set(state).issubset(LIFECYCLE_ATTRIBUTES):
                        raise ValueError("Invalid native lifecycle state")
                    for name, value in state.items():
                        if name not in {"_transport_error", "_failed_open_cleanup_outcome"} and type(value) is not bool:
                            raise ValueError("Invalid native lifecycle flag")
                        if name == "_transport_error" and value is not None and not isinstance(value, str):
                            raise ValueError("Invalid native lifecycle error")
                        self._lifecycle[name] = decode(value)
                    if response["success"]:
                        if "value" not in response:
                            raise ValueError("Missing native reply value")
                        pending.value = decode(response["value"])
                    else:
                        error = response.get("error")
                        if (not isinstance(error, dict) or not isinstance(error.get("kind"), str)
                                or not isinstance(error.get("message"), str)):
                            raise ValueError("Invalid native error reply")
                    pending.response = response
                    pending.event.set()
        except Exception as exc:
            self._fail(self._failure_message(str(exc)))
            if not self._closing.is_set():
                self.close(grace_s=0)

    def _read_stderr(self):
        try:
            while chunk := self._process.stderr.read(4096):
                with self._state_lock:
                    self._stderr_tail.extend(chunk)
                    del self._stderr_tail[:-8192]
                self._log("stderr", message=chunk.decode("utf-8", errors="replace"))
        except (OSError, ValueError):
            pass

    def _request(self, op, timeout_s, *, _cancel_event=None, _progress=None, **fields):
        timeout_s = float(timeout_s)
        if not math.isfinite(timeout_s) or not .001 <= timeout_s <= 3600.:
            raise ValueError("Native operation timeout must be between 1ms and 1h")
        deadline = time.monotonic() + timeout_s
        while True:
            if _cancel_event is not None and _cancel_event.is_set():
                raise RuntimeError("Native operation cancelled before dispatch")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"{self.family} operation queue timed out")
            if self._request_lock.acquire(timeout=min(.05, remaining)):
                break
        request_id = None
        try:
            if _cancel_event is not None and _cancel_event.is_set():
                raise RuntimeError("Native operation cancelled before dispatch")
            remaining = deadline - time.monotonic()
            if remaining < .001:
                raise TimeoutError(f"{self.family} operation queue timed out")
            request_id, pending = self._enqueue(op, timeout_s=remaining, **fields)
            with self._state_lock:
                self._active = request_id
                self._active_method = fields.get("method")
            self._log("request", request=request_id, operation=op, method=fields.get("method"), timeout_s=remaining)
            completed = pending.event.wait(min(.05, remaining))
            def deliver_progress():
                with self._state_lock:
                    reports = list(pending.progress)
                    pending.progress.clear()
                if _progress is not None:
                    for report in reports:
                        try:
                            _progress(report)
                        except Exception as exc:
                            self._log("progress_callback_error", error=str(exc))
            while not completed and time.monotonic() < deadline:
                with self._state_lock:
                    if pending.io_deadline is not None and time.monotonic() >= pending.io_deadline:
                        pending.timeout_failure = f"{pending.io_symbol} blocked past its native I/O deadline ({pending.io_timeout:g}s)"
                    io_expired = pending.timeout_failure
                if io_expired is not None:
                    break
                if _cancel_event is not None and _cancel_event.is_set():
                    self.cancel()
                    if not pending.event.wait(self._stop_grace_s):
                        self.close(grace_s=0)
                        raise RuntimeError(self._failure_message("Cancelled native operation remained blocked; worker terminated"))
                    completed = True
                    break
                deliver_progress()
                completed = pending.event.wait(min(.05, max(0., deadline - time.monotonic())))
            deliver_progress()
            if not completed or pending.timeout_failure is not None:
                self.cancel()
                pending.event.wait(self._stop_grace_s)
                reason = pending.timeout_failure or f"{op} timed out after {timeout_s:g}s"
                self._log("timeout", request=request_id, error=reason)
                self.close(grace_s=0)
                raise TimeoutError(self._failure_message(f"{reason}; worker terminated"))
            if pending.failure is not None:
                self.close(grace_s=0)
                raise RuntimeError(pending.failure)
            response = pending.response
            if not response["success"]:
                error = response.get("error", {})
                exception = getattr(builtins, error.get("kind", "RuntimeError"), RuntimeError)
                if not isinstance(exception, type) or not issubclass(exception, Exception):
                    exception = RuntimeError
                self._log("error", request=request_id, error=error)
                failure = exception(error.get("message", "Native operation failed"))
                failure.native_error_kind = error.get("kind", "RuntimeError")
                if (isinstance(failure, TimeoutError) or "unusable" in str(failure).lower()
                        or self.lifecycle_attribute("_transport_poisoned")):
                    self._fail(self._failure_message(str(failure)))
                    self.close(grace_s=0)
                    failure = exception(self._failure_message(str(failure)))
                    failure.native_error_kind = error.get("kind", "RuntimeError")
                raise failure
            self._log("reply", request=request_id)
            if self.lifecycle_attribute("_transport_poisoned"):
                self.close(grace_s=0)
                raise RuntimeError(self._failure_message("Controller retired its transport"))
            return pending.value
        finally:
            with self._state_lock:
                self._pending.pop(request_id, None)
                if self._active == request_id:
                    self._active = None
                    self._active_method = None
            self._request_lock.release()

    def call_method(self, method_name, *args, rpc_timeout_s=30., _cancel_event=None, _io_timeout_s=None, **kwargs):
        cancel = kwargs.pop("cancel_event", None)
        cancel_alias = kwargs.pop("cancel", None)
        if cancel_alias is not None:
            if cancel is not None and cancel_alias is not cancel:
                raise ValueError("Conflicting local native cancellation events")
            cancel = cancel_alias
        progress = kwargs.pop("on_progress", None)
        discharge = kwargs.pop("on_discharge", None)
        if discharge is not None:
            if progress is not None and discharge is not progress:
                raise ValueError("Conflicting local native progress callbacks")
            progress = discharge
        if cancel is not None:
            if _cancel_event is not None and cancel is not _cancel_event:
                raise ValueError("Conflicting local native cancellation events")
            _cancel_event = cancel
        if _cancel_event is not None and not callable(getattr(_cancel_event, "is_set", None)):
            raise TypeError("Native cancellation requires a local Event")
        if progress is not None and not callable(progress):
            raise TypeError("Native discharge progress requires a local callback")
        if _cancel_event is not None and _cancel_event.is_set():
            raise RuntimeError("Native operation cancelled before dispatch")
        io_timeout = _io_timeout_s if _io_timeout_s is not None else kwargs.get("timeout_s")
        io_timeout = 5. if io_timeout is None else io_timeout
        if type(io_timeout) not in (int, float) or not math.isfinite(io_timeout) or not .001 <= io_timeout <= 3600.:
            raise ValueError("Native I/O timeout must be between 1ms and 1h")
        return self._request("call", rpc_timeout_s, _cancel_event=_cancel_event, _progress=progress,
                             io_timeout_s=io_timeout, method=str(method_name), args=[encode(arg) for arg in args], kwargs=encode(kwargs))

    def call(self, method_name, *args, **kwargs):
        return self.call_method(method_name, *args, **kwargs)

    def call_json(self, method_name, *args, rpc_timeout_s=30., **kwargs):
        """Scan adapters may supply already-tagged JSON, without encoding it twice."""
        return self._request("call", rpc_timeout_s, method=str(method_name), args=list(args), kwargs=kwargs)

    def get_attribute(self, name, *, timeout_s=5.):
        return self._request("getattr", timeout_s, name=str(name))

    def set_attribute(self, name, value, *, timeout_s=5.):
        return self._request("setattr", timeout_s, name=str(name), value=encode(value))

    def cancel(self):
        with self._state_lock:
            target = self._active
        if target is None or self.closed:
            return False
        try:
            request_id, _ = self._enqueue("cancel", target=target)
            with self._state_lock:
                self._pending.pop(request_id, None)
            return True
        except RuntimeError:
            return False

    def wait_for_idle(self, timeout_s=1.):
        acquired = self._request_lock.acquire(timeout=max(0., float(timeout_s)))
        if acquired:
            self._request_lock.release()
        return acquired

    def close(self, *, grace_s=None):
        with self._close_lock:
            self._closing.set()
            process = self._process
            if process.poll() is None:
                self.cancel()
                self.wait_for_idle(self._stop_grace_s if grace_s is None else max(0., float(grace_s)))
            self._fail("Native worker stopped")
            try:
                self._writes.put_nowait(None)
            except queue.Full:
                pass
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=1.)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1.)
            for thread in self._threads:
                if thread is not threading.current_thread() and thread is not self._log_thread:
                    thread.join(timeout=.2)
            for stream in (process.stdin, process.stdout, process.stderr):
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass
            if not self._logs_stop.is_set():
                self._log("close", exit=process.returncode, outputs_confirmed_off=False)
                self._flush_logs()
                self._logs_stop.set()
                if self._log_thread is not None and self._log_thread is not threading.current_thread():
                    self._log_thread.join(timeout=.2)
            if self._awake_request is not None:
                self._awake_request.hold(False)
            return process.poll() is not None

    def __del__(self):
        try:
            if hasattr(self, "_process"):
                self.close(grace_s=0)
        except Exception:
            pass
