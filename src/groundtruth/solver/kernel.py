"""Partial inductances on a uniform grid, and vector potentials of straight trace segments.

All lengths in metres here. The plane is a sheet of square strips of side `d`; an x-directed
strip carries current between two neighbouring grid nodes. Mutual partial inductance between
two parallel strips only depends on their grid offset, so the inductance "matrix" of the plane
is a convolution kernel and its product with a current vector is an FFT convolution.

Partial inductance as in [Ruehli 1972] and [Paul 2010]; the closed forms for filaments and the GMD of a flat
strip from [Grover 1946]. Keys: docs/REFERENCES.md.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Optional

import numpy as np
from numpy.polynomial import legendre

MU0 = 4e-7 * np.pi
GMD_FLAT = 0.2235            # geometric mean distance of a thin flat strip, as a fraction of its width [Grover 1946]


def skin_thickness(t, f, sigma):
    """The thickness (m) a foil t m thick of conductivity sigma conducts in at frequency f, as far as its
    resistance goes: R_ac = R_dc·F, t_eff = t / F, with F = Δ (sinh 2Δ + sin 2Δ) / (cosh 2Δ - cos 2Δ), Δ = t/δ,
    δ = 1/√(π f μ0 σ) [Ramo 1994]: the exact AC resistance of a conducting slab with the magnetic field on one face
    only [Dowell 1966] (m = 1), as a strip has over its return plane and the plane under it (their currents crowd
    onto the faces that look at each other) [A2]. t at DC, 1 + 4Δ⁴/45 for a thin foil, δ once δ ≪ t. Arrays
    broadcast.

    Before 2026-10-19 t_eff = δ(1 - e^(-t/δ)) (the integral of an exponential decay through the thickness): the
    same limits, but 28 % too resistive where δ = 2t (1 MHz in 35 µm copper), 58 % where δ = t (3.6 MHz) and
    34 % at 10 MHz, against this exact one-sided result (VALIDATION_AUDIT.md round 5, ISSUE-16)."""
    t = np.asarray(t, float)
    f = np.asarray(f, float)
    with np.errstate(divide="ignore", over="ignore", invalid="ignore"):
        delta = 1.0 / np.sqrt(np.pi * np.maximum(f, 1e-300) * MU0 * sigma)
        x = np.where(f > 0, t / delta, 0.0)
        y = np.minimum(2 * x, 60.0)                     # e^60: the ratio below is 1 to double precision beyond
        exact = x * (np.sinh(y) + np.sin(y)) / (np.cosh(y) - np.cos(y))
        # the series for a thin foil (Dowell's low-frequency form, m = 1): the formula above cancels badly there
        factor = np.where(x < 0.1, 1.0 + 4.0 * x ** 4 / 45.0, exact)
    out = t / factor
    return float(out) if out.ndim == 0 else out


@lru_cache(maxsize=None)
def leggauss(n: int) -> tuple[np.ndarray, np.ndarray]:
    """Gauss-Legendre nodes and weights [Abramowitz 1964, §25.4] (cached: numpy solves an eigenvalue problem on
    every call, which was two thirds of a power stage's model build, ~35 000 calls)."""
    x, w = legendre.leggauss(n)
    x.setflags(write=False)
    w.setflags(write=False)
    return x, w


def _g(u, rho):
    """Antiderivative used for parallel filaments: d²G/du² = 1/sqrt(u²+rho²)."""
    return u * np.arcsinh(u / rho) - np.sqrt(u * u + rho * rho)


def filament_mutual(length: float, axial, rho):
    """Mutual partial inductance of two parallel filaments of equal length, axial offset `axial`,
    perpendicular distance `rho` (arrays broadcast). Henry."""
    rho = np.maximum(rho, 1e-12 * length)     # coincident filaments: a tiny distance keeps the log finite
    # Neumann's double integral of 1/r over the two filaments [Paul 2010], done analytically: the second difference
    # of the antiderivative G at the offsets between their ends (parallel filaments [Grover 1946])
    return MU0 / (4 * np.pi) * (_g(axial + length, rho) - 2 * _g(axial, rho) + _g(axial - length, rho))


def strip_self(length: float, width: float, thickness: float = 0.0) -> float:
    """Partial self inductance of a flat strip (filament formula at the strip's GMD)."""
    # A strip's self inductance equals the mutual inductance of two filaments at its geometric mean distance
    # from itself; for a thin rectangle that GMD is 0.2235 (width + thickness) [Grover 1946]
    rho = GMD_FLAT * (width + thickness)
    return float(filament_mutual(length, np.array(0.0), np.array(rho)))


def strip_self_numeric(length: float, width):
    """Self partial inductance of a flat (zero-thickness) strip with its current spread evenly across it: the mean
    of the filament mutuals over every pair of points across its width, integrated accurately (the single-GMD formula
    is ~5 % low when length ~ width). `width` may be an array (one value per width, in one numpy pass).

    The pairs' mean is (2/w²)∫₀^w (w − u)·M_fil(u) du over their distance u, = 2∫₀¹ (1 − s)·M_fil(w·s) ds: the
    tape_mutual integral of a strip with itself, on the same panels graded toward u = 0 (the log singularity) but
    vectorised over the widths (a model asks for one per narrow strip: tape_mutual per width held the GIL for
    seconds on kraft_flux_mini_2, and the plugin window with it).

    Until 2026-10-20 the mean over 24 sub-strips with the GMD formula on the diagonal: it converges as 1/n and was
    0.86 % low for a square strip (VALIDATION_AUDIT.md round 6, ISSUE-28)."""
    s, ws = _unit_rule()
    w = np.asarray(width, float)
    flat = np.atleast_1d(w).ravel()
    out = np.empty(len(flat))
    for a in range(0, len(flat), 2048):                 # chunks: 2048 widths x 240 points at a time
        u = flat[a:a + 2048, None] * s[None, :]
        out[a:a + 2048] = 2.0 * (filament_mutual(length, 0.0, u) * ((1.0 - s) * ws)[None, :]).sum(axis=1)
    return float(out[0]) if w.ndim == 0 else out.reshape(w.shape)


@lru_cache(maxsize=1)
def _unit_rule() -> tuple[np.ndarray, np.ndarray]:
    """The nodes and weights on (0, 1) for strip_self_numeric: TAPE_PANELS panels graded toward 0 (cached)."""
    s, ws = _panel_rule(0.0, 1.0, 0.0, TAPE_PANELS, TAPE_ORDER)
    s.setflags(write=False)
    ws.setflags(write=False)
    return s, ws


TAPE_PANELS = 30            # tape_mutual: panels graded geometrically (ratio ½) toward a near-singular point ...
TAPE_ORDER = 8              # ... each with an 8-point Gauss-Legendre rule [Abramowitz 1964, §25.4]


def _panel_rule(p: float, q: float, toward: Optional[float], panels: int, order: int) -> tuple[np.ndarray, np.ndarray]:
    """Nodes and weights of a composite Gauss-Legendre rule on [p, q]: `panels` equal panels, or panels shrinking
    geometrically (each half the next) toward the end `toward`, where the integrand varies on a short scale or has an
    integrable log singularity. The smallest panel is 2^-(panels-1) of the interval."""
    x, w = leggauss(order)
    if toward is None:
        edges = np.linspace(0.0, 1.0, panels + 1)
    else:
        edges = np.concatenate([[0.0], 0.5 ** np.arange(panels - 1, -1, -1)])
        if toward == q:
            edges = 1.0 - edges[::-1]
    t0, t1 = edges[:-1], edges[1:]
    nodes = p + (q - p) * (t0[:, None] + (t1 - t0)[:, None] * (x[None, :] + 1.0) / 2.0)
    weights = (q - p) * (t1 - t0)[:, None] * w[None, :] / 2.0
    return nodes.ravel(), weights.ravel()


def tape_mutual(length: float, wa: float, wb: float, axial: float, lateral: float, dz: float) -> float:
    """Mutual partial inductance (H) of two parallel flat strips (zero thickness) `length` long, wa and wb wide, in
    parallel planes dz apart, their centres `axial` apart along them and `lateral` across: the mean of the filament
    mutuals [Grover 1946] over every pair of points across the two widths, each width carrying its current evenly.

    The double integral over the two widths is one integral over their difference v = y_b - y_a, weighted by how
    many pairs have that difference (the overlap of the two widths shifted by v, a trapezoid) [Kreyszig 2011]:
    M = (1 / (wa·wb)) ∫ W(v) M_fil(length, axial, √((lateral + v)² + dz²)) dv. It is split where W bends and where
    the two filaments cross (v = -lateral), toward which the panels shrink geometrically: there the integrand
    varies on the scale dz, or (dz = 0) has a log singularity. Accurate to ~1e-10 relative (against scipy's adaptive
    quad), where the 8 x 8 Gauss points across both widths the near field had before missed by 1.6 % at
    d / dz = 20 (VALIDATION_AUDIT.md round 6, ISSUE-28)."""
    h, k, s = 0.5 * (wa + wb), 0.5 * abs(wa - wb), -lateral
    cuts = {-h, -k, k, h}
    if -h < s < h:
        cuts.add(s)
    pts = sorted(cuts)
    total = 0.0
    for p, q in zip(pts[:-1], pts[1:]):
        if q - p <= 0.0:
            continue
        toward = p if p == s else q if q == s else None
        v, wt = _panel_rule(p, q, toward, TAPE_PANELS if toward is not None else 4, TAPE_ORDER)
        W = np.clip(np.minimum(0.5 * wa, v + 0.5 * wb) - np.maximum(-0.5 * wa, v - 0.5 * wb), 0.0, None)
        rho = np.sqrt((lateral + v) ** 2 + dz * dz)
        total += float(np.sum(wt * W * filament_mutual(length, np.full(len(v), axial), rho)))
    return total / (wa * wb)


def strip_kernel(n_axial: int, n_lateral: int, d: float, dz: float = 0.0) -> np.ndarray:
    """Mutual partial inductance between square strips (side d, direction = axis 0) for every grid
    offset (Δaxial, Δlateral) in [-(n-1), n-1]. Shape (2*n_axial-1, 2*n_lateral-1), centre = zero offset.

    Near neighbours use Gauss quadrature across both widths; distant ones a single filament.
    """
    ia = np.arange(-(n_axial - 1), n_axial) * d
    il = np.arange(-(n_lateral - 1), n_lateral) * d
    A, L = np.meshgrid(ia, il, indexing="ij")
    # far field: each strip as a filament along its centre line (dz: the vertical distance between two layers)
    K = filament_mutual(d, A, np.sqrt(L * L + dz * dz))

    # Near field: integrate over the strip widths (the filament approximation fails below ~3d), accurately also when
    # the layers are much closer than a cell (tape_mutual): a plane pair's sheet inductance μ0·dz per square is the
    # small difference of a strip's own-layer and other-layer sums, so an error in these near terms is magnified by
    # ~d/dz (8 x 8 Gauss points read it 1.5 / 3.9 / 20 % low at d/dz = 5 / 10 / 20, its resonances that much x ½
    # high: ISSUE-28). The offsets are symmetric in sign: each |offset| is integrated once. The strip itself (dz = 0,
    # no offset) is its exact self inductance, where the GMD formula is ~5 % off.
    near = 4
    ca, cl = n_axial - 1, n_lateral - 1
    for ia in range(0, min(near, n_axial - 1) + 1):
        for il in range(0, min(near, n_lateral - 1) + 1):
            m = tape_mutual(d, d, d, ia * d, il * d, dz)
            for sa_ in {ca + ia, ca - ia}:
                for sl_ in {cl + il, cl - il}:
                    K[sa_, sl_] = m
    return K


def segment_vector_potential(a: np.ndarray, b: np.ndarray, points: np.ndarray, rho_min: float = 1e-9) -> np.ndarray:
    """Vector potential magnitude (per ampere, along the segment direction) of a straight
    current filament from a to b at `points` (N, 3). Returns (N,) in T·m/A; multiply by the unit
    direction vector of the segment to get A."""
    ab = b - a
    L = np.linalg.norm(ab)
    if L == 0:
        return np.zeros(len(points))
    u = ab / L
    p = points - a
    pu = p @ u                                      # each point's position along the segment's axis
    rho2 = np.einsum("ij,ij->i", p, p) - pu * pu    # and its squared distance from the axis
    rho = np.sqrt(np.maximum(rho2, rho_min * rho_min))
    # mu0/(4 pi) times the integral of 1/r along the segment (the magnetostatic vector potential [Jackson 1999,
    # §5.4]), in closed form: asinh of the two ends' axial offsets over the distance
    return MU0 / (4 * np.pi) * (np.arcsinh((L - pu) / rho) + np.arcsinh(pu / rho))


def segment_mutual(a1, b1, a2, b2, rho_min: float, n: int = 16) -> float:
    """Mutual partial inductance between two straight filaments (Neumann integral [Paul 2010], Gauss quadrature on
    the second one). rho_min keeps collinear/touching segments finite (set it to the GMD)."""
    a2 = np.asarray(a2, float)
    b2 = np.asarray(b2, float)
    d2 = b2 - a2
    L2 = np.linalg.norm(d2)
    if L2 == 0:
        return 0.0
    x, w = leggauss(n)
    t = 0.5 * (x + 1)
    pts = a2 + t[:, None] * d2
    a1 = np.asarray(a1, float)
    b1 = np.asarray(b1, float)
    L1 = np.linalg.norm(b1 - a1)
    if L1 == 0:
        return 0.0
    u1 = (b1 - a1) / L1
    u2 = d2 / L2
    # M = (u1 . u2) times the first filament's vector potential integrated along the second (the inner integral
    # is analytic, the outer one Gauss quadrature on [0, L2])
    psi = segment_vector_potential(a1, b1, pts, rho_min)
    return float(np.dot(u1, u2) * 0.5 * L2 * np.sum(w * psi))
