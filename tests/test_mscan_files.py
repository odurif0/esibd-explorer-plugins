"""MScan scan files: <Data path>/MScan/<date>/<date_time>[_<name>]_PSU-<X>_<from>-<to>V.h5.

User choice 2026-10-26: the Explorer session names (session date of May, substrate "None",
molecule "GroEL", measurement counter) made the files unreadable.
"""
import time
from types import SimpleNamespace as NS

NOW = time.strptime('2026-10-26 14:46:27', '%Y-%m-%d %H:%M:%S')


def scan_in(rig, tmp_path, label=''):
    s = rig.scan
    s.pluginManager.Settings = NS(dataPath=tmp_path)
    s.file_label = label
    s.start, s.stop = 0., 150.
    return s


def test_default_name_uses_the_scan_time_psu_and_range(mscan_rig, tmp_path):
    s = scan_in(mscan_rig, tmp_path)
    assert s._scan_file(NOW) == tmp_path / 'MScan' / '2026-10-26' / '2026-10-26_14-46-27_PSU-A_0-150V.h5'


def test_name_field_is_inserted_and_cleaned(mscan_rig, tmp_path):
    s = scan_in(mscan_rig, tmp_path, label='  essai 1/ref*  ')
    assert s._scan_file(NOW).name == '2026-10-26_14-46-27_essai-1-ref_PSU-A_0-150V.h5'


def test_an_existing_file_is_never_overwritten(mscan_rig, tmp_path):
    s = scan_in(mscan_rig, tmp_path, label='essai1')
    first = s._scan_file(NOW)
    first.parent.mkdir(parents=True)
    first.write_bytes(b'kept')
    assert s._scan_file(NOW).name == '2026-10-26_14-46-27_essai1_PSU-A_0-150V_2.h5'
    assert first.read_bytes() == b'kept'


def test_update_file_creates_the_day_folder_without_the_explorer_counter(mscan_rig, tmp_path):
    s = scan_in(mscan_rig, tmp_path)
    s._dummy_initialization = False
    s.displayActive = lambda: False
    s.updateFile()
    assert s.file.parent.is_dir() and s.file.parent.parent == tmp_path / 'MScan'
    assert s.file.suffix == '.h5' and '_PSU-A_0-150V' in s.file.name
    assert not hasattr(s.pluginManager.Settings, 'measurementNumber')


def test_axis_names_the_driven_psu(mscan_rig):
    assert mscan_rig.scan._axis_label() == 'PSU A (V)'
