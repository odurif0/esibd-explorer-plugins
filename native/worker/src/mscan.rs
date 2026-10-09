// SPDX-License-Identifier: GPL-2.0-or-later
// Ported from mscan/mscan_plugin.py, originally Copyright (C) 2021-2026 Tim Esser.
//! MScan's deterministic engine. Explorer owns all channels and performs the
//! returned `command` actions on Qt; this module never accesses a device DLL.
//!
//! RPC: prepare(settings, observation), start(settings, observation),
//! advance(observation, acknowledgement?), cancel(reason?, observation?),
//! action_failed(session, action_id, message), abort(reason), reset(), snapshot(),
//! result(start=0, count=1000), raw(start=0, count=1000), amplitude_steps(...).
//! Observations use Explorer wall/monotonic timestamps and a strictly increasing
//! sequence. Commands have session/action identities and need an acknowledgement
//! with start_wall/end_wall/end_mono, revisions and actual channel values. A
//! command is never retried. Errors and stop hold the last requested voltages.
//!
//! Settings: start/stop, mode ("Step by step" or "Continuous"), settling_s,
//! settle_timeout (> settling_s), voltage_tolerance. Stepped uses step and
//! integration_s; continuous uses sweep_rate * time_step_s and ignores step.
//! An AMPR offset additionally uses offset_factor. All active numbers are finite.
//! Observation fields are described by Amx/Rail/Detector/Offset below; identities
//! must include Explorer object/controller/backend/token identities and names.
//! Detector histories include un-subtracted currents, including NaN/Inf slots.
//! A window is (start, end], never nanmean; a sample at/after end proves closure.
//!
//! A step settles continuously for settling_s within settle_timeout, then waits
//! integration_s plus at most settle_timeout for closing detector data. A sweep
//! requires the PSU target observed ready before each time-step deadline and a
//! new finite selected-module sample before issuing the next command. A whole
//! missed interval faults, and late commands never shorten the next interval.
//! Wall/monotonic skew > 0.5s faults. The adapter additionally bounds Qt callbacks
//! to 5s, rejects expired/cancelled callbacks, and revalidates before each write.
//! Context cancellation/deadlines persist stopped/error status; reading results
//! and explicit cancel/abort remain available. Reset requires a terminal scan.
//! result/raw are paginated (default 1000, maximum 4096); replies carry totals.
//! Raw detector and PSU records are independently limited to max_raw_samples
//! (default 1,000,000). Exceeding that limit stops without another command.

use crate::Backend;
use crate::codec::float;
use crate::context::Context;
use crate::error::{Error, Result};
use serde::Deserialize;
use serde_json::{Value, json};
use std::collections::VecDeque;

const STEPPED: &str = "Step by step";
const CONTINUOUS: &str = "Continuous";
const PAGE_LIMIT: usize = 4096;
const POLL_S: f64 = 0.05;

fn missing_number() -> f64 {
    f64::NAN
}
fn stepped() -> String {
    STEPPED.to_owned()
}

fn number(value: &Value) -> std::result::Result<f64, String> {
    if let Some(v) = value.as_f64() {
        return Ok(v);
    }
    match value.get("$float").and_then(Value::as_str) {
        Some("nan") => Ok(f64::NAN),
        Some("inf") => Ok(f64::INFINITY),
        Some("-inf") => Ok(f64::NEG_INFINITY),
        _ => Err("Expected a number or a $float telemetry tag".to_owned()),
    }
}

fn read_number<'de, D: serde::Deserializer<'de>>(d: D) -> std::result::Result<f64, D::Error> {
    number(&Value::deserialize(d)?).map_err(serde::de::Error::custom)
}

fn read_numbers<'de, D: serde::Deserializer<'de>>(d: D) -> std::result::Result<Vec<f64>, D::Error> {
    Vec::<Value>::deserialize(d)?
        .iter()
        .map(number)
        .collect::<std::result::Result<_, _>>()
        .map_err(serde::de::Error::custom)
}

fn parse<T: serde::de::DeserializeOwned>(value: &Value, label: &str) -> Result<T> {
    serde_json::from_value(value.clone())
        .map_err(|e| Error::argument(format!("Invalid {label}: {e}")))
}

fn finite(v: f64, label: &str) -> Result<f64> {
    if !v.is_finite() {
        return Err(Error::argument(format!("{label} must be finite")));
    }
    Ok(v)
}

fn close(a: f64, b: f64, abs: f64) -> bool {
    a.is_finite() && b.is_finite() && (a - b).abs() <= abs.max(1e-10 * a.abs().max(b.abs()))
}

fn floats(values: &[f64]) -> Value {
    Value::Array(values.iter().copied().map(float).collect())
}

/// Inclusive only when aligned, with the same floating-point margin as numpy's
/// reference plan. The requested stop is validated even if it is not sampled.
pub fn amplitude_steps(start: f64, stop: f64, step: f64) -> Result<Vec<f64>> {
    if ![start, stop, step].iter().all(|v| v.is_finite()) || start.min(stop) < 0.0 || step <= 0.0 {
        return Err(Error::argument(
            "Use finite, nonnegative voltages and a positive step.",
        ));
    }
    let distance = (stop - start).abs();
    let ratio = distance / step;
    if distance == 0.0 || ratio > 100_000.0 {
        return Err(Error::argument(
            "Choose different limits and at most 100,001 points.",
        ));
    }
    let count = (ratio + 1e-10).floor() as usize + 1;
    if count < 2 {
        return Err(Error::argument("The step is larger than the scan span."));
    }
    let direction = if stop > start { 1.0 } else { -1.0 };
    let mut values: Vec<_> = (0..count)
        .map(|i| start + direction * step * i as f64)
        .collect();
    if let Some(last) = values.last_mut() {
        *last = if direction > 0.0 {
            last.min(stop)
        } else {
            last.max(stop)
        };
    }
    Ok(values)
}

#[derive(Clone, Deserialize)]
struct Settings {
    #[serde(deserialize_with = "read_number")]
    start: f64,
    #[serde(deserialize_with = "read_number")]
    stop: f64,
    #[serde(default = "missing_number", deserialize_with = "read_number")]
    step: f64,
    #[serde(default = "stepped")]
    mode: String,
    #[serde(deserialize_with = "read_number")]
    settling_s: f64,
    #[serde(deserialize_with = "read_number")]
    settle_timeout: f64,
    #[serde(deserialize_with = "read_number")]
    voltage_tolerance: f64,
    #[serde(default = "missing_number", deserialize_with = "read_number")]
    integration_s: f64,
    #[serde(default = "missing_number", deserialize_with = "read_number")]
    time_step_s: f64,
    #[serde(default = "missing_number", deserialize_with = "read_number")]
    sweep_rate: f64,
    #[serde(default = "missing_number", deserialize_with = "read_number")]
    offset_factor: f64,
    #[serde(default)]
    metadata: Value,
}

impl Settings {
    fn duration(&self) -> Result<f64> {
        let v = match self.mode.as_str() {
            STEPPED => self.integration_s,
            CONTINUOUS => self.time_step_s,
            _ => {
                return Err(Error::argument(
                    "Select Step by step or Continuous scan mode.",
                ));
            }
        };
        if !v.is_finite() || v <= 0.0 {
            return Err(Error::argument(
                "Measurement time / Time step must be positive and finite.",
            ));
        }
        Ok(v)
    }

    fn command_step(&self) -> Result<f64> {
        if self.mode != CONTINUOUS {
            return Ok(self.step);
        }
        if !self.sweep_rate.is_finite() || self.sweep_rate <= 0.0 {
            return Err(Error::argument("Sweep rate must be positive and finite."));
        }
        let v = self.sweep_rate * self.duration()?;
        if !v.is_finite() || v <= 0.0 {
            return Err(Error::argument(
                "Sweep rate * Time step must give a finite positive voltage increment.",
            ));
        }
        Ok(v)
    }

    fn steps(&self) -> Result<Vec<f64>> {
        let step = self.command_step()?;
        amplitude_steps(self.start, self.stop, step)
    }

    fn validate(&self) -> Result<Vec<f64>> {
        let steps = self.steps()?;
        self.duration()?;
        if ![self.settling_s, self.settle_timeout, self.voltage_tolerance]
            .iter()
            .all(|v| v.is_finite() && *v > 0.0)
        {
            return Err(Error::argument(
                "Settling, timeout and tolerance must be positive and finite.",
            ));
        }
        if self.settling_s >= self.settle_timeout {
            return Err(Error::argument(
                "Settle timeout must exceed the settling duration.",
            ));
        }
        Ok(steps)
    }
}

#[derive(Clone, Deserialize)]
struct Amx {
    identity: String,
    initialized: bool,
    on: bool,
    busy: bool,
    selected: Vec<u8>,
    links: Value,
    rows: Vec<Value>,
}

fn tagged_nonfinite(value: &Value) -> bool {
    match value {
        Value::Object(v) => v.contains_key("$float") || v.values().any(tagged_nonfinite),
        Value::Array(v) => v.iter().any(tagged_nonfinite),
        _ => false,
    }
}

impl Amx {
    fn validate(&self) -> Result<()> {
        if self.identity.is_empty() || !self.initialized || !self.on || self.busy {
            return Err(Error::runtime(
                "AMX must be initialized, ON and outside a transition.",
            ));
        }
        if self.selected != [0, 1] && self.selected != [2, 3] {
            return Err(Error::argument("Select one AMX pair: CH0-CH1 or CH2-CH3."));
        }
        if self.rows.len() != 2
            || !self.links.is_array()
            || self.links.as_array().is_none_or(Vec::is_empty)
        {
            return Err(Error::runtime(
                "AMX waveform / PSU association readback is unavailable.",
            ));
        }
        for row in &self.rows {
            if row.get("state").and_then(Value::as_str) != Some("Periodic") {
                return Err(Error::runtime("Selected AMX output must be Periodic."));
            }
            let has_timing = ["timing", "waveform"].iter().any(|k| {
                row.get(k).is_some_and(|v| match v {
                    Value::Object(o) => !o.is_empty(),
                    Value::Array(a) => !a.is_empty(),
                    Value::Null | Value::Bool(false) => false,
                    _ => true,
                })
            });
            if !has_timing || tagged_nonfinite(row) {
                return Err(Error::runtime(
                    "AMX edge-delay / waveform readback unavailable or nonfinite.",
                ));
            }
        }
        Ok(())
    }
}

#[derive(Clone, Deserialize)]
struct Rail {
    identity: String,
    device_identity: String,
    name: String,
    number: u8,
    registered: bool,
    real: bool,
    enabled: bool,
    active: bool,
    initialized: bool,
    on: bool,
    busy: bool,
    shutdown: bool,
    unit: String,
    use_monitors: bool,
    readback_status: String,
    revision: u64,
    #[serde(deserialize_with = "read_number")]
    value: f64,
    #[serde(deserialize_with = "read_number")]
    monitor: f64,
    #[serde(deserialize_with = "read_number")]
    min: f64,
    #[serde(deserialize_with = "read_number")]
    max: f64,
    #[serde(deserialize_with = "read_number")]
    hardware_voltage_limit: f64,
    #[serde(deserialize_with = "read_number")]
    hardware_current_limit: f64,
    #[serde(deserialize_with = "read_number")]
    current_limit_readback: f64,
    #[serde(deserialize_with = "read_number")]
    voltage_setpoint_readback: f64,
    #[serde(deserialize_with = "read_number")]
    current_readback: f64,
}

impl Rail {
    fn source(&self) -> Result<()> {
        if self.identity.is_empty()
            || self.device_identity.is_empty()
            || !self.registered
            || !self.real
            || !self.enabled
            || !self.active
            || !self.initialized
            || !self.on
            || self.shutdown
        {
            return Err(Error::runtime(format!(
                "{}: source changed or was stopped.",
                self.name
            )));
        }
        if self.unit != "V" || !self.use_monitors || self.readback_status.is_empty() {
            return Err(Error::runtime(format!(
                "{}: current PSU Channel interface required.",
                self.name
            )));
        }
        Ok(())
    }

    fn bounds(&self) -> Result<(f64, f64)> {
        let low = finite(self.min, "channel minimum")?.max(0.0);
        let high = finite(self.max, "channel maximum")?.min(finite(
            self.hardware_voltage_limit,
            "hardware voltage limit",
        )?);
        if self.hardware_voltage_limit < 0.0 || low > high {
            return Err(Error::runtime(format!(
                "{}: no admissible voltage range.",
                self.name
            )));
        }
        Ok((low, high))
    }

    fn current_limits(&self) -> Result<f64> {
        let limit = finite(self.current_limit_readback, "Ilim readback")?;
        let capacity = finite(self.hardware_current_limit, "hardware current limit")?;
        if limit <= 0.0 || limit > capacity {
            return Err(Error::runtime(format!(
                "{}: Ilim must be positive and within the hardware current limit.",
                self.name
            )));
        }
        Ok(limit)
    }

    fn check_current(&self, limit: f64) -> Result<()> {
        if !self.current_readback.is_finite() || self.current_readback < 0.0 {
            return Err(Error::runtime(format!(
                "{}: invalid current measurement.",
                self.name
            )));
        }
        if self.current_readback >= limit {
            return Err(Error::runtime(format!(
                "{}: measured current reached/exceeded Ilim; point not acquired. MScan does not switch HV OFF.",
                self.name
            )));
        }
        Ok(())
    }
}

#[derive(Clone, Deserialize)]
struct Detector {
    identity: String,
    name: String,
    device_name: String,
    module: i64,
    registered: bool,
    real: bool,
    unit: String,
    enabled: bool,
    initialized: bool,
    acquiring: bool,
    recording: bool,
    history_id: String,
    #[serde(deserialize_with = "read_number")]
    interval_ms: f64,
    #[serde(default)]
    cadence_since: Option<f64>,
    #[serde(deserialize_with = "read_numbers")]
    times: Vec<f64>,
    #[serde(deserialize_with = "read_numbers")]
    values: Vec<f64>,
}

impl Detector {
    fn validate(&self) -> Result<()> {
        if self.identity.is_empty()
            || self.history_id.is_empty()
            || !self.registered
            || !self.real
            || self.device_name.trim().to_uppercase() != "DMMR"
            || self.module < 0
            || self.unit != "A"
        {
            return Err(Error::runtime(
                "Select a registered real DMMR current module.",
            ));
        }
        if !self.enabled || !self.initialized || !self.acquiring || !self.recording {
            return Err(Error::runtime(format!(
                "{}: DMMR acquisition/recording stopped or unavailable.",
                self.name
            )));
        }
        if !self.interval_ms.is_finite() || self.interval_ms <= 0.0 {
            return Err(Error::runtime("DMMR polling interval unavailable."));
        }
        if self.times.len() != self.values.len() {
            return Err(Error::runtime("DMMR timestamps and values are misaligned."));
        }
        if self.times.iter().any(|v| !v.is_finite()) || self.times.windows(2).any(|v| v[0] >= v[1])
        {
            return Err(Error::runtime("Invalid or nonmonotonic DMMR timestamps."));
        }
        if self.cadence_since.is_some_and(|v| !v.is_finite()) {
            return Err(Error::argument("Invalid DMMR cadence epoch."));
        }
        Ok(())
    }

    fn cadence(&self, wall: f64, duration: f64, window_start: Option<f64>) -> Result<Value> {
        self.validate()?;
        let mut valid: Vec<_> = self
            .times
            .iter()
            .zip(&self.values)
            .rev()
            .filter(|(t, v)| v.is_finite() && self.cadence_since.is_none_or(|s| **t > s))
            .take(21)
            .map(|(t, _)| *t)
            .collect();
        valid.reverse();
        if valid.len() < 2 {
            return Err(Error::runtime(format!(
                "{}: waiting for two valid recorded DMMR samples to determine Time step / after interval change.",
                self.name
            )));
        }
        let mut intervals: Vec<_> = valid.windows(2).map(|v| v[1] - v[0]).collect();
        intervals.sort_by(f64::total_cmp);
        let largest = *intervals.last().unwrap();
        let configured = self.interval_ms / 1000.0;
        let last = *valid.last().unwrap();
        let age = wall - last;
        if age < 0.0 || age > 2.0_f64.max(3.0 * configured).max(3.0 * largest) {
            return Err(Error::runtime(format!(
                "{}: waiting for fresh valid DMMR samples.",
                self.name
            )));
        }
        // Python round uses ties-to-even; timestamp roundoff must not add 1 ms.
        let micros = (configured.max(largest) * 1e6).round_ties_even();
        let minimum = ((micros + 999.0) / 1000.0).floor() / 1000.0;
        if !minimum.is_finite() || duration < minimum {
            return Err(Error::runtime(format!(
                "{}: Time step must be at least {minimum} s for the selected DMMR module.",
                self.name
            )));
        }
        if window_start.is_some_and(|s| last <= s) {
            return Err(Error::runtime(format!(
                "{}: no new valid DMMR sample within Time step. Scan stopped without advancing.",
                self.name
            )));
        }
        let mid = intervals.len() / 2;
        let typical = if intervals.len() % 2 == 0 {
            (intervals[mid - 1] + intervals[mid]) / 2.0
        } else {
            intervals[mid]
        };
        Ok(
            json!({"configured_interval_s": configured, "typical_interval_s": typical,
            "largest_interval_s": largest, "minimum_time_step_s": minimum,
            "last_sample_time": last, "sample_count": valid.len()}),
        )
    }
}

#[derive(Clone, Deserialize)]
struct Offset {
    identity: String,
    name: String,
    registered: bool,
    real: bool,
    enabled: bool,
    active: bool,
    initialized: bool,
    on: bool,
    busy: bool,
    shutdown: bool,
    state: String,
    #[serde(deserialize_with = "read_number")]
    value: f64,
    #[serde(deserialize_with = "read_number")]
    monitor: f64,
    #[serde(deserialize_with = "read_number")]
    min: f64,
    #[serde(deserialize_with = "read_number")]
    max: f64,
}

impl Offset {
    fn source(&self) -> Result<()> {
        if self.identity.is_empty()
            || !self.registered
            || !self.real
            || !self.enabled
            || !self.active
            || !self.initialized
            || !self.on
            || self.shutdown
        {
            return Err(Error::runtime(format!(
                "{}: offset source changed or was stopped.",
                self.name
            )));
        }
        if ["error", "mismatch", "stored"].contains(&self.state.as_str()) {
            return Err(Error::runtime(format!(
                "{}: offset setpoint {}.",
                self.name, self.state
            )));
        }
        Ok(())
    }
    fn bounds(&self, value: f64) -> Result<()> {
        finite(self.min, "offset minimum")?;
        finite(self.max, "offset maximum")?;
        if !value.is_finite() || self.min > self.max || value < self.min || value > self.max {
            return Err(Error::runtime(format!(
                "{}: offset target / restoration outside allowed range.",
                self.name
            )));
        }
        Ok(())
    }
}

#[derive(Clone, Deserialize)]
struct Observation {
    sequence: u64,
    wall: f64,
    mono: f64,
    amx: Amx,
    rails: Vec<Rail>,
    detector: Detector,
    #[serde(default)]
    offset: Option<Offset>,
    #[serde(default)]
    other_scan_running: bool,
}

impl Observation {
    fn clock(&self) -> Result<()> {
        finite(self.wall, "wall clock")?;
        finite(self.mono, "monotonic clock")?;
        if self.sequence == 0 || self.mono < 0.0 {
            return Err(Error::argument(
                "Invalid observation sequence / monotonic clock.",
            ));
        }
        Ok(())
    }
    fn volts(&self) -> Vec<f64> {
        self.rails.iter().map(|r| r.monitor).collect()
    }
    fn currents(&self) -> Vec<f64> {
        self.rails.iter().map(|r| r.current_readback).collect()
    }
}

#[derive(Clone)]
struct RailState {
    initial: f64,
    ilim: f64,
    revision: u64,
    confirmed: Option<f64>,
    expected: f64,
    value: f64,
}

#[derive(Clone)]
struct Point {
    status: String,
    mean: f64,
    samples: usize,
    finite_samples: usize,
    start: f64,
    end: f64,
    volts: Vec<f64>,
    currents: Vec<f64>,
    offset: Option<(f64, f64)>,
}

impl Point {
    fn empty(rails: usize, offset: bool) -> Self {
        Self {
            status: "not acquired".to_owned(),
            mean: f64::NAN,
            samples: 0,
            finite_samples: 0,
            start: f64::NAN,
            end: f64::NAN,
            volts: vec![f64::NAN; rails],
            currents: vec![f64::NAN; rails],
            offset: offset.then_some((f64::NAN, f64::NAN)),
        }
    }
    fn value(&self, index: usize) -> Value {
        json!({"index": index, "status": self.status, "mean": float(self.mean), "samples": self.samples,
            "finite_samples": self.finite_samples, "window_start": float(self.start), "window_end": float(self.end),
            "rail_v": floats(&self.volts), "rail_i": floats(&self.currents),
            "offset": self.offset.map(|(a,b)| vec![float(a),float(b)])})
    }
}

#[derive(Clone)]
struct Pending {
    index: usize,
    start: f64,
    end: f64,
    volts: Vec<f64>,
    currents: Vec<f64>,
    offset: Option<(f64, f64)>,
}

#[derive(Clone, Copy, PartialEq)]
enum Phase {
    AwaitCommand,
    Settling,
    Measuring,
    Continuous,
    Draining,
    Restoring,
    Finished,
}

impl Phase {
    fn name(self) -> &'static str {
        match self {
            Self::AwaitCommand => "awaiting_command",
            Self::Settling => "settling",
            Self::Measuring => "acquiring",
            Self::Continuous => "continuous",
            Self::Draining => "waiting_for_detector",
            Self::Restoring => "restoring",
            Self::Finished => "finished",
        }
    }
}

struct Command {
    id: u64,
    targets: Vec<f64>,
    offset: Option<f64>,
    restore: bool,
    latest_mono: Option<f64>,
    previous: Option<Pending>,
    issued_wall: f64,
    issued_mono: f64,
}

#[derive(Deserialize)]
struct Acknowledgement {
    session: u64,
    action_id: u64,
    start_wall: f64,
    end_wall: f64,
    end_mono: f64,
    revisions: Vec<u64>,
    #[serde(deserialize_with = "read_numbers")]
    values: Vec<f64>,
    #[serde(default)]
    offset_value: Option<f64>,
}

#[derive(Default)]
struct Raw {
    command_start: Vec<f64>,
    command_end: Vec<f64>,
    detector_time: Vec<f64>,
    detector_current: Vec<f64>,
    psu_time: Vec<f64>,
    psu_v: Vec<Vec<f64>>,
    psu_i: Vec<Vec<f64>>,
    psu_target: Vec<Vec<f64>>,
    psu_ready: Vec<bool>,
    offset_v: Vec<f64>,
    offset_target: Vec<f64>,
}

struct Run {
    session: u64,
    settings: Settings,
    frozen: Observation,
    rails: Vec<RailState>,
    steps: Vec<f64>,
    metadata: Value,
    phase: Phase,
    status: String,
    error: String,
    index: usize,
    last_sequence: u64,
    last_mono: f64,
    command_sequence: u64,
    command: Option<Command>,
    since: Option<f64>,
    deadline: f64,
    begin_wall: f64,
    begin_mono: f64,
    origin: Option<(f64, f64)>,
    baseline: Option<f64>,
    cutoff: Option<f64>,
    ready_seen: bool,
    offset_expected: Option<f64>,
    points: Vec<Point>,
    pending: VecDeque<Pending>,
    updates: Vec<Value>,
    raw: Raw,
    max_raw: usize,
}

impl Run {
    fn prepare(settings: Settings, obs: Observation, session: u64, max_raw: usize) -> Result<Self> {
        let steps = settings.validate()?;
        obs.clock()?;
        let duration = settings.duration()?;
        if ![
            obs.wall + duration,
            obs.mono + duration + settings.settle_timeout,
            obs.mono + settings.settle_timeout,
        ]
        .iter()
        .all(|v| v.is_finite())
        {
            return Err(Error::argument(
                "Timing would overflow a finite acquisition window / deadline.",
            ));
        }
        obs.amx.validate()?;
        obs.detector.validate()?;
        if obs.other_scan_running {
            return Err(Error::runtime(
                "Finish the other scan before starting this coupled scan.",
            ));
        }
        if obs.rails.len() != 2
            || obs.rails[0].number != 0
            || obs.rails[1].number != 1
            || obs.rails[0].identity == obs.rails[1].identity
            || obs.rails[0].device_identity != obs.rails[1].device_identity
        {
            return Err(Error::argument(
                "The selected AMX pair requires two distinct rails (CH0/CH1) of one associated PSU.",
            ));
        }
        let low = settings.start.min(settings.stop);
        let high = settings.start.max(settings.stop);
        let mut rails = Vec::with_capacity(obs.rails.len());
        for r in &obs.rails {
            r.source()?;
            if r.busy {
                return Err(Error::runtime(format!(
                    "{}: wait for the PSU transition to finish.",
                    r.name
                )));
            }
            finite(r.monitor, "PSU voltage readback")?;
            let (min, max) = r.bounds()?;
            if low < min || high > max {
                return Err(Error::argument(format!(
                    "{}: requested scan exceeds allowed {min}-{max} V.",
                    r.name
                )));
            }
            let initial = finite(r.voltage_setpoint_readback, "Vset readback")?;
            if initial < 0.0 || !close(r.value, initial, 1e-9) {
                return Err(Error::runtime(format!(
                    "{}: Vset is not yet confirmed by the PSU.",
                    r.name
                )));
            }
            if initial < min || initial > max {
                return Err(Error::argument(format!(
                    "{}: final restoration outside allowed range.",
                    r.name
                )));
            }
            let ilim = r.current_limits()?;
            r.check_current(ilim)?;
            rails.push(RailState {
                initial,
                ilim,
                revision: r.revision,
                confirmed: Some(initial),
                expected: initial,
                value: r.value,
            });
        }
        let offset_expected = if let Some(o) = &obs.offset {
            o.source()?;
            if o.busy || o.state != "confirmed" || !o.monitor.is_finite() {
                return Err(Error::runtime(
                    "Offset setpoint / Monitor not confirmed by the AMPR.",
                ));
            }
            finite(settings.offset_factor, "Offset coefficient U/V")?;
            o.bounds(settings.offset_factor * low)?;
            o.bounds(settings.offset_factor * high)?;
            o.bounds(o.value)?;
            Some(o.value)
        } else {
            None
        };
        let cadence = if settings.mode == CONTINUOUS {
            obs.detector.cadence(obs.wall, settings.duration()?, None)?
        } else {
            Value::Null
        };
        let mut metadata = if settings.metadata.is_object() {
            settings.metadata.clone()
        } else {
            json!({})
        };
        metadata["mode"] = json!(settings.mode);
        metadata["command_step_v"] = json!(settings.command_step()?);
        metadata["requested_rate_v_s"] = if settings.mode == CONTINUOUS {
            json!(settings.sweep_rate.copysign(settings.stop - settings.start))
        } else {
            Value::Null
        };
        metadata["settling_s"] = json!(settings.settling_s);
        metadata["measurement_s"] = json!(settings.duration()?);
        metadata["time_step_s"] = if settings.mode == CONTINUOUS {
            json!(settings.duration()?)
        } else {
            Value::Null
        };
        metadata["detector_cadence"] = cadence;
        metadata["waveform"] = json!(obs.amx.rows);
        metadata["links"] = obs.amx.links.clone();
        metadata["amplitude_definition"] =
            json!("Vpos=+A, Vneg=-A relative to PSU reference; external offset not included");
        metadata["calibration"] = json!("None; amplitude in V, not m/z");
        metadata["background_subtracted"] = json!(false);
        let points = steps
            .iter()
            .map(|_| Point::empty(rails.len(), obs.offset.is_some()))
            .collect();
        let raw = Raw {
            command_start: vec![f64::NAN; steps.len()],
            command_end: vec![f64::NAN; steps.len()],
            ..Raw::default()
        };
        Ok(Self {
            session,
            settings,
            last_sequence: obs.sequence,
            last_mono: obs.mono,
            frozen: obs,
            rails,
            steps,
            metadata,
            phase: Phase::AwaitCommand,
            status: "running".to_owned(),
            error: String::new(),
            index: 0,
            command_sequence: 0,
            command: None,
            since: None,
            deadline: 0.0,
            begin_wall: 0.0,
            begin_mono: 0.0,
            origin: None,
            baseline: None,
            cutoff: None,
            ready_seen: false,
            offset_expected,
            points,
            pending: VecDeque::new(),
            updates: Vec::new(),
            raw,
            max_raw,
        })
    }

    fn observe(&mut self, obs: &Observation) -> Result<bool> {
        obs.clock()?;
        if ![
            obs.wall + self.settings.duration()?,
            obs.mono + self.settings.duration()? + self.settings.settle_timeout,
        ]
        .iter()
        .all(|v| v.is_finite())
        {
            return Err(Error::runtime(
                "Timing would overflow a finite acquisition window / deadline.",
            ));
        }
        if obs.sequence <= self.last_sequence || obs.mono < self.last_mono {
            return Err(Error::runtime(
                "Stale observation / monotonic clock moved backwards.",
            ));
        }
        self.last_sequence = obs.sequence;
        self.last_mono = obs.mono;
        obs.amx.validate()?;
        let amx = &self.frozen.amx;
        if obs.amx.identity != amx.identity
            || obs.amx.links != amx.links
            || obs.amx.rows != amx.rows
            || obs.amx.selected != amx.selected
        {
            return Err(Error::runtime(
                "AMX waveform, source or PSU association changed during the scan.",
            ));
        }
        obs.detector.validate()?;
        let detector = &self.frozen.detector;
        if obs.detector.identity != detector.identity
            || obs.detector.module != detector.module
            || obs.detector.name != detector.name
        {
            return Err(Error::runtime(
                "DMMR module identity changed during the scan.",
            ));
        }
        if obs.detector.interval_ms != detector.interval_ms {
            return Err(Error::runtime(
                "DMMR interval changed during the scan. Restart with the new cadence.",
            ));
        }
        if obs.detector.history_id != detector.history_id {
            return Err(Error::runtime("Detector history reset during the scan."));
        }
        if obs.rails.len() != self.rails.len()
            || obs.offset.is_some() != self.frozen.offset.is_some()
        {
            return Err(Error::runtime("Scan source count changed."));
        }
        let mut ready = true;
        let low = self.settings.start.min(self.settings.stop);
        let high = self.settings.start.max(self.settings.stop);
        for ((r, frozen), state) in obs
            .rails
            .iter()
            .zip(&self.frozen.rails)
            .zip(&mut self.rails)
        {
            r.source()?;
            if r.identity != frozen.identity
                || r.device_identity != frozen.device_identity
                || r.name != frozen.name
                || r.number != frozen.number
            {
                return Err(Error::runtime(format!(
                    "{}: source changed or was stopped.",
                    frozen.name
                )));
            }
            if r.revision != state.revision {
                return Err(Error::runtime(format!(
                    "{}: setpoint changed outside the scan (request revision).",
                    r.name
                )));
            }
            let fresh = !r.busy
                && [
                    r.hardware_voltage_limit,
                    r.hardware_current_limit,
                    r.current_limit_readback,
                    r.voltage_setpoint_readback,
                ]
                .iter()
                .all(|v| v.is_finite());
            if fresh {
                if state
                    .confirmed
                    .is_some_and(|c| !close(c, r.voltage_setpoint_readback, 1e-9))
                {
                    return Err(Error::runtime(format!(
                        "{}: PSU setpoint changed outside the scan (confirmed readback).",
                        r.name
                    )));
                }
                state.confirmed = Some(r.voltage_setpoint_readback);
                let (min, max) = r.bounds()?;
                if low < min || high > max || state.initial < min || state.initial > max {
                    return Err(Error::runtime(format!(
                        "{}: voltage limits changed; scan or final restoration is no longer allowed.",
                        r.name
                    )));
                }
                let ilim = r.current_limits()?;
                if !close(ilim, state.ilim, 1e-12) {
                    return Err(Error::runtime(format!(
                        "{}: Ilim changed during the scan.",
                        r.name
                    )));
                }
                if r.current_readback.is_finite() {
                    r.check_current(ilim)?;
                }
            }
            ready &= fresh
                && r.current_readback.is_finite()
                && r.current_readback >= 0.0
                && r.monitor.is_finite()
                && r.monitor >= 0.0
                && (r.monitor - state.expected).abs() <= self.settings.voltage_tolerance;
            state.value = r.value;
        }
        if let (Some(o), Some(frozen)) = (&obs.offset, &self.frozen.offset) {
            o.source()?;
            if o.identity != frozen.identity || o.name != frozen.name {
                return Err(Error::runtime("Offset source changed."));
            }
            let expected = self.offset_expected.unwrap();
            if !close(o.value, expected, 1e-9) {
                return Err(Error::runtime(format!(
                    "{}: offset changed outside the scan.",
                    o.name
                )));
            }
            ready &= !o.busy
                && o.state == "confirmed"
                && o.monitor.is_finite()
                && (o.monitor - expected).abs() <= self.settings.voltage_tolerance;
        }
        if let Some((wall, mono)) = self.origin
            && ((obs.wall - wall) - (obs.mono - mono)).abs() > 0.5
        {
            return Err(Error::runtime("System clock changed during acquisition."));
        }
        Ok(ready)
    }

    fn issue(
        &mut self,
        obs: &Observation,
        restore: bool,
        previous: Option<Pending>,
        latest: Option<f64>,
    ) -> Result<()> {
        let targets: Vec<_> = self
            .rails
            .iter()
            .map(|r| {
                if restore {
                    r.initial
                } else {
                    self.steps[self.index]
                }
            })
            .collect();
        for (r, target) in obs.rails.iter().zip(&targets) {
            if r.busy {
                return Err(Error::runtime(format!(
                    "{}: PSU transition in progress; no scan command sent.",
                    r.name
                )));
            }
            let (min, max) = r.bounds()?;
            if !target.is_finite() || *target < min || *target > max {
                return Err(Error::runtime(
                    "Requested amplitude / restoration exceeds allowed range.",
                ));
            }
            r.check_current(r.current_limits()?)?;
        }
        let offset = self.frozen.offset.as_ref().map(|o| {
            if restore {
                o.value
            } else {
                self.settings.offset_factor * self.steps[self.index]
            }
        });
        if let (Some(o), Some(target)) = (&obs.offset, offset) {
            if o.busy {
                return Err(Error::runtime(
                    "AMPR transition in progress; no scan command sent.",
                ));
            }
            o.bounds(target)?;
        }
        if self.settings.mode == CONTINUOUS && !restore {
            obs.detector.cadence(
                obs.wall,
                self.settings.duration()?,
                previous.as_ref().map(|p| p.start),
            )?;
        }
        if latest.is_some_and(|t| obs.mono >= t) {
            return Err(Error::runtime(
                "Continuous time step missed; no catch-up command sent.",
            ));
        }
        self.command_sequence += 1;
        self.command = Some(Command {
            id: self.command_sequence,
            targets,
            offset,
            restore,
            latest_mono: latest,
            previous,
            issued_wall: obs.wall,
            issued_mono: obs.mono,
        });
        self.phase = Phase::AwaitCommand;
        Ok(())
    }

    fn acknowledge(&mut self, ack: Acknowledgement, obs: &Observation) -> Result<()> {
        let command = self
            .command
            .as_ref()
            .ok_or_else(|| Error::runtime("Unexpected command acknowledgement."))?;
        if ack.session != self.session || ack.action_id != command.id {
            return Err(Error::runtime(
                "Stale or mismatched command acknowledgement.",
            ));
        }
        if ack.revisions.len() != self.rails.len()
            || ack.values.len() != self.rails.len()
            || ack.offset_value.is_some() != command.offset.is_some()
        {
            return Err(Error::argument(
                "Command acknowledgement dimensions / offset do not match.",
            ));
        }
        for v in [ack.start_wall, ack.end_wall, ack.end_mono] {
            finite(v, "Command acknowledgement timestamp")?;
        }
        if ack.start_wall < command.issued_wall - 0.5
            || ack.end_wall < ack.start_wall
            || ack.end_mono < command.issued_mono
            || ack.end_mono > obs.mono
            || ack.end_wall > obs.wall + 0.5
            || ((ack.end_wall - command.issued_wall) - (ack.end_mono - command.issued_mono)).abs()
                > 0.5
        {
            return Err(Error::runtime(
                "Invalid command acknowledgement clock / system clock changed.",
            ));
        }
        // The adapter checks the deadline immediately before the first write.
        // Reject a late acknowledgement too; never use it to advance/catch up.
        if command.latest_mono.is_some_and(|t| ack.end_mono >= t) {
            return Err(Error::runtime(
                "Continuous time step missed during command; no catch-up command sent.",
            ));
        }
        for (j, r) in self.rails.iter().enumerate() {
            if ack.revisions[j] < r.revision || ack.revisions[j] > r.revision.saturating_add(1) {
                return Err(Error::runtime(
                    "Unexpected setpoint revision while acknowledging scan command.",
                ));
            }
            finite(ack.values[j], "Acknowledged channel value")?;
            if ack.revisions[j] == r.revision && !close(ack.values[j], r.value, 1e-9) {
                return Err(Error::runtime(
                    "Changed channel value without acknowledged request revision.",
                ));
            }
        }
        if ack.offset_value.is_some_and(|v| !v.is_finite()) {
            return Err(Error::argument("Nonfinite acknowledged offset target."));
        }
        let command = self.command.take().unwrap();
        for (j, r) in self.rails.iter_mut().enumerate() {
            if ack.revisions[j] != r.revision {
                r.confirmed = None;
            }
            r.revision = ack.revisions[j];
            r.value = ack.values[j];
            r.expected = command.targets[j];
        }
        self.offset_expected = ack.offset_value;
        if !command.restore && self.settings.mode == CONTINUOUS {
            self.raw.command_start[self.index] = ack.start_wall;
            self.raw.command_end[self.index] = ack.end_wall;
        }
        if let Some(mut previous) = command.previous {
            if ack.start_wall <= previous.start {
                return Err(Error::runtime(
                    "System clock moved backwards during continuous acquisition.",
                ));
            }
            previous.end = ack.start_wall;
            self.pending.push_back(previous);
            self.begin_wall = ack.start_wall;
            self.begin_mono = ack.end_mono;
            self.deadline = ack.end_mono + self.settings.duration()?;
            self.ready_seen = false;
            self.phase = Phase::Continuous;
        } else {
            self.phase = if command.restore {
                Phase::Restoring
            } else {
                Phase::Settling
            };
            self.deadline = ack.end_mono + self.settings.settle_timeout;
        }
        self.since = None;
        Ok(())
    }

    fn history(&self, d: &Detector, baseline: Option<f64>) -> Result<()> {
        if let Some(t) = baseline
            && d.times.first().is_none_or(|v| *v > t)
        {
            return Err(Error::runtime(
                "Detector history buffer truncated during averaging.",
            ));
        }
        Ok(())
    }

    fn offset_snapshot(&self, obs: &Observation) -> Option<(f64, f64)> {
        obs.offset
            .as_ref()
            .map(|o| (o.monitor, self.offset_expected.unwrap()))
    }

    fn pending(&self, obs: &Observation) -> Pending {
        Pending {
            index: self.index,
            start: self.begin_wall,
            end: 0.0,
            volts: obs.volts(),
            currents: obs.currents(),
            offset: self.offset_snapshot(obs),
        }
    }

    fn store(&mut self, window: Pending, samples: &[f64], continuous: bool) {
        let count = samples.len();
        let valid = samples.iter().filter(|v| v.is_finite()).count();
        let mean = if count > 0 && valid == count {
            samples.iter().sum::<f64>() / count as f64
        } else {
            f64::NAN
        };
        let status = if mean.is_finite() {
            if continuous {
                "acquired during sweep"
            } else {
                "acquired"
            }
        } else {
            "invalid detector data"
        };
        let point = Point {
            status: status.to_owned(),
            mean,
            samples: count,
            finite_samples: valid,
            start: window.start,
            end: window.end,
            volts: window.volts,
            currents: window.currents,
            offset: window.offset,
        };
        self.updates.push(point.value(window.index));
        self.points[window.index] = point;
    }

    fn capture(&mut self, obs: &Observation) -> Result<f64> {
        let origin = self.origin.unwrap().0;
        let cursor = self.raw.detector_time.last().copied().unwrap_or(origin);
        self.history(
            &obs.detector,
            if self.raw.detector_time.is_empty() {
                self.baseline
            } else {
                Some(cursor)
            },
        )?;
        let cutoff = self.cutoff.unwrap_or(obs.wall);
        let lo = obs.detector.times.partition_point(|t| *t <= cursor);
        let hi = obs.detector.times.partition_point(|t| *t <= cutoff);
        if hi > lo {
            if self.raw.detector_time.len() + hi - lo > self.max_raw {
                return Err(Error::runtime(
                    "MScan raw detector sample limit exceeded; stopped without another command.",
                ));
            }
            self.raw
                .detector_time
                .extend_from_slice(&obs.detector.times[lo..hi]);
            self.raw
                .detector_current
                .extend_from_slice(&obs.detector.values[lo..hi]);
        }
        Ok(obs
            .detector
            .times
            .last()
            .copied()
            .unwrap_or(f64::NEG_INFINITY))
    }

    fn finish_windows(&mut self, watermark: f64) {
        while self.pending.front().is_some_and(|p| p.end <= watermark) {
            let p = self.pending.pop_front().unwrap();
            let lo = self.raw.detector_time.partition_point(|t| *t <= p.start);
            let hi = self.raw.detector_time.partition_point(|t| *t <= p.end);
            let samples = self.raw.detector_current[lo..hi].to_vec();
            self.store(p, &samples, true);
        }
    }

    fn raw_observation(&mut self, obs: &Observation, ready: bool) -> Result<()> {
        if self.raw.psu_time.len() >= self.max_raw {
            return Err(Error::runtime("MScan raw PSU observation limit exceeded."));
        }
        self.raw.psu_time.push(obs.wall);
        self.raw.psu_v.push(obs.volts());
        self.raw.psu_i.push(obs.currents());
        self.raw
            .psu_target
            .push(self.rails.iter().map(|r| r.expected).collect());
        self.raw.psu_ready.push(ready);
        if let Some((v, target)) = self.offset_snapshot(obs) {
            self.raw.offset_v.push(v);
            self.raw.offset_target.push(target);
        }
        Ok(())
    }

    fn advance(&mut self, obs: &Observation, ack: Option<Acknowledgement>) -> Result<()> {
        if self.phase == Phase::Finished {
            return Err(Error::runtime(
                "Scan is finished; reset before starting a new scan.",
            ));
        }
        let continuous_command = self.phase == Phase::AwaitCommand
            && self.command.as_ref().is_some_and(|c| c.previous.is_some());
        if self.phase == Phase::AwaitCommand {
            self.acknowledge(
                ack.ok_or_else(|| {
                    Error::runtime(
                        "A pending command requires an acknowledgement; it will not be retried.",
                    )
                })?,
                obs,
            )?;
        } else if ack.is_some() {
            return Err(Error::runtime(
                "Unexpected or duplicate command acknowledgement.",
            ));
        }
        let ready = self.observe(obs)?;
        // Acknowledgement completes the command callback, not the next scan
        // poll. Like the reference, defer pending-window finalization until
        // that poll validates the common wall/monotonic acquisition clock.
        if continuous_command {
            return Ok(());
        }
        let duration = self.settings.duration()?;
        match self.phase {
            Phase::Settling | Phase::Restoring => {
                self.since = if ready {
                    self.since.or(Some(obs.mono))
                } else {
                    None
                };
                if self
                    .since
                    .is_some_and(|s| obs.mono - s >= self.settings.settling_s)
                {
                    if self.phase == Phase::Restoring {
                        self.status = "completed".to_owned();
                        self.phase = Phase::Finished;
                    } else {
                        self.begin_wall = obs.wall;
                        self.begin_mono = obs.mono;
                        self.origin = Some((obs.wall, obs.mono));
                        self.baseline = obs.detector.times.last().copied();
                        self.deadline = obs.mono + duration;
                        self.ready_seen = true;
                        self.phase = if self.settings.mode == CONTINUOUS {
                            Phase::Continuous
                        } else {
                            Phase::Measuring
                        };
                    }
                } else if obs.mono >= self.deadline {
                    return Err(Error::runtime(
                        "PSU voltage / offset settling timed out; no detector value assigned to this point.",
                    ));
                }
            }
            Phase::Measuring => {
                if !ready {
                    return Err(Error::runtime(
                        "PSU / offset readback became invalid or left tolerance during acquisition.",
                    ));
                }
                if obs.mono >= self.deadline {
                    self.history(&obs.detector, self.baseline)?;
                    let end = self.begin_wall + duration;
                    if obs.detector.times.last().is_some_and(|t| *t >= end) {
                        let lo = obs
                            .detector
                            .times
                            .partition_point(|t| *t <= self.begin_wall);
                        let hi = obs.detector.times.partition_point(|t| *t <= end);
                        let mut p = self.pending(obs);
                        p.end = end;
                        self.store(p, &obs.detector.values[lo..hi], false);
                        self.origin = None;
                        if self.index + 1 == self.steps.len() {
                            self.issue(obs, true, None, None)?;
                        } else {
                            self.index += 1;
                            self.issue(obs, false, None, None)?;
                        }
                    } else if obs.mono >= self.deadline + self.settings.settle_timeout {
                        return Err(Error::runtime("Fresh detector data timed out."));
                    }
                }
            }
            Phase::Continuous | Phase::Draining => {
                self.raw_observation(obs, ready)?;
                if self.ready_seen && !ready {
                    return Err(Error::runtime(
                        "PSU / offset readback became invalid or left tolerance after reaching the continuous target.",
                    ));
                }
                self.ready_seen |= ready && obs.mono <= self.deadline;
                if !ready && obs.mono - self.begin_mono >= self.settings.settle_timeout {
                    return Err(Error::runtime(
                        "PSU voltage settling timed out during continuous acquisition.",
                    ));
                }
                let watermark = self.capture(obs)?;
                self.finish_windows(watermark);
                if self
                    .pending
                    .front()
                    .is_some_and(|p| obs.wall - p.end > self.settings.settle_timeout)
                {
                    return Err(Error::runtime(
                        "Fresh detector data timed out during continuous acquisition.",
                    ));
                }
                if self.phase == Phase::Draining {
                    if self.pending.is_empty() {
                        self.issue(obs, true, None, None)?;
                    }
                } else if obs.mono >= self.deadline {
                    if !ready || !self.ready_seen {
                        return Err(Error::runtime(
                            "PSU target was not confirmed within the continuous time step; point not acquired and no next command sent.",
                        ));
                    }
                    if obs.mono >= self.deadline + duration {
                        return Err(Error::runtime(
                            "Continuous time step missed; no catch-up command sent.",
                        ));
                    }
                    let mut previous = self.pending(obs);
                    obs.detector
                        .cadence(obs.wall, duration, Some(previous.start))?;
                    if self.index + 1 < self.steps.len() {
                        self.index += 1;
                        self.issue(obs, false, Some(previous), Some(self.deadline + duration))?;
                    } else {
                        previous.end = obs.wall;
                        if previous.end <= previous.start {
                            return Err(Error::runtime(
                                "System clock moved backwards during continuous acquisition.",
                            ));
                        }
                        self.cutoff = Some(obs.wall);
                        self.pending.push_back(previous);
                        self.phase = Phase::Draining;
                        self.finish_windows(watermark);
                        if self.pending.is_empty() {
                            self.issue(obs, true, None, None)?;
                        }
                    }
                }
            }
            Phase::AwaitCommand | Phase::Finished => {}
        }
        Ok(())
    }

    fn terminate(&mut self, status: &str, message: &str, obs: Option<&Observation>) {
        if self.phase == Phase::Finished {
            return;
        }
        // A best-effort last *read* can retain partial raw data and finalize an
        // already closed interval. It must never create the interrupted point.
        if let (Some((wall, mono)), Some(obs)) = (self.origin, obs) {
            let detector = &self.frozen.detector;
            if self.settings.mode == CONTINUOUS
                && obs.clock().is_ok()
                && obs.detector.validate().is_ok()
                && obs.detector.identity == detector.identity
                && obs.detector.history_id == detector.history_id
                && obs.detector.interval_ms == detector.interval_ms
                && obs.detector.module == detector.module
                && obs.detector.name == detector.name
                && obs.sequence >= self.last_sequence
                && obs.mono >= self.last_mono
                && ((obs.wall - wall) - (obs.mono - mono)).abs() <= 0.5
                && let Ok(watermark) = self.capture(obs)
            {
                self.finish_windows(watermark);
            }
        }
        self.status = status.to_owned();
        self.error = message.to_owned();
        self.command = None;
        self.phase = Phase::Finished;
        if let Some((index, p)) = self
            .points
            .iter_mut()
            .enumerate()
            .find(|(_, p)| p.status == "not acquired")
        {
            p.status = status.to_owned();
            self.updates.push(p.value(index));
        }
    }

    fn snapshot(&self) -> Value {
        let action = if self.phase == Phase::Finished {
            Value::Null
        } else if let Some(c) = &self.command {
            json!({"kind": "command", "session": self.session, "action_id": c.id,
                "targets": c.targets, "offset": c.offset, "restore": c.restore,
                "latest_mono": c.latest_mono, "index": self.index,
                "expected_revisions": self.rails.iter().map(|r| r.revision).collect::<Vec<_>>()})
        } else {
            let remaining = self.deadline - self.last_mono;
            let seconds =
                if matches!(self.phase, Phase::Measuring | Phase::Continuous) && remaining > 0.0 {
                    // A sub-clock-resolution remainder must not spin or create
                    // duplicate timestamps in a simulated Explorer recorder.
                    POLL_S.min(remaining.max(1e-6))
                } else {
                    POLL_S
                };
            json!({"kind": "wait", "seconds": seconds})
        };
        json!({"session": self.session, "state": self.phase.name(), "status": self.status, "error": self.error,
            "index": self.index, "total": self.steps.len(), "action": action,
            "updates": self.updates, "expected": self.rails.iter().map(|r| r.expected).collect::<Vec<_>>(),
            "revisions": self.rails.iter().map(|r| r.revision).collect::<Vec<_>>(),
            "confirmed_vset": self.rails.iter().map(|r| r.confirmed.map(float)).collect::<Vec<_>>(),
            "offset_target": self.offset_expected, "pending_windows": self.pending.len()})
    }

    fn reply(&mut self) -> Value {
        let reply = self.snapshot();
        self.updates.clear();
        reply
    }

    fn result(&self, start: usize, count: usize) -> Value {
        let end = start.saturating_add(count).min(self.points.len());
        let points = &self.points[start.min(end)..end];
        let mut validation = json!({"status": self.status, "error": self.error,
            "point_status": points.iter().map(|p| p.status.clone()).collect::<Vec<_>>(),
            "rail_v": points.iter().map(|p| floats(&p.volts)).collect::<Vec<_>>(),
            "rail_i": points.iter().map(|p| floats(&p.currents)).collect::<Vec<_>>(),
            "window_start": points.iter().map(|p| float(p.start)).collect::<Vec<_>>(),
            "window_end": points.iter().map(|p| float(p.end)).collect::<Vec<_>>(),
            "samples": points.iter().map(|p| vec![p.samples]).collect::<Vec<_>>(),
            "finite_samples": points.iter().map(|p| vec![p.finite_samples]).collect::<Vec<_>>()});
        if self.frozen.offset.is_some() {
            validation["offset_v"] = json!(
                points
                    .iter()
                    .map(|p| float(p.offset.unwrap().0))
                    .collect::<Vec<_>>()
            );
            validation["offset_target"] = json!(
                points
                    .iter()
                    .map(|p| float(p.offset.unwrap().1))
                    .collect::<Vec<_>>()
            );
        }
        json!({"session": self.session, "start": start, "count": points.len(), "total": self.steps.len(),
            "steps": floats(&self.steps[start.min(end)..end]), "data": points.iter().map(|p| float(p.mean)).collect::<Vec<_>>(),
            "validation": validation, "metadata": self.metadata, "continuous": self.settings.mode == CONTINUOUS})
    }

    fn raw(&self, start: usize, count: usize) -> Value {
        let mut data = serde_json::Map::new();
        let mut lengths = serde_json::Map::new();
        macro_rules! page {
            ($name:ident, $convert:expr) => {{
                let items = &self.raw.$name;
                let end = start.saturating_add(count).min(items.len());
                data.insert(
                    stringify!($name).to_owned(),
                    Value::Array(items[start.min(end)..end].iter().map($convert).collect()),
                );
                lengths.insert(stringify!($name).to_owned(), json!(items.len()));
            }};
        }
        page!(command_start, |v| float(*v));
        page!(command_end, |v| float(*v));
        page!(detector_time, |v| float(*v));
        page!(detector_current, |v| float(*v));
        page!(psu_time, |v| float(*v));
        page!(psu_v, |v: &Vec<f64>| floats(v));
        page!(psu_i, |v: &Vec<f64>| floats(v));
        page!(psu_target, |v: &Vec<f64>| floats(v));
        page!(psu_ready, |v| json!(v));
        if self.frozen.offset.is_some() {
            page!(offset_v, |v| float(*v));
            page!(offset_target, |v| float(*v));
        }
        json!({"start": start, "count": count, "lengths": lengths, "data": data})
    }
}

pub struct Controller {
    run: Option<Run>,
    session: u64,
    max_raw: usize,
}

impl Controller {
    pub fn new(config: &Value) -> Result<Self> {
        let max_raw = match config.get("max_raw_samples") {
            None => 1_000_000,
            Some(v) => v
                .as_u64()
                .filter(|n| *n > 0 && *n <= 10_000_000)
                .ok_or_else(|| {
                    Error::argument("max_raw_samples must be an integer in 1..=10,000,000")
                })? as usize,
        };
        Ok(Self {
            run: None,
            session: 0,
            max_raw,
        })
    }

    fn active(&self) -> Result<&Run> {
        self.run
            .as_ref()
            .ok_or_else(|| Error::runtime("No scan has been prepared / started."))
    }
    fn active_mut(&mut self) -> Result<&mut Run> {
        self.run
            .as_mut()
            .ok_or_else(|| Error::runtime("No scan has been prepared / started."))
    }
    fn snapshot(&self) -> Value {
        self.run.as_ref().map_or(
            json!({"state": "idle", "status": "idle", "action": null, "updates": []}),
            Run::snapshot,
        )
    }
}

fn argument<'a>(
    args: &'a [Value],
    kwargs: &'a Value,
    index: usize,
    name: &str,
) -> Result<&'a Value> {
    if args.get(index).is_some() && kwargs.get(name).is_some() {
        return Err(Error::argument(format!("Multiple values for {name}")));
    }
    args.get(index)
        .or_else(|| kwargs.get(name))
        .ok_or_else(|| Error::argument(format!("Missing {name}")))
}

fn optional<'a>(
    args: &'a [Value],
    kwargs: &'a Value,
    index: usize,
    name: &str,
) -> Option<&'a Value> {
    args.get(index)
        .or_else(|| kwargs.get(name))
        .filter(|v| !v.is_null())
}

fn page(args: &[Value], kwargs: &Value) -> Result<(usize, usize)> {
    let start = optional(args, kwargs, 0, "start").map_or(Ok(0), |v| {
        v.as_u64()
            .ok_or_else(|| Error::argument("start must be a nonnegative integer"))
    })?;
    let count = optional(args, kwargs, 1, "count").map_or(Ok(1000), |v| {
        v.as_u64()
            .filter(|n| *n > 0 && *n <= PAGE_LIMIT as u64)
            .ok_or_else(|| Error::argument("count must be an integer in 1..=4096"))
    })?;
    Ok((
        usize::try_from(start).map_err(|_| Error::argument("start is too large"))?,
        count as usize,
    ))
}

impl Backend for Controller {
    fn call(
        &mut self,
        method: &str,
        args: &[Value],
        kwargs: &Value,
        ctx: &Context,
    ) -> Result<Value> {
        if !matches!(
            method,
            "cancel" | "abort" | "snapshot" | "result" | "raw" | "reset" | "action_failed"
        ) && let Err(error) = ctx.check_cancelled()
        {
            if let Some(run) = &mut self.run {
                run.terminate(
                    if ctx.is_cancelled() {
                        "stopped"
                    } else {
                        "error"
                    },
                    &error.message,
                    None,
                );
            }
            return Err(error);
        }
        match method {
            "amplitude_steps" => {
                let get =
                    |i, name| number(argument(args, kwargs, i, name)?).map_err(Error::argument);
                Ok(floats(&amplitude_steps(
                    get(0, "start")?,
                    get(1, "stop")?,
                    get(2, "step")?,
                )?))
            }
            "scan_steps" => Ok(floats(
                &parse::<Settings>(argument(args, kwargs, 0, "settings")?, "settings")?.steps()?,
            )),
            "prepare" | "start" => {
                if method == "start" && self.run.is_some() {
                    return Err(Error::runtime("Reset before starting a new scan."));
                }
                let settings = parse(argument(args, kwargs, 0, "settings")?, "settings")?;
                let obs: Observation =
                    parse(argument(args, kwargs, 1, "observation")?, "observation")?;
                let session = self
                    .session
                    .checked_add(1)
                    .ok_or_else(|| Error::runtime("MScan session counter exhausted"))?;
                let mut run = Run::prepare(settings, obs.clone(), session, self.max_raw)?;
                ctx.check_cancelled()?;
                if method == "prepare" {
                    return Ok(
                        json!({"steps": floats(&run.steps), "metadata": run.metadata, "ready": true}),
                    );
                }
                run.issue(&obs, false, None, None)?;
                ctx.check_cancelled()?;
                let reply = run.reply();
                self.run = Some(run);
                self.session = session;
                Ok(reply)
            }
            "advance" => {
                let parsed = (|| {
                    Ok((
                        parse::<Observation>(
                            argument(args, kwargs, 0, "observation")?,
                            "observation",
                        )?,
                        optional(args, kwargs, 1, "acknowledgement")
                            .map(|v| parse::<Acknowledgement>(v, "acknowledgement"))
                            .transpose()?,
                    ))
                })();
                let run = self.active_mut()?;
                let (result, observation) = match parsed {
                    Ok((obs, ack)) => (
                        run.advance(&obs, ack).and_then(|_| ctx.check_cancelled()),
                        Some(obs),
                    ),
                    Err(e) => (Err(e), None),
                };
                if let Err(error) = result {
                    run.terminate(
                        if ctx.is_cancelled() {
                            "stopped"
                        } else {
                            "error"
                        },
                        &error.message,
                        observation.as_ref(),
                    );
                    return Err(error);
                }
                Ok(run.reply())
            }
            "abort" => {
                let message = argument(args, kwargs, 0, "reason")?
                    .as_str()
                    .ok_or_else(|| Error::argument("Invalid abort reason"))?;
                let run = self.active_mut()?;
                run.terminate("error", message, None);
                Ok(run.reply())
            }
            "action_failed" => {
                let session = argument(args, kwargs, 0, "session")?
                    .as_u64()
                    .ok_or_else(|| Error::argument("Invalid session"))?;
                let action_id = argument(args, kwargs, 1, "action_id")?
                    .as_u64()
                    .ok_or_else(|| Error::argument("Invalid action_id"))?;
                let message = argument(args, kwargs, 2, "message")?
                    .as_str()
                    .ok_or_else(|| Error::argument("Invalid failure message"))?;
                let run = self.active_mut()?;
                if run.session != session || run.command.as_ref().is_none_or(|c| c.id != action_id)
                {
                    return Err(Error::runtime("Stale action failure report."));
                }
                run.terminate("error", message, None);
                Ok(run.reply())
            }
            "cancel" => {
                let message = optional(args, kwargs, 0, "reason")
                    .and_then(Value::as_str)
                    .unwrap_or("Scan stopped.");
                let obs = optional(args, kwargs, 1, "observation")
                    .map(|v| parse(v, "observation"))
                    .transpose()?;
                if let Some(run) = &mut self.run {
                    run.terminate("stopped", message, obs.as_ref());
                    return Ok(run.reply());
                }
                Ok(self.snapshot())
            }
            "reset" => {
                if self
                    .run
                    .as_ref()
                    .is_some_and(|r| r.phase != Phase::Finished)
                {
                    return Err(Error::runtime(
                        "Cancel the active scan before reset; no voltage restoration is implied.",
                    ));
                }
                self.run = None;
                Ok(self.snapshot())
            }
            "snapshot" => Ok(self.snapshot()),
            "result" => {
                let (start, count) = page(args, kwargs)?;
                Ok(self.active()?.result(start, count))
            }
            "raw" => {
                let (start, count) = page(args, kwargs)?;
                let run = self.active()?;
                if run.settings.mode != CONTINUOUS {
                    return Err(Error::unsupported(
                        "Raw histories are only recorded in Continuous mode.",
                    ));
                }
                Ok(run.raw(start, count))
            }
            _ => Err(Error::unsupported(format!(
                "MScan method not supported: {method}"
            ))),
        }
    }

    fn get_attribute(&self, name: &str) -> Result<Value> {
        match name {
            "state" => Ok(self.snapshot()["state"].clone()),
            "status" => Ok(self.snapshot()["status"].clone()),
            "recording" => Ok(json!(
                self.run
                    .as_ref()
                    .is_some_and(|r| r.phase != Phase::Finished)
            )),
            "finished" => Ok(json!(
                self.run.as_ref().is_none_or(|r| r.phase == Phase::Finished)
            )),
            "metadata" => Ok(self.active()?.metadata.clone()),
            "steps" => Ok(floats(&self.active()?.steps)),
            _ => Err(Error::new(
                "AttributeError",
                format!("Unknown MScan attribute: {name}"),
            )),
        }
    }

    fn set_attribute(&mut self, name: &str, value: Value) -> Result<()> {
        match (name, value.as_bool()) {
            ("recording", Some(false)) => {
                if let Some(run) = &mut self.run {
                    run.terminate("stopped", "Scan stopped.", None);
                }
                Ok(())
            }
            ("recording", Some(true)) => Err(Error::runtime(
                "Use start(settings, observation) to start a validated scan.",
            )),
            _ => Err(Error::new(
                "AttributeError",
                format!("Read-only or unknown MScan attribute: {name}"),
            )),
        }
    }
}
