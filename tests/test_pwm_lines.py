"""The line spectrum of a period-to-period varying PWM current and the receiver reading it (emi/pwm_lines.py),
against closed forms."""
import math

import numpy as np
import pytest

from groundtruth.analysis.power import ripple_harmonics
from groundtruth.emi.pwm_lines import line_spectrum, receiver
from groundtruth.emi.spectrum import trapezoid_harmonics

F_PWM, T = 20e3, 1 / 20e3


def _pulses(n, amps, D=0.3, t1=0.05):
    """n PWM periods, period m a pulse of amps[m] from t1·T to (t1 + D)·T (steps: the edges become ramps)."""
    t, y = [], []
    for m in range(n):
        b = m * T
        t += [b, b + t1 * T, b + t1 * T, b + (t1 + D) * T, b + (t1 + D) * T]
        y += [0.0, 0.0, amps[m], amps[m], 0.0]
    return np.array(t), np.array(y)


def test_a_constant_pwm_train_is_the_trapezoid_series_with_no_sidebands():
    """S1. Equal pulses in every period: only the PWM harmonics, each 2AD|sinc(nD)||sinc(n f0 tr)|; the lines
    between them (every f_pwm/6 here) vanish."""
    n, A, D, tr = 6, 10.0, 0.3, 50e-9
    t, y = _pulses(n, [A] * n, D)
    f, ph, mean = line_spectrum(t, y, n * T, T, tr, 2e6)
    assert mean == pytest.approx(A * D, rel=1e-9)
    fh, ref = trapezoid_harmonics(F_PWM, tr, A, D, f_max=2e6)
    at = np.rint(fh / (F_PWM / n)).astype(int) - 1            # the reference leaves out its zeros: match by f
    assert np.abs(ph[at]) == pytest.approx(ref, rel=1e-9)
    side = np.delete(np.abs(ph), np.arange(n - 1, len(ph), n))
    assert side.max() < 1e-12 * A


def test_a_triangle_is_the_ripple_series():
    """S5. A current rising by dI over D·T and falling over the rest: dI·|sin(n pi D)| / (pi^2 n^2 D (1-D))."""
    D, dI = 0.35, 2.0
    t, y = np.array([0.0, D * T]), np.array([0.0, dI])
    f, ph, mean = line_spectrum(t, y, T, T, 1e-9, 1e6)
    fr, ref = ripple_harmonics(F_PWM, dI, D, 1e6)
    at = np.rint(fr / F_PWM).astype(int) - 1                  # matched by frequency (the zeros are left out)
    assert np.abs(ph[at]) == pytest.approx(ref, rel=1e-9)
    assert mean == pytest.approx(dI / 2)


def test_parseval_and_the_grouping_against_the_direct_sum():
    """S3, S4. A pulse train whose amplitude follows a sine over 24 periods with a few odd breakpoints in between:
    the FFT over the periods gives the same lines as summing every breakpoint directly, and the power in the lines
    is the waveform's mean square (slow edges, so the series converges)."""
    n = 24
    rng = np.random.default_rng(1)
    amps = 5 + 3 * np.sin(2 * np.pi * np.arange(n) / n)
    t, y = _pulses(n, amps, 0.4)
    extra_t = np.sort(rng.uniform(0, n * T, 5))
    t = np.concatenate([t, extra_t])
    y = np.concatenate([y, np.zeros(5)])
    order = np.argsort(t, kind="stable")
    t, y = t[order], y[order]
    tr = 2e-6
    f, ph, mean = line_spectrum(t, y, n * T, T, tr, 3e6)
    # the direct sum over every breakpoint, the same formula without the grouping
    from groundtruth.emi.pwm_lines import jumps_to_ramps
    tt, yy = jumps_to_ramps(t, y, tr)
    tc = np.concatenate([tt, [tt[0] + n * T]])
    yc = np.concatenate([yy, [yy[0]]])
    slope = np.diff(yc) / np.diff(tc)
    ds = slope - np.roll(slope, 1)
    w = 2 * np.pi * f[:400]
    direct = -2 * (ds[None, :] * np.exp(-1j * w[:, None] * tt[None, :])).sum(axis=1) / (n * T * w * w)
    assert np.abs(ph[:400] - direct).max() < 1e-10 * np.abs(direct).max()
    ms = float(((yc[:-1] ** 2 + yc[:-1] * yc[1:] + yc[1:] ** 2) / 3 * np.diff(tc)).sum() / (n * T))
    assert mean ** 2 + (np.abs(ph) ** 2 / 2).sum() == pytest.approx(ms, rel=1e-4)


def test_a_drive_with_coincident_breakpoints_has_the_sampled_spectrum():
    """S6. A six-step drive at speed records three breakpoints at one instant where an open phase starts to
    conduct at a switching edge: they are one jump. Its low lines (f_e, 2·f_e, ...) and its Parseval sum match the
    FFT of the waveform sampled finely (a ramp squeezed to a few float steps between them used to be off by 20×)."""
    from groundtruth.analysis.motor import six_step
    from groundtruth.config.settings import BridgeSpec, LegSpec, MotorSpec
    from groundtruth.emi.pwm_lines import jumps_to_ramps
    b = BridgeSpec("b", "V", "GND", legs=[LegSpec(ph, f"PH{ph}", ["Q1"], ["Q2"]) for ph in "ABC"], v_bus=48.0,
                   f_pwm=40e3, i_phase=30.0, speed_rpm=3000.0, motor=MotorSpec("star", 0.05, 50e-6, 5.0, 7))
    w = six_step(b)
    t = w.t
    assert any(t[j] == t[j - 1] == t[j - 2] for j in range(2, len(t)))
    y = w.hs[:, 0]
    f, ph, mean = line_spectrum(t, y, w.period, w.T, b.rise_time, 3 * b.f_pwm)
    tt, yy = jumps_to_ramps(t, y, b.rise_time)
    ts = np.linspace(0, w.period, 2 ** 20, endpoint=False)
    sampled = np.fft.rfft(np.interp(ts, tt, yy, period=w.period)) / len(ts) * 2
    for line in (1, 2, 3, 7, w.n // 2):
        assert abs(ph[line - 1]) == pytest.approx(abs(sampled[line]), rel=1e-3, abs=1e-4)
    f, ph, mean = line_spectrum(t, y, w.period, w.T, b.rise_time, 400 * b.f_pwm)
    assert mean ** 2 + (np.abs(ph) ** 2 / 2).sum() == pytest.approx(w._avg(w.hs, True)[0], rel=1e-3)


def test_the_receiver_reads_one_line_and_two_lines_as_they_add():
    """R1, R2. One line of amplitude A: A when tuned on it, A/2 (-6 dB) RBW/2 away. Two equal lines RBW/10 apart
    tuned between them: their beat, peak 2A·G, average (4/pi)·A·G (G: the filter at RBW/20)."""
    rbw, A, fc = 9e3, 1.0, 1e6
    one = receiver(np.array([fc]), np.array([A + 0j]), 100.0, np.array([fc, fc + rbw / 2]), lambda f: rbw, "peak")
    assert one == pytest.approx([A, A / 2], rel=1e-9)
    step = rbw / 10
    f = np.array([fc - step / 2, fc + step / 2])
    ph = np.array([A + 0j, A + 0j])
    G = math.exp(-math.log(2) * (2 * (step / 2) / rbw) ** 2)
    pk = receiver(f, ph, step, np.array([fc]), lambda x: rbw, "peak")[0]
    av = receiver(f, ph, step, np.array([fc]), lambda x: rbw, "average")[0]
    assert pk == pytest.approx(2 * A * G, rel=1e-6)
    assert av == pytest.approx(4 / math.pi * A * G, rel=0.02)
