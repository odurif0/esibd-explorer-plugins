#![cfg(feature = "transmission")]

use esibd_native_worker::{
    Backend,
    context::Context,
    transmission::{Controller, fit_peak, pick_peak},
};
use serde_json::{Value, json};
use std::collections::BTreeMap;

fn configuration(strategy: &str) -> Value {
    json!({"options":{"strategy":strategy,"settle_s":0.0,"average_s":1.0,"poll_s":0.5,
        "settle_timeout_s":2.0,"readback_tolerance":0.01,"seed":3,"verify_pairs":3,"reference_every":2},
        "pressure":{"P":[2.0,6.0]},
        "stages":[{"name":"Tune","aperture":"A","downstream":["D"],"budget":24,
            "knobs":[{"name":"X","channels":{"X":1.0},"window":[-5.0,5.0],"max_step":1.0}]}]})
}

struct Instrument {
    time: f64,
    values: BTreeMap<String, f64>,
    limits: BTreeMap<String, [f64; 2]>,
    commands: Vec<BTreeMap<String, f64>>,
    marks: Vec<bool>,
    windows: usize,
    window_commands: Vec<usize>,
    off: bool,
    stop: bool,
    pressure: f64,
    missing_pressure: bool,
    discharge: bool,
    noisy_verify: bool,
    readback_stuck: bool,
    no_readback: bool,
    no_window: bool,
    sparse: usize,
    always_sparse: bool,
    thin: bool,
    no_beam: bool,
    lost: bool,
    ratio_loses: bool,
    optimum: f64,
    filter: bool,
    partial_command: bool,
}
impl Instrument {
    fn new() -> Self {
        Self {
            time: 1000.0,
            values: BTreeMap::from([("X".into(), 0.0), ("Y".into(), 0.0), ("F".into(), 5.0)]),
            limits: BTreeMap::from([
                ("X".into(), [-6.0, 6.0]),
                ("Y".into(), [-100.0, 100.0]),
                ("F".into(), [0.0, 30.0]),
            ]),
            commands: vec![],
            marks: vec![],
            windows: 0,
            window_commands: vec![],
            off: false,
            stop: false,
            pressure: 4.0,
            missing_pressure: false,
            discharge: false,
            noisy_verify: false,
            readback_stuck: false,
            no_readback: false,
            no_window: false,
            sparse: 0,
            always_sparse: false,
            thin: false,
            no_beam: false,
            lost: false,
            ratio_loses: false,
            optimum: 2.0,
            filter: false,
            partial_command: false,
        }
    }
    fn current(&self, name: &str) -> f64 {
        if name == "P" {
            return self.pressure;
        }
        let x = self.values["X"];
        let factor = if self.no_beam {
            1e-8
        } else if self.lost && x > 1.5 {
            0.005
        } else {
            1.0
        };
        let mut d = if self.ratio_loses {
            80e-12 - 2e-12 * x * x
        } else {
            (4e-10 - 1e-11 * (x - self.optimum).powi(2)).max(1e-12)
        };
        if self.noisy_verify && (14..=19).contains(&self.windows) {
            d += [1.0, 0.0, 0.0, -1.0, 1.0, 0.0][self.windows - 14] * 1e-10;
        }
        if self.filter {
            d *= (1.0 - ((self.values["F"] - 10.2) / 2.0).powi(2)).max(0.0);
        }
        match name {
            "D" => {
                if self.discharge {
                    2e-6
                } else {
                    d * factor
                }
            }
            "A" => (1e-9 - d) * factor,
            "N" => (160e-12 - 20e-12 * x) * factor,
            _ => 1e-10 * factor,
        }
    }
    fn error(&self, id: &Value, kind: &str, message: &str) -> Value {
        json!({"id":id,"time":self.time,"error":{"kind":kind,"message":message}})
    }
    fn observe(&mut self, action: &Value) -> Value {
        let op = action["op"].as_str().unwrap();
        let args = &action["args"];
        let id = &action["id"];
        if self.stop
            && action["cancellable"] == true
            && !(op == "measuring" && args["active"] == false)
        {
            return self.error(id, "Stopped", "operator stopped");
        }
        if self.off && matches!(op, "inspect" | "command" | "window") {
            return self.error(id, "InstrumentError", "device switched off");
        }
        let list = |name: &str| {
            args[name]
                .as_array()
                .unwrap()
                .iter()
                .map(|v| v.as_str().unwrap().to_owned())
                .collect::<Vec<_>>()
        };
        let value = match op {
            "now" => Value::Null,
            "inspect" => {
                let latest: BTreeMap<_, _> = list("latest")
                    .iter()
                    .filter(|n| !self.missing_pressure || *n != "P")
                    .map(|n| (n.clone(), self.current(n)))
                    .collect();
                let limits: BTreeMap<_, _> = list("limits")
                    .iter()
                    .map(|n| (n.clone(), self.limits[n]))
                    .collect();
                let readbacks: BTreeMap<_, _> = list("readbacks")
                    .iter()
                    .map(|n| {
                        (
                            n.clone(),
                            if self.no_readback {
                                Value::Null
                            } else {
                                json!(
                                    self.values[n] + if self.readback_stuck { 100.0 } else { 0.0 }
                                )
                            },
                        )
                    })
                    .collect();
                let setpoints: BTreeMap<_, _> = list("setpoints")
                    .iter()
                    .map(|n| (n.clone(), self.values[n]))
                    .collect();
                json!({"latest":latest,"limits":limits,"readbacks":readbacks,"setpoints":setpoints})
            }
            "command" => {
                let point: BTreeMap<String, f64> =
                    serde_json::from_value(args["values"].clone()).unwrap();
                self.values.extend(point.clone());
                self.commands.push(point);
                if self.partial_command {
                    return self.error(
                        id,
                        "InstrumentError",
                        "command partially applied; device failed",
                    );
                }
                Value::Null
            }
            "wait" => {
                self.time += args["seconds"].as_f64().unwrap();
                Value::Null
            }
            "measuring" => {
                self.marks.push(args["active"].as_bool().unwrap());
                Value::Null
            }
            "window" => {
                self.windows += 1;
                self.window_commands.push(self.commands.len());
                if self.no_window {
                    json!({"data":null})
                } else {
                    let sparse = self.always_sparse || self.windows <= self.sparse;
                    let data: BTreeMap<_, _> = list("channels")
                        .iter()
                        .map(|n| {
                            let data = if sparse && n != "P" {
                                if self.thin {
                                    json!([self.current(n), null, 1])
                                } else {
                                    json!([null, null, 0])
                                }
                            } else {
                                json!([self.current(n), 0.0, if n == "P" { 1 } else { 3 }])
                            };
                            (n.clone(), data)
                        })
                        .collect();
                    json!({"data":data,"diagnostics":{"mock":true}})
                }
            }
            other => panic!("Unexpected Explorer action {other}"),
        };
        json!({"id":id,"time":self.time,"value":value})
    }
}
struct Trace {
    result: Value,
    events: Vec<Value>,
    logs: Vec<Value>,
}
fn drive<F: FnMut(&mut Instrument, &Value)>(
    c: &mut Controller,
    i: &mut Instrument,
    method: &str,
    args: &[Value],
    mut hook: F,
) -> Trace {
    let ctx = Context::test(120.0);
    let mut response = c.call(method, args, &json!({}), &ctx).unwrap();
    let mut events = Vec::new();
    let mut logs = Vec::new();
    for _ in 0..50_000 {
        for event in response["events"].as_array().unwrap() {
            hook(i, event);
            events.push(event.clone());
        }
        logs.extend(response["logs"].as_array().unwrap().clone());
        if response["complete"] == true {
            return Trace {
                result: response["result"].clone(),
                events,
                logs,
            };
        }
        let observation = i.observe(&response["action"]);
        response = c.call("observe", &[observation], &json!({}), &ctx).unwrap();
    }
    panic!("Native plan did not finish within bounded test actions")
}
fn assert_steps(i: &Instrument, start: &BTreeMap<String, f64>, steps: &BTreeMap<String, f64>) {
    let mut previous = start.clone();
    for command in &i.commands {
        for (n, v) in command {
            assert!(
                (v - previous[n]).abs() <= steps[n] + 1e-8,
                "{n}: {} -> {v}",
                previous[n]
            );
            assert!(*v >= i.limits[n][0] && *v <= i.limits[n][1]);
            previous.insert(n.clone(), *v);
        }
    }
}

#[test]
fn coordinate_search_matches_quadratic_optimum_and_verifies_before_adopting() {
    let mut c = Controller::new(&configuration("coordinate")).unwrap();
    let mut i = Instrument::new();
    let start = i.values.clone();
    let t = drive(&mut c, &mut i, "run", &[], |_, _| {});
    assert_eq!(t.result["status"], "completed");
    let s = &t.result["stages"][0];
    assert_eq!(s["adopted"], true);
    assert!((s["final"]["X"].as_f64().unwrap() - 2.0).abs() < 1e-8);
    assert!(s["gain"].as_f64().unwrap() > 0.0);
    assert!(s["evaluations"].as_u64().unwrap() <= 24);
    assert_steps(&i, &start, &BTreeMap::from([("X".into(), 1.0)]));
    let roles: Vec<_> = t
        .events
        .iter()
        .filter(|e| e["kind"] == "evaluation" && e["data"]["record"]["kind"] == "verify")
        .map(|e| e["data"]["record"]["verify_role"].as_str().unwrap())
        .collect();
    assert_eq!(
        roles,
        vec!["best", "initial", "initial", "best", "best", "initial"]
    );
    assert!(
        t.events
            .iter()
            .any(|e| e["kind"] == "evaluation" && e["data"]["record"]["kind"] == "reference")
    );
    for kind in [
        "run_start",
        "stage_start",
        "reference",
        "step",
        "measure",
        "evaluation",
        "ask",
        "tell",
        "verify",
        "stage_done",
        "snapshot",
        "run_end",
    ] {
        assert!(t.logs.iter().any(|l| l["event"] == kind), "missing {kind}");
    }
    assert_eq!(
        i.marks.len(),
        2 * t.result["evaluations"].as_u64().unwrap() as usize + 4
    );
    assert!(
        i.marks
            .as_chunks::<2>()
            .0
            .iter()
            .all(|p| *p == [true, false])
    );
}

#[test]
fn bayesian_gp_improves_with_bounded_trust_region_and_no_stub_results() {
    let mut config = configuration("bayesian");
    config["stages"][0]["budget"] = json!(20);
    let mut c = Controller::new(&config).unwrap();
    let mut i = Instrument::new();
    let start = i.values.clone();
    let t = drive(&mut c, &mut i, "run", &[], |_, _| {});
    assert_eq!(t.result["status"], "completed", "{}", t.result);
    assert_eq!(t.result["stages"][0]["adopted"], true);
    assert!((i.values["X"] - 2.0).abs() < 0.6);
    assert_steps(&i, &start, &BTreeMap::from([("X".into(), 1.0)]));
    let ask = t.logs.iter().find(|l| l["event"] == "ask").unwrap();
    for key in [
        "fits",
        "lengthscales",
        "predicted",
        "predicted_std",
        "radius",
        "fit_s",
        "noise_floor",
    ] {
        assert!(!ask["data"][key].is_null(), "missing {key}");
    }
}

#[test]
fn ratio_gain_with_fewer_ions_is_never_adopted() {
    let mut config = configuration("coordinate");
    config["stages"][0]["normalize_by"] = json!(["N"]);
    let mut c = Controller::new(&config).unwrap();
    let mut i = Instrument::new();
    i.ratio_loses = true;
    let t = drive(&mut c, &mut i, "run", &[], |_, _| {});
    assert_eq!(t.result["status"], "completed");
    assert_eq!(t.result["stages"][0]["adopted"], false);
    assert!(i.values["X"].abs() < 1e-10);
    let verdict = t.logs.iter().find(|l| l["event"] == "verify").unwrap();
    assert_eq!(verdict["data"]["refused_fewer_ions"], true);
}

#[test]
fn soft_interlock_and_stop_restore_only_the_current_stage_in_bounded_steps() {
    for stop in [false, true] {
        let mut c = Controller::new(&configuration("coordinate")).unwrap();
        let mut i = Instrument::new();
        let start = i.values.clone();
        let t = drive(&mut c, &mut i, "run", &[], |i, e| {
            if e["kind"] == "evaluation" && e["data"]["record"]["index"] == 4 {
                if stop {
                    i.stop = true
                } else {
                    i.pressure = 9.0
                }
            }
        });
        assert_eq!(
            t.result["status"],
            if stop { "stopped" } else { "interlock" }
        );
        assert_eq!(i.values["X"], 0.0);
        assert_steps(&i, &start, &BTreeMap::from([("X".into(), 1.0)]));
        assert!(
            t.logs
                .iter()
                .any(|l| l["event"] == "step" && l["data"]["restoring"] == true)
        );
        assert!(
            i.marks
                .as_chunks::<2>()
                .0
                .iter()
                .all(|p| *p == [true, false])
        );
    }
}

#[test]
fn unavailable_pressure_or_discharge_is_a_soft_interlock_not_a_device_fault() {
    for discharge in [false, true] {
        let mut c = Controller::new(&configuration("coordinate")).unwrap();
        let mut i = Instrument::new();
        let t = drive(&mut c, &mut i, "run", &[], |i, e| {
            if e["kind"] == "evaluation" && e["data"]["record"]["index"] == 4 {
                if discharge {
                    i.discharge = true;
                } else {
                    i.missing_pressure = true;
                }
            }
        });
        assert_eq!(t.result["status"], "interlock");
        assert_eq!(i.values["X"], 0.0);
        assert!(t.result["reason"].as_str().unwrap().contains(if discharge {
            "discharge"
        } else {
            "unavailable"
        }));
    }
}

#[test]
fn a_lost_beam_is_penalized_and_not_adopted_as_better_transmission() {
    let mut c = Controller::new(&configuration("coordinate")).unwrap();
    let mut i = Instrument::new();
    i.lost = true;
    let t = drive(&mut c, &mut i, "run", &[], |_, _| {});
    assert_eq!(t.result["status"], "completed");
    let lost: Vec<_> = t
        .events
        .iter()
        .filter(|e| e["kind"] == "evaluation" && e["data"]["record"]["valid"] == false)
        .collect();
    assert!(!lost.is_empty());
    assert!(
        lost.iter()
            .all(|e| e["data"]["record"]["model_objective"] == 0.0)
    );
    assert!(i.values["X"] <= 1.5);
}

#[test]
fn a_positive_gain_below_paired_significance_retains_the_start() {
    let mut config = configuration("coordinate");
    config["stages"][0]["budget"] = json!(18);
    let mut c = Controller::new(&config).unwrap();
    let mut i = Instrument::new();
    i.noisy_verify = true;
    let t = drive(&mut c, &mut i, "run", &[], |_, _| {});
    assert_eq!(t.result["status"], "completed");
    let stage = &t.result["stages"][0];
    assert!(stage["gain"].as_f64().unwrap() > 0.0);
    assert!(stage["gain"].as_f64().unwrap() <= 2.0 * stage["gain_sem"].as_f64().unwrap());
    assert_eq!(stage["adopted"], false);
    assert_eq!(i.values["X"], 0.0);
}

#[test]
fn a_stop_in_the_second_stage_preserves_the_adopted_first_stage() {
    let mut config = configuration("coordinate");
    let mut second = config["stages"][0].clone();
    second["name"] = json!("Second");
    second["knobs"][0]["name"] = json!("Y");
    second["knobs"][0]["channels"] = json!({"Y":1.0});
    config["stages"].as_array_mut().unwrap().push(second);
    let mut c = Controller::new(&config).unwrap();
    let mut i = Instrument::new();
    let t = drive(&mut c, &mut i, "run", &[], |i, e| {
        if e["kind"] == "evaluation"
            && e["data"]["record"]["stage"] == "Second"
            && e["data"]["record"]["delta"][0] != 0.0
        {
            i.stop = true;
        }
    });
    assert_eq!(t.result["status"], "stopped");
    assert_eq!(t.result["stages"].as_array().unwrap().len(), 1);
    assert!((i.values["X"] - 2.0).abs() < 1e-8);
    assert_eq!(i.values["Y"], 0.0);
}

#[test]
fn stage_windows_are_clipped_and_a_too_narrow_window_refuses_before_a_write() {
    for narrow in [false, true] {
        let mut c = Controller::new(&configuration("coordinate")).unwrap();
        let mut i = Instrument::new();
        i.limits
            .insert("X".into(), if narrow { [-0.1, 0.1] } else { [-2.0, 2.0] });
        let t = drive(&mut c, &mut i, "run", &[], |_, _| {});
        assert_eq!(
            t.result["status"],
            if narrow {
                "configuration error"
            } else {
                "completed"
            }
        );
        if narrow {
            assert!(i.commands.is_empty());
        } else {
            assert!((i.values["X"] - 2.0).abs() < 1e-8);
        }
    }
}

#[test]
fn device_off_or_partial_command_failure_gets_no_followup_command_or_restore() {
    for partial in [false, true] {
        let mut c = Controller::new(&configuration("coordinate")).unwrap();
        let mut i = Instrument::new();
        let mut commands_at_fault = 0;
        let t = drive(&mut c, &mut i, "run", &[], |i, e| {
            if e["kind"] == "evaluation" && e["data"]["record"]["index"] == 4 {
                commands_at_fault = i.commands.len();
                if partial {
                    i.partial_command = true
                } else {
                    i.off = true
                }
            }
        });
        assert_eq!(t.result["status"], "instrument error");
        assert_eq!(i.commands.len(), commands_at_fault + usize::from(partial));
        assert!(!t.logs.iter().any(|l| l["event"] == "restore"));
    }
}

#[test]
fn readback_divergence_is_bounded_but_absent_readback_is_allowed() {
    for absent in [false, true] {
        let mut c = Controller::new(&configuration("coordinate")).unwrap();
        let mut i = Instrument::new();
        i.no_readback = absent;
        i.readback_stuck = !absent;
        let t = drive(&mut c, &mut i, "run", &[], |_, _| {});
        assert_eq!(
            t.result["status"],
            if absent {
                "completed"
            } else {
                "instrument error"
            }
        );
        if !absent {
            assert!(
                t.result["reason"]
                    .as_str()
                    .unwrap()
                    .contains("Readback did not reach")
            );
            assert_eq!(i.commands.len(), 1);
        }
    }
}

#[test]
fn sparse_windows_are_repeated_without_a_command_and_thin_currents_are_rejected() {
    for thin in [false, true] {
        let mut c = Controller::new(&configuration("coordinate")).unwrap();
        let mut i = Instrument::new();
        i.sparse = 2;
        i.thin = thin;
        let t = drive(&mut c, &mut i, "run", &[], |_, _| {});
        assert_eq!(t.result["status"], "completed", "{}", t.result);
        assert_eq!(&i.window_commands[..3], &[0, 0, 0]);
        assert_eq!(c.get_attribute("counts").unwrap()["empty_windows"], 2);
        assert!(
            i.marks
                .as_chunks::<2>()
                .0
                .iter()
                .all(|p| *p == [true, false])
        );
        let mut c = Controller::new(&configuration("coordinate")).unwrap();
        let mut i = Instrument::new();
        i.always_sparse = true;
        i.thin = thin;
        let t = drive(&mut c, &mut i, "run", &[], |_, _| {});
        assert_eq!(t.result["status"], "instrument error");
        assert_eq!(i.windows, 3);
        assert!(i.commands.is_empty());
        assert!(t.result["reason"].as_str().unwrap().contains(if thin {
            "Fewer than 2 samples"
        } else {
            "No valid samples"
        }));
    }
}

#[test]
fn windows_that_never_close_time_out_and_close_the_measuring_guard() {
    let mut c = Controller::new(&configuration("coordinate")).unwrap();
    let mut i = Instrument::new();
    i.no_window = true;
    let t = drive(&mut c, &mut i, "run", &[], |_, _| {});
    assert_eq!(t.result["status"], "instrument error");
    assert!(
        t.result["reason"]
            .as_str()
            .unwrap()
            .contains("No fresh data")
    );
    assert_eq!(i.marks, vec![true, false]);
    assert!(i.time <= 1004.0);
    assert!(i.commands.is_empty());
}

#[test]
fn recovery_fault_is_logged_and_never_claims_the_settings_were_restored() {
    let mut c = Controller::new(&configuration("coordinate")).unwrap();
    let mut i = Instrument::new();
    let t = drive(&mut c, &mut i, "run", &[], |i, e| {
        if e["kind"] == "evaluation" && e["data"]["record"]["index"] == 4 {
            i.stop = true;
        }
        if e["kind"] == "status" && e["data"]["text"] == "Restoring the settings of the stage start"
        {
            i.off = true;
        }
    });
    assert_eq!(t.result["status"], "stopped");
    assert!(
        t.result["reason"]
            .as_str()
            .unwrap()
            .contains("restoring the stage start failed")
    );
    assert!(t.logs.iter().any(|l| l["event"] == "restore_failed"));
    assert_ne!(i.values["X"], 0.0);
}

#[test]
fn missing_beam_at_start_is_not_an_optimization_success() {
    let mut c = Controller::new(&configuration("coordinate")).unwrap();
    let mut i = Instrument::new();
    i.no_beam = true;
    let t = drive(&mut c, &mut i, "run", &[], |_, _| {});
    assert_eq!(t.result["status"], "instrument error");
    assert!(
        t.result["reason"]
            .as_str()
            .unwrap()
            .contains("no beam reaches")
    );
    assert_eq!(i.commands.len(), 2);
}

#[test]
fn observations_must_match_the_exact_action_and_clock_without_consuming_the_plan() {
    let mut c = Controller::new(&configuration("coordinate")).unwrap();
    let ctx = Context::test(10.0);
    let response = c.call("run", &[], &json!({}), &ctx).unwrap();
    let id = response["action"]["id"].as_u64().unwrap();
    for bad in [
        json!({"id":id+1,"time":1000.0,"value":null}),
        json!({"id":id,"time":-1.0,"value":null}),
        json!({"id":id,"time":1000.0,"value":null,"error":{"kind":"InstrumentError","message":"wrong"}}),
    ] {
        assert!(c.call("observe", &[bad], &json!({}), &ctx).is_err());
        assert_eq!(
            c.call("state", &[], &json!({}), &ctx).unwrap()["action"]["id"],
            id
        );
    }
    let accepted = c
        .call(
            "observe",
            &[json!({"id":id,"time":1000.0,"value":null})],
            &json!({}),
            &ctx,
        )
        .unwrap();
    assert!(accepted["action"]["id"].as_u64().unwrap() > id);
    assert!(
        c.call(
            "observe",
            &[json!({"id":id,"time":1000.0,"value":null})],
            &json!({}),
            &ctx
        )
        .is_err()
    );
    assert!(c.call("_move", &[], &json!({}), &ctx).is_err());
    assert!(c.set_attribute("commanded", json!({"X":4.0})).is_err());
}

#[test]
fn pending_executed_command_is_acknowledged_before_cancellation_recovery() {
    let mut c = Controller::new(&configuration("coordinate")).unwrap();
    let mut i = Instrument::new();
    let start = i.values.clone();
    let ctx = Context::test(30.0);
    let mut response = c.call("run", &[], &json!({}), &ctx).unwrap();
    let mut cancelled = false;
    for _ in 0..10_000 {
        if response["complete"] == true {
            assert_eq!(response["result"]["status"], "stopped");
            break;
        }
        let observation = i.observe(&response["action"]);
        if !cancelled && response["action"]["op"] == "command" && i.values["X"] != 0.0 {
            let pending = c.call("cancel", &[], &json!({}), &ctx).unwrap();
            assert_eq!(pending["action"]["id"], response["action"]["id"]);
            cancelled = true;
        }
        response = c.call("observe", &[observation], &json!({}), &ctx).unwrap();
    }
    assert!(cancelled);
    assert_eq!(response["complete"], true);
    assert_eq!(i.values["X"], 0.0);
    assert_steps(&i, &start, &BTreeMap::from([("X".into(), 1.0)]));
}

#[test]
fn relative_grouped_knob_preserves_gain_and_limits() {
    let mut config = configuration("coordinate");
    config["stages"][0]["knobs"][0] = json!({"name":"RF","channels":{"X":1.0,"Y":-0.5},"scale_channel":"X","relative":true,"floor":10.0,"window":[-0.15,0.15],"max_step":0.02});
    let mut c = Controller::new(&config).unwrap();
    let mut i = Instrument::new();
    i.values.insert("X".into(), 100.0);
    i.values.insert("Y".into(), -50.0);
    i.limits.insert("X".into(), [70.0, 130.0]);
    i.optimum = 105.0;
    let start = i.values.clone();
    let t = drive(&mut c, &mut i, "run", &[], |_, _| {});
    assert_eq!(t.result["status"], "completed");
    assert_steps(
        &i,
        &start,
        &BTreeMap::from([("X".into(), 2.0), ("Y".into(), 1.0)]),
    );
    assert!(
        i.commands
            .iter()
            .all(|p| (p["Y"] + 0.5 * p["X"]).abs() < 1e-9)
    );
}

fn target_configuration(peak: bool) -> Value {
    let mut config = configuration("coordinate");
    config["target"] = json!({"channels":{"F":1.0},"center":10.0,"width":1.0,"measure":["D"],"max_step":0.5,"points":5,"track":2.0,"spectrum_points":5});
    if peak {
        config["stages"][0]["peak"] = json!(true);
    } else {
        config["stages"] = json!([]);
    }
    config
}

#[test]
fn peak_stages_track_center_and_undo_restores_both_filter_and_knob() {
    let mut c = Controller::new(&target_configuration(true)).unwrap();
    let mut i = Instrument::new();
    i.filter = true;
    let start = i.values.clone();
    let t = drive(&mut c, &mut i, "run", &[], |_, _| {});
    assert_eq!(t.result["status"], "completed", "{}", t.result);
    assert_eq!(t.result["stages"][0]["adopted"], true);
    assert!((t.result["target"]["final_center"].as_f64().unwrap() - 10.2).abs() < 1e-7);
    assert_eq!(
        t.result["spectra"]["before"]["amplitude"]
            .as_array()
            .unwrap()
            .len(),
        5
    );
    let peak = t.events.iter().find(|e| e["kind"] == "evaluation").unwrap();
    assert_eq!(peak["data"]["record"]["peak"]["measure"], json!(["D"]));
    let undo = drive(&mut c, &mut i, "restore_initial", &[], |_, _| {});
    assert_eq!(undo.result["status"], "restored");
    assert_eq!(i.values["X"], start["X"]);
    assert_eq!(i.values["F"], start["F"]);
    assert_steps(
        &i,
        &start,
        &BTreeMap::from([("X".into(), 1.0), ("F".into(), 0.5)]),
    );
}

#[test]
fn standalone_sweep_preflights_all_amplitudes_and_returns_to_start() {
    for invalid in [false, true] {
        let mut c = Controller::new(&target_configuration(false)).unwrap();
        let mut i = Instrument::new();
        i.filter = true;
        let start = i.values.clone();
        let amplitudes = if invalid {
            json!([9.0, 10.0, 31.0])
        } else {
            json!([9.0, 10.0, 11.0])
        };
        let t = drive(
            &mut c,
            &mut i,
            "spectrum",
            &[json!({"amplitudes":amplitudes,"measure":["D"]})],
            |_, _| {},
        );
        assert_eq!(
            t.result["status"],
            if invalid {
                "configuration error"
            } else {
                "completed"
            }
        );
        assert_eq!(i.values["F"], 5.0);
        if invalid {
            assert!(i.commands.is_empty());
        } else {
            assert_eq!(t.result["current"].as_array().unwrap().len(), 3);
        }
        assert_steps(&i, &start, &BTreeMap::from([("F".into(), 0.5)]));
    }
}

#[test]
fn target_range_is_refused_before_any_move_and_native_capacity_is_validated() {
    let mut config = target_configuration(true);
    config["target"]["spectrum_span"] = json!(100.0);
    let mut c = Controller::new(&config).unwrap();
    let mut i = Instrument::new();
    let t = drive(&mut c, &mut i, "run", &[], |_, _| {});
    assert_eq!(t.result["status"], "configuration error");
    assert!(i.commands.is_empty());
    for config in [
        {
            let mut v = configuration("coordinate");
            v["stages"][0]["budget"] = json!(usize::MAX);
            v
        },
        {
            let mut v = configuration("coordinate");
            v["stages"][0]["knobs"][0]["channels"] = json!({"D":1.0});
            v
        },
        {
            let mut v = configuration("coordinate");
            v["options"]["poll_s"] = json!(0.0);
            v
        },
        {
            let mut v = configuration("coordinate");
            v["pressure"]["P"] = json!([6.0, 2.0]);
            v
        },
        {
            let mut v = configuration("coordinate");
            v["options"]["strategy"] = json!("placeholder");
            v
        },
        {
            let mut v = configuration("coordinate");
            v["stages"][0]["knobs"][0]["channels"] = json!({"X":0.0});
            v
        },
    ] {
        assert!(Controller::new(&config).is_err(), "{config}");
    }
}

#[test]
fn selected_stage_order_is_preserved_with_all_configured_currents_in_interlocks() {
    let mut config = configuration("coordinate");
    let mut other = config["stages"][0].clone();
    other["name"] = json!("Other");
    other["downstream"] = json!(["N"]);
    other["knobs"][0]["name"] = json!("Y");
    other["knobs"][0]["channels"] = json!({"Y":1.0});
    config["stages"].as_array_mut().unwrap().push(other);
    config["selected_stages"] = json!(["Other", "Tune"]);
    let mut c = Controller::new(&config).unwrap();
    let mut i = Instrument::new();
    let t = drive(&mut c, &mut i, "run", &[], |_, _| {});
    let names: Vec<_> = t.result["stages"]
        .as_array()
        .unwrap()
        .iter()
        .map(|s| s["stage"].as_str().unwrap())
        .collect();
    assert_eq!(names, vec!["Other", "Tune"]);
    assert!(t.result["checks"]["before"]["currents"].get("N").is_some());
}

#[test]
fn peak_fit_and_peak_picking_match_reference_analytic_cases() {
    let x: Vec<_> = (0..5).map(|i| 498.0 + i as f64).collect();
    let y: Vec<_> = x.iter().map(|v| 5.0 - (v - 500.4).powi(2)).collect();
    let (h, s, c) = fit_peak(&x, &y, &[0.01; 5]).unwrap();
    assert!((h - 5.0).abs() < 1e-8);
    assert!((c - 500.4).abs() < 1e-8);
    assert!(s >= 0.01);
    let (h, _, c) = fit_peak(&x, &[8.0, 9.0, 10.0, 11.0, 12.0], &[0.0; 5]).unwrap();
    assert_eq!(h, 12.0);
    assert_eq!(c, 502.0);
    let x: Vec<_> = (0..81).map(|i| 100.0 + 10.0 * i as f64).collect();
    let y: Vec<_> = x
        .iter()
        .map(|v| {
            3e-11 * (-0.5 * ((v - 300.0) / 20.0).powi(2)).exp()
                + 1.2e-11 * (-0.5 * ((v - 520.0) / 30.0).powi(2)).exp()
        })
        .collect();
    for (near, center, sigma) in [
        (280.0, 300.0, 20.0),
        (560.0, 520.0, 30.0),
        (420.0, 520.0, 30.0),
    ] {
        let (c, w) = pick_peak(&x, &y, near).unwrap();
        assert!((c - center).abs() < 4.0);
        assert!((w / (1.1774 * sigma) - 1.0).abs() < 0.25);
    }
    assert!(pick_peak(&[1.0, 2.0], &[1.0, 2.0], 1.5).is_err());
    assert!(pick_peak(&x, &vec![0.0; x.len()], 300.0).is_err());
}

#[test]
fn shutdown_does_not_claim_off_and_cannot_reopen_a_closed_session() {
    let mut c = Controller::new(&json!({})).unwrap();
    let ctx = Context::test(10.0);
    let empty = c.call("restore_initial", &[], &json!({}), &ctx).unwrap();
    assert_eq!(empty["result"]["status"], "nothing to undo");
    let close = c.call("shutdown", &[], &json!({}), &ctx).unwrap();
    assert!(
        close["reason"]
            .as_str()
            .unwrap()
            .contains("not OFF confirmation")
    );
    assert!(
        c.call(
            "configure",
            &[configuration("coordinate")],
            &json!({}),
            &ctx
        )
        .is_err()
    );
}
