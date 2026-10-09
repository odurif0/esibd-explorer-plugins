#!/usr/bin/env python3
"""Optional Linux-to-Windows GNU linker using an explicitly installed Zig."""
import os
import shutil
import sys

zig = os.environ.get("ESIBD_ZIG") or shutil.which("zig")
if not zig:
    raise SystemExit("Install Zig or set ESIBD_ZIG to its executable")
# Zig provides its compiler runtime instead of GCC's unwind/runtime libraries.
arguments = [arg for arg in sys.argv[1:] if arg not in {"-lgcc", "-lgcc_eh", "-Wl,-Bdynamic", "-Wl,-Bstatic", "-lmsvcrt", "-l:libpthread.a"}]
os.execv(zig, [zig, "cc", "-target", "x86_64-windows-gnu", *arguments, "-lc", "-lunwind"])
