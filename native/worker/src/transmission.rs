//! Transmission scan engine. Hardware is accessible only through acknowledged
//! Explorer actions; an instrument fault retires the plan without another write.
//!
//! The numerical port uses nalgebra and bounded argmin Nelder-Mead fits, not
//! scipy L-BFGS-B. Seeded PCG box/local candidates replace scrambled Sobol points.
//! Safety, scoring, stage order, and paired verification follow _engine.py.

use crate::{
    Backend,
    context::Context,
    error::{Error, Result},
};
use argmin::{
    core::{CostFunction, Executor, State},
    solver::neldermead::NelderMead,
};
use nalgebra::{DMatrix, DVector, Dyn, linalg::Cholesky};
use rand::{RngExt, SeedableRng};
use rand_pcg::Pcg64Mcg;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::collections::{BTreeMap, BTreeSet, VecDeque};
use std::time::Instant;

type Values = BTreeMap<String, f64>;
type Samples = BTreeMap<String, [f64; 3]>;
const MAX_STAGE_POINTS: usize = 256;
const MAX_TOTAL_POINTS: usize = 4096;
const MAX_CHANNELS: usize = 128;
const MAX_ACTIONS: u64 = 2_000_000;

fn config_error(message: impl Into<String>) -> Error {
    Error::new("ConfigError", message)
}
fn instrument_error(message: impl Into<String>) -> Error {
    Error::new("InstrumentError", message)
}
fn telemetry(value: &Value) -> Result<f64> {
    if value.is_null() {
        return Ok(f64::NAN);
    }
    if let Some(n) = value.as_f64() {
        return Ok(n);
    }
    match value.get("$float").and_then(Value::as_str) {
        Some("nan") => Ok(f64::NAN),
        Some("inf") => Ok(f64::INFINITY),
        Some("-inf") => Ok(f64::NEG_INFINITY),
        _ => Err(instrument_error("Malformed numeric observation")),
    }
}
fn vf(n: f64) -> Value {
    crate::codec::float(n)
}
fn values_json(values: &Values) -> Value {
    Value::Object(values.iter().map(|(n, v)| (n.clone(), vf(*v))).collect())
}
fn samples_json(values: &Samples) -> Value {
    Value::Object(
        values
            .iter()
            .map(|(n, v)| (n.clone(), json!([vf(v[0]), vf(v[1]), v[2] as u64])))
            .collect(),
    )
}
fn names_union(names: impl IntoIterator<Item = String>) -> Vec<String> {
    let mut seen = BTreeSet::new();
    names
        .into_iter()
        .filter(|n| seen.insert(n.clone()))
        .collect()
}
fn valid_name(name: &str) -> bool {
    !name.trim().is_empty() && name == name.trim()
}
fn finite_positive(value: f64, name: &str, zero: bool) -> Result<()> {
    if !value.is_finite() || value < 0.0 || (!zero && value == 0.0) {
        return Err(config_error(format!(
            "{name} must be finite and {}",
            if zero { "nonnegative" } else { "positive" }
        )));
    }
    Ok(())
}
fn validate_names(names: &[String], what: &str, empty: bool) -> Result<()> {
    if (!empty && names.is_empty())
        || names.iter().any(|n| !valid_name(n))
        || names.iter().collect::<BTreeSet<_>>().len() != names.len()
    {
        return Err(config_error(format!(
            "{what} needs distinct nonempty channel names"
        )));
    }
    Ok(())
}
fn validate_gains(channels: &Values) -> Result<()> {
    if channels.is_empty()
        || channels
            .iter()
            .any(|(n, v)| !valid_name(n) || !v.is_finite() || *v == 0.0)
    {
        return Err(config_error(
            "channels must map channel names to finite nonzero gains",
        ));
    }
    Ok(())
}
fn floats(value: &Value, names: &[String], what: &str) -> Result<Values> {
    let object = value
        .as_object()
        .ok_or_else(|| instrument_error(format!("Missing {what} observations")))?;
    names
        .iter()
        .map(|n| {
            let v = object
                .get(n)
                .ok_or_else(|| instrument_error(format!("{n}: missing {what}")))?;
            Ok((n.clone(), telemetry(v)?))
        })
        .collect()
}
fn optional_floats(value: &Value, names: &[String], what: &str) -> Result<Values> {
    let object = value
        .as_object()
        .ok_or_else(|| instrument_error(format!("Missing {what} observations")))?;
    names
        .iter()
        .map(|n| {
            Ok((
                n.clone(),
                match object.get(n) {
                    Some(v) => telemetry(v)?,
                    None => f64::NAN,
                },
            ))
        })
        .collect()
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(default, deny_unknown_fields)]
pub struct Options {
    pub polarity: i32,
    pub strategy: String,
    pub settle_s: f64,
    pub average_s: f64,
    pub readback_tolerance: f64,
    pub settle_timeout_s: f64,
    pub min_current: f64,
    pub lost_fraction: f64,
    pub verify_pairs: usize,
    pub significance: f64,
    pub max_current: f64,
    pub reference_every: usize,
    pub poll_s: f64,
    pub seed: Option<i64>,
}
impl Default for Options {
    fn default() -> Self {
        Self {
            polarity: 1,
            strategy: "bayesian".into(),
            settle_s: 1.0,
            average_s: 2.0,
            readback_tolerance: 0.5,
            settle_timeout_s: 20.0,
            min_current: 1e-13,
            lost_fraction: 0.2,
            verify_pairs: 3,
            significance: 2.0,
            max_current: 1e-6,
            reference_every: 8,
            poll_s: 0.1,
            seed: None,
        }
    }
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Knob {
    pub name: String,
    pub channels: Values,
    pub window: [f64; 2],
    pub max_step: f64,
    #[serde(default)]
    pub relative: bool,
    #[serde(default = "default_floor")]
    pub floor: f64,
    // Preserve Python's first channel for relative scaling, regardless of map order.
    #[serde(default)]
    pub scale_channel: Option<String>,
}
fn default_floor() -> f64 {
    10.0
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Stage {
    pub name: String,
    pub knobs: Vec<Knob>,
    #[serde(default)]
    pub aperture: Option<String>,
    pub downstream: Vec<String>,
    #[serde(default)]
    pub normalize_by: Vec<String>,
    #[serde(default)]
    pub budget: Option<usize>,
    #[serde(default)]
    pub peak: bool,
}
impl Stage {
    fn budget(&self) -> usize {
        self.budget
            .unwrap_or((if self.peak { 8 } else { 12 }) * self.knobs.len() + 16)
    }
    fn channels(&self) -> Vec<String> {
        self.knobs
            .iter()
            .flat_map(|k| k.channels.keys().cloned())
            .collect::<BTreeSet<_>>()
            .into_iter()
            .collect()
    }
    fn currents(&self) -> Vec<String> {
        names_union(
            self.aperture
                .iter()
                .cloned()
                .chain(self.downstream.clone())
                .chain(self.normalize_by.clone()),
        )
    }
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Target {
    pub channels: Values,
    pub center: f64,
    pub width: f64,
    pub measure: Vec<String>,
    #[serde(default = "default_points")]
    pub points: usize,
    #[serde(default)]
    pub track: Option<f64>,
    #[serde(default)]
    pub max_step: Option<f64>,
    #[serde(default = "default_spectrum_points")]
    pub spectrum_points: usize,
    #[serde(default = "default_spectrum_span")]
    pub spectrum_span: f64,
    #[serde(default)]
    pub filter_name: String,
    #[serde(default)]
    pub amplitude_channel: Option<String>,
}
fn default_points() -> usize {
    5
}
fn default_spectrum_points() -> usize {
    21
}
fn default_spectrum_span() -> f64 {
    3.0
}
impl Target {
    fn track(&self) -> f64 {
        self.track.unwrap_or(2.0 * self.width)
    }
    fn step(&self) -> f64 {
        self.max_step.unwrap_or(self.width / 2.0)
    }
    fn values(&self, amplitude: f64) -> Values {
        self.channels
            .iter()
            .map(|(n, g)| (n.clone(), g * amplitude))
            .collect()
    }
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Config {
    #[serde(default)]
    pub stages: Vec<Stage>,
    #[serde(default)]
    pub pressure: BTreeMap<String, [f64; 2]>,
    #[serde(default)]
    pub target: Option<Target>,
    #[serde(default)]
    pub options: Options,
    #[serde(default)]
    pub selected_stages: Option<Vec<String>>,
}
impl Config {
    fn currents(&self) -> Vec<String> {
        names_union(
            self.stages
                .iter()
                .flat_map(Stage::currents)
                .chain(self.target.iter().flat_map(|t| t.measure.clone())),
        )
    }
    fn driven(&self) -> Vec<String> {
        names_union(
            self.stages
                .iter()
                .flat_map(Stage::channels)
                .chain(self.target.iter().flat_map(|t| t.channels.keys().cloned())),
        )
    }
    fn validate(&self) -> Result<()> {
        let o = &self.options;
        if ![1, -1].contains(&o.polarity) {
            return Err(config_error("polarity must be 1 or -1"));
        }
        if !["bayesian", "coordinate"].contains(&o.strategy.as_str()) {
            return Err(config_error("Unknown strategy"));
        }
        for (n, v, z) in [
            ("settle_s", o.settle_s, true),
            ("average_s", o.average_s, false),
            ("readback_tolerance", o.readback_tolerance, false),
            ("settle_timeout_s", o.settle_timeout_s, false),
            ("min_current", o.min_current, false),
            ("max_current", o.max_current, false),
            ("significance", o.significance, false),
            ("poll_s", o.poll_s, false),
        ] {
            finite_positive(v, n, z)?;
        }
        if !o.lost_fraction.is_finite() || !(0.0..1.0).contains(&o.lost_fraction) {
            return Err(config_error("lost_fraction must be in [0, 1)"));
        }
        if !(2..=32).contains(&o.verify_pairs) || o.reference_every < 2 {
            return Err(config_error(
                "verify_pairs must be 2..32 and reference_every must be >= 2",
            ));
        }
        if self.stages.len() > 64
            || self
                .stages
                .iter()
                .map(Stage::budget)
                .fold(0_usize, usize::saturating_add)
                > MAX_TOTAL_POINTS
        {
            return Err(config_error(
                "Native plan exceeds bounded stage/evaluation capacity",
            ));
        }
        let mut stage_names = BTreeSet::new();
        for stage in &self.stages {
            if !valid_name(&stage.name)
                || !stage_names.insert(&stage.name)
                || stage.knobs.is_empty()
                || stage.knobs.len() > 32
            {
                return Err(config_error("Stages need unique names and 1..32 knobs"));
            }
            validate_names(&stage.downstream, "downstream", false)?;
            validate_names(&stage.normalize_by, "normalize_by", true)?;
            if let Some(n) = &stage.aperture
                && (!valid_name(n) || stage.downstream.contains(n))
            {
                return Err(config_error("The aperture cannot also be downstream"));
            }
            let minimum = 2 * stage.knobs.len() + 6 + 2 * o.verify_pairs;
            if stage.budget() < minimum || stage.budget() > MAX_STAGE_POINTS {
                return Err(config_error(format!(
                    "{}: budget must be {minimum}..{MAX_STAGE_POINTS}",
                    stage.name
                )));
            }
            let mut driven = BTreeSet::new();
            let mut knob_names = BTreeSet::new();
            for k in &stage.knobs {
                validate_gains(&k.channels)?;
                if !valid_name(&k.name) || !knob_names.insert(&k.name) {
                    return Err(config_error("Knob names must be unique text"));
                }
                if k.channels.keys().any(|n| !driven.insert(n)) {
                    return Err(config_error("A channel is driven by two knobs"));
                }
                if k.window.iter().any(|v| !v.is_finite())
                    || !(k.window[0] <= 0.0 && k.window[1] >= 0.0 && k.window[0] < k.window[1])
                {
                    return Err(config_error("Knob window must be finite and contain 0"));
                }
                finite_positive(k.max_step, "max_step", false)?;
                finite_positive(k.floor, "floor", false)?;
                if k.relative && (k.window[1] - k.window[0] > 4.0 || k.max_step > 1.0) {
                    return Err(config_error(
                        "Relative windows/steps are fractions of the start",
                    ));
                }
                if k.scale_channel
                    .as_ref()
                    .is_some_and(|n| !k.channels.contains_key(n))
                {
                    return Err(config_error("Unknown relative scale channel"));
                }
            }
            if stage.peak && self.target.is_none() {
                return Err(config_error("Peak stages need a target"));
            }
        }
        if let Some(selected) = &self.selected_stages {
            validate_names(selected, "selected stages", false)?;
            if selected
                .iter()
                .any(|n| !self.stages.iter().any(|s| &s.name == n))
            {
                return Err(config_error("No stage with the selected name"));
            }
        }
        if let Some(t) = &self.target {
            validate_gains(&t.channels)?;
            validate_names(&t.measure, "target measure", false)?;
            finite_positive(t.center, "target center", true)?;
            finite_positive(t.width, "target width", false)?;
            finite_positive(t.track(), "target track", false)?;
            finite_positive(t.step(), "target max_step", false)?;
            finite_positive(t.spectrum_span, "spectrum_span", false)?;
            if !(3..=15).contains(&t.points)
                || t.points % 2 == 0
                || (t.spectrum_points != 0 && !(5..=4097).contains(&t.spectrum_points))
            {
                return Err(config_error(
                    "Target needs odd 3..15 points; spectrum_points must be 0 or 5..4097",
                ));
            }
            if t.amplitude_channel
                .as_ref()
                .is_some_and(|n| !t.channels.contains_key(n))
            {
                return Err(config_error("Unknown amplitude channel"));
            }
            if self
                .stages
                .iter()
                .flat_map(Stage::channels)
                .any(|c| t.channels.contains_key(&c))
            {
                return Err(config_error("A stage cannot drive the tracked mass filter"));
            }
        }
        for (name, pair) in &self.pressure {
            if !valid_name(name)
                || pair.iter().any(|v| !v.is_finite())
                || pair[0] < 0.0
                || pair[0] >= pair[1]
            {
                return Err(config_error("Pressure needs 0 <= minimum < maximum"));
            }
        }
        let currents = self.currents();
        let driven = self.driven();
        if currents.iter().any(|n| self.pressure.contains_key(n)) {
            return Err(config_error(
                "A channel cannot be both current and pressure",
            ));
        }
        if driven
            .iter()
            .any(|n| currents.contains(n) || self.pressure.contains_key(n))
        {
            return Err(config_error("A channel cannot be both driven and measured"));
        }
        if names_union(
            driven
                .into_iter()
                .chain(currents)
                .chain(self.pressure.keys().cloned()),
        )
        .len()
            > MAX_CHANNELS
        {
            return Err(config_error("Native plan exceeds 128 channels"));
        }
        Ok(())
    }
    fn stage_indices(&self) -> Vec<usize> {
        match &self.selected_stages {
            Some(selected) => selected
                .iter()
                .map(|n| self.stages.iter().position(|s| &s.name == n).unwrap())
                .collect(),
            None => (0..self.stages.len()).collect(),
        }
    }
}

fn mean(x: &[f64]) -> f64 {
    x.iter().sum::<f64>() / x.len() as f64
}
fn sem(x: &[f64]) -> f64 {
    let m = mean(x);
    (x.iter().map(|v| (v - m).powi(2)).sum::<f64>() / (x.len() - 1) as f64 / x.len() as f64).sqrt()
}
fn linspace(lo: f64, hi: f64, count: usize) -> Vec<f64> {
    (0..count)
        .map(|i| lo + (hi - lo) * i as f64 / (count - 1).max(1) as f64)
        .collect()
}

// Center and scale the polynomial basis before SVD; current magnitudes can be pA.
fn quadratic(x: &[f64], y: &[f64]) -> Option<[f64; 3]> {
    if x.len() != y.len() || x.len() < 3 {
        return None;
    }
    let mid = mean(x);
    let scale = x.iter().map(|v| (v - mid).abs()).fold(0.0_f64, f64::max);
    if scale == 0.0 {
        return None;
    }
    let a = DMatrix::from_fn(x.len(), 3, |i, j| {
        let u = (x[i] - mid) / scale;
        match j {
            0 => u * u,
            1 => u,
            _ => 1.0,
        }
    });
    let svd = a.svd(true, true);
    let p = svd.solve(&DVector::from_column_slice(y), 1e-12).ok()?;
    let a = p[0] / scale.powi(2);
    let b = p[1] / scale - 2.0 * a * mid;
    Some([a, b, p[2] - p[1] * mid / scale + a * mid.powi(2)])
}

pub fn fit_peak(amplitudes: &[f64], currents: &[f64], sems: &[f64]) -> Result<(f64, f64, f64)> {
    if amplitudes.is_empty()
        || amplitudes.len() != currents.len()
        || currents.len() != sems.len()
        || amplitudes.iter().chain(currents).any(|v| !v.is_finite())
    {
        return Err(Error::argument(
            "Peak arrays need equal nonempty finite amplitudes/currents",
        ));
    }
    let floor = (sems
        .iter()
        .map(|v| if v.is_finite() { v * v } else { 0.0 })
        .sum::<f64>()
        / sems.len() as f64)
        .sqrt();
    let best = (0..currents.len())
        .max_by(|a, b| currents[*a].total_cmp(&currents[*b]))
        .unwrap();
    let mid = mean(amplitudes);
    let x: Vec<f64> = amplitudes.iter().map(|a| a - mid).collect();
    if let Some([a, b, c]) = quadratic(&x, currents) {
        let vertex = -b / (2.0 * a);
        let lo = x.iter().copied().fold(f64::INFINITY, f64::min);
        let hi = x.iter().copied().fold(f64::NEG_INFINITY, f64::max);
        if a < 0.0 && vertex >= lo && vertex <= hi {
            let spread = (x
                .iter()
                .zip(currents)
                .map(|(x, y)| (y - (a * x * x + b * x + c)).powi(2))
                .sum::<f64>()
                / currents.len().saturating_sub(3).max(1) as f64)
                .sqrt();
            return Ok((
                c - b * b / (4.0 * a),
                floor.hypot(spread / (x.len() as f64).sqrt()),
                mid + vertex,
            ));
        }
    }
    Ok((currents[best], floor, amplitudes[best]))
}

pub fn pick_peak(amplitudes: &[f64], currents: &[f64], near: f64) -> Result<(f64, f64)> {
    if amplitudes.len() != currents.len() || !near.is_finite() {
        return Err(Error::argument("Malformed spectrum"));
    }
    let mut pairs: Vec<_> = amplitudes
        .iter()
        .zip(currents)
        .filter(|(a, c)| a.is_finite() && c.is_finite())
        .map(|(a, c)| (*a, *c))
        .collect();
    pairs.sort_by(|a, b| a.0.total_cmp(&b.0));
    if pairs.len() < 3 {
        return Err(Error::argument("The spectrum needs at least three points"));
    }
    if pairs.windows(2).any(|p| p[0].0 >= p[1].0) {
        return Err(Error::argument("Spectrum amplitudes must be distinct"));
    }
    let a: Vec<_> = pairs.iter().map(|p| p.0).collect();
    let raw: Vec<_> = pairs.iter().map(|p| p.1).collect();
    let c: Vec<_> = (0..a.len())
        .map(|i| {
            if a.len() >= 7 {
                (raw[i.saturating_sub(1)] + raw[i] + raw[(i + 1).min(a.len() - 1)]) / 3.0
            } else {
                raw[i]
            }
        })
        .collect();
    let base = |i: usize| {
        let mut sides = Vec::new();
        for direction in [-1_isize, 1] {
            if !(0..a.len() as isize).contains(&(i as isize + direction)) {
                continue;
            }
            let mut j = i as isize;
            let mut low = c[i];
            while (0..a.len() as isize).contains(&(j + direction))
                && c[(j + direction) as usize] <= c[i]
            {
                j += direction;
                low = low.min(c[j as usize]);
            }
            sides.push(low);
        }
        sides.into_iter().fold(f64::NEG_INFINITY, f64::max)
    };
    let span = c.iter().copied().fold(f64::NEG_INFINITY, f64::max)
        - c.iter().copied().fold(f64::INFINITY, f64::min);
    let i = (0..a.len())
        .filter(|&i| {
            c[i] > 0.0
                && (i == 0 || c[i] >= c[i - 1])
                && (i + 1 == c.len() || c[i] >= c[i + 1])
                && span > 0.0
                && c[i] - base(i) >= 0.05 * span
        })
        .min_by(|i, j| (a[*i] - near).abs().total_cmp(&(a[*j] - near).abs()))
        .ok_or_else(|| Error::argument("No positive peak in the spectrum"))?;
    let mut center = a[i];
    if i > 0 && i + 1 < a.len() {
        let x: Vec<_> = a[i - 1..=i + 1].iter().map(|v| v - a[i]).collect();
        if let Some([curvature, slope, _]) = quadratic(&x, &c[i - 1..=i + 1])
            && curvature < 0.0
        {
            center = (a[i] - slope / (2.0 * curvature)).clamp(a[i - 1], a[i + 1]);
        }
    }
    let half = 0.5 * (c[i] + base(i).max(0.0));
    let mut widths = Vec::new();
    for direction in [-1_isize, 1] {
        let mut j = i as isize;
        while (0..a.len() as isize).contains(&(j + direction))
            && c[(j + direction) as usize] >= half
        {
            j += direction;
        }
        if !(0..a.len() as isize).contains(&(j + direction)) {
            continue;
        }
        let k = (j + direction) as usize;
        let j = j as usize;
        let crossing = if c[k] != c[j] {
            a[j] + (half - c[j]) * (a[k] - a[j]) / (c[k] - c[j])
        } else {
            a[k]
        };
        widths.push((crossing - center).abs());
    }
    let width = if widths.is_empty() {
        0.5 * (a[a.len() - 1] - a[0])
    } else {
        mean(&widths)
    };
    let mut steps: Vec<_> = a.windows(2).map(|p| p[1] - p[0]).collect();
    steps.sort_by(f64::total_cmp);
    let middle = steps.len() / 2;
    let step = if steps.len() % 2 == 0 {
        0.5 * (steps[middle - 1] + steps[middle])
    } else {
        steps[middle]
    };
    Ok((center, width.max(step)))
}

#[derive(Clone)]
struct Record {
    delta: Vec<f64>,
    time: f64,
    objective: f64,
    objective_sem: f64,
    model: f64,
    incident: f64,
    transmitted: f64,
    valid: bool,
    json: Value,
}

fn matern(a: &[f64], b: &[f64], scales: &[f64]) -> f64 {
    let r = a
        .iter()
        .zip(b)
        .zip(scales)
        .map(|((a, b), s)| ((a - b) / s).powi(2))
        .sum::<f64>()
        .max(0.0)
        .sqrt()
        * 5.0_f64.sqrt();
    (1.0 + r + r * r / 3.0) * (-r).exp()
}

struct Likelihood {
    x: Vec<Vec<f64>>,
    y: DVector<f64>,
    noise: Vec<f64>,
    bounds: Vec<(f64, f64)>,
    ctx: Context,
}
impl Likelihood {
    fn theta(&self, param: &[f64]) -> Vec<f64> {
        // A logistic transform enforces finite log-parameter bounds on every evaluation.
        param
            .iter()
            .zip(&self.bounds)
            .map(|(p, (lo, hi))| lo + (hi - lo) / (1.0 + (-p.clamp(-36.0, 36.0)).exp()))
            .collect()
    }
    fn factor(&self, theta: &[f64]) -> Option<Cholesky<f64, Dyn>> {
        let dims = self.x[0].len();
        let scales: Vec<_> = theta[..dims].iter().map(|v| v.exp()).collect();
        let signal = theta[dims].exp();
        let floor = theta[dims + 1].exp();
        DMatrix::from_fn(self.x.len(), self.x.len(), |i, j| {
            signal * matern(&self.x[i], &self.x[j], &scales)
                + if i == j {
                    self.noise[i] + floor + 1e-10
                } else {
                    0.0
                }
        })
        .cholesky()
    }
}
impl CostFunction for Likelihood {
    type Param = Vec<f64>;
    type Output = f64;
    fn cost(&self, param: &Self::Param) -> std::result::Result<f64, argmin::core::Error> {
        // Nelder-Mead unwraps cost errors while sorting. A finite penalty skips
        // further matrix work; fit() propagates cancellation after the bounded solve.
        if self.ctx.check_cancelled().is_err() {
            return Ok(1e25);
        }
        let theta = self.theta(param);
        let Some(cholesky) = self.factor(&theta) else {
            return Ok(1e25);
        };
        let alpha = cholesky.solve(&self.y);
        let cost = 0.5 * self.y.dot(&alpha)
            + cholesky.l().diagonal().iter().map(|v| v.ln()).sum::<f64>()
            + 0.5 * self.y.len() as f64 * (2.0 * std::f64::consts::PI).ln();
        Ok(if cost.is_finite() { cost } else { 1e25 })
    }
}

struct GaussianProcess {
    x: Vec<Vec<f64>>,
    factor: Cholesky<f64, Dyn>,
    alpha: DVector<f64>,
    theta: Vec<f64>,
    scales: Vec<f64>,
    signal: f64,
    floor: f64,
    y_mean: f64,
    y_scale: f64,
}
impl GaussianProcess {
    fn fit(
        x: Vec<Vec<f64>>,
        y: &[f64],
        noise: Vec<f64>,
        rng: &mut Pcg64Mcg,
        previous: Option<&[f64]>,
        restarts: usize,
        ctx: &Context,
    ) -> Result<Self> {
        let y_mean = mean(y);
        let y_scale = (y.iter().map(|v| (v - y_mean).powi(2)).sum::<f64>() / y.len() as f64).sqrt();
        let y_scale = if y_scale > 0.0 { y_scale } else { 1.0 };
        let dims = x[0].len();
        let mut bounds = vec![(0.03_f64.ln(), 3.0_f64.ln()); dims - 1];
        bounds.extend([
            (0.05_f64.ln(), 100.0_f64.ln()),
            (0.05_f64.ln(), 20.0_f64.ln()),
            (1e-6_f64.ln(), 1.0_f64.ln()),
        ]);
        let default: Vec<_> = bounds
            .iter()
            .enumerate()
            .map(|(i, (lo, hi))| {
                if i == dims {
                    0.0
                } else if i == dims + 1 {
                    1e-2_f64.ln()
                } else {
                    (lo + hi) / 2.0
                }
            })
            .collect();
        let mut starts = vec![previous.unwrap_or(&default).to_vec()];
        for _ in 0..restarts {
            starts.push(
                bounds
                    .iter()
                    .map(|(lo, hi)| rng.random_range(*lo..*hi))
                    .collect(),
            );
        }
        let normalized = DVector::from_iterator(y.len(), y.iter().map(|v| (v - y_mean) / y_scale));
        let noise: Vec<_> = noise.iter().map(|v| v / y_scale.powi(2)).collect();
        let mut best: Option<(f64, Vec<f64>)> = None;
        for theta in starts {
            ctx.check_cancelled()?;
            let param: Vec<_> = theta
                .iter()
                .zip(&bounds)
                .map(|(t, (lo, hi))| {
                    let p = ((t - lo) / (hi - lo)).clamp(1e-8, 1.0 - 1e-8);
                    (p / (1.0 - p)).ln()
                })
                .collect();
            let mut simplex = vec![param.clone()];
            for i in 0..param.len() {
                let mut p = param.clone();
                p[i] += 0.5;
                simplex.push(p);
            }
            let problem = Likelihood {
                x: x.clone(),
                y: normalized.clone(),
                noise: noise.clone(),
                bounds: bounds.clone(),
                ctx: ctx.clone(),
            };
            let solver = NelderMead::new(simplex)
                .with_sd_tolerance(1e-7)
                .map_err(|e| Error::runtime(e.to_string()))?;
            let result = Executor::new(problem, solver)
                .configure(|s| s.max_iters(100))
                .run();
            ctx.check_cancelled()?;
            let result =
                result.map_err(|e| Error::runtime(format!("Gaussian process fit failed: {e}")))?;
            let param = result
                .state
                .get_best_param()
                .ok_or_else(|| Error::runtime("No fitted GP parameters"))?;
            let theta: Vec<_> = param
                .iter()
                .zip(&bounds)
                .map(|(p, (lo, hi))| lo + (hi - lo) / (1.0 + (-p.clamp(-36.0, 36.0)).exp()))
                .collect();
            if best
                .as_ref()
                .is_none_or(|(cost, _)| result.state.get_best_cost() < *cost)
            {
                best = Some((result.state.get_best_cost(), theta));
            }
        }
        let theta = best.ok_or_else(|| Error::runtime("No GP fit attempted"))?.1;
        let problem = Likelihood {
            x: x.clone(),
            y: normalized,
            noise,
            bounds,
            ctx: ctx.clone(),
        };
        let factor = problem
            .factor(&theta)
            .ok_or_else(|| Error::runtime("GP covariance is not positive definite"))?;
        let alpha = factor.solve(&problem.y);
        Ok(Self {
            x,
            factor,
            alpha,
            scales: theta[..dims].iter().map(|v| v.exp()).collect(),
            signal: theta[dims].exp(),
            floor: theta[dims + 1].exp(),
            theta,
            y_mean,
            y_scale,
        })
    }
    fn predict(&self, point: &[f64]) -> (f64, f64) {
        let k = DVector::from_iterator(
            self.x.len(),
            self.x
                .iter()
                .map(|x| self.signal * matern(point, x, &self.scales)),
        );
        let m = k.dot(&self.alpha);
        let v = self.factor.solve(&k);
        (
            m * self.y_scale + self.y_mean,
            (self.signal - k.dot(&v)).max(1e-12).sqrt() * self.y_scale,
        )
    }
}

struct Strategy {
    name: String,
    lower: Vec<f64>,
    upper: Vec<f64>,
    steps: Vec<f64>,
    center: Vec<f64>,
    axis: usize,
    scale: f64,
    queue: VecDeque<Vec<f64>>,
    improved: bool,
    radius: f64,
    successes: usize,
    failures: usize,
    fits: usize,
    model: Option<GaussianProcess>,
    t0: Option<f64>,
    converged: bool,
    diagnostics: Value,
}
impl Strategy {
    fn new(name: &str, lower: Vec<f64>, upper: Vec<f64>, steps: Vec<f64>) -> Self {
        Self {
            name: name.into(),
            center: vec![0.0; lower.len()],
            lower,
            upper,
            steps,
            axis: 0,
            scale: 1.0,
            queue: VecDeque::new(),
            improved: false,
            radius: 2.0,
            successes: 0,
            failures: 0,
            fits: 0,
            model: None,
            t0: None,
            converged: false,
            diagnostics: json!({}),
        }
    }
    fn features(&self, delta: &[f64], time: f64) -> Vec<f64> {
        let mut p: Vec<_> = delta
            .iter()
            .enumerate()
            .map(|(i, v)| (v - self.lower[i]) / (self.upper[i] - self.lower[i]))
            .collect();
        p.push((time - self.t0.unwrap_or(time)) / 3600.0);
        p
    }
    fn incumbent(&self, history: &[Record], now: f64) -> Vec<f64> {
        if self.name == "coordinate" {
            return self.center.clone();
        }
        let valid: Vec<_> = history.iter().filter(|r| r.valid).collect();
        if valid.is_empty() {
            return vec![0.0; self.lower.len()];
        }
        if let Some(model) = &self.model {
            return valid
                .iter()
                .max_by(|a, b| {
                    model
                        .predict(&self.features(&a.delta, now))
                        .0
                        .total_cmp(&model.predict(&self.features(&b.delta, now)).0)
                })
                .unwrap()
                .delta
                .clone();
        }
        let mut groups: Vec<(Vec<f64>, Vec<f64>)> = Vec::new();
        for r in valid {
            let rounded: Vec<_> = r.delta.iter().map(|v| (v * 1e9).round() / 1e9).collect();
            if let Some(g) = groups.iter_mut().find(|g| g.0 == rounded) {
                g.1.push(r.objective);
            } else {
                groups.push((rounded, vec![r.objective]));
            }
        }
        groups
            .into_iter()
            .max_by(|a, b| mean(&a.1).total_cmp(&mean(&b.1)))
            .unwrap()
            .0
    }
    fn advance(&mut self) {
        self.axis += 1;
        if self.axis == self.lower.len() {
            self.axis = 0;
            if !self.improved {
                self.scale /= 2.0;
                self.converged = self.scale < 0.25;
            }
            self.improved = false;
        }
    }
    fn ask(
        &mut self,
        history: &[Record],
        now: f64,
        rng: &mut Pcg64Mcg,
        ctx: &Context,
    ) -> Result<Vec<f64>> {
        if self.name == "coordinate" {
            for _ in 0..2 * self.lower.len() {
                if !self.queue.is_empty() {
                    break;
                }
                for offset in [-2.0, -1.0, 1.0, 2.0] {
                    let mut p = self.center.clone();
                    p[self.axis] += offset * self.scale * self.steps[self.axis];
                    if p[self.axis] >= self.lower[self.axis]
                        && p[self.axis] <= self.upper[self.axis]
                    {
                        self.queue.push_back(p);
                    }
                }
                if self.queue.is_empty() {
                    self.advance();
                }
            }
            return self
                .queue
                .pop_front()
                .ok_or_else(|| config_error("Coordinate axis is pinned at both bounds"));
        }
        let started = Instant::now();
        self.t0.get_or_insert(history[0].time);
        let x: Vec<_> = history
            .iter()
            .map(|r| self.features(&r.delta, r.time))
            .collect();
        let y: Vec<_> = history.iter().map(|r| r.model).collect();
        let noise: Vec<_> = history
            .iter()
            .map(|r| {
                if r.objective_sem.is_finite() {
                    r.objective_sem.max(0.0).powi(2)
                } else {
                    0.0
                }
            })
            .collect();
        let previous = self.model.as_ref().map(|m| m.theta.as_slice());
        let restarts = if previous.is_none() || self.fits.is_multiple_of(5) {
            2
        } else {
            0
        };
        self.model = Some(GaussianProcess::fit(
            x, &y, noise, rng, previous, restarts, ctx,
        )?);
        self.fits += 1;
        let fit_s = started.elapsed().as_secs_f64();
        let center = self.incumbent(history, now);
        let model = self.model.as_ref().unwrap();
        let unit: Vec<_> = center
            .iter()
            .enumerate()
            .map(|(i, v)| (v - self.lower[i]) / (self.upper[i] - self.lower[i]))
            .collect();
        let half: Vec<_> = (0..unit.len())
            .map(|i| (self.radius * self.steps[i] / (self.upper[i] - self.lower[i])).min(0.5))
            .collect();
        let mut best = (f64::NEG_INFINITY, center.clone(), 0.0, 0.0);
        for k in 0..768 {
            ctx.check_cancelled()?;
            let mut p = Vec::with_capacity(unit.len());
            for i in 0..unit.len() {
                let lo = (unit[i] - half[i]).max(0.0);
                let hi = (unit[i] + half[i]).min(1.0);
                let u = if k < 512 {
                    lo + rng.random::<f64>() * (hi - lo)
                } else {
                    let normal = (-2.0 * rng.random::<f64>().max(f64::MIN_POSITIVE).ln()).sqrt()
                        * (2.0 * std::f64::consts::PI * rng.random::<f64>()).cos();
                    (unit[i] + 0.35 * normal * half[i]).clamp(lo, hi)
                };
                p.push(self.lower[i] + u * (self.upper[i] - self.lower[i]));
            }
            let (m, s) = model.predict(&self.features(&p, now));
            let score = m + 2.0 * s;
            if score > best.0 {
                best = (score, p, m, s);
            }
        }
        self.diagnostics = json!({"incumbent":center,"predicted":best.2,"predicted_std":best.3,"ucb":best.0,
            "fit_s":fit_s,"ask_s":started.elapsed().as_secs_f64(),"numerics":"nalgebra/argmin-nelder-mead/pcg"});
        Ok(best.1)
    }
    fn tell(&mut self, history: &[Record], now: f64) {
        let record = history.last().unwrap();
        if self.name == "coordinate" {
            if !self.queue.is_empty() {
                return;
            }
            let same: Vec<_> = history
                .iter()
                .filter(|r| {
                    r.valid
                        && r.delta.iter().enumerate().all(|(i, v)| {
                            i == self.axis
                                || (*v - self.center[i]).abs() <= 1e-8 + 1e-5 * self.center[i].abs()
                        })
                })
                .collect();
            if same.len() >= 3 {
                let x: Vec<_> = same.iter().map(|r| r.delta[self.axis]).collect();
                let y: Vec<_> = same.iter().map(|r| r.objective).collect();
                let distinct = x.iter().map(|v| v.to_bits()).collect::<BTreeSet<_>>().len();
                let mut best = x[(0..y.len()).max_by(|a, b| y[*a].total_cmp(&y[*b])).unwrap()];
                if distinct >= 3
                    && let Some([a, b, _]) = quadratic(&x, &y)
                    && a < 0.0
                {
                    best = -b / (2.0 * a);
                }
                let reach = 2.0 * self.scale * self.steps[self.axis];
                best = best
                    .clamp(
                        self.center[self.axis] - reach,
                        self.center[self.axis] + reach,
                    )
                    .clamp(self.lower[self.axis], self.upper[self.axis]);
                if (best - self.center[self.axis]).abs() > 1e-12 {
                    self.center[self.axis] = best;
                    self.improved = true;
                }
            }
            self.advance();
            return;
        }
        let Some(model) = &self.model else {
            return;
        };
        let reference = history[..history.len() - 1]
            .iter()
            .filter(|r| r.valid)
            .map(|r| model.predict(&self.features(&r.delta, now)).0)
            .fold(f64::NEG_INFINITY, f64::max);
        if !reference.is_finite() {
            return;
        }
        let margin = (if record.objective_sem.is_finite() {
            record.objective_sem
        } else {
            0.0
        })
        .max(1e-3 * reference.abs());
        if record.valid && record.model > reference + margin {
            self.successes += 1;
            self.failures = 0;
        } else {
            self.successes = 0;
            self.failures += 1;
        }
        if self.successes >= 3 {
            self.radius = (2.0 * self.radius).min(8.0);
            self.successes = 0;
        } else if self.failures >= 3.max(self.lower.len()) {
            self.radius /= 2.0;
            self.failures = 0;
            self.converged = self.radius < 0.5;
        }
    }
    fn diagnostics(&self) -> Value {
        let mut v = self.diagnostics.clone();
        if self.name == "coordinate" {
            return json!({"converged":self.converged,"axis":self.axis,"scale":self.scale,"center":self.center,"queued":self.queue.len(),"improved":self.improved});
        }
        v["converged"] = json!(self.converged);
        v["radius"] = json!(self.radius);
        v["successes"] = json!(self.successes);
        v["failures"] = json!(self.failures);
        v["fits"] = json!(self.fits);
        if let Some(m) = &self.model {
            v["lengthscales"] = json!(m.scales);
            v["signal"] = json!(m.signal);
            v["noise_floor"] = json!(m.floor);
            v["y_mean"] = json!(m.y_mean);
            v["y_scale"] = json!(m.y_scale);
        }
        v
    }
}

#[derive(Clone)]
struct Movement {
    start: Values,
    targets: Values,
    count: usize,
    index: usize,
    interlocks: bool,
}
#[derive(Clone)]
struct Settling {
    movement: Movement,
    point: Values,
    begin: f64,
    deadline: f64,
    polls: usize,
}
#[derive(Clone)]
struct Window {
    criterion: Vec<String>,
    channels: Vec<String>,
    attempt: usize,
    begin: f64,
    end: f64,
    deadline: f64,
}
#[derive(Clone)]
struct Measurement {
    begin: f64,
    end: f64,
    data: Samples,
}
struct Sweep {
    amplitudes: Vec<f64>,
    measure: Vec<String>,
    label: String,
    back: f64,
    began: f64,
    current: Vec<f64>,
    sem: Vec<f64>,
}
struct Peak {
    amplitudes: Vec<f64>,
    currents: Vec<f64>,
    sems: Vec<f64>,
    measure: Vec<String>,
    center_data: Option<Measurement>,
}
struct StageRun {
    stage: Stage,
    start: Values,
    lower: Vec<f64>,
    upper: Vec<f64>,
    strategy: Strategy,
    began: f64,
    history: Vec<Record>,
    reference: f64,
    since_reference: usize,
    best: Vec<f64>,
    verification: Vec<(String, Record)>,
    verdict: Value,
}

enum Task {
    Request(&'static str, Value, Reply),
    Begin,
    PrepareTarget,
    Stage(usize),
    Reference,
    Evaluate(Vec<f64>, &'static str, Option<&'static str>),
    EvaluationMeasure,
    Score(Vec<f64>, &'static str, Option<&'static str>),
    Search,
    Tell,
    VerifyCollect(&'static str),
    Decide,
    StageDone,
    Snapshot(&'static str),
    SnapshotDone(&'static str),
    Sweep(Vec<f64>, Vec<String>, String),
    SweepPoint(usize),
    SweepCollect(usize),
    SweepDone,
    PeakPoint(usize),
    PeakCollect(usize),
    Move(Values, bool),
    MoveStep(Movement),
    Settle(Settling),
    MoveDone(Settling, f64, Values),
    Pause(f64, bool),
    Measure(Vec<String>, usize),
    Window(Window),
    RecoveryDone,
    Cleanup,
    Finish,
}
enum Reply {
    Began,
    PreparedTarget,
    PreparedStage(usize),
    MoveChecked(Movement, Values),
    Commanded(Movement, Values),
    Settled(Settling),
    PauseChecked(f64, bool),
    PauseWait(f64, bool),
    SettleWait(Settling),
    MeasureOpened(Vec<String>, usize),
    WindowChecked(Window),
    WindowData(Window),
    WindowWait(Window),
    MeasureClosed(Window, Value),
    Cleanup,
}
struct Pending {
    action: Value,
    reply: Reply,
}

/// One session, one outstanding action. `observe` never accepts an old action id.
/// A stop/interlock restores only the current stage; instrument faults never write.
pub struct Controller {
    config: Config,
    rng: Pcg64Mcg,
    now: f64,
    began: f64,
    status: String,
    reason: String,
    operation: String,
    active: bool,
    closed: bool,
    restoring: bool,
    cancel_requested: bool,
    pending: Option<Pending>,
    next_id: u64,
    action_count: u64,
    tasks: Vec<Task>,
    events: Vec<Value>,
    logs: Vec<Value>,
    initial: Values,
    commanded: Values,
    max_step: Values,
    stage_start: Values,
    stage: Option<StageRun>,
    history: Vec<Record>,
    results: Vec<Value>,
    last_measurement: Option<Measurement>,
    last_peak: Option<Value>,
    peak: Option<Peak>,
    peak_center: Option<f64>,
    sweep: Option<Sweep>,
    spectra: BTreeMap<String, Value>,
    checks: BTreeMap<String, Value>,
    counts: [u64; 3],
    result: Option<Value>,
    measuring_active: bool,
    spectrum_amplitudes: Vec<f64>,
    spectrum_measure: Vec<String>,
}
impl Controller {
    pub fn new(config: &Value) -> Result<Self> {
        let configuration = config.get("configuration").unwrap_or(config);
        let config: Config = serde_json::from_value(configuration.clone())
            .map_err(|e| config_error(e.to_string()))?;
        config.validate()?;
        let rng = match config.options.seed {
            Some(s) => Pcg64Mcg::seed_from_u64(s as u64),
            None => Pcg64Mcg::from_rng(&mut rand::rng()),
        };
        let peak_center = config.target.as_ref().map(|t| t.center);
        Ok(Self {
            config,
            rng,
            now: 0.0,
            began: 0.0,
            status: "ready".into(),
            reason: String::new(),
            operation: String::new(),
            active: false,
            closed: false,
            restoring: false,
            cancel_requested: false,
            pending: None,
            next_id: 1,
            action_count: 0,
            tasks: Vec::new(),
            events: Vec::new(),
            logs: Vec::new(),
            initial: Values::new(),
            commanded: Values::new(),
            max_step: Values::new(),
            stage_start: Values::new(),
            stage: None,
            history: Vec::new(),
            results: Vec::new(),
            last_measurement: None,
            last_peak: None,
            peak: None,
            peak_center,
            sweep: None,
            spectra: BTreeMap::new(),
            checks: BTreeMap::new(),
            counts: [0; 3],
            result: None,
            measuring_active: false,
            spectrum_amplitudes: Vec::new(),
            spectrum_measure: Vec::new(),
        })
    }
    fn schedule(&mut self, tasks: Vec<Task>) {
        self.tasks.extend(tasks.into_iter().rev());
    }
    fn log(&mut self, event: &str, data: Value) {
        self.logs.push(json!({"event":event,"data":data}));
    }
    fn emit(&mut self, kind: &str, data: Value) {
        self.events.push(json!({"kind":kind,"data":data}));
    }
    fn count_json(&self) -> Value {
        json!({"steps":self.counts[0],"measures":self.counts[1],"empty_windows":self.counts[2]})
    }
    fn state(&self) -> Value {
        json!({"status":self.status,"reason":self.reason,"initial":values_json(&self.initial),
            "commanded":values_json(&self.commanded),"max_step":values_json(&self.max_step),"results":self.results,
            "peak_center":self.peak_center,"counts":self.count_json(),"evaluations":self.history.len(),
            "active":self.active,"restoring":self.restoring,"closed":self.closed})
    }
    fn summary(&self) -> Value {
        let target = self.config.target.as_ref().map(|t| json!({"center":t.center,"width":t.width,"final_center":self.peak_center,"filter":t.filter_name}));
        json!({"status":self.status,"reason":self.reason,"stages":self.results,"initial":values_json(&self.initial),
            "final":values_json(&self.commanded),"evaluations":self.history.len(),"spectra":self.spectra,"checks":self.checks,"target":target})
    }
    fn reply(&mut self) -> Value {
        json!({"action":self.pending.as_ref().map(|p| &p.action),"complete":!self.active,
            "result":self.result,"state":self.state(),"events":std::mem::take(&mut self.events),"logs":std::mem::take(&mut self.logs)})
    }
    fn inspect_args(
        &self,
        interlocks: bool,
        limits: &[String],
        readbacks: &[String],
        setpoints: &[String],
    ) -> Value {
        let latest = if interlocks {
            names_union(
                self.config
                    .pressure
                    .keys()
                    .cloned()
                    .chain(self.config.currents()),
            )
        } else {
            vec![]
        };
        json!({"interlocks":interlocks,"latest":latest,"limits":limits,"readbacks":readbacks,"setpoints":setpoints})
    }
    fn issue(&mut self, op: &'static str, mut args: Value, reply: Reply) -> Result<()> {
        if self.pending.is_some() {
            return Err(Error::runtime("An Explorer action is already pending"));
        }
        self.action_count += 1;
        if self.action_count > MAX_ACTIONS {
            return Err(instrument_error("Native action budget exceeded"));
        }
        let id = self.next_id;
        self.next_id = self
            .next_id
            .checked_add(1)
            .ok_or_else(|| Error::runtime("Action identity exhausted"))?;
        if op == "command" {
            args["guard"] = json!({"pressure":self.config.pressure,"currents":self.config.currents(),"max_current":self.config.options.max_current});
        }
        let timeout = self.config.options.settle_timeout_s + 10.0 * MAX_CHANNELS as f64;
        self.pending = Some(Pending {
            action: json!({"id":id,"op":op,"args":args,"cancellable":!self.restoring,"timeout_s":timeout}),
            reply,
        });
        Ok(())
    }
    fn interlocks(&self, value: &Value) -> Result<()> {
        let channels = names_union(
            self.config
                .pressure
                .keys()
                .cloned()
                .chain(self.config.currents()),
        );
        if channels.is_empty() {
            return Ok(());
        }
        let latest = optional_floats(&value["latest"], &channels, "latest data")?;
        for (n, [lo, hi]) in &self.config.pressure {
            let v = latest[n];
            if !v.is_finite() {
                return Err(Error::new(
                    "SoftInterlock",
                    format!("Pressure {n} unavailable"),
                ));
            }
            if v < *lo || v > *hi {
                return Err(Error::new(
                    "SoftInterlock",
                    format!("Pressure {n} = {v} mbar outside [{lo}, {hi}] mbar"),
                ));
            }
        }
        for n in self.config.currents() {
            let v = latest[&n];
            if v.is_finite() && v.abs() > self.config.options.max_current {
                return Err(Error::new(
                    "SoftInterlock",
                    format!(
                        "Current {n} = {v} A exceeds {} A (discharge?)",
                        self.config.options.max_current
                    ),
                ));
            }
        }
        Ok(())
    }
    fn limits(value: &Value, channels: &[String]) -> Result<BTreeMap<String, [f64; 2]>> {
        channels
            .iter()
            .map(|n| {
                let pair = value["limits"]
                    .get(n)
                    .and_then(Value::as_array)
                    .ok_or_else(|| instrument_error(format!("{n}: missing device limits")))?;
                if pair.len() != 2 {
                    return Err(instrument_error("Malformed device limits"));
                }
                let lo = telemetry(&pair[0])?;
                let hi = telemetry(&pair[1])?;
                if !lo.is_finite() || !hi.is_finite() || lo >= hi {
                    return Err(instrument_error(format!("{n}: no admissible device range")));
                }
                Ok((n.clone(), [lo, hi]))
            })
            .collect()
    }
    fn targets(&self, delta: &[f64]) -> Values {
        let s = self.stage.as_ref().unwrap();
        s.stage
            .knobs
            .iter()
            .zip(delta)
            .flat_map(|(k, d)| {
                k.channels
                    .iter()
                    .map(move |(n, g)| (n.clone(), s.start[n] + g * d))
            })
            .collect()
    }
    fn amplitude(&self) -> f64 {
        let t = self.config.target.as_ref().unwrap();
        let n = t
            .amplitude_channel
            .as_ref()
            .unwrap_or_else(|| t.channels.keys().next().unwrap());
        self.commanded[n] / t.channels[n]
    }
    fn peak_measure(&self, stage: &Stage) -> Vec<String> {
        let t = self.config.target.as_ref().unwrap();
        let selected: Vec<_> = t
            .measure
            .iter()
            .filter(|n| stage.downstream.contains(n))
            .cloned()
            .collect();
        if selected.is_empty() {
            t.measure.clone()
        } else {
            selected
        }
    }
    fn filtered(&self, m: &Measurement, channels: &[String]) -> Result<(f64, f64)> {
        let mut value = 0.0;
        let mut variance = 0.0;
        for n in channels {
            let v = m
                .data
                .get(n)
                .ok_or_else(|| instrument_error(format!("{n}: missing current window")))?;
            value += self.config.options.polarity as f64 * v[0];
            if v[1].is_finite() {
                variance += v[1].powi(2);
            }
        }
        Ok((value, variance.sqrt()))
    }
    fn abort(&mut self, error: Error) {
        self.tasks.clear();
        if self.restoring {
            self.log(
                "restore_failed",
                json!({"reason":error.message,"traceback":error.to_string()}),
            );
            self.reason.push_str(&format!(
                "; restoring the stage start failed: {}",
                error.message
            ));
            self.restoring = false;
            self.schedule(vec![Task::Cleanup, Task::Finish]);
            return;
        }
        let stopped = error.kind == "Stopped";
        let interlock = error.kind == "SoftInterlock";
        if self.operation == "revert" {
            self.result = Some(
                json!({"status":if stopped {"stopped"} else {"error"},"reason":if stopped {"Undo stopped by the operator".to_owned()} else {error.message.clone()}}),
            );
        } else {
            self.status = (if stopped {
                "stopped"
            } else if interlock {
                "interlock"
            } else if error.kind == "ConfigError" {
                "configuration error"
            } else {
                "instrument error"
            })
            .into();
            self.reason = if stopped {
                "Stopped by the operator".into()
            } else {
                error.message.clone()
            };
        }
        self.log("fault", json!({"kind":error.kind,"message":error.message}));
        if (stopped || interlock) && self.operation != "revert" && !self.stage_start.is_empty() {
            self.restoring = true;
            self.emit(
                "status",
                json!({"text":"Restoring the settings of the stage start"}),
            );
            self.log("restore",json!({"targets":values_json(&self.stage_start),"commanded":values_json(&self.commanded)}));
            self.schedule(vec![
                Task::Cleanup,
                Task::Move(self.stage_start.clone(), false),
                Task::RecoveryDone,
                Task::Finish,
            ]);
        } else {
            self.schedule(vec![Task::Cleanup, Task::Finish]);
        }
    }
    fn begin(&mut self, mode: &str, payload: &Value) -> Result<()> {
        if self.closed {
            return Err(Error::runtime("Transmission controller is closed"));
        }
        if self.active || self.pending.is_some() {
            return Err(Error::runtime("A Transmission operation is already active"));
        }
        match mode {
            "run" => {
                if self.config.stages.is_empty() {
                    return Err(config_error("Define at least one stage"));
                }
                if !self.initial.is_empty() {
                    return Err(Error::runtime("Configure a new session before another run"));
                }
            }
            "spectrum" => {
                if self.config.target.is_none() {
                    return Err(config_error("A spectrum needs a target filter"));
                }
                let amplitudes = payload
                    .get("amplitudes")
                    .and_then(Value::as_array)
                    .ok_or_else(|| config_error("Spectrum amplitudes must be an array"))?;
                if amplitudes.is_empty() || amplitudes.len() > 4097 {
                    return Err(config_error("A spectrum needs 1..4097 amplitudes"));
                }
                self.spectrum_amplitudes = amplitudes
                    .iter()
                    .map(|v| {
                        v.as_f64()
                            .filter(|v| v.is_finite() && *v >= 0.0)
                            .ok_or_else(|| {
                                config_error("Spectrum amplitudes must be finite and nonnegative")
                            })
                    })
                    .collect::<Result<_>>()?;
                self.spectrum_measure = match payload.get("measure") {
                    Some(v) => serde_json::from_value(v.clone())
                        .map_err(|e| config_error(e.to_string()))?,
                    None => self.config.target.as_ref().unwrap().measure.clone(),
                };
                validate_names(&self.spectrum_measure, "spectrum measure", false)?;
                if self
                    .spectrum_measure
                    .iter()
                    .any(|n| !self.config.currents().contains(n))
                {
                    return Err(config_error(
                        "Spectrum measure is not in the configured current channels",
                    ));
                }
            }
            "revert" => {
                if self.initial.is_empty() {
                    self.result = Some(json!({"status":"nothing to undo","reason":""}));
                    return Ok(());
                }
            }
            _ => return Err(Error::argument("mode must be run, spectrum, or revert")),
        }
        self.config.validate()?;
        self.operation = mode.into();
        self.active = true;
        self.cancel_requested = false;
        self.restoring = false;
        self.result = None;
        self.action_count = 0;
        if mode != "revert" {
            self.status = "running".into();
            self.reason.clear();
        }
        self.schedule(vec![
            Task::Request("now", json!({}), Reply::Began),
            Task::Begin,
        ]);
        Ok(())
    }
    fn drive(&mut self, ctx: &Context) -> Result<Value> {
        for _ in 0..1024 {
            if self.pending.is_some() || !self.active {
                return Ok(self.reply());
            }
            if !self.restoring
                && (self.cancel_requested || ctx.is_cancelled())
                && self.result.is_none()
                && ![
                    "stopped",
                    "interlock",
                    "instrument error",
                    "configuration error",
                ]
                .contains(&self.status.as_str())
            {
                self.abort(Error::new("Stopped", "Stopped by the operator"));
            }
            if ctx.remaining().is_zero() {
                self.abort(instrument_error(
                    "Native request deadline expired; no more writes are permitted",
                ));
                return Err(Error::new(
                    "TimeoutError",
                    "Native request deadline expired",
                ));
            }
            let Some(task) = self.tasks.pop() else {
                return Err(Error::runtime("Transmission plan ended without an outcome"));
            };
            if let Err(e) = self.execute(task, ctx) {
                let e = if ctx.is_cancelled() && !self.restoring {
                    Error::new("Stopped", "Stopped by the operator")
                } else {
                    e
                };
                self.abort(e);
            }
        }
        self.abort(instrument_error("Native continuation budget exceeded"));
        Err(Error::runtime("Native continuation budget exceeded"))
    }
    fn observe(&mut self, observation: &Value, ctx: &Context) -> Result<Value> {
        let pending = self
            .pending
            .as_ref()
            .ok_or_else(|| Error::argument("No Explorer action is pending"))?;
        let id = observation
            .get("id")
            .and_then(Value::as_u64)
            .ok_or_else(|| Error::argument("Observation needs an integer action id"))?;
        if Some(id) != pending.action["id"].as_u64() {
            return Err(Error::argument("Stale or mismatched Explorer action id"));
        }
        let now = observation
            .get("time")
            .and_then(Value::as_f64)
            .filter(|n| n.is_finite() && *n >= self.now)
            .ok_or_else(|| Error::argument("Observation time must be finite and nondecreasing"))?;
        let error = observation.get("error").filter(|v| !v.is_null());
        if error.is_some() == observation.get("value").is_some() {
            return Err(Error::argument(
                "Observation needs exactly one value or error",
            ));
        }
        let error = error
            .map(|v| {
                let kind = v
                    .get("kind")
                    .and_then(Value::as_str)
                    .ok_or_else(|| Error::argument("Fault needs an exception kind"))?;
                let message = v
                    .get("message")
                    .and_then(Value::as_str)
                    .ok_or_else(|| Error::argument("Fault needs a message"))?;
                Ok(Error::new(kind, message))
            })
            .transpose()?;
        let pending = self.pending.take().unwrap();
        self.now = now;
        if let Some(error) = error {
            self.abort(error);
        } else {
            let value = &observation["value"];
            let result = if pending.action["op"] == "inspect"
                && pending.action["args"]["interlocks"] == true
            {
                self.interlocks(value)
                    .and_then(|_| self.accept(pending.reply, value))
            } else {
                self.accept(pending.reply, value)
            };
            if let Err(error) = result {
                self.abort(error);
            }
        }
        self.drive(ctx)
    }

    fn accept(&mut self, reply: Reply, value: &Value) -> Result<()> {
        match reply {
            Reply::Began => {
                self.began = self.now;
            }
            Reply::PreparedTarget => {
                let t = self.config.target.clone().unwrap();
                let channels: Vec<_> = t.channels.keys().cloned().collect();
                let start = floats(&value["setpoints"], &channels, "setpoint")?;
                let limits = Self::limits(value, &channels)?;
                for n in &channels {
                    if !start[n].is_finite() {
                        return Err(instrument_error(format!("{n}: no valid setpoint")));
                    }
                    let [lo, hi] = limits[n];
                    if start[n] < lo || start[n] > hi {
                        return Err(config_error(format!(
                            "{n}: initial filter value leaves device limits"
                        )));
                    }
                    let amplitudes = if self.operation == "spectrum" {
                        self.spectrum_amplitudes.clone()
                    } else {
                        let half = (t.track() + t.width).max(if t.spectrum_points > 0 {
                            t.spectrum_span * t.width
                        } else {
                            0.0
                        });
                        vec![(t.center - half).max(0.0), t.center + half]
                    };
                    if amplitudes
                        .iter()
                        .any(|a| !a.is_finite() || t.channels[n] * a < lo || t.channels[n] * a > hi)
                    {
                        return Err(config_error(format!(
                            "{n}: target or sweep leaves the device limits [{lo}, {hi}]"
                        )));
                    }
                }
                for (n, v) in &start {
                    self.initial.entry(n.clone()).or_insert(*v);
                    self.commanded.insert(n.clone(), *v);
                    self.max_step
                        .insert(n.clone(), t.channels[n].abs() * t.step());
                }
                self.stage_start = start;
                if self.operation == "run" {
                    self.emit(
                        "status",
                        json!({"text":format!("Mass filter to the selected peak ({} V)",t.center)}),
                    );
                    self.peak_center = Some(t.center);
                    self.tasks.push(Task::Move(t.values(t.center), true));
                }
            }
            Reply::PreparedStage(index) => self.prepare_stage(index, value)?,
            Reply::MoveChecked(movement, point) => {
                let channels: Vec<_> = point.keys().cloned().collect();
                let limits = Self::limits(value, &channels)?;
                for (n, v) in &point {
                    let [lo, hi] = limits[n];
                    if !v.is_finite() || *v < lo || *v > hi {
                        return Err(instrument_error(format!(
                            "{n}: {v} outside device limits [{lo}, {hi}]"
                        )));
                    }
                }
                self.tasks.push(Task::Request(
                    "command",
                    json!({"values":values_json(&point),"interlocks":movement.interlocks}),
                    Reply::Commanded(movement, point),
                ));
            }
            Reply::Commanded(movement, point) => {
                self.commanded.extend(point.clone());
                self.counts[0] += 1;
                let state = Settling {
                    movement,
                    point,
                    begin: self.now,
                    deadline: self.now + self.config.options.settle_timeout_s,
                    polls: 0,
                };
                self.tasks.push(Task::Settle(state));
            }
            Reply::Settled(mut s) => {
                let channels: Vec<_> = s.point.keys().cloned().collect();
                let readbacks = optional_floats(&value["readbacks"], &channels, "readback")?;
                let off: Vec<_> = channels
                    .iter()
                    .filter(|n| {
                        readbacks[*n].is_finite()
                            && (readbacks[*n] - s.point[*n]).abs()
                                > self.config.options.readback_tolerance
                    })
                    .collect();
                if off.is_empty() {
                    let dwell = if s.movement.index == s.movement.count {
                        self.config.options.settle_s
                    } else {
                        0.0
                    };
                    let elapsed = self.now - s.begin;
                    let interlocks = s.movement.interlocks;
                    self.schedule(vec![
                        Task::Pause(self.now + dwell, interlocks),
                        Task::MoveDone(s, elapsed, readbacks),
                    ]);
                } else {
                    if self.now >= s.deadline {
                        return Err(instrument_error(format!(
                            "Readback did not reach the setpoint within {} s ({})",
                            self.config.options.settle_timeout_s,
                            off.iter()
                                .map(|n| format!(
                                    "{n}: set {}, read {}",
                                    s.point[*n], readbacks[*n]
                                ))
                                .collect::<Vec<_>>()
                                .join(", ")
                        )));
                    }
                    s.polls += 1;
                    self.tasks.push(Task::Request(
                        "wait",
                        json!({"seconds":self.config.options.poll_s}),
                        Reply::SettleWait(s),
                    ));
                }
            }
            Reply::SettleWait(s) => self.tasks.push(Task::Settle(s)),
            Reply::PauseChecked(end, interlocks) => {
                if self.now < end {
                    self.tasks.push(Task::Request(
                        "wait",
                        json!({"seconds":self.config.options.poll_s.min(end-self.now)}),
                        Reply::PauseWait(end, interlocks),
                    ));
                }
            }
            Reply::PauseWait(end, interlocks) => self.tasks.push(Task::Pause(end, interlocks)),
            Reply::MeasureOpened(criterion, attempt) => {
                let channels = names_union(
                    criterion
                        .iter()
                        .cloned()
                        .chain(self.config.pressure.keys().cloned()),
                );
                let end = self.now + self.config.options.average_s;
                if !end.is_finite() || end <= self.now {
                    return Err(config_error(
                        "average_s cannot be represented on the instrument clock",
                    ));
                }
                let window = Window {
                    criterion,
                    channels,
                    attempt,
                    begin: self.now,
                    end,
                    deadline: 0.0,
                };
                self.schedule(vec![Task::Pause(end, true), Task::Window(window)]);
            }
            Reply::WindowChecked(window) => self.tasks.push(Task::Request(
                "window",
                json!({"channels":window.channels,"begin":window.begin,"end":window.end}),
                Reply::WindowData(window),
            )),
            Reply::WindowData(window) => {
                if value.get("data").is_none() {
                    return Err(instrument_error(
                        "Window response needs data and optional diagnostics",
                    ));
                }
                if value["data"].is_null() {
                    if self.now >= window.deadline {
                        return Err(instrument_error(
                            "No fresh data closed the averaging window; check acquisition and recording",
                        ));
                    }
                    self.tasks.push(Task::Request(
                        "wait",
                        json!({"seconds":self.config.options.poll_s}),
                        Reply::WindowWait(window),
                    ));
                } else {
                    self.tasks.push(Task::Request(
                        "measuring",
                        json!({"active":false}),
                        Reply::MeasureClosed(window, value.clone()),
                    ));
                }
            }
            Reply::WindowWait(window) => self.tasks.push(Task::Window(window)),
            Reply::MeasureClosed(window, value) => self.close_window(window, &value)?,
            Reply::Cleanup => {}
        }
        Ok(())
    }

    fn close_window(&mut self, window: Window, value: &Value) -> Result<()> {
        let mut data = Samples::new();
        let mut empty = Vec::new();
        let mut thin = Vec::new();
        for n in &window.channels {
            let v = value["data"]
                .get(n)
                .and_then(Value::as_array)
                .ok_or_else(|| instrument_error(format!("{n}: missing averaging window")))?;
            if v.len() != 3 {
                return Err(instrument_error(
                    "Window data must contain mean, SEM, and sample count",
                ));
            }
            let avg = telemetry(&v[0])?;
            let sem = telemetry(&v[1])?;
            let count = v[2]
                .as_u64()
                .filter(|n| *n <= 1_000_000_000)
                .ok_or_else(|| instrument_error("Invalid sample count"))?;
            if sem.is_finite() && sem < 0.0 {
                return Err(instrument_error("Negative standard error"));
            }
            data.insert(n.clone(), [avg, sem, count as f64]);
            if count == 0 || !avg.is_finite() {
                empty.push(n.clone());
            } else if count < 2 && window.criterion.contains(n) {
                thin.push(n.clone());
            }
        }
        self.counts[1] += 1;
        self.log("measure",json!({"begin":window.begin,"end":window.end,"closed_after_s":self.now-window.end,"attempt":window.attempt,
            "data":samples_json(&data),"empty":empty,"thin":thin,"instrument":value.get("diagnostics").cloned().unwrap_or(json!({}))}));
        if empty.is_empty() && thin.is_empty() {
            self.last_measurement = Some(Measurement {
                begin: window.begin,
                end: window.end,
                data,
            });
        } else {
            self.counts[2] += 1;
            let missing = empty
                .iter()
                .chain(&thin)
                .cloned()
                .collect::<Vec<_>>()
                .join(", ");
            self.emit("status",json!({"text":format!("Too few samples for {missing} in the averaging window; measuring again")}));
            if window.attempt >= 3 {
                return Err(instrument_error(if !empty.is_empty() {
                    format!(
                        "No valid samples in 3 averaging windows in a row for {}",
                        empty.join(", ")
                    )
                } else {
                    format!(
                        "Fewer than 2 samples in 3 averaging windows in a row for {}: lengthen average_s or shorten the device interval",
                        thin.join(", ")
                    )
                }));
            }
            self.tasks
                .push(Task::Measure(window.criterion, window.attempt + 1));
        }
        Ok(())
    }

    fn prepare_stage(&mut self, index: usize, value: &Value) -> Result<()> {
        let stage = self.config.stages[index].clone();
        let channels = stage.channels();
        let start = floats(&value["setpoints"], &channels, "setpoint")?;
        let limits = Self::limits(value, &channels)?;
        if start.values().any(|v| !v.is_finite()) {
            return Err(instrument_error("No valid stage-start setpoint"));
        }
        let mut lower = Vec::new();
        let mut upper = Vec::new();
        let mut steps = Vec::new();
        for k in &stage.knobs {
            let scale = if k.relative {
                let n = k
                    .scale_channel
                    .as_ref()
                    .unwrap_or_else(|| k.channels.keys().next().unwrap());
                (start[n] / k.channels[n]).abs().max(k.floor)
            } else {
                1.0
            };
            let mut lo = k.window[0] * scale;
            let mut hi = k.window[1] * scale;
            let step = k.max_step * scale;
            if !lo.is_finite() || !hi.is_finite() || !step.is_finite() {
                return Err(config_error(
                    "Resolved relative window or step is not finite",
                ));
            }
            for (n, g) in &k.channels {
                let [cmin, cmax] = limits[n];
                if start[n] < cmin || start[n] > cmax {
                    return Err(config_error(format!(
                        "{n}: start value {} outside device limits [{cmin}, {cmax}]",
                        start[n]
                    )));
                }
                let a = (cmin - start[n]) / g;
                let b = (cmax - start[n]) / g;
                lo = lo.max(a.min(b));
                hi = hi.min(a.max(b));
            }
            if !lo.is_finite()
                || !hi.is_finite()
                || !(hi - lo).is_finite()
                || hi - lo < step
                || step <= 0.0
            {
                return Err(config_error(format!(
                    "knob {:?}: the window inside device limits is narrower than max_step",
                    k.name
                )));
            }
            lower.push(lo);
            upper.push(hi);
            steps.push(step);
        }
        for (n, v) in &start {
            self.initial.entry(n.clone()).or_insert(*v);
            self.commanded.insert(n.clone(), *v);
        }
        for (k, step) in stage.knobs.iter().zip(&steps) {
            for (n, g) in &k.channels {
                let step = g.abs() * step;
                if !step.is_finite() || step <= 0.0 {
                    return Err(config_error(
                        "Resolved channel step is not finite and positive",
                    ));
                }
                self.max_step.insert(n.clone(), step);
            }
        }
        self.stage_start = start.clone();
        if let Some(t) = &self.config.target {
            for n in t.channels.keys() {
                self.stage_start.insert(n.clone(), self.commanded[n]);
            }
        }
        self.log("stage_start",json!({"stage":stage.name,"knobs":stage.knobs.iter().map(|k| (k.name.clone(),k.channels.clone())).collect::<BTreeMap<_,_>>(),
            "lower":lower,"upper":upper,"start":start,"steps":steps,"budget":stage.budget(),"aperture":stage.aperture,"downstream":stage.downstream,
            "normalize_by":stage.normalize_by,"peak":stage.peak,"strategy":self.config.options.strategy,"peak_measure":if stage.peak {Some(self.peak_measure(&stage))} else {None}}));
        self.emit(
            "stage",
            json!({"stage":stage.name,"lower":lower,"upper":upper,"start":start,"steps":steps}),
        );
        let strategy = Strategy::new(
            &self.config.options.strategy,
            lower.clone(),
            upper.clone(),
            steps.clone(),
        );
        let zero = vec![0.0; stage.knobs.len()];
        let mut tasks = vec![
            Task::Evaluate(zero.clone(), "initial", None),
            Task::Evaluate(zero.clone(), "initial", None),
            Task::Reference,
        ];
        for i in 0..stage.knobs.len() {
            for sign in [1.0, -1.0] {
                let mut delta = zero.clone();
                delta[i] = sign * steps[i];
                if delta[i] >= lower[i] && delta[i] <= upper[i] {
                    tasks.push(Task::Evaluate(delta, "probe", None));
                }
            }
        }
        tasks.push(Task::Search);
        self.stage = Some(StageRun {
            stage,
            start,
            lower,
            upper,
            strategy,
            began: self.now,
            history: vec![],
            reference: f64::NAN,
            since_reference: 0,
            best: zero,
            verification: vec![],
            verdict: json!({}),
        });
        self.schedule(tasks);
        Ok(())
    }

    fn score(
        &mut self,
        delta: Vec<f64>,
        kind: &'static str,
        role: Option<&'static str>,
    ) -> Result<()> {
        let m = self
            .last_measurement
            .take()
            .ok_or_else(|| instrument_error("An evaluation has no completed measurement"))?;
        let s = self.stage.as_ref().unwrap();
        let stage = &s.stage;
        let o = &self.config.options;
        let (down, down_sem) = self.filtered(&m, &stage.downstream)?;
        let incident = down
            + match &stage.aperture {
                Some(n) => self.filtered(&m, std::slice::from_ref(n))?.0,
                None => 0.0,
            };
        let mut objective = down;
        let mut objective_sem = down_sem;
        if let Some(p) = &self.last_peak {
            objective = telemetry(&p["height"])?;
            objective_sem = telemetry(&p["height_sem"])?;
        } else if !stage.normalize_by.is_empty() {
            let (norm, norm_sem) = self.filtered(&m, &stage.normalize_by)?;
            if norm > o.min_current {
                objective = down / norm;
                objective_sem = (down_sem / norm).hypot(down * norm_sem / norm.powi(2));
            } else {
                objective = f64::NAN;
                objective_sem = f64::NAN;
            }
        }
        let reason = if !objective.is_finite() {
            "no incoming flux to normalize by".into()
        } else if incident <= o.min_current {
            "no beam reaches the aperture".into()
        } else if s.reference.is_finite() && incident < o.lost_fraction * s.reference {
            format!(
                "beam lost: flux {incident} A < {} x stage start",
                o.lost_fraction
            )
        } else {
            String::new()
        };
        let valid = reason.is_empty();
        let model = if valid { objective } else { 0.0 };
        let currents: Samples = stage
            .currents()
            .iter()
            .map(|n| {
                m.data
                    .get(n)
                    .copied()
                    .map(|v| (n.clone(), v))
                    .ok_or_else(|| instrument_error(format!("{n}: missing criterion current")))
            })
            .collect::<Result<_>>()?;
        let pressure: Samples = self
            .config
            .pressure
            .keys()
            .map(|n| {
                m.data
                    .get(n)
                    .copied()
                    .map(|v| (n.clone(), v))
                    .ok_or_else(|| instrument_error(format!("{n}: missing pressure window")))
            })
            .collect::<Result<_>>()?;
        let setpoints: Values = stage
            .channels()
            .iter()
            .map(|n| (n.clone(), self.commanded[n]))
            .collect();
        let knobs: Values = stage
            .knobs
            .iter()
            .zip(&delta)
            .map(|(k, d)| (k.name.clone(), *d))
            .collect();
        let mut value = json!({"stage":stage.name,"kind":kind,"time":m.end,"begin":m.begin,"delta":delta,"knobs":values_json(&knobs),
            "setpoints":values_json(&setpoints),"currents":samples_json(&currents),"pressures":samples_json(&pressure),"objective":vf(objective),
            "objective_sem":vf(objective_sem),"incident":vf(incident),"transmitted":vf(down),"valid":valid,"reason":reason,"model_objective":vf(model),
            "peak":self.last_peak.take(),"index":self.history.len()});
        if let Some(role) = role {
            value["verify_role"] = json!(role);
        }
        let record = Record {
            delta,
            time: m.end,
            objective,
            objective_sem,
            model,
            incident,
            transmitted: down,
            valid,
            json: value.clone(),
        };
        self.history.push(record.clone());
        self.stage.as_mut().unwrap().history.push(record);
        self.log("evaluation", value.clone());
        self.emit("evaluation", json!({"record":value}));
        Ok(())
    }

    fn decide(&mut self) -> Result<()> {
        let s = self.stage.as_ref().unwrap();
        let pairs = &s.verification;
        let mut gains = Vec::new();
        let mut initials = Vec::new();
        let mut absolute = Vec::new();
        let mut valid = true;
        for pair in pairs.as_chunks::<2>().0 {
            let best = &pair
                .iter()
                .find(|p| p.0 == "best")
                .ok_or_else(|| Error::runtime("Missing best verification measurement"))?
                .1;
            let initial = &pair
                .iter()
                .find(|p| p.0 == "initial")
                .ok_or_else(|| Error::runtime("Missing initial verification measurement"))?
                .1;
            valid &= best.valid;
            gains.push(best.model - initial.model);
            initials.push(initial.model);
            absolute.push(best.transmitted - initial.transmitted);
        }
        if gains.len() != self.config.options.verify_pairs {
            return Err(Error::runtime("Incomplete paired verification"));
        }
        let gain = mean(&gains);
        let gain_sem = sem(&gains);
        let baseline = mean(&initials);
        let absolute_gain = mean(&absolute);
        let guarded = !s.stage.normalize_by.is_empty() && !s.stage.peak && absolute_gain <= 0.0;
        let adopt = valid
            && gain > 0.0
            && gain > self.config.options.significance * gain_sem.max(1e-300)
            && !guarded;
        self.log("verify",json!({"stage":s.stage.name,"best":s.best,"gains":gains,"initials":initials,"absolute":absolute,"gain":gain,
            "gain_sem":gain_sem,"absolute_gain":absolute_gain,"best_valid":valid,"refused_fewer_ions":guarded,"adopt":adopt}));
        self.stage.as_mut().unwrap().verdict = json!({"gain":gain,"gain_sem":gain_sem,"baseline":baseline,"absolute_gain":absolute_gain,
            "relative_gain":vf(if baseline > 0.0 {gain/baseline} else {f64::NAN}),"adopt":adopt});
        let s = self.stage.as_ref().unwrap();
        let delta = if adopt {
            s.best.clone()
        } else {
            vec![0.0; s.stage.knobs.len()]
        };
        self.schedule(vec![
            Task::Move(self.targets(&delta), true),
            Task::StageDone,
        ]);
        Ok(())
    }

    fn execute(&mut self, task: Task, ctx: &Context) -> Result<()> {
        match task {
            Task::Request(op, args, reply) => {
                if op == "measuring" {
                    self.measuring_active = args["active"] == true;
                }
                self.issue(op, args, reply)?;
            }
            Task::Begin => {
                let o = &self.config.options;
                if self.operation == "revert" {
                    self.stage_start = self.initial.clone();
                    self.log("revert_start",json!({"initial":values_json(&self.initial),"commanded":values_json(&self.commanded)}));
                    self.schedule(vec![Task::Move(self.initial.clone(), true), Task::Finish]);
                } else {
                    let stages: Vec<_> = self.config.stage_indices().iter().map(|i| &self.config.stages[*i]).map(|s| json!({"name":s.name,"budget":s.budget(),"peak":s.peak,"knobs":s.knobs.iter().map(|k| &k.name).collect::<Vec<_>>()})).collect();
                    let target = self.config.target.as_ref().map(|t| json!({"channels":t.channels,"center":t.center,"width":t.width,"measure":t.measure,"points":t.points,"track":t.track(),"max_step":t.step(),"filter":t.filter_name}));
                    let event = if self.operation == "run" {
                        "run_start"
                    } else {
                        "sweep_start"
                    };
                    self.log(event,json!({"strategy":o.strategy,"options":o,"stages":stages,"pressure":self.config.pressure,"target":target}));
                    if self.operation == "spectrum" {
                        self.schedule(vec![
                            Task::PrepareTarget,
                            Task::Sweep(
                                self.spectrum_amplitudes.clone(),
                                self.spectrum_measure.clone(),
                                "spectrum".into(),
                            ),
                            Task::Finish,
                        ]);
                    } else {
                        let mut tasks = Vec::new();
                        if let Some(t) = &self.config.target {
                            tasks.push(Task::PrepareTarget);
                            if t.spectrum_points > 0 {
                                tasks.push(Task::Sweep(
                                    self.spectrum_grid(),
                                    t.measure.clone(),
                                    "before".into(),
                                ));
                            }
                        }
                        tasks.push(Task::Snapshot("before"));
                        tasks.extend(self.config.stage_indices().into_iter().map(Task::Stage));
                        tasks.push(Task::Snapshot("after"));
                        if let Some(t) = &self.config.target
                            && t.spectrum_points > 0
                        {
                            tasks.push(Task::Sweep(
                                self.spectrum_grid(),
                                t.measure.clone(),
                                "after".into(),
                            ));
                        }
                        tasks.push(Task::Finish);
                        self.schedule(tasks);
                    }
                }
            }
            Task::PrepareTarget => {
                let channels: Vec<_> = self
                    .config
                    .target
                    .as_ref()
                    .unwrap()
                    .channels
                    .keys()
                    .cloned()
                    .collect();
                self.issue(
                    "inspect",
                    self.inspect_args(false, &channels, &[], &channels),
                    Reply::PreparedTarget,
                )?;
            }
            Task::Stage(index) => {
                let channels = self.config.stages[index].channels();
                self.issue(
                    "inspect",
                    self.inspect_args(false, &channels, &[], &channels),
                    Reply::PreparedStage(index),
                )?;
            }
            Task::Reference => {
                let s = self.stage.as_mut().unwrap();
                if s.history.len() != 2 {
                    return Err(Error::runtime("Stage needs two initial reference points"));
                }
                s.reference = 0.5 * (s.history[0].incident + s.history[1].incident);
                let reference = s.reference;
                let stage = s.stage.clone();
                if reference <= self.config.options.min_current {
                    return Err(instrument_error(format!(
                        "{}: no beam reaches {} at the start; tune upstream first",
                        stage.name,
                        stage.aperture.as_deref().unwrap_or("the collectors")
                    )));
                }
                for record in &mut s.history {
                    record.valid = true;
                    record.model = if record.objective.is_finite() {
                        record.objective
                    } else {
                        0.0
                    };
                    record.json["valid"] = json!(true);
                    record.json["reason"] = json!("");
                    record.json["model_objective"] = vf(record.model);
                    let index = record.json["index"].as_u64().unwrap() as usize;
                    self.history[index] = record.clone();
                }
                let records: Vec<_> = s.history.iter().map(|r| r.json.clone()).collect();
                self.emit("history_update", json!({"records":records}));
                self.log("reference",json!({"stage":stage.name,"incident":reference,"lost_below":self.config.options.lost_fraction*reference}));
            }
            Task::Evaluate(delta, kind, role) => {
                self.last_peak = None;
                self.last_measurement = None;
                self.schedule(vec![
                    Task::Move(self.targets(&delta), true),
                    Task::EvaluationMeasure,
                    Task::Score(delta, kind, role),
                ]);
            }
            Task::EvaluationMeasure => {
                let stage = &self.stage.as_ref().unwrap().stage;
                if stage.peak {
                    let t = self.config.target.as_ref().unwrap();
                    let center = self.peak_center.unwrap();
                    let amplitudes = linspace(-1.0, 1.0, t.points)
                        .into_iter()
                        .map(|u| (center + u * t.width).max(0.0))
                        .collect();
                    self.peak = Some(Peak {
                        amplitudes,
                        currents: vec![],
                        sems: vec![],
                        measure: self.peak_measure(stage),
                        center_data: None,
                    });
                    self.tasks.push(Task::PeakPoint(0));
                } else {
                    self.tasks.push(Task::Measure(stage.currents(), 1));
                }
            }
            Task::Score(delta, kind, role) => self.score(delta, kind, role)?,
            Task::Search => {
                let s = self.stage.as_ref().unwrap();
                let budget = s.stage.budget() - 2 * self.config.options.verify_pairs;
                if s.history.len() >= budget || s.strategy.converged {
                    let best: Vec<_> = s
                        .strategy
                        .incumbent(&s.history, self.now)
                        .iter()
                        .enumerate()
                        .map(|(i, d)| d.clamp(s.lower[i], s.upper[i]))
                        .collect();
                    if best.iter().all(|v| v.abs() <= 1e-8) {
                        self.stage.as_mut().unwrap().verdict = json!({"gain":0.0,"gain_sem":vf(f64::NAN),"baseline":vf(f64::NAN),"relative_gain":0.0,"absolute_gain":0.0,"adopt":false});
                        self.schedule(vec![
                            Task::Move(self.targets(&vec![0.0; best.len()]), true),
                            Task::StageDone,
                        ]);
                    } else {
                        self.stage.as_mut().unwrap().best = best.clone();
                        let zero = vec![0.0; best.len()];
                        let mut tasks = Vec::new();
                        for pair in 0..self.config.options.verify_pairs {
                            let order = if pair % 2 == 0 {
                                [(best.clone(), "best"), (zero.clone(), "initial")]
                            } else {
                                [(zero.clone(), "initial"), (best.clone(), "best")]
                            };
                            for (delta, role) in order {
                                tasks.extend([
                                    Task::Evaluate(delta, "verify", Some(role)),
                                    Task::VerifyCollect(role),
                                ]);
                            }
                        }
                        tasks.push(Task::Decide);
                        self.schedule(tasks);
                    }
                } else if s.since_reference >= self.config.options.reference_every {
                    let best = s.strategy.incumbent(&s.history, self.now);
                    self.stage.as_mut().unwrap().since_reference = 0;
                    self.schedule(vec![Task::Evaluate(best, "reference", None), Task::Search]);
                } else {
                    let s = self.stage.as_mut().unwrap();
                    let proposed = s.strategy.ask(&s.history, self.now, &mut self.rng, ctx)?;
                    let delta: Vec<_> = proposed
                        .iter()
                        .enumerate()
                        .map(|(i, d)| d.clamp(s.lower[i], s.upper[i]))
                        .collect();
                    let mut data = s.strategy.diagnostics();
                    data["stage"] = json!(s.stage.name);
                    data["proposed"] = json!(proposed);
                    data["delta"] = json!(delta);
                    self.log("ask", data);
                    self.schedule(vec![
                        Task::Evaluate(delta, "explore", None),
                        Task::Tell,
                        Task::Search,
                    ]);
                }
            }
            Task::Tell => {
                let s = self.stage.as_mut().unwrap();
                s.strategy.tell(&s.history, self.now);
                s.since_reference += 1;
                let mut data = s.strategy.diagnostics();
                let r = s.history.last().unwrap();
                data["stage"] = json!(s.stage.name);
                data["index"] = r.json["index"].clone();
                data["valid"] = json!(r.valid);
                self.log("tell", data);
            }
            Task::VerifyCollect(role) => {
                let s = self.stage.as_mut().unwrap();
                s.verification
                    .push((role.into(), s.history.last().unwrap().clone()));
            }
            Task::Decide => self.decide()?,
            Task::StageDone => {
                let s = self.stage.as_ref().unwrap();
                let adopted = s.verdict["adopt"] == true;
                let final_delta = if adopted {
                    s.best.clone()
                } else {
                    vec![0.0; s.best.len()]
                };
                let mut result = s.verdict.clone();
                result.as_object_mut().unwrap().remove("adopt");
                result["stage"] = json!(s.stage.name);
                result["start"] = values_json(&s.start);
                result["best"] = values_json(
                    &s.stage
                        .knobs
                        .iter()
                        .zip(&s.best)
                        .map(|(k, d)| (k.name.clone(), *d))
                        .collect(),
                );
                result["adopted"] = json!(adopted);
                result["final"] = values_json(&self.targets(&final_delta));
                result["evaluations"] = json!(s.history.len());
                result["converged"] = json!(s.strategy.converged);
                if s.stage.peak {
                    result["peak_center"] = json!(self.peak_center);
                }
                let mut data = result.clone();
                data["duration_s"] = json!(self.now - s.began);
                data["strategy_state"] = s.strategy.diagnostics();
                self.results.push(result.clone());
                self.log("stage_done", data);
                self.emit("stage_done", json!({"result":result}));
            }
            Task::Snapshot(label) => {
                let channels = self.config.currents();
                if !channels.is_empty() {
                    self.schedule(vec![Task::Measure(channels, 1), Task::SnapshotDone(label)]);
                }
            }
            Task::SnapshotDone(label) => {
                let m = self
                    .last_measurement
                    .take()
                    .ok_or_else(|| instrument_error("Snapshot has no measurement"))?;
                let currents: Values = self
                    .config
                    .currents()
                    .iter()
                    .map(|n| {
                        (
                            n.clone(),
                            m.data[n][0] * self.config.options.polarity as f64,
                        )
                    })
                    .collect();
                let data = json!({"time":m.end,"currents":values_json(&currents)});
                self.checks.insert(label.into(), data.clone());
                let mut log = data.clone();
                log["label"] = json!(label);
                self.log("snapshot", log);
                self.emit("check", json!({"label":label,"data":data}));
            }
            Task::Sweep(amplitudes, measure, label) => {
                if self.sweep.is_some() {
                    return Err(Error::runtime("Nested filter sweep"));
                }
                self.sweep = Some(Sweep {
                    amplitudes,
                    measure,
                    label,
                    back: self.amplitude(),
                    began: self.now,
                    current: vec![],
                    sem: vec![],
                });
                self.tasks.push(Task::SweepPoint(0));
            }
            Task::SweepPoint(index) => {
                let s = self.sweep.as_ref().unwrap();
                if index == s.amplitudes.len() {
                    self.schedule(vec![
                        Task::Move(self.config.target.as_ref().unwrap().values(s.back), true),
                        Task::SweepDone,
                    ]);
                } else {
                    self.schedule(vec![
                        Task::Move(
                            self.config
                                .target
                                .as_ref()
                                .unwrap()
                                .values(s.amplitudes[index]),
                            true,
                        ),
                        Task::Measure(s.measure.clone(), 1),
                        Task::SweepCollect(index),
                    ]);
                }
            }
            Task::SweepCollect(index) => {
                let m = self
                    .last_measurement
                    .take()
                    .ok_or_else(|| instrument_error("Sweep has no measurement"))?;
                let (value, error) = self.filtered(&m, &self.sweep.as_ref().unwrap().measure)?;
                let s = self.sweep.as_mut().unwrap();
                s.current.push(value);
                s.sem.push(error);
                let data =
                    json!({"label":s.label,"amplitude":s.amplitudes[index],"current":vf(value)});
                self.emit("sweep", data);
                self.tasks.push(Task::SweepPoint(index + 1));
            }
            Task::SweepDone => {
                let s = self.sweep.take().unwrap();
                let data = json!({"amplitude":s.amplitudes,"current":s.current.iter().copied().map(vf).collect::<Vec<_>>(),"sem":s.sem.iter().copied().map(vf).collect::<Vec<_>>()});
                let mut log = data.clone();
                log["label"] = json!(s.label);
                log["measure"] = json!(s.measure);
                log["filter"] = json!(self.config.target.as_ref().unwrap().channels);
                log["duration_s"] = json!(self.now - s.began);
                self.log("sweep", log);
                if self.operation == "spectrum" {
                    self.result = Some(data);
                } else {
                    self.spectra.insert(s.label.clone(), data.clone());
                    self.emit("spectrum", json!({"phase":s.label,"data":data}));
                }
            }
            Task::PeakPoint(index) => {
                let p = self.peak.as_ref().unwrap();
                let t = self.config.target.as_ref().unwrap();
                if index < p.amplitudes.len() {
                    let channels = names_union(
                        self.stage
                            .as_ref()
                            .unwrap()
                            .stage
                            .currents()
                            .into_iter()
                            .chain(t.measure.clone()),
                    );
                    self.schedule(vec![
                        Task::Move(t.values(p.amplitudes[index]), true),
                        Task::Measure(channels, 1),
                        Task::PeakCollect(index),
                    ]);
                } else {
                    let p = self.peak.take().unwrap();
                    let (height, height_sem, fitted) =
                        fit_peak(&p.amplitudes, &p.currents, &p.sems)?;
                    let center =
                        fitted.clamp((t.center - t.track()).max(0.0), t.center + t.track());
                    self.peak_center = Some(center);
                    self.last_peak = Some(
                        json!({"amplitudes":p.amplitudes,"currents":p.currents,"sems":p.sems,"height":vf(height),"height_sem":vf(height_sem),"center":center,"fitted_center":fitted,"measure":p.measure}),
                    );
                    self.last_measurement = p.center_data;
                    self.tasks.push(Task::Move(t.values(center), true));
                }
            }
            Task::PeakCollect(index) => {
                let m = self
                    .last_measurement
                    .take()
                    .ok_or_else(|| instrument_error("Peak sweep has no measurement"))?;
                let (value, error) = self.filtered(&m, &self.peak.as_ref().unwrap().measure)?;
                let p = self.peak.as_mut().unwrap();
                p.currents.push(value);
                p.sems.push(error);
                if index == p.amplitudes.len() / 2 {
                    p.center_data = Some(m);
                }
                self.tasks.push(Task::PeakPoint(index + 1));
            }
            Task::Move(targets, interlocks) => {
                let mut start = Values::new();
                let mut count = 1_usize;
                for (n, v) in &targets {
                    let previous = *self.commanded.get(n).ok_or_else(|| {
                        instrument_error(format!("{n}: no acknowledged starting setpoint"))
                    })?;
                    let step = *self
                        .max_step
                        .get(n)
                        .ok_or_else(|| instrument_error(format!("{n}: no step limit")))?;
                    let moves = ((v - previous).abs() / step - 1e-9).ceil().max(1.0);
                    if !v.is_finite()
                        || !step.is_finite()
                        || step <= 0.0
                        || !moves.is_finite()
                        || moves > MAX_ACTIONS as f64
                    {
                        return Err(instrument_error(
                            "Move is not finite or exceeds the native step budget",
                        ));
                    }
                    count = count.max(moves as usize);
                    start.insert(n.clone(), previous);
                }
                self.tasks.push(Task::MoveStep(Movement {
                    start,
                    targets,
                    count,
                    index: 1,
                    interlocks,
                }));
            }
            Task::MoveStep(movement) => {
                let point: Values = movement
                    .targets
                    .iter()
                    .map(|(n, v)| {
                        (
                            n.clone(),
                            movement.start[n]
                                + (v - movement.start[n]) * movement.index as f64
                                    / movement.count as f64,
                        )
                    })
                    .collect();
                let channels: Vec<_> = point.keys().cloned().collect();
                self.issue(
                    "inspect",
                    self.inspect_args(movement.interlocks, &channels, &[], &[]),
                    Reply::MoveChecked(movement, point),
                )?;
            }
            Task::Settle(s) => {
                let channels: Vec<_> = s.point.keys().cloned().collect();
                self.issue(
                    "inspect",
                    self.inspect_args(s.movement.interlocks, &[], &channels, &[]),
                    Reply::Settled(s),
                )?;
            }
            Task::MoveDone(s, elapsed, readbacks) => {
                self.log("step",json!({"step":s.movement.index,"of":s.movement.count,"setpoints":values_json(&s.point),"restoring":self.restoring,"settle_s":elapsed,"polls":s.polls,"readbacks":values_json(&readbacks)}));
                if s.movement.index < s.movement.count {
                    let mut m = s.movement;
                    m.index += 1;
                    self.tasks.push(Task::MoveStep(m));
                }
            }
            Task::Pause(end, interlocks) => self.issue(
                "inspect",
                self.inspect_args(interlocks, &[], &[], &[]),
                Reply::PauseChecked(end, interlocks),
            )?,
            Task::Measure(criterion, attempt) => {
                let criterion = names_union(criterion);
                self.measuring_active = true;
                self.issue(
                    "measuring",
                    json!({"active":true}),
                    Reply::MeasureOpened(criterion, attempt),
                )?;
            }
            Task::Window(mut window) => {
                if window.deadline == 0.0 {
                    window.deadline = self.now + self.config.options.settle_timeout_s;
                }
                self.issue(
                    "inspect",
                    self.inspect_args(true, &[], &[], &[]),
                    Reply::WindowChecked(window),
                )?;
            }
            Task::RecoveryDone => self.restoring = false,
            Task::Cleanup => {
                if self.measuring_active {
                    self.measuring_active = false;
                    self.issue("measuring", json!({"active":false}), Reply::Cleanup)?;
                }
            }
            Task::Finish => self.finish(),
        }
        Ok(())
    }
    fn spectrum_grid(&self) -> Vec<f64> {
        let t = self.config.target.as_ref().unwrap();
        let half = t.spectrum_span * t.width;
        linspace(
            (t.center - half).max(0.0),
            t.center + half,
            t.spectrum_points,
        )
    }
    fn finish(&mut self) {
        if self.operation == "revert" {
            let result = self
                .result
                .take()
                .unwrap_or_else(|| json!({"status":"restored","reason":""}));
            let mut data = result.clone();
            data["duration_s"] = json!(self.now - self.began);
            data["commanded"] = values_json(&self.commanded);
            data["traceback"] = if result["status"] == "restored" {
                Value::Null
            } else {
                result["reason"].clone()
            };
            self.log("revert_end", data);
            self.result = Some(result);
        } else {
            if self.status == "running" {
                self.status = "completed".into();
            }
            self.log(if self.operation == "run" {"run_end"} else {"sweep_end"},json!({"status":self.status,"reason":self.reason,"duration_s":self.now-self.began,
                "evaluations":self.history.len(),"counts":self.count_json(),"commanded":values_json(&self.commanded),"initial":values_json(&self.initial),
                "traceback":if self.reason.is_empty() {None} else {Some(self.reason.clone())}}));
            if self.operation == "run" {
                self.result = Some(self.summary());
                self.emit("done", json!({"status":self.status,"reason":self.reason}));
            } else {
                let mut data = self.result.take().unwrap_or_else(|| {
                    if let Some(s) = &self.sweep { json!({"amplitude":s.amplitudes[..s.current.len()],"current":s.current.iter().copied().map(vf).collect::<Vec<_>>(),"sem":s.sem.iter().copied().map(vf).collect::<Vec<_>>()}) }
                    else {json!({"amplitude":[],"current":[],"sem":[]})}
                });
                data["status"] = json!(self.status);
                data["reason"] = json!(self.reason);
                self.result = Some(data);
            }
        }
        self.active = false;
        self.restoring = false;
        self.cancel_requested = false;
        self.tasks.clear();
        self.sweep = None;
        self.peak = None;
    }
}

fn payload<'a>(args: &'a [Value], kwargs: &'a Value, name: &str) -> Result<&'a Value> {
    if args.len() > 1 || (!args.is_empty() && kwargs.get(name).is_some()) {
        return Err(Error::argument(
            "Expected one payload, without duplicate arguments",
        ));
    }
    args.first()
        .or_else(|| kwargs.get(name))
        .ok_or_else(|| Error::argument(format!("Missing {name}")))
}
fn no_arguments(args: &[Value], kwargs: &Value) -> Result<()> {
    if !args.is_empty() || kwargs.as_object().is_some_and(|v| !v.is_empty()) {
        return Err(Error::argument("Method takes no arguments"));
    }
    Ok(())
}
impl Backend for Controller {
    fn call(
        &mut self,
        method: &str,
        args: &[Value],
        kwargs: &Value,
        ctx: &Context,
    ) -> Result<Value> {
        match method {
            "configure" => {
                ctx.check_cancelled()?;
                if self.active || self.pending.is_some() {
                    return Err(Error::runtime(
                        "Cannot configure during an active operation",
                    ));
                }
                if self.closed {
                    return Err(Error::runtime("Transmission controller is closed"));
                }
                let mut replacement = Self::new(payload(args, kwargs, "configuration")?)?;
                replacement.next_id = self.next_id;
                *self = replacement;
                Ok(self.reply())
            }
            "start" => {
                ctx.check_cancelled()?;
                let p = payload(args, kwargs, "plan")?;
                let mode = p.get("mode").and_then(Value::as_str).unwrap_or("run");
                self.begin(mode, p)?;
                self.drive(ctx)
            }
            "run" => {
                no_arguments(args, kwargs)?;
                ctx.check_cancelled()?;
                self.begin("run", &json!({}))?;
                self.drive(ctx)
            }
            "spectrum" => {
                ctx.check_cancelled()?;
                self.begin("spectrum", payload(args, kwargs, "plan")?)?;
                self.drive(ctx)
            }
            "restore_initial" => {
                no_arguments(args, kwargs)?;
                ctx.check_cancelled()?;
                self.begin("revert", &json!({}))?;
                self.drive(ctx)
            }
            "observe" => self.observe(payload(args, kwargs, "observation")?, ctx),
            "cancel" => {
                no_arguments(args, kwargs)?;
                if self.active {
                    self.cancel_requested = true;
                }
                self.drive(ctx)
            }
            "state" => {
                no_arguments(args, kwargs)?;
                Ok(self.reply())
            }
            "summary" => {
                no_arguments(args, kwargs)?;
                Ok(self.summary())
            }
            "shutdown" => {
                no_arguments(args, kwargs)?;
                if self.active {
                    self.cancel_requested = true;
                    return self.drive(ctx);
                }
                self.closed = true;
                Ok(
                    json!({"status":"closed","reason":"Devices remain owned by Explorer; this is not OFF confirmation"}),
                )
            }
            "fit_peak" => {
                let p = payload(args, kwargs, "data")?;
                let arrays = |n: &str| -> Result<Vec<f64>> {
                    let a = p[n]
                        .as_array()
                        .ok_or_else(|| Error::argument(format!("{n} must be an array")))?;
                    if a.len() > 4097 {
                        return Err(Error::argument("Peak array exceeds native capacity"));
                    }
                    a.iter().map(telemetry).collect()
                };
                let (height, error, center) = fit_peak(
                    &arrays("amplitudes")?,
                    &arrays("currents")?,
                    &arrays("sems")?,
                )?;
                Ok(crate::codec::tuple(vec![vf(height), vf(error), vf(center)]))
            }
            "pick_peak" => {
                let p = payload(args, kwargs, "data")?;
                let array = |n: &str| -> Result<Vec<f64>> {
                    let a = p[n]
                        .as_array()
                        .ok_or_else(|| Error::argument(format!("{n} must be an array")))?;
                    if a.len() > 4097 {
                        return Err(Error::argument("Spectrum exceeds native capacity"));
                    }
                    a.iter().map(telemetry).collect()
                };
                let near = p["near"]
                    .as_f64()
                    .ok_or_else(|| Error::argument("near must be finite"))?;
                let (center, width) = pick_peak(&array("amplitudes")?, &array("currents")?, near)?;
                Ok(crate::codec::tuple(vec![vf(center), vf(width)]))
            }
            _ => Err(Error::unsupported(format!(
                "Unsupported Transmission method: {method}"
            ))),
        }
    }
    fn get_attribute(&self, name: &str) -> Result<Value> {
        match name {
            "status" => Ok(json!(self.status)),
            "reason" => Ok(json!(self.reason)),
            "initial" => Ok(values_json(&self.initial)),
            "commanded" => Ok(values_json(&self.commanded)),
            "max_step" => Ok(values_json(&self.max_step)),
            "results" => Ok(json!(self.results)),
            "counts" => Ok(self.count_json()),
            "peak_center" => Ok(json!(self.peak_center)),
            "spectra" => Ok(json!(self.spectra)),
            "checks" => Ok(json!(self.checks)),
            "history" => {
                let v = json!(self.history.iter().map(|r| &r.json).collect::<Vec<_>>());
                if serde_json::to_vec(&v)
                    .map_err(|e| Error::runtime(e.to_string()))?
                    .len()
                    > 12 * 1024 * 1024
                {
                    return Err(Error::runtime(
                        "History exceeds RPC capacity; consume evaluation events incrementally",
                    ));
                }
                Ok(v)
            }
            _ => Err(Error::new(
                "AttributeError",
                format!("Unknown Transmission attribute: {name}"),
            )),
        }
    }
}

#[cfg(test)]
mod cancellation_tests {
    use super::*;

    #[test]
    fn cancelled_likelihood_does_not_panic_nelder_mead_initialization() {
        let ctx = Context::test(5.0);
        ctx.cancel();
        let problem = Likelihood {
            x: vec![vec![0.0, 0.0], vec![1.0, 0.1]],
            y: DVector::from_vec(vec![0.0, 1.0]),
            noise: vec![0.0, 0.0],
            bounds: vec![(-4.0, 2.0); 4],
            ctx: ctx.clone(),
        };
        assert_eq!(problem.cost(&vec![0.0; 4]).unwrap(), 1e25);
        let mut simplex = vec![vec![0.0; 4]];
        for axis in 0..4 {
            let mut p = vec![0.0; 4];
            p[axis] = 0.5;
            simplex.push(p);
        }
        let solver = NelderMead::new(simplex).with_sd_tolerance(1e-7).unwrap();
        let result = Executor::new(problem, solver)
            .configure(|s| s.max_iters(100))
            .run();
        assert!(result.is_ok());
        assert!(ctx.check_cancelled().is_err());
    }
}
