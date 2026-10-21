# Plan: a generic small-signal model of op-amp circuits (any topology)

Status: **steps 1-5 done** on branch `analog-ac` (2026-10-21); the step response is the next branch. Asked by the user, of refboard's ADC path (ZXCT1107 → R304 →
TLV6001 follower → R303 / C304 → ADC): "Are we currently considering the Opamp circuit and it's amplification etc.
If I were to build an active lowpass filter, would that simulate it as one?", then "yes plan it", "present the
plan", and: **"I want a generic model that can calculate any circuit made from an OPA"**. Builds on `bode-sweep`
(the signal paths' Bode sweeps, `simulate.bode_paths`, the full telegrapher line, the op-amp's closed-loop output
impedance).

## Why

Today an op-amp on a signal path is a **gain block** (`emi/path.PathAmp`): its gain is the `Gain` field (default
1), one pole at GBW/|gain|, an input C, an output impedance, and the path is cut into a chain of stages at it. The
**circuit around it is never read**: a ×2 non-inverting stage without a `Gain` field is modelled as ×1; an active
filter's capacitor from an internal node to the op-amp's output (Sallen-Key, multiple feedback) is neither a
series part nor a shunt to a return and is lost; feedback from one op-amp back to an earlier one (a state-variable
filter, an instrumentation amplifier, a composite amplifier) cannot be expressed as a chain at all.

## What: one network, any topology, solved by modified nodal analysis

The **whole analog network** a path runs through, every op-amp and every part on its nets, as one linear circuit,
solved by **modified nodal analysis** (MNA) [Ho 1975] at each frequency: `(G + sC)·x = b`, the node voltages and
the currents of the voltage-defined elements. Nothing in the method assumes a topology: inverting, non-inverting,
differential, summing, integrators, differentiators, transimpedance stages, Sallen-Key / MFB / state-variable /
biquad filters, instrumentation amplifiers, composite amplifiers, any number of op-amps, feedback across op-amps,
dual and quad parts. What decides a circuit's response is its parts and wiring as they are on the board.

- **Passive parts** (models GroundTruth already has, `components/models.py`): resistor; capacitor as C with its
  ESR and ESL in series (`C_bias` replacing C); inductor L + R; ferrite R + L; short (merges two nodes); open (left
  out). Every one is lumped and rational in s.
- **The op-amp macromodel** (one per channel of a dual / quad), linear, from its datasheet values [Franco 2015]:
  - inputs: differential Rin ∥ Cin between IN+ and IN−, and common-mode Cin to ground at each (`Rin`, `Cin`,
    optional `Cin_cm`); defaults: 1 TΩ, 2 pF;
  - open-loop gain **A(s) = A0 / ((1 + s/ω1)(1 + s/ω2))**: A0 the new `Aol` field (default 100 dB), the dominant
    pole ω1 = 2π·GBW/A0, and an optional second pole ω2 (new `Fp2` field, or from the datasheet's phase margin at
    unity gain, `PM`: ω2 = 2π·GBW·tan(PM)); without either, one pole (as the gain block assumed). The second pole
    is what makes a follower driving a capacitor, or a transimpedance stage, ring: their stability shows;
  - a voltage-controlled source A(s)·(v₊ − v₋) behind the open-loop output resistance `Zout` (R_O);
  - its supplies: AC ground (decoupled); no rails, saturation or slew in a small-signal model (the slew cap stays
    in the emission estimate); `CMRR` and supply rejection left out (a later option: a CMRR term
    A/CMRR·(v₊ + v₋)/2).
  - `Gain` stays as an override: a part GroundTruth can't see inside (an instrumentation amplifier IC, a PGA)
    is still the gain block.
- **Sources and loads**: the path's driver as its Thevenin source (`Source R/L/C`; a current output such as the
  ZXCT1107: a large Source R), the receivers' load (`Load C/R`); other ICs' pins on the network open, or their
  input capacitance where they are receivers.
- **AC ground**: the reference nets, the supply rails (every net a decoupling capacitor joins to a reference net,
  and the nets detection already calls rails), reference voltages (a REF pin's net decoupled to ground).
- **Tracks**: the route's own stretches as transmission-line two-ports, Y = [[coth γl, −csch γl], [−csch γl,
  coth γl]] / Zc with the full line of `PathLine.zc` / `gamma_l` [Pozar 2012]; the short tracks inside an op-amp
  circuit (feedback, a filter's parts) as ideal wires at first (millimetres, far below a wavelength), said in a
  note; later the parasitic C of a summing node (a TIA's input) from the cross-sections.

## What it calculates

For each path (input: its driver, output: its receivers), and on request between any two pins of the network:

- the **transfer function** H(f): magnitude and phase, the gain at the sweep's start (DC), −3 dB, peaking;
- **poles and zeros, exact**: the finite generalised eigenvalues of the pencil (G, −C) [Golub 2013] (to add to
  REFERENCES.md) for the poles, of the pencil bordered with the input and output vectors for the zeros (the
  transmission zeros [Rosenbrock 1970], as `transfer.py` already does); a filter's pole pairs as **f0 and Q**.
  With lines in the network the response is fitted by vector fitting as today;
- every **op-amp's own output** and its closed-loop gain from the path's input (a multi-op-amp circuit: each node);
- the **input impedance** the driver sees and the **output impedance** at each op-amp output;
- each op-amp's **loop gain and phase margin**: the loop broken at the op-amp's output by the voltage-injection
  method (Middlebrook's, the loop gain T(s) = −v_return / v_inject) [Middlebrook 1975] (to add: "Measurement of loop gain in feedback systems", Int. J. Electronics, 1975), with its crossover
  frequency and phase margin: a stage that oscillates or rings is flagged (an issue below 45°);
- the **step response** from the exact poles and residues (inverse Laplace of H(s)/s): overshoot and settling, for
  a filter or an ADC driver.
- the **transfer function itself, shown in the results** (the user: "i want the transfer function to be shown in
  the results as well"):
  - **numeric, exact**: H(s) from the exact poles, zeros and gain, in the engineering normal form
    H(s) = K · Π(1 + s/ω_z) · Π(1 + s/(Q_z·ω_z) + s²/ω_z²) / [Π(1 + s/ω_p) · Π(1 + s/(Q_p·ω_p) + s²/ω_p²)]
    (K the DC gain, real roots as first-order factors, conjugate pairs as second-order factors with their f0 and
    Q, a pole or zero at the origin as s·…), and the expanded N(s)/D(s) coefficients; with lines in the network,
    the fitted rational function with its error;
  - **symbolic, in the part names** (option): H(s) for the ideal circuit (ideal op-amps, A → ∞; R, C, L without
    parasitics), e.g. a Sallen-Key's H(s) = 1 / (1 + s·C2·(R1 + R2) + s²·R1·R2·C1·C2), an inverting stage's
    −(R2/R1) / (1 + s·R2·C2), by solving the MNA system symbolically (sympy, BSD licence: a new dependency) for
    circuits up to about 15 parts (beyond: a note, the numeric form only); checked by substituting the values
    against the numeric ideal-op-amp solve;
  - **where**: the Results tab's Bode view (the expression over the plot in matplotlib's math text, the full
    factor list in the readout and tooltip), the HTML report (typeset, with the factor table), the console report,
    `groundtruth bode` and the JSON (`transfer_function`: K, the factors, N and D, the symbolic text).

## How it fits in

1. **`analysis/analog_net.py`, the netlist**: from a path's nets, grow the network: every two-pin part with a
   model that touches it, every op-amp channel with a pin on it, and the nets beyond them, until AC ground; a
   size cap (e.g. 200 nodes) with a note. Op-amp pin roles per channel: the schematic pin functions (IN+ / +IN /
   INP / +INA, IN− / −IN / INN / −INB, OUT / VOUT / OUTA), grouped by channel letter or number for duals and
   quads; else GT labels `amp_in`, `amp_out` and a new **`amp_inv`** (`amp_inv:6`); else a note naming the part
   (that op-amp then stays the gain block). Works on an unrouted board too: the pads carry their nets.
2. **`emi/mna.py`, the solver**: element stamps (R, C, L, a two-port line, a VCVS with A(s), sources), a sparse
   solve per frequency (tens to a few hundred nodes: well under a millisecond), the descriptor matrices (G, C) for
   the exact poles and zeros; the loop-gain injection; the step response.
3. **Bode first**: `bode_paths` / `path_responses` take the MNA network for every path (a passive path gives
   today's answer: checked to 1e-9); the Bode view's responses: the path, each op-amp's output, each op-amp's loop
   gain; tiles: gain, −3 dB, a pole pair's f0 and Q, the lowest phase margin.
4. **Then Simulate's emission estimate**: the loop currents of each part of the network (today: per stage) from
   the MNA solution, so a feedback capacitor's current loop counts. Separately, after the Bode part is validated,
   because it moves emission figures.
5. **UI and report**: the Parts tab's amplifier gets `Aol`, `Fp2` / `PM`, `Cin_cm` and the pin roles (with the
   schematic fields `Aol`, `PM` read as the others are); the Signal paths tab's path text lists the network's op-amps
   and parts; notes name parts without a model and pins whose role was guessed; help, README, ARCHITECTURE,
   REFERENCES (Ho 1975, Golub 2013, Middlebrook 1975; assumptions: the macromodel, rails as AC ground, in-circuit
   tracks as ideal wires).

## Validation (closed forms; tests/test_mna.py, boardgen boards)

| Circuit | Against |
|---|---|
| RC, RLC series and parallel, a capacitor's ESL notch | their rational functions |
| Inverting amplifier | −R2/R1, bandwidth GBW / (1 + R2/R1), closed-loop R_O / (1 + Aβ) |
| Non-inverting, follower | 1 + R2/R1, same bandwidth; refboard's U301 path unchanged (451 Hz) |
| Differential amplifier (4 resistors) | R2/R1 · (v₊ − v₋), its common-mode rejection from resistor mismatch |
| Integrator, differentiator | −1/(sRC) between its corners; the differentiator's peaking from the second pole |
| Transimpedance stage with feedback C | −R_f/(1 + sR_fC_f), its stability (phase margin) against the closed form |
| **Sallen-Key low-pass** (unity gain) | f0 = 1/(2π√(R1R2C1C2)), Q = √(R1R2C1C2)/(C2(R1 + R2)), 1 % [Franco 2015] |
| **Multiple-feedback low-pass** | f0, Q and −R2/R1, 1 %; the Q enhancement from a low GBW |
| **State-variable filter** (3 op-amps, feedback across them) | its LP / BP / HP outputs' f0 and Q |
| **Instrumentation amplifier** (3 op-amps) | 1 + 2R/R_g times the difference stage's gain |
| Follower driving 1 nF | the ringing frequency and phase margin from A(s)'s two poles and R_O·C |
| Step responses | a second-order low-pass's overshoot e^(−π/√(4Q²−1)) |
| Poles and zeros | exact eigenvalues against the closed forms and against the vector fit of the same sweep |
| A passive path | MNA against today's cascade, 1e-9 |

On the real board: refboard's two ADC paths (unchanged), and test boards for the filters and amplifiers above in
`tests/boardgen.py`.

## Decided (the user, 2026-10-21)

1. **Simulate's emission estimate uses the circuit model too** ("yes"): step 4 is part of this milestone, after the
   Bode side is validated (it moves emission figures: before / after on refboard).
2. **Tracks inside an op-amp circuit are modelled now** ("no, do it now"), not ideal wires: every track segment of
   the network's nets as a line two-port (its Zc and γ from its width and the stackup, as the route's stretches),
   joined at their junctions and pads; where a circuit must stay rational (the exact poles and zeros, the symbolic
   form) a short track (below λ/20 at the sweep's top) is its lumped π section (R, L, and C/2 at each end), a long
   one (the route's own lines) leaves the response to the vector fit.
3. **The loop gain (and phase margin) in the first branch, the step response in a second** ("Yes").
4. The symbolic transfer function: not answered; planned as recommended (numeric exact and symbolic, sympy as a
   new dependency): ask before adding the dependency.

## Progress

- **Branch `analog-ac`, step 2 first (the solver)**: `emi/mna.py` (Circuit: r, c, l, part (a model's series
  R-L-C), vsource, vcvs, vccs, opamp (OpAmpParams: A0, GBW, a second pole, R_O, Rin, Cin, Cin_cm), y2 / line_y (a
  line two-port); solve, transfer; poles_zeros exact by the generalised eigenvalues, equilibrated, with the
  cancelling pairs dropped). tests/test_mna.py: 9 pass (RC; series RLC's f0 and Q; a capacitor's ESL notch; an
  inverting amplifier's gain and its pole GBW·β + GBW/A0 to 1e-6; non-inverting ×2 at 5 MHz; a follower; Sallen-Key
  and MFB f0 and Q to 0.1 %; an integrator; a three-op-amp instrumentation amplifier, gain 21; a follower's second
  pole, Q 1 and +1.25 dB peaking; a line two-port open at its end = its capacitance).
- **Step 2, the circuit from the board** (f2ee51c): `analysis/analog_net.py` (the network from a path's nets out
  to AC ground: `ac_ground_nets`; `opamp_channels` / `pin_role`: per channel from the pin names, or the GT labels
  amp_in / **amp_inv** (new) / amp_out; tracks as line two-ports or π sections (`lumped=True`), vias R + L,
  unrouted nets joined as the netlist says with a note; the macromodel, or the gain block for a part with a Gain
  field), `route.copper_graph` split out of find_route; mna.py: sparse solve (refboard's ADC path, 97 track
  segments: 0.17 s for 300 frequencies), the normal form H(s) = K·s^m·Π(1 − s/z)/Π(1 − s/p). tests/test_analog_net.py:
  4 pass (an inverting amplifier and a Sallen-Key read from generated boards, `boardgen.opamp_circuit_board`;
  pin roles; a dual op-amp's channels). Refboard's /CPU/ADC_I_RAIL_1: U301 as the macromodel (its IN− from the pin
  name "−"), 451 Hz from the circuit, the follower's pole at 686 kHz.
- **Step 3, the transfer function written out** (ab320dc, 10f5eed; the user: "add sympy make sure to list it under
  the used libs": pyproject, plugin/requirements.txt, the help's licence list with mpmath): `analysis/symbolic.py`
  (the ideal circuit's H(s) in part names: text, LaTeX, MathML), `transfer.normal_form` (the full circuit's exact
  poles and zeros as factors with f and Q), `simulate.network_responses` (every path's Bode responses from its
  circuit: the receiver, each op-amp's output; doublets cancelled), the Bode plot's math-text line, the report's
  MathML. Refboard ADC_I_RAIL_1: `H(s) = 1 / (C304·R303·s + 1)`. Two model fixes on the way: an op-amp's c_in to ground
  (between the inputs it made an undamped 5.88 MHz notch with R_O), pads joined through tracks are not "unrouted".
- **Step 4, loop gain and phase margin** (488e151): `Circuit.loop_gain` (broken at the op-amp's ideal internal
  output [Middlebrook 1975]), `transfer.phase_margin`, a loop-gain response per op-amp and a "phase margin" tile.
  Refboard's U301: crossover 671 kHz, 89°.
- **T-junctions** (383d0e6): a track another track ends on is split there (copper_graph's joins from the junction
  to its ends had doubled its copper); refboard's ADC path 35 track pieces, not 97.
- **Step 5, Simulate's emission from the circuit** (`simulate.NetworkPath`): per stage a PathResult from the MNA
  solution (each track piece's exact mean current, shunts' currents, the share past each), the loops and field
  solves unchanged; the cascade where the circuit can't be made. test_path's termination comparison within 15 dB
  of the peak (the stub lines move a 2.45 GHz notch).
- **Docs**: help (section "Op-amp circuits and the transfer function", the Signal paths and Bode passages), README
  (the same, the amplifier model's aol / fp2 / pm, the fields Aol / Fp2 / PM, the amp_inv label), ARCHITECTURE
  ("Analog circuits"), REFERENCES (Golub 2013, Middlebrook 1975, Franco 2015; A52 the macromodel, A53 the circuit,
  A54 the symbolic form).
- **Left**: the schematic fields Aol / Fp2 / PM (components/fields.py) and the Parts tab showing them; the full
  suite; HANDOFF; the step response (a second branch, as decided).

Estimate: three to four sessions (the solver and the closed-form tests; the netlist, pin roles and macromodel
fields; the loop gain; integration, UI, docs and refboard).
