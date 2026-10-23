# The other analyses

Op-amp circuits and frequency responses, capacitance at DC bias, DC loads, power integrity, the voltage-aware safety checks and signal integrity. Back to the [README](../../README.md).

## Op-amp circuits and the transfer function

A signal path is solved as the circuit it is on the board (`analysis/analog_net.py`, `emi/mna.py`): every part with a model and every op-amp channel on its nets and beyond, up to ground and the supply rails, in any topology (inverting, non-inverting, differential, integrators, transimpedance stages, Sallen-Key / MFB / state-variable filters, instrumentation amplifiers, several op-amps with feedback between them). Every track is a transmission line of its own, every via R + L; an unrouted net is joined as the netlist says. An op-amp is its datasheet's small-signal model (Aol, GBW, a second pole or PM, R_O, Cin, Rin; pins from their names or GT labels); a `Gain` field keeps the old fixed gain block.

The Bode views (Simulate, the Signal paths tab's **Bode**, `groundtruth bode`) show the path's response, each op-amp's output and each op-amp's loop gain with its crossover and phase margin (a tile: amber below 45°, red below 20°), the exact poles and zeros, and the **transfer function**: in the part names for the ideal circuit (SymPy; e.g. a Sallen-Key's `1 / (C1·C2·R1·R2·s² + C2·(R1 + R2)·s + 1)`), and numerically for the full circuit in the normal form `K · Π(1 + s/ω) · Π(1 + s/(Q·ω) + s²/ω²) / […]` with each frequency and Q. Each response also has its **step response** (1 V at the driver: final value, 10-90 % rise, overshoot and undershoot, settling into ±2 %; a plot under the Bode plot, a `step` tile, `--plots` writes `<path>_step.png`). The JSON report's responses carry `transfer_function` (`numeric`, `symbolic`), `step` (its figures and the curve) and a loop gain's `phase_margin`. Simulate's emission estimate takes the path's currents from the same circuit. On refboard's ADC path (ZXCT1107 → TLV6001 follower → 4.7 kΩ / 100 nF → ADC): `H(s) = 1 / (C304·R303·s + 1)`, numerically K 0.9975, poles 451 Hz (the filter), 686 kHz (the follower), the loop's crossover 671 kHz with 89° of margin.

## Capacitance at DC bias

A class II ceramic (X7R, X5R …) loses capacitance as the DC voltage across it rises. A part's `Cbias` (schematic field, InvenTree parameter, or the Parts tab's **C at bias**) is its DC-bias curve: one value (`75nF`, taken at half the part's Rated Voltage), one point (`75nF@50V`: the curve C0 / (1 + (V/V1)²) through it) or 1-12 points (`95nF@10V, 66nF@30V, 39nF@50V, 20nF@80V`, or `-5%@10V, …`: a monotone cubic through them). InvenTree's numeric templates can't hold the `@`: use a text template **Cbias Curve** (it wins), or **Cbias** with **Cbias Voltage**. Every run gives each capacitor its capacitance at the worst DC voltage across it (`analysis/bias.py`): a net's voltage from its name, PDN rails, the power stages and bridges, the settings' `safety: voltages:`, through series parts; else at most the supply of a low-voltage IC on it (an op-amp's, an MCU's: ≤ 18 V), else at most a divider's top; else 0 V (the nominal C, named in a note). C0G / NP0, tantalum and electrolytics keep their C. The Parts tab's **C in circuit**, the report's *Capacitors at their DC bias* table and the JSON's `capacitors_at_bias` show what was used; the plugin's help (Cbias) has a worked chart.

## Frequency responses: Bode plots, poles and zeros

Simulate gives each signal path, power stage and PDN rail its complex frequency responses, with their poles and
zeros (`analysis/transfer.py`):

| Owner | Response | How |
|---|---|---|
| signal path | v at the receivers / v of the driver (lines, series parts, shunts, op-amp stages without slew), each op-amp stage's gain | the path's circuit swept 300 points from a hundredth of its first harmonic to the top of its band; vector fitting |
| power stage | the hot loop's ringing (node current per A of load pulse) | exact rational of L, C_oss, damping and an RC snubber |
| power stage | the input transfer (LISN current per A of loop current) | the conducted band's solves, vector fitted |
| PDN rail | each IC's supply impedance Z(f) | the rail's solves (10 kHz-1 GHz), vector fitted with an s·e term |

Vector fitting (Gustavsen & Semlyen) fits Σ r/(s−a) + d + s·e to the samples, weighting each by 1/|H|; the order
rises until the RMS error is under 1 %, and negligible poles are pruned. The largest error is reported, and a
coarse fit is noted. Poles and zeros are given as natural frequency f0 = |s|/2π and Q = |s|/(−2σ); RHP marks a
right-half-plane zero (a line's delay, approximated). The plugin shows each owner as a **Bode / poles and zeros**
view on the Results tab (magnitude and phase, the s-plane below, the dominant pole in the tiles). The HTML report
puts both plots and a table in each net's, stage's and rail's article, and the console prints the poles and zeros.
Validated against known rational functions and closed forms (tests/test_transfer.py): an RC path's pole at
1/(2π·(R_drv + R)·C) within 1 %, an op-amp stage's at GBW/gain, the ringing's at 1/(2π·sqrt(L·C_oss)) with its Q,
and a PDN rail's poles at its impedance peaks.

## DC loads

A supply rail with the currents its loads draw (a connector feeding other boards, a fused output): the **DC loads**
tab, or `dc_loads:` in the settings (`{name, source: J1.2, source_return: J1.1, max_drop: 200mV, loads: [{pin: J2.2,
return: J2.1, current: 500mA}]}`). Simulate solves it on the whole board's copper: the drop each load sees (supply and
return), the current density and loss, the busiest vias (issues `current_density`, `via_overload`, `dc_drop`);
Thermal and Current capacity include it. In the tab, **Fill from net…** picks the rail's net and ground, then the
part feeding it and the parts drawing from it (each one's rail pin and ground pin found for you); typing in the Source
pin, Source return and Loads fields lists the board's pins with their nets (Ctrl+Space on an empty field), the
fitting ones first.

## Power integrity (PDN impedance)

A supply rail's impedance as its ICs see it between their supply and ground pins, from 10 kHz to 1 GHz: the decaps,
the planes' capacitance, the vias and the regulator together, solved on one model of the whole board. Below a target
impedance `Z_t = V · ripple / I_step` the rail holds its voltage within the ripple whatever the frequency of the IC's
current step. In the settings:

```yaml
pdn:
  - {rail: +3V3, i_step: 300mA}                     # ripple 5 %, the voltage from the name, the source found
  - {rail: VDD_CORE, voltage: 1.2V, ripple: 3%, i_step: 2A, source: U7, vrm_r: 2mΩ, vrm_l: 10nH, check_to: 50MHz}
```

`ground` (default the first reference net), `source` (the part feeding the rail: found as the part with a `power_out`
pin on it, else a connector; `none` for no source), `vrm_r` / `vrm_l` (its output resistance and inductance: a
regulator's flat output impedance and the inductance above its loop bandwidth; defaults 5 mΩ, 20 nH), `parts` (the
ICs to check; default every part with `power_in` pins on the rail and a pad on the ground), `check_to` (the target
applies up to here, default 100 MHz: above, the package and the die decouple), `decap_mm` (default 3 mm). Simulate
gives per IC |Z(f)|, which part holds the rail at each frequency (its share of the current), the anti-resonance peaks,
the inductance it sees above its decaps and the worst margin to the target; issues `pdn_impedance` (over the target,
naming the part holding the rail there) and `decap_missing` (a supply pin without a decap pad within `decap_mm`). The
HTML report and the plugin's **Power integrity** tab (Find rails lists the rails ICs draw from; the Results tab
shows each rail's curves and what holds it at each frequency). An IC whose pads the model's grid cannot join to the
rail's copper (pads smaller than a cell) is left out with a note: raise the resolution. Each IC's pins are also solved
on a fine model of their own surroundings (the inductance at small pads and vias), added to its board-wide curve: on
refboard (+3V3, a 0.3 A step, source U505; 2026-10-19, `tools/dev/pdn_check.py --budgets 40000,80000`) the
0.86 mm and 0.59 mm board-wide grids then agree within 1-5 % at 100 MHz (margins within 0.4 dB; U1102 0.62 Ω against
the 0.55 Ω target); U203 and U305 join only at 0.59 mm (their pads do not reach the rail's copper on the coarser
grid, which a note says).

## Voltage-aware safety checks

What a net's voltage asks of the copper and the parts around it. Each net's voltage comes from: grounds and the
reference nets (0 V); its name (+12V, VDD_3V3, 48V_BUS); a power stage's input rail and switch node (its v_in; a
boost's v_out), its output after the inductor; a motor bridge's bus and phases; a PDN rail; and through parts in
series with (nearly) no drop (shorts, 0R, shunts and resistors up to 10 Ω, inductors, ferrites, a pass FET given a
short or resistor model: its drain and source, not its gate). Your own win:

```yaml
safety:
  voltages: {/Surge/V_BATT: 58V, Net-(U303-BOOST1): 57V}   # peak, V
  coated: false             # external layers with a permanent coating: IPC-2221B B4 instead of B2
  clearance_mm: [[50, 0.6], [100, 1.5]]   # your own table for external layers ([V, mm], e.g. B3 above 3050 m)
  cap_derating: {ceramic: 50%, tantalum: 50%, electrolytic: 80%}
  resistor_derating: 50%
  vds_derating: 80%
  esd_mm: 10mm              # an ESD / TVS part on a connector pin's net this close
  esd_connectors: [J1, J4]  # the board's outside world: warnings (empty: every connector, info)
```

**Run checks** (the whole board, no solve):
- `clearance_voltage`: copper of two nets on one layer closer than IPC-2221B Table 6-1 asks for their voltage
  difference (B1 inner layers, B2 outer uncoated, B4 outer coated; over 30 V only: below, the fab's basic spacing
  applies), measured edge to edge at the closest point, the largest difference at any moment: a switch node swings
  from 0 V to its rail (so beside its rail it is the rail's voltage apart at times), a FET's gate rides on its source
  and a bootstrap net (a capacitor to the switch node and a driver pin) on its switch node, up to `gate_drive_v`
  (10 V) above it, and on through gate resistors (≤ 100 Ω) to the driver's pins. A part's own pads against each
  other don't count (its maker spaces and rates them); a track against them does. An error; a warning when the
  other net's voltage is not known (taken as 0 V; give it under `voltages`).
- `cap_voltage`: a capacitor's voltage against its rating (its **Rated Voltage** field, InvenTree's name, or
  Voltage; else "50V" in its value), derated by kind: over the rating an error, over the derated value a warning.
  Capacitors at 10 V or more without a rating: one info issue naming them.
- `esd_protection`: a connector's pins (not ground, not unconnected) with no ESD / TVS part (by its value or
  library name: TVS, ESD, SMBJ, PESD, TPD, USBLC …) on the same net within `esd_mm`.

**Simulate** (from the solved currents and voltages):
- `resistor_power`: a resistor's loss I²R in the DC paths (all stages together where combined, DC loads) or a motor
  bridge's RMS current, against its **Rated Power** field (or Power; "1/16W" as written; else by package: 0402
  1/16 W … 2512 1 W, 0612 shunts 1 W), derated. Resistors dissipating 50 mW or more without a rating: one info
  issue naming them.
- `vds_rating`: a power stage's switch-node peak (its rail plus the overshoot of the ringing) against the switches'
  **Vds** field, above `vds_derating` a warning, above the rating an error.

On refboard (2026-10-19, the current board, +VDC 55 V as in the scratch settings, uncoated): clearance 38
errors and 13 warnings (since the voltage ranges and riding nets of 2026-10-17 the gate-drive and bootstrap nets carry
their switch node's voltage, so most of the earlier warnings became errors), none coated; ESD infos at J101, J104,
J106, J1101; C601 (10 nF, 50 V) over its rating (65 V between GND and the surge stopper's gate), 23 capacitors over
the 50 % ceramic derating. Simulate of conv1 and conv2: the switch nodes peak at 95.5 / 95.9 V (55 V + the ringing's
~40 V, as the hot-loop check estimates it with the default damping) against the EPC2306's 100 V: over 80 % of it, a
warning each; no resistor over its derated power. (Before, with +VDC 51.8 V: 16 errors, 27 warnings, 89.6 / 89.9 V.)

## Signal integrity

Run checks solves cross-sections of the board along the listed nets and the differential pairs: a cut across every
copper layer perpendicular to the track (every `step_mm`, 2 mm), with all the copper in it (other nets, pours and
planes as the reference at 0 V), the stackup's dielectrics and solder mask, solved as a 2D electrostatic problem
(`solver/xsection.py`: within 0.2 % of Cohn's exact stripline and coupled-stripline results and of Hammerstad &
Jensen's microstrip). From each cut: Z0 and εeff of a line; Zdiff and Zcomm of a pair; the crosstalk coefficients
of two tracks side by side.

```yaml
si:
  impedance:                       # targets: nets by name or pattern, or a KiCad net class (the .kicad_pro)
    - {nets: [/CPU/SPI_*], z0: 50Ω, tol: 10%}
    - {class: HighSpeed, z0: 45Ω, layers: [F.Cu, B.Cu]}
  pairs:                           # `groundtruth detect` suggests them by name (unreviewed)
    - {p: /USB/D_P, n: /USB/D_N, zdiff: 90Ω, tol: 10%, rise_time: 500ps, max_skew: 10ps}
  crosstalk_max: 5%                # NEXT or FEXT of the aggressor's swing
  stub_factor: 3                   # a via stub resonating below 3 × the knee 0.35/t_r
  step_mm: 2
```

**KiCad 10 tuning profiles** (Board Setup > Tuning Profiles, assigned to net classes) are read from the project
file: a profile's target impedance becomes its class's Z0 target (a Differential profile's, its pairs' Zdiff), and
with time-domain tuning on, its unit delay per layer is checked against the cross-sections' (`tuning_delay`: KiCad
converts lengths to delays with the figure typed there; FR-4 microstrip is ~60 ps/cm, not 100). A profile on the
Default class applies to the listed nets only.

Issues: `impedance` (a net's Z0 outside its target, with the length and the worst cut), `diff_impedance` (a pair's
Zdiff where its nets run together), `diff_skew` (the routed lengths' delay mismatch over max_skew, by default a tenth
of the edge), `crosstalk` (a listed net into a track beside it: NEXT = K_b·min(1, 2·T_d/t_r), FEXT = |K_f|·T_d/t_r,
from the pair's cross-section at its closest stretch and the coupled length), `via_stub` (a via's barrel past the
layers its net uses, resonating at c/(4·l·sqrt(εr))). Listed nets without a target get their Z0 shown. Pairs are
found by name (_P/_N, +/-, CAN_H/CAN_L, DP/DM, X and X*); USB gets 90 Ω, Ethernet / LVDS / HDMI / MIPI / SATA 100 Ω,
PCIe 85 Ω, CAN and RS-485 no target. In the plugin: the **Signal integrity** tab (pairs, targets, limits, Find
pairs, each row's result after Run checks); the console, JSON and HTML reports carry the figures.

On refboard (6 layers, 0.1 mm to the planes): the SPI nets 43.1-43.7 Ω (SPI_MISO up to 62 Ω over 0.27 mm
where In1 has a void under it), SW_CLK 35.6-36.7 Ω on its inner layer, CAN_H / CAN_L 88-98 Ω differential, routed
13.0 / 15.6 mm (17 ps apart), the strongest crosstalk 1.7 % (ADC_I_RAIL_2 into ADC_V_RAIL_2, 42 mm side by side),
the lowest via stub resonance 25 GHz. Run checks with these 8 nets and the pair: about 10 s more (156 cuts, solved in
the solver threads).
