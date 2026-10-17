"""Collect copper geometry per (net, layer) from a board model."""

from __future__ import annotations

from collections import defaultdict

from shapely.geometry import LineString, Point
from shapely.ops import unary_union
from shapely.strtree import STRtree

from ..kicad.board import Board, Via


def via_span(board: Board, v: Via) -> list[str]:
    """The copper layers a via connects (its end layers and everything between)."""
    cu = board.copper_layers
    a, b = cu.index(v.top), cu.index(v.bottom)
    return cu[min(a, b):max(a, b) + 1]


def via_annulus(v: Via):
    """A via's copper on each layer it spans, as a disk of its pad size."""
    # Solid disk: the drill is usually smaller than a cell, and the barrel model
    # (electrical.py) accounts for where current actually enters the plating.
    return Point(v.x, v.y).buffer(v.size / 2, quad_segs=8)


def track_poly(t):
    """A track's copper: its centre line buffered by half its width (round ends, as KiCad draws tracks); a
    zero-length track is a dot."""
    if len(t.points) == 1 or all(p == t.points[0] for p in t.points):
        return Point(t.points[0]).buffer(t.width / 2, quad_segs=8)
    return LineString(t.points).buffer(t.width / 2, quad_segs=8)


def drilled_holes(board: Board) -> list:
    """Holes that remove board material on every layer: NPTH, cut-outs and plated pad drills.

    Vias are left out: their drills are usually below a cell, and the barrel models
    (thermal.py, electrical.py) treat them as solid disks.
    """
    return list(board.holes) + [p.drill_shape for p in board.pads if p.plated and p.drill_shape is not None]


def via_holes(board: Board) -> dict[str, list]:
    """{layer: the via drill disks on it}."""
    per: dict[str, list] = defaultdict(list)
    for v in board.vias:
        if v.drill > 0:
            h = Point(v.x, v.y).buffer(v.drill / 2, quad_segs=8)
            for L in via_span(board, v):
                per[L].append(h)
    return per


def _cut(geoms: list, tree: STRtree | None, holes: list) -> list:
    """Subtract the holes from each geometry. The STRtree (an R-tree of the holes' bounding boxes) finds the few
    holes touching a geometry, so it is not tested against all of them."""
    if tree is None:
        return geoms
    out = []
    for g in geoms:
        hits = tree.query(g, predicate="intersects")
        if len(hits):
            g = g.difference(unary_union([holes[i] for i in hits]))
            if g.is_empty:
                continue
        out.append(g)
    return out


def copper_geoms(board: Board, nets: set[str] | None = None,
                 via_drills: bool = False) -> dict[tuple[str, str], list]:
    """Map (net, layer) -> list of shapely geometries. ``nets=None`` means every item.

    Drilled holes are cut out of every item (zone fills and tracks run over pad
    drills in the KiCad file). ``via_drills`` also cuts via holes, for display.
    """
    out = _copper_geoms(board, nets)
    common = drilled_holes(board)
    per_layer = via_holes(board) if via_drills else {}
    trees = {}
    for key, gl in out.items():
        L = key[1]
        if L not in trees:
            holes = common + per_layer.get(L, [])
            trees[L] = (STRtree(holes) if holes else None, holes)
        tree, holes = trees[L]
        out[key] = _cut(gl, tree, holes)
    return out


def _copper_geoms(board: Board, nets: set[str] | None) -> dict[tuple[str, str], list]:
    """Every copper item's shape, by (net, layer): tracks, pads, zone fills and via disks (holes not cut yet)."""
    out: dict[tuple[str, str], list] = defaultdict(list)

    def want(n):
        """The item's net is one asked for."""
        return nets is None or n in nets

    for t in board.tracks:
        if want(t.net):
            out[(t.net, t.layer)].append(track_poly(t))
    for p in board.pads:
        if want(p.net):
            for L in p.layers:
                out[(p.net, L)].append(p.shape)
    for c in board.copper:
        if want(c.net):
            out[(c.net, c.layer)].append(c.shape)
    for v in board.vias:
        if want(v.net):
            ann = via_annulus(v)
            for L in via_span(board, v):
                out[(v.net, L)].append(ann)
    return out


def layer_geoms(board: Board) -> dict[str, list]:
    """All copper on each layer regardless of net (for thermal conduction)."""
    per: dict[str, list] = defaultdict(list)
    for (net, layer), g in copper_geoms(board).items():
        per[layer].extend(g)
    return per
