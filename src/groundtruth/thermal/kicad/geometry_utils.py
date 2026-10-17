"""Small geometric helpers shared by the KiCad reader."""

from __future__ import annotations

import math

# Maximum chord deviation used when tessellating arcs (mm).
ARC_TOL = 0.005


def rotate(x: float, y: float, deg: float) -> tuple[float, float]:
    """Rotate a point the way KiCad does (y axis down, +angle = CCW on screen)."""
    if deg == 0:
        return x, y
    a = math.radians(deg)
    c, s = math.cos(a), math.sin(a)
    return x * c + y * s, -x * s + y * c


def _segments_for(radius: float, sweep: float) -> int:
    """How many chords an arc needs so none deviates from it by more than ARC_TOL: a chord spanning angle a sags
    r (1 - cos(a/2)) (the sagitta), so the largest step is a = 2 acos(1 - ARC_TOL / r)."""
    if radius <= 0:
        return 1
    step = 2 * math.acos(max(-1.0, min(1.0, 1 - ARC_TOL / radius))) if radius > ARC_TOL else math.pi / 2
    return max(2, int(math.ceil(abs(sweep) / max(step, 1e-3))))


def circle_center(p1, p2, p3):
    """The centre of the circle through three points (the circumcentre, from the determinant formula); None when
    they are on a line."""
    ax, ay = p1
    bx, by = p2
    cx, cy = p3
    d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-12:
        return None
    ux = ((ax * ax + ay * ay) * (by - cy) + (bx * bx + by * by) * (cy - ay) + (cx * cx + cy * cy) * (ay - by)) / d
    uy = ((ax * ax + ay * ay) * (cx - bx) + (bx * bx + by * by) * (ax - cx) + (cx * cx + cy * cy) * (bx - ax)) / d
    return ux, uy


def arc_points(start, mid, end) -> list[tuple[float, float]]:
    """Tessellate a three-point arc into a polyline including both end points."""
    c = circle_center(start, mid, end)
    if c is None:
        return [start, end]
    cx, cy = c
    r = math.hypot(start[0] - cx, start[1] - cy)
    a0 = math.atan2(start[1] - cy, start[0] - cx)
    am = math.atan2(mid[1] - cy, mid[0] - cx)
    a1 = math.atan2(end[1] - cy, end[0] - cx)

    def norm(a):
        """An angle in [0, 2 pi)."""
        return a % (2 * math.pi)

    # Pick the sweep direction that passes through the mid point.
    ccw = norm(a1 - a0)
    if norm(am - a0) <= ccw:
        sweep = ccw
    else:
        sweep = ccw - 2 * math.pi
    n = _segments_for(r, sweep)
    return [(cx + r * math.cos(a0 + sweep * i / n), cy + r * math.sin(a0 + sweep * i / n)) for i in range(n + 1)]


def circle_points(cx: float, cy: float, r: float) -> list[tuple[float, float]]:
    """A circle as a closed polygon's points (at least 16, within ARC_TOL)."""
    n = max(16, _segments_for(r, 2 * math.pi))
    return [(cx + r * math.cos(2 * math.pi * i / n), cy + r * math.sin(2 * math.pi * i / n)) for i in range(n)]
