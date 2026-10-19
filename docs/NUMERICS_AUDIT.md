# Numerical audit, round 6 (2026-10-20, branch `audit-6`)

The sixth round of the validation audit ([VALIDATION_AUDIT.md](VALIDATION_AUDIT.md) holds rounds 1-4, a summary of
5 and 6; [PHYSICS_AUDIT.md](PHYSICS_AUDIT.md) is round 5). Round 5 checked the physics, the references and the
track-width chain. This round asks whether the numerics solve that physics accurately and reliably: the iterative
solvers and their stopping tests, convergence under refinement against independent references, adversarial copper,
metamorphic properties, stale state, and failure handling. Source keys in square brackets are in
[REFERENCES.md](REFERENCES.md).

1. [Scope, method, baseline](#1-scope-method-baseline)
2. [The numerical methods](#2-the-numerical-methods)
3. [Independent references](#3-independent-references)
4. [Convergence studies and measured errors](#4-convergence-studies-and-measured-errors)
5. [Adversarial copper](#5-adversarial-copper)
6. [Metamorphic and property tests](#6-metamorphic-and-property-tests)
7. [Stale state and caches](#7-stale-state-and-caches)
8. [Stress, failure, recovery](#8-stress-failure-recovery)
9. [Defects: ISSUE-25 to ISSUE-30](#9-defects-issue-25-to-issue-30)
10. [The real board](#10-the-real-board)
11. [Accuracy, convergence and model uncertainty, apart](#11-accuracy-convergence-and-model-uncertainty-apart)
12. [Remaining limitations](#12-remaining-limitations)
13. [Tests and checks](#13-tests-and-checks)

## 1. Scope, method, baseline

**Scope.** Every solver behind Signal paths, Converters (power stages, motor bridges) and DC loads, and the thermal
solve they feed: the PEEC system (partial-inductance kernels, the FFT convolution, the saddle-point system, its
Schur-complement preconditioner, restarted GMRES and its stopping test), the DC solves (the same system at 10 Hz),
the current-density figures, the 2D cross-section field solver (line Z0), the thermal finite-volume model (AMG-CG,
the coupled ρ(T) iteration), the vector fitting of the Bode views, and the plugin's state (which board and settings
a result is of).

**Method.** For each solver: what it discretises and how, its tolerance and what the tolerance is measured on, what
happens when it does not converge, and whether a result can look successful while being wrong. Then the numbers:
- the same problem at the shipped tolerance and at a far tighter one (does the tolerance limit the answer?);
- the same problem on refined grids against an independent reference (does it converge, and to the right value?);
- shapes chosen to break the discretisation (a neck shorter than a cell, acute bends, branches, near-touching
  copper of another net, gaps either side of the joining tolerance, a re-entrant corner);
- transformations with a known effect (rotation, mirroring, translation, reversed and re-split tracks, a reordered
  file, reciprocity, linearity, Tellegen, twice the copper);
- degenerate inputs and forced failures (a stalled CG, an unconverged DC solve, zero thicknesses, zero/negative/NaN
  currents, a runaway).

The independent references share no code with GroundTruth: the finite-difference sheet solver of round 5
(`tests/refsolvers.py`), extended this round with GroundTruth's own boundary condition (`sheet_potentials_fed`: each
pad fed uniformly, the pin's potential the feed-weighted mean) and a windowed density (`sheet_density_peak_fed`);
scipy's adaptive quadrature for the kernels; Cohn's exact stripline results; the magnetic-wall cavity; Wheeler's
loop inductance; the 2D thermal cross-section of round 5.

**Baseline.** `main` at 8f40781 (round 5 merged), its full suite 960 passed / 5 skipped on the round-5 branch. The
round's code is on `audit-6`; the before/after figures name the tree they come from.

**The tools** (all in `tools/dev/`, reusable): `tol_check.py` (a Simulate at any GMRES tolerance, with the KVL and KCL
rows' residuals apart), `thermal_tol_check.py` (Thermal at the shipped tolerances against CG 1e-12),
`adversarial_dc.py` (the shapes of section 5 at 1x/2x/4x against both sheet references), `metamorphic_dc.py`
(section 6), `cavity_analytic_converge.py` (the cavity, the plate capacitance and the loop inductance against closed
forms by pitch), `stress_inputs.py` (section 8).

## 2. The numerical methods

| Solver | Discretisation | Tolerance, what it is measured on | Non-convergence before → after this round |
|---|---|---|---|
| PEEC strips and nodes | uniform grid, strips across cell borders with the copper's exact width (round 5), nodes with its exact area; per-layer-pair convolution kernels of partial inductance [Ruehli 1972] | – | – |
| Kernels (`strip_kernel`) | far: filaments [Grover 1946]; near (≤ 4 cells) and self: **exact 1D reduction of the double width integral** (`tape_mutual`, graded composite Gauss) | 2e-11 against adaptive quadrature | was 8 × 8 Gauss + 24 sub-strips: **ISSUE-28** |
| L_p product | FFT convolution in double precision [Oppenheim 2010] | 1e-12 against direct sums (round 1) | – |
| Preconditioner | the saddle-point system with diag(Z) or per-position sheet blocks, solved exactly by the Schur complement (SuperLU, MMD, diag pivots 0.1) [Benzi 2005] | – | LU failures: memory ladder, SuperLU size limit (sessions 8-10) |
| GMRES | right-preconditioned, restarted (≥ 100), CGS2, Givens [Saad 1986] | ‖b − Kx‖ ≤ 1e-5·‖b‖, the true residual recomputed per cycle; one norm over KVL (V) and KCL (A) rows | unconverged → SolverError → the frequency unsolved, `band_unsolved`; a DC path's: a note only → **ISSUE-25** |
| DC solves | the same system at 10 Hz (skin factor 1 + 4Δ⁴/45, Δ ≈ 0.003), injections spread over pads | as GMRES | as above |
| Current density | node field from the strips' heat over the node's copper; sustained = own-net RMS over 1 mm [A40]; **peak = own-net RMS over 0.25 mm** [A45] | – | the peak was a node's value, singular at corners: **ISSUE-27** |
| 2D cross-section | finite differences on a graded grid, two grids Richardson-extrapolated (first order: the r^(−½) edge charge) | – | – |
| Thermal | finite volumes, cut cells, adaptive refinement [Patankar 1980]; AMG-preconditioned CG [Hestenes 1952] [Ruge 1987], hierarchy reused | CG ‖r‖ ≤ tol·‖b‖ (1e-5 … 1e-8, tightened as the iteration settles); coupled ρ(T) until max ΔT < 0.02 K, under-relaxed on growth | CG unconverged/broken down: a log line; a reused hierarchy out of budget: ValueError → **ISSUE-26** |
| Vector fitting | [Gustavsen 1999] | its largest error over the band, shown with every fitted view ("fitted, ±2.86 dB") | reported, not hidden |
| Plugin state | the views keep their board and settings | Board.fingerprint (round 5), settings key | shown and heated as current after a reload or edit → **ISSUE-29** |
| Stackup input | copper and dielectric thicknesses from the file / KiCad | – | 0 µm copper: 35 µm in the PEEC models, 0 in Thermal's heat, silent → **ISSUE-30** |

**Where a solver could report success while wrong**, found by reading and confirmed by forcing it: a DC path that
did not solve was dropped while the stage's efficiency and heat were shown as complete (ISSUE-25, also motor
bridges' legs); a stalled thermal CG gave a "settled" field at the start temperature (ISSUE-26); a reported peak
density that was the grid's, not the board's (ISSUE-27); results of an earlier board shown, and heated, as current
(ISSUE-29). And one that looked suspicious but measures clean: GMRES's single norm over volts and amperes (section 4).

## 3. Independent references

| Reference | Source | Own accuracy (checked) | Used for |
|---|---|---|---|
| Uniform track R' = ρ/(w·t) | closed form [IEC 60028] | exact | minimum width, thickness scaling |
| Sheet solve, equipotential pads | `refsolvers.sheet_resistance` (round 5), FD with coverage-weighted faces | h = 0.02 vs 0.01 mm: < 1 % on axis-aligned copper, **up to 2 % and erratic on narrow oblique copper** (staircase aliasing; order −2 to +7 between h steps) | necks, branches, tee |
| Sheet solve, fed pads | `refsolvers.sheet_potentials_fed` (new): GroundTruth's boundary condition | same faces; turned so a bend's arms lie on its grid: 57.444 / 57.436 / 57.431 mΩ at h = 0.02 / 0.01 / 0.005 | bends, branches, tee, the corner |
| Windowed density | `refsolvers.sheet_density_peak_fed` (new) | 0.25 mm window: 318.9 / 320.5 A/mm² at h = 0.025 / 0.0125 (its cell maximum 591 / 745: singular) | the peak (ISSUE-27) |
| Tape mutual inductance | scipy `quad` of the filament formula [Grover 1946] over the widths | epsrel 1e-12 | kernels (ISSUE-28) |
| Plane pair sheet inductance | μ0·dz per square [Pozar 2012] | exact (infinite plane) | kernels |
| Cavity f10 | c/(2a√εr), magnetic walls [Pozar 2012] | the model limit for a pair without fringing: see section 11 | cavity convergence |
| Loop inductance | Wheeler's microstrip loop (test_solver) | the form's own ~1 % | route-model convergence |
| Stripline Z0 | Cohn's exact conformal maps [Cohn 1954] [Cohn 1955] | exact | cross-section extremes |
| Track temperature | round 5's 2D cross-section | 0.2 % | thermal tolerance (round 5 for the grid) |

## 4. Convergence studies and measured errors

**GMRES tolerance (does rtol 1e-5 limit any result?).** The same Simulate at 1e-5 and at 1e-10/1e-11
(`tol_check.py`), every number of the JSON report compared (`result_diff.py`):

| Case | Solves | KVL rows' own residual at 1e-5 | Largest change, tight vs shipped |
|---|---|---|---|
| example series_path (route + board-wide) | 11 | 6.7e-6 | 6e-5 dB |
| kraft conv1 (DC paths, hot loop, conducted to 150 kHz, radiated, fixes) | 29 | **1.5e-3** at 150 kHz (the whole 4e-6) | 0.007 dB (conducted, 563 kHz); every DC path identical to 6 digits |
| kraft SPI_SCK (route + board-wide, resonances 0.94-1.58 GHz) | 12 | 7.8e-6 | 0.0003 dB |

The mixed-units norm lets the KVL rows sit at 1e-3 of their own size at low frequency, but nothing reported moves by
more than 0.007 dB: the tolerance is adequate as shipped. (At 1e-11 one 10 Hz DC solve could not converge, the floor
of double precision there; that exposed ISSUE-25.)

**Thermal tolerances.** A 100 mm, 0.1 mm track at 1 A (1.14 M unknowns, normal quality): CG converged in 6-13
iterations per solve (true residual 4e-6 … 3e-7), 5 coupled iterations; continuing from that answer with CG at 1e-12
and four more passes moves the temperatures by at most 2.8e-4 K on a 29 K rise (1e-5). The fixed-point iteration's
contraction is α·ΔT/(1 + α(T − 20)): 0.24 at an 80 K rise, so stopping at 0.02 K leaves 0.006 K; it only slows near
runaway, which is caught (T > 1000 °C).

**DC grids against the sheet solves** (`adversarial_dc.py`, 1 A unless noted; errors of GroundTruth at 1x / 2x / 4x):

| Copper | Reference | 1x | 2x | 4x |
|---|---|---|---|---|
| 0.02 / 0.03 mm tracks, R' | 1/(σwt) | 0.00 / 0.00 % | −0.11 / −0.10 % | 0.00 / 0.00 % |
| 90° bend, 0.25 mm, arms at ±45° | fed, turned (57.431 mΩ) | −0.70 % | −0.39 % | −0.14 % |
| 0.4 + 0.1 mm in parallel, R | fed (30.51 mΩ) | −0.80 % | −0.66 % | −0.42 % |
| the same, the narrow branch's share | the branches' R | +1.6 % | +1.8 % | +1.6 % |
| symmetric tee, 0.5 A per arm | fed (23.64 mV) | +0.15 % | +0.79 % | +0.37 % |
| the same three against equipotential pads | `sheet_resistance` | +1.2 / +2.0 % | +1.3 / +1.1 % | +1.6 / +1.5 % |

The last row is the pads' model, not the grid: against a reference with GroundTruth's own feed the errors fall to
the first rows' and shrink with the grid. An equipotential pad (a perfect conductor over the whole pad) and a
uniformly fed pad bracket a real soldered joint; the difference is about one square per path (A42).

**AC solver against closed forms** (`cavity_analytic_converge.py`; sub-grid 4 × 4, as Simulate):

| Pitch | Plate C (ε0εrA/t) | Loop L (Wheeler) | Cavity f10, before ISSUE-28 | after |
|---|---|---|---|---|
| 1 mm | +0.04 % | +0.15 % | −0.19 % | −0.91 % |
| 0.5 mm | +0.04 % | +0.17 % | +1.00 % | +0.63 % |
| 0.25 mm | +0.04 % | +0.16 % | +1.31 % | +1.12 % |

After the kernel fix the cavity converges monotonically, to about +1.5 % (Richardson): the PEEC model's own limit,
above the magnetic-wall value because it has the plates' inductive fringing and not their capacitive fringing (A46).

**Kernels** (ISSUE-28): a plane pair's sheet inductance 2·Σ(K(0) − K(dz)) per square against μ0·dz, 0.5 mm cells,
41 × 41 plane:

| d/dz | 1 | 2.5 | 5 | 10 | 20 |
|---|---|---|---|---|---|
| before (8 × 8 Gauss, 24 sub-strips) | −0.30 % | −0.74 % | −1.46 % | −3.92 % | −19.7 % |
| after (`tape_mutual`) | 0 | 0 | 0 | 0 | 0 |

(against the same sums with every near term by adaptive quadrature; the remaining distance to μ0·dz itself, 0.1-1 %,
is the finite plane's outside, which shrinks with dz.)

**Cross-section** (Cohn's exact results, default grid, extrapolated): stripline w/b 0.02-8: −0.08 to −0.30 %;
edge-coupled s/b 0.01-1: Zdiff −0.21 to −0.25 %, Zcomm −0.14 to −0.20 %. Within the 0.5 % the tests ask. Refining
past the default is not monotone below s/b ≈ 0.03 (the cells reach the 0.5 µm floor), so the default is the right
setting, not a coarser stand-in for a better one.

**The peak density** (ISSUE-27), a 2 mm wide L at 10 A, its inner corner: the node value 290 / 348 / 366 A/mm² at 1x
/ 2x / 4x (× 2^(1/3) per halving: the singularity); now 290 / 292 / 297 / 309 at 1x / 2x / 4x / 8x against the
independent 320 A/mm²; the sustained value 210 / 207 / 207 / 207.

## 5. Adversarial copper

| Case | What could go wrong | Result |
|---|---|---|
| A 0.1 mm neck 0.1 / 0.25 / 0.5 / 1 mm long in a 0.4 mm track | a feature shorter than a cell | the absolute error stays under 0.36 square at 1x and falls with the grid (−0.08 square at 4x); relative to the neck's own small resistance −6 to −22 % at 1x for 0.5-1 mm. Shorter necks are not necks: the 0.4 mm track's round ends overlap (0.0016 / 0.036 mΩ added) |
| 0.02 / 0.03 mm tracks | narrower than a sample | R' exact to 0.1 % (along an axis) |
| 0.1 / 0.15 mm tracks at every 3° from 0 to 90°, two positions (`angle_scan.py`); 8 generated tracks (NA-8) | copper narrower than a cell crossing rows at any angle | rms 0.8 / 1.0 %; **+1 to +2.5 % within ~10° of an axis** (both positions) and **−2.8 to +3.6 % at 57-60°** (one position); converging: 6° +2.2 / +2.0 / +0.7 %, 60° −2.8 / −0.8 / −0.1 % at 1x / 2x / 4x; 0.4 mm within 0.3 % (ISSUE-31) |
| V bends at 20°, 45°, 90° | acute staircases | 90°: −0.70 → −0.14 % (exact reference); 20° and 45°: within the equipotential reference's own ±2 % |
| Parallel 0.4 / 0.1 mm branches | current sharing | R −0.8 → −0.4 %, the share within 2 % |
| A tee, 0.5 A to each arm | a junction | within 0.8 % of the fed reference |
| A 0.3 mm ring, the pads opposite | a loop of oblique copper | 12.31 / 12.18 / 12.22 mΩ at 1x / 2x / 4x against the fed reference's 12.52 / 12.39 at h = 0.02 / 0.01 (itself still moving 1 % there): within its uncertainty |
| Another net's track 0.05 / 0.1 mm beside a 0.2 mm track | copper of two nets in one cell | the drop identical to the track alone (0.0) |
| A gap of 0.003 / 0.006 / 0.02 mm | connectivity flipping on a small edit | joined at 3 µm (`PIECE_TOL_MM` = 5 µm, touching fills: by design), "not connected" from 6 µm |
| A 2 mm L at 10 A | a re-entrant corner | the node peak diverged with the grid (ISSUE-27); the windowed peak converges |
| A 3 mm wide track stepping to 0.1 mm | a step in a band wider than 4 cells | what the 0.1 mm part adds: −0.29 / −0.42 / −0.03 % of the fed reference (67.58 mΩ) at 1x / 2x / 4x; 0.2-0.8 square against the equipotential one (A39's ±d/2) |

## 6. Metamorphic and property tests

`metamorphic_dc.py` on a rail with a bend, a via and a plane (tests NA-1 to NA-5):

| Transformation | Exact effect | Measured |
|---|---|---|
| track reversed, split at odd points, drawn twice; file items shuffled | none | 0 (bit-identical; the shuffled file has the same fingerprint) |
| source ↔ load | none (reciprocity [Desoer 1969]) | ≤ 3e-16 |
| current × 5 | drop × 5, loss × 25 | ≤ 2e-16 |
| loss vs I·drop | equal (Tellegen [Tellegen 1952]) | 3e-16 |
| rotated 90/180/270°, mirrored | none | +0.04 to +0.44 % (where the grid falls) |
| shifted by sub-cell amounts | none | +0.02 to +0.19 % |
| copper 35 → 70 µm (no vias) | × 0.5 | × 0.5 to 1e-4 (with the via: 0.509, the barrel stays) |
| simulated twice | identical | identical |

## 7. Stale state and caches

- **Caches checked** against what their values depend on: the kernel spectra (key nx, ny, d, dz: complete; 128 MB
  cap), the operator cache (two factorisations per stack, keyed by frequency; the borrow plan decided per round,
  reproducible), the line cuts (per Simulate), the spread receiver's `lru_cache` (floats), the board's part index and
  `_planes` (keyed by the list's identity and length: no path reassigns those lists twice on one board, so no stale
  hit today; noted as fragile), Suggest fixes' baseline (reused only when the board and settings objects are the
  same).
- **The plugin's results** (ISSUE-29): Simulate re-reads the board since round 5, but the Heatmap kept showing the
  previous run after Reload board or a settings edit with nothing to say so, and Thermal / Current capacity heated
  the board and settings of that run (a thermal setting edited since, the ambient, was ignored). Now the views carry
  their board's fingerprint and a settings key; the summary line says OUT OF DATE (and why), Thermal and Capacity read
  the board first and refuse stale views, and Thermal takes the current thermal section.
- Save and reload: the board file's items in another order give the same fingerprint and bit-identical results (NA-1).

## 8. Stress, failure, recovery

`stress_inputs.py` and forced failures:

| Input | Outcome |
|---|---|
| zero-width track, a load pin on another net | "not connected" error naming the rail |
| load current 0, −1 A, NaN | ConfigError naming the entry |
| 1 MA | linear, finite (4.8e7 mV) |
| 0.05 mm track at 10 A, Thermal | "thermal runaway: the copper passes 1000 °C" |
| 0 µm copper in the stackup | **was** silent and inconsistent (35 µm in PEEC, 0 in Thermal's heat) → board warning, 35 µm everywhere (ISSUE-30) |
| 0 mm dielectric | **was** silent (layers 1 µm apart) → board warning (ISSUE-30) |
| a DC path's solve fails (forced) | **was** a note while loss, efficiency and heat looked complete → listed, warned, noted (ISSUE-25) |
| a motor bridge leg's solve fails (forced) | **was** silent unless all failed → listed, warned, noted (ISSUE-25) |
| the thermal CG stalls (forced) | **was** "settled" at the start temperature → not settled, noted (ISSUE-26) |
| a reused AMG hierarchy uses the whole budget | **was** ValueError → rebuilt with its own budget (ISSUE-26) |
| GMRES unconverged on a route | SolverError, the frequency unsolved, `band_unsolved` warning (sessions 7-8) |
| SuperLU out of memory / past its 32-bit limit | the memory ladder, diagonal pivots, `lu_too_large` (sessions 8-10, test_memory_relief, test_lu_size) |

## 9. Defects: ISSUE-25 to ISSUE-30

| ID | Area | Severity | Status |
|---|---|---|---|
| ISSUE-25 | A failed DC path (stage) or leg solve (bridge) left out silently of loss, efficiency, heat | High | Fixed, tested |
| ISSUE-26 | Thermal CG: a stall read as settled; a reused hierarchy out of budget crashed | High | Fixed, tested |
| ISSUE-27 | The peak current density was the grid's (diverging at re-entrant corners) | Medium | Fixed, tested |
| ISSUE-28 | Kernel near field and self term inaccurate when layers are closer than ~d/5 | Medium | Fixed, tested |
| ISSUE-29 | Plugin: results of an earlier board / settings shown and heated as current | Medium | Fixed, tested |
| ISSUE-30 | Zero copper / dielectric thickness accepted silently and inconsistently | Low | Fixed, tested |
| ISSUE-31 | A track narrower than a cell: R' +1 to +3.6 % at shallow and some other angles at 1x | Low-Medium | Documented (converges with the grid); round 5's "any angle" corrected |

**ISSUE-25.** *Reproduce:* kraft conv1 at GMRES 1e-11: the input path's 10 Hz solve did not converge; the report
then had four DC paths, input copper 0.229 instead of 0.301 W, losses 3.11 instead of 3.36 W, efficiency 98.9 instead
of 98.8 %, with one note ("DC input J101.2 → Q301 not solved"). *Root cause:* `dc_analysis` skipped an unsolved path
and nothing downstream knew it existed. Motor bridges' `RmsBasis` did the same per leg and band. *Fix:* the stage
lists `dc_unsolved` (the JSON report too), `input_estimate` names each in `missing` (the input note shows it),
`thermal_inputs` notes the missing heat, a `dc_unsolved` warning names the paths; bridges: `rms_missing` with the same
three. *Tests:* `test_ISSUE25_a_failed_dc_path_…`, `test_ISSUE25_a_bridge_whose_leg_…` (both fail on `main`).

**ISSUE-26.** *Reproduce:* a CG that makes no progress (pyamg's info > 0, x returned as started): two equal answers,
the coupled loop's ΔT = 0 < 0.02 K → settled, the board at the start temperature, no note; and a reused hierarchy
given cap = maxiter that used it all: `run_cg(x, maxiter − iters)` with 0 → ValueError. *Root cause:* `SPDSolver`
only logged `info`, ignored `info < 0`, and budgeted the rebuild from what was left. *Fix:* `last_converged` /
`last_residual` per solve; the rebuild gets its own budget; both coupled loops do not stop on an unconverged solve and
say so. *Tests:* `test_ISSUE26_…` (both fail on `main`).

**ISSUE-27.** Section 4. *Fix:* `peak_density`, A45; the help says what the peak is. *Test:* `test_ISSUE27_…`.

**ISSUE-28.** Section 4. *Root cause:* the near field's 8 × 8 Gauss points cannot follow an integrand that varies on
the scale dz ≪ d, and a plane pair's sheet inductance is a difference of sums that magnifies that error ~d/dz; the
self term's 24 sub-strips converge as 1/n (0.86 % low). *Fix:* `kernel.tape_mutual`, the exact reduction to one
integral over the widths' difference with panels graded toward the crossing (2e-11), for the near field and the self
term (`strip_self_numeric`, `_narrow_self` with it). *Tests:* `test_ISSUE28_…` (10 cases, all fail on `main`).
*Reach:* every model's partial self inductance +0.86 %; plane-pair cavities where d/dz is large (kraft's board-wide
model: 6.4 at 1x); a route's loop inductance +0.03 to +0.11 % by pitch (Wheeler: +0.04 … +0.13 → +0.15 … +0.17 %).

**ISSUE-29.** Section 7. *Test:* `test_results_of_an_earlier_board_or_settings_are_marked_and_not_heated` (test_gui).

**ISSUE-30.** Section 8. *Test:* `test_ISSUE30_…`.

**ISSUE-31.** Found by the generated cases (NA-8: 2 of 8 random tracks failed 1.5 %), mapped with
`tools/dev/angle_scan.py` (`out/audit6/angle_scan.txt`). *Root cause:* a track narrower than a cell that crosses a
row border at a shallow angle shares its copper between two rows of strips over several cells; each strip's width
is exact (round 5), but the two-point flux between partly covered cells puts the current's path a fraction of a cell
off, a first-order finite-volume error for copper not aligned with the grid. It falls with the grid. Round 5 had
checked 0°, 30° and 45° (all within 1 %), and its summary said "any angle": corrected in PHYSICS_AUDIT.md and
VALIDATION_AUDIT.md. *Not fixed:* a cure is a different face model for oblique sub-cell copper; at 2x the worst seen
is 2.2 %, at 4x 0.8 %. *Test:* NA-8 (1.5 % at least a cell wide, 4 % below).

## 10. The real board

kraft_flux_mini_2 (`flux-2`, read only; settings `out/kraft_refresh/kraft_all.groundtruth.yaml`, scratch copies),
1x, CPU, on this laptop (4 cores): conv1 and SPI_SCK before (the tree before ISSUE-27/28) and after.

KRAFT_TBD

## 11. Accuracy, convergence and model uncertainty, apart

- **Demonstrated accuracy** (against an independent reference, converged): track R' (0.1 %), DC paths through bends,
  branches and junctions with GroundTruth's own pad model (≤ 0.8 % at 1x, falling with the grid), plate capacitance
  (0.04 %), loop inductance (0.16 %), stripline Z0 (≤ 0.3 %), kernels (2e-11), thermal tolerance (1e-5 of the rise),
  the windowed peak (−10 % at 1x, −4 % at 8x, converging from below).
- **Numerical convergence without an absolute reference**: the board-wide resonances above 1 GHz (round 4: ±2 dB from
  4x on), the 20° and 45° bends (inside the oblique reference's ±2 %).
- **Physical-model uncertainty** (does not shrink with the grid): the pads' feed (uniform vs equipotential, ~1 square
  per path, A42), the plane pairs' missing fringing capacitance (a cavity ~+1.5 % high, A46), quasi-statics above the
  board's size in wavelengths (A1), the convection correlations and materials (round 5), differential-mode emission
  only (A11).

## 12. Remaining limitations

- A cavity's resonance is good to about ±2 % from the model (A46) plus the grid's ±4 % for narrow copper (round 4).
- A neck shorter than about two cells reads up to ~20 % of its own resistance low at 1x (≤ 0.36 square); 4x for such
  features.
- A track narrower than a cell at a shallow angle (within ~10° of an axis) reads +1 to +2.5 % resistive at 1x, up to
  ±3.6 % at some other angles and positions (ISSUE-31); 2x / 4x reduce it to ≤ 2.2 / 0.8 %.
- The peak density at a corner reads ~10 % low at 1x (the cell is the window); the sustained value is unaffected.
- The equipotential sheet reference is not accurate enough (±2 %) to arbitrate narrow oblique copper; the rotated
  fed reference covers 90° bends only.
- GMRES's stopping test keeps one norm over volts and amperes; measured harmless on the real board (≤ 0.007 dB), not
  proven for every board. `tools/dev/tol_check.py` checks any board.
- The board's part index and plane cache are keyed by list identity and length: safe for today's code paths, not for
  a future in-place list edit.
- Not exercised here: the GPU path (no usable GPU on this laptop), a live KiCad (the checks use the file reader),
  thermal fine quality on kraft (memory).

## 13. Tests and checks

New: tests/test_numerics_audit.py (ISSUE-25 to -30, NA-1 to NA-8: 39 tests), one window test in test_gui.py;
refsolvers' fed sheet solve and windowed density; tools/dev/angle_scan.py.

FULLSUITE_TBD
