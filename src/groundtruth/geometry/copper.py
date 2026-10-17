"""Copper geometry per (layer, net), built once per board and cached."""
from __future__ import annotations

from collections import defaultdict
from functools import cached_property
from typing import Iterable, Optional

from shapely.geometry import Point as SPoint, Polygon as SPolygon, box
from shapely.ops import unary_union

from ..core.board import Board
from .shapes import clean_polygon, pad_shape, to_shapely, track_shape


class CopperMap:
    """The board's copper as shapely geometry, per (layer, net): tracks, via barrels' pads, footprint pads and
    filled zones. The shapes are collected once here; their union for a (layer, net) is made when first asked
    for and cached (unions of whole planes are the expensive part). Zone fills are also kept on their own:
    they are what counts as a reference plane for return currents."""

    def __init__(self, board: Board):
        """Collect the board's copper shapes (no unions yet)."""
        self.board = board
        # every piece of copper, by (layer, net); unioned lazily in get()
        self._parts: dict[tuple[str, str], list] = defaultdict(list)
        for t in board.tracks:
            self._parts[(t.layer, t.net)].append(track_shape(t))
        for v in board.vias:
            # a via's annular ring on every layer it spans (its drill hole is not cut out: the plating conducts)
            disc = SPoint(v.position).buffer(v.size / 2, quad_segs=8)
            for layer in v.layers:
                self._parts[(layer, v.net)].append(disc)
        for fp in board.footprints:
            for p in fp.pads:
                shape = pad_shape(p)
                for layer in p.layers:
                    self._parts[(layer, p.net)].append(shape)
        for z in board.zones:
            if z.is_rule_area:              # keep-outs and rule areas are not copper
                continue
            for layer, polys in z.filled.items():
                for poly in polys:
                    g = clean_polygon(poly)     # KiCad's fractured fill polygons back into polygons with holes
                    if not g.is_empty:
                        self._parts[(layer, z.net)].append(g)
        self._cache: dict[tuple[str, str], object] = {}
        # Zone fills alone: what counts as a "plane" for return currents.
        self._zone_parts: dict[tuple[str, str], list] = defaultdict(list)
        for z in board.zones:
            if z.is_rule_area:
                continue
            for layer, polys in z.filled.items():
                for poly in polys:
                    g = clean_polygon(poly)
                    if not g.is_empty:
                        self._zone_parts[(layer, z.net)].append(g)
        self._zone_cache: dict[tuple[str, Optional[str]], object] = {}

    def planes(self, layer: str) -> dict[str, object]:
        """net -> filled zone copper on `layer`."""
        out = {}
        for (lay, net) in self._zone_parts:
            if lay == layer:
                out[net] = self.plane(layer, net)
        return out

    def plane(self, layer: str, net: Optional[str] = None):
        """Zone copper on a layer for one net, or for all nets when net is None."""
        key = (layer, net)
        if key not in self._zone_cache:
            if net is None:
                parts = [g for (lay, _), gs in self._zone_parts.items() if lay == layer for g in gs]
            else:
                parts = self._zone_parts.get((layer, net), [])
            self._zone_cache[key] = unary_union(parts) if parts else SPolygon()
        return self._zone_cache[key]

    def plane_net_at(self, layer: str, geom) -> Optional[str]:
        """Net whose plane overlaps `geom` most on `layer`."""
        best, best_area = None, 0.0
        for net, g in self.planes(layer).items():
            if not g.intersects(geom):
                continue
            inter = g.intersection(geom)
            a = inter.area if inter.area > 0 else inter.length
            if a > best_area:
                best, best_area = net, a
        return best

    def nets_on(self, layer: str) -> set[str]:
        """The nets with any copper on `layer`."""
        return {net for (lay, net) in self._parts if lay == layer}

    def get(self, layer: str, net: str):
        """All copper of `net` on `layer` as one (Multi)Polygon, empty when there is none (cached)."""
        key = (layer, net)
        if key not in self._cache:
            parts = self._parts.get(key, [])
            self._cache[key] = unary_union(parts) if parts else SPolygon()
        return self._cache[key]

    def union(self, layer: str, nets: Iterable[str]):
        """The copper of several nets on `layer`, merged into one shape."""
        geoms = [self.get(layer, n) for n in nets]
        geoms = [g for g in geoms if not g.is_empty]
        return unary_union(geoms) if geoms else SPolygon()

    @cached_property
    def outline(self):
        """The board's outline (Edge.Cuts) as a shape; without one, the bounding box of all its copper."""
        if self.board.outline:
            return unary_union([to_shapely(p) for p in self.board.outline])
        all_parts = [g for parts in self._parts.values() for g in parts]
        if not all_parts:
            return SPolygon()
        return box(*unary_union(all_parts).bounds)


def reference_layers(board: Board, layer: str, tolerance: float = 0.02) -> list[tuple[str, float]]:
    """Adjacent copper layer(s) a trace on `layer` is referenced to: the nearest neighbour, or
    both neighbours when they are (almost) equally far away (symmetric stripline).

    Returns [(layer name, height mm), ...] sorted by height.
    """
    cands = sorted(((other, board.stackup.distance(layer, other)) for other in board.stackup.neighbours(layer)),
                   key=lambda c: c[1])
    if not cands:
        return []
    h0 = cands[0][1]
    # within 2 % of the nearest counts as "equally far" (stackup thicknesses are rounded in the file)
    return [c for c in cands if c[1] <= h0 * (1 + tolerance) + 1e-9]
