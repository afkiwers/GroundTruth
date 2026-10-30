"""Electrical models a footprint can be replaced with.

Every model is a two-terminal impedance between the first two pads of the part (or between
the pads the user names). Multi-pin parts (ICs, connectors) are `open`: their pins are
analysis ports, never a path for current through the part. An `amplifier` (an op-amp stage in a
signal path) is no impedance: it takes the voltage at its input pin and drives its output pin with
gain × that voltage (emi/path.py PathAmp); `pins` names the input and the output.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Optional

MODEL_KINDS = ("short", "open", "resistor", "cap", "inductor", "ferrite", "amplifier", "buffer")

# Parameters each kind uses (also the editable columns in the UI and the YAML keys)
MODEL_PARAMS: dict[str, tuple[str, ...]] = {
    "short": (),
    "open": (),
    "resistor": ("R",),
    "cap": ("C", "ESR", "ESL", "C_bias"),
    "inductor": ("L", "R"),
    "ferrite": ("R", "L"),
    # an op-amp stage: gain (V/V, the stage's closed-loop gain, negative = inverting), gbw / bandwidth (Hz),
    # z_out (Ω), c_in (F), r_in (Ω), slew (V/s), cmrr (dB, kept and shown: a single-ended path has no common mode);
    # for the circuit model (analysis/analog_net, emi/mna.Circuit.opamp): aol (open-loop DC gain, dB), fp2 (a second
    # open-loop pole, Hz) or pm (the phase margin at unity gain, degrees: fp2 = gbw·sin(pm)·tan(pm))
    "amplifier": ("gain", "gbw", "bandwidth", "z_out", "c_in", "r_in", "slew", "cmrr", "aol", "fp2", "pm"),
    # a logic buffer (a Schmitt trigger, SN74LVC2G17): not linear. Its input loads the path before it (c_in); its
    # output drives the path after it with an edge of its own: rise (s), behind z_out (Ω), swinging its supply
    # pin's voltage. vt_pos / vt_neg (V): its input thresholds; hyst (V): their difference, when only that is given
    "buffer": ("rise", "z_out", "c_in", "vt_pos", "vt_neg", "hyst"),
}
# Parameters that may stay empty: C_bias, a ceramic capacitor's capacitance at its DC bias (from the maker's
# DC-bias curve; an X7R 10 µF 100 V 1210 keeps ~30-60 % at 50 V), used instead of C where given
OPTIONAL_PARAMS = {"C_bias", "gbw", "bandwidth", "z_out", "c_in", "r_in", "slew", "cmrr", "aol", "fp2", "pm",
                   "rise", "vt_pos", "vt_neg", "hyst"}
AMP_Z_OUT = 1.0         # Ω, an amplifier's output impedance when its z_out is not given
AMP_C_IN = 2e-12        # F, its input capacitance when its c_in is not given
AMP_IN = re.compile(r"^(\+IN|IN\+|INP|IN_P|\+|NONINV|VIN\+|\+VIN|IN)$", re.I)     # the input pin's name
AMP_OUT = re.compile(r"^(OUT|VOUT|OUTPUT|VO|O)$", re.I)                              # the output pin's name
# a logic buffer's values when its fields don't give them [A73]: CMOS logic of the LVC class at 5 V
BUF_RISE = 2.5e-9       # s, its output's edge
BUF_Z_OUT = 25.0        # Ω, its output resistance
BUF_C_IN = 5e-12        # F, its input capacitance
BUF_IN = re.compile(r"^(\d*)(A|IN)$", re.I)          # a buffer channel's input pin name (1A, A, IN)
BUF_OUT = re.compile(r"^(\d*)(Y|OUT)$", re.I)        # its output pin name (1Y, Y, OUT)
GROUND_NAME = re.compile(r"^(A|D|P|S)?GNDA?\w*$|^VSS\w*$|^0$", re.I)       # a net named as ground (its last part)

SHORT_OHMS = 1e-3       # numerical stand-in for a dead short
SERIES_PATH_MAX_OHMS = 1e3  # resistors above this are pull-ups/downs, not part of a signal path


def _tidy(v: float) -> float:
    """Round to 6 significant digits so YAML shows 1e-05, not 9.999999999999999e-06."""
    return float(f"{v:.6g}")


@dataclass
class ComponentModel:
    """What a part is to the analysis: one of MODEL_KINDS with its parameters (SI units: ohm, F, H ...), a note
    for the user (how it was guessed), the two pins it joins when the part has more, and which parameters came
    from the schematic symbol's fields (those are refreshed from the fields; the others are the user's)."""
    kind: str = "open"
    params: dict[str, float] = field(default_factory=dict)
    note: str = ""
    pins: list[str] = field(default_factory=list)   # the two pins it joins (pad numbers or pin names), when the part
                                                    # has more: a current-sense IC's IP+ / IP-; empty = all its pads
    from_schematic: list[str] = field(default_factory=list)   # params taken from the symbol's fields (fields.py):
                                                              # refreshed from them, the others are set here
    # a capacitor's DC-bias curve as text (components/bias.py: "75nF@50V", "95nF@10V, 39nF@50V", "75nF"), stored
    # under the settings' C_bias key; params["C_bias"] is the capacitance at the part's own DC voltage, set per run
    # (analysis/bias.at_dc), which capacitance() and every solver read
    bias: str = ""

    def __post_init__(self):
        """Refuse an unknown kind at once (a typo in the settings file), with the valid ones in the message."""
        if self.kind not in MODEL_KINDS:
            raise ValueError(f"unknown model {self.kind!r}; choose one of {', '.join(MODEL_KINDS)}")

    def get(self, name: str, default: float = 0.0) -> float:
        """A parameter as a float, `default` when it is not set."""
        v = self.params.get(name)
        return default if v is None else float(v)

    def missing_params(self) -> list[str]:
        """The parameters this kind needs that are not set (optional ones left out): shown as "set ..." hints."""
        return [p for p in MODEL_PARAMS[self.kind] if self.params.get(p) is None and p not in OPTIONAL_PARAMS]

    def capacitance(self) -> float:
        """A capacitor's capacitance in the circuit: at its own DC voltage (params C_bias, set for a run by
        analysis/bias.at_dc), else a single bias value without a voltage (taken as it is), else the nominal C."""
        c = self.params.get("C_bias")
        if c is not None and c > 0:
            return float(c)
        flat = self.flat_bias()
        return flat if flat is not None else self.get("C")

    def flat_bias(self) -> Optional[float]:
        """The bias curve's value when it is one capacitance without a voltage ("75nF", as Cbias always was), else
        None (a curve needs the part's voltage: analysis/bias.at_dc)."""
        if not self.bias:
            return None
        from .bias import parse_bias
        points, _ = parse_bias(self.bias)
        if len(points) == 1 and points[0].v is None and points[0].c is not None:
            return points[0].c
        return None

    def value_of(self, key: str):
        """A parameter as the settings and the schematic sync see it: the bias curve's text for C_bias (a cap),
        else the number (None when not set)."""
        if key == "C_bias" and self.kind == "cap":
            return self.bias or None
        return self.params.get(key)

    def set_value(self, key: str, value) -> None:
        """Set (or, with None, clear) a parameter as value_of reads it: C_bias as its curve text."""
        if key == "C_bias" and self.kind == "cap":
            from .bias import bias_text, parse_bias
            points, _ = parse_bias(value)
            self.bias = bias_text(points) if points else ""
            self.params.pop("C_bias", None)
        elif value is None:
            self.params.pop(key, None)
        else:
            self.params[key] = float(value)

    def impedance(self, f: float) -> Optional[complex]:
        """Complex impedance at frequency f (Hz). None means open circuit."""
        w = 2 * math.pi * f
        k = self.kind
        if k == "open":
            return None
        if k == "short":
            return complex(SHORT_OHMS, 0)
        if k == "resistor":
            return complex(max(self.get("R"), SHORT_OHMS), 0)
        if k == "cap":
            # a real capacitor is a series R-L-C [A24]: ESR + j(w ESL - 1/(w C)); below its self-resonance
            # 1/(2 pi sqrt(ESL C)) it is capacitive, above it inductive (at DC: open)
            c = self.capacitance()
            if c <= 0:
                return None
            return complex(self.get("ESR"), w * self.get("ESL") - (1 / (w * c) if w > 0 else math.inf))
        if k in ("inductor", "ferrite"):
            # a series R-L (its winding's DCR and inductance; a ferrite modelled the same way [A24], see defaults.py)
            return complex(max(self.get("R"), SHORT_OHMS), w * self.get("L"))
        return None

    def amp_gain(self, f) -> complex:
        """An amplifier's voltage gain at f: `gain` (default 1), one pole at `bandwidth` (the closed-loop -3 dB), or
        at gbw / |gain| (the gain-bandwidth product over the noise gain, taken as |gain|, at least 1)."""
        g = complex(self.params.get("gain", 1.0) if self.params.get("gain") is not None else 1.0)
        bw = self.params.get("bandwidth")
        if not bw and self.params.get("gbw"):
            # a voltage-feedback op-amp's closed-loop bandwidth is its gain-bandwidth product over the noise gain
            bw = self.params["gbw"] / max(abs(g), 1.0)
        # a single-pole low-pass: g / (1 + j f / f_-3dB); no bandwidth given = an ideal, flat gain
        return g / (1 + 1j * f / bw) if bw else g

    def amp_z_in(self, f) -> complex:
        """The impedance an amplifier's input puts on the path: r_in (default open) ∥ c_in (default 2 pF)."""
        # in parallel: admittances add (j w C_in + 1/R_in); at f = 0 with no R_in the input is open (1 TΩ)
        y = 1j * 2 * math.pi * f * (self.params.get("c_in") or AMP_C_IN)
        if self.params.get("r_in"):
            y = y + 1.0 / self.params["r_in"]
        return 1.0 / y if y != 0 else complex(1e12)

    def amp_z_out(self) -> float:
        """An amplifier's output impedance (ohm) as given: z_out, else AMP_Z_OUT (a closed-loop op-amp output is
        low). The path model uses amp_z_out_at."""
        return float(self.params.get("z_out") or AMP_Z_OUT)

    def amp_z_out_at(self, f) -> complex:
        """The stage's output impedance at f (ohm). z_out is the op-amp's open-loop output impedance (the datasheet's
        R_O or Z_O, e.g. 2.3 kΩ for a TLV6001): with its loop known (`bandwidth`, else gbw / |gain|: the closed-loop
        corner f_c, as in amp_gain) the feedback divides it by 1 + Aβ, A = GBW/jf, so Z_out = z_out·jf/(jf + f_c)
        [Franco 2015]: far below z_out up to f_c, inductive, z_out above it. Without a loop it is used as given;
        without z_out, AMP_Z_OUT flat. (Before 2026-10-21 z_out was taken as flat at every frequency: refboard's
        TLV6001 followers put 2.3 kΩ in series with R303 and moved the ADC filter's corner from 451 to 303 Hz.)"""
        zo = self.params.get("z_out")
        if not zo:
            return complex(AMP_Z_OUT)
        g = complex(self.params.get("gain", 1.0) if self.params.get("gain") is not None else 1.0)
        bw = self.params.get("bandwidth") or (self.params["gbw"] / max(abs(g), 1.0) if self.params.get("gbw")
                                              else None)
        if not bw:
            return complex(zo)
        return complex(zo) * 1j * f / (1j * f + bw)

    def buf_rise(self) -> float:
        """A buffer's output edge (s): `rise`, else BUF_RISE [A73]."""
        return float(self.params.get("rise") or BUF_RISE)

    def buf_z_out(self) -> float:
        """A buffer's output resistance (Ω): `z_out`, else BUF_Z_OUT [A73]."""
        return float(self.params.get("z_out") or BUF_Z_OUT)

    def buf_z_in(self, f) -> complex:
        """The impedance a buffer's input puts on the path before it: its c_in (else BUF_C_IN [A73]) to ground."""
        y = 1j * 2 * math.pi * f * (self.params.get("c_in") or BUF_C_IN)
        return 1.0 / y if y != 0 else complex(1e12)

    def buf_hysteresis(self) -> Optional[float]:
        """A buffer's input hysteresis (V): `hyst`, else vt_pos − vt_neg when both are given, else None."""
        if self.params.get("hyst"):
            return float(self.params["hyst"])
        hi, lo = self.params.get("vt_pos"), self.params.get("vt_neg")
        return float(hi - lo) if hi is not None and lo is not None and hi > lo else None

    def passes_signal(self) -> bool:
        """True if a signal route may run through this part in series (termination R, 0R, bead...)."""
        if self.kind in ("short", "ferrite", "inductor"):
            return True
        if self.kind == "resistor":
            return self.get("R", math.inf) <= SERIES_PATH_MAX_OHMS
        return False

    def to_dict(self) -> dict:
        """The model as the settings file writes it: {model: kind, the set parameters (tidied), pins,
        from_schematic, note}; only what is set appears."""
        d: dict = {"model": self.kind}
        for p in MODEL_PARAMS[self.kind]:
            if p == "C_bias" and self.kind == "cap" and self.bias:
                # the curve as text; one value without a voltage as the number old files hold (they stay as written)
                flat = self.flat_bias()
                d[p] = _tidy(flat) if flat is not None else self.bias
            elif p in self.params:
                d[p] = _tidy(self.params[p])
        if self.pins:
            d["pins"] = list(self.pins)
        if self.from_schematic:
            d["from_schematic"] = [p for p in MODEL_PARAMS[self.kind] if p in self.from_schematic]
        if self.note:
            d["note"] = self.note
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "ComponentModel":
        """A model from its settings-file entry (the reverse of to_dict). Parameters the kind does not use are
        ignored; `pins` and `from_schematic` may be lists or comma-separated text; a bad kind or a `pins` that
        does not name exactly two pins raises ValueError (the settings report it with the part's reference)."""
        kind = str(d.get("model", "open"))
        if kind not in MODEL_KINDS:
            raise ValueError(f"unknown model {kind!r}; choose one of {', '.join(MODEL_KINDS)}")
        params, bias = {}, ""
        for p in MODEL_PARAMS[kind]:
            if p == "C_bias" and kind == "cap":
                if d.get(p) is not None:            # a number (old files) or a curve's text (components/bias.py)
                    from .bias import bias_text, parse_bias
                    points, problems = parse_bias(d[p])
                    if problems:
                        raise ValueError("C_bias: " + "; ".join(problems))
                    bias = bias_text(points)
            elif d.get(p) is not None:
                params[p] = float(d[p])
        pins = d.get("pins") or []
        if isinstance(pins, str):
            pins = [p.strip() for p in pins.split(",") if p.strip()]
        if kind == "buffer":
            if len(pins) % 2:
                raise ValueError("pins names a buffer's channels as input / output pairs, e.g. [1, 6, 3, 4]")
        elif pins and len(pins) != 2:
            raise ValueError("pins names the two pins the model joins, e.g. [IP+, IP-]")
        linked = d.get("from_schematic") or []
        if isinstance(linked, str):
            linked = [p.strip() for p in linked.split(",") if p.strip()]
        return cls(kind, params, str(d.get("note", "") or ""), [str(p) for p in pins],
                   [str(p) for p in linked if str(p) in MODEL_PARAMS[kind]], bias)


KELVIN_FORCE_RATIO = 1.5    # a 4-terminal shunt's force pads are at least this much larger than its sense pads


def pad_area(p) -> float:
    """A pad's copper area (mm²): its outline's when the board gives one (a custom pad: its anchor and its
    primitives, whose `size` is only the small anchor), else its size's rectangle."""
    if p.polygon is not None:
        from shapely.geometry import Polygon as _Poly
        outer, holes = p.polygon
        try:
            return float(_Poly(outer, holes).area)
        except (ValueError, TypeError):
            pass
    return float(p.size[0] * p.size[1])


def kelvin_force_pads(fp) -> Optional[list]:
    """The two force pads of a 4-terminal (Kelvin) resistor: a part with four or more connected pads on at least
    three nets whose two largest pads are on two different nets and each at least KELVIN_FORCE_RATIO times the area
    of every other pad (the sense pads are small) [A72]. None when the pads don't tell (equal sizes, two nets)."""
    pads = [p for p in fp.pads if p.net]
    if len(pads) < 4 or len({p.net for p in pads}) < 3:
        return None
    # by copper area (pad_area: a custom pad's whole outline; a battery shunt's custom force pads can have an anchor
    # smaller than its sense pads: by `size` the sense pads were taken as the force pads)
    area = sorted(pads, key=lambda p: -pad_area(p))
    a, b, rest = area[0], area[1], area[2:]
    small = max(pad_area(p) for p in rest)
    if a.net == b.net or min(pad_area(a), pad_area(b)) < KELVIN_FORCE_RATIO * small:
        return None
    return [p for p in fp.pads if p.net in (a.net, b.net)]


def buffer_pins(fp, model: Optional[ComponentModel], net: Optional[str] = None) -> list:
    """A logic buffer's channels as (input pad, output pad) pairs, both on a net: from the model's `pins` (input,
    output, input, output ...: pad numbers or pin names), else the part's GT labels (buf_in / buf_out, in order),
    else its pin names paired by their channel digit (1A with 1Y, A with Y, IN with OUT). With `net`: only the
    channels with a pad on it. [] for a part that is no buffer, or whose channels can't be told."""
    if model is None or model.kind != "buffer":
        return []

    def pad(name):
        """The pad named `name`: by its number first, else by its schematic pin function."""
        want = str(name).upper()
        by_number = next((p for p in fp.pads if str(p.number).upper() == want), None)
        return by_number or next((p for p in fp.pads if (p.pin_function or "").upper() == want), None)
    names = list(model.pins) if len(model.pins) >= 2 else []
    if not names:
        from ..analysis.tags import buf_label_pins       # GT = buf_in:1, buf_out:6 on the symbol
        names = [x for pair in buf_label_pins(fp) for x in pair]
    if names:
        pairs = [(pad(names[k]), pad(names[k + 1])) for k in range(0, len(names) - 1, 2)]
    else:                       # the pin names: an input and an output of one channel (its digit, or none)
        ins = {m.group(1): p for p in fp.pads if (m := BUF_IN.match((p.pin_function or "").strip()))}
        outs = {m.group(1): p for p in fp.pads if (m := BUF_OUT.match((p.pin_function or "").strip()))}
        pairs = [(ins[k], outs[k]) for k in sorted(ins) if k in outs]
    pairs = [(a, b) for a, b in pairs if a is not None and b is not None and a.net and b.net and a.net != b.net]
    return [(a, b) for a, b in pairs if net is None or net in (a.net, b.net)]


def _gate_known(fp, gate: str) -> bool:
    """Whether a transistor's gate net is told, not guessed: a pin named as a gate (G, GN, GATE) is on it, or it has
    fewer pads than each power net (a power FET's single gate pad beside its many drain and source pads). Three nets
    of one pad each (a SOT-23 without pin names) tell nothing: detect.fet_terms then picks by name order."""
    from ..analysis.detect import GATE_FN
    if any(p.net == gate and GATE_FN.match(p.pin_function or "") for p in fp.pads):
        return True
    count = {}
    for p in fp.pads:
        if p.net:
            count[p.net] = count.get(p.net, 0) + 1
    return all(count[gate] < c for n, c in count.items() if n != gate)


def model_pads(fp, model: Optional[ComponentModel]) -> list:
    """The pads a part's model joins: the two named in `pins` (by pad number or pin name), else a 4-terminal
    resistor's force pads (kelvin_force_pads: its current never flows through its sense pads), else all of them."""
    if model is not None and not model.pins and model.kind == "resistor":
        force = kelvin_force_pads(fp)
        if force is not None:
            return force
    if model is not None and not model.pins and model.kind in ("short", "resistor"):
        # a transistor taken as closed (a surge stopper's or an ideal diode's FET: a short, or its R_DS(on)) joins its
        # drain and source, never its gate: a short over all three pins put the gate net on the power path
        from ..analysis.detect import fet_terms
        from .defaults import ref_prefix
        terms = fet_terms(fp) if ref_prefix(fp.reference) in ("Q", "T", "M") else None
        if terms is not None and _gate_known(fp, terms[0]):
            return [p for p in fp.pads if p.net in terms[1]]
    if model is None or not model.pins:
        return list(fp.pads)
    want = {p.upper() for p in model.pins}
    return [p for p in fp.pads if str(p.number).upper() in want or (p.pin_function or "").upper() in want]


def amplifier_pins(fp, model: Optional[ComponentModel]):
    """(input pad, output pad) of an amplifier: the two `pins` in that order (pad number or pin name), else its GT
    labels (amp_in:3, amp_out:1), else the one non-inverting input and the one output found by their pin names (a
    single op-amp: +IN / IN+ / INP and OUT). None when they can't be told (a dual or quad op-amp without labels)."""
    if model is None or model.kind != "amplifier":
        return None
    names = list(model.pins[:2]) if len(model.pins) >= 2 else None
    if names is None:
        from ..analysis.tags import amp_label_pins       # GT = amp_in:3, amp_out:1 on the symbol
        labelled = amp_label_pins(fp)
        names = list(labelled) if labelled else None
    if names is not None:
        def pad(name):
            """The pad named `name`: by its number first, else by its schematic pin function."""
            want = name.upper()
            return next((p for p in fp.pads if str(p.number).upper() == want), None) or \
                next((p for p in fp.pads if (p.pin_function or "").upper() == want), None)
        a, b = pad(names[0]), pad(names[1])
        return (a, b) if a is not None and b is not None and a.net and b.net else None
    # no names given: by the schematic pin names, but only when they are unambiguous (one input, one output); else
    # the op-amp by its pin roles (analysis/analog_net.opamp_pin_roles: an unnamed output by its pin type)
    from ..analysis.analog_net import opamp_pin_roles
    roles = opamp_pin_roles(fp)
    ins = [p for p in fp.pads if AMP_IN.match((p.pin_function or "").strip()) and p.net]
    outs = [p for p in fp.pads if AMP_OUT.match((p.pin_function or "").strip()) and p.net]
    if len(ins) == 1 and len(outs) == 1:
        pair = (ins[0], outs[0])
    elif roles is not None and roles["inp"].net and roles["out"].net:
        pair = (roles["inp"], roles["out"])
    else:
        return None
    # an inverting stage: its + input on ground, the signal comes in at its − input (through the input resistor)
    if roles is not None and GROUND_NAME.match(pair[0].net.split("/")[-1]) and roles["inn"].net:
        pair = (roles["inn"], pair[1])
    return pair


def model_nets(fp, model: Optional[ComponentModel]) -> set[str]:
    """The nets a part's model joins (see model_pads)."""
    return {p.net for p in model_pads(fp, model) if p.net}
