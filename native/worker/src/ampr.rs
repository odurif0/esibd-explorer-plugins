//! AMPR-12 facade, retaining Python's one-based channels and integer-keyed maps.
//! GUI: connect/initialize, scan/state/capabilities, voltage get/set, shutdown,
//! disconnect, format_status; attributes NO_ERR and transport/connection flags.
//! Housekeeping is serialized by Backend::tick, never by another DLL thread.

use crate::Backend;
use crate::codec::{float, tuple};
use crate::context::Context;
use crate::error::{Error, Result};
use crate::ffi::{Dll, NativeReply};
use serde_json::{Map, Value, json};
use std::collections::BTreeMap;
use std::time::{Duration, Instant};

pub const GUI_METHODS: &[&str] = &[
    "connect",
    "initialize",
    "get_state",
    "get_device_state",
    "get_voltage_state",
    "get_temperature_state",
    "get_scanned_module_state",
    "rescan_modules",
    "set_scanned_module_state",
    "scan_modules",
    "get_module_capabilities",
    "get_module_voltages",
    "set_module_voltage",
    "set_module_voltages",
    "enable_psu",
    "shutdown",
    "disconnect",
    "format_status",
];
pub const GUI_ATTRIBUTES: &[&str] = &[
    "NO_ERR",
    "connected",
    "_transport_poisoned",
    "_transport_error",
    "_dll_port_claimed",
    "_open_failed",
    "_failed_open_released",
    "_failed_open_cleanup_outcome",
    "_opening_in_progress",
];

const MAIN_STATE: &[(u64, &str)] = &[
    (0, "ST_ON"),
    (1, "ST_OVERLOAD"),
    (2, "ST_STBY"),
    (0x8000, "ST_ERROR"),
    (0x8001, "ST_ERR_MODULE"),
    (0x8002, "ST_ERR_VSUP"),
    (0x8003, "ST_ERR_TEMP_LOW"),
    (0x8004, "ST_ERR_TEMP_HIGH"),
    (0x8005, "ST_ERR_ILOCK"),
    (0x8006, "ST_ERR_PSU_DIS"),
    (0x8007, "ST_ERR_HV_PSU"),
];
const DEVICE_STATE: &[(u64, &str)] = &[
    (1, "DS_PSU_ENB"),
    (1 << 8, "DS_VOLT_FAIL"),
    (1 << 9, "DS_HV_FAIL"),
    (1 << 10, "DS_FAN_FAIL"),
    (1 << 11, "DS_ILOCK_FAIL"),
    (1 << 12, "DS_MODULE_FAIL"),
    (1 << 13, "DS_RATING_FAIL"),
    (1 << 14, "DS_HV_STOP"),
];
const VOLTAGE_STATE: &[(u64, &str)] = &[
    (1, "VS_3V3_OK"),
    (2, "VS_5V0_OK"),
    (4, "VS_12V_OK"),
    (8, "VS_LINE_ON"),
    (16, "VS_12VP_OK"),
    (32, "VS_12VN_OK"),
    (64, "VS_HVP_OK"),
    (128, "VS_HVN_OK"),
    (256, "VS_HVP_NZ"),
    (512, "VS_HVN_NZ"),
    (1 << 15, "VS_ICL_ON"),
];
const TEMPERATURE_STATE: &[(u64, &str)] = &[
    (1, "TS_HVPPSU_HIGH"),
    (2, "TS_HVNPSU_HIGH"),
    (4, "TS_AVPSU_HIGH"),
    (8, "TS_TADC_HIGH"),
    (16, "TS_TCPU_HIGH"),
    (256, "TS_HVPPSU_LOW"),
    (512, "TS_HVNPSU_LOW"),
    (1024, "TS_AVPSU_LOW"),
    (2048, "TS_TADC_LOW"),
    (4096, "TS_TCPU_LOW"),
];
const INTERLOCK_STATE: &[(u64, &str)] = &[
    (1, "SI_ILOCK_FRONT_ENB"),
    (2, "SI_ILOCK_REAR_ENB"),
    (4, "SI_ILOCK_FRONT_INV"),
    (8, "SI_ILOCK_REAR_INV"),
    (256, "SI_ILOCK_FRONT"),
    (512, "SI_ILOCK_REAR"),
    (1024, "SI_ILOCK_FRONT_LAST"),
    (2048, "SI_ILOCK_REAR_LAST"),
    (1 << 15, "SI_ILOCK_ENB"),
];

#[derive(Clone, Copy, Default)]
struct Capabilities {
    rating: Option<u64>,
    channels: Option<u64>,
}

pub struct Controller {
    dll: Box<dyn Dll>,
    device_id: String,
    com: u64,
    baudrate: u64,
    connected: bool,
    port_claimed: bool,
    open_failed: bool,
    failed_open_released: bool,
    failed_open_cleanup_outcome: Value,
    poisoned: bool,
    transport_error: Option<String>,
    hk_running: bool,
    hk_interval_s: f64,
    next_hk: Instant,
    housekeeping: Value,
    capabilities: BTreeMap<u64, Capabilities>,
    accepted: BTreeMap<(u64, u64), f64>,
    errors: Value,
}

impl Controller {
    pub fn new(config: &Value, dll: Box<dyn Dll>) -> Result<Self> {
        let device_id = config
            .get("device_id")
            .and_then(Value::as_str)
            .ok_or_else(|| Error::new("TypeError", "AMPR device_id must be a string"))?;
        if device_id.trim().is_empty() {
            return Err(Error::argument("AMPR device_id must not be empty"));
        }
        let com = integer(config.get("com").unwrap_or(&Value::Null), "com", 1, 255)?;
        let baudrate = integer(
            config.get("baudrate").unwrap_or(&json!(230400)),
            "baudrate",
            1,
            u32::MAX as u64,
        )?;
        let hk_interval_s = positive(
            config.get("hk_interval_s").unwrap_or(&json!(5.0)),
            "hk_interval_s",
        )?;
        Ok(Self {
            dll,
            device_id: device_id.into(),
            com,
            baudrate,
            connected: false,
            port_claimed: false,
            open_failed: false,
            poisoned: false,
            transport_error: None,
            failed_open_released: false,
            failed_open_cleanup_outcome: Value::Null,
            hk_running: false,
            hk_interval_s,
            next_hk: Instant::now(),
            housekeeping: Value::Null,
            capabilities: BTreeMap::new(),
            accepted: BTreeMap::new(),
            errors: serde_json::from_str(crate::ERROR_CATALOG)
                .map_err(|e| Error::runtime(format!("Invalid bundled error catalog: {e}")))?,
        })
    }

    fn native(
        &mut self,
        export: &str,
        args: &[Value],
        count: usize,
        ctx: &Context,
    ) -> Result<NativeReply> {
        ctx.check_cancelled()?;
        if self.poisoned {
            return Err(Error::runtime(
                "AMPR transport unusable; recreate worker. Hardware OFF is not confirmed.",
            ));
        }
        let symbol = format!("COM_AMPR_12_{export}");
        let reply = ctx.native_call(&symbol, None, || self.dll.call(&symbol, args));
        if let Err(error) = &reply
            && error.kind == "TimeoutError"
        {
            self.poisoned = true;
            self.port_claimed = true;
            self.transport_error = Some(error.message.clone());
            self.hk_running = false;
        }
        let reply = reply?;
        if let Err(error) = ctx.check_cancelled() {
            if error.kind == "TimeoutError" {
                self.poisoned = true;
                self.port_claimed = true;
                self.transport_error = Some(error.message.clone());
                self.hk_running = false;
            }
            return Err(error);
        }
        if reply.values.len() != count {
            return Err(Error::runtime(format!(
                "AMPR {export}: expected {count} pointer values, got {}",
                reply.values.len()
            )));
        }
        Ok(reply)
    }

    fn read(
        &mut self,
        export: &str,
        mut inputs: Vec<Value>,
        seeds: Vec<Value>,
        ctx: &Context,
    ) -> Result<NativeReply> {
        let count = seeds.len();
        inputs.extend(seeds);
        self.native(export, &inputs, count, ctx)
    }

    fn write(&mut self, export: &str, inputs: &[Value], ctx: &Context) -> Result<i64> {
        Ok(self.native(export, inputs, 0, ctx)?.status)
    }

    fn connected(&self) -> Result<()> {
        if !self.connected {
            return Err(Error::runtime("AMPR device is not connected"));
        }
        if self.poisoned {
            return Err(Error::runtime(
                "AMPR transport unusable; OFF is unconfirmed",
            ));
        }
        Ok(())
    }

    fn connect(&mut self, ctx: &Context) -> Result<bool> {
        if self.poisoned {
            return Err(Error::runtime("AMPR transport unusable; recreate worker"));
        }
        if self.connected {
            return Ok(true);
        }
        if self.port_claimed {
            return Err(Error::runtime(
                "Previous AMPR open has not been released; disconnect first",
            ));
        }
        // A failed Open may still own a native serial handle. Retain its claim until Close succeeds.
        self.failed_open_released = false;
        self.failed_open_cleanup_outcome = Value::Null;
        self.port_claimed = true;
        self.open_failed = true;
        let outcome = (|| {
            ensure(self.write("Open", &[json!(self.com)], ctx)?, "open_port")?;
            self.connected = true;
            let baud = self.read("SetBaudRate", vec![], vec![json!(self.baudrate)], ctx)?;
            ensure(baud.status, "set_baud_rate")?;
            let identity = self.read("GetDevType", vec![], vec![json!(0)], ctx)?;
            ensure(identity.status, "get_device_type")?;
            if output_u64(&identity, 0)? != 0xA3D8 {
                return Err(Error::runtime(format!(
                    "AMPR device type mismatch: expected 0xA3D8, got 0x{:04X}",
                    output_u64(&identity, 0)?
                )));
            }
            Ok(true)
        })();
        if outcome.is_err() {
            self.connected = false;
            if !self.poisoned {
                let cleanup = ctx.cleanup(Duration::from_secs(5));
                let closed = self.write("Close", &[], &cleanup);
                self.failed_open_cleanup_outcome = match &closed {
                    Ok(status) => tuple(vec![json!("result"), json!(status)]),
                    Err(error) => tuple(vec![json!("error"), json!(error.to_string())]),
                };
                if closed.is_ok_and(|status| status == 0) {
                    self.port_claimed = false;
                    self.open_failed = false;
                    self.failed_open_released = true;
                }
            }
        } else {
            self.open_failed = false;
        }
        outcome
    }

    fn disconnect(&mut self, ctx: &Context) -> Result<bool> {
        self.hk_running = false;
        if self.poisoned {
            return Ok(false);
        }
        if !self.port_claimed && !self.connected {
            return Ok(true);
        }
        let closed = self.write("Close", &[], ctx);
        if self.open_failed {
            self.failed_open_cleanup_outcome = match &closed {
                Ok(status) => tuple(vec![json!("result"), json!(status)]),
                Err(error) => tuple(vec![json!("error"), json!(error.to_string())]),
            };
            self.failed_open_released = closed.as_ref().is_ok_and(|status| *status == 0);
        }
        let status = closed?;
        if status != 0 {
            return Ok(false);
        }
        self.connected = false;
        self.port_claimed = false;
        self.open_failed = false;
        self.accepted.clear();
        self.capabilities.clear();
        Ok(true)
    }

    fn state(&mut self, export: &str, ctx: &Context) -> Result<Value> {
        let reply = self.read(export, vec![], vec![json!(0)], ctx)?;
        if reply.status != 0 {
            return Ok(tuple(vec![json!(reply.status), Value::Null, Value::Null]));
        }
        let raw = output_u64(&reply, 0)?;
        let label = if export == "GetState" {
            json!(
                MAIN_STATE
                    .iter()
                    .find(|(n, _)| *n == raw)
                    .map(|(_, s)| s.to_string())
                    .unwrap_or_else(|| format!("UNKNOWN_STATE_0x{raw:04X}"))
            )
        } else {
            let (flags, ok) = match export {
                "GetDeviceState" => (DEVICE_STATE, Some("DEVICE_OK")),
                "GetVoltageState" => (VOLTAGE_STATE, Some("VOLTAGE_OK")),
                "GetTemperatureState" => (TEMPERATURE_STATE, Some("TEMPERATURE_OK")),
                "GetInterlockState" => (INTERLOCK_STATE, None),
                _ => return Err(Error::unsupported("Unknown AMPR state")),
            };
            flag_names(raw, flags, ok)
        };
        Ok(tuple(vec![
            json!(reply.status),
            json!(format!("0x{raw:x}")),
            label,
        ]))
    }

    fn initialize(&mut self, timeout: f64, poll: f64, ctx: &Context) -> Result<()> {
        if self.connected {
            let state = self.read("GetState", vec![], vec![json!(0)], ctx)?;
            if state.status == 0 && output_u64(&state, 0)? == 0 {
                return Ok(());
            }
        } else {
            self.connect(ctx)?;
        }
        let mut enable_attempted = false;
        let result = (|| {
            let scan = self.read("GetScannedModuleState", vec![], vec![json!(false); 2], ctx)?;
            ensure(scan.status, "get_scanned_module_state")?;
            if output_bool(&scan, 0)? || output_bool(&scan, 1)? {
                ensure(self.write("RescanModules", &[], ctx)?, "rescan_modules")?;
                ensure(
                    self.write("SetScannedModuleState", &[], ctx)?,
                    "set_scanned_module_state",
                )?;
            }
            enable_attempted = true;
            let enabled = self.read("EnablePSU", vec![], vec![json!(true)], ctx)?;
            ensure(enabled.status, "enable_psu")?;
            if !output_bool(&enabled, 0)? {
                return Err(Error::runtime("AMPR PSU enable was not confirmed"));
            }
            let deadline = Instant::now() + duration(timeout)?;
            loop {
                let state = self.read("GetState", vec![], vec![json!(0)], ctx)?;
                if state.status == 0 && output_u64(&state, 0)? == 0 {
                    return Ok(());
                }
                if Instant::now() >= deadline {
                    return Err(Error::runtime("AMPR did not reach ST_ON"));
                }
                ctx.sleep(duration(poll)?.min(deadline.saturating_duration_since(Instant::now())))?;
            }
        })();
        if result.is_err() && enable_attempted && !self.poisoned {
            // Cancellation of ramp/startup must not suppress a bounded OFF attempt.
            let cleanup = ctx.cleanup(Duration::from_secs(5));
            match self.read("EnablePSU", vec![], vec![json!(false)], &cleanup) {
                Ok(reply) if reply.status == 0 && reply.values[0] == json!(false) => (),
                _ => {
                    self.transport_error = Some(
                        "Startup cleanup did not confirm PSU disable; retry explicit OFF".into(),
                    )
                }
            }
        }
        result
    }

    fn presence(&mut self, ctx: &Context) -> Result<NativeReply> {
        let reply = self.read(
            "GetModulePresence",
            vec![],
            vec![json!(false), json!(0), json!(vec![0; 13])],
            ctx,
        )?;
        array(&reply.values[2], 13, "AMPR module presence")?;
        Ok(reply)
    }

    fn module_metadata(&mut self, address: u64, ctx: &Context) -> Result<Value> {
        let mut info = Map::new();
        for (key, export, seed) in [
            ("fw_version", "GetModuleFwVersion", json!(0)),
            ("product_no", "GetModuleProductNo", json!(0)),
            ("product_id", "GetModuleProductID", buffer(81, "")),
            ("hw_type", "GetModuleHwType", json!(0)),
            ("hw_version", "GetModuleHwVersion", json!(0)),
            ("state", "GetModuleState", json!(0)),
        ] {
            let reply = self.read(export, vec![json!(address)], vec![seed], ctx)?;
            if reply.status == 0 {
                info.insert(key.into(), reply.values[0].clone());
            }
        }
        let cap = resolve_capabilities(&Value::Object(info.clone()));
        self.capabilities.insert(address, cap);
        Ok(Value::Object(info))
    }

    fn scan(&mut self, ctx: &Context) -> Result<BTreeMap<u64, Value>> {
        let presence = self.presence(ctx)?;
        ensure(presence.status, "scan_modules")?;
        let flags = array(&presence.values[2], 13, "AMPR module presence")?;
        let max = output_u64(&presence, 1)?.min(11);
        let addresses: Vec<_> = (0..=max)
            .filter(|n| flags[*n as usize] == json!(1))
            .collect();
        self.capabilities.clear();
        let mut modules = BTreeMap::new();
        for address in addresses {
            modules.insert(address, self.module_metadata(address, ctx)?);
        }
        Ok(modules)
    }

    fn module_capabilities(&mut self, address: u64, ctx: &Context) -> Result<Value> {
        let id = self.read(
            "GetModuleProductID",
            vec![json!(address)],
            vec![buffer(81, "")],
            ctx,
        )?;
        let no = self.read(
            "GetModuleProductNo",
            vec![json!(address)],
            vec![json!(0)],
            ctx,
        )?;
        let hw = self.read("GetModuleHwType", vec![json!(address)], vec![json!(0)], ctx)?;
        let status = [no.status, hw.status]
            .into_iter()
            .find(|s| *s != 0)
            .unwrap_or(id.status);
        let mut info = json!({"status": status, "product_id": id.values[0], "product_no": no.values[0], "hw_type": hw.values[0]});
        let cap = resolve_capabilities(&info);
        info["voltage_rating"] = json!(cap.rating);
        info["channel_count"] = json!(cap.channels);
        self.capabilities.insert(address, cap);
        Ok(info)
    }

    fn valid_voltage(&self, address: u64, channel: u64, voltage: f64) -> bool {
        let cap = self.capabilities.get(&address).copied().unwrap_or_default();
        address < 12
            && channel >= 1
            && channel <= cap.channels.unwrap_or(4)
            && voltage.is_finite()
            && voltage.abs() <= cap.rating.unwrap_or(1000).min(1000) as f64
    }

    fn set_voltage(
        &mut self,
        address: u64,
        channel: u64,
        voltage: f64,
        ctx: &Context,
    ) -> Result<i64> {
        if !self.valid_voltage(address, channel, voltage) {
            return Ok(-15);
        }
        let status = self.write(
            "SetModuleOutputVoltage",
            &[json!(address), json!(channel - 1), float(voltage)],
            ctx,
        )?;
        if status == 0 {
            self.accepted.insert((address, channel), voltage);
        }
        Ok(status)
    }

    fn measured(&mut self, address: u64, ctx: &Context) -> Result<NativeReply> {
        let reply = self.read(
            "GetMeasuredModuleOutputVoltages",
            vec![json!(address)],
            vec![json!(vec![0.0; 4])],
            ctx,
        )?;
        array(&reply.values[0], 4, "AMPR measured voltages")?;
        Ok(reply)
    }

    fn voltages(&mut self, address: u64, ctx: &Context) -> Result<Value> {
        let measured = self.measured(address, ctx)?;
        let mut pairs = Vec::new();
        for channel in 1..=4 {
            let setpoint = self.read(
                "GetModuleOutputVoltage",
                vec![json!(address), json!(channel - 1)],
                vec![json!(0.0)],
                ctx,
            )?;
            if setpoint.status == 0 || measured.status == 0 {
                pairs.push((channel, json!({
                    "setpoint": if setpoint.status == 0 { setpoint.values[0].clone() } else { Value::Null },
                    "measured": if measured.status == 0 { measured.values[0][(channel - 1) as usize].clone() } else { Value::Null },
                })));
            }
        }
        Ok(int_map(pairs))
    }

    fn module_info(&mut self, address: u64, ctx: &Context) -> Result<Value> {
        let mut info = self.module_metadata(address, ctx)?;
        let hk = self.read(
            "GetModuleHousekeeping",
            vec![json!(address)],
            vec![json!(0.0); 7],
            ctx,
        )?;
        if hk.status == 0 {
            info["housekeeping"] = named_values(
                &[
                    "volt_3v3",
                    "temp_cpu",
                    "volt_5v0",
                    "volt_12vp",
                    "volt_12vn",
                    "volt_1v8p",
                    "volt_1v8n",
                ],
                &hk.values,
            );
        }
        info["voltages"] = self.voltages(address, ctx)?;
        let cap = resolve_capabilities(&info);
        if let Some(rating) = cap.rating {
            info["voltage_rating"] = json!(rating);
        }
        if let Some(channels) = cap.channels {
            info["channel_count"] = json!(channels);
        }
        Ok(info)
    }

    fn shutdown(&mut self, ctx: &Context) -> Result<bool> {
        self.connected()?;
        self.hk_running = false;
        let mut errors = Vec::new();
        let modules = match self.scan(ctx) {
            Ok(modules) => modules,
            Err(error) => {
                errors.push(format!("scan_modules: {error}"));
                BTreeMap::new()
            }
        };
        for (address, info) in modules {
            let cap = resolve_capabilities(&info);
            for channel in 1..=cap.channels.unwrap_or(4) {
                match self.set_voltage(address, channel, 0.0, ctx) {
                    Ok(0) => (),
                    Ok(status) => errors.push(format!(
                        "zero module {address} CH{channel}: status {status}"
                    )),
                    Err(error) => {
                        errors.push(format!("zero module {address} CH{channel}: {error}"))
                    }
                }
            }
        }
        match self.read("EnablePSU", vec![], vec![json!(false)], ctx) {
            Ok(reply) if reply.status == 0 && reply.values[0] == json!(false) => (),
            Ok(reply) => errors.push(format!(
                "PSU disable unconfirmed: status {}, enabled={}",
                reply.status, reply.values[0]
            )),
            Err(error) => errors.push(format!("PSU disable: {error}")),
        }
        // Do not equate closed transport with OFF; retain it for an explicit retry.
        if !errors.is_empty() {
            return Err(Error::runtime(format!(
                "AMPR shutdown sequence reported errors: {}",
                errors.join("; ")
            )));
        }
        if !self.disconnect(ctx)? {
            return Err(Error::runtime(
                "AMPR disconnect failed after shutdown; retry explicit OFF",
            ));
        }
        Ok(true)
    }

    fn housekeeping(&mut self, ctx: &Context) -> Result<()> {
        self.connected()?;
        let mut snapshot = Map::new();
        for method in [
            "get_product_no",
            "get_state",
            "get_device_state",
            "get_housekeeping",
            "get_voltage_state",
            "get_temperature_state",
            "get_interlock_state",
            "get_fan_data",
            "get_led_data",
            "get_cpu_data",
            "get_module_presence",
        ] {
            snapshot.insert(
                method.into(),
                self.low_level(method, &[], &Value::Null, ctx)?
                    .ok_or_else(|| Error::unsupported(method))?,
            );
        }
        self.housekeeping = Value::Object(snapshot);
        Ok(())
    }

    fn due_housekeeping(&mut self, ctx: &Context) -> Result<()> {
        if self.hk_running && self.connected && Instant::now() >= self.next_hk {
            self.next_hk = Instant::now() + duration(self.hk_interval_s)?;
            self.housekeeping(ctx)?;
        }
        Ok(())
    }

    fn ramp(
        &mut self,
        address: u64,
        targets: &Value,
        rates: &Value,
        ctx: &Context,
    ) -> Result<Value> {
        self.connected()?;
        self.module_capabilities(address, ctx)?;
        let targets = channel_map(targets)?;
        let rate_map = if rates.is_object() || rates.get("$map").is_some() {
            Some(channel_map(rates)?)
        } else {
            None
        };
        let mut current = BTreeMap::new();
        let mut speeds = BTreeMap::new();
        for (&channel, &target) in &targets {
            let rate = match &rate_map {
                Some(map) => *map
                    .get(&channel)
                    .ok_or_else(|| Error::argument("Missing per-channel ramp rate"))?,
                None => number(rates, "rates_v_s")?,
            };
            if !rate.is_finite() || rate < 0.0 {
                return Err(Error::argument("Ramp rate must be finite and nonnegative"));
            }
            if !self.valid_voltage(address, channel, target) {
                return Err(Error::argument(
                    "AMPR ramp target exceeds module/channel rating",
                ));
            }
            let readback = self.read(
                "GetModuleOutputVoltage",
                vec![json!(address), json!(channel - 1)],
                vec![json!(0.0)],
                ctx,
            )?;
            ensure(readback.status, "ramp setpoint readback")?;
            let start = number(&readback.values[0], "ramp start readback")?;
            if !self.valid_voltage(address, channel, start) {
                return Err(Error::runtime("Invalid AMPR ramp start readback"));
            }
            current.insert(channel, start);
            speeds.insert(channel, rate);
        }
        let mut last = Instant::now();
        loop {
            ctx.check_cancelled()?;
            let state = self.read("GetState", vec![], vec![json!(0)], ctx)?;
            ensure(state.status, "ramp get_state")?;
            if output_u64(&state, 0)? != 0 {
                return Err(Error::runtime(
                    "AMPR left ST_ON during ramp; interlock/state prevents further writes",
                ));
            }
            let now = Instant::now();
            let elapsed = now.duration_since(last).as_secs_f64();
            last = now;
            for (&channel, &target) in &targets {
                let previous = current[&channel];
                let distance = target - previous;
                if distance == 0.0 {
                    continue;
                }
                let step = speeds[&channel] * elapsed;
                let next = if speeds[&channel] == 0.0 || distance.abs() <= step + 1e-9 {
                    target
                } else {
                    previous + distance.signum() * step
                };
                ensure(
                    self.set_voltage(address, channel, next, ctx)?,
                    "ramp voltage write",
                )?;
                current.insert(channel, next);
            }
            let reached = targets
                .iter()
                .all(|(channel, target)| current[channel] == *target);
            if reached {
                for (&channel, &target) in &targets {
                    let reply = self.read(
                        "GetModuleOutputVoltage",
                        vec![json!(address), json!(channel - 1)],
                        vec![json!(0.0)],
                        ctx,
                    )?;
                    ensure(reply.status, "ramp final setpoint readback")?;
                    if (number(&reply.values[0], "setpoint readback")? - target).abs() > 0.05 {
                        return Err(Error::runtime(format!(
                            "AMPR CH{channel} final setpoint mismatch"
                        )));
                    }
                }
                return self.voltages(address, ctx);
            }
            ctx.progress(json!({"kind": "ramp", "module": address, "targets": int_map(current.iter().map(|(c, v)| (*c, float(*v))).collect())}))?;
            ctx.sleep(Duration::from_millis(50))?;
        }
    }

    fn low_level(
        &mut self,
        method: &str,
        args: &[Value],
        kwargs: &Value,
        ctx: &Context,
    ) -> Result<Option<Value>> {
        let (export, names, seeds): (&str, &[&str], Vec<Value>) = match method {
            "get_fw_version" => ("GetFwVersion", &[], vec![json!(0)]),
            "get_fw_date" => ("GetFwDate", &[], vec![buffer(12, "")]),
            "get_product_id" => ("GetProductID", &[], vec![buffer(81, "")]),
            "get_product_no" => ("GetProductNo", &[], vec![json!(0)]),
            "get_manuf_date" => ("GetManufDate", &[], vec![json!(0); 2]),
            "get_device_type" => ("GetDevType", &[], vec![json!(0)]),
            "get_hw_type" => ("GetHwType", &[], vec![json!(0)]),
            "get_hw_version" => ("GetHwVersion", &[], vec![json!(0)]),
            "get_uptime" => ("GetUptime", &[], vec![json!(0); 4]),
            "get_optime" => ("GetOptime", &[], vec![json!(0); 4]),
            "get_cpu_data" => ("GetCPUdata", &[], vec![json!(0.0); 2]),
            "get_housekeeping" => ("GetHousekeeping", &[], vec![json!(0.0); 14]),
            "get_buffer_state" => ("GetBufferState", &[], vec![json!(false)]),
            "device_purge" => ("DevicePurge", &[], vec![json!(false)]),
            "get_scanned_module_state" => ("GetScannedModuleState", &[], vec![json!(false); 2]),
            "get_inputs" => ("GetInputs", &[], vec![json!(false); 3]),
            "get_sync_control" => ("GetSyncControl", &[], vec![json!(false); 3]),
            "get_led_data" => ("GetLEDData", &[], vec![json!(false); 3]),
            "get_fan_data" => (
                "GetFanData",
                &[],
                vec![json!(false), json!(0), json!(0), json!(0), json!(0)],
            ),
            "get_module_fw_version" => ("GetModuleFwVersion", &["address"], vec![json!(0)]),
            "get_module_hw_version" => ("GetModuleHwVersion", &["address"], vec![json!(0)]),
            "get_module_state" => ("GetModuleState", &["address"], vec![json!(0)]),
            "get_module_housekeeping" => {
                ("GetModuleHousekeeping", &["address"], vec![json!(0.0); 7])
            }
            "purge" => ("Purge", &[], vec![]),
            "restart" => ("Restart", &[], vec![]),
            "update_module_presence" => ("UpdateModulePresence", &[], vec![]),
            "rescan_modules" => ("RescanModules", &[], vec![]),
            "set_scanned_module_state" => ("SetScannedModuleState", &[], vec![]),
            "rescan_module" => ("RescanModule", &["address"], vec![]),
            "restart_module" => ("RestartModule", &["address"], vec![]),
            "set_baud_rate" => ("SetBaudRate", &["baud_rate"], vec![]),
            "enable_psu" => ("EnablePSU", &["enable"], vec![]),
            "set_interlock_state" => ("SetInterlockState", &["interlock_control"], vec![]),
            "set_sync_control" => ("SetSyncControl", &["external", "invert", "level"], vec![]),
            "get_state"
            | "get_device_state"
            | "get_voltage_state"
            | "get_temperature_state"
            | "get_interlock_state" => {
                Params::new(args, kwargs, &["timeout_s"], &[])?;
                let export = match method {
                    "get_state" => "GetState",
                    "get_device_state" => "GetDeviceState",
                    "get_voltage_state" => "GetVoltageState",
                    "get_temperature_state" => "GetTemperatureState",
                    _ => "GetInterlockState",
                };
                return Ok(Some(self.state(export, ctx)?));
            }
            "get_module_presence" => {
                Params::new(args, kwargs, &[], &["timeout_s"])?;
                return Ok(Some(reply_tuple(self.presence(ctx)?)));
            }
            "get_sw_version" => {
                Params::new(args, kwargs, &[], &[])?;
                return Ok(Some(json!(
                    self.native("GetSWVersion", &[], 0, ctx)?.status
                )));
            }
            _ => return Ok(None),
        };
        let mut positional = names.to_vec();
        if matches!(
            method,
            "enable_psu"
                | "restart"
                | "get_scanned_module_state"
                | "rescan_modules"
                | "set_scanned_module_state"
        ) {
            positional.push("timeout_s");
        }
        let params = Params::new(args, kwargs, &positional, &["timeout_s"])?;
        let mut inputs = Vec::new();
        for name in names {
            let value = params.required(name)?;
            inputs.push(match *name {
                "address" => json!(integer(value, name, 0, 11)?),
                "baud_rate" => json!(integer(value, name, 1, u32::MAX as u64)?),
                "interlock_control" => json!(integer(value, name, 0, 255)?),
                _ => json!(boolean(value, name)?),
            });
        }
        if matches!(method, "set_baud_rate" | "enable_psu") {
            return Ok(Some(reply_tuple(self.read(export, vec![], inputs, ctx)?)));
        }
        if seeds.is_empty() {
            Ok(Some(json!(self.write(export, &inputs, ctx)?)))
        } else {
            Ok(Some(reply_tuple(self.read(export, inputs, seeds, ctx)?)))
        }
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
        ctx.check_cancelled()?;
        // OFF must not wait for a due housekeeping batch.
        if !matches!(
            method,
            "shutdown"
                | "disconnect"
                | "stop_housekeeping"
                | "hk_monitor"
                | "do_housekeeping_cycle"
        ) {
            self.due_housekeeping(ctx)?;
        }
        if let Some(value) = self.low_level(method, args, kwargs, ctx)? {
            return Ok(value);
        }
        match method {
            "connect" | "disconnect" | "shutdown" => {
                let p = Params::new(
                    args,
                    kwargs,
                    if method == "disconnect" {
                        &[]
                    } else {
                        &["timeout_s"]
                    },
                    &[],
                )?;
                let _ = p.optional_positive("timeout_s", 5.0)?;
                Ok(json!(match method {
                    "connect" => self.connect(ctx)?,
                    "disconnect" => self.disconnect(ctx)?,
                    _ => self.shutdown(ctx)?,
                }))
            }
            "initialize" => {
                let p = Params::new(args, kwargs, &["timeout_s", "poll_s"], &[])?;
                self.initialize(
                    p.optional_positive("timeout_s", 5.0)?,
                    p.optional_positive("poll_s", 0.2)?,
                    ctx,
                )?;
                Ok(Value::Null)
            }
            "scan_modules" | "scan_all_modules" => {
                Params::new(args, kwargs, &["timeout_s"], &[])?;
                Ok(int_map(self.scan(ctx)?.into_iter().collect()))
            }
            "get_module_capabilities"
            | "get_module_voltage_rating"
            | "get_module_channel_count"
            | "get_module_product_id"
            | "get_module_product_no"
            | "get_module_hw_type"
            | "get_scanned_module_params"
            | "get_module_info" => {
                let p = Params::new(args, kwargs, &["address", "timeout_s"], &[])?;
                let explicit = p.get("address").filter(|v| !v.is_null());
                let addresses = match explicit {
                    Some(v) => vec![integer(v, "address", 0, 11)?],
                    None => self.scan(ctx)?.keys().copied().collect(),
                };
                let mut values = Vec::new();
                for address in addresses {
                    let value = match method {
                        "get_module_info" => self.module_info(address, ctx)?,
                        "get_module_capabilities" => self.module_capabilities(address, ctx)?,
                        "get_module_voltage_rating" | "get_module_channel_count" => {
                            let cap = self.module_capabilities(address, ctx)?;
                            let field = if method == "get_module_voltage_rating" {
                                "voltage_rating"
                            } else {
                                "channel_count"
                            };
                            if explicit.is_some() {
                                tuple(vec![cap["status"].clone(), cap[field].clone()])
                            } else {
                                let mut item = json!({"status": cap["status"], "product_id": cap["product_id"]});
                                item[field] = cap[field].clone();
                                item
                            }
                        }
                        _ => {
                            let (export, field, seeds) = match method {
                                "get_module_product_id" => {
                                    ("GetModuleProductID", "product_id", vec![buffer(81, "")])
                                }
                                "get_module_product_no" => {
                                    ("GetModuleProductNo", "product_no", vec![json!(0)])
                                }
                                "get_module_hw_type" => {
                                    ("GetModuleHwType", "hw_type", vec![json!(0)])
                                }
                                _ => ("GetScannedModuleParams", "", vec![json!(0); 4]),
                            };
                            let reply = self.read(export, vec![json!(address)], seeds, ctx)?;
                            if explicit.is_some() {
                                reply_tuple(reply)
                            } else if field.is_empty() {
                                let mut item = named_values(
                                    &[
                                        "scanned_product_no",
                                        "saved_product_no",
                                        "scanned_hw_type",
                                        "saved_hw_type",
                                    ],
                                    &reply.values,
                                );
                                item["status"] = json!(reply.status);
                                item
                            } else {
                                let mut item = json!({"status": reply.status});
                                item[field] = reply.values[0].clone();
                                item
                            }
                        }
                    };
                    values.push((address, value));
                }
                Ok(if explicit.is_some() {
                    values.remove(0).1
                } else {
                    int_map(values)
                })
            }
            "set_module_voltage"
            | "get_module_voltage_setpoint"
            | "get_module_voltage_measured" => {
                let names: &[&str] = if method == "set_module_voltage" {
                    &["address", "channel", "voltage", "timeout_s"]
                } else {
                    &["address", "channel", "timeout_s"]
                };
                let p = Params::new(args, kwargs, names, &[])?;
                let address = p.required("address")?.as_u64();
                let channel = p.required("channel")?.as_u64();
                let valid = address.is_some_and(|v| v < 12)
                    && channel.is_some_and(|v| (1..=4).contains(&v));
                if !valid {
                    return Ok(if method == "set_module_voltage" {
                        json!(-15)
                    } else {
                        tuple(vec![json!(-15), Value::Null])
                    });
                }
                let (address, channel) = (address.unwrap(), channel.unwrap());
                if method == "set_module_voltage" {
                    let voltage = number(p.required("voltage")?, "voltage").unwrap_or(f64::NAN);
                    Ok(json!(self.set_voltage(address, channel, voltage, ctx)?))
                } else if method == "get_module_voltage_setpoint" {
                    Ok(reply_tuple(self.read(
                        "GetModuleOutputVoltage",
                        vec![json!(address), json!(channel - 1)],
                        vec![json!(0.0)],
                        ctx,
                    )?))
                } else {
                    let reply = self.measured(address, ctx)?;
                    Ok(tuple(vec![
                        json!(reply.status),
                        if reply.status == 0 {
                            reply.values[0][(channel - 1) as usize].clone()
                        } else {
                            Value::Null
                        },
                    ]))
                }
            }
            "get_module_voltages"
            | "get_all_module_voltages"
            | "get_all_module_voltage_measured" => {
                let p = Params::new(args, kwargs, &["address", "timeout_s"], &[])?;
                let address = integer(p.required("address")?, "address", 0, 11)?;
                if method == "get_all_module_voltage_measured" {
                    let reply = self.measured(address, ctx)?;
                    Ok(tuple(vec![
                        json!(reply.status),
                        if reply.status == 0 {
                            reply.values[0].clone()
                        } else {
                            Value::Null
                        },
                    ]))
                } else {
                    self.voltages(address, ctx)
                }
            }
            "set_module_voltages" | "set_all_module_voltages" => {
                let p = Params::new(args, kwargs, &["address", "voltages", "timeout_s"], &[])?;
                let address = integer(p.required("address")?, "address", 0, 11)?;
                let values = p.required("voltages")?;
                let targets = if method == "set_all_module_voltages" && values.is_array() {
                    let mut targets = BTreeMap::new();
                    for (index, value) in values.as_array().unwrap().iter().take(4).enumerate() {
                        if !value.is_null() {
                            targets.insert(
                                index as u64 + 1,
                                number(value, "voltage").unwrap_or(f64::NAN),
                            );
                        }
                    }
                    targets
                } else {
                    channel_map(values)?
                };
                let mut statuses = Vec::new();
                for (channel, target) in targets {
                    statuses.push((
                        channel,
                        json!(self.set_voltage(address, channel, target, ctx)?),
                    ));
                }
                Ok(int_map(statuses))
            }
            "ramp_module_voltages" => {
                let p = Params::new(
                    args,
                    kwargs,
                    &["address", "voltages", "rates_v_s"],
                    &["timeout_s"],
                )?;
                self.ramp(
                    integer(p.required("address")?, "address", 0, 11)?,
                    p.required("voltages")?,
                    p.get("rates_v_s").unwrap_or(&json!(10.0)),
                    ctx,
                )
            }
            "get_status" => {
                Params::new(args, kwargs, &[], &[])?;
                Ok(
                    json!({"device_id": self.device_id, "com": self.com, "baudrate": self.baudrate,
                    "connected": self.connected, "transport_poisoned": self.poisoned, "hk_running": self.hk_running,
                    "hk_interval_s": self.hk_interval_s, "external_thread": false, "external_lock": false}),
                )
            }
            "start_housekeeping" => {
                let p = Params::new(args, kwargs, &["interval_s"], &[])?;
                let interval = p.optional_positive("interval_s", self.hk_interval_s)?;
                if !self.connected {
                    return Ok(json!(false));
                }
                if !self.hk_running {
                    self.hk_interval_s = interval;
                    self.next_hk = Instant::now();
                    self.hk_running = true;
                }
                Ok(json!(true))
            }
            "stop_housekeeping" => {
                Params::new(args, kwargs, &[], &[])?;
                self.hk_running = false;
                Ok(json!(true))
            }
            "hk_monitor" | "do_housekeeping_cycle" => {
                Params::new(args, kwargs, &[], &[])?;
                if method == "do_housekeeping_cycle" && (!self.hk_running || !self.connected) {
                    return Ok(json!(false));
                }
                self.housekeeping(ctx)?;
                self.next_hk = Instant::now() + duration(self.hk_interval_s)?;
                Ok(if method == "hk_monitor" {
                    Value::Null
                } else {
                    json!(true)
                })
            }
            "describe_error" | "format_status" => {
                let p = Params::new(args, kwargs, &["status"], &[])?;
                let status = p
                    .required("status")?
                    .as_i64()
                    .ok_or_else(|| Error::argument("status must be an integer"))?;
                let message = self
                    .errors
                    .get(status.to_string())
                    .and_then(Value::as_str)
                    .unwrap_or("Unknown status code");
                Ok(json!(if method == "format_status" {
                    format!("{status} ({message})")
                } else {
                    message.to_string()
                }))
            }
            _ => Err(Error::new(
                "AttributeError",
                format!("Unknown AMPR method: {method}"),
            )),
        }
    }

    fn tick(&mut self, ctx: &Context) -> Result<()> {
        self.due_housekeeping(ctx)
    }

    fn get_attribute(&self, name: &str) -> Result<Value> {
        Ok(match name {
            "device_id" | "idn" => json!(self.device_id),
            "com" => json!(self.com),
            "baudrate" => json!(self.baudrate),
            "port_num" => json!(0),
            "connected" => json!(self.connected),
            "_dll_port_claimed" => json!(self.port_claimed),
            "_open_failed" => json!(self.open_failed),
            "_transport_poisoned" => json!(self.poisoned),
            "_failed_open_released" => json!(self.failed_open_released),
            "_failed_open_cleanup_outcome" => self.failed_open_cleanup_outcome.clone(),
            "_opening_in_progress" => json!(false),
            "_transport_error" => json!(self.transport_error),
            "hk_running" => json!(self.hk_running),
            "hk_interval_s" => json!(self.hk_interval_s),
            "external_thread" | "external_lock" => json!(false),
            "housekeeping_snapshot" => self.housekeeping.clone(),
            "err_dict" => self.errors.clone(),
            "MODULE_NUM" => json!(12),
            "CHANNEL_NUM" => json!(4),
            "MAX_ABS_MODULE_VOLTAGE" => json!(1000.0),
            "ADDR_BASE" => json!(128),
            "ADDR_BROADCAST" => json!(255),
            "DEVICE_TYPE" => json!(0xA3D8),
            "MODULE_NOT_FOUND" => json!(0),
            "MODULE_PRESENT" => json!(1),
            "MODULE_INVALID" => json!(2),
            "FAN_PWM_MAX" => json!(10000),
            "MAIN_STATE" => int_map(MAIN_STATE.iter().map(|(n, s)| (*n, json!(s))).collect()),
            "DEVICE_STATE" => int_map(DEVICE_STATE.iter().map(|(n, s)| (*n, json!(s))).collect()),
            "VOLTAGE_STATE" => int_map(VOLTAGE_STATE.iter().map(|(n, s)| (*n, json!(s))).collect()),
            "TEMPERATURE_STATE" => int_map(
                TEMPERATURE_STATE
                    .iter()
                    .map(|(n, s)| (*n, json!(s)))
                    .collect(),
            ),
            "INTERLOCK_STATE" => int_map(
                INTERLOCK_STATE
                    .iter()
                    .map(|(n, s)| (*n, json!(s)))
                    .collect(),
            ),
            _ => {
                if let Some(code) = error_constant(name) {
                    json!(code)
                } else {
                    return Err(Error::new(
                        "AttributeError",
                        format!("Unknown AMPR attribute: {name}"),
                    ));
                }
            }
        })
    }

    fn set_attribute(&mut self, name: &str, value: Value) -> Result<()> {
        match name {
            "hk_interval_s" => {
                self.hk_interval_s = positive(&value, name)?;
                self.next_hk = Instant::now() + duration(self.hk_interval_s)?;
            }
            "device_id" | "idn" => {
                let text = value
                    .as_str()
                    .filter(|s| !s.trim().is_empty())
                    .ok_or_else(|| Error::argument("device_id must be a non-empty string"))?;
                self.device_id = text.into();
            }
            "com" | "baudrate" => {
                if self.connected || self.port_claimed {
                    return Err(Error::runtime(
                        "Disconnect before changing connection settings",
                    ));
                }
                let number = integer(
                    &value,
                    name,
                    1,
                    if name == "com" { 255 } else { u32::MAX as u64 },
                )?;
                if name == "com" {
                    self.com = number;
                } else {
                    self.baudrate = number;
                }
            }
            _ => {
                return Err(Error::new(
                    "AttributeError",
                    format!("Read-only or unknown AMPR attribute: {name}"),
                ));
            }
        }
        Ok(())
    }
}

struct Params<'a> {
    args: &'a [Value],
    kwargs: &'a Value,
    names: &'a [&'a str],
}
impl<'a> Params<'a> {
    fn new(
        args: &'a [Value],
        kwargs: &'a Value,
        names: &'a [&'a str],
        extra: &[&str],
    ) -> Result<Self> {
        if args.len() > names.len() {
            return Err(Error::new("TypeError", "Too many positional arguments"));
        }
        if let Some(index) = names.iter().position(|name| *name == "timeout_s")
            && let Some(timeout) = args.get(index).filter(|value| !value.is_null())
        {
            positive(timeout, "timeout_s")?;
        }
        if !kwargs.is_null() && !kwargs.is_object() {
            return Err(Error::new("TypeError", "kwargs must be an object"));
        }
        if let Some(map) = kwargs.as_object() {
            for key in map.keys() {
                if !names.contains(&key.as_str()) && !extra.contains(&key.as_str()) {
                    return Err(Error::new(
                        "TypeError",
                        format!("Unexpected keyword: {key}"),
                    ));
                }
                if names
                    .iter()
                    .position(|name| *name == key)
                    .is_some_and(|i| i < args.len())
                {
                    return Err(Error::new(
                        "TypeError",
                        format!("Multiple values for {key}"),
                    ));
                }
            }
            if let Some(timeout) = map.get("timeout_s").filter(|v| !v.is_null()) {
                positive(timeout, "timeout_s")?;
            }
        }
        Ok(Self {
            args,
            kwargs,
            names,
        })
    }
    fn get(&self, name: &str) -> Option<&'a Value> {
        self.names
            .iter()
            .position(|s| *s == name)
            .and_then(|i| self.args.get(i))
            .or_else(|| self.kwargs.get(name))
    }
    fn required(&self, name: &str) -> Result<&'a Value> {
        self.get(name)
            .ok_or_else(|| Error::new("TypeError", format!("Missing argument: {name}")))
    }
    fn optional_positive(&self, name: &str, default: f64) -> Result<f64> {
        self.get(name)
            .filter(|v| !v.is_null())
            .map(|v| positive(v, name))
            .unwrap_or(Ok(default))
    }
}
fn duration(value: f64) -> Result<Duration> {
    let duration =
        Duration::try_from_secs_f64(value).map_err(|_| Error::argument("Duration out of range"))?;
    if Instant::now().checked_add(duration).is_none() {
        return Err(Error::argument("Duration out of range"));
    }
    Ok(duration)
}
fn positive(value: &Value, name: &str) -> Result<f64> {
    let number = number(value, name)?;
    if !number.is_finite() || number <= 0.0 {
        return Err(Error::argument(format!(
            "{name} must be finite and positive"
        )));
    }
    duration(number)?;
    Ok(number)
}
fn number(value: &Value, name: &str) -> Result<f64> {
    if let Some(number) = value.as_f64() {
        return Ok(number);
    }
    match value.get("$float").and_then(Value::as_str) {
        Some("nan") => Ok(f64::NAN),
        Some("inf") => Ok(f64::INFINITY),
        Some("-inf") => Ok(f64::NEG_INFINITY),
        _ => Err(Error::new("TypeError", format!("{name} must be a number"))),
    }
}
fn integer(value: &Value, name: &str, min: u64, max: u64) -> Result<u64> {
    if let Some(number) = value.as_u64()
        && number >= min
        && number <= max
    {
        return Ok(number);
    }
    if value.is_i64() || value.is_u64() {
        Err(Error::argument(format!(
            "{name} must be between {min} and {max}"
        )))
    } else {
        Err(Error::new(
            "TypeError",
            format!("{name} must be an integer"),
        ))
    }
}
fn boolean(value: &Value, name: &str) -> Result<bool> {
    value
        .as_bool()
        .ok_or_else(|| Error::new("TypeError", format!("{name} must be a boolean")))
}
fn output_u64(reply: &NativeReply, index: usize) -> Result<u64> {
    reply.values[index]
        .as_u64()
        .ok_or_else(|| Error::runtime("Malformed unsigned DLL output"))
}
fn output_bool(reply: &NativeReply, index: usize) -> Result<bool> {
    reply.values[index]
        .as_bool()
        .ok_or_else(|| Error::runtime("Malformed bool DLL output"))
}
fn array<'a>(value: &'a Value, size: usize, name: &str) -> Result<&'a [Value]> {
    value
        .as_array()
        .filter(|v| v.len() == size)
        .map(Vec::as_slice)
        .ok_or_else(|| Error::runtime(format!("{name} requires exactly {size} values")))
}
fn buffer(capacity: usize, text: &str) -> Value {
    json!({"capacity": capacity, "text": text})
}
fn reply_tuple(reply: NativeReply) -> Value {
    let mut values = vec![json!(reply.status)];
    values.extend(reply.values);
    tuple(values)
}
fn ensure(status: i64, operation: &str) -> Result<()> {
    if status == 0 {
        Ok(())
    } else {
        Err(Error::status(status, operation))
    }
}
fn int_map(pairs: Vec<(u64, Value)>) -> Value {
    json!({"$map": pairs.into_iter().map(|(k, v)| json!([k, v])).collect::<Vec<_>>()})
}
fn named_values(names: &[&str], values: &[Value]) -> Value {
    Value::Object(
        names
            .iter()
            .zip(values)
            .map(|(n, v)| ((*n).into(), v.clone()))
            .collect(),
    )
}
fn flag_names(raw: u64, flags: &[(u64, &str)], ok: Option<&str>) -> Value {
    if raw == 0
        && let Some(name) = ok
    {
        return json!([name]);
    }
    json!(
        flags
            .iter()
            .filter(|(bit, _)| raw & bit != 0)
            .map(|(_, name)| *name)
            .collect::<Vec<_>>()
    )
}
fn channel_map(value: &Value) -> Result<BTreeMap<u64, f64>> {
    let mut result = BTreeMap::new();
    if let Some(pairs) = value.get("$map").and_then(Value::as_array) {
        for pair in pairs {
            let pair = array(pair, 2, "channel map entry")?;
            let channel = integer(&pair[0], "channel", 1, 4)?;
            if result
                .insert(channel, number(&pair[1], "voltage")?)
                .is_some()
            {
                return Err(Error::argument("Duplicate channel"));
            }
        }
    } else if let Some(map) = value.as_object() {
        for (key, value) in map {
            let channel = key
                .parse::<u64>()
                .map_err(|_| Error::argument("Channel must be an integer"))?;
            if !(1..=4).contains(&channel) {
                return Err(Error::argument("Channel must be 1..4"));
            }
            result.insert(channel, number(value, "voltage")?);
        }
    } else {
        return Err(Error::new("TypeError", "voltages must be a channel map"));
    }
    Ok(result)
}
fn resolve_capabilities(info: &Value) -> Capabilities {
    if info["product_no"] == json!(132401) && info["hw_type"] == json!(222308) {
        return Capabilities {
            rating: Some(1000),
            channels: Some(4),
        };
    }
    let text = info["product_id"]
        .as_str()
        .unwrap_or("")
        .to_ascii_lowercase();
    let tokens: Vec<_> = text
        .split(|c: char| !c.is_ascii_alphanumeric())
        .filter(|s| !s.is_empty())
        .collect();
    let mut cap = Capabilities::default();
    for (i, token) in tokens.iter().enumerate() {
        if matches!(*token, "500v" | "1000v") {
            cap.rating = token.strip_suffix('v').and_then(|s| s.parse().ok());
        }
        if matches!(*token, "500" | "1000") && tokens.get(i + 1) == Some(&"v") {
            cap.rating = token.parse().ok();
        }
        if matches!(
            *token,
            "2ch" | "4ch" | "2channel" | "4channel" | "2channels" | "4channels"
        ) {
            cap.channels = token
                .chars()
                .next()
                .and_then(|c| c.to_digit(10))
                .map(u64::from);
        }
        if matches!(*token, "2" | "4")
            && tokens
                .get(i + 1)
                .is_some_and(|s| matches!(*s, "ch" | "channel" | "channels"))
        {
            cap.channels = token.parse().ok();
        }
    }
    if text.contains("quad") {
        cap.channels = Some(4);
    } else if text.contains("dual") || text.contains("double") {
        cap.channels = Some(2);
    }
    cap
}
fn error_constant(name: &str) -> Option<i64> {
    Some(match name {
        "NO_ERR" => 0,
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
        _ => return None,
    })
}
