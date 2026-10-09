#![cfg(feature = "esi")]

use esibd_native_worker::Backend;
use esibd_native_worker::codec::{float, tuple};
use esibd_native_worker::context::Context;
use esibd_native_worker::error::{Error, Result};
use esibd_native_worker::esi::{Controller, SUPPORTED_METHODS};
use esibd_native_worker::ffi::{Dll, NativeReply};
use serde_json::{Value, json};
use std::collections::{BTreeMap, VecDeque};
use std::sync::{Arc, Mutex, atomic::AtomicBool, mpsc};
use std::time::Duration;

const ACTIVE: u64 = 0xc100;
const HV_READY: u64 = 0x50;

struct Rig {
    controller: Controller,
    state: Arc<Mutex<Instrument>>,
}

struct Instrument {
    calls: Vec<(String, Vec<Value>)>,
    counts: BTreeMap<String, usize>,
    injected: BTreeMap<(String, usize), NativeReply>,
    cancel: BTreeMap<String, Context>,
    delays: BTreeMap<String, Duration>,
    enabled: bool,
    active: [bool; 3],
    target: [f64; 3],
    heat_target: f64,
    maxima: [f64; 4],
    limits: [f64; 3],
    heat_valid: bool,
    heat_temperature: f64,
    heat_ready: bool,
    complete_states: VecDeque<u64>,
    device_state: u64,
    main_state: u64,
    device_type: u64,
    presence: [u64; 5],
    module_types: [u64; 4],
    inventory_valid: bool,
    ranges: [(bool, bool); 3],
    adc_steps: [usize; 3],
    adc_ready: [u64; 3],
    adc_sticky: bool,
    adc_never_ready: bool,
    voltage_valid: bool,
    current_valid: bool,
    voltages: [[f64; 2]; 3],
    current: f64,
    voltage_plan: BTreeMap<(usize, bool), VecDeque<f64>>,
    fresh_counts: [[usize; 2]; 3],
    config: Vec<u8>,
    config_corrupt: bool,
    slots: BTreeMap<usize, (String, bool, bool)>,
}

impl Default for Instrument {
    fn default() -> Self {
        Self {
            calls: Vec::new(),
            counts: BTreeMap::new(),
            injected: BTreeMap::new(),
            cancel: BTreeMap::new(),
            delays: BTreeMap::new(),
            enabled: true,
            active: [true; 3],
            target: [0.0, 100.0, 200.0],
            heat_target: 200.0,
            maxima: [24.0, 4.0, 96.0, 400.0],
            limits: [12.0, 2.0, 24.0],
            heat_valid: true,
            heat_temperature: 30.0,
            heat_ready: true,
            complete_states: VecDeque::new(),
            device_state: 0,
            main_state: 0,
            device_type: 0x8ed6,
            presence: [1, 1, 1, 0, 1],
            module_types: [0xdb1c, 0x0a0d, 0x0a0d, 0x1c34],
            inventory_valid: true,
            ranges: [(false, false), (true, true), (false, false)],
            adc_steps: [0; 3],
            adc_ready: [0; 3],
            adc_sticky: false,
            adc_never_ready: false,
            voltage_valid: true,
            current_valid: true,
            voltages: [[0.5, -0.5]; 3],
            current: -2e-9,
            voltage_plan: BTreeMap::new(),
            fresh_counts: [[0; 2]; 3],
            config: vec![0; 53],
            config_corrupt: false,
            slots: BTreeMap::from([
                (10, ("Spray-2kV".to_owned(), true, true)),
                (12, ("Test-100V".to_owned(), true, false)),
            ]),
        }
    }
}

struct MockDll(Arc<Mutex<Instrument>>);

impl Dll for MockDll {
    fn call(&mut self, symbol: &str, args: &[Value]) -> Result<NativeReply> {
        let mut state = self.0.lock().unwrap();
        state.calls.push((symbol.to_owned(), args.to_vec()));
        let count = *state
            .counts
            .entry(symbol.to_owned())
            .and_modify(|count| *count += 1)
            .or_insert(1);
        if let Some(ctx) = state.cancel.get(symbol) {
            ctx.cancel();
        }
        if let Some(delay) = state.delays.get(symbol) {
            std::thread::sleep(*delay);
        }
        if let Some(reply) = state.injected.remove(&(symbol.to_owned(), count)) {
            return Ok(reply);
        }
        let address = || args[0].as_u64().unwrap() as usize;
        let values = match symbol {
            "COM_ESI_CTRL_Open"
            | "COM_ESI_CTRL_Close"
            | "COM_ESI_CTRL_Purge"
            | "COM_ESI_CTRL_UpdateModulePresence" => vec![],
            "COM_ESI_CTRL_SetBaudRate" => vec![args[0].clone()],
            "COM_ESI_CTRL_GetDevType" => vec![json!(state.device_type)],
            "COM_ESI_CTRL_SetEnable" => {
                state.enabled = args[0].as_bool().unwrap();
                vec![]
            }
            "COM_ESI_CTRL_GetEnable" => vec![json!(state.enabled)],
            "COM_ESI_CTRL_SetModuleActivationState" => {
                state.active[address()] = args[1].as_bool().unwrap();
                vec![]
            }
            "COM_ESI_CTRL_GetModuleActivationState" => {
                assert_eq!(address(), 0, "HV activation must use PWM, not this getter");
                vec![json!(state.active[0])]
            }
            "COM_ESI_CTRL_SetHVsupplyTargetOutputVoltage" => {
                state.target[address()] = args[1].as_f64().unwrap();
                vec![]
            }
            "COM_ESI_CTRL_GetHVsupplyTargetOutputVoltage" => vec![float(state.target[address()])],
            "COM_ESI_CTRL_GetHVsupplyParamsPWM" => {
                vec![
                    json!(1.0),
                    json!(0.5),
                    json!(0.01),
                    json!(0.02),
                    float(state.target[address()]),
                    json!(0.5),
                    json!(state.active[address()]),
                    json!(HV_READY),
                ]
            }
            "COM_ESI_CTRL_GetModulePresence" => {
                assert_eq!(args[2].as_array().unwrap().len(), 5);
                vec![
                    json!(state.inventory_valid),
                    json!(3),
                    json!(state.presence),
                ]
            }
            "COM_ESI_CTRL_GetModuleDevType" => vec![json!(state.module_types[address()])],
            "COM_ESI_CTRL_GetHeatCtrlHwLimits" => state.maxima.into_iter().map(float).collect(),
            "COM_ESI_CTRL_GetHeatCtrlVoltageLimit" => vec![float(state.limits[0])],
            "COM_ESI_CTRL_GetHeatCtrlCurrentLimit" => vec![float(state.limits[1])],
            "COM_ESI_CTRL_GetHeatCtrlPowerLimit" => vec![float(state.limits[2])],
            "COM_ESI_CTRL_SetHeatCtrlVoltageLimit"
            | "COM_ESI_CTRL_SetHeatCtrlCurrentLimit"
            | "COM_ESI_CTRL_SetHeatCtrlPowerLimit" => {
                let index = match symbol {
                    "COM_ESI_CTRL_SetHeatCtrlVoltageLimit" => 0,
                    "COM_ESI_CTRL_SetHeatCtrlCurrentLimit" => 1,
                    _ => 2,
                };
                state.limits[index] = args[0].as_f64().unwrap();
                vec![float(state.limits[index])]
            }
            "COM_ESI_CTRL_SetHeatCtrlHeaterTemperature" => {
                state.heat_target = args[0].as_f64().unwrap();
                vec![float(state.heat_target)]
            }
            "COM_ESI_CTRL_GetHeatCtrlHeaterTemperature" => vec![float(state.heat_target)],
            "COM_ESI_CTRL_GetHeatCtrlMonitoring" => vec![
                json!(state.heat_valid),
                json!(3.0),
                json!(2.99),
                json!(0.5),
                float(state.heat_temperature),
            ],
            "COM_ESI_CTRL_GetModuleDataReadyFlags" => {
                if address() == 0 {
                    vec![json!(u64::from(state.heat_ready))]
                } else if state.adc_sticky {
                    vec![json!(HV_READY)]
                } else if state.adc_never_ready {
                    vec![json!(0)]
                } else {
                    let address = address();
                    let flags = if state.adc_steps[address] == 0 {
                        0
                    } else {
                        HV_READY
                    };
                    state.adc_steps[address] += 1;
                    state.adc_ready[address] = flags;
                    vec![json!(flags)]
                }
            }
            "COM_ESI_CTRL_GetCompleteState" => {
                assert_eq!(args[7].as_array().unwrap().len(), 5);
                assert_eq!(args[8].as_array().unwrap().len(), 5);
                let default = if state.active[0] && state.enabled {
                    ACTIVE
                } else {
                    0x0100
                };
                let heat = state.complete_states.pop_front().unwrap_or(default);
                vec![
                    json!(5),
                    json!(state.device_state),
                    json!(0x37),
                    json!(0),
                    json!(0xf),
                    json!(0xf00c),
                    json!(state.main_state),
                    json!([1, HV_READY, HV_READY, 0, 0]),
                    json!([heat, ACTIVE, ACTIVE, 0, 0]),
                ]
            }
            "COM_ESI_CTRL_GetHousekeeping" => vec![
                json!(24.0),
                json!(5.0),
                json!(3.3),
                json!(25.0),
                json!(26.0),
            ],
            "COM_ESI_CTRL_GetModuleLEDData" => vec![json!(false), json!(true), json!(false)],
            "COM_ESI_CTRL_GetHeatCtrlOutputVoltage" => vec![json!(3.0)],
            "COM_ESI_CTRL_GetHeatCtrlHeaterPower" => vec![json!(1.5)],
            "COM_ESI_CTRL_GetHeatCtrlIlockState" => vec![json!(0)],
            "COM_ESI_CTRL_GetHeatCtrlHousekeeping" => vec![
                json!(true),
                json!(3.3),
                json!(25.0),
                json!(5.0),
                json!(24.0),
                json!(26.0),
            ],
            "COM_ESI_CTRL_GetHVsupplyMeasRanges" => vec![
                json!(state.ranges[address()].0),
                json!(state.ranges[address()].1),
            ],
            "COM_ESI_CTRL_SetHVsupplyMeasRanges" => {
                state.ranges[address()] = (args[1].as_bool().unwrap(), args[2].as_bool().unwrap());
                state.adc_steps[address()] = 0;
                state.adc_ready[address()] = 0;
                vec![]
            }
            "COM_ESI_CTRL_GetHVsupplyOutputVoltage" => {
                let address = address();
                let negative = state.ranges[address].0;
                let mut voltage = state.voltages[address][usize::from(negative)];
                if state.adc_ready[address] & 0x10 != 0 {
                    if let Some(plan) = state.voltage_plan.get_mut(&(address, negative))
                        && let Some(value) = plan.pop_front()
                    {
                        voltage = value;
                    }
                    state.fresh_counts[address][usize::from(negative)] += 1;
                }
                state.adc_ready[address] &= !0x10;
                vec![json!(state.voltage_valid), float(voltage)]
            }
            "COM_ESI_CTRL_GetHVsupplyOutputCurrent" => {
                state.adc_ready[address()] &= !0x40;
                vec![json!(state.current_valid), float(state.current)]
            }
            "COM_ESI_CTRL_GetCurrentConfig" => {
                assert_eq!(args[0].as_array().unwrap().len(), 53);
                let mut config = state.config.clone();
                if state.config_corrupt {
                    config[50] ^= 1;
                }
                vec![json!(config)]
            }
            "COM_ESI_CTRL_SetCurrentConfig" => {
                assert_eq!(args[0].as_array().unwrap().len(), 53);
                state.config = args[0]
                    .as_array()
                    .unwrap()
                    .iter()
                    .map(|value| value.as_u64().unwrap() as u8)
                    .collect();
                vec![args[0].clone()]
            }
            "COM_ESI_CTRL_GetConfigList" => {
                assert_eq!(args[0].as_array().unwrap().len(), 1023);
                assert_eq!(args[1].as_array().unwrap().len(), 1023);
                let mut active = vec![false; 1023];
                let mut valid = vec![false; 1023];
                for (index, (_, on, ok)) in &state.slots {
                    active[*index] = *on;
                    valid[*index] = *ok;
                }
                vec![json!(active), json!(valid)]
            }
            "COM_ESI_CTRL_GetConfigName" => {
                assert_eq!(args[1]["capacity"], 202);
                vec![json!(
                    state
                        .slots
                        .get(&address())
                        .map_or("", |slot| slot.0.as_str())
                )]
            }
            "COM_ESI_CTRL_GetConfigFlags" => {
                let slot = state.slots.get(&address());
                vec![
                    json!(slot.is_some_and(|slot| slot.1)),
                    json!(slot.is_some_and(|slot| slot.2)),
                ]
            }
            "COM_ESI_CTRL_LoadCurrentConfig" => {
                state.config.fill(0);
                state.target = [0.0; 3];
                state.active = [false; 3];
                state.enabled = false;
                vec![]
            }
            "COM_ESI_CTRL_SaveCurrentConfig" | "COM_ESI_CTRL_SetConfigFlags" => vec![],
            "COM_ESI_CTRL_SetConfigName" => {
                assert_eq!(args[1]["capacity"], 202);
                vec![args[1]["text"].clone()]
            }
            "COM_ESI_CTRL_GetProductID" => {
                assert_eq!(args[0]["capacity"], 81);
                vec![json!("ESI-CTRL")]
            }
            "COM_ESI_CTRL_GetModuleProductID" => {
                assert_eq!(args[1]["capacity"], 81);
                vec![json!("MODULE")]
            }
            "COM_ESI_CTRL_GetFwDate" => {
                assert_eq!(args[0]["capacity"], 12);
                vec![json!("2026-07-23")]
            }
            "COM_ESI_CTRL_GetProductNo"
            | "COM_ESI_CTRL_GetHwType"
            | "COM_ESI_CTRL_GetModuleProductNo"
            | "COM_ESI_CTRL_GetModuleHwType"
            | "COM_ESI_CTRL_GetHVsupplyFpgaVersion" => vec![json!(123456)],
            "COM_ESI_CTRL_GetFwVersion"
            | "COM_ESI_CTRL_GetHwVersion"
            | "COM_ESI_CTRL_GetModuleFwVersion"
            | "COM_ESI_CTRL_GetModuleHwVersion" => vec![json!(0x101)],
            "COM_ESI_CTRL_GetSWVersion" => return Ok(reply(0x123, vec![])),
            _ => {
                return Err(Error::unsupported(format!(
                    "Mock does not implement {symbol}"
                )));
            }
        };
        Ok(reply(0, values))
    }
}

fn reply(status: i64, values: Vec<Value>) -> NativeReply {
    NativeReply { status, values }
}

impl Rig {
    fn new() -> Self {
        let state = Arc::new(Mutex::new(Instrument::default()));
        let controller = Controller::new(
            &json!({"device_id": "test_esi", "com": 14, "baudrate": 230400}),
            Box::new(MockDll(state.clone())),
        )
        .unwrap();
        Self { controller, state }
    }
    fn connected(preserve: bool) -> Self {
        let mut rig = Self::new();
        rig.call("connect", &[], json!({"preserve_outputs": preserve}))
            .unwrap();
        rig.clear_calls();
        rig
    }
    fn call(&mut self, method: &str, args: &[Value], kwargs: Value) -> Result<Value> {
        self.controller
            .call(method, args, &kwargs, &Context::test(5.0))
    }
    fn clear_calls(&self) {
        self.state.lock().unwrap().calls.clear();
    }
    fn calls(&self) -> Vec<(String, Vec<Value>)> {
        self.state.lock().unwrap().calls.clone()
    }
    fn count(&self, symbol: &str) -> usize {
        self.calls()
            .iter()
            .filter(|(name, _)| name == symbol)
            .count()
    }
    fn inject(&self, symbol: &str, nth: usize, status: i64, values: Vec<Value>) {
        let mut state = self.state.lock().unwrap();
        let count = state.counts.get(symbol).copied().unwrap_or(0) + nth;
        state
            .injected
            .insert((symbol.to_owned(), count), reply(status, values));
    }
}

fn int_key(map: &Value, key: u64) -> &Value {
    &map["$map"]
        .as_array()
        .unwrap()
        .iter()
        .find(|pair| pair[0] == json!(key))
        .unwrap()[1]
}

fn output_command(symbol: &str) -> bool {
    [
        "SetEnable",
        "SetModuleActivationState",
        "SetHVsupplyTargetOutputVoltage",
        "SetHeatCtrlHeaterTemperature",
        "SetHeatCtrlVoltageLimit",
        "SetHeatCtrlCurrentLimit",
        "SetHeatCtrlPowerLimit",
        "SetCurrentConfig",
        "LoadCurrentConfig",
    ]
    .iter()
    .any(|suffix| symbol.ends_with(suffix))
}

#[test]
fn constructor_validates_identity_port_baud_and_unknown_keys() {
    for config in [
        json!({"device_id": " ", "com": 14}),
        json!({"device_id": "esi", "com": true}),
        json!({"device_id": "esi", "com": 0}),
        json!({"device_id": "esi", "com": 256}),
        json!({"device_id": "esi", "com": 14, "baudrate": 0}),
        json!({"device_id": "esi", "com": 14, "baudrate": true}),
        json!({"device_id": "esi", "com": 14, "unknown": 1}),
    ] {
        assert!(
            Controller::new(
                &config,
                Box::new(MockDll(Arc::new(Mutex::new(Instrument::default()))))
            )
            .is_err()
        );
    }
    let rig = Rig::new();
    assert_eq!(rig.controller.get_attribute("com").unwrap(), 14);
    assert_eq!(rig.controller.get_attribute("baudrate").unwrap(), 230400);
    assert_eq!(
        rig.controller.get_attribute("HV_MODULE_ADDRESSES").unwrap(),
        tuple(vec![json!(1), json!(2)])
    );
}

#[test]
fn connect_disarms_every_gate_before_reopening_communication_then_leaves_safe_off() {
    let mut rig = Rig::new();
    assert_eq!(rig.call("connect", &[], json!({})).unwrap(), true);
    let state = rig.state.lock().unwrap();
    assert!(!state.enabled);
    assert_eq!(state.active, [false; 3]);
    assert_eq!(state.target, [0.0; 3]);
    assert_eq!(state.heat_target, 0.0);
    assert_eq!(
        state.calls[0],
        ("COM_ESI_CTRL_Open".to_owned(), vec![json!(14)])
    );
    let enables = state
        .calls
        .iter()
        .filter(|(name, _)| name == "COM_ESI_CTRL_SetEnable")
        .map(|(_, args)| args[0].clone())
        .collect::<Vec<_>>();
    assert_eq!(enables, vec![json!(false), json!(true), json!(false)]);
    let first_on = state
        .calls
        .iter()
        .position(|(symbol, args)| symbol == "COM_ESI_CTRL_SetEnable" && args[0] == true)
        .unwrap();
    for address in [0, 1, 2] {
        assert!(state.calls[..first_on].contains(&(
            "COM_ESI_CTRL_SetModuleActivationState".to_owned(),
            vec![json!(address), json!(false)]
        )));
    }
    drop(state);
    assert_eq!(
        int_key(
            &rig.controller.get_attribute("_module_inventory").unwrap(),
            0
        )["device_type"],
        0xdb1c
    );
    rig.clear_calls();
    assert_eq!(rig.call("connect", &[], json!({})).unwrap(), true);
    assert!(rig.calls().is_empty());
}

#[test]
fn crash_resume_only_changes_communication_and_inventory() {
    let rig = Rig::connected(true);
    let state = rig.state.lock().unwrap();
    assert!(state.enabled);
    assert_eq!(state.active, [true; 3]);
    assert_eq!(state.target[1..], [100.0, 200.0]);
    assert_eq!(state.heat_target, 200.0);
}

#[test]
fn wrong_controller_identity_closes_without_output_commands() {
    let mut rig = Rig::new();
    rig.state.lock().unwrap().device_type = 0xffff;
    assert!(
        rig.call("connect", &[], json!({}))
            .unwrap_err()
            .message
            .contains("type mismatch")
    );
    assert_eq!(rig.count("COM_ESI_CTRL_Close"), 1);
    assert!(!rig.calls().iter().any(|(name, _)| output_command(name)));
    assert_eq!(rig.controller.get_attribute("connected").unwrap(), false);
    assert_eq!(
        rig.controller.get_attribute("_dll_port_claimed").unwrap(),
        false
    );
}

#[test]
fn inventory_rejects_missing_invalid_wrong_and_uncontrolled_hv_modules() {
    for failure in ["missing", "invalid", "wrong", "extra", "not_valid"] {
        let mut rig = Rig::new();
        {
            let mut state = rig.state.lock().unwrap();
            match failure {
                "missing" => state.presence[2] = 0,
                "invalid" => state.presence[1] = 2,
                "wrong" => state.module_types[0] = 0xffff,
                "extra" => {
                    state.presence[3] = 1;
                    state.module_types[3] = 0x0a0d;
                }
                _ => state.inventory_valid = false,
            }
        }
        assert!(
            rig.call("connect", &[], json!({"preserve_outputs": true}))
                .is_err(),
            "{failure}"
        );
        assert!(!rig.calls().iter().any(|(name, _)| output_command(name)));
        assert_eq!(rig.controller.get_attribute("connected").unwrap(), true);
        assert_eq!(
            rig.controller.get_attribute("_dll_port_claimed").unwrap(),
            true
        );
    }
}

#[test]
fn identity_preserves_optional_probe_errors_and_all_fixed_string_capacities() {
    let mut rig = Rig::connected(true);
    rig.inject("COM_ESI_CTRL_GetHwVersion", 1, -15, vec![json!(0)]);
    let identity = rig.call("collect_identity", &[], json!({})).unwrap();
    assert_eq!(identity["controller"]["product_id"], "ESI-CTRL");
    assert_eq!(identity["controller"]["dll_version"], 0x123);
    assert!(
        identity["controller"]["hardware_version"]["error"]
            .as_str()
            .unwrap()
            .contains("-15")
    );
    assert_eq!(int_key(&identity["modules"], 2)["fpga_version"], 123456);
    assert!(!rig.calls().iter().any(|(name, _)| output_command(name)));
}

#[test]
fn global_on_requires_matching_readback_and_never_replays_on() {
    let mut rig = Rig::connected(false);
    rig.inject("COM_ESI_CTRL_GetEnable", 1, 0, vec![json!(false)]);
    let error = rig
        .call("set_global_active", &[json!(true)], json!({}))
        .unwrap_err();
    assert!(error.message.contains("verification failed"));
    assert_eq!(rig.count("COM_ESI_CTRL_SetEnable"), 1);
}

#[test]
fn hv_targets_validate_before_output_write_and_require_readback() {
    let mut rig = Rig::connected(false);
    for voltage in [
        json!(-1),
        json!(3000.01),
        float(f64::NAN),
        float(f64::INFINITY),
    ] {
        assert_eq!(
            rig.call("set_hv_module_target", &[json!(1), voltage], json!({}))
                .unwrap_err()
                .kind,
            "ValueError"
        );
    }
    for address in [json!(true), json!(0), json!(3), json!(1.5)] {
        assert!(
            rig.call("set_hv_module_target", &[address, json!(1.0)], json!({}))
                .is_err()
        );
    }
    assert!(rig.calls().is_empty());
    assert_eq!(
        rig.call("set_target_voltage", &[json!(2), json!(30.0)], json!({}))
            .unwrap(),
        30.0
    );
    rig.inject(
        "COM_ESI_CTRL_GetHVsupplyTargetOutputVoltage",
        1,
        0,
        vec![json!(20.0)],
    );
    assert!(
        rig.call("set_hv_module_target", &[json!(1), json!(10.0)], json!({}))
            .is_err()
    );
}

#[test]
fn hv_gate_uses_pwm_retries_read_once_and_never_uses_direct_getter() {
    let mut rig = Rig::connected(false);
    rig.inject("COM_ESI_CTRL_GetHVsupplyParamsPWM", 1, -15, vec![]);
    assert_eq!(
        rig.call("set_hv_module_active", &[json!(1), json!(true)], json!({}))
            .unwrap(),
        true
    );
    assert_eq!(rig.count("COM_ESI_CTRL_SetModuleActivationState"), 1);
    assert_eq!(rig.count("COM_ESI_CTRL_GetHVsupplyParamsPWM"), 2);
    assert_eq!(rig.count("COM_ESI_CTRL_GetModuleActivationState"), 0);
}

#[test]
fn hv_gate_mismatch_never_opens_global_gate() {
    let mut rig = Rig::connected(false);
    rig.inject(
        "COM_ESI_CTRL_GetHVsupplyParamsPWM",
        1,
        0,
        vec![
            json!(1),
            json!(0),
            json!(0),
            json!(0),
            json!(0),
            json!(0),
            json!(false),
            json!(0),
        ],
    );
    assert!(
        rig.call("set_output_active", &[json!(1), json!(true)], json!({}))
            .is_err()
    );
    assert_eq!(rig.count("COM_ESI_CTRL_SetEnable"), 0);
}

#[test]
fn heat_on_validates_sensor_limits_and_direct_gate_then_complete_activation() {
    let mut rig = Rig::connected(false);
    assert_eq!(
        rig.call("set_heater_temperature", &[json!(150.0)], json!({}))
            .unwrap(),
        150.0
    );
    rig.clear_calls();
    rig.state.lock().unwrap().complete_states = VecDeque::from([0x4100, 0x4100, ACTIVE]);
    assert_eq!(
        rig.call(
            "set_output_active",
            &[json!(0), json!(true)],
            json!({"timeout_s": 0.5})
        )
        .unwrap(),
        true
    );
    assert_eq!(rig.count("COM_ESI_CTRL_GetCompleteState"), 3);
    assert_eq!(rig.count("COM_ESI_CTRL_SetModuleActivationState"), 1);
    assert_eq!(rig.count("COM_ESI_CTRL_SetEnable"), 1);
    assert_eq!(rig.count("COM_ESI_CTRL_SetHeatCtrlPowerLimit"), 0);
    let snapshot = rig.call("collect_diagnostics", &[], json!({})).unwrap();
    assert_eq!(snapshot["heat"]["active"], true);
}

#[test]
fn missing_activation_bits_timeout_without_on_replay_and_off_remains_possible() {
    for state in [0x4100, 0x8100, 0xc000, 0] {
        let mut rig = Rig::connected(false);
        rig.state.lock().unwrap().complete_states = VecDeque::from(vec![state; 10]);
        assert!(
            rig.call(
                "set_output_active",
                &[json!(0), json!(true)],
                json!({"timeout_s": 0.025})
            )
            .unwrap_err()
            .message
            .contains("not confirmed")
        );
        assert_eq!(rig.count("COM_ESI_CTRL_SetEnable"), 1);
        assert_eq!(rig.count("COM_ESI_CTRL_SetModuleActivationState"), 1);
        assert_eq!(
            rig.controller.get_attribute("_transport_poisoned").unwrap(),
            false
        );
        assert_eq!(
            rig.call("set_output_active", &[json!(0), json!(false)], json!({}))
                .unwrap(),
            false
        );
    }
}

#[test]
fn heater_off_does_not_require_operating_limits_sensor_or_global_disable() {
    let mut rig = Rig::connected(true);
    {
        let mut state = rig.state.lock().unwrap();
        state.limits = [0.0; 3];
        state.heat_valid = false;
        state.heat_temperature = f64::NAN;
    }
    assert_eq!(
        rig.call("set_output_active", &[json!(0), json!(false)], json!({}))
            .unwrap(),
        false
    );
    assert_eq!(rig.count("COM_ESI_CTRL_GetHeatCtrlHwLimits"), 0);
    assert_eq!(rig.count("COM_ESI_CTRL_GetHeatCtrlMonitoring"), 0);
    assert_eq!(rig.count("COM_ESI_CTRL_SetEnable"), 0);
    let state = rig.state.lock().unwrap();
    assert_eq!(state.active, [false, true, true]);
    assert!(state.enabled);
    assert_eq!(state.heat_target, 0.0);
}

#[test]
fn heater_on_refuses_invalid_limits_and_sensor_without_any_output_command() {
    for failure in [
        "zero_voltage",
        "current_exceeds_max",
        "power_nan",
        "sensor_invalid",
        "temperature_nan",
        "temperature_high",
    ] {
        let mut rig = Rig::connected(false);
        {
            let mut state = rig.state.lock().unwrap();
            match failure {
                "zero_voltage" => state.limits[0] = 0.0,
                "current_exceeds_max" => state.limits[1] = 4.1,
                "power_nan" => state.limits[2] = f64::NAN,
                "sensor_invalid" => state.heat_valid = false,
                "temperature_nan" => state.heat_temperature = f64::NAN,
                _ => state.heat_temperature = 401.0,
            }
        }
        assert!(
            rig.call("set_output_active", &[json!(0), json!(true)], json!({}))
                .is_err(),
            "{failure}"
        );
        assert!(
            !rig.calls().iter().any(|(symbol, _)| output_command(symbol)),
            "{failure}"
        );
    }
}

#[test]
fn heater_on_rejects_missing_ack_and_mismatched_direct_activation_readback() {
    for status in [-10, 0] {
        let mut rig = Rig::connected(false);
        if status == 0 {
            rig.inject(
                "COM_ESI_CTRL_GetModuleActivationState",
                1,
                0,
                vec![json!(false)],
            );
        } else {
            rig.inject("COM_ESI_CTRL_SetModuleActivationState", 1, status, vec![]);
        }
        assert!(
            rig.call("set_output_active", &[json!(0), json!(true)], json!({}))
                .is_err()
        );
        assert_eq!(rig.count("COM_ESI_CTRL_SetEnable"), 0);
    }
}

#[test]
fn heater_fault_interlock_and_lost_gates_fail_without_polling_or_reactivation() {
    for failure in ["device", "main", "unknown_main", "lost_global", "lost_heat"] {
        let mut rig = Rig::connected(false);
        match failure {
            "device" => rig.state.lock().unwrap().device_state = 1,
            "main" => rig.state.lock().unwrap().main_state = 21,
            "unknown_main" => rig.state.lock().unwrap().main_state = 0xffff,
            "lost_global" => rig.inject("COM_ESI_CTRL_GetEnable", 2, 0, vec![json!(false)]),
            _ => rig.inject(
                "COM_ESI_CTRL_GetModuleActivationState",
                2,
                0,
                vec![json!(false)],
            ),
        }
        assert!(
            rig.call("set_output_active", &[json!(0), json!(true)], json!({}))
                .is_err(),
            "{failure}"
        );
        assert_eq!(rig.count("COM_ESI_CTRL_GetCompleteState"), 1);
        assert_eq!(rig.count("COM_ESI_CTRL_SetEnable"), 1);
    }
}

#[test]
fn cancelled_on_is_refused_but_local_off_still_runs() {
    let mut rig = Rig::connected(true);
    let ctx = Context::test(1.0);
    ctx.cancel();
    let error = rig
        .controller
        .call(
            "set_output_active",
            &[json!(0), json!(true)],
            &json!({}),
            &ctx,
        )
        .unwrap_err();
    assert_eq!(error.kind, "InterruptedError");
    assert!(rig.calls().is_empty());
    assert_eq!(
        rig.controller
            .call(
                "set_output_active",
                &[json!(0), json!(false)],
                &json!({}),
                &ctx
            )
            .unwrap(),
        false
    );
}

#[test]
fn cancellation_after_native_boundary_prevents_following_commands() {
    for symbol in [
        "COM_ESI_CTRL_GetHeatCtrlMonitoring",
        "COM_ESI_CTRL_SetModuleActivationState",
        "COM_ESI_CTRL_GetCompleteState",
    ] {
        let mut rig = Rig::connected(false);
        let ctx = Context::test(2.0);
        rig.state
            .lock()
            .unwrap()
            .cancel
            .insert(symbol.to_owned(), ctx.clone());
        assert_eq!(
            rig.controller
                .call(
                    "set_output_active",
                    &[json!(0), json!(true)],
                    &json!({}),
                    &ctx
                )
                .unwrap_err()
                .kind,
            "InterruptedError"
        );
        if symbol != "COM_ESI_CTRL_GetCompleteState" {
            assert_eq!(rig.count("COM_ESI_CTRL_SetEnable"), 0);
        }
        assert_eq!(rig.calls().last().unwrap().0, symbol);
        rig.state.lock().unwrap().cancel.clear();
        assert_eq!(
            rig.controller
                .call(
                    "set_output_active",
                    &[json!(0), json!(false)],
                    &json!({}),
                    &ctx
                )
                .unwrap(),
            false
        );
    }
}

#[test]
fn no_monitoring_data_is_not_a_sensor_read_and_invalid_tuple_is_not_retried() {
    let mut rig = Rig::connected(false);
    rig.state.lock().unwrap().heat_ready = false;
    assert!(
        rig.call(
            "set_output_active",
            &[json!(0), json!(true)],
            json!({"timeout_s": 0.025})
        )
        .unwrap_err()
        .message
        .contains("not ready")
    );
    assert_eq!(rig.count("COM_ESI_CTRL_GetHeatCtrlMonitoring"), 0);
    rig.clear_calls();
    {
        let mut state = rig.state.lock().unwrap();
        state.heat_ready = true;
        state.heat_valid = false;
    }
    assert!(
        rig.call("set_output_active", &[json!(0), json!(true)], json!({}))
            .is_err()
    );
    assert_eq!(rig.count("COM_ESI_CTRL_GetHeatCtrlMonitoring"), 1);
}

#[test]
fn live_temperature_changes_validate_sensor_and_accept_verified_quantization() {
    let mut rig = Rig::connected(true);
    rig.state.lock().unwrap().heat_valid = false;
    assert!(
        rig.call("set_heater_temperature", &[json!(100.0)], json!({}))
            .is_err()
    );
    assert_eq!(rig.count("COM_ESI_CTRL_SetHeatCtrlHeaterTemperature"), 0);
    {
        let mut state = rig.state.lock().unwrap();
        state.heat_valid = true;
        state.heat_target = 100.125;
    }
    rig.inject(
        "COM_ESI_CTRL_SetHeatCtrlHeaterTemperature",
        1,
        0,
        vec![json!(100.125)],
    );
    assert_eq!(
        rig.call("set_heater_temperature", &[json!(100.1)], json!({}))
            .unwrap(),
        100.125
    );
    rig.inject(
        "COM_ESI_CTRL_GetHeatCtrlHeaterTemperature",
        2,
        0,
        vec![json!(150.0)],
    );
    assert!(
        rig.call("set_heater_temperature", &[json!(100.0)], json!({}))
            .is_err()
    );
}

#[test]
fn heater_limits_validate_entire_batch_before_any_write_and_use_applied_values() {
    let mut rig = Rig::connected(false);
    assert!(
        rig.call(
            "configure_heat_limits",
            &[],
            json!({"voltage_v": 10.0, "current_a": 4.1})
        )
        .is_err()
    );
    assert_eq!(rig.count("COM_ESI_CTRL_SetHeatCtrlVoltageLimit"), 0);
    assert_eq!(rig.state.lock().unwrap().limits, [12.0, 2.0, 24.0]);
    rig.inject(
        "COM_ESI_CTRL_SetHeatCtrlPowerLimit",
        1,
        0,
        vec![json!(20.125)],
    );
    assert_eq!(
        rig.call("configure_heat_limits", &[], json!({"power_w": 20.1}))
            .unwrap(),
        json!({"power_w": 20.125})
    );
    rig.inject(
        "COM_ESI_CTRL_SetHeatCtrlPowerLimit",
        1,
        0,
        vec![float(f64::NAN)],
    );
    assert!(
        rig.call("configure_heat_limits", &[], json!({"power_w": 20.0}))
            .is_err()
    );
}

#[test]
fn safe_off_retries_stuck_global_gate_once_and_does_not_confirm_on_uncertainty() {
    let mut rig = Rig::connected(true);
    rig.inject("COM_ESI_CTRL_GetEnable", 1, 0, vec![json!(true)]);
    rig.inject("COM_ESI_CTRL_GetEnable", 2, 0, vec![json!(true)]);
    assert!(
        rig.call("force_safe_off", &[], json!({}))
            .unwrap_err()
            .message
            .contains("remained ON")
    );
    assert_eq!(rig.count("COM_ESI_CTRL_SetEnable"), 2);
    assert_eq!(rig.count("COM_ESI_CTRL_Close"), 0);
    rig.clear_calls();
    rig.inject("COM_ESI_CTRL_GetEnable", 1, 0, vec![json!(true)]);
    assert_eq!(rig.call("force_safe_off", &[], json!({})).unwrap(), true);
    assert_eq!(rig.count("COM_ESI_CTRL_SetEnable"), 2);
}

#[test]
fn safe_off_attempts_remaining_outputs_after_returned_heater_error() {
    let mut rig = Rig::connected(true);
    rig.inject("COM_ESI_CTRL_SetModuleActivationState", 1, -10, vec![]);
    let error = rig.call("force_safe_off", &[], json!({})).unwrap_err();
    assert!(error.message.contains("safe OFF failed"));
    assert_eq!(rig.count("COM_ESI_CTRL_SetHVsupplyTargetOutputVoltage"), 2);
    assert_eq!(rig.count("COM_ESI_CTRL_SetHeatCtrlHeaterTemperature"), 1);
    assert_eq!(rig.count("COM_ESI_CTRL_SetEnable"), 1);
    assert_eq!(rig.controller.get_attribute("connected").unwrap(), true);
}

#[test]
fn communication_error_purges_next_transaction_without_replaying_failed_write() {
    let mut rig = Rig::connected(false);
    rig.inject("COM_ESI_CTRL_SetHVsupplyTargetOutputVoltage", 1, -7, vec![]);
    assert!(
        rig.call("set_hv_module_target", &[json!(1), json!(10.0)], json!({}))
            .is_err()
    );
    assert_eq!(rig.count("COM_ESI_CTRL_SetHVsupplyTargetOutputVoltage"), 1);
    assert_eq!(rig.count("COM_ESI_CTRL_Purge"), 0);
    assert!(
        rig.controller
            .get_attribute("_communication_error")
            .unwrap()
            .is_string()
    );
    rig.clear_calls();
    rig.call("get_heat_configuration", &[], json!({})).unwrap();
    assert_eq!(rig.calls()[0].0, "COM_ESI_CTRL_Purge");
    assert_eq!(rig.count("COM_ESI_CTRL_SetHVsupplyTargetOutputVoltage"), 0);
    assert_eq!(
        rig.controller
            .get_attribute("_communication_error")
            .unwrap(),
        Value::Null
    );
}

#[test]
fn late_native_reply_poisons_controller_and_never_continues_or_runs_cleanup() {
    let mut rig = Rig::connected(false);
    rig.state.lock().unwrap().delays.insert(
        "COM_ESI_CTRL_GetCompleteState".to_owned(),
        Duration::from_millis(30),
    );
    let error = rig
        .call(
            "set_output_active",
            &[json!(0), json!(true)],
            json!({"timeout_s": 0.01}),
        )
        .unwrap_err();
    assert_eq!(error.kind, "TimeoutError");
    assert_eq!(
        rig.controller.get_attribute("_transport_poisoned").unwrap(),
        true
    );
    let calls = rig.calls();
    for method in [
        "force_safe_off",
        "disconnect",
        "force_close_transport",
        "collect_diagnostics",
    ] {
        assert!(rig.call(method, &[], json!({})).is_err());
        assert_eq!(rig.calls(), calls);
    }
    assert_eq!(calls.last().unwrap().0, "COM_ESI_CTRL_GetCompleteState");
}

#[test]
fn every_esi_dll_call_emits_paired_watchdog_frames_with_the_io_budget() {
    let mut rig = Rig::connected(false);
    let (sender, receiver) = mpsc::sync_channel(128);
    let ctx = Context::new(
        Duration::from_secs(2),
        Arc::new(AtomicBool::new(false)),
        Some(sender),
    )
    .with_request_id(37);
    rig.controller
        .call(
            "set_output_active",
            &[json!(1), json!(false)],
            &json!({"timeout_s": 0.2}),
            &ctx,
        )
        .unwrap();
    let frames = receiver.try_iter().collect::<Vec<_>>();
    assert_eq!(frames.len(), 2 * rig.calls().len());
    for (pair, (symbol, _)) in frames.as_chunks::<2>().0.iter().zip(rig.calls()) {
        assert_eq!(pair[0]["kind"], "native_io");
        assert_eq!(pair[0]["id"], 37);
        assert_eq!(pair[0]["symbol"], symbol);
        assert_eq!(pair[0]["phase"], "enter");
        let limit = pair[0]["timeout_s"].as_f64().unwrap();
        assert!(limit > 0.0 && limit <= 0.2);
        assert_eq!(pair[1]["phase"], "exit");
        assert_eq!(pair[1]["sequence"], pair[0]["sequence"]);
        assert_eq!(pair[1]["symbol"], pair[0]["symbol"]);
    }
}

#[test]
fn cancelled_off_still_emits_watchdog_frames_and_confirms_module_off() {
    let mut rig = Rig::connected(true);
    let (sender, receiver) = mpsc::sync_channel(128);
    let ctx = Context::new(
        Duration::from_secs(2),
        Arc::new(AtomicBool::new(false)),
        Some(sender),
    )
    .with_request_id(38);
    ctx.cancel();
    assert_eq!(
        rig.controller
            .call(
                "set_output_active",
                &[json!(0), json!(false)],
                &json!({}),
                &ctx
            )
            .unwrap(),
        false
    );
    assert!(!rig.state.lock().unwrap().active[0]);
    let frames = receiver.try_iter().collect::<Vec<_>>();
    assert!(!frames.is_empty());
    assert_eq!(frames.len(), 2 * rig.calls().len());
    assert!(
        frames
            .iter()
            .all(|frame| frame["id"] == 38 && frame["kind"] == "native_io")
    );
}

#[test]
fn aggregate_operation_budget_bounds_multiple_individually_timely_dll_calls() {
    let mut rig = Rig::connected(false);
    for symbol in ["COM_ESI_CTRL_SetEnable", "COM_ESI_CTRL_GetEnable"] {
        rig.state
            .lock()
            .unwrap()
            .delays
            .insert(symbol.to_owned(), Duration::from_millis(300));
    }
    let (sender, receiver) = mpsc::sync_channel(16);
    let ctx = Context::new(
        Duration::from_millis(500),
        Arc::new(AtomicBool::new(false)),
        Some(sender),
    )
    .with_request_id(39);
    let error = rig
        .controller
        .call(
            "set_global_active",
            &[json!(true)],
            &json!({"timeout_s": 1.0}),
            &ctx,
        )
        .unwrap_err();
    assert_eq!(error.kind, "TimeoutError");
    assert_eq!(rig.calls().len(), 2);
    assert_eq!(
        rig.controller.get_attribute("_transport_poisoned").unwrap(),
        true
    );
    let frames = receiver.try_iter().collect::<Vec<_>>();
    assert_eq!(frames.len(), 4);
    let first = frames[0]["timeout_s"].as_f64().unwrap();
    let second = frames[2]["timeout_s"].as_f64().unwrap();
    assert!(first <= 0.5 && second < first);
    assert!(frames[3]["elapsed_s"].as_f64().unwrap() > second);
    let calls = rig.calls();
    assert!(rig.call("force_safe_off", &[], json!({})).is_err());
    assert_eq!(rig.calls(), calls);
}

#[test]
fn adc_selection_preserves_current_range_and_does_not_change_outputs() {
    let mut rig = Rig::connected(true);
    rig.call(
        "select_hv_voltage_adc",
        &[json!(1)],
        json!({"negative": false}),
    )
    .unwrap();
    assert_eq!(rig.state.lock().unwrap().ranges[1], (false, true));
    assert!(!rig.calls().iter().any(|(name, _)| output_command(name)));
    rig.clear_calls();
    assert_eq!(
        rig.call(
            "select_hv_measurement",
            &[json!(1)],
            json!({"negative": false, "high_current": true})
        )
        .unwrap(),
        true
    );
    assert!(rig.calls().is_empty());
    assert_eq!(
        int_key(
            &rig.controller
                .get_attribute("_hv_measurement_requests")
                .unwrap(),
            1
        ),
        &tuple(vec![json!(false), json!(true), json!(true)])
    );
}

#[test]
fn diagnostics_retains_gui_shape_units_flags_and_readback_confirmed_activity() {
    let mut rig = Rig::connected(true);
    rig.state.lock().unwrap().complete_states = VecDeque::from([0x8100]);
    let snapshot = rig.call("collect_diagnostics", &[], json!({})).unwrap();
    assert_eq!(
        snapshot["main_state"],
        json!({"hex": "0x0", "name": "STATE_ON"})
    );
    assert_eq!(
        snapshot["device_state"],
        json!({"hex": "0x0", "flags": ["DEVST_OK"]})
    );
    assert_eq!(snapshot["heat"]["active"], false);
    assert_eq!(snapshot["heat"]["module_active"], true);
    assert_eq!(snapshot["heat"]["module_gate_active"], false);
    assert_eq!(snapshot["heat"]["monitor_temperature_c"], 30.0);
    assert_eq!(snapshot["heat"]["hardware_limits"]["max_power_w"], 96.0);
    assert_eq!(snapshot["heat"]["housekeeping"]["temp_psu_c"], 26.0);
    let hv = int_key(&snapshot["modules"], 1);
    assert_eq!(hv["measurement"]["voltage_polarity"], "negative");
    assert_eq!(hv["measurement"]["voltage_fresh"], true);
    assert_eq!(hv["measured_v"], -0.5);
    assert_eq!(hv["measured_a"], -2e-9);
    assert_eq!(hv["pwm"]["phase_set_s"], 0.02);
    assert_eq!(snapshot["global_active"], true);
    assert!(!rig.calls().iter().any(|(name, _)| output_command(name)));
    rig.state.lock().unwrap().complete_states.clear();
    assert_eq!(
        rig.call("collect_diagnostics", &[], json!({})).unwrap()["heat"]["active"],
        true
    );
}

#[test]
fn diagnostics_preserves_nonfinite_invalid_telemetry_without_cache_or_retry() {
    let mut rig = Rig::connected(true);
    {
        let mut state = rig.state.lock().unwrap();
        state.heat_valid = false;
        state.heat_temperature = f64::NAN;
        state.voltage_valid = false;
        state.voltages[1][1] = f64::INFINITY;
    }
    let snapshot = rig.call("collect_diagnostics", &[], json!({})).unwrap();
    assert_eq!(snapshot["heat"]["valid"], false);
    assert_eq!(snapshot["heat"]["monitor_temperature_c"], float(f64::NAN));
    assert_eq!(int_key(&snapshot["modules"], 1)["voltage_valid"], false);
    assert_eq!(
        int_key(&snapshot["modules"], 1)["measured_v"],
        float(f64::INFINITY)
    );
    assert_eq!(rig.count("COM_ESI_CTRL_GetHeatCtrlMonitoring"), 1);
}

#[test]
fn raw_notebook_and_internal_methods_are_explicitly_unsupported_not_success_stubs() {
    let mut rig = Rig::connected(false);
    for method in [
        "get_heat_configuration_unlocked",
        "get_complete_state",
        "open_port",
        "restart",
        "_verify_hv_discharge",
        "wait_for_idle",
        "close",
    ] {
        assert_eq!(
            rig.call(method, &[], json!({})).unwrap_err().kind,
            "NotImplementedError"
        );
    }
    assert!(rig.calls().is_empty());
    for method in [
        "connect",
        "force_safe_off",
        "collect_diagnostics",
        "set_output_active",
        "disconnect",
        "load_config",
    ] {
        assert!(SUPPORTED_METHODS.contains(&method));
    }
}

#[test]
fn argument_errors_cannot_become_successful_operations() {
    let mut rig = Rig::connected(false);
    for (method, args, kwargs) in [
        ("set_output_active", vec![json!(0)], json!({})),
        (
            "set_global_active",
            vec![json!(true)],
            json!({"active": true}),
        ),
        ("force_safe_off", vec![], json!({"unexpected": true})),
        ("collect_diagnostics", vec![], json!({"timeout_s": 0})),
        ("connect", vec![], json!({"timeout_s": {"$float": "nan"}})),
        (
            "disconnect",
            vec![],
            json!({"on_discharge": "serialized callback"}),
        ),
    ] {
        assert!(rig.call(method, &args, kwargs).is_err());
    }
    assert!(rig.calls().is_empty());
}

#[test]
fn runtime_attributes_validate_configuration_and_never_allow_forged_state() {
    let mut rig = Rig::new();
    rig.controller.set_attribute("com", json!(15)).unwrap();
    rig.controller
        .set_attribute("baudrate", json!(115200))
        .unwrap();
    rig.controller
        .set_attribute("_DEFAULT_IO_TIMEOUT_S", json!(2.0))
        .unwrap();
    assert_eq!(rig.controller.get_attribute("com").unwrap(), 15);
    assert_eq!(
        rig.controller
            .get_attribute("_DEFAULT_IO_TIMEOUT_S")
            .unwrap(),
        2.0
    );
    for (name, value) in [
        ("connected", json!(true)),
        ("_transport_poisoned", json!(false)),
        ("DISCHARGE_SAMPLES", json!(1)),
        ("DISCHARGE_LIMIT_V", json!(2.0)),
        ("DISCHARGE_TIMEOUT_S", json!(0)),
    ] {
        assert!(rig.controller.set_attribute(name, value).is_err());
    }
    rig.call("connect", &[], json!({})).unwrap();
    assert!(rig.controller.set_attribute("com", json!(14)).is_err());
    assert_eq!(
        rig.controller
            .get_attribute("HEATER_SAFETY_CONTRACT")
            .unwrap(),
        1
    );
    assert_eq!(
        rig.call("format_status", &[json!(-10)], json!({})).unwrap(),
        "-10 (Error receiving command)"
    );
}

#[test]
fn configure_steps_changes_only_both_step_fields_and_verifies_safe_readback() {
    let mut rig = Rig::connected(false);
    let original = {
        let mut state = rig.state.lock().unwrap();
        state.config[8] = 31;
        state.config[50] = 77;
        state.config.clone()
    };
    let applied = rig
        .call("configure_hv_max_voltage_steps", &[], json!({}))
        .unwrap();
    assert_eq!(int_key(&applied, 1), &json!(10.008));
    assert_eq!(int_key(&applied, 2), &json!(10.008));
    let modified = rig.state.lock().unwrap().config.clone();
    for index in 0..53 {
        if !(21..25).contains(&index) && !(33..37).contains(&index) {
            assert_eq!(modified[index], original[index], "byte {index}");
        }
    }
    assert_eq!(
        i32::from_le_bytes(modified[21..25].try_into().unwrap()),
        10008
    );
    assert_eq!(
        i32::from_le_bytes(modified[33..37].try_into().unwrap()),
        10008
    );
    assert_eq!(rig.count("COM_ESI_CTRL_SetCurrentConfig"), 1);
    rig.clear_calls();
    rig.call("configure_hv_max_voltage_steps", &[], json!({}))
        .unwrap();
    assert_eq!(rig.count("COM_ESI_CTRL_SetCurrentConfig"), 0);
    assert_eq!(rig.count("COM_ESI_CTRL_GetCurrentConfig"), 2);
}

#[test]
fn configure_steps_rejects_unsafe_snapshot_precision_and_corrupt_readback() {
    for unsafe_byte in [0, 17, 29, 27, 39] {
        let mut rig = Rig::connected(false);
        rig.state.lock().unwrap().config[unsafe_byte] = 1;
        assert!(
            rig.call("configure_hv_max_voltage_steps", &[], json!({}))
                .unwrap_err()
                .message
                .contains("not safely OFF")
        );
        assert_eq!(rig.count("COM_ESI_CTRL_SetCurrentConfig"), 0);
    }
    let mut rig = Rig::connected(false);
    for step in [json!(0), json!(3001.0), json!(0.0005), float(f64::NAN)] {
        assert!(
            rig.call("configure_hv_max_voltage_steps", &[step], json!({}))
                .is_err()
        );
    }
    assert!(rig.calls().is_empty());
    rig.state.lock().unwrap().config_corrupt = true;
    assert!(
        rig.call("configure_hv_max_voltage_steps", &[], json!({}))
            .unwrap_err()
            .message
            .contains("byte offsets")
    );
}

#[test]
fn configure_steps_cannot_succeed_when_runtime_gates_or_targets_leave_safe_off() {
    for failure in ["global", "target", "module"] {
        let mut rig = Rig::connected(false);
        {
            let mut state = rig.state.lock().unwrap();
            match failure {
                "global" => state.enabled = true,
                "target" => state.target[1] = 10.0,
                _ => state.active[2] = true,
            }
        }
        assert!(
            rig.call("configure_hv_max_voltage_steps", &[], json!({}))
                .is_err(),
            "{failure}"
        );
    }
}

#[test]
fn configuration_list_reads_sparse_slots_and_empty_slots_with_correct_buffers() {
    let mut rig = Rig::connected(true);
    assert_eq!(
        rig.call("list_configs", &[], json!({})).unwrap(),
        json!([
            {"index": 10, "name": "Spray-2kV", "active": true, "valid": true},
            {"index": 12, "name": "Test-100V", "active": true, "valid": false},
        ])
    );
    let configs = rig.call("list_configs", &[json!(true)], json!({})).unwrap();
    assert_eq!(configs.as_array().unwrap().len(), 1023);
    assert_eq!(
        configs[1022],
        json!({"index": 1022, "name": "", "active": false, "valid": false})
    );
    assert!(!rig.calls().iter().any(|(name, _)| output_command(name)));
}

#[test]
fn load_slot_forces_off_before_and_after_load_and_reapplies_volatile_steps() {
    let mut rig = Rig::connected(true);
    assert_eq!(
        rig.call("load_config", &[json!(10)], json!({})).unwrap(),
        Value::Null
    );
    assert_eq!(rig.count("COM_ESI_CTRL_LoadCurrentConfig"), 1);
    assert_eq!(rig.count("COM_ESI_CTRL_SetCurrentConfig"), 1);
    assert_eq!(rig.count("COM_ESI_CTRL_SetHVsupplyTargetOutputVoltage"), 4);
    let calls = rig.calls();
    let load = calls
        .iter()
        .position(|(name, _)| name == "COM_ESI_CTRL_LoadCurrentConfig")
        .unwrap();
    assert!(
        calls[..load]
            .iter()
            .any(|(name, args)| name == "COM_ESI_CTRL_SetEnable" && args[0] == false)
    );
    assert!(
        calls[load + 1..]
            .iter()
            .any(|(name, args)| name == "COM_ESI_CTRL_SetEnable" && args[0] == false)
    );
    let state = rig.state.lock().unwrap();
    assert!(!state.enabled);
    assert_eq!(state.active, [false; 3]);
    assert_eq!(
        i32::from_le_bytes(state.config[21..25].try_into().unwrap()),
        10008
    );
}

#[test]
fn load_and_save_reject_out_of_range_slots_before_any_io() {
    let mut rig = Rig::connected(false);
    for method in ["load_config", "save_config"] {
        for slot in [json!(-1), json!(1023), json!(true), json!(2.5)] {
            assert_eq!(
                rig.call(method, &[slot], json!({})).unwrap_err().kind,
                "ValueError"
            );
        }
    }
    assert!(rig.calls().is_empty());
}

#[test]
fn save_slot_applies_ascii_truncated_name_and_independent_flags() {
    let mut rig = Rig::connected(false);
    let name = format!("{}{}", "X".repeat(200), char::from_u32(0xe9).unwrap());
    assert_eq!(
        rig.call(
            "save_config",
            &[json!(42)],
            json!({"name": name, "active": false})
        )
        .unwrap(),
        Value::Null
    );
    let calls = rig.calls();
    let set_name = calls
        .iter()
        .find(|(name, _)| name == "COM_ESI_CTRL_SetConfigName")
        .unwrap();
    assert_eq!(set_name.1[1]["text"], format!("{}?", "X".repeat(200)));
    assert_eq!(
        calls.last().unwrap(),
        &(
            "COM_ESI_CTRL_SetConfigFlags".to_owned(),
            vec![json!(42), json!(false), json!(true)]
        )
    );
    rig.clear_calls();
    rig.inject("COM_ESI_CTRL_SaveCurrentConfig", 1, -15, vec![]);
    assert!(
        rig.call(
            "save_config",
            &[json!(42)],
            json!({"name": "Test", "active": true})
        )
        .is_err()
    );
    assert_eq!(rig.count("COM_ESI_CTRL_SetConfigName"), 0);
    assert_eq!(rig.count("COM_ESI_CTRL_SetConfigFlags"), 0);
}

#[test]
fn malformed_fixed_buffers_are_errors_not_truncated_or_defaulted_data() {
    for (symbol, values, method) in [
        (
            "COM_ESI_CTRL_GetCurrentConfig",
            vec![json!(vec![0; 52])],
            "configure_hv_max_voltage_steps",
        ),
        (
            "COM_ESI_CTRL_GetConfigList",
            vec![json!(vec![false; 1022]), json!(vec![false; 1023])],
            "list_configs",
        ),
        (
            "COM_ESI_CTRL_GetCompleteState",
            vec![
                json!(0),
                json!(0),
                json!(0),
                json!(0),
                json!(0),
                json!(0),
                json!(0),
                json!(vec![0; 4]),
                json!(vec![0; 5]),
            ],
            "collect_diagnostics",
        ),
    ] {
        let mut rig = Rig::connected(false);
        rig.inject(symbol, 1, 0, values);
        assert!(rig.call(method, &[], json!({})).is_err(), "{symbol}");
    }
}

#[test]
fn disconnect_requires_three_fresh_rounds_on_all_four_outputs_then_closes() {
    let mut rig = Rig::connected(true);
    let original_ranges = rig.state.lock().unwrap().ranges;
    let (sender, receiver) = mpsc::sync_channel(64);
    let ctx = Context::new(
        Duration::from_secs(3),
        Arc::new(AtomicBool::new(false)),
        Some(sender),
    );
    assert_eq!(
        rig.controller
            .call("disconnect", &[], &json!({}), &ctx)
            .unwrap(),
        true
    );
    let reports = receiver.try_iter().collect::<Vec<_>>();
    let last = reports.last().unwrap();
    assert_eq!(last["consecutive"], 3);
    assert_eq!(last["limit_v"], 1.0);
    for address in [1, 2] {
        let values = int_key(&last["modules"], address);
        assert_eq!(values["positive_v"], 0.5);
        assert_eq!(values["negative_v"], -0.5);
        assert_eq!(values["measured_a"], -2e-9);
    }
    let state = rig.state.lock().unwrap();
    assert_eq!(state.ranges, original_ranges);
    assert_eq!(state.fresh_counts[1], [3, 3]);
    assert_eq!(state.fresh_counts[2], [3, 3]);
    assert_eq!(state.calls.last().unwrap().0, "COM_ESI_CTRL_Close");
    assert!(
        !state
            .calls
            .iter()
            .any(|(symbol, args)| symbol == "COM_ESI_CTRL_SetEnable" && args[0] == true)
    );
    drop(state);
    assert_eq!(rig.controller.get_attribute("connected").unwrap(), false);
    assert_eq!(
        rig.controller.get_attribute("_dll_port_claimed").unwrap(),
        false
    );
    assert_eq!(
        rig.controller
            .get_attribute("_hv_measurement_requests")
            .unwrap(),
        json!({"$map": []})
    );
}

#[test]
fn discharge_exact_voltage_boundary_is_inclusive() {
    let mut rig = Rig::connected(false);
    rig.state.lock().unwrap().voltages = [[1.0, -1.0]; 3];
    assert_eq!(rig.call("disconnect", &[], json!({})).unwrap(), true);
}

#[test]
fn stuck_ready_or_absent_updates_never_prove_discharge_even_with_valid_zero() {
    for sticky in [false, true] {
        let mut rig = Rig::connected(false);
        {
            let mut state = rig.state.lock().unwrap();
            state.adc_sticky = sticky;
            state.adc_never_ready = !sticky;
            state.voltages = [[0.0; 2]; 3];
        }
        rig.controller
            .set_attribute("DISCHARGE_TIMEOUT_S", json!(0.12))
            .unwrap();
        let error = rig.call("disconnect", &[], json!({})).unwrap_err();
        assert!(error.message.contains("discharge"), "{error}");
        assert_eq!(rig.count("COM_ESI_CTRL_Close"), 0);
        assert_eq!(rig.controller.get_attribute("connected").unwrap(), true);
        assert_eq!(
            rig.controller.get_attribute("_dll_port_claimed").unwrap(),
            true
        );
        assert_eq!(
            rig.controller.get_attribute("_transport_poisoned").unwrap(),
            false
        );
    }
}

#[test]
fn invalid_or_nonfinite_adc_voltage_and_current_prevent_close() {
    for failure in [
        "voltage_invalid",
        "current_invalid",
        "nan",
        "inf",
        "current_nan",
    ] {
        let mut rig = Rig::connected(false);
        {
            let mut state = rig.state.lock().unwrap();
            match failure {
                "voltage_invalid" => state.voltage_valid = false,
                "current_invalid" => state.current_valid = false,
                "nan" => state.voltages[1][0] = f64::NAN,
                "inf" => state.voltages[1][0] = f64::INFINITY,
                _ => state.current = f64::NAN,
            }
        }
        assert!(
            rig.call("disconnect", &[], json!({}))
                .unwrap_err()
                .message
                .contains("invalid ADC"),
            "{failure}"
        );
        assert_eq!(rig.count("COM_ESI_CTRL_Close"), 0);
        assert_eq!(rig.controller.get_attribute("connected").unwrap(), true);
    }
}

#[test]
fn one_high_polarity_on_either_module_prevents_shutdown_confirmation() {
    for (address, negative) in [(1, false), (1, true), (2, false), (2, true)] {
        let mut rig = Rig::connected(false);
        rig.state.lock().unwrap().voltages[address][usize::from(negative)] =
            if negative { -1.01 } else { 1.01 };
        rig.controller
            .set_attribute("DISCHARGE_TIMEOUT_S", json!(0.25))
            .unwrap();
        assert!(rig.call("disconnect", &[], json!({})).is_err());
        assert_eq!(rig.count("COM_ESI_CTRL_Close"), 0);
        assert_eq!(
            rig.controller.get_attribute("_dll_port_claimed").unwrap(),
            true
        );
    }
}

#[test]
fn high_voltage_round_resets_consecutive_confirmation_count() {
    let mut rig = Rig::connected(false);
    rig.state
        .lock()
        .unwrap()
        .voltage_plan
        .insert((2, true), VecDeque::from([-0.5, -2.0, -0.5, -0.5, -0.5]));
    let (sender, receiver) = mpsc::sync_channel(128);
    let ctx = Context::new(
        Duration::from_secs(3),
        Arc::new(AtomicBool::new(false)),
        Some(sender),
    );
    rig.controller
        .call("disconnect", &[], &json!({}), &ctx)
        .unwrap();
    let counts = receiver
        .try_iter()
        .map(|report| report["consecutive"].as_u64().unwrap())
        .collect::<Vec<_>>();
    assert!(counts.windows(2).any(|pair| pair[0] > 0 && pair[1] == 0));
    assert_eq!(*counts.last().unwrap(), 3);
    assert_eq!(rig.state.lock().unwrap().fresh_counts[2][1], 5);
}

#[test]
fn cancellation_during_discharge_keeps_safe_off_attempts_and_ownership_not_success() {
    let mut rig = Rig::connected(true);
    let ctx = Context::test(2.0);
    ctx.cancel();
    assert!(
        rig.controller
            .call("disconnect", &[], &json!({}), &ctx)
            .is_err()
    );
    let state = rig.state.lock().unwrap();
    assert_eq!(state.active, [false; 3]);
    assert!(!state.enabled);
    drop(state);
    assert_eq!(rig.count("COM_ESI_CTRL_Close"), 0);
    assert_eq!(rig.controller.get_attribute("connected").unwrap(), true);
    assert_eq!(
        rig.controller.get_attribute("_dll_port_claimed").unwrap(),
        true
    );
}

#[test]
fn discharge_failure_restores_mux_unless_communication_failed_or_native_reply_was_late() {
    let mut rig = Rig::connected(false);
    let original = rig.state.lock().unwrap().ranges;
    rig.state.lock().unwrap().voltage_valid = false;
    assert!(rig.call("disconnect", &[], json!({})).is_err());
    assert_eq!(rig.state.lock().unwrap().ranges, original);
    rig.state.lock().unwrap().voltage_valid = true;
    rig.clear_calls();
    rig.inject(
        "COM_ESI_CTRL_GetHVsupplyOutputVoltage",
        1,
        -10,
        vec![json!(false), json!(0)],
    );
    assert!(rig.call("disconnect", &[], json!({})).is_err());
    assert_eq!(
        rig.calls().last().unwrap().0,
        "COM_ESI_CTRL_GetHVsupplyOutputVoltage"
    );
    assert_eq!(rig.count("COM_ESI_CTRL_Close"), 0);
}

#[test]
fn late_adc_response_prevents_final_confirmation_and_all_cleanup() {
    let mut rig = Rig::connected(false);
    rig.state.lock().unwrap().delays.insert(
        "COM_ESI_CTRL_GetHVsupplyOutputCurrent".to_owned(),
        Duration::from_millis(30),
    );
    assert_eq!(
        rig.call("disconnect", &[], json!({"timeout_s": 0.01}))
            .unwrap_err()
            .kind,
        "TimeoutError"
    );
    assert_eq!(
        rig.calls().last().unwrap().0,
        "COM_ESI_CTRL_GetHVsupplyOutputCurrent"
    );
    assert_eq!(rig.count("COM_ESI_CTRL_Close"), 0);
    assert_eq!(
        rig.controller.get_attribute("_transport_poisoned").unwrap(),
        true
    );
}

#[test]
fn failed_close_keeps_connection_and_port_ownership_and_forced_release_has_no_purge() {
    let mut rig = Rig::connected(false);
    rig.inject("COM_ESI_CTRL_Close", 1, -3, vec![]);
    assert!(rig.call("disconnect", &[], json!({})).is_err());
    assert_eq!(rig.controller.get_attribute("connected").unwrap(), true);
    assert_eq!(
        rig.controller.get_attribute("_dll_port_claimed").unwrap(),
        true
    );
    rig.inject(
        "COM_ESI_CTRL_GetHeatCtrlVoltageLimit",
        1,
        -10,
        vec![json!(0)],
    );
    assert!(rig.call("get_heat_configuration", &[], json!({})).is_err());
    rig.clear_calls();
    assert_eq!(
        rig.call("force_close_transport", &[], json!({})).unwrap(),
        true
    );
    assert_eq!(rig.calls(), vec![("COM_ESI_CTRL_Close".to_owned(), vec![])]);
}

#[test]
fn forced_transport_release_is_not_hardware_off_confirmation_and_reconnect_can_preserve_outputs() {
    let mut rig = Rig::connected(true);
    assert_eq!(
        rig.call("force_close_transport", &[], json!({})).unwrap(),
        true
    );
    assert_eq!(rig.calls(), vec![("COM_ESI_CTRL_Close".to_owned(), vec![])]);
    let state = rig.state.lock().unwrap();
    assert!(state.enabled);
    assert_eq!(state.active, [true; 3]);
    drop(state);
    rig.clear_calls();
    rig.call("connect", &[], json!({"preserve_outputs": true}))
        .unwrap();
    assert!(!rig.calls().iter().any(|(symbol, _)| output_command(symbol)));
}

#[test]
fn failed_initial_open_attempt_only_issues_local_close_not_instrument_commands() {
    let mut rig = Rig::new();
    rig.inject("COM_ESI_CTRL_Open", 1, -2, vec![]);
    assert!(rig.call("connect", &[], json!({})).is_err());
    assert_eq!(
        rig.calls(),
        vec![
            ("COM_ESI_CTRL_Open".to_owned(), vec![json!(14)]),
            ("COM_ESI_CTRL_Close".to_owned(), vec![])
        ]
    );
    assert_eq!(rig.controller.get_attribute("_open_failed").unwrap(), true);
    assert_eq!(
        rig.controller
            .get_attribute("_failed_open_released")
            .unwrap(),
        true
    );
    assert_eq!(
        rig.controller.get_attribute("_dll_port_claimed").unwrap(),
        false
    );
}

#[test]
fn parent_injected_dll_path_and_sha256_are_accepted_and_read_only() {
    let hash = "a".repeat(64);
    let mut controller = Controller::new(&json!({"device_id": "esi", "com": 14, "dll_path": "/absolute/vendor.dll", "dll_sha256": hash}), Box::new(MockDll(Arc::new(Mutex::new(Instrument::default()))))).unwrap();
    assert_eq!(
        controller.get_attribute("esi_dll_path").unwrap(),
        "/absolute/vendor.dll"
    );
    assert_eq!(controller.get_attribute("dll_sha256").unwrap(), hash);
    assert!(
        controller
            .set_attribute("dll_sha256", json!("b".repeat(64)))
            .is_err()
    );
}

#[test]
fn reconnect_opens_before_pending_purge_and_idle_disconnect_does_not_purge() {
    let mut rig = Rig::connected(false);
    rig.inject("COM_ESI_CTRL_SetHVsupplyTargetOutputVoltage", 1, -7, vec![]);
    assert!(
        rig.call("set_hv_module_target", &[json!(1), json!(10.0)], json!({}))
            .is_err()
    );
    rig.call("force_close_transport", &[], json!({})).unwrap();
    rig.clear_calls();
    assert_eq!(rig.call("disconnect", &[], json!({})).unwrap(), true);
    assert!(rig.calls().is_empty());
    rig.call("connect", &[], json!({})).unwrap();
    let calls = rig.calls();
    assert_eq!(calls[0].0, "COM_ESI_CTRL_Open");
    assert_eq!(calls[1].0, "COM_ESI_CTRL_Purge");
    assert_eq!(calls[2].0, "COM_ESI_CTRL_SetBaudRate");
}

#[test]
fn failed_purge_prevents_next_operation_and_retains_recovery_requirement() {
    let mut rig = Rig::connected(false);
    rig.inject("COM_ESI_CTRL_SetHVsupplyTargetOutputVoltage", 1, -7, vec![]);
    assert!(
        rig.call("set_hv_module_target", &[json!(1), json!(10.0)], json!({}))
            .is_err()
    );
    rig.clear_calls();
    rig.inject("COM_ESI_CTRL_Purge", 1, -4, vec![]);
    assert!(rig.call("get_heat_configuration", &[], json!({})).is_err());
    assert_eq!(rig.calls(), vec![("COM_ESI_CTRL_Purge".to_owned(), vec![])]);
    assert!(
        rig.controller
            .get_attribute("_communication_error")
            .unwrap()
            .is_string()
    );
    rig.clear_calls();
    rig.call("get_heat_configuration", &[], json!({})).unwrap();
    assert_eq!(rig.calls()[0].0, "COM_ESI_CTRL_Purge");
}

#[test]
fn readback_failures_for_off_are_not_success_and_do_not_close_transport() {
    for symbol in [
        "COM_ESI_CTRL_GetModuleActivationState",
        "COM_ESI_CTRL_GetHeatCtrlHeaterTemperature",
        "COM_ESI_CTRL_GetEnable",
    ] {
        let mut rig = Rig::connected(true);
        rig.inject(symbol, 1, -15, vec![json!(0)]);
        assert!(rig.call("disconnect", &[], json!({})).is_err(), "{symbol}");
        assert_eq!(rig.count("COM_ESI_CTRL_Close"), 0);
        assert_eq!(rig.controller.get_attribute("connected").unwrap(), true);
        assert_eq!(
            rig.controller.get_attribute("_dll_port_claimed").unwrap(),
            true
        );
    }
}

#[test]
fn strict_native_boolean_and_state_shapes_cannot_produce_success() {
    let mut rig = Rig::connected(false);
    rig.inject("COM_ESI_CTRL_GetEnable", 1, 0, vec![json!(1)]);
    assert!(
        rig.call("set_global_active", &[json!(true)], json!({}))
            .is_err()
    );
    rig.inject(
        "COM_ESI_CTRL_GetModuleActivationState",
        1,
        0,
        vec![json!(1)],
    );
    assert!(
        rig.call("set_output_active", &[json!(0), json!(true)], json!({}))
            .is_err()
    );
    rig.inject("COM_ESI_CTRL_GetCompleteState", 1, 0, vec![json!(0)]);
    assert!(rig.call("collect_diagnostics", &[], json!({})).is_err());
}

#[test]
fn every_gui_hardware_method_has_explicit_dispatch_and_other_operations_stay_in_supervisor() {
    let methods = [
        "connect",
        "collect_identity",
        "collect_diagnostics",
        "set_global_active",
        "configure_heat_limits",
        "force_safe_off",
        "configure_hv_max_voltage_steps",
        "select_hv_voltage_adc",
        "list_configs",
        "load_config",
        "set_heater_temperature",
        "get_heat_configuration",
        "set_output_active",
        "set_hv_module_target",
        "disconnect",
        "force_close_transport",
    ];
    let mut rig = Rig::new();
    for method in methods {
        assert!(SUPPORTED_METHODS.contains(&method), "GUI method {method}");
        if let Err(error) = rig.call(method, &[], json!({})) {
            assert_ne!(error.kind, "NotImplementedError", "GUI method {method}");
        }
    }
    assert!(!SUPPORTED_METHODS.contains(&"close"));
    assert!(!SUPPORTED_METHODS.contains(&"wait_for_idle"));
}

#[test]
fn malformed_hv_activation_reply_is_not_retried_as_a_vendor_error() {
    let mut rig = Rig::connected(false);
    rig.inject(
        "COM_ESI_CTRL_GetHVsupplyParamsPWM",
        1,
        0,
        vec![
            json!(1),
            json!(0),
            json!(0),
            json!(0),
            json!(0),
            json!(0),
            json!(1),
            json!(0),
        ],
    );
    assert!(
        rig.call("set_output_active", &[json!(1), json!(true)], json!({}))
            .is_err()
    );
    assert_eq!(rig.count("COM_ESI_CTRL_GetHVsupplyParamsPWM"), 1);
    assert_eq!(rig.count("COM_ESI_CTRL_SetEnable"), 0);
}
