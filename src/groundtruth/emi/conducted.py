"""Conducted (differential-mode) emission of a switching converter at its power input, measured with a LISN.

The hot loop's pulsed current divides between the input capacitors and the input lines. The solver gives the
fraction that reaches the input connector, where one LISN (line impedance stabilisation network) per line closes
the loop: the differential-mode current runs out on one line and back on the other, through both LISNs in series.
Each LISN's receiver reads the voltage across its 50 ohm port.

A switching waveform is a set of phase-locked CW harmonics. While f_sw exceeds the receiver bandwidth each reading
holds one harmonic, and peak, quasi-peak and average detectors all read its RMS value, so the estimate is
compared with the average limit (the strictest). Below that, neighbouring harmonics add up in one reading
(`receiver_sum`).

Not modelled: common-mode emission (switch-node dV/dt through stray capacitance to the chassis or heatsink),
which often dominates above a few MHz.

The limit tables were entered from the standards' tables as commonly published: check them against the edition
your test plan cites (CISPR 25 in particular differs between editions and is band-limited).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

import numpy as np

from .radiation import receiver_bandwidth, receiver_sum, thin_rows


@dataclass(frozen=True)
class Lisn:
    """One line's LISN: series inductance (with resistance and the supply-side capacitor to the reference) towards
    the supply, and the receiver's 50 ohm port behind a coupling capacitor."""
    name: str
    L: float              # H
    R_s: float            # ohm, in series with L on the supply side
    C_s: float            # F, supply-side capacitor to the reference (0: an ideal RF short)
    C_c: float            # F, coupling capacitor to the receiver port (0: none, the nominal network)
    R_m: float = 50.0     # receiver input

    def impedance(self, f) -> tuple[np.ndarray, np.ndarray]:
        """(impedance at the EUT terminal, receiver voltage / terminal voltage) per frequency."""
        w = 2 * np.pi * np.asarray(f, float)
        # the EUT terminal sees the supply branch (R + jwL, plus the supply-side capacitor) in parallel with the
        # measuring branch (coupling capacitor + 50 ohm); the receiver reads the 50 ohm share of the terminal
        # voltage (a voltage divider with the coupling capacitor)
        z_supply = self.R_s + 1j * w * self.L
        if self.C_s > 0:
            z_supply = z_supply + 1.0 / (1j * w * self.C_s)
        z_meas = self.R_m + (1.0 / (1j * w * self.C_c) if self.C_c > 0 else 0.0)
        return z_supply * z_meas / (z_supply + z_meas), self.R_m / z_meas


LISNS = {
    # CISPR 16-1-2 V-network: nominal impedance 50 ohm || (50 uH + 5 ohm)
    "cispr32": Lisn("50 Ω ∥ (50 µH + 5 Ω) (CISPR 16-1-2)", 50e-6, 5.0, 0.0, 0.0),
    # CISPR 25 artificial network: nominal 50 ohm || 5 uH
    "cispr25": Lisn("50 Ω ∥ 5 µH (CISPR 25)", 5e-6, 0.0, 0.0, 0.0),
    # MIL-STD-461 LISN as drawn: 50 uH, 8 uF + 5 ohm to the reference on the supply side, 0.25 uF to the 50 ohm port
    "mil461": Lisn("50 µH (MIL-STD-461)", 50e-6, 5.0, 8e-6, 0.25e-6),
}

# Receiver resolution bandwidth tables [(upper frequency, bandwidth)]
BANDS = {
    "cispr32": [(150e3, 200.0), (30e6, 9e3), (np.inf, 120e3)],          # CISPR 16-1-1 bands A, B, C/D
    "cispr25": [(150e3, 200.0), (30e6, 9e3), (np.inf, 120e3)],
    "mil461": [(150e3, 1e3), (30e6, 10e3), (np.inf, 100e3)],            # MIL-STD-461G Table II
}

# Limits: [(f_lo, f_hi, dBµV at f_lo, dBµV at f_hi)], linear in log f within a band; the lowest applies where
# bands meet or overlap.
CISPR32_AV = {   # CISPR 32 Table A.10 / A.11, AC mains port, average detector
    "B": [(0.15e6, 0.5e6, 56.0, 46.0), (0.5e6, 5e6, 46.0, 46.0), (5e6, 30e6, 50.0, 50.0)],
    "A": [(0.15e6, 0.5e6, 66.0, 66.0), (0.5e6, 30e6, 60.0, 60.0)],
}
# CISPR 25 voltage method, average detector, class 5 and the step to each lower class (class 1 = class 5 + 4 steps)
CISPR25_AV5 = [   # (f_lo, f_hi, class 5 dBµV, dB per class)
    (0.15e6, 0.30e6, 50.0, 10.0),     # LW
    (0.53e6, 1.8e6, 34.0, 8.0),       # MW
    (5.9e6, 6.2e6, 33.0, 6.0),        # SW
    (26e6, 28e6, 24.0, 6.0),          # CB
    (30e6, 54e6, 24.0, 6.0),          # VHF
    (41e6, 88e6, 24.0, 6.0),          # TV band I
    (68e6, 87e6, 18.0, 6.0),          # VHF
    (76e6, 108e6, 18.0, 6.0),         # FM
]
MIL461_RELAX = {"basic": 0.0, "28v": 0.0, "115v": 6.0, "220v": 9.0, "270v": 10.0, "440v": 12.0}


def band_limits(standard: str, cls: str = "B") -> list[tuple[float, float, float, float]]:
    """The limit line of a standard and class as bands [(f_lo, f_hi, dB_lo, dB_hi)]."""
    std = standard.lower()
    c = str(cls).strip().lower()
    if std == "cispr32":
        return CISPR32_AV["A" if c == "a" else "B"]
    if std == "cispr25":
        k = int(c) if c.isdigit() and 1 <= int(c) <= 5 else 5
        return [(lo, hi, v + (5 - k) * step, v + (5 - k) * step) for lo, hi, v, step in CISPR25_AV5]
    if std == "mil461":       # CE102 basic curve: 94 dBµV at 10 kHz falling 20 dB/decade to 60 dBµV at 500 kHz
        r = MIL461_RELAX.get(c.replace(" ", ""), 0.0)
        return [(10e3, 500e3, 94.0 + r, 60.0 + r), (500e3, 10e6, 60.0 + r, 60.0 + r)]
    return []


def frequency_range(standard: str, cls: str = "B") -> Optional[tuple[float, float]]:
    """The lowest and highest frequency a standard's limits cover, or None for no standard."""
    bands = band_limits(standard, cls)
    if not bands:
        return None
    return min(b[0] for b in bands), max(b[1] for b in bands)


def limit_line(freqs, standard: str, cls: str = "B") -> np.ndarray:
    """dBµV per frequency, NaN outside the standard's bands."""
    f = np.asarray(freqs, float)
    out = np.full(f.shape, np.nan)
    for lo, hi, a, b in band_limits(standard, cls):
        sel = (f >= lo) & (f <= hi)
        if not sel.any():
            continue
        v = a + (b - a) * np.log(f[sel] / lo) / math.log(hi / lo)    # straight on a log-frequency axis
        out[sel] = np.fmin(out[sel], v)            # fmin ignores the NaN of not-yet-set entries
    return out


def describe(standard: str, cls: str = "B") -> str:
    """The standard and class in words, for reports ("CISPR 32 class B (average)")."""
    std = standard.lower()
    if std == "cispr32":
        return f"CISPR 32 class {'A' if str(cls).upper() == 'A' else 'B'} (average)"
    if std == "cispr25":
        c = str(cls)
        return f"CISPR 25 class {c if c.isdigit() and 1 <= int(c) <= 5 else '5'} (voltage method, average)"
    if std == "mil461":
        return "MIL-STD-461 CE102" + ("" if str(cls).lower() in ("basic", "28v", "b") else f" ({cls})")
    return "none"


def lisn_loop_impedance(standard: str):
    """Impedance of the differential-mode loop through both lines' LISNs, as a function of f (for the solver).
    Within CE101_LEAD_F the leads of the CE101 test instead (CE101_LEAD: the supply behind the LISN's 50 µH)."""
    lisn = LISNS[standard.lower()]

    def z(f, lisn=lisn):
        """Out on one line and back on the other: the two lines' LISNs are in series in the loop."""
        net = CE101_LEAD if CE101_LEAD_F[0] <= f < CE101_LEAD_F[1] else lisn
        return complex(2.0 * net.impedance(f)[0])
    return z


def detector(standard: str) -> str:
    """The detector the limits are written for: MIL-STD-461 uses peak; the CISPR limits here are the average ones."""
    return "peak" if standard.lower() == "mil461" else "average"


def conducted_arrays(freqs, line_current, standard: str, cls: str = "B", spread=None) -> dict[str, np.ndarray]:
    """Receiver reading at each harmonic (RMS dBµV, receiver bandwidth applied) from the peak differential-mode
    line current per harmonic, the limit and the margin (NaN where no limit applies). spread = (f0, Spread): the
    switching frequency is dithered; the receiver is simulated (emi/spread.py) within the standard's range. That
    model takes one harmonic per receiver bandwidth; for f0 at or below the bandwidth the spread is ignored and
    the harmonics add coherently as without it (conservative)."""
    from .spread import spread_readings
    f = np.asarray(freqs, float)
    i_pk = np.abs(np.asarray(line_current, complex))
    std = standard.lower()
    lisn = LISNS[std]
    z, frac = lisn.impedance(f)
    v_pk = i_pk * np.abs(z) * np.abs(frac)         # the line current through one LISN, its port's share
    bands = BANDS[std]
    rng = frequency_range(standard, cls)
    # spread spectrum only where each reading holds one harmonic (f0 above every bandwidth used)
    if spread is not None and spread[0] > float(np.max(receiver_bandwidth(f, bands), initial=0.0)):
        f0, sp = spread
        reading = spread_readings(f, v_pk, f0, sp, lambda x: float(receiver_bandwidth([x], bands)[0]),
                                  detector(std), rng[1] if rng else math.inf) / math.sqrt(2)
    else:
        reading = receiver_sum(f, v_pk, bands) / math.sqrt(2)
    v_db = 20 * np.log10(np.maximum(reading, 1e-30) / 1e-6)
    lim = limit_line(f, standard, cls)
    return {"f": f, "current_a": i_pk, "v_dbuv": v_db, "limit_dbuv": lim, "margin_db": lim - v_db}


def conducted_table(freqs, line_current, standard: str, cls: str = "B", per_decade: Optional[int] = None,
                    spread=None) -> list[dict]:
    """Report rows within the standard's range: one per receiver bin (the strongest reading in it), with
    per_decade at most that many per decade (see radiation.thin_rows)."""
    a = conducted_arrays(freqs, line_current, standard, cls, spread)
    f = a["f"]
    rng = frequency_range(standard, cls)
    inside = (f >= rng[0]) & (f <= rng[1]) if rng else np.ones(len(f), bool)
    idx = thin_rows(f, a["v_dbuv"], np.nonzero(inside)[0], BANDS[standard.lower()], per_decade)
    rows = []
    for j in idx:
        lim = a["limit_dbuv"][j]
        rows.append({"f": float(f[j]), "current_a": float(a["current_a"][j]), "v_dbuv": float(a["v_dbuv"][j]),
                     "limit_dbuv": None if np.isnan(lim) else float(lim),
                     "margin_db": None if np.isnan(lim) else float(a["margin_db"][j])})
    return rows


# --------------------------------------------------------------------------- MIL-STD-461G CE101
# CE101, "conducted emissions, audio frequency currents, power leads": the current in each power lead (and return),
# 30 Hz to 10 kHz, read with a current probe 5 cm from the LISN (Figure CE101-6) and a peak detector, in dBµA.
# Limits and bandwidths entered from MIL-STD-461G (11 December 2015): Figures CE101-1 and CE101-4, Table II.
CE101_RANGE = (30.0, 10e3)              # Hz
CE101_TOP_SOLVE = 9.9e3                 # the highest CE101 solve: CE102's band starts at 10 kHz with another LISN
CE101_LEAD_F = (15.0, 9.95e3)           # where a LISN branch is the CE101 lead (above the DC solves' 10 Hz)
CE101_PLATFORMS = ("none", "aircraft", "ships")
CE101_BANDS = [(1e3, 10.0), (10e3, 100.0), (np.inf, 1e3)]   # Table II: 10 Hz to 1 kHz, 100 Hz to 10 kHz
# The lead below 10 kHz: the LISN's 50 µH to the power source, which is stiff there (a bench supply's output
# capacitors and regulation; the 8 µF + 5 Ω to the reference sits across it and carries little), and the 0.25 µF
# to the terminated port. A stiff source takes the most current out of the board's capacitors: conservative. In
# series CE101_LEAD_R per line: the LISN coil's and the 2 m test lead's resistance and the source's (a few tens of
# mΩ together). Without it the leads' 100 µH and the board's bus capacitors resonate in band with a Q of hundreds
# (45 µF of ceramics: ~2.4 kHz), set only by the capacitors' ESR; with it the Q of such a resonance is ~10-15.
CE101_LEAD_R = 0.05                     # Ω per line
CE101_LEAD = Lisn("50 µH + 50 mΩ to a stiff source (CE101)", 50e-6, CE101_LEAD_R, 0.0, 0.25e-6)
CE101_CURVES = {   # (f Hz, dBµA) breakpoints, straight on a log-frequency axis between them
    # Figure CE101-4: Army aircraft (including flight line) and Navy ASW aircraft, AC and DC, by nominal EUT
    # source voltage: curve #1 above 28 V, curve #2 at 28 V or below
    "aircraft_above_28v": ((30.0, 110.0), (1e3, 110.0), (10e3, 90.0)),
    "aircraft_28v": ((30.0, 100.0), (1e3, 100.0), (10e3, 80.0)),
    # Figure CE101-1: surface ships and submarines, DC (relaxed with the load current, Ce101.relax_db)
    "ships_dc": ((30.0, 95.0), (2.6e3, 95.0), (10e3, 76.0)),
}


@dataclass(frozen=True)
class Ce101:
    """The CE101 limit that applies: the platform (aircraft: Figure CE101-4; ships: Figure CE101-1, DC), the
    nominal source voltage (picks CE101-4's curve) and the EUT's load current (relaxes CE101-1)."""
    platform: str
    source_v: float
    load_a: float = 0.0

    @property
    def curve(self) -> str:
        """The key of the curve in CE101_CURVES."""
        if self.platform == "ships":
            return "ships_dc"
        return "aircraft_above_28v" if self.source_v > 28.0 else "aircraft_28v"

    @property
    def relax_db(self) -> float:
        """CE101-1's relaxation: none up to 3 A, 20·log(I/3) between 3 and 185 A, 35 dB from 185 A. CE101-4 has
        none for DC sources."""
        if self.platform != "ships" or self.load_a <= 3.0:
            return 0.0
        return 35.0 if self.load_a >= 185.0 else 20.0 * math.log10(self.load_a / 3.0)

    def describe(self) -> str:
        """In words, for reports ("MIL-STD-461G CE101 (Figure CE101-4 curve #1, source above 28 V)")."""
        if self.platform == "ships":
            relax = f", relaxed {self.relax_db:.1f} dB for {self.load_a:.3g} A" if self.relax_db else ""
            return f"MIL-STD-461G CE101 (Figure CE101-1, ships DC{relax})"
        which = "#1, source above 28 V" if self.source_v > 28.0 else "#2, source 28 V or below"
        return f"MIL-STD-461G CE101 (Figure CE101-4 curve {which})"


def ce101_limit(freqs, ce: Ce101) -> np.ndarray:
    """dBµA per frequency (RMS, as the standard's limits), NaN outside 30 Hz-10 kHz."""
    f = np.asarray(freqs, float)
    pts = CE101_CURVES[ce.curve]
    fs, vs = np.log([p[0] for p in pts]), np.array([p[1] for p in pts])
    out = np.interp(np.log(np.maximum(f, 1e-30)), fs, vs) + ce.relax_db
    return np.where((f >= CE101_RANGE[0]) & (f <= CE101_RANGE[1]), out, np.nan)


def ce101_arrays(freqs, line_current, ce: Ce101) -> dict[str, np.ndarray]:
    """The current probe's reading per harmonic (RMS dBµA; the lines within one bandwidth add as receiver_sum),
    from the peak line current per harmonic, with the limit and the margin."""
    f = np.asarray(freqs, float)
    i_pk = np.abs(np.asarray(line_current, complex))
    reading = receiver_sum(f, i_pk, CE101_BANDS) / math.sqrt(2)
    i_db = 20 * np.log10(np.maximum(reading, 1e-30) / 1e-6)
    lim = ce101_limit(f, ce)
    return {"f": f, "current_a": i_pk, "i_dbua": i_db, "limit_dbua": lim, "margin_db": lim - i_db}


def ce101_rows(f, i_dbua, current_a, ce: Ce101, per_decade: Optional[int] = None) -> list[dict]:
    """Report rows {f, current_a, i_dbua, limit_dbua, margin_db} within 30 Hz-10 kHz: one per receiver bin (the
    strongest reading in it), with per_decade at most that many per decade (radiation.thin_rows)."""
    f = np.asarray(f, float)
    i_dbua = np.asarray(i_dbua, float)
    lim = ce101_limit(f, ce)
    inside = np.nonzero((f >= CE101_RANGE[0]) & (f <= CE101_RANGE[1]))[0]
    rows = []
    # thinned by the excess over the limit, not the reading: the limit falls 20 dB/decade above 1 kHz (2.6 kHz
    # for ships), so the strongest reading of a decade need not be its worst margin
    for j in thin_rows(f, np.nan_to_num(i_dbua - lim, nan=-1e9), inside, CE101_BANDS, per_decade):
        ok = not np.isnan(lim[j])
        rows.append({"f": float(f[j]), "current_a": float(current_a[j]), "i_dbua": float(i_dbua[j]),
                     "limit_dbua": float(lim[j]) if ok else None,
                     "margin_db": float(lim[j] - i_dbua[j]) if ok else None})
    return rows


def ce101_table(freqs, line_current, ce: Ce101, per_decade: Optional[int] = None) -> list[dict]:
    """ce101_arrays as report rows (ce101_rows)."""
    a = ce101_arrays(freqs, line_current, ce)
    return ce101_rows(a["f"], a["i_dbua"], a["current_a"], ce, per_decade)
