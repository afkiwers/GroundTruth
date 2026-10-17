"""Read a ``.kicad_pcb`` file into a geometry model suitable for simulation.

All coordinates are in millimetres in KiCad's frame (x right, y down).
Geometry is held as shapely polygons.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import shapely
from shapely.geometry import LineString, MultiPolygon, Point, Polygon, box
from shapely.ops import polygonize, unary_union
from shapely import affinity

from . import sexpr as S
from .geometry_utils import arc_points, circle_points, rotate

DEFAULT_CU_MM = 0.035
DEFAULT_BOARD_MM = 1.6


# ---- model ------------------------------------------------------------------

@dataclass
class StackLayer:
    """One layer of the stackup, top to bottom."""
    name: str
    kind: str  # "copper" | "dielectric"
    thickness: float  # mm
    material: str = ""
    epsilon_r: float | None = None


@dataclass
class Pad:
    """A pad in board coordinates: its copper shape (hole removed), layers, drill and solder-mask openings."""
    ref: str
    number: str
    net: str
    kind: str  # smd | thru_hole | np_thru_hole | connect
    x: float
    y: float
    layers: list[str]  # copper layer names
    shape: Polygon | MultiPolygon  # copper shape (holes removed)
    drill: float = 0.0  # equivalent round drill diameter (mm), 0 if none
    drill_shape: Polygon | None = None
    mask_top: bool = False
    mask_bottom: bool = False

    @property
    def key(self) -> str:
        """"REF.NUMBER", how the setup names pads."""
        return f"{self.ref}.{self.number}"

    @property
    def plated(self) -> bool:
        """A plated through hole (its barrel conducts)."""
        return self.kind == "thru_hole" and self.drill > 0


@dataclass
class Track:
    """A track as a polyline (an arc already tessellated) of one width."""
    layer: str
    net: str
    width: float
    points: list[tuple[float, float]]


@dataclass
class Via:
    """A via between two copper layers."""
    x: float
    y: float
    size: float
    drill: float
    top: str
    bottom: str
    net: str
    filled: bool = False


@dataclass
class CopperShape:
    """Zone fill or copper graphic."""
    layer: str
    net: str
    shape: Polygon | MultiPolygon
    source: str = "zone"


@dataclass
class Footprint:
    """A placed footprint with its pads and courtyard (where a padless part's power goes)."""
    ref: str
    value: str
    layer: str
    x: float
    y: float
    angle: float
    lib_id: str
    pads: list[Pad] = field(default_factory=list)
    courtyard: Polygon | None = None
    dnp: bool = False


@dataclass
class Board:
    """The board for the thermal solve: outline, stackup, copper items and holes, in mm."""
    path: Path
    outline: Polygon | MultiPolygon
    copper_layers: list[str]
    stackup: list[StackLayer]
    nets: list[str]
    footprints: list[Footprint]
    tracks: list[Track]
    vias: list[Via]
    copper: list[CopperShape]
    holes: list[Polygon]  # non-plated holes and cut-outs that remove board material
    warnings: list[str] = field(default_factory=list)
    zones_unfilled: int = 0
    kicad_version: str = ""

    @property
    def pads(self) -> list[Pad]:
        """Every footprint's pads."""
        return [p for f in self.footprints for p in f.pads]

    @property
    def thickness(self) -> float:
        """Total board thickness (mm)."""
        return sum(l.thickness for l in self.stackup)

    def copper_thickness(self, layer: str) -> float:
        """A copper layer's thickness (1 oz when the stackup has none)."""
        for l in self.stackup:
            if l.kind == "copper" and l.name == layer:
                return l.thickness
        return DEFAULT_CU_MM

    def footprint(self, ref: str) -> Footprint | None:
        """The footprint with this reference."""
        for f in self.footprints:
            if f.ref == ref:
                return f
        return None

    def pad(self, key: str) -> Pad | None:
        """The pad "REF.NUMBER", None when gone."""
        ref, _, num = key.partition(".")
        f = self.footprint(ref)
        if f:
            for p in f.pads:
                if p.number == num:
                    return p
        return None

    def bounds(self) -> tuple[float, float, float, float]:
        """The outline's bounding box."""
        return self.outline.bounds


# ---- helpers ----------------------------------------------------------------

def _copper_sort_key(name: str):
    """Stackup order: F.Cu, inner layers by number, B.Cu."""
    if name == "F.Cu":
        return (0, 0)
    if name == "B.Cu":
        return (2, 0)
    m = re.match(r"In(\d+)\.Cu", name)
    return (1, int(m.group(1)) if m else 999)


def _expand_layers(tokens: list[str], copper: list[str]) -> list[str]:
    """A pad's layer list as copper layers ("*.Cu": all, "F&B.Cu": the outer two), each once in order."""
    out: list[str] = []
    for t in tokens:
        if t in ("*.Cu",):
            out.extend(copper)
        elif t == "F&B.Cu":
            out.extend([copper[0], copper[-1]])
        elif t.endswith(".Cu") and t in copper:
            out.append(t)
    seen: set[str] = set()
    return [l for l in out if not (l in seen or seen.add(l))]


def _valid(poly) -> Polygon | MultiPolygon:
    """A valid (multi)polygon: invalid geometry repaired with make_valid, lines and points it produced dropped."""
    if poly is None or poly.is_empty:
        return Polygon()
    if not poly.is_valid:
        poly = shapely.make_valid(poly)
    if poly.geom_type == "GeometryCollection":
        poly = unary_union([g for g in poly.geoms if g.geom_type in ("Polygon", "MultiPolygon")])
    return poly


def _stroke_to_poly(points, width: float):
    """A stroked polyline's area (buffered by half its width; a single point is a dot)."""
    if len(points) < 2:
        return Point(points[0]).buffer(width / 2, quad_segs=8) if points else Polygon()
    return LineString(points).buffer(max(width, 1e-4) / 2, quad_segs=8)


def _graphic_geometry(node, prefix: str, transform=None):
    """Return (linework, filled_polygon) for gr_*/fp_* graphic nodes."""
    kind = node[0][len(prefix):]
    width = S.fvalue(S.child(node, "stroke") or [], "width", None)
    if width is None:
        width = S.fvalue(node, "width", 0.0)
    fill_tok = S.value(node, "fill", "no")
    filled = fill_tok in ("yes", "solid")
    line_pts: list[list[tuple[float, float]]] = []
    fill_poly = None
    if kind == "line":
        line_pts.append([S.xy(S.child(node, "start")), S.xy(S.child(node, "end"))])
    elif kind == "arc":
        line_pts.append(arc_points(S.xy(S.child(node, "start")), S.xy(S.child(node, "mid")), S.xy(S.child(node, "end"))))
    elif kind == "circle":
        c = S.xy(S.child(node, "center"))
        e = S.xy(S.child(node, "end"))
        r = math.hypot(e[0] - c[0], e[1] - c[1])
        ring = circle_points(c[0], c[1], r)
        line_pts.append(ring + [ring[0]])
        if filled:
            fill_poly = Polygon(ring)
    elif kind == "rect":
        (x0, y0), (x1, y1) = S.xy(S.child(node, "start")), S.xy(S.child(node, "end"))
        ring = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
        line_pts.append(ring + [ring[0]])
        if filled:
            fill_poly = Polygon(ring)
    elif kind == "poly":
        ring = S.pts(S.child(node, "pts"))
        if len(ring) >= 2:
            line_pts.append(ring + [ring[0]])
        if filled and len(ring) >= 3:
            fill_poly = Polygon(ring)
    elif kind == "curve":
        p = S.pts(S.child(node, "pts"))
        if len(p) == 4:                 # a cubic Bezier from its 4 control points, sampled at 24 points
            t = np.linspace(0, 1, 24)[:, None]
            P = np.array(p)
            b = ((1 - t) ** 3) * P[0] + 3 * ((1 - t) ** 2) * t * P[1] + 3 * (1 - t) * t * t * P[2] + t ** 3 * P[3]
            line_pts.append([tuple(q) for q in b])
    if transform:
        line_pts = [[transform(*q) for q in pl] for pl in line_pts]
        if fill_poly is not None:
            fill_poly = Polygon([transform(*q) for q in fill_poly.exterior.coords])
    return line_pts, width, fill_poly


def _rounded_rect(w, h, r):
    """A w x h rectangle centred on 0 with corner radius r: the inner rectangle grown by r (round corners)."""
    r = max(0.0, min(r, min(w, h) / 2 - 1e-6))
    if r <= 0:
        return box(-w / 2, -h / 2, w / 2, h / 2)
    return box(-w / 2 + r, -h / 2 + r, w / 2 - r, h / 2 - r).buffer(r, quad_segs=8)


def _chamfer(poly, w, h, c, corners):
    """Cut a triangle of leg c off each named corner."""
    cuts = []
    # KiCad y down: "top" is -y
    for name, (sx, sy) in {"top_left": (-1, -1), "top_right": (1, -1), "bottom_left": (-1, 1), "bottom_right": (1, 1)}.items():
        if name in corners:
            cx, cy = sx * w / 2, sy * h / 2
            cuts.append(Polygon([(cx, cy), (cx - sx * c, cy), (cx, cy - sy * c)]))
    return poly.difference(unary_union(cuts)) if cuts else poly


def _pad_local_shape(pnode, shape: str, w: float, h: float):
    """A pad's shape around its own origin, unrotated: circle, oval (a stadium: a segment buffered), rect,
    roundrect (with chamfers), trapezoid (rect_delta), or custom (the anchor united with its primitives)."""
    if shape == "circle":
        return Point(0, 0).buffer(w / 2, quad_segs=16)
    if shape == "oval":
        if abs(w - h) < 1e-9:
            return Point(0, 0).buffer(w / 2, quad_segs=16)
        if w > h:
            return LineString([(-(w - h) / 2, 0), ((w - h) / 2, 0)]).buffer(h / 2, quad_segs=16)
        return LineString([(0, -(h - w) / 2), (0, (h - w) / 2)]).buffer(w / 2, quad_segs=16)
    if shape == "rect":
        return box(-w / 2, -h / 2, w / 2, h / 2)
    if shape == "roundrect":
        rr = S.fvalue(pnode, "roundrect_rratio", 0.25)
        poly = _rounded_rect(w, h, rr * min(w, h))
        ch = S.child(pnode, "chamfer")
        if ch is not None:
            poly = _chamfer(poly, w, h, S.fvalue(pnode, "chamfer_ratio", 0.0) * min(w, h), ch[1:])
        return poly
    if shape == "trapezoid":
        d = S.child(pnode, "rect_delta")
        dx, dy = (float(d[1]) / 2, float(d[2]) / 2) if d else (0.0, 0.0)
        hx, hy = w / 2, h / 2
        return Polygon([(-hx - dy, hy + dx), (-hx + dy, -hy - dx), (hx - dy, -hy + dx), (hx + dy, hy - dx)])
    if shape == "custom":
        parts = []
        opts = S.child(pnode, "options")
        anchor = S.value(opts, "anchor", "rect") if opts else "rect"
        parts.append(Point(0, 0).buffer(w / 2, quad_segs=16) if anchor == "circle" else box(-w / 2, -h / 2, w / 2, h / 2))
        prims = S.child(pnode, "primitives")
        for g in (prims[1:] if prims else []):
            if not isinstance(g, list):
                continue
            lines, width, fill = _graphic_geometry(g, "gr_")
            if fill is not None:
                parts.append(fill.buffer(width / 2) if width else fill)
            elif g[0] == "gr_poly":
                ring = S.pts(S.child(g, "pts"))
                if len(ring) >= 3:
                    p = Polygon(ring)
                    parts.append(p.buffer(width / 2) if width else p)
            else:
                for pl in lines:
                    parts.append(_stroke_to_poly(pl, width))
        return _valid(unary_union(parts))
    return box(-w / 2, -h / 2, w / 2, h / 2)


# ---- reader -------------------------------------------------------------------

class _NetResolver:
    """A node's net name: KiCad 10 writes the name inline; older files a number into the board's net table."""

    def __init__(self, root):
        """Read the net table (empty in KiCad 10 files)."""
        self.by_id: dict[str, str] = {}
        for n in S.children(root, "net"):
            if len(n) >= 3:
                self.by_id[n[1]] = n[2]

    def __call__(self, node) -> str:
        """The net of a node's (net ...) child, "" for none (net 0)."""
        n = S.child(node, "net")
        if n is None:
            return ""
        if len(n) >= 3:
            return n[2]
        tok = n[1] if len(n) > 1 else ""
        if tok in self.by_id:
            return self.by_id[tok]
        return "" if tok == "0" else tok


def load_board(path: str | Path) -> Board:
    """Read a .kicad_pcb file: copper layers, stackup, footprints with their pads, tracks and arcs, vias, zone
    fills, copper graphics, the outline from Edge.Cuts (the copper's bounding box when it is not closed), and
    non-plated holes."""
    path = Path(path)
    root = S.parse(path.read_text(encoding="utf-8"))
    if not root or root[0] != "kicad_pcb":
        raise ValueError(f"{path.name} is not a KiCad PCB file")
    warnings: list[str] = []
    net_of = _NetResolver(root)

    # Copper layers
    layers_node = S.child(root, "layers")
    copper = sorted(
        {l[1] for l in (layers_node[1:] if layers_node else []) if isinstance(l, list) and len(l) > 1 and str(l[1]).endswith(".Cu")},
        key=_copper_sort_key,
    )
    if not copper:
        copper = ["F.Cu", "B.Cu"]

    stackup = _read_stackup(root, copper, warnings)

    # Footprints / pads
    footprints: list[Footprint] = []
    edge_lines: list[list[tuple[float, float]]] = []
    copper_shapes: list[CopperShape] = []
    holes: list[Polygon] = []
    for fnode in S.children(root, "footprint"):
        fp = _read_footprint(fnode, copper, net_of, edge_lines, copper_shapes, holes)
        footprints.append(fp)

    # Tracks
    tracks: list[Track] = []
    for t in S.children(root, "segment"):
        layer = S.value(t, "layer")
        if layer in copper:
            tracks.append(Track(layer, net_of(t), S.fvalue(t, "width", 0.2),
                                [S.xy(S.child(t, "start")), S.xy(S.child(t, "end"))]))
    for t in S.children(root, "arc"):
        layer = S.value(t, "layer")
        if layer in copper:
            tracks.append(Track(layer, net_of(t), S.fvalue(t, "width", 0.2),
                                arc_points(S.xy(S.child(t, "start")), S.xy(S.child(t, "mid")), S.xy(S.child(t, "end")))))

    # Vias
    vias: list[Via] = []
    for v in S.children(root, "via"):
        lay = S.child(v, "layers")
        names = [n for n in (lay[1:] if lay else []) if n in copper] or [copper[0], copper[-1]]
        names.sort(key=copper.index)
        x, y = S.xy(S.child(v, "at"))
        filled = S.child(v, "filling") is not None or S.child(v, "capping") is not None
        vias.append(Via(x, y, S.fvalue(v, "size", 0.6), S.fvalue(v, "drill", 0.3), names[0], names[-1], net_of(v), filled))

    # Zones
    zones_unfilled = 0
    for z in S.children(root, "zone"):
        if S.child(z, "keepout") is not None:
            continue
        net = net_of(z) or (S.value(z, "net_name", "") or "")
        fills = list(S.children(z, "filled_polygon"))
        if not fills:
            zl = S.child(z, "layers") or S.child(z, "layer")
            if zl and any(str(n).endswith(".Cu") for n in zl[1:]):
                zones_unfilled += 1
            continue
        for fpoly in fills:
            layer = S.value(fpoly, "layer")
            if layer not in copper:
                continue
            ring = S.pts(S.child(fpoly, "pts"))
            if len(ring) >= 3:
                copper_shapes.append(CopperShape(layer, net, _valid(Polygon(ring)), "zone"))
    if zones_unfilled:
        warnings.append(f"{zones_unfilled} copper zone(s) have no fill data. Refill zones in KiCad (press B) and save.")

    # Board graphics: outline and copper graphics
    for g in root[1:]:
        if not (isinstance(g, list) and g and str(g[0]).startswith("gr_")) or g[0] in ("gr_text", "gr_text_box"):
            continue
        layer = S.value(g, "layer")
        lines, width, fill = _graphic_geometry(g, "gr_")
        if layer == "Edge.Cuts":
            edge_lines.extend(lines)
        elif layer in copper:
            parts = [fill.buffer(width / 2) if (fill is not None and width) else fill] if fill is not None else []
            parts += [_stroke_to_poly(pl, width) for pl in lines] if fill is None else []
            shp = _valid(unary_union(parts)) if parts else Polygon()
            if not shp.is_empty:
                copper_shapes.append(CopperShape(layer, net_of(g), shp, "graphic"))

    outline = _build_outline(edge_lines, warnings)
    if outline.is_empty:
        allc = [c.shape for c in copper_shapes] + [p.shape for f in footprints for p in f.pads]
        if allc:
            outline = box(*unary_union(allc).bounds).buffer(1.0, join_style=2)
        else:
            outline = box(0, 0, 10, 10)
        warnings.append("No closed board outline found on Edge.Cuts; using the copper bounding box.")

    for fp in footprints:
        for p in fp.pads:
            if p.kind == "np_thru_hole" and p.drill_shape is not None:
                holes.append(p.drill_shape)

    ver = str(S.value(root, "version", ""))
    # KiCad 10 names nets inline instead of keeping a net table.
    nets = {*net_of.by_id.values()} | {t.net for t in tracks} | {v.net for v in vias} |         {c.net for c in copper_shapes} | {p.net for f in footprints for p in f.pads}
    return Board(path, outline, copper, stackup, sorted(nets - {""}),
                 footprints, tracks, vias, copper_shapes, holes, warnings, zones_unfilled, ver)


def _read_stackup(root, copper, warnings) -> list[StackLayer]:
    """The stackup from (setup (stackup ...)), a sub-layered dielectric with its sub-layers' total thickness.
    Accepted only when it alternates copper and dielectric for every copper layer; otherwise 1 oz copper spread
    evenly over the board's thickness, as FR4."""
    setup = S.child(root, "setup")
    st = S.child(setup, "stackup") if setup else None
    out: list[StackLayer] = []
    if st is not None:
        for l in S.children(st, "layer"):
            name = l[1]
            typ = S.value(l, "type", "")
            thick = sum(float(t[1]) for t in S.children(l, "thickness") if len(t) > 1)
            if name.endswith(".Cu") and name in copper:
                out.append(StackLayer(name, "copper", thick or DEFAULT_CU_MM))
            elif typ in ("core", "prepreg") or name.startswith("dielectric"):
                out.append(StackLayer(name, "dielectric", thick or 0.1, S.value(l, "material", "FR4") or "FR4",
                                      S.fvalue(l, "epsilon_r", None)))
    ok = [l.name for l in out if l.kind == "copper"] == copper and len(out) == 2 * len(copper) - 1
    if not ok:
        gen = S.child(root, "general")
        total = S.fvalue(gen, "thickness", DEFAULT_BOARD_MM) if gen else DEFAULT_BOARD_MM
        n = len(copper)
        d = max(0.05, (total - n * DEFAULT_CU_MM) / max(1, n - 1))
        out = []
        for i, c in enumerate(copper):
            out.append(StackLayer(c, "copper", DEFAULT_CU_MM))
            if i < n - 1:
                out.append(StackLayer(f"dielectric {i + 1}", "dielectric", d, "FR4"))
        if st is not None:
            warnings.append("Board stackup incomplete; assumed evenly spaced FR4 with 1 oz copper.")
    return out


def _read_footprint(fnode, copper, net_of, edge_lines, copper_shapes, holes) -> Footprint:
    """A footprint: reference and value, DNP, its graphics (Edge.Cuts lines join the outline, courtyard lines
    become its courtyard's convex hull, copper graphics become copper), and its pads."""
    at = S.child(fnode, "at")
    fx, fy = S.xy(at)
    fa = float(at[3]) if at is not None and len(at) > 3 else 0.0
    ref = ""
    value = ""
    for p in S.children(fnode, "property"):
        if p[1] == "Reference":
            ref = p[2]
        elif p[1] == "Value":
            value = p[2]
    for t in S.children(fnode, "fp_text"):  # KiCad 6/7
        if t[1] == "reference" and not ref:
            ref = t[2]
        elif t[1] == "value" and not value:
            value = t[2]
    attr = S.child(fnode, "attr")
    dnp = bool(attr and "dnp" in attr[1:])
    fp = Footprint(ref or "?", value, S.value(fnode, "layer", "F.Cu"), fx, fy, fa, fnode[1] if len(fnode) > 1 else "", dnp=dnp)

    def to_board(x, y):
        """A footprint-local point in board coordinates."""
        rx, ry = rotate(x, y, fa)
        return fx + rx, fy + ry

    court: list = []
    for g in fnode[1:]:
        if not (isinstance(g, list) and g and str(g[0]).startswith("fp_")) or g[0] in ("fp_text", "fp_text_box"):
            continue
        layer = S.value(g, "layer")
        lines, width, fill = _graphic_geometry(g, "fp_", to_board)
        if layer == "Edge.Cuts":
            edge_lines.extend(lines)
        elif layer in ("F.CrtYd", "B.CrtYd"):
            if fill is not None:
                court.append(fill)
            for pl in lines:
                if len(pl) >= 3 and pl[0] == pl[-1]:
                    court.append(Polygon(pl))
                else:
                    court.append(LineString(pl))
        elif layer in copper:
            shp = fill if fill is not None else unary_union([_stroke_to_poly(pl, width) for pl in lines])
            if not shp.is_empty:
                copper_shapes.append(CopperShape(layer, "", _valid(shp), "footprint"))
    if court:
        u = unary_union(court)
        fp.courtyard = Polygon(u.convex_hull.exterior) if u.convex_hull.geom_type == "Polygon" else None

    for pn in S.children(fnode, "pad"):
        pad = _read_pad(pn, fp, fx, fy, copper, net_of)
        if pad is not None:
            fp.pads.append(pad)
    return fp


def _read_pad(pn, fp: Footprint, fx, fy, copper, net_of) -> Pad | None:
    """A pad in board coordinates: its shape turned by the pad's own absolute angle and moved to its position
    (turned with the footprint), the drill cut out (an oval drill counts as a round one of the same area for the
    barrel), and its mask openings."""
    number = pn[1] if len(pn) > 1 else ""
    kind = pn[2] if len(pn) > 2 else "smd"
    shape = pn[3] if len(pn) > 3 else "rect"
    at = S.child(pn, "at")
    px, py = S.xy(at)
    pa = float(at[3]) if at is not None and len(at) > 3 else fp.angle  # pad angle is absolute
    size = S.child(pn, "size")
    w, h = (float(size[1]), float(size[2])) if size else (1.0, 1.0)
    lay = S.child(pn, "layers")
    lay_tokens = list(lay[1:]) if lay else []
    cu = _expand_layers(lay_tokens, copper)
    rx, ry = rotate(px, py, fp.angle)
    cx, cy = fx + rx, fy + ry

    local = _pad_local_shape(pn, shape, w, h)

    drill = S.child(pn, "drill")
    drill_d = 0.0
    hole_local = None
    if drill is not None and len(drill) > 1:
        toks = [t for t in drill[1:] if not isinstance(t, list)]
        off = S.xy(S.child(drill, "offset"))
        if toks and toks[0] == "oval":
            dw = float(toks[1])
            dh = float(toks[2]) if len(toks) > 2 else dw
            hole_local = _pad_local_shape([], "oval", dw, dh)
            drill_d = math.sqrt(dw * dh)
        elif toks:
            drill_d = float(toks[0])
            hole_local = Point(0, 0).buffer(drill_d / 2, quad_segs=16)
        # Pad position is the hole centre; (offset) shifts the copper shape.
        local = affinity.translate(local, *off)

    def place(g):
        """Local pad geometry to the board."""
        # Pad geometry: rotate by pad absolute angle (KiCad convention) then translate.
        g = affinity.affine_transform(g, _rot_matrix(pa))
        return affinity.translate(g, cx, cy)

    shp = place(local)
    hole = place(hole_local) if hole_local is not None else None
    if hole is not None and kind in ("thru_hole", "np_thru_hole"):
        shp = shp.difference(hole)
    if kind == "np_thru_hole":
        cu = [] if shp.is_empty or shp.area < 1e-6 else cu
    return Pad(fp.ref, number, net_of(pn), kind, cx, cy, cu, _valid(shp), drill_d if kind != "smd" else 0.0, hole,
               "F.Mask" in lay_tokens or "*.Mask" in lay_tokens, "B.Mask" in lay_tokens or "*.Mask" in lay_tokens)


def _rot_matrix(deg: float):
    """shapely's affine matrix [a, b, d, e, xoff, yoff] for KiCad's rotation (y down, see rotate)."""
    a = math.radians(deg)
    c, s = math.cos(a), math.sin(a)
    # x' = x c + y s ; y' = -x s + y c
    return [c, s, -s, c, 0.0, 0.0]


def _build_outline(lines, warnings) -> Polygon | MultiPolygon:
    """The board from its Edge.Cuts linework: the segments noded and polygonized into faces; starting from the
    largest, each further face is XORed in, so a contour inside the board becomes a cut-out (and one inside a
    cut-out board again)."""
    if not lines:
        return Polygon()
    segs = []
    for pl in lines:
        for a, b in zip(pl[:-1], pl[1:]):
            if a != b:
                segs.append(LineString([a, b]))
    if not segs:
        return Polygon()
    # Snap near-coincident end points so small drafting gaps still close.
    merged = shapely.set_precision(unary_union(segs), 1e-4)
    faces = sorted((Polygon(f.exterior) for f in polygonize(merged)), key=lambda p: p.area, reverse=True)
    if not faces:
        warnings.append("Board outline on Edge.Cuts is not closed.")
        return Polygon()
    result = faces[0]
    for f in faces[1:]:
        result = result.symmetric_difference(f)
    return _valid(result)
