"""Harmonic content of a trapezoidal digital signal, trace impedance, and the current on the line."""
from __future__ import annotations

import math
from typing import Optional

import numpy as np

C0 = 299_792_458.0


def trapezoid_harmonics(f0: float, rise_time: float, amplitude: float, duty: float = 0.5,
                        f_max: float = 6e9, fall_time: Optional[float] = None) -> tuple[np.ndarray, np.ndarray]:
    """Frequencies and peak amplitudes of the harmonics of a trapezoidal wave.

    Equal edges: c_n = 2·A·D·|sinc(nπD)|·|sinc(nπ·f0·tr)|. A different fall time tf (duty D at half amplitude):
    c_n = 2·|X_n|, X_n = A/(j2πn)·[sinc(n·f0·tr) - e^(-j2πnD)·sinc(n·f0·tf)] (the edges' derivative, integrated);
    it reduces to the first form when tf = tr.
    """
    n_max = max(1, int(f_max // f0))
    n = np.arange(1, n_max + 1)
    # np.sinc(x) is sin(pi x)/(pi x), so np.sinc(n*D) is the textbook sinc(n pi D). The first factor is the
    # rectangular pulse's envelope (falls 20 dB/decade above 1/(pi tau)), the second the edges' (another 20
    # dB/decade above 1/(pi tr)): the familiar two-corner envelope of a digital signal.
    if fall_time is None or fall_time == rise_time:
        a = 2 * amplitude * duty * np.abs(np.sinc(n * duty)) * np.abs(np.sinc(n * f0 * rise_time))
    else:
        x = amplitude / (2j * np.pi * n) * (np.sinc(n * f0 * rise_time)
                                           - np.exp(-2j * np.pi * n * duty) * np.sinc(n * f0 * fall_time))
        a = 2 * np.abs(x)
    keep = a > amplitude * 1e-9     # harmonics at the nulls of the sinc (even ones at 50 % duty) are dropped
    return n[keep] * f0, a[keep]


def analysis_band(f0: float, rise_time: float) -> float:
    """Highest frequency worth evaluating: a decade above the second corner (1/π·tr), capped at 6 GHz."""
    return min(6e9, max(10 / (math.pi * rise_time), 3 * f0))


def microstrip(w: float, h: float, er: float, t: float = 35e-6) -> tuple[float, float]:
    """Hammerstad & Jensen (1980) microstrip with their thickness corrections: (Z0, effective εr). w, h, t in any
    consistent unit.

    Z01(u) = 60·ln(f(u)/u + √(1 + (2/u)²)) is the line in air, εe(u) the effective permittivity's fit; a thickness t
    widens the strip by Δu1 = (t/h)/π·ln(1 + 4e/((t/h)·coth²√(6.517u))) in air and by Δur = ½(1 + sech√(εr − 1))·Δu1
    with the dielectric, so Z0 = Z01(u_r)/√εe(u_r) and εeff = εe(u_r)·(Z01(u_1)/Z01(u_r))² (a thick strip's side
    walls hold field in the air: εeff falls). Against the 2D field solver (tests/test_validation_audit.py SP-2):
    Z0 within 0.8 %, εeff within 1.8 % for w/h ≥ 0.3 and t up to 70 µm. (The single-correction Hammerstad form used
    before 2026-10-17 read Z0 up to 4.5 % low and εeff up to 26 % high for thick copper.)"""
    def z01(u: float) -> float:
        """The line's impedance in air at width/height u."""
        f = 6 + (2 * math.pi - 6) * math.exp(-(30.666 / u) ** 0.7528)
        return 60 * math.log(f / u + math.sqrt(1 + (2 / u) ** 2))

    def e_e(u: float) -> float:
        """The effective permittivity of a zero-thickness strip at width/height u."""
        a = 1 + math.log((u ** 4 + (u / 52) ** 2) / (u ** 4 + 0.432)) / 49 + math.log(1 + (u / 18.1) ** 3) / 18.7
        b = 0.564 * ((er - 0.9) / (er + 3)) ** 0.053
        return (er + 1) / 2 + (er - 1) / 2 * (1 + 10 / u) ** (-a * b)
    u = w / h
    if t > 0 and w > 0:
        th = t / h
        du1 = th / math.pi * math.log(1 + 4 * math.e / (th / math.tanh(math.sqrt(6.517 * u)) ** 2))
        dur = 0.5 * (1 + 1 / math.cosh(math.sqrt(max(er - 1, 0.0)))) * du1
    else:
        du1 = dur = 0.0
    u1, ur = u + du1, u + dur
    return z01(ur) / math.sqrt(e_e(ur)), e_e(ur) * (z01(u1) / z01(ur)) ** 2


def microstrip_z0(w: float, h: float, er: float, t: float = 35e-6) -> float:
    """The microstrip's characteristic impedance alone (see microstrip)."""
    return microstrip(w, h, er, t)[0]


def stripline(w: float, b: float, er: float, t: float = 35e-6) -> tuple[float, float]:
    """Symmetric stripline, b = plane-to-plane spacing: (Z0, εr). Wheeler (1978, Wadell's Transmission Line Design
    Handbook 3.5.1) with the strip's thickness:

    Z0 = 30/√εr · ln{1 + 4/(πW')·[8/(πW') + √((8/(πW'))² + 6.27)]},  W' = (w + Δw)/(b − t),
    Δw/(b − t) = x/(π(1 − x))·{1 − ½·ln[(x/(2 − x))² + (0.0796x/(w/b + 1.1x))^m]},  m = 2/(1 + ⅔·x/(1 − x)), x = t/b.

    Against the 2D field solver within 0.5 % for w/b from 0.1 to 4 and t from 5 to 35 µm (tests/test_validation_
    audit.py SP-2). The field is wholly in the dielectric, so εeff = εr. (IPC-2141's ln(4b/(0.67π(0.8w + t))) used
    before 2026-10-17 holds only for w/b < 0.35: −12 % at w/b 0.8, and negative from w/b ≈ 2.5 on.)"""
    tt = min(max(t, 0.0), 0.9 * b)
    x = tt / b
    if x > 0:
        m = 2 / (1 + 2 / 3 * x / (1 - x))
        dw = (b - tt) * x / (math.pi * (1 - x)) * (1 - 0.5 * math.log((x / (2 - x)) ** 2
                                                                       + (0.0796 * x / (w / b + 1.1 * x)) ** m))
    else:
        dw = 0.0
    wp = (w + dw) / (b - tt)
    a = 8 / (math.pi * wp)
    return 30 / math.sqrt(er) * math.log(1 + 4 / (math.pi * wp) * (a + math.sqrt(a * a + 6.27))), er


def offset_stripline(w: float, h1: float, h2: float, er: float, t: float = 35e-6) -> tuple[float, float]:
    """A stripline between two planes that is not centred: h1, h2 the dielectric heights from the strip's faces to
    each plane, (Z0, εr). The usual combination of the two symmetric striplines it lies between (each with the strip
    h_i from both planes, b_i = 2·h_i + t): Z0 = 2·Z(b1)·Z(b2)/(Z(b1) + Z(b2)) (Wadell 3.5.3; Cohn). Against the 2D
    field solver (tests/test_validation_audit.py SP-2): within 1.5 % while the nearer plane is at least 0.35 of the
    spacing away, 5.5 % at 0.2, 10 % (high) at 0.1. Centred (h1 = h2) it is stripline() exactly."""
    z1 = stripline(w, 2 * h1 + t, er, t)[0]
    z2 = stripline(w, 2 * h2 + t, er, t)[0]
    return 2 * z1 * z2 / (z1 + z2), er


def line_current(freqs, v_source, source_ohms: float, z0: float, eeff: float, length_m: float,
                 load_farads: float) -> np.ndarray:
    """Average current along a lossless line driven by a source (amplitudes v_source, resistance
    source_ohms) and ended by a capacitive receiver load, per harmonic.

    Electrically short lines carry the capacitive charging current V/(Rs + 1/jωC_total); long lines
    approach V/(Rs + Z0); quarter-wave resonances of the open-ended line show up as current peaks.
    The average Ī = (1/l)∫I(x)dx is what drives the loop's magnetic moment.
    """
    f = np.asarray(freqs, float)
    w = 2 * np.pi * f
    beta_l = w * math.sqrt(eeff) / C0 * length_m             # electrical length (radians) of the line
    zl = 1.0 / (1j * w * max(load_farads, 1e-15))             # the receivers' capacitance as the load
    t = np.tan(beta_l)
    # the transmission-line input impedance of a line of length l ended in Z_L
    z_in = z0 * (zl + 1j * z0 * t) / (z0 + 1j * zl * t)
    i_in = np.asarray(v_source, float) / (source_ohms + z_in)
    v_in = i_in * z_in
    bl = np.maximum(beta_l, 1e-12)
    # On the line I(x) = I_in cos(beta x) - j (V_in / Z0) sin(beta x); its mean over the length is this closed
    # form (sin(bl)/bl -> 1 for a short line: the whole line carries I_in)
    i_avg = i_in * np.sin(bl) / bl - 1j * (v_in / z0) * (1 - np.cos(bl)) / bl
    return np.abs(i_avg)
