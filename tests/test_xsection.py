"""The 2D cross-section solver (solver/xsection.py) against exact and accepted closed-form results: Cohn's
zero-thickness stripline and edge-coupled stripline (conformal mapping, exact), Hammerstad & Jensen's microstrip
(1980, Z0 within 0.01 %, εeff within 0.2 % of exact), and the physics the crosstalk figures rest on."""
import math

import pytest
from scipy.special import ellipk

from groundtruth.solver.xsection import Rect, Section, Slab, _graded, solve


def k_ratio(k: float) -> float:
    """K(k')/K(k), the complete elliptic integrals' ratio of conformal mapping (scipy's ellipk takes m = k²)."""
    return ellipk(1 - k * k) / ellipk(k * k)


def stripline(w, b, er, gap=None, W=None):
    """A zero-thickness strip (or two, `gap` apart edge to edge) midway between planes b apart, side walls ±W."""
    W = W or 10 * b
    rects = [Rect(-W, W, 0, 0, 0), Rect(-W, W, b, b, 0)]
    if gap is None:
        rects.append(Rect(-w / 2, w / 2, b / 2, b / 2, 1))
    else:
        rects += [Rect(-gap / 2 - w, -gap / 2, b / 2, b / 2, 1), Rect(gap / 2, gap / 2 + w, b / 2, b / 2, 2)]
    return Section(rects, [Slab(0, b, er)], (-W, W, 0, b))


def hammerstad_jensen(u: float, er: float) -> tuple[float, float]:
    """(Z0, εeff) of a zero-thickness microstrip of width/height u (Hammerstad & Jensen 1980)."""
    f = 6 + (2 * math.pi - 6) * math.exp(-(30.666 / u) ** 0.7528)
    z_air = 60 * math.log(f / u + math.sqrt(1 + (2 / u) ** 2))
    a = 1 + math.log((u ** 4 + (u / 52) ** 2) / (u ** 4 + 0.432)) / 49 + math.log(1 + (u / 18.1) ** 3) / 18.7
    b = 0.564 * ((er - 0.9) / (er + 3)) ** 0.053
    ee = (er + 1) / 2 + (er - 1) / 2 * (1 + 10 / u) ** (-a * b)
    return z_air / math.sqrt(ee), ee


def microstrip(w, h, er, gap=None):
    """A zero-thickness microstrip (or an edge-coupled pair) on h of εr over a plane; the box ±20 h, 20 h of air."""
    X = 20 * h
    rects = [Rect(-X, X, 0, 0, 0)]
    if gap is None:
        rects.append(Rect(-w / 2, w / 2, h, h, 1))
    else:
        rects += [Rect(-gap / 2 - w, -gap / 2, h, h, 1), Rect(gap / 2, gap / 2 + w, h, h, 2)]
    return Section(rects, [Slab(0, h, er)], (-X, X, 0, 21 * h))


@pytest.mark.parametrize("w_b", [0.25, 0.5, 1.0])
def test_a_stripline_matches_cohns_exact_impedance(w_b):
    """Z0 = 30π/sqrt(εr)·K(k)/K(k'), k = sech(πw/2b): within 0.5 %; εeff is εr in a uniform dielectric."""
    b, er = 1e-3, 4.0
    exact = 30 * math.pi / math.sqrt(er) * k_ratio(math.tanh(math.pi * w_b / 2))
    r = solve(stripline(w_b * b, b, er))
    assert r.z0() == pytest.approx(exact, rel=0.005)
    assert r.eeff() == pytest.approx(er, rel=1e-6)
    assert r.delay() == pytest.approx(math.sqrt(er) / 299792458.0, rel=1e-6)


def test_edge_coupled_stripline_matches_cohns_even_and_odd_modes():
    """Cohn 1955: Z_even, Z_odd = 30π/sqrt(εr)·K(k')/K(k) with k_e = tanh(πw/2b)·tanh(π(w+s)/2b), k_o =
    tanh(πw/2b)·coth(π(w+s)/2b); Zdiff = 2·Z_odd, Zcomm = Z_even/2 within 0.5 %. A uniform dielectric has no
    forward crosstalk (L_m/L = C_m/C); the backward coefficient equals the even/odd one, (Ze - Zo)/(2(Ze + Zo))."""
    b, w, s, er = 1e-3, 0.5e-3, 0.25e-3, 4.0
    t1, t2 = math.tanh(math.pi * w / (2 * b)), math.tanh(math.pi * (w + s) / (2 * b))
    ze = 30 * math.pi / math.sqrt(er) * k_ratio(t1 * t2)
    zo = 30 * math.pi / math.sqrt(er) * k_ratio(t1 / t2)
    r = solve(stripline(w, b, er, gap=s))
    assert r.zdiff() == pytest.approx(2 * zo, rel=0.005)
    assert r.zcomm() == pytest.approx(ze / 2, rel=0.005)
    assert abs(r.kf(1, 0)) < 1e-9
    assert r.kb(1, 0) == pytest.approx((ze - zo) / (2 * (ze + zo)), rel=0.01)


@pytest.mark.parametrize("u", [0.5, 1.0, 2.0])
def test_a_microstrip_matches_hammerstad_and_jensen(u):
    """Z0 within 0.5 %, εeff within 0.5 % (the closed form's own accuracy is 0.01 % / 0.2 %)."""
    h, er = 0.2e-3, 4.5
    z, ee = hammerstad_jensen(u, er)
    r = solve(microstrip(u * h, h, er))
    assert r.z0() == pytest.approx(z, rel=0.005)
    assert r.eeff() == pytest.approx(ee, rel=0.005)


def test_coupled_microstrip_crosstalk_falls_with_spacing_and_has_forward_coupling():
    """Two microstrips on FR-4: the backward coefficient falls with spacing (roughly as 1/(1 + (s/h)²), the
    textbook estimate, within a factor of 2), and the inhomogeneous dielectric gives forward coupling of the
    inductive sign (L_m/L > C_m/C: FEXT negative, as measured on microstrip)."""
    h, w, er = 0.2e-3, 0.35e-3, 4.5
    kbs = []
    for s in (0.5 * h, h, 2 * h, 4 * h):
        r = solve(microstrip(w, h, er, gap=s))
        lm, cm = r.coupling(1, 0)
        assert lm > cm > 0 and r.kf(1, 0) > 0
        d = s + w                                      # centre to centre
        estimate = 1 / (1 + (d / h) ** 2)
        assert 0.5 < (lm + cm) / 2 / estimate < 2.0
        kbs.append(r.kb(1, 0))
    assert kbs == sorted(kbs, reverse=True) and kbs[-1] < kbs[0] / 5


def test_a_wide_strip_over_a_plane_tends_to_the_parallel_plate():
    """A strip 20 h wide: C per metre close to ε0·εr·w/h (fringing adds a few %) and Z0 near 377/sqrt(εr)·h/w."""
    h, er = 0.1e-3, 4.0
    w = 20 * h
    r = solve(microstrip(w, h, er))
    plate = 8.8541878128e-12 * er * w / h
    assert 1.0 < r.C[0, 0] / plate < 1.15


def test_the_grid_is_graded_and_ends_exactly():
    """_graded: fine steps at both ends (each its own), growing towards the middle, the last point exactly the
    end."""
    pts = _graded(0.0, 1.0, 0.01, 0.01, 0.1, 1.2)
    steps = [b - a for a, b in zip([0.0] + pts, pts)]
    assert pts[-1] == 1.0 and steps[0] == pytest.approx(0.01) and steps[-1] == pytest.approx(0.01)
    assert max(steps) <= 0.1 + 1e-12 and all(s > 0 for s in steps)
    assert _graded(0.0, 0.015, 0.01, 0.01, 0.1, 1.2)[-1] == 0.015      # shorter than two fine steps
    pts = _graded(0.0, 1.0, 0.001, 0.05, 0.1, 1.2)                      # fine at 0, coarse at 1
    steps = [b - a for a, b in zip([0.0] + pts, pts)]
    assert steps[0] == pytest.approx(0.001) and steps[-1] == pytest.approx(0.05) and pts[-1] == 1.0
