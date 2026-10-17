"""GroundTruth's board model -> PCB Thermal's (thermal/kicad/board.py), so the thermal solve runs on exactly the
board GroundTruth analysed: the live one read through KiCad's API too (the saved file may be older).

Mapped: outline (Edge.Cuts polygons, inner ones as cut-outs), stackup (copper and dielectric thicknesses; the
dielectrics as FR4, PCB Thermal's default material), footprints and pads (polygons from geometry.shapes, plated
drills cut out), tracks (arcs as chords), vias, zone fills. Not in GroundTruth's model, so not here: non-plated
holes (the file reader skips NPTH pads), copper graphics, solder-mask openings (every face counts as masked for the
emissivity), DNP parts' bodies (they carry no heat either way)."""
from __future__ import annotations

from pathlib import Path

from shapely.geometry import Point, Polygon as SPolygon
from shapely.ops import unary_union

from ..core.board import Board
from ..geometry.shapes import arc_points, pad_shape
from .kicad import board as tb

FR4 = "FR4"


def _poly(p) -> SPolygon:
    """A board-model polygon (outline, holes) as a shapely polygon, repaired with buffer(0) when invalid (a
    self-touching outline)."""
    outline, holes = p
    g = SPolygon(outline, holes)
    return g if g.is_valid else g.buffer(0)


def _outline(board: Board):
    """The board shape: the outline polygons not inside a larger one, minus those inside (cut-outs). Sorting by
    area first means each polygon only needs testing against the larger ones before it."""
    polys = [_poly(p) for p in board.outline if len(p[0]) >= 3]
    polys = [p for p in polys if not p.is_empty and p.area > 0]
    if not polys:
        raise ValueError("the board has no outline (Edge.Cuts): the thermal model needs one")
    polys.sort(key=lambda p: -p.area)
    outer = [p for k, p in enumerate(polys) if not any(q.contains(p) for q in polys[:k])]
    inner = [p for p in polys if p not in outer]
    shape = unary_union(outer)
    return shape.difference(unary_union(inner)) if inner else shape


def _stackup(board: Board) -> list:
    """PCB Thermal's stack, top to bottom: each copper layer, then the dielectric under it (as FR4)."""
    out = []
    st = board.stackup
    for k, c in enumerate(st.copper):
        out.append(tb.StackLayer(c.name, "copper", c.thickness))
        if k < len(st.dielectrics):
            d = st.dielectrics[k]
            out.append(tb.StackLayer(d.name or f"dielectric {k + 1}", "dielectric", d.thickness, FR4, d.epsilon_r))
    return out


def _pad(p, fp) -> tb.Pad:
    """A pad with its copper shape; a plated hole is cut out of it (the barrel is modelled as a via)."""
    shape = pad_shape(p)
    drill_shape = None
    if p.drill > 0 and p.pad_type == "thru_hole":
        drill_shape = Point(p.position).buffer(p.drill / 2, quad_segs=8)
        shape = shape.difference(drill_shape)
    return tb.Pad(fp.reference, p.number, p.net, p.pad_type, p.position[0], p.position[1], list(p.layers), shape,
                  p.drill, drill_shape, mask_top="F.Cu" in p.layers, mask_bottom="B.Cu" in p.layers)


def to_thermal_board(board: Board) -> tb.Board:
    """PCB Thermal's board for GroundTruth's `board` (see the module for what is mapped)."""
    footprints = []
    for fp in board.footprints:
        f = tb.Footprint(fp.reference, fp.value, fp.layer, fp.position[0], fp.position[1], fp.angle, fp.lib_id,
                         dnp=fp.dnp)
        f.pads = [_pad(p, fp) for p in fp.pads if p.layers and p.pad_type != "np_thru_hole"]
        footprints.append(f)
    tracks = []
    for t in board.tracks:
        pts = arc_points(t.start, t.mid, t.end) if t.is_arc else [t.start, t.end]
        tracks.append(tb.Track(t.layer, t.net, t.width, [tuple(p) for p in pts]))
    vias = [tb.Via(v.position[0], v.position[1], v.size, v.drill, v.layers[0], v.layers[-1], v.net)
            for v in board.vias if v.layers]
    copper = []
    for z in board.zones:
        if z.is_rule_area:
            continue
        for layer, polys in z.filled.items():
            shapes = [_poly(p) for p in polys if len(p[0]) >= 3]
            if shapes:
                copper.append(tb.CopperShape(layer, z.net, unary_union(shapes), "zone"))
    path = Path(board.source) if board.source and not board.source.startswith("kicad-ipc:") else Path("live")
    out = tb.Board(path=path, outline=_outline(board), copper_layers=list(board.copper_layers),
                   stackup=_stackup(board), nets=list(board.nets), footprints=footprints, tracks=tracks, vias=vias,
                   copper=copper, holes=[], warnings=[])
    out.zones_unfilled = len(board.unfilled_zones())
    return out
