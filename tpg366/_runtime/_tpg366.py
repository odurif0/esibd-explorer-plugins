"""Read-only TPG 366 mnemonic protocol (Pfeiffer BG 5511 BEN /A, 2022-07).

The caller owns the serial port and serializes all operations, including close.
No gauge, relay, calibration, unit or EEPROM setting is written.
SystemAwakeRequest (same as the CGC runtimes) keeps Windows from idle sleep
while the port is open.
SPDX-License-Identifier: GPL-2.0-or-later
"""
from __future__ import annotations

from dataclasses import dataclass
import functools
import math
import threading
from threading import Event
import time


class ProtocolError(RuntimeError):
    """The reply cannot be used as a pressure measurement."""


class Cancelled(Exception):
    """The user stopped communication; do not issue another command."""


class _Rejected(Exception):
    """The controller answered NAK: the string was rejected and not executed."""

    def __init__(self, ack: bytes):
        super().__init__(ack)
        self.ack = ack


STATUS = (
    "OK", "Underrange", "Overrange", "Sensor error", "Sensor off",
    "No sensor", "Identification error",
)
# UNI codes, not an assumption about the front-panel display. 1 Torr = 101325/760 Pa.
# BG 5501 BEN lists code 0 as mbar; hPa (code 4) is the factory default.
UNITS = {0: ("mbar", 1.0), 1: ("Torr", 1013.25 / 760), 2: ("Pa", .01),
         3: ("Micron", 1013.25 / 760_000), 4: ("hPa", 1.0)}
READ_COMMANDS = frozenset({"AYT", "TID", "UNI", "PRX"})


@dataclass(frozen=True)
class Reading:
    pressures: tuple[float, ...]
    statuses: tuple[int, ...]
    unit: str
    received_at: float


def parse_pressures(reply: str, unit: int, received_at: float) -> Reading:
    """Require all six status/value pairs; never turn a sentinel into a pressure."""
    if unit not in UNITS:
        raise ProtocolError("TPG366 is not reporting pressure units (UNI=%s). Select a pressure unit on the controller." % unit)
    fields = [field.strip() for field in reply.split(",")]
    if len(fields) != 12:
        raise ProtocolError(f"Expected six status/pressure pairs, received {reply!r}.")
    pressures, statuses = [], []
    label, factor = UNITS[unit]
    for i in range(0, 12, 2):
        if fields[i] not in {str(n) for n in range(len(STATUS))}:
            raise ProtocolError(f"Unknown gauge status {fields[i]!r} on input {i // 2 + 1}.")
        status = int(fields[i])
        try:
            value = float(fields[i + 1])
        except ValueError as exc:
            raise ProtocolError(f"Invalid pressure {fields[i + 1]!r} on input {i // 2 + 1}.") from exc
        value *= factor
        if not math.isfinite(value):
            raise ProtocolError(f"Non-finite pressure on input {i // 2 + 1}.")
        # A negative, finite reading with status OK can reflect a linear gauge's
        # offset. Keep the actual value, even though it cannot appear on log Y.
        pressures.append(value if status == 0 else math.nan)
        statuses.append(status)
    return Reading(tuple(pressures), tuple(statuses), label, received_at)


class TPG366Link:
    """One bounded, synchronous serial connection; use from a worker thread."""

    # BG 5511 "Error detection protocol": after <NAK> (transmission or programming
    # error) the host sends the mnemonic again. Read-only commands only; bounded.
    NAK_RETRANSMISSIONS = 2

    def __init__(self, port, cancelled: Event, timeout: float = 1.0, etx_settle_s: float = 0.2):
        self.port = port
        self.cancelled = cancelled
        self.timeout = timeout
        # <ETX> clears the controller's input buffer. A command sent right behind
        # it (often in the same USB packet) can be cleared too: NAK or no reply.
        self.etx_settle_s = etx_settle_s
        self.identification = ""
        self.gauges: tuple[str, ...] = ()
        self.unit: int | None = None  # UNI code confirmed by the last transaction
        self.nak_count = 0  # NAKs received; each was followed by a retransmission or an error

    def _check_cancelled(self):
        if self.cancelled.is_set():
            raise Cancelled

    def _write(self, data: bytes):
        self._check_cancelled()
        if self.port.write(data) != len(data):
            raise ProtocolError("Incomplete serial write.")

    def _line(self, deadline: float) -> bytes:
        data = bytearray()
        while time.monotonic() < deadline:
            self._check_cancelled()
            char = self.port.read(1)  # port timeout must be short (50 ms)
            if char:
                data.extend(char)
                if data.endswith(b"\r\n"):
                    return bytes(data[:-2])
                if len(data) > 512:
                    raise ProtocolError("Serial reply exceeded 512 bytes.")
        raise ProtocolError(f"Serial reply timed out (partial reply: {bytes(data)!r}).")

    def query(self, command: str, *, synchronizing: bool = False) -> str:
        if command not in READ_COMMANDS:
            raise ValueError(f"Not a supported read-only command: {command!r}")
        for attempt in range(1, self.NAK_RETRANSMISSIONS + 2):
            try:
                return self._exchange(command, synchronizing)
            except _Rejected as rejected:
                self.nak_count += 1
                if attempt > self.NAK_RETRANSMISSIONS:
                    raise ProtocolError(f"{command} [waiting for ACK]: TPG366 returned NAK "
                                        f"(received {rejected.ack!r}) to {attempt} transmissions.") from None

    def _exchange(self, command: str, synchronizing: bool) -> str:
        deadline = time.monotonic() + self.timeout
        phase = "sending command"
        try:
            self._write(command.encode("ascii") + b"\r")
            phase = "waiting for ACK"
            ack = self._line(deadline)
            # A host RX purge cannot remove a startup frame still in flight.
            # Its last LF may prefix ACK in the same CRLF-delimited read. Only
            # initial synchronization may discard residual line endings or
            # whole data frames; embedded/corrupt controls are never an ACK.
            control = ack.lstrip(b"\r\n") if synchronizing else ack
            while synchronizing and control not in {b"\x06", b"\x15"}:
                if b"\x06" in ack or b"\x15" in ack:
                    raise ProtocolError(f"Malformed acknowledgement: {ack!r}.")
                ack = self._line(deadline)
                control = ack.lstrip(b"\r\n")
            if control == b"\x15":
                raise _Rejected(ack)
            if control != b"\x06":
                raise ProtocolError(f"Expected ACK for {command}, received {ack!r}.")
            phase = "sending ENQ"
            self._write(b"\x05")  # ENQ alone: no CR, and no additional NAK to read.
            phase = "waiting for data"
            return self._line(deadline).decode("ascii")
        except UnicodeDecodeError as exc:
            raise ProtocolError(f"{command} [{phase}]: Non-ASCII serial reply.") from exc
        except (ProtocolError, OSError) as exc:
            raise ProtocolError(f"{command} [{phase}]: {exc}") from exc

    def initialize(self):
        self._check_cancelled()
        self.port.reset_input_buffer()
        self._write(b"\x03")  # stop automatic output / clear the command buffer
        flush = getattr(self.port, "flush", None)
        if callable(flush):
            flush()  # the ETX has left the host before the settling pause starts
        if self.cancelled.wait(self.etx_settle_s):
            raise Cancelled
        self.port.reset_input_buffer()
        self.identification = self.query("AYT", synchronizing=True)
        parts = self.identification.split(",")
        if len(parts) != 5 or parts[0].strip().replace(" ", "").upper() != "TPG366":
            raise ProtocolError(f"The selected port did not identify a TPG366: {self.identification!r}.")
        self.gauges = tuple(part.strip() for part in self.query("TID").split(","))
        if len(self.gauges) != 6:
            raise ProtocolError(f"Expected six gauge identifications, received {self.gauges!r}.")
        self.unit = self._unit()

    def _unit(self) -> int:
        reply = self.query("UNI").strip()
        if reply not in {str(code) for code in UNITS}:
            raise ProtocolError(f"Unsupported pressure unit UNI={reply!r}; select mbar, hPa, Pa, Torr or Micron on the controller.")
        return int(reply)

    def read_pressures(self) -> Reading:
        # Two transactions per poll: PRX, then UNI compared with the unit confirmed
        # by the previous transaction (initialization or the last poll). A front-
        # panel change on either side of PRX discards the frame, never mislabels it.
        if self.unit is None:
            self.unit = self._unit()
        reply = self.query("PRX")
        received_at = time.time()
        unit = self._unit()
        if unit != self.unit:
            raise ProtocolError("Pressure unit changed during the read; measurement discarded. Reconnect to continue.")
        self._check_cancelled()
        return parse_pressures(reply, unit, received_at)


_POWER_REQUEST_SYSTEM_REQUIRED = 1  # POWER_REQUEST_TYPE PowerRequestSystemRequired


def _reason_context_type():
    """REASON_CONTEXT with POWER_REQUEST_CONTEXT_SIMPLE_STRING (Windows x64 layout)."""
    import ctypes

    class ReasonContext(ctypes.Structure):
        # The union is sized for its detailed form: HMODULE, two ULONGs, LPWSTR*.
        _fields_ = [("Version", ctypes.c_uint32), ("Flags", ctypes.c_uint32),
                    ("SimpleReasonString", ctypes.c_wchar_p), ("_detailed_tail", ctypes.c_void_p * 2)]

    return ReasonContext


@functools.lru_cache(maxsize=1)
def _power_api():
    """kernel32 power-request functions (Windows 7+); raises elsewhere."""
    import ctypes
    from types import SimpleNamespace

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    reason_context = _reason_context_type()
    kernel32.PowerCreateRequest.argtypes = [ctypes.POINTER(reason_context)]
    kernel32.PowerCreateRequest.restype = ctypes.c_void_p
    for name in ("PowerSetRequest", "PowerClearRequest"):
        getattr(kernel32, name).argtypes = [ctypes.c_void_p, ctypes.c_int]
        getattr(kernel32, name).restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int

    def create(reason):
        context = reason_context(0, 1, reason)  # POWER_REQUEST_CONTEXT_VERSION, _SIMPLE_STRING
        handle = kernel32.PowerCreateRequest(ctypes.byref(context))
        if not handle or handle == ctypes.c_void_p(-1).value:
            raise OSError(ctypes.get_last_error(), "PowerCreateRequest failed")
        return handle

    return SimpleNamespace(create=create, set=kernel32.PowerSetRequest,
                           clear=kernel32.PowerClearRequest, close=kernel32.CloseHandle)


class SystemAwakeRequest:
    """Keep Windows from sleeping on idle while an instrument port is claimed.

    During sleep the outputs keep their last state with no software
    supervision. Each holder owns its own Windows power request, so overlapping
    holders never cancel each other, and ``powercfg /requests`` names the
    instrument. A manual sleep, a closed lid or the power button are not
    prevented. Outside Windows, or if the API fails, this is a no-op.
    """

    def __init__(self, reason: str):
        self.reason = str(reason)
        self._handle = None
        self._lock = threading.Lock()

    @property
    def held(self) -> bool:
        return self._handle is not None

    def hold(self, held: bool) -> bool:
        """Set or clear the request; return whether it is held now. Never raises."""
        with self._lock:
            try:
                if held and self._handle is None:
                    api = _power_api()
                    handle = api.create(self.reason)
                    if not api.set(handle, _POWER_REQUEST_SYSTEM_REQUIRED):
                        api.close(handle)
                    else:
                        self._handle = handle
                elif not held and self._handle is not None:
                    handle, self._handle = self._handle, None
                    api = _power_api()
                    try:
                        api.clear(handle, _POWER_REQUEST_SYSTEM_REQUIRED)
                    finally:
                        api.close(handle)  # Closing the handle also ends the request.
            except Exception:  # noqa: BLE001 - power management must never affect instrument control
                pass
            return self._handle is not None
