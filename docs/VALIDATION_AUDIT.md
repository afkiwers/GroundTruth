# Validation audit: Signal paths, Converters, DC loads

Round 1 on branch `validation-audit` (merged f171f99, 2026-10-17); round 2 on `audit-2` (2026-10-18, merged c5c313e);
round 3 on `audit-3` (2026-10-18). This file is
the audit's record: the system overview, the validation matrix with every row's outcome, the issue log with
evidence, and the final assessment. The independent checks are `tests/test_validation_audit.py` (56 test functions,
100 cases); each matrix row names its test or the existing test it rests on. Status words: **Verified** (an independent method agrees within the stated tolerance),
**Verified (conditions)** (only under the conditions named), **Covered** (an existing analytic test, reviewed, not
re-derived here), **Limitation** (known model error, quantified, not fixed), **Unverified** (no independent
evidence available), **Fixed** (a defect, corrected, with a regression test that fails on the old code).

## Executive summary

- 10 defects found, all fixed, each with a regression test that fails on the code before its fix and passes now.
  Round 1: ISSUE-1 to -8 (ISSUE-7 then documented); round 2: ISSUE-7 fixed, ISSUE-9 and -10 found and fixed, the
  items left unverified in round 1 checked (the boost loop against a switching simulation, the MIL-STD-461G limits
  against the standard, the Tier 1 rules on constructed geometry, the spread receiver against an independent one),
  and refboard simulated with the old and the new code ("Round 2" below).
- The most consequential:
  - **ISSUE-1**: a boost's losses (switches, inductor, diode), and through them its input current, its thermal
    heat and its current capacity, were wrong at any duty but ½. Example: 5 V → 20 V at 1 A put 0.2 W into the
    switch instead of 0.6 W and 0.018 W into the inductor instead of 0.16 W.
  - **ISSUE-5**: every signal path's line impedance came from closed forms used outside their range and fed with
    the wrong heights. On refboard, Z0 read +21 % (outer layers) and +52 % (inner layers) against the
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
| SP-19 | Board-wide resonance heights above 1 GHz | grid convergence (1x / 2x / 4x / 8x) | – | Improved (round 3: strips as wide as their copper, SP-27); round 4: 8x run, 4x vs 8x 0.8 dB rms at 0.5-1 GHz (converged), 2.9 dB rms at 1-1.5 GHz; the grid note points at 4x below 1 GHz and 8x above. Limitation (~3 dB above 1 GHz) |
| SP-20 | Route finding through series parts, `route_missing` | – | – | Covered (test_path, test_checks) |
| SP-21 | Tier 1 gap_crossing: a slot's length, below min_gap_mm none | constructed geometry | 0.01 mm | Verified (round 2, 3 cases) |
| SP-22 | Tier 1 split between two nets named; reference_net over the other net | constructed geometry | 0.05 mm | Verified (round 2) |
| SP-23 | Tier 1 edge_proximity within edge_factor·h, its distance | constructed geometry | 0.01 mm | Verified (round 2; h is the foils' centre spacing: the limit ~17 % wider than with the dielectric, conservative) |
| SP-24 | Tier 1 off_plane for the uncovered length | constructed geometry | 0.05 mm | Verified (round 2) |
| SP-25 | RE102 limit curves (MIL-STD-461G Figs. RE102-1 to -4) | the standard's figures | exact | Verified (round 2, test_CV26); round 3: fixed wing from 2 MHz, helicopter / space curves apart (461H) |
| SP-26 | The path's line: the field-solved cut of the board at the stretch (solder mask, as-built planes) | the 2D field solver on a hand-built section | 1 % | **Fixed** (ISSUE-5's remainder, rounds 3 and 4), Verified (In1.Cu, B.Cu with mask; a stretch whose middle has no cut takes one a quarter along: refboard's 14 paths all field-solved) |
| SP-27 | Strips narrower than the pitch: width, centre, inductance, a neck's resonance on and off the cell rows | the copper's own width; a single strip's integral; the aligned neck | one sub-pitch / 1 % / 2 % | **Fixed** (round 3), Verified (6 cases) |

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
  Every settings file in the repository and `out/` (14) still loads, and so do the real refboard
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
- **On refboard** (read-only, against the as-built cut with solder mask): outer layers old +21-22 %, new
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
- **refboard, old code (fae19c9) against new (audit-2), every net and converter** (they are switched off
  in the user's file), same settings, the board only read:

  | | Old | New | After rounds 3 and 4 (2026-10-18 evening) |
  |---|---|---|---|
  | Line Z0, outer / inner | 52.7 / 54.5 Ω | 44.8 / 38.6 Ω | field-solved cut: 43.1-43.8 / 35.6-41.7 Ω |
  | Worst margin SPI_SCK / SPI_MISO / SPI_MOSI | +15.3 / +18.8 / +18.8 dB | +13.9 / +18.1 / +17.8 dB | +11.4 / +14.8 / +15.4 dB; with lossy path lines (round 4) +11.8 / +15.2 / +15.8 dB |
  | Other nets' worst margins | – | within −0.7 to +0.6 dB of old | SW_CLK +32.8 → +40.5 dB; Net-(U301--) +45.7 → +22.1 and ADC_I_RAIL_1 +34.2 → +21.4 dB, a lossless line at a resonance: with the lines' loss (round 4) +33.5, ADC_I_RAIL_2 +35.4; the rest within −1.5 to +2.1 dB |
  | conv1 switch-node copper loss, sustained density | 19.5 mW, 28.5 A/mm² | 27.7 mW, 35.9 A/mm² | 29.7 mW, 37.4 A/mm² |
  | conv2 switch-node copper loss | 17.6 mW | 20.3 mW | 21.5 mW |
  | Hot-loop L, radiated and conducted margins, other DC paths | – | unchanged | hot loops +6 % (conv1 1.48 → 1.57 nH), margins within 0.2 dB, control loops unchanged |

  The last column: the current board (`Downloads\refboard\refboard.kicad_pcb`, 2026-10-17, the same results as round
  2's board where the code did not change: conv1's loop 11.2 kHz / 76.8° / 20.2 dB, 37 errors, 50 warnings),
  `out/refboard_refresh/now2/`, 18 min on 16 CPU threads; with lossy path lines `out/path_loss/full/` (the converters and the
  issue counts the same).

  The four stages are bucks with CISPR 32 and no spread, so ISSUE-1, -9 and -10 do not show there. The emission
  margins move little because these short lines are capacitive loads at most harmonics; long or terminated lines
  depend on Z0 more.

## Round 3 (2026-10-18, branch `audit-3`)

The user: "Fix this" (inner-layer Z0 and solder mask), "Is that a bad thing?" (interleaved phases), "Try to fix this"
(resonance heights above 1 GHz), and the MIL-STD-461 sources downloaded for review.

- **ISSUE-5's remainder fixed (SP-26).** A path's line is now the field-solved cut of the board at its longest track
  on the main layer (`analysis/si.SectionCache`, the SI module's cuts: as-built planes, laminate between the foils,
  solder mask), once per model (`simulate.line_cache`); the closed forms remain the fallback where no cut can be
  made. Test: the cut against a hand-built section within 1 % on In1.Cu and on B.Cu with its mask; the closed form
  differs by more. On refboard (read-only) the closed forms had read 3-4 % high on the outer layers
  (43.0-43.6 Ω by the cut) and −7.5 to +8.3 % on the inner ones (35.6-41.7 Ω); one stretch (U301−) has no cut there
  and keeps the closed form.
- **MIL-STD-461H** (17 April 2026, supersedes 461G; the full standard is in the downloads). Against 461G, for what
  the code uses: RE102-3 now has "Space and Helicopters" from 10 kHz, fixed wing external from 2 MHz in both
  editions. The aircraft key had the 10 kHz start; it is now three keys: `re102_aircraft_external` (fixed wing,
  2 MHz-18 GHz), `re102_helicopter`, `re102_space` (10 kHz-18 GHz). CE101-1 extends to 20 kHz in 461H; the code
  follows 461G (to 10 kHz). The LISN, CE102, Table II, CE101-4 and RE102-1/-2/-4 are unchanged.
- **Interleaved phases (ISSUE-7's assumption): no error for DC and thermal.** The switching variance
  D(1−D)·|J_hs − J_ls|² is confined to each switch node's own copper, and two stages' switch nodes are different
  nets, so their cross term is exactly zero, locked or not. The combined *emission* of interleaved stages is
  not modelled (README, not done; it followed in round 4, `analysis/interleave.py`).
- **Resonances above 1 GHz (SP-19): the cause and a fix.**
  - The loss physics converges: a plate pair's cavity Q 35.6 against 36.0 by 1/Q = tanδ + δs/h, its peaks within
    0.3 dB from d = 1 to 0.25 mm. The demo board converges within ~1 dB.
  - What does not: copper narrower than the grid. A 15 × 15 mm island joined to its plate by a 0.35 × 3 mm neck
    (~740 MHz) read 900+ / 900+ / 832 / 881 / 722 / 793 / 735 MHz at d = 1.0 / 0.7 / 0.5 / 0.35 / 0.25 / 0.15 / 0.1 mm; the same island with a
    3 mm neck converged at fine grids (1240 / 1243 MHz at 0.15 / 0.1 mm). Every strip was a full cell wide, so a neck
    conducted as if it were a pitch wide, and its inductance was the grid's.
  - Fix (`peec.Grid.wx/wy/ox/oy`, `PlaneStack._widths`, CPU and GPU): a strip is as wide as the copper the sub-grid
    sees cross its border, R/φ and the narrower strip's partial self inductance; side-by-side partial strips get
    their mutual inductance for their real widths and centre distance. Now 797 / 756 / 736 / 764 / 717 MHz from 1.0 to
    0.25 mm, a ±5 % scatter around ~750 MHz where it was 722 to 900+. At 0.15 and 0.1 mm Simulate takes no sub-grid
    (the grid itself is the 0.15 mm sampling), so strips stayed full there (793 / 735 MHz); with the 2 × 2 sub-grid
    every model now takes (branch `small-fixes`), 769 / 742 MHz. The 3 mm neck: 1237 / 1254 / 1269 / 1252 / 1271 MHz from 1.0
    to 0.25 mm (1296 / 1349 / 1324 / 1275 / 1294 before; 1240 at 0.15 mm). On
    one 0.35 mm grid, the neck aligned with a cell row or straddling two reads 751 / 759 MHz (754 / 817 before).
  - Densities follow: the DC maps, peaks, thermal heat and bridge RMS maps take a strip's current over its own
    width (a 0.35 mm neck on a 1 mm grid read about a third of its A/mm²); the heat adds up to the solver's loss
    (CV-28).
  - refboard's SPI_SCK board-wide model at 1x / 2x / 4x, the same 25 harmonics 540-1460 MHz, before
    (`out/converge/spi_sck_head_*.json`, this round's starting code, a worktree of 20ec0a9) and after
    (`spi_sck_widths*.json`). The rms (max) |m| difference between grids, before → after:

    | | 1x vs 2x | 2x vs 4x |
    |---|---|---|
    | 0.5-1 GHz | 2.7 (7.8) → 6.3 (17.2) dB | 1.1 (3.3) → 1.5 (2.7) dB |
    | 1-1.5 GHz | 7.0 (16.7) → 4.0 (8.0) dB | 5.3 (8.3) → 4.4 (7.0) dB |

    The largest mode (F.Cu GND / In1 +3V3 at (119-121, 128-130) mm by C1105 / C1110) was at 1060 / 1100 / 1060 MHz
    (139 / 61 / 53 mm²) and is at 940 / 1020 / 1020 MHz (113 / 69 / 78 mm²): 2x and 4x agree on it now, 1x places it
    8 % low, inside 0.5-1 GHz, hence that band's 1x-vs-2x number. Above 1 GHz 4x has a peak at 1260-1300 MHz that 2x
    has not. `grid_sensitive_note` now applies from 600 MHz (it was 1 GHz), so the mode is flagged at 1x too. The
    20 MHz loop is the same before and after (14.97 / 15.04 mm²). Which frequency is right for the mode (1020 or
    1060 MHz) would take 8x.
- **Not changed:** the combined emission of interleaved stages. (CE101 to 20 kHz under 461H followed as a setting,
  `analysis.mil461`, on branch `mil461h`.) (The sub-grid for cells of
  0.15 mm and less followed on branch `small-fixes`.)

## Round 4 (2026-10-18, branch `next-steps`, on the bigger machine)
The items left in "Remaining risks" (docs/NEXT_STEPS.md), on the 16-thread, 32 GB machine with an RTX 3050. The
refboard board is the user's copy `refboard.kicad_pcb` (read only); its 1x convergence run reproduces round 3's numbers to
the last digit or two.
- **SP-19, the 8x run.** SPI_SCK's board-wide model at 8x (0.305 mm, 256 152 nodes, the same 25 harmonics,
  `out/converge/spi_sck_widths_8x.json`, 44 min on 4 threads). It first failed to converge at almost every
  frequency: GMRES's Krylov basis was limited to 400 MB (`GMRES_MEMORY`), a restart of ~26 at ~1 M unknowns, and a
  short restart stalls near a resonance (`spi_sck_widths_8x_restart26.log`). With ~150 every solve converged in
  116-163 iterations. A plugin Simulate at 8x builds the same model (the node budget scales with the resolution),
  so the restart now has a floor, `GMRES_RESTART_MIN` = 100 (`peec.gmres_restart`; the memory planner,
  `simulate.solve_bytes`, counts the longer basis): 1020 / 1300 MHz at 8x then converge in 284 / 260 iterations
  (`probe_8x_restart_floor.log`). Smaller models are unchanged (they restart at 200 already).
  The rms (max) |m| difference between grids:

  | | 1x vs 2x | 2x vs 4x | 4x vs 8x |
  |---|---|---|---|
  | 0.5-1 GHz | 6.3 (17.2) dB | 1.5 (2.7) dB | **0.8 (1.6) dB** |
  | 1-1.5 GHz | 4.0 (8.0) dB | 4.4 (7.0) dB | **2.9 (5.1) dB** |

  Below 1 GHz 4x is converged. Above it the differences keep shrinking but ~3 dB remain. 4x's peak at 1260-1300 MHz
  (45-52 mm²) is not at 8x (25-35 mm²): at 1280 MHz both grids excite the same mode, the +3V3 pour on F.Cu against
  the +3V3 plane on In1 around (120-123, 123) mm through the same vias and C1104, with 11.6 vs 8.5 A/A of
  displacement current (`hot_1280_4x.log`, `hot_1280_8x.log`): one mode a few per cent apart in frequency, no
  connection that exists on one grid only. The F.Cu/In1 mode by C1105/C1110 stays at 1020 MHz at 2x, 4x and 8x
  (69 / 78 / 62 mm²). `grid_sensitive_note` now names 4x for a peak below 1 GHz (none from 4x on) and 8x above it
  (from 8x on it says the height is good to a few dB). 16x would need ~4x the nodes again, more than this
  machine's memory alongside the solve.
- **ISSUE-5's remainder (line cuts).** `tools/dev/line_cut_check.py` on all 14 nets: one path fell back on the
  closed forms, Net-(U301--) (1.8 mm of track: the middle of its longest track is in a pad's copper). `_line_of` now
  tries the longest track at its middle, then a quarter from either end, then the next two longest tracks
  (`LINE_CUT_TRACKS`); U301- reads 43.8 Ω (closed form 44.8), and no path falls back. Test: a track that widens
  into a pour of its own net at its middle reads the line of the same track without the pour within 1 %
  (`test_SP26_a_stretch_without_a_cut_at_its_middle_takes_one_further_along`; it fails on the old code).
  Net-(U1001-VIB) reads 60.6 Ω against the closed form's 38.6 (In1.Cu): a cut that sees its real neighbourhood,
  not a fallback.
- **Copper narrower than the 0.15 mm sampling.** `tools/dev/neck_converge.py`, the 0.35 mm neck on seven grids
  (1.0 to 0.1 mm, `out/neck/`): at Simulate's sampling (0.15 mm, at most 6 × 6 per cell) 763 / 758 / 736 / 762 /
  716 / 768 / 743 MHz, ±3.5 %; at 0.075 mm and up to 12 × 12, 763 / 783 / 775 / 763 / 807 / 768 / 743 MHz, ±4.1 %.
  A finer sampling does not narrow the spread (NEXT_STEPS asked for ±2 % before trying it on refboard), so `RASTER_MM`
  stays: the remaining scatter is how the grid falls on the geometry, ±4 % on a resonance set by a neck narrower
  than the pitch.
- **Loss in the signal path's lines (NEXT_STEPS 6, at the user's "Fix those").** `PathLine.alpha` (copper from the
  skin depth, strip and plane alike [A36]; dielectric G = ωC tan δ with the filling factor), γ = α + jβ in the
  cascade. Verified: a matched 100 mm microstrip's end voltage against e^(-αl) with Pozar's α at 0.3 / 1 / 3 GHz
  within 0.5 %, an open quarter-wave stub's input against Z0·tanh(αl) within 0.5 % (tests/test_path.py). On refboard:
  ADC_I_RAIL_1 +21.4 → +33.5 dB (C304 carries 1.0 × the input current, not 4.5 ×), ADC_I_RAIL_2 +30.1 → +35.4,
  the SPI nets +0.4 dB, the converters and the issue counts unchanged (`out/path_loss/full/`). Net-(U301--) stays at
  +22.1 dB: its worst is a board-wide plane resonance at 1590 MHz, not the path (the grid note applies).
- **The GPU path on a small GPU (NEXT_STEPS 7).** All of it is complex128, which a GeForce card runs at 1/64 of
  its FP32 rate: the RTX 3050 Laptop GPU was 3.5 × slower than the CPU on a small board and > 6 × on refboard, and two
  processes on it (a split Simulate's workers) stalled each other for hours (`tools/dev/gpu_contention.py`). `auto`
  now times the solver's convolution on the device against the CPU and uses it from 1.5 × (`gpu._speedup`; the
  3050: 0.3-0.8 ×, declined with the reason in the run's summary); one process at a time holds a device
  (`gpu._DeviceLock`, taken before anything runs on it, the speed check included). With the GPU visible,
  test_split's lead-split test passes (it hung for hours), and two `on` processes take turns (38 s and 74 s).
  Tests: a slower device declined with its ratio, a busy device leaves auto on the CPU without touching it.
- **Interleaved stages (the user: "Yes, build it").** `analysis/interleave.py` (ARCHITECTURE "Interleaved stages",
  REFERENCES A35). Verified: the phasors against the DFT of sampled trapezoids and triangles (0.002 rad, 0.2 %),
  the ringing rational against `ringing_response`; 180° and 120° sums exactly; two identical stages end to end
  (180°: the fundamental falls by > 60 dB, 2·f_sw +6.02 dB; in phase +6.02 dB conducted and radiated). On refboard,
  conv1 + conv2 (detection proposes "U303 phases", 0° / 180°): conducted worst −38.6 dB together, −39.6 in phase,
  −38.8 / −27.9 alone (conv1's D = 0.51 fundamental dominates; conv2's D = 0.095 adds little); radiated −21.6 dB
  together at 161 MHz, −25.4 in phase, −19.8 / −21.0 alone (`out/interleave/`).

## Remaining risks and unverified items
- SP-19: board-wide resonance heights above 1 GHz still move by ~3 dB rms between the 4x and 8x grids on refboard
  (converged within 0.8 dB below 1 GHz); the grid note says so and names the grid to check at (round 4).
- ISSUE-5: where no cut can be made at any of the places tried (three tracks, three places each), the closed forms
  remain: very asymmetric inner layers up to ~10 % high, no solder mask. None of refboard's 14 paths does (round 4).
- A resonance set by copper narrower than the grid pitch scatters by about ±4 % from grid to grid (the 0.35 mm neck
  case); a finer copper sampling does not reduce it (round 4).
- MIL-STD-461H: selectable since branch `mil461h` (`analysis.mil461`, default G); between 10 and 20 kHz its ships
  CE101 reads the CE102 band's solves (the LISN as specified, REFERENCES A34).
- The emission estimate's model limits are unchanged: quasi-static, differential mode only, no cables; it ranks
  layouts, it is not a compliance prediction.
- The signal path's lines carry their copper and dielectric loss since round 4 (the plane's loss taken as the
  strip's, A36); before, a line resonance beside a shunt had an unbounded Q.
- Published refboard numbers: refreshed in round 4 (the table under Round 2; the README's loop and Sharper-map figures). README figures from
  other settings or tools (the safety counts, the PDN grids) were not re-taken.

## Final run
- **Round 3, full suite** (`out/audit_validation/full_round3.log`, 76 min, the machine also running neck sweeps):
  **859 passed, 1 skipped, 4 failed**: the launcher's git mode (as before) and three GUI timing tests (0.53-1.06 s
  against 0.5 s). Re-run under the same load, two of them fail on round 3's code and on the code before it
  (HEAD 20ec0a9, a worktree: 0.64 s and 0.87 s), so the load, not the change. After the run, the grid note's
  threshold and one test line changed; their files (test_capacitance, test_validation_audit, test_ce101) were
  run again: 113 passed.
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
