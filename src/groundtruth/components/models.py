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
    # z_out (Ω), c_in (F), r_in (Ω), slew (V/s), cmrr (dB, kept and shown: a single-ended path has no common mode)
    "amplifier": ("gain", "gbw", "bandwidth", "z_out", "c_in", "r_in", "slew", "cmrr"),
}
# Parameters that may stay empty: C_bias, a ceramic capacitor's capacitance at its DC bias (from the maker's
# DC-bias curve; an X7R 10 µF 100 V 1210 keeps ~30-60 % at 50 V), used instead of C where given
OPTIONAL_PARAMS = {"C_bias", "gbw", "bandwidth", "z_out", "c_in", "r_in", "slew", "cmrr"}
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
        """A capacitor's capacitance in the circuit: at its DC bias where given, else the nominal C."""
        c = self.params.get("C_bias")
        return float(c) if c is not None and c > 0 else self.get("C")

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
        """An amplifier's output impedance (ohm): z_out, else AMP_Z_OUT (a closed-loop op-amp output is low)."""
        return float(self.params.get("z_out") or AMP_Z_OUT)

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
            if p in self.params:
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
        params = {}
        for p in MODEL_PARAMS[kind]:
            if d.get(p) is not None:
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
                   [str(p) for p in linked if str(p) in MODEL_PARAMS[kind]])


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
