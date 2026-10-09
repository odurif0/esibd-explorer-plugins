//! AMX A/B controller, with the lifecycle shared by the independently addressed HD.
//!
//! GUI RPC inventory: connect, disconnect, get_status, list_configs, load_config,
//! set_device_enabled, collect_housekeeping, set_frequency_khz,
//! set_pulser_width_ticks, shutdown. HD also uses collect_state_snapshot.
//! GUI attributes: connected, CLOCK, OSC_OFFSET, PULSER_{WIDTH,DELAY}_OFFSET,
//! _dll_port_claimed, _transport_poisoned, _transport_error, _opening_in_progress,
//! _open_failed, _failed_open_released, loaded_config_{number,name,source}.
//! Qt transitions and crash-resume policy remain in the Python adapter. In
//! particular, connect and monitoring never load a config or overwrite timing.

use crate::Backend;
use crate::codec::{float, tuple};
use crate::context::Context;
use crate::error::{Error, Result};
use crate::ffi::{Dll, NativeReply};
use serde_json::{Map, Value, json};

pub(crate) const CLOCK: f64 = 100_000_000.0;
pub(crate) const DEVICE_BITS: &[(u32, &str)] = &[
    (1, "DEVST_VCPU_FAIL"),
    (2, "DEVST_VSUP_FAIL"),
    (1 << 8, "DEVST_FAN1_FAIL"),
    (1 << 9, "DEVST_FAN2_FAIL"),
    (1 << 10, "DEVST_FAN3_FAIL"),
    (1 << 15, "DEVST_FPGA_DIS"),
    (1 << 16, "DEVST_SEN1_HIGH"),
    (1 << 17, "DEVST_SEN2_HIGH"),
    (1 << 18, "DEVST_SEN3_HIGH"),
    (1 << 24, "DEVST_SEN1_LOW"),
    (1 << 25, "DEVST_SEN2_LOW"),
    (1 << 26, "DEVST_SEN3_LOW"),
];
const CONTROLLER_BITS: &[(u32, &str)] = &[
    (1, "ENB"),
    (2, "ENB_OSC"),
    (4, "ENB_PULSER"),
    (8, "SW_TRIG"),
    (16, "SW_PULSE"),
    (32, "PREVENT_DIS"),
    (64, "DIS_DITHER"),
    (128, "NC"),
    (256, "ENABLE"),
    (512, "SW_TRIG_OUT"),
    (1024, "CLRN"),
];
pub(crate) const HD_DEVICE_BITS: &[(u32, &str)] = &[
    (1, "DEVST_VCPU_FAIL"),
    (2, "DEVST_VSUP_FAIL"),
    (16, "DEVST_PLL0_FAIL"),
    (32, "DEVST_PLL1_FAIL"),
    (64, "DEVST_PLL2_FAIL"),
    (128, "DEVST_PLL3_FAIL"),
    (256, "DEVST_FAN1_FAIL"),
    (512, "DEVST_FAN2_FAIL"),
    (1024, "DEVST_FAN3_FAIL"),
    (32768, "DEVST_FPGA_DIS"),
];
pub(crate) const HD_TEMPERATURE_BITS: &[(u32, &str)] = &[
    (1, "TMPST_SEN1_HIGH"),
    (2, "TMPST_SEN2_HIGH"),
    (4, "TMPST_SEN3_HIGH"),
    (256, "TMPST_SEN1_LOW"),
    (512, "TMPST_SEN2_LOW"),
    (1024, "TMPST_SEN3_LOW"),
];
pub(crate) const HD_CONTROLLER_BITS: &[(u32, &str)] = &[
    (1, "ST_ENABLE"),
    (2, "ST_ENB_TIMER"),
    (4, "ST_ENB_OSC0"),
    (8, "ST_ENB_OSC1"),
    (16, "ST_ENB_OSC2"),
    (32, "ST_ENB_OSC3"),
    (64, "ST_SW_TRIG0"),
    (128, "ST_SW_PULSE0"),
    (256, "ST_SW_TRIG1"),
    (512, "ST_SW_PULSE1"),
    (1024, "ST_DITHER"),
    (2048, "ST_ENB_SWITCH"),
    (4096, "ST_ENB_PULS_GEN"),
    (1 << 16, "ST_CLRN"),
    (1 << 17, "ST_DEL_BUSY"),
    (1 << 18, "ST_ENB_PWM"),
    (1 << 22, "ST_SW_TRIG_OUT0"),
    (1 << 23, "ST_SW_TRIG_OUT1"),
    (1 << 24, "ST_DIO0"),
    (1 << 25, "ST_DIO1"),
    (1 << 26, "ST_DIO2"),
    (1 << 27, "ST_DIO3"),
    (1 << 28, "ST_DIO4"),
    (1 << 29, "ST_DIO5"),
    (1 << 30, "ST_DIO6"),
    (1 << 31, "ST_DIO7"),
];

pub struct Controller {
    dll: Box<dyn Dll>,
    pub(crate) hd: bool,
    device_id: String,
    com: u32,
    channel: u32,
    baudrate: u32,
    connected: bool,
    claimed: bool,
    poisoned: bool,
    transport_error: Option<String>,
    opening: bool,
    open_failed: bool,
    failed_open_released: bool,
    loaded_number: Option<u32>,
    loaded_name: String,
    loaded_source: String,
    hardware_mutated: bool,
    recording: bool,
    errors: Value,
}

impl Controller {
    pub fn new(config: &Value, dll: Box<dyn Dll>) -> Result<Self> {
        Self::new_family(config, dll, false)
    }

    pub(crate) fn new_family(config: &Value, dll: Box<dyn Dll>, hd: bool) -> Result<Self> {
        let config = config
            .as_object()
            .ok_or_else(|| Error::argument("AMX config must be an object"))?;
        let com = uint(
            config.get("com").ok_or_else(|| type_error("Missing com"))?,
            "com",
            65535,
        )?;
        if com == 0 {
            return Err(Error::argument("com must be >= 1"));
        }
        let channel_name = if hd { "stream" } else { "port" };
        let channel = uint(
            config.get(channel_name).unwrap_or(&json!(0)),
            channel_name,
            15,
        )?;
        let baudrate = uint(
            config.get("baudrate").unwrap_or(&json!(230400)),
            "baudrate",
            u32::MAX,
        )?;
        if baudrate == 0 {
            return Err(Error::argument("baudrate must be > 0"));
        }
        let device_id = config
            .get("device_id")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_owned();
        let errors = serde_json::from_str(crate::ERROR_CATALOG)
            .map_err(|error| Error::runtime(format!("Invalid AMX error catalog: {error}")))?;
        Ok(Self {
            dll,
            hd,
            device_id,
            com,
            channel,
            baudrate,
            connected: false,
            claimed: false,
            poisoned: false,
            transport_error: None,
            opening: false,
            open_failed: false,
            failed_open_released: false,
            loaded_number: None,
            loaded_name: String::new(),
            loaded_source: String::new(),
            hardware_mutated: false,
            recording: false,
            errors,
        })
    }

    pub(crate) fn require_connected(&self) -> Result<()> {
        self.require_usable()?;
        if self.connected {
            Ok(())
        } else {
            Err(Error::runtime("AMX device is not connected."))
        }
    }

    fn require_usable(&self) -> Result<()> {
        if self.poisoned {
            Err(Error::runtime(format!(
                "AMX transport is unusable; start a new worker. {}",
                self.transport_error.as_deref().unwrap_or("")
            )))
        } else {
            Ok(())
        }
    }

    pub(crate) fn raw(
        &mut self,
        suffix: &str,
        tail: &[Value],
        ctx: &Context,
    ) -> Result<NativeReply> {
        self.require_usable()?;
        ctx.check_cancelled()?;
        let mut args = vec![json!(self.channel)];
        args.extend_from_slice(tail);
        self.native(suffix, &args, ctx)
    }

    pub(crate) fn native(
        &mut self,
        suffix: &str,
        args: &[Value],
        ctx: &Context,
    ) -> Result<NativeReply> {
        self.require_usable()?;
        ctx.check_cancelled()?;
        if (suffix.starts_with("Set") && suffix != "SetBaudRate")
            || suffix.starts_with("Load")
            || suffix == "Restart"
        {
            self.hardware_mutated = true;
        }
        let symbol = format!("COM_HVAMX4ED{}_{}", if self.hd { "H" } else { "" }, suffix);
        let result = ctx.native_call(&symbol, None, || self.dll.call(&symbol, args));
        if let Err(error) = &result
            && (error.kind == "TimeoutError" || error.kind == "ConnectionError")
        {
            self.poisoned = true;
            self.connected = false;
            self.claimed = true;
            self.transport_error = Some(error.to_string());
            if suffix == "LoadCurrentConfig" {
                self.clear_loaded();
            }
        }
        let reply = result?;
        if suffix == "LoadCurrentConfig" && reply.status == 0 {
            // The hardware has changed even if cancellation prevents the
            // facade's subsequent metadata/name reads from completing.
            self.clear_loaded();
        }
        ctx.check_cancelled()?;
        Ok(reply)
    }

    pub(crate) fn read(
        &mut self,
        suffix: &str,
        tail: &[Value],
        count: usize,
        ctx: &Context,
    ) -> Result<Vec<Value>> {
        let reply = self.raw(suffix, tail, ctx)?;
        check_status(reply.status, suffix)?;
        outputs(reply, count)
    }

    pub(crate) fn write(&mut self, suffix: &str, tail: &[Value], ctx: &Context) -> Result<()> {
        let reply = self.raw(suffix, tail, ctx)?;
        check_status(reply.status, suffix)?;
        outputs(reply, 0)?;
        Ok(())
    }

    fn clear_loaded(&mut self) {
        self.loaded_number = None;
        self.loaded_name.clear();
        self.loaded_source.clear();
    }

    fn loaded_status(&self) -> Value {
        json!({"memory_config": self.loaded_number,
            "memory_config_name": if self.loaded_name.is_empty() { None } else { Some(&self.loaded_name) },
            "memory_config_source": if self.loaded_source.is_empty() { None } else { Some(&self.loaded_source) }})
    }

    fn status(&self) -> Value {
        let mut value = self.loaded_status();
        let map = value.as_object_mut().expect("object");
        map.extend([
            ("device_id".into(), json!(self.device_id)),
            ("com".into(), json!(self.com)),
            (
                (if self.hd { "stream" } else { "port" }).into(),
                json!(self.channel),
            ),
            ("baudrate".into(), json!(self.baudrate)),
            ("connected".into(), json!(self.connected)),
            ("transport_poisoned".into(), json!(self.poisoned)),
        ]);
        value
    }

    fn connect(&mut self, ctx: &Context) -> Result<Value> {
        self.require_usable()?;
        ctx.check_cancelled()?;
        if self.connected {
            return Ok(json!(true));
        }
        if self.claimed {
            return Err(Error::runtime(
                "AMX DLL channel is still claimed after failed cleanup; disconnect before reconnecting.",
            ));
        }
        self.opening = true;
        self.open_failed = false;
        self.failed_open_released = false;
        // Open can partially claim the DLL slot even when its return status fails.
        self.claimed = true;
        let opened = self.native("Open", &[json!(self.channel), json!(self.com)], ctx);
        self.opening = false;
        let result = (|| {
            let reply = opened?;
            check_status(reply.status, "open_port")?;
            outputs(reply, 0)?;
            self.read("SetBaudRate", &[json!(self.baudrate)], 1, ctx)?;
            self.connected = true;
            // A vendor failure is fatal. The reference tolerates probe
            // exceptions on a usable transport and warns about an alien ID.
            match self.raw(
                "GetProductID",
                &[text_buffer(if self.hd { 81 } else { 60 })],
                ctx,
            ) {
                Ok(reply) => {
                    check_status(reply.status, "get_product_id")?;
                    let values = outputs(reply, 1)?;
                    let product_id = text(&values[0], "product_id")?;
                    if !product_id.is_empty() && !product_id.to_uppercase().contains("AMX") {
                        let _ = ctx.progress(json!({"event": "warning", "message": format!(
                            "Connected device does not look like an AMX controller. Reported product_id='{product_id}'. Check the COM port and use the matching driver for that instrument.")}));
                    }
                }
                Err(error) if ctx.is_cancelled() || ctx.remaining().is_zero() || self.poisoned => {
                    return Err(error);
                }
                Err(error) => {
                    let _ = ctx.progress(json!({"event": "debug", "message": format!("Skipping AMX identity probe after connect: {error}")}));
                }
            }
            ctx.check_cancelled()?;
            Ok(json!(true))
        })();
        if let Err(error) = result {
            self.open_failed = true;
            self.connected = false;
            let cleanup = self.rollback_port(ctx);
            return Err(with_cleanup(error, cleanup));
        }
        result
    }

    fn rollback_port(&mut self, ctx: &Context) -> Result<()> {
        self.require_usable()?;
        let cleanup = ctx.cleanup(std::time::Duration::from_secs(5));
        let reply = self.raw("Close", &[], &cleanup)?;
        check_status(reply.status, "close_port rollback")?;
        outputs(reply, 0)?;
        self.claimed = false;
        self.failed_open_released = self.open_failed;
        self.clear_loaded();
        Ok(())
    }

    fn disconnect(&mut self, ctx: &Context) -> Result<Value> {
        ctx.check_cancelled()?;
        if self.poisoned {
            return Ok(json!(false));
        }
        if !self.connected && !self.claimed {
            self.clear_loaded();
            return Ok(json!(true));
        }
        let reply = match self.raw("Close", &[], ctx) {
            Ok(reply) => reply,
            Err(error) if ctx.is_cancelled() || ctx.remaining().is_zero() => return Err(error),
            Err(_) => {
                self.claimed = true;
                return Ok(json!(false));
            }
        };
        if reply.status != 0 {
            self.claimed = true;
            return Ok(json!(false));
        }
        outputs(reply, 0)?;
        self.connected = false;
        self.claimed = false;
        self.failed_open_released = self.open_failed;
        self.clear_loaded();
        Ok(json!(true))
    }

    pub(crate) fn config_limit(&self) -> u32 {
        if self.hd { 500 } else { 126 }
    }
    pub(crate) fn config_number(&self, value: &Value) -> Result<u32> {
        uint(value, "config_number", self.config_limit() - 1)
    }

    fn config_name(&mut self, number: u32, ctx: &Context) -> Result<String> {
        let values = self.read(
            "GetConfigName",
            &[json!(number), text_buffer(if self.hd { 193 } else { 52 })],
            1,
            ctx,
        )?;
        text(&values[0], "config name")
    }

    fn load_config(&mut self, number: u32, source: &str, ctx: &Context) -> Result<()> {
        self.require_connected()?;
        self.write("LoadCurrentConfig", &[json!(number)], ctx)?;
        // A failed optional name query must not retain provenance from the previous slot.
        self.loaded_number = Some(number);
        self.loaded_source = source.to_owned();
        self.loaded_name.clear();
        match self.config_name(number, ctx) {
            Ok(name) => self.loaded_name = name.trim().to_owned(),
            Err(error) if ctx.is_cancelled() || ctx.remaining().is_zero() || self.poisoned => {
                return Err(error);
            }
            Err(_) => {}
        }
        Ok(())
    }

    fn list_configs(&mut self, include_empty: bool, ctx: &Context) -> Result<Value> {
        self.require_connected()?;
        let n = self.config_limit() as usize;
        let values = self.read(
            "GetConfigList",
            &[json!(vec![false; n]), json!(vec![false; n])],
            2,
            ctx,
        )?;
        let active = array(&values[0], n, "config active flags")?;
        let valid = array(&values[1], n, "config valid flags")?;
        let mut configs = Vec::new();
        for index in 0..n {
            ctx.check_cancelled()?;
            let active = boolean(&active[index], "active")?;
            let valid = boolean(&valid[index], "valid")?;
            if include_empty || active || valid {
                let name = self.config_name(index as u32, ctx)?;
                configs
                    .push(json!({"index": index, "name": name, "active": active, "valid": valid}));
            }
        }
        Ok(json!(configs))
    }

    fn get_enabled(&mut self, ctx: &Context) -> Result<bool> {
        self.require_connected()?;
        let values = self.read("GetDeviceEnable", &[json!(false)], 1, ctx)?;
        boolean(&values[0], "device enabled")
    }

    fn set_enabled(&mut self, enabled: bool, ctx: &Context) -> Result<()> {
        self.require_connected()?;
        self.write("SetDeviceEnable", &[json!(enabled)], ctx)
    }

    fn initialize(&mut self, p: &Params, ctx: &Context) -> Result<Value> {
        let mut standby = optional_uint(&p[1], "standby_config", self.config_limit() - 1)?;
        let operating = optional_uint(&p[2], "operating_config", self.config_limit() - 1)?;
        let require_disabled = p.bool_or(3, true)?;
        let result = (|| {
            self.connect(ctx)?;
            let mut source = "standby";
            if standby.is_none() && operating.is_none() {
                match self.list_configs(false, ctx) {
                    Ok(configs) => {
                        let configs = configs.as_array().expect("config list");
                        let valid: Vec<&Value> =
                            configs.iter().filter(|v| v["valid"] == true).collect();
                        let exact = valid.iter().find(|v| {
                            v["name"]
                                .as_str()
                                .unwrap_or("")
                                .trim()
                                .eq_ignore_ascii_case("standby")
                        });
                        let partial: Vec<_> = valid
                            .iter()
                            .filter(|v| {
                                v["name"]
                                    .as_str()
                                    .unwrap_or("")
                                    .trim()
                                    .to_lowercase()
                                    .contains("standby")
                            })
                            .collect();
                        if let Some(config) = exact.copied().or_else(|| {
                            if partial.len() == 1 {
                                Some(partial[0])
                            } else {
                                None
                            }
                        }) {
                            standby = Some(config["index"].as_u64().expect("index") as u32);
                            source = "auto-standby";
                        }
                    }
                    Err(error)
                        if ctx.is_cancelled() || ctx.remaining().is_zero() || self.poisoned =>
                    {
                        return Err(error);
                    }
                    Err(_) => {}
                }
            }
            let mut state = Map::new();
            if let Some(number) = standby {
                self.load_config(number, source, ctx)?;
                let enabled = self.get_enabled(ctx)?;
                state.insert("standby_config".into(), json!(number));
                state.insert("device_enabled".into(), json!(enabled));
                if require_disabled && enabled {
                    return Err(Error::runtime(
                        "AMX standby configuration left the device enabled. Refusing to continue initialization.",
                    ));
                }
            }
            if let Some(number) = operating {
                self.load_config(number, "operating", ctx)?;
                state.insert("operating_config".into(), json!(number));
            }
            if self.loaded_number.is_some() {
                state.extend(
                    self.loaded_status()
                        .as_object()
                        .expect("loaded status")
                        .clone(),
                );
            }
            Ok(Value::Object(state))
        })();
        if let Err(error) = result {
            if self.connected {
                let cleanup_ctx = ctx.cleanup(std::time::Duration::from_secs(5));
                let cleanup = self.shutdown(None, true, &cleanup_ctx).map(|_| ());
                return Err(with_cleanup(error, cleanup));
            }
            return Err(error);
        }
        result
    }

    fn shutdown(&mut self, standby: Option<u32>, disable: bool, ctx: &Context) -> Result<Value> {
        if standby.is_some() && disable {
            return Err(Error::argument(
                "standby_config cannot be combined with disable_device. Either load an explicit standby config or request an explicit shutdown sequence.",
            ));
        }
        self.require_usable()?;
        if !self.connected {
            return Ok(json!(false));
        }
        let mut errors = Vec::new();
        let mut standby_loaded = false;
        if let Some(number) = standby {
            match self.load_config(number, "explicit", ctx) {
                Ok(()) => standby_loaded = true,
                Err(error) => errors.push(format!("load_config({number}): {error}")),
            }
        }
        let mut should_disable = disable || !standby_loaded;
        if standby_loaded && !disable {
            match self.get_enabled(ctx) {
                Ok(enabled) => should_disable = enabled,
                Err(error) => {
                    errors.push(format!("standby disable verification: {error}"));
                    should_disable = true;
                }
            }
        }
        if should_disable && let Err(error) = self.set_enabled(false, ctx) {
            errors.push(format!("set_device_enabled(False): {error}"));
        }
        match self.get_enabled(ctx) {
            Ok(false) => {}
            Ok(true) => {
                errors.push("disable verification: AMX remained enabled after shutdown.".to_owned())
            }
            Err(error) => errors.push(format!("disable verification: {error}")),
        }
        // Preserve the link after any failed shutdown step, including a failed
        // standby load followed by a successful fallback disable.
        if !errors.is_empty() {
            return Err(shutdown_errors(errors));
        }
        if self.disconnect(ctx)? != true {
            return Err(shutdown_errors(vec![
                "disconnect(): AMX disconnect failed during shutdown.".into(),
            ]));
        }
        Ok(json!(true))
    }

    pub(crate) fn begin_call(&mut self) {
        self.hardware_mutated = false;
    }

    pub(crate) fn finish_call(&mut self, result: Result<Value>, ctx: &Context) -> Result<Value> {
        if self.hardware_mutated
            && (ctx.is_cancelled() || ctx.remaining().is_zero())
            && self.connected
        {
            let error = result
                .err()
                .unwrap_or_else(|| ctx.check_cancelled().expect_err("cancelled context"));
            // Cancellation must not cancel the compensating OFF/readback. The
            // parent still supervises a blocked DLL and must not claim OFF.
            let cleanup_ctx = ctx.cleanup(std::time::Duration::from_secs(5));
            let cleanup = self.set_enabled(false, &cleanup_ctx).and_then(|_| {
                if self.get_enabled(&cleanup_ctx)? {
                    Err(Error::runtime(
                        "AMX disable readback remained enabled; OFF unconfirmed",
                    ))
                } else {
                    Ok(())
                }
            });
            return Err(with_cleanup(error, cleanup));
        }
        result
    }

    fn oscillator_period(&mut self, ctx: &Context) -> Result<u32> {
        let tail = if self.hd {
            vec![json!(0), json!(0)]
        } else {
            vec![json!(0)]
        };
        let values = self.read("GetOscillatorPeriod", &tail, 1, ctx)?;
        uint(&values[0], "oscillator period readback", u32::MAX)
    }

    fn set_frequency(&mut self, hz: f64, ctx: &Context) -> Result<()> {
        if !hz.is_finite() || hz <= 0.0 {
            return Err(Error::argument("frequency_hz must be finite and > 0"));
        }
        let period = (CLOCK / hz - 2.0).round_ties_even();
        if !(1.0..=u32::MAX as f64).contains(&period) {
            return Err(Error::argument(format!(
                "frequency_hz={hz} results in an invalid oscillator period register: {period}"
            )));
        }
        self.require_connected()?;
        let tail = if self.hd {
            vec![json!(0), json!(period as u32)]
        } else {
            vec![json!(period as u32)]
        };
        self.write("SetOscillatorPeriod", &tail, ctx)
    }

    fn pulser(&mut self, value: &Value, ctx: &Context) -> Result<u32> {
        if !self.hd {
            return uint(value, "pulser_no", 3);
        }
        let timer = uint(value, "pulser_no", u32::MAX)?;
        let count = self.read("GetTimerCount", &[json!(0)], 1, ctx)?;
        let count = uint(&count[0], "timer count", 65535)?;
        if timer >= count {
            return Err(Error::argument(format!(
                "Invalid timer {timer}; device reports {count} timers"
            )));
        }
        Ok(timer)
    }

    fn pulser_read(&mut self, number: u32, width: bool, ctx: &Context) -> Result<u32> {
        let suffix = match (self.hd, width) {
            (true, true) => "GetTimerWidth",
            (true, false) => "GetTimerDelay",
            (false, true) => "GetPulserWidth",
            (false, false) => "GetPulserDelay",
        };
        let values = self.read(suffix, &[json!(number), json!(0)], 1, ctx)?;
        uint(&values[0], "pulser register readback", u32::MAX)
    }

    fn pulser_write(&mut self, number: u32, value: u32, width: bool, ctx: &Context) -> Result<()> {
        let suffix = match (self.hd, width) {
            (true, true) => "SetTimerWidth",
            (true, false) => "SetTimerDelay",
            (false, true) => "SetPulserWidth",
            (false, false) => "SetPulserDelay",
        };
        self.write(suffix, &[json!(number), json!(value)], ctx)
    }

    pub(crate) fn call_common(
        &mut self,
        method: &str,
        args: &[Value],
        kwargs: &Value,
        ctx: &Context,
    ) -> Result<Value> {
        match method {
            "connect" => {
                Params::new(args, kwargs, &["timeout_s"], 0, 1)?;
                self.connect(ctx)
            }
            "initialize" => {
                let p = Params::new(
                    args,
                    kwargs,
                    &[
                        "timeout_s",
                        "standby_config",
                        "operating_config",
                        "require_standby_device_disabled",
                    ],
                    0,
                    1,
                )?;
                self.initialize(&p, ctx)
            }
            "disconnect" | "close" => {
                Params::new(args, kwargs, &["timeout_s"], 0, 1)?;
                self.disconnect(ctx)
            }
            "get_status" => {
                Params::new(args, kwargs, &[], 0, 0)?;
                Ok(self.status())
            }
            "list_configs" => {
                let p = Params::new(args, kwargs, &["include_empty", "timeout_s"], 0, 2)?;
                self.list_configs(p.bool_or(0, false)?, ctx)
            }
            "load_config" => {
                let p = Params::new(args, kwargs, &["config_number", "timeout_s"], 1, 2)?;
                let number = self.config_number(&p[0])?;
                self.load_config(number, "explicit", ctx)?;
                Ok(Value::Null)
            }
            "set_device_enabled" => {
                let p = Params::new(args, kwargs, &["enable", "timeout_s"], 1, 2)?;
                self.set_enabled(boolean(&p[0], "enable")?, ctx)?;
                Ok(Value::Null)
            }
            "get_device_enabled" => {
                Params::new(args, kwargs, &["timeout_s"], 0, 1)?;
                Ok(json!(self.get_enabled(ctx)?))
            }
            "get_frequency_hz" | "get_frequency_khz" => {
                Params::new(args, kwargs, &["timeout_s"], 0, 1)?;
                self.require_connected()?;
                let hz = CLOCK / (self.oscillator_period(ctx)? as f64 + 2.0);
                Ok(float(if method == "get_frequency_khz" {
                    hz / 1000.0
                } else {
                    hz
                }))
            }
            "set_frequency_hz" | "set_frequency_khz" => {
                let p = Params::new(
                    args,
                    kwargs,
                    &[
                        if method == "set_frequency_hz" {
                            "frequency_hz"
                        } else {
                            "frequency_khz"
                        },
                        "timeout_s",
                    ],
                    1,
                    2,
                )?;
                let mut hz = finite(&p[0], "frequency")?;
                if method == "set_frequency_khz" {
                    hz *= 1000.0;
                }
                self.set_frequency(hz, ctx)?;
                Ok(Value::Null)
            }
            "get_pulser_delay_ticks"
            | "get_pulser_width_ticks"
            | "get_pulser_delay_seconds"
            | "get_pulser_width_seconds" => {
                let p = Params::new(args, kwargs, &["pulser_no", "timeout_s"], 1, 2)?;
                self.require_connected()?;
                let pulser = self.pulser(&p[0], ctx)?;
                let width = method.contains("width");
                let register = self.pulser_read(pulser, width, ctx)?;
                if method.ends_with("seconds") {
                    Ok(float(
                        (register as f64 + if width { 2.0 } else { 3.0 }) / CLOCK,
                    ))
                } else {
                    Ok(json!(register))
                }
            }
            "set_pulser_delay_ticks" | "set_pulser_width_ticks" => {
                let width = method.contains("width");
                let p = Params::new(
                    args,
                    kwargs,
                    &[
                        "pulser_no",
                        if width { "width" } else { "delay" },
                        "timeout_s",
                    ],
                    2,
                    3,
                )?;
                let value = uint(&p[1], if width { "width" } else { "delay" }, u32::MAX)?;
                self.require_connected()?;
                let pulser = self.pulser(&p[0], ctx)?;
                self.pulser_write(pulser, value, width, ctx)?;
                Ok(Value::Null)
            }
            "set_pulser_duty_cycle" => {
                let p = Params::new(
                    args,
                    kwargs,
                    &["pulser_no", "duty_cycle", "timeout_s"],
                    2,
                    3,
                )?;
                let duty = finite(&p[1], "duty_cycle")?;
                if !(0.0 < duty && duty <= 1.0) {
                    return Err(Error::argument(
                        "duty_cycle must satisfy 0 < duty_cycle <= 1",
                    ));
                }
                self.require_connected()?;
                let pulser = self.pulser(&p[0], ctx)?;
                let width =
                    ((self.oscillator_period(ctx)? as f64 + 2.0) * duty - 2.0).round_ties_even();
                if !(1.0..=u32::MAX as f64).contains(&width) {
                    return Err(Error::argument(format!(
                        "duty_cycle={duty} produces an invalid width register: {width}"
                    )));
                }
                self.pulser_write(pulser, width as u32, true, ctx)?;
                Ok(Value::Null)
            }
            "get_product_info" => {
                Params::new(args, kwargs, &["timeout_s"], 0, 1)?;
                self.product_info(ctx)
            }
            "collect_housekeeping" => {
                Params::new(args, kwargs, &["timeout_s"], 0, 1)?;
                self.snapshot(ctx)
            }
            "collect_state_snapshot" if self.hd => {
                Params::new(args, kwargs, &["timeout_s"], 0, 1)?;
                self.state_snapshot(ctx)
            }
            "shutdown" => {
                let p = Params::new(
                    args,
                    kwargs,
                    &["standby_config", "disable_device", "timeout_s"],
                    0,
                    0,
                )?;
                let standby = optional_uint(&p[0], "standby_config", self.config_limit() - 1)?;
                self.shutdown(standby, p.bool_or(1, true)?, ctx)
            }
            "describe_error" | "format_status" => {
                let p = Params::new(args, kwargs, &["status"], 1, 1)?;
                let status = p[0]
                    .as_i64()
                    .ok_or_else(|| Error::argument("status must be an integer"))?;
                let description = self.errors[status.to_string()]
                    .as_str()
                    .unwrap_or("Unknown status code");
                Ok(json!(if method == "format_status" {
                    format!("{status} ({description})")
                } else {
                    description.to_owned()
                }))
            }
            _ => self.call_base(method, args, kwargs, ctx),
        }
    }

    fn product_info(&mut self, ctx: &Context) -> Result<Value> {
        self.require_connected()?;
        let number = self.read("GetProductNo", &[json!(0)], 1, ctx)?;
        let id = self.read(
            "GetProductID",
            &[text_buffer(if self.hd { 81 } else { 60 })],
            1,
            ctx,
        )?;
        let fw = self.read(
            if self.hd {
                "GetFwVersion"
            } else {
                "GetFWVersion"
            },
            &[json!(0)],
            1,
            ctx,
        )?;
        let date = self.read(
            if self.hd { "GetFwDate" } else { "GetFWDate" },
            &[text_buffer(if self.hd { 12 } else { 16 })],
            1,
            ctx,
        )?;
        let hw = self.read(
            if self.hd { "GetHwType" } else { "GetHWType" },
            &vec![json!(0); if self.hd { 9 } else { 1 }],
            if self.hd { 9 } else { 1 },
            ctx,
        )?;
        let version = self.read(
            if self.hd {
                "GetHwVersion"
            } else {
                "GetHWVersion"
            },
            &vec![json!(0); if self.hd { 2 } else { 1 }],
            if self.hd { 2 } else { 1 },
            ctx,
        )?;
        let mut hardware = json!({"type": hw[0], "version": version[0]});
        if self.hd {
            hardware["fpga_version"] = version[1].clone();
            hardware["modules"] = json!({"signal": hw[1], "register": hw[2], "burst_timer": hw[3],
                "timer": hw[4], "oscillator": hw[5], "mapping": hw[6],
                "switch_dual_level": hw[7], "switch_tri_level": hw[8]});
        }
        Ok(json!({"product_no": number[0], "product_id": id[0],
            "firmware": {"version": fw[0], "date": date[0]}, "hardware": hardware}))
    }

    fn state_snapshot(&mut self, ctx: &Context) -> Result<Value> {
        self.require_connected()?;
        let (main, device, controller) = if self.hd {
            let state = self.read("GetDeviceState", &[json!(0), json!(0), json!(0)], 3, ctx)?;
            let main = uint(&state[0], "main state", 65535)?;
            let device = uint(&state[1], "device state", 65535)?;
            let control = self.read("GetState", &[json!(0), json!(0)], 2, ctx)?;
            let control = uint(&control[0], "controller state", u32::MAX)?;
            (
                json!({"hex": format!("0x{main:x}"), "name": main_name(main, true)}),
                json!({"hex": format!("0x{device:x}"), "flags": flags(device, HD_DEVICE_BITS, Some("DEVST_OK"), None)}),
                json!({"hex": format!("0x{control:x}"), "flags": flags(control, HD_CONTROLLER_BITS, None, None)}),
            )
        } else {
            let main = self.read("GetMainState", &[json!(0)], 1, ctx)?;
            let main = uint(&main[0], "main state", 65535)?;
            let device = self.read("GetDeviceState", &[json!(0)], 1, ctx)?;
            let device = uint(&device[0], "device state", u32::MAX)?;
            let controller = self.read("GetControllerState", &[json!(0)], 1, ctx)?;
            let controller = uint(&controller[0], "controller state", 65535)?;
            (
                json!({"hex": format!("0x{main:x}"), "name": main_name(main, false)}),
                json!({"hex": format!("0x{device:x}"), "flags": flags(device, DEVICE_BITS, Some("DEVST_OK"), Some(8))}),
                json!({"hex": format!("0x{controller:x}"), "flags": flags(controller, CONTROLLER_BITS, None, Some(4))}),
            )
        };
        Ok(
            json!({"main_state": main, "device_state": device, "controller_state": controller,
            "device_enabled": self.get_enabled(ctx)?}),
        )
    }

    fn optional_routing(&mut self, suffix: &str, tail: &[Value], ctx: &Context) -> Result<Value> {
        match self.read(suffix, tail, 1, ctx) {
            Ok(values) => Ok(values[0].clone()),
            Err(error) if missing_export(&error) => Ok(Value::Null),
            Err(error) => Err(error),
        }
    }

    fn snapshot(&mut self, ctx: &Context) -> Result<Value> {
        let mut snapshot = self.state_snapshot(ctx)?;
        let housekeeping = self.read(
            "GetHousekeeping",
            &vec![json!(0.0); if self.hd { 8 } else { 4 }],
            if self.hd { 8 } else { 4 },
            ctx,
        )?;
        if self.hd {
            snapshot["housekeeping"] = json!({"volt_12v_v": housekeeping[0], "volt_fans_v": housekeeping[1],
                "volt_5v0_v": housekeeping[2], "volt_3v3_v": housekeeping[3], "volt_3v3p_v": housekeeping[4],
                "volt_2v5p_v": housekeeping[5], "volt_vc_v": housekeeping[6], "temp_cpu_c": housekeeping[7]});
        } else {
            snapshot["housekeeping"] = json!({"volt_12v_v": housekeeping[0], "volt_5v0_v": housekeeping[1],
                "volt_3v3_v": housekeeping[2], "temp_cpu_c": housekeeping[3]});
            let sensors = self.read("GetSensorData", &[json!(vec![0.0; 3])], 1, ctx)?;
            array(&sensors[0], 3, "sensor data")?;
            snapshot["sensors_c"] = sensors[0].clone();
            let fans = self.read(
                "GetFanData",
                &[
                    json!(vec![false; 3]),
                    json!(vec![false; 3]),
                    json!(vec![0; 3]),
                    json!(vec![0; 3]),
                    json!(vec![0; 3]),
                ],
                5,
                ctx,
            )?;
            for values in &fans {
                array(values, 3, "fan data")?;
            }
            let mut fan_rows = Vec::new();
            for (index, enabled) in array(&fans[0], 3, "fan enabled")?.iter().enumerate() {
                fan_rows.push(
                    json!({"fan": index, "enabled": boolean(enabled, "fan enabled")?,
                    "failed": boolean(&fans[1][index], "fan failed")?, "set_rpm": fans[2][index],
                    "measured_rpm": fans[3][index], "pwm": fans[4][index]}),
                );
            }
            snapshot["fans"] = json!(fan_rows);
            let led = self.read(
                "GetLEDData",
                &[json!(false), json!(false), json!(false)],
                3,
                ctx,
            )?;
            snapshot["led"] = json!({"red": boolean(&led[0], "red")?, "green": boolean(&led[1], "green")?, "blue": boolean(&led[2], "blue")?});
            let cpu = self.read("GetCPUData", &[json!(0.0), json!(0.0)], 2, ctx)?;
            snapshot["cpu"] = json!({"load": cpu[0], "frequency_hz": cpu[1]});
            let uptime = self.read("GetUptime", &[json!(0), json!(0), json!(0)], 3, ctx)?;
            let total = self.read("GetTotalTime", &[json!(0), json!(0)], 2, ctx)?;
            snapshot["uptime"] = json!({"seconds": uptime[0], "milliseconds": uptime[1], "operation_seconds": uptime[2],
                "total_uptime_seconds": total[0], "total_operation_seconds": total[1]});
        }
        let period = self.oscillator_period(ctx)?;
        snapshot["oscillator"] =
            json!({"period": period, "frequency_hz": float(CLOCK / (period as f64 + 2.0))});
        let count = if self.hd {
            let count = self.read("GetTimerCount", &[json!(0)], 1, ctx)?;
            uint(&count[0], "timer count", 65535)?
        } else {
            4
        };
        if !self.hd {
            let trigger =
                self.optional_routing("GetSwitchTriggerMappingEnable", &[json!(false)], ctx)?;
            let enable =
                self.optional_routing("GetSwitchEnableMappingEnable", &[json!(false)], ctx)?;
            snapshot["switch_mapping"] = json!({"trigger_enabled": if trigger.is_null() { Value::Null } else { json!(boolean(&trigger, "trigger mapping")?) },
                "enable_enabled": if enable.is_null() { Value::Null } else { json!(boolean(&enable, "enable mapping")?) }});
        }
        let mut pulsers = Vec::new();
        for number in 0..count {
            ctx.check_cancelled()?;
            let delay = self.pulser_read(number, false, ctx)?;
            let width = self.pulser_read(number, true, ctx)?;
            let burst = if self.hd {
                match self.read("GetTimerBurst", &[json!(number), json!(0)], 1, ctx) {
                    Ok(values) => values[0].clone(),
                    Err(error)
                        if ctx.is_cancelled() || ctx.remaining().is_zero() || self.poisoned =>
                    {
                        return Err(error);
                    }
                    Err(_) => Value::Null,
                }
            } else if number < 2 {
                self.read("GetPulserBurst", &[json!(number), json!(0)], 1, ctx)?[0].clone()
            } else {
                Value::Null
            };
            let mut row = json!({"pulser": number, "label": if number < 4 { format!("pulser_{number}") } else { number.to_string() },
                "delay_ticks": delay, "width_ticks": width, "burst": burst});
            if !self.hd {
                let config = if number < 2 { number * 2 } else { number + 2 };
                row["trigger_config"] =
                    self.optional_routing("GetPulserConfig", &[json!(config), json!(0)], ctx)?;
                row["stop_config"] = if number < 2 {
                    self.optional_routing(
                        "GetPulserConfig",
                        &[json!(number * 2 + 1), json!(0)],
                        ctx,
                    )?
                } else {
                    Value::Null
                };
            }
            pulsers.push(row);
        }
        snapshot["pulsers"] = json!(pulsers);
        if !self.hd {
            let mut switches = Vec::new();
            for number in 0..4 {
                let trigger =
                    self.read("GetSwitchTriggerConfig", &[json!(number), json!(0)], 1, ctx)?;
                let enable =
                    self.read("GetSwitchEnableConfig", &[json!(number), json!(0)], 1, ctx)?;
                let delay = self.read(
                    "GetSwitchTriggerDelay",
                    &[json!(number), json!(0), json!(0)],
                    2,
                    ctx,
                )?;
                let enable_delay =
                    self.read("GetSwitchEnableDelay", &[json!(number), json!(0)], 1, ctx)?;
                switches.push(json!({"switch": number, "trigger_config": trigger[0], "enable_config": enable[0],
                    "trigger_delay": {"rise": delay[0], "fall": delay[1]}, "enable_delay": enable_delay[0]}));
            }
            snapshot["switches"] = json!(switches);
        }
        Ok(snapshot)
    }

    fn call_base(
        &mut self,
        method: &str,
        args: &[Value],
        kwargs: &Value,
        ctx: &Context,
    ) -> Result<Value> {
        match method {
            "get_main_state" if !self.hd => {
                Params::new(args, kwargs, &[], 0, 0)?;
                let reply = self.raw("GetMainState", &[json!(0)], ctx)?;
                let status = reply.status;
                let values = outputs(reply, 1)?;
                let state = uint(&values[0], "main state", 65535)?;
                Ok(tuple(vec![
                    json!(status),
                    json!(format!("0x{state:x}")),
                    json!(main_name(state, false)),
                ]))
            }
            "get_device_state" => {
                Params::new(args, kwargs, &[], 0, 0)?;
                let reply = self.raw(
                    "GetDeviceState",
                    &vec![json!(0); if self.hd { 3 } else { 1 }],
                    ctx,
                )?;
                let status = reply.status;
                let values = outputs(reply, if self.hd { 3 } else { 1 })?;
                if self.hd {
                    let main = uint(&values[0], "main state", 65535)?;
                    let dev = uint(&values[1], "device state", 65535)?;
                    let temp = uint(&values[2], "temperature state", 65535)?;
                    Ok(tuple(vec![
                        json!(status),
                        json!(format!("0x{main:x}")),
                        json!(main_name(main, true)),
                        json!(format!("0x{dev:x}")),
                        flags(dev, HD_DEVICE_BITS, Some("DEVST_OK"), None),
                        json!(format!("0x{temp:x}")),
                        flags(temp, HD_TEMPERATURE_BITS, Some("TMPST_OK"), None),
                    ]))
                } else {
                    let state = uint(&values[0], "device state", u32::MAX)?;
                    Ok(tuple(vec![
                        json!(status),
                        json!(format!("0x{state:x}")),
                        flags(state, DEVICE_BITS, Some("DEVST_OK"), Some(8)),
                    ]))
                }
            }
            "get_controller_state" if !self.hd => {
                Params::new(args, kwargs, &[], 0, 0)?;
                let reply = self.raw("GetControllerState", &[json!(0)], ctx)?;
                let status = reply.status;
                let values = outputs(reply, 1)?;
                let state = uint(&values[0], "controller state", 65535)?;
                Ok(tuple(vec![
                    json!(status),
                    json!(format!("0x{state:x}")),
                    flags(state, CONTROLLER_BITS, None, Some(4)),
                ]))
            }
            "get_config_name" | "get_config_flags" => {
                let p = Params::new(args, kwargs, &["config_number"], 1, 1)?;
                let number = self.config_number(&p[0])?;
                let (suffix, tail, count) = if method == "get_config_name" {
                    (
                        "GetConfigName",
                        vec![json!(number), text_buffer(if self.hd { 193 } else { 52 })],
                        1,
                    )
                } else {
                    (
                        "GetConfigFlags",
                        vec![json!(number), json!(false), json!(false)],
                        2,
                    )
                };
                native_tuple(self.raw(suffix, &tail, ctx)?, count)
            }
            "get_config_list" => {
                Params::new(args, kwargs, &[], 0, 0)?;
                let count = self.config_limit() as usize;
                let reply = self.raw(
                    "GetConfigList",
                    &[json!(vec![false; count]), json!(vec![false; count])],
                    ctx,
                )?;
                let status = reply.status;
                let mut values = outputs(reply, 2)?;
                for value in &mut values {
                    *value = json!(
                        array(value, count, "config flags")?
                            .iter()
                            .map(|v| boolean(v, "config flag"))
                            .collect::<Result<Vec<_>>>()?
                    );
                }
                values.insert(0, json!(status));
                Ok(tuple(values))
            }
            "load_current_config" | "save_current_config" => {
                let p = Params::new(args, kwargs, &["config_number"], 1, 1)?;
                let number = self.config_number(&p[0])?;
                let reply = self.raw(
                    if method == "load_current_config" {
                        "LoadCurrentConfig"
                    } else {
                        "SaveCurrentConfig"
                    },
                    &[json!(number)],
                    ctx,
                )?;
                if reply.status == 0 && method == "load_current_config" {
                    self.clear_loaded();
                }
                outputs(reply.clone(), 0)?;
                Ok(json!(reply.status))
            }
            "set_device_enable" => {
                let p = Params::new(args, kwargs, &["enable"], 1, 1)?;
                let reply =
                    self.raw("SetDeviceEnable", &[json!(boolean(&p[0], "enable")?)], ctx)?;
                outputs(reply.clone(), 0)?;
                Ok(json!(reply.status))
            }
            "get_housekeeping" | "get_cpu_data" | "get_uptime" | "get_total_time"
            | "get_led_data" | "get_device_enable" | "device_purge" | "get_buffer_state"
            | "get_product_no" | "get_product_id" | "get_fw_version" | "get_fw_date"
            | "get_hw_type" | "get_hw_version" | "get_sensor_data" | "get_fan_data" => {
                Params::new(args, kwargs, &[], 0, 0)?;
                let (suffix, tail, count) = match method {
                    "get_housekeeping" => (
                        "GetHousekeeping",
                        vec![json!(0.0); if self.hd { 8 } else { 4 }],
                        if self.hd { 8 } else { 4 },
                    ),
                    "get_cpu_data" => ("GetCPUData", vec![json!(0.0); 2], 2),
                    "get_uptime" => ("GetUptime", vec![json!(0); 3], 3),
                    "get_total_time" => ("GetTotalTime", vec![json!(0); 2], 2),
                    "get_led_data" => ("GetLEDData", vec![json!(false); 3], 3),
                    "get_device_enable" => ("GetDeviceEnable", vec![json!(false)], 1),
                    "device_purge" => ("DevicePurge", vec![json!(false)], 1),
                    "get_buffer_state" => ("GetBufferState", vec![json!(false)], 1),
                    "get_product_no" => ("GetProductNo", vec![json!(0)], 1),
                    "get_product_id" => (
                        "GetProductID",
                        vec![text_buffer(if self.hd { 81 } else { 60 })],
                        1,
                    ),
                    "get_fw_version" => (
                        if self.hd {
                            "GetFwVersion"
                        } else {
                            "GetFWVersion"
                        },
                        vec![json!(0)],
                        1,
                    ),
                    "get_fw_date" => (
                        if self.hd { "GetFwDate" } else { "GetFWDate" },
                        vec![text_buffer(if self.hd { 12 } else { 16 })],
                        1,
                    ),
                    "get_hw_type" => (
                        if self.hd { "GetHwType" } else { "GetHWType" },
                        vec![json!(0); if self.hd { 9 } else { 1 }],
                        if self.hd { 9 } else { 1 },
                    ),
                    "get_hw_version" => (
                        if self.hd {
                            "GetHwVersion"
                        } else {
                            "GetHWVersion"
                        },
                        vec![json!(0); if self.hd { 2 } else { 1 }],
                        if self.hd { 2 } else { 1 },
                    ),
                    "get_sensor_data" => ("GetSensorData", vec![json!(vec![0.0; 3])], 1),
                    "get_fan_data" => (
                        "GetFanData",
                        vec![
                            json!(vec![false; 3]),
                            json!(vec![false; 3]),
                            json!(vec![0; 3]),
                            json!(vec![0; 3]),
                            json!(vec![0; 3]),
                        ],
                        5,
                    ),
                    _ => unreachable!(),
                };
                let reply = self.raw(suffix, &tail, ctx)?;
                let status = reply.status;
                let mut values = outputs(reply, count)?;
                if tail.first().is_some_and(Value::is_boolean) {
                    for value in &mut values {
                        *value = json!(boolean(value, method)?);
                    }
                }
                if method == "get_fan_data" || method == "get_sensor_data" {
                    for (index, value) in values.iter_mut().enumerate() {
                        let row = array(value, 3, method)?;
                        if method == "get_fan_data" && index < 2 {
                            *value = json!(
                                row.iter()
                                    .map(|v| boolean(v, method))
                                    .collect::<Result<Vec<_>>>()?
                            );
                        }
                    }
                }
                values.insert(0, json!(status));
                Ok(tuple(values))
            }
            "purge" | "close_port" => {
                Params::new(args, kwargs, &[], 0, 0)?;
                let reply =
                    self.raw(if method == "purge" { "Purge" } else { "Close" }, &[], ctx)?;
                outputs(reply.clone(), 0)?;
                if method == "close_port" && reply.status == 0 {
                    self.connected = false;
                    self.claimed = false;
                    self.clear_loaded();
                }
                Ok(json!(reply.status))
            }
            "set_baud_rate" if !self.hd => {
                let p = Params::new(args, kwargs, &["baud_rate"], 1, 1)?;
                let baud = uint(&p[0], "baud_rate", u32::MAX)?;
                native_tuple(self.raw("SetBaudRate", &[json!(baud)], ctx)?, 1)
            }
            "set_comspeed" if self.hd => {
                let p = Params::new(args, kwargs, &["baud_rate"], 1, 1)?;
                let baud = uint(&p[0], "baud_rate", u32::MAX)?;
                native_tuple(self.raw("SetBaudRate", &[json!(baud)], ctx)?, 1)
            }
            "open_port" => {
                let p = Params::new(
                    args,
                    kwargs,
                    &[
                        "com_number",
                        if self.hd {
                            "stream_number"
                        } else {
                            "port_number"
                        },
                    ],
                    1,
                    2,
                )?;
                let com = uint(&p[0], "com_number", 65535)?;
                if com == 0 {
                    return Err(Error::argument("com_number must be >= 1"));
                }
                if self.connected || self.claimed {
                    return Err(Error::runtime(
                        "AMX DLL channel is already claimed; disconnect before opening it again",
                    ));
                }
                let port = if p[1].is_null() {
                    self.channel
                } else {
                    uint(&p[1], "port_number", 15)?
                };
                if port != self.channel {
                    return Err(Error::argument(
                        "open_port must use this worker's configured port",
                    ));
                }
                self.claimed = true;
                let reply = self.native("Open", &[json!(port), json!(com)], ctx)?;
                outputs(reply.clone(), 0)?;
                self.open_failed = reply.status != 0;
                Ok(json!(reply.status))
            }
            _ if !self.hd => self.call_amx_base(method, args, kwargs, ctx),
            _ => Err(Error::unsupported(format!(
                "Unsupported AMX HD method: {method}"
            ))),
        }
    }

    fn call_amx_base(
        &mut self,
        method: &str,
        args: &[Value],
        kwargs: &Value,
        ctx: &Context,
    ) -> Result<Value> {
        match method {
            "get_oscillator_period"
            | "get_switch_trigger_mapping_enable"
            | "get_switch_enable_mapping_enable" => {
                Params::new(args, kwargs, &[], 0, 0)?;
                let suffix = match method {
                    "get_oscillator_period" => "GetOscillatorPeriod",
                    "get_switch_trigger_mapping_enable" => "GetSwitchTriggerMappingEnable",
                    _ => "GetSwitchEnableMappingEnable",
                };
                let reply = self.raw(
                    suffix,
                    &[if method == "get_oscillator_period" {
                        json!(0)
                    } else {
                        json!(false)
                    }],
                    ctx,
                )?;
                native_tuple(reply, 1)
            }
            "set_oscillator_period" | "set_controller_config" => {
                let name = if method == "set_oscillator_period" {
                    "period"
                } else {
                    "config"
                };
                let p = Params::new(args, kwargs, &[name], 1, 1)?;
                let value = uint(&p[0], name, if name == "period" { u32::MAX } else { 65535 })?;
                // The reference accepts WORD-range configs; the vendor ABI
                // takes BYTE and therefore transmits only the lower eight bits.
                let value = if name == "config" { value & 255 } else { value };
                let reply = self.raw(
                    if name == "period" {
                        "SetOscillatorPeriod"
                    } else {
                        "SetControllerConfig"
                    },
                    &[json!(value)],
                    ctx,
                )?;
                outputs(reply.clone(), 0)?;
                Ok(json!(reply.status))
            }
            "get_pulser_delay" | "get_pulser_width" | "get_pulser_burst" | "get_pulser_config" => {
                let name = if method == "get_pulser_config" {
                    "config_no"
                } else {
                    "pulser_no"
                };
                let p = Params::new(args, kwargs, &[name], 1, 1)?;
                let maximum = match method {
                    "get_pulser_config" => 5,
                    "get_pulser_burst" => 1,
                    _ => 3,
                };
                let number = uint(&p[0], name, maximum)?;
                let suffix = match method {
                    "get_pulser_delay" => "GetPulserDelay",
                    "get_pulser_width" => "GetPulserWidth",
                    "get_pulser_burst" => "GetPulserBurst",
                    _ => "GetPulserConfig",
                };
                native_tuple(self.raw(suffix, &[json!(number), json!(0)], ctx)?, 1)
            }
            "set_pulser_delay" | "set_pulser_width" | "set_pulser_burst" => {
                let (name, suffix, maximum) = match method {
                    "set_pulser_delay" => ("delay", "SetPulserDelay", 3),
                    "set_pulser_width" => ("width", "SetPulserWidth", 3),
                    _ => ("burst", "SetPulserBurst", 1),
                };
                let p = Params::new(args, kwargs, &["pulser_no", name], 2, 2)?;
                let number = uint(&p[0], "pulser_no", maximum)?;
                let value = uint(&p[1], name, u32::MAX)?;
                let reply = self.raw(suffix, &[json!(number), json!(value)], ctx)?;
                outputs(reply.clone(), 0)?;
                Ok(json!(reply.status))
            }
            "get_switch_trigger_config" | "get_switch_enable_config" => {
                let p = Params::new(args, kwargs, &["switch_no"], 1, 1)?;
                let number = uint(&p[0], "switch_no", 3)?;
                native_tuple(
                    self.raw(
                        if method == "get_switch_trigger_config" {
                            "GetSwitchTriggerConfig"
                        } else {
                            "GetSwitchEnableConfig"
                        },
                        &[json!(number), json!(0)],
                        ctx,
                    )?,
                    1,
                )
            }
            "get_switch_trigger_delay" | "get_switch_enable_delay" => {
                let p = Params::new(args, kwargs, &["switch_no", "timeout_s"], 1, 2)?;
                self.require_connected()?;
                let number = uint(&p[0], "switch_no", 3)?;
                if method == "get_switch_trigger_delay" {
                    Ok(tuple(self.read(
                        "GetSwitchTriggerDelay",
                        &[json!(number), json!(0), json!(0)],
                        2,
                        ctx,
                    )?))
                } else {
                    Ok(
                        self.read("GetSwitchEnableDelay", &[json!(number), json!(0)], 1, ctx)?[0]
                            .clone(),
                    )
                }
            }
            "set_switch_trigger_delay" => {
                let p = Params::new(
                    args,
                    kwargs,
                    &["switch_no", "rise_delay", "fall_delay", "timeout_s"],
                    3,
                    4,
                )?;
                self.require_connected()?;
                let number = uint(&p[0], "switch_no", 3)?;
                let rise = uint(&p[1], "rise_delay", 15)?;
                let fall = uint(&p[2], "fall_delay", 15)?;
                self.write(
                    "SetSwitchTriggerDelay",
                    &[json!(number), json!(rise), json!(fall)],
                    ctx,
                )?;
                Ok(Value::Null)
            }
            "set_switch_enable_delay" => {
                let p = Params::new(args, kwargs, &["switch_no", "delay", "timeout_s"], 2, 3)?;
                self.require_connected()?;
                let number = uint(&p[0], "switch_no", 3)?;
                let delay = uint(&p[1], "delay", 15)?;
                self.write("SetSwitchEnableDelay", &[json!(number), json!(delay)], ctx)?;
                Ok(Value::Null)
            }
            _ => Err(Error::unsupported(format!(
                "Unsupported AMX method: {method}"
            ))),
        }
    }

    pub(crate) fn attribute(&self, name: &str) -> Result<Value> {
        Ok(match name {
            "device_id" | "idn" => json!(self.device_id),
            "com" => json!(self.com),
            "port_num" => json!(self.channel),
            "port" if !self.hd => json!(self.channel),
            "stream" if self.hd => json!(self.channel),
            "baudrate" => json!(self.baudrate),
            "connected" => json!(self.connected),
            "_dll_port_claimed" => json!(self.claimed),
            "_transport_poisoned" => json!(self.poisoned),
            "_transport_error" => json!(self.transport_error),
            "_opening_in_progress" => json!(self.opening),
            "_open_failed" => json!(self.open_failed),
            "_failed_open_released" => json!(self.failed_open_released),
            "loaded_config_number" => json!(self.loaded_number),
            "loaded_config_name" => json!(self.loaded_name),
            "loaded_config_source" => json!(self.loaded_source),
            "recording" => json!(self.recording),
            "CLOCK" => json!(CLOCK),
            "DEF_CLOCK" if self.hd => json!(CLOCK),
            "OSC_OFFSET" | "PULSER_WIDTH_OFFSET" => json!(2),
            "PULSER_DELAY_OFFSET" => json!(3),
            "TIMER_WIDTH_OFFSET" if self.hd => json!(2),
            "TIMER_DELAY_OFFSET" if self.hd => json!(3),
            "PULSER_NUM" | "SWITCH_NUM" => json!(4),
            "PULSER_BURST_NUM" if !self.hd => json!(2),
            "SWITCH_DELAY_MAX" => json!(16),
            "MAX_CONFIG" => json!(self.config_limit()),
            "CONFIG_NAME_SIZE" => json!(if self.hd { 193 } else { 52 }),
            "PRODUCT_ID_SIZE" => json!(if self.hd { 81 } else { 60 }),
            "FW_DATE_SIZE" if !self.hd => json!(16),
            "DATA_STRING_SIZE" if self.hd => json!(12),
            "SENSOR_NUM" | "FAN_NUM" if !self.hd => json!(3),
            "SEN_COUNT" | "FAN_COUNT" if self.hd => json!(3),
            "FAN_PWM_MAX" => json!(1000),
            "MAX_PORT" if !self.hd => json!(16),
            "MAX_STREAM" if self.hd => json!(16),
            "UINT32_MAX" => json!(u32::MAX),
            "NO_ERR" => json!(0),
            "ERR_PORT_RANGE" | "ERR_STREAM_RANGE" => json!(-1),
            "ERR_OPEN" => json!(-2),
            "ERR_CLOSE" => json!(-3),
            "ERR_PURGE" => json!(-4),
            "ERR_CONTROL" => json!(-5),
            "ERR_STATUS" => json!(-6),
            "ERR_COMMAND_SEND" => json!(-7),
            "ERR_DATA_SEND" => json!(-8),
            "ERR_TERM_SEND" => json!(-9),
            "ERR_COMMAND_RECEIVE" => json!(-10),
            "ERR_DATA_RECEIVE" => json!(-11),
            "ERR_TERM_RECEIVE" => json!(-12),
            "ERR_COMMAND_WRONG" => json!(-13),
            "ERR_ARGUMENT_WRONG" => json!(-14),
            "ERR_ARGUMENT" => json!(-15),
            "ERR_RATE" => json!(-16),
            "ERR_NOT_CONNECTED" => json!(-100),
            "ERR_NOT_READY" => json!(-101),
            "ERR_READY" => json!(-102),
            "ERR_CONFIG" if self.hd => json!(-200),
            "ERR_CONFIG_EMPTY" if self.hd => json!(-201),
            "ERR_DEBUG_OPEN" => json!(-400),
            "ERR_DEBUG_CLOSE" => json!(-401),
            "_process_backend_disabled_reason" => json!(""),
            "err_dict" => self.errors.clone(),
            "MAIN_STATE" => {
                let states: &[u32] = if self.hd {
                    &[0, 1, 0x8000, 0x8001, 0x8002, 0x8003]
                } else {
                    &[0, 0x8000, 0x8001, 0x8002, 0x8003, 0x8004]
                };
                json!({"$map": states.iter().map(|state| json!([state, main_name(*state, self.hd)])).collect::<Vec<_>>()})
            }
            "DEVICE_STATE" => state_map(if self.hd { HD_DEVICE_BITS } else { DEVICE_BITS }),
            "CONTROLLER_STATE" => {
                if self.hd {
                    state_map(
                        &HD_CONTROLLER_BITS
                            .iter()
                            .copied()
                            .filter(|(bit, _)| *bit < 4096)
                            .collect::<Vec<_>>(),
                    )
                } else {
                    state_map(CONTROLLER_BITS)
                }
            }
            "EXTENDED_STATE" if self.hd => state_map(
                &HD_CONTROLLER_BITS
                    .iter()
                    .copied()
                    .filter(|(bit, _)| *bit >= 4096)
                    .collect::<Vec<_>>(),
            ),
            "TEMPERATURE_STATE" if self.hd => state_map(HD_TEMPERATURE_BITS),
            "PULSER_LABELS" => {
                json!({"$map": [[0,"pulser_0"],[1,"pulser_1"],[2,"pulser_2"],[3,"pulser_3"]]})
            }
            _ => {
                return Err(Error::new(
                    "AttributeError",
                    format!("Unknown AMX attribute: {name}"),
                ));
            }
        })
    }

    pub(crate) fn assign_attribute(&mut self, name: &str, value: Value) -> Result<()> {
        match name {
            "recording" => {
                self.recording = boolean(&value, "recording")?;
                Ok(())
            }
            "baudrate" | "com" | "port" | "port_num" | "stream" => {
                if self.connected || self.claimed {
                    return Err(Error::runtime(
                        "Cannot change AMX addressing or baudrate while a DLL channel is claimed",
                    ));
                }
                match name {
                    "baudrate" => {
                        let value = uint(&value, name, u32::MAX)?;
                        if value == 0 {
                            return Err(Error::argument("baudrate must be > 0"));
                        }
                        self.baudrate = value;
                    }
                    "com" => {
                        let value = uint(&value, name, 65535)?;
                        if value == 0 {
                            return Err(Error::argument("com must be >= 1"));
                        }
                        self.com = value;
                    }
                    "stream" if self.hd => self.channel = uint(&value, name, 15)?,
                    "port" if !self.hd => self.channel = uint(&value, name, 15)?,
                    "port_num" => self.channel = uint(&value, name, 15)?,
                    _ => {
                        return Err(Error::new(
                            "AttributeError",
                            format!("Unknown AMX attribute: {name}"),
                        ));
                    }
                }
                Ok(())
            }
            _ => Err(Error::new(
                "AttributeError",
                format!("Read-only or unknown AMX attribute: {name}"),
            )),
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
        self.begin_call();
        ctx.check_cancelled()?;
        let result = self.call_common(method, args, kwargs, ctx);
        self.finish_call(result, ctx)
    }

    fn get_attribute(&self, name: &str) -> Result<Value> {
        self.attribute(name)
    }
    fn set_attribute(&mut self, name: &str, value: Value) -> Result<()> {
        self.assign_attribute(name, value)
    }
}

pub(crate) struct Params(Vec<Value>);

impl Params {
    pub(crate) fn new(
        args: &[Value],
        kwargs: &Value,
        names: &[&str],
        required: usize,
        positional: usize,
    ) -> Result<Self> {
        if args.len() > positional {
            return Err(type_error("Too many positional arguments"));
        }
        let empty = Map::new();
        let kwargs = if kwargs.is_null() {
            &empty
        } else {
            kwargs
                .as_object()
                .ok_or_else(|| type_error("kwargs must be an object"))?
        };
        if let Some(name) = kwargs.keys().find(|name| !names.contains(&name.as_str())) {
            return Err(type_error(format!("Unexpected keyword argument: {name}")));
        }
        let mut values = vec![Value::Null; names.len()];
        for (index, name) in names.iter().enumerate() {
            if let Some(value) = args.get(index) {
                if kwargs.contains_key(*name) {
                    return Err(type_error(format!("Multiple values for argument: {name}")));
                }
                values[index] = value.clone();
            } else if let Some(value) = kwargs.get(*name) {
                values[index] = value.clone();
            } else if index < required {
                return Err(type_error(format!("Missing argument: {name}")));
            }
            if *name == "timeout_s"
                && !values[index].is_null()
                && finite(&values[index], "timeout_s")? <= 0.0
            {
                return Err(Error::argument("AMX timeout_s must be greater than 0."));
            }
        }
        Ok(Self(values))
    }
    pub(crate) fn bool_or(&self, index: usize, default: bool) -> Result<bool> {
        if self[index].is_null() {
            Ok(default)
        } else {
            boolean(&self[index], "boolean argument")
        }
    }
}

impl std::ops::Index<usize> for Params {
    type Output = Value;
    fn index(&self, index: usize) -> &Value {
        &self.0[index]
    }
}

pub(crate) fn type_error(message: impl Into<String>) -> Error {
    Error::new("TypeError", message)
}
pub(crate) fn check_status(status: i64, action: &str) -> Result<()> {
    if status == 0 {
        Ok(())
    } else {
        Err(Error::status(status, action))
    }
}
pub(crate) fn outputs(reply: NativeReply, count: usize) -> Result<Vec<Value>> {
    if reply.values.len() != count {
        return Err(Error::runtime(format!(
            "Malformed AMX DLL reply: expected {count} pointer values, received {}",
            reply.values.len()
        )));
    }
    Ok(reply.values)
}
pub(crate) fn native_tuple(reply: NativeReply, count: usize) -> Result<Value> {
    let status = reply.status;
    let mut values = outputs(reply, count)?;
    values.insert(0, json!(status));
    Ok(tuple(values))
}
pub(crate) fn uint(value: &Value, name: &str, maximum: u32) -> Result<u32> {
    let integer = if let Some(v) = value.as_u64() {
        v
    } else if let Some(v) = value.as_i64() {
        u64::try_from(v).map_err(|_| Error::argument(format!("{name} must be >= 0")))?
    } else {
        return Err(Error::argument(format!("{name} must be an integer")));
    };
    if integer > maximum as u64 {
        return Err(Error::argument(format!(
            "{name} must satisfy 0 <= {name} <= {maximum}"
        )));
    }
    Ok(integer as u32)
}
fn optional_uint(value: &Value, name: &str, maximum: u32) -> Result<Option<u32>> {
    if value.is_null() {
        Ok(None)
    } else {
        uint(value, name, maximum).map(Some)
    }
}
pub(crate) fn finite(value: &Value, name: &str) -> Result<f64> {
    let number = value
        .as_f64()
        .ok_or_else(|| Error::argument(format!("{name} must be finite numeric data")))?;
    if !number.is_finite() {
        return Err(Error::argument(format!("{name} must be finite")));
    }
    Ok(number)
}
pub(crate) fn boolean(value: &Value, name: &str) -> Result<bool> {
    if let Some(v) = value.as_bool() {
        Ok(v)
    } else if let Some(v) = value.as_i64() {
        Ok(v != 0)
    } else {
        Err(Error::runtime(format!(
            "Invalid AMX {name}: expected bool or BOOL"
        )))
    }
}
pub(crate) fn array<'a>(value: &'a Value, size: usize, name: &str) -> Result<&'a Vec<Value>> {
    let values = value
        .as_array()
        .ok_or_else(|| Error::runtime(format!("Invalid AMX {name}: expected array")))?;
    if values.len() != size {
        return Err(Error::runtime(format!(
            "Invalid AMX {name}: expected {size} elements, received {}",
            values.len()
        )));
    }
    Ok(values)
}
pub(crate) fn text(value: &Value, name: &str) -> Result<String> {
    value
        .as_str()
        .map(str::to_owned)
        .ok_or_else(|| Error::runtime(format!("Invalid AMX {name}: expected string")))
}
pub(crate) fn text_buffer(capacity: usize) -> Value {
    json!({"capacity": capacity, "text": ""})
}
pub(crate) fn missing_export(error: &Error) -> bool {
    matches!(
        error.kind.as_str(),
        "AttributeError" | "NotImplementedError" | "MissingExportError"
    )
}
pub(crate) fn flags(
    value: u32,
    bits: &[(u32, &str)],
    zero: Option<&str>,
    unknown_width: Option<usize>,
) -> Value {
    let mut names: Vec<String> = bits
        .iter()
        .filter(|(bit, _)| value & bit != 0)
        .map(|(_, name)| (*name).to_owned())
        .collect();
    if value == 0
        && let Some(name) = zero
    {
        names.push(name.to_owned());
    }
    if let Some(width) = unknown_width {
        let known = bits.iter().fold(0, |mask, (bit, _)| mask | bit);
        let residual = value & !known;
        if residual != 0 {
            names.push(format!("UNKNOWN_BITS_0x{residual:0width$X}"));
        }
    }
    json!(names)
}
fn state_map(bits: &[(u32, &str)]) -> Value {
    json!({"$map": bits.iter().map(|(bit, name)| json!([bit, name])).collect::<Vec<_>>()})
}
pub(crate) fn main_name(state: u32, hd: bool) -> String {
    let name = match state {
        0 if hd => "STATE_STANDBY",
        0 => "STATE_ON",
        1 if hd => "STATE_ON",
        0x8000 => "STATE_ERROR",
        0x8001 => "STATE_ERR_VSUP",
        0x8002 => "STATE_ERR_TEMP_LOW",
        0x8003 => "STATE_ERR_TEMP_HIGH",
        0x8004 if !hd => "STATE_ERR_FPGA_DIS",
        _ => return format!("UNKNOWN_STATE_0x{state:04X}"),
    };
    name.to_owned()
}
fn shutdown_errors(errors: Vec<String>) -> Error {
    Error::runtime(format!(
        "AMX shutdown completed with {} error(s): {}",
        errors.len(),
        errors.join("; ")
    ))
}
fn with_cleanup(mut error: Error, cleanup: Result<()>) -> Error {
    if let Err(cleanup) = cleanup {
        error.message.push_str(&format!(
            "; cleanup failed, OFF/port release unconfirmed: {cleanup}"
        ));
    }
    error
}
