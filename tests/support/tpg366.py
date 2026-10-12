"""TPG366 serial fakes shared by the TPG366 tests."""
from collections import deque
import importlib.util
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("tpg366_protocol_test", ROOT / "tpg366/_runtime/_tpg366.py")
protocol = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = protocol
spec.loader.exec_module(protocol)


def frame(statuses=(0,) * 6, values=None):
    values = values or [f"{i}.2345E-0{i}" for i in range(1, 7)]
    return ",".join(str(item) for pair in zip(statuses, values) for item in pair)


class FakeSerial:
    """A real byte-oriented ACK/ENQ exchange, not mocked high-level readings."""
    def __init__(self, *, unit=4, statuses=(0,) * 6, **kwargs):
        self.buffer = bytearray()
        self.writes = []
        self.responses = {
            "AYT": "TPG366,PTG28770,44990000,010300,010100",
            "TID": "TPR/PCR,IKR,PKR,CMR,IMR,PBR",
            "UNI": str(unit), "PRX": frame(statuses),
        }
        self.units = deque()
        self.command = None
        self.awaiting_enq = False
        self.is_open = True
        self.fail_close = False
        self.close_count = 0
        self.before_ack = b""
        self.ack = b"\x06\r\n"
        self.on_write = None
        self.on_read = None

    def reset_input_buffer(self):
        self.buffer.clear()

    def write(self, data):
        assert self.is_open
        self.writes.append(data)
        if self.on_write:
            self.on_write(data)
        if data == b"\x03":
            self.awaiting_enq = False
        elif data == b"\x05":
            assert self.awaiting_enq, "ENQ without a pending mnemonic"
            self.awaiting_enq = False
            reply = self.units.popleft() if self.command == "UNI" and self.units else self.responses[self.command]
            self.buffer.extend(reply.encode("ascii") + b"\r\n")
        else:
            assert data.endswith(b"\r") and b"\n" not in data
            assert not self.awaiting_enq, "A second command interrupted the handshake"
            self.command = data[:-1].decode("ascii")
            assert self.command in protocol.READ_COMMANDS, f"Unexpected device command: {data!r}"
            self.buffer.extend(self.before_ack + self.ack)
            self.before_ack = b""
            # A NAK rejects the string: nothing is pending, the host may resend it.
            self.awaiting_enq = not self.ack.startswith(b"\x15")
        return len(data)

    def read(self, n):
        if self.on_read:
            self.on_read()
        if not self.buffer:
            time.sleep(.001)
        data = bytes(self.buffer[:n])
        del self.buffer[:n]
        return data

    def close(self):
        self.close_count += 1
        if self.fail_close:
            raise OSError("simulated close failure")
        self.is_open = False
