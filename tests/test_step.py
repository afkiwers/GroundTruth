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
