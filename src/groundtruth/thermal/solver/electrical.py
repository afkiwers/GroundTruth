"""DC current-flow model of the copper carrying the user's terminals.

Finite-volume discretisation [Patankar 1980] of div(sigma grad V) = 0 over each copper layer of
each involved net, with via/PTH barrels as vertical conductances. Conductance
between neighbouring cells is proportional to the open (copper) fraction of the
face they share (a cut-cell scheme [A29]), so traces at any angle keep their true
resistance even when only two or three cells wide.

Pads that belong to a terminal or link are merged into one equipotential node
(an ideal soldered connection).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import scipy.sparse as sp
from scipy.sparse.csgraph import connected_components

from ..kicad.board import Board, Pad
from ..materials import COPPER
from ..project import BoardSetup
from .common import Progress, SolverError, SPDSolver
from .geometry import copper_geoms, via_span
from .raster import Grid, cells_in, coverage, face_coverage
from .stack import Layer, copper_z

F_MIN = 0.02  # minimum copper coverage for a cell to become a node



@dataclass
class SubGrid:
    """The cells of one net on one copper layer: a window of the fine electrical grid with each cell's coverage,
    node number and face openings."""
    net: str
    layer: int  # copper layer index
    grid: Grid
    I0: int  # offset in global fine-cell indices
    J0: int
    cov: np.ndarray
    node: np.ndarray  # (ny, nx) int32, -1 where no copper
    fx: np.ndarray  # open fraction of vertical faces (ny, nx+1)
    fy: np.ndarray  # open fraction of horizontal faces (ny+1, nx)


@dataclass
class ViaRecord:
    """A via or plated hole in the model, and its barrel segments between copper layers (for its current)."""
    label: str
    x: float
    y: float
    drill: float
    net: str
    segments: list[int] = field(default_factory=list)


@dataclass
class Group:
    """Pads joined into one ideal node: a terminal (source, sink) or one end of a link."""
    kind: str  # source | sink | link
    name: str
    pads: list[Pad]
    current: float = 0.0
    nodes: np.ndarray | None = None


class ElectricalModel:
    """The DC network of the nets the terminals touch: cells, barrels and links as conductances, sources as
    injected currents, sinks held at 0 V. solve() gives the potentials (resistivity at each cell's temperature),
    cell_power() the Joule heat the thermal model receives."""

    def __init__(self, board: Board, setup: BoardSetup, stack: list[Layer], grid_t: Grid, ratio: int,
                 progress: Progress):
        """Build the model on a grid `ratio` times finer than the thermal grid grid_t (cells nest exactly):
        terminals and links into groups, the copper rasterised, conductances, groups merged, the matrix pattern."""
        self.board = board
        self.setup = setup
        self.ratio = ratio
        self.grid_t = grid_t
        self.dx = grid_t.dx / ratio  # mm
        self.progress = progress
        self.warnings: list[str] = []
        cu = board.copper_layers
        self.cu_layers = cu
        tmap = {l.name: l.t for l in stack if l.kind == "copper"}
        self.t_cu = np.array([tmap.get(L, 0.035) * 1e-3 for L in cu])  # m
        self.zc = copper_z(stack)
        self.plating = setup.materials.plating_um * 1e-6
        self.temp_dep = setup.materials.temperature_dependent_resistance

        self.groups = self._make_groups()
        nets = {p.net for g in self.groups for p in g.pads if p.net}
        if not nets:
            raise SolverError("The selected pads have no net assigned in KiCad.")
        self.nets = sorted(nets)
        self._build_subgrids()
        self._build_edges()
        self._build_groups()
        self._build_system()

    # ---- setup ------------------------------------------------------------------
    def _make_groups(self) -> list[Group]:
        """The terminals and each end of every link as groups; at least one source and one sink are required."""
        b, s = self.board, self.setup
        groups: list[Group] = []

        def pads_of(keys, owner):
            """The pads of these keys that exist and have copper (others noted as warnings)."""
            out = []
            for k in keys:
                p = b.pad(k)
                if p is None:
                    self.warnings.append(f"{owner}: pad {k} no longer exists on the board and was ignored.")
                elif not p.layers:
                    self.warnings.append(f"{owner}: pad {k} has no copper and was ignored.")
                else:
                    out.append(p)
            return out

        for t in s.terminals:
            pads = pads_of(t.pads, t.name)
            if pads:
                groups.append(Group(t.kind, t.name, pads, t.current if t.kind == "source" else 0.0))
        for l in s.links:
            for key in (l.pad_a, l.pad_b):
                pads = pads_of([key], l.name)
                if pads:
                    groups.append(Group("link", f"{l.name}:{key}", pads))
        if not any(g.kind == "source" for g in groups):
            raise SolverError("Add at least one current source terminal.")
        if not any(g.kind == "sink" for g in groups):
            raise SolverError("Add at least one sink (return) terminal.")
        return groups

    def _build_subgrids(self):
        """Rasterise each (net, layer) of the involved nets on its own window of the fine grid; a cell with more
        than F_MIN copper becomes a node. Fills the per-node lookups (layer, row, column, coverage)."""
        pr = self.progress
        geoms = copper_geoms(self.board, set(self.nets))
        r, gt = self.ratio, self.grid_t
        self.subgrids: list[SubGrid] = []
        n_nodes = 0
        items = [(net, li) for net in self.nets for li, L in enumerate(self.cu_layers) if geoms.get((net, L))]
        for k, (net, li) in enumerate(items):
            pr.update(k / max(1, len(items)), f"Meshing copper: {net or '(no net)'} on {self.cu_layers[li]}")
            gl = geoms[(net, self.cu_layers[li])]
            minx = min(g.bounds[0] for g in gl)
            miny = min(g.bounds[1] for g in gl)
            maxx = max(g.bounds[2] for g in gl)
            maxy = max(g.bounds[3] for g in gl)
            # Window aligned to thermal cells so fine cells nest exactly inside them.
            tj0 = max(0, int(math.floor((minx - gt.x0) / gt.dx)))
            ti0 = max(0, int(math.floor((miny - gt.y0) / gt.dx)))
            tj1 = min(gt.nx, int(math.ceil((maxx - gt.x0) / gt.dx)))
            ti1 = min(gt.ny, int(math.ceil((maxy - gt.y0) / gt.dx)))
            if tj1 <= tj0 or ti1 <= ti0:
                continue
            g = Grid(gt.x0 + tj0 * gt.dx, gt.y0 + ti0 * gt.dx, self.dx, (tj1 - tj0) * r, (ti1 - ti0) * r)
            cov = coverage(gl, g, cancel=pr.check)
            node = np.full(cov.shape, -1, np.int32)
            m = cov > F_MIN
            cnt = int(m.sum())
            if cnt == 0:
                continue
            node[m] = np.arange(n_nodes, n_nodes + cnt, dtype=np.int32)
            n_nodes += cnt
            fx, fy = face_coverage(gl, g, cancel=pr.check)
            self.subgrids.append(SubGrid(net, li, g, ti0 * r, tj0 * r, cov, node, fx, fy))
        self.n_cells = n_nodes
        # Per-node lookups
        self.node_layer = np.empty(n_nodes, np.int16)
        self.node_I = np.empty(n_nodes, np.int32)
        self.node_J = np.empty(n_nodes, np.int32)
        self.node_cov = np.empty(n_nodes, np.float32)
        for sg in self.subgrids:
            ii, jj = np.nonzero(sg.node >= 0)
            ids = sg.node[ii, jj]
            self.node_layer[ids] = sg.layer
            self.node_I[ids] = ii + sg.I0
            self.node_J[ids] = jj + sg.J0
            self.node_cov[ids] = sg.cov[ii, jj]
        pr.log(f"Electrical mesh: {n_nodes:,} copper cells at {self.dx * 1000:.0f} µm")

    def _subgrid(self, net: str, layer: int) -> SubGrid | None:
        """The subgrid of a net on a layer, None without copper there."""
        for sg in self.subgrids:
            if sg.net == net and sg.layer == layer:
                return sg
        return None

    def _build_edges(self):
        """The network's edges as (a, b, geometric conductance g/sigma): in-plane between neighbouring cells
        (thickness x open face fraction: the cut-cell scheme), and for every via or plated hole a node per layer
        tied to the cells around it, joined layer to layer by the barrel (plating area / length). Barrel edges
        carry a segment number so via currents can be read back."""
        ea, eb, eg, eseg = [], [], [], []
        for sg in self.subgrids:
            t = self.t_cu[sg.layer]
            for axis in (0, 1):
                if axis == 1:
                    a, b, face = sg.node[:, :-1], sg.node[:, 1:], sg.fx[:, 1:-1]
                else:
                    a, b, face = sg.node[:-1, :], sg.node[1:, :], sg.fy[1:-1, :]
                v = (a >= 0) & (b >= 0) & (face > 0)
                ea.append(a[v])
                eb.append(b[v])
                eg.append(t * face[v].astype(np.float64))  # square cells: dy/dx = 1
        n_inplane = sum(len(x) for x in ea)

        # Vertical barrels (vias and plated through-hole pads)
        self.vias: list[ViaRecord] = []
        barrels = []
        for v in self.board.vias:
            if v.net in self.nets:
                barrels.append((f"Via {v.x:.2f},{v.y:.2f}", v.x, v.y, v.drill, v.size / 2, v.net,
                                via_span(self.board, v)))
        for p in self.board.pads:
            if p.net in self.nets and p.plated and len(p.layers) > 1:
                # Equivalent radius of the annular pad including its hole
                r_p = math.sqrt((p.shape.area + math.pi * p.drill ** 2 / 4) / math.pi)
                barrels.append((f"{p.key} (PTH)", p.x, p.y, p.drill, r_p, p.net, p.layers))
        missing = 0
        seg_count = 0
        cu_index = {L: i for i, L in enumerate(self.cu_layers)}
        # Barrel nodes: one per via per connected layer, appended after the cell nodes.
        b_layer, b_I, b_J = [], [], []
        next_node = self.n_cells
        for label, x, y, d, r_pad, net, span in barrels:
            area = math.pi * self.plating * (d * 1e-3 + self.plating)
            r_b = d / 2 + self.plating * 1e3 / 2  # barrel wall radius, mm
            found = []  # (layer index, tied cell nodes, correction conductance)
            for L in span:
                li = cu_index[L]
                sg = self._subgrid(net, li)
                if sg is None:
                    continue
                ids, g_corr = self._barrel_connection(sg, x, y, r_b, max(r_pad, r_b))
                if ids:
                    found.append((li, ids, g_corr))
            if len(found) < 2:
                missing += 1
                continue

            def new_node(li, ref_cell):
                """A new barrel node on layer li, placed at ref_cell (for the maps)."""
                nonlocal next_node
                b_layer.append(li)
                b_I.append(self.node_I[ref_cell])
                b_J.append(self.node_J[ref_cell])
                next_node += 1
                return next_node - 1

            rec = ViaRecord(label, x, y, d, net)
            present = []
            for li, ids, g_corr in found:
                strong = 50.0 * self.t_cu[li]  # effectively ideal tie (0.02 squares)
                bn = new_node(li, ids[0])
                hub = bn
                if g_corr is not None:
                    hub = new_node(li, ids[0])
                    ea.append(np.array([bn]))
                    eb.append(np.array([hub]))
                    eg.append(np.array([g_corr]))
                    eseg.append(np.array([-1]))
                ea.append(np.full(len(ids), hub))
                eb.append(np.array(ids))
                eg.append(np.full(len(ids), strong / len(ids)))
                eseg.append(np.full(len(ids), -1))
                present.append((li, bn, None))
            for (la, ba, _), (lb, bb, _) in zip(present[:-1], present[1:]):
                lz = abs(self.zc[self.cu_layers[la]] - self.zc[self.cu_layers[lb]]) * 1e-3
                ea.append(np.array([ba]))
                eb.append(np.array([bb]))
                eg.append(np.array([area / lz]))
                eseg.append(np.array([seg_count]))
                rec.segments.append(seg_count)
                seg_count += 1
            self.vias.append(rec)
        if missing:
            self.warnings.append(f"{missing} via(s) connect to copper on fewer than two layers and were ignored.")
        # Extend per-node lookups with the barrel nodes (they sit at their via's cell).
        nb = next_node - self.n_cells
        if nb:
            self.node_layer = np.concatenate([self.node_layer, np.array(b_layer, np.int16)])
            self.node_I = np.concatenate([self.node_I, np.array(b_I, np.int32)])
            self.node_J = np.concatenate([self.node_J, np.array(b_J, np.int32)])
            self.node_cov = np.concatenate([self.node_cov, np.ones(nb, np.float32)])
        self.n_grid_cells = self.n_cells
        self.n_cells = next_node
        self.edge_a = np.concatenate(ea).astype(np.int64) if ea else np.zeros(0, np.int64)
        self.edge_b = np.concatenate(eb).astype(np.int64) if eb else np.zeros(0, np.int64)
        self.edge_g = np.concatenate(eg) if eg else np.zeros(0)
        self.edge_seg = np.concatenate([np.full(n_inplane, -1)] + eseg).astype(np.int64)
        self.n_segments = seg_count
        self.n_inplane = n_inplane

    def _barrel_connection(self, sg: SubGrid, x: float, y: float, r_b: float, r_p: float):
        """Where a barrel meets one layer: (cell nodes tied together, correction conductance or None).

        Inside the via's own pad (radius r_p) current flows radially into the
        barrel wall (radius r_b) whatever copper lies outside, so the cells whose
        centres fall inside the pad are tied together and the radial resistance
        ln(r_p / r_b) / (2 pi sigma t) is added in series.

        When the mesh is too coarse to put any cell centre inside the pad, the
        barrel joins the single cell under it. That cell's potential represents
        the field at Peaceman's radius r_eq = 0.198 dx; the part of the radial
        term beyond the pad (r_p to r_eq) is only real where copper surrounds the
        via (planes, pours), so it is weighted by the surrounding copper coverage.
        """
        dx = self.dx
        g = sg.grid
        ny, nx = sg.node.shape
        ci, cj = g.cell_of(x, y)
        ci, cj = int(ci), int(cj)

        def within(radius):
            """The copper cells whose centres are within `radius` of the hole."""
            n = int(math.ceil(radius / dx)) + 1
            out = []
            for i in range(max(0, ci - n), min(ny, ci + n + 1)):
                for j in range(max(0, cj - n), min(nx, cj + n + 1)):
                    if sg.node[i, j] >= 0 and \
                            math.hypot(g.x0 + (j + 0.5) * dx - x, g.y0 + (i + 0.5) * dx - y) <= radius:
                        out.append(int(sg.node[i, j]))
            return out

        def equiv_radius(n_t):
            """The radius a cluster of n_t tied cells acts like for radial current."""
            # Equivalent radius of n tied cells on a 5-point grid: Peaceman's 0.198 dx for one
            # cell; for clusters the square-block fit (0.5845 sqrt(n) - 0.48) dx, calibrated
            # numerically against the continuum log potential.
            return 0.198 * dx if n_t == 1 else (0.5845 * math.sqrt(n_t) - 0.48) * dx

        ids = within(r_p)
        centre = 0 <= ci < ny and 0 <= cj < nx and sg.node[ci, cj] >= 0
        if not ids and not centre:
            ids = within(r_p + dx)  # the drill removed the centre cell: tie the ring around it
        if not ids:
            if not (0 <= ci < ny and 0 <= cj < nx and sg.node[ci, cj] >= 0):
                return [], None
            ids = [int(sg.node[ci, cj])]
        # The tied cluster must be at least as large as the barrel, or the model would
        # over-predict resistance (a negative correction is not representable).
        radius = max(r_p, dx)
        while equiv_radius(len(ids)) < r_b and radius < r_p + 3 * dx:
            radius += 0.5 * dx
            ids = within(radius) or ids
        r_eq = equiv_radius(len(ids))
        if r_eq <= r_p:
            ln = math.log(max(r_eq, r_b) / r_b)
        else:
            nb = [sg.cov[a, b] if 0 <= a < ny and 0 <= b < nx else 0.0
                  for a, b in ((ci - 1, cj), (ci + 1, cj), (ci, cj - 1), (ci, cj + 1))]
            ln = math.log(r_p / r_b) + float(np.clip(np.mean(nb), 0.0, 1.0)) * math.log(r_eq / r_p)
        if ln < 1e-4:
            return ids, None
        return ids, 2 * math.pi * self.t_cu[sg.layer] / ln

    def _build_groups(self):
        """Merge each group's pad cells into one node (an ideal connection). Groups sharing copper are merged with
        union-find; a source and a sink on the same copper is an error. self.label maps every cell node to its
        merged node."""
        n = self.n_cells
        parent = list(range(len(self.groups)))

        def find(a):
            """Union-find root of group a (with path halving)."""
            while parent[a] != a:
                parent[a] = parent[parent[a]]
                a = parent[a]
            return a

        owner = np.full(n, -1, np.int64)
        for gi, g in enumerate(self.groups):
            ids = []
            for p in g.pads:
                got = False
                for L in p.layers:
                    li = self.cu_layers.index(L)
                    sg = self._subgrid(p.net, li)
                    if sg is None:
                        continue
                    ii, jj = cells_in(p.shape, sg.grid)
                    nid = sg.node[ii, jj]
                    nid = nid[nid >= 0]
                    if nid.size:
                        ids.append(nid)
                        got = True
                if not got:
                    raise SolverError(f"Pad {p.key} of '{g.name}' has no meshed copper. Try a finer electrical cell size.")
            g.nodes = np.unique(np.concatenate(ids))
            prev = owner[g.nodes]
            for h in np.unique(prev[prev >= 0]):
                parent[find(int(h))] = find(gi)
            owner[g.nodes] = gi
        roots = sorted({find(i) for i in range(len(self.groups))})
        for gi in range(len(self.groups)):
            r = find(gi)
            if r != gi and self.groups[r].kind != self.groups[gi].kind and "link" not in (self.groups[r].kind, self.groups[gi].kind):
                raise SolverError(f"'{self.groups[gi].name}' and '{self.groups[r].name}' share copper; "
                                  "a source and a sink cannot be the same pad.")
        # Label: merged index for every cell node
        label = np.arange(n, dtype=np.int64)
        root_index = {r: n + k for k, r in enumerate(roots)}
        for gi, g in enumerate(self.groups):
            label[g.nodes] = root_index[find(gi)]
        uniq, inv = np.unique(label, return_inverse=True)
        self.label = inv
        self.n_nodes = len(uniq)
        self.group_node = np.array([inv[g.nodes[0]] for g in self.groups], np.int64)
        self.is_super = np.zeros(self.n_nodes, bool)
        self.is_super[self.group_node] = True

    def _build_system(self):
        """Links as conductances between their ends' nodes; check every source reaches a sink (connected
        components); sinks are the 0 V (Dirichlet) nodes, copper not connected to a sink is left out (it carries no
        current); and the sparse matrix's pattern built once, so each solve only fills in the values."""
        nn = self.n_nodes
        # Link resistors between groups
        la, lb, lg, self.link_info = [], [], [], []
        gidx = {g.name: i for i, g in enumerate(self.groups)}
        for l in self.setup.links:
            a, b = gidx.get(f"{l.name}:{l.pad_a}"), gidx.get(f"{l.name}:{l.pad_b}")
            if a is None or b is None:
                continue
            if l.resistance <= 0:
                raise SolverError(f"Link '{l.name}' needs a resistance above zero.")
            la.append(self.group_node[a])
            lb.append(self.group_node[b])
            lg.append(1.0 / l.resistance)
            self.link_info.append((l.name, a, b))
        self.link_a = np.array(la, np.int64)
        self.link_b = np.array(lb, np.int64)
        self.link_g = np.array(lg)

        fa = np.concatenate([self.label[self.edge_a], self.link_a])
        fb = np.concatenate([self.label[self.edge_b], self.link_b])
        # Connectivity check
        keep = fa != fb
        W = sp.coo_matrix((np.ones(keep.sum()), (fa[keep], fb[keep])), shape=(nn, nn))
        ncomp, comp = connected_components(W, directed=False)
        sinks = {self.group_node[i] for i, g in enumerate(self.groups) if g.kind == "sink"}
        sink_comps = {comp[s] for s in sinks}
        for i, g in enumerate(self.groups):
            if g.kind == "source" and comp[self.group_node[i]] not in sink_comps:
                raise SolverError(f"No copper path from source '{g.name}' to any sink. "
                                  "Add a link (resistor) across the component in between, or choose other pads.")
        active = np.isin(comp, list(sink_comps))
        dirichlet = np.zeros(nn, bool)
        dirichlet[list(sinks)] = True
        free = active & ~dirichlet
        self.free_index = np.full(nn, -1, np.int64)
        self.free_index[free] = np.arange(free.sum())
        self.n_free = int(free.sum())
        self.active = active
        floating = int((~active).sum())
        if floating:
            self.progress.log(f"{floating:,} copper cells are not on a current path and carry no current.")

        # Unique node pairs so the matrix pattern can be built once.
        a, b = np.minimum(fa, fb), np.maximum(fa, fb)
        ok = (a != b) & active[a]
        self._edge_ok = ok
        key = a[ok] * nn + b[ok]
        pairs, self._pair_inv = np.unique(key, return_inverse=True)
        pa, pb = pairs // nn, pairs % nn
        self._pair_a, self._pair_b = pa, pb
        # Off-diagonal entries only where both ends are free
        fr_a, fr_b = self.free_index[pa], self.free_index[pb]
        both = (fr_a >= 0) & (fr_b >= 0)
        self._both = both
        nf = self.n_free
        rows = np.concatenate([fr_a[both], fr_b[both], np.arange(nf)])
        cols = np.concatenate([fr_b[both], fr_a[both], np.arange(nf)])
        # the CSR pattern built from entry numbers 1..k: after tocsr(), data[i] - 1 says which entry landed in slot
        # i, so a new set of values is placed with one fancy-index (self._perm) per solve
        order = sp.coo_matrix((np.arange(len(rows), dtype=np.float64) + 1, (rows, cols)), shape=(nf, nf)).tocsr()
        self._A = order
        self._perm = order.data.astype(np.int64) - 1
        self.rhs = np.zeros(nf)
        for i, g in enumerate(self.groups):
            if g.kind == "source":
                self.rhs[self.free_index[self.group_node[i]]] += g.current
        self.solver = SPDSolver("Electrical", method="aggregation")
        self.V = None
        self._x = None

    # ---- solve --------------------------------------------------------------------
    def edge_conductance(self, T_cell: np.ndarray | None) -> np.ndarray:
        """Each edge's conductance (S) at the cells' temperatures: sigma between two cells is the inverse of their
        mean resistivity (two half-cells in series)."""
        if T_cell is None or not self.temp_dep:
            rho = np.full(self.n_cells, COPPER.resistivity_20c)
        else:
            rho = COPPER.resistivity(T_cell)
        return 2.0 / (rho[self.edge_a] + rho[self.edge_b]) * self.edge_g

    def solve(self, T_cell: np.ndarray | None, tol: float = 1e-9) -> None:
        """Solve for potentials given cell temperatures (°C) or None for 20 °C."""
        # sum parallel edges per node pair, then the Laplacian of the free nodes: -G off the diagonal, the sum of
        # every edge at a node on it (edges to a sink add to the diagonal only: V = 0 there). Source currents are
        # the right-hand side.
        G_cells = self.edge_conductance(T_cell)
        G = np.concatenate([G_cells, self.link_g])[self._edge_ok]
        Gp = np.bincount(self._pair_inv, weights=G, minlength=len(self._pair_a))
        nf = self.n_free
        diag = np.zeros(nf)
        fa, fb = self.free_index[self._pair_a], self.free_index[self._pair_b]
        np.add.at(diag, fa[fa >= 0], Gp[fa >= 0])
        np.add.at(diag, fb[fb >= 0], Gp[fb >= 0])
        both = self._both
        vals = np.concatenate([-Gp[both], -Gp[both], diag])
        A = self._A.copy()
        A.data = vals[self._perm]
        x = self.solver.solve(A, self.rhs, self._x, self.progress, tol=tol)
        self._x = x
        V = np.zeros(self.n_nodes)
        free = self.free_index >= 0
        V[free] = x[self.free_index[free]]
        self.V = V
        self._G_cells = G_cells

    # ---- results --------------------------------------------------------------------
    def cell_power(self) -> np.ndarray:
        """Joule power (W) deposited in each copper cell node."""
        V = self.V
        la, lb = self.label[self.edge_a], self.label[self.edge_b]
        # each edge's loss G dV^2 split between its two cells; an edge into a merged pad node goes wholly to the
        # cell outside the pad (the pad itself is ideal)
        P = self._G_cells * (V[la] - V[lb]) ** 2
        sa, sb = self.is_super[la], self.is_super[lb]
        wa = np.where(sa & ~sb, 0.0, np.where(sb & ~sa, 1.0, 0.5))
        out = np.bincount(self.edge_a, weights=P * wa, minlength=self.n_cells)
        out += np.bincount(self.edge_b, weights=P * (1 - wa), minlength=self.n_cells)
        return out

    def link_power(self) -> list[tuple[str, float, float, np.ndarray, np.ndarray]]:
        """(name, current, power, nodes_a, nodes_b) per link."""
        out = []
        for (name, a, b), g in zip(self.link_info, self.link_g):
            dv = self.V[self.group_node[a]] - self.V[self.group_node[b]]
            out.append((name, g * dv, g * dv * dv, self.groups[a].nodes, self.groups[b].nodes))
        return out

    def summary(self) -> dict:
        """The DC results: terminal voltages, link currents and losses, copper and input power, the vias' largest
        segment current and loss (busiest first), and the path resistance when there is one source."""
        V = self.V
        terms = []
        for i, g in enumerate(self.groups):
            if g.kind in ("source", "sink"):
                terms.append({"name": g.name, "kind": g.kind, "current": g.current,
                              "voltage": float(V[self.group_node[i]]),
                              "pads": [p.key for p in g.pads]})
        links = [{"name": n, "current": float(abs(i)), "power": float(p)} for n, i, p, _, _ in self.link_power()]
        P_cu = float(self.cell_power().sum())
        P_links = sum(l["power"] for l in links)
        P_in = sum(t["current"] * t["voltage"] for t in terms if t["kind"] == "source")
        # Via currents
        la, lb = self.label[self.edge_a], self.label[self.edge_b]
        I_e = self._G_cells * (V[la] - V[lb])
        P_e = I_e * (V[la] - V[lb])
        seg = self.edge_seg
        m = seg >= 0
        I_seg = np.bincount(seg[m], weights=I_e[m], minlength=self.n_segments)
        P_seg = np.bincount(seg[m], weights=P_e[m], minlength=self.n_segments)
        vias = []
        for v in self.vias:
            if v.segments:
                vias.append({"label": v.label, "x": v.x, "y": v.y, "drill": v.drill, "net": v.net,
                             "current": float(np.max(np.abs(I_seg[v.segments]))),
                             "power": float(P_seg[v.segments].sum())})
        vias.sort(key=lambda r: -r["current"])
        src = [t for t in terms if t["kind"] == "source"]
        out = {"terminals": terms, "links": links, "vias": vias,
               "power_copper": P_cu, "power_links": P_links, "power_input": P_in,
               "cells": self.n_cells, "cell_um": self.dx * 1000}
        if len(src) == 1 and src[0]["current"]:
            out["resistance"] = src[0]["voltage"] / src[0]["current"]
        return out

    def maps(self) -> list[dict]:
        """Per-subgrid voltage, current density (A/mm²) and power density (W/mm²)."""
        V = self.V
        la, lb = self.label[self.edge_a], self.label[self.edge_b]
        I_e = self._G_cells * (V[la] - V[lb])
        n_in = self.n_inplane
        ea, eb = self.edge_a[:n_in], self.edge_b[:n_in]
        # a cell's current density: half of each edge current on its faces, per axis, over the cell's copper
        # cross-section (coverage at least 0.3, so sliver cells at an edge do not show spurious peaks)
        horiz = self.node_I[ea] == self.node_I[eb]
        Jx = np.zeros(self.n_cells)
        Jy = np.zeros(self.n_cells)
        np.add.at(Jx, ea[horiz], 0.5 * I_e[:n_in][horiz])
        np.add.at(Jx, eb[horiz], 0.5 * I_e[:n_in][horiz])
        np.add.at(Jy, ea[~horiz], 0.5 * I_e[:n_in][~horiz])
        np.add.at(Jy, eb[~horiz], 0.5 * I_e[:n_in][~horiz])
        area = self.t_cu[self.node_layer] * self.dx * 1e-3 * np.maximum(self.node_cov, 0.3)  # m²
        Jmag = np.hypot(Jx, Jy) / area * 1e-6  # A/mm²
        Jx_d, Jy_d = Jx / area * 1e-6, Jy / area * 1e-6
        P = self.cell_power() / (self.dx ** 2)  # W/mm²
        Vc = V[self.label]
        out = []
        for sg in self.subgrids:
            m = sg.node >= 0
            ids = sg.node[m]

            def fill(vals):
                """A per-node array as the subgrid's 2D map (NaN off copper)."""
                a = np.full(sg.node.shape, np.nan, np.float32)
                a[m] = vals[ids]
                return a

            out.append({"net": sg.net, "layer": sg.layer, "grid": sg.grid,
                        "V": fill(Vc), "J": fill(Jmag), "Jx": fill(Jx_d), "Jy": fill(Jy_d), "P": fill(P),
                        "cov": sg.cov})
        return out
