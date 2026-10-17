"""Mesh planning and the coupled electro-thermal iteration."""

from __future__ import annotations

import math
import time

import numpy as np

from ..kicad.board import Board
from ..project import Project
from .common import Progress, SolverError
from .electrical import ElectricalModel
from .raster import Grid
from .mesh import B, K_REF, HeatSamples, Mesh, plan_refinement, shape_samples, slab_count
from .results import ElectricalMap, Patch, Results
from .stack import build_stack
from .thermal import ThermalModel, component_heat_areas

QUALITY = {
    #          thermal unknowns (level 0), cells across narrowest track, electrical cells,
    #          refinement tolerance (K), finest thermal cell (mm), refinement budget (unknowns)
    "draft": (1.5e5, 2, 4e5, 0.5, 0.05, 1.5e5),
    "normal": (6e5, 4, 1.5e6, 0.3, 0.05, 6e5),
    "fine": (2.5e6, 6, 5e6, 0.1, 0.025, 2.5e6),
}
NICE = [0.02, 0.025, 0.03, 0.04, 0.05, 0.06, 0.075, 0.1, 0.125, 0.15, 0.2, 0.25, 0.3, 0.4, 0.5, 0.6,
        0.75, 1.0, 1.25, 1.5, 2.0, 2.5, 3.0]


def _nice_at_least(v: float) -> float:
    """The smallest round cell size (NICE) at least v."""
    for n in NICE:
        if n >= v - 1e-9:
            return n
    return NICE[-1]


def involved_nets(board: Board, setup) -> set[str]:
    """The nets of the terminals' and links' pads (the electrical model covers these)."""
    keys = [k for t in setup.terminals for k in t.pads] + [k for l in setup.links for k in (l.pad_a, l.pad_b)]
    out = set()
    for k in keys:
        p = board.pad(k)
        if p is not None and p.net:
            out.add(p.net)
    return out


def plan_mesh(board: Board, setup) -> dict:
    """Choose cell sizes and estimate problem size. Cheap: no rasterisation."""
    stack = build_stack(board, setup.materials)
    q = QUALITY.get(setup.mesh.quality, QUALITY["normal"])
    area = max(board.outline.area, 1.0)
    notes = []

    def slabs_at(dx):
        """Slabs per column at cell size dx."""
        return slab_count(stack, dx)

    if setup.mesh.thermal_cell_mm > 0:
        dx_t = setup.mesh.thermal_cell_mm
    else:
        # The slab count depends on the cell size (graded split), so iterate to a fixed point.
        dx_t = 0.1
        for _ in range(4):
            dx_t = max(0.1, _nice_at_least(math.sqrt(area * slabs_at(dx_t) / q[0])))
    n_slabs = slabs_at(dx_t)
    x0, y0, x1, y1 = board.outline.bounds
    grid = Grid(x0, y0, dx_t, max(1, math.ceil((x1 - x0) / dx_t - 1e-9)), max(1, math.ceil((y1 - y0) / dx_t - 1e-9)))
    n_t = int(area / dx_t ** 2 * n_slabs)

    nets = involved_nets(board, setup)
    ratio, dx_e, n_e, min_w = 1, dx_t, 0, None
    if nets:
        widths = [t.width for t in board.tracks if t.net in nets]
        min_w = min(widths) if widths else None
        if min_w is None:
            dims = [math.sqrt(p.shape.area) for p in board.pads if p.net in nets and p.shape.area > 0]
            min_w = min(dims) if dims else 1.0
        cu_area = sum(t.width * sum(math.dist(a, b) for a, b in zip(t.points[:-1], t.points[1:]))
                      for t in board.tracks if t.net in nets)
        cu_area += sum(c.shape.area for c in board.copper if c.net in nets)
        cu_area += sum(p.shape.area * len(p.layers) for p in board.pads if p.net in nets)
        cu_area = min(cu_area, area * len(board.copper_layers))
        # electrical cell: fine enough for the narrowest track (q[1] cells across it), within the node budget
        # q[2] for the copper area; a whole fraction of the thermal cell so the grids nest
        if setup.mesh.electrical_cell_mm > 0:
            target = setup.mesh.electrical_cell_mm
        else:
            target = max(min_w / q[1], math.sqrt(max(cu_area, 1e-6) / q[2]), 0.01)
        # Never coarser than the target; step back once if that overshoots the node budget.
        ratio = int(min(64, max(1, math.ceil(dx_t / target - 1e-9))))
        if setup.mesh.electrical_cell_mm <= 0 and ratio > 1 and cu_area / (dx_t / ratio) ** 2 > 1.5 * q[2]:
            ratio -= 1
        dx_e = dx_t / ratio
        n_e = int(cu_area / dx_e ** 2)
        if dx_e > min_w / 2.5:
            notes.append(f"Narrowest track ({min_w:.3g} mm) is only {min_w / dx_e:.1f} cells wide: angled "
                         "sections may be off by up to ~10%. Use Normal/Fine or a smaller electrical cell.")
    adaptive = bool(setup.mesh.adaptive)
    dx_fine = dx_t
    while adaptive and dx_fine / 2 >= q[4] * (1 - 1e-9):
        dx_fine /= 2
    n_refine = int(q[5]) if adaptive and dx_fine < dx_t else 0
    mem_mb = 150 + (n_t * 950 + n_e * 1100) / 1e6
    return {"dx_t": dx_t, "ratio": ratio, "dx_e": dx_e, "grid": grid, "thermal_unknowns": n_t,
            "electrical_unknowns": n_e, "memory_mb": mem_mb, "memory_max_mb": mem_mb + n_refine * 950 / 1e6,
            "notes": notes, "nets": sorted(nets),
            "min_track": min_w, "n_slabs": n_slabs, "adaptive": adaptive, "dx_fine": dx_fine,
            "refine_unknowns": n_refine, "quality": q}


def run(board: Board, project: Project, board_index: int, progress: Progress) -> Results:
    """The electro-thermal solve of one board: plan the mesh, build the electrical model, refine the thermal
    mesh where heat enters, build the thermal model, then iterate: currents at the present temperatures (copper
    resistance rises with T) -> Joule heat -> temperatures, until the largest change is under the tolerance.
    Under-relaxed (omega drops) when an iteration changes more than the one before (oscillation)."""
    t_start = time.perf_counter()
    setup = project.boards[board_index]
    env = project.environment
    stack = build_stack(board, setup.materials)
    plan = plan_mesh(board, setup)
    warnings = list(board.warnings) + list(plan["notes"])
    grid: Grid = plan["grid"]
    q = plan["quality"]

    el = None
    if setup.terminals:
        progress.stage("Building electrical model", 0.02, 0.15)
        el = ElectricalModel(board, setup, stack, grid, plan["ratio"], progress)
        warnings += el.warnings

    nbx, nby = math.ceil(grid.nx / B), math.ceil(grid.ny / B)
    leaves = {0: {(i, j) for i in range(nby) for j in range(nbx)}}
    if plan["adaptive"] and plan["dx_fine"] < grid.dx:
        progress.stage("Planning adaptive refinement", 0.15, 0.20)
        samples = _heat_samples(board, setup, el, plan)
        # A sample this small cannot move any cell's error estimate by more than 1% of the tolerance.
        keep = samples.p > 0.01 * q[3] * K_REF * plan["dx_fine"] * 1e-3
        samples = HeatSamples(samples.x[keep], samples.y[keep], samples.s[keep], samples.p[keep])
        leaves, info = plan_refinement(grid, samples, q[3], plan["dx_fine"], q[5], stack)
        if info["limited"]:
            more = ("a smaller thermal cell" if setup.mesh.quality == "fine" else "Fine quality")
            warnings.append(f"Adaptive refinement reached its budget. The worst features were refined first; "
                            f"local temperatures elsewhere may be off by up to about {info['remaining_err']:.2g} K. "
                            f"Use {more} for more detail.")

    progress.stage("Building thermal model", 0.20, 0.30)
    mesh = Mesh(board, stack, grid, leaves, progress)
    th = ThermalModel(board, setup, env, stack, mesh, progress)
    el_map = th.electrical_map(el) if el is not None else None
    Qc, comp_info = th.component_heat()
    warnings += th.warnings
    if el is None and Qc.sum() <= 0:
        raise SolverError("Nothing heats the board. Add a current path (source and sink pads) or component power.")

    T = np.full(th.n, env.ambient_c)
    omega = 1.0
    hist = []
    max_it = max(2, setup.mesh.max_iterations)
    tol = max(1e-4, setup.mesh.tolerance_c)
    Q = Qc
    for it in range(max_it):
        lo = 0.30 + 0.65 * (1 - 0.7 ** it)
        hi = 0.30 + 0.65 * (1 - 0.7 ** (it + 1))
        progress.stage(f"Coupled iteration {it + 1}", lo, hi)
        # Inexact coupled iteration: solve each linear system only a little more accurately
        # than the temperatures are still changing (tight again as the iteration settles).
        rise = max(float(T.max()) - env.ambient_c, 1.0)
        lin_tol = 1e-5 if not hist else float(np.clip(1e-3 * hist[-1]["dT"] / rise, 1e-8, 1e-5))
        Q = Qc.copy()
        if el is not None:
            el.solve(el_map.T @ T, tol=0.1 * lin_tol)
            Q += el_map @ _electrical_power(el)
        T_new = th.solve(Q, T, tol=lin_tol)
        dT = float(np.max(np.abs(T_new - T)))
        if hist and dT > hist[-1]["dT"] * 1.05:
            omega = max(0.25, omega * 0.6)
        T = T + omega * (T_new - T)
        tmax = float(T.max())
        hist.append({"iter": it + 1, "dT": dT, "Tmax": tmax, "power": float(Q.sum()), "omega": omega})
        progress.send({"type": "conv", **hist[-1]})
        if not np.isfinite(tmax) or tmax > 1000:
            raise SolverError(f"Thermal runaway: copper passes 1000 °C (iteration {it + 1}). "
                              "At this current the copper would fuse. Reduce the current or widen the path.")
        if it >= 1 and dT < tol:
            break
    else:
        warnings.append(f"Coupled iteration did not reach {tol} °C tolerance in {max_it} iterations "
                        f"(last change {hist[-1]['dT']:.3g} °C).")

    progress.stage("Post-processing", 0.95, 1.0)
    Tcu = th.copper_values(T)  # (ncu, columns)
    Qcu = th.copper_values(Q) / (mesh.col_area * 1e6)  # W/mm²
    Tf = np.stack([mesh.to_level0(v) for v in Tcu])
    Qmap = np.stack([mesh.to_level0(v) for v in Qcu])
    patches = [Patch(l, g, np.stack(a[:th.ncu]), np.stack(a[th.ncu:]))
               for l, g, a in mesh.patches(list(Tcu) + list(Qcu))]

    # Hot spot
    col, slab = th.node_column_slab(int(np.argmax(T)))
    summary = {
        "t_max": float(T.max()), "t_min": float(T.min()),
        "hotspot": {"x": float(mesh.col_x[col]), "y": float(mesh.col_y[col]), "slab": slab.name},
        "ambient": env.ambient_c,
        "layers": [],
        "power_total": float(Q.sum()),
        "power_components": float(Qc.sum()),
        "heat_out": th.heat_out(T),
        "h": th.h_info,
        "iterations": hist,
        "mesh": {"thermal_cell_mm": grid.dx, "thermal_unknowns": th.n, "slabs": len(th.slabs),
                 "finest_cell_mm": float(mesh.col_dx.min()), "levels": mesh.summary(),
                 "electrical_cell_mm": plan["dx_e"] if el else None,
                 "electrical_unknowns": el.n_cells if el else 0},
        "elapsed_s": 0.0,
        "warnings": warnings,
    }
    A = mesh.col_area
    for k, L in enumerate(board.copper_layers):
        summary["layers"].append({"layer": L, "t_max": float(Tcu[k].max()),
                                  "t_mean": float((Tcu[k] * A).sum() / A.sum())})
    if env.enclosure.enabled:
        summary["enclosure"] = {"t_air": float(th.t_air), "t_wall_inside": float(th.t_wall),
                                "t_wall_outside": float(th.t_wall_out)}
    if th.mount_g.size:
        Gm = np.zeros(th.n)
        np.add.at(Gm, th.mount_nodes, th.mount_g)
        t_ch = th.t_wall if env.enclosure.enabled else env.ambient_c
        summary["mount_heat"] = float((Gm * (T - t_ch)).sum())
    summary["components"] = _component_temps(board, setup, th, T)
    emaps = []
    if el is not None:
        summary["electrical"] = el.summary()
        for m in el.maps():
            emaps.append(ElectricalMap(m["net"], board.copper_layers[m["layer"]], m["grid"],
                                       m["V"], m["J"], m["Jx"], m["Jy"], m["P"]))
    # Result maps hold the copper layers only (the slabs a user inspects).
    slabs = [{"name": sl.name, "kind": "copper", "z_bot": sl.z_bot, "z_top": sl.z_top, "layer": sl.layer}
             for sl in (th.slabs[th.slab_of_copper(L)] for L in board.copper_layers)]
    summary["elapsed_s"] = time.perf_counter() - t_start
    return Results(summary, grid, slabs, Tf, Qmap, list(board.copper_layers), emaps, patches)


def _electrical_power(el: ElectricalModel) -> np.ndarray:
    """Joule power per electrical cell, with each link's loss split over its two pads."""
    P = el.cell_power()
    for _, _, p_link, na, nb in el.link_power():
        P[na] += 0.5 * p_link / len(na)
        P[nb] += 0.5 * p_link / len(nb)
    return P


def _heat_samples(board: Board, setup, el: ElectricalModel | None, plan: dict) -> HeatSamples:
    """Where heat enters the board, as small squares, to steer refinement."""
    out = HeatSamples.empty()
    if el is not None:
        el.solve(None)
        g = plan["grid"]
        out = out + HeatSamples(g.x0 + el.node_J * el.dx, g.y0 + el.node_I * el.dx,
                                np.full(el.n_cells, el.dx), _electrical_power(el))
    for c in setup.components:
        fp = board.footprint(c.ref)
        if c.power <= 0 or fp is None:
            continue
        for geom, share, kind in component_heat_areas(board, fp):
            shape = geom.shape if kind == "pad" else geom
            ds = max(plan["dx_fine"] / 2, math.sqrt(shape.area / 1e5))
            out = out + shape_samples(shape, c.power * share, ds)
    return out


def _component_temps(board: Board, setup, th: ThermalModel, T: np.ndarray) -> list[dict]:
    """Every part's temperature under its pads (max and area mean) and, with a theta_jb, its junction:
    T_board + P theta_jb. Hottest first."""
    out = []
    for fp in board.footprints:
        pads = [p for p in fp.pads if p.layers]
        if not pads:
            continue
        temps, ws = [], []
        for p in pads:
            n_, w = th.pad_nodes(p)
            temps.append(T[n_])
            ws.append(w)
        t_all, w_all = np.concatenate(temps), np.concatenate(ws)
        t_mean = float((t_all * w_all).sum() / w_all.sum())
        cp = setup.component(fp.ref)
        power = cp.power if cp else 0.0
        rec = {"ref": fp.ref, "value": fp.value, "t_max": float(t_all.max()), "t_mean": t_mean,
               "power": power}
        if cp and cp.theta_jb > 0:
            rec["t_junction"] = t_mean + power * cp.theta_jb
        out.append(rec)
    out.sort(key=lambda r: -r.get("t_junction", r["t_max"]))
    return out
