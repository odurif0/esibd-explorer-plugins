#![cfg(feature = "mscan")]

use esibd_native_worker::{Backend, context::Context, mscan::Controller};
use serde_json::{Value, json};

fn call(
    c: &mut Controller,
    name: &str,
    args: &[Value],
) -> esibd_native_worker::error::Result<Value> {
    c.call(name, args, &json!({}), &Context::test(600.0))
}

fn instrument() -> Value {
    json!({"q1_dc_v": 36.0, "ap_q1_q2_v": 34.0, "x_v": 10.0, "ilim_a": 0.5, "psu_max_v": 350.0,
        "full_range": true, "pair_min_v": -200.0, "pair_max_v": 200.0, "stub_min_v": -200.0,
        "stub_max_v": 200.0, "q1_max_v": 200.0, "clock_hz": 100e6, "f_hz": 500e3,
        "frequency_writable": true, "free_disk_bytes": 1e12})
}

use esibd_native_worker::mscan_map::{
    APEX, Filter, StepPolicy, Stepper, amplitude_grid, band, frequency_grid, lstsq, on_clock,
    sobol2,
};

fn plan_with(map: Value) -> Value {
    json!({"map": map})
}

#[test]
fn square_wave_band_matches_the_python_reference() {
    // mscan_plugin.square_wave_band on a 5e-5 q grid; the engine bisects the edges.
    for (k, lo, hi) in [
        (0.1, 0.24601925625, 0.62877140625),
        (0.2, 0.5096363, 0.5619297625),
        (0.21, 0.5377327875, 0.55598050625),
        (0.215, 0.5519310125, 0.553030875),
    ] {
        let (a, b) = band(k).unwrap();
        assert!(
            (a - lo).abs() < 6e-5 && (b - hi).abs() < 6e-5,
            "k={k}: {a} {b}"
        );
    }
    assert_eq!(band(0.0), Some((0.0, 0.712)));
    assert!(band(APEX + 0.001).is_none());
}

#[test]
fn mz_and_amplitude_are_inverse_and_match_python() {
    let f = Filter {
        k: 0.2,
        delta: 0.0,
        umin: 0.5,
        c: 0.978,
        r0_mm: 2.625,
    };
    let m = f.mz(206.7, 500e3).unwrap();
    assert!((m / 2121.58216261 - 1.0).abs() < 2e-4, "{m}");
    let a = f.amplitude_for(2121.93, 500e3).unwrap();
    assert!((f.mz(a, 500e3).unwrap() / 2121.93 - 1.0).abs() < 1e-6);
    let g = Filter {
        k: 0.21,
        delta: 1.0,
        umin: 0.5,
        c: 0.978,
        r0_mm: 2.625,
    };
    assert!((g.mz(100.0, 400e3).unwrap() / 1603.76010115 - 1.0).abs() < 2e-4);
    // Above the plan's apex the ladders still know where each ion would be.
    let beyond = Filter { k: 0.2120, ..f };
    assert!(beyond.amplitude_for(922.01, 500e3).is_some());
    let fr = f.frequency_for(2121.93, 206.7).unwrap();
    assert!((fr / 500e3 - 1.0).abs() < 1e-3, "{fr}");
}

#[test]
fn grids_sobol_and_least_squares() {
    let grid = amplitude_grid(20.0, 348.0, 100.0, 4);
    assert_eq!(grid[0], 20.0);
    assert_eq!(*grid.last().unwrap(), 348.0);
    assert!((grid[1] / grid[0] - 1.00125).abs() < 1e-5);
    assert!(grid.windows(2).all(|w| w[1] > w[0]));
    let freq = frequency_grid(330e3, 1e6, 100e6);
    assert!(freq.windows(2).all(|w| w[1] > w[0]));
    // Even periods only: an odd one has no 50 % duty cycle.
    assert!(
        freq.iter()
            .all(|f| (100e6 / f / 2.0 - (100e6 / f / 2.0).round()).abs() < 1e-6)
    );
    assert_eq!((freq[0], *freq.last().unwrap()), (100e6 / 302.0, 1e6));
    assert_eq!(freq.len(), 102);
    // The nearest even period, kept inside [lo, hi] when given.
    assert_eq!(on_clock(300e3, 100e6, 0.0, 0.0), 100e6 / 334.0);
    assert_eq!(on_clock(100e6 / 333.0, 100e6, 0.0, 0.0), 100e6 / 334.0); // 2026-11-13: 166 of 333 ticks at Vpos
    assert_eq!(on_clock(500e3, 100e6, 0.0, 0.0), 500e3);
    assert_eq!(on_clock(550e3, 100e6, 0.0, 0.0), 100e6 / 182.0);
    assert_eq!(on_clock(640e3, 100e6, 250e3, 640e3), 100e6 / 158.0); // 156 would be 641.0 kHz
    assert_eq!(on_clock(250e3, 100e6, 250e3, 640e3), 250e3);
    assert_eq!(on_clock(251e3, 100e6, 250e3, 640e3), 100e6 / 398.0);
    assert!(on_clock(f64::NAN, 100e6, 0.0, 0.0).is_nan());
    assert_eq!(sobol2(0), (0.0, 0.0));
    assert_eq!(sobol2(1), (0.5, 0.5));
    // Natural order (not Gray code): the first four points are the same set either way.
    assert_eq!(sobol2(2), (0.25, 0.75));
    assert_eq!(sobol2(3), (0.75, 0.25));
    let x = lstsq(
        &[vec![1.0, 0.0], vec![1.0, 1.0], vec![1.0, 2.0]],
        &[1.0, 3.0, 5.0],
    )
    .unwrap();
    assert!((x[0] - 1.0).abs() < 1e-9 && (x[1] - 2.0).abs() < 1e-9);
}

fn run_stepper(values: &dyn Fn(usize) -> f64, n: usize) -> Vec<usize> {
    let mut s = Stepper::new(
        StepPolicy {
            coarse: 4,
            sigma: 3.0,
            fraction: 0.1,
            noise_floor: 0.01,
        },
        n,
    );
    let mut order = Vec::new();
    while let Some(i) = s.next_index() {
        assert!(
            i < n && !s.measured(i),
            "index {i} out of range or measured twice"
        );
        order.push(i);
        s.record(i, values(i), 0.01);
        assert!(order.len() <= n, "the step policy must terminate");
    }
    order
}

#[test]
fn measurement_driven_step_is_coarse_on_flats_and_fine_on_peaks() {
    let flat = run_stepper(&|_| 1.0, 401);
    assert!(flat.len() <= 105, "{} points on a flat line", flat.len());
    assert_eq!(
        *flat.last().unwrap(),
        400,
        "the last grid point is always measured"
    );
    let peak = |i: usize| 1.0 + 10.0 * (-((i as f64 - 200.0) / 4.0).powi(2)).exp();
    let order = run_stepper(&peak, 401);
    // Every fine point across the peak (±1.5 widths) is measured, not just the coarse grid.
    for i in 195..=205 {
        assert!(order.contains(&i), "fine point {i} skipped on the peak");
    }
    assert!(order.len() < 200, "{} points", order.len());
    // Monotonic apart from the step back after a detected change.
    let backs = order.windows(2).filter(|w| w[1] < w[0]).count();
    assert!(backs <= 12, "{backs} steps back");
}

#[test]
fn check_lists_what_to_change_before_start() {
    let mut c = Controller::new(&json!({})).unwrap();
    let ok = call(&mut c, "map_check", &[json!({}), instrument()]).unwrap();
    assert_eq!(ok["problems"], json!([]), "{}", ok["problems"]);
    assert!(ok["estimate"]["total_s"].as_f64().unwrap() > 3600.0);
    let mut weak = instrument();
    weak["ilim_a"] = json!(0.2);
    weak["full_range"] = json!(false);
    let bad = call(&mut c, "map_check", &[json!({}), weak]).unwrap();
    let text = bad["problems"].to_string();
    assert!(text.contains("set Ilim to at least"), "{text}");
    assert!(text.contains("Full range"), "{text}");
    assert!(bad["estimate"].is_null());
    let err = call(
        &mut c,
        "map_begin",
        &[json!({}), bad_instrument(), json!(1.8e9)],
    )
    .unwrap_err();
    assert!(err.message.contains("cannot start"));
    for (range, ok) in [
        (json!([]), true),
        (json!([30, 60]), true),
        (json!([60, 30]), false),
        (json!([30]), false),
    ] {
        let checked = call(
            &mut c,
            "map_check",
            &[plan_with(json!({"spray_range_na": range})), instrument()],
        )
        .unwrap();
        let text = checked["problems"].to_string();
        assert_eq!(!text.contains("spray_range_na"), ok, "{range}: {text}");
    }
    let unknown = call(
        &mut c,
        "map_check",
        &[plan_with(json!({"r_targett": 40})), instrument()],
    );
    assert!(unknown.unwrap_err().message.contains("unknown field"));
}

fn bad_instrument() -> Value {
    let mut i = instrument();
    i["ilim_a"] = json!(0.05);
    i
}

#[test]
fn journal_drives_the_map_and_rejects_stale_results() {
    let mut c = Controller::new(&json!({})).unwrap();
    let journal = call(
        &mut c,
        "map_begin",
        &[json!({}), instrument(), json!(1.8e9)],
    )
    .unwrap();
    let next = call(&mut c, "map_next", std::slice::from_ref(&journal)).unwrap();
    assert_eq!(next["decision"], json!("line"));
    let request = &next["request"];
    assert_eq!(request["id"], json!(1));
    assert_eq!(
        request["role"],
        json!("reference"),
        "the first line is a reference"
    );
    assert_eq!(
        next,
        call(&mut c, "map_next", std::slice::from_ref(&journal)).unwrap(),
        "pure: same journal, same line"
    );
    let stale = json!({"id": 2, "outcome": "done", "axis": request["axis"], "signal": []});
    assert!(call(&mut c, "map_record", &[journal.clone(), stale]).is_err());
    // A line that keeps failing stops the map after max_failures attempts.
    let mut j = journal;
    for attempt in 1..=3 {
        let id = j["records"].as_array().unwrap().len() + 1;
        let failed =
            json!({"id": id, "outcome": "failed", "error": "spray current left its window"});
        j = call(&mut c, "map_record", &[j, failed]).unwrap()["journal"].clone();
        let next = call(&mut c, "map_next", &[j.clone()]).unwrap();
        let expected = if attempt < 3 { "line" } else { "stop" };
        assert_eq!(next["decision"], json!(expected), "{next}");
    }
    // 2026-11-13 lab: two Resumes stopped at once on the line that had failed three times. A Resume
    // starts a new session: that line gets three new attempts.
    let label = j["records"].as_array().unwrap().last().unwrap()["request"]["label"].clone();
    j["session_start"] = json!(j["records"].as_array().unwrap().len());
    let resumed = call(&mut c, "map_next", &[j.clone()]).unwrap();
    assert_eq!(resumed["decision"], json!("line"), "{resumed}");
    assert_eq!(resumed["request"]["label"], label);
    for attempt in 1..=3 {
        let id = j["records"].as_array().unwrap().len() + 1;
        let failed = json!({"id": id, "outcome": "failed", "error": "AMX frequency not reached"});
        j = call(&mut c, "map_record", &[j, failed]).unwrap()["journal"].clone();
        let next = call(&mut c, "map_next", &[j.clone()]).unwrap();
        let expected = if attempt < 3 { "line" } else { "stop" };
        assert_eq!(next["decision"], json!(expected), "{next}");
    }
    assert_eq!(
        j["session_start"],
        json!(3),
        "map_record keeps the session start"
    );
}

#[test]
fn every_line_asks_for_a_frequency_the_amx_makes() {
    // 2026-11-13 lab: 300 kHz is 333.3 periods of 10 ns. The AMX made 300.300 kHz, the map waited
    // for 300.000 kHz and stopped after three attempts.
    // 2026-11-14: at 333 ticks the duty cycle is 166/333, a DC of A/333 between the rod pairs. The
    // map asks for even periods: 334 ticks, 299.401 kHz.
    let mut c = Controller::new(&json!({})).unwrap();
    let plan = plan_with(
        json!({"design": {"amplitude": [[300, 9.4]], "frequency": []},
        "reference": {"f_khz": 333}}),
    );
    let checked = call(&mut c, "map_check", &[plan.clone(), instrument()]).unwrap();
    let warnings = checked["warnings"].to_string();
    assert!(
        warnings.contains("P1: the AMX makes 299.401 kHz, not 300 kHz"),
        "{warnings}"
    );
    assert_eq!(
        checked["summary"]["points"][0]["f_khz"],
        json!(100e6 / 334.0 / 1e3)
    );
    // A plan that names the frequency the AMX makes is not told so.
    let named = plan_with(json!({"design": {"amplitude": [[299.401, 9.3]], "frequency": []}}));
    let checked = call(&mut c, "map_check", &[named, instrument()]).unwrap();
    assert!(
        !checked["warnings"].to_string().contains("the AMX makes"),
        "{}",
        checked["warnings"]
    );
    assert_eq!(
        checked["summary"]["points"][0]["f_khz"],
        json!(100e6 / 334.0 / 1e3)
    );
    let journal = simulated_journal(&mut c, plan);
    let records = journal["records"].as_array().unwrap();
    assert!(records.len() > 5, "{}", records.len());
    for r in records {
        let f = r["request"]["f_hz"].as_f64().unwrap();
        let half = 100e6 / f / 2.0;
        assert!(
            (half - half.round()).abs() < 1e-9,
            "{}: {f} Hz",
            r["request"]["label"]
        );
    }
    let f_of = |role: &str| {
        records
            .iter()
            .find(|r| r["request"]["role"] == json!(role))
            .map(|r| r["request"]["f_hz"].as_f64().unwrap())
    };
    assert_eq!(f_of("ladder"), Some(100e6 / 334.0));
    assert_eq!(f_of("reference"), Some(100e6 / 300.0));
}

#[test]
fn simulated_map_converges_on_a_best_scan_with_faults() {
    let mut c = Controller::new(&json!({})).unwrap();
    let out = call(
        &mut c,
        "map_simulate",
        &[
            json!({}),
            instrument(),
            json!({"hypothesis": "H4", "fail_every": 9, "journal": true}),
        ],
    )
    .unwrap();
    assert_eq!(out["outcome"], json!("done"), "{}", out["reason"]);
    let lines = out["lines"].as_array().unwrap();
    assert!(
        lines.iter().any(|l| l["outcome"] == json!("failed")),
        "faults were injected"
    );
    let roles: Vec<&str> = lines.iter().map(|l| l["role"].as_str().unwrap()).collect();
    for role in [
        "reference",
        "stopping",
        "ladder",
        "repeat",
        "verify",
        "stub",
        "validation",
        "local",
        "best",
    ] {
        assert!(roles.contains(&role), "no {role} line");
    }
    let report = &out["report"];
    let optimum = &report["optimum"];
    let domain = report["domain_hz"].as_array().unwrap();
    let f = optimum["f_hz"].as_f64().unwrap();
    assert!(
        f >= domain[0].as_f64().unwrap() - 1.0 && f <= domain[1].as_f64().unwrap() + 1.0,
        "optimum {optimum} outside {domain:?}"
    );
    assert!((report["stopping"]["x_stop"].as_f64().unwrap() - 35.7).abs() < 0.5);
    assert!(report["stopping"]["thermalized"].as_bool().unwrap());
    let c_fit = report["laws"]["c"].as_f64().unwrap();
    assert!((c_fit - 0.978).abs() < 0.01, "c = {c_fit}");
    assert_eq!(report["next"]["decision"], json!("done"));
    // Resume: the journal cut anywhere continues with a line, never an error.
    let journal = &out["journal"];
    for cut in [1usize, 10, 60, 150] {
        let mut part = journal.clone();
        part["records"].as_array_mut().unwrap().truncate(cut);
        let next = call(&mut c, "map_next", &[part]).unwrap();
        assert!(
            matches!(next["decision"].as_str(), Some("line" | "wait")),
            "cut {cut}: {next}"
        );
    }
}

#[test]
#[ignore]
fn explore_simulation() {
    let mut c = Controller::new(&json!({})).unwrap();
    for h in ["H1", "H2", "H4"] {
        let out = call(
            &mut c,
            "map_simulate",
            &[json!({}), instrument(), json!({"hypothesis": h})],
        )
        .unwrap();
        println!(
            "== {h}: {} {} lines={} total_h={:.1}",
            out["outcome"],
            out["reason"],
            out["lines"].as_array().unwrap().len(),
            out["total_s"].as_f64().unwrap() / 3600.0
        );
        let mut counts = std::collections::BTreeMap::new();
        for l in out["lines"].as_array().unwrap() {
            *counts
                .entry(format!("{}:{}", l["stage"], l["role"]))
                .or_insert(0) += 1;
        }
        println!("{counts:?}");
        let r = &out["report"];
        println!("stopping {}", r["stopping"]);
        println!(
            "laws c={} cap={} signal_valid={} valid={}",
            r["laws"]["c"],
            json!({"a": r["laws"]["cap"]["a"], "b": r["laws"]["cap"]["b"], "none": r["laws"]["cap"]["none"], "res": r["laws"]["cap"]["residual"], "valid": r["laws"]["cap"]["valid"]}),
            r["laws"]["signal"]["valid"],
            r["laws"]["valid"]
        );
        for p in r["points"].as_array().unwrap() {
            println!(
                "  {} {} f={:.0} E={} c={:.4} feas={} merit={:.3e} minR={:.1} sol=({}, {}) cover={} closed={} empty={}",
                p["id"],
                p["mode"],
                p["f_hz"].as_f64().unwrap() / 1e3,
                p["energy_ev"],
                p["c"].as_f64().unwrap(),
                p["feasible"],
                p["merit"].as_f64().unwrap(),
                p["min_r"].as_f64().unwrap(),
                p["solution"]["k"],
                p["solution"]["delta"],
                p["covers_best_range"],
                p["closed"],
                p["empty"]
            );
        }
        println!("optimum {}", r["optimum"]);
        println!("validation passed {} ", r["validation_passed"]);
        println!(
            "local passed={} target={} {}",
            r["local_passed"],
            r["target"],
            r["local"]
                .as_array()
                .unwrap()
                .iter()
                .map(|l| format!(
                    "{}:{}:{:.1}",
                    l["name"].as_str().unwrap(),
                    l["verdict"].as_str().unwrap(),
                    l["min_r"].as_f64().unwrap()
                ))
                .collect::<Vec<_>>()
                .join(" ")
        );
        println!("search {}", r["search"]);
    }
}

#[test]
#[ignore]
fn explore_ladder() {
    let h = std::env::var("HYP").unwrap_or("H4".into());
    let point = std::env::var("POINT").unwrap_or("P1".into());
    let mut c = Controller::new(&json!({})).unwrap();
    let out = call(
        &mut c,
        "map_simulate",
        &[
            json!({}),
            instrument(),
            json!({"hypothesis": h, "journal": true}),
        ],
    )
    .unwrap();
    for r in out["journal"]["records"].as_array().unwrap() {
        let q = &r["request"];
        if q["point"] != json!(point) {
            continue;
        }
        print!(
            "{} {} {} k={} d={} out={} :",
            q["id"], q["role"], q["variant"], q["k"], q["delta_v"], r["outcome"]
        );
        for p in r["analysis"]["peaks"].as_array().unwrap() {
            print!(
                " [{:.0} {} R={:.1} h={:.2e} n={:.1e} {:?}]",
                p["mz"].as_f64().unwrap(),
                if p["found"].as_bool().unwrap() {
                    "F"
                } else {
                    "-"
                },
                p["r"].as_f64().unwrap(),
                p["height"].as_f64().unwrap(),
                p["noise"].as_f64().unwrap(),
                p["flags"]
            );
        }
        if let Some(t) = r["analysis"]["profile"].as_object() {
            print!(" profile {:?}", t);
        }
        println!();
    }
    let r = &out["report"];
    for p in r["points"].as_array().unwrap() {
        if p["id"] != json!(point) {
            continue;
        }
        println!(
            "c={} closing={} sol={}",
            p["c"], p["closing_k"], p["solution"]
        );
        for i in p["ions"].as_array().unwrap() {
            println!(
                "  ion {} pos={:.1} N={:.1} cap={:.1} table={}",
                i["mz"],
                i["position"].as_f64().unwrap(),
                i["cycles"].as_f64().unwrap(),
                i["r_cap"].as_f64().unwrap(),
                i["table"]
            );
        }
    }
}

#[test]
#[ignore]
fn explore_validation() {
    let h = std::env::var("HYP").unwrap_or("H2".into());
    let mut c = Controller::new(&json!({})).unwrap();
    let out = call(
        &mut c,
        "map_simulate",
        &[json!({}), instrument(), json!({"hypothesis": h})],
    )
    .unwrap();
    let r = &out["report"];
    println!("laws {}", r["laws"]);
    println!("domain {}", r["domain_hz"]);
    for v in r["validation"].as_array().unwrap() {
        println!("V {v}");
    }
    println!("next {}", r["next"]);
}

fn simulated_journal(c: &mut Controller, plan: Value) -> Value {
    call(
        c,
        "map_simulate",
        &[
            plan,
            instrument(),
            json!({"hypothesis": "H1", "journal": true, "max_lines": 40}),
        ],
    )
    .unwrap()["journal"]
        .clone()
}

#[test]
fn amplitude_lines_span_the_best_range_ions_with_their_margin() {
    let mut c = Controller::new(&json!({})).unwrap();
    let journal = simulated_journal(&mut c, json!({}));
    let ladder = journal["records"]
        .as_array()
        .unwrap()
        .iter()
        .map(|r| &r["request"])
        .find(|q| q["role"] == json!("ladder") && q["point"] == json!("P1"))
        .expect("a P1 ladder line");
    let axis: Vec<f64> = ladder["axis"]
        .as_array()
        .unwrap()
        .iter()
        .map(|v| v.as_f64().unwrap())
        .collect();
    // P1 (500 kHz, k = 0.200): 622 at 61 V and 2122 at 207 V, ± 20 % (2026-11-04: 60.4 and 207 V).
    let (lo, hi) = (axis[0], *axis.last().unwrap());
    assert!(
        (lo - 61.0 / 1.2).abs() < 2.0 && (hi - 207.0 * 1.2).abs() < 6.0,
        "{lo}–{hi} V"
    );
    let ions: Vec<f64> = ladder["ions"]
        .as_array()
        .unwrap()
        .iter()
        .map(|v| v.as_f64().unwrap().round())
        .collect();
    assert_eq!(ions, vec![622.0, 922.0, 1522.0, 2122.0]);
    // No best-range ion inside a_range_v: the whole range, as before.
    let wide = simulated_journal(
        &mut c,
        json!({"map": {"best_scan_range": [5000, 6000], "mix": [622.03, 922.01, 5500.0, 5900.0],
            "stages": {"stopping": false, "search": false, "validation": false, "local": false,
                "best": false}}}),
    );
    let first = wide["records"]
        .as_array()
        .unwrap()
        .iter()
        .map(|r| &r["request"])
        .find(|q| q["role"] == json!("ladder"))
        .expect("a ladder line");
    let axis = first["axis"].as_array().unwrap();
    assert_eq!(axis[0].as_f64().unwrap(), 20.0);
    assert!(axis.last().unwrap().as_f64().unwrap() > 340.0);
}

#[test]
fn line_estimates_follow_the_time_the_map_measures() {
    let mut c = Controller::new(&json!({})).unwrap();
    let journal = simulated_journal(&mut c, json!({}));
    let records = journal["records"].as_array().unwrap();
    // The simulator times a point like the plan's prior (0.8 + 0.3 + 2.5 s): estimates match it.
    for r in records.iter().filter(|r| r["outcome"] == json!("done")) {
        let (expected, took) = (
            r["request"]["expected_s"].as_f64().unwrap(),
            r["duration_s"].as_f64().unwrap(),
        );
        if r["request"]["kind"] == json!("offset") {
            assert!((expected - took).abs() < 1.0, "{expected} vs {took}");
        }
    }
    // Lines that took 1.5 × longer per point: the next estimate follows them.
    let mut slow = journal.clone();
    slow["records"].as_array_mut().unwrap().truncate(12);
    let mut points = 0.0;
    let mut seconds = 0.0;
    for r in slow["records"].as_array_mut().unwrap() {
        let measured = r["analysis"]["measured"].as_f64().unwrap_or(0.0);
        let took = r["duration_s"].as_f64().unwrap();
        if r["outcome"] == json!("done") && measured >= 10.0 && took > 15.0 {
            let longer = 15.0 + 1.5 * (took - 15.0);
            r["duration_s"] = json!(longer);
            points += measured;
            seconds += longer - 15.0;
        }
    }
    assert!(points >= 100.0);
    let next = call(&mut c, "map_next", &[slow.clone()]).unwrap();
    let request = &next["request"];
    let per_point = seconds / points;
    assert!((per_point - 5.4).abs() < 0.3, "{per_point}");
    if request["kind"] == json!("offset") {
        let n = request["axis"].as_array().unwrap().len() as f64;
        let expected = request["expected_s"].as_f64().unwrap();
        assert!(
            (expected - (15.0 + n * per_point)).abs() < 1.0,
            "{expected}"
        );
    } else {
        let fast = call(
            &mut c,
            "map_next",
            &[{
                let mut j = journal.clone();
                j["records"].as_array_mut().unwrap().truncate(12);
                j
            }],
        )
        .unwrap();
        let ratio = (request["expected_s"].as_f64().unwrap() - 15.0)
            / (fast["request"]["expected_s"].as_f64().unwrap() - 15.0);
        assert!((ratio - 1.5).abs() < 0.05, "{ratio}");
    }
}

/// m/z 922 on the 2026-11-12 map (500 kHz, E 26 eV), A (V) and M03 (pA).
#[rustfmt::skip]
const BAND_K_0_20806: &[(f64, f64)] = &[
    (84.335, 7.647), (84.758, 7.575), (85.182, 7.609), (85.609, 7.637), (86.038, 7.666),
    (86.469, 7.611), (86.902, 7.64), (87.337, 7.697), (87.775, 7.825), (87.994, 7.909),
    (88.104, 8.005), (88.215, 8.137), (88.435, 8.251), (88.546, 8.476), (88.656, 8.598),
    (88.767, 8.968), (88.878, 9.175), (88.989, 9.44), (89.101, 10.247), (89.212, 11.724),
    (89.323, 12.995), (89.435, 14.832), (89.547, 21.585), (89.659, 34.546), (89.771, 41.508),
    (89.883, 48.034), (89.996, 54.795), (90.108, 54.557), (90.333, 52.436), (90.786, 49.624),
    (91.013, 54.017), (91.241, 55.959), (91.469, 52.155), (91.583, 48.377), (91.698, 44.036),
    (92.157, 48.154), (92.388, 50.826), (92.503, 44.818), (92.619, 37.283), (92.735, 31.387),
    (92.85, 27.573), (93.083, 27.342), (93.199, 33.509), (93.316, 40.275), (93.432, 34.029),
    (93.549, 22.921), (93.666, 16.139), (93.783, 13.667), (94.018, 11.966), (94.489, 9.953),
    (94.962, 8.382), (95.438, 8.131), (95.916, 8.096), (96.396, 8.097), (96.879, 7.978),
    (97.364, 8.053), (97.852, 7.861),
];

#[rustfmt::skip]
const BAND_K_0_20936: &[(f64, f64)] = &[
    (85.308, 7.552), (85.735, 7.566), (86.165, 7.559), (86.596, 7.587), (87.03, 7.557),
    (87.466, 7.587), (87.904, 7.662), (88.345, 7.773), (88.787, 7.971), (89.009, 8.135),
    (89.121, 8.241), (89.232, 8.459), (89.343, 8.567), (89.455, 8.775), (89.567, 9.283),
    (89.679, 9.637), (89.791, 10.965), (89.903, 11.847), (90.016, 14.699), (90.128, 16.445),
    (90.241, 24.206), (90.354, 29.972), (90.467, 41.592), (90.58, 44.798), (90.806, 46.361),
    (91.261, 46.725), (91.375, 44.481), (91.489, 40.857), (91.604, 37.814), (91.718, 36.289),
    (91.948, 39.739), (92.063, 43.875), (92.178, 46.528), (92.293, 46.996), (92.408, 41.613),
    (92.524, 32.649), (92.639, 27.735), (92.755, 24.757), (92.987, 25.906), (93.104, 32.297),
    (93.22, 37.515), (93.336, 31.481), (93.453, 19.963), (93.57, 14.548), (93.687, 12.766),
    (93.921, 11.704), (94.392, 9.214), (94.865, 8.012), (95.34, 8.0), (95.817, 8.014),
    (96.297, 7.991), (96.78, 7.858), (97.264, 7.997), (97.752, 7.767),
];

fn band_peak(points: &[(f64, f64)], k: f64) -> Value {
    use esibd_native_worker::mscan_map::{LineResult, MapConfig, Request, analyse};
    let req: Request = serde_json::from_value(json!({"kind": "amplitude", "f_hz": 500e3, "k": k,
        "delta_v": 0.0, "ions": [922.01]}))
    .unwrap();
    let result: LineResult = serde_json::from_value(json!({
        "axis": points.iter().map(|p| p.0).collect::<Vec<_>>(),
        "signal": points.iter().map(|p| p.1 * 1e-12).collect::<Vec<_>>()}))
    .unwrap();
    let analysis = analyse(&MapConfig::default(), &req, &result);
    serde_json::to_value(&analysis.peaks[0]).unwrap()
}

#[test]
fn the_width_of_a_rippled_band_is_read_at_its_outer_half_height_edges() {
    // The nearest crossing stopped at a ripple below half height (R 29), a Gaussian fitted to the
    // flat top widened it (R 20); the band at half height runs from 89.6 to 93.5 V.
    let p = band_peak(BAND_K_0_20806, 0.20806);
    assert!(p["found"].as_bool().unwrap(), "{p}");
    let r = p["r"].as_f64().unwrap();
    assert!((22.0..25.5).contains(&r), "R {r}: {p}");
    let fwhm = p["fwhm"].as_f64().unwrap();
    assert!((3.7..4.1).contains(&fwhm), "FWHM {fwhm}: {p}");
    assert!(p["flags"].as_array().unwrap().is_empty(), "{p}");
}

#[test]
fn a_band_with_two_maxima_is_flagged_split() {
    let p = band_peak(BAND_K_0_20936, 0.20936);
    assert!(p["found"].as_bool().unwrap(), "{p}");
    assert!(
        p["flags"].as_array().unwrap().contains(&json!("split")),
        "{p}"
    );
    let r = p["r"].as_f64().unwrap();
    assert!((26.0..33.0).contains(&r), "R {r}: {p}");
}

fn beam_plan(extra: Value) -> Value {
    let mut map = json!({
        "reference_every": 3,
        "design": {"amplitude": [[500, 12], [500, 5]], "frequency": []},
        "ladder": {"repeats": 0, "max_lines": 4},
        "stopping": {"repeats": 1},
        "stages": {"search": false, "validation": false, "local": false, "best": false},
    });
    if let (Some(m), Some(e)) = (map.as_object_mut(), extra.as_object()) {
        for (k, v) in e {
            m.insert(k.clone(), v.clone());
        }
    }
    plan_with(map)
}

fn simulate_with(c: &mut Controller, plan: Value, options: Value) -> Value {
    let mut opts = json!({"hypothesis": "H1", "journal": true, "max_lines": 200, "drift": 0.0});
    for (k, v) in options.as_object().unwrap() {
        opts[k] = v.clone();
    }
    call(c, "map_simulate", &[plan, instrument(), opts]).unwrap()
}

#[test]
fn lines_measured_while_the_beam_is_lost_are_measured_again() {
    // 2026-11-15 lab: the beam was lost from 18:55 to 21:44 while the spray current stayed in
    // range; the 922 reference fell from 54 to 3.5 pA and 12 lines measured nothing.
    let mut c = Controller::new(&json!({})).unwrap();
    let clean = simulate_with(&mut c, beam_plan(json!({})), json!({}));
    let start = 1.8e9;
    let (off_from, off_to) = (6000.0, 13000.0);
    let out = simulate_with(
        &mut c,
        beam_plan(json!({})),
        json!({"beam_off": [[off_from, off_to]]}),
    );
    assert_eq!(out["outcome"], json!("done"), "{}", out["reason"]);
    let records = out["journal"]["records"].as_array().unwrap();
    let waits: Vec<&Value> = records
        .iter()
        .filter(|r| r["request"]["role"] == json!("wait"))
        .collect();
    assert!(!waits.is_empty());
    let text = waits[0]["request"]["variant"].as_str().unwrap();
    assert!(
        text.starts_with("beam lost: the 922 reference fell to"),
        "{text}"
    );
    // Every ladder rung has a line measured with the beam, and the map measures the same rungs as
    // without the loss.
    let rungs = |records: &[Value], valid_only: bool| {
        let mut set: Vec<(String, String)> = records
            .iter()
            .filter(|r| r["request"]["role"] == json!("ladder") && r["outcome"] == json!("done"))
            .filter(|r| {
                let t = r["started"].as_f64().unwrap() - start;
                let end = t + r["duration_s"].as_f64().unwrap();
                !valid_only || end <= off_from || t >= off_to
            })
            .map(|r| {
                (
                    r["request"]["point"].as_str().unwrap().to_owned(),
                    format!("{:.5}", r["request"]["k"].as_f64().unwrap()),
                )
            })
            .collect();
        set.sort();
        set.dedup();
        set
    };
    let clean_rungs = rungs(clean["journal"]["records"].as_array().unwrap(), false);
    assert_eq!(rungs(records, true), clean_rungs);
    let lost = records
        .iter()
        .filter(|r| {
            let t = r["started"].as_f64().unwrap() - start;
            r["request"]["role"] == json!("ladder") && t > off_from && t < off_to
        })
        .count();
    assert_eq!(
        lost, 0,
        "no ladder line is started while the beam is known to be lost"
    );
    assert!(records.len() > clean["journal"]["records"].as_array().unwrap().len());
}

#[test]
fn a_beam_that_does_not_come_back_stops_the_map() {
    let mut c = Controller::new(&json!({})).unwrap();
    let out = simulate_with(
        &mut c,
        beam_plan(json!({"reference": {"beam_max_wait_s": 3600}})),
        json!({"beam_off": [[6000, 1e9]]}),
    );
    assert_eq!(out["outcome"], json!("stop"));
    let reason = out["reason"].as_str().unwrap();
    assert!(reason.starts_with("beam lost for 6"), "{reason}");
    assert!(reason.contains("Map… → Resume"), "{reason}");
}

#[test]
fn a_resume_checks_the_beam_against_the_reference_before_it() {
    // A Resume without beam must not make a low 922 peak the new normal (2026-11-15: 54 → 3.5 pA,
    // still a found peak).
    let mut c = Controller::new(&json!({})).unwrap();
    let out = simulate_with(
        &mut c,
        beam_plan(json!({"reference": {"beam_max_wait_s": 1800}})),
        json!({"beam_off": [[6000, 1e9]]}),
    );
    assert_eq!(out["outcome"], json!("stop"));
    let mut j = out["journal"].clone();
    let records = j["records"].as_array().unwrap().clone();
    let references: Vec<&Value> = records
        .iter()
        .filter(|r| r["request"]["role"] == json!("reference") && r["outcome"] == json!("done"))
        .collect();
    let height = |r: &Value| {
        r["analysis"]["peaks"]
            .as_array()
            .unwrap()
            .iter()
            .find(|p| (p["mz"].as_f64().unwrap() - 922.01).abs() < 0.5)
            .map_or(0.0, |p| p["height"].as_f64().unwrap())
    };
    let good = references.first().unwrap();
    let low = references.last().unwrap();
    assert!(height(low) > 0.0 && height(low) < 0.5 * height(good));
    let resume = |j: &mut Value, reference: &Value| {
        let records = j["records"].as_array_mut().unwrap();
        let mut r = reference.clone();
        let last = records.last().unwrap();
        r["request"]["id"] = json!(records.len() + 1);
        r["started"] = json!(last["started"].as_f64().unwrap() + 7200.0);
        records.push(r);
    };
    j["session_start"] = json!(records.len());
    let first = call(&mut c, "map_next", &[j.clone()]).unwrap();
    assert_eq!(
        first["request"]["role"],
        json!("reference"),
        "a Resume starts with a reference: {first}"
    );
    let mut without_beam = j.clone();
    resume(&mut without_beam, low);
    let next = call(&mut c, "map_next", &[without_beam]).unwrap();
    assert_eq!(next["decision"], json!("wait"), "{next}");
    let mut with_beam = j;
    resume(&mut with_beam, good);
    let next = call(&mut c, "map_next", &[with_beam]).unwrap();
    assert_eq!(next["decision"], json!("line"), "{next}");
    assert_ne!(next["request"]["role"], json!("reference"));
}

fn sequence(records: &[Value], start: f64) -> Vec<(u64, String, f64, String, bool)> {
    records
        .iter()
        .map(|r| {
            (
                r["request"]["id"].as_u64().unwrap(),
                r["request"]["role"].as_str().unwrap().to_owned(),
                ((r["started"].as_f64().unwrap() - start) / 60.0).round(),
                r["outcome"].as_str().unwrap().to_owned(),
                r["analysis"]["level"].as_f64().unwrap_or(0.0) > 0.0,
            )
        })
        .collect()
}

#[test]
fn a_probe_before_each_long_line_finds_a_lost_beam_at_once() {
    // 2026-11-16 lab: the beam faded during a 36 min ladder line and was gone for the next two
    // (44 min) while the ESI current stayed in range; a reference every three lines saw it too late.
    let mut c = Controller::new(&json!({})).unwrap();
    let start = 1.8e9;
    let (off_from, off_to) = (5000.0, 9000.0);
    let out = simulate_with(
        &mut c,
        beam_plan(json!({"reference_every": 5})),
        json!({"beam_off": [[off_from, off_to]]}),
    );
    assert_eq!(out["outcome"], json!("done"), "{}", out["reason"]);
    let records = out["journal"]["records"].as_array().unwrap();
    let seq = sequence(records, start);
    // Every ladder line is preceded by a reference or a probe.
    for (i, r) in records.iter().enumerate() {
        if r["request"]["role"] == json!("ladder") && r["outcome"] == json!("done") {
            let before = records[..i]
                .iter()
                .rev()
                .find(|p| p["outcome"] == json!("done") && p["request"]["role"] != json!("wait"))
                .unwrap();
            let role = before["request"]["role"].as_str().unwrap();
            assert!(role == "probe" || role == "reference", "{seq:?}");
        }
    }
    // The first probe in the dark is followed by a wait, not by a ladder line.
    let dark_probe = records
        .iter()
        .position(|r| {
            let t = r["started"].as_f64().unwrap() - start;
            r["request"]["role"] == json!("probe") && t > off_from && t < off_to
        })
        .expect("a probe while the beam is off");
    assert_eq!(
        records[dark_probe + 1]["request"]["role"],
        json!("wait"),
        "{seq:?}"
    );
    // No ladder line started in the dark.
    assert!(!records.iter().any(|r| {
        let t = r["started"].as_f64().unwrap() - start;
        r["request"]["role"] == json!("ladder") && t > off_from + 60.0 && t < off_to
    }));
    // A probe is short: about a minute.
    let probe = records
        .iter()
        .find(|r| r["request"]["role"] == json!("probe"))
        .unwrap();
    assert!(probe["duration_s"].as_f64().unwrap() < 120.0, "{probe}");
}

#[test]
fn a_resume_measures_again_the_lines_no_reference_confirmed() {
    // 2026-11-16 lab: after two ladder lines without beam, the three references failed on the spray
    // and the map stopped with those lines counted as done.
    let mut c = Controller::new(&json!({})).unwrap();
    let out = simulate_with(
        &mut c,
        beam_plan(json!({"reference": {"probe": false}, "max_lines": 8})),
        json!({}),
    );
    let mut j = out["journal"].clone();
    let records = j["records"].as_array().unwrap().clone();
    let last_reference = records
        .iter()
        .rposition(|r| r["request"]["role"] == json!("reference"))
        .unwrap();
    let unconfirmed: Vec<&Value> = records[last_reference + 1..]
        .iter()
        .filter(|r| r["outcome"] == json!("done"))
        .collect();
    assert!(!unconfirmed.is_empty(), "{:?}", sequence(&records, 1.8e9));
    j["plan"]["map"]["max_lines"] = json!(600);
    j["session_start"] = json!(records.len());
    let next = call(&mut c, "map_next", &[j.clone()]).unwrap();
    assert_eq!(next["request"]["role"], json!("reference"));
    assert_eq!(
        next["progress"]["lines_without_beam"],
        json!(unconfirmed.len()),
        "{next}"
    );
}

#[test]
fn an_ordered_design_measures_its_points_in_the_order_written() {
    // 2026-11-16 lab: seed 52 kept the order of five points but ran four as P1, P3, P4, P2.
    let mut c = Controller::new(&json!({})).unwrap();
    let design = json!([[550, 14.5], [550, 6.0], [400, 16.6], [400, 7.7]]);
    let first_points = |ordered: bool, c: &mut Controller| {
        let plan = plan_with(json!({"seed": 52, "max_lines": 400,
            "design": {"amplitude": design, "frequency": [], "ordered": ordered},
            "ladder": {"repeats": 0, "max_lines": 2}, "stopping": {"repeats": 1},
            "stages": {"search": false, "validation": false, "local": false, "best": false}}));
        let out = call(
            c,
            "map_simulate",
            &[
                plan,
                instrument(),
                json!({"hypothesis": "H1", "journal": true, "max_lines": 400}),
            ],
        )
        .unwrap();
        let mut order: Vec<String> = Vec::new();
        for r in out["journal"]["records"].as_array().unwrap() {
            let point = r["request"]["point"].as_str().unwrap().to_owned();
            if r["request"]["role"] == json!("ladder") && !order.contains(&point) {
                order.push(point);
            }
        }
        order
    };
    assert_eq!(first_points(true, &mut c), ["P1", "P2", "P3", "P4"]);
    assert_ne!(first_points(false, &mut c), ["P1", "P2", "P3", "P4"]);
}

#[test]
fn a_beam_fading_step_by_step_is_lost_against_the_best_recent_reference() {
    // 2026-11-16: the 15:55 reference (26.5 pA) passed at 61 % of the one before (43.3 pA) and became
    // the measure of the next probes. Judged against the last good reference, a beam losing 40 % per
    // reference is never lost.
    let mut c = Controller::new(&json!({})).unwrap();
    let fade = json!({"beam_scale": [[4000, 1e9, 0.6], [6000, 1e9, 0.6], [8000, 1e9, 0.6]]});
    let lost = |window: f64, c: &mut Controller| {
        let out = simulate_with(
            c,
            beam_plan(json!({"reference": {"beam_window_s": window, "beam_max_wait_s": 1200}})),
            fade.clone(),
        );
        out["journal"]["records"]
            .as_array()
            .unwrap()
            .iter()
            .any(|r| r["request"]["role"] == json!("wait"))
    };
    assert!(
        !lost(0.0, &mut c),
        "judged against the last reference only, the fade passes"
    );
    assert!(
        lost(7200.0, &mut c),
        "judged against the best of the last 2 h, it is lost"
    );
}
