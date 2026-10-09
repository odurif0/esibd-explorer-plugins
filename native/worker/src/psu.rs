//! Canonical PSU A/B/C/D/E controller. GUI ownership stays in the Python adapter.
//!
//! GUI driver calls: connect, initialize, disconnect, get_status, list_configs,
//! load_config, save_config, channel setpoints/limits/measurements, output/range/
//! device/interlock getters and setters, collect_housekeeping. GUI-only duties
//! include channel creation, setpoint memory, plots, status and acquisition.
//! Native adapters route hardware manual/ramp work to apply_manual_state and
//! ramp_channel_voltage, with parent-local Events driving IPC cancellation.
//! cached_setpoint_limits is a Python
//! context manager, not an RPC operation; native ramps cache within one call.

use crate::Backend;
use crate::codec::{float, tuple};
use crate::context::Context;
use crate::error::{Error, Result};
use crate::ffi::{Dll, NativeReply};
use serde_json::{Map, Value, json};
use std::time::{Duration, Instant};

pub const MAX_CONFIG: usize = 168;
pub const CONFIG_NAME_SIZE: usize = 75;
pub const PRODUCT_ID_SIZE: usize = 60;
pub const FW_DATE_SIZE: usize = 16;
pub const SENSOR_COUNT: usize = 3;
pub const FAN_COUNT: usize = 3;
pub const PUBLIC_METHODS: &[&str] = &[
    "connect",
    "initialize",
    "disconnect",
    "get_status",
    "list_configs",
    "load_config",
    "save_config",
    "set_config_name",
    "set_config_flags",
    "set_device_enabled",
    "get_device_enabled",
    "set_output_enabled",
    "get_output_enabled",
    "set_output_full_range",
    "get_output_full_range",
    "set_interlock_enabled",
    "get_interlock_enabled",
    "set_channel_voltage",
    "get_channel_voltage",
    "get_channel_voltage_limits",
    "set_channel_current",
    "get_channel_current",
    "get_channel_current_limits",
    "get_channel_measurements",
    "get_channel_measured_voltage",
    "get_channel_measured_current",
    "get_product_info",
    "collect_housekeeping",
    "shutdown",
    "apply_manual_state",
    "ramp_channel_voltage",
];

const RANGE_SAFE_V: f64 = 5.0;
const RANGE_SETTLE_S: f64 = 2.0;
const RAMP_THRESHOLD_V: f64 = 50.0;
const DEFAULT_STEP_V: f64 = 100.0;
const DEFAULT_STEP_S: f64 = 0.05;
const VOLTAGE_ABS_TOLERANCE: f64 = 0.01;
const CURRENT_ABS_TOLERANCE: f64 = 0.001;
const SETPOINT_REL_TOLERANCE: f64 = 0.01;

pub struct Controller {
    dll: Box<dyn Dll>,
    device_id: String,
    com: u16,
    port: u16,
    baudrate: u32,
    connected: bool,
    port_claimed: bool,
    port_open: bool,
    transport_poisoned: bool,
    transport_error: Option<String>,
    open_failed: bool,
    failed_open_released: bool,
    opening: bool,
}

// A per-export budget supplements the parent request deadline. It cannot
// interrupt a blocked DLL; the supervisor remains responsible for hard bounds.
struct Io<'a> {
    ctx: &'a Context,
    timeout: Duration,
}

impl<'a> Io<'a> {
    fn new(ctx: &'a Context, seconds: f64) -> Result<Self> {
        let timeout = Duration::try_from_secs_f64(seconds)
            .map_err(|_| Error::argument("PSU timeout_s must be finite and greater than 0."))?;
        if timeout.is_zero() {
            return Err(Error::argument("PSU timeout_s must be greater than 0."));
        }
        Ok(Self { ctx, timeout })
    }
}

struct Arguments {
    values: Map<String, Value>,
}

impl Arguments {
    fn bind(
        method: &str,
        args: &[Value],
        kwargs: &Value,
        positional: &[&str],
        keyword: &[&str],
    ) -> Result<Self> {
        let mut values = match kwargs {
            Value::Null => Map::new(),
            Value::Object(values) => values.clone(),
            _ => return Err(Error::new("TypeError", "PSU kwargs must be an object.")),
        };
        if args.len() > positional.len() {
            return Err(Error::new(
                "TypeError",
                format!("PSU {method}: too many positional arguments"),
            ));
        }
        for key in values.keys() {
            if !positional.contains(&key.as_str()) && !keyword.contains(&key.as_str()) {
                return Err(Error::new(
                    "TypeError",
                    format!("PSU {method}: unexpected keyword argument {key}"),
                ));
            }
        }
        for (name, value) in positional.iter().zip(args) {
            if values.insert((*name).into(), value.clone()).is_some() {
                return Err(Error::new(
                    "TypeError",
                    format!("PSU {method}: multiple values for {name}"),
                ));
            }
        }
        Ok(Self { values })
    }

    fn required(&self, name: &str) -> Result<&Value> {
        self.values.get(name).ok_or_else(|| {
            Error::new(
                "TypeError",
                format!("PSU missing required argument: {name}"),
            )
        })
    }

    fn optional(&self, name: &str) -> Option<&Value> {
        self.values.get(name).filter(|v| !v.is_null())
    }

    fn number(&self, name: &str, default: f64) -> Result<f64> {
        self.optional(name)
            .map(|v| number(v, name))
            .unwrap_or(Ok(default))
    }

    fn boolean(&self, name: &str, default: bool) -> bool {
        self.values.get(name).map(truthy).unwrap_or(default)
    }

    fn timeout(&self) -> Result<f64> {
        let value = self.number("timeout_s", 5.0)?;
        if !value.is_finite() || value <= 0.0 {
            return Err(Error::argument(
                "PSU timeout_s must be finite and greater than 0.",
            ));
        }
        Duration::try_from_secs_f64(value)
            .map_err(|_| Error::argument("PSU timeout_s is too large."))?;
        Ok(value)
    }
}

fn number(value: &Value, name: &str) -> Result<f64> {
    let result = match value {
        Value::Number(n) => n.as_f64(),
        Value::Bool(b) => Some(if *b { 1.0 } else { 0.0 }),
        Value::String(s) => s.trim().parse::<f64>().ok(),
        Value::Object(map) => match map.get("$float").and_then(Value::as_str) {
            Some("nan") => Some(f64::NAN),
            Some("inf") => Some(f64::INFINITY),
            Some("-inf") => Some(f64::NEG_INFINITY),
            _ => None,
        },
        _ => None,
    };
    result.ok_or_else(|| Error::argument(format!("PSU {name} must be a real number.")))
}

fn integer(value: &Value, name: &str, minimum: u64, maximum: u64) -> Result<u64> {
    let n = match value {
        Value::String(s) => s
            .trim()
            .parse::<i128>()
            .ok()
            .filter(|n| *n >= 0)
            .map(|n| n as u128),
        Value::Number(n) if n.as_u64().is_some() => n.as_u64().map(u128::from),
        _ => number(value, name)
            .ok()
            .filter(|n| n.is_finite() && *n >= 0.0)
            .map(|n| n.trunc() as u128),
    };
    match n {
        Some(n) if n >= minimum as u128 && n <= maximum as u128 => Ok(n as u64),
        _ => Err(Error::argument(format!(
            "PSU {name} must be in {minimum}..={maximum}."
        ))),
    }
}

fn channel(value: &Value) -> Result<usize> {
    Ok(integer(value, "channel (0 or 1)", 0, 1)? as usize)
}
fn config_number(value: &Value) -> Result<usize> {
    Ok(integer(value, "config number", 0, (MAX_CONFIG - 1) as u64)? as usize)
}

fn truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_f64().is_some_and(|n| n != 0.0),
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
    }
}

fn optional_status(status: i64) -> bool {
    matches!(status, -10 | -11 | -13 | -14)
}

fn win_bool(value: &Value) -> Result<bool> {
    match value {
        Value::Bool(b) => Ok(*b),
        Value::Number(n) => n
            .as_i64()
            .filter(|n| i32::try_from(*n).is_ok())
            .map(|n| n != 0)
            .ok_or_else(|| Error::runtime("Invalid PSU Windows BOOL readback.")),
        _ => Err(Error::runtime(
            "Missing or invalid PSU Windows BOOL readback.",
        )),
    }
}

fn c_bool(value: &Value) -> Result<bool> {
    value
        .as_bool()
        .ok_or_else(|| Error::runtime("Invalid PSU C bool readback."))
}

fn pointer_values(reply: NativeReply, count: usize, operation: &str) -> Result<Vec<Value>> {
    if reply.status != 0 {
        return Err(Error::status(reply.status, operation));
    }
    if reply.values.len() != count {
        return Err(Error::runtime(format!(
            "PSU {operation}: expected {count} pointer values, got {}",
            reply.values.len()
        )));
    }
    Ok(reply.values)
}

fn read_number(value: &Value, quantity: &str) -> Result<f64> {
    number(value, quantity).map_err(|error| {
        Error::runtime(format!(
            "Invalid PSU {quantity} readback: {}",
            error.message
        ))
    })
}

fn telemetry(value: &Value) -> Result<Value> {
    Ok(float(read_number(value, "telemetry")?))
}

fn word(value: &Value, maximum: u64) -> Result<u64> {
    value
        .as_u64()
        .filter(|n| *n <= maximum)
        .ok_or_else(|| Error::runtime("Invalid PSU state/counter readback."))
}

fn pair_value(values: [bool; 2]) -> Value {
    tuple(values.into_iter().map(Value::Bool).collect())
}

fn main_state_name(state: u64) -> String {
    match state {
        0 => "STATE_ON".into(),
        0x8000 => "STATE_ERROR".into(),
        0x8001 => "STATE_ERR_VSUP".into(),
        0x8002 => "STATE_ERR_TEMP_LOW".into(),
        0x8003 => "STATE_ERR_TEMP_HIGH".into(),
        0x8004 => "STATE_ERR_ILOCK".into(),
        0x8005 => "STATE_ERR_PSU_DIS".into(),
        _ => format!("UNKNOWN_STATE_0x{state:04X}"),
    }
}

fn device_flags(state: u64) -> Vec<&'static str> {
    if state == 0 {
        return vec!["DEVST_OK"];
    }
    [
        (0, "DEVST_VCPU_FAIL"),
        (1, "DEVST_VFAN_FAIL"),
        (2, "DEVST_VPSU0_FAIL"),
        (3, "DEVST_VPSU1_FAIL"),
        (8, "DEVST_FAN1_FAIL"),
        (9, "DEVST_FAN2_FAIL"),
        (10, "DEVST_FAN3_FAIL"),
        (15, "DEVST_PSU_DIS"),
        (16, "DEVST_SEN1_HIGH"),
        (17, "DEVST_SEN2_HIGH"),
        (18, "DEVST_SEN3_HIGH"),
        (24, "DEVST_SEN1_LOW"),
        (25, "DEVST_SEN2_LOW"),
        (26, "DEVST_SEN3_LOW"),
    ]
    .into_iter()
    .filter(|(bit, _)| state & (1 << bit) != 0)
    .map(|(_, name)| name)
    .collect()
}

impl Controller {
    pub fn new(config: &Value, dll: Box<dyn Dll>) -> Result<Self> {
        let config = config
            .as_object()
            .ok_or_else(|| Error::argument("PSU config must be an object."))?;
        for name in config.keys() {
            if ![
                "device_id",
                "com",
                "port",
                "baudrate",
                "dll_path",
                "dll_sha256",
                "log_dir",
                "logger",
                "thread_lock",
            ]
            .contains(&name.as_str())
            {
                return Err(Error::new(
                    "TypeError",
                    format!("Unexpected PSU init argument: {name}"),
                ));
            }
        }
        for name in ["logger", "thread_lock"] {
            if config.get(name).is_some_and(|value| !value.is_null()) {
                return Err(Error::argument(format!(
                    "PSU {name} is parent-local and cannot be sent to the native worker."
                )));
            }
        }
        let get = |key: &str| {
            config
                .get(key)
                .ok_or_else(|| Error::new("TypeError", format!("PSU missing init argument: {key}")))
        };
        let device_id = get("device_id")?
            .as_str()
            .ok_or_else(|| Error::argument("PSU device_id must be a string."))?
            .to_owned();
        let com = integer(get("com")?, "com", 1, u16::MAX as u64)? as u16;
        let port = integer(config.get("port").unwrap_or(&json!(0)), "port", 0, 15)? as u16;
        let baudrate = integer(
            config.get("baudrate").unwrap_or(&json!(230400)),
            "baudrate",
            1,
            u32::MAX as u64,
        )? as u32;
        Ok(Self {
            dll,
            device_id,
            com,
            port,
            baudrate,
            connected: false,
            port_claimed: false,
            port_open: false,
            transport_poisoned: false,
            transport_error: None,
            open_failed: false,
            failed_open_released: false,
            opening: false,
        })
    }

    fn usable(&self) -> Result<()> {
        if self.transport_poisoned {
            return Err(Error::runtime(format!(
                "PSU transport is unusable: {}",
                self.transport_error.as_deref().unwrap_or("unknown failure")
            )));
        }
        Ok(())
    }

    fn require_connected(&self) -> Result<()> {
        self.usable()?;
        if !self.connected {
            return Err(Error::runtime("PSU device is not connected."));
        }
        Ok(())
    }

    fn poison(&mut self, error: &Error) {
        self.transport_poisoned = true;
        self.transport_error = Some(error.to_string());
        self.port_claimed = true;
    }

    fn raw(&mut self, suffix: &str, extra: &[Value], io: &Io<'_>) -> Result<NativeReply> {
        self.usable()?;
        io.ctx.check_cancelled()?;
        let mut args = Vec::with_capacity(extra.len() + 1);
        args.push(json!(self.port));
        args.extend_from_slice(extra);
        let start = Instant::now();
        let symbol = format!("COM_HVPSU2D_{suffix}");
        let result = io
            .ctx
            .native_call(&symbol, Some(io.timeout), || self.dll.call(&symbol, &args));
        if let Err(error) = &result
            && matches!(
                error.kind.as_str(),
                "TimeoutError" | "BrokenPipeError" | "EOFError" | "ConnectionError"
            )
        {
            self.poison(error);
        }
        if start.elapsed() >= io.timeout || io.ctx.remaining().is_zero() {
            let error = Error::new(
                "TimeoutError",
                format!("PSU {suffix} exceeded the I/O deadline; transport is unusable."),
            );
            self.poison(&error);
            return Err(error);
        }
        let reply = result?;
        if reply.status == 0 {
            match suffix {
                "Open" => self.port_open = true,
                "Close" => self.port_open = false,
                _ => (),
            }
        }
        io.ctx.check_cancelled()?;
        Ok(reply)
    }

    fn read(
        &mut self,
        suffix: &str,
        extra: &[Value],
        count: usize,
        io: &Io<'_>,
    ) -> Result<Vec<Value>> {
        pointer_values(self.raw(suffix, extra, io)?, count, suffix)
    }

    fn write(&mut self, suffix: &str, extra: &[Value], io: &Io<'_>) -> Result<()> {
        pointer_values(self.raw(suffix, extra, io)?, 0, suffix)?;
        Ok(())
    }

    fn recovery_context(timeout: f64, ctx: &Context) -> Context {
        ctx.cleanup(Duration::from_secs_f64((timeout.min(5.0) * 8.0).min(5.0)))
    }

    fn connect(&mut self, io: &Io<'_>) -> Result<bool> {
        self.usable()?;
        io.ctx.check_cancelled()?;
        if self.connected {
            return Ok(true);
        }
        if self.port_claimed {
            return Err(Error::runtime(
                "PSU port cleanup is unconfirmed; disconnect before reconnecting.",
            ));
        }
        self.opening = true;
        self.open_failed = false;
        self.failed_open_released = false;
        // Claim before Open: even an error may have partially allocated a port.
        self.port_claimed = true;
        let result = (|| {
            self.write("Open", &[json!(self.com)], io)?;
            let baud = self.read("SetBaudRate", &[json!(self.baudrate)], 1, io)?;
            word(&baud[0], u32::MAX as u64)?;
            self.connected = true;
            // Identity is advisory, but transport poisoning is never optional.
            let probe = self.raw(
                "GetProductID",
                &[json!({"capacity": PRODUCT_ID_SIZE, "text": ""})],
                io,
            );
            if self.transport_poisoned {
                return Err(probe
                    .err()
                    .unwrap_or_else(|| Error::runtime("PSU identity probe poisoned transport.")));
            }
            io.ctx.check_cancelled()?;
            if let Ok(reply) = probe
                && reply.status == 0
            {
                let values = pointer_values(reply, 1, "GetProductID")?;
                let product_id = values[0]
                    .as_str()
                    .ok_or_else(|| Error::runtime("Invalid PSU product identity readback."))?;
                let normalized = product_id.to_uppercase();
                if !product_id.is_empty() && !normalized.contains("PSU") {
                    eprintln!(
                        "PSU warning: reported product_id='{product_id}' does not look like a PSU controller; check the COM port."
                    );
                }
            }
            Ok(true)
        })();
        self.opening = false;
        if let Err(error) = result {
            self.open_failed = !self.port_open;
            if io.ctx.is_cancelled() && self.port_open && !self.transport_poisoned {
                // Open may return after OFF was requested, including when the
                // controller was already energised before connecting. Close
                // alone cannot satisfy that pending OFF request.
                self.connected = true;
                let recovery = Self::recovery_context(io.timeout.as_secs_f64(), io.ctx);
                let cleanup = self.shutdown(
                    None,
                    true,
                    true,
                    &Io::new(&recovery, io.timeout.as_secs_f64())?,
                );
                return match cleanup {
                    Ok(_) => Err(error),
                    Err(cleanup) => Err(Error::new(
                        error.kind,
                        format!(
                            "{}; PSU cancelled connect safety cleanup failed: {cleanup}",
                            error.message
                        ),
                    )),
                };
            }
            let was_connected = self.connected;
            if !was_connected && !self.transport_poisoned {
                let recovery = Self::recovery_context(io.timeout.as_secs_f64(), io.ctx);
                let cleanup_io = Io::new(&recovery, io.timeout.as_secs_f64())?;
                if self.write("Close", &[], &cleanup_io).is_ok() {
                    self.port_claimed = false;
                    self.failed_open_released = true;
                }
            }
            self.connected = false;
            return Err(error);
        }
        result
    }

    fn disconnect(&mut self, io: &Io<'_>) -> Result<bool> {
        io.ctx.check_cancelled()?;
        if self.transport_poisoned {
            return Ok(false);
        }
        if !self.connected && !self.port_claimed {
            return Ok(true);
        }
        match self.write("Close", &[], io) {
            Ok(()) => {
                self.connected = false;
                self.port_claimed = false;
                if self.open_failed {
                    self.failed_open_released = true;
                }
                Ok(true)
            }
            Err(error) => {
                self.port_claimed = true;
                if error.kind == "TimeoutError" || io.ctx.is_cancelled() {
                    return Err(error);
                }
                Ok(false)
            }
        }
    }

    fn get_status(&self) -> Value {
        json!({"device_id": self.device_id, "com": self.com, "port": self.port,
            "baudrate": self.baudrate, "connected": self.connected,
            "transport_poisoned": self.transport_poisoned})
    }

    fn get_device_enabled(&mut self, io: &Io<'_>) -> Result<bool> {
        let v = self.read("GetDeviceEnable", &[json!(0)], 1, io)?;
        win_bool(&v[0])
    }

    fn set_device_enabled(&mut self, enabled: bool, io: &Io<'_>) -> Result<()> {
        self.write("SetDeviceEnable", &[json!(i32::from(enabled))], io)
    }

    fn psu_state(&mut self, io: &Io<'_>) -> Result<u64> {
        let v = self.read("GetPSUState", &[json!(0)], 1, io)?;
        word(&v[0], u32::MAX as u64)
    }

    fn get_pair(
        &mut self,
        suffix: &str,
        fallback_bits: Option<[u8; 2]>,
        io: &Io<'_>,
    ) -> Result<[bool; 2]> {
        let reply = self.raw(suffix, &[json!(0), json!(0)], io)?;
        if optional_status(reply.status)
            && let Some(bits) = fallback_bits
        {
            let state = self.psu_state(io)?;
            return Ok(bits.map(|bit| state & (1 << bit) != 0));
        }
        let v = pointer_values(reply, 2, suffix)?;
        Ok([win_bool(&v[0])?, win_bool(&v[1])?])
    }

    fn get_outputs(&mut self, io: &Io<'_>) -> Result<[bool; 2]> {
        self.get_pair("GetPSUEnable", Some([4, 5]), io)
    }
    fn get_ranges(&mut self, io: &Io<'_>) -> Result<[bool; 2]> {
        self.get_pair("GetPSUFullRange", Some([13, 14]), io)
    }

    fn set_pair(&mut self, suffix: &str, pair: [bool; 2], io: &Io<'_>) -> Result<()> {
        self.write(suffix, &pair.map(|b| json!(i32::from(b))), io)
    }

    fn limits(&mut self, channel: usize, quantity: &str, io: &Io<'_>) -> Result<[f64; 2]> {
        let symbol = if quantity == "voltage" {
            "GetPSUSetOutputVoltage"
        } else {
            "GetPSUSetOutputCurrent"
        };
        let v = self.read(symbol, &[json!(channel), json!(0.0), json!(0.0)], 2, io)?;
        Ok([read_number(&v[0], quantity)?, read_number(&v[1], quantity)?])
    }

    fn setpoint(
        &mut self,
        channel: usize,
        quantity: &str,
        value: f64,
        verified_limit: Option<f64>,
        io: &Io<'_>,
    ) -> Result<()> {
        if !value.is_finite() {
            return Err(Error::argument(format!("PSU {quantity} must be finite.")));
        }
        let bounded = if value <= 0.0 {
            0.0
        } else {
            let limit = match verified_limit {
                Some(limit) => limit,
                None => {
                    let result = self.limits(channel, quantity, io);
                    // Cancellation and poisoned transport must not be disguised as a bad limit.
                    io.ctx.check_cancelled()?;
                    self.usable()?;
                    result.map(|v| v[1]).map_err(|e| Error::runtime(format!("Cannot verify PSU {quantity} limit for channel {channel}; refusing a positive setpoint. Zero and OFF remain available. {e}")))?
                }
            };
            if !limit.is_finite() || limit < 0.0 {
                return Err(Error::runtime(format!(
                    "Cannot verify PSU {quantity} limit for channel {channel}; refusing a positive setpoint. Zero and OFF remain available."
                )));
            }
            if value > limit {
                eprintln!(
                    "PSU {quantity} setpoint {value} for channel {channel} clamped to device-reported limit {limit}."
                );
            }
            value.min(limit)
        };
        self.write(
            if quantity == "voltage" {
                "SetPSUOutputVoltage"
            } else {
                "SetPSUOutputCurrent"
            },
            &[json!(channel), json!(bounded)],
            io,
        )
    }

    fn measurements(&mut self, channel: usize, io: &Io<'_>) -> Result<[Value; 3]> {
        let v = self.read(
            "GetPSUData",
            &[json!(channel), json!(0.0), json!(0.0), json!(0.0)],
            3,
            io,
        )?;
        Ok([telemetry(&v[0])?, telemetry(&v[1])?, telemetry(&v[2])?])
    }

    fn load_config(&mut self, config: usize, io: &Io<'_>) -> Result<()> {
        self.write("LoadCurrentConfig", &[json!(config)], io)
    }

    fn set_config_name(&mut self, config: usize, value: &Value, io: &Io<'_>) -> Result<()> {
        let name = if value.is_null() {
            String::new()
        } else if let Some(s) = value.as_str() {
            s.to_owned()
        } else {
            value.to_string()
        };
        let text: String = name
            .chars()
            .map(|c| if c.is_ascii() { c } else { '?' })
            .take(CONFIG_NAME_SIZE - 1)
            .take_while(|c| *c != '\0')
            .collect();
        let v = self.read(
            "SetConfigName",
            &[
                json!(config),
                json!({"capacity": CONFIG_NAME_SIZE, "text": text}),
            ],
            1,
            io,
        )?;
        if !v[0].is_string() {
            return Err(Error::runtime("Invalid PSU configuration name buffer."));
        }
        Ok(())
    }

    fn set_config_flags(
        &mut self,
        config: usize,
        active: bool,
        valid: bool,
        io: &Io<'_>,
    ) -> Result<()> {
        // Vendor config flags alone use one-byte C++ bool, never Windows BOOL.
        self.write(
            "SetConfigFlags",
            &[json!(config), json!(active), json!(valid)],
            io,
        )
    }

    fn config_flags(&mut self, io: &Io<'_>) -> Result<[Vec<bool>; 2]> {
        let reply = self.raw(
            "GetConfigList",
            &[
                json!(vec![false; MAX_CONFIG]),
                json!(vec![false; MAX_CONFIG]),
            ],
            io,
        )?;
        if optional_status(reply.status) {
            let mut active = Vec::with_capacity(MAX_CONFIG);
            let mut valid = Vec::with_capacity(MAX_CONFIG);
            for config in 0..MAX_CONFIG {
                let v = self.read(
                    "GetConfigFlags",
                    &[json!(config), json!(false), json!(false)],
                    2,
                    io,
                )?;
                active.push(c_bool(&v[0])?);
                valid.push(c_bool(&v[1])?);
            }
            return Ok([active, valid]);
        }
        let v = pointer_values(reply, 2, "GetConfigList")?;
        let parse = |value: &Value| -> Result<Vec<bool>> {
            let a = value
                .as_array()
                .filter(|a| a.len() == MAX_CONFIG)
                .ok_or_else(|| {
                    Error::runtime("PSU config flags must contain exactly 168 C bools.")
                })?;
            a.iter().map(c_bool).collect()
        };
        Ok([parse(&v[0])?, parse(&v[1])?])
    }

    fn list_configs(&mut self, include_empty: bool, io: &Io<'_>) -> Result<Value> {
        let [active, valid] = self.config_flags(io)?;
        let mut configs = Vec::new();
        for config in 0..MAX_CONFIG {
            if !include_empty && !active[config] && !valid[config] {
                continue;
            }
            let v = self.read(
                "GetConfigName",
                &[
                    json!(config),
                    json!({"capacity": CONFIG_NAME_SIZE, "text": ""}),
                ],
                1,
                io,
            )?;
            let name = v[0]
                .as_str()
                .ok_or_else(|| Error::runtime("Invalid PSU configuration name readback."))?;
            configs.push(json!({"index": config, "name": name, "active": active[config], "valid": valid[config]}));
        }
        Ok(json!(configs))
    }

    fn optional_metadata(&mut self, suffix: &str, initial: Value, io: &Io<'_>) -> Result<Value> {
        let reply = self.raw(suffix, &[initial], io)?;
        if optional_status(reply.status) {
            return Ok(Value::Null);
        }
        self.validate_metadata(suffix, pointer_values(reply, 1, suffix)?[0].clone())
    }

    fn validate_metadata(&self, suffix: &str, value: Value) -> Result<Value> {
        if matches!(suffix, "GetProductID" | "GetFWDate") {
            if !value.is_string() {
                return Err(Error::runtime(format!(
                    "Invalid PSU {suffix} text readback."
                )));
            }
        } else {
            word(
                &value,
                if matches!(suffix, "GetHWVersion" | "GetFWVersion") {
                    u16::MAX as u64
                } else {
                    u32::MAX as u64
                },
            )?;
        }
        Ok(value)
    }

    fn product_info(&mut self, io: &Io<'_>) -> Result<Value> {
        let v = self.read("GetProductNo", &[json!(0)], 1, io)?;
        let product_no = word(&v[0], u32::MAX as u64)?;
        let product_id = self.optional_metadata(
            "GetProductID",
            json!({"capacity": PRODUCT_ID_SIZE, "text": ""}),
            io,
        )?;
        let fw_version = self.optional_metadata("GetFWVersion", json!(0), io)?;
        let fw_date = self.optional_metadata(
            "GetFWDate",
            json!({"capacity": FW_DATE_SIZE, "text": ""}),
            io,
        )?;
        let hw_type = self.optional_metadata("GetHWType", json!(0), io)?;
        let hw_version = self.optional_metadata("GetHWVersion", json!(0), io)?;
        Ok(json!({"product_no": product_no, "product_id": product_id,
            "firmware": {"version": fw_version, "date": fw_date},
            "hardware": {"type": hw_type, "version": hw_version}}))
    }

    fn doubles(
        &mut self,
        suffix: &str,
        prefix: &[Value],
        count: usize,
        io: &Io<'_>,
    ) -> Result<Vec<Value>> {
        let mut args = prefix.to_vec();
        args.extend((0..count).map(|_| json!(0.0)));
        self.read(suffix, &args, count, io)?
            .iter()
            .map(telemetry)
            .collect()
    }

    fn housekeeping(&mut self, io: &Io<'_>) -> Result<Value> {
        let main = self.read("GetMainState", &[json!(0)], 1, io)?;
        let main = word(&main[0], u16::MAX as u64)?;
        let device = self.read("GetDeviceState", &[json!(0)], 1, io)?;
        let device = word(&device[0], u32::MAX as u64)?;
        let state = self.psu_state(io)?;
        let enabled = self.get_device_enabled(io)?;
        let outputs = self.get_outputs(io)?;
        let support = self.raw("HasPSUFullRange", &[json!(0), json!(0)], io)?;
        let supported = if optional_status(support.status) {
            [false, false]
        } else {
            let v = pointer_values(support, 2, "HasPSUFullRange")?;
            [win_bool(&v[0])?, win_bool(&v[1])?]
        };
        let ranges = self.get_ranges(io)?;
        let h = self.doubles("GetHousekeeping", &[], 4, io)?;
        let sensors = self.read("GetSensorData", &[json!(vec![0.0; SENSOR_COUNT])], 1, io)?;
        let sensors = normalize_array(&sensors[0], SENSOR_COUNT, float(f64::NAN), telemetry)?;
        let fans = self.read(
            "GetFanData",
            &(0..5)
                .map(|_| json!(vec![0; FAN_COUNT]))
                .collect::<Vec<_>>(),
            5,
            io,
        )?;
        let fan_enabled = normalize_array(&fans[0], FAN_COUNT, json!(false), |v| {
            Ok(json!(win_bool(v)?))
        })?;
        let fan_failed = normalize_array(&fans[1], FAN_COUNT, json!(false), |v| {
            Ok(json!(win_bool(v)?))
        })?;
        let fan_set = normalize_array(&fans[2], FAN_COUNT, json!(0), |v| {
            Ok(json!(word(v, u16::MAX as u64)?))
        })?;
        let fan_measured = normalize_array(&fans[3], FAN_COUNT, json!(0), |v| {
            Ok(json!(word(v, u16::MAX as u64)?))
        })?;
        let fan_pwm = normalize_array(&fans[4], FAN_COUNT, json!(0), |v| {
            Ok(json!(word(v, u16::MAX as u64)?))
        })?;
        let led = self.read("GetLEDData", &[json!(0), json!(0), json!(0)], 3, io)?;
        let cpu = self.doubles("GetCPUData", &[], 2, io)?;
        let uptime = self.read("GetUptime", &[json!(0), json!(0), json!(0)], 3, io)?;
        let total = self.read("GetTotalTime", &[json!(0), json!(0)], 2, io)?;
        let mut channels = Vec::new();
        for ch in 0..2 {
            let measured = self.measurements(ch, io)?;
            let voltage = self.limits(ch, "voltage", io)?;
            let current = self.limits(ch, "current", io)?;
            let adc = self.doubles("GetADCHousekeeping", &[json!(ch)], 6, io)?;
            let rails = self.doubles("GetPSUHousekeeping", &[json!(ch)], 4, io)?;
            channels.push(json!({"channel": ch, "label": if ch == 0 { "positive" } else { "negative" },
                "enabled": outputs[ch], "full_range": {"supported": supported[ch], "enabled": ranges[ch]},
                "voltage": {"measured_v": measured[0], "set_v": float(voltage[0]), "limit_v": float(voltage[1])},
                "current": {"measured_a": measured[1], "set_a": float(current[0]), "limit_a": float(current[1])},
                "dropout_v": measured[2], "adc": {"volt_avdd_v": adc[0], "volt_dvdd_v": adc[1],
                    "volt_aldo_v": adc[2], "volt_dldo_v": adc[3], "volt_ref_v": adc[4], "temp_adc_c": adc[5]},
                "rails": {"volt_24vp_v": rails[0], "volt_12vp_v": rails[1], "volt_12vn_v": rails[2], "volt_ref_v": rails[3]}}));
        }
        Ok(
            json!({"device_enabled": enabled, "output_enabled": pair_value(outputs),
            "main_state": {"hex": format!("0x{main:x}"), "name": main_state_name(main)},
            "device_state": {"hex": format!("0x{device:x}"), "flags": device_flags(device)},
            "psu_state": {"hex": format!("0x{state:x}"), "current_limit_active": state & (1 << 12) != 0,
                "interlock_active": state & (1 << 18) != 0, "psu_enabled_actual": state & (1 << 19) != 0,
                "interlock_out_disabled": state & (1 << 8) != 0, "interlock_bnc_disabled": state & (1 << 9) != 0},
            "housekeeping": {"volt_rect_v": h[0], "volt_5v0_v": h[1], "volt_3v3_v": h[2], "temp_cpu_c": h[3]},
            "sensors_c": sensors, "fans": (0..FAN_COUNT).map(|index| json!({"fan": index,
                "enabled": fan_enabled[index], "failed": fan_failed[index], "set_rpm": fan_set[index],
                "measured_rpm": fan_measured[index], "pwm": fan_pwm[index]})).collect::<Vec<_>>(),
            "led": {"red": win_bool(&led[0])?, "green": win_bool(&led[1])?, "blue": win_bool(&led[2])?},
            "cpu": {"load": cpu[0], "frequency_hz": cpu[1]},
            "uptime": {"seconds": word(&uptime[0], u32::MAX as u64)?, "milliseconds": word(&uptime[1], u16::MAX as u64)?,
                "operation_seconds": word(&uptime[2], u32::MAX as u64)?, "total_uptime_seconds": word(&total[0], u32::MAX as u64)?,
                "total_operation_seconds": word(&total[1], u32::MAX as u64)?}, "channels": channels}),
        )
    }

    fn shutdown(
        &mut self,
        standby: Option<usize>,
        disable_outputs: bool,
        disable_device: bool,
        io: &Io<'_>,
    ) -> Result<bool> {
        if standby.is_some() && (disable_outputs || disable_device) {
            return Err(Error::argument(
                "standby_config cannot be combined with disable_outputs or disable_device.",
            ));
        }
        self.usable()?;
        io.ctx.check_cancelled()?;
        if !self.connected {
            return Ok(false);
        }
        let mut errors = Vec::new();
        if let Some(config) = standby {
            record_error(&mut errors, "load_config", self.load_config(config, io));
        }
        if disable_outputs || disable_device {
            for ch in 0..2 {
                record_error(
                    &mut errors,
                    &format!("set_channel_current({ch}, 0.0)"),
                    self.setpoint(ch, "current", 0.0, None, io),
                );
            }
            for ch in 0..2 {
                record_error(
                    &mut errors,
                    &format!("set_channel_voltage({ch}, 0.0)"),
                    self.setpoint(ch, "voltage", 0.0, None, io),
                );
            }
        }
        if disable_outputs || standby.is_some() {
            record_error(
                &mut errors,
                "set_output_enabled(False, False)",
                self.set_pair("SetPSUEnable", [false; 2], io),
            );
        }
        if disable_device || standby.is_some() {
            record_error(
                &mut errors,
                "set_device_enabled(False)",
                self.set_device_enabled(false, io),
            );
        }
        if disable_outputs || standby.is_some() {
            record_error(
                &mut errors,
                "output disable verification",
                self.get_outputs(io).and_then(|v| {
                    if v != [false; 2] {
                        Err(Error::runtime(format!(
                            "PSU outputs were not confirmed OFF: {v:?}"
                        )))
                    } else {
                        Ok(())
                    }
                }),
            );
        }
        if disable_device || standby.is_some() {
            record_error(
                &mut errors,
                "device disable verification",
                self.get_device_enabled(io).and_then(|v| {
                    if v {
                        Err(Error::runtime("PSU device disable was not confirmed"))
                    } else {
                        Ok(())
                    }
                }),
            );
        }
        // A failed OFF must keep communication available for explicit recovery.
        raise_errors("shutdown", errors)?;
        if !self.disconnect(io)? {
            return Err(Error::runtime("PSU disconnect failed during shutdown."));
        }
        Ok(true)
    }

    fn initialize(&mut self, a: &Arguments, io: &Io<'_>) -> Result<Value> {
        let standby = a
            .optional("standby_config")
            .map(config_number)
            .transpose()?;
        let operating = a
            .optional("operating_config")
            .map(config_number)
            .transpose()?;
        if a.optional("cancel").is_some() {
            return Err(Error::argument(
                "PSU cancellation must use the native cancel operation, not a serialized Event.",
            ));
        }
        let result = (|| {
            self.connect(io)?;
            let mut state = Map::new();
            if let Some(config) = standby {
                self.load_config(config, io)?;
                let enabled = self.get_device_enabled(io)?;
                let mut outputs = self.get_outputs(io)?;
                state.insert("standby_config".into(), json!(config));
                state.insert("device_enabled".into(), json!(enabled));
                if a.boolean("require_standby_outputs_disabled", true) && outputs != [false; 2] {
                    state.insert(
                        "standby_output_enabled_before_recovery".into(),
                        pair_value(outputs),
                    );
                    self.set_pair("SetPSUEnable", [false; 2], io)?;
                    outputs = self.get_outputs(io)?;
                    state.insert(
                        "standby_outputs_recovered".into(),
                        json!(outputs == [false; 2]),
                    );
                    if outputs != [false; 2] {
                        return Err(Error::runtime(
                            "PSU standby configuration left outputs enabled even after forcing them OFF. Refusing to continue initialization.",
                        ));
                    }
                }
                state.insert("output_enabled".into(), pair_value(outputs));
            }
            io.ctx.check_cancelled()?;
            if let Some(config) = operating {
                self.load_config(config, io)?;
                state.insert("operating_config".into(), json!(config));
            }
            io.ctx.check_cancelled()?;
            Ok(Value::Object(state))
        })();
        if let Err(error) = result {
            if self.connected && !self.transport_poisoned {
                let recovery = Self::recovery_context(io.timeout.as_secs_f64(), io.ctx);
                let cleanup = self.shutdown(
                    None,
                    true,
                    true,
                    &Io::new(&recovery, io.timeout.as_secs_f64())?,
                );
                if let Err(cleanup) = cleanup {
                    return Err(Error::new(
                        error.kind,
                        format!(
                            "{}; PSU startup safety cleanup failed: {cleanup}",
                            error.message
                        ),
                    ));
                }
            }
            return Err(error);
        }
        result
    }
}

fn normalize_array(
    value: &Value,
    count: usize,
    fill: Value,
    parse: impl Fn(&Value) -> Result<Value>,
) -> Result<Vec<Value>> {
    let source: Vec<&Value> = match value {
        Value::Array(values) => values.iter().collect(),
        Value::Null => Vec::new(),
        value => vec![value],
    };
    (0..count)
        .map(|i| {
            source
                .get(i)
                .map(|v| parse(v))
                .unwrap_or_else(|| Ok(fill.clone()))
        })
        .collect()
}

fn record_error(errors: &mut Vec<String>, step: &str, result: Result<()>) {
    if let Err(error) = result {
        errors.push(format!("{step}: {error}"));
    }
}

fn raise_errors(operation: &str, errors: Vec<String>) -> Result<()> {
    if errors.is_empty() {
        Ok(())
    } else {
        Err(Error::runtime(format!(
            "PSU {operation} completed with {} error(s): {}",
            errors.len(),
            errors.join("; ")
        )))
    }
}

struct ManualState {
    setpoints_only: bool,
    outputs: [bool; 2],
    ranges: [bool; 2],
    voltages: [Option<f64>; 2],
    currents: [Option<f64>; 2],
}

struct RampProfile {
    step_v: f64,
    interval: Duration,
}

impl RampProfile {
    fn from_arguments(a: &Arguments) -> Result<Self> {
        let step_v = a.number("ramp_step_v", DEFAULT_STEP_V)?;
        let step_s = a.number("ramp_step_interval_s", DEFAULT_STEP_S)?;
        if !step_v.is_finite() || step_v <= 0.0 || !step_s.is_finite() || step_s <= 0.0 {
            return Err(Error::argument(
                "PSU ramp step and interval must be finite and positive.",
            ));
        }
        let interval = Duration::try_from_secs_f64(step_s)
            .map_err(|_| Error::argument("PSU ramp interval is too large."))?;
        if interval.is_zero() {
            return Err(Error::argument("PSU ramp interval is too small."));
        }
        Ok(Self { step_v, interval })
    }
}

fn channel_map(value: Option<&Value>, name: &str) -> Result<[Option<Value>; 2]> {
    let mut result = [None, None];
    let Some(value) = value else {
        return Ok(result);
    };
    if value.is_null() {
        return Ok(result);
    }
    let entries = if let Some(pairs) = value.get("$map") {
        value
            .as_object()
            .filter(|v| v.len() == 1)
            .ok_or_else(|| Error::argument(format!("PSU {name} has invalid map encoding.")))?;
        pairs
            .as_array()
            .ok_or_else(|| Error::argument(format!("PSU {name} must be a channel map.")))?
            .iter()
            .map(|pair| {
                let pair = pair.as_array().filter(|p| p.len() == 2).ok_or_else(|| {
                    Error::argument(format!("PSU {name} has invalid channel entry."))
                })?;
                Ok((pair[0].clone(), pair[1].clone()))
            })
            .collect::<Result<Vec<_>>>()?
    } else {
        value
            .as_object()
            .ok_or_else(|| Error::argument(format!("PSU {name} must be a channel map.")))?
            .iter()
            .map(|(key, value)| (json!(key), value.clone()))
            .collect()
    };
    for (key, value) in entries {
        // New native operations accept channel identities, not truncating floats.
        let index = match &key {
            Value::String(s) if s == "0" || s == "1" => s.parse::<usize>().unwrap(),
            Value::Number(n) if n.as_u64().is_some_and(|n| n < 2) => n.as_u64().unwrap() as usize,
            _ => {
                return Err(Error::argument(format!(
                    "PSU {name} channel must be 0 or 1."
                )));
            }
        };
        if result[index].replace(value).is_some() {
            return Err(Error::argument(format!(
                "PSU {name} contains duplicate channel {index}."
            )));
        }
    }
    Ok(result)
}

impl ManualState {
    fn parse(value: &Value) -> Result<Self> {
        let state = value
            .as_object()
            .ok_or_else(|| Error::argument("PSU manual_state must be an object."))?;
        for name in state.keys() {
            if ![
                "setpoints_only",
                "output_enabled",
                "full_range_enabled",
                "voltage_values",
                "current_limit_values",
            ]
            .contains(&name.as_str())
            {
                return Err(Error::argument(format!(
                    "Unknown PSU manual state field: {name}"
                )));
            }
        }
        let setpoints_only = state
            .get("setpoints_only")
            .map(|v| {
                v.as_bool()
                    .ok_or_else(|| Error::argument("PSU setpoints_only must be bool."))
            })
            .transpose()?
            .unwrap_or(false);
        if setpoints_only
            && (state.contains_key("output_enabled") || state.contains_key("full_range_enabled"))
        {
            return Err(Error::argument(
                "PSU setpoints_only cannot request output or range changes.",
            ));
        }
        let masks = |name: &str| -> Result<[bool; 2]> {
            let values = channel_map(state.get(name), name)?;
            let parse = |i: usize| -> Result<bool> {
                values[i]
                    .as_ref()
                    .map(|v| {
                        v.as_bool().ok_or_else(|| {
                            Error::argument(format!("PSU {name} values must be bool."))
                        })
                    })
                    .unwrap_or(Ok(false))
            };
            Ok([parse(0)?, parse(1)?])
        };
        let setpoints = |name: &str| -> Result<[Option<f64>; 2]> {
            let values = channel_map(state.get(name), name)?;
            let parse = |i: usize| -> Result<Option<f64>> {
                let value = values[i].as_ref().map(|v| number(v, name)).transpose()?;
                if value.is_some_and(|v| !v.is_finite() || v < 0.0) {
                    return Err(Error::argument(
                        "PSU setpoints must be finite and non-negative.",
                    ));
                }
                Ok(if setpoints_only {
                    value
                } else {
                    Some(value.unwrap_or(0.0))
                })
            };
            Ok([parse(0)?, parse(1)?])
        };
        let voltages = setpoints("voltage_values")?;
        let currents = setpoints("current_limit_values")?;
        if setpoints_only && voltages.iter().chain(currents.iter()).all(Option::is_none) {
            return Err(Error::argument(
                "PSU setpoint-only update must include at least one voltage/current.",
            ));
        }
        Ok(Self {
            setpoints_only,
            outputs: masks("output_enabled")?,
            ranges: masks("full_range_enabled")?,
            voltages,
            currents,
        })
    }
}

impl Controller {
    fn verify_interlock(&mut self, monitoring: bool, io: &Io<'_>) -> Result<()> {
        let actual = self.get_pair("GetInterlockEnable", None, io)?;
        if actual != [monitoring; 2] {
            return Err(Error::runtime(format!(
                "PSU interlock setting was not confirmed: requested={monitoring}, actual={actual:?}"
            )));
        }
        Ok(())
    }

    fn verify_enables(&mut self, outputs: [bool; 2], io: &Io<'_>) -> Result<()> {
        let enabled = self.get_device_enabled(io)?;
        if enabled != outputs.iter().any(|v| *v) {
            return Err(Error::runtime(
                "PSU device enable readback does not match the requested state.",
            ));
        }
        if self.get_outputs(io)? != outputs {
            return Err(Error::runtime(
                "PSU output-enable readback does not match the requested state.",
            ));
        }
        Ok(())
    }

    fn verify_manual(
        &mut self,
        voltages: [f64; 2],
        currents: [f64; 2],
        ranges: [bool; 2],
        targets: [f64; 2],
        io: &Io<'_>,
    ) -> Result<()> {
        for ch in 0..2 {
            let [voltage, limit] = self.limits(ch, "voltage", io)?;
            validate_voltage_target(ch, targets[ch], limit)?;
            if !voltage.is_finite() || !setpoint_matches(voltage, voltages[ch]) {
                return Err(Error::runtime(format!(
                    "CH{ch} voltage setpoint readback {voltage} does not match requested {}.",
                    voltages[ch]
                )));
            }
            let [current, limit] = self.limits(ch, "current", io)?;
            if currents[ch] > 0.0 && (!limit.is_finite() || limit < 0.0) {
                return Err(Error::runtime(format!(
                    "CH{ch} current hardware limit is invalid."
                )));
            }
            if !current.is_finite() || current < 0.0 {
                return Err(Error::runtime(format!(
                    "CH{ch} current limit readback is invalid."
                )));
            }
            // A lower current clamp is safe; an overshoot is never accepted.
            if current > currents[ch] + CURRENT_ABS_TOLERANCE {
                return Err(Error::runtime(format!(
                    "CH{ch} current limit readback exceeds configured limit."
                )));
            }
        }
        if self.get_ranges(io)? != ranges {
            return Err(Error::runtime(
                "PSU full-range readback does not match the requested state.",
            ));
        }
        Ok(())
    }

    fn recover_outputs(&mut self, io: &Io<'_>) -> Result<()> {
        let mut errors = Vec::new();
        record_error(
            &mut errors,
            "set_output_enabled(False, False)",
            self.set_pair("SetPSUEnable", [false; 2], io),
        );
        record_error(
            &mut errors,
            "set_device_enabled(False)",
            self.set_device_enabled(false, io),
        );
        record_error(
            &mut errors,
            "disable verification",
            self.verify_enables([false; 2], io),
        );
        raise_errors(
            "safety recovery incomplete - outputs may still be live",
            errors,
        )
    }

    fn after_manual_failure(&mut self, error: Error, timeout: f64, ctx: &Context) -> Error {
        if self.transport_poisoned {
            return Error::new(
                error.kind,
                format!(
                    "{}; PSU safety recovery unavailable: transport is unusable; outputs may still be live.",
                    error.message
                ),
            );
        }
        let recovery = Self::recovery_context(timeout, ctx);
        match Io::new(&recovery, timeout).and_then(|io| self.recover_outputs(&io)) {
            Ok(()) => error,
            Err(recovery) => Error::new(error.kind, format!("{}; {recovery}", error.message)),
        }
    }

    fn await_discharge(&mut self, io: &Io<'_>) -> Result<()> {
        let until = Instant::now() + Duration::from_secs_f64(RANGE_SETTLE_S);
        while Instant::now() < until {
            io.ctx.check_cancelled()?;
            let mut discharged = true;
            for ch in 0..2 {
                let measured = self.measurements(ch, io).map_err(|e| {
                    Error::new(
                        e.kind,
                        format!(
                            "Cannot verify PSU discharge before range switch: {}",
                            e.message
                        ),
                    )
                })?;
                let voltage = number(&measured[0], "measured voltage")?;
                discharged &= voltage.is_finite() && voltage.abs() < RANGE_SAFE_V;
            }
            io.ctx.check_cancelled()?;
            if discharged {
                return Ok(());
            }
            io.ctx.sleep(
                Duration::from_millis(100).min(until.saturating_duration_since(Instant::now())),
            )?;
        }
        Err(Error::runtime(format!(
            "PSU outputs did not discharge below {RANGE_SAFE_V} V within {RANGE_SETTLE_S} s; range switch aborted to avoid relay arcing."
        )))
    }

    fn ramp(
        &mut self,
        ch: usize,
        start: f64,
        target: f64,
        profile: &RampProfile,
        io: &Io<'_>,
    ) -> Result<()> {
        let [actual, limit] = self.limits(ch, "voltage", io)?;
        validate_voltage_target(ch, target, limit)?;
        if !actual.is_finite() || actual < 0.0 || !setpoint_matches(actual, start) {
            return Err(Error::runtime(format!(
                "CH{ch} ramp start setpoint readback is invalid or changed."
            )));
        }
        let steps = ((target - start).abs() / profile.step_v).ceil().max(1.0);
        if !steps.is_finite() || steps > 1_000_000.0 {
            return Err(Error::argument("PSU ramp requires too many steps."));
        }
        for step in 1..=steps as u64 {
            io.ctx.check_cancelled()?;
            let value = if step == steps as u64 {
                target
            } else {
                start + (target - start) * step as f64 / steps
            };
            self.setpoint(ch, "voltage", value, Some(limit), io)?;
            io.ctx.progress(json!({"operation": "ramp_channel_voltage", "channel": ch, "set_v": value, "target_v": target, "step": step, "steps": steps as u64}))?;
            io.ctx.sleep(profile.interval)?;
        }
        let [actual, limit] = self.limits(ch, "voltage", io)?;
        validate_voltage_target(ch, target, limit)?;
        if !actual.is_finite() || !setpoint_matches(actual, target) {
            return Err(Error::runtime(format!(
                "CH{ch} ramp target readback does not match requested {target}."
            )));
        }
        Ok(())
    }

    fn apply_in_place(
        &mut self,
        state: &ManualState,
        outputs: [bool; 2],
        ranges: [bool; 2],
        monitoring: bool,
        profile: &RampProfile,
        io: &Io<'_>,
    ) -> Result<()> {
        let mut voltages = [0.0; 2];
        let mut targets = [0.0; 2];
        let mut currents = [0.0; 2];
        let mut current_targets = [0.0; 2];
        for ch in 0..2 {
            let [voltage, voltage_limit] = self.limits(ch, "voltage", io)?;
            let [current, current_limit] = self.limits(ch, "current", io)?;
            if !voltage.is_finite() || voltage < 0.0 || !current.is_finite() || current < 0.0 {
                return Err(Error::runtime(format!(
                    "CH{ch} setpoint readback is invalid."
                )));
            }
            voltages[ch] = voltage;
            currents[ch] = current;
            targets[ch] = state.voltages[ch].unwrap_or(voltage);
            current_targets[ch] = state.currents[ch].unwrap_or(current);
            validate_voltage_target(ch, targets[ch], voltage_limit)?;
            if current_targets[ch] > 0.0 && (!current_limit.is_finite() || current_limit < 0.0) {
                return Err(Error::runtime(format!(
                    "CH{ch} current hardware limit is invalid."
                )));
            }
        }
        if outputs.iter().any(|b| *b) {
            self.verify_interlock(monitoring, io)?;
        }
        for ch in 0..2 {
            if currents[ch] != current_targets[ch] {
                self.setpoint(ch, "current", current_targets[ch], None, io)?;
            }
        }
        self.verify_manual(voltages, current_targets, ranges, targets, io)?;
        for ch in 0..2 {
            if voltages[ch] == targets[ch] {
                continue;
            }
            if outputs[ch] {
                self.ramp(ch, voltages[ch], targets[ch], profile, io)?;
            } else {
                self.setpoint(ch, "voltage", targets[ch], None, io)?;
            }
        }
        self.verify_manual(targets, current_targets, ranges, targets, io)?;
        self.verify_enables(outputs, io)
    }

    fn apply_manual(
        &mut self,
        state: &ManualState,
        monitoring: bool,
        profile: &RampProfile,
        io: &Io<'_>,
    ) -> Result<()> {
        let ranges = self.get_ranges(io)?;
        let enabled = self.get_device_enabled(io)?;
        let outputs = self.get_outputs(io)?;
        io.ctx.check_cancelled()?;
        if state.setpoints_only {
            if enabled != outputs.iter().any(|b| *b) {
                return Err(Error::runtime(
                    "PSU output state is inconsistent; refusing setpoint-only update.",
                ));
            }
            return self.apply_in_place(state, outputs, ranges, monitoring, profile, io);
        }
        if ranges == state.ranges
            && outputs == state.outputs
            && enabled == state.outputs.iter().any(|b| *b)
        {
            return self.apply_in_place(state, outputs, ranges, monitoring, profile, io);
        }
        self.set_pair("SetPSUEnable", [false; 2], io)?;
        if self.get_outputs(io)? != [false; 2] {
            return Err(Error::runtime(
                "PSU outputs were not confirmed disabled before reconfiguration.",
            ));
        }
        if ranges != state.ranges {
            self.await_discharge(io)?;
            self.set_pair("SetPSUFullRange", state.ranges, io)?;
        }
        let targets = state.voltages.map(|v| v.unwrap_or(0.0));
        let currents = state.currents.map(|v| v.unwrap_or(0.0));
        let ramping = [0, 1].map(|ch| state.outputs[ch] && targets[ch] > RAMP_THRESHOLD_V);
        let pre_ramp = [0, 1].map(|ch| if ramping[ch] { 0.0 } else { targets[ch] });
        for ch in 0..2 {
            self.setpoint(ch, "voltage", pre_ramp[ch], None, io)?;
            self.setpoint(ch, "current", currents[ch], None, io)?;
        }
        self.verify_manual(pre_ramp, currents, state.ranges, targets, io)?;
        io.ctx.check_cancelled()?;
        if state.outputs.iter().any(|b| *b) {
            self.set_pair("SetInterlockEnable", [monitoring; 2], io)?;
            self.verify_interlock(monitoring, io)?;
        }
        self.set_device_enabled(state.outputs.iter().any(|b| *b), io)?;
        if state.outputs.iter().any(|b| *b) {
            self.set_pair("SetPSUEnable", state.outputs, io)?;
        }
        self.verify_enables(state.outputs, io)?;
        for ch in 0..2 {
            if ramping[ch] {
                self.ramp(ch, 0.0, targets[ch], profile, io)?;
            }
        }
        self.verify_manual(targets, currents, state.ranges, targets, io)?;
        self.verify_enables(state.outputs, io)?;
        io.ctx.check_cancelled()
    }

    fn standalone_ramp(
        &mut self,
        ch: usize,
        target: f64,
        monitoring: bool,
        profile: &RampProfile,
        io: &Io<'_>,
    ) -> Result<()> {
        if !target.is_finite() || target < 0.0 {
            return Err(Error::argument(
                "PSU ramp target must be finite and non-negative.",
            ));
        }
        let outputs = self.get_outputs(io)?;
        let ranges = self.get_ranges(io)?;
        self.verify_enables(outputs, io)?;
        let mut voltages = [0.0; 2];
        let mut currents = [0.0; 2];
        for index in 0..2 {
            voltages[index] = self.limits(index, "voltage", io)?[0];
            currents[index] = self.limits(index, "current", io)?[0];
            if !voltages[index].is_finite()
                || voltages[index] < 0.0
                || !currents[index].is_finite()
                || currents[index] < 0.0
            {
                return Err(Error::runtime(format!(
                    "CH{index} ramp setpoint readback is invalid."
                )));
            }
        }
        if outputs.iter().any(|v| *v) {
            self.verify_interlock(monitoring, io)?;
        }
        let mut targets = voltages;
        targets[ch] = target;
        self.verify_manual(voltages, currents, ranges, targets, io)?;
        self.ramp(ch, voltages[ch], target, profile, io)?;
        self.verify_manual(targets, currents, ranges, targets, io)?;
        self.verify_enables(outputs, io)
    }
}

fn validate_voltage_target(ch: usize, target: f64, limit: f64) -> Result<()> {
    if target > 0.0 && (!limit.is_finite() || limit < target) {
        return Err(Error::runtime(format!(
            "CH{ch} voltage target exceeds a verified hardware limit."
        )));
    }
    Ok(())
}

fn setpoint_matches(actual: f64, expected: f64) -> bool {
    (actual - expected).abs()
        <= VOLTAGE_ABS_TOLERANCE.max(SETPOINT_REL_TOLERANCE * actual.abs().max(expected.abs()))
}

impl Backend for Controller {
    fn call(
        &mut self,
        method: &str,
        args: &[Value],
        kwargs: &Value,
        ctx: &Context,
    ) -> Result<Value> {
        let (positional, keyword): (&[&str], &[&str]) = match method {
            "connect" => (&["timeout_s"], &[]),
            "initialize" => (
                &["timeout_s"],
                &[
                    "standby_config",
                    "operating_config",
                    "require_standby_outputs_disabled",
                    "cancel",
                ],
            ),
            "disconnect"
            | "get_device_enabled"
            | "get_output_enabled"
            | "get_output_full_range"
            | "get_interlock_enabled"
            | "get_product_info"
            | "collect_housekeeping" => (&["timeout_s"], &[]),
            "get_status" => (&[], &[]),
            "list_configs" => (&["include_empty", "timeout_s"], &[]),
            "load_config" => (&["config_number", "timeout_s"], &[]),
            "save_config" => (
                &["config_number"],
                &["name", "active", "valid", "timeout_s"],
            ),
            "set_config_name" => (&["config_number", "name", "timeout_s"], &[]),
            "set_config_flags" => (&["config_number"], &["active", "valid", "timeout_s"]),
            "set_device_enabled" => (&["enable", "timeout_s"], &[]),
            "set_output_enabled" | "set_output_full_range" => (&["psu0", "psu1", "timeout_s"], &[]),
            "set_interlock_enabled" => (&["connector_output", "connector_bnc", "timeout_s"], &[]),
            "set_channel_voltage" => (&["channel", "voltage_v", "timeout_s"], &[]),
            "set_channel_current" => (&["channel", "current_a", "timeout_s"], &[]),
            "get_channel_voltage"
            | "get_channel_current"
            | "get_channel_voltage_limits"
            | "get_channel_current_limits"
            | "get_channel_measurements"
            | "get_channel_measured_voltage"
            | "get_channel_measured_current" => (&["channel", "timeout_s"], &[]),
            "shutdown" => (
                &[],
                &[
                    "standby_config",
                    "disable_outputs",
                    "disable_device",
                    "timeout_s",
                ],
            ),
            "apply_manual_state" => (
                &["manual_state"],
                &[
                    "interlock_monitoring",
                    "ramp_step_v",
                    "ramp_step_interval_s",
                    "timeout_s",
                ],
            ),
            "ramp_channel_voltage" => (
                &["channel", "target_v"],
                &[
                    "interlock_monitoring",
                    "ramp_step_v",
                    "ramp_step_interval_s",
                    "timeout_s",
                ],
            ),
            _ => {
                return Err(Error::unsupported(format!(
                    "Unsupported PSU operation: {method}"
                )));
            }
        };
        let a = Arguments::bind(method, args, kwargs, positional, keyword)?;
        let timeout = a.timeout()?;
        let io = Io::new(ctx, timeout)?;
        if method == "get_status" {
            return Ok(self.get_status());
        }
        ctx.check_cancelled()?;
        if method == "connect" {
            return Ok(json!(self.connect(&io)?));
        }
        if method == "initialize" {
            return self.initialize(&a, &io);
        }
        if method == "disconnect" {
            return Ok(json!(self.disconnect(&io)?));
        }
        if method == "shutdown" {
            let standby = a
                .optional("standby_config")
                .map(config_number)
                .transpose()?;
            let outputs = a.boolean("disable_outputs", true);
            let device = a.boolean("disable_device", true);
            let result = self.shutdown(standby, outputs, device, &io);
            if let Err(error) = result {
                // Once OFF starts, cancellation must not prevent remaining
                // disable attempts or readback. Never replay a saved config.
                if ctx.is_cancelled()
                    && self.connected
                    && !self.transport_poisoned
                    && (outputs || device || standby.is_some())
                {
                    let recovery = Self::recovery_context(timeout, ctx);
                    let cleanup = self.shutdown(None, true, true, &Io::new(&recovery, timeout)?);
                    return match cleanup {
                        Ok(_) => Err(error),
                        Err(cleanup) => Err(Error::new(
                            error.kind,
                            format!(
                                "{}; PSU cancelled shutdown safety cleanup failed: {cleanup}",
                                error.message
                            ),
                        )),
                    };
                }
                return Err(error);
            }
            return Ok(json!(result?));
        }
        self.require_connected()?;
        ctx.check_cancelled()?;
        match method {
            "list_configs" => self.list_configs(a.boolean("include_empty", false), &io),
            "load_config" => {
                self.load_config(config_number(a.required("config_number")?)?, &io)?;
                Ok(Value::Null)
            }
            "save_config" => {
                let config = config_number(a.required("config_number")?)?;
                self.write("SaveCurrentConfig", &[json!(config)], &io)?;
                if let Some(name) = a.optional("name") {
                    self.set_config_name(config, name, &io)?;
                }
                if a.optional("active").is_some() || a.optional("valid").is_some() {
                    self.set_config_flags(
                        config,
                        a.optional("active").map(truthy).unwrap_or(true),
                        a.optional("valid").map(truthy).unwrap_or(true),
                        &io,
                    )?;
                }
                Ok(Value::Null)
            }
            "set_config_name" => {
                self.set_config_name(
                    config_number(a.required("config_number")?)?,
                    a.required("name")?,
                    &io,
                )?;
                Ok(Value::Null)
            }
            "set_config_flags" => {
                self.set_config_flags(
                    config_number(a.required("config_number")?)?,
                    truthy(a.required("active")?),
                    truthy(a.required("valid")?),
                    &io,
                )?;
                Ok(Value::Null)
            }
            "set_device_enabled" => {
                self.set_device_enabled(truthy(a.required("enable")?), &io)?;
                Ok(Value::Null)
            }
            "get_device_enabled" => Ok(json!(self.get_device_enabled(&io)?)),
            "set_output_enabled" | "set_output_full_range" | "set_interlock_enabled" => {
                let (suffix, first, second) = match method {
                    "set_output_enabled" => ("SetPSUEnable", "psu0", "psu1"),
                    "set_output_full_range" => ("SetPSUFullRange", "psu0", "psu1"),
                    _ => ("SetInterlockEnable", "connector_output", "connector_bnc"),
                };
                self.set_pair(
                    suffix,
                    [truthy(a.required(first)?), truthy(a.required(second)?)],
                    &io,
                )?;
                Ok(Value::Null)
            }
            "get_output_enabled" => Ok(pair_value(self.get_outputs(&io)?)),
            "get_output_full_range" => Ok(pair_value(self.get_ranges(&io)?)),
            "get_interlock_enabled" => Ok(pair_value(self.get_pair(
                "GetInterlockEnable",
                None,
                &io,
            )?)),
            "set_channel_voltage" | "set_channel_current" => {
                let ch = channel(a.required("channel")?)?;
                let (quantity, name) = if method == "set_channel_voltage" {
                    ("voltage", "voltage_v")
                } else {
                    ("current", "current_a")
                };
                self.setpoint(
                    ch,
                    quantity,
                    number(a.required(name)?, quantity)?,
                    None,
                    &io,
                )?;
                Ok(Value::Null)
            }
            "get_channel_voltage" | "get_channel_current" => {
                let ch = channel(a.required("channel")?)?;
                let suffix = if method == "get_channel_voltage" {
                    "GetPSUOutputVoltage"
                } else {
                    "GetPSUOutputCurrent"
                };
                let v = self.read(suffix, &[json!(ch), json!(0.0)], 1, &io)?;
                telemetry(&v[0])
            }
            "get_channel_voltage_limits" | "get_channel_current_limits" => {
                let ch = channel(a.required("channel")?)?;
                let quantity = if method == "get_channel_voltage_limits" {
                    "voltage"
                } else {
                    "current"
                };
                Ok(tuple(
                    self.limits(ch, quantity, &io)?
                        .into_iter()
                        .map(float)
                        .collect(),
                ))
            }
            "get_channel_measurements"
            | "get_channel_measured_voltage"
            | "get_channel_measured_current" => {
                let measured = self.measurements(channel(a.required("channel")?)?, &io)?;
                match method {
                    "get_channel_measured_voltage" => Ok(measured[0].clone()),
                    "get_channel_measured_current" => Ok(measured[1].clone()),
                    _ => Ok(tuple(measured.into_iter().collect())),
                }
            }
            "get_product_info" => self.product_info(&io),
            "collect_housekeeping" => self.housekeeping(&io),
            "apply_manual_state" | "ramp_channel_voltage" => {
                let profile = RampProfile::from_arguments(&a)?;
                let monitoring = a
                    .optional("interlock_monitoring")
                    .map(|v| {
                        v.as_bool().ok_or_else(|| {
                            Error::argument("PSU interlock_monitoring must be bool.")
                        })
                    })
                    .transpose()?
                    .unwrap_or(true);
                let result = if method == "apply_manual_state" {
                    let state = ManualState::parse(a.required("manual_state")?)?;
                    self.apply_manual(&state, monitoring, &profile, &io)
                } else {
                    let ch = channel(a.required("channel")?)?;
                    let target = number(a.required("target_v")?, "ramp target")?;
                    if !target.is_finite() || target < 0.0 {
                        return Err(Error::argument(
                            "PSU ramp target must be finite and non-negative.",
                        ));
                    }
                    self.standalone_ramp(ch, target, monitoring, &profile, &io)
                };
                result.map_err(|error| self.after_manual_failure(error, timeout, ctx))?;
                Ok(Value::Null)
            }
            _ => Err(Error::unsupported(format!(
                "Unsupported PSU operation: {method}"
            ))),
        }
    }

    fn get_attribute(&self, name: &str) -> Result<Value> {
        match name {
            "device_id" | "idn" => Ok(json!(self.device_id)),
            "com" => Ok(json!(self.com)),
            "port" | "port_num" => Ok(json!(self.port)),
            "baudrate" => Ok(json!(self.baudrate)),
            "connected" => Ok(json!(self.connected)),
            "_dll_port_claimed" => Ok(json!(self.port_claimed)),
            "_transport_poisoned" => Ok(json!(self.transport_poisoned)),
            "_transport_error" => Ok(json!(self.transport_error)),
            "_open_failed" => Ok(json!(self.open_failed)),
            "_failed_open_released" => Ok(json!(self.failed_open_released)),
            "_opening_in_progress" => Ok(json!(self.opening)),
            "PSU_POS" => Ok(json!(0)),
            "PSU_NEG" => Ok(json!(1)),
            "PSU_NUM" => Ok(json!(2)),
            "SENSOR_NUM" | "FAN_NUM" => Ok(json!(3)),
            "MAX_PORT" => Ok(json!(16)),
            "MAX_CONFIG" => Ok(json!(MAX_CONFIG)),
            "CONFIG_NAME_SIZE" => Ok(json!(CONFIG_NAME_SIZE)),
            "FW_DATE_SIZE" => Ok(json!(FW_DATE_SIZE)),
            "PRODUCT_ID_SIZE" => Ok(json!(PRODUCT_ID_SIZE)),
            "NO_ERR" => Ok(json!(0)),
            _ => Err(Error::new(
                "AttributeError",
                format!("Unknown PSU attribute: {name}"),
            )),
        }
    }

    fn set_attribute(&mut self, name: &str, value: Value) -> Result<()> {
        match name {
            "device_id" | "idn" => {
                self.device_id = value
                    .as_str()
                    .ok_or_else(|| Error::argument("PSU device_id must be a string."))?
                    .to_owned();
                Ok(())
            }
            "com" | "port" | "port_num" | "baudrate" => {
                if self.connected || self.port_claimed || self.transport_poisoned {
                    return Err(Error::runtime(
                        "Cannot change PSU connection settings while connected or cleanup is unconfirmed.",
                    ));
                }
                match name {
                    "com" => self.com = integer(&value, "com", 1, u16::MAX as u64)? as u16,
                    "port" | "port_num" => self.port = integer(&value, "port", 0, 15)? as u16,
                    _ => self.baudrate = integer(&value, "baudrate", 1, u32::MAX as u64)? as u32,
                }
                Ok(())
            }
            _ => Err(Error::new(
                "AttributeError",
                format!("Read-only or unknown PSU attribute: {name}"),
            )),
        }
    }
}
