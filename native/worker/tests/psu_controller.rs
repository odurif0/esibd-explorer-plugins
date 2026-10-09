#![cfg(feature = "psu")]

use esibd_native_worker::Backend;
use esibd_native_worker::codec::{float, tuple};
use esibd_native_worker::context::Context;
use esibd_native_worker::error::{Error, Result};
use esibd_native_worker::ffi::{Dll, NativeReply};
use esibd_native_worker::psu::{Controller, PUBLIC_METHODS};
use serde_json::{Value, json};
use std::collections::{HashMap, HashSet, VecDeque};
use std::sync::{Arc, Mutex};
use std::time::Duration;

#[derive(Clone, Debug)]
struct Call {
    name: String,
    args: Vec<Value>,
}

struct Hardware {
    calls: Vec<Call>,
    open: bool,
    enabled: bool,
    outputs: [bool; 2],
    ranges: [bool; 2],
    interlocks: [bool; 2],
    voltages: [f64; 2],
    currents: [f64; 2],
    voltage_limits: [f64; 2],
    current_limits: [f64; 2],
    measured: [[f64; 3]; 2],
    main_state: u64,
    device_state: u64,
    extra_psu_state: u64,
    config_active: Vec<bool>,
    config_valid: Vec<bool>,
    config_names: Vec<String>,
    load_outputs: HashMap<usize, [bool; 2]>,
    load_interlocks: Option<[bool; 2]>,
    status: HashMap<String, i64>,
    scripted: HashMap<String, VecDeque<Result<NativeReply>>>,
    ignore: HashSet<String>,
    cancel_on: Option<(String, Context)>,
    cancel_on_voltage: Option<Context>,
    delays: HashMap<String, Duration>,
}

impl Default for Hardware {
    fn default() -> Self {
        Self {
            calls: vec![],
            open: false,
            enabled: false,
            outputs: [false; 2],
            ranges: [false; 2],
            interlocks: [true; 2],
            voltages: [0.0; 2],
            currents: [0.0; 2],
            voltage_limits: [10_000.0; 2],
            current_limits: [0.1; 2],
            measured: [[0.0, 0.0, 2.5]; 2],
            main_state: 0,
            device_state: 0,
            extra_psu_state: 0,
            config_active: vec![false; 168],
            config_valid: vec![false; 168],
            config_names: (0..168).map(|i| format!("slot {i}")).collect(),
            load_outputs: HashMap::new(),
            load_interlocks: None,
            status: HashMap::new(),
            scripted: HashMap::new(),
            ignore: HashSet::new(),
            cancel_on: None,
            cancel_on_voltage: None,
            delays: HashMap::new(),
        }
    }
}

impl Hardware {
    fn names(&self) -> Vec<&str> {
        self.calls.iter().map(|c| c.name.as_str()).collect()
    }
    fn calls_named(&self, name: &str) -> Vec<&Call> {
        self.calls.iter().filter(|c| c.name == name).collect()
    }
    fn script(&mut self, name: &str, reply: NativeReply) {
        self.scripted
            .entry(name.into())
            .or_default()
            .push_back(Ok(reply));
    }
    fn script_error(&mut self, name: &str, error: Error) {
        self.scripted
            .entry(name.into())
            .or_default()
            .push_back(Err(error));
    }
}

fn reply(values: Vec<Value>) -> NativeReply {
    NativeReply { status: 0, values }
}
fn failed(status: i64) -> NativeReply {
    NativeReply {
        status,
        values: vec![],
    }
}

struct MockDll(Arc<Mutex<Hardware>>);

impl Dll for MockDll {
    fn call(&mut self, symbol: &str, args: &[Value]) -> Result<NativeReply> {
        let name = symbol
            .strip_prefix("COM_HVPSU2D_")
            .expect("PSU symbol prefix");
        let mut h = self.0.lock().unwrap();
        assert_eq!(
            args[0],
            json!(2),
            "every export uses its configured WORD port"
        );
        h.calls.push(Call {
            name: name.into(),
            args: args.to_vec(),
        });
        if let Some(values) = h.scripted.get_mut(name)
            && let Some(value) = values.pop_front()
        {
            return value;
        }
        if let Some(status) = h.status.get(name).copied().filter(|s| *s != 0) {
            return Ok(failed(status));
        }
        let ch = || args[1].as_u64().unwrap() as usize;
        let pair = || {
            [
                args[1].as_i64().unwrap() != 0,
                args[2].as_i64().unwrap() != 0,
            ]
        };
        let ignored = h.ignore.contains(name);
        let value = match name {
            "Open" => {
                assert_eq!(args.len(), 2);
                h.open = true;
                reply(vec![])
            }
            "Close" => {
                assert_eq!(args.len(), 1);
                h.open = false;
                reply(vec![])
            }
            "SetBaudRate" => {
                assert_eq!(args.len(), 2);
                reply(vec![args[1].clone()])
            }
            "GetProductID" => {
                assert_eq!(args[1], json!({"capacity":60,"text":""}));
                reply(vec![json!("CGC PSU-CTRL")])
            }
            "GetProductNo" => reply(vec![json!(12345)]),
            "GetFWVersion" => reply(vec![json!(0x102)]),
            "GetFWDate" => {
                assert_eq!(args[1], json!({"capacity":16,"text":""}));
                reply(vec![json!("2021-09-20")])
            }
            "GetHWType" => reply(vec![json!(2)]),
            "GetHWVersion" => reply(vec![json!(3)]),
            "SetDeviceEnable" => {
                if !ignored {
                    h.enabled = args[1].as_i64().unwrap() != 0;
                }
                reply(vec![])
            }
            "GetDeviceEnable" => reply(vec![json!(i32::from(h.enabled))]),
            "SetPSUEnable" => {
                if !ignored {
                    h.outputs = pair();
                }
                reply(vec![])
            }
            "GetPSUEnable" => reply(h.outputs.map(|b| json!(i32::from(b))).to_vec()),
            "SetPSUFullRange" => {
                if !ignored {
                    h.ranges = pair();
                }
                reply(vec![])
            }
            "GetPSUFullRange" => reply(h.ranges.map(|b| json!(i32::from(b))).to_vec()),
            "HasPSUFullRange" => reply(vec![json!(1), json!(1)]),
            "SetInterlockEnable" => {
                if !ignored {
                    h.interlocks = pair();
                }
                reply(vec![])
            }
            "GetInterlockEnable" => reply(h.interlocks.map(|b| json!(i32::from(b))).to_vec()),
            "SetPSUOutputVoltage" => {
                if !ignored {
                    h.voltages[ch()] = args[2].as_f64().unwrap();
                }
                reply(vec![])
            }
            "SetPSUOutputCurrent" => {
                if !ignored {
                    h.currents[ch()] = args[2].as_f64().unwrap();
                }
                reply(vec![])
            }
            "GetPSUOutputVoltage" => reply(vec![float(h.voltages[ch()])]),
            "GetPSUOutputCurrent" => reply(vec![float(h.currents[ch()])]),
            "GetPSUSetOutputVoltage" => {
                reply(vec![float(h.voltages[ch()]), float(h.voltage_limits[ch()])])
            }
            "GetPSUSetOutputCurrent" => {
                reply(vec![float(h.currents[ch()]), float(h.current_limits[ch()])])
            }
            "GetPSUData" => reply(h.measured[ch()].map(float).to_vec()),
            "LoadCurrentConfig" => {
                if let Some(outputs) = h.load_outputs.get(&ch()).copied() {
                    h.outputs = outputs;
                }
                if let Some(interlocks) = h.load_interlocks {
                    h.interlocks = interlocks;
                }
                reply(vec![])
            }
            "SaveCurrentConfig" => {
                h.config_valid[ch()] = true;
                reply(vec![])
            }
            "GetConfigName" => {
                assert_eq!(args[2], json!({"capacity":75,"text":""}));
                reply(vec![json!(h.config_names[ch()])])
            }
            "SetConfigName" => {
                assert_eq!(args[2]["capacity"], json!(75));
                let text = args[2]["text"].as_str().unwrap();
                assert!(text.is_ascii() && text.len() < 75 && !text.contains('\0'));
                h.config_names[ch()] = text.into();
                reply(vec![json!(text)])
            }
            "GetConfigFlags" => {
                assert_eq!(args[2], json!(false));
                assert_eq!(args[3], json!(false));
                reply(vec![
                    json!(h.config_active[ch()]),
                    json!(h.config_valid[ch()]),
                ])
            }
            "SetConfigFlags" => {
                h.config_active[ch()] = args[2].as_bool().expect("C bool config active");
                h.config_valid[ch()] = args[3].as_bool().expect("C bool config valid");
                reply(vec![])
            }
            "GetConfigList" => {
                for value in &args[1..] {
                    assert_eq!(value, &json!(vec![false; 168]));
                }
                reply(vec![json!(h.config_active), json!(h.config_valid)])
            }
            "GetMainState" => reply(vec![json!(h.main_state)]),
            "GetDeviceState" => reply(vec![json!(h.device_state)]),
            "GetPSUState" => {
                let mut state = h.extra_psu_state;
                for ch in 0..2 {
                    if h.outputs[ch] {
                        state |= 1 << (4 + ch);
                    }
                    if h.ranges[ch] {
                        state |= 1 << (13 + ch);
                    }
                }
                reply(vec![json!(state)])
            }
            "GetHousekeeping" => {
                assert_eq!(args.len(), 5);
                reply([24.0, 5.0, 3.3, 40.0].map(float).to_vec())
            }
            "GetSensorData" => {
                assert_eq!(args[1], json!(vec![0.0; 3]));
                reply(vec![json!([30.0, 31.0, 32.0])])
            }
            "GetFanData" => {
                for arg in &args[1..] {
                    assert_eq!(arg, &json!(vec![0; 3]));
                }
                reply(vec![
                    json!([1, 1, 0]),
                    json!([0, 0, 0]),
                    json!([1000, 2000, 3000]),
                    json!([900, 1900, 2900]),
                    json!([100, 200, 300]),
                ])
            }
            "GetLEDData" => reply(vec![json!(0), json!(1), json!(0)]),
            "GetCPUData" => reply(vec![json!(0.15), json!(72_000_000.0)]),
            "GetUptime" => reply(vec![json!(123), json!(456), json!(78)]),
            "GetTotalTime" => reply(vec![json!(90_000), json!(60_000)]),
            "GetADCHousekeeping" => {
                assert_eq!(args.len(), 8);
                reply([5.0, 3.3, 1.8, 1.2, 2.5, 35.0].map(float).to_vec())
            }
            "GetPSUHousekeeping" => {
                assert_eq!(args.len(), 6);
                reply([24.0, 12.0, -12.0, 2.5].map(float).to_vec())
            }
            _ => {
                return Err(Error::unsupported(format!(
                    "Unexpected mock export: {symbol}"
                )));
            }
        };
        if h.cancel_on
            .as_ref()
            .is_some_and(|(symbol, _)| symbol == name)
        {
            let (_, ctx) = h.cancel_on.take().unwrap();
            ctx.cancel();
        }
        if name == "SetPSUOutputVoltage"
            && args[2].as_f64().unwrap() > 0.0
            && let Some(ctx) = h.cancel_on_voltage.take()
        {
            ctx.cancel();
        }
        if let Some(delay) = h.delays.get(name) {
            std::thread::sleep(*delay);
        }
        Ok(value)
    }
}

fn setup() -> (Controller, Arc<Mutex<Hardware>>) {
    let h = Arc::new(Mutex::new(Hardware::default()));
    let controller = Controller::new(
        &json!({"device_id":"PSU_A","com":7,"port":2,"baudrate":230400}),
        Box::new(MockDll(h.clone())),
    )
    .unwrap();
    (controller, h)
}

fn call(c: &mut Controller, name: &str, args: &[Value], kwargs: Value) -> Result<Value> {
    c.call(name, args, &kwargs, &Context::test(10.0))
}

fn connected() -> (Controller, Arc<Mutex<Hardware>>) {
    let (mut c, h) = setup();
    assert_eq!(
        call(&mut c, "connect", &[], json!({})).unwrap(),
        json!(true)
    );
    h.lock().unwrap().calls.clear();
    (c, h)
}

fn manual(voltage: f64) -> Value {
    json!({"output_enabled":{"0":true,"1":false}, "full_range_enabled":{"0":false,"1":false},
        "voltage_values":{"0":voltage,"1":0.0}, "current_limit_values":{"0":0.01,"1":0.0}})
}

fn fast_profile() -> Value {
    json!({"ramp_step_v":100.0,"ramp_step_interval_s":0.000001})
}

#[test]
fn connect_is_idempotent_and_never_enables_outputs() {
    let (mut c, h) = setup();
    assert_eq!(
        call(&mut c, "connect", &[], json!({})).unwrap(),
        json!(true)
    );
    assert_eq!(
        call(&mut c, "connect", &[], json!({})).unwrap(),
        json!(true)
    );
    let h = h.lock().unwrap();
    assert_eq!(h.names(), ["Open", "SetBaudRate", "GetProductID"]);
    assert_eq!(h.calls[0].args, [json!(2), json!(7)]);
    assert!(!h.enabled && h.outputs == [false; 2]);
    assert_eq!(c.get_attribute("_dll_port_claimed").unwrap(), json!(true));
}

#[test]
fn negotiated_baud_is_read_but_status_retains_requested_baud() {
    let (mut c, h) = setup();
    h.lock()
        .unwrap()
        .script("SetBaudRate", reply(vec![json!(115200)]));
    call(&mut c, "connect", &[], json!({})).unwrap();
    assert_eq!(
        call(&mut c, "get_status", &[], json!({})).unwrap(),
        json!({"device_id":"PSU_A","com":7,"port":2,"baudrate":230400,"connected":true,"transport_poisoned":false})
    );
}

#[test]
fn failed_open_is_closed_and_reconnect_requires_confirmed_cleanup() {
    let (mut c, h) = setup();
    h.lock().unwrap().status.insert("Open".into(), -2);
    h.lock().unwrap().status.insert("Close".into(), -3);
    assert!(
        call(&mut c, "connect", &[], json!({}))
            .unwrap_err()
            .message
            .contains("-2")
    );
    assert_eq!(c.get_attribute("connected").unwrap(), json!(false));
    assert_eq!(c.get_attribute("_dll_port_claimed").unwrap(), json!(true));
    assert!(
        call(&mut c, "connect", &[], json!({}))
            .unwrap_err()
            .message
            .contains("cleanup")
    );
    h.lock().unwrap().status.remove("Close");
    assert_eq!(
        call(&mut c, "disconnect", &[], json!({})).unwrap(),
        json!(true)
    );
    assert_eq!(
        c.get_attribute("_failed_open_released").unwrap(),
        json!(true)
    );
    h.lock().unwrap().status.remove("Open");
    call(&mut c, "connect", &[], json!({})).unwrap();
}

#[test]
fn failed_baud_rolls_back_without_automatic_write_retry() {
    let (mut c, h) = setup();
    h.lock().unwrap().status.insert("SetBaudRate".into(), -16);
    let error = call(&mut c, "connect", &[], json!({})).unwrap_err();
    assert!(error.message.contains("-16"));
    assert_eq!(h.lock().unwrap().names(), ["Open", "SetBaudRate", "Close"]);
    assert_eq!(c.get_attribute("connected").unwrap(), json!(false));
    assert_eq!(c.get_attribute("_dll_port_claimed").unwrap(), json!(false));
}

#[test]
fn identity_is_advisory_but_timeout_poison_is_not() {
    for status in [-10, -100] {
        let (mut c, h) = setup();
        h.lock()
            .unwrap()
            .status
            .insert("GetProductID".into(), status);
        call(&mut c, "connect", &[], json!({})).unwrap();
        assert_eq!(c.get_attribute("connected").unwrap(), json!(true));
    }
    let (mut c, h) = setup();
    h.lock()
        .unwrap()
        .script_error("GetProductID", Error::new("TimeoutError", "hung identity"));
    assert!(call(&mut c, "connect", &[], json!({})).is_err());
    assert_eq!(c.get_attribute("_transport_poisoned").unwrap(), json!(true));
    assert_eq!(
        call(&mut c, "disconnect", &[], json!({})).unwrap(),
        json!(false)
    );
    assert!(!h.lock().unwrap().names().contains(&"Close"));
}

#[test]
fn facade_clamps_positive_setpoints_and_allows_zero_when_limits_fail() {
    let (mut c, h) = connected();
    h.lock().unwrap().voltage_limits = [200.0; 2];
    h.lock().unwrap().current_limits = [0.005; 2];
    call(
        &mut c,
        "set_channel_voltage",
        &[json!(1), json!(300)],
        json!({}),
    )
    .unwrap();
    call(
        &mut c,
        "set_channel_current",
        &[json!(0), json!(0.02)],
        json!({}),
    )
    .unwrap();
    assert_eq!(h.lock().unwrap().voltages[1], 200.0);
    assert_eq!(h.lock().unwrap().currents[0], 0.005);
    h.lock()
        .unwrap()
        .status
        .insert("GetPSUSetOutputVoltage".into(), -100);
    assert!(
        call(
            &mut c,
            "set_channel_voltage",
            &[json!(0), json!(30)],
            json!({})
        )
        .unwrap_err()
        .message
        .contains("Cannot verify")
    );
    call(
        &mut c,
        "set_channel_voltage",
        &[json!(0), json!(0)],
        json!({}),
    )
    .unwrap();
    call(
        &mut c,
        "set_channel_voltage",
        &[json!(1), json!(-5)],
        json!({}),
    )
    .unwrap();
    assert_eq!(h.lock().unwrap().voltages, [0.0; 2]);
}

#[test]
fn invalid_hardware_limits_never_authorize_positive_writes() {
    for value in [f64::NAN, f64::INFINITY, -1.0] {
        for method in ["set_channel_voltage", "set_channel_current"] {
            let (mut c, h) = connected();
            h.lock().unwrap().voltage_limits[0] = value;
            h.lock().unwrap().current_limits[0] = value;
            assert!(call(&mut c, method, &[json!(0), json!(10.0)], json!({})).is_err());
            assert!(
                !h.lock()
                    .unwrap()
                    .names()
                    .iter()
                    .any(|n| n.starts_with("SetPSUOutput"))
            );
            call(&mut c, method, &[json!(0), json!(0)], json!({})).unwrap();
        }
    }
}

#[test]
fn all_setpoints_validate_channels_and_finiteness_before_writes() {
    let (mut c, h) = connected();
    for method in ["set_channel_voltage", "set_channel_current"] {
        for value in [
            float(f64::NAN),
            float(f64::INFINITY),
            Value::Null,
            json!("bad"),
        ] {
            assert_eq!(
                call(&mut c, method, &[json!(0), value], json!({}))
                    .unwrap_err()
                    .kind,
                "ValueError"
            );
        }
        for ch in [json!(-1), json!(2), json!(65536)] {
            assert_eq!(
                call(&mut c, method, &[ch, json!(0)], json!({}))
                    .unwrap_err()
                    .kind,
                "ValueError"
            );
        }
    }
    assert!(h.lock().unwrap().calls.is_empty());
}

#[test]
fn measurements_are_not_configured_targets_and_preserve_units_and_tuple() {
    let (mut c, h) = connected();
    {
        let mut h = h.lock().unwrap();
        h.voltages = [400.0, 600.0];
        h.currents = [0.03, 0.04];
        h.measured = [[395.0, 0.00025, 3.0], [f64::NAN, f64::INFINITY, 2.0]];
    }
    assert_eq!(
        call(&mut c, "get_channel_voltage", &[json!(0)], json!({})).unwrap(),
        json!(400.0)
    );
    assert_eq!(
        call(&mut c, "get_channel_current", &[json!(0)], json!({})).unwrap(),
        json!(0.03)
    );
    assert_eq!(
        call(&mut c, "get_channel_measurements", &[json!(0)], json!({})).unwrap(),
        tuple(vec![json!(395.0), json!(0.00025), json!(3.0)])
    );
    assert_eq!(
        call(
            &mut c,
            "get_channel_measured_voltage",
            &[json!(1)],
            json!({})
        )
        .unwrap(),
        float(f64::NAN)
    );
    assert_eq!(
        call(
            &mut c,
            "get_channel_measured_current",
            &[json!(1)],
            json!({})
        )
        .unwrap(),
        float(f64::INFINITY)
    );
}

#[test]
fn masks_and_interlocks_preserve_windows_bool_and_config_flags_use_c_bool() {
    let (mut c, h) = connected();
    call(
        &mut c,
        "set_output_enabled",
        &[json!(true), json!(false)],
        json!({}),
    )
    .unwrap();
    call(
        &mut c,
        "set_output_full_range",
        &[json!(false), json!(true)],
        json!({}),
    )
    .unwrap();
    call(
        &mut c,
        "set_interlock_enabled",
        &[json!(true), json!(true)],
        json!({}),
    )
    .unwrap();
    call(&mut c, "set_device_enabled", &[json!(true)], json!({})).unwrap();
    call(
        &mut c,
        "set_config_flags",
        &[json!(7)],
        json!({"active":false,"valid":true}),
    )
    .unwrap();
    assert_eq!(
        call(&mut c, "get_output_enabled", &[], json!({})).unwrap(),
        tuple(vec![json!(true), json!(false)])
    );
    assert_eq!(
        call(&mut c, "get_output_full_range", &[], json!({})).unwrap(),
        tuple(vec![json!(false), json!(true)])
    );
    assert_eq!(
        call(&mut c, "get_interlock_enabled", &[], json!({})).unwrap(),
        tuple(vec![json!(true), json!(true)])
    );
    let h = h.lock().unwrap();
    assert_eq!(
        h.calls_named("SetPSUEnable")[0].args,
        [json!(2), json!(1), json!(0)]
    );
    assert_eq!(
        h.calls_named("SetConfigFlags")[0].args,
        [json!(2), json!(7), json!(false), json!(true)]
    );
}

#[test]
fn only_compatibility_statuses_allow_output_and_range_state_fallbacks() {
    for status in [-10, -11, -13, -14] {
        let (mut c, h) = connected();
        h.lock()
            .unwrap()
            .status
            .insert("GetPSUEnable".into(), status);
        h.lock()
            .unwrap()
            .status
            .insert("GetPSUFullRange".into(), status);
        h.lock().unwrap().extra_psu_state = (1 << 4) | (1 << 14) | (1 << 20) | (1 << 6);
        assert_eq!(
            call(&mut c, "get_output_enabled", &[], json!({})).unwrap(),
            tuple(vec![json!(true), json!(false)])
        );
        assert_eq!(
            call(&mut c, "get_output_full_range", &[], json!({})).unwrap(),
            tuple(vec![json!(false), json!(true)])
        );
    }
    let (mut c, h) = connected();
    h.lock().unwrap().status.insert("GetPSUEnable".into(), -100);
    assert!(call(&mut c, "get_output_enabled", &[], json!({})).is_err());
    assert!(!h.lock().unwrap().names().contains(&"GetPSUState"));
}

#[test]
fn config_load_save_name_and_flags_preserve_slot_bounds_and_ascii_buffers() {
    let (mut c, h) = connected();
    let long_name = format!("Ren{}{}", '\u{00e9}', "x".repeat(100));
    call(&mut c, "load_config", &[json!(167)], json!({})).unwrap();
    call(
        &mut c,
        "save_config",
        &[json!(167)],
        json!({"name":long_name,"active":false}),
    )
    .unwrap();
    let h = h.lock().unwrap();
    assert_eq!(
        h.names(),
        [
            "LoadCurrentConfig",
            "SaveCurrentConfig",
            "SetConfigName",
            "SetConfigFlags"
        ]
    );
    assert_eq!(h.config_names[167], format!("Ren?{}", "x".repeat(70)));
    assert!(!h.config_active[167] && h.config_valid[167]);
    drop(h);
    for slot in [json!(-1), json!(168), json!(u32::MAX)] {
        assert!(call(&mut c, "load_config", &[slot], json!({})).is_err());
    }
}

#[test]
fn save_partial_failure_is_reported_and_is_not_replayed() {
    let (mut c, h) = connected();
    h.lock().unwrap().status.insert("SetConfigName".into(), -11);
    assert!(
        call(
            &mut c,
            "save_config",
            &[json!(4)],
            json!({"name":"test","active":true,"valid":true})
        )
        .is_err()
    );
    assert_eq!(
        h.lock().unwrap().names(),
        ["SaveCurrentConfig", "SetConfigName"]
    );
    assert!(h.lock().unwrap().config_valid[4]);
}

#[test]
fn config_list_filters_empty_slots_and_falls_back_to_all_168_flags() {
    for fallback in [false, true] {
        let (mut c, h) = connected();
        {
            let mut h = h.lock().unwrap();
            h.config_active[7] = true;
            h.config_valid[167] = true;
            if fallback {
                h.status.insert("GetConfigList".into(), -13);
            }
        }
        let list = call(&mut c, "list_configs", &[], json!({})).unwrap();
        assert_eq!(
            list,
            json!([{"index":7,"name":"slot 7","active":true,"valid":false}, {"index":167,"name":"slot 167","active":false,"valid":true}])
        );
        assert_eq!(
            h.lock().unwrap().calls_named("GetConfigFlags").len(),
            if fallback { 168 } else { 0 }
        );
        assert_eq!(
            call(&mut c, "list_configs", &[], json!({"include_empty":true}))
                .unwrap()
                .as_array()
                .unwrap()
                .len(),
            168
        );
    }
}

#[test]
fn malformed_configuration_and_safety_readbacks_never_become_false_success() {
    let (mut c, h) = connected();
    h.lock()
        .unwrap()
        .script("GetConfigList", reply(vec![json!([true]), json!([true])]));
    assert!(call(&mut c, "list_configs", &[], json!({})).is_err());
    for values in [
        vec![],
        vec![json!(false)],
        vec![Value::Null, json!(false)],
        vec![json!("bad"), json!(0)],
    ] {
        h.lock().unwrap().script("GetPSUEnable", reply(values));
        assert!(call(&mut c, "get_output_enabled", &[], json!({})).is_err());
    }
}

#[test]
fn product_info_uses_optional_metadata_only_for_documented_statuses() {
    let (mut c, h) = connected();
    h.lock().unwrap().status.insert("GetFWDate".into(), -14);
    h.lock().unwrap().status.insert("GetHWType".into(), -11);
    assert_eq!(
        call(&mut c, "get_product_info", &[], json!({})).unwrap(),
        json!({"product_no":12345,"product_id":"CGC PSU-CTRL","firmware":{"version":258,"date":null},"hardware":{"type":null,"version":3}})
    );
    h.lock().unwrap().status.insert("GetProductNo".into(), -13);
    assert!(call(&mut c, "get_product_info", &[], json!({})).is_err());
}

#[test]
fn telemetry_schema_state_flags_arrays_and_nonfinite_readings_match_python() {
    let (mut c, h) = connected();
    {
        let mut h = h.lock().unwrap();
        h.main_state = 0x8004;
        h.device_state = (1 << 2) | (1 << 9) | (1 << 26);
        h.extra_psu_state = (1 << 12) | (1 << 18) | (1 << 19) | (1 << 8);
        h.outputs = [true, false];
        h.ranges = [false, true];
        h.enabled = true;
        h.voltages = [120.0, 140.0];
        h.currents = [0.01, 0.02];
        h.measured[1] = [f64::NAN, 0.000004, 2.0];
        h.script("GetSensorData", reply(vec![json!([31.0])]));
        h.script(
            "GetFanData",
            reply(vec![
                json!([1]),
                json!([0]),
                json!([1000]),
                json!([900]),
                json!([500]),
            ]),
        );
    }
    let snapshot = call(&mut c, "collect_housekeeping", &[], json!({})).unwrap();
    assert_eq!(
        snapshot["output_enabled"],
        tuple(vec![json!(true), json!(false)])
    );
    assert_eq!(
        snapshot["main_state"],
        json!({"hex":"0x8004","name":"STATE_ERR_ILOCK"})
    );
    assert_eq!(
        snapshot["device_state"]["flags"],
        json!(["DEVST_VPSU0_FAIL", "DEVST_FAN2_FAIL", "DEVST_SEN3_LOW"])
    );
    assert_eq!(snapshot["psu_state"]["current_limit_active"], json!(true));
    assert_eq!(
        snapshot["psu_state"]["interlock_bnc_disabled"],
        json!(false)
    );
    assert_eq!(
        snapshot["sensors_c"],
        json!([31.0,{"$float":"nan"},{"$float":"nan"}])
    );
    assert_eq!(snapshot["fans"].as_array().unwrap().len(), 3);
    assert_eq!(
        snapshot["fans"][2],
        json!({"fan":2,"enabled":false,"failed":false,"set_rpm":0,"measured_rpm":0,"pwm":0})
    );
    assert_eq!(snapshot["channels"][1]["label"], json!("negative"));
    assert_eq!(
        snapshot["channels"][1]["voltage"],
        json!({"measured_v":{"$float":"nan"},"set_v":140.0,"limit_v":10000.0})
    );
    assert_eq!(
        snapshot["channels"][1]["current"]["measured_a"],
        json!(0.000004)
    );
    assert_eq!(
        snapshot["channels"][0]["rails"]["volt_12vn_v"],
        json!(-12.0)
    );
    assert_eq!(snapshot["uptime"]["milliseconds"], json!(456));
}

#[test]
fn telemetry_unknown_states_and_optional_full_range_support_are_preserved() {
    let (mut c, h) = connected();
    h.lock().unwrap().main_state = 0x1234;
    h.lock()
        .unwrap()
        .status
        .insert("HasPSUFullRange".into(), -10);
    let s = call(&mut c, "collect_housekeeping", &[], json!({})).unwrap();
    assert_eq!(s["main_state"]["name"], json!("UNKNOWN_STATE_0x1234"));
    assert_eq!(s["device_state"]["flags"], json!(["DEVST_OK"]));
    assert_eq!(s["channels"][0]["full_range"]["supported"], json!(false));
    h.lock().unwrap().status.insert("GetPSUData".into(), -11);
    assert!(call(&mut c, "collect_housekeeping", &[], json!({})).is_err());
}

#[test]
fn standby_recovery_is_confirmed_before_loading_operating_configuration() {
    let (mut c, h) = setup();
    h.lock().unwrap().load_outputs.insert(3, [true, false]);
    let state = call(
        &mut c,
        "initialize",
        &[],
        json!({"standby_config":3,"operating_config":4}),
    )
    .unwrap();
    assert_eq!(
        state,
        json!({"standby_config":3,"device_enabled":false,"output_enabled":{"$tuple":[false,false]},"standby_output_enabled_before_recovery":{"$tuple":[true,false]},"standby_outputs_recovered":true,"operating_config":4})
    );
    let h = h.lock().unwrap();
    let names = h.names();
    let disable = names.iter().position(|n| *n == "SetPSUEnable").unwrap();
    let operating = h
        .calls
        .iter()
        .position(|c| c.name == "LoadCurrentConfig" && c.args[1] == json!(4))
        .unwrap();
    assert!(disable < operating);
    assert!(names[disable + 1..operating].contains(&"GetPSUEnable"));
}

#[test]
fn failed_standby_off_keeps_link_and_never_loads_operating_config() {
    let (mut c, h) = connected();
    h.lock().unwrap().load_outputs.insert(3, [true, false]);
    h.lock().unwrap().ignore.insert("SetPSUEnable".into());
    let error = call(
        &mut c,
        "initialize",
        &[],
        json!({"standby_config":3,"operating_config":4}),
    )
    .unwrap_err();
    assert!(error.message.contains("cleanup failed"));
    assert!(
        !h.lock()
            .unwrap()
            .calls
            .iter()
            .any(|c| c.name == "LoadCurrentConfig" && c.args[1] == json!(4))
    );
    assert!(!h.lock().unwrap().names().contains(&"Close"));
    assert_eq!(c.get_attribute("connected").unwrap(), json!(true));
    assert_eq!(c.get_attribute("_open_failed").unwrap(), json!(false));
}

#[test]
fn startup_cancellation_after_standby_does_off_cleanup_not_operating_load() {
    let (mut c, h) = connected();
    let ctx = Context::test(10.0);
    h.lock().unwrap().load_outputs.insert(3, [true, true]);
    h.lock().unwrap().cancel_on = Some(("LoadCurrentConfig".into(), ctx.clone()));
    let error = c
        .call(
            "initialize",
            &[],
            &json!({"standby_config":3,"operating_config":4}),
            &ctx,
        )
        .unwrap_err();
    assert!(error.message.contains("cancelled"));
    let h = h.lock().unwrap();
    assert_eq!(h.calls_named("LoadCurrentConfig").len(), 1);
    assert_eq!(h.outputs, [false; 2]);
    assert!(!h.enabled && !h.open);
    assert!(h.names().contains(&"Close"));
}

#[test]
fn shutdown_zeroes_current_then_voltage_confirms_off_then_disconnects() {
    let (mut c, h) = connected();
    {
        let mut h = h.lock().unwrap();
        h.enabled = true;
        h.outputs = [true, true];
        h.voltages = [1000.0; 2];
        h.currents = [0.02; 2];
    }
    assert_eq!(
        call(&mut c, "shutdown", &[], json!({})).unwrap(),
        json!(true)
    );
    let h = h.lock().unwrap();
    assert_eq!(
        h.names(),
        [
            "SetPSUOutputCurrent",
            "SetPSUOutputCurrent",
            "SetPSUOutputVoltage",
            "SetPSUOutputVoltage",
            "SetPSUEnable",
            "SetDeviceEnable",
            "GetPSUEnable",
            "GetDeviceEnable",
            "Close"
        ]
    );
    assert_eq!(h.voltages, [0.0; 2]);
    assert_eq!(h.currents, [0.0; 2]);
    assert_eq!(h.outputs, [false; 2]);
    assert!(!h.enabled && !h.open);
    assert_eq!(c.get_attribute("connected").unwrap(), json!(false));
}

#[test]
fn any_shutdown_error_is_aggregated_keeps_link_and_later_off_can_recover() {
    let (mut c, h) = connected();
    h.lock().unwrap().enabled = true;
    h.lock().unwrap().outputs = [true, true];
    h.lock()
        .unwrap()
        .status
        .insert("SetPSUOutputCurrent".into(), -8);
    h.lock().unwrap().ignore.insert("SetPSUEnable".into());
    let error = call(&mut c, "shutdown", &[], json!({})).unwrap_err();
    assert!(error.message.contains("3 error(s)"));
    assert!(error.message.contains("not confirmed OFF"));
    assert!(!h.lock().unwrap().names().contains(&"Close"));
    assert!(h.lock().unwrap().names().contains(&"SetPSUOutputVoltage"));
    assert_eq!(c.get_attribute("connected").unwrap(), json!(true));
    h.lock().unwrap().status.clear();
    h.lock().unwrap().ignore.clear();
    assert_eq!(
        call(&mut c, "shutdown", &[], json!({})).unwrap(),
        json!(true)
    );
}

#[test]
fn explicit_standby_shutdown_still_disables_and_verifies_outputs() {
    let (mut c, h) = connected();
    h.lock().unwrap().load_outputs.insert(7, [true, true]);
    assert!(call(&mut c, "shutdown", &[], json!({"standby_config":7})).is_err());
    assert!(h.lock().unwrap().calls.is_empty());
    assert_eq!(
        call(
            &mut c,
            "shutdown",
            &[],
            json!({"standby_config":7,"disable_outputs":false,"disable_device":false})
        )
        .unwrap(),
        json!(true)
    );
    assert_eq!(
        h.lock().unwrap().names(),
        [
            "LoadCurrentConfig",
            "SetPSUEnable",
            "SetDeviceEnable",
            "GetPSUEnable",
            "GetDeviceEnable",
            "Close"
        ]
    );
}

#[test]
fn shutdown_close_failure_is_not_confirmed_disconnect() {
    let (mut c, h) = connected();
    h.lock().unwrap().status.insert("Close".into(), -3);
    assert!(
        call(&mut c, "shutdown", &[], json!({}))
            .unwrap_err()
            .message
            .contains("disconnect failed")
    );
    assert_eq!(c.get_attribute("connected").unwrap(), json!(true));
    assert_eq!(c.get_attribute("_dll_port_claimed").unwrap(), json!(true));
}

#[test]
fn manual_cold_start_preserves_mask_and_checks_before_enabling_and_ramping() {
    let (mut c, h) = connected();
    call(
        &mut c,
        "apply_manual_state",
        &[manual(300.0)],
        fast_profile(),
    )
    .unwrap();
    let h = h.lock().unwrap();
    assert_eq!(h.outputs, [true, false]);
    assert!(h.enabled);
    assert_eq!(h.voltages, [300.0, 0.0]);
    assert_eq!(h.currents, [0.01, 0.0]);
    let names = h.names();
    let global = names.iter().position(|n| *n == "SetDeviceEnable").unwrap();
    let gates = h
        .calls
        .iter()
        .position(|c| c.name == "SetPSUEnable" && c.args[1] == json!(1))
        .unwrap();
    assert!(names[..global].contains(&"GetPSUSetOutputCurrent"));
    assert!(names[..global].contains(&"GetInterlockEnable"));
    assert!(global < gates);
    let voltages: Vec<f64> = h
        .calls_named("SetPSUOutputVoltage")
        .iter()
        .filter(|c| c.args[1] == json!(0))
        .map(|c| c.args[2].as_f64().unwrap())
        .collect();
    assert_eq!(voltages, [0.0, 100.0, 200.0, 300.0]);
}

#[test]
fn setpoint_only_ramps_from_hardware_and_does_not_restart_or_touch_other_channel() {
    let (mut c, h) = connected();
    {
        let mut h = h.lock().unwrap();
        h.outputs = [true, false];
        h.enabled = true;
        h.voltages = [320.0, 777.12345];
        h.currents = [0.01, 0.01234];
    }
    let state = json!({"setpoints_only":true,"voltage_values":{"$map":[[0,80.0]]}});
    call(&mut c, "apply_manual_state", &[state], fast_profile()).unwrap();
    let h = h.lock().unwrap();
    assert_eq!(h.voltages, [80.0, 777.12345]);
    assert_eq!(h.currents, [0.01, 0.01234]);
    assert!(!h.names().iter().any(|n| {
        [
            "SetDeviceEnable",
            "SetPSUEnable",
            "SetPSUFullRange",
            "SetInterlockEnable",
            "SetPSUOutputCurrent",
        ]
        .contains(n)
    }));
    let steps: Vec<f64> = h
        .calls_named("SetPSUOutputVoltage")
        .iter()
        .map(|c| {
            assert_eq!(c.args[1], json!(0));
            c.args[2].as_f64().unwrap()
        })
        .collect();
    assert_eq!(steps, [240.0, 160.0, 80.0]);
}

#[test]
fn manual_readback_failures_disable_without_activation() {
    for fault in [
        "voltage",
        "current",
        "range",
        "interlock",
        "voltage_limit",
        "current_limit",
    ] {
        let (mut c, h) = connected();
        {
            let mut h = h.lock().unwrap();
            match fault {
                "voltage" => {
                    h.ignore.insert("SetPSUOutputVoltage".into());
                }
                "current" => {
                    h.currents[0] = 0.04;
                    h.ignore.insert("SetPSUOutputCurrent".into());
                }
                "range" => {
                    h.script("GetPSUFullRange", reply(vec![json!(0), json!(0)]));
                    h.ranges = [true, true];
                }
                "interlock" => {
                    h.interlocks = [false, true];
                    h.ignore.insert("SetInterlockEnable".into());
                }
                "voltage_limit" => h.voltage_limits[0] = 10.0,
                _ => h.current_limits[0] = f64::NAN,
            }
        }
        assert!(
            call(
                &mut c,
                "apply_manual_state",
                &[manual(20.0)],
                fast_profile()
            )
            .is_err(),
            "{fault}"
        );
        let h = h.lock().unwrap();
        assert!(
            !h.calls
                .iter()
                .any(|c| c.name == "SetDeviceEnable" && c.args[1] == json!(1)),
            "{fault}"
        );
        assert_eq!(h.outputs, [false; 2]);
        assert!(!h.enabled);
    }
}

#[test]
fn ignored_disable_refuses_range_and_setpoint_reconfiguration() {
    let (mut c, h) = connected();
    {
        let mut h = h.lock().unwrap();
        h.enabled = true;
        h.outputs = [true, false];
        h.ignore.insert("SetPSUEnable".into());
    }
    let mut state = manual(20.0);
    state["full_range_enabled"]["0"] = json!(true);
    let error = call(&mut c, "apply_manual_state", &[state], fast_profile()).unwrap_err();
    assert!(
        error
            .message
            .contains("not confirmed disabled before reconfiguration")
    );
    assert!(error.message.contains("outputs may still be live"));
    let h = h.lock().unwrap();
    assert!(!h.names().iter().any(|n| {
        [
            "SetPSUFullRange",
            "SetPSUOutputCurrent",
            "SetPSUOutputVoltage",
        ]
        .contains(n)
    }));
    assert!(!h.names().contains(&"Close"));
}

#[test]
fn range_switch_uses_measured_voltage_discharge_not_target_and_checks_both_channels() {
    let (mut c, h) = connected();
    h.lock().unwrap().voltages = [2000.0, 2000.0];
    h.lock().unwrap().measured = [[1.0, 0.0, 2.0], [4.9, 0.0, 2.0]];
    let mut state = manual(20.0);
    state["full_range_enabled"]["0"] = json!(true);
    call(&mut c, "apply_manual_state", &[state], fast_profile()).unwrap();
    let h = h.lock().unwrap();
    let range = h
        .names()
        .iter()
        .position(|n| *n == "SetPSUFullRange")
        .unwrap();
    let measurements: Vec<&Call> = h.calls[..range]
        .iter()
        .filter(|c| c.name == "GetPSUData")
        .collect();
    assert_eq!(measurements.len(), 2);
    assert_eq!(measurements[1].args[1], json!(1));
    assert_eq!(h.ranges, [true, false]);
}

#[test]
fn missing_or_nonfinite_discharge_readback_never_switches_relay() {
    for missing in [true, false] {
        let (mut c, h) = connected();
        if missing {
            h.lock().unwrap().status.insert("GetPSUData".into(), -11);
        } else {
            h.lock().unwrap().measured[1][0] = f64::NAN;
        }
        let mut state = manual(20.0);
        state["full_range_enabled"]["0"] = json!(true);
        let error = c
            .call(
                "apply_manual_state",
                &[state],
                &fast_profile(),
                &Context::test(0.01),
            )
            .unwrap_err();
        assert!(error.message.contains("discharge") || error.kind == "TimeoutError");
        assert!(!h.lock().unwrap().names().contains(&"SetPSUFullRange"));
        assert!(!h.lock().unwrap().enabled);
    }
}

#[test]
fn interlock_setting_false_is_applied_and_verified_not_silently_forced_true() {
    let (mut c, h) = connected();
    let mut kwargs = fast_profile();
    kwargs["interlock_monitoring"] = json!(false);
    call(&mut c, "apply_manual_state", &[manual(20.0)], kwargs).unwrap();
    assert_eq!(h.lock().unwrap().interlocks, [false; 2]);
}

#[test]
fn off_cancels_after_first_positive_ramp_step_and_recovers_before_return() {
    let (mut c, h) = connected();
    let (sender, receiver) = std::sync::mpsc::sync_channel(128);
    let ctx = Context::new(
        Duration::from_secs(600),
        Arc::new(std::sync::atomic::AtomicBool::new(false)),
        Some(sender),
    )
    .with_request_id(42)
    .with_native_timeout(Duration::from_secs(1));
    h.lock().unwrap().cancel_on_voltage = Some(ctx.clone());
    let error = c
        .call(
            "apply_manual_state",
            &[manual(500.0)],
            &fast_profile(),
            &ctx,
        )
        .unwrap_err();
    assert!(error.message.contains("cancelled"));
    let h = h.lock().unwrap();
    assert_eq!(
        h.calls_named("SetPSUOutputVoltage")
            .iter()
            .filter(|c| c.args[2].as_f64().unwrap() > 0.0)
            .count(),
        1
    );
    assert_eq!(h.outputs, [false; 2]);
    assert!(!h.enabled);
    assert!(h.open);
    assert_eq!(c.get_attribute("connected").unwrap(), json!(true));
    assert!(ctx.is_cancelled());
    let reports: Vec<_> = receiver.try_iter().collect();
    let enters: Vec<_> = reports
        .iter()
        .filter(|report| report["kind"] == "native_io" && report["phase"] == "enter")
        .collect();
    assert_eq!(enters.len(), h.calls.len());
    for (sequence, (report, call)) in enters.iter().zip(&h.calls).enumerate() {
        assert_eq!(report["id"], json!(42));
        assert_eq!(report["sequence"], json!(sequence));
        assert_eq!(
            report["symbol"],
            json!(format!("COM_HVPSU2D_{}", call.name))
        );
    }
    for report in &enters[enters.len() - 4..] {
        let timeout = report["timeout_s"].as_f64().unwrap();
        assert!(timeout > 0.0 && timeout <= 5.0);
    }
    assert_eq!(
        enters.last().unwrap()["symbol"],
        json!("COM_HVPSU2D_GetPSUEnable")
    );
}

#[test]
fn cancel_during_discharge_or_interlock_readback_prevents_relay_or_enable() {
    for symbol in ["GetPSUData", "GetInterlockEnable"] {
        let (mut c, h) = connected();
        let ctx = Context::test(10.0);
        h.lock().unwrap().cancel_on = Some((symbol.into(), ctx.clone()));
        let mut state = manual(20.0);
        if symbol == "GetPSUData" {
            state["full_range_enabled"]["0"] = json!(true);
        }
        assert!(
            c.call("apply_manual_state", &[state], &fast_profile(), &ctx)
                .is_err()
        );
        let h = h.lock().unwrap();
        assert!(
            !h.calls
                .iter()
                .any(|c| c.name == "SetDeviceEnable" && c.args[1] == json!(1))
        );
        if symbol == "GetPSUData" {
            assert!(!h.names().contains(&"SetPSUFullRange"));
        }
        assert_eq!(h.outputs, [false; 2]);
        assert!(!h.enabled);
    }
}

#[test]
fn pre_cancelled_or_expired_requests_do_no_hardware_io() {
    let (mut c, h) = connected();
    let cancelled = Context::test(10.0);
    cancelled.cancel();
    let expired = Context::test(0.0);
    for ctx in [cancelled, expired] {
        assert!(
            c.call("apply_manual_state", &[manual(20.0)], &fast_profile(), &ctx)
                .is_err()
        );
        assert!(
            c.call(
                "set_channel_voltage",
                &[json!(0), json!(20.0)],
                &json!({}),
                &ctx
            )
            .is_err()
        );
        assert!(c.call("connect", &[], &json!({}), &ctx).is_err());
    }
    assert!(h.lock().unwrap().calls.is_empty());
}

#[test]
fn ramp_final_bad_readback_disables_and_does_not_return_success() {
    let (mut c, h) = connected();
    {
        let mut h = h.lock().unwrap();
        h.enabled = true;
        h.outputs = [true, false];
        h.voltages = [100.0, 0.0];
        h.ignore.insert("SetPSUOutputVoltage".into());
    }
    let error = call(
        &mut c,
        "ramp_channel_voltage",
        &[json!(0), json!(300.0)],
        fast_profile(),
    )
    .unwrap_err();
    assert!(error.message.contains("ramp target readback"));
    assert_eq!(h.lock().unwrap().outputs, [false; 2]);
    assert!(!h.lock().unwrap().enabled);
}

#[test]
fn native_ramp_caches_verified_limit_but_refreshes_final_and_other_state() {
    let (mut c, h) = connected();
    h.lock().unwrap().enabled = true;
    h.lock().unwrap().outputs = [true, false];
    call(
        &mut c,
        "ramp_channel_voltage",
        &[json!(0), json!(500.0)],
        fast_profile(),
    )
    .unwrap();
    let h = h.lock().unwrap();
    let names = h.names();
    let first = names
        .iter()
        .position(|n| *n == "SetPSUOutputVoltage")
        .unwrap();
    let last = names
        .iter()
        .rposition(|n| *n == "SetPSUOutputVoltage")
        .unwrap();
    assert!(!names[first..=last].contains(&"GetPSUSetOutputVoltage"));
    assert_eq!(h.calls_named("SetPSUOutputVoltage").len(), 5);
    assert!(names[last + 1..].contains(&"GetPSUSetOutputVoltage"));
}

#[test]
fn invalid_native_manual_state_is_rejected_without_hardware_side_effects() {
    let (mut c, h) = connected();
    for state in [
        json!({"voltage_values":{"2":1}}),
        json!({"voltage_values":{"0":-1}}),
        json!({"current_limit_values":{"0":{"$float":"nan"}}}),
        json!({"output_enabled":{"0":1}}),
        json!({"setpoints_only":true}),
        json!({"setpoints_only":true,"output_enabled":{"0":true},"voltage_values":{"0":20}}),
        json!({"voltage_values":{"$map":[[0,10],[0,20]]}}),
        json!({"unknown":1}),
    ] {
        assert_eq!(
            call(&mut c, "apply_manual_state", &[state], fast_profile())
                .unwrap_err()
                .kind,
            "ValueError"
        );
    }
    for kwargs in [
        json!({"ramp_step_v":0}),
        json!({"ramp_step_interval_s":0}),
        json!({"interlock_monitoring":"true"}),
    ] {
        assert_eq!(
            call(&mut c, "apply_manual_state", &[manual(20.0)], kwargs)
                .unwrap_err()
                .kind,
            "ValueError"
        );
    }
    assert!(h.lock().unwrap().calls.is_empty());
}

#[test]
fn poisoned_transport_does_not_retry_or_claim_shutdown_or_recovery_succeeded() {
    let (mut c, h) = connected();
    h.lock().unwrap().script_error(
        "SetPSUOutputVoltage",
        Error::new("TimeoutError", "blocked native export"),
    );
    let error = call(
        &mut c,
        "apply_manual_state",
        &[manual(20.0)],
        fast_profile(),
    )
    .unwrap_err();
    assert!(error.message.contains("outputs may still be live"));
    assert_eq!(c.get_attribute("_transport_poisoned").unwrap(), json!(true));
    let count = h.lock().unwrap().calls.len();
    assert!(call(&mut c, "shutdown", &[], json!({})).is_err());
    assert!(call(&mut c, "connect", &[], json!({})).is_err());
    assert_eq!(
        call(&mut c, "disconnect", &[], json!({})).unwrap(),
        json!(false)
    );
    assert_eq!(h.lock().unwrap().calls.len(), count);
}

#[test]
fn unsupported_methods_private_helpers_and_bad_arguments_are_explicit_errors() {
    let (mut c, h) = connected();
    for method in [
        "_zero_output_setpoints",
        "cached_setpoint_limits",
        "unknown",
        "close",
        "open_port",
    ] {
        assert_eq!(
            call(&mut c, method, &[], json!({})).unwrap_err().kind,
            "NotImplementedError"
        );
        assert!(!PUBLIC_METHODS.contains(&method));
    }
    assert_eq!(
        call(
            &mut c,
            "set_channel_voltage",
            &[json!(0), json!(20)],
            json!({"channel":1})
        )
        .unwrap_err()
        .kind,
        "TypeError"
    );
    assert_eq!(
        call(&mut c, "get_status", &[json!(0)], json!({}))
            .unwrap_err()
            .kind,
        "TypeError"
    );
    assert_eq!(
        call(&mut c, "save_config", &[json!(0), json!("name")], json!({}))
            .unwrap_err()
            .kind,
        "TypeError"
    );
    assert_eq!(
        call(
            &mut c,
            "set_config_flags",
            &[json!(0)],
            json!({"active":true})
        )
        .unwrap_err()
        .kind,
        "TypeError"
    );
    for timeout in [json!(0), json!(-1), float(f64::NAN), float(f64::INFINITY)] {
        assert_eq!(
            call(
                &mut c,
                "get_device_enabled",
                &[],
                json!({"timeout_s":timeout})
            )
            .unwrap_err()
            .kind,
            "ValueError"
        );
    }
    assert!(h.lock().unwrap().calls.is_empty());
}

#[test]
fn state_attributes_are_read_only_and_connection_identity_cannot_change_live() {
    let (mut c, _h) = connected();
    for name in ["connected", "_transport_poisoned", "_dll_port_claimed"] {
        assert_eq!(
            c.set_attribute(name, json!(false)).unwrap_err().kind,
            "AttributeError"
        );
    }
    assert!(c.set_attribute("com", json!(9)).is_err());
    call(&mut c, "disconnect", &[], json!({})).unwrap();
    c.set_attribute("com", json!(9)).unwrap();
    assert_eq!(c.get_attribute("com").unwrap(), json!(9));
    assert_eq!(c.get_attribute("MAX_CONFIG").unwrap(), json!(168));
    assert!(c.set_attribute("port", json!(16)).is_err());
    assert!(c.get_attribute("dll").is_err());
}

#[test]
fn facade_inventory_contains_every_high_level_python_controller_method() {
    let source = include_str!("../../../psu_a/vendor/runtime/psu/psu.py");
    let source = source
        .split("class PSU(ProcessIsolatedClientMixin)")
        .next()
        .unwrap();
    let methods: Vec<&str> = source
        .lines()
        .filter_map(|line| line.strip_prefix("    def "))
        .filter_map(|line| line.split('(').next())
        .filter(|name| !name.starts_with('_') && *name != "cached_setpoint_limits")
        .collect();
    for method in methods {
        assert!(
            PUBLIC_METHODS.contains(&method),
            "unported public facade method: {method}"
        );
    }
    let unique: HashSet<_> = PUBLIC_METHODS.iter().collect();
    assert_eq!(unique.len(), PUBLIC_METHODS.len());
}

#[test]
fn cancelled_initial_open_disables_existing_live_hardware_before_close() {
    for symbol in ["Open", "SetBaudRate", "GetProductID"] {
        let (mut c, h) = setup();
        let ctx = Context::test(10.0);
        {
            let mut h = h.lock().unwrap();
            h.enabled = true;
            h.outputs = [true; 2];
            h.voltages = [300.0; 2];
            h.currents = [0.02; 2];
            h.cancel_on = Some((symbol.into(), ctx.clone()));
        }
        let error = c
            .call("initialize", &[], &json!({"operating_config": 4}), &ctx)
            .unwrap_err();
        assert!(error.message.contains("cancelled"));
        let h = h.lock().unwrap();
        assert_eq!(h.outputs, [false; 2]);
        assert!(!h.enabled && !h.open);
        assert!(!h.names().contains(&"LoadCurrentConfig"));
        let off = h.names().iter().position(|n| *n == "GetPSUEnable").unwrap();
        let close = h.names().iter().position(|n| *n == "Close").unwrap();
        assert!(off < close);
    }
}

#[test]
fn cancelled_connect_retains_live_connection_if_off_is_unconfirmed() {
    let (mut c, h) = setup();
    let ctx = Context::test(10.0);
    {
        let mut h = h.lock().unwrap();
        h.enabled = true;
        h.outputs = [true; 2];
        h.ignore.insert("SetPSUEnable".into());
        h.cancel_on = Some(("Open".into(), ctx.clone()));
    }
    let error = c.call("connect", &[], &json!({}), &ctx).unwrap_err();
    assert!(error.message.contains("safety cleanup failed"));
    assert_eq!(c.get_attribute("connected").unwrap(), json!(true));
    assert!(!h.lock().unwrap().names().contains(&"Close"));
    h.lock().unwrap().ignore.clear();
    assert_eq!(
        call(&mut c, "shutdown", &[], json!({})).unwrap(),
        json!(true)
    );
}

#[test]
fn cancelling_shutdown_does_not_cancel_remaining_off_steps_or_verify_close() {
    let (mut c, h) = connected();
    let ctx = Context::test(10.0);
    {
        let mut h = h.lock().unwrap();
        h.enabled = true;
        h.outputs = [true; 2];
        h.voltages = [1000.0; 2];
        h.currents = [0.02; 2];
        h.cancel_on = Some(("SetPSUOutputCurrent".into(), ctx.clone()));
    }
    let error = c.call("shutdown", &[], &json!({}), &ctx).unwrap_err();
    assert!(error.message.contains("cancelled"));
    let h = h.lock().unwrap();
    assert_eq!(h.outputs, [false; 2]);
    assert!(!h.enabled && !h.open);
    assert_eq!(h.voltages, [0.0; 2]);
    assert_eq!(h.currents, [0.0; 2]);
    assert_eq!(h.names().last(), Some(&"Close"));
    assert_eq!(c.get_attribute("connected").unwrap(), json!(false));
}

#[test]
fn per_export_io_deadline_poison_stops_all_further_calls() {
    let (mut c, h) = connected();
    h.lock()
        .unwrap()
        .delays
        .insert("GetPSUData".into(), Duration::from_millis(20));
    let (sender, receiver) = std::sync::mpsc::sync_channel(4);
    let ctx = Context::new(
        Duration::from_secs(10),
        Arc::new(std::sync::atomic::AtomicBool::new(false)),
        Some(sender),
    )
    .with_request_id(41)
    .with_native_timeout(Duration::from_secs(1));
    let error = c
        .call(
            "get_channel_measurements",
            &[json!(0)],
            &json!({"timeout_s": 0.001}),
            &ctx,
        )
        .unwrap_err();
    assert_eq!(error.kind, "TimeoutError");
    assert_eq!(c.get_attribute("_transport_poisoned").unwrap(), json!(true));
    let reports: Vec<_> = receiver.try_iter().collect();
    assert_eq!(reports.len(), 2);
    for (report, phase) in reports.iter().zip(["enter", "exit"]) {
        assert_eq!(report["kind"], json!("native_io"));
        assert_eq!(report["id"], json!(41));
        assert_eq!(report["sequence"], json!(0));
        assert_eq!(report["symbol"], json!("COM_HVPSU2D_GetPSUData"));
        assert_eq!(report["phase"], json!(phase));
    }
    assert_eq!(reports[0]["timeout_s"], json!(0.001));
    assert!(reports[1]["elapsed_s"].as_f64().unwrap() >= 0.001);
    let count = h.lock().unwrap().calls.len();
    assert!(call(&mut c, "get_device_enabled", &[], json!({})).is_err());
    assert_eq!(h.lock().unwrap().calls.len(), count);

    // A recoverable read failure must not hide a delayed OFF behind a ramp budget.
    let (mut c, h) = connected();
    {
        let mut h = h.lock().unwrap();
        h.enabled = true;
        h.outputs = [true, false];
        h.status.insert("GetPSUFullRange".into(), -16);
        h.delays
            .insert("SetPSUEnable".into(), Duration::from_millis(20));
    }
    let (sender, receiver) = std::sync::mpsc::sync_channel(8);
    let ctx = Context::new(
        Duration::from_secs(600),
        Arc::new(std::sync::atomic::AtomicBool::new(false)),
        Some(sender),
    )
    .with_request_id(43)
    .with_native_timeout(Duration::from_secs(1));
    let mut kwargs = fast_profile();
    kwargs["timeout_s"] = json!(0.001);
    let error = c
        .call("apply_manual_state", &[manual(20.0)], &kwargs, &ctx)
        .unwrap_err();
    assert!(error.message.contains("outputs may still be live"));
    assert_eq!(c.get_attribute("_transport_poisoned").unwrap(), json!(true));
    assert_eq!(
        h.lock().unwrap().names(),
        ["GetPSUFullRange", "SetPSUEnable"]
    );
    let reports: Vec<_> = receiver.try_iter().collect();
    assert_eq!(reports.len(), 4);
    for (report, phase) in reports[2..].iter().zip(["enter", "exit"]) {
        assert_eq!(report["kind"], json!("native_io"));
        assert_eq!(report["id"], json!(43));
        assert_eq!(report["sequence"], json!(1));
        assert_eq!(report["symbol"], json!("COM_HVPSU2D_SetPSUEnable"));
        assert_eq!(report["phase"], json!(phase));
    }
    assert_eq!(reports[2]["timeout_s"], json!(0.001));
}

#[test]
fn blocked_progress_consumer_cannot_leave_a_failed_ramp_running() {
    let (mut c, h) = connected();
    {
        let mut h = h.lock().unwrap();
        h.enabled = true;
        h.outputs = [true, false];
    }
    let (sender, _receiver) = std::sync::mpsc::sync_channel(1);
    let ctx = Context::new(
        Duration::from_secs(10),
        Arc::new(std::sync::atomic::AtomicBool::new(false)),
        Some(sender),
    );
    let error = c
        .call(
            "ramp_channel_voltage",
            &[json!(0), json!(400.0)],
            &fast_profile(),
            &ctx,
        )
        .unwrap_err();
    assert_eq!(error.kind, "TimeoutError");
    assert!(
        error
            .message
            .contains("Native supervision transport is unusable")
    );
    let h = h.lock().unwrap();
    assert_eq!(h.calls_named("SetPSUOutputVoltage").len(), 2);
    assert_eq!(h.outputs, [false; 2]);
    assert!(!h.enabled);
}

#[test]
fn discharge_must_be_strictly_below_threshold_and_timeout_preserves_relays() {
    let (mut c, h) = connected();
    h.lock().unwrap().measured[1][0] = -5.0;
    let mut state = manual(20.0);
    state["full_range_enabled"]["0"] = json!(true);
    let error = call(&mut c, "apply_manual_state", &[state], fast_profile()).unwrap_err();
    assert!(
        error
            .message
            .contains("did not discharge below 5 V within 2 s")
    );
    assert!(!h.lock().unwrap().names().contains(&"SetPSUFullRange"));
    assert_eq!(h.lock().unwrap().ranges, [false; 2]);
}

#[test]
fn old_cancel_token_cannot_be_revived_by_a_new_output_session() {
    let (mut c, h) = connected();
    let old = Context::test(10.0);
    old.cancel();
    assert!(
        c.call("apply_manual_state", &[manual(20.0)], &fast_profile(), &old)
            .is_err()
    );
    call(
        &mut c,
        "apply_manual_state",
        &[manual(20.0)],
        fast_profile(),
    )
    .unwrap();
    let count = h.lock().unwrap().calls.len();
    assert!(
        c.call(
            "set_channel_voltage",
            &[json!(0), json!(40.0)],
            &json!({}),
            &old
        )
        .is_err()
    );
    assert!(
        c.call("initialize", &[], &json!({"operating_config": 4}), &old)
            .is_err()
    );
    assert!(c.call("shutdown", &[], &json!({}), &old).is_err());
    assert_eq!(h.lock().unwrap().calls.len(), count);
    assert_eq!(h.lock().unwrap().voltages[0], 20.0);
}

#[test]
fn optional_boolean_none_retains_python_truthiness_not_default_true() {
    let (mut c, h) = connected();
    h.lock().unwrap().load_outputs.insert(3, [true, false]);
    let state = call(
        &mut c,
        "initialize",
        &[],
        json!({"standby_config": 3, "require_standby_outputs_disabled": null}),
    )
    .unwrap();
    assert_eq!(
        state["output_enabled"],
        tuple(vec![json!(true), json!(false)])
    );
    assert!(!h.lock().unwrap().names().contains(&"SetPSUEnable"));
    h.lock().unwrap().calls.clear();
    call(
        &mut c,
        "save_config",
        &[json!(3)],
        json!({"active": null, "valid": false}),
    )
    .unwrap();
    assert!(h.lock().unwrap().config_active[3]);
    assert!(!h.lock().unwrap().config_valid[3]);
    h.lock().unwrap().calls.clear();
    call(
        &mut c,
        "shutdown",
        &[],
        json!({"disable_outputs": null, "disable_device": false}),
    )
    .unwrap();
    assert_eq!(h.lock().unwrap().names(), ["Close"]);
}

#[test]
fn init_accepts_common_integrity_log_fields_but_rejects_unknown_or_host_objects() {
    let base = json!({"device_id": "PSU_A", "com": 7, "port": 2,
        "dll_path": "/tmp/COM-HVPSU2D.dll", "dll_sha256": "a".repeat(64),
        "log_dir": "/tmp/psu-logs", "logger": null, "thread_lock": null});
    let h = Arc::new(Mutex::new(Hardware::default()));
    Controller::new(&base, Box::new(MockDll(h.clone()))).unwrap();
    for field in ["unknown", "logger", "thread_lock"] {
        let mut config = base.clone();
        config[field] = json!("host-only");
        assert!(Controller::new(&config, Box::new(MockDll(h.clone()))).is_err());
    }
    for (field, value) in [
        ("com", json!(0)),
        ("port", json!(16)),
        ("baudrate", json!(0)),
        ("baudrate", json!(u64::MAX)),
    ] {
        let mut config = base.clone();
        config[field] = value;
        assert!(Controller::new(&config, Box::new(MockDll(h.clone()))).is_err());
    }
    assert!(h.lock().unwrap().calls.is_empty());
}

#[test]
fn windows_bool_accepts_nonzero_i32_but_c_bool_remains_typed() {
    let (mut c, h) = connected();
    h.lock()
        .unwrap()
        .script("GetDeviceEnable", reply(vec![json!(-1)]));
    assert_eq!(
        call(&mut c, "get_device_enabled", &[], json!({})).unwrap(),
        json!(true)
    );
    h.lock()
        .unwrap()
        .script("GetPSUEnable", reply(vec![json!(-5), json!(0)]));
    assert_eq!(
        call(&mut c, "get_output_enabled", &[], json!({})).unwrap(),
        tuple(vec![json!(true), json!(false)])
    );
    h.lock().unwrap().status.insert("GetConfigList".into(), -13);
    h.lock()
        .unwrap()
        .script("GetConfigFlags", reply(vec![json!(1), json!(0)]));
    assert!(
        call(&mut c, "list_configs", &[], json!({}))
            .unwrap_err()
            .message
            .contains("C bool")
    );
}
