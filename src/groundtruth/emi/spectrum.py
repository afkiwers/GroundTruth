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
    """Hammerstad/Wheeler microstrip: (Z0, effective εr). w, h, t in any consistent unit."""
    if t > 0 and w > 0:
        w = w + (t / math.pi) * (1 + math.log(2 * h / t))  # finite-thickness width correction
    u = w / h
    # the effective permittivity: part of the field runs in air above the trace, so eps_eff lies between 1 and
    # eps_r; narrow and wide traces have their own closed-form fits (Hammerstad 1975), accurate to ~1 %
    if u <= 1:
        eeff = (er + 1) / 2 + (er - 1) / 2 * ((1 + 12 / u) ** -0.5 + 0.04 * (1 - u) ** 2)
        return 60 / math.sqrt(eeff) * math.log(8 / u + u / 4), eeff
    eeff = (er + 1) / 2 + (er - 1) / 2 * (1 + 12 / u) ** -0.5
    return 120 * math.pi / (math.sqrt(eeff) * (u + 1.393 + 0.667 * math.log(u + 1.444))), eeff


def microstrip_z0(w: float, h: float, er: float, t: float = 35e-6) -> float:
    """The microstrip's characteristic impedance alone (see microstrip)."""
    return microstrip(w, h, er, t)[0]


def stripline(w: float, b: float, er: float, t: float = 35e-6) -> tuple[float, float]:
    """Symmetric stripline, b = plane-to-plane spacing: (Z0, εr)."""
    # IPC-2141's closed form; the field is wholly in the dielectric, so eps_eff = eps_r
    return 60 / math.sqrt(er) * math.log(4 * b / (0.67 * math.pi * (0.8 * w + t))), er


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
