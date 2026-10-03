"""TPG366 protocol exercised against a stateful serial instrument, without Qt."""
from collections import deque
import importlib.util
import math
from pathlib import Path
import sys
from threading import Event
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
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
            self.awaiting_enq = True
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


def link(port=None):
    return protocol.TPG366Link(port or FakeSerial(), Event(), timeout=.02)


def test_real_handshake_uses_enq_alone_and_never_waits_for_a_trailing_nak():
    driver = link()
    driver.initialize()
    result = driver.read_pressures()
    assert driver.port.writes == [b"\x03", b"AYT\r", b"\x05", b"TID\r", b"\x05",
                                   b"UNI\r", b"\x05", b"PRX\r", b"\x05", b"UNI\r", b"\x05"]
    assert result.pressures == pytest.approx([i * 10**-i + .2345 * 10**-i for i in range(1, 7)])
    assert result.unit == "hPa"
    assert len(driver.gauges) == 6
    assert not driver.port.buffer


@pytest.mark.parametrize("unit,factor", [(0, 1), (1, 1013.25 / 760), (2, .01),
                                        (3, 1013.25 / 760_000), (4, 1)])
def test_all_documented_pressure_units_convert_to_mbar_without_writing_uni(unit, factor):
    port = FakeSerial(unit=unit)
    port.responses["PRX"] = frame(values=["1.0000E+00"] * 6)
    result = link(port).read_pressures()
    assert result.pressures == pytest.approx([factor] * 6)
    assert all(data in {b"UNI\r", b"PRX\r", b"\x05"} for data in port.writes)


@pytest.mark.parametrize("status", range(1, 7))
def test_non_ok_readings_are_nan_not_zero_or_the_controller_sentinel(status):
    result = link(FakeSerial(statuses=(0, status, 0, 0, 0, 0))).read_pressures()
    assert math.isnan(result.pressures[1])
    assert math.isfinite(result.pressures[0])
    assert result.statuses[1] == status


@pytest.mark.parametrize("unit", [5, 6, -1, "", "garbage"])
def test_unknown_or_voltage_units_cannot_be_presented_as_pressure(unit):
    with pytest.raises(protocol.ProtocolError, match="unit"):
        link(FakeSerial(unit=unit)).read_pressures()


def test_later_polls_use_two_transactions():
    driver = link()
    driver.initialize()
    driver.read_pressures()
    driver.port.writes.clear()
    driver.read_pressures()
    assert driver.port.writes == [b"PRX\r", b"\x05", b"UNI\r", b"\x05"]


def test_unit_change_between_polls_discards_the_next_frame():
    driver = link()
    driver.initialize()
    driver.read_pressures()
    driver.port.responses["UNI"] = "1"  # changed on the front panel between two polls
    with pytest.raises(protocol.ProtocolError, match="changed"):
        driver.read_pressures()


def test_front_panel_unit_change_invalidates_entire_frame():
    port = FakeSerial()
    port.units.extend(["0", "1"])
    with pytest.raises(protocol.ProtocolError, match="changed"):
        link(port).read_pressures()


@pytest.mark.parametrize("bad_frame", ["0,1E-3", frame() + ",0,1E-3", frame().replace("0,", "7,", 1),
                                       frame().replace("1.2345E-01", "nan"),
                                       frame().replace("1.2345E-01", "inf"),
                                       frame().replace("1.2345E-01", "1E999"),
                                       frame().replace("1.2345E-01", "bad")])
def test_corrupt_measurement_is_rejected(bad_frame):
    with pytest.raises(protocol.ProtocolError):
        protocol.parse_pressures(bad_frame, 0, 0.)


def test_valid_negative_linear_gauge_reading_is_not_clamped():
    result = protocol.parse_pressures(frame(values=["-1.0000E-03"] * 6), 0, 0.)
    assert result.pressures == (-.001,) * 6


def test_startup_stream_is_discarded_only_during_initial_synchronization():
    port = FakeSerial()
    port.before_ack = frame().encode() + b"\r\n"
    driver = link(port)
    driver.initialize()
    port.before_ack = b"unsolicited\r\n"
    with pytest.raises(protocol.ProtocolError, match="ACK"):
        driver.read_pressures()


def test_startup_ack_survives_every_partial_stream_boundary():
    # Purging the host buffer does not remove bytes still in flight. In
    # particular, a lone terminal LF must not swallow the next complete ACK.
    stream = frame().encode("ascii") + b"\r\n"
    for offset in range(len(stream) + 1):
        port = FakeSerial()
        port.before_ack = stream[offset:]
        driver = link(port)
        driver.initialize()
        assert driver.identification == port.responses["AYT"]
        assert len(driver.read_pressures().pressures) == 6
        assert port.writes.count(b"AYT\r") == 1  # No implicit retry.


def test_initial_sync_does_not_discard_a_negative_ack_after_a_partial_line():
    port = FakeSerial()
    port.before_ack, port.ack = b"\n", b"\x15\r\n"
    with pytest.raises(protocol.ProtocolError, match=r"AYT.*waiting for ACK.*NAK"):
        link(port).initialize()
    assert port.writes == [b"\x03", b"AYT\r"]


@pytest.mark.parametrize("ack", [b"", b"\x06", b"\x06\r", b"\x06\n", b"\x06X\r\n",
                                  b"junk\x06\r\n", b"\x06\x06\r\n", b"\x15\x06\r\n",
                                  b"\x00\x06\r\n", b"\x06\n\r\n"])
def test_startup_never_accepts_an_incomplete_or_corrupt_ack(ack):
    port = FakeSerial()
    port.ack = ack
    with pytest.raises(protocol.ProtocolError, match=r"AYT.*waiting for ACK"):
        link(port).initialize()
    assert port.writes == [b"\x03", b"AYT\r"]  # No ENQ, retry or extra command.


def test_normal_query_still_rejects_a_partial_line_before_ack():
    port = FakeSerial()
    port.before_ack = b"\n"
    with pytest.raises(protocol.ProtocolError, match="ACK"):
        link(port).query("UNI")
    assert port.writes == [b"UNI\r"]


@pytest.mark.parametrize("phase", ["waiting for ACK", "waiting for data"])
def test_timeout_identifies_the_command_and_handshake_phase(phase):
    port = FakeSerial()
    if phase == "waiting for ACK":
        port.ack = b""
    else:
        def remove_data(data):
            if data == b"\x05":
                port.on_read = port.buffer.clear
        port.on_write = remove_data
    with pytest.raises(protocol.ProtocolError, match=f"UNI.*{phase}.*timed out"):
        link(port).query("UNI")
    assert port.writes == ([b"UNI\r"] if phase == "waiting for ACK" else [b"UNI\r", b"\x05"])


@pytest.mark.parametrize("phase", ["sending command", "sending ENQ"])
def test_io_error_identifies_the_command_and_handshake_phase(phase):
    port = FakeSerial()
    def fail(data):
        if data == (b"UNI\r" if phase == "sending command" else b"\x05"):
            raise OSError("USB transport failed")
    port.on_write = fail
    with pytest.raises(protocol.ProtocolError, match=f"UNI.*{phase}.*USB transport failed"):
        link(port).query("UNI")


@pytest.mark.parametrize("ack", [b"\x15\r\n", b"junk\r\n", b"\x06", b""])
def test_rejected_or_partial_ack_does_not_send_enq(ack):
    driver = link()
    driver.port.ack = ack
    with pytest.raises(protocol.ProtocolError):
        driver.query("UNI")
    assert driver.port.writes == [b"UNI\r"]


@pytest.mark.parametrize("synchronizing", [False, True])
def test_off_between_ack_and_enq_cancels_without_another_write(synchronizing):
    driver = link()
    driver.port.before_ack = b"\n" if synchronizing else b""
    def cancel_on_last_ack_byte():
        if driver.port.buffer == b"\n":
            driver.cancelled.set()
    driver.port.on_read = cancel_on_last_ack_byte
    command = "AYT" if synchronizing else "UNI"
    with pytest.raises(protocol.Cancelled):
        driver.query(command, synchronizing=synchronizing)
    assert driver.port.writes == [command.encode("ascii") + b"\r"]


def test_partial_reply_timeout_is_bounded():
    driver = link()
    driver.port.responses["UNI"] = "0"
    def truncate(data):
        if data == b"\x05":
            driver.port.on_read = lambda: driver.port.buffer.clear()
    driver.port.on_write = truncate
    started = time.monotonic()
    with pytest.raises(protocol.ProtocolError, match="timed out"):
        driver.query("UNI")
    assert time.monotonic() - started < .2


@pytest.mark.parametrize("command", ["SEN", "SEN,2,2,2,2,2,2", "UNI,0", "DGS", "RES", "SP1", "SAV", "COM", "PRX\rSEN"])
def test_settings_and_control_commands_cannot_be_sent(command):
    driver = link()
    with pytest.raises(ValueError, match="read-only"):
        driver.query(command)
    assert driver.port.writes == []


def test_wrong_device_and_missing_gauge_identifications_fail_before_acquisition():
    port = FakeSerial()
    port.responses["AYT"] = "TPG362,PTG,123,010300,010100"
    with pytest.raises(protocol.ProtocolError, match="TPG366"):
        link(port).initialize()
    assert b"PRX\r" not in port.writes
    port = FakeSerial()
    port.responses["TID"] = "TPR/PCR,IKR"
    with pytest.raises(protocol.ProtocolError, match="six"):
        link(port).initialize()


@pytest.mark.parametrize("reply,match", [(b"\xff\r\n", "Non-ASCII"), (b"x" * 513, "512")])
def test_non_ascii_and_oversized_replies_fail(reply, match):
    driver = link()
    def replace(data):
        if data == b"\x05":
            driver.port.buffer.extend(reply)
    driver.port.on_write = replace
    with pytest.raises(protocol.ProtocolError, match=match):
        driver.query("UNI")


def test_short_serial_write_is_not_accepted():
    driver = link()
    driver.port.write = lambda data: len(data) - 1
    with pytest.raises(protocol.ProtocolError, match="Incomplete serial write"):
        driver.query("UNI")


@pytest.mark.parametrize("startup_tail", [b"", b"\n"])
def test_real_pyserial_exchange_on_a_pseudo_terminal(startup_tail):
    """Pass actual bytes through the OS and pyserial, with fragmented replies."""
    import os
    import subprocess
    if os.name != "posix":
        pytest.skip("POSIX pseudo-terminal test")
    script = r'''
import os, pty, select, sys, threading, time
import serial
from test_tpg366_protocol import FakeSerial, protocol
master, slave = pty.openpty()
port = serial.Serial(os.ttyname(slave), 9600, timeout=.05, write_timeout=1.)
stop = threading.Event()
instrument = FakeSerial()
instrument.before_ack = bytes.fromhex(sys.argv[1])
errors = []
def serve():
    command = bytearray()
    try:
        while not stop.is_set():
            if not select.select([master], [], [], .05)[0]:
                continue
            for char in os.read(master, 128):
                if char in (3, 5):
                    instrument.write(bytes([char]))
                elif char == 13:
                    instrument.write(bytes(command) + b'\r')
                    command.clear()
                else:
                    command.append(char)
                while instrument.buffer:
                    chunk = bytes(instrument.buffer[:2])
                    del instrument.buffer[:2]
                    os.write(master, chunk)
                    time.sleep(.001)
    except BaseException as exc:
        errors.append(exc)
worker = threading.Thread(target=serve, daemon=True)
worker.start()
try:
    driver = protocol.TPG366Link(port, threading.Event())
    driver.initialize()
    reading = driver.read_pressures()
    assert len(reading.pressures) == 6 and abs(reading.pressures[0] - .12345) < 1e-9
    assert instrument.writes.count(b'\x05') == 5
finally:
    stop.set()
    worker.join(1)
    port.close()
    os.close(master)
    os.close(slave)
assert not errors, errors
'''
    result = subprocess.run([sys.executable, "-c", script, startup_tail.hex()], cwd=ROOT / "tests",
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
