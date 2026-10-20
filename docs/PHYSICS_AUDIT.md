# Physics audit, round 5 (2026-10-19 to -20, branch `audit-5`)

The fifth round of the validation audit ([VALIDATION_AUDIT.md](VALIDATION_AUDIT.md) holds rounds 1-4): a second,
deeper check of the physics, the engineering assumptions and the references behind them, with the track width
(a track narrowed from 0.4 to 0.1 mm) followed through every calculation it reaches. It contains:

1. [Scope, method and baseline](#1-scope-method-and-baseline)
2. [Physics audit report](#2-physics-audit-report): model by model
3. [Reference verification register](#3-reference-verification-register)
4. [Track width: 0.4 → 0.1 mm](#4-track-width-04--01-mm)
5. [Issue log](#5-issue-log)
6. [Benchmarks](#6-benchmarks)
7. [The real board](#7-the-real-board-kraft_flux_mini_2)
8. [Final assessment](#8-final-assessment)
9. [Completion criteria](#9-completion-criteria)

The regression tests are [tests/test_track_width.py](../tests/test_track_width.py) (TW-1 to TW-15) and
[tests/test_physics_audit.py](../tests/test_physics_audit.py) (PA-1 to PA-4), plus one window test
(`test_simulate_reads_a_board_changed_since_it_was_loaded`, ISSUE-19); the independent reference solvers are
[tests/refsolvers.py](../tests/refsolvers.py); the width sweep is
[tools/dev/track_width_check.py](../tools/dev/track_width_check.py), the real board's before / after
[tools/dev/dc_thermal_summary.py](../tools/dev/dc_thermal_summary.py) and
[tools/dev/topology_diff.py](../tools/dev/topology_diff.py). Source keys in square brackets are in
[REFERENCES.md](REFERENCES.md).

The round ran in two parts: the audit and its fixes (2026-10-19, ISSUE-11 to -18), then, at the user's "fix all of
it", the open items (2026-10-20): the plugin's stale board (ISSUE-19), the heat Thermal left out (ISSUE-20), the
width-step error (ISSUE-21), heat put beside the copper (ISSUE-22, found on the way), the DC figures at temperature
(ISSUE-23), and the thermal mesh's budget on long narrow tracks (ISSUE-24, documented).

## 1. Scope, method and baseline

**Scope.** Every model that turns copper geometry into an electrical or thermal number: the board model and its
units, the PEEC grid (strip widths, node areas, topology), resistance and skin effect, vias, the DC solves (drops,
losses, current density, the checks), the copper heat and the 3D thermal solve (conduction, convection, radiation,
ρ(T)), the IPC-2221 via capacity and clearances, the plate capacitances. The emission, converter and loop models were
validated in rounds 1-4; their references were re-checked here (section 3), their equations were not re-derived.

**Method.** Each calculation was traced from the board file to the number shown; its equation was compared with its
source, and its result with something the code does not compute itself:
- closed forms (R = ρL/(w·t), J = I/(w·t), the slab's surface impedance, series capacitors);
- shapely's exact geometry (border lengths, cell areas);
- two reference solvers written for this round and sharing no code with GroundTruth
  ([tests/refsolvers.py](../tests/refsolvers.py)): a finite-difference sheet-resistance solve of any copper shape
  and a 2D finite-difference cross-section of a heated track on a board, each checked for its own convergence;
- the same copper drawn in different ways (one track, two collinear tracks, a zone): identical drops to 0.01 mV.

**Baseline.** `main` at 68b85af, run from a separate worktree so that nothing edited during the audit could leak into
it (`out/audit5/baseline_full_suite.log`): **901 passed, 5 skipped, 0 failed** in 37 min. The first attempt ran in
the working tree while code was being changed and was discarded.

**What was re-checked from rounds 1-4.** The fixes they record are in the code (ISSUE-1 to -10: the boost duty
convention in `part_losses`, the field-solved line cuts, the switch node's RMS run, the LISN's port resistor, the
spread receiver's search); their tests pass on the baseline and on this branch. What they had not examined: the
grid's widths and areas, the copper heat's consistency with the solved loss, the sustained density's window, the
skin-effect formula, the thermal model's accuracy for narrow tracks, the gap topology, and the references' content
(round 4 listed them, marking the unchecked locations).

## 2. Physics audit report

Status: **Verified** (an independent method agrees within the stated tolerance), **Corrected** (a defect found and
fixed this round, section 5), **Covered** (validated in rounds 1-4, re-read here), **Limitation** (a known error,
quantified, documented).

| Model | Equation / method | Assumptions | Validity, evidence | Status |
|---|---|---|---|---|
| Board geometry and units | KiCad S-expression and kipy (nm → mm); tracks as round-capped buffers, unioned per (layer, net) | widths as stored, no snapping or minimum | mm in the board model, m in `solver/`; width read as stored (`file_source._track`, `kipy_source._mm`). One segment, two collinear segments and a zone of the same copper give the same drop to 0.01 mV | Verified |
| PEEC strips' widths | finite-volume face σ·t·w/d, w = the copper's exact length across the cell border [Patankar 1980] [A39]; a narrow band's width along its strips in series for R | two-point flux over the strip's length; the series band 1D | 0.05-0.4 mm tracks at 0°, 30°, 45°, any grid offset: R' within 1 % (TW-1, TW-3); a neck's steps a fixed ≤ 0.18 square per end (TW-4); wider bands' steps within ±d/2 (A39) | **Corrected** (ISSUE-11, ISSUE-21) |
| Node copper area, density in the copper | exact cell area by Green's theorem [Kreyszig 2011]; |J| from the strips' heat over that area | heat-consistent magnitude, mean direction | the copper heat = the solved I²R within 0.5 % at any angle (TW-3); J = I/(w·t) within 2 % (TW-1) | **Corrected** (ISSUE-12) |
| Topology | one connected piece of a net's copper per cell; strips and track walks within a piece | sub-grid ≥ 2 × 2 samples per cell | a 0.15 mm gap reported as not connected (TW-7); slots narrower than a sample that do not separate the copper are still bridged (A39) | **Corrected** (ISSUE-15) |
| Resistance at frequency | R_ac/R_dc = Δ(sinh 2Δ + sin 2Δ)/(cosh 2Δ − cos 2Δ), Δ = t/δ [Dowell 1966] [A2] | field on one face (strip over its plane) | equal to the slab's surface impedance to 1e-6, 1 kHz-10 GHz (PA-1) | **Corrected** (ISSUE-16) |
| Copper resistivity | ρ = 1/58 Ω·mm²/m at 20 °C, α = 0.00393 /K [IEC 60028] | 100 % IACS for foil and plating | ED and plated copper conduct a few per cent less (A27): DC resistances that much low | Verified, limitation documented |
| Via barrel | R = ρ·L/(π·d·t_plating) [A3]; L of a filament at the radius [Grover 1946] | t = 25 µm; d the drill | π·d·t lies between the finished- and drilled-hole readings (±t/d, 8 % at 0.3 mm); track + via + track against the sheet solves within 2 % (TW-6) | Verified (A3 states the reading) |
| DC solve (drops, losses) | resistive PEEC at DC, injections over pad areas; R = Σ V·I (Tellegen) | current sinks, ideal source pin, 20 °C copper [A42] | linear in current; drop and loss 4.00 × for 0.1 against 0.4 mm at a fixed current (TW-2); the neck, corner and via against independent solves within 2-3 % (TW-4 to TW-6); after Thermal each path also at its temperature (TW-15) | Verified; **Added** (ISSUE-23) |
| Current density check | sustained = √(Σ J²·A / Σ A) over the node's net within 1 mm [A40]; limits 50 / 100 A/mm² | rules of thumb | another net's pour no longer dilutes it (TW-8); the limits have no published source and do not depend on width or cooling (A40) | **Corrected** (ISSUE-13), limitation documented |
| IPC-2221 via capacity | I = 0.024·ΔT^0.44·A^0.725, A = π(d + t)t in mil² [IPC-2221B] [A17] | an internal conductor's chart for a barrel | the code is the formula (0.3 mm via, 25 µm, 10 K: 0.95 A); the formula is a fit to IPC-2221's Figure 6-4, superseded by IPC-2152 for conductor sizing | Verified (code), reference completed |
| Clearance (safety) | IPC-2221B Table 6-1, B1 / B2 / B4; above 500 V the 500 V value + per-volt | voltages from the operating points | all nine rows and the per-volt values agree with the published table | Verified |
| Plate capacitance | C/A = ε0 / Σ(t_i/εr_i), tan δ weighted by t_i/εr_i [Pozar 2012] | parallel plates | two dielectrics against series capacitors to 1e-6 (PA-2); the mean εr read 1.6 % high on a 3.5 / 4.5 hybrid, 0.1 % on FR-4 | **Corrected** (ISSUE-17) |
| Copper heat → thermal | heat per node ρ·J²·t·A·(1 + α(T − 20)) in the node's own copper [A15] [A44]; plus the inductor's ripple, the hot loop's switching current and the via barrels [A43] | currents fixed, ρ(T) first order | heat = solved loss (0.1 %, ISSUE-12); an edge node's heat on its copper, not in the resin beside it (TW-14: was 36 % hot at fine quality, ISSUE-22); the barrels' heat (TW-13), the ripples (CV-28, PA-4) | **Corrected** (ISSUE-12, -20, -22) |
| 3D thermal conduction | finite volume, cut-cell copper, adaptive mesh [Patankar 1980] | laminate k_xy / k_z, resin 0.3 W/(m·K) in empty copper slabs [A27] [A41] | a long 0.4 and 0.1 mm track within 2 % of the independent cross-section where the mesh resolves it (TW-10); a fin's profile (tests/thermal); the mesh's budget leaves part of a long narrow track coarse at normal quality (−5 to −12 % there; fine: +1.5 %) | Verified, limitation documented (ISSUE-24) |
| Convection | Nu = 0.54 Ra^¼ / 0.15 Ra^⅓ (hot face up), 0.52 Ra^⅕ (down), Churchill-Chu vertical, flat-plate forced, Nu³ mixing [Incropera 2011] [Churchill 1975] [Churchill 1977] | one h per face from its mean temperature [A41] | the equations match their sources; extrapolated below Ra ≈ 10⁴ (small boards); the heated-face-down form edition-specific (unverified after a second search, section 3) | Verified, limitations documented |
| Air properties | Sutherland μ(T) [Sutherland 1893] [White 2006], k(T) power law [A28] | dry air, 1 atm | μ(300 K) within 0.3 % of Incropera's table, k 1.1 %, Pr 1.1 % (PA-3) | **Corrected** (constants to the cited source) |
| Radiation | h_r = εσ(T² + Ts²)(T + Ts) [Incropera 2011] | ε 0.9 mask, 0.1 copper [A27] | exact linearisation, updated per iteration | Verified |
| Temperature map | each level-0 cell's hottest refined cell [A41] | – | a 0.1 mm track's map value within 2 % of its node temperature (TW-11) | **Corrected** (ISSUE-14) |
| Line impedance, loss | field-solved cut; closed forms [Hammerstad 1980] [Wheeler 1978]; R' from [A2], plane from Johnson's spread [A36] | quasi-TEM [A5] | rounds 1-4 (SP-2, SP-14, SP-26); `plane_resistance` re-derived here by Parseval: [a·atan(a/h) − (h/2)·ln(1 + a²/h²)]/(π a²) | Covered |
| Inductance kernels, PEEC system | partial inductances [Ruehli 1972] [Paul 2010] [Grover 1946]; MNA [Ho 1975]; GMRES [Saad 1986] | quasi-static [A1] | rounds 1-4 (SP-15, SP-27, test_solver) | Covered |
| Emission, spectra, limits | trapezoid spectrum and loop field [Paul 2006] [Balanis 2016]; CISPR 32, MIL-STD-461 | differential mode only [A11] | rounds 1-4 (SP-1 to SP-25); CISPR 32 radiated and conducted (average) limits re-checked against the published values | Covered |
| Converters, loops, LISNs | [Erickson 2001] [Ridley 1991] [Middlebrook 1976], CISPR 16-1-2, MIL-STD-461 | CCM [A13] | rounds 1-4 (CV-1 to CV-30) | Covered |
| Crosstalk | NEXT = K_b·min(1, 2T_d/t_r), FEXT = |K_f|·T_d/t_r, K_b = (L_m/L + C_m/C)/4, K_f = (L_m/L − C_m/C)/2 [Hall 2000] | weak coupling | re-read: as published | Covered |
| Pins (THT) | alloys by % IACS [CDA], SAC305 1.3e-7 Ω·m [A18] | – | brass 28 %, phosphor bronze 15 %, BeCu 22 %, Te-Cu 93 % IACS give the code's resistivities | Verified |

## 3. Reference verification register

Every source in [REFERENCES.md](REFERENCES.md). **Verified**: the identity (authors, title, venue, edition, year) is
right and what the code takes from it was checked: against the source's text where the source was at hand (the
MIL-STD-461 PDFs), by deriving it again, or by an independent numerical test. **Verified (content)**: the identity is
right and the result used is the source's standard one, checked by derivation or test; the section number was not
re-read (no copy at hand). **Corrected**: wrong or incomplete, fixed this round. **Unverified**: could not be
checked; the entry says so.

### Electromagnetics, lines, EMC

| Key | Supports | Location | Implemented vs source | Applicability | Status | Action |
|---|---|---|---|---|---|---|
| Ruehli 1972 | partial inductance | method | loops as sums of partials, as published | quasi-static [A1] | Verified (content): loop L against Wheeler / image theory 3-6 % (SP-15) | keep |
| Ruehli 1974 | the PEEC method | method | cells with partial L, C, R solved as a circuit | quasi-static [A1] | Verified (content) | keep |
| Paul 2010 | filament / bar partials, Neumann | ch. 5 (usual) | `filament_mutual` = μ0/(2π)·[ℓ asinh(ℓ/ρ) − √(ℓ² + ρ²) + ρ] form | filaments; bars by integration | Verified (content): strip self against numeric integration (SP-27) | keep |
| Grover 1946 | GMD of a rectangle 0.2235(a + b), thin tube = radius | tables | as published (e^(−3/2) = 0.2231 for a line) | – | Verified (content) | keep |
| Jackson 1999 | m = ½∫ r × J dV | §5.6 | as published | – | Verified (content): loop field (SP-5) | keep |
| Ramo 1994 | δ = 1/√(π f μ0 σ), surface resistance | ch. 4 (usual) | as published | good conductor | Verified (content) | keep |
| Dowell 1966 | one-sided slab's R_ac (m = 1) | eq. for F_R | Δ(sinh 2Δ + sin 2Δ)/(cosh 2Δ − cos 2Δ), series 1 + 4Δ⁴/45 | field on one face | Verified: equal to Re((k/σ)coth kt) to 1e-6 (PA-1) | new |
| Pozar 2012 | tan δ, plates, cavities, μh per square, line input impedance, α | ch. 2-3, 6 (usual) | as published; plates in series (new) | – | Verified (content): PA-2, SP-16, test_path | keep |
| Paul 2008 | per-unit-length L, C, Z0, modes | ch. 3-5 (usual) | L = μ0ε0C0⁻¹ | quasi-TEM [A5] | Verified (content): against Cohn's exact results (test_xsection) | keep |
| Johnson 1993 | crosstalk saturation; return spread 1/(1 + (x/h)²) | ch. 5 (usual) | the image solution of a line over a plane | thin strip, infinite plane | Verified: `plane_resistance` re-derived (Parseval) and against the integral (test_path) | keep |
| Hall 2000 | K_b, K_f | ch. 3 (usual) | K_b = (L_m/L + C_m/C)/4, \|K_f\| = \|L_m/L − C_m/C\|/2 | weak coupling, matched | Verified (content) | keep |
| CODATA 2018 | ε0, μ0 | Rev. Mod. Phys. 93, 025010 | ε0 = 8.8541878128e-12; μ0 = 4π × 10⁻⁷ (5.5e-10 relative) | – | Verified | keep |
| IEC 60028 | ρ = 1/58 Ω·mm²/m, α = 0.00393 /K at 20 °C | the standard (1925) | as the standard's values (widely reproduced) | annealed copper; ED / plated a few % higher (A27) | Verified (content) | keep, limitation added (A27) |
| Hammerstad 1980 | microstrip Z0, εeff with thickness | eqs. (usual) | as published | w/h ≥ 0.3 checked | Verified (content): 0.8 / 1.8 % against the field solver (SP-2) | keep |
| Wheeler 1978 | stripline Z0 with thickness | eqs. (usual) | as published | w/b 0.1-4 | Verified (content): 0.5 % (SP-2) | keep |
| Cohn 1954 / 1955 | exact zero-thickness (coupled) stripline | elliptic integrals | as published | the cross-section solver's check | Verified (content): test_xsection | keep |
| Wadell 1991 | stripline formulas, offset stripline | handbook | as collected there | offset ≥ 0.35 within 1.5 % | Verified (content): SP-14 | keep |
| Desoer 1969, Tellegen 1952 | superposition, Thévenin, Σ V·I* = 0 | theorems | as published | linear networks | Verified: DC-1, DC-2, TW-3 (heat = loss) | keep |
| Paul 2006 | trapezoid spectrum; \|E\| = 1.316e-14 f²\|m\|/r | ch. 3, 8 (usual) | as published | far field, small loop | Verified (content): SP-1, SP-5 | keep |
| Balanis 2016 | small loop with the induction term | §5.2 | \|E\| ∝ k²\|m\|√(1 + 1/(kr)²)/r | small loop | Verified (content): SP-5 | keep |
| Haykin 2001, Hardin 1994, Papoulis 2002 | FM Bessel lines; spread clocks; Rayleigh envelope | textbook results | as published | – | Verified (content): test_spread, CV-25, CV-30 | keep |
| Middlebrook 1976 | input-filter stability | criterion | \|Z_out,filter\| ≪ \|Z_in\| = V²/P | – | Verified (content): CV-16 | keep |

### Power electronics, numerics

| Key | Supports | Status | Action |
|---|---|---|---|
| Erickson 2001 | buck / boost steady state, small signal, CPM [ch. 12], loss models | Verified (content): CV-1, CV-11, CV-13, CV-23, CV-24 against a switching simulation | keep |
| Ridley 1991 | CPM sampling double pole, Q_p = 1/(π(m_c D′ − ½)) | Verified (content): CV-14, CV-24 | keep |
| McMurray 1972, Ogata 2010 | RC snubber around √(L/C); second-order overshoot exp(−πζ/√(1 − ζ²)) | Verified (content): CV-3 (RK4) | keep |
| Smith 1999, Gustavsen 1999 / 2006 | target impedance; vector fitting | Verified (content): test_pdn, test_vectors | keep |
| Krishnan 2010 | six-step BLDC drive, star equivalent of delta | Verified (content): test_bridge | keep |
| Ho 1975, Benzi 2005, Saad 1986 / 2003, Giraud 2005, Woodbury 1950, Demmel 1999, Liu 1985 | MNA; saddle point and Schur preconditioner; GMRES; CGS2; low-rank update; SuperLU; MMD | Verified (identity; the methods as used): GMRES stops on the true residual (test_solver), the Woodbury update against a full factorisation (test_speed) | keep |
| Oppenheim 2010 / 1997, Eymard 2000, Kabsch 1976, Rosenbrock 1970, Press 2007, Dijkstra 1959, Hestenes 1952, Vanek 1996, Ruge 1987, Bell 2022, Abramowitz 1964 | FFT convolution, Fourier series, finite volumes, rigid fit, pencil zeros, Newton, shortest path, CG, AMG, Gauss-Legendre | Verified (identity; the methods as used) | keep |
| Foley 1990 | scanline polygon fill (even-odd, half-open edges) | Verified: `_line_cover` equal to shapely's intersections to 1e-9 mm (TW-9) | new |
| Kreyszig 2011 | Green's theorem, area = ∮ x dy | Verified: `_cell_areas` equal to shapely's cell intersections to 1e-9 mm² (TW-9) | new |

### Standards

| Key | Supports | Location | Implemented vs source | Status | Action |
|---|---|---|---|---|---|
| CISPR 32:2015+A1:2019 | radiated limits A / B; conducted average (AC mains) | Annex A tables | 30 / 37 dBµV/m at 10 m (B), 40 / 47 (A); 50 / 54 and 56 / 60 at 3 m above 1 GHz; AV 56→46, 46, 50 dBµV (B), 66, 60 (A) | Verified (values agree with the published limits; the standard's text was not re-read this round) | keep |
| CISPR 16-1-1 | RBW 200 Hz / 9 kHz / 120 kHz / 1 MHz | band table | as published | Verified (content) | keep |
| CISPR 16-1-2 | AMN 50 Ω ∥ (50 µH + 5 Ω) | V-network | as published | Verified (content) | keep |
| CISPR 25 | 5 µH AN, voltage-method limits by class | limit table | class 5 average values and steps as commonly published | **Partly verified** (2026-10-20): the class 5 peak limits of all bands but TV band I are the code's averages + 20 dB (TI SLLA569B, Table 3), as the standard's tables are laid out; the average column and the edition not seen (A10) | keep the A10 warning |
| MIL-STD-461G / H | CE101, CE102, RE102, LISN, Table II | figures and tables named | as the standards | Verified against the PDFs (rounds 2-3, `Downloads\GroundTruth audit sources`) | keep |
| IPC-A-610 | 75 % vertical fill of a PTH (class 2/3) | acceptance criteria | as commonly specified | Verified (content); edition and clause unverified | keep |
| CDA alloy data | % IACS of brass, phosphor bronze, BeCu, Te-Cu | alloy pages | 28 / 15 / 22 / 93 % IACS → the code's resistivities within 1 % | Verified (content) | keep |
| IPC-2221B | current-capacity fit; Table 6-1 spacing | §6.2, Fig. 6-4; Table 6-1 | via: I = 0.024·ΔT^0.44·A^0.725 as the fit; spacing B1/B2/B4 as the table | **Incomplete → corrected**: the entry now says the equation is a curve fit to Figure 6-4 (whether §6.2 prints it is unverified; secondary sources differ) and that IPC-2221B defers to IPC-2152 for conductor sizing; Table 6-1 verified against independent reproductions. Second attempt (2026-10-20): the copies of the standard found online returned 403 or failed their TLS check (not bypassed); calculators citing IPC-2221A §6.2 agree on k = 0.048 / 0.024 and the exponents | documentation corrected |

### Heat transfer

| Key | Supports | Location | Implemented vs source | Status | Action |
|---|---|---|---|---|---|
| Incropera 2011 | plate correlations, A/P, forced flat plate, radiation, enclosure | ch. 7, 9, 13 (usual) | the code's equations are the textbook's | Verified (content) for the hot-face-up, vertical, forced, radiation and enclosure forms; the hot-face-down 0.52 Ra^⅕ **unverified** (edition-specific, as remembered; the 6th edition's 0.27 Ra^¼ is 8-36 % lower at Ra 10⁵-10³; a second search found only the 6th edition's form and the 1/5-power data behind the newer one, Radziemska and Lewandowski 2001: 0.68 Ra^⅕ on a disc's diameter, which does not settle the coefficient on A/P); stated from Ra ≈ 10⁴, extrapolated below | entry completed (validity range, edition) |
| Churchill 1975 | vertical plate, full range | Int. J. Heat Mass Transf. 18 | Nu = {0.825 + 0.387 Ra^⅙/[1 + (0.492/Pr)^(9/16)]^(8/27)}² | Verified (content) | keep |
| Churchill 1977 | mixed convection Nu³ = Nu_F³ + Nu_N³ | AIChE J. 23 | n = 3 | Verified (content) | keep |
| Sutherland 1893 | μ(T) law | Phil. Mag. 36 | μ0 (T/T0)^1.5 (T0 + S)/(T + S) | Verified | keep |
| White 2006 | air's Sutherland constants | ch. 1 table | code had 273.15 K / 110.4 K (U.S. Standard Atmosphere), cited to White, whose table gives 273 K / 111 K | **Incorrect → corrected** (code uses White's; 0.05 % at 300 K) | corrected |
| Patankar 1980 | finite-volume conduction, face conductances | ch. 4 (usual) | harmonic face conductances | Verified (content): fin (tests/thermal), TW-10 | keep |

### Assumptions

The assumptions A1-A44 are in [REFERENCES.md](REFERENCES.md) with their reasons and evidence. This round changed:
A2 (the exact one-sided slab instead of δ(1 − e^(−t/δ))), A3 (the barrel's π·d·t reading stated), A10 (the CISPR 25
table's peak limits checked), A27 (emissivities, ED copper's conductivity, the stackup's copper thickness), and added
A39 (exact widths and areas; a narrow band's change of width in series, the wider bands' step error), A40 (the
sustained density; its limits are rules of thumb), A41 (resin slabs, one h per face, the map's maximum), A42 (current
sinks, ideal source, 20 °C drops; the paths at temperature after Thermal), A43 (the ripples' and the barrels' heat,
in Thermal since 2026-10-19), A44 (a node's heat in its own copper). Each is cited where the code applies it
(`tools/dev/ref_keys.py`: 115 keys cited, 115 listed, none unknown).

## 4. Track width: 0.4 → 0.1 mm

The example: a 1 oz (35 µm) copper track on F.Cu carrying 1 A, narrowed from 0.4 to 0.1 mm, everything else the same.
Copper at 20 °C: ρ = 1.7241 × 10⁻⁸ Ω·m [IEC 60028]. Before / after: `main` (68b85af) against this branch, from
`tools/dev/track_width_check.py` (`out/audit5/track_width_main.txt`, `track_width_new.txt`).

### 4.1 Geometry

- **Storage and units.** A track's width is stored in mm as KiCad writes it (`(width 0.1)` in the file; kipy's nm ×
  1e-6), with no snapping, rounding or minimum (`file_source._track`, `kipy_source`). Its copper is the centreline
  buffered by w/2 with round ends (`geometry.shapes.track_shape`), unioned with the net's other copper on that layer
  (`CopperMap.get`). Inside the solver lengths are metres (`d·1e-3`).
- **What the solver consumes.** The grid's strips (one per cell border) and nodes (one per cell). Before this round a
  strip's width was the number of sub-grid samples (0.1175 mm apart on kraft's DC model at 1x) that crossed the
  border, at least the track's own width where the track's walk forced the strip. That made the width depend on where
  the track lay: a 0.1 mm track read 0.1175 mm (one sample) or 0.159 mm (the forced row plus a sampled neighbour row:
  the copper counted twice); a 0.05 mm track 0.1175 mm. A track at an angle had strips as wide as the track, where
  the border it crosses is w/cos θ long. Now each strip's width is the exact length of its border covered by its net's
  copper (`peec._exact_copper`), and each node's area the exact copper in its cell.
- **Is the edited geometry what is solved?** Each Simulate builds its models from the board it is given (a model is
  shared within one Simulate only; the fix search reuses its baseline's on purpose). The CLI reads the saved file.
  The plugin simulated the board as it was last read (on opening the window, or with Reload board): a width changed
  in KiCad afterwards was not seen until Reload board (ISSUE-19). Since 2026-10-20 Simulate first reads the board
  again (in the job: the window stays responsive) and compares its fingerprint (`Board.fingerprint`, ~0.1 s on kraft)
  with the one it has; a changed board is loaded before the solve (a window test narrows a track behind the window's
  back: its drop ×4).
- **Joins, bends, vias, overlaps.** One segment, two collinear segments and a zone of the same shape give the same
  drop to 0.01 mV (48.42 mV at 0.4 mm, 192.87 mV at 0.1 mm, 40 mm between two 1 mm pads). A right-angle corner where
  the width changes (TW-5), a track through a via (TW-6) and a neck (TW-4) agree with independent sheet solves.
- **Disconnection.** A track broken by a 0.15 mm gap was solved as whole (the second piece's walk took the first's
  cell). Now cells join only within one connected piece of the net's copper: the rail is reported as not connected
  (TW-7, ISSUE-15).
- **Cross-section.** w × t, t the layer's copper thickness in the stackup (A27: it should be the finished thickness;
  an outer layer entered as 35 µm but plated to 55 µm reads its tracks' resistance 57 % high).

### 4.2 Resistance

For a uniform track, R' = ρ/(w·t): **1.2315 Ω/m at 0.4 mm and 4.926 Ω/m at 0.1 mm, a ratio of exactly 4**. What the
solver gives (R' from a 40 and a 20 mm track, so the pads cancel; three positions on the grid, two angles):

| w mm | angle | position | R'/exact, `main` | R'/exact, now |
|---|---|---|---|---|
| 0.4 | 0° | 3 positions | 1.134 / 1.134 / 0.851 | 0.999 / 0.999 / 0.999 |
| 0.4 | 30° / 45° | – | 1.571 / 1.203 | 1.003 / 0.998 |
| 0.1 | 0° | 3 positions | 0.851 / 0.851 / 0.851 | 0.999 / 0.999 / 0.999 |
| 0.1 | 30° / 45° | – | 1.375 / 1.413 | 1.004 / 0.998 |
| 0.05 | 0° | 3 positions | 0.425 / 0.425 / 1.000 | 0.999 / 0.999 / 0.999 |

So the 0.1 / 0.4 mm ratio read anywhere from 2.2 to 4.5 on `main` (by angle and position) and reads 4.00 ± 0.5 %
now. Over the whole sweep (0.05-0.4 mm, 0°, 30°, 45°, three positions) R' is within 1 % (section 6).

**The segment is not the path.** Only the narrowed piece's resistance quadruples. Between two 1 mm pads, 40 mm of
track gives 48.42 against 192.87 mV at 1 A (3.98 ×: the pads' share does not change). A 2 mm, 0.1 mm neck in a 30 mm,
0.4 mm track adds 6.36 mΩ (the independent sheet solve) to the track's 36.9 mΩ: +17 %, not × 4, and less than the
neck's own 20 squares minus the 5 it replaces (7.4 mΩ) because the 0.4 mm track's round ends reach into it.

**Remaining discretisation error.** A strip took its border's width along its whole length (a two-point flux
[Patankar 1980]), so an abrupt change of width sat within ±d/2 of where it is: up to (d/2)·(1/w₁ − 1/w₂) squares per
step, the 2 mm neck −9.1 / −4.8 / −0.3 % at 1x / 2x / 4x (ISSUE-21). Now a narrow band of strips (at most 4 side by
side, alone on its border line) takes its copper's width at 6 lines along its length in series, which places the step
to d/12, for the resistance only (A39). The neck against the independent sheet solve (`tools/dev/neck_check.py`, the same
boards as TW-4):

| neck | 1x (0.25 mm) | 2x (0.177) | 4x (0.125) | 16x (0.0625) |
|---|---|---|---|---|
| 2 mm, before | −9.1 % | −4.8 % | −0.3 % | −1.7 % |
| 2 mm, now | −2.9 % | −2.6 % | −0.7 % | −1.5 % |
| 5 mm, now | −1.0 % | −0.9 % | −0.25 % | −0.5 % |
| 20 mm, now | −0.25 % | −0.22 % | −0.06 % | −0.13 % |

The remainder is the same in mΩ for every neck length at a resolution (0.18 / 0.17 / 0.04 / 0.09 mΩ: 0.37 / 0.34 /
0.09 / 0.19 square for the two ends together): the current's 2D turn into the neck at the grid's resolution and the
reference's own 1 %, not the step's position. A band wider than 4 strips (a pour, a track wider than 4 cells) and a
junction keep the border's width, so their steps still sit within ±d/2. The first version took every band in series:
the strips across a track form a band as long as the track, whose sample lines past its edge find only its pads, and
read a quarter of their width (a straight 0.4 mm track's density +5 %, a neck island's resonance +2 %; TW-1 and SP-27
caught it before release).

**Temperature.** ρ(T) = ρ20·(1 + 0.00393·(T − 20)) [IEC 60028]. The thermal solve heats the copper with it (A15); the
DC tables' drops and resistances are at 20 °C (A42: 0.39 %/K more on a warm board), and after Thermal its result
lists every DC path at its temperature (ISSUE-23): R₂₀ times the mean of 1 + α(T − 20) over the path's own heat. The
0.2 mm track of TW-15 at 2 A: its heat-weighted copper temperature equals the map's mean along the track within 1.5 K,
5 K below its middle.

### 4.3 Current density

J = I/(w·t): **71.4 A/mm² at 0.4 mm, 285.7 A/mm² at 0.1 mm (4 ×) at 1 A.** The solver's density in the middle of a
straight track now equals it (1.000 at every width and position; `main` 0.43-1.28). The sustained density (the
check's: an RMS over 1 mm of the net's copper) reads 1.01-1.08 × it (the ends near the pads are in the window); on
`main` 0.43-1.63. Node by node along an angled track the density varies ±25 % around I/(w·t) (the staircase's x and
y strips carry the current's components at different nodes); the 1 mm window evens that out to within 8 %. Peaks
above I/(w·t) at the pads and corners (1.1-1.5 ×) are the current crowding into and out of the track, which a finer
grid resolves further (a re-entrant corner's density is singular in 2D).

The check's limits (50 / 100 A/mm² by default) are rules of thumb (A40): a 0.1 mm track at 1 A is at 286 A/mm²
(an error), and what that does to its temperature is section 4.5's question, which depends on the board, not on J.

### 4.4 Voltage drop and power: the boundary conditions

GroundTruth's DC loads draw their set current whatever the drop (current sinks), from an ideal source pin (A42). So:

| Quantity of the narrowed track | Fixed current I (GroundTruth's DC loads) | Fixed voltage V across the track (not modelled) |
|---|---|---|
| R | × 4 | × 4 |
| I | × 1 | × ¼ |
| J = I/(w·t) | × 4 | × 1 |
| V = I·R | × 4 | × 1 |
| P = I²R = V²/R | × 4 | × ¼ |

With a source resistance R_s in series and the track's R: I = V/(R_s + R), so I falls by (R_s + R)/(R_s + 4R) and the
track's power changes by 4·((R_s + R)/(R_s + 4R))², between × 4 (R ≪ R_s) and × ¼ (R ≫ R_s). A real rail is a
regulator holding its output with loads that draw what they need: close to the fixed-current case while the drop is
a small fraction of the rail (at a 3 % drop a resistive load would draw 3 % less). Verified: at a fixed 1 A the
track's drop and loss differ by (R'₀.₁ − R'₀.₄)·39 mm within 2 % (TW-2), and the load's current is the set one in
both. The path's own dissipation is reported (`loss_w`); the narrowed segment's share follows from R'.

### 4.5 Thermal behaviour and allowable current

**The model** (`analysis/thermal.py` on `thermal/solver`): steady state; the DC paths' copper heat (now exactly
their I²R, ISSUE-12) with ρ(T) iterated to convergence; 3D finite-volume conduction through the stack (copper with
k = 390 W/(m·K), laminate k_xy / k_z, resin 0.3 W/(m·K) in copper layers' empty areas), adaptively refined where heat
enters; convection on both faces and the edges from correlations (or fixed h), linearised radiation. It computes a
temperature; it is not an empirical current rule. IPC-2221's chart is not used for tracks (only for a via's barrel,
A17).

**Independent check.** A 100 mm track across a 60 mm wide 2-layer board (1.51 mm FR-4), h = 10 W/(m²·K) on both
faces, no radiation, 25 °C, 1 A: the solver's temperature at the middle of the track against the 2D finite-difference
cross-section of the same board (`refsolvers.cross_section_rise`, converged to 0.2 %), with ρ(T):

| w mm | reference rise | `main`: solver / map | now: solver / map |
|---|---|---|---|
| 0.4 | 5.63 K | 5.32 (−6 %) / 5.32 | 5.71 (+1.4 %) / 5.71 |
| 0.3 | 7.85 K | 9.54 (+21 %) / 9.27 | 7.90 (+0.6 %) / 7.90 |
| 0.2 | 12.56 K | 9.81 (−22 %) / 9.13 | 12.38 (−1.4 %) / 12.38 |
| 0.15 | 17.45 K | 10.34 (−41 %) / 9.49 | 17.71 (+1.5 %) / 17.71 |
| 0.1 | 28.45 K | 28.92 (+2 %) / 24.59 (−14 %) | 28.92 (+1.7 %) / 28.92 |
| 0.075 | 40.47 K | 40.07 (−1 %) / 34.06 (−16 %) | 40.07 (−1.0 %) / 40.07 |
| 0.05 | 68.30 K | 65.18 (−5 %) / 55.37 (−19 %) | 65.18 (−4.6 %) / 65.18 |

`main`'s errors came from the heat (each board's own width errors, section 4.2) and, for the map, from the cell
means (ISSUE-14). The 0.05 mm track is narrower than normal quality's finest thermal cell here (0.075 mm): its heat
spreads from a cell half again as wide as the track, which reads it 4.6 % cool. At thermal.quality fine (0.0375 mm
cells) it reads +1.2 to +1.5 % along its whole length (`out/audit5/thermal_narrow.txt`).

**The mesh's budget (ISSUE-24, documented).** Normal quality refines within 6 × 10⁵ extra unknowns, the worst blocks
first. A 100 mm track of 0.05 or 0.1 mm used it up 70 mm along: the last 30 mm kept 0.15 mm cells and read 12 % (0.05
mm) and 5.4 % (0.1 mm) cool there, while the note estimated "up to 2.1 K" (it was 8 K). The tests' points (the
middle) are in the refined part. Refining only the hot blocks instead of each with its eight neighbours covers the
whole track in the same budget at the same accuracy there, but on kraft it read the hottest parts 1.5-3 K cooler
than fine quality (Q303 122.7 against 126.0 °C; the eight neighbours resolve a FET's heat spreading into its pour), so
the planner is unchanged: a board with long narrow hot tracks needs fine quality (it has the note).

**Heat beside the copper (ISSUE-22, fixed).** The DC grid gives a cell at the copper's edge a node when one of its
sub-grid samples is copper, so the node's own point can lie up to about d/4 outside the copper. Its heat went to the
thermal cell under that point: on a thermal mesh finer than the DC grid, a resin-only cell (0.3 W/(m·K)). A 0.4 mm
track 0.01 mm off the grid at 3 A, fine quality: hot spots beside the track 36 % (20 K) above the track itself; at 1 A
on the 100 mm board +20 %. The node's heat now goes to the nearest point of its own copper (`peec._heat_points`, A44):
the hottest copper is the track's own temperature (TW-14, 1.000; on the 100 mm board 0.993 of the reference). At
normal quality's coarser cells the effect was smaller (+2.9 % on TW-14's board at 1 A).

**0.4 → 0.1 mm at 1 A: the rise grows 5.06 ×** (5.71 → 28.92 K; the reference 5.05 ×): the heat 4 ×, the narrower
track's local spreading resistance + 16 % (the cross-section's rise per W/m: 5.10 against 4.39 K·m/W, the board's own
spreading, 3.17 K·m/W, being the same for both), ρ(T) + 9 % more at the hotter track. Not the 4 × of the heat alone,
and not IPC-2221's chart either: its fit I = 0.048·ΔT^0.44·A^0.725 gives 6.2 and 61 K for these two tracks (9.8 ×), a
far steeper dependence on the cross-section than a board conducting heat away from a narrow track has (the reason
IPC-2152 replaced that chart).

**Allowable current.** Thermal's Current capacity to 105 °C (80 K over ambient) on the same board
(`tools/dev/track_capacity_check.py`): **3.25 A at 0.4 mm and 1.53 A at 0.1 mm**, against 3.33 and 1.54 A from the
reference cross-section with ρ(T): within 2.4 %, on the safe side. Narrowing to a quarter divides the capacity by
2.1 here (IPC-2221's fit: 4^0.725 = 2.7). These figures are for this board and cooling; GroundTruth computes them for
the real one.

### 4.6 Regression tests

tests/test_track_width.py, each against an independent expectation:

| Test | Case | Expectation |
|---|---|---|
| TW-1 | uniform track, 0.4 / 0.1 / 0.05 mm, three positions | R' = ρ/(w·t) within 1 %, J = I/(w·t) within 2 % |
| TW-2 | 0.4 → 0.1 mm at a fixed 1 A | R' 4.00 × within 1.5 %; drop and loss differences = ΔR'·L·I within 2 %; J 4.00 × |
| TW-3 | 30° and 45°, 0.4 / 0.15 / 0.1 mm | R' within 1.5 %; heat = loss within 0.5 %; sustained J within −3 / +8 % |
| TW-4 | a short (2 mm) and a long (20 mm) neck | against the sheet solve: a quarter square per end; the short one within 4 %, the long one within 0.5 %; J in and beside the neck |
| TW-5 | a corner where the width changes | the sheet solve within 2 % |
| TW-6 | track, via, track | the sheet solves + the barrel within 2 % |
| TW-7 | a 0.15 mm gap | not connected (an error) |
| TW-8 | another net's pour 0.2 mm away | sustained J unchanged within 1 % |
| TW-9 | border lengths and cell areas | shapely's to 1e-9; each net's area kept within 0.5 % |
| TW-10 | 100 mm track's temperature, 0.4 / 0.1 mm | the 2D cross-section within 3 %; their ratio within 3 % |
| TW-11 | the temperature map | the solver's own temperature within 2 % |
| TW-12 | two fills touching (0.0000 mm) vs a 0.02 mm gap | one piece (the same drop as one zone) vs not connected |
| TW-13 | a via's barrel in Thermal | its heat ρ·L/(π·d·t)·I², half at each layer |
| TW-14 | an edge node outside its copper, fine quality, 3 A | every heat sample on the copper; the hottest copper the track's own temperature within 2 % (was +36 %) |
| TW-15 | the DC path after Thermal | R, drop, loss × (1 + α(T − 20)), T within 1.5 K of the track's mean temperature |

tests/test_physics_audit.py: PA-1 (the slab's surface impedance), PA-2 (plates through two dielectrics), PA-3 (air's
μ, k, Pr), PA-4 (the hot loop's switching current I_L·√(D(1−D)) and its heat).

## 5. Issue log

| ID | Area | Severity | Status |
|---|---|---|---|
| ISSUE-11 | PEEC strip widths from the sub-grid's samples | High | Fixed, verified |
| ISSUE-12 | Copper heat ≠ the solved I²R | High | Fixed, verified |
| ISSUE-13 | Sustained density diluted by another net's copper | Medium | Fixed, verified |
| ISSUE-14 | Temperature map: cell means | Medium | Fixed, verified |
| ISSUE-15 | Gaps narrower than a cell bridged | Medium | Fixed, verified |
| ISSUE-16 | Skin effect formula (A2) | Medium-Low | Fixed, verified |
| ISSUE-17 | Plate capacitance through mixed dielectrics | Low | Fixed, verified |
| ISSUE-18 | Three references (White, IPC-2221, Incropera) | Low | Corrected / documented |
| ISSUE-19 | Plugin: Simulate uses the board as last read | Medium | Fixed, verified (2026-10-20) |
| ISSUE-20 | Heat Thermal left out: via barrels, the hot loop, the inductor's ripple | Medium | Fixed, verified |
| ISSUE-21 | A change of width placed anywhere within ±d/2 | Medium-Low | Fixed for narrow bands, verified; wider bands documented |
| ISSUE-22 | An edge node's heat in the resin beside its copper | Medium (fine quality: High) | Fixed, verified |
| ISSUE-23 | DC drops and resistances only at 20 °C | Low | Added: the paths at temperature after Thermal |
| ISSUE-24 | Thermal mesh budget on long narrow tracks; the note's estimate low | Medium-Low | Documented (fine quality); planner unchanged |

Found and fixed inside this branch before release (the real board's before / after comparison and the tests caught
them; not in `main`): one net's fills touching at 0.0000 mm split into separate pieces by the piece-aware grid (a
converter's input path cut on kraft; `PIECE_TOL_MM`, TW-12); the combined DC view's loss weighted by whole cells
instead of the copper area and without the vias' share (test_power: equal to Thermal's heat within 0.5 %); a motor
bridge's copper heat without its vias; the width-step series factor applied to every band (ISSUE-21).

**ISSUE-11: strip widths.** *Pathway:* `build_grid` → `_strip_widths` → `PlaneStack._widths` (R = R_s/φ, the narrow
strip's self inductance, side-by-side mutuals) → every model's DC and AC solve, the densities, the heat. *Reproduce:*
`tools/dev/track_width_check.py --src <main>`: R'/exact 0.43-1.81 (section 4.2). *Root cause:* the width was counted
in sub-grid samples (±1 sample, 0.1175 mm on kraft's DC model), raised to the track's own width only on the row its
walk forced, so a neighbouring sampled row counted the same copper again; and an angled track's strips took the
track's width instead of the border's (w/cos θ). *Fix:* exact border coverage per net by a scanline on each border
line (`_line_cover`), copper crossing where its net has no strip moved to the nearest strip on that line
(`_adopt`, 3 cells), widths may exceed one pitch where a strip takes in such a sliver. *Evidence:* TW-1, TW-2, TW-3,
TW-9; the earlier rounds' SP-27 (neck widths) and all solver tests pass.

**ISSUE-12: heat.** *Pathway:* `Solution.node_field(copper=True)` × `Grid.copper_area` → `thermal.copper_heat`,
`bridge_rms`. *Reproduce:* thermal_inputs' heat over the path's own loss: 1.11 (0.4 mm, straight, 1x), 1.41-1.99
(30-45°) on `main`. *Root cause:* a node's density was the mean of its strips' current densities and its area their
mean width; ρ·J²·t·A is then not the strips' I²R once the strips differ (a staircase's corner, a junction, a wide
track's edge rows). *Fix:* the node's magnitude from its strips' heat over its exact copper area (direction from the
mean), the area shared out over 3 × 3 like the strips along the node's run. *Evidence:* heat = loss within 0.5 %
(TW-3; 0.999-1.000 measured on all 35 cases).

**ISSUE-13: sustained density.** *Root cause:* `sustained` averaged over every copper cell in the 1 mm window,
another net's included (at 0 A/mm²): a 0.1 mm track 0.2 mm beside a GND pour read 0.57 × (1x) / 0.42 × (4x) of its
density on `main`, a third below its value alone. *Fix:* the RMS over the node's own net, weighted by area.
*Evidence:* TW-8.

**ISSUE-14: temperature map.** *Root cause:* the level-0 map was the area mean of its refined cells: a 0.1 mm track
on 0.3 mm cells read 14-19 % cool (t_max and the issues always used the refined cells). *Fix:* each cell shows its
hottest refined cell (`Mesh.to_level0(how="max")`). *Evidence:* TW-11.

**ISSUE-15: gaps.** *Root cause:* topology by net: a cell, its strips and the track walk knew only the net, so two
pieces of one net's copper met in any cell they shared. *Fix:* the pieces of each net's union label the sub-grid; a
cell keeps one piece; strips, seeds and the walk join only within a piece. *Evidence:* TW-7 (0.15 mm gap: not
connected; `main`: 143.9 mV as if whole).

**ISSUE-16: skin effect.** *Root cause:* A2's t_eff = δ(1 − e^(−t/δ)) has the right limits but not the right
transition: 28 % too resistive at 1 MHz, 58 % at 3.6 MHz, 34 % at 10 MHz in 35 µm copper. *Fix:* the exact one-sided
slab [Dowell 1966] in `kernel.skin_thickness`, used by the strips, bridged tracks, a route's trace and the path
lines. *Evidence:* PA-1 (the slab's surface impedance to 1e-6). *Reach:* the copper's loss in the conducted band and
the low-MHz damping of plane resonances; above ~50 MHz (Δ > 3.5) unchanged within 3 %.

**ISSUE-17: plate capacitance.** *Root cause:* `epsilon_between`'s thickness-weighted mean εr for parallel plates,
whose dielectrics are in series. *Fix:* `Stackup.epsilon_series` / `loss_tangent_series` for the plane pairs and the
victims' plates (the line closed forms keep the mean). *Evidence:* PA-2; 0.1 % on FR-4's prepreg / core, 1.6 % on a
3.5 / 4.5 hybrid.

**ISSUE-18: references.** White 2006's constants (code: 110.4 K at 273.15 K, the U.S. Standard Atmosphere's; White:
111 K at 273 K; corrected in code, 0.05 %); IPC-2221B's current equation (a curve fit to Figure 6-4, superseded by
IPC-2152 for conductor sizing; documented); Incropera's heated-face-down correlation (edition-specific, unverified;
validity range from Ra ≈ 10⁴ added).

**ISSUE-19: the board the plugin simulates.** Simulate used `MainFrame.board`, read when the window opened or at
Reload board; nothing checked whether KiCad's board had changed since. A track narrowed in KiCad and simulated
without Reload board gave the old track's results. *Fix (at "fix all of it"):* Simulate first reads the board again
in a job (KiCad's API in live mode, the file otherwise; the window stays responsive) and compares
`Board.fingerprint()` (a SHA-256 of the stackup, nets, tracks, vias, pads, zone fills, parts with their fields and
the outline, to 1 nm; ~0.1 s on kraft) with the loaded board's; a
changed board is loaded as Reload board would, then the Simulate runs on it; a board that cannot be read just then
(KiCad busy) is analysed as read, and the status bar says so. *Evidence:* the window test narrows a
track in the file behind the window's back and simulates: the drop ×4 (`test_gui`). A live-KiCad test was not run
(the API read is the one Reload board has always used).

**ISSUE-20: heat left out of Thermal.** *Root cause:* the copper heat was the DC paths' (the average currents and the
switch node's two-state variance); the input capacitors' switching current in the hot loop (no mean, RMS
I_L·√(D(1−D))), the inductor's triangular ripple (ΔI²/12) and the via barrels' I²R were in no heat (A43 said so).
*Fix:* the ripple's variance on the switch-node path (`dc_summary(extra_var)`), the hot loop solved as a path of its
own from the rail capacitors (their admittance shares at f_sw) to the switches, and every barrel segment's
R·(I² + variance) at its two layers (`_via_segments`). *Evidence:* CV-28 (a buck's loss with its ripple), PA-4 (1.218
A RMS, its heat = its loss), TW-13 (a barrel's heat), test_power (all stages' heat = the combined loss within 0.5 %).

**ISSUE-21: width steps.** *Root cause:* a strip took its border's width over its whole length (section 4.2): a
step anywhere within ±d/2, −9 % of a 2 mm neck at 1x. *Fix:* a narrow band's width at 6 lines along its strips in
series (`peec._series_factor`), for the resistance only; the inductance keeps the border's width (its kernels have
no wider strip: a straddling neck's resonance moved 2.2 % when they took it, SP-27). *Evidence:* TW-4 (a quarter
square per end), the table in 4.2; TW-1, TW-3, SP-27 unchanged.

**ISSUE-22: heat beside the copper.** *Root cause:* section 4.5: an edge node's own point, up to d/4 off its copper,
got its heat; a finer thermal mesh put it in resin. *Fix:* `Grid.hx / hy`, the offset to the nearest point of the
node's own net's copper (`peec._heat_points`), used by `thermal._layer_currents` [A44]. *Evidence:* TW-14 (+36 % →
1.000).

**ISSUE-23: DC figures at temperature.** The DC tables stay at 20 °C (A42) and say so; after Thermal the result lists
each path at its solved temperature (`thermal.dc_at_temperature`, also the plugin's and a compute server's Thermal:
computed where the stages' maps are) with a note on the largest changes, and the JSON report's
`dc_at_temperature`. The CLI's JSON report had no thermal result at all (`ThermalResult.summary` was written for it
but never called): `simulate --thermal / --capacity --format json` now holds `thermal` and `capacity`.
*Evidence:* TW-15 (also the JSON).

**ISSUE-24: the thermal mesh's budget.** Section 4.5. Documented, with the evidence for keeping the planner.

## 6. Benchmarks

| Benchmark | Independent method | Result |
|---|---|---|
| Track R', 0.05-0.4 mm, 0° / 30° / 45°, 3 positions (35 cases) | ρ/(w·t) | within 1 % (was 0.43-1.81 ×) |
| Track J at the middle, straight (21 cases) | I/(w·t) | 1.000 (was 0.43-1.28) |
| Copper heat vs the solved loss (35 cases) | Tellegen: Σ R·I² | 0.999-1.000 (was up to 1.99) |
| Neck (2 / 5 / 20 mm) at 1x / 2x / 4x / 16x, corner, via | finite-difference sheet solve (h 0.01 mm, converged to 1 %) | corner and via within 2 %; necks −2.9 / −1.0 / −0.25 % at 1x (were −9.1 / −3.3 / −0.7 %), a fixed ≤ 0.19 square per end at every resolution |
| Border lengths, cell areas | shapely's exact intersections | 1e-9 mm, 1e-9 mm² |
| Same copper drawn three ways | each other | identical to 0.01 mV |
| Track temperature, 0.05-0.4 mm | 2D finite-difference cross-section with ρ(T) (converged 0.2 %) | within 1.7 % for 0.075-0.4 mm where refined, −4.6 % at 0.05 mm (thinner than normal's finest cell; fine: +1.2 to +1.5 %); part of a long narrow track left coarse by normal's budget −5 to −12 % (ISSUE-24) |
| Edge node outside its copper, fine quality | the track's own temperature | hottest copper 1.000 × it (was 1.36 × at 3 A, 1.20 × at 1 A) |
| A DC path's R at temperature | 1 + α(T − 20) at the map's mean along the track | within 1.5 K of the track's mean temperature (TW-15) |
| Track current capacity, 0.4 / 0.1 mm | the same cross-section solved for the limit | −2.4 % / −0.8 % |
| AC resistance of a foil, 1 kHz-10 GHz | the slab's surface impedance (k/σ)coth(kt) | 1e-6 |
| Plane pair through two dielectrics | series capacitors | 1e-6 |
| Air μ, k, Pr at 300 K | Incropera's Table A.4 | 0.05 %, 1.1 %, 1.1 % |
| Thermal reference solver itself | its own refinement; the thin-plate fin limit | 0.2 %; the fin's 3.17 K·m/W plus a local constriction that shrinks with the width |
| Sheet reference solver itself | h = 0.02 against 0.01 mm | within 1 % |

Numerical error and model uncertainty, apart: the grid's remaining error is a narrow band's fixed ≤ 0.19 square per
step and a wide band's step placement (±d/2, converging with resolution, A39), and the thermal mesh's budget
(reported in a note, whose estimate can be low by 4 × on long narrow tracks: ISSUE-24); the model uncertainties are
the material constants (A27: the stackup's copper thickness, ED copper's conductivity, laminate k), the convection
correlations (A41: ±10-30 % on h for small boards and the heated face down) and the steady state.

## 7. The real board: kraft_flux_mini_2

kraft_flux_mini_2 as the user's copy (`Downloads\flux-2`, 6 layers, read only) with its scratch settings
(`out/kraft_refresh/kraft_all.groundtruth.yaml`: 14 nets, 4 power stages), `main` (68b85af, a separate worktree)
against this branch with every fix, CPU, normal thermal quality. Outputs in `out/audit5/` (`kraft_main/`,
`kraft_final/`, `kraft_compare_final.txt`, `kraft_dc_thermal_*.txt`, `topology_diff_final.json`).

**Topology.** The whole-board DC model joins the same pads in both (`tools/dev/topology_diff.py`: 351 pads, 0 pairs
joined differently). An earlier state of the branch had cut conv3's input path (two +VDC fills on In1 touching at
0.0000 mm became separate pieces: 22.8 → 271 mΩ); one net's copper within 5 µm is now one piece (`PIECE_TOL_MM`,
TW-12).

**DC paths** (R at 20 °C, mΩ; `main` → now):

| Stage | Path | `main` | now | |
|---|---|---|---|---|
| conv1 | switch node → L301 | 0.297 | 0.291 | −1.9 %: the widths −3.9 %, the inductor's 4.9 A p-p ripple +2.0 % (its loss share on 10 A) |
| conv1 | input J101.2 → Q301 | 9.253 | 9.218 | −0.4 % |
| conv1 | output L301 → J104.2 | 5.662 | 5.628 | −0.6 % |
| conv1 | ground J104.1 → Q302 + J101.1 | 0.223 | 0.213 | −4.5 % |
| conv1 | hot loop C327+C332+C322… ↔ Q301/Q302 | – | 0.641 | new: 5.0 A RMS, 16.0 mW |
| conv2 | output L302 → J106.2 | 5.674 | 5.650 | −0.4 % |
| conv2 | hot loop | – | 0.629 | new: 2.9 A RMS, 5.4 mW |
| conv3 | input J101.2 → U502 | 22.770 | 22.204 | −2.5 % |
| conv4 | switch node → L503 | 2.104 | 2.620 | +24.5 %: the loss-equivalent R (loss = R·I²) with the inductor's ripple: 0.18 A p-p on 0.1 A, I²·(1 + ΔI²/(12 I²)) = ×1.26 |

The widths measured on the copper move the big paths by under 1 % and the short, wide ones (the ground paths, a
fraction of a milliohm, many vias and pads) by 2-5 %; the GaN stages' sustained densities by −4 to +3 %, their
peaks by −22 to +7 % (a peak is one node at a corner, now with its exact area). Of the whole-board DC model's 443,830
strips, the width-step series factor moves 2,530 (0.57 %) by more than 1 % and 388 by more than 20 % (necks and the
tracks' entries into pads); 13,600 of its 235,162 nodes (5.8 %) lie just outside their copper and have their heat
put on it (ISSUE-22).

**Thermal** (`tools/dev/dc_thermal_summary.py`, the same settings): hottest copper 124.27 → 124.54 °C (F.Cu,
(139.34, 83.69), at Q303); copper heat at 20 °C 537.1 → 557.5 mW (+20.4 mW: the two GaN stages' hot loops 21.4 mW,
the via barrels and the inductors' ripple, less the widths' −1 %); at temperature 670.4 → 697.3 mW; the parts
6.307 W in both. The combined DC view's loss now equals Thermal's heat (557.6 against 557.5 mW; `main`: 559.1
against 537.1, the view weighting whole cells and leaving out the vias). The paths at temperature: copper +20 to
+29 % (each path's copper at its heat-weighted 71-95 °C); the paths' totals less where a part's resistance is in
them (the dc paths' series parts stay as they were): conv1's output L301 → J104.2 5.628 → 6.035 mΩ at 83.8 °C, its
drop 56.3 → 60.4 mV and loss 563 → 604 mW; its input J101.2 → Q301 9.218 → 9.827 mΩ at 77.1 °C (drop 47.5 → 50.6 mV);
its switch node 0.291 → 0.377 mΩ at 95.0 °C (`kraft_dc_thermal_final.txt`).

**Emission.** The converters' conducted margins within 0.05 dB and their radiated worst within 0.21 dB. The nets'
worst margins below 1 GHz within ±0.8 dB; above 1 GHz, where a board-wide plane resonance sets them, SPI_SCK
+1.5 dB, SPI_MISO +3.8, SPI_MOSI +3.0 and Net-(U301--) +8.9 dB (its worst resonance moved from 1.59 to 1.29 GHz;
this net's settings describe a signal it does not carry, NEXT_STEPS item 2), and one plane_resonance issue fewer
(9 → 8). Is that the model or the grid? The four nets alone at 1x and 2x, both trees (`out/audit5/run_converge.sh`;
the nets alone at 1x reproduce the full run's figures exactly):

| net | `main` 1x | now 1x | `main` 2x | now 2x |
|---|---|---|---|---|
| SPI_SCK | 11.67 dB @ 0.94 GHz | 13.20 @ 1.22 | 18.81 @ 1.14 | 18.40 @ 0.74 |
| SPI_MISO | 15.07 @ 1.25 | 18.91 @ 1.25 | 15.54 @ 1.21 | 14.19 @ 1.21 |
| SPI_MOSI | 15.69 @ 1.23 | 18.66 @ 1.25 | 18.11 @ 1.21 | 16.60 @ 1.21 |
| Net-(U301--) | 22.12 @ 1.59 | 31.03 @ 1.29 | 47.95 @ 1.29 | 46.62 @ 1.29 |

At 2x the two trees agree within 0.4-1.5 dB, while each tree's 1x is 0.5-26 dB from its own 2x: above 1 GHz the 1x
grid has not converged in either (the grid note asks for 4x there, SP-19), and the 1x shifts between the trees are
inside that error, neither tree's 1x systematically nearer its 2x. The model change itself moves these margins by
about a dB.

**The window** (`tools/dev/gui_probe.py` on a copy, with Simulate): no stall from the board check that now precedes
every run; a run starts 0.8-0.9 s later on kraft (the board read again and fingerprinted, in a job). The one stall
over 150 ms after a Simulate (the first heatmap drawn as the results arrive, with a garbage collection) is `main`'s
too: 254 against 190 ms from the same cached results (`out/gui_probe/audit5/`).

**The tests.** The branch's full suite (`out/audit5/full_suite_final.log`, run while kraft was simulating beside it):
**960 passed, 5 skipped**, of which two (a window's busy-gap timing, 0.54 against 0.5 s, and a compute server's job
file deleted a moment late) failed under that load and passed alone (main's baseline: 901 passed, 5 skipped).
`tools/dev/undocumented.py` 0, `py311_check.py` 0 rejected, `ref_keys.py` 115 / 115.

## 8. Final assessment

Confidence: **high** (an independent method agrees within a few per cent over the conditions tested and the
remaining error is understood), **moderate** (agrees, but the model leaves out effects that can matter, or the
inputs are uncertain), **low** (an estimate).

| Model | After round 5 | Confidence | Outstanding limitations |
|---|---|---|---|
| Track and pour resistance, DC drop and loss | exact widths: R' within 1 % for 0.05-0.4 mm at 0°, 30° and 45° and any position (round 6, ISSUE-31: a track narrower than a cell is +1 to +2.5 % within ~10° of an axis and up to ±3.6 % at some angles, converging with the grid); a narrow band's width steps in series (a neck's ends a fixed ≤ 0.19 square each); corners and vias within 2 % | **high** | a step in copper wider than 4 cells or at a junction within ±d/2 (A39); copper at 100 % IACS (A27); the stackup's copper thickness is the user's (finished thickness needed) |
| DC figures at temperature | the DC tables at 20 °C; after Thermal each path's R, drop and loss at its temperature | **high** to first order | the currents keep their 20 °C pattern (a hotter stretch would push a little current elsewhere) |
| Current density (maps, sustained, checks) | J = I/(w·t) at the middle of a track; sustained 1.01-1.08 ×; another net's copper no longer dilutes it | **high** for the value, **low** for the limits | node-to-node ±25 % along angled tracks (the window evens it out); the 50 / 100 A/mm² limits are rules of thumb (A40) |
| Copper heat into Thermal | exactly the solved I²R of each path, with the inductor's ripple, the hot loop's switching current and the via barrels, in the node's own copper | **high** | paths sharing copper add as vectors node by node (exact where they flow alike); the hot loop's current shared by the capacitors' admittance at f_sw (first order) |
| Track temperature, current capacity | within 2 % of an independent cross-section for 0.075-0.4 mm where refined (fine quality also at 0.05 mm), the capacity within 2.4 % | **high** for the conduction model, **moderate** for absolute temperatures | convection correlations (one h per face, extrapolated for small boards, the face-down form unverified: ±10-30 % on h), laminate and resin conductivities, steady state only; normal quality's budget can leave part of a long narrow hot track coarse (−5 to −12 % there; the note's estimate low, ISSUE-24) |
| Topology (gaps, slots) | separate pieces of a net never join; fills touching within 5 µm are one | **high** | a slot narrower than a sample (≤ 0.15 mm) that does not cut the copper apart is still bridged |
| AC resistance (skin effect) | exact one-sided slab | **high** within its assumption | a conductor carrying current on both faces (a plane between two others) is not resolved; proximity effects between tracks not modelled |
| Plate capacitance | dielectrics in series | **high** | the facing copper share per cell from the sub-grid's sampled fill |
| Via barrel R, IPC-2221 capacity | as documented; the barrel's heat now in Thermal | **moderate** | the barrel's π·d·t reading ±t/d; the capacity is IPC-2221's chart fit for an internal conductor (A17), superseded for conductors by IPC-2152 |
| Clearances | IPC-2221B Table 6-1 as published | **high** (values), **moderate** (the >500 V reading, the standard's text not at hand) | B3, A5-A7 not built in |
| Emission, converters, loops, lines | not re-derived this round; references re-checked | as rounds 1-4 (moderate for absolute emission, high for the circuits) | as rounds 1-4: quasi-static, differential mode, ±2 dB above 1 GHz; a neck's inductance keeps the border's width (its resonance's ±d/2 step, ±4 % scatter) |
| Plugin geometry freshness | Simulate reads the board again and reloads a changed one | **high** | a board that cannot be read at that moment (KiCad busy) is analysed as read, with a status note; not tested against a live KiCad here |

What the round leaves unverified: CISPR 25's average column and edition (A10; its peak limits checked), IPC-2221B's
text (whether §6.2 prints the current equation; the >500 V reading), Incropera's heated-face-down coefficient, the
locations (section numbers) of the textbook results marked "usual". None of them is presented as verified.

## 9. Completion criteria

| Criterion | Where |
|---|---|
| Every relevant model and equation inspected | section 2 (the round's scope in full; the emission / converter models' references re-checked, their equations covered by rounds 1-4) |
| Every relevant reference verified or classified as unverified | section 3; REFERENCES.md updated (3 new sources, 3 corrected, A10 checked further, 6 new assumptions A39-A44) |
| Every significant assumption justified, limits documented | REFERENCES.md A1-A44 (A2, A3, A10, A27 revised; A39-A44 new), section 8 |
| 0.4 → 0.1 mm tested independently, with regression tests | section 4; TW-1 to TW-15 |
| Validated under the right boundary conditions | section 4.4 (fixed current vs voltage vs source resistance; GroundTruth's are current sinks, A42) |
| Thermal conclusions from the implemented model and independent evidence | section 4.5 (the solver against an independent cross-section; the capacity search; the heat's placement) |
| Confirmed defects corrected and retested, or documented | section 5: ISSUE-11 to -23 fixed or added with tests; ISSUE-24 documented with the evidence for leaving the planner |
| Numerical error and model limits kept apart | sections 4.2 (the step error), 6 (last paragraph), 8 |
| The report reflects the code and test results | the branch's full suite and the real board (section 7), the numbers above from the scripts named |
