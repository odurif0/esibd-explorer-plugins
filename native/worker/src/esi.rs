//! ESI high-level facade. Native calls stay serialized in the worker; killing a
//! blocked DLL and retiring its replies is the supervisor's responsibility.

use crate::Backend;
use crate::codec::{float, tuple};
use crate::context::Context;
use crate::error::{Error, Result};
use crate::ffi::{Dll, NativeReply};
use serde_json::{Map, Value, json};
use std::collections::BTreeMap;
use std::time::{Duration, Instant};

const DEVICE_TYPE: u64 = 0x8ed6;
const HEAT_TYPE: u64 = 0xdb1c;
const HV_TYPE: u64 = 0x0a0d;
const HV_ADDRESSES: [u64; 2] = [1, 2];
const MODULE_COUNT: usize = 4;
const MODULE_ARRAY_SIZE: usize = MODULE_COUNT + 1;
const CONFIG_SIZE: usize = 53;
const CONFIG_COUNT: usize = 1023;
const CONFIG_NAME_SIZE: usize = 202;
const MAX_VOLTAGE: f64 = 3000.0;
const DEFAULT_STEP: f64 = 10.008;
const CTRL_ACTIVE: u64 = 0x0100;
const MOD_ACTIVE: u64 = 0x4000;
const DEV_ACTIVE: u64 = 0x8000;
const ACTIVE_BITS: u64 = CTRL_ACTIVE | MOD_ACTIVE | DEV_ACTIVE;
const VOLTAGE_READY: u64 = 0x10;
const CURRENT_READY: u64 = 0x40;
const HEAT_READY: u64 = 1;
const POLL_INTERVAL: Duration = Duration::from_millis(100);

pub const SUPPORTED_METHODS: &[&str] = &[
    "connect",
    "force_safe_off",
    "discover_modules",
    "collect_identity",
    "configure_hv_max_voltage_steps",
    "list_configs",
    "load_config",
    "save_config",
    "set_hv_module_target",
    "set_target_voltage",
    "select_hv_measurement",
    "select_hv_voltage_adc",
    "set_global_active",
    "get_hv_module_active",
    "set_hv_module_active",
    "set_output_active",
    "get_heat_configuration",
    "configure_heat_limits",
    "set_heater_temperature",
    "collect_diagnostics",
    "disconnect",
    "force_close_transport",
    "describe_error",
    "format_status",
];

pub struct Controller {
    dll: Box<dyn Dll>,
    device_id: String,
    com: u64,
    baudrate: u64,
    dll_path: Value,
    dll_sha256: Value,
    connected: bool,
    port_claimed: bool,
    opening: bool,
    open_failed: bool,
    failed_open_released: bool,
    transport_poisoned: bool,
    transport_error: Option<String>,
    communication_error: Option<String>,
    inventory: BTreeMap<u64, Value>,
    measurement_requests: BTreeMap<u64, (bool, bool)>,
    io_timeout: Duration,
    default_timeout: Duration,
    discharge_timeout: Duration,
    discharge_limit: f64,
    discharge_samples: usize,
    discharge_deadline: Option<Instant>,
    error_codes: Value,
}

impl Controller {
    pub fn new(config: &Value, dll: Box<dyn Dll>) -> Result<Self> {
        let config = config
            .as_object()
            .ok_or_else(|| Error::argument("ESI config must be an object"))?;
        for key in config.keys() {
            if ![
                "device_id",
                "com",
                "baudrate",
                "dll_path",
                "dll_sha256",
                "log_dir",
                "logger",
                "thread_lock",
            ]
            .contains(&key.as_str())
            {
                return Err(Error::new(
                    "TypeError",
                    format!("Unexpected ESI init kwarg: {key}"),
                ));
            }
        }
        for name in ["logger", "thread_lock"] {
            if config.get(name).is_some_and(|value| !value.is_null()) {
                return Err(Error::argument(format!(
                    "A native ESI worker cannot share a {name}"
                )));
            }
        }
        let device_id = config
            .get("device_id")
            .and_then(Value::as_str)
            .filter(|name| !name.trim().is_empty())
            .ok_or_else(|| Error::argument("ESI device_id must be a non-empty string"))?
            .to_owned();
        let com = config
            .get("com")
            .and_then(Value::as_u64)
            .filter(|com| (1..=255).contains(com))
            .ok_or_else(|| Error::argument("ESI com must be an integer between 1 and 255"))?;
        let baudrate = config
            .get("baudrate")
            .unwrap_or(&json!(230400))
            .as_u64()
            .filter(|rate| *rate > 0 && *rate <= u32::MAX as u64)
            .ok_or_else(|| Error::argument("ESI baudrate must be a positive 32-bit integer"))?;
        let dll_path = config.get("dll_path").cloned().unwrap_or(Value::Null);
        if !dll_path.is_null() && !dll_path.is_string() {
            return Err(Error::argument("ESI dll_path must be a string"));
        }
        let dll_sha256 = config.get("dll_sha256").cloned().unwrap_or(Value::Null);
        if !dll_sha256.is_null()
            && !dll_sha256.as_str().is_some_and(|hash| {
                hash.len() == 64 && hash.bytes().all(|byte| byte.is_ascii_hexdigit())
            })
        {
            return Err(Error::argument(
                "ESI dll_sha256 must contain 64 hexadecimal digits",
            ));
        }
        Ok(Self {
            dll,
            device_id,
            com,
            baudrate,
            dll_path,
            dll_sha256,
            connected: false,
            port_claimed: false,
            opening: false,
            open_failed: false,
            failed_open_released: false,
            transport_poisoned: false,
            transport_error: None,
            communication_error: None,
            inventory: BTreeMap::new(),
            measurement_requests: BTreeMap::new(),
            io_timeout: Duration::from_secs(5),
            default_timeout: Duration::from_secs(5),
            discharge_timeout: Duration::from_secs(60),
            discharge_limit: 1.0,
            discharge_samples: 3,
            discharge_deadline: None,
            error_codes: serde_json::from_str(crate::ERROR_CATALOG)
                .map_err(|error| Error::runtime(format!("Invalid ESI error catalog: {error}")))?,
        })
    }

    fn require_connected(&self) -> Result<()> {
        if !self.connected {
            return Err(Error::runtime("ESI device is not connected"));
        }
        self.require_transport()
    }

    fn require_transport(&self) -> Result<()> {
        if self.transport_poisoned {
            return Err(Error::runtime(format!(
                "ESI transport is poisoned: {}",
                self.transport_error
                    .as_deref()
                    .unwrap_or("native call did not complete in time")
            )));
        }
        Ok(())
    }

    fn permission(&self, ctx: &Context, cancellable: bool) -> Result<()> {
        self.require_transport()?;
        if ctx.remaining().is_zero() {
            return Err(Error::new("TimeoutError", "ESI operation deadline expired"));
        }
        if cancellable && ctx.is_cancelled() {
            return Err(Error::new(
                "InterruptedError",
                "ESI output operation cancelled",
            ));
        }
        Ok(())
    }

    fn poison(&mut self, message: String) -> Error {
        self.transport_poisoned = true;
        self.transport_error = Some(message.clone());
        Error::new("TimeoutError", message)
    }

    fn native(
        &mut self,
        symbol: &str,
        args: &[Value],
        ctx: &Context,
        cancellable: bool,
    ) -> Result<NativeReply> {
        self.permission(ctx, cancellable)?;
        self.check_discharge_deadline()?;
        let start = Instant::now();
        let reply = ctx.native_call(symbol, Some(self.io_timeout), || {
            self.dll.call(symbol, args)
        });
        // Never continue a transaction or perform cleanup after a late native reply.
        if start.elapsed() >= self.io_timeout || ctx.remaining().is_zero() {
            return Err(self.poison(format!(
                "ESI {symbol} returned after the native I/O deadline; output state is unconfirmed"
            )));
        }
        let reply = reply.inspect_err(|error| {
            if error.kind == "TimeoutError" {
                self.transport_poisoned = true;
                self.transport_error = Some(error.message.clone());
            }
        })?;
        self.permission(ctx, cancellable)?;
        self.check_discharge_deadline()?;
        Ok(reply)
    }

    fn check_discharge_deadline(&self) -> Result<()> {
        if self
            .discharge_deadline
            .is_some_and(|deadline| Instant::now() >= deadline)
        {
            return Err(Error::runtime(
                "ESI discharge unconfirmed: ADC response or operation arrived after the discharge deadline",
            ));
        }
        Ok(())
    }

    fn check_status(&mut self, status: i64, symbol: &str) -> Result<()> {
        if status == 0 {
            return Ok(());
        }
        let message = format!("ESI {symbol} failed: {}", self.format_status(status));
        if ((-14..=-7).contains(&status) || status == -100) && symbol != "COM_ESI_CTRL_Close" {
            self.communication_error = Some(message.clone());
        }
        Err(Error::runtime(message))
    }

    fn read(
        &mut self,
        symbol: &str,
        args: &[Value],
        count: usize,
        ctx: &Context,
        cancellable: bool,
    ) -> Result<Vec<Value>> {
        let reply = self.native(symbol, args, ctx, cancellable)?;
        self.check_status(reply.status, symbol)?;
        if reply.values.len() != count {
            return Err(Error::runtime(format!(
                "ESI {symbol} returned {} pointer values; expected {count}",
                reply.values.len()
            )));
        }
        Ok(reply.values)
    }

    fn write(
        &mut self,
        symbol: &str,
        args: &[Value],
        ctx: &Context,
        cancellable: bool,
    ) -> Result<()> {
        self.read(symbol, args, 0, ctx, cancellable).map(|_| ())
    }

    fn scalar(
        &mut self,
        symbol: &str,
        initial: Value,
        ctx: &Context,
        cancellable: bool,
    ) -> Result<Value> {
        Ok(self
            .read(symbol, &[initial], 1, ctx, cancellable)?
            .remove(0))
    }

    fn describe_error(&self, status: i64) -> &str {
        self.error_codes
            .get(status.to_string())
            .and_then(Value::as_str)
            .unwrap_or("Unknown status code")
    }

    fn format_status(&self, status: i64) -> String {
        format!("{status} ({})", self.describe_error(status))
    }

    fn recover(&mut self, ctx: &Context, cancellable: bool) -> Result<()> {
        if self.communication_error.is_some() {
            self.write("COM_ESI_CTRL_Purge", &[], ctx, cancellable)?;
            self.communication_error = None;
        }
        Ok(())
    }

    fn global_active(&mut self, active: bool, ctx: &Context) -> Result<bool> {
        self.write("COM_ESI_CTRL_SetEnable", &[json!(active)], ctx, active)?;
        let enabled =
            boolean(&self.scalar("COM_ESI_CTRL_GetEnable", json!(false), ctx, active)?)?;
        if enabled != active {
            return Err(Error::runtime(format!(
                "ESI global output enable verification failed: requested {active}, controller reports {enabled}"
            )));
        }
        Ok(enabled)
    }

    fn pwm(&mut self, address: u64, ctx: &Context, cancellable: bool) -> Result<Vec<Value>> {
        let values = self.read(
            "COM_ESI_CTRL_GetHVsupplyParamsPWM",
            &pwm_args(address),
            8,
            ctx,
            cancellable,
        )?;
        validate_pwm(&values)?;
        Ok(values)
    }

    fn hv_active(&mut self, address: u64, active: bool, ctx: &Context) -> Result<bool> {
        self.write(
            "COM_ESI_CTRL_SetModuleActivationState",
            &[json!(address), json!(active)],
            ctx,
            active,
        )?;
        // Only retry the read, never an activation command. The direct HV getter
        // is unreliable on this DLL; the converter gate lives in PWM status.
        let args = pwm_args(address);
        let mut reply = self.native("COM_ESI_CTRL_GetHVsupplyParamsPWM", &args, ctx, active)?;
        if reply.status != 0 {
            reply = self.native("COM_ESI_CTRL_GetHVsupplyParamsPWM", &args, ctx, active)?;
        }
        self.check_status(reply.status, "COM_ESI_CTRL_GetHVsupplyParamsPWM")?;
        validate_pwm(&reply.values)?;
        let observed = boolean(&reply.values[6])?;
        if observed != active {
            return Err(Error::runtime(format!(
                "ESI module {address} activation verification failed: requested {active}, PWM status reports {observed}"
            )));
        }
        Ok(observed)
    }

    fn heat_active(&mut self, active: bool, ctx: &Context) -> Result<bool> {
        self.write(
            "COM_ESI_CTRL_SetModuleActivationState",
            &[json!(0), json!(active)],
            ctx,
            active,
        )?;
        let values = self.read(
            "COM_ESI_CTRL_GetModuleActivationState",
            &[json!(0), json!(false)],
            1,
            ctx,
            active,
        )?;
        let observed = boolean(&values[0])?;
        if observed != active {
            return Err(Error::runtime(format!(
                "ESI heater module 0 activation verification failed: requested {active}, controller reports {observed}"
            )));
        }
        Ok(observed)
    }

    fn hv_target(&mut self, address: u64, target: f64, ctx: &Context) -> Result<f64> {
        self.write(
            "COM_ESI_CTRL_SetHVsupplyTargetOutputVoltage",
            &[json!(address), json!(target)],
            ctx,
            target > 0.0,
        )?;
        let values = self.read(
            "COM_ESI_CTRL_GetHVsupplyTargetOutputVoltage",
            &[json!(address), json!(0.0)],
            1,
            ctx,
            target > 0.0,
        )?;
        let observed = number(&values[0])?;
        if !close(observed, target) {
            return Err(Error::runtime(format!(
                "ESI module {address} target verification failed: requested {target} V, controller reports {observed} V"
            )));
        }
        Ok(observed)
    }

    fn heat_target(&mut self, target: f64, maximum: f64, ctx: &Context) -> Result<f64> {
        let applied = number(&self.scalar(
            "COM_ESI_CTRL_SetHeatCtrlHeaterTemperature",
            json!(target),
            ctx,
            target > 0.0,
        )?)?;
        if !applied.is_finite() || !(0.0..=maximum).contains(&applied) {
            return Err(Error::runtime(
                "ESI heater applied temperature is outside the allowed range",
            ));
        }
        let observed = number(&self.scalar(
            "COM_ESI_CTRL_GetHeatCtrlHeaterTemperature",
            json!(0.0),
            ctx,
            target > 0.0,
        )?)?;
        if !close(observed, applied) {
            return Err(Error::runtime(format!(
                "ESI heater temperature verification failed: applied {applied} degC, controller reports {observed} degC"
            )));
        }
        Ok(observed)
    }

    fn disable_heat(&mut self, ctx: &Context) -> Result<Vec<String>> {
        let mut failures = Vec::new();
        if let Err(error) = self.heat_active(false, ctx) {
            failures.push(error.to_string());
        }
        self.permission(ctx, false)?;
        if let Err(error) = self.heat_target(0.0, 0.0, ctx) {
            failures.push(error.to_string());
        }
        self.permission(ctx, false)?;
        Ok(failures)
    }

    fn safe_off(&mut self, ctx: &Context) -> Result<()> {
        let mut failures = self.disable_heat(ctx)?;
        for address in HV_ADDRESSES {
            if let Err(error) = self.write(
                "COM_ESI_CTRL_SetHVsupplyTargetOutputVoltage",
                &[json!(address), json!(0.0)],
                ctx,
                false,
            ) {
                failures.push(error.to_string());
            }
            self.permission(ctx, false)?;
            if let Err(error) = self.hv_active(address, false, ctx) {
                failures.push(error.to_string());
            }
            self.permission(ctx, false)?;
        }
        let gate = (|| {
            self.write("COM_ESI_CTRL_SetEnable", &[json!(false)], ctx, false)?;
            let mut enabled =
                boolean(&self.scalar("COM_ESI_CTRL_GetEnable", json!(false), ctx, false)?)?;
            // Retry OFF only after a successful acknowledgement and an ON
            // readback, not after a failed command or a failed status read.
            if enabled {
                self.write("COM_ESI_CTRL_SetEnable", &[json!(false)], ctx, false)?;
                enabled =
                    boolean(&self.scalar("COM_ESI_CTRL_GetEnable", json!(false), ctx, false)?)?;
            }
            if enabled {
                return Err(Error::runtime(
                    "ESI global enable remained ON after disable; outputs may be live",
                ));
            }
            Ok(())
        })();
        if let Err(error) = gate {
            failures.push(error.to_string());
        }
        self.permission(ctx, false)?;
        if failures.is_empty() {
            Ok(())
        } else {
            Err(Error::runtime(format!(
                "ESI safe OFF failed: {}",
                failures.join("; ")
            )))
        }
    }

    fn connect(&mut self, preserve_outputs: bool, ctx: &Context) -> Result<bool> {
        self.permission(ctx, true)?;
        if self.connected {
            return Ok(true);
        }
        if self.port_claimed {
            return Err(Error::runtime(
                "ESI port is still owned after an incomplete connection; disconnect or force-close it before reconnecting",
            ));
        }
        self.measurement_requests.clear();
        self.opening = true;
        self.open_failed = false;
        self.failed_open_released = false;
        let result = (|| {
            self.port_claimed = true;
            self.write("COM_ESI_CTRL_Open", &[json!(self.com)], ctx, true)?;
            self.recover(ctx, true)?;
            let baud = self.scalar("COM_ESI_CTRL_SetBaudRate", json!(self.baudrate), ctx, true)?;
            unsigned(&baud)?;
            let device_type =
                unsigned(&self.scalar("COM_ESI_CTRL_GetDevType", json!(0), ctx, true)?)?;
            if device_type != DEVICE_TYPE {
                return Err(Error::runtime(format!(
                    "ESI device type mismatch: expected 0x{DEVICE_TYPE:04X}, got 0x{device_type:04X}"
                )));
            }
            self.connected = true;
            if !preserve_outputs {
                self.global_active(false, ctx)?;
                self.heat_active(false, ctx)?;
                for address in HV_ADDRESSES {
                    self.hv_active(address, false, ctx)?;
                }
                self.global_active(true, ctx)?;
                self.safe_off(ctx)?;
            }
            self.discover(ctx)?;
            Ok(true)
        })();
        self.opening = false;
        if let Err(error) = &result {
            self.open_failed = true;
            // After identity validation, retain ownership unless safe OFF and
            // fresh discharge are proven. Before identity, issue only local Close.
            if self.port_claimed
                && !self.transport_poisoned
                && !ctx.remaining().is_zero()
                && !preserve_outputs
            {
                let cleanup = self.disconnect(ctx);
                if let Err(cleanup) = cleanup {
                    return Err(Error::runtime(format!(
                        "{error}; ESI connect cleanup unconfirmed: {cleanup}"
                    )));
                }
                self.failed_open_released = true;
            }
        }
        result
    }

    fn discover(&mut self, ctx: &Context) -> Result<Value> {
        self.write("COM_ESI_CTRL_UpdateModulePresence", &[], ctx, true)?;
        let presence = self.read(
            "COM_ESI_CTRL_GetModulePresence",
            &[json!(false), json!(0), json!(vec![0; MODULE_ARRAY_SIZE])],
            3,
            ctx,
            true,
        )?;
        if !boolean(&presence[0])? {
            return Err(Error::runtime("ESI module inventory is not valid"));
        }
        unsigned(&presence[1])?;
        let flags = unsigned_array(&presence[2], MODULE_ARRAY_SIZE, 255)?;
        let mut modules = BTreeMap::new();
        for (address, flag) in flags[..MODULE_COUNT].iter().enumerate() {
            if *flag == 0 {
                continue;
            }
            let mut info = json!({"address": address, "presence": flag});
            if *flag == 1 {
                let values = self.read(
                    "COM_ESI_CTRL_GetModuleDevType",
                    &[json!(address), json!(0)],
                    1,
                    ctx,
                    true,
                )?;
                info["device_type"] = json!(unsigned(&values[0])?);
            }
            modules.insert(address as u64, info);
        }
        for address in [0, 1, 2] {
            let expected = if address == 0 { HEAT_TYPE } else { HV_TYPE };
            let info = modules.get(&address).ok_or_else(|| {
                Error::runtime(format!(
                    "Required ESI {} module {address} was not detected",
                    if address == 0 { "heat" } else { "HV" }
                ))
            })?;
            if info["presence"] != json!(1) {
                return Err(Error::runtime(format!(
                    "ESI module {address} is present but invalid"
                )));
            }
            if info["device_type"] != json!(expected) {
                return Err(Error::runtime(format!(
                    "ESI module {address} type mismatch: expected 0x{expected:04X}, got {}",
                    info["device_type"]
                )));
            }
        }
        if modules
            .get(&3)
            .is_some_and(|info| info["device_type"] == json!(HV_TYPE))
        {
            return Err(Error::runtime(
                "Unexpected HV module at address 3; refusing uncontrolled HV outputs",
            ));
        }
        self.inventory = modules;
        Ok(integer_map(&self.inventory))
    }

    fn heat_configuration(&mut self, ctx: &Context, cancellable: bool) -> Result<Value> {
        let maxima = self.heat_maxima(ctx, cancellable)?;
        let voltage = self.scalar(
            "COM_ESI_CTRL_GetHeatCtrlVoltageLimit",
            json!(0.0),
            ctx,
            cancellable,
        )?;
        let current = self.scalar(
            "COM_ESI_CTRL_GetHeatCtrlCurrentLimit",
            json!(0.0),
            ctx,
            cancellable,
        )?;
        let power = self.scalar(
            "COM_ESI_CTRL_GetHeatCtrlPowerLimit",
            json!(0.0),
            ctx,
            cancellable,
        )?;
        let target = self.scalar(
            "COM_ESI_CTRL_GetHeatCtrlHeaterTemperature",
            json!(0.0),
            ctx,
            cancellable,
        )?;
        Ok(json!({
            "hardware_limits": {"max_voltage_v": float(maxima[0]), "max_current_a": float(maxima[1]), "max_power_w": float(maxima[2]), "max_temperature_c": float(maxima[3])},
            "voltage_limit_v": float(number(&voltage)?), "current_limit_a": float(number(&current)?),
            "power_limit_w": float(number(&power)?), "target_temperature_c": float(number(&target)?),
        }))
    }

    fn heat_maxima(&mut self, ctx: &Context, cancellable: bool) -> Result<[f64; 4]> {
        let values = self.read(
            "COM_ESI_CTRL_GetHeatCtrlHwLimits",
            &[json!(0.0), json!(0.0), json!(0.0), json!(0.0)],
            4,
            ctx,
            cancellable,
        )?;
        Ok([
            number(&values[0])?,
            number(&values[1])?,
            number(&values[2])?,
            number(&values[3])?,
        ])
    }

    fn wait(
        &self,
        deadline: Instant,
        ctx: &Context,
        cancellable: bool,
        message: &str,
    ) -> Result<()> {
        self.permission(ctx, cancellable)?;
        if Instant::now() >= deadline {
            return Err(Error::runtime(message));
        }
        let delay = POLL_INTERVAL
            .min(deadline.saturating_duration_since(Instant::now()))
            .min(ctx.remaining());
        if cancellable {
            ctx.sleep(delay).map_err(|error| {
                if ctx.is_cancelled() {
                    Error::new("InterruptedError", "ESI output operation cancelled")
                } else {
                    error
                }
            })?;
        } else {
            std::thread::sleep(delay);
        }
        self.permission(ctx, cancellable)
    }

    fn heat_monitoring(&mut self, timeout: Duration, ctx: &Context) -> Result<Vec<Value>> {
        let deadline = Instant::now() + timeout.min(ctx.remaining());
        loop {
            self.permission(ctx, true)?;
            if Instant::now() >= deadline {
                return Err(Error::runtime(
                    "ESI heater monitoring data not ready within the I/O timeout",
                ));
            }
            let flags = self.read(
                "COM_ESI_CTRL_GetModuleDataReadyFlags",
                &[json!(0), json!(0)],
                1,
                ctx,
                true,
            )?;
            if Instant::now() >= deadline {
                return Err(Error::runtime(
                    "ESI heater monitoring data not ready within the I/O timeout",
                ));
            }
            if unsigned(&flags[0])? & HEAT_READY != 0 {
                let values = self.read(
                    "COM_ESI_CTRL_GetHeatCtrlMonitoring",
                    &[json!(false), json!(0.0), json!(0.0), json!(0.0), json!(0.0)],
                    5,
                    ctx,
                    true,
                )?;
                boolean(&values[0])?;
                for value in &values[1..] {
                    number(value)?;
                }
                return Ok(values);
            }
            self.wait(
                deadline,
                ctx,
                true,
                "ESI heater monitoring data not ready within the I/O timeout",
            )?;
        }
    }

    fn validate_heat(&mut self, target: Option<f64>, ctx: &Context) -> Result<f64> {
        let configuration = self.heat_configuration(ctx, true)?;
        for (name, unit) in [("voltage", "v"), ("current", "a"), ("power", "w")] {
            let value = number(&configuration[format!("{name}_limit_{unit}")])?;
            let maximum = number(&configuration["hardware_limits"][format!("max_{name}_{unit}")])?;
            if !value.is_finite() || !maximum.is_finite() || value <= 0.0 || value > maximum {
                return Err(Error::runtime(format!(
                    "ESI heater {name} limit is invalid; configure a positive limit no greater than {maximum} before ON. A plugin setting of zero retains the device setting"
                )));
            }
        }
        let maximum = number(&configuration["hardware_limits"]["max_temperature_c"])?;
        let target = target.unwrap_or(number(&configuration["target_temperature_c"])?);
        if !maximum.is_finite()
            || maximum <= 0.0
            || !target.is_finite()
            || !(0.0..=maximum).contains(&target)
        {
            return Err(Error::runtime(
                "ESI heater temperature target or hardware limit is invalid",
            ));
        }
        let monitoring = self.heat_monitoring(self.io_timeout, ctx)?;
        let temperature = number(&monitoring[4])?;
        if !boolean(&monitoring[0])?
            || !temperature.is_finite()
            || !(0.0..=maximum).contains(&temperature)
        {
            return Err(Error::runtime(
                "ESI heater temperature sensor readback is invalid; ON refused",
            ));
        }
        Ok(maximum)
    }

    fn complete_state(&mut self, ctx: &Context, cancellable: bool) -> Result<Vec<Value>> {
        let values = self.read(
            "COM_ESI_CTRL_GetCompleteState",
            &[
                json!(0),
                json!(0),
                json!(0),
                json!(0),
                json!(0),
                json!(0),
                json!(0),
                json!(vec![0; MODULE_ARRAY_SIZE]),
                json!(vec![0; MODULE_ARRAY_SIZE]),
            ],
            9,
            ctx,
            cancellable,
        )?;
        for value in &values[..7] {
            unsigned(value)?;
        }
        unsigned_array(&values[7], MODULE_ARRAY_SIZE, 255)?;
        unsigned_array(&values[8], MODULE_ARRAY_SIZE, 65535)?;
        Ok(values)
    }

    fn confirm_heat(&mut self, ctx: &Context) -> Result<bool> {
        let deadline = Instant::now() + self.io_timeout.min(ctx.remaining());
        let mut last_state = None;
        loop {
            self.permission(ctx, true)?;
            if Instant::now() >= deadline {
                return Err(Error::runtime(format!(
                    "ESI heater activation not confirmed within the I/O timeout; last module state={}",
                    last_state
                        .map_or_else(|| "unavailable".to_owned(), |state| format!("0x{state:x}"))
                )));
            }
            let values = self.complete_state(ctx, true)?;
            let state = unsigned(&values[8][0])?;
            last_state = Some(state);
            let device = unsigned(&values[1])?;
            let main = unsigned(&values[6])?;
            if device != 0 || main != 0 {
                return Err(Error::runtime(format!(
                    "ESI heater activation blocked by controller fault/state: device=0x{device:x}, main=0x{main:x}, module=0x{state:x}"
                )));
            }
            if Instant::now() >= deadline {
                continue;
            }
            let enabled =
                boolean(&self.scalar("COM_ESI_CTRL_GetEnable", json!(false), ctx, true)?)?;
            if Instant::now() >= deadline {
                continue;
            }
            let module = self.read(
                "COM_ESI_CTRL_GetModuleActivationState",
                &[json!(0), json!(false)],
                1,
                ctx,
                true,
            )?;
            if !enabled || !boolean(&module[0])? {
                return Err(Error::runtime(
                    "ESI heater activation command readback lost",
                ));
            }
            if Instant::now() >= deadline {
                continue;
            }
            if state & ACTIVE_BITS == ACTIVE_BITS {
                self.permission(ctx, true)?;
                return Ok(true);
            }
            self.wait(
                deadline,
                ctx,
                true,
                "ESI heater activation not confirmed within the I/O timeout",
            )?;
        }
    }

    fn output_active(&mut self, address: u64, active: bool, ctx: &Context) -> Result<bool> {
        if address == 0 {
            if active {
                self.validate_heat(None, ctx)?;
                self.heat_active(true, ctx)?;
                self.global_active(true, ctx)?;
                self.confirm_heat(ctx)
            } else {
                let failures = self.disable_heat(ctx)?;
                if !failures.is_empty() {
                    return Err(Error::runtime(format!(
                        "ESI heater OFF failed: {}",
                        failures.join("; ")
                    )));
                }
                Ok(false)
            }
        } else if active {
            self.hv_active(address, true, ctx)?;
            self.global_active(true, ctx)?;
            self.permission(ctx, true)?;
            Ok(true)
        } else {
            self.hv_target(address, 0.0, ctx)?;
            self.hv_active(address, false, ctx)
        }
    }

    fn ranges(&mut self, address: u64, ctx: &Context, cancellable: bool) -> Result<(bool, bool)> {
        let values = self.read(
            "COM_ESI_CTRL_GetHVsupplyMeasRanges",
            &[json!(address), json!(false), json!(false)],
            2,
            ctx,
            cancellable,
        )?;
        Ok((boolean(&values[0])?, boolean(&values[1])?))
    }

    fn select_ranges(
        &mut self,
        address: u64,
        selection: (bool, bool),
        ctx: &Context,
        cancellable: bool,
    ) -> Result<()> {
        self.write(
            "COM_ESI_CTRL_SetHVsupplyMeasRanges",
            &[json!(address), json!(selection.0), json!(selection.1)],
            ctx,
            cancellable,
        )?;
        let observed = self.ranges(address, ctx, cancellable)?;
        if observed != selection {
            return Err(Error::runtime(format!(
                "ESI module {address} measurement selection verification failed: requested {selection:?}, controller reports {observed:?}"
            )));
        }
        Ok(())
    }

    fn configure_steps(&mut self, step: f64, ctx: &Context) -> Result<Value> {
        if !step.is_finite() || step <= 0.0 || step > MAX_VOLTAGE {
            return Err(Error::argument(
                "ESI HV maximum voltage step must be finite, greater than 0 and no more than 3000 V",
            ));
        }
        let millivolts = (step * 1000.0).round();
        if (millivolts / 1000.0 - step).abs() > 1e-9 {
            return Err(Error::argument(
                "ESI HV maximum voltage step must use millivolt precision",
            ));
        }
        let current = self.read(
            "COM_ESI_CTRL_GetCurrentConfig",
            &[json!(vec![0; CONFIG_SIZE])],
            1,
            ctx,
            true,
        )?;
        let mut requested = unsigned_array(&current[0], CONFIG_SIZE, 255)?
            .into_iter()
            .map(|byte| byte as u8)
            .collect::<Vec<_>>();
        let mut unsafe_state = Vec::new();
        if requested[0] != 0 {
            unsafe_state.push("global enable is ON".to_owned());
        }
        for address in HV_ADDRESSES {
            let offset = 17 + (address as usize - 1) * 12;
            let target = i32::from_le_bytes(requested[offset..offset + 4].try_into().unwrap());
            if target != 0 {
                unsafe_state.push(format!(
                    "module {address} target is {} V",
                    target as f64 / 1000.0
                ));
            }
            if requested[offset + 10] != 0 {
                unsafe_state.push(format!("module {address} gate is enabled"));
            }
        }
        if !unsafe_state.is_empty() {
            return Err(Error::runtime(format!(
                "Refusing volatile HV configuration while outputs are not safely OFF: {}",
                unsafe_state.join("; ")
            )));
        }
        for offset in [21, 33] {
            requested[offset..offset + 4].copy_from_slice(&(millivolts as i32).to_le_bytes());
        }
        if json!(requested) != current[0] {
            // SetCurrentConfig takes one input pointer, so its values vector still
            // contains that pointer even though the high-level result is None.
            self.read(
                "COM_ESI_CTRL_SetCurrentConfig",
                &[json!(requested)],
                1,
                ctx,
                true,
            )?;
        }
        let observed = self.read(
            "COM_ESI_CTRL_GetCurrentConfig",
            &[json!(vec![0; CONFIG_SIZE])],
            1,
            ctx,
            true,
        )?;
        let observed = unsigned_array(&observed[0], CONFIG_SIZE, 255)?;
        let mismatches = requested
            .iter()
            .zip(&observed)
            .enumerate()
            .filter_map(|(index, (expected, actual))| {
                (*expected as u64 != *actual).then_some(index)
            })
            .collect::<Vec<_>>();
        if !mismatches.is_empty() {
            return Err(Error::runtime(format!(
                "ESI volatile HV configuration verification failed at byte offsets {mismatches:?}"
            )));
        }
        if boolean(&self.scalar("COM_ESI_CTRL_GetEnable", json!(false), ctx, true)?)? {
            return Err(Error::runtime(
                "ESI global gate opened during HV configuration",
            ));
        }
        for address in HV_ADDRESSES {
            let target = self.read(
                "COM_ESI_CTRL_GetHVsupplyTargetOutputVoltage",
                &[json!(address), json!(0.0)],
                1,
                ctx,
                true,
            )?;
            let pwm = self.pwm(address, ctx, true)?;
            if number(&target[0])? != 0.0 || boolean(&pwm[6])? {
                return Err(Error::runtime(format!(
                    "ESI module {address} left safe OFF during HV configuration"
                )));
            }
        }
        Ok(integer_map(
            &HV_ADDRESSES
                .into_iter()
                .map(|address| (address, float(millivolts / 1000.0)))
                .collect(),
        ))
    }

    fn config_list(&mut self, include_empty: bool, ctx: &Context) -> Result<Value> {
        let flags = self.read(
            "COM_ESI_CTRL_GetConfigList",
            &[
                json!(vec![false; CONFIG_COUNT]),
                json!(vec![false; CONFIG_COUNT]),
            ],
            2,
            ctx,
            true,
        )?;
        let active = bool_array(&flags[0], CONFIG_COUNT)?;
        bool_array(&flags[1], CONFIG_COUNT)?;
        let mut result = Vec::new();
        for (index, active) in active.into_iter().enumerate() {
            if !include_empty && !active {
                continue;
            }
            let name = self.read(
                "COM_ESI_CTRL_GetConfigName",
                &[json!(index), buffer(CONFIG_NAME_SIZE)],
                1,
                ctx,
                true,
            )?;
            let name = string(&name[0])?;
            let flags = self.read(
                "COM_ESI_CTRL_GetConfigFlags",
                &[json!(index), json!(false), json!(false)],
                2,
                ctx,
                true,
            )?;
            result.push(json!({"index": index, "name": name, "active": boolean(&flags[0])?, "valid": boolean(&flags[1])?}));
        }
        Ok(json!(result))
    }

    fn probe(
        &mut self,
        symbol: &str,
        args: &[Value],
        count: usize,
        ctx: &Context,
    ) -> Result<Value> {
        match self.read(symbol, args, count, ctx, true) {
            Ok(mut values) => Ok(if count == 1 {
                values.remove(0)
            } else {
                json!(values)
            }),
            Err(error) => {
                self.permission(ctx, true)?;
                Ok(json!({"error": error.message}))
            }
        }
    }

    fn identity(&mut self, ctx: &Context) -> Result<Value> {
        let mut controller = Map::new();
        for (key, symbol, initial) in [
            ("product_id", "COM_ESI_CTRL_GetProductID", buffer(81)),
            ("product_no", "COM_ESI_CTRL_GetProductNo", json!(0)),
            ("firmware_version", "COM_ESI_CTRL_GetFwVersion", json!(0)),
            ("firmware_date", "COM_ESI_CTRL_GetFwDate", buffer(12)),
            ("hardware_type", "COM_ESI_CTRL_GetHwType", json!(0)),
            ("hardware_version", "COM_ESI_CTRL_GetHwVersion", json!(0)),
            ("device_type", "COM_ESI_CTRL_GetDevType", json!(0)),
        ] {
            controller.insert(key.to_owned(), self.probe(symbol, &[initial], 1, ctx)?);
        }
        let version = self.native("COM_ESI_CTRL_GetSWVersion", &[], ctx, true)?;
        if !(0..=65535).contains(&version.status) {
            return Err(Error::runtime("ESI DLL version is not a WORD"));
        }
        controller.insert("dll_version".to_owned(), json!(version.status));
        let mut modules = BTreeMap::new();
        for address in self.inventory.keys().copied().collect::<Vec<_>>() {
            let mut module = Map::new();
            for (key, symbol, initial) in [
                ("product_id", "COM_ESI_CTRL_GetModuleProductID", buffer(81)),
                ("product_no", "COM_ESI_CTRL_GetModuleProductNo", json!(0)),
                ("device_type", "COM_ESI_CTRL_GetModuleDevType", json!(0)),
                ("hardware_type", "COM_ESI_CTRL_GetModuleHwType", json!(0)),
                (
                    "hardware_version",
                    "COM_ESI_CTRL_GetModuleHwVersion",
                    json!(0),
                ),
                (
                    "firmware_version",
                    "COM_ESI_CTRL_GetModuleFwVersion",
                    json!(0),
                ),
            ] {
                module.insert(
                    key.to_owned(),
                    self.probe(symbol, &[json!(address), initial], 1, ctx)?,
                );
            }
            if module.get("device_type") == Some(&json!(HV_TYPE)) {
                module.insert(
                    "fpga_version".to_owned(),
                    self.probe(
                        "COM_ESI_CTRL_GetHVsupplyFpgaVersion",
                        &[json!(address), json!(0)],
                        1,
                        ctx,
                    )?,
                );
            }
            modules.insert(address, Value::Object(module));
        }
        Ok(json!({"controller": controller, "modules": integer_map(&modules)}))
    }

    fn diagnostics(&mut self, ctx: &Context) -> Result<Value> {
        let state = self.complete_state(ctx, true)?;
        let enabled = boolean(&self.scalar("COM_ESI_CTRL_GetEnable", json!(false), ctx, true)?)?;
        let housekeeping = self.read(
            "COM_ESI_CTRL_GetHousekeeping",
            &[json!(0.0), json!(0.0), json!(0.0), json!(0.0), json!(0.0)],
            5,
            ctx,
            true,
        )?;
        for value in &housekeeping {
            number(value)?;
        }
        let mut modules = BTreeMap::new();
        let mut any_hv_active = false;
        for address in HV_ADDRESSES {
            let module_state = unsigned(&state[8][address as usize])?;
            let flags = unsigned(&state[7][address as usize])?;
            let (negative, high) = self.ranges(address, ctx, true)?;
            let target = self.read(
                "COM_ESI_CTRL_GetHVsupplyTargetOutputVoltage",
                &[json!(address), json!(0.0)],
                1,
                ctx,
                true,
            )?;
            let target = number(&target[0])?;
            let (valid_v, measured_v) =
                self.discharge_data("COM_ESI_CTRL_GetHVsupplyOutputVoltage", address, ctx)?;
            let (valid_i, measured_a) =
                self.discharge_data("COM_ESI_CTRL_GetHVsupplyOutputCurrent", address, ctx)?;
            let pwm = self.pwm(address, ctx, true)?;
            let led = self.read(
                "COM_ESI_CTRL_GetModuleLEDData",
                &[json!(address), json!(false), json!(false), json!(false)],
                3,
                ctx,
                true,
            )?;
            let module_active = boolean(&pwm[6])?;
            let active = enabled && module_active && target != 0.0;
            any_hv_active |= active;
            modules.insert(address, json!({
                "active": active, "module_active": module_active, "module_state": module_state,
                "control_active": module_state & CTRL_ACTIVE != 0, "module_gate_active": module_state & MOD_ACTIVE != 0,
                "device_gate_active": module_state & DEV_ACTIVE != 0, "data_ready_flags": flags,
                "measurement": {"voltage_polarity": if negative { "negative" } else { "positive" },
                    "negative_voltage": negative, "high_current_range": high, "voltage_fresh": flags & VOLTAGE_READY != 0},
                "target_v": float(target), "voltage_valid": valid_v, "measured_v": float(measured_v),
                "current_valid": valid_i, "measured_a": float(measured_a),
                "led": {"red": boolean(&led[0])?, "green": boolean(&led[1])?, "blue": boolean(&led[2])?},
                "pwm": {"period_s": pwm[0], "width_s": pwm[1], "phase_measured_s": pwm[2], "phase_set_s": pwm[3],
                    "voltage_set_v": pwm[4], "voltage_measured_v": pwm[5], "data_ready_flags": pwm[7]},
            }));
        }
        let monitoring = self.heat_monitoring(self.io_timeout, ctx)?;
        let output = self.scalar(
            "COM_ESI_CTRL_GetHeatCtrlOutputVoltage",
            json!(0.0),
            ctx,
            true,
        )?;
        let power = self.scalar("COM_ESI_CTRL_GetHeatCtrlHeaterPower", json!(0.0), ctx, true)?;
        let interlock = self.scalar("COM_ESI_CTRL_GetHeatCtrlIlockState", json!(0), ctx, true)?;
        let heat_hk = self.read(
            "COM_ESI_CTRL_GetHeatCtrlHousekeeping",
            &[
                json!(false),
                json!(0.0),
                json!(0.0),
                json!(0.0),
                json!(0.0),
                json!(0.0),
            ],
            6,
            ctx,
            true,
        )?;
        let configuration = self.heat_configuration(ctx, true)?;
        let heat_command = self.read(
            "COM_ESI_CTRL_GetModuleActivationState",
            &[json!(0), json!(false)],
            1,
            ctx,
            true,
        )?;
        let heat_command = boolean(&heat_command[0])?;
        let heat_state = unsigned(&state[8][0])?;
        let heat_active = enabled && heat_command && heat_state & ACTIVE_BITS == ACTIVE_BITS;
        let mut heat = json!({
            "active": heat_active, "module_active": heat_command, "module_state": heat_state,
            "control_active": heat_state & CTRL_ACTIVE != 0, "module_gate_active": heat_state & MOD_ACTIVE != 0,
            "device_gate_active": heat_state & DEV_ACTIVE != 0, "valid": boolean(&monitoring[0])?,
            "output_voltage_v": float(number(&output)?), "heater_power_w": float(number(&power)?),
            "monitor_output_v": float(number(&monitoring[1])?), "monitor_voltage_v": float(number(&monitoring[2])?),
            "monitor_current_a": float(number(&monitoring[3])?), "monitor_temperature_c": float(number(&monitoring[4])?),
            "interlock_state": unsigned(&interlock)?,
            "housekeeping": {"valid": boolean(&heat_hk[0])?, "volt_3v3": float(number(&heat_hk[1])?),
                "temp_cpu_c": float(number(&heat_hk[2])?), "volt_5v": float(number(&heat_hk[3])?),
                "volt_24v": float(number(&heat_hk[4])?), "temp_psu_c": float(number(&heat_hk[5])?)},
        });
        heat.as_object_mut()
            .unwrap()
            .extend(configuration.as_object().unwrap().clone());
        let main = unsigned(&state[6])?;
        let device = unsigned(&state[1])?;
        let mut device_flags = flag_names(device, DEVICE_FLAGS);
        if device_flags.is_empty() {
            device_flags.push("DEVST_OK".to_owned());
        }
        self.permission(ctx, true)?;
        Ok(json!({
            "main_state": {"hex": format!("0x{main:x}"), "name": main_name(main)},
            "data_ready_flags": unsigned(&state[0])?,
            "device_state": {"hex": format!("0x{device:x}"), "flags": device_flags},
            "voltage_state": state_flags(unsigned(&state[2])?, VOLTAGE_FLAGS),
            "temperature_state": state_flags(unsigned(&state[3])?, TEMPERATURE_FLAGS),
            "fan_state": state_flags(unsigned(&state[4])?, FAN_FLAGS),
            "interlock_state": state_flags(unsigned(&state[5])?, INTERLOCK_FLAGS),
            "enabled": enabled, "global_active": enabled && (heat_active || any_hv_active),
            "housekeeping": {"volt_24v": housekeeping[0], "volt_5v": housekeeping[1], "volt_3v3": housekeeping[2],
                "temp_cpu_c": housekeeping[3], "temp_psu_c": housekeeping[4]},
            "modules": integer_map(&modules), "heat": heat,
        }))
    }

    fn discharge_data(&mut self, symbol: &str, address: u64, ctx: &Context) -> Result<(bool, f64)> {
        let values = self.read(
            symbol,
            &[json!(address), json!(false), json!(0.0)],
            2,
            ctx,
            true,
        )?;
        Ok((boolean(&values[0])?, number(&values[1])?))
    }

    fn discharge(&mut self, ctx: &Context) -> Result<()> {
        let deadline = Instant::now() + self.discharge_timeout.min(ctx.remaining());
        self.discharge_deadline = Some(deadline);
        let mut saved = BTreeMap::new();
        let mut readings = BTreeMap::from([(1, [f64::NAN; 3]), (2, [f64::NAN; 3])]);
        let mut consecutive = 0;
        let message = "ESI discharge unconfirmed: fresh ADC voltages on both polarities did not remain below the discharge limit for consecutive rounds";
        let result = (|| {
            for address in HV_ADDRESSES {
                saved.insert(address, self.ranges(address, ctx, true)?);
            }
            while consecutive < self.discharge_samples {
                for negative in [false, true] {
                    for (&address, &(_, high)) in &saved {
                        if Instant::now() >= deadline {
                            return Err(Error::runtime(message));
                        }
                        self.select_ranges(address, (negative, high), ctx, true)?;
                        self.discharge_data("COM_ESI_CTRL_GetHVsupplyOutputVoltage", address, ctx)?;
                        self.discharge_data("COM_ESI_CTRL_GetHVsupplyOutputCurrent", address, ctx)?;
                    }
                    let mut armed = [0_u64; 3];
                    let mut pending = [true; 2];
                    while pending.iter().any(|pending| *pending) {
                        for address in HV_ADDRESSES {
                            if !pending[address as usize - 1] {
                                continue;
                            }
                            self.permission(ctx, true)?;
                            if Instant::now() >= deadline {
                                return Err(Error::runtime(message));
                            }
                            let flags = self.read(
                                "COM_ESI_CTRL_GetModuleDataReadyFlags",
                                &[json!(address), json!(0)],
                                1,
                                ctx,
                                true,
                            )?;
                            let flags = unsigned(&flags[0])?;
                            let mask = VOLTAGE_READY | CURRENT_READY;
                            armed[address as usize] |= !flags & mask;
                            if flags & armed[address as usize] & mask == mask {
                                if self.ranges(address, ctx, true)? != (negative, saved[&address].1)
                                {
                                    return Err(Error::runtime(format!(
                                        "ESI ADC polarity changed on module {address}"
                                    )));
                                }
                                let (valid_v, voltage) = self.discharge_data(
                                    "COM_ESI_CTRL_GetHVsupplyOutputVoltage",
                                    address,
                                    ctx,
                                )?;
                                let (valid_i, current) = self.discharge_data(
                                    "COM_ESI_CTRL_GetHVsupplyOutputCurrent",
                                    address,
                                    ctx,
                                )?;
                                if Instant::now() >= deadline {
                                    return Err(Error::runtime(
                                        "ESI discharge unconfirmed: ADC response arrived after the discharge deadline",
                                    ));
                                }
                                if !valid_v
                                    || !valid_i
                                    || !voltage.is_finite()
                                    || !current.is_finite()
                                {
                                    return Err(Error::runtime(format!(
                                        "ESI invalid ADC voltage/current on module {address}; discharge unconfirmed"
                                    )));
                                }
                                let data = readings.get_mut(&address).unwrap();
                                data[usize::from(negative)] = voltage;
                                data[2] = current;
                                pending[address as usize - 1] = false;
                                ctx.progress(discharge_report(
                                    &readings,
                                    consecutive,
                                    self.discharge_limit,
                                ))?;
                            } else {
                                if flags & VOLTAGE_READY != 0
                                    && armed[address as usize] & VOLTAGE_READY == 0
                                {
                                    self.discharge_data(
                                        "COM_ESI_CTRL_GetHVsupplyOutputVoltage",
                                        address,
                                        ctx,
                                    )?;
                                }
                                if flags & CURRENT_READY != 0
                                    && armed[address as usize] & CURRENT_READY == 0
                                {
                                    self.discharge_data(
                                        "COM_ESI_CTRL_GetHVsupplyOutputCurrent",
                                        address,
                                        ctx,
                                    )?;
                                }
                                let flags = self.read(
                                    "COM_ESI_CTRL_GetModuleDataReadyFlags",
                                    &[json!(address), json!(0)],
                                    1,
                                    ctx,
                                    true,
                                )?;
                                armed[address as usize] |= !unsigned(&flags[0])? & mask;
                            }
                        }
                        if pending.iter().any(|pending| *pending) {
                            self.wait(deadline, ctx, true, message)?;
                        }
                    }
                }
                let below = readings.values().all(|data| {
                    data[..2]
                        .iter()
                        .all(|value| value.abs() <= self.discharge_limit)
                });
                consecutive = if below { consecutive + 1 } else { 0 };
                ctx.progress(discharge_report(
                    &readings,
                    consecutive,
                    self.discharge_limit,
                ))?;
            }
            Ok(())
        })();
        self.discharge_deadline = None;
        self.measurement_requests.clear();
        // Restore only on a responsive, synchronized transport. Cancellation
        // stops measurement but does not block bounded measurement-only cleanup.
        if !self.transport_poisoned
            && self.communication_error.is_none()
            && !ctx.remaining().is_zero()
        {
            for (address, selection) in saved {
                if let Err(cleanup) = self.write(
                    "COM_ESI_CTRL_SetHVsupplyMeasRanges",
                    &[json!(address), json!(selection.0), json!(selection.1)],
                    ctx,
                    false,
                ) {
                    return Err(Error::runtime(format!(
                        "{}ADC restoration also failed: {cleanup}",
                        result
                            .as_ref()
                            .err()
                            .map_or(String::new(), |error| format!("{error}; "))
                    )));
                }
            }
        }
        result
    }

    fn disconnect(&mut self, ctx: &Context) -> Result<bool> {
        self.permission(ctx, false)?;
        if !self.connected && !self.port_claimed {
            return Ok(true);
        }
        if self.connected {
            self.recover(ctx, false)?;
            self.safe_off(ctx)?;
            self.global_active(false, ctx)?;
            self.discharge(ctx)?;
        }
        self.write("COM_ESI_CTRL_Close", &[], ctx, false)?;
        self.connected = false;
        self.port_claimed = false;
        self.failed_open_released = self.open_failed;
        Ok(true)
    }
}

impl Backend for Controller {
    fn call(
        &mut self,
        method: &str,
        args: &[Value],
        kwargs: &Value,
        ctx: &Context,
    ) -> Result<Value> {
        let (positional, keyword_only): (&[&str], &[&str]) = match method {
            "connect" => (&["timeout_s"], &["preserve_outputs"]),
            "force_safe_off"
            | "discover_modules"
            | "collect_identity"
            | "get_heat_configuration"
            | "force_close_transport" => (&["timeout_s"], &[]),
            "collect_diagnostics" => (&["timeout_s"], &["cancel_event"]),
            "disconnect" => (&["timeout_s"], &["on_discharge"]),
            "configure_hv_max_voltage_steps" => (&["max_step_v", "timeout_s"], &[]),
            "list_configs" => (&["include_empty", "timeout_s"], &[]),
            "load_config" => (&["config_number", "timeout_s"], &[]),
            "save_config" => (
                &["config_number"],
                &["name", "active", "valid", "timeout_s"],
            ),
            "set_hv_module_target" | "set_target_voltage" => {
                (&["address", "voltage", "timeout_s"], &[])
            }
            "select_hv_measurement" => (&["address"], &["negative", "high_current", "timeout_s"]),
            "select_hv_voltage_adc" => (&["address"], &["negative", "timeout_s"]),
            "set_global_active" => (&["active", "timeout_s"], &["cancel_event"]),
            "get_hv_module_active" => (&["address", "timeout_s"], &[]),
            "set_hv_module_active" | "set_output_active" => {
                (&["address", "active", "timeout_s"], &["cancel_event"])
            }
            "configure_heat_limits" => (
                &[],
                &[
                    "voltage_v",
                    "current_a",
                    "power_w",
                    "timeout_s",
                    "cancel_event",
                ],
            ),
            "set_heater_temperature" => (&["temperature_c", "timeout_s"], &["cancel_event"]),
            "describe_error" | "format_status" => (&["status"], &[]),
            _ => {
                return Err(Error::unsupported(format!(
                    "ESI method {method} is not migrated; raw ESIBase notebook APIs and internal/unlocked helpers are not RPC operations"
                )));
            }
        };
        let arguments = Arguments::new(method, args, kwargs, positional, keyword_only)?;
        if matches!(method, "describe_error" | "format_status") {
            let status = arguments
                .required("status")?
                .as_i64()
                .ok_or_else(|| Error::argument("ESI status must be an integer"))?;
            return Ok(json!(if method == "describe_error" {
                self.describe_error(status).to_owned()
            } else {
                self.format_status(status)
            }));
        }
        self.io_timeout = arguments.timeout(self.default_timeout)?;
        let cancellable = match method {
            "force_safe_off" | "disconnect" | "force_close_transport" => false,
            "set_global_active" | "set_hv_module_active" | "set_output_active" => {
                arguments.bool_required("active")?
            }
            "set_hv_module_target" | "set_target_voltage" => {
                input_number(arguments.required("voltage")?, "voltage")? > 0.0
            }
            "set_heater_temperature" => {
                input_number(arguments.required("temperature_c")?, "temperature_c")? > 0.0
            }
            _ => true,
        };
        self.permission(ctx, cancellable)?;
        if method != "connect" && method != "disconnect" && method != "force_close_transport" {
            self.require_connected()?;
        }
        if !matches!(method, "connect" | "disconnect" | "force_close_transport") {
            self.recover(ctx, cancellable)?;
        }
        match method {
            "connect" => self
                .connect(arguments.bool_optional("preserve_outputs", false)?, ctx)
                .map(Value::Bool),
            "force_safe_off" => {
                self.safe_off(ctx)?;
                Ok(json!(true))
            }
            "discover_modules" => self.discover(ctx),
            "collect_identity" => self.identity(ctx),
            "collect_diagnostics" => self.diagnostics(ctx),
            "configure_hv_max_voltage_steps" => {
                let step = arguments
                    .optional("max_step_v")
                    .map(|value| input_number(value, "max_step_v"))
                    .transpose()?
                    .unwrap_or(DEFAULT_STEP);
                self.configure_steps(step, ctx)
            }
            "list_configs" => {
                self.config_list(arguments.bool_optional("include_empty", false)?, ctx)
            }
            "load_config" => {
                let slot = arguments.slot()?;
                self.safe_off(ctx)?;
                self.write("COM_ESI_CTRL_LoadCurrentConfig", &[json!(slot)], ctx, true)?;
                self.configure_steps(DEFAULT_STEP, ctx)?;
                self.safe_off(ctx)?;
                Ok(Value::Null)
            }
            "save_config" => {
                let slot = arguments.slot()?;
                let name = arguments
                    .optional("name")
                    .filter(|value| !value.is_null())
                    .map(|value| {
                        value
                            .as_str()
                            .ok_or_else(|| Error::argument("ESI config name must be a string"))
                    })
                    .transpose()?;
                let active = arguments.optional_bool("active")?;
                let valid = arguments.optional_bool("valid")?;
                self.write("COM_ESI_CTRL_SaveCurrentConfig", &[json!(slot)], ctx, true)?;
                if let Some(name) = name {
                    let name = name
                        .chars()
                        .map(|character| if character.is_ascii() { character } else { '?' })
                        .take(CONFIG_NAME_SIZE - 1)
                        .collect::<String>();
                    self.read(
                        "COM_ESI_CTRL_SetConfigName",
                        &[
                            json!(slot),
                            json!({"capacity": CONFIG_NAME_SIZE, "text": name}),
                        ],
                        1,
                        ctx,
                        true,
                    )?;
                }
                if active.is_some() || valid.is_some() {
                    self.write(
                        "COM_ESI_CTRL_SetConfigFlags",
                        &[
                            json!(slot),
                            json!(active.unwrap_or(true)),
                            json!(valid.unwrap_or(true)),
                        ],
                        ctx,
                        true,
                    )?;
                }
                Ok(Value::Null)
            }
            "set_hv_module_target" | "set_target_voltage" => {
                let address = arguments.address(false)?;
                let target = input_number(arguments.required("voltage")?, "target voltage")?;
                if !target.is_finite() || !(0.0..=MAX_VOLTAGE).contains(&target) {
                    return Err(Error::argument(
                        "ESI target voltage must be finite and between 0 and 3000 V; each HV module drives both connectors from one unsigned magnitude",
                    ));
                }
                self.hv_target(address, target, ctx).map(float)
            }
            "get_hv_module_active" => {
                let values = self.pwm(arguments.address(false)?, ctx, true)?;
                Ok(json!(boolean(&values[6])?))
            }
            "set_hv_module_active" => self
                .hv_active(
                    arguments.address(false)?,
                    arguments.bool_required("active")?,
                    ctx,
                )
                .map(Value::Bool),
            "set_global_active" => self
                .global_active(arguments.bool_required("active")?, ctx)
                .map(Value::Bool),
            "set_output_active" => self
                .output_active(
                    arguments.address(true)?,
                    arguments.bool_required("active")?,
                    ctx,
                )
                .map(Value::Bool),
            "select_hv_measurement" => {
                let address = arguments.address(false)?;
                let selection = (
                    arguments.bool_required("negative")?,
                    arguments.bool_optional("high_current", false)?,
                );
                if self.measurement_requests.get(&address) != Some(&selection) {
                    self.measurement_requests.remove(&address);
                    self.select_ranges(address, selection, ctx, true)?;
                    self.measurement_requests.insert(address, selection);
                }
                Ok(json!(true))
            }
            "select_hv_voltage_adc" => {
                let address = arguments.address(false)?;
                let selection = (
                    arguments.bool_required("negative")?,
                    self.ranges(address, ctx, true)?.1,
                );
                self.measurement_requests.remove(&address);
                self.select_ranges(address, selection, ctx, true)?;
                self.measurement_requests.insert(address, selection);
                Ok(Value::Null)
            }
            "get_heat_configuration" => self.heat_configuration(ctx, true),
            "configure_heat_limits" => {
                let maxima = self.heat_maxima(ctx, true)?;
                let settings = [
                    ("voltage_v", "COM_ESI_CTRL_SetHeatCtrlVoltageLimit"),
                    ("current_a", "COM_ESI_CTRL_SetHeatCtrlCurrentLimit"),
                    ("power_w", "COM_ESI_CTRL_SetHeatCtrlPowerLimit"),
                ];
                let mut requested = Vec::new();
                for (index, (name, symbol)) in settings.into_iter().enumerate() {
                    if let Some(value) = arguments.optional(name).filter(|value| !value.is_null()) {
                        let value = input_number(value, name)?;
                        if !value.is_finite()
                            || !maxima[index].is_finite()
                            || value <= 0.0
                            || value > maxima[index]
                        {
                            return Err(Error::argument(format!(
                                "ESI heat {name} must be greater than 0 and no more than the hardware maximum {}",
                                maxima[index]
                            )));
                        }
                        requested.push((name, symbol, value, maxima[index]));
                    }
                }
                let mut applied = Map::new();
                for (name, symbol, value, maximum) in requested {
                    let actual = number(&self.scalar(symbol, json!(value), ctx, true)?)?;
                    if !actual.is_finite() || actual <= 0.0 || actual > maximum {
                        return Err(Error::argument(format!(
                            "ESI heat {name} returned an invalid applied limit: {actual}"
                        )));
                    }
                    applied.insert(name.to_owned(), float(actual));
                }
                Ok(Value::Object(applied))
            }
            "set_heater_temperature" => {
                let target = input_number(arguments.required("temperature_c")?, "heater target")?;
                let maxima = self.heat_maxima(ctx, target > 0.0)?;
                let maximum = maxima[3];
                if !maximum.is_finite()
                    || maximum <= 0.0
                    || !target.is_finite()
                    || !(0.0..=maximum).contains(&target)
                {
                    return Err(Error::argument(format!(
                        "ESI heater target must be between 0 and the hardware maximum {maximum} degC"
                    )));
                }
                if target > 0.0 {
                    self.validate_heat(Some(target), ctx)?;
                }
                self.heat_target(target, maximum, ctx).map(float)
            }
            "disconnect" => self.disconnect(ctx).map(Value::Bool),
            "force_close_transport" => {
                if self.connected || self.port_claimed {
                    // Local port cleanup only: no Purge and no claim of safe OFF.
                    self.write("COM_ESI_CTRL_Close", &[], ctx, false)?;
                    self.connected = false;
                    self.port_claimed = false;
                    self.failed_open_released = self.open_failed;
                }
                Ok(json!(true))
            }
            _ => unreachable!("validated public ESI method"),
        }
    }

    fn get_attribute(&self, name: &str) -> Result<Value> {
        Ok(match name {
            "device_id" | "idn" => json!(self.device_id),
            "com" => json!(self.com),
            "baudrate" => json!(self.baudrate),
            "dll_path" | "esi_dll_path" => self.dll_path.clone(),
            "dll_sha256" => self.dll_sha256.clone(),
            "port_num" => json!(0),
            "connected" => json!(self.connected),
            "_dll_port_claimed" => json!(self.port_claimed),
            "_transport_poisoned" => json!(self.transport_poisoned),
            "_transport_error" => json!(self.transport_error),
            "_communication_error" => json!(self.communication_error),
            "_open_failed" => json!(self.open_failed),
            "_opening_in_progress" => json!(self.opening),
            "_failed_open_released" => json!(self.failed_open_released),
            "_module_inventory" => integer_map(&self.inventory),
            "_hv_measurement_requests" => integer_map(
                &self
                    .measurement_requests
                    .iter()
                    .map(|(&address, &(negative, high))| {
                        (
                            address,
                            tuple(vec![json!(negative), json!(high), json!(true)]),
                        )
                    })
                    .collect(),
            ),
            "_DEFAULT_IO_TIMEOUT_S" => float(self.default_timeout.as_secs_f64()),
            "DISCHARGE_TIMEOUT_S" => float(self.discharge_timeout.as_secs_f64()),
            "DISCHARGE_LIMIT_V" => float(self.discharge_limit),
            "DISCHARGE_SAMPLES" => json!(self.discharge_samples),
            "NO_ERR" => json!(0),
            "DEVICE_TYPE" => json!(DEVICE_TYPE),
            "MODULE_HTCTRL_TYPE" => json!(HEAT_TYPE),
            "MODULE_HVPS_TYPE" => json!(HV_TYPE),
            "MODULE_BASE_TYPE" => json!(0x1c34),
            "HEAT_MODULE_ADDRESS" | "ADDR_HTCTRL" => json!(0),
            "HV_MODULE_ADDRESSES" => tuple(vec![json!(1), json!(2)]),
            "CONTROLLED_MODULE_ADDRESSES" => tuple(vec![json!(0), json!(1), json!(2)]),
            "MAX_ABS_VOLTAGE_V" => float(MAX_VOLTAGE),
            "DEFAULT_HV_MAX_VOLTAGE_STEP_V" => float(DEFAULT_STEP),
            "HEATER_SAFETY_CONTRACT" => json!(1),
            "HEAT_MON_READY" => json!(HEAT_READY),
            "HV_ADC_V_READY" => json!(VOLTAGE_READY),
            "HV_ADC_I_READY" => json!(CURRENT_READY),
            "MS_CTRL_ACT" => json!(CTRL_ACTIVE),
            "MS_MOD_ACT" => json!(MOD_ACTIVE),
            "MS_DEV_ACT" => json!(DEV_ACTIVE),
            "MS_ACTIVE" => json!(ACTIVE_BITS),
            "MODULE_NUM" => json!(MODULE_COUNT),
            "CONFIG_DATA_SIZE" => json!(CONFIG_SIZE),
            "MAX_CONFIG" => json!(CONFIG_COUNT),
            "CONFIG_NAME_SIZE" => json!(CONFIG_NAME_SIZE),
            "PRODUCT_ID_SIZE" => json!(81),
            "DATA_STRING_SIZE" => json!(12),
            "ADDR_BASE" => json!(128),
            "ADDR_BROADCAST" => json!(255),
            "PRESENCE_BASE" => json!(MODULE_COUNT),
            "MODULE_NOT_FOUND" => json!(0),
            "MODULE_PRESENT" => json!(1),
            "MODULE_INVALID" => json!(2),
            "HV_CONFIG_BASE_OFFSET" => json!(17),
            "HV_CONFIG_STRIDE" => json!(12),
            "HV_CONFIG_MAX_STEP_OFFSET" => json!(4),
            "HV_CONFIG_ENABLE_OFFSET" => json!(10),
            "HK_RDY" => json!(1),
            "HK_OFL" => json!(2),
            "FAN_RDY" => json!(4),
            "FAN_OFL" => json!(8),
            "err_dict" => self.error_codes.clone(),
            "MAIN_STATE" => integer_map(
                &[0, 1, 16, 17, 18, 19, 20, 21]
                    .into_iter()
                    .map(|state| (state, json!(main_name(state))))
                    .collect(),
            ),
            "DEVICE_STATE" => flag_map(DEVICE_FLAGS),
            "VOLTAGE_STATE" => flag_map(VOLTAGE_FLAGS),
            "TEMPERATURE_STATE" => flag_map(TEMPERATURE_FLAGS),
            "FAN_STATE" => flag_map(FAN_FLAGS),
            "INTERLOCK_STATE" => flag_map(INTERLOCK_FLAGS),
            name if name.starts_with("ERR_") => {
                let code = match name {
                    "ERR_OPEN" => -2,
                    "ERR_CLOSE" => -3,
                    "ERR_PURGE" => -4,
                    "ERR_CONTROL" => -5,
                    "ERR_STATUS" => -6,
                    "ERR_COMMAND_SEND" => -7,
                    "ERR_DATA_SEND" => -8,
                    "ERR_TERM_SEND" => -9,
                    "ERR_COMMAND_RECEIVE" => -10,
                    "ERR_DATA_RECEIVE" => -11,
                    "ERR_TERM_RECEIVE" => -12,
                    "ERR_COMMAND_WRONG" => -13,
                    "ERR_ARGUMENT_WRONG" => -14,
                    "ERR_ARGUMENT" => -15,
                    "ERR_RATE" => -16,
                    "ERR_NOT_CONNECTED" => -100,
                    "ERR_NOT_READY" => -101,
                    "ERR_READY" => -102,
                    _ => {
                        return Err(Error::new(
                            "AttributeError",
                            format!("Unknown ESI attribute: {name}"),
                        ));
                    }
                };
                json!(code)
            }
            _ => {
                return Err(Error::new(
                    "AttributeError",
                    format!("Unknown or worker-local ESI attribute: {name}"),
                ));
            }
        })
    }

    fn set_attribute(&mut self, name: &str, value: Value) -> Result<()> {
        match name {
            "_DEFAULT_IO_TIMEOUT_S" => self.default_timeout = positive_duration(&value, name)?,
            "DISCHARGE_TIMEOUT_S" => self.discharge_timeout = positive_duration(&value, name)?,
            "DISCHARGE_LIMIT_V" => {
                let limit = input_number(&value, name)?;
                if !limit.is_finite() || !(0.0..=1.0).contains(&limit) {
                    return Err(Error::argument(
                        "ESI discharge limit must be finite and between 0 and 1 V; native configuration cannot weaken the operational shutdown check",
                    ));
                }
                self.discharge_limit = limit;
            }
            "DISCHARGE_SAMPLES" => {
                let samples = value
                    .as_u64()
                    .filter(|samples| (3..=1000).contains(samples))
                    .ok_or_else(|| {
                        Error::argument(
                            "ESI discharge requires at least 3 and at most 1000 consecutive rounds",
                        )
                    })?;
                self.discharge_samples = samples as usize;
            }
            "com" | "baudrate" => {
                if self.connected || self.port_claimed {
                    return Err(Error::runtime(
                        "ESI communication configuration cannot change while the port is owned",
                    ));
                }
                let maximum = if name == "com" { 255 } else { u32::MAX as u64 };
                let setting = value
                    .as_u64()
                    .filter(|setting| *setting > 0 && *setting <= maximum)
                    .ok_or_else(|| {
                        Error::argument(format!(
                            "ESI {name} must be an integer between 1 and {maximum}"
                        ))
                    })?;
                if name == "com" {
                    self.com = setting;
                } else {
                    self.baudrate = setting;
                }
            }
            "device_id" | "idn" => {
                let id = value
                    .as_str()
                    .filter(|id| !id.trim().is_empty())
                    .ok_or_else(|| Error::argument("ESI device_id must be a non-empty string"))?;
                self.device_id = id.to_owned();
            }
            _ => {
                return Err(Error::new(
                    "AttributeError",
                    format!("Read-only or unknown ESI attribute: {name}"),
                ));
            }
        }
        Ok(())
    }
}

struct Arguments {
    method: String,
    values: BTreeMap<String, Value>,
}

impl Arguments {
    fn new(
        method: &str,
        args: &[Value],
        kwargs: &Value,
        positional: &[&str],
        keyword_only: &[&str],
    ) -> Result<Self> {
        if args.len() > positional.len() {
            return Err(Error::new(
                "TypeError",
                format!(
                    "ESI {method} takes at most {} positional arguments",
                    positional.len()
                ),
            ));
        }
        let mut values = BTreeMap::new();
        for (name, value) in positional.iter().zip(args) {
            values.insert((*name).to_owned(), value.clone());
        }
        if !kwargs.is_null() {
            let kwargs = kwargs
                .as_object()
                .ok_or_else(|| Error::new("TypeError", "ESI kwargs must be an object"))?;
            for (name, value) in kwargs {
                if !positional.contains(&name.as_str()) && !keyword_only.contains(&name.as_str()) {
                    return Err(Error::new(
                        "TypeError",
                        format!("ESI {method} got an unexpected keyword argument '{name}'"),
                    ));
                }
                if values.insert(name.clone(), value.clone()).is_some() {
                    return Err(Error::new(
                        "TypeError",
                        format!("ESI {method} got multiple values for '{name}'"),
                    ));
                }
            }
        }
        // Python events and callbacks are supervisor-side objects, represented
        // by Context cancellation and progress rather than arbitrary JSON data.
        for name in ["cancel_event", "on_discharge"] {
            if values.get(name).is_some_and(|value| !value.is_null()) {
                return Err(Error::argument(format!(
                    "ESI {name} must be handled by the Python supervisor, not serialized into the worker"
                )));
            }
        }
        Ok(Self {
            method: method.to_owned(),
            values,
        })
    }

    fn optional(&self, name: &str) -> Option<&Value> {
        self.values.get(name)
    }
    fn required(&self, name: &str) -> Result<&Value> {
        self.optional(name).ok_or_else(|| {
            Error::new(
                "TypeError",
                format!("ESI {} missing required argument: {name}", self.method),
            )
        })
    }
    fn bool_required(&self, name: &str) -> Result<bool> {
        self.required(name)?
            .as_bool()
            .ok_or_else(|| Error::argument(format!("ESI {name} must be a bool")))
    }
    fn bool_optional(&self, name: &str, default: bool) -> Result<bool> {
        self.optional(name).map_or(Ok(default), |value| {
            value
                .as_bool()
                .ok_or_else(|| Error::argument(format!("ESI {name} must be a bool")))
        })
    }
    fn optional_bool(&self, name: &str) -> Result<Option<bool>> {
        self.optional(name)
            .filter(|value| !value.is_null())
            .map(|value| {
                value
                    .as_bool()
                    .ok_or_else(|| Error::argument(format!("ESI {name} must be a bool")))
            })
            .transpose()
    }
    fn address(&self, heat: bool) -> Result<u64> {
        let value = self.required("address")?;
        if !value.is_i64() && !value.is_u64() {
            return Err(Error::new(
                "TypeError",
                "ESI module address must be an integer",
            ));
        }
        let address = value.as_u64().unwrap_or(u64::MAX);
        if !HV_ADDRESSES.contains(&address) && !(heat && address == 0) {
            return Err(Error::argument(format!(
                "ESI module address must be one of {}",
                if heat { "(0, 1, 2)" } else { "(1, 2)" }
            )));
        }
        Ok(address)
    }
    fn slot(&self) -> Result<u64> {
        self.required("config_number")?
            .as_u64()
            .filter(|slot| *slot < CONFIG_COUNT as u64)
            .ok_or_else(|| Error::argument("ESI config number must be 0..1022"))
    }
    fn timeout(&self, default: Duration) -> Result<Duration> {
        self.optional("timeout_s")
            .filter(|value| !value.is_null())
            .map_or(Ok(default), |value| positive_duration(value, "timeout_s"))
    }
}

fn positive_duration(value: &Value, name: &str) -> Result<Duration> {
    let seconds = input_number(value, name)?;
    if !seconds.is_finite() || seconds <= 0.0 || seconds > 86400.0 {
        return Err(Error::argument(format!(
            "ESI {name} must be finite, greater than 0 and at most 86400 seconds"
        )));
    }
    let duration = Duration::try_from_secs_f64(seconds)
        .map_err(|_| Error::argument(format!("ESI {name} is outside the duration range")))?;
    if duration.is_zero() {
        return Err(Error::argument(format!(
            "ESI {name} is below clock resolution"
        )));
    }
    Ok(duration)
}

const DEVICE_FLAGS: &[(u64, &str)] = &[
    (1, "DS_ILOCK_FAIL"),
    (2, "DS_VOLT_FAIL"),
    (4, "DS_TEMP_FAIL"),
    (8, "DS_FAN_FAIL"),
    (16, "DS_MODULE_FAIL"),
];
const VOLTAGE_FLAGS: &[(u64, &str)] = &[
    (1, "VS_3V3_OK"),
    (2, "VS_5V0_OK"),
    (4, "VS_24V_OK"),
    (16, "VS_LINE_OK"),
    (32, "VS_PSU_OK"),
];
const TEMPERATURE_FLAGS: &[(u64, &str)] = &[
    (1, "TS_TCPU_HIGH"),
    (2, "TS_TPSU_HIGH"),
    (4, "TS_TCPU_LOW"),
    (8, "TS_TPSU_LOW"),
];
const FAN_FLAGS: &[(u64, &str)] = &[
    (1, "FS_FAN_OK"),
    (2, "FS_FAN_SW_CURR"),
    (4, "FS_FAN_SW_LAST"),
    (8, "FS_FAN_ENB"),
];
const INTERLOCK_FLAGS: &[(u64, &str)] = &[
    (1, "IS_HTCTRL_ILOCK1"),
    (2, "IS_HTCTRL_ILOCK2"),
    (4, "IS_CTRL_ILOCK_FP"),
    (8, "IS_CTRL_ILOCK_RP"),
    (256, "IS_HTCTRL_ILOCK1_CURR"),
    (512, "IS_HTCTRL_ILOCK2_CURR"),
    (1024, "IS_HTCTRL_ILOCK1_LAST"),
    (2048, "IS_HTCTRL_ILOCK2_LAST"),
    (4096, "IS_CTRL_ILOCK_FP_CURR"),
    (8192, "IS_CTRL_ILOCK_RP_CURR"),
    (16384, "IS_CTRL_ILOCK_FP_LAST"),
    (32768, "IS_CTRL_ILOCK_RP_LAST"),
];

fn main_name(state: u64) -> String {
    match state {
        0 => "STATE_ON",
        1 => "STATE_STANDBY",
        16 => "STATE_ERROR",
        17 => "STATE_ERR_MODULE",
        18 => "STATE_ERR_VSUP",
        19 => "STATE_ERR_TEMP_LOW",
        20 => "STATE_ERR_TEMP_HIGH",
        21 => "STATE_ERR_ILOCK",
        _ => return format!("UNKNOWN_STATE_0x{state:04X}"),
    }
    .to_owned()
}

fn flag_names(state: u64, flags: &[(u64, &str)]) -> Vec<String> {
    flags
        .iter()
        .filter_map(|(flag, name)| (state & flag != 0).then_some((*name).to_owned()))
        .collect()
}
fn state_flags(state: u64, flags: &[(u64, &str)]) -> Value {
    json!({"hex": format!("0x{state:x}"), "flags": flag_names(state, flags)})
}
fn flag_map(flags: &[(u64, &str)]) -> Value {
    integer_map(
        &flags
            .iter()
            .map(|&(flag, name)| (flag, json!(name)))
            .collect(),
    )
}

fn buffer(capacity: usize) -> Value {
    json!({"capacity": capacity, "text": ""})
}

fn pwm_args(address: u64) -> Vec<Value> {
    vec![
        json!(address),
        json!(0.0),
        json!(0.0),
        json!(0.0),
        json!(0.0),
        json!(0.0),
        json!(0.0),
        json!(false),
        json!(0),
    ]
}

fn validate_pwm(values: &[Value]) -> Result<()> {
    if values.len() != 8 {
        return Err(Error::runtime(
            "ESI PWM reply must contain exactly 8 pointer values",
        ));
    }
    for value in &values[..6] {
        number(value)?;
    }
    boolean(&values[6])?;
    unsigned(&values[7])?;
    Ok(())
}

fn integer_map(values: &BTreeMap<u64, Value>) -> Value {
    json!({"$map": values.iter().map(|(key, value)| json!([key, value])).collect::<Vec<_>>()})
}

fn discharge_report(readings: &BTreeMap<u64, [f64; 3]>, consecutive: usize, limit: f64) -> Value {
    let modules = readings.iter().map(|(&address, data)| (address, json!({"positive_v": float(data[0]), "negative_v": float(data[1]), "measured_a": float(data[2])}))).collect();
    json!({"modules": integer_map(&modules), "consecutive": consecutive, "limit_v": float(limit)})
}

fn unsigned(value: &Value) -> Result<u64> {
    value
        .as_u64()
        .ok_or_else(|| Error::runtime("ESI native reply contains an invalid unsigned integer"))
}

fn boolean(value: &Value) -> Result<bool> {
    value
        .as_bool()
        .ok_or_else(|| Error::runtime("ESI native reply contains an invalid bool"))
}

fn number(value: &Value) -> Result<f64> {
    if let Some(value) = value.as_f64() {
        return Ok(value);
    }
    match value.get("$float").and_then(Value::as_str) {
        Some("nan") => Ok(f64::NAN),
        Some("inf") => Ok(f64::INFINITY),
        Some("-inf") => Ok(f64::NEG_INFINITY),
        _ => Err(Error::runtime(
            "ESI native reply contains an invalid floating-point value",
        )),
    }
}

fn input_number(value: &Value, name: &str) -> Result<f64> {
    number(value).map_err(|_| Error::argument(format!("ESI {name} must be a number")))
}

fn unsigned_array(value: &Value, size: usize, maximum: u64) -> Result<Vec<u64>> {
    let array = value
        .as_array()
        .filter(|array| array.len() == size)
        .ok_or_else(|| {
            Error::runtime(format!(
                "ESI native buffer must contain exactly {size} elements"
            ))
        })?;
    array
        .iter()
        .map(|value| {
            let value = unsigned(value)?;
            if value > maximum {
                return Err(Error::runtime("ESI native buffer element is out of range"));
            }
            Ok(value)
        })
        .collect()
}

fn bool_array(value: &Value, size: usize) -> Result<Vec<bool>> {
    let array = value
        .as_array()
        .filter(|array| array.len() == size)
        .ok_or_else(|| {
            Error::runtime(format!(
                "ESI native bool buffer must contain exactly {size} elements"
            ))
        })?;
    array.iter().map(boolean).collect()
}

fn string(value: &Value) -> Result<&str> {
    value
        .as_str()
        .ok_or_else(|| Error::runtime("ESI native reply contains an invalid string"))
}

fn close(left: f64, right: f64) -> bool {
    left.is_finite()
        && right.is_finite()
        && (left - right).abs() <= 1e-6_f64.max(1e-9 * left.abs().max(right.abs()))
}
