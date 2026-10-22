# Audit round 7: the analog features (DC-bias derating, MNA, loop gain, transfer functions, step, bandwidth)

Branch `audit-7`, 2026-10-22. The user asked for "a thorough, independent scientific and engineering audit of the
features that have just been implemented", in a fixed priority order (DC-bias derating, MNA, op-amp stability,
H(s) / poles / zeros / Bode, step response and bandwidth, the ADC signal settings, the presentation), with every
expected result derived independently of GroundTruth, findings documented before any code change, every correction
protected by a test that fails on the old code, and a final status per feature. Earlier rounds:
[PHYSICS_AUDIT.md](PHYSICS_AUDIT.md) (round 5), [NUMERICS_AUDIT.md](NUMERICS_AUDIT.md) (round 6),
[VALIDATION_AUDIT.md](VALIDATION_AUDIT.md) (the summary of all rounds).

The benchmarks are `tools/dev/analog_audit.py` (every case states its hand-derived expectation in its source and
prints expected / actual / deviation / PASS or FAIL) and `tools/dev/lcapy_reference.py` (an independent nodal
analysis, run in its own venv: it never imports GroundTruth):

```
py -3.13 -m venv %TEMP%\auditvenv && %TEMP%\auditvenv\Scripts\python -m pip install lcapy scipy numpy
%TEMP%\auditvenv\Scripts\python -I tools/dev/lcapy_reference.py REF.json        # Lcapy 1.26, SymPy 1.14
.venv\Scripts\python tools/dev/analog_audit.py --lcapy REF.json [--only dcbias,mna,loop,tf,step,bw] [--json OUT]
```

No SPICE simulator is installed on this machine (no ngspice, LTspice or Qucs): the independent solver is Lcapy
(symbolic modified nodal analysis on SymPy, its numbers evaluated in 30-digit arithmetic). Where the audit says
"independent" it means a hand derivation, SciPy, or Lcapy; never GroundTruth's own functions.

## 1. Executive summary

(Filled in after Phase 7, section 17.)

## 2. Scope (Phase 1)

The rounds audited, from git (`git log 034e7d0^..fa9fa4f`): `analog-ac` (034e7d0, 56fb575, 8fd0b37), `step-response`
(d2233c2), the ADC settings (e1f1131), `bode-legend` (b9f01cf, a091402), `dc-bias` (2f30fef, 73d93c7),
`cbias-estimate` (fa9fa4f); `bode-sweep` before them is in scope for the sweep only. 30 source files, +2913 / −154
lines since e9b2c7d.

### Traceability matrix

| Feature | Implementation | Principle / equation | Assumptions | Reference | Independent validation | Tests (before) | Status |
|---|---|---|---|---|---|---|---|
| DC-bias curve | `components/bias.py`: `parse_bias`, `c_at`, `one_point`, `v1_through`, `resolve` | C(V) = C0/(1+(V/V1)²) (one point); PCHIP through (0, C0) and the points | class II, even in V; one point fixes the curve [A56]; one value at half rating [A57] | [Johnson 1962] (low-field shape), [Fritsch 1980], Murata 2022 (measurement) | closed forms B1, B2, B9, B10; the user's datasheet chart | test_bias (9) | see §6 |
| Voltage across a capacitor | `analysis/bias.py`: `cap_voltages`, `bounds`, `part_bias`, `at_dc`; `analysis/safety.py`: `voltage_model`, `Volts.dv`; `analysis/pdn.rail_voltage` | ΔV = V(a) − V(b) at the DC operating point | worst-case bounds per net [A58] | — (an engineering bound) | hand-derived operating points B3-B7 | test_bias | see §6 |
| Applying it | `at_dc` in `simulate`, `bode_paths`, `suggest_fixes`; `ComponentModel.capacitance` | the small-signal (incremental) C at the operating point | one pass (C does not move the DC point: capacitors are open at DC) | Murata 2022 | inspection | test_bias (end to end) | see §6 |
| MNA | `emi/mna.py`: `Circuit.r/c/l/vsource/vcvs/vccs/opamp/part/y2`, `solve`, `transfer` | (G + sC)x = b, KCL + branch equations | linear, small signal | [Ho 1975] | closed forms M1, M2; Lcapy M3 (7 circuits, 61 frequencies) | test_mna (10) | see §7 |
| Poles / zeros | `Circuit.poles_zeros`, `_finite_eigs`, `_cancel`; `simulate.cancel_doublets` | generalised eigenvalues of (G, −C); bordered system matrix | roots < 1 mHz at the origin | [Golub 2013], [Rosenbrock 1970] | Lcapy's numerator / denominator roots (30 digits) M4; closed form M2 | test_mna, test_transfer | see §8 |
| Op-amp macromodel | `Circuit.opamp`, `OpAmpParams`, `analog_net` (fp2 from PM) | A(s) = A0/((1+s/ω1)(1+s/ω2)), R_O, Cin | small signal, no rails or slew | [Franco 2015], [A52] | Lcapy (same model, independent solver) | test_mna, test_analog_net | see §7 |
| Loop gain | `Circuit.loop_gain` | Bode's return ratio T = −v_r/v_t at the controlled source | the break at an ideal source is exact | [Bode 1945], [Middlebrook 1975] | T = A·β by hand L1-L3 | test_analog_net | see §10 |
| Phase margin, `opamp_stability` | `transfer.phase_margin`, `simulate._bandwidth_and_stability` | PM = 180° + ∠T at |T| = 1 | first crossover; negative feedback | [Nyquist 1932], [Ogata 2010] | closed-form crossover L1-L3; positive feedback L4; three crossovers L5 | test_analog_net | see §10 |
| Symbolic H(s) | `analysis/symbolic.py` | MNA in s, fraction-free elimination | ideal op-amps (nullors), no parasitics [A54] | [Ho 1975] | closed forms (test_symbolic) | test_symbolic (4) | see §8 |
| Normal form, doublets | `transfer.normal_form`, `simulate.cancel_doublets` | H = K s^m Π(1−s/z)/Π(1−s/p) | a pair within 0.1 % of |p| cancels | [Franco 2015] | the response near a high-Q pair T1 | test_transfer | see §8 |
| Bode sweep | `simulate.sweep_range`, `bode_paths`, `network_responses` | log-spaced f, s = j2πf | — | — | inspection; M3 | test_bode | see §8 |
| Step response | `Circuit.step` (BDF2), `simulate._step_of`, `transfer.step_metrics` | C x' + G x = b u | 12 τ of the slowest pole, 3000 steps | [Gear 1971], [Ogata 2010] | closed form RLC overshoot S1 (Q 2-300); unstable S2 | test_step (4) | see §9 |
| Bandwidth limit | `transfer.upper_3db`, `_bandwidth_and_stability`; tile: `gain_figures` | −3 dB frequency | from the passband peak (check) / the low-frequency gain (tile) | [Franco 2015] | closed form one pole W1, second order Q 0.7-2 W2 | test_analog_net | see §10 |
| ADC signal | the user's settings `freq: 1kHz, rise_time: 2us` | the trapezoid's repetition and edge (NetSpec.freq, rise_time) | the emission spectrum's source | ZXCT1107 datasheet DS35033 | inspection | — | see §11 |
| Presentation | `plots.draw_bode` legend, `pz_tip`, `results_panels._hover`, report tables | f0, Q, ζ = 1/2Q | — | [Ogata 2010] | test_transfer (legend, hover) | 2 | see §13 |

### Initial risk assessment

Highest: the voltage across a capacitor (it can look plausible and be far off), the loop gain's sign and margin (a
plausible Bode plot does not prove stability), the bandwidth check's reference (a check that passes when it should
not). Lower: the MNA stamps (textbook, already compared with closed forms), the presentation.

## 3. Reference register (Phase 2)

| Key | Source | Supports | Limits |
|---|---|---|---|
| Ho 1975 | C.-W. Ho, A. E. Ruehli, P. A. Brennan, "The modified nodal approach to network analysis," *IEEE Trans. Circuits Syst.*, vol. CAS-22, no. 6, pp. 504-509, June 1975 | the MNA formulation: node voltages plus the currents of voltage-defined branches; the element stamps | linear elements |
| Golub 2013 | G. H. Golub, C. F. Van Loan, *Matrix Computations*, 4th ed., Johns Hopkins, 2013 (§7.7, the QZ algorithm) | finite generalised eigenvalues of a singular pencil | conditioning: the pencil is equilibrated first |
| Rosenbrock 1970 | H. H. Rosenbrock, *State-Space and Multivariable Theory*, Nelson, 1970 | transmission zeros as the roots of the system matrix [[A − sE, B], [C, D]] | — |
| Bode 1945 | H. W. Bode, *Network Analysis and Feedback Amplifier Design*, Van Nostrand, 1945 (the return ratio) | T = −v_r/v_t with the controlled source replaced by a test source | (new key: added to REFERENCES) |
| Middlebrook 1975 | R. D. Middlebrook, "Measurement of loop gain in feedback systems," *Int. J. Electronics*, vol. 38, no. 4, pp. 485-512, 1975 | voltage injection gives the loop gain exactly where the injection point is driven by a zero-impedance source | GroundTruth injects at the macromodel's ideal buffer: the condition holds by construction |
| Nyquist 1932 | H. Nyquist, "Regeneration theory," *Bell Syst. Tech. J.*, vol. 11, pp. 126-147, 1932 | stability from the encirclements of −1 by T(jω): with several |T| = 1 crossings each crossing's margin matters; T(0) < −1 (positive feedback) is unstable | (new key) |
| Ogata 2010 | K. Ogata, *Modern Control Engineering*, 5th ed., Prentice Hall, 2010 | phase margin; a second-order step's overshoot e^(−πζ/√(1−ζ²)), its peak time π/ω_d; rise and settling definitions | second-order closed forms |
| Franco 2015 | S. Franco, *Design with Operational Amplifiers and Analog Integrated Circuits*, 4th ed., McGraw-Hill, 2015 | the op-amp's two-pole open-loop model; a low-pass's bandwidth as its −3 dB point below the passband (DC) gain | the macromodel is small signal |
| Gear 1971 | C. W. Gear, *Numerical Initial Value Problems in Ordinary Differential Equations*, Prentice-Hall, 1971 | BDF2: second order, A-stable, damping stiff modes; its numerical damping of an oscillation grows as the step's ωh grows | a resolved oscillation needs ωh ≪ 1 |
| Johnson 1962 | K. M. Johnson, "Variation of dielectric constant with voltage in ferroelectrics and its application to parametric devices," *J. Appl. Phys.*, vol. 33, no. 9, pp. 2826-2831, 1962 | the permittivity's even, quadratic fall with the field at low field | not the full shape of a commercial X7R curve (A56 is fitted, not derived) |
| Fritsch 1980 | F. N. Fritsch, R. E. Carlson, "Monotone piecewise cubic interpolation," *SIAM J. Numer. Anal.*, vol. 17, no. 2, pp. 238-246, 1980 | the interpolant through the points preserves monotonicity: no overshoot between them | interpolation, not a model beyond the points |
| Murata 2022 | Murata, "SimSurfing Multilayer Ceramic Capacitors Characteristics Viewer: Measurement Conditions," Sept. 2022, pp. 7-8 (ds.murata.com/simsurfing_data/pdf/en-us/mlcc/sim_mlcc_measuringcond_e.pdf) | DC-bias data are an LCR meter's small-signal capacitance (1 kHz for C ≤ 10 µF, 120 Hz above; 0.5-1 Vrms) at each DC bias from 0 V to the rated voltage, 25 °C, 60 s applied; C0G "hardly change" | typical characteristics, not guaranteed limits; no data above the rated voltage (new key) |
| ADI MLCC note | Analog Devices, "How to Measure Capacity Versus Bias Voltage on MLCCs" (analog.com technical article) | the effect is present in all class II MLCCs and differs considerably between types: use the part's own curve | (supporting, not cited in code) |
| Lcapy | M. Hayes, Lcapy 1.26 (lcapy.readthedocs.io), symbolic linear circuit analysis on SymPy | the independent reference solver of this audit | its own models of R, L, C, E (VCVS) only |
| ZXCT1107 | Diodes Inc., ZXCT1107/1109/1110 datasheet DS35033 Rev. 3-2, July 2011, pp. 3, 8 | its 650 kHz bandwidth at R_GAIN 2.5 kΩ; the pulse responses at 10 kΩ | typical values |

Unverified: the page of Gear 1971 with BDF2's damping formula (not opened in this audit; the claim is checked
numerically instead, §9).

## 4-5. Independent derivations and results (Phases 3-4)

The expectations are written beside each case in `tools/dev/analog_audit.py`. The baseline run on `main` fa9fa4f
(before any change): **DC bias 20 comparisons, 4 FAIL; MNA 25, 1 FAIL (a reference artefact, below); loop 11,
2 FAIL; transfer 2, 2 FAIL; step 6, 4 FAIL; bandwidth 9, 2 FAIL.**

### 6. DC-bias derating

The curve (B1, B2, B7, B9-B11): **PASS**, exact to rounding. C(0) = C0; C(40 V) = 40 nF for 40nF@40V; 12 V gives
C0/(1+(12/32.660)²) = 88.106 nF; C(−12 V) = C(+12 V); a 4-point list passes through each point; beyond the last
point the one-point tail through it (100 V: 13.053 nF); duplicate voltages, a list without voltages, −100 %, 0 nF
are refused. No double multiplication by C0: absolute points are used as given, percent points once (C0·(1 + %)).

The voltage across each capacitor, on a generated board (+12V, +5V, −12V rails; a 10k/10k divider; an AC-coupled
inverting stage on 5 V with VREF = 2.5 V from a divider and Cf across Rf; two capacitors in series):

| Case | Real DC across it (hand) | GroundTruth | |
|---|---|---|---|
| B3 between +12V and +5V | 7 V | 7 V | PASS |
| B4 two in series, midpoint only on capacitors | undefined (set by leakage) | unknown, named in a note | PASS |
| B5 a 10k/10k divider's midpoint to GND | 6 V | 12 V | **FAIL** |
| B6 Cf across Rf (virtual ground, AC-coupled input) | 0 V | 5 V | **FAIL** |
| B7 between +12V and −12V | 24 V | 0 V | **FAIL** |

### 7. MNA

Closed forms (M1 RC, M2 series RLC Q 2, its poles, its DC gain): **PASS**, errors 3e-16-8e-16. Against Lcapy
(M3, M4: RC, RLC, an inverting ×10 with A0 1e5, GBW 1 MHz, fp2 20 MHz, R_O 50 Ω; a non-inverting ×2 into 1 nF; a
Sallen-Key; an MFB band-pass; a circuit of values from 1 pF to 1 mF and 0.1 Ω to 10 MΩ): **PASS**, H(jω) within
3e-13 everywhere, poles and zeros within 5e-13. One case first failed: the wide-value circuit's H by 4.9e-4 and its
poles by 1e27. That was the reference's: a float evaluation of Lcapy's polynomials (coefficients from 1e-30 to
1e10) loses its digits; in 30-digit arithmetic GroundTruth's H agrees to 3e-13. Its poles differ for a documented
reason: GroundTruth takes a root below 1e-6·2π·F_REF (1 mHz) as at the origin (every node of a board's circuit has
a 1 TΩ leak, whose roots are numerical residue), so the circuit's real pole at 0.18 mHz (τ ≈ 900 s) is listed at the
origin, and its step is refused as an integrator's (AA-9, informational).

### 8. Transfer functions, poles, zeros

The plotted curve is the full circuit's own solve (`net.circuit.transfer`), the listed roots and the written H(s)
the lumped circuit's exact roots with doublets cancelled. T1: a pole pair of Q 2000 at 1 MHz with a zero pair 0.05 %
or 0.09 % above it is cancelled (DOUBLET 0.1 % of |p|) though the circuit's response deviates **7.7 dB / 11.7 dB**
near it: the written H(s) and the root lists hide it (AA-8). The curve is right.

### 9. Step response

S1 series RLC, closed-form overshoot e^(−πζ/√(1−ζ²)) at the grid `_step_of` picks: Q 2 within 0.003 % points; Q 20
90.5 % vs 92.4 %; **Q 100 62.9 % vs 98.4 %; Q 300 25.0 % vs 99.5 %** (AA-6): 3000 steps over 12 time constants of
a pole whose time constant is 2Q/ω0 leave 2.6-8 steps per period at high Q, where BDF2 neither resolves the first peak
nor keeps the oscillation's amplitude. S2 an unstable circuit (pole at +1000 /s): its step grows without bound, yet
its metrics give a final value −1 (the DC gain of an unstable system, which it never reaches) (AA-7).

### 10. Loop gain, phase margin, bandwidth

Loop gain (L1-L3, T = A(s)·β by hand, crossover by root finding on the closed form): **PASS**, T within 1e-10, the
crossover within 2e-7, the margin within 3e-5°: a follower 90.0006°, a non-inverting ×10 with fp2 5 MHz 78.90°, a
follower with R_O 100 Ω into 10 nF 4.83° (the break at the ideal buffer keeps R_O's loading: exact). **L4** a
follower with its inputs swapped (positive feedback, T(1 Hz) = −A(j1 Hz) = −99010): reported with a phase margin of
**270°**, no issue (AA-3): the loop latches. **L5** three |T| = 1 crossings at 101 kHz (98°), 1.88 MHz (155°) and
2.11 MHz (27°): reported **98°** (the first), the loop's margin is 27° (AA-4).

Bandwidth (W1 one pole, 300 / 30 / 8 points: within 1e-6, the sweep starting at 1 Hz, not DC). W2 a second-order
low-pass, the usual bandwidth its −3 dB point below the DC gain (closed form x² = (b + √(b²+4))/2, b = 2 − 1/Q²):
the tile (`gain_figures`) within 7e-4; **the check (`upper_3db`, from the peak) 8 % low at Q 1, 22 % low at Q 2**
(AA-5): a peaked low-pass above its max_bandwidth passes the check.

### 11. ADC signal settings

`freq: 1kHz, rise_time: 2us` on kraft's two ADC paths are NetSpec.freq and rise_time: the repetition frequency and
the 10-90 % edge of the trapezoid whose harmonics drive the emission estimate (emi/path, simulate._signal_harmonics).
They are not a sampling rate, a sampling period, an acquisition or a conversion time: GroundTruth does not model the
ADC's sampling (its receiver is a load C, 5 pF by default). 1 kHz is the user's max_bandwidth for the path (the
measured current's band); 2 µs is read off the ZXCT1107's typical pulse responses at R_GAIN 10 kΩ (DS35033 p. 8,
measured on the ZXCT1110 test circuit), kraft's R304 / R334. A 1 kHz trapezoid with 2 µs edges has its spectrum
corner at 1/(π·2 µs) = 159 kHz, far below CISPR's 30 MHz start: consistent with the run's "no radiated worst case".
Status: verified with limitations (typical data, read from a plot; the ADC itself not modelled) (AA-11).

## 13. Findings (Phase 5)

| ID | Severity | Where | Finding | Evidence | Impact | Correction |
|---|---|---|---|---|---|---|
| AA-1 | High | `analysis/bias.cap_voltages`, `bounds` | The voltage across a capacitor is not the DC operating point: each unknown net gets an independent bound (0 … an IC's supply, or a resistor's far side) and the capacitor the widest difference, so a divider's midpoint reads its top rail (12 V for 6 V) and a feedback capacitor across Rf reads the op-amp's whole supply (5 V for 0 V) | B5, B6 | over-derating: a 16 V X7R feedback capacitor at 5 V loses ~38 % under the estimated curves instead of 0 %; an active filter's corner moves ~60 % | the DC operating point of the linear network (resistors, inductors, op-amp channels; capacitors open) with the known rails as sources, by superposition, each unknown source's range kept (an interval) |
| AA-2 | High | `analysis/pdn.rail_voltage`, `safety.voltage_model` | A negative rail's sign is dropped ("-12V" → +12 V): a capacitor between +12V and −12V sees 0 V instead of 24 V; the same voltages feed the safety checks (cap_voltage, clearance_voltage) | B7 | a capacitor across ±12 V is neither derated nor checked against its rating | signed rail voltages in voltage_model (a name starting with − / "-" is negative); dv already handles ranges |
| AA-3 | High | `transfer.phase_margin`, `opamp_stability` | A loop with positive feedback at low frequency (∠T ≈ 180°) is given a phase margin of 270° and no issue | L4 | a latching / unstable stage (inputs swapped) reported stable | T(f_low) with a negative real part and |T| > 1: unstable (Nyquist), an error |
| AA-4 | Medium | `transfer.phase_margin` | Only the first |T| = 1 crossing is evaluated | L5: 98° reported, 27° at the third crossing | a loop with a resonance in its feedback looks well damped | the smallest margin over every downward crossing, with its crossover |
| AA-5 | Medium | `transfer.upper_3db` (bandwidth_limit) | The bandwidth is measured 3 dB below the response's peak, not below its passband (DC) gain as the −3 dB tile does | W2: Q 1 8 % low, Q 2 22 % low | a peaked low-pass above its max_bandwidth passes the check | the low-frequency gain as the reference when the response starts in its passband (within 3 dB of its peak), the peak only for a band-pass |
| AA-6 | Medium | `simulate._step_of` | The time step is set by the slowest pole only: a lightly damped resonance is under-resolved | S1: Q 100 62.9 % vs 98.4 %, Q 300 25 % vs 99.5 % | a ringing circuit's overshoot reported far too low | at least STEP_PER_PERIOD steps per period of the fastest lightly damped (Q > 0.5) listed pole, up to a cap, with a note when capped |
| AA-7 | Medium | `simulate._step_of`, `transfer.step_metrics` | An unstable circuit's step reports the DC gain as its final value | S2: final −1 for a response growing as e^(1000 t) | a nonsense "final value" beside an unbounded response | a right-half-plane pole: the step marked unstable, no final value, rise or settling |
| AA-8 | Low | `simulate.cancel_doublets` | A pair within 0.1 % of |p| cancels in the root lists and the written H(s) even when it is within 0.1 % of a high-Q pole's |p| but far outside its bandwidth | T1: 7.7 / 11.7 dB hidden at Q 2000 | the written H(s) does not reproduce the plotted curve near the pair (the curve is right) | cancel only within 0.1 % of the pole's distance from the jω axis, |Re p|: then |(s−z)/(s−p)| − 1 ≤ 0.1 % for every s on the axis |
| AA-9 | Info | `Circuit.poles_zeros` | Roots below 1 mHz are taken as at the origin | the wide-value circuit's 0.18 mHz pole | a 900 s time constant reads as an integrator | documented (a board's 1 TΩ node leaks make such roots residue) |
| AA-10 | Info | `analysis/bias.py`, help, A56 | The derated value is the small-signal capacitance at the DC operating point (as the makers measure it: Murata 2022); it is used in the AC sweeps, the hot loops and the small-signal step. It is not a large-signal charge model (a snubber across a switch node, a large step) and there is no data above the rated voltage | Murata 2022 | none for small-signal results; a large-signal transient would need Q(V) | documented in A56 and the help |
| AA-11 | Info | the user's ADC path settings | 1 kHz / 2 µs are the emission signal's repetition and edge, not ADC timing | §11 | — | documented |
| AA-12 | Low | help "Cbias", A58 | "Less capacitance is the pessimistic side" is not true for control loops: kraft's conv1 phase margin rose 77° → 84° with its derated output capacitors | the dc-bias kraft run | a reader may take the derated loop figures as a worst case | the claim narrowed to filters and decoupling |
| AA-13 | Low | `_bandwidth_and_stability` | A sweep that ends below max_bandwidth without the response falling 3 dB is silently unchecked | inspection | a limit violation above the sweep goes unnoticed | a note: sweep higher |
| AA-14 | Low | report / Parts tab "V across" | The voltage shown is a worst-case bound (after AA-1: the operating point's worst within its sources' ranges), labelled as if measured | inspection | — | the label says "worst-case DC" |

## 14. Corrections (Phase 6)

(Filled in as each correction is made, with its regression test.)

## 15-17. Re-audit, limitations, status

(Filled in after Phase 7.)
