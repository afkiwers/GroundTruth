"""Quasi-static PEEC solver for return currents in a stack of copper layers.

PEEC [Ruehli 1974] with partial inductances [Ruehli 1972] in modified nodal form [Ho 1975]; quasi-static [A1].
Source keys: docs/REFERENCES.md.

Every plane layer is rasterised on the same xy grid. Unknowns are the currents in x/y strips between
neighbouring nodes of each layer, displacement currents between facing planes, currents in vertical and
lumped branches (stitching vias, through-hole pads, decaps, 0R, ferrites), plus all node potentials:

    Z·I - A·V = -jω·ψ        (KVL per branch; ψ = flux coupled from the known signal current)
    Aᵀ·I      = J            (KCL; +1 A injected at the receiver return node, -1 A at the driver's)

Z = R_s(f) + jω·L_p for the strips (R_s with the skin depth [Ramo 1994] [A2]), where L_p holds all mutual
partial inductances between parallel strips on every pair of layers. On a uniform grid each layer pair's L_p is a
convolution kernel that only depends on the grid offset and the layer distance Δz, so L_p·I is evaluated with FFTs
and never formed. Plane-pair capacitance (C = ε0·εr·d²/t per cell, from the stackup, plus dielectric loss ωC·tanδ
[Pozar 2012]) and the vertical/lumped branches are diagonal in Z.

Two objects split the work:
  * PlaneStack - everything that does not depend on the signal route: topology, kernels, and a cache of
    per-frequency operators (preconditioner factorisations). All routes on a board, and every fix candidate,
    share it. A candidate via is `stack.with_branches([...])`, solved with the base factorisation plus a
    Woodbury low-rank update [Woodbury 1950] instead of a new factorisation.
  * RouteExcitation - one signal's loop: ψ, injection nodes, trace self-inductance.

Solver: right-preconditioned GMRES [Saad 1986] [Saad 2003] (block classical Gram-Schmidt via BLAS, run twice
[Giraud 2005], so it stops on the true residual). Preconditioner: Z replaced by diag(Z), applied through the sparse
Schur complement Aᵀ·diag(Z)⁻¹·A of the saddle-point system [Benzi 2005] (SuperLU [Demmel 1999], MMD ordering
[Liu 1985]).
The L_p product is done in double precision: near plane resonances the system is ill-conditioned and the
~1e-7 error of a single-precision FFT product stalled GMRES (measured 10 000+ iterations vs ~20). With plane
capacitance, the "sheet" preconditioner keeps each position's cross-layer inductance block (the cavity mode);
for purely inductive stacks it gave no gain on real boards, so they use diag(Z).

Rasterisation (`build_grid`): a coarse grid must keep the copper's topology. Sampling only the node points lets
thin webs between rows of via antipads vanish, which cut real board GND pours into dozens of pieces that only
vias joined - spurious LC resonators whose frequencies moved with the grid. With `sub` > 1 the copper is
sampled on a sub-grid (d/sub), every cell keeps its largest copper piece, and two cells are joined when a
sub-grid strip of that copper crosses their common border.
"""
from __future__ import annotations

import atexit
import copy
import math
import queue
import threading
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Callable, Optional

import numpy as np
import scipy.sparse as sp
import shapely
from scipy.fft import fft2, ifft2, next_fast_len
from scipy.linalg import solve_triangular
from scipy.linalg.blas import zgemv
from scipy.sparse.csgraph import connected_components
from scipy.sparse.linalg import splu

from . import gpu
from .kernel import (GMD_FLAT, MU0, filament_mutual, leggauss, segment_mutual, segment_vector_potential, strip_kernel,
                     strip_self, strip_self_numeric)

SIGMA_CU = 5.8e7            # S/m, annealed copper at 20 °C [IEC 60028]
VIA_PLATING = 25e-6         # m, a via barrel's plating [A3]
EPS0 = 8.8541878128e-12
GMRES_MEMORY = 4e8          # bytes allowed for the Krylov basis (sets the restart length)
GMRES_FIRST_ROWS = 16       # basis rows allocated at first; doubled as the iterations need them (_gmres)
GMRES_EXTRA_CYCLES = 24     # restarts beyond max_cycles while the residual still falls ...
GMRES_PROGRESS = 0.7        # ... to at most this fraction per restart (stagnation stops at once)
OPERATOR_CACHE = 2          # per-frequency factorisations kept per stack (4 before: see ARCHITECTURE "Memory")
PRECOND_REUSE = 0.25        # reuse a factorisation as preconditioner within ±25 % (log ratio) of its frequency
BORROW_BUDGET = 1.5         # a borrowed preconditioner's GMRES budget: 1.5 x the iterations its source needed at
BORROW_EXTRA_ITS = 15       # home, plus 15
LU_PIVOT_THRESHOLD = 0.1    # SuperLU symmetric mode: take the diagonal pivot unless it is below 0.1 x the column's


def too_large_text(unknowns: int, error) -> str:
    """Why a model can't be factorised at all: past SuperLU's 32-bit limit even with diagonal pivots."""
    return (f"the model ({unknowns} unknowns) is too large for the sparse solver: its factorisation passes "
            "SuperLU's limit of about 2.1 billion entries (a 32-bit count, about 34 GB) even with diagonal pivots "
            f"only; lower the resolution (e.g. 25x to 16x) or use Sharper map on the views you need finer ({error})")


class SolverError(RuntimeError):
    """A model that cannot be solved: no copper where current must go, injections that don't balance, GMRES not
    converging. The caller reports it as a note or issue; it is not a bug."""
    pass


# --------------------------------------------------------------------------- threads
# Solves at different frequencies run in parallel threads (analysis.simulate.run_parallel). A PlaneStack is
# shared by them: its operator cache is locked, and each frequency is factorised by one thread only.
_tls = threading.local()


def fft_workers() -> int:
    """FFT threads for the current thread's solves: all cores, unless a parallel run set a share."""
    return getattr(_tls, "fft_workers", -1)


@contextmanager
def thread_budget(workers: int):
    """Limit this thread's FFTs to `workers` cores while several solves share the machine."""
    old = getattr(_tls, "fft_workers", -1)
    _tls.fft_workers = workers
    try:
        yield
    finally:
        _tls.fft_workers = old


USE_NUMBA = True                # the compiled kernel multiply-add (solver/_mac.py) when numba is installed
_MAC: list = []                 # [kernel or None] once looked up
_MAC_LOCK = threading.Lock()


def _kernel_mac() -> Optional[Callable]:
    """The compiled multiply-add, or None (numba missing or switched off: numpy does it). Compiled on first use
    (~1 s, cached on disk by numba after that)."""
    if not USE_NUMBA:
        return None
    if not _MAC:
        with _MAC_LOCK:
            if not _MAC:
                try:
                    from ._mac import kernel_mac
                except Exception:       # noqa: BLE001 - no numba, or one that does not load: numpy instead
                    kernel_mac = None
                _MAC.append(kernel_mac)
    return _MAC[0]


# --------------------------------------------------------------------------- grid
@dataclass
class Grid:
    """One layer's copper sampled on a square grid of pitch d (mm): which nodes are copper (and of which net),
    which neighbouring nodes are joined by a copper strip, and the tracks bridged across other nets' cells."""
    x0: float          # mm, node (0, 0)
    y0: float
    d: float           # mm pitch
    nx: int
    ny: int
    node: np.ndarray   # (nx, ny) bool, copper at node
    xb: np.ndarray     # (nx-1, ny) bool, x strip between (i,j) and (i+1,j)
    yb: np.ndarray     # (nx, ny-1) bool
    label: Optional[np.ndarray] = None   # (nx, ny) index into `nets`, -1 = no copper
    nets: list = field(default_factory=list)
    fill: Optional[np.ndarray] = None    # (nx, ny) copper fraction of each node's cell (None = full cells)
    bridges: list = field(default_factory=list)   # [(i0, j0, i1, j1, length mm, width mm)]: tracks across
                                                  # another net's cells (_rasterise_lines), lumped branches
    wx: Optional[np.ndarray] = None      # (nx-1, ny) copper width of each x strip as a fraction of d (None = full)
    wy: Optional[np.ndarray] = None      # (nx, ny-1)
    ox: Optional[np.ndarray] = None      # (nx-1, ny) where that copper's centre lies across the strip: offset from
    oy: Optional[np.ndarray] = None      # the cell centre (+y for x strips, +x for y strips), a fraction of d

    def xy(self, i, j):
        """Board coordinates (mm) of node (i, j) (arrays work too)."""
        return self.x0 + np.asarray(i) * self.d, self.y0 + np.asarray(j) * self.d

    def nearest_node(self, x: float, y: float, max_dist: Optional[float] = None,
                     net: Optional[str] = None) -> Optional[tuple[int, int]]:
        """The copper node nearest to (x, y) mm (of `net` only, when given), or None when there is none (within
        max_dist)."""
        mask = self.node
        if net is not None and self.label is not None:
            if net not in self.nets:
                return None
            mask = self.label == self.nets.index(net)
        # search a window first (fast), the whole grid only if needed
        r = max(int(math.ceil((max_dist or 0) / self.d)) + 1, 2) if max_dist is not None else None
        ci, cj = int(round((x - self.x0) / self.d)), int(round((y - self.y0) / self.d))
        if r is not None:
            i0, i1 = max(ci - r, 0), min(ci + r + 1, self.nx)
            j0, j1 = max(cj - r, 0), min(cj + r + 1, self.ny)
            ii, jj = np.nonzero(mask[i0:i1, j0:j1])
            ii, jj = ii + i0, jj + j0
        else:
            ii, jj = np.nonzero(mask)
        if len(ii) == 0:
            return None
        px, py = self.xy(ii, jj)
        d2 = (px - x) ** 2 + (py - y) ** 2
        k = int(np.argmin(d2))
        if max_dist is not None and d2[k] > max_dist ** 2:
            return None
        return int(ii[k]), int(jj[k])

    def copper_area(self) -> np.ndarray:
        """Each node's share of its cell that carries current: the mean copper width (wx / wy, a fraction of d) of
        the strips that touch it, 1 at a node of full strips (or of a grid without widths), 0 off copper. What a
        density in the copper (Solution.node_field(copper=True)) is weighted by for a sum over the cells, so that a
        strip's heat ρ·(I/φd)²/t·φd² is its R/φ·I² (the cell's area fill would not do: a via's antipad lowers it,
        not the current-carrying width around it)."""
        out = self.node.astype(float)
        if self.wx is None or self.wy is None:
            return out
        s, n = np.zeros(self.node.shape), np.zeros(self.node.shape)
        for w, on, lo, hi in ((self.wx, self.xb, (slice(None, -1), slice(None)), (slice(1, None), slice(None))),
                              (self.wy, self.yb, (slice(None), slice(None, -1)), (slice(None), slice(1, None)))):
            for sl in (lo, hi):
                s[sl] += np.where(on, w, 0.0)
                n[sl] += on
        return np.where(n > 0, s / np.maximum(n, 1), out)

    def net_at(self, node: tuple[int, int]) -> str:
        """The net of a node's copper ("" for no copper, or a grid without net labels)."""
        if self.label is None or self.label[node] < 0:
            return ""
        return self.nets[self.label[node]]

    @property
    def extent(self) -> tuple[float, float, float, float]:
        """(x0, x1, y0, y1) mm the grid's cells cover (each node at the centre of its cell): imshow's extent."""
        return (self.x0 - self.d / 2, self.x0 + (self.nx - 0.5) * self.d,
                self.y0 - self.d / 2, self.y0 + (self.ny - 0.5) * self.d)


def build_grid(geom, bbox: tuple[float, float, float, float], d: float,
               seeds: Optional[dict[str, list[tuple[float, float]]]] = None,
               lines: Optional[dict[str, list[list[tuple[float, float]]]]] = None, sub: int = 1) -> Grid:
    """Sample copper (mm) on a grid of pitch d covering bbox (x0, y0, x1, y1).

    `geom` is one geometry, or a dict {net: geometry} so that copper of different nets is never
    joined by a strip even when their clearance gap is narrower than the grid pitch.
    `seeds` {net: [(x, y), ...]} marks the nearest empty node as that net's copper, so small via
    rings and pads always get a node to connect to.
    `lines` {net: [(polyline, width mm), ...]} are track centrelines: their nodes and strips are forced, so a
    track narrower than the grid pitch (a GND escape from a pad to its via) still conducts. Where a track's
    path crosses a cell another net's copper owns, the grid keeps a bridge (`Grid.bridges`) for a lumped branch.
    `sub` > 1 derives nodes and strips from a sub-grid of pitch d/sub (see the module docstring): the coarse
    grid then keeps connections and slots down to that size, and `fill` holds each cell's copper fraction.
    """
    x0, y0, x1, y1 = bbox
    nx = max(int(math.ceil((x1 - x0) / d)) + 1, 3)
    ny = max(int(math.ceil((y1 - y0) / d)) + 1, 3)
    xs = x0 + np.arange(nx) * d
    ys = y0 + np.arange(ny) * d
    nets = list(geom.keys()) if isinstance(geom, dict) else [""]
    geoms = list(geom.values()) if isinstance(geom, dict) else [geom]
    fill = None
    fine = sub > 1
    if fine:
        label, cross_x, cross_y, fill, off_x, off_y = _coarse_from_fine(geoms, x0, y0, d, nx, ny, sub)
        mids_x = np.where(cross_x > 0, label[:-1, :], -1)
        mids_y = np.where(cross_y > 0, label[:, :-1], -1)
        geoms = []
    else:
        label = -np.ones((nx, ny), int)
        mids_x = np.full((nx - 1, ny), -1)
        mids_y = np.full((nx, ny - 1), -1)
    for k, g in enumerate(geoms):
        if g is None or g.is_empty:
            continue
        # only test the grid window covering this net's copper
        gx0, gy0, gx1, gy1 = g.bounds
        i0, i1 = max(int(math.floor((gx0 - x0) / d)) - 1, 0), min(int(math.ceil((gx1 - x0) / d)) + 2, nx)
        j0, j1 = max(int(math.floor((gy0 - y0) / d)) - 1, 0), min(int(math.ceil((gy1 - y0) / d)) + 2, ny)
        if i0 >= i1 or j0 >= j1:
            continue
        shapely.prepare(g)              # spatial index for the many point-in-polygon tests below
        X, Y = np.meshgrid(xs[i0:i1], ys[j0:j1], indexing="ij")
        sub = label[i0:i1, j0:j1]
        # a node is this net's copper if its point lies in the copper (the first net to claim a node keeps it);
        # likewise each strip's midpoint, half a pitch to the right (x) or below (y)
        sub[shapely.contains_xy(g, X, Y) & (sub < 0)] = k
        mx = mids_x[i0:min(i1, nx - 1), j0:j1]
        mx[shapely.contains_xy(g, X[: mx.shape[0]] + d / 2, Y[: mx.shape[0]]) & (mx < 0)] = k
        my = mids_y[i0:i1, j0:min(j1, ny - 1)]
        my[shapely.contains_xy(g, X[:, : my.shape[1]], Y[:, : my.shape[1]] + d / 2) & (my < 0)] = k
    seeded = np.zeros((nx, ny), bool)
    for net, points in (seeds or {}).items():
        if net not in nets:
            continue
        k = nets.index(net)
        for (px, py) in points:
            i, j = int(round((px - x0) / d)), int(round((py - y0) / d))
            if 0 <= i < nx and 0 <= j < ny and label[i, j] < 0:
                label[i, j] = k
                seeded[i, j] = True
    forced_x = np.zeros((nx - 1, ny))           # the widest forced track along each strip, mm (0 = none)
    forced_y = np.zeros((nx, ny - 1))
    bridges = []
    for net, polylines in (lines or {}).items():
        if net not in nets:
            continue
        bridges += _rasterise_lines(nets.index(net), polylines, x0, y0, d, label, seeded, forced_x, forced_y)
    # A strip exists if both end nodes and its midpoint are the same net's copper (catches slots >= d/2).
    # A seeded node (via ring / pad smaller than the grid) joins same-net neighbours without the midpoint test.
    xb = (label[:-1, :] >= 0) & (label[:-1, :] == label[1:, :]) & (
        (mids_x == label[:-1, :]) | seeded[:-1, :] | seeded[1:, :] | (forced_x > 0))
    yb = (label[:, :-1] >= 0) & (label[:, :-1] == label[:, 1:]) & (
        (mids_y == label[:, :-1]) | seeded[:, :-1] | seeded[:, 1:] | (forced_y > 0))
    wx = wy = ox = oy = None
    if fine:
        wx, ox = _strip_widths(xb, cross_x, off_x, forced_x, d)
        wy, oy = _strip_widths(yb, cross_y, off_y, forced_y, d)
    return Grid(x0, y0, d, nx, ny, label >= 0, xb, yb, label, nets, fill, bridges, wx, wy, ox, oy)


def _strip_widths(exists: np.ndarray, cross: np.ndarray, offset: np.ndarray, forced: np.ndarray,
                  d: float) -> tuple[np.ndarray, np.ndarray]:
    """Each strip's copper width as a fraction of the pitch d, and where its centre lies across the strip (an
    offset from the cell centre, a fraction of d): the share of its cell border the sub-grid saw copper cross and
    that copper's centre, at least a forced track's own width (centred where the sub-grid saw none); 1 and centred
    where the strip has neither (a seeded via ring or pad joined to its neighbour, as before). Zero where there is
    no strip.

    A strip used to be a full cell wide wherever it existed, so a neck narrower than the grid pitch conducted as
    if it were a pitch wide: on a 15 x 15 mm island joined to its plane by a 0.35 mm x 3 mm neck over a 0.2 mm
    cavity, the island's resonance moved 735-900 MHz as d went 0.1-1 mm, the neck's L being the grid's. With the
    strip's width, its resistance and partial self inductance are the neck's (PlaneStack._widths; SP-27 in
    tests/test_validation_audit.py, docs/ARCHITECTURE.md "A strip is as wide as the copper it crosses")."""
    w = np.where(forced > 0, np.maximum(cross, np.minimum(forced / d, 1.0)), cross)
    w = np.where(w > 0, np.minimum(w, 1.0), 1.0)
    o = np.where(cross > 0, offset, 0.0)
    # copper of width w centred at o stays inside the cell: |o| <= (1 - w) / 2 (a track wider than what the
    # sub-grid saw, centred on it)
    lim = (1.0 - w) / 2
    return np.where(exists, w, 0.0), np.where(exists, np.clip(o, -lim, lim), 0.0)


def _coarse_from_fine(geoms, x0: float, y0: float, d: float, nx: int, ny: int, s: int):
    """Coarse nodes and connections from a sub-grid of pitch d/s.

    Returns (label (nx, ny), cross_x (nx-1, ny), cross_y (nx, ny-1), fill (nx, ny), off_x, off_y): each cell's net
    (its largest same-net copper piece inside the cell, kept when it spans at least one sub-grid row), how much of
    the common border neighbouring cells' kept pieces touch across (0 = not at all, 1 = all of it: the fraction of
    its s fine strips that cross it), the kept piece's area fraction, and where along the border the crossing
    copper's centre lies (the mean of the crossing fine strips' positions, from the border's middle, a fraction of
    d; 0 where none cross)."""
    # Label the fine grid with nets (point sampling at d/s), find the connected pieces of each net's fine copper
    # *inside* each coarse cell (strips across cell borders left out), keep each cell's largest piece as its
    # copper, and join two coarse cells where a fine strip of their kept pieces crosses their common border.
    df = d / s
    mx, my = nx * s, ny * s
    xf = x0 - d / 2 + (np.arange(mx) + 0.5) * df
    yf = y0 - d / 2 + (np.arange(my) + 0.5) * df
    lab = -np.ones((mx, my), np.int32)
    for k, g in enumerate(geoms):
        if g is None or g.is_empty:
            continue
        gx0, gy0, gx1, gy1 = g.bounds
        p0, p1 = max(int(math.floor((gx0 - xf[0]) / df)) - 1, 0), min(int(math.ceil((gx1 - xf[0]) / df)) + 2, mx)
        q0, q1 = max(int(math.floor((gy0 - yf[0]) / df)) - 1, 0), min(int(math.ceil((gy1 - yf[0]) / df)) + 2, my)
        if p0 >= p1 or q0 >= q1:
            continue
        shapely.prepare(g)
        X, Y = np.meshgrid(xf[p0:p1], yf[q0:q1], indexing="ij")
        win = lab[p0:p1, q0:q1]
        win[shapely.contains_xy(g, X, Y) & (win < 0)] = k
    sx = (lab[:-1, :] >= 0) & (lab[:-1, :] == lab[1:, :])          # same-net sub-grid strips
    sy = (lab[:, :-1] >= 0) & (lab[:, :-1] == lab[:, 1:])
    bx = ((np.arange(mx - 1) + 1) % s == 0)[:, None]                # strip crosses a cell border
    by = ((np.arange(my - 1) + 1) % s == 0)[None, :]
    n = mx * my
    idx = np.arange(n).reshape(mx, my)
    ix, iy = sx & ~bx, sy & ~by                 # strips within a cell: the graph of the cells' pieces
    rows = np.concatenate([idx[:-1, :][ix], idx[:, :-1][iy]])
    cols = np.concatenate([idx[1:, :][ix], idx[:, 1:][iy]])
    _, comp = connected_components(sp.coo_matrix((np.ones(len(rows), np.int8), (rows, cols)), shape=(n, n)),
                                   directed=False)
    comp = comp.reshape(mx, my)
    cell = (np.arange(mx) // s)[:, None] * ny + (np.arange(my) // s)[None, :]
    has = lab >= 0
    cid = comp[has]
    size = np.bincount(cid, minlength=n)
    comp_cell = np.full(n, -1)
    comp_cell[cid] = cell[has]
    comp_net = np.full(n, -1)
    comp_net[cid] = lab[has]
    comps = np.unique(cid)
    order = np.lexsort((-size[comps], comp_cell[comps]))           # by cell, largest piece first
    comps = comps[order]
    first = np.ones(len(comps), bool)
    first[1:] = comp_cell[comps[1:]] != comp_cell[comps[:-1]]
    keep = comps[first]
    keep = keep[size[keep] >= s]                                    # spans at least one sub-grid row
    label = -np.ones(nx * ny, int)
    label[comp_cell[keep]] = comp_net[keep]
    fill = np.zeros(nx * ny)
    fill[comp_cell[keep]] = size[keep] / (s * s)
    kept = np.zeros(n, bool)
    kept[keep] = True
    on = has & kept[comp]
    # how many of the border's s fine strips cross it: the copper width joining the two cells, as a fraction of d
    cross_x, off_x = np.zeros((nx - 1, ny)), np.zeros((nx - 1, ny))
    pi, qi = np.nonzero(sx & bx & on[:-1, :] & on[1:, :])
    np.add.at(cross_x, (pi // s, qi // s), 1.0 / s)
    np.add.at(off_x, (pi // s, qi // s), ((qi % s + 0.5) / s - 0.5) / s)
    cross_y, off_y = np.zeros((nx, ny - 1)), np.zeros((nx, ny - 1))
    pj, qj = np.nonzero(sy & by & on[:, :-1] & on[:, 1:])
    np.add.at(cross_y, (pj // s, qj // s), 1.0 / s)
    np.add.at(off_y, (pj // s, qj // s), ((pj % s + 0.5) / s - 0.5) / s)
    with np.errstate(invalid="ignore", divide="ignore"):
        off_x = np.where(cross_x > 0, off_x / cross_x, 0.0)
        off_y = np.where(cross_y > 0, off_y / cross_y, 0.0)
    return label.reshape(nx, ny), cross_x, cross_y, fill.reshape(nx, ny), off_x, off_y


def _rasterise_lines(k: int, polylines, x0: float, y0: float, d: float, label: np.ndarray, seeded: np.ndarray,
                     forced_x: np.ndarray, forced_y: np.ndarray) -> list:
    """Walk each centreline (polyline, width mm) through the grid (4-connected) and claim its nodes and strips
    for net k (forced_x / forced_y get the widest claiming track's width, mm).

    The track's own copper decides which cells: at every point along the centreline the walk takes the nearest
    of the four cells around it that is already k's (the sub-grid gave it the track's piece), else the nearest
    free one; consecutive cells are joined along whichever L-shaped path crosses fewer cells of other nets. A cell
    another net's copper owns (its piece of the cell is larger: a track beside a pour) can't be claimed. The walk
    used to take the nearest cell only and stop at a foreign one, which cut tracks apart: on kraft_flux_mini_2 a
    78 mm +3V3 track on In3, tied to its pour at one end, floated free on the 0.86 and 0.61 mm grids (a half-wave
    resonance at ~1.24 GHz, the board's worst SPI_SCK margin) and was joined on the 0.43 mm grid; on the 0.86 mm
    grid its own cells were the column beside the one the walk took. A gap that is left (all four cells
    foreign) is joined by a bridge: returned as (i0, j0, i1, j1, length along the track mm, width mm), for a
    lumped branch (analysis.simulate adds them, peec.track_impedance)."""
    nx, ny = label.shape
    bridges = []

    def inside(c) -> bool:
        """Cell c is on the grid."""
        return 0 <= c[0] < nx and 0 <= c[1] < ny

    def own(c) -> bool:
        """Cell c is already net k's copper."""
        return inside(c) and label[c] == k

    def free(c) -> bool:
        """Cell c may be claimed for net k (no copper, or k's own)."""
        return inside(c) and label[c] in (-1, k)

    def path(a, b) -> list:
        """4-connected cells from a (exclusive) to b (inclusive): x first or y first, the freer one."""
        sx, sy = (b[0] > a[0]) - (b[0] < a[0]), (b[1] > a[1]) - (b[1] < a[1])
        xs = [(a[0] + sx * n, a[1]) for n in range(1, abs(b[0] - a[0]) + 1)]
        ys = [(a[0], a[1] + sy * n) for n in range(1, abs(b[1] - a[1]) + 1)]
        x_first = xs + [(b[0], c[1]) for c in ys]
        y_first = ys + [(c[0], b[1]) for c in xs]
        return min((x_first, y_first), key=lambda p: sum(not free(c) for c in p))

    for pts, width in polylines:
        cells: list[tuple[int, int, float]] = []          # (i, j, distance along the centreline mm)
        s0 = 0.0
        for (ax, ay), (bx, by) in zip(pts, pts[1:]):
            seg = math.hypot(bx - ax, by - ay)
            n = max(int(math.ceil(seg / (0.4 * d))), 1)    # samples 0.4 pitch apart: no cell is skipped
            for tt in np.linspace(0.0, 1.0, n + 1):
                u, v = (ax + tt * (bx - ax) - x0) / d, (ay + tt * (by - ay) - y0) / d
                i0, j0 = math.floor(u), math.floor(v)
                near = sorted(((i, j) for i in (i0, i0 + 1) for j in (j0, j0 + 1)),
                              key=lambda c: (c[0] - u) ** 2 + (c[1] - v) ** 2)
                c = next((c for c in near if own(c)), None) or next((c for c in near if free(c)), near[0])
                s = s0 + tt * seg
                if not cells:
                    cells.append((c[0], c[1], s))
                elif cells[-1][:2] != c:
                    cells += [(q[0], q[1], s) for q in path(cells[-1][:2], c)]
            s0 += seg
        prev = None
        last = None                         # the last cell of k before a gap: (i, j, distance)
        for i, j, s in cells:
            if not (0 <= i < nx and 0 <= j < ny):
                prev = last = None
                continue
            if label[i, j] < 0:
                label[i, j] = k
                seeded[i, j] = True
            if label[i, j] != k:
                prev = None
                continue
            if prev is not None:
                pi, pj = prev
                tw = max(float(width), 1e-6)            # the strip's track width (the widest one along it)
                if pj == j and abs(pi - i) == 1:
                    forced_x[min(pi, i), j] = max(forced_x[min(pi, i), j], tw)
                elif pi == i and abs(pj - j) == 1:
                    forced_y[i, min(pj, j)] = max(forced_y[i, min(pj, j)], tw)
            elif last is not None and last[:2] != (i, j):
                bridges.append((last[0], last[1], i, j, max(s - last[2], d), float(width)))
            prev = (i, j)
            last = (i, j, s)
    return bridges


# --------------------------------------------------------------------------- problem description
@dataclass
class PlaneLayer:
    """One copper layer of a model: its name, height (m) and copper thickness, and its rasterised copper."""
    name: str
    z: float                  # metres, vertical position (up = +), any common origin
    grid: Grid
    thickness: float = 35e-6  # copper thickness, m


@dataclass
class Branch:
    """A lumped connection between two nodes, possibly on different layers."""
    a: tuple[int, int, int]   # (layer index, i, j)
    b: tuple[int, int, int]
    z: Callable[[float], complex]
    name: str = ""
    kind: str = "part"        # part | via | track (a bridge: Grid.bridges)
    radius: float = 1.5e-4    # via barrel radius (m), for its coupling to the signal via


@dataclass
class PlanePair:
    """Capacitance between two facing plane layers (indices into the layer list, upper < lower)."""
    upper: int
    lower: int
    c_per_area: float         # ε0·εr / t   (F/m²)
    tan_delta: float = 0.02   # dielectric loss tangent


@dataclass
class TraceSegment:
    """A straight piece of the known current path (a track, a via's barrel, a switch), as a filament from a to b
    carrying `i` A per A of loop current. The plane currents are solved; these are given."""
    a: np.ndarray         # (3,) metres; x, y relative to grid node (0, 0), z as in PlaneLayer.z
    b: np.ndarray
    width: float          # metres (for self inductance / GMD)
    i: float = 1.0        # current a -> b per A of loop current (a switch's current splits over its pads)


def via_impedance(length_m: float, drill_m: float, sigma: float = SIGMA_CU, filled: bool = False
                  ) -> Callable[[float], complex]:
    """Barrel of a plated via between two layers: R (plating) + jωL (partial self inductance [Paul 2010]). filled: a
    part's pin soldered in the hole carries the current over the whole cross-section (copper-equivalent)."""
    r = max(drill_m / 2, 5e-5)
    # the barrel as a tube [A3]: its partial self inductance is a filament's at its radius (a thin tube's GMD
    # [Grover 1946]); its
    # resistance that of the plating ring 2 pi r t (or of the whole hole when a pin fills it)
    L = float(filament_mutual(max(length_m, 1e-6), np.array(0.0), np.array(r)))
    area = math.pi * r * r if filled else math.pi * 2 * r * VIA_PLATING
    R = max(length_m, 1e-6) / (sigma * area)
    return lambda f, R=R, L=L: complex(R, 2 * math.pi * f * L)


def track_impedance(length_m: float, width_m: float, thickness_m: float = 35e-6, sigma: float = SIGMA_CU
                    ) -> Callable[[float], complex]:
    """A piece of track as a lumped branch (a bridge across another net's grid cells, Grid.bridges): R with the
    skin depth (as _trace_resistance) + jω·its partial self inductance. Its coupling to the strips around it is
    left out: a bridge spans one or two cells."""
    length_m = max(length_m, 1e-6)
    L = strip_self(length_m, width_m, thickness_m)

    def z(f: float) -> complex:
        """The bridge's impedance at f: R with the copper's effective thickness for the skin depth, + jwL."""
        t = thickness_m
        if f > 0:
            # skin depth delta = 1 / sqrt(pi f mu0 sigma) [Ramo 1994]; the current flows in an effective thickness
            # delta (1 - e^(-t/delta)) [A2]: t at low frequencies, delta once delta << t
            delta = 1 / math.sqrt(math.pi * f * MU0 * sigma)
            t = delta * (1 - math.exp(-thickness_m / delta))
        return complex(length_m / (sigma * width_m * t), 2 * math.pi * f * L)
    return z


@dataclass
class Solution:
    """One solve at one frequency, per A of loop current: every strip's current, every node's potential, the
    branch and displacement currents, the loop impedance and the loop's magnetic dipole moment (what radiates)."""
    f: float
    I_x: np.ndarray       # (N, nx-1, ny) complex strip currents (A per A of signal)
    I_y: np.ndarray       # (N, nx, ny-1)
    V: np.ndarray         # (N, nx, ny) node potentials
    I_branch: np.ndarray  # currents in the lumped/vertical branches (a -> b)
    Z_loop: complex
    m: np.ndarray         # (3,) complex magnetic dipole moment per A (A·m²)
    iterations: int
    I_cap: np.ndarray = field(default_factory=lambda: np.zeros(0, complex))  # plane-pair displacement currents
    state: Optional[np.ndarray] = None      # unused now: see state_for (results pickled before may carry it)
    W_x: Optional[np.ndarray] = None        # (N, nx-1, ny) the strips' copper width / d (the stack's, shared by its
    W_y: Optional[np.ndarray] = None        # solutions; None: every strip full width)

    def state_for(self, stack: "PlaneStack") -> np.ndarray:
        """The solver's unknown vector [branch currents; node potentials] of this solution on `stack`, the stack
        it was solved on (a warm start for the fix search), rebuilt from the currents and potentials kept here:
        the same numbers. Solutions stored it as well before, a copy of what they hold (tools/dev/mem_profile.py)."""
        if self.state is not None:
            return self.state
        Ib = np.concatenate([self.I_x[tuple(stack.xa.T)], self.I_y[tuple(stack.ya.T)], self.I_cap, self.I_branch])
        return np.concatenate([Ib, self.V[np.nonzero(stack.node)][stack.keep]])

    @property
    def L_loop(self) -> float:
        """The loop inductance (H): Im(Z_loop) / w."""
        return self.Z_loop.imag / (2 * math.pi * self.f)

    def node_field(self, k: int, d_mm: float, copper: bool = False) -> tuple[np.ndarray, np.ndarray]:
        """Complex (Kx, Ky) at the nodes of layer k (A/m per A of signal): each node averages the strips that
        touch it in each direction. The current per cell width (what a cut across the cells adds up), or with
        copper=True per width of the copper itself: a strip narrower than the pitch (PlaneStack._widths) carries
        its current in φd, not d (a density: weighted by Grid.copper_area in a sum over the cells, the heat
        ρ·K²/t·φd² of a strip is its R/φ·I²)."""
        d = d_mm * 1e-3
        Ix, Iy = self.I_x[k], self.I_y[k]
        if copper and self.W_x is not None:
            Ix, Iy = Ix / self.W_x[k], Iy / self.W_y[k]
        nx, ny = self.V.shape[1:]
        kx = np.zeros((nx, ny), complex)
        ky = np.zeros((nx, ny), complex)
        cx = np.zeros((nx, ny))
        cy = np.zeros((nx, ny))
        kx[:-1, :] += Ix
        kx[1:, :] += Ix
        cx[:-1, :] += Ix != 0
        cx[1:, :] += Ix != 0
        ky[:, :-1] += Iy
        ky[:, 1:] += Iy
        cy[:, :-1] += Iy != 0
        cy[:, 1:] += Iy != 0
        return kx / np.maximum(cx, 1) / d, ky / np.maximum(cy, 1) / d

    def sheet_current(self, k: int, d_mm: float, copper: bool = False) -> np.ndarray:
        """|K| at the nodes of layer k, in A/m per A of signal current (copper: see node_field)."""
        kx, ky = self.node_field(k, d_mm, copper)
        return np.sqrt(np.abs(kx) ** 2 + np.abs(ky) ** 2)

    def dominant_phase(self) -> float:
        """The phase at which the plane currents are largest, φ = ½·angle(Σ I²) over all strips of all layers
        (the principal axis of the complex currents), with the sign that follows the signal's own phase (the
        1 A excitation is real): Re(I·e^(-jφ)) correlates positively with Re(I). 0 for a real (DC) solution."""
        cached = getattr(self, "_phase", None)
        if cached is not None:
            return cached
        # Re(I e^(-j phi)) is largest overall when phi is half the angle of sum(I^2): the principal axis of the
        # currents as points in the complex plane
        sq = complex(np.sum(self.I_x ** 2) + np.sum(self.I_y ** 2))
        phi = 0.5 * math.atan2(sq.imag, sq.real) if abs(sq) > 0 else 0.0
        rot = np.exp(-1j * phi)
        corr = float(np.sum((self.I_x * rot).real * self.I_x.real) + np.sum((self.I_y * rot).real * self.I_y.real))
        if corr < 0:
            phi += math.pi
        self._phase = phi
        return phi

    def sheet_vectors(self, k: int, d_mm: float, copper: bool = False) -> tuple[np.ndarray, np.ndarray]:
        """Real (Kx, Ky) at the nodes of layer k (A/m per A of signal) at the dominant phase: the direction the
        current flows when it is strongest. x to the right, y down (board coordinates). copper: see node_field."""
        kx, ky = self.node_field(k, d_mm, copper)
        rot = np.exp(-1j * self.dominant_phase())
        return (kx * rot).real, (ky * rot).real

    def layer_share(self, masks: Optional[list] = None) -> np.ndarray:
        """Fraction of the total strip current magnitude flowing in each layer. masks: per layer (x, y) boolean
        arrays of the strips to count (e.g. only the return nets, not a pour carrying the signal itself)."""
        per = []
        for k in range(len(self.I_x)):
            ix, iy = np.abs(self.I_x[k]), np.abs(self.I_y[k])
            if masks is not None:
                ix, iy = ix * masks[k][0], iy * masks[k][1]
            per.append(ix.sum() + iy.sum())
        per = np.array(per)
        tot = per.sum()
        return per / tot if tot > 0 else per


# --------------------------------------------------------------------------- SuperLU factorisations
class _LuHost:
    """A thread that makes SuperLU factorisations and frees them again. scipy's SuperLU wrapper (1.18) books its C
    allocations per thread: a factorisation freed from another thread than the one that made it never gave its
    memory back (82 MiB each on a test system; ~640 MiB per Simulate on 4 threads, made in the solver's pool
    threads and dropped from the job thread; the user saw 5.4 GB kept after a failed Simulate). Solving with it
    from any thread is fine (tools/dev/mem_probe.py and the test in tests/test_solver.py)."""

    def __init__(self):
        """Start the host thread (a daemon: it never keeps the process alive), waiting for jobs."""
        self.jobs: "queue.SimpleQueue[Optional[Callable[[], None]]]" = queue.SimpleQueue()
        self.thread = threading.Thread(target=self._run, name="groundtruth-lu", daemon=True)
        self.thread.start()

    def _run(self):
        """The host thread: run each job (make a factorisation, or free one) until told to stop (None)."""
        while True:
            job = self.jobs.get()
            if job is None:                 # _lu_exit
                return
            job()
            job = None                      # what it held (a factorisation to free) goes now, in this thread


_LU_IDLE: list = []                         # hosts not factorising right now (one per concurrent factorisation)
_LU_HOSTS: list = []                        # every host made
_LU_LOCK = threading.Lock()
_EXITING: list = []                         # [True] once the interpreter is shutting down (_lu_exit)
LU_EXIT_WAIT_S = 5.0


@atexit.register
def _lu_exit():
    """At exit the hosts end their threads, before the interpreter is torn down. A daemon thread that had made a
    SuperLU factorisation and was still alive then (even with the factorisation freed) left an exception set:
    Python printed "Exception ignored on flushing sys.stdout: SystemError ... returned a result with an exception
    set" and exited with 120 instead of the run's own code (a CLI run with its output redirected; plain threads
    that had ended were fine). Factorisations still alive are freed where they are dropped from then on: memory no
    longer matters. A host still factorising (an interrupted run) is not waited for longer than LU_EXIT_WAIT_S."""
    _EXITING.append(True)
    with _LU_LOCK:
        hosts = list(_LU_HOSTS)
    for host in hosts:
        host.jobs.put(None)
    for host in hosts:
        host.thread.join(LU_EXIT_WAIT_S)


class HostedLU:
    """A SuperLU factorisation that lives and dies in its host thread (_LuHost); solve() works from any thread."""
    __slots__ = ("_lu", "_host")

    def __init__(self, lu, host: _LuHost):
        """Wrap SuperLU object `lu`, which `host` made (and must free)."""
        self._lu, self._host = lu, host

    def solve(self, rhs):
        """Solve with the factorisation (from any thread)."""
        return self._lu.solve(rhs)

    def __del__(self):
        """Hand the factorisation to its host thread to be freed there (see _LuHost)."""
        # The host must hold the last reference: a list does, which the host empties. (Handing it the object while
        # this frame still had it in a local lost a race: a host idle at the time ran first, this local was the
        # last reference, and the factorisation was freed here, its memory kept: +330 MB per Simulate on a test
        # board once the factorisations were dropped before the finishing phase.)
        held = [getattr(self, "_lu", None)]
        self._lu = None
        if held[0] is not None and not _EXITING:
            try:
                self._host.jobs.put(held.clear)
            except Exception:       # noqa: BLE001 - interpreter shutdown: nothing to give back to any more
                pass


def hosted_splu(S, **kwargs) -> HostedLU:
    """splu(S, **kwargs) made in a host thread (an idle one, or a new one: factorisations stay as parallel as
    their callers), so that the host frees it too. Raises what splu raises."""
    with _LU_LOCK:
        if _LU_IDLE:
            host = _LU_IDLE.pop()
        else:
            host = _LuHost()
            _LU_HOSTS.append(host)          # ended at exit (_lu_exit)
    box: list = []
    done = threading.Event()

    def job():
        """In the host thread: factorise, passing the result (or the exception) back through `box`."""
        try:
            box.append(splu(S, **kwargs))
        except BaseException as e:          # noqa: BLE001 - handed to the caller
            box.append(e)
        finally:
            done.set()
    try:
        host.jobs.put(job)
        done.wait()
    finally:
        with _LU_LOCK:
            _LU_IDLE.append(host)
    out = box.pop()
    if isinstance(out, BaseException):
        raise out
    return HostedLU(out, host)


# --------------------------------------------------------------------------- the plane system
@dataclass
class _Operator:
    """The system and preconditioner at one frequency."""
    w: float
    R_strip: np.ndarray
    z_tail: np.ndarray        # impedances of the capacitance and branch unknowns
    Dinv: sp.csr_matrix       # preconditioner admittance: diag(Z)⁻¹ or per-position layer blocks
    solve_S: Callable         # (Aᵀ·D⁻¹·A)⁻¹ applied to a vector (or block of vectors)
    f_lu: float = 0.0         # frequency the preconditioner was factorised at (≠ f when borrowed)
    made: Optional["_Made"] = None  # this stack's record of the factorisation (own operators; None when borrowed)
    src: Optional["_Made"] = None   # a borrowed preconditioner: the record of THIS stack's factorisation it uses
    ref: Optional["_Made"] = None   # a stack derived from a borrowing base: the base's source (budget only)

    def borrow_budget(self) -> Optional[int]:
        """GMRES iterations a solve with this borrowed preconditioner may take before a factorisation at its own
        frequency is cheaper: what the source needed at home, half again, plus BORROW_EXTRA_ITS. None when the
        source was never solved at home (then only a failed solve factorises). A derived stack's operator on a
        borrowed base factorisation is judged by the base's source (`ref`), but never borrows from it: its own
        system has more branches. Counted in iterations, not seconds (it was half a factorisation's measured time
        before): a decision must not depend on how busy the machine was (see PlaneStack.operator)."""
        s = self.src or self.ref
        if s is not None and s.its is None and s.ref is not None:   # a derived stack's operator on a borrowed base
            s = s.ref
        if s is None or s.its is None:
            return None
        return int(BORROW_BUDGET * s.its) + BORROW_EXTRA_ITS


@dataclass
class _Made:
    """A factorisation a stack made (or decided to make): what other frequencies may borrow. Kept when the
    factorisation itself is freed (OPERATOR_CACHE, drop_factorisations): it is made again, identically."""
    f: float                  # the frequency it was asked for
    exact: bool               # asked with exact=True (a derived stack's own factorisation on the base's exact one)
    round: int                # the stack's round when it was decided: other frequencies see it from the next one
    f_lu: Optional[float] = None    # the frequency its preconditioner is factorised at (≠ f on a borrowing base)
    its: Optional[int] = None       # fewest GMRES iterations of an unguessed solve at f with it, in its own round
    ref: Optional["_Made"] = None   # derived stacks: the base's record it was built on, when that one was borrowed


class _Build:
    """A factorisation one thread is making; the others wait for it and take `op` (None when it failed)."""

    def __init__(self):
        """Not done yet; no operator yet."""
        self.done = threading.Event()
        self.op: Optional[_Operator] = None


class PlaneStack:
    """Plane layers with their vias, lumped parts and plane-pair capacitance: the part of the problem that
    does not depend on the signal route. Unknown order: [x strips | y strips | capacitances | branches]."""

    def __init__(self, layers: list[PlaneLayer], branches: Optional[list[Branch]] = None,
                 capacitance: Optional[list[PlanePair]] = None, sigma: float = SIGMA_CU,
                 precond: str = "diag"):
        """The stack of `layers` (all on one grid), with lumped `branches` (vias, parts) and the plane pairs'
        `capacitance`; precond "diag" or "sheet" (see the module docstring). Builds the topology (unknowns,
        incidence matrix) and the inductance kernels; nothing is factorised until a frequency is solved."""
        if not layers:
            raise SolverError("no plane layers")
        if precond not in ("diag", "sheet"):
            raise ValueError("precond must be 'diag' or 'sheet'")
        self.precond_mode = precond
        g0 = layers[0].grid
        for l in layers:
            if (l.grid.nx, l.grid.ny, l.grid.d) != (g0.nx, g0.ny, g0.d):
                raise SolverError("all layers must share one grid")
        self.layers = list(layers)
        self.N = len(layers)
        self.nx, self.ny = g0.nx, g0.ny
        self.d = g0.d * 1e-3
        self.zs = np.array([l.z for l in layers])
        self.sigma = sigma
        self.pairs = list(capacitance or [])
        self._base: Optional[PlaneStack] = None
        self._ops: OrderedDict = OrderedDict()
        self._lock = threading.Lock()
        self._building: dict = {}           # factorisation key -> _Build, while one thread factorises it
        self._made: dict = {}               # factorisation key -> _Made (see operator)
        self._plan: dict = {}               # frequency key -> the factorisation key it uses
        self._round = 0
        self._topology(list(branches or []))
        self._kernels()

    # ------------------------------------------------------------------ topology
    def _topology(self, branches: list[Branch]):
        """Number the unknowns and build the incidence matrix A (branches x nodes): x strips, y strips, plane-pair
        capacitances, lumped branches, each from node a to node b (+1 at a, -1 at b)."""
        node = np.stack([l.grid.node for l in self.layers])
        # a strip exists only where both of its end nodes are copper
        self.xb = np.stack([l.grid.xb for l in self.layers]) & node[:, :-1, :] & node[:, 1:, :]
        self.yb = np.stack([l.grid.yb for l in self.layers]) & node[:, :, :-1] & node[:, :, 1:]
        self.node = node
        self.nid = -np.ones(node.shape, int)            # (layer, i, j) -> node number, -1 off copper
        idx = np.nonzero(node)
        self.nid[idx] = np.arange(len(idx[0]))
        self.n_nodes = len(idx[0])
        self.xa, self.ya = np.argwhere(self.xb), np.argwhere(self.yb)
        self.nxb, self.nyb = len(self.xa), len(self.ya)
        self.ns = self.nxb + self.nyb
        self._widths()
        # Plane-pair capacitance wherever two planes face each other with no copper in between
        cap = []   # rows: (upper k, lower l, i, j, C, tanδ)
        cell = self.d ** 2
        fill = [l.grid.fill if l.grid.fill is not None else node[k].astype(float) for k, l in enumerate(self.layers)]
        for p in self.pairs:
            between = np.zeros(node.shape[1:], bool)
            for m in range(p.upper + 1, p.lower):
                between |= node[m]
            share = np.minimum(fill[p.upper], fill[p.lower])     # facing copper area of the cell
            # a parallel-plate capacitor per cell where both planes have copper and no layer between them does
            # (copper in between shields: its own pairs cover it); C = eps/t x the facing area (parallel plates,
            # fringing left out [Pozar 2012])
            ii, jj = np.nonzero(node[p.upper] & node[p.lower] & ~between & (share > 0))
            cap.append(np.column_stack([np.full(len(ii), p.upper), np.full(len(ii), p.lower), ii, jj,
                                        p.c_per_area * cell * share[ii, jj], np.full(len(ii), p.tan_delta)]))
        self.cap = np.concatenate(cap) if cap else np.zeros((0, 6))
        self.ncap = len(self.cap)
        # branches whose ends are both on copper (and different): the others would join nothing
        self.branches = [b for b in branches if self.nid[b.a] >= 0 and self.nid[b.b] >= 0 and b.a != b.b]
        self.nbr = len(self.branches)
        self.nb = self.ns + self.ncap + self.nbr
        cu = self.cap[:, :4].astype(int)
        self.a_nodes = np.concatenate([self.nid[tuple(self.xa.T)], self.nid[tuple(self.ya.T)],
                                       self.nid[cu[:, 0], cu[:, 2], cu[:, 3]],
                                       np.array([self.nid[b.a] for b in self.branches], int)]).astype(int)
        self.b_nodes = np.concatenate([self.nid[self.xa[:, 0], self.xa[:, 1] + 1, self.xa[:, 2]],
                                       self.nid[self.ya[:, 0], self.ya[:, 1], self.ya[:, 2] + 1],
                                       self.nid[cu[:, 1], cu[:, 2], cu[:, 3]],
                                       np.array([self.nid[b.b] for b in self.branches], int)]).astype(int)
        r = np.arange(self.nb)
        self.A = sp.csr_matrix((np.concatenate([np.ones(self.nb), -np.ones(self.nb)]),
                                (np.concatenate([r, r]), np.concatenate([self.a_nodes, self.b_nodes]))),
                               shape=(self.nb, self.n_nodes))
        self._components()

    def _widths(self):
        """Strips narrower than the pitch (the grids' wx / wy, ox / oy; full where a grid has none):
        `strip_width` (ns), each strip's copper width as a fraction of the pitch (and per layer as `W_x` / `W_y`,
        None when every strip is full: the solutions' densities in the copper); `dL` (H per strip, None when
        every strip is full), the partial self inductance a narrower strip adds to the full square strip's in the
        kernels, L(d, φd) − L(d, d) of a flat strip of the layer's copper thickness (_narrow_self); `M_near`
        (sparse ns x ns, None when every strip is full), the change in mutual inductance between side-by-side
        neighbours where one of them is narrower: the kernels have them a full pitch apart, a full pitch wide,
        while their copper is narrower and its centres nearer (a neck straddling two cell rows is two partial
        strips touching: at a pitch apart they acted as a conductor wider than the neck, its L 16 % low). The
        resistance is the sheet resistance / φ (_impedances)."""
        def gather(attr, idx, shape, default):
            """One orientation's per-strip values of the grids' `attr` (`default` where a grid has none)."""
            if len(idx) == 0:
                return np.zeros(0)
            if all(getattr(l.grid, attr, None) is None for l in self.layers):
                return np.full(len(idx), default)
            w = np.stack([getattr(l.grid, attr) if getattr(l.grid, attr, None) is not None
                          else np.full(shape, default) for l in self.layers])
            return w[tuple(idx.T)]
        sx, sy = (self.nx - 1, self.ny), (self.nx, self.ny - 1)
        w = np.concatenate([gather("wx", self.xa, sx, 1.0), gather("wy", self.ya, sy, 1.0)])
        ok = w > 0                          # a strip added to a grid after it was built: full width, centred
        self.strip_width = np.where(ok, w, 1.0)
        off = np.where(ok, np.concatenate([gather("ox", self.xa, sx, 0.0), gather("oy", self.ya, sy, 0.0)]), 0.0)
        self.dL = self.M_near = self.W_x = self.W_y = None
        narrow = self.strip_width < 1.0
        if not np.any(narrow):
            return
        self.W_x, self.W_y = np.ones((self.N, *sx)), np.ones((self.N, *sy))     # per layer (Solution.node_field)
        self.W_x[tuple(self.xa.T)] = self.strip_width[: self.nxb]
        self.W_y[tuple(self.ya.T)] = self.strip_width[self.nxb:]
        d = self.d
        t = np.array([l.thickness for l in self.layers])[np.concatenate([self.xa[:, 0], self.ya[:, 0]])]
        dL = np.zeros(self.ns)
        keys, inv = np.unique(np.round(np.column_stack([self.strip_width[narrow], t[narrow]]), 12), axis=0,
                              return_inverse=True)
        dL[narrow] = np.array([_narrow_self(d, phi, tk) for phi, tk in keys])[np.ravel(inv)]
        self.dL = dL
        # side-by-side neighbours: x strips at row j and j + 1, y strips at column i and i + 1, the same layer, up
        # to NEAR_AXIAL strips apart along their length (a long neck's two rows couple all along it)
        rows, cols, steps = [], [], []
        for idx, shape, lo, axis in ((self.xa, sx, 0, 2), (self.ya, sy, self.nxb, 1)):
            if len(idx) == 0:
                continue
            at = -np.ones((self.N, *shape), int)
            at[tuple(idx.T)] = lo + np.arange(len(idx))
            n_ax = shape[0] if axis == 2 else shape[1]
            for k in range(-NEAR_AXIAL, NEAR_AXIAL + 1):
                if abs(k) >= n_ax:
                    continue
                ax_a = slice(max(-k, 0), n_ax - max(k, 0))
                ax_b = slice(max(k, 0), n_ax - max(-k, 0))
                a = at[:, ax_a, :-1] if axis == 2 else at[:, :-1, ax_a]
                b = at[:, ax_b, 1:] if axis == 2 else at[:, 1:, ax_b]
                both = (a >= 0) & (b >= 0)
                a, b = a[both], b[both]
                part = narrow[a] | narrow[b]
                rows.append(a[part])
                cols.append(b[part])
                steps.append(np.full(int(part.sum()), abs(k)))
        a, b = np.concatenate(rows), np.concatenate(cols)
        if len(a) == 0:
            return
        k = np.concatenate(steps)
        wa, wb = self.strip_width[a], self.strip_width[b]
        gap = 1.0 + off[b] - off[a]                         # centre distance, a fraction of the pitch
        keys, inv = np.unique(np.round(np.column_stack([wa, wb, gap, k]), 9), axis=0, return_inverse=True)
        dM = (_side_mutual(d, keys[:, 0], keys[:, 1], keys[:, 2], keys[:, 3])
              - _side_mutual(d, np.ones(len(keys)), np.ones(len(keys)), np.ones(len(keys)), keys[:, 3]))
        dM = dM[np.ravel(inv)]
        M = sp.coo_matrix((dM, (a, b)), shape=(self.ns, self.ns))
        self.M_near = (M + M.T).tocsr()

    def _components(self):
        """Conductive connectivity (strips, vias, parts) and full connectivity (plus plane capacitance).
        One node per full component is the potential reference (floating islands are kept: they carry
        eddy currents)."""
        n = self.n_nodes
        cond = np.ones(self.nb, bool)
        cond[self.ns: self.ns + self.ncap] = False
        g = sp.coo_matrix((np.ones(cond.sum()), (self.a_nodes[cond], self.b_nodes[cond])), shape=(n, n))
        _, self.cond_label = connected_components(g, directed=False)
        g = sp.coo_matrix((np.ones(self.nb), (self.a_nodes, self.b_nodes)), shape=(n, n))
        _, self.full_label = connected_components(g, directed=False)
        # one potential per connected piece is the reference (V = 0): its column is dropped from A, which makes
        # the system non-singular (potentials are only defined up to a constant per piece)
        _, first = np.unique(self.full_label, return_index=True)
        self.keep = np.ones(n, bool)
        self.keep[first] = False
        self.Ar = self.A[:, self.keep].tocsc()
        self.ArT = self.Ar.T.tocsr()

    def node_id(self, node: tuple[int, int, int]) -> int:
        """The node number of (layer, i, j), -1 when it is not on copper."""
        return int(self.nid[node])

    def with_branches(self, extra: list[Branch]) -> "PlaneStack":
        """This stack plus a few branches (candidate vias / parts). Shares grids and kernels; solves reuse
        this stack's factorisations through a low-rank (Woodbury) update [Woodbury 1950]."""
        extra = [b for b in extra if self.nid[b.a] >= 0 and self.nid[b.b] >= 0 and b.a != b.b]
        if not extra:
            return self
        a = np.array([self.nid[b.a] for b in extra])
        b = np.array([self.nid[b.b] for b in extra])
        if np.any(self.full_label[a] != self.full_label[b]):   # joins islands: references change
            return PlaneStack(self.layers, self.branches + extra, self.pairs, self.sigma, self.precond_mode)
        # a shallow copy shares the grids, kernels and node numbering; only the branch list, the incidence matrix
        # (new rows) and the caches are its own
        new = copy.copy(self)
        new.branches = self.branches + extra
        new.nbr = self.nbr + len(extra)
        new.nb = self.nb + len(extra)
        new.a_nodes = np.concatenate([self.a_nodes, a])
        new.b_nodes = np.concatenate([self.b_nodes, b])
        r = np.arange(self.nb, new.nb)
        rows = sp.csr_matrix((np.concatenate([np.ones(len(r)), -np.ones(len(r))]),
                              (np.concatenate([r - self.nb] * 2), np.concatenate([a, b]))),
                             shape=(len(r), self.n_nodes))
        new.A = sp.vstack([self.A, rows]).tocsr()
        new.Ar = new.A[:, self.keep].tocsc()
        new.ArT = new.Ar.T.tocsr()
        n = self.n_nodes
        cond = np.ones(new.nb, bool)
        cond[new.ns: new.ns + new.ncap] = False
        _, new.cond_label = connected_components(
            sp.coo_matrix((np.ones(cond.sum()), (new.a_nodes[cond], new.b_nodes[cond])), shape=(n, n)), directed=False)
        new._base = self
        new._ops = OrderedDict()
        new._lock = threading.Lock()
        new._building = {}
        new._made = {}
        new._plan = {}
        new._round = 0
        gpu.derived(new)            # its own incidence matrices on a GPU; the kernels stay shared
        return new

    def map_state(self, x: Optional[np.ndarray], stack: "PlaneStack") -> Optional[np.ndarray]:
        """A solution vector of `stack` expressed in this stack's unknowns (for a warm start): the same stack,
        or this stack's base before with_branches, whose added branches start with zero current."""
        if x is None:
            return None
        if stack is self:
            return x
        if self._base is stack and len(x) == stack.nb + stack.Ar.shape[1]:
            return np.concatenate([x[:stack.nb], np.zeros(self.nb - stack.nb, complex), x[stack.nb:]])
        return None

    # ------------------------------------------------------------------ inductance
    def _kernels(self):
        """The layer-pair kernel spectra, per orientation one real (P0 // 2 + 1, P1 // 2 + 1, N, N) array: a quarter
        of each spectrum (they are even in both axes, K[i, j] = K[P0 - i, j] = K[i, P1 - j], to 4e-17), pixel-major
        (what the compiled multiply-add reads; kernel() / Kx / Ky unfold full arrays on demand). They were kept
        twice before, a dict of full per-pair spectra (each still holding its complex FFT buffer, twice its size)
        and a full (P0, P1, N, N) copy for the compiled loop: 624 bytes per padded pixel for 6 layers, now 72
        (tools/dev/mem_profile.py). Only the distinct pairs (42) made the compiled loop 10-35 % slower (_mac.py)."""
        nx, ny, d = self.nx, self.ny, self.d
        N = self.N
        # FFT sizes [Oppenheim 2010]: a linear (not circular) convolution of an n-point array with a 2n-1 point
        # kernel needs at least 2n-1 points; next_fast_len rounds up to a size with small prime factors (fast FFTs)
        self.Px = (next_fast_len(2 * (nx - 1) - 1), next_fast_len(2 * ny - 1))
        self.Py = (next_fast_len(2 * nx - 1), next_fast_len(2 * (ny - 1) - 1))
        Kx = np.empty((self.Px[0] // 2 + 1, self.Px[1] // 2 + 1, N, N))
        Ky = np.empty((self.Py[0] // 2 + 1, self.Py[1] // 2 + 1, N, N))
        self.L_self = _kernel_pair(nx, ny, d, 0.0)[2]
        centre = np.zeros((self.N, self.N))
        sum_x = np.zeros((self.N, self.N))
        sum_y = np.zeros((self.N, self.N))
        for s in range(self.N):
            for t in range(s, self.N):
                # mutual inductance depends only on the layers' distance: one kernel per pair, symmetric in s, t
                kx, ky, c, sx, sy = _kernel_pair(nx, ny, d, abs(self.zs[s] - self.zs[t]))
                Kx[:, :, s, t] = Kx[:, :, t, s] = kx
                Ky[:, :, s, t] = Ky[:, :, t, s] = ky
                centre[s, t] = centre[t, s] = c
                sum_x[s, t] = sum_x[t, s] = sx
                sum_y[s, t] = sum_y[t, s] = sy
        self._kq = {"x": Kx, "y": Ky}
        self.blocks = []
        if self.precond_mode == "sheet" and self.N > 1:
            self.B_x = _sheet_blocks(self.L_self, centre, sum_x)
            self.B_y = _sheet_blocks(self.L_self, centre, sum_y)
            self._block_layout()

    def _block_layout(self):
        """Strips at the same (orientation, i, j) on different layers form one preconditioner block;
        positions are grouped by which layers are present (a bitmask)."""
        for idx, mask_arr, off, orient in ((self.xa, self.xb, 0, "x"), (self.ya, self.yb, self.nxb, "y")):
            if len(idx) == 0:
                continue
            sidx = -np.ones(mask_arr.shape, int)
            sidx[tuple(idx.T)] = off + np.arange(len(idx))
            bits = np.zeros(mask_arr.shape[1:], np.int64)
            for k in range(self.N):
                bits |= mask_arr[k].astype(np.int64) << k
            pos = np.nonzero(bits)
            codes = bits[pos]
            for code in np.unique(codes):
                layers = [k for k in range(self.N) if code >> k & 1]
                if len(layers) < 2:
                    continue
                sel = codes == code
                strips = np.stack([sidx[k, pos[0][sel], pos[1][sel]] for k in layers], axis=1)
                self.blocks.append((strips, layers, orient))

    def _conv(self, X: np.ndarray, orient: str) -> np.ndarray:
        """Σ_s K[t,s] ⊛ X[s] for every layer t (double precision, see module docstring), on the whole padded FFT
        grid: the caller picks its strips out of it."""
        P = self.Px if orient == "x" else self.Py
        # convolution theorem: FFT each layer's currents (zero-padded to P), multiply by the kernel spectra and
        # sum over source layers per pixel, inverse FFT
        F = fft2(X, s=P, axes=(1, 2), workers=fft_workers())
        out = np.empty_like(F)
        mac = _kernel_mac()
        if mac is not None:
            mac(self._kq[orient], F, out)
        else:
            tmp = np.empty_like(F[0])
            for t in range(self.N):
                np.multiply(self.kernel(orient, t, 0), F[0], out=out[t])
                for s in range(1, self.N):
                    np.multiply(self.kernel(orient, t, s), F[s], out=tmp)
                    out[t] += tmp
        return ifft2(out, axes=(1, 2), workers=fft_workers(), overwrite_x=True)

    def kernel(self, orient: str, t: int, s: int) -> np.ndarray:
        """The full (P0, P1) kernel spectrum between layers t and s, unfolded from its stored quadrant (a new
        array: the numpy fallback of _conv, the GPU backend, tools/dev/solver_bench.py)."""
        Kq, P = (self._kq["x"], self.Px) if orient == "x" else (self._kq["y"], self.Py)
        # pixel i of the full spectrum is quadrant row min(i, P0 - i) (the spectrum is even)
        i = np.arange(P[0])
        j = np.arange(P[1])
        return Kq[np.minimum(i, P[0] - i)[:, None], np.minimum(j, P[1] - j)[None, :], t, s]

    @property
    def Kx(self) -> dict:
        """{(t, s): the x strips' full kernel spectrum between layers t and s} (built on demand: see kernel)."""
        return {(t, s): self.kernel("x", t, s) for t in range(self.N) for s in range(self.N)}

    @property
    def Ky(self) -> dict:
        """{(t, s): the y strips' full kernel spectrum} (as Kx)."""
        return {(t, s): self.kernel("y", t, s) for t in range(self.N) for s in range(self.N)}

    def _strip_index(self, orient: str) -> tuple[np.ndarray, np.ndarray]:
        """Flat indices of the orientation's strips in the (N, nx', ny') current array and in the padded
        convolution result (fancy indexing with three index arrays took ~5 % of an iteration)."""
        cache = self.__dict__.setdefault("_flat", {})
        idx = cache.get(orient)
        if idx is None:
            a, shape, P = ((self.xa, (self.N, self.nx - 1, self.ny), self.Px) if orient == "x"
                           else (self.ya, (self.N, self.nx, self.ny - 1), self.Py))
            idx = (np.ravel_multi_index(tuple(a.T), shape), np.ravel_multi_index(tuple(a.T), (self.N, *P)))
            cache[orient] = idx
        return idx

    def Lp_matvec(self, I_s: np.ndarray) -> np.ndarray:
        """L_p · I for the strip currents (length ns)."""
        out = np.empty(self.ns, complex)
        for orient, lo, hi, shape in (("x", 0, self.nxb, (self.N, self.nx - 1, self.ny)),
                                      ("y", self.nxb, self.ns, (self.N, self.nx, self.ny - 1))):
            if hi > lo:
                # scatter the strip currents into a dense (layer, i, j) array (zero where there is no strip),
                # convolve, gather the result back at the strips
                into, outof = self._strip_index(orient)
                X = np.zeros(shape[0] * shape[1] * shape[2], complex)
                X[into] = I_s[lo:hi]
                out[lo:hi] = self._conv(X.reshape(shape), orient).reshape(-1)[outof]
        if self.dL is not None:             # strips narrower than the pitch (_widths)
            out += self.dL * I_s
            if self.M_near is not None:
                out += self.M_near @ I_s
        return out

    # ------------------------------------------------------------------ operators
    def surface_resistance(self, f: float, t: float) -> float:
        """Sheet resistance (ohm per square) of copper t thick at f: 1/(sigma t) at DC, the skin depth's [Ramo 1994]
        effective thickness delta (1 - e^(-t/delta)) above [A2] (as track_impedance). A square strip's R is its sheet
        resistance."""
        if f <= 0:
            return 1 / (self.sigma * t)
        delta = 1 / math.sqrt(math.pi * f * MU0 * self.sigma)
        return 1 / (self.sigma * delta * (1 - math.exp(-t / delta)))

    def _impedances(self, f: float):
        """(w, each strip's resistance, the impedances of the other unknowns: capacitances, then branches) at f.
        The strips' inductance is not here: it is the L_p product (Lp_matvec)."""
        w = 2 * math.pi * f
        rs = np.array([self.surface_resistance(f, l.thickness) for l in self.layers])
        # a square strip's R is the sheet resistance; one φ of the pitch wide, sheet resistance / φ
        R_strip = (np.concatenate([rs[self.xa[:, 0]], rs[self.ya[:, 0]]]) / self.strip_width if self.ns
                   else np.zeros(0))
        if self.ncap:   # Y = jωC(1 - j·tanδ): susceptance plus dielectric-loss conductance [Pozar 2012]
            C, tand = self.cap[:, 4], self.cap[:, 5]
            z_cap = 1.0 / (w * C * tand + 1j * w * C) if w > 0 else np.full(self.ncap, 1e18 + 0j)
        else:
            z_cap = np.zeros(0, complex)
        z_br = np.array([complex(b.z(f)) for b in self.branches], complex)
        return w, R_strip, np.concatenate([z_cap, z_br])

    def next_round(self):
        """A new round of solves starts (analysis.simulate.solve_setups calls it between rounds): the
        factorisations decided before it may be borrowed from now on, those of the round itself only by the next.
        Also the base's, for a derived stack."""
        with self._lock:
            self._round += 1
        if self._base is not None:
            self._base.next_round()

    def operator(self, f: float, exact: bool = False) -> _Operator:
        """System data and preconditioner at f (cached; shared by every route and candidate on this stack).

        The system is always evaluated exactly at f. Its preconditioner may be borrowed from the nearest
        factorisation within PRECOND_REUSE: on the real 4-layer board, factorisations 6-29 % away needed the same
        GMRES iterations as a fresh one (57 % away: +13 %), and each borrow saves a sparse LU. exact=True
        factorises at f (used when a borrowed preconditioner did not converge within its budget). Thread-safe:
        when several threads need the same factorisation, one makes it and the others wait for its result.

        Reproducible: which factorisation a frequency uses is decided once, from those decided in earlier rounds
        (next_round), and kept (`_plan`). It used to be the nearest one cached at that moment, which depended on
        the threads' timing (which solve of a round finished first) and on what OPERATOR_CACHE had evicted; the
        borrow window and budget also used measured seconds. Two runs of the same board differed by up to
        0.002 dB, and their iteration counts by 20 %. A factorisation that was freed is made again, identically."""
        key = round(f, 3)
        while True:
            with self._lock:
                if exact or f <= 0:
                    fkey = self._own(key, exact)
                else:
                    fkey = self._plan.get(key)
                    if fkey is None:
                        fkey = self._plan[key] = self._choose(f, key)
                m = self._made.get(fkey)
                if m is None:                   # its own factorisation, decided now
                    m = self._made[fkey] = _Made(f, fkey[1], self._round)
                op = self._ops.get(fkey)
                if op is not None:
                    self._ops.move_to_end(fkey)
                    return self._lend(f, key, op, m)
                build = self._building.get(fkey)
                if build is None:
                    build = self._building[fkey] = _Build()
                    break
            build.done.wait()           # another thread is making this factorisation: use its result
            if build.op is not None:
                with self._lock:
                    return self._lend(f, key, build.op, m)
        try:
            op = self._factorise(m.f, m.exact)
            op.made = m
            with self._lock:
                m.f_lu, m.ref = op.f_lu, op.ref
                self._store(fkey, op)
                build.op = op
        finally:
            with self._lock:
                self._building.pop(fkey, None)
            build.done.set()
        with self._lock:
            return self._lend(f, key, op, m)

    def _own(self, key: float, exact: bool) -> tuple:
        """The key of this stack's own factorisation at a frequency: on a derived stack an exact one differs (it
        is built on the base's exact factorisation, the other one on whatever the base uses there)."""
        return key, bool(exact and self._base is not None)

    def _choose(self, f: float, key: float) -> tuple:
        """The factorisation a frequency uses: the nearest one within PRECOND_REUSE (log ratio of its
        preconditioner's frequency) among those decided in earlier rounds, else its own. Ties go to the lower
        frequency: never to whichever came first."""
        best = None
        for k, m in self._made.items():
            if m.round >= self._round or not m.f_lu:      # this round's, or one that could not be made
                continue
            d = abs(math.log(f / m.f_lu))
            if d < PRECOND_REUSE and (best is None or (d, m.f, m.exact) < best[0]):
                best = ((d, m.f, m.exact), k)
        return best[1] if best is not None else self._own(key, False)

    def _lend(self, f: float, key: float, op: _Operator, m: _Made) -> _Operator:
        """The operator at f with the factorisation `op` (record m) as its preconditioner (lock held)."""
        if round(m.f, 3) == key:
            return op
        view = ("at", key)
        v = self._ops.get(view)
        if v is None or v.src is not m:
            w, R_strip, z_tail = self._impedances(f)
            v = _Operator(w, R_strip, z_tail, op.Dinv, op.solve_S, op.f_lu, src=m)
        self._store(view, v)
        return v

    def _home(self, m: _Made, its: int):
        """An unguessed solve at a factorisation's own frequency took `its` iterations: the basis of borrow
        budgets. Only in the round the factorisation was decided in (all of them have finished before anyone can
        borrow it), the fewest of them: the same whichever finished first."""
        with self._lock:
            if m.round == self._round:
                m.its = its if m.its is None else min(m.its, its)

    def _store(self, key, op):
        """Keep an operator in the cache, the most recently used last; the least recently used beyond
        OPERATOR_CACHE are dropped (and with them their factorisations)."""
        self._ops[key] = op
        self._ops.move_to_end(key)
        while len(self._ops) > OPERATOR_CACHE:
            self._ops.popitem(last=False)

    def _factorise(self, f: float, exact: bool) -> _Operator:
        """The operator at f with a preconditioner made for it. A stack derived from a base (with_branches) builds
        on the base's factorisation with a Woodbury update for its few extra branches; otherwise the Schur
        complement S = A^T D^-1 A is factorised by SuperLU (in a host thread)."""
        w, R_strip, z_tail = self._impedances(f)
        Dinv = self._preconditioner_admittance(w, R_strip, z_tail)
        base = self._base
        f_lu = f
        ref = None
        if base is not None and base.nb <= self.nb:
            bop = base.operator(f, exact)
            f_lu = bop.f_lu
            ref = bop.src or bop.ref     # borrowed by the base: this one is as good (or bad) as that borrow
            k0 = base.nb
            U = self.ArT[:, k0:].toarray()                      # Schur change: S = S_base + U·diag(1/z_new)·Uᵀ
            SiU = bop.solve_S(U)
            small = np.diag(z_tail[k0 - self.ns:]) + U.T @ SiU   # C⁻¹ + Uᵀ S⁻¹ U   [Woodbury 1950]
            small_inv = np.linalg.inv(small)

            def solve_S(v, b=bop.solve_S, SiU=SiU, Ut=U.T, small_inv=small_inv):
                """(S_base + U C U^T)^-1 v by the Woodbury identity: S_base^-1 v - S_base^-1 U (C^-1 + U^T
                S_base^-1 U)^-1 U^T S_base^-1 v; only a k x k matrix (k = extra branches) is inverted."""
                y = b(v)
                return y - SiU @ (small_inv @ (Ut @ y))
        else:
            S = (self.ArT @ Dinv @ self.Ar).tocsc()
            # S is complex symmetric: symmetric mode keeps the A+Aᵀ ordering only when the diagonal pivots are
            # taken (SuperLU's advice: a small pivot threshold). With the default threshold 1.0 it pivoted off
            # the diagonal: kraft_flux_mini_2's 6-layer board-wide model (34k unknowns) took 11.4 s and 10.4 M
            # fill, 3.4 s and 5.8 M with 0.1 (same residual; solves 31 -> 21 ms). Small pivots still pivot.
            if getattr(self, "lu_too_large", None):         # this model failed even with diagonal pivots
                raise SolverError(self.lu_too_large)
            # once a factorisation of this model overflowed SuperLU, the others take diagonal pivots at once
            # (_factorise_diagonal): the usual attempt would only grow to the same limit again (minutes, ~22 GB)
            thresh = 0.0 if getattr(self, "lu_fallbacks", 0) else LU_PIVOT_THRESHOLD
            try:                    # made (and freed) in a host thread: see _LuHost
                lu = hosted_splu(S, permc_spec="MMD_AT_PLUS_A", diag_pivot_thresh=thresh,
                                 options={"SymmetricMode": True})
            except RuntimeError as e:
                # SuperLU reports a failed allocation as a RuntimeError: as a MemoryError the scheduler retries the
                # solve on its own once the others have finished (simulate.run_parallel)
                if "SUPERLU_MALLOC" in str(e):
                    raise MemoryError(f"the sparse factorisation ran out of memory ({S.shape[0]} unknowns)") from e
                raise
            except SystemError as e:
                if "invalid arguments" not in str(e):
                    raise
                if thresh == 0.0:                           # diagonal pivots already: too large for SuperLU
                    self.lu_too_large = too_large_text(S.shape[0], e)
                    raise SolverError(self.lu_too_large) from e
                lu = self._factorise_diagonal(S, e)
            solve_S = lu.solve
        return _Operator(w, R_strip, z_tail, Dinv, solve_S, f_lu, ref=ref)

    def _factorise_diagonal(self, S, first: BaseException):
        """S factorised again with diagonal pivots only, after SuperLU gave up on its size: its row and column
        counts are 32-bit integers, so a factorisation past ~2.1 billion entries (~34 GB complex) overflows and is
        reported as "gstrf was called with invalid arguments" (a 1.9 M-node power stage at 25x on the user's
        62 GB server: peak 21.9 GB, the next growth step overflowed, the job failed after 30 min). Off-diagonal
        pivots add fill; with diag_pivot_thresh 0 the fill is just the ordering's, the least SuperLU can do, and a
        preconditioner needs no more (GMRES corrects it). Counted in `lu_fallbacks` (the run notes it). Too large
        even so: a SolverError naming the limit (the frequency is left unsolved, with that note), not a crash."""
        self.lu_fallbacks = getattr(self, "lu_fallbacks", 0) + 1
        try:
            return hosted_splu(S, permc_spec="MMD_AT_PLUS_A", diag_pivot_thresh=0.0,
                               options={"SymmetricMode": True})
        except (SystemError, RuntimeError) as e:
            self.lu_too_large = too_large_text(S.shape[0], e)
            raise SolverError(self.lu_too_large) from e

    def _preconditioner_admittance(self, w: float, R_strip: np.ndarray, z_tail: np.ndarray) -> sp.csr_matrix:
        """Inverse of the preconditioner's branch impedance: diag(Z), or for 'sheet' per-position layer
        blocks that keep each plane pair's true sheet (cavity) inductance."""
        # each strip with only its own partial self inductance (all mutual coupling dropped), the other branches
        # with their exact impedances
        L_own = self.L_self if self.dL is None else self.L_self + self.dL
        d = np.concatenate([1.0 / (R_strip + 1j * w * L_own), 1.0 / z_tail])
        if not self.blocks:
            return sp.diags(d).tocsr()
        rows, cols, vals = [np.arange(self.nb)], [np.arange(self.nb)], [d]
        in_block = np.zeros(self.nb, bool)
        for strips, layers, orient in self.blocks:
            B = (self.B_x if orient == "x" else self.B_y)[np.ix_(layers, layers)]
            m = len(layers)
            if self.dL is None:                 # every strip full width: every position alike, one inverse
                R = np.array([R_strip[strips[0, k]] for k in range(m)])
                Zi = np.linalg.inv(np.diag(R).astype(complex) + 1j * w * B)
                Zi = np.broadcast_to(Zi.ravel(), (len(strips), m * m)).ravel()
            else:               # strips narrower than the pitch: each position its own R and self inductance
                Z = np.broadcast_to(1j * w * B, (len(strips), m, m)).copy()
                Z[:, np.arange(m), np.arange(m)] += R_strip[strips] + 1j * w * self.dL[strips]
                Zi = np.linalg.inv(Z).ravel()
            rows.append(np.repeat(strips, m, axis=1).ravel())
            cols.append(np.tile(strips, (1, m)).ravel())
            vals.append(Zi)
            in_block[strips.ravel()] = True
        vals[0] = np.where(in_block, 0.0, d)     # block strips replace their diagonal entries
        return sp.csr_matrix((np.concatenate(vals), (np.concatenate(rows), np.concatenate(cols))),
                             shape=(self.nb, self.nb))

    def solve(self, f: float, rhs_top: np.ndarray, J: np.ndarray, tol: float = 1e-5, max_cycles: int = 8,
              guess: Optional[list[np.ndarray]] = None) -> tuple[np.ndarray, np.ndarray, int, np.ndarray]:
        """Solve K·[I; V] = [rhs_top; J]. Returns branch currents (nb), node potentials (n_nodes), iterations
        and the unknown vector.

        guess: a few vectors spanning a likely solution (e.g. the baseline solution of a candidate fix). The
        best combination of them (least squares, one product each) starts GMRES, which then only has to
        remove the remaining residual; a guess that does not lower the residual is ignored.

        Inside `gpu.use(...)` with a device, the iterations run there (solver/gpu.py); a solve the device cannot
        finish (out of memory, a missing operation) is done again here on the CPU and counted in the run."""
        op = self.operator(f)
        run = gpu.active()
        if run is not None:
            try:
                return self._solve(f, op, rhs_top, J, tol, max_cycles, guess, gpu.TorchMath(run, self))
            except SolverError:
                raise
            except Exception as e:      # noqa: BLE001 - any device failure: the CPU does this solve
                run.failed(e)
        return self._solve(f, op, rhs_top, J, tol, max_cycles, guess, _HostMath(self))

    def _solve(self, f, op, rhs_top, J, tol, max_cycles, guess, m) -> tuple:
        """solve() with the matrix and vector work of `m` (_HostMath: numpy; gpu.TorchMath: a device)."""
        nb = self.nb
        # the right-hand side: the induced EMFs (KVL rows) and the injections (KCL rows, reference nodes dropped)
        rhs_h = np.concatenate([rhs_top.astype(complex), J[self.keep].astype(complex)])
        bnorm = float(np.linalg.norm(rhs_h))
        rhs = m.dev(rhs_h)
        x0, r0 = None, rhs
        G = [g for g in (guess or []) if g is not None and len(g) == len(rhs_h) and np.any(g)]
        if G:
            # the warm start: x0 = G c with c minimising ||K G c - b|| (one product per guess vector)
            KG = np.stack([m.host(m.matvec(op, m.dev(g))) for g in G], axis=1)
            c, *_ = np.linalg.lstsq(KG, rhs_h, rcond=None)
            r = rhs_h - KG @ c
            if np.linalg.norm(r) < 0.9 * bnorm:
                x0, r0 = m.dev(np.stack(G, axis=1) @ c), m.dev(r)
        # right preconditioning: (K·M⁻¹)·y = r0, x = x0 + M⁻¹·y  -> the Krylov residual is the true residual
        borrowed = op.f_lu != f
        y, its, ok = _gmres(lambda v: m.matvec(op, m.precond(op, v)), r0, tol, max_cycles=max_cycles, bnorm=bnorm,
                            max_its=op.borrow_budget() if borrowed else None, vec=m.vec,
                            extra_cycles=0 if borrowed else GMRES_EXTRA_CYCLES)
        if not ok and borrowed:
            # The borrowed preconditioner is not good enough here (near a resonance it can take 10x the
            # iterations): factorise at f and go on from the solution so far. Without the budget, kraft_flux_mini_2's
            # board-wide model at 1.2 GHz ran 996 iterations (127 s) on a 400 MHz factorisation.
            if m.vec.any(y):
                x0 = m.precond(op, y) if x0 is None else x0 + m.precond(op, y)
                r0 = rhs - m.matvec(op, x0)
            op = self.operator(f, exact=True)
            y, more, ok = _gmres(lambda v: m.matvec(op, m.precond(op, v)), r0, tol, max_cycles=max_cycles,
                                 bnorm=bnorm, vec=m.vec, extra_cycles=GMRES_EXTRA_CYCLES)
            its += more
        elif ok and not borrowed and x0 is None and its and op.made is not None:
            self._home(op.made, its)                                  # what it needs at home (borrow budget)
        if not ok:
            raise SolverError(f"GMRES did not converge at {f:.4g} Hz ({its} iterations)")
        x = m.precond(op, y) if m.vec.any(y) else m.vec.zeros(len(rhs_h))
        if x0 is not None:
            x = x + x0
        x = m.host(x)
        V = np.zeros(self.n_nodes, complex)
        V[self.keep] = x[nb:]
        return x[:nb], V, its, x

    # ------------------------------------------------------------------ coupling to a signal path
    def pos(self, node: tuple[int, int, int]) -> np.ndarray:
        """A node's position (m): x, y from the grid (relative to node (0, 0)), z of its layer."""
        k, i, j = node
        return np.array([i * self.d, j * self.d, self.zs[k]])

    def strip_coupling(self, trace: list[TraceSegment]) -> np.ndarray:
        """ψ = ∫ A·dl along every strip for 1 A of signal current (length ns)."""
        d = self.d
        # The signal's vector potential A, integrated along each strip, is the flux it couples into that strip's
        # KVL loop (emf -jw psi). A strip far from the trace takes A at its centre; near the trace (within
        # 3 pitches) A varies across the strip, so it is averaged over 3x3 Gauss points of the strip's square.
        x, w = leggauss(3)
        q = 0.5 * x
        W = np.outer(w, w).ravel() / 4.0
        qa, ql = (g.ravel() for g in np.meshgrid(q, q, indexing="ij"))
        horiz = [s for s in trace if np.linalg.norm((s.b - s.a)[:2]) > 0]
        psi = np.zeros(self.ns)
        for axis, idx, off in ((0, self.xa, 0), (1, self.ya, self.nxb)):
            if len(idx) == 0:
                continue
            k, i, j = idx[:, 0], idx[:, 1], idx[:, 2]
            centres = np.stack([i * d + (d / 2 if axis == 0 else 0.0), j * d + (d / 2 if axis == 1 else 0.0),
                                self.zs[k]], axis=1)
            acc = np.zeros(len(idx))
            for s in horiz:
                ab = s.b - s.a
                L = np.linalg.norm(ab)
                if abs(ab[axis]) < 1e-15 * L:      # a segment perpendicular to these strips couples nothing
                    continue
                rho_min = max(0.5 * s.width, 1e-7)
                a_c = segment_vector_potential(s.a, s.b, centres, rho_min)
                near = _dist_to_segment(centres, s.a, s.b) < 3 * d + 2 * s.width
                if near.any():
                    c = centres[near]
                    da, dl = (qa, ql) if axis == 0 else (ql, qa)
                    pts = np.stack([c[:, 0, None] + da * d, c[:, 1, None] + dl * d,
                                    np.broadcast_to(c[:, 2, None], (len(c), len(W)))], axis=2)
                    a_q = segment_vector_potential(s.a, s.b, pts.reshape(-1, 3), rho_min).reshape(len(c), len(W))
                    a_c[near] = (a_q * W).sum(1)
                acc += (s.i * ab[axis] / L) * a_c
            psi[off: off + len(idx)] = d * acc
        return psi

    def branch_coupling(self, trace: list[TraceSegment], start: int = 0) -> np.ndarray:
        """ψ of branches[start:]: vias couple to the vertical parts of the signal path (signal vias)."""
        vert = [s for s in trace if abs((s.b - s.a)[2]) > 0 and np.linalg.norm((s.b - s.a)[:2]) < 1e-12]
        out = np.zeros(self.nbr - start)
        for n, b in enumerate(self.branches[start:]):
            if b.kind == "via" and vert:
                pa, pb = self.pos(b.a), self.pos(b.b)
                out[n] = sum(s.i * segment_mutual(s.a, s.b, pa, pb, b.radius) for s in vert)
        return out

    def moment(self, trace: list[TraceSegment], Ib: np.ndarray) -> np.ndarray:
        """m = ½ ∮ r × I dl over the closed loop (signal path, plane strips, displacement currents, branches): the
        magnetic dipole moment [Jackson 1999, §5.6]."""
        # m = 1/2 sum over every piece of the closed loop of r x (I dl); for a straight piece from a to b this is
        # I (a x b) / 2 exactly. The loop is closed (KCL holds), so the result does not depend on the origin.
        m = np.zeros(3, complex)
        for s in trace:
            m += 0.5 * s.i * np.cross(s.a, s.b)
        d = self.d
        Ix, Iy = Ib[: self.nxb], Ib[self.nxb: self.ns]
        k, _, j = self.xa.T if self.nxb else (np.zeros(0, int),) * 3
        z = self.zs[k]
        m[1] += 0.5 * np.sum(z * d * Ix)          # x strip: r × x̂d = (0, z·d, -y·d)
        m[2] += 0.5 * np.sum(-(j * d) * d * Ix)
        k, i, _ = self.ya.T if self.nyb else (np.zeros(0, int),) * 3
        z = self.zs[k]
        m[0] += 0.5 * np.sum(-z * d * Iy)         # y strip: r × ŷd = (-z·d, 0, x·d)
        m[2] += 0.5 * np.sum((i * d) * d * Iy)
        I_cap = Ib[self.ns: self.ns + self.ncap]
        if len(I_cap):   # vertical (x, y, za) -> (x, y, zb): ½ a×b = ½ (y(zb-za), x(za-zb), 0)
            cu = self.cap[:, :4].astype(int)
            x, y = cu[:, 2] * d, cu[:, 3] * d
            dz = self.zs[cu[:, 1]] - self.zs[cu[:, 0]]
            m[0] += 0.5 * np.sum(y * dz * I_cap)
            m[1] += 0.5 * np.sum(-x * dz * I_cap)
        for b, cur in zip(self.branches, Ib[self.ns + self.ncap:]):
            m += 0.5 * np.cross(self.pos(b.a), self.pos(b.b)) * cur
        return m


class RouteExcitation:
    """A current loop on a PlaneStack: a known current along the trace chain (filaments), plus currents
    injected into the solved copper.

    A signal on tracks is one pair: -1 A at the driver's return node, +1 A at the receiver's. Where the
    route runs through a copper pour, the pour is solved copper too and gets its own pair (+1 A where the
    current enters it, -1 A where it leaves), so the current spreads through the pour instead of following a
    thin line. A power stage's switching cell injects at its switches' terminals. Every connected piece of
    copper must receive as much current as it gives."""

    def __init__(self, stack: PlaneStack, trace: list[TraceSegment], inject_driver: Optional[tuple] = None,
                 inject_receiver: Optional[tuple] = None, trace_thickness: float = 35e-6,
                 strip_psi: Optional[np.ndarray] = None, branch_psi: Optional[np.ndarray] = None,
                 injections: Optional[list[tuple[tuple, float]]] = None):
        """A loop on `stack`: the known current along `trace`, -1 A at inject_driver and +1 A at
        inject_receiver ((layer, i, j) nodes) and any further `injections` [(node, A)]. The coupling psi of the
        trace into every strip and branch is computed here (or reused: strip_psi / branch_psi, for the same loop on
        a derived stack). SolverError when an injection is off copper or the currents don't balance."""
        self.stack = stack
        self.trace = trace
        self.trace_t = trace_thickness
        self.inject = (inject_driver, inject_receiver)
        self.extra = list(injections or [])
        # the injections: what the signal's return draws from the planes at the driver end, gives at the
        # receiver end, and any explicit ones (a pour on the route, a switching cell)
        terms = []
        if inject_driver is not None:
            terms.append((inject_driver, -1.0, "driver return node"))
        if inject_receiver is not None:
            terms.append((inject_receiver, 1.0, "receiver return node"))
        terms += [(node, float(amps), f"current injection at {node}") for node, amps in self.extra]
        if not terms:
            raise SolverError("no current is injected")
        ids = []
        for node, amps, what in terms:
            n = stack.nid[node]
            if n < 0:
                raise SolverError(f"{what} is not on plane copper")
            ids.append((int(n), amps))
        # every separate piece of copper must get as much current as it gives (KCL for the piece as a whole),
        # or the system has no solution
        balance: dict[int, float] = {}
        for n, amps in ids:
            comp = int(stack.full_label[n])
            balance[comp] = balance.get(comp, 0.0) + amps
        if any(abs(v) > 1e-9 for v in balance.values()):
            if inject_driver is not None and inject_receiver is not None and not self.extra:
                raise SolverError("receiver return node is not connected to the driver's return node")
            raise SolverError("the injected currents do not balance on every connected piece of copper")
        self.injections = ids
        if inject_driver is not None and inject_receiver is not None:
            self.src, self.dst = ids[0][0], ids[1][0]
        else:
            self.src = self.dst = ids[0][0]
        # DC path: within each piece of copper, all injection nodes are joined by conductors (not only by
        # plane capacitance)
        groups: dict[int, set] = {}
        for n, _ in ids:
            groups.setdefault(int(stack.full_label[n]), set()).add(int(stack.cond_label[n]))
        self.dc_connected = all(len(g) == 1 for g in groups.values())
        sp_ = stack.strip_coupling(trace) if strip_psi is None else strip_psi
        bp = branch_psi if branch_psi is not None else np.zeros(0)
        if len(bp) < stack.nbr:
            bp = np.concatenate([bp, stack.branch_coupling(trace, start=len(bp))])
        self.strip_psi, self.branch_psi = sp_, bp
        self.psi = np.concatenate([sp_, np.zeros(stack.ncap), bp])
        self.L_trace = _trace_self(trace, trace_thickness) if trace else 0.0

    def on(self, stack: PlaneStack) -> "RouteExcitation":
        """The same loop on a derived stack (with_branches): strip coupling is reused."""
        return RouteExcitation(stack, self.trace, *self.inject, self.trace_t, self.strip_psi, self.branch_psi,
                               self.extra)

    # compatibility helpers
    @property
    def n_nodes(self) -> int:
        """The stack's node count."""
        return self.stack.n_nodes

    @property
    def branches(self) -> list[Branch]:
        """The stack's lumped branches."""
        return self.stack.branches

    def solve(self, f: float, guess: Optional[list[np.ndarray]] = None) -> Solution:
        """guess: vectors in this stack's unknowns to start from (see PlaneStack.solve)."""
        st = self.stack
        w = 2 * math.pi * f
        J = np.zeros(st.n_nodes)
        for n, amps in self.injections:
            J[n] += amps
        # the known signal current induces -jw psi in every strip and via loop; the injections are the sources
        Ib, V, its, x = st.solve(f, -1j * w * self.psi, J, guess=guess)
        I_x = np.zeros((st.N, st.nx - 1, st.ny), complex)
        I_y = np.zeros((st.N, st.nx, st.ny - 1), complex)
        I_x[tuple(st.xa.T)] = Ib[: st.nxb]
        I_y[tuple(st.ya.T)] = Ib[st.nxb: st.ns]
        Vg = np.zeros((st.N, st.nx, st.ny), complex)
        Vg[np.nonzero(st.node)] = V
        # loop impedance: drop along the filaments + Σ J·V over the injections (current enters at +, leaves at -)
        v_inj = sum(amps * V[n] for n, amps in self.injections)
        Z = complex(_trace_resistance(self.trace, f, st.sigma, self.trace_t) + v_inj
                    + 1j * w * (self.L_trace + np.dot(self.psi, Ib)))
        # copies: views of Ib would keep the whole unknown vector x alive (Ib is a view of it). Not x itself either:
        # Solution.state_for rebuilds it from these when the fix search wants a warm start
        return Solution(f, I_x, I_y, Vg, Ib[st.ns + st.ncap:].copy(), Z, st.moment(self.trace, Ib), its,
                        Ib[st.ns: st.ns + st.ncap].copy(), W_x=st.W_x, W_y=st.W_y)


def MultiPlaneProblem(layers: list[PlaneLayer], trace: list[TraceSegment], inject_driver: tuple[int, int, int],
                      inject_receiver: tuple[int, int, int], branches: Optional[list[Branch]] = None,
                      sigma: float = SIGMA_CU, trace_thickness: float = 35e-6,
                      capacitance: Optional[list[PlanePair]] = None) -> RouteExcitation:
    """One-off problem: a stack plus one route."""
    return RouteExcitation(PlaneStack(layers, branches, capacitance, sigma), trace, inject_driver, inject_receiver,
                           trace_thickness)


def PlaneProblem(grid: Grid, trace: list[TraceSegment], inject_driver: tuple[int, int],
                 inject_receiver: tuple[int, int], branches: Optional[list[Branch]] = None,
                 cu_thickness: float = 35e-6, sigma: float = SIGMA_CU,
                 trace_thickness: float = 35e-6) -> RouteExcitation:
    """Single plane at z = 0 (trace z = height above it). Branch nodes may be (i, j) or (0, i, j)."""
    fix = lambda n: n if len(n) == 3 else (0, *n)  # noqa: E731
    br = [Branch(fix(b.a), fix(b.b), b.z, b.name, b.kind, b.radius) for b in (branches or [])]
    return MultiPlaneProblem([PlaneLayer("plane", 0.0, grid, cu_thickness)], trace, fix(inject_driver),
                             fix(inject_receiver), br, sigma, trace_thickness)


# --------------------------------------------------------------------------- the CPU side of a solve
class _HostVectors:
    """GMRES's vector work on numpy arrays (gpu.TorchVectors is the device twin)."""

    @staticmethod
    def zeros(n):
        """A complex zero vector of length n."""
        return np.zeros(n, complex)

    @staticmethod
    def basis(rows, n):
        """An uninitialised (rows, n) array for Krylov basis vectors."""
        return np.empty((rows, n), complex)

    @staticmethod
    def copy(v):
        """A complex copy of v."""
        return v.astype(complex)

    @staticmethod
    def norm(v) -> float:
        """The Euclidean norm of v."""
        return float(np.linalg.norm(v))

    @staticmethod
    def any(v) -> bool:
        """Whether v has a non-zero entry."""
        return bool(v.any())

    @staticmethod
    def project(V, j, w):
        """(Vᴴ·w, w − V·Vᴴ·w) over the first j+1 basis rows: BLAS on the basis in place, no conjugate copy."""
        Vt = V[: j + 1].T                  # F-ordered view
        h = zgemv(1.0, Vt, w, trans=2)
        return h, zgemv(-1.0, Vt, h, beta=1.0, y=w, overwrite_y=1)

    @staticmethod
    def combine(y, V, k):
        """sum_i y[i] V[i] over the first k basis vectors."""
        return y @ V[:k]


_HOST_VECTORS = _HostVectors()


class _HostMath:
    """PlaneStack.solve's matrix work in numpy and SciPy (gpu.TorchMath is the device twin)."""
    vec = _HOST_VECTORS

    def __init__(self, stack: "PlaneStack"):
        """The math for solves on `stack`."""
        self.st = stack

    @staticmethod
    def dev(v):
        """A vector moved to the device: numpy arrays stay as they are."""
        return v

    @staticmethod
    def host(x):
        """A vector back from the device: already a numpy array."""
        return x

    def matvec(self, op: _Operator, x: np.ndarray) -> np.ndarray:
        """The system matrix times x = [I; V]: [Z I - A V; A^T I], with Z I = (R + jw L_p) I for the strips (the
        FFT product) and the diagonal impedances for capacitances and branches."""
        st = self.st
        nb, ns = st.nb, st.ns
        Ib = x[:nb]
        top = np.empty(nb, complex)
        top[:ns] = op.R_strip * Ib[:ns] + 1j * op.w * st.Lp_matvec(Ib[:ns])
        top[ns:] = op.z_tail * Ib[ns:]
        top -= st.Ar @ x[nb:]
        return np.concatenate([top, st.ArT @ Ib])

    def precond(self, op: _Operator, r: np.ndarray) -> np.ndarray:
        """The preconditioner M^-1 r: the saddle-point system with Z replaced by its approximation D, solved
        exactly by block elimination: y = S^-1 (r2 - A^T D^-1 r1) with S = A^T D^-1 A (the factorised Schur
        complement), then the branch part D^-1 (r1 + A y)."""
        st = self.st
        r1, r2 = r[:st.nb], r[st.nb:]
        y = op.solve_S(r2 - st.ArT @ (op.Dinv @ r1))
        return np.concatenate([op.Dinv @ (r1 + st.Ar @ y), y])


# --------------------------------------------------------------------------- helpers
def _gmres(op: Callable, b, tol: float, max_cycles: int = 8,
           restart: Optional[int] = None, bnorm: Optional[float] = None,
           max_its: Optional[int] = None, vec=_HOST_VECTORS, extra_cycles: int = 0) -> tuple:
    """Restarted GMRES [Saad 1986] [Saad 2003] with classical Gram-Schmidt + reorthogonalisation [Giraud 2005] (BLAS
    gemv on the basis, no copies).
    Stops when the true residual ‖b - op(x)‖ ≤ tol·bnorm (default ‖b‖; a warm start passes the norm of the
    original right-hand side with b = its remaining residual), or unconverged after max_cycles restarts or
    max_its iterations (x is then the best so far). extra_cycles more restarts are allowed as long as each one
    still lowers the residual by GMRES_PROGRESS: a short restart (a big model's memory limit) near a plane
    resonance converges steadily but slowly (kraft_flux_mini_2's board-wide model at 4x resolution, 1.58 GHz:
    ~2x per cycle, done after 11 cycles of 52).

    The small least-squares problem is kept triangular with Givens rotations [Saad 2003], so each iteration costs
    O(j) for it: np.linalg.lstsq on the whole Hessenberg matrix every iteration took 18 ms at j = 120 and 44 ms at 200,
    more than the rest of an iteration on long solves."""
    n = len(b)
    if bnorm is None:
        bnorm = vec.norm(b)
    x = vec.zeros(n)
    if bnorm == 0 or vec.norm(b) <= tol * bnorm:
        return x, 0, True
    if restart is None:
        restart = int(min(200, max(20, GMRES_MEMORY // (16 * n))))
    its = 0
    r = vec.copy(b)
    last = math.inf
    # The Krylov basis grows as the iterations need it (GMRES_FIRST_ROWS, doubling up to restart + 1) and is
    # kept across restarts. It was allocated at its full restart length every cycle: up to GMRES_MEMORY (400 MB)
    # committed per solve and thread, though a route model converges in a few iterations.
    V = None
    for cycle in range(max_cycles + extra_cycles):
        beta = vec.norm(r)
        if beta <= tol * bnorm:
            return x, its, True
        if max_its is not None and its >= max_its:
            return x, its, False
        if cycle >= max_cycles and beta > GMRES_PROGRESS * last:
            return x, its, False
        last = beta
        if V is None:
            V = vec.basis(min(restart + 1, GMRES_FIRST_ROWS), n)
        H = np.zeros((restart + 1, restart), complex)     # rotated in place: upper triangular
        cs = np.zeros(restart)
        sn = np.zeros(restart, complex)
        g = np.zeros(restart + 1, complex)                # rotated right-hand side; |g[j+1]| = residual
        g[0] = beta
        V[0] = r / beta
        k = 0
        for j in range(restart):
            w = op(V[j])
            w_norm = vec.norm(w)
            h, w = vec.project(V, j, w)        # h = Vᴴ·w (on the CPU), w -= V·h
            hn = vec.norm(w)
            if hn < 0.7 * w_norm:              # reorthogonalise only when cancellation happened (DGKS)
                h2, w = vec.project(V, j, w)
                h = h + h2
                hn = vec.norm(w)
            col = np.empty(j + 2, complex)
            col[: j + 1] = h
            col[j + 1] = hn
            # Givens rotations keep the Hessenberg matrix upper triangular as it grows: the new column gets the
            # earlier rotations, then a new rotation zeroes its subdiagonal entry; applied to g too, |g[j+1]| is
            # the residual norm at every step without solving anything
            for i in range(j):                 # the earlier rotations, then a new one that zeroes col[j+1]
                a, c = col[i], col[i + 1]
                col[i] = cs[i] * a + sn[i] * c
                col[i + 1] = -np.conj(sn[i]) * a + cs[i] * c
            a = col[j]
            rho = math.hypot(abs(a), hn)
            if rho == 0.0:
                cs[j], sn[j] = 1.0, 0.0
            elif abs(a) == 0.0:
                cs[j], sn[j] = 0.0, 1.0
                col[j] = hn
            else:
                phase = a / abs(a)
                cs[j], sn[j] = abs(a) / rho, phase * hn / rho
                col[j] = phase * rho
            col[j + 1] = 0.0
            H[: j + 2, j] = col
            g[j + 1] = -np.conj(sn[j]) * g[j]
            g[j] = cs[j] * g[j]
            its += 1
            k = j + 1
            if abs(g[j + 1]) <= 0.5 * tol * bnorm or hn <= 1e-14 * np.linalg.norm(h) or \
                    (max_its is not None and its >= max_its):
                break
            if j + 1 >= len(V):                # full: twice the rows (at most restart + 1), the written ones copied
                grown = vec.basis(min(restart + 1, 2 * len(V)), n)
                grown[: j + 1] = V[: j + 1]
                V = grown
            V[j + 1] = w / hn
        while k and H[k - 1, k - 1] == 0:    # an all-zero last column (exact breakdown): nothing to add from it
            k -= 1
        # end of a cycle: the least-squares coefficients by back substitution, the update, and the true residual
        # (recomputed, not the recurrence's: the restart and the stopping test use what is really left)
        y = solve_triangular(H[:k, :k], g[:k]) if k else np.zeros(0, complex)
        x = x + vec.combine(y, V, k)
        r = b - op(x)
    return x, its, vec.norm(r) <= tol * bnorm


def _trace_self(trace: list[TraceSegment], trace_t: float) -> float:
    """Partial self inductance of the whole signal path (all segment pairs, weighted by their currents), as a sum of
    partial self and mutual inductances [Paul 2010]."""
    segs = [s for s in trace if np.linalg.norm(s.b - s.a) > 0]
    L = 0.0
    # L = sum_ij I_i I_j M_ij: the self terms, plus each mutual pair twice (M is symmetric)
    for i, si in enumerate(segs):
        L += si.i * si.i * strip_self(np.linalg.norm(si.b - si.a), si.width, trace_t)
        for sj in segs[i + 1:]:
            L += 2 * si.i * sj.i * segment_mutual(si.a, si.b, sj.a, sj.b, GMD_FLAT * (si.width + trace_t))
    return L


def _trace_resistance(trace: list[TraceSegment], f: float, sigma: float, trace_t: float) -> float:
    """The resistance of the known current path's horizontal segments at f (skin depth as track_impedance [A2]),
    each weighted by the square of its share of the current (I^2 R power, per A)."""
    R = 0.0
    for s in trace:
        L = np.linalg.norm(s.b - s.a)
        if L == 0 or abs(s.b[2] - s.a[2]) > 0:
            continue
        if f > 0:
            delta = 1 / math.sqrt(math.pi * f * MU0 * sigma)
            teff = delta * (1 - math.exp(-trace_t / delta))
        else:
            teff = trace_t
        R += s.i * s.i * L / (sigma * s.width * teff)
    return R


_KERNELS: dict[tuple, tuple[np.ndarray, np.ndarray, float]] = {}
_KERNEL_CACHE_BYTES = 128 * 2**20   # the cache's spectra at most (oldest out first). It held 48 entries before,
                                    # complex-backed: up to ~375 MB on a big board, kept for the process's life
_KERNEL_LOCK = threading.Lock()


def _kernel_pair(nx: int, ny: int, d: float, dz: float) -> tuple:
    """(FFT spectrum x, FFT spectrum y, same-position mutual (self inductance at dz = 0), Σ kernel x, Σ kernel y)
    for strips at layer distance dz (~0.2 s for a 231 x 264 route model). Cached up to _KERNEL_CACHE_BYTES:
    models on the same grid (a stage's own models, a Simulate repeated) reuse them."""
    key = (nx, ny, round(d, 12), round(dz, 12))
    with _KERNEL_LOCK:
        hit = _KERNELS.get(key)
    if hit is None:
        Px = (next_fast_len(2 * (nx - 1) - 1), next_fast_len(2 * ny - 1))
        Py = (next_fast_len(2 * nx - 1), next_fast_len(2 * (ny - 1) - 1))
        kx = strip_kernel(nx - 1, ny, d, dz)
        ky = strip_kernel(ny - 1, nx, d, dz).T          # y strips: the x kernel with the axes swapped
        hit = (_kernel_spectrum(kx, Px), _kernel_spectrum(ky, Py), float(kx[nx - 2, ny - 1]), float(kx.sum()),
               float(ky.sum()))
        with _KERNEL_LOCK:
            _KERNELS[key] = hit
            def held() -> int:
                """Bytes the cached spectra hold."""
                return sum(v[0].nbytes + v[1].nbytes for v in _KERNELS.values())
            while len(_KERNELS) > 1 and held() > _KERNEL_CACHE_BYTES:
                _KERNELS.pop(next(iter(_KERNELS)))
    return hit


def _narrow_self(d: float, phi: float, t: float) -> float:
    """The partial self inductance (H) a strip d long and φd wide adds to the kernels' full square strip: the
    numeric flat-strip integral the kernels' self term is (kernel.strip_self_numeric, zero thickness), and the
    copper thickness t's change to it from the GMD formula (kernel.strip_self), as a difference to the full
    strip's (the GMD formula alone was 1.5 % off the integral: two touching half-pitch strips read 1.5 % above
    the one strip they make). The GMD formula is [Grover 1946]; the split of a strip's partials [A4]."""
    def thick(w):
        """What thickness t lowers a strip w wide by (GMD formula)."""
        return strip_self(d, w) - strip_self(d, w, t)
    return strip_self_numeric(d, phi * d) - strip_self_numeric(d, d) - thick(phi * d) + thick(d)


NEAR_AXIAL = 2      # PlaneStack._widths: side-by-side partial strips corrected up to this many strips along [A4]


def _side_mutual(length: float, wa, wb, gap, steps=0.0, n: int = 8) -> np.ndarray:
    """Mutual partial inductance (H) of two flat coplanar parallel strips `length` long, widths wa and wb, centre
    distance gap across them and `steps` lengths apart along them (wa, wb, gap: fractions of `length`; arrays
    broadcast): each strip n filaments across its width, the mean of their filament mutuals (as
    kernel.strip_self_numeric; filament mutuals [Grover 1946]). Touching filaments are kept a filament's GMD apart."""
    wa, wb, gap, steps = (np.atleast_1d(v) for v in np.broadcast_arrays(*(np.asarray(v, float) for v in
                                                                            (wa, wb, gap, steps))))
    wa, wb, gap = wa * length, wb * length, gap * length
    u = (np.arange(n) + 0.5) / n - 0.5                     # filament positions across a unit-width strip
    ya = wa[:, None, None] * u[None, :, None]
    yb = gap[:, None, None] + wb[:, None, None] * u[None, None, :]
    rho = np.maximum(np.abs(yb - ya), GMD_FLAT * np.maximum(wa, wb)[:, None, None] / n)
    axial = np.broadcast_to((steps * length)[:, None, None], rho.shape)
    return filament_mutual(length, axial, rho).mean(axis=(1, 2))


def _sheet_blocks(L_self: float, centre: np.ndarray, sums: np.ndarray) -> np.ndarray:
    """N×N layer block for strips at one position whose quadratic form gives every layer pair its sheet
    (plane-pair cavity) inductance D_kl = 2·Σ(M_kk − M_kl) ≈ μ0·Δz per square (a parallel-plate line's
    [Pozar 2012]) - the inductance a diagonal preconditioner misses (7x for a 1.2 mm core), which is what makes
    GMRES slow near plane resonances.
    B = β·11ᵀ − D/2, β chosen so a pair's common mode keeps L_self + M_kl and B stays positive definite."""
    N = len(sums)
    D = np.maximum(2.0 * (sums[0, 0] - sums), 0.0)
    np.fill_diagonal(D, 0.0)
    beta = max((L_self + centre[k, l]) / 2 + D[k, l] / 4 for k in range(N) for l in range(N) if k != l)
    B = beta - D / 2
    for _ in range(60):
        if np.linalg.eigvalsh(B).min() > 0.05 * L_self:
            break
        B += 0.25 * L_self
    return B


def _kernel_spectrum(K: np.ndarray, P: tuple[int, int]) -> np.ndarray:
    """FFT of a centred, even kernel embedded circularly in a P-sized array (real-valued): its first quadrant."""
    Z = np.zeros(P)
    Z[: K.shape[0], : K.shape[1]] = K
    # roll the kernel's centre (zero offset) to index (0, 0): negative offsets wrap to the end, so the circular
    # convolution of the padded arrays equals the linear one over the grid
    Z = np.roll(Z, (-(K.shape[0] // 2), -(K.shape[1] // 2)), axis=(0, 1))
    # the quadrant [0, P0 // 2] x [0, P1 // 2] only (the spectrum of a circularly even kernel is even in both axes:
    # PlaneStack._kernels), a copy: a view kept the whole complex result (8x as much) alive
    return np.ascontiguousarray(fft2(Z, workers=-1).real[: P[0] // 2 + 1, : P[1] // 2 + 1])


def _dist_to_segment(P: np.ndarray, a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Distance of each point of P (N, 3) to the segment a-b: to the nearest point on it (the projection, clamped
    to the ends)."""
    ab = b - a
    L2 = float(ab @ ab)
    if L2 == 0:
        return np.linalg.norm(P - a, axis=1)
    t = np.clip((P - a) @ ab / L2, 0, 1)
    return np.linalg.norm(P - (a + t[:, None] * ab), axis=1)
