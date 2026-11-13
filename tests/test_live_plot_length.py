"""Live plots show the last 30 min by default (user, 2026-11-12: "au moins les 30 dernières minutes").

Explorer's length menu offers 0.2 to 1440 min without 30 and starts at 2 min. Each device plugin adds
30 and selects it once; the menu is saved with its items, so the operator's later choice stays.
"""
from __future__ import annotations

import pytest
from support import ampr, amx, dmmr, esi, explorer, psu

DEVICES = {
    'ampr_a': (lambda: ampr.load_plugin(module_name='ampr_live_plot_test'), 'AMPRDevice'),
    'amx_a': (lambda: amx.load_plugin('amx_a', 'amx_a_live_plot_test'), 'AMXDevice'),
    'amx_hd': (lambda: amx.load_plugin('amx_hd', 'amx_hd_live_plot_test'), 'AMXHDDevice'),
    'dmmr': (lambda: dmmr.load_plugin(module_name='dmmr_live_plot_test'), 'DMMRDevice'),
    'esi': (lambda: esi.load_plugin(module_name='esi_live_plot_test'), 'ESIDevice'),
    'psu_a': (lambda: psu.load_plugin('psu_a', 'psu_live_plot_test'), 'PSUDevice'),
    'tpg366': (lambda: explorer.load_plugin('tpg366/tpg366_plugin.py', 'tpg366_live_plot_test',
                                            runtimes=('tpg366',)), 'TPG366'),
}
EXPLORER_MENU = ['-1', '0.2', '1', '2', '3', '5', '10', '60', '600', '1440']


class _Combo:
    """Explorer's RestoreFloatComboBox and its internal Setting, as far as the plugins use them."""

    def __init__(self, items, value):
        self.texts, self.value, self.setting, self.loading = list(items), value, self, False

    @property
    def items(self):
        return list(self.texts)

    def findText(self, text):
        return self.texts.index(text) if text in self.texts else -1

    def insertItem(self, index, text):
        self.texts.insert(index, text)


@pytest.mark.parametrize('folder', DEVICES)
def test_the_live_plot_offers_30_minutes_and_selects_it_once(folder, monkeypatch):
    load, name = DEVICES[folder]
    module = load()
    display_class = getattr(module, name).LiveDisplay
    base = next(k for k in display_class.__mro__[1:] if k.__name__ == 'LiveDisplay')
    monkeypatch.setattr(base, 'initGUI', lambda self: None, raising=False)
    display = display_class.__new__(display_class)
    display.displayTimeComboBox = _Combo(EXPLORER_MENU, '2')
    display.initGUI()
    assert display.displayTimeComboBox.texts == [*EXPLORER_MENU[:7], '30', *EXPLORER_MENU[7:]]
    assert display.displayTimeComboBox.value == '30'
    display.displayTimeComboBox.value = '10'  # The operator's later choice, 30 already in the saved menu.
    display.initGUI()
    assert display.displayTimeComboBox.value == '10' and display.displayTimeComboBox.texts.count('30') == 1


class _View:
    def __init__(self):
        self.x_enabled = False

    def mouseEnabled(self):
        return self.x_enabled, True

    def sceneBoundingRect(self):
        return type('Rect', (), {'contains': lambda _, pos: pos == 'plot area'})()


class _Axis:
    def __init__(self, view):
        self.view, self.wheels = view, []

    def linkedView(self):
        return self.view

    def wheelEvent(self, event):
        self.wheels.append(event)


class _Event:
    def __init__(self, where):
        self.where = where

    def scenePos(self):
        return self.where


@pytest.mark.parametrize('folder', DEVICES)
def test_the_wheel_over_the_time_axis_switches_to_manual_x_then_zooms(folder, monkeypatch):
    """User, 2026-11-13: zoom the time axis with the wheel, as the y axis."""
    load, name = DEVICES[folder]
    display_class = getattr(load(), name).LiveDisplay
    base = next(k for k in display_class.__mro__[1:] if k.__name__ == 'LiveDisplay')
    view = _View()
    axes = {side: _Axis(view) for side in ('bottom', 'top')}
    widget = type('Plot', (), {'getAxis': lambda _, side: axes.get(side)})()

    def built(self):  # Explorer's initFig: the plots, with their axes, are new
        self.livePlotWidgets, self.plotSplitter = [widget], None
    monkeypatch.setattr(base, 'initFig', built, raising=False)
    display = display_class.__new__(display_class)
    switches = []

    def manual_x(x, y=None):  # Explorer's updateMouseEnabled, as its "Scale x manually" button
        switches.append(x)
        view.x_enabled = x
    display.updateMouseEnabled = manual_x
    display.initFig()
    display.initFig()  # A rebuild must not wrap the same axis twice.

    axes['bottom'].wheelEvent(_Event('plot area'))  # pyqtgraph passes this one on to the plot itself
    assert switches == [] and len(axes['bottom'].wheels) == 1
    axes['bottom'].wheelEvent(_Event('axis'))
    assert switches == [True] and len(axes['bottom'].wheels) == 2
    axes['bottom'].wheelEvent(_Event('axis'))  # already manual: zoom only
    axes['top'].wheelEvent(_Event('axis'))
    assert switches == [True] and len(axes['bottom'].wheels) == 3 and len(axes['top'].wheels) == 1
