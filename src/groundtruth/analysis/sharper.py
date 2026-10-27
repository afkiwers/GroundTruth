"""A sharper heatmap for one view (the plugin's Results tab, "Sharper map"): the view's own model rebuilt with
SHARPER_FACTOR times the node budget (about half the grid pitch) and solved at the map's frequency only.

The physics is the analysis's own (prepare_route / prepare_converter on the same board and settings); only the
grid is finer. Every number outside the maps (loop inductance, margins, emissions) stays as the full analysis
computed it: on refboard's SW1 the default grid is already converged for those (3.43 vs 3.55 nH,
-19.5 vs -19.2 dB at 4x the nodes), so a finer grid buys a sharper picture, not different results.

Views: a signal route (its fine model at map_frequency), a power stage's hot loop (its loop model at LOOP_F) and
a power stage's DC path (that path's DC model). The board-wide plane-capacitance model is not rebuilt: a route's
map always comes from its fine model."""
from __future__ import annotations

import copy
import math
import time
from dataclasses import replace
from typing import Optional

import numpy as np

from ..config.settings import Config
from ..core.board import Board
from ..solver import gpu

SHARPER_FACTOR = 4      # times the nodes: half the cell size
MIN_PITCH_MM = 0.05


class SharperError(RuntimeError):
    """The view cannot be rebuilt (the board or the settings changed, the route is gone): an expected condition."""


def sharper_view(board: Board, config: Config, view, factor: float = SHARPER_FACTOR, copper=None):
    """A copy of `view` (RouteResult / ConverterResult / a dc_view) with maps on a finer grid, and
    `view.sharper = {"d_mm", "was_mm", "nodes", "seconds"}`. `board` and `config` must be the ones the view was
    computed with (what-if runs: the edited copies)."""
    from .simulate import BoardModel
    if getattr(view, "kind", "") == "dcload":
        raise SharperError(f"{view.net}: a DC load is solved on the whole-board DC model; for a finer map raise the "
                           "Resolution in Preferences and Simulate again")
    t0 = time.time()
    a = config.analysis
    if view.grid is None:
        raise SharperError(f"{view.net}: this view has no map")
    # the pitch itself, not only the budget: where the budget does not bind, "auto" picks the dielectric height
    # and a larger budget alone would change nothing. 1/sqrt(factor) of the view's cell, capped by factor x budget.
    pitch = max(view.grid.d / math.sqrt(factor), MIN_PITCH_MM)
    fine_cfg = replace(config, analysis=replace(a, max_nodes=int(a.max_nodes * factor * a.resolution), grid_mm=pitch,
                                                plane_capacitance=False, resolution=1.0))   # the pitch is set
    model = BoardModel(board, fine_cfg, copper)
    with gpu.use(getattr(a, "gpu", "auto")):            # 4x the nodes: the solve that gains most from a GPU
        out = _converter(model, view) if view.kind == "converter" else _route(model, view)
    was = view.grid.d if view.grid is not None else None
    out.sharper = {"d_mm": out.grid.d, "was_mm": was, "nodes": int(sum(int(g.node.sum()) for g in
                                                                       out.layer_grids.values())),
                   "seconds": time.time() - t0}
    return out


def _maps(view, st, sol):
    """A copy of `view` with its grids, current maps and arrow fields from solution `sol` of model `st` (the
    arrows as float32: they are only drawn)."""
    out = copy.copy(view)
    out.layer_grids = dict(zip(st.names, st.grids))
    out.grid = st.grids[0]
    out.sheet_current = {n: sol.sheet_current(k, st.d, True) for k, n in enumerate(st.names)}   # as Simulate's
    out.sheet_vectors = {n: tuple(v.astype(np.float32) for v in sol.sheet_vectors(k, st.d, True))
                         for k, n in enumerate(st.names)}
    from .simulate import terminal_vectors
    out.terminal_vectors = terminal_vectors(st, sol)
    return out


def _route(model, view):
    """A route's view again on the finer model: the route traced anew, its fine model built and solved at the
    map's frequency; maps, layer shares, cross-section and the vias carrying the return come from it (the
    board-wide model's capacitive transfers are kept from the view)."""
    from .route import RouteError, find_route
    from .section import corridor_mm, route_section
    from .simulate import _return_masks, prepare_route, transfers
    board, config = model.board, model.config
    spec = next((n for n in config.nets if n.name == view.net), None)
    if spec is None:
        raise SharperError(f"{view.net} is no longer in the settings: run Simulate again")
    try:
        route = find_route(board, config, spec.name, spec.driver, view.receiver, spec.through)
    except RouteError as e:
        raise SharperError(f"{view.net}: {e}") from e
    setup, res, _ = prepare_route(model, spec, route)
    if setup is None:
        raise SharperError(f"{view.net}: {res.error}")
    f = view.map_frequency or setup.f_ref
    sol = setup.fine.exc.solve(f)
    st = setup.fine.build
    out = _maps(view, st, sol)
    own = set(route.nets)
    share = sol.layer_share(_return_masks(st, own))
    out.layer_share = {n: float(s) for n, s in zip(st.names, share)}
    out.section = route_section(sol, st, route, own, (getattr(view, "section", None) or {}).get("half_mm")
                                or corridor_mm(setup.h_mm))
    pins = [setup.drv_ret.position, setup.rcv_ret.position]
    caps = [t for t in view.transfers if t.get("kind") == "capacitance"]      # from the board-wide model
    out.transfers = sorted(transfers(st, setup.fine.exc.stack, sol, route, pins, own) + caps,
                           key=lambda t: -t["fraction"])[:8]
    return out


def _converter(model, view):
    """A power stage's view again on the finer model: a DC path's view from that path's DC model, the hot-loop
    view from the stage's loop model at LOOP_F."""
    from .power import DC_F, LOOP_F, dc_summary, dc_vector_maps, prepare_converter
    from .simulate import terminal_vectors, transfers
    config = model.config
    spec = next((c for c in config.stages() if c.name == view.net), None)
    if spec is None:
        raise SharperError(f"{view.net} is no longer in the settings: run Simulate again")
    name = getattr(view, "view_of", None)
    setup, res, _ = prepare_converter(model, spec)
    if setup is None:
        raise SharperError(f"{view.net}: {res.error}")
    if name:                                     # a DC path: that path's own DC model
        run = next(((cur, r) for n, cur, r in setup.dc_runs if n == name), None)
        if run is None:
            raise SharperError(f"{view.net}: DC {name} is not modelled any more: run Simulate again")
        current, r = run
        sol = r.exc.solve(DC_F)
        st = r.build
        # a switch node's two conduction states: its variance solved too, so the finer map is RMS like the first
        ripple = None
        if name in setup.dc_ripple:
            k_r, rrun = setup.dc_ripple[name]
            ripple = (k_r, rrun.exc.solve(DC_F))
        summary, maps = dc_summary(st, sol, current, name, ripple=ripple)
        out = copy.copy(view)
        out.layer_grids = dict(zip(st.names, st.grids))
        out.grid = st.grids[0]
        out.sheet_current = maps
        out.sheet_vectors = dc_vector_maps(st, sol, current)
        out.terminal_vectors = terminal_vectors(st, sol, current, density=True)
        tot = sum(float(m.sum()) for m in maps.values()) or 1.0
        out.layer_share = {l: float(m.sum()) / tot for l, m in maps.items()}
        out.transfers = [{"name": x["name"], "kind": "via", "layers": list(maps), "x": x["x"], "y": x["y"],
                          "fraction": x["current_a"] / max(summary["current_a"], 1e-12), "distance_mm": 0.0,
                          "current_a": x["current_a"]} for x in summary["vias"]]
        return out
    sol = setup.fine.exc.solve(LOOP_F)           # the hot loop
    st = setup.fine.build
    out = _maps(view, st, sol)
    share = sol.layer_share()
    out.layer_share = {n: float(s) for n, s in zip(st.names, share)}
    out.transfers = [dict(t, distance_mm=math.dist((t["x"], t["y"]), view.location))
                     for t in transfers(st, setup.fine.exc.stack, sol, None, [])]
    return out


def describe(view) -> Optional[str]:
    """'sharper map: 0.14 mm grid (was 0.26 mm), 324 526 nodes, 31 s' for a view made by sharper_view."""
    s = getattr(view, "sharper", None)
    if not s:
        return None
    was = f" (was {s['was_mm']:.2f} mm)" if s.get("was_mm") else ""
    nodes = f"{s['nodes']:,}".replace(",", " ")
    return f"sharper map: {s['d_mm']:.2f} mm grid{was}, {nodes} nodes, {s['seconds']:.0f} s"
