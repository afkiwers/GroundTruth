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

MODEL_KINDS = ("short", "open", "resistor", "cap", "inductor", "ferrite", "amplifier")

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
}
# Parameters that may stay empty: C_bias, a ceramic capacitor's capacitance at its DC bias (from the maker's
# DC-bias curve; an X7R 10 µF 100 V 1210 keeps ~30-60 % at 50 V), used instead of C where given
OPTIONAL_PARAMS = {"C_bias", "gbw", "bandwidth", "z_out", "c_in", "r_in", "slew", "cmrr", "aol", "fp2", "pm"}
AMP_Z_OUT = 1.0         # Ω, an amplifier's output impedance when its z_out is not given
AMP_C_IN = 2e-12        # F, its input capacitance when its c_in is not given
AMP_IN = re.compile(r"^(\+IN|IN\+|INP|IN_P|\+|NONINV|VIN\+|\+VIN|IN)$", re.I)     # the input pin's name
AMP_OUT = re.compile(r"^(OUT|VOUT|OUTPUT|VO|O)$", re.I)                              # the output pin's name

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
        if pins and len(pins) != 2:
            raise ValueError("pins names the two pins the model joins, e.g. [IP+, IP-]")
        linked = d.get("from_schematic") or []
        if isinstance(linked, str):
            linked = [p.strip() for p in linked.split(",") if p.strip()]
        return cls(kind, params, str(d.get("note", "") or ""), [str(p) for p in pins],
                   [str(p) for p in linked if str(p) in MODEL_PARAMS[kind]], bias)


def model_pads(fp, model: Optional[ComponentModel]) -> list:
    """The pads a part's model joins: the two named in `pins` (by pad number or pin name), else all of them."""
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
    # no names given: by the schematic pin names, but only when they are unambiguous (one input, one output)
    ins = [p for p in fp.pads if AMP_IN.match((p.pin_function or "").strip()) and p.net]
    outs = [p for p in fp.pads if AMP_OUT.match((p.pin_function or "").strip()) and p.net]
    return (ins[0], outs[0]) if len(ins) == 1 and len(outs) == 1 else None


def model_nets(fp, model: Optional[ComponentModel]) -> set[str]:
    """The nets a part's model joins (see model_pads)."""
    return {p.net for p in model_pads(fp, model) if p.net}
