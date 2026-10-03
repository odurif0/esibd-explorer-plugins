"""Pure V/I/P encoding for the matched ESI DLL; no instrument access.

The native setters round to nearest. Prepare a representable value no higher
than the approved ceiling and fresh hardware maximum before issuing a setter.
This does not validate its return or independent readback; callers must do both.
Temperature encoding is separate. Operation order below is intentional.
"""
import math


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def native_value(code, power=False):
    # Exact operation order of this matched DLL's limit getter/setter echoes.
    value = code / 1e6 * 1024.
    return value / 1e6 * 1024. * 1024. if power else value


def native_code(value, power=False):
    value = value * 1e6 * (1. / 1024.)
    if power:
        value = value * 1e6 * (1. / 1024.) * (1. / 1024.)
    return int(value + .5)


def bounded_limit(cap, power=False):
    if not finite(cap) or cap <= 0:
        raise ValueError('Missing positive hardware limit')
    code = native_code(cap, power)
    while code > 0 and native_value(code, power) > cap:
        code -= 1
    value = native_value(code, power)
    if code <= 0 or native_code(value, power) != code or not 0 < value <= cap:
        raise ValueError('No positive, verified native code inside the envelope')
    return value
