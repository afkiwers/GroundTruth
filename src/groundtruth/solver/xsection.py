"""Two-dimensional electrostatic solver for transmission-line cross-sections (quasi-TEM).

A cut across one or more parallel traces, perpendicular to them: conductors are rectangles at fixed potentials (one
reference conductor, id 0, at 0 V: planes, pours, other nets; signal conductors 1..n), dielectrics are horizontal
slabs (εr per layer: the board's cores and prepregs, the solder mask, air elsewhere). Laplace's equation
∇·(εr ∇φ) = 0 is solved by finite volumes on a rectilinear grid graded towards every edge (potentials at the grid
nodes, εr per cell; each cell adds the conductances εr·(dy/2)/dx and εr·(dx/2)/dy to its four edges, the five-point
scheme of the bilinear finite-volume method). The outer box has zero normal flux (Neumann); it is placed far enough
out that the box does not matter (tests/test_xsection.py checks against exact results).

The charge on a conductor is the discrete Gauss law: the total flux leaving the dual cells of its nodes,
Q_m = ε0·Σ_{n∈m} (Aφ)_n. Driving each signal conductor at 1 V in turn gives the Maxwell capacitance matrix C (F/m)
with the dielectrics and C0 in air. For a TEM line the inductance does not depend on the dielectric:
L = μ0·ε0·C0⁻¹ (H/m). From C and L:

- one line (the others held at 0 V): Z0 = 1/(c·sqrt(C_ii·C0_ii)), εeff = C_ii/C0_ii;
- a pair driven differentially (+I, -I): L_d = L11 + L22 - 2·L12, C_d = 1/(P11 + P22 - 2·P12) with P = C⁻¹
  (equal and opposite charges), Z_diff = sqrt(L_d/C_d); common mode: L_c = (L11 + L22 + 2·L12)/4,
  C_c = C11 + C22 + 2·C12, Z_comm = sqrt(L_c/C_c);
- crosstalk between an aggressor a and a victim v (weakly coupled lines): backward (near-end) coefficient
  K_b = (L_m/L + C_m/C)/4, forward (far-end) K_f = (L_m/L - C_m/C)/2 with L_m/L = L_av/sqrt(L_aa·L_vv) and
  C_m/C = -C_av/sqrt(C_aa·C_vv) (Maxwell C_av is negative). NEXT = K_b·min(1, 2·T_d/t_r) of the aggressor's swing,
  FEXT = K_f·T_d/t_r, T_d the coupled length's delay (Johnson & Graham; Hall, Hall & McCall).

Units: SI (metres) inside; the callers convert from the board's millimetres.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable, Optional

import numpy as np

EPS0 = 8.8541878128e-12
C_LIGHT = 299792458.0
MU0 = 4e-7 * math.pi


@dataclass(frozen=True)
class Rect:
    """A conductor's cross-section, x0 <= x1, y0 <= y1 (m); y0 == y1 is a zero-thickness strip. conductor: 0 the
    reference (0 V), 1..n the signal conductors."""
    x0: float
    x1: float
    y0: float
    y1: float
    conductor: int = 0


@dataclass(frozen=True)
class Slab:
    """A dielectric layer across the whole cut between y0 and y1 (m); a later slab wins where two overlap."""
    y0: float
    y1: float
    er: float


@dataclass
class Section:
    """A cross-section: conductors, dielectric slabs (air, εr 1, elsewhere) and the box (m)."""
    rects: list[Rect]
    slabs: list[Slab] = field(default_factory=list)
    box: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)     # x0, x1, y0, y1; 0s: around the rects

    def conductors(self) -> int:
        """The number of signal conductors (ids 1..n)."""
        return max((r.conductor for r in self.rects), default=0)


@dataclass
class SectionResult:
    """The per-unit-length matrices of a section's signal conductors (index 0 = conductor 1): C and C0 (F/m, Maxwell
    form), L (H/m), and the grid's size."""
    C: np.ndarray
    C0: np.ndarray
    L: np.ndarray
    nodes: int

    def z0(self, i: int = 0) -> float:
        """Conductor i's characteristic impedance with the others at 0 V (Ω)."""
        return 1.0 / (C_LIGHT * math.sqrt(self.C[i, i] * self.C0[i, i]))

    def eeff(self, i: int = 0) -> float:
        """Conductor i's effective permittivity C/C0."""
        return self.C[i, i] / self.C0[i, i]

    def delay(self, i: int = 0) -> float:
        """Conductor i's propagation delay per metre (s/m)."""
        return math.sqrt(self.eeff(i)) / C_LIGHT

    def _diff(self, i: int, j: int) -> tuple[float, float]:
        """(L, C) of the pair i, j driven differentially (see the module)."""
        P = np.linalg.inv(self.C)
        ld = self.L[i, i] + self.L[j, j] - 2 * self.L[i, j]
        cd = 1.0 / (P[i, i] + P[j, j] - 2 * P[i, j])
        return ld, cd

    def zdiff(self, i: int = 0, j: int = 1) -> float:
        """The differential impedance of conductors i and j (Ω): twice the odd-mode impedance of a symmetric pair."""
        ld, cd = self._diff(i, j)
        return math.sqrt(ld / cd)

    def diff_delay(self, i: int = 0, j: int = 1) -> float:
        """The differential mode's delay per metre (s/m)."""
        ld, cd = self._diff(i, j)
        return math.sqrt(ld * cd)

    def zcomm(self, i: int = 0, j: int = 1) -> float:
        """The common-mode impedance of conductors i and j (Ω): half the even-mode impedance of a symmetric pair."""
        lc = (self.L[i, i] + self.L[j, j] + 2 * self.L[i, j]) / 4
        cc = self.C[i, i] + self.C[j, j] + 2 * self.C[i, j]
        return math.sqrt(lc / cc)

    def coupling(self, victim: int, aggressor: int) -> tuple[float, float]:
        """(L_m/L, C_m/C) between two conductors: the inductive and capacitive coupling ratios."""
        L, C = self.L, self.C
        lm = L[victim, aggressor] / math.sqrt(L[victim, victim] * L[aggressor, aggressor])
        cm = -C[victim, aggressor] / math.sqrt(C[victim, victim] * C[aggressor, aggressor])
        return lm, cm

    def kb(self, victim: int, aggressor: int) -> float:
        """The backward (near-end) crosstalk coefficient: NEXT's saturated share of the aggressor's swing."""
        lm, cm = self.coupling(victim, aggressor)
        return (lm + cm) / 4

    def kf(self, victim: int, aggressor: int) -> float:
        """The forward (far-end) crosstalk coefficient: FEXT = |kf|·T_d/t_r of the swing (0 in a homogeneous
        dielectric, where L_m/L = C_m/C)."""
        lm, cm = self.coupling(victim, aggressor)
        return (lm - cm) / 2


def _graded(a: float, b: float, ha: float, hb: float, hmax: float, growth: float) -> list[float]:
    """Points from a to b (a excluded, b included): steps of ha at a and hb at b, each growing by `growth` towards
    the middle up to hmax (fine where the field changes fastest, coarse in between)."""
    span = b - a
    if span <= 0:
        return []
    hl, hr = min(ha, hmax), min(hb, hmax)
    left, right, rest = [], [], span
    while rest > hl + hr and not (hl >= hmax and hr >= hmax):
        if hl <= hr:                                    # grow the finer side first: both ends stay fine
            left.append(hl)
            rest -= hl
            hl = min(hl * growth, hmax)
        else:
            right.append(hr)
            rest -= hr
            hr = min(hr * growth, hmax)
    middle = []
    if rest > 1e-3 * min(ha, hb):                       # the rest in even steps no larger than the next graded one
        n = max(1, int(math.ceil(rest / min(hl, hr))))
        middle = [rest / n] * n
    elif left:
        left[-1] += rest                                # a sliver left over: added to the innermost step
    elif right:
        right[-1] += rest
    seq = left + middle + right[::-1]
    pts, x = [], a
    for st in seq:
        x += st
        pts.append(x)
    pts[-1] = b
    return pts


def _axis(breaks: list[float], local: Callable[[float], float], hmax: float, growth: float,
          merge: float) -> np.ndarray:
    """The grid along one axis through the breakpoints (those closer than `merge` to the previous one dropped),
    graded between them from each breakpoint's own finest step local(b)."""
    bs = []
    for v in sorted(breaks):
        if not bs or v - bs[-1] > merge:
            bs.append(v)
    pts = [bs[0]]
    for a, b in zip(bs, bs[1:]):
        pts += _graded(a, b, local(a), local(b), hmax, growth)
    return np.unique(np.array(pts))


H_FLOOR = 0.5e-6                # m: the finest cell at most this small (a sliver of copper sets no finer grid)
H_SPREAD = 0.2                  # the finest step grows by this times the distance from the signal conductors


def signal_scale(sec: Section) -> float:
    """The smallest dimension of the signal conductors' surroundings (m): their widths and thicknesses, and their
    gaps to any other copper beside, above or below them (a pair's gap, a coplanar pour, the reference plane)."""
    sig = [r for r in sec.rects if r.conductor > 0]
    feats = [d for r in sig for d in (r.x1 - r.x0, r.y1 - r.y0) if d > 0]
    for s in sig:
        for r in sec.rects:
            if r is s or r.conductor == s.conductor:
                continue
            if min(s.y1, r.y1) >= max(s.y0, r.y0):              # side by side: the horizontal gap
                g = max(r.x0 - s.x1, s.x0 - r.x1)
            elif min(s.x1, r.x1) >= max(s.x0, r.x0):            # above or below: the vertical gap
                g = max(r.y0 - s.y1, s.y0 - r.y1)
            else:
                continue
            if g > 0:
                feats.append(g)
    return min(feats) if feats else 0.0


def build_grid(sec: Section, hmin: Optional[float] = None, growth: float = 1.15, cells_across: int = 40,
               edge_div: int = 20) -> tuple[np.ndarray, np.ndarray, tuple]:
    """(x nodes, y nodes, box) for a section: breakpoints at every rectangle and slab edge. The finest cell hmin (by
    default signal_scale / edge_div, at least H_FLOOR) at the signal conductors, growing with the distance d from
    them (hmin + H_SPREAD·d at a breakpoint d away: far copper edges need no fine grid), the coarsest a
    cells_across-th of the box; breakpoints closer than hmin/4 merged."""
    x0, x1, y0, y1 = sec.box
    if not (x1 > x0 and y1 > y0):
        xs = [v for r in sec.rects for v in (r.x0, r.x1)]
        ys = [v for r in sec.rects for v in (r.y0, r.y1)] + [v for s in sec.slabs for v in (s.y0, s.y1)]
        x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    xb = [x0, x1] + [min(max(v, x0), x1) for r in sec.rects for v in (r.x0, r.x1)]
    yb = ([y0, y1] + [min(max(v, y0), y1) for r in sec.rects for v in (r.y0, r.y1)]
          + [min(max(v, y0), y1) for s in sec.slabs for v in (s.y0, s.y1)])
    hmax = max(x1 - x0, y1 - y0) / cells_across
    if hmin is None:
        scale = signal_scale(sec)
        hmin = max(scale / edge_div, H_FLOOR) if scale > 0 else max(x1 - x0, y1 - y0) / 2000
    sig = [r for r in sec.rects if r.conductor > 0] or sec.rects
    sx0, sx1 = min(r.x0 for r in sig), max(r.x1 for r in sig)
    sy0, sy1 = min(r.y0 for r in sig), max(r.y1 for r in sig)

    def local(lo: float, hi: float) -> Callable[[float], float]:
        """The finest step at a breakpoint: hmin at the signal's span, coarser with the distance from it."""
        return lambda v: min(hmax, hmin + H_SPREAD * max(lo - v, v - hi, 0.0))
    return (_axis(xb, local(sx0, sx1), hmax, growth, hmin / 4), _axis(yb, local(sy0, sy1), hmax, growth, hmin / 4),
            (x0, x1, y0, y1))


EDGE_DIV, GROWTH, CELLS_ACROSS = 10, 1.15, 40     # the default grid (tests/test_xsection.py: within 0.5 % of exact)


def solve(sec: Section, hmin: Optional[float] = None, growth: float = GROWTH, cells_across: int = CELLS_ACROSS,
          edge_div: int = EDGE_DIV, extrapolate: bool = True) -> SectionResult:
    """C, C0 and L of a section's signal conductors (see the module). extrapolate: solved on two grids, the finest
    cells h and h/2, and extrapolated to h -> 0 (Richardson, C = 2·C(h/2) - C(h)): the error of the charge at a
    strip's edges falls in proportion to h (measured: -2.1 % and -1.1 % from Cohn's exact stripline at h and h/2,
    -0.15 % extrapolated)."""
    if not extrapolate:
        C, C0, nodes = _capacitances(sec, hmin, growth, cells_across, edge_div)
    else:
        if hmin is not None:
            c1, c01, n1 = _capacitances(sec, hmin, growth, cells_across, edge_div)
            c2, c02, n2 = _capacitances(sec, hmin / 2, growth, cells_across, edge_div)
        else:
            c1, c01, n1 = _capacitances(sec, None, growth, cells_across, edge_div)
            c2, c02, n2 = _capacitances(sec, None, growth, cells_across, edge_div * 2)
        C, C0, nodes = 2 * c2 - c1, 2 * c02 - c01, n1 + n2
    L = np.linalg.inv(C0) / C_LIGHT ** 2
    return SectionResult(C, C0, L, nodes)


def _capacitances(sec: Section, hmin: Optional[float], growth: float, cells_across: int,
                  edge_div: int) -> tuple[np.ndarray, np.ndarray, int]:
    """(C, C0, grid nodes) of a section on one grid (see solve)."""
    import scipy.sparse as sp
    from scipy.sparse.linalg import splu
    xs, ys, _ = build_grid(sec, hmin, growth, cells_across, edge_div)
    nx, ny = len(xs), len(ys)
    n_sig = sec.conductors()
    if n_sig == 0:
        raise ValueError("no signal conductor in the section")
    # which conductor each node belongs to (-1: free); a later rectangle wins where two touch
    owner = np.full((ny, nx), -1, dtype=int)
    # a rectangle's edge merged into a breakpoint up to hmin/4 away still owns its nodes
    tol = 1e-12 + 0.3 * min(np.diff(xs).min(), np.diff(ys).min())
    for r in sec.rects:
        ix = (xs >= r.x0 - tol) & (xs <= r.x1 + tol)
        iy = (ys >= r.y0 - tol) & (ys <= r.y1 + tol)
        if ix.any() and iy.any():
            owner[np.ix_(iy, ix)] = r.conductor
    if not (owner == 0).any():
        raise ValueError("no reference conductor (id 0) in the section")
    for k in range(1, n_sig + 1):
        if not (owner == k).any():
            raise ValueError(f"conductor {k} has no grid node (too thin for the grid?)")
    dx, dy = np.diff(xs), np.diff(ys)
    yc = (ys[:-1] + ys[1:]) / 2
    er_rows = np.ones(ny - 1)
    for s in sec.slabs:
        er_rows[(yc >= s.y0 - tol) & (yc <= s.y1 + tol)] = s.er
    idx = np.arange(nx * ny).reshape(ny, nx)

    def laplacian(er_row: np.ndarray):
        """The finite-volume matrix for permittivities per cell row."""
        e = er_row[:, None] * np.ones((1, nx - 1))           # (ny-1, nx-1) cells
        DX, DY = np.meshgrid(dx, dy)
        gx = e * DY / 2 / DX                                 # each cell's horizontal edge conductance (top and bottom)
        gy = e * DX / 2 / DY                                 # vertical edge conductance (left and right)
        rows, cols, vals = [], [], []

        def edge(a, b, g):
            """Add conductance g between node arrays a and b."""
            a, b, g = a.ravel(), b.ravel(), g.ravel()
            rows.extend([a, b, a, b])
            cols.extend([a, b, b, a])
            vals.extend([g, g, -g, -g])
        edge(idx[:-1, :-1], idx[:-1, 1:], gx)        # the cell's bottom edge
        edge(idx[1:, :-1], idx[1:, 1:], gx)          # top edge
        edge(idx[:-1, :-1], idx[1:, :-1], gy)        # left edge
        edge(idx[:-1, 1:], idx[1:, 1:], gy)          # right edge
        n = nx * ny
        return sp.csr_matrix((np.concatenate(vals), (np.concatenate(rows), np.concatenate(cols))), shape=(n, n))

    own = owner.ravel()
    free = np.flatnonzero(own < 0)
    fixed = np.flatnonzero(own >= 0)

    def capacitance(er_row: np.ndarray) -> np.ndarray:
        """The Maxwell capacitance matrix (F/m) of the signal conductors for these permittivities."""
        A = laplacian(er_row)
        Aff = A[free][:, free].tocsc()
        Afc = A[free][:, fixed]
        # minimum degree on A+Aᵀ in symmetric mode: half the fill of the default COLAMD on this symmetric matrix
        lu = splu(Aff, permc_spec="MMD_AT_PLUS_A", diag_pivot_thresh=0.0, options={"SymmetricMode": True})
        C = np.zeros((n_sig, n_sig))
        for k in range(1, n_sig + 1):
            phi = np.zeros(nx * ny)
            phi[own == k] = 1.0
            phi[free] = lu.solve(-(Afc @ phi[fixed]))
            flux = A @ phi                                   # flux out of each node's dual cell (ε0 units)
            for m in range(1, n_sig + 1):
                C[m - 1, k - 1] = EPS0 * flux[own == m].sum()
        return (C + C.T) / 2                                 # symmetric up to round-off
    return capacitance(er_rows), capacitance(np.ones_like(er_rows)), nx * ny
