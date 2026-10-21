"""Tier 2: solve the return current of each configured net and estimate its emission.

Two plane models per route, because the two jobs need very different resolution:

* fine model (route region + margin, grid pitch ≈ the trace height, inductive): where the return current
  flows next to the trace. Gives loop inductance vs. an ideal reference, heatmaps, the vias/parts that
  carry the return between layers and nets, and the detour issues. Solved at f0 and at the worst harmonic.
* cavity model (the whole board, coarser grid, plane-pair capacitance from the stackup's εr, thickness and
  tanδ): how displacement current and plane resonances change the loop's magnetic moment across the band.
  Shared by every route on the board. Solved at the route's own harmonics: a few log-spaced ones first, then
  more where the moment changes quickly (resonances). Calibrated to the fine model at f0.
  Truncating planes to a route region would create artificial cavity edges, hence the whole board.

With `analysis.plane_capacitance: false` only the fine model runs, at the harmonics.

Reference frequency: loop inductance, the ideal comparison and the board-wide offset are taken at
f_ref = max(f0, 1 MHz) [A20]. Below about 1 MHz the return still follows resistance rather than inductance, so a
DC-DC converter switching at 10 kHz is judged where its edges radiate, not at its fundamental.

Parallel: solves at different frequencies, for different routes and in the final per-route step are independent,
so they run in a thread pool (`analysis.threads`). SciPy's sparse LU, the FFTs and BLAS release the GIL; on a
4-core laptop four board-wide solves ran 2.3x faster than one after another, with identical results.

Emission: harmonic current on the line (driver resistance, line Z0/εeff, receiver load) × loop moment per
ampere → far-field estimate → CISPR 32 margin. Source keys in square brackets: docs/REFERENCES.md.
"""
from __future__ import annotations

import gc
import math
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from dataclasses import dataclass, field
from typing import Callable, Optional

import numpy as np
from shapely.geometry import LineString, MultiLineString, Point, box
from shapely.ops import unary_union

from ..components.models import model_pads
from ..config.settings import Config, NetSpec
from ..core.board import Board, Pad
from ..core.issues import Issue, dedupe, resolve_severity, sort_issues
from ..emi.radiation import emission_arrays, emission_table, limit_range
from ..emi.path import (PathAmp, PathLine, PathLoad, PathPart, PathShunt, PathSource, amplified_path,
                        path_currents)
from ..emi.spectrum import analysis_band, microstrip, offset_stripline, trapezoid_harmonics
from ..geometry.copper import CopperMap, reference_layers
from ..geometry.shapes import arc_points
from ..rules.checks import CheckResult, run_checks
from ..solver.peec import (EPS0, gmres_restart, Branch, Grid, PlaneLayer, PlanePair, PlaneStack, RouteExcitation,
                           Solution, SolverError, TraceSegment, build_grid, thread_budget, track_impedance,
                           via_impedance)
from ..solver import gpu, peec
from .progress import Phases
from .pins import column_conductance, feed_layer, params_cache, segment_impedance, solder_span
from .route import SHUNT_KINDS, Route, RouteSegment, RouteVia, return_pad, shunt_parts, signal_nets  # noqa: F401
from .section import corridor_mm, route_section

MOUNT_L = 0.3e-9            # pad + escape inductance added to lumped parts (vias are modelled explicitly) [A21]
MAX_PART_BRANCHES = 8       # a lumped part is spread over its pads as up to this many parallel branches
INJECT_MAX_MM = 3.0         # return pin must be within this distance of its net's copper
INJECT_FALLBACK_MM = 5.0    # ...or of any plane copper
GRID_MIN_MM, GRID_MAX_MM = 0.05, 0.25
RESOLUTION_MIN_MM = 0.02           # the finest cell a higher resolution goes to
RESOLUTIONS = (1, 2, 4, 8, 16, 20, 25)   # the plugin's Resolution choices: x the nodes of every model
CAVITY_MIN_MM = 0.25        # the cavity model never goes finer than this
IDEAL_STITCH_MM = 2.0       # stitching pitch of the ideal reference planes
IDEAL_NET = "ideal"
F_SOLVE_MAX = 6e9           # highest harmonic solved (CISPR 32 ends at 6 GHz)
LIMIT_F_MIN = 30e6          # lowest frequency with a radiated limit
REFINE_DB = 3.0             # solve between two harmonics whose loop moments differ by more than this
REFINE_OCTAVE_DB = 3.0      # ...or (with plane capacitance) that are far apart: an octave counts as 3 dB
REFINE_MARGIN_DB = 20.0     # ...unless the emission there stays this far below the warning level
WORST_SOLVED_LOG = 0.05     # the worst harmonic gets its own solve unless one lies within ±5 %
QS_FRACTION = 0.25          # the board-wide model is quasi-static while the board is under this many wavelengths
                            # across [A47]: above, its emissions are indicative upper bounds (quasi_static_limit)
WORST_EXTRA = 3             # ... even past analysis.max_solves, at most this many more (VALIDATION_AUDIT.md round 6,
                            # ISSUE-32: with the budget spent, the reported worst was an interpolation)
SHUNT_MIN_SHARE = 0.01      # a shunt part on a signal path gets a loop of its own when it carries this share of the
                            # path's current at any harmonic (find_shunts, prepare_route)
GRID_SENSITIVE_F = 600e6    # a worst emission above this from a board-wide plane resonance (rise >= GRID_SENSITIVE_RISE
GRID_SENSITIVE_RISE_DB = 6.0  # dB over the low-frequency loop moment) within GRID_SENSITIVE_DB of the warning level
GRID_SENSITIVE_DB = 10.0    # gets a note: such peaks moved several dB between grids (grid_sensitive_note)
GRID_CHECK_F = 1e9          # above this, a peak's height stays uncertain by about GRID_ABOVE_DB from 4x on
GRID_ABOVE_DB = 2.0         # (refboard's worst margin at 1-1.5 GHz: 4x / 8x / 16x +14.1 / +17.6 / +15.8 dB)
RASTER_MM = 0.15            # copper is sampled at this pitch or finer, so thin webs between antipads conduct
F_REF_MIN = 1e6             # loop L, the ideal reference and the board-wide offset at max(f0, this): inductive regime
LINE_CUT_TRACKS = 3         # _line_of tries the cut on this many of a stretch's longest tracks before the closed forms
MAX_THREADS = 4           # "auto" thread count: physical cores, at most this (memory: ~300 MB per board-wide solve)
ALL_CORES_K = 4             # "Use all cores": at most this many frequencies per run and refinement round

Progress = Callable[[str, float], None]


@dataclass
class RouteResult:
    """One route's results: where its return current flows (maps per layer, the share each layer carries, the vias
    and parts carrying it between layers, the cross-section), its loop against an ideal layout, the emission
    estimate per harmonic and the worst margin, and notes. The window's views and the reports read these; a power
    stage's and a bridge's results extend it."""
    kind = "route"      # a class attribute, not in summary(): the result subclasses set their own
    net: str
    driver: str
    receiver: str
    reference_layer: str = ""                 # layer carrying the largest share of the plane current
    height_mm: float = 0.0                    # route to nearest reference plane
    plane_layers: list[str] = field(default_factory=list)
    grid: Optional[Grid] = None               # xy grid shared by all layers (first layer's mask)
    layer_grids: dict = field(default_factory=dict)       # layer -> Grid
    sheet_current: dict = field(default_factory=dict)     # layer -> |K| (A/m per A of signal)
    sheet_vectors: dict = field(default_factory=dict)     # layer -> (Kx, Ky) at the dominant phase (same units)
    layer_share: dict = field(default_factory=dict)       # layer -> fraction of plane current (incl. induced)
    transfers: list[dict] = field(default_factory=list)   # vias/parts/plane capacitance carrying the return
    section: dict = field(default_factory=dict)           # return per layer along the route (analysis/section.py)
    board_maps: dict = field(default_factory=dict)        # board-wide model at the worst harmonic (board_maps())
    map_frequency: float = 0.0
    solves: list[dict] = field(default_factory=list)      # fine model
    cavity_solves: list[dict] = field(default_factory=list)
    cavity: dict = field(default_factory=dict)            # pitch, nodes, layers, calibration
    ideal: dict = field(default_factory=dict)
    l_ratio: Optional[float] = None
    m_excess_db: Optional[float] = None
    resonance: Optional[dict] = None          # strongest rise of the loop moment over its f0 value
    unsolved: Optional[dict] = None           # {"from", "failed"} Hz: the band above "from" is held, not solved
    band: dict = field(default_factory=dict)  # the band evaluated: f_max (Hz, the top harmonic's limit), natural (the
                                              # edge's own), capped (analysis.max_frequency below it), and with the
                                              # board-wide model f_qs (quasi_static_limit) [A47]
    dc_connected: bool = True                # conductive return path between the return pins
    plane_capacitance: bool = False
    z0: float = 0.0
    eeff: float = 0.0
    series_ohms: float = 0.0
    path: dict = field(default_factory=dict)              # the signal path at the worst harmonic (path_summary)
    responses: list = field(default_factory=list)         # Bode / pole-zero responses (analysis/transfer.Response)
    emissions: list[dict] = field(default_factory=list)
    worst: Optional[dict] = None
    route_xy: list[tuple[str, list[tuple[float, float]]]] = field(default_factory=list)
    return_pins: tuple[str, str] = ("", "")
    notes: list[str] = field(default_factory=list)
    error: str = ""
    seconds: float = 0.0

    def summary(self) -> dict:
        """The result as plain data for the JSON report: the large arrays (maps, grids, cross-section) left out,
        the grid described by its origin, pitch and size."""
        skip = ("grid", "layer_grids", "sheet_current", "sheet_vectors", "section", "board_maps")
        d = {k: v for k, v in self.__dict__.items() if k not in skip}
        d["responses"] = [r.summary() for r in getattr(self, "responses", ())]
        if self.grid is not None:
            d["grid"] = {"x0": self.grid.x0, "y0": self.grid.y0, "d_mm": self.grid.d, "nx": self.grid.nx,
                         "ny": self.grid.ny}
        return d

    def main_layers(self, min_share: float = 0.02) -> list[str]:
        """Layers carrying a noticeable share of the plane current, biggest first."""
        return [l for l, s in sorted(self.layer_share.items(), key=lambda kv: -kv[1]) if s >= min_share]


@dataclass
class SimulationResult:
    """A whole Simulate: the rule checks, every route's, power stage's and bridge's result, the issues, and (for
    the fix search) the setups and models it solved; where the solves ran and in how many groups."""
    checks: CheckResult
    routes: list[RouteResult] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    setups: list = field(default_factory=list)       # RouteSetup per solved route (for fix suggestions)
    model: Optional["BoardModel"] = None
    converters: list = field(default_factory=list)          # ConverterResult per power stage
    converter_setups: list = field(default_factory=list)
    bridges: list = field(default_factory=list)             # BridgeResult per motor bridge (its legs are converters)
    dc_combined: object = None                              # every stage's DC paths together (power.combine_dc)
    dc_loads: list = field(default_factory=list)            # DcLoadResult per DC rail with loads (analysis/dcload.py)
    dc_load_setups: list = field(default_factory=list)
    pdn: list = field(default_factory=list)                 # PdnResult per supply rail checked (analysis/pdn.py)
    pdn_setups: list = field(default_factory=list)
    solver: dict = field(default_factory=dict)              # gpu.Run.summary(): device, GPU fallbacks
    notes: list[str] = field(default_factory=list)          # about the whole run (a stack's placement)
    groups: int = 1                         # model groups solved one after the other (solve_groups: memory)
    # a split Simulate's part (simulate(part=)): its issues by origin, not yet deduplicated/sorted, for merge_parts:
    # {"issues", "route_issues", "stage_issues", "checks": whether its result.checks are the whole job's}
    part: Optional[dict] = None

    def drop_factorisations(self):
        """Free the cached per-frequency factorisations (hundreds of MB on a big board) and the GPU copies but
        keep models, kernels and solutions, so the result can still seed fix suggestions; they factorise (and
        upload) again when needed."""
        drop_factorisations(self.setups + self.converter_setups + self.dc_load_setups + self.pdn_setups)

    def model_bytes(self) -> int:
        """About what the models and solutions kept for the fix search hold (bytes): their arrays and sparse
        matrices, each stack once. The maps and rows the window shows are not counted: they stay anyway."""
        return models_bytes(self.setups + self.converter_setups + self.dc_load_setups + self.pdn_setups)

    def drop_models(self):
        """Let the models and solutions go (only the routes, stages and issues stay): the fix search then simulates
        its own baseline. ~2.3 GB on refboard with every net and stage (tools/dev/mem_profile.py)."""
        self.setups, self.converter_setups, self.dc_load_setups, self.model = [], [], [], None
        self.pdn_setups = []


def _model_runs(s) -> list:
    """Every model run of a setup (route or stage): fine, board-wide, and the extra runs."""
    return [r for r in (s.fine, s.cavity, *getattr(s, "extra_runs", ())) if r is not None]   # extra: DC paths too


def _array_bytes(obj) -> int:
    """Bytes of the numpy arrays and scipy sparse matrices among an object's attributes (and in dicts of them)."""
    total = 0
    values = list(vars(obj).values()) if hasattr(obj, "__dict__") else []
    for v in values:
        for x in (v.values() if isinstance(v, dict) else (v,)):
            if isinstance(x, np.ndarray):
                total += x.nbytes
            elif hasattr(x, "data") and hasattr(x, "nnz") and isinstance(getattr(x, "data"), np.ndarray):
                total += sum(a.nbytes for a in (x.data, getattr(x, "indices", None), getattr(x, "indptr", None),
                                                getattr(x, "row", None), getattr(x, "col", None))
                             if isinstance(a, np.ndarray))
    return total


def models_bytes(setups: list) -> int:
    """The models' (stacks') and solutions' arrays of these setups, each stack once (SimulationResult.model_bytes)."""
    seen, total = set(), 0
    for s in setups:
        for run in _model_runs(s):
            for st in (run.exc.stack, run.exc.stack._base):
                if st is not None and id(st) not in seen:
                    seen.add(id(st))
                    total += _array_bytes(st)
            total += sum(_array_bytes(sol) for sol in run.sols.values() if sol is not None)
    return total


def drop_factorisations(setups: list):
    """The setups' models' cached factorisations and GPU copies, freed (see SimulationResult.drop_factorisations)."""
    for s in setups:
        for run in (s.fine, s.cavity, *getattr(s, "extra_runs", ())):
            if run is not None:
                for st in (run.exc.stack, run.exc.stack._base):
                    if st is not None:
                        st._ops.clear()
                        gpu.release(st)


# --------------------------------------------------------------------------- plane system of a region
class StackBuild:
    """Plane layers, grids, vias/parts and plane-pair capacitance of one region: shared by every route."""

    def __init__(self, names: list[str], grids: list[Grid], z_phys: dict[str, float], bbox, d: float,
                 t_phys: Optional[dict[str, float]] = None):
        """The layers `names` with their grids, heights (m, up = +) and copper thicknesses, the region bbox (mm)
        and the pitch d (mm); branches and plane pairs are added by the builder."""
        self.names = names
        self.grids = grids
        self.z = [z_phys[n] for n in names]
        self.t = [(t_phys or {}).get(n, 35e-6) for n in names]      # copper thickness per layer, m
        self.bbox = bbox
        self.d = d
        self.branches: list[Branch] = []
        self.pairs: list[PlanePair] = []
        self._plane: Optional[PlaneStack] = None
        # a through-hole pin whose solder does not reach its part's side: the layer its current enters the board on
        # (pins.feed_layer; power._pad_node takes it instead of the part's side)
        self.pin_feed: dict[str, str] = {}

    def index(self, layer: str) -> Optional[int]:
        """A layer's index in the model, None when the model does not have it."""
        return self.names.index(layer) if layer in self.names else None

    def node(self, layer: str, x: float, y: float, net: Optional[str], radius: float):
        """The (layer index, i, j) node of `net`'s copper nearest to (x, y) mm on `layer`, within radius; None."""
        k = self.index(layer)
        if k is None:
            return None
        n = self.grids[k].nearest_node(x, y, radius, net=net)
        return (k, *n) if n is not None else None

    def plane_layers(self) -> list[PlaneLayer]:
        """The layers as the solver's PlaneLayers."""
        return [PlaneLayer(n, z, g, t) for n, z, g, t in zip(self.names, self.z, self.grids, self.t)]

    @property
    def plane(self) -> PlaneStack:
        """The solver's PlaneStack of this model (made on first use, then shared by every route on it)."""
        if self._plane is None:
            # Plane-pair cavities need their sheet inductance in the preconditioner (resonances); the
            # inductive fine model converges best with the plain diagonal.
            self._plane = PlaneStack(self.plane_layers(), self.branches, self.pairs,
                                     precond="sheet" if self.pairs else "diag")
        return self._plane

    def xy(self, node) -> tuple[float, float]:
        """A node's board position (mm)."""
        x, y = self.grids[node[0]].xy(node[1], node[2])
        return float(x), float(y)

    @property
    def nodes(self) -> int:
        """The model's copper node count (its size, for progress and notes)."""
        return int(sum(g.node.sum() for g in self.grids))


class BoardModel:
    """Copper geometry plus cached plane systems of one board and configuration."""

    def __init__(self, board: Board, config: Config, copper: Optional[CopperMap] = None):
        """The board's model cache (copper map, layer heights in metres with up positive, thicknesses); models are
        built on demand and kept for every route and stage that needs the same one."""
        self.board = board
        self.config = config
        self.copper = copper or CopperMap(board)
        self.z_phys = {l.name: -l.z * 1e-3 for l in board.stackup.copper}   # metres, up = +
        self.t_phys = {l.name: (l.thickness or 0.035) * 1e-3 for l in board.stackup.copper}
        self._builds: dict[tuple, StackBuild] = {}
        self._ideals: dict[tuple, StackBuild] = {}
        self._lock = threading.RLock()      # geometry builds share cached shapely objects: one thread at a time

    @property
    def capacitance(self) -> bool:
        """Whether plane-pair capacitance (the board-wide model) is modelled."""
        return bool(self.config.analysis.plane_capacitance)

    def route_region(self, route: Route, pins: list[Pad]) -> tuple[float, float, float, float]:
        """The route model's region: the route and its pins' bounding box grown by analysis.margin_mm, clipped to
        the board outline (mm)."""
        pts = [p for s in route.segments for p in (s.start, s.end)] + [p.position for p in pins]
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        m = self.config.analysis.margin_mm
        outline = self.copper.outline
        ob = outline.bounds if not outline.is_empty else (min(xs) - m, min(ys) - m, max(xs) + m, max(ys) + m)
        return (max(min(xs) - m, ob[0]), max(min(ys) - m, ob[1]), min(max(xs) + m, ob[2]), min(max(ys) + m, ob[3]))

    def board_region(self) -> Optional[tuple[float, float, float, float]]:
        """The whole board's bounding box plus 0.5 mm (the board-wide model's region), None without an outline."""
        outline = self.copper.outline
        if outline.is_empty:
            return None
        x0, y0, x1, y1 = outline.bounds
        return (x0 - 0.5, y0 - 0.5, x1 + 0.5, y1 + 0.5)

    def pour_nets(self) -> set[str]:
        """Nets with filled zones (planes, and pours of power or switch nets)."""
        return {z.net for z in self.board.zones if z.net and not z.is_rule_area and z.is_filled}

    def plane_layers(self, region, extra: set[str], exclude_nets: set[str]) -> list[str]:
        """The layers with plane copper (zones not of `exclude_nets`) in the region, plus `extra`, in stackup
        order."""
        layers = set(extra)
        for layer in self.board.copper_layers:
            if any(net not in exclude_nets and g.intersects(region) for net, g in self.copper.planes(layer).items()):
                layers.add(layer)
        return [l for l in self.board.copper_layers if l in layers]

    def plane_nets(self, region, layers: list[str], exclude_nets: set[str]) -> set[str]:
        """The nets solved as copper: the reference nets and every net with a zone on these layers in the region,
        except `exclude_nets` (the signal itself)."""
        nets = set(self.config.reference_nets)
        for layer in layers:
            nets |= {n for n, g in self.copper.planes(layer).items() if n and n not in exclude_nets
                     and g.intersects(region)}
        return nets - exclude_nets

    def build(self, bbox, layers: list[str], plane_nets: set[str], d: float, capacitance: bool) -> StackBuild:
        """The model of this region, layers, nets and pitch: built once, then shared (routes over the same region
        reuse it, and its factorisations)."""
        key = (tuple(round(v, 3) for v in bbox), tuple(layers), frozenset(plane_nets), round(d, 6), capacitance)
        hit = self._builds.get(key)
        if hit is None:
            hit = self._builds[key] = self._make_build(bbox, layers, plane_nets, d, capacitance)
        return hit

    def converter_nets(self) -> tuple[set[str], set[str]]:
        """(nets, component layers) of the configured power stages: solved copper in the board-wide model too."""
        from .power import _side, converter_nets
        nets, layers = set(), set()
        for spec in self.config.stages():        # converters and motor bridges' legs
            nets |= converter_nets(self.board, self.config, spec)
            for ref in spec.parts:
                fp = self.board.footprint(ref)
                if fp is not None:
                    layers.add(_side(fp))
        return nets & set(self.board.nets), layers & set(self.board.copper_layers)

    def cavity_build(self, h_mm: float) -> Optional[StackBuild]:
        """The whole-board model with plane-pair capacitance, shared by all routes and power stages."""
        bbox = self.board_region()
        if bbox is None:
            return None
        signal = set()
        for spec in self.config.nets:          # nets being analysed are not planes ...
            if spec.name in self.board.nets:
                signal |= signal_nets(self.board, self.config, spec.name, spec.through)
        signal -= self.pour_nets()             # ... unless they have pours: those are solved copper
        power_nets, power_layers = self.converter_nets()
        signal -= power_nets                   # ... and converter nets are solved copper as well
        region = box(*bbox)
        layers = self.plane_layers(region, power_layers, signal)
        if not layers:
            return None
        nets = self.plane_nets(region, layers, signal) | power_nets
        a = self.config.analysis
        d = _choose_pitch(max(h_mm, CAVITY_MIN_MM), bbox, len(layers), a.cavity_max_nodes, "auto", a.resolution)
        return self.build(bbox, layers, nets, d, True)

    def _make_build(self, bbox, layers: list[str], plane_nets: set[str], d: float, capacitance: bool) -> StackBuild:
        """Build a model: each layer's copper of the plane nets rasterised (seeded at vias and pads so small
        rings connect, track centrelines forced so thin tracks conduct), vias and through-hole pads as vertical
        branches, tracks bridged across another net's cells, lumped parts (decaps, 0R, ferrites) between plane
        nets, and (with capacitance) every pair of layers' capacitance from the stackup."""
        board, config, copper = self.board, self.config, self.copper
        region = box(*bbox)
        rb = region.buffer(1.0)
        seeds: dict[str, dict[str, list]] = {l: {} for l in layers}
        vias = [v for v in board.vias if v.net in plane_nets and rb.contains(Point(v.position))]
        pads = [p for p in board.pads() if p.net in plane_nets and rb.contains(Point(p.position))]
        for item, ls in [(v, v.layers) for v in vias] + [(p, p.layers) for p in pads]:
            for l in ls:
                if l in seeds:
                    seeds[l].setdefault(item.net, []).append(item.position)
        # Track centrelines of plane nets: kept conductive even when narrower than the grid pitch
        lines: dict[str, dict[str, list]] = {l: {} for l in layers}
        for t in board.tracks:
            if t.net in plane_nets and t.layer in lines:
                pts = arc_points(t.start, t.mid, t.end) if t.is_arc else [t.start, t.end]
                lines[t.layer].setdefault(t.net, []).append((pts, t.width))
        names, grids = [], []
        # sample the copper at RASTER_MM or finer (2 x 2 to 6 x 6 per cell), so thin webs between antipads conduct
        # and every strip carries its copper's width (peec.Grid.wx): with one sample per cell (cells of RASTER_MM and
        # less, before 2026-10-18) the strips were full cells, and a 0.35 mm neck's island resonance read 793 MHz at
        # 0.15 mm cells against 769 with 2 x 2 (and 736-797 on the coarser grids, which had their sub-grid)
        sub = max(2, min(6, math.ceil(d / RASTER_MM - 1e-9)))
        for layer in layers:
            geoms = {}
            # the reference nets first: where two nets' copper share a node, the first one keeps it
            for net in sorted(plane_nets, key=lambda n: (n not in config.reference_nets, n)):
                g = copper.get(layer, net)
                if not g.is_empty and g.intersects(region):
                    geoms[net] = g.intersection(region.buffer(d))
            if geoms:
                names.append(layer)
                grids.append(build_grid(geoms, bbox, d, seeds[layer], lines[layer], sub))
        if not grids:
            raise SolverError("no plane copper near the route")
        st = StackBuild(names, grids, self.z_phys, bbox, d, self.t_phys)

        # Vertical branches: stitching vias and through-hole pads of plane nets
        snap = max(1.5 * d, 0.4)
        for v in vias:
            add_vertical(st, v.net, v.position, v.layers, v.drill,
                         f"via {v.net} @({v.position[0]:.2f}, {v.position[1]:.2f})", snap)
        pin_params = params_cache(board, config)
        for p in pads:
            if p.pad_type == "thru_hole" and len(p.layers) > 1:
                fp = board.footprint(p.footprint)
                filled = fp is not None and not fp.dnp
                column = None
                if filled:
                    # the part's pin values (analysis/pins.py: its GT field, its settings entry, the board's
                    # defaults, else built in); the built-in column is the copper-filled hole as before
                    pp = pin_params(fp.reference)
                    if not pp.plain:
                        side = "B.Cu" if fp.layer == "B.Cu" else "F.Cu"
                        column = pin_column(pp, p.drill or 0.8, side, self.z_phys)
                        feed = feed_layer(pp, side, [l for l in p.layers if l in st.names], self.z_phys)
                        if feed:
                            st.pin_feed[p.pin] = feed
                # joined all around its barrel (add_pin): a big connector pin is not a point source
                add_pin(st, p.net, p.position, p.layers, p.drill or 0.8, f"pad {p.pin}", snap,
                        filled=filled, column=column)
        # Tracks across another net's grid cells: the bridges that keep them joined (peec._rasterise_lines)
        for k, g in enumerate(grids):
            for i0, j0, i1, j1, length, width in g.bridges:
                x, y = g.xy(i0, j0)
                st.branches.append(Branch((k, i0, j0), (k, i1, j1),
                                          track_impedance(length * 1e-3, width * 1e-3, st.t[k]),
                                          f"track {g.nets[g.label[i0, j0]]} on {names[k]} @({x:.2f}, {y:.2f})",
                                          "track"))
        # Lumped parts between plane nets (decaps, 0R, ferrites)
        for fp in board.footprints:
            model = config.model_for(fp.reference)
            if model is None or model.kind == "open":
                continue
            fpads = [p for p in model_pads(fp, model) if p.net in plane_nets]   # a sensor IC: IP+ / IP- only
            if len({p.net for p in fpads}) != 2:
                continue
            pa = fpads[0]
            pb = next(p for p in fpads if p.net != pa.net)
            # spread over the whole area of every pad on each side (a FET's five drain pads, an inductor's wide
            # terminals): the current enters the copper there, not at one pad's centre, which crowded it into a
            # point (refboard's Q601: all of the input current at one spot of its 4 x 3 mm drain pad)
            pairs = part_pairs(st, [p for p in fpads if p.net == pa.net], [p for p in fpads if p.net == pb.net])
            if not pairs:                   # no grid node under the pads: the nearest ones, as before
                na = next((n for n in (st.node(l, *pa.position, pa.net, snap) for l in pa.layers) if n), None)
                nb = next((n for n in (st.node(l, *pb.position, pb.net, snap) for l in pb.layers) if n), None)
                pairs = [(na, nb)] if na and nb and na != nb else []
            z = part_impedance(model)
            n = len(pairs)                  # n branches of n·Z in parallel: the part's own Z
            for na, nb in pairs:
                st.branches.append(Branch(na, nb, z if n == 1 else (lambda f, z=z, n=n: n * z(f)), fp.reference,
                                          "part"))
        # Plane-pair capacitance from the physical stackup (εr, insulation thickness, loss tangent): the dielectrics
        # between two planes in series (Stackup.epsilon_series [Pozar 2012])
        if capacitance:
            stk = board.stackup
            for k in range(len(names)):
                for l in range(k + 1, len(names)):
                    a, b = names[k], names[l]
                    t = stk.dielectric_between(a, b) * 1e-3
                    st.pairs.append(PlanePair(k, l, EPS0 * stk.epsilon_series(a, b) / t,
                                              stk.loss_tangent_series(a, b)))
        return st

    def ideal_stack(self, route: Route, ref_layers: list[str], bbox, d) -> StackBuild:
        """The route's own reference planes, solid (outside a keep-out around traces on those layers) and
        stitched every IDEAL_STITCH_MM: what a careful layout of the same route could achieve."""
        with self._lock:          # called from parallel finish steps; the solve itself runs outside the lock
            return self._ideal_stack(route, ref_layers, bbox, d)

    def _ideal_stack(self, route: Route, ref_layers: list[str], bbox, d) -> StackBuild:
        """Build (or reuse) the ideal reference model (see ideal_stack); keyed by the region, layers, pitch and
        the route's geometry on those layers."""
        keep_segs = tuple((s.layer, tuple(np.round(s.start, 3)), tuple(np.round(s.end, 3)), round(s.width, 3))
                          for s in route.segments if s.layer in ref_layers and s.start != s.end)
        vias = tuple(tuple(np.round(v.position, 3)) for v in route.vias)
        key = (tuple(round(v, 3) for v in bbox), tuple(ref_layers), round(d, 6), keep_segs, vias)
        hit = self._ideals.get(key)
        if hit is not None:
            return hit
        outline = self.copper.outline
        region = outline.intersection(box(*bbox)) if not outline.is_empty else box(*bbox)
        grids = []
        for layer in ref_layers:
            segs = [s for s in route.segments if s.layer == layer and s.start != s.end]
            keep_out = unary_union([LineString([s.start, s.end]).buffer(max(0.3, 2 * s.width)) for s in segs]) \
                if segs else None
            geom = region.difference(keep_out) if keep_out is not None else region
            grids.append(build_grid({IDEAL_NET: geom}, bbox, d))
        st = StackBuild(list(ref_layers), grids, self.z_phys, bbox, d, self.t_phys)
        step = max(int(round(IDEAL_STITCH_MM / d)), 1)
        for k in range(len(grids) - 1):
            both = grids[k].node & grids[k + 1].node
            lattice = np.zeros_like(both)
            lattice[::step, ::step] = True
            zl = via_impedance(abs(st.z[k] - st.z[k + 1]), 0.3e-3)
            for i, j in np.argwhere(both & lattice):
                st.branches.append(Branch((k, int(i), int(j)), (k + 1, int(i), int(j)), zl, "ideal stitch", "via"))
            for v in route.vias:   # a good layout puts a return via right next to every signal via
                for dxy in ((1.0, 0.0), (-1.0, 0.0), (0.0, 1.0), (0.0, -1.0)):
                    na = st.node(ref_layers[k], v.position[0] + dxy[0], v.position[1] + dxy[1], IDEAL_NET, d)
                    nb = st.node(ref_layers[k + 1], v.position[0] + dxy[0], v.position[1] + dxy[1], IDEAL_NET, d)
                    if na and nb and na[1:] == nb[1:]:
                        st.branches.append(Branch(na, nb, zl, "ideal return via", "via"))
                        break
        self._ideals[key] = st
        return st


def pad_nodes(st: StackBuild, pads: list) -> list[tuple]:
    """The grid nodes of each pad's own net inside its outline, on its first layer in the model (an SMD pad's
    side; a through-hole pad's first); [] where none is (a pad smaller than a cell between the nodes)."""
    import shapely
    from ..geometry.shapes import pad_shape
    out = []
    for p in pads:
        layer = next((l for l in p.layers if l in st.names), None)
        poly = pad_shape(p)
        if layer is None or poly is None or not p.net:
            continue
        k = st.names.index(layer)
        g = st.grids[k]
        x0, y0, x1, y1 = poly.bounds
        i0, i1 = max(int((x0 - g.x0) / g.d) - 1, 0), min(int((x1 - g.x0) / g.d) + 2, g.nx)
        j0, j1 = max(int((y0 - g.y0) / g.d) - 1, 0), min(int((y1 - g.y0) / g.d) + 2, g.ny)
        if i0 >= i1 or j0 >= j1:
            continue
        ii, jj = np.meshgrid(np.arange(i0, i1), np.arange(j0, j1), indexing="ij")
        ii, jj = ii.ravel(), jj.ravel()
        xs, ys = g.xy(ii, jj)
        inside = shapely.contains_xy(poly, xs, ys) & g.node[ii, jj]
        if g.label is not None and g.nets:
            inside &= g.label[ii, jj] == (g.nets.index(p.net) if p.net in g.nets else -1)
        out += [(k, int(i), int(j)) for i, j in zip(ii[inside], jj[inside])]
    return list(dict.fromkeys(out))


def _spread(st: StackBuild, nodes: list, k: int) -> list:
    """k of the nodes spread over their area (farthest-point sampling, from the one nearest their centre)."""
    if len(nodes) <= k:
        return list(nodes)
    xy = np.array([st.xy(n) for n in nodes])
    pick = [int(np.argmin(np.hypot(*(xy - xy.mean(axis=0)).T)))]
    dist = np.hypot(*(xy - xy[pick[0]]).T)
    while len(pick) < k:
        nxt = int(np.argmax(dist))
        pick.append(nxt)
        dist = np.minimum(dist, np.hypot(*(xy - xy[nxt]).T))
    return [nodes[i] for i in pick]


def part_pairs(st: StackBuild, pads_a: list, pads_b: list) -> list[tuple]:
    """Node pairs for a lumped part spread over its pads: up to MAX_PART_BRANCHES nodes under the pads on each
    side, spread over their area, paired in order across the part (sorted along the line at right angles to the
    one joining the two sides' centres), so the branches run side by side like the current through a wide part.
    [] when either side has no node under its pads."""
    a, b = pad_nodes(st, pads_a), pad_nodes(st, pads_b)
    if not a or not b:
        return []
    k = min(MAX_PART_BRANCHES, len(a), len(b))
    a, b = _spread(st, a, k), _spread(st, b, k)
    ca = np.mean([st.xy(n) for n in a], axis=0)
    cb = np.mean([st.xy(n) for n in b], axis=0)
    axis = cb - ca
    perp = np.array([-axis[1], axis[0]]) if np.hypot(*axis) > 1e-9 else np.array([1.0, 0.0])
    a.sort(key=lambda n: float(np.dot(st.xy(n), perp)))
    b.sort(key=lambda n: float(np.dot(st.xy(n), perp)))
    return [(na, nb) for na, nb in zip(a, b) if na != nb]


def part_impedance(model) -> Callable[[float], complex]:
    """A part model as a branch impedance: its own impedance (an open part as 1 TΩ) plus the mounting inductance
    MOUNT_L of its pads and escapes."""
    return lambda f, mdl=model: (mdl.impedance(f) or complex(1e12)) + 2j * math.pi * f * MOUNT_L


def add_vertical(st: StackBuild, net: str, pos, span: list[str], drill_mm: float, name: str, snap: float,
                 filled: bool = False) -> int:
    """Chain a via/pad's connections between consecutive plane layers where it touches its own net (filled: a
    part's pin in the hole, see via_impedance)."""
    nodes = [n for n in (st.node(l, pos[0], pos[1], net, snap) for l in span if l in st.names) if n]
    added = 0
    for a, b in zip(nodes, nodes[1:]):
        length = abs(st.z[a[0]] - st.z[b[0]])
        st.branches.append(Branch(a, b, via_impedance(length, drill_mm * 1e-3, filled=filled), name, "via",
                                  drill_mm * 5e-4))
        added += 1
    return added


PIN_JOINTS_MAX = 48         # a through-hole pin's joints to each layer, around its barrel, at most (about 1.5 cells apart)


def pin_joints(drill_mm: float, d_mm: float) -> int:
    """How many joints a through-hole pin of `drill_mm` gets to each layer on a grid of pitch `d_mm`: one per ~1.5
    cells of its ring's circumference (radius drill/2 + d/2), 1 to PIN_JOINTS_MAX. A ring about one cell round (a
    small via) keeps one joint."""
    return max(1, min(PIN_JOINTS_MAX, int(round(math.pi * drill_mm / (1.5 * d_mm)))))


def pin_column(params, drill_mm: float, part_side: str, z_phys: dict) -> tuple:
    """What add_pin needs for a pin with its own values (analysis/pins.py): (soldered and unsoldered conductance
    per metre, the soldered height (z_low, z_high) in metres)."""
    t = z_phys.get("F.Cu", max(z_phys.values())) - z_phys.get("B.Cu", min(z_phys.values()))
    g_solder, g_open = column_conductance(params, drill_mm, t)
    return g_solder, g_open, solder_span(params, part_side, z_phys)


def _column_z(column: Optional[tuple], drill_mm: float, filled: bool, za: float, zb: float):
    """A pin's piece between heights za and zb as an impedance: the built-in via / copper-filled hole
    (via_impedance), or its column's soldered and unsoldered parts in series (pins.segment_impedance)."""
    length = abs(za - zb)
    if column is None:
        return via_impedance(length, drill_mm * 1e-3, filled=filled)
    g_solder, g_open, (lo, hi) = column
    soldered = max(0.0, min(max(za, zb), hi) - max(min(za, zb), lo))
    return segment_impedance(length, soldered, drill_mm, g_solder, g_open)


def add_pin(st: StackBuild, net: str, pos, span: list[str], drill_mm: float, name: str, snap: float,
            filled: bool = False, column: Optional[tuple] = None) -> int:
    """A through-hole pad's pin between consecutive layers, joined to each layer all around its barrel: `k`
    (pin_joints) parallel branches between points on a circle just outside the drill (its copper ring), each with k
    times the pin's impedance (together the pin's own). The user, of a big connector pin's map: "the current density
    around that area looks odd": joined at the one node nearest its centre (add_vertical), the whole pin current
    entered a layer at one cell on the ring's edge, a point source with a hot spot that is not there. A ring of about
    one cell (k = 1) keeps the single joint. `column` (pin_column): the pin's own values (material, solder fill)
    instead of a copper-filled hole."""
    k = pin_joints(drill_mm, st.d)
    if k <= 1:
        if column is None:
            return add_vertical(st, net, pos, span, drill_mm, name, snap, filled)
        nodes = [n for n in (st.node(l, pos[0], pos[1], net, snap) for l in span if l in st.names) if n]
        for a, b in zip(nodes, nodes[1:]):
            st.branches.append(Branch(a, b, _column_z(column, drill_mm, filled, st.z[a[0]], st.z[b[0]]), name,
                                      "via", drill_mm * 5e-4))
        return max(len(nodes) - 1, 0)
    r = drill_mm / 2 + 0.5 * st.d
    pts = [(pos[0] + r * math.cos(2 * math.pi * m / k), pos[1] + r * math.sin(2 * math.pi * m / k)) for m in range(k)]
    rows = []                                       # per layer the pin touches: its k joint nodes
    for layer in span:
        if layer not in st.names:
            continue
        centre = st.node(layer, pos[0], pos[1], net, snap)
        if centre is None:
            continue                                # no copper of this net here: the pin does not join this layer
        rows.append([st.node(layer, x, y, net, 0.75 * st.d) or centre for x, y in pts])
    added = 0
    for a, b in zip(rows, rows[1:]):
        z = _column_z(column, drill_mm, filled, st.z[a[0][0]], st.z[b[0][0]])
        for na, nb in zip(a, b):
            st.branches.append(Branch(na, nb, (lambda f, z=z, k=k: k * z(f)), name, "via", drill_mm * 5e-4))
            added += 1
    return added


def vertical_branches(st: StackBuild, net: str, pos, span: list[str], drill_mm: float, name: str) -> list[Branch]:
    """The branches add_vertical would create, without adding them (fix candidates)."""
    snap = max(1.5 * st.d, 0.4)
    nodes = [n for n in (st.node(l, pos[0], pos[1], net, snap) for l in span if l in st.names) if n]
    return [Branch(a, b, via_impedance(abs(st.z[a[0]] - st.z[b[0]]), drill_mm * 1e-3), name, "via", drill_mm * 5e-4)
            for a, b in zip(nodes, nodes[1:])]


# --------------------------------------------------------------------------- one route
@dataclass
class ModelRun:
    """One plane model of one route: its stack, the route's excitation on it, and the solutions."""
    build: StackBuild
    exc: RouteExcitation
    sols: dict = field(default_factory=dict)      # f -> Solution (None when the solve failed)
    planned: list[float] = field(default_factory=list)
    loops: list = field(default_factory=list)     # RouteExcitation per shunt loop of a signal path (find_shunts)
    loop_m: dict = field(default_factory=dict)    # f -> [loop moment per ampere (complex 3-vector) or None]

    def ok(self) -> dict:
        """The solutions that succeeded, by frequency."""
        return {f: s for f, s in self.sols.items() if s is not None}


@dataclass
class RouteSetup:
    """Everything needed to solve (and re-solve with modifications) one route."""
    spec: NetSpec
    route: Route
    res: RouteResult
    fine: ModelRun
    cavity: Optional[ModelRun]
    drv_ret: Pad
    rcv_ret: Pad
    route_refs: list[str]
    main_ref: str
    h_mm: float
    harmonics: np.ndarray
    issues: list[Issue] = field(default_factory=list)
    t_start: float = 0.0
    currents: Optional[np.ndarray] = None      # signal current per harmonic (A peak)
    path: Optional[tuple] = None               # (summary rows, emi.path.PathResult) of the signal path
    response_fn: Optional[Callable] = None     # f array -> v_receiver / v_source (complex) of the path's circuit
    amp_gains: list = field(default_factory=list)   # [(op-amp, f array -> its stage gain)]
    loop_weights: Optional[Callable] = None    # f array -> [complex weight per shunt loop ..., the receivers'
                                               # loop]: loop current / path mean current (band_moments)
    f_ref: float = 0.0                         # inductive reference frequency, max(f0, F_REF_MIN)

    # the fine model (kept as attributes for callers that only need one model)
    @property
    def build(self) -> StackBuild:
        """The fine model's build."""
        return self.fine.build

    @property
    def exc(self) -> RouteExcitation:
        """The route's excitation on the fine model."""
        return self.fine.exc

    @property
    def spectrum_run(self) -> ModelRun:
        """The model whose loop moment feeds the emission estimate."""
        return self.cavity if self.cavity is not None else self.fine

    def issue(self, rules, check, message, loc, severity=None, **kw):
        """Add an issue for this route (unless its check is switched off)."""
        sev = resolve_severity(check, rules.severity, severity)
        if sev != "off":
            self.issues.append(Issue(check, sev, self.spec.name, message, (round(loc[0], 4), round(loc[1], 4)), **kw))


def _route_references(board: Board, route: Route) -> tuple[list[str], str, float]:
    """(reference layers of the route in stackup order, the dominant one, its height in mm)."""
    weight: dict[str, float] = {}
    for seg in route.segments:
        for name, _ in reference_layers(board, seg.layer):
            weight[name] = weight.get(name, 0.0) + seg.length
    if not weight:
        return [], "", 0.0
    best = max(weight, key=weight.get)
    users = {s.layer for s in route.segments if best in [r[0] for r in reference_layers(board, s.layer)]}
    h = min(board.stackup.distance(best, l) for l in users)
    order = board.copper_layers
    return sorted(weight, key=order.index), best, h


def _choose_pitch(h_mm: float, bbox, n_layers: int, max_nodes: int, grid_mm="auto", resolution: float = 1.0
                  ) -> float:
    """The grid pitch (mm): the dielectric height (or grid_mm), coarser until the node budget holds. A
    resolution r > 1 starts at that pitch / sqrt(r) with r x the budget (where the budget does not bind, a larger
    budget alone would change nothing)."""
    d = float(grid_mm) if grid_mm != "auto" else min(max(h_mm, GRID_MIN_MM), GRID_MAX_MM)
    d = max(d / math.sqrt(max(resolution, 1.0)), RESOLUTION_MIN_MM)
    max_nodes = max_nodes * max(resolution, 1.0)
    w, hgt = bbox[2] - bbox[0], bbox[3] - bbox[1]
    while n_layers * (w / d + 1) * (hgt / d + 1) > max_nodes:
        d *= 1.1
    return d


def _line(board: Board, route: Route, ref_layer: str, cache=None) -> tuple[float, float]:
    """(Z0, εeff) of the route's main layer."""
    return _line_of(board, route.segments, ref_layer, cache)


def line_cache(model: "BoardModel"):
    """The board's cross-section cache (analysis/si.SectionCache) for the signal paths' lines, made once per model:
    equal cuts (a straight run) are solved once for every route."""
    with model._lock:
        if getattr(model, "_line_cache", None) is None:
            from .si import SectionCache
            model._line_cache = SectionCache(model.board, model.copper)
        return model._line_cache


def _line_of(board: Board, segments: list, ref_layer: str, cache=None) -> tuple[float, float]:
    """(Z0, εeff) of a stretch of route: its longest layer, the median track width there.

    With `cache` (line_cache: what Run checks' signal-integrity checks solve): the 2D field solver on the board's own
    cut across the stretch's longest track, at its middle (the copper of every net in the cut, the stackup's
    dielectrics and solder mask; within 0.2 % of exact, tests/test_xsection.py), else a quarter from either end, else
    the same on the next longest tracks (LINE_CUT_TRACKS). Where no clean cut exists at any of them (the track
    widens into a pad or a pour there, no reference copper in the cut) and without a cache, the closed forms
    below. (Before 2026-10-18 the closed forms always: ~10 % high on very asymmetric inner layers, a few per cent on
    outer layers for the solder mask they leave out; VALIDATION_AUDIT.md ISSUE-5.)"""
    lengths: dict[str, float] = {}
    for s in segments:
        lengths[s.layer] = lengths.get(s.layer, 0.0) + s.length
    layer = max(lengths, key=lengths.get) if lengths else ref_layer
    widths = [s.width for s in segments if s.layer == layer and s.kind == "track"] or [0.2]
    w = float(np.median(widths))
    if cache is not None:
        tracks = [s for s in segments if s.layer == layer and s.kind == "track" and s.length > 0 and s.net]
        # the longest tracks first, each at its middle, then a quarter from either end: a cut at one place can fail
        # (a pad, a via or a pour's edge in it) where one a little further along is clean. On refboard,
        # Net-(U301--) (1.8 mm of track) had no cut at its longest track's middle and 43.8 ohm at three quarters
        # (2026-10-18; before, the closed forms took over at the first failed cut)
        for s in sorted(tracks, key=lambda x: -x.length)[:LINE_CUT_TRACKS]:
            (ax, ay), (bx, by) = s.start, s.end
            n = math.hypot(bx - ax, by - ay)
            for t in (0.5, 0.25, 0.75):
                got = cache.solve_at((ax + t * (bx - ax), ay + t * (by - ay)), ((bx - ax) / n, (by - ay) / n), layer,
                                     [s.net], s.width)
                if got is not None and got[0].z0() > 0:
                    return float(got[0].z0()), float(got[0].eeff())
    st = board.stackup
    neigh = st.neighbours(layer)
    t = st.layer(layer).thickness if st.layer(layer) else 0.035
    # the closed forms want the dielectric heights from the strip's faces to the planes (Stackup.dielectric_between),
    # not the foils' centre-to-centre distance (35 µm more on a 0.1 mm core: Z0 ~10 % high), and an inner layer is
    # rarely centred between its neighbours (a thin prepreg on one side, a core on the other): an offset stripline.
    # Before 2026-10-17: the centred IPC-2141 stripline on the centre-to-centre spacing (+30-40 % for a strip at a
    # fifth of the spacing, negative Z0 for wide strips) and the microstrip on the centre-to-centre height
    if len(neigh) == 2:
        h1, h2 = (st.dielectric_between(layer, n) for n in neigh)
        return offset_stripline(w, h1, h2, st.epsilon_between(neigh[0], neigh[1]), t)
    ref = neigh[0] if neigh else ref_layer
    return microstrip(w, st.dielectric_between(layer, ref), st.epsilon_between(layer, ref), t)


def _line_loss(board: Board, segments: list, ref_layer: str) -> dict:
    """A stretch's loss parameters for emi/path.PathLine: its longest layer's median track width and foil
    thickness, and the dielectric between that layer and its neighbouring copper (thickness-weighted εr and tan δ,
    Stackup.epsilon_between / loss_tangent_between), as _line_of picks the layer."""
    lengths: dict[str, float] = {}
    for s in segments:
        lengths[s.layer] = lengths.get(s.layer, 0.0) + s.length
    layer = max(lengths, key=lengths.get) if lengths else ref_layer
    st = board.stackup
    cu = st.layer(layer) if layer else None
    widths = [s.width for s in segments if s.layer == layer and s.kind == "track"]
    if cu is None or not widths:
        return {}
    neigh = st.neighbours(layer) or ([ref_layer] if ref_layer and ref_layer != layer else [])
    if not neigh:
        return {}
    er = sum(st.epsilon_between(layer, n) for n in neigh) / len(neigh)
    tan_d = sum(st.loss_tangent_between(layer, n) for n in neigh) / len(neigh)
    hs = tuple(st.dielectric_between(layer, n) * 1e-3 for n in neigh[:2])
    return {"width_m": float(np.median(widths)) * 1e-3, "thickness_m": cu.thickness * 1e-3, "er": er,
            "tan_d": tan_d, "planes_h_m": hs}


def find_shunts(model: "BoardModel", route: Route) -> list[dict]:
    """Parts from the signal path to a return: a 2-net passive part (resistor, capacitor, inductor, ferrite, 0R)
    with one pin on a net of the path and the other on a reference net or a poured net (a rail), not one of the
    path's series parts. Each with its place along the path: the nearest point of the route's segments to its
    path pin ({"ref", "model", "path_pad", "ret_pad", "seg", "t", "at"}: segment index, fraction, point)."""
    board, config = model.board, model.config
    returns = set(config.reference_nets) | model.pour_nets()
    segs = route.segments
    out = []
    for fp, m, pin, ret in shunt_parts(board, config, route, returns):     # which parts: route.shunt_parts
        best = None
        for k, sg in enumerate(segs):
            if sg.kind == "part":
                continue
            (ax, ay), (bx, by) = sg.start, sg.end
            dx, dy = bx - ax, by - ay
            ll = dx * dx + dy * dy
            t = 0.0 if ll == 0 else min(max(((pin.position[0] - ax) * dx + (pin.position[1] - ay) * dy) / ll, 0.0), 1.0)
            pt = (ax + t * dx, ay + t * dy)
            dist = math.dist(pt, pin.position) + (0.0 if sg.net == pin.net else 1e3)   # its own net first
            if best is None or dist < best[0]:
                best = (dist, k, t, pt)
        if best is not None:
            out.append({"ref": fp.reference, "model": m, "path_pad": pin, "ret_pad": ret, "seg": best[1],
                        "t": best[2], "at": best[3]})
    out.sort(key=lambda x: (x["seg"], x["t"]))
    return out


def shunt_route(route: Route, shunt: dict) -> Route:
    """The route of a shunt's loop: along the signal path up to the shunt's place, to its path pin, through the
    part to its return pin (the loop's "receiver")."""
    seg = route.segments[shunt["seg"]]
    j = next(i for i, x in enumerate(route.steps) if x is seg)
    steps = list(route.steps[:j])
    steps.append(RouteSegment(seg.start, shunt["at"], seg.layer, seg.width, seg.kind, seg.uuid, seg.line, seg.net))
    pp, rp = shunt["path_pad"], shunt["ret_pad"]
    steps.append(RouteSegment(shunt["at"], pp.position, seg.layer, 0.2, "track", "", 0, pp.net))
    steps.append(RouteSegment(pp.position, rp.position, pp.layers[0], 0.3, "part", "", 0, ""))
    return Route(route.net, route.driver, rp, steps, set(route.nets), list(route.parts))


def path_elements(board: Board, config: Config, route: Route, ref_layer: str,
                  shunts: Optional[list[dict]] = None, cache=None) -> tuple[list, list[dict]]:
    """The route as emi/path.py elements: a line per stretch between series parts (its own Z0 and εeff, from the
    board's cut when `cache`, line_cache, is given: _line_of), each
    part a series impedance (its component model; none: a short), each shunt (find_shunts) at its place. Also the
    summary rows for the result."""
    elements, rows, stretch = [], [], []
    parts = iter(route.parts)
    by_seg: dict[int, list] = {}
    for sh in shunts or []:
        by_seg.setdefault(sh["seg"], []).append(sh)

    def close():
        """End the current stretch of track: one line element with its own Z0 and eps_eff (from its layer and
        median width) and its length, and its summary row."""
        if stretch:
            z0, eeff = _line_of(board, stretch, ref_layer, cache)
            length = sum(s.length for s in stretch)
            loss = _line_loss(board, stretch, ref_layer)
            elements.append(PathLine(z0, eeff, length * 1e-3, **loss))
            rows.append({"kind": "line", "length_mm": length, "z0": z0, "eeff": eeff,
                         "nets": sorted({s.net for s in stretch if s.net}),
                         **({"width_mm": loss["width_m"] * 1e3, "thickness_mm": loss["thickness_m"] * 1e3,
                             "er": loss["er"], "tan_d": loss["tan_d"],
                             "planes_h_mm": [h * 1e3 for h in loss["planes_h_m"]]} if loss else {})})
            stretch.clear()
    for i, seg in enumerate(route.segments):
        if seg.kind != "part":
            start = seg.start
            for sh in sorted(by_seg.get(i, []), key=lambda x: x["t"]):
                stretch.append(RouteSegment(start, sh["at"], seg.layer, seg.width, seg.kind, net=seg.net))
                close()
                elements.append(PathShunt(sh["ref"], sh["model"].impedance))
                rows.append({"kind": "shunt", "ref": sh["ref"], "model": sh["model"].kind, "to": sh["ret_pad"].net,
                             "length_mm": 0.0})
                start = sh["at"]
            stretch.append(seg if start == seg.start else
                           RouteSegment(start, seg.end, seg.layer, seg.width, seg.kind, net=seg.net))
            continue
        close()
        ref = next(parts, "?")
        model = config.model_for(ref)
        z = model.impedance if model is not None else (lambda f: 1e-3)
        elements.append(PathPart(ref, z, seg.length * 1e-3))
        rows.append({"kind": "part", "ref": ref, "model": model.kind if model is not None else "none",
                     "length_mm": seg.length})
    close()
    return elements, rows


def _shunt_loops(model: "BoardModel", setup: RouteSetup, shunts: list[dict], elements: list, source, load, pr,
                 n_drv, cavity: Optional["ModelRun"]):
    """A loop of its own (on the fine and the board-wide model) for each shunt carrying SHUNT_MIN_SHARE of the
    path's current at some harmonic; the others' currents go to the next loop. Sets setup.loop_weights."""
    res, route = setup.res, setup.route
    scale = float(np.max(np.abs(pr.i_in))) or 1.0
    keep = []
    for k, sh in enumerate(shunts):
        if float(np.max(np.abs(pr.shunt_i[sh["ref"]]))) < SHUNT_MIN_SHARE * scale:      # the part's own current
            continue
        r = shunt_route(route, sh)
        try:
            built = []
            for run in (setup.fine, cavity):
                if run is None:
                    continue
                st = run.build
                n0 = n_drv if run is setup.fine else _inject(model, st, setup.drv_ret)
                n1 = _inject(model, st, sh["ret_pad"])
                if n0 is None or n1 is None:
                    raise SolverError(f"{sh['ret_pad'].pin} is not near any plane copper")
                chain, sections = make_chain_with_pours(r, st, n0, n1, model.z_phys)
                built.append((run, RouteExcitation(st.plane, chain, n0, n1, injections=_pour_injections(st, sections))))
        except SolverError as e:
            res.notes.append(f"shunt {sh['ref']} on the path left out of the loops ({e}): its current is counted "
                             "with the next loop")
            continue
        for run, exc in built:
            run.loops.append(exc)
        keep.append(k)
    if not keep:
        return
    kept = set(keep)

    def weights(fs: np.ndarray) -> list:
        """Each kept loop's current per ampere of the path's mean current at frequencies fs (complex): what
        band_moments weights the loops' moments with. A shunt too small for its own loop passes its current on
        to the next kept loop (carry)."""
        q = path_currents(fs, np.ones(len(fs)), elements, source, load)
        lc = q.loop_currents()
        out, carry = [], 0
        for k in range(len(shunts)):
            if k in kept:
                out.append(lc[k] + carry)
                carry = 0
            else:
                carry = carry + lc[k]
        out.append(lc[-1] + carry)
        mean = np.where(np.abs(q.i_mean) > 0, q.i_mean, 1.0)
        return [x / mean for x in out]
    setup.loop_weights = weights
    res.notes.append(f"{len(keep)} part(s) from the path to a return carry part of its current: "
                     f"{', '.join(shunts[k]['ref'] for k in keep)}, each a loop of its own (signal path)")



def _stage_routes(route: Route, config: Config) -> Optional[list[list]]:
    """The route's steps cut at its op-amp stages: [(steps, the amp's ref and model ending the stage, or None, None
    for the last, the series parts inside the stage)], or None without an amplifier on the route. Part segments
    pair with route.parts in order; each stage gets its own parts (all of the route's but the op-amp, as before,
    paired the stage after an op-amp's R303 with the op-amp U301: a 4.7 kΩ resistor modelled as the op-amp)."""
    parts = iter(route.parts)
    cuts, owner = [], {}                    # owner: step index -> the part of that part segment
    for i, step in enumerate(route.steps):
        if isinstance(step, RouteSegment) and step.kind == "part":
            ref = next(parts, None)
            owner[i] = ref
            m = config.model_for(ref) if ref else None
            if m is not None and m.kind == "amplifier":
                cuts.append((i, ref, m))
    if not cuts:
        return None
    out, start = [], 0
    for i, ref, m in cuts + [(len(route.steps), None, None)]:
        inside = [owner[k] for k in range(start, i) if k in owner]
        out.append((list(route.steps[start:i]), ref, m, inside))
        start = i + 1
    return out


def _amp_circuit(model: "BoardModel", route: Route, main_ref: str, notes: list) -> Optional[tuple]:
    """The circuit of a signal path through op-amp stages (part model `amplifier`), without plane models: (stages
    [Route per stage], amps [PathAmp], their ground pins [Pad], elements per stage, shunts per stage, report rows).
    None when the route has no op-amp, or one whose input, output or ground pin can't be told (a note says so: the
    path is then one stage). The path is cut at each op-amp: the driver (or the op-amp before) drives the stage, its
    receivers' end is the next op-amp's input (loaded by its input impedance) or the receivers."""
    from ..components.models import amplifier_pins
    board, config = model.board, model.config
    raw = _stage_routes(route, config)
    if raw is None:
        return None
    stages, pamps, rows, grounds = [], [], [], []
    drv = route.driver
    for steps, ref, m, inside in raw:
        if ref is None:
            rcv = route.receiver
        else:
            fp = board.footprint(ref)
            pads = amplifier_pins(fp, m) if fp is not None else None
            ret = return_pad(board, config, pads[0], None) if pads else None
            if pads is None or ret is None:
                notes.append(f"op-amp {ref}: its input, output or ground pin can't be told: the path is treated as "
                             "one stage")
                return None
            rcv = pads[0]
        nets = {sg.net for sg in steps if isinstance(sg, RouteSegment) and sg.net} | {drv.net, rcv.net}
        sr = Route(route.net, drv, rcv, steps, nets, inside)
        stages.append(sr)
        if ref is not None:
            grounds.append(ret)
            pamps.append(PathAmp(ref, m.amp_gain, m.amp_z_in, m.amp_z_out_at, m.params.get("slew")))
            drv = pads[1]
    # the circuit of each stage, its shunts, its rows for the report
    els, shunt_sets = [], []
    for k, sr in enumerate(stages):
        sh = find_shunts(model, sr)
        e, r = path_elements(board, config, sr, main_ref, sh, line_cache(model))
        els.append(e)
        shunt_sets.append(sh)
        rows += r
        if k < len(pamps):
            rows.append({"kind": "amp", "ref": pamps[k].name, "model": "amplifier", "length_mm": 0.0})
    return stages, pamps, grounds, els, shunt_sets, rows


def _amp_response(els: list, pamps: list, source, load) -> tuple[Callable, list]:
    """(response_fn, amp_gains) of an op-amp path's circuit: v_receiver / v_source at any frequencies (the amps
    without their slew limit: a small-signal sweep), and each amp's own gain."""
    linear = [PathAmp(a.name, a.gain, a.z_in, a.z_out) for a in pamps]
    response = lambda fs: amplified_path(fs, np.ones(len(fs)), els, linear, source, load)[-1].v_end   # noqa: E731
    gains = [(a.name, lambda fs, g=a.gain: np.array([complex(g(float(x))) for x in fs])) for a in pamps]
    return response, gains


def _amplified_route(model: "BoardModel", setup: RouteSetup, freqs, amps, source, load, cavity,
                     main_ref: str) -> bool:
    """A signal path through op-amp stages (_amp_circuit): False when the route has none. Each stage's current
    returns to the pin its source returns to: the driver's return pin, or the op-amp's ground pin (its nearest
    reference-net pin). So every stage is a current loop of its own (its shunts too), solved on the fine and
    board-wide models; the main loop is the last stage's (the op-amp to the receivers: the maps, loop inductance and
    ideal comparison refer to it). Loop weights are per volt of the source, and setup.currents the source's
    harmonics (× a slew cap)."""
    res = setup.res
    got = _amp_circuit(model, setup.route, main_ref, res.notes)
    if got is None:
        return False
    stages, pamps, grounds, els, shunt_sets, rows = got
    returns = [setup.drv_ret, *grounds, setup.rcv_ret]
    results = amplified_path(freqs, amps, els, pamps, source, load)
    unclamped = amplified_path(freqs, amps, els, [PathAmp(a.name, a.gain, a.z_in, a.z_out) for a in pamps],
                               source, load) if any(a.slew for a in pamps) else results
    clamp = np.where(np.abs(unclamped[-1].v_end) > 0,
                     np.abs(results[-1].v_end) / np.maximum(np.abs(unclamped[-1].v_end), 1e-300), 1.0)
    # the loops: every stage's kept shunts, then the stage itself; the last stage's own loop is the main one
    loops, keep_sets = [], []
    for k, sr in enumerate(stages):
        q = results[k]
        scale = float(np.max(np.abs(q.i_in))) or 1.0
        kept = []
        for j, sh in enumerate(shunt_sets[k]):
            if float(np.max(np.abs(q.shunt_i[sh["ref"]]))) >= SHUNT_MIN_SHARE * scale:
                loops.append((shunt_route(sr, sh), returns[k], sh["ret_pad"]))
                kept.append(j)
        keep_sets.append(set(kept))
        loops.append((sr, returns[k], returns[k + 1]))
    built = {id(setup.fine): [], id(cavity): []}
    for r, ret0, ret1 in loops:
        for run in (setup.fine, cavity):
            if run is None:
                continue
            st = run.build
            n0, n1 = _inject(model, st, ret0), _inject(model, st, ret1)
            if n0 is None or n1 is None:
                res.error = f"the op-amp path's return pin {(ret1 if n0 else ret0).pin} is not near any plane copper"
                return True
            try:
                chain, sections = make_chain_with_pours(r, st, n0, n1, model.z_phys)
                built[id(run)].append(RouteExcitation(st.plane, chain, n0, n1, injections=_pour_injections(st, sections)))
            except SolverError as e:
                res.error = f"op-amp path: {e}"
                return True
    for run in (setup.fine, cavity):
        if run is not None:
            *others, main = built[id(run)]
            run.exc, run.loops = main, others
    setup.route, setup.drv_ret = stages[-1], returns[-2]

    def weights(fs: np.ndarray) -> list:
        """Each loop's current at frequencies fs, per volt of the source (stage by stage: the shunt loops kept,
        then the stage's terminal loop), for band_moments."""
        qs = amplified_path(fs, np.ones(len(fs)), els, [PathAmp(a.name, a.gain, a.z_in, a.z_out) for a in pamps],
                            source, load)
        out = []
        for k, q in enumerate(qs):
            lc, carry = q.loop_currents(), 0
            for j in range(len(shunt_sets[k])):
                if j in keep_sets[k]:
                    out.append(lc[j] + carry)
                    carry = 0
                else:
                    carry = carry + lc[j]
            out.append(lc[-1] + carry)
        return out
    setup.loop_weights = weights
    setup.currents = np.abs(np.asarray(amps, float)) * clamp
    merged = _merge_results(results, pamps)
    setup.path = (rows, merged)
    setup.response_fn, setup.amp_gains = _amp_response(els, pamps, source, load)
    res.notes.append(f"signal path through op-amp(s) {', '.join(a.name for a in pamps)}: {len(stages)} stages, "
                     f"{len(loops)} current loop(s); the maps and loop inductance show the last stage's loop "
                     "(the op-amp to the receivers)")
    return True


def _merge_results(results: list, pamps: list):
    """One PathResult-like summary of an op-amp path's stages (for path_summary): the parts' |Z| and shunt currents
    of every stage, each amp's |gain|, the current into the first stage, the last stage's mean and end voltage."""
    from types import SimpleNamespace
    part_z, shunt_i = {}, {}
    for q in results:
        part_z.update(q.part_z)
        shunt_i.update(q.shunt_i)
    for a in pamps:
        part_z[a.name] = np.abs(np.array([complex(a.gain(float(fk))) for fk in results[0].freqs]))
    return SimpleNamespace(freqs=results[0].freqs, part_z=part_z, shunt_i=shunt_i, i_in=results[0].i_in,
                           i_mean=results[-1].i_mean, v_end=results[-1].v_end)


PATH_SWEEP = 300               # frequencies of a signal path's Bode sweep (unless the net sets sweep_points)


def sweep_range(spec: NetSpec, harmonics) -> np.ndarray:
    """The Bode sweep's frequencies: log-spaced from the net's sweep_from to its sweep_to with sweep_points; each
    unset one as before: a hundredth of the first harmonic (at least 1 Hz), the top of the harmonics' band (at
    least ten times the first), PATH_SWEEP points. A from above the to (one set, the other the default) is moved a
    decade below it."""
    fs = np.asarray(harmonics, float)
    lo = getattr(spec, "sweep_from", None) or max(fs.min() / 100, 1.0)
    hi = getattr(spec, "sweep_to", None) or max(fs.max(), fs.min() * 10)
    if lo >= hi:
        lo = hi / 10 if getattr(spec, "sweep_to", None) else lo
        hi = max(hi, lo * 10)
    return np.geomspace(lo, hi, getattr(spec, "sweep_points", None) or PATH_SWEEP)


POLE_SPAN = 10.0               # poles and zeros up to this many times the sweep's top are listed and written out
DOUBLET = 1e-3                 # a pole and a zero this close (relative) cancel in the lists and the written-out H(s)


def cancel_doublets(poles, zeros) -> tuple[list, list, int]:
    """Poles and zeros without the pairs closer than DOUBLET (relative): a pole-zero doublet changes |H| by less
    than that fraction anywhere, but writing it out doubles the transfer function's length (refboard's TLV6001 output:
    451 Hz twice, 0.02 % apart: the R303 / C304 load its closed loop hardly sees). (poles, zeros, pairs removed)."""
    p, z, n = list(poles), list(zeros), 0
    for zi in list(z):
        best = min(p, key=lambda pi: abs(pi - zi), default=None)
        if best is not None and abs(best - zi) <= DOUBLET * max(abs(best), abs(zi), 1e-12):
            p.remove(best)
            z.remove(zi)
            n += 1
    return p, z, n


def network_responses(board: Board, config: Config, spec: NetSpec, receiver: str, f: np.ndarray,
                      cache=None) -> tuple[list, list[str]]:
    """The path's responses from its whole circuit (analysis/analog_net: every op-amp and part on its nets, any
    topology; docs/ANALOG_AC.md): the receiver per volt of the source, swept with every track a line; its poles and
    zeros exact, from the circuit with each track as its π section (emi/mna.Circuit.poles_zeros), up to POLE_SPAN
    times the sweep's top; the numeric normal form (transfer.normal_form) and the ideal circuit's H(s) in its part
    names (analysis/symbolic.py); each op-amp's output as a response of its own. (responses, notes)."""
    from .analog_net import analog_network
    from .symbolic import symbolic_transfer
    from .transfer import Response, normal_form
    net = analog_network(board, config, spec, receiver, cache=cache)
    lumped = analog_network(board, config, spec, receiver, lumped=True, f_top=float(f[-1]), cache=cache)
    f_max = POLE_SPAN * float(f[-1])
    keep = lambda roots: [r for r in roots if r == 0 or abs(r) / (2 * math.pi) <= f_max]     # noqa: E731
    outs = [(f"{spec.name}: receiver / source", net.out_node, lumped.out_node)]
    for name, _kind in net.opamps:              # each op-amp channel's output (not one on AC ground)
        node, lnode = net.amp_out.get(name), lumped.amp_out.get(name)
        if node in net.circuit.nodes and lnode in lumped.circuit.nodes and node != net.out_node:
            outs.append((f"{name}: output", node, lnode))
    out = []
    for title, node, lnode in outs:
        h = net.circuit.transfer(f, node)
        poles, zeros, k = lumped.circuit.poles_zeros(lnode)
        poles, zeros, n_doublets = cancel_doublets(poles, zeros)
        r = Response(title, spec.name, "voltage gain", np.asarray(f, float), h, keep(poles), keep(zeros), exact=True)
        r.normal = normal_form(poles, zeros, k, f_max=f_max)
        if n_doublets:
            r.notes.append(f"{n_doublets} pole-zero pair(s) within {DOUBLET:.1%} of each other cancelled (doublets: "
                           "a load the output hardly sees)")
        out.append(r)
    sym = symbolic_transfer(net.schematic)
    if sym is not None and out:
        out[0].symbolic = {"text": sym.text, "latex": sym.latex, "mathml": sym.mathml}
    # each op-amp's loop gain and phase margin (emi/mna.Circuit.loop_gain): a loop that rings or oscillates shows
    from .transfer import PM_WARN, fmt_hz, phase_margin
    for name in net.circuit.opamps:
        t = net.circuit.loop_gain(name, f)
        r = Response(f"{name}: loop gain", spec.name, "loop gain", np.asarray(f, float), t)
        pm = phase_margin(f, t)
        r.margin = pm
        if pm is None:
            r.notes.append("its loop gain does not fall through 1 in the sweep (sweep higher for the phase margin)")
        else:
            r.notes.append(f"crossover {fmt_hz(pm['f_c'])}, phase margin {pm['pm']:.0f}°"
                           + (" (it rings: below 45°)" if pm["pm"] < PM_WARN else ""))
        out.append(r)
    notes = list(dict.fromkeys(net.notes + lumped.notes))
    kinds = ", ".join(f"{n} ({k})" for n, k in net.opamps)
    notes.insert(0, f"circuit: {len(net.nets)} net(s), {len(net.parts)} part(s), {net.tracks} track segment(s)"
                 + (f", op-amps {kinds}" if kinds else ""))
    return out, notes


def path_responses(setup, net: str, model: Optional["BoardModel"] = None) -> list:
    """The signal path's Bode responses (analysis/transfer.py) over the net's sweep (sweep_range: by default a
    hundredth of its first harmonic to the top of its band). With the board's `model`: from the path's whole circuit
    (network_responses: any op-amp topology, exact poles and zeros, the transfer function written out); without
    one, or when that circuit can't be made (a note says why): the path model's v_receiver / v_source (source, lines,
    series parts, shunts, op-amp stages as gain blocks) with poles and zeros by vector fitting, each op-amp stage's
    gain too."""
    from .transfer import fitted_response
    f = sweep_range(setup.spec, setup.harmonics)
    if model is not None:
        try:
            out, notes = network_responses(model.board, model.config, setup.spec, setup.res.receiver, f,
                                           line_cache(model))
            setup.res.notes += [f"Bode: {n}" for n in notes]
            if out:
                return out
        except (ValueError, SolverError, KeyError, ZeroDivisionError, np.linalg.LinAlgError) as e:
            setup.res.notes.append(f"Bode: the path's circuit could not be solved ({e}): the path model instead")
    out = []
    try:
        out.append(fitted_response(f"{net}: receiver / source", net, "voltage gain", f, setup.response_fn(f)))
    except (ValueError, ZeroDivisionError, FloatingPointError) as e:
        setup.res.notes.append(f"Bode: the path's response could not be computed ({e})")
    for name, gain in setup.amp_gains:
        out.append(fitted_response(f"{name}: stage gain", net, "voltage gain", f, gain(f)))
    return out


def path_summary(rows: list[dict], pr, f: float) -> dict:
    """The signal path for the result at harmonic f: its stretches and series parts (|Z| there), the current into
    the path and its mean along it, the voltage at the receivers (peak, per harmonic amplitude)."""
    j = int(np.argmin(np.abs(pr.freqs - f)))
    out = []
    for r in rows:
        r = dict(r)
        if r["kind"] in ("part", "shunt") and r["ref"] in pr.part_z:
            r["z_ohm"] = float(pr.part_z[r["ref"]][j])
        if r["kind"] == "amp" and r["ref"] in pr.part_z:
            r["gain"] = float(pr.part_z[r["ref"]][j])
        if r["kind"] == "shunt" and r["ref"] in pr.shunt_i:
            r["i_ma"] = float(abs(pr.shunt_i[r["ref"]][j])) * 1e3
        out.append(r)
    return {"f": float(pr.freqs[j]), "elements": out, "i_in_ma": float(abs(pr.i_in[j])) * 1e3,
            "i_mean_ma": float(abs(pr.i_mean[j])) * 1e3, "v_end_v": float(abs(pr.v_end[j]))}


def _route_xy(route: Route) -> list:
    """The route as polylines per layer [(layer, points)] for the maps (consecutive segments joined)."""
    out: list = []
    for s in route.steps:
        if isinstance(s, RouteSegment):
            if out and out[-1][0] == s.layer and out[-1][1][-1] == s.start:
                out[-1][1].append(s.end)
            else:
                out.append((s.layer, [s.start, s.end]))
    return out


def _inject(model: BoardModel, st: StackBuild, pad: Pad, notes: Optional[list] = None):
    """Grid node where the return current enters the planes at a return pin."""
    d = st.d
    r = max(1.5 * d, max(pad.size) / 2 + d)
    for l in pad.layers:
        n = st.node(l, *pad.position, pad.net, r)
        if n:
            return n
    by_z = sorted(st.names, key=lambda n: abs(model.z_phys[n] - model.z_phys[pad.layers[0]]))
    for l in by_z:
        n = st.node(l, *pad.position, pad.net, INJECT_MAX_MM)
        if n:
            if notes is not None:
                notes.append(f"{pad.pin}: return enters {pad.net} copper on {l} (no {pad.net} copper at the pin)")
            return n
    for l in by_z:
        n = st.node(l, *pad.position, None, INJECT_FALLBACK_MM)
        if n:
            if notes is not None:
                notes.append(f"{pad.pin}: no {pad.net} copper within {INJECT_MAX_MM} mm; return assumed to enter "
                             f"the {st.grids[n[0]].net_at(n[1:])} plane on {l}")
            return n
    return None


def _signal_harmonics(config: Config, spec: NetSpec) -> tuple[np.ndarray, np.ndarray, tuple]:
    """The signal's trapezoid harmonics up to the top of its analysis band: (frequencies, amplitudes, the band)."""
    edge = min(spec.rise_time, spec.fall_time or spec.rise_time)
    top, band = band_top(config, analysis_band(spec.freq, edge))
    freqs, amps = trapezoid_harmonics(spec.freq, spec.rise_time, spec.amplitude_v, spec.duty, top, spec.fall_time)
    return freqs, amps, band


def _source_load(config: Config, spec: NetSpec) -> tuple[PathSource, PathLoad]:
    """The path's Thevenin source (R + jωL, an output capacitance) and its receivers' load (their capacitance in
    parallel, a termination), each from the net's settings or the analysis defaults."""
    a = config.analysis
    source = PathSource(spec.driver_ohms if spec.driver_ohms is not None else a.driver_ohms,
                        (spec.driver_nh or 0.0) * 1e-9, (spec.driver_pf or 0.0) * 1e-12)
    load = PathLoad((spec.load_pf if spec.load_pf is not None else a.load_pf) * 1e-12 * max(len(spec.receivers), 1),
                    spec.load_ohms)
    return source, load


def bode_paths(board: Board, config: Config, nets: Optional[list[str]] = None,
               progress: Optional[Progress] = None) -> list[RouteResult]:
    """The signal paths' Bode sweeps alone (Signal paths tab "Bode", `groundtruth bode`): per path and receiver
    its circuit (source, lines with their cross-sections' Z0, series parts, shunts, op-amp stages, the receivers'
    load: what Simulate solves the path with) swept over the net's sweep range and fitted for poles and zeros
    (path_responses), without the plane models or the field solve: seconds, not minutes. A RouteResult per path
    and receiver with `responses` (bode_views shows them), or `error` when its route or circuit can't be made.
    nets: these paths only (switched off ones too, when named); else every enabled path. `progress` is called
    between paths (the window's raises to cancel)."""
    from types import SimpleNamespace
    from .route import RouteError, find_route
    active = config.active(nets)
    specs = [s for s in active.nets if not nets or s.name in nets]
    model = BoardModel(board, active)
    jobs = [(s, rx) for s in specs for rx in s.receivers]
    out = []
    for k, (spec, rx) in enumerate(jobs):
        if progress is not None:
            progress(f"Bode: {spec.name} → {rx}", k / max(len(jobs), 1))
        res = RouteResult(spec.name, spec.driver, rx)
        out.append(res)
        try:
            route = find_route(board, active, spec.name, spec.driver, rx, spec.through)
        except RouteError as e:
            res.error = f"no route: {e}"
            continue
        _, main_ref, _ = _route_references(board, route)
        if not main_ref:
            res.error = "no reference layer next to the route (the lines' impedance needs one)"
            continue
        source, load = _source_load(active, spec)
        notes: list[str] = []
        got = _amp_circuit(model, route, main_ref, notes)
        if got is not None:
            _, pamps, _, els, _, _ = got
            response_fn, amp_gains = _amp_response(els, pamps, source, load)
        else:
            elements, _ = path_elements(board, active, route, main_ref, find_shunts(model, route), line_cache(model))
            response_fn = lambda fs, e=elements: path_currents(fs, np.ones(len(fs)), e, source, load).v_end   # noqa
            amp_gains = []
        res.notes += notes
        freqs, _, _ = _signal_harmonics(active, spec)
        sweep = SimpleNamespace(spec=spec, harmonics=freqs, response_fn=response_fn, amp_gains=amp_gains, res=res)
        res.responses = path_responses(sweep, spec.name, model)
    if progress is not None:
        progress("Bode: done", 1.0)
    return out


def prepare_route(model: BoardModel, spec: NetSpec, route: Route) -> tuple[Optional[RouteSetup], RouteResult,
                                                                              list[Issue]]:
    """Build (or reuse) the plane models for a route and its excitations. Returns (setup or None, result, issues)."""
    t_start = time.time()
    board, config = model.board, model.config
    a = config.analysis
    res = RouteResult(spec.name, spec.driver, route.receiver.pin, plane_capacitance=model.capacitance)
    res.route_xy = _route_xy(route)
    issues: list[Issue] = []

    def issue(check, message, loc, severity=None, **kw):
        """Record an issue of this route (unless its check is switched off)."""
        sev = resolve_severity(check, config.rules.severity, severity)
        if sev != "off":
            issues.append(Issue(check, sev, spec.name, message, (round(loc[0], 4), round(loc[1], 4)), **kw))

    route_refs, main_ref, h_mm = _route_references(board, route)
    if not main_ref:
        res.error = "no reference layer next to the route"
        return None, res, issues
    res.reference_layer, res.height_mm = main_ref, h_mm

    drv_ret = return_pad(board, config, route.driver, spec.driver_return)
    rcv_ret = return_pad(board, config, route.receiver, spec.receiver_return)
    if drv_ret is None or rcv_ret is None:
        missing = spec.driver if drv_ret is None else route.receiver.pin
        res.error = f"no {'/'.join(config.reference_nets)} pin found near {missing}"
        issue("return_path_broken", res.error + "; set driver_return/receiver_return in the settings",
              (route.driver if drv_ret is None else route.receiver).position, key=f"noret:{missing}")
        return None, res, issues
    res.return_pins = (drv_ret.pin, rcv_ret.pin)

    # Fine model: route region, inductive. Where the route runs through a pour of its own net, that copper
    # is solved too (the current spreads through it) instead of being a line.
    bbox = model.route_region(route, [drv_ret, rcv_ret])
    region = box(*bbox)
    pours = {seg.net for seg in route.segments if seg.kind == "zone"}
    signal = set(route.nets) - pours
    pour_layers = {seg.layer for seg in route.segments if seg.kind == "zone"}
    if a.planes == "nearest":
        layers = [l for l in model.board.copper_layers if l == main_ref or l in pour_layers]
    else:
        layers = model.plane_layers(region, set(route_refs) | set(drv_ret.layers[:1]) | set(rcv_ret.layers[:1])
                                    | pour_layers, signal)
    plane_nets = model.plane_nets(region, layers, signal) | pours
    d = _choose_pitch(h_mm, bbox, len(layers), a.max_nodes, a.grid_mm, a.resolution)
    try:
        st = model.build(bbox, layers, plane_nets, d, False)
    except SolverError:
        res.error = "no plane copper near the route"
        issue("return_path_broken", "no plane copper on any layer near the route", route.driver.position,
              key=f"noplane:{route.receiver.pin}")
        return None, res, issues
    res.plane_layers = st.names
    res.grid = st.grids[0]
    res.layer_grids = dict(zip(st.names, st.grids))

    n_drv, n_rcv = _inject(model, st, drv_ret, res.notes), _inject(model, st, rcv_ret, res.notes)
    if n_drv is None or n_rcv is None:
        pad = drv_ret if n_drv is None else rcv_ret
        res.error = f"return pin {pad.pin} is not near any plane copper"
        issue("return_path_broken", res.error, pad.position, key=f"inject:{pad.pin}")
        return None, res, issues
    try:
        chain, sections = make_chain_with_pours(route, st, n_drv, n_rcv, model.z_phys)
        exc = RouteExcitation(st.plane, chain, n_drv, n_rcv, injections=_pour_injections(st, sections))
    except SolverError:
        res.error = "return pins not connected"
        issue("return_path_broken",
              f"{route.driver.pin}→{route.receiver.pin}: no copper connection between the return pins "
              f"{drv_ret.pin} and {rcv_ret.pin} on {', '.join(st.names)} (split plane without via or bridging part)",
              route.driver.position, key=f"split:{route.driver.pin}:{route.receiver.pin}")
        return None, res, issues

    # Cavity model: whole board with plane capacitance (shared by all routes)
    cavity = None
    if model.capacitance:
        cst = model.cavity_build(h_mm)
        if cst is not None:
            cd, cr = _inject(model, cst, drv_ret), _inject(model, cst, rcv_ret)
            if cd is not None and cr is not None:
                try:
                    cchain, csections = make_chain_with_pours(route, cst, cd, cr, model.z_phys)
                    cexc = RouteExcitation(cst.plane, cchain, cd, cr, injections=_pour_injections(cst, csections))
                    cavity = ModelRun(cst, cexc)
                    res.cavity = {"d_mm": cst.d, "nodes": cst.nodes, "layers": cst.names}
                except SolverError as e:
                    res.notes.append(f"cavity model: {e}")
        if cavity is None:
            res.notes.append("plane capacitance not modelled for this route (no board outline or plane copper)")
    res.dc_connected = exc.dc_connected

    f0 = spec.freq
    freqs, amps, res.band = _signal_harmonics(config, spec)
    setup = RouteSetup(spec, route, res, ModelRun(st, exc), cavity, drv_ret, rcv_ret, route_refs, main_ref, h_mm,
                       freqs, issues=issues, t_start=t_start)
    # Current on the line per harmonic: driver resistance (+ series parts), line Z0/εeff, receiver load
    res.z0, res.eeff = _line(board, route, main_ref, line_cache(model))
    for ref in route.parts:
        mdl = config.model_for(ref)
        if mdl is not None and mdl.kind == "resistor":
            res.series_ohms += mdl.get("R")
    if spec.current_a is not None:
        # current_a is the line's peak current at the fundamental (README, settings): the driver's harmonics scaled
        # so harmonic 1 carries it. (Before 2026-10-17 they were scaled by current_a / amplitude_v, which gave the
        # fundamental c_1/A·current_a: 2/π of it for a fast 50 % square wave, 3.9 dB low.)
        k1 = int(np.argmin(np.abs(freqs - f0)))
        setup.currents = amps / amps[k1] * spec.current_a
    else:
        # the path: a Thevenin source, a line per stretch between series parts, each part's impedance at its
        # place, the receivers' load (emi/path.py). Before, one line with the resistors' R added to the source
        source, load = _source_load(config, spec)
        if _amplified_route(model, setup, freqs, amps, source, load, cavity, main_ref):
            if res.error:
                issue("return_path_broken", res.error, route.driver.position, key=f"amp:{route.receiver.pin}")
                return None, res, issues
        else:
            shunts = find_shunts(model, route)
            elements, rows = path_elements(board, config, route, main_ref, shunts, line_cache(model))
            pr = path_currents(freqs, amps, elements, source, load)
            setup.currents = np.abs(pr.i_mean)
            setup.path = (rows, pr)
            setup.response_fn = lambda fs: path_currents(fs, np.ones(len(fs)), elements, source, load).v_end
            if shunts:
                _shunt_loops(model, setup, shunts, elements, source, load, pr, n_drv, cavity)
    setup.f_ref = float(max(f0, F_REF_MIN))
    spectral = initial_frequencies(setup, a.solve_points, limit_range(a.limit_spec()))
    if cavity is not None:
        setup.fine.planned = [setup.f_ref]
        cavity.planned = spectral
    else:
        setup.fine.planned = spectral
    return setup, res, issues


def initial_frequencies(setup: RouteSetup, points: int, band: tuple = (LIMIT_F_MIN, F_SOLVE_MAX)) -> list[float]:
    """The reference frequency plus log-spaced harmonics across the radiated-limit band (`band`: the active
    limit's, emi/radiation.limit_range; solving at real harmonics means no interpolation error where the emission is
    evaluated)."""
    f_ref = setup.f_ref or setup.spec.freq
    H = setup.harmonics
    cand = H[(H >= band[0]) & (H <= min(band[1], F_SOLVE_MAX)) & (np.abs(H - f_ref) > 1e-6 * f_ref)]
    picks = {float(f_ref)}
    n = max(points - 1, 1)
    if len(cand):
        idx = np.unique(np.round(np.geomspace(1, len(cand), min(n, len(cand)))).astype(int) - 1)
        picks |= {float(cand[i]) for i in idx}
    return sorted(picks)


def next_frequencies(setup: RouteSetup, run: ModelRun, max_solves: int, config: Optional[Config] = None,
                     k: int = 1, gaps_only: bool = False) -> list[float]:
    """Up to k harmonics to solve next (from different gaps), or [] when the band is resolved well enough.

    Every gap between solved neighbours gets an uncertainty: the change of the loop moment across it or,
    with plane capacitance (resonances possible), REFINE_OCTAVE_DB per octave of width, whichever is larger;
    a narrow resonance can sit inside a wide gap whose ends look alike. Gaps below REFINE_DB count as
    resolved. Among the rest, the gaps whose estimated emission comes closest to the limit are split at their
    log-midpoint harmonic; gaps that stay more than REFINE_MARGIN_DB below the warning level are left alone
    (resolving a resonance far below the limit changes nothing). Once no gap qualifies, the harmonic with the
    worst estimated margin gets a solve of its own (with plane capacitance), so the reported worst case is not
    an interpolation across a possible resonance. k > 1 lets parallel workers refine several gaps at once.

    The gaps share the budget max_solves; the worst harmonic's own solve does not stop at it, up to WORST_EXTRA
    more. It did: with the budget spent the function returned before that rule, and the reported worst could be an
    interpolation across a resonance (refboard's SPI_SCK at 1x: +13.2 dB at 1220 MHz from the solves at
    1020 and 1580 MHz, where solving every harmonic gives +8.4 dB: ISSUE-32). finish_route says when it still is.
    gaps_only: the gaps' midpoints only, never the worst's own solve (unresolved_gaps_note asks what the gaps want)."""
    k = min(k, max_solves - len(run.sols))
    if k <= 0 and len(run.sols) >= max_solves + WORST_EXTRA:
        return []
    fs, ms = band_moments(setup) if run is setup.spectrum_run else (sorted(run.ok()), None)
    if ms is None:
        ms = np.array([_m(run.ok()[f]) for f in fs])
    if len(fs) < 2:
        return []
    weigh = config is not None and setup.currents is not None
    floor = (config.rules.emi_margin_db + REFINE_MARGIN_DB) if weigh else None
    octave_db = REFINE_OCTAVE_DB if setup.cavity is not None and run is setup.cavity else 0.0
    H_all = setup.harmonics
    margins = _margin_curve(setup, config, fs, ms) if weigh else None    # per harmonic, NaN without a limit
    tried = np.round(np.array(sorted(run.sols)), 3)
    solvable = (H_all <= F_SOLVE_MAX) & ~np.isin(np.round(H_all, 3), tried)
    gaps = []
    for g in range(len(fs) - 1 if k > 0 else 0):        # the gaps only within the budget
        fa, fb = fs[g], fs[g + 1]
        lo, hi = np.searchsorted(H_all, fa * 1.0001), np.searchsorted(H_all, fb * 0.9999)
        inside = np.arange(lo, hi)[solvable[lo:hi]]
        if not len(inside) or ms[g] <= 0 or ms[g + 1] <= 0:
            continue
        unc = max(abs(20 * math.log10(ms[g + 1] / ms[g])), octave_db * math.log2(fb / fa))
        if unc < REFINE_DB:
            continue
        score = unc
        if weigh:
            seg = margins[np.searchsorted(H_all, fa):np.searchsorted(H_all, fb, side="right")]
            seg = seg[np.isfinite(seg)]
            if not len(seg) or seg.min() > floor:
                continue
            score = unc - float(seg.min())       # closeness to the limit first, uncertainty breaks ties
        between = H_all[inside]
        mid = between[int(np.argmin(np.abs(np.log(between) - 0.5 * (math.log(fa) + math.log(fb)))))]
        gaps.append((score, float(mid)))
    # A failed solve leaves the band between it and the solved frequency below it to interpolation, or above the
    # last solved one to a held value: close in on it from below first
    nearest: dict[float, float] = {}                 # solved frequency -> the first failure above it
    for ff in sorted(f for f, s in run.sols.items() if s is None and k > 0):
        fa = max((f for f in fs if f < ff), default=None)
        if fa is not None:
            nearest.setdefault(fa, ff)
    for fa, ff in nearest.items():
        lo, hi = np.searchsorted(H_all, fa * 1.0001), np.searchsorted(H_all, ff * 0.9999)
        inside = np.arange(lo, hi)[solvable[lo:hi]]
        if len(inside):
            between = H_all[inside]
            mid = between[int(np.argmin(np.abs(np.log(between) - 0.5 * (math.log(fa) + math.log(ff)))))]
            gaps.append((math.inf, float(mid)))
    if gaps:
        return list(dict.fromkeys(f for _, f in sorted(gaps, reverse=True)))[:k]
    if weigh and octave_db and not gaps_only:     # a purely inductive model is smooth: interpolating it is accurate
        inner = (H_all > fs[0]) & (H_all < fs[-1]) & solvable & np.isfinite(margins)
        if inner.any():
            j = np.nonzero(inner)[0][int(np.argmin(margins[inner]))]
            f = float(H_all[j])
            if margins[j] <= floor and min(abs(math.log(f / x)) for x in fs) > WORST_SOLVED_LOG:
                return [f]
    return []


def solve_at(setup: RouteSetup, run: ModelRun, f: float) -> Optional[Solution]:
    """Solve one model run at f and keep the solution (None, with a note, when it fails); a signal path's shunt
    loops on the same model are solved at f too (their moments only)."""
    try:
        sol = run.exc.solve(f)
    except SolverError as e:
        setup.res.notes.append(str(e))
        run.sols[f] = None
        return None
    run.sols[f] = sol
    if run.loops:       # a signal path's shunt loops: the same model and frequency (its factorisation), moments only
        ms = []
        for exc in run.loops:
            try:
                ms.append(exc.solve(f).m)
            except SolverError as e:
                setup.res.notes.append(f"a shunt loop at {f / 1e6:.4g} MHz: {e}")
                ms.append(None)
        run.loop_m[f] = ms
    return sol


def _m(sol: Solution) -> float:
    """|m|: the magnitude of a solution's dipole moment (A m^2 per A)."""
    return float(np.linalg.norm(sol.m))


def finish_route(model: BoardModel, setup: RouteSetup) -> RouteResult:
    """Ideal reference, emissions, layer shares, transfers and Tier 2 issues from the solved frequencies."""
    config = model.config
    rules, a = config.rules, config.analysis
    res, spec, route = setup.res, setup.spec, setup.route
    fine = setup.fine.ok()
    spec_run = setup.spectrum_run
    spectral = spec_run.ok()
    if not fine or not spectral:
        res.error = "solver did not converge"
        return res
    res.solves = [_solve_row(f, s) for f, s in sorted(fine.items())]
    if setup.cavity is not None:
        res.cavity_solves = [_solve_row(f, s) for f, s in sorted(spectral.items())]
    f_ref = setup.f_ref or spec.freq
    s0 = fine.get(f_ref) or fine[min(fine)]

    # Ideal reference at f0 (inductive regime), on the fine grid
    try:
        ist = model.ideal_stack(route, setup.route_refs, setup.fine.build.bbox, setup.fine.build.d)

        def ideal_node(pad: Pad):
            """The ideal plane's node under a pad: the ideal layer nearest the pad's layer in z that has a node
            there."""
            for l in sorted(ist.names, key=lambda n: abs(model.z_phys[n] - model.z_phys[pad.layers[0]])):
                n = ist.node(l, *pad.position, IDEAL_NET, INJECT_FALLBACK_MM)
                if n:
                    return n
            raise SolverError(f"no ideal plane near {pad.pin}")

        nd, nr = ideal_node(setup.drv_ret), ideal_node(setup.rcv_ret)
        ideal = RouteExcitation(ist.plane, make_chain(route, ist, nd, nr, model.z_phys), nd, nr).solve(s0.f)
        res.ideal = {"f": s0.f, "L_nH": ideal.L_loop * 1e9, "m_mm2": _m(ideal) * 1e6, "layers": setup.route_refs}
        res.l_ratio = s0.L_loop / ideal.L_loop if ideal.L_loop > 0 else None
        # Don't credit near-perfect cancellation in the ideal (e.g. F.Cu/B.Cu halves of a route): compare
        # with at least the moment of a plain loop of the route's length at its reference height.
        m_ref = max(_m(ideal), route.length * setup.h_mm * 1e-6)
        res.m_excess_db = 20 * math.log10(_m(s0) / m_ref) if m_ref > 0 else None
    except SolverError as e:
        res.notes.append(f"ideal reference failed: {e}")

    # Loop moment per ampere across the band: the fine model's loop plus what plane capacitance adds
    fs, ms = band_moments(setup)
    if setup.cavity is not None:
        for row, m in zip(res.cavity_solves, ms):
            row["m_used_mm2"] = float(m) * 1e6        # what the emission estimate uses
        c0 = spectral.get(f_ref) or spectral[min(spectral)]
        bias = 20 * math.log10(_m(c0) / _m(s0)) if _m(s0) > 0 and _m(c0) > 0 else 0.0
        res.cavity["bias_db"] = bias
        if abs(bias) > 10:
            res.notes.append(f"the board-wide grid ({setup.cavity.build.d:.2f} mm) cannot resolve this route's "
                             f"return ({bias:+.0f} dB at {f_ref / 1e6:.4g} MHz); only its plane-capacitance "
                             "contribution is used")

    # Emission: current on the line per harmonic × loop moment per ampere
    freqs = setup.harmonics
    m_curve = moment_curve(setup, freqs, fs, ms)
    currents = setup.currents
    res.emissions = emission_table(freqs, currents, m_curve, a.limit_spec(), a.distance_m, a.ground_reflection)
    compared = [r for r in res.emissions if r["margin_db"] is not None]
    mark_quasi_static(res, model.board)
    res.notes += band_notes(res)
    if compared:
        res.worst = min(compared, key=lambda r: r["margin_db"])
        res.unsolved = unsolved_band(setup.spectrum_run, max(r["f"] for r in compared))
    if setup.path is not None:
        res.path = path_summary(*setup.path, res.worst["f"] if res.worst else float(freqs[0]))
    if setup.response_fn is not None:
        res.responses = path_responses(setup, res.net, model)

    # Plane resonance: strongest rise of the loop moment over its low-frequency value
    if setup.cavity is not None and len(fs) > 1:
        k = int(np.argmax(ms))
        rise = 20 * math.log10(ms[k] / ms[0]) if ms[0] > 0 else 0.0
        if rise > 1.0:
            res.resonance = {"f": fs[k], "rise_db": rise, "pair": _top_plane_pair(spec_run.build, spectral[fs[k]])}
        if res.worst is not None:
            j = int(np.argmin(np.abs(freqs - res.worst["f"])))
            note = grid_sensitive_note(res.worst, float(m_curve[j]), float(ms[0]), rules.emi_margin_db,
                                       a.resolution)
            if note:
                res.notes.append(note)
            note = interpolated_worst_note(res.worst, fs, rules.emi_margin_db)
            if note:
                res.notes.append(note)
            note = unresolved_gaps_note(setup, config)
            if note:
                res.notes.append(note)

    # Maps and transfers from the fine model at the solved frequency nearest the worst harmonic. With the
    # board-wide model on, the fine model is only solved at f0: it is inductive, so its current pattern
    # hardly changes above a few MHz, and the frequency-dependent part comes from the board-wide model.
    target = res.worst["f"] if res.worst else max(fine)
    rep = fine[min(fine, key=lambda f: abs(math.log(f / target)))]
    res.map_frequency = rep.f
    # return statistics count the return copper only: a pour the route runs through carries the signal itself
    # (its current density still shows in the heatmap)
    own = set(route.nets)
    share = rep.layer_share(_return_masks(setup.fine.build, own))
    names = setup.fine.build.names
    res.layer_share = {n: float(s) for n, s in zip(names, share)}
    res.sheet_current = {n: rep.sheet_current(k, setup.fine.build.d) for k, n in enumerate(names)}
    res.sheet_vectors = {n: tuple(v.astype(np.float32) for v in rep.sheet_vectors(k, setup.fine.build.d))
                         for k, n in enumerate(names)}
    res.reference_layer = names[int(np.argmax(share))]
    res.section = route_section(rep, setup.fine.build, route, own, corridor_mm(setup.h_mm))
    pins = [setup.drv_ret.position, setup.rcv_ret.position]
    res.transfers = transfers(setup.fine.build, setup.fine.exc.stack, rep, route, pins, own)
    if setup.cavity is not None:
        crep = spectral[min(spectral, key=lambda f: abs(math.log(f / target)))]
        caps = [t for t in transfers(setup.cavity.build, setup.cavity.exc.stack, crep, route, pins)
                if t["kind"] == "capacitance"]
        # the 3D view's board-wide picture keeps the vias at the return pins: at a resonance they carry the current
        # circulating with the plane capacitance (the demo board's ICs' GND vias: 11.9x the signal current)
        res.board_maps = board_maps(setup.cavity.build, setup.cavity.exc.stack, crep,
                                    transfers(setup.cavity.build, setup.cavity.exc.stack, crep, route, []))
        # ties (a capacitor and its via in series carry the same current, to 1e-15) go to the one nearest the
        # route, then by name: sorting on the fraction alone let rounding noise pick the one the issue names
        res.transfers = sorted(res.transfers + caps, key=lambda t: (-round(t["fraction"], 6),
                                                                     t.get("distance_mm", 0.0), t["name"]))[:8]

    _route_issues(setup, rules, a)
    res.seconds = time.time() - setup.t_start
    return res


def quasi_static_limit(board: Board) -> Optional[dict]:
    """{"f": Hz, "size_mm", "epsilon_r"}: the frequency at which the board's longer outline side is QS_FRACTION of a
    wavelength in its densest dielectric, f = QS_FRACTION·c / (size·√εr) [A47] (100 mm of FR-4, εr 4.5: 354 MHz).
    Above it the board-wide model, whose partial inductances and capacitances are static [A1], leaves out the
    retardation across the board and the planes' radiation loss: its resonances are placed about right (the cells'
    L and C carry the standing wave) but their peaks are too high. None without an outline."""
    pts = [pt for poly in board.outline for pt in poly[0]]
    if not pts:
        return None
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    size = max(max(xs) - min(xs), max(ys) - min(ys))
    if size <= 0:
        return None
    er = max((d.epsilon_r for d in board.stackup.dielectrics), default=4.5)
    from ..emi.radiation import C0
    return {"f": QS_FRACTION * C0 / (size * 1e-3 * math.sqrt(er)), "size_mm": size, "epsilon_r": er}


def band_top(config: Config, natural: float) -> tuple[float, dict]:
    """(the highest frequency to evaluate, the result's `band` record): the edge's own band (`natural`: a decade above
    its second corner [A6], or the ringing's reach), cut at analysis.max_frequency when that is set and lower. A cut
    band is not a passed one: band_notes says so and the plots grey it out."""
    cap = config.analysis.max_frequency
    top = natural if cap is None else min(natural, cap)
    return top, {"f_max": float(top), "natural": float(natural), "capped": bool(cap is not None and cap < natural)}


def mark_quasi_static(res: RouteResult, board: Board):
    """With the board-wide model (res.cavity): every emission row above quasi_static_limit gets "indicative": True,
    the band record its f_qs, and a note says what that means when any row lies there [A47]. A route or hot loop
    alone is a few millimetres and stays quasi-static far higher: without the board-wide model nothing is marked."""
    qs = quasi_static_limit(board) if res.cavity else None
    if qs is None:
        return
    res.band["f_qs"] = qs["f"]
    above = [r for r in res.emissions if r["f"] > qs["f"]]
    for r in above:
        r["indicative"] = True
    if above:
        res.notes.append(f"above {qs['f'] / 1e6:.0f} MHz the board ({qs['size_mm']:.0f} mm, εr {qs['epsilon_r']:g}) is "
                         f"more than a quarter wavelength across: the board-wide model's static fields leave out the "
                         "retardation across it and the planes' radiation loss, so the emissions there are indicative "
                         "upper bounds (resonances about where they are, their peaks too high)")


def indicative_text(worst: Optional[dict]) -> str:
    """What an emission issue adds when its worst harmonic lies in the indicative band (mark_quasi_static)."""
    if not worst or not worst.get("indicative"):
        return ""
    return "; above the board's quasi-static range: an indicative upper bound"


def resonance_text(res: RouteResult, r: dict) -> str:
    """What a plane_resonance issue adds when its resonance lies above the board's quasi-static limit [A47]."""
    f_qs = res.band.get("f_qs")
    return "; above the board's quasi-static range: an indicative upper bound" if f_qs and r["f"] > f_qs else ""


def band_notes(res: RouteResult) -> list[str]:
    """The note for a band cut by analysis.max_frequency: nothing above it was computed (not passed)."""
    b = res.band
    if not b.get("capped"):
        return []
    return [f"evaluated up to {b['f_max'] / 1e6:.4g} MHz (analysis.max_frequency); the edge's spectrum reaches "
            f"{b['natural'] / 1e6:.4g} MHz: nothing above {b['f_max'] / 1e6:.4g} MHz was computed, so it is not "
            "checked against the limit"]


def interpolated_worst_note(worst: dict, fs: list, warn_db: float) -> Optional[str]:
    """A note when the worst emission is not a solved value: no solve of the board-wide model within ±WORST_SOLVED_LOG
    of it (the budget and its WORST_EXTRA solves spent), so its moment is interpolated between the solves around it
    and a resonance between them is not excluded (ISSUE-32). Only near the warning level (as REFINE_MARGIN_DB)."""
    f = worst["f"]
    if worst["margin_db"] > warn_db + REFINE_MARGIN_DB or not fs:
        return None
    if min(abs(math.log(f / x)) for x in fs) <= WORST_SOLVED_LOG:
        return None
    lo = max((x for x in fs if x < f), default=None)
    hi = min((x for x in fs if x > f), default=None)
    where = (f"between the solves at {lo / 1e6:.4g} and {hi / 1e6:.4g} MHz" if lo and hi else "beyond the last solve")
    return (f"the worst emission ({f / 1e6:.4g} MHz, margin {worst['margin_db']:+.1f} dB) is interpolated {where}: a "
            "resonance there would be missed; raise analysis.max_solves to solve the band more finely")


def unresolved_gaps_note(setup: RouteSetup, config: Config) -> Optional[str]:
    """A note when the solve budget (analysis.max_solves) ended the board-wide model's refinement while gaps near the
    limit were still uncertain (next_frequencies would split them with more solves): between their ends the moment
    is interpolated, and a resonance there would be missed (ISSUE-32). None when the refinement ended by itself."""
    run, n = setup.spectrum_run, config.analysis.max_solves
    if run is None or run is not setup.cavity or len(run.sols) < n:
        return None
    left = next_frequencies(setup, run, len(run.sols) + 10 ** 6, config, 10 ** 6, gaps_only=True)
    if not left:
        return None
    where = ", ".join(f"{f / 1e6:.4g}" for f in sorted(left)[:4]) + (" …" if len(left) > 4 else "")
    return (f"the solve budget (analysis.max_solves: {n}) ended with {len(left)} gap(s) near the limit not resolved "
            f"(around {where} MHz): the moment there is interpolated and a resonance would be missed; raise "
            "analysis.max_solves to solve them")


def grid_sensitive_note(worst: dict, m_worst: float, m_low: float, warn_db: float, resolution: float = 1.0
                        ) -> Optional[str]:
    """A note when the worst emission comes from a board-wide plane resonance above GRID_SENSITIVE_F (the loop
    moment per ampere there, m_worst, at least GRID_SENSITIVE_RISE_DB over its low-frequency value m_low) and its
    margin is within GRID_SENSITIVE_DB of the warning level: on refboard (6 layers) such peaks changed by
    several dB between the 1x, 2x and 4x grids (two resonances near each other reinforce on one grid and not on
    the next; ARCHITECTURE "Board-wide resonances above 1 GHz"). The threshold was 1 GHz; since the strips carry
    their copper's width (2026-10-18) the 1x grid places refboard's largest mode (F.Cu/In1) at 940 MHz, where 2x and 4x
    have it at 1020, and the grids disagree from ~620 MHz up (they agree within 0.5 dB at 540-580 MHz), so 600 MHz.
    Where to check (the 8x and 16x runs, 2026-10-18 / 19): 4x and 8x agree within 0.8 dB rms at 0.5-1 GHz, so the
    note asks for 4x and none is given from 4x on below GRID_CHECK_F. Above it the grids keep differing point by point
    (4x/8x 2.9, 8x/16x 2.3 dB rms: sharp resonances a few per cent apart in frequency), but the band's worst margin
    moves about ±2 dB around its value from 4x on, 8x no closer to 16x than 4x is (+14.1 / +17.6 / +15.8 dB): so 4x
    there too, and from 4x on the note gives that uncertainty (GRID_ABOVE_DB)."""
    if worst is None or worst.get("margin_db") is None or worst["f"] < GRID_SENSITIVE_F or m_low <= 0 or m_worst <= 0:
        return None
    rise = 20 * math.log10(m_worst / m_low)
    if rise < GRID_SENSITIVE_RISE_DB or worst["margin_db"] > warn_db + GRID_SENSITIVE_DB:
        return None
    target = 4.0
    head = (f"the worst emission ({worst['f'] / 1e6:.4g} MHz, margin {worst['margin_db']:+.1f} dB) comes from a "
            f"plane resonance above {GRID_SENSITIVE_F / 1e6:g} MHz ({rise:+.0f} dB over the loop's low-frequency "
            f"value)")
    if resolution < target:
        return (f"{head}: on a coarse grid such a peak can sit several per cent off in frequency and several dB off "
                f"in height. Check it at {target:g}x resolution (Preferences, or --resolution)")
    if worst["f"] < GRID_CHECK_F:
        return None
    return (f"{head}: above {GRID_CHECK_F / 1e9:g} GHz such a peak's height moved by about ±{GRID_ABOVE_DB:g} dB "
            f"between the 4x, 8x and 16x grids on a test board, so read its margin to within that")


def unsolved_band(run: ModelRun, f_limit: float) -> Optional[dict]:
    """{"from", "failed"} when a solve above every solved frequency failed and harmonics up to f_limit lie above the
    last solved one: there the loop moment is held at its last value (_interp_loglog), so a plane resonance in that
    band would be missed and the margin there is not a result."""
    ok = run.ok()
    if not ok:
        return None
    top = max(ok)
    above = [f for f, s in run.sols.items() if s is None and f > top]
    if not above or top >= f_limit:
        return None
    return {"from": top, "failed": min(above)}


def unsolved_issue(setup, rules, who: str, loc, layer: str, key: str):
    """The band_unsolved warning for setup.res.unsolved."""
    u = setup.res.unsolved
    if u is None:
        return
    setup.issue(rules, "band_unsolved",
                f"{who}: the solver did not converge at {u['failed'] / 1e6:.4g} MHz; above {u['from'] / 1e6:.4g} MHz "
                "the emission is held at its value there, so a plane resonance in that band would be missed: "
                "simulate again at another resolution (Preferences)", loc, layer=layer, key=key, details=dict(u))


def _solve_row(f: float, s: Solution) -> dict:
    """One solve as a report row: frequency, loop L and R, |m| in mm^2, GMRES iterations."""
    return {"f": f, "L_nH": s.L_loop * 1e9, "R_ohm": s.Z_loop.real, "m_mm2": _m(s) * 1e6, "iterations": s.iterations}


def _route_issues(setup: RouteSetup, rules, a):
    """A route's Tier 2 issues: a return detour (loop inductance against the ideal reference), a plane resonance
    raising the loop moment, an unsolved part of the band, and the emission margin."""
    res, route = setup.res, setup.route
    mid = route.segments[len(route.segments) // 2] if route.segments else None
    loc = mid.start if mid else route.driver.position
    key_base = f"{route.driver.pin}->{route.receiver.pin}"
    if res.l_ratio is not None and res.l_ratio >= rules.detour_warning:
        sev = "error" if res.l_ratio >= rules.detour_error else "warning"
        peak = _peak_detour(res)
        top = next(iter(point_transfers(res.transfers)), None)
        via_note = (f"; largest layer/net transfer through {top['name']} ({top['fraction'] * 100:.0f} %, "
                    f"{top['distance_mm']:.1f} mm from the route)") if top else ""
        setup.issue(rules, "return_detour",
                    f"{route.driver.pin}→{route.receiver.pin}: loop inductance {res.solves[0]['L_nH']:.2f} nH is "
                    f"{res.l_ratio:.2f}× the ideal {res.ideal['L_nH']:.2f} nH"
                    + (f"; loop moment {res.m_excess_db:+.1f} dB vs reference" if res.m_excess_db is not None else "")
                    + via_note, peak[1] if peak else loc, severity=sev,
                    layer=peak[0] if peak else res.reference_layer, key=f"detour:{key_base}",
                    details={"l_ratio": res.l_ratio, "m_excess_db": res.m_excess_db})
    if res.resonance and res.resonance["rise_db"] >= rules.resonance_db:
        r = res.resonance
        pair = f" between {r['pair'][0]} and {r['pair'][1]}" if r.get("pair") else ""
        setup.issue(rules, "plane_resonance",
                    f"{route.driver.pin}→{route.receiver.pin}: the loop moment rises {r['rise_db']:+.1f} dB at "
                    f"{r['f'] / 1e6:.4g} MHz over its value at {(setup.f_ref or setup.spec.freq) / 1e6:.4g} MHz: a plane-pair "
                    f"resonance{pair} is excited by this net's return current{resonance_text(res, r)}", loc,
                    layer=r["pair"][0] if r.get("pair") else res.reference_layer, key=f"resonance:{key_base}",
                    details=dict(r))
    unsolved_issue(setup, rules, f"{route.driver.pin}→{route.receiver.pin}", loc, res.reference_layer,
                   f"unsolved:{key_base}")
    if res.worst is not None and res.worst["margin_db"] < rules.emi_margin_db:
        w = res.worst
        sev = "error" if w["margin_db"] < 0 else "warning"
        setup.issue(rules, "emi_margin",
                    f"{route.driver.pin}→{route.receiver.pin}: estimated {w['e_dbuv']:.1f} dBµV/m at "
                    f"{w['f'] / 1e6:.4g} MHz ({w['distance_m']:g} m) vs {a.limit_text()} limit "
                    f"{w['limit_dbuv']:.1f} dBµV/m (margin {w['margin_db']:+.1f} dB){indicative_text(w)}", loc,
                    severity=sev, layer=res.reference_layer, key=f"emi:{key_base}",
                    details={**{k: w[k] for k in ("f", "e_dbuv", "limit_dbuv", "margin_db")},
                             "indicative": bool(w.get("indicative"))})


def _interp_loglog(f, fs, vs):
    """Interpolate positive values vs at fs to f linearly in log-log (exact for power laws, the usual shape of a
    transfer or moment between resonances [A22]); one point: a constant."""
    fs = np.asarray(fs, float)
    vs = np.maximum(np.asarray(vs, float), 1e-30)
    if len(fs) == 1:
        return np.full_like(np.asarray(f, float), vs[0])
    return np.exp(np.interp(np.log(f), np.log(fs), np.log(vs)))


def make_chain(route: Route, st: StackBuild, n_drv, n_rcv, z_phys) -> list[TraceSegment]:
    """The signal loop from the driver's return node, along the route, to the receiver's return node, every
    step a filament with a known current (used where pours are not solved copper, e.g. the ideal reference)."""
    return _chain(route, st, n_drv, n_rcv, z_phys, pours=False)[0]


def make_chain_with_pours(route: Route, st: StackBuild, n_drv, n_rcv, z_phys):
    """Like make_chain, but a stretch of the route through pours of its own net (pour segments and the vias
    between them) is left to the solved copper: the filaments stop where the route enters the pour and resume
    where it leaves it, and the pour carries the current in between, spread over its width and all its
    stitching vias. Returns (chain, sections), sections = [(node in, node out)]: the grid nodes of the pour where
    the current enters and leaves it (the filaments run exactly to them, so the loop stays closed)."""
    return _chain(route, st, n_drv, n_rcv, z_phys, pours=True)


def _chain(route: Route, st: StackBuild, n_drv, n_rcv, z_phys, pours: bool):
    """make_chain / make_chain_with_pours: the closed filament path from the driver's return node up to the
    driver pad, along every route step (segments and vias at their layers' heights), to the receiver pad and
    down to its return node."""
    g0 = st.grids[0]

    def P(x, y, z):
        """A board point (mm) at height z (m) as a solver point (m, relative to the grid's node (0, 0))."""
        return np.array([(x - g0.x0) * 1e-3, (y - g0.y0) * 1e-3, z])

    chain: list[TraceSegment] = []
    sections = []

    def add(a, b, w=0.2e-3):
        """Append a filament from a to b of width w (a zero-length one is dropped)."""
        if np.linalg.norm(b - a) > 1e-12:
            chain.append(TraceSegment(a, b, w))

    def at_node(n):
        """A model node's solver position."""
        x, y = g0.xy(*n[1:])
        return P(x, y, st.z[n[0]])

    snap = max(1.5 * st.d, 0.5)
    # from the driver's return node in the plane, up to the driver pad's layer, across to the pad
    dx, dy = g0.xy(*n_drv[1:])
    rx, ry = g0.xy(*n_rcv[1:])
    dl, rl = route.driver.layers[0], route.receiver.layers[0]
    add(P(dx, dy, st.z[n_drv[0]]), P(dx, dy, z_phys[dl]))
    cur = P(dx, dy, z_phys[dl])
    add(cur, P(*route.driver.position, z_phys[dl]))
    cur = P(*route.driver.position, z_phys[dl])
    steps = route.steps
    k = 0
    while k < len(steps):
        s = steps[k]
        if pours and isinstance(s, RouteSegment) and s.kind == "zone" and _net_is_solved(st, s.net, s.layer):
            last, j = k, k + 1
            while j < len(steps):
                nxt = steps[j]
                if isinstance(nxt, RouteSegment) and nxt.kind == "zone" and nxt.net == s.net:
                    last = j
                elif not isinstance(nxt, RouteVia):
                    break
                j += 1
            end = steps[last]
            na, nb = st.node(s.layer, *s.start, s.net, snap), st.node(end.layer, *end.end, s.net, snap)
            if na is None or nb is None:
                raise SolverError(f"the route enters or leaves the {s.net} pour where the model has no {s.net} copper")
            entry, exit_ = P(*s.start, z_phys[s.layer]), P(*end.end, z_phys[end.layer])
            add(cur, entry)                                  # up to where the route enters the pour,
            add(entry, at_node(na))                          # into its copper at the grid node,
            sections.append((na, nb))                        # the pour carries the current from there
            add(at_node(nb), exit_)                          # to where the route leaves it
            cur = exit_
            k = last + 1
            continue
        if isinstance(s, RouteSegment):
            a, b = P(*s.start, z_phys[s.layer]), P(*s.end, z_phys[s.layer])
            add(cur, a)
            add(a, b, s.width * 1e-3)
            cur = b
        elif isinstance(s, RouteVia):
            a, b = P(*s.position, z_phys[s.from_layer]), P(*s.position, z_phys[s.to_layer])
            add(cur, a)
            add(a, b, max(s.drill, 0.2) * 1e-3)
            cur = b
        k += 1
    end_pad = P(*route.receiver.position, z_phys[rl])
    add(cur, end_pad)
    add(end_pad, P(rx, ry, z_phys[rl]))
    add(P(rx, ry, z_phys[rl]), P(rx, ry, st.z[n_rcv[0]]))
    return chain, sections


def _net_is_solved(st: StackBuild, net: str, layer: str) -> bool:
    """Is this net's copper part of the solved model on that layer?"""
    if layer not in st.names:
        return False
    return net in (st.grids[st.names.index(layer)].nets or [])


def _pour_injections(st: StackBuild, sections) -> list:
    """+1 A where the route enters a pour, -1 A where it leaves it (nodes of the pour's own net)."""
    out = []
    for na, nb in sections:
        if na != nb:
            out += [(na, 1.0), (nb, -1.0)]
    return out


def _top_plane_pair(st: StackBuild, sol: Solution) -> Optional[tuple[str, str]]:
    """The plane pair carrying the most displacement current in a solution (the resonating cavity), or None."""
    plane = st.plane
    if not plane.ncap or not len(sol.I_cap):
        return None
    cu = plane.cap[:, :2].astype(int)
    best, best_v = None, 0.0
    for k, l in {(int(a), int(b)) for a, b in cu}:
        sel = (cu[:, 0] == k) & (cu[:, 1] == l)
        v = float(np.abs(sol.I_cap[sel]).sum())
        if v > best_v:
            best, best_v = (st.names[k], st.names[l]), v
    return best


def _return_masks(st: StackBuild, own: set[str]) -> list:
    """Per layer (x strips, y strips): True where a strip belongs to a return net, False for the route's own
    copper (pours it runs through)."""
    masks = []
    for g in st.grids:
        ids = [k for k, n in enumerate(g.nets or []) if n in own]
        lab = g.label if g.label is not None else np.zeros((g.nx, g.ny), int)
        mine = np.isin(lab, ids) if ids else np.zeros(lab.shape, bool)
        masks.append((~mine[:-1, :], ~mine[:, :-1]))
    return masks


def transfers(st: StackBuild, plane: PlaneStack, sol: Solution, route: Optional[Route], return_pins: list,
              own: Optional[set] = None) -> list[dict]:
    """Vias, parts and plane capacitance that carry return current between layers or nets, biggest first.

    One entry per via/part (its layer pairs merged) and per capacitively coupled plane pair (net displacement
    current). Vias at the return pins themselves are left out: every return current has to pass them. So are
    vias of the route's own nets (`own`): in a pour they carry the signal, not its return.

    A plane-pair entry is spread over the whole pair ("distributed": its x/y are only the current-weighted
    centre) and is taken at the solution's frequency ("f"). Its fraction can exceed 1: KCL holds, so at a
    resonance of the plane capacitance with the vias joining the planes, the displacement current and the vias'
    current circulate in antiphase, each several times the signal current (tools/dev/transfer_check.py; the demo
    board: 12.9 A per A across In1-In2 at 1.45 GHz, 11.9 A back through the vias)."""
    route_lines = MultiLineString([LineString([s.start, s.end]) for s in route.segments if s.start != s.end]) \
        if route is not None and route.segments else None
    merged: dict[str, dict] = {}
    part_sum: dict[str, complex] = {}       # a part spread over parallel branches (part_pairs): their currents add
    joint_sum: dict[tuple, complex] = {}    # a through-hole pin's joints (add_pin): add up per layer pair
    for b, cur in zip(plane.branches, sol.I_branch):
        frac = float(abs(cur))
        if b.kind == "part":
            part_sum[b.name] = part_sum.get(b.name, 0j) + complex(cur)
            frac = abs(part_sum[b.name])
        elif b.kind == "via":
            key = (b.name, b.a[0], b.b[0])
            joint_sum[key] = joint_sum.get(key, 0j) + complex(cur)
            frac = abs(joint_sum[key])
        x, y = st.xy(b.a)
        if b.kind == "track":                 # a piece of track on one layer: no transfer between layers
            continue
        if b.kind == "via" and any(math.dist((x, y), p) < 1.0 for p in return_pins):
            continue
        if b.kind == "via" and own and st.grids[b.a[0]].net_at(b.a[1:]) in own:
            continue
        layers = sorted({st.names[b.a[0]], st.names[b.b[0]]}, key=st.names.index)
        t = merged.get(b.name)
        if t is None:
            dist = route_lines.distance(Point(x, y)) if route_lines is not None else 0.0
            merged[b.name] = {"name": b.name, "kind": b.kind, "layers": layers, "x": x, "y": y,
                              "fraction": frac, "distance_mm": float(dist)}
        else:
            t["fraction"] = frac if b.kind == "part" else max(t["fraction"], frac)
            t["layers"] = sorted(set(t["layers"]) | set(layers), key=st.names.index)
    if plane.ncap and len(sol.I_cap):
        cu = plane.cap[:, :4].astype(int)
        for k, l in sorted({(int(a), int(b)) for a, b in cu[:, :2]}):
            sel = (cu[:, 0] == k) & (cu[:, 1] == l)
            I = sol.I_cap[sel]
            net = abs(I.sum())
            if net < 0.02:
                continue
            wts = np.abs(I)
            gx, gy = st.grids[0].xy(cu[sel, 2], cu[sel, 3])
            x, y = float((gx * wts).sum() / wts.sum()), float((gy * wts).sum() / wts.sum())
            dist = route_lines.distance(Point(x, y)) if route_lines is not None else 0.0
            name = f"plane capacitance {st.names[k]}–{st.names[l]}"
            merged[name] = {"name": name, "kind": "capacitance", "layers": [st.names[k], st.names[l]], "x": x,
                            "y": y, "fraction": float(net), "distance_mm": float(dist), "distributed": True,
                            "f": float(sol.f)}
    out = [t for t in merged.values() if t["fraction"] >= 0.01]        # every carrier (the maps can show fewer)
    out.sort(key=lambda t: -t["fraction"])
    return out


def board_maps(st: StackBuild, plane: PlaneStack, sol: Solution, board_transfers: list[dict]) -> dict:
    """The board-wide model's picture at one frequency, for the 3D view (report/view3d.py): per layer |K| and
    (Kx, Ky) at the solution's dominant phase (A/m per A of signal), per plane pair the displacement current
    density between the two planes at the same phase (A/m² per A, + = from the upper plane to the lower), and the
    vias and parts that carry current between layers there. All layers share one grid (the board's)."""
    d = st.d
    rot = np.exp(-1j * sol.dominant_phase())
    out = {"f": float(sol.f), "layers": list(st.names), "grids": dict(zip(st.names, st.grids)),
           "sheet_current": {n: sol.sheet_current(k, d).astype(np.float32) for k, n in enumerate(st.names)},
           "sheet_vectors": {n: tuple(v.astype(np.float32) for v in sol.sheet_vectors(k, d))
                             for k, n in enumerate(st.names)},
           "displacement": {}, "transfers": point_transfers(board_transfers)}
    if plane.ncap and len(sol.I_cap):
        cu = plane.cap[:, :4].astype(int)
        cell = (d * 1e-3) ** 2
        for k, l in sorted({(int(a), int(b)) for a, b in cu[:, :2]}):
            sel = (cu[:, 0] == k) & (cu[:, 1] == l)
            m = np.zeros(st.grids[0].node.shape, np.float32)
            m[cu[sel, 2], cu[sel, 3]] = (sol.I_cap[sel] * rot).real / cell
            out["displacement"][(st.names[k], st.names[l])] = m
    return out


def layer_crossing(plane: PlaneStack, sol: Solution, k: int) -> tuple[complex, complex]:
    """(displacement current, via/part current) flowing from plane layers 0..k down to layers k+1.. (per A of
    signal). By KCL their sum is what the injections above the cut put in: 1 for a return that changes from an
    upper to a lower plane, 0 for one that stays above."""
    cap = complex(0)
    if plane.ncap and len(sol.I_cap):
        up, lo = plane.cap[:, 0].astype(int), plane.cap[:, 1].astype(int)
        cap = complex(sol.I_cap[(up <= k) & (lo > k)].sum())
    via = complex(0)
    for b, cur in zip(plane.branches, sol.I_branch):
        if b.a[0] <= k < b.b[0]:
            via += cur
        elif b.b[0] <= k < b.a[0]:
            via -= cur
    return cap, via


def point_transfers(transfers: list[dict]) -> list[dict]:
    """The transfers through a via, pad or part, biggest first: the "main transfer" of a route. A plane pair's
    displacement current is spread over the pair, not at one place."""
    return [t for t in transfers if t.get("kind") != "capacitance"]


def displacement(transfers: list[dict]) -> Optional[dict]:
    """The largest plane-pair displacement current among the transfers, or None."""
    return next((t for t in transfers if t.get("kind") == "capacitance"), None)


def _peak_detour(res: RouteResult) -> Optional[tuple[str, tuple[float, float]]]:
    """(layer, location) of the strongest plane current far from the route: where the detour is."""
    import shapely
    if not res.route_xy:
        return None
    lines = MultiLineString([LineString(pts) for _, pts in res.route_xy if len(pts) >= 2])
    best = None
    for layer, K in res.sheet_current.items():
        g = res.layer_grids[layer]
        if K.max() <= 0:
            continue
        ii, jj = np.nonzero(0.05 * K.max() < K)
        X, Y = g.xy(ii, jj)
        dist = shapely.distance(shapely.points(X, Y), lines)
        score = K[ii, jj] * dist * res.layer_share.get(layer, 0.0)
        k = int(np.argmax(score))
        if dist[k] >= 3 * max(res.height_mm, g.d) and (best is None or score[k] > best[0]):
            best = (score[k], layer, (float(X[k]), float(Y[k])))
    return (best[1], best[2]) if best else None


# --------------------------------------------------------------------------- all nets
def solver_threads(config: Config) -> int:
    """analysis.threads: a number, or "auto" = the physical cores (half the logical ones), at most MAX_THREADS."""
    t = getattr(config.analysis, "threads", "auto")
    if isinstance(t, (int, float)) and t >= 1:
        return int(t)
    if getattr(config.analysis, "all_cores", False):
        return max(1, (os.cpu_count() or 2) // 2)          # "Use all cores": every physical core
    return max(1, min(MAX_THREADS, (os.cpu_count() or 2) // 2))


def refine_width(config: Config, threads: int, runs: int) -> int:
    """How many frequencies each refining run takes per round: 1 (the default: exactly the sequence one thread
    would choose, the same result on every machine), or with analysis.all_cores ("Use all cores", the user: "make it
    an opt-in") enough to keep `threads` busy with `runs` runs refining, at most ALL_CORES_K. Refining several gaps
    at once once spent the budget elsewhere and missed a resonance peak on the real board, so solve_setups raises
    the budget by the same factor: the extra frequencies come on top."""
    if not getattr(config.analysis, "all_cores", False):
        return 1
    return max(1, min(ALL_CORES_K, threads // max(1, runs)))


FILL_PER_UNKNOWN = peec.FILL_PER_UNKNOWN       # SuperLU fill per branch unknown until measured [A38]
LU_COMMIT = peec.LU_COMMIT                      # SuperLU commits ~6.5x its fill (ARCHITECTURE "Memory use") [A38]
MEMORY_SHARE = 0.8                              # of the available memory the parallel solves may plan with


def factor_bytes(stack: PlaneStack) -> int:
    """About the memory one factorisation of this model takes: its fill, as SuperLU commits it (measured once the
    model has one: peec.lu_bytes)."""
    return peec.lu_bytes(stack)


def solve_bytes(stack: PlaneStack) -> int:
    """About the memory one solve of this model needs on top of the model itself: a factorisation being made
    (its fill, as SuperLU commits it) and the GMRES basis."""
    n = stack.nb + stack.n_nodes
    basis = (gmres_restart(n) + 1) * n * 16      # its longest (peec.gmres_restart: at least GMRES_RESTART_MIN rows)
    return int(factor_bytes(stack) + basis + 12 * n * 16)


def _stacks_of(run) -> list:
    """The stacks a run's solves touch: its own, and the base of a derived one (whose factorisations it uses)."""
    st = run.exc.stack
    return [st] if st._base is None else [st, st._base]


def solve_groups(runs: list, threads: int, available: Optional[int]) -> list[list]:
    """The runs [(setup, run)] in groups solved one after the other, so that the factorisations alive at once fit
    in MEMORY_SHARE of the available memory: one group (everything together, as before) when they fit or the
    available memory is unknown.

    A group is made of whole components: runs that share a model (a stack, or the base of a derived one) are never
    split, so every model sees the same rounds as when everything is solved together and every number stays the
    same (a run's refinement depends only on its own model's solutions and its setup's planned ones). The
    components with a board-wide model (shared by every route and stage) come last: their refinement weighs the
    route and stage models' planned solves (band_moments, the ringing). Peak per group: OPERATOR_CACHE
    factorisations kept per model, plus `threads` being made."""
    if available is None or len(runs) < 2:
        return [runs]
    parent: dict[int, int] = {}

    def root(k: int) -> int:
        """Union-find: a stack's component representative (with path halving)."""
        while parent.setdefault(k, k) != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k
    for _, r in runs:
        ids = [id(x) for x in _stacks_of(r)]
        for k in ids[1:]:
            parent[root(k)] = root(ids[0])
    comps: dict[int, list] = {}
    for s, r in runs:
        comps.setdefault(root(id(r.exc.stack)), []).append((s, r))
    boardwide = lambda c: any(r is s.cavity for s, r in c)
    order = [c for c in comps.values() if not boardwide(c)] + [c for c in comps.values() if boardwide(c)]

    def held(c) -> int:
        """The factorisation memory a component keeps (OPERATOR_CACHE per stack)."""
        stacks = {id(x): x for _, r in c for x in _stacks_of(r)}.values()
        return sum(peec.OPERATOR_CACHE * factor_bytes(x) for x in stacks)
    in_flight = threads * max(solve_bytes(r.exc.stack) for _, r in runs)
    room = MEMORY_SHARE * available - in_flight
    if sum(held(c) for c in order) <= room or len(order) < 2:
        return [runs]
    groups, size = [[]], 0
    for c in order:
        h = held(c)
        if groups[-1] and size + h > room:
            groups.append([])
            size = 0
        groups[-1] += c
        size += h
    return groups


def memory_threads(threads: int, stacks: list, available: Optional[int]) -> int:
    """`threads` (from analysis.threads "auto"), fewer when that many solves of the largest model would not fit in
    MEMORY_SHARE of the available memory: a run that fits beats one that runs out and retries solves one by one
    (run_parallel). At least 1; unchanged when the available memory is unknown. Results don't depend on it."""
    if available is None or not stacks:
        return threads
    need = max(solve_bytes(st) for st in stacks)
    return max(1, min(threads, int(MEMORY_SHARE * available // max(need, 1))))


class MemoryRelief:
    """What a run does when a solve runs out of memory (peec's memory gate keeps most from it; see "memory under
    pressure" there), and what it did, for the run's notes. The ladder, one solve at a time once the others are
    done (run_parallel):

    1. again alone, with every cached factorisation of every model freed first (they are made again, identically,
       when needed);
    2. again, with the run's models (`stacks`; every live one when not given) factorising with diagonal pivots
       only from now on (the least fill; GMRES corrects the weaker preconditioner: the results stand, the solves
       take more iterations) [A38];
    3. `on_short(task, error)`: Simulate leaves that frequency unsolved, with a note (as when GMRES does not
       converge: the band_unsolved warning names what it misses); without it the MemoryError is raised.

    `shorts` counts the solves that ran out beside others; solve_setups halves its threads for the rounds after."""

    def __init__(self, on_short: Optional[Callable] = None, stacks: Optional[list] = None):
        """No shortage yet; `on_short(task, error)` is the last rung, `stacks` the models rung 2 switches (see the
        class)."""
        self.on_short, self.stacks = on_short, stacks
        self.shorts = 0         # solves that ran out of memory beside others (retried alone)
        self.freed = 0          # cached factorisations freed by the ladder
        self.low = False        # rung 2 taken: every model factorises with the least fill
        self.unsolved = 0       # solves given up (rung 3)
        self._lock = threading.Lock()

    def step(self, rung: int):
        """Make room before the solve's next try: rung 0 frees the cached factorisations, rung 1 also switches every
        model to the least fill."""
        n = peec.free_factorisations()
        if rung >= 1:
            peec.low_memory_everywhere(self.stacks)
            self.low = True
        gc.collect()
        with self._lock:
            self.freed += n

    def give_up(self, task, error: MemoryError):
        """The last rung: on_short's answer, or the MemoryError with advice (no on_short)."""
        if self.on_short is None:
            raise MemoryError("not enough memory for this model, even solved on its own with every cached "
                              "factorisation freed and the least-fill factorisation: close other programs, or lower "
                              "analysis.max_nodes in the settings") from error
        with self._lock:
            self.unsolved += 1
        return self.on_short(task, error)


def memory_notes(relief: MemoryRelief, stacks, gate0: tuple = (0, 0)) -> list[str]:
    """What the run did because memory was short, for its notes (none when it never was): the memory gate's waits
    and freed caches (since `gate0`, its counters before the run: the gate serves the whole process), the
    least-fill factorisations, the solves retried alone, the frequencies given up."""
    out = []
    freed = peec.GATE.freed - gate0[0] + relief.freed
    waits = peec.GATE.waits - gate0[1]
    low = sum(getattr(x, "lu_memory_fallbacks", 0) for x in stacks)
    if freed or waits or low or relief.shorts or relief.low:
        what = [f"{freed} cached factorisation(s) freed early" if freed else "",
                f"{waits} factorisation(s) waited for others to finish" if waits else "",
                f"{relief.shorts} solve(s) retried on their own" if relief.shorts else "",
                f"{low} factorisation(s) made with diagonal pivots only (the least fill)" if low else "",
                "every later factorisation made with the least fill" if relief.low else ""]
        out.append("memory was short during the solves, so the run worked around it: " + "; ".join(w for w in what if w)
                   + ". The results stand (a freed factorisation is made again identically; a least-fill one only "
                     "costs GMRES iterations), the run took longer")
    if relief.unsolved:
        out.append(f"{relief.unsolved} frequency solve(s) could not be done even so and were left unsolved (each "
                   "model's notes name them; a band_unsolved warning says what that misses): a lower resolution, "
                   "fewer jobs at once on the compute server or more free memory solves them")
    return out


def run_parallel(tasks: list, work: Callable, threads: int, relief: Optional[MemoryRelief] = None) -> list:
    """work(task) for every task, in a thread pool when threads > 1. The FFTs inside share the cores (each
    worker gets cpu_count / threads of them). An exception in any task (e.g. Cancelled from a progress
    callback) cancels the tasks not started yet and is raised once the running ones have finished. A task out of
    memory goes through `relief`'s ladder once the others are done (MemoryRelief; a default one without
    on_short)."""
    relief = relief if relief is not None else MemoryRelief()
    if threads <= 1 or len(tasks) <= 1:
        return [_alone(work, t, relief) for t in tasks]
    n = min(threads, len(tasks))
    share = max(1, (os.cpu_count() or 2) // n)
    short = object()

    def one(t):
        """Run one task in a pool thread with its share of the FFT threads; out of memory marks it for a retry."""
        with thread_budget(share):
            try:
                return work(t)
            except MemoryError:
                return short                 # retried alone below, once the others have freed their memory
    with ThreadPoolExecutor(n, thread_name_prefix="groundtruth-solve") as pool:
        futures = [pool.submit(one, t) for t in tasks]
        try:
            out = [f.result() for f in futures]
        except BaseException:
            for f in futures:
                f.cancel()
            raise
    # Out of memory in parallel (refboard's four power stages and four nets: ~2 GB per model at once,
    # with ~5 GB free beside the user's other programs): those solves again, one at a time.
    for k, r in enumerate(out):
        if r is short:
            relief.shorts += 1
            gc.collect()
            out[k] = _alone(work, tasks[k], relief)
    return out


def _alone(work: Callable, task, relief: MemoryRelief):
    """Run one task by itself; out of memory, the relief's ladder (MemoryRelief): freed caches, then the least
    fill, then its last rung."""
    for rung in range(2):
        try:
            return work(task)
        except MemoryError:
            relief.step(rung)
    try:
        return work(task)
    except MemoryError as e:
        gc.collect()
        return relief.give_up(task, e)


def solve_cost(run: ModelRun) -> float:
    """A solve's relative cost on this model (progress plans): its unknowns per 100 000. A 34 000-node board-wide
    solve and a DC solve on a small stage model are not the same step of the bar."""
    return max(run.exc.stack.nb / 1e5, 0.05)


def solve_setups(setups: list, config: Config, progress=None, rest: float = 0.0, free_done: bool = False,
                 relief: Optional[MemoryRelief] = None):
    """Solve every route (and power stage): the planned frequencies first, then refinement rounds. Each round's
    solves are independent (different frequencies, routes and models) and run in parallel; within a model, a
    factorisation serves every route at that frequency (and, as a preconditioner, its neighbours).

    A setup may bring extra runs (`extra_runs`, refined by its own `refine(run, config)`, e.g. a converter's
    conducted band) and a `planned_solved()` hook called after the first round.

    progress: a callback or a Phases plan; this step's share is its planned solves (each weighted by its model's
    size, solve_cost) against `rest`, the estimated work after it.

    free_done: after each round, the factorisations of the models no run still refining uses are freed (Simulate:
    it frees them all after the solves anyway). Results don't change: a freed one is made again identically when
    needed (PlaneStack.operator). With it, when the factorisations kept at once would not fit in the available
    memory, the models are solved in groups, one after the other (solve_groups; the same numbers).

    Out of memory: `relief` (MemoryRelief; one is made without it) takes a solve that ran out through its ladder,
    and its last rung leaves that frequency unsolved with a note: the run always finishes. After a round in which
    solves ran out beside others, the rounds after it run on half the threads; with threads "auto" each round's
    threads are also planned again from the models' measured fill (memory_threads, peec.lu_bytes).

    Returns the number of groups."""
    from ..core.sysmem import available_bytes
    max_solves = max(config.analysis.max_solves, 1)
    threads = solver_threads(config)
    runs = [(s, s.fine) for s in setups] + [(s, s.cavity) for s in setups if s.cavity is not None]
    runs += [(s, r) for s in setups for r in getattr(s, "extra_runs", ())]
    t = getattr(config.analysis, "threads", "auto")
    auto = not (isinstance(t, (int, float)) and t >= 1)    # "auto" (a number is the user's choice)
    avail0 = available_bytes()          # before this run's own factorisations take any of it
    if auto:
        threads = memory_threads(threads, [r.exc.stack for _, r in runs], avail0)

    def unsolved(task, error: MemoryError):
        """The relief's last rung: the frequency is left unsolved, with a note (as a SolverError in solve_at)."""
        s, r, f = task
        r.sols[f] = None
        kind = "board-wide" if r is s.cavity else getattr(s, "model_label", "route")
        s.res.notes.append(f"{f / 1e6:.4g} MHz on the {kind} model ({r.build.nodes} nodes): not solved, not enough "
                           f"memory even on its own with the least-fill factorisation ({error}); a lower "
                           "resolution, fewer jobs at once on the server or more free memory solves it")
    relief = relief if relief is not None else MemoryRelief()
    if relief.on_short is None:
        relief.on_short = unsolved
    if relief.stacks is None:           # rung 2 switches this run's models (and their bases) only
        relief.stacks = list({id(x): x for _, r in runs for x in _stacks_of(r)}.values())
    seen = [relief.shorts]

    def plan(threads: int) -> int:
        """The threads for the next round: halved after solves ran out beside others, and (auto) planned again with
        the models' measured fill against the memory free when the solves began (not the memory free now, which
        this run's own cached factorisations lower): fewer only when the first estimate was too low. Never more
        than before: results don't depend on it."""
        if relief.shorts > seen[0]:
            seen[0] = relief.shorts
            threads = max(1, threads // 2)
        if auto and threads > 1:
            threads = memory_threads(threads, [r.exc.stack for _, r in runs], avail0)
        return threads
    # "Use all cores": several frequencies per refining run and round, the budget raised as much (refine_width)
    width = refine_width(config, threads, sum(1 for s, r in runs if r is s.spectrum_run or
                                              any(r is x for x in getattr(s, "extra_runs", ()))))
    max_solves *= width

    def extra(s, r) -> bool:
        """Whether run r is one of setup s's extra runs (refined by the setup itself)."""
        return any(r is x for x in getattr(s, "extra_runs", ()))
    # progress against the solve budget (refinement may stop early), so the bar never moves backwards
    budget = {id(r): max(len(r.planned), max_solves) if r is s.spectrum_run else len(r.planned) for s, r in runs}
    planned = sum(budget[id(r)] * solve_cost(r) for _, r in runs)
    n_planned = sum(budget.values())
    ph = progress if isinstance(progress, Phases) or progress is None else Phases(progress)
    if ph is not None:
        ph.start(planned, rest=rest)
    done = [0.0, 0]
    lock = threading.Lock()

    def run(task):
        """One solve (setup, run, frequency), with a progress report before it and the count after."""
        s, r, f = task
        if ph is not None:
            kind = "board-wide" if r is s.cavity else getattr(s, "model_label", "route")
            with lock:
                frac, k = done[0] / max(planned, 1e-9), done[1] + 1
            ph.report(f"{s.spec.name}: {f / 1e6:.4g} MHz, {kind} model ({r.build.nodes} nodes), "
                      f"solve {k} of ≤ {max(n_planned, k)}", frac)
        solve_at(s, r, f)
        with lock:
            done[0] += solve_cost(r)
            done[1] += 1

    stacks = list({id(r.exc.stack): r.exc.stack for _, r in runs}.values())

    def next_round():
        """Start a new round on every stack."""
        # Factorisations decided in a round are borrowed only from the next one on (PlaneStack.next_round): which
        # one a solve uses must not depend on which solve of its round finished first
        for st in stacks:
            st.next_round()

    def free(still: list):
        """Free the factorisations of every stack no run in `still` uses (with free_done only)."""
        if not free_done:
            return
        keep = {id(x) for _, r in still for x in (r.exc.stack, r.exc.stack._base) if x is not None}
        for st in stacks:
            for x in (st, st._base):
                if x is not None and id(x) not in keep and x._ops:
                    x._ops.clear()
                    gpu.release(x)
    # in groups only when freeing between them lowers the peak and the factorisations kept would not fit otherwise
    groups = [runs]
    if free_done:
        groups = solve_groups(runs, threads, available_bytes())
    hooked: set[int] = set()
    for group in groups:
        next_round()
        first = [(s, r, f) for s, r in group for f in r.planned if f not in r.sols]
        threads = plan(threads)
        run_parallel(sorted(first, key=lambda t: t[2]), run, threads, relief)
        for s in setups:        # its own model's planned solves are done (they come first: solve_groups)
            hook = getattr(s, "planned_solved", None)
            if hook is not None and id(s) not in hooked and any(r is s.fine for _, r in group):
                hooked.add(id(s))
                hook()
        pending = [(s, r) for s, r in group if r is s.spectrum_run or extra(s, r)]
        free(pending)
        while pending:
            # one frequency per route and round, exactly as one thread would choose them: the thread count must not
            # change which harmonics get solved (refining several gaps at once spent the budget elsewhere and
            # missed a resonance peak on the real board). Different routes still refine in parallel. With "Use all
            # cores" (refine_width) a run takes up to `k` at once while threads would idle, on a larger budget.
            k = refine_width(config, threads, len(pending)) if width > 1 else 1
            wanted = []
            for s, r in pending:
                fs = next_frequencies(s, r, max_solves, config, k) if r is s.spectrum_run else s.refine(r, config)
                wanted += [(s, r, f) for f in fs]
            if not wanted:
                break
            next_round()
            threads = plan(threads)
            run_parallel(sorted(wanted, key=lambda t: t[2]), run, threads, relief)
            pending = list({id(r): (s, r) for s, r, _ in wanted}.values())
            free(pending)
        if len(groups) > 1:
            free([])            # the next group's peak without this one's factorisations
    next_round()        # what comes after (finishing, the fix search) may borrow the last round's
    return len(groups)


def band_moments(setup: RouteSetup, extra: Optional[dict] = None) -> tuple[list[float], np.ndarray]:
    """|loop moment| per ampere at the solved frequencies of the spectrum model (plus `extra` solutions of that
    model, {f: Solution}, which the run itself does not keep).

    With the board-wide model: m(f) = m_fine(f0) + [m_board(f) - m_board(f0)] (complex vectors). The fine
    model resolves the inductive loop next to the trace (frequency-independent above a few MHz); the
    board-wide model adds what plane capacitance changes (displacement current, resonances). Its own grid
    bias at f0 cancels in the difference.
    """
    run = setup.spectrum_run
    sols = {**run.ok(), **(extra or {})}
    fs = sorted(sols)
    if getattr(setup, "loop_weights", None) is not None and fs:     # signal paths only (not power stages)
        return fs, _loop_moments(setup, run, sols, fs)
    fs, vs = band_vectors(setup, extra)
    return fs, np.array([float(np.linalg.norm(v)) for v in vs])


def band_vectors(setup, extra: Optional[dict] = None) -> tuple[list[float], list[np.ndarray]]:
    """The loop moment per ampere as complex vectors (A m² per A, x/y/z) at the solved frequencies of the spectrum
    model: band_moments' m(f) before its magnitude is taken (with the board-wide model, m_fine(f0) + m_board(f) -
    m_board(f0)). The interleaved stages' fields add as these vectors (analysis/interleave.py)."""
    run = setup.spectrum_run
    sols = {**run.ok(), **(extra or {})}
    fs = sorted(sols)
    if setup.cavity is None or not fs:
        return fs, [sols[f].m for f in fs]
    fine = setup.fine.ok()
    f_ref = setup.f_ref or setup.spec.freq
    s0 = fine.get(f_ref) or (fine[min(fine)] if fine else None)
    if s0 is None:
        return fs, [sols[f].m for f in fs]
    c0 = sols.get(f_ref) or sols[fs[0]]
    offset = s0.m - c0.m
    return fs, [sols[f].m + offset for f in fs]


def _loops_at(run: ModelRun, f: float, main: np.ndarray) -> list:
    """[each shunt loop's moment ..., the receivers' (main)] at f; a loop not solved there (a solve that failed,
    or `extra` solutions of the fix search, which re-solve the main loop only) takes the main loop's."""
    ms = run.loop_m.get(f) or [None] * len(run.loops)
    return [main if m is None else m for m in ms] + [main]


def _loop_moments(setup: RouteSetup, run: ModelRun, sols: dict, fs: list[float]) -> np.ndarray:
    """|Σ_k w_k(f)·m_k(f)| per solved frequency: each loop's moment per ampere weighted by its current over the
    path's mean current (emi/path.py loop_currents), complex vectors. With the board-wide model each loop is
    m_fine(f0) + [m_board(f) - m_board(f0)], as for a single loop."""
    w = setup.loop_weights(np.array(fs, float))
    vec = _loop_vectors(setup, run, sols, fs)
    out = []
    for j, f in enumerate(fs):
        m = sum(w[k][j] * vec[f][k] for k in range(len(vec[f])))
        out.append(float(np.linalg.norm(m)))
    return np.array(out)


def _loop_vectors(setup: RouteSetup, run: ModelRun, sols: dict, fs: list[float]) -> dict:
    """{solved f: [each loop's moment vector per ampere ..., the main loop's]} (the board-wide offset applied)."""
    vec = {f: _loops_at(run, f, sols[f].m) for f in fs}
    if setup.cavity is not None:
        fine = setup.fine.ok()
        f_ref = setup.f_ref or setup.spec.freq
        f0 = f_ref if f_ref in fine else (min(fine) if fine else None)
        if f0 is not None:
            fine_loops = _loops_at(setup.fine, f0, fine[f0].m)
            c0 = f_ref if f_ref in sols else fs[0]
            board0 = vec[c0]
            vec = {f: [a + (fv - b) for a, fv, b in zip(fine_loops, v, board0)] for f, v in vec.items()}
    return vec


def moment_curve(setup, freqs, fs: Optional[list[float]] = None, ms=None) -> np.ndarray:
    """|loop moment| per ampere at the harmonics `freqs`. One loop: |m| interpolated log-log between the solved
    frequencies (fs, ms from band_moments). Several loops (a signal path's shunts or op-amp stages): each loop's
    moment VECTOR is interpolated (linear in log f, complex components) and the loops are combined at every
    harmonic with that harmonic's weights. Combining at the solved frequencies first and interpolating the sum
    missed what the weights do in between: on an op-amp test board the output line's resonance turned the two
    loops' weights opposite at 3.2 GHz, and the sum interpolated from 60 MHz to 3.2 GHz came out up to 14 dB low."""
    loop_weights = getattr(setup, "loop_weights", None)
    if loop_weights is None:
        if fs is None:
            fs, ms = band_moments(setup)
        return _interp_loglog(freqs, fs, ms)
    run = setup.spectrum_run
    sols = run.ok()
    sf = sorted(sols)
    f = np.asarray(freqs, float)
    if not sf:
        return np.zeros(len(f))
    vec = _loop_vectors(setup, run, sols, sf)
    n = len(vec[sf[0]])
    w = loop_weights(f)
    total = np.zeros((len(f), 3), complex)
    logs, logt = np.log(np.asarray(sf, float)), np.log(np.maximum(f, 1e-30))
    for k in range(n):
        v = np.array([vec[x][k] for x in sf])                       # (solved, 3) complex
        if len(sf) == 1:
            vi = np.repeat(v, len(f), axis=0)
        else:
            vi = np.stack([np.interp(logt, logs, v[:, c].real) + 1j * np.interp(logt, logs, v[:, c].imag)
                           for c in range(3)], axis=1)
        total += w[k][:, None] * vi
    return np.maximum(np.linalg.norm(total, axis=1), 1e-30)


def _margin_curve(setup: RouteSetup, config: Config, fs: list[float], ms) -> np.ndarray:
    """Estimated emission margin (dB) at every harmonic, with the loop moment interpolated between the solved
    frequencies (refinement decisions). NaN where no limit applies."""
    a = config.analysis
    m = moment_curve(setup, setup.harmonics, fs, ms)
    return emission_arrays(setup.harmonics, setup.currents, m, a.limit_spec(), a.distance_m,
                           a.ground_reflection)["margin_db"]


def simulate(board: Board, config: Config, nets: Optional[list[str]] = None,
             progress: Optional[Progress] = None, copper: Optional[CopperMap] = None,
             model: Optional[BoardModel] = None, keep_factorisations: bool = False,
             part: Optional[dict] = None) -> SimulationResult:
    """Tier 1 + Tier 2 for the configured nets and power stages. `nets` limits the run to these names (nets
    and/or converters). The solver iterations run on a GPU per `analysis.gpu` (solver/gpu.py); `result.solver`
    says where they ran. Nets, converters and bridges switched off (enabled: false) are left out unless named
    in `nets` (Config.active). The factorisations are freed once the solves are done, unless
    keep_factorisations (the fix search's baseline: its candidates are low-rank updates of them).

    `part`: this run is one part of a Simulate split over several compute servers (remote/split.py), `nets` its
    items; merge_parts joins the parts into the whole run's result. {"checks": True, "job_nets": [...] or None}
    makes this part the one that runs every rule check of the whole run (the others check only their items'
    routes, which they need). A part leaves out what only the whole run can do: the stages' DC paths together
    (combine_dc; each stage's paths are kept portable instead, compact_dc_branch) and the issues' final
    dedupe/waivers/sort; see `SimulationResult.part`."""
    full = config
    config = config.active(nets)
    with gpu.use(getattr(config.analysis, "gpu", "auto")) as run:
        result = _simulate(board, config, nets, progress, copper, model, keep_factorisations, part,
                           full if part is not None else None)
    result.solver = run.summary()
    return result


def _simulate(board: Board, config: Config, nets: Optional[list[str]], progress: Optional[Progress],
              copper: Optional[CopperMap], model: Optional[BoardModel],
              keep_factorisations: bool = False, part: Optional[dict] = None,
              full: Optional[Config] = None) -> SimulationResult:
    """simulate's work: the rule checks (they trace the routes), every route's and power stage's (and bridge's)
    models built, all their solves scheduled together (solve_setups), the factorisations freed, then each one's
    result finished (in parallel; bridges after), the issues collected, waivers applied and sorted. `part` /
    `full` (the settings before Config.active): a split Simulate's part (see simulate)."""
    from .power import ConverterSetup, finish_converter, prepare_converters
    ph = Phases(progress) if progress else None
    stack_notes: list[str] = []
    if model is None and config.stack:      # a stack of boards (analysis/stack.py): one model of all of them
        from .stack import stack_board
        board, stack_notes = stack_board(board, config)
        copper = None
    model = model or BoardModel(board, config, copper)
    if part and part.get("checks"):
        # the part that runs the whole job's rule checks (its routes are all the job's routes; only its own items'
        # are simulated below, `todo`)
        checks = run_checks(board, full or config, model.copper, part.get("job_nets"))
    else:
        checks = run_checks(board, config, model.copper, nets)
    result = SimulationResult(checks, model=model, notes=stack_notes)
    if getattr(config.analysis, "all_cores", False):
        result.notes.append(f"Use all cores: {solver_threads(config)} solver threads, up to {ALL_CORES_K} frequencies "
                            "per refinement round on a budget raised as much; figures can differ slightly from a "
                            "run without it, or on a computer with another number of cores")
    issues = [] if part is not None else list(checks.issues)        # a part's check issues stay in result.checks
    specs = {s.name: s for s in config.nets}
    todo = [(k, r) for k, r in checks.routes.items() if not nets or k[0] in nets]
    conv = [c for c in config.converters if not nets or c.name in nets]
    if ph is not None:      # building a model takes about what a solve or two does (6-layer board: 3-4 s)
        a = config.analysis
        est = (len(todo) * (a.solve_points + a.max_solves) + len(conv) * (a.solve_points + a.max_solves + 10) * 1.5)
        ph.start(1.5 * (len(todo) + 2 * len(conv)), rest=est, steps=len(todo) + (1 if conv else 0))
    def step(msg):
        """A progress step (a no-op context without progress)."""
        return ph.step(msg) if ph is not None else nullcontext()
    setups: list[RouteSetup] = []
    for (net, _rx), route in todo:
        with step(f"{net}: building plane models"):
            setup, res, route_issues = prepare_route(model, specs[net], route)
        result.routes.append(res)
        if setup is None:
            issues.extend(route_issues)
        else:
            setups.append(setup)
    with step("power stages: building their models") if conv else nullcontext():
        csetups, result.converters, conv_issues = prepare_converters(model, conv)
    issues.extend(conv_issues)
    brid = [b for b in config.bridges if not nets or b.name in nets or any(b.leg_name(l) in nets for l in b.legs)]
    bsetups = []
    if brid:
        from .bridge import prepare_bridges
        with step("motor bridges: the drive and the legs' models"):
            bsetups, leg_setups, leg_results, result.bridges, b_issues = prepare_bridges(model, brid)
            for br in result.bridges:
                br.notes[:0] = stack_notes
        csetups += leg_setups
        result.converters += leg_results
        issues.extend(b_issues)
    # DC rails with loads (analysis/dcload.py): one DC run each on the whole-board DC model
    dl = [d for d in getattr(config, "dc_loads", ()) if not nets or d.name in nets]
    dsetups = []
    if dl:
        from .dcload import prepare_dc_loads
        with step("DC loads: their model"):
            dsetups, result.dc_loads, d_issues = prepare_dc_loads(model, dl)
        issues.extend(d_issues)
    # supply rails' PDN impedance (analysis/pdn.py): one whole-board model per rail, its ICs as ports
    pl = [d for d in getattr(config, "pdn", ()) if not nets or d.label in nets]
    psetups = []
    if pl:
        from .pdn import prepare_pdn
        with step("PDN: the rails' models"):
            psetups, result.pdn, p_issues = prepare_pdn(model, pl)
        issues.extend(p_issues)
    everything = setups + csetups + dsetups + psetups
    # finishing: the ideal reference (a route-model solve), emissions, the power stages' DC paths and maps
    finish_cost = sum(solve_cost(s.fine) * (1.0 if isinstance(s, ConverterSetup) else 1.5) for s in everything)
    relief = MemoryRelief()
    gate0 = (peec.GATE.freed, peec.GATE.waits)
    result.groups = solve_setups(everything, config, ph, rest=finish_cost, free_done=not keep_factorisations,
                                 relief=relief)
    # factorisations past SuperLU's size limit, made with diagonal pivots instead (peec._factorise_diagonal)
    runs_all = [r for s in everything for r in [s.fine, getattr(s, "cavity", None), *getattr(s, "extra_runs", ())]
                if r is not None and getattr(r, "exc", None) is not None]
    stacks = {id(x): x for r in runs_all for x in (r.exc.stack, getattr(r.exc.stack, "_base", None)) if x is not None}
    n_diag = sum(getattr(x, "lu_fallbacks", 0) for x in stacks.values())
    if n_diag:
        result.notes.append(f"{n_diag} model(s) at this resolution passed the sparse solver's usual size limit "
                            "(SuperLU, ~2.1 billion entries) and were factorised with diagonal pivots only (the "
                            "least fill): the results stand, their solves took more iterations; a lower resolution "
                            "avoids it")
    result.notes += memory_notes(relief, stacks.values(), gate0)
    if not keep_factorisations:
        # finishing needs none of them (it solves only new models: a route's ideal reference): freed before it,
        # not after. On refboard (7 nets and stages) 41 were alive at the peak, ~2.6 GB of fill, each
        # SuperLU object committing ~6x its fill (tools/dev/mem_profile.py)
        drop_factorisations(everything)
    if ph is not None:
        ph.start(finish_cost, steps=len(everything))

    from .dcload import DcLoadSetup, finish_dc_load
    from .pdn import PdnSetup, finish_pdn

    def finish(s):
        """Finish one route, stage, DC load or PDN rail (its ideal reference solve and emission estimate; a DC
        load's drops; a rail's impedance curves and checks)."""
        name = s.spec.label if isinstance(s, PdnSetup) else s.spec.name
        with step(f"{name}: ideal reference and emission"):
            if isinstance(s, ConverterSetup):
                finish_converter(model, s)
            elif isinstance(s, DcLoadSetup):
                finish_dc_load(model, s)
            elif isinstance(s, PdnSetup):
                finish_pdn(model, s)
            else:
                finish_route(model, s)
    def unfinished(s, error: MemoryError):
        """The relief's last rung in finishing: this route, stage, DC load or rail keeps an error, the others finish
        (the run is not lost for one of them)."""
        s.res.error = (f"not enough memory to finish it (its ideal reference and emission), even on its own with every "
                       f"cached factorisation freed and the least fill ({error}): a lower resolution, fewer jobs at "
                       "once on the compute server or more free memory finishes it")
    # rung 2 here switches every live model: finishing makes models of its own (a route's ideal reference, a stage's
    # DC paths), and this run's are the ones that matter now
    run_parallel(setups + csetups + dsetups + psetups, finish, solver_threads(config), MemoryRelief(on_short=unfinished))
    for s in psetups:
        issues.extend(s.issues)
    if bsetups:
        from .bridge import finish_bridge
        # one thread from here (the receiver, the sweep): a short switch interval lets a waiting GUI thread have
        # the GIL at once (ui/gil.py; the solver's default interval only keeps many waiting threads from spinning)
        saved = sys.getswitchinterval()
        sys.setswitchinterval(min(saved, 0.0005))
        try:
            for bs in bsetups:
                finish_bridge(model, bs)
                issues.extend(bs.issues)
        finally:
            sys.setswitchinterval(saved)
    result.setups = setups
    result.converter_setups = csetups
    result.dc_load_setups = dsetups
    result.pdn_setups = psetups
    route_issues = [i for s in setups for i in s.issues]
    # a DC load's issues count as a stage's: its density and via ones give way to the combined view's
    stage_issues = [i for s in csetups + dsetups for i in s.issues]
    if part is not None:
        # a split Simulate's part: the stages' DC paths portable for merge_parts (it combines every part's stages),
        # the issues by origin (merge_parts drops the stages' DC-path ones when the combination works, as below)
        from .power import compact_dc_branch
        for s in csetups + dsetups:
            compact_dc_branch(s.res)
        result.part = {"issues": issues, "route_issues": route_issues, "stage_issues": stage_issues,
                       "checks": bool(part.get("checks"))}
        if ph is not None:
            ph.report("done", 1.0)
        return result
    result.issues = finish_issues(result, issues, route_issues, stage_issues,
                                  [s.res for s in csetups] + list(result.dc_loads), config, board)
    if ph is not None:
        ph.report("done", 1.0)
    return result


def finish_issues(result: SimulationResult, issues: list, route_issues: list, stage_issues: list, converters: list,
                  config: Config, board: Optional[Board] = None) -> list:
    """The end of a Simulate (and of merge_parts): every stage's DC paths together (combine_dc: the combined view in
    result.dc_combined, its issues replacing each stage's own DC-path ones: the copper carries all loads at once),
    the interleave groups' emissions (analysis/interleave.py: their stages' phasors added, the group on each member),
    the stages' DC branch currents released, then all issues (the checks' and the preparations' `issues`, then the
    combination's, the routes', the stages') deduplicated, waivers applied and sorted. With the board: the
    voltage-aware checks of the solved results (analysis/safety.after_simulate: resistor losses with every stage's
    DC currents together, switch-node peaks against the Vds rating)."""
    from .power import combine_dc
    combo, combo_issues, combo_notes = combine_dc(converters, config.rules, config.analysis.via_plating_um)
    result.notes += combo_notes
    if combo is not None:
        result.dc_combined = combo
        issues = issues + combo_issues
        stage_issues = [i for i in stage_issues if not (i.check in ("current_density", "via_overload")
                                                        and (":dc:" in (i.key or "") or ":via:" in (i.key or "")))]
    # interleaved stages (analysis/interleave.py): each group's stages' phasors added, its figures on its members
    from .interleave import interleave_groups
    group_issues, group_notes = interleave_groups(result.converters, config)
    issues = issues + group_issues
    result.notes += group_notes
    if board is not None:
        from .safety import after_simulate
        issues = issues + after_simulate(board, config, result)
    # the paths' branch currents were kept for combine_dc only: they hold the whole-board DC model (hundreds of MB
    # on a large board) for as long as the window shows the result, and its branches' impedance closures keep the
    # result from being pickled (the compute server sends results back pickled)
    for r in converters:
        r.dc_branch = {}
        if hasattr(r, "dc_ripple_branch"):
            r.dc_ripple_branch = {}
    issues = dedupe(issues + route_issues + stage_issues)
    for w in config.waivers:
        for i in issues:
            if i.fingerprint == w.fingerprint:
                i.waived, i.waiver_reason = True, w.reason
    return sort_issues(issues)
