"""Install the targeted Explorer host fixes. No instrument is imported or opened.

Run with Explorer's Python environment, with Explorer closed. --check is read-only.
Existing sources are backed up; incompatible source layouts are refused before writing.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.util
import os
from pathlib import Path
import stat
import tempfile


PLOT_HELPER = ("def _format_linear_plot_value(value: float) -> str:\n"
               "    \"\"\"Keep ordinary cursor formatting without rounding small nonzero signals to zero.\"\"\"\n"
               "    return f'{value:.2e}' if 0 < abs(value) < .01 else f'{value:.2f}'\n\n\n")


DOCK_TAB_ORDER_HELPER = (
    "DOCK_TAB_ORDER = 'dockTabOrder'\n\n\n"
    "def _dock_tab_bars(mainWindow):\n"
    "    \"\"\"Tab bars of tabbed docks (ESIBD Explorer Plugins host fix).\"\"\"\n"
    "    from PyQt6.QtCore import Qt  # noqa: PLC0415\n"
    "    from PyQt6.QtWidgets import QTabBar  # noqa: PLC0415\n"
    "    return [bar for bar in mainWindow.findChildren(QTabBar, options=Qt.FindChildOption.FindDirectChildrenOnly)\n"
    "            if not bar.isHidden() and bar.count() > 1]\n\n\n"
    "def _save_dock_tab_order(mainWindow):\n"
    "    \"\"\"Remember the order of tabbed docks as arranged by the user; never blocks closing.\"\"\"\n"
    "    import json  # noqa: PLC0415\n"
    "    try:\n"
    "        groups = [[bar.tabText(i) for i in range(bar.count())] for bar in _dock_tab_bars(mainWindow)]\n"
    "        if groups:\n"
    "            qSet.setValue(DOCK_TAB_ORDER, json.dumps(groups))\n"
    "    except Exception:  # noqa: BLE001\n"
    "        pass\n\n\n"
    "def _restore_dock_tab_order(mainWindow):\n"
    "    \"\"\"Reapply the remembered order with adjacent moves, like a mouse drag; new docks go last.\"\"\"\n"
    "    import json  # noqa: PLC0415\n"
    "    try:\n"
    "        groups = json.loads(qSet.value(DOCK_TAB_ORDER, '[]'))\n"
    "        for bar in _dock_tab_bars(mainWindow):\n"
    "            titles = [bar.tabText(i) for i in range(bar.count())]\n"
    "            saved = max(groups, key=lambda group: len(set(group) & set(titles)), default=[])\n"
    "            order = [title for title in saved if title in titles]\n"
    "            order += [title for title in titles if title not in order]\n"
    "            for target, title in enumerate(order):\n"
    "                index = [bar.tabText(i) for i in range(bar.count())].index(title)\n"
    "                while index > target:  # one position at a time: a long move misorders Qt docks\n"
    "                    bar.moveTab(index, index - 1)\n"
    "                    index -= 1\n"
    "    except Exception:  # noqa: BLE001 - the default order remains usable\n"
    "        pass\n\n")


def replace_once(text, old, new):
    if text.count(old) == 1:
        return text.replace(old, new, 1)
    if not text.count(old) and text.count(new) == 1:
        return text
    raise ValueError(f"Unsupported Explorer source around {old!r}; no files were written")


def insert_once(text, anchor, addition, *, before=False):
    """Insert next to a unique anchor; already installed when the combined text is present."""
    combined = addition + anchor if before else anchor + addition
    if text.count(combined) == 1:
        return text
    return replace_once(text, anchor, combined)


def patch_core(text):
    text = replace_once(text, "{item[self.NAME]} using default value",
                        '{item.get(self.NAME, "<unnamed>")} using default value')
    old, new = "y = {pos.y():.2f}", "y = {_format_linear_plot_value(pos.y())}"
    if old in text:
        if "def _format_linear_plot_value(" in text:
            raise ValueError("Ambiguous cursor implementation; no files were written")
        text = replace_once(text, "class PlotItem(pg.PlotItem):", PLOT_HELPER + "class PlotItem(pg.PlotItem):")
    text = replace_once(text, old, new)
    text = replace_once(text, "self.confParser.read(self.pluginFile)",
                        "self.confParser.read(self.pluginFile, encoding=UTF8)")
    text = replace_once(text, "\n            confParser.read(self.pluginFile)",
                        "\n            confParser.read(self.pluginFile, encoding=UTF8)")
    # The order of tabbed plugin docks survives a restart (Explorer saves only the geometry).
    text = insert_once(text, "\nclass PluginManager:", "\n" + DOCK_TAB_ORDER_HELPER, before=True)
    text = insert_once(text, "        qSet.setValue(GEOMETRY, self.saveGeometry())\n",
                       "        _save_dock_tab_order(self)\n", before=True)
    text = insert_once(text, "\n        self.afterFinalizeInit()\n",
                       "        _restore_dock_tab_order(self.mainWindow)\n")
    ast.parse(text)
    return text


def patch_plugins(text):
    for old, new in (("confParser.read(file)", "confParser.read(file, encoding=self.UTF8)"),
                     ("confParser.read(self.activeFileFullPath)",
                      "confParser.read(self.activeFileFullPath, encoding=self.UTF8)")):
        if old in text:
            text = text.replace(old, new)
        elif new not in text:
            raise ValueError(f"Unsupported Explorer INI readers around {old!r}; no files were written")
    ast.parse(text)
    return text


def atomic_write(path, data, mode):
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def install(package, *, check=False, backup_root=None):
    package = Path(package).resolve()
    paths = (package / "core.py", package / "plugins.py")
    original = {path: path.read_bytes() for path in paths}
    patched = {paths[0]: patch_core(original[paths[0]].decode("utf-8")).encode("utf-8"),
               paths[1]: patch_plugins(original[paths[1]].decode("utf-8")).encode("utf-8")}
    changed = [path for path in paths if patched[path] != original[path]]
    if check or not changed:
        return {"package": str(package), "changed_files": len(changed), "check_only": check}
    identity = hashlib.sha256(str(package).encode() + b"".join(original.values())).hexdigest()[:20]
    backups = Path(backup_root or Path.home() / ".esibd/explorer_updates") / identity
    backups.mkdir(parents=True, exist_ok=True)
    for path in changed:
        backup = backups / path.name
        if backup.exists() and backup.read_bytes() != original[path]:
            raise ValueError(f"Backup differs: {backup}; installation refused")
        if not backup.exists():
            with backup.open("xb") as stream:
                stream.write(original[path])
                stream.flush()
                os.fsync(stream.fileno())
    modes = {path: stat.S_IMODE(path.stat().st_mode) for path in changed}
    written = []
    try:
        for path in changed:
            if path.read_bytes() != original[path]:
                raise ValueError(f"Source changed during installation: {path}")
            atomic_write(path, patched[path], modes[path])
            written.append(path)
    except BaseException:
        for path in reversed(written):
            atomic_write(path, original[path], modes[path])
        raise
    assert all(path.read_bytes() == patched[path] for path in paths)
    return {"package": str(package), "changed_files": len(changed), "backup": str(backups), "check_only": False}


def retire_legacy_probe(root, *, check=False, backup_root=None):
    """Archive an old unguarded notebook without touching logs or current notebooks."""
    path = Path(root) / "esi/esi_hv_activation_probe.ipynb"
    if not path.is_file():
        return None
    original = path.read_bytes()
    if check:
        return str(path)
    digest = hashlib.sha256(original).hexdigest()
    directory = Path(backup_root or Path.home() / ".esibd/explorer_updates/retired_probes")
    directory.mkdir(parents=True, exist_ok=True)
    backup = directory / f"esi_hv_activation_probe.{digest}.ipynb.bak"
    if not backup.exists():
        with backup.open("xb") as stream:
            stream.write(original)
            stream.flush()
            os.fsync(stream.fileno())
    if backup.read_bytes() != original or path.read_bytes() != original:
        raise ValueError("Legacy notebook changed during archival; original was not removed")
    path.unlink()
    return str(backup)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Validate compatibility without changing files")
    args = parser.parse_args()
    spec = importlib.util.find_spec("esibd")
    if spec is None or spec.origin is None:
        parser.error("Use the Python environment used by ESIBD Explorer; esibd is not installed here")
    try:
        result = install(Path(spec.origin).parent, check=args.check)
        result["retired_probe"] = retire_legacy_probe(Path(__file__).resolve().parent, check=args.check)
    except (OSError, ValueError, SyntaxError) as error:
        parser.exit(1, f"Explorer update failed: {error}\n")
    print(result)
    if not args.check:
        print("Host fixes installed. Restart Explorer. No instrument was opened; logs and markers were not changed.")


if __name__ == "__main__":
    main()
