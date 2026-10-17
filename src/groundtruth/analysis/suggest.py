"""Fix suggestions: propose stitching vias, decaps, bridge capacitors and copper fills for the issues found,
check that they can be placed, predict their effect with the solver, and verify the best set together.

Power stages get stitching vias at the hot loop's pads (rail capacitors, switches) that have no via of their net
nearby: the loop then closes through the planes right there, not through a detour. They are predicted on the
converter's model (hot-loop inductance and moment at 100 MHz).

Flow (`suggest_fixes`):
  1. baseline simulation (fine route models + board-wide cavity model),
  2. candidates per issue, placed only where they are legal: inside the outline, clear of other nets'
     tracks/pads/vias (zones refill around them), and connected to the right plane copper,
  3. each candidate evaluated on the route's fine model at f0 and at the worst harmonic. A via or capacitor
     only adds branches, so the solve reuses the baseline factorisation through a low-rank update,
  4. the best candidate per issue (up to max_fixes per route) is verified together in a simulation of a copy of
     the board, giving before/after numbers and the issues that are fixed or new. Only what the fixes can change
     is simulated again (verify_scope); the rest keeps its baseline results, and its rule checks are re-run.

Progress (analysis/progress.py): each phase gets the part of the bar its estimated solver work (solves weighted
by model size, `_cost`) has against the work still to come, and counts its steps ("3 of 12").
"""
from __future__ import annotations

import copy
import math
from contextlib import nullcontext
from dataclasses import dataclass, field
from typing import Callable, Optional

import numpy as np
from shapely import STRtree
from shapely.geometry import Point, Polygon
from shapely.ops import unary_union

from ..config.modifications import Modification
from ..config.settings import Config
from ..core.board import Board
from ..core.issues import Issue
from ..geometry.copper import CopperMap, reference_layers
from ..geometry.shapes import pad_shape, track_shape
from ..rules.checks import run_checks
from ..solver import gpu
from ..solver.peec import Branch, SolverError, via_impedance
from .progress import Phases
from .simulate import (BoardModel, RouteSetup, SimulationResult, part_impedance, run_parallel, simulate,
                       solve_cost, solver_threads, vertical_branches)
from .whatif import apply_modifications, cap_pads

Progress = Callable[[str, float], None]

VIA_RADII = (0.8, 1.0, 1.3, 1.7, 2.2)
CAP_RADII = (1.5, 2.0, 2.5, 3.0, 4.0)
ANGLES = 12
CLEARANCE_MM = 0.2
EDGE_MARGIN_MM = 0.5
MIN_GAIN_DB = 0.3            # keep a candidate when it lowers the loop moment at least this much ...
MIN_GAIN_L = 0.01            # ... or the loop inductance by 1 %
MAX_HARM_DB = 1.0            # never select a fix predicted to raise a loop moment more than this (Tier 1 included)
GAP_RATIO = 1.15             # gaps between solved harmonics wider than this, next to one near the limit, get a
                             # baseline solve at their midpoint before the selected fixes are checked


@dataclass
class Suggestion:
    """One proposed fix (or advice where none can be placed): what it fixes, the what-if edit it would be, its
    predicted effect (loop moment at the worst harmonic, loop inductance), whether it was selected, and the
    predicted worst margins of the routes near the limit with it alone."""
    id: str
    net: str
    route: str                      # "U1.12->U3.6"
    issue: Optional[Issue]
    kind: str                       # via | cap | fill | advice
    title: str
    message: str
    location: tuple[float, float]
    layer: str = ""
    modification: Optional[Modification] = None
    delta_db: Optional[float] = None        # loop moment change at the worst harmonic (dB, negative = better)
    delta_l: Optional[float] = None         # loop inductance change at f0 (fraction, negative = better)
    f_eval: Optional[float] = None
    selected: bool = False
    note: str = ""
    # predicted worst emission margin of each route near the limit with this fix alone:
    # {"net drv->rcv": {"before_db", "after_db", "f"}}
    worst: Optional[dict] = None

    def to_dict(self) -> dict:
        """The suggestion as plain data for the JSON report (the issue by its fingerprint)."""
        return {"id": self.id, "net": self.net, "route": self.route, "kind": self.kind, "title": self.title,
                "message": self.message, "location": list(self.location), "layer": self.layer,
                "issue": self.issue.fingerprint if self.issue else None,
                "issue_check": self.issue.check if self.issue else None,
                "modification": self.modification.to_dict() if self.modification else None,
                "delta_db": self.delta_db, "delta_l": self.delta_l, "f_eval": self.f_eval,
                "selected": self.selected, "note": self.note, "worst": self.worst}


@dataclass
class SuggestResult:
    """A fix search: the suggestions, the simulation before and (verified) after the selected fixes, the
    comparison rows, the issues fixed and new, and what was not simulated again."""
    suggestions: list[Suggestion] = field(default_factory=list)
    before: Optional[SimulationResult] = None
    after: Optional[SimulationResult] = None       # the routes and power stages simulated again (verify_scope)
    comparison: list[dict] = field(default_factory=list)
    fixed: list[Issue] = field(default_factory=list)
    new: list[Issue] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)  # routes / power stages not simulated again (baseline kept)
    solver: dict = field(default_factory=dict)     # gpu.Run.summary(): device, GPU fallbacks

    @property
    def selected(self) -> list[Suggestion]:
        """The suggestions the search selected."""
        return [s for s in self.suggestions if s.selected]


# --------------------------------------------------------------------------- placement rules
class PlacementChecker:
    """Where a via or a small capacitor may go: inside the outline, clear of other nets' fixed copper
    (tracks, pads, vias), not on top of own vias. Zones are not obstacles: they refill around new items."""

    def __init__(self, board: Board, copper: CopperMap, clearance: float = CLEARANCE_MM):
        """Index the board's fixed copper (tracks, vias, pads) per layer in spatial trees, with their nets."""
        self.board = board
        self.copper = copper
        self.clearance = clearance
        self.inside = copper.outline.buffer(-EDGE_MARGIN_MM) if not copper.outline.is_empty else None
        items: dict[str, list] = {}
        for t in board.tracks:
            items.setdefault(t.layer, []).append((track_shape(t), t.net, "track"))
        for v in board.vias:
            disc = Point(v.position).buffer(v.size / 2, quad_segs=8)
            for l in v.layers:
                items.setdefault(l, []).append((disc, v.net, "via"))
        for p in board.pads():
            shape = pad_shape(p)
            for l in p.layers:
                items.setdefault(l, []).append((shape, p.net, "pad"))
        self.items = items
        self.trees = {l: STRtree([g for g, _, _ in its]) for l, its in items.items()}

    def blocked(self, at, net: str, radius: float, layers: list[str]) -> Optional[str]:
        """Reason a copper disc of `radius` at `at` on `layers` is not allowed, or None."""
        p = Point(at)
        if self.inside is not None and not self.inside.contains(p):
            return "too close to the board edge"
        disc = p.buffer(radius + self.clearance)
        own = p.buffer(radius * 2)
        for l in layers:
            tree = self.trees.get(l)
            if tree is None:
                continue
            for k in tree.query(disc, predicate="intersects"):
                g, n, kind = self.items[l][k]
                if n != net:
                    return f"clearance to {n or 'unconnected'} {kind} on {l}"
                if kind == "via" and g.intersects(own):
                    return f"on top of an existing {n} via"
        return None

    def on_plane(self, layer: str, net: str, at, radius: float = 0.3) -> bool:
        """Whether a disc of `radius` at `at` touches `net`'s plane copper on `layer` (a via there would join it)."""
        return self.copper.plane(layer, net).intersects(Point(at).buffer(radius))


def _ring(center, radii, n=ANGLES):
    """Candidate points on circles around `center`: n evenly spaced on each radius, nearest radius first."""
    for r in radii:
        for k in range(n):
            a = 2 * math.pi * k / n
            yield (center[0] + r * math.cos(a), center[1] + r * math.sin(a))


def _spread(points: list, keep: int, min_sep: float) -> list:
    """Up to `keep` of the points (in their order: the best first), each at least min_sep from those kept, so the
    candidates are different places rather than neighbours."""
    out = []
    for p in points:
        if all(math.dist(p, q) >= min_sep for q in out):
            out.append(p)
        if len(out) >= keep:
            break
    return out


# --------------------------------------------------------------------------- candidate generation
def via_candidates(checker: PlacementChecker, board: Board, at, net: str, planes: list[str],
                   keep: int = 3, size: float = 0.6, drill: float = 0.3) -> list[Modification]:
    """Through vias of `net` near `at` that land in that net's plane on every layer in `planes`."""
    span = list(board.copper_layers)
    ok = []
    for p in _ring(at, VIA_RADII):
        if checker.blocked(p, net, size / 2, span):
            continue
        if not all(checker.on_plane(l, net, p) for l in planes):
            continue
        ok.append(p)
    ok.sort(key=lambda p: math.dist(p, at))
    return [Modification("via", (round(p[0], 3), round(p[1], 3)), net=net, size=size, drill=drill,
                         source="suggested") for p in _spread(ok, keep, 0.8)]


def cap_candidates(checker: PlacementChecker, board: Board, at, nets: tuple[str, str], planes: tuple[str, str],
                   layer: str, keep: int = 3, C: float = 100e-9, ESL: float = 0.4e-9) -> list[Modification]:
    """0402-size capacitors between two plane nets near `at`, each pad with a via down to its net's plane."""
    out = []
    for p in _ring(at, CAP_RADII):
        for angle in (0.0, 90.0):
            m = Modification("cap", (round(p[0], 3), round(p[1], 3)), nets=nets, layer=layer, angle=angle, C=C,
                             ESL=ESL, source="suggested")
            pads = cap_pads(m)
            if any(checker.blocked(q, n, 0.3, list(board.copper_layers)) for q, n in zip(pads, nets)):
                continue
            if not all(checker.on_plane(pl, n, q) for q, n, pl in zip(pads, nets, planes)):
                continue
            out.append(m)
            break
    out.sort(key=lambda m: math.dist(m.at, at))
    picked = _spread([m.at for m in out], keep, 1.2)
    return [m for m in out if m.at in picked]


def fill_candidate(checker: PlacementChecker, copper: CopperMap, issue: Issue, track, layer: str,
                   net: str, h: float) -> Optional[Modification]:
    """Copper on `layer` (net) bridging a slot under the trace, clear of other nets' fixed copper."""
    (x0, y0), (x1, y1) = track.start, track.end
    L = math.hypot(x1 - x0, y1 - y0)
    if L == 0:
        return None
    ux, uy = (x1 - x0) / L, (y1 - y0) / L
    cx, cy = issue.location
    half_along = issue.length_mm / 2 + max(1.0, 2 * h)
    half_across = max(1.0, 3 * h)
    corners = [(cx + sa * half_along * ux - sc * half_across * uy, cy + sa * half_along * uy + sc * half_across * ux)
               for sa, sc in ((-1, -1), (1, -1), (1, 1), (-1, 1))]

    rect = Polygon(corners)
    obstacles = [g.buffer(checker.clearance) for g, n, _ in checker.items.get(layer, []) if n != net and g.intersects(rect)]
    piece = rect.difference(unary_union(obstacles)) if obstacles else rect
    if piece.is_empty:
        return None
    if piece.geom_type != "Polygon":   # obstacles split the rectangle: keep the part under the crossing
        piece = min(piece.geoms, key=lambda g: g.distance(Point(cx, cy)))
    plane = copper.plane(layer, net)
    a = Point(cx - (issue.length_mm / 2 + 0.3) * ux, cy - (issue.length_mm / 2 + 0.3) * uy)
    b = Point(cx + (issue.length_mm / 2 + 0.3) * ux, cy + (issue.length_mm / 2 + 0.3) * uy)
    if not (plane.distance(a) < 0.2 and plane.distance(b) < 0.2 and piece.distance(a) < 0.2 and piece.distance(b) < 0.2):
        return None
    pts = [(round(x, 3), round(y, 3)) for x, y in list(piece.simplify(0.02).exterior.coords)[:-1]]
    return Modification("fill", layer=layer, net=net, polygon=pts, source="suggested")


ADVICE = {
    "off_plane": "Route this part of the trace over continuous plane copper, or add {ref} copper under it on {layer}.",
    "edge_proximity": "Move the trace further from the plane edge (≥ 3×h) or extend the plane under it.",
    "reference_net": "The return flows on the {net} plane here: decouple {net} to GND close to where the trace "
                     "enters and leaves this plane, or reference the trace to GND.",
    "return_path_broken": "Give the return a conductive path between the return pins: a stitching via, a 0R or a "
                          "capacitor across the split.",
    "plane_resonance": "Damp or move the plane resonance: decoupling capacitors between the resonating planes near "
                       "the signal's layer change, or GND stitching vias along the board edge.",
    "emi_margin": "Shrink the loop: stitch the return where the trace changes layers, keep the trace over one "
                  "continuous plane, or slow the edges with a series resistor at the driver.",
}


# --------------------------------------------------------------------------- evaluation
def modification_branches(model: BoardModel, m: Modification, st) -> list[Branch]:
    """The branches a what-if via or capacitor adds to a model (empty if it connects nothing there)."""
    if m.type == "via":
        return vertical_branches(st, m.net, m.at, list(model.board.copper_layers), m.drill,
                                 f"what-if via {m.net} @({m.at[0]:.2f}, {m.at[1]:.2f})")
    if m.type == "cap":
        ends = []
        for pos, net in zip(cap_pads(m), m.nets):
            order = sorted(st.names, key=lambda n: abs(model.z_phys[n] - model.z_phys[m.layer]))
            node = next((n for n in (st.node(l, *pos, net, max(1.5 * st.d, 0.5)) for l in order) if n), None)
            if node is None:
                return []
            ends.append(node)
        from ..components.models import ComponentModel
        cap = part_impedance(ComponentModel("cap", {"C": m.C, "ESR": m.ESR, "ESL": m.ESL}))
        vias = [via_impedance(abs(model.z_phys[m.layer] - st.z[e[0]]) + 1e-6, 0.25e-3) for e in ends]
        z = (lambda f, c=cap, v=vias: c(f) + sum(vv(f) for vv in v))
        return [Branch(ends[0], ends[1], z, f"what-if cap {m.nets[0]}-{m.nets[1]}", "part")]
    return []


class _Evaluator:
    """Predict a candidate's effect on one route as low-rank updates of the baseline factorisations, started from
    the baseline solutions: loop L on the route model at f0, and the loop moment at the worst harmonic. With
    plane capacitance that moment is m_route(f0) + [m_board(fw) - m_board(f0)], the same combination the
    simulation uses, so a fix that damps a plane resonance is credited at the resonance."""

    def __init__(self, model: BoardModel, setup: RouteSetup):
        """The baseline solutions the candidates start from: the route model at f0 (and at the worst harmonic
        without a board-wide model), the board-wide model at the solved harmonics nearest f0 and the worst."""
        self.model = model
        self.setup = setup
        self.f0 = f0 = setup.f_ref or setup.spec.freq
        fine, cav = setup.fine, setup.cavity
        self.base_fine = {f0: fine.sols.get(f0) or fine.exc.solve(f0)}
        solved = setup.spectrum_run.ok()
        target = setup.res.worst["f"] if setup.res.worst else f0
        self.fw = min(solved, key=lambda f: abs(math.log(f / target))) if solved else f0
        self.base_cav: dict = {}
        self.c0 = f0
        if cav is not None and solved:
            self.c0 = min(solved, key=lambda f: abs(math.log(f / f0)))
            self.base_cav = {f: solved[f] for f in (self.c0, self.fw)}
        elif self.fw not in self.base_fine:
            self.base_fine[self.fw] = solved.get(self.fw) or fine.exc.solve(self.fw)

    def branches_for(self, m: Modification, st) -> list[Branch]:
        """The model branches an edit adds on build st (a via's barrel pieces, a capacitor's branch)."""
        return modification_branches(self.model, m, st)

    @staticmethod
    def _solve(exc, branches: list[Branch], base: dict) -> dict:
        """The loop solved with the extra branches at each frequency of `base`: on a derived stack (the base's
        factorisation plus a Woodbury update), warm-started from the baseline solution."""
        stack = exc.stack.with_branches(branches)
        new = exc.on(stack)
        # the baseline solution (no current in the new branches yet) is a close starting point
        return {f: new.solve(f, guess=[stack.map_state(sol.state_for(exc.stack), exc.stack)])
                for f, sol in base.items()}

    def evaluate(self, m: Modification) -> Optional[tuple[float, float]]:
        """(Δ loop moment dB at the worst harmonic, relative Δ loop L at f0), or None if the candidate does not
        connect anything in the route model."""
        if m.type == "fill":
            return self._evaluate_rebuilt(m)
        br = self.branches_for(m, self.setup.fine.build)
        brc = self.branches_for(m, self.setup.cavity.build) if self.base_cav else []
        if not br:
            return None
        try:
            fine = self._solve(self.setup.fine.exc, br, self.base_fine)
            cav = self._solve(self.setup.cavity.exc, brc, self.base_cav) if brc else self.base_cav
        except SolverError:
            return None
        return self._deltas(fine, cav)

    def _evaluate_rebuilt(self, m: Modification) -> Optional[tuple[float, float]]:
        """A fill changes the copper, so the route model is rebuilt (inductive only; the board-wide part of the
        moment is kept from the baseline)."""
        from .simulate import prepare_route
        board, cfg = apply_modifications(self.model.board, self.model.config, [m])
        cfg = copy.copy(cfg)
        cfg.analysis = copy.copy(cfg.analysis)
        cfg.analysis.plane_capacitance = False
        model = BoardModel(board, cfg)
        setup, _, _ = prepare_route(model, self.setup.spec, self.setup.route)
        if setup is None:
            return None
        try:
            fine = {f: setup.fine.exc.solve(f) for f in self.base_fine}
        except SolverError:
            return None
        return self._deltas(fine, self.base_cav)

    def _moment(self, fine: dict, cav: dict) -> np.ndarray:
        """The loop moment at the worst harmonic: m_route(f0) + [m_board(fw) - m_board(c0)] with the board-wide
        model (as band_moments), else the route model's at fw."""
        if cav:
            return fine[self.f0].m + cav[self.fw].m - cav[self.c0].m
        return fine[self.fw].m

    def _deltas(self, fine: dict, cav: dict) -> tuple[float, float]:
        """(moment change in dB, relative loop-inductance change at f0) of a candidate against the baseline."""
        mb = float(np.linalg.norm(self._moment(self.base_fine, self.base_cav)))
        mn = float(np.linalg.norm(self._moment(fine, cav)))
        d_db = 20 * math.log10(mn / mb) if mb > 0 and mn > 0 else 0.0
        lb, ln = self.base_fine[self.f0].L_loop, fine[self.f0].L_loop
        return d_db, (ln - lb) / lb if lb > 0 else 0.0


def gap_midpoints(setup: RouteSetup, config: Config) -> list[float]:
    """Harmonics at the log-midpoints of the gaps (wider than GAP_RATIO) next to the solved harmonics within
    REFINE_MARGIN_DB of the warning level. The route's refinement stops at max_solves and can leave a resonance
    inside such a gap: on the real board SPI_MOSI's worst margin was +12.8 dB at 780 MHz, between its solves at
    660 and 820 MHz (reported: +14.9 dB at 820), and the selected fixes moved it to 740 MHz."""
    from .simulate import REFINE_MARGIN_DB, _margin_curve, band_moments
    if setup.cavity is None or setup.currents is None:
        return []
    fs, ms = band_moments(setup)
    if len(fs) < 2:
        return []
    curve = _margin_curve(setup, config, fs, ms)
    H = setup.harmonics
    floor = config.rules.emi_margin_db + REFINE_MARGIN_DB
    near = {f for f in fs if curve[int(np.argmin(np.abs(H - f)))] <= floor}      # NaN (no limit): False
    out = []
    for fa, fb in zip(fs, fs[1:]):
        if fb / fa < GAP_RATIO or not ({fa, fb} & near):
            continue
        inside = H[(fa * 1.0001 < H) & (fb * 0.9999 > H)]
        if len(inside):
            out.append(float(inside[int(np.argmin(np.abs(np.log(inside) - 0.5 * math.log(fa * fb))))]))
    return out


class _WorstCheck:
    """One selected fix on one route: the route's worst emission margin over its whole spectrum, before and with
    the fix alone. A via or capacitor can move a plane resonance onto a harmonic that was not the worst before
    (on the real board a stitching via for SPI_MOSI moved one onto SPI_SCK's 660 MHz), which the prediction at
    the old worst harmonic misses. The board-wide model is re-solved with the fix (a low-rank update started from
    the baseline solution) at every solved harmonic whose estimate is within REFINE_MARGIN_DB of the warning
    level, and the route model at f0; the other harmonics keep their board-wide baseline. `extra` are baseline
    solutions at gap midpoints (gap_midpoints), solved once per route before the checks: both the before and the
    after curve include them. A route with no harmonic that close is not checked (as in the refinement: so far
    below the limit, a change cannot set a warning). The solves are separate jobs, so all checks can run in one
    frequency-sorted parallel batch."""

    def __init__(self, ev: _Evaluator, m: Modification, config: Config, stacks: dict,
                 extra: Optional[dict] = None):
        """Plan the check's solves (jobs): the route model at f0 and the board-wide model at every harmonic within
        REFINE_MARGIN_DB of the warning level. No jobs for a fill, a route without a board-wide model, or one with
        no harmonic that close."""
        from .simulate import REFINE_MARGIN_DB
        self.ev, self.config = ev, config
        self.jobs: list[tuple[str, float]] = []
        self.sols: dict = {}
        self.failed = False
        setup = ev.setup
        if setup.cavity is None or setup.currents is None or m.type == "fill":
            return
        self.solved = {**setup.spectrum_run.ok(), **(extra or {})}
        if ev.c0 not in self.solved or len(self.solved) < 2:
            return
        self.fs = sorted(self.solved)
        self.base = self._curve(ev.base_fine[ev.f0], self.solved)
        floor = config.rules.emi_margin_db + REFINE_MARGIN_DB
        H = setup.harmonics
        near = [f for f in self.fs if self.base[int(np.argmin(np.abs(H - f)))] <= floor]   # NaN (no limit): False
        if not near:
            return
        self.fine = self._derived(setup.fine, ev.branches_for(m, setup.fine.build), stacks)
        self.cav = self._derived(setup.cavity, ev.branches_for(m, setup.cavity.build), stacks)
        if self.fine is not None:
            self.jobs.append(("fine", ev.f0))
        if self.cav is not None:
            self.jobs += [("cav", f) for f in sorted(set(near) | {ev.c0})]

    @staticmethod
    def _derived(run, branches: list[Branch], stacks: dict):
        """The run's loop on its stack plus the fix's branches, or None when the fix touches nothing there. One
        derived stack per fix and model, shared by the routes on that model (with its cached operators)."""
        if not branches:
            return None
        base = run.exc.stack
        if id(base) not in stacks:
            stacks[id(base)] = base.with_branches(branches)
        new = stacks[id(base)]
        return None if new is base else run.exc.on(new)

    def solve(self, job: tuple[str, float]):
        """One planned solve (a ("fine" | "cav", f) job) of the route with the fix, warm-started from the baseline;
        a failure marks the check failed."""
        kind, f = job
        run, exc = (self.ev.setup.fine, self.fine) if kind == "fine" else (self.ev.setup.cavity, self.cav)
        base = self.ev.base_fine[f] if kind == "fine" else self.solved[f]
        try:
            self.sols[job] = exc.solve(f, guess=[exc.stack.map_state(base.state_for(run.exc.stack), run.exc.stack)])
        except SolverError:
            self.failed = True

    def _curve(self, fine0, cav: dict) -> np.ndarray:
        """Margin per harmonic from the moments m(f) = m_fine(f0) + [m_board(f) - m_board(c0)] (as band_moments)."""
        from .simulate import _margin_curve
        offset = fine0.m - cav[self.ev.c0].m
        ms = np.array([float(np.linalg.norm(cav[f].m + offset)) for f in self.fs])
        return _margin_curve(self.ev.setup, self.config, self.fs, ms)

    def result(self) -> Optional[dict]:
        """{"before_db", "after_db", "f"}: the worst margin before, with the fix, and the harmonic of the latter;
        None when not checked or a solve failed."""
        if not self.jobs or self.failed or len(self.sols) < len(self.jobs):
            return None
        ev = self.ev
        fine0 = self.sols.get(("fine", ev.f0), ev.base_fine[ev.f0])
        cav = dict(self.solved)
        cav.update({f: sol for (kind, f), sol in self.sols.items() if kind == "cav"})
        new = self._curve(fine0, cav)
        if not np.isfinite(new).any():
            return None
        k = int(np.nanargmin(new))
        return {"before_db": float(np.nanmin(self.base)), "after_db": float(new[k]),
                "f": float(ev.setup.harmonics[k])}


def harmonic_verdict(own: Optional[dict], others: dict[str, dict], delta_db: Optional[float],
                     warn_db: float) -> tuple[bool, str]:
    """(keep the fix, note) from its predicted worst margins: own = its own route's, others = {net: ...} of
    the other routes near the limit (see _WorstCheck.result). A fix is dropped when it lowers its own route's
    worst margin by more than MAX_HARM_DB, or another route's by more than that and either more than the fix
    gains on its own route or below the warning level. Smaller trade-offs are kept and noted."""
    if own is not None and own["before_db"] - own["after_db"] > MAX_HARM_DB:
        return False, (f"not selected: moves the worst margin {own['before_db']:+.1f} → {own['after_db']:+.1f} dB "
                       f"(at {own['f'] / 1e6:.4g} MHz)")
    gain = own["after_db"] - own["before_db"] if own is not None else -(delta_db or 0.0)
    notes = [f"worst margin {own['before_db']:+.1f} → {own['after_db']:+.1f} dB"] if own is not None else []
    for net, r in others.items():
        loss = r["before_db"] - r["after_db"]
        if loss <= MAX_HARM_DB:
            continue
        what = f"{net}'s worst margin {r['before_db']:+.1f} → {r['after_db']:+.1f} dB (at {r['f'] / 1e6:.4g} MHz)"
        if loss > gain or r["after_db"] < warn_db:
            return False, f"not selected: lowers {what}"
        notes.append(f"also lowers {what}")
    return True, "; ".join(notes)


# --------------------------------------------------------------------------- main entry
def _size(run) -> float:
    """A solve's relative cost on this model (simulate.solve_cost; 0 for no model): the progress plan's unit."""
    return solve_cost(run) if run is not None else 0.0


def _cost(result: SimulationResult, names=None) -> float:
    """What simulating these routes / power stages (default: all) again costs, from the baseline's solves."""
    total = 0.0
    for s in result.setups + result.converter_setups:
        if names is None or s.spec.name in names:
            for run in (s.fine, s.cavity, *getattr(s, "extra_runs", ())):
                if run is not None:
                    total += len(run.sols) * _size(run)
    return total


def _simulate_estimate(config: Config, nets: Optional[list[str]]) -> float:
    """A simulation's cost before anything is known (progress plan): the planned solves at a typical size."""
    a = config.analysis
    per = a.solve_points + a.max_solves
    routes = [n for n in config.nets if not nets or n.name in nets]
    convs = [c for c in config.converters if not nets or c.name in nets]
    return len(routes) * per + len(convs) * (per + 10) * 1.5


WARM = 0.6                  # a solve started from the baseline solution: fewer iterations (progress plan)


def verify_scope(before: SimulationResult, selected: list[Suggestion], config: Config) -> tuple[list[str], list[str]]:
    """(names simulated again, names kept from the baseline) to verify the selected fixes together.

    Simulated again: every route and power stage with a selected fix; every route whose worst margin is within
    REFINE_MARGIN_DB of the warning level (a via or capacitor anywhere can move a plane resonance onto one of its
    harmonics, see _WorstCheck); every power stage whose model region contains a selected fix. Kept: routes so far
    below the limit that no change can set a warning there (the refinement's own rule) and power stages no fix
    touches. The full simulation of every route and stage made the final step of the search as long as a whole
    Simulate, with the bar standing still meanwhile."""
    from .simulate import REFINE_MARGIN_DB
    floor = config.rules.emi_margin_db + REFINE_MARGIN_DB
    fixed = {s.net for s in selected}
    spots = [(s.modification.at if s.modification is not None and s.modification.at else s.location)
             for s in selected]
    scope = []
    for r in before.routes:
        margin = (r.worst or {}).get("margin_db")
        if r.net in fixed or (margin is not None and margin <= floor):
            scope.append(r.net)
    for cs in before.converter_setups:
        x0, y0, x1, y1 = cs.fine.build.bbox
        if cs.spec.name in fixed or any(x0 <= p[0] <= x1 and y0 <= p[1] <= y1 for p in spots):
            scope.append(cs.spec.name)
    scope = list(dict.fromkeys(scope))
    names = [r.net for r in before.routes] + [c.net for c in before.converters] + [i.net for i in before.issues]
    return scope, [n for n in dict.fromkeys(names) if n and n not in scope]


def suggest_fixes(board: Board, config: Config, nets: Optional[list[str]] = None, progress: Optional[Progress] = None,
                  max_fixes: int = 3, candidates: int = 3, verify: bool = True,
                  baseline: Optional[SimulationResult] = None, verify_all: bool = False) -> SuggestResult:
    """verify_all: simulate every route and power stage again to verify the selected fixes (default: only what
    they can change, verify_scope). The solves run on a GPU per `analysis.gpu`; `SuggestResult.solver` says
    where. Rows switched off (enabled: false) are left out unless named (Config.active)."""
    config = config.active(nets)
    with gpu.use(getattr(config.analysis, "gpu", "auto")) as run:
        out = _suggest_fixes(board, config, nets, progress, max_fixes, candidates, verify, baseline, verify_all)
    out.solver = run.summary()
    return out


def _suggest_fixes(board: Board, config: Config, nets: Optional[list[str]], progress: Optional[Progress],
                   max_fixes: int, candidates: int, verify: bool, baseline: Optional[SimulationResult],
                   verify_all: bool) -> SuggestResult:
    """suggest_fixes' work, phase by phase (see _FixSearch)."""
    search = _FixSearch(board, config, nets, Phases(progress), max_fixes, candidates, baseline, verify, verify_all)
    for k, issue in enumerate(search.pending):
        search.place_route_candidates(k, issue)
    search.place_power_candidates()
    search.evaluate()
    search.pick_power_fixes()
    search.pick_route_fixes()
    search.check_at_every_harmonic()
    search.number()
    if verify and search.out.selected:
        search.verify()
    search.ph.start(0.0)
    search.ph.report("done", 1.0)
    return search.out


class _FixSearch:
    """The fix search, phase by phase: candidates for every issue (stitching vias, capacitors, fills, advice) and
    for the power stages' hot loops; each candidate evaluated alone; the best per issue and route (greedy for the
    power stages, so redundant vias are not all picked); the chosen ones checked at every harmonic near the limit;
    then all of them simulated together."""

    def __init__(self, board: Board, config: Config, nets: Optional[list[str]], ph: Phases, max_fixes: int,
                 candidates: int, baseline: Optional[SimulationResult], verify: bool, verify_all: bool):
        """Set up the search: the baseline (simulated here, its factorisations kept, unless one is given), the
        placement checker, the routes and stages, the issues to work on, and a first estimate of its cost for
        the progress bar."""
        self.board, self.config, self.nets, self.ph = board, config, nets, ph
        self.max_fixes, self.candidates, self.verify_wanted, self.verify_all = max_fixes, candidates, verify, verify_all
        self.out = SuggestResult()
        if baseline is None:            # about one simulation, then the search (a guess: half as much again)
            est = _simulate_estimate(config, nets)
            ph.start(est, rest=0.5 * est)
            baseline = simulate(board, config, nets, progress=ph.nested("baseline: "),
                                keep_factorisations=True)     # the candidates are low-rank updates of them
        self.before = self.out.before = baseline
        self.model = baseline.model or BoardModel(board, config)
        self.checker = PlacementChecker(board, self.model.copper)
        self.tracks = {t.uuid: t for t in board.tracks}
        self.setups = {(s.spec.name, s.route.receiver.pin): s for s in baseline.setups}
        self.net_setup: dict[str, RouteSetup] = {}          # a net's first route
        for (net, _), s in self.setups.items():
            self.net_setup.setdefault(net, s)
        # power stages, motor bridges' legs and the bridges themselves: their issues get power candidates
        self.converter_names = {c.net for c in baseline.converters} | {b.net for b in baseline.bridges}
        self.pending = [i for i in baseline.issues if not i.waived and i.net not in self.converter_names]
        self.threads = solver_threads(config)
        self.evaluators: dict = {}
        self.power_evaluators: dict = {}
        self.n_sel = 0
        # what the rest of the search will roughly cost, before the candidates are known: a few evaluations per
        # issue, the checks of the selected fixes at the harmonics near the limit, and the verification
        self.cav = max((_size(s.cavity) for s in baseline.setups if s.cavity is not None), default=0.0)
        n_fix = (min(len(self.pending), max_fixes * max(len(baseline.setups), 1))
                 + len(baseline.converter_setups))
        rest = (len(self.pending) * candidates * 2 * self.cav * WARM
                + n_fix * len(baseline.setups) * config.analysis.max_solves * 0.5 * self.cav * WARM + _cost(baseline))
        ph.start(0.02 * rest, rest=rest)      # the candidates: geometry, quick

    # ------------------------------------------------------------------ candidates
    def add(self, net, route_label, issue, kind, title, message, loc, layer="", mod=None, note="") -> Suggestion:
        """Add a suggestion (numbered S1, S2 ... in the order found)."""
        s = Suggestion(f"S{len(self.out.suggestions) + 1}", net, route_label, issue, kind, title, message, loc,
                       layer, mod, note=note)
        self.out.suggestions.append(s)
        return s

    def unseen(self, items, mod_of=lambda x: x):
        """The items whose modification is not suggested yet (the same stitching via for two vias of a route, or
        for two issues, is suggested once)."""
        have = {x.modification.key() for x in self.out.suggestions if x.modification is not None}
        for item in items:
            key = mod_of(item).key()
            if key not in have:
                have.add(key)
                yield item

    def advice(self, issue: Issue) -> str:
        """The advice text for an issue that has no placeable fix (ADVICE, filled in)."""
        return ADVICE[issue.check].format(net=issue.details.get("plane_net", "this"), layer=issue.layer,
                                          ref="/".join(self.config.reference_nets))

    def place_route_candidates(self, k: int, issue: Issue):
        """Add the candidates for the k-th pending issue (new edits only, see unseen)."""
        n = max(len(self.pending), 1)
        self.ph.report(f"placing candidates: {issue.net}, {issue.check} ({k + 1} of {n})", k / n)
        setup = self.net_setup.get(issue.net)
        route_label = f"{setup.route.driver.pin}->{setup.route.receiver.pin}" if setup else ""
        for m in self.unseen(self.route_candidates(issue, setup, route_label)):
            self.add(issue.net, route_label, issue, m.type, _title(m), m.label(), m.at or issue.location,
                     m.layer or issue.layer, m)

    def route_candidates(self, issue: Issue, setup, route_label: str) -> list[Modification]:
        """The modifications that could fix an issue; where there is none, advice."""
        board, checker, n_try = self.board, self.checker, self.candidates
        mods: list[Modification] = []

        def advise(title, message):
            """Add advice for this issue instead of an edit."""
            self.add(issue.net, route_label, issue, "advice", title, message, issue.location, issue.layer)
        if issue.check == "layer_change_stitch":
            d = issue.details
            mods = via_candidates(checker, board, issue.location, d["plane_net"], [d["from_plane"], d["to_plane"]],
                                  n_try)
            if not mods:
                advise("No room for a stitching via",
                       f"No legal spot for a {d['plane_net']} via within {VIA_RADII[-1]} mm of the signal via; "
                       f"consider moving the signal via next to an existing {d['plane_net']} via.")
        elif issue.check in ("layer_change_decap", "gap_crossing"):
            if issue.check == "gap_crossing":
                nets_, planes = _gap_sides(self.model.copper, issue, self.tracks)
            else:
                nets_ = tuple(issue.details.get("nets") or ())
                planes = _planes_of(self.model.copper, board, issue.location, nets_)
            if len(nets_) == 2 and nets_[0] != nets_[1]:
                if len(planes) == 2:
                    mods = cap_candidates(checker, board, issue.location, nets_, planes,
                                          _outer_layer(board, issue.layer), n_try)
                if not mods:
                    advise("No room for a capacitor",
                           f"No legal spot for a {nets_[0]}–{nets_[1]} capacitor within {CAP_RADII[-1]} mm; route the "
                           f"trace so that it keeps one reference net, or move parts to make room.")
            elif issue.check == "gap_crossing" and len(nets_) == 2:     # same-net slot: bridge it with copper
                track = self.tracks.get(issue.items[0]) if issue.items else None
                ref = (issue.details.get("reference_layers") or [None])[0]
                if track is not None and ref:
                    fill = fill_candidate(checker, self.model.copper, issue, track, ref, nets_[0],
                                          issue.details.get("height_mm", 0.2))
                    mods = [fill] if fill else []
                if not mods:
                    advise("Slot cannot be filled", "Other copper runs through the gap under the trace; move that "
                                                    "copper or cross the gap elsewhere.")
            else:
                advise("Cross the gap elsewhere" if issue.check == "gap_crossing" else "Decouple the reference change",
                       "The plane copper on both sides could not be identified here; keep the trace over one "
                       "continuous plane, or bridge the planes with a capacitor next to the crossing.")
        elif issue.check in ("return_detour", "plane_resonance", "emi_margin") and setup is not None:
            for v in setup.route.vias:          # stitch where the return has to change planes
                refs = [r[0] for r in reference_layers(board, v.from_layer) + reference_layers(board, v.to_layer)]
                for net in board_nets_at(self.model.copper, refs, v.position, self.config.reference_nets):
                    mods += via_candidates(checker, board, v.position, net, list(dict.fromkeys(refs))[:2], 1)
            if issue.check != "return_detour":
                advise(issue.title, self.advice(issue))
        elif ADVICE.get(issue.check):
            advise(issue.title, self.advice(issue))
        return mods

    def place_power_candidates(self):
        """Stitching vias at the hot loop's pads and small capacitors at the switches, per power stage."""
        board, checker, model, config = self.board, self.checker, self.model, self.config
        for cs in self.before.converter_setups:
            c = cs.res
            if c.error:
                continue
            issue = next((i for i in self.before.issues if i.net in (c.net, getattr(c, "bridge", None))
                          and not i.waived and i.check in POWER_ISSUES), None)     # a leg's, or its bridge's
            self.ph.report(f"placing candidates: {c.net}, hot loop", 1.0)
            self.power_evaluators[c.net] = _PowerEvaluator(model, cs)
            for pad, m in self.unseen(power_via_candidates(checker, board, model, cs, config), lambda pm: pm[1]):
                self.add(c.net, "hot loop", issue, "via", _title(m),
                         f"{m.label()}: {pad.pin} ({pad.net}) has no {pad.net} via within "
                         f"{config.rules.stitch_distance_mm:g} mm", m.at, "", m)
            for m in self.unseen(power_cap_candidates(checker, board, model, cs)):
                self.add(c.net, "hot loop", issue, "cap", _title(m),
                         f"{m.label()}: a small capacitor right at the switches ({', '.join(c.parts)}) closes the "
                         f"hot loop there; use a part rated for the rail voltage", m.at, m.layer, m)

    # ------------------------------------------------------------------ evaluation and choice
    def evaluate(self):
        """Each candidate on its route's models at the reference frequency and the worst harmonic. The candidates
        are independent (each is a low-rank update of the shared, thread-safe factorisations): they run in
        parallel."""
        before, ph = self.before, self.ph
        for st in before.setups:
            self.evaluators[id(st)] = _Evaluator(self.model, st)
        todo = [s for s in self.out.suggestions if s.modification is not None]

        def cost(s) -> float:
            """A candidate's estimated cost: its solves' model sizes (for the progress bar)."""
            # a candidate's solves start from the baseline solution but needed about as many iterations (real
            # board: 59 per solve vs 64), and often a factorisation the cache no longer holds
            if s.net in self.power_evaluators:
                return _size(self.power_evaluators[s.net].setup.fine)
            st = self.net_setup.get(s.net)
            if st is None:
                return 0.0
            ev = self.evaluators[id(st)]
            fine = _size(st.fine) * len(ev.base_fine) * (3.0 if s.modification.type == "fill" else 1.0)
            return fine + _size(st.cavity) * len(ev.base_cav) * 1.5
        # The rest: the greedy rounds of the power stages, the checks at the harmonics near the limit and the
        # verification. They only run when something is selected, so they count half (on the real board three
        # SW_CLK candidates were all rejected, and the bar then jumped from 10 % to the end).
        self.n_sel = min(len({s.issue.fingerprint for s in todo if s.issue is not None}),
                         self.max_fixes * max(len(self.setups), 1))
        rest = 0.5 * (sum(_size(ev.setup.fine) * GREEDY_POOL * (self.max_fixes - 1)
                          for ev in self.power_evaluators.values()) + self.checks_guess() + _cost(before))
        ph.start(sum(cost(s) for s in todo), rest=rest, steps=len(todo))

        def evaluate(s):
            """Evaluate one candidate as a progress step."""
            with ph.step(f"evaluating candidates: {s.net}, {s.title.lower()}"):
                self.evaluate_one(s)
        run_parallel(todo, evaluate, self.threads)

    def checks_guess(self) -> float:
        """The checks' estimated cost, before the selected fixes are known (for the progress bar)."""
        return self.n_sel * len(self.before.setups) * self.config.analysis.max_solves * 0.5 * self.cav * WARM

    def evaluate_one(self, s: Suggestion):
        """Predict one candidate's effect (a power stage's or a route's evaluator) into s.delta_db / s.delta_l;
        a note when it joins nothing in the model."""
        power = s.net in self.power_evaluators
        if power:
            ev = self.power_evaluators[s.net]
        else:
            setup = self.net_setup.get(s.net)
            if setup is None:
                return
            ev = self.evaluators[id(setup)]
        r = ev.evaluate(s.modification)
        if r is None:
            s.note = ("does not join two layers of the net in the model" if power
                      else "does not connect two planes in the model")
            return
        s.delta_db, s.delta_l = r
        s.f_eval = ev.f if power else ev.fw

    def pick_power_fixes(self):
        """Greedy per power stage: the next fix is the one that adds most to those already picked, so redundant
        vias (the first one already closes the detour) are not all picked."""
        out, ph, max_fixes = self.out, self.ph, self.max_fixes
        pools = {name: sorted((x for x in out.suggestions if x.net == name and x.delta_l is not None
                               and x.delta_l <= -MIN_GAIN_L), key=lambda x: x.delta_l)[:GREEDY_POOL]
                 for name in self.power_evaluators}
        greedy = sum(len(p) * (min(max_fixes, len(p)) - 1) for p in pools.values())   # at most this many sets
        if greedy:
            cost = sum(_size(self.power_evaluators[n].setup.fine) * len(p) * (min(max_fixes, len(p)) - 1)
                       for n, p in pools.items())
            ph.start(cost, rest=self.checks_guess() + _cost(self.before), steps=greedy)
        for name, ev in self.power_evaluators.items():

            def evaluate_set(cands, ev=ev, name=name):
                """Predict a set of hot-loop fixes together, as a named progress step."""
                with ph.step(f"combining the best hot-loop fixes: {name}"):
                    return ev.evaluate_set([c.modification for c in cands])

            for k, (x, total) in enumerate(greedy_pick(pools[name], evaluate_set, max_fixes, MIN_GAIN_L,
                                                       self.threads)):
                x.selected = True
                if k:
                    x.note = f"with the fixes before it: L {total * 100:+.0f} % in total"
            for x in out.suggestions:
                if x.net == name and x.delta_l is not None and not x.selected and not x.note:
                    x.note = ("not selected: adds less than 1 % to the selected fixes" if x.delta_l <= -MIN_GAIN_L
                              else "not selected: no gain")

    def pick_route_fixes(self):
        """The best candidate per issue, then the best issues per route."""
        by_issue: dict[str, list[Suggestion]] = {}
        for s in self.out.suggestions:
            if s.modification is not None and s.issue is not None and s.net not in self.power_evaluators:
                by_issue.setdefault(s.issue.fingerprint, []).append(s)
        best = []
        for cands in by_issue.values():
            rated = [c for c in cands if c.delta_db is not None]
            if not rated:
                continue
            c = min(rated, key=lambda c: (c.delta_db, c.delta_l))
            tier1 = c.issue.check in ("layer_change_stitch", "layer_change_decap", "gap_crossing")
            if c.delta_db > MAX_HARM_DB:
                # e.g. a stitching via that moves a plane resonance onto the worst harmonic: it would satisfy the
                # rule, but the solver predicts more emission. Leave it to the user.
                for x in rated:
                    x.note = (f"not selected: predicted {x.delta_db:+.1f} dB at {x.f_eval / 1e6:.4g} MHz"
                              + (" (fixes the rule)" if tier1 else ""))
                continue
            if tier1 or c.delta_db <= -MIN_GAIN_DB or c.delta_l <= -MIN_GAIN_L:
                best.append(c)
        per_route: dict[str, int] = {}
        for c in sorted(best, key=lambda c: (c.delta_db, c.delta_l)):
            if per_route.get(c.net, 0) < self.max_fixes:
                c.selected = True
                per_route[c.net] = per_route.get(c.net, 0) + 1

    # ------------------------------------------------------------------ checks and verification
    def check_at_every_harmonic(self):
        """A via or capacitor changes the plane resonances of the whole board: each selected fix is checked at
        every harmonic near the limit of every route (a resonance can move onto a harmonic that was not the worst
        before), after solving the baseline between those harmonics (a resonance can hide there)."""
        before, config, ph, out, threads = self.before, self.config, self.ph, self.out, self.threads
        chosen = [c for c in out.selected if c.modification is not None and c.modification.type != "fill"
                  and c.net not in self.power_evaluators]
        routes = [st for st in before.setups if st.cavity is not None]
        gaps = sorted(((st, f) for st in routes for f in gap_midpoints(st, config)), key=lambda t: t[1]) if chosen else []
        extra: dict = {id(st): {} for st in routes}
        verify_cost = (_cost(before, None if self.verify_all else verify_scope(before, out.selected, config)[0])
                       if self.verify_wanted and out.selected else 0.0)
        jobs_guess = len(chosen) * len(routes) * config.analysis.max_solves * 0.5 * self.cav * WARM
        if gaps:
            ph.start(sum(_size(st.cavity) for st, _ in gaps), rest=jobs_guess + verify_cost, steps=len(gaps))

        def solve_gap(task):
            """Solve the baseline board-wide model at a gap midpoint (both before and after curves use it)."""
            st, f = task
            with ph.step(f"solving between the solved harmonics near the limit: {st.spec.name} at {f / 1e6:.4g} MHz"):
                try:
                    extra[id(st)][f] = st.cavity.exc.solve(f)
                except SolverError:
                    pass
        run_parallel(gaps, solve_gap, threads)
        checks = []
        for c in chosen:
            stacks: dict = {}            # one fix's modified stacks, shared by its checks on every route
            checks += [(c, st, _WorstCheck(self.evaluators[id(st)], c.modification, config, stacks, extra[id(st)]))
                       for st in routes]
        jobs = sorted(((w, job) for _, _, w in checks for job in w.jobs), key=lambda t: t[1][1])
        if jobs:
            ph.start(sum(_size(w.ev.setup.fine if kind == "fine" else w.ev.setup.cavity) * WARM
                         for w, (kind, _) in jobs), rest=verify_cost, steps=len(jobs))

            def solve_job(task):
                """Solve one (route, harmonic) job of the check, as a named progress step."""
                with ph.step(f"checking the selected fixes at every harmonic near the limit: "
                             f"{task[0].ev.setup.spec.name} at {task[1][1] / 1e6:.4g} MHz"):
                    task[0].solve(task[1])
            run_parallel(jobs, solve_job, threads)
        names = [st.spec.name for st in routes]

        def label(st):
            """A route's name: the net, with its receiver when the net has several routes."""
            return st.spec.name if names.count(st.spec.name) == 1 else f"{st.spec.name} → {st.route.receiver.pin}"
        results = [(fix, st, w.result()) for fix, st, w in checks]
        for c in chosen:
            own, others, worst = None, {}, {}
            for _, st, r in (x for x in results if x[0] is c and x[2] is not None):
                worst[label(st)] = r
                if st.spec.name == c.net and f"{st.route.driver.pin}->{st.route.receiver.pin}" == c.route:
                    own = r                     # the route the fix was evaluated on
                else:
                    others[label(st)] = r
            c.worst = worst or None
            keep, note = harmonic_verdict(own, others, c.delta_db, config.rules.emi_margin_db)
            if note:
                c.note = note
            c.selected = keep

    def number(self):
        """Number by usefulness: selected first, then by predicted gain."""
        self.out.suggestions.sort(key=lambda s: (not s.selected, s.kind == "advice",
                                                 s.delta_db if s.delta_db is not None else 0.0))
        for k, s in enumerate(self.out.suggestions, 1):
            s.id = f"S{k}"

    def verify(self):
        """Every selected fix on the board at once, simulated again; which issues they fix and which are new."""
        before, config, out, ph = self.before, self.config, self.out, self.ph
        b2, c2 = apply_modifications(self.board, config, [s.modification for s in out.selected])
        scope, out.kept = (self.nets, []) if self.verify_all else verify_scope(before, out.selected, config)
        names = scope if scope is not None else [r.net for r in before.routes] + [c.net for c in before.converters]
        n_align = sum(1 for st in before.setups if st.spec.name in names) * 4      # aligned_worst: ≤ 2 per run
        ph.start(_cost(before, names), rest=n_align * self.cav)
        out.after = simulate(b2, c2, scope, progress=ph.nested("verifying the selected fixes together: "))
        ph.start(n_align * self.cav)
        out.comparison = compare_results(before, out.after, progress=ph)
        before_issues = list(before.issues) if scope is None else [i for i in before.issues if i.net in scope]
        after_issues = list(out.after.issues)
        kept = [n for n in out.kept if n not in self.converter_names]
        if kept:                # the rule checks of the rest, on the modified board (geometry only, quick)
            rechecked = run_checks(b2, c2, out.after.model.copper, kept).issues
            waived = {w.fingerprint: w.reason for w in c2.waivers}
            for i in rechecked:
                if i.fingerprint in waived:
                    i.waived, i.waiver_reason = True, waived[i.fingerprint]
            after_issues += rechecked
            before_issues += [i for i in before.checks.issues if i.net in kept]
        out.fixed, out.new = issue_changes(before_issues, after_issues)


def _worst_with(setup: RouteSetup, config: Config, extra: dict) -> Optional[tuple[float, float]]:
    """(worst margin dB, its harmonic) of a route's emission estimate including `extra` spectrum solutions."""
    from .simulate import _margin_curve, band_moments
    if setup.currents is None:
        return None
    fs, ms = band_moments(setup, extra)
    if not fs:
        return None
    curve = _margin_curve(setup, config, fs, ms)
    if not np.isfinite(curve).any():
        return None
    k = int(np.nanargmin(curve))
    return float(curve[k]), float(setup.harmonics[k])


def aligned_worst(before: SimulationResult, after: SimulationResult, progress: Optional[Phases] = None) -> dict:
    """{(net, receiver): ((dB, f) before, (dB, f) after)}: each route's worst margin in both runs over the same
    harmonics. Each run refines its own spectrum (max_solves), so the two worst cases can sit at harmonics the
    other run never solved: on the real board SPI_MOSI read +14.9 dB @ 820 MHz → +11.0 dB @ 740 MHz, and the
    baseline had not solved 740 MHz (+13.9 dB there). Both runs are solved at both worst harmonics (at most two
    board-wide solves per run and route) before they are compared; the stored results are not changed."""
    if before.model is None or after.model is None:
        return {}
    pairs = []
    sa_by = {(st.spec.name, st.route.receiver.pin): st for st in after.setups}
    for sb in before.setups:
        sa = sa_by.get((sb.spec.name, sb.route.receiver.pin))
        if sa is None or sb.res.worst is None or sa.res.worst is None:
            continue
        pairs.append((sb, sa, {sb.res.worst["f"], sa.res.worst["f"]}))
    extra: dict = {id(st): {} for sb, sa, _ in pairs for st in (sb, sa)}
    tasks = [(st, f) for sb, sa, fs in pairs for st in (sb, sa) for f in sorted(fs)
             if f not in st.spectrum_run.ok()]
    if progress is not None:
        progress.expect(len(tasks))

    def solve(task):
        """Solve one route's spectrum model at one harmonic (a progress step when there is progress)."""
        st, f = task
        with (progress.step(f"comparing before and after at the same harmonics: {st.spec.name} at {f / 1e6:.4g} MHz")
              if progress is not None else nullcontext()):
            try:
                extra[id(st)][f] = st.spectrum_run.exc.solve(f)
            except SolverError:
                pass
    with gpu.use(getattr(before.model.config.analysis, "gpu", "auto")):
        run_parallel(sorted(tasks, key=lambda t: t[1]), solve, solver_threads(before.model.config))
    out = {}
    for sb, sa, _ in pairs:
        wb = _worst_with(sb, before.model.config, extra[id(sb)])
        wa = _worst_with(sa, after.model.config, extra[id(sa)])
        if wb is not None and wa is not None:
            out[(sb.spec.name, sb.route.receiver.pin)] = (wb, wa)
    return out


def compare_results(before: SimulationResult, after: SimulationResult, align: bool = True,
                    progress: Optional[Phases] = None) -> list[dict]:
    """Per route: loop inductance ratio, moment excess, worst margin and resonance before and after. Per
    converter (kind "converter"): hot-loop inductance, radiated and conducted worst margins. With `align`, the
    routes' worst margins are compared over the same harmonics (aligned_worst); "worst_margin_sampled_db" keeps
    each run's own value. Routes and stages missing from `after` (not simulated again) get no row."""
    aligned = aligned_worst(before, after, progress) if align else {}
    rows = []
    after_conv = {c.net: c for c in after.converters}
    for b in before.converters:
        a = after_conv.get(b.net)
        if a is None or b.error or a.error:
            continue
        rows.append({
            "kind": "converter", "net": b.net, "route": "hot loop", "loop_nh": (b.loop_nh, a.loop_nh),
            "l_ratio": (None, None), "m_excess_db": (None, None),
            "worst_margin_db": (b.worst["margin_db"] if b.worst else None, a.worst["margin_db"] if a.worst else None),
            "conducted_margin_db": (b.conducted_worst["margin_db"] if b.conducted_worst else None,
                                    a.conducted_worst["margin_db"] if a.conducted_worst else None),
            "resonance_db": ((b.resonance or {}).get("rise_db"), (a.resonance or {}).get("rise_db")),
        })
    after_routes = {(r.net, r.receiver): r for r in after.routes}
    for b in before.routes:
        a = after_routes.get((b.net, b.receiver))
        if a is None:
            continue
        sampled = (b.worst["margin_db"] if b.worst else None, a.worst["margin_db"] if a.worst else None)
        sampled_f = (b.worst["f"] if b.worst else None, a.worst["f"] if a.worst else None)
        al = aligned.get((b.net, b.receiver))
        rows.append({
            "net": b.net, "route": f"{b.driver}->{b.receiver}",
            "l_ratio": (b.l_ratio, a.l_ratio), "m_excess_db": (b.m_excess_db, a.m_excess_db),
            "worst_margin_db": (al[0][0], al[1][0]) if al else sampled,
            "worst_f": (al[0][1], al[1][1]) if al else sampled_f,
            "worst_margin_sampled_db": sampled, "aligned": al is not None,
            "resonance_db": ((b.resonance or {}).get("rise_db"), (a.resonance or {}).get("rise_db")),
        })
    return rows


def issue_changes(before: list[Issue], after: list[Issue]) -> tuple[list[Issue], list[Issue]]:
    """(fixed, new): unwaived issues that disappear / appear, matched by fingerprint."""
    fp_before = {i.fingerprint for i in before if not i.waived}
    fp_after = {i.fingerprint for i in after if not i.waived}
    return ([i for i in before if not i.waived and i.fingerprint not in fp_after],
            [i for i in after if not i.waived and i.fingerprint not in fp_before])


def as_modifications(suggestions: list[Suggestion], enabled: Optional[bool] = None) -> list[Modification]:
    """SettingsFile what-if edits for these suggestions (copies; the note carries the prediction). enabled=None keeps
    the search's selection, True/False sets it for all."""
    out = []
    for s in suggestions:
        if s.modification is None:
            continue
        m = copy.copy(s.modification)
        m.enabled = s.selected if enabled is None else enabled
        pred = (f" (predicted {s.delta_db:+.1f} dB, L {s.delta_l * 100:+.0f} %)" if s.delta_db is not None
                else "")
        m.note = f"{s.id}: {s.title}{pred}"
        out.append(m)
    return out


POWER_ISSUES = ("hot_loop", "emi_margin", "conducted_emission", "plane_resonance")
GREEDY_POOL = 8             # best single candidates considered for the combined (greedy) selection


def greedy_pick(cands: list, evaluate_set: Callable, max_fixes: int, min_gain: float, threads: int = 1
                ) -> list[tuple]:
    """Pick up to max_fixes candidates one at a time: each round adds the one that lowers the combined relative L
    change most, judged with the ones already picked in place (evaluate_set(list) -> (d_db, d_l) or None). Stops
    when the best addition gains less than min_gain. Returns [(candidate, combined d_l after it)]."""
    chosen: list = []
    totals: list[float] = []
    total = 0.0
    while len(chosen) < max_fixes:
        rest = [c for c in cands if all(c is not x for x in chosen)]
        if not rest:
            break
        if not chosen:            # singles are already known
            results = [(c.delta_db, c.delta_l) for c in rest]
        else:
            results = run_parallel(rest, lambda c: evaluate_set(chosen + [c]), threads)
        scored = [(r[1], k) for k, r in enumerate(results) if r is not None]
        if not scored:
            break
        d_l, k = min(scored)
        if d_l - total > -min_gain:
            break
        chosen.append(rest[k])
        totals.append(d_l)
        total = d_l
    return list(zip(chosen, totals))


def _layers_with(copper: CopperMap, board: Board, at, net: str, radius: float = 0.3) -> list[str]:
    """The layers where `net` has plane copper at `at` (a via there would join those layers)."""
    probe = Point(at).buffer(radius)
    return [l for l in board.copper_layers if copper.plane(l, net).intersects(probe)]


def power_via_candidates(checker: PlacementChecker, board: Board, model: BoardModel, setup, config: Config,
                         keep: int = 1) -> list[tuple]:
    """(pad, via) for the hot loop's rail and ground pads (rail capacitors, switches) with no via or through-hole
    pad of their net within rules.stitch_distance_mm: a via of the pad's net next to it, landing in the net's
    copper on the pad's layer and on the nearest other layer that has it there."""
    from .power import _side, rail_caps
    spec = setup.spec
    pads = []
    for fp in rail_caps(board, config, spec, setup.res.location):
        pads += [p for p in fp.pads if p.net in (spec.rail, spec.ground)]
    for el in setup.elements:
        pads += [p for p in el.a + el.b if p.net in (spec.rail, spec.ground)]
    stitch: dict[str, list] = {}
    for v in board.vias:
        stitch.setdefault(v.net, []).append(v.position)
    for p in board.pads():
        if p.pad_type == "thru_hole":
            stitch.setdefault(p.net, []).append(p.position)
    reach = config.rules.stitch_distance_mm
    out, seen = [], set()
    z = model.z_phys
    for p in pads:
        if p.pin in seen or any(math.dist(p.position, q) <= reach for q in stitch.get(p.net, [])):
            continue
        seen.add(p.pin)
        side = _side(board.footprint(p.footprint))
        here = _layers_with(model.copper, board, p.position, p.net, max(p.size) / 2 + 1.0)
        others = sorted((l for l in here if l != side), key=lambda l: abs(z[l] - z[side]))
        if side not in here or not others:
            continue                      # nothing to stitch to (no copper of the net on another layer here)
        for m in via_candidates(checker, board, p.position, p.net, [side, others[0]], keep):
            out.append((p, m))
    return out


POWER_CAP = (100e-9, 0.4e-9)    # suggested hot-loop capacitor: 100 nF, 0402-size ESL


def power_cap_candidates(checker: PlacementChecker, board: Board, model: BoardModel, setup, keep: int = 2
                         ) -> list[Modification]:
    """Small capacitors between the rail and ground right at the switches: the classic hot-loop fix. Anchored
    between the high side's rail pads and the low side's ground pads (an integrated stage: its rail and ground
    pins), each pad with a via to its net's plane."""
    from .power import _side
    spec = setup.spec
    rail_pads = [p for el in setup.elements for p in el.a + el.b if p.net == spec.rail]
    gnd_pads = [p for el in setup.elements for p in el.a + el.b if p.net == spec.ground]
    if not rail_pads or not gnd_pads:
        return []
    ra = (sum(p.position[0] for p in rail_pads) / len(rail_pads), sum(p.position[1] for p in rail_pads) / len(rail_pads))
    ga = (sum(p.position[0] for p in gnd_pads) / len(gnd_pads), sum(p.position[1] for p in gnd_pads) / len(gnd_pads))
    anchor = ((ra[0] + ga[0]) / 2, (ra[1] + ga[1]) / 2)
    planes = _planes_of(model.copper, board, anchor, (spec.rail, spec.ground))
    if len(planes) != 2:
        return []
    layer = _side(setup.elements[0].fp)
    C, ESL = POWER_CAP
    return cap_candidates(checker, board, anchor, (spec.rail, spec.ground), planes, layer, keep, C=C, ESL=ESL)


class _PowerEvaluator:
    """A via's effect on a converter's hot loop at LOOP_F (inductance and moment), as a low-rank update of the
    converter model's factorisation, started from the baseline solution."""

    def __init__(self, model: BoardModel, setup):
        """The stage's baseline hot-loop solution at LOOP_F, the start of every candidate's solve."""
        from .power import LOOP_F
        self.model, self.setup, self.f = model, setup, LOOP_F
        fine = setup.fine
        self.base = fine.sols.get(LOOP_F) or fine.exc.solve(LOOP_F)

    def evaluate(self, m: Modification) -> Optional[tuple[float, float]]:
        """One edit's effect (see evaluate_set)."""
        return self.evaluate_set([m])

    def evaluate_set(self, mods: list[Modification]) -> Optional[tuple[float, float]]:
        """(Δ moment dB, relative ΔL) with all these vias and capacitors in place."""
        st = self.setup.fine.build
        br = []
        for m in mods:
            br += modification_branches(self.model, m, st)
        if not br:
            return None
        exc = self.setup.fine.exc
        try:
            stack = exc.stack.with_branches(br)
            sol = exc.on(stack).solve(self.f, guess=[stack.map_state(self.base.state_for(exc.stack), exc.stack)])
        except SolverError:
            return None
        mb, mn = float(np.linalg.norm(self.base.m)), float(np.linalg.norm(sol.m))
        d_db = 20 * math.log10(mn / mb) if mb > 0 and mn > 0 else 0.0
        lb, ln = self.base.L_loop, sol.L_loop
        return d_db, (ln - lb) / lb if lb > 0 else 0.0


def _title(m: Modification) -> str:
    """A suggestion's title from its edit ("Add a GND stitching via")."""
    return {"via": f"Add a {m.net} stitching via", "cap": f"Add a {m.nets[0]}–{m.nets[1]} capacitor",
            "fill": f"Fill the slot with {m.net} on {m.layer}"}[m.type]


def _outer_layer(board: Board, layer: str) -> str:
    """The outer layer nearer to `layer` (a capacitor goes on the side closer to the planes it joins)."""
    order = board.copper_layers
    if layer in order and order.index(layer) > len(order) // 2:
        return order[-1]
    return order[0]


def _planes_of(copper: CopperMap, board: Board, at, nets) -> tuple:
    """For each net, the plane layer nearest to `at` that carries it."""
    out = []
    probe = Point(at).buffer(1.0)
    for net in nets:
        layer = next((l for l in board.copper_layers if copper.plane(l, net).intersects(probe)), None)
        if layer is None:
            return ()
        out.append(layer)
    return tuple(out)


def _gap_sides(copper: CopperMap, issue: Issue, tracks: dict) -> tuple[tuple, tuple]:
    """(nets, plane layers) on both sides of a gap crossing."""
    ref = (issue.details.get("reference_layers") or [None])[0]
    track = tracks.get(issue.items[0]) if issue.items else None
    if ref is None or track is None:
        return (), ()
    (x0, y0), (x1, y1) = track.start, track.end
    L = math.hypot(x1 - x0, y1 - y0) or 1.0
    ux, uy = (x1 - x0) / L, (y1 - y0) / L
    cx, cy = issue.location
    off = issue.length_mm / 2 + 0.3
    nets = []
    for sgn in (-1, 1):
        n = copper.plane_net_at(ref, Point(cx + sgn * off * ux, cy + sgn * off * uy).buffer(0.2))
        if n is None:
            return (), ()
        nets.append(n)
    return tuple(nets), (ref, ref)


def board_nets_at(copper: CopperMap, layers: list[str], at, prefer: list[str]) -> list[str]:
    """Reference nets that have plane copper at `at` on all of `layers` (stitching candidates)."""
    probe = Point(at).buffer(1.5)
    out = []
    for net in prefer:
        if layers and all(copper.plane(l, net).intersects(probe) for l in dict.fromkeys(layers)):
            out.append(net)
    return out
