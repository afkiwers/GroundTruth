"""Turn a current-density vector field into arrows: for the heatmaps (quiver) and for drawing on the board.

Every arrow has the same small size and points along the local current. The *number* of arrows in an area shows
how much current flows there: like field lines, their density is proportional to the current density.
"""
from __future__ import annotations

import math

import numpy as np

N_ACROSS = 45          # candidate spots across the wider side of a map (plots)
ARROW = 0.7            # arrow length, fraction of the spot spacing
JITTER = 0.25          # random offset of a spot, fraction of the spacing (breaks up the lattice look)
REF_PERCENTILE = 95    # full fill at this percentile of the copper's density (not at one hot pad corner)
RETURN_ARROW_COLOUR = "#00e5ff"   # the heatmaps' return-current arrows unless the Preferences choose another (cyan:
                                  # it stands out on the inferno scale and is not the via markers' green)
FLOOR = 1e-3           # no arrows below this fraction of the reference: the heatmap's log floor (three decades)
MIN_FILL = 0.1         # a spot above the floor gets at least this fill: weak current shows sparse arrows, never none


def _bayer(n: int = 8) -> np.ndarray:
    """Ordered-dither thresholds in (0, 1), n×n (n a power of 2): any fill fraction f switches on a fraction f of
    every n×n tile, spread evenly."""
    m = np.zeros((1, 1), int)
    while m.shape[0] < n:                       # the recursive Bayer construction: each step doubles the size
        m = np.block([[4 * m, 4 * m + 2], [4 * m + 3, 4 * m + 1]])
    return (m + 0.5) / m.size


BAYER = _bayer(8)


def reference_density(maps) -> float:
    """Full-fill density for one or several maps shown together: the current-weighted REF_PERCENTILE of their
    densities, i.e. the level that REF_PERCENTILE % of the current flows below. Weighting by current keeps idle
    copper out of it (a DC path through a small part of a big pour would otherwise put the reference at the idle
    level), and a hot pad corner, which carries little current, does not set it either."""
    vals = np.concatenate([np.ravel(np.asarray(m, float)) for m in maps]) if maps else np.zeros(0)
    vals = np.sort(vals[vals > 0])
    if not vals.size:
        return 0.0
    cum = np.cumsum(vals)
    # the density at which the running sum (densities sorted ascending, each weighing as its own current) passes
    # REF_PERCENTILE % of the total
    return float(vals[min(int(np.searchsorted(cum, REF_PERCENTILE / 100 * cum[-1])), len(vals) - 1)])


def arrow_field(grid, K: np.ndarray, Kx: np.ndarray, Ky: np.ndarray, n_across: int = N_ACROSS,
                spacing_mm: float | None = None, max_arrows: int | None = None, gamma: float = 0.5,
                seed: int = 1, ref: float | None = None, with_density: bool = False,
                with_net: bool = False, floor: float | None = None) -> list[tuple]:
    """[(x, y, dx, dy), ...] in board mm (y down): equal arrows whose number per area follows the current density.

    The map is divided into spots of b×b grid nodes (about `n_across` across the wider side, or `spacing_mm`
    apart). A spot's fill is its mean density over `ref`, raised to `gamma`, capped at 1, at least MIN_FILL, and 0
    below `floor` (an absolute density, the heatmap's colour floor: its peak / 1000; default FLOOR·ref). Every
    connected region of one net's spots above the floor shows at least one arrow, at its strongest spot (the user:
    "even when the density is a lot less, it should still draw a few on the lightest setting and never omit
    them!": a pour carrying 0.1-0.4 A/mm² beside 10 A paths fell under the old floor, 1 % of a 38 A/mm² reference).
    `ref` defaults to reference_density of this map; pass a shared one when several maps are shown together, so
    a layer carrying little current also shows few arrows. gamma 0.5 (default) compresses the decades a current
    density spans (a return current falls off fast beside its trace) while staying monotonic: more current,
    more arrows; 1 is strictly proportional. An 8×8 ordered-dither pattern switches on that fraction of
    the spots, spread evenly and the same on every run; each arrow sits at its spot's copper centroid (jittered a
    little where the spot is all copper), points along the spot's mean current and is ARROW spot spacings long.
    A spot takes the copper of one net only (grid.label), the one carrying the most current in it: an arrow stays
    on its own net's copper and reads that net's density, not an average with a quiet pour beside it.
    With `max_arrows`, the spacing grows until the arrows fit. with_density: each arrow also gets its spot's
    mean current density, (x, y, dx, dy, K) in K's units (the board drawing sets the line width by it); with_net:
    then its net's name ("" on a grid without nets) as the last value (split_return takes it)."""
    node = np.asarray(grid.node, bool)
    nx, ny = node.shape
    Kn = np.where(node, np.asarray(K, float), 0.0)
    on = Kn[node & (Kn > 0)]
    if not on.size:
        return []
    ref = ref or reference_density([Kn[node]])
    if ref <= 0:
        return []
    floor = FLOOR * ref if floor is None else float(floor)
    b = max(1, round(spacing_mm / grid.d)) if spacing_mm else max(1, math.ceil(max(nx, ny) / max(n_across, 1)))
    # node indices, for the copper centroid of each spot
    i, j = np.meshgrid(np.arange(nx), np.arange(ny), indexing="ij")
    # the copper by net (Grid.label): a spot takes the nodes of ONE net, the one carrying the most current in it, so
    # a narrow strip between two pours of another net keeps its arrows on itself and at its own density (the user:
    # refboard's 1 mm +VDC strip on In1.Cu between two GND pours had its arrows averaged with the idle ground and put
    # on it, then drawn as GND's return current). Nets with no current worth an arrow are left out.
    label = getattr(grid, "label", None)
    if label is not None and getattr(grid, "nets", None):
        lab = np.where(node, np.asarray(label), -1)
        groups = [n for n in np.unique(lab[node & (Kn >= floor) & (Kn > 0)]) if n >= 0]
        masks = [lab == n for n in groups] or [node]
        names = [grid.nets[n] for n in groups] or [""]
    else:
        masks, names = [node], [""]
    Kxn, Kyn = np.where(node, np.asarray(Kx, float), 0.0), np.where(node, np.asarray(Ky, float), 0.0)
    while True:
        bx, by = math.ceil(nx / b), math.ceil(ny / b)

        def blocks(a: np.ndarray) -> np.ndarray:        # sums over b×b blocks (zero-padded at the far edges)
            """Sums of `a` over b x b blocks, by reshaping the zero-padded array to (bx, b, by, b)."""
            pad = np.zeros((bx * b, by * b), float)
            pad[:nx, :ny] = a
            return pad.reshape(bx, b, by, b).sum(axis=(1, 3))

        # per net: its nodes, centroid sums, current and vector sums in each spot; then each spot's net
        sums = np.array([[blocks(m.astype(float)), blocks(np.where(m, i, 0)), blocks(np.where(m, j, 0)),
                          blocks(np.where(m, Kn, 0.0)), blocks(np.where(m, Kxn, 0.0)), blocks(np.where(m, Kyn, 0.0))]
                         for m in masks])                                    # (nets, 6, bx, by)
        pick = np.argmax(sums[:, 3], axis=0)                                 # the net with the most current
        best = np.take_along_axis(sums, pick[None, None], axis=0)[0]
        count = best[0]
        with np.errstate(invalid="ignore", divide="ignore"):
            ci, cj = best[1] / count, best[2] / count
            mag = best[3] / count
            ux, uy = best[4] / count, best[5] / count
        norm = np.hypot(ux, uy)                 # the spot's mean current vector; its direction is the arrow's
        rel = np.nan_to_num(mag) / ref
        above = (np.nan_to_num(mag) >= floor) & (np.nan_to_num(mag) > 0)
        fill = np.where(above, np.maximum(np.clip(rel, 0.0, 1.0) ** gamma, MIN_FILL), 0.0)
        ix, iy = np.meshgrid(np.arange(bx), np.arange(by), indexing="ij")
        # ordered dithering: a spot shows an arrow when its fill exceeds the Bayer threshold of its place in the
        # 8x8 tile, so a region filled to f shows arrows on about a fraction f of its spots, evenly spread
        keep = (count > 0) & (norm > 0) & (fill > BAYER[ix % 8, iy % 8])
        if max_arrows is None or keep.sum() <= max_arrows or b > max(nx, ny):
            break
        b = math.ceil(b * 1.25)
    keep = _every_region(keep, (count > 0) & (norm > 0) & (fill > 0), pick, np.nan_to_num(mag))
    pitch = b * grid.d
    jit = np.random.default_rng(seed).uniform(-JITTER, JITTER, (bx, by, 2)) * pitch
    full = count == b * b                                   # jitter only where it cannot leave the copper
    x = grid.x0 + ci * grid.d + np.where(full, jit[..., 0], 0.0)
    y = grid.y0 + cj * grid.d + np.where(full, jit[..., 1], 0.0)
    L = ARROW * pitch
    with np.errstate(invalid="ignore", divide="ignore"):
        dx, dy = ux / norm * L, uy / norm * L
    k = np.nonzero(keep)
    # each arrow starts half its length before the spot's centre, so it is centred on the spot
    return [(float(x[a, c] - dx[a, c] / 2), float(y[a, c] - dy[a, c] / 2), float(dx[a, c]), float(dy[a, c]))
            + ((float(mag[a, c]),) if with_density else ()) + ((names[pick[a, c]],) if with_net else ())
            for a, c in zip(*k)]


def _every_region(keep: np.ndarray, candidates: np.ndarray, net: np.ndarray, mag: np.ndarray) -> np.ndarray:
    """`keep` with one more spot in every 4-connected region of one net's candidate spots that the dithering left
    without an arrow: its strongest spot (a narrow strip or a small pour carrying weak current has few spots, and
    the 8 x 8 pattern can miss all of them)."""
    from scipy import ndimage
    keep = keep.copy()
    for n in np.unique(net[candidates]):
        regions, count = ndimage.label(candidates & (net == n))
        if not count:
            continue
        index = np.arange(1, count + 1)
        shown = ndimage.maximum(keep.astype(float), regions, index)
        for r in index[np.asarray(shown) == 0]:
            keep[ndimage.maximum_position(mag, regions, r)] = True
    return keep


def arrow_widths(densities, peak: float, w_min: float, w_max: float, decades: float = 3.0) -> list[float]:
    """Line widths (mm) for arrows of these current densities, on the heatmap's log scale: w_max at `peak` (the
    peak over all of the result's layers, so every layer's arrows compare), w_min `decades` below it and lower,
    log-linear in between. The user: "high density, thick arrows, low density very thin arrows"."""
    if peak <= 0:
        return [w_min for _ in densities]
    out = []
    for k in densities:
        t = 1.0 + math.log10(k / peak) / decades if k > 0 else 0.0
        out.append(w_min + (w_max - w_min) * min(max(t, 0.0), 1.0))
    return out


def return_rule(rr, config, board=None):
    """Which nets carry a view's return current, as a test of a net name (split_return): a signal route's return is
    every net but its own (RouteResult.signal_nets: the plane under it, whatever its net); a power stage's, a DC
    path's and a DC load's is the ground: the stages' `ground` nets, the loads' return pins' nets (from `board`) and
    every net named like a ground (analysis.safety.is_ground: GND, VSS, 0V, ...RTN). Without a `config` (a map drawn
    outside the plugin): the names alone."""
    from ..analysis.safety import is_ground
    if getattr(rr, "kind", "") == "route":
        own = set(getattr(rr, "signal_nets", None) or [rr.net])
        return lambda net: bool(net) and net not in own
    grounds = {s.ground for s in config.stages() if s.ground} if config is not None else set()
    for spec in getattr(config, "dc_loads", None) or []:
        for pin in [spec.source_return, *(ld.ret for ld in spec.loads)]:
            pad = board.find_pin(pin) if board is not None and pin else None
            if pad is not None and pad.net:
                grounds.add(pad.net)
    return lambda net: bool(net) and (net in grounds or is_ground(net))


def split_return(arrows: list[tuple], grid, is_return) -> tuple[list[tuple], list[tuple]]:
    """Arrows (x, y, dx, dy, ...) mm split into (the rest, the return current's) by the net of the copper at each
    arrow's foot (grid.net_at at its node, else at the nearest copper node within one pitch: an arrow sits at its
    spot's copper centroid); an arrow on no labelled copper stays with the rest. An arrow that names its net (a last
    value that is a string: arrow_field's with_net) is split by that, and keeps it off the list it returns."""
    ahead, back = [], []
    nx, ny = grid.node.shape
    for a in arrows:
        if a and isinstance(a[-1], str):
            (back if is_return(a[-1]) else ahead).append(a[:-1])
            continue
        i0, j0 = int(round((a[0] - grid.x0) / grid.d)), int(round((a[1] - grid.y0) / grid.d))
        net = ""
        for di, dj in ((0, 0), (1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1)):
            i, j = i0 + di, j0 + dj
            if 0 <= i < nx and 0 <= j < ny and grid.node[i, j]:
                net = grid.net_at((i, j))
                break
        (back if is_return(net) else ahead).append(a)
    return ahead, back
