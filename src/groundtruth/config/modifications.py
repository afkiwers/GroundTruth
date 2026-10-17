"""What-if edits as stored in the settings (`modifications:`): virtual vias, capacitors and copper fills.

Applying them to a board is analysis/whatif.py; this module only defines and parses them.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ..components.values import format_si, parse_value

MOD_TYPES = ("via", "cap", "fill")


class ModificationError(ValueError):
    """A `modifications:` entry in the settings that cannot be read (the message names the entry and the key)."""
    pass


@dataclass
class Modification:
    """One what-if edit: a virtual via, a virtual capacitor (two pads joined by a cap model) or a copper fill
    polygon. Only the fields of its type are used; `enabled` lets the user keep an edit without applying it, and
    `source` tells the user's edits from the fix search's suggestions."""
    type: str                                   # via | cap | fill
    at: Optional[tuple[float, float]] = None    # via / cap position (mm)
    net: str = ""                               # via, fill
    nets: tuple[str, str] = ("", "")            # cap: (pad 1 net, pad 2 net)
    layers: list[str] = field(default_factory=list)   # via span [first, last]; empty = through
    layer: str = ""                             # cap pad layer, fill layer
    size: float = 0.6                           # via pad diameter
    drill: float = 0.3
    angle: float = 0.0                          # cap orientation (degrees, KiCad convention)
    C: float = 100e-9
    ESR: float = 0.02
    ESL: float = 0.4e-9
    polygon: list[tuple[float, float]] = field(default_factory=list)   # fill outline
    enabled: bool = True
    source: str = "manual"                      # manual | suggested
    note: str = ""

    def key(self) -> tuple:
        """Identity for de-duplication: type, nets, layer and geometry (rounded as stored in the settings)."""
        at = (round(self.at[0], 4), round(self.at[1], 4)) if self.at is not None else None
        poly = tuple((round(x, 4), round(y, 4)) for x, y in self.polygon)
        return self.type, at, self.net, tuple(self.nets), self.layer, poly

    def label(self) -> str:
        """A one-line description for the What-if list ("via GND @(12.30, 4.50)")."""
        if self.type == "via":
            return f"via {self.net} @({self.at[0]:.2f}, {self.at[1]:.2f})"
        if self.type == "cap":
            return f"cap {format_si(self.C, 'F')} {self.nets[0]}–{self.nets[1]} @({self.at[0]:.2f}, {self.at[1]:.2f})"
        return f"fill {self.net} on {self.layer} ({len(self.polygon)} points)"

    def to_dict(self) -> dict:
        """The edit as the settings file stores it: only its type's keys, coordinates rounded to 0.1 µm, values with
        units ("100nF"), and the defaults (manual source, no note) left out."""
        d: dict = {"type": self.type}
        if self.type == "via":
            d.update(at=[round(self.at[0], 4), round(self.at[1], 4)], net=self.net, size=self.size, drill=self.drill)
            if self.layers:
                d["layers"] = list(self.layers)
        elif self.type == "cap":
            d.update(at=[round(self.at[0], 4), round(self.at[1], 4)], nets=list(self.nets), layer=self.layer,
                     C=format_si(self.C, "F"), ESR=self.ESR, ESL=format_si(self.ESL, "H"))
            if self.angle:
                d["angle"] = self.angle
        else:
            d.update(layer=self.layer, net=self.net, polygon=[[round(x, 4), round(y, 4)] for x, y in self.polygon])
        d["enabled"] = self.enabled
        if self.source != "manual":
            d["source"] = self.source
        if self.note:
            d["note"] = self.note
        return d

    @classmethod
    def from_dict(cls, d: dict, where: str = "modification") -> "Modification":
        """An edit from its settings entry (the reverse of to_dict), checked: each type's required keys must be
        there, or ModificationError names `where` (e.g. "modifications[3]") and the key."""
        t = str(d.get("type", ""))
        if t not in MOD_TYPES:
            raise ModificationError(f"{where}: type must be one of {', '.join(MOD_TYPES)}")

        def num(key, default):
            """A number from the entry: as written, or from text with units ("100nF", "20mohm")."""
            v = d.get(key, default)
            if isinstance(v, (int, float)):
                return float(v)
            x = parse_value(str(v).rstrip("FHΩ").replace("ohm", ""))
            if x is None:
                raise ModificationError(f"{where}.{key}: cannot read {v!r}")
            return x

        m = cls(t, enabled=bool(d.get("enabled", True)), source=str(d.get("source", "manual")),
                note=str(d.get("note", "") or ""))
        if t in ("via", "cap"):
            at = d.get("at")
            if not isinstance(at, (list, tuple)) or len(at) != 2:
                raise ModificationError(f"{where}.at: needs [x, y] in mm")
            m.at = (float(at[0]), float(at[1]))
        if t == "via":
            m.net = str(d.get("net", ""))
            m.layers = [str(x) for x in d.get("layers") or []]
            m.size, m.drill = num("size", 0.6), num("drill", 0.3)
            if not m.net:
                raise ModificationError(f"{where}: a via needs a net")
        elif t == "cap":
            nets = d.get("nets") or []
            if len(nets) != 2:
                raise ModificationError(f"{where}.nets: a capacitor needs two nets")
            m.nets = (str(nets[0]), str(nets[1]))
            m.layer = str(d.get("layer", "F.Cu"))
            m.C, m.ESR, m.ESL = num("C", 100e-9), num("ESR", 0.02), num("ESL", 0.4e-9)
            m.angle = float(d.get("angle", 0.0))
        else:
            m.layer, m.net = str(d.get("layer", "")), str(d.get("net", ""))
            pts = d.get("polygon") or []
            if len(pts) < 3 or not m.layer or not m.net:
                raise ModificationError(f"{where}: a fill needs layer, net and a polygon of ≥ 3 points")
            m.polygon = [(float(p[0]), float(p[1])) for p in pts]
        return m
