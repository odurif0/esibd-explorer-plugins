"""Read actual voltages throughout parallel ramps, never echo the requested target."""
from types import SimpleNamespace

import numpy as np
import pytest

import test_ampr_parallel_ramps as ramps

rig = ramps.rig


def monitor_rig(rig, *, down=False, read_time=0.):
    c, hw = rig.controller, rig.controller.device
    c.main_state = "ST_ON"
    c.detected_module_ids = [2]
    c.acquiring = False
    rig.on.state = not down
    c.transitioning = True
    c.transition_target_on = not down
    rig.parent.interval = 5000  # The recording interval is not the live-monitor period.
    rig.parent.getConfiguredModules = lambda: [2]
    for ch in rig.channels:
        ch.waitToStabilize = True  # Must not hide real readings during ramps.
        ch.monitor = np.nan
    accepted = {ch.channel_number(): ch.value if down else 0. for ch in rig.channels}
    reads, snapshots = [], []
    original_write = hw.set_module_voltages

    def write(address, values):
        assert c.lock.locked()
        result = original_write(address, values)
        accepted.update(values)
        return result

    def read(address):
        assert c.lock.locked(), "Reads must be serialized with ramp writes"
        assert address == 2
        reads.append(rig.clock.now)
        rig.clock.now += read_time
        # A small measurement offset distinguishes readbacks from commands.
        return {number: {"setpoint": value, "measured": value + .002}
                for number, value in accepted.items()}

    def publish():
        c.updateValues()
        snapshots.append((rig.clock.now, [ch.monitor for ch in rig.channels]))

    hw.set_module_voltages = write
    hw.get_module_voltages = read
    hw.get_state = lambda: (0, 0, "ST_ON")
    c.signalComm = SimpleNamespace(updateValuesSignal=SimpleNamespace(emit=publish))
    c.initializeValues(reset=True)
    return SimpleNamespace(controller=c, hw=hw, reads=reads, snapshots=snapshots, accepted=accepted)


@pytest.mark.parametrize("down", [False, True])
def test_ramp_reads_and_publishes_each_second_until_every_channel_finishes(rig, down):
    live = monitor_rig(rig, down=down)
    ramps.ramp(rig, down=down)
    assert live.reads == pytest.approx(list(range(21)))
    assert len(live.snapshots) == 21
    for t, values in live.snapshots:
        expected = []
        for ch in rig.channels:
            distance = min(abs(ch.value), t * ch.ramp_rate_v_s)
            target = np.sign(ch.value) * (abs(ch.value) - distance if down else distance)
            expected.append(target + .002)
        assert values == pytest.approx(expected), (t, values, expected)
    assert [ch.value for ch in rig.channels] == [100., -200., 50.]
    assert rig.clock.now == pytest.approx(20.)
    assert rig.parent.interval == 5000, "Do not change the user's recording setting"


def test_last_readback_is_not_lost_for_a_ramp_shorter_than_one_second(rig):
    live = monitor_rig(rig)
    for ch in rig.channels:
        ch.value = 2.
    ramps.ramp(rig)
    assert live.reads == pytest.approx([0., .4])
    assert live.snapshots[-1][1] == pytest.approx([2.002] * 3)


def test_slow_readbacks_do_not_add_their_duration_to_each_ramp_step(rig):
    live = monitor_rig(rig, read_time=.15)
    ramps.ramp(rig)
    assert 20. <= rig.clock.now < 20.5
    assert len(live.reads) >= 20
    for (start, end) in zip(live.reads[:-2], live.reads[1:-1]):
        assert .999 <= end - start <= 1.101
    for t, values in rig.writes:
        for number, value in values.items():
            assert abs(value) <= rig.channels[number - 1].ramp_rate_v_s * t + 1e-8


def test_failed_read_clears_old_voltages_instead_of_repeating_them(rig):
    live = monitor_rig(rig)
    good_read = live.hw.get_module_voltages

    def read(address):
        if .99 <= rig.clock.now <= 1.01:
            live.reads.append(rig.clock.now)
            raise RuntimeError("Invalid measured voltage reply")
        return good_read(address)

    live.hw.get_module_voltages = read
    ramps.ramp(rig)
    assert len(live.snapshots) > 2
    assert np.isnan(live.snapshots[1][1]).all()
    assert np.isfinite(live.snapshots[2][1]).all()
    assert rig.controller.errorCount == 1


@pytest.mark.parametrize("stop_at", [0., 1.])
def test_off_during_readback_stops_further_ramp_up_commands(rig, stop_at):
    live = monitor_rig(rig)
    good_read = live.hw.get_module_voltages

    def read(address):
        result = good_read(address)
        if rig.clock.now + 1e-9 >= stop_at:
            rig.module.AMPRDevice.setOn(rig.parent, on=False)
        return result

    live.hw.get_module_voltages = read
    with pytest.raises(rig.module._AMPRRampCancelled):
        ramps.ramp(rig)
    assert live.reads[-1] == pytest.approx(stop_at)
    if stop_at == 0.:
        assert not rig.writes
    else:
        assert rig.writes
        for t, values in rig.writes:
            assert t <= stop_at + 1e-9
            for number, value in values.items():
                assert abs(value) <= stop_at * rig.channels[number - 1].ramp_rate_v_s + 1e-8
    assert not rig.on.state


@pytest.mark.parametrize("read_time", [.2, 1.4])
def test_normal_monitoring_uses_one_second_start_to_start_or_actual_io_duration(rig, read_time):
    live = monitor_rig(rig, read_time=read_time)
    c = rig.controller
    c.transitioning = False
    c.acquiring = True
    # Even just after a transition, a settling output remains a valid reading.
    assert all(ch.waitToStabilize for ch in rig.channels)
    publish = c.signalComm.updateValuesSignal.emit

    def stop_after_three_samples():
        publish()
        if len(live.reads) == 3:
            c.acquiring = False

    c.signalComm.updateValuesSignal.emit = stop_after_three_samples
    c.runAcquisition()
    period = max(1., read_time)
    assert live.reads == pytest.approx([0., period, 2 * period])
    assert np.isfinite(live.snapshots[-1][1]).all()


@pytest.mark.parametrize("down", [False, True])
def test_off_and_virtual_channels_remain_neutral_during_other_channels_ramps(rig, down):
    live = monitor_rig(rig, down=down)
    rig.channels[0].enabled = False
    rig.channels[1].real = False
    ramps.ramp(rig, down=down)
    assert live.snapshots
    for _, values in live.snapshots:
        assert np.isnan(values[:2]).all()
        assert np.isfinite(values[2])
