# CGC PSU

Python driver for the CGC `PSU-CTRL-2D` unit.

## Design

This driver follows a configuration-first workflow:

1. connect to the device
2. load a known standby configuration
3. verify the standby state with targeted enable-state checks
4. optionally load the operating configuration
5. optionally adjust voltages or current limits

This matches the vendor recommendation for reproducible operation.

## Recommended API

For normal application code:

- construct the driver with `PSU(..., com=..., port=...)`
- prefer `initialize(standby_config=..., operating_config=...)` for the routine startup sequence
- or call `connect()` and `load_config(...)` manually when you need step-by-step control
- reserve `get_product_info()` and `collect_housekeeping()` for metadata, deeper diagnostics, or maintenance checks
- use the high-level getters and setters
- use `get_product_info()` for stable metadata
- use `collect_housekeeping()` for a structured runtime snapshot
- finish with `shutdown()`

`load_config(config_number)` applies the configuration stored in the
controller NVM. Depending on how that CGC configuration was saved, it may also
apply device enable, PSU enable, range and output setpoints.

`initialize()` connects to the controller, loads the standby configuration,
checks the device and output enable state, and optionally loads an operating
configuration. By default it guarantees that the standby phase leaves both HV
outputs OFF before it continues. If the saved standby slot brings one output
up, `initialize()` forces both outputs back OFF, verifies the readback, and
only then proceeds or raises.

`shutdown()` drives both channel current and voltage setpoints to `0` before
disabling the outputs and the device.

Do not treat `open_port()` as the normal entry point. It is a low-level DLL
primitive exposed by `psu_base.py`. `connect()` remains the safe transport
entry point when you need a manual workflow.

## What It Does

- Connects to a CGC PSU controller over a Windows COM port
- Uses the vendor `COM-HVPSU2D.dll`
- Lists and loads stored user configurations
- Enables or disables the device and both PSU outputs
- Controls the interlock enable state for output and BNC connectors
- Sets and reads output voltages and currents
- Exposes `get_product_info()` for product, firmware and hardware metadata
- Exposes `collect_housekeeping()` for structured monitoring data

## Files

- `psu_base.py`: low-level DLL wrapper
- `psu.py`: high-level config-centric API
- `vendor/`: vendor-provided DLL and header

## Platform

- Windows only

## Process Isolation

On Windows, the high-level `PSU` client runs the DLL-backed controller in a
dedicated worker process when possible. This keeps a blocked vendor DLL call
from poisoning the main Python process, which is especially important in
notebooks. Advanced injected objects such as an external `logger` or
`thread_lock` fall back to inline mode because they cannot be shared across
process boundaries.

## Timeout Recovery

When an inline DLL call exceeds its timeout, the controller transport is marked
unusable immediately.

- `_transport_poisoned` becomes `True`
- all later driver calls fail immediately and tell you to recreate the instance
- the internal lock is intentionally not released, so the timed-out transport
  cannot be reused unsafely
- the blocked daemon thread may continue running in the background

Recovery is explicit: create a new `PSU(...)` instance. On Windows process
isolation, the blocked DLL call is confined to the worker process, which is
terminated after an RPC timeout.

## Notebook

- Manual notebook: [`docs/notebooks/cgc/psu_wrapper.ipynb`](../../../docs/notebooks/cgc/psu_wrapper.ipynb)

## Minimal Example

```python
from cgc.psu import PSU

OPERATING_CONFIG = 2  # Example only: replace with your saved operating config.

psu = PSU("psu_main", com=6, port=0)
startup_state = psu.initialize(
    standby_config=1,
    operating_config=OPERATING_CONFIG,
)
print(startup_state)
# If standby slot 1 briefly leaves an HV output enabled, initialize() forces
# both outputs back OFF before it continues to the operating slot.
try:
    psu.set_channel_voltage(0, 25.0)
    psu.set_channel_current(0, 0.5)
finally:
    psu.shutdown()
```

## Low-Level Primitives

Low-level communication primitives are implemented in `psu_base.py`. They are
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
- `get_adc_housekeeping(psu_no)`
- `get_psu_housekeeping(psu_no)`
- `get_psu_data(psu_no)`
- `get_psu_state()`

Enable and outputs:

- `get_device_enable()`
- `set_device_enable(enable)`
- `get_interlock_enable()`
- `set_interlock_enable(connector_output, connector_bnc)`
- `get_psu_enable()`
- `set_psu_enable(psu0, psu1)`
- `has_psu_full_range()`
- `get_psu_full_range()`
- `set_psu_full_range(psu0, psu1)`

Voltage and current:

- `set_psu_output_voltage(psu_no, voltage)`
- `get_psu_output_voltage(psu_no)`
- `get_psu_set_output_voltage(psu_no)`
- `set_psu_output_current(psu_no, current)`
- `get_psu_output_current(psu_no)`
- `get_psu_set_output_current(psu_no)`

Configurations:

- `reset_current_config()`
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
