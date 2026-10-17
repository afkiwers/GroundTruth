# Validation audit: Signal paths, Converters, DC loads

Round 1 on branch `validation-audit` (merged f171f99, 2026-10-17); round 2 on `audit-2` (2026-10-18). This file is
the audit's record: the system overview, the validation matrix with every row's outcome, the issue log with
evidence, and the final assessment. The independent checks are `tests/test_validation_audit.py` (52 test functions,
92 cases); each matrix row names its test or the existing test it rests on. Status words: **Verified** (an independent method agrees within the stated tolerance),
**Verified (conditions)** (only under the conditions named), **Covered** (an existing analytic test, reviewed, not
re-derived here), **Limitation** (known model error, quantified, not fixed), **Unverified** (no independent
evidence available), **Fixed** (a defect, corrected, with a regression test that fails on the old code).

## Executive summary

- 10 defects found, all fixed, each with a regression test that fails on the code before its fix and passes now.
  Round 1: ISSUE-1 to -8 (ISSUE-7 then documented); round 2: ISSUE-7 fixed, ISSUE-9 and -10 found and fixed, the
  items left unverified in round 1 checked (the boost loop against a switching simulation, the MIL-STD-461G limits
  against the standard, the Tier 1 rules on constructed geometry, the spread receiver against an independent one),
  and kraft_flux_mini_2 simulated with the old and the new code ("Round 2" below).
- The most consequential:
  - **ISSUE-1**: a boost's losses (switches, inductor, diode), and through them its input current, its thermal
    heat and its current capacity, were wrong at any duty but ½. Example: 5 V → 20 V at 1 A put 0.2 W into the
    switch instead of 0.6 W and 0.018 W into the inductor instead of 0.16 W.
  - **ISSUE-5**: every signal path's line impedance came from closed forms used outside their range and fed with
    the wrong heights. On kraft_flux_mini_2, Z0 read +21 % (outer layers) and +52 % (inner layers) against the
    field-solved as-built cut, so the line current at high harmonics was underestimated. It is now within +3-4 %
    (outer) and ±8 % (inner).
- Everything else examined agrees with independent methods: the path circuit (to 1e-4 against a 600-cell ladder),
  spectra, field formula, limits, LISNs, ringing (RK4), node charge, ripple, capacitor sharing, loop gain and its
  margins, DC-load drops (reciprocity, Tellegen, series parts, pins), via capacity and the energy balance at 60
  random operating points.
- Confidence, after the fixes: **Signal paths** good for the circuit, spectra and emission arithmetic; moderate for
  absolute emission levels (the quasi-static loop model's documented limits). **Converters** good for the hot
  loop, ringing, conducted chain, spread receiver, loop gain (both topologies against a switching simulation),
  losses and DC paths (the switch node now RMS), buck and boost alike. **DC loads** high: every result checked
  against circuit identities that must hold exactly.

## Phase 1: the system and the baseline

### Architecture of the three areas

**Signal paths** (`config.nets`; `analysis/route.py`, `rules/checks.py`, `analysis/simulate.py`, `emi/path.py`,
`emi/spectrum.py`, `emi/radiation.py`, `solver/peec.py`, `analysis/transfer.py`).
1. `route.find_route`: the copper path driver → receiver (tracks, vias, pads, passing series parts, pours).
2. Tier 1 (`rules/checks.py`): gap/split crossing, off-plane, layer change without stitching via / decap, edge
   proximity, non-ground reference.
3. Tier 2 (`simulate.prepare_route` → `solve_setups` → `finish_route`): the fine route model and the shared
   board-wide model (plane capacitance); per solve L, R, the loop moment m per ampere.
4. Line current (`emi/path.path_currents`): the driver's trapezoid harmonics (`spectrum.trapezoid_harmonics`)
   through a Thevenin source, a lossless line per stretch (`simulate._line_of` → `spectrum.microstrip` /
   `offset_stripline`), series parts, shunts (loops of their own), op-amp stages, the receivers' load; the mean
   current along the path drives the loop. `current_a` overrides it.
5. Emission (`radiation.emission_arrays`): the dipole field of m·I, the receiver bandwidth, limits, margins;
   issues `return_detour`, `plane_resonance`, `band_unsolved`, `emi_margin`; Bode responses.

**Converters** (`config.converters`; `analysis/power.py`, `emi/conducted.py`, `emi/spread.py`,
`analysis/thermal.part_losses`, `analysis/loop.py`, `analysis/fix_advisor.py`).
1. `ConverterSpec.pulse()`: the hot loop's pulse (amplitude; duty = the high side's conduction time).
2. The hot-loop model: L (series-LC fit at 30 / 100 MHz), R, capacitor shares, maps.
3. Ringing, node-charge factor, overshoot; radiated estimate; conducted chain (input transfer × LISN × port ×
   receiver × limit); boost ripple; common-mode budget; spread spectrum; CE101.
4. DC paths on the whole-board DC model (switch node, input, output, ground return by Kirchhoff); the input
   current from the energy balance (`input_estimate` + `part_losses`); stages combined.
5. Control loop (buck / boost, peak current / voltage mode, gm / op-amp compensator); fix advisor.

**DC loads** (`config.dc_loads`; `analysis/dcload.py` on `power.dc_board_build`).
1. Settings validated by `dc_load_problems` (source, loads > 0 A, returns all or none).
2. One resistive solve of the whole-board copper, injections per A of the total spread over each pad's nodes.
3. `finish_dc_load`: R = Σ V·I_inj (Tellegen), loss, densities, vias against IPC-2221, series parts' shares,
   per-load supply / return drops (+ pin joint / contact I·R), issues `current_density`, `via_overload`, `dc_drop`.

Shared code: the PEEC solver and `RouteExcitation` (all three); `_area_shares` / `dc_summary` / `dc_board_build`
(converter DC paths and DC loads); `thermal.part_losses` (converter input current, thermal, DC loads' series parts,
pins); `radiation` (signal paths and converters); the component models (all three).

### Baseline (before any change)

- **Tests:** the 17 in-scope test files (test_path, test_emission, test_power, test_dcload, test_loop,
  test_transfer, test_spread, test_ce101, test_checks, test_solver, test_multilayer, test_pours, test_fix_advisor,
  test_pins, test_components, test_fields, test_detect): **246 passed, 0 failed, 312 s**
  (`out/audit_validation/baseline_pytest.log`).
- **Baseline copy:** the original `src/` and `tests/` were copied before any edit (the repository had no commit
  of them then; fae19c9, committed during the audit, holds the same sources). Every regression test was then run
  against that copy as well (Phase 6).
- **Environment:** the venv's editable install points at the project's old folder (`...\Dokumente\PCB-EMI\src`).
  pytest's own `pythonpath = src` (pyproject) is used, so the tests run against this tree.

### Independent methods used
- Sampled-waveform FFTs: spectra, ripple, the node-charge current.
- A lumped π-ladder (600 cells per line) solved by nodal analysis: the signal-path circuit.
- RK4 time-domain integration: ringing and overshoot.
- The project's 2D field solver (`solver/xsection.py`, validated separately against Cohn's exact results to 0.2 %)
  and Cohn's elliptic-integral stripline: the line closed forms. On the real board, its cut of the as-built copper
  (`analysis/si.SectionCache`, solder mask included).
- Literature closed forms typed in independently: the dipole field, the LISN networks, Ridley's buck loop gain,
  IPC-2221, the standards' limit tables.
- Circuit identities that must hold exactly: reciprocity, Tellegen, superposition, Kirchhoff, the energy balance.
- Property-based runs: random passive paths, random operating points.

## Phase 2 and 3-5: validation matrix and outcomes

### Signal paths

| ID | Result / calculation | Independent method | Acceptance | Outcome |
|---|---|---|---|---|
| SP-1 | Trapezoid harmonics (equal / own fall time, duty) | sampled FFT | 0.2 % | Verified (test_SP1, 4 cases) |
| SP-2 | Line Z0 / εeff closed forms | Cohn exact; 2D field solver, t > 0 | 1 % / 2 % | **Fixed** (ISSUE-5), Verified |
| SP-14 | The line of a stretch from the stackup (outer, inner offset) | field solver on the same geometry | 3 % outer, 7 % inner | **Fixed** (ISSUE-5), Verified (conditions: nearer plane ≥ 0.15 of the spacing) |
| SP-3 | Path circuit: mean current, I_in, V_end (lines, series R / L / C / ferrite, shunts, source L / C, termination) | nodal π-ladder | 2 % (found ≤ 1e-4) | Verified (test_SP3, 4 paths) |
| SP-3b | Shunt loop currents | superposition identity | exact | Verified |
| SP-4 | Op-amp stages: gain, pole, z_out, slew cap | composed path solves | 1e-9 | Verified |
| P-1 | 40 random passive paths: passivity, ladder agreement | Re(V·I*) ≥ 0; ladder | 1 % | Verified |
| SP-5 | Loop field with the induction term, ground reflection | magnetic dipole formula | 0.1 % | Verified |
| SP-6 | CISPR 32 radiated limits, distances, band edges | CISPR 32 tables | exact | Verified |
| SP-7 | Receiver bandwidth sum | brute-force window sums | exact | Verified |
| SP-8 | Emission dBµV/m, margin, distance scaling | hand calculation | 0.02 dB | Verified |
| SP-9 | End to end: every emission row = field(m·I); the path current = ladder of the reported stretches | as SP-3/SP-5 | 0.01 dB / 1 % | Verified |
| P-2 | Linearity in the driver's swing | −6.02 dB at half swing | 1e-6 dB | Verified |
| SP-12 | `current_a` = the fundamental's peak | definition (README) | exact | **Fixed** (ISSUE-6), Verified |
| SP-13 | Impossible signals refused | crash / nonsense before | – | **Fixed** (ISSUE-8), Verified (12 cases) |
| SP-10/11 | Tier 1 layer-change rules at their 2 mm / 5 mm thresholds | geometry | flag / no flag | Verified |
| SP-15 | Loop L and \|m\| over a plane (Wheeler, image theory), slots, layer changes, full cut | analytic | 3-6 % | Covered (test_solver, test_multilayer) |
| SP-16 | Plane-pair capacitance, cavity resonance f10 | ε0εrA/t; c/(2a√εr) | exact / 3 % | Covered (test_capacitance) |
| SP-17 | Shunt loops on a board (50 Ω shunt vs load_ohms), op-amp board path | equivalent circuits | 0.1-0.4 dB | Covered (test_path) |
| SP-18 | Signal-path Bode: RC pole, op-amp pole | 1/(2π(R_drv+R)C), GBW/gain | 1 % | Covered (test_transfer) |
| SP-19 | Board-wide resonance heights above 1 GHz | grid convergence | – | Limitation (documented in ARCHITECTURE: several dB between grids; a note says so) |
| SP-20 | Route finding through series parts, `route_missing` | – | – | Covered (test_path, test_checks) |
| SP-21 | Tier 1 gap_crossing: a slot's length, below min_gap_mm none | constructed geometry | 0.01 mm | Verified (round 2, 3 cases) |
| SP-22 | Tier 1 split between two nets named; reference_net over the other net | constructed geometry | 0.05 mm | Verified (round 2) |
| SP-23 | Tier 1 edge_proximity within edge_factor·h, its distance | constructed geometry | 0.01 mm | Verified (round 2; h is the foils' centre spacing: the limit ~17 % wider than with the dielectric, conservative) |
| SP-24 | Tier 1 off_plane for the uncovered length | constructed geometry | 0.05 mm | Verified (round 2) |
| SP-25 | RE102 limit curves (MIL-STD-461G Figs. RE102-1 to -4) | the standard's figures | exact | Verified (round 2, test_CV26) |

### Converters

| ID | Result / calculation | Independent method | Acceptance | Outcome |
|---|---|---|---|---|
| CV-1 | Pulse amplitude and duty, buck / boost | ideal CCM | exact | Verified |
| CV-15 | Boost known by its duty only: inductor current, ground split | Kirchhoff | 1 % | **Fixed** (ISSUE-3), Verified |
| CV-2 | Hot-loop L from two reactances | synthetic X(ω) | 1e-9 | Verified |
| CV-3 | Ringing f_r, Q, overshoot (edges 0.05-0.7 periods) | RK4 of the series RLC after a ramp | 3 % | Verified |
| CV-4 | Node-charge factor, bare node and RC snubber | FFT of the time-domain sum | 2 % | Verified |
| CV-5 | Boost ripple harmonics | FFT of the asymmetric triangle | 0.1 % | Verified |
| CV-6 | LISN networks (CISPR 16-1-2, CISPR 25, MIL-STD-461G Fig. 7) | circuit formulas | exact | Verified (MIL-461: input unterminated, as its calibration specifies) |
| CV-7 | Conducted limits (CISPR 32 A/B av., CISPR 25 classes 1-5, CE102 + relaxations) | the standards' tables | exact | Verified |
| CV-8 | Conducted reading I·Z_LISN·port share, RMS | hand calculation | 1e-6 dB | Verified |
| CV-9 | Input-filter advice | f_c rule, L·C, R = √(L/C) | exact | Verified |
| CV-10 | Common-mode budget | linear in C: 0 / −6.02 dB | 0.05 dB | Verified |
| CV-11 | Part losses, buck / boost, synchronous / diode, D ≠ ½ | independent loss budget | 1e-6 | **Fixed** (ISSUE-1), Verified |
| CV-13 | A boost's input current (energy balance) | V_in·I_in = P_out + losses | 1e-6 | **Fixed** (ISSUE-1), Verified |
| P-4 | 60 random operating points: losses ≥ 0, balance, efficiency ≤ 1 | as CV-11 | 1e-9 | Verified |
| CV-12 | Capacitor shares at f_sw | the capacitors' own divider | 2 % / 5 % | Verified |
| CV-14 | Loop gain with ESR and DCR; fc, PM, GM readout | Ridley written out; root search | 1e-9; 0.1 % / 0.2° / 0.05 dB | Verified |
| CV-16 | Fix advisor input side, Middlebrook \|Z_in\| | V_in²/P_in | exact | **Fixed** (ISSUE-2), Verified |
| CV-17 | Voltages contradicting the topology refused | – | – | **Fixed** (ISSUE-8), Verified |
| CV-18 | Hot-loop L per length vs a wide microstrip | Hammerstad | 10 % (−3.6 %) | Covered (test_power) |
| CV-19 | LISN current divider below the capacitor's SRF | Z_C/(Z_C + 2Z_LISN) | 3 % | Covered (test_power) |
| CV-20 | DC paths of a buck: output R by squares, ground split by Kirchhoff | analytic | 10 % / 1 % | Covered (test_power) |
| CV-21 | Boost ground split with the energy balance | Kirchhoff | 1 % | Covered (test_power) |
| CV-22 | Stages' DC currents combined | superposition, 2× | exact | Covered (test_power) |
| CV-23 | Loop gain: buck Ridley (no ESR), voltage-mode LC pair, boost RHP zero and LC pair at D' | written-out models | 1e-9 | Covered (test_loop) |
| CV-24 | Peak-current-mode loop plant, buck and boost (the boost: the code's own derivation) | cycle-by-cycle switching simulation, cycle means | 0.3 dB / 1.5° to f_sw/10 | Verified (round 2: boost 0.06 dB / 0.3°, buck 0.21 dB / 0.9°) |
| CV-25 | Spread spectrum receiver (sinusoidal FM, slow / fast sweeps, random hops) | Bessel, chirp gain, Rayleigh | – | Covered (test_spread) |
| CV-26 | CE101 limit curves (Figs. CE101-1, -4), Table II bandwidths, CE102, the LISN (Fig. 6, 4.3.6) | MIL-STD-461G itself (everyspec) | exact | Verified (round 2); the LISN's 1 kΩ: **Fixed** (ISSUE-9) |
| CV-27/28 | Switch-node copper loss, densities, heat | the two conduction states solved apart | 0.2 % | **Fixed** (ISSUE-7, round 2), Verified |
| CV-29 | Fix advisor on a boost: no-change outcome = the stage's figures; Middlebrook at the input | full Simulate | 1e-9 | Verified (round 2) |
| CV-30 | Spread receiver, triangle sweeps narrow and wide | an independent receiver (direct convolution, dense tuning) | 0.1 dB | **Fixed** (ISSUE-10), Verified (round 2, 4 cases) |

### DC loads

| ID | Result / calculation | Independent method | Acceptance | Outcome |
|---|---|---|---|---|
| DC-1 | Drops: reciprocity of the transfer resistances; the pour's R per length; loss = Σ I·drop; r = loss/I² | Tellegen, reciprocity, ΔL/(σwt) | 1e-3 / 7 % / 1e-4 | Verified |
| DC-2 | With returns: loss over both nets, drop = supply + return | Tellegen | 1e-4 | Verified |
| DC-3 | A series part on the rail: I·ΔR in the drop, I²R its loss | analytic | 1 % | Verified |
| DC-4 | A pin named by two loads | = one load with both currents | 1e-6 | **Fixed** (ISSUE-4), Verified |
| DC-5 | Via capacity | IPC-2221 internal conductor | exact | Verified (barrel π(d+t)t for a finished hole d) |
| DC-6 | A load off the rail's copper | error, no crash | – | Verified |
| DC-7 | Pin joint + contact: I_total·R at the source pin, I_k·R at each load pin; copper unchanged | analytic | 1e-6 | Verified |
| P-3 | Linearity (2× currents: 2× drops, 4× loss), order independence, repeatability | identities | 1e-6 / exact | Verified |
| DC-8 | Drop along a pour = I·ΔL/(σwt) (1x: −5.5 %, 4x: +0.1 %); returns positive; thermal √ law | analytic | – | Covered (test_dcload) |
| DC-9 | Validation: currents > 0, returns all or none, max_drop > 0 | – | – | Covered (`dc_load_problems`) |

### Cross-system (Phase 7)
- Shared `dc_summary` / `_area_shares`: exercised by DC-1-DC-7 (DC loads) and CV-20-CV-22 (converter paths); both
  obey the same identities.
- `part_losses` feeds the converter's input current, the DC input path's current and scale (`_rescale_path`), the
  thermal solve and the current capacity: ISSUE-1's fix reaches all of them (CV-13, P-4).
- The line closed forms feed the path current, the reported Z0 and the Bode responses (ISSUE-5).
- Adversarial inputs: 12 impossible net signals and 3 contradictory converter voltage pairs (SP-13, CV-17).
  Every settings file in the repository and `out/` (14) still loads, and so do the real kraft_flux_mini_2
  settings (read-only: 14 nets, 4 converters). None of detection's 13 signal classes breaks the new edge rule.

## Issue log

| ID | Area / pathway | Severity | Status |
|---|---|---|---|
| ISSUE-1 | Converter (boost): `part_losses` duty convention → losses, input current, thermal, capacity | High | Fixed, verified |
| ISSUE-5 | Signal paths: line Z0 / εeff (closed forms out of range, wrong heights, centred inner layers) | High | Fixed, verified |
| ISSUE-8 | Signal paths / converters: impossible inputs crash Simulate or give nonsense | Medium | Fixed, verified |
| ISSUE-4 | DC loads: a pin named twice fails the rail with a misleading message | Medium | Fixed, verified |
| ISSUE-2 | Converter (boost): fix advisor's Middlebrook \|Z_in\|, filter current and voltage | Medium | Fixed, verified |
| ISSUE-6 | Signal paths: `current_a` read 2/π of the documented fundamental (−3.9 dB) | Medium | Fixed, verified |
| ISSUE-7 | Converter: switch-node DC path averaged, not RMS | Medium-Low | Fixed (round 2), verified |
| ISSUE-9 | Converter (MIL-461): the LISN's 1 kΩ across its port left out (0.4 dB high) | Low | Fixed (round 2), verified |
| ISSUE-10 | Converter: the spread receiver's tuning search missed mid-band peaks (0.5-0.7 dB optimistic) | Medium | Fixed (round 2), verified |
| ISSUE-3 | Converter (boost) without V_out: pulse amplitude I_out, ground split 1 − d | Low | Fixed, verified |

### ISSUE-1: a boost's losses took the high side's conduction time for the switch duty
- **Pathway:** `ConverterSpec.pulse()` gives the duty as the high side's conduction time, V_in/V_out for a
  boost. That becomes `ConverterResult.duty`, which feeds `thermal.part_losses`. There `i_l = i_out/(1−d)`,
  `d_hs, d_ls = 1−d, d`, the diode `(1−d)·i_l·V_f` and the connectors' `i_in = i_out/(1−d)` all assumed d was the
  switch's duty.
- **Reaches:** `power.input_estimate` (the stage's input current, the DC input path's current), the thermal solve,
  current capacity.
- **Reproduce:** test_CV11[boost-False-5.0-20.0]: Q1 0.2 W (expected 0.6 W), L1 0.018 W (expected 0.16 W:
  I_L = 4 A, not 1.33 A); test_CV11[boost-True-12.0-36.0]: Q1 0.27 W (expected 0.54 W); test_CV13: the input
  current. The old test used D = ½, where both readings coincide.
- **Root cause:** two duty conventions in one codebase; `part_losses` kept the switch-duty one.
- **Fix:** `thermal.part_losses` treats d as the high side's conduction time in both topologies: I_L = I_out/d for
  a boost, the high side conducts d and the low side 1 − d, a boost diode conducts d, the connectors carry I_out/d.
- **Evidence:** CV-11 (5 cases), CV-13 (2), P-4 (60 random points) pass; on the baseline code CV-11 boost, CV-13
  fail. Buck results unchanged (CV-11 buck cases pass on both).

### ISSUE-5: line impedance of the signal path
- **Pathway:** `simulate._line_of` → `spectrum.stripline` (IPC-2141, centred) or `spectrum.microstrip`
  (single-correction Hammerstad) → `PathLine` → the line current, the reported Z0, the Bode responses.
- **Defects, against the 2D field solver:**
  - (a) IPC-2141 is valid for w/b < 0.35 only: −4 to −6 % at w/b 0.5, −12 % at 0.8, 4.7 Ω for 18 Ω at w/b 2,
    negative beyond about 2.5.
  - (b) The heights were the foils' centre-to-centre distance, not the dielectric: +35 µm on a 0.1 mm core.
  - (c) An inner layer was taken as centred between its neighbours: +30 to +40 % for a strip at a fifth of the
    spacing.
  - (d) The microstrip's εeff ignored the thickness: up to +26 %.
- **On kraft_flux_mini_2** (read-only, against the as-built cut with solder mask): outer layers old +21-22 %, new
  +3-4 % (the rest is mostly the solder mask, which the closed form ignores); inner layers old +51-53 %, new
  −7.5 to +8 %.
- **Fix:**
  - `spectrum.microstrip` is now Hammerstad & Jensen 1980 with thickness: within 0.8 % / 1.8 % for w/h ≥ 0.3.
  - `spectrum.stripline` is now Wheeler 1978: within 0.5 % for w/b 0.1-4.
  - New `spectrum.offset_stripline`: the combination of the two symmetric halves, within 1.5 % for an offset of
    0.35 or more, 5.5 % at 0.2, 10 % at 0.1, reading high.
  - `_line_of` passes `Stackup.dielectric_between` heights.
- **Evidence:** SP-2, SP-14 (4 geometries) pass; on the baseline code all fail.
- **Remaining:**
  - very asymmetric inner layers (nearer plane < 0.15 of the spacing) still read up to ~10 % high;
  - the solder mask is ignored (Z0 a few per cent high on outer layers);
  - a neighbour that is not a plane is taken as one.
  The SI module's field-solved cut (`SectionCache`) would remove all three, at ~0.1-0.4 s per stretch.

### ISSUE-8: impossible inputs
- **Reproduce** (`scratchpad/adversarial.py`, now SP-13 / CV-17):
  - a net with freq 0 or rise_time 0: `ZeroDivisionError` in Simulate;
  - duty 0: `IndexError`;
  - duty 1.2: a −1.7 dB margin;
  - amplitude −3.3 V: +301 dB;
  - a negative frequency: no result and no error;
  - a buck with V_out ≥ V_in, or a boost with V_out ≤ V_in: solved at a made-up duty of ½, with a buck drawing more
    input current than it delivers.
- **Fix:** `settings.net_problems` requires:
  - f, t_r, t_f > 0, and edges within the high / low time;
  - 0 < D < 1, amplitude > 0, current_a > 0;
  - no negative source or load values;
  - load_ohms > 0.

  The converter checks also refuse voltages that contradict the topology.

  Problems refuse the settings with messages, as the existing converter and DC-load checks do.

### ISSUE-4: a pin named twice in a DC rail
- **Reproduce:** test_DC4: loads (U2.1, 1 A) and (U2.1, 2 A). The rail failed with "the injected currents do not
  balance … a series part without a model, or a pin on another net?". `dcload._injections` keyed its feeds by
  pin, so the second replaced the first.
- **Fix:** a pin's feeds add node by node (as `pin_currents` already counted them). `_potential` takes the plain
  mean when a pin's feeds cancel (a load on the source pin itself).
- **Evidence:** DC-4 now equals one 3 A load to 1e-6.

### ISSUE-2: the fix advisor's input filter for a boost
- **Wrong figures:**
  - `middlebrook` and `filter_responses` used `res.v_in`, the switched V_out for a boost:
    |Z_in| = V_out²/P_in, (V_out/V_in)² too high (×9 for 12 → 36 V), so the stability check read too optimistic.
  - The filter's DCR loss and its current rating used pulse·duty = I_out instead of I_L.
  - Its capacitor's voltage text used V_out.
- **Fix:** `fix_advisor.input_side(spec, res)` gives (V_in, I_in), used at all four places.
- **Evidence:** CV-16.

### ISSUE-6: `current_a`
- **Documented (README):** "peak A at the fundamental".
- **Was:** the harmonics scaled by current_a/amplitude_v, so the fundamental carried c_1/A·current_a: 2/π of it
  for a fast 50 % square wave, 3.9 dB low.
- **Fix:** the spectrum is scaled so that harmonic 1 carries current_a.
- **Evidence:** SP-12.

### ISSUE-3: a boost known by its duty alone
- **Was:** without V_out, `pulse()` returned I_out as the inductor current; the ground split without an energy
  balance used 1 − d.
- **Fix:** I_L = I_out/d; the load's share of the input current is d.
- **Evidence:** CV-15.

### ISSUE-7: switch-node DC path (fixed in round 2)
- **What happened:** the path fed the averaged current: D from the high side's pads, 1 − D from the low side's.
  The copper actually carries the full current from each side in turn.
- **Consequence:** the copper loss and heat read D·(1−D)·I²·R_hl low, R_hl being the resistance between the
  switches' pads along the node. On the test buck that is 5.9 % (D = 0.1), 12.5 % (0.25) and 17 % (0.5) of the
  path's loss; near the high side's pads the heat read lower still. The densities and via currents for the checks
  were means, not RMS.
- **Fix (exact, and linear where it must be):** the time-averaged square of a two-state current is
  |J_mean|² + D(1−D)·|J_hs − J_ls|².
  - One more DC solve per stage (`ConverterSetup.dc_ripple`): 1 A in at the high side's pads, out at the low
    side's, which is J_hs − J_ls.
  - `dc_summary(ripple=)`: RMS densities, via and part currents; R and the loss gain D(1−D)·R_d.
  - The mean (`dc_vectors`) still superposes linearly in `combine_dc`; the variance is kept as `dc_ripple_sq`
    (heat, as a bridge's RMS density) and `dc_ripple_branch` (vias, in quadrature); the stages' switching is taken
    as independent.
  - Thermal adds the variance heat, scaled ∝ s² in the capacity search; Sharper map solves it too; split
    Simulates carry it (`compact_dc_branch`).
- **Evidence:** CV-28 (D = 0.1, 0.5): the loss equals D·P_hs + (1−D)·P_ls of the two states solved apart (0.2 %),
  and the density maps equal their RMS. `test_the_stages_dc_currents_add_up_on_one_board_model` (test_power)
  updated: the combined map is √(|2·J_mean|² + 2·variance), not 2·|J_mean|.

### ISSUE-9: the MIL-STD-461 LISN's port (fixed in round 2)
- MIL-STD-461G Figure 6 has 1 kΩ from the signal port to the case, in parallel with the receiver's 50 Ω (47.6 Ω).
  The code had the 50 Ω alone, so CE102 readings came out 20·log(50/47.6) = 0.42 dB high (conservative); the CE101
  lead model had the same port.
- **Fix:** `Lisn.R_p` (the port's own resistor), 1 kΩ on `mil461` and `CE101_LEAD`. **Evidence:** CV-6.
- Confirmed from the standard while there: the LISN impedance is specified with the power input unterminated
  (4.3.6 c), as modelled; Table II's bandwidths; CE102 Figure CE102-1 and its relaxations; CE101 Figures CE101-1 and
  -4; RE102 Figures -1 to -4. The 15 ms dwell is Table II's (the code cited Table III; comment corrected).

### ISSUE-10: the spread receiver's tuning search (fixed in round 2)
- **Reproduce:** a 500 kHz-wide triangle sweep at 10 kHz (harmonic 5 of 500 kHz, 20 %), RBW 9 kHz.
  `receiver_gain` read −14.01 dB; an independent receiver (direct convolution, tunings every RBW/32) reads
  −13.48 dB. A 400 kHz sweep at 30 kHz: −8.98 dB against −8.25 dB.
- **Root cause:** with the modulation rate near the RBW, the swept harmonic is a comb of lines one rate apart whose
  strongest lies anywhere in the band (here 110 kHz in). The search placed 25 coarse tunings across the band,
  dense ones only at the ends, then refined the 3 best; it missed the peak by 0.5-0.7 dB, optimistically.
- **Fix:** the whole band is scanned at RBW/2 (at most 2000 tunings, as random hopping already was), plus the
  dense ends; the 5 best are refined.
- **Evidence:** CV-30, 4 cases within 0.03 dB.

### Smaller observations (no change)
- The via plating density uses π·d·t, the IPC capacity π·(d+t)·t: ~8 % apart for 0.3 mm vias.
- All receivers' input capacitance sits at the route's end (`load_pf × receivers`).
- The inner-layer εr is the thickness-weighted mean of the dielectrics between the neighbours.
- The Tier 1 edge rule's h is the foils' centre-to-centre height: a limit ~17 % wider than with the dielectric
  (on a 0.2 mm core), so it flags slightly more (conservative).

## Phase 6-8: corrections and verification

Changed code (a diff against the baseline copy shows only these):

| File | Change |
|---|---|
| `analysis/thermal.py` | `part_losses`: the duty convention (ISSUE-1) |
| `analysis/fix_advisor.py` | `input_side`; Middlebrook, filter responses, filter loss and ratings (ISSUE-2) |
| `config/settings.py` | `ConverterSpec.pulse` (ISSUE-3); `net_problems`; converter voltage check (ISSUE-8) |
| `analysis/power.py` | `_output_runs`: a boost's load share d (ISSUE-3) |
| `analysis/dcload.py` | feeds add up per pin; `_potential` fallback (ISSUE-4) |
| `analysis/simulate.py` | `current_a` (ISSUE-6); `_line_of`: dielectric heights, offset stripline (ISSUE-5) |
| `emi/spectrum.py` | H&J 1980 microstrip, Wheeler stripline, `offset_stripline` (ISSUE-5) |
| `docs/ARCHITECTURE.md`, `ui/help.html` | the line model, the duty convention, ISSUE-7 |
| round 2: `analysis/power.py`, `analysis/thermal.py`, `analysis/sharper.py`, `analysis/simulate.py` | ISSUE-7: the switch node's variance run, `dc_summary(ripple=)`, heat, `combine_dc`, portability |
| round 2: `emi/conducted.py`, `emi/spread.py` | ISSUE-9 (`Lisn.R_p`), ISSUE-10 (the tuning search), the Table II citation |
| round 2: `tests/test_power.py` | the combined-view expectation (mean superposed, variance in quadrature) |

Regression evidence:
- Against the baseline code: the 8 regression cases of ISSUE-1/2/3/4/6 failed (the 3 buck and D = ½ cases
  passed, correctly), and so did the 20 cases of ISSUE-5/8.
- On the fixed code, `tests/test_validation_audit.py`: **75 passed** (95 s).
- Quality gates: `tools/dev/py311_check.py` 0 files rejected; `tools/dev/undocumented.py` 0 functions without a
  docstring; ruff finds no new issue in the changed source files.

Full suite on the final code: see "Final run" below.

## Round 2 (2026-10-18, branch `audit-2`)

The user: "continue with fixing the other issues as well a continue your audit", and fix the venv.

- **Venv:** the whole venv had been moved from `...\Dokumente\PCB-EMI\.venv`. Its editable install pointed at
  `PCB-EMI\src`, and every launcher (`pytest.exe`, `groundtruth.exe`, `pip.exe`) had the old interpreter built in,
  so re-linking the package alone would not have fixed it. It was rebuilt in place as `.venv` with the old one's
  exact versions as constraints (`out/audit_validation/venv_constraints.txt`): the editable install now points at
  this folder. Left out: the ad-hoc dev tools the old venv held (py-spy, pyflakes, vulture, pyopencl, pypdf);
  install them again if needed. `src/pcbemi.egg-info` (the project's old name, gitignored) is a harmless leftover
  that pip lists.
- **Fixed:** ISSUE-7 (switch node RMS), ISSUE-9 (MIL-461 LISN port), ISSUE-10 (spread tuning search).
- **Verified, round 1's open items:**
  - CV-24: the loop plant against a switching simulation. A first comparison sampled v_out at the switching
    instants and showed 4-15° of phase difference on the boost; that came from the measurement, not the code
    (the boost's pulsed output ripple), and vanished with cycle means.
  - CV-26 / SP-25: CE101, RE102, CE102, Table II and the LISN against MIL-STD-461G itself.
  - SP-21 to -24: the Tier 1 rules on constructed geometry.
  - CV-29: the fix advisor on a boost.
  - CV-30: the spread receiver.
- **Covered, reviewed:** plane-pair capacitance and cavity resonance (test_capacitance), shunt and op-amp loops on
  boards (test_path), Bode responses (test_transfer), victims' plate coupling and termination divider
  (test_stack, test_stage_victims), the fix advisor's other options (test_fix_advisor: every change against a full
  Simulate with it).
- **kraft_flux_mini_2, old code (fae19c9) against new (audit-2), every net and converter** (they are switched off
  in the user's file), same settings, the board only read:

  | | Old | New |
  |---|---|---|
  | Line Z0, outer / inner | 52.7 / 54.5 Ω | 44.8 / 38.6 Ω |
  | Worst margin SPI_SCK / SPI_MISO / SPI_MOSI | +15.3 / +18.8 / +18.8 dB | +13.9 / +18.1 / +17.8 dB |
  | Other nets' worst margins | – | within −0.7 to +0.6 dB of old |
  | conv1 switch-node copper loss, sustained density | 19.5 mW, 28.5 A/mm² | 27.7 mW, 35.9 A/mm² |
  | conv2 switch-node copper loss | 17.6 mW | 20.3 mW |
  | Hot-loop L, radiated and conducted margins, other DC paths | – | unchanged |

  The four stages are bucks with CISPR 32 and no spread, so ISSUE-1, -9 and -10 do not show there. The emission
  margins move little because these short lines are capacitive loads at most harmonics; long or terminated lines
  depend on Z0 more.

## Remaining risks and unverified items
- SP-19: board-wide resonance heights above 1 GHz depend on the grid by several dB (documented; a note says so).
- ISSUE-5's remainder: very asymmetric inner layers up to ~10 % high, and no solder mask in the closed forms (the
  SI module's field-solved cut would remove both).
- ISSUE-7's assumption: two stages' switch-node variances add as independent (unlocked phases). Two phases of one
  controller (interleaved) are locked: their cross term is not modelled.
- The emission estimate's model limits are unchanged: quasi-static, differential mode only, no cables; it ranks
  layouts, it is not a compliance prediction.
- Published kraft_flux_mini_2 numbers in HANDOFF and README predate the fixes: the SPI margins about 1 dB lower now,
  conv1's switch-node copper about 42 % more.

## Final run
- **Round 2, full suite** on the final code with the rebuilt venv (`out/audit_validation/full_round2.log`, 37 min):
  **852 passed, 1 skipped, 3 failed**, none from the audit:
  - `test_server_setup::test_the_mac_launcher_is_lf_and_executable`: the launcher's git mode (below);
  - `test_gui::test_a_new_board_gets_its_power_stages_detected` and
    `…::test_the_bridges_tab_detects_a_motor_bridge_simulates_it_and_shows_its_drive`: the window's busy-gap timing
    (0.70 s and 0.61 s against 0.5 s) on the loaded machine. Re-run alone: both pass.
- Round 2's regression tests on round 1's code (b479e10): CV-6, CV-28 (both duties) and the two wide CV-30 sweeps
  fail, as they should; the verification-only checks (CV-24, CV-26, CV-29, SP-21 to -24) pass on both.
- **Round 1, full suite** on its final code (`out/audit_validation/full_final.log`, 36 min): **835 passed, 1 skipped,
  3 failed**. None of the three failures is caused by the audit's changes:
  - `test_server_setup::test_the_mac_launcher_is_lf_and_executable`: `tools/install_server.command` is in the
    repository's new first commit (fae19c9, made during the audit) with mode 100644. The test wants 100755 once
    it is committed. Cure: `git update-index --chmod=+x tools/install_server.command`.
  - `test_split::test_a_simulate_sent_to_a_member_is_split_by_the_lead_and_equals_one_run` and
    `…_a_lost_member_s_part_runs_on_the_lead_and_cancel_stops_every_part`: the cluster splits a Simulate by each
    server's free memory, and the full run's log shows the machine at 0.5 GB free. Re-run alone, all 10 test_split
    tests pass on the final code (89 s), as both did on the baseline code.
- **Tested code = final code:** nothing under `src/` or `tests/` changed after the full run started. The audit's
  changes are exactly `git diff` against fae19c9 (8 source and doc files, 195 lines added, 43 removed) plus the
  two new files. fae19c9's sources match the baseline copy the audit started from.
