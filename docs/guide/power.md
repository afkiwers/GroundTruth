# Power stages, motor bridges and their temperatures

DC-DC converters, motor bridges, what they do to the rest of the board, their control loops and the thermal solve. Back to the [README](../../README.md).

## Switching power stages (DC-DC converters)

A converter is not a signal net. Its switches move the load current between the high-side and the low-side switch within the edge time. That fast current flows around the **hot loop**: input capacitors, high-side switch, switch node, low-side switch, ground and back to the capacitors. groundtruth models the loop as copper, not as lines:

- **Copper:** every net the loop touches is solved, pours and tracks alike. That covers the rail, the switch-node pour, ground, the input path up to the connector, and every plane in the region. A route that runs through its own net's pour is solved through the pour too, so the current spreads over the pour's width and stitching vias.
- **Capacitors:** they are branches with their C, ESR and ESL. The solver gives each one's share of the loop current, at the switching frequency (ripple) and at 100 MHz (the edges).
- **Switches:** they are known currents through each package, injected into the copper at every pad.
- **Conducted emission:** with an input connector, a LISN per line closes the input. The solver then gives the share of the pulsed current that reaches the input lines.

```yaml
converters:
  - {name: buck 5V, topology: buck, rail: VIN, switch_node: SW, ground: GND, high_side: Q1, low_side: Q2,
     f_sw: 400kHz, v_in: 24, v_out: 5, i_out: 3, rise_time: 5ns, c_oss: 1nF, input_connector: [J1.1, J1.2],
     output_connector: [J2.1, J2.2],          # optional: the load (DC output path and ground return)
     c_stray: 20pF,                           # optional: switch node to chassis/heatsink (common mode)
     spread: 0.2, spread_mode: triangle, spread_rate: 10kHz}   # optional: spread spectrum (f_sw … +20 %)
  - {name: 3V3 regulator, rail: +12V, ground: GND, switch: U5, f_sw: 2MHz, i_out: 0.5, v_in: 12, v_out: 3.3}
analysis: {conducted: cispr32, conducted_class: B}   # cispr32 A/B | cispr25 1-5 | mil461 basic/115V/... | none
rules: {overshoot_warning: 0.25, overshoot_error: 0.5}   # switch-node overshoot as a fraction of the rail voltage
```

| Key of a `converters:` entry | Default | Meaning |
|---|---|---|
| `name` | the switch node | Its name in reports and `--nets`. |
| `topology` | `buck` | `buck`, `boost` or `custom` (sets the pulse amplitude and duty). |
| `rail`, `ground`, `switch_node` | | The hot loop's capacitor net (a buck's VIN, a boost's VOUT), power ground, switch node. |
| `high_side`, `low_side`, `switch` | | Parts (one or a list): discrete switches and a catch diode, or `switch` for an IC with both switches inside. |
| `f_sw`, `i_out`, `rise_time` | 500 kHz, 1 A, 10 ns | Switching frequency, load current, current commutation (switch-node edge) time. |
| `v_in`, `v_out` | | Voltages; they also give the duty of a buck or boost. |
| `duty` | from `v_in`/`v_out`, else 0.5 | Fraction of the period the high side conducts. |
| `c_oss`, `q_oss`, `r_damp` | –, –, 0.1 Ω | Switch output capacitance (ringing) and charge (switching loss), ringing damping. |
| `c_stray` | | Switch node to chassis or heatsink: common-mode estimate. |
| `input_connector`, `output_connector` | | `[plus, minus]` pins: the LISN, and the load (DC output path and ground return). |
| `spread`, `spread_mode`, `spread_rate` | –, `random`, per cycle | Spread spectrum (see below). |
| `interleave`, `phase_deg` | –, 360°/n apart | Interleaved stages: a group name shared by the phases of one controller, and this stage's delay in degrees of the switching period (see below). |
| `conducted` | `analysis.conducted` | This stage's conducted standard. |
| `rds_on`, `rds_on_low`, `dcr`, `diode_vf` | –, `rds_on`, –, 0.5 V | Losses for the thermal solve (see "Temperatures"). |
| `source`, `reviewed`, `note` | `manual`, true | Set by detection (`auto`, unreviewed) and its note. |

- **Topologies:** discrete stages use `high_side` and `low_side` (a catch diode counts as the low side), and the high side can be an IC with its own switch. An IC with both switches inside is listed as `switch`; its hot loop runs from its input pins to its ground pins. A boost's hot loop sits on its output, so `rail` is VOUT. A boost's conducted emission comes from its inductor ripple (ΔI = V_in·D/(L·f_sw), with `v_in`, `v_out` and the inductor's L), drawn from the inductor's input net.
- **Results:**
  - the hot-loop inductance and resistance;
  - the L·di/dt overshoot;
  - with `c_oss`, the ringing frequency, its Q and the overshoot, plus the spectrum shaped by that resonance (damping `r_damp`, default 0.1 Ω plus the loop resistance). The spectrum also carries the charge each edge moves into the switch node (C_oss·dv/dt, or `q_oss` per edge when given; with a snubber its current too) on top of the load pulse: the loop current is the pulse × |1 + Y_node·V/I|. Near the ringing this dominates when V/I is large: on refboard's 51.8 V stages it is 3× the pulse at 10 A and 6× at 5 A. Without `c_oss` (an integrated stage, a catch diode) neither the ringing nor this current is modelled, so give the diode's junction capacitance as `c_oss` for a diode buck;
  - the capacitor current sharing;
  - the radiated estimate of the hot loop, solved as for nets, plane capacitance included;
  - the conducted emission at the input against CISPR 32 (class A/B average limit), CISPR 25 (voltage method, classes 1–5) or MIL-STD-461 CE102, with the standards' receiver bandwidths.
- **DC power paths** (current density and resistance in the pours): the load current from the switches through the switch node to the inductor, and the input current from the input connector to the high side. The input current is the expected one, from an energy balance: output power plus the losses Thermal uses (conduction, switching edges, output charge, diode, inductor DCR, series parts) plus the DC paths' copper, over the input voltage (no gate drive or core loss); it is shown beside the connector, with the efficiency. With `output_connector: [J2.1, J2.2]` (the load's + and − pins; `groundtruth detect` finds a connector), two more: the output from the inductor (a boost: from its high side) to the load, and the ground return from the load, which splits by Kirchhoff between the low side and the input connector (the input connector takes back the input current). All DC paths of all stages are solved on one copper model of the whole board, so nothing is cut off at the edge of a box around the stage. Parts join the copper over the whole area of their pads: each pin takes its share of the current by its copper area, pads drawn on top of each other (an EPC GaN FET's source pin: a bar under three tabs) counted once.
  - **All power stages together:** with two or more stages, the Results tab's Route list starts with **all power stages DC currents together**: every stage at its own load at once, their currents added (they share the input connector, a surge stopper, the rail and the ground). Its title gives the loads, the total input current and the copper loss. The current-density and via-load checks run on this total, as issues of *all power stages*, in place of each stage's own DC checks. The HTML report shows it first under Power stages (input current, copper loss, hotspots, the busiest vias, the map). Each layer's copper thickness comes from the stackup. The results are the resistance, the voltage drop and loss, the current density in A/mm², and the current in each via and its plating. The density is reported both as the peak cell and as "sustained", averaged over 1 mm: corner peaks at pads don't heat copper, a narrow neck does. Every DC path is also a heatmap, in the plugin's Results tab and in the HTML report. Thresholds: `rules: {current_density_warning: 50, current_density_error: 100}` (A/mm², copper and via plating).
- **Input filter** (guidance): when the conducted estimate misses the margin, the report proposes a π-filter: a series L and a capacitor at the connector, with the stage's input capacitors on the other side. Its corner f_c is set so the 40 dB/decade roll-off gives every harmonic its missing margin plus 6 dB. The proposal includes an example L/C pair and the usual RC damping (R = √(L/C) in series with 4·C). It is first-order guidance: check the damped filter against the converter's input impedance (Middlebrook). The
fix advisor's proposed filter is also drawn: its attenuation among the stage's Bode responses, and the filter's
output impedance next to the converter's input impedance −V²/P (Middlebrook's two curves) in the stage's
"impedances" view.
- **Spread spectrum:** with `spread: 0.2` the switching frequency is dithered from f_sw to f_sw + 20 %. GroundTruth simulates the EMI receiver (its bandwidth, detector and dwell time) on the swept harmonics instead of using a rule of thumb, because the benefit depends strongly on the modulation. A slow sweep passes a peak detector at full amplitude. Random changes every cycle barely spread the line. A sweep at ~10 kHz gained ~9 dB at the fundamental on the example board. Set `spread_mode: random` (with `spread_rate` hops per second, default every cycle) or `triangle` (with `spread_rate`, the modulation frequency). Datasheets often omit the modulation, so try both. The radiated estimate of the stage is read the same way. Where the sweeps of neighbouring harmonics overlap (high harmonics), a peak detector reads the largest single pass: the harmonics are f_sw apart, more than the receiver bandwidth, so only one is in the filter at a time.
- **Interleaved stages:** the channels of a dual or multiphase controller (refboard's LTC7890 runs SW1 and SW2 180° apart) share one `interleave:` group name, each with its `phase_deg`. Simulate then adds their input currents at the LISN and their hot loops' fields as phasors per harmonic, with each stage's own transfer and loop moment. 180° apart they cancel at the odd harmonics of f_sw and add at the even ones. Each member stage reports the group's margins together, the same sum in phase for comparison, and its own. `groundtruth detect` proposes a group for the stages one IC switches (0° and 180° for two): check the controller's datasheet. The stages need one f_sw and the same spread spectrum. CE101 is combined the same way (for stages switching within its range), against the limit at the stages' input current together. Tested end to end on two bucks, two boosts (their inductor ripple) and a 2 kHz pair's CE101.
- **Common mode** (budget): the switch node swings the rail voltage with the switching edges. Through its stray capacitance to a chassis or heatsink, that drives a common-mode current back through both LISNs. That capacitance depends on the mechanics, not the board. groundtruth therefore reports the budget: the largest switch-node capacitance to the chassis that keeps the conducted reading under the limit, and the frequency where it binds. With `c_stray: 20pF` in the converter entry it also estimates the common-mode emission and checks it.
- **What the radiated estimate is:** the hot loop's own (differential-mode) radiation at the limit's distance. Cables driven by the switch node's common-mode voltage usually radiate more, so read it as a lower bound for the product, not a compliance prediction.
- **Issues:** `hot_loop` (overshoot), `current_density`, `conducted_emission`, `emi_margin` and `plane_resonance`. A loop that cannot be closed in the copper gives `power_loop_broken`. When the loop closes only through copper beyond the default 5 mm around the stage, the model region widens (10, then 20 mm) and a note says so: that detour is usually the finding.
- **How to fix it** (every Simulate; the Fixes tab's *Design changes* list, each stage in the reports): for a stage with ringing, radiated or conducted issues, the design changes that clear them. Each comes with its parts, the predicted radiated and conducted margins, the overshoot, the extra loss and the issues it clears. They are worked out on the stage's own solves (no new solve), so the stage's row reproduces its reported figures:
  - an RC snubber across the switch node (C a few × C_oss, R ≈ √(L/C_oss), its C·V²·f_sw loss);
  - a slower edge (gate resistor; ½·V·I·Δt·f_sw);
  - the largest hot-loop inductance that meets the targets, if a layout can reach it;
  - the cheapest mix of these when none alone does;
  - a damped input π-filter from standard values, checked against the converter's input impedance (Middlebrook);
  - spread spectrum through the simulated receiver;
  - the common-mode choke and Y capacitors for assumed strays.

  The targets are `rules.emi_margin_db` and `overshoot_warning`, or 80 % of the switches' `Vds` field. **Check in Simulate** (Fixes tab) re-runs a stage with a snubber, edge time or spread applied in memory. A converter can list its snubber as `snubber: [C12, R12]` (parts on the board, also detected) or `snubber: {C: 2.2n, R: 1.5}` (proposed values, modelled as if fitted).
- **Fix suggestions** (`groundtruth suggest`, the Fixes tab) also cover power stages, with two kinds of candidate:
  - stitching vias: a rail-capacitor or switch pad with no via of its net within `stitch_distance_mm` gets a via of that net next to it, landing in the net's copper on the pad's layer and on another layer;
  - capacitors: 100 nF (0402 size) right at the switches, between the high side's rail pads and the low side's ground pads, each pad with a via to its plane. Use a part rated for the rail voltage.

  Each candidate's change of the hot-loop inductance and moment is predicted on the converter's model. Candidates are picked greedily: each round adds the one that helps most with the earlier picks in place, until the next adds less than 1 % or `--max-fixes` is reached. The picks are then verified together, and the comparison lists the hot-loop L and the radiated and conducted margins before and after. Capacitors are suggested, never placed automatically.
- **Detection:** `groundtruth detect` (or **Detect…** on the plugin's Converters tab) also suggests power stages. A net between an inductor and a switching transistor, a catch diode or an IC's SW/LX pin is a switch node. The transistor whose other power pin is on ground is the low side. The input connector is found through series parts (0R, fuses, ferrites, filter inductors, low-ohm resistors). A series switch such as a surge-stopper or reverse-polarity FET is recognised and crossed as closed. It must be a gate-driven FET whose drain and source sit on two supply nets, neither one ground nor an inductor's (a switching FET always touches its inductor). Detection then names it and gives it a `short` model, unless you already set a model for it yourself. Replace that with a `resistor` model of its R_DS(on) for the DC path. Switching frequency, load current, voltages and edge time cannot be read from the board: suggestions start with defaults to review.
- **Limits of the model:**
  - Only differential-mode emission is modelled. Common-mode emission (switch-node dV/dt through stray capacitance to a chassis or heatsink) often dominates above a few MHz.
  - The limit tables were entered from the standards as commonly published; check them against the edition your test plan cites.

## Motor bridges (BLDC drives, ESCs)

A motor bridge is a set of half-bridge legs on one bus: in each leg a high-side switch from the bus to the phase node and a low-side switch from the node to ground. The phase node goes to the motor, whose winding is the inductor, so there is none on the board. The plugin's **Bridges** tab and the `bridges:` section of the settings file hold them; `groundtruth detect` (or **Detect…**, and the first open of a board) finds them:

- a leg is a node with a transistor to ground and one to a non-ground bus, and no inductor on the node or behind a 0R, shunt or ferrite (that would be a converter's switch node); a FET pair without a FET to ground (a load switch, an ORing FET) is no leg;
- legs on one bus and ground form a bridge; each leg gets its phase (from the motor connector's pins, J1.A, or the node's name, PH_A), its gate driver (the IC with a SW / SHx pin on the node) and the driver's input nets (where the PWM comes from), its RC snubber, and the motor connector pin;
- the bus input is found through series parts, with current-sense ICs (pins IP+ / IP-) and ideal-diode or reverse-polarity FETs taken as closed (suggested `short` models; a sensor's model joins only its IP+ and IP- pins: `pins: [IP+, IP-]`), or as two wire tabs (an ESC's battery leads, the minus one behind its shunt).

What you set (Detect cannot read it from the board): the bus voltage, PWM frequency, phase current (the mean current of the conducting phases; `mode: motoring | generating` gives its direction), speed (0 for a locked rotor), the PWM scheme (`hpwm_lon`: the sourcing phase's high side chops, the sinking phase's low side stays on; `hon_lpwm`; `bipolar`), synchronous or not, the dead time and edge time, the switches' R_DS(on), reverse-conduction voltage, C_oss (all parallel switches together) and the motor: star or delta, line-to-line R and L, the back-EMF constant (line-to-line peak per 1000 rpm, as measured at the terminals) and pole pairs.

```yaml
bridges:
  - name: bridge J1
    rail: V_BLDC
    ground: GND
    legs:
      - {phase: A, switch_node: PH_A, high_side: [Q1, Q13], low_side: [Q2, Q14], output: J1.A, snubber: [C18, R82]}
      - {phase: B, ...}
      - {phase: C, ...}
    input_connector: [J5.1, J5.2]
    v_bus: 48
    f_pwm: 20kHz
    i_phase: 100
    mode: motoring
    speed_rpm: 3000
    pwm: hpwm_lon
    dead_time: 47ns
    rise_time: 2ns
    rds_on: 1.4m
    c_oss: 2nF
    q_oss: 170nC
    motor: {connection: star, r_ll: 20m, l_ll: 40µH, ke_ll: 5, pole_pairs: 7, emf: trapezoidal}
    sweep: {i_phase: [50, 100, 200], speed_rpm: [0, 1500, 3000]}
```

| Key of a `bridges:` entry | Default | Meaning |
|---|---|---|
| `name`, `rail`, `ground` | | The bridge, its bus net and ground. |
| `legs[]` | | The legs (six-step needs three): `phase`, `switch_node`, `high_side`, `low_side` (parts, parallel FETs as a list), `output` (motor connector pin), `driver` (gate driver IC, information), `inputs` (the driver's input nets), `snubber` (`[C, R]`). |
| `input_connector` | | `[plus, minus]` where the bus comes in (the LISN goes there). |
| `conducted` | `analysis.conducted` | This bridge's conducted standard. |
| `v_bus`, `i_phase`, `speed_rpm` | | Bus voltage, mean current of the conducting phase pair (positive), mechanical rpm (0: locked rotor). Needed to simulate. |
| `mode` | `motoring` | `motoring` or `generating`: the current's direction. |
| `f_pwm`, `pwm` | 20 kHz, `hpwm_lon` | PWM frequency and scheme (`hpwm_lon`, `hon_lpwm`, `bipolar`). |
| `synchronous` | true | The chopping leg's other switch conducts in the off time. |
| `dead_time`, `rise_time` | 50 ns, 2 ns | Before every turn-on; switch-node edge. |
| `d_max` | 0.98 | Largest duty (bootstrap refresh). |
| `rds_on`, `rds_on_low` | –, `rds_on` | R_DS(on) of one high-side / low-side FET. Without it the conduction is left out. |
| `v_sd` | 2 V | Reverse conduction of an off switch (GaN about 2 V, a Si body diode about 0.8 V). |
| `c_oss`, `q_oss`, `r_damp` | | As for a converter; `c_oss` and `q_oss` for one side's parallel switches together. |
| `c_stray` | | Phase node, cable and motor frame to chassis (common mode). |
| `motor` | | `connection` (`star`/`delta`), `r_ll` and `l_ll` (line to line, needed), `ke_ll` (V line-to-line peak per 1000 rpm) and `pole_pairs` (needed with a speed), `emf` (`trapezoidal` BLDC / `sinusoidal` PMSM), `advance_deg` (commutation advance, electrical degrees, default 0). |
| `sweep` | | `i_phase` and `speed_rpm` lists: extra operating points, every current at every speed, at most 64. |
| `source`, `reviewed`, `note` | `manual`, true | Set by detection. |

Simulate then:

- **drives the motor in six-step** (Hall commutation) at the operating point: the phase, switch and bus currents in steady state, with the PWM, the dead time, reverse conduction while a switch is off, and each commutation (the outgoing phase's current running out, back into the bus for a held-low phase). The duty is found so the phase current is reached; if the bus voltage cannot drive it at that speed, a `bridge_operating_point` issue says what is reached. Star and delta motors are the same at the terminals for a sinusoidal back-EMF; for a trapezoidal one the delta's circulating current is reported;
- **solves each leg's hot loop** (bus capacitors, high side, phase node, low side, ground) on one model of the bridge, at the leg's largest switched current: loop inductance, L·di/dt, ringing with C_oss (damped by the leg's RC snubber) and the radiated estimate, as for a power stage;
- **adds the legs at the input**: each leg's high-side current spectrum (exact, every electrical-frequency sideband of every PWM harmonic) times its share reaching the LISN, read by a simulated EMI receiver (its bandwidth, peak or average detector), against the conducted limit (`conducted_emission`);
- **shares the ripple among the bus capacitors**: each capacitor's ripple current (RMS) at the operating point.

- **counts the parts' losses** from the drive: each FET's conduction (R_DS(on), parallel FETs sharing the current), reverse conduction in the dead times (V_SD), the hard turn-on (overlap ½·V·I·t_r and the output charge Q_oss·V; `q_oss` and `c_oss` are one side's parallel switches together), the snubber resistors (½·C·ΔV² per swing), the bus capacitors' ESR and the series parts on the battery path (give a current sensor or ideal-diode FET a `resistor` model with its real resistance: a `short` gets no loss, only a note). Turn-off is taken as soft (GaN); the gate drive's loss is left out;
- **maps the RMS current density** from the battery to the motor: the phase current through each leg to its motor pin, the bus current from the battery, and the switching currents in the hot loops, as RMS values (what heats the copper), with each via's RMS current against its IPC-2221 capacity (`current_density`, `via_overload`);
- **sweeps operating points** if you list them: `sweep: {i_phase: [50, 100, 200], speed_rpm: [0, 1500, 3000]}` (or the Bridges tab's *Sweep* fields) evaluates every current at every speed from the same solves, a few seconds per point: duty (and whether the current is reached), losses, the busiest capacitor, the RMS density, the conducted and radiated margins.

On the Results tab a bridge's first view shows its drive (the phase and bus currents over one electrical period, with a commutation zoomed in), the capacitors' ripple currents, the conducted spectrum and the losses tile; its legs follow as power-stage views with their maps, then **RMS currents** (the RMS current density map, copper loss, busiest via) and **sweep** (losses, RMS density, conducted and radiated margin against the phase current, one line per speed; a hollow marker where the current is not reached). **Thermal** and **Current capacity** include the bridge: its RMS copper heat and its parts' losses; the capacity is the phase current at which the hottest copper or FET junction reaches the limit. **Suggest fixes** offers each leg's hot-loop fixes (stitching vias, a capacitor at the switches) for the leg's and the bridge's issues. Not yet: H-bridges (two legs).

## What a bridge or a power stage does to the rest of the board (victims)

A motor controller that resets under load is often not short of supply but hit by its own bridge: a ground that moves
between the microcontroller and what drives its pins, a reset or PWM line picking up the hot loop's field, a track
facing a phase node across a stack's gap. When a bridge or a power stage is simulated, GroundTruth also looks at
the **victims**:

In the plugin, **Victims…** on the Bridges tab picks the victim nets: the board's nets on one side (filtered as you type), the victims on the other with why each is one; add or remove one or several at once. The automatic ones are shown until the first change, which keeps them in the list; **Reset to automatic** goes back.

- the board's microcontrollers and reset supervisors (by their value: STM32, GD32, ATSAM, PIC, TCM809, MAX809 …);
- its reset, crystal, Hall, debug and PWM / gate-input nets (by name), when they are routed;
- or the ones listed in the settings (`victims: parts:` and `nets:`, which replace the automatic choice).

Each leg's (each stage's) hot loop is solved once more on a model that also holds the victims, at the edge's
frequencies, and the bridge's drive view (the stage's part of the report) gives, at the worst leg's switching edges
(the stage's: its pulse current over its edge, the switch node swinging to its input voltage):

| Result | What it is | Threshold (settings) |
|---|---|---|
| **noise** at each victim net's receiver | The net is in the model, driven at its driver pin (a pull-up drives with its own value, else `drive_ohms`, 50 Ω) and loaded at the receiver pin by `input_pf` (5 pF), each to its local ground. The voltage across the receiver's input: its ground moving against the driver's, plus what the hot loop induces along the track and across any connector pins it crosses. As a coupling inductance M, the peak is M·I/t_r. | `induced_warning_v`, 0.3 V |
| **injected** current | A victim track facing a phase node's copper on another layer with no copper layer between them (a logic board's bottom layer over the bridge's pours) has the capacitance ε0·εr·A/h to it and takes C·V_bus/t_r at every edge. | `injected_warning_ma`, 1 mA |
| **ground** of a victim part | Its ground pins' potential against the reference pin (the bridge's input minus), as an inductance. Information only: a part's ground alone does not upset it. Set `reference:` to a pin it takes signals or supply from to have it checked. | `shift_warning_v`, 0.3 V |

Values over their threshold raise `victim_coupling` issues. A high-impedance net (a reset with a 10 kΩ pull-up)
sees little of what is induced in its loop, since most of it drops across the pull-up, but takes all of an injected
current; a low-impedance one the other way round. `victims: {enabled: false}` skips all of it (it costs one more
model per leg or stage). On the discrete buck test board (12 V, 3 A, 2 ns edges) a reset line 2 mm from the input
pour, pulled up by 10 kΩ, sees 0.15 mV at the microcontroller's pin; routed away in the far corner, less than half
the coupling (tests/test_stage_victims.py).

**Stacked boards (experimental, on hold).** An ESC built as a power board with a logic board above it is one model
when the power board's settings list the other board. This is limited: one other board only, the bridge must be on
this board, it is set in the settings file only (no plugin form), and Thermal, drawing and the 3D view do not cover
the other board. Stacks with more boards (a leg per board, the bus capacitors on their own board) are not supported;
work on stacks is paused.

```yaml
stack:
  - name: logic                  # its parts and nets become logic:U8, logic:/CPU/NRST
    file: ../logic/logic.kicad_pcb   # relative to this board's folder
    gap: 3.2mm                   # between the two boards' facing surfaces
    connectors: [[J7, J1]]       # [this board's connector, the other board's], pins joined by number
    side: above                  # or below
    flip: auto                   # auto | no | yes: the other board turned over (its top facing this board)
victims:
  nets: [logic:/CPU/NRST, logic:/CPU/PWM_AH]   # optional: otherwise found
  drive_ohms: 50
  input_pf: 5
```

The other board is placed from the connectors' pins (the rotation and offset that lay its pins on ours; the run's
notes say how well they line up). Its copper sits at its height, air in the gap; every joined pin becomes a vertical
conductor through the gap, so a ground or a PWM line that crosses on a few pins carries their inductance, and the
nets a pin joins become one net. Nothing is written to either board.

Limits: the edges are taken as the leg's largest hard-switched current over the edge time (the same as L·di/dt);
the noise is a peak estimate at the hot-loop frequency, not a waveform; the switch node's ringing, the motor cable
and anything off the boards are not in it; what the microcontroller or its supervisor tolerate is yours to set as
thresholds (a reset pin's glitch filter, a supervisor's threshold).

## Control loop of a buck or boost stage

Simulate gives each buck or boost stage with a controller on the board its loop gain T(s): crossover, phase margin, gain
margin, the Bode plot and the poles and zeros (exact: the model is a rational function of s). The parts come from
the board, the controller's figures from its symbol fields (HTTP lib parameters):

- **the controller**: the IC with a pin on the stage's switch node (SW1 of a dual controller picks channel 1: ITH1,
  V_FB1, SENSE1±), by pin names (FB / VFB / VOSENSE, ITH / COMP / VC, SENSE± / ISNS± / CS±);
- **the compensation** on ITH / COMP: a series R-C to ground and a small C to ground; **the divider** on FB: the
  resistors (and a feed-forward C) from the output on top, to ground below (a resistor from another net, an output
  adjust input, as an AC ground); **the sense resistor** between the SENSE pins' filter resistors (in parallel), or
  the inductor's DCR; **the output capacitors** from the output to ground (C at bias, ESR) and the load V_out / I_out.

The controller's fields (`components/fields.py`, the help's field table):

| Field | Unit | |
|---|---|---|
| `Control Mode` | text | peak current, voltage, COT or internal (the last two: a note, no loop model) |
| `Vref` | V | the feedback reference |
| `EA gm` | S ("1.8mmho") | a transconductance error amplifier: the compensator gm·Z(COMP) |
| `EA Ro` | Ω | its output resistance (gain gm·Ro at low frequency; empty: an integrator) |
| `EA Gain`, `EA GBW` | dB, Hz | instead of gm: an op-amp error amplifier (Z_f / Z_in, ideal) |
| `Vsense Max`, `ITH Range` | V, V | peak current mode: the current-sense gain k = Vsense Max / ITH Range |
| `Current Sense Gain` | V/V | or k directly |
| `Slope Compensation` | V/s ("50mV/us") | at the sense input; empty: half the inductor's down-slope |
| `Ramp` | V | voltage mode: the PWM ramp |

The model (Ridley; Erickson & Maksimovic): peak current mode G_vc = Z'(s)·F_h(s)/R_i with R_i = R_sense/k,
Z' = 1/(Y_o + T_s·(m_c·D' − ½)/L), m_c = 1 + S_e/S_n, the sampling double pole F_h at f_sw/2 with
Q_p = 1/(π·(m_c·D' − ½)); voltage mode G_vd = (V_in/V_ramp)/(1 + (sL + R_L)·Y_o); Y_o = Σ sC/(1 + sC·ESR) + 1/R_load.
A boost (its output is its rail, behind the rectifier; D' = V_in/V_out, a = I_L/V_out, Z_L = sL + R_L): peak current
mode G_vc = (D' − a·Z_L)/(Y_o + a·D' + G·(D' − a·Z_L))/R_i·F_h with G = T_s·D'²·(m_c − ½)/L (the averaged
peak-current law with the duty cycle taken quasi-statically from the inductor, which gives Ridley's buck model
above exactly), voltage mode G_vd = V_out·(D' − a·Z_L)/(Z_L·Y_o + D'²)/V_ramp; both have the right-half-plane zero
at D'²·R_load/L. Continuous conduction is assumed (a note when the load is lighter).
Issues: `loop_phase_margin` (under 45°, or a gain margin under 6 dB: a warning; under 30°: an error),
`loop_crossover` (above f_sw/5, or a boost's above a fifth of its right-half-plane zero), and m_c·D' ≤ ½
(subharmonic oscillation) as the stage's note. Overrides per stage:
`converters: [{..., loop: {mode: current, gm: 1.8m, cs_gain: 0.05, slope: 50mV/us, r_sense: 4m}}]`.

On refboard (U303's fields set on 2026-10-17 at the user's "add those yourself": Control Mode peak current,
Vref 0.8V, EA gm 1.8mmho, Vsense Max 50mV for ILIM floating, ITH Range 1V from the datasheet's
Figure 13, "Maximum Current Sense Threshold vs V_ITH": 0 mV at 0.4 V to 50 mV at 1.4 V; U502 COT, U503 internal): conv1 (28 V, 10 A)
crosses over at 11.2 kHz with 77° of phase margin and 20.2 dB of gain margin (Simulate of 2026-10-18; 22.6 dB when
this was first written), conv2 (5.4 V, 5 A) at 29 kHz with 91°
and 20 dB (R311 22 kΩ + C313 2.2 nF (1.9 nF at bias) + C311 5 pF; R306 / R307 and R331 / R332 with R305 / R333 from
ADJ_RAIL as AC grounds; R309 ∥ R310 and R328 ∥ R329, 4 mΩ; slope compensation assumed at half the down-slope).
`tools/dev/loop_check.py BOARD SETTINGS [--set conv1:ith_range=0.8] [--png DIR]` prints a stage's loop without a
Simulate.

## Temperatures (thermal solve)

**Thermal** (top bar), or `groundtruth simulate --thermal`, solves the copper and part temperatures of the power
stages after Simulate, with the solver of PCB Thermal (validated against analytic cases; `groundtruth.thermal`).
Motor bridges heat the board with their RMS currents and their parts' losses (see "Motor bridges").

- **Heat:** the power stages' DC paths, as Simulate computed them (switch node → inductor, input, output and
  ground returns, with the switching split of the currents), heat the copper: ρ(T)·|J|² with the paths' current
  vectors added where they share copper. The parts get their losses at the operating point: the switches'
  conduction (D·I²·R_DS(on), (1−D)·I²·R_DS(on); a catch diode (1−D)·I·V_f), the inductor's I²·DCR, the series
  parts on the DC paths (shunts, a surge stopper's FET), and switching on the hard-switched transistor (a buck's
  high side, a boost's low side):
  - **edges:** ½·V·I·t_edge·f_sw per hard edge. Synchronous (a FET rectifies, or an IC with both switches):
    only the turn-on edge, because at turn-off the inductor current carries the switch node across while the
    channel is already off. With a catch or boost diode: both edges.
  - **output charge:** Q_oss·V·f_sw synchronous (the switch dumps its own charge and charges the other FET's
    through itself: for two equal FETs together Q_oss·V per cycle); ½·Q_oss·V·f_sw with a diode (its own energy).
    Without `q_oss`, C_oss·V² stands in for Q_oss·V. For GaN use Q_oss: its capacitance falls steeply with
    voltage, so the small-signal C_oss at the bus voltage underestimates the loss (EPC2306 at 52.8 V, 750 kHz:
    1.5 W from Q_OSS 38 nC, 1.0 W from C_OSS 482 pF).
  - **dead times** (a synchronous rectifier FET): both switches are off before each turns on, one dead time per
    switch-node edge (`dead_rise`: low side off → high side on; `dead_fall`: the other; `dead_time` sets both), and
    the inductor current flows through the rectifier in reverse at `v_sd`. At the hard edge (the main switch's
    turn-on: a buck's rise) for the whole dead time, at the ripple's valley current; at the soft edge, at the peak,
    only after the current has swung the node across (both FETs' output charge 2·Q_oss). Loss V_SD·f_sw·(I_v·t_hard
    + I_p·max(t_soft − 2·Q_oss/I_p, 0)) in the rectifier, both dead times taken out of its channel's conduction time.
    With GaN (V_SD ≈ 1.5-2 V) often as much as the conduction loss. The dead times come from the settings or a GT
    field on the controller IC (`GT = dead:20ns`; per channel `dead1:20ns`; per edge `dead_rise:20ns`,
    `dead1_fall:10ns`; per labelled converter `conv1_dead:20ns`); without one 20 ns per edge (an adaptive
    controller's), with a note.
    `v_sd` from the rectifier FETs' `Vsd` field, else 2 V for GaN, 0.8 V otherwise.
  - **reverse recovery** (`q_rr`, a silicon FET's body diode or a PN catch diode; the FETs' `Qrr` field; GaN has
    none): Q_rr·V·f_sw in the main switch at its hard turn-on. The same charge is a current spike in the hot loop,
    I_rr = √(2·Q_rr·di/dt) over t_rr = √(2·Q_rr/(di/dt)) at di/dt = I/t_edge, added to the pulse's spectrum, and
    its snap-off an overshoot I_rr·√(L/C_oss) in the `hot_loop` issue.
- **Inputs** per power stage (Converters tab; hover a field for what it means): `rds_on`, `rds_on_low`, `dcr`,
  `diode_vf`, `q_oss` (and `c_oss`, which also sets the ringing), `dead_rise` / `dead_fall` (or `dead_time` for
  both), `v_sd`, `q_rr`. A missing value
  leaves that loss out, with a note (the dead time and V_SD take their defaults, also with a note). The `thermal:` settings section: `ambient_c` (25), `orientation`
  (horizontal / vertical), `convection` (natural / forced with `airspeed`, 1 m/s / fixed with `h_top` and
  `h_bottom`, 10 and 5 W/m²K), `radiation` (true), `quality` (draft / normal / fine; default normal), the limits
  `warn_c` (85) and `max_c` (105), `capacity_c` (see "Current capacity" below), `theta_jb` (part → K/W) for
  junction estimates, and the electro-thermal coupling below (`electrothermal`, `electrothermal_rounds`,
  `electrothermal_tolerance`).
- **Electro-thermal coupling** (`thermal.electrothermal: true`, or Preferences… → Electro-thermal; off by default):
  hot copper is more resistive (+0.393 %/K), so a hot via or track hands current to cooler ones beside it. Off,
  the DC paths keep their 20 °C current pattern and only the heat follows ρ(T). On, after the first thermal solve
  every power stage's and DC load's DC paths are solved again on the whole-board DC model with each copper cell and
  via barrel at its solved temperature, then Thermal again, until no path's loss and no via's share of its path's
  current moves by more than `electrothermal_tolerance` (1 %; also `"2%"`) in a round, for at most
  `electrothermal_rounds` rounds (3; 1-10, never more than 10). A round that changes more than the one before ends
  it with the round before kept; Cancel stops it. The notes say whether it settled and in how many rounds; the
  result (JSON `electrothermal`, the report's "Busiest vias at temperature", the plugin's tile) gives the busiest
  vias' currents at 20 °C and at temperature. Bridges' RMS currents are not re-solved, and Current capacity
  keeps the 20 °C current pattern.
- **Results:** a "Thermal: copper temperature" view in the Results tab (one map per copper layer, × the hot
  spot, tiles: hottest copper, total loss, hottest part), the hottest parts as bars against the limits, issues
  `thermal_hotspot` and `part_temperature` (parts without losses of their own are listed together, past
  `max_c`). **Draw contours on board** draws isotherms for this view.
- The board model is the one GroundTruth read (live from KiCad too), adapted to PCB Thermal's. Not in it:
  non-plated holes, copper graphics, solder-mask openings (all faces count as masked).
- Heat leaves only through the board's faces: part bodies, heatsinks, an enclosure and mounting are not modelled
  yet, so large parts come out on the hot (conservative) side.
- **Current capacity** (Results tab, **Current capacity**; `groundtruth simulate --capacity`): per power stage, the
  load current at which the hottest copper or part junction (where `theta_jb` is given) reaches
  `thermal.capacity_c` (default `max_c`), with the other stages at their operating points; with several stages
  also **all power stages** scaled together by one factor ("the board reaches 105 °C at 62 % of every load").
  Each result is a heatmap view at that current (tiles: the capacity, what limits it, the loss). The model is built
  twice, not per trial: a draft model finds where each capacity lies, then a model at `thermal.quality`, its mesh
  planned for the heat at those currents, settles it to 0.25 K. A stage whose neighbour alone already passes the
  limit shows 0 A: read the "all power stages" row then.
- The plugin solves in a process of its own and stops it (with a message) when it uses more than 60 % of the
  computer's memory, or when less than 5 % (0.5 GB) is left free: use `thermal.quality: draft` (or Thermal quality
  *draft* in **Preferences…**) then.
