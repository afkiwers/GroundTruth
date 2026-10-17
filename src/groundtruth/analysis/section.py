"""The return current along a signal route, layer by layer: the cross-section view.

At stations along the route (driver → receiver) a cut perpendicular to the trace, ±half_mm wide, is laid across
every plane layer, and the sheet current crossing it along the route is integrated:

    I_l(s) = −∫ K_l(p(s) + u·n) · t du        (A per A of signal current)

t is the route's direction there, n its normal. The minus sign: a return flows against the signal, so the return a
layer carries counts positive, and a current flowing with the signal (induced, or circulating between layers)
negative. I_l is complex (the solution's phase); the views use Re(I_l), the part in phase with the 1 A signal (the
excitation is real). Over a cut across all the copper Σ_l I_l = 1 at any frequency (KCL: everything that returns
from the receiver to the driver crosses it); inside ±half_mm it is the part of the return that stays near the
trace, so where the sum dips the return has left the route (a detour). Copper of the route's own nets (a pour it
runs through) is left out: it carries the signal itself.

No wx or kipy here: the plugin, the HTML report and the CLI's PNGs draw it (report/plots.draw_section).
"""
from __future__ import annotations

import math
from typing import Optional

import numpy as np

from .route import Route, RouteSegment, RouteVia

MAX_STATIONS = 400          # stations along the route at most
HALF_HEIGHTS = 20.0         # corridor half-width in dielectric heights: (2/π)·atan(20) = 96.8 % of an ideal return
HALF_MIN_MM, HALF_MAX_MM = 1.0, 5.0


def corridor_mm(h_mm: float) -> float:
    """Half-width of the cut: 20 dielectric heights, 1 to 5 mm."""
    return min(max(HALF_HEIGHTS * h_mm, HALF_MIN_MM), HALF_MAX_MM)


def _path(route: Route) -> tuple[list, list, list]:
    """(pieces [(s0, s1, start, end, layer)], signal runs [(s0, s1, layer)], layer changes [(s, from, to)]) along
    the route from the driver, s in mm."""
    pieces, runs, changes = [], [], []
    s = 0.0
    for step in route.steps:
        if isinstance(step, RouteVia):
            if step.from_layer != step.to_layer:
                changes.append((s, step.from_layer, step.to_layer))
            continue
        if not isinstance(step, RouteSegment) or step.length <= 0:
            continue
        pieces.append((s, s + step.length, np.asarray(step.start, float), np.asarray(step.end, float), step.layer))
        if runs and runs[-1][2] == step.layer and abs(runs[-1][1] - s) < 1e-9:     # continues the run
            runs[-1] = (runs[-1][0], s + step.length, step.layer)
        else:
            runs.append((s, s + step.length, step.layer))
        s += step.length
    return pieces, runs, changes


def route_section(sol, st, route: Route, own: Optional[set] = None, half_mm: float = 4.0,
                  max_stations: int = MAX_STATIONS) -> dict:
    """The cross-section of one solved route: sol a Solution of the route model, st its StackBuild (names, grids,
    d). Returns {} for a route without length. Keys:

    s_mm      stations along the route from the driver (mm)
    layers    plane layers (st.names)
    I         complex (layers × stations): return current crossing the cut on each layer, A per A of signal
    half_mm   the cut's half-width
    f         the solution's frequency
    signal    [(s0, s1, layer)]: where the signal runs on which layer
    vias      [(s, from_layer, to_layer)]: where it changes layer
    length_mm the route's length"""
    from scipy.ndimage import map_coordinates
    pieces, runs, changes = _path(route)
    if not pieces:
        return {}
    length = pieces[-1][1]
    d = st.d
    # about one station per grid cell (at most max_stations), at the middles of equal intervals
    n = max(2, min(max_stations, int(math.ceil(length / d))))
    s = (np.arange(n) + 0.5) * length / n
    # which straight piece each station lies on (binary search on the pieces' end distances)
    k = np.clip(np.searchsorted([p[1] for p in pieces], s), 0, len(pieces) - 1)
    s0 = np.array([pieces[i][0] for i in k])
    a = np.array([pieces[i][2] for i in k])
    b = np.array([pieces[i][3] for i in k])
    seg = np.array([pieces[i][1] - pieces[i][0] for i in k])
    t = (b - a) / seg[:, None]                               # unit direction, driver → receiver
    p = a + t * (s - s0)[:, None]
    normal = np.stack([-t[:, 1], t[:, 0]], axis=1)
    m = max(2, int(math.ceil(2 * half_mm / (d / 2))))       # samples across the cut: two per grid cell
    du = 2 * half_mm / m
    u = -half_mm + (np.arange(m) + 0.5) * du                 # midpoint rule across the cut
    x = p[:, 0, None] + u[None, :] * normal[:, 0, None]      # (stations, samples), board mm
    y = p[:, 1, None] + u[None, :] * normal[:, 1, None]
    I = np.zeros((len(st.names), n), complex)
    for li, g in enumerate(st.grids):
        kx, ky = sol.node_field(li, d)                       # complex, A/m per A, at the nodes
        if own and g.label is not None and g.nets:
            ids = [i for i, net in enumerate(g.nets) if net in own]
            if ids:
                mine = np.isin(g.label, ids)
                kx, ky = np.where(mine, 0, kx), np.where(mine, 0, ky)
        coords = [((x - g.x0) / g.d).ravel(), ((y - g.y0) / g.d).ravel()]

        def at(v):
            """The node field v sampled at the cut's points (bilinear interpolation, 0 off the grid)."""
            return (map_coordinates(v.real, coords, order=1, mode="constant", cval=0.0)
                    + 1j * map_coordinates(v.imag, coords, order=1, mode="constant", cval=0.0)).reshape(x.shape)
        along = at(kx) * t[:, 0, None] + at(ky) * t[:, 1, None]   # K . t: the component along the route
        I[li] = -along.sum(axis=1) * du * 1e-3                # A/m × m
    return {"s_mm": s.astype(np.float32), "layers": list(st.names), "I": I.astype(np.complex64),
            "half_mm": float(half_mm), "f": float(sol.f), "signal": runs, "vias": changes,
            "length_mm": float(length)}


def station_at(x: float, y: float, route_xy: list) -> Optional[tuple[float, float]]:
    """(s along the route, distance from it) of the route point nearest (x, y), both mm; None without a route.
    route_xy: RouteResult.route_xy (runs in route order, the same path as the section's stations)."""
    best = None
    s_run = 0.0
    for _, pts in route_xy:
        for a, b in zip(pts, pts[1:]):
            a, b = np.asarray(a, float), np.asarray(b, float)
            L = float(np.hypot(*(b - a)))
            if L <= 0:
                continue
            w = float(np.clip(np.dot((x, y) - a, b - a) / L ** 2, 0.0, 1.0))   # projection onto the segment
            dist = float(np.hypot(*(a + w * (b - a) - (x, y))))
            if best is None or dist < best[1]:
                best = (s_run + w * L, dist)
            s_run += L
    return best
