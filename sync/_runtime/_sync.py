"""One-way copy of Explorer's configuration and data folders to a network share.

User request (2026-11-20): copy the ESIBD Explorer folder of the lab PC to the local network (\\\\hive\\esi),
one way, ``conf`` and ``data`` only, every hour and after each new scan file, with an account of the user's
choice. Nothing is ever deleted, on either side, and the local folders are only read.

The copy is made by robocopy, part of Windows, in its own process: Explorer never waits for the network, an
interrupted copy resumes (/Z), a newer file on the share is kept (/XO) and nothing is mirrored (no /MIR, no
/PURGE). A file written in the last few minutes is left for the next copy: Explorer may still be writing it (its
device histories are saved every hour). The logs are the exception (2026-11-20): written all the time, they would
never be copied while Explorer runs, yet they explain a stopped scan. Text that only grows, they are copied at
every copy by this module itself, which can read a file another program holds open (robocopy refuses it).
SPDX-License-Identifier: MIT
"""
from __future__ import annotations

from dataclasses import dataclass
import os
import shutil
import subprocess
import sys
import time

FOLDERS = ("conf", "data")  # Folder names under <destination>\<computer>; sources: Explorer's config and data paths.
SETTLE_S = 300.0  # Files modified more recently are left for the next copy.
INTERVAL_S = 3600.0  # A copy every hour...
SCAN_SUFFIX = ".h5"  # ...and one after each new scan file (data, outside its logs), once it has settled.
EXCLUDED_DIRS = ("__pycache__",)
EXCLUDED_FILES = ("*.tmp", "*.lock")
LOG_SUFFIXES = (".log", ".jsonl")  # Copied at every copy by copy_logs, never by robocopy.
LOG_ERRORS_SHOWN = 3
FAILED = 8  # robocopy exit codes: below 8 every file was copied or skipped as intended; 8 and above, failures.
UNREACHABLE = 16  # robocopy: a serious error, no file copied (destination unreachable or refused).
# A stale password retried without end can lock the account (side note 2026-11-20): each copy opens the
# destination once, before robocopy and its retries, and the automatic copies stop after this many failures in a
# row (user: 3 attempts and a warning).
MAX_ATTEMPTS = 3
TAIL_LINES = 6  # Lines of robocopy output kept in a failure message.


@dataclass(frozen=True)
class Result:
    """One copy: when it started, files copied, files left for later, and the problems (empty: success)."""

    started: float
    copied: int
    deferred: int
    problems: tuple[str, ...]
    unreachable: bool = False  # The destination did not open (account, password, network): nothing was tried.

    @property
    def ok(self) -> bool:
        return not self.problems

    def summary(self) -> str:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(self.started))
        text = f"{when}: {self.copied} file{'s' * (self.copied != 1)} copied"
        if self.deferred:
            text += f", {self.deferred} being written left for the next copy"
        return text + ("." if self.ok else ". " + " ".join(self.problems))


def computer_name() -> str:
    return os.environ.get("COMPUTERNAME") or (os.uname().nodename if hasattr(os, "uname") else "") or "PC"


def target(destination: str, computer: str, folder: str) -> str:
    return os.path.join(destination.rstrip("\\/"), computer, folder)


def recent_files(root: str, now: float, settle_s: float = SETTLE_S) -> list[str]:
    """Files under ``root`` modified less than ``settle_s`` ago."""
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDED_DIRS]
        for name in filenames:
            path = os.path.join(dirpath, name)
            try:
                modified = os.stat(path).st_mtime
            except OSError:
                continue
            if now - modified < settle_s and not name.lower().endswith(LOG_SUFFIXES):
                found.append(path)
    return found


def newest_scan(data: str) -> float | None:
    """Modification time of the newest scan file (``.h5`` in the data folder, outside its logs)."""
    newest = None
    for dirpath, dirnames, filenames in os.walk(data):
        if os.path.normcase(os.path.abspath(dirpath)) == os.path.normcase(os.path.abspath(data)):
            dirnames[:] = [d for d in dirnames if d.lower() != "logs"]
        dirnames[:] = [d for d in dirnames if d not in EXCLUDED_DIRS]
        for name in filenames:
            if name.lower().endswith(SCAN_SUFFIX):
                try:
                    modified = os.stat(os.path.join(dirpath, name)).st_mtime
                except OSError:
                    continue
                newest = modified if newest is None else max(newest, modified)
    return newest


def due(now: float, last: float | None, scan: float | None, *, interval_s: float = INTERVAL_S,
        settle_s: float = SETTLE_S) -> bool:
    """A copy is due every ``interval_s``, and once a scan file the last copy did not take has settled.

    The last copy took the files older than ``settle_s`` when it started; a newer scan file waits until it is as old.
    """
    if last is None or now - last >= interval_s:
        return True
    return scan is not None and scan > last - settle_s and now - scan >= settle_s


def command(source: str, destination: str, deferred: list[str]) -> list[str]:
    """robocopy: subfolders, newer files only, nothing deleted, restartable, a few retries, one line per file copied."""
    return ["robocopy", source, destination, "/E", "/XO", "/FFT", "/Z", "/R:2", "/W:10", "/NP", "/NDL", "/NJH", "/NJS",
            "/NC", "/NS", "/FP", "/XD", *EXCLUDED_DIRS, "/XF", *EXCLUDED_FILES,
            *(f"*{suffix}" for suffix in LOG_SUFFIXES), *deferred]


def copy_logs(source: str, destination: str) -> tuple[int, list[str]]:
    """Copy the logs (.log, .jsonl) that changed, even while they are being written.

    A log shorter than its copy started again (rotated or restarted): the copy is kept under a dated name first,
    so nothing is lost on the share. Returns (files copied, problems).
    """
    copied, problems = 0, []
    for dirpath, dirnames, filenames in os.walk(source):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDED_DIRS]
        for name in filenames:
            if not name.lower().endswith(LOG_SUFFIXES):
                continue
            path = os.path.join(dirpath, name)
            target_path = os.path.join(destination, os.path.relpath(path, source))
            try:
                local = os.stat(path)
                remote = os.stat(target_path) if os.path.exists(target_path) else None
                if remote is not None and remote.st_size == local.st_size and remote.st_mtime >= local.st_mtime - 2:
                    continue  # Unchanged (2 s: the time resolution of some network folders).
                if remote is not None and local.st_size < remote.st_size:
                    stem, suffix = os.path.splitext(target_path)
                    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(remote.st_mtime))
                    os.replace(target_path, f"{stem}.until-{stamp}{suffix}")
                os.makedirs(os.path.dirname(target_path), exist_ok=True)
                partial = target_path + ".tmp"
                with open(path, "rb") as reader, open(partial, "wb") as writer:
                    shutil.copyfileobj(reader, writer, 1 << 20)
                os.utime(partial, (local.st_atime, local.st_mtime))
                os.replace(partial, target_path)
                copied += 1
            except OSError as exc:
                problems.append(f"{os.path.relpath(path, source)}: {exc.strerror or exc}")
    return copied, problems


def copied_files(output: str, source: str) -> int:
    """Files robocopy listed as copied: with /FP /NC /NS /NDL, each is one line, its full source path."""
    prefix = os.path.normcase(os.path.abspath(source))
    return sum(1 for line in output.splitlines() if line.strip() and os.path.normcase(line.strip()).startswith(prefix))


def run_robocopy(arguments: list[str], cancel) -> tuple[int, str]:
    """Run robocopy in its own process, without a console window; stop it if ``cancel`` is set."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    process = subprocess.Popen(arguments, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, creationflags=flags)
    output = b""
    while True:
        try:
            output += process.communicate(timeout=0.5)[0] or b""
            break
        except subprocess.TimeoutExpired:
            if cancel is not None and cancel.is_set():
                process.terminate()
                output += process.communicate()[0] or b""
                break
    encoding = "oem" if sys.platform == "win32" else "utf-8"
    return process.returncode, output.decode(encoding, errors="replace")


def reach(destination: str) -> str | None:
    """Open the destination once (Windows sends the account once); None when it opens, else why not."""
    try:
        os.stat(destination)
    except OSError as exc:
        code = getattr(exc, "winerror", None)
        return f"{exc.strerror or exc}" + (f" (Windows error {code})" if code else "")
    return None if os.path.isdir(destination) else "not a folder"


def after_copy(failures: int, result: Result, destination: str) -> tuple[int, bool, str | None]:
    """(failures in a row, automatic copies stopped, warning) after a copy.

    Only a destination that did not open counts: the account may be wrong, and retrying it could lock it.
    """
    if not result.unreachable:
        return 0, False, None if result.ok else f"Copy to {destination} incomplete: {' '.join(result.problems)}"
    failures += 1
    reason = " ".join(result.problems)
    if failures >= MAX_ATTEMPTS:
        return failures, True, (f"{reason} {failures} failed attempts in a row: automatic copies stopped, so that a "
                                "wrong password cannot lock the account. Check it with Connect…, then Copy now.")
    return failures, False, f"{reason} Attempt {failures} of {MAX_ATTEMPTS}."


def copy(sources: dict[str, str], destination: str, computer: str, *, runner=run_robocopy, cancel=None,
         now: float | None = None, settle_s: float = SETTLE_S) -> Result:
    """Copy each source folder to <destination>\\<computer>\\<folder>."""
    started = time.time() if now is None else now
    reason = reach(destination)
    if reason is not None:
        return Result(started, 0, 0, (f"Cannot open {destination}: {reason}.",), unreachable=True)
    copied, deferred, problems = 0, 0, []
    for folder, source in sources.items():
        if not source or not os.path.isdir(source):
            problems.append(f"{folder}: {source or 'no folder'} not found.")
            continue
        recent = recent_files(source, started, settle_s)
        deferred += len(recent)
        try:
            code, output = runner(command(source, target(destination, computer, folder), recent), cancel)
        except OSError as exc:
            problems.append(f"{folder}: robocopy could not start ({exc}).")
            continue
        copied += copied_files(output, source)
        if cancel is not None and cancel.is_set():
            problems.append("Stopped.")
            break
        if code >= FAILED:
            prefix = os.path.normcase(os.path.abspath(source))
            notes = [line.strip() for line in output.splitlines()
                     if line.strip() and not os.path.normcase(line.strip()).startswith(prefix)]
            tail = " | ".join(notes[-TAIL_LINES:])
            problems.append(f"{folder}: robocopy {code}{': ' + tail if tail else ''}")
        if code >= UNREACHABLE:
            continue  # Nothing could be copied (share unreachable, access denied): the logs neither.
        logs, failed = copy_logs(source, target(destination, computer, folder))
        copied += logs
        if failed:
            more = f" (and {len(failed) - LOG_ERRORS_SHOWN} more)" if len(failed) > LOG_ERRORS_SHOWN else ""
            problems.append(f"{folder} logs: {'; '.join(failed[:LOG_ERRORS_SHOWN])}{more}.")
    return Result(started, copied, deferred, tuple(problems))
