"""Audit round 9 (docs/VALIDATION_AUDIT.md): independent benchmarks of the power stages' physics. Every expected
value is derived here (a time-domain simulation of the switched circuit, the Fourier transform of a sampled waveform,
or a closed form in the comments), never from GroundTruth's own functions, then compared with what GroundTruth gives.

    .venv/Scripts/python tools/dev/converter_audit.py [--only td,spectrum,structure,sweep] [--json OUT.json]

- td: a synchronous buck, a diode buck and a synchronous boost integrated in time (the inductor current through the
  on-state resistances, the dead times at V_SD, the winding's DCR; the on-time regulated to the load current):
  each part's mean loss against thermal.part_losses, and the simulation's own energy balance.
- spectrum: the harmonics of the hot loop's current pulse with the inductor's ripple on its top (a sampled
  waveform's FFT) against the spectrum the stage is solved with.
- structure: parallel switches, an integrated boost, the hard edge's current.
- sweep: the efficiency sweep at the stage's own current against the losses' sum.

Each case prints expected, actual, the deviation and PASS / FAIL against its stated tolerance. A FAIL is a finding to
investigate, not a test to loosen.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests"))             # boardgen, conftest.make: the generated KiCad boards

ROWS: list[dict] = []


def check(area: str, case: str, expected, actual, tol: float, rel: bool = True, note: str = "") -> bool:
    """Record one comparison: |actual − expected| within tol (relative to |expected| when rel, else absolute)."""
    dev = abs(actual - expected) / (abs(expected) if rel and expected != 0 else 1.0)
    ok = dev <= tol
    ROWS.append({"area": area, "case": case, "expected": expected, "actual": actual, "dev": dev, "tol": tol,
                 "rel": rel, "ok": ok, "note": note})
    print(f"  [{'PASS' if ok else 'FAIL'}] {area:9} {case:66} expected {expected:>11.5g}  actual {actual:>11.5g}"
          f"  dev {dev:.2e}" + (f"  ({note})" if note else ""))
    return ok


# ------------------------------------------------------------------------------- the switched circuit in time
def _steady(intervals, l_h: float, n: int = 4000):
    """One period of the inductor current in steady state. intervals: [(duration, e, r)], L·di/dt = e − r·i in each
    (e: the driving voltage with the output and any constant drop taken off, r: the series resistance). Each
    interval is the exact exponential of its linear equation; the period map i_end = a·i0 + b is linear, so the
    steady state is i0 = b/(1 − a). Returns [(t, i)] per interval (n samples each)."""
    def run(i0):
        """One period from the current i0: (the intervals' samples, the current at its end)."""
        out = []
        for dur, e, r in intervals:
            t = np.linspace(0.0, dur, n)
            if r > 0:
                i = e / r + (i0 - e / r) * np.exp(-r * t / l_h)
            else:
                i = i0 + e * t / l_h
            out.append((t, i))
            i0 = float(i[-1])
        return out, i0
    _, b = run(0.0)
    _, a1 = run(1.0)
    a = a1 - b
    return run(b / (1.0 - a))[0]


def _int(t, y) -> float:
    """∫ y dt over an interval's samples (trapezoid rule on 4000 points of a near-linear current)."""
    return float(np.trapezoid(y, t)) if hasattr(np, "trapezoid") else float(np.trapz(y, t))


def _solve_on_time(build, target, lo, hi):
    """The on-time for which build(t_on) -> mean load current meets the target (bisection: the mean rises with it)."""
    for _ in range(80):
        mid = 0.5 * (lo + hi)
        if build(mid) < target:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def buck_reference(v_in, v_out, i_out, f, l_h, r_hs, r_ls, dcr, t_rise, t_fall, v_sd, diode_vf=None) -> dict:
    """A buck into a stiff output voltage, simulated in time. Synchronous: high side on (the node at V_in − i·R_hs),
    the fall's dead time (the rectifier's reverse conduction: the node at −V_SD), the low side's channel (−i·R_ls),
    the rise's dead time (−V_SD). With diode_vf: the diode for the whole off-time (−V_f). The on-time is regulated
    so the mean inductor current is the load current. Mean powers in W."""
    T = 1.0 / f

    def stages(t_on):
        """The period's intervals (duration, driving voltage, series resistance) for an on-time."""
        if diode_vf is not None:
            return [(t_on, v_in - v_out, r_hs + dcr), (T - t_on, -diode_vf - v_out, dcr)]
        return [(t_on, v_in - v_out, r_hs + dcr), (t_fall, -v_sd - v_out, dcr),
                (T - t_on - t_fall - t_rise, -v_out, r_ls + dcr), (t_rise, -v_sd - v_out, dcr)]

    def mean(t_on):
        """The mean inductor (load) current in steady state for an on-time."""
        return sum(_int(t, i) for t, i in _steady(stages(t_on), l_h)) / T

    t_on = _solve_on_time(mean, i_out, 0.0, T - t_fall - t_rise)
    seg = _steady(stages(t_on), l_h)
    sq = [_int(t, i * i) for t, i in seg]
    lin = [_int(t, i) for t, i in seg]
    out = {"t_on": t_on, "duty": t_on / T, "p_in": v_in * lin[0] / T, "p_out": v_out * sum(lin) / T,
           "hs": r_hs * sq[0] / T, "dcr": dcr * sum(sq) / T,
           "i_valley": float(seg[0][1][0]), "i_peak": float(seg[0][1][-1])}
    if diode_vf is not None:
        out.update(diode=diode_vf * lin[1] / T, ls=0.0, dead=0.0)
    else:
        out.update(ls=r_ls * sq[2] / T, dead=v_sd * (lin[1] + lin[3]) / T)
    out["balance"] = out["p_in"] - out["p_out"] - sum(out[k] for k in ("hs", "ls", "dead", "dcr")) \
        - out.get("diode", 0.0)
    return out


def boost_reference(v_in, v_out, i_out, f, l_h, r_ls, r_hs, dcr, t_rise, t_fall, v_sd) -> dict:
    """A synchronous boost into a stiff output voltage: the low side (the main switch) on (the node at i·R_ls), the
    rise's dead time (the high side's reverse conduction: the node at V_out + V_SD), the high side's channel
    (V_out + i·R_hs), the fall's dead time. The on-time is regulated so the mean current into the output (everything
    but the low side's on-time) is the load current."""
    T = 1.0 / f

    def stages(t_on):
        """The period's intervals (duration, driving voltage, series resistance) for an on-time."""
        return [(t_on, v_in, r_ls + dcr), (t_rise, v_in - v_out - v_sd, dcr),
                (T - t_on - t_rise - t_fall, v_in - v_out, r_hs + dcr), (t_fall, v_in - v_out - v_sd, dcr)]

    def mean_out(t_on):
        """The mean current into the output in steady state for an on-time."""
        return sum(_int(t, i) for t, i in _steady(stages(t_on), l_h)[1:]) / T

    t_on = _solve_on_time(mean_out, i_out, 0.0, T - t_fall - t_rise)
    seg = _steady(stages(t_on), l_h)
    sq = [_int(t, i * i) for t, i in seg]
    lin = [_int(t, i) for t, i in seg]
    out = {"t_on": t_on, "p_in": v_in * sum(lin) / T, "p_out": v_out * sum(lin[1:]) / T,
           "ls": r_ls * sq[0] / T, "hs": r_hs * sq[2] / T, "dead": v_sd * (lin[1] + lin[3]) / T,
           "dcr": dcr * sum(sq) / T, "i_l": sum(lin) / T}
    out["balance"] = out["p_in"] - out["p_out"] - sum(out[k] for k in ("hs", "ls", "dead", "dcr"))
    return out


def _board(gen, name: str):
    """A generated board and its settings in a temporary folder (kept for the run)."""
    from conftest import make
    tmp = Path(tempfile.mkdtemp(prefix="converter_audit_"))
    board, sc, _ = make(tmp, gen, name, nets=())
    return board, sc.config


def _by(board, cfg, spec, duty: float, v_in: float) -> tuple[dict, dict]:
    """({ref: {category: W}}, {ref: W}) of thermal.part_losses for one stage alone (no DC paths)."""
    from groundtruth.analysis.thermal import part_losses
    rows, _ = part_losses(board, replace(cfg, converters=[spec]),
                          [SimpleNamespace(net=spec.name, duty=duty, v_in=v_in, dc=[])])
    return {r["ref"]: r["by"] for r in rows}, {r["ref"]: r["power_w"] for r in rows}


def td():
    """The losses that follow from the circuit alone (conduction with the ripple, the dead times, the winding, a
    diode) against a time-domain simulation of it. The simulation regulates its on-time; GroundTruth takes the ideal
    duty V_out/V_in, so the two differ by the losses' share of the power (a per cent or two), not more."""
    from boardgen import boost_board, discrete_buck_board
    from groundtruth.config.settings import ConverterSpec
    print("Time domain: a synchronous buck 55 V -> 27.5 V, 10 A, 500 kHz, 4.7 uH")
    board, cfg = _board(discrete_buck_board(), "td")
    l_h = cfg.model_for("L1").get("L")
    v_in, v_out, i_out, f = 55.0, 27.5, 10.0, 500e3
    r_hs, r_ls, dcr, dead, v_sd = 3.1e-3, 4.0e-3, 6.9e-3, 20e-9, 2.0
    ref = buck_reference(v_in, v_out, i_out, f, l_h, r_hs, r_ls, dcr, dead, dead, v_sd)
    check("td", "T0 the simulation's own energy balance (W left over)", 0.0, ref["balance"], 1e-6, rel=False)
    spec = ConverterSpec("buck", "VIN", "GND", switch_node="SW", high_side=["Q1"], low_side=["Q2"], f_sw=f,
                         i_out=i_out, v_in=v_in, v_out=v_out, rise_time=5e-9, rds_on=r_hs, rds_on_low=r_ls, dcr=dcr,
                         dead_rise=dead, dead_fall=dead, v_sd=v_sd)
    by, _ = _by(board, cfg, spec, v_out / v_in, v_in)
    check("td", "T1 high side's conduction", ref["hs"], by["Q1"]["conduction"], 0.03)
    check("td", "T2 low side's channel (both dead times taken out)", ref["ls"], by["Q2"]["conduction"], 0.03)
    check("td", "T3 reverse conduction in the dead times (no node swing)", ref["dead"], by["Q2"]["dead time"], 0.01)
    check("td", "T4 the winding's DCR with the ripple", ref["dcr"], by["L1"]["inductor"], 0.005)
    di = v_in * 0.5 * 0.5 / (l_h * f)
    check("td", "T5 the ripple p-p (simulated peak - valley)", ref["i_peak"] - ref["i_valley"], di, 0.01)

    print("Time domain: the same at light load (1 A: the ripple is most of the RMS)")
    ref = buck_reference(v_in, v_out, 1.0, f, l_h, r_hs, r_ls, dcr, 0.0, 0.0, v_sd)
    by, _ = _by(board, cfg, replace(spec, i_out=1.0, dead_rise=0.0, dead_fall=0.0), v_out / v_in, v_in)
    check("td", "T6 high side's conduction at 1 A (forced continuous)", ref["hs"], by["Q1"]["conduction"], 0.01)
    check("td", "T7 the winding at 1 A", ref["dcr"], by["L1"]["inductor"], 0.01)

    print("Time domain: a diode buck 24 V -> 5 V, 2 A, 400 kHz")
    v_in, v_out, i_out, f, vf = 24.0, 5.0, 2.0, 400e3, 0.5
    ref = buck_reference(v_in, v_out, i_out, f, l_h, 50e-3, 0.0, 30e-3, 0.0, 0.0, 0.0, diode_vf=vf)
    check("td", "T8 the simulation's own energy balance (W left over)", 0.0, ref["balance"], 1e-6, rel=False)
    spec_d = ConverterSpec("buck", "VIN", "GND", switch_node="SW", high_side=["Q1"], low_side=["D2"], f_sw=f,
                           i_out=i_out, v_in=v_in, v_out=v_out, rise_time=5e-9, rds_on=50e-3, dcr=30e-3, diode_vf=vf)
    # the board's low side is Q2: the loss model takes a part as a diode by its reference, so D2 has no footprint
    # and its loss is dropped by add(); compare the formula's terms on the parts that exist, and the diode by hand
    by, _ = _by(board, cfg, replace(spec_d, low_side=["Q2"]), v_out / v_in, v_in)
    d_ideal = v_out / v_in
    check("td", "T9 high side's conduction (ideal duty: the diode's drop raises the real one)", ref["hs"],
          by["Q1"]["conduction"], 0.12, note=f"real duty {ref['duty']:.3f}, ideal {d_ideal:.3f}")
    check("td", "T10 the catch diode: (1 - D)·I·V_f by hand at the ideal duty", ref["diode"],
          (1 - d_ideal) * i_out * vf, 0.03)

    print("Time domain: a synchronous boost 12 V -> 36 V, 1 A, 300 kHz")
    board_b, cfg_b = _board(boost_board(), "tdb")
    l_b = cfg_b.model_for("L1").get("L")
    v_in, v_out, i_out, f = 12.0, 36.0, 1.0, 300e3
    r_ls, r_hs, dcr, dead, v_sd = 20e-3, 30e-3, 40e-3, 30e-9, 0.8
    ref = boost_reference(v_in, v_out, i_out, f, l_b, r_ls, r_hs, dcr, dead, dead, v_sd)
    check("td", "T11 the simulation's own energy balance (W left over)", 0.0, ref["balance"], 1e-6, rel=False)
    # boost_board's rectifier is a diode D1: the loss model's synchronous terms need a FET's reference there, so the
    # closed forms are written out here from the model's documented rules [A48, A80] and compared with the simulation
    d_hs = v_in / v_out                              # the high side's share (ConverterSpec.pulse)
    i_l = i_out / d_hs
    rip = v_in * (1 - d_hs) / (l_b * f)
    i_sq = i_l ** 2 + rip ** 2 / 12
    check("td", "T12 boost: the inductor current I_out/d", ref["i_l"], i_l, 0.03)
    check("td", "T13 boost: low side (1 - d)(I_L^2 + dI^2/12)R", ref["ls"], (1 - d_hs) * i_sq * r_ls, 0.05)
    check("td", "T14 boost: high side's channel (d - 2 t_dead f)(I_L^2 + dI^2/12)R", ref["hs"],
          (d_hs - 2 * dead * f) * i_sq * r_hs, 0.05)
    check("td", "T15 boost: dead times, V_SD f (I_v t_hard + I_p t_soft)", ref["dead"],
          v_sd * f * ((i_l - rip / 2) * dead + (i_l + rip / 2) * dead), 0.05)
    check("td", "T16 boost: the winding", ref["dcr"], i_sq * dcr, 0.05)


# ------------------------------------------------------------------------------------- the pulse's spectrum
def pulse_fft(i_mean: float, ripple: float, duty: float, f_sw: float, tr: float, n_max: int, rising: bool = True,
              samples: int = 1 << 20) -> np.ndarray:
    """Peak amplitudes c_1..c_n_max of a hot loop's current pulse by FFT of its sampled waveform: zero outside the
    conduction time, inside it the inductor current on its ripple (from I − ΔI/2 to I + ΔI/2 when rising: a buck's
    high side; falling: a boost's rectifier), the two edges linear over tr (the commutation)."""
    T = 1.0 / f_sw
    t = (np.arange(samples) + 0.5) * T / samples
    tau = duty * T
    top = i_mean + (1.0 if rising else -1.0) * ripple * (t / tau - 0.5)
    x = np.where(t < tau, top, 0.0)
    # the edges: a box of width tr smooths the two jumps into linear ramps (a convolution: the spectrum times
    # sinc(n·f·tr)); done in time here so the reference does not use that identity
    k = max(int(round(tr / T * samples)), 1)
    x = np.convolve(np.concatenate([x[-k:], x, x[:k]]), np.ones(k) / k, mode="same")[k:-k]
    spec = np.fft.rfft(x) / samples
    return 2 * np.abs(spec[1:n_max + 1])


def spectrum():
    """The hot loop's pulse carries the inductor's ripple on its top. The pulse's harmonics from a sampled
    waveform against the harmonics the stage is solved with (power.stage_pulse)."""
    from groundtruth.analysis import power
    print("Spectrum: a buck's high-side current, 10 A with 4.9 A p-p of ripple, D = 0.509, 500 kHz, 2 ns edges")
    i, di, d, f, tr = 10.0, 4.9, 0.509, 500e3, 2e-9
    n = np.arange(1, 41)
    ref = pulse_fft(i, di, d, f, tr, 40)
    have = _stage_pulse(power, n * f, f, i, d, tr, di)
    worst_even = max(abs(20 * math.log10(have[k] / ref[k])) for k in range(1, 40, 2))
    worst_odd = max(abs(20 * math.log10(have[k] / ref[k])) for k in range(0, 40, 2))
    check("spectrum", "S1 the fundamental (dB from the sampled pulse)", 0.0, 20 * math.log10(have[0] / ref[0]),
          0.2, rel=False)
    check("spectrum", "S2 odd harmonics 1-39, worst |dB|", 0.0, worst_odd, 0.5, rel=False)
    check("spectrum", "S3 even harmonics 2-40 (near the duty's nulls), worst |dB|", 0.0, worst_even, 0.5, rel=False)
    check("spectrum", "S4 the 2nd harmonic in A (the sampled pulse's)", float(ref[1]), float(have[1]), 0.02)

    print("Spectrum: two such phases 180° apart: what is left at 2·f_sw (the odd harmonics cancel)")
    # each phase's 2nd harmonic adds in phase: 2·c_2. At D = ½ exactly the flat pulse has none: all of it is the ripple
    ref5 = pulse_fft(i, di, 0.5, f, tr, 4)
    have5 = _stage_pulse(power, np.arange(1, 5) * f, f, i, 0.5, tr, di)
    check("spectrum", "S5 D = 0.5: the 2nd harmonic is the ripple's, dI/(2 pi) (closed form)", di / (2 * math.pi),
          float(ref5[1]), 0.01, note="the reference against its own closed form")
    check("spectrum", "S6 D = 0.5: the 2nd harmonic the stage is solved with", float(ref5[1]), float(have5[1]), 0.02)

    print("Spectrum: a boost's rectifier current, 3 A falling by 2 A p-p, d = 1/3, 300 kHz, 5 ns edges")
    ref = pulse_fft(3.0, 2.0, 1 / 3, 300e3, 5e-9, 30, rising=False)
    have = _stage_pulse(power, np.arange(1, 31) * 300e3, 300e3, 3.0, 1 / 3, 5e-9, 2.0)
    worst = max(abs(20 * math.log10(max(have[k], 1e-12) / ref[k])) for k in range(30))
    check("spectrum", "S7 boost: harmonics 1-30, worst |dB|", 0.0, worst, 0.5, rel=False)


def _stage_pulse(power, freqs, f_sw, amplitude, duty, tr, ripple) -> np.ndarray:
    """The harmonics GroundTruth solves a stage with: pulse_harmonics with the ripple where the code takes one
    (since round 9), else the flat trapezoid's (before)."""
    import inspect
    if "ripple" in inspect.signature(power.pulse_harmonics).parameters:
        return power.pulse_harmonics(freqs, f_sw, amplitude, duty, tr, ripple=ripple)
    return power.pulse_harmonics(freqs, f_sw, amplitude, duty, tr)


# ------------------------------------------------------------------------------------------------ structure
def structure():
    """Losses that must not depend on how the same silicon is drawn: parallel switches, an integrated boost; and
    the current at the hard edge."""
    from boardgen import boost_board, discrete_buck_board
    from groundtruth.config.settings import ConverterSpec
    print("Structure: the output charge of a side's parallel switches")
    board, cfg = _board(discrete_buck_board(), "st")
    v_in, v_out, f, q = 48.0, 12.0, 500e3, 40e-9
    base = dict(switch_node="SW", f_sw=f, i_out=5.0, v_in=v_in, v_out=v_out, rise_time=5e-9, rds_on=5e-3,
                q_oss=q, dead_rise=0.0, dead_fall=0.0)
    one = ConverterSpec("buck", "VIN", "GND", high_side=["Q1"], low_side=["Q2"], **base)
    by1, _ = _by(board, cfg, one, v_out / v_in, v_in)
    check("structure", "P1 one high side: Q_oss·V·f_sw", q * v_in * f, by1["Q1"]["output charge"], 1e-9)
    # the same stage with two parts named on the high side (Q1 and the board's C1 standing in for a second FET in
    # parallel: the loss model asks only that the part exists and is no diode): q_oss is the side's switches
    # together (the guide, components/fields.py), so the loss is the same
    two = ConverterSpec("buck", "VIN", "GND", high_side=["Q1", "C1"], low_side=["Q2"], **base)
    by2, _ = _by(board, cfg, two, v_out / v_in, v_in)
    total = sum(v.get("output charge", 0.0) for v in by2.values())
    check("structure", "P2 two in parallel: the side's Q_oss·V·f_sw, once", q * v_in * f, total, 1e-9)

    print("Structure: the hard edge's overlap is at the current the switch turns on")
    # a synchronous buck's main switch turns on at the ripple's valley I − ΔI/2 (where the dead-time model [A48]
    # already takes it): ½·V·I_v·t·f_sw
    l_h = cfg.model_for("L1").get("L")
    di = v_in * 0.25 * 0.75 / (l_h * f)
    check("structure", "P3 synchronous: ½·V·(I − ΔI/2)·t_r·f_sw", 0.5 * v_in * (5.0 - di / 2) * 5e-9 * f,
          by1["Q1"]["switching"], 1e-6, note=f"ripple {di:.2f} A p-p")

    print("Structure: a boost with both switches in one IC")
    board_b, cfg_b = _board(boost_board(), "stb")
    kw = dict(switch_node="SW", topology="boost", f_sw=300e3, i_out=1.0, v_in=12.0, v_out=36.0, rise_time=10e-9,
              rds_on=20e-3, q_oss=5e-9)
    disc = ConverterSpec("boost", "VOUT", "GND", high_side=["D1"], low_side=["Q1"], **kw)
    integ = ConverterSpec("boost", "VOUT", "GND", switch=["Q1"], **kw)
    by_d, _ = _by(board_b, cfg_b, disc, 12.0 / 36.0, 36.0)
    by_i, _ = _by(board_b, cfg_b, integ, 12.0 / 36.0, 36.0)
    check("structure", "P4 discrete boost: its switch has a switching loss", 1.0,
          float(by_d["Q1"].get("switching", 0.0) > 0), 0.0)
    check("structure", "P5 integrated boost: the IC has a switching loss too", 1.0,
          float(by_i.get("Q1", {}).get("switching", 0.0) > 0), 0.0)
    l_b = cfg_b.model_for("L1").get("L")
    i_l, rip = 3.0, 12.0 * (2 / 3) / (l_b * 300e3)
    check("structure", "P6 integrated boost: ½·V_out·(I_L − ΔI/2)·t_r·f_sw", 0.5 * 36.0 * (i_l - rip / 2) * 10e-9
          * 300e3, by_i.get("Q1", {}).get("switching", 0.0), 1e-6)
    check("structure", "P7 integrated boost: Q_oss·V_out·f_sw", 5e-9 * 36.0 * 300e3,
          by_i.get("Q1", {}).get("output charge", 0.0), 1e-9)


# ---------------------------------------------------------------------------------------------------- sweep
def sweep():
    """The efficiency sweep on a simulated stage: at each current the categories add up to the loss, and the
    efficiency is P_out/(P_out + P_loss); the energy balance against the input current."""
    from boardgen import discrete_buck_board
    from groundtruth.analysis.detect import detect_converters
    from groundtruth.analysis.simulate import simulate
    from groundtruth.config.settings import SettingsFile
    from conftest import make
    print("Sweep: the test buck 12 V -> 3 V, 3 A, 300 kHz, simulated")
    tmp = Path(tempfile.mkdtemp(prefix="converter_audit_"))
    board, sc, _ = make(tmp, discrete_buck_board(out_connector=True), "sw", nets=())
    spec = detect_converters(board, sc.config)[0].spec
    spec.f_sw, spec.i_out, spec.v_in, spec.v_out = 300e3, 3.0, 12.0, 3.0
    spec.rds_on, spec.dcr, spec.q_oss, spec.qg, spec.iq = 10e-3, 8e-3, 10e-9, 8e-9, 2e-3
    sc.set_converters([spec])
    sc.save()
    cfg = SettingsFile.load(sc.path).config
    r = simulate(board, cfg).converters[0]
    e = r.efficiency
    worst = max(abs(sum(v[k] for v in e.by.values()) - e.p_loss[k]) for k in range(len(e.currents)))
    check("sweep", "W1 the categories add up to the loss at every current (W)", 0.0, worst, 1e-9, rel=False)
    check("sweep", "W2 the stage's own loss = the sweep's at 3 A (gates and controller in both)",
          float(np.interp(3.0, e.currents, e.p_loss)), r.input["p_loss_w"], 0.01)
    check("sweep", "W3 the efficiency the stage reports = the sweep's at its current", float(
        np.interp(3.0, e.currents, e.efficiency)), r.input["efficiency"], 2e-3,
        note="one efficiency for one operating point")


def main():
    """Run the chosen areas; exit 1 when a case fails."""
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--only", default="td,spectrum,structure,sweep")
    ap.add_argument("--json")
    args = ap.parse_args()
    areas = {"td": td, "spectrum": spectrum, "structure": structure, "sweep": sweep}
    for name in args.only.split(","):
        areas[name.strip()]()
    bad = [r for r in ROWS if not r["ok"]]
    print(f"\n{len(ROWS) - len(bad)} of {len(ROWS)} cases pass" + (f"; FAIL: {', '.join(r['case'][:3].strip() for r in bad)}"
                                                                 if bad else ""))
    if args.json:
        Path(args.json).write_text(json.dumps(ROWS, indent=1), encoding="utf-8")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
