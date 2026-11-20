"""Sync: one-way copy of Explorer's conf and data folders to a network share (logic, without Windows).

User request (2026-11-20): copy the ESIBD Explorer folder of the lab PC to \\\\hive\\esi, one way, conf and data
only, every hour and after each new scan file, with an account of the user's choice. robocopy exists on Windows
only: these tests check the command it is given and run the copy with a stand-in that follows those options.
"""
from __future__ import annotations

import ctypes
import importlib.util
import os
from pathlib import Path
import sys
import time

import pytest
from support.sync import fake_robocopy, write

ROOT = Path(__file__).resolve().parents[1]


def _module(name):
    spec = importlib.util.spec_from_file_location(f"_test_sync_{name}", ROOT / "sync" / "_runtime" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # A dataclass looks its module up while it is defined.
    spec.loader.exec_module(module)
    return module


sync = _module("_sync")
windows = _module("_windows")


def test_the_command_copies_new_files_and_never_deletes():
    arguments = sync.command(r"C:\ESIBD Explorer\data", r"\\hive\esi\LABPC\data", [r"C:\ESIBD Explorer\data\a.h5"])
    assert arguments[:3] == ["robocopy", r"C:\ESIBD Explorer\data", r"\\hive\esi\LABPC\data"]
    assert {"/E", "/XO", "/FFT", "/Z", "/R:2", "/W:10", "/FP", "/NC", "/NS", "/NDL"} <= set(arguments)
    assert not {"/MIR", "/PURGE", "/MOV", "/MOVE", "/CREATE"} & {a.upper() for a in arguments}
    # The logs are left to copy_logs, the files being written to the next copy.
    assert arguments[arguments.index("/XF") + 1:] == ["*.tmp", "*.lock", "*.log", "*.jsonl", r"C:\ESIBD Explorer\data\a.h5"]
    assert sync.target(r"\\hive\esi" + "\\", "LABPC", "conf") == os.path.join(r"\\hive\esi", "LABPC", "conf")


def test_conf_and_data_are_copied_one_way_leaving_files_being_written(tmp_path):
    conf, data, share = tmp_path / "conf", tmp_path / "data", tmp_path / "share"
    share.mkdir()  # The network folder exists; <PC name>\conf and \data are made by the copy.
    write(conf / "MScan.ini")
    write(data / "MScan" / "2026-11-20" / "scan.h5")
    write(data / "MScan" / "__pycache__" / "x.pyc")
    write(data / "MScan" / "note.tmp")
    live = write(conf / "dmmr.h5", age_s=10.0)  # Saved by Explorer a moment ago.
    # Logs written all the time (side note 2026-11-20): copied at every copy, even a second old.
    explorer_log = write(conf / "esibd explorer.log", "started\n", age_s=1.0)
    write(data / "logs" / "dmmr" / "native" / "20261120.jsonl", "{}\n", age_s=1.0)
    result = sync.copy({"conf": str(conf), "data": str(data)}, str(share), "LABPC", runner=fake_robocopy)
    assert result.ok and (result.copied, result.deferred) == (4, 1), result
    copied = sorted(p.relative_to(share).as_posix() for p in share.rglob("*") if p.is_file())
    assert copied == ["LABPC/conf/MScan.ini", "LABPC/conf/esibd explorer.log", "LABPC/data/MScan/2026-11-20/scan.h5",
                      "LABPC/data/logs/dmmr/native/20261120.jsonl"]
    assert "4 files copied, 1 being written left for the next copy" in result.summary()
    # A file deleted on the PC stays on the share; the one being written is copied once it has settled.
    (data / "MScan" / "2026-11-20" / "scan.h5").unlink()
    os.utime(live, (time.time() - 3600, time.time() - 3600))
    with explorer_log.open("a", encoding="utf-8") as log:
        log.write("scan stopped\n")  # Grown: copied again; the unchanged .jsonl is not.
    result = sync.copy({"conf": str(conf), "data": str(data)}, str(share), "LABPC", runner=fake_robocopy)
    assert (result.copied, result.deferred) == (2, 0)
    assert (share / "LABPC" / "conf" / "esibd explorer.log").read_text(encoding="utf-8") == "started\nscan stopped\n"
    assert (share / "LABPC" / "data" / "MScan" / "2026-11-20" / "scan.h5").is_file()
    assert (share / "LABPC" / "conf" / "dmmr.h5").is_file()
    assert sorted(p.name for p in conf.iterdir()) == ["MScan.ini", "dmmr.h5", "esibd explorer.log"]  # Only read.


def test_a_log_that_started_again_keeps_its_former_copy_on_the_share(tmp_path):
    logs, share = tmp_path / "logs", tmp_path / "share"
    log = write(logs / "worker.jsonl", "one\ntwo\nthree\n", age_s=600)
    assert sync.copy_logs(str(logs), str(share)) == (1, [])
    assert sync.copy_logs(str(logs), str(share)) == (0, [])  # Unchanged.
    write(logs / "worker.jsonl", "new\n", age_s=1)  # Rotated: shorter than its copy.
    assert sync.copy_logs(str(logs), str(share)) == (1, [])
    (former,) = [p for p in share.iterdir() if p.name.startswith("worker.until-") and p.suffix == ".jsonl"]
    assert sorted(p.name for p in share.iterdir()) == sorted(["worker.jsonl", former.name])
    assert former.read_text(encoding="utf-8") == "one\ntwo\nthree\n"
    assert (share / "worker.jsonl").read_text(encoding="utf-8") == "new\n" and log.exists()


def test_a_failed_copy_says_why_with_the_end_of_the_robocopy_output(tmp_path):
    conf = tmp_path / "conf"
    write(conf / "a.ini")
    write(conf / "esibd explorer.log")

    def unreachable(arguments, cancel):
        return 16, "2026/11/20 16:00:00 ERROR 53 (0x00000035) Accessing Destination Directory \\\\hive\\esi\\\nThe network path was not found."
    share = tmp_path / "share"
    share.mkdir()  # Opened, then lost by robocopy.
    result = sync.copy({"conf": str(conf), "data": str(tmp_path / "missing")}, str(share), "LABPC", runner=unreachable)
    assert not result.ok and not result.unreachable
    assert result.problems[0].startswith("conf: robocopy 16: 2026/11/20 16:00:00 ERROR 53")
    assert "The network path was not found." in result.problems[0]
    assert result.problems[1].startswith("data: ") and result.problems[1].endswith("not found.")
    assert not any("logs" in problem for problem in result.problems)  # Unreachable: no log tried one by one.


def test_a_destination_that_does_not_open_is_tried_once_and_three_times_at_most(tmp_path):
    """Side note 2026-11-20: after a password change, retries with the former one can lock the account. User: three
    attempts and a warning. Each copy opens the destination once, without robocopy and its retries."""
    conf = tmp_path / "conf"
    write(conf / "a.ini")

    def never(arguments, cancel):
        raise AssertionError("robocopy started on a destination that does not open")
    missing = str(tmp_path / "hive" / "esi")
    result = sync.copy({"conf": str(conf)}, missing, "LABPC", runner=never)
    assert result.unreachable and not result.ok and result.problems[0].startswith(f"Cannot open {missing}: ")
    failures, warnings = 0, []
    for _ in range(3):
        failures, stopped, warning = sync.after_copy(failures, result, missing)
        warnings.append(warning)
    assert (failures, stopped) == (3, True)
    assert warnings[0].endswith("Attempt 1 of 3.") and warnings[1].endswith("Attempt 2 of 3.")
    assert "automatic copies stopped" in warnings[2] and "Connect…" in warnings[2]
    opened = sync.Result(result.started, 0, 0, ("conf: robocopy 8: ERROR 32",))  # Opened: the account is fine.
    assert sync.after_copy(3, opened, missing) == (0, False, f"Copy to {missing} incomplete: conf: robocopy 8: ERROR 32")
    assert sync.after_copy(2, sync.Result(result.started, 3, 0, ()), missing) == (0, False, None)


def test_a_copy_is_due_every_hour_and_once_a_new_scan_file_has_settled():
    hour, settle = sync.INTERVAL_S, sync.SETTLE_S
    assert sync.due(1000.0, None, None)  # Never copied.
    assert not sync.due(10_000.0, 10_000.0 - 60, None)
    assert sync.due(10_000.0, 10_000.0 - hour, None)
    last = 10_000.0
    assert not sync.due(last + 100, last, last + 50)  # A scan file 50 s old: not settled yet.
    assert sync.due(last + 50 + settle, last, last + 50)
    # Too recent for the last copy, which left it: taken once settled.
    assert not sync.due(last + 10, last, last - 60) and sync.due(last - 60 + settle, last, last - 60)
    assert not sync.due(last + 10, last, last - settle - 1)  # Already taken by the last copy.


def test_the_newest_scan_file_is_looked_for_outside_the_logs(tmp_path):
    data = tmp_path / "data"
    write(data / "MScan" / "2026-11-20" / "a.h5", age_s=500)
    write(data / "logs" / "transmission" / "run.h5", age_s=1)
    write(data / "Transmission" / "notes.txt", age_s=1)
    assert sync.newest_scan(str(data)) == pytest.approx(time.time() - 500, abs=5)
    assert sync.newest_scan(str(tmp_path / "empty")) is None


def test_robocopy_lists_one_full_source_path_per_copied_file(tmp_path):
    source = tmp_path / "conf"
    output = f"\t\t\t\t\t{source / 'a.ini'}\n\t\t\t\t\t{source / 'b' / 'c.h5'}\n2026/11/20 ERROR 5 (0x00000005) x\n"
    assert sync.copied_files(output, str(source)) == 2


def test_the_network_account_is_kept_by_windows_never_by_the_plugin():
    assert windows.server(r"\\hive\esi") == "hive" and windows.server("//hive/esi/sub") == "hive"
    for share in ("hive\\esi", r"C:\data", r"\\hive"):
        with pytest.raises(windows.AccountError, match="not a network folder"):
            windows.server(share)
    assert windows.message(1326) == "unknown account or wrong password (1326)"
    assert "another account" in windows.message(1219) and windows.message(4242) == "Windows error (4242)"
    if ctypes.sizeof(ctypes.c_void_p) == 8:  # The layouts of CREDENTIALW and NETRESOURCEW on 64-bit Windows.
        assert (ctypes.sizeof(windows._CREDENTIAL), ctypes.sizeof(windows._NETRESOURCE)) == (80, 48)
    if sys.platform != "win32":
        with pytest.raises(windows.AccountError, match="Windows only"):
            windows.connect(r"\\hive\esi", "user", "secret")
