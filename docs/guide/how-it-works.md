# How GroundTruth works

The analyses, how the solver splits the work, and what the numbers mean. Back to the [README](../../README.md).

| Tier | What it does | Speed |
|---|---|---|
| **1 – rule checks** | Finds the copper route from driver to receiver(s), including series 0R and termination parts. For every segment it projects onto the adjacent plane layer(s) in the stackup and flags: gap/split crossings, traces running off the plane, layer changes without a return via (same net) or decap (different net), traces close to a plane edge, and traces referenced to a non-ground plane. | seconds |
| **2 – field solver** | Rasterises the plane copper of **every copper layer** (any layer count) and solves the return current with a quasi-static multi-layer PEEC model: resistive and inductive plane strips, mutual partial inductances between all layer pairs applied as FFT convolutions, exact trace vector potential, and GMRES. Stitching vias and through-hole pads are vertical branches, and they couple to the signal via. Decaps, 0R parts and ferrites between plane nets are lumped branches. **Plane-pair capacitance** comes from the board's physical stackup (dielectric thickness, εr, loss tangent), so the return can also cross between planes as displacement current, and plane resonances show up. The return can therefore change layer and net wherever the copper allows. Results are loop inductance, magnetic moment, the share of return current per layer, the vias, parts and plane pairs that carry it between layers, and plane resonances excited by the net. These are compared with an ideal reference: the route's own reference planes, solid and stitched every 2 mm. From the loop moment and the current on the line at each harmonic it estimates the differential-mode field and compares it with the CISPR 32 class A/B limits. | 1–4 min per board |
| **power stages** | The same solver on each converter's copper: the switching cell as known currents through the switch packages, capacitors and the LISN as branches, pours as solved copper. It gives the hot loop, DC paths, conducted and radiated estimates, and fix suggestions. | 30–70 s per stage |
| **motor bridges** | Each leg solved as a power stage on one model of the bridge, combined with a six-step drive model of the motor currents: conducted emission, capacitor ripple, losses, RMS current maps, victims. | per leg, as a power stage |
| **thermal** | PCB Thermal's solver on the board, heated by the power stages' and bridges' currents and their parts' losses. | after Simulate |
| **DC loads** | A supply rail with the currents its loads draw, solved at DC on the whole board's copper: the drop each load sees (supply and return), current density, loss and the busiest vias. Thermal and Current capacity include it. | part of Simulate |
| **power integrity** | The same field solver on one model of the whole board, from 10 kHz to 1 GHz: each IC's supply impedance between its supply and ground pins, with the decaps, the planes' capacitance, the vias and the regulator together. Each IC's pins also get a fine model of their own surroundings. It gives \|Z(f)\| per IC against the target impedance `V · ripple / I_step`, which part holds the rail at each frequency, the anti-resonance peaks and supply pins with no decap nearby. **Power integrity** tab. | ~10–30 min per rail |
| **signal integrity** | A 2D electrostatic solver on cross-sections cut across the board every 2 mm along each listed net and differential pair, with all the copper, dielectrics and solder mask in the cut. It gives Z0 and εeff, the differential and common-mode impedance, the pair's skew, the crosstalk between tracks side by side and via stub resonances, against targets from the settings or KiCad 10's tuning profiles. **Signal integrity** tab. | seconds, part of Run checks |
| **safety** | Each net's voltage (from its name, the power stages and the parts) checked against clearances (IPC-2221B), capacitor and FET voltage ratings, resistor power and ESD protection. | part of Run checks and Simulate |
| **control loop and responses** | The loop gain of each buck or boost stage from the parts on the board and the controller's schematic fields; Bode plots, poles and zeros of signal paths, power stages and PDN rails, fitted to the solves Simulate already made. | part of Simulate |

Solver validation (`tests/test_solver.py`, `tests/test_multilayer.py`):
- The return-current profile under a microstrip matches image theory (`K(y) = I·h / π(h²+y²)`) to within a few percent.
- Loop inductance agrees with Wheeler's formula to within 1–4 %.
- Inductance rises steadily with slot length.
- At low frequency the current spreads along the resistive path.
- A fully cut plane is reported as a broken return path.
- On a 4-layer layer change, the nearest stitching via carries most of the plane-to-plane transfer, and moving it away enlarges the loop.
- A +3V3 reference plane carries return current only when decaps connect it to GND.
- Plane-pair capacitance matches ε0·εr·A/t exactly, and a plate pair resonates at c/(2a√εr) to within 3 % (`tests/test_capacitance.py`).

**How the solver splits the work.** Each route gets a fine model of its own region, which resolves the loop next to the trace. One coarser board-wide model includes the plane-pair capacitance of the whole board. All routes share it, and it is solved at the signal's actual harmonics. The two models are combined as vectors, so the coarse grid's own error at the fundamental cancels.

**Which harmonics get solved.** Harmonics near the emission limit get more solves. The worst harmonic gets a solve of its own, so the reported worst case is never an interpolation across a resonance.

**Accuracy.** On a dense 4-layer board, two board-wide grids, 0.71 and 0.49 mm, agree to 1.9 dB rms from 200 MHz to 1.6 GHz. They find resonances at the same frequencies, but peak heights can differ by up to 7 dB. A margin set by a resonance peak is therefore good to a few dB. The copper is sampled on a 0.15 mm sub-grid, so thin copper webs between rows of via antipads still conduct and the planes keep their real connectivity.

The emission numbers are meant for ranking and comparing layouts. They are not a compliance prediction, for several reasons:
- the model is quasi-static: it has no retardation and no radiation from plane edges,
- it estimates only the differential-mode loop of one net,
- common-mode currents on cables usually dominate real emissions.
