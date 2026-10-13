"""The expected-switching sketch draws only what the routing summary inferred, never a guess."""
import pytest

from support.amx import config79_snapshot, config273, load_module


@pytest.fixture(params=("amx_a", "amx_b", "amx_hd"))
def module(request):
    return load_module(request.param)


def rows_of(module, snapshot=None):
    if hasattr(module, "_amx_hd_output_rows"):
        return module._amx_hd_output_rows(*config273())
    return module._amx_output_rows(snapshot or config79_snapshot())


def test_reference_configs_draw_four_square_waves_from_the_reference_edge(module):
    model = module._amx_waveform_traces(rows_of(module))
    hd = hasattr(module, "_amx_hd_output_rows")
    half = 50.0 if hd else 1.0  # 10 kHz or 500 kHz, symmetric.
    assert model["window_us"] == pytest.approx(4 * half)
    traces = model["traces"]
    assert [t["kind"] for t in traces] == ["wave"] * 4
    assert all(t["label"] == "" for t in traces)  # Same period: phases are related.
    vpos_first = [(0.0, half), (2 * half, 3 * half)]
    vneg_first = [(half, 2 * half), (3 * half, 4 * half)]
    for trace, expected in zip(traces, (vpos_first, vneg_first, vpos_first, vneg_first)):
        assert trace["segments"] == pytest.approx(expected)


def test_changed_registers_static_and_hiz_rows():
    module = load_module("amx_a")
    snapshot = config79_snapshot()
    snapshot["oscillator"]["period"] = 798
    snapshot["pulsers"][0]["width_ticks"] = 298
    snapshot["switches"][2]["trigger_config"] = 32
    snapshot["switches"][3]["enable_config"] = 0
    traces = module._amx_waveform_traces(module._amx_output_rows(snapshot))["traces"]
    assert traces[0]["segments"] == pytest.approx([(0.0, 3.0), (8.0, 11.0)])  # 3 µs Vpos / 5 µs Vneg.
    assert traces[1]["segments"] == pytest.approx([(3.0, 8.0), (11.0, 16.0)])
    assert traces[2] == {"channel": "CH2", "kind": "level", "high": True, "label": "Vpos (constant)"}
    assert traces[3]["kind"] == "hiz" and "not 0 V" in traces[3]["label"]


def test_a_delayed_pulser_is_drawn_shifted_and_edge_delays_are_not_drawn():
    module = load_module("amx_a")
    snapshot = config79_snapshot()
    snapshot["pulsers"][1] = dict(snapshot["pulsers"][0], pulser=1, delay_ticks=57)
    snapshot["switches"][1]["trigger_config"] = 11
    traces = module._amx_waveform_traces(module._amx_output_rows(snapshot))["traces"]
    assert traces[1]["segments"] == pytest.approx([(0.5, 1.5), (2.5, 3.5)])
    snapshot["switches"][1]["trigger_delay"]["rise"] = 1
    trace = module._amx_waveform_traces(module._amx_output_rows(snapshot))["traces"][1]
    assert trace["kind"] == "none" and "edge delays" in trace["label"]


def test_unknown_signals_stay_labelled_gaps(module):
    if hasattr(module, "_amx_hd_output_rows"):
        pytest.skip("Same drawing code; AMX HD unknown routing is covered by its row tests.")
    snapshot = config79_snapshot()
    snapshot["switch_mapping"]["trigger_enabled"] = True
    model = module._amx_waveform_traces(module._amx_output_rows(snapshot))
    assert model["window_us"] is None
    assert [(t["kind"], t["label"]) for t in model["traces"]] == [("none", "Unknown")] * 4


def test_stopped_generator_gives_flat_rails_without_a_time_axis(module):
    if hasattr(module, "_amx_hd_output_rows"):
        pytest.skip("AMX HD static configurations are covered by its row tests.")
    snapshot = config79_snapshot()
    snapshot["pulsers"][0]["width_ticks"] = 0
    model = module._amx_waveform_traces(module._amx_output_rows(snapshot))
    assert model["window_us"] is None
    assert [(t["kind"], t["high"]) for t in model["traces"]] == [("level", False), ("level", True)] * 2


def test_unrelated_or_too_fast_rows_are_flagged(module):
    def row(channel, period, high):
        return {"channel": channel, "state": "Periodic", "frequency": f"{1e5 / period:g} kHz",
                "timing": {"period": period, "high": high, "phase": 0}}
    rows = [row("CH0", 100_000, 50_000), row("CH1", 100, 50), row("CH2", 125_000, 60_000)]
    traces = module._amx_waveform_traces(rows)["traces"]
    assert traces[0]["kind"] == "wave" and traces[0]["label"] == ""
    assert traces[1]["kind"] == "dense" and "too fast" in traces[1]["label"]
    assert traces[2]["kind"] == "wave" and "phase not related" in traces[2]["label"]
    assert module._amx_waveform_traces(None) == {"window_us": None, "traces": []}
