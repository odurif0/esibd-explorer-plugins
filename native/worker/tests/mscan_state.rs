#![cfg(feature = "mscan")]

use esibd_native_worker::{
    Backend, codec,
    context::Context,
    mscan::{Controller, amplitude_steps},
};
use serde_json::{Value, json};

fn settings(mode: &str) -> Value {
    json!({"start":10.0,"stop":30.0,"step":10.0,"mode":mode,
        "settling_s":0.004,"settle_timeout":0.15,"voltage_tolerance":1.0,
        "integration_s":0.012,"time_step_s":0.04,"sweep_rate":250.0,"offset_factor":0.1})
}

fn observation() -> Value {
    let rail = |n: u8| {
        json!({"identity":format!("rail{n}"),"device_identity":"psu1","name":format!("PSU_A_CH{n}"),
        "number":n,"registered":true,"real":true,"enabled":true,"active":true,"initialized":true,
        "on":true,"busy":false,"shutdown":false,"unit":"V","use_monitors":true,"readback_status":"Valid",
        "revision":0,"value":40.0 + 10.0*n as f64,"monitor":40.0 + 10.0*n as f64,"min":0.0,"max":250.0,
        "hardware_voltage_limit":200.0,"hardware_current_limit":0.5,"current_limit_readback":0.01,
        "voltage_setpoint_readback":40.0 + 10.0*n as f64,"current_readback":0.002})
    };
    json!({"sequence":1,"wall":100.0,"mono":0.0,
        "amx":{"identity":"amx1","initialized":true,"on":true,"busy":false,"selected":[0,1],
            "links":[["psu_ch01","PSU_A"]],"rows":[{"state":"Periodic","timing":{"period":200,"high":100}},
                {"state":"Periodic","timing":{"period":200,"high":100,"phase":100}}]},
        "rails":[rail(0),rail(1)],
        "detector":{"identity":"detector1","name":"Detector","device_name":"DMMR","module":3,
            "registered":true,"real":true,"unit":"A","enabled":true,"initialized":true,
            "acquiring":true,"recording":true,"history_id":"history1","interval_ms":2.0,
            "times":[99.996,99.998,100.0],"values":[40.0,40.0,40.0]},
        "offset":null})
}

fn call(
    c: &mut Controller,
    name: &str,
    args: &[Value],
) -> esibd_native_worker::error::Result<Value> {
    c.call(name, args, &json!({}), &Context::test(2.0))
}

struct Rig {
    controller: Controller,
    obs: Value,
    settings: Value,
    status: Value,
    writes: Vec<(bool, Vec<f64>)>,
    record: bool,
    quantize: bool,
    sample: Option<f64>,
}

impl Rig {
    fn new(mode: &str) -> Self {
        Self {
            controller: Controller::new(&json!({})).unwrap(),
            obs: observation(),
            settings: settings(mode),
            status: Value::Null,
            writes: Vec::new(),
            record: true,
            quantize: false,
            sample: None,
        }
    }
    fn start(&mut self) {
        self.status = call(
            &mut self.controller,
            "start",
            &[self.settings.clone(), self.obs.clone()],
        )
        .unwrap();
    }
    fn tick(&mut self, dt: f64) {
        self.obs["sequence"] = json!(self.obs["sequence"].as_u64().unwrap() + 1);
        self.obs["wall"] = json!(self.obs["wall"].as_f64().unwrap() + dt);
        self.obs["mono"] = json!(self.obs["mono"].as_f64().unwrap() + dt);
        if self.record && dt > 0.0 {
            let t = self.obs["wall"].clone();
            let sample = self
                .sample
                .map(codec::float)
                .unwrap_or_else(|| self.obs["rails"][0]["value"].clone());
            self.obs["detector"]["times"]
                .as_array_mut()
                .unwrap()
                .push(t);
            self.obs["detector"]["values"]
                .as_array_mut()
                .unwrap()
                .push(sample);
        }
    }
    fn command(&mut self) -> Value {
        let action = self.status["action"].clone();
        assert_eq!(action["kind"], "command");
        let targets: Vec<f64> = action["targets"]
            .as_array()
            .unwrap()
            .iter()
            .map(|v| v.as_f64().unwrap())
            .collect();
        self.writes
            .push((action["restore"].as_bool().unwrap(), targets.clone()));
        let started = self.obs["wall"].clone();
        for (r, target) in self.obs["rails"]
            .as_array_mut()
            .unwrap()
            .iter_mut()
            .zip(targets)
        {
            if r["value"].as_f64().unwrap() != target {
                r["revision"] = json!(r["revision"].as_u64().unwrap() + 1);
            }
            let actual = if self.quantize {
                target - 0.001
            } else {
                target
            };
            r["value"] = json!(actual);
            r["monitor"] = json!(actual);
            r["voltage_setpoint_readback"] = json!(actual);
        }
        if let Some(target) = action["offset"].as_f64() {
            let actual = (target * 1000.0).round() / 1000.0;
            self.obs["offset"]["value"] = json!(actual);
            self.obs["offset"]["monitor"] = json!(actual);
        }
        self.tick(0.0001);
        json!({"session":action["session"],"action_id":action["action_id"],
            "start_wall":started,"end_wall":self.obs["wall"],"end_mono":self.obs["mono"],
            "revisions":self.obs["rails"].as_array().unwrap().iter().map(|r| r["revision"].clone()).collect::<Vec<_>>(),
            "values":self.obs["rails"].as_array().unwrap().iter().map(|r| r["value"].clone()).collect::<Vec<_>>(),
            "offset_value":self.obs["offset"].get("value")})
    }
    fn step(&mut self) -> esibd_native_worker::error::Result<()> {
        let ack = if self.status["action"]["kind"] == "command" {
            self.command()
        } else {
            self.tick(0.002);
            Value::Null
        };
        self.status = call(&mut self.controller, "advance", &[self.obs.clone(), ack])?;
        Ok(())
    }
    fn until(&mut self, phase: &str) {
        for _ in 0..500 {
            if self.status["state"] == phase {
                return;
            }
            self.step().unwrap();
        }
        panic!("did not reach {phase}: {}", self.status);
    }
    fn run(&mut self) -> Value {
        if self.status.is_null() {
            self.start();
        }
        self.until("finished");
        call(&mut self.controller, "result", &[]).unwrap()
    }
    fn fault(&mut self, text: &str) {
        let e = self.step().unwrap_err();
        assert!(
            e.message.to_lowercase().contains(&text.to_lowercase()),
            "{e}"
        );
        let status = call(&mut self.controller, "snapshot", &[]).unwrap();
        assert_eq!(status["status"], "error");
        assert!(status["action"].is_null());
        assert!(!self.writes.iter().any(|(restore, _)| *restore));
    }
}

#[test]
fn bounded_inclusive_plans_match_python_examples() {
    for (a, b, s, want) in [
        (0.0, 1.0, 0.4, vec![0.0, 0.4, 0.8]),
        (1.0, 0.0, 0.4, vec![1.0, 0.6, 0.2]),
        (100.0, 80.0, 10.0, vec![100.0, 90.0, 80.0]),
        (0.0, 0.3, 0.1, vec![0.0, 0.1, 0.2, 0.3]),
    ] {
        let result = amplitude_steps(a, b, s).unwrap();
        for (got, want) in result.iter().zip(want) {
            assert!((got - want).abs() < 1e-12);
        }
        assert!(result.iter().all(|v| *v >= a.min(b) && *v <= a.max(b)));
    }
    assert_eq!(amplitude_steps(0.0, 100_000.0, 1.0).unwrap().len(), 100_001);
    for (a, b, s) in [
        (0.0, 0.0, 1.0),
        (-1.0, 1.0, 1.0),
        (0.0, 10.0, 0.0),
        (0.0, 10.0, -1.0),
        (0.0, 10.0, 20.0),
        (0.0, f64::NAN, 1.0),
        (0.0, 1e6, 1e-9),
        (0.0, 1.0, f64::INFINITY),
    ] {
        assert!(amplitude_steps(a, b, s).is_err());
    }
}

#[test]
fn inactive_settings_do_not_control_the_other_mode() {
    for mode in ["Step by step", "Continuous"] {
        let mut r = Rig::new(mode);
        if mode == "Continuous" {
            r.settings["step"] = codec::float(f64::NAN);
            r.settings["integration_s"] = json!(-10.0);
        } else {
            r.settings["sweep_rate"] = codec::float(f64::NAN);
            r.settings["time_step_s"] = json!(-10.0);
        }
        assert_eq!(r.run()["validation"]["status"], "completed");
    }
}

#[test]
fn invalid_timing_counts_and_rates_fail_without_starting() {
    for (key, value) in [
        ("start", json!(-1)),
        ("stop", codec::float(f64::NAN)),
        ("step", json!(0)),
        ("integration_s", json!(0)),
        ("settling_s", json!(0)),
        ("settle_timeout", json!(0.001)),
        ("voltage_tolerance", codec::float(f64::INFINITY)),
        ("mode", json!("unknown")),
    ] {
        let mut r = Rig::new("Step by step");
        r.settings[key] = value;
        assert!(
            call(&mut r.controller, "start", &[r.settings, r.obs]).is_err(),
            "{key}"
        );
        assert_eq!(r.controller.get_attribute("state").unwrap(), "idle");
    }
    for rate in [0.0, -1.0, f64::NAN, f64::INFINITY, 1e308, 1e-308, 1000.0] {
        let mut r = Rig::new("Continuous");
        r.settings["sweep_rate"] = codec::float(rate);
        assert!(
            call(&mut r.controller, "start", &[r.settings, r.obs]).is_err(),
            "rate {rate}"
        );
    }
}

#[test]
fn finite_values_that_overflow_combined_windows_are_rejected() {
    let mut r = Rig::new("Step by step");
    r.settings["integration_s"] = json!(1e308);
    r.settings["settle_timeout"] = json!(1e308);
    let error = call(&mut r.controller, "start", &[r.settings, r.obs]).unwrap_err();
    assert!(error.message.contains("finite acquisition window"));
    assert_eq!(r.controller.get_attribute("state").unwrap(), "idle");
}

#[test]
fn preflight_checks_requested_stop_even_when_unaligned_and_restoration() {
    for restore in [false, true] {
        let mut r = Rig::new("Step by step");
        if restore {
            r.obs["rails"][1]["value"] = json!(220.0);
            r.obs["rails"][1]["voltage_setpoint_readback"] = json!(220.0);
        } else {
            r.settings["stop"] = json!(210.0);
            r.settings["step"] = json!(120.0);
        }
        assert!(call(&mut r.controller, "prepare", &[r.settings, r.obs]).is_err());
    }
}

#[test]
fn invalid_initial_sources_limits_and_confirmation_block_before_writes() {
    for (key, value) in [
        ("enabled", json!(false)),
        ("active", json!(false)),
        ("initialized", json!(false)),
        ("on", json!(false)),
        ("shutdown", json!(true)),
        ("registered", json!(false)),
        ("real", json!(false)),
        ("busy", json!(true)),
        ("unit", json!("A")),
        ("use_monitors", json!(false)),
        ("monitor", codec::float(f64::NAN)),
        ("hardware_voltage_limit", json!(29.0)),
        ("hardware_current_limit", json!(0.009)),
        ("current_limit_readback", json!(0.0)),
        ("current_readback", json!(0.01)),
        ("current_readback", json!(-0.001)),
        ("voltage_setpoint_readback", json!(49.0)),
    ] {
        let mut r = Rig::new("Step by step");
        r.obs["rails"][1][key] = value;
        assert!(
            call(&mut r.controller, "start", &[r.settings, r.obs]).is_err(),
            "{key}"
        );
    }
}

#[test]
fn unavailable_amx_or_detector_never_produces_an_action() {
    for key in ["on", "initialized"] {
        let mut r = Rig::new("Step by step");
        r.obs["amx"][key] = json!(false);
        assert!(call(&mut r.controller, "start", &[r.settings, r.obs]).is_err());
    }
    for (key, value) in [
        ("device_name", json!("Other")),
        ("module", json!(-1)),
        ("unit", json!("V")),
        ("real", json!(false)),
        ("acquiring", json!(false)),
        ("recording", json!(false)),
        ("registered", json!(false)),
    ] {
        let mut r = Rig::new("Step by step");
        r.obs["detector"][key] = value;
        assert!(
            call(&mut r.controller, "start", &[r.settings, r.obs]).is_err(),
            "{key}"
        );
    }
}

#[test]
fn stepped_restores_distinct_initials_only_after_closed_valid_windows() {
    let mut r = Rig::new("Step by step");
    let result = r.run();
    assert_eq!(result["validation"]["status"], "completed");
    assert_eq!(result["data"], json!([10.0, 20.0, 30.0]));
    assert_eq!(
        result["validation"]["rail_v"],
        json!([[10.0, 10.0], [20.0, 20.0], [30.0, 30.0]])
    );
    assert_eq!(
        r.writes,
        vec![
            (false, vec![10.0, 10.0]),
            (false, vec![20.0, 20.0]),
            (false, vec![30.0, 30.0]),
            (true, vec![40.0, 50.0])
        ]
    );
    for sample in result["validation"]["samples"].as_array().unwrap() {
        assert!(sample[0].as_u64().unwrap() > 0);
    }
}

#[test]
fn quantized_echo_is_acknowledged_once_not_an_external_request() {
    let mut r = Rig::new("Step by step");
    r.quantize = true;
    let result = r.run();
    assert_eq!(result["validation"]["status"], "completed");
    assert_eq!(result["steps"], json!([10.0, 20.0, 30.0]));
    assert_eq!(result["validation"]["rail_v"][0], json!([9.999, 9.999]));
    assert_eq!(
        call(&mut r.controller, "snapshot", &[]).unwrap()["revisions"],
        json!([4, 4])
    );
}

#[test]
fn noop_command_keeps_confirmed_echo() {
    let mut r = Rig::new("Step by step");
    r.settings["start"] = json!(40.0);
    r.settings["stop"] = json!(60.0);
    r.start();
    r.step().unwrap();
    assert_eq!(r.status["revisions"], json!([0, 1]));
    r.obs["rails"][0]["voltage_setpoint_readback"] = json!(40.001);
    r.fault("setpoint changed outside");
}

#[test]
fn later_hardware_echo_changes_are_not_hidden_by_voltage_tolerance() {
    let mut r = Rig::new("Step by step");
    r.quantize = true;
    r.start();
    r.until("acquiring");
    r.obs["rails"][0]["voltage_setpoint_readback"] = json!(9.998);
    r.fault("setpoint changed outside");
}

#[test]
fn live_identity_waveform_link_limits_or_requests_abort() {
    for mode in ["Step by step", "Continuous"] {
        for fault in [
            "identity", "links", "waveform", "revision", "shutdown", "min", "capacity", "ilim",
            "current",
        ] {
            let mut r = Rig::new(mode);
            r.start();
            r.step().unwrap();
            match fault {
                "identity" => r.obs["rails"][0]["identity"] = json!("reconnected"),
                "links" => r.obs["amx"]["links"] = json!([["psu_ch01", "PSU_B"]]),
                "waveform" => r.obs["amx"]["rows"][0]["timing"]["high"] = json!(101),
                "revision" => r.obs["rails"][0]["revision"] = json!(2),
                "shutdown" => r.obs["rails"][0]["shutdown"] = json!(true),
                "min" => r.obs["rails"][0]["min"] = json!(11.0),
                "capacity" => r.obs["rails"][0]["hardware_current_limit"] = json!(0.009),
                "ilim" => r.obs["rails"][0]["current_limit_readback"] = json!(0.008),
                "current" => r.obs["rails"][0]["current_readback"] = json!(0.01),
                _ => unreachable!(),
            }
            r.fault("");
            assert_eq!(r.writes.len(), 1, "{mode} {fault}");
        }
    }
}

#[test]
fn busy_or_nonfinite_readbacks_restart_settling_and_timeout() {
    let mut r = Rig::new("Step by step");
    r.start();
    r.step().unwrap();
    r.obs["rails"][0]["busy"] = json!(true);
    r.obs["rails"][0]["monitor"] = codec::float(f64::NAN);
    r.tick(0.2);
    r.fault("settling timed out");
    let result = call(&mut r.controller, "result", &[]).unwrap();
    assert_eq!(result["data"][0], codec::float(f64::NAN));
}

#[test]
fn acquisition_cannot_accept_busy_invalid_or_out_of_tolerance_readback() {
    for (key, value) in [
        ("busy", json!(true)),
        ("monitor", codec::float(f64::NAN)),
        ("monitor", json!(5.0)),
        ("voltage_setpoint_readback", codec::float(f64::NAN)),
    ] {
        let mut r = Rig::new("Step by step");
        r.start();
        r.until("acquiring");
        r.obs["rails"][0][key] = value;
        r.fault("during acquisition");
    }
}

#[test]
fn window_is_open_on_start_closed_on_end_and_needs_a_closing_sample() {
    let mut r = Rig::new("Step by step");
    r.start();
    r.until("acquiring");
    let start = r.obs["wall"].as_f64().unwrap();
    let begin_mono = r.obs["mono"].as_f64().unwrap();
    r.obs["detector"]["times"] = json!([start - 0.001, start, start + 0.006, start + 0.012]);
    r.obs["detector"]["values"] = json!([1000.0, 1000.0, 2.0, 4.0]);
    r.record = false;
    r.obs["wall"] = json!(start + 0.02);
    r.obs["mono"] = json!(begin_mono + 0.02);
    r.step().unwrap();
    let result = call(&mut r.controller, "result", &[]).unwrap();
    assert_eq!(result["data"][0], json!(3.0));
    assert_eq!(result["validation"]["samples"][0], json!([2]));
}

#[test]
fn invalid_samples_are_not_zero_or_nanmean_and_zero_is_valid() {
    for (sample, mean, status) in [
        (f64::NAN, codec::float(f64::NAN), "invalid detector data"),
        (
            f64::INFINITY,
            codec::float(f64::NAN),
            "invalid detector data",
        ),
        (0.0, json!(0.0), "acquired"),
    ] {
        let mut r = Rig::new("Step by step");
        r.sample = Some(sample);
        let result = r.run();
        assert_eq!(result["validation"]["status"], "completed");
        assert_eq!(result["data"][0], mean);
        assert_eq!(result["validation"]["point_status"][0], status);
    }
}

#[test]
fn reset_misaligned_nonmonotonic_truncated_and_missing_history_fail_closed() {
    for fault in [
        "reset",
        "misaligned",
        "nonmonotonic",
        "truncated",
        "missing",
    ] {
        let mut r = Rig::new("Step by step");
        r.start();
        r.until("acquiring");
        match fault {
            "reset" => r.obs["detector"]["history_id"] = json!("new-history"),
            "misaligned" => {
                r.obs["detector"]["values"].as_array_mut().unwrap().pop();
            }
            "nonmonotonic" => r.obs["detector"]["times"][0] = json!(101.0),
            "truncated" => {
                r.obs["detector"]["times"] = json!([r.obs["wall"].as_f64().unwrap() + 0.002]);
                r.obs["detector"]["values"] = json!([10.0]);
                r.tick(0.02);
            }
            "missing" => {
                r.record = false;
                r.tick(0.2);
            }
            _ => unreachable!(),
        }
        r.fault("");
    }
}

#[test]
fn cancellation_holds_setpoints_and_reset_retires_old_acknowledgements() {
    let mut r = Rig::new("Step by step");
    r.start();
    r.until("acquiring");
    let old_session = r.status["session"].clone();
    let stopped = call(&mut r.controller, "cancel", &[]).unwrap();
    assert_eq!(stopped["status"], "stopped");
    assert!(stopped["action"].is_null());
    assert_eq!(r.writes.len(), 1);
    call(&mut r.controller, "reset", &[]).unwrap();
    r.obs["sequence"] = json!(r.obs["sequence"].as_u64().unwrap() + 1);
    r.start();
    assert_ne!(r.status["session"], old_session);
    let mut ack = r.command();
    ack["session"] = old_session;
    let e = call(&mut r.controller, "advance", &[r.obs.clone(), ack]).unwrap_err();
    assert!(e.message.contains("Stale"));
}

#[test]
fn context_cancel_and_deadline_do_not_silently_leave_scan_running() {
    for cancelled in [true, false] {
        let mut r = Rig::new("Step by step");
        r.start();
        let ctx = Context::test(if cancelled { 2.0 } else { 0.0 });
        if cancelled {
            ctx.cancel();
        }
        assert!(
            r.controller
                .call("advance", &[r.obs], &json!({}), &ctx)
                .is_err()
        );
        assert_eq!(
            r.controller.get_attribute("status").unwrap(),
            if cancelled { "stopped" } else { "error" }
        );
        assert!(
            !r.controller
                .get_attribute("recording")
                .unwrap()
                .as_bool()
                .unwrap()
        );
    }
}

#[test]
fn pending_ack_is_required_and_cannot_be_duplicated_or_faked() {
    for fault in ["missing", "id", "revisions", "value", "clock"] {
        let mut r = Rig::new("Step by step");
        r.start();
        let mut ack = if fault == "missing" {
            Value::Null
        } else {
            r.command()
        };
        match fault {
            "id" => ack["action_id"] = json!(9),
            "revisions" => ack["revisions"] = json!([2, 1]),
            "value" => ack["revisions"] = json!([0, 1]),
            "clock" => ack["end_mono"] = json!(99.0),
            _ => {}
        }
        assert!(
            call(&mut r.controller, "advance", &[r.obs, ack]).is_err(),
            "{fault}"
        );
        assert_eq!(r.controller.get_attribute("status").unwrap(), "error");
    }
}

#[test]
fn initial_and_final_confirmation_are_required_even_after_all_points() {
    let mut r = Rig::new("Step by step");
    r.start();
    while !r.status["action"]["restore"].as_bool().unwrap_or(false) {
        r.step().unwrap();
    }
    assert_eq!(
        call(&mut r.controller, "result", &[]).unwrap()["validation"]["status"],
        "running"
    );
    let ack = r.command();
    r.obs["rails"][0]["busy"] = json!(true);
    r.status = call(&mut r.controller, "advance", &[r.obs.clone(), ack]).unwrap();
    r.tick(0.2);
    assert!(r.step().unwrap_err().message.contains("settling timed out"));
    assert_eq!(r.controller.get_attribute("status").unwrap(), "error");
}

#[test]
fn continuous_intervals_restore_and_preserve_raw_transition_samples() {
    let mut r = Rig::new("Continuous");
    let result = r.run();
    assert_eq!(result["validation"]["status"], "completed");
    assert_eq!(
        result["validation"]["point_status"],
        json!(vec!["acquired during sweep"; 3])
    );
    let raw = call(&mut r.controller, "raw", &[]).unwrap();
    assert!(
        !raw["data"]["detector_current"]
            .as_array()
            .unwrap()
            .is_empty()
    );
    assert_eq!(r.writes.last().unwrap(), &(true, vec![40.0, 50.0]));
    let starts = result["validation"]["window_start"].as_array().unwrap();
    let ends = result["validation"]["window_end"].as_array().unwrap();
    assert_eq!(ends[0], starts[1]);
    assert_eq!(ends[1], starts[2]);
    for j in 0..3 {
        assert!(ends[j].as_f64().unwrap() - starts[j].as_f64().unwrap() >= 0.04 - 1e-10);
    }
}

#[test]
fn descending_continuous_plan_and_signed_rate() {
    let mut r = Rig::new("Continuous");
    r.settings["start"] = json!(30.0);
    r.settings["stop"] = json!(10.0);
    let result = r.run();
    assert_eq!(result["steps"], json!([30.0, 20.0, 10.0]));
    assert_eq!(result["metadata"]["requested_rate_v_s"], json!(-250.0));
}

#[test]
fn cadence_uses_longest_finite_sample_gap_not_recorder_ticks_or_median() {
    for duration in [0.099, 0.1, 0.2] {
        let mut r = Rig::new("Continuous");
        r.settings["time_step_s"] = json!(duration);
        r.settings["sweep_rate"] = json!(10.0);
        r.obs["detector"]["interval_ms"] = json!(1.0);
        r.obs["detector"]["times"] = json!([99.8, 99.85, 99.9, 99.95, 100.0]);
        r.obs["detector"]["values"] = json!([
            7.0,
            codec::float(f64::NAN),
            7.0,
            codec::float(f64::NAN),
            7.0
        ]);
        let result = call(&mut r.controller, "prepare", &[r.settings, r.obs]);
        assert_eq!(result.is_ok(), duration >= 0.1);
        if let Ok(p) = result {
            assert_eq!(
                p["metadata"]["detector_cadence"]["minimum_time_step_s"],
                json!(0.1)
            );
        }
    }
    let mut r = Rig::new("Continuous");
    r.obs["detector"]["times"] = json!([99.55, 99.65, 99.75, 100.0]);
    r.obs["detector"]["values"] = json!([7.0, 7.0, 7.0, 7.0]);
    assert!(
        call(&mut r.controller, "prepare", &[r.settings, r.obs])
            .unwrap_err()
            .message
            .contains("0.25")
    );
}

#[test]
fn unavailable_or_old_cadence_blocks_initial_command() {
    for fault in [
        "empty",
        "one",
        "nan",
        "stale",
        "future",
        "configured",
        "epoch",
    ] {
        let mut r = Rig::new("Continuous");
        match fault {
            "empty" => {
                r.obs["detector"]["times"] = json!([]);
                r.obs["detector"]["values"] = json!([]);
            }
            "one" => {
                r.obs["detector"]["values"] =
                    json!([codec::float(f64::NAN), codec::float(f64::NAN), 1.0])
            }
            "nan" => r.obs["detector"]["values"] = json!(vec![codec::float(f64::NAN); 3]),
            "stale" => r.obs["wall"] = json!(103.0),
            "future" => r.obs["wall"] = json!(99.0),
            "configured" => r.obs["detector"]["interval_ms"] = json!(500.0),
            "epoch" => r.obs["detector"]["cadence_since"] = json!(99.999),
            _ => {}
        }
        assert!(
            call(&mut r.controller, "start", &[r.settings, r.obs]).is_err(),
            "{fault}"
        );
    }
}

#[test]
fn no_new_finite_continuous_sample_means_no_next_command_or_point() {
    for sample in [None, Some(f64::NAN)] {
        let mut r = Rig::new("Continuous");
        r.start();
        r.until("continuous");
        r.record = sample.is_some();
        r.sample = sample;
        r.tick(0.041);
        r.fault("no new valid DMMR sample");
        assert_eq!(r.writes.len(), 1);
        let result = call(&mut r.controller, "result", &[]).unwrap();
        assert_eq!(result["data"][0], codec::float(f64::NAN));
    }
}

#[test]
fn late_or_unconfirmed_continuous_target_never_sends_catchup() {
    let mut r = Rig::new("Continuous");
    r.start();
    r.until("continuous");
    r.tick(0.1);
    r.fault("time step missed");
    let mut r = Rig::new("Continuous");
    r.start();
    r.until("continuous");
    while r.status["action"]["kind"] != "command" {
        r.step().unwrap();
    }
    let ack = r.command();
    r.obs["rails"][0]["busy"] = json!(true);
    r.status = call(&mut r.controller, "advance", &[r.obs.clone(), ack]).unwrap();
    r.tick(0.041);
    r.obs["rails"][0]["busy"] = json!(false);
    r.fault("within the continuous time step");
    assert_eq!(r.writes.len(), 2);
}

#[test]
fn continuous_readiness_must_not_drop_after_confirmation() {
    let mut r = Rig::new("Continuous");
    r.start();
    r.until("continuous");
    r.obs["rails"][0]["monitor"] = json!(0.0);
    r.fault("after reaching the continuous target");
}

#[test]
fn continuous_small_lateness_does_not_shorten_the_next_interval() {
    let mut r = Rig::new("Continuous");
    r.start();
    r.until("continuous");
    // Explorer's recorder continues independently while a scan callback is late.
    for _ in 0..30 {
        r.tick(0.002);
    }
    r.step().unwrap();
    let result = r.run();
    assert_eq!(result["validation"]["status"], "completed");
    let raw = call(&mut r.controller, "raw", &[]).unwrap();
    let cmd = &raw["data"]["command_start"];
    assert!(cmd[2].as_f64().unwrap() - cmd[1].as_f64().unwrap() >= 0.04 - 1e-9);
}

#[test]
fn continuous_rolling_history_retains_cursor_or_aborts_on_loss() {
    for lost in [false, true] {
        let mut r = Rig::new("Continuous");
        r.start();
        r.until("continuous");
        for _ in 0..4 {
            r.step().unwrap();
        }
        let times = r.obs["detector"]["times"].as_array_mut().unwrap();
        let keep = if lost { 0 } else { 3 };
        let len = times.len();
        times.drain(..len - keep);
        let values = r.obs["detector"]["values"].as_array_mut().unwrap();
        let len = values.len();
        values.drain(..len - keep);
        if lost {
            r.fault("history buffer truncated");
        } else {
            assert_eq!(r.run()["validation"]["status"], "completed");
        }
    }
}

#[test]
fn continuous_stop_keeps_partial_raw_without_fabricating_current_point() {
    let mut r = Rig::new("Continuous");
    r.start();
    r.until("continuous");
    for _ in 0..4 {
        r.step().unwrap();
    }
    let stopped = call(&mut r.controller, "cancel", &[]).unwrap();
    assert_eq!(stopped["status"], "stopped");
    let raw = call(&mut r.controller, "raw", &[]).unwrap();
    assert!(raw["lengths"]["detector_time"].as_u64().unwrap() > 0);
    let result = call(&mut r.controller, "result", &[]).unwrap();
    assert_eq!(result["data"][0], codec::float(f64::NAN));
    assert_eq!(r.writes.len(), 1);
}

#[test]
fn detector_interval_identity_and_clock_changes_abort_in_both_modes() {
    for mode in ["Step by step", "Continuous"] {
        for fault in ["interval", "identity", "module", "clock", "sequence"] {
            let mut r = Rig::new(mode);
            r.start();
            r.until(if mode == "Continuous" {
                "continuous"
            } else {
                "acquiring"
            });
            match fault {
                "interval" => r.obs["detector"]["interval_ms"] = json!(100.0),
                "identity" => r.obs["detector"]["identity"] = json!("replaced"),
                "module" => r.obs["detector"]["module"] = json!(5),
                "clock" => r.obs["wall"] = json!(r.obs["wall"].as_f64().unwrap() + 1.0),
                "sequence" => r.obs["sequence"] = json!(0),
                _ => {}
            }
            r.fault("");
        }
    }
}

fn add_offset(r: &mut Rig) {
    r.obs["offset"] = json!({"identity":"offset1","name":"Offset","registered":true,"real":true,
        "enabled":true,"active":true,"initialized":true,"on":true,"busy":false,"shutdown":false,
        "state":"confirmed","value":-5.0,"monitor":-5.0,"min":-200.0,"max":200.0});
}

#[test]
fn offset_commands_round_confirm_and_restore_distinct_initial_offset() {
    let mut r = Rig::new("Step by step");
    add_offset(&mut r);
    r.settings["offset_factor"] = json!(0.12345);
    let result = r.run();
    assert_eq!(result["validation"]["status"], "completed");
    assert_eq!(
        result["validation"]["offset_target"],
        json!([1.235, 2.469, 3.704])
    );
    assert_eq!(r.obs["offset"]["value"], json!(-5.0));
}

#[test]
fn offset_interlocks_bounds_external_edits_and_confirmation_are_enforced() {
    for fault in [
        "factor", "bounds", "state", "external", "shutdown", "identity",
    ] {
        let mut r = Rig::new("Step by step");
        add_offset(&mut r);
        if fault == "factor" {
            r.settings["offset_factor"] = codec::float(f64::NAN);
            assert!(call(&mut r.controller, "start", &[r.settings, r.obs]).is_err());
            continue;
        }
        if fault == "bounds" {
            r.obs["offset"]["max"] = json!(2.0);
            assert!(call(&mut r.controller, "start", &[r.settings, r.obs]).is_err());
            continue;
        }
        r.start();
        r.step().unwrap();
        match fault {
            "state" => r.obs["offset"]["state"] = json!("mismatch"),
            "external" => r.obs["offset"]["value"] = json!(2.0),
            "shutdown" => r.obs["offset"]["shutdown"] = json!(true),
            "identity" => r.obs["offset"]["identity"] = json!("new"),
            _ => {}
        }
        r.fault("offset");
    }
}

#[test]
fn pagination_and_raw_limits_are_explicit_and_bounded() {
    let mut r = Rig::new("Step by step");
    r.run();
    let page = call(&mut r.controller, "result", &[json!(1), json!(1)]).unwrap();
    assert_eq!(page["total"], json!(3));
    assert_eq!(page["data"], json!([20.0]));
    assert_eq!(
        call(&mut r.controller, "result", &[json!(9)]).unwrap()["count"],
        json!(0)
    );
    assert!(call(&mut r.controller, "result", &[json!(0), json!(4097)]).is_err());
    let mut r = Rig::new("Continuous");
    r.controller = Controller::new(&json!({"max_raw_samples":2})).unwrap();
    r.start();
    r.until("continuous");
    r.step().unwrap();
    r.step().unwrap();
    r.fault("limit exceeded");
}

#[test]
fn lifecycle_attributes_and_unsupported_operations_do_not_succeed_as_stubs() {
    let mut r = Rig::new("Step by step");
    r.start();
    assert!(call(&mut r.controller, "reset", &[]).is_err());
    assert!(call(&mut r.controller, "close_hardware", &[]).is_err());
    assert!(
        r.controller
            .set_attribute("recording", json!(true))
            .is_err()
    );
    r.controller
        .set_attribute("recording", json!(false))
        .unwrap();
    assert_eq!(r.controller.get_attribute("finished").unwrap(), json!(true));
    assert!(r.controller.get_attribute("dll").is_err());
    assert!(
        r.controller
            .set_attribute("status", json!("completed"))
            .is_err()
    );
    call(&mut r.controller, "reset", &[]).unwrap();
    assert_eq!(r.controller.get_attribute("state").unwrap(), json!("idle"));
}
