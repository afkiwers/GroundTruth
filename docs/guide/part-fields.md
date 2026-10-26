# Part parameters for your parts database

Every part parameter GroundTruth reads from a symbol's fields, by the tab that uses it. Add them as parameters to the
parts in your parts database (an HTTP lib: KiCad's HTTP library interface hands a part's parameters to its symbol as
fields), and they arrive in the schematic with the parts; GroundTruth then reads them on every open and before every
run, and the plugin's tabs show them with a ◆ (a value typed into a tab instead is flagged where it differs). Back to
the [README](../../README.md); how the fields are read in detail: [the settings file](settings.md#part-parameters-from-the-schematic-http-lib-fields).

- **Names** are matched without regard to case; use one of them per parameter, exactly as below.
- **Values** may carry their unit as a parts database writes it (`3.8milliohm`, `616 picofarad`, `6.9 mΩ`) or as a
  datasheet does (`3.8m`, `616p`, `4m7`). A bare number is read in the unit of the column "a bare number is in".
- **Every parameter is optional.** A missing one falls back to the part's model, a value typed into a tab, or a
  default; the run says what it left out (e.g. "no Rds(on), so the switches' conduction loss is left out").
- A parameter used on more than one tab is listed under each, with the others in **Also used in**.

## Converters (DC-DC power stages)

| Field | Parts | A bare number is in | Example | What it does | Also used in |
|---|---|---|---|---|---|
| `Rds(on)` | FET, switch IC | mΩ | `3.8milliohm` | the switch's on resistance (high side, low side, an integrated switch): conduction loss | Bridges, Thermal |
| `Coss` | FET | pF | `616 picofarad` | the switch-node device's output capacitance: ringing frequency, spectrum, switching loss | Bridges |
| `Qoss` | FET | nC | `38 nanocoulomb` | output charge: switching loss Q_oss·V·f | Bridges |
| `Vsd` | FET | V | `2.1V` | reverse conduction of a synchronous rectifier in the dead time: dead-time loss | Bridges |
| `Qrr` | FET | nC | `62 nanocoulomb` | a silicon body diode's reverse-recovery charge (GaN has none): recovery loss and current spike | |
| `Vds` | FET | V | `100V` | the switch's voltage rating: the fix advisor keeps the switch-node peak below 80 % of it | Run checks |
| `Vf` | diode | V | `0.55V` | a buck's catch diode: its conduction loss | |
| `Cj` | diode | pF | `120 picofarad` | a diode buck's catch-diode capacitance at the switch node: ringing, node charge current | |
| `DCR` | inductor | mΩ | `6.9milliohm` | the winding resistance: winding loss, DC paths | DC loads, Thermal, Signal paths |
| `Inductance` | inductor | H | `5.6uH` | the inductor's L (else its Value field): ripple, input filter | Signal paths |
| `Capacitance` | capacitor | F | `10uF` | the capacitor's C (else its Value field) | Bridges, Signal paths, Power integrity |
| `ESR` | capacitor | mΩ | `5milliohm` | ripple sharing between the input capacitors, conducted emission, capacitor loss | Bridges, Signal paths, Power integrity, Thermal |
| `ESL` | capacitor | nH | `0.4nH` | the hot loop's inductance, the high-frequency sharing | Bridges, Signal paths, Power integrity |
| `Cbias` | capacitor | µF | `75nF@50V` | the capacitance at DC bias: one value, a point or a curve (1 to 12 points) | Bridges, Signal paths, Power integrity |
| `Cbias Voltage` | capacitor | V | `50V` | the voltage a single `Cbias` value holds at (else half the `Rated Voltage`) | Bridges, Signal paths, Power integrity |
| `Cbias Curve` | capacitor | µF (text) | `95nF@10V, 66nF@30V, 39nF@50V` | the DC-bias curve as text; wins over `Cbias` | Bridges, Signal paths, Power integrity |
| `Dielectric` | capacitor | text | `X7R` | `C0G` / `NP0`: no DC-bias curve applied | Bridges, Signal paths, Power integrity |
| `Rated Voltage` | capacitor | V | `50V` | the half-rating point a single `Cbias` holds at | Run checks |
| `Terminal Sheet Resistance` | inductor, capacitor, resistor, ferrite, FET | Ω per square | `0.77mΩ` | the part's terminal metal over its pads: how a pad's current is shared between the board copper and the terminal (without it, or `0`: a perfect conductor, the pad dark with a sharp rim) | DC loads, Thermal |
| `Terminal Metal` | inductor, capacitor, resistor, ferrite, FET | thickness and material | `0.15mm bronze` | the same terminal as thickness and material (copper, brass, bronze, becu, tellurium); `Terminal Sheet Resistance` wins when both are given | DC loads, Thermal |

### The control loop (a controller's fields)

On the switching controller's symbol; the Results tab's loop view of a buck or boost stage reads them.

| Field | Parts | A bare number is in | Example | What it does | Also used in |
|---|---|---|---|---|---|
| `Control Mode` | controller | text | `peak current` | `peak current`, `voltage`, `COT` or `internal`: which loop model applies | |
| `Vref` | controller | V | `0.8V` | the feedback reference: the output the divider regulates to | |
| `EA gm` | controller | mS | `1.8mmho` | a transconductance error amplifier's gm: the compensator gm × Z(COMP) | |
| `EA Ro` | controller | Ω | `2Mohm` | its output resistance (its DC gain gm × Ro; empty: ideal) | |
| `EA Gain` | controller | dB | `80dB` | a voltage error amplifier's open-loop gain (type II / III networks) | |
| `EA GBW` | controller | MHz | `10MHz` | a voltage error amplifier's gain-bandwidth | |
| `Vsense Max` | controller | mV | `75mV` | peak current mode: the maximum current-sense threshold | |
| `ITH Range` | controller | V | `1.2V` | peak current mode: the COMP / ITH swing to that threshold | |
| `Current Sense Gain` | controller | V/V | `0.0625` | peak current mode: dV(sense) / dV(COMP), instead of the two above | |
| `Slope Compensation` | controller | V/µs | `50mV/us` | peak current mode: the internal slope ramp (empty: half the inductor's down-slope) | |
| `Ramp` | controller | V | `1V` | voltage mode: the PWM ramp's amplitude (modulator gain 1 / Ramp) | |

## Bridges (motor bridges)

| Field | Parts | A bare number is in | Example | What it does | Also used in |
|---|---|---|---|---|---|
| `Rds(on)` | FET | mΩ | `3.8milliohm` | the switches' on resistance (high and low side): conduction loss, the phase resistance | Converters, Thermal |
| `Coss` | FET | pF | `616 picofarad` | output capacitance: the edges' charge current, switching loss | Converters |
| `Qoss` | FET | nC | `38 nanocoulomb` | output charge: switching loss | Converters |
| `Vsd` | FET | V | `2.1V` | reverse conduction in the dead time | Converters |
| `Capacitance` | capacitor | F | `100uF` | the bus capacitors' C | Converters, Signal paths, Power integrity |
| `ESR` | capacitor | mΩ | `5milliohm` | the bus capacitors' ripple sharing and loss, conducted emission | Converters, Signal paths, Power integrity, Thermal |
| `ESL` | capacitor | nH | `0.4nH` | the legs' hot loops | Converters, Signal paths, Power integrity |
| `Cbias` | capacitor | µF | `75nF@50V` | the bus ceramics at the bus voltage | Converters, Signal paths, Power integrity |
| `Cbias Voltage` | capacitor | V | `50V` | where a single `Cbias` holds | Converters, Signal paths, Power integrity |
| `Cbias Curve` | capacitor | µF (text) | `95nF@10V, 66nF@30V` | the DC-bias curve as text | Converters, Signal paths, Power integrity |
| `Dielectric` | capacitor | text | `X7R` | `C0G` / `NP0`: no DC-bias curve | Converters, Signal paths, Power integrity |

## Signal paths

### Op-amps and gain blocks

A part with a `Gain` field (or the `amplifier` model) is a gain stage in the path; an op-amp solved in its circuit uses
the open-loop figures instead.

| Field | Parts | A bare number is in | Example | What it does | Also used in |
|---|---|---|---|---|---|
| `Gain` | op-amp stage | V/V | `10`, `20dB` | the stage's gain (negative: inverting); makes the part a gain block | |
| `GBW` | op-amp | MHz | `10MHz` | the gain-bandwidth: bandwidth = GBW / gain, the dominant pole | |
| `Bandwidth` | op-amp | MHz | `1MHz` | a gain block's closed-loop −3 dB bandwidth (wins over GBW); on an op-amp in its circuit: the stage's bandwidth limit | |
| `Zout` | op-amp | Ω | `2.3kohm` | the open-loop output resistance R_O | |
| `Cin` | op-amp | pF | `3pF` | each input's capacitance to ground: the load on the input side | |
| `Rin` | op-amp | Ω | `1Gohm` | the input resistance | |
| `Slew Rate` | op-amp | V/µs | `20V/us` | caps each output harmonic at SR / 2πf | |
| `Aol` | op-amp | dB | `110dB` | the open-loop DC gain (the circuit model, with GBW) | |
| `Fp2` | op-amp | MHz | `20MHz` | a second open-loop pole: the loop's phase margin | |
| `PM` | op-amp | ° | `60` | the phase margin at unity gain, instead of Fp2 | |
| `CMRR` | op-amp | dB | `90dB` | shown only (the path model is single-ended) | |

### Passive parts in a path

Series resistors, filters, terminations and decoupling along a path, and the parts of an op-amp's circuit.

| Field | Parts | A bare number is in | Example | What it does | Also used in |
|---|---|---|---|---|---|
| `Resistance` | resistor | Ω | `4.7kohm` | the resistor's R (else its Value field) | DC loads |
| `Capacitance` | capacitor | F | `100nF` | the capacitor's C (else its Value field) | Converters, Bridges, Power integrity |
| `ESR` | capacitor | mΩ | `20milliohm` | the capacitor's series resistance | Converters, Bridges, Power integrity, Thermal |
| `ESL` | capacitor | nH | `0.5nH` | the capacitor's series inductance | Converters, Bridges, Power integrity |
| `Cbias` | capacitor | µF | `75nF@50V` | the capacitance at the DC voltage across it (an RC filter's corner moves with it) | Converters, Bridges, Power integrity |
| `Cbias Voltage` | capacitor | V | `50V` | where a single `Cbias` holds | Converters, Bridges, Power integrity |
| `Cbias Curve` | capacitor | µF (text) | `95nF@10V, 66nF@30V` | the DC-bias curve as text | Converters, Bridges, Power integrity |
| `Dielectric` | capacitor | text | `C0G` | `C0G` / `NP0`: no DC-bias curve | Converters, Bridges, Power integrity |
| `Inductance` | inductor, ferrite | H | `10uH` | the inductor's L (else its Value field) | Converters |
| `DCR` | inductor | mΩ | `50milliohm` | the inductor's series resistance | Converters, DC loads, Thermal |

## DC loads

| Field | Parts | A bare number is in | Example | What it does | Also used in |
|---|---|---|---|---|---|
| `Resistance` | resistor | Ω | `8milliohm` | a series part's R in the rail's DC path (a shunt, a 0R) | Signal paths |
| `DCR` | inductor | mΩ | `6.9milliohm` | an inductor's or ferrite's R in the DC path | Converters, Thermal, Signal paths |
| `Rated Power` | resistor | W | `1/16W` | the resistor's power rating: its loss in the DC paths against it | Run checks |

## Power integrity

The decoupling capacitors on each rail; the regulator's output capacitors too.

| Field | Parts | A bare number is in | Example | What it does | Also used in |
|---|---|---|---|---|---|
| `Capacitance` | capacitor | F | `100nF` | the decap's C (else its Value field) | Converters, Bridges, Signal paths |
| `ESR` | capacitor | mΩ | `20milliohm` | the decap's series resistance: the damping of the anti-resonances | Converters, Bridges, Signal paths, Thermal |
| `ESL` | capacitor | nH | `0.4nH` | the decap's series inductance: where it stops helping | Converters, Bridges, Signal paths |
| `Cbias` | capacitor | µF | `75nF@50V` | the decap at the rail's voltage | Converters, Bridges, Signal paths |
| `Cbias Voltage` | capacitor | V | `50V` | where a single `Cbias` holds | Converters, Bridges, Signal paths |
| `Cbias Curve` | capacitor | µF (text) | `95nF@10V, 66nF@30V` | the DC-bias curve as text | Converters, Bridges, Signal paths |
| `Dielectric` | capacitor | text | `X5R` | `C0G` / `NP0`: no DC-bias curve | Converters, Bridges, Signal paths |

## Thermal

The temperatures come from the losses of the power stages, bridges and DC loads, so these fields set the heat.

| Field | Parts | A bare number is in | Example | What it does | Also used in |
|---|---|---|---|---|---|
| `Rds(on)` | FET, switch IC | mΩ | `3.8milliohm` | the switches' conduction loss | Converters, Bridges |
| `DCR` | inductor | mΩ | `6.9milliohm` | the winding loss | Converters, DC loads, Signal paths |
| `ESR` | capacitor | mΩ | `5milliohm` | the capacitors' ripple loss | Converters, Bridges, Signal paths, Power integrity |
| `Terminal Sheet Resistance` | inductor, capacitor, resistor, ferrite, FET | Ω per square | `0.77mΩ` | where a part's DC current heats the copper under its pads | Converters, DC loads |

## Run checks (voltage-aware safety checks)

| Field | Parts | A bare number is in | Example | What it does | Also used in |
|---|---|---|---|---|---|
| `Rated Voltage` | capacitor | V | `50V` | `cap_voltage`: the voltage across it against its rating, derated (also read as `Voltage`, or a voltage in the value, `100n 50V`) | Converters |
| `Rated Power` | resistor | W | `1/16W` | `resistor_power` (after Simulate): its loss against its rating, derated (also read as `Power`; else by package) | DC loads |
| `Vds` | FET | V | `100V` | `vds_rating` (after Simulate): the switch-node peak against the switches' rating | Converters |

## Not from the parts database

- **Value** (KiCad's own field): a part's value when it has no `Capacitance`, `Inductance` or `Resistance`; a
  capacitor's voltage rating may be in it (`100n 50V`). **Description**: a capacitor's dielectric when it has no
  `Dielectric` field (`X7R` in its description).
- **`GT`**: detection labels (`hb_hs`, `conv1_in`, `sig_rx:9` …) and a through-hole pin's values (`contact:0.5m,
  fill:75`). They describe the part's role on one board, so they belong on the schematic symbol, not in the parts
  database: see [Detection labels](settings.md#detection-labels-gt-field).
