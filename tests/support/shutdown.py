"""Per-family controllers shared by the shutdown-confirmation tests."""
import logging
import threading
import types

from support.ampr import load_plugin as load_ampr
from support.amx import load_plugin as load_amx
from support.dmmr import load_plugin as load_dmmr
from support.esi import load_plugin as load_esi
from support.psu import load_plugin as load_psu

FAMILIES = {
    "ampr": (load_ampr, "AMPRController", "_get_ampr_driver_class"),
    "amx": (load_amx, "AMXController", "_get_amx_driver_class"),
    "amx_hd": (lambda: load_amx("amx_hd"), "AMXHDController", "_get_amx_driver_class"),
    "dmmr": (load_dmmr, "DMMRController", "_get_dmmr_driver_class"),
    "esi": (load_esi, "ESIController", "_get_esi_driver_class"),
    "psu": (load_psu, "PSUController", "_get_psu_driver_class"),
}


def controller_for(family):
    loader, name, _getter = FAMILIES[family]
    module = loader()
    parent = types.SimpleNamespace(
        name=family, getChannels=lambda: [], isOn=lambda: False,
        connect_timeout_s=0.1, poll_timeout_s=0.1, startup_timeout_s=0.1,
    )
    controller = getattr(module, name)(parent)
    controller.lock = threading.Lock()
    controller.errorCount = 0
    messages = []
    controller.print = lambda msg, **kwargs: messages.append(msg)
    return module, controller, messages


def runtime_for(family):
    loader, _name, getter = FAMILIES[family]
    module = loader()
    cls = getattr(module, getter)()._PROCESS_CONTROLLER_CLASS
    driver = cls.__new__(cls)
    driver.connected = True
    driver._transport_poisoned = False
    driver._transport_error = None
    driver._dll_port_claimed = True
    driver.thread_lock = threading.Lock()
    driver.device_id = "shutdown-regression"
    driver.logger = logging.getLogger("shutdown-regression")
    driver.stop_housekeeping = lambda: None
    driver._set_port_claimed = lambda value: setattr(driver, "_dll_port_claimed", value)
    return driver
