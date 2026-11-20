"""Stand-ins for the Sync plugin tests: robocopy exists on Windows only."""
from __future__ import annotations

import fnmatch
import os
from pathlib import Path
import re
import shutil
import time


def fake_robocopy(arguments, cancel):
    """Follows the options the plugin gives robocopy: /XD names, /XF patterns or paths, /XO, nothing deleted."""
    assert arguments[0] == "robocopy"
    source, target = Path(arguments[1]), Path(arguments[2])
    options = arguments[3:]
    excluded_dirs, excluded_files, current = set(), [], None
    for option in options:
        if re.fullmatch(r"/[A-Z]+(:\d+)?", option):  # An option; a Linux path also starts with "/".
            current = option
        elif current == "/XD":
            excluded_dirs.add(option)
        elif current == "/XF":
            excluded_files.append(option)
    lines = []
    for dirpath, dirnames, filenames in os.walk(source):
        dirnames[:] = [d for d in dirnames if d not in excluded_dirs]
        for name in filenames:
            path = Path(dirpath) / name
            if any(fnmatch.fnmatch(name, p) or str(path) == p for p in excluded_files):
                continue
            destination = target / path.relative_to(source)
            if destination.exists() and destination.stat().st_mtime >= path.stat().st_mtime:  # /XO
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)
            lines.append(f"\t\t\t\t\t{path}")
    return (1 if lines else 0), "\n".join(lines)


def write(path, text="x", age_s=3600.0):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    when = time.time() - age_s
    os.utime(path, (when, when))
    return path
