"""Quasi-static PEEC solver for return currents in a stack of copper layers.

PEEC [Ruehli 1974] with partial inductances [Ruehli 1972] in modified nodal form [Ho 1975]; quasi-static [A1].
Source keys: docs/REFERENCES.md.

Every plane layer is rasterised on the same xy grid. Unknowns are the currents in x/y strips between
neighbouring nodes of each layer, displacement currents between facing planes, currents in vertical and
lumped branches (stitching vias, through-hole pads, decaps, 0R, ferrites), plus all node potentials:

    Z·I - A·V = -jω·ψ        (KVL per branch; ψ = flux coupled from the known signal current)
    Aᵀ·I      = J            (KCL; +1 A injected at the receiver return node, -1 A at the driver's)

Z = R_s(f) + jω·L_p for the strips (R_s with the skin depth [Ramo 1994] [Dowell 1966] [A2]), where L_p holds all mutual
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
import sys
import threading
import time
import weakref
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
from . import sparse as _sparse
from . import stats as _stats
from .kernel import (GMD_FLAT, MU0, filament_mutual, leggauss, segment_mutual, segment_vector_potential, skin_thickness,
                     strip_kernel, strip_self, strip_self_numeric)

SIGMA_CU = 5.8e7            # S/m, annealed copper at 20 °C [IEC 60028]
VIA_PLATING = 25e-6         # m, a via barrel's plating [A3]
EPS0 = 8.8541878128e-12
GMRES_MEMORY = 4e8          # bytes allowed for the Krylov basis (sets the restart length) ...
GMRES_RESTART_MIN = 100     # ... but a restart at least this long: refboard's board-wide model at 8x (~1 M
                            # unknowns) got 26 from 400 MB and stalled near every resonance above 600 MHz (2026-10-18);
                            # with ~150 each solve converged in 116-163 iterations. Costs 1.6 GB per solve at 1 M
                            # unknowns, only when the iterations need it (the basis grows); simulate.solve_bytes counts it
GMRES_FIRST_ROWS = 16       # basis rows allocated at first; doubled as the iterations need them (_gmres)
GMRES_EXTRA_CYCLES = 24     # restarts beyond max_cycles while the residual still falls ...
GMRES_PROGRESS = 0.7        # ... to at most this fraction per restart (stagnation stops at once)
OPERATOR_CACHE = 2          # per-frequency factorisations kept per stack (4 before: see ARCHITECTURE "Memory")
PRECOND_REUSE = 0.25        # reuse a factorisation as preconditioner within ±25 % (log ratio) of its frequency
BORROW_BUDGET = 1.5         # a borrowed preconditioner's GMRES budget: 1.5 x the iterations its source needed at
BORROW_EXTRA_ITS = 15       # home, plus 15
LU_PIVOT_THRESHOLD = 0.1    # SuperLU symmetric mode: take the diagonal pivot unless it is below 0.1 x the column's
PIECE_TOL_MM = 0.005        # one net's copper within this of itself is one piece (touching fills; _pieces)
STRIP_SAMPLES = 6           # lines along each strip where its copper's width is measured (_exact_copper, A39) ...
BAND_STRIPS = 4             # ... for a band of at most this many side-by-side strips (a narrow conductor), with no
                            # other strip of its net within 2 x ADOPT_FACES of it along the border (_series_factor)
ADOPT_FACES = 3             # copper crossing a cell border where its net has no strip: to the nearest strip on that
ADOPT_CELLS = 2             # border line this many cells along it; a cell's copper without a node: to the nearest node
                            # this many cells away (_exact_copper)


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


USE_BASIS32 = True              # GMRES's basis in single precision (solver/_mac.gs_*) when numba is installed ...
BASIS32_MIN_TOL = 1e-6          # ... for tolerances this loose or looser: down to 1e-7 it takes the same iterations as
                                # complex128, at 1e-8 3x more (tests/test_speed.py; the solver's default is 1e-5) [A66]
_GS: list = []                  # [(gs_project, gs_combine) or None] once looked up


def _gs_kernels() -> Optional[tuple]:
    """The compiled single-precision-basis Gram-Schmidt (solver/_mac.py: gs_project, gs_combine), or None (numba
    missing or USE_BASIS32 off: the basis stays complex128, BLAS does the projections)."""
    if not (USE_BASIS32 and USE_NUMBA):
        return None
    if not _GS:
        with _MAC_LOCK:
            if not _GS:
                try:
                    from ._mac import gs_combine, gs_project
                    kernels = (gs_project, gs_combine)
                except Exception:       # noqa: BLE001 - no numba, or one that does not load: complex128 and BLAS
                    kernels = None
                _GS.append(kernels)
    return _GS[0]


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
    area: Optional[np.ndarray] = None    # (nx, ny) the node's own copper in its cell, a fraction of d² (exact, with
                                         # the slivers of cells without a node of that net; None = not measured)
    rx: Optional[np.ndarray] = None      # (nx-1, ny) the x strip's width for its resistance, a fraction of d: wx
    ry: Optional[np.ndarray] = None      # with a change of width along the strip in series (_series_factor; None = wx)
    hx: Optional[np.ndarray] = None      # (nx, ny) mm from the node to where its heat is: the nearest point of its
    hy: Optional[np.ndarray] = None      # copper where the node lies outside it (a cell at the copper's edge), else 0

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
        """Each node's copper as a share of its cell (a fraction of d²), 0 off copper: the measured area (`area`,
        the node's own net in its cell plus the slivers next to it that have no node of their own) where the grid
        has one, else the mean copper width (wx / wy) of the strips that touch it, 1 at a node of full strips (or
        of a grid without widths). A density in the copper (Solution.node_field(copper=True)) is defined with this
        area, so that ρ·|J|²·t·(area·d²) summed over the nodes is the strips' own Joule heat Σ R·I² (exactly, for
        any pattern of currents), and |J| is the current over the copper's real cross-section."""
        out = self.node.astype(float)
        if self.wx is None or self.wy is None:
            return out
        # the strips touching each node: their widths summed and counted per direction, and whether the node has a
        # strip on both sides in x (a run along x) or in y
        sx, nxs, sy, nys = (np.zeros(self.node.shape) for _ in range(4))
        sx[:-1, :] += np.where(self.xb, self.wx, 0.0)
        sx[1:, :] += np.where(self.xb, self.wx, 0.0)
        nxs[:-1, :] += self.xb
        nxs[1:, :] += self.xb
        sy[:, :-1] += np.where(self.yb, self.wy, 0.0)
        sy[:, 1:] += np.where(self.yb, self.wy, 0.0)
        nys[:, :-1] += self.yb
        nys[:, 1:] += self.yb
        n = nxs + nys
        mean_width = np.where(n > 0, (sx + sy) / np.maximum(n, 1), out)
        if self.area is None:
            return mean_width
        cached = self.__dict__.get("_copper_area")
        if cached is not None:
            return cached
        # The measured copper, shared out over the nodes like their strips' widths: each node gets its strips' width
        # times the ratio of measured area to strip width summed over its 3 x 3 neighbourhood of the same net. A track
        # at an angle becomes a staircase whose corner cells hold a sliver of its copper while their strips carry all
        # of its current: over the bare cell area such a node read 2-4 x the track's density. The local total is kept,
        # and with it the density's mean; the heat is exact whatever area a node gets (Solution.node_field). The
        # width is that of the strips along the node's run (x strips where it has one on both sides in x and not in
        # y, and the other way round), else of all its strips: the strips across a wide track, from one cell row of
        # it to the next, carry nothing in a straight run and must not count (they moved a 0.4 mm track's density
        # between its rows by ±5 %). Unchanged where area = width: a straight run, a pour
        x_run, y_run = nxs == 2, nys == 2
        run_width = np.where(x_run & ~y_run, sx / np.maximum(nxs, 1),
                             np.where(y_run & ~x_run, sy / np.maximum(nys, 1), mean_width))
        area = np.where(self.node, self.area, 0.0)
        width = np.where(self.node, run_width, 0.0)
        lab = np.where(self.node, self.label if self.label is not None else 0, -1)
        sum_a, sum_w = np.zeros(area.shape), np.zeros(area.shape)
        nx, ny = area.shape
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                src = (slice(max(di, 0), nx + min(di, 0)), slice(max(dj, 0), ny + min(dj, 0)))
                dst = (slice(max(-di, 0), nx + min(-di, 0)), slice(max(-dj, 0), ny + min(-dj, 0)))
                same = (lab[src] == lab[dst]) & (lab[dst] >= 0)
                sum_a[dst] += np.where(same, area[src], 0.0)
                sum_w[dst] += np.where(same, width[src], 0.0)
        shared = np.where(sum_w > 0, width * sum_a / np.maximum(sum_w, 1e-12), 0.0)
        # a node with no measured copper around it (a via ring or pad seeded into cells its copper only touches):
        # the strips' width, as before
        out = np.where(self.node, np.where(shared > 0, shared, mean_width), 0.0)
        self.__dict__["_copper_area"] = out
        return out

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
    geom_list = list(geoms)                 # kept for the exact widths (the sub-grid branch clears `geoms`)
    fill = None
    fine = sub > 1
    pieces: list = []                       # the fine path: [(net index, one connected piece of its copper)]
    if fine:
        # Each net's copper in its connected pieces (the polygons of its union): a cell keeps one piece, and only
        # cells of the same piece are joined, so copper of one net that does not touch on this layer stays apart
        # however narrow the gap (with the net alone, a track broken by a 0.15 mm gap was solved as if whole: the
        # second piece's walk took the first one's last cell as its own; VALIDATION_AUDIT.md round 5, ISSUE-15)
        for k, g in enumerate(geoms):
            for p in _pieces(g):
                pieces.append((k, p))
        part, cross_x, cross_y, fill, off_x, off_y = _coarse_from_fine([p for _, p in pieces], x0, y0, d, nx, ny,
                                                                       sub)
        net_of = np.array([k for k, _ in pieces] + [-1], int)
        label = net_of[part]                 # part -1 (no copper) maps to the trailing -1
        mids_x = np.where(cross_x > 0, label[:-1, :], -1)
        mids_y = np.where(cross_y > 0, label[:, :-1], -1)
        geoms = []
    else:
        label = -np.ones((nx, ny), int)
        mids_x = np.full((nx - 1, ny), -1)
        mids_y = np.full((nx, ny - 1), -1)
        part = None
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
    if part is None:
        part = label                            # without pieces: the net is the piece (the same array, as before)
    piece_tree = shapely.STRtree([p for _, p in pieces]) if pieces else None

    def piece_at(k: int, px: float, py: float) -> int:
        """The piece of net k's copper that holds (px, py), or a new piece of its own when none does (a seed or
        a track outside the copper geometry); the net itself without pieces."""
        if piece_tree is None:
            return k
        for c in piece_tree.query(shapely.Point(px, py)):
            if pieces[c][0] == k and pieces[c][1].covers(shapely.Point(px, py)):
                return int(c)
        pieces.append((k, None))
        return len(pieces) - 1

    seeded = np.zeros((nx, ny), bool)
    for net, points in (seeds or {}).items():
        if net not in nets:
            continue
        k = nets.index(net)
        for (px, py) in points:
            i, j = int(round((px - x0) / d)), int(round((py - y0) / d))
            if 0 <= i < nx and 0 <= j < ny and label[i, j] < 0:
                label[i, j] = k
                part[i, j] = piece_at(k, px, py)
                seeded[i, j] = True
    forced_x = np.zeros((nx - 1, ny))           # the widest forced track along each strip, mm (0 = none)
    forced_y = np.zeros((nx, ny - 1))
    bridges = []
    for net, polylines in (lines or {}).items():
        if net not in nets:
            continue
        k = nets.index(net)
        owners = [piece_at(k, *_line_point(pts)) if pieces else None for pts, _ in polylines]
        bridges += _rasterise_lines(k, polylines, x0, y0, d, label, seeded, forced_x, forced_y, part, owners)
    # A strip exists if both end nodes and its midpoint are the same net's copper (catches slots >= d/2), of the
    # same piece. A seeded node (via ring / pad smaller than the grid) joins its piece's neighbours without the
    # midpoint test.
    xb = (label[:-1, :] >= 0) & (label[:-1, :] == label[1:, :]) & (part[:-1, :] == part[1:, :]) & (
        (mids_x == label[:-1, :]) | seeded[:-1, :] | seeded[1:, :] | (forced_x > 0))
    yb = (label[:, :-1] >= 0) & (label[:, :-1] == label[:, 1:]) & (part[:, :-1] == part[:, 1:]) & (
        (mids_y == label[:, :-1]) | seeded[:, :-1] | seeded[:, 1:] | (forced_y > 0))
    wx = wy = ox = oy = area = rx = ry = hx = hy = None
    if fine:
        # the strips' widths and the nodes' copper areas from the copper itself (exact), not from the sub-grid's
        # samples: those quantise a width to whole samples (d/sub) and count a track twice where its forced row and
        # a sampled neighbour row both carry it (_exact_copper)
        ex_x, ex_y, area, kx, ky = _exact_copper(geom_list, label, xb, yb, x0, y0, d)
        wx, ox = _strip_widths(xb, cross_x, off_x, forced_x, d, ex_x)
        wy, oy = _strip_widths(yb, cross_y, off_y, forced_y, d, ex_y)
        # the resistance takes a change of width along a strip in series (_series_factor); the inductance keeps the
        # border's width (PlaneStack._widths): its kernels are for strips of one width, at most a pitch
        rx, ry = wx * kx, wy * ky
        hx, hy = _heat_points(geom_list, label, x0, y0, d)
    return Grid(x0, y0, d, nx, ny, label >= 0, xb, yb, label, nets, fill, bridges, wx, wy, ox, oy, area, rx, ry,
                hx, hy)


def _heat_points(geoms: list, label: np.ndarray, x0: float, y0: float, d: float) -> tuple[np.ndarray, np.ndarray]:
    """(hx, hy) (nx, ny), mm: from each node to the nearest point of its own net's copper where the node lies
    outside that copper, 0 elsewhere. The sub-grid gives a cell at the copper's edge a node when one of its samples
    is copper, so the node itself can be up to about d/4 off the copper; its Joule heat is made in the copper and
    goes there (analysis/thermal.copper_heat), not onto the bare laminate beside it, where a thermal mesh finer than
    the grid put it into resin (0.3 W/mK) and read a 0.4 mm track's edge 20 % hot [A44]. A hair past the edge
    (1e-4 mm), so the point lies in the copper's cell, not on its border."""
    nx, ny = label.shape
    hx, hy = np.zeros((nx, ny)), np.zeros((nx, ny))
    for k, g in enumerate(geoms):
        if g is None or g.is_empty:
            continue
        ii, jj = np.nonzero(label == k)
        if len(ii) == 0:
            continue
        px, py = x0 + ii * d, y0 + jj * d
        shapely.prepare(g)
        out = ~shapely.intersects_xy(g, px, py)            # outside, not on the border
        if not out.any():
            continue
        ii, jj, px, py = ii[out], jj[out], px[out], py[out]
        ends = shapely.get_coordinates(shapely.shortest_line(shapely.points(px, py), g))[1::2]
        vx, vy = ends[:, 0] - px, ends[:, 1] - py
        n = np.hypot(vx, vy)
        step = np.where(n > 0, 1e-4 / np.maximum(n, 1e-300), 0.0)
        hx[ii, jj] = vx * (1 + step)
        hy[ii, jj] = vy * (1 + step)
    return hx, hy


def _strip_widths(exists: np.ndarray, cross: np.ndarray, offset: np.ndarray, forced: np.ndarray,
                  d: float, exact: Optional[np.ndarray] = None) -> tuple[np.ndarray, np.ndarray]:
    """Each strip's copper width as a fraction of the pitch d, and where its centre lies across the strip (an
    offset from the cell centre, a fraction of d). The width is `exact` (_exact_copper: the length of the cell
    border its net's copper covers, measured on the copper itself) where that is above zero; else the share of
    its border the sub-grid saw copper cross, at least a forced track's own width (centred where the sub-grid saw
    none); 1 and centred where the strip has neither (a seeded via ring or pad joined to its neighbour). Zero where
    there is no strip.

    A strip used to be a full cell wide wherever it existed, so a neck narrower than the grid pitch conducted as
    if it were a pitch wide: on a 15 x 15 mm island joined to its plane by a 0.35 mm x 3 mm neck over a 0.2 mm
    cavity, the island's resonance moved 735-900 MHz as d went 0.1-1 mm, the neck's L being the grid's. With the
    strip's width, its resistance and partial self inductance are the neck's (PlaneStack._widths; SP-27 in
    tests/test_validation_audit.py, docs/ARCHITECTURE.md "A strip is as wide as the copper it crosses").

    The sub-grid's width alone (2026-10-18 to -19) was whole samples of d/sub: a 0.1 mm track on the DC model's
    0.235 mm cells (samples 0.1175 mm) read 0.1175 mm wide, and where its forced row and a sampled neighbour row
    both carried it, 0.16 mm: its resistance and current density 15-37 % low, a 0.4 mm track's -12 to +17 %, and a
    track at 30-45° 20-57 % too resistive, its strips as wide as the track rather than as the border it crosses
    (w / cos θ). VALIDATION_AUDIT.md round 5, ISSUE-11."""
    w = np.where(forced > 0, np.maximum(cross, np.minimum(forced / d, 1.0)), cross)
    w = np.where(w > 0, np.minimum(w, 1.0), 1.0)
    if exact is not None:
        # a border is at most a pitch long, but a strip also carries the sliver of copper beside it that has no
        # strip of its own (_adopt): a 0.4 mm track over 0.235 mm cells is 0.54 + 1 + 0.16 pitches, its middle strip
        # 1.16 wide (capped at 1, the track lost 9 % of its width); 3 only bounds a pathological case
        w = np.where(exact > 0, np.minimum(exact, 3.0), w)
    o = np.where(cross > 0, offset, 0.0)
    # copper of width w centred at o stays inside the cell: |o| <= (1 - w) / 2 (a track wider than what the
    # sub-grid saw, centred on it); centred when it is a pitch wide or more
    lim = np.maximum((1.0 - w) / 2, 0.0)
    return np.where(exists, w, 0.0), np.where(exists, np.clip(o, -lim, lim), 0.0)


def _ring_edges(geom) -> tuple[np.ndarray, np.ndarray]:
    """The edges of a (multi)polygon's rings, outlines and holes alike, as start and end points (mm), (n, 2) each,
    each outline counter-clockwise and each hole clockwise (the copper on the left of every edge, for _cell_areas);
    lines and points of a geometry collection are left out."""
    parts = np.asarray(_polygons(geom), dtype=object)
    if len(parts) == 0:
        return np.zeros((0, 2)), np.zeros((0, 2))
    if hasattr(shapely, "orient_polygons"):         # shapely 2.1
        parts = shapely.orient_polygons(parts, exterior_cw=False)
    else:
        from shapely.geometry.polygon import orient
        parts = np.asarray([orient(p, 1.0) for p in parts], dtype=object)
    xy, idx = shapely.get_coordinates(shapely.get_rings(parts), return_index=True)
    same = idx[1:] == idx[:-1]              # consecutive points of one ring (closed: its last point is its first)
    return xy[:-1][same], xy[1:][same]


def _line_cover(ua: np.ndarray, va: np.ndarray, ub: np.ndarray, vb: np.ndarray, u0: float, du: float,
                n_lines: int, v0: float, d: float, n_cells: int) -> np.ndarray:
    """(n_lines, n_cells): on each line u = u0 + m·du (m = 0 .. n_lines - 1), the length (mm) of each interval
    [v0 + (c - ½)·d, v0 + (c + ½)·d] along it that lies inside the polygons with the edges (ua, va) → (ub, vb).

    A scanline [Foley 1990]: an edge crosses the lines with u in [min(ua, ub), max(ua, ub)) (half open, so a vertex
    on a line counts once and an edge along a line not at all), the crossings of each line sorted along it bound
    the inside in pairs (even-odd: one net's copper is a valid (multi)polygon, its pieces and holes do not
    overlap), and each inside span adds its exact overlap with every interval."""
    out = np.zeros((n_lines, n_cells))
    if len(ua) == 0 or n_lines <= 0 or n_cells <= 0:
        return out
    lo, hi = np.minimum(ua, ub), np.maximum(ua, ub)
    m0 = np.clip(np.ceil((lo - u0) / du), 0, n_lines).astype(np.int64)
    m1 = np.clip(np.ceil((hi - u0) / du), 0, n_lines).astype(np.int64)
    sel = np.nonzero(m1 > m0)[0]
    if len(sel) == 0:
        return out
    n = (m1 - m0)[sel]
    e = np.repeat(sel, n)                                   # one entry per (edge, line) crossing
    m = np.repeat(m0[sel], n) + (np.arange(int(n.sum())) - np.repeat(np.cumsum(n) - n, n))
    u = u0 + m * du
    v = va[e] + (u - ua[e]) * (vb[e] - va[e]) / (ub[e] - ua[e])
    order = np.lexsort((v, m))
    m, v = m[order], v[order]
    count = np.bincount(m, minlength=n_lines)
    rank = np.arange(len(m)) - np.searchsorted(m, m)        # position of each crossing along its line
    start = np.nonzero((rank % 2 == 0) & (count[m] % 2 == 0))[0]    # a line with an odd count (degenerate): skipped
    lines, a, b = m[start], v[start], v[start + 1]
    vlo, top = v0 - d / 2, n_cells * d
    a, b = np.clip(a - vlo, 0.0, top), np.clip(b - vlo, 0.0, top)
    keep = b > a
    lines, a, b = lines[keep], a[keep], b[keep]
    ca = np.minimum((a / d).astype(np.int64), n_cells - 1)
    cb = np.minimum((b / d).astype(np.int64), n_cells - 1)
    flat = out.reshape(-1)
    size = n_lines * n_cells
    one = ca == cb                                          # a span inside one interval
    flat += np.bincount(lines[one] * n_cells + ca[one], b[one] - a[one], size)
    two = ~one                                              # partial first and last intervals, full ones between
    l2, a2, b2, ca2, cb2 = lines[two], a[two], b[two], ca[two], cb[two]
    flat += np.bincount(l2 * n_cells + ca2, (ca2 + 1) * d - a2, size)
    flat += np.bincount(l2 * n_cells + cb2, b2 - cb2 * d, size)
    full = np.zeros((n_lines, n_cells + 1))
    full.reshape(-1)[:] += np.bincount(l2 * (n_cells + 1) + ca2 + 1, np.full(len(l2), d), n_lines * (n_cells + 1))
    full.reshape(-1)[:] -= np.bincount(l2 * (n_cells + 1) + cb2, np.full(len(l2), d), n_lines * (n_cells + 1))
    out += np.cumsum(full, axis=1)[:, :n_cells]
    return out


def _cell_areas(pa: np.ndarray, pb: np.ndarray, xl: float, yl: float, d: float, wi: int, wj: int,
                right: np.ndarray) -> np.ndarray:
    """(wi, wj): the area (mm²) of each cell [xl + i·d, xl + (i+1)·d] x [yl + j·d, yl + (j+1)·d] that lies inside
    the polygons whose oriented edges are pa → pb (outlines counter-clockwise, holes clockwise: _ring_edges), with
    `right` the covered length (mm) of each cell's right border (_line_cover on x = xl + (i+1)·d).

    Green's theorem [Kreyszig 2011]: a region's area is ∮ x dy around it. A cell's copper is bounded by the
    polygons' edges inside the cell and by the parts of the cell's own border inside the copper: the edges are cut
    at every grid line they cross and each piece adds (x̄ - x_left)·Δy to its cell; along the cell's border, x -
    x_left is d on the right side (traversed upwards where it is copper: d times its covered length) and 0 on the
    left, and the top and bottom have dy = 0. Exact for straight edges (an arc is a polygon here already)."""
    out = np.zeros((wi, wj))
    if len(pa):
        xa, ya, xb, yb = pa[:, 0], pa[:, 1], pb[:, 0], pb[:, 1]
        # the parameters t along each edge where it crosses a vertical or a horizontal grid line, plus its ends
        ts, es = [np.zeros(len(xa)), np.ones(len(xa))], [np.arange(len(xa))] * 2
        for a, b, origin, n in ((xa, xb, xl, wi + 1), (ya, yb, yl, wj + 1)):
            lo, hi = np.minimum(a, b), np.maximum(a, b)
            m0 = np.clip(np.ceil((lo - origin) / d), 0, n).astype(np.int64)
            m1 = np.clip(np.ceil((hi - origin) / d), 0, n).astype(np.int64)
            sel = np.nonzero(m1 > m0)[0]
            cnt = (m1 - m0)[sel]
            e = np.repeat(sel, cnt)
            m = np.repeat(m0[sel], cnt) + (np.arange(int(cnt.sum())) - np.repeat(np.cumsum(cnt) - cnt, cnt))
            ts.append((origin + m * d - a[e]) / (b[e] - a[e]))
            es.append(e)
        t, e = np.concatenate(ts), np.concatenate(es)
        order = np.lexsort((t, e))
        t, e = t[order], e[order]
        same = e[1:] == e[:-1]                       # consecutive parameters of one edge bound a piece
        e, t0, t1 = e[:-1][same], t[:-1][same], t[1:][same]
        x0p, y0p = xa[e] + t0 * (xb[e] - xa[e]), ya[e] + t0 * (yb[e] - ya[e])
        x1p, y1p = xa[e] + t1 * (xb[e] - xa[e]), ya[e] + t1 * (yb[e] - ya[e])
        i = np.floor(((x0p + x1p) / 2 - xl) / d).astype(np.int64)
        j = np.floor(((y0p + y1p) / 2 - yl) / d).astype(np.int64)
        ok = (i >= 0) & (i < wi) & (j >= 0) & (j < wj)
        left = xl + i * d
        contrib = ((x0p + x1p) / 2 - left) * (y1p - y0p)
        out.reshape(-1)[:] += np.bincount(i[ok] * wj + j[ok], contrib[ok], wi * wj)
    return out + d * right


def _adopt(cov: np.ndarray, has: np.ndarray, steps, reach: int = 1) -> np.ndarray:
    """`cov` where `has` (a strip or node of the net exists), plus each value where none exists (`orphan` copper)
    moved to the nearest that has one along the index offsets `steps`, up to `reach` steps away (of two at the same
    distance, the one with more copper of its own). Orphans with none within reach are dropped (copper the model
    has no node or strip of its net near)."""
    own = np.where(has, cov, 0.0)
    orphan = np.where(has, 0.0, cov)
    out = own.copy()
    ni, nj = cov.shape
    for k in range(1, reach + 1):
        if not np.any(orphan > 0):
            break
        best = np.full(cov.shape, -1.0)
        pick = np.full(cov.shape, -1)
        for s, (di, dj) in enumerate(steps):
            di, dj = di * k, dj * k
            # the cell k steps away along (di, dj) of each cell, as arrays aligned with the cell
            nb_has, nb_cov = np.zeros(cov.shape, bool), np.zeros(cov.shape)
            src = (slice(max(di, 0), ni + min(di, 0)), slice(max(dj, 0), nj + min(dj, 0)))
            dst = (slice(max(-di, 0), ni + min(-di, 0)), slice(max(-dj, 0), nj + min(-dj, 0)))
            nb_has[dst], nb_cov[dst] = has[src], own[src]
            better = (orphan > 0) & nb_has & (nb_cov > best)
            best = np.where(better, nb_cov, best)
            pick = np.where(better, s, pick)
        for s, (di, dj) in enumerate(steps):
            di, dj = di * k, dj * k
            moved = np.where(pick == s, orphan, 0.0)
            src = (slice(max(-di, 0), ni + min(-di, 0)), slice(max(-dj, 0), nj + min(-dj, 0)))
            dst = (slice(max(di, 0), ni + min(di, 0)), slice(max(dj, 0), nj + min(dj, 0)))
            out[dst] += moved[src]
        orphan = np.where(pick >= 0, 0.0, orphan)
    return out


def _series_factor(face: np.ndarray, samples: list, has: np.ndarray, axis: int) -> np.ndarray:
    """Per strip, how much a band of strips' conductance changes when its copper's width is taken all along the
    strips' length instead of at the border they cross (_exact_copper): the harmonic mean of the band's total width
    at the sample lines over its total at the border. A band is a run of side-by-side strips (consecutive rows of
    x strips, `axis` 1; columns of y strips, `axis` 0) in one column of strips: together they carry what crosses
    there. A track at any angle has the same total all along (w / cos θ) and keeps its factor 1; at a step from W₁
    to W₂ the band conducts as the two lengths in series, d₁/W₁ + d₂/W₂. A band whose copper vanishes at a sample
    line keeps 1 (its strip stays as the border makes it: the topology decided it conducts).

    Only a narrow conductor on its own is one band in series [A39]: at most BAND_STRIPS strips side by side, and no
    other strip of its net within 2 x ADOPT_FACES of it along the border (else the copper at a sample line can be
    the other run's). Every other band keeps 1, as all bands did before: the strips across a track (a run as long
    as the track; a sample line just past its edge finds only the pads at its ends, and took those strips to 0.24 of
    their width, a 0.4 mm track's density +5 %, a neck island's resonance +2 %), a pour (whose step the 1D series
    model would put on the whole column, also on the lanes that go straight on), a junction with a pad or a branch
    (a staircase step next to a pad read 0.28)."""
    h = has if axis == 1 else has.T
    if not h.any():
        return np.ones(has.shape)
    f_ = face if axis == 1 else face.T
    rows = [s if axis == 1 else s.T for s in samples]
    n0, n1 = h.shape
    start = h & ~np.concatenate([np.zeros((n0, 1), bool), h[:, :-1]], axis=1)
    run = np.cumsum(start, axis=1) - 1                  # the run number along each column (where a strip is)
    r_i, c_i = np.nonzero(h)                            # the strips, column by column, in order along each
    key = r_i * n1 + run[r_i, c_i]
    nk = n0 * n1
    total = np.bincount(key, f_[h], nk)
    size = np.bincount(key, minlength=nk)
    # the lanes without a strip between each run and the run before / after it in the same column (none: far)
    far = n1 + 2 * ADOPT_FACES + 2
    step = np.where(np.r_[True, r_i[1:] != r_i[:-1]], far, np.r_[far, c_i[1:] - c_i[:-1]])
    first = step != 1                                   # the first strip of each run
    gap_before = np.full(nk, far)
    gap_before[key[first]] = step[first] - 1
    gap_after = np.where(np.r_[size[1:] > 0, False], np.r_[gap_before[1:], far], far)   # the next run's, same column
    alone = (size <= BAND_STRIPS) & (gap_before > 2 * ADOPT_FACES) & (gap_after > 2 * ADOPT_FACES)
    inverse, ok = np.zeros(nk), np.ones(nk, bool)
    for s in rows:
        t = np.bincount(key, s[h], nk)
        ok &= t > 0
        inverse += 1.0 / np.maximum(t, 1e-300)
    harmonic = len(rows) / np.maximum(inverse, 1e-300)
    factor = np.where(alone & ok & (total > 0), harmonic / np.maximum(total, 1e-300), 1.0)
    out = np.ones(h.shape)
    out[h] = factor[key]
    return out if axis == 1 else out.T


def _exact_copper(geoms: list, label: np.ndarray, xb: np.ndarray, yb: np.ndarray, x0: float, y0: float,
                  d: float) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """The strips' copper widths and the nodes' copper areas measured on the copper itself, as fractions of d and
    d²: (ex_x (nx-1, ny), ex_y (nx, ny-1), area (nx, ny)); zero where a strip or node has none of its net's copper;
    and each strip's series factor for its resistance (kx, ky; 1 where none: _series_factor).

    A strip's width is the length of the cell border between its two nodes (x = x_i + d/2 across the cell's height,
    or y = y_j + d/2) that its net's copper covers (_line_cover on that border): the face of a finite-volume
    conductance σ·t·w/d [Patankar 1980], which also gives a track at an angle θ its own resistance (it crosses a
    border w / cos θ long, and the staircase of strips it becomes is cos θ as long per strip: [A39]). A node's area
    is its cell's copper of its net, exactly (_cell_areas, Green's theorem on the copper's edges cut by the cell).

    Copper crossing a border where its net has no strip (the sliver of a track in the next cell row, which kept no
    node: the sub-grid saw too little of it there) is added to the nearest strip of its net on the same border line,
    up to ADOPT_FACES cells along it, and a cell's copper of a net with no node there to the nearest node of that net,
    up to ADOPT_CELLS cells away (_adopt): a track straddling two cell rows keeps its whole width and area in the row
    that carries it, and a track at an angle, which crosses a row border over w / sin θ (0.3 mm for 0.15 mm at 30°),
    keeps it in the one step of its staircase there, which the track's walk can put a cell or two from where the
    copper crosses (with one cell's reach, that track read 11 % too resistive)."""
    nx, ny = label.shape
    ex_x, ex_y, area = np.zeros((nx - 1, ny)), np.zeros((nx, ny - 1)), np.zeros((nx, ny))
    kx, ky = np.ones((nx - 1, ny)), np.ones((nx, ny - 1))
    for k, g in enumerate(geoms):
        if g is None or g.is_empty:
            continue
        pa, pb = _ring_edges(g)
        if len(pa) == 0:
            continue
        # the window of cells around this net's copper, with a margin for the slivers' neighbours
        gx0, gy0, gx1, gy1 = g.bounds
        i0, i1 = max(int(math.floor((gx0 - x0) / d)) - 2, 0), min(int(math.ceil((gx1 - x0) / d)) + 3, nx)
        j0, j1 = max(int(math.floor((gy0 - y0) / d)) - 2, 0), min(int(math.ceil((gy1 - y0) / d)) + 3, ny)
        wi, wj = i1 - i0, j1 - j0
        if wi < 2 or wj < 2:
            continue
        xa, ya, xe, ye = pa[:, 0], pa[:, 1], pb[:, 0], pb[:, 1]
        # lines x = x_i - d/2 (the borders between the window's columns: line m is cell i0 + m's left border and cell
        # i0 + m - 1's right one), the covered length of each cell row on them
        cv = _line_cover(xa, ya, xe, ye, x0 + (i0 - 0.5) * d, d, wi + 1, y0 + j0 * d, d, wj)
        # lines y = y_j + d/2 (the borders between the window's rows), the covered length of each cell column
        ch = _line_cover(ya, xa, ye, xe, y0 + (j0 + 0.5) * d, d, wj - 1, x0 + i0 * d, d, wi).T
        own = label[i0:i1, j0:j1] == k
        has_x = xb[i0:i1 - 1, j0:j1] & own[:-1, :]
        has_y = yb[i0:i1, j0:j1 - 1] & own[:, :-1]
        face_x = _adopt(cv[1:wi, :] / d, has_x, ((0, 1), (0, -1)), ADOPT_FACES)
        face_y = _adopt(ch / d, has_y, ((1, 0), (-1, 0)), ADOPT_FACES)
        ex_x[i0:i1 - 1, j0:j1] += face_x
        ex_y[i0:i1, j0:j1 - 1] += face_y
        # the width all along each strip, not only at its border: STRIP_SAMPLES lines across every strip's length
        # (x_i + (q + ½)·d/Q), the copper there adopted as at the border; a narrow band's series factor
        # (_series_factor) places a change of width where it is, to d/(2Q), not anywhere within ±d/2 [A39]
        q_n = STRIP_SAMPLES
        sx = (_line_cover(xa, ya, xe, ye, x0 + (i0 + 0.5 / q_n) * d, d / q_n, (wi - 1) * q_n, y0 + j0 * d, d, wj)
              / d).reshape(wi - 1, q_n, wj)
        sy = (_line_cover(ya, xa, ye, xe, y0 + (j0 + 0.5 / q_n) * d, d / q_n, (wj - 1) * q_n, x0 + i0 * d, d, wi)
              / d).reshape(wj - 1, q_n, wi)
        along_x = [_adopt(sx[:, q, :], has_x, ((0, 1), (0, -1)), ADOPT_FACES) for q in range(q_n)]
        along_y = [_adopt(sy[:, q, :].T, has_y, ((1, 0), (-1, 0)), ADOPT_FACES) for q in range(q_n)]
        win_x, win_y = kx[i0:i1 - 1, j0:j1], ky[i0:i1, j0:j1 - 1]     # views: this net's strips only
        win_x[has_x] = _series_factor(face_x, along_x, has_x, 1)[has_x]
        win_y[has_y] = _series_factor(face_y, along_y, has_y, 0)[has_y]
        cell = _cell_areas(pa, pb, x0 + (i0 - 0.5) * d, y0 + (j0 - 0.5) * d, d, wi, wj, cv[1:wi + 1, :]) / (d * d)
        area[i0:i1, j0:j1] += _adopt(np.clip(cell, 0.0, 1.0), own, ((1, 0), (-1, 0), (0, 1), (0, -1)), ADOPT_CELLS)
    return ex_x, ex_y, area, kx, ky


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


def _pieces(geom, tol: float = PIECE_TOL_MM) -> list:
    """The connected pieces of one net's copper: its polygons, grouped where they touch or lie within `tol` mm of
    each other (as one MultiPolygon). A union keeps two zone fills that only touch (at a point, or along an edge the
    fills' rounding leaves a hair apart) as two polygons: refboard's +VDC on In1.Cu is two such fills, 0.0000
    mm apart, which KiCad and the copper join, and taking each polygon for a piece cut the LM5008's supply off its
    input (its conducted transfer 1e4 x low). Real gaps are tens of µm at the least (a fab's clearance)."""
    polys = _polygons(geom)
    if len(polys) <= 1:
        return polys
    tree = shapely.STRtree(polys)
    try:
        left, right = tree.query(polys, predicate="dwithin", distance=tol)
    except Exception:       # noqa: BLE001 - a GEOS without dwithin: the polygons grown by half the tolerance
        left, right = tree.query([shapely.buffer(p, tol / 2) for p in polys], predicate="intersects")
    n = len(polys)
    _, group = connected_components(sp.coo_matrix((np.ones(len(left)), (left, right)), shape=(n, n)), directed=False)
    out = []
    for g in range(int(group.max()) + 1):
        members = [polys[i] for i in np.nonzero(group == g)[0]]
        out.append(members[0] if len(members) == 1 else shapely.MultiPolygon(members))
    return out


def _polygons(geom) -> list:
    """The polygons of a geometry (the parts of a union), lines and points left out."""
    if geom is None or geom.is_empty:
        return []
    parts = np.asarray([geom], dtype=object)
    for _ in range(4):                      # multi-part geometries and collections, nested a few levels at most
        parts = shapely.get_parts(parts)
        if not np.any(np.isin(shapely.get_type_id(parts), (4, 5, 6, 7))):
            break
    return [p for p in parts if shapely.get_type_id(p) == 3 and not p.is_empty]


def _line_point(pts) -> tuple[float, float]:
    """A point on a track's centreline (the middle of its first segment): which copper piece the track is part of."""
    (ax, ay), (bx, by) = pts[0], pts[1] if len(pts) > 1 else pts[0]
    return (ax + bx) / 2, (ay + by) / 2


def _rasterise_lines(k: int, polylines, x0: float, y0: float, d: float, label: np.ndarray, seeded: np.ndarray,
                     forced_x: np.ndarray, forced_y: np.ndarray, part: Optional[np.ndarray] = None,
                     owners: Optional[list] = None) -> list:
    """Walk each centreline (polyline, width mm) through the grid (4-connected) and claim its nodes and strips
    for net k (forced_x / forced_y get the widest claiming track's width, mm).

    The track's own copper decides which cells: at every point along the centreline the walk takes the nearest
    of the four cells around it that is already k's (the sub-grid gave it the track's piece), else the nearest
    free one; consecutive cells are joined along whichever L-shaped path crosses fewer cells of other nets. A cell
    another net's copper owns (its piece of the cell is larger: a track beside a pour) can't be claimed. The walk
    used to take the nearest cell only and stop at a foreign one, which cut tracks apart: on refboard a
    78 mm +3V3 track on In3, tied to its pour at one end, floated free on the 0.86 and 0.61 mm grids (a half-wave
    resonance at ~1.24 GHz, the board's worst SPI_SCK margin) and was joined on the 0.43 mm grid; on the 0.86 mm
    grid its own cells were the column beside the one the walk took. A gap that is left (all four cells
    foreign) is joined by a bridge: returned as (i0, j0, i1, j1, length along the track mm, width mm), for a
    lumped branch (analysis.simulate adds them, peec.track_impedance).

    With `part` (build_grid's pieces: which connected piece of its net's copper each cell holds) and `owners` (the
    piece of each polyline), a cell is the track's own only when it holds the track's piece: a cell of another piece
    of net k (copper of the same net that the track does not touch) is foreign, so the walk neither joins nor
    claims it."""
    nx, ny = label.shape
    bridges = []
    pid = None                                  # the piece of the polyline being walked (None: the net is enough)

    def inside(c) -> bool:
        """Cell c is on the grid."""
        return 0 <= c[0] < nx and 0 <= c[1] < ny

    def own(c) -> bool:
        """Cell c is already this track's copper: net k's, and of its piece."""
        return inside(c) and label[c] == k and (part is None or pid is None or part[c] == pid)

    def free(c) -> bool:
        """Cell c may be claimed for this track (no copper, or its own)."""
        return inside(c) and (label[c] == -1 or own(c))

    def path(a, b) -> list:
        """4-connected cells from a (exclusive) to b (inclusive): x first or y first, the freer one."""
        sx, sy = (b[0] > a[0]) - (b[0] < a[0]), (b[1] > a[1]) - (b[1] < a[1])
        xs = [(a[0] + sx * n, a[1]) for n in range(1, abs(b[0] - a[0]) + 1)]
        ys = [(a[0], a[1] + sy * n) for n in range(1, abs(b[1] - a[1]) + 1)]
        x_first = xs + [(b[0], c[1]) for c in ys]
        y_first = ys + [(c[0], b[1]) for c in xs]
        return min((x_first, y_first), key=lambda p: sum(not free(c) for c in p))

    for n_line, (pts, width) in enumerate(polylines):
        pid = owners[n_line] if owners is not None else None
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
                if part is not None and pid is not None:
                    part[i, j] = pid
                seeded[i, j] = True
            if not own((i, j)):
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
    kind: str = "part"        # part | via | track (a bridge: Grid.bridges) | bond (a solder joint: LidNode)
    radius: float = 1.5e-4    # via barrel radius (m), for its coupling to the signal via


class LidNode(tuple):
    """A circuit node that is no grid node: the metal of one side of a lumped part (a capacitor's terminal, a FET's
    drain or source) that its pads are soldered to (analysis/simulate.part_lid). Every grid node under those pads is
    joined to it by a `bond` branch, so the copper decides where the current hands over to the part [Berger 1972].

    As a tuple it is the (layer, i, j) of the grid cell it sits over: its position for the loop moment and its layer
    for whatever reads one (transfers, maps). `lid` numbers it within its model: two lids over one cell stay two
    nodes (equality and hash go by the number), and a grid node never equals a lid."""

    def __new__(cls, layer: int, i: int, j: int, lid: int):
        """The lid numbered `lid`, over cell (i, j) of layer `layer`."""
        t = super().__new__(cls, (int(layer), int(i), int(j)))
        t.lid = int(lid)
        return t

    def __getnewargs__(self):
        """What pickle passes to __new__ (a tuple subclass with an argument of its own)."""
        return (self[0], self[1], self[2], self.lid)

    def __eq__(self, other):
        """The same lid: the same number (and cell)."""
        return isinstance(other, LidNode) and other.lid == self.lid and tuple.__eq__(self, other)

    def __ne__(self, other):
        """Not the same lid (a grid node never is one)."""
        return not self.__eq__(other)

    def __hash__(self):
        """By the lid's number: apart from every grid node's tuple."""
        return hash(("lid", self.lid))


@dataclass
class PlanePair:
    """Capacitance between two facing plane layers (indices into the layer list, upper < lower): the parallel-plate
    value per cell, without the fringing field at the copper's edges, beside complete partial inductances [A46]."""
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
        # the copper's thickness for its resistance at f: t at DC, the skin depth's once δ ≪ t (kernel.skin_thickness:
        # the exact one-sided slab [Dowell 1966] [A2])
        t = skin_thickness(thickness_m, f, sigma)
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
    A_cu: Optional[np.ndarray] = None       # (N, nx, ny) the nodes' copper area / d² (the stack's; None: solutions
                                            # pickled before 2026-10-19, whose densities are the strips' mean)
    V_lid: Optional[np.ndarray] = None      # the potentials of the stack's LidNodes (its parts' metal), in order

    def state_for(self, stack: "PlaneStack") -> np.ndarray:
        """The solver's unknown vector [branch currents; node potentials] of this solution on `stack`, the stack
        it was solved on (a warm start for the fix search), rebuilt from the currents and potentials kept here:
        the same numbers. Solutions stored it as well before, a copy of what they hold (tools/dev/mem_profile.py)."""
        if self.state is not None:
            return self.state
        Ib = np.concatenate([self.I_x[tuple(stack.xa.T)], self.I_y[tuple(stack.ya.T)], self.I_cap, self.I_branch])
        n_lid = stack.n_nodes - stack.n_grid
        lids = self.V_lid if self.V_lid is not None and len(self.V_lid) == n_lid else np.zeros(n_lid, complex)
        return np.concatenate([Ib, np.concatenate([self.V[np.nonzero(stack.node)], lids])[stack.keep]])

    @property
    def L_loop(self) -> float:
        """The loop inductance (H): Im(Z_loop) / w."""
        return self.Z_loop.imag / (2 * math.pi * self.f)

    def node_field(self, k: int, d_mm: float, copper: bool = False) -> tuple[np.ndarray, np.ndarray]:
        """Complex (Kx, Ky) at the nodes of layer k (A/m per A of signal): each node averages the strips that
        touch it in each direction. The current per cell width (what a cut across the cells adds up), or with
        copper=True per width of the copper itself: a strip narrower than the pitch (PlaneStack._widths) carries
        its current in φd, not d (a density: weighted by Grid.copper_area in a sum over the cells, the heat
        ρ·K²/t·φd² of a strip is its R/φ·I²).

        With copper=True the vector's direction is that mean, and its length is set by the strips' heat: |K|² =
        Σ ½·|I_s|²/φ_s over the strips touching the node, over its copper area A (Grid.copper_area) and d², so that
        ρ·|K|²/t·A·d² is exactly the node's half of every touching strip's R·|I|² [Tellegen 1952] and |K|/t the
        current density in the copper's real cross-section. The mean alone is right in a straight run but not where
        the current turns: on a track at 45° (a staircase of x and y strips, each φ = w·√2/d) the nodes read √2 × the
        density with twice the strips' heat, and at a junction where current enters and splits the mean cancels
        while every strip carries current (VALIDATION_AUDIT.md round 5, ISSUE-12)."""
        d = d_mm * 1e-3
        Ix, Iy = self.I_x[k], self.I_y[k]
        wx = wy = None
        if copper and self.W_x is not None:
            wx, wy = self.W_x[k], self.W_y[k]
            Ix, Iy = Ix / wx, Iy / wy
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
        kx, ky = kx / np.maximum(cx, 1) / d, ky / np.maximum(cy, 1) / d
        if not copper or self.A_cu is None:
            return kx, ky
        # the node's share of its strips' heat, per ρ/t: Σ ½ |I|² / φ (|I/φ|² · φ: current per copper width, squared,
        # times that width), over its area
        hx = 0.5 * np.abs(self.I_x[k]) ** 2 / (wx if wx is not None else 1.0)
        hy = 0.5 * np.abs(self.I_y[k]) ** 2 / (wy if wy is not None else 1.0)
        q = np.zeros((nx, ny))
        q[:-1, :] += hx
        q[1:, :] += hx
        q[:, :-1] += hy
        q[:, 1:] += hy
        a = self.A_cu[k].astype(float)
        mag = np.sqrt(np.where(a > 0, q / np.maximum(a, 1e-12), 0.0)) / d
        vec = np.sqrt(np.abs(kx) ** 2 + np.abs(ky) ** 2)
        # scale the mean's direction to that length; where the mean cancels (current fed in at the node and leaving
        # in opposite directions) the length alone, along x
        s = np.where(vec > 1e-9 * mag, mag / np.maximum(vec, 1e-300), 0.0)
        alone = (vec <= 1e-9 * mag) & (mag > 0)
        return np.where(alone, mag, kx * s), np.where(alone, 0.0, ky * s)

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

    @property
    def nnz(self) -> int:
        """The factorisation's stored entries (L and U): its fill, measured (lu_bytes plans with it)."""
        return int(self._lu.nnz)

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


# --------------------------------------------------------------------------- memory under pressure
# A Simulate at 8x on the user's 62 GB compute server failed after 82 minutes, "out of memory" with 45 GB free (the
# user, 2026-10-19: "It should not fail, it should recognise the shortage and proactive work around it to finish the
# simulation!"). Windows refuses an allocation once the commit is used up, and SuperLU commits ~6.5x the fill it
# touches, growing by doubling (ARCHITECTURE "Memory use of a Simulate"): one growth step of one large factorisation
# asks for many GB at once. So before each factorisation the gate below compares what it will commit with what is
# free, makes room, waits, or takes the least-fill pivots; what still runs out is handled by the caller's ladder
# (simulate.MemoryRelief).
FILL_PER_UNKNOWN = {"sheet": 70, "diag": 35}    # SuperLU fill per branch unknown, before a model's first
                                                # factorisation is measured (refboard: 65 board-wide, 15-31
                                                # route models at 1x, rounded up) [A38]
LU_COMMIT = 6.5                                 # SuperLU commits ~6.5x its fill on Windows (ARCHITECTURE "Memory
                                                # use") [A38] ...
LU_RESIDENT = 2.0                               # ... and keeps at most ~2x it resident elsewhere [A67]
BACKEND_FILL = 0.45                             # a faster backend's fill per unknown against SuperLU's estimate,
                                                # before its first factorisation (Accelerate on the M2 at 8x: 0.27
                                                # board-wide, ~0.41 the others; PARDISO at 4x 0.5-0.6) [A69]
BACKEND_MEMORY = 1.5                            # its memory per byte of fill: no SuperLU commit growth (PARDISO's
                                                # own peak, Accelerate's measured: <= ~1.5x) [A69]
GATE_SHARE = 0.8                                # of the available memory a factorisation may plan to commit
GATE_WAIT_S = 1.0                               # a waiting factorisation looks at the memory again this often
_STACKS: "weakref.WeakSet[PlaneStack]" = weakref.WeakSet()     # every stack alive: their caches can be freed


def lu_bytes(stack: "PlaneStack") -> int:
    """About the memory one factorisation of this model commits: its fill as measured on its largest factorisation
    so far (`lu_nnz`), else as estimated from its unknowns (FILL_PER_UNKNOWN; fill grows faster than the unknowns
    on finer grids, so the estimate is low at high resolutions until the first one is measured), x 16 bytes x
    lu_commit() [A38] [A67]; with a faster backend (solver/sparse.py) its own fill and memory: BACKEND_FILL,
    BACKEND_MEMORY [A69]."""
    backend = _sparse.active() != "superlu"
    nnz = getattr(stack, "lu_nnz", 0) or (FILL_PER_UNKNOWN.get(stack.precond_mode, 70) * stack.nb
                                          * (BACKEND_FILL if backend else 1.0))
    return int(nnz * 16 * (BACKEND_MEMORY if backend else lu_commit()))


def lu_commit() -> float:
    """What a factorisation takes of the memory the system counts, per byte of its fill: on Windows the commit
    (SuperLU's arrays grow by doubling and every page allocated is committed, touched or not: LU_COMMIT [A38]); on
    macOS and Linux only the pages touched are resident, at most twice the fill while an array grows (LU_RESIDENT
    [A67]). Before 2026-10-25 every platform planned with Windows' 6.5: the M2 server ran its 8x jobs one solve at a
    time (docs/SPEED.md 4.3)."""
    return LU_COMMIT if sys.platform == "win32" else LU_RESIDENT


def free_factorisations() -> int:
    """Drop every live stack's cached factorisations (all models of this process): the memory goes back once no
    solve still uses them. Results don't change: a freed factorisation is made again identically when needed
    (PlaneStack.operator). How many were dropped."""
    n = 0
    for st in list(_STACKS):
        with st._lock:
            n += len(st._ops)
            st._ops.clear()
        gpu.release(st)
    return n


def low_memory_everywhere(stacks=None) -> int:
    """From now on these stacks (every live one when None) factorise with diagonal pivots only (the least fill
    SuperLU can do: the ordering's own; a preconditioner needs no more, GMRES corrects it): the second rung of
    simulate.MemoryRelief. How many stacks were switched."""
    n = 0
    for st in list(_STACKS if stacks is None else stacks):
        if not getattr(st, "lu_low_memory", False):
            st.lu_low_memory = True
            n += 1
    return n


class _MemoryGate:
    """Admits the factorisations of the whole process one by one while memory is short (see the section above).

    A factorisation states what it will commit (lu_bytes). It goes ahead when that, plus what the factorisations
    already being made will commit, fits in GATE_SHARE of the available memory (core.sysmem.available_bytes: the
    free commit on Windows, within the compute server's limit for the job). Otherwise: first every cached
    factorisation of every model is freed (once per wait); then it waits for the ones being made to finish; with
    none being made and still too little, it goes ahead with the least-fill pivots (`short`). It never waits for
    memory nothing in this process will free."""

    def __init__(self):
        """Nothing being made."""
        self.cond = threading.Condition()
        self.held = 0           # bytes the factorisations being made will commit (as planned)
        self.making = 0         # how many are being made
        self.freed = 0          # cached factorisations freed to make room (the run notes it)
        self.waits = 0          # factorisations that waited for others to finish

    @contextmanager
    def admit(self, need: int):
        """Hold the gate while one factorisation of `need` bytes is made; yields True when it must take the
        least-fill pivots (the memory is short even with nothing else being made)."""
        from ..core.sysmem import available_bytes
        freed = waited = False
        short = False
        while True:
            avail = available_bytes()
            with self.cond:
                if avail is None or self.held + need <= GATE_SHARE * avail:
                    break
                if self.making and freed:
                    # others are being made: their memory comes back when they finish (or GATE_WAIT_S passes)
                    if not waited:
                        self.waits += 1
                        waited = True
                    self.cond.wait(GATE_WAIT_S)
                    continue
                if freed:
                    short = need > GATE_SHARE * avail      # alone and still too little: the least fill
                    break
            # not under the condition's lock: freeing takes each stack's own lock
            n = free_factorisations()
            with self.cond:
                self.freed += n
            freed = True
            import gc
            gc.collect()
        with self.cond:
            self.held += need
            self.making += 1
        try:
            yield short
        finally:
            with self.cond:
                self.held -= need
                self.making -= 1
                self.cond.notify_all()


GATE = _MemoryGate()


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
        _STACKS.add(self)                   # its cache can be freed when memory runs short (free_factorisations)

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
        self.n_grid = len(idx[0])
        # the parts' lids (LidNode: no grid node) are numbered after the grid's, in the order of their numbers
        lids = sorted({e.lid for b in branches for e in (b.a, b.b) if isinstance(e, LidNode)})
        self.lid_nid = {k: self.n_grid + n for n, k in enumerate(lids)}
        self.n_nodes = self.n_grid + len(lids)
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
        # branches whose ends are both on copper or a lid (and different): the others would join nothing
        self.branches = [b for b in branches if self.node_id(b.a) >= 0 and self.node_id(b.b) >= 0 and b.a != b.b]
        self.nbr = len(self.branches)
        self.nb = self.ns + self.ncap + self.nbr
        cu = self.cap[:, :4].astype(int)
        self.a_nodes = np.concatenate([self.nid[tuple(self.xa.T)], self.nid[tuple(self.ya.T)],
                                       self.nid[cu[:, 0], cu[:, 2], cu[:, 3]],
                                       np.array([self.node_id(b.a) for b in self.branches], int)]).astype(int)
        self.b_nodes = np.concatenate([self.nid[self.xa[:, 0], self.xa[:, 1] + 1, self.xa[:, 2]],
                                       self.nid[self.ya[:, 0], self.ya[:, 1], self.ya[:, 2] + 1],
                                       self.nid[cu[:, 1], cu[:, 2], cu[:, 3]],
                                       np.array([self.node_id(b.b) for b in self.branches], int)]).astype(int)
        r = np.arange(self.nb)
        self.A = sp.csr_matrix((np.concatenate([np.ones(self.nb), -np.ones(self.nb)]),
                                (np.concatenate([r, r]), np.concatenate([self.a_nodes, self.b_nodes]))),
                               shape=(self.nb, self.n_nodes))
        self._components()

    def _widths(self):
        """Strips narrower than the pitch (the grids' wx / wy, ox / oy; full where a grid has none):
        `strip_width` (ns), each strip's copper width as a fraction of the pitch at the border it crosses (for the
        inductance); `r_width` (ns), the width its resistance and heat take, the border's with a change of width
        along the strip in series (Grid.rx / ry [A39]; per layer as `W_x` / `W_y`, None when every strip is
        full: the solutions' densities in the copper); `dL` (H per strip, None when
        every strip is full), the partial self inductance a narrower strip adds to the full square strip's in the
        kernels, L(d, φd) − L(d, d) of a flat strip of the layer's copper thickness (_narrow_self); `M_near`
        (sparse ns x ns, None when every strip is full), the change in mutual inductance between side-by-side
        neighbours where one of them is narrower: the kernels have them a full pitch apart, a full pitch wide,
        while their copper is narrower and its centres nearer (a neck straddling two cell rows is two partial
        strips touching: at a pitch apart they acted as a conductor wider than the neck, its L 16 % low). The
        resistance is the sheet resistance / r_width (_impedances)."""
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
        # the width a strip's resistance and heat take: its border's, with a change of width along the strip in
        # series where the grid has one (Grid.rx / ry, _series_factor [A39]); the inductance keeps the border's
        rw = np.concatenate([gather("rx", self.xa, sx, 0.0), gather("ry", self.ya, sy, 0.0)])
        self.r_width = np.where(ok & (rw > 0), rw, self.strip_width)
        self.r_uniform = not np.any(self.r_width != 1.0)        # every strip a square's R (the preconditioner)
        off = np.where(ok, np.concatenate([gather("ox", self.xa, sx, 0.0), gather("oy", self.ya, sy, 0.0)]), 0.0)
        # each node's copper area (Grid.copper_area, a fraction of d²): what a density in the copper is defined
        # over (Solution.node_field(copper=True)), shared by the stack's solutions
        self.A_cu = np.stack([l.grid.copper_area() for l in self.layers]).astype(np.float32)
        self.dL = self.M_near = self.W_x = self.W_y = None
        if not np.any(self.strip_width != 1.0) and not np.any(self.r_width != 1.0):
            return
        self.W_x, self.W_y = np.ones((self.N, *sx)), np.ones((self.N, *sy))     # per layer (Solution.node_field)
        self.W_x[tuple(self.xa.T)] = self.r_width[: self.nxb]
        self.W_y[tuple(self.ya.T)] = self.r_width[self.nxb:]
        # the inductance corrections are for strips narrower than the pitch; one a little wider (a strip that took
        # in its neighbour's sliver, _strip_widths) keeps the full strip's kernels
        narrow = self.strip_width < 1.0
        if not np.any(narrow):
            return
        d = self.d
        t = np.array([l.thickness for l in self.layers])[np.concatenate([self.xa[:, 0], self.ya[:, 0]])]
        dL = np.zeros(self.ns)
        keys, inv = np.unique(np.round(np.column_stack([self.strip_width[narrow], t[narrow]]), 12), axis=0,
                              return_inverse=True)
        dL[narrow] = _narrow_self(d, keys[:, 0], keys[:, 1])[np.ravel(inv)]
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
        """The node number of (layer, i, j), -1 when it is not on copper; of a LidNode its own number (after the
        grid's), -1 when no branch of this stack names it."""
        if isinstance(node, LidNode):
            return self.lid_nid.get(node.lid, -1)
        return int(self.nid[node])

    def with_branches(self, extra: list[Branch]) -> "PlaneStack":
        """This stack plus a few branches (candidate vias / parts). Shares grids and kernels; solves reuse
        this stack's factorisations through a low-rank (Woodbury) update [Woodbury 1950]."""
        # (a branch to a lid this stack does not have joins nothing here: new lids need a model of their own)
        extra = [b for b in extra if self.node_id(b.a) >= 0 and self.node_id(b.b) >= 0 and b.a != b.b]
        if not extra:
            return self
        a = np.array([self.node_id(b.a) for b in extra])
        b = np.array([self.node_id(b.b) for b in extra])
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
        _STACKS.add(new)
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

    def _conv(self, X: np.ndarray, orient: str, times: Optional[_stats.SolveTimes] = None) -> np.ndarray:
        """Σ_s K[t,s] ⊛ X[s] for every layer t (double precision, see module docstring), on the whole padded FFT
        grid: the caller picks its strips out of it. `times`: a solve's time split (solver/stats.py), the FFTs and
        the multiply-add apart."""
        P = self.Px if orient == "x" else self.Py
        t0 = time.perf_counter() if times is not None else 0.0
        # convolution theorem: FFT each layer's currents (zero-padded to P), multiply by the kernel spectra and
        # sum over source layers per pixel, inverse FFT
        F = fft2(X, s=P, axes=(1, 2), workers=fft_workers())
        t1 = time.perf_counter() if times is not None else 0.0
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
        if times is None:
            return ifft2(out, axes=(1, 2), workers=fft_workers(), overwrite_x=True)
        t2 = time.perf_counter()
        res = ifft2(out, axes=(1, 2), workers=fft_workers(), overwrite_x=True)
        times.fft += (t1 - t0) + (time.perf_counter() - t2)
        times.mac += t2 - t1
        return res

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

    def Lp_matvec(self, I_s: np.ndarray, times: Optional[_stats.SolveTimes] = None) -> np.ndarray:
        """L_p · I for the strip currents (length ns). `times`: a solve's time split (solver/stats.py)."""
        t0 = time.perf_counter() if times is not None else 0.0
        out = np.empty(self.ns, complex)
        for orient, lo, hi, shape in (("x", 0, self.nxb, (self.N, self.nx - 1, self.ny)),
                                      ("y", self.nxb, self.ns, (self.N, self.nx, self.ny - 1))):
            if hi > lo:
                # scatter the strip currents into a dense (layer, i, j) array (zero where there is no strip),
                # convolve, gather the result back at the strips
                into, outof = self._strip_index(orient)
                X = np.zeros(shape[0] * shape[1] * shape[2], complex)
                X[into] = I_s[lo:hi]
                out[lo:hi] = self._conv(X.reshape(shape), orient, times).reshape(-1)[outof]
        if self.dL is not None:             # strips narrower than the pitch (_widths)
            out += self.dL * I_s
            if self.M_near is not None:
                out += self.M_near @ I_s
        if times is not None:
            times.lp += time.perf_counter() - t0
        return out

    # ------------------------------------------------------------------ operators
    def surface_resistance(self, f: float, t: float) -> float:
        """Sheet resistance (ohm per square) of copper t thick at f: 1/(sigma t) at DC, above it the thickness the
        skin depth leaves it [Ramo 1994] (kernel.skin_thickness, the exact one-sided slab [Dowell 1966] [A2]; as
        track_impedance). A square strip's R is its sheet resistance."""
        return 1 / (self.sigma * skin_thickness(t, f, self.sigma))

    def _impedances(self, f: float):
        """(w, each strip's resistance, the impedances of the other unknowns: capacitances, then branches) at f.
        The strips' inductance is not here: it is the L_p product (Lp_matvec)."""
        w = 2 * math.pi * f
        rs = np.array([self.surface_resistance(f, l.thickness) for l in self.layers])
        # a square strip's R is the sheet resistance; one φ of the pitch wide, sheet resistance / φ (φ with a change
        # of width along the strip in series: _widths)
        R_strip = (np.concatenate([rs[self.xa[:, 0]], rs[self.ya[:, 0]]]) / self.r_width if self.ns
                   else np.zeros(0))
        scale = getattr(self, "r_scale", None)            # the copper at its temperature (set_resistance_scale)
        if scale is not None and self.ns:
            R_strip = R_strip * scale
        if self.ncap:   # Y = jωC(1 - j·tanδ): susceptance plus dielectric-loss conductance [Pozar 2012]
            C, tand = self.cap[:, 4], self.cap[:, 5]
            z_cap = 1.0 / (w * C * tand + 1j * w * C) if w > 0 else np.full(self.ncap, 1e18 + 0j)
        else:
            z_cap = np.zeros(0, complex)
        z_br = np.array([complex(b.z(f)) for b in self.branches], complex)
        bscale = getattr(self, "branch_scale", None)
        if bscale is not None and len(z_br):             # a branch's resistance only (its inductance stays)
            z_br = z_br.real * bscale + 1j * z_br.imag
        return w, R_strip, np.concatenate([z_cap, z_br])

    def set_resistance_scale(self, strips: Optional[np.ndarray] = None, branches: Optional[np.ndarray] = None):
        """Scale the strips' resistances (`strips`, one factor per strip in the unknowns' order: x strips then y
        strips) and the branches' (`branches`, one per branch: their real part), e.g. by 1 + α(T − 20 °C) for copper
        at its temperature [IEC 60028] (analysis/thermal.electrothermal); None: as built. Every factorisation made
        before is dropped (they were of the old resistances), and the preconditioner no longer takes every strip's
        resistance as alike. Not while other threads solve on this stack."""
        with self._lock:
            self.r_scale = None if strips is None else np.asarray(strips, float)
            self.branch_scale = None if branches is None else np.asarray(branches, float)
            if self.r_scale is not None and len(self.r_scale) != self.ns:
                raise ValueError(f"{len(self.r_scale)} strip factors for {self.ns} strips")
            if self.branch_scale is not None and len(self.branch_scale) != len(self.branches):
                raise ValueError(f"{len(self.branch_scale)} branch factors for {len(self.branches)} branches")
            self.r_uniform = not np.any(self.r_width != 1.0) and self.r_scale is None
            self._ops.clear()
            self._made.clear()
            self._plan.clear()
            self._building.clear()

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
            t_wait = time.perf_counter()
            build.done.wait()           # another thread is making this factorisation: use its result
            if _stats._active[0] is not None:
                _stats.thread_add("wait", time.perf_counter() - t_wait)
            if build.op is not None:
                with self._lock:
                    return self._lend(f, key, build.op, m)
        remade = m.f_lu is not None     # made before and freed since (OPERATOR_CACHE, the memory gate): made again
        try:
            op = self._factorise(m.f, m.exact)
            c = _stats._active[0]
            if c is not None and remade:
                c.add(self, remade=1)
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
        c = _stats._active[0]
        t0 = time.perf_counter()
        w, R_strip, z_tail = self._impedances(f)
        Dinv = self._preconditioner_admittance(w, R_strip, z_tail)
        base = self._base
        f_lu = f
        ref = None
        if base is not None and base.nb <= self.nb:
            t_base = time.perf_counter()
            bop = base.operator(f, exact)
            t1 = time.perf_counter()        # the base's own factorisation (when made just now) counts as the base's
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
            if c is not None:
                dt = (t_base - t0) + (time.perf_counter() - t1)
                c.add(self, updates=1, update_s=dt)
                _stats.thread_add("fact", dt)
        else:
            S = (self.ArT @ Dinv @ self.Ar).tocsc()
            # S is complex symmetric: symmetric mode keeps the A+Aᵀ ordering only when the diagonal pivots are
            # taken (SuperLU's advice: a small pivot threshold). With the default threshold 1.0 it pivoted off
            # the diagonal: refboard's 6-layer board-wide model (34k unknowns) took 11.4 s and 10.4 M
            # fill, 3.4 s and 5.8 M with 0.1 (same residual; solves 31 -> 21 ms). Small pivots still pivot.
            if getattr(self, "lu_too_large", None):         # this model failed even with diagonal pivots
                raise SolverError(self.lu_too_large)
            # once a factorisation of this model overflowed SuperLU, the others take diagonal pivots at once
            # (_factorise_diagonal): the usual attempt would only grow to the same limit again (minutes, ~22 GB);
            # so do all of them once memory ran short (low_memory_everywhere: the least fill)
            low = bool(getattr(self, "lu_fallbacks", 0) or getattr(self, "lu_low_memory", False))
            thresh = 0.0 if low else LU_PIVOT_THRESHOLD
            # admitted by the memory gate: room made, or a wait for the others being made, or the least fill
            t_gate = time.perf_counter()
            with GATE.admit(lu_bytes(self)) as short:
                if c is not None:
                    c.add(self, gate_wait_s=time.perf_counter() - t_gate)
                if short and thresh:
                    thresh = 0.0
                    self.lu_memory_fallbacks = getattr(self, "lu_memory_fallbacks", 0) + 1
                # a faster backend when one is configured and loads (solver/sparse.py: PARDISO, Accelerate),
                # else None and SuperLU as before; the least-fill retries below stay SuperLU's
                lu = _sparse.factorise(S) if not low else None
                try:                    # made (and freed) in a host thread: see _LuHost
                    if lu is None:
                        lu = hosted_splu(S, permc_spec="MMD_AT_PLUS_A", diag_pivot_thresh=thresh,
                                         options={"SymmetricMode": True})
                except RuntimeError as e:
                    # SuperLU reports a failed allocation as a RuntimeError: as a MemoryError the scheduler retries
                    # the solve with more room (simulate.MemoryRelief)
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
            # its measured fill plans the next ones (lu_bytes): the estimate from the unknowns runs low on fine grids
            self.lu_nnz = max(getattr(self, "lu_nnz", 0), lu.nnz)
            solve_S = lu.solve
            if c is not None:               # the Schur complement's assembly, the gate's wait and SuperLU's work
                dt = time.perf_counter() - t0
                c.factorised(self, dt, lu.nnz)
                _stats.thread_add("fact", dt)
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
            if "SUPERLU_MALLOC" in str(e):          # short of memory now, not too large for good: the ladder's
                raise MemoryError(f"the sparse factorisation ran out of memory ({S.shape[0]} unknowns)") from e
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
            if self.dL is None and getattr(self, "r_uniform", True):   # every strip full width: every position
                R = np.array([R_strip[strips[0, k]] for k in range(m)])          # alike, one inverse
                Zi = np.linalg.inv(np.diag(R).astype(complex) + 1j * w * B)
                Zi = np.broadcast_to(Zi.ravel(), (len(strips), m * m)).ravel()
            else:               # strips not a pitch wide: each position its own R and self inductance
                Z = np.broadcast_to(1j * w * B, (len(strips), m, m)).copy()
                Z[:, np.arange(m), np.arange(m)] += R_strip[strips] + (
                    0.0 if self.dL is None else 1j * w * self.dL[strips])
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
        finish (out of memory, a missing operation) is done again here on the CPU and counted in the run.

        While a statistics collector is active (solver/stats.py), the solve, its iterations and its time are
        counted for this model; what it computes does not change."""
        c = _stats._active[0]
        if c is None:
            out = self._solve_here(f, rhs_top, J, tol, max_cycles, guess, None)
        else:
            mark = _stats.thread_mark()
            t0 = time.perf_counter()
            times = _stats.SolveTimes()
            try:
                out = self._solve_here(f, rhs_top, J, tol, max_cycles, guess, times)
            except SolverError:
                c.solved(self, None, time.perf_counter() - t0, mark, times, failed=True)
                raise
            c.solved(self, out[2], time.perf_counter() - t0, mark, times)
        # the most iterations a solve of this model took: the basis its next solves plan for (simulate.basis_rows)
        self.its_max = max(getattr(self, "its_max", 0), out[2])
        return out

    def _solve_here(self, f, rhs_top, J, tol, max_cycles, guess, times) -> tuple:
        """solve()'s work: the operator at f, then the iterations on the active device, or on the CPU (also when
        the device fails). `times`: the solve's time split while statistics are collected, else None."""
        op = self.operator(f)
        run = gpu.active()
        if run is not None:
            try:
                return self._solve(f, op, rhs_top, J, tol, max_cycles, guess, gpu.TorchMath(run, self), times)
            except SolverError:
                raise
            except Exception as e:      # noqa: BLE001 - any device failure: the CPU does this solve
                run.failed(e)
                if times is not None:   # the device's attempt is not this solve's: count the CPU's afresh
                    times.borrowed = times.refactor = 0
        return self._solve(f, op, rhs_top, J, tol, max_cycles, guess, _HostMath(self, times), times)

    def _solve(self, f, op, rhs_top, J, tol, max_cycles, guess, m, times=None) -> tuple:
        """solve() with the matrix and vector work of `m` (_HostMath: numpy; gpu.TorchMath: a device); `times`
        notes a borrowed preconditioner and a borrow past its budget (solver/stats.py)."""
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
        if times is not None and borrowed:
            times.borrowed += 1
        y, its, ok = _gmres(lambda v: m.matvec(op, m.precond(op, v)), r0, tol, max_cycles=max_cycles, bnorm=bnorm,
                            max_its=op.borrow_budget() if borrowed else None, vec=m.vec,
                            extra_cycles=0 if borrowed else GMRES_EXTRA_CYCLES)
        if not ok and borrowed:
            if times is not None:
                times.refactor += 1
            # The borrowed preconditioner is not good enough here (near a resonance it can take 10x the
            # iterations): factorise at f and go on from the solution so far. Without the budget, refboard's
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
            n = stack.node_id(node)
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
        Vg[np.nonzero(st.node)] = V[:st.n_grid]                 # the grid's nodes; the parts' lids after them
        # loop impedance: drop along the filaments + Σ J·V over the injections (current enters at +, leaves at -)
        v_inj = sum(amps * V[n] for n, amps in self.injections)
        Z = complex(_trace_resistance(self.trace, f, st.sigma, self.trace_t) + v_inj
                    + 1j * w * (self.L_trace + np.dot(self.psi, Ib)))
        # copies: views of Ib would keep the whole unknown vector x alive (Ib is a view of it). Not x itself either:
        # Solution.state_for rebuilds it from these when the fix search wants a warm start
        return Solution(f, I_x, I_y, Vg, Ib[st.ns + st.ncap:].copy(), Z, st.moment(self.trace, Ib), its,
                        Ib[st.ns: st.ns + st.ncap].copy(), W_x=st.W_x, W_y=st.W_y, A_cu=getattr(st, "A_cu", None),
                        V_lid=V[st.n_grid:].copy())


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
    def put(V, j, v):
        """Store v as basis vector j."""
        V[j] = v

    @staticmethod
    def row(V, j):
        """Basis vector j (a complex vector the operator takes)."""
        return V[j]

    def for_tol(self, tol: float):
        """The vector work for a solve to `tol`: this one (a complex128 basis serves every tolerance)."""
        return self

    def grow(self, V, rows, used):
        """A basis of `rows` rows holding the first `used` of V."""
        out = self.basis(rows, V.shape[1])
        out[:used] = V[:used]
        return out

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


class _Basis32:
    """A Krylov basis in single precision: its real and imaginary parts as two (rows, n) float32 planes."""
    __slots__ = ("re", "im")

    def __init__(self, rows: int, n: int):
        """Uninitialised rows."""
        self.re = np.empty((rows, n), np.float32)
        self.im = np.empty((rows, n), np.float32)

    def __len__(self) -> int:
        """Its rows."""
        return self.re.shape[0]


class _Basis32Vectors(_HostVectors):
    """_HostVectors with the Krylov basis stored in single precision (_Basis32) and every product with it in
    double precision (solver/_mac.gs_project, gs_combine): half the basis memory (a 2 M-unknown model's 101 rows
    3.2 -> 1.6 GB per solve) and half the bytes each Gram-Schmidt pass reads (~32 % of an iteration's time on the
    M2, docs/SPEED.md). Mixed-precision GMRES [Lindquist 2020] [Aliaga 2023]: each basis vector is rounded to
    ~6e-8 relative, so the Arnoldi relation holds to that and the recurrence's residual can drift from the true
    one below it; _gmres stops on the true residual (double precision) and restarts from it, so the answer meets
    the tolerance as before (1e-5 by default, two orders above the rounding) [A66]."""

    def __init__(self, kernels: tuple):
        """With the compiled (gs_project, gs_combine)."""
        self._project, self._combine = kernels

    @staticmethod
    def basis(rows, n):
        """An uninitialised single-precision basis of `rows` rows."""
        return _Basis32(rows, n)

    def for_tol(self, tol: float):
        """This one down to BASIS32_MIN_TOL; complex128 (_HostVectors) for a tighter tolerance, which a
        single-precision basis reaches only slowly (its rounding, ~6e-8, is then within two decades of it)."""
        return self if tol >= BASIS32_MIN_TOL else _HOST_VECTORS

    @staticmethod
    def put(V, j, v):
        """Store v as basis vector j, rounded to single precision."""
        V.re[j] = v.real
        V.im[j] = v.imag

    @staticmethod
    def row(V, j):
        """Basis vector j as a complex128 vector."""
        out = np.empty(V.re.shape[1], complex)
        out.real = V.re[j]
        out.imag = V.im[j]
        return out

    def grow(self, V, rows, used):
        """A basis of `rows` rows holding the first `used` of V."""
        out = _Basis32(rows, V.re.shape[1])
        out.re[:used] = V.re[:used]
        out.im[:used] = V.im[:used]
        return out

    def project(self, V, j, w):
        """(Vᴴ·w, w − V·Vᴴ·w) over the first j+1 basis rows, in double precision on the stored rows."""
        wr, wi = np.ascontiguousarray(w.real), np.ascontiguousarray(w.imag)
        hr, hi = np.empty(j + 1), np.empty(j + 1)
        self._project(V.re, V.im, j + 1, wr, wi, hr, hi)
        out = np.empty(len(w), complex)
        out.real, out.imag = wr, wi
        return hr + 1j * hi, out

    def combine(self, y, V, k):
        """sum_i y[i] V[i] over the first k basis vectors, in double precision."""
        n = V.re.shape[1]
        xr, xi = np.empty(n), np.empty(n)
        y = np.asarray(y, complex)
        self._combine(V.re, V.im, k, np.ascontiguousarray(y.real), np.ascontiguousarray(y.imag), xr, xi)
        out = np.empty(n, complex)
        out.real, out.imag = xr, xi
        return out


def host_vectors() -> _HostVectors:
    """The CPU's GMRES vector work: the single-precision basis when its compiled kernels load (_gs_kernels),
    else complex128 with BLAS."""
    k = _gs_kernels()
    return _Basis32Vectors(k) if k is not None else _HOST_VECTORS


class _TimedVectors:
    """A vector class (`base`) with the basis work (Gram-Schmidt's projections, the cycle's combination) timed into
    a solve's time split (solver/stats.py); the same arithmetic."""

    def __init__(self, times: _stats.SolveTimes, base=None):
        """Time `base`'s basis work (host_vectors() when None) into `times`."""
        self.times = times
        self.base = base if base is not None else host_vectors()

    def __getattr__(self, name):
        """Everything else is the base's."""
        return getattr(self.base, name)

    def for_tol(self, tol: float):
        """The base's choice for `tol` (_Basis32Vectors.for_tol), timed the same way."""
        base = self.base.for_tol(tol)
        return self if base is self.base else _TimedVectors(self.times, base)

    def project(self, V, j, w):
        """The base's project, timed."""
        t0 = time.perf_counter()
        out = self.base.project(V, j, w)
        self.times.gs += time.perf_counter() - t0
        return out

    def combine(self, y, V, k):
        """The base's combine, timed."""
        t0 = time.perf_counter()
        out = self.base.combine(y, V, k)
        self.times.gs += time.perf_counter() - t0
        return out


class _HostMath:
    """PlaneStack.solve's matrix work in numpy and SciPy (gpu.TorchMath is the device twin)."""

    def __init__(self, stack: "PlaneStack", times: Optional[_stats.SolveTimes] = None):
        """The math for solves on `stack`; `times`: the solve's time split while statistics are collected
        (solver/stats.py), the basis work then timed too."""
        self.st = stack
        self.times = times
        self.vec = _TimedVectors(times) if times is not None else host_vectors()

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
        st, t = self.st, self.times
        t0 = time.perf_counter() if t is not None else 0.0
        nb, ns = st.nb, st.ns
        Ib = x[:nb]
        top = np.empty(nb, complex)
        top[:ns] = op.R_strip * Ib[:ns] + 1j * op.w * st.Lp_matvec(Ib[:ns], t)
        top[ns:] = op.z_tail * Ib[ns:]
        top -= st.Ar @ x[nb:]
        out = np.concatenate([top, st.ArT @ Ib])
        if t is not None:
            t.matvec += time.perf_counter() - t0
            t.matvecs += 1
        return out

    def precond(self, op: _Operator, r: np.ndarray) -> np.ndarray:
        """The preconditioner M^-1 r: the saddle-point system with Z replaced by its approximation D, solved
        exactly by block elimination: y = S^-1 (r2 - A^T D^-1 r1) with S = A^T D^-1 A (the factorised Schur
        complement), then the branch part D^-1 (r1 + A y)."""
        st, t = self.st, self.times
        r1, r2 = r[:st.nb], r[st.nb:]
        if t is None:
            y = op.solve_S(r2 - st.ArT @ (op.Dinv @ r1))
            return np.concatenate([op.Dinv @ (r1 + st.Ar @ y), y])
        # the same, timed: the whole application and its SuperLU triangular solves apart (solver/stats.py)
        t0 = time.perf_counter()
        rhs = r2 - st.ArT @ (op.Dinv @ r1)
        t1 = time.perf_counter()
        y = op.solve_S(rhs)
        t2 = time.perf_counter()
        out = np.concatenate([op.Dinv @ (r1 + st.Ar @ y), y])
        t.lu += t2 - t1
        t.precond += time.perf_counter() - t0
        t.preconds += 1
        return out


# --------------------------------------------------------------------------- helpers
def gmres_restart(n: int) -> int:
    """GMRES's restart length for n unknowns: as long as GMRES_MEMORY allows, at most 200 and at least
    GMRES_RESTART_MIN (a short restart stalls near a plane resonance; [Saad 2003] on restarted GMRES's stagnation)."""
    return int(min(200, max(GMRES_RESTART_MIN, GMRES_MEMORY // (16 * n))))


def _gmres(op: Callable, b, tol: float, max_cycles: int = 8,
           restart: Optional[int] = None, bnorm: Optional[float] = None,
           max_its: Optional[int] = None, vec=None, extra_cycles: int = 0) -> tuple:
    """Restarted GMRES [Saad 1986] [Saad 2003] with classical Gram-Schmidt + reorthogonalisation [Giraud 2005] (BLAS
    gemv on the basis, no copies).
    Stops when the true residual ‖b - op(x)‖ ≤ tol·bnorm (default ‖b‖; a warm start passes the norm of the
    original right-hand side with b = its remaining residual), or unconverged after max_cycles restarts or
    max_its iterations (x is then the best so far). extra_cycles more restarts are allowed as long as each one
    still lowers the residual by GMRES_PROGRESS: a short restart (a big model's memory limit) near a plane
    resonance converges steadily but slowly (refboard's board-wide model at 4x resolution, 1.58 GHz:
    ~2x per cycle, done after 11 cycles of 52).

    The small least-squares problem is kept triangular with Givens rotations [Saad 2003], so each iteration costs
    O(j) for it: np.linalg.lstsq on the whole Hessenberg matrix every iteration took 18 ms at j = 120 and 44 ms at 200,
    more than the rest of an iteration on long solves.

    `vec`: the vector work (host_vectors() when None: on the CPU the basis is kept in single precision when numba
    is installed, _Basis32Vectors; gpu.TorchVectors on a device)."""
    if vec is None:
        vec = host_vectors()
    if hasattr(vec, "for_tol"):         # the CPU's: single precision for the usual tolerances (gpu: complex128)
        vec = vec.for_tol(tol)
    n = len(b)
    if bnorm is None:
        bnorm = vec.norm(b)
    x = vec.zeros(n)
    if bnorm == 0 or vec.norm(b) <= tol * bnorm:
        return x, 0, True
    if restart is None:
        restart = gmres_restart(n)
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
        vec.put(V, 0, r / beta)
        k = 0
        aim = tol * bnorm               # the recurrence's residual at which the true one is checked (lowered on a miss)
        for j in range(restart):
            w = op(vec.row(V, j))
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
            if hn <= 1e-14 * np.linalg.norm(h) or (max_its is not None and its >= max_its):
                break
            if abs(g[j + 1]) <= aim:
                # The recurrence says the tolerance is met: check the true residual at once (one product) and stop
                # when it agrees, else go on in this cycle towards half the residual. With right preconditioning
                # the two agree in exact arithmetic [Saad 2003, §9.3]; before 2026-10-25 the cycle went on to
                # 0.5·tol first, ~3-8 % more iterations (docs/SPEED.md).
                y = solve_triangular(H[:k, :k], g[:k])
                xt = x + vec.combine(y, V, k)
                rt = b - op(xt)
                if vec.norm(rt) <= tol * bnorm:
                    return xt, its, True
                aim = 0.5 * abs(g[j + 1])
            if j + 1 >= len(V):                # full: twice the rows (at most restart + 1), the written ones copied
                V = vec.grow(V, min(restart + 1, 2 * len(V)), j + 1)
            vec.put(V, j + 1, w / hn)
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
        teff = skin_thickness(trace_t, f, sigma)
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


def _narrow_self(d: float, phi, t):
    """The partial self inductance (H) a strip d long and φd wide adds to the kernels' full square strip: the
    numeric flat-strip integral the kernels' self term is (kernel.strip_self_numeric, zero thickness), and the
    copper thickness t's change to it from the GMD formula (kernel.strip_self), as a difference to the full
    strip's (the GMD formula alone was 1.5 % off the integral: two touching half-pitch strips read 1.5 % above
    the one strip they make). The GMD formula is [Grover 1946]; the split of a strip's partials [A4]. phi and t may
    be arrays (one per narrow strip: all in one numpy pass, see kernel.strip_self_numeric)."""
    phi, t = np.asarray(phi, float), np.asarray(t, float)

    def thick(w):
        """What thickness t lowers a strip w wide by (GMD formula, strip_self = the filament mutual at the GMD)."""
        return filament_mutual(d, 0.0, GMD_FLAT * w) - filament_mutual(d, 0.0, GMD_FLAT * (w + t))
    out = strip_self_numeric(d, phi * d) - strip_self_numeric(d, d) - thick(phi * d) + thick(d)
    return float(out) if np.ndim(out) == 0 else out


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
