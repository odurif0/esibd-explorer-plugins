//! Standalone DMMR-8 facade. Currents remain in amps; vendor's `Curent` spelling
//! is intentional. Raw getters preserve status/tuple contracts. `poll_currents`
//! produces paired validity/range snapshots without reusing previous samples.
//! Recovery objects in the Python facade are not serializable: native recovery
//! state is owned here and reached through explicit configure/recover methods.

use crate::Backend;
use crate::codec::{float, tuple};
use crate::context::Context;
use crate::error::{Error, Result};
use crate::ffi::{Dll, NativeReply};
use serde_json::{Map, Value, json};
use std::collections::{BTreeMap, BTreeSet, VecDeque};
use std::fs::{self, File, OpenOptions};
use std::io::{Read, Write};
use std::path::PathBuf;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

pub const GUI_METHODS: &[&str] = &[
    "connect",
    "initialize",
    "get_state",
    "get_device_state",
    "get_voltage_state",
    "get_temperature_state",
    "get_module_current",
    "get_module_ready_flags",
    "get_module_meas_range",
    "set_module_meas_range",
    "set_module_auto_range",
    "get_enable",
    "set_enable",
    "get_automatic_current",
    "set_automatic_current",
    "get_current",
    "scan_modules",
    "get_module_info",
    "collect_housekeeping",
    "get_product_info",
    "begin_startup_diagnostics",
    "end_startup_diagnostics",
    "record_protocol_event",
    "configure_protocol_log",
    "protocol_diagnostics",
    "shutdown",
    "disconnect",
    "format_status",
    "begin_command_recovery",
    "recover_command",
    "verify_running",
    "configure_read_recovery",
    "recover_read",
    "configure_module_ranges",
    "poll_currents",
    "disable_acquisition",
];
pub const GUI_ATTRIBUTES: &[&str] = &[
    "NO_ERR",
    "NO_DATA",
    "connected",
    "_transport_poisoned",
    "_transport_error",
    "_dll_port_claimed",
    "_open_failed",
    "_failed_open_released",
    "_failed_open_cleanup_outcome",
    "_opening_in_progress",
    "_startup_log_path",
    "baudrate",
];
pub const PYTHON_OBJECT_API_GAPS: &[&str] = &["new_read_recovery", "new_command_recovery"];
const DEBUG_SHA256: &str = "e3bb4674f88e5a894c36cdb5e544f0b5a35691c2a0dbdc56719c9928f05bcea3";
const MAIN_STATE: &[(u64, &str)] = &[
    (0, "ST_ON"),
    (0x8000, "ST_ERROR"),
    (0x8001, "ST_ERR_MODULE"),
    (0x8002, "ST_ERR_VSUP"),
];
const DEVICE_STATE: &[(u64, &str)] = &[
    (1, "DS_PSU_ENB"),
    (256, "DS_VOLT_FAIL"),
    (1024, "DS_FAN_FAIL"),
    (4096, "DS_MODULE_FAIL"),
    (16384, "DS_HV_STOP"),
];
const VOLTAGE_STATE: &[(u64, &str)] = &[(1, "VS_3V3_OK"), (2, "VS_5V0_OK"), (4, "VS_12V_OK")];
const TEMPERATURE_STATE: &[(u64, &str)] = &[(16, "TS_TCPU_HIGH"), (4096, "TS_TCPU_LOW")];
const BASE_STATE: &[(u64, &str)] = &[
    (1, "BS_MRES"),
    (2, "BS_DIS_OVRD"),
    (4, "BS_DIS_CTRL"),
    (8, "BS_ILOCK_OVRD"),
    (16, "BS_ILOCK_CTRL"),
    (32, "BS_DITH_OVRD"),
    (64, "BS_DITH_CTRL"),
    (128, "BS_ENB"),
    (256, "BS_DISABLE"),
    (512, "BS_ENABLE"),
    (1024, "BS_ILOCK_IN"),
    (2048, "BS_ILOCK_OUT"),
    (4096, "BS_DITH_IN"),
    (8192, "BS_DITH_OUT"),
    (16384, "BS_ON_TEMP"),
    (32768, "BS_OFF_TEMP"),
];
const FAN_STATE: &[(u64, &str)] = &[
    (512, "FAN_OK"),
    (1024, "FAN_PWM_DIS"),
    (2048, "FAN_T_LOW"),
    (4096, "FAN_INSTAL"),
    (8192, "FAN_DIS"),
    (16384, "FAN_OVRD_DIS"),
    (32768, "FAN_CTRL_EXT"),
];
const MODULE_HK: &[&str] = &[
    "volt_3v3_v",
    "temp_cpu_c",
    "volt_5v0_v",
    "volt_12v_v",
    "volt_3v3i_v",
    "temp_cpui_c",
    "volt_2v5i_v",
    "volt_36vn_v",
    "volt_20vp_v",
    "volt_20vn_v",
    "volt_15vp_v",
    "volt_15vn_v",
    "volt_1v8p_v",
    "volt_1v8n_v",
    "volt_vrefp_v",
    "volt_vrefn_v",
];

struct ReadRecovery {
    ranges: BTreeMap<u64, (u64, bool)>,
    automatic: bool,
    recent: VecDeque<Instant>,
    awaiting: BTreeSet<u64>,
    count: u64,
    failed: bool,
    last: Value,
}
struct CommandRecovery {
    phase: String,
    used: bool,
    last: Value,
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
    optional_unsupported: BTreeSet<(String, Option<u64>)>,
    errors: Value,
    read_recovery: Option<ReadRecovery>,
    command_recovery: Option<CommandRecovery>,
    verified_ranges: BTreeMap<u64, (u64, bool)>,
    sample_sequence: u64,
    sample_snapshot: Value,
    timestamps: BTreeMap<u64, f64>,
    log_dir: Option<PathBuf>,
    protocol_path: Option<PathBuf>,
    protocol_error: Option<String>,
    events: VecDeque<Value>,
    event_count: u64,
    dll_path: String,
    dll_sha256: String,
    startup_path: Option<PathBuf>,
}

impl Controller {
    pub fn new(config: &Value, dll: Box<dyn Dll>) -> Result<Self> {
        let device_id = config
            .get("device_id")
            .and_then(Value::as_str)
            .ok_or_else(|| Error::new("TypeError", "DMMR device_id must be a string"))?;
        if device_id.trim().is_empty() {
            return Err(Error::argument("DMMR device_id must not be empty"));
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
            hk_running: false,
            failed_open_released: false,
            failed_open_cleanup_outcome: Value::Null,
            hk_interval_s,
            next_hk: Instant::now(),
            housekeeping: Value::Null,
            optional_unsupported: BTreeSet::new(),
            errors: serde_json::from_str(crate::ERROR_CATALOG)
                .map_err(|e| Error::runtime(format!("Invalid bundled error catalog: {e}")))?,
            read_recovery: None,
            command_recovery: None,
            verified_ranges: BTreeMap::new(),
            sample_sequence: 0,
            sample_snapshot: Value::Null,
            timestamps: BTreeMap::new(),
            log_dir: config
                .get("log_dir")
                .and_then(Value::as_str)
                .map(PathBuf::from),
            protocol_path: None,
            protocol_error: None,
            events: VecDeque::new(),
            event_count: 0,
            dll_path: config
                .get("dll_path")
                .and_then(Value::as_str)
                .unwrap_or("")
                .into(),
            dll_sha256: config
                .get("dll_sha256")
                .and_then(Value::as_str)
                .unwrap_or("")
                .into(),
            startup_path: None,
        })
    }

    fn native_raw(
        &mut self,
        export: &str,
        args: &[Value],
        count: usize,
        ctx: &Context,
    ) -> Result<NativeReply> {
        ctx.check_cancelled()?;
        if self.poisoned {
            return Err(Error::runtime(
                "DMMR DLL blocked; no recovery or concurrent calls permitted. Hardware stop is unconfirmed.",
            ));
        }
        let symbol = format!("COM_DMMR_8_{export}");
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
                "DMMR {export}: expected {count} pointer values, got {}",
                reply.values.len()
            )));
        }
        Ok(reply)
    }

    fn native(
        &mut self,
        export: &str,
        args: &[Value],
        count: usize,
        ctx: &Context,
    ) -> Result<NativeReply> {
        let reply = self.native_raw(export, args, count, ctx)?;
        if reply.status < 0 {
            // The diagnostics are clear-on-read, so capture them before another transaction.
            let mut event = json!({"kind": "dll_error", "action": export, "status": reply.status, "transport_poisoned": self.poisoned});
            for (key, diagnostic) in [
                ("get_io_state", "GetIOState"),
                ("get_comm_error", "GetCommError"),
            ] {
                if !ctx.remaining().is_zero() && !ctx.is_cancelled() && !self.poisoned {
                    event[key] = match self.native_raw(diagnostic, &[json!(0)], 1, ctx) {
                        Ok(data) => reply_tuple(data),
                        Err(error) => json!({"unavailable": error.to_string()}),
                    };
                }
            }
            event["transport_poisoned"] = json!(self.poisoned);
            self.record_event(event);
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
    fn active(&self, ctx: &Context) -> Result<()> {
        ctx.check_cancelled()?;
        if self.poisoned {
            return Err(Error::runtime(
                "DLL blocked; no recovery or concurrent calls permitted",
            ));
        }
        if !self.connected {
            return Err(Error::runtime(
                "Port disconnected; no automatic reconnection permitted",
            ));
        }
        Ok(())
    }

    fn connect(&mut self, ctx: &Context) -> Result<bool> {
        if self.poisoned {
            return Err(Error::runtime("DMMR transport unusable; recreate worker"));
        }
        if self.connected {
            return Ok(true);
        }
        if self.port_claimed {
            return Err(Error::runtime(
                "Previous DMMR open has not been released; disconnect first",
            ));
        }
        self.failed_open_released = false;
        self.failed_open_cleanup_outcome = Value::Null;
        self.port_claimed = true;
        self.open_failed = true;
        let outcome = (|| {
            ensure(self.write("Open", &[json!(self.com)], ctx)?, "open_port")?;
            self.connected = true;
            let baud = self.read("SetBaudRate", vec![], vec![json!(self.baudrate)], ctx)?;
            ensure(baud.status, "set_baud_rate")?;
            let kind = self.read("GetDevType", vec![], vec![json!(0)], ctx)?;
            ensure(kind.status, "get_device_type")?;
            if output_u64(&kind, 0)? != 0xAC38 {
                return Err(Error::runtime(format!(
                    "DMMR device type mismatch: expected 0xAC38, got 0x{:04X}",
                    output_u64(&kind, 0)?
                )));
            }
            let identity = self.read("GetProductID", vec![], vec![buffer(81, "")], ctx)?;
            if identity.status == 0
                && identity.values[0]
                    .as_str()
                    .is_some_and(|s| !s.is_empty() && !s.to_ascii_uppercase().contains("DMMR"))
            {
                self.record_event(json!({"kind": "identity_warning", "product_id": identity.values[0], "message": "Device type verified but product ID does not contain DMMR"}));
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
        if !self.connected && !self.port_claimed {
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
        if closed? != 0 {
            return Ok(false);
        }
        self.connected = false;
        self.port_claimed = false;
        self.open_failed = false;
        self.read_recovery = None;
        self.command_recovery = None;
        self.verified_ranges.clear();
        self.timestamps.clear();
        self.sample_snapshot = Value::Null;
        Ok(true)
    }

    fn initialize(&mut self, persist: bool, ctx: &Context) -> Result<Value> {
        let outcome = (|| {
            self.connect(ctx)?;
            ensure(self.write("RescanModules", &[], ctx)?, "rescan_modules")?;
            let modules = self.scan(ctx, false)?;
            let mismatch = self.read("GetScannedModuleState", vec![], vec![json!(false)], ctx)?;
            ensure(mismatch.status, "get_scanned_module_state")?;
            if output_bool(&mismatch, 0)? && persist {
                ensure(
                    self.write("SetScannedModuleState", &[], ctx)?,
                    "set_scanned_module_state",
                )?;
            }
            Ok(modules)
        })();
        if outcome.is_err() && !self.poisoned {
            let _ = self.disconnect(&ctx.cleanup(Duration::from_secs(5)));
        }
        outcome
    }

    fn state(&mut self, export: &str, ctx: &Context) -> Result<Value> {
        let reply = self.read(export, vec![], vec![json!(0)], ctx)?;
        // DMMRBase decodes initialized pointer values even when status is nonzero.
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
                "GetBaseState" => (BASE_STATE, Some("BASE_OK")),
                _ => return Err(Error::unsupported("Unknown DMMR state")),
            };
            flag_names(raw, flags, ok)
        };
        Ok(tuple(vec![
            json!(reply.status),
            json!(format!("0x{raw:04X}")),
            label,
        ]))
    }

    fn presence(&mut self, ctx: &Context) -> Result<NativeReply> {
        let reply = self.read(
            "GetModulePresence",
            vec![],
            vec![json!(false), json!(0), json!(vec![0; 8])],
            ctx,
        )?;
        array(&reply.values[2], 8, "DMMR module presence")?;
        Ok(reply)
    }

    fn scan(&mut self, ctx: &Context, base_semantics: bool) -> Result<Value> {
        if !base_semantics {
            self.active(ctx)?;
        }
        let presence = self.presence(ctx)?;
        if presence.status != 0 {
            if base_semantics {
                return Ok(int_map(Vec::new()));
            }
            ensure(presence.status, "get_module_presence")?;
        }
        let max = output_u64(&presence, 1)?.min(7);
        let flags = array(&presence.values[2], 8, "DMMR module presence")?;
        let addresses: Vec<_> = (0..=max)
            .filter(|n| flags[*n as usize] == json!(1))
            .collect();
        let mut modules = Vec::new();
        for address in addresses {
            let mut info = Map::new();
            for (key, export) in [
                ("fw_version", "GetModuleFwVersion"),
                ("product_no", "GetModuleProductNo"),
                ("hw_type", "GetModuleHwType"),
                ("hw_version", "GetModuleHwVersion"),
                ("state", "GetModuleState"),
            ] {
                let reply = self.read(export, vec![json!(address)], vec![json!(0)], ctx)?;
                if reply.status == 0 {
                    info.insert(key.into(), reply.values[0].clone());
                }
            }
            modules.push((address, Value::Object(info)));
        }
        Ok(int_map(modules))
    }

    fn optional_read(
        &mut self,
        export: &str,
        address: Option<u64>,
        seeds: Vec<Value>,
        ctx: &Context,
    ) -> Result<Option<Vec<Value>>> {
        let key = (export.to_string(), address);
        if self.optional_unsupported.contains(&key) {
            return Ok(None);
        }
        let reply = self.read(
            export,
            address.map(|n| vec![json!(n)]).unwrap_or_default(),
            seeds,
            ctx,
        )?;
        if reply.status == 0 {
            return Ok(Some(reply.values));
        }
        if matches!(reply.status, -10 | -11) {
            self.optional_unsupported.insert(key);
        }
        Ok(None)
    }

    fn module_info(&mut self, address: u64, ctx: &Context) -> Result<Value> {
        let mut info = json!({"address": address});
        for (path, export, seeds) in [
            (
                &["product_id"][..],
                "GetModuleProductID",
                vec![buffer(81, "")],
            ),
            (&["product_no"][..], "GetModuleProductNo", vec![json!(0)]),
            (&["device_type"][..], "GetModuleDevType", vec![json!(0)]),
            (
                &["firmware", "version"][..],
                "GetModuleFwVersion",
                vec![json!(0)],
            ),
            (
                &["firmware", "date"][..],
                "GetModuleFwDate",
                vec![buffer(12, "")],
            ),
            (&["hardware", "type"][..], "GetModuleHwType", vec![json!(0)]),
            (
                &["hardware", "version"][..],
                "GetModuleHwVersion",
                vec![json!(0)],
            ),
            (&["cpu", "load"][..], "GetModuleCPUdata", vec![json!(0.0)]),
            (&["state"][..], "GetModuleState", vec![json!(0)]),
            (
                &["buffer", "empty"][..],
                "GetModuleBufferState",
                vec![json!(false)],
            ),
        ] {
            let reply = self.read(export, vec![json!(address)], seeds, ctx)?;
            if reply.status == 0 {
                if path.len() == 1 {
                    info[path[0]] = reply.values[0].clone();
                } else {
                    info[path[0]][path[1]] = reply.values[0].clone();
                }
            }
        }
        let manuf = self.read(
            "GetModuleManufDate",
            vec![json!(address)],
            vec![json!(0); 2],
            ctx,
        )?;
        if manuf.status == 0 {
            info["manufacturing"] = named_values(&["year", "calendar_week"], &manuf.values);
        }
        let mut uptime = Map::new();
        for (export, keys) in [
            (
                "GetModuleUptimeInt",
                &[
                    "seconds",
                    "milliseconds",
                    "total_seconds",
                    "total_milliseconds",
                ][..],
            ),
            (
                "GetModuleOptimeInt",
                &[
                    "operation_seconds",
                    "operation_milliseconds",
                    "total_operation_seconds",
                    "total_operation_milliseconds",
                ][..],
            ),
        ] {
            if let Some(values) =
                self.optional_read(export, Some(address), vec![json!(0); 4], ctx)?
            {
                for (key, value) in keys.iter().zip(values) {
                    uptime.insert((*key).into(), value);
                }
            }
        }
        if !uptime.is_empty() {
            info["uptime"] = Value::Object(uptime);
        }
        let hk = self.read(
            "GetModuleHousekeeping",
            vec![json!(address)],
            vec![json!(0.0); 16],
            ctx,
        )?;
        if hk.status == 0 {
            info["housekeeping"] = named_values(MODULE_HK, &hk.values);
        }
        let ready = self.read(
            "GetModuleReadyFlags",
            vec![json!(address)],
            vec![json!(0)],
            ctx,
        )?;
        if ready.status == 0 {
            let raw = output_u64(&ready, 0)?;
            info["ready_flags"] = json!({"raw": raw, "measurement_current_ready": raw & 1 != 0, "measurement_housekeeping_ready": raw & 2 != 0, "module_housekeeping_ready": raw & 4 != 0});
        }
        if let Some(values) = self.optional_read(
            "GetModuleMeasRange",
            Some(address),
            vec![json!(0), json!(false)],
            ctx,
        )? {
            info["measurement_range"] = named_values(&["range", "auto_range"], &values);
        }
        let current = self.read(
            "GetModuleCurent",
            vec![json!(address)],
            vec![json!(0.0), json!(0)],
            ctx,
        )?;
        if current.status == 0 {
            info["current"] = named_values(&["value", "range"], &current.values);
        }
        let params = self.read(
            "GetScannedModuleParams",
            vec![json!(address)],
            vec![json!(0); 4],
            ctx,
        )?;
        if params.status == 0 {
            info["scanned_params"] = named_values(
                &[
                    "scanned_product_no",
                    "saved_product_no",
                    "scanned_hw_type",
                    "saved_hw_type",
                ],
                &params.values,
            );
        }
        Ok(info)
    }

    fn required_read(
        &mut self,
        export: &str,
        seeds: Vec<Value>,
        ctx: &Context,
    ) -> Result<Vec<Value>> {
        let reply = self.read(export, vec![], seeds, ctx)?;
        ensure(reply.status, export)?;
        Ok(reply.values)
    }

    fn product_info(&mut self, ctx: &Context) -> Result<Value> {
        self.active(ctx)?;
        let mut info = json!({});
        for (path, export, seed) in [
            (&["product_no"][..], "GetProductNo", json!(0)),
            (&["product_id"][..], "GetProductID", buffer(81, "")),
            (&["device_type"][..], "GetDevType", json!(0)),
            (&["firmware", "version"][..], "GetFwVersion", json!(0)),
            (&["firmware", "date"][..], "GetFwDate", buffer(12, "")),
            (&["hardware", "type"][..], "GetHwType", json!(0)),
            (&["hardware", "version"][..], "GetHwVersion", json!(0)),
        ] {
            let values = self.required_read(export, vec![seed], ctx)?;
            if path.len() == 1 {
                info[path[0]] = values[0].clone();
            } else {
                info[path[0]][path[1]] = values[0].clone();
            }
        }
        info["manufacturing"] = named_values(
            &["year", "calendar_week"],
            &self.required_read("GetManufDate", vec![json!(0); 2], ctx)?,
        );
        let base_no = self.required_read("GetBaseProductNo", vec![json!(0)], ctx)?;
        let base_hw = self.required_read("GetBaseHwType", vec![json!(0)], ctx)?;
        let base_version = self.required_read("GetBaseHwVersion", vec![json!(0)], ctx)?;
        let base_manuf = self.required_read("GetBaseManufDate", vec![json!(0); 2], ctx)?;
        info["base"] = json!({"product_no": base_no[0], "hardware": {"type": base_hw[0], "version": base_version[0]}, "manufacturing": named_values(&["year", "calendar_week"], &base_manuf)});
        Ok(info)
    }

    fn housekeeping(&mut self, ctx: &Context) -> Result<Value> {
        self.active(ctx)?;
        let mut info = json!({});
        for (field, export) in [
            ("main_state", "GetState"),
            ("device_state", "GetDeviceState"),
            ("voltage_state", "GetVoltageState"),
            ("temperature_state", "GetTemperatureState"),
        ] {
            let value = self.state(export, ctx)?;
            let parts = tuple_values(&value)?;
            ensure(
                parts[0]
                    .as_i64()
                    .ok_or_else(|| Error::runtime("Malformed state status"))?,
                export,
            )?;
            info[field] = if field == "main_state" {
                json!({"hex": parts[1], "name": parts[2]})
            } else {
                json!({"hex": parts[1], "flags": parts[2]})
            };
        }
        info["device_enabled"] =
            self.required_read("GetEnable", vec![json!(false)], ctx)?[0].clone();
        info["automatic_current"] =
            self.required_read("GetAutomaticCurent", vec![json!(false)], ctx)?[0].clone();
        info["housekeeping"] = named_values(
            &["volt_12v_v", "volt_5v0_v", "volt_3v3_v", "temp_cpu_c"],
            &self.required_read("GetHousekeeping", vec![json!(0.0); 4], ctx)?,
        );
        info["cpu"] = named_values(
            &["load", "frequency_hz"],
            &self.required_read("GetCPUdata", vec![json!(0.0); 2], ctx)?,
        );
        let uptime = self.required_read("GetUptimeInt", vec![json!(0); 4], ctx)?;
        let operation = self.required_read("GetOptimeInt", vec![json!(0); 4], ctx)?;
        info["uptime"] = json!({"seconds": uptime[0], "milliseconds": uptime[1], "operation_seconds": operation[0], "operation_milliseconds": operation[1], "total_uptime_seconds": uptime[2], "total_uptime_milliseconds": uptime[3], "total_operation_seconds": operation[2], "total_operation_milliseconds": operation[3]});
        let base = self.state("GetBaseState", ctx)?;
        let base_parts = tuple_values(&base)?;
        ensure(base_parts[0].as_i64().unwrap_or(-100), "GetBaseState")?;
        let base_temp = self.required_read("GetBaseTemp", vec![json!(0.0)], ctx)?;
        let fan = self.required_read("GetBaseFanPWM", vec![json!(0); 2], ctx)?;
        let fan_state = fan[1]
            .as_u64()
            .ok_or_else(|| Error::runtime("Malformed fan state"))?;
        let rpm = self.required_read("GetBaseFanRPM", vec![json!(0.0)], ctx)?;
        let leds = self.required_read("GetBaseLEDData", vec![json!(false); 3], ctx)?;
        info["base"] = json!({"state": {"hex": base_parts[1], "flags": base_parts[2]}, "temperature_c": base_temp[0], "fan": {"pwm": fan[0], "rpm": rpm[0], "state": {"hex": format!("0x{fan_state:04X}"), "flags": flag_names(fan_state, FAN_STATE, None)}}, "led": named_values(&["red", "green", "blue"], &leds)});
        let presence = self.presence(ctx)?;
        ensure(presence.status, "GetModulePresence")?;
        let mismatch = self.required_read("GetScannedModuleState", vec![json!(false)], ctx)?;
        let flags = array(&presence.values[2], 8, "DMMR presence")?;
        let addresses: Vec<u64> = (0..8).filter(|n| flags[*n as usize] == json!(1)).collect();
        info["module_presence"] = json!({"valid": presence.values[0], "max_module": presence.values[1], "present": addresses, "raw": presence.values[2]});
        info["scanned_module_state"] = json!({"module_mismatch": mismatch[0]});
        let mut modules = Vec::new();
        for address in addresses {
            modules.push((address, self.module_info(address, ctx)?));
        }
        info["modules"] = int_map(modules);
        self.housekeeping = info.clone();
        Ok(info)
    }

    fn monitor_housekeeping(&mut self, ctx: &Context) -> Result<()> {
        self.active(ctx)?;
        // Python's periodic monitor is a lightweight batch, not the deep per-module snapshot.
        let mut values = Map::new();
        for method in [
            "get_product_no",
            "get_state",
            "get_device_state",
            "get_housekeeping",
            "get_voltage_state",
            "get_temperature_state",
            "get_base_state",
            "get_base_temp",
            "get_base_fan_pwm",
            "get_base_fan_rpm",
            "get_base_led_data",
            "get_cpu_data",
            "get_module_presence",
        ] {
            values.insert(
                method.into(),
                self.low_level(method, &[], &Value::Null, ctx)?
                    .ok_or_else(|| Error::unsupported(method))?,
            );
        }
        self.housekeeping = Value::Object(values);
        Ok(())
    }
    fn due_housekeeping(&mut self, ctx: &Context) -> Result<()> {
        if self.hk_running && self.connected && Instant::now() >= self.next_hk {
            self.next_hk = Instant::now() + duration(self.hk_interval_s)?;
            self.monitor_housekeeping(ctx)?;
        }
        Ok(())
    }

    fn set_auto_range(&mut self, address: u64, automatic: bool, ctx: &Context) -> Result<i64> {
        let status = self.write(
            "SetModuleAutoRange",
            &[json!(address), json!(automatic)],
            ctx,
        )?;
        if !matches!(status, -10 | -11) {
            return Ok(status);
        }
        // Malformed ACKs are verified, never replayed, including during manual range setup.
        for attempt in 0..3 {
            let reply = self.read(
                "GetModuleMeasRange",
                vec![json!(address)],
                vec![json!(0), json!(false)],
                ctx,
            )?;
            if reply.status == 0 {
                return Ok(if output_bool(&reply, 1)? == automatic {
                    0
                } else {
                    status
                });
            }
            if !matches!(reply.status, -10 | -11) {
                return Ok(status);
            }
            if attempt < 2 {
                ctx.sleep(Duration::from_millis(50))?;
            }
        }
        Ok(status)
    }

    fn config_list(&mut self, ctx: &Context) -> Result<Value> {
        let key = ("GetConfigList".into(), None);
        if !self.optional_unsupported.contains(&key) {
            let reply = self.read(
                "GetConfigList",
                vec![],
                vec![json!(vec![false; 500]); 2],
                ctx,
            )?;
            array(&reply.values[0], 500, "config active flags")?;
            array(&reply.values[1], 500, "config valid flags")?;
            if reply.status == 0 {
                return Ok(reply_tuple(reply));
            }
            if !matches!(reply.status, -10 | -11) {
                ensure(reply.status, "get_config_list")?;
            }
            self.optional_unsupported.insert(key);
        }
        let mut active = Vec::new();
        let mut valid = Vec::new();
        for number in 0..500 {
            let reply = self.read(
                "GetConfigFlags",
                vec![json!(number)],
                vec![json!(false); 2],
                ctx,
            )?;
            ensure(reply.status, "get_config_flags")?;
            active.push(json!(output_bool(&reply, 0)?));
            valid.push(json!(output_bool(&reply, 1)?));
        }
        Ok(tuple(vec![json!(0), json!(active), json!(valid)]))
    }

    fn shutdown(
        &mut self,
        disable_device: bool,
        disable_auto: bool,
        ctx: &Context,
    ) -> Result<bool> {
        if self.poisoned {
            return Err(Error::runtime(
                "DMMR DLL blocked; hardware stop is unconfirmed",
            ));
        }
        if !self.connected {
            return Ok(false);
        }
        self.hk_running = false;
        if disable_device && disable_auto {
            self.disable_acquisition(ctx)?;
            if !self.disconnect(ctx)? {
                return Err(Error::runtime(
                    "DMMR shutdown incomplete: disconnect failed",
                ));
            }
            return Ok(true);
        }
        let mut gates = Vec::new();
        if disable_auto {
            gates.push((
                "automatic current",
                "SetAutomaticCurent",
                "GetAutomaticCurent",
            ));
        }
        if disable_device {
            gates.push(("measurement enable", "SetEnable", "GetEnable"));
        }
        let mut errors = Vec::new();
        for (label, setter, _) in &gates {
            match self.write(setter, &[json!(false)], ctx) {
                Ok(0) => (),
                Ok(status) => errors.push(format!("disable {label}: status {status}")),
                Err(error) => errors.push(format!("disable {label}: {error}")),
            }
        }
        for (label, _, getter) in &gates {
            match self.read(getter, vec![], vec![json!(false)], ctx) {
                Ok(reply) if reply.status == 0 && reply.values[0] == json!(false) => (),
                Ok(reply) => errors.push(format!(
                    "verify {label}: status {}, enabled={}",
                    reply.status, reply.values[0]
                )),
                Err(error) => errors.push(format!("verify {label}: {error}")),
            }
        }
        if !errors.is_empty() {
            return Err(Error::runtime(format!(
                "DMMR shutdown incomplete: {}",
                errors.join("; ")
            )));
        }
        if !self.disconnect(ctx)? {
            return Err(Error::runtime(
                "DMMR shutdown incomplete: disconnect failed",
            ));
        }
        Ok(true)
    }

    fn disable_acquisition(&mut self, ctx: &Context) -> Result<bool> {
        self.active(ctx)?;
        let mut errors = Vec::new();
        let mut receive_status = None;
        let mut fatal = false;
        let mut confirmed = 0;
        for (export, write) in [
            ("SetAutomaticCurent", true),
            ("SetEnable", true),
            ("GetAutomaticCurent", false),
            ("GetEnable", false),
        ] {
            let result = if write {
                self.native(export, &[json!(false)], 0, ctx)
            } else {
                self.read(export, vec![], vec![json!(false)], ctx)
            };
            match result {
                Ok(reply) if reply.status == 0 => {
                    if !write {
                        if reply.values[0] == json!(false) {
                            confirmed += 1;
                        } else {
                            errors.push(format!("{export}: OFF not confirmed"));
                            fatal = true;
                        }
                    }
                }
                Ok(reply) => {
                    errors.push(format!("{export}: status {}", reply.status));
                    if receive_error(reply.status) {
                        receive_status = Some(reply.status);
                    } else {
                        fatal = true;
                    }
                }
                Err(error) => {
                    errors.push(format!("{export}: {error}"));
                    fatal = true;
                }
            }
            if self.poisoned || ctx.is_cancelled() || ctx.remaining().is_zero() {
                break;
            }
        }
        if let Some(status) = receive_status.filter(|_| !fatal) {
            self.command_recovery = Some(CommandRecovery {
                phase: "shutdown".into(),
                used: false,
                last: Value::Null,
            });
            self.recover_command(
                status,
                "disable acquisition",
                &json!({"enabled": false, "automatic": false}),
                confirmed != 2,
                ctx,
            )?;
            errors.clear();
        }
        if !errors.is_empty() {
            return Err(Error::runtime(format!(
                "DMMR shutdown incomplete: {}",
                errors.join("; ")
            )));
        }
        if confirmed != 2 && receive_status.is_none() {
            return Err(Error::runtime(
                "DMMR shutdown incomplete: both OFF gates not verified",
            ));
        }
        Ok(true)
    }

    fn recovery_read(
        &mut self,
        export: &str,
        inputs: Vec<Value>,
        seeds: Vec<Value>,
        incident: &mut Value,
        ctx: &Context,
    ) -> Result<Vec<Value>> {
        self.active(ctx)?;
        let reply = self.read(export, inputs.clone(), seeds, ctx)?;
        self.active(ctx)?;
        incident["operations"]
            .as_array_mut()
            .ok_or_else(|| Error::runtime("Missing recovery operation journal"))?
            .push(json!({"action": export, "arguments": inputs, "status": reply.status}));
        recovery_ensure(reply.status, export)?;
        Ok(reply.values)
    }
    fn recovery_write(
        &mut self,
        export: &str,
        inputs: Vec<Value>,
        incident: &mut Value,
        ctx: &Context,
    ) -> Result<()> {
        self.recovery_read(export, inputs, vec![], incident, ctx)?;
        Ok(())
    }
    fn resynchronize(&mut self, incident: &mut Value, ctx: &Context) -> Result<()> {
        incident["purge_attempted"] = json!(true);
        incident["data_loss"] = json!("purge attempted; pending serial data may be lost");
        self.recovery_write("Purge", vec![], incident, ctx)?;
        incident["purged"] = json!(true);
        incident["data_loss"] = json!("pending serial data discarded; lost sample count unknown");
        let baud = self.recovery_read(
            "SetBaudRate",
            vec![],
            vec![json!(self.baudrate)],
            incident,
            ctx,
        )?;
        if baud[0] != json!(self.baudrate) {
            return Err(Error::runtime("Baud rate not restored after purge"));
        }
        Ok(())
    }

    fn verify_running(
        &mut self,
        automatic: bool,
        ranges: &BTreeMap<u64, (u64, bool)>,
        incident: &mut Value,
        ctx: &Context,
    ) -> Result<Value> {
        let state = self.recovery_read("GetState", vec![], vec![json!(0)], incident, ctx)?;
        if state[0] != json!(0) {
            return Err(Error::runtime("Controller is not ST_ON"));
        }
        let enabled = self.recovery_read("GetEnable", vec![], vec![json!(false)], incident, ctx)?;
        if enabled[0] != json!(true) {
            return Err(Error::runtime(
                "Measurement enable not confirmed; no automatic re-enable",
            ));
        }
        let mode = self.recovery_read(
            "GetAutomaticCurent",
            vec![],
            vec![json!(false)],
            incident,
            ctx,
        )?;
        if mode[0] != json!(automatic) {
            return Err(Error::runtime(
                "Current acquisition mode changed; recovery stopped",
            ));
        }
        for (&address, &(range, auto)) in ranges {
            let values = self.recovery_read(
                "GetModuleMeasRange",
                vec![json!(address)],
                vec![json!(0), json!(false)],
                incident,
                ctx,
            )?;
            validate_range(&values, Some(auto), if auto { None } else { Some(range) })?;
        }
        Ok(tuple(vec![json!(0), json!(0), json!("ST_ON")]))
    }

    fn recover_read(&mut self, status: i64, action: &str, ctx: &Context) -> Result<Value> {
        if !receive_error(status) {
            return Err(Error::runtime(format!(
                "{action}: non-recoverable status {status}"
            )));
        }
        self.active(ctx)?;
        let mut recovery = self
            .read_recovery
            .take()
            .ok_or_else(|| Error::runtime("Read recovery requires verified module ranges"))?;
        let now = Instant::now();
        while recovery
            .recent
            .front()
            .is_some_and(|t| now.duration_since(*t) >= Duration::from_secs(60))
        {
            recovery.recent.pop_front();
        }
        let mut incident = json!({"kind": "read_recovery", "action": action, "status": status, "verified": false,
            "resumed": false, "purged": false, "purge_attempted": false, "discarded_frames": 0, "operations": [],
            "data_loss": if recovery.automatic { "failed read excluded; no reconstruction" } else { "failed polling cycle excluded; no reconstruction" }});
        let outcome = (|| {
            if recovery.failed || !recovery.awaiting.is_empty() || recovery.recent.len() >= 3 {
                return Err(Error::runtime(
                    "Persistent/repeated receive errors; recovery budget exhausted",
                ));
            }
            recovery.recent.push_back(now);
            recovery.count += 1;
            match self.verify_running(recovery.automatic, &recovery.ranges, &mut incident, ctx) {
                Ok(_) => (),
                Err(error) if error.kind == "ReceiveError" => {
                    self.resynchronize(&mut incident, ctx)?;
                    self.recovery_write(
                        "SetAutomaticCurent",
                        vec![json!(false)],
                        &mut incident,
                        ctx,
                    )?;
                    self.verify_running(false, &recovery.ranges, &mut incident, ctx)?;
                    let mut emptied = false;
                    for count in 0..1024 {
                        self.active(ctx)?;
                        let frame = self.read(
                            "GetCurent",
                            vec![],
                            vec![json!(0), json!(0.0), json!(0), json!(0.0)],
                            ctx,
                        )?;
                        self.active(ctx)?;
                        if frame.status == 1 {
                            emptied = true;
                            break;
                        }
                        ensure(frame.status, "FIFO flush")?;
                        incident["discarded_frames"] = json!(count + 1);
                    }
                    if !emptied {
                        return Err(Error::runtime("FIFO did not empty after automatic OFF"));
                    }
                    if recovery.automatic {
                        self.recovery_write(
                            "SetAutomaticCurent",
                            vec![json!(true)],
                            &mut incident,
                            ctx,
                        )?;
                        let mode = self.recovery_read(
                            "GetAutomaticCurent",
                            vec![],
                            vec![json!(false)],
                            &mut incident,
                            ctx,
                        )?;
                        if mode[0] != json!(true) {
                            return Err(Error::runtime("Automatic current restart not confirmed"));
                        }
                    }
                    self.timestamps.clear();
                }
                Err(error) => return Err(error),
            }
            self.active(ctx)?;
            incident["verified"] = json!(true);
            recovery.awaiting = recovery.ranges.keys().copied().collect();
            Ok(incident.clone())
        })();
        if let Err(error) = &outcome {
            recovery.failed = true;
            incident["error"] = json!(error.to_string());
        }
        recovery.last = incident.clone();
        self.read_recovery = Some(recovery);
        self.record_event(incident);
        outcome
    }

    fn note_sample(&mut self, address: u64) {
        let mut resumed = None;
        if let Some(recovery) = &mut self.read_recovery
            && recovery.awaiting.remove(&address)
            && recovery.awaiting.is_empty()
        {
            recovery.last["resumed"] = json!(true);
            resumed = Some(
                json!({"kind": "read_resumed", "action": recovery.last["action"], "status": recovery.last["status"], "modules": recovery.ranges.keys().copied().collect::<Vec<_>>() }),
            );
        }
        if let Some(event) = resumed {
            self.record_event(event);
        }
    }

    fn configure_recovery(
        &mut self,
        ranges: &Value,
        automatic: bool,
        ctx: &Context,
    ) -> Result<Value> {
        let ranges = parse_ranges(ranges)?;
        if ranges.is_empty() {
            return Err(Error::argument("Recovery requires verified module ranges"));
        }
        self.active(ctx)?;
        self.read_recovery = Some(ReadRecovery {
            ranges,
            automatic,
            recent: VecDeque::new(),
            awaiting: BTreeSet::new(),
            count: 0,
            failed: false,
            last: Value::Null,
        });
        Ok(self.recovery_state())
    }
    fn recovery_state(&self) -> Value {
        match &self.read_recovery {
            Some(r) => {
                json!({"ranges": int_map(r.ranges.iter().map(|(a, (range, automatic))| (*a, tuple(vec![json!(range), json!(automatic)]))).collect()),
                "automatic": r.automatic, "awaiting_samples": r.awaiting.iter().copied().collect::<Vec<_>>(), "recovery_count": r.count, "failed": r.failed, "last_incident": r.last})
            }
            None => Value::Null,
        }
    }

    fn recover_command(
        &mut self,
        status: i64,
        action: &str,
        expected: &Value,
        purge_first: bool,
        ctx: &Context,
    ) -> Result<Value> {
        if !receive_error(status) {
            return Err(Error::runtime(format!(
                "{action}: non-recoverable status {status}"
            )));
        }
        self.active(ctx)?;
        let mut recovery = self
            .command_recovery
            .take()
            .ok_or_else(|| Error::runtime("Begin a command recovery phase first"))?;
        let mut incident = json!({"kind": "command_recovery", "phase": recovery.phase, "action": action, "status": status, "verified": false, "purged": false, "purge_attempted": false, "operations": []});
        let outcome = (|| {
            if recovery.used {
                return Err(Error::runtime("Command recovery budget exhausted"));
            }
            recovery.used = true;
            if purge_first {
                if recovery.phase != "shutdown" {
                    return Err(Error::argument(
                        "Only shutdown may repeat OFF commands after purge",
                    ));
                }
                self.resynchronize(&mut incident, ctx)?;
                self.recovery_write("SetAutomaticCurent", vec![json!(false)], &mut incident, ctx)?;
                self.recovery_write("SetEnable", vec![json!(false)], &mut incident, ctx)?;
                self.verify_command(expected, &mut incident, ctx)?;
            } else {
                match self.verify_command(expected, &mut incident, ctx) {
                    Ok(_) => (),
                    Err(error) if error.kind == "ReceiveError" => {
                        self.resynchronize(&mut incident, ctx)?;
                        self.verify_command(expected, &mut incident, ctx)?;
                    }
                    Err(error) => return Err(error),
                }
            }
            self.active(ctx)?;
            incident["verified"] = json!(true);
            Ok(incident.clone())
        })();
        if let Err(error) = &outcome {
            incident["error"] = json!(error.to_string());
        }
        recovery.last = incident.clone();
        self.command_recovery = Some(recovery);
        self.record_event(incident);
        outcome
    }

    fn verify_command(
        &mut self,
        expected: &Value,
        incident: &mut Value,
        ctx: &Context,
    ) -> Result<()> {
        let object = expected
            .as_object()
            .ok_or_else(|| Error::argument("expected command state must be an object"))?;
        if object.is_empty() {
            return Err(Error::argument("Expected hardware state is required"));
        }
        for key in object.keys() {
            if !matches!(
                key.as_str(),
                "running" | "enabled" | "automatic" | "ranges" | "module_range"
            ) {
                return Err(Error::argument(format!(
                    "Unsupported verification field: {key}"
                )));
            }
        }
        if expected.get("running").is_some() {
            if !boolean(&expected["running"], "running")? {
                return Err(Error::argument(
                    "running=false is not an OFF confirmation; use enabled/automatic gates",
                ));
            }
            let ranges = if let Some(ranges) = expected.get("ranges") {
                parse_ranges(ranges)?
            } else {
                self.verified_ranges.clone()
            };
            incident["result"] = self.verify_running(false, &ranges, incident, ctx)?;
        }
        for (field, export) in [
            ("enabled", "GetEnable"),
            ("automatic", "GetAutomaticCurent"),
        ] {
            if let Some(value) = expected.get(field) {
                let requested = boolean(value, field)?;
                let actual =
                    self.recovery_read(export, vec![], vec![json!(false)], incident, ctx)?;
                if actual[0] != json!(requested) {
                    return Err(Error::runtime(format!(
                        "{field} readback not confirmed; write will not be replayed"
                    )));
                }
            }
        }
        if expected.get("running").is_none()
            && let Some(ranges) = expected.get("ranges")
        {
            for (address, (range, auto)) in parse_ranges(ranges)? {
                let values = self.recovery_read(
                    "GetModuleMeasRange",
                    vec![json!(address)],
                    vec![json!(0), json!(false)],
                    incident,
                    ctx,
                )?;
                validate_range(&values, Some(auto), if auto { None } else { Some(range) })?;
            }
        }
        if let Some(module) = expected.get("module_range") {
            let address = integer(&module["address"], "address", 0, 7)?;
            let automatic = boolean(&module["auto_range"], "auto_range")?;
            let fixed = module
                .get("range")
                .map(|v| integer(v, "range", 0, 4))
                .transpose()?;
            let values = self.recovery_read(
                "GetModuleMeasRange",
                vec![json!(address)],
                vec![json!(0), json!(false)],
                incident,
                ctx,
            )?;
            validate_range(&values, Some(automatic), fixed)?;
        }
        Ok(())
    }

    fn configure_ranges(&mut self, modes: &Value, ctx: &Context) -> Result<Value> {
        self.active(ctx)?;
        let modes = integer_pairs(modes)?;
        let mut requested = Vec::new();
        for (address, mode) in modes {
            if address >= 8 {
                return Err(Error::argument("Module address must be 0..7"));
            }
            let (auto, fixed) = match mode.as_str() {
                Some("Auto") => (true, None),
                Some(s) if matches!(s, "0" | "1" | "2" | "3" | "4") => {
                    (false, s.parse::<u64>().ok())
                }
                _ => return Err(Error::argument("Range mode must be Auto or 0..4")),
            };
            requested.push((address, auto, fixed));
        }
        self.read_recovery = None;
        self.verified_ranges.clear();
        if !self
            .command_recovery
            .as_ref()
            .is_some_and(|r| r.phase == "startup")
        {
            self.command_recovery = Some(CommandRecovery {
                phase: "startup".into(),
                used: false,
                last: Value::Null,
            });
        }
        for (address, auto, fixed) in requested {
            let mut result = self.read(
                "GetModuleMeasRange",
                vec![json!(address)],
                vec![json!(0), json!(false)],
                ctx,
            )?;
            if result.status != 0 {
                self.recover_command(
                    result.status,
                    "get_module_meas_range",
                    &json!({"running": true}),
                    false,
                    ctx,
                )?;
                result = self.read(
                    "GetModuleMeasRange",
                    vec![json!(address)],
                    vec![json!(0), json!(false)],
                    ctx,
                )?;
            }
            ensure(result.status, "get_module_meas_range")?;
            let (_, actual_auto) = validate_range(&result.values, None, None)?;
            if actual_auto != auto {
                let status = self.set_auto_range(address, auto, ctx)?;
                if status != 0 {
                    self.recover_command(status, "set_module_auto_range", &json!({"running": true, "module_range": {"address": address, "auto_range": auto}}), false, ctx)?;
                }
                result = self.read(
                    "GetModuleMeasRange",
                    vec![json!(address)],
                    vec![json!(0), json!(false)],
                    ctx,
                )?;
                ensure(result.status, "verify_set_module_auto_range")?;
                validate_range(&result.values, Some(auto), None)?;
            }
            if let Some(fixed) = fixed
                && output_u64(&result, 0)? != fixed
            {
                let status =
                    self.write("SetModuleMeasRange", &[json!(address), json!(fixed)], ctx)?;
                if status != 0 {
                    let mut ranges = self.verified_ranges.clone();
                    ranges.insert(address, (fixed, auto));
                    self.recover_command(
                        status,
                        "set_module_meas_range",
                        &json!({"running": true, "ranges": ranges_value(&ranges)}),
                        false,
                        ctx,
                    )?;
                }
                result = self.read(
                    "GetModuleMeasRange",
                    vec![json!(address)],
                    vec![json!(0), json!(false)],
                    ctx,
                )?;
                ensure(result.status, "verify_set_module_meas_range")?;
            }
            let observed = validate_range(&result.values, Some(auto), fixed)?;
            self.verified_ranges.insert(address, observed);
        }
        if !self.verified_ranges.is_empty() {
            self.read_recovery = Some(ReadRecovery {
                ranges: self.verified_ranges.clone(),
                automatic: false,
                recent: VecDeque::new(),
                awaiting: BTreeSet::new(),
                count: 0,
                failed: false,
                last: Value::Null,
            });
        }
        Ok(ranges_value(&self.verified_ranges))
    }

    fn poll_currents(&mut self, modules: &[u64], ctx: &Context) -> Result<Value> {
        self.active(ctx)?;
        let mut samples = BTreeMap::new();
        let mut ranges = BTreeMap::new();
        let mut valid = BTreeMap::new();
        let mut recovered = Value::Null;
        for address in modules {
            samples.insert(*address, float(f64::NAN));
            ranges.insert(*address, float(f64::NAN));
            valid.insert(*address, json!(false));
        }
        self.sample_snapshot = json!({"values": int_map(samples.clone().into_iter().collect()), "ranges": int_map(ranges.clone().into_iter().collect()), "valid": int_map(valid.clone().into_iter().collect()), "token": Value::Null});
        for &address in modules {
            let reply = self.read(
                "GetModuleCurent",
                vec![json!(address)],
                vec![json!(0.0), json!(0)],
                ctx,
            )?;
            if reply.status != 0 {
                if receive_error(reply.status) && self.read_recovery.is_some() {
                    recovered = self.recover_read(reply.status, "get_module_current", ctx)?;
                    break;
                }
                self.record_event(
                    json!({"kind": "missing_sample", "address": address, "status": reply.status}),
                );
                continue;
            }
            let sample = number(&reply.values[0], "current").unwrap_or(f64::NAN);
            let range = reply.values[1].as_u64().filter(|r| *r < 5);
            let range_confirmed = self
                .read_recovery
                .as_ref()
                .and_then(|r| {
                    if r.awaiting.is_empty() {
                        None
                    } else {
                        r.ranges.get(&address)
                    }
                })
                .is_none_or(|(expected, auto)| *auto || Some(*expected) == range);
            if sample.is_finite()
                && range_confirmed
                && let Some(range) = range
            {
                samples.insert(address, float(sample));
                ranges.insert(address, json!(range));
                valid.insert(address, json!(true));
                self.note_sample(address);
            } else {
                self.record_event(json!({"kind": "invalid_sample", "address": address, "current": reply.values[0], "range": reply.values[1]}));
            }
        }
        if !recovered.is_null() {
            for address in modules {
                samples.insert(*address, float(f64::NAN));
                ranges.insert(*address, float(f64::NAN));
                valid.insert(*address, json!(false));
            }
        }
        if recovered.is_null()
            && self
                .read_recovery
                .as_ref()
                .is_some_and(|r| !r.awaiting.is_empty())
        {
            if let Some(recovery) = &mut self.read_recovery {
                recovery.failed = true;
            }
            return Err(Error::runtime(
                "No new valid reply after recovery for all tracked modules; acquisition must stop",
            ));
        }
        self.sample_sequence = self
            .sample_sequence
            .checked_add(1)
            .ok_or_else(|| Error::runtime("Sample token exhausted"))?;
        self.sample_snapshot = json!({"values": int_map(samples.into_iter().collect()), "ranges": int_map(ranges.into_iter().collect()), "valid": int_map(valid.into_iter().collect()), "token": self.sample_sequence, "recovery": recovered, "recovery_state": self.recovery_state()});
        Ok(self.sample_snapshot.clone())
    }

    fn valid_frame(&mut self, ctx: &Context) -> Result<Value> {
        self.active(ctx)?;
        let frame = self.read(
            "GetCurent",
            vec![],
            vec![json!(0), json!(0.0), json!(0), json!(0.0)],
            ctx,
        )?;
        let mut result = json!({"status": frame.status, "valid": false, "new": false, "frame": reply_tuple(frame.clone())});
        if frame.status != 0 {
            return Ok(result);
        }
        let address = frame.values[0].as_u64().filter(|n| *n < 8);
        let sample = number(&frame.values[1], "current").unwrap_or(f64::NAN);
        let range = frame.values[2].as_u64().filter(|n| *n < 5);
        let timestamp = number(&frame.values[3], "timestamp").unwrap_or(f64::NAN);
        if let (Some(address), Some(range)) = (address, range) {
            let fresh = timestamp.is_finite()
                && timestamp >= 0.0
                && self
                    .timestamps
                    .get(&address)
                    .is_none_or(|previous| timestamp > *previous);
            let range_ok = self
                .read_recovery
                .as_ref()
                .and_then(|r| {
                    if r.awaiting.is_empty() {
                        None
                    } else {
                        r.ranges.get(&address)
                    }
                })
                .is_none_or(|(expected, auto)| *auto || *expected == range);
            if fresh && sample.is_finite() && range_ok {
                self.timestamps.insert(address, timestamp);
                self.note_sample(address);
                result["valid"] = json!(true);
                result["new"] = json!(true);
            }
        }
        Ok(result)
    }

    fn configure_protocol(&mut self, directory: Option<&str>) {
        let root = directory
            .map(PathBuf::from)
            .or_else(|| self.log_dir.clone());
        let path = root
            .as_ref()
            .map(|root| root.join(format!("dmmr_protocol_com{}.jsonl", self.com)));
        if self.protocol_path.is_some() && self.protocol_path != path {
            self.events.clear();
            self.event_count = 0;
        }
        self.protocol_path = path;
        self.protocol_error = match root {
            Some(root) => fs::create_dir_all(root)
                .err()
                .map(|error| error.to_string()),
            None => Some("No Explorer log directory configured; events retained in memory".into()),
        };
        if self.protocol_error.is_none()
            && let Some(path) = &self.protocol_path
            && let Err(error) = OpenOptions::new().create(true).append(true).open(path)
        {
            self.protocol_error = Some(error.to_string());
        }
    }

    fn record_event(&mut self, mut event: Value) {
        if self.protocol_path.is_none() && self.protocol_error.is_none() {
            self.configure_protocol(None);
        }
        if !event.is_object() {
            event = json!({"event": event});
        }
        self.event_count = self.event_count.saturating_add(1);
        event["sequence"] = json!(self.event_count);
        event["unix_time_s"] = json!(
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap_or_default()
                .as_secs_f64()
        );
        self.events.push_back(event.clone());
        while self.events.len() > 128 {
            self.events.pop_front();
        }
        if let Some(path) = &self.protocol_path {
            let outcome = (|| -> std::io::Result<()> {
                if fs::metadata(path).is_ok_and(|m| m.len() >= 1_000_000) {
                    let last = PathBuf::from(format!("{}.3", path.display()));
                    if last.exists() {
                        fs::remove_file(last)?;
                    }
                    for index in (1..3).rev() {
                        let old = PathBuf::from(format!("{}.{index}", path.display()));
                        if old.exists() {
                            fs::rename(
                                old,
                                PathBuf::from(format!("{}.{}", path.display(), index + 1)),
                            )?;
                        }
                    }
                    fs::rename(path, PathBuf::from(format!("{}.1", path.display())))?;
                }
                let mut file = OpenOptions::new().create(true).append(true).open(path)?;
                let text = serde_json::to_string(&event).map_err(std::io::Error::other)?;
                writeln!(file, "{text}")
            })();
            if let Err(error) = outcome {
                self.protocol_error = Some(error.to_string());
            }
        }
    }

    fn begin_startup(&mut self, ctx: &Context) -> Value {
        let identity = format!(
            "COM{}; baud={}; DLL={}; SHA-256={}",
            self.com, self.baudrate, self.dll_path, self.dll_sha256
        );
        let outcome = (|| -> Result<PathBuf> {
            ctx.check_cancelled()?;
            if self.poisoned {
                return Err(Error::runtime(
                    "DLL blocked; native capture cannot be opened",
                ));
            }
            if self.startup_path.is_some() {
                return Err(Error::runtime("a native startup capture is already active"));
            }
            // This optional ABI is separately verified in one DLL, not declared by the SDK header.
            if self.dll_sha256 != DEBUG_SHA256 {
                return Err(Error::runtime(
                    "unverified DLL: native debug ABI is not known",
                ));
            }
            let root = self
                .log_dir
                .as_ref()
                .ok_or_else(|| Error::runtime("Explorer log directory is not configured"))?;
            fs::create_dir_all(root).map_err(|e| Error::runtime(e.to_string()))?;
            let path = root.join(format!("dmmr_startup_com{}.log", self.com));
            let path = if path.is_absolute() {
                path
            } else {
                std::env::current_dir()
                    .map_err(|e| Error::runtime(e.to_string()))?
                    .join(path)
            };
            let text = path
                .to_str()
                .ok_or_else(|| Error::runtime("Debug path is not representable as text"))?;
            // fopen uses ANSI on Windows. Do not misaddress a non-ASCII path through UTF-8 IPC.
            if !text.is_ascii() || text.contains('\0') || text.len() > 4095 {
                return Err(Error::runtime(
                    "Native debug capture requires a bounded ASCII path",
                ));
            }
            self.startup_path = Some(path.clone());
            let status = self.write("OpenDebugFile", &[json!(text)], ctx)?;
            if status != 0 {
                self.startup_path = None;
                ensure(status, "OpenDebugFile")?;
            }
            Ok(path)
        })();
        json!(match outcome {
            Ok(path) => format!(
                "[DMMR startup] {identity}\n[DMMR startup] Native capture opened: {}",
                path.display()
            ),
            Err(error) => format!(
                "[DMMR startup] {identity}\n[DMMR startup] Native capture unavailable: {error}"
            ),
        })
    }

    fn end_startup(&mut self, ctx: &Context) -> Value {
        let Some(path) = self.startup_path.clone() else {
            return json!("");
        };
        let mut notes = Vec::new();
        let mut closed = false;
        if self.poisoned {
            notes.push(format!(
                "Native capture not closed: DLL still active; restart worker. Partial file: {}",
                path.display()
            ));
        } else {
            match self.write("CloseDebugFile", &[], ctx) {
                Ok(status) => {
                    closed = true;
                    self.startup_path = None;
                    if status != 0 {
                        notes.push(format!(
                            "CloseDebugFile returned {status}; capture may be incomplete."
                        ));
                    }
                }
                Err(error) => notes.push(format!(
                    "Native capture not closed: {error}. File: {}",
                    path.display()
                )),
            }
        }
        let outcome = (|| -> std::io::Result<Vec<u8>> {
            let mut raw = Vec::new();
            File::open(&path)?.take(65537).read_to_end(&mut raw)?;
            Ok(raw)
        })();
        match outcome {
            Ok(mut raw) => {
                if raw.len() > 65536 {
                    raw.truncate(65536);
                    notes.push("Capture truncated to the first 65536 bytes.".into());
                    if closed
                        && let Err(error) = OpenOptions::new()
                            .write(true)
                            .open(&path)
                            .and_then(|f| f.set_len(65536))
                    {
                        notes.push(format!("Could not trim native file: {error}"));
                    }
                }
                let mut any = false;
                for line in raw
                    .split(|byte| *byte == b'\n')
                    .filter(|line| !line.is_empty())
                {
                    let line = line.strip_suffix(b"\r").unwrap_or(line);
                    let text: String = line
                        .iter()
                        .map(|byte| {
                            if (32..127).contains(byte) {
                                char::from(*byte).to_string()
                            } else {
                                format!("\\x{byte:02x}")
                            }
                        })
                        .collect();
                    notes.push(text);
                    any = true;
                }
                if !any {
                    notes.push("Native capture is empty.".into());
                }
            }
            Err(error) => notes.push(format!("Native capture unavailable: {error}")),
        }
        json!(
            notes
                .into_iter()
                .map(|note| format!("[DMMR native] {note}"))
                .collect::<Vec<_>>()
                .join("\n")
        )
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
            "get_device_type" => ("GetDevType", &[], vec![json!(0)]),
            "get_hw_type" => ("GetHwType", &[], vec![json!(0)]),
            "get_hw_version" => ("GetHwVersion", &[], vec![json!(0)]),
            "get_manuf_date" => ("GetManufDate", &[], vec![json!(0); 2]),
            "get_uptime_int" => ("GetUptimeInt", &[], vec![json!(0); 4]),
            "get_optime_int" => ("GetOptimeInt", &[], vec![json!(0); 4]),
            "get_uptime" => ("GetUptime", &[], vec![json!(0.0); 2]),
            "get_optime" => ("GetOptime", &[], vec![json!(0.0); 2]),
            "get_cpu_data" => ("GetCPUdata", &[], vec![json!(0.0); 2]),
            "get_housekeeping" => ("GetHousekeeping", &[], vec![json!(0.0); 4]),
            "get_buffer_state" => ("GetBufferState", &[], vec![json!(false)]),
            "device_purge" => ("DevicePurge", &[], vec![json!(false)]),
            "get_auto_mask" => ("GetAutoMask", &[], vec![json!(0)]),
            "check_auto_input" => ("CheckAutoInput", &[], vec![json!(0)]),
            "get_enable" => ("GetEnable", &[], vec![json!(false)]),
            "get_automatic_current" => ("GetAutomaticCurent", &[], vec![json!(false)]),
            "get_current" => (
                "GetCurent",
                &[],
                vec![json!(0), json!(0.0), json!(0), json!(0.0)],
            ),
            "get_base_product_no" => ("GetBaseProductNo", &[], vec![json!(0)]),
            "get_base_manuf_date" => ("GetBaseManufDate", &[], vec![json!(0); 2]),
            "get_base_hw_type" => ("GetBaseHwType", &[], vec![json!(0)]),
            "get_base_hw_version" => ("GetBaseHwVersion", &[], vec![json!(0)]),
            "get_base_temp" => ("GetBaseTemp", &[], vec![json!(0.0)]),
            "get_base_fan_rpm" => ("GetBaseFanRPM", &[], vec![json!(0.0)]),
            "get_base_led_data" => ("GetBaseLEDData", &[], vec![json!(false); 3]),
            "get_scanned_module_state" => ("GetScannedModuleState", &[], vec![json!(false)]),
            "get_scanned_module_params" => {
                ("GetScannedModuleParams", &["address"], vec![json!(0); 4])
            }
            "get_module_fw_version" => ("GetModuleFwVersion", &["address"], vec![json!(0)]),
            "get_module_fw_date" => ("GetModuleFwDate", &["address"], vec![buffer(12, "")]),
            "get_module_product_id" => ("GetModuleProductID", &["address"], vec![buffer(81, "")]),
            "get_module_product_no" => ("GetModuleProductNo", &["address"], vec![json!(0)]),
            "get_module_manuf_date" => ("GetModuleManufDate", &["address"], vec![json!(0); 2]),
            "get_module_device_type" => ("GetModuleDevType", &["address"], vec![json!(0)]),
            "get_module_hw_type" => ("GetModuleHwType", &["address"], vec![json!(0)]),
            "get_module_hw_version" => ("GetModuleHwVersion", &["address"], vec![json!(0)]),
            "get_module_uptime_int" => ("GetModuleUptimeInt", &["address"], vec![json!(0); 4]),
            "get_module_optime_int" => ("GetModuleOptimeInt", &["address"], vec![json!(0); 4]),
            "get_module_uptime" => ("GetModuleUptime", &["address"], vec![json!(0.0); 2]),
            "get_module_optime" => ("GetModuleOptime", &["address"], vec![json!(0.0); 2]),
            "get_module_cpu_data" => ("GetModuleCPUdata", &["address"], vec![json!(0.0)]),
            "get_module_housekeeping" => {
                ("GetModuleHousekeeping", &["address"], vec![json!(0.0); 16])
            }
            "get_module_state" => ("GetModuleState", &["address"], vec![json!(0)]),
            "get_module_buffer_state" => ("GetModuleBufferState", &["address"], vec![json!(false)]),
            "module_purge" => ("ModulePurge", &["address"], vec![json!(false)]),
            "get_module_ready_flags" => ("GetModuleReadyFlags", &["address"], vec![json!(0)]),
            "get_module_meas_range" => (
                "GetModuleMeasRange",
                &["address"],
                vec![json!(0), json!(false)],
            ),
            "get_current_config" => ("GetCurrentConfig", &[], vec![json!(vec![0; 93])]),
            "get_config_data" => (
                "GetConfigData",
                &["config_number"],
                vec![json!(vec![0; 93])],
            ),
            "get_config_name" => ("GetConfigName", &["config_number"], vec![buffer(137, "")]),
            "get_config_flags" => ("GetConfigFlags", &["config_number"], vec![json!(false); 2]),
            "get_io_state" => ("GetIOState", &[], vec![json!(0)]),
            "get_comm_error" => ("GetCommError", &[], vec![json!(0)]),
            "purge" => ("Purge", &[], vec![]),
            "restart" => ("Restart", &[], vec![]),
            "restart_base" => ("RestartBase", &[], vec![]),
            "update_module_presence" => ("UpdateModulePresence", &[], vec![]),
            "rescan_modules" => ("RescanModules", &[], vec![]),
            "set_scanned_module_state" => ("SetScannedModuleState", &[], vec![]),
            "rescan_module" => ("RescanModule", &["address"], vec![]),
            "restart_module" => ("RestartModule", &["address"], vec![]),
            "set_enable" => ("SetEnable", &["enable"], vec![]),
            "set_automatic_current" => ("SetAutomaticCurent", &["automatic_current"], vec![]),
            "set_module_meas_range" => ("SetModuleMeasRange", &["address", "meas_range"], vec![]),
            "save_current_config" => ("SaveCurrentConfig", &["config_number"], vec![]),
            "load_current_config" => ("LoadCurrentConfig", &["config_number"], vec![]),
            "set_config_flags" => (
                "SetConfigFlags",
                &["config_number", "active", "valid"],
                vec![],
            ),
            "get_state"
            | "get_device_state"
            | "get_voltage_state"
            | "get_temperature_state"
            | "get_base_state" => {
                Params::new(args, kwargs, &["timeout_s"], &[])?;
                let export = match method {
                    "get_state" => "GetState",
                    "get_device_state" => "GetDeviceState",
                    "get_voltage_state" => "GetVoltageState",
                    "get_temperature_state" => "GetTemperatureState",
                    _ => "GetBaseState",
                };
                return Ok(Some(self.state(export, ctx)?));
            }
            "get_base_fan_pwm" => {
                Params::new(args, kwargs, &[], &["timeout_s"])?;
                let reply = self.read("GetBaseFanPWM", vec![], vec![json!(0); 2], ctx)?;
                let state = output_u64(&reply, 1)?;
                return Ok(Some(tuple(vec![
                    json!(reply.status),
                    reply.values[0].clone(),
                    json!(format!("0x{state:04X}")),
                    flag_names(state, FAN_STATE, None),
                ])));
            }
            "get_module_presence" => {
                Params::new(args, kwargs, &[], &["timeout_s"])?;
                return Ok(Some(reply_tuple(self.presence(ctx)?)));
            }
            "get_sw_version" | "get_interface_state" => {
                Params::new(args, kwargs, &[], &[])?;
                return Ok(Some(json!(
                    self.native(
                        if method == "get_sw_version" {
                            "GetSWVersion"
                        } else {
                            "GetInterfaceState"
                        },
                        &[],
                        0,
                        ctx
                    )?
                    .status
                )));
            }
            "get_error_message"
            | "get_io_error_message"
            | "get_io_state_message"
            | "get_comm_error_message" => {
                let names: &[&str] = match method {
                    "get_io_state_message" => &["io_state"],
                    "get_comm_error_message" => &["comm_error"],
                    _ => &[],
                };
                let p = Params::new(args, kwargs, names, &[])?;
                let inputs = match method {
                    "get_io_state_message" => {
                        vec![json!(signed_integer(p.required("io_state")?, "io_state")?)]
                    }
                    "get_comm_error_message" => vec![json!(integer(
                        p.required("comm_error")?,
                        "comm_error",
                        0,
                        u32::MAX as u64
                    )?)],
                    _ => vec![],
                };
                let export = match method {
                    "get_error_message" => "GetErrorMessage",
                    "get_io_error_message" => "GetIOErrorMessage",
                    "get_io_state_message" => "GetIOStateMessage",
                    _ => "GetCommErrorMessage",
                };
                let reply = self.native_raw(export, &inputs, 1, ctx)?;
                return Ok(Some(json!(
                    reply.values[0]
                        .as_str()
                        .ok_or_else(|| Error::runtime("Malformed C string return"))?
                )));
            }
            "set_baud_rate" => {
                let p = Params::new(args, kwargs, &["baud_rate"], &["timeout_s"])?;
                let baud = integer(p.required("baud_rate")?, "baud_rate", 1, u32::MAX as u64)?;
                return Ok(Some(reply_tuple(self.read(
                    "SetBaudRate",
                    vec![],
                    vec![json!(baud)],
                    ctx,
                )?)));
            }
            "set_module_auto_range" => {
                let p = Params::new(args, kwargs, &["address", "auto_range", "timeout_s"], &[])?;
                return Ok(Some(json!(self.set_auto_range(
                    integer(p.required("address")?, "address", 0, 7)?,
                    boolean(p.required("auto_range")?, "auto_range")?,
                    ctx
                )?)));
            }
            "set_current_config" | "set_config_data" => {
                let names: &[&str] = if method == "set_current_config" {
                    &["config_data"]
                } else {
                    &["config_number", "config_data"]
                };
                let p = Params::new(args, kwargs, names, &["timeout_s"])?;
                let registers = config_data(p.required("config_data")?)?;
                let inputs = if method == "set_config_data" {
                    vec![json!(integer(
                        p.required("config_number")?,
                        "config_number",
                        0,
                        499
                    )?)]
                } else {
                    vec![]
                };
                // const DWORD* is an input pointer and is still included in NativeReply.values.
                let reply = self.read(
                    if method == "set_current_config" {
                        "SetCurrentConfig"
                    } else {
                        "SetConfigData"
                    },
                    inputs,
                    vec![json!(registers)],
                    ctx,
                )?;
                array(&reply.values[0], 93, "config register output")?;
                return Ok(Some(json!(reply.status)));
            }
            "set_config_name" => {
                let p = Params::new(args, kwargs, &["config_number", "name"], &["timeout_s"])?;
                let slot = integer(p.required("config_number")?, "config_number", 0, 499)?;
                let name = p
                    .required("name")?
                    .as_str()
                    .ok_or_else(|| Error::new("TypeError", "name must be a string"))?;
                if name.len() >= 137 || name.contains('\0') {
                    return Err(Error::argument(
                        "name must be NUL-free and shorter than 137 UTF-8 bytes",
                    ));
                }
                let reply = self.read(
                    "SetConfigName",
                    vec![json!(slot)],
                    vec![buffer(137, name)],
                    ctx,
                )?;
                return Ok(Some(json!(reply.status)));
            }
            _ => return Ok(None),
        };
        let mut positional = names.to_vec();
        if matches!(
            method,
            "set_enable"
                | "get_enable"
                | "restart"
                | "get_scanned_module_state"
                | "rescan_modules"
                | "set_scanned_module_state"
                | "set_automatic_current"
                | "get_automatic_current"
                | "get_current"
                | "set_module_meas_range"
                | "get_module_meas_range"
                | "get_module_ready_flags"
        ) {
            positional.push("timeout_s");
        }
        let p = Params::new(args, kwargs, &positional, &["timeout_s"])?;
        let mut inputs = Vec::new();
        for name in names {
            let value = p.required(name)?;
            inputs.push(match *name {
                "address" => json!(integer(value, name, 0, 7)?),
                "config_number" => json!(integer(value, name, 0, 499)?),
                "meas_range" => json!(integer(value, name, 0, 4)?),
                _ => json!(boolean(value, name)?),
            });
        }
        if seeds.is_empty() {
            Ok(Some(json!(self.write(export, &inputs, ctx)?)))
        } else {
            let reply = self.read(export, inputs, seeds, ctx)?;
            if matches!(method, "get_current_config" | "get_config_data") {
                array(&reply.values[0], 93, "configuration registers")?;
            }
            Ok(Some(reply_tuple(reply)))
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
        if !matches!(
            method,
            "shutdown"
                | "disconnect"
                | "stop_housekeeping"
                | "hk_monitor"
                | "do_housekeeping_cycle"
                | "end_startup_diagnostics"
        ) {
            self.due_housekeeping(ctx)?;
        }
        if let Some(value) = self.low_level(method, args, kwargs, ctx)? {
            return Ok(value);
        }
        match method {
            "connect" | "disconnect" => {
                let p = Params::new(
                    args,
                    kwargs,
                    if method == "connect" {
                        &["timeout_s"]
                    } else {
                        &[]
                    },
                    &[],
                )?;
                p.optional_positive("timeout_s", 5.0)?;
                Ok(json!(if method == "connect" {
                    self.connect(ctx)?
                } else {
                    self.disconnect(ctx)?
                }))
            }
            "initialize" => {
                let p = Params::new(args, kwargs, &["timeout_s"], &["persist_scan"])?;
                p.optional_positive("timeout_s", 5.0)?;
                self.initialize(
                    p.get("persist_scan")
                        .map(|v| boolean(v, "persist_scan"))
                        .transpose()?
                        .unwrap_or(true),
                    ctx,
                )
            }
            "scan_modules" | "scan_all_modules" => {
                Params::new(args, kwargs, &["timeout_s"], &[])?;
                self.scan(ctx, method == "scan_all_modules")
            }
            "get_product_info" => {
                Params::new(args, kwargs, &["timeout_s"], &[])?;
                self.product_info(ctx)
            }
            "collect_housekeeping" => {
                Params::new(args, kwargs, &["timeout_s"], &[])?;
                self.housekeeping(ctx)
            }
            "get_module_info" | "get_module_current" => {
                let p = Params::new(args, kwargs, &["address", "timeout_s"], &[])?;
                let explicit = p.get("address").filter(|v| !v.is_null());
                let addresses: Vec<_> = if let Some(address) = explicit {
                    vec![integer(address, "address", 0, 7)?]
                } else {
                    integer_pairs(&self.scan(ctx, false)?)?
                        .into_iter()
                        .map(|(address, _)| address)
                        .collect()
                };
                let mut values = Vec::new();
                for address in addresses {
                    let value = if method == "get_module_info" {
                        self.module_info(address, ctx)?
                    } else {
                        let reply = self.read(
                            "GetModuleCurent",
                            vec![json!(address)],
                            vec![json!(0.0), json!(0)],
                            ctx,
                        )?;
                        if explicit.is_some() {
                            reply_tuple(reply)
                        } else {
                            json!({"status": reply.status, "current": reply.values[0], "meas_range": reply.values[1]})
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
            "get_config_list" | "list_configs" => {
                let p = Params::new(
                    args,
                    kwargs,
                    if method == "list_configs" {
                        &["include_empty", "timeout_s"]
                    } else {
                        &["timeout_s"]
                    },
                    &[],
                )?;
                let flags = self.config_list(ctx)?;
                if method == "get_config_list" {
                    return Ok(flags);
                }
                let include = p
                    .get("include_empty")
                    .map(|v| boolean(v, "include_empty"))
                    .transpose()?
                    .unwrap_or(false);
                let parts = tuple_values(&flags)?;
                let active = array(&parts[1], 500, "config active flags")?;
                let valid = array(&parts[2], 500, "config valid flags")?;
                let mut configs = Vec::new();
                for index in 0..500 {
                    if !include && active[index] == json!(false) && valid[index] == json!(false) {
                        continue;
                    }
                    let name = self.read(
                        "GetConfigName",
                        vec![json!(index)],
                        vec![buffer(137, "")],
                        ctx,
                    )?;
                    ensure(name.status, "get_config_name")?;
                    configs.push(json!({"index": index, "name": name.values[0], "active": active[index], "valid": valid[index]}));
                }
                Ok(json!(configs))
            }
            "shutdown" => {
                let p = Params::new(
                    args,
                    kwargs,
                    &[],
                    &["disable_device", "disable_automatic_current", "timeout_s"],
                )?;
                self.shutdown(
                    p.get("disable_device")
                        .map(|v| boolean(v, "disable_device"))
                        .transpose()?
                        .unwrap_or(true),
                    p.get("disable_automatic_current")
                        .map(|v| boolean(v, "disable_automatic_current"))
                        .transpose()?
                        .unwrap_or(true),
                    ctx,
                )
                .map(|v| json!(v))
            }
            "disable_acquisition" => {
                Params::new(args, kwargs, &[], &["timeout_s"])?;
                self.disable_acquisition(ctx).map(|v| json!(v))
            }
            "get_status" => {
                Params::new(args, kwargs, &[], &[])?;
                Ok(
                    json!({"device_id": self.device_id, "com": self.com, "baudrate": self.baudrate, "connected": self.connected,
                    "transport_poisoned": self.poisoned, "hk_running": self.hk_running, "hk_interval_s": self.hk_interval_s, "external_thread": false, "external_lock": false}),
                )
            }
            "start_housekeeping" => {
                let p = Params::new(args, kwargs, &["interval_s"], &[])?;
                let interval = p.optional_positive("interval_s", self.hk_interval_s)?;
                if !self.connected {
                    return Ok(json!(false));
                }
                if !self.hk_running {
                    self.hk_running = true;
                    self.hk_interval_s = interval;
                    self.next_hk = Instant::now();
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
                self.monitor_housekeeping(ctx)?;
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
            "configure_read_recovery" => {
                let p = Params::new(args, kwargs, &["ranges"], &["automatic", "timeout_s"])?;
                self.configure_recovery(
                    p.required("ranges")?,
                    p.get("automatic")
                        .map(|v| boolean(v, "automatic"))
                        .transpose()?
                        .unwrap_or(false),
                    ctx,
                )
            }
            "recover_read" => {
                let p = Params::new(args, kwargs, &["status", "action"], &["timeout_s"])?;
                let status = p
                    .required("status")?
                    .as_i64()
                    .ok_or_else(|| Error::argument("status must be an integer"))?;
                let action = p
                    .required("action")?
                    .as_str()
                    .ok_or_else(|| Error::argument("action must be text"))?;
                self.recover_read(status, action, ctx)
            }
            "note_sample" => {
                let p = Params::new(args, kwargs, &["address"], &[])?;
                let address = integer(p.required("address")?, "address", 0, 7)?;
                self.active(ctx)?;
                self.note_sample(address);
                Ok(Value::Null)
            }
            "begin_command_recovery" => {
                let p = Params::new(args, kwargs, &["phase"], &["timeout_s"])?;
                let phase = p
                    .required("phase")?
                    .as_str()
                    .filter(|s| matches!(*s, "startup" | "shutdown"))
                    .ok_or_else(|| Error::argument("phase must be startup or shutdown"))?;
                self.command_recovery = Some(CommandRecovery {
                    phase: phase.into(),
                    used: false,
                    last: Value::Null,
                });
                Ok(Value::Null)
            }
            "recover_command" => {
                let p = Params::new(
                    args,
                    kwargs,
                    &["status", "action", "expected"],
                    &["purge_first", "timeout_s"],
                )?;
                let status = p
                    .required("status")?
                    .as_i64()
                    .ok_or_else(|| Error::argument("status must be an integer"))?;
                let action = p
                    .required("action")?
                    .as_str()
                    .ok_or_else(|| Error::argument("action must be text"))?;
                self.recover_command(
                    status,
                    action,
                    p.required("expected")?,
                    p.get("purge_first")
                        .map(|v| boolean(v, "purge_first"))
                        .transpose()?
                        .unwrap_or(false),
                    ctx,
                )
            }
            "verify_running" => {
                Params::new(args, kwargs, &[], &["timeout_s"])?;
                self.verify_running(
                    false,
                    &self.verified_ranges.clone(),
                    &mut json!({"operations": []}),
                    ctx,
                )
            }
            "configure_module_ranges" => {
                let p = Params::new(args, kwargs, &["modes"], &["timeout_s"])?;
                self.configure_ranges(p.required("modes")?, ctx)
            }
            "poll_currents" => {
                let p = Params::new(args, kwargs, &["modules"], &["timeout_s"])?;
                let modules = module_list(p.required("modules")?)?;
                self.poll_currents(&modules, ctx)
            }
            "get_valid_current" => {
                Params::new(args, kwargs, &["timeout_s"], &[])?;
                self.valid_frame(ctx)
            }
            "configure_protocol_log" => {
                let p = Params::new(args, kwargs, &["directory"], &[])?;
                let directory = p
                    .get("directory")
                    .filter(|v| !v.is_null())
                    .map(|v| {
                        v.as_str()
                            .ok_or_else(|| Error::argument("directory must be text"))
                    })
                    .transpose()?;
                self.configure_protocol(directory);
                Ok(Value::Null)
            }
            "record_protocol_event" => {
                let p = Params::new(args, kwargs, &["event"], &[])?;
                let event = p.required("event")?;
                if !event.is_object() {
                    return Err(Error::argument("event must be an object"));
                }
                self.record_event(event.clone());
                Ok(Value::Null)
            }
            "protocol_diagnostics" => {
                Params::new(args, kwargs, &[], &[])?;
                Ok(self.protocol_diagnostics())
            }
            "begin_startup_diagnostics" => {
                Params::new(args, kwargs, &["timeout_s"], &[])?;
                Ok(self.begin_startup(ctx))
            }
            "end_startup_diagnostics" => {
                Params::new(args, kwargs, &["timeout_s"], &[])?;
                Ok(self.end_startup(ctx))
            }
            "new_read_recovery" | "new_command_recovery" => Err(Error::unsupported(
                "Python recovery objects/callbacks cannot cross native IPC; adapter must use configure_read_recovery or begin_command_recovery and explicit recover methods",
            )),
            _ => Err(Error::new(
                "AttributeError",
                format!("Unknown DMMR method: {method}"),
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
            "_failed_open_released" => json!(self.failed_open_released),
            "_failed_open_cleanup_outcome" => self.failed_open_cleanup_outcome.clone(),
            "_opening_in_progress" => json!(false),
            "_transport_poisoned" => json!(self.poisoned),
            "_transport_error" => json!(self.transport_error),
            "hk_running" => json!(self.hk_running),
            "hk_interval_s" => json!(self.hk_interval_s),
            "external_thread" | "external_lock" => json!(false),
            "housekeeping_snapshot" => self.housekeeping.clone(),
            "measurement_snapshot" => self.sample_snapshot.clone(),
            "read_recovery" => self.recovery_state(),
            "command_recovery" => match &self.command_recovery {
                Some(r) => json!({"phase": r.phase, "used": r.used, "last_incident": r.last}),
                None => Value::Null,
            },
            "_startup_log_path" => json!(
                self.startup_path
                    .as_ref()
                    .map(|p| p.to_string_lossy().into_owned())
            ),
            "verified_ranges" => ranges_value(&self.verified_ranges),
            "err_dict" => self.errors.clone(),
            "dmmr_dll_path" => json!(self.dll_path),
            "MODULE_NUM" => json!(8),
            "MEAS_RANGE_NUM" => json!(5),
            "ADDR_BROADCAST" => json!(255),
            "DEVICE_TYPE" => json!(0xAC38),
            "MODULE_TYPE" => json!(0xC41E),
            "MODULE_NOT_FOUND" => json!(0),
            "MODULE_PRESENT" => json!(1),
            "MODULE_INVALID" => json!(2),
            "MEAS_CUR_RDY" => json!(1),
            "HK_MEAS_DATA_RDY" => json!(2),
            "HK_MOD_DATA_RDY" => json!(4),
            "MAX_REG" => json!(93),
            "MAX_CONFIG" => json!(500),
            "CONFIG_NAME_SIZE" => json!(137),
            "DATA_STRING_SIZE" => json!(12),
            "PRODUCT_ID_SIZE" => json!(81),
            "FAN_PWM_MAX" => json!(319),
            "MAIN_STATE" => state_map(MAIN_STATE),
            "DEVICE_STATE" => state_map(DEVICE_STATE),
            "VOLTAGE_STATE" => state_map(VOLTAGE_STATE),
            "TEMPERATURE_STATE" => state_map(TEMPERATURE_STATE),
            "BASE_STATE" => state_map(BASE_STATE),
            "FAN_STATE" => state_map(FAN_STATE),
            _ => {
                if let Some(code) = error_constant(name) {
                    json!(code)
                } else {
                    return Err(Error::new(
                        "AttributeError",
                        format!("Unknown DMMR attribute: {name}"),
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
                self.device_id = value
                    .as_str()
                    .filter(|s| !s.trim().is_empty())
                    .ok_or_else(|| Error::argument("device_id must be a non-empty string"))?
                    .into();
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
                    format!("Read-only or unknown DMMR attribute: {name}"),
                ));
            }
        }
        Ok(())
    }
}

impl Controller {
    fn protocol_diagnostics(&self) -> Value {
        json!({"event_count": self.event_count, "file": self.protocol_path.as_ref().map(|p| p.to_string_lossy().into_owned()), "log_error": self.protocol_error, "events": self.events})
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
                    .position(|n| *n == key)
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
fn signed_integer(value: &Value, name: &str) -> Result<i64> {
    value
        .as_i64()
        .filter(|n| *n >= i32::MIN as i64 && *n <= i32::MAX as i64)
        .ok_or_else(|| Error::argument(format!("{name} must be a signed 32-bit integer")))
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
fn tuple_values(value: &Value) -> Result<&[Value]> {
    value
        .get("$tuple")
        .and_then(Value::as_array)
        .map(Vec::as_slice)
        .ok_or_else(|| Error::runtime("Malformed facade tuple"))
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
fn receive_error(status: i64) -> bool {
    matches!(status, -13..=-10)
}
fn recovery_ensure(status: i64, operation: &str) -> Result<()> {
    if receive_error(status) {
        Err(Error::new(
            "ReceiveError",
            format!("{operation}: receive status {status}"),
        ))
    } else {
        ensure(status, operation)
    }
}
fn int_map(pairs: Vec<(u64, Value)>) -> Value {
    json!({"$map": pairs.into_iter().map(|(k, v)| json!([k, v])).collect::<Vec<_>>()})
}
fn state_map(flags: &[(u64, &str)]) -> Value {
    int_map(flags.iter().map(|(n, s)| (*n, json!(s))).collect())
}
fn ranges_value(ranges: &BTreeMap<u64, (u64, bool)>) -> Value {
    int_map(
        ranges
            .iter()
            .map(|(a, (r, auto))| (*a, tuple(vec![json!(r), json!(auto)])))
            .collect(),
    )
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
fn integer_pairs(value: &Value) -> Result<Vec<(u64, Value)>> {
    let mut values = BTreeMap::new();
    if let Some(pairs) = value.get("$map").and_then(Value::as_array) {
        for pair in pairs {
            let pair = array(pair, 2, "integer map entry")?;
            let key = integer(&pair[0], "map key", 0, u32::MAX as u64)?;
            if values.insert(key, pair[1].clone()).is_some() {
                return Err(Error::argument("Duplicate map key"));
            }
        }
    } else if let Some(map) = value.as_object() {
        for (key, value) in map {
            let key = key
                .parse::<u64>()
                .map_err(|_| Error::argument("Map key must be an integer"))?;
            if values.insert(key, value.clone()).is_some() {
                return Err(Error::argument("Duplicate map key"));
            }
        }
    } else {
        return Err(Error::new("TypeError", "Expected integer-keyed map"));
    }
    Ok(values.into_iter().collect())
}
fn parse_ranges(value: &Value) -> Result<BTreeMap<u64, (u64, bool)>> {
    let mut result = BTreeMap::new();
    for (address, value) in integer_pairs(value)? {
        if address >= 8 {
            return Err(Error::argument("Recovery module must be 0..7"));
        }
        let pair = if value.get("$tuple").is_some() {
            tuple_values(&value)?
        } else {
            array(&value, 2, "range state")?
        };
        if pair.len() != 2 {
            return Err(Error::argument("Range state requires range and auto flag"));
        }
        result.insert(
            address,
            (
                integer(&pair[0], "meas_range", 0, 4)?,
                boolean(&pair[1], "auto_range")?,
            ),
        );
    }
    Ok(result)
}
fn validate_range(values: &[Value], auto: Option<bool>, fixed: Option<u64>) -> Result<(u64, bool)> {
    if values.len() != 2 {
        return Err(Error::runtime("Malformed measurement range readback"));
    }
    let range = values[0]
        .as_u64()
        .filter(|r| *r < 5)
        .ok_or_else(|| Error::runtime("Measurement range readback is not 0..4"))?;
    let automatic = values[1]
        .as_bool()
        .ok_or_else(|| Error::runtime("Measurement auto-range readback is not a bool"))?;
    if auto.is_some_and(|auto| auto != automatic) || fixed.is_some_and(|fixed| range != fixed) {
        return Err(Error::runtime("Measurement range not confirmed"));
    }
    Ok((range, automatic))
}
fn module_list(value: &Value) -> Result<Vec<u64>> {
    let values = value
        .as_array()
        .ok_or_else(|| Error::new("TypeError", "modules must be an array"))?;
    let mut result = BTreeSet::new();
    for value in values {
        if !result.insert(integer(value, "address", 0, 7)?) {
            return Err(Error::argument("Duplicate module"));
        }
    }
    Ok(result.into_iter().collect())
}
fn config_data(value: &Value) -> Result<Vec<Value>> {
    let data = value
        .as_array()
        .ok_or_else(|| Error::new("TypeError", "config_data must be an array of integers"))?;
    if data.len() != 93 {
        return Err(Error::argument(
            "config_data must contain exactly 93 registers",
        ));
    }
    data.iter()
        .map(|value| integer(value, "register", 0, u32::MAX as u64).map(|n| json!(n)))
        .collect()
}
fn error_constant(name: &str) -> Option<i64> {
    Some(match name {
        "NO_ERR" => 0,
        "NO_DATA" => 1,
        "AUTO_MEAS_CUR" => 2,
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
        "ERR_BUFF_FULL" => -200,
        _ => return None,
    })
}
