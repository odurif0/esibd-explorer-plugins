#![cfg(feature = "mscan")]
//! Map lines of the MScan engine (v0.5.10): explicit points, the x, extra-channel and frequency
//! axes, and the measurement-driven step.

use esibd_native_worker::{Backend, codec, context::Context, mscan::Controller};
use serde_json::{Value, json};

fn call(
    c: &mut Controller,
    name: &str,
    args: &[Value],
) -> esibd_native_worker::error::Result<Value> {
    c.call(name, args, &json!({}), &Context::test(2.0))
}

fn offset(name: &str, value: f64) -> Value {
    json!({"identity": format!("ampr:{name}"), "name": name, "registered": true, "real": true,
        "enabled": true, "active": true, "initialized": true, "on": true, "busy": false,
        "shutdown": false, "state": "confirmed", "value": value, "monitor": value,
        "min": -200.0, "max": 200.0})
}

fn observation(offsets: Vec<Value>) -> Value {
    let rail = |n: u8| {
        json!({"identity":format!("rail{n}"),"device_identity":"psu1","name":format!("PSU_A_CH{n}"),
        "number":n,"registered":true,"real":true,"enabled":true,"active":true,"initialized":true,
        "on":true,"busy":false,"shutdown":false,"unit":"V","use_monitors":true,"readback_status":"Valid",
        "revision":0,"value":20.0,"monitor":20.0,"min":0.0,"max":350.0,
        "hardware_voltage_limit":350.0,"hardware_current_limit":0.5,"current_limit_readback":0.4,
        "voltage_setpoint_readback":20.0,"current_readback":0.002})
    };
    json!({"sequence":1,"wall":100.0,"mono":0.0,
        "amx":{"identity":"amx1","initialized":true,"on":true,"busy":false,"selected":[0,1],
            "links":[["psu_ch01","PSU_A"]],"rows":[{"state":"Periodic","timing":{"period":200,"high":100}},
                {"state":"Periodic","timing":{"period":200,"high":100,"phase":100}}],
            "frequency_hz": 500e3, "duty": [50.0, 50.0]},
        "rails":[rail(0),rail(1)],
        "detector":{"identity":"detector1","name":"DMMR","device_name":"DMMR","module":0,
            "registered":true,"real":true,"unit":"A","enabled":true,"initialized":true,
            "acquiring":true,"recording":true,"history_id":"history1","interval_ms":2.0,
            "times":[99.996,99.998,100.0],"values":[0.0,0.0,0.0],"signal":[0.0,0.0,0.0]},
        "offsets": offsets})
}

fn settings(axis: &str, points: Vec<f64>) -> Value {
    json!({"start": 0.0, "stop": 1.0, "mode": "Step by step", "settling_s": 0.004,
        "settle_timeout": 0.15, "voltage_tolerance": 0.5, "integration_s": 0.012,
        "offset_factor": 0.2, "offset_fixed": 10.0, "points": points, "axis": axis, "amplitude": 100.0})
}

struct Rig {
    controller: Controller,
    obs: Value,
    status: Value,
    signal: Box<dyn Fn(&Value) -> f64>,
    commands: Vec<Value>,
    hide_rows: usize,
}

impl Rig {
    fn new(settings: &Value, obs: Value, signal: Box<dyn Fn(&Value) -> f64>) -> Self {
        let mut controller = Controller::new(&json!({})).unwrap();
        let status = call(&mut controller, "start", &[settings.clone(), obs.clone()]).unwrap();
        Self {
            controller,
            obs,
            status,
            signal,
            commands: Vec::new(),
            hide_rows: 0,
        }
    }
    fn tick(&mut self, dt: f64) {
        self.obs["sequence"] = json!(self.obs["sequence"].as_u64().unwrap() + 1);
        self.obs["wall"] = json!(self.obs["wall"].as_f64().unwrap() + dt);
        self.obs["mono"] = json!(self.obs["mono"].as_f64().unwrap() + dt);
        if dt > 0.0 {
            let t = self.obs["wall"].clone();
            let s = (self.signal)(&self.obs);
            self.obs["detector"]["times"]
                .as_array_mut()
                .unwrap()
                .push(t);
            self.obs["detector"]["values"]
                .as_array_mut()
                .unwrap()
                .push(json!(0.0));
            self.obs["detector"]["signal"]
                .as_array_mut()
                .unwrap()
                .push(json!(s));
        }
        if self.hide_rows > 0 {
            self.hide_rows -= 1;
            self.obs["amx"]["rows"] = json!([]);
            self.obs["amx"]["duty"] = json!([]);
        } else {
            self.obs["amx"]["rows"] = observation(vec![])["amx"]["rows"].clone();
            self.obs["amx"]["duty"] = json!([50.0, 50.0]);
        }
    }
    fn command(&mut self) -> Value {
        let action = self.status["action"].clone();
        self.commands.push(action.clone());
        let started = self.obs["wall"].clone();
        for (r, target) in self.obs["rails"]
            .as_array_mut()
            .unwrap()
            .iter_mut()
            .zip(action["targets"].as_array().unwrap())
        {
            if r["value"] != *target {
                r["revision"] = json!(r["revision"].as_u64().unwrap() + 1);
            }
            r["value"] = target.clone();
            r["monitor"] = target.clone();
            r["voltage_setpoint_readback"] = target.clone();
        }
        for (o, target) in self.obs["offsets"]
            .as_array_mut()
            .unwrap()
            .iter_mut()
            .zip(action["offsets"].as_array().unwrap())
        {
            o["value"] = target.clone();
            o["monitor"] = target.clone();
        }
        if !action["frequency_hz"].is_null() {
            self.obs["amx"]["frequency_hz"] = action["frequency_hz"].clone();
            self.hide_rows = 2; // The AMX re-decodes its outputs after a frequency write.
        }
        self.tick(0.0001);
        json!({"session":action["session"],"action_id":action["action_id"],
            "start_wall":started,"end_wall":self.obs["wall"],"end_mono":self.obs["mono"],
            "revisions":self.obs["rails"].as_array().unwrap().iter().map(|r| r["revision"].clone()).collect::<Vec<_>>(),
            "values":self.obs["rails"].as_array().unwrap().iter().map(|r| r["value"].clone()).collect::<Vec<_>>(),
            "offset_values":self.obs["offsets"].as_array().unwrap().iter().map(|o| o["value"].clone()).collect::<Vec<_>>(),
            "frequency_hz": action["frequency_hz"]})
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
    fn run(&mut self) -> Value {
        for _ in 0..200_000 {
            if self.status["state"] == "finished" {
                return call(&mut self.controller, "result", &[json!(0), json!(4096)]).unwrap();
            }
            self.step().unwrap();
        }
        panic!("line did not finish: {}", self.status);
    }
}

#[test]
fn measurement_driven_amplitude_line_skips_flat_points_and_resolves_the_peak() {
    let points: Vec<f64> = (0..161).map(|i| 20.0 + 0.25 * i as f64).collect();
    let mut s = settings("amplitude", points.clone());
    s["adaptive"] = json!({"coarse": 4, "sigma": 3.0, "fraction": 0.1, "noise_floor": 1e-15});
    let peak = |obs: &Value| {
        let a = obs["rails"][0]["value"].as_f64().unwrap();
        1e-13 + 1e-12 * (-((a - 40.0) / 0.6).powi(2)).exp()
    };
    let mut rig = Rig::new(
        &s,
        observation(vec![offset("Q2_DC_1", 10.0), offset("Q2_DC_2", 10.0)]),
        Box::new(peak),
    );
    let result = rig.run();
    let status: Vec<String> = result["validation"]["point_status"]
        .as_array()
        .unwrap()
        .iter()
        .map(|v| v.as_str().unwrap().to_owned())
        .collect();
    assert_eq!(result["validation"]["status"], json!("completed"));
    let measured: Vec<usize> = (0..status.len())
        .filter(|i| status[*i] == "acquired")
        .collect();
    assert!(
        status.iter().all(|s| s == "acquired" || s == "skipped"),
        "{status:?}"
    );
    assert!(
        measured.len() < 100,
        "{} of 161 points measured",
        measured.len()
    );
    for (i, a) in points.iter().enumerate().take(85).skip(76) {
        assert!(
            measured.contains(&i),
            "point {i} ({a} V) on the peak was skipped"
        );
    }
    let signal = &result["validation"]["signal"];
    assert!(
        signal[80].as_f64().unwrap() > 1e-12,
        "the signal of the selected module is kept"
    );
    // Every pair target follows A at x = 10 V, and the last command restores the start values.
    let last = rig.commands.last().unwrap();
    assert_eq!(last["restore"], json!(true));
    assert_eq!(last["targets"], json!([20.0, 20.0]));
}

#[test]
fn offset_axis_moves_x_at_a_fixed_amplitude() {
    let s = settings("offset", vec![25.0, 25.5, 26.0]);
    let mut rig = Rig::new(
        &s,
        observation(vec![offset("Q2_DC_1", 10.0), offset("Q2_DC_2", 10.0)]),
        Box::new(|_| 1e-12),
    );
    let result = rig.run();
    assert_eq!(result["validation"]["status"], json!("completed"));
    let moves: Vec<&Value> = rig
        .commands
        .iter()
        .filter(|c| c["restore"] == json!(false))
        .collect();
    assert_eq!(moves.len(), 3);
    for (c, x) in moves.iter().zip([25.0, 25.5, 26.0]) {
        assert_eq!(c["targets"], json!([100.0, 100.0]));
        let pairs: Vec<f64> = c["offsets"]
            .as_array()
            .unwrap()
            .iter()
            .map(|v| v.as_f64().unwrap())
            .collect();
        assert!(
            (pairs[0] - (x + 20.0)).abs() < 1e-9 && (pairs[1] - (x - 20.0)).abs() < 1e-9,
            "{pairs:?}"
        );
    }
    let restore = rig.commands.last().unwrap();
    assert_eq!(restore["offsets"], json!([10.0, 10.0]));
}

#[test]
fn extra_axis_sweeps_the_last_ampr_channel_and_restores_it() {
    let s = settings("extra", vec![18.0, 19.0, 20.0]);
    let obs = observation(vec![
        offset("Q2_DC_1", 10.0),
        offset("Q2_DC_2", 10.0),
        offset("Stub_Entry_Q2", 20.0),
    ]);
    let mut rig = Rig::new(&s, obs, Box::new(|_| 1e-12));
    rig.run();
    let moves: Vec<&Value> = rig
        .commands
        .iter()
        .filter(|c| c["restore"] == json!(false))
        .collect();
    for (c, stub) in moves.iter().zip([18.0, 19.0, 20.0]) {
        assert_eq!(c["offsets"], json!([30.0, -10.0, stub]));
    }
    assert_eq!(
        rig.commands.last().unwrap()["offsets"],
        json!([10.0, 10.0, 20.0])
    );
}

#[test]
fn frequency_axis_waits_for_the_amx_readback_and_restores_the_frequency() {
    let points = vec![400e3, 450e3, 500e3 + 1.0e3];
    let s = settings("frequency", points.clone());
    let mut rig = Rig::new(
        &s,
        observation(vec![offset("Q2_DC_1", 10.0), offset("Q2_DC_2", 10.0)]),
        Box::new(|_| 1e-12),
    );
    let result = rig.run();
    assert_eq!(
        result["validation"]["status"],
        json!("completed"),
        "{}",
        result["validation"]["error"]
    );
    let moves: Vec<&Value> = rig
        .commands
        .iter()
        .filter(|c| c["restore"] == json!(false))
        .collect();
    let sent: Vec<f64> = moves
        .iter()
        .map(|c| c["frequency_hz"].as_f64().unwrap())
        .collect();
    assert_eq!(sent, points);
    for c in &moves {
        assert_eq!(c["targets"], json!([100.0, 100.0]), "A and U stay fixed");
        assert_eq!(c["offsets"], json!([30.0, -10.0]));
    }
    assert_eq!(rig.commands.last().unwrap()["frequency_hz"], json!(500e3));
}

#[test]
fn frequency_axis_stops_when_the_duty_cycle_changes() {
    let s = settings("frequency", vec![400e3, 450e3]);
    let mut rig = Rig::new(&s, observation(vec![]), Box::new(|_| 1e-12));
    rig.step().unwrap();
    rig.hide_rows = 0;
    rig.tick(0.0);
    rig.obs["amx"]["duty"] = json!([45.0, 55.0]);
    // Keep the rows visible with the changed duty for the next observation.
    let duty = rig.obs["amx"]["duty"].clone();
    let mut error = None;
    for _ in 0..50 {
        rig.obs["amx"]["duty"] = duty.clone();
        rig.obs["amx"]["rows"] = observation(vec![])["amx"]["rows"].clone();
        let ack = if rig.status["action"]["kind"] == "command" {
            rig.command()
        } else {
            Value::Null
        };
        rig.obs["amx"]["duty"] = duty.clone();
        rig.obs["amx"]["rows"] = observation(vec![])["amx"]["rows"].clone();
        rig.obs["sequence"] = json!(rig.obs["sequence"].as_u64().unwrap() + 1);
        match call(&mut rig.controller, "advance", &[rig.obs.clone(), ack]) {
            Ok(status) => rig.status = status,
            Err(e) => {
                error = Some(e.message);
                break;
            }
        }
    }
    assert!(error.unwrap_or_default().contains("duty cycle"));
}

#[test]
fn invalid_map_line_settings_are_refused_before_any_write() {
    let obs = observation(vec![]);
    let mut c = Controller::new(&json!({})).unwrap();
    let mut continuous = settings("amplitude", vec![20.0, 30.0]);
    continuous["mode"] = json!("Continuous");
    continuous["time_step_s"] = json!(0.04);
    continuous["sweep_rate"] = json!(100.0);
    assert!(
        call(&mut c, "prepare", &[continuous, obs.clone()])
            .unwrap_err()
            .message
            .contains("Step by step")
    );
    let mut no_amplitude = settings("offset", vec![20.0, 30.0]);
    no_amplitude["amplitude"] = codec::float(f64::NAN);
    assert!(
        call(&mut c, "prepare", &[no_amplitude, obs.clone()])
            .unwrap_err()
            .message
            .contains("fixed amplitude")
    );
    let mut adaptive = settings("amplitude", vec![20.0, 30.0]);
    adaptive["adaptive"] = json!({"coarse": 4, "sigma": 3.0, "fraction": 0.1, "noise_floor": 0.0});
    let mut no_signal = obs.clone();
    no_signal["detector"]["signal"] = json!([]);
    assert!(
        call(&mut c, "prepare", &[adaptive, no_signal])
            .unwrap_err()
            .message
            .contains("selected DMMR module")
    );
    let mut axis_without_points = settings("frequency", vec![]);
    axis_without_points
        .as_object_mut()
        .unwrap()
        .remove("points");
    assert!(
        call(&mut c, "prepare", &[axis_without_points, obs])
            .unwrap_err()
            .message
            .contains("explicit scan points")
    );
}
