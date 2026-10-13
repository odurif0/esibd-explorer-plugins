"""Optional PSU associations must not become imports, commands or invented volts."""
from types import SimpleNamespace

import pytest

from support.amx import make_case
from support.amx import config79_snapshot
from support.amx import register_sources


PSU_COLUMN = 4  # _AMX_OUTPUT_COLUMNS: PSU sits next to the levels it explains.


class Item:
    def setText(self, value):
        self.text = value

    def setToolTip(self, value):
        self.tooltip = value


@pytest.fixture(params=("amx_a", "amx_b", "amx_hd"))
def rig(request):
    module, parent, controller = make_case(request.param)
    assert module._AMX_OUTPUT_COLUMNS[PSU_COLUMN][0] == "psu"
    items = [[Item() for _ in range(len(module._AMX_OUTPUT_COLUMNS))] for _ in range(4)]
    parent.amxOutputTable = SimpleNamespace(item=lambda row, col: items[row][col])
    apply_reference_routing(request.param, controller)
    parent.psu_ch01, parent.psu_ch23 = "PSU_A", "None"
    register_sources(parent)
    return module, parent, controller, items


def apply_reference_routing(folder, controller, *, static=False):
    """Four periodic outputs; optionally CH0 Vpos, CH1 Vneg, CH2 Hi-Z, CH3 external."""
    if folder == "amx_hd":
        from support.amx import config273
        state, routing = config273()
        if static:
            for switch, (field, value) in zip(routing["switches"], (
                    ("trigger_source", 0x80), ("trigger_source", 0), ("enable_source", 0), ("trigger_source", 56))):
                switch[field] = value
        controller.hd_routing = routing
    else:
        state = config79_snapshot()
        if static:
            state["switches"][0]["trigger_config"] = 32  # constant 1 -> Vpos
            state["switches"][1]["trigger_config"] = 0   # constant 0 -> Vneg
            state["switches"][2]["enable_config"] = 0    # Hi-Z
            state["switches"][3]["trigger_config"] = 3   # external
    controller._apply_snapshot(state)


def provider(name="PSU_A", positive=101., negative=102.):
    channels = [SimpleNamespace(
        id=ch, name=f"{name}_CH{ch}", monitor=value, value=9999., initialized=True,
        unit="V", useMonitors=True, real=True, enabled=True,
        readback_status="Measured PSU voltage.",
    ) for ch, value in enumerate((positive, negative))]
    return SimpleNamespace(name=name, getChannels=lambda: channels)


@pytest.mark.parametrize("case", ("none", "absent", "old", "duplicate", "error", "malformed"))
def test_unavailable_provider_keeps_symbolic_rails(rig, case):
    _, parent, _, items = rig
    psu = provider()
    if case == "none": parent.psu_ch01 = "None"
    elif case == "old": parent.pluginManager.plugins = [SimpleNamespace(name="PSU_A")]
    elif case == "duplicate": parent.pluginManager.plugins = [psu, psu]
    elif case == "error":
        def fail():
            raise RuntimeError("Unreadable PSU")
        psu.getChannels = fail
        parent.pluginManager.plugins = [psu]
    elif case == "malformed":
        psu.getChannels = lambda: None
        parent.pluginManager.plugins = [psu]
    parent._update_output_table()
    assert items[0][3].text == "Vneg ↔ Vpos"
    assert "0 V" not in items[0][3].text
    assert items[0][PSU_COLUMN].text == ("—" if case == "none" else "PSU_A")


@pytest.mark.parametrize("invalid", (None, float("nan"), float("inf"), -3.))
def test_bad_or_wrong_sign_value_does_not_gain_a_voltage(rig, invalid):
    _, parent, _, items = rig
    parent.pluginManager.plugins = [provider(positive=invalid)]
    parent._update_output_table()
    assert items[0][3].text == "-102 V ↔ Vpos"
    parent.pluginManager.plugins = [provider(negative=invalid)]
    parent._update_output_table()
    assert items[0][3].text == "Vneg ↔ +101 V"


@pytest.mark.parametrize("psu_folder", [f"psu_{suffix}" for suffix in "abcde"])
def test_each_real_psu_publishes_channels_for_either_pair(rig, psu_folder):
    from support.psu import make_psu, snapshot
    _, parent, controller, items = rig
    _, psu, psu_controller = make_psu(psu_folder)
    psu_controller._apply_snapshot(snapshot(101., 102.))
    parent.psu_ch01 = parent.psu_ch23 = psu.name
    parent.pluginManager.plugins = [parent, psu]
    before = [dict(row) for row in controller.output_rows]
    parent._update_output_table()
    assert all(row[3].text == "-102 V ↔ +101 V" for row in items)
    assert all(row[PSU_COLUMN].text == psu.name for row in items)
    assert controller.output_rows == before
    assert controller.device.timing_writes == []


def test_static_hiz_and_unknown_routing_are_not_replaced_by_both_rails(rig):
    module, parent, controller, items = rig
    parent.pluginManager.plugins = [provider()]
    parent.psu_ch23 = "PSU_A"
    apply_reference_routing("amx_hd" if hasattr(module, "AMXHDDevice") else "amx_a", controller, static=True)
    parent._update_output_table()
    assert [row[3].text for row in items] == ["+101 V", "-102 V", "Hi-Z", "Unknown"]
    parent.main_state = "Shutdown unconfirmed"
    parent._update_output_table()
    assert all(row[3].text == "Unknown" for row in items)


def test_renaming_reordering_and_ambiguous_registry(rig):
    _, parent, _, items = rig
    psu = provider()
    register_sources(parent, psu)
    sources = psu.getChannels()
    sources[0].name, sources[1].name = "My positive supply", "My negative supply"
    sources.reverse()
    parent._update_output_table()
    assert items[0][3].text == "-102 V ↔ +101 V"
    duplicate = SimpleNamespace(getChannels=lambda: [sources[0]])
    parent.pluginManager.plugins.append(duplicate)
    parent._update_output_table()
    assert items[0][3].text == "Vneg ↔ +101 V"


@pytest.mark.parametrize("attribute,value", (("real", False), ("enabled", False), ("initialized", False),
                                           ("useMonitors", False), ("unit", "A")))


def test_ineligible_source_is_not_used(rig, attribute, value):
    _, parent, _, items = rig
    psu = provider()
    setattr(psu.getChannels()[0], attribute, value)
    register_sources(parent, psu)
    parent._update_output_table()
    assert items[0][3].text == "-102 V ↔ Vpos"


def test_old_psu_monitor_without_validity_contract_is_rejected(rig):
    _, parent, _, items = rig
    psu = provider()
    del psu.getChannels()[0].readback_status
    register_sources(parent, psu)
    parent._update_output_table()
    assert items[0][3].text == "-102 V ↔ Vpos"
    assert "update the PSU" in items[0][3].tooltip


def test_settings_are_persistent_independent_and_do_not_use_slashes(rig, monkeypatch):
    module, parent, _, _ = rig
    parent.MAXDATAPOINTS = "Max data points"
    monkeypatch.setattr(module.Device, "getDefaultSettings", lambda self: {
        f"{self.name}/Interval": {module.Parameter.VALUE: 1000},
        f"{self.name}/Max data points": {module.Parameter.VALUE: 100000},
    }, raising=False)
    definitions = parent.getDefaultSettings()
    for key, attr in ((parent.PSU_CH01, "psu_ch01"), (parent.PSU_CH23, "psu_ch23")):
        assert "/" not in key  # Explorer splits at the last slash.
        definition = definitions[f"{parent.name}/{key}"]
        assert definition["value"] == "None"
        assert definition["attr"] == attr
        assert definition["items"].split(",") == ["None", "PSU_A", "PSU_B", "PSU_C", "PSU_D", "PSU_E"]
        assert definition["fixedItems"] is True
        assert definition.get("restore", True) is True
        assert definition["event"] == parent._update_output_table


