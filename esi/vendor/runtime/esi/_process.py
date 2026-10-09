"""ESI DLL worker launched from a file, including privately loaded runtimes."""

from __future__ import annotations

import builtins
import contextlib
import importlib.util
import os
import pickle
import queue
import struct
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path


def _send(stream, message, lock):
    # These pipes connect only this parent and its child; they are not network endpoints.
    data = pickle.dumps(message, protocol=4)
    with lock:
        stream.write(struct.pack("!I", len(data)) + data)
        stream.flush()


def _receive(stream):
    def read_exact(count):
        chunks = bytearray()
        while len(chunks) < count:
            chunk = stream.read(count - len(chunks))
            if not chunk:
                raise EOFError("ESI worker pipe closed")
            chunks.extend(chunk)
        return bytes(chunks)

    size, = struct.unpack("!I", read_exact(4))
    if size > 16 * 1024 * 1024:
        raise ValueError("ESI worker message exceeds its size limit")
    return pickle.loads(read_exact(size))


def _console_python(executable):
    path = Path(executable)
    if path.name.lower() == "pythonw.exe":
        console = path.with_name("python.exe")
        if not console.is_file():
            raise RuntimeError(
                "ESI requires python.exe, not pythonw.exe (the worker needs standard I/O). "
                "Set ESIBD_ESI_WORKER_PYTHON to a 64-bit Python 3.10+ python.exe."
            )
        return str(console)
    return executable


def _worker_python():
    if getattr(sys, "frozen", False):
        directory = Path(__file__).resolve().parents[2] / "python"
        for name in ("python.exe", "python314.dll", "python314.zip", "python314._pth"):
            path = directory / name
            if path.is_symlink() or not path.is_file():
                raise RuntimeError(
                    f"Bundled ESI Python is incomplete: {path}. "
                    "Restore the complete esi/ plugin folder; external Python is not required."
                )
        return [str(directory / "python.exe")]
    override = os.environ.get("ESIBD_ESI_WORKER_PYTHON")
    if override:
        return [_console_python(override)]
    return [_console_python(sys.executable)]


class ESIProcessProxy:
    """Serialize calls and terminate only ESI when a native call stalls or crashes."""

    def __init__(self, controller_kwargs, *, startup_timeout_s=30., controller_file=None):
        self._request_lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._close_lock = threading.Lock()
        self._replies = queue.Queue()
        self._closed = False
        self._closed_reason = "ESI worker is closed"
        self._sequence = 0
        self._stderr_lock = threading.Lock()
        self._stderr_tail = bytearray()
        environment = os.environ.copy()
        if getattr(sys, "frozen", False):
            environment.pop("PYTHONHOME", None)
            environment.pop("PYTHONPATH", None)
        self._command = [*_worker_python(), "-u", str(Path(__file__).resolve())]
        self._process = subprocess.Popen(
            self._command,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=environment, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self._stderr_reader = threading.Thread(target=self._read_stderr, daemon=True)
        self._stderr_reader.start()
        self._reader = threading.Thread(target=self._read_replies, daemon=True)
        self._reader.start()
        try:
            self._send({"kwargs": controller_kwargs, "controller_file": controller_file})
            response = self._next_reply(time.monotonic() + startup_timeout_s)
            if response.get("kind") != "ready":
                message = response.get("message", "ESI worker startup failed")
                if response.get("traceback"):
                    message += "\n" + response["traceback"]
                raise RuntimeError(message)
        except BaseException as exc:
            returncode = self._exit_code()
            self.close()
            if isinstance(exc, Exception):
                raise RuntimeError(self._failure_message(
                    f"ESI worker startup failed: {exc}", returncode)) from exc
            raise

    @property
    def closed(self):
        return self._closed or self._process.poll() is not None

    def _read_replies(self):
        try:
            while True:
                self._replies.put(_receive(self._process.stdout))
        except Exception as exc:
            self._replies.put({"kind": "exited", "message": str(exc)})

    def _read_stderr(self):
        # Drain continuously: native debug output must not fill a pipe and block I/O.
        while chunk := self._process.stderr.read1(4096):
            with self._stderr_lock:
                self._stderr_tail.extend(chunk)
                del self._stderr_tail[:-8192]

    def _failure_message(self, message, returncode):
        status = "still running before termination" if returncode is None else (
            f"{returncode} (0x{returncode & 0xFFFFFFFF:08X})")
        with self._stderr_lock:
            stderr = bytes(self._stderr_tail).decode("utf-8", errors="replace").strip()
        return (f"{message}; interpreter={self._command[:-2]!r}; exit code={status}; "
                f"worker stderr: {stderr or '(empty)'}")

    def _exit_code(self):
        # Pipe EOF can arrive just before the OS publishes the child's exit status.
        try:
            return self._process.wait(timeout=.1)
        except subprocess.TimeoutExpired:
            return None

    def _send(self, request):
        _send(self._process.stdin, request, self._send_lock)

    def _next_reply(self, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("ESI worker response timed out")
        try:
            return self._replies.get(timeout=remaining)
        except queue.Empty as exc:
            raise TimeoutError("ESI worker response timed out") from exc

    def call_method(self, name, *args, rpc_timeout_s, **kwargs):
        cancel = kwargs.pop("cancel_event", None)
        progress = kwargs.pop("on_discharge", None)
        return self._request(
            {"op": "call", "name": name, "args": args, "kwargs": kwargs,
             "cancellable": cancel is not None, "progress": progress is not None},
            rpc_timeout_s, cancel=cancel, progress=progress,
        )

    def get_attribute(self, name, *, timeout_s):
        return self._request({"op": "getattr", "name": name}, timeout_s)

    def set_attribute(self, name, value, *, timeout_s):
        return self._request({"op": "setattr", "name": name, "value": value}, timeout_s)

    def wait_for_idle(self, timeout_s):
        if not self._request_lock.acquire(timeout=timeout_s):
            return False
        self._request_lock.release()
        return True

    def _request(self, request, timeout, *, cancel=None, progress=None):
        deadline = time.monotonic() + timeout
        if not self._request_lock.acquire(timeout=timeout):
            raise TimeoutError("ESI worker request lock timed out")
        try:
            if self.closed:
                raise RuntimeError(self._closed_reason)
            self._sequence += 1
            request["id"] = self._sequence
            self._send(request)
            cancellation_sent = False
            while True:
                if self.closed:
                    if not self._closed:
                        returncode = self._exit_code()
                        self.close()
                        raise RuntimeError(self._failure_message(
                            "ESI DLL worker exited unexpectedly", returncode))
                    raise RuntimeError(self._closed_reason)
                if cancel is not None and cancel.is_set() and not cancellation_sent:
                    self._send({"op": "cancel", "id": request["id"]})
                    cancellation_sent = True
                try:
                    response = self._next_reply(min(deadline, time.monotonic() + .05))
                except TimeoutError:
                    if time.monotonic() >= deadline:
                        raise
                    continue
                if response.get("kind") == "exited":
                    self._closed_reason = "ESI DLL worker exited unexpectedly"
                    returncode = self._exit_code()
                    self.close()
                    raise RuntimeError(self._failure_message(self._closed_reason, returncode))
                if response.get("id") != request["id"]:
                    self._closed_reason = "ESI worker response does not match its request"
                    self.close()
                    raise RuntimeError(self._closed_reason)
                if response.get("kind") == "progress":
                    if progress is not None:
                        try:
                            progress(response["value"])
                        except BaseException:
                            self.close()
                            raise
                    continue
                if response.get("poisoned"):
                    self._closed_reason = f"ESI DLL call blocked during {request['name']}"
                    self.close()
                    if response["ok"]:
                        raise RuntimeError(self._closed_reason)
                if not response["ok"]:
                    error = response["error"]
                    error_type = getattr(builtins, error["type"], RuntimeError)
                    if not isinstance(error_type, type) or not issubclass(error_type, Exception):
                        error_type = RuntimeError
                    exc = error_type(error["message"])
                    if hasattr(exc, "add_note"):
                        exc.add_note(error["traceback"])
                    raise exc
                return response["value"]
        except (TimeoutError, EOFError, BrokenPipeError, OSError) as exc:
            self._closed_reason = f"ESI worker failed during {request['name']}: {exc}"
            returncode = self._exit_code()
            self.close()
            raise RuntimeError(self._failure_message(self._closed_reason, returncode)) from exc
        except RuntimeError:
            if self._process.poll() is not None:
                self.close()
            raise
        finally:
            self._request_lock.release()

    def close(self):
        # Never wait for the request lock: OFF must be able to kill a blocked call.
        with self._close_lock:
            self._closed = True
            if self._process.poll() is None:
                self._process.terminate()
                try:
                    self._process.wait(timeout=1.)
                except subprocess.TimeoutExpired:
                    self._process.kill()
                    self._process.wait(timeout=1.)
            self._reader.join(timeout=1.)
            self._stderr_reader.join(timeout=1.)
            self._process.stdin.close()
            if not self._reader.is_alive():
                self._process.stdout.close()
            if not self._stderr_reader.is_alive():
                self._process.stderr.close()
            return self._process.poll() is not None


def _worker_main():
    # Reserve a separate protocol fd before redirecting native DLL stdout prints.
    output = os.fdopen(os.dup(sys.stdout.fileno()), "wb", buffering=0)
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    output_lock = threading.Lock()
    stream = sys.stdin.buffer
    try:
        setup = _receive(stream)
        if sys.version_info < (3, 10):
            raise RuntimeError("The ESI worker requires Python 3.10 or newer")
        if struct.calcsize("P") != 8:
            raise RuntimeError("The ESI worker requires 64-bit Python for the bundled DLL")
        controller_file = setup.get("controller_file")
        if controller_file is not None:
            spec = importlib.util.spec_from_file_location("_esi_test_controller", controller_file)
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            controller_type = module.Controller
        else:
            root = Path(__file__).resolve().parents[1]
            spec = importlib.util.spec_from_file_location(
                "_esibd_esi_worker_runtime", root / "__init__.py",
                submodule_search_locations=[str(root)],
            )
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            controller_type = sys.modules[spec.name + ".esi.esi"]._ESIController
        controller = controller_type(**setup["kwargs"])
        _send(output, {"kind": "ready"}, output_lock)
    except Exception as exc:
        _send(output, {"kind": "startup_error", "message": str(exc),
                       "traceback": traceback.format_exc()}, output_lock)
        return

    requests = queue.Queue()
    events = {}
    events_lock = threading.Lock()

    def read_requests():
        try:
            while True:
                request = _receive(stream)
                with events_lock:
                    if request["op"] == "cancel":
                        event = events.get(request["id"])
                        if event is not None:
                            event.set()
                        continue
                    events[request["id"]] = threading.Event()
                requests.put(request)
        except (EOFError, OSError):
            # Parent crash: even a permanently blocked DLL must not leave an orphan
            # owning the COM port and prevent Explorer's crash-resume connection.
            os._exit(0)

    threading.Thread(target=read_requests, daemon=True).start()
    while (request := requests.get()) is not None:
        try:
            if request["op"] == "call":
                kwargs = request["kwargs"]
                if request["cancellable"]:
                    kwargs["cancel_event"] = events[request["id"]]
                if request["progress"]:
                    kwargs["on_discharge"] = lambda value: _send(
                        output, {"kind": "progress", "value": value, "id": request["id"]}, output_lock)
                value = getattr(controller, request["name"])(*request["args"], **kwargs)
            elif request["op"] == "getattr":
                value = getattr(controller, request["name"])
            elif request["op"] == "setattr":
                setattr(controller, request["name"], request["value"])
                value = None
            else:
                raise ValueError("Unknown ESI worker request")
            response = {"ok": True, "value": value}
        except Exception as exc:
            response = {"ok": False, "error": {
                "type": type(exc).__name__, "message": str(exc),
                "traceback": traceback.format_exc(),
            }}
        with events_lock:
            events.pop(request["id"], None)
        response["poisoned"] = bool(getattr(controller, "_transport_poisoned", False))
        response["id"] = request["id"]
        _send(output, response, output_lock)


if __name__ == "__main__":
    with contextlib.suppress(BrokenPipeError):
        _worker_main()
