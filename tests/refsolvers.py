"""Independent reference solvers for the track-width checks (VALIDATION_AUDIT.md round 5).

Nothing here imports GroundTruth: these are separate finite-difference discretisations [Patankar 1980] written for
the audit, so a test that compares GroundTruth with them compares two methods, not one method with itself.

- sheet_resistance: DC resistance of planar copper (a sheet of thickness t) between two electrodes, by a
  cell-centred finite-difference solve of div(σ t grad V) = 0 on a fine uniform grid with a cell-coverage mask.
- cross_section_rise: steady temperature of a long straight track on a board, as the 2D heat conduction of its
  cross-section (anisotropic laminate, the copper track on top, convection on both faces and the edges).
"""
from __future__ import annotations

import math

import numpy as np
import scipy.sparse as sp
import shapely
from scipy.sparse.linalg import spsolve


def _coverage(poly, x0: float, y0: float, h: float, nx: int, ny: int, k: int = 4) -> np.ndarray:
    """Fraction of each h x h cell covered by `poly`, from k x k point samples per cell."""
    xs = x0 + (np.arange(nx * k) + 0.5) * h / k
    ys = y0 + (np.arange(ny * k) + 0.5) * h / k
    X, Y = np.meshgrid(xs, ys, indexing="ij")
    inside = shapely.contains_xy(poly, X, Y)
    return inside.reshape(nx, k, ny, k).mean(axis=(1, 3))


def sheet_resistance(poly, electrode_a, electrode_b, h: float, sigma: float, t: float) -> float:
    """Resistance (Ω) of the copper `poly` (mm, a shapely polygon) of thickness t (m) and conductivity sigma (S/m)
    between two electrodes (shapely polygons, mm) held at 1 V and 0 V, on cells of h mm.

    Cells are copper in proportion to their coverage (a conductance of σt times the harmonic mean of the two cells'
    coverage per face, the usual finite-volume face average [Patankar 1980]); a cell more than half inside an
    electrode is fixed at its potential. R = 1 V / the current out of electrode A. The discretisation error falls as
    h → 0: a test compares two h to bound it."""
    x0, y0, x1, y1 = poly.bounds
    nx, ny = int(math.ceil((x1 - x0) / h)) + 2, int(math.ceil((y1 - y0) / h)) + 2
    x0, y0 = x0 - h, y0 - h
    c = _coverage(poly, x0, y0, h, nx, ny)
    fa = _coverage(electrode_a, x0, y0, h, nx, ny) > 0.5
    fb = _coverage(electrode_b, x0, y0, h, nx, ny) > 0.5
    on = c > 1e-9
    idx = -np.ones((nx, ny), np.int64)
    free = on & ~fa & ~fb
    idx[free] = np.arange(int(free.sum()))
    n = int(free.sum())
    g0 = sigma * t                                     # S per square
    rows, cols, vals = [], [], []
    rhs = np.zeros(n)
    diag = np.zeros(n)
    for di, dj in ((1, 0), (0, 1)):
        a = (slice(0, nx - di), slice(0, ny - dj))
        b = (slice(di, nx), slice(dj, ny))
        ca, cb = c[a], c[b]
        g = np.where((ca > 0) & (cb > 0), g0 * 2 * ca * cb / np.maximum(ca + cb, 1e-30), 0.0)
        ia, ib = idx[a], idx[b]
        va = np.where(fa[a], 1.0, 0.0)
        vb = np.where(fa[b], 1.0, 0.0)
        # free - free
        m = (ia >= 0) & (ib >= 0) & (g > 0)
        rows += [ia[m], ib[m]]
        cols += [ib[m], ia[m]]
        vals += [-g[m], -g[m]]
        np.add.at(diag, ia[m], g[m])
        np.add.at(diag, ib[m], g[m])
        # free - fixed
        m = (ia >= 0) & (ib < 0) & (g > 0) & (fa[b] | fb[b])
        np.add.at(diag, ia[m], g[m])
        np.add.at(rhs, ia[m], g[m] * vb[m])
        m = (ib >= 0) & (ia < 0) & (g > 0) & (fa[a] | fb[a])
        np.add.at(diag, ib[m], g[m])
        np.add.at(rhs, ib[m], g[m] * va[m])
    A = sp.coo_matrix((np.concatenate(vals + [diag]), (np.concatenate(rows + [np.arange(n)]),
                                                      np.concatenate(cols + [np.arange(n)]))), shape=(n, n)).tocsr()
    v = spsolve(A, rhs)
    V = np.zeros((nx, ny))
    V[free] = v
    V[fa] = 1.0
    # the current leaving electrode A: over its faces to cells that are not in A
    cur = 0.0
    for di, dj in ((1, 0), (0, 1)):
        a = (slice(0, nx - di), slice(0, ny - dj))
        b = (slice(di, nx), slice(dj, ny))
        ca, cb = c[a], c[b]
        g = np.where((ca > 0) & (cb > 0), g0 * 2 * ca * cb / np.maximum(ca + cb, 1e-30), 0.0)
        cur += float(np.sum(np.where(fa[a] & ~fa[b], g * (1.0 - V[b]), 0.0)))
        cur += float(np.sum(np.where(fa[b] & ~fa[a], g * (1.0 - V[a]), 0.0)))
    return 1.0 / cur


def _graded(lo: float, hi: float, fine: float, coarse: float, focus: float) -> np.ndarray:
    """Cell edges from lo to hi: `fine` within ±focus of 0, growing by 1.15 per cell to at most `coarse` beyond."""
    edges = [0.0]
    x, step = 0.0, fine
    while x < hi:
        x = min(x + step, hi)
        edges.append(x)
        if x > focus:
            step = min(step * 1.15, coarse)
    pos = np.array(edges)
    return np.concatenate([-pos[::-1][:-1], pos]) if lo < 0 else pos


def cross_section_rise(width_mm: float, q_w_per_m: float, board_w_mm: float = 60.0, board_t_mm: float = 1.51,
                       cu_t_mm: float = 0.035, k_xy: float = 0.81, k_z: float = 0.29, k_cu: float = 390.0,
                       k_fill: float = 0.3, h_top: float = 10.0, h_bottom: float = 10.0) -> float:
    """The temperature rise (K) of a long straight track `width_mm` wide on the top of a board, heated by
    q_w_per_m (W per metre of track), from the 2D steady conduction of the cross-section: a laminate board_t_mm
    thick (k_xy across, k_z through), a copper layer cu_t_mm thick on each face (copper under the track, `k_fill`
    elsewhere), convection h_top above and h_bottom below the whole width and the mean of the two on the board's
    edges. The track's mean temperature over its copper."""
    w = width_mm * 1e-3
    yb = _graded(-board_w_mm / 2, board_w_mm / 2, min(width_mm / 8, 0.01), 0.5, max(2 * width_mm, 1.0)) * 1e-3
    # through the stack: bottom copper slab, the laminate in cells from a sixteenth of the track's width under the
    # top face (where the heat spreads out of the track) growing to at most 0.05 mm, the top copper slab in two
    lam = _graded(0.0, board_t_mm, min(width_mm / 16, 0.01), 0.05, 0.0)
    lam = (board_t_mm - lam[::-1]) * 1e-3          # fine at the top of the laminate
    zc = [0.0, cu_t_mm * 1e-3]
    zc += list(cu_t_mm * 1e-3 + lam[1:])
    zc += [zc[-1] + cu_t_mm * 1e-3 / 2, zc[-1] + cu_t_mm * 1e-3]
    zb = np.array(zc)
    ny, nz = len(yb) - 1, len(zb) - 1
    yc, dy = 0.5 * (yb[:-1] + yb[1:]), np.diff(yb)
    dz = np.diff(zb)
    top_cu = zb[:-1] >= zb[-1] - cu_t_mm * 1e-3 - 1e-12
    bot_cu = zb[1:] <= cu_t_mm * 1e-3 + 1e-12
    kx = np.zeros((ny, nz))
    kz = np.zeros((ny, nz))
    lam = ~top_cu & ~bot_cu
    kx[:, lam], kz[:, lam] = k_xy, k_z
    # copper fraction of each top cell under the track (exact overlap of the cell with |y| <= w/2)
    frac = np.clip((np.minimum(yb[1:], w / 2) - np.maximum(yb[:-1], -w / 2)) / dy, 0.0, 1.0)
    for s in np.nonzero(top_cu)[0]:
        kx[:, s] = frac * k_cu + (1 - frac) * k_fill
        kz[:, s] = frac * k_cu + (1 - frac) * k_fill
    for s in np.nonzero(bot_cu)[0]:
        kx[:, s], kz[:, s] = k_fill, k_fill
    n = ny * nz
    idx = np.arange(n).reshape(ny, nz)
    rows, cols, vals = [], [], []
    diag = np.zeros(n)
    # conductances per metre of track between neighbouring cells: half-cell resistances in series
    gy = 1.0 / (dy[:-1, None] / (2 * kx[:-1, :] * dz[None, :]) + dy[1:, None] / (2 * kx[1:, :] * dz[None, :]))
    gz = 1.0 / (dz[None, :-1] / (2 * kz[:, :-1] * dy[:, None]) + dz[None, 1:] / (2 * kz[:, 1:] * dy[:, None]))
    for g, a, b in ((gy, idx[:-1, :], idx[1:, :]), (gz, idx[:, :-1], idx[:, 1:])):
        rows += [a.ravel(), b.ravel()]
        cols += [b.ravel(), a.ravel()]
        vals += [-g.ravel(), -g.ravel()]
        np.add.at(diag, a.ravel(), g.ravel())
        np.add.at(diag, b.ravel(), g.ravel())
    # convection: top and bottom faces (half-cell conduction in series with h), the two edges at the mean h
    gt = 1.0 / (1.0 / (h_top * dy) + dz[-1] / (2 * kz[:, -1] * dy))
    gb = 1.0 / (1.0 / (h_bottom * dy) + dz[0] / (2 * kz[:, 0] * dy))
    np.add.at(diag, idx[:, -1], gt)
    np.add.at(diag, idx[:, 0], gb)
    h_e = 0.5 * (h_top + h_bottom)
    for j in (0, ny - 1):
        ge = 1.0 / (1.0 / (h_e * dz) + dy[j] / (2 * kx[j, :] * dz))
        np.add.at(diag, idx[j, :], ge)
    # the heat, spread over the track's copper in proportion to each cell's copper cross-section
    q = np.zeros((ny, nz))
    s_top = np.nonzero(top_cu)[0]
    share = frac[:, None] * dy[:, None] * dz[None, s_top]
    q[:, s_top] = q_w_per_m * share / share.sum()
    A = sp.coo_matrix((np.concatenate(vals + [diag]), (np.concatenate(rows + [np.arange(n)]),
                                                      np.concatenate(cols + [np.arange(n)]))), shape=(n, n)).tocsr()
    T = spsolve(A, q.ravel()).reshape(ny, nz)
    return float(np.sum(T[:, s_top] * share) / share.sum())
