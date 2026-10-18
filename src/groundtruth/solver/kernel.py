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

import numpy as np
from numpy.polynomial import legendre

MU0 = 4e-7 * np.pi
GMD_FLAT = 0.2235            # geometric mean distance of a thin flat strip, as a fraction of its width [Grover 1946]


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


def strip_self_numeric(length: float, width: float, n: int = 24) -> float:
    """Self partial inductance of a flat strip from n parallel sub-strips (accurate also when
    length ~ width, where the single-GMD formula is ~5 % low)."""
    w = width / n
    ys = (np.arange(n) + 0.5) * w
    dy = np.abs(ys[:, None] - ys[None, :])
    M = filament_mutual(length, np.zeros_like(dy), dy)
    np.fill_diagonal(M, strip_self(length, w))
    # with the current spread evenly over the n sub-strips (1/n each), the strip's inductance is the mean of the
    # sub-strips' inductance matrix: sum_ij M_ij / n^2
    return float(M.mean())


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

    # Near field: integrate over the strip widths (the filament approximation fails below ~3d).
    near = 4
    ca, cl = n_axial - 1, n_lateral - 1
    sa = slice(max(ca - near, 0), ca + near + 1)
    sl = slice(max(cl - near, 0), cl + near + 1)
    An, Ln = A[sa, sl], L[sa, sl]
    # 8-point Gauss-Legendre across each strip's width [Abramowitz 1964, §25.4]: 64 filament pairs per offset,
    # weights w_i w_j / 4 (the nodes map [-1, 1] onto [-d/2, d/2], and the result is averaged over both widths)
    x, w = leggauss(8)
    y1 = 0.5 * d * x
    W = np.outer(w, w).ravel() / 4.0
    dy = (y1[:, None] - y1[None, :]).ravel()                       # y2 - y1 over all pairs
    rho = np.sqrt((Ln[..., None] + dy) ** 2 + dz * dz)
    Kn = (filament_mutual(d, An[..., None], rho) * W).sum(-1)
    K[sa, sl] = Kn
    if dz == 0.0:
        K[ca, cl] = strip_self_numeric(d, d)        # the strip itself: square, where the GMD formula is ~5 % off
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
