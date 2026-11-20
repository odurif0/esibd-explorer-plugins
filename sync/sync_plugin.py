"""Sync: one-way copy of Explorer's conf and data folders to a network share.

User request (2026-11-20): the lab PC is on the local network; copy its ESIBD Explorer folder to \\\\hive\\esi, one
way, ``conf`` and ``data`` only, every hour and after each new scan file, with an account of the user's choice.
The local folders are only read and nothing is ever deleted on the share. The copy is made by robocopy, in its own
process, by a background thread of this plugin: Explorer never waits for the network. The password is given once
to the Windows Credential Manager, never written in Explorer's files. Each copy is logged in
<Explorer data path>/logs/sync/sync.log. Logic in ``_runtime/_sync.py``, Windows account in ``_runtime/_windows.py``.
SPDX-License-Identifier: MIT
"""
from __future__ import annotations

import importlib
import importlib.util
import json
import os
from pathlib import Path
import sys
from collections import deque
from threading import Event, Lock, Thread
import time

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
                             QPushButton, QVBoxLayout)

from esibd.core import PLUGINTYPE, PRINT
from esibd.plugins import Plugin

_RUNTIME_FILES = ("__init__.py", "_sync.py", "_windows.py")


def _load_runtime():
    # Explorer imports every root *.py file without sys.modules registration: the runtime is loaded privately.
    root = Path(__file__).resolve().parent / "_runtime"
    missing = [name for name in _RUNTIME_FILES if not (root / name).is_file()]
    if missing:
        raise ModuleNotFoundError(f"Missing bundled Sync runtime file(s) in {root}: {', '.join(missing)}")
    name = f"_esibd_bundled_sync_{root.parent.name}"
    try:
        if name not in sys.modules:
            spec = importlib.util.spec_from_file_location(name, root / "__init__.py", submodule_search_locations=[str(root)])
            if spec is None or spec.loader is None:
                raise ModuleNotFoundError(f"Cannot load the bundled Sync runtime: {root}")
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            spec.loader.exec_module(module)
        return tuple(importlib.import_module(f"{name}.{part}") for part in ("_sync", "_windows"))
    except BaseException:
        for key in [key for key in sys.modules if key == name or key.startswith(name + ".")]:
            sys.modules.pop(key, None)
        raise


_sync, _windows = _load_runtime()


def providePlugins():
    return [Sync]


class _AccountDialog(QDialog):
    """Account of the share: tried at once, then kept by Windows. The password stays in this dialog only."""

    def __init__(self, parent, share, account):
        super().__init__(parent)
        self.setWindowTitle("Network account")
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"Account for {share}. The password is given to Windows (Credential Manager), "
                                "never written in Explorer's files."))
        form = QFormLayout()
        self.account = QLineEdit(account)
        self.account.setPlaceholderText("DOMAIN\\user or user")
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.remember = QCheckBox("Keep it on this PC for later copies")
        self.remember.setChecked(True)
        form.addRow("Account", self.account)
        form.addRow("Password", self.password)
        layout.addLayout(form)
        layout.addWidget(self.remember)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class Sync(Plugin):
    """Copies Explorer's conf and data folders to a network share, every hour and after each new scan file.

    One way: the local folders are only read, and nothing is deleted on the share. A file written in the last
    minutes is left for the next copy. The account of the share is kept by Windows, never in Explorer's files.
    """

    name = "Sync"
    pluginType = PLUGINTYPE.CONTROL
    version = "0.1.0"
    supportedVersion = "1.0"
    iconFile = "sync.png"
    STATE_FILE = "Sync.json"
    # failures/stopped: the destination did not open that many times in a row; after MAX_ATTEMPTS the automatic
    # copies stop until Connect… or Copy now succeeds. Kept in the file, so restarting Explorer does not retry.
    DEFAULTS = dict(destination="\\\\hive\\esi", account="", automatic=True, last=None, summary="No copy yet.",
                    failures=0, stopped=False)
    CHECK_S = 60.0  # The background thread looks every minute whether a copy is due.
    RETRY_S = 600.0  # After a failed copy, the next automatic attempt waits this long.

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.runner = _sync.run_robocopy  # Tests give a stand-in: robocopy exists on Windows only.
        self.settle_s = _sync.SETTLE_S
        self._state = dict(self.DEFAULTS)
        self._state_lock = Lock()
        self._wake, self._stop, self._cancel = Event(), Event(), Event()
        self._forced = False
        self._copying = False
        self._failed_at = None
        self._thread = None
        self._timer = None
        self._warnings = deque()  # From the copy thread, printed by the GUI.

    # ---------------------------------------------------------------- state
    def _state_path(self):
        return Path(self.pluginManager.Settings.configPath) / self.STATE_FILE

    def _load_state(self):
        try:
            saved = json.loads(self._state_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            saved = {}
        with self._state_lock:
            self._state = dict(self.DEFAULTS, **{k: v for k, v in saved.items() if k in self.DEFAULTS})

    def _save_state(self, **changes):
        # Called from the GUI and from the copy thread: one writer at a time.
        with self._state_lock:
            self._state.update(changes)
            path = self._state_path()
            try:
                temporary = path.with_name(path.name + ".tmp")
                temporary.write_text(json.dumps(self._state, indent=1, ensure_ascii=False), encoding="utf-8")
                os.replace(temporary, path)
            except OSError as exc:
                problem = f"Cannot save {path.name}: {exc}"
            else:
                return
        self.print(problem, flag=PRINT.WARNING)

    def state(self):
        with self._state_lock:
            return dict(self._state)

    # ---------------------------------------------------------------- GUI
    def initGUI(self):
        super().initGUI()
        self._load_state()
        state = self.state()
        form = QFormLayout()
        self.destination = QLineEdit(state["destination"])
        self.destination.setToolTip("Network folder; each PC copies into <folder>\\<PC name>\\conf and \\data.")
        self.destination.editingFinished.connect(lambda: self._save_state(destination=self.destination.text().strip()))
        form.addRow("Destination", self.destination)
        account = QHBoxLayout()
        self.account = QLabel(state["account"] or "Windows account")
        self.connect_button = QPushButton("Connect…")
        self.connect_button.setToolTip("Choose the account of the network folder.")
        self.connect_button.clicked.connect(self.connect_account)
        account.addWidget(self.account, 1)
        account.addWidget(self.connect_button)
        form.addRow("Account", account)
        self.automatic = QCheckBox("Every hour and after each new scan file")
        self.automatic.setChecked(bool(state["automatic"]))
        self.automatic.toggled.connect(lambda checked: self._save_state(automatic=bool(checked)))
        form.addRow("Automatic", self.automatic)
        self.status = QLabel(state["summary"])
        self.status.setWordWrap(True)
        form.addRow("Last copy", self.status)
        self.copy_button = QPushButton("Copy now")
        self.copy_button.clicked.connect(self.copy_now)
        form.addRow("", self.copy_button)
        layout = QVBoxLayout()
        layout.addLayout(form)
        layout.addStretch(1)
        self.addContentLayout(layout)

    def finalizeInit(self):
        super().finalizeInit()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._show)
        self._timer.start(1000)
        self._thread = Thread(target=self._loop, name="Sync", daemon=True)
        self._thread.start()

    def _show(self):
        while self._warnings:
            self.print(self._warnings.popleft(), flag=PRINT.WARNING)
        state = self.state()
        text = "Copying…" if self._copying else state["summary"]
        if self.status.text() != text:
            self.status.setText(text)
        style = "color: #c53030;" if state["failures"] and not self._copying else ""
        if self.status.styleSheet() != style:
            self.status.setStyleSheet(style)
        self.copy_button.setEnabled(not self._copying)

    def connect_account(self):
        share = self.destination.text().strip()
        dialog = _AccountDialog(self, share, self.state()["account"])
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        account, password, keep = dialog.account.text().strip(), dialog.password.text(), dialog.remember.isChecked()
        dialog.password.clear()
        try:
            _windows.connect(share, account, password)
            if keep:
                _windows.remember(share, account, password)
        except _windows.AccountError as exc:
            self.print(f"{share}: {exc}", flag=PRINT.WARNING)
            self._save_state(summary=f"Not connected: {exc}.")
            return
        finally:
            password = None  # noqa: F841 - dropped as soon as Windows has it
        self._save_state(account=account, failures=0, stopped=False,
                         summary=f"Connected to {share} as {account or 'the Windows account'}.")
        self.account.setText(account or "Windows account")
        self.copy_now()

    def copy_now(self):
        self._save_state(destination=self.destination.text().strip())
        self._forced = True
        self._wake.set()

    # ---------------------------------------------------------------- background copy
    def _sources(self):
        settings = self.pluginManager.Settings
        return {"conf": str(settings.configPath), "data": str(settings.dataPath)}

    def _loop(self):
        while not self._stop.is_set():
            self._wake.wait(self.CHECK_S)
            self._wake.clear()
            if self._stop.is_set():
                break
            forced, self._forced = self._forced, False
            try:
                self._maybe_copy(forced)
            except Exception as exc:  # noqa: BLE001 - the loop must survive anything; reported
                self._failed_at = time.time()
                self._save_state(summary=f"Copy failed: {exc}")

    def _maybe_copy(self, forced):
        state, now = self.state(), time.time()
        if not forced:
            if (not state["automatic"] or state["stopped"]
                    or (self._failed_at is not None and now - self._failed_at < self.RETRY_S)):
                return
            sources = self._sources()
            scan = _sync.newest_scan(sources["data"]) if os.path.isdir(sources["data"]) else None
            if not _sync.due(now, state["last"], scan, settle_s=self.settle_s):
                return
        self._copy(state["destination"])

    def _copy(self, destination):
        self._copying, self._cancel = True, Event()
        try:
            result = _sync.copy(self._sources(), destination, _sync.computer_name(), runner=self.runner,
                                cancel=self._cancel, settle_s=self.settle_s)
        finally:
            self._copying = False
        failures, stopped, warning = _sync.after_copy(self.state()["failures"], result, destination)
        summary = result.summary()
        if stopped:
            summary += " Automatic copies stopped: check the account with Connect…, then Copy now."
        if result.ok:
            self._failed_at = None
            self._save_state(last=result.started, summary=summary, failures=0, stopped=False)
        else:
            self._failed_at = time.time()
            self._save_state(summary=summary, failures=failures, stopped=stopped)
        if warning:
            self._warnings.append(warning)
        self._log(destination, result)
        return result

    def _log(self, destination, result):
        try:
            folder = Path(self.pluginManager.Settings.dataPath) / "logs" / "sync"
            folder.mkdir(parents=True, exist_ok=True)
            when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(result.started))
            with (folder / "sync.log").open("a", encoding="utf-8") as log:
                log.write(f"{when} {'OK' if result.ok else 'FAILED'} {destination}\\{_sync.computer_name()}: "
                          f"{result.copied} copied, {result.deferred} left for later. {' '.join(result.problems)}\n")
        except OSError:
            pass  # A log that cannot be written never stops the copies.

    def close(self):
        self._stop.set()
        self._cancel.set()
        self._wake.set()
        if self._timer is not None:
            self._timer.stop()
        if self._thread is not None:
            self._thread.join(timeout=3)
        return super().close()
