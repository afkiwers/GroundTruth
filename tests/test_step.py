"""The step response of a signal path's circuit (emi/mna.Circuit.step: BDF2 on the lumped circuit [Gear 1971];
transfer.step_metrics [Ogata 2010]) against closed forms. The user, of the plan's second branch: "let's do it now
-> Step response"."""
import math

import numpy as np
import pytest

from groundtruth.analysis.transfer import step_metrics, step_text
from groundtruth.emi.mna import Circuit, OpAmpParams


def test_an_rc_step_and_its_rise_and_settling_times():
    """1 kΩ, 1 µF: y = 1 − e^(−t/τ) to 1e-4; rise (10-90 %) τ·ln 9 = 2.197 ms, settling into ±2 % τ·ln 50 = 3.912 ms
    (0.5 %), no overshoot."""
    ckt = Circuit()
    ckt.vsource("in", "0")
    ckt.r("in", "out", 1e3)
    ckt.c("out", "0", 1e-6)
    t = np.linspace(0, 12e-3, 3000)
    y = ckt.step("out", t)
    assert np.max(np.abs(y[1:] - (1 - np.exp(-t[1:] / 1e-3)))) < 1e-4
    m = step_metrics(t, y, 1.0)
    assert m["rise"] == pytest.approx(1e-3 * math.log(9), rel=5e-3)
    assert m["settling"] == pytest.approx(1e-3 * math.log(50), rel=5e-3)
    assert m["overshoot_pct"] == pytest.approx(0.0, abs=0.01) and m["undershoot_pct"] is None


def test_a_second_order_step_has_its_overshoot():
    """A series RLC low-pass with Q 2 (ζ 0.25): the closed-form step 1 − e^(−ζω₀t)(cos ω_d t + ζ/√(1−ζ²) sin ω_d t)
    to 1e-3, its overshoot e^(−πζ/√(1−ζ²)) = 44.43 % (0.05 % points)."""
    l_, c_, q = 1e-3, 1e-6, 2.0
    ckt = Circuit()
    ckt.vsource("in", "0")
    ckt.r("in", "a", math.sqrt(l_ / c_) / q)
    ckt.l("a", "b", l_)
    ckt.c("b", "0", c_)
    w0, z = 1 / math.sqrt(l_ * c_), 1 / (2 * q)
    wd = w0 * math.sqrt(1 - z * z)
    t = np.linspace(0, 12 / (z * w0), 3000)
    y = ckt.step("b", t)
    closed = 1 - np.exp(-z * w0 * t) * (np.cos(wd * t) + z / math.sqrt(1 - z * z) * np.sin(wd * t))
    assert np.max(np.abs(y - closed)) < 1e-3
    m = step_metrics(t, y, 1.0)
    assert m["overshoot_pct"] == pytest.approx(100 * math.exp(-math.pi * z / math.sqrt(1 - z * z)), abs=0.05)
    assert m["t_peak"] == pytest.approx(math.pi / wd, rel=0.01)


def test_a_sallen_key_read_from_the_board_overshoots_by_its_q(tmp_path):
    """The unity-gain Sallen-Key board (Q 0.707, ζ 0.707): its step overshoots e^(−π) = 4.32 % (0.2 % points), its
    final value the DC gain 1 (0.2 %); the JSON summary and the console line carry it."""
    from test_analog_net import OPAMP, _setup  # noqa: F401  (the generated op-amp boards)
    from groundtruth.analysis.simulate import network_responses
    from groundtruth.components.models import ComponentModel
    from groundtruth.report.formats import response_lines
    parts = [("R1", "10k", "R", "SIG_IN", "A"), ("R2", "10k", "R", "A", "B"), ("C1", "20n", "C", "A", "OUT"),
             ("C2", "10n", "C", "B", "GND")]
    pins = {3: ("+IN", "B"), 2: ("-IN", "OUT"), 1: ("OUT", "OUT"), 5: ("V+", "+5V"), 4: ("V-", "GND")}
    board, cfg, spec, _ = _setup(tmp_path, parts, [("U3", pins)])
    for ref, c in (("C1", 20e-9), ("C2", 10e-9)):
        cfg.components[ref].model = ComponentModel("cap", {"C": c, "ESR": 0.0, "ESL": 0.0})
    out, _, _ = network_responses(board, cfg, spec, "U2.1", np.geomspace(1.0, 1e7, 200))
    m = out[0].step["metrics"]
    assert m["overshoot_pct"] == pytest.approx(100 * math.exp(-math.pi), abs=0.2)
    assert m["final"] == pytest.approx(1.0, rel=2e-3)
    assert out[0].summary()["step"]["metrics"]["overshoot_pct"] == pytest.approx(m["overshoot_pct"])
    assert any("step: final" in line and "overshoot 4." in line for line in response_lines(out[0]))


def test_an_all_pass_stage_undershoots_and_an_integrator_has_no_step():
    """A first-order all-pass (1 − sRC)/(1 + sRC) with an op-amp: a right-half-plane zero, its step starts the wrong
    way (undershoot ~100 %) and ends at 1; an inverting integrator (a pole at the origin, the normal form's m = −1)
    has no step response (no final value)."""
    from groundtruth.analysis.simulate import _step_of
    ckt = Circuit()
    ckt.vsource("in", "0")
    ckt.r("in", "m", 10e3)
    ckt.r("m", "out", 10e3)
    ckt.r("in", "p", 10e3)
    ckt.c("p", "0", 10e-9)
    ckt.opamp("U1", "p", "m", "out", OpAmpParams(gbw=1e9, c_in=0.0))
    t = np.linspace(0, 12 * 1e-4, 3000)
    m = step_metrics(t, ckt.step("out", t), 1.0)
    assert m["undershoot_pct"] > 90 and m["final"] == 1.0
    assert "undershoot" in step_text(m)
    assert _step_of(None, "out", [-1.0], {"m": -1, "k": 1.0}, np.array([1.0, 10.0])) is None


def test_a_lightly_damped_step_is_resolved_and_an_unstable_one_has_no_final_value():
    """Audit round 7, AA-6 / AA-7 (tools/dev/analog_audit.py S1 / S2): a series RLC of Q 100 through simulate._step_of
    overshoots e^(−πζ/√(1−ζ²)) = 98.44 % within 0.5 % (it read 62.9 %: 3000 steps over 382 periods); a circuit with
    a pole at +1000 /s (a negative conductance) is unstable, without a final value (it read the DC gain, −1)."""
    from types import SimpleNamespace
    from groundtruth.analysis.simulate import _step_of
    from groundtruth.analysis.transfer import normal_form, step_text
    lc, cc, q = 1e-3, 1e-6, 100.0
    c = Circuit()
    c.vsource("in", "0")
    c.r("in", "a", math.sqrt(lc / cc) / q)
    c.l("a", "b", lc)
    c.c("b", "0", cc)
    p, z, k = c.poles_zeros("b")
    st = _step_of(SimpleNamespace(circuit=c), "b", list(p), normal_form(p, z, k), np.array([1.0, 1e6]), list(z))
    zeta = 1 / (2 * q)
    assert st["metrics"]["overshoot_pct"] == pytest.approx(100 * math.exp(-math.pi * zeta / math.sqrt(1 - zeta ** 2)),
                                                           rel=5e-3)
    c = Circuit()
    c.vsource("in", "0")
    c.r("in", "a", 1e3)
    c.c("a", "0", 1e-6)
    c.vccs("0", "a", "a", "0", 2e-3)
    p, z, k = c.poles_zeros("a")
    st = _step_of(SimpleNamespace(circuit=c), "a", list(p), normal_form(p, z, k), np.array([1.0, 1e6]), list(z))
    assert st["metrics"]["unstable"] and st["metrics"]["final"] is None and st["metrics"]["settling"] is None
    assert step_text(st["metrics"]).startswith("unstable")


def test_a_ringing_pole_that_hardly_shows_does_not_stretch_the_step():
    """A pole's share of the step (simulate.step_share, the residue of H(s)/s over H(0)): an RC low-pass (1 kHz)
    whose response also has a pair at 300 MHz, Q 150, cancelled to 1e-6 by a zero pair beside it, keeps STEP_POINTS
    steps (it would need 1e8 to resolve that ringing); the pair's share is below STEP_SHARE."""
    from groundtruth.analysis.simulate import STEP_SHARE, step_share
    from groundtruth.analysis.transfer import STEP_POINTS
    w = 2 * math.pi * 300e6
    pair = list(np.roots([1, w / 150, w * w]))
    zpair = list(np.roots([1, w / 150, (w * (1 + 1e-6)) ** 2]))
    rc = -2 * math.pi * 1e3
    assert step_share(pair[0], [rc] + pair, zpair) < STEP_SHARE
    assert step_share(rc, [rc] + pair, zpair) == pytest.approx(1.0, rel=1e-3)
    assert STEP_POINTS == 3000


# ------------------------------------------------------------------------- audit phase 2 (M-1, M-2, M-7, M-8)
def _sym_step(h, s):
    """An independent oracle: the exact step of a rational H(s) (a SymPy expression with exact coefficients) by its
    residues [Ogata 2010]: the denominator's roots to 30 digits (mpmath, not the QZ), y(t) = H(0) + Σ Re r·e^{pt}
    with r = N(p)/(p·D'(p)) (distinct poles). (H(0), y as a numpy function of t)."""
    import sympy as sp
    num, den = sp.fraction(sp.together(h))
    pn, pd = sp.Poly(sp.expand(num), s), sp.Poly(sp.expand(den), s)
    dd = pd.diff(s)
    terms = [(complex(p), complex(pn.eval(p) / (p * dd.eval(p)))) for p in pd.nroots(n=30)]
    h0 = float(pn.eval(0) / pd.eval(0))

    def y(t):
        """The step at the times t."""
        t = np.asarray(t, float)
        return h0 + sum((r * np.exp(p * t)).real for p, r in terms)
    return h0, y


def _gt_step(c, out, f_low=1.0):
    """simulate._step_of on a circuit as network_responses calls it (every pole and zero)."""
    from types import SimpleNamespace
    from groundtruth.analysis.simulate import _step_of, cancel_doublets
    from groundtruth.analysis.transfer import normal_form
    p, z, k = c.poles_zeros(out)
    p, z, _ = cancel_doublets(p, z)
    return _step_of(SimpleNamespace(circuit=c), out, p, normal_form(p, z, k), np.array([f_low, 1e8]), z)


def test_an_ac_coupled_step_has_final_value_zero_and_its_peak():
    """Audit M-1: a high-pass (a zero at the origin) took its last sample, e⁻¹² of the peak, as its final value:
    'final 6.1e-06, rise 3.14 ps, overshoot 16211361 %'. Its final value is 0 (H(0) = 0 exactly): no rise, no
    overshoot; the peak, its time and the decay into ±2 % of the peak. An RC high-pass (1 kHz): y = e^(−t/RC), peak 1
    at 0, decayed at RC·ln 50; an AC-coupled low-pass (1.6 Hz, 10 kHz): peak and decay against SymPy's residues."""
    import sympy as sp
    from scipy.optimize import brentq, minimize_scalar
    rc = 1e3 * 159e-9
    c = Circuit()
    c.vsource("in", "0")
    c.c("in", "o", 159e-9)
    c.r("o", "0", 1e3)
    m = _gt_step(c, "o")["metrics"]
    assert m["final"] == 0.0 and m["rise"] is None and m["overshoot_pct"] is None
    assert m["peak"] == pytest.approx(1.0, abs=1e-9) and m["t_peak"] == pytest.approx(0.0, abs=1e-12)
    assert m["settling"] == pytest.approx(rc * math.log(50), rel=1e-9)
    text = step_text(m)
    assert text.startswith("final value 0 (AC-coupled): peak 1 at 0 s") and "overshoot" not in text
    # the AC-coupled low-pass: Ca 1 µF, Ra 100 kΩ, R1 1 kΩ, C2 16 nF; its nodal equations by hand
    s = sp.symbols("s")
    va, vo = sp.symbols("va vo")
    ca, ra, r1, c2 = sp.Rational(1, 10 ** 6), 10 ** 5, 10 ** 3, sp.Rational(16, 10 ** 9)
    sol = sp.solve([(1 - va) * s * ca - va / ra - (va - vo) / r1, (va - vo) / r1 - vo * s * c2], [va, vo])
    _, y = _sym_step(sol[vo], s)
    c = Circuit()
    c.vsource("in", "0")
    c.c("in", "a", 1e-6)
    c.r("a", "0", 1e5)
    c.r("a", "o", 1e3)
    c.c("o", "0", 16e-9)
    st = _gt_step(c, "o")
    m = st["metrics"]
    td = np.linspace(0, 2e-3, 20001)
    k = int(np.argmax(y(td)))
    best = minimize_scalar(lambda x: -y(x), bounds=(td[k - 1], td[k + 1]), method="bounded", options={"xatol": 1e-14})
    assert m["final"] == 0.0 and m["peak"] == pytest.approx(-best.fun, rel=1e-9)
    assert m["t_peak"] == pytest.approx(best.x, rel=1e-5)
    decay = brentq(lambda x: y(x) - 0.02 * -best.fun, best.x, 10.0, xtol=1e-15)
    assert m["settling"] == pytest.approx(decay, rel=1e-8)
    assert "decays into ±2% of the peak" in step_text(m)


def _side_branch():
    """RC 1 kΩ / 159 nF with a 10 µF / 100 kΩ side branch on its output (a slow pole with 1 % of the step) and its
    H(s) from its nodal equations by hand (SymPy)."""
    import sympy as sp
    s, vo, vx = sp.symbols("s vo vx")
    r, cc, cx, rx = 10 ** 3, sp.Rational(159, 10 ** 9), sp.Rational(10, 10 ** 6), 10 ** 5
    sol = sp.solve([(1 - vo) / r - vo * s * cc - (vo - vx) * s * cx, (vo - vx) * s * cx - vx / rx], [vo, vx])
    c = Circuit()
    c.vsource("in", "0")
    c.r("in", "o", 1e3)
    c.c("o", "0", 159e-9)
    c.c("o", "x", 10e-6)
    c.r("x", "0", 1e5)
    return c, sol[vo], s


def _oracle_figures(y, final, t_hi):
    """Rise (10-90 %) and settling (±2 %) of a monotone step y by Brent's root finding on its closed form."""
    from scipy.optimize import brentq
    t10 = brentq(lambda x: y(x) / final - 0.1, 0.0, t_hi, xtol=1e-16)
    t90 = brentq(lambda x: y(x) / final - 0.9, 0.0, t_hi, xtol=1e-16)
    ts = brentq(lambda x: y(x) / final - 0.98, 0.0, t_hi, xtol=1e-16)
    return t90 - t10, ts


def test_a_slow_pole_with_a_small_share_does_not_stretch_a_fast_rise(monkeypatch):
    """Audit M-2: the window was STEP_SPAN over the slowest pole listed and the steps uniform, so an RC's 0.35 ms
    rise with a 1 % slow tail (a 10 µF / 100 kΩ side branch, τ ~1 s) fell in one or two BDF2 steps: rise 3.4 ms
    (+840 %). The exact step's rise and settling match SymPy's residues to 1e-7; the BDF2 fallback (exact_step
    refused) on its graded grid to 0.5 %."""
    from groundtruth.analysis import transfer
    c, h, s = _side_branch()
    final, y = _sym_step(h, s)
    rise, settle = _oracle_figures(y, final, 0.05)
    st = _gt_step(c, "o")
    assert st["method"] == "exact"
    m = st["metrics"]
    assert m["final"] == pytest.approx(final, rel=1e-12)
    assert m["rise"] == pytest.approx(rise, rel=1e-7) and m["settling"] == pytest.approx(settle, rel=1e-7)
    assert m["overshoot_pct"] == pytest.approx(0.0, abs=1e-9)
    assert np.max(np.abs(st["y"] - y(st["t"]))) < 1e-9
    monkeypatch.setattr(transfer, "exact_step", lambda *a, **k: None)
    st = _gt_step(c, "o")
    assert st["method"] == "bdf2"
    m = st["metrics"]
    assert m["rise"] == pytest.approx(rise, rel=5e-3) and m["settling"] == pytest.approx(settle, rel=5e-3)


def test_a_high_q_step_settles_when_its_ringing_does():
    """Audit M-8: a series RLC of Q 5000 needed more than STEP_MAX steps; on the capped grid it 'settled' in 4 ms
    instead of 1.24 s and overshot 74.6 % instead of 99.95 %. The exact step: overshoot e^(−πζ/√(1−ζ²)) to 1e-9,
    the rise (closed form, Brent) to 1e-9 and the settling time to 1 µs: the last exit from ±2 %, found on the closed
    form 1 − e^(−σt)(cos ω_d t + σ/ω_d sin ω_d t) within the last periods before its envelope's crossing; the drawn
    curve is capped with a note that its figures are exact."""
    from scipy.optimize import brentq
    lc, cc, q = 1e-3, 1e-6, 5000.0
    c = Circuit()
    c.vsource("in", "0")
    c.r("in", "a", math.sqrt(lc / cc) / q)
    c.l("a", "b", lc)
    c.c("b", "0", cc)
    st = _gt_step(c, "b")
    m = st["metrics"]
    w0 = 1 / math.sqrt(lc * cc)
    zeta = 1 / (2 * q)
    sig, wd = zeta * w0, w0 * math.sqrt(1 - zeta ** 2)

    def y(x):
        """The closed form."""
        return 1 - np.exp(-sig * x) * (np.cos(wd * x) + sig / wd * np.sin(wd * x))
    assert m["overshoot_pct"] == pytest.approx(100 * math.exp(-math.pi * zeta / math.sqrt(1 - zeta ** 2)), abs=1e-7)
    t10 = brentq(lambda x: y(x) - 0.1, 0, math.pi / wd)
    t90 = brentq(lambda x: y(x) - 0.9, 0, math.pi / wd)
    assert m["rise"] == pytest.approx(t90 - t10, rel=1e-9)
    t_env = math.log(math.sqrt(1 + (sig / wd) ** 2) / 0.02) / sig       # |y − 1| ≤ e^(−σt)·√(1 + (σ/ω_d)²)
    period = 2 * math.pi / wd
    td = np.linspace(t_env - 3 * period, t_env, 300001)
    i = int(np.nonzero(np.abs(y(td) - 1) > 0.02)[0][-1])
    exit_ = brentq(lambda x: abs(y(x) - 1) - 0.02, td[i], td[i + 1], xtol=1e-15)
    assert m["settling"] == pytest.approx(exit_, abs=1e-6)
    assert st["capped"] and "figures are exact" in st["note"]


def test_repeated_poles_step_exactly():
    """Identical RC sections buffered by unity VCVSs have one pole of multiplicity n (the QZ splits it by ~1e-8):
    exact_step merges and expands it [Ogata 2010]; n = 2: y = 1 − e^(−x)(1 + x), n = 3: 1 − e^(−x)(1 + x + x²/2),
    x = t/RC, to 1e-9."""
    for n in (2, 3):
        c = Circuit()
        c.vsource("in", "0")
        prev = "in"
        for i in range(n):
            c.r(prev, f"a{i}", 1e3)
            c.c(f"a{i}", "0", 1e-9)
            c.vcvs(f"b{i}", "0", f"a{i}", "0", 1.0)
            prev = f"b{i}"
        st = _gt_step(c, prev)
        assert st["method"] == "exact"
        x = st["t"] / 1e-6
        closed = 1 - np.exp(-x) * sum(x ** j / math.factorial(j) for j in range(n))
        assert np.max(np.abs(st["y"] - closed)) < 1e-9


def test_circuit_step_takes_a_graded_grid_and_refuses_a_bad_one():
    """Audit M-7: Circuit.step read only t[1] − t[0]: on a grid of two spacings its RC step was off by 0.005, on a
    log grid by 0.82. The variable-step BDF2 [Hairer 1993, §III.5] follows any increasing grid: on the graded grid
    of transfer.step_grid (h 1 µs doubling to 50 µs) and on a two-spacing grid within BDF2's error of
    1 − e^(−t/τ); a grid not from 0, or not increasing, is a ValueError."""
    from groundtruth.analysis.transfer import step_grid
    c = Circuit()
    c.vsource("in", "0")
    c.r("in", "o", 1e3)
    c.c("o", "0", 1e-6)
    t, capped = step_grid(12e-3, 1e-6, 50e-6)
    assert not capped and t[0] == 0 and t[-1] == pytest.approx(12e-3) and len(t) < 2000
    assert np.max(np.abs(c.step("o", t) - (1 - np.exp(-t / 1e-3)))) < 2e-4
    t = np.concatenate([np.linspace(0, 1e-3, 101), np.linspace(1.02e-3, 10e-3, 450)])
    assert np.max(np.abs(c.step("o", t) - (1 - np.exp(-t / 1e-3)))) < 2e-4
    for bad in (np.linspace(1e-6, 1e-3, 10), np.array([0.0, 2e-6, 1e-6, 3e-6])):
        with pytest.raises(ValueError):
            c.step("o", bad)


def test_random_ladders_step_as_their_state_space_does():
    """A seeded fuzz of the exact step (simulate._step_of) against an independent oracle: random R-L-C ladders of
    1-4 sections (series R + L, shunt C; values over decades) written as their state equations by hand
    (L·i' = v_prev − v − R·i, C·v' = i − i_next) and stepped by the matrix exponential, x(t) = A⁻¹(e^{At} − I)·B
    (scipy.linalg.expm) [Ogata 2010]: y at 40 times over the window to 1e-8 of the final value, the overshoot to
    1e-6 points and the rise and settling times against the oracle's on a 200 001-point grid to its resolution."""
    from scipy.linalg import expm
    rng = np.random.default_rng(20261104)
    for trial in range(25):
        n = int(rng.integers(1, 5))
        rs = 10 ** rng.uniform(0, 3, n)
        ls = 10 ** rng.uniform(-7, -4, n)
        cs = 10 ** rng.uniform(-9, -6, n)
        c = Circuit()
        c.vsource("in", "0")
        prev = "in"
        for i in range(n):
            c.r(prev, f"m{i}", float(rs[i]))
            c.l(f"m{i}", f"v{i}", float(ls[i]))
            c.c(f"v{i}", "0", float(cs[i]))
            prev = f"v{i}"
        st = _gt_step(c, prev)
        assert st is not None and st["method"] == "exact", trial
        # the oracle: states [i0, v0, i1, v1, …]
        a = np.zeros((2 * n, 2 * n))
        b = np.zeros(2 * n)
        for i in range(n):
            ii, vi = 2 * i, 2 * i + 1
            a[ii, ii] = -rs[i] / ls[i]
            a[ii, vi] = -1 / ls[i]
            if i:
                a[ii, 2 * i - 1] = 1 / ls[i]
            else:
                b[ii] = 1 / ls[i]
            a[vi, ii] = 1 / cs[i]
            if i + 1 < n:
                a[vi, 2 * i + 2] = -1 / cs[i]
        ainv_b = np.linalg.solve(a, b)
        t_end = float(st["t"][-1])
        idx = np.linspace(0, len(st["t"]) - 1, 40).astype(int)
        y_or = np.array([(np.linalg.solve(a, expm(a * x) @ b) - ainv_b)[-1] for x in st["t"][idx]])
        assert np.max(np.abs(st["y"][idx] - y_or)) < 1e-8, trial
        w, v = np.linalg.eig(a)
        coef = np.linalg.solve(v, b)

        def y_fast(x):
            """The oracle at many times through A's eigendecomposition (the same x(t), faster than expm)."""
            x = np.asarray(x, float)
            return ((v[-1] * coef / w)[None, :] * (np.exp(np.outer(x, w)) - 1)).sum(axis=1).real
        td = np.linspace(0, t_end, 200001)
        yd = y_fast(td)
        m = st["metrics"]
        assert m["final"] == pytest.approx(1.0, rel=1e-9)
        assert m["overshoot_pct"] == pytest.approx(max(yd.max() - 1, 0) * 100, abs=max(1e-6, 1e-3 * m["overshoot_pct"]))
        dt = td[1]
        t10, t90 = td[np.argmax(yd >= 0.1)], td[np.argmax(yd >= 0.9)]
        assert m["rise"] == pytest.approx(t90 - t10, abs=2 * dt), trial
        out = np.nonzero(np.abs(yd - 1) > 0.02)[0]
        assert m["settling"] == pytest.approx(td[out[-1]], abs=2 * dt + 1e-3 * m["settling"]), trial
