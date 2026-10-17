"""A contour map of a heatmap layer for drawing on the board, like the height lines of a topographic map.

KiCad draws a User layer in one colour, so the lines carry their values as text instead of colours:

- Levels at round values. A current density map (log scale on the heatmap) gets the 1-2-5 steps over the
  heatmap's three decades below the peak (500, 200, 100, 50, ... A/m); a temperature map gets even steps in °C
  (every 2 °C, say). Every decade (1, 10, 100 ...), or every fifth step in °C, is an index line: drawn thicker.
- Isolines (marching squares on the solved grid, contourpy) rather than filled regions: a filled region's outline
  also runs along every copper edge, once per level, which buried the map in parallel lines. The field is masked
  off the copper, so lines stop at the copper's edge.
- The lines are smoothed (Chaikin corner cutting: the polyline of marching squares has a corner in every cell; the
  smoothing moves a line by at most a quarter of a cell) and simplified to a small tolerance, within a budget of
  graphic segments for KiCad.
- Each line long enough carries its value, written along the line and upright, at regular distances; the line is
  broken where its label sits, as on a map. Labels that would overlap one already placed are moved along their
  line or left out.

Everything is in board mm, y down (KiCad's).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

LOG_DECADES = 3              # as the heatmap's log scale: levels down to peak / 1000
LINEAR_LINES = 10            # about this many isotherms on a temperature map
MAX_SEGMENTS = 8000          # graphic segments drawn in KiCad (the arrows draw up to 4500 and stay quick)
MIN_TEXT_MM, MAX_TEXT_MM = 0.4, 1.2       # label height: a 140th of the copper diagonal, within these


@dataclass
class ContourMap:
    """What to draw: `lines` [(value, index line?, [polyline (x, y) ...])] with gaps where labels sit, `labels`
    [(x, y, angle in degrees counter-clockwise as seen on the board, text)] centred on the line, the label text
    height, and a `legend` line (what the values are, the step, the peak) with where it goes."""
    lines: list = field(default_factory=list)
    labels: list = field(default_factory=list)
    text_mm: float = 0.5
    legend: str = ""
    legend_at: tuple = (0.0, 0.0)
    levels: list = field(default_factory=list)

    def segment_count(self) -> int:
        """The graphic segments the lines need (one per pair of neighbouring points)."""
        return sum(max(len(pts) - 1, 0) for _, _, pts in self.lines)


def short_value(v: float) -> str:
    """A contour value as short as a map's height label: 3 significant digits, an SI prefix past 1000 or below 1
    (2k, 500, 20, 5, 0.5 -> 500m would be confusing for A/m, so plain decimals down to 0.001)."""
    a = abs(v)
    if a >= 1e6:
        return f"{v / 1e6:.3g}M"
    if a >= 1e3:
        return f"{v / 1e3:.3g}k"
    if a >= 1e-3 or a == 0:
        return f"{v:.3g}"
    return f"{v:.2e}"


def log_levels(peak: float, decades: int = LOG_DECADES) -> list[tuple[float, bool]]:
    """[(value, index line?)] at 1-2-5 steps from just below `peak` down to peak / 10^decades; the 1, 10, 100 ...
    are index lines."""
    if peak <= 0:
        return []
    low = peak / 10 ** decades
    out = []
    for e in range(int(math.floor(math.log10(low))), int(math.ceil(math.log10(peak))) + 1):
        for m in (1, 2, 5):
            v = m * 10.0 ** e
            if low <= v < peak * 0.999:
                out.append((v, m == 1))
    return sorted(out, reverse=True)


def nice_step(span: float, lines: int = LINEAR_LINES) -> float:
    """The round step (1, 2 or 5 × 10^n) nearest (by ratio) to giving `lines` levels over `span`."""
    raw = span / max(lines, 1)
    e = 10 ** math.floor(math.log10(raw))
    return min((m * e for m in (1, 2, 5, 10)), key=lambda st: abs(math.log(st / raw)))


def linear_levels(low: float, peak: float, lines: int = LINEAR_LINES) -> tuple[list[tuple[float, bool]], float]:
    """([(value, index line?)], step): round steps strictly between `low` and `peak`; every fifth step (counted from
    0, so 10 °C, 20 °C with a 2 °C step) is an index line."""
    if peak <= low:
        return [], 0.0
    step = nice_step(peak - low, lines)
    out = []
    k = math.floor(low / step) + 1
    while k * step < peak * 0.9999:
        v = round(k * step, 10)
        out.append((v, k % 5 == 0))
        k += 1
    return out, step


def _chaikin(pts: np.ndarray, closed: bool, rounds: int = 2) -> np.ndarray:
    """Corner cutting: each segment is replaced by points at 1/4 and 3/4 of it (an open line keeps its ends)."""
    for _ in range(rounds):
        if len(pts) < 3:
            return pts
        a = pts if not closed else np.vstack([pts, pts[:1]])
        q = 0.75 * a[:-1] + 0.25 * a[1:]
        r = 0.25 * a[:-1] + 0.75 * a[1:]
        mid = np.empty((2 * len(q), 2))
        mid[0::2], mid[1::2] = q, r
        pts = mid if closed else np.vstack([a[:1], mid, a[-1:]])
    return np.vstack([pts, pts[:1]]) if closed else pts


def _simplify(pts: np.ndarray, tol: float) -> np.ndarray:
    """The polyline with points dropped while it stays within `tol` (Douglas-Peucker, shapely)."""
    from shapely.geometry import LineString
    if len(pts) < 3 or tol <= 0:
        return pts
    return np.asarray(LineString(pts).simplify(tol, preserve_topology=False).coords)


def _iso_lines(grid, Z: np.ndarray, levels: list[tuple[float, bool]]) -> list[tuple[float, bool, np.ndarray, bool]]:
    """[(value, index, points, closed)]: the raw marching-squares isolines of Z (indexed [x, y], masked off the
    copper) at each level, smoothed."""
    import contourpy
    xs = grid.x0 + np.arange(grid.nx) * grid.d
    ys = grid.y0 + np.arange(grid.ny) * grid.d
    Zm = np.ma.masked_array(Z.T, mask=~np.asarray(grid.node).T)         # contourpy wants [y, x]
    gen = contourpy.contour_generator(xs, ys, Zm, line_type=contourpy.LineType.Separate)
    out = []
    for value, index in levels:
        for pts in gen.lines(value):
            pts = np.asarray(pts, float)
            if len(pts) < 3:
                continue
            closed = bool(np.allclose(pts[0], pts[-1]))
            if closed:
                pts = pts[:-1]
            out.append((value, index, _chaikin(pts, closed), closed))
    return out


def _length(pts: np.ndarray) -> np.ndarray:
    """Cumulative arc length at each point of a polyline (0 at the first)."""
    return np.concatenate([[0.0], np.cumsum(np.hypot(*np.diff(pts, axis=0).T))])


def _at(pts: np.ndarray, s: np.ndarray, where: float) -> np.ndarray:
    """The point at arc length `where` along the polyline."""
    return np.array([np.interp(where, s, pts[:, 0]), np.interp(where, s, pts[:, 1])])


def _cut(pts: np.ndarray, s: np.ndarray, gaps: list[tuple[float, float]]) -> list[np.ndarray]:
    """The polyline without the arc-length intervals in `gaps` (the labels' places): its remaining pieces."""
    pieces, start = [], 0.0
    for a, b in sorted(gaps) + [(s[-1], s[-1])]:
        if a > start:
            inside = (s > start) & (s < a)
            piece = np.vstack([_at(pts, s, start), pts[inside], _at(pts, s, a)])
            if len(piece) >= 2 and _length(piece)[-1] > 1e-6:
                pieces.append(piece)
        start = max(start, b)
    return pieces


def _place_labels(lines, text_mm: float, spacing: float) -> tuple[list, list]:
    """Label the lines along their length: ([(value, index, piece)], [(x, y, angle, text)]). A label goes where its
    stretch of line is nearly straight (its chord at least 90 % of its arc) and clear of the labels already placed;
    the first one at half the spacing from the line's start (or the middle of a short closed line), then every
    `spacing` mm."""
    placed: list[tuple[float, float, float]] = []          # (x, y, half width) of every label so far
    out_lines, labels = [], []
    # index lines first, so they get their labels when space is short
    for value, index, pts, closed in sorted(lines, key=lambda l: (not l[1], -_length(l[2])[-1])):
        text = short_value(value)
        w = (0.75 * len(text) + 0.6) * text_mm           # the label's width with a little gap on each side
        s = _length(pts)
        total = s[-1]
        gaps = []
        if total >= 2.5 * w:
            first = total / 2 if total < spacing else spacing / 2
            where = first
            while where + w / 2 <= total - (0 if closed else 0.5 * w):
                ok = False
                for shift in (0.0, 0.5 * w, -0.5 * w, w, -w, 2 * w, -2 * w):   # a little along the line if needed
                    c = where + shift
                    a, b = c - w / 2, c + w / 2
                    if a < (0 if closed else 0.5 * w) or b > total - (0 if closed else 0.5 * w):
                        continue
                    p, q = _at(pts, s, a), _at(pts, s, b)
                    if math.dist(p, q) < 0.9 * w:                       # too curved here
                        continue
                    mid = _at(pts, s, c)
                    if any(math.dist(mid, (x, y)) < hw + w / 2 + text_mm for x, y, hw in placed):
                        continue
                    # angle as seen on the board (y down): counter-clockwise from +x, kept upright
                    ang = math.degrees(math.atan2(-(q[1] - p[1]), q[0] - p[0]))
                    if ang > 90:
                        ang -= 180
                    elif ang <= -90:
                        ang += 180
                    labels.append((float(mid[0]), float(mid[1]), ang, text))
                    placed.append((float(mid[0]), float(mid[1]), w / 2))
                    gaps.append((a, b))
                    ok = True
                    break
                where += spacing if ok else spacing / 2
        if closed:                                          # a closed line's pieces go round through its start
            pts2 = np.vstack([pts, pts[:1]])
            s2 = _length(pts2)
            pieces = _cut(pts2, s2, gaps) if gaps else [pts2]
        else:
            pieces = _cut(pts, s, gaps) if gaps else [pts]
        out_lines += [(value, index, p) for p in pieces]
    return out_lines, labels


def contour_map(grid, Z: np.ndarray, peak: float | None = None, scale: str = "log", low: float | None = None,
                unit: str = "", what: str = "", max_segments: int = MAX_SEGMENTS) -> ContourMap:
    """The contour map of Z (a layer's |K| or temperature, indexed [x, y]) on `grid`, see the module. `peak`: the
    heatmap's peak over all of the result's layers (so every layer gets the same levels; default: this layer's).
    scale "log": 1-2-5 levels over three decades; "linear": round steps from `low` (the ambient temperature) up.
    `unit` and `what` go into the legend."""
    node = np.asarray(grid.node)
    Zc = np.where(node, Z, np.nan)
    here = float(np.nanmax(Zc)) if np.any(node) else 0.0
    peak = here if peak is None else peak
    out = ContourMap()
    if not np.isfinite(here) or peak <= (low or 0.0):
        return out
    if scale == "linear":
        levels, step = linear_levels(low if low is not None else float(np.nanmin(Zc)), peak)
        steps = f"every {short_value(step)} {unit}, bold every {short_value(5 * step)} {unit}".replace("  ", " ")
    else:
        levels = log_levels(peak)
        steps = "1-2-5 steps over three decades, bold at 1, 10, 100 …"
    levels = [(v, i) for v, i in levels if v < here]           # none above this layer's own peak
    out.levels = [v for v, _ in levels]
    if not levels:
        return out
    xs = np.nonzero(node.any(axis=1))[0]
    ys = np.nonzero(node.any(axis=0))[0]
    cx0, cx1 = grid.x0 + xs.min() * grid.d, grid.x0 + xs.max() * grid.d
    cy0, cy1 = grid.y0 + ys.min() * grid.d, grid.y0 + ys.max() * grid.d
    out.text_mm = text = float(np.clip(math.hypot(cx1 - cx0, cy1 - cy0) / 140, MIN_TEXT_MM, MAX_TEXT_MM))
    raw = _iso_lines(grid, np.nan_to_num(Zc, nan=0.0), levels)
    # simplify within the segment budget: from a twentieth of a cell up, until the lines fit
    tol = grid.d * 0.05
    while True:
        lines = [(v, i, _simplify(p if not c else np.vstack([p, p[:1]]), tol)[: (None if not c else -1)], c)
                 for v, i, p, c in raw]
        lines = [(v, i, p, c) for v, i, p, c in lines if len(p) >= 2 and _length(p)[-1] >= 2 * grid.d]
        if sum(len(p) for _, _, p, _ in lines) <= max_segments or tol > 4 * grid.d:
            break
        tol *= 1.6
    out.lines, out.labels = _place_labels(lines, text, spacing=40 * text)
    out.legend = (f"{what}: {steps}; peak {short_value(peak)}{(' ' + unit) if unit else ''}"
                  if what else steps)
    out.legend_at = (cx0, cy0 - 2.5 * text)
    return out
