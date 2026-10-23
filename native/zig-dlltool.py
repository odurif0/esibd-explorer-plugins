#!/usr/bin/env python3
"""Import library generation for Rust windows-gnu cross builds."""
import os
import shutil
import sys


def main():
    zig = os.environ.get("ESIBD_ZIG") or shutil.which("zig")
    if not zig:
        raise SystemExit("Install Zig or set ESIBD_ZIG to its executable")
    os.execv(zig, [zig, "dlltool", *sys.argv[1:]])


# Only as Cargo's dlltool: importing this file (e.g. from an Explorer plugin path) must not exec Zig.
if __name__ == "__main__":
    main()
