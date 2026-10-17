"""Rasterise shapely polygons into cell area coverage and cell face apertures.

A numba scanline routine finds each polygon's even-odd interior spans on a set
of horizontal lines; spans from all polygons are unioned per line and clipped
analytically to cell edges, so coverage is exact along x and sampled finely
along y. The same machinery on lines through the cell corners (and on the
transposed geometry) gives the exact open fraction of every cell face, used by
the cut-cell conduction scheme. Tiling keeps peak memory bounded.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import shapely
import shapely.ops
from numba import njit
from shapely.strtree import STRtree

@dataclass(frozen=True)
class Grid:
    """Regular grid of square cells. Row i spans y, column j spans x (KiCad frame, mm)."""
    x0: float
    y0: float
    dx: float
    nx: int
    ny: int

    def centers(self):
        """The cell centres' x (per column) and y (per row)."""
        xs = self.x0 + (np.arange(self.nx) + 0.5) * self.dx
        ys = self.y0 + (np.arange(self.ny) + 0.5) * self.dx
        return xs, ys

    def cell_of(self, x, y):
        """(row, column) of the cell holding each point (may be outside the grid: see inside())."""
        j = np.floor((np.asarray(x) - self.x0) / self.dx).astype(np.int64)
        i = np.floor((np.asarray(y) - self.y0) / self.dx).astype(np.int64)
        return i, j

    def inside(self, i, j):
        """Which (row, column) pairs are on the grid."""
        return (i >= 0) & (i < self.ny) & (j >= 0) & (j < self.nx)

    @property
    def extent(self):
        """(x0, y0, x1, y1) of the whole grid."""
        return self.x0, self.y0, self.x0 + self.nx * self.dx, self.y0 + self.ny * self.dx


def explode(geoms) -> list:
    """The non-empty polygons of the geometries (multi-polygons and collections split up, lines and points
    dropped)."""
    out = []
    for g in geoms:
        if g is None or g.is_empty:
            continue
        t = g.geom_type
        if t == "Polygon":
            out.append(g)
        elif t in ("MultiPolygon", "GeometryCollection"):
            out.extend(p for p in g.geoms if p.geom_type == "Polygon" and not p.is_empty)
    return out


@njit(cache=True, nogil=True)
def _spans(ex0, ey0, ex1, ey1, K, off, n_lines, width, out_line, out_a, out_b, count):
    """Append the even-odd interior spans of one polygon on each scanline.

    Coordinates are in cell units; scanline s sits at y = (s + off) / K.
    Returns the new span count, or -1 if the output buffers are full.
    """
    # Classic scanline fill: on each line, the x where it crosses the polygon's edges (half-open in y, so a
    # vertex on the line is counted once), sorted; pairs of crossings bound the interior (even-odd rule, which
    # also leaves holes out since their rings are among the edges). Spans are clipped to the tile [0, width].
    ne = ex0.shape[0]
    ymin = min(ey0.min(), ey1.min())
    ymax = max(ey0.max(), ey1.max())
    s0 = max(0, int(np.floor(ymin * K - off)))
    s1 = min(n_lines - 1, int(np.ceil(ymax * K - off)))
    xs = np.empty(ne, np.float64)
    cap = out_line.shape[0]
    for s in range(s0, s1 + 1):
        yc = (s + off) / K
        n = 0
        for e in range(ne):
            ya, yb = ey0[e], ey1[e]
            if (ya <= yc < yb) or (yb <= yc < ya):
                xs[n] = ex0[e] + (yc - ya) * (ex1[e] - ex0[e]) / (yb - ya)
                n += 1
        if n < 2:
            continue
        srt = np.sort(xs[:n])
        for k in range(0, n - 1, 2):
            a = max(srt[k], 0.0)
            b = min(srt[k + 1], width)
            if b <= a:
                continue
            if count >= cap:
                return -1
            out_line[count] = s
            out_a[count] = a
            out_b[count] = b
            count += 1
    return count


@njit(cache=True, nogil=True)
def _accumulate(lines, a, b, K, acc):
    """Union overlapping spans per scanline (input sorted by line, then a) and add exact overlap."""
    n = lines.shape[0]
    w = 1.0 / K
    i = 0
    while i < n:
        line = lines[i]
        ca, cb = a[i], b[i]
        i += 1
        while i < n and lines[i] == line and a[i] <= cb:
            if b[i] > cb:
                cb = b[i]
            i += 1
        # the merged span [ca, cb] adds its exact overlap with each cell it crosses, weighted by the scanline's
        # share 1/K of the row's height
        row = line // K
        c0 = int(np.floor(ca))
        c1 = int(np.floor(cb))
        if c1 >= acc.shape[1]:
            c1 = acc.shape[1] - 1
        for c in range(c0, c1 + 1):
            lo = max(ca, float(c))
            hi = min(cb, float(c + 1))
            if hi > lo:
                acc[row, c] += (hi - lo) * w


def _edges(poly, x0: float, y0: float, dx: float):
    """A polygon's edges (exterior and holes) as start and end coordinate arrays in cell units relative to (x0,
    y0), contiguous for numba."""
    rings = [np.asarray(poly.exterior.coords)] + [np.asarray(r.coords) for r in poly.interiors]
    a = np.concatenate([r[:-1, :2] for r in rings])
    b = np.concatenate([r[1:, :2] for r in rings])
    a = (a - (x0, y0)) / dx
    b = (b - (x0, y0)) / dx
    return (np.ascontiguousarray(a[:, 0]), np.ascontiguousarray(a[:, 1]),
            np.ascontiguousarray(b[:, 0]), np.ascontiguousarray(b[:, 1]))


def coverage(geoms, grid: Grid, supersample: int = 12, cancel=None) -> np.ndarray:
    """Fraction of each cell's area covered by the union of ``geoms`` (float32).

    Coverage along x is exact (spans are clipped analytically to cell edges and
    unioned per scanline); along y it is sampled on ``supersample`` scanlines
    per cell row.
    """
    return _raster(explode(geoms), grid, max(1, int(supersample)), 0.5, grid.ny, cancel)


def face_coverage(geoms, grid: Grid, cancel=None) -> tuple[np.ndarray, np.ndarray]:
    """Open fraction of every cell face (exact).

    Returns ``(fx, fy)``: ``fx[i, j]`` is the fraction of the vertical face at
    x = x0 + j dx spanning row i (shape ny x nx+1); ``fy[i, j]`` the horizontal
    face at y = y0 + i dx spanning column j (shape ny+1 x nx).
    """
    # A horizontal face is a line at y = y0 + i dx: one scanline exactly on it (offset 0) gives its exact open
    # length. Vertical faces are horizontal faces of the geometry mirrored about y = x (x and y swapped), on the
    # transposed grid.
    polys = explode(geoms)
    fy = _raster(polys, grid, 1, 0.0, grid.ny + 1, cancel)
    swap = [shapely.ops.transform(lambda x, y, z=None: (y, x), p) for p in polys]
    gt = Grid(grid.y0, grid.x0, grid.dx, grid.ny, grid.nx)
    fx = _raster(swap, gt, 1, 0.0, grid.nx + 1, cancel).T
    return np.ascontiguousarray(fx), fy


def _raster(polys, grid: Grid, K: int, off: float, n_rows: int, cancel) -> np.ndarray:
    """The covered fraction per cell (or per face line, K=1 off=0), tile by tile (512 x 512 cells): the polygons
    touching a tile come from an STRtree query, their spans are collected (the buffers doubled when full), sorted
    by line and start, then merged and accumulated (_accumulate). A tile's memory stays bounded whatever the
    board size."""
    out = np.zeros((n_rows, grid.nx), np.float32)
    if not polys:
        return out
    tree = STRtree(polys)
    tile = 512
    for ti in range(0, n_rows, tile):
        for tj in range(0, grid.nx, tile):
            if cancel is not None:
                cancel()
            h = min(tile, n_rows - ti)
            w = min(tile, grid.nx - tj)
            tx0 = grid.x0 + tj * grid.dx
            ty0 = grid.y0 + ti * grid.dx
            idx = tree.query(shapely.box(tx0, ty0 - grid.dx, tx0 + w * grid.dx, ty0 + h * grid.dx))
            if len(idx) == 0:
                continue
            cap = max(4096, 8 * h * K)
            line = np.empty(cap, np.int64)
            sa = np.empty(cap, np.float64)
            sb = np.empty(cap, np.float64)
            count = 0
            for k in idx:
                edges = _edges(polys[k], tx0, ty0, grid.dx)
                while True:
                    c = _spans(*edges, K, off, h * K, float(w), line, sa, sb, count)
                    if c >= 0:
                        count = c
                        break
                    cap *= 2
                    line = np.resize(line, cap)
                    sa = np.resize(sa, cap)
                    sb = np.resize(sb, cap)
            if count == 0:
                continue
            order = np.lexsort((sa[:count], line[:count]))
            acc = np.zeros((h, w), np.float64)
            _accumulate(line[:count][order], sa[:count][order], sb[:count][order], K, acc)
            out[ti:ti + h, tj:tj + w] = np.minimum(acc, 1.0)
    return out


def cells_in(geom, grid: Grid) -> tuple[np.ndarray, np.ndarray]:
    """Cells whose centre lies inside ``geom``; falls back to the cell under its centroid."""
    x0, y0, x1, y1 = geom.bounds
    j0 = max(0, int(np.floor((x0 - grid.x0) / grid.dx)))
    j1 = min(grid.nx - 1, int(np.floor((x1 - grid.x0) / grid.dx)))
    i0 = max(0, int(np.floor((y0 - grid.y0) / grid.dx)))
    i1 = min(grid.ny - 1, int(np.floor((y1 - grid.y0) / grid.dx)))
    if j1 >= j0 and i1 >= i0:
        jj, ii = np.meshgrid(np.arange(j0, j1 + 1), np.arange(i0, i1 + 1))
        xs = grid.x0 + (jj + 0.5) * grid.dx
        ys = grid.y0 + (ii + 0.5) * grid.dx
        m = shapely.contains_xy(geom, xs, ys)
        if m.any():
            return ii[m], jj[m]
    c = geom.representative_point()
    i, j = grid.cell_of(c.x, c.y)
    i = int(np.clip(i, 0, grid.ny - 1))
    j = int(np.clip(j, 0, grid.nx - 1))
    return np.array([i]), np.array([j])
