"""Through-hole pins: how a part's pins conduct through the board and join it, and what a connector adds in series.

The user, of a big connector's map: "do we assume a solid connection between the connector and the via; e.g. filled
with solder? ... introduce a GT field parameter that can be assigned to connectors to define the solder joint
resistance", then: "implement all of them but allow default values for each when one or more are not entered".

Six values per part, each one falling back on its own (`params_for`), from the most specific to the most general:

    1. the part's GT field             GT = contact:0.5m, fill:75      (with its labels: GT = conv_in, contact:0.5m)
    2. the part's entry in the settings components: {J1: {..., tht: {contact: 0.5m}}}   (the Parts tab's Pins column)
    3. the board's defaults             tht: {contact: 0.5m, pin: brass}                (the Parts tab's Pin defaults…)
    4. built in                         the model as before: copper filling the hole, a perfect joint, no contact

    pin      the pin's conductor through the board: `hole` (built in: copper filling the whole drilled hole, as a
             perfectly soldered copper pin would) or a material: copper, brass, bronze (phosphor bronze), becu
             (beryllium copper), tellurium (tellurium copper). With a material the pin is a rod of pin_d, the gap to
             the hole's wall is solder, and the plating conducts alongside.
    pin_d    the pin's diameter, mm (default: the hole less PIN_CLEARANCE_MM, the usual lead-to-hole clearance)
    pin_r    the pin's own resistance through the board, Ω (instead of a material's)
    fill     how far up the hole the solder goes, % of the board's thickness from the soldering side (the side
             opposite the part: wave or selective soldering). Built in 100. IPC-A-610 accepts 75 [IPC-A-610]. Above the
             solder only the hole's plating joins the layers to the pin; the current from the part enters the board at
             the top of the solder (the layer nearest to it inside the filled height).
    joint    the solder joint's resistance per pin, Ω, where the pin's current enters the board copper
    contact  the mating contact's resistance per pin, Ω (a connector's datasheet value)
    bond     any lumped part (SMD or not): the solder between its pads and its metal, µm of SAC305 (built in
             simulate.SOLDER_UM, 75; 0 for a perfect bond). Its resistance per area joins every grid node under the
             pads to the part's side (simulate.part_lid, [A65]). Added 2026-10-24 (the user: "Add now").
    entry    a power-stage switch (or diode, integrated stage): how its current enters the copper. "tab": each side's
             metal (a silicon FET's drain tab, a diode's cathode) soldered over its pads like a lumped part's, the
             current handing over where the copper brings it; "even": spread evenly over each pin's copper [A51] (a
             chip-scale GaN FET, whose die spreads it over its bars). Built in: "tab" for a side with a pad at least
             simulate.TAB_MM wide, else "even" (simulate.switch_lids). Added 2026-10-25 (the user: "I won't always
             use GANs").

A plated hole's inner layers are always joined to its plating (made with the board): solder only joins the pin to
the plating. So a partly filled hole does not cut a layer off: it takes the pin's thick conductor out of part of the
board's thickness.

`joint` and `contact` are in series where a part's current is fed into the board (a DC load's source and load pins,
a power stage's connectors): the model feeds a current, so on a single pin they do not change how it spreads in the
copper; they add I·R to the drop and I²·R of heat at the part (thermal.part_losses). No value is guessed for a
connector's contact: a connector carrying NOTE_A or more without one gets a note instead.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Callable, Optional

import numpy as np

from ..solver.kernel import filament_mutual
from ..solver.peec import SIGMA_CU, VIA_PLATING

KEYS = ("pin", "pin_d", "pin_r", "fill", "joint", "contact", "bond", "entry")
# resistivity at 20 °C, Ω·m: copper 100 % IACS [IEC 60028]; brass C26000 ~28 %; phosphor bronze C51000 ~15 %;
# beryllium copper C17200 ~22 %; tellurium copper C14500 ~93 % (the alloys' conductivities [CDA])
MATERIALS = {"copper": 1.72e-8, "brass": 6.2e-8, "bronze": 1.15e-7, "becu": 7.8e-8, "tellurium": 1.85e-8}
ALIASES = {"cu": "copper", "phosphor-bronze": "bronze", "phosphorbronze": "bronze", "pb": "bronze",
           "beryllium": "becu", "cube": "becu", "tecu": "tellurium"}
SOLDER_RHO = 1.3e-7             # SAC305 (~13 % IACS), Ω·m: the solder between the pin and the hole's wall [A18]
PIN_CLEARANCE_MM = 0.25         # a lead's diameter is the hole's less this (a usual lead-to-hole clearance [A18])
NOTE_A = 3.0                    # a connector pin carrying this much without a contact value gets a note
SOURCES = ("GT field", "part setting", "board default", "built in")
WORD = re.compile(r"^(pin_d|pin_r|pin|fill|joint|contact|bond|entry)\s*[:=]\s*(.+)$", re.I)
ENTRIES = ("tab", "even")        # a power-stage switch's pads: soldered to one metal per side, or fed evenly [A65]


def _material(text: str) -> Optional[str]:
    """A pin material's name ("hole" or a MATERIALS key) from what was written; None when unknown."""
    t = text.strip().lower().replace(" ", "").replace("_", "-")
    if t in ("hole", "fill", "filled", "default"):
        return "hole"
    t = ALIASES.get(t, t)
    return t if t in MATERIALS else None


def parse_value(key: str, text) -> tuple[Optional[object], str]:
    """One value: (the value in SI units, a material name or a fraction for fill; or None, why not)."""
    from ..components.values import parse_quantity
    s = str(text).strip()
    if key == "pin":
        m = _material(s)
        return (m, "") if m else (None, f"pin: {s!r} is not a pin material (hole, copper, brass, bronze, becu, "
                                        "tellurium)")
    if key == "fill":
        try:
            pct = float(s.rstrip("%").strip())
        except ValueError:
            return None, f"fill: {s!r} is not a percentage (e.g. 75)"
        if not 0 < pct <= 100:
            return None, f"fill: {s} is not between 0 and 100 %"
        return pct / 100.0, ""
    if key == "pin_d":
        try:
            d = float(re.sub(r"(?i)\s*mm$", "", s))
        except ValueError:
            return None, f"pin_d: {s!r} is not a diameter in mm (e.g. 1.0)"
        return (d, "") if d > 0 else (None, f"pin_d: {s} must be above 0")
    if key == "entry":
        e = s.lower()
        return (e, "") if e in ENTRIES else (None, f"entry: {s!r} is not tab or even")
    if key == "bond":
        try:
            t = float(re.sub(r"(?i)\s*(µm|um)$", "", s))
        except ValueError:
            return None, f"bond: {s!r} is not a solder thickness in µm (e.g. 50; 0 for a perfect bond)"
        return (t, "") if 0 <= t <= 1000 else (None, f"bond: {s} µm is not between 0 and 1000")
    value, why = parse_quantity(s, "ohm")              # joint, contact, pin_r: Ω with SI prefixes ("0.5m", "500µΩ")
    if value is None:
        return None, f"{key}: {s!r} is not a resistance (e.g. 0.5m for 0.5 mΩ)" + (f": {why}" if why else "")
    if value < 0:
        return None, f"{key}: {s} is negative"
    return value, ""


def parse_words(text: str) -> tuple[dict, list[str]]:
    """The pin values in a GT field's (or a settings line's) text, e.g. "conv_in, contact:0.5m, fill:75": ({key:
    value}, problems). Words that are not pin values (the field's labels) are left alone."""
    values, problems = {}, []
    for raw in re.split(r"[,;]", text or ""):
        m = WORD.match(raw.strip())
        if not m:
            continue
        key = m.group(1).lower()
        value, why = parse_value(key, m.group(2))
        if value is None:
            problems.append(why)
        else:
            values[key] = value
    return values, problems


def is_pin_word(word: str) -> bool:
    """Whether a GT field's word is a pin value (contact:0.5m), not a label."""
    return bool(WORD.match(word.strip()))


def values_from_data(data, where: str) -> tuple[dict, list[str]]:
    """The settings' `tht:` entry (a mapping, or the GT field's text) as {key: value}, and problems naming `where`."""
    if data is None or data == "":
        return {}, []
    if isinstance(data, str):
        values, problems = parse_words(data)
        bad = [w.strip() for w in re.split(r"[,;]", data) if w.strip() and not is_pin_word(w)]
        problems += [f"{w!r} is not a pin value ({', '.join(KEYS)})" for w in bad]
        return values, [f"{where}: {p}" for p in problems]
    if not isinstance(data, dict):
        return {}, [f"{where}: must be a mapping, e.g. {{contact: 0.5m, fill: 75}}"]
    values, problems = {}, []
    for key, text in data.items():
        k = str(key).lower()
        if k not in KEYS:
            problems.append(f"{where}: {key!r} is not a pin value ({', '.join(KEYS)})")
            continue
        value, why = parse_value(k, text)
        if value is None:
            problems.append(f"{where}: {why}")
        else:
            values[k] = value
    return values, problems


def format_value(key: str, value) -> str:
    """A value as the settings and the Parts tab write it (read back by parse_value)."""
    from ..components.values import format_si
    if key in ("pin", "entry"):
        return str(value)
    if key == "fill":
        return f"{value * 100:g}"
    if key in ("pin_d", "bond"):
        return f"{value:g}"
    return format_si(value, "Ω")


def format_values(values: dict) -> str:
    """{key: value} as one line, in KEYS order: "contact:500µΩ, fill:75"."""
    return ", ".join(f"{k}:{format_value(k, values[k])}" for k in KEYS if k in values)


def to_data(values: dict) -> dict:
    """{key: value} as the settings file's mapping (text values, KEYS order)."""
    return {k: format_value(k, values[k]) for k in KEYS if k in values}


@dataclass(frozen=True)
class PinParams:
    """A part's through-hole pin values, each with where it came from (SOURCES)."""
    pin: str = "hole"
    pin_d: Optional[float] = None       # mm
    pin_r: Optional[float] = None       # Ω through the board
    fill: float = 1.0                   # the soldered share of the board's thickness
    joint: float = 0.0                  # Ω per pin
    contact: float = 0.0                # Ω per pin
    bond: Optional[float] = None        # µm of solder between any lumped part's pads and its metal (None: built in)
    entry: Optional[str] = None         # a stage switch's pads: "tab" (one soldered metal per side), "even" (spread
                                        # evenly over its pins, a chip-scale die), None: by the pads' size (TAB_MM)
    source: dict = field(default_factory=dict, compare=False, hash=False)   # key -> one of SOURCES

    @property
    def series(self) -> float:
        """The resistance in series where the part's current enters the board, per pin: joint + contact."""
        return self.joint + self.contact

    @property
    def plain(self) -> bool:
        """The built-in column (copper filling the whole hole): the model as before these values existed."""
        return self.pin == "hole" and self.pin_r is None and self.fill >= 1.0

    def given(self, key: str) -> bool:
        """Whether a value was set anywhere (not built in)."""
        return self.source.get(key, SOURCES[-1]) != SOURCES[-1]

    def describe(self) -> str:
        """The values with their sources, for notes and tooltips: "contact 0.5 mΩ (GT field), fill 75 % (board
        default)"."""
        out = []
        for k in KEYS:
            v = getattr(self, k)
            if v is None or not self.given(k):
                continue
            text = {"fill": f"{v * 100:g} %", "pin_d": f"{v:g} mm", "pin": str(v),
                    "bond": f"{v:g} µm of solder", "entry": f"{v} pads"}.get(k, f"{v * 1e3:g} mΩ")
            out.append(f"{k} {text} ({self.source[k]})")
        return ", ".join(out) or "built in (copper filling the hole, a perfect joint, no contact resistance)"


def gt_values(fp) -> tuple[dict, list[str]]:
    """The pin values in a footprint's GT field, and problems with them (named after the part)."""
    from .tags import tag_text
    values, problems = parse_words(tag_text(fp))
    return values, [f"{fp.reference}: GT {p}" for p in problems]


def params_for(board, config, ref: str) -> PinParams:
    """A part's pin values: each from its GT field, else its settings entry, else the board's defaults, else built
    in (see the module)."""
    fp = board.footprint(ref) if board is not None else None
    layers = [(gt_values(fp)[0] if fp is not None else {}, SOURCES[0])]
    entry = config.components.get(ref) if config is not None else None
    layers.append((dict(getattr(entry, "tht", None) or {}), SOURCES[1]))
    layers.append((dict(getattr(config, "tht", None) or {}), SOURCES[2]))
    values, source = {}, {}
    for key in KEYS:
        for vals, where in layers:
            if key in vals:
                values[key], source[key] = vals[key], where
                break
    return PinParams(**values, source=source)


def params_cache(board, config) -> Callable[[str], PinParams]:
    """params_for, remembered per reference (a model build asks once per pad)."""
    memo: dict[str, PinParams] = {}

    def get(ref: str) -> PinParams:
        """The part's pin values (cached)."""
        if ref not in memo:
            memo[ref] = params_for(board, config, ref)
        return memo[ref]
    return get


def column_conductance(params: PinParams, drill_mm: float, thickness_m: float) -> tuple[float, float]:
    """(soldered, unsoldered) conductance per metre of a pin's column, S·m: σ·A summed over its conductors.

    Built in ("hole"): copper filling the whole hole (σ_Cu·π·r², as before), unsoldered the plating alone. A
    material: the pin rod (pin_d, or the hole less PIN_CLEARANCE_MM) + the solder in the gap + the plating ring
    (2·π·r·t); pin_r replaces the rod's σ·A by thickness / pin_r. Above the solder only the plating conducts: the pin is
    not joined to the hole there."""
    r = max(drill_mm / 2 * 1e-3, 5e-5)
    plating = SIGMA_CU * 2 * math.pi * r * VIA_PLATING
    if params.pin == "hole" and params.pin_r is None:
        return SIGMA_CU * math.pi * r * r, plating
    d = params.pin_d if params.pin_d is not None else max(drill_mm - PIN_CLEARANCE_MM, 0.6 * drill_mm)
    a_pin = math.pi * (min(d, drill_mm) / 2 * 1e-3) ** 2
    a_gap = max(math.pi * r * r - a_pin, 0.0)
    if params.pin_r is not None:
        rod = thickness_m / max(params.pin_r, 1e-12)
    else:
        rod = a_pin / MATERIALS.get(params.pin if params.pin != "hole" else "copper", MATERIALS["copper"])
    return rod + a_gap / SOLDER_RHO + plating, plating


def segment_impedance(length_m: float, soldered_m: float, drill_mm: float, g_solder: float, g_open: float
                      ) -> Callable[[float], complex]:
    """A pin column's piece between two layers: `soldered_m` of its `length_m` soldered (g_solder S·m), the rest
    the plating alone (g_open): the two in series, R = l_s/g_s + l_o/g_o, + jωL of a filament at the hole's radius
    (as peec.via_impedance)."""
    length = max(length_m, 1e-6)
    soldered = min(max(soldered_m, 0.0), length)
    R = soldered / g_solder + (length - soldered) / g_open
    L = float(filament_mutual(length, np.array(0.0), np.array(max(drill_mm / 2 * 1e-3, 5e-5))))
    return lambda f, R=R, L=L: complex(R, 2 * math.pi * f * L)


def solder_span(params: PinParams, part_side: str, z: dict) -> tuple[float, float]:
    """(z_low, z_high), metres: the soldered height of the hole, from the soldering side (opposite the part) up
    to `fill` of the board's thickness between its outer copper layers."""
    z_top, z_bot = z.get("F.Cu", max(z.values())), z.get("B.Cu", min(z.values()))
    t = z_top - z_bot
    if part_side == "B.Cu":                         # a bottom-side part: soldered from the top down
        return z_top - params.fill * t, z_top
    return z_bot, z_bot + params.fill * t


def feed_layer(params: PinParams, part_side: str, layers: list[str], z: dict) -> Optional[str]:
    """Where a pin's current enters the board when the solder does not reach the part's side: the layer of `layers`
    nearest to the part inside the soldered height. None when the part's own side is soldered (fill 100 %)."""
    if params.fill >= 1.0:
        return None
    lo, hi = solder_span(params, part_side, z)
    inside = [l for l in layers if l in z and lo - 1e-9 <= z[l] <= hi + 1e-9]
    if not inside:
        return None
    return max(inside, key=lambda l: z[l]) if part_side != "B.Cu" else min(inside, key=lambda l: z[l])


def feed_layers(board, config, pins: list, z: dict) -> set[str]:
    """The layers where these fed pins' (REF.PAD) currents enter the board when their solder does not reach their
    part's side (feed_layer): a whole-board model must have them."""
    get = params_cache(board, config)
    out = set()
    for name in pins:
        pad = board.find_pin(name) if name else None
        fp = board.footprint(pad.footprint) if pad is not None else None
        if fp is None or fp.dnp or pad.pad_type != "thru_hole":
            continue
        layer = feed_layer(get(fp.reference), "B.Cu" if fp.layer == "B.Cu" else "F.Cu", list(pad.layers), z)
        if layer:
            out.add(layer)
    return out


def series_note(ref: str, pin: str, amps: float, params: PinParams) -> Optional[str]:
    """A note when a connector pin carries NOTE_A or more and nobody gave its contact resistance."""
    from .dcload import CONNECTOR_PREFIXES
    r = ref.upper()
    connector = r.startswith(CONNECTOR_PREFIXES) and r.lstrip("ABCDEFGHIJKLMNOPQRSTUVWXYZ")[:1].isdigit()
    if not connector or abs(amps) < NOTE_A or params.given("contact"):
        return None
    return (f"{pin} carries {abs(amps):.3g} A without a contact resistance (GT contact:0.5m, the Parts tab's Pins, or "
            "Pin defaults): the connector's own drop and heat are left out")
