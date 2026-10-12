"""Fast stand-in for the ESIBD Explorer 1.0.2 API that plugin unit tests import.

Names and constant values match Explorer 1.0.2 (tests/test_fake_explorer.py compares them with
the installed host). Base methods do as little as possible and record that the plugin called
them, so tests can check the plugin's own logic without Qt. A test that relies on a different
base behaviour subclasses the class here and passes it to :func:`install`.
Real-Qt tests run the installed Explorer in a subprocess instead (support.qt.run_probe).
"""
from __future__ import annotations

from enum import Enum
import importlib.util
from pathlib import Path
import sys
import threading
import types

ROOT = Path(__file__).resolve().parents[2]


class PARAMETERTYPE(Enum):
    LABEL = "LABEL"
    PATH = "PATH"
    COMBO = "COMBO"
    INTCOMBO = "INTCOMBO"
    FLOATCOMBO = "FLOATCOMBO"
    TEXT = "TEXT"
    COLOR = "COLOR"
    BOOL = "BOOL"
    INT = "INT"
    FLOAT = "FLOAT"
    EXP = "EXP"


class PLUGINTYPE(Enum):
    CONSOLE = "Console"
    CONTROL = "Generic Control"
    INPUTDEVICE = "Input Device"
    OUTPUTDEVICE = "Output Device"
    CHANNELMANAGER = "Channel Manager"
    DISPLAY = "Display"
    LIVEDISPLAY = "LiveDisplay"
    SCAN = "Scan"
    DEVICEMGR = "DeviceManager"
    INTERNAL = "Internal"


class PRINT(Enum):
    EXPLORER = 0
    MESSAGE = 1
    WARNING = 2
    ERROR = 3
    CONSOLE = 4
    DEBUG = 5
    VERBOSE = 6
    TRACE = 7


class INOUT(Enum):
    IN = 0
    OUT = 1
    BOTH = 2
    NONE = 3
    ALL = 4


INFO = "Info"


def infoDict(name):
    return {"Plugin": name}


def plotting(function):
    return function


class Parameter:
    NAME = "Name"
    UNIT = "Unit"
    ATTR = "Attribute"
    ADVANCED = "Advanced"
    HEADER = "Header"
    VALUE = "Value"
    MIN = "Min"
    MAX = "Max"
    DEFAULT = "Default"
    ITEMS = "Items"
    FIXEDITEMS = "FixedItems"
    TREE = "Tree"
    TOOLTIP = "Tooltip"
    EVENT = "Event"
    INTERNAL = "Internal"
    RECORDED = "Recorded"
    LOGY = "logY"
    INDICATOR = "Indicator"
    RESTORE = "Restore"
    INSTANTUPDATE = "InstantUpdate"
    PARAMETER_TYPE = "PARAMETER_TYPE"
    DISPLAYDECIMALS = "DISPLAYDECIMALS"
    WIDGET = "WIDGET"


_PARAMETER_KEYS = {"value": Parameter.VALUE, "advanced": Parameter.ADVANCED, "header": Parameter.HEADER,
                   "toolTip": Parameter.TOOLTIP, "indicator": Parameter.INDICATOR,
                   "parameterType": Parameter.PARAMETER_TYPE, "minimum": Parameter.MIN, "maximum": Parameter.MAX}


def parameterDict(**kwargs):
    """Keep the keyword arguments and add the Explorer keys a plugin may read back."""
    parameter = dict(kwargs)
    parameter.update({key: kwargs[name] for name, key in _PARAMETER_KEYS.items() if name in kwargs})
    return parameter


class Signal:
    def emit(self, *args, **kwargs):
        self.last_emit = (args, kwargs)


class ToolButton:
    def __init__(self):
        self.maximum_height = self.minimum_width = self.text = None
        self.checkable = self.auto_raise = self.checked = self.style = None

    def setMaximumHeight(self, height):
        self.maximum_height = height

    def setMinimumWidth(self, width):
        self.minimum_width = width

    def setText(self, text):
        self.text = text

    def setCheckable(self, checkable):
        self.checkable = checkable

    def setAutoRaise(self, auto_raise):
        self.auto_raise = auto_raise

    def setChecked(self, checked):
        self.checked = checked

    def setStyleSheet(self, style):
        self.style = style


class LabviewDoubleSpinBox:
    NAN = "NaN"

    def __init__(self, indicator=False, displayDecimals=2):
        self.indicator = indicator
        self.displayDecimals = displayDecimals
        self._decimals = displayDecimals

    def setDecimals(self, value):
        self._decimals = value

    def decimals(self):
        return self._decimals


class Channel:
    HEADER = "HEADER"
    SELECT = "Select"
    COLLAPSE = "Collapse"
    ENABLED = "Enabled"
    NAME = "Name"
    VALUE = "Value"
    BACKGROUND = "Background"
    EQUATION = "Equation"
    DISPLAY = "Display"
    ACTIVE = "Active"
    REAL = "Real"
    SMOOTH = "Smooth"
    LINEWIDTH = "Linewidth"
    LINESTYLE = "Linestyle"
    DISPLAYGROUP = "Group"
    SCALING = "Scaling"
    COLOR = "Color"
    MIN = "Min"
    MAX = "Max"
    OPTIMIZE = "Optimize"
    MONITOR = "Monitor"
    UNIT = "Unit"
    ADDITEM = "Add Item"
    EDITITEM = "Edit Item"
    REMOVEITEM = "Remove Item"
    ADDPARTOCONSOLE = "Add Parameter to Console"
    ADDCHANTOCONSOLE = "Add Channel to Console"
    NOTES = "Notes"

    def __init__(self, channelParent=None, tree=None):
        self.channelParent = channelParent
        self.tree = tree

    def getDevice(self):
        return getattr(self, "channelParent", None)  # Tests may build channels without __init__.

    @property
    def loading(self):
        return getattr(self.getDevice(), "loading", False)

    def getDefaultChannel(self):
        return {
            self.ACTIVE: {Parameter.HEADER: "A", Parameter.ADVANCED: True},
            self.DISPLAY: {Parameter.HEADER: "D", Parameter.ADVANCED: False},
            self.REAL: {Parameter.HEADER: "R", Parameter.ADVANCED: True},
            self.ENABLED: {Parameter.HEADER: "E", Parameter.ADVANCED: True},
            self.VALUE: {Parameter.HEADER: "Value"},
            self.MONITOR: {Parameter.HEADER: "Monitor"},
            self.SCALING: {Parameter.VALUE: "normal"},
            self.MIN: {},
            self.MAX: {},
        }

    def getSortedDefaultChannel(self):
        return self.getDefaultChannel()

    def setDisplayedParameters(self):
        self.displayedParameters = [self.COLLAPSE, self.ENABLED, self.NAME, self.VALUE, self.DISPLAY, self.ACTIVE,
                                    self.REAL, self.SCALING, self.MIN, self.MAX, self.OPTIMIZE]
        if getattr(self.getDevice(), "useMonitors", False):  # As in Explorer.
            self.displayedParameters.insert(self.displayedParameters.index(self.VALUE) + 1, self.MONITOR)

    def getParameterByName(self, name):
        return getattr(self, "_parameters", {}).get(name)

    def initGUI(self, item):
        self.super_init_gui_called = item

    def scalingChanged(self):
        self.rowHeight = 18

    def nameChanged(self):
        self.super_name_changed_called = True

    def valueChanged(self):
        self.super_value_changed_called = True

    def equationChanged(self):
        self.super_equation_changed_called = True

    def activeChanged(self):
        self.super_active_changed_called = True

    def enabledChanged(self):
        self.super_enabled_changed_called = True

    def updateDisplay(self):
        self.super_update_display_called = True

    def updateMin(self):
        self.super_update_min_called = True

    def updateMax(self):
        self.super_update_max_called = True


class DeviceController:
    def __init__(self, controllerParent=None):
        self.controllerParent = controllerParent
        self.lock = threading.Lock()
        self.signalComm = types.SimpleNamespace(initCompleteSignal=Signal(), closeCommunicationSignal=Signal(),
                                                updateValuesSignal=Signal())  # Explorer's DeviceController signals.
        self.errorCount = 0
        self.initializing = False
        self.acquiring = False
        self.values = None

    def print(self, message, **kwargs):
        pass

    def startAcquisition(self):
        self.acquiring = True

    def stopAcquisition(self):
        self.acquiring = False

    def toggleOn(self):
        self.super_toggle_called = True

    def closeCommunication(self):
        return None

    def initComplete(self):
        self.super_init_complete_called = True


class Plugin:
    pass


class LiveDisplay:
    pass


class SettingsManager:
    pass


class TreeWidget:
    pass


class MetaChannel:
    pass


class Scan:
    PARAMETER = "Parameter"
    VERSION = "Version"
    VALUE = "Value"
    UNIT = "Unit"
    NOTES = "Notes"
    DISPLAY = "Display"
    LEFTRIGHT = "Left-Right"
    UPDOWN = "Up-Down"
    WAITLONG = "Wait long"
    LARGESTEP = "Large step"
    WAIT = "Wait"
    AVERAGE = "Average"
    SCANTIME = "Scan time"
    INVALIDWHILEWAITING = "Invalid while waiting"
    INTERVAL = "Interval"
    START = "From"
    STOP = "To"
    STEP = "Step"
    CHANNEL = "Channel"
    SCAN = "Scan"
    INPUTCHANNELS = "Input Channels"
    OUTPUTCHANNELS = "Output Channels"
    MYBLUE = "#1f77b4"
    MYGREEN = "#00aa00"
    MYRED = "#d62728"
    MIN_STEPS = 3
    Display = type("Display", (), {})
    recording = property(lambda self: getattr(self, "_recording", False),
                         lambda self, value: setattr(self, "_recording", bool(value)))


class Device:
    MAXSTORAGE = "Max storage"
    MAXDATAPOINTS = "Max data points"
    ERRORCOUNT = "Error count"
    MAXERRORCOUNT = "Max error count"
    LOGGING = "Logging"
    MAX_DISPLAY_SIZE_DEFAULT = 1000

    def getChannels(self):
        return getattr(self, "channels", [])

    def toggleAdvanced(self, advanced=False):
        self.super_toggle_advanced_called = advanced


CORE = ("PARAMETERTYPE", "PLUGINTYPE", "PRINT", "INOUT", "INFO", "infoDict", "plotting", "Parameter",
        "parameterDict", "Channel", "MetaChannel", "DeviceController", "ToolButton", "LabviewDoubleSpinBox",
        "TreeWidget", "getTestMode")
PLUGINS = ("Device", "Plugin", "LiveDisplay", "Scan", "SettingsManager")


def getTestMode():
    return False


def install(monkeypatch=None, **replacements) -> types.SimpleNamespace:
    """Register fresh fake ``esibd``, ``esibd.core`` and ``esibd.plugins`` modules.

    ``replacements`` swaps any name (``Channel=MyChannel``) in the module that defines it. With
    ``monkeypatch``, the previous ``sys.modules`` entries come back after the test.
    """
    esibd = types.ModuleType("esibd")
    core = types.ModuleType("esibd.core")
    plugins = types.ModuleType("esibd.plugins")
    unknown = set(replacements) - set(CORE) - set(PLUGINS)
    if unknown:
        raise TypeError(f"Unknown Explorer names: {sorted(unknown)}")
    for module, names in ((core, CORE), (plugins, PLUGINS)):
        for name in names:
            value = replacements.get(name, globals()[name])
            if isinstance(value, type) and not issubclass(value, Enum):
                # A fresh subclass per install: a test that patches it cannot leak into the next one.
                value = type(value.__name__, (value,), {"__module__": module.__name__})
            setattr(module, name, value)
    esibd.core, esibd.plugins = core, plugins
    for name, module in (("esibd", esibd), ("esibd.core", core), ("esibd.plugins", plugins)):
        if monkeypatch is None:
            sys.modules[name] = module
        else:
            monkeypatch.setitem(sys.modules, name, module)
    return types.SimpleNamespace(esibd=esibd, core=core, plugins=plugins)


def clear(*module_names: str, runtimes: tuple[str, ...] = ()) -> None:
    """Forget the fake Explorer, the given plugin modules and their private bundled runtimes."""
    prefixes = tuple(f"_esibd_bundled_{runtime}_runtime" for runtime in runtimes)
    for name in list(sys.modules):
        if name == "esibd" or name.startswith("esibd.") or name in module_names or name.startswith(prefixes or ("\0",)):
            sys.modules.pop(name, None)


def load_plugin(path: Path | str, module_name: str, *, runtimes: tuple[str, ...] = (), **replacements):
    """Import a plugin entry point against a freshly installed fake Explorer."""
    clear(module_name, runtimes=runtimes)
    install(**replacements)
    spec = importlib.util.spec_from_file_location(module_name, ROOT / path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def install_fake_qtcore(monkeypatch):
    """Minimal PyQt6.QtCore for the wheel-event filter; returns the fake QEvent class."""
    pyqt = types.ModuleType("PyQt6")
    pyqt.__path__ = []
    qtcore = types.ModuleType("PyQt6.QtCore")

    class QObject:
        def __init__(self, parent=None):
            self.parent = parent

    class QEvent:
        class Type:
            Wheel = "wheel"

    qtcore.QObject, qtcore.QEvent = QObject, QEvent
    pyqt.QtCore = qtcore
    monkeypatch.setitem(sys.modules, "PyQt6", pyqt)
    monkeypatch.setitem(sys.modules, "PyQt6.QtCore", qtcore)
    return QEvent
