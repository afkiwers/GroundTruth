# The settings file: `groundtruth/<board>.groundtruth.yaml`

What GroundTruth keeps for a board, the part models, the checks, finding the nets to analyse and the GT labels. Back to the [README](../../README.md).

Everything GroundTruth keeps for a board goes into one folder next to it, `groundtruth/`, so the KiCad project folder stays tidy: the settings file `<board>.groundtruth.yaml`, the simulation history (`history/`) and, offered first by **Export report…**, the exported reports. Boards in the same folder share it; each file carries its board's name. A settings file directly next to the board (the layout before 2026-10-10) is not read. The settings file should be committed with the board. The plugin writes it, and you can also edit it by hand. When the plugin opens a board whose settings list no nets, power stages or motor bridges yet, it detects them in the background (as `groundtruth detect --write` does); they show in orange until you review them. `groundtruth init` creates it, or updates it after board changes: new parts are added, removed parts are flagged, and your edits are kept.

```yaml
reference_nets: [GND]
analysis: {grid_mm: auto, margin_mm: 10, solve_points: 4, limit_class: B, distance_m: auto, ground_reflection: true,
           planes: auto,                         # auto = all plane layers in the region, nearest = one plane only
           max_nodes: 100000,                    # grid budget of each route model
           plane_capacitance: true,              # board-wide model with plane-pair capacitance from the stackup
           cavity_max_nodes: 40000,              # its grid budget (raise for big boards; cost grows ~linearly)
           max_solves: 10,                       # harmonics solved per route (initial + refinement)
           threads: auto, gpu: auto,             # parallel solves (auto: physical cores, at most 4); GPU: auto | on | off
           driver_ohms: 25, load_pf: 5}          # default driver resistance and receiver capacitance
rules: {stitch_distance_mm: 2.0, decap_distance_mm: 5.0, edge_factor: 3.0, min_gap_mm: 0.1,
        detour_warning: 1.25, detour_error: 2.0, emi_margin_db: 6.0, resonance_db: 10.0,
        severity: {reference_net: off}}          # per-check override: error | warning | info | off
nets:                                            # nets to analyse: typed in, or suggested by `groundtruth detect`
  - {name: /CPU/SPI_SCK, driver: U1102.12, receivers: [U1103.6], freq: 20MHz, rise_time: 2ns, amplitude_v: 3.3,
     driver_ohms: 33}                            # optional per net: driver_ohms, load_pf, current_a ...
  - {name: /USB_D_A, driver: U1.5, receivers: [U9.2], freq: 50MHz, rise_time: 1ns, fall_time: 1.5ns,
     driver_nh: 1.5, driver_pf: 3, load_ohms: 50, through: [C12]}   # a path through an AC-coupling C12
  - {name: /CPU/SPI_MISO, driver: U1103.2, receivers: [U1102.13], freq: 10MHz, rise_time: 2ns, amplitude_v: 3.3,
     source: auto, reviewed: false, class: SPI data}   # suggestion, not yet confirmed
  - {name: /CPU/SW_CLK, driver: J1101.4, receivers: [U1102.28], freq: 10MHz, rise_time: 3ns,
     enabled: false}                             # switched off: kept, but not analysed (the tick box in the plugin)
components:                                      # one line per part, pre-filled with a suggested model
  R12: {value: 0R, footprint: R_0402_1005Metric, model: short}
  R7:  {value: 33R, footprint: R_0402_1005Metric, model: resistor, R: 33.0}
  C4:  {value: 100n, footprint: C_0402_1005Metric, model: cap, C: 1e-07, ESR: 0.02, ESL: 4e-10}
  FB1: {value: 600R@100MHz, footprint: L_0603_1608Metric, model: ferrite, R: 0.1, L: 9.5493e-07}
  U1:  {value: STM32, footprint: LQFP-48, model: open}
waivers:
  - {fingerprint: 7bacf605e3401d93, reason: "slow signal, accepted"}
modifications: []                                # what-if edits, see "Fix suggestions" below
report: {title: "EMI and power analysis", company: ACME, author: J. Smith, document: R-1234}   # optional: the report's cover
```

The plugin's **Preferences…** are not in this file: they belong to the computer, not the board (see "Preferences" below). Simulate's resolution (`analysis.resolution`, the plugin's Resolution or the CLI's `--resolution`) is never written to it.

**Configurations.** The file is the working settings: every edit in the plugin is saved into it, and the plugin opens with it as it was left. A set-up can also be kept as a **configuration** (the plugin's **Configuration ▾ → Save configuration as…**): `groundtruth/configs/<name>.gtconfig.yaml`, holding what is simulated and how: `nets`, `converters`, `bridges`, `dc_loads`, `pdn`, `si` (each row with its `enabled` tick), `modifications` (what-if edits), `analysis`, `thermal` and `victims`, plus a short hash of every part model at save time (`parts`). Everything else (part models, `tht`, `reference_nets`, `stack`, `safety`, `rules`, `waivers`, `drawing`, `report`) is shared by all configurations of the board. **Load configuration…** puts a configuration's sections back in place of the working settings' (one it lacks is emptied) and names the parts changed since it was saved; **Load from a past run** does the same with a kept Simulate's settings; **Back to last settings** returns to the settings before the last load (`configs/.before-load.gtconfig.yaml`); **Undo** / **Redo** (Ctrl+Z / Ctrl+Y) step through every change of the settings in the window. On the command line, `--config FILE` runs a configuration for that run only (`config/scenario.py`).

Available part models are `short`, `open`, `resistor (R)`, `cap (C, ESR, ESL, optional C_bias: its DC-bias curve, `75nF@50V` or `95nF@10V, 66nF@30V, 39nF@50V`, applied per capacitor at the DC voltage across it; see [Capacitance at DC bias](analyses.md#capacitance-at-dc-bias))`, `inductor (L, R)`, `ferrite (R, L)` and `amplifier` (an op-amp in a signal path: `gbw`, optional `aol` (open-loop gain, dB, 100), `fp2` (a second open-loop pole) or `pm` (its phase margin at unity gain, degrees), `z_out` (the open-loop output resistance R_O, 100 Ω), `c_in` (each input to ground, 2 pF), `r_in`, `slew`, `cmrr`; `pins: [in, out]` for a dual or quad op-amp; with these the circuit around it decides its gain (see [Op-amp circuits](analyses.md#op-amp-circuits-and-the-transfer-function)); `gain` in V/V, negative = inverting, makes it a fixed gain block instead). `C_bias` is a ceramic capacitor's capacitance at its DC voltage, from the maker's DC-bias curve, and replaces C in every solve: a class II ceramic (X7R, X5R) keeps only 30–60 % of its nominal C at half its voltage rating, which raises a converter's ripple and conducted emission. A power stage's results name its ceramic capacitors of 1 µF and up that are still at their nominal C on a rail of 10 V or more. They are used in two places:
- **Signal route:** a route runs through a part whose model passes a signal, such as a 0R, a termination resistor (≤ 1 kΩ) or a ferrite.
- **Plane connections:** caps, 0R and ferrites that bridge plane islands, for example decaps between GND and +3V3, become lumped branches in the solver.

## Part parameters from the schematic (HTTP lib fields)

A part's parameters can come from its symbol's fields instead of being typed in: give the parts these fields in the schematic, or name the HTTP lib's parameters so (KiCad's HTTP library hands them to the symbols as fields). One field name per parameter, matched without regard to case:

| Field | Parts | Fills | A bare number is in | Example |
|---|---|---|---|---|
| `Rds(on)` | FET, switch IC | `rds_on` (high side or integrated switch), `rds_on_low` (low side); per FET | mΩ | `3.8milliohm` |
| `Coss` | FET | `c_oss` of the switch-node device (a side's parallel FETs together) | pF | `616 picofarad` |
| `Qoss` | FET | `q_oss` (as Coss) | nC | `38 nanocoulomb` |
| `Vsd` | FET | a bridge's `v_sd` | V | `2.1V` |
| `Vf` | diode | a catch diode's `diode_vf` | V | `0.55V` |
| `Cj` | diode | a diode buck's `c_oss` | pF | `120 picofarad` |
| `DCR` | inductor | `dcr` and the inductor model's R | mΩ | `6.9milliohm` |
| `ESR`, `ESL`, `Cbias` | capacitor | the model's ESR, ESL, C_bias (one value, a point or a curve of 1-12) | mΩ, nH, µF | `5milliohm`, `0.4nH`, `75nF@50V` |
| `Cbias Voltage`, `Cbias Curve`, `Dielectric` | capacitor | the voltage a single Cbias holds at; the curve as text (an HTTP lib text parameter, wins); C0G / NP0: no curve | V, text, text | `50V`, `95nF@10V, 39nF@50V`, `X7R` |
| `Capacitance`, `Inductance`, `Resistance` | C, L, R | the model's C, L, R (else the Value field) | F, H, Ω | `10uF` |
| `Gain` | op-amp stage | the amplifier's gain (a Gain field makes the part an amplifier); `20dB` = 10 | V/V | `10`, `-2`, `20dB` |
| `GBW`, `Bandwidth` | op-amp | GBW: the gain-bandwidth (the circuit model's dominant pole at GBW/Aol). Bandwidth: a gain block's (a part with `Gain`) one pole, else at GBW / \|gain\|; on an op-amp solved in its circuit, **its stage's bandwidth limit**: a stage wider than it is a `bandwidth_limit` issue | MHz | `10MHz` |
| `Zout`, `Cin`, `Rin` | op-amp | the open-loop output impedance (the datasheet's R_O / Z_O; with GBW or Bandwidth the stage's output is Zout·jf/(jf + f_c), divided by the loop below its bandwidth f_c; without, Zout as given; none: 1 Ω), input C and R (load of the input side) | Ω, pF, Ω | `2.3kohm`, `3pF` |
| `Slew Rate` | op-amp | caps each output harmonic at SR / 2πf | V/µs | `20V/us` |
| `Aol`, `Fp2`, `PM` | op-amp | open-loop gain, a second open-loop pole or the phase margin at unity gain (the circuit model) | dB, Hz, ° | `110dB`, `20MHz`, `60` |
| `CMRR` | op-amp | shown only: the path model is single-ended | dB | `90dB` |

- **Values:** read with units as an HTTP lib sends them (`3.8milliohm`, `616 picofarad`, `6.9 mΩ`) or in datasheet notation (`3.8m`, `616p`, `4m7`); a bare number is in the unit above. A value of the wrong kind (`100V` for Rds(on)) is not used, and the stage's or part's note says why; a value outside the usual range is used as written, with a note. Parallel parts whose fields disagree give no value.
- **In sync:** a stage's or part's value from the schematic is listed in its `from_schematic` (in the settings file) and is read again on every open (`groundtruth init`, the plugin) and before every run, so an HTTP lib change arrives by itself once KiCad's symbols and PCB are updated. Detection fills a new stage from its parts' fields.
- **Set here:** a value typed in GroundTruth (the forms, the Parts table, the file) is the user's: kept, and where the schematic says something else, marked `!` in the forms and the Parts table's Schematic column, and reported as the info issue `schematic_differs`. **Use schematic values** (Converters, Bridges) and **Reset filtered to suggested** (Parts) take the schematic's values back; an emptied form field takes it too.
- The plugin's Help (**Part parameters from the schematic**) lists the same table, generated from `components/fields.PARAMS` with the usual ranges.

**Settings reference.** Every key the parser reads, with its default. The power-stage, bridge, victim, stack,
thermal and what-if keys are listed in their own sections below.

| Key | Default | Meaning |
|---|---|---|
| `version` | 1 | File format, written by `init` (not read). |
| `reference_nets` | `[GND]` | Ground nets the return may use and return pins are taken from. `init` guesses them from the net names (GND, AGND, PGND, VSS, 0V, …). |
| `analysis.grid_mm` | `auto` | Grid pitch of the route and power-stage models in mm. `auto`: the dielectric height (0.05 to 0.25 mm), coarsened until `max_nodes` holds. |
| `analysis.margin_mm` | 10 | Copper around a route that its model includes. |
| `analysis.solve_points` | 4 | Harmonics solved first per route: the fundamental plus log-spaced harmonics across the limit band. |
| `analysis.max_solves` | 10 | Harmonics solved per route in all (the first ones plus refinement near the limit). |
| `analysis.max_nodes` | 100000 | Grid budget of each route or power-stage model. |
| `analysis.plane_capacitance` | true | The board-wide model with plane-pair capacitance (displacement current, plane resonances). |
| `analysis.cavity_max_nodes` | 40000 | Grid budget of that board-wide model (raise it for big boards; the cost grows about linearly). |
| `analysis.planes` | `auto` | `auto`: every plane layer in the region; `nearest`: one plane only. |
| `analysis.limit_class` | `B` | CISPR 32 class `A` or `B` for the radiated estimate. |
| `analysis.radiated_limit` | none | Another radiated limit instead of CISPR 32. MIL-STD-461G RE102 by name (1 m, Table II bandwidths, Figures RE102-1 to -4): `re102_ground_army` (Navy mobile & Army), `re102_ground_navy_fixed` (Navy fixed & Air Force), `re102_aircraft_external` (fixed wing external, from 2 MHz), `re102_helicopter` (helicopters, from 10 kHz; `re102_space` the same, 461H's space systems), `re102_aircraft_internal` (fixed wing internal, < 25 m nose to tail), `re102_aircraft_internal_25m`, `re102_ship_above_deck`, `re102_ship_below_deck`, `re102_submarine_external`, `re102_submarine_internal`. Or a curve of your own: `{name: "customer limit", distance_m: 1, bands: mil461, points: [[2MHz, 24], [100MHz, 24], [18GHz, 69]]}`, straight on a log-frequency axis between the points, no limit outside them; `bands`: `mil461` or `cispr`. The radiated band follows the limit (from 2 MHz for ground and fixed wing, 10 kHz for helicopters). The curves are 461G's; 461H (April 2026) keeps them, its Figure RE102-3 naming the 10 kHz curve "Space and Helicopters". |
| `analysis.distance_m` | `auto` | Measurement distance; `auto`: CISPR 32's (10 m below 1 GHz, 3 m above). |
| `analysis.ground_reflection` | true | Doubles the field (+6 dB) for the ground reflection of an open-area or semi-anechoic test site. |
| `analysis.driver_ohms`, `analysis.load_pf` | 25, 5 | A net's driver output resistance and receiver input capacitance, unless the net sets its own. |
| `analysis.conducted`, `analysis.conducted_class` | `cispr32`, `B` | Conducted limit of power stages and bridges: `cispr32` (`A`/`B`), `cispr25` (`1`–`5`), `mil461` (`basic` = `28V`, `115V`, `220V`, `270V`, `440V`), `none`. |
| `analysis.mil461` | `G` | The MIL-STD-461 revision: `G` (11 December 2015) or `H` (17 April 2026, which supersedes G). Only CE101 differs between them here: under `H` the ships' limit runs to 20 kHz (Figure CE101-1 continued to 66 dBµA) and its relaxation is capped at 35 dB; CE102, RE102, the LISN and the bandwidths are the same in both. |
| `analysis.ce101` | `none` | MIL-STD-461 CE101, the power leads' current from 30 Hz to 10 kHz, for ships under 461H to 20 kHz (dBµA, current probe, 10 Hz / 100 Hz / 1 kHz bandwidths): `aircraft` (Figure CE101-4: Army aircraft including flight line, Navy ASW aircraft; curve #1 above 28 V, #2 at 28 V or below) or `ships` (Figure CE101-1: surface ships and submarines, DC, relaxed by 20·log(I/3) above 3 A, at most 35 dB). Needs a conducted model at the input (`conducted`, e.g. `mil461`). A motor bridge's commutation (lines every electrical frequency, strongest at 6·f_e) and a converter switching within the range are read (between 10 and 20 kHz from the CE102 band's solves, the LISN as specified at the input); below 10 kHz the leads (2 × (50 µH + 50 mΩ) to a stiff source) can resonate with the bus capacitors in this band. `ce101_source_v` and `ce101_load_a` override each stage's own input voltage (curve) and current (relaxation). |
| `analysis.via_plating_um` | 25 | Via barrel plating, for the via current density and IPC-2221 capacity. |
| `analysis.threads` | `auto` | Parallel solves: `auto` (physical cores, at most 4) or a number. |
| `analysis.gpu` | `auto` | `auto`, `on` or `off` (see "GPU acceleration"). |
| `rules.stitch_distance_mm` | 2.0 | A layer change between planes of one net needs a via of that net this close (`layer_change_stitch`). Also the distance for power-stage via fixes. |
| `rules.decap_distance_mm` | 5.0 | A layer change between planes of different nets needs a capacitor between them this close (`layer_change_decap`). |
| `rules.edge_factor` | 3.0 | `edge_proximity` when a trace runs closer than this × its dielectric height to its plane's edge. |
| `rules.min_gap_mm` | 0.1 | Uncovered stretches of trace shorter than this are ignored (`gap_crossing`, `off_plane`). |
| `rules.detour_warning`, `rules.detour_error` | 1.25, 2.0 | `return_detour` at this loop inductance / ideal loop inductance. |
| `rules.emi_margin_db` | 6.0 | `emi_margin` and `conducted_emission` when the margin to the limit is below this (an error below 0 dB). |
| `rules.resonance_db` | 10.0 | `plane_resonance` when a plane resonance raises the loop moment this much over its value at the fundamental. |
| `rules.overshoot_warning`, `rules.overshoot_error` | 0.25, 0.5 | `hot_loop`: switch-node overshoot as a fraction of the rail voltage. |
| `rules.current_density_warning`, `rules.current_density_error` | 50, 100 | `current_density` in A/mm² (copper averaged over 1 mm, via plating). |
| `rules.via_load_warning`, `rules.via_load_error` | 100, 150 | `via_overload` in % of a via's IPC-2221 capacity. |
| `rules.severity` | `{}` | Per-check severity override (see "Checks and severities"). |
| `drawing.issues_layer`, `drawing.suggest_layer`, `drawing.current_layer` | `User.9`, `User.8`, `User.7` | Layers the plugin draws issue markers, suggested fixes and contours/arrows on. |
| `report.title`, `company`, `author`, `document` | empty | The HTML report's cover (title default "EMI and power analysis"). |
| `waivers[].fingerprint`, `reason` | | An accepted issue: it is still listed, marked "waived" with the reason, but does not fail the run and is left out of the counts and the Code Quality report. |

A `nets:` entry:

| Key | Default | Meaning |
|---|---|---|
| `name` | | The net name as on the board. |
| `driver`, `receivers` | | Pins as `REF.PAD`; `receiver:` (one pin) works too. |
| `freq`, `rise_time` | | Fundamental and edge time (`20MHz`, `2ns`, or plain Hz and s). |
| `amplitude_v`, `duty` | 3.3, 0.5 | Swing and duty cycle of the trapezoid spectrum. |
| `current_a` | | Overrides the line-current estimate: peak A at the fundamental. |
| `driver_ohms`, `load_pf` | `analysis` | Driver output resistance, input capacitance per receiver. |
| `driver_nh`, `driver_pf`, `fall_time` | none, none, `rise_time` | The source: its series inductance (nH), its output capacitance (pF), a falling edge of its own. |
| `load_ohms` | none | A resistance at the receivers (a parallel termination). |
| `max_bandwidth` | none | The path's bandwidth limit (`1kHz`: an ADC input's anti-alias filter): its −3 dB frequency at the receiver (3 dB below the passband peak) above it is a `bandwidth_limit` issue; also when the response does not fall 3 dB within the sweep while the sweep reaches above the limit. |
| `sweep_from`, `sweep_to`, `sweep_points` | f/100, the band's top, 300 | The path's Bode sweep (`1Hz`, `10MHz`; from 10 mHz; 20 to 5000 points): Simulate and `groundtruth bode` / the Signal paths tab's **Bode** sweep the path's circuit over it. A sensor or ADC input wants `sweep_from: 1Hz`: the gain at the sweep's start is shown as the DC gain. |
| `through` | | Parts the signal path may run through although they don't pass a signal by default (an AC-coupling capacitor, a resistor above 1 kΩ). The path from `driver` to the receivers runs through series parts into other nets, each part with its impedance. |
| `driver_return`, `receiver_return` | | The return pin (`REF.PAD`). Default: the nearest reference-net pad of the same part, else of any part within 10 mm; without one the route gets `return_path_broken`. |
| `source`, `reviewed`, `class` | `manual`, true | Set by detection: `auto`, unreviewed, and the signal class (information only). |

A `components:` entry: `value` and `footprint` (from the board; `init` compares them), `model` with its parameters
(above), `pins` (the two pins the model joins, by pad number or pin name, e.g. `[IP+, IP-]`; default all pads),
`note` (`init` writes "value changed …" there), `removed: true`, which `init` sets when the part has left the
board (its model is then ignored), and `tht` (its through-hole pin values, below).

**Through-hole pins** (`tht`): built in, a soldered pin is copper filling its whole hole, joined to every layer with a
perfect joint. A part's own values (`components: J1: {..., tht: {contact: 0.5m, fill: 75}}`, the Parts tab's Pins
column, or its schematic GT field: `GT = conv_in, contact:0.5m`) and the board's defaults (top-level `tht: {pin:
brass}`, the Parts tab's Pin defaults…) change that, each value from the first place that has it (GT field, the
part's entry, the board's defaults, built in): `contact` (the mating contact, Ω per pin), `joint` (the solder joint,
Ω per pin), `fill` (% of the hole soldered from the side opposite the part; above it only the plating conducts and
the current enters the board where the solder ends), `pin` (`hole`, `copper`, `brass`, `bronze`, `becu`,
`tellurium`), `pin_d` (mm, default the hole less 0.25 mm), `pin_r` (Ω through the board). Contact and joint add I·R
to a DC load's drop and I²·R of heat in Thermal at the fed pins (DC loads, converters' connectors); a connector pin
carrying 3 A or more without a contact value gets a note.

## Checks and severities

Every issue has one of these checks. The default severity is the one listed; where two are given, the threshold
rules above decide.

| Check | Default | Meaning |
|---|---|---|
| `route_missing` | error | No copper path between driver and receiver. |
| `no_reference` | error | The signal layer has no adjacent copper layer to act as reference plane. |
| `gap_crossing` | error | The trace crosses a gap or split in its reference plane. |
| `off_plane` | warning | The trace runs without a reference plane (says when the return falls back to a farther layer). |
| `layer_change_stitch` | error | A signal via moves the return between planes of one net with no via of that net within `stitch_distance_mm`. |
| `layer_change_decap` | warning | A signal via moves the return between planes of different nets with no capacitor between them within `decap_distance_mm`. |
| `edge_proximity` | warning | Trace closer than `edge_factor` × h to its plane's edge. |
| `reference_net` | info | Trace referenced to a non-ground plane. |
| `schematic_differs` | info | A part parameter set in GroundTruth differs from the part's schematic field (see "Part parameters from the schematic"). |
| `unusual_value` | info | A power stage's or bridge's value is outside the usual range for its kind (often a missing unit prefix). It is used as written. |
| `return_path_broken` | error | The solver finds no return: no reference-net pin near a driver or receiver, no plane copper near the route or a return pin, or no copper connection between the return pins (a split plane). |
| `return_detour` | warning / error | Loop inductance over `detour_warning` / `detour_error` × the ideal. |
| `plane_resonance` | warning | A plane-pair resonance raises a route's or hot loop's moment by `resonance_db` or more. |
| `bandwidth_limit` | warning | A signal path's bandwidth at its receiver above its `max_bandwidth`, or an op-amp stage's (from its input to its output, solved in its circuit) above its `Bandwidth` field (1 % tolerance). |
| `opamp_stability` | warning / error | An op-amp's loop has less than 45° of phase margin (it rings); below 20° an error (close to oscillating). Give the op-amp `PM` or `Fp2` for its second pole: with one pole the margin is always near 90°. |
| `emi_margin` | warning / error | Radiated estimate within `emi_margin_db` of the limit / over it (routes and hot loops). |
| `band_unsolved` | warning | A solve did not converge and the band above the last solved frequency is held at its value there, not computed: a plane resonance in it would be missed. Simulate again at another resolution. |
| `hot_loop` | warning / error | Switch-node overshoot over `overshoot_warning` / `overshoot_error`. |
| `conducted_emission` | warning / error | Conducted estimate at the input (differential or common mode) within `emi_margin_db` of the limit / over it. |
| `power_loop_broken` | error | A converter's hot loop cannot be closed in the copper. |
| `current_density` | warning / error | DC (or a bridge's RMS) current density over the `current_density_*` limits. |
| `via_overload` | warning / error | A via over `via_load_warning` / `via_load_error` % of its IPC-2221 capacity. |
| `bridge_operating_point` | warning | A motor bridge cannot reach its phase current at this speed and bus voltage. |
| `victim_coupling` | warning | A bridge's or power stage's switching puts noise or injected current on a victim net, or moves a victim part's ground. |
| `thermal_hotspot` | warning / error | Copper over `thermal.warn_c` / `thermal.max_c`. |
| `part_temperature` | warning / error | A part with losses over `warn_c` / `max_c`; parts heated past `max_c` by the copper alone, as one error. |

`rules.severity` maps a check to `error`, `warning`, `info` or `off` (`severity: {reference_net: off, edge_proximity: info}`).
The override replaces the severity whatever the threshold says, so `{emi_margin: warning}` makes even a failed limit
a warning; `off` drops the check's issues entirely. `--fail-on` then decides which severities fail a CLI run.

## Finding the nets to analyse

`groundtruth detect board.kicad_pcb` (or **Detect…** on the plugin's Signal paths tab) suggests nets with their driver, receivers and default timing:
- **Signal class** comes from the net name and the schematic pin names, for example SPI clock (20 MHz / 2 ns), RMII (50 MHz / 1 ns), USB, SDIO/QSPI, debug clocks, crystals and switching nodes. A net with no class gets 10 MHz / 2 ns, unless an op-amp with a `Slew Rate` or `GBW` / `Bandwidth` field drives it (its output on the driver's net): then the fastest full 3.3 V swing that op-amp can make, edge max(0.8·3.3 V / SR, 0.35 / BW) and frequency min(10 MHz, BW, SR / (π·3.3 V)). refboard's U301 (TLV6001, 0.5 V/µs) gives Net-(U301--) 5.28 µs at 48.2 kHz instead of GHz content on a current-sense signal.
- **Driver** comes from the schematic pin types: an output beats a bidirectional pin, which beats a connector/passive pin, which beats an input.
- **Series parts:** suggestions follow 0R/termination resistors and ferrites, so a series-terminated line is one net.
- **Excluded:** power nets.

Each suggestion shows its confidence and the reasons it was made. `--write` adds them to the settings file as `source: auto, reviewed: false`. Reports keep reminding you about unreviewed nets until you mark them reviewed: edit them, press **Mark reviewed**, or set `reviewed: true` in the file. `--min-score` widens the search: 1 adds UART/CAN/PWM, 0 adds I2C.

**Adding a net from KiCad.** Select a track, via or pad in the PCB editor (click it, or right-click it: that selects it too), then press the toolbar button **Add selected net to GroundTruth** (green plus). Its net appears in the Signal paths table of the open GroundTruth window, which comes to the front; if no window is open for the board, one opens. In the window, **Add KiCad selection** on the Signal paths tab does the same. Driver and receivers are suggested from the pin types as above, also for nets whose names match no signal class (then set the frequency and rise time yourself); the new row is unreviewed (orange). Reference nets (GND), supply nets and every net of a power stage's power flow are refused with the reason. That includes the switch node, the inductor's output side and the output path through current-sense shunts, even when a sense amplifier's input pin sits on one. Detection (`groundtruth detect`, **Detect…**) skips them too. KiCad 10 does not let plugins add entries to the right-click menu, hence the button.

**Adding a whole path from KiCad.** Select one bit of copper on each net of a signal path (Ctrl+click: the micro's ADC net, the op-amp's output net, its input net from the current-sense amplifier), then press the same button. GroundTruth puts the nets in order through the parts joining them, takes the direction from the op-amp's input and output pins (or the pin types at the ends; a selected pad fixes an end), and adds one signal path with its Pass-through resistors and the op-amp modelled as an amplifier with its pins. It also works out the GT labels for the schematic symbols; KiCad 10 has no schematic API for plugins, so they are shown to add by hand (Symbol Fields Table), while the path itself works in Simulate at once.

## Detection labels (`GT` field)

Detection guesses converters and motor bridges from reference designators, pin names and connections. It gets
most boards right; where it misses a stage, takes the wrong parts, or picks the wrong connector, you can say in
the schematic what a part is. Add a field named **`GT`** to the symbol (Symbol Properties → add field, or many
symbols at once in **Tools → Edit Symbol Fields**), hide it if you like, and update the PCB from the schematic: the
field travels to the board like an MPN does, and both the board file and the live KiCad API carry it. Labels are
optional, change no net name, and boards without them are detected as before.

| Label | On which part |
|---|---|
| `conv_hs` / `conv_ls` | a converter's high-side / low-side switch (each of parallel FETs); a catch diode is `conv_ls` |
| `conv_sw` | a converter IC with its switches inside |
| `conv_L` | the converter's power inductor (found on the switches' net when not labelled) |
| `conv_in` / `conv_out` | the converter's input / output connector |
| `hb_hs` / `hb_ls` | a motor-bridge leg's high-side / low-side switch |
| `hb_out` | the motor connector (or each phase's wire tab) |
| `hb_in` | the bus input connector (both battery tabs when + and - are separate) |
| `ignore` | any part detection should never take as a power-stage part |
| `sig` | the Signal paths tab: every net of this 2-pin part is a signal |
| `sig:9`, `sig:PA3` | the net on that pin (pad number or pin name) is a signal, even with a supply-like name |
| `sig_drv:5` / `sig_rx:9` | ... and this pin drives / receives it (Detect's driver and receivers are replaced) |
| `amp_in:3, amp_out:1` | an op-amp stage's input and output pin: the part becomes an `amplifier` the signal path runs through |
| `amp_inv:2` | its inverting input, where the schematic pin names don't say (+IN / −IN / OUT, +INA … are read by themselves) |

- **Which stage**: an id between the kind and the role ties parts together: `conv1_hs`, `conv1_ls`, `conv1_L`,
  `conv2_hs`, … (letters or digits: `conv5V_hs`); `conv_out_1` (id after the role) works too. A bridge leg's id is
  its phase: `hbA_hs`, `hbB_ls` (A-C, U-W or 1-3).
- **Reused sheets**: a sheet used several times (one per phase, one per rail) has the same field values in every
  copy, since KiCad keeps only the reference per copy. GroundTruth reads each part's sheet copy from the board, so
  `hb_hs` / `hb_ls` in `phase.kicad_sch` used three times make three legs, named and phased after their sheets
  ("Phase A", "Phase B", "Phase C").
- **Shared parts**: one connector feeding several converters: `conv_in` (every converter it reaches) or
  `conv1_in, conv2_in`. Several labels on one part are separated by commas. Bus capacitors need no label: every
  capacitor on the rail is part of the solve.
- **Feedback**: `groundtruth detect` prints "GT labels on this board: …" with every problem (an unknown label, a
  converter with a high side but no low side labelled, labelled switches that share no net). In the plugin, the
  Converters and Bridges tabs show the same line at the bottom; **Show** opens the tab's explanation. A labelled
  stage's first reason reads "labelled (GT): Q1 hs, Q2 ls, L1 L".
- To try labels on a board without touching it: `python tools/dev/add_labels.py BOARD OUT.kicad_pcb Q1=hb_hs
  Q2=hb_ls J1=hb_out` writes a copy with the fields set.

Why a symbol field and not net labels: a label such as `hb_A` would rename the net (and a stage's roles belong to
parts, not nets: which FET is the high side). KiCad's component classes and net classes are meant for design rules.
A field is the one place that reaches the board unchanged, with one value per part.
