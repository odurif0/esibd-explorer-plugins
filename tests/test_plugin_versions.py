"""Every plugin declares the version of the release that ships it.

User, 2026-11-20: Explorer's plugins.ini showed 0.1.0, 0.1.1, 0.1.3 or 0.2.0 for the plugins of v0.6.0; each
plugin's ``version`` had never followed the releases. A release changes the ZIP name in README.md and these
versions together.
"""
from __future__ import annotations

from pathlib import Path
import re

import pytest

from conftest import PLUGIN_SPECS

ROOT = Path(__file__).resolve().parents[1]


def release_version() -> str:
    found = re.findall(r"esibd-explorer-plugins-v(\d+\.\d+\.\d+)\.zip", (ROOT / "README.md").read_text(encoding="utf-8"))
    assert len(set(found)) == 1, found
    return found[0]


@pytest.mark.parametrize("spec", PLUGIN_SPECS, ids=lambda spec: spec.folder)
def test_every_plugin_declares_the_release_version(spec):
    text = (ROOT / spec.folder / spec.entrypoint).read_text(encoding="utf-8")
    assert re.findall(r"""^    version = ["']([^"']+)["']$""", text, re.MULTILINE) == [release_version()]
