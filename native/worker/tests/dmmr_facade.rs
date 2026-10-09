#![cfg(feature = "dmmr")]

use esibd_native_worker::{
    Backend,
    codec::float,
    context::Context,
    dmmr::{Controller, GUI_ATTRIBUTES, GUI_METHODS, PYTHON_OBJECT_API_GAPS},
    error::Result,
    ffi::{Dll, NativeReply},
};
use serde_json::{Value, json};
use std::collections::VecDeque;
use std::sync::{
    Arc, Mutex,
    atomic::{AtomicU64, Ordering},
};

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
        let export = symbol.strip_prefix("COM_DMMR_8_").unwrap();
        if matches!(export, "GetIOState" | "GetCommError")
            && script
                .steps
                .front()
                .is_none_or(|step| step.export != export)
        {
            return Ok(NativeReply {
                status: 0,
                values: vec![json!(if export == "GetIOState" { 17 } else { 2 })],
            });
        }
        let expected = script
            .steps
            .pop_front()
            .unwrap_or_else(|| panic!("Unexpected DLL call {symbol} {args:?}"));
        assert_eq!(export, expected.export);
        if let Some(expected_args) = expected.args {
            assert_eq!(json!(args), expected_args, "{export} ABI arguments");
        }
        if let Some(ctx) = expected.cancel {
            ctx.cancel();
        }
        Ok(NativeReply {
            status: expected.status,
            values: expected.values,
        })
    }
}
fn controller(steps: Vec<Step>) -> (Controller, Arc<Mutex<Script>>) {
    config_controller(json!({"device_id":"test", "com":5}), steps)
}
fn config_controller(config: Value, steps: Vec<Step>) -> (Controller, Arc<Mutex<Script>>) {
    let script = Arc::new(Mutex::new(Script {
        steps: steps.into(),
        calls: vec![],
    }));
    (
        Controller::new(&config, Box::new(Mock(script.clone()))).unwrap(),
        script,
    )
}
fn call(c: &mut Controller, method: &str, args: Vec<Value>) -> Result<Value> {
    c.call(method, &args, &json!({}), &Context::test(5.0))
}
fn completed(script: &Arc<Mutex<Script>>) {
    assert!(
        script.lock().unwrap().steps.is_empty(),
        "Unconsumed DLL expectations"
    );
}
fn map(value: &Value, key: u64) -> &Value {
    &value["$map"]
        .as_array()
        .unwrap()
        .iter()
        .find(|entry| entry[0] == json!(key))
        .unwrap()[1]
}
fn connect_steps() -> Vec<Step> {
    vec![
        exact("Open", json!([5]), 0, vec![]),
        exact("SetBaudRate", json!([230400]), 0, vec![json!(230400)]),
        step("GetDevType", 0, vec![json!(0xAC38)]),
        exact(
            "GetProductID",
            json!([{"capacity":81,"text":""}]),
            0,
            vec![json!("DMMR-8")],
        ),
    ]
}
fn verify(automatic: bool, ranges: &[(u64, u64, bool)]) -> Vec<Step> {
    let mut steps = vec![
        step("GetState", 0, vec![json!(0)]),
        step("GetEnable", 0, vec![json!(true)]),
        step("GetAutomaticCurent", 0, vec![json!(automatic)]),
    ];
    for (address, range, auto) in ranges {
        steps.push(exact(
            "GetModuleMeasRange",
            json!([address, 0, false]),
            0,
            vec![json!(range), json!(auto)],
        ));
    }
    steps
}
fn configure(c: &mut Controller, ranges: Value, automatic: bool) {
    c.call(
        "configure_read_recovery",
        &[ranges],
        &json!({"automatic":automatic}),
        &Context::test(5.0),
    )
    .unwrap();
}
fn off_steps() -> Vec<Step> {
    vec![
        exact("SetAutomaticCurent", json!([false]), 0, vec![]),
        exact("SetEnable", json!([false]), 0, vec![]),
        step("GetAutomaticCurent", 0, vec![json!(false)]),
        step("GetEnable", 0, vec![json!(false)]),
    ]
}

#[test]
fn constructor_validation_is_finite_strict_and_dll_free() {
    for (field, value) in [
        ("device_id", json!(true)),
        ("device_id", json!("  ")),
        ("com", json!(true)),
        ("com", json!(256)),
        ("baudrate", json!(0)),
        ("hk_interval_s", float(f64::NAN)),
        ("hk_interval_s", json!(1e308)),
    ] {
        let script = Arc::new(Mutex::new(Script::default()));
        let mut config = json!({"device_id":"test","com":5});
        config[field] = value;
        assert!(
            Controller::new(&config, Box::new(Mock(script.clone()))).is_err(),
            "{field}"
        );
        assert!(script.lock().unwrap().calls.is_empty());
    }
}

#[test]
fn connect_confirms_identity_and_does_not_reopen_an_existing_connection() {
    let (mut c, script) = controller(connect_steps());
    assert_eq!(call(&mut c, "connect", vec![]).unwrap(), json!(true));
    assert_eq!(call(&mut c, "connect", vec![]).unwrap(), json!(true));
    assert_eq!(c.get_attribute("connected").unwrap(), json!(true));
    assert!(c.set_attribute("com", json!(6)).is_err());
    assert!(c.set_attribute("connected", json!(false)).is_err());
    completed(&script);
}

#[test]
fn cancelled_initial_open_cleanup_remains_serialized_and_supervised_under_original_request_id() {
    let (sender, receiver) = std::sync::mpsc::sync_channel(16);
    let ctx = Context::new(
        std::time::Duration::from_secs(5),
        Arc::new(std::sync::atomic::AtomicBool::new(false)),
        Some(sender),
    )
    .with_request_id(88)
    .with_native_timeout(std::time::Duration::from_secs(1));
    let (mut c, script) = controller(vec![
        Step {
            cancel: Some(ctx.clone()),
            ..step("Open", 0, vec![])
        },
        step("Close", 0, vec![]),
    ]);
    assert!(
        c.call("connect", &[], &json!({}), &ctx)
            .unwrap_err()
            .message
            .contains("cancelled")
    );
    assert_eq!(c.get_attribute("connected").unwrap(), json!(false));
    assert_eq!(
        c.get_attribute("_failed_open_released").unwrap(),
        json!(true)
    );
    let telemetry: Vec<_> = receiver.try_iter().collect();
    assert_eq!(telemetry.len(), 4);
    assert!(telemetry.iter().all(|event| event["id"] == json!(88)));
    assert_eq!(telemetry[2]["symbol"], json!("COM_DMMR_8_Close"));
    assert_eq!(telemetry[2]["phase"], json!("enter"));
    assert_eq!(telemetry[2]["timeout_s"], json!(1.0));
    assert_eq!(telemetry[3]["sequence"], json!(1));
    assert!(ctx.is_cancelled());
    completed(&script);
}

#[test]
fn wrong_instrument_and_failed_open_never_enable_acquisition() {
    let mut steps = connect_steps();
    steps.truncate(3);
    steps[2].values = vec![json!(0xA3D8)];
    steps.push(step("Close", 0, vec![]));
    let (mut c, script) = controller(steps);
    assert!(
        call(&mut c, "connect", vec![])
            .unwrap_err()
            .message
            .contains("device type mismatch")
    );
    completed(&script);
    assert_eq!(
        c.get_attribute("_failed_open_released").unwrap(),
        json!(true)
    );
    assert_eq!(
        c.get_attribute("_failed_open_cleanup_outcome").unwrap(),
        json!({"$tuple":["result",0]})
    );
    let (mut c, script) = controller(vec![
        step("Open", -2, vec![]),
        step("Close", -3, vec![]),
        step("Close", 0, vec![]),
    ]);
    assert!(call(&mut c, "connect", vec![]).is_err());
    assert_eq!(c.get_attribute("_open_failed").unwrap(), json!(true));
    assert_eq!(
        c.get_attribute("_failed_open_cleanup_outcome").unwrap(),
        json!({"$tuple":["result",-3]})
    );
    assert!(call(&mut c, "connect", vec![]).is_err());
    assert_eq!(call(&mut c, "shutdown", vec![]).unwrap(), json!(false));
    assert_eq!(call(&mut c, "disconnect", vec![]).unwrap(), json!(true));
    completed(&script);
    assert_eq!(
        c.get_attribute("_failed_open_released").unwrap(),
        json!(true)
    );
    assert_eq!(
        c.get_attribute("_failed_open_cleanup_outcome").unwrap(),
        json!({"$tuple":["result",0]})
    );
}

#[test]
fn facade_positional_timeouts_are_finite_and_never_become_dll_arguments() {
    let (mut c, script) = controller(vec![
        exact("GetState", json!([0]), 0, vec![json!(0)]),
        exact(
            "GetModuleMeasRange",
            json!([3, 0, false]),
            0,
            vec![json!(2), json!(false)],
        ),
    ]);
    for method in [
        "get_state",
        "scan_modules",
        "get_product_info",
        "collect_housekeeping",
    ] {
        assert_eq!(
            call(&mut c, method, vec![float(f64::INFINITY)])
                .unwrap_err()
                .kind,
            "ValueError"
        );
    }
    assert!(
        call(
            &mut c,
            "set_module_auto_range",
            vec![json!(0), json!(true), json!(0)]
        )
        .is_err()
    );
    assert_eq!(
        call(&mut c, "get_state", vec![json!(0.1)]).unwrap()["$tuple"][2],
        json!("ST_ON")
    );
    assert_eq!(
        call(&mut c, "get_module_meas_range", vec![json!(3), json!(0.2)]).unwrap(),
        json!({"$tuple":[0,2,false]})
    );
    completed(&script);
}

#[test]
fn initialize_refreshes_modules_and_only_persists_scan_when_requested() {
    for persist in [true, false] {
        let mut steps = connect_steps();
        steps.push(step("RescanModules", 0, vec![]));
        steps.push(exact(
            "GetModulePresence",
            json!([false, 0, vec![0; 8]]),
            0,
            vec![json!(true), json!(7), json!([1, 0, 2, 0, 0, 0, 0, 0])],
        ));
        for export in [
            "GetModuleFwVersion",
            "GetModuleProductNo",
            "GetModuleHwType",
            "GetModuleHwVersion",
            "GetModuleState",
        ] {
            steps.push(step(export, 0, vec![json!(1)]));
        }
        steps.push(step("GetScannedModuleState", 0, vec![json!(true)]));
        if persist {
            steps.push(step("SetScannedModuleState", 0, vec![]));
        }
        let (mut c, script) = controller(steps);
        let modules = c
            .call(
                "initialize",
                &[],
                &json!({"persist_scan":persist}),
                &Context::test(5.0),
            )
            .unwrap();
        assert_eq!(modules["$map"].as_array().unwrap().len(), 1);
        assert_eq!(map(&modules, 0)["fw_version"], json!(1));
        completed(&script);
    }
}

#[test]
fn dmmr_unknown_states_are_not_borrowed_from_hv_instruments() {
    let (mut c, script) = controller(vec![
        step("GetState", 0, vec![json!(1)]),
        step("GetState", -10, vec![json!(0)]),
        step("GetBaseState", 0, vec![json!(1024 | 2048)]),
    ]);
    assert_eq!(
        call(&mut c, "get_state", vec![]).unwrap(),
        json!({"$tuple":[0,"0x0001","UNKNOWN_STATE_0x0001"]})
    );
    assert_eq!(
        call(&mut c, "get_state", vec![]).unwrap(),
        json!({"$tuple":[-10,"0x0000","ST_ON"]})
    );
    assert_eq!(
        call(&mut c, "get_base_state", vec![]).unwrap(),
        json!({"$tuple":[0,"0x0C00",["BS_ILOCK_IN","BS_ILOCK_OUT"]]})
    );
    completed(&script);
}

#[test]
fn range_address_and_boolean_validation_rejects_invalid_requests_without_dll() {
    let (mut c, script) = controller(vec![]);
    for args in [
        vec![json!(8), json!(0)],
        vec![json!(0), json!(5)],
        vec![json!(0), json!(true)],
        vec![json!(0), json!(1.2)],
    ] {
        assert!(call(&mut c, "set_module_meas_range", args).is_err());
    }
    assert!(call(&mut c, "set_module_auto_range", vec![json!(0), json!(1)]).is_err());
    assert!(script.lock().unwrap().calls.is_empty());
}

#[test]
fn malformed_autorange_ack_is_verified_not_replayed() {
    let (mut c, script) = controller(vec![
        exact("SetModuleAutoRange", json!([3, false]), -11, vec![]),
        exact(
            "GetModuleMeasRange",
            json!([3, 0, false]),
            0,
            vec![json!(2), json!(false)],
        ),
    ]);
    assert_eq!(
        call(
            &mut c,
            "set_module_auto_range",
            vec![json!(3), json!(false)]
        )
        .unwrap(),
        json!(0)
    );
    assert_eq!(
        script
            .lock()
            .unwrap()
            .calls
            .iter()
            .filter(|(n, _)| n.ends_with("SetModuleAutoRange"))
            .count(),
        1
    );
    completed(&script);
}

#[test]
fn unchanged_ranges_are_not_rewritten_and_manual_mode_uses_fresh_readback() {
    let mut steps = connect_steps();
    steps.extend([
        exact(
            "GetModuleMeasRange",
            json!([0, 0, false]),
            0,
            vec![json!(3), json!(true)],
        ),
        exact(
            "GetModuleMeasRange",
            json!([3, 0, false]),
            0,
            vec![json!(4), json!(true)],
        ),
        exact("SetModuleAutoRange", json!([3, false]), 0, vec![]),
        exact(
            "GetModuleMeasRange",
            json!([3, 0, false]),
            0,
            vec![json!(2), json!(false)],
        ),
    ]);
    let (mut c, script) = controller(steps);
    call(&mut c, "connect", vec![]).unwrap();
    let ranges = call(
        &mut c,
        "configure_module_ranges",
        vec![json!({"$map":[[0,"Auto"],[3,"2"]]})],
    )
    .unwrap();
    assert_eq!(map(&ranges, 3), &json!({"$tuple":[2,false]}));
    assert!(
        script
            .lock()
            .unwrap()
            .calls
            .iter()
            .all(|(s, _)| !s.ends_with("SetModuleMeasRange"))
    );
    completed(&script);
}

#[test]
fn range_setup_does_not_reset_an_already_used_startup_recovery_budget() {
    let mut steps = connect_steps();
    steps.push(step("GetEnable", 0, vec![json!(true)]));
    steps.push(step(
        "GetModuleMeasRange",
        -12,
        vec![json!(0), json!(false)],
    ));
    let (mut c, script) = controller(steps);
    call(&mut c, "connect", vec![]).unwrap();
    call(&mut c, "begin_command_recovery", vec![json!("startup")]).unwrap();
    call(
        &mut c,
        "recover_command",
        vec![json!(-11), json!("enable"), json!({"enabled":true})],
    )
    .unwrap();
    let error = call(&mut c, "configure_module_ranges", vec![json!({"0":"Auto"})]).unwrap_err();
    assert!(error.message.contains("budget exhausted"));
    completed(&script);
}

#[test]
fn polling_pairs_current_and_range_and_never_reuses_invalid_or_missing_samples() {
    let mut steps = connect_steps();
    steps.extend([
        exact(
            "GetModuleCurent",
            json!([0, 0.0, 0]),
            0,
            vec![json!(0.534e-12), json!(0)],
        ),
        step("GetModuleCurent", 0, vec![json!(-2e-12), json!(4)]),
        step("GetModuleCurent", 0, vec![float(f64::INFINITY), json!(0)]),
        step("GetModuleCurent", 0, vec![json!(1e-12), json!(5)]),
    ]);
    let (mut c, script) = controller(steps);
    call(&mut c, "connect", vec![]).unwrap();
    let first = call(&mut c, "poll_currents", vec![json!([0, 3])]).unwrap();
    assert_eq!(map(&first["values"], 0), &json!(0.534e-12));
    assert_eq!(map(&first["ranges"], 3), &json!(4));
    let second = call(&mut c, "poll_currents", vec![json!([0, 3])]).unwrap();
    assert_eq!(map(&second["values"], 0), &float(f64::NAN));
    assert_eq!(map(&second["ranges"], 3), &float(f64::NAN));
    assert_eq!(map(&second["valid"], 0), &json!(false));
    assert!(second["token"].as_u64().unwrap() > first["token"].as_u64().unwrap());
    completed(&script);
}

#[test]
fn raw_current_getters_keep_failed_payloads_for_diagnostics_without_fabricating_validity() {
    let (mut c, script) = controller(vec![
        step("GetModuleCurent", -10, vec![float(f64::NAN), json!(5)]),
        step(
            "GetCurent",
            1,
            vec![json!(0), json!(0.0), json!(0), json!(0.0)],
        ),
    ]);
    assert_eq!(
        call(&mut c, "get_module_current", vec![json!(0)]).unwrap(),
        json!({"$tuple":[-10,{"$float":"nan"},5]})
    );
    assert_eq!(
        call(&mut c, "get_current", vec![]).unwrap()["$tuple"][0],
        json!(1)
    );
    completed(&script);
}

#[test]
fn automatic_frames_require_finite_current_valid_range_and_strictly_fresh_timestamp() {
    let mut steps = connect_steps();
    steps.extend([
        step(
            "GetCurent",
            0,
            vec![json!(0), json!(1e-12), json!(2), json!(1.0)],
        ),
        step(
            "GetCurent",
            0,
            vec![json!(0), json!(2e-12), json!(2), json!(1.0)],
        ),
        step(
            "GetCurent",
            0,
            vec![json!(0), float(f64::NAN), json!(2), json!(2.0)],
        ),
        step(
            "GetCurent",
            0,
            vec![json!(0), json!(3e-12), json!(5), json!(3.0)],
        ),
        step(
            "GetCurent",
            0,
            vec![json!(0), json!(4e-12), json!(1), json!(4.0)],
        ),
    ]);
    let (mut c, script) = controller(steps);
    call(&mut c, "connect", vec![]).unwrap();
    for expected in [true, false, false, false, true] {
        assert_eq!(
            call(&mut c, "get_valid_current", vec![]).unwrap()["valid"],
            json!(expected)
        );
    }
    completed(&script);
}

#[test]
fn read_recovery_first_verifies_without_purge_and_refuses_a_second_fault_before_fresh_samples() {
    let mut steps = connect_steps();
    steps.extend(verify(false, &[(0, 2, false), (3, 4, true)]));
    let (mut c, script) = controller(steps);
    call(&mut c, "connect", vec![]).unwrap();
    configure(&mut c, json!({"0":[2,false],"3":[4,true]}), false);
    let incident = call(&mut c, "recover_read", vec![json!(-12), json!("current")]).unwrap();
    assert_eq!(incident["verified"], json!(true));
    assert_eq!(incident["purged"], json!(false));
    assert_eq!(
        c.get_attribute("read_recovery").unwrap()["awaiting_samples"],
        json!([0, 3])
    );
    assert!(
        call(&mut c, "recover_read", vec![json!(-11), json!("current")])
            .unwrap_err()
            .message
            .contains("budget exhausted")
    );
    completed(&script);
}

#[test]
fn read_recovery_budget_is_three_incidents_per_minute_not_an_endless_retry() {
    let mut steps = connect_steps();
    for _ in 0..3 {
        steps.extend(verify(false, &[(0, 2, false)]));
    }
    let (mut c, script) = controller(steps);
    call(&mut c, "connect", vec![]).unwrap();
    configure(&mut c, json!({"0":[2,false]}), false);
    for _ in 0..3 {
        call(&mut c, "recover_read", vec![json!(-10), json!("current")]).unwrap();
        call(&mut c, "note_sample", vec![json!(0)]).unwrap();
    }
    assert!(call(&mut c, "recover_read", vec![json!(-10), json!("current")]).is_err());
    assert_eq!(
        c.get_attribute("read_recovery").unwrap()["recovery_count"],
        json!(3)
    );
    completed(&script);
}

#[test]
fn framing_failure_allows_one_purge_restores_baud_drains_fifo_and_restarts_only_previous_auto_mode()
{
    let mut steps = connect_steps();
    steps.push(step("GetState", -13, vec![json!(0)]));
    steps.extend([
        step("Purge", 0, vec![]),
        exact("SetBaudRate", json!([230400]), 0, vec![json!(230400)]),
        step("SetAutomaticCurent", 0, vec![]),
    ]);
    steps.extend(verify(false, &[(0, 2, false)]));
    steps.extend([
        step(
            "GetCurent",
            0,
            vec![json!(0), json!(5e-12), json!(2), json!(1.0)],
        ),
        step(
            "GetCurent",
            1,
            vec![json!(0), json!(0.0), json!(0), json!(0.0)],
        ),
        step("SetAutomaticCurent", 0, vec![]),
        step("GetAutomaticCurent", 0, vec![json!(true)]),
    ]);
    let (mut c, script) = controller(steps);
    call(&mut c, "connect", vec![]).unwrap();
    configure(&mut c, json!({"0":[2,false]}), true);
    let incident = call(
        &mut c,
        "recover_read",
        vec![json!(-10), json!("automatic current")],
    )
    .unwrap();
    assert_eq!(incident["purged"], json!(true));
    assert_eq!(incident["discarded_frames"], json!(1));
    assert!(
        script
            .lock()
            .unwrap()
            .calls
            .iter()
            .all(|(s, _)| !s.ends_with("SetEnable"))
    );
    completed(&script);
}

#[test]
fn bad_state_range_gate_and_baud_readback_never_trigger_reenable_or_reconnect() {
    for failure in ["state", "enabled", "range", "baud"] {
        let mut steps = connect_steps();
        match failure {
            "state" => steps.push(step("GetState", 0, vec![json!(0x8001)])),
            "enabled" => steps.extend([
                step("GetState", 0, vec![json!(0)]),
                step("GetEnable", 0, vec![json!(false)]),
            ]),
            "range" => steps.extend(verify(false, &[(0, 3, false)])),
            _ => steps.extend([
                step("GetState", -10, vec![json!(0)]),
                step("Purge", 0, vec![]),
                step("SetBaudRate", 0, vec![json!(9600)]),
            ]),
        }
        let (mut c, script) = controller(steps);
        call(&mut c, "connect", vec![]).unwrap();
        configure(&mut c, json!({"0":[2,false]}), false);
        assert!(
            call(&mut c, "recover_read", vec![json!(-11), json!("current")]).is_err(),
            "{failure}"
        );
        completed(&script);
    }
}

#[test]
fn recovered_poll_cycle_is_missing_and_invalid_followup_must_stop_acquisition() {
    let mut steps = connect_steps();
    steps.push(step("GetModuleCurent", -10, vec![json!(0.0), json!(0)]));
    steps.extend(verify(false, &[(0, 2, false)]));
    steps.push(step("GetModuleCurent", 0, vec![float(f64::NAN), json!(2)]));
    let (mut c, script) = controller(steps);
    call(&mut c, "connect", vec![]).unwrap();
    configure(&mut c, json!({"0":[2,false]}), false);
    let result = call(&mut c, "poll_currents", vec![json!([0])]).unwrap();
    assert_eq!(map(&result["valid"], 0), &json!(false));
    assert_eq!(result["recovery"]["verified"], json!(true));
    assert!(
        call(&mut c, "poll_currents", vec![json!([0])])
            .unwrap_err()
            .message
            .contains("No new valid reply")
    );
    assert_eq!(
        c.get_attribute("measurement_snapshot").unwrap()["token"],
        Value::Null
    );
    completed(&script);
}

#[test]
fn fresh_recovery_sample_requires_fixed_range_and_all_tracked_modules() {
    let mut steps = connect_steps();
    steps.extend(verify(false, &[(0, 2, false), (3, 4, true)]));
    steps.extend([
        step("GetModuleCurent", 0, vec![json!(1e-12), json!(2)]),
        step("GetModuleCurent", 0, vec![json!(-1e-12), json!(0)]),
    ]);
    let (mut c, script) = controller(steps);
    call(&mut c, "connect", vec![]).unwrap();
    configure(&mut c, json!({"0":[2,false],"3":[4,true]}), false);
    call(&mut c, "recover_read", vec![json!(-10), json!("current")]).unwrap();
    call(&mut c, "poll_currents", vec![json!([0, 3])]).unwrap();
    assert_eq!(
        c.get_attribute("read_recovery").unwrap()["last_incident"]["resumed"],
        json!(true)
    );
    completed(&script);
}

#[test]
fn command_recovery_verifies_enable_ack_without_replaying_write_or_purging_healthy_link() {
    let mut steps = connect_steps();
    steps.push(step("GetEnable", 0, vec![json!(true)]));
    let (mut c, script) = controller(steps);
    call(&mut c, "connect", vec![]).unwrap();
    call(&mut c, "begin_command_recovery", vec![json!("startup")]).unwrap();
    let incident = call(
        &mut c,
        "recover_command",
        vec![json!(-11), json!("set_enable"), json!({"enabled":true})],
    )
    .unwrap();
    assert_eq!(incident["verified"], json!(true));
    assert_eq!(incident["purged"], json!(false));
    assert!(
        call(
            &mut c,
            "recover_command",
            vec![json!(-11), json!("set_enable"), json!({"enabled":true})]
        )
        .is_err()
    );
    completed(&script);
}

#[test]
fn shutdown_confirms_both_gates_then_closes_and_unconfirmed_off_preserves_retry() {
    let mut steps = connect_steps();
    steps.extend(off_steps());
    steps.push(step("Close", 0, vec![]));
    let (mut c, script) = controller(steps);
    call(&mut c, "connect", vec![]).unwrap();
    assert_eq!(call(&mut c, "shutdown", vec![]).unwrap(), json!(true));
    completed(&script);
    let mut steps = connect_steps();
    let mut off = off_steps();
    off[3].values = vec![json!(true)];
    steps.extend(off);
    steps.extend(off_steps());
    steps.push(step("Close", 0, vec![]));
    let (mut c, script) = controller(steps);
    call(&mut c, "connect", vec![]).unwrap();
    assert!(call(&mut c, "shutdown", vec![]).is_err());
    assert_eq!(c.get_attribute("connected").unwrap(), json!(true));
    assert_eq!(call(&mut c, "shutdown", vec![]).unwrap(), json!(true));
    completed(&script);
}

#[test]
fn shutdown_lost_ack_with_confirmed_off_readbacks_needs_no_purge_or_write_retry() {
    let mut steps = connect_steps();
    let mut off = off_steps();
    off[0].status = -12;
    steps.extend(off);
    steps.extend([
        step("GetEnable", 0, vec![json!(false)]),
        step("GetAutomaticCurent", 0, vec![json!(false)]),
        step("Close", 0, vec![]),
    ]);
    let (mut c, script) = controller(steps);
    call(&mut c, "connect", vec![]).unwrap();
    assert_eq!(call(&mut c, "shutdown", vec![]).unwrap(), json!(true));
    let s = script.lock().unwrap();
    assert_eq!(
        s.calls
            .iter()
            .filter(|(n, _)| n.ends_with("SetAutomaticCurent"))
            .count(),
        1
    );
    assert!(!s.calls.iter().any(|(n, _)| n.ends_with("Purge")));
    drop(s);
    completed(&script);
}

#[test]
fn shutdown_framing_fault_can_repeat_only_off_after_one_resynchronization() {
    let mut steps = connect_steps();
    let mut off = off_steps();
    off[2].status = -13;
    steps.extend(off);
    steps.extend([
        step("Purge", 0, vec![]),
        step("SetBaudRate", 0, vec![json!(230400)]),
        exact("SetAutomaticCurent", json!([false]), 0, vec![]),
        exact("SetEnable", json!([false]), 0, vec![]),
        step("GetEnable", 0, vec![json!(false)]),
        step("GetAutomaticCurent", 0, vec![json!(false)]),
        step("Close", 0, vec![]),
    ]);
    let (mut c, script) = controller(steps);
    call(&mut c, "connect", vec![]).unwrap();
    assert_eq!(call(&mut c, "shutdown", vec![]).unwrap(), json!(true));
    completed(&script);
}

#[test]
fn non_receive_shutdown_error_is_not_recovered_and_close_failure_keeps_port_claimed() {
    let mut steps = connect_steps();
    let mut off = off_steps();
    off[0].status = -100;
    steps.extend(off);
    let (mut c, script) = controller(steps);
    call(&mut c, "connect", vec![]).unwrap();
    assert!(call(&mut c, "shutdown", vec![]).is_err());
    assert_eq!(c.get_attribute("_dll_port_claimed").unwrap(), json!(true));
    completed(&script);
    let mut steps = connect_steps();
    steps.extend(off_steps());
    steps.extend([step("Close", -3, vec![]), step("Close", 0, vec![])]);
    let (mut c, script) = controller(steps);
    call(&mut c, "connect", vec![]).unwrap();
    assert!(call(&mut c, "shutdown", vec![]).is_err());
    assert_eq!(call(&mut c, "disconnect", vec![]).unwrap(), json!(true));
    completed(&script);
}

#[test]
fn configs_validate_93_unsigned_registers_and_137_byte_names_before_native_call() {
    let (mut c, script) = controller(vec![
        exact(
            "SetCurrentConfig",
            json!([vec![42; 93]]),
            0,
            vec![json!(vec![42; 93])],
        ),
        exact(
            "GetConfigData",
            json!([499, vec![0; 93]]),
            -11,
            vec![json!(vec![0; 93])],
        ),
        exact(
            "SetConfigName",
            json!([0,{"capacity":137,"text":"test"}]),
            0,
            vec![json!("test")],
        ),
    ]);
    for registers in [
        json!(vec![0; 92]),
        {
            let mut v = vec![json!(0); 93];
            v[1] = json!(true);
            json!(v)
        },
        {
            let mut v = vec![json!(0); 93];
            v[1] = json!(4294967296u64);
            json!(v)
        },
    ] {
        assert!(call(&mut c, "set_current_config", vec![registers]).is_err());
    }
    assert_eq!(
        call(&mut c, "set_current_config", vec![json!(vec![42; 93])]).unwrap(),
        json!(0)
    );
    assert_eq!(
        call(&mut c, "get_config_data", vec![json!(499)]).unwrap()["$tuple"][1]
            .as_array()
            .unwrap()
            .len(),
        93
    );
    for name in [json!("x".repeat(137)), json!("a\0b")] {
        assert!(call(&mut c, "set_config_name", vec![json!(0), name]).is_err());
    }
    assert_eq!(
        call(&mut c, "set_config_name", vec![json!(0), json!("test")]).unwrap(),
        json!(0)
    );
    completed(&script);
}

#[test]
fn optional_config_list_falls_back_once_and_retains_full_500_flags() {
    let mut steps = vec![exact(
        "GetConfigList",
        json!([vec![false; 500], vec![false; 500]]),
        -10,
        vec![json!(vec![false; 500]); 2],
    )];
    for _ in 0..2 {
        for n in 0..500 {
            steps.push(exact(
                "GetConfigFlags",
                json!([n, false, false]),
                0,
                vec![json!(n == 3), json!(n == 4)],
            ));
        }
    }
    let (mut c, script) = controller(steps);
    for _ in 0..2 {
        let flags = call(&mut c, "get_config_list", vec![]).unwrap();
        assert_eq!(flags["$tuple"][1][3], json!(true));
        assert_eq!(flags["$tuple"][2][4], json!(true));
    }
    assert_eq!(
        script
            .lock()
            .unwrap()
            .calls
            .iter()
            .filter(|(n, _)| n.ends_with("GetConfigList"))
            .count(),
        1
    );
    completed(&script);
}

#[test]
fn error_diagnostics_capture_clear_on_read_state_before_next_transaction() {
    let (mut c, script) = controller(vec![step(
        "GetModuleCurent",
        -10,
        vec![json!(0.0), json!(0)],
    )]);
    call(&mut c, "get_module_current", vec![json!(0)]).unwrap();
    let diagnostics = call(&mut c, "protocol_diagnostics", vec![]).unwrap();
    assert_eq!(
        diagnostics["events"][0]["get_io_state"],
        json!({"$tuple":[0,17]})
    );
    assert_eq!(script.lock().unwrap().calls[1].0, "COM_DMMR_8_GetIOState");
    completed(&script);
}

#[test]
fn char_pointer_returns_use_string_reply_extension() {
    let (mut c, script) = controller(vec![
        exact(
            "GetIOStateMessage",
            json!([17]),
            0,
            vec![json!("port error")],
        ),
        step("GetErrorMessage", 0, vec![json!("driver error")]),
    ]);
    assert_eq!(
        call(&mut c, "get_io_state_message", vec![json!(17)]).unwrap(),
        json!("port error")
    );
    assert_eq!(
        call(&mut c, "get_error_message", vec![]).unwrap(),
        json!("driver error")
    );
    completed(&script);
}

#[test]
fn facade_inventory_rejects_unknown_keywords_and_object_rpc_gaps_are_explicit() {
    let (mut c, _) = controller(vec![]);
    for attr in GUI_ATTRIBUTES {
        assert!(c.get_attribute(attr).is_ok(), "{attr}");
    }
    for method in GUI_METHODS {
        assert_eq!(
            c.call(
                method,
                &[],
                &json!({"unexpected":true}),
                &Context::test(5.0)
            )
            .unwrap_err()
            .kind,
            "TypeError",
            "{method}"
        );
    }
    for gap in PYTHON_OBJECT_API_GAPS {
        assert_eq!(
            call(&mut c, gap, vec![]).unwrap_err().kind,
            "NotImplementedError"
        );
    }
    assert_eq!(
        call(&mut c, "_protocol_call_unlocked", vec![])
            .unwrap_err()
            .kind,
        "AttributeError"
    );
}

fn monitor_steps() -> Vec<Step> {
    vec![
        step("GetProductNo", 0, vec![json!(42)]),
        step("GetState", 0, vec![json!(0)]),
        step("GetDeviceState", 0, vec![json!(1)]),
        step(
            "GetHousekeeping",
            0,
            vec![json!(12.0), json!(5.0), json!(3.3), json!(27.0)],
        ),
        step("GetVoltageState", 0, vec![json!(7)]),
        step("GetTemperatureState", 0, vec![json!(0)]),
        step("GetBaseState", 0, vec![json!(128)]),
        step("GetBaseTemp", 0, vec![json!(25.0)]),
        step("GetBaseFanPWM", 0, vec![json!(123), json!(512)]),
        step("GetBaseFanRPM", 0, vec![json!(1000.0)]),
        step(
            "GetBaseLEDData",
            0,
            vec![json!(false), json!(true), json!(false)],
        ),
        step("GetCPUdata", 0, vec![json!(0.2), json!(80000000.0)]),
        step(
            "GetModulePresence",
            0,
            vec![json!(true), json!(7), json!(vec![0; 8])],
        ),
    ]
}

#[test]
fn housekeeping_runs_on_idle_tick_and_request_entry_but_stops_without_another_dll_thread() {
    let mut steps = connect_steps();
    steps.extend(monitor_steps());
    steps.extend(monitor_steps());
    let (mut c, script) = controller(steps);
    call(&mut c, "connect", vec![]).unwrap();
    call(&mut c, "start_housekeeping", vec![json!(60.0)]).unwrap();
    c.tick(&Context::test(5.0)).unwrap();
    assert_eq!(
        c.get_attribute("housekeeping_snapshot").unwrap()["get_product_no"],
        json!({"$tuple":[0,42]})
    );
    c.tick(&Context::test(5.0)).unwrap();
    call(&mut c, "stop_housekeeping", vec![]).unwrap();
    call(&mut c, "start_housekeeping", vec![json!(60.0)]).unwrap();
    call(&mut c, "get_status", vec![]).unwrap(); // Busy queues still perform the due housekeeping batch.
    call(&mut c, "stop_housekeeping", vec![]).unwrap();
    c.tick(&Context::test(5.0)).unwrap();
    assert_eq!(c.get_attribute("external_thread").unwrap(), json!(false));
    assert_eq!(
        call(&mut c, "do_housekeeping_cycle", vec![]).unwrap(),
        json!(false)
    );
    completed(&script);
}

fn module_snapshot_steps(address: u64, first: bool) -> Vec<Step> {
    let mut steps = vec![
        exact(
            "GetModuleProductID",
            json!([address,{"capacity":81,"text":""}]),
            0,
            vec![json!("DMMR picoammeter")],
        ),
        step("GetModuleProductNo", 0, vec![json!(42)]),
        step("GetModuleDevType", 0, vec![json!(0xC41E)]),
        step("GetModuleFwVersion", 0, vec![json!(17)]),
        exact(
            "GetModuleFwDate",
            json!([address,{"capacity":12,"text":""}]),
            0,
            vec![json!("2025-05-01")],
        ),
        step("GetModuleHwType", 0, vec![json!(2)]),
        step("GetModuleHwVersion", 0, vec![json!(3)]),
        step("GetModuleCPUdata", 0, vec![json!(0.1)]),
        step("GetModuleState", 0, vec![json!(0)]),
        step("GetModuleBufferState", 0, vec![json!(true)]),
        step("GetModuleManufDate", 0, vec![json!(2025), json!(12)]),
    ];
    if first {
        steps.push(step("GetModuleUptimeInt", -10, vec![json!(0); 4]));
    }
    steps.push(step("GetModuleOptimeInt", -12, vec![json!(0); 4]));
    steps.push(exact(
        "GetModuleHousekeeping",
        json!([
            address, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0
        ]),
        0,
        vec![json!(1.0); 16],
    ));
    steps.push(step("GetModuleReadyFlags", 0, vec![json!(5)]));
    if first {
        steps.push(step(
            "GetModuleMeasRange",
            -11,
            vec![json!(0), json!(false)],
        ));
    }
    steps.extend([
        step("GetModuleCurent", 0, vec![json!(-1e-12), json!(2)]),
        step(
            "GetScannedModuleParams",
            0,
            vec![json!(42), json!(42), json!(2), json!(2)],
        ),
    ]);
    steps
}

#[test]
fn module_snapshot_retains_scientific_units_nested_metadata_and_optional_support_cache() {
    let mut steps = connect_steps();
    steps.extend(module_snapshot_steps(3, true));
    steps.extend(module_snapshot_steps(3, false));
    let (mut c, script) = controller(steps);
    call(&mut c, "connect", vec![]).unwrap();
    for _ in 0..2 {
        let info = call(&mut c, "get_module_info", vec![json!(3)]).unwrap();
        assert_eq!(info["firmware"], json!({"version":17,"date":"2025-05-01"}));
        assert_eq!(
            info["manufacturing"],
            json!({"year":2025,"calendar_week":12})
        );
        assert_eq!(info["current"], json!({"value":-1e-12,"range":2}));
        assert_eq!(info["housekeeping"]["volt_3v3_v"], json!(1.0));
        assert_eq!(info["housekeeping"]["volt_vrefn_v"], json!(1.0));
        assert_eq!(
            info["ready_flags"],
            json!({"raw":5,"measurement_current_ready":true,"measurement_housekeeping_ready":false,"module_housekeeping_ready":true})
        );
        assert!(info.get("measurement_range").is_none());
        assert!(info.get("uptime").is_none());
    }
    {
        let locked = script.lock().unwrap();
        let calls = &locked.calls;
        assert_eq!(
            calls
                .iter()
                .filter(|(name, _)| name.ends_with("GetModuleUptimeInt"))
                .count(),
            1
        );
        assert_eq!(
            calls
                .iter()
                .filter(|(name, _)| name.ends_with("GetModuleMeasRange"))
                .count(),
            1
        );
        assert_eq!(
            calls
                .iter()
                .filter(|(name, _)| name.ends_with("GetModuleOptimeInt"))
                .count(),
            2
        );
    }
    completed(&script);
}

#[test]
fn controller_housekeeping_keeps_flags_counters_base_data_and_real_snapshot() {
    let mut steps = connect_steps();
    steps.extend([
        step("GetState", 0, vec![json!(0)]),
        step("GetDeviceState", 0, vec![json!(1)]),
        step("GetVoltageState", 0, vec![json!(7)]),
        step("GetTemperatureState", 0, vec![json!(16)]),
        step("GetEnable", 0, vec![json!(true)]),
        step("GetAutomaticCurent", 0, vec![json!(false)]),
        step(
            "GetHousekeeping",
            0,
            vec![json!(12.0), json!(5.0), json!(3.3), json!(27.0)],
        ),
        step("GetCPUdata", 0, vec![json!(0.2), json!(80000000.0)]),
        step(
            "GetUptimeInt",
            0,
            vec![json!(1), json!(2), json!(3), json!(4)],
        ),
        step(
            "GetOptimeInt",
            0,
            vec![json!(5), json!(6), json!(7), json!(8)],
        ),
        step("GetBaseState", 0, vec![json!(128)]),
        step("GetBaseTemp", 0, vec![json!(25.0)]),
        step("GetBaseFanPWM", 0, vec![json!(123), json!(512)]),
        step("GetBaseFanRPM", 0, vec![json!(1000.0)]),
        step(
            "GetBaseLEDData",
            0,
            vec![json!(false), json!(true), json!(false)],
        ),
        step(
            "GetModulePresence",
            0,
            vec![json!(true), json!(7), json!(vec![0; 8])],
        ),
        step("GetScannedModuleState", 0, vec![json!(false)]),
    ]);
    let (mut c, script) = controller(steps);
    call(&mut c, "connect", vec![]).unwrap();
    let hk = call(&mut c, "collect_housekeeping", vec![]).unwrap();
    assert_eq!(hk["main_state"], json!({"hex":"0x0000","name":"ST_ON"}));
    assert_eq!(hk["temperature_state"]["flags"], json!(["TS_TCPU_HIGH"]));
    assert_eq!(hk["housekeeping"]["temp_cpu_c"], json!(27.0));
    assert_eq!(hk["cpu"]["frequency_hz"], json!(80000000.0));
    assert_eq!(hk["uptime"]["total_operation_milliseconds"], json!(8));
    assert_eq!(
        hk["base"]["fan"]["state"],
        json!({"hex":"0x0200","flags":["FAN_OK"]})
    );
    assert_eq!(
        hk["base"]["led"],
        json!({"red":false,"green":true,"blue":false})
    );
    assert_eq!(hk["module_presence"]["present"], json!([]));
    assert_eq!(hk["modules"], json!({"$map":[]}));
    assert_eq!(c.get_attribute("housekeeping_snapshot").unwrap(), hk);
    completed(&script);
}

#[test]
fn product_inventory_preserves_controller_and_base_identities_and_fixed_string_buffers() {
    let mut steps = connect_steps();
    steps.extend([
        step("GetProductNo", 0, vec![json!(42)]),
        step("GetProductID", 0, vec![json!("DMMR-8")]),
        step("GetDevType", 0, vec![json!(0xAC38)]),
        step("GetFwVersion", 0, vec![json!(17)]),
        exact(
            "GetFwDate",
            json!([{"capacity":12,"text":""}]),
            0,
            vec![json!("2025-05-01")],
        ),
        step("GetHwType", 0, vec![json!(2)]),
        step("GetHwVersion", 0, vec![json!(3)]),
        step("GetManufDate", 0, vec![json!(2025), json!(12)]),
        step("GetBaseProductNo", 0, vec![json!(9)]),
        step("GetBaseHwType", 0, vec![json!(4)]),
        step("GetBaseHwVersion", 0, vec![json!(5)]),
        step("GetBaseManufDate", 0, vec![json!(2024), json!(41)]),
    ]);
    let (mut c, script) = controller(steps);
    call(&mut c, "connect", vec![]).unwrap();
    let info = call(&mut c, "get_product_info", vec![]).unwrap();
    assert_eq!(info["device_type"], json!(0xAC38));
    assert_eq!(info["firmware"], json!({"version":17,"date":"2025-05-01"}));
    assert_eq!(
        info["base"],
        json!({"product_no":9,"hardware":{"type":4,"version":5},"manufacturing":{"year":2024,"calendar_week":41}})
    );
    completed(&script);
}

#[test]
fn read_recovery_bounds_persistent_fifo_and_never_reenables_measurement() {
    let mut steps = connect_steps();
    steps.push(step("GetState", -12, vec![json!(0)]));
    steps.extend([
        step("Purge", 0, vec![]),
        step("SetBaudRate", 0, vec![json!(230400)]),
        step("SetAutomaticCurent", 0, vec![]),
    ]);
    steps.extend(verify(false, &[(0, 2, false)]));
    for _ in 0..1024 {
        steps.push(step(
            "GetCurent",
            0,
            vec![json!(0), json!(1e-12), json!(2), json!(1.0)],
        ));
    }
    let (mut c, script) = controller(steps);
    call(&mut c, "connect", vec![]).unwrap();
    configure(&mut c, json!({"0":[2,false]}), false);
    assert!(
        call(&mut c, "recover_read", vec![json!(-12), json!("read")])
            .unwrap_err()
            .message
            .contains("FIFO did not empty")
    );
    assert_eq!(
        c.get_attribute("read_recovery").unwrap()["failed"],
        json!(true)
    );
    assert!(
        script
            .lock()
            .unwrap()
            .calls
            .iter()
            .all(|(name, _)| !name.ends_with("SetEnable") && !name.ends_with("OpenDebugFile"))
    );
    completed(&script);
}

static TEMP_ID: AtomicU64 = AtomicU64::new(0);
fn temporary_dir() -> std::path::PathBuf {
    std::env::temp_dir().join(format!(
        "esibd-dmmr-test-{}-{}",
        std::process::id(),
        TEMP_ID.fetch_add(1, Ordering::Relaxed)
    ))
}

#[test]
fn startup_capture_requires_verified_hash_and_preserves_nonprintable_bytes_in_bounded_report() {
    let (mut c, script) = controller(vec![]);
    assert!(
        call(&mut c, "begin_startup_diagnostics", vec![])
            .unwrap()
            .as_str()
            .unwrap()
            .contains("unverified DLL")
    );
    completed(&script);
    let directory = temporary_dir();
    std::fs::create_dir_all(&directory).unwrap();
    let path = directory.join("dmmr_startup_com5.log");
    std::fs::write(&path, b"ACK\0\x1b\xff\r\n").unwrap();
    let config = json!({"device_id":"test","com":5,"dll_path":"COM-DMMR-8.dll","dll_sha256":"e3bb4674f88e5a894c36cdb5e544f0b5a35691c2a0dbdc56719c9928f05bcea3","log_dir":directory.to_str().unwrap()});
    let (mut c, script) = config_controller(
        config,
        vec![
            exact("OpenDebugFile", json!([path.to_str().unwrap()]), 0, vec![]),
            step("CloseDebugFile", 0, vec![]),
        ],
    );
    assert!(
        call(&mut c, "begin_startup_diagnostics", vec![])
            .unwrap()
            .as_str()
            .unwrap()
            .contains("Native capture opened")
    );
    assert_eq!(
        call(&mut c, "end_startup_diagnostics", vec![]).unwrap(),
        json!("[DMMR native] ACK\\x00\\x1b\\xff")
    );
    assert!(c.get_attribute("_startup_log_path").unwrap().is_null());
    completed(&script);
    std::fs::remove_dir_all(directory).unwrap();
}

#[test]
fn protocol_journal_is_bounded_and_written_only_under_supplied_directory() {
    let directory = temporary_dir();
    let (mut c, _) = config_controller(
        json!({"device_id":"test","com":5,"log_dir":directory.to_str().unwrap()}),
        vec![],
    );
    for n in 0..130 {
        call(
            &mut c,
            "record_protocol_event",
            vec![json!({"kind":"test","n":n})],
        )
        .unwrap();
    }
    let report = call(&mut c, "protocol_diagnostics", vec![]).unwrap();
    assert_eq!(report["event_count"], json!(130));
    assert_eq!(report["events"].as_array().unwrap().len(), 128);
    let text = std::fs::read_to_string(directory.join("dmmr_protocol_com5.jsonl")).unwrap();
    assert_eq!(text.lines().count(), 130);
    std::fs::remove_dir_all(directory).unwrap();
}

#[test]
fn cancellation_and_late_dll_return_do_not_authorize_recovery_or_another_call() {
    let ctx = Context::test(5.0);
    let mut steps = connect_steps();
    steps.push(Step {
        cancel: Some(ctx.clone()),
        ..step("GetModuleCurent", 0, vec![json!(1e-12), json!(0)])
    });
    let (mut c, script) = controller(steps);
    call(&mut c, "connect", vec![]).unwrap();
    configure(&mut c, json!({"0":[0,false]}), false);
    assert!(
        c.call("get_module_current", &[json!(0)], &json!({}), &ctx)
            .is_err()
    );
    assert!(
        c.call(
            "recover_read",
            &[json!(-10), json!("read")],
            &json!({}),
            &ctx
        )
        .is_err()
    );
    completed(&script);
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
    let mut c = Controller::new(&json!({"device_id":"slow","com":5}), Box::new(Slow)).unwrap();
    assert_eq!(
        c.call("get_state", &[], &json!({}), &Context::test(0.001))
            .unwrap_err()
            .kind,
        "TimeoutError"
    );
    assert_eq!(c.get_attribute("_transport_poisoned").unwrap(), json!(true));
    assert_eq!(call(&mut c, "disconnect", vec![]).unwrap(), json!(false));
    let mut c = Controller::new(&json!({"device_id":"slow","com":5}), Box::new(Slow)).unwrap();
    let ctx = Context::test(5.0).with_native_timeout(std::time::Duration::from_millis(1));
    assert_eq!(
        c.call("get_state", &[], &json!({}), &ctx).unwrap_err().kind,
        "TimeoutError"
    );
    assert!(!ctx.remaining().is_zero());
    assert_eq!(c.get_attribute("_dll_port_claimed").unwrap(), json!(true));
    assert_eq!(
        call(&mut c, "get_state", vec![]).unwrap_err().kind,
        "RuntimeError"
    );
}
