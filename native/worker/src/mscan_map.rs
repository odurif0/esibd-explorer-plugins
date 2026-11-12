// SPDX-License-Identifier: GPL-2.0-or-later
//! MScan map engine (v0.5.10): the plan of `docs/quadrupole_optimization_plan.md`.
//!
//! Pure and event-sourced. The journal (normalized plan, instrument readings at Start, every
//! finished line with its analysis) is the whole state: `map_next(journal)` computes the next line
//! from it and `map_record(journal, result)` analyses a finished line and appends it. A restarted
//! worker, an Explorer crash or a power cut lose nothing, and Resume is an ordinary `map_next`.
//! This module never times anything and never touches a device: every line is an ordinary MScan
//! scan run by the Python adapter, which reports its arrays here.
//!
//! RPC (through the MScan controller):
//! - `map_check(plan, instrument)`: problems (Start refused), warnings and a summary;
//! - `map_begin(plan, instrument, started)`: a new journal (refused while problems remain);
//! - `map_next(journal)`: `{decision: line|wait|done|stop, request?, reason?, progress}`;
//! - `map_record(journal, result)`: `{journal, analysis}`;
//! - `map_report(journal)`: laws, points, search, checks, best scan, data for the figures;
//! - `map_simulate(plan, instrument, options)`: the whole plan against the Q2 simulator.
//!
//! Physics (Section 3 of the plan): the ideal 50 % square-wave filter transmits up to
//! k_eff = 0.2153 and R ≈ 0.157 / (0.2153 − k_eff), with k_eff = max(U_min, k·A − δ) / (c·A).
//! A limit that adds its own width adds 1/R_cap. N = f·L·√(m / 2eE) RF cycles in Q2.

use crate::codec::float;
use crate::error::{Error, Result};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::collections::{BTreeMap, HashMap};
use std::sync::{Mutex, OnceLock};

// ---------------------------------------------------------------- physics

/// Largest U/A transmitted by an ideal 50 % square wave.
pub const APEX: f64 = 0.2153;
/// R ≈ R_SLOPE / (APEX − k_eff) near the apex of the square-wave diagram.
pub const R_SLOPE: f64 = 0.157;
const RF_ONLY_Q: f64 = 0.7120;
const E_CHARGE: f64 = 1.602_176_634e-19;
const AMU: f64 = 1.660_539_066_60e-27;

#[derive(Clone, Copy)]
struct Cx {
    re: f64,
    im: f64,
}

impl Cx {
    fn sqrt_of(x: f64) -> Self {
        // The Python reference adds 1e-16 to avoid a division by zero at w = 0.
        if x >= 0.0 {
            Self {
                re: x.sqrt() + 1e-16,
                im: 0.0,
            }
        } else {
            Self {
                re: 1e-16,
                im: (-x).sqrt(),
            }
        }
    }
    fn add(self, o: Self) -> Self {
        Self {
            re: self.re + o.re,
            im: self.im + o.im,
        }
    }
    fn sub(self, o: Self) -> Self {
        Self {
            re: self.re - o.re,
            im: self.im - o.im,
        }
    }
    fn mul(self, o: Self) -> Self {
        Self {
            re: self.re * o.re - self.im * o.im,
            im: self.re * o.im + self.im * o.re,
        }
    }
    fn div(self, o: Self) -> Self {
        let d = o.re * o.re + o.im * o.im;
        Self {
            re: (self.re * o.re + self.im * o.im) / d,
            im: (self.im * o.re - self.re * o.im) / d,
        }
    }
    fn scale(self, s: f64) -> Self {
        Self {
            re: self.re * s,
            im: self.im * s,
        }
    }
    fn cos(self) -> Self {
        Self {
            re: self.re.cos() * self.im.cosh(),
            im: -self.re.sin() * self.im.sinh(),
        }
    }
    fn sin(self) -> Self {
        Self {
            re: self.re.sin() * self.im.cosh(),
            im: self.re.cos() * self.im.sinh(),
        }
    }
}

/// Half-trace of the one-period transfer matrix of the 50 % square wave (x direction).
fn half_trace(a: f64, q: f64) -> f64 {
    let w1 = Cx::sqrt_of(a + 2.0 * q);
    let w2 = Cx::sqrt_of(a - 2.0 * q);
    let h = std::f64::consts::FRAC_PI_2;
    let first = w1.scale(h).cos().mul(w2.scale(h).cos()).scale(2.0);
    let ratio = w1.div(w2).add(w2.div(w1));
    let second = ratio.mul(w1.scale(h).sin()).mul(w2.scale(h).sin());
    first.sub(second).re
}

fn stable(k: f64, q: f64) -> bool {
    let a = 2.0 * k * q;
    half_trace(a, q).abs() < 2.0 && half_trace(-a, -q).abs() < 2.0
}

type BandCache = Mutex<HashMap<i64, Option<(f64, f64)>>>;

fn bands() -> &'static BandCache {
    static BANDS: OnceLock<BandCache> = OnceLock::new();
    BANDS.get_or_init(|| Mutex::new(HashMap::new()))
}

/// Stable q interval on the scan line a = 2kq; None above the apex. k = 0 is RF only.
pub fn band(k: f64) -> Option<(f64, f64)> {
    let k = k.abs();
    if !k.is_finite() {
        return None;
    }
    if k == 0.0 {
        return Some((0.0, RF_ONLY_Q));
    }
    let key = (k * 1e7).round() as i64;
    if let Some(found) = bands().lock().ok().and_then(|m| m.get(&key).copied()) {
        return found;
    }
    let k = key as f64 / 1e7;
    // A coarse scan finds the stable interval (one interval below the apex), bisection refines it.
    let (lo, hi, n) = (1e-4, 0.8, 800usize);
    let step = (hi - lo) / n as f64;
    let mut first = None;
    let mut last = None;
    for j in 0..=n {
        let q = lo + step * j as f64;
        if stable(k, q) {
            if first.is_none() {
                first = Some(j);
            }
            last = Some(j);
        }
    }
    if first.is_none() && k > 0.19 && k < APEX + 1e-3 {
        // Near the apex the band is narrower than the coarse step: look closely where it closes.
        let (fine_lo, fine_n) = (0.55, 125_000usize);
        let fine = (hi - fine_lo) / fine_n as f64;
        let mut found: Option<(f64, f64)> = None;
        for j in 0..=fine_n {
            let q = fine_lo + fine * j as f64;
            if stable(k, q) {
                found = Some(found.map_or((q, q), |f| (f.0, q)));
            }
        }
        let result = found;
        if let Ok(mut m) = bands().lock() {
            m.insert(key, result);
        }
        return result;
    }
    let result = match (first, last) {
        (Some(f), Some(l)) => {
            let refine = |mut inside: f64, mut outside: f64| {
                for _ in 0..40 {
                    let mid = 0.5 * (inside + outside);
                    if stable(k, mid) {
                        inside = mid;
                    } else {
                        outside = mid;
                    }
                }
                inside
            };
            let q1 = if f == 0 {
                lo
            } else {
                refine(lo + step * f as f64, lo + step * (f - 1) as f64)
            };
            let q2 = if l == n {
                hi
            } else {
                refine(lo + step * l as f64, lo + step * (l + 1) as f64)
            };
            Some((q1, q2))
        }
        _ => None,
    };
    if let Ok(mut m) = bands().lock() {
        if m.len() > 200_000 {
            m.clear();
        }
        m.insert(key, result);
    }
    result
}

/// The filter of one line: what turns an amplitude (or a frequency) into m/z.
#[derive(Clone, Copy, Debug)]
pub struct Filter {
    pub k: f64,
    pub delta: f64,
    pub umin: f64,
    pub c: f64,
    pub r0_mm: f64,
}

impl Filter {
    /// U/(c·A): the ratio the ions see at amplitude A.
    pub fn k_eff(&self, a: f64) -> f64 {
        if self.k == 0.0 {
            return 0.0;
        }
        (self.k.abs() * a - self.delta).max(self.umin) / (self.c * a)
    }
    /// m/z at the centre of the transmitted band (RF only: the low-mass cut-off).
    pub fn mz(&self, a: f64, f_hz: f64) -> Option<f64> {
        if !(a > 0.0 && f_hz > 0.0) {
            return None;
        }
        let omega = 2.0 * std::f64::consts::PI * f_hz;
        let scale = 4.0 * E_CHARGE / AMU / ((self.r0_mm * 1e-3).powi(2) * omega * omega);
        let rods = self.c * a;
        let (q1, q2) = band(self.k_eff(a))?;
        let q = if self.k == 0.0 { q2 } else { 0.5 * (q1 + q2) };
        Some(scale * rods / q)
    }
    fn cache_key(&self, mz: f64, other: f64) -> [i64; 7] {
        [
            (mz * 1e4).round() as i64,
            (other * 1e3).round() as i64,
            (self.k * 1e8).round() as i64,
            (self.delta * 1e6).round() as i64,
            (self.umin * 1e6).round() as i64,
            (self.c * 1e8).round() as i64,
            (self.r0_mm * 1e8).round() as i64,
        ]
    }

    /// Amplitude at which `mz` is at the band centre, at frequency f.
    pub fn amplitude_for(&self, mz: f64, f_hz: f64) -> Option<f64> {
        static CACHE: OnceLock<Mutex<HashMap<[i64; 7], Option<f64>>>> = OnceLock::new();
        let cache = CACHE.get_or_init(|| Mutex::new(HashMap::new()));
        let key = self.cache_key(mz, f_hz);
        if let Some(found) = cache.lock().ok().and_then(|m| m.get(&key).copied()) {
            return found;
        }
        let found = self
            .amplitude_search(mz, f_hz)
            .or_else(|| self.near_apex()?.amplitude_search(mz, f_hz));
        if let Ok(mut m) = cache.lock() {
            if m.len() > 500_000 {
                m.clear();
            }
            m.insert(key, found);
        }
        found
    }

    fn amplitude_search(&self, mz: f64, f_hz: f64) -> Option<f64> {
        let grid: Vec<f64> = (0..=60)
            .map(|i| 0.5 * (10000.0f64).powf(i as f64 / 60.0))
            .collect();
        let values: Vec<Option<f64>> = grid.iter().map(|a| self.mz(*a, f_hz)).collect();
        for i in 0..grid.len() - 1 {
            if let (Some(m0), Some(m1)) = (values[i], values[i + 1])
                && m0 <= mz
                && mz <= m1
            {
                let (mut lo, mut hi) = (grid[i], grid[i + 1]);
                for _ in 0..45 {
                    let mid = (lo * hi).sqrt();
                    match self.mz(mid, f_hz) {
                        Some(m) if m < mz => lo = mid,
                        Some(_) => hi = mid,
                        None => hi = mid,
                    }
                }
                return Some((lo * hi).sqrt());
            }
        }
        None
    }
    /// The same filter just below the apex: above it no band exists, but the ladders still need
    /// to know where each ion would be (k beyond the expected closing is measured on purpose).
    fn near_apex(&self) -> Option<Self> {
        (self.k != 0.0).then(|| Self {
            k: self.k.abs().min(0.999 * APEX * self.c),
            ..*self
        })
    }

    /// Frequency at which `mz` is at the band centre, at fixed amplitude A (U fixed too).
    pub fn frequency_for(&self, mz: f64, a: f64) -> Option<f64> {
        let reference = 500e3;
        let m = self
            .mz(a, reference)
            .or_else(|| self.near_apex()?.mz(a, reference))?;
        Some(reference * (m / mz).sqrt())
    }
}

/// RF cycles an ion of mass `mz` (singly charged) spends in a quadrupole of length `length_m`.
pub fn cycles(f_hz: f64, mz: f64, energy_ev: f64, length_m: f64) -> f64 {
    f_hz * length_m * (mz * AMU / (2.0 * E_CHARGE * energy_ev)).sqrt()
}

/// Ideal resolution of the filter at effective ratio k_eff (infinite at and above the apex).
pub fn ideal_r(k_eff: f64) -> f64 {
    if k_eff >= APEX {
        f64::INFINITY
    } else {
        R_SLOPE / (APEX - k_eff)
    }
}

// ---------------------------------------------------------------- numerics

/// SplitMix64: small, deterministic, good enough for designs, orders and the simulator.
#[derive(Clone)]
pub struct Rng(u64);

impl Rng {
    pub fn new(seed: u64) -> Self {
        Self(seed ^ 0x9E37_79B9_7F4A_7C15)
    }
    pub fn next_u64(&mut self) -> u64 {
        self.0 = self.0.wrapping_add(0x9E37_79B9_7F4A_7C15);
        let mut z = self.0;
        z = (z ^ (z >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
        z = (z ^ (z >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
        z ^ (z >> 31)
    }
    pub fn uniform(&mut self) -> f64 {
        (self.next_u64() >> 11) as f64 / (1u64 << 53) as f64
    }
    pub fn normal(&mut self) -> f64 {
        let u1 = self.uniform().max(1e-300);
        let u2 = self.uniform();
        (-2.0 * u1.ln()).sqrt() * (2.0 * std::f64::consts::PI * u2).cos()
    }
    pub fn permutation(&mut self, n: usize) -> Vec<usize> {
        let mut order: Vec<usize> = (0..n).collect();
        for i in (1..n).rev() {
            let j = (self.next_u64() % (i as u64 + 1)) as usize;
            order.swap(i, j);
        }
        order
    }
}

/// Point `i` of the two-dimensional Sobol sequence (natural order; point 0 is the origin).
pub fn sobol2(i: u32) -> (f64, f64) {
    let mut m = [0u32; 32];
    m[0] = 1;
    for k in 1..32 {
        m[k] = (m[k - 1] << 1) ^ m[k - 1];
    }
    let (mut x, mut y) = (0u32, 0u32);
    for (bit, direction) in m.iter().enumerate() {
        if i >> bit & 1 == 1 {
            x ^= 1u32 << (31 - bit);
            y ^= direction << (31 - bit);
        }
    }
    (x as f64 / 4_294_967_296.0, y as f64 / 4_294_967_296.0)
}

/// Least squares by normal equations with partial pivoting; None when singular.
pub fn lstsq(rows: &[Vec<f64>], b: &[f64]) -> Option<Vec<f64>> {
    let n = rows.first()?.len();
    if rows.len() < n || n == 0 {
        return None;
    }
    let mut m = vec![vec![0.0; n + 1]; n];
    for (row, y) in rows.iter().zip(b) {
        for i in 0..n {
            for j in 0..n {
                m[i][j] += row[i] * row[j];
            }
            m[i][n] += row[i] * y;
        }
    }
    let scale = (0..n)
        .map(|i| m[i][i].abs())
        .fold(0.0, f64::max)
        .max(1e-300);
    for (i, row) in m.iter_mut().enumerate() {
        row[i] += 1e-12 * scale;
    }
    for col in 0..n {
        let pivot = (col..n).max_by(|a, b| m[*a][col].abs().total_cmp(&m[*b][col].abs()))?;
        if m[pivot][col].abs() < 1e-14 * scale {
            return None;
        }
        m.swap(col, pivot);
        let pivot_row = m[col].clone();
        for (r, row) in m.iter_mut().enumerate() {
            if r != col {
                let f = row[col] / pivot_row[col];
                for (value, p) in row.iter_mut().zip(&pivot_row).skip(col) {
                    *value -= f * p;
                }
            }
        }
    }
    let x: Vec<f64> = (0..n).map(|i| m[i][n] / m[i][i]).collect();
    x.iter().all(|v| v.is_finite()).then_some(x)
}

fn median(values: &[f64]) -> Option<f64> {
    let mut v: Vec<f64> = values.iter().copied().filter(|x| x.is_finite()).collect();
    if v.is_empty() {
        return None;
    }
    v.sort_by(f64::total_cmp);
    let n = v.len();
    Some(if n % 2 == 1 {
        v[n / 2]
    } else {
        0.5 * (v[n / 2 - 1] + v[n / 2])
    })
}

/// Robust standard deviation (1.4826 × median absolute deviation).
fn robust_sigma(values: &[f64]) -> Option<f64> {
    let m = median(values)?;
    let dev: Vec<f64> = values
        .iter()
        .filter(|x| x.is_finite())
        .map(|x| (x - m).abs())
        .collect();
    median(&dev).map(|d| 1.4826 * d)
}

fn mean(values: &[f64]) -> Option<f64> {
    let v: Vec<f64> = values.iter().copied().filter(|x| x.is_finite()).collect();
    (!v.is_empty()).then(|| v.iter().sum::<f64>() / v.len() as f64)
}

fn std_dev(values: &[f64]) -> Option<f64> {
    let v: Vec<f64> = values.iter().copied().filter(|x| x.is_finite()).collect();
    if v.len() < 2 {
        return None;
    }
    let m = v.iter().sum::<f64>() / v.len() as f64;
    Some((v.iter().map(|x| (x - m).powi(2)).sum::<f64>() / (v.len() - 1) as f64).sqrt())
}

/// Complementary error function (Abramowitz and Stegun 7.1.26, |error| < 1.5e-7).
fn erfc(x: f64) -> f64 {
    let z = x.abs();
    let t = 1.0 / (1.0 + 0.327_591_1 * z);
    let poly = t
        * (0.254_829_592
            + t * (-0.284_496_736
                + t * (1.421_413_741 + t * (-1.453_152_027 + t * 1.061_405_429))));
    let r = poly * (-z * z).exp();
    if x >= 0.0 { r } else { 2.0 - r }
}

fn round_to(v: f64, step: f64) -> f64 {
    (v / step).round() * step
}

// ---------------------------------------------------------------- plan

macro_rules! defaults {
    ($name:ident { $($field:ident : $ty:ty = $value:expr),* $(,)? }) => {
        #[derive(Clone, Debug, Serialize, Deserialize)]
        #[serde(default, deny_unknown_fields)]
        pub struct $name { $(pub $field: $ty),* }
        impl Default for $name {
            fn default() -> Self { Self { $($field: $value),* } }
        }
    };
}

defaults!(Bounds {
    // 2026-11-12: at k = 0.200, 2122 sits at 339 V at 640 kHz and 922–2122 are above 20 V at
    // 250 kHz. The RF current follows from f and A (≈ 1.47 nA/(Hz·V) · f · A, 307 mA at 600 kHz
    // and 348 V): an Ilim of 330 mA, set by the operator; P9 (900 kHz) needed 490 mA.
    f_khz: [f64; 2] = [250.0, 640.0],
    energy_ev: [f64; 2] = [3.0, 30.0],
    delta_v: [f64; 2] = [0.0, 2.0],
    stub_below_q1_v: f64 = 1.0,
});

defaults!(Design {
    // (f kHz, E eV) of the amplitude-scan points, then (A V, E eV) of the frequency-scan points.
    amplitude: Vec<[f64; 2]> = vec![[500.0, 26.0], [500.0, 12.0], [500.0, 5.0], [300.0, 26.0],
        [300.0, 9.4], [300.0, 4.3], [550.0, 26.0], [550.0, 8.0]],
    frequency: Vec<[f64; 2]> = vec![[110.0, 26.0], [110.0, 8.0]],
});

defaults!(Ladder {
    first_k: f64 = 0.200,
    distances: Vec<f64> = vec![0.005, 0.0025, 0.0012, 0.0006, 0.0003],
    finest_k: f64 = 0.00025,
    beyond_step: f64 = 0.0003,
    repeats: usize = 2,
    max_lines: usize = 14,
});

defaults!(StubPlan {
    s: f64 = 0.42,
    cut_from_x_v: f64 = -10.0,
    cut_to_q1_v: f64 = -2.0,
    cut_step_v: f64 = 1.0,
    significant: f64 = 0.10,
});

defaults!(Stopping {
    f_khz: f64 = 500.0,
    k: f64 = 0.200,
    ions: Vec<f64> = vec![922.01, 2121.93],
    x_from_v: f64 = 25.0,
    x_to_v: f64 = 45.0,
    x_step_v: f64 = 0.25,
    q1_shift_v: f64 = 2.0,
    repeats: usize = 2,
});

defaults!(Reference {
    f_khz: f64 = 500.0,
    k: f64 = 0.200,
    energy_ev: f64 = 26.0,
    ion: f64 = 922.01,
    window: f64 = 0.10,
});

defaults!(Outer {
    max_ladders: usize = 6,
    radius: f64 = 0.08,
});

defaults!(Validation {
    points: usize = 10,
    pass: usize = 8,
});

defaults!(Local {
    f_rel: f64 = 0.10,
    e_rel: f64 = 0.25,
    stub_v: f64 = 2.0,
    k: f64 = 0.0005,
    delta_v: f64 = 0.25,
    repeats: usize = 2,
});

defaults!(Best {
    repeats: usize = 5,
    spacing_s: f64 = 3600.0,
});

defaults!(Calibration {
    c: f64 = 0.978,
    r0_mm: f64 = 2.625,
    u_min_v: f64 = 0.5,
});

defaults!(Stages {
    stopping: bool = true,
    design: bool = true,
    search: bool = true,
    validation: bool = true,
    local: bool = true,
    best: bool = true,
});

defaults!(Channels {
    q1_dc: String = "Q1_DC".to_owned(),
    aperture: String = "Ap_Q1_Q2".to_owned(),
    stub_entry: String = "Stub_Entry_Q2".to_owned(),
});

fn default_mix() -> Vec<f64> {
    vec![118.09, 322.05, 622.03, 922.01, 1521.97, 2121.93, 2721.89]
}

fn default_follow() -> BTreeMap<String, f64> {
    BTreeMap::from([("Stub_Exit_Q2".to_owned(), -15.0)])
}

defaults!(MapConfig {
    name: String = "q2-full".to_owned(),
    mix: Vec<f64> = default_mix(),
    // 2026-11-04: 322 sits unresolved on the low-A background at k = 0.200 and 2722 never rose
    // above the noise (nor on 2026-11-03); an ion the beam lacks makes every point infeasible.
    best_scan_range: [f64; 2] = [622.0, 2122.0],
    r_target: f64 = 40.0,
        r_max: f64 = 100.0,
    r_noise: f64 = 0.10,
    a_range_v: [f64; 2] = [20.0, 348.0],
    // An amplitude line spans the best-range ions it holds ± this fraction of their amplitude
    // (within a_range_v): each peak, its ±6 % search window and baseline on both sides at R ≈ 10.
    a_margin: f64 = 0.20,
    min_ions_in_line: usize = 2,
    reference_every: usize = 5,
    // 2026-11-04: the ESI current went 42 → 48 → 36 nA while the M03 peaks stayed within 2 %.
    spray_window_percent: f64 = 40.0,
    // A fixed ESI current range in nA, [low, high], instead of the window around the reference
    // (user, 2026-11-12); empty: the window. Checked by MScan before each line and over each line.
    spray_range_na: Vec<f64> = Vec::new(),
    seed: u64 = 1,
    detector: String = "M03".to_owned(),
    point_s: f64 = 0.8,
    settle_s: f64 = 0.3,
    // Command and readback confirmation of each point, until the map has timed its own points
    // (2026-11-04: a line took 2.02 s per point + 15.5 s).
    point_overhead_s: f64 = 0.9,
    q2_length_m: f64 = 0.15,
    max_failures: usize = 3,
    max_lines: usize = 600,
    coarse: usize = 4,
    bounds: Bounds = Bounds::default(),
    design: Design = Design::default(),
    ladder: Ladder = Ladder::default(),
    stub: StubPlan = StubPlan::default(),
    follow: BTreeMap<String, f64> = default_follow(),
    stopping: Stopping = Stopping::default(),
    reference: Reference = Reference::default(),
    outer: Outer = Outer::default(),
    validation: Validation = Validation::default(),
    local: Local = Local::default(),
    best: Best = Best::default(),
    calibration: Calibration = Calibration::default(),
    stages: Stages = Stages::default(),
    channels: Channels = Channels::default(),
});

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct Plan {
    pub map: MapConfig,
}

defaults!(Instrument {
    // Read live by MScan at Start; frozen in the journal so that Resume uses the same numbers.
    q1_dc_v: f64 = 36.0,
    ap_q1_q2_v: f64 = 34.0,
    x_v: f64 = 10.0,
    ilim_a: f64 = 0.5,
    // RF current per (Hz · V) of amplitude: 150 mA at 250 V and 500 kHz on 2026-11-03.
    current_per_hz_v: f64 = 0.150 / (500e3 * 250.0),
    psu_max_v: f64 = 350.0,
    full_range: bool = true,
    pair_min_v: f64 = -1000.0,
    pair_max_v: f64 = 1000.0,
    stub_min_v: f64 = -1000.0,
    stub_max_v: f64 = 1000.0,
    q1_max_v: f64 = 1000.0,
    clock_hz: f64 = 100e6,
    f_hz: f64 = 500e3,
    frequency_writable: bool = true,
    free_disk_bytes: f64 = 1e18,
});

impl MapConfig {
    fn filter(&self, k: f64, delta: f64) -> Filter {
        Filter {
            k,
            delta,
            umin: self.calibration.u_min_v,
            c: self.calibration.c,
            r0_mm: self.calibration.r0_mm,
        }
    }
    fn in_best_range(&self, mz: f64) -> bool {
        mz >= self.best_scan_range[0] - 1.0 && mz <= self.best_scan_range[1] + 1.0
    }
    fn best_ions(&self) -> Vec<f64> {
        self.mix
            .iter()
            .copied()
            .filter(|m| self.in_best_range(*m))
            .collect()
    }
}

// ---------------------------------------------------------------- requests, records, journal

pub const KIND_AMPLITUDE: &str = "amplitude";
pub const KIND_OFFSET: &str = "offset";
pub const KIND_EXTRA: &str = "extra";
pub const KIND_FREQUENCY: &str = "frequency";
pub const KIND_WAIT: &str = "wait";

/// One line of the map, as MScan runs it. Every number is what the line sets, never a prediction.
#[derive(Clone, Debug, Default, Serialize, Deserialize, PartialEq)]
#[serde(default)]
pub struct Request {
    pub id: u64,
    /// R reference, A stopping cut, B design ladders, D outer search, E out-of-sample check,
    /// F local check, G best scan.
    pub stage: String,
    pub role: String,
    pub point: String,
    pub variant: String,
    /// amplitude, offset (x), extra (entry stub), frequency or wait.
    pub kind: String,
    pub f_hz: f64,
    pub k: f64,
    pub delta_v: f64,
    pub energy_ev: f64,
    pub x_v: f64,
    pub q1_dc_v: f64,
    pub stub_v: f64,
    /// Fixed amplitude of offset, extra and frequency lines; 0 for amplitude lines.
    pub amplitude_v: f64,
    pub axis: Vec<f64>,
    /// The measurement-driven step: `axis` is the fine grid, MScan measures a subset of it.
    pub adaptive: bool,
    pub coarse: usize,
    pub ions: Vec<f64>,
    pub expected_s: f64,
    pub seconds: f64,
    /// Merit the laws predict for this line (out-of-sample check); 0 when none.
    pub predicted: f64,
    pub label: String,
}

impl Request {
    /// Identity of a decision, independent of its sequence number (a failed line is decided again).
    fn key(&self) -> String {
        format!(
            "{}|{}|{}|{}|{}|{:.1}|{:.6}|{:.3}|{:.3}|{:.3}|{:.3}|{:.3}",
            self.stage,
            self.role,
            self.point,
            self.variant,
            self.kind,
            self.f_hz,
            self.k,
            self.delta_v,
            self.x_v,
            self.q1_dc_v,
            self.stub_v,
            self.amplitude_v
        )
    }
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
#[serde(default)]
pub struct Peak {
    pub mz: f64,
    pub found: bool,
    /// Expected and measured centre in axis units (V or Hz).
    pub expected: f64,
    pub center: f64,
    pub height: f64,
    pub area: f64,
    pub fwhm: f64,
    /// m/Δm at half height; 0 when the half-height crossings were not both found.
    pub r: f64,
    pub baseline: f64,
    pub noise: f64,
    pub flags: Vec<String>,
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
#[serde(default)]
pub struct Transition {
    pub found: bool,
    pub x50: f64,
    pub width: f64,
    pub plateau: f64,
    pub floor: f64,
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
#[serde(default)]
pub struct Profile {
    pub found: bool,
    pub best: f64,
    pub max: f64,
    pub min: f64,
    pub variation: f64,
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
#[serde(default)]
pub struct Analysis {
    pub peaks: Vec<Peak>,
    pub transition: Option<Transition>,
    pub profile: Option<Profile>,
    pub measured: usize,
    pub noise: f64,
    pub spray_mean: f64,
    pub spray_rel_std: f64,
}

fn finite_or_zero(v: &mut f64) {
    if !v.is_finite() {
        *v = 0.0;
    }
}

impl Analysis {
    /// JSON has no NaN: every stored number is finite (0 with a flag or `found = false` instead).
    fn sanitize(&mut self) {
        for p in &mut self.peaks {
            for v in [
                &mut p.mz,
                &mut p.expected,
                &mut p.center,
                &mut p.height,
                &mut p.area,
                &mut p.fwhm,
                &mut p.r,
                &mut p.baseline,
                &mut p.noise,
            ] {
                finite_or_zero(v);
            }
        }
        if let Some(t) = &mut self.transition {
            for v in [&mut t.x50, &mut t.width, &mut t.plateau, &mut t.floor] {
                finite_or_zero(v);
            }
        }
        if let Some(p) = &mut self.profile {
            for v in [&mut p.best, &mut p.max, &mut p.min, &mut p.variation] {
                finite_or_zero(v);
            }
        }
        for v in [
            &mut self.noise,
            &mut self.spray_mean,
            &mut self.spray_rel_std,
        ] {
            finite_or_zero(v);
        }
    }

    fn peak(&self, mz: f64) -> Option<&Peak> {
        self.peaks
            .iter()
            .find(|p| (p.mz - mz).abs() < 0.5 && p.found)
    }
    fn any_signal(&self) -> bool {
        self.peaks.iter().any(|p| p.found)
    }
}

pub const DONE: &str = "done";
pub const FAILED: &str = "failed";
pub const UNREACHABLE: &str = "unreachable";

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
#[serde(default)]
pub struct Record {
    pub request: Request,
    /// done, failed (repeated), unreachable (a hardware limit closes that setting).
    pub outcome: String,
    pub error: String,
    pub started: f64,
    pub duration_s: f64,
    pub file: String,
    pub analysis: Analysis,
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
#[serde(default)]
pub struct Journal {
    pub version: u32,
    pub engine: String,
    pub plan: Plan,
    pub instrument: Instrument,
    pub started: f64,
    pub records: Vec<Record>,
}

pub const JOURNAL_VERSION: u32 = 1;

// ---------------------------------------------------------------- measurement-driven step

/// The step policy shared by the MScan line engine and the simulator: the line measures a subset
/// of a fine grid, coarse where the signal is flat, every fine point where it changes.
#[derive(Clone, Copy, Debug)]
pub struct StepPolicy {
    pub coarse: usize,
    pub sigma: f64,
    pub fraction: f64,
    pub noise_floor: f64,
}

impl Default for StepPolicy {
    fn default() -> Self {
        Self {
            coarse: 4,
            sigma: 3.0,
            fraction: 0.10,
            noise_floor: 0.0,
        }
    }
}

/// State of one adaptive line: which grid points are measured, where the cursor is.
#[derive(Clone, Debug)]
pub struct Stepper {
    policy: StepPolicy,
    values: Vec<Option<(f64, f64)>>,
    cursor: Option<usize>,
    level: usize,
}

impl Stepper {
    pub fn new(policy: StepPolicy, points: usize) -> Self {
        let coarse = policy.coarse.max(1);
        Self {
            policy: StepPolicy { coarse, ..policy },
            values: vec![None; points],
            cursor: None,
            level: coarse,
        }
    }

    pub fn measured(&self, index: usize) -> bool {
        self.values.get(index).is_some_and(Option::is_some)
    }

    /// Record a point: its mean and the standard deviation of that mean (NaN when unknown).
    pub fn record(&mut self, index: usize, mean: f64, sigma: f64) {
        if index < self.values.len() {
            self.values[index] = Some((mean, sigma));
        }
    }

    fn significant(&self, from: usize, to: usize) -> bool {
        let (Some((a, sa)), Some((b, sb))) = (self.values[from], self.values[to]) else {
            return false;
        };
        if !(a.is_finite() && b.is_finite()) {
            return true; // A missing reading is examined closely rather than skipped.
        }
        let floor = self.policy.noise_floor;
        let sa = if sa.is_finite() { sa.max(floor) } else { floor };
        let sb = if sb.is_finite() { sb.max(floor) } else { floor };
        let change = (b - a).abs();
        let window = 8 * self.policy.coarse;
        let lo = to.saturating_sub(window);
        let near: Vec<f64> = self.values[lo..=to.max(from)]
            .iter()
            .filter_map(|v| v.map(|x| x.0))
            .filter(|v| v.is_finite())
            .collect();
        let height = near.iter().copied().fold(f64::NEG_INFINITY, f64::max)
            - near.iter().copied().fold(f64::INFINITY, f64::min);
        change > self.policy.sigma * (sa * sa + sb * sb).sqrt()
            && change > self.policy.fraction * height.max(0.0)
    }

    /// The next grid index to measure, or None when the line is complete.
    pub fn next_index(&mut self) -> Option<usize> {
        let last = self.values.len().checked_sub(1)?;
        loop {
            let Some(cursor) = self.cursor else {
                if self.measured(0) {
                    self.cursor = Some(0);
                    continue;
                }
                return Some(0);
            };
            if cursor >= last {
                return None;
            }
            let ahead = (cursor + 1..=last).find(|i| self.measured(*i));
            let mut candidate = (cursor + self.level).min(last);
            if let Some(a) = ahead {
                candidate = candidate.min(a);
            }
            if !self.measured(candidate) {
                return Some(candidate);
            }
            // Compare the cursor with the next measured point: refine between them on a change.
            if self.level > 1 && candidate > cursor + 1 && self.significant(cursor, candidate) {
                self.level = (self.level / 2).max(1);
                continue;
            }
            let changed = self.significant(cursor, candidate);
            self.cursor = Some(candidate);
            self.level = if changed {
                1
            } else {
                (self.level * 2).min(self.policy.coarse)
            };
        }
    }

    pub fn measured_count(&self) -> usize {
        self.values.iter().filter(|v| v.is_some()).count()
    }
}

/// Geometric amplitude grid from lo to hi: fine step A / (2·coarse·R_max), so that the coarse
/// step is A / (2·R_max) as the plan requires.
pub fn amplitude_grid(lo: f64, hi: f64, r_max: f64, coarse: usize) -> Vec<f64> {
    let ratio = 1.0 + 1.0 / (2.0 * coarse.max(1) as f64 * r_max.max(1.0));
    let mut grid = Vec::new();
    let mut a = lo;
    while a < hi * (1.0 - 1e-12) {
        grid.push(round_to(a, 1e-4));
        a *= ratio;
    }
    grid.push(round_to(hi, 1e-4));
    grid.dedup_by(|a, b| (*a - *b).abs() < 5e-5);
    grid
}

/// Frequencies of whole oscillator periods between lo and hi, increasing.
pub fn frequency_grid(lo_hz: f64, hi_hz: f64, clock_hz: f64) -> Vec<f64> {
    let longest = (clock_hz / lo_hz).floor() as i64;
    let shortest = (clock_hz / hi_hz).ceil() as i64;
    (shortest..=longest)
        .rev()
        .map(|p| clock_hz / p as f64)
        .collect()
}

fn linear_grid(from: f64, to: f64, step: f64) -> Vec<f64> {
    let n = ((to - from) / step + 1e-9).floor().max(0.0) as usize;
    (0..=n)
        .map(|i| round_to(from + step * i as f64, 1e-6))
        .collect()
}

// ---------------------------------------------------------------- analysis of one finished line

fn number_value(value: &Value) -> f64 {
    if let Some(v) = value.as_f64() {
        return v;
    }
    match value.get("$float").and_then(Value::as_str) {
        Some("inf") => f64::INFINITY,
        Some("-inf") => f64::NEG_INFINITY,
        _ => f64::NAN,
    }
}

fn numbers<'de, D: serde::Deserializer<'de>>(d: D) -> std::result::Result<Vec<f64>, D::Error> {
    Ok(Option::<Vec<Value>>::deserialize(d)?
        .unwrap_or_default()
        .iter()
        .map(number_value)
        .collect())
}

fn one_number<'de, D: serde::Deserializer<'de>>(d: D) -> std::result::Result<f64, D::Error> {
    Ok(Option::<Value>::deserialize(d)?
        .as_ref()
        .map_or(f64::NAN, number_value))
}

/// What MScan reports for a finished line: arrays on the request's grid, NaN where not measured.
#[derive(Clone, Debug, Default, Deserialize)]
#[serde(default)]
pub struct LineResult {
    pub id: u64,
    pub outcome: String,
    pub error: String,
    #[serde(deserialize_with = "numbers")]
    pub axis: Vec<f64>,
    #[serde(deserialize_with = "numbers")]
    pub signal: Vec<f64>,
    #[serde(deserialize_with = "numbers")]
    pub sigma: Vec<f64>,
    #[serde(deserialize_with = "numbers")]
    pub spray: Vec<f64>,
    #[serde(deserialize_with = "one_number")]
    pub started: f64,
    #[serde(deserialize_with = "one_number")]
    pub duration_s: f64,
    pub file: String,
}

fn points_of(axis: &[f64], signal: &[f64]) -> Vec<(f64, f64)> {
    let mut pts: Vec<(f64, f64)> = axis
        .iter()
        .zip(signal)
        .filter(|(x, y)| x.is_finite() && y.is_finite())
        .map(|(x, y)| (*x, *y))
        .collect();
    pts.sort_by(|a, b| a.0.total_cmp(&b.0));
    pts
}

/// Baseline and noise around `center`: the lower 40 % of the points within ±30 %, which works for
/// broad peaks (R ≈ 14) as well as narrow ones, and for the background of M03 that depends on A.
fn local_baseline(pts: &[(f64, f64)], center: f64) -> Option<(f64, f64)> {
    let near: Vec<f64> = pts
        .iter()
        .filter(|(x, _)| (*x - center).abs() <= 0.3 * center.abs())
        .map(|p| p.1)
        .collect();
    if near.len() < 5 {
        return None;
    }
    let mut sorted = near.clone();
    sorted.sort_by(f64::total_cmp);
    let low = &sorted[..(sorted.len() * 2 / 5).max(3)];
    // The noise from point-to-point differences: blind to the peaks and to a sloping background,
    // and not biased low like the spread of the lowest points.
    let diffs: Vec<f64> = near
        .windows(2)
        .map(|w| (w[1] - w[0]) / 2f64.sqrt())
        .collect();
    Some((median(low)?, robust_sigma(&diffs)?.max(1e-30)))
}

fn crossing(pts: &[(f64, f64)], from: usize, step: isize, level: f64, limit: f64) -> Option<f64> {
    let mut i = from as isize;
    loop {
        let j = i + step;
        if j < 0 || j as usize >= pts.len() {
            return None;
        }
        let (x0, y0) = pts[i as usize];
        let (x1, y1) = pts[j as usize];
        if (x1 - pts[from].0).abs() > limit {
            return None;
        }
        if y1 < level {
            let t = (y0 - level) / (y0 - y1);
            return Some(x0 + t * (x1 - x0));
        }
        i = j;
    }
}

/// Centre and FWHM of a Gaussian fitted to the contiguous points above 30 % of the height around
/// `top` (quadratic in ln y); None with fewer than four points or no maximum.
fn gaussian_width(
    pts: &[(f64, f64)],
    top: usize,
    baseline: f64,
    height: f64,
) -> Option<(f64, f64)> {
    let level = baseline + 0.3 * height;
    let mut lo = top;
    while lo > 0 && pts[lo - 1].1 > level {
        lo -= 1;
    }
    let mut hi = top;
    while hi + 1 < pts.len() && pts[hi + 1].1 > level {
        hi += 1;
    }
    if hi - lo + 1 < 4 {
        return None;
    }
    let x0 = pts[top].0;
    let rows: Vec<Vec<f64>> = pts[lo..=hi]
        .iter()
        .map(|p| vec![1.0, p.0 - x0, (p.0 - x0).powi(2)])
        .collect();
    let b: Vec<f64> = pts[lo..=hi]
        .iter()
        .map(|p| (p.1 - baseline).max(1e-300).ln())
        .collect();
    let x = lstsq(&rows, &b)?;
    if x[2] >= 0.0 {
        return None;
    }
    let sigma = (-1.0 / (2.0 * x[2])).sqrt();
    let center = x0 - x[1] / (2.0 * x[2]);
    ((center - x0).abs() < 3.0 * sigma).then_some((center, 2.354_820_045 * sigma))
}

fn analyse_peaks(cfg: &MapConfig, req: &Request, pts: &[(f64, f64)]) -> (Vec<Peak>, f64) {
    let filter = cfg.filter(req.k, req.delta_v);
    let frequency = req.kind == KIND_FREQUENCY;
    let (lo, hi) = match (pts.first(), pts.last()) {
        (Some(a), Some(b)) => (a.0, b.0),
        _ => (f64::NAN, f64::NAN),
    };
    let mut noises = Vec::new();
    let mut peaks = Vec::new();
    for &mz in &req.ions {
        let expected = if frequency {
            filter.frequency_for(mz, req.amplitude_v)
        } else {
            filter.amplitude_for(mz, req.f_hz)
        };
        let mut peak = Peak {
            mz,
            expected: expected.unwrap_or(f64::NAN),
            ..Peak::default()
        };
        let Some(p) = expected else {
            peak.flags.push("no expected position".to_owned());
            peaks.push(peak);
            continue;
        };
        let w = 0.06;
        if p * (1.0 - w) < lo || p * (1.0 + w) > hi {
            peak.flags.push("edge".to_owned());
        }
        let Some((baseline, noise)) = local_baseline(pts, p) else {
            peak.flags.push("too few points".to_owned());
            peaks.push(peak);
            continue;
        };
        noises.push(noise);
        peak.baseline = baseline;
        peak.noise = noise;
        let inside: Vec<usize> = (0..pts.len())
            .filter(|i| (pts[*i].0 - p).abs() <= w * p)
            .collect();
        let Some(&top) = inside
            .iter()
            .max_by(|a, b| pts[**a].1.total_cmp(&pts[**b].1))
        else {
            peak.flags
                .push("no point near the expected position".to_owned());
            peaks.push(peak);
            continue;
        };
        let height = pts[top].1 - baseline;
        peak.height = height;
        peak.center = pts[top].0;
        let above = inside
            .iter()
            .filter(|i| pts[**i].1 > baseline + 0.5 * height)
            .count();
        peak.found = inside.len() >= 2 && above >= 2 && height > 5.0 * noise && height > 0.0;
        if !peak.found {
            peaks.push(peak);
            continue;
        }
        let half = baseline + 0.5 * height;
        let limit = 0.25 * p;
        let left = crossing(pts, top, -1, half, limit);
        let right = crossing(pts, top, 1, half, limit);
        // Centroid of the points above half height around the top.
        let (mut sw, mut sx) = (0.0, 0.0);
        for &(x, y) in pts {
            let inside_half = match (left, right) {
                (Some(l), Some(r)) => x >= l && x <= r,
                _ => (x - pts[top].0).abs() <= 0.02 * p,
            };
            if inside_half && y > half {
                sw += y - baseline;
                sx += (y - baseline) * x;
            }
        }
        if sw > 0.0 {
            peak.center = sx / sw;
        }
        // A Gaussian through the points above 30 % of the height refines the half-height width:
        // the crossings alone follow the noise of the two points around each.
        let (left, right) = match (left, right, gaussian_width(pts, top, baseline, height)) {
            (Some(l), Some(r), Some((center, fwhm))) if fwhm > 0.0 && fwhm < 2.0 * (r - l) => {
                peak.center = center;
                (Some(center - 0.5 * fwhm), Some(center + 0.5 * fwhm))
            }
            other => (other.0, other.1),
        };
        if let (Some(l), Some(r)) = (left, right) {
            peak.fwhm = r - l;
            peak.r = if frequency {
                let m = |x: f64| 1.0 / (x * x);
                m(peak.center) / (m(l) - m(r)).abs()
            } else {
                // Beyond the plan's apex (k measured past the expected closing) the m/z scale of
                // the filter just below it still gives the relative width.
                let scale = if filter.mz(peak.center, req.f_hz).is_some() {
                    filter
                } else {
                    filter.near_apex().unwrap_or(filter)
                };
                match (
                    scale.mz(peak.center, req.f_hz),
                    scale.mz(l, req.f_hz),
                    scale.mz(r, req.f_hz),
                ) {
                    (Some(c), Some(ml), Some(mr)) if (mr - ml).abs() > 0.0 => c / (mr - ml).abs(),
                    _ => 0.0,
                }
            };
            let (a, b) = (l - peak.fwhm, r + peak.fwhm);
            let mut area = 0.0;
            for pair in pts.windows(2) {
                let ((x0, y0), (x1, y1)) = (pair[0], pair[1]);
                if x0 >= a && x1 <= b {
                    area += 0.5 * ((y0 - baseline) + (y1 - baseline)) * (x1 - x0);
                }
            }
            peak.area = area;
        } else {
            peak.flags.push("unresolved".to_owned());
        }
        peaks.push(peak);
    }
    (peaks, median(&noises).unwrap_or(f64::NAN))
}

fn analyse_transition(pts: &[(f64, f64)]) -> Transition {
    let n = pts.len();
    if n < 8 {
        return Transition::default();
    }
    let edge = (n * 3 / 20).max(3);
    let low: Vec<f64> = pts[..edge].iter().map(|p| p.1).collect();
    let high: Vec<f64> = pts[n - edge..].iter().map(|p| p.1).collect();
    let (Some(plateau), Some(floor)) = (median(&low), median(&high)) else {
        return Transition::default();
    };
    let noise = robust_sigma(&high).unwrap_or(0.0).max(1e-30);
    let amplitude = plateau - floor;
    let level = |fraction: f64| {
        let target = floor + fraction * amplitude;
        pts.windows(2).find_map(|w| {
            let ((x0, y0), (x1, y1)) = (w[0], w[1]);
            (y0 >= target && y1 < target).then(|| x0 + (y0 - target) / (y0 - y1) * (x1 - x0))
        })
    };
    match (level(0.5), level(0.84), level(0.16)) {
        (Some(x50), x84, x16) if amplitude > 5.0 * noise => Transition {
            found: true,
            x50,
            width: match (x84, x16) {
                (Some(a), Some(b)) => 0.5 * (b - a).abs(),
                _ => 0.0,
            },
            plateau,
            floor,
        },
        _ => Transition {
            plateau,
            floor,
            ..Transition::default()
        },
    }
}

fn analyse_profile(pts: &[(f64, f64)]) -> Profile {
    if pts.len() < 3 {
        return Profile::default();
    }
    let smooth: Vec<(f64, f64)> = (0..pts.len())
        .map(|i| {
            let lo = i.saturating_sub(1);
            let hi = (i + 1).min(pts.len() - 1);
            let v: Vec<f64> = pts[lo..=hi].iter().map(|p| p.1).collect();
            (pts[i].0, v.iter().sum::<f64>() / v.len() as f64)
        })
        .collect();
    let best = smooth
        .iter()
        .copied()
        .max_by(|a, b| a.1.total_cmp(&b.1))
        .unwrap_or((f64::NAN, f64::NAN));
    let min = smooth.iter().map(|p| p.1).fold(f64::INFINITY, f64::min);
    let noise = robust_sigma(
        &pts.windows(2)
            .map(|w| (w[1].1 - w[0].1) / 2f64.sqrt())
            .collect::<Vec<_>>(),
    )
    .unwrap_or(0.0);
    Profile {
        found: best.1 - min > 5.0 * noise.max(1e-30) || (noise == 0.0 && best.1 > min),
        best: best.0,
        max: best.1,
        min,
        variation: if best.1.abs() > 0.0 {
            (best.1 - min) / best.1.abs()
        } else {
            0.0
        },
    }
}

/// Analyse a finished line. The analysis is stored in the journal and never recomputed, so later
/// fits never change what a past line said.
pub fn analyse(cfg: &MapConfig, req: &Request, result: &LineResult) -> Analysis {
    let pts = points_of(&result.axis, &result.signal);
    let mut analysis = Analysis {
        measured: pts.len(),
        spray_mean: mean(&result.spray).unwrap_or(f64::NAN),
        ..Analysis::default()
    };
    analysis.spray_rel_std = match (std_dev(&result.spray), mean(&result.spray)) {
        (Some(s), Some(m)) if m.abs() > 0.0 => s / m.abs(),
        _ => f64::NAN,
    };
    match req.kind.as_str() {
        KIND_AMPLITUDE | KIND_FREQUENCY => {
            let (peaks, noise) = analyse_peaks(cfg, req, &pts);
            analysis.peaks = peaks;
            analysis.noise = noise;
        }
        KIND_OFFSET => analysis.transition = Some(analyse_transition(&pts)),
        KIND_EXTRA => analysis.profile = Some(analyse_profile(&pts)),
        _ => {}
    }
    analysis.sanitize();
    analysis
}

// ---------------------------------------------------------------- the journal, read back

/// Seconds per line besides its points: approach, static settings, file (2026-11-04: 15.5 s).
const LINE_S: f64 = 15.0;

const ROLE_REFERENCE: &str = "reference";
const ROLE_STOPPING: &str = "stopping";
const ROLE_LADDER: &str = "ladder";
const ROLE_REPEAT: &str = "repeat";
const ROLE_VERIFY: &str = "verify";
const ROLE_STUB: &str = "stub";
const ROLE_VALIDATION: &str = "validation";
const ROLE_LOCAL: &str = "local";
const ROLE_BEST: &str = "best";
const ROLE_WAIT: &str = "wait";

#[derive(Clone, Copy, Debug, PartialEq)]
enum Mode {
    /// Amplitude scans at frequency f (Hz).
    Amplitude(f64),
    /// Frequency scans at amplitude A (V).
    Frequency(f64),
}

#[derive(Clone, Debug)]
struct PointSpec {
    id: String,
    stage: &'static str,
    mode: Mode,
    energy: f64,
}

/// The journal with what every decision needs: the stopping potential, the drift, the points.
struct View<'a> {
    cfg: &'a MapConfig,
    inst: &'a Instrument,
    records: &'a [Record],
    x_stop: f64,
    /// R_target, or the highest R reached when no setting reaches it (Section 4).
    target: f64,
}

#[derive(Clone, Debug, Default, Serialize)]
struct StoppingSummary {
    x_stop: f64,
    spread_v: f64,
    shift_v: f64,
    expected_shift_v: f64,
    thermalized: bool,
    curves: usize,
}

impl<'a> View<'a> {
    fn new(journal: &'a Journal) -> Self {
        let mut view = Self {
            cfg: &journal.plan.map,
            inst: &journal.instrument,
            records: &journal.records,
            x_stop: journal.instrument.q1_dc_v,
            target: journal.plan.map.r_target,
        };
        if let Some(s) = view.stopping_summary() {
            view.x_stop = s.x_stop;
        }
        view
    }

    fn done(&self) -> impl Iterator<Item = (usize, &'a Record)> {
        self.records
            .iter()
            .enumerate()
            .filter(|(_, r)| r.outcome == DONE)
    }

    fn of(&self, point: &str) -> Vec<(usize, &'a Record)> {
        self.records
            .iter()
            .enumerate()
            .filter(|(_, r)| r.request.point == point && r.outcome != FAILED)
            .collect()
    }

    fn now(&self) -> f64 {
        self.records
            .iter()
            .map(|r| r.started + r.duration_s)
            .fold(0.0, f64::max)
    }

    fn stopping_summary(&self) -> Option<StoppingSummary> {
        let curves: Vec<(&Request, &Transition)> = self
            .done()
            .filter(|(_, r)| r.request.role == ROLE_STOPPING)
            .filter_map(|(_, r)| {
                r.analysis
                    .transition
                    .as_ref()
                    .filter(|t| t.found)
                    .map(|t| (&r.request, t))
            })
            .collect();
        let base: Vec<f64> = curves
            .iter()
            .filter(|(r, _)| r.variant.starts_with("q1"))
            .map(|(_, t)| t.x50)
            .collect();
        let shifted: Vec<f64> = curves
            .iter()
            .filter(|(r, _)| r.variant.starts_with("shift"))
            .map(|(_, t)| t.x50)
            .collect();
        let x_stop = mean(&base)?;
        let shift = mean(&shifted).map_or(f64::NAN, |s| s - x_stop);
        let expected = self.cfg.stopping.q1_shift_v;
        Some(StoppingSummary {
            x_stop,
            spread_v: mean(
                &curves
                    .iter()
                    .map(|(_, t)| t.width)
                    .filter(|w| *w > 0.0)
                    .collect::<Vec<_>>(),
            )
            .unwrap_or(0.0),
            shift_v: if shift.is_finite() { shift } else { 0.0 },
            expected_shift_v: expected,
            thermalized: shift.is_finite() && (shift - expected).abs() <= 0.5,
            curves: curves.len(),
        })
    }

    /// Height of the reference ion relative to the first reference, interpolated between the
    /// references around record `index` (drift of the spray and of everything upstream).
    fn drift(&self, index: usize) -> f64 {
        let ion = self.cfg.reference.ion;
        let refs: Vec<(usize, f64)> = self
            .done()
            .filter(|(_, r)| r.request.role == ROLE_REFERENCE)
            .filter_map(|(i, r)| r.analysis.peak(ion).map(|p| (i, p.height)))
            .filter(|(_, h)| *h > 0.0)
            .collect();
        let Some(&(_, first)) = refs.first() else {
            return 1.0;
        };
        let before = refs.iter().rev().find(|(i, _)| *i <= index);
        let after = refs.iter().find(|(i, _)| *i > index);
        let h = match (before, after) {
            (Some(&(i0, h0)), Some(&(i1, h1))) => {
                h0 + (h1 - h0) * (index - i0) as f64 / (i1 - i0) as f64
            }
            (Some(&(_, h0)), None) => h0,
            (None, Some(&(_, h1))) => h1,
            (None, None) => first,
        };
        (h / first).clamp(0.05, 20.0)
    }

    /// The solver aims a little above R_target and a line counts as reaching it a little below:
    /// the measured R of one peak scatters by about `r_noise`.
    fn aim(&self) -> f64 {
        self.target * (1.0 + self.cfg.r_noise)
    }

    fn accept(&self) -> f64 {
        self.target * (1.0 - self.cfg.r_noise)
    }

    fn verify_variant(&self) -> String {
        format!("R{:.0}", self.target)
    }

    fn x_for(&self, energy: f64) -> f64 {
        round_to(self.x_stop - energy, 0.01)
    }

    fn stub_for(&self, x: f64) -> f64 {
        round_to(x + self.cfg.stub.s * (self.inst.ap_q1_q2_v - x), 0.01)
    }

    fn current_at(&self, f_hz: f64, a: f64) -> f64 {
        self.inst.current_per_hz_v * f_hz * a
    }

    fn reachable(&self, f_hz: f64, a: f64) -> bool {
        self.current_at(f_hz, a) <= 0.95 * self.inst.ilim_a
    }

    /// Seconds per measured point: the map's own step lines once they hold 100 points, else
    /// point_s + settle_s + point_overhead_s. Never below point_s + settle_s.
    fn per_point(&self) -> f64 {
        let floor = self.cfg.point_s + self.cfg.settle_s;
        let prior = floor + self.cfg.point_overhead_s.max(0.0);
        let (mut seconds, mut points) = (0.0, 0usize);
        for (_, r) in self.done() {
            if matches!(
                r.request.kind.as_str(),
                KIND_AMPLITUDE | KIND_OFFSET | KIND_EXTRA
            ) && r.analysis.measured >= 10
                && r.duration_s > LINE_S
            {
                seconds += r.duration_s - LINE_S;
                points += r.analysis.measured;
            }
        }
        if points < 100 {
            return prior;
        }
        (seconds / points as f64).clamp(floor, 4.0 * prior)
    }

    fn line_time(&self, points: usize) -> f64 {
        points as f64 * self.per_point() + LINE_S
    }

    /// Mix ions inside the amplitude range at (f, k, δ), with their expected amplitude.
    fn ions_in_line(&self, f_hz: f64, k: f64, delta: f64) -> Vec<(f64, f64)> {
        self.ions_between(f_hz, k, delta, self.cfg.a_range_v)
    }

    fn ions_between(&self, f_hz: f64, k: f64, delta: f64, [lo, hi]: [f64; 2]) -> Vec<(f64, f64)> {
        let filter = self.cfg.filter(k, delta);
        self.cfg
            .mix
            .iter()
            .filter_map(|m| {
                filter
                    .amplitude_for(*m, f_hz)
                    .filter(|a| *a >= lo * 1.02 && *a <= hi * 0.98)
                    .map(|a| (*m, a))
            })
            .collect()
    }

    /// The amplitudes of a line at (f, k, δ): the best-range ions in a_range_v ± a_margin, within
    /// a_range_v; all of a_range_v when it holds none. P1 (500 kHz, k = 0.200): 50–248 V of 20–348 V.
    fn amplitude_extent(&self, f_hz: f64, k: f64, delta: f64) -> [f64; 2] {
        let [lo, hi] = self.cfg.a_range_v;
        let best: Vec<f64> = self
            .ions_in_line(f_hz, k, delta)
            .into_iter()
            .filter(|(m, _)| self.cfg.in_best_range(*m))
            .map(|(_, a)| a)
            .collect();
        let (Some(first), Some(last)) = (
            best.iter().copied().reduce(f64::min),
            best.iter().copied().reduce(f64::max),
        ) else {
            return [lo, hi];
        };
        let m = self.cfg.a_margin.max(0.0);
        [
            round_to((first / (1.0 + m)).max(lo), 0.01),
            round_to((last * (1.0 + m)).min(hi), 0.01),
        ]
    }

    /// The top of the amplitude lines of a point at f, for its RF current.
    fn amplitude_top(&self, f_hz: f64) -> f64 {
        self.amplitude_extent(f_hz, self.cfg.ladder.first_k, 0.0)[1]
    }

    fn base_request(&self, stage: &str, role: &str, point: &str, variant: &str) -> Request {
        Request {
            stage: stage.to_owned(),
            role: role.to_owned(),
            point: point.to_owned(),
            variant: variant.to_owned(),
            q1_dc_v: self.inst.q1_dc_v,
            coarse: 1,
            ..Request::default()
        }
    }

    #[allow(clippy::too_many_arguments)]
    fn amplitude_line(
        &self,
        stage: &str,
        role: &str,
        point: &str,
        variant: &str,
        f_hz: f64,
        energy: f64,
        k: f64,
        delta: f64,
        stub: Option<f64>,
    ) -> Request {
        let x = self.x_for(energy);
        let extent = self.amplitude_extent(f_hz, k, delta);
        let axis = amplitude_grid(extent[0], extent[1], self.cfg.r_max, self.cfg.coarse);
        let ions: Vec<f64> = self
            .ions_between(f_hz, k, delta, extent)
            .iter()
            .map(|p| p.0)
            .collect();
        let points = axis.len() / self.cfg.coarse.max(1) + 30 * ions.len() + 10;
        let mut r = self.base_request(stage, role, point, variant);
        r.kind = KIND_AMPLITUDE.to_owned();
        r.f_hz = round_to(f_hz, 1.0);
        r.k = round_to(k, 1e-5);
        r.delta_v = round_to(delta, 0.01);
        r.energy_ev = energy;
        r.x_v = x;
        r.stub_v = stub.unwrap_or_else(|| self.stub_for(x));
        r.axis = axis;
        r.adaptive = true;
        r.coarse = self.cfg.coarse;
        r.ions = ions;
        r.expected_s = self.line_time(points);
        r.label = format!("{}{}-{}", r.stage, r.point, r.role);
        r
    }

    fn frequency_range(&self, a: f64, k: f64, delta: f64) -> Option<(f64, f64, Vec<f64>)> {
        let filter = self.cfg.filter(k, delta);
        let [f_lo, f_hi] = self.cfg.bounds.f_khz;
        let mut at: Vec<(f64, f64)> = self
            .cfg
            .mix
            .iter()
            .filter_map(|m| filter.frequency_for(*m, a).map(|f| (*m, f)))
            .filter(|(_, f)| *f >= f_lo * 1e3 * 1.03 && *f <= f_hi * 1e3 / 1.03)
            .collect();
        at.sort_by(|a, b| a.1.total_cmp(&b.1));
        let (first, last) = (at.first()?.1, at.last()?.1);
        let lo = (first / 1.05).max(f_lo * 1e3);
        let hi = (last * 1.05).min(f_hi * 1e3);
        Some((lo, hi, at.iter().map(|p| p.0).collect()))
    }

    #[allow(clippy::too_many_arguments)]
    fn frequency_line(
        &self,
        stage: &str,
        role: &str,
        point: &str,
        variant: &str,
        a: f64,
        energy: f64,
        k: f64,
        delta: f64,
    ) -> Option<Request> {
        let (lo, hi, ions) = self.frequency_range(a, k, delta)?;
        let x = self.x_for(energy);
        let mut r = self.base_request(stage, role, point, variant);
        r.kind = KIND_FREQUENCY.to_owned();
        r.f_hz = round_to(hi, 1.0);
        r.k = round_to(k, 1e-5);
        r.delta_v = round_to(delta, 0.01);
        r.energy_ev = energy;
        r.x_v = x;
        r.stub_v = self.stub_for(x);
        r.amplitude_v = a;
        r.axis = frequency_grid(lo, hi, self.inst.clock_hz);
        r.adaptive = false;
        r.ions = ions;
        r.expected_s = self.line_time(r.axis.len()) + 1.5 * r.axis.len() as f64;
        r.label = format!("{}{}-{}", r.stage, r.point, r.role);
        Some(r)
    }

    fn reference_line(&self) -> Request {
        let cfg = &self.cfg.reference;
        let f = cfg.f_khz * 1e3;
        let x = self.x_for(cfg.energy_ev);
        let a = self
            .cfg
            .filter(cfg.k, 0.0)
            .amplitude_for(cfg.ion, f)
            .unwrap_or(90.0);
        let axis = amplitude_grid(
            a * (1.0 - cfg.window),
            a * (1.0 + cfg.window),
            self.cfg.r_max,
            self.cfg.coarse,
        );
        let mut r = self.base_request("R", ROLE_REFERENCE, "REF", "");
        r.kind = KIND_AMPLITUDE.to_owned();
        r.f_hz = f;
        r.k = cfg.k;
        r.energy_ev = cfg.energy_ev;
        r.x_v = x;
        r.stub_v = self.stub_for(x);
        r.expected_s = self.line_time(axis.len() / self.cfg.coarse.max(1) + 30);
        r.axis = axis;
        r.adaptive = true;
        r.coarse = self.cfg.coarse;
        r.ions = vec![cfg.ion];
        r.label = "reference".to_owned();
        r
    }
}

// ---------------------------------------------------------------- points: ladders and their solution

/// One ion's ladder: (k, R, S) per line at δ = 0, S drift-corrected, sorted by k.
#[derive(Clone, Debug, Default, Serialize)]
struct IonLadder {
    mz: f64,
    position: f64,
    cycles: f64,
    table: Vec<(f64, f64, f64)>,
    /// Signal-to-noise of each table line (height over the local noise).
    snr: Vec<f64>,
    r_cap: f64,
}

#[derive(Clone, Debug, Default, Serialize)]
struct Solution {
    feasible: bool,
    k: f64,
    delta: f64,
    merit: f64,
    ions: Vec<(f64, f64, f64, f64)>,
}

#[derive(Clone, Debug, Default, Serialize)]
struct PointResult {
    id: String,
    stage: String,
    mode: String,
    f_hz: f64,
    amplitude_v: f64,
    energy_ev: f64,
    x_v: f64,
    closed: String,
    empty: bool,
    complete: bool,
    covers_best_range: bool,
    /// c used for this point's caps: its own (closing, else slope), else the global one.
    c: f64,
    c_source: String,
    /// c that sets this point's ladder targets: from the points completed before it.
    c_ladder: f64,
    closing_k: f64,
    target: f64,
    ions: Vec<IonLadder>,
    solution: Solution,
    merit: f64,
    feasible: bool,
    min_r: f64,
    stub_best_v: f64,
    stub_variation: f64,
    rel_noise: f64,
    /// The settings the merit was measured at (the verification line).
    verified_k: f64,
    verified_delta: f64,
}

fn interp_ln(table: &[(f64, f64, f64)], k: f64) -> Option<f64> {
    let first = table.first()?;
    if k <= first.0 {
        return Some(first.2.max(1e-300).ln());
    }
    for w in table.windows(2) {
        let ((k0, _, s0), (k1, _, s1)) = (w[0], w[1]);
        if k >= k0 && k <= k1 {
            let t = if k1 > k0 { (k - k0) / (k1 - k0) } else { 0.0 };
            return Some(s0.max(1e-300).ln() * (1.0 - t) + s1.max(1e-300).ln() * t);
        }
    }
    None
}

/// Smallest k at which R reaches `target` (linear in 1/R between lines); None if never reached.
fn k_for_r(table: &[(f64, f64, f64)], target: f64) -> Option<f64> {
    let first = table.first()?;
    if first.1 >= target {
        return Some(first.0);
    }
    table.windows(2).find_map(|w| {
        let ((k0, r0, _), (k1, r1, _)) = (w[0], w[1]);
        (r0 < target && r1 >= target).then(|| {
            let (a, b, t) = (1.0 / r0, 1.0 / r1, 1.0 / target);
            k0 + (k1 - k0) * (a - t) / (a - b)
        })
    })
}

/// The (k, δ) giving every ion R ≥ target with the largest Σ S: δ on a grid, the smallest k for
/// each δ (S falls as k rises). Ions see k − δ/A_i. No extrapolation beyond a ladder.
fn solve(ions: &[IonLadder], target: f64, delta_range: [f64; 2], frequency: bool) -> Solution {
    if ions.is_empty() || ions.iter().any(|i| i.table.is_empty()) {
        return Solution::default();
    }
    let needs: Vec<Option<f64>> = ions.iter().map(|i| k_for_r(&i.table, target)).collect();
    if needs.iter().any(Option::is_none) {
        return Solution::default();
    }
    let needs: Vec<f64> = needs.into_iter().flatten().collect();
    let (lo, hi) = if frequency {
        (0.0, 0.0)
    } else {
        (delta_range[0], delta_range[1])
    };
    let steps = ((hi - lo) / 0.02).round().max(0.0) as usize;
    let mut best = Solution::default();
    for s in 0..=steps {
        let delta = lo + 0.02 * s as f64;
        let k = ions
            .iter()
            .zip(&needs)
            .map(|(i, need)| need + delta / i.position.max(1e-9))
            .fold(f64::NEG_INFINITY, f64::max);
        let mut merit = 0.0;
        let mut detail = Vec::new();
        let mut ok = true;
        for (ion, _) in ions.iter().zip(&needs) {
            let x = k - delta / ion.position.max(1e-9);
            let last = ion.table.last().map_or(f64::NEG_INFINITY, |t| t.0);
            match interp_ln(&ion.table, x) {
                Some(ln_s) if x <= last + 1e-9 => {
                    let s = ln_s.exp();
                    merit += s;
                    detail.push((ion.mz, x, target, s));
                }
                _ => ok = false,
            }
        }
        if ok && (!best.feasible || merit > best.merit) {
            best = Solution {
                feasible: true,
                k: round_to(k, 1e-5),
                delta: round_to(delta, 0.01),
                merit,
                ions: detail,
            };
        }
    }
    best
}

const MIN_SNR: f64 = 8.0;

/// c from the slope of 1/R in k, one slope shared by every ion (1/R = a_i − k/(R_SLOPE·c)),
/// weighted by the signal-to-noise of each peak. The fallback when no closing was bracketed.
fn fit_c_slope(ions: &[IonLadder]) -> Option<f64> {
    let used: Vec<&IonLadder> = ions
        .iter()
        .filter(|i| {
            i.table
                .iter()
                .zip(&i.snr)
                .filter(|(t, s)| t.1 > 0.0 && **s >= MIN_SNR)
                .count()
                >= 3
        })
        .collect();
    if used.is_empty() {
        return None;
    }
    let mut rows = Vec::new();
    let mut b = Vec::new();
    for (j, ion) in used.iter().enumerate() {
        for (t, snr) in ion.table.iter().zip(&ion.snr) {
            if t.1 > 0.0 && *snr >= MIN_SNR {
                let w = snr.min(50.0);
                let mut row = vec![0.0; used.len() + 1];
                row[j] = w;
                row[used.len()] = w * t.0;
                rows.push(row);
                b.push(w / t.1);
            }
        }
    }
    let x = lstsq(&rows, &b)?;
    let slope = x[used.len()];
    if slope >= 0.0 {
        return None;
    }
    let c = -1.0 / (R_SLOPE * slope);
    (0.85..=1.05).contains(&c).then_some(c)
}

/// R_cap of one ion with the ideal part fixed by c (weighted by signal-to-noise); 1e6 when no
/// cap is measurable, 0 when the ion has no usable line.
fn fit_cap(ion: &IonLadder, c: f64) -> f64 {
    let (mut sum, mut weight) = (0.0, 0.0);
    for (t, snr) in ion.table.iter().zip(&ion.snr) {
        if t.1 > 0.0 && *snr >= MIN_SNR {
            let w = snr.min(50.0).powi(2);
            sum += w * (1.0 / t.1 - (APEX - t.0 / c) / R_SLOPE);
            weight += w;
        }
    }
    if weight == 0.0 {
        return 0.0;
    }
    let inv = sum / weight;
    if inv > 1e-6 {
        (1.0 / inv).min(1e6)
    } else {
        1e6
    }
}

/// The closing of a ladder: the middle of the last k with signal and the first k without, when
/// they are bracketed to within a few finest steps. The precise measurement of c.
fn closing_of(ladder: &[(f64, bool)], finest: f64) -> Option<f64> {
    let k_sig = ladder
        .iter()
        .filter(|l| l.1)
        .map(|l| l.0)
        .fold(f64::NEG_INFINITY, f64::max);
    let k_none = ladder
        .iter()
        .filter(|l| !l.1 && l.0 > k_sig)
        .map(|l| l.0)
        .fold(f64::INFINITY, f64::min);
    (k_sig.is_finite() && k_none.is_finite() && k_none - k_sig <= 4.0 * finest + 1e-9)
        .then_some(0.5 * (k_sig + k_none))
}

impl View<'_> {
    fn design(&self) -> Vec<PointSpec> {
        let mut amplitude: Vec<PointSpec> = self
            .cfg
            .design
            .amplitude
            .iter()
            .enumerate()
            .map(|(i, p)| PointSpec {
                id: format!("P{}", i + 1),
                stage: "B",
                mode: Mode::Amplitude(p[0] * 1e3),
                energy: p[1],
            })
            .collect();
        let frequency: Vec<PointSpec> = self
            .cfg
            .design
            .frequency
            .iter()
            .enumerate()
            .map(|(i, p)| PointSpec {
                id: format!("F{}", i + 1),
                stage: "B",
                mode: Mode::Frequency(p[0]),
                energy: p[1],
            })
            .collect();
        if amplitude.is_empty() {
            return frequency;
        }
        // P1 first (today's point, the instrument check), then the others in random order.
        let first = amplitude.remove(0);
        let mut rest = amplitude;
        rest.extend(frequency);
        let order = Rng::new(self.cfg.seed).permutation(rest.len());
        let mut points = vec![first];
        points.extend(order.into_iter().map(|i| rest[i].clone()));
        points
    }

    fn ladder_line(&self, spec: &PointSpec, role: &str, variant: &str, k: f64) -> Option<Request> {
        match spec.mode {
            Mode::Amplitude(f) => Some(self.amplitude_line(
                spec.stage,
                role,
                &spec.id,
                variant,
                f,
                spec.energy,
                k,
                0.0,
                None,
            )),
            Mode::Frequency(a) => {
                self.frequency_line(spec.stage, role, &spec.id, variant, a, spec.energy, k, 0.0)
            }
        }
    }

    /// Everything measured at one point, read back from the journal.
    fn point(&self, spec: &PointSpec, c_global: f64) -> PointResult {
        let records = self.of(&spec.id);
        let (mode, f_hz, a_fixed) = match spec.mode {
            Mode::Amplitude(f) => ("amplitude", f, 0.0),
            Mode::Frequency(a) => ("frequency", 0.0, a),
        };
        let mut result = PointResult {
            id: spec.id.clone(),
            stage: spec.stage.to_owned(),
            mode: mode.to_owned(),
            f_hz,
            amplitude_v: a_fixed,
            energy_ev: spec.energy,
            x_v: self.x_for(spec.energy),
            c: c_global,
            c_source: "global".to_owned(),
            c_ladder: c_global,
            closing_k: APEX * c_global,
            target: self.target,
            rel_noise: 0.05,
            ..PointResult::default()
        };
        if let Some((_, r)) = records.iter().find(|(_, r)| r.outcome == UNREACHABLE) {
            result.closed = format!("hardware limit: {}", r.error);
            return result;
        }
        let ladder: Vec<(usize, &Record)> = records
            .iter()
            .copied()
            .filter(|(_, r)| {
                r.outcome == DONE && matches!(r.request.role.as_str(), ROLE_LADDER | ROLE_REPEAT)
            })
            .collect();
        let best_ions = self.cfg.best_ions();
        let mut ions: BTreeMap<i64, IonLadder> = BTreeMap::new();
        let mut repeat_pairs = Vec::new();
        for (index, record) in &ladder {
            let drift = self.drift(*index);
            for peak in record.analysis.peaks.iter().filter(|p| p.found) {
                let entry = ions
                    .entry((peak.mz * 100.0).round() as i64)
                    .or_insert_with(|| {
                        let f_ion = if f_hz > 0.0 { f_hz } else { peak.center };
                        IonLadder {
                            mz: peak.mz,
                            position: if f_hz > 0.0 { peak.center } else { a_fixed },
                            cycles: cycles(
                                f_ion,
                                peak.mz,
                                spec.energy.max(0.1),
                                self.cfg.q2_length_m,
                            ),
                            ..IonLadder::default()
                        }
                    });
                if record.request.k <= self.cfg.ladder.first_k + 1e-9 && f_hz > 0.0 {
                    entry.position = peak.center;
                }
                entry
                    .table
                    .push((record.request.k, peak.r, peak.height / drift));
                entry.snr.push(if peak.noise > 0.0 {
                    peak.height / peak.noise
                } else {
                    0.0
                });
            }
            if record.request.role == ROLE_REPEAT {
                let original = ladder.iter().find(|(_, r)| {
                    r.request.role == ROLE_LADDER && (r.request.k - record.request.k).abs() < 5e-6
                });
                if let Some((i0, r0)) = original {
                    let total = |r: &Record, d: f64| {
                        r.analysis
                            .peaks
                            .iter()
                            .filter(|p| p.found)
                            .map(|p| p.height)
                            .sum::<f64>()
                            / d
                    };
                    let (a, b) = (total(r0, self.drift(*i0)), total(record, drift));
                    if a > 0.0 && b > 0.0 {
                        repeat_pairs.push((a - b).abs() / (0.5 * (a + b)) / 2f64.sqrt());
                    }
                }
            }
        }
        if let Some(n) = mean(&repeat_pairs) {
            result.rel_noise = n.clamp(0.01, 0.5);
        }
        for ion in ions.values_mut() {
            // Merge repeated k: mean R (of finite values), mean S, best signal-to-noise.
            let mut rows: Vec<((f64, f64, f64), f64)> = ion
                .table
                .iter()
                .copied()
                .zip(ion.snr.iter().copied())
                .collect();
            rows.sort_by(|a, b| a.0.0.total_cmp(&b.0.0));
            let mut merged: Vec<((f64, f64, f64), f64)> = Vec::new();
            for ((k, r, s), snr) in rows {
                match merged.last_mut() {
                    Some(last) if (last.0.0 - k).abs() < 5e-6 => {
                        last.0.1 = if last.0.1 > 0.0 && r > 0.0 {
                            0.5 * (last.0.1 + r)
                        } else {
                            last.0.1.max(r)
                        };
                        last.0.2 = 0.5 * (last.0.2 + s);
                        last.1 = last.1.max(snr);
                    }
                    _ => merged.push(((k, r, s), snr)),
                }
            }
            ion.table = merged.iter().map(|m| m.0).collect();
            ion.snr = merged.iter().map(|m| m.1).collect();
        }
        let ladder_ions: Vec<IonLadder> = ions.into_values().collect();
        result.empty = !ladder.is_empty() && ladder.iter().all(|(_, r)| !r.analysis.any_signal());
        let bracket: Vec<(f64, bool)> = ladder
            .iter()
            .filter(|(_, r)| r.request.role == ROLE_LADDER)
            .map(|(_, r)| (r.request.k, r.analysis.any_signal()))
            .collect();
        if let Some(k_c) = closing_of(&bracket, self.cfg.ladder.finest_k) {
            result.c = k_c / APEX;
            result.c_source = "closing".to_owned();
        } else if let Some(c) = fit_c_slope(&ladder_ions) {
            result.c = c;
            result.c_source = "slope".to_owned();
        }
        result.closing_k = APEX * result.c;
        result.ions = ladder_ions
            .into_iter()
            .map(|mut i| {
                i.r_cap = fit_cap(&i, result.c);
                i
            })
            .collect();
        let in_range: Vec<IonLadder> = result
            .ions
            .iter()
            .filter(|i| best_ions.iter().any(|m| (m - i.mz).abs() < 0.5))
            .cloned()
            .collect();
        let expected_best: Vec<f64> = match spec.mode {
            Mode::Amplitude(f) => self
                .ions_in_line(f, self.cfg.ladder.first_k, 0.0)
                .iter()
                .map(|p| p.0)
                .filter(|m| self.cfg.in_best_range(*m))
                .collect(),
            Mode::Frequency(a) => self
                .frequency_range(a, self.cfg.ladder.first_k, 0.0)
                .map(|r| r.2)
                .unwrap_or_default()
                .into_iter()
                .filter(|m| self.cfg.in_best_range(*m))
                .collect(),
        };
        result.covers_best_range = best_ions
            .iter()
            .all(|m| expected_best.iter().any(|e| (e - m).abs() < 0.5));
        result.solution = solve(
            &in_range,
            self.aim(),
            self.cfg.bounds.delta_v,
            matches!(spec.mode, Mode::Frequency(_)),
        );
        if in_range.len() < expected_best.len() {
            result.solution.feasible = false;
        }
        // Verification line: the measured merit at (k*, δ*).
        let verify_variant = self.verify_variant();
        if let Some((index, verify)) = records.iter().find(|(_, r)| {
            r.outcome == DONE
                && r.request.role == ROLE_VERIFY
                && r.request.variant == verify_variant
        }) {
            let drift = self.drift(*index);
            result.verified_k = verify.request.k;
            result.verified_delta = verify.request.delta_v;
            let mut merit = 0.0;
            let mut min_r = f64::INFINITY;
            let mut all = true;
            for m in &expected_best {
                match verify.analysis.peak(*m) {
                    Some(p) => {
                        merit += p.height / drift;
                        min_r = min_r.min(if p.r > 0.0 { p.r } else { 0.0 });
                    }
                    None => all = false,
                }
            }
            result.merit = merit;
            result.min_r = if min_r.is_finite() { min_r } else { 0.0 };
            result.feasible = all && result.min_r >= self.accept();
        }
        // Stub cuts: the variation relative to that ion's peak height at the point.
        let mut bests = Vec::new();
        for (_, r) in records
            .iter()
            .filter(|(_, r)| r.outcome == DONE && r.request.role == ROLE_STUB)
        {
            let Some(profile) = r.analysis.profile.as_ref().filter(|p| p.found) else {
                continue;
            };
            let mz = r.request.ions.first().copied().unwrap_or(0.0);
            let height = records
                .iter()
                .rev()
                .filter(|(_, x)| {
                    x.outcome == DONE
                        && matches!(x.request.role.as_str(), ROLE_VERIFY | ROLE_LADDER)
                })
                .find_map(|(_, x)| x.analysis.peak(mz).map(|p| p.height))
                .unwrap_or(0.0);
            if height > 0.0 {
                result.stub_variation = result
                    .stub_variation
                    .max((profile.max - profile.min) / height);
            }
            bests.push(profile.best);
        }
        result.stub_best_v = mean(&bests).unwrap_or(0.0);
        result.complete =
            result.empty || !result.closed.is_empty() || self.point_next(spec, &result).is_none();
        result
    }

    /// Next k of a ladder (Section 5 B): approach the expected closing, then step past it until
    /// the signal disappears and bisect; stop at the closing or when R stops improving.
    fn ladder_next(&self, ladder: &[(f64, &Analysis)], c_est: f64) -> Option<f64> {
        let cfg = &self.cfg.ladder;
        if ladder.is_empty() {
            return Some(cfg.first_k);
        }
        if ladder.len() >= cfg.max_lines {
            return None;
        }
        let measured = |k: f64| {
            ladder
                .iter()
                .any(|(m, _)| (m - k).abs() < 0.5 * cfg.finest_k)
        };
        let mut signal: Vec<(f64, f64, f64)> = ladder
            .iter()
            .filter(|(_, a)| a.any_signal())
            .map(|(k, a)| {
                let found: Vec<&Peak> = a.peaks.iter().filter(|p| p.found).collect();
                let rs: Vec<f64> = found.iter().map(|p| p.r).filter(|r| *r > 0.0).collect();
                (
                    *k,
                    found.iter().map(|p| p.height).sum(),
                    median(&rs).unwrap_or(0.0),
                )
            })
            .collect();
        signal.sort_by(|a, b| a.0.total_cmp(&b.0));
        let Some(&(k_sig, _, _)) = signal.last() else {
            return None; // No ion at the first k: an empty point.
        };
        let k_none = ladder
            .iter()
            .filter(|(k, a)| *k > k_sig && !a.any_signal())
            .map(|(k, _)| *k)
            .fold(f64::INFINITY, f64::min);
        if signal.len() >= 3 {
            let n = signal.len();
            let (a, b, c) = (signal[n - 3], signal[n - 2], signal[n - 1]);
            if a.2 > 0.0 && b.2 <= a.2 * 1.05 && c.2 <= b.2 * 1.05 && c.1 < 0.9 * a.1 {
                return None; // R no longer improves while the signal falls: past the optimum.
            }
        }
        let closing = APEX * c_est;
        for d in &cfg.distances {
            let t = round_to(closing - d, 1e-5);
            if t > cfg.first_k + 1e-6 && t < k_none && !measured(t) {
                return Some(t);
            }
        }
        let next = if k_none.is_finite() {
            if k_none - k_sig <= cfg.finest_k * 1.01 {
                return None;
            }
            round_to(0.5 * (k_sig + k_none), 1e-5)
        } else {
            let beyond = ladder.iter().filter(|(k, _)| *k > closing - 1e-9).count() as i32;
            round_to(k_sig + cfg.beyond_step * 2f64.powi(beyond), 1e-5)
        };
        (next < APEX + 0.002 && !measured(next)).then_some(next)
    }

    /// The next line of a point: ladder, repeats, verification, stub cut; None when complete.
    fn point_next(&self, spec: &PointSpec, result: &PointResult) -> Option<Request> {
        if result.empty || !result.closed.is_empty() {
            return None;
        }
        if let Mode::Amplitude(f) = spec.mode {
            let top = self.amplitude_top(f);
            if !self.reachable(f, top) {
                return None; // Closed before the first line: the PSU cannot reach the top of it.
            }
        }
        let records = self.of(&spec.id);
        let done = |role: &str| -> Vec<&Record> {
            records
                .iter()
                .filter(|(_, r)| r.outcome == DONE && r.request.role == role)
                .map(|(_, r)| *r)
                .collect()
        };
        let ladder: Vec<(f64, &Analysis)> = done(ROLE_LADDER)
            .iter()
            .map(|r| (r.request.k, &r.analysis))
            .collect();
        if let Some(k) = self.ladder_next(&ladder, result.c_ladder) {
            return self.ladder_line(spec, ROLE_LADDER, "", k);
        }
        if ladder.iter().all(|(_, a)| !a.any_signal()) {
            return None;
        }
        // Repeats: the line whose lowest R is nearest R_target, and the last one with signal.
        let target = self.target;
        let mut chosen: Vec<f64> = Vec::new();
        let low_r = |a: &Analysis| {
            a.peaks
                .iter()
                .filter(|p| p.found && p.r > 0.0)
                .map(|p| p.r)
                .fold(f64::INFINITY, f64::min)
        };
        if let Some((k, _)) = ladder
            .iter()
            .filter(|(_, a)| low_r(a).is_finite())
            .min_by(|a, b| {
                (low_r(a.1) - target)
                    .abs()
                    .total_cmp(&(low_r(b.1) - target).abs())
            })
        {
            chosen.push(*k);
        }
        if let Some((k, _)) = ladder
            .iter()
            .filter(|(_, a)| a.any_signal())
            .max_by(|a, b| a.0.total_cmp(&b.0))
            && !chosen.iter().any(|c| (c - k).abs() < 5e-6)
        {
            chosen.push(*k);
        }
        chosen.truncate(self.cfg.ladder.repeats);
        let repeated = done(ROLE_REPEAT);
        for k in chosen {
            if !repeated.iter().any(|r| (r.request.k - k).abs() < 5e-6) {
                return self.ladder_line(spec, ROLE_REPEAT, "", k);
            }
        }
        let solution = &result.solution;
        let verify_variant = self.verify_variant();
        if solution.feasible
            && !done(ROLE_VERIFY)
                .iter()
                .any(|r| r.request.variant == verify_variant)
        {
            return match spec.mode {
                Mode::Amplitude(f) => Some(self.amplitude_line(
                    spec.stage,
                    ROLE_VERIFY,
                    &spec.id,
                    &verify_variant,
                    f,
                    spec.energy,
                    solution.k,
                    solution.delta,
                    None,
                )),
                Mode::Frequency(a) => self.frequency_line(
                    spec.stage,
                    ROLE_VERIFY,
                    &spec.id,
                    &verify_variant,
                    a,
                    spec.energy,
                    solution.k,
                    0.0,
                ),
            };
        }
        // Stub cut at (k*, δ*) on the lightest and heaviest ion of the best range in the line.
        let Mode::Amplitude(f) = spec.mode else {
            return None;
        };
        let (k, delta) = if solution.feasible {
            (solution.k, solution.delta)
        } else {
            (self.cfg.ladder.first_k, 0.0)
        };
        let in_line: Vec<(f64, f64)> = self
            .ions_in_line(f, k, delta)
            .into_iter()
            .filter(|(m, _)| self.cfg.in_best_range(*m))
            .collect();
        let mut targets: Vec<(f64, f64)> = Vec::new();
        if let Some(first) = in_line.first() {
            targets.push(*first);
        }
        if let Some(last) = in_line.last()
            && in_line.len() > 1
        {
            targets.push(*last);
        }
        let stubs = done(ROLE_STUB);
        for (m, a) in targets {
            let variant = format!("ion {m:.0}");
            if !stubs.iter().any(|r| r.request.variant == variant) {
                return Some(self.stub_line(spec, &variant, f, k, delta, a, m));
            }
        }
        None
    }

    #[allow(clippy::too_many_arguments)]
    fn stub_line(
        &self,
        spec: &PointSpec,
        variant: &str,
        f: f64,
        k: f64,
        delta: f64,
        a: f64,
        mz: f64,
    ) -> Request {
        let x = self.x_for(spec.energy);
        let cfg = &self.cfg.stub;
        let top = (self.inst.q1_dc_v + cfg.cut_to_q1_v)
            .min(self.inst.q1_dc_v - self.cfg.bounds.stub_below_q1_v);
        let axis = linear_grid(x + cfg.cut_from_x_v, top, cfg.cut_step_v);
        let mut r = self.base_request(spec.stage, ROLE_STUB, &spec.id, variant);
        r.kind = KIND_EXTRA.to_owned();
        r.f_hz = f;
        r.k = round_to(k, 1e-5);
        r.delta_v = round_to(delta, 0.01);
        r.energy_ev = spec.energy;
        r.x_v = x;
        r.stub_v = self.stub_for(x);
        r.amplitude_v = round_to(a, 0.01);
        r.expected_s = self.line_time(axis.len());
        r.axis = axis;
        r.ions = vec![mz];
        r.label = format!("{}{}-stub", spec.stage, spec.id);
        r
    }

    fn stopping_lines(&self) -> Vec<Request> {
        let cfg = &self.cfg.stopping;
        let f = cfg.f_khz * 1e3;
        let axis = linear_grid(cfg.x_from_v, cfg.x_to_v, cfg.x_step_v);
        let mut lines = Vec::new();
        for (shift_name, q1) in [
            ("q1", self.inst.q1_dc_v),
            ("shift", self.inst.q1_dc_v + cfg.q1_shift_v),
        ] {
            for ion in &cfg.ions {
                let Some(a) = self.cfg.filter(cfg.k, 0.0).amplitude_for(*ion, f) else {
                    continue;
                };
                for repeat in 0..cfg.repeats.max(1) {
                    let variant = format!("{shift_name} ion {ion:.0} #{}", repeat + 1);
                    let mut r = self.base_request("A", ROLE_STOPPING, "STOP", &variant);
                    r.kind = KIND_OFFSET.to_owned();
                    r.f_hz = f;
                    r.k = cfg.k;
                    r.q1_dc_v = round_to(q1, 0.01);
                    r.x_v = cfg.x_from_v;
                    r.stub_v = self.stub_for(cfg.x_from_v);
                    r.amplitude_v = round_to(a, 0.01);
                    r.axis = axis.clone();
                    r.ions = vec![*ion];
                    r.expected_s = self.line_time(axis.len());
                    r.label = "stopping".to_owned();
                    lines.push(r);
                }
            }
        }
        lines
    }
}

// ---------------------------------------------------------------- laws (Section 5 C)

#[derive(Clone, Debug, Default, Serialize)]
struct CapLaw {
    /// R_cap = a · N^b; `none` when no ion shows a cap (the ideal filter up to the closing).
    a: f64,
    b: f64,
    none: bool,
    residual: f64,
    points: Vec<(f64, f64, f64, String)>,
    valid: bool,
}

#[derive(Clone, Debug, Default, Serialize)]
struct SignalLaw {
    /// ln S0_i = alpha_i + beta · (lf, le, lf², le², lf·le) at the first k; S ∝ (R/R0)^-p.
    alpha: BTreeMap<String, f64>,
    beta: Vec<f64>,
    p: f64,
    residual: f64,
    valid: bool,
}

#[derive(Clone, Debug, Default, Serialize)]
struct Laws {
    c: f64,
    c_spread: f64,
    cap: CapLaw,
    signal: SignalLaw,
    valid: bool,
    points: usize,
}

fn features(f_hz: f64, energy: f64, quadratic: bool) -> Vec<f64> {
    let lf = (f_hz / 500e3).log10();
    let le = (energy / 26.0).log10();
    if quadratic {
        vec![lf, le, lf * lf, le * le, lf * le]
    } else {
        vec![lf, le]
    }
}

fn ion_key(mz: f64) -> String {
    format!("{mz:.2}")
}

impl Laws {
    /// c of the completed points (closing first), else the plan's.
    fn global_c(plan: f64, points: &[PointResult]) -> f64 {
        let closing: Vec<f64> = points
            .iter()
            .filter(|p| p.complete && p.mode == "amplitude" && p.c_source == "closing")
            .map(|p| p.c)
            .collect();
        let slope: Vec<f64> = points
            .iter()
            .filter(|p| p.complete && p.mode == "amplitude" && p.c_source == "slope")
            .map(|p| p.c)
            .collect();
        median(&closing).or_else(|| median(&slope)).unwrap_or(plan)
    }

    fn fit(cfg: &MapConfig, points: &[PointResult]) -> Self {
        let mut laws = Self {
            c: cfg.calibration.c,
            ..Self::default()
        };
        let measured: Vec<&PointResult> = points.iter().filter(|p| !p.ions.is_empty()).collect();
        laws.points = measured.len();
        laws.c = Self::global_c(cfg.calibration.c, points);
        let cs: Vec<f64> = measured
            .iter()
            .filter(|p| p.c_source == "closing")
            .map(|p| p.c)
            .collect();
        laws.c_spread = std_dev(&cs).unwrap_or(0.0);
        // R_cap versus N.
        let mut caps = Vec::new();
        let mut finite = 0usize;
        for p in &measured {
            for ion in &p.ions {
                if ion.r_cap > 0.0 && ion.table.len() >= 3 {
                    caps.push((ion.cycles, ion.r_cap, ion.mz, p.id.clone()));
                    if ion.r_cap < 1e5 {
                        finite += 1;
                    }
                }
            }
        }
        laws.cap.points = caps.clone();
        if !caps.is_empty() && finite * 3 < caps.len() {
            laws.cap.none = true;
            laws.cap.valid = true;
        } else if caps.iter().filter(|c| c.1 < 300.0).count() >= 4 {
            let pts: Vec<&(f64, f64, f64, String)> =
                caps.iter().filter(|c| c.1 < 300.0 && c.0 > 0.0).collect();
            let rows: Vec<Vec<f64>> = pts.iter().map(|c| vec![1.0, c.0.ln()]).collect();
            let b: Vec<f64> = pts.iter().map(|c| c.1.ln()).collect();
            if let Some(x) = lstsq(&rows, &b) {
                laws.cap.a = x[0].exp();
                laws.cap.b = x[1];
                let res: Vec<f64> = rows
                    .iter()
                    .zip(&b)
                    .map(|(r, y)| y - (x[0] + x[1] * r[1]))
                    .collect();
                laws.cap.residual =
                    (res.iter().map(|v| v * v).sum::<f64>() / res.len() as f64).sqrt();
                laws.cap.valid = laws.cap.residual <= 0.35 && pts.len() >= 4;
            }
        }
        // Signal at the first k, amplitude points only (heights in A are comparable).
        let first_k = cfg.ladder.first_k;
        let mut rows = Vec::new();
        let mut y = Vec::new();
        let mut keys: Vec<String> = Vec::new();
        let mut obs: Vec<(String, f64, f64, f64)> = Vec::new();
        for p in measured.iter().filter(|p| p.mode == "amplitude") {
            for ion in &p.ions {
                if let Some(t) = ion.table.iter().find(|t| (t.0 - first_k).abs() < 5e-6)
                    && t.2 > 0.0
                {
                    let key = ion_key(ion.mz);
                    if !keys.contains(&key) {
                        keys.push(key.clone());
                    }
                    obs.push((key, p.f_hz, p.energy_ev, t.2.ln()));
                }
            }
        }
        let n_points = measured.iter().filter(|p| p.mode == "amplitude").count();
        for quadratic in [true, false] {
            let nf = if quadratic { 5 } else { 2 };
            if obs.len() < keys.len() + nf + 2 || n_points < if quadratic { 6 } else { 3 } {
                continue;
            }
            rows.clear();
            y.clear();
            for (key, f, e, ln_s) in &obs {
                let mut row = vec![0.0; keys.len()];
                row[keys.iter().position(|k| k == key).unwrap_or(0)] = 1.0;
                row.extend(features(*f, *e, quadratic));
                rows.push(row);
                y.push(*ln_s);
            }
            if let Some(x) = lstsq(&rows, &y) {
                let res: Vec<f64> = rows
                    .iter()
                    .zip(&y)
                    .map(|(r, v)| v - r.iter().zip(&x).map(|(a, b)| a * b).sum::<f64>())
                    .collect();
                laws.signal.residual =
                    (res.iter().map(|v| v * v).sum::<f64>() / res.len() as f64).sqrt();
                laws.signal.alpha = keys.iter().cloned().zip(x.iter().copied()).collect();
                laws.signal.beta = x[keys.len()..].to_vec();
                laws.signal.valid = laws.signal.residual <= 0.5;
                break;
            }
        }
        // Signal along the ladders against the distance to the apex (the ideal width):
        // ln(S/S0) = p · ln((APEX − k/c) / (APEX − k0/c)).
        let mut num = 0.0;
        let mut den = 0.0;
        for p in measured.iter().filter(|p| p.mode == "amplitude") {
            for ion in &p.ions {
                let Some(base) = ion.table.iter().find(|t| t.2 > 0.0) else {
                    continue;
                };
                let gap = |k: f64| APEX - k / p.c;
                for t in ion
                    .table
                    .iter()
                    .filter(|t| t.2 > 0.0 && t.0 > base.0 && gap(t.0) > 0.0)
                {
                    let dx = (gap(t.0) / gap(base.0)).ln();
                    let dy = (t.2 / base.2).ln();
                    num += dx * dy;
                    den += dx * dx;
                }
            }
        }
        laws.signal.p = if den > 0.0 {
            (num / den).clamp(0.0, 5.0)
        } else {
            1.0
        };
        laws.valid = laws.cap.valid && laws.signal.valid && n_points >= 4;
        laws
    }

    fn r_cap(&self, n: f64) -> f64 {
        if self.cap.none || !self.cap.valid {
            return 1e6;
        }
        (self.cap.a * n.max(1e-3).powf(self.cap.b)).min(1e6)
    }

    /// Resolution of ion `mz` at ratio x = k − δ/A (MScan units), at energy E and frequency f.
    fn r_at(&self, x: f64, n: f64) -> f64 {
        let ideal_inv = (APEX - x / self.c) / R_SLOPE;
        if ideal_inv <= 0.0 {
            return 0.0;
        }
        1.0 / (ideal_inv + 1.0 / self.r_cap(n))
    }

    fn s0(&self, mz: f64, f_hz: f64, energy: f64) -> Option<f64> {
        let alpha = self.signal.alpha.get(&ion_key(mz))?;
        let quadratic = self.signal.beta.len() == 5;
        let f = features(f_hz, energy, quadratic);
        Some(
            (alpha
                + f.iter()
                    .zip(&self.signal.beta)
                    .map(|(a, b)| a * b)
                    .sum::<f64>())
            .exp(),
        )
    }

    /// Model ladders of every best-range ion at (f, E): what a measured ladder would give.
    fn model_ions(&self, view: &View, f_hz: f64, energy: f64) -> Option<Vec<IonLadder>> {
        let first_k = view.cfg.ladder.first_k;
        let in_line = view.ions_in_line(f_hz, first_k, 0.0);
        let best = view.cfg.best_ions();
        if !best
            .iter()
            .all(|m| in_line.iter().any(|(i, _)| (i - m).abs() < 0.5))
        {
            return None;
        }
        let top = self.c * APEX - 1e-4;
        let mut ions = Vec::new();
        for (mz, a) in in_line
            .into_iter()
            .filter(|(m, _)| view.cfg.in_best_range(*m))
        {
            let n = cycles(f_hz, mz, energy, view.cfg.q2_length_m);
            let s0 = self.s0(mz, f_hz, energy)?;
            let r0 = self.r_at(first_k, n);
            if r0 <= 0.0 {
                return None;
            }
            let gap0 = APEX - first_k / self.c;
            let table: Vec<(f64, f64, f64)> = (0..=240)
                .map(|i| first_k + (top - first_k) * i as f64 / 240.0)
                .map(|x| {
                    let gap = (APEX - x / self.c).max(0.0);
                    (x, self.r_at(x, n), s0 * (gap / gap0).powf(self.signal.p))
                })
                .filter(|t| t.1 > 0.0)
                .collect();
            ions.push(IonLadder {
                mz,
                position: a,
                cycles: n,
                snr: vec![100.0; table.len()],
                table,
                r_cap: self.r_cap(n),
            });
        }
        Some(ions)
    }

    fn predict(&self, view: &View, f_hz: f64, energy: f64) -> Option<Solution> {
        if !self.valid {
            return None;
        }
        let ions = self.model_ions(view, f_hz, energy)?;
        let s = solve(&ions, view.aim(), view.cfg.bounds.delta_v, false);
        s.feasible.then_some(s)
    }
}

/// Local quadratic model of the measured merits in (log f, log E): the fallback of the search.
fn local_quadratic(points: &[&PointResult]) -> Option<Vec<f64>> {
    if points.len() < 6 {
        return None;
    }
    let rows: Vec<Vec<f64>> = points
        .iter()
        .map(|p| {
            let mut r = vec![1.0];
            r.extend(features(p.f_hz, p.energy_ev, true));
            r
        })
        .collect();
    let y: Vec<f64> = points.iter().map(|p| p.merit.max(1e-30).ln()).collect();
    lstsq(&rows, &y)
}

fn quadratic_value(coef: &[f64], f_hz: f64, energy: f64) -> f64 {
    let mut r = vec![1.0];
    r.extend(features(f_hz, energy, true));
    r.iter().zip(coef).map(|(a, b)| a * b).sum::<f64>().exp()
}

// ---------------------------------------------------------------- the decision

#[derive(Clone, Debug)]
pub enum Decision {
    Line(Request),
    Wait(Request),
    Done(String),
    Stop(String),
}

#[derive(Clone, Debug, Default, Serialize)]
struct SearchStep {
    point: String,
    f_hz: f64,
    energy_ev: f64,
    predicted: f64,
    merit: f64,
    feasible: bool,
    radius: f64,
}

#[derive(Clone, Debug, Default, Serialize)]
struct Optimum {
    point: String,
    f_hz: f64,
    energy_ev: f64,
    x_v: f64,
    k: f64,
    delta_v: f64,
    stub_v: f64,
    merit: f64,
}

#[derive(Clone, Debug, Serialize)]
struct LocalResult {
    name: String,
    merit: f64,
    min_r: f64,
    lines: usize,
    found: usize,
    #[serde(skip)]
    request: Request,
    verdict: String,
}

struct Engine<'a> {
    view: View<'a>,
    design: Vec<PointSpec>,
    points: Vec<PointResult>,
    search: Vec<PointSpec>,
    laws: Laws,
    domain: Option<(f64, f64)>,
}

impl<'a> Engine<'a> {
    fn new(journal: &'a Journal) -> Self {
        let view = View::new(journal);
        let design = if view.cfg.stages.design {
            view.design()
        } else {
            Vec::new()
        };
        let mut search: Vec<PointSpec> = Vec::new();
        for r in view.records.iter().filter(|r| r.request.stage == "D") {
            if !search.iter().any(|s| s.id == r.request.point) {
                search.push(PointSpec {
                    id: r.request.point.clone(),
                    stage: "D",
                    mode: Mode::Amplitude(r.request.f_hz),
                    energy: r.request.energy_ev,
                });
            }
        }
        let c_plan = view.cfg.calibration.c;
        let mut engine = Self {
            view,
            design,
            points: Vec::new(),
            search,
            laws: Laws::default(),
            domain: None,
        };
        // c from the points completed so far sets the expected closing of the next ladders.
        let specs: Vec<PointSpec> = engine
            .design
            .iter()
            .chain(&engine.search)
            .cloned()
            .collect();
        engine.points = engine.sequential(&specs, c_plan);
        // No setting reaches R_target: aim at the highest R measured on the whole range instead.
        let design_done = engine
            .points
            .iter()
            .filter(|p| p.stage == "B")
            .all(|p| p.complete);
        if design_done && engine.comparable().is_empty() {
            let reached = engine.highest_r();
            if reached > 2.0 && reached < engine.view.target {
                engine.view.target = (0.9 * reached).floor();
                engine.points = engine.sequential(&specs, c_plan);
            }
        }
        engine.laws = Laws::fit(engine.view.cfg, &engine.points);
        engine.domain = engine.coverage();
        engine
    }

    fn cfg(&self) -> &MapConfig {
        self.view.cfg
    }

    /// Points in measurement order; each ladder aims at the closing of the points before it.
    fn sequential(&self, specs: &[PointSpec], c_plan: f64) -> Vec<PointResult> {
        let mut points: Vec<PointResult> = Vec::new();
        for spec in specs {
            let c = Laws::global_c(c_plan, &points);
            points.push(self.view.point(spec, c));
        }
        points
    }

    /// Frequencies where an amplitude line holds every ion of the best-scan range.
    fn coverage(&self) -> Option<(f64, f64)> {
        let [lo, hi] = self.cfg().bounds.f_khz;
        let best = self.cfg().best_ions();
        let k = self.cfg().ladder.first_k;
        let ok: Vec<f64> = (0..=60)
            .map(|i| lo * 1e3 * (hi / lo).powf(i as f64 / 60.0))
            .filter(|f| {
                let ions = self.view.ions_in_line(*f, k, 0.0);
                best.iter()
                    .all(|m| ions.iter().any(|(i, _)| (i - m).abs() < 0.5))
            })
            .collect();
        Some((*ok.first()?, *ok.last()?))
    }

    fn result(&self, id: &str) -> Option<&PointResult> {
        self.points.iter().find(|p| p.id == id)
    }

    fn comparable(&self) -> Vec<&PointResult> {
        self.points
            .iter()
            .filter(|p| p.mode == "amplitude" && p.covers_best_range && p.feasible && p.merit > 0.0)
            .collect()
    }

    /// Highest lowest-R of the best-range ions on any line that holds all of them.
    fn highest_r(&self) -> f64 {
        let best = self.cfg().best_ions();
        self.view
            .done()
            .filter(|(_, r)| r.request.kind == KIND_AMPLITUDE && r.request.role != ROLE_REFERENCE)
            .filter_map(|(_, r)| {
                let rs: Option<Vec<f64>> = best
                    .iter()
                    .map(|m| r.analysis.peak(*m).map(|p| p.r))
                    .collect();
                rs.map(|v| v.into_iter().fold(f64::INFINITY, f64::min))
            })
            .filter(|r| r.is_finite())
            .fold(0.0, f64::max)
    }

    fn rel_noise(&self) -> f64 {
        median(
            &self
                .points
                .iter()
                .filter(|p| !p.ions.is_empty())
                .map(|p| p.rel_noise)
                .collect::<Vec<_>>(),
        )
        .unwrap_or(0.05)
    }

    /// The best point; when none reaches the target on every ion, the best-resolved point that
    /// still shows every ion of the range, so that the map always ends with a best scan.
    fn best_point(&self) -> Option<&PointResult> {
        let comparable = self.comparable();
        if !comparable.is_empty() {
            return comparable
                .into_iter()
                .max_by(|a, b| a.merit.total_cmp(&b.merit));
        }
        self.points
            .iter()
            .filter(|p| p.mode == "amplitude" && p.covers_best_range && p.merit > 0.0 && p.complete)
            .max_by(|a, b| a.min_r.total_cmp(&b.min_r))
    }

    fn predict_merit(&self, f_hz: f64, energy: f64, quadratic: &Option<Vec<f64>>) -> Option<f64> {
        if self.laws.valid {
            return self.laws.predict(&self.view, f_hz, energy).map(|s| s.merit);
        }
        quadratic.as_ref().map(|q| quadratic_value(q, f_hz, energy))
    }

    fn search_history(&self) -> (Vec<SearchStep>, f64, f64) {
        let mut radius = self.cfg().outer.radius;
        let mut best = self
            .points
            .iter()
            .filter(|p| p.stage == "B")
            .filter(|p| p.mode == "amplitude" && p.covers_best_range && p.feasible)
            .map(|p| p.merit)
            .fold(0.0, f64::max);
        let noise = self.rel_noise();
        let mut steps = Vec::new();
        let last = self.search.len().saturating_sub(1);
        for (i, spec) in self.search.iter().enumerate() {
            let Some(p) = self.result(&spec.id) else {
                continue;
            };
            // Only the last point can still be measuring. An earlier one is a step even when a
            // lower target (0.9 × the highest R reached) asks it for one more line: otherwise
            // it hid every later step from max_ladders and the search ran to max_lines.
            if !p.complete && i == last {
                break;
            }
            let predicted = self
                .view
                .records
                .iter()
                .find(|r| r.request.point == spec.id)
                .map_or(0.0, |r| r.request.predicted);
            if p.feasible && p.merit > best * (1.0 + noise) {
                best = p.merit;
                radius = (radius * 1.5).min(0.3);
            } else {
                radius *= 0.5;
            }
            steps.push(SearchStep {
                point: spec.id.clone(),
                f_hz: p.f_hz,
                energy_ev: p.energy_ev,
                predicted,
                merit: p.merit,
                feasible: p.feasible,
                radius,
            });
        }
        (steps, radius, best)
    }

    fn search_next(&self) -> Option<Request> {
        if let Some(spec) = self.search.last()
            && let Some(p) = self.result(&spec.id)
            && !p.complete
        {
            return self.view.point_next(spec, p);
        }
        let (steps, radius, best) = self.search_history();
        if steps.len() >= self.cfg().outer.max_ladders || radius < 0.02 {
            return None;
        }
        let center = self.best_point()?;
        let (f_lo, f_hi) = self.domain?;
        let quadratic = if self.laws.valid {
            None
        } else {
            local_quadratic(&self.comparable())
        };
        let [e_lo, e_hi] = self.cfg().bounds.energy_ev;
        let (cf, ce) = (center.f_hz.log10(), center.energy_ev.log10());
        if !self.laws.valid && quadratic.is_none() {
            return self.coordinate_step(center, radius, (f_lo, f_hi));
        }
        let measured: Vec<(f64, f64)> = self
            .points
            .iter()
            .filter(|p| p.mode == "amplitude")
            .map(|p| (p.f_hz.log10(), p.energy_ev.log10()))
            .collect();
        let mut proposal: Option<(f64, f64, f64)> = None;
        for i in 0..=24 {
            for j in 0..=24 {
                let lf = f_lo.log10() + (f_hi / f_lo).log10() * i as f64 / 24.0;
                let le = e_lo.log10() + (e_hi / e_lo).log10() * j as f64 / 24.0;
                if ((lf - cf).powi(2) + (le - ce).powi(2)).sqrt() > radius {
                    continue;
                }
                if measured
                    .iter()
                    .any(|(a, b)| ((a - lf).powi(2) + (b - le).powi(2)).sqrt() < 0.015)
                {
                    continue;
                }
                // Whole kHz inside the domain: rounding never steps past its edge.
                let f = round_to(10f64.powf(lf), 1e3)
                    .clamp((f_lo / 1e3).ceil() * 1e3, (f_hi / 1e3).floor() * 1e3);
                let e = round_to(10f64.powf(le), 0.1);
                if !self.view.reachable(f, self.view.amplitude_top(f)) {
                    continue;
                }
                if let Some(m) = self.predict_merit(f, e, &quadratic)
                    && proposal.is_none_or(|p| m > p.2)
                {
                    proposal = Some((f, e, m));
                }
            }
        }
        let (f, e, predicted) = proposal?;
        if predicted <= best * (1.0 + 2.0 * self.rel_noise()) {
            return None; // Converged: nothing within reach is expected to beat the best point.
        }
        let spec = PointSpec {
            id: format!("S{}", self.search.len() + 1),
            stage: "D",
            mode: Mode::Amplitude(f),
            energy: e,
        };
        let empty = PointResult {
            c: self.laws.c,
            c_ladder: self.laws.c,
            ..PointResult::default()
        };
        let mut request = self.view.point_next(&spec, &empty)?;
        request.predicted = predicted;
        Some(request)
    }

    /// Without a model, step from the best point along f and E in turn (the plan's fallback).
    fn coordinate_step(
        &self,
        center: &PointResult,
        radius: f64,
        domain: (f64, f64),
    ) -> Option<Request> {
        let [e_lo, e_hi] = self.cfg().bounds.energy_ev;
        let step = 10f64.powf(radius);
        let candidates = [
            (center.f_hz * step, center.energy_ev),
            (center.f_hz / step, center.energy_ev),
            (center.f_hz, center.energy_ev * step),
            (center.f_hz, center.energy_ev / step),
        ];
        for (f, e) in candidates {
            let (f, e) = (round_to(f, 1e3), round_to(e, 0.1));
            if f < domain.0 || f > domain.1 || e < e_lo || e > e_hi {
                continue;
            }
            if !self.view.reachable(f, self.view.amplitude_top(f)) {
                continue;
            }
            let near = self.points.iter().any(|p| {
                p.mode == "amplitude"
                    && ((p.f_hz / f).log10().powi(2) + (p.energy_ev / e).log10().powi(2)).sqrt()
                        < 0.015
            });
            if near {
                continue;
            }
            let spec = PointSpec {
                id: format!("S{}", self.search.len() + 1),
                stage: "D",
                mode: Mode::Amplitude(f),
                energy: e,
            };
            let empty = PointResult {
                c: self.laws.c,
                c_ladder: self.laws.c,
                ..PointResult::default()
            };
            return self.view.point_next(&spec, &empty);
        }
        None
    }

    fn validation_points(&self) -> Vec<Request> {
        let Some((f_lo, f_hi)) = self.domain else {
            return Vec::new();
        };
        let [e_lo, e_hi] = self.cfg().bounds.energy_ev;
        let quadratic = if self.laws.valid {
            None
        } else {
            local_quadratic(&self.comparable())
        };
        let mut lines = Vec::new();
        for i in 1..400u32 {
            if lines.len() >= self.cfg().validation.points {
                break;
            }
            let (u, v) = sobol2(i);
            let f = round_to(f_lo * (f_hi / f_lo).powf(u), 1e3);
            let e = round_to(e_lo * (e_hi / e_lo).powf(v), 0.1);
            if !self.view.reachable(f, self.view.amplitude_top(f)) {
                continue;
            }
            let (k, delta, predicted) = if let Some(s) = self.laws.predict(&self.view, f, e) {
                (s.k, s.delta, s.merit)
            } else if let Some(near) = self.comparable().into_iter().min_by(|a, b| {
                let d = |p: &PointResult| {
                    (p.f_hz / f).log10().powi(2) + (p.energy_ev / e).log10().powi(2)
                };
                d(a).total_cmp(&d(b))
            }) {
                let m = quadratic.as_ref().map_or(0.0, |q| quadratic_value(q, f, e));
                (near.solution.k, near.solution.delta, m)
            } else {
                continue;
            };
            let id = format!("V{}", lines.len() + 1);
            let mut r =
                self.view
                    .amplitude_line("E", ROLE_VALIDATION, &id, "", f, e, k, delta, None);
            r.predicted = predicted;
            lines.push(r);
        }
        lines
    }

    fn optimum(&self) -> Option<Optimum> {
        let p = self.best_point()?;
        let stub = if p.stub_variation > self.cfg().stub.significant && p.stub_best_v != 0.0 {
            p.stub_best_v
        } else {
            self.view.stub_for(self.view.x_for(p.energy_ev))
        };
        Some(Optimum {
            point: p.id.clone(),
            f_hz: p.f_hz,
            energy_ev: p.energy_ev,
            x_v: self.view.x_for(p.energy_ev),
            k: p.verified_k,
            delta_v: p.verified_delta,
            stub_v: stub,
            merit: p.merit,
        })
    }

    fn local_lines(&self) -> Vec<Request> {
        let Some(o) = self.optimum() else {
            return Vec::new();
        };
        let l = &self.cfg().local;
        let [d_lo, d_hi] = self.cfg().bounds.delta_v;
        let [e_lo, e_hi] = self.cfg().bounds.energy_ev;
        let mut variants: Vec<(String, f64, f64, f64, f64, f64)> = vec![(
            "center".to_owned(),
            o.f_hz,
            o.energy_ev,
            o.k,
            o.delta_v,
            o.stub_v,
        )];
        let mut add = |name: &str, f: f64, e: f64, k: f64, d: f64, s: f64| {
            variants.push((name.to_owned(), f, e, k, d, s));
        };
        add(
            "f+",
            o.f_hz * (1.0 + l.f_rel),
            o.energy_ev,
            o.k,
            o.delta_v,
            o.stub_v,
        );
        add(
            "f-",
            o.f_hz * (1.0 - l.f_rel),
            o.energy_ev,
            o.k,
            o.delta_v,
            o.stub_v,
        );
        add(
            "E+",
            o.f_hz,
            (o.energy_ev * (1.0 + l.e_rel)).min(e_hi),
            o.k,
            o.delta_v,
            o.stub_v,
        );
        add(
            "E-",
            o.f_hz,
            (o.energy_ev * (1.0 - l.e_rel)).max(e_lo),
            o.k,
            o.delta_v,
            o.stub_v,
        );
        add(
            "stub+",
            o.f_hz,
            o.energy_ev,
            o.k,
            o.delta_v,
            o.stub_v + l.stub_v,
        );
        add(
            "stub-",
            o.f_hz,
            o.energy_ev,
            o.k,
            o.delta_v,
            o.stub_v - l.stub_v,
        );
        add("k+", o.f_hz, o.energy_ev, o.k + l.k, o.delta_v, o.stub_v);
        add("k-", o.f_hz, o.energy_ev, o.k - l.k, o.delta_v, o.stub_v);
        add(
            "delta+",
            o.f_hz,
            o.energy_ev,
            o.k,
            (o.delta_v + l.delta_v).min(d_hi),
            o.stub_v,
        );
        add(
            "delta-",
            o.f_hz,
            o.energy_ev,
            o.k,
            (o.delta_v - l.delta_v).max(d_lo),
            o.stub_v,
        );
        let q1 = self.view.inst.q1_dc_v - self.cfg().bounds.stub_below_q1_v;
        let mut lines = Vec::new();
        for (name, f, e, k, d, s) in variants {
            let changed = name == "center"
                || (f - o.f_hz).abs() > 1.0
                || (e - o.energy_ev).abs() > 1e-6
                || (k - o.k).abs() > 1e-7
                || (d - o.delta_v).abs() > 1e-6
                || (s - o.stub_v).abs() > 1e-6;
            if !changed || s >= q1 || !self.view.reachable(f, self.view.amplitude_top(f)) {
                continue;
            }
            for repeat in 0..l.repeats.max(1) {
                let variant = format!("{name} #{}", repeat + 1);
                lines.push(self.view.amplitude_line(
                    "F",
                    ROLE_LOCAL,
                    "L",
                    &variant,
                    f,
                    e,
                    k,
                    d,
                    Some(s),
                ));
            }
        }
        let order = Rng::new(self.cfg().seed ^ 0xF).permutation(lines.len());
        order.into_iter().map(|i| lines[i].clone()).collect()
    }

    /// Σ S of the best-range ions on one line (drift-corrected) and their lowest R; None when an
    /// ion is missing.
    fn line_merit(&self, index: usize, record: &Record) -> Option<(f64, f64)> {
        let best = self.cfg().best_ions();
        let drift = self.view.drift(index);
        let mut merit = 0.0;
        let mut min_r = f64::INFINITY;
        for m in &best {
            let p = record.analysis.peak(*m)?;
            merit += p.height / drift;
            min_r = min_r.min(p.r);
        }
        Some((merit, if min_r.is_finite() { min_r } else { 0.0 }))
    }

    fn feasible_merit(&self, index: usize, record: &Record) -> f64 {
        match self.line_merit(index, record) {
            Some((m, r)) if r >= self.view.accept() => m,
            _ => 0.0,
        }
    }

    /// Mean merit of each local-check variant (center first).
    fn local_results(&self) -> Vec<LocalResult> {
        let mut out: Vec<LocalResult> = Vec::new();
        for (i, r) in self
            .view
            .done()
            .filter(|(_, r)| r.request.role == ROLE_LOCAL)
        {
            let name = r
                .request
                .variant
                .split(" #")
                .next()
                .unwrap_or("")
                .to_owned();
            // A line where an ion was not found counts as measured, but not in the means.
            let measured = self.line_merit(i, r);
            let (merit, min_r, n) = measured.map_or((0.0, 0.0, 0), |m| (m.0, m.1, 1));
            match out.iter_mut().find(|o| o.name == name) {
                Some(o) => {
                    o.merit += merit;
                    o.min_r += min_r;
                    o.found += n;
                    o.lines += 1;
                }
                None => out.push(LocalResult {
                    name,
                    merit,
                    min_r,
                    found: n,
                    lines: 1,
                    request: r.request.clone(),
                    verdict: String::new(),
                }),
            }
        }
        for o in &mut out {
            if o.found > 0 {
                o.merit /= o.found as f64;
                o.min_r /= o.found as f64;
            }
        }
        out.sort_by_key(|o| o.name != "center");
        let noise = self.rel_noise();
        let target = self.view.target;
        let (c_merit, c_r) = out
            .iter()
            .find(|o| o.name == "center")
            .map_or((0.0, 0.0), |c| (c.merit, c.min_r));
        for o in &mut out {
            // A neighbour is better only at the same resolution: a lower k or a larger δ always
            // gives more signal by giving up resolution.
            let resolved = o.min_r >= self.view.accept() && o.min_r >= 0.97 * c_r.min(target);
            o.verdict = if o.name == "center" {
                "center"
            } else if resolved && o.merit > c_merit * (1.0 + 2.0 * noise) {
                "higher"
            } else if !resolved || o.merit < c_merit * (1.0 - 2.0 * noise) {
                "lower"
            } else {
                "equal"
            }
            .to_owned();
        }
        out
    }

    fn best_settings(&self) -> Option<Request> {
        let locals = self.local_results();
        if let Some(c) = locals.iter().find(|o| o.name == "center") {
            let better = locals
                .iter()
                .filter(|o| o.verdict == "higher")
                .max_by(|a, b| a.merit.total_cmp(&b.merit));
            return Some(better.unwrap_or(c).request.clone());
        }
        let o = self.optimum()?;
        Some(self.view.amplitude_line(
            "G",
            ROLE_BEST,
            "BEST",
            "",
            o.f_hz,
            o.energy_ev,
            o.k,
            o.delta_v,
            Some(o.stub_v),
        ))
    }

    fn frequency_best(&self) -> Option<Request> {
        let clock = self.view.inst.clock_hz;
        self.points
            .iter()
            .filter(|p| p.mode == "frequency" && p.feasible && p.covers_best_range)
            .max_by(|a, b| a.merit.total_cmp(&b.merit))
            .and_then(|p| {
                let r = self.view.frequency_line(
                    "G",
                    ROLE_BEST,
                    "BEST",
                    "",
                    p.amplitude_v,
                    p.energy_ev,
                    p.solution.k,
                    0.0,
                )?;
                // One period tick must keep two points per peak width at R_max.
                let top = r.axis.last().copied().unwrap_or(0.0);
                (2.0 * top / clock <= 1.0 / (2.0 * self.cfg().r_max)).then_some(r)
            })
    }

    fn best_next(&self) -> Option<Decision> {
        let done: Vec<(usize, &Record)> = self
            .view
            .done()
            .filter(|(_, r)| r.request.role == ROLE_BEST)
            .collect();
        let mut amplitude = self.best_settings()?;
        amplitude.stage = "G".to_owned();
        amplitude.role = ROLE_BEST.to_owned();
        amplitude.point = "BEST".to_owned();
        amplitude.label = "best".to_owned();
        let mut first = vec![{
            let mut r = amplitude.clone();
            r.variant = "amplitude #1".to_owned();
            r
        }];
        if let Some(mut f) = self.frequency_best() {
            f.variant = "frequency #1".to_owned();
            first.push(f);
        }
        for r in &first {
            if !done.iter().any(|(_, d)| d.request.variant == r.variant) {
                return Some(Decision::Line(r.clone()));
            }
        }
        let winner = first
            .iter()
            .filter_map(|r| {
                done.iter()
                    .find(|(_, d)| d.request.variant == r.variant)
                    .map(|(i, d)| (r, self.feasible_merit(*i, d)))
            })
            .max_by(|a, b| a.1.total_cmp(&b.1))
            .map(|(r, _)| r.clone())?;
        let mode = winner
            .variant
            .split(" #")
            .next()
            .unwrap_or("amplitude")
            .to_owned();
        for n in 2..=self.cfg().best.repeats.max(1) {
            let variant = format!("{mode} #{n}");
            if done.iter().any(|(_, d)| d.request.variant == variant) {
                continue;
            }
            let last_start = done.iter().map(|(_, d)| d.started).fold(0.0, f64::max);
            let wait = last_start + self.cfg().best.spacing_s - self.view.now();
            if wait > 1.0 {
                let mut w =
                    self.view
                        .base_request("G", ROLE_WAIT, "BEST", &format!("before {variant}"));
                w.kind = KIND_WAIT.to_owned();
                w.seconds = wait.min(3600.0).round();
                w.expected_s = w.seconds;
                w.label = "wait".to_owned();
                return Some(Decision::Wait(w));
            }
            let mut r = winner.clone();
            r.variant = variant;
            return Some(Decision::Line(r));
        }
        None
    }

    fn reference_due(&self) -> bool {
        let mut since = 0usize;
        let mut any = false;
        for r in self.view.records.iter().filter(|r| r.outcome == DONE) {
            match r.request.role.as_str() {
                ROLE_REFERENCE => {
                    any = true;
                    since = 0;
                }
                ROLE_WAIT => {}
                _ => since += 1,
            }
        }
        !any || since >= self.cfg().reference_every.max(1)
    }

    fn instrument_check(&self) -> Option<String> {
        let first = self.design.first()?;
        let k0 = self.cfg().ladder.first_k;
        let (_, r) = self.view.of(&first.id).into_iter().find(|(_, r)| {
            r.outcome == DONE && r.request.role == ROLE_LADDER && (r.request.k - k0).abs() < 5e-6
        })?;
        let found: Vec<&Peak> = r.analysis.peaks.iter().filter(|p| p.found).collect();
        if found.len() < 2 {
            return Some(format!(
                "instrument check: {} shows {} mix ion(s) at k = {k0} (two or more expected)",
                first.id,
                found.len()
            ));
        }
        let rs: Vec<f64> = found.iter().map(|p| p.r).filter(|r| *r > 0.0).collect();
        match median(&rs) {
            Some(r) if !(6.0..=30.0).contains(&r) => Some(format!(
                "instrument check: {} gives R ≈ {r:.0} at k = {k0} (12–16 on 2026-11-03)",
                first.id
            )),
            _ => None,
        }
    }

    fn decide(&self) -> Decision {
        let records = self.view.records;
        if records.len() >= self.cfg().max_lines {
            return Decision::Stop(format!("line budget of {} reached", self.cfg().max_lines));
        }
        if let Some(last) = records.last() {
            let key = last.request.key();
            let failures = records
                .iter()
                .rev()
                .take_while(|r| r.request.key() == key && r.outcome == FAILED)
                .count();
            if failures >= self.cfg().max_failures.max(1) {
                return Decision::Stop(format!(
                    "line {} failed {failures} times: {}",
                    last.request.label, last.error
                ));
            }
            if last.outcome == UNREACHABLE
                && matches!(
                    last.request.role.as_str(),
                    ROLE_REFERENCE | ROLE_STOPPING | ROLE_BEST
                )
            {
                return Decision::Stop(format!(
                    "{} line unreachable: {}",
                    last.request.role, last.error
                ));
            }
        }
        if self.reference_due() {
            return Decision::Line(self.view.reference_line());
        }
        if self.cfg().stages.stopping {
            for line in self.view.stopping_lines() {
                if !self.view.done().any(|(_, r)| {
                    r.request.role == ROLE_STOPPING && r.request.variant == line.variant
                }) {
                    return Decision::Line(line);
                }
            }
            if self.view.stopping_summary().is_none() {
                return Decision::Stop(
                    "the stopping cut found no ion: check the beam before a map".to_owned(),
                );
            }
        }
        if let Some(reason) = self.instrument_check() {
            return Decision::Stop(reason);
        }
        for spec in &self.design {
            if let Mode::Amplitude(f) = spec.mode {
                let ions = self.view.ions_in_line(f, self.cfg().ladder.first_k, 0.0);
                if ions.len() < self.cfg().min_ions_in_line {
                    continue;
                }
            }
            if let Some(result) = self.result(&spec.id)
                && let Some(request) = self.view.point_next(spec, result)
            {
                return Decision::Line(request);
            }
        }
        if self.cfg().stages.search
            && let Some(request) = self.search_next()
        {
            return Decision::Line(request);
        }
        if self.cfg().stages.validation {
            let done: Vec<&Record> = records
                .iter()
                .filter(|r| r.request.role == ROLE_VALIDATION && r.outcome != FAILED)
                .collect();
            for line in self.validation_points() {
                if !done.iter().any(|r| r.request.point == line.point) {
                    return Decision::Line(line);
                }
            }
        }
        if self.cfg().stages.local {
            let done: Vec<&Record> = records
                .iter()
                .filter(|r| r.request.role == ROLE_LOCAL && r.outcome != FAILED)
                .collect();
            for line in self.local_lines() {
                if !done.iter().any(|r| r.request.variant == line.variant) {
                    return Decision::Line(line);
                }
            }
        }
        if self.cfg().stages.best
            && let Some(decision) = self.best_next()
        {
            return decision;
        }
        Decision::Done(match self.optimum() {
            Some(o) => format!(
                "map complete: best at {:.0} kHz, E = {:.1} eV, k = {:.5}, δ = {:.2} V",
                o.f_hz / 1e3,
                o.energy_ev,
                o.k,
                o.delta_v
            ),
            None => {
                "map complete: no setting reached R_target on every ion of the range".to_owned()
            }
        })
    }

    fn stage_name(stage: &str) -> &'static str {
        match stage {
            "R" => "reference",
            "A" => "stopping cut",
            "B" => "ladders at the design points",
            "D" => "outer search",
            "E" => "out-of-sample check",
            "F" => "local check",
            "G" => "best scan",
            _ => "",
        }
    }

    fn progress(&self, next: Option<&Request>) -> Value {
        let durations: Vec<f64> = self
            .view
            .done()
            .filter(|(_, r)| r.request.kind == KIND_AMPLITUDE && r.request.role != ROLE_REFERENCE)
            .map(|(_, r)| r.duration_s)
            .collect();
        let typical = median(&durations).unwrap_or(600.0);
        let incomplete = self
            .points
            .iter()
            .filter(|p| p.stage == "B" && !p.complete)
            .count();
        let cfg = self.cfg();
        let mut left = incomplete * 11;
        if cfg.stages.search {
            left += cfg.outer.max_ladders.saturating_sub(self.search.len()) * 6;
        }
        if cfg.stages.validation {
            left += cfg.validation.points;
        }
        if cfg.stages.local {
            left += 22;
        }
        if cfg.stages.best {
            left += cfg.best.repeats + 1;
        }
        left += left / cfg.reference_every.max(1);
        json!({
            "lines": self.view.records.len(),
            "done": self.view.done().count(),
            "failed": self.view.records.iter().filter(|r| r.outcome == FAILED).count(),
            "stage": next.map_or("", |r| Self::stage_name(&r.stage)),
            "point": next.map_or("", |r| r.point.as_str()),
            "lines_left_estimate": left,
            "remaining_s_estimate": left as f64 * typical,
            "x_stop": self.view.x_stop,
            "c": self.laws.c,
        })
    }
}

// ---------------------------------------------------------------- the Q2 simulator

#[derive(Clone, Debug, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct SimOptions {
    /// H1: c 0.978 and R capped near 45 for every ion; H2: c 0.99, no cap; H4: R_cap = N²/16.
    pub hypothesis: String,
    pub seed: u64,
    /// Every n-th line fails once (spray drop-out), 0 for never.
    pub fail_every: usize,
    pub drift: f64,
    pub noise_a: f64,
    pub max_lines: usize,
    pub start: f64,
    pub journal: bool,
}

impl Default for SimOptions {
    fn default() -> Self {
        Self {
            hypothesis: "H4".to_owned(),
            seed: 7,
            fail_every: 0,
            drift: 0.08,
            noise_a: 2e-14,
            max_lines: 1200,
            start: 1.8e9,
            journal: false,
        }
    }
}

fn abundance(mz: f64) -> f64 {
    match mz.round() as i64 {
        118 => 0.3,
        322 => 0.6,
        622 => 1.0,
        922 => 0.9,
        1522 => 0.7,
        2122 => 0.5,
        2722 => 0.3,
        _ => 0.5,
    }
}

struct Q2Sim {
    cfg: MapConfig,
    inst: Instrument,
    opts: SimOptions,
    c: f64,
    r0: f64,
    rng: Rng,
    clock: f64,
    lines: usize,
}

impl Q2Sim {
    fn new(cfg: &MapConfig, inst: &Instrument, opts: &SimOptions) -> Self {
        let c = if opts.hypothesis == "H2" { 0.99 } else { 0.978 };
        Self {
            cfg: cfg.clone(),
            inst: inst.clone(),
            opts: opts.clone(),
            c,
            r0: cfg.calibration.r0_mm * 1.001,
            rng: Rng::new(opts.seed),
            clock: opts.start,
            lines: 0,
        }
    }

    fn cap(&self, n: f64) -> f64 {
        match self.opts.hypothesis.as_str() {
            "H1" => 45.0,
            "H4" => n * n / 16.0,
            _ => 1e6,
        }
    }

    fn x_stop(&self, q1: f64) -> f64 {
        q1 - 0.3
    }

    fn truth(&self, k: f64, delta: f64) -> Filter {
        Filter {
            k,
            delta,
            umin: self.cfg.calibration.u_min_v,
            c: self.c,
            r0_mm: self.r0,
        }
    }

    /// Height (A) of ion `mz` transmitted at ratio k_eff, energy E, frequency f and entry stub.
    fn height(&self, mz: f64, k_eff: f64, energy: f64, f_hz: f64, stub: f64, x: f64) -> f64 {
        if energy <= 0.05 || k_eff >= APEX {
            return 0.0;
        }
        let g_e = (-(energy / 8.0).ln().powi(2) / (2.0 * 0.9 * 0.9)).exp();
        let g_f = (f_hz / 500e3).powf(0.2);
        let optimum = x + 0.42 * (self.inst.ap_q1_q2_v - x) + 1.5;
        let g_stub = (1.0 - 0.12 * ((stub - optimum) / 5.0).powi(2)).max(0.3);
        let t = ((APEX - k_eff) / (APEX - 0.2 / self.c)).clamp(0.0, 3.0);
        2e-12 * abundance(mz) * g_e * g_f * g_stub * t
    }

    fn drift(&self) -> f64 {
        1.0 + self.opts.drift
            * (2.0 * std::f64::consts::PI * (self.clock - self.opts.start) / 21600.0).sin()
    }

    fn model(&self, req: &Request, ions: &[(f64, f64)], value: f64) -> f64 {
        let energy = self.x_stop(self.inst.q1_dc_v) - req.x_v;
        let baseline = -3e-13
            + 2e-16
                * if req.kind == KIND_AMPLITUDE {
                    value
                } else {
                    100.0
                };
        let mut y = 0.0;
        match req.kind.as_str() {
            KIND_AMPLITUDE => {
                let truth = self.truth(req.k, req.delta_v);
                for &(mz, a) in ions {
                    let k_eff = truth.k_eff(a);
                    let n = cycles(req.f_hz, mz, energy.max(0.1), self.cfg.q2_length_m);
                    let r = 1.0 / (1.0 / ideal_r(k_eff) + 1.0 / self.cap(n));
                    let h = self.height(mz, k_eff, energy, req.f_hz, req.stub_v, req.x_v);
                    y += h * (-4.0 * 2f64.ln() * ((value - a) / (a / r)).powi(2)).exp();
                }
            }
            KIND_FREQUENCY => {
                let truth = self.truth(req.k, req.delta_v);
                let k_eff = truth.k_eff(req.amplitude_v);
                for &(mz, f) in ions {
                    let n = cycles(f, mz, energy.max(0.1), self.cfg.q2_length_m);
                    let r = 1.0 / (1.0 / ideal_r(k_eff) + 1.0 / self.cap(n));
                    let h = self.height(mz, k_eff, energy, f, req.stub_v, req.x_v);
                    y += h * (-4.0 * 2f64.ln() * ((value - f) / (f / (2.0 * r))).powi(2)).exp();
                }
            }
            KIND_OFFSET => {
                let truth = self.truth(req.k, req.delta_v);
                if let Some(&(mz, _)) = ions.first() {
                    let k_eff = truth.k_eff(req.amplitude_v);
                    let h = self.height(mz, k_eff, 26.0, req.f_hz, req.stub_v, req.x_v);
                    let stop = self.x_stop(req.q1_dc_v);
                    y += h * 0.5 * erfc((value - stop) / (2f64.sqrt() * 0.6));
                }
            }
            KIND_EXTRA => {
                let truth = self.truth(req.k, req.delta_v);
                if let Some(&(mz, _)) = ions.first() {
                    let k_eff = truth.k_eff(req.amplitude_v);
                    y += self.height(mz, k_eff, energy, req.f_hz, value, req.x_v);
                }
            }
            _ => {}
        }
        y * self.drift() + baseline
    }

    fn run(&mut self, req: &Request) -> LineResult {
        self.lines += 1;
        let mut result = LineResult {
            id: req.id,
            outcome: DONE.to_owned(),
            axis: req.axis.clone(),
            started: self.clock,
            ..LineResult::default()
        };
        let top = match req.kind.as_str() {
            KIND_AMPLITUDE => {
                self.inst.current_per_hz_v * req.f_hz * req.axis.iter().copied().fold(0.0, f64::max)
            }
            KIND_FREQUENCY => {
                self.inst.current_per_hz_v
                    * req.axis.last().copied().unwrap_or(0.0)
                    * req.amplitude_v
            }
            _ => self.inst.current_per_hz_v * req.f_hz * req.amplitude_v,
        };
        if top > self.inst.ilim_a {
            result.outcome = UNREACHABLE.to_owned();
            result.error = "simulated: PSU current would exceed Ilim".to_owned();
            result.duration_s = 20.0;
            self.clock += 20.0;
            return result;
        }
        if self.opts.fail_every > 0 && self.lines.is_multiple_of(self.opts.fail_every) {
            result.outcome = FAILED.to_owned();
            result.error = "simulated: spray current left its window".to_owned();
            result.duration_s = 0.5 * req.expected_s;
            self.clock += result.duration_s;
            return result;
        }
        let truth = self.truth(req.k, req.delta_v);
        let ions: Vec<(f64, f64)> = self
            .cfg
            .mix
            .iter()
            .filter_map(|m| {
                let at = match req.kind.as_str() {
                    KIND_FREQUENCY => truth.frequency_for(*m, req.amplitude_v),
                    _ if req.ions.iter().any(|i| (i - m).abs() < 0.5)
                        || req.kind == KIND_AMPLITUDE =>
                    {
                        truth.amplitude_for(*m, req.f_hz)
                    }
                    _ => None,
                };
                at.map(|a| (*m, a))
            })
            .filter(|(m, _)| {
                req.kind == KIND_AMPLITUDE
                    || req.kind == KIND_FREQUENCY
                    || req.ions.iter().any(|i| (i - m).abs() < 0.5)
            })
            .collect();
        let n = req.axis.len();
        let mut signal = vec![f64::NAN; n];
        let mut sigma = vec![f64::NAN; n];
        let noise = self.opts.noise_a;
        let mut measure = |i: usize, sim: &mut Self| {
            let y = sim.model(req, &ions, req.axis[i]) + noise * sim.rng.normal();
            signal[i] = y;
            sigma[i] = noise;
            y
        };
        let mut count = 0usize;
        if req.adaptive {
            let mut stepper = Stepper::new(
                StepPolicy {
                    coarse: req.coarse.max(1),
                    noise_floor: noise,
                    ..StepPolicy::default()
                },
                n,
            );
            while let Some(i) = stepper.next_index() {
                let y = measure(i, self);
                stepper.record(i, y, noise);
                count += 1;
                if count > 4 * n {
                    break;
                }
            }
        } else {
            for i in 0..n {
                measure(i, self);
                count += 1;
            }
        }
        result.signal = signal;
        result.sigma = sigma;
        result.spray = vec![45e-9 * (1.0 + 0.01 * self.rng.normal()); 3];
        let per_point = self.cfg.point_s
            + self.cfg.settle_s
            + self.cfg.point_overhead_s.max(0.0)
            + if req.kind == KIND_FREQUENCY { 1.5 } else { 0.0 };
        result.duration_s = count as f64 * per_point + LINE_S;
        self.clock += result.duration_s;
        result
    }
}

// ---------------------------------------------------------------- check, begin, next, record, report

fn parse_plan(value: &Value) -> Result<Plan> {
    serde_json::from_value(value.clone())
        .map_err(|e| Error::argument(format!("Invalid map plan: {e}")))
}

fn parse_instrument(value: &Value) -> Result<Instrument> {
    let inst: Instrument = serde_json::from_value(value.clone())
        .map_err(|e| Error::argument(format!("Invalid instrument readings: {e}")))?;
    let numbers = [
        inst.q1_dc_v,
        inst.ap_q1_q2_v,
        inst.x_v,
        inst.ilim_a,
        inst.current_per_hz_v,
        inst.psu_max_v,
        inst.clock_hz,
        inst.f_hz,
    ];
    if numbers.iter().any(|v| !v.is_finite()) || inst.clock_hz <= 0.0 || inst.ilim_a <= 0.0 {
        return Err(Error::argument(
            "Instrument readings must be finite (Ilim and clock positive).",
        ));
    }
    Ok(inst)
}

fn parse_journal(value: &Value) -> Result<Journal> {
    let journal: Journal = serde_json::from_value(value.clone())
        .map_err(|e| Error::argument(format!("Invalid map journal: {e}")))?;
    if journal.version != JOURNAL_VERSION {
        return Err(Error::argument(format!(
            "Map journal version {} is not supported (expected {JOURNAL_VERSION}).",
            journal.version
        )));
    }
    Ok(journal)
}

fn check(plan: &Plan, inst: &Instrument) -> (Vec<String>, Vec<String>, Value) {
    let cfg = &plan.map;
    let mut problems = Vec::new();
    let mut warnings = Vec::new();
    let [a_lo, a_hi] = cfg.a_range_v;
    let [f_lo, f_hi] = cfg.bounds.f_khz;
    let [e_lo, e_hi] = cfg.bounds.energy_ev;
    if !(a_lo > 0.0 && a_hi > a_lo) {
        problems.push("a_range_v must be increasing and positive.".to_owned());
    }
    if !(f_lo > 0.0 && f_hi > f_lo && e_lo > 0.0 && e_hi > e_lo) {
        problems
            .push("bounds.f_khz and bounds.energy_ev must be increasing and positive.".to_owned());
    }
    if cfg.bounds.delta_v[0] < 0.0 {
        problems.push(
            "A negative δ is not supported by MScan yet (bounds.delta_v must start at 0)."
                .to_owned(),
        );
    }
    if !(cfg.a_margin >= 0.0 && cfg.a_margin <= 1.0) {
        problems.push("a_margin must lie between 0 and 1.".to_owned());
    }
    if !(cfg.point_overhead_s >= 0.0 && cfg.point_overhead_s.is_finite()) {
        problems.push("point_overhead_s must be zero or positive.".to_owned());
    }
    if !(cfg.spray_window_percent > 0.0 && cfg.spray_window_percent.is_finite()) {
        problems.push("spray_window_percent must be positive.".to_owned());
    }
    match cfg.spray_range_na.as_slice() {
        [] => {}
        [lo, hi] if lo.is_finite() && hi.is_finite() && *lo >= 0.0 && hi > lo => {}
        _ => problems.push(
            "spray_range_na must be empty (window around the reference) or [low, high] in nA, 0 <= low < high."
                .to_owned(),
        ),
    }
    if !(cfg.r_target > 1.0 && cfg.r_max >= cfg.r_target) {
        problems.push("r_target must exceed 1 and r_max must be at least r_target.".to_owned());
    }
    if cfg.mix.is_empty() || cfg.best_ions().len() < 2 {
        problems.push("The best-scan range must hold at least two mix ions.".to_owned());
    }
    if a_hi > inst.psu_max_v - 1.0 {
        problems.push(format!(
            "Amplitude {a_hi} V needs a PSU maximum above {:.0} V (now {:.0} V).",
            a_hi + 1.0,
            inst.psu_max_v
        ));
    }
    if a_hi > 245.0 && !inst.full_range {
        problems.push(format!(
            "Switch the PSU to Full range: {a_hi} V is above the Half range (MScan never switches it)."
        ));
    }
    let journal = Journal {
        version: JOURNAL_VERSION,
        plan: plan.clone(),
        instrument: inst.clone(),
        ..Journal::default()
    };
    let view = View::new(&journal);
    let mut table = Vec::new();
    let mut worst: f64 = 0.0;
    for (i, p) in cfg.design.amplitude.iter().enumerate() {
        let id = format!("P{}", i + 1);
        let (f, e) = (p[0] * 1e3, p[1]);
        if p[0] < f_lo || p[0] > f_hi || e < e_lo || e > e_hi {
            problems.push(format!(
                "{id} ({} kHz, {e} eV) lies outside the bounds.",
                p[0]
            ));
        }
        let ions = view.ions_in_line(f, cfg.ladder.first_k, 0.0);
        if ions.len() < cfg.min_ions_in_line {
            warnings.push(format!(
                "{id}: {} mix ion(s) in its line, fewer than {}: it is left out.",
                ions.len(),
                cfg.min_ions_in_line
            ));
        }
        let current = view.current_at(f, view.amplitude_top(f));
        worst = worst.max(current);
        let x = view.x_for(e);
        table.push(json!({"point": id, "mode": "amplitude", "f_khz": p[0], "energy_ev": e, "x_v": x,
            "stub_v": view.stub_for(x), "ions": ions.iter().map(|i| i.0.round()).collect::<Vec<_>>(),
            "cycles": ions.iter().map(|i| cycles(f, i.0, e, cfg.q2_length_m).round()).collect::<Vec<_>>(),
            "current_ma": current * 1e3}));
    }
    for (i, p) in cfg.design.frequency.iter().enumerate() {
        let id = format!("F{}", i + 1);
        let (a, e) = (p[0], p[1]);
        match view.frequency_range(a, cfg.ladder.first_k, 0.0) {
            Some((lo, hi, ions)) => {
                let current = view.current_at(hi, a);
                worst = worst.max(current);
                if 2.0 * hi / inst.clock_hz > 1.0 / (2.0 * cfg.r_max) {
                    warnings.push(format!(
                        "{id}: one oscillator tick is {:.1} % in m/z at {:.0} kHz, coarser than R_max = {} \
                         needs: its ladders inform the laws, no best frequency scan from it.",
                        200.0 * hi / inst.clock_hz,
                        hi / 1e3,
                        cfg.r_max
                    ));
                }
                table.push(json!({"point": id, "mode": "frequency", "amplitude_v": a, "energy_ev": e,
                    "f_khz": [lo / 1e3, hi / 1e3], "ions": ions.iter().map(|m| m.round()).collect::<Vec<_>>(),
                    "current_ma": current * 1e3}));
            }
            None => problems.push(format!(
                "{id}: no mix ion between {f_lo} and {f_hi} kHz at {a} V."
            )),
        }
    }
    if cfg.stages.search || cfg.stages.validation {
        worst = worst.max(
            view.current_at(f_hi * 1e3, a_hi)
                .min(view.current_at(600e3, a_hi)),
        );
    }
    if worst > 0.95 * inst.ilim_a {
        let f_ok = 0.95 * inst.ilim_a / (inst.current_per_hz_v * a_hi);
        problems.push(format!(
            "The RF current reaches about {:.0} mA (it follows from f and A): set Ilim to at least {:.0} mA on the PSU \
             yourself (now {:.0} mA; MScan never changes Ilim), or limit f to {:.0} kHz in the plan.",
            worst * 1e3,
            (worst / 0.95 * 1e3 / 10.0).ceil() * 10.0,
            inst.ilim_a * 1e3,
            f_ok / 1e3
        ));
    }
    // AMPR limits: the rod pairs x ± U, the entry stub, Q1_DC + shift.
    let u_max = APEX * a_hi;
    let x_lo = inst.q1_dc_v - e_hi;
    let x_hi = (inst.q1_dc_v - e_lo).max(if cfg.stages.stopping {
        cfg.stopping.x_to_v
    } else {
        f64::MIN
    });
    if x_lo - u_max < inst.pair_min_v || x_hi + u_max > inst.pair_max_v {
        problems.push(format!(
            "Rod pairs would need {:.0} to {:.0} V; the AMPR channels allow {:.0} to {:.0} V.",
            x_lo - u_max,
            x_hi + u_max,
            inst.pair_min_v,
            inst.pair_max_v
        ));
    }
    let stub_lo = x_lo + cfg.stub.cut_from_x_v;
    let stub_hi = inst.q1_dc_v - cfg.bounds.stub_below_q1_v;
    if stub_lo < inst.stub_min_v || stub_hi > inst.stub_max_v {
        problems.push(format!(
            "The entry stub would need {stub_lo:.0} to {stub_hi:.0} V; its channel allows {:.0} to {:.0} V.",
            inst.stub_min_v, inst.stub_max_v
        ));
    }
    if cfg.stages.stopping && inst.q1_dc_v + cfg.stopping.q1_shift_v > inst.q1_max_v {
        problems.push(format!(
            "Q1_DC + {} V exceeds its channel maximum {:.0} V.",
            cfg.stopping.q1_shift_v, inst.q1_max_v
        ));
    }
    let needs_frequency = !cfg.design.frequency.is_empty()
        || cfg
            .design
            .amplitude
            .iter()
            .any(|p| (p[0] * 1e3 - inst.f_hz).abs() > 1.0)
        || cfg.stages.search;
    if needs_frequency && !inst.frequency_writable {
        problems.push(
            "The AMX frequency cannot be set from MScan (AMX HD with its 100 MHz clock required)."
                .to_owned(),
        );
    }
    let bytes = cfg.max_lines as f64 * 4e6;
    if inst.free_disk_bytes < bytes {
        problems.push(format!(
            "Free disk space {:.1} GB, at least {:.1} GB needed.",
            inst.free_disk_bytes / 1e9,
            bytes / 1e9
        ));
    }
    (
        problems,
        warnings,
        json!({"points": table, "worst_current_ma": worst * 1e3}),
    )
}

fn new_journal(plan: Plan, inst: Instrument, started: f64) -> Journal {
    Journal {
        version: JOURNAL_VERSION,
        engine: format!("mscan_map {JOURNAL_VERSION}"),
        plan,
        instrument: inst,
        started,
        records: Vec::new(),
    }
}

fn numbered(journal: &Journal, mut request: Request) -> Request {
    request.id = journal.records.len() as u64 + 1;
    request
}

fn next(journal: &Journal) -> Value {
    let engine = Engine::new(journal);
    match engine.decide() {
        Decision::Line(r) => {
            let r = numbered(journal, r);
            json!({"decision": "line", "request": r, "progress": engine.progress(Some(&r))})
        }
        Decision::Wait(r) => {
            let r = numbered(journal, r);
            json!({"decision": "wait", "request": r, "progress": engine.progress(Some(&r))})
        }
        Decision::Done(reason) => {
            json!({"decision": "done", "reason": reason, "progress": engine.progress(None)})
        }
        Decision::Stop(reason) => {
            json!({"decision": "stop", "reason": reason, "progress": engine.progress(None)})
        }
    }
}

fn record(journal: &mut Journal, result: &LineResult) -> Result<Analysis> {
    let decision = Engine::new(journal).decide();
    let request = match decision {
        Decision::Line(r) | Decision::Wait(r) => numbered(journal, r),
        Decision::Done(reason) | Decision::Stop(reason) => {
            return Err(Error::runtime(format!("No line is expected: {reason}")));
        }
    };
    if result.id != request.id {
        return Err(Error::runtime(format!(
            "Result of line {} does not match the expected line {}.",
            result.id, request.id
        )));
    }
    if ![DONE, FAILED, UNREACHABLE].contains(&result.outcome.as_str()) {
        return Err(Error::argument(
            "Line outcome must be done, failed or unreachable.",
        ));
    }
    if result.outcome == DONE
        && request.kind != KIND_WAIT
        && result.axis.len() != request.axis.len()
    {
        return Err(Error::argument(
            "Line arrays do not match the requested axis.",
        ));
    }
    let analysis = if result.outcome == DONE && request.kind != KIND_WAIT {
        analyse(&journal.plan.map, &request, result)
    } else {
        Analysis::default()
    };
    let started = if result.started.is_finite() {
        result.started
    } else {
        0.0
    };
    let duration = if result.duration_s.is_finite() {
        result.duration_s.max(0.0)
    } else {
        0.0
    };
    journal.records.push(Record {
        request,
        outcome: result.outcome.clone(),
        error: result.error.clone(),
        started,
        duration_s: duration,
        file: result.file.clone(),
        analysis: analysis.clone(),
    });
    Ok(analysis)
}

fn report(journal: &Journal) -> Value {
    let engine = Engine::new(journal);
    let view = &engine.view;
    let references: Vec<Value> = view
        .done()
        .filter(|(_, r)| r.request.role == ROLE_REFERENCE)
        .map(|(i, r)| {
            json!({"line": i + 1, "started": r.started,
                "height": r.analysis.peak(view.cfg.reference.ion).map_or(0.0, |p| p.height),
                "drift": view.drift(i)})
        })
        .collect();
    let validation: Vec<Value> = view
        .done()
        .filter(|(_, r)| r.request.role == ROLE_VALIDATION)
        .map(|(i, r)| {
            let measured = engine.feasible_merit(i, r);
            let predicted = r.request.predicted;
            let tolerance = (2.0 * engine.rel_noise()).max(0.15);
            json!({"point": r.request.point, "f_hz": r.request.f_hz, "energy_ev": r.request.energy_ev,
                "k": r.request.k, "delta_v": r.request.delta_v, "predicted": predicted, "measured": measured,
                "within": predicted > 0.0 && (measured - predicted).abs() <= tolerance * predicted})
        })
        .collect();
    let passed = validation
        .iter()
        .filter(|v| v["within"] == json!(true))
        .count();
    let local = engine.local_results();
    let local_passed = !local.is_empty() && local.iter().all(|l| l.verdict != "higher");
    let noise = engine.rel_noise();
    let best: Vec<Value> = view
        .done()
        .filter(|(_, r)| r.request.role == ROLE_BEST)
        .map(|(i, r)| {
            let rs: Vec<f64> = r.analysis.peaks.iter().filter(|p| p.found).map(|p| p.r).collect();
            json!({"line": i + 1, "variant": r.request.variant, "file": r.file, "started": r.started,
                "merit": engine.line_merit(i, r).map_or(0.0, |m| m.0),
                "min_r": rs.iter().copied().fold(f64::INFINITY, f64::min).min(1e9),
                "request": r.request})
        })
        .collect();
    let mut convergence = Vec::new();
    let mut best_so_far: f64 = 0.0;
    for p in &engine.points {
        if p.mode == "amplitude" && p.covers_best_range && p.feasible {
            best_so_far = best_so_far.max(p.merit);
        }
        if p.complete && !p.ions.is_empty() {
            convergence.push(json!({"point": p.id, "merit": p.merit, "best": best_so_far}));
        }
    }
    let mut pareto: Vec<(f64, f64, String, f64)> = Vec::new();
    for (i, r) in view.done() {
        if !matches!(
            r.request.role.as_str(),
            ROLE_LADDER | ROLE_REPEAT | ROLE_VERIFY | ROLE_VALIDATION | ROLE_LOCAL | ROLE_BEST
        ) || r.request.kind != KIND_AMPLITUDE
        {
            continue;
        }
        let best_ions = view.cfg.best_ions();
        let found: Vec<&Peak> = best_ions
            .iter()
            .filter_map(|m| r.analysis.peak(*m))
            .collect();
        if found.len() < best_ions.len() {
            continue;
        }
        let min_r = found.iter().map(|p| p.r).fold(f64::INFINITY, f64::min);
        let merit: f64 = found.iter().map(|p| p.height).sum::<f64>() / view.drift(i);
        if min_r.is_finite() && min_r > 0.0 {
            pareto.push((min_r, merit, r.request.point.clone(), r.request.k));
        }
    }
    pareto.sort_by(|a, b| b.0.total_cmp(&a.0));
    let mut front = Vec::new();
    let mut top = f64::NEG_INFINITY;
    for p in pareto {
        if p.1 > top {
            top = p.1;
            front.push(json!({"min_r": p.0, "merit": p.1, "point": p.2, "k": p.3}));
        }
    }
    let (search, radius, _) = engine.search_history();
    let decision = next(journal);
    json!({
        "name": view.cfg.name,
        "started": journal.started,
        "lines": journal.records.len(),
        "stopping": view.stopping_summary(),
        "references": references,
        "points": engine.points,
        "laws": engine.laws,
        "domain_hz": engine.domain.map(|d| vec![d.0, d.1]),
        "search": search,
        "search_radius": radius,
        "optimum": engine.optimum(),
        "validation": validation,
        "validation_passed": passed,
        "validation_required": view.cfg.validation.pass,
        "local": local,
        "local_passed": local_passed,
        "target": view.target,
        "best": best,
        "convergence": convergence,
        "pareto": front,
        "rel_noise": noise,
        "next": decision,
    })
}

fn simulate(plan: Plan, inst: Instrument, opts: &SimOptions) -> Value {
    let mut sim = Q2Sim::new(&plan.map, &inst, opts);
    let mut journal = new_journal(plan, inst, opts.start);
    let mut outcome = ("limit".to_owned(), "simulation line limit".to_owned());
    for _ in 0..opts.max_lines.max(1) {
        let decision = Engine::new(&journal).decide();
        match decision {
            Decision::Line(r) => {
                let request = numbered(&journal, r);
                let result = sim.run(&request);
                if record(&mut journal, &result).is_err() {
                    outcome = ("error".to_owned(), "simulated result rejected".to_owned());
                    break;
                }
            }
            Decision::Wait(r) => {
                let request = numbered(&journal, r);
                let result = LineResult {
                    id: request.id,
                    outcome: DONE.to_owned(),
                    started: sim.clock,
                    duration_s: request.seconds,
                    ..LineResult::default()
                };
                sim.clock += request.seconds;
                if record(&mut journal, &result).is_err() {
                    outcome = ("error".to_owned(), "simulated wait rejected".to_owned());
                    break;
                }
            }
            Decision::Done(reason) => {
                outcome = ("done".to_owned(), reason);
                break;
            }
            Decision::Stop(reason) => {
                outcome = ("stop".to_owned(), reason);
                break;
            }
        }
    }
    let lines: Vec<Value> = journal
        .records
        .iter()
        .map(|r| {
            json!({"id": r.request.id, "stage": r.request.stage, "role": r.request.role,
                "point": r.request.point, "variant": r.request.variant, "kind": r.request.kind,
                "f_khz": r.request.f_hz / 1e3, "energy_ev": r.request.energy_ev, "k": r.request.k,
                "delta_v": r.request.delta_v, "outcome": r.outcome, "duration_s": r.duration_s,
                "expected_s": r.request.expected_s})
        })
        .collect();
    let mut out = json!({
        "outcome": outcome.0,
        "reason": outcome.1,
        "lines": lines,
        "total_s": sim.clock - opts.start,
        "report": report(&journal),
    });
    if opts.journal {
        out["journal"] = serde_json::to_value(&journal).unwrap_or(Value::Null);
    }
    out
}

// ---------------------------------------------------------------- RPC

fn arg<'a>(args: &'a [Value], kwargs: &'a Value, index: usize, name: &str) -> Result<&'a Value> {
    args.get(index)
        .or_else(|| kwargs.get(name))
        .ok_or_else(|| Error::argument(format!("Missing {name}")))
}

/// The map methods of the MScan controller; None for any other method.
pub fn call(method: &str, args: &[Value], kwargs: &Value) -> Option<Result<Value>> {
    let run = || -> Result<Value> {
        match method {
            "map_check" => {
                let plan = parse_plan(arg(args, kwargs, 0, "plan")?)?;
                let inst = parse_instrument(arg(args, kwargs, 1, "instrument")?)?;
                let (problems, warnings, summary) = check(&plan, &inst);
                let estimate = if problems.is_empty() {
                    let opts = SimOptions {
                        hypothesis: "H1".to_owned(),
                        ..SimOptions::default()
                    };
                    let s = simulate(plan.clone(), inst, &opts);
                    let lines = s["lines"].as_array().cloned().unwrap_or_default();
                    // The simulated lines, each timed by the line model (2026-11-04: it matched the
                    // instrument; the simulator's clean peaks need about 30 % fewer points).
                    let total: f64 = lines
                        .iter()
                        .map(|l| {
                            let took = l["duration_s"].as_f64().unwrap_or(0.0);
                            if l["outcome"] == json!(DONE) {
                                took.max(l["expected_s"].as_f64().unwrap_or(0.0))
                            } else {
                                took
                            }
                        })
                        .sum();
                    json!({"lines": lines.len(), "total_s": total, "simulated_s": s["total_s"],
                        "outcome": s["outcome"], "reason": s["reason"]})
                } else {
                    Value::Null
                };
                Ok(
                    json!({"problems": problems, "warnings": warnings, "summary": summary,
                    "estimate": estimate, "plan": plan}),
                )
            }
            "map_begin" => {
                let plan = parse_plan(arg(args, kwargs, 0, "plan")?)?;
                let inst = parse_instrument(arg(args, kwargs, 1, "instrument")?)?;
                let started = number_value(arg(args, kwargs, 2, "started")?);
                let (problems, _, _) = check(&plan, &inst);
                if !problems.is_empty() {
                    return Err(Error::argument(format!(
                        "The map cannot start: {}",
                        problems.join(" ")
                    )));
                }
                let journal =
                    new_journal(plan, inst, if started.is_finite() { started } else { 0.0 });
                serde_json::to_value(&journal).map_err(|e| Error::runtime(e.to_string()))
            }
            "map_next" => Ok(next(&parse_journal(arg(args, kwargs, 0, "journal")?)?)),
            "map_record" => {
                let mut journal = parse_journal(arg(args, kwargs, 0, "journal")?)?;
                let result: LineResult =
                    serde_json::from_value(arg(args, kwargs, 1, "result")?.clone())
                        .map_err(|e| Error::argument(format!("Invalid line result: {e}")))?;
                let analysis = record(&mut journal, &result)?;
                Ok(json!({"journal": journal, "analysis": analysis}))
            }
            "map_report" => Ok(report(&parse_journal(arg(args, kwargs, 0, "journal")?)?)),
            "map_simulate" => {
                let plan = parse_plan(arg(args, kwargs, 0, "plan")?)?;
                let inst = parse_instrument(arg(args, kwargs, 1, "instrument")?)?;
                let opts: SimOptions = match args.get(2).or_else(|| kwargs.get("options")) {
                    Some(v) if !v.is_null() => serde_json::from_value(v.clone())
                        .map_err(|e| Error::argument(format!("Invalid simulation options: {e}")))?,
                    _ => SimOptions::default(),
                };
                Ok(simulate(plan, inst, &opts))
            }
            "map_grid" => {
                let lo = number_value(arg(args, kwargs, 0, "lo")?);
                let hi = number_value(arg(args, kwargs, 1, "hi")?);
                let r_max = number_value(arg(args, kwargs, 2, "r_max")?);
                Ok(Value::Array(
                    amplitude_grid(lo, hi, r_max, 4)
                        .into_iter()
                        .map(float)
                        .collect(),
                ))
            }
            _ => Err(Error::unsupported(format!(
                "MScan method not supported: {method}"
            ))),
        }
    };
    method.starts_with("map_").then(run)
}
