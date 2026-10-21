"""Part parameters from the schematic: a symbol's fields (InvenTree parameters through KiCad's HTTP libraries, or
typed in) give R_DS(on), C_oss, an inductor's DCR, a capacitor's ESR ... One concrete field name per parameter
(PARAMS), matched without regard to case; values with units as InvenTree sends them ("3.8milliohm"), or datasheet
notation ("3.8m"); a bare number is in the unit the table assumes.

A stage's or part's value comes from the schematic (its key is listed in `from_schematic`, refreshed on every open
and run) or is set in GroundTruth (an override, flagged where it differs from the schematic)."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from .values import format_si, parse_quantity

REL_TOL = 1e-4          # an override this close to the schematic's value is the same value (format_si rounds)
CONTROL_MODE = "Control Mode"   # a controller's text field: peak current | voltage | COT | internal (analysis/loop.py)


@dataclass(frozen=True)
class Param:
    """One part parameter a schematic field can give: the field's name, which parts carry it, what it fills in
    GroundTruth, how its value is read, its usual range, and what the analysis uses it for (the help table)."""
    field: str          # the symbol field's name
    parts: str          # which parts carry it
    keys: str           # what it fills in GroundTruth
    quantity: str       # "ohm", "F", "H", "C", "V"
    assumed: float      # a bare number is in these units (1e-3: mΩ)
    unit: str           # its name, for messages and the help
    lo: float           # usual range (SI units): outside it a value is used, with a note
    hi: float
    example: str        # an example value as a field would hold it
    feeds: str          # what the analysis does with it
    also: tuple = ()    # other field names read the same way (when the part has no `field`)


PARAMS = (
    Param("Rds(on)", "FET, switch IC", "rds_on (high side or integrated switch), rds_on_low (low side), per FET",
          "ohm", 1e-3, "mΩ", 1e-5, 1.0, "3.8milliohm", "conduction losses, thermal, a bridge's phase resistance"),
    Param("Coss", "FET", "c_oss: the switch-node device's (a side's parallel FETs together)", "F", 1e-12, "pF",
          1e-13, 1e-6, "616 picofarad", "ringing frequency and spectrum, the node charge current, switching loss"),
    Param("Qoss", "FET", "q_oss: as Coss", "C", 1e-9, "nC", 1e-12, 1e-5, "38 nanocoulomb",
          "switching loss Q_oss·V·f, the charge per edge"),
    Param("Vsd", "FET", "v_sd: reverse conduction in the dead time (a synchronous rectifier's, a bridge's switches)",
          "V", 1.0, "V", 0.0, 10.0, "2.1V", "dead-time losses, the drive model"),
    Param("Qrr", "FET", "q_rr: the synchronous rectifier's reverse-recovery charge (silicon body diode; GaN has none)",
          "C", 1e-9, "nC", 0.0, 1e-5, "62 nanocoulomb",
          "recovery loss Q_rr·V·f_sw, the recovery current spike in the spectrum and the overshoot"),
    Param("Vds", "FET", "the switch's voltage rating (read by the fix advisor)", "V", 1.0, "V", 1.0, 5000.0, "100V",
          "the overshoot's limit: the switch node's peak below 80 % of it"),
    # InvenTree's names for the ratings (kraft_flux_mini_2's parts carry them); "Voltage" / "Power" read too
    Param("Rated Voltage", "capacitor", "the capacitor's rated voltage (else a voltage in its value: 100n 50V)", "V",
          1.0, "V", 1.0, 20000.0, "50V", "cap_voltage: the voltage across it against its rating, derated",
          ("Voltage",)),
    Param("Rated Power", "resistor", "the resistor's rated power (else by its package)", "W", 1.0, "W", 0.005, 500.0,
          "1/16W", "resistor_power: its loss in the DC paths against its rating, derated", ("Power",)),
    # a switching controller's figures for its control loop (analysis/loop.py; "Control Mode" is text: CONTROL_MODE)
    Param("Vref", "controller", "the loop's feedback reference", "V", 1.0, "V", 0.05, 5.0, "0.8V",
          "the control loop: the output the divider regulates to (Vref / divider)"),
    Param("EA gm", "controller", "the error amplifier's transconductance (a gm amplifier)", "S", 1e-3, "mS", 1e-6,
          0.1, "1.8mmho", "the control loop: the compensator gm x Z(COMP)"),
    Param("EA Ro", "controller", "the gm amplifier's output resistance (its DC gain gm x Ro)", "ohm", 1.0, "Ω",
          1e3, 1e10, "2Mohm", "the control loop's gain at low frequency (empty: ideal)"),
    Param("EA Gain", "controller", "a voltage error amplifier's open-loop gain", "dB", 1.0, "dB", 20.0, 160.0, "80dB",
          "the control loop with an op-amp error amplifier (type II / III networks)"),
    Param("EA GBW", "controller", "a voltage error amplifier's gain-bandwidth", "Hz", 1e6, "MHz", 1e4, 1e9, "10MHz",
          "the control loop with an op-amp error amplifier"),
    Param("Vsense Max", "controller", "the maximum current-sense threshold", "V", 1e-3, "mV", 1e-3, 1.0, "75mV",
          "peak current mode: the current-sense gain Vsense Max / ITH Range"),
    Param("ITH Range", "controller", "the COMP / ITH swing from zero to the maximum sense threshold", "V", 1.0, "V",
          0.1, 5.0, "1.2V", "peak current mode: the current-sense gain"),
    Param("Current Sense Gain", "controller", "dV(sense) / dV(COMP), instead of Vsense Max and ITH Range", "gain",
          1.0, "V/V", 1e-4, 10.0, "0.0625", "peak current mode: the modulator gain"),
    Param("Slope Compensation", "controller", "the internal slope ramp at the current-sense input", "V/s", 1e6,
          "V/µs", 1e3, 1e10, "50mV/us", "peak current mode: the sampling double pole's damping (empty: half the "
          "inductor's down-slope)"),
    Param("Ramp", "controller", "the PWM ramp's amplitude (voltage mode)", "V", 1.0, "V", 0.1, 10.0, "1V",
          "voltage mode: the modulator gain 1 / Ramp"),
    Param("Vf", "diode", "diode_vf (a buck's catch diode)", "V", 1.0, "V", 0.0, 10.0, "0.55V",
          "catch-diode loss"),
    Param("Cj", "diode", "c_oss of a diode buck (the catch diode at the switch node)", "F", 1e-12, "pF", 1e-13, 1e-6,
          "120 picofarad", "ringing and the node charge current"),
    Param("DCR", "inductor", "dcr, and the inductor model's R", "ohm", 1e-3, "mΩ", 1e-5, 10.0, "6.9milliohm",
          "winding loss, DC paths"),
    Param("ESR", "capacitor", "the capacitor model's ESR", "ohm", 1e-3, "mΩ", 0.0, 100.0, "5milliohm",
          "ripple sharing, conducted emission, capacitor losses"),
    Param("ESL", "capacitor", "the capacitor model's ESL", "H", 1e-9, "nH", 0.0, 1e-6, "0.4nH",
          "hot-loop inductance, high-frequency sharing"),
    Param("Cbias", "capacitor", "the capacitor model's C_bias (its capacitance at its DC voltage)", "F", 1e-6, "µF",
          1e-13, 1.0, "4.2uF", "replaces C: ripple, conducted emission"),
    Param("Capacitance", "capacitor", "the capacitor model's C (else the Value field)", "F", 1.0, "F", 1e-13, 1.0,
          "10uF", "every solve"),
    Param("Inductance", "inductor", "the inductor model's L (else the Value field)", "H", 1.0, "H", 1e-10, 1.0,
          "5.6uH", "boost ripple, filters"),
    Param("Resistance", "resistor", "the resistor model's R (else the Value field)", "ohm", 1.0, "Ω", 0.0, 1e9,
          "8milliohm", "DC paths, series parts, signal routes"),
    # an op-amp stage in a signal path (model `amplifier`): gain is the stage's (set by its feedback resistors, so a
    # field on the symbol in the schematic), the others are the op-amp's datasheet values (InvenTree)
    Param("Gain", "op-amp", "the amplifier model's gain (V/V; negative = inverting; 20dB = 10)", "gain", 1.0, "V/V",
          -1e6, 1e6, "10", "signal paths: the output side's source"),
    Param("GBW", "op-amp", "the amplifier model's gbw: bandwidth = GBW / |gain|", "Hz", 1e6, "MHz", 1e3, 1e11,
          "10MHz", "signal paths: the gain's roll-off"),
    Param("Bandwidth", "op-amp", "a gain block's closed-loop -3 dB bandwidth (wins over GBW); on an op-amp solved "
          "in its circuit: its stage's bandwidth limit", "Hz", 1e6, "MHz", 1e-1, 1e11, "1MHz",
          "signal paths: the gain block's roll-off, or the bandwidth_limit check"),
    Param("Zout", "op-amp", "the amplifier model's open-loop output resistance R_O (the circuit divides it by its "
          "loop; a gain block: its output impedance)", "ohm", 1.0, "Ω", 0.0, 1e5, "2.3kohm",
          "signal paths: the output side's source impedance"),
    Param("Cin", "op-amp", "the amplifier model's input capacitance (each input to ground)", "F", 1e-12, "pF", 1e-14,
          1e-9, "3pF", "signal paths: the input side's load"),
    Param("Rin", "op-amp", "the amplifier model's input resistance", "ohm", 1.0, "Ω", 1.0, 1e15, "1Gohm",
          "signal paths: the input side's load"),
    Param("Slew Rate", "op-amp", "the amplifier model's slew (V/µs)", "V/s", 1e6, "V/µs", 1e3, 1e11, "20V/us",
          "signal paths: caps each output harmonic at SR / 2πf"),
    Param("CMRR", "op-amp", "the amplifier model's cmrr (dB): shown, not used (a single-ended path)", "dB", 1.0,
          "dB", 0.0, 200.0, "90dB", "nothing yet"),
    Param("Aol", "op-amp", "the amplifier model's aol: its open-loop DC gain (the circuit model)", "dB", 1.0, "dB",
          40.0, 200.0, "110dB", "signal paths: the op-amp's gain in its circuit (with GBW)"),
    Param("Fp2", "op-amp", "the amplifier model's fp2: a second open-loop pole (or PM)", "Hz", 1e6, "MHz", 1e4, 1e11,
          "20MHz", "signal paths: the op-amp's loop, its phase margin"),
    Param("PM", "op-amp", "the amplifier model's pm: its phase margin at unity gain, degrees (fp2 = "
          "GBW·sin(PM)·tan(PM))",
          "deg", 1.0, "°", 10.0, 90.0, "60", "signal paths: the op-amp's loop, its phase margin"),
)
BY_FIELD = {p.field.lower(): p for p in PARAMS}
# component model parameter -> field, per model kind
MODEL_FIELDS = {"cap": {"C": "Capacitance", "ESR": "ESR", "ESL": "ESL", "C_bias": "Cbias"},
                "inductor": {"L": "Inductance", "R": "DCR"},
                "resistor": {"R": "Resistance"},
                "amplifier": {"gain": "Gain", "gbw": "GBW", "bandwidth": "Bandwidth", "z_out": "Zout", "c_in": "Cin",
                              "aol": "Aol", "fp2": "Fp2", "pm": "PM",
                              "r_in": "Rin", "slew": "Slew Rate", "cmrr": "CMRR"}}


@dataclass(frozen=True)
class Reading:
    """One field read from one part: its value (or None when unusable), the text as written, where it came from
    and a note (why it was not used, or what unit was assumed)."""
    value: Optional[float]   # SI units; None: the field is there but unusable (see note)
    text: str                # the field as written
    source: str              # "Q301 Rds(on) 3.8milliohm"
    note: str = ""           # why it is not used, or "mΩ assumed"


def field_text(fp, name: str) -> Optional[str]:
    """A footprint's field by name, without regard to case (None without one)."""
    low = name.lower()
    for key, value in fp.fields.items():
        if key.strip().lower() == low:
            return value
    return None


def read(fp, name: str) -> Optional[Reading]:
    """One parameter of one part, or None when the part has no such field (or it is empty)."""
    p = BY_FIELD[name.lower()]
    field_name = p.field
    text = field_text(fp, p.field)
    for other in p.also:                        # the same parameter under another name (Voltage for Rated Voltage)
        if text is None or not text.strip():
            field_name, text = other, field_text(fp, other)
    if text is None or not text.strip():
        return None
    source = f"{fp.reference} {field_name} {text.strip()}"
    # gains, slew rates and dB values have no SI unit parse_quantity knows: _special reads those
    value, why = _special(text, p) if p.quantity in ("gain", "V/s", "dB", "S", "deg") else parse_quantity(
        text, p.quantity, p.assumed)
    if value is None:
        return Reading(None, text, source, why)
    note = f"{p.unit} assumed" if why == "unit assumed" else ""
    if not p.lo <= value <= p.hi:               # the user's value: used, with a note (a missing unit?)
        note = "; ".join(n for n in (note, f"{format_si(value, _symbol(p))} is outside the usual "
                                           f"{format_si(p.lo, _symbol(p))} … {format_si(p.hi, _symbol(p))}: "
                                           "used as written") if n)
    return Reading(value, text, source, note)


def _special(text: str, p: Param) -> tuple[Optional[float], str]:
    """Quantities parse_quantity doesn't know: a gain ("10", "10V/V", "-2", "20dB"), a slew rate ("20V/us",
    "20 volt / microsecond", "2e7V/s"; a bare number in V/µs), a ratio in dB ("90dB", "90")."""
    t = text.strip().replace(" ", "").replace(",", ".")
    low = t.lower()
    try:
        if p.quantity == "S":                           # a transconductance: "1.8mmho", "1.8mS", "1800uS", "0.0018"
            m = re.match(r"^([-+]?\d+(?:\.\d+)?(?:e[-+]?\d+)?)(.*)$", low)
            if not m:
                return None, f"'{text}': not a number"
            v, rest = float(m.group(1)), m.group(2)
            if not rest:
                return v * p.assumed, "unit assumed"
            for unit in ("siemens", "mhos", "mho", "a/v", "s"):
                if rest.endswith(unit):
                    prefix = rest[: -len(unit)]
                    scale = {"": 1.0, "m": 1e-3, "milli": 1e-3, "u": 1e-6, "µ": 1e-6, "μ": 1e-6, "micro": 1e-6,
                             "n": 1e-9}.get(prefix)
                    if scale is not None:
                        return v * scale, ""
            return None, f"'{text}': not a transconductance (S, mho)"
        if p.quantity == "dB":
            return float(re.sub(r"(?i)db$", "", t)), ""
        if p.quantity == "deg":                         # an angle in degrees: "60", "60°", "60deg", "60 degrees"
            return float(re.sub(r"(?i)(°|deg|degrees|degree)$", "", t)), ""
        if p.quantity == "gain":
            if low.endswith("db"):                      # a voltage gain in dB: 20 log10 |G|
                return 10 ** (float(t[:-2]) / 20), ""
            return float(re.sub(r"(?i)(v/v|x)$", "", t)), ""
        m = re.match(r"^([-+]?\d+(?:\.\d+)?(?:e[-+]?\d+)?)(.*)$", low)
        if not m:
            return None, f"'{text}': not a number"
        v, rest = float(m.group(1)), m.group(2)
        if not rest:
            return v * p.assumed, "unit assumed"
        per = {"v/us": 1e6, "v/µs": 1e6, "v/μs": 1e6, "volt/microsecond": 1e6, "volts/microsecond": 1e6,
               "v/ns": 1e9, "v/ms": 1e3, "v/s": 1.0, "volt/second": 1.0}
        if rest in per:
            return v * per[rest], ""
        # a prefixed volt: "50mV/us" (a slope compensation ramp), "0.5kV/us"
        volt = {"m": 1e-3, "u": 1e-6, "µ": 1e-6, "μ": 1e-6, "k": 1e3}
        if rest[:1] in volt and rest[1:] in per:
            return v * volt[rest[0]] * per[rest[1:]], ""
        return None, f"'{text}': not a slew rate (V/µs)"
    except ValueError:
        return None, f"'{text}': not a number"


def _symbol(p: Param) -> str:
    """The unit symbol for messages ("Ω" for ohm, else the quantity as it is: "F", "V" ...)."""
    return {"ohm": "Ω", "deg": "°"}.get(p.quantity, p.quantity)


def _common(board, refs: list[str], name: str, times: int = 1) -> tuple[Optional[Reading], list[str]]:
    """The one value the parts `refs` agree on (× times: a side's parallel FETs together), and notes. Parts without
    the field are skipped; parts that disagree give no value."""
    # parallel switches (or the legs of a bridge) should be the same part: one value only when they all agree
    readings = [r for r in (read(fp, name) for fp in (board.footprint(x) for x in refs) if fp is not None) if r]
    notes = [f"{r.source}: {r.note}" for r in readings if r.value is None]
    good = [r for r in readings if r.value is not None]
    if not good:
        return None, notes
    if any(abs(r.value - good[0].value) > REL_TOL * abs(good[0].value) for r in good):
        notes.append(f"{name} differs between {', '.join(r.source for r in good)}: not used")
        return None, notes
    r = good[0]
    if times == 1:
        return r, notes
    return Reading(r.value * times, r.text, f"{r.source} × {times}", r.note), notes


def stage_values(board, config, spec) -> tuple[dict[str, Reading], list[str]]:
    """{key: Reading} a converter's or bridge's parameters take from its parts' fields, and notes (unreadable or
    disagreeing fields). Per FET: rds_on, rds_on_low, v_sd; per switch-node side (the parallel FETs together):
    c_oss, q_oss, and the synchronous rectifier's q_rr; a catch diode's Cj is a diode buck's c_oss, its Vf the
    diode_vf; the inductor's DCR the dcr; the controller's GT dead-time words (dead:20ns, dead_rise:20ns,
    analysis/tags.py) the dead_rise and dead_fall."""
    from ..config.settings import BridgeSpec
    out: dict[str, Reading] = {}
    notes: list[str] = []

    def put(key, refs, name, times=1):
        """The common value of `name` over `refs` (× times) into out[key], unless an earlier field filled it."""
        r, n = _common(board, refs, name, times)
        notes.extend(n)
        if r is not None and key not in out:
            out[key] = r

    if isinstance(spec, BridgeSpec):
        hs = [r for leg in spec.legs for r in leg.high_side]
        ls = [r for leg in spec.legs for r in leg.low_side]
        # C_oss / Q_oss are per switch-node side: n parallel FETs give n x the part's value, so only when every
        # side of every leg has the same number of FETs
        counts = {len(leg.low_side) for leg in spec.legs} | {len(leg.high_side) for leg in spec.legs}
        put("rds_on", hs, "Rds(on)")
        put("rds_on_low", ls, "Rds(on)")
        put("v_sd", hs + ls, "Vsd")
        if len(counts) == 1:
            n = counts.pop()
            put("c_oss", hs + ls, "Coss", n)
            put("q_oss", hs + ls, "Qoss", n)
        return out, notes
    hs = spec.high_side or spec.switch
    put("rds_on", hs, "Rds(on)")
    put("rds_on_low", spec.low_side, "Rds(on)")              # a catch diode has none
    # the capacitance switched at the node: the low side's (or an integrated switch's); for a diode buck the
    # catch diode's junction capacitance Cj takes that role when no Coss is given (put keeps the first found)
    node = spec.low_side or spec.switch
    put("c_oss", node, "Coss", len(node))
    put("c_oss", spec.low_side, "Cj", len(spec.low_side))
    put("q_oss", node, "Qoss", len(node))
    put("diode_vf", spec.low_side, "Vf")
    # the synchronous rectifier (a buck's low side, a boost's high side): its reverse conduction in the dead times
    # and its body diode's recovered charge (the side's parallel FETs together: each recovers its own share)
    rect = [r for r in (spec.high_side if spec.topology == "boost" else spec.low_side) if not r.upper().startswith("D")]
    put("v_sd", rect, "Vsd")
    put("q_rr", rect, "Qrr", len(rect))
    if rect:
        for edge, r in _controller_dead_times(board, spec).items():
            out.setdefault(f"dead_{edge}", r)
    from ..analysis.power import switch_inductor
    _, inductor = switch_inductor(board, config, spec)
    if inductor is not None:
        put("dcr", [inductor.reference], "DCR")
    return out, notes


def _controller_dead_times(board, spec) -> dict[str, Reading]:
    """{edge: Reading} of the dead times the stage's controller IC gives in its GT field (analysis/tags.
    dead_time_of), per switch-node edge ("rise", "fall"): for the stage's GT id, its SW pin's channel, or every
    stage of the IC. Empty without a controller or such a word."""
    from ..analysis.loop import controller
    from ..analysis.tags import EDGES, dead_time_of, stage_ident
    ctrl, channel = controller(board, spec)
    if ctrl is None:
        return {}
    ident, out = stage_ident(board, spec.parts), {}
    for edge in EDGES:
        hit = dead_time_of(ctrl, ident, channel, edge)
        if hit is not None:
            out[edge] = Reading(hit[0], hit[1], f"{ctrl.reference} GT {hit[1]}")
    return out


def model_values(fp, kind: str) -> tuple[dict[str, Reading], list[str]]:
    """{model parameter: Reading} a part's model takes from its fields (MODEL_FIELDS), and notes."""
    out, notes = {}, []
    for param, name in MODEL_FIELDS.get(kind, {}).items():
        r = read(fp, name)
        if r is None:
            continue
        if r.value is None:
            notes.append(f"{r.source}: {r.note}")
        else:
            out[param] = r
    return out, notes


def same(a: Optional[float], b: Optional[float]) -> bool:
    """Two values are the same to within REL_TOL (format_si's rounding when a value went through the form)."""
    return a is not None and b is not None and abs(a - b) <= REL_TOL * max(abs(a), abs(b), 1e-30)


STAGE_KEYS = {"converter": ("rds_on", "rds_on_low", "c_oss", "q_oss", "diode_vf", "dcr", "v_sd", "q_rr",
                            "dead_rise", "dead_fall"),
              "bridge": ("rds_on", "rds_on_low", "c_oss", "q_oss", "v_sd")}


@dataclass(frozen=True)
class Difference:
    """A value set in GroundTruth that differs from the schematic's field (an info issue, and a mark in the
    form): where, which key, both values."""
    kind: str           # "converter" | "bridge" | "part"
    name: str           # the stage's name or the part's reference
    key: str            # rds_on, c_oss ... or a model parameter (ESR)
    ours: float         # the value set in GroundTruth
    schematic: Reading  # what the fields say

    def message(self) -> str:
        """The difference as the issue's message: both values with their units and the field's source."""
        unit = _KEY_UNITS.get(self.key, "")
        return (f"{self.name}: {self.key} {format_si(self.ours, unit)} set in GroundTruth, the schematic says "
                f"{format_si(self.schematic.value, unit)} ({self.schematic.source})")


_KEY_UNITS = {"rds_on": "Ω", "rds_on_low": "Ω", "dcr": "Ω", "ESR": "Ω", "R": "Ω", "c_oss": "F", "C": "F",
              "C_bias": "F", "q_oss": "C", "q_rr": "C", "dead_rise": "s", "dead_fall": "s", "v_sd": "V", "diode_vf": "V",
              "ESL": "H", "L": "H", "gbw": "Hz",
              "bandwidth": "Hz", "z_out": "Ω", "c_in": "F", "r_in": "Ω", "slew": "V/s", "cmrr": "dB", "gain": "",
              "aol": "dB", "fp2": "Hz", "pm": "°"}


def _sync(getter, setter, linked: list[str], values: dict, keys, default=None) -> bool:
    """Take the schematic's values for the linked keys and the empty ones (or ones equal to the schematic's, or
    `default(key)`, the value it would have without fields); a linked key whose field is gone goes back to the
    default. Returns whether anything changed. `linked` is changed in place."""
    changed = False
    for key in keys:
        r = values.get(key)
        cur = getter(key)
        # The rule: a value that came from the schematic follows it; an empty value, or one that equals the
        # schematic's (or what the part would have without fields), joins it; any other value is the user's
        # override and stays.
        if r is not None:
            if key in linked or cur is None or same(cur, r.value) or (default and same(cur, default(key))):
                if not same(cur, r.value):
                    setter(key, r.value)
                    changed = True
                if key not in linked:
                    linked.append(key)
                    changed = True
        elif key in linked:
            linked.remove(key)
            setter(key, default(key) if default else None)
            changed = True
    return changed


def fill_stage(board, config, spec) -> tuple[bool, list[str]]:
    """One converter's or bridge's parameters from its parts' fields (in place, as apply_fields): (changed, notes).
    Detection calls it for every stage it finds, so a new stage starts with the schematic's values."""
    from ..config.settings import BridgeSpec
    values, notes = stage_values(board, config, spec)
    kind = "bridge" if isinstance(spec, BridgeSpec) else "converter"
    changed = _sync(lambda k: getattr(spec, k), lambda k, v: setattr(spec, k, v), spec.from_schematic, values,
                    STAGE_KEYS[kind])
    return changed, notes


def stage_marks(board, config, spec) -> dict[str, tuple[str, str]]:
    """{key: (state, explanation)} for a stage's form: state "schematic" (the value comes from the fields),
    "differs" (set in GroundTruth, the schematic says otherwise) or "" (no field)."""
    from ..config.settings import BridgeSpec
    values, _ = stage_values(board, config, spec)
    out = {}
    for key in STAGE_KEYS["bridge" if isinstance(spec, BridgeSpec) else "converter"]:
        r, cur = values.get(key), getattr(spec, key)
        unit = _KEY_UNITS.get(key, "")
        if r is None:
            out[key] = ("", "")
        elif key in spec.from_schematic or same(cur, r.value):
            out[key] = ("schematic", f"from the schematic: {r.source}" + (f" ({r.note})" if r.note else ""))
        elif cur is None:
            out[key] = ("", "")
        else:
            out[key] = ("differs", f"set here: {format_si(cur, unit)}; the schematic says "
                                   f"{format_si(r.value, unit)} ({r.source}). Use schematic values takes it.")
    return out


def link_form(board, config, new, old=None):
    """A stage from a form (Add, Update), in place: a key stays linked to the schematic while its value is
    unchanged, a value equal to the schematic's is linked (typed in or kept), and an emptied one takes the
    schematic's value (fill_stage); every other value is the user's."""
    from ..config.settings import BridgeSpec
    values, _ = stage_values(board, config, new)
    linked = []
    for key in STAGE_KEYS["bridge" if isinstance(new, BridgeSpec) else "converter"]:
        cur, r = getattr(new, key), values.get(key)
        kept = old is not None and key in old.from_schematic and (same(cur, getattr(old, key)) or
                                                                  (cur is None and getattr(old, key) is None))
        if r is not None and (kept or same(cur, r.value)):
            linked.append(key)
    new.from_schematic = linked
    fill_stage(board, config, new)


def use_schematic(board, config, spec) -> list[str]:
    """Put every parameter the fields give back to the schematic's value (in place). Returns the keys."""
    values, _ = stage_values(board, config, spec)
    for key, r in values.items():
        setattr(spec, key, r.value)
        if key not in spec.from_schematic:
            spec.from_schematic.append(key)
    return list(values)


def apply_fields(config, board) -> dict[str, list[str]]:
    """Bring the settings in line with the schematic fields (in place): every stage's and part's linked values
    are refreshed, empty ones filled, overrides kept. Returns {"converters"|"bridges"|"parts": [names changed],
    "notes": [...]}."""
    from .defaults import suggest_model
    done: dict[str, list[str]] = {"converters": [], "bridges": [], "parts": [], "notes": []}
    for kind, specs in (("converter", config.converters), ("bridge", config.bridges)):
        for spec in specs:
            changed, notes = fill_stage(board, config, spec)
            done["notes"] += [f"{spec.name}: {n}" for n in notes]
            if changed:
                done[kind + "s"].append(spec.name)
    for ref, entry in config.components.items():
        fp = board.footprint(ref)
        m = entry.model
        if entry.removed or fp is None or m.kind not in MODEL_FIELDS:
            continue
        values, notes = model_values(fp, m.kind)
        done["notes"] += [f"{ref}: {n}" for n in notes]
        plain = suggest_model(fp, fields=False)        # the part without its fields: what a value falls back to
        if _sync(lambda k: m.params.get(k), lambda k, v: (m.params.pop(k, None) if v is None
                                                              else m.params.__setitem__(k, v)),
                 m.from_schematic, values, list(MODEL_FIELDS[m.kind]),
                 default=lambda k: plain.params.get(k) if plain.kind == m.kind else None):
            done["parts"].append(ref)
    return done


def differences(config, board) -> list[Difference]:
    """The values set in GroundTruth that differ from what the schematic fields say."""
    out = []
    for kind, specs in (("converter", config.converters), ("bridge", config.bridges)):
        for spec in specs:
            values, _ = stage_values(board, config, spec)
            for key in STAGE_KEYS[kind]:
                r, cur = values.get(key), getattr(spec, key)
                if r is not None and key not in spec.from_schematic and cur is not None and not same(cur, r.value):
                    out.append(Difference(kind, spec.name, key, cur, r))
    for ref, entry in config.components.items():
        fp = board.footprint(ref)
        m = entry.model
        if entry.removed or fp is None or m.kind not in MODEL_FIELDS:
            continue
        values, _ = model_values(fp, m.kind)
        for key, r in values.items():
            cur = m.params.get(key)
            if key not in m.from_schematic and cur is not None and not same(cur, r.value):
                out.append(Difference("part", ref, key, cur, r))
    return out
