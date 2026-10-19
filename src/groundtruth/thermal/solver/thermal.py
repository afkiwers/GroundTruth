"""3-D steady-state conduction model of the board (finite volumes [Patankar 1980]; source keys:
docs/REFERENCES.md).

The board is discretised on the adaptive mesh of ``mesh.py``: square columns of
several sizes, each split through the stack into slabs (one per copper layer,
graded sub-slabs per dielectric). Copper slabs get an effective conductivity
from their local copper coverage (and cut-cell face apertures in-plane) [A29], and
dielectric slabs carry the extra through-plane conduction of via and PTH
barrels. Faces lose heat by convection and radiation (to ambient, or to a lumped
enclosure).
"""

from __future__ import annotations

import math

import numpy as np
import scipy.sparse as sp

from ..kicad.board import Board
from ..materials import COPPER, WALL_MATERIALS
from ..project import BoardSetup, Environment
from . import convection as C
from .common import Progress, SolverError, SPDSolver, laplacian
from .geometry import via_span
from .mesh import DIRS, MAX_DIELECTRIC_SLAB_MM, Mesh, Slab, dielectric_split  # noqa: F401 (re-exported)
from .stack import Layer

K_RESIN = 0.3  # W/mK, resin/soldermask between copper features [A41]


class ThermalModel:
    """The board as a thermal resistor network: one node per slab of every column, conductances between
    neighbouring nodes in a graph Laplacian L, and the faces' convection, radiation and mounts on the diagonal.
    solve() gives the steady temperatures for a heat input Q: (L + diag(G)) T = Q + G T_surroundings."""

    def __init__(self, board: Board, setup: BoardSetup, env: Environment, stack: list[Layer], mesh: Mesh,
                 progress: Progress):
        """Assemble the conduction network on `mesh`: in-plane conductances between neighbouring columns (same
        level, and small cell to the cell twice its size), through-plane conductances within each column, and the
        board's exposed edges, emissivities and mounts."""
        self.board, self.setup, self.env, self.progress = board, setup, env, progress
        self.stack = stack
        self.mesh = m = mesh
        self.grid = mesh.grid0
        self.warnings: list[str] = []
        if m.n_cols == 0:
            raise SolverError("The board outline is empty at this cell size.")
        self.slabs = m.levels[0].slabs
        layer_by_name = {l.name: l for l in stack}
        self.ncu = len(board.copper_layers)

        # Node layout: per level, slab-major blocks of that level's columns.
        self.node0 = np.zeros(len(m.levels), np.int64)
        n = 0
        for lv in m.levels:
            self.node0[lv.l] = n
            n += lv.n * len(lv.slabs)
        self.n = n
        self.col_area = m.col_area
        lvl = m.col_level.astype(np.int64)
        self._col_node0 = self.node0[lvl] - np.array([m.levels[l].col0 for l in range(len(m.levels))])[lvl]
        self._col_ncol = np.array([lv.n for lv in m.levels])[lvl]
        self._cu_slab = np.stack([lv.cu_slab for lv in m.levels])  # (levels, ncu)
        self.top = self._node(np.arange(m.n_cols), np.zeros(m.n_cols, np.int64))
        self.bot = self._node(np.arange(m.n_cols), np.array([len(lv.slabs) - 1 for lv in m.levels])[lvl])

        progress.update(0.75, "Assembling conduction model")
        barrel = self._barrel_area()  # dielectric layer name -> per-column barrel copper area (m²)
        ea, eb, eg = [], [], []
        exposed = 4 * m.col_dx * 1e-3  # perimeter not shared with another board cell (m)
        self._kz = []
        for lv in m.levels:
            if lv.n == 0:
                self._kz.append([])
                continue
            cols = np.arange(lv.col0, lv.col0 + lv.n)
            area = (lv.dx * 1e-3) ** 2
            kz = []
            for sl in lv.slabs:
                # through-plane conductivity: a copper slab's coverage-weighted mix of copper and resin (parallel
                # paths); a dielectric's own k_z plus the via barrels crossing it (their copper area as a share of
                # the cell), capped at solid copper
                if sl.kind == "copper":
                    f = lv.cov[sl.cu_index].astype(np.float64)
                    kz.append(f * COPPER.k + (1 - f) * K_RESIN)
                else:
                    l = layer_by_name[sl.layer]
                    kzv = np.full(lv.n, l.k_z)
                    extra = barrel.get(sl.layer)
                    if extra is not None:
                        kzv = np.minimum(kzv + COPPER.k * extra[cols] / area, COPPER.k)
                    kz.append(kzv)
            self._kz.append(kz)
            # Same-level neighbours (east and north, so each face once)
            for d in (1, 3):
                di, dj = DIRS[d]
                nb = m.find(lv.l, lv.I + di, lv.J + dj)
                ok = nb >= 0
                a, b = cols[ok], nb[ok]
                for s, sl in enumerate(lv.slabs):
                    if sl.kind == "copper":
                        # Cut-cell conduction: copper share of the shared face, resin elsewhere.
                        f = lv.faces[sl.cu_index, d][ok]
                        kf = f * COPPER.k + (1 - f) * K_RESIN
                    else:
                        kf = layer_by_name[sl.layer].k_xy
                    # g = k * (face area t*dx) / (centre distance dx) = k t: the cell size cancels
                    ea.append(self._node(a, s))
                    eb.append(self._node(b, s))
                    eg.append(np.broadcast_to(sl.t * kf, a.shape))
                exposed[a] -= lv.dx * 1e-3
                exposed[b] -= lv.dx * 1e-3
            # Through-plane, within each column
            for s in range(len(lv.slabs) - 1):    # centre to centre: two half-slab resistances in series
                r = lv.slabs[s].t / (2 * kz[s]) + lv.slabs[s + 1].t / (2 * kz[s + 1])
                ea.append(self._node(cols, s))
                eb.append(self._node(cols, s + 1))
                eg.append(area / r)
            # Faces shared with a cell twice the size (level l-1)
            if lv.l == 0:
                continue
            cl = m.levels[lv.l - 1]
            pairs = self._slab_overlaps(cl.slabs, lv.slabs)
            for d, (di, dj) in DIRS.items():
                same = m.find(lv.l, lv.I + di, lv.J + dj)
                coarse = m.find(lv.l - 1, (lv.I + di) >> 1, (lv.J + dj) >> 1)
                ok = (same < 0) & (coarse >= 0)
                if not ok.any():
                    continue
                a, c = cols[ok], coarse[ok]
                # Two-point flux: face of the small cell, centres 0.5 dx + 1 dx apart, so g = t k / 1.5.
                for sc, sf, overlap in pairs:
                    sl = lv.slabs[sf]
                    if sl.kind == "copper":
                        f = lv.faces[sl.cu_index, d][ok]
                        kf = f * COPPER.k + (1 - f) * K_RESIN
                    else:
                        kf = layer_by_name[sl.layer].k_xy
                    ea.append(self._node(a, sf))
                    eb.append(self._node(c, sc))
                    eg.append(np.broadcast_to(overlap * kf / 1.5, a.shape))
                exposed[a] -= lv.dx * 1e-3
                np.add.at(exposed, c, -lv.dx * 1e-3)
        self.L = laplacian(self.n, np.concatenate(ea), np.concatenate(eb),
                           np.concatenate([np.asarray(g, np.float64) for g in eg]))

        # Boundary faces: top and bottom of every column, and the board edge (each column's perimeter not
        # shared with another cell, counted down above), slab by slab
        self.eps_top, self.eps_bot = self._emissivity()
        exposed = np.maximum(exposed, 0.0)
        edge_cols = np.nonzero(exposed > 1e-9 * m.col_dx)[0]
        en, earea = [], []
        for l in range(len(m.levels)):
            ec = edge_cols[m.col_level[edge_cols] == l]
            for s, sl in enumerate(m.levels[l].slabs):
                en.append(self._node(ec, s))
                earea.append(exposed[ec] * sl.t)
        self.edge_nodes = np.concatenate(en) if en else np.zeros(0, np.int64)
        self.edge_area = np.concatenate(earea) if earea else np.zeros(0)
        self.board_area = float(self.col_area.sum())
        x0, y0, x1, y1 = board.outline.bounds
        self.length = (x1 - x0) * 1e-3
        self.width = (y1 - y0) * 1e-3

        # Mounts
        self.mount_nodes, self.mount_g = self._mounts()

        self.solver = SPDSolver("Thermal")
        self._x = None
        self.t_air = env.ambient_c
        self.t_wall = env.ambient_c
        self.t_wall_out = env.ambient_c
        lv_txt = ", ".join(f"{lv.n:,} x {len(lv.slabs)} at {lv.dx:.3g} mm" for lv in m.levels if lv.n)
        progress.log(f"Thermal mesh: {self.n:,} unknowns ({lv_txt})")

    # ---- indexing --------------------------------------------------------------------------
    def _node(self, cols, slab):
        """Node of ``slab`` (index within the column's level) in each column."""
        cols = np.asarray(cols, np.int64)
        return self._col_node0[cols] + np.asarray(slab, np.int64) * self._col_ncol[cols] + cols

    def cu_nodes(self, cols, k) -> np.ndarray:
        """Copper-slab node of copper layer(s) ``k`` in each column."""
        cols = np.asarray(cols, np.int64)
        return self._node(cols, self._cu_slab[self.mesh.col_level[cols], k])

    def slab_of_copper(self, layer: str) -> int:
        """Level-0 slab index of a copper layer."""
        for s, sl in enumerate(self.slabs):
            if sl.kind == "copper" and sl.layer == layer:
                return s
        raise KeyError(layer)

    def node_column_slab(self, node: int) -> tuple[int, Slab]:
        """(column, slab) of a node (the inverse of _node: find the level by its first node, then divmod)."""
        l = int(np.searchsorted(self.node0, node, side="right") - 1)
        lv = self.mesh.levels[l]
        s, c = divmod(int(node - self.node0[l]), lv.n)
        return lv.col0 + c, lv.slabs[s]

    @staticmethod
    def _slab_overlaps(coarse: list[Slab], fine: list[Slab]) -> list[tuple[int, int, float]]:
        """(coarse slab, fine slab, shared thickness in m) for slabs of the same stack layer."""
        out = []
        for sc, a in enumerate(coarse):
            for sf, b in enumerate(fine):
                if a.layer != b.layer:
                    continue
                if a.kind == "copper":
                    out.append((sc, sf, b.t))
                    continue
                o = min(a.z_top, b.z_top) - max(a.z_bot, b.z_bot)
                if o > 1e-9:
                    out.append((sc, sf, o * 1e-3))
        return out

    def _barrel_area(self) -> dict[str, np.ndarray]:
        """{dielectric layer: per-column copper cross-section of the via and plated-hole barrels through it}
        (m²). A barrel's cross-section is the plating annulus, pi t (d + t) for drill d and plating t. It goes to
        the column holding the hole, or, for a hole wider than half a cell (cut out of the mesh), shared among the
        ring of columns around its wall."""
        b = self.board
        m = self.mesh
        t = self.setup.materials.plating_um * 1e-6
        out: dict[str, np.ndarray] = {}
        cu = b.copper_layers
        diel_between: dict[tuple[int, int], list[str]] = {}
        names = [l.name for l in self.stack]
        cu_pos = [names.index(L) for L in cu]

        def dielectrics(ia, ib):
            """The dielectric layers between copper layers ia and ib (cached)."""
            key = (ia, ib)
            if key not in diel_between:
                lo, hi = cu_pos[ia], cu_pos[ib]
                diel_between[key] = [n for n in names[lo:hi + 1] if n not in cu]
            return diel_between[key]

        items = [(v.x, v.y, v.drill, cu.index(via_span(b, v)[0]), cu.index(via_span(b, v)[-1])) for v in b.vias]
        items += [(p.x, p.y, p.drill, 0, len(cu) - 1) for p in b.pads if p.plated]
        if not items:
            return out
        from shapely.geometry import Point
        g0 = self.grid.extent
        xs = np.array([it[0] for it in items])
        ys = np.array([it[1] for it in items])
        at = m.locate(xs, ys)
        for (x, y, d, ia, ib), c in zip(items, at):
            if not (g0[0] <= x <= g0[2] and g0[1] <= y <= g0[3]):
                continue
            dx = m.col_dx[c]
            if d / 2 > 0.5 * dx:
                # Hole wider than a cell: the drill is cut out of the mesh, so the plating
                # goes to the ring of cells around the hole wall.
                ring = Point(x, y).buffer(d / 2 + 0.75 * dx).difference(Point(x, y).buffer(d / 2))
                cols = m.columns_in(ring)
            else:
                cols = np.array([c])
            a = math.pi * t * (d * 1e-3 + t) / len(cols)
            for dn in dielectrics(ia, ib):
                arr = out.setdefault(dn, np.zeros(m.n_cols))
                np.add.at(arr, cols, a)
        return out

    def _emissivity(self):
        """Top and bottom emissivity per column: the solder mask's, and bare copper's where a pad opens the mask
        (by the opening's share of the cell)."""
        env, m = self.env, self.mesh
        res = []
        for attr in ("mask_top", "mask_bot"):
            f = np.concatenate([getattr(lv, attr) for lv in m.levels if lv.n]).astype(np.float64)
            res.append(env.emissivity_mask * (1 - f) + env.emissivity_copper * f)
        return res

    def pad_nodes(self, pad) -> tuple[np.ndarray, np.ndarray]:
        """Thermal nodes under a pad (on each of its copper layers) and area weights."""
        cols = self.mesh.columns_in(pad.shape)
        a = self.col_area[cols]
        w = a / a.sum() / len(pad.layers)
        nodes, ws = [], []
        for L in pad.layers:
            nodes.append(self.cu_nodes(cols, self.board.copper_layers.index(L)))
            ws.append(w)
        return np.concatenate(nodes), np.concatenate(ws)

    def _mounts(self):
        """(nodes, conductances) to the chassis: each mount's resistance shared among its pads, and within a pad
        by area. A non-plated mounting hole uses the top and bottom cells around it."""
        nodes, g = [], []
        for mt in self.setup.mounts:
            pads = [self.board.pad(k) for k in mt.pads]
            pads = [p for p in pads if p is not None]
            if not pads or mt.resistance <= 0:
                continue
            for p in pads:
                if not p.layers:  # NPTH mounting hole: use the cells around it on the outer layers
                    from shapely.geometry import Point
                    ring = Point(p.x, p.y).buffer(max(p.drill, 1.0) * 0.9)
                    c = self.mesh.columns_in(ring)
                    n_ = np.concatenate([self.top[c], self.bot[c]])
                    w = np.concatenate([self.col_area[c], self.col_area[c]])
                else:
                    n_, w = self.pad_nodes(p)
                nodes.append(n_)
                g.append(w / w.sum() / (mt.resistance * len(pads)))
        if not nodes:
            return np.zeros(0, np.int64), np.zeros(0)
        return np.concatenate(nodes), np.concatenate(g)

    # ---- heat sources ------------------------------------------------------------------
    def component_heat(self) -> tuple[np.ndarray, list[dict]]:
        """The parts' dissipation as nodal heat input (W per node), and what was put in (component_heat_areas
        says where)."""
        Q = np.zeros(self.n)
        info = []
        for c in self.setup.components:
            if c.power <= 0:
                continue
            fp = self.board.footprint(c.ref)
            if fp is None:
                self.warnings.append(f"Component {c.ref} not found on the board; its power was ignored.")
                continue
            for geom, share, nodes_of in component_heat_areas(self.board, fp):
                if nodes_of == "pad":
                    n_, w = self.pad_nodes(geom)
                else:
                    cols = self.mesh.columns_in(geom)
                    n_ = self.top[cols] if nodes_of == "top" else self.bot[cols]
                    w = self.col_area[cols] / self.col_area[cols].sum()
                np.add.at(Q, n_, c.power * share * w)
            info.append({"ref": c.ref, "power": c.power, "theta_jb": c.theta_jb})
        return Q, info

    def electrical_map(self, el) -> sp.csr_matrix:
        """Sparse (thermal nodes x electrical cells) matrix of area shares.

        Each electrical cell's power is split over the thermal cells it overlaps, by
        overlap area times their copper coverage (so heat from a cell on a copper edge
        lands on the copper, not on resin next to it), and is conserved at any ratio of
        cell sizes. Its transpose gives each electrical cell the weighted temperature of
        those thermal cells.
        """
        m = self.mesh
        g0 = self.grid
        de = el.dx
        ne = el.n_cells
        x = g0.x0 + el.node_J * de
        y = g0.y0 + el.node_I * de
        centre = m.locate(x + 0.5 * de, y + 0.5 * de)
        # Electrical cells inside thermal cells smaller than themselves are split first.
        msub = np.maximum(1, np.ceil(de / m.col_dx[centre] - 1e-6)).astype(np.int64)
        rows, cols, vals = [], [], []
        for mv in np.unique(msub):
            sel = np.nonzero(msub == mv)[0]
            s = de / mv
            for a in range(mv):
                for b in range(mv):
                    rx, ry = x[sel] + b * s, y[sel] + a * s
                    li = el.node_layer[sel].astype(np.int64)
                    cand, wts = [], []
                    eps = 1e-6 * s
                    # an electrical (sub)cell no larger than the thermal cells around it overlaps at most four of
                    # them: the ones under its corners (each counted once)
                    for cx, cy in ((rx + eps, ry + eps), (rx + s - eps, ry + eps),
                                   (rx + eps, ry + s - eps), (rx + s - eps, ry + s - eps)):
                        c = m.locate(cx, cy)
                        h = 0.5 * m.col_dx[c]
                        ox = np.clip(np.minimum(rx + s, m.col_x[c] + h) - np.maximum(rx, m.col_x[c] - h), 0, None)
                        oy = np.clip(np.minimum(ry + s, m.col_y[c] + h) - np.maximum(ry, m.col_y[c] - h), 0, None)
                        w = ox * oy * (m.col_cov[li, c] + 1e-3)
                        for pc in cand:
                            w = np.where(pc == c, 0.0, w)
                        cand.append(c)
                        wts.append(w)
                    wts = np.stack(wts)
                    tot = wts.sum(axis=0)
                    lost = tot <= 1e-12 * s * s  # all corners off the board: nearest column takes it
                    wts[0, lost] = 1.0
                    tot[lost] = 1.0
                    wts /= tot * mv * mv
                    for c, w in zip(cand, wts):
                        keep = w > 0
                        rows.append(self.cu_nodes(c[keep], li[keep]))
                        cols.append(sel[keep])
                        vals.append(w[keep])
        return sp.csr_matrix((np.concatenate(vals), (np.concatenate(rows), np.concatenate(cols))),
                             shape=(self.n, ne))

    # ---- boundary conditions --------------------------------------------------------
    def boundary(self, T: np.ndarray):
        """Return (G_conv, G_rad, t_rad_surr) arrays over all nodes for the current T."""
        env = self.env
        encl = env.enclosure.enabled
        A = self.col_area
        t_top = float(np.sum(T[self.top] * A) / A.sum())
        t_bot = float(np.sum(T[self.bot] * A) / A.sum())
        t_air = self.t_air if encl else env.ambient_c
        h_top = C.board_h(env, "top", t_top, t_air, self.length, self.width, natural_only=encl)
        h_bot = C.board_h(env, "bottom", t_bot, t_air, self.length, self.width, natural_only=encl)
        h_edge = 0.5 * (h_top + h_bot)                  # the board edge: the two faces' mean [A41]
        Gc = np.zeros(self.n)
        Gr = np.zeros(self.n)
        Gc[self.top] += h_top * A
        Gc[self.bot] += h_bot * A
        np.add.at(Gc, self.edge_nodes, h_edge * self.edge_area)
        t_surr = self.t_wall if encl else env.ambient_c
        if env.radiation:
            eps_t, eps_b = self.eps_top, self.eps_bot
            if encl:
                e = env.enclosure
                a_in = 2 * (e.length * e.width + e.length * e.height + e.width * e.height) * 1e-6
                # radiation from a body enclosed by a surface: effective emissivity 1 / (1/e1 + A1/A2 (1/e2 - 1))
                # (the two-surface enclosure [Incropera 2011])
                ratio = 2 * self.board_area / a_in
                eps_t = 1 / (1 / eps_t + ratio * (1 / e.emissivity_inside - 1))
                eps_b = 1 / (1 / eps_b + ratio * (1 / e.emissivity_inside - 1))
            Gr[self.top] += C.h_radiation(T[self.top], t_surr, eps_t) * A
            Gr[self.bot] += C.h_radiation(T[self.bot], t_surr, eps_b) * A
            np.add.at(Gr, self.edge_nodes,
                      C.h_radiation(T[self.edge_nodes], t_surr, env.emissivity_mask) * self.edge_area)
        self.h_info = {"h_top": h_top, "h_bottom": h_bot}
        return Gc, Gr, t_air, t_surr

    def solve(self, Q: np.ndarray, T_prev: np.ndarray, tol: float = 1e-8) -> np.ndarray:
        """Temperatures for heat input Q, with the boundary coefficients linearised at T_prev (convection and
        radiation depend on temperature: the coupled loop calls this until T settles). The previous solution is
        the CG's starting point. With an enclosure, its air and wall temperatures are updated after."""
        Gc, Gr, t_air, t_surr = self.boundary(T_prev)
        Gm = np.zeros(self.n)
        np.add.at(Gm, self.mount_nodes, self.mount_g)
        t_chassis = self.t_wall if self.env.enclosure.enabled else self.env.ambient_c
        diag = Gc + Gr + Gm
        A = (self.L + sp.diags(diag)).tocsr()
        b = Q + Gc * t_air + Gr * t_surr + Gm * t_chassis
        x = self.solver.solve(A, b, self._x, self.progress, tol=tol)
        self._x = x
        self._last_bc = (Gc, Gr, Gm, t_air, t_surr, t_chassis)
        if self.env.enclosure.enabled:
            self._update_enclosure(x)
        return x

    def _update_enclosure(self, T):
        """Energy balance of the lumped enclosure (internal air, wall inside, wall outside)."""
        env, e = self.env, self.env.enclosure
        Gc, Gr, Gm, *_ = self._last_bc
        L, W, H = e.length * 1e-3, e.width * 1e-3, e.height * 1e-3
        t = max(e.wall_thickness, 0.1) * 1e-3
        a_in = 2 * (L * W + L * H + W * H)
        a_out = 2 * ((L + 2 * t) * (W + 2 * t) + (L + 2 * t) * (H + 2 * t) + (W + 2 * t) * (H + 2 * t))
        k_wall = WALL_MATERIALS.get(e.wall_material, 0.2)
        g_wall = k_wall * 0.5 * (a_in + a_out) / t
        h_in = C.h_vertical(self.t_air - self.t_wall, H, 0.5 * (self.t_air + self.t_wall))
        g_aw = h_in * a_in
        h_out = C.box_outer_h(env, self.t_wall_out, env.ambient_c, L, W, H)
        if env.radiation:
            h_out += float(C.h_radiation(self.t_wall_out, env.ambient_c, e.emissivity_outside))
        g_out = h_out * a_out
        # three heat balances: the air takes the board's convection and gives it to the wall inside; the wall
        # takes the board's radiation (and the mounts' heat) and conducts it out; the outside gives it to ambient
        sgc, stc = Gc.sum(), (Gc * T).sum()
        sgr, str_ = (Gr + Gm).sum(), ((Gr + Gm) * T).sum()
        ta = env.ambient_c
        # Unknowns [T_air, T_wall_in, T_wall_out]
        M = np.array([
            [sgc + g_aw, -g_aw, 0.0],
            [-g_aw, sgr + g_aw + g_wall, -g_wall],
            [0.0, -g_wall, g_wall + g_out],
        ])
        rhs = np.array([stc, str_, g_out * ta])
        self.t_air, self.t_wall, self.t_wall_out = np.linalg.solve(M, rhs)

    def heat_out(self, T):
        """Heat leaving the board at temperatures T (W): convection, radiation and mounts (for the energy
        balance check)."""
        Gc, Gr, Gm, t_air, t_surr, t_ch = self._last_bc
        return float((Gc * (T - t_air) + Gr * (T - t_surr) + Gm * (T - t_ch)).sum())

    # ---- output ----------------------------------------------------------------------------
    def copper_values(self, V: np.ndarray) -> np.ndarray:
        """(ncu, n_cols) values of the copper-slab nodes of every column."""
        cols = np.arange(self.mesh.n_cols)
        return np.stack([V[self.cu_nodes(cols, k)] for k in range(self.ncu)])


def component_heat_areas(board: Board, fp):
    """Where a component's power enters: [(pad or shape, share, 'pad' | 'top' | 'bottom')]."""
    pads = [p for p in fp.pads if p.layers]
    if pads:
        areas = np.array([max(p.shape.area, 1e-6) for p in pads])
        return [(p, a, "pad") for p, a in zip(pads, areas / areas.sum())]
    geom = fp.courtyard
    if geom is None:
        from shapely.geometry import Point
        geom = Point(fp.x, fp.y).buffer(0.5)
    return [(geom, 1.0, "top" if fp.layer.startswith("F") else "bottom")]
