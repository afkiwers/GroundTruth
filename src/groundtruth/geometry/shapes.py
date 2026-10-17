"""Primitive geometry helpers shared by the adapters and the copper builder (mm units)."""
from __future__ import annotations

import math
from typing import Iterable, Optional, Sequence

import shapely
from shapely.geometry import LineString, MultiPolygon, Point as SPoint, Polygon as SPolygon, box
from shapely.ops import polygonize, unary_union

from ..core.board import Pad, Point, Polygon, Track

ARC_TOLERANCE = 0.005  # mm max chord error when sampling arcs


def rotate(p: Point, angle_deg: float, origin: Point = (0.0, 0.0)) -> Point:
    """Rotate like KiCad: positive angle is counter-clockwise on screen (y axis points down)."""
    a = math.radians(angle_deg)
    c, s = math.cos(a), math.sin(a)
    x, y = p[0] - origin[0], p[1] - origin[1]
    # the usual rotation matrix with y mirrored: in KiCad's coordinates y grows downwards, so a counter-clockwise
    # turn on screen is a clockwise one in the maths convention
    return (origin[0] + x * c + y * s, origin[1] - x * s + y * c)


def circle_from_3pts(a: Point, b: Point, c: Point) -> Optional[tuple[Point, float]]:
    """The circle through three points: ((centre x, centre y), radius), or None when they lie on a line.
    The circumcentre formula: d is twice the signed area of the triangle (zero for collinear points)."""
    ax, ay = a
    bx, by = b
    cx, cy = c
    d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-12:
        return None
    ux = ((ax * ax + ay * ay) * (by - cy) + (bx * bx + by * by) * (cy - ay) + (cx * cx + cy * cy) * (ay - by)) / d
    uy = ((ax * ax + ay * ay) * (cx - bx) + (bx * bx + by * by) * (ax - cx) + (cx * cx + cy * cy) * (bx - ax)) / d
    return (ux, uy), math.hypot(ax - ux, ay - uy)


def arc_points(start: Point, mid: Point, end: Point, tol: float = ARC_TOLERANCE) -> list[Point]:
    """Sample an arc given by start/mid/end into a polyline (inclusive of both ends)."""
    circ = circle_from_3pts(start, mid, end)
    if circ is None:
        return [start, end]
    (cx, cy), r = circ
    a0 = math.atan2(start[1] - cy, start[0] - cx)
    am = math.atan2(mid[1] - cy, mid[0] - cx)
    a1 = math.atan2(end[1] - cy, end[0] - cx)

    def norm(a: float) -> float:
        """An angle in [0, 2 pi)."""
        return a % (2 * math.pi)

    # the arc runs from start to end through mid: take the counter-clockwise sweep from start to end, and if mid
    # is not on it, the arc goes the other (clockwise) way round
    sweep = norm(a1 - a0)
    if norm(am - a0) > sweep:  # mid not on the ccw sweep -> go the other way
        sweep -= 2 * math.pi
    if abs(sweep) < 1e-12:  # full circle
        sweep = 2 * math.pi
    # the angle per chord that keeps the chord within `tol` of the arc: a chord over angle t deviates from the
    # arc by the sagitta r(1 - cos(t/2)), so t = 2 acos(1 - tol/r); tiny arcs get 45 degree steps
    step = 2 * math.acos(max(-1.0, min(1.0, 1 - tol / max(r, 1e-9)))) if r > tol else math.pi / 4
    n = max(2, int(math.ceil(abs(sweep) / max(step, 1e-3))) + 1)
    return [(cx + r * math.cos(a0 + sweep * i / (n - 1)), cy + r * math.sin(a0 + sweep * i / (n - 1)))
            for i in range(n)]


def to_shapely(poly: Polygon) -> SPolygon:
    """A board Polygon (outline, holes) as a shapely Polygon."""
    outline, holes = poly
    return SPolygon(outline, holes)


def from_shapely(geom) -> list[Polygon]:
    """Shapely (Multi)Polygon -> list of board Polygons."""
    out: list[Polygon] = []
    if geom is None or geom.is_empty:
        return out
    geoms = geom.geoms if hasattr(geom, "geoms") else [geom]
    for g in geoms:
        if isinstance(g, SPolygon) and not g.is_empty:
            # shapely repeats the first point at the end of each ring; board polygons do not
            out.append((list(g.exterior.coords)[:-1], [list(r.coords)[:-1] for r in g.interiors]))
    return out


def clean_polygon(poly: Polygon):
    """KiCad stores filled zones as 'fractured' polygons (holes joined to the outline by
    zero-width slits). make_valid/buffer(0) turns them back into polygons with holes."""
    g = SPolygon(poly[0], poly[1]) if len(poly[0]) >= 3 else SPolygon()
    if not g.is_valid:
        g = g.buffer(0)
    return g


def track_shape(t: Track):
    """The copper a track covers: its centreline widened by half its width on each side (round ends, as KiCad
    draws them). A zero-length track is a dot of its width."""
    if t.is_arc:
        line = LineString(arc_points(t.start, t.mid, t.end))
    elif t.start == t.end:
        return SPoint(t.start).buffer(t.width / 2, quad_segs=8)
    else:
        line = LineString([t.start, t.end])
    return line.buffer(t.width / 2, quad_segs=8)


def track_centerline(t: Track) -> LineString:
    """The track's centreline (an arc sampled into a polyline). A zero-length track gets a 1 nm stub, so callers
    always have a valid line to measure along."""
    if t.is_arc:
        return LineString(arc_points(t.start, t.mid, t.end))
    if t.start == t.end:
        return LineString([t.start, (t.start[0] + 1e-6, t.start[1])])
    return LineString([t.start, t.end])


def pad_shape(p: Pad):
    """Copper outline of a pad (absolute coordinates)."""
    if p.polygon is not None:
        return clean_polygon(p.polygon)
    w, h = p.size
    if w <= 0 or h <= 0:                    # a degenerate pad: a small disc, so it still connects
        return SPoint(p.position).buffer(max(w, h, 0.1) / 2)
    if p.shape == "circle":
        return SPoint(p.position).buffer(w / 2, quad_segs=8)
    if p.shape == "oval":
        # an oval (stadium) is the segment between its two end-cap centres widened by the cap radius
        r = min(w, h) / 2
        dx = max(0.0, w / 2 - r)
        dy = max(0.0, h / 2 - r)
        a = rotate((-dx, -dy), p.angle)
        b = rotate((dx, dy), p.angle)
        return LineString([(p.position[0] + a[0], p.position[1] + a[1]),
                           (p.position[0] + b[0], p.position[1] + b[1])]).buffer(r, quad_segs=8)
    if p.shape == "roundrect" and p.roundrect_ratio > 0:
        # a rounded rectangle is the rectangle shrunk by the corner radius, then grown back by it (round corners)
        r = min(w, h) * min(p.roundrect_ratio, 0.5)
        core = box(-w / 2 + r, -h / 2 + r, w / 2 - r, h / 2 - r) if r < min(w, h) / 2 else None
        local = core.buffer(r, quad_segs=4) if core is not None and not core.is_empty else box(-w / 2, -h / 2, w / 2, h / 2)
    else:  # rect, trapezoid, chamfered_rect, custom anchor fallback
        local = box(-w / 2, -h / 2, w / 2, h / 2)
    # built around the origin, then turned by the pad's angle and moved to its position
    pts = [rotate(c, p.angle) for c in local.exterior.coords]
    return SPolygon([(p.position[0] + x, p.position[1] + y) for x, y in pts])


def polygonize_outline(segments: Iterable[Sequence[Point]], closed_shapes: Iterable[Polygon] = ()) -> list[Polygon]:
    """Turn Edge.Cuts line work into board polygons. Inner closed contours become holes."""
    lines = [LineString(s) for s in segments if len(s) >= 2]
    polys = [SPolygon(to_shapely(p).exterior) for p in closed_shapes]
    if lines:
        # node the loose segments where they meet (unary_union), then find every closed face they enclose
        faces = list(polygonize(unary_union(lines)))
        if not faces:
            # Tolerate tiny gaps in hand-drawn outlines: use the holes of a thin buffer.
            snapped = unary_union([l.buffer(0.01) for l in lines])
            faces = [SPolygon(r) for g in getattr(snapped, "geoms", [snapped]) for r in getattr(g, "interiors", [])]
        polys.extend(SPolygon(f.exterior) for f in faces)
    if not polys:
        return []
    # Even-odd nesting on exterior rings: biggest first; a ring inside current copper area is a cut-out.
    polys.sort(key=lambda g: g.area, reverse=True)
    result = SPolygon()
    for g in polys:
        if not result.is_empty and result.contains(g.representative_point()):
            result = result.difference(g)
        else:
            result = result.union(g)
    return from_shapely(result)


def multipolygon(geoms: list) -> MultiPolygon:
    """The union of the shapes as one MultiPolygon (empty and None shapes ignored; lines or points that the
    union may produce are dropped)."""
    u = unary_union([g for g in geoms if g is not None and not g.is_empty])
    if isinstance(u, SPolygon):
        return MultiPolygon([u])
    if isinstance(u, MultiPolygon):
        return u
    return MultiPolygon([g for g in getattr(u, "geoms", []) if isinstance(g, SPolygon)])


__all__ = [
    "rotate", "arc_points", "to_shapely", "from_shapely", "clean_polygon", "track_shape",
    "track_centerline", "pad_shape", "polygonize_outline", "multipolygon", "shapely",
]
