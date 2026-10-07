"""JSON Lines logs of the Transmission plugin, for troubleshooting and later tuning.

One line per event: ``{"t": unix time, "seq": n, "event": name, ...data}``.
Each line is written and flushed at once, so a log survives a crash or a sleep.
Non-finite numbers become ``null``; numpy values become plain JSON. A logging
failure never interrupts the optimizer: it is kept in ``error`` and later
writes are skipped.
SPDX-License-Identifier: MIT
"""
from __future__ import annotations

import datetime as _datetime
import hashlib
import json
import math
import os
from pathlib import Path
import threading
import time

RUNS = "runs"
SESSION = "transmission_session.jsonl"


def clean(value):
    """A JSON-safe copy: dicts, lists, finite floats (non-finite -> None), str, int, bool, None."""
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [clean(v) for v in value]
    if hasattr(value, "tolist") and not isinstance(value, (str, bytes)):  # numpy arrays and scalars
        return clean(value.tolist())
    if isinstance(value, bool) or value is None or isinstance(value, (str, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Path):
        return str(value)
    return repr(value)


def digests(paths):
    """SHA-256 of files, as provenance of the code that ran (not a version pin)."""
    result = {}
    for path in paths:
        try:
            result[Path(path).name] = hashlib.sha256(Path(path).read_bytes()).hexdigest()
        except OSError as exc:
            result[Path(path).name] = f"unreadable: {exc}"
    return result


class JsonlLog:
    """Append-only JSON Lines file; optional size rotation (``path.1`` … ``path.N``)."""

    def __init__(self, path, *, max_bytes=0, backups=0):
        self.path = Path(path)
        self.max_bytes, self.backups = int(max_bytes), int(backups)
        self.lock = threading.Lock()
        self.seq = 0
        self.error = None
        self._stream = None
        self.closed = False

    def _open(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._stream = self.path.open("a", encoding="utf-8", newline="\n")

    def _rotate(self, size):
        if not (self.max_bytes > 0 and self.backups > 0) or self._stream.tell() + size < self.max_bytes:
            return
        if self._stream.tell() == 0:
            return
        self._stream.close()  # Windows cannot rename an open file.
        self._stream = None
        for index in range(self.backups, 0, -1):
            source = self.path if index == 1 else self.path.with_name(f"{self.path.name}.{index - 1}")
            if source.exists():
                os.replace(source, self.path.with_name(f"{self.path.name}.{index}"))
        self._open()

    def write(self, event, data=None, **fields):
        """Log one event. Never raises."""
        with self.lock:
            if self.error is not None or self.closed:
                return
            try:
                self.seq += 1
                record = {"t": time.time(), "seq": self.seq, "event": str(event)}
                record.update(clean(dict(data or {}, **fields)))
                text = json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n"
                if self._stream is None:
                    self._open()
                self._rotate(len(text.encode("utf-8")))
                self._stream.write(text)
                self._stream.flush()
            except Exception as exc:  # noqa: BLE001 - logging must never stop the optimizer
                self.error = f"{type(exc).__name__}: {exc}"

    def close(self):
        with self.lock:
            self.closed = True
            if self._stream is not None:
                try:
                    self._stream.close()
                except OSError:
                    pass
                self._stream = None


class NullLog:
    """Stands in when no log folder is available; ``error`` says why."""

    path = None

    def __init__(self, error=""):
        self.error = error

    def write(self, event, data=None, **fields):
        pass

    def close(self):
        pass


def run_log(directory, kind, *, keep=200, max_total_bytes=500_000_000):
    """A new log file for one run, quick sweep or revert.

    Older files are deleted beyond the ``keep`` newest or ``max_total_bytes`` in all
    (a full run takes about 1 to 5 MB).
    """
    folder = Path(directory) / RUNS
    stamp = _datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    log = JsonlLog(folder / f"transmission_{stamp}_{kind}.jsonl")
    try:
        folder.mkdir(parents=True, exist_ok=True)
        total = 0
        for index, path in enumerate(sorted(folder.glob("transmission_*.jsonl"), reverse=True)):
            total += path.stat().st_size
            if index >= keep - 1 or total > max_total_bytes:
                path.unlink()
    except OSError as exc:
        log.error = f"{type(exc).__name__}: {exc}"
    return log


def session_log(directory):
    """GUI actions and run outcomes across sessions, rotated at 1 MB (5 backups)."""
    return JsonlLog(Path(directory) / SESSION, max_bytes=1_000_000, backups=5)


def read(path):
    """All events of a log file (for analysis and tests)."""
    with Path(path).open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]
