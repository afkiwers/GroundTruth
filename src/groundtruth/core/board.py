"""KiCad-independent board model.

Both adapters (the .kicad_pcb file parser and the live IPC/kipy reader) produce these
dataclasses, so rule checks and the solver never touch KiCad APIs directly.

Units: millimetres for all geometry, as in the KiCad file format.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

Point = tuple[float, float]
# A polygon is (outline, [holes]); each ring is a list of points.
Polygon = tuple[list[Point], list[list[Point]]]


@dataclass
class CopperLayer:
    """One copper layer of the stackup: its KiCad name, its position from the top and its foil thickness."""
    name: str            # "F.Cu", "In1.Cu", ..., "B.Cu"
    index: int           # 0 = top, increasing towards bottom
    z: float = 0.0       # mm, centre of the copper foil measured downwards from the top surface
    thickness: float = 0.035


@dataclass
class DielectricLayer:
    """The insulation between two consecutive copper layers (core or prepreg): its thickness and the material
    constants the capacitance and loss models use."""
    name: str
    thickness: float     # mm
    epsilon_r: float = 4.5
    loss_tangent: float = 0.02


@dataclass
class Stackup:
    """The board's layer stack, top to bottom. `dielectrics[i]` lies between `copper[i]` and `copper[i + 1]`, so
    the dielectrics between two copper layers are the slice between their indexes."""
    copper: list[CopperLayer]
    dielectrics: list[DielectricLayer] = field(default_factory=list)  # between consecutive copper layers
    board_thickness: float = 1.6
    # the solder masks (F.Mask over the top copper, B.Mask under the bottom), from the stackup; None when the board
    # file has no stackup (the cross-sections then take KiCad's default, 0.01 mm of εr 3.3)
    mask_top: Optional[DielectricLayer] = None
    mask_bottom: Optional[DielectricLayer] = None

    def layer(self, name: str) -> Optional[CopperLayer]:
        """The copper layer called `name`, or None when the board has no such layer."""
        for layer in self.copper:
            if layer.name == name:
                return layer
        return None

    def names(self) -> list[str]:
        """The copper layers' names, top to bottom."""
        return [layer.name for layer in self.copper]

    def distance(self, a: str, b: str) -> float:
        """Centre-to-centre distance between two copper layers' foils (mm)."""
        la, lb = self.layer(a), self.layer(b)
        if la is None or lb is None:
            raise KeyError(f"unknown copper layer {a!r} or {b!r}")
        return abs(la.z - lb.z)

    def epsilon_between(self, a: str, b: str) -> float:
        """Thickness-weighted epsilon_r of the dielectrics between two copper layers."""
        la, lb = self.layer(a), self.layer(b)
        lo, hi = sorted((la.index, lb.index))
        layers = self.dielectrics[lo:hi]
        total = sum(d.thickness for d in layers)
        if total <= 0:
            return 4.5              # no stackup data: FR-4's usual value
        # dielectrics in series between two plates add as 1/eps_i weighted by thickness; the plain thickness-
        # weighted mean used here is the simpler approximation, close for the similar materials of one board
        return sum(d.epsilon_r * d.thickness for d in layers) / total

    def epsilon_series(self, a: str, b: str) -> float:
        """εr of the dielectrics between two copper layers as one parallel-plate capacitor: the field crosses them in
        series (D the same in each), so 1/εr = Σ (t_i / t) / εr_i [Pozar 2012]. For the plane pairs' and the plates'
        capacitance; the thickness-weighted mean (epsilon_between) reads it high by about the spread of the εr_i
        squared over their mean (0.1 % for FR-4's prepreg and core, 1.4 % for a 3.48 / 4.4 hybrid)."""
        la, lb = self.layer(a), self.layer(b)
        lo, hi = sorted((la.index, lb.index))
        layers = self.dielectrics[lo:hi]
        total = sum(d.thickness for d in layers)
        if total <= 0:
            return 4.5              # no stackup data: FR-4's usual value
        return total / sum(d.thickness / d.epsilon_r for d in layers)

    def loss_tangent_series(self, a: str, b: str) -> float:
        """tan δ of the dielectrics between two copper layers as one parallel-plate capacitor: each one's weighted by
        its share of the field's energy, t_i / εr_i (the series capacitors' loss to first order in tan δ
        [Pozar 2012])."""
        la, lb = self.layer(a), self.layer(b)
        lo, hi = sorted((la.index, lb.index))
        layers = self.dielectrics[lo:hi]
        weight = sum(d.thickness / d.epsilon_r for d in layers)
        if weight <= 0:
            return 0.02             # FR-4's usual value
        return sum(d.loss_tangent * d.thickness / d.epsilon_r for d in layers) / weight

    def loss_tangent_between(self, a: str, b: str) -> float:
        """Thickness-weighted dielectric loss tangent between two copper layers."""
        la, lb = self.layer(a), self.layer(b)
        lo, hi = sorted((la.index, lb.index))
        layers = self.dielectrics[lo:hi]
        total = sum(d.thickness for d in layers)
        if total <= 0:
            return 0.02             # FR-4's usual value
        return sum(d.loss_tangent * d.thickness for d in layers) / total

    def dielectric_between(self, a: str, b: str) -> float:
        """Insulation thickness between the facing surfaces of two copper layers (mm)."""
        la, lb = self.layer(a), self.layer(b)
        # z is each foil's centre: subtract half of each foil to get the gap between the facing surfaces; at least
        # 1 µm so a capacitance per area (eps / d) never divides by zero on an odd stackup
        return max(abs(la.z - lb.z) - (la.thickness + lb.thickness) / 2, 1e-3)

    def neighbours(self, name: str) -> list[str]:
        """Adjacent copper layers (above and below) in stackup order."""
        layer = self.layer(name)
        out = []
        if layer is None:
            return out
        if layer.index > 0:
            out.append(self.copper[layer.index - 1].name)
        if layer.index < len(self.copper) - 1:
            out.append(self.copper[layer.index + 1].name)
        return out


@dataclass
class Track:
    """A copper track segment on one layer: straight from `start` to `end`, or an arc through `mid`."""
    start: Point
    end: Point
    width: float
    layer: str
    net: str
    uuid: str = ""
    line: int = 0        # line in the source file (0 = unknown)
    mid: Optional[Point] = None  # set for arc tracks

    @property
    def is_arc(self) -> bool:
        """True for an arc track (KiCad stores arcs by start, mid and end point)."""
        return self.mid is not None


@dataclass
class Via:
    """A plated hole joining copper layers. `layers` lists every copper layer it spans (all of them for a through
    via, a range for blind and buried ones)."""
    position: Point
    size: float
    drill: float
    layers: list[str]    # every copper layer the via spans
    net: str
    via_type: str = "through"  # through | blind | buried | micro
    uuid: str = ""
    line: int = 0


@dataclass
class Pad:
    """One pad of a footprint, in board coordinates (already rotated and moved with its footprint), with the
    schematic pin's electrical type and name when the board carries them (used to guess drivers and receivers)."""
    footprint: str       # reference designator of the parent
    number: str
    position: Point      # absolute
    angle: float         # absolute orientation in degrees
    size: Point
    shape: str           # rect | roundrect | circle | oval | trapezoid | custom | chamfered_rect
    layers: list[str]    # copper layers only
    net: str
    pad_type: str = "smd"  # smd | thru_hole | np_thru_hole | connect
    drill: float = 0.0
    polygon: Optional[Polygon] = None  # absolute outline when known (custom pads, kipy)
    roundrect_ratio: float = 0.0
    uuid: str = ""
    pin_type: str = ""       # schematic electrical type: input | output | bidirectional | tri_state | passive | ...
    pin_function: str = ""   # schematic pin name, e.g. "PA5"

    @property
    def pin(self) -> str:
        """The pin's name as the settings write it: "REF.NUMBER" (e.g. "U1.12")."""
        return f"{self.footprint}.{self.number}"


@dataclass
class Footprint:
    """A placed part: its reference, value and library footprint, its pads, the schematic symbol's own fields
    (MPN, GT labels, model parameters ...), the schematic sheet it sits in, and its outline for the maps."""
    reference: str
    value: str
    lib_id: str          # "Resistor_SMD:R_0402_1005Metric"
    position: Point
    angle: float
    layer: str
    pads: list[Pad] = field(default_factory=list)
    dnp: bool = False
    exclude_from_sim: bool = False
    uuid: str = ""
    line: int = 0
    fields: dict[str, str] = field(default_factory=dict)   # the symbol's own fields (MPN, GT ...): not Reference/Value
    sheet: str = ""             # the schematic sheet copy it sits in: a key, one per instance of a reused sheet
    sheet_name: str = ""        # that sheet's name as shown ("Phase B"), "" for the root sheet or when unknown
    sheet_file: str = ""        # its file ("phase.kicad_sch"): copies of a reused sheet share it
    outline: list = field(default_factory=list)   # polylines (board mm) of its courtyard, else its fab outline

    @property
    def footprint_name(self) -> str:
        """The library footprint's name without its library ("R_0402_1005Metric")."""
        return self.lib_id.split(":", 1)[-1]

    def pad(self, number: str) -> Optional[Pad]:
        """The pad with this number ("1", "A3" ...), or None. With several pads of one number, the first."""
        for pad in self.pads:
            if pad.number == number:
                return pad
        return None

    def nets(self) -> set[str]:
        """The nets its pads connect to (unconnected pads, net "", left out)."""
        return {p.net for p in self.pads if p.net}


@dataclass
class Zone:
    """A copper zone (pour or plane) of one net, possibly on several layers. `filled` is what KiCad's zone fill
    produced, per layer; an unfilled zone has the outline only and carries no current in the models."""
    net: str
    layers: list[str]
    filled: dict[str, list[Polygon]] = field(default_factory=dict)  # layer -> filled polygons
    outline: Optional[Polygon] = None
    is_rule_area: bool = False
    name: str = ""
    uuid: str = ""
    line: int = 0

    @property
    def is_filled(self) -> bool:
        """True when the zone has fill polygons on any layer."""
        return any(self.filled.values())


@dataclass
class Board:
    """The whole board: stackup, nets, copper (tracks, vias, pads through the footprints, zones), the board
    outline and the notes the reader left (`warnings`). Lookups by reference and by net are indexed lazily."""
    source: str                          # file path or "kicad-ipc:<name>"
    stackup: Stackup
    nets: list[str] = field(default_factory=list)
    tracks: list[Track] = field(default_factory=list)
    vias: list[Via] = field(default_factory=list)
    footprints: list[Footprint] = field(default_factory=list)
    zones: list[Zone] = field(default_factory=list)
    outline: list[Polygon] = field(default_factory=list)   # Edge.Cuts, polygonised
    file_version: str = ""
    generator_version: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def copper_layers(self) -> list[str]:
        """The copper layers' names, top to bottom."""
        return self.stackup.names()

    def _indexes(self) -> tuple[dict, dict]:
        """(reference -> footprint, net -> footprints with a pad on it), built once per footprint list: a copy of the
        board with its own list (a what-if board, `copy.copy` plus a new list) gets its own."""
        # The cache is keyed by the footprint list's identity and length rather than invalidated explicitly: code
        # that edits a board replaces or extends the list, which changes one of the two. It lives in __dict__ (not
        # a dataclass field) so it never takes part in comparisons, repr or pickling of the board's fields.
        key = (id(self.footprints), len(self.footprints))
        cached = self.__dict__.get("_index_cache")
        if cached is None or cached[0] != key:
            by_net: dict[str, list[Footprint]] = {}
            for fp in self.footprints:
                for n in fp.nets():
                    by_net.setdefault(n, []).append(fp)
            by_ref = {}
            for fp in self.footprints:
                by_ref.setdefault(fp.reference, fp)        # the first of a duplicated reference, as before
            cached = self.__dict__["_index_cache"] = (key, (by_ref, by_net))
        return cached[1]

    def footprint(self, reference: str) -> Optional[Footprint]:
        """The footprint with this reference ("U1"), or None."""
        return self._indexes()[0].get(reference)

    def parts_by_net(self) -> dict[str, list[Footprint]]:
        """net -> the footprints with a pad on it (shared: do not change the lists)."""
        return self._indexes()[1]

    def pads(self) -> list[Pad]:
        """Every pad of every footprint."""
        return [pad for fp in self.footprints for pad in fp.pads]

    def find_pin(self, pin: str) -> Optional[Pad]:
        """Look up a pad by "REF.NUMBER" (e.g. "U1.12")."""
        if "." not in pin:
            return None
        # the pad number is what follows the last dot: a pad number with a dot of its own cannot be named this way
        ref, number = pin.rsplit(".", 1)
        fp = self.footprint(ref)
        return fp.pad(number) if fp else None

    def fingerprint(self) -> str:
        """A digest of everything the analyses read from the board: the stackup, the nets, every track, via, pad and
        zone fill with its geometry, the parts with their fields, the outline (coordinates to 1 nm). Two reads of an
        unchanged board give the same digest whatever the order the reader lists the items in; a track's width
        changed by a micrometre gives another. Not in it: where the board came from, the file's line numbers, the
        reader's warnings. The plugin compares it before a Simulate with the board's when it was read, so an edit in
        KiCad since is read again before it is analysed (VALIDATION_AUDIT.md round 5, ISSUE-19)."""
        import hashlib

        import numpy as np

        def r(v) -> float:
            """A coordinate or size to 1 nm (mm)."""
            return round(float(v), 6)

        def poly(p) -> Optional[str]:
            """A polygon (outline, holes) as the digest of its points in nm (numpy: a zone fill has tens of thousands
            of points, and this runs beside the window's thread)."""
            if not p:
                return None
            d = hashlib.sha256()
            for points in (p[0], *p[1]):
                d.update(np.rint(np.asarray(points, dtype=float).reshape(-1, 2) * 1e6).astype(np.int64).tobytes())
                d.update(b"|")
            return d.hexdigest()

        def point(p) -> Optional[tuple]:
            """A point, rounded."""
            return (r(p[0]), r(p[1])) if p is not None else None

        st = self.stackup
        parts = [repr(("stackup", [(c.name, c.index, r(c.z), r(c.thickness)) for c in st.copper],
                       [(d.name, r(d.thickness), r(d.epsilon_r), r(d.loss_tangent)) for d in st.dielectrics],
                       r(st.board_thickness),
                       [(m.name, r(m.thickness), r(m.epsilon_r)) if m else None for m in (st.mask_top, st.mask_bottom)])),
                 repr(("nets", sorted(self.nets)))]
        items = [repr(("t", point(t.start), point(t.end), r(t.width), t.layer, t.net, point(t.mid))) for t in self.tracks]
        items += [repr(("v", point(v.position), r(v.size), r(v.drill), list(v.layers), v.net, v.via_type))
                  for v in self.vias]
        for fp in self.footprints:
            pads = sorted(repr((p.number, point(p.position), r(p.angle), point(p.size), p.shape, list(p.layers), p.net,
                                p.pad_type, r(p.drill), poly(p.polygon), r(p.roundrect_ratio), p.pin_type,
                                p.pin_function)) for p in fp.pads)
            items.append(repr(("f", fp.reference, fp.value, fp.lib_id, point(fp.position), r(fp.angle), fp.layer, fp.dnp,
                               fp.exclude_from_sim, sorted(fp.fields.items()), fp.sheet, pads)))
        items += [repr(("z", z.net, list(z.layers), z.is_rule_area, z.name,
                        sorted((layer, [poly(p) for p in polys]) for layer, polys in z.filled.items()), poly(z.outline)))
                  for z in self.zones]
        items += [repr(("o", poly(o))) for o in self.outline]
        h = hashlib.sha256()
        for s in parts + sorted(items):
            h.update(s.encode("utf-8"))
            h.update(b"\n")
        return h.hexdigest()

    def unfilled_zones(self) -> list[Zone]:
        """Copper zones of a net that have never been filled (the model would miss their copper: the checks and
        the plugin ask the user to fill them, or refill them with kicad-cli)."""
        return [z for z in self.zones if not z.is_rule_area and z.net and not z.is_filled]
