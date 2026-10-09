#![cfg(feature = "ampr")]

use esibd_native_worker::{
    Backend,
    ampr::{Controller, GUI_ATTRIBUTES, GUI_METHODS},
    codec::float,
    context::Context,
    error::Result,
    ffi::{Dll, NativeReply},
};
use serde_json::{Value, json};
use std::collections::VecDeque;
use std::sync::{Arc, Mutex};

#[derive(Clone)]
struct Step {
    export: &'static str,
    args: Option<Value>,
    status: i64,
    values: Vec<Value>,
    cancel: Option<Context>,
}
fn step(export: &'static str, status: i64, values: Vec<Value>) -> Step {
    Step {
        export,
        args: None,
        status,
        values,
        cancel: None,
    }
}
fn exact(export: &'static str, args: Value, status: i64, values: Vec<Value>) -> Step {
    Step {
        args: Some(args),
        ..step(export, status, values)
    }
}

#[derive(Default)]
struct Script {
    steps: VecDeque<Step>,
    calls: Vec<(String, Vec<Value>)>,
}
struct Mock(Arc<Mutex<Script>>);
impl Dll for Mock {
    fn call(&mut self, symbol: &str, args: &[Value]) -> Result<NativeReply> {
        let mut script = self.0.lock().unwrap();
        script.calls.push((symbol.into(), args.to_vec()));
        let expected = script
            .steps
            .pop_front()
            .unwrap_or_else(|| panic!("Unexpected native call {symbol} {args:?}"));
        assert_eq!(symbol, format!("COM_AMPR_12_{}", expected.export));
        if let Some(expected_args) = expected.args {
            assert_eq!(json!(args), expected_args, "{symbol} pointer seeds/inputs");
        }
        if let Some(context) = expected.cancel {
            context.cancel();
        }
        Ok(NativeReply {
            status: expected.status,
            values: expected.values,
        })
    }
}
fn controller(steps: Vec<Step>) -> (Controller, Arc<Mutex<Script>>) {
    let shared = Arc::new(Mutex::new(Script {
        steps: steps.into(),
        calls: vec![],
    }));
    let controller = Controller::new(
        &json!({"device_id": "test", "com": 5}),
        Box::new(Mock(shared.clone())),
    )
    .unwrap();
    (controller, shared)
}
fn call(c: &mut Controller, method: &str, args: Vec<Value>) -> Result<Value> {
    c.call(method, &args, &json!({}), &Context::test(5.0))
}
fn connect_steps() -> Vec<Step> {
    vec![
        exact("Open", json!([5]), 0, vec![]),
        exact("SetBaudRate", json!([230400]), 0, vec![json!(230400)]),
        step("GetDevType", 0, vec![json!(0xA3D8)]),
    ]
}
fn completed(script: &Arc<Mutex<Script>>) {
    assert!(
        script.lock().unwrap().steps.is_empty(),
        "Unconsumed mock DLL calls"
    );
}
fn tuple(value: &Value) -> &[Value] {
    value["$tuple"].as_array().unwrap()
}
fn map(value: &Value, key: u64) -> &Value {
    &value["$map"]
        .as_array()
        .unwrap()
        .iter()
        .find(|entry| entry[0] == json!(key))
        .unwrap()[1]
}
fn metadata(address: u64, text: &str, product: u64, hw: u64) -> Vec<Step> {
    vec![
        exact("GetModuleFwVersion", json!([address, 0]), 0, vec![json!(1)]),
        step("GetModuleProductNo", 0, vec![json!(product)]),
        step("GetModuleProductID", 0, vec![json!(text)]),
        step("GetModuleHwType", 0, vec![json!(hw)]),
        step("GetModuleHwVersion", 0, vec![json!(2)]),
        step("GetModuleState", 0, vec![json!(0)]),
    ]
}
fn scan_steps(address: u64, text: &str) -> Vec<Step> {
    let mut presence = vec![0; 13];
    presence[address as usize] = 1;
    presence[12] = 1;
    let mut steps = vec![exact(
        "GetModulePresence",
        json!([false, 0, vec![0; 13]]),
        0,
        vec![json!(true), json!(12), json!(presence)],
    )];
    steps.extend(metadata(address, text, 9, 8));
    steps
}

#[test]
fn constructor_rejects_wrong_types_nonfinite_intervals_and_out_of_range_values_before_dll() {
    for (field, invalid, kind) in [
        ("device_id", json!(true), "TypeError"),
        ("device_id", json!("  "), "ValueError"),
        ("com", json!(true), "TypeError"),
        ("com", json!(0), "ValueError"),
        ("com", json!(256), "ValueError"),
        ("baudrate", json!(0), "ValueError"),
        ("baudrate", json!(4294967296u64), "ValueError"),
        ("hk_interval_s", json!(false), "TypeError"),
        ("hk_interval_s", float(f64::NAN), "ValueError"),
        ("hk_interval_s", float(f64::INFINITY), "ValueError"),
        ("hk_interval_s", json!(1e308), "ValueError"),
    ] {
        let mut config = json!({"device_id": "test", "com": 5});
        config[field] = invalid;
        let script = Arc::new(Mutex::new(Script::default()));
        let error = Controller::new(&config, Box::new(Mock(script.clone())))
            .err()
            .expect("Invalid config accepted");
        assert_eq!(error.kind, kind, "{field}");
        assert!(script.lock().unwrap().calls.is_empty());
    }
}

#[test]
fn connect_verifies_identity_is_idempotent_and_protects_connection_attributes() {
    let (mut c, script) = controller(connect_steps());
    assert_eq!(call(&mut c, "connect", vec![]).unwrap(), json!(true));
    assert_eq!(call(&mut c, "connect", vec![]).unwrap(), json!(true));
    assert_eq!(c.get_attribute("connected").unwrap(), json!(true));
    assert_eq!(
        c.set_attribute("connected", json!(false)).unwrap_err().kind,
        "AttributeError"
    );
    assert_eq!(
        c.set_attribute("com", json!(6)).unwrap_err().kind,
        "RuntimeError"
    );
    completed(&script);
}

#[test]
fn wrong_instrument_rolls_back_without_enabling_psu() {
    let mut steps = connect_steps();
    steps[2].values = vec![json!(0xAC38)];
    steps.push(step("Close", 0, vec![]));
    let (mut c, script) = controller(steps);
    assert!(
        call(&mut c, "connect", vec![])
            .unwrap_err()
            .message
            .contains("device type mismatch")
    );
    assert_eq!(c.get_attribute("connected").unwrap(), json!(false));
    assert_eq!(c.get_attribute("_dll_port_claimed").unwrap(), json!(false));
    completed(&script);
    assert_eq!(
        c.get_attribute("_failed_open_released").unwrap(),
        json!(true)
    );
    assert_eq!(
        c.get_attribute("_failed_open_cleanup_outcome").unwrap(),
        json!({"$tuple": ["result", 0]})
    );
}

#[test]
fn failed_open_keeps_pending_claim_until_close_is_confirmed() {
    let (mut c, script) = controller(vec![
        step("Open", -2, vec![]),
        step("Close", -3, vec![]),
        step("Close", 0, vec![]),
    ]);
    assert!(call(&mut c, "connect", vec![]).is_err());
    assert_eq!(c.get_attribute("_open_failed").unwrap(), json!(true));
    assert_eq!(
        c.get_attribute("_failed_open_released").unwrap(),
        json!(false)
    );
    assert_eq!(
        c.get_attribute("_failed_open_cleanup_outcome").unwrap(),
        json!({"$tuple": ["result", -3]})
    );
    assert!(
        call(&mut c, "connect", vec![])
            .unwrap_err()
            .message
            .contains("not been released")
    );
    assert_eq!(call(&mut c, "disconnect", vec![]).unwrap(), json!(true));
    assert_eq!(c.get_attribute("_open_failed").unwrap(), json!(false));
    completed(&script);
    assert_eq!(
        c.get_attribute("_failed_open_released").unwrap(),
        json!(true)
    );
    assert_eq!(
        c.get_attribute("_failed_open_cleanup_outcome").unwrap(),
        json!({"$tuple": ["result", 0]})
    );
}

#[test]
fn facade_accepts_positional_timeouts_and_rejects_nonfinite_budgets_before_io() {
    let (mut c, script) = controller(vec![step("GetState", 0, vec![json!(0)])]);
    for method in ["get_state", "scan_modules", "initialize"] {
        assert_eq!(
            call(&mut c, method, vec![float(f64::NAN)])
                .unwrap_err()
                .kind,
            "ValueError"
        );
    }
    assert_eq!(
        call(
            &mut c,
            "set_module_voltage",
            vec![json!(0), json!(1), json!(0.0), json!(-1)]
        )
        .unwrap_err()
        .kind,
        "ValueError"
    );
    assert_eq!(
        call(&mut c, "get_state", vec![json!(0.5)]).unwrap()["$tuple"][2],
        json!("ST_ON")
    );
    completed(&script);
}

#[test]
fn initialize_rescans_mismatch_enables_and_waits_for_st_on() {
    let mut steps = connect_steps();
    steps.extend([
        step("GetScannedModuleState", 0, vec![json!(true), json!(false)]),
        step("RescanModules", 0, vec![]),
        step("SetScannedModuleState", 0, vec![]),
        step("EnablePSU", 0, vec![json!(true)]),
        step("GetState", 0, vec![json!(2)]),
        step("GetState", 0, vec![json!(0)]),
        step("GetState", 0, vec![json!(0)]),
    ]);
    let (mut c, script) = controller(steps);
    assert!(
        c.call(
            "initialize",
            &[],
            &json!({"timeout_s": 1.0, "poll_s": 0.001}),
            &Context::test(5.0)
        )
        .unwrap()
        .is_null()
    );
    assert!(call(&mut c, "initialize", vec![]).unwrap().is_null());
    completed(&script);
}

#[test]
fn cancellation_after_enable_still_attempts_bounded_psu_disable() {
    let (sender, receiver) = std::sync::mpsc::sync_channel(32);
    let ctx = Context::new(
        std::time::Duration::from_secs(5),
        Arc::new(std::sync::atomic::AtomicBool::new(false)),
        Some(sender),
    )
    .with_request_id(77)
    .with_native_timeout(std::time::Duration::from_secs(1));
    let mut steps = connect_steps();
    steps.extend([
        step("GetScannedModuleState", 0, vec![json!(false); 2]),
        Step {
            cancel: Some(ctx.clone()),
            ..step("EnablePSU", 0, vec![json!(true)])
        },
        exact("EnablePSU", json!([false]), 0, vec![json!(false)]),
    ]);
    let (mut c, script) = controller(steps);
    assert!(
        c.call("initialize", &[], &json!({}), &ctx)
            .unwrap_err()
            .message
            .contains("cancelled")
    );
    assert_eq!(c.get_attribute("connected").unwrap(), json!(true));
    completed(&script);
    let telemetry: Vec<_> = receiver.try_iter().collect();
    assert_eq!(telemetry.len(), 12);
    assert!(telemetry.iter().all(|event| event["id"] == json!(77)));
    assert_eq!(telemetry[10]["symbol"], json!("COM_AMPR_12_EnablePSU"));
    assert_eq!(telemetry[10]["phase"], json!("enter"));
    assert_eq!(telemetry[10]["timeout_s"], json!(1.0));
    assert_eq!(telemetry[11]["sequence"], json!(5));
    assert!(
        ctx.is_cancelled(),
        "Cleanup must not rearm the retired request"
    );
}

#[test]
fn state_and_interlock_labels_preserve_python_shapes_and_failures() {
    let (mut c, script) = controller(vec![
        step("GetState", 0, vec![json!(0x8005)]),
        step("GetInterlockState", 0, vec![json!(0x8101)]),
        step("GetState", -10, vec![json!(0)]),
        step("GetState", 0, vec![json!(3)]),
    ]);
    assert_eq!(
        call(&mut c, "get_state", vec![]).unwrap(),
        json!({"$tuple": [0, "0x8005", "ST_ERR_ILOCK"]})
    );
    assert_eq!(
        call(&mut c, "get_interlock_state", vec![]).unwrap(),
        json!({"$tuple": [0, "0x8101", ["SI_ILOCK_FRONT_ENB", "SI_ILOCK_FRONT", "SI_ILOCK_ENB"]]})
    );
    assert_eq!(
        call(&mut c, "get_state", vec![]).unwrap(),
        json!({"$tuple": [-10, null, null]})
    );
    assert_eq!(
        tuple(&call(&mut c, "get_state", vec![]).unwrap())[2],
        json!("UNKNOWN_STATE_0x0003")
    );
    completed(&script);
}

#[test]
fn scan_excludes_base_and_invalid_modules_and_preserves_numeric_keys() {
    let mut presence = vec![0; 13];
    presence[1] = 1;
    presence[2] = 2;
    presence[12] = 1;
    let mut steps = vec![step(
        "GetModulePresence",
        0,
        vec![json!(true), json!(12), json!(presence)],
    )];
    steps.extend(metadata(1, "Amplifier", 132401, 222308));
    let (mut c, script) = controller(steps);
    let modules = call(&mut c, "scan_modules", vec![]).unwrap();
    assert_eq!(modules["$map"].as_array().unwrap().len(), 1);
    assert_eq!(map(&modules, 1)["product_no"], json!(132401));
    completed(&script);
}

#[test]
fn numeric_ids_take_precedence_and_product_id_limits_are_enforced() {
    for (product, hw, rating, channels) in [(132401, 222308, 1000, 4), (99, 88, 500, 2)] {
        let mut steps = vec![
            exact(
                "GetModuleProductID",
                json!([0, {"capacity":81,"text":""}]),
                0,
                vec![json!("Dual 500 V amplifier")],
            ),
            step("GetModuleProductNo", 0, vec![json!(product)]),
            step("GetModuleHwType", 0, vec![json!(hw)]),
        ];
        if rating == 1000 {
            steps.push(exact(
                "SetModuleOutputVoltage",
                json!([0, 3, 750.0]),
                0,
                vec![],
            ));
        }
        let (mut c, script) = controller(steps);
        let cap = call(&mut c, "get_module_capabilities", vec![json!(0)]).unwrap();
        assert_eq!(cap["voltage_rating"], json!(rating));
        assert_eq!(cap["channel_count"], json!(channels));
        assert_eq!(
            call(
                &mut c,
                "set_module_voltage",
                vec![json!(0), json!(4), json!(750.0)]
            )
            .unwrap(),
            json!(if rating == 1000 { 0 } else { -15 })
        );
        completed(&script);
    }
}

#[test]
fn finite_absolute_range_and_address_validation_never_calls_dll() {
    let (mut c, script) = controller(vec![]);
    for args in [
        vec![json!(12), json!(1), json!(0.0)],
        vec![json!(0), json!(0), json!(0.0)],
        vec![json!(0), json!(5), json!(0.0)],
        vec![json!(0), json!(1), float(f64::NAN)],
        vec![json!(0), json!(1), float(f64::INFINITY)],
        vec![json!(0), json!(1), json!(-1000.001)],
    ] {
        assert_eq!(
            call(&mut c, "set_module_voltage", args).unwrap(),
            json!(-15)
        );
    }
    assert_eq!(
        call(
            &mut c,
            "get_module_voltage_setpoint",
            vec![json!(12), json!(1)]
        )
        .unwrap(),
        json!({"$tuple": [-15, null]})
    );
    assert!(script.lock().unwrap().calls.is_empty());
}

#[test]
fn all_fixed_buffers_and_housekeeping_orders_match_vendor_header() {
    let (mut c, script) = controller(vec![
        exact(
            "GetHousekeeping",
            json!(vec![0.0; 14]),
            0,
            (0..14).map(|n| json!(n as f64)).collect(),
        ),
        exact(
            "GetModuleHousekeeping",
            json!([3, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]),
            0,
            (0..7).map(|n| json!(n as f64)).collect(),
        ),
        exact(
            "GetMeasuredModuleOutputVoltages",
            json!([3, [0.0, 0.0, 0.0, 0.0]]),
            -10,
            vec![json!([0.0, 0.0, 0.0, 0.0])],
        ),
    ]);
    assert_eq!(
        tuple(&call(&mut c, "get_housekeeping", vec![]).unwrap()).len(),
        15
    );
    let module = call(&mut c, "get_module_housekeeping", vec![json!(3)]).unwrap();
    assert_eq!(tuple(&module)[2], json!(1.0));
    assert_eq!(
        call(&mut c, "get_all_module_voltage_measured", vec![json!(3)]).unwrap(),
        json!({"$tuple": [-10, null]})
    );
    completed(&script);
}

#[test]
fn voltage_snapshot_does_not_substitute_setpoints_for_missing_measurements() {
    let mut steps = vec![step(
        "GetMeasuredModuleOutputVoltages",
        -10,
        vec![json!([0.0, 0.0, 0.0, 0.0])],
    )];
    for ch in 0..4 {
        steps.push(exact(
            "GetModuleOutputVoltage",
            json!([2, ch, 0.0]),
            0,
            vec![json!(100.0 + ch as f64)],
        ));
    }
    let (mut c, script) = controller(steps);
    let values = call(&mut c, "get_module_voltages", vec![json!(2)]).unwrap();
    assert_eq!(map(&values, 1)["setpoint"], json!(100.0));
    assert!(map(&values, 1)["measured"].is_null());
    completed(&script);
}

#[test]
fn bulk_write_is_sorted_sequential_and_preserves_partial_statuses_without_retry() {
    let (mut c, script) = controller(vec![
        exact("SetModuleOutputVoltage", json!([0, 0, 10.0]), 0, vec![]),
        exact("SetModuleOutputVoltage", json!([0, 1, 20.0]), -11, vec![]),
        exact("SetModuleOutputVoltage", json!([0, 3, 30.0]), 0, vec![]),
    ]);
    let result = call(
        &mut c,
        "set_module_voltages",
        vec![json!(0), json!({"$map": [[4,30.0],[2,20.0],[1,10.0]]})],
    )
    .unwrap();
    assert_eq!(map(&result, 2), &json!(-11));
    completed(&script);
}

#[test]
fn shutdown_zeros_only_detected_dual_channels_then_confirms_disable_before_close() {
    let mut steps = connect_steps();
    steps.extend(scan_steps(3, "Dual 500V"));
    steps.extend([
        exact("SetModuleOutputVoltage", json!([3, 0, 0.0]), 0, vec![]),
        exact("SetModuleOutputVoltage", json!([3, 1, 0.0]), 0, vec![]),
        exact("EnablePSU", json!([false]), 0, vec![json!(false)]),
        step("Close", 0, vec![]),
    ]);
    let (mut c, script) = controller(steps);
    call(&mut c, "connect", vec![]).unwrap();
    assert_eq!(call(&mut c, "shutdown", vec![]).unwrap(), json!(true));
    assert_eq!(c.get_attribute("connected").unwrap(), json!(false));
    completed(&script);
}

#[test]
fn zero_failure_still_disables_but_does_not_close_and_explicit_retry_can_succeed() {
    let mut steps = connect_steps();
    steps.extend(scan_steps(0, "Dual 500V"));
    steps.extend([
        step("SetModuleOutputVoltage", -7, vec![]),
        step("SetModuleOutputVoltage", 0, vec![]),
        step("EnablePSU", 0, vec![json!(false)]),
    ]);
    steps.extend(scan_steps(0, "Dual 500V"));
    steps.extend([
        step("SetModuleOutputVoltage", 0, vec![]),
        step("SetModuleOutputVoltage", 0, vec![]),
        step("EnablePSU", 0, vec![json!(false)]),
        step("Close", 0, vec![]),
    ]);
    let (mut c, script) = controller(steps);
    call(&mut c, "connect", vec![]).unwrap();
    assert!(
        call(&mut c, "shutdown", vec![])
            .unwrap_err()
            .message
            .contains("zero module")
    );
    assert_eq!(c.get_attribute("connected").unwrap(), json!(true));
    assert_eq!(call(&mut c, "shutdown", vec![]).unwrap(), json!(true));
    completed(&script);
}

#[test]
fn scan_or_disable_failure_cannot_be_reported_as_successful_shutdown() {
    let mut steps = connect_steps();
    steps.extend([
        step(
            "GetModulePresence",
            -10,
            vec![json!(false), json!(0), json!(vec![0; 13])],
        ),
        step("EnablePSU", 0, vec![json!(true)]),
    ]);
    let (mut c, script) = controller(steps);
    call(&mut c, "connect", vec![]).unwrap();
    let error = call(&mut c, "shutdown", vec![]).unwrap_err();
    assert!(error.message.contains("scan_modules"));
    assert!(error.message.contains("disable unconfirmed"));
    assert_eq!(c.get_attribute("_dll_port_claimed").unwrap(), json!(true));
    completed(&script);
}

#[test]
fn cancelled_request_and_unknown_method_do_not_touch_dll() {
    let (mut c, script) = controller(vec![]);
    let ctx = Context::test(5.0);
    ctx.cancel();
    assert!(c.call("connect", &[], &json!({}), &ctx).is_err());
    assert_eq!(
        call(&mut c, "_hk_batch", vec![]).unwrap_err().kind,
        "AttributeError"
    );
    assert_eq!(
        c.call(
            "get_state",
            &[],
            &json!({"timeout_s": float(f64::NAN)}),
            &Context::test(5.0)
        )
        .unwrap_err()
        .kind,
        "ValueError"
    );
    assert!(script.lock().unwrap().calls.is_empty());
}

#[test]
fn facade_inventory_covers_existing_gui_contract_without_exposing_internals() {
    let (mut c, _) = controller(vec![]);
    for attr in GUI_ATTRIBUTES {
        assert!(c.get_attribute(attr).is_ok(), "{attr}");
    }
    assert!(GUI_METHODS.contains(&"get_module_capabilities"));
    assert!(GUI_METHODS.contains(&"shutdown"));
    for method in GUI_METHODS {
        let error = c
            .call(
                method,
                &[],
                &json!({"not_a_keyword": true}),
                &Context::test(5.0),
            )
            .unwrap_err();
        assert_eq!(error.kind, "TypeError", "Unimplemented GUI method {method}");
    }
}

#[test]
fn housekeeping_is_driven_by_tick_and_stop_prevents_more_calls() {
    let mut steps = connect_steps();
    steps.extend([
        step("GetProductNo", 0, vec![json!(123)]),
        step("GetState", 0, vec![json!(0)]),
        step("GetDeviceState", 0, vec![json!(0)]),
        step("GetHousekeeping", 0, vec![json!(0.0); 14]),
        step("GetVoltageState", 0, vec![json!(0)]),
        step("GetTemperatureState", 0, vec![json!(0)]),
        step("GetInterlockState", 0, vec![json!(0)]),
        step(
            "GetFanData",
            0,
            vec![json!(false), json!(0), json!(0), json!(0), json!(0)],
        ),
        step("GetLEDData", 0, vec![json!(false); 3]),
        step("GetCPUdata", 0, vec![json!(0.0); 2]),
        step(
            "GetModulePresence",
            0,
            vec![json!(true), json!(0), json!(vec![0; 13])],
        ),
    ]);
    let (mut c, script) = controller(steps);
    call(&mut c, "connect", vec![]).unwrap();
    call(&mut c, "start_housekeeping", vec![json!(60.0)]).unwrap();
    c.tick(&Context::test(5.0)).unwrap();
    assert!(
        c.get_attribute("housekeeping_snapshot")
            .unwrap()
            .is_object()
    );
    call(&mut c, "stop_housekeeping", vec![]).unwrap();
    c.tick(&Context::test(5.0)).unwrap();
    completed(&script);
}

#[derive(Default)]
struct RampState {
    values: [f64; 4],
    writes: Vec<(u64, f64)>,
    state: u64,
    fail_interlock_after: Option<usize>,
}
struct RampDll(Arc<Mutex<RampState>>);
impl Dll for RampDll {
    fn call(&mut self, symbol: &str, args: &[Value]) -> Result<NativeReply> {
        let mut state = self.0.lock().unwrap();
        let values = match symbol.strip_prefix("COM_AMPR_12_").unwrap() {
            "Open" | "Close" => vec![],
            "SetBaudRate" => vec![args[0].clone()],
            "GetDevType" => vec![json!(0xA3D8)],
            "GetModuleProductID" => vec![json!("Quad 1000V")],
            "GetModuleProductNo" => vec![json!(132401)],
            "GetModuleHwType" => vec![json!(222308)],
            "GetState" => vec![json!(state.state)],
            "GetModuleOutputVoltage" => {
                vec![json!(state.values[args[1].as_u64().unwrap() as usize])]
            }
            "GetMeasuredModuleOutputVoltages" => vec![json!(state.values)],
            "SetModuleOutputVoltage" => {
                let channel = args[1].as_u64().unwrap();
                let voltage = args[2].as_f64().unwrap();
                state.values[channel as usize] = voltage;
                state.writes.push((channel, voltage));
                if state
                    .fail_interlock_after
                    .is_some_and(|n| state.writes.len() >= n)
                {
                    state.state = 0x8005;
                }
                vec![]
            }
            _ => panic!("Unexpected ramp DLL call {symbol}"),
        };
        Ok(NativeReply { status: 0, values })
    }
}
fn ramp_controller(state: Arc<Mutex<RampState>>) -> Controller {
    let mut c = Controller::new(
        &json!({"device_id":"ramp", "com":5}),
        Box::new(RampDll(state)),
    )
    .unwrap();
    call(&mut c, "connect", vec![]).unwrap();
    c
}

#[test]
fn ramp_advances_channels_together_and_confirms_hardware_setpoints() {
    let state = Arc::new(Mutex::new(RampState::default()));
    let mut c = ramp_controller(state.clone());
    let result = call(
        &mut c,
        "ramp_module_voltages",
        vec![
            json!(0),
            json!({"$map":[[1,1.0],[2,-2.0]]}),
            json!({"$map":[[1,10.0],[2,20.0]]}),
        ],
    )
    .unwrap();
    assert_eq!(map(&result, 1)["setpoint"], json!(1.0));
    assert_eq!(map(&result, 2)["setpoint"], json!(-2.0));
    let s = state.lock().unwrap();
    assert!(s.writes.len() >= 4);
    for pair in s.writes.as_chunks::<2>().0 {
        assert_eq!(pair[0].0, 0);
        assert_eq!(pair[1].0, 1);
        assert!((pair[1].1 + pair[0].1 * 2.0).abs() < 1e-8);
    }
}

#[test]
fn zero_ramp_rate_is_immediate_but_state_interlock_failure_stops_further_steps() {
    let state = Arc::new(Mutex::new(RampState::default()));
    let mut c = ramp_controller(state.clone());
    call(
        &mut c,
        "ramp_module_voltages",
        vec![json!(0), json!({"1":50.0}), json!(0.0)],
    )
    .unwrap();
    assert_eq!(state.lock().unwrap().writes.len(), 1);
    let state = Arc::new(Mutex::new(RampState {
        fail_interlock_after: Some(1),
        ..Default::default()
    }));
    let mut c = ramp_controller(state.clone());
    let error = call(
        &mut c,
        "ramp_module_voltages",
        vec![json!(0), json!({"1":10.0}), json!(1.0)],
    )
    .unwrap_err();
    assert!(error.message.contains("left ST_ON"));
    assert_eq!(state.lock().unwrap().writes.len(), 1);
}

#[test]
fn malformed_reply_is_not_accepted_and_expired_hardware_return_poisoned() {
    let (mut c, _) = controller(vec![step(
        "GetModulePresence",
        0,
        vec![json!(false), json!(0), json!(vec![0; 12])],
    )]);
    assert!(
        call(&mut c, "get_module_presence", vec![])
            .unwrap_err()
            .message
            .contains("13")
    );
    struct Slow;
    impl Dll for Slow {
        fn call(&mut self, _: &str, _: &[Value]) -> Result<NativeReply> {
            std::thread::sleep(std::time::Duration::from_millis(15));
            Ok(NativeReply {
                status: 0,
                values: vec![json!(0)],
            })
        }
    }
    let mut c = Controller::new(&json!({"device_id":"slow", "com":5}), Box::new(Slow)).unwrap();
    let error = c
        .call("get_state", &[], &json!({}), &Context::test(0.001))
        .unwrap_err();
    assert_eq!(error.kind, "TimeoutError");
    assert_eq!(c.get_attribute("_transport_poisoned").unwrap(), json!(true));
    assert_eq!(call(&mut c, "disconnect", vec![]).unwrap(), json!(false));
    let mut c = Controller::new(&json!({"device_id":"slow", "com":5}), Box::new(Slow)).unwrap();
    let ctx = Context::test(5.0).with_native_timeout(std::time::Duration::from_millis(1));
    assert_eq!(
        c.call("get_state", &[], &json!({}), &ctx).unwrap_err().kind,
        "TimeoutError"
    );
    assert!(
        !ctx.remaining().is_zero(),
        "Per-DLL watchdog must not wait for the aggregate deadline"
    );
    assert_eq!(c.get_attribute("_dll_port_claimed").unwrap(), json!(true));
    assert_eq!(
        call(&mut c, "get_state", vec![]).unwrap_err().kind,
        "RuntimeError"
    );
}
