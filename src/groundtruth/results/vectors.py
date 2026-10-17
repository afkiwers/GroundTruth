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
FLOOR = 0.01           # no arrows below this fraction of the reference (idle copper, numerical noise)


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
                seed: int = 1, ref: float | None = None, with_density: bool = False) -> list[tuple]:
    """[(x, y, dx, dy), ...] in board mm (y down): equal arrows whose number per area follows the current density.

    The map is divided into spots of b×b grid nodes (about `n_across` across the wider side, or `spacing_mm`
    apart). A spot's fill is its mean density over `ref`, raised to `gamma`, capped at 1, and 0 below FLOOR·ref.
    `ref` defaults to reference_density of this map; pass a shared one when several maps are shown together, so
    a layer carrying little current also shows few arrows. gamma 0.5 (default) compresses the decades a current
    density spans (a return current falls off fast beside its trace) while staying monotonic: more current,
    more arrows; 1 is strictly proportional. An 8×8 ordered-dither pattern switches on that fraction of
    the spots, spread evenly and the same on every run; each arrow sits at its spot's copper centroid (jittered a
    little where the spot is all copper), points along the spot's mean current and is ARROW spot spacings long.
    With `max_arrows`, the spacing grows until the arrows fit. with_density: each arrow also gets its spot's
    mean current density, (x, y, dx, dy, K) in K's units (the board drawing sets the line width by it)."""
    node = np.asarray(grid.node, bool)
    nx, ny = node.shape
    Kn = np.where(node, np.asarray(K, float), 0.0)
    on = Kn[node & (Kn > 0)]
    if not on.size:
        return []
    ref = ref or reference_density([Kn[node]])
    if ref <= 0:
        return []
    b = max(1, round(spacing_mm / grid.d)) if spacing_mm else max(1, math.ceil(max(nx, ny) / max(n_across, 1)))
    # node indices, for the copper centroid of each spot
    i, j = np.meshgrid(np.arange(nx), np.arange(ny), indexing="ij")
    while True:
        bx, by = math.ceil(nx / b), math.ceil(ny / b)

        def blocks(a: np.ndarray) -> np.ndarray:        # sums over b×b blocks (zero-padded at the far edges)
            """Sums of `a` over b x b blocks, by reshaping the zero-padded array to (bx, b, by, b)."""
            pad = np.zeros((bx * b, by * b), float)
            pad[:nx, :ny] = a
            return pad.reshape(bx, b, by, b).sum(axis=(1, 3))

        count = blocks(node.astype(float))
        with np.errstate(invalid="ignore", divide="ignore"):
            ci = blocks(np.where(node, i, 0)) / count
            cj = blocks(np.where(node, j, 0)) / count
            mag = blocks(Kn) / count
            ux = blocks(np.where(node, Kx, 0.0)) / count
            uy = blocks(np.where(node, Ky, 0.0)) / count
        norm = np.hypot(ux, uy)                 # the spot's mean current vector; its direction is the arrow's
        rel = np.nan_to_num(mag) / ref
        fill = np.where(rel >= FLOOR, np.clip(rel, 0.0, 1.0) ** gamma, 0.0)
        ix, iy = np.meshgrid(np.arange(bx), np.arange(by), indexing="ij")
        # ordered dithering: a spot shows an arrow when its fill exceeds the Bayer threshold of its place in the
        # 8x8 tile, so a region filled to f shows arrows on about a fraction f of its spots, evenly spread
        keep = (count > 0) & (norm > 0) & (fill > BAYER[ix % 8, iy % 8])
        if max_arrows is None or keep.sum() <= max_arrows or b > max(nx, ny):
            break
        b = math.ceil(b * 1.25)
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
            + ((float(mag[a, c]),) if with_density else ())
            for a, c in zip(*k)]


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
