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
    APEX, Filter, StepPolicy, Stepper, amplitude_grid, band, frequency_grid, lstsq, sobol2,
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
    assert!(
        freq.iter()
            .all(|f| (100e6 / f - (100e6 / f).round()).abs() < 1e-6)
    );
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
    // The simulator times a point like the plan's prior (0.8 + 0.3 + 0.9 s): estimates match it.
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
    assert!((per_point - 3.0).abs() < 0.2, "{per_point}");
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
