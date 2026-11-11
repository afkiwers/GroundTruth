"""Differential-mode radiated field estimate of a small current loop, and its limits: CISPR 32 class A or B, or a
curve the user gives (`RadiatedLimit`: MIL-STD-461 RE102 for their platform, a customer's own).

|E|max = η0·k²·|m| / (4π·r) · √(1 + 1/(k·r)²)     (V/m, free space, broadside [Balanis 2016, §5.2])

the far field (1.316e-14 · f² · |m| / r [Paul 2006]) with the small loop's induction term, which matters at
MIL-STD-461's 1 m below ~50 MHz (k·r = 1 at 48 MHz); at CISPR's 10 m from 30 MHz on it adds at most 0.11 dB.

|m| is the loop's magnetic dipole moment (A·m²) from the solver. A ground-plane reflection (as on
an OATS / semi-anechoic chamber) can add up to +6 dB; it is included by default [A7].
This is a ranking estimate for one net's loop, differential mode only [A11]. Common-mode currents on cables usually
dominate real emissions, but return-path detours are the main thing that drives those currents.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional, Union

import numpy as np

C0 = 299_792_458.0

# CISPR 32 radiated emission limits (dBµV/m) [CISPR 32, Annex A]: (f_lo, f_hi, limit, standard distance m)
# Below 1 GHz: quasi-peak at 10 m. Above 1 GHz: average at 3 m.
CISPR32 = {
    "B": [(30e6, 230e6, 30.0, 10.0), (230e6, 1e9, 37.0, 10.0), (1e9, 3e9, 50.0, 3.0), (3e9, 6e9, 54.0, 3.0)],
    "A": [(30e6, 230e6, 40.0, 10.0), (230e6, 1e9, 47.0, 10.0), (1e9, 3e9, 56.0, 3.0), (3e9, 6e9, 60.0, 3.0)],
}


@dataclass(frozen=True)
class RadiatedLimit:
    """A radiated-emission limit the user gives (settings `analysis: radiated_limit`): its name for the reports, the
    curve as (frequency Hz, dBµV/m) points, straight on a log-frequency axis between them and no limit outside, the
    measuring distance, and the receiver's bandwidth table ("mil461": MIL-STD-461G Table II; "cispr": CISPR 16).
    MIL-STD-461G RE102's platform curves are built in (RE102_LIMITS, by name); a curve of the user's own (a
    customer's, another edition's) is given as points."""
    name: str
    points: tuple
    distance_m: float = 1.0
    bands: str = "mil461"


# MIL-STD-461G Table II resolution bandwidths [MIL-STD-461G, Table II], as [(upper frequency, bandwidth)]: 1 kHz to
# 150 kHz, 10 kHz to 30 MHz, 100 kHz to 1 GHz, 1 MHz above
MIL461_RBW = [(150e3, 1e3), (30e6, 10e3), (1e9, 100e3), (np.inf, 1e6)]


def _re102(name: str, *points) -> RadiatedLimit:
    """An RE102 curve: 1 m, MIL-STD-461G Table II bandwidths, (f Hz, dBµV/m) breakpoints."""
    return RadiatedLimit(name, tuple(points), 1.0, "mil461")


# MIL-STD-461G (11 December 2015) RE102, Figures RE102-1 to -4 [MIL-STD-461G] (the helicopter and space curves
# from 10 kHz: [MIL-STD-461H]): the breakpoints as printed (the levels at 10 kHz,
# 2 MHz, 100 MHz and 18 GHz are on the figures' grid lines or labelled; the rise above 100 MHz is ~20 dB/decade). The
# submarine internal curve's low end is not labelled: read from the figure (88 dBµV/m at 10 kHz falling to 50 at
# ~0.8 MHz, ±1 dB). The applicable range is the curve's: ground and Air Force aircraft from 2 MHz (5.18.1); the
# fixed-wing internal curves are drawn from 2 MHz only.
RE102_LIMITS = {
    "re102_ground_army": _re102("RE102 ground, Navy mobile & Army (Fig. RE102-4)",
                                (2e6, 24.0), (100e6, 24.0), (18e9, 69.0)),
    "re102_ground_navy_fixed": _re102("RE102 ground, Navy fixed & Air Force (Fig. RE102-4)",
                                      (2e6, 44.0), (100e6, 44.0), (18e9, 89.0)),
    # fixed wing external applies from 2 MHz ("Fixed wing external (2 MHz to 18 GHz)" in both 461G and 461H); the
    # curve from 10 kHz is the helicopters' (461H: "Space and Helicopters (10 kHz to 18 GHz)"). Before 2026-10-18
    # one entry from 10 kHz for both
    "re102_aircraft_external": _re102("RE102 aircraft, fixed wing external (Fig. RE102-3)",
                                      (2e6, 24.0), (100e6, 24.0), (18e9, 69.0)),
    "re102_helicopter": _re102("RE102 helicopters (and space, 461H) (Fig. RE102-3)",
                               (10e3, 60.0), (2e6, 24.0), (100e6, 24.0), (18e9, 69.0)),
    "re102_space": _re102("RE102 space systems (461H Fig. RE102-3; 461G: as helicopters)",
                          (10e3, 60.0), (2e6, 24.0), (100e6, 24.0), (18e9, 69.0)),
    "re102_aircraft_internal": _re102("RE102 aircraft, fixed wing internal < 25 m (Fig. RE102-3)",
                                      (2e6, 34.0), (100e6, 34.0), (18e9, 79.0)),
    "re102_aircraft_internal_25m": _re102("RE102 aircraft, fixed wing internal ≥ 25 m (Fig. RE102-3)",
                                          (2e6, 44.0), (100e6, 44.0), (18e9, 89.0)),
    "re102_ship_above_deck": _re102("RE102 surface ship, above deck & exposed below deck (Fig. RE102-1)",
                                    (10e3, 70.0), (100e6, 36.0), (18e9, 82.0)),
    "re102_ship_below_deck": _re102("RE102 surface ship, below deck (Fig. RE102-1)",
                                    (10e3, 90.0), (100e6, 56.0), (18e9, 102.0)),
    "re102_submarine_external": _re102("RE102 submarine, external to the pressure hull (Fig. RE102-2)",
                                       (10e3, 60.0), (2e6, 24.0), (100e6, 24.0), (18e9, 69.0)),
    "re102_submarine_internal": _re102("RE102 submarine, internal to the pressure hull (Fig. RE102-2)",
                                       (10e3, 88.0), (0.8e6, 50.0), (100e6, 50.0), (18e9, 95.0)),
}


def rbw_bands(cls) -> list:
    """The receiver bandwidth table of a limit: CISPR 16's, or a user limit's own."""
    if isinstance(cls, RadiatedLimit) and cls.bands == "mil461":
        return MIL461_RBW
    return RBW_BANDS


def limit_range(cls) -> tuple[float, float]:
    """(lowest, highest) frequency a limit covers: CISPR 32's 30 MHz-6 GHz, or a user limit's first and last
    point (RE102 from 2 MHz, or 10 kHz for some platforms)."""
    if isinstance(cls, RadiatedLimit):
        return float(cls.points[0][0]), float(cls.points[-1][0])
    return 30e6, 6e9


def describe_limit(cls) -> str:
    """A limit in words, for messages and reports ("CISPR 32 class B", or a user limit's name)."""
    return cls.name if isinstance(cls, RadiatedLimit) else f"CISPR 32 class {cls}"


def loop_field(f, m_abs, r, ground_reflection: bool = True):
    """Peak |E| (V/m) from a loop with dipole moment |m| (A·m²) at distance r (m; a number or one per frequency),
    broadside: the far field with the induction term (see the module)."""
    f = np.asarray(f, float)
    r = np.asarray(r, float)
    # eta0 k^2 / (4 pi) with k = 2 pi f / c: 377 * (2 pi / c)^2 / (4 pi) = 1.316e-14 (s^2 ohm / m^2) [Paul 2006]; the
    # small loop's E_phi is that times (1 + 1/(j k r)): |.| = sqrt(1 + 1/(k r)^2) [Balanis 2016, §5.2]
    kr = 2 * np.pi * np.maximum(f, 1.0) * r / C0
    e = 1.316e-14 * f ** 2 * np.asarray(m_abs, float) / r * np.sqrt(1.0 + 1.0 / kr ** 2)
    # the worst case of the direct and the floor-reflected wave arriving in phase (a factor 2, +6 dB) [A7]
    return e * (2.0 if ground_reflection else 1.0)


def dbuv(e_v_per_m):
    """A field strength in dBµV/m (20 log10 of E / 1 µV/m); zero reads as -270 dB rather than -inf."""
    return 20 * np.log10(np.maximum(np.asarray(e_v_per_m, float), 1e-30) / 1e-6)


def limits(freqs, cls: Union[str, RadiatedLimit] = "B") -> tuple[np.ndarray, np.ndarray]:
    """(limit dBµV/m, standard distance m) per frequency: CISPR 32 class `cls` (NaN outside 30 MHz-6 GHz,
    distance 10 m there), or a user limit (its curve, NaN outside it, its distance)."""
    f = np.asarray(freqs, float)
    if isinstance(cls, RadiatedLimit):
        pts = np.asarray(cls.points, float)
        lim = np.interp(np.log10(np.maximum(f, 1e-30)), np.log10(pts[:, 0]), pts[:, 1], left=np.nan, right=np.nan)
        return lim, np.full(f.shape, float(cls.distance_m))
    lim = np.full(f.shape, np.nan)
    dist = np.full(f.shape, 10.0)
    for lo, hi, value, d in CISPR32[cls]:
        # at a band edge (230 MHz, 1 GHz) the lower band's limit applies: a frequency is set only once
        sel = (f >= lo) & (f <= hi) & np.isnan(lim)
        lim[sel] = value
        dist[sel] = d
    return lim, dist


# CISPR 16 receiver resolution bandwidth [CISPR 16-1-1]: 120 kHz (30 MHz-1 GHz, quasi-peak) and 1 MHz above 1 GHz.
# A table is [(upper frequency, bandwidth), ...] in ascending order.
RBW_BANDS = [(1e9, 120e3), (np.inf, 1e6)]


def receiver_bandwidth(freqs, bands=RBW_BANDS) -> np.ndarray:
    """The receiver's resolution bandwidth at each frequency, from a table of (upper frequency, bandwidth)."""
    f = np.asarray(freqs, float)
    out = np.full(f.shape, bands[-1][1])
    for hi, bw in reversed(bands):
        out[f < hi] = bw
    return out


def receiver_sum(freqs, peaks, bands=RBW_BANDS) -> np.ndarray:
    """What a peak detector reads at each harmonic: the coherent sum of all harmonics within ±RBW/2.

    Harmonics of a periodic signal are phase-locked, so when they are closer together than the receiver
    bandwidth (switching frequencies below 120 kHz in the radiated band) they add up in one reading, here as
    magnitudes (all in phase: the peak detector's worst case [A12]). A
    per-harmonic comparison would be too optimistic by up to 20·log10(RBW/f0) dB. For f0 above the
    bandwidth every window holds just its own harmonic and nothing changes."""
    f = np.asarray(freqs, float)
    p = np.asarray(peaks, float)
    if len(f) < 2:
        return p.copy()
    half = receiver_bandwidth(f, bands) / 2
    # f is sorted: the harmonics within +-RBW/2 of each one are a contiguous run [lo, hi), found by binary search;
    # their sum comes from a running (prefix) sum, so all windows cost O(n log n) instead of O(n^2)
    lo = np.searchsorted(f, f - half, side="left")
    hi = np.searchsorted(f, f + half, side="right")
    cs = np.concatenate([[0.0], np.cumsum(p)])
    return cs[hi] - cs[lo]


WINDOW_DIRECT = 1000        # harmonics each side of one summed one by one in window_weight (more: their mean)


def window_weight(freqs, harmonic_of: tuple, bands=RBW_BANDS) -> np.ndarray:
    """What the harmonics of a trapezoid within the receiver's bandwidth add up to, per unit of the harmonic at
    each frequency: harmonic_of = (f0, duty). A trapezoid's harmonic n is its edges' envelope times |sin(nπD)|
    [Paul 2006] (the two edges of a period beating: every even one null at D = ½), and the envelope is smooth
    across a bandwidth, so the in-phase sum [A12] of the harmonics n_k − m … n_k + m in the window ±RBW/2
    (m = floor(RBW / (2·f0))) is the harmonic n_k's amplitude times Σ|sin(nπD)| / |sin(n_k·πD)|. The sum is
    taken one by one for up to WINDOW_DIRECT harmonics each side, beyond that from their mean. 1 where the
    harmonics are further apart than the bandwidth."""
    f0, duty = harmonic_of
    f = np.asarray(freqs, float)
    n = np.rint(f / f0)
    m_all = np.floor(receiver_bandwidth(f, bands) / (2 * f0) + 1e-9).astype(np.int64)
    out = np.ones(len(f))
    for m in np.unique(m_all):
        if m < 1:
            continue
        sel = m_all == m
        mm = int(min(m, WINDOW_DIRECT))
        offs = np.arange(-mm, mm + 1)
        nn = n[sel][:, None] + offs[None, :]
        s = np.where(nn >= 1, np.abs(np.sin(np.pi * duty * nn)), 0.0)
        total = s.sum(axis=1) * (2 * m + 1) / (2 * mm + 1)
        own = np.abs(np.sin(np.pi * duty * n[sel]))
        # a harmonic near a null of the beat carries almost nothing itself: its weight is capped (the thinned
        # choice takes the harmonics at the beat's maxima, simulate.thin_harmonics)
        out[sel] = np.maximum(total / np.maximum(own, 0.5), 1.0)
    return out


CISPR_FLOOR_TOP = 1e9       # CISPR 32: a reflecting floor up to here (OATS / SAC), absorber on it above (FSOATS) [A7]


def emission_arrays(freqs, currents, m_per_amp, cls: Union[str, RadiatedLimit] = "B", distance_m: Optional[float] = None,
                    ground_reflection: bool = True, spread=None, harmonic_of: Optional[tuple] = None
                    ) -> dict[str, np.ndarray]:
    """Vectorised estimate per harmonic: field (RMS, dBµV/m at the standard or given distance, receiver
    bandwidth applied), limit and margin (NaN where no limit applies). spread = (f0, emi.spread.Spread): a power
    stage's dithered switching frequency; the receiver is simulated as for the conducted estimate
    (spread.spread_readings), within the limit's band and where each reading holds one harmonic (f0 above every
    bandwidth used); elsewhere the harmonics within a bandwidth add up as without spread.
    harmonic_of = (f0, duty): `freqs` are a thinned choice of the signal's harmonics n·f0 (a path through a logic
    buffer [A76]); a reading then takes its own harmonic times what the signal's harmonics within the bandwidth
    add up to per unit of it (window_weight), what receiver_sum gives on all of them where the edges' envelope
    is smooth across a bandwidth [A12]. Before 2026-11-10 a thinned set read one harmonic per bandwidth: a 1 kHz
    signal through a buffer 36 dB low at 50 % duty (60 odd harmonics in 120 kHz) (audit round 11, F11-1).
    The floor's reflection [A7] is taken for CISPR 32 up to 1 GHz only: above it the standard's site has absorber
    on the floor (free-space conditions; before 2026-11-10 +6 dB there too, F11-2). A user limit (RE102) keeps it
    over its whole range."""
    f = np.asarray(freqs, float)
    i_pk = np.asarray(currents, float)
    m = np.asarray(m_per_amp, float) * np.ones_like(f)
    lim, dist = limits(f, cls)
    r = np.full(f.shape, float(distance_m)) if distance_m else dist
    e_pk = loop_field(f, m * i_pk, r, ground_reflection)
    if ground_reflection and not isinstance(cls, RadiatedLimit):
        e_pk = np.where(f > CISPR_FLOOR_TOP, e_pk / 2.0, e_pk)
    bands = rbw_bands(cls)
    if spread is not None and len(f) and spread[0] > float(np.max(receiver_bandwidth(f, bands))):
        from .spread import spread_readings
        f0, sp = spread
        reading = spread_readings(f, e_pk, f0, sp, lambda x: float(receiver_bandwidth([x], bands)[0]),
                                  f_max=limit_range(cls)[1])
    else:
        # peak amplitudes summed within the receiver bandwidth (a thinned set: each one for its window's harmonics)
        reading = receiver_sum(f, e_pk, bands) if harmonic_of is None else e_pk * window_weight(f, harmonic_of, bands)
    # as RMS (/sqrt 2): what the limits are written in
    e_db = dbuv(reading / math.sqrt(2))
    # at another distance than the standard's the limit is moved by the far field's 1/r: 20 log10(d_std / r)
    limit = lim + (20 * np.log10(dist / r) if distance_m else 0.0)
    return {"f": f, "current_a": i_pk, "m_m2": m * i_pk, "e_dbuv": e_db, "distance_m": r, "limit_dbuv": limit,
            "margin_db": limit - e_db}


def strongest_per_bin(bins: np.ndarray, values: np.ndarray) -> np.ndarray:
    """Indices (ascending) of the largest value in each bin."""
    if len(bins) < 2:
        return np.arange(len(bins))
    # sort by bin, then by value descending (lexsort's last key is the primary one): the first entry of each bin
    # in this order is its largest
    order = np.lexsort((-values, bins))
    first = np.ones(len(order), bool)
    first[1:] = bins[order[1:]] != bins[order[:-1]]
    return np.sort(order[first])


def thin_rows(f: np.ndarray, values: np.ndarray, idx: np.ndarray, bands=RBW_BANDS,
              per_decade: Optional[int] = None) -> np.ndarray:
    """Of the harmonics idx, one per receiver-bandwidth bin (the strongest reading in it: neighbours only repeat
    that reading), and with per_decade at most that many per decade (the envelope, for reports and plots of
    converters switching at kHz, which have thousands of bins)."""
    if len(idx) < 2:
        return idx
    rbw = receiver_bandwidth(f[idx], bands)
    # a bin is identified by its start frequency (bin numbers alone would collide between bandwidth bands)
    idx = idx[strongest_per_bin(np.floor(f[idx] / rbw) * rbw, values[idx])]
    if per_decade and len(idx) > 1:
        idx = idx[strongest_per_bin(np.floor(np.log10(f[idx]) * per_decade), values[idx])]
    return idx


def emission_table(freqs, currents, m_per_amp, cls: Union[str, RadiatedLimit] = "B", distance_m: Optional[float] = None,
                   ground_reflection: bool = True, per_decade: Optional[int] = None, spread=None,
                   harmonic_of: Optional[tuple] = None) -> list[dict]:
    """Rows of `emission_arrays` for reports. When the harmonics are closer than the receiver bandwidth
    (low switching frequencies: 16 000 harmonics at 10 kHz), one row per bandwidth is kept: neighbouring
    rows would only repeat the same receiver reading. per_decade: see thin_rows."""
    a = emission_arrays(freqs, currents, m_per_amp, cls, distance_m, ground_reflection, spread, harmonic_of)
    f = a["f"]
    keep = thin_rows(f, a["e_dbuv"], np.arange(len(f)), rbw_bands(cls), per_decade=per_decade)
    rows = []
    for j in keep:
        lim = a["limit_dbuv"][j]
        rows.append({"f": float(f[j]), "current_a": float(a["current_a"][j]), "m_m2": float(a["m_m2"][j]),
                     "e_dbuv": float(a["e_dbuv"][j]), "distance_m": float(a["distance_m"][j]),
                     "limit_dbuv": None if np.isnan(lim) else float(lim),
                     "margin_db": None if np.isnan(lim) else float(a["margin_db"][j])})
    return rows
