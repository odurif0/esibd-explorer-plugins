"""A missing authoritative config list must never authorize an NVM write."""

import runpy
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("folder", ["psu_a", "psu_b", "psu_c", "psu_d", "psu_e"])
@pytest.mark.parametrize("scenario", ["initial_read_error", "locked_read_error", "occupied", "empty"])
def test_save_requires_successful_locked_config_check(folder, scenario):
    helpers = runpy.run_path(str(ROOT / "tests/test_psu_plugin_behavior.py"))
    loader = helpers["_load_module"]
    loader.__globals__["PLUGIN_PATH"] = ROOT / folder / "psu_plugin.py"
    module = loader()
    writes = []
    reads = []

    class Device:
        def list_configs(self, timeout_s=None):
            reads.append(controller.lock.locked())
            if scenario == "initial_read_error" or scenario == "locked_read_error" and len(reads) == 2:
                raise RuntimeError("configuration list could not be read")
            if scenario == "occupied" and len(reads) >= 2:
                return [{"index": 7, "name": "Existing config", "active": True, "valid": True}]
            return []

        def save_config(self, index, **kwargs):
            assert controller.lock.locked()
            writes.append(index)

    parent = types.SimpleNamespace(
        name=folder.upper(), startup_timeout_s=1.0, connect_timeout_s=1.0,
        getChannels=lambda: [],
    )
    controller = module.PSUController(parent)
    controller.initialized = True
    controller.device = Device()
    messages = []
    controller.print = lambda message, **kwargs: messages.append(message)
    controller.saveCurrentConfig(config_index=7, config_name="New config")
    if scenario == "empty":
        assert writes == [7]
        assert reads == [False, True, False]
        assert any("Saved PSU config 7" in message for message in messages)
    else:
        assert writes == []
        assert reads == ([False] if scenario == "initial_read_error" else [False, True])
        assert any("Cannot save" in message for message in messages)
        assert not any("Saved PSU" in message for message in messages)
