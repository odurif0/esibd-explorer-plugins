"""Regression test for the AMPR ``GetModuleHousekeeping`` ctypes ABI (P0-1).

``COM-AMPR-12.h`` declares::

    COM_AMPR_12_GetModuleHousekeeping(unsigned Address,
        double& Volt3V3, double& TempCPU, double& Volt5V0,
        double& Volt12Vp, double& Volt12Vn, double& Volt1V8p, double& Volt1V8n)

i.e. **7** output doubles. The Python binding previously allocated and passed **9**
``byref(double)`` arguments (with field names that did not even exist in the header),
so every returned rail voltage / temperature landed in the wrong slot. This test
pins the contract: exactly 7 byref doubles, in header order, surfaced as an 8-tuple
``(status, volt_3v3, temp_cpu, volt_5v0, volt_12vp, volt_12vn, volt_1v8p, volt_1v8n)``.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# Header order: Volt3V3, TempCPU, Volt5V0, Volt12Vp, Volt12Vn, Volt1V8p, Volt1V8n.
_INJECTED = [3.3, 41.5, 5.0, 12.1, -12.1, 1.8, -1.8]


def _load_base(plugin: str):
    spec = importlib.util.spec_from_file_location(
        f"_ampr_base_abi_{plugin}",
        ROOT / plugin / "vendor" / "runtime" / "ampr" / "ampr_base.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeDll:
    """Records the byref args written by the binding and injects known values."""

    NO_ERR = 0
    n_byref = None

    def COM_AMPR_12_GetModuleHousekeeping(self, *args):
        # args[0] is the address (c_uint); the rest are byref(double) pointers.
        self.n_byref = len(args) - 1
        for ref, value in zip(args[1:], _INJECTED):
            ref._obj.value = value
        return self.NO_ERR


@pytest.mark.parametrize("plugin", ["ampr_a", "ampr_b"])
def test_get_module_housekeeping_passes_seven_byref_in_header_order(plugin):
    base_module = _load_base(plugin)
    instance = base_module.AMPRBase.__new__(base_module.AMPRBase)
    fake = _FakeDll()
    instance.ampr_dll = fake

    status, *values = instance.get_module_housekeeping(address=3)

    assert fake.n_byref == 7, f"expected 7 output doubles, got {fake.n_byref}"
    assert status == _FakeDll.NO_ERR
    assert len(values) == 7
    assert values == [pytest.approx(v) for v in _INJECTED]
