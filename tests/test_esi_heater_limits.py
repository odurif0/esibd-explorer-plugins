"""Pure encoding checks; no vendor library or instrument is loaded."""
import ast
import importlib.util
import json
import math
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / 'esi/_heater_limits.py'


@pytest.fixture
def limits():
    name = '_esi_pure_limits_test'
    spec = importlib.util.spec_from_file_location(name, HELPER)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('power', [False, True])
@pytest.mark.parametrize('cap', [None, True, False, '50', 0, -1, math.nan, math.inf, -math.inf, 1e-20])
def test_invalid_or_unrepresentable_caps_fail(limits, power, cap):
    with pytest.raises(ValueError):
        limits.bounded_limit(cap, power)


@pytest.mark.parametrize('cap,power,expected', [
    (22., False, 21.999616), (10., False, 9.99936),
    (50., True, 49.999861776384),
])
def test_approved_envelope_quantization(limits, cap, power, expected):
    value = limits.bounded_limit(cap, power)
    assert value == pytest.approx(expected, rel=0, abs=1e-12)
    assert 0 < value <= cap
    assert limits.native_value(limits.native_code(value, power), power) == value


def test_direct_ten_amp_request_rounds_above_ceiling(limits):
    assert limits.native_code(10.) == 9766
    assert limits.native_value(9766) == pytest.approx(10.000384, rel=0, abs=1e-12)
    assert limits.native_value(9766) > 10.
    assert limits.native_code(limits.bounded_limit(10.)) == 9765


@pytest.mark.parametrize('power', [False, True])
@pytest.mark.parametrize('code', [2, 3, 9765, 21484, 46566, 167638063])
def test_adjacent_representable_boundaries(limits, power, code):
    exact = limits.native_value(code, power)
    for cap in (math.nextafter(exact, 0.), exact, math.nextafter(exact, math.inf)):
        value = limits.bounded_limit(cap, power)
        chosen = limits.native_code(value, power)
        assert 0 < value <= cap
        assert limits.native_value(chosen, power) == value
        assert limits.native_value(chosen + 1, power) > cap


def test_char_uses_one_canonical_encoding_helper():
    nb = json.loads((ROOT / 'notebooks/esi_heater_characterization.ipynb').read_text())
    source = '\n'.join(''.join(c['source']) for c in nb['cells'] if c['cell_type'] == 'code')
    tree = ast.parse(source)
    names = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    assert not names.intersection({'native_code', 'native_value', 'bounded_limit'})
    assert "load_pure_helper(root, '_heater_limits.py').bounded_limit" in source


def test_helper_has_no_instrument_or_notebook_import():
    tree = ast.parse(HELPER.read_text())
    imports = [n for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))]
    assert len(imports) == 1 and isinstance(imports[0], ast.Import)
    assert [alias.name for alias in imports[0].names] == ['math']
