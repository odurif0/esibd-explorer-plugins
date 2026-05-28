"""Process-isolated controller execution for CGC instrument drivers."""

from __future__ import annotations

import importlib
import multiprocessing as mp
import traceback
from typing import Any


def _resolve_object(import_path: str) -> Any:
    module_name, separator, qualname = import_path.partition(":")
    if not separator or not module_name or not qualname:
        raise ValueError(
            "Controller import path must use the format 'module.submodule:QualifiedName'."
        )

    obj = importlib.import_module(module_name)
    for part in qualname.split("."):
        obj = getattr(obj, part)
    return obj


def _serialize_exception(exc: BaseException) -> dict[str, str]:
    return {
        "module": exc.__class__.__module__,
        "name": exc.__class__.__name__,
        "message": str(exc),
        "traceback": traceback.format_exc(),
    }


def _restore_exception(payload: dict[str, str]) -> BaseException:
    exc_type: type[BaseException] = RuntimeError

    try:
        module = importlib.import_module(payload["module"])
        candidate = getattr(module, payload["name"])
        if isinstance(candidate, type) and issubclass(candidate, BaseException):
            exc_type = candidate
    except Exception:
        exc_type = RuntimeError

    return exc_type(payload["message"])


def _controller_worker_main(connection, controller_path: str, controller_kwargs: dict[str, Any]):
    controller = None
    try:
        controller_cls = _resolve_object(controller_path)
        controller = controller_cls(**controller_kwargs)
        connection.send({"kind": "ready"})
    except Exception as exc:  # pragma: no cover - startup failure path
        connection.send(
            {
                "kind": "startup_error",
                "error": _serialize_exception(exc),
            }
        )
        connection.close()
        return

    try:
        while True:
            try:
                request = connection.recv()
            except EOFError:
                break

            operation = request["op"]
            if operation == "close":
                break

            try:
                if operation == "call":
                    target = getattr(controller, request["name"])
                    value = target(*request["args"], **request["kwargs"])
                elif operation == "getattr":
                    value = getattr(controller, request["name"])
                elif operation == "setattr":
                    setattr(controller, request["name"], request["value"])
                    value = None
                else:
                    raise ValueError(f"Unknown controller worker operation: {operation}")

                connection.send(
                    {
                        "kind": "response",
                        "ok": True,
                        "value": value,
                        "transport_poisoned": bool(
                            getattr(controller, "_transport_poisoned", False)
                        ),
                    }
                )
            except Exception as exc:
                connection.send(
                    {
                        "kind": "response",
                        "ok": False,
                        "error": _serialize_exception(exc),
                        "transport_poisoned": bool(
                            getattr(controller, "_transport_poisoned", False)
                        ),
                    }
                )
    finally:
        connection.close()


class ControllerProcessProxy:
    """Run a controller instance inside a dedicated worker process."""

    def __init__(
        self,
        controller_path: str,
        controller_kwargs: dict[str, Any],
        *,
        label: str,
        startup_timeout_s: float = 30.0,
    ):
        self._label = label
        self._closed = False
        self._closed_reason = ""

        context = mp.get_context("spawn")
        parent_conn, child_conn = context.Pipe()
        process = context.Process(
            target=_controller_worker_main,
            args=(child_conn, controller_path, controller_kwargs),
            daemon=True,
            name=f"{label.replace(' ', '_')}_worker",
        )
        process.start()
        child_conn.close()

        self._connection = parent_conn
        self._process = process

        try:
            response = self._recv_with_timeout(
                startup_timeout_s, action="worker startup"
            )
            if response.get("kind") == "startup_error":
                self._close_transport()
                raise _restore_exception(response["error"])
        except Exception:
            self.close()
            raise

    def call_method(
        self,
        method_name: str,
        *args,
        rpc_timeout_s: float,
        **kwargs,
    ):
        return self._request(
            {
                "op": "call",
                "name": method_name,
                "args": args,
                "kwargs": kwargs,
            },
            timeout_s=rpc_timeout_s,
            action=f"{method_name}()",
        )

    def get_attribute(self, attr_name: str, *, timeout_s: float):
        return self._request(
            {"op": "getattr", "name": attr_name},
            timeout_s=timeout_s,
            action=f"getattr({attr_name})",
        )

    def set_attribute(self, attr_name: str, value, *, timeout_s: float):
        return self._request(
            {"op": "setattr", "name": attr_name, "value": value},
            timeout_s=timeout_s,
            action=f"setattr({attr_name})",
        )

    def close(self):
        if self._closed:
            return

        try:
            if self._process.is_alive():
                try:
                    self._connection.send({"op": "close"})
                except (BrokenPipeError, EOFError, OSError):
                    pass
                self._process.join(timeout=1.0)
                if self._process.is_alive():
                    self._process.terminate()
                    self._process.join(timeout=1.0)
        finally:
            self._closed = True
            self._close_transport()

    def _request(self, payload: dict[str, Any], *, timeout_s: float, action: str):
        self._ensure_available()

        try:
            self._connection.send(payload)
        except (BrokenPipeError, EOFError, OSError) as exc:
            self._mark_closed(f"{self._label} worker is unavailable: {exc}")
            raise RuntimeError(self._closed_reason) from exc

        response = self._recv_with_timeout(timeout_s, action=action)
        return self._handle_response(response, action=action)

    def _recv_with_timeout(self, timeout_s: float, *, action: str):
        if self._closed:
            raise RuntimeError(self._closed_reason)

        if self._connection.poll(timeout_s):
            try:
                return self._connection.recv()
            except EOFError as exc:
                self._mark_closed(
                    f"{self._label} worker exited unexpectedly during {action}."
                )
                raise RuntimeError(self._closed_reason) from exc

        self._mark_closed(
            f"{self._label} worker timed out during {action}. "
            "The worker process was terminated."
        )
        raise RuntimeError(self._closed_reason)

    def _handle_response(self, response: dict[str, Any], *, action: str):
        poisoned = bool(response.get("transport_poisoned", False))
        if response.get("ok", False):
            if poisoned:
                self._mark_closed(
                    f"{self._label} worker became unusable during {action}. "
                    "Create a new instrument instance before retrying."
                )
                raise RuntimeError(self._closed_reason)
            return response.get("value")

        exc = _restore_exception(response["error"])
        if poisoned:
            self._mark_closed(
                f"{self._label} worker became unusable during {action}. "
                "Create a new instrument instance before retrying."
            )
        raise exc

    def _ensure_available(self):
        if self._closed:
            raise RuntimeError(self._closed_reason)
        if not self._process.is_alive():
            self._mark_closed(f"{self._label} worker is no longer running.")
            raise RuntimeError(self._closed_reason)

    def _mark_closed(self, reason: str):
        self._closed = True
        self._closed_reason = reason
        self._close_transport()

    def _close_transport(self):
        try:
            self._connection.close()
        except Exception:
            pass
        if self._process.is_alive():
            self._process.terminate()
            self._process.join(timeout=1.0)

    def __del__(self):  # pragma: no cover - best effort cleanup
        try:
            self.close()
        except Exception:
            pass
