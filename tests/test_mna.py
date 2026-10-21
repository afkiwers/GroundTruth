"""The modified-nodal-analysis solver (emi/mna.py) against closed forms: passive networks, op-amp circuits of any
topology (inverting, non-inverting, Sallen-Key, multiple feedback, integrator, a three-op-amp instrumentation
amplifier), the exact poles and zeros, a line two-port. The user: "I want a generic model that can calculate any
circuit made from an OPA"."""
import math

import numpy as np
import pytest

from groundtruth.emi.mna import Circuit, OpAmpParams, line_y

TAU = 2 * math.pi


def _f0q(poles):
    """(f0 Hz, Q) of the complex pole pair among `poles` (rad/s)."""
    p = next(x for x in poles if x.imag > 1e-6 * abs(x))
    w0 = abs(p)
    return w0 / TAU, w0 / (-2 * p.real)


def test_an_rc_low_pass_and_its_one_pole():
    """1 kΩ, 10 nF: H = 1/(1 + sRC) at every frequency to 1e-12; one pole at −1/RC, no zeros, the normal form's k = 1."""
    ckt = Circuit()
    ckt.vsource("in", "0")
    ckt.r("in", "out", 1e3)
    ckt.c("out", "0", 10e-9)
    f = np.geomspace(1, 1e8, 50)
    assert np.allclose(ckt.transfer(f, "out"), 1 / (1 + 2j * math.pi * f * 1e-5), rtol=1e-12, atol=0)
    p, z, k = ckt.poles_zeros("out")
    assert len(z) == 0 and len(p) == 1 and p[0] == pytest.approx(-1e5, rel=1e-9) and k == pytest.approx(1.0)


def test_a_series_rlc_band_pass_and_a_capacitors_esl_notch():
    """Series R-L-C into a 50 Ω load: f0 = 1/(2π√LC), Q = √(L/C)/R_total, a zero at the origin; a 100 nF capacitor
    with 0.5 nH ESL and 16 mΩ ESR across a 50 Ω source: its notch at 1/(2π√(ESL·C)) (`part`)."""
    ckt = Circuit()
    ckt.vsource("in", "0")
    ckt.r("in", "a", 10.0)
    ckt.l("a", "b", 1e-6)
    ckt.c("b", "out", 1e-9)
    ckt.r("out", "0", 40.0)
    p, z, k = ckt.poles_zeros("out")
    f0, q = _f0q(p)
    assert f0 == pytest.approx(1 / (TAU * math.sqrt(1e-6 * 1e-9)), rel=1e-9)
    assert q == pytest.approx(math.sqrt(1e-6 / 1e-9) / 50.0, rel=1e-9)
    assert len(z) == 1 and z[0] == 0
    ckt = Circuit()
    ckt.vsource("in", "0")
    ckt.r("in", "out", 50.0)
    ckt.part("out", "0", "cap", {"C": 100e-9, "ESR": 0.016, "ESL": 0.5e-9}, "C1")
    p, z, k = ckt.poles_zeros("out")
    fz, _ = _f0q(z)
    assert fz == pytest.approx(1 / (TAU * math.sqrt(0.5e-9 * 100e-9)), rel=1e-9)


def _inverting(r1, r2, gbw, a0=1e5):
    """An inverting amplifier: in → R1 → inverting input, R2 feedback, + input to ground."""
    ckt = Circuit()
    ckt.vsource("in", "0")
    ckt.r("in", "m", r1)
    ckt.r("m", "out", r2)
    ckt.opamp("U1", "0", "m", "out", OpAmpParams(gbw=gbw, a0=a0, r_out=50.0, c_in=0.0))
    return ckt


def test_an_inverting_amplifier_has_its_gain_and_its_bandwidth():
    """R2/R1 = 10, GBW 1 MHz, A0 1e5, R_O 50 Ω: gain −10 (×1/(1 + 11/A0)), one pole at GBW·β + GBW/A0 with the
    feedback factor β = R1/(R1 + R2 + R_O) (R_O inside the divider): 90.51 kHz, ≈ GBW/(1 + R2/R1) [Franco 2015]
    (1e-6: exact for a one-pole op-amp)."""
    ckt = _inverting(1e3, 10e3, 1e6)
    h = ckt.transfer([10.0], "out")[0]
    assert h.real == pytest.approx(-10 / (1 + 11 / 1e5), rel=1e-6)
    p, z, k = ckt.poles_zeros("out")
    beta = 1e3 / (1e3 + 10e3 + 50.0)
    assert len(p) == 1 and -p[0].real / TAU == pytest.approx(1e6 * beta + 1e6 / 1e5, rel=1e-6)


def test_a_non_inverting_amplifier_and_a_follower():
    """1 + R2/R1 = 2 with GBW 10 MHz: gain 2, −3 dB at 5 MHz (0.2 %); a follower: gain 1, pole at GBW."""
    ckt = Circuit()
    ckt.vsource("in", "0")
    ckt.r("out", "m", 1e3)
    ckt.r("m", "0", 1e3)
    ckt.opamp("U1", "in", "m", "out", OpAmpParams(gbw=10e6, c_in=0.0))
    assert abs(ckt.transfer([1.0], "out")[0]) == pytest.approx(2.0, rel=1e-4)
    p, _, _ = ckt.poles_zeros("out")
    assert -p[0].real / TAU == pytest.approx(5e6, rel=2e-3)
    ckt = Circuit()
    ckt.vsource("in", "0")
    ckt.opamp("U1", "in", "out", "out", OpAmpParams(gbw=1e6, c_in=0.0))
    p, _, _ = ckt.poles_zeros("out")
    assert abs(ckt.transfer([1.0], "out")[0]) == pytest.approx(1.0, rel=1e-4)
    assert -p[0].real / TAU == pytest.approx(1e6, rel=2e-3)


def test_a_sallen_key_low_pass_has_its_f0_and_q():
    """Unity-gain Sallen-Key, R1 = R2 = 10 kΩ, C1 = 20 nF (to the output), C2 = 10 nF (to ground), a fast op-amp
    (GBW 100 MHz): f0 = 1/(2π√(R1R2C1C2)) = 1125 Hz, Q = √(R1R2C1C2)/(C2(R1 + R2)) = 0.707 (0.1 %) [Franco 2015]."""
    r1 = r2 = 10e3
    c1, c2 = 20e-9, 10e-9
    ckt = Circuit()
    ckt.vsource("in", "0")
    ckt.r("in", "a", r1)
    ckt.r("a", "b", r2)
    ckt.c("a", "out", c1)
    ckt.c("b", "0", c2)
    ckt.opamp("U1", "b", "out", "out", OpAmpParams(gbw=100e6, c_in=0.0))
    p, z, k = ckt.poles_zeros("out")
    f0, q = _f0q(p)
    assert f0 == pytest.approx(1 / (TAU * math.sqrt(r1 * r2 * c1 * c2)), rel=1e-3)
    assert q == pytest.approx(math.sqrt(r1 * r2 * c1 * c2) / (c2 * (r1 + r2)), rel=1e-3)
    assert abs(ckt.transfer([1.0], "out")[0]) == pytest.approx(1.0, rel=1e-4)


def test_a_multiple_feedback_low_pass_has_its_gain_f0_and_q():
    """MFB low-pass: R1 in → a, R2 a → out, R3 a → b (the inverting input), C1 a → ground, C2 b → out; GBW 100 MHz:
    gain −R2/R1, f0 = 1/(2π√(R2R3C1C2)), Q = √(C1/C2)·√(R2R3)/(R2 + R3 + R2R3/R1) (0.1 %) [Franco 2015]."""
    r1, r2, r3, c1, c2 = 10e3, 20e3, 15e3, 47e-9, 2.2e-9
    ckt = Circuit()
    ckt.vsource("in", "0")
    ckt.r("in", "a", r1)
    ckt.r("a", "out", r2)
    ckt.r("a", "b", r3)
    ckt.c("a", "0", c1)
    ckt.c("b", "out", c2)
    ckt.opamp("U1", "0", "b", "out", OpAmpParams(gbw=100e6, c_in=0.0))
    assert ckt.transfer([0.1], "out")[0].real == pytest.approx(-r2 / r1, rel=1e-3)
    f0, q = _f0q(ckt.poles_zeros("out")[0])
    assert f0 == pytest.approx(1 / (TAU * math.sqrt(r2 * r3 * c1 * c2)), rel=1e-3)
    assert q == pytest.approx(math.sqrt(c1 / c2) * math.sqrt(r2 * r3) / (r2 + r3 + r2 * r3 / r1), rel=1e-3)


def test_an_integrator_and_a_three_opamp_instrumentation_amplifier():
    """An inverting integrator (10 kΩ, 100 nF): −1/(sRC) between A0's corner and GBW (1 %); an instrumentation
    amplifier (two followers with gain 1 + 2R/Rg, then a difference stage of gain 1), R = 10 kΩ, Rg = 1 kΩ: the
    differential gain 21 (0.1 %), both inputs together: ~0 (ideal resistors)."""
    ckt = Circuit()
    ckt.vsource("in", "0")
    ckt.r("in", "m", 10e3)
    ckt.c("m", "out", 100e-9)
    ckt.opamp("U1", "0", "m", "out", OpAmpParams(gbw=10e6, c_in=0.0))
    f = np.array([10.0, 100.0, 1e3])
    assert np.allclose(ckt.transfer(f, "out"), -1 / (2j * math.pi * f * 1e-3), rtol=0.01)

    def ina(vp, vn):
        """The instrumentation amplifier with its inputs on these source nodes."""
        c = Circuit()
        c.vsource("s", "0")
        c.r("s", "p", 1e-3) if vp else c.r("p", "0", 1e-3)
        c.r("s", "n", 1e-3) if vn else c.r("n", "0", 1e-3)
        prm = OpAmpParams(gbw=10e6, c_in=0.0)
        c.opamp("A1", "p", "x1", "o1", prm)
        c.opamp("A2", "n", "x2", "o2", prm)
        c.r("o1", "x1", 10e3)
        c.r("x1", "x2", 1e3)
        c.r("x2", "o2", 10e3)
        c.r("o1", "d1", 10e3)            # difference stage, gain 1
        c.r("d1", "out", 10e3)
        c.r("o2", "d2", 10e3)
        c.r("d2", "0", 10e3)
        c.opamp("A3", "d2", "d1", "out", prm)
        return c.transfer([10.0], "out")[0]
    assert abs(ina(True, False)) == pytest.approx(21.0, rel=1e-3)
    assert abs(ina(True, True)) < 1e-3


def test_a_followers_second_pole_shows_its_phase_margin():
    """A follower (GBW 10 MHz) with a second pole at 10 MHz: the closed loop's pole pair has Q = 1 (ω1·ω2 = GBW·fp2,
    two equal poles: Q = √(GBW·fp2)/(fp2) → 1), its peaking +1.25 dB; with the second pole at 100 MHz it hardly
    peaks."""
    def follower(fp2):
        """A follower with a second pole at fp2."""
        c = Circuit()
        c.vsource("in", "0")
        c.opamp("U1", "in", "out", "out", OpAmpParams(gbw=10e6, fp2=fp2, c_in=0.0))
        return c
    _, q = _f0q(follower(10e6).poles_zeros("out")[0])
    assert q == pytest.approx(1.0, rel=2e-3)
    f = np.geomspace(1e5, 1e8, 600)
    assert 20 * np.log10(np.abs(follower(10e6).transfer(f, "out"))).max() == pytest.approx(1.25, abs=0.02)
    assert 20 * np.log10(np.abs(follower(100e6).transfer(f, "out"))).max() < 0.05


def test_a_line_two_port_open_at_its_end_is_its_capacitance():
    """An 0.1 m, 50 Ω, εeff 4 lossless line, open at its end, driven through 1 kΩ: at 1 kHz the line's whole
    capacitance l·√εeff/(c·Z0) loads the source (0.1 %); the circuit then has no exact poles (ValueError)."""
    from groundtruth.emi.path import PathLine
    from groundtruth.emi.spectrum import C0
    line = PathLine(50.0, 4.0, 0.1)
    ckt = Circuit()
    ckt.vsource("in", "0")
    ckt.r("in", "a", 1e3)
    ckt.y2("a", "0", "b", "0", line_y(line.zc, line.gamma_l))
    cl = 0.1 * 2.0 / (C0 * 50.0)
    f = np.array([1e3, 1e4])
    assert np.allclose(ckt.transfer(f, "b"), 1 / (1 + 2j * math.pi * f * 1e3 * cl), rtol=1e-3)
    with pytest.raises(ValueError):
        ckt.poles_zeros("b")


def test_the_loop_gain_and_phase_margin_of_followers_and_an_inverting_stage():
    """loop_gain broken at the op-amp's ideal output: a follower with one pole: |T| = A0 at DC, crossover at GBW,
    90° of phase margin; with a second pole at GBW: crossover at 0.786·GBW (x·√(1 + x²) = 1) and
    90° − atan(0.786) = 51.8°; an inverting stage of gain 10: T = A0/11 at DC, crossover GBW/11 [Franco 2015]."""
    from groundtruth.analysis.transfer import phase_margin

    def follower(fp2=None):
        """A follower (GBW 1 MHz, A0 1e5, R_O 1 mΩ)."""
        c = Circuit()
        c.vsource("in", "0")
        c.opamp("U1", "in", "out", "out", OpAmpParams(gbw=1e6, a0=1e5, fp2=fp2, r_out=1e-3, c_in=0.0))
        return c
    f = np.geomspace(1, 1e8, 2000)
    t = follower().loop_gain("U1", f)
    assert abs(t[0]) == pytest.approx(1e5 / math.hypot(1, 1 / 10.0), rel=1e-6)      # its open-loop pole at GBW/A0 = 10 Hz
    pm = phase_margin(f, t)
    assert pm["f_c"] == pytest.approx(1e6, rel=0.01) and pm["pm"] == pytest.approx(90.0, abs=0.5)
    pm = phase_margin(f, follower(1e6).loop_gain("U1", f))
    x = math.sqrt((math.sqrt(5) - 1) / 2)
    assert pm["f_c"] == pytest.approx(x * 1e6, rel=0.01)
    assert pm["pm"] == pytest.approx(90 - math.degrees(math.atan(x)), abs=0.5)
    inv = _inverting(1e3, 10e3, 1e6)
    t = inv.loop_gain("U1", f)
    beta = 1e3 / (1e3 + 10e3 + 50.0)
    assert abs(t[0]) == pytest.approx(1e5 * beta / math.hypot(1, 1 / 10.0), rel=1e-6)
    assert phase_margin(f, t)["f_c"] == pytest.approx(1e6 * beta, rel=0.01)
