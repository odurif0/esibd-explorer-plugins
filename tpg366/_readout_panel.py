"""Read-only pressure cards; editing affects Explorer channels, never gauge hardware.

SPDX-License-Identifier: GPL-2.0-or-later
"""
from __future__ import annotations

import math

from PyQt6.QtCore import QRect, QSize
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import QCheckBox, QFrame, QHBoxLayout, QLabel, QLayout, QLineEdit, QVBoxLayout, QWidget


class CardGrid(QLayout):
    """Wrap cards while retaining a one-card minimum dock width."""

    def __init__(self, parent):
        super().__init__(parent)
        self._items = []
        self.setContentsMargins(0, 0, 0, 0)
        self.setSpacing(10)

    def addItem(self, item):
        self._items.append(item)
        self.invalidate()

    def count(self):
        return len(self._items)

    def itemAt(self, index):
        return self._items[index] if 0 <= index < self.count() else None

    def takeAt(self, index):
        if 0 <= index < self.count():
            item = self._items.pop(index)
            self.invalidate()
            return item
        return None

    def minimumSize(self):
        size = QSize(0, 0)
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        return size

    def sizeHint(self):
        columns = min(3, self.count())
        if not columns:
            return QSize(0, 0)
        width = columns * max(item.sizeHint().width() for item in self._items)
        width += (columns - 1) * self.spacing()
        return QSize(width, self.heightForWidth(width))

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._arrange(QRect(0, 0, width, 0), apply=False)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._arrange(rect, apply=True)

    def _arrange(self, rect, *, apply):
        if not self._items:
            return 0
        gap = self.spacing()
        minimum = max(1, self.minimumSize().width())
        columns = min(self.count(), max(1, (rect.width() + gap) // (minimum + gap)))
        width = max(minimum, (rect.width() - (columns - 1) * gap) // columns)
        width = min(width, max(item.maximumSize().width() for item in self._items))
        left = rect.x() + max(0, (rect.width() - columns * width - (columns - 1) * gap) // 2)
        y = rect.y()
        for start in range(0, self.count(), columns):
            row = self._items[start:start + columns]
            heights = [max(item.minimumSize().height(), item.sizeHint().height()) for item in row]
            if apply:
                for column, (item, height) in enumerate(zip(row, heights)):
                    item.setGeometry(QRect(left + column * (width + gap), y, width, height))
            y += max(heights) + gap
        return y - rect.y() - gap


class PressurePanel(QWidget):
    def __init__(self, edit):
        super().__init__()
        self._edit = edit
        self._grid = CardGrid(self)
        self.cards = {}

    def _commit_label(self, channel, editor):
        self._edit(channel, "name", editor.text().strip() or channel.name)
        editor.setText(channel.name)

    def refresh(self, channels, *, live):
        channels = list(channels)
        if tuple(self.cards) != tuple(id(channel) for channel in channels):
            for card in self.cards.values():
                card[2].blockSignals(True)  # Removing a focused editor must not re-enter refresh.
            while self._grid.count():
                widget = self._grid.takeAt(0).widget()
                widget.setParent(None)
                widget.deleteLater()
            self.cards.clear()
            for channel in channels:
                frame = QFrame()
                frame.setObjectName("tpgPressureCard")
                frame.setMinimumWidth(180)
                frame.setMaximumWidth(210)
                layout = QVBoxLayout(frame)
                layout.setContentsMargins(10, 10, 10, 10)
                layout.setSpacing(5)
                title = QLabel(f"Input {int(channel.input)}")
                title.setStyleSheet("color: #cbd5e1; font-weight: 600; font-size: 12px;")
                layout.addWidget(title)
                label = QLineEdit(channel.name)
                label.setStyleSheet("color: #f8fafc; background: transparent; border: none; font-size: 13px;")
                label.setToolTip("Gauge label. Enter, Tab or click elsewhere to save.")
                label.editingFinished.connect(lambda ch=channel, editor=label: self._commit_label(ch, editor))
                layout.addWidget(label)
                value = QLabel("—")
                value.setStyleSheet("color: #f8fafc; font-weight: 600; font-size: 24px;")
                layout.addWidget(value)
                unit = QLabel("mbar")
                unit.setStyleSheet("color: #94a3b8; font-size: 11px;")
                layout.addWidget(unit)
                status = QLabel()
                status.setWordWrap(True)
                status.setStyleSheet("color: #a8b3c4; font-size: 11px;")
                layout.addWidget(status)
                gauge = QLabel()
                gauge.setWordWrap(True)
                gauge.setStyleSheet("color: #94a3b8; font-size: 11px;")
                layout.addWidget(gauge)
                toggles = QHBoxLayout()
                include = QCheckBox("Read")
                include.setToolTip("Include in acquisition; does not switch the physical gauge.")
                include.toggled.connect(lambda checked, ch=channel: self._edit(ch, "enabled", checked))
                display = QCheckBox("Display")
                display.setToolTip("Show the curve; acquisition is unchanged.")
                display.toggled.connect(lambda checked, ch=channel: self._edit(ch, "display", checked))
                for toggle in (include, display):
                    toggle.setStyleSheet("QCheckBox {color: #cbd5e1; font-size: 11px;}")
                toggles.addWidget(include)
                toggles.addWidget(display)
                layout.addLayout(toggles)
                self._grid.addWidget(frame)
                self.cards[id(channel)] = (frame, title, label, value, status, gauge, include, display)
        for channel in channels:
            frame, title, label, value, status, gauge, include, display = self.cards[id(channel)]
            real = bool(getattr(channel, "real", True))
            title.setText(f"Input {int(channel.input)}" if real else "Derived")
            if not label.hasFocus():
                label.setText(channel.name)
            try:
                reading = float(channel.value)
            except (TypeError, ValueError):
                reading = math.nan
            valid = live and channel.enabled and channel.active and (not real or channel.status == "OK") and math.isfinite(reading)
            curve_color = QColor(getattr(channel, "color", "#cbd5e1"))
            title_color = curve_color.name() if valid and curve_color.isValid() else "#94a3b8"
            title.setStyleSheet(f"color: {title_color}; font-weight: 600; font-size: 12px;")
            value.setText(f"{reading:.2e}" if valid else "—")
            current_status = "Virtual" if not real and live else channel.status
            status.setText(current_status if channel.enabled else "Excluded")
            gauge_text = channel.gauge if real else "Virtual channel"
            gauge.setText(gauge_text)
            gauge.setToolTip(gauge_text)
            border, background = ("#3182ce", "#162433") if valid else ("#475569", "#151b26")
            frame.setStyleSheet(f"QFrame#tpgPressureCard {{background-color: {background}; border: 1px solid {border}; border-radius: 8px;}}")
            for toggle, checked in ((include, channel.enabled), (display, channel.display)):
                previous = toggle.blockSignals(True)
                toggle.setChecked(bool(checked))
                toggle.blockSignals(previous)
