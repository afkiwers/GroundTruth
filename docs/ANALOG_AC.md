# Plan: small-signal AC analysis of analog stages (active filters, amplifiers)

Status: **planned, not started** (2026-10-21). Asked by the user, of refboard's ADC path (ZXCT1107 → R304 →
TLV6001 follower → R303 / C304 → ADC): "Are we currently considering the Opamp circuit and it's amplification etc.
If I were to build an active lowpass filter, would that simulate it as one?", then "yes plan it". Builds on the
`bode-sweep` branch (signal paths' Bode sweeps, `simulate.bode_paths`, the full telegrapher line, the op-amp's
closed-loop output impedance).

## Why

Today an op-amp on a signal path is a **gain block** (`emi/path.PathAmp`): its gain is the `Gain` field (default
1), one pole at GBW/|gain|, an input C, an output impedance. The **feedback network is never read**: a non-inverting
×2 stage without a `Gain` field is modelled as ×1, and the parts around the op-amp count only where they sit on
the path or across it to ground. An active filter (Sallen-Key, multiple feedback) has a capacitor from an internal
node to the op-amp's **output**: neither a series part of the path nor a shunt to a return, so its second-order
response (f0, Q) is not simulated; only its first RC and the Gain/GBW block are.

## What: modified nodal analysis per stage

A stage's network (every part on the nets around the op-amp) solved by **modified nodal analysis** (MNA)
[Ho 1975] at each frequency, `(G + sC)·x = b`: node voltages and the currents of the voltage-defined elements.

- **Elements** (all from models GroundTruth already has, `components/models.py`): resistor; capacitor as C with
  its ESR and ESL in series (`C_bias` replacing C); inductor L + R; ferrite R + L; short (a 0 Ω branch, merging
  nodes); open (left out).
- **Op-amp** from its pins **IN+, IN−, OUT, its supplies**: an input impedance (`Rin` ∥ `Cin`, between the inputs
  and to ground), a voltage-controlled source A(s)·(v₊ − v₋) with the open-loop gain of a single dominant pole,
  A(s) = A0 / (1 + s·A0/(2π·GBW)) (A0 the new optional `Aol` field, default 10⁵ = 100 dB), behind the open-loop
  output resistance `Zout` (the datasheet's R_O: what the field means since `bode-sweep`) [Franco 2015]. The loop's
  gain, bandwidth and output impedance then come from the resistors and capacitors, not from fields; a `Gain`
  field stays as an override for a part GroundTruth can't see (an IC amplifier, a PGA). Small-signal only: no
  output swing, no slew in the AC sweep (the slew cap stays in the emission estimate).
- **Supplies and references are AC ground** (the reference nets, every net a decoupling capacitor joins to a
  reference net, the nets named as rails: the same sets detection already uses).
- **Source and load**: the path's driver as its Thevenin source (`Source R/L/C`; a current output such as the
  ZXCT1107: a large Source R), its receivers' load (`Load C/R`).
- **Tracks**: the route's own stretches as transmission-line two-ports (Y = [[coth γl, −csch γl], [−csch γl,
  coth γl]] / Zc, the full line from `PathLine.zc` / `gamma_l` [Pozar 2012]); the short tracks inside a stage
  (feedback, the filter's parts) as ideal wires at first (millimetres, far below a wavelength at the stage's
  frequencies), a note says so.
- **Poles and zeros exact** for a lumped stage: the poles are the finite generalised eigenvalues of the pencil
  (G, −C) [Golub 2013] (to add to REFERENCES.md), the zeros those of the pencil bordered with the input and output vectors (the
  transmission zeros, [Rosenbrock 1970] as `transfer.py` already uses); with lines in the network the response is
  fitted by vector fitting as today.

## How it fits in

1. **`analysis/stage_net.py`, the netlist**: from a path's nets, grow the stage: every two-pin passive with a
   model whose pads both reach the stage's nets, stopping at AC ground; op-amps with their pin roles; other ICs'
   pins on the stage's nets as open (their input capacitance where they are receivers). Pin roles: the schematic
   pin functions (IN+ / +IN / INP, IN− / −IN / INN, OUT / VOUT, V+ / V−), else GT labels `amp_in`, `amp_out` and a
   new **`amp_inv`** (a dual op-amp: `amp_inv:6`), else a note naming the part (the path then falls back to
   today's gain block). Works on an unrouted board too: the pads carry their nets.
2. **`emi/mna.py`, the solver**: stamps, a sparse solve per frequency (stages are tens of nodes: microseconds),
   the descriptor matrices for the exact poles and zeros.
3. **Bode first**: `bode_paths` / `path_responses` take the MNA network when the path has an op-amp stage (or
   always: a passive path gives today's answer, checked); each stage's own transfer as a response beside the
   whole path's ("U301: stage gain" becomes the stage's real closed-loop gain); tiles: gain, −3 dB, and for a
   complex pole pair its **f0 and Q** (a filter's numbers).
4. **Then Simulate's emission estimate**: amplified_path's op-amp stages take the MNA stage's closed-loop gain and
   output impedance instead of the fields (the loop currents per stage stay as they are). Separately, after the
   Bode part is validated, because it moves emission figures.
5. **UI and report**: the Parts tab's amplifier gets `Aol` and (optional) pin roles; the Signal paths tab's path
   text lists each stage's parts; the Bode view's notes name parts without a model (left out) and pins whose role
   was guessed; help, README, ARCHITECTURE, REFERENCES (Ho 1975, Golub 2013; assumptions: single-pole op-amp,
   rails as AC ground, stage tracks as ideal wires).

## Validation (closed forms, tests/test_mna.py and boardgen boards)

- RC, RLC (series and parallel) against their rational functions; a capacitor's ESL notch.
- Inverting amplifier: −R2/R1, its bandwidth GBW / (1 + R2/R1), its closed-loop output impedance.
- Non-inverting ×(1 + R2/R1) with the same bandwidth; a follower: today's answer (refboard's U301 path: 451 Hz).
- **Sallen-Key low-pass** (unity gain): f0 = 1/(2π√(R1R2C1C2)), Q = √(R1R2C1C2)/(C2(R1 + R2)) [Franco 2015];
  **multiple-feedback low-pass**: its f0, Q and gain −R2/R1; both within 1 % where GBW ≫ f0, and the GBW's effect
  (Q enhancement) against the two-pole closed form.
- Poles and zeros: exact (eigenvalues) against the fit of the same sweep and against the closed forms.
- A passive path: MNA and today's cascade agree to 1e-9.
- On the real board: refboard's two ADC paths (follower: unchanged), and a test board with a Sallen-Key and an MFB
  stage in `tests/boardgen.py`.

## Questions for the user before starting

1. The op-amp model: single dominant pole with A0, GBW, R_O, Rin, Cin: enough, or do you want a second pole /
   phase margin (for stability of the filter's loop)?
2. Should Simulate's emission estimate use the stage network too (step 4), or only the Bode sweeps for now?
3. Tracks inside a stage as ideal wires at first: fine?

Estimate: two to three sessions (solver and tests; netlist and pin roles; integration, UI, docs).
