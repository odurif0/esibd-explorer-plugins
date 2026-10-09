#![cfg(all(feature = "amx", feature = "amx_hd"))]

use esibd_native_worker::{
    Backend, amx, amx_hd, codec,
    context::Context,
    error::{Error, Result},
    ffi::{self, Dll, NativeReply},
};
use serde_json::{Value, json};
use std::collections::{BTreeMap, VecDeque};
use std::sync::{Arc, Mutex, OnceLock};

#[derive(Clone)]
struct Config {
    name: String,
    active: bool,
    valid: bool,
    enabled: bool,
    period: u32,
    widths: Vec<u32>,
}

struct Instrument {
    hd: bool,
    calls: Vec<(String, Vec<Value>)>,
    enabled: bool,
    sticky_enabled: bool,
    period: u32,
    widths: Vec<u32>,
    delays: Vec<u32>,
    timer_count: u32,
    configs: BTreeMap<u32, Config>,
    overrides: BTreeMap<String, VecDeque<Result<NativeReply>>>,
    cancel_on: Option<(String, Context)>,
    delay_on: Option<String>,
    registers: BTreeMap<(String, u32), Value>,
    main_state: Option<u32>,
    device_bits: u32,
    controller_bits: Option<u32>,
    nonfinite: bool,
}

impl Instrument {
    fn new(hd: bool) -> Self {
        Self {
            hd,
            calls: Vec::new(),
            enabled: false,
            sticky_enabled: false,
            period: 198,
            widths: vec![98, 0, 0, 0],
            delays: vec![7, 0, 0, 0],
            timer_count: if hd { 2 } else { 4 },
            configs: BTreeMap::new(),
            overrides: BTreeMap::new(),
            cancel_on: None,
            delay_on: None,
            registers: BTreeMap::new(),
            main_state: None,
            device_bits: 0,
            controller_bits: None,
            nonfinite: false,
        }
    }

    fn add_config(&mut self, index: u32, name: &str, enabled: bool) {
        self.configs.insert(
            index,
            Config {
                name: name.into(),
                active: true,
                valid: true,
                enabled,
                period: if enabled { 798 } else { 49998 },
                widths: if enabled {
                    vec![298, 508, 0, 0]
                } else {
                    vec![0; 4]
                },
            },
        );
    }

    fn inject(&mut self, suffix: &str, status: i64, values: Vec<Value>) {
        self.overrides
            .entry(suffix.into())
            .or_default()
            .push_back(Ok(NativeReply { status, values }));
    }

    fn fail(&mut self, suffix: &str, kind: &str) {
        self.overrides
            .entry(suffix.into())
            .or_default()
            .push_back(Err(Error::new(kind, "injected DLL failure")));
    }

    fn names(&self) -> Vec<String> {
        self.calls
            .iter()
            .map(|(s, _)| suffix(s).to_owned())
            .collect()
    }

    fn dispatch(&mut self, symbol: &str, args: &[Value]) -> Result<NativeReply> {
        let name = suffix(symbol);
        assert!(symbol.starts_with(if self.hd {
            "COM_HVAMX4EDH_"
        } else {
            "COM_HVAMX4ED_"
        }));
        let initial_outputs = validate_abi(self.hd, symbol, args);
        self.calls.push((symbol.to_owned(), args.to_vec()));
        let overridden = self.overrides.get_mut(name).and_then(VecDeque::pop_front);
        let result = if let Some(result) = overridden {
            result.map(|mut reply| {
                // Vendor errors still return the ABI's correctly sized pointer
                // buffers; untouched buffers retain their supplied initial data.
                if reply.status != 0 && reply.values.is_empty() {
                    reply.values = initial_outputs;
                }
                reply
            })
        } else {
            self.respond(name, args)
        };
        if self.delay_on.as_deref() == Some(name) {
            std::thread::sleep(std::time::Duration::from_millis(60));
        }
        if let Some((name_to_cancel, context)) = &self.cancel_on
            && name_to_cancel == name
        {
            context.cancel();
        }
        result
    }

    fn respond(&mut self, name: &str, args: &[Value]) -> Result<NativeReply> {
        let values = match name {
            "Open" => {
                assert_eq!(args.len(), 2);
                assert_eq!(args[0], 3);
                assert_eq!(args[1], 7);
                vec![]
            }
            "Close" | "Purge" | "SaveCurrentConfig" | "SaveDefaults" | "LoadDefaults"
            | "Restart" | "GetInterfaceState" => vec![],
            "SetBaudRate" => {
                assert_eq!(args.len(), 2);
                vec![args[1].clone()]
            }
            "GetProductID" | "GetConfigName" | "GetFWDate" | "GetFwDate" => {
                let capacity = match name {
                    "GetProductID" => {
                        if self.hd {
                            81
                        } else {
                            60
                        }
                    }
                    "GetConfigName" => {
                        if self.hd {
                            193
                        } else {
                            52
                        }
                    }
                    "GetFwDate" => 12,
                    _ => 16,
                };
                assert_eq!(args.last().unwrap()["capacity"], capacity);
                let value = if name == "GetProductID" {
                    "HV-AMX-CTRL-4EDH".into()
                } else if name == "GetConfigName" {
                    self.configs
                        .get(&(args[1].as_u64().unwrap() as u32))
                        .map(|c| c.name.clone())
                        .unwrap_or_default()
                } else {
                    "2026-10-09".into()
                };
                vec![json!(value)]
            }
            "GetConfigList" => {
                let n = if self.hd { 500 } else { 126 };
                assert_eq!(args[1].as_array().unwrap().len(), n);
                assert_eq!(args[2].as_array().unwrap().len(), n);
                let mut active = vec![false; n];
                let mut valid = vec![false; n];
                for (index, config) in &self.configs {
                    active[*index as usize] = config.active;
                    valid[*index as usize] = config.valid;
                }
                vec![json!(active), json!(valid)]
            }
            "GetConfigFlags" => {
                let config = self.configs.get(&(args[1].as_u64().unwrap() as u32));
                vec![
                    json!(config.is_some_and(|c| c.active)),
                    json!(config.is_some_and(|c| c.valid)),
                ]
            }
            "LoadCurrentConfig" => {
                let index = args[1].as_u64().unwrap() as u32;
                if let Some(config) = self.configs.get(&index) {
                    self.enabled = config.enabled;
                    self.period = config.period;
                    self.widths = config.widths.clone();
                }
                vec![]
            }
            "SetDeviceEnable" => {
                if !self.sticky_enabled {
                    self.enabled = args[1].as_bool().unwrap();
                }
                vec![]
            }
            "GetDeviceEnable" => vec![json!(self.enabled)],
            "GetMainState" => vec![json!(self.main_state.unwrap_or(if self.enabled {
                0
            } else {
                0x8004
            }))],
            "GetDeviceState" => {
                if self.hd {
                    vec![
                        json!(self.main_state.unwrap_or(u32::from(self.enabled))),
                        json!(self.device_bits),
                        json!(0),
                    ]
                } else {
                    vec![json!(self.device_bits)]
                }
            }
            "GetControllerState" => vec![json!(self.controller_bits.unwrap_or(if self.enabled {
                1287
            } else {
                0
            }))],
            "GetState" => vec![
                json!(
                    self.controller_bits
                        .unwrap_or(if self.enabled { 0x1_0007 } else { 0 })
                ),
                json!(3),
            ],
            "GetHousekeeping" => {
                if self.hd {
                    vec![
                        json!(12.),
                        json!(11.5),
                        json!(5.),
                        json!(3.3),
                        json!(3.31),
                        json!(2.51),
                        json!(1.2),
                        json!(42.5),
                    ]
                } else {
                    vec![
                        json!(12.),
                        json!(5.),
                        json!(3.3),
                        if self.nonfinite {
                            codec::float(f64::NAN)
                        } else {
                            json!(42.5)
                        },
                    ]
                }
            }
            "GetSensorData" => {
                assert_eq!(args[1].as_array().unwrap().len(), 3);
                vec![json!([20., 21., 22.])]
            }
            "GetFanData" => {
                for value in &args[1..] {
                    assert_eq!(value.as_array().unwrap().len(), 3);
                }
                vec![
                    json!(vec![true; 3]),
                    json!(vec![false; 3]),
                    json!(vec![1500; 3]),
                    json!(vec![1495; 3]),
                    json!(vec![500; 3]),
                ]
            }
            "GetLEDData" => vec![json!(false), json!(true), json!(false)],
            "GetCPUData" => vec![json!(0.1), json!(100e6)],
            "GetUptime" => vec![json!(100), json!(500), json!(50)],
            "GetTotalTime" => vec![json!(1000), json!(500)],
            "GetProductNo" => vec![json!(1234)],
            "GetFWVersion" | "GetFwVersion" => vec![json!(0x1234)],
            "GetHWType" => vec![json!(2)],
            "GetHWVersion" => vec![json!(5)],
            "GetHwType" => vec![
                json!(2),
                json!(64),
                json!(8),
                json!(2),
                json!(2),
                json!(4),
                json!(4),
                json!(4),
                json!(0),
            ],
            "GetHwVersion" => vec![json!(5), json!(6)],
            "GetOscillatorPeriod" => {
                assert_eq!(args.len(), if self.hd { 3 } else { 2 });
                if self.hd {
                    assert_eq!(args[1], 0);
                }
                vec![json!(self.period)]
            }
            "SetOscillatorPeriod" => {
                assert_eq!(args.len(), if self.hd { 3 } else { 2 });
                self.period = args.last().unwrap().as_u64().unwrap() as u32;
                vec![]
            }
            "GetTimerCount" => vec![json!(self.timer_count)],
            "GetPulserWidth" | "GetTimerWidth" => {
                vec![json!(self.widths[args[1].as_u64().unwrap() as usize])]
            }
            "GetPulserDelay" | "GetTimerDelay" => {
                vec![json!(self.delays[args[1].as_u64().unwrap() as usize])]
            }
            "SetPulserWidth" | "SetTimerWidth" => {
                self.widths[args[1].as_u64().unwrap() as usize] = args[2].as_u64().unwrap() as u32;
                vec![]
            }
            "SetPulserDelay" | "SetTimerDelay" => {
                self.delays[args[1].as_u64().unwrap() as usize] = args[2].as_u64().unwrap() as u32;
                vec![]
            }
            "GetPulserBurst"
            | "GetTimerBurst"
            | "GetPulserConfig"
            | "GetSwitchTriggerConfig"
            | "GetSwitchEnableConfig"
            | "GetSwitchEnableDelay" => vec![json!(0)],
            "GetSwitchTriggerMappingEnable"
            | "GetSwitchEnableMappingEnable"
            | "DevicePurge"
            | "GetBufferState"
            | "GetInterlockFunct" => vec![json!(false)],
            "GetSwitchTriggerDelay" | "GetSwitchDelay" => vec![json!(2), json!(3)],
            "GetSWVersion" => {
                assert!(args.is_empty());
                return Ok(NativeReply {
                    status: 0x1234,
                    values: vec![],
                });
            }
            "GetClockCount" | "GetPllCount" | "GetDividerCount" | "GetCounterCount"
            | "GetOscillatorCount" => vec![json!(4)],
            "GetManufDate" => vec![json!(2026), json!(10)],
            "GetCPU_ID" | "GetDevType" | "GetIOState" | "GetCommError" | "GetFanSpeed" => {
                vec![json!(7)]
            }
            "GetErrorMessage"
            | "GetIOErrorMessage"
            | "GetIOStateMessage"
            | "GetCommErrorMessage" => vec![json!("mock diagnostic")],
            "GetSignalValues"
            | "GetSignals"
            | "GetCurrentConfig"
            | "GetDefaults"
            | "GetConfigData"
            | "GetMappingEngineInputSource" => {
                let size = match name {
                    "GetSignalValues" => 64,
                    "GetSignals" => 8,
                    "GetDefaults" => 20,
                    "GetMappingEngineInputSource" => 4,
                    _ => 318,
                };
                assert_eq!(args.last().unwrap().as_array().unwrap().len(), size);
                vec![args.last().unwrap().clone()]
            }
            "GetMappingEngineOutputValue" => {
                assert_eq!(args[3].as_array().unwrap().len(), 7);
                vec![json!(false), json!(vec![0; 7])]
            }
            "SetCurrentConfig"
            | "SetDefaults"
            | "SetConfigData"
            | "SetConfigName"
            | "SetMappingEngineInputSource"
            | "SetMappingEngineOutputValue" => vec![
                args.last()
                    .unwrap()
                    .get("text")
                    .unwrap_or(args.last().unwrap())
                    .clone(),
            ],
            "SetControllerConfig"
            | "SetConfig"
            | "SetInterlockFunct"
            | "SetSwitchTriggerDelay"
            | "SetSwitchEnableDelay"
            | "SetSwitchDelay"
            | "SetConfigFlags"
            | "SetPulserBurst" => vec![],
            _ if name.starts_with("Get") => {
                let key = (name.to_owned(), args[1].as_u64().unwrap() as u32);
                vec![self.registers.get(&key).cloned().unwrap_or_else(|| {
                    if name == "GetPllPowerDown" {
                        json!(false)
                    } else {
                        json!(7)
                    }
                })]
            }
            _ if name.starts_with("Set") => {
                let key = (
                    format!("Get{}", &name[3..]),
                    args[1].as_u64().unwrap() as u32,
                );
                self.registers.insert(key, args[2].clone());
                vec![]
            }
            _ => return Err(Error::unsupported(format!("Unmocked symbol {name}"))),
        };
        Ok(NativeReply { status: 0, values })
    }
}

fn suffix(symbol: &str) -> &str {
    symbol
        .strip_prefix("COM_HVAMX4EDH_")
        .or_else(|| symbol.strip_prefix("COM_HVAMX4ED_"))
        .unwrap()
}

fn validate_abi(hd: bool, symbol: &str, args: &[Value]) -> Vec<Value> {
    static ABI: OnceLock<Value> = OnceLock::new();
    let abi = ABI
        .get_or_init(|| serde_json::from_str(include_str!("../src/generated/abi.json")).unwrap());
    let spec = &abi[if hd { "amx_hd" } else { "amx" }]["exports"][symbol];
    assert!(!spec.is_null(), "{symbol} is not in the generated SDK ABI");
    let parameters = spec["args"].as_array().unwrap();
    assert_eq!(args.len(), parameters.len(), "{symbol} arity");
    let mut outputs = Vec::new();
    for (input, parameter) in args.iter().zip(parameters) {
        let pointer = parameter["pointer"].as_bool().unwrap();
        let size = parameter["length"].as_u64().unwrap() as usize;
        let kind = parameter["type"].as_str().unwrap();
        macro_rules! numeric {
            ($typ:ty, $boolean:expr) => {
                if pointer {
                    ffi::pointer::<$typ>(input, size, $boolean).unwrap();
                } else {
                    ffi::scalar::<$typ>(input, $boolean).unwrap();
                }
            };
        }
        match kind {
            "WORD" => numeric!(u16, false),
            "BYTE" => numeric!(u8, false),
            "DWORD" | "unsigned" => numeric!(u32, false),
            "int" => numeric!(i32, false),
            "BOOL" => numeric!(i32, true),
            "bool" => numeric!(u8, true),
            "double" => numeric!(f64, false),
            "float" => numeric!(f32, false),
            "char" => {
                ffi::text_buffer(input, size).unwrap();
            }
            _ => panic!("Unsupported ABI type {kind}"),
        }
        if pointer {
            outputs.push(if kind == "char" {
                input["text"].clone()
            } else {
                input.clone()
            });
        }
    }
    outputs
}

struct MockDll(Arc<Mutex<Instrument>>);
impl Dll for MockDll {
    fn call(&mut self, symbol: &str, args: &[Value]) -> Result<NativeReply> {
        self.0.lock().unwrap().dispatch(symbol, args)
    }
}

fn case(hd: bool) -> (Box<dyn Backend>, Arc<Mutex<Instrument>>) {
    let state = Arc::new(Mutex::new(Instrument::new(hd)));
    let dll = Box::new(MockDll(state.clone()));
    let config = json!({"device_id": "test_amx", "com": 7, "port": 3, "stream": 3});
    let controller: Box<dyn Backend> = if hd {
        Box::new(amx_hd::Controller::new(&config, dll).unwrap())
    } else {
        Box::new(amx::Controller::new(&config, dll).unwrap())
    };
    (controller, state)
}

fn rpc(device: &mut dyn Backend, method: &str, args: &[Value], kwargs: Value) -> Result<Value> {
    device.call(method, args, &kwargs, &Context::test(2.0))
}
fn connect(device: &mut dyn Backend) {
    assert_eq!(rpc(device, "connect", &[], json!({})).unwrap(), true);
}

#[test]
fn batch_call_counts_define_outer_deadlines_independently_of_per_dll_watchdogs() {
    for hd in [false, true] {
        let (mut device, state) = case(hd);
        connect(device.as_mut());
        assert_eq!(state.lock().unwrap().calls.len(), 3);
        let limit = if hd { 500 } else { 126 };
        {
            let mut state = state.lock().unwrap();
            for index in 0..limit {
                state.add_config(index, if index == 0 { "Standby" } else { "Stored" }, false);
            }
            state.calls.clear();
        }
        let configs = rpc(
            device.as_mut(),
            "list_configs",
            &[],
            json!({"include_empty": true}),
        )
        .unwrap();
        assert_eq!(configs.as_array().unwrap().len(), limit as usize);
        assert_eq!(state.lock().unwrap().calls.len(), (limit + 1) as usize);
        state.lock().unwrap().calls.clear();
        rpc(device.as_mut(), "get_product_info", &[], json!({})).unwrap();
        assert_eq!(state.lock().unwrap().calls.len(), 6);
        for timers in [2, 4] {
            {
                let mut state = state.lock().unwrap();
                state.calls.clear();
                state.timer_count = timers;
            }
            rpc(device.as_mut(), "collect_housekeeping", &[], json!({})).unwrap();
            assert_eq!(
                state.lock().unwrap().calls.len(),
                if hd { 6 + 3 * timers as usize } else { 46 }
            );
        }
        state.lock().unwrap().calls.clear();
        rpc(device.as_mut(), "disconnect", &[], json!({})).unwrap();
        state.lock().unwrap().calls.clear();
        rpc(device.as_mut(), "initialize", &[], json!({})).unwrap();
        assert_eq!(state.lock().unwrap().calls.len(), (limit + 7) as usize);
        rpc(device.as_mut(), "disconnect", &[], json!({})).unwrap();
        state.lock().unwrap().calls.clear();
        rpc(
            device.as_mut(),
            "initialize",
            &[],
            json!({"standby_config": 0, "operating_config": 12}),
        )
        .unwrap();
        assert_eq!(state.lock().unwrap().calls.len(), 8);
    }
}

#[test]
fn per_dll_watchdog_expires_before_the_long_batch_deadline_and_preserves_its_telemetry() {
    for hd in [false, true] {
        let (mut device, state) = case(hd);
        state.lock().unwrap().delay_on = Some("Open".into());
        let (sender, receiver) = std::sync::mpsc::sync_channel(16);
        let ctx = Context::new(
            std::time::Duration::from_secs(2),
            Arc::new(std::sync::atomic::AtomicBool::new(false)),
            Some(sender),
        )
        .with_request_id(42)
        .with_native_timeout(std::time::Duration::from_millis(10));
        let error = device.call("connect", &[], &json!({}), &ctx).unwrap_err();
        assert_eq!(error.kind, "TimeoutError");
        assert!(error.message.contains("native I/O deadline"));
        assert!(!ctx.remaining().is_zero());
        assert_eq!(state.lock().unwrap().names(), ["Open"]);
        assert_eq!(device.get_attribute("_transport_poisoned").unwrap(), true);
        let messages: Vec<_> = receiver.try_iter().collect();
        assert_eq!(messages.len(), 2);
        assert_eq!(messages[0]["id"], 42);
        assert_eq!(messages[0]["kind"], "native_io");
        assert_eq!(messages[0]["phase"], "enter");
        assert_eq!(messages[1]["phase"], "exit");
        assert_eq!(messages[0]["sequence"], messages[1]["sequence"]);
        assert_eq!(messages[0]["symbol"], messages[1]["symbol"]);
        assert!(messages[0]["symbol"].as_str().unwrap().ends_with("_Open"));
        assert_eq!(messages[0]["timeout_s"], 0.01);
    }
}

#[test]
fn cancellation_cleanup_keeps_request_watchdogs_but_uses_a_fresh_uncancelled_token() {
    for hd in [false, true] {
        let (mut device, state) = case(hd);
        connect(device.as_mut());
        state.lock().unwrap().calls.clear();
        let (sender, receiver) = std::sync::mpsc::sync_channel(16);
        let ctx = Context::new(
            std::time::Duration::from_secs(2),
            Arc::new(std::sync::atomic::AtomicBool::new(false)),
            Some(sender),
        )
        .with_request_id(43)
        .with_native_timeout(std::time::Duration::from_millis(200));
        state.lock().unwrap().cancel_on = Some(("SetDeviceEnable".into(), ctx.clone()));
        assert!(
            device
                .call("set_device_enabled", &[json!(true)], &json!({}), &ctx)
                .is_err()
        );
        assert!(ctx.is_cancelled());
        assert!(!state.lock().unwrap().enabled);
        assert_eq!(
            state.lock().unwrap().names(),
            ["SetDeviceEnable", "SetDeviceEnable", "GetDeviceEnable"]
        );
        let messages: Vec<_> = receiver.try_iter().collect();
        assert_eq!(messages.len(), 6);
        for (sequence, pair) in messages.chunks(2).enumerate() {
            assert_eq!(pair[0]["id"], 43);
            assert_eq!(pair[1]["id"], 43);
            assert_eq!(pair[0]["sequence"], sequence);
            assert_eq!(pair[1]["sequence"], sequence);
            assert_eq!(pair[0]["timeout_s"], 0.2);
        }
    }
}

#[test]
fn constructors_validate_addresses_and_rates() {
    for hd in [false, true] {
        for config in [
            json!({}),
            json!({"com": 0}),
            json!({"com": 65536}),
            json!({"com": 7, "baudrate": 0}),
            json!({"com": 7, "port": 16, "stream": 16}),
            json!({"com": 7, "port": -1, "stream": -1}),
        ] {
            let dll = Box::new(MockDll(Arc::new(Mutex::new(Instrument::new(hd)))));
            let result = if hd {
                amx_hd::Controller::new(&config, dll).map(|_| ())
            } else {
                amx::Controller::new(&config, dll).map(|_| ())
            };
            assert!(result.is_err(), "{config}");
        }
    }
}

#[test]
fn connect_is_transport_only_idempotent_and_resume_reads_never_write_timing() {
    for hd in [false, true] {
        let (mut device, state) = case(hd);
        connect(device.as_mut());
        connect(device.as_mut());
        assert_eq!(
            state.lock().unwrap().names(),
            ["Open", "SetBaudRate", "GetProductID"]
        );
        rpc(device.as_mut(), "collect_housekeeping", &[], json!({})).unwrap();
        let state = state.lock().unwrap();
        assert!(
            !state
                .names()
                .iter()
                .any(|n| n == "LoadCurrentConfig" || (n.starts_with("Set") && n != "SetBaudRate"))
        );
        assert_eq!(device.get_attribute("connected").unwrap(), true);
        assert_eq!(device.get_attribute("_dll_port_claimed").unwrap(), true);
    }
}

#[test]
fn failed_open_and_baud_failures_rollback_and_can_reconnect() {
    for hd in [false, true] {
        for failing in ["Open", "SetBaudRate", "GetProductID"] {
            let (mut device, state) = case(hd);
            state.lock().unwrap().inject(failing, -2, vec![]);
            assert!(rpc(device.as_mut(), "connect", &[], json!({})).is_err());
            assert_eq!(device.get_attribute("connected").unwrap(), false);
            assert_eq!(device.get_attribute("_dll_port_claimed").unwrap(), false);
            assert_eq!(device.get_attribute("_failed_open_released").unwrap(), true);
            assert_eq!(state.lock().unwrap().names().last().unwrap(), "Close");
            connect(device.as_mut());
        }
    }
}

#[test]
fn failed_close_keeps_the_claim_and_requires_explicit_cleanup_before_reconnect() {
    for hd in [false, true] {
        let (mut device, state) = case(hd);
        state.lock().unwrap().inject("SetBaudRate", -16, vec![]);
        state.lock().unwrap().inject("Close", -3, vec![]);
        let error = rpc(device.as_mut(), "connect", &[], json!({})).unwrap_err();
        assert!(error.message.contains("cleanup failed"));
        let count = state.lock().unwrap().calls.len();
        assert!(rpc(device.as_mut(), "connect", &[], json!({})).is_err());
        assert_eq!(state.lock().unwrap().calls.len(), count);
        assert_eq!(
            rpc(device.as_mut(), "disconnect", &[], json!({})).unwrap(),
            true
        );
        connect(device.as_mut());
    }
}

#[test]
fn poisoned_dll_is_never_reloaded_reopened_or_used_for_cleanup() {
    for hd in [false, true] {
        let (mut device, state) = case(hd);
        state.lock().unwrap().fail("SetBaudRate", "TimeoutError");
        assert!(rpc(device.as_mut(), "connect", &[], json!({})).is_err());
        let count = state.lock().unwrap().calls.len();
        assert_eq!(device.get_attribute("_transport_poisoned").unwrap(), true);
        assert_eq!(device.get_attribute("_dll_port_claimed").unwrap(), true);
        assert!(rpc(device.as_mut(), "connect", &[], json!({})).is_err());
        assert_eq!(
            rpc(device.as_mut(), "disconnect", &[], json!({})).unwrap(),
            false
        );
        assert!(rpc(device.as_mut(), "shutdown", &[], json!({})).is_err());
        assert_eq!(state.lock().unwrap().calls.len(), count);
    }
}

#[test]
fn initialize_autoselects_only_an_exact_or_unique_valid_standby() {
    for hd in [false, true] {
        for (names, expected) in [
            (vec!["STANDBY", "Other Standby"], Some(3)),
            (vec!["Safe Standby"], Some(3)),
            (vec!["Standby A", "Standby B"], None),
            (vec!["Run"], None),
        ] {
            let (mut device, state) = case(hd);
            for (index, name) in names.iter().enumerate() {
                state
                    .lock()
                    .unwrap()
                    .add_config(index as u32 + 3, name, false);
            }
            let result = rpc(device.as_mut(), "initialize", &[], json!({})).unwrap();
            assert_eq!(
                result.get("standby_config").and_then(Value::as_u64),
                expected
            );
            if expected.is_some() {
                assert_eq!(result["memory_config_source"], "auto-standby");
            }
            assert!(
                !state
                    .lock()
                    .unwrap()
                    .names()
                    .iter()
                    .any(|n| n == "SetDeviceEnable")
            );
        }
    }
}

#[test]
fn initialize_standby_interlock_refuses_operating_load_and_confirms_cleanup() {
    for hd in [false, true] {
        let (mut device, state) = case(hd);
        state.lock().unwrap().add_config(3, "Unsafe standby", true);
        state.lock().unwrap().add_config(12, "Operating", true);
        let error = rpc(
            device.as_mut(),
            "initialize",
            &[],
            json!({"standby_config": 3, "operating_config": 12}),
        )
        .unwrap_err();
        assert!(
            error
                .message
                .contains("standby configuration left the device enabled")
        );
        let state = state.lock().unwrap();
        let loads: Vec<_> = state
            .calls
            .iter()
            .filter(|(s, _)| suffix(s) == "LoadCurrentConfig")
            .map(|(_, a)| a[1].clone())
            .collect();
        assert_eq!(loads, [json!(3)]);
        assert!(!state.enabled);
        assert_eq!(state.names().last().unwrap(), "Close");
        assert_eq!(device.get_attribute("connected").unwrap(), false);
    }
}

#[test]
fn initialize_preserves_explicit_config_order_and_provenance_without_timing_writes() {
    for hd in [false, true] {
        let (mut device, state) = case(hd);
        state.lock().unwrap().add_config(3, "Standby", false);
        state
            .lock()
            .unwrap()
            .add_config(12, "Stored waveform", true);
        let result = rpc(
            device.as_mut(),
            "initialize",
            &[],
            json!({"standby_config": 3, "operating_config": 12}),
        )
        .unwrap();
        assert_eq!(result["memory_config"], 12);
        assert_eq!(result["memory_config_name"], "Stored waveform");
        assert_eq!(result["memory_config_source"], "operating");
        assert_eq!(state.lock().unwrap().period, 798);
        assert!(
            !state
                .lock()
                .unwrap()
                .names()
                .iter()
                .any(|n| n == "SetOscillatorPeriod"
                    || n == "SetTimerWidth"
                    || n == "SetPulserWidth")
        );
    }
}

#[test]
fn optional_config_name_failure_clears_stale_name_and_keeps_loaded_slot() {
    for hd in [false, true] {
        let (mut device, state) = case(hd);
        connect(device.as_mut());
        state.lock().unwrap().add_config(3, "Old name", false);
        rpc(device.as_mut(), "load_config", &[json!(3)], json!({})).unwrap();
        state.lock().unwrap().fail("GetConfigName", "RuntimeError");
        rpc(device.as_mut(), "load_config", &[json!(12)], json!({})).unwrap();
        let status = rpc(device.as_mut(), "get_status", &[], json!({})).unwrap();
        assert_eq!(status["memory_config"], 12);
        assert_eq!(status["memory_config_name"], Value::Null);
        state
            .lock()
            .unwrap()
            .inject("LoadCurrentConfig", -201, vec![]);
        assert!(rpc(device.as_mut(), "load_config", &[json!(13)], json!({})).is_err());
        assert_eq!(device.get_attribute("loaded_config_number").unwrap(), 12);
    }
}

#[test]
fn list_configs_filters_empty_but_retains_active_invalid_and_valid_inactive_entries() {
    for hd in [false, true] {
        let (mut device, state) = case(hd);
        connect(device.as_mut());
        state.lock().unwrap().add_config(3, "inactive", false);
        state.lock().unwrap().configs.get_mut(&3).unwrap().active = false;
        state.lock().unwrap().add_config(12, "invalid", false);
        state.lock().unwrap().configs.get_mut(&12).unwrap().valid = false;
        let configs = rpc(device.as_mut(), "list_configs", &[], json!({})).unwrap();
        assert_eq!(configs.as_array().unwrap().len(), 2);
        assert_eq!(configs[0]["active"], false);
        assert_eq!(configs[1]["valid"], false);
        let configs = rpc(
            device.as_mut(),
            "list_configs",
            &[],
            json!({"include_empty": true}),
        )
        .unwrap();
        assert_eq!(
            configs.as_array().unwrap().len(),
            if hd { 500 } else { 126 }
        );
    }
}

#[test]
fn malformed_config_arrays_are_not_treated_as_empty_valid_configuration_lists() {
    let (mut device, state) = case(false);
    connect(device.as_mut());
    state
        .lock()
        .unwrap()
        .inject("GetConfigList", 0, vec![json!([false]), json!([false])]);
    assert!(rpc(device.as_mut(), "list_configs", &[], json!({})).is_err());
}

#[test]
fn shutdown_requires_confirmed_disable_before_close_and_clears_provenance() {
    for hd in [false, true] {
        let (mut device, state) = case(hd);
        connect(device.as_mut());
        state.lock().unwrap().enabled = true;
        state.lock().unwrap().calls.clear();
        assert_eq!(
            rpc(device.as_mut(), "shutdown", &[], json!({})).unwrap(),
            true
        );
        assert_eq!(
            state.lock().unwrap().names(),
            ["SetDeviceEnable", "GetDeviceEnable", "Close"]
        );
        assert_eq!(device.get_attribute("connected").unwrap(), false);
        assert_eq!(
            device.get_attribute("loaded_config_number").unwrap(),
            Value::Null
        );
        assert_eq!(
            rpc(device.as_mut(), "shutdown", &[], json!({})).unwrap(),
            false
        );
    }
}

#[test]
fn standby_shutdown_verifies_the_load_and_disables_an_enabled_standby() {
    for hd in [false, true] {
        for enabled in [false, true] {
            let (mut device, state) = case(hd);
            connect(device.as_mut());
            state.lock().unwrap().add_config(3, "Standby", enabled);
            state.lock().unwrap().calls.clear();
            assert_eq!(
                rpc(
                    device.as_mut(),
                    "shutdown",
                    &[],
                    json!({"standby_config": 3, "disable_device": false})
                )
                .unwrap(),
                true
            );
            assert_eq!(
                state
                    .lock()
                    .unwrap()
                    .names()
                    .iter()
                    .any(|n| n == "SetDeviceEnable"),
                enabled
            );
            assert!(!state.lock().unwrap().enabled);
        }
    }
}

#[test]
fn shutdown_errors_keep_the_link_even_when_fallback_disable_succeeded() {
    for hd in [false, true] {
        for failure in ["LoadCurrentConfig", "SetDeviceEnable", "GetDeviceEnable"] {
            let (mut device, state) = case(hd);
            connect(device.as_mut());
            state.lock().unwrap().enabled = true;
            state.lock().unwrap().calls.clear();
            state.lock().unwrap().inject(failure, -15, vec![]);
            let kwargs = if failure == "LoadCurrentConfig" {
                json!({"standby_config": 3, "disable_device": false})
            } else {
                json!({})
            };
            assert!(rpc(device.as_mut(), "shutdown", &[], kwargs).is_err());
            assert!(!state.lock().unwrap().names().iter().any(|n| n == "Close"));
            assert_eq!(device.get_attribute("connected").unwrap(), true);
            assert_eq!(
                rpc(device.as_mut(), "shutdown", &[], json!({})).unwrap(),
                true
            );
        }
    }
}

#[test]
fn ineffective_disable_readback_and_close_failure_do_not_report_success() {
    for hd in [false, true] {
        let (mut device, state) = case(hd);
        connect(device.as_mut());
        state.lock().unwrap().enabled = true;
        state.lock().unwrap().sticky_enabled = true;
        let error = rpc(device.as_mut(), "shutdown", &[], json!({})).unwrap_err();
        assert!(error.message.contains("remained enabled"));
        state.lock().unwrap().sticky_enabled = false;
        state.lock().unwrap().inject("Close", -3, vec![]);
        assert!(rpc(device.as_mut(), "shutdown", &[], json!({})).is_err());
        assert_eq!(device.get_attribute("connected").unwrap(), true);
        assert_eq!(device.get_attribute("_dll_port_claimed").unwrap(), true);
    }
}

#[test]
fn frequency_and_duty_use_python_ties_to_even_and_exact_offsets() {
    for hd in [false, true] {
        let (mut device, state) = case(hd);
        connect(device.as_mut());
        rpc(
            device.as_mut(),
            "set_frequency_hz",
            &[json!(8_000_000.)],
            json!({}),
        )
        .unwrap();
        assert_eq!(state.lock().unwrap().period, 10);
        assert_eq!(
            rpc(device.as_mut(), "get_frequency_hz", &[], json!({})).unwrap(),
            json!(100e6 / 12.)
        );
        rpc(
            device.as_mut(),
            "set_frequency_khz",
            &[json!(500.)],
            json!({}),
        )
        .unwrap();
        assert_eq!(state.lock().unwrap().period, 198);
        state.lock().unwrap().period = 8;
        rpc(
            device.as_mut(),
            "set_pulser_duty_cycle",
            &[json!(0), json!(0.45)],
            json!({}),
        )
        .unwrap();
        assert_eq!(state.lock().unwrap().widths[0], 2);
        rpc(
            device.as_mut(),
            "set_pulser_width_ticks",
            &[json!(0), json!(0)],
            json!({}),
        )
        .unwrap();
        assert_eq!(state.lock().unwrap().widths[0], 0);
        assert_eq!(
            rpc(
                device.as_mut(),
                "get_pulser_width_seconds",
                &[json!(0)],
                json!({})
            )
            .unwrap(),
            json!(2e-8)
        );
        assert_eq!(
            rpc(
                device.as_mut(),
                "get_pulser_delay_seconds",
                &[json!(0)],
                json!({})
            )
            .unwrap(),
            json!(1e-7)
        );
    }
}

#[test]
fn invalid_frequencies_duties_registers_and_indices_never_write() {
    for hd in [false, true] {
        let (mut device, state) = case(hd);
        connect(device.as_mut());
        for (method, args) in [
            ("set_frequency_hz", vec![json!(0)]),
            ("set_frequency_hz", vec![json!(-1)]),
            ("set_frequency_hz", vec![json!(40e6)]),
            ("set_frequency_hz", vec![json!(1e-9)]),
            ("set_frequency_hz", vec![json!({"$float": "nan"})]),
            ("set_pulser_duty_cycle", vec![json!(0), json!(0)]),
            ("set_pulser_duty_cycle", vec![json!(0), json!(1.1)]),
            ("set_pulser_duty_cycle", vec![json!(0), json!(1e-9)]),
            ("set_pulser_width_ticks", vec![json!(0), json!(-1)]),
            (
                "set_pulser_width_ticks",
                vec![json!(0), json!(4294967296u64)],
            ),
            ("set_pulser_width_ticks", vec![json!(4), json!(1)]),
            ("set_pulser_delay_ticks", vec![json!(-1), json!(0)]),
        ] {
            let error = rpc(device.as_mut(), method, &args, json!({})).unwrap_err();
            assert_eq!(error.kind, "ValueError", "{method}: {error}");
        }
        assert!(
            !state
                .lock()
                .unwrap()
                .names()
                .iter()
                .any(|n| n.starts_with("Set") && n != "SetBaudRate")
        );
    }
}

#[test]
fn classic_switch_delays_are_four_bit_and_burst_pulsers_are_only_zero_and_one() {
    let (mut device, state) = case(false);
    connect(device.as_mut());
    rpc(
        device.as_mut(),
        "set_switch_trigger_delay",
        &[json!(3), json!(15), json!(0)],
        json!({}),
    )
    .unwrap();
    assert_eq!(
        rpc(
            device.as_mut(),
            "get_switch_trigger_delay",
            &[json!(3)],
            json!({})
        )
        .unwrap(),
        codec::tuple(vec![json!(2), json!(3)])
    );
    for (method, args) in [
        (
            "set_switch_trigger_delay",
            vec![json!(0), json!(16), json!(0)],
        ),
        ("set_switch_enable_delay", vec![json!(4), json!(0)]),
        ("set_switch_enable_delay", vec![json!(0), json!(-1)]),
        ("set_pulser_burst", vec![json!(2), json!(0)]),
        ("get_pulser_config", vec![json!(6)]),
    ] {
        assert!(rpc(device.as_mut(), method, &args, json!({})).is_err());
    }
    rpc(
        device.as_mut(),
        "set_controller_config",
        &[json!(0xffff)],
        json!({}),
    )
    .unwrap();
    assert_eq!(state.lock().unwrap().calls.last().unwrap().1[1], 255);
}

#[test]
fn classic_snapshot_preserves_unknown_bits_nonfinite_telemetry_and_routing() {
    let (mut device, state) = case(false);
    connect(device.as_mut());
    state.lock().unwrap().device_bits = 4;
    state.lock().unwrap().controller_bits = Some(0x8000);
    state.lock().unwrap().main_state = Some(0x1234);
    state.lock().unwrap().nonfinite = true;
    let snapshot = rpc(device.as_mut(), "collect_housekeeping", &[], json!({})).unwrap();
    assert_eq!(snapshot["main_state"]["name"], "UNKNOWN_STATE_0x1234");
    assert_eq!(
        snapshot["device_state"]["flags"],
        json!(["UNKNOWN_BITS_0x00000004"])
    );
    assert_eq!(
        snapshot["controller_state"]["flags"],
        json!(["UNKNOWN_BITS_0x8000"])
    );
    assert_eq!(
        snapshot["housekeeping"]["temp_cpu_c"],
        codec::float(f64::NAN)
    );
    assert_eq!(snapshot["pulsers"].as_array().unwrap().len(), 4);
    assert_eq!(snapshot["switches"].as_array().unwrap().len(), 4);
    assert_eq!(snapshot["pulsers"][2]["burst"], Value::Null);
    let configs: Vec<_> = state
        .lock()
        .unwrap()
        .calls
        .iter()
        .filter(|(s, _)| suffix(s) == "GetPulserConfig")
        .map(|(_, a)| a[1].clone())
        .collect();
    assert_eq!(
        configs,
        [json!(0), json!(1), json!(2), json!(3), json!(4), json!(5)]
    );
}

#[test]
fn only_missing_routing_exports_become_unknown_not_failed_vendor_reads() {
    let (mut device, state) = case(false);
    connect(device.as_mut());
    state
        .lock()
        .unwrap()
        .fail("GetSwitchTriggerMappingEnable", "AttributeError");
    let snapshot = rpc(device.as_mut(), "collect_housekeeping", &[], json!({})).unwrap();
    assert_eq!(snapshot["switch_mapping"]["trigger_enabled"], Value::Null);
    state
        .lock()
        .unwrap()
        .inject("GetSwitchTriggerMappingEnable", -15, vec![json!(false)]);
    assert!(rpc(device.as_mut(), "collect_housekeeping", &[], json!({})).is_err());
}

#[test]
fn hd_lightweight_snapshot_uses_three_calls_and_its_distinct_state_encoding() {
    let (mut device, state) = case(true);
    connect(device.as_mut());
    state.lock().unwrap().calls.clear();
    let standby = rpc(device.as_mut(), "collect_state_snapshot", &[], json!({})).unwrap();
    assert_eq!(standby["main_state"]["name"], "STATE_STANDBY");
    assert_eq!(
        state.lock().unwrap().names(),
        ["GetDeviceState", "GetState", "GetDeviceEnable"]
    );
    state.lock().unwrap().enabled = true;
    let running = rpc(device.as_mut(), "collect_state_snapshot", &[], json!({})).unwrap();
    assert_eq!(running["main_state"]["name"], "STATE_ON");
    assert!(!running.as_object().unwrap().contains_key("oscillator"));
    assert_eq!(
        running["controller_state"]["flags"],
        json!(["ST_ENABLE", "ST_ENB_TIMER", "ST_ENB_OSC0", "ST_CLRN"])
    );
}

#[test]
fn hd_snapshot_and_product_info_use_real_module_counts_and_richer_readbacks() {
    let (mut device, state) = case(true);
    connect(device.as_mut());
    state
        .lock()
        .unwrap()
        .inject("GetTimerBurst", -15, vec![json!(0)]);
    let snapshot = rpc(device.as_mut(), "collect_housekeeping", &[], json!({})).unwrap();
    assert_eq!(snapshot["pulsers"].as_array().unwrap().len(), 2);
    assert_eq!(snapshot["pulsers"][0]["burst"], Value::Null);
    assert_eq!(snapshot["housekeeping"]["volt_fans_v"], 11.5);
    assert_eq!(snapshot["housekeeping"]["volt_2v5p_v"], 2.51);
    assert!(!snapshot.as_object().unwrap().contains_key("switches"));
    let info = rpc(device.as_mut(), "get_product_info", &[], json!({})).unwrap();
    assert_eq!(info["hardware"]["fpga_version"], 6);
    assert_eq!(info["hardware"]["modules"]["timer"], 2);
    assert_eq!(info["hardware"]["modules"]["signal"], 64);
}

#[test]
fn hd_switch_timing_sources_pll_and_interlock_operations_are_real_validated_calls() {
    let (mut device, state) = case(true);
    connect(device.as_mut());
    assert_eq!(
        rpc(
            device.as_mut(),
            "set_switch_delay",
            &[json!(0), json!(15), json!(3)],
            json!({})
        )
        .unwrap(),
        0
    );
    rpc(
        device.as_mut(),
        "set_switch_rise_delay_fine",
        &[json!(0), json!(511)],
        json!({}),
    )
    .unwrap();
    assert_eq!(
        rpc(
            device.as_mut(),
            "get_switch_rise_delay_fine",
            &[json!(0)],
            json!({})
        )
        .unwrap(),
        codec::tuple(vec![json!(0), json!(511)])
    );
    rpc(
        device.as_mut(),
        "set_timer_trigger_source",
        &[json!(0), json!(130)],
        json!({}),
    )
    .unwrap();
    rpc(
        device.as_mut(),
        "set_pll_power_down",
        &[json!(1), json!(true)],
        json!({}),
    )
    .unwrap();
    assert_eq!(
        rpc(
            device.as_mut(),
            "get_pll_power_down",
            &[json!(1)],
            json!({})
        )
        .unwrap(),
        codec::tuple(vec![json!(0), json!(true)])
    );
    rpc(
        device.as_mut(),
        "set_interlock_funct",
        &[json!(true)],
        json!({}),
    )
    .unwrap();
    assert_eq!(
        state.lock().unwrap().names().last().unwrap(),
        "SetInterlockFunct"
    );
    for (method, args) in [
        ("set_switch_delay", vec![json!(0), json!(16), json!(0)]),
        ("set_switch_fall_delay_fine", vec![json!(0), json!(512)]),
        ("set_timer_trigger_source", vec![json!(0), json!(64)]),
        ("set_timer_burst", vec![json!(0), json!(1 << 24)]),
        ("set_pll_ref_div", vec![json!(0), json!(0)]),
        ("set_pll_fdbk_div", vec![json!(0), json!(6)]),
    ] {
        assert!(
            rpc(device.as_mut(), method, &args, json!({})).is_err(),
            "{method}"
        );
    }
}

#[test]
fn hd_fixed_buffers_and_names_are_exact_not_silently_padded_or_truncated() {
    let (mut device, state) = case(true);
    connect(device.as_mut());
    for (method, size) in [("set_current_config", 318), ("set_defaults", 20)] {
        for data in [
            json!(vec![0; size - 1]),
            json!(vec![0; size + 1]),
            json!(vec![256; size]),
        ] {
            assert!(rpc(device.as_mut(), method, &[data], json!({})).is_err());
        }
        assert_eq!(
            rpc(
                device.as_mut(),
                method,
                &[json!(vec![255; size])],
                json!({})
            )
            .unwrap(),
            0
        );
    }
    rpc(
        device.as_mut(),
        "set_config_name",
        &[json!(499), json!("Stored name")],
        json!({}),
    )
    .unwrap();
    assert!(
        rpc(
            device.as_mut(),
            "set_config_name",
            &[json!(500), json!("name")],
            json!({})
        )
        .is_err()
    );
    assert!(
        rpc(
            device.as_mut(),
            "set_config_name",
            &[json!(499), json!("a".repeat(193))],
            json!({})
        )
        .is_err()
    );
    assert!(
        rpc(
            device.as_mut(),
            "set_config_name",
            &[json!(499), json!("a\0b")],
            json!({})
        )
        .is_err()
    );
    rpc(
        device.as_mut(),
        "set_mapping_engine_output_value",
        &[json!(0), json!(true), json!(vec![0; 7])],
        json!({}),
    )
    .unwrap();
    assert!(
        rpc(
            device.as_mut(),
            "set_mapping_engine_output_value",
            &[json!(0), json!(true), json!(vec![0; 6])],
            json!({})
        )
        .is_err()
    );
    let names = state.lock().unwrap().names();
    assert!(names.contains(&"SetConfigName".to_owned()));
}

#[test]
fn hd_diagnostics_return_scalar_strings_and_global_helpers_do_not_prepend_stream() {
    let (mut device, state) = case(true);
    assert_eq!(
        rpc(device.as_mut(), "get_sw_version", &[], json!({})).unwrap(),
        0x1234
    );
    assert_eq!(
        rpc(
            device.as_mut(),
            "get_io_state_message",
            &[json!(-1)],
            json!({})
        )
        .unwrap(),
        "mock diagnostic"
    );
    assert_eq!(state.lock().unwrap().calls.last().unwrap().1, [json!(-1)]);
    assert_eq!(
        rpc(
            device.as_mut(),
            "get_comm_error_message",
            &[json!(u32::MAX)],
            json!({})
        )
        .unwrap(),
        "mock diagnostic"
    );
    assert_eq!(
        state.lock().unwrap().calls.last().unwrap().1,
        [json!(u32::MAX)]
    );
}

#[test]
fn cancellation_after_activation_load_or_width_write_attempts_and_confirms_off() {
    for hd in [false, true] {
        for (method, args, export) in [
            ("set_device_enabled", vec![json!(true)], "SetDeviceEnable"),
            ("load_config", vec![json!(12)], "LoadCurrentConfig"),
            (
                "set_pulser_width_ticks",
                vec![json!(0), json!(98)],
                if hd {
                    "SetTimerWidth"
                } else {
                    "SetPulserWidth"
                },
            ),
        ] {
            let (mut device, state) = case(hd);
            connect(device.as_mut());
            state.lock().unwrap().add_config(12, "Operating", true);
            let ctx = Context::test(2.);
            state.lock().unwrap().cancel_on = Some((export.into(), ctx.clone()));
            let error = device.call(method, &args, &json!({}), &ctx).unwrap_err();
            assert!(error.message.contains("cancelled"));
            assert!(!state.lock().unwrap().enabled);
            assert_eq!(
                state.lock().unwrap().names().last().unwrap(),
                "GetDeviceEnable"
            );
            assert_eq!(device.get_attribute("connected").unwrap(), true);
        }
    }
}

#[test]
fn cancellation_cleanup_failure_is_explicit_and_does_not_disconnect() {
    for hd in [false, true] {
        let (mut device, state) = case(hd);
        connect(device.as_mut());
        state.lock().unwrap().enabled = true;
        state.lock().unwrap().sticky_enabled = true;
        let ctx = Context::test(2.);
        let export = if hd {
            "SetTimerWidth"
        } else {
            "SetPulserWidth"
        };
        state.lock().unwrap().cancel_on = Some((export.into(), ctx.clone()));
        let error = device
            .call(
                "set_pulser_width_ticks",
                &[json!(0), json!(98)],
                &json!({}),
                &ctx,
            )
            .unwrap_err();
        assert!(error.message.contains("OFF/port release unconfirmed"));
        assert!(!state.lock().unwrap().names().contains(&"Close".to_owned()));
        assert_eq!(device.get_attribute("connected").unwrap(), true);
    }
}

#[test]
fn cancellation_of_connect_releases_a_completed_open_without_output_writes() {
    for hd in [false, true] {
        let (mut device, state) = case(hd);
        let ctx = Context::test(2.);
        state.lock().unwrap().cancel_on = Some(("Open".into(), ctx.clone()));
        assert!(device.call("connect", &[], &json!({}), &ctx).is_err());
        assert_eq!(state.lock().unwrap().names(), ["Open", "Close"]);
        assert_eq!(device.get_attribute("_dll_port_claimed").unwrap(), false);
    }
}

#[test]
fn cancelled_requests_and_read_only_snapshots_do_not_modify_a_running_instrument() {
    for hd in [false, true] {
        let (mut device, state) = case(hd);
        connect(device.as_mut());
        state.lock().unwrap().enabled = true;
        let ctx = Context::test(2.);
        ctx.cancel();
        let count = state.lock().unwrap().calls.len();
        assert!(
            device
                .call("set_device_enabled", &[json!(true)], &json!({}), &ctx)
                .is_err()
        );
        assert_eq!(state.lock().unwrap().calls.len(), count);
        let ctx = Context::test(2.);
        state.lock().unwrap().cancel_on = Some(("GetDeviceState".into(), ctx.clone()));
        assert!(
            device
                .call("collect_housekeeping", &[], &json!({}), &ctx)
                .is_err()
        );
        assert!(state.lock().unwrap().enabled);
        assert!(
            !state
                .lock()
                .unwrap()
                .names()
                .contains(&"SetDeviceEnable".to_owned())
        );
    }
}

#[test]
fn public_dispatch_rejects_internal_helpers_invalid_signatures_and_spoofed_state() {
    for hd in [false, true] {
        let (mut device, state) = case(hd);
        for method in [
            "_collect_housekeeping_unlocked",
            "_set_loaded_config_state",
            "nonexistent",
        ] {
            assert_eq!(
                rpc(device.as_mut(), method, &[], json!({}))
                    .unwrap_err()
                    .kind,
                "NotImplementedError"
            );
        }
        for (method, args, kwargs) in [
            ("connect", vec![], json!({"timeout_s": 0})),
            ("connect", vec![], json!({"unexpected": true})),
            ("load_config", vec![], json!({})),
            ("load_config", vec![json!(3)], json!({"config_number": 3})),
            ("shutdown", vec![], json!({"standby_config": 3})),
            ("shutdown", vec![json!(3)], json!({})),
        ] {
            assert!(rpc(device.as_mut(), method, &args, kwargs).is_err());
        }
        assert!(state.lock().unwrap().calls.is_empty());
        assert!(device.set_attribute("connected", json!(true)).is_err());
        assert!(
            device
                .set_attribute("_dll_port_claimed", json!(false))
                .is_err()
        );
        device.set_attribute("recording", json!(true)).unwrap();
        assert_eq!(device.get_attribute("recording").unwrap(), true);
        assert_eq!(device.get_attribute("CLOCK").unwrap(), 100e6);
        assert_eq!(device.get_attribute("OSC_OFFSET").unwrap(), 2);
        assert_eq!(device.get_attribute("PULSER_DELAY_OFFSET").unwrap(), 3);
        connect(device.as_mut());
        assert!(device.set_attribute("com", json!(8)).is_err());
    }
}

#[test]
fn late_dll_write_poisons_transport_without_claiming_off_or_reusing_the_dll() {
    for hd in [false, true] {
        let (mut device, state) = case(hd);
        connect(device.as_mut());
        state.lock().unwrap().delay_on = Some("LoadCurrentConfig".into());
        state.lock().unwrap().add_config(12, "Operating", true);
        let ctx = Context::test(0.03);
        let error = device
            .call("load_config", &[json!(12)], &json!({}), &ctx)
            .unwrap_err();
        assert_eq!(error.kind, "TimeoutError");
        assert!(state.lock().unwrap().enabled);
        assert_eq!(
            state.lock().unwrap().names().last().unwrap(),
            "LoadCurrentConfig"
        );
        assert_eq!(device.get_attribute("connected").unwrap(), false);
        assert_eq!(device.get_attribute("_transport_poisoned").unwrap(), true);
        assert_eq!(device.get_attribute("_dll_port_claimed").unwrap(), true);
        assert_eq!(
            rpc(device.as_mut(), "get_status", &[], json!({})).unwrap()["memory_config"],
            Value::Null
        );
    }
}

#[test]
fn interrupted_load_invalidates_previous_provenance_and_optional_read_failures_are_not_swallowed() {
    for hd in [false, true] {
        let (mut device, state) = case(hd);
        connect(device.as_mut());
        state.lock().unwrap().add_config(3, "Standby", false);
        state.lock().unwrap().add_config(12, "Operating", true);
        rpc(device.as_mut(), "load_config", &[json!(3)], json!({})).unwrap();
        let ctx = Context::test(2.);
        state.lock().unwrap().cancel_on = Some(("LoadCurrentConfig".into(), ctx.clone()));
        assert!(
            device
                .call("load_config", &[json!(12)], &json!({}), &ctx)
                .is_err()
        );
        assert_eq!(
            device.get_attribute("loaded_config_number").unwrap(),
            Value::Null
        );
        assert_eq!(device.get_attribute("loaded_config_name").unwrap(), "");
        state.lock().unwrap().cancel_on = None;
        let ctx = Context::test(2.);
        state.lock().unwrap().cancel_on = Some(("GetConfigName".into(), ctx.clone()));
        assert!(
            device
                .call("load_config", &[json!(12)], &json!({}), &ctx)
                .is_err()
        );
        assert!(!state.lock().unwrap().enabled);
    }
}

#[test]
fn initialize_cleanup_failure_does_not_hide_the_original_interlock_failure() {
    for hd in [false, true] {
        let (mut device, state) = case(hd);
        state.lock().unwrap().add_config(3, "Unsafe standby", true);
        state.lock().unwrap().sticky_enabled = true;
        let error = rpc(
            device.as_mut(),
            "initialize",
            &[],
            json!({"standby_config": 3}),
        )
        .unwrap_err();
        assert!(
            error
                .message
                .contains("standby configuration left the device enabled")
        );
        assert!(error.message.contains("cleanup failed"));
        assert!(!state.lock().unwrap().names().contains(&"Close".to_owned()));
        assert_eq!(device.get_attribute("connected").unwrap(), true);
    }
}

#[test]
fn cancellation_during_shutdown_keeps_the_link_and_completes_a_fresh_disable_confirmation() {
    for hd in [false, true] {
        let (mut device, state) = case(hd);
        connect(device.as_mut());
        state.lock().unwrap().enabled = true;
        let ctx = Context::test(2.);
        state.lock().unwrap().cancel_on = Some(("SetDeviceEnable".into(), ctx.clone()));
        assert!(device.call("shutdown", &[], &json!({}), &ctx).is_err());
        assert!(!state.lock().unwrap().enabled);
        assert_eq!(
            state.lock().unwrap().names().last().unwrap(),
            "GetDeviceEnable"
        );
        assert!(!state.lock().unwrap().names().contains(&"Close".to_owned()));
        assert_eq!(device.get_attribute("connected").unwrap(), true);
    }
}

#[test]
fn hd_optional_burst_read_does_not_hide_cancellation_or_a_poisoned_transport() {
    let (mut device, state) = case(true);
    connect(device.as_mut());
    let ctx = Context::test(2.);
    state.lock().unwrap().cancel_on = Some(("GetTimerBurst".into(), ctx.clone()));
    assert!(
        device
            .call("collect_housekeeping", &[], &json!({}), &ctx)
            .is_err()
    );
    state.lock().unwrap().cancel_on = None;
    state
        .lock()
        .unwrap()
        .fail("GetTimerBurst", "ConnectionError");
    assert!(rpc(device.as_mut(), "collect_housekeeping", &[], json!({})).is_err());
    assert_eq!(device.get_attribute("_transport_poisoned").unwrap(), true);
}

#[test]
fn classic_low_level_facade_preserves_status_tuple_shapes_and_sdk_arguments() {
    let (mut device, _) = case(false);
    connect(device.as_mut());
    for (method, args) in [
        ("get_main_state", vec![]),
        ("get_device_state", vec![]),
        ("get_controller_state", vec![]),
        ("get_device_enable", vec![]),
        ("get_housekeeping", vec![]),
        ("get_sensor_data", vec![]),
        ("get_fan_data", vec![]),
        ("get_led_data", vec![]),
        ("get_cpu_data", vec![]),
        ("get_uptime", vec![]),
        ("get_total_time", vec![]),
        ("get_product_id", vec![]),
        ("get_product_no", vec![]),
        ("get_fw_date", vec![]),
        ("get_fw_version", vec![]),
        ("get_hw_type", vec![]),
        ("get_hw_version", vec![]),
        ("get_buffer_state", vec![]),
        ("device_purge", vec![]),
        ("get_oscillator_period", vec![]),
        ("get_switch_trigger_mapping_enable", vec![]),
        ("get_switch_enable_mapping_enable", vec![]),
        ("get_pulser_delay", vec![json!(0)]),
        ("get_pulser_width", vec![json!(0)]),
        ("get_pulser_burst", vec![json!(1)]),
        ("get_pulser_config", vec![json!(5)]),
        ("get_switch_trigger_config", vec![json!(3)]),
        ("get_switch_enable_config", vec![json!(3)]),
        ("get_config_name", vec![json!(3)]),
        ("get_config_flags", vec![json!(3)]),
        ("get_config_list", vec![]),
        ("set_baud_rate", vec![json!(230400)]),
    ] {
        let reply = rpc(device.as_mut(), method, &args, json!({})).unwrap();
        assert_eq!(reply["$tuple"][0], 0, "{method}");
    }
    for (method, args) in [
        ("set_oscillator_period", vec![json!(u32::MAX)]),
        ("set_controller_config", vec![json!(255)]),
        ("set_pulser_delay", vec![json!(3), json!(0)]),
        ("set_pulser_width", vec![json!(3), json!(1)]),
        ("set_pulser_burst", vec![json!(1), json!(u32::MAX)]),
        ("set_device_enable", vec![json!(false)]),
        ("save_current_config", vec![json!(125)]),
        ("load_current_config", vec![json!(125)]),
        ("purge", vec![]),
    ] {
        assert_eq!(
            rpc(device.as_mut(), method, &args, json!({})).unwrap(),
            0,
            "{method}"
        );
    }
    assert_eq!(
        rpc(device.as_mut(), "close_port", &[], json!({})).unwrap(),
        0
    );
    assert_eq!(
        rpc(device.as_mut(), "open_port", &[json!(7)], json!({})).unwrap(),
        0
    );
    assert_eq!(
        rpc(device.as_mut(), "disconnect", &[], json!({})).unwrap(),
        true
    );
}

#[test]
fn hd_indexed_facade_matches_the_sdk_for_every_clock_pll_io_timer_and_switch_operation() {
    let (mut device, _) = case(true);
    connect(device.as_mut());
    for method in [
        "get_clock_count",
        "get_pll_count",
        "get_divider_count",
        "get_counter_count",
        "get_oscillator_count",
        "get_timer_count",
    ] {
        assert_eq!(
            rpc(device.as_mut(), method, &[], json!({})).unwrap()["$tuple"][0],
            0,
            "{method}"
        );
    }
    for method in [
        "get_clock_source",
        "get_pll_source",
        "get_pll_ref_div",
        "get_pll_fdbk_div",
        "get_pll_post_div1",
        "get_pll_post_div2",
        "get_pll_post_div3",
        "get_pll_power_down",
        "get_pll_cp_current",
        "get_pll_lf_resistor",
        "get_pll_lf_capacitor",
        "get_divider_period",
        "get_counter_period",
        "get_oscillator_period",
        "get_fan_speed",
        "get_digital_io_config",
        "get_digital_io_signal_source",
        "get_switch_trigger_source",
        "get_switch_enable_source",
        "get_switch_rise_delay_fine",
        "get_switch_fall_delay_fine",
        "get_timer_delay",
        "get_timer_width",
        "get_timer_burst",
        "get_timer_trigger_source",
        "get_timer_stop_source",
        "get_switch_delay",
        "get_mapping_engine_input_source",
        "get_mapping_engine_output_value",
    ] {
        assert_eq!(
            rpc(device.as_mut(), method, &[json!(0)], json!({})).unwrap()["$tuple"][0],
            0,
            "{method}"
        );
    }
    for (method, value) in [
        ("set_clock_source", json!(128)),
        ("set_pll_source", json!(128)),
        ("set_pll_ref_div", json!(4095)),
        ("set_pll_fdbk_div", json!(16383)),
        ("set_pll_post_div1", json!(12)),
        ("set_pll_post_div2", json!(12)),
        ("set_pll_post_div3", json!(3)),
        ("set_pll_power_down", json!(false)),
        ("set_pll_cp_current", json!(3)),
        ("set_pll_lf_resistor", json!(3)),
        ("set_pll_lf_capacitor", json!(1)),
        ("set_divider_period", json!(255)),
        ("set_counter_period", json!(20)),
        ("set_oscillator_period", json!(u32::MAX)),
        ("set_digital_io_config", json!(2)),
        ("set_digital_io_signal_source", json!(255)),
        ("set_switch_trigger_source", json!(191)),
        ("set_switch_enable_source", json!(191)),
        ("set_switch_rise_delay_fine", json!(511)),
        ("set_switch_fall_delay_fine", json!(511)),
        ("set_timer_delay", json!(u32::MAX)),
        ("set_timer_width", json!(u32::MAX)),
        ("set_timer_burst", json!((1 << 24) - 1)),
        ("set_timer_trigger_source", json!(191)),
        ("set_timer_stop_source", json!(191)),
    ] {
        assert_eq!(
            rpc(device.as_mut(), method, &[json!(0), value], json!({})).unwrap(),
            0,
            "{method}"
        );
    }
}

#[test]
fn hd_config_buffers_telemetry_interlock_and_diagnostics_match_the_sdk() {
    let (mut device, state) = case(true);
    connect(device.as_mut());
    for method in [
        "get_current_config",
        "get_defaults",
        "get_signal_values",
        "get_signals",
        "get_manuf_date",
        "get_cpu_id",
        "get_dev_type",
        "get_io_state",
        "get_comm_error",
        "get_interlock_funct",
        "get_device_enable",
        "get_config_list",
        "get_sensor_data",
        "get_fan_data",
        "get_led_data",
        "get_cpu_data",
        "get_uptime",
        "get_total_time",
        "get_fw_version",
        "get_fw_date",
        "get_product_id",
        "get_product_no",
        "get_hw_type",
        "get_hw_version",
        "device_purge",
        "get_buffer_state",
        "get_housekeeping",
        "get_state",
        "get_device_state",
    ] {
        assert_eq!(
            rpc(device.as_mut(), method, &[], json!({})).unwrap()["$tuple"][0],
            0,
            "{method}"
        );
    }
    for (method, args) in [
        (
            "set_mapping_engine_input_source",
            vec![json!(0), json!(vec![191; 4])],
        ),
        (
            "set_mapping_engine_output_value",
            vec![json!(0), json!(false), json!(vec![15; 7])],
        ),
        ("set_current_config", vec![json!(vec![255; 318])]),
        ("set_defaults", vec![json!(vec![255; 20])]),
        ("set_config_data", vec![json!(499), json!(vec![255; 318])]),
        ("set_config_name", vec![json!(499), json!("x".repeat(192))]),
        (
            "set_config_flags",
            vec![json!(499), json!(true), json!(false)],
        ),
        ("set_interlock_funct", vec![json!(true)]),
        ("set_config", vec![json!(65535)]),
        ("save_defaults", vec![]),
        ("load_defaults", vec![]),
        ("restart", vec![]),
        ("get_interface_state", vec![]),
    ] {
        assert_eq!(
            rpc(device.as_mut(), method, &args, json!({})).unwrap(),
            0,
            "{method}"
        );
    }
    assert_eq!(
        rpc(device.as_mut(), "get_config_data", &[json!(499)], json!({})).unwrap()["$tuple"][1]
            .as_array()
            .unwrap()
            .len(),
        318
    );
    for method in ["get_error_message", "get_io_error_message"] {
        assert_eq!(
            rpc(device.as_mut(), method, &[], json!({})).unwrap(),
            "mock diagnostic"
        );
    }
    // Loading configurations and shutting down never tamper with the interlock.
    state.lock().unwrap().calls.clear();
    rpc(device.as_mut(), "shutdown", &[], json!({})).unwrap();
    assert!(
        !state
            .lock()
            .unwrap()
            .names()
            .contains(&"SetInterlockFunct".to_owned())
    );
}

#[test]
fn optional_identity_probe_exceptions_are_tolerated_but_vendor_failures_are_fatal() {
    for hd in [false, true] {
        for kind in ["AttributeError", "RuntimeError"] {
            let (mut device, state) = case(hd);
            state.lock().unwrap().fail("GetProductID", kind);
            connect(device.as_mut());
            assert!(!state.lock().unwrap().names().contains(&"Close".to_owned()));
        }
        let (mut device, state) = case(hd);
        state
            .lock()
            .unwrap()
            .inject("GetProductID", -15, vec![json!("")]);
        assert!(rpc(device.as_mut(), "connect", &[], json!({})).is_err());
        assert_eq!(state.lock().unwrap().names().last().unwrap(), "Close");
    }
}

#[test]
fn unexpected_identity_is_logged_as_warning_and_state_maps_keep_integer_keys() {
    for hd in [false, true] {
        let (mut device, state) = case(hd);
        state
            .lock()
            .unwrap()
            .inject("GetProductID", 0, vec![json!("Other instrument")]);
        let (tx, rx) = std::sync::mpsc::sync_channel(4);
        let ctx = Context::new(
            std::time::Duration::from_secs(2),
            Arc::new(std::sync::atomic::AtomicBool::new(false)),
            Some(tx),
        );
        assert_eq!(device.call("connect", &[], &json!({}), &ctx).unwrap(), true);
        assert_eq!(rx.try_recv().unwrap()["event"], "warning");
        let main = device.get_attribute("MAIN_STATE").unwrap();
        assert_eq!(main["$map"][0][0], 0);
        assert_eq!(
            main["$map"][0][1],
            if hd { "STATE_STANDBY" } else { "STATE_ON" }
        );
        if hd {
            assert_eq!(
                device.get_attribute("CONTROLLER_STATE").unwrap()["$map"]
                    .as_array()
                    .unwrap()
                    .len(),
                12
            );
        }
    }
}
