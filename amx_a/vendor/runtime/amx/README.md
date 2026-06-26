# CGC AMX

Python driver for the CGC `AMX-CTRL-4ED` power switch unit.

## Design

This driver follows the vendor recommendation:

1. connect to the device
2. if available, load a validated standby configuration first
3. confirm that the device stays disabled in that standby state
4. load the operating configuration
5. adjust only frequency, duty cycle or delays at runtime

## Recommended API

For normal application code:

- construct the driver with `AMX(..., com=..., port=...)`
- prefer `initialize(standby_config=..., operating_config=...)` when you have a known safe disabled AMX config
- or use `initialize(operating_config=...)` when you only want a routine connect-and-load sequence
- keep `connect()` and `load_config(...)` for step-by-step control
- use the high-level frequency, pulser and switch helpers
- reserve `get_product_info()` and `collect_housekeeping()` for metadata, troubleshooting, or maintenance checks
- finish with `shutdown()`

`load_config(config_number)` applies the configuration stored in the controller
NVM. Depending on how that CGC configuration was saved, it may also apply
device enable and active timing or switching settings.

`initialize()` always establishes the transport with `connect()`. If
`standby_config` is provided, it loads that configuration first, reads back the
device enable state, and by default refuses to continue if that standby config
leaves the AMX enabled. `operating_config` remains optional because the driver
does not assume a universal standby slot number or content for every AMX.

## Configuration Slots Are Controller-Specific

AMX config slots are not universal. The valid indices and their meanings depend
on what is stored in the controller NVM on that particular unit.

- Always inspect `list_configs()` before choosing a slot.
- Treat `standby_config` and `operating_config` as optional inputs.
- `initialize()` without configs is valid and simply establishes the transport.
- Do not assume that slot `40` exists on every AMX. On the controller tested
  on April 14, 2026, slot `40` was not valid and `load_current_config(40)`
  failed with vendor status `-10`.

Do not treat `open_port()` as the normal entry point. It is a low-level DLL
primitive exposed by `amx_base.py`. `connect()` remains the safe transport
entry point when you need a manual workflow.

## What It Does

- Connects to a CGC AMX controller over a Windows COM port
- Uses the vendor `COM-HVAMX4ED.dll`
- Lists and loads stored user configurations
- Enables or disables the device
- Adjusts oscillator frequency
- Adjusts pulser duty cycle, width and delay
- Adjusts coarse switch trigger and enable delays
- Exposes `get_product_info()` for product, firmware and hardware metadata
- Exposes `collect_housekeeping()` for structured monitoring data

## Files

- `amx_base.py`: low-level DLL wrapper
- `amx.py`: high-level config-centric API
- `vendor/`: vendor-provided DLL and header

## Platform

- Windows only

## Process Isolation

The high-level `AMX` client now defaults to the inline controller path. This
avoids slow worker startup failures on systems where process isolation is not
stable. Process isolation remains available explicitly through
`AMX(..., process_backend=True)` when you want DLL calls isolated in a worker
process. Advanced injected objects such as an external `logger` or
`thread_lock` still force inline mode because they cannot be shared across
process boundaries.

## Timeout Recovery

When an inline DLL call exceeds its timeout, the controller transport is marked
unusable immediately.

- `_transport_poisoned` becomes `True`
- all later driver calls fail immediately and tell you to recreate the instance
- the internal lock is intentionally not released, so the timed-out transport
  cannot be reused unsafely
- the blocked daemon thread may continue running in the background

Recovery is explicit: create a new `AMX(...)` instance. On Windows process
isolation, the blocked DLL call is confined to the worker process, which is
terminated after an RPC timeout.

## Notebook

- Manual notebook: [`docs/notebooks/cgc/amx_wrapper.ipynb`](../../../docs/notebooks/cgc/amx_wrapper.ipynb)

## Minimal Example

```python
from cgc.amx import AMX

STANDBY_CONFIG = None  # Optional: replace with a validated disabled config.
OPERATING_CONFIG = None  # Optional: choose a validated index from list_configs().

amx = AMX("amx_main", com=8, port=0)
startup_kwargs = {}
if OPERATING_CONFIG is not None:
    startup_kwargs["operating_config"] = OPERATING_CONFIG
if STANDBY_CONFIG is not None:
    startup_kwargs["standby_config"] = STANDBY_CONFIG

startup_state = amx.initialize(**startup_kwargs)
print(startup_state)
try:
    amx.set_frequency_hz(2_000.0)
    amx.set_pulser_duty_cycle(0, 0.5)
finally:
    amx.shutdown()
```

## Low-Level Primitives

Low-level communication primitives are implemented in `amx_base.py`. They are
useful for debugging or vendor-level investigations, but they are not the
recommended application API.

Transport:

- `open_port(com_number, port_number=None)`
- `close_port()`
- `set_baud_rate(baud_rate)`
- `purge()`
- `device_purge()`
- `get_buffer_state()`

Device state and housekeeping:

- `get_main_state()`
- `get_device_state()`
- `get_housekeeping()`
- `get_sensor_data()`
- `get_fan_data()`
- `get_led_data()`
- `get_controller_state()`

Enable and timing:

- `get_device_enable()`
- `set_device_enable(enable)`
- `get_oscillator_period()`
- `set_oscillator_period(period)`
- `get_pulser_delay(pulser_no)`
- `set_pulser_delay(pulser_no, delay)`
- `get_pulser_width(pulser_no)`
- `set_pulser_width(pulser_no, width)`
- `get_pulser_burst(pulser_no)`
- `get_switch_trigger_config(switch_no)`
- `get_switch_enable_config(switch_no)`
- `get_switch_trigger_delay(switch_no)`
- `set_switch_trigger_delay(switch_no, rise_delay, fall_delay)`
- `get_switch_enable_delay(switch_no)`
- `set_switch_enable_delay(switch_no, delay)`

Configurations:

- `save_current_config(config_number)`
- `load_current_config(config_number)`
- `get_config_name(config_number)`
- `get_config_flags(config_number)`
- `get_config_list()`

Device information:

- `get_cpu_data()`
- `get_uptime()`
- `get_total_time()`
- `get_hw_type()`
- `get_hw_version()`
- `get_fw_version()`
- `get_fw_date()`
- `get_product_id()`
- `get_product_no()`
