"""Block-structured adaptive mesh for the thermal model.

The board plane is covered by square blocks of ``B`` x ``B`` cells. A block can be
split into four children with cells half the size, so level ``l`` has cells of
``dx0 / 2**l``. Neighbouring blocks differ by at most one level (2:1 balance), so
each cell face touches either one cell of the same size, one cell twice the size,
or (seen from the coarse side) two half-size cells.

Every cell is a column through the stack. Each level has its own through-plane
split: dielectrics are graded with sub-slabs about half a cell thick next to the
copper (see ``dielectric_split``), so small cells get thin slabs and large cells
stay cheap. Columns of different levels connect over the overlap of their slabs.

Refinement is driven by where heat is generated and how unevenly it is spread
inside a cell (``plan_refinement``): a uniformly heated pour needs no refinement,
while a narrow trace or a small powered pad inside a large cell does.
"""

from __future__ import annotations

import heapq
import math
from collections import defaultdict
from dataclasses import dataclass, field

import numpy as np
import shapely
from shapely.strtree import STRtree

from ..kicad.board import Board
from .geometry import drilled_holes, layer_geoms
from .raster import Grid, coverage, explode, face_coverage
from .stack import Layer

B = 8  # cells per block side
GROUP = 8  # blocks per raster window side
MAX_LEVELS = 6

MAX_DIELECTRIC_SLAB_MM = 0.8
Z_FIRST = 0.5  # first dielectric sub-slab next to copper, as a fraction of the in-plane cell
Z_GROWTH = 2.0

K_REF = 0.5  # W/mK, conductance scale that turns misplaced heat into a temperature error estimate
W, E, S, N = 0, 1, 2, 3
DIRS = {W: (0, -1), E: (0, 1), S: (-1, 0), N: (1, 0)}  # (di, dj); row i runs along y


@dataclass
class Slab:
    """One through-plane layer of the mesh: a copper layer, or a sub-slab of a dielectric."""
    name: str
    kind: str  # copper | dielectric
    t: float  # m
    z_bot: float  # mm
    z_top: float
    layer: str  # stack layer name
    cu_index: int = -1


def dielectric_split(t: float, dx: float) -> list[float]:
    """Sub-slab thicknesses (mm, top to bottom) for a dielectric of thickness ``t``.

    Thin next to the copper on both faces and growing geometrically towards the
    middle. Heat entering from a feature about one cell wide spreads within a
    depth of about one cell, so a uniform split into thick slabs would force it
    through a narrow column and over-predict small-feature temperatures.
    """
    # sub-slabs from each face inwards: Z_FIRST * dx, then growing by Z_GROWTH, while they fit in half the
    # thickness and stay under MAX_DIELECTRIC_SLAB_MM; the middle takes the rest (a thin remainder merges with
    # the last pair), split evenly if thicker than the maximum
    side, nxt = [], max(Z_FIRST * dx, 1e-3)
    while nxt <= MAX_DIELECTRIC_SLAB_MM and 2 * (sum(side) + nxt) < t:
        side.append(nxt)
        nxt *= Z_GROWTH
    mid = t - 2 * sum(side)
    if side and mid < 0.5 * side[-1]:
        mid += 2 * side.pop()
    n = max(1, math.ceil(mid / MAX_DIELECTRIC_SLAB_MM - 1e-9))
    return side + [mid / n] * n + side[::-1]


def build_slabs(stack: list[Layer], copper_layers: list[str], dx: float) -> list[Slab]:
    """The slabs of a column with in-plane cell `dx`, top to bottom: each copper layer, each dielectric split by
    dielectric_split."""
    slabs: list[Slab] = []
    for l in stack:
        if l.kind == "copper":
            slabs.append(Slab(l.name, "copper", l.t * 1e-3, l.z_bot, l.z_top, l.name, copper_layers.index(l.name)))
            continue
        parts = dielectric_split(l.t, dx)
        zt = l.z_top
        for k, tk in enumerate(parts):
            name = l.name + (f" ({k + 1}/{len(parts)})" if len(parts) > 1 else "")
            slabs.append(Slab(name, "dielectric", tk * 1e-3, zt - tk, zt, l.name))
            zt -= tk
    return slabs


def slab_count(stack: list[Layer], dx: float) -> int:
    """How many slabs build_slabs makes for cell size dx (the cost of a column)."""
    return sum(1 if l.kind == "copper" else len(dielectric_split(l.t, dx)) for l in stack)


# ---- refinement --------------------------------------------------------------------------

@dataclass
class HeatSamples:
    """Heat input as small squares: lower-left corner, side (mm) and power (W)."""
    x: np.ndarray
    y: np.ndarray
    s: np.ndarray
    p: np.ndarray

    @classmethod
    def empty(cls):
        """No samples."""
        z = np.zeros(0)
        return cls(z, z, z, z)

    def __add__(self, o):
        """Both sets of samples together."""
        return HeatSamples(*(np.concatenate([a, b]) for a, b in
                             ((self.x, o.x), (self.y, o.y), (self.s, o.s), (self.p, o.p))))

    def split(self, smax: float) -> "HeatSamples":
        """Subdivide samples larger than ``smax`` so each fits inside one cell of that size."""
        # a sample of side s becomes m x m squares of side s/m, each with p/m^2; done per distinct m so the
        # sub-squares are laid out with one broadcast per group
        m = np.maximum(1, np.ceil(self.s / smax - 1e-9)).astype(np.int64)
        if (m == 1).all():
            return self
        keep = m == 1
        parts = [HeatSamples(self.x[keep], self.y[keep], self.s[keep], self.p[keep])]
        for mv in np.unique(m[~keep]):
            sel = m == mv
            x, y, s, p = self.x[sel], self.y[sel], self.s[sel] / mv, self.p[sel] / mv ** 2
            a, b = np.meshgrid(np.arange(mv), np.arange(mv))
            a, b = a.ravel(), b.ravel()
            parts.append(HeatSamples((x[:, None] + a * s[:, None]).ravel(), (y[:, None] + b * s[:, None]).ravel(),
                                     np.repeat(s, mv * mv), np.repeat(p, mv * mv)))
        out = parts[0]
        for q in parts[1:]:
            out = out + q
        return out


def shape_samples(geom, power: float, ds: float) -> HeatSamples:
    """Spread ``power`` uniformly over a polygon, sampled on squares of side ``ds``."""
    x0, y0, x1, y1 = geom.bounds
    g = Grid(x0, y0, ds, max(1, math.ceil((x1 - x0) / ds)), max(1, math.ceil((y1 - y0) / ds)))
    c = coverage([geom], g, supersample=4)
    ii, jj = np.nonzero(c > 0)
    if len(ii) == 0 or power <= 0:
        return HeatSamples.empty()
    w = c[ii, jj].astype(np.float64)
    return HeatSamples(x0 + jj * ds, y0 + ii * ds, np.full(len(ii), ds), power * w / w.sum())


def _refine_block(leaves, l, b):
    """Replace leaf block b of level l by its four children at level l + 1 (block indices double)."""
    leaves[l].discard(b)
    bi, bj = b
    leaves.setdefault(l + 1, set()).update({(2 * bi + a, 2 * bj + c) for a in (0, 1) for c in (0, 1)})


def _covering_level(leaves, l, bi, bj):
    """Level of the leaf that covers block (bi, bj) of level ``l`` (None if finer or absent)."""
    for m in range(l, -1, -1):
        if (bi >> (l - m), bj >> (l - m)) in leaves.get(m, ()):
            return m
    return None


def plan_refinement(grid0: Grid, samples: HeatSamples, tol_k: float, dx_min: float, budget: float,
                    stack: list[Layer]) -> tuple[dict[int, set], dict]:
    """Choose leaf blocks. Returns (leaves by level, info).

    Heat is binned at the finest cell size allowed. Each cell is rated by how much of
    its heat a uniform spread over the cell would put in the wrong place (``misplaced``,
    W); ``misplaced / (K_REF * dx)`` is a rough local temperature error, and a block is
    rated by its worst cell. The worst block anywhere, at any level, is refined first
    (with its neighbours, so copper edges and gaps next to the heat are resolved as
    well), until every block is within ``tol_k`` or the extra-unknown ``budget`` is spent.
    """
    nbx, nby = math.ceil(grid0.nx / B), math.ceil(grid0.ny / B)
    leaves: dict[int, set] = {0: {(i, j) for i in range(nby) for j in range(nbx)}}
    info = {"levels": 0, "limited": False, "remaining_err": 0.0}
    n_fine = 0
    while n_fine < MAX_LEVELS - 1 and grid0.dx / 2 ** (n_fine + 1) >= dx_min * (1 - 1e-9):
        n_fine += 1
    if n_fine == 0 or samples.p.size == 0 or samples.p.sum() <= 0:
        return leaves, info
    # Heat binned once at the finest level the mesh may reach. Judging a cell by its
    # children alone would miss a trace centred in it (the halves look alike).
    dxf = grid0.dx / 2 ** n_fine
    sm = samples.split(dxf)
    nxf = (nbx * B) << n_fine
    fi = np.floor((sm.y + 0.5 * sm.s - grid0.y0) / dxf).astype(np.int64)
    fj = np.floor((sm.x + 0.5 * sm.s - grid0.x0) / dxf).astype(np.int64)
    ok = (fi >= 0) & (fj >= 0) & (fi < (nby * B) << n_fine) & (fj < nxf)
    fine_key, inv = np.unique(fi[ok] * nxf + fj[ok], return_inverse=True)
    p_fine = np.bincount(inv, weights=sm.p[ok])
    fi, fj = fine_key // nxf, fine_key % nxf

    # Worst cell error per block, for every level that may be refined
    block_err: list[dict] = []
    for l in range(n_fine):
        sh = n_fine - l
        nxl = (nbx * B) << l
        parent, pinv = np.unique((fi >> sh) * nxl + (fj >> sh), return_inverse=True)
        n_sub = 4 ** sh
        mean = np.bincount(pinv, weights=p_fine) / n_sub
        nbin = np.bincount(pinv)
        # Heat a uniform spread over the cell would put in the wrong place: half the sum of |fine power - mean|
        # over the cell's n_sub fine cells (those without heat deviate by the whole mean). Divided by a
        # conductance K_REF * dx it is a temperature error in K.
        dev = np.bincount(pinv, weights=np.abs(p_fine - mean[pinv])) + (n_sub - nbin) * mean
        err = 0.5 * dev / (K_REF * grid0.dx / 2 ** l * 1e-3)
        # the worst cell of each block whose error is over the tolerance (sorted by block, max per run of keys)
        hot = err >= tol_k
        bkey = ((parent[hot] // nxl) // B) * (nbx << l) + (parent[hot] % nxl) // B
        be: dict[tuple[int, int], float] = {}
        if hot.any():
            order = np.argsort(bkey, kind="stable")
            ub, start = np.unique(bkey[order], return_index=True)
            mx = np.maximum.reduceat(err[hot][order], start)
            be = {(int(k // (nbx << l)), int(k % (nbx << l))): float(e) for k, e in zip(ub, mx)}
        block_err.append(be)

    # unknowns added by refining one block of level l: its B*B columns become 4 B*B columns, which have more
    # slabs each (thinner cells get thinner dielectric sub-slabs)
    cost = [B * B * (4 * slab_count(stack, grid0.dx / 2 ** (l + 1)) - slab_count(stack, grid0.dx / 2 ** l))
            for l in range(n_fine)]
    spent = 0.0

    def refine(l, b):
        """Refine a leaf, first refining coarser face neighbours so the 2:1 balance holds."""
        nonlocal spent
        bi, bj = b
        for di, dj in DIRS.values():
            ni, nj = bi + di, bj + dj
            if not (0 <= ni < nby << l and 0 <= nj < nbx << l):
                continue
            m = _covering_level(leaves, l, ni, nj)
            while m is not None and m < l:
                refine(m, (ni >> (l - m), nj >> (l - m)))
                m = _covering_level(leaves, l, ni, nj)
        if b in leaves.get(l, ()):
            _refine_block(leaves, l, b)
            spent += cost[l]
            info["levels"] = max(info["levels"], l + 1)
            for a in (0, 1):
                for c in (0, 1):
                    ch = (2 * bi + a, 2 * bj + c)
                    if l + 1 < n_fine and ch in block_err[l + 1]:
                        heapq.heappush(heap, (-block_err[l + 1][ch], l + 1, ch))

    # Greedy refinement from a max-heap of block errors (negated: heapq is a min-heap): the worst block and its
    # eight neighbours are refined; their children's errors join the heap. A block popped that is no longer a
    # leaf (refined meanwhile as someone's neighbour) is skipped. Stops when the budget would be exceeded.
    heap = [(-e, 0, b) for b, e in block_err[0].items()]
    heapq.heapify(heap)
    while heap:
        e, l, b = heapq.heappop(heap)
        if b not in leaves.get(l, ()):
            continue
        group = [b] + [(b[0] + di, b[1] + dj) for di in (-1, 0, 1) for dj in (-1, 0, 1) if di or dj]
        group = [g for g in group if g in leaves.get(l, ())]
        if spent + cost[l] * len(group) > budget:
            info["limited"] = True
            info["remaining_err"] = -e
            break
        for g in group:
            refine(l, g)
    return {k: v for k, v in leaves.items() if v}, info


# ---- mesh -------------------------------------------------------------------------------------

@dataclass
class Level:
    """The leaf cells of one refinement level: their lattice indices (sorted keys for binary search), copper
    coverage and face openings per copper layer, solder-mask openings, and the raster windows they came from."""
    l: int
    dx: float  # mm
    nx: int  # lattice size in cells
    ny: int
    slabs: list[Slab]
    cu_slab: np.ndarray  # slab index of each copper layer
    col0: int = 0
    keys: np.ndarray = field(default_factory=lambda: np.zeros(0, np.int64))  # sorted I * nx + J
    I: np.ndarray = field(default_factory=lambda: np.zeros(0, np.int64))
    J: np.ndarray = field(default_factory=lambda: np.zeros(0, np.int64))
    cov: np.ndarray = None  # (ncu, n) copper coverage
    faces: np.ndarray = None  # (ncu, 4, n) open copper fraction of the W, E, S, N faces
    mask_top: np.ndarray = None  # (n,) solder-mask-free (pad) coverage
    mask_bot: np.ndarray = None
    windows: list = field(default_factory=list)  # (Grid, ii, jj, column) for output patches

    @property
    def n(self) -> int:
        """Number of columns (leaf cells) at this level."""
        return len(self.keys)


class Mesh:
    """The adaptive mesh: every leaf cell of every level is a column through the stack. Columns are numbered
    level by level (Level.col0 is a level's first), and the per-column arrays (level, lattice indices, size,
    centre, area, copper coverage) are what the thermal model is assembled from."""

    def __init__(self, board: Board, stack: list[Layer], grid0: Grid, leaves: dict[int, set], progress):
        """Rasterise the board on each level's leaf blocks: the board outline (drill holes cut out), each copper
        layer's coverage and face openings, and the pads' solder-mask openings. The polygons are indexed once in
        STRtrees; blocks are rasterised in windows of GROUP x GROUP blocks."""
        self.grid0 = grid0
        self.nbx, self.nby = math.ceil(grid0.nx / B), math.ceil(grid0.ny / B)
        cu = board.copper_layers
        self.ncu = len(cu)
        n_levels = max(leaves) + 1
        outline = board.outline
        holes = drilled_holes(board)
        if holes:
            outline = outline.difference(shapely.union_all(holes))
        geoms = layer_geoms(board)
        layer_polys = []
        for L in cu:
            polys = explode(geoms.get(L, []))
            layer_polys.append((np.array(polys, dtype=object), STRtree(polys) if polys else None))
        mask_polys = []
        for attr in ("mask_top", "mask_bottom"):
            polys = explode([p.shape for p in board.pads if getattr(p, attr) and p.layers])
            mask_polys.append((np.array(polys, dtype=object), STRtree(polys) if polys else None))
        total = sum(len(v) for v in leaves.values())
        done = 0
        self.levels: list[Level] = []
        col0 = 0
        for l in range(n_levels):
            dx = grid0.dx / 2 ** l
            slabs = build_slabs(stack, cu, dx)
            cu_slab = np.array([next(s for s, sl in enumerate(slabs) if sl.kind == "copper" and sl.layer == L)
                                for L in cu])
            lv = Level(l, dx, (self.nbx * B) << l, (self.nby * B) << l, slabs, cu_slab, col0)
            groups = defaultdict(list)
            for bi, bj in leaves.get(l, ()):
                groups[(bi // GROUP, bj // GROUP)].append((bi, bj))
            parts = []
            for blist in groups.values():
                progress.update(0.05 + 0.65 * done / max(1, total), f"Meshing level {l} ({dx:.3g} mm cells)")
                done += len(blist)
                parts.append(self._raster_window(lv, blist, outline, layer_polys, mask_polys, progress))
            parts = [p for p in parts if p is not None]
            if parts:
                I = np.concatenate([p[0] for p in parts])
                J = np.concatenate([p[1] for p in parts])
                # the windows' cells in one array per field, sorted by key (find() binary-searches it); each
                # window remembers its cells' column numbers for the output patches
                keys = I * lv.nx + J
                order = np.argsort(keys, kind="stable")
                lv.keys, lv.I, lv.J = keys[order], I[order], J[order]
                lv.cov = np.concatenate([p[2] for p in parts], axis=1)[:, order]
                lv.faces = np.concatenate([p[3] for p in parts], axis=2)[:, :, order]
                lv.mask_top = np.concatenate([p[4] for p in parts])[order]
                lv.mask_bot = np.concatenate([p[5] for p in parts])[order]
                rank = np.empty(len(order), np.int64)
                rank[order] = np.arange(len(order))
                start = 0
                for (win, ii, jj), p in zip([p[6] for p in parts], parts):
                    cnt = len(p[0])
                    lv.windows.append((win, ii, jj, col0 + rank[start:start + cnt]))
                    start += cnt
            col0 += lv.n
            self.levels.append(lv)
        self.n_cols = col0
        self.col_level = np.concatenate([np.full(lv.n, lv.l, np.int16) for lv in self.levels])
        self.col_I = np.concatenate([lv.I for lv in self.levels])
        self.col_J = np.concatenate([lv.J for lv in self.levels])
        self.col_dx = np.concatenate([np.full(lv.n, lv.dx) for lv in self.levels])  # mm
        self.col_x = grid0.x0 + (self.col_J + 0.5) * self.col_dx
        self.col_y = grid0.y0 + (self.col_I + 0.5) * self.col_dx
        self.col_area = (self.col_dx * 1e-3) ** 2  # m²
        self.col_cov = np.concatenate([lv.cov for lv in self.levels if lv.n], axis=1)  # (ncu, n_cols)
        self._kd = None

    def _raster_window(self, lv: Level, blist, outline, layer_polys, mask_polys, progress):
        """Rasterise one window (the bounding box of some leaf blocks of a level): its active cells' indices,
        copper coverage, face openings and mask openings. A cell is active when it is a leaf and at least half on
        the board, or carries copper while touching the board at all (copper at the board edge). None when the
        window has no active cell."""
        g0 = self.grid0
        bi0 = min(b[0] for b in blist)
        bj0 = min(b[1] for b in blist)
        bi1 = max(b[0] for b in blist)
        bj1 = max(b[1] for b in blist)
        win = Grid(g0.x0 + bj0 * B * lv.dx, g0.y0 + bi0 * B * lv.dx, lv.dx, (bj1 - bj0 + 1) * B, (bi1 - bi0 + 1) * B)
        leaf = np.zeros((win.ny, win.nx), bool)
        for bi, bj in blist:
            leaf[(bi - bi0) * B:(bi - bi0 + 1) * B, (bj - bj0) * B:(bj - bj0 + 1) * B] = True
        wx0, wy0, wx1, wy1 = win.extent
        pad = 2 * lv.dx
        box = shapely.box(wx0 - pad, wy0 - pad, wx1 + pad, wy1 + pad)

        def clipped(polys_tree):
            """The polygons near the window (STRtree query), clipped to it plus a two-cell margin (faces on the
            window's edge need the copper just outside)."""
            polys, tree = polys_tree
            if tree is None:
                return []
            idx = tree.query(box)
            if len(idx) == 0:
                return []
            return [g for g in shapely.clip_by_rect(polys[idx], wx0 - pad, wy0 - pad, wx1 + pad, wy1 + pad)
                    if not g.is_empty]

        bcov = coverage([shapely.clip_by_rect(outline, wx0 - pad, wy0 - pad, wx1 + pad, wy1 + pad)], win,
                        cancel=progress.check)
        covs, fxs, fys = [], [], []
        for lp in layer_polys:
            gl = clipped(lp)
            covs.append(coverage(gl, win, cancel=progress.check))
            fx, fy = face_coverage(gl, win, cancel=progress.check)
            fxs.append(fx)
            fys.append(fy)
        any_cu = np.max(np.stack(covs), axis=0) if covs else np.zeros_like(bcov)
        act = leaf & ((bcov >= 0.5) | ((any_cu > 0.3) & (bcov > 0.05)))
        ii, jj = np.nonzero(act)
        if len(ii) == 0:
            return None
        cov = np.stack([c[ii, jj] for c in covs]) if covs else np.zeros((0, len(ii)), np.float32)
        faces = np.stack([np.stack([fx[ii, jj], fx[ii, jj + 1], fy[ii, jj], fy[ii + 1, jj]])
                          for fx, fy in zip(fxs, fys)]) if covs else np.zeros((0, 4, len(ii)), np.float32)
        mt, mb = (coverage(clipped(mp), win, supersample=4)[ii, jj] for mp in mask_polys)
        return (ii + bi0 * B, jj + bj0 * B, cov, faces, mt, mb, (win, ii, jj))

    # ---- lookups ---------------------------------------------------------------------------
    def find(self, l: int, I, J) -> np.ndarray:
        """Global column index of cell (I, J) of level ``l``, or -1."""
        I, J = np.asarray(I, np.int64), np.asarray(J, np.int64)
        out = np.full(I.shape, -1, np.int64)
        if l < 0 or l >= len(self.levels) or self.levels[l].n == 0:
            return out
        lv = self.levels[l]
        ok = (I >= 0) & (I < lv.ny) & (J >= 0) & (J < lv.nx)
        k = I * lv.nx + J
        pos = np.clip(np.searchsorted(lv.keys, k), 0, lv.n - 1)      # binary search in the sorted keys
        hit = ok & (lv.keys[pos] == k)
        out[hit] = lv.col0 + pos[hit]
        return out

    def locate(self, x, y, nearest: bool = True) -> np.ndarray:
        """Column containing each point; off-board points go to the nearest column."""
        x, y = np.asarray(x, float), np.asarray(y, float)
        out = np.full(x.shape, -1, np.int64)
        g0 = self.grid0
        for lv in reversed(self.levels):        # finest first: a point is in exactly one leaf
            todo = out < 0
            if not todo.any():
                break
            I = np.floor((y[todo] - g0.y0) / lv.dx).astype(np.int64)
            J = np.floor((x[todo] - g0.x0) / lv.dx).astype(np.int64)
            out[todo] = self.find(lv.l, I, J)
        miss = out < 0
        if nearest and miss.any():
            if self._kd is None:
                from scipy.spatial import cKDTree
                self._kd = cKDTree(np.column_stack([self.col_x, self.col_y]))
            _, out[miss] = self._kd.query(np.column_stack([x[miss], y[miss]]))
        return out

    def columns_in(self, geom) -> np.ndarray:
        """Columns whose centre lies inside ``geom``; falls back to the column under a point of it."""
        x0, y0, x1, y1 = geom.bounds
        g0 = self.grid0
        found = []
        for lv in self.levels:
            if lv.n == 0:
                continue
            j0, j1 = int(math.floor((x0 - g0.x0) / lv.dx)), int(math.floor((x1 - g0.x0) / lv.dx))
            i0, i1 = int(math.floor((y0 - g0.y0) / lv.dx)), int(math.floor((y1 - g0.y0) / lv.dx))
            i0, i1 = max(i0, 0), min(i1, lv.ny - 1)
            j0, j1 = max(j0, 0), min(j1, lv.nx - 1)
            if i1 < i0 or j1 < j0:
                continue
            rows = np.arange(i0, i1 + 1, dtype=np.int64)
            lo = np.searchsorted(lv.keys, rows * lv.nx + j0)
            hi = np.searchsorted(lv.keys, rows * lv.nx + j1 + 1)
            cnt = hi - lo
            if cnt.sum() == 0:
                continue
            # every key between lo and hi of each row, as one array: the rows' ranges laid end to end
            pos = np.repeat(lo - np.cumsum(np.concatenate([[0], cnt[:-1]])), cnt) + np.arange(cnt.sum())
            found.append(lv.col0 + pos)
        if found:
            c = np.concatenate(found)
            c = c[shapely.contains_xy(geom, self.col_x[c], self.col_y[c])]
            if len(c):
                return c
        p = geom.representative_point()
        return self.locate(np.array([p.x]), np.array([p.y]))

    # ---- output ----------------------------------------------------------------------------
    def to_level0(self, vals: np.ndarray) -> np.ndarray:
        """Area-weighted mean of per-column values on the level-0 grid (NaN off the board)."""
        g0 = self.grid0
        sh = self.col_level.astype(np.int64)    # a level-l cell lies in level-0 cell (I >> l, J >> l)
        I0, J0 = self.col_I >> sh, self.col_J >> sh
        ok = (I0 < g0.ny) & (J0 < g0.nx)
        flat = I0[ok] * g0.nx + J0[ok]
        a = self.col_area[ok]
        wsum = np.bincount(flat, weights=a, minlength=g0.nx * g0.ny)
        vsum = np.bincount(flat, weights=a * vals[ok], minlength=g0.nx * g0.ny)
        with np.errstate(invalid="ignore", divide="ignore"):
            out = np.where(wsum > 0, vsum / wsum, np.nan)
        return out.reshape(g0.ny, g0.nx).astype(np.float32)

    def patches(self, fields: list[np.ndarray]) -> list[tuple[int, Grid, list[np.ndarray]]]:
        """Refined windows as (level, grid, [array per field]) with NaN where not a leaf cell."""
        out = []
        for lv in self.levels[1:]:
            for win, ii, jj, cols in lv.windows:
                arrs = []
                for f in fields:
                    a = np.full((win.ny, win.nx), np.nan, np.float32)
                    a[ii, jj] = f[cols]
                    arrs.append(a)
                out.append((lv.l, win, arrs))
        return out

    def summary(self) -> list[dict]:
        """Per level: cell size, columns and slabs (for the solve's summary)."""
        return [{"level": lv.l, "cell_mm": lv.dx, "columns": lv.n, "slabs": len(lv.slabs)}
                for lv in self.levels if lv.n]
