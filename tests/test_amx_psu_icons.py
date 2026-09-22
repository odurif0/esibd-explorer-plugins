"""AMX/PSU icons: shared family glyphs, eight colors and matching SVG/PNG."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
FAMILIES = {
    "amx": ("amx_a/amx", "amx_b/amx", "amx_hd/amx_hd"),
    "psu": tuple(f"psu_{suffix}/psu" for suffix in "abcde"),
}


ICON_COLORS = {
    "psu_a/psu": "#ffd078",
    "psu_b/psu": "#6fdfa0",
    "psu_c/psu": "#c799ff",
    "psu_d/psu": "#ff8899",
    "psu_e/psu": "#86aaff",
    "amx_a/amx": "#65dbf5",
    "amx_b/amx": "#ffa16b",
    "amx_hd/amx_hd": "#ed8bdb",
}


def normalized_svg(stem):
    source = (ROOT / stem).with_suffix(".svg").read_text()
    foreground = f'stroke="{ICON_COLORS[stem]}"'
    assert source.count(foreground) == 1, f"{stem}: expected its approved foreground color"
    # Only the glyph color may change; background, border and geometry stay fixed.
    return source.replace(foreground, 'stroke="DEVICE_COLOR"')


@pytest.mark.parametrize("family", FAMILIES)
def test_icon_variants_keep_their_family_shape(family):
    stems = FAMILIES[family]
    canonical = normalized_svg(stems[0])
    for stem in stems[1:]:
        assert normalized_svg(stem) == canonical, f"{stem} has a different family glyph"


@pytest.mark.parametrize("extension", (".png", ".svg"))
def test_all_variants_are_distinct(extension):
    assert len(set(ICON_COLORS.values())) == len(ICON_COLORS)
    icons = [(ROOT / stem).with_suffix(extension).read_bytes() for stem in ICON_COLORS]
    assert len(set(icons)) == len(icons), "Every variant must have its own color"


def test_amx_and_psu_have_distinct_symbols():
    assert normalized_svg(FAMILIES["amx"][0]) != normalized_svg(FAMILIES["psu"][0])


@pytest.mark.parametrize("stem", ICON_COLORS, ids=lambda stem: stem.split("/")[0])
def test_png_matches_svg_and_loads_at_toolbar_sizes(stem, tmp_path):
    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), str(ROOT / stem), str(tmp_path)],
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
        capture_output=True, text=True, timeout=20,
    )
    if result.returncode == 77:
        pytest.skip("PyQt6/SVG unavailable")
    assert result.returncode == 0, result.stdout + result.stderr


def probe(stem, output):
    try:
        from PyQt6.QtCore import QSize, Qt
        from PyQt6.QtGui import QIcon, QImage, QPainter
        from PyQt6.QtSvg import QSvgRenderer
        from PyQt6.QtWidgets import QApplication
    except ImportError:
        return 77

    app = QApplication([])
    renderer = QSvgRenderer(str(stem.with_suffix(".svg")))
    assert renderer.isValid()
    expected = QImage(128, 128, QImage.Format.Format_ARGB32_Premultiplied)
    expected.fill(Qt.GlobalColor.transparent)
    painter = QPainter(expected)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    renderer.render(painter)
    painter.end()
    png = QImage(str(stem.with_suffix(".png")))
    assert not png.isNull()
    assert png.size() == QSize(128, 128) and png.hasAlphaChannel()
    assert png.convertToFormat(QImage.Format.Format_RGBA8888) == expected.convertToFormat(
        QImage.Format.Format_RGBA8888), "PNG does not match its vector source"

    icon = QIcon(str(stem.with_suffix(".png")))
    for size in (16, 24, 32, 64):
        pixmap = icon.pixmap(QSize(size, size))
        assert not pixmap.isNull() and pixmap.size() == QSize(size, size)
        assert pixmap.save(str(output / f"{stem.name}-{size}.png"))
    app.quit()
    return 0


if __name__ == "__main__":
    raise SystemExit(probe(Path(sys.argv[1]), Path(sys.argv[2])))
