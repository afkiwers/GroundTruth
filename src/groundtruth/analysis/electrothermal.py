"""Electro-thermal coupling of the DC paths and Thermal (the settings' thermal.electrothermal; the user: "rising heat
will increase via resistance which will force other vias to carry more load", then "add this as a settable option,
with a user settable max for rounds before considered settled ... I don't want it to get stuck in the loop").

Without it the DC paths are solved once with the copper at 20 °C and Thermal heats the board with that current
pattern, each sample's heat scaled by ρ(T) (first order, [A15]). With it, rounds follow the first thermal solve:
  1. every power stage's and DC load's DC paths are solved again on the whole-board DC model with each copper strip's
     resistance and each via barrel's at its temperature, R·(1 + α(T − 20 °C)) [IEC 60028] (T: the thermal result's
     map at the strip's two nodes, at the via's two layers), from the excitations Simulate kept (power._dc_plan);
  2. their summaries (dc_analysis, finish_dc_load: currents, densities, vias, resistance, drop, loss) replace the
     20 °C ones in copies of the results;
  3. if no path's loss and no via's current (as a share of its path's current) moved by more than the tolerance since
     the round before, the coupling has settled; otherwise Thermal is solved again with the new currents.
At most `electrothermal_rounds` rounds (never more than MAX_ROUNDS): the loop always ends. Copper's positive α makes
it converge (a hotter via sheds current to cooler ones, so the spread narrows) [A50]; a round that changes more than
the one before (it should not happen) ends it with the round before kept. Simulate's own results are not changed:
the coupled paths are the thermal result's (`electrothermal`, `dc_hot`)."""
from __future__ import annotations

import copy
import math
from types import SimpleNamespace
from typing import Callable, Optional

import numpy as np

from ..core.board import Board
from ..config.settings import Config

MAX_ROUNDS = 10            # the most rounds whatever the settings say
DIVERGE = 1.5              # a round changing this much more than the one before ends the loop (the round before kept)


class _Run:
    """A solved DC run as dc_analysis / finish_dc_load read one: its model and its solution at DC_F."""

    def __init__(self, build, sol):
        """build: the whole-board DC model; sol: its Solution at DC_F."""
        self.build, self.sol = build, sol

    def ok(self) -> dict:
        """{DC_F: solution} (ModelRun.ok's form)."""
        from .power import DC_F
        return {DC_F: self.sol}


def _cancelled(cancel) -> bool:
    """Whether the user cancelled (a threading or multiprocessing Event, as the thermal solve takes)."""
    return cancel is not None and getattr(cancel, "is_set", lambda: False)()


def _temperatures(th, layer: str, x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """The thermal result's temperature (°C) at points (x, y mm) of a copper layer: its map's nearest level-0 cell
    (each its hottest copper [A41]); the ambient where the layer has no map or the cell no copper."""
    grid, tmap = (getattr(th, "layer_grids", {}) or {}).get(layer), (th.sheet_current or {}).get(layer)
    if grid is None or tmap is None:
        return np.full(len(x), th.ambient_c, float)
    ti = np.clip(np.rint((np.asarray(x) - grid.x0) / grid.d).astype(np.int64), 0, tmap.shape[0] - 1)
    tj = np.clip(np.rint((np.asarray(y) - grid.y0) / grid.d).astype(np.int64), 0, tmap.shape[1] - 1)
    t = np.asarray(tmap[ti, tj], float)
    return np.where(np.isfinite(t), t, th.ambient_c)


def resistance_scales(st, th) -> tuple[np.ndarray, np.ndarray]:
    """(per strip, per branch) factors 1 + α(T − 20 °C) of the whole-board DC model `st` at the thermal result's
    temperatures [IEC 60028]: a strip at the mean of its two nodes', a via barrel at the mean of its two layers'
    at its position; the other branches (parts, pins) 1."""
    from ..thermal.materials import COPPER
    plane = st.plane
    t_nodes = {}
    for k, layer in enumerate(st.names):
        g = st.grids[k]
        ii, jj = np.meshgrid(np.arange(g.nx), np.arange(g.ny), indexing="ij")
        t_nodes[k] = _temperatures(th, layer, (g.x0 + ii * g.d).ravel(), (g.y0 + jj * g.d).ravel()).reshape(g.nx, g.ny)
    xa, ya = plane.xa, plane.ya
    tx = np.array([0.5 * (t_nodes[l][i, j] + t_nodes[l][i + 1, j]) for l, i, j in xa]) if len(xa) else np.zeros(0)
    ty = np.array([0.5 * (t_nodes[l][i, j] + t_nodes[l][i, j + 1]) for l, i, j in ya]) if len(ya) else np.zeros(0)
    strips = 1.0 + COPPER.alpha * (np.concatenate([tx, ty]) - 20.0)
    branches = np.ones(len(plane.branches))
    for k, b in enumerate(plane.branches):
        if b.kind == "via" and not b.name.startswith("pad "):
            x, y = st.xy(b.a)
            ta = _temperatures(th, st.names[b.a[0]], np.array([x]), np.array([y]))[0]
            tb = _temperatures(th, st.names[b.b[0]], np.array([x]), np.array([y]))[0]
            branches[k] = 1.0 + COPPER.alpha * (0.5 * (ta + tb) - 20.0)
    return strips, branches


def _solve(dst, inj):
    """One DC run on the (temperature-scaled) whole-board model: the Solution at DC_F. `inj` is the run's injections
    as Simulate's excitation was given them, [((layer, i, j), A)] (the same model, its fingerprint checked, has the
    same grid; the excitation checks again that they are on copper and balance)."""
    from ..solver.peec import RouteExcitation
    from .power import DC_F
    return RouteExcitation(dst.plane, [], injections=[(tuple(int(v) for v in n), float(a)) for n, a in inj]).solve(DC_F)


def _hot_stage(model, dst, c):
    """A copy of power stage `c` with its DC paths solved again on `dst` (dc_analysis on the new solutions): the
    paths' current as Simulate ended with them (the input path rescaled to the energy balance), their switching
    share, the inductor's ripple and the hot loop's switching current as its plan has them."""
    from .power import dc_analysis
    plan = c.dc_plan
    hot = copy.copy(c)
    for k in ("dc_vectors", "dc_branch", "dc_via", "dc_ripple_sq", "dc_ripple_branch", "dc_ripple_via"):
        setattr(hot, k, {})
    # the terminals' current is shown only (results/terminals.py): the cold one stays, a copy so that the rescale of
    # a hot path (power._rescale_path) leaves the cold result's alone
    hot.dc_terminal = dict(getattr(c, "dc_terminal", {}))
    current = {p["name"]: p["current_a"] for p in c.dc}
    runs, ripple, extra = [], {}, {}
    for r in plan["runs"]:
        runs.append((r["name"], current.get(r["name"], r["current"]), _Run(dst, _solve(dst, r["inj"]))))
        if r["ripple"] is not None:
            ripple[r["name"]] = (r["ripple"][0], _Run(dst, _solve(dst, r["ripple"][1])))
        if r["extra"]:
            extra[r["name"]] = r["extra"]
    h = plan["hot"]
    setup = SimpleNamespace(res=hot, dc_runs=runs, dc_ripple=ripple, dc_extra=extra,
                            dc_hot=(h["name"], h["current"], _Run(dst, _solve(dst, h["inj"]))) if h else None)
    paths, maps, grids, _ = dc_analysis(model, setup)
    hot.dc, hot.dc_maps, hot.dc_grids = paths, maps, grids
    hot.dc_branch, hot.dc_ripple_branch = {}, {}          # the whole model's branches: only the vias' are needed
    hot.dc_plan = plan
    return hot


def _hot_load(model, dst, d):
    """A copy of DC load result `d` solved again on `dst` (finish_dc_load: its path, maps, vias and the loads'
    drops at temperature)."""
    from .dcload import DcLoadSetup, finish_dc_load
    spec = next((s for s in model.config.dc_loads if s.name == d.net), None)
    if spec is None:
        return d
    hot = copy.copy(d)
    hot.notes, hot.issues = list(d.notes), []
    plan = d.dc_plan
    setup = DcLoadSetup(spec, hot, _Run(dst, _solve(dst, plan["inj"])), plan["feeds"])
    setup.issues = []
    finish_dc_load(model, setup)
    hot.dc_branch = {}
    hot.dc_plan = plan
    return hot


def _state(results) -> dict:
    """{(result, path): (loss W, path current A, {via: current A})}: what the rounds compare."""
    out = {}
    for c in results:
        for p in getattr(c, "dc", None) or []:
            out[(c.net, p["name"])] = (float(p.get("loss_w") or 0.0), float(p.get("current_a") or 0.0),
                                       {v["name"]: float(v["current_a"]) for v in p.get("vias", [])})
    return out


def change(a: dict, b: dict) -> float:
    """The largest relative change between two rounds' states: a path's loss (of its loss), a via's current (of its
    path's current, so idle vias' noise does not count)."""
    worst = 0.0
    for key, (loss_b, i_b, vias_b) in b.items():
        if key not in a:
            continue
        loss_a, i_a, vias_a = a[key]
        if loss_a > 0:
            worst = max(worst, abs(loss_b - loss_a) / loss_a)
        scale = max(i_a, i_b, 1e-12)
        for name in set(vias_a) | set(vias_b):
            worst = max(worst, abs(vias_b.get(name, 0.0) - vias_a.get(name, 0.0)) / scale)
    return worst


def dc_rows(cold: list, hot: list) -> list[dict]:
    """The DC paths at temperature from the coupled solve, in dc_at_temperature's form: R, drop and loss at 20 °C
    and as solved at temperature ("factor" R_T / R_20; the hot loop's switching current has no drop)."""
    rows, hot_by = [], {(c.net, p["name"]): p for c in hot for p in getattr(c, "dc", None) or []}
    for c in cold:
        for p in getattr(c, "dc", None) or []:
            h = hot_by.get((c.net, p["name"]))
            if h is None or not p.get("r_mohm"):
                continue
            ac = bool(p.get("ac"))
            rows.append({"stage": c.net, "path": p["name"], "factor": h["r_mohm"] / p["r_mohm"], "t_mean_c": None,
                         "r20_mohm": p["r_mohm"], "r_mohm": h["r_mohm"],
                         "drop20_mv": None if ac else p.get("drop_mv"), "drop_mv": None if ac else h.get("drop_mv"),
                         "loss20_w": p.get("loss_w"), "loss_w": h.get("loss_w")})
    return rows


def busiest_vias(cold: list, hot: list, n: int = 12) -> list[dict]:
    """The vias most loaded at temperature, with their 20 °C figures: [{stage, path, via, current20_a, current_a,
    load20_pct, load_pct}], by load at temperature (a via in several paths: each)."""
    cold_by = {(c.net, p["name"], v["name"]): v for c in cold for p in getattr(c, "dc", None) or []
               for v in p.get("vias", [])}
    rows = []
    for c in hot:
        for p in getattr(c, "dc", None) or []:
            for v in p.get("vias", []):
                old = cold_by.get((c.net, p["name"], v["name"]), {})
                rows.append({"stage": c.net, "path": p["name"], "via": v["name"],
                             "current20_a": old.get("current_a"), "current_a": v["current_a"],
                             "load20_pct": old.get("load_pct"), "load_pct": v.get("load_pct")})
    rows.sort(key=lambda r: -(r["load_pct"] if r["load_pct"] is not None else r["current_a"]))
    return rows[:n]


def couple(board: Board, config: Config, converters: list, th, issues: list,
           solve: Callable[[list], tuple], progress=None, cancel=None) -> tuple:
    """Rounds of DC paths at temperature and Thermal (see the module), from the first thermal solve (`th`,
    `issues`, of the 20 °C currents). solve(converters) -> (ThermalResult, issues): Thermal of these results (in
    this process, the thermal child or on the compute server). Returns (th, issues, the results the temperatures
    belong to); th.electrothermal says how the rounds went, th.dc_hot holds the paths at temperature. A cancel
    ends it between solves (thermal's Cancelled)."""
    from ..solver.peec import SolverError
    from ..thermal.solver.common import Cancelled
    from .power import dc_board_build, dc_board_of
    from .simulate import BoardModel
    tc = config.thermal
    rounds_max = max(1, min(int(tc.electrothermal_rounds), MAX_ROUNDS))
    tol = float(tc.electrothermal_tolerance)
    say = progress or (lambda msg, frac: None)
    info = {"rounds": 0, "max_rounds": rounds_max, "tolerance": tol, "settled": False, "changes": [], "vias": [],
            "stopped": ""}
    th.electrothermal = info
    if th.error:
        info["stopped"] = "the first thermal solve failed"
        return th, issues, converters
    targets = [c for c in converters if getattr(c, "dc_plan", None) and not getattr(c, "error", None)
               and (c.kind == "dcload" or (c.kind == "converter" and not getattr(c, "view_of", None)
                                           and not getattr(c, "bridge", None)))]
    if not targets:
        info["stopped"] = ("no DC path to solve again (Simulate kept none on the whole-board DC model: run Simulate "
                           "again with this version)")
        th.notes.append(f"electro-thermal: {info['stopped']}")
        return th, issues, converters
    say("electro-thermal: building the whole-board DC model again", 0.0)
    model = BoardModel(board, config)
    try:
        dst = dc_board_build(model)
    except SolverError as e:
        dst, why = None, str(e)
    else:
        why = "no board outline"
    if dst is None:
        info["stopped"] = f"the whole-board DC model could not be built ({why})"
        th.notes.append(f"electro-thermal: {info['stopped']}")
        return th, issues, converters
    fp = dc_board_of(dst).fingerprint
    ok = [c for c in targets if c.dc_plan.get("fingerprint") == fp]
    if len(ok) < len(targets):
        th.notes.append("electro-thermal: " + ", ".join(c.net for c in targets if c not in ok)
                        + " left at 20 °C: the DC model is not Simulate's (the board or the settings changed, or "
                          "another resolution): run Simulate again")
    if not ok:
        info["stopped"] = "the DC model is not Simulate's"
        return th, issues, converters
    current = list(converters)
    before = _state(ok)                          # the 20 °C state, then each round's
    cold = list(ok)
    last_change = None
    try:
        for k in range(1, rounds_max + 1):
            if _cancelled(cancel):
                raise Cancelled("cancelled")
            say(f"electro-thermal round {k}: the DC paths with the copper at its temperature", k / (rounds_max + 1))
            strips, branches = resistance_scales(dst, th)
            dst.plane.set_resistance_scale(strips, branches)
            try:
                hot = [(_hot_load(model, dst, c) if c.kind == "dcload" else _hot_stage(model, dst, c)) for c in ok]
            except SolverError as e:
                info["stopped"] = f"round {k}'s DC solve failed ({e}): the round before kept"
                break
            now = _state(hot)
            moved = change(before, now)
            info["rounds"] = k
            info["changes"].append(moved)
            if last_change is not None and moved > DIVERGE * last_change and moved > tol:
                info["stopped"] = (f"round {k} changed more ({moved * 100:.2g} %) than the one before "
                                   f"({last_change * 100:.2g} %): the round before kept")
                break
            swap = {id(c): h for c, h in zip(ok, hot)}
            current = [swap.get(id(c), c) for c in converters]
            ok, before, last_change = hot, now, moved
            if moved <= tol:            # the currents moved less than the tolerance: the temperatures stand
                info["settled"] = True
                break
            if _cancelled(cancel):
                raise Cancelled("cancelled")
            th_new, issues_new = solve(current)
            if th_new.error:
                info["stopped"] = f"round {k}'s thermal solve failed ({th_new.error}): the round before kept"
                break
            th_new.electrothermal = info
            th, issues = th_new, issues_new
    finally:
        dst.plane.set_resistance_scale(None, None)      # free the factorisations
    info["vias"] = busiest_vias(cold, ok)
    th.dc_hot = dc_rows(cold, ok)
    top = info["vias"][0] if info["vias"] else None
    via_txt = (f"; the busiest via {top['via']} ({top['stage']}, {top['path']}) {top['current20_a']:.3g} A at 20 °C, "
               f"{top['current_a']:.3g} A at temperature" if top and top["current20_a"] is not None else "")
    ch = ", ".join(f"{c * 100:.2g} %" for c in info["changes"])
    if info["settled"]:
        th.notes.append(f"electro-thermal: settled in {info['rounds']} round(s) (changes {ch}; tolerance "
                        f"{tol * 100:g} %): the DC paths with the copper at its temperature{via_txt}")
    else:
        th.notes.append(f"electro-thermal: not settled after {info['rounds']} round(s) of at most {rounds_max} "
                        f"(changes {ch}; tolerance {tol * 100:g} %)"
                        + (f": {info['stopped']}" if info["stopped"] else "") + via_txt)
    return th, issues, current
