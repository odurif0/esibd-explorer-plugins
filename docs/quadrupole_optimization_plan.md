# Q2 mass filter: the map, its laws and the best scan

Draft of 2026-11-03, revision 2, after v0.5.9. Covers Q2 only: the beam stops at Q2/Q3 for now
(detector M03); Q3/Q4 come later. Physics background: `docs/mscan_quadrupole.pdf`.

Revision 2 (user, 2026-11-03): the engine is adaptive, so the limits come from the measurement and not
from bounds fixed in advance. The map is measured through the few laws that shape it, and the best scan
is computed from them and then verified. Run time is not a constraint: the ESI runs from a syringe with
an unlimited supply, which is more stable than the glass emitter.

## 1. Goal

Measure the **map** of Q2, find its best settings, **demonstrate** that they are the best within the
measurement noise, and record the **best scan** there: a full, calibrated mass spectrum of the
tuning mix, repeated to show that it is reproducible.

**Strategy: measure the laws, not the map point by point.** A blind search in five dimensions spends
most of its lines confirming what physics already says. Here:
- k and δ are computed from a ladder in k at each operating point, not searched;
- f and E are expected to act on the resolution through a single number, the RF cycles N, which is tested;
- the entry stub follows x by a fixed rule and is checked at every operating point.

The search is then over (f, E) only. The engine computes the best scan from the measured laws, then
measures it. Every step is adaptive: a direction where the measurement degrades is past its optimum
and is not pushed further.

## 2. The map

**Coordinates.**

| Axis | Parameter | Hard bounds (fixed before Start) |
|---|---|---|
| f | RF frequency (AMX HD) | 100 kHz – 1 MHz |
| A | RF amplitude (PSU rails, "PSU" in our notes) | 20–348 V on every amplitude line (Full range) |
| k | U/A ratio (α in older notes) | none needed: nothing passes above the closing point |
| δ | resolution offset: U = max(Umin, k·A − δ) | −2 to 2 V (negative values to confirm, Section 11) |
| x | Q2 axis potential (U0); sets the ion energy E | E from 3 to 30 eV |
| stub | Stub_Entry_Q2, follows x (Section 3.5) | never above Q1_DC − 1 V |

Inside the hard bounds, the measurement finds every limit.

**E, the ion energy in Q2.** The ions are thermalized in Q1 (about 1e-1 mbar), almost at rest at the
potential of Q1. They then fall to the axis potential x of Q2, so their kinetic energy in Q2 is
**E = e·(Q1_DC − x)** for the singly charged ions of the mix: 26 eV on 2026-11-03 (Q1_DC 36 V, x 10 V).
The stopping cut (stage A) measures the real stopping potential x_stop and the energy spread.
From then on, E is counted from x_stop rather than from Q1_DC: x = x_stop − E/e. The run sets x, but
the plan is written in E, because the physics depends on E (N ∝ 1/√E, transmission), and E stays
meaningful if Q1_DC changes.

**Value.** At each point: the current of every DMMR module, with the ESI, the pressures and the time
(all recorded since v0.5.9).

**Scans are lines through the map.** An amplitude line runs along A at fixed (f, k, δ, x). A frequency
line runs along f at fixed A and U. A stopping curve runs along x, and a stub cut along the stub. Every
line adds its points to one map.

**Every amplitude line runs from 20 to 348 V**: the whole A axis, hence every ion of the mix that
falls in range. 20 V: below, one 0.01 V AMPR step is more than 0.0005 in k. 348 V: 2 V under the PSU
Full range maximum, so Full range is required (Half range stops near 245 V).

## 3. Why a few laws describe the map

### 3.1 A is the mass axis
For an ion of mass m, q ∝ c·A/(m·f²): one line along A measures every ion of the mix at once.

**What a 20–348 V line covers** (m/z ∝ A/f²):

| f | m/z covered | Tuning-mix ions in the line |
|---|---|---|
| 100 kHz | 5000 – 87000 | none |
| 200 kHz | 1270 – 22000 | 1522, 2122, 2722 |
| 300 kHz | 560 – 9700 | 622 to 2722 |
| 500 kHz | 200 – 3480 | 322 to 2722 |
| 560 kHz | 160 – 2780 | 322 to 2722 (2722 near 340 V) |
| 900 kHz | 60 – 1070 | 118, 322, 622, 922 |
| 1 MHz | 50 – 870 | 118, 322, 622 |

A line needs at least two mix ions, so f must be above about 150 kHz. An amplitude scan holding the
whole mix from 322 to 2722 needs f between about 300 and 570 kHz.

### 3.2 k and δ: computed, not searched
k and δ act on an ion only through its effective ratio at its peak,
k_eff,i = (k·A_i − δ) / (c·A_i), where A_i is the amplitude of its peak. A **ladder in k** at
δ = 0 (Section 5, stage B) measures, for every ion of the line, its resolution R_i and its signal
S_i as functions of k_eff. With these, the engine computes the pair (k*, δ*) that gives every ion of the
best-scan range R_i ≥ R_target with the most signal. This is a two-parameter search on a grid, with no
numerical subtlety. One line at (k*, δ*) then verifies the computation.

**The ladder also separates the hypotheses (Section 7).** In the ideal filter,
R ≈ 0.157 / (0.2153 − k_eff), so **1/R is linear in k**: 1/R_i = (0.2153 − k/c)/0.157. Its slope gives c.
A limit that adds its own width adds a constant 1/R_cap,i, which shows as the value of 1/R_i at the
closing. Each ladder thus measures c and R_cap per ion. The fit residuals check whether widths really
add.

### 3.3 f and E: one number for the resolution, the RF cycles N
An ion spends N = f·L·√(m / 2eE) RF cycles in Q2 (L ≈ 15 cm). Few cycles cap the resolution near
N²/(12–20). If that is what limits R (H4), f and E act on the resolution only through N, and every ion
at every operating point falls on one curve R_cap(N). Estimates for 2122:

| f | E | N | R ceiling |
|---|---|---|---|
| 100 kHz | 26 eV | 10 | 5–8 |
| 100 kHz | 3 eV | 29 | 41–69 |
| 500 kHz | 26 eV | 49 | 120–198 |
| 1 MHz | 26 eV | 98 | 480–790 |

Each line already spans a range of N, because N ∝ √m: at 500 kHz and 26 eV, from 19 cycles for 322
to 55 for 2722. The operating points of stage B add pairs with the **same N but different (f, E)**;
these test directly whether N alone matters.

Transmission (the signal S) depends on E (injection, fringe field, losses at low E), f
(pseudo-potential depth), the stub and collisions (pressure P4). It is measured, not modelled in
advance.

### 3.4 The scan mode follows from N
- **Amplitude scan** (fixed f): N ∝ √m, so the light ions limit. At 500 kHz and 26 eV, 322 gets
  19 cycles (ceiling 18–30) and 622 gets 26 (35–58): if H4 holds, R = 40 is out of reach at 322 there.
  To reach it, use f near the top of the mix window (about 570 kHz) and E below about 15 eV, or a
  negative δ, which raises k_eff at low A.
- **Frequency scan** (fixed A and U): every ion sees the same (a, q) and the same N ∝ √A, hence the
  same resolution. At A = 120 V the line runs from 2722 at about 335 kHz to 322 at about 980 kHz,
  N ≈ 37 for every ion at 26 eV (ceiling 68–114) and ≈ 67 at 8 eV.
- **Frequency step.** One 10 ns tick is f × 10 ns of the frequency, twice that in m/z: 0.7 % at
  335 kHz but 2 % at 1 MHz, against a peak width of 2.5 % at R = 40. The frequency scan is thus a
  strong candidate for the best scan only if the AMX HD gives finer steps than 10 ns at the high-f
  end (Section 11).

### 3.5 The entry stub
The stub must follow x. With a fixed offset (x + 10 V, as on 2026-11-03), it rises above Q1_DC as
soon as E falls below 10 eV, and then it is the stub, not Q2, that stops the ions. The rule is to keep
it between the aperture and the axis in the same proportion as on 2026-11-03:

  **Stub_Entry_Q2 = x + s·(Ap_Q1_Q2 − x)**, s = 0.42 ((20 − 10)/(34 − 10)).

It then never stops the ions: it stays below Q1_DC for every E of the map and below x when x > Ap_Q1_Q2
(stopping cut). Its effect is mainly on transmission, so it is checked by a short stub cut at every
operating point (stage B). If its optimum moves with f or E, it becomes a third search axis; otherwise
the rule stays. Stub_Exit_Q2 stays at x − 15 V, always below x.

### 3.6 Amplitude step: driven by the measurement, not by a predicted resolution
The map exists to measure R, so the step must not assume it (the ideal theory was off by a factor of
6 at k = 0.21). The only assumption is declared and conservative: **R_max, the highest resolution we
want to be able to see** (a specification, not a prediction; default 100). The noise of each point is
taken from the DMMR readings behind it, kept since v0.5.9.

- **Coarse step** where the signal is flat: A / (2·R_max), so that even a peak at R_max has two
  points and cannot fall between them.
- **Fine step** where the signal changes. The step is halved while the change between neighbours
  exceeds both 3σ and 10 % of the local peak height. It goes down to A / (10·R_max) (or to the PSU/AMPR
  resolution if larger) and grows back as the changes shrink. This gives about ten points per edge
  whatever the peak width, without knowing it in advance.
- **Edges kept**: when a change is detected, the scan goes back one coarse step and continues at the
  fine step, at once and in the same direction, so that spray drift does not split a peak between two
  passes.

Frequency lines follow the same rule in whole 10 ns ticks. A 20–348 V line with R_max = 100 takes about
570 coarse points plus about 30 per peak: 600–770 points, 8–10 min at 0.8 s per point. A linear step
fine enough for R = 100 would need about 16000 points.

### 3.7 RF current
It grows as f·A. From 2026-11-03 (about 150 mA at 250 V, 500 kHz), at 348 V: about 40 mA at
100 kHz, 125 mA at 300 kHz, 210 mA at 500 kHz, 375 mA at 900 kHz and 420 mA at 1 MHz. Ilim and the
PSU range limit (Full range 0.5 A?) bound the high-f end, and so does the AMX HD switch dissipation
(∝ f·A²), to be checked against its specification.

This current is not a setting: it follows from f and A. The operator sets only its limit, Ilim, by
hand on the PSU; MScan reads Ilim and never changes it. Measured on 2026-11-04: about
1.47 nA/(Hz·V), so 307 mA at 600 kHz and 348 V. The lab runs at Ilim 300 mA, so the plan keeps
f within 250–640 kHz and needs Ilim ≥ 330 mA (2026-11-12). With P9 (900 kHz) it needed 490 mA.

### 3.8 Starting values (2026-11-03)
- c = 0.978 ± 0.005 and r0 = 2.625 ± 0.003 mm (Agilent G2421A mix: 622, 922, 1522 and 2122 on M03 at
  400–500 kHz).
- E ≈ 26 eV (Q1_DC 36 V, x 10 V).
- R ≈ 12–16 at k = 0.20 and about 30–55 at k = 0.21 (1 V steps: undersampled). At k = 0.21 the ideal
  theory gives R ≈ 270, so something else limits R today (Section 7).

## 4. Figure of merit

The merit is computed on the mix ions of the **best-scan range** (default 622 to 2122 since 2026-11-04:
322 sat unresolved on the low-A background and 2722 never rose above the noise), each with its
local baseline subtracted and its signal divided by the latest reference (drift):

- S_i: peak height above the local baseline (the current transmitted at the peak, comparable between
  amplitude and frequency lines, unlike an area in V or Hz); R_i: m/Δm at half height (Gaussian fit
  of the points above 30 % of the height).
- **Best = largest Σ S_i such that R_i ≥ R_target for every ion of the range.** R_target = 40
  (decided 2026-11-03).
- The laws also give the whole **Pareto front** (largest signal for each resolution), so the best
  settings for another R_target or another mass range come from the same data, without a new run.

## 5. Strategy

### A. Stopping cut (about 15 min)
At 500 kHz, k = 0.20 and δ = 0, A fixed on the peak of 922 (89.8 V), then on 2122 (206.7 V). x runs
from 25 to 45 V by 0.25 V. Then the same with Q1_DC + 2 V (38 V), and Q1_DC is restored afterwards.
Each curve is measured twice. The stub follows the rule of Section 3.5, so the Q2 axis, not the stub,
is what stops the ions.
- The 50 % point gives x_stop, the origin of E for the rest of the run.
- The width gives the energy spread.
- The shift with Q1_DC (expected 2 V) checks that the ions are thermalized.

### B. Ladders at the design points (the laws, about 20 h)
**Instrument check first.** The first ladder is at P1, today's point. If it does not reproduce
2026-11-03 (peaks at the same A within tolerance, R at k = 0.20 within 12–16), the run does a safe stop:
something on the instrument has changed. The other points follow in random order (plan seed).

| Point | f | E | Mix ions in the line | N per ion (322 → 2722) | Role |
|---|---|---|---|---|---|
| P1 | 500 kHz | 26 eV | 322 to 2722 | 19 → 55 | today; instrument check |
| P2 | 500 kHz | 12 eV | 322 to 2722 | 28 → 81 | E at fixed f |
| P3 | 500 kHz | 5 eV | 322 to 2722 | 43 → 126 | low E |
| P4 | 300 kHz | 26 eV | 622 to 2722 | 16 → 33 | f at fixed E |
| P5 | 300 kHz | 9.4 eV | 622 to 2722 | 26 → 55 | **same N as P1**: does N alone matter? |
| P6 | 300 kHz | 4.3 eV | 622 to 2722 | 39 → 82 | **same N as P2**: second test |
| P7 | 550 kHz | 26 eV | 322 to 2722 | 21 → 61 | top of the mix window |
| P8 | 550 kHz | 8 eV | 322 to 2722 | 38 → 110 | top of the window, low E |
| F1 | frequency lines, A = 110 V | 26 eV | 322 to 2722 | 35 for every ion | frequency mode |
| F2 | frequency lines, A = 110 V | 8 eV | 322 to 2722 | 64 for every ion | frequency mode, low E |

P9 (900 kHz, 26 eV) and P10 (200 kHz, 26 eV) were dropped on 2026-11-12. P9 needed Ilim 490 mA (the
lab runs at 300 mA), and neither point holds the best range 622–2122 inside 20–348 V. P4 now gives the
low N (16 for 622). H3 is still tested at equal N with different masses, but at a different E: 922 in P2
(N 47) against 2122 in P1 (49), 922 in P3 (73) against 2122 in P2 (72), 622 in P8 (52) against 2122 in
P1 (49). Only P9 compared them at equal N and equal E.

**The ladder at each point** (lines at δ = 0, adaptive):
1. First line at k = 0.200, a well-known setting (R ≈ 14).
2. Then the ladder approaches the expected closing k_c = 0.2153·c, with c from the previous ladders
   (0.978 at first). The lines sit at distances 0.005, 0.0025, 0.0012, 0.0006 and 0.0003 below it, and
   the estimate is updated after each line from the 1/R fit.
3. Past the estimate, the step doubles until the signal disappears. The last gap is then bisected
   down to 0.00025.
4. **Stop** when the closing is confirmed (no signal on any ion), or **when the measurement stops
   improving**: R gains nothing beyond its noise over two lines in a row while S falls. Going further
   would only lose signal (user's rule).
5. Two lines are measured again at the end of the ladder: the one nearest R_target and the last one
   with signal. This gives reproducibility and drift over the ladder.
6. (k*, δ*) is computed for this point (Section 3.2), then measured once: the **verification line**.
7. **Stub cut** at (k*, δ*): A fixed on the lightest and the heaviest ion of the range present in the
   line, stub from x − 10 V to Q1_DC − 2 V by 1 V (two short lines).

In all, about 8 ladder lines, 2 repeats, 1 verification and 2 short stub lines per point, about 1.7 h.
A point where no (k, δ) reaches R_target still counts: it gives the laws.

In frequency mode (F1, F2) the ladder runs over the same k (U = k·A at fixed A), each line being a
frequency line.

### C. The laws (fitted after each ladder)
1. **Closing and cap per ion**: c from the slope of 1/R_i in k, and R_cap,i from its value at the
   closing (Section 3.2). Is c the same for every ion and every f, i.e. is the attenuation independent
   of f?
2. **R_cap versus N**: do all ions of all points fall on one curve (P5 against P1, P6 against P2)? If
   yes, the curve's exponent tests H4 (2 expected); ions of different mass at equal N (P2 against P1,
   P3 against P2, P8 against P1) test H3.
3. **Signal at R_target per ion**, as a smooth function of (log f, log E), with the stub at its rule
   or at its optimum if the cuts show one.
4. **Calibration**: (c, r0) fitted on every line, proposed as new MScan defaults.

### D. Outer search: adaptive, on (f, E)
- From the laws, predict the merit (with (k*, δ*) at each (f, E)) and its optimum within the hard
  bounds.
- If the predicted optimum lies outside the measured points, or on an edge that no measurement has
  closed, measure a ladder there. The step is limited to one design spacing. Then refit and repeat.
- **Stop** when the predicted optimum has been measured and the prediction holds within its noise on
  two iterations in a row.
- A direction is closed when the measurement there degrades, confirmed on two lines (user's rule), or
  when it hits a hardware limit (PSU current, AMX, AMPR).
- **Fallback**: if the laws do not hold (no common curve in N, residuals above the noise), the same
  loop runs on a local model, a quadratic fit on the nearest measured points. This is an adaptive
  search in (f, E); k and δ are still computed by the ladders. Expected: 3–6 ladders.

### E. Out-of-sample check (about 1.5 h)
Ten points in the reachable (f, E) domain, drawn by a Sobol sequence (plan seed) and **not used in any
fit**. At each one, the engine predicts the merit at (k*, δ*) before measuring a line there. The
pass mark is at least 8 of 10 within two standard deviations of the prediction. This is the strongest
proof that the computed best scan really is the best.

### F. Local check around the optimum (about 3 h)
Each setting one step up and one step down: f ±10 %, E ±25 %, stub ±2 V, k ±0.0005, δ ±0.25 V. Each
of these 10 neighbours is measured twice, in random order. The merit must be lower in every direction,
or equal within noise (then that setting is not critical there).

### G. The best scan (about 1.2 h)
1. The best amplitude line and, if the frequency step allows R_max, the best frequency line, both
   with the measurement-driven step.
2. The better of the two by the merit is repeated 5 times over at least 4 hours, with references
   between. Merit, R and peak positions must agree within their spread.
3. It is saved as the result of the map: a file tagged `best`, its settings as an MScan settings
   file, the merit table, the laws and the figures.

**Figures:**
- 1/R versus k per ion and point;
- R_cap versus N (common curve or not);
- signal versus E and f;
- predicted versus measured merit (fitted points and out-of-sample points);
- merit map in (f, E), convergence of the outer search, Pareto front;
- local check;
- best scan with its calibration.

## 6. Budget (indicative: time is not a constraint)

| Stage | Lines | Time |
|---|---|---|
| A. Stopping cut | 8 short curves | 15 min |
| B. Ladders at 10 points | about 130, plus 24 short stub lines | about 20 h |
| D. Outer search | 3–6 ladders | 5–10 h |
| E. Out-of-sample check | 10 | 1.5 h |
| F. Local check | 20 | 3 h |
| G. Best scan | 2 + 5 | 1.2 h |
| References (1–2 min window, every 5 lines) | about 40 | 1 h |
| **Total** | **about 200** | **about 32–37 h** |

Ways to shorten it, if ever needed:
- no repeats in the ladders (−3.5 h);
- drop P6 (−1.7 h);
- 0.5 s per point (−35 %).

The run can also be split at any line boundary and continued with Resume (Section 9).

## 7. What the laws will settle

| | Hypothesis on today's limit | Seen in the ladders as |
|---|---|---|
| H1 | c = 0.978; something mass-independent caps R (U/V noise, waveform) | slope of 1/R gives c ≈ 0.978; R_cap ≈ 45 for every ion, whatever N |
| H2 | c ≈ 0.99: k = 0.21 was further from the apex | slope gives c ≈ 0.99, closing near k = 0.213, R_cap large |
| H3 | collisions in Q2 | at equal N, R_cap and S fall with mass (922 in P2 against 2122 in P1, 622 in P8 against 2122 in P1) and with P4 (about 1.5 collisions per pass for 2122 at 1e-4 mbar, 0.15 at 1e-5) |
| H4 | too few RF cycles | R_cap of every ion and point on one curve, rising as N² (P5 ≡ P1, P6 ≡ P2) |

## 8. Quality rules

- Spray from the syringe, current within ±40 % of its value during the last reference line that found
  its peak (at Start before the first one; plan setting; the 30–60 nA of the glass emitter no longer
  applies; 2026-11-04: 42 → 48 → 36 nA while the M03 peaks held within 2 %), otherwise the line is repeated. TPG366 acquiring; a note in each
  file.
- **Reference**: a 1–2 min window around 922 (500 kHz, k = 0.20, δ = 0, E = 26 eV, A from 80 to 100 V)
  every 5 lines; the signals are divided by it.
- P1 first as the instrument check; the other design points in random order.
- A local baseline under each peak, from the flat parts on both sides (the M03 background depends on A).
- PSU checks before each line: current predicted as I ∝ f·A from the present reading, against Ilim,
  the range limit and the headroom. A line the PSU cannot reach is not forced: its direction is closed
  as a hardware limit. MScan never switches the PSU range.
- The frequency changes through the AMX plugin, keeping the 50 % duty cycle; the waveform is checked
  again after each change.

## 9. Unattended operation: a requirement

A run of many hours is accepted (user, 2026-11-03) on one condition: **fully automatic, without crash or
bug.** Nothing below depends on the length of the run. The operator acts only before Start; after it,
nothing waits for a human.

**Before Start (the only manual part):**
- PSU_A in Full range;
- Ilim set by hand on the PSU above the highest RF current the hard bounds allow (330 mA for
  `q2_map`; MScan reads Ilim and never changes it: it stays the operator's safeguard);
- ESI spraying steadily from the syringe;
- TPG366 acquiring, DMMR recording.

The lines are chosen during the run, so the validation checks the **worst case of the hard bounds**:
Ilim for the highest f at 348 V (307 mA at 600 kHz), AMPR limits for x, the stubs and Q1_DC + 2 V, and
disk space for the planned maximum. It refuses to start with a list of what to change, for example
"set Ilim to at least 330 mA on the PSU yourself (now 300 mA; MScan never changes Ilim), or limit f to
557 kHz in the plan", instead of discovering it at 3 a.m.

**Everything else is automatic**: frequency changes through the AMX plugin, x and the stubs, k, δ, the
laws, the outer search, the checks, the best scan.

**Faults, sorted by what the run does with them:**

| Fault | Run |
|---|---|
| Lost reply, port resynchronized (PSU, AMX, ESI, AMPR, DMMR) | continues (recovery already in the workers) |
| DMMR records missing or with a repeated time stamp | continues (skipped, counted) |
| Spray current outside its window | pauses, waits for it to come back, repeats the line; stops after 30 min outside |
| No new point for 5 min, or a line longer than every point of its axis would take | aborts the line, repeats it once (a line slower than its estimate is not a fault, 2026-11-04) |
| A worker dies or hangs | reconnects it (the plugins already can), repeats the line; stops after three failures in one hour |
| PSU without headroom, out of tolerance, Ilim reached | the line stops at once (v0.5.9 guard); that direction is closed as a hardware limit and the run goes on |
| P1 does not reproduce 2026-11-03 | **safe stop** (instrument changed) |
| Anything else, or a fault repeated | **safe stop** |

**Safe stop.** The AMX frequency, Q1_DC, the stubs and MScan's settings are restored to their values
at Start. The RF amplitude and the rod pairs stay as the last line left them: restored at the end of a
completed line, held after a line fault, as for any MScan scan (no write after a fault). The PSU range,
Ilim and the ESI are never touched. The cause, the line and the time go
to the log and to a status file. Reaping a worker never confirms that an output is off (same rule as the
plugins).

**Nothing is lost, and the run resumes.** Each finished line is written at once (its scan file and
the map store), together with a journal of the run: lines done, x_stop, c, the fitted laws, closed
directions and the random seed. After a safe stop, an Explorer crash or a power cut, **Resume**
continues from the last finished line with the same plan. A line cut in the middle is measured again.

**Long-run hygiene:**
- the PC is kept awake for the whole run (as the pressure–temperature notebook does), with sleep also
  disabled in the power plan;
- free disk space is checked at Start;
- memory stays bounded (only recent DMMR history is read since v0.5.9, bounded buffers, the plot shows
  the current line only);
- the GUI stays responsive (no work proportional to the run length at each poll).

**How "without crash or bug" is shown before the first real run:**
1. **Unit tests** of every part, in the Rust suite and the Python tests as for every release:
   - plan validation;
   - ladder steps and stop rules;
   - (k, δ) solver and law fits;
   - outer search and its fallback;
   - journal and Resume;
   - safe stop.
2. **Full simulated run** of the whole plan against the simulated devices of the test backend, in
   accelerated time.
   - A **Q2 simulator** produces the peaks from the laws of Section 3 and can be switched between H1,
     H2 and H4, so that every branch runs, the fallback included.
   - Faults are injected: lost replies, a killed worker, DMMR gaps, spray drop-outs, PSU without
     headroom, an Explorer restart and Resume.
   - The run must end with a best scan, or in a safe stop where one is expected, and never otherwise.
3. **Soak test**: the simulated run repeated in real time, for at least the longest run planned (about
   35 h here), on a Windows PC. Memory, handles and GUI response must stay flat.
4. **On the instrument**, in three steps:
   - a 1 h plan (stopping cut and part of the P1 ladder);
   - a 4 h plan (P1 and P5 ladders);
   - the full map.

## 10. Software: v0.5.10

Constraints: one Rust worker per plugin; hardware is driven only through Explorer and MScan's
checks.

- **Plan file** (TOML), copied into every scan file of the map. Draft:

  ```toml
  [map]
  name = "q2-map-2026-11"
  mix = [118.09, 322.05, 622.03, 922.01, 1521.97, 2121.93, 2721.89]   # Agilent G2421A
  best_scan_range = [622, 2122]  # the merit and R_target apply to the mix ions in this range
  r_target = 40
  r_max = 100                    # highest R the step must resolve (a specification, not a prediction)
  a_range_v = [20, 348]          # every amplitude line stays inside
  a_margin = 0.2                 # and spans its best-range ions ± 20 % of their amplitude
  min_ions_in_line = 2
  reference_every = 5
  spray_window_percent = 40      # around the ESI current at Start
  point_s = 0.8                  # measurement, settling and command/readback per point (s);
  settle_s = 0.3                 # the map then times its own lines (2026-11-04: 2.02 s per point
  point_overhead_s = 0.9         # and 15.5 s per line)
  seed = 1

  [map.bounds]                   # hard limits, checked before Start; the measurement finds the rest
  f_khz = [250, 640]             # Ilim ≥ 330 mA, set on the PSU by the operator
  energy_ev = [3, 30]            # x = x_stop - E
  delta_v = [-2, 2]
  stub_below_q1_v = 1

  [map.design]                   # first operating points; the outer search adds more
  amplitude = [[500, 26], [500, 12], [500, 5], [300, 26], [300, 9.4], [300, 4.3],
               [550, 26], [550, 8]]                         # (f kHz, E eV)
  frequency = [[110, 26], [110, 8]]                          # (A V, E eV)

  [map.ladder]
  first_k = 0.200
  distances = [0.005, 0.0025, 0.0012, 0.0006, 0.0003]   # below the expected closing
  finest_k = 0.00025
  repeats = 2

  [map.stub]                     # Stub_Entry_Q2 = x + s * (Ap_Q1_Q2 - x)
  s = 0.42
  cut_from_x_v = -10             # stub cut from x - 10 V ...
  cut_to_q1_v = -2               # ... to Q1_DC - 2 V
  cut_step_v = 1

  [map.follow]                   # other channels as offsets from x, restored at the end
  Stub_Exit_Q2 = -15.0
  ```
- **Map store**: every point of every line in one HDF5 file per map (coordinates, all modules, ESI,
  pressures, time), linked to the individual scan files.
- **Rust worker (MScan)**, small and deterministic, with no Gaussian process:
  - plan validation (worst case of the hard bounds);
  - ladder engine (steps, bracketing, stop rules);
  - per-ion peak analysis;
  - law fits (1/R in k, R_cap in N, signal in f and E);
  - (k, δ) solver;
  - outer search and its fallback;
  - Sobol out-of-sample points and local check;
  - merit, Pareto front, best scan.
- **Python adapter**: applies each line's settings (frequency through the AMX plugin; x, stubs and
  Q1_DC through AMPR; k, δ), runs a normal scan, saves it with the map name and line number, handles
  faults as in Section 9 and restores the settings at the end.
- **MScan**: δ allowed below 0, if confirmed (today its minimum is 0).
- **Scan axes**: amplitude A (today), axis potential x (stopping curves: transmission, derivative and
  fit), stub (stub cuts) and frequency f (fixed A and U). Every line uses the measurement-driven step.
- **Run journal and Resume**, keep-awake for the whole run, disk and memory checks.
- **Q2 simulator, simulated full run with fault injection and real-time soak test**, as release tests.
- **Figures** as listed in Section 5 G.

**Built in v0.5.10 (2026-11-04).** The map engine (`native/worker/src/mscan_map.rs`), the line axes
and the measurement-driven step of the MScan worker, the Map group of MScan and its runner
(`mscan/_runtime/_map_runner.py`), the plan templates (`mscan/plans/`). Differences from the text
above: the merit uses peak heights; the solver aims at R_target·(1 + r_noise) and a line reaches
R_target from R_target·(1 − r_noise) (r_noise = 0.10, the scatter of R on one peak); when no setting
reaches R_target on every ion, the target becomes 90 % of the highest R reached and the map still ends
with a best scan; a stopping or stub cut holds the stubs at fixed values (they never stop the ions). In the Q2 simulator the
full map takes about 40 h when R follows the RF cycles (H4) and about 65 h when R is capped near 45
(H1): the target is then lowered and the search runs longer. The soak test is `tools/soak_mscan_map.py`.
See `mscan/README.md`, section Map of Q2.

## 11. Decided and open

**Decided (2026-11-03):**
- The axes are f, A ("PSU"), k, δ and x ("U0"). E = e·(Q1_DC − x) is the ion energy in Q2, counted
  from the measured stopping potential.
- Every amplitude line runs from 20 to 348 V. R_target = 40. f runs from 100 kHz to 1 MHz.
- The engine is adaptive: the limits come from the measurement, and a direction that degrades is not
  pushed further.
- The map is measured through its laws: ladders in k, k and δ computed, search in (f, E).
- The entry stub follows x proportionally and is checked at every point.
- Time is not a constraint; the ESI runs from a syringe with an unlimited supply.
- The run is fully automatic, without crash or bug (Section 9).
- Everything is in v0.5.10.

**Open:**
1. AMX HD limits at high f·A (switch dissipation, maximum frequency) and its finest frequency step.
   The step decides whether the frequency mode can compete at high f: one 10 ns tick is 2 % in m/z at
   1 MHz.
2. PSU current in Full range (0.5 A?) and the Ilim to allow (about 420 mA for 1 MHz at 348 V).
3. Typical P4 with beam (decides H3).
4. Negative δ: useful in amplitude mode to raise the resolution of the light ions; MScan accepts only
   δ ≥ 0 today.
5. Best-scan range: 622 to 2122 by default since 2026-11-04 (322 unresolved, 2722 not in the beam);
   put 322 back once a low-E point resolves it.

## 12. Integration and launch

**A mode of MScan, not a new plugin.** Every line of the map is an ordinary MScan scan, with all of its
checks: PSU headroom and Ilim, verified U1,2 writes, DMMR, ESI and TPG366 recording, and file naming. A
separate plugin would have to drive MScan through Explorer and duplicate these checks.

**The engine has no state of its own: the journal is its state.** The MScan Rust worker computes each
next line from the plan and the journal of finished lines. A restarted worker, a crash or a power cut
lose nothing, and Resume is the same call as a normal step.

**Launching a map (operator):**
1. Prepare the instrument as for a normal scan:
   - AMX HD ON with its RF configuration (50 % duty);
   - PSU_A in Full range with its Ilim;
   - AMPR ON;
   - DMMR recording, TPG366 acquiring, ESI spraying from the syringe.
2. In MScan, Map group, click **Map…** and choose a new map with a plan (or resume a stopped map).
   Three templates are provided:
   - `q2_P1_2h.toml`: stopping cut and the P1 ladder up to the R_target region (about 2 h);
   - `q2_P1_P5_6h.toml`: P1 and P5 ladders (about 6 h);
   - `q2_full.toml`: the full map.
3. The plan is checked first: the worst case of the hard bounds, what to change, and the lines and time
   the Q2 simulator decides. The map starts only if nothing must change.
4. From then on nothing waits for a human. The MScan settings are locked, and a manual change of a
   driven channel causes a safe stop. The button becomes **Stop map**.
5. Follow the progress in the panel: stage, line, point (f, E, k), laws and remaining time. A status file
   and the log in `<Data path>/logs/mscan/` can be read remotely.
6. Files:
   - each line is a normal MScan file in `…/MScan/<date>/`;
   - the map has its own folder, `…/MScan/maps/<name>/`, holding the plan, the journal, `map.h5`, the
     figures and the best scan's settings file.
7. **Stop** causes a safe stop. **Resume** continues later with the same plan, also after a crash.

**Building it.** It is developed in this order, so that each part is tested before the next relies on it:
1. the map engine in Rust, with its Q2 simulator;
2. the new line axes (x, stub, frequency) and the measurement-driven step in the MScan line engine;
3. the map mode in MScan, with the journal, Resume, safe stop and fault handling;
4. the simulated run with fault injection, the plan templates and the documentation.
