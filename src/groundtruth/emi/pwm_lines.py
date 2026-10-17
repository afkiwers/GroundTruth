"""The line spectrum of a PWM current that changes from period to period (a six-step drive's switch and bus
currents), and an EMI receiver reading it.

The current is periodic in the electrical period T_e = n·T (n PWM periods), so its lines lie on the grid
f_l = l / T_e: the PWM harmonics with sidebands every electrical frequency. It is taken as piecewise linear between
breakpoints (switching edges as ramps of the edge time): then its second derivative is a train of impulses, the
slope changes ds_j at t_j, and its Fourier coefficients are exactly

    c_l = -1 / (T_e (2 pi f_l)^2) · sum_j ds_j · exp(-j 2 pi f_l t_j)

Most breakpoints sit at the same few places within every PWM period (the switching instants, since the duty is
constant); their sum over the periods is an FFT over the period index, so millions of lines cost little. The rest
(a freewheeling current running out during a commutation) are summed directly.

A receiver tuned to f_c passes the lines through its resolution bandwidth (Gaussian, -6 dB at ±RBW/2, as
emi/spread.py); the envelope of what passes is periodic in T_e, computed by an inverse FFT, and read by the peak
detector (its maximum) or the average detector (its mean). Lines closer than the RBW therefore add as the
waveform makes them add, not as the sum of their magnitudes.
"""
from __future__ import annotations

import math
from typing import Callable, Optional

import numpy as np

MAX_LINES = 2_000_000


def jumps_to_ramps(t: np.ndarray, y: np.ndarray, tr: float) -> tuple[np.ndarray, np.ndarray]:
    """Breakpoints with a step (two values at one time) turned into a ramp of the edge time after it (shortened
    to half the gap when the next breakpoint is nearer). Three or more breakpoints at one time are one jump, from
    the first value to the last: a ramp squeezed between them would be a few float steps long, and its slope
    change, divided by (2πf)², ruins the low lines."""
    t = np.asarray(t, float)
    y = np.asarray(y, float)
    keep = np.ones(len(t), bool)
    keep[1:-1] = ~((t[1:-1] == t[:-2]) & (t[1:-1] == t[2:]))     # drop the middle ones of 3+ at one time
    t, y = t[keep].copy(), y[keep]
    # a breakpoint at (or before) its predecessor's time is a step: move it later by the edge time (or half the
    # gap to the next breakpoint), so every step becomes a ramp and the slopes stay finite
    for j in range(1, len(t)):
        if t[j] <= t[j - 1]:
            nxt = t[j + 1] if j + 1 < len(t) else t[j - 1] + tr * 4
            t[j] = t[j - 1] + max(min(tr, (nxt - t[j - 1]) / 2), 1e-18)
    return t, y


def line_spectrum(t, y, period: float, pwm_period: float, tr: float, f_max: float
                  ) -> tuple[np.ndarray, np.ndarray, float]:
    """(line frequencies, complex peak phasors 2·c_l, mean) of a periodic current given at breakpoints over one
    period [0, period) (steps allowed: they become ramps of the edge time tr). Lines up to f_max."""
    T_e = float(period)
    t, y = np.asarray(t, float), np.asarray(y, float)
    if len(t) > 1 and t[-1] >= t[0] + T_e * (1 - 1e-12):
        # a breakpoint on the period's end: its value is the one just before the start, a jump there (its two slope
        # changes at t0 and t0 + T_e would cancel); as a breakpoint at the start it becomes a ramp like any jump
        t, y = np.concatenate([[t[0]], t[:-1]]), np.concatenate([[y[-1]], y[:-1]])
    t, y = jumps_to_ramps(t, y, tr)
    # close the period: the waveform repeats, the last breakpoint joins the first one shifted by T_e
    tt = np.concatenate([t, [t[0] + T_e]])
    yy = np.concatenate([y, [y[0]]])
    dt = np.diff(tt)
    dt[dt <= 0] = 1e-18
    slope = np.diff(yy) / dt
    ds = slope - np.roll(slope, 1)             # change of slope at each breakpoint t[j]
    mean = float(((yy[:-1] + yy[1:]) / 2 * np.diff(tt)).sum() / T_e)
    f_e = 1.0 / T_e
    n_lines = int(min(math.floor(f_max / f_e), MAX_LINES))
    if n_lines < 1:
        return np.zeros(0), np.zeros(0, complex), mean
    # Split each breakpoint time into its PWM period m and its offset tau within that period. Offsets shared by
    # many periods ("regular": the switching instants) are grouped: for such a group the sum over its
    # breakpoints, sum_m ds_m e^(-j w (m T + tau)), is e^(-j w tau) times a DFT over m (w = 2 pi l / T_e makes
    # e^(-j w m T) = e^(-j 2 pi l m / n)), so one FFT per group gives it for every line l. Offsets are compared on
    # a 2^-24 grid of the period (float noise would split a group otherwise).
    n = max(int(round(T_e / pwm_period)), 1)
    m = np.minimum(np.floor(t / pwm_period + 1e-9).astype(int), n - 1)
    tau = t - m * pwm_period
    key = np.round(tau / pwm_period * 2 ** 24).astype(np.int64)
    keys, inverse, counts = np.unique(key, return_inverse=True, return_counts=True)
    regular = counts >= max(3, n // 12)
    out = np.zeros(n_lines, complex)
    lines = np.arange(1, n_lines + 1)
    chunk = 200_000
    reg_ids = np.nonzero(regular)[0]
    F = {}
    for r in reg_ids:
        sel = inverse == r
        A = np.zeros(n)
        np.add.at(A, m[sel], ds[sel])
        F[r] = np.fft.fft(A)                    # sum_m A[m] e^{-j 2 pi k m / n}
    taus = {r: float(np.mean(tau[inverse == r])) for r in reg_ids}
    free = ~regular[inverse]                 # the irregular breakpoints: summed directly per line
    tf, dsf = t[free], ds[free]
    for a in range(0, n_lines, chunk):           # in chunks of lines, so the work arrays stay small
        ls = lines[a:a + chunk]
        w = 2 * np.pi * ls * f_e
        acc = np.zeros(len(ls), complex)
        for r in reg_ids:
            acc += np.exp(-1j * w * taus[r]) * F[r][ls % n]
        for tj, dj in zip(tf, dsf):
            acc += dj * np.exp(-1j * w * tj)
        out[a:a + chunk] = -acc / (T_e * w * w)         # c_l = -1/(T_e w^2) sum ds_j e^(-j w t_j)
    return lines * f_e, 2 * out, mean


def _gauss(df: np.ndarray, rbw: float) -> np.ndarray:
    """The receiver filter's amplitude response at offset df: Gaussian, 1/2 (-6 dB) at df = ±rbw/2."""
    return np.exp(-math.log(2) * (2 * df / rbw) ** 2)


def receiver(freqs: np.ndarray, phasors: np.ndarray, f_step: float, tunings: np.ndarray,
             rbw: Callable[[float], float], det: str) -> np.ndarray:
    """Peak-amplitude readings at the tunings of a receiver with a Gaussian resolution bandwidth rbw(f) and the
    peak or average detector, for lines (freqs, complex peak phasors) on a grid of f_step."""
    out = np.zeros(len(tunings))
    if len(freqs) == 0:
        return out
    idx = np.rint(freqs / f_step).astype(np.int64)
    for k, fc in enumerate(tunings):
        bw = rbw(fc)
        lo, hi = np.searchsorted(freqs, [fc - 2 * bw, fc + 2 * bw])
        if hi <= lo:
            continue
        g = _gauss(freqs[lo:hi] - fc, bw) * phasors[lo:hi]      # the lines within +-2 RBW, filtered
        if hi - lo == 1:
            out[k] = abs(g[0])
            continue
        # The filtered lines are on a grid of f_step: placed in FFT bins (shifted to start at bin 0), their inverse
        # FFT is the complex envelope over one period of f_step, and its magnitude what the detector sees. The
        # peak detector gets twice the bins (finer time samples, so the maximum is not missed between samples).
        span = int(idx[hi - 1] - idx[lo]) + 1
        size = 1 << max(int(span - 1).bit_length(), 3)
        size = max(size, 2 * span) if det == "peak" else size
        bins = np.zeros(size, complex)
        np.add.at(bins, (idx[lo:hi] - idx[lo]) % size, g)
        env = np.abs(np.fft.ifft(bins) * size)
        out[k] = env.max() if det == "peak" else env.mean()
    return out


def tunings_for(lo: float, hi: float, rbw: Callable[[float], float], f_pwm: Optional[float] = None) -> np.ndarray:
    """Receiver tunings from lo to hi in steps of a quarter of the bandwidth (and of the PWM frequency, so no
    harmonic group falls between two tunings)."""
    out, f = [], lo
    while f <= hi:
        out.append(f)
        step = rbw(f) / 4
        if f_pwm:
            step = min(step, f_pwm / 4)
        f += step
    return np.array(out)
