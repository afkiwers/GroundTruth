"""Audit round 11 (docs/VALIDATION_AUDIT.md): independent benchmarks of the signal paths' physics, for what the
earlier rounds left open. Every expected value is made here (a time-domain simulation, a sum written out, the
textbook formula with exact constants), never with GroundTruth's own functions.

    .venv/Scripts/python tools/dev/signal_audit.py [--only line,long,field,receiver,buffer] [--json OUT.json]

- line: a trapezoid source behind a resistance, a lossless line and a capacitive load simulated in time (travelling
  waves on a lattice of unit-delay sections, the method of characteristics), its steady state Fourier-transformed:
  the mean current along the line, the current into it and the end voltage per harmonic against emi/path.
- field: a small loop's field with exact constants, the induction term, the floor's reflection and where a
  standard's test site has one.
- receiver: what a peak detector reads of harmonics closer together than its bandwidth (the envelope of their sum
  in time) against the in-phase sum; a thinned choice of harmonics against all of them.
- buffer: a path through a logic buffer simulated with its harmonics thinned and with all of them.

Each case prints expected, actual, the deviation and PASS / FAIL against its stated tolerance. A FAIL is a finding to
investigate, not a test to loosen.
"""
from __future__ import annotations

import argparse
import inspect
import json
import math
import sys
import tempfile
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests"))             # boardgen, conftest.make: the generated KiCad boards

ROWS: list[dict] = []
C0 = 299_792_458.0
MU0 = 4e-7 * math.pi


def check(area: str, case: str, expected, actual, tol: float, rel: bool = True, note: str = "") -> bool:
    """Record one comparison: |actual − expected| within tol (relative to |expected| when rel, else absolute)."""
    dev = abs(actual - expected) / (abs(expected) if rel and expected != 0 else 1.0)
    ok = dev <= tol
    ROWS.append({"area": area, "case": case, "expected": expected, "actual": actual, "dev": dev, "tol": tol,
                 "rel": rel, "ok": ok, "note": note})
    print(f"  [{'PASS' if ok else 'FAIL'}] {area:8} {case:70} expected {expected:>11.5g}  actual {actual:>11.5g}"
          f"  dev {dev:.2e}" + (f"  ({note})" if note else ""))
    return ok


# --------------------------------------------------------------------------------------- the line in time
def trapezoid_samples(n: int, duty: float, rise: int, fall: int, amplitude: float) -> np.ndarray:
    """One period of a trapezoid on n samples: a linear rise over `rise` samples from 0, the top until the fall
    starts (the width at half height is duty·n), a linear fall over `fall` samples."""
    t = np.arange(n, dtype=float)
    start_fall = duty * n + rise / 2 - fall / 2
    up = np.clip(t / rise, 0.0, 1.0)
    down = np.clip((t - start_fall) / fall, 0.0, 1.0)
    return amplitude * (up - down)


def line_reference(z0: float, sections: int, period_steps: int, vs: np.ndarray, r_s: float, c_load: float,
                   dt: float, periods: int = 12) -> dict:
    """A source vs(t) behind r_s, a lossless line of `sections` unit-delay sections (each dt long) and a capacitor
    c_load, stepped in time: a forward and a backward travelling wave per section, each moving one section per
    step [Paul 2006] (exact for a lossless line); the source's node from its Thevenin equation, the capacitor's by
    the trapezoid rule. The last period's node currents and voltages are Fourier-transformed. Returns the complex
    peak phasors per harmonic of the source voltage, the current into the line, its mean along the line (the nodes'
    average, the ends at half weight) and the end voltage."""
    fwd = np.zeros(sections)            # the wave in section j travelling to node j + 1, arriving at the next step
    bwd = np.zeros(sections)            # the wave in section j travelling to node j
    g = 2 * c_load / dt
    v_prev, i_prev = 0.0, 0.0
    n = period_steps
    rec_in, rec_mean, rec_end = np.zeros(n), np.zeros(n), np.zeros(n)
    for step in range(periods * n):
        v_src = vs[step % n]
        b0 = bwd[0]                                           # arriving at the source's node
        a0 = (v_src * z0 + b0 * (r_s - z0)) / (z0 + r_s)      # leaving it into the line
        a_end = fwd[-1]                                       # arriving at the load
        b_end = (a_end / z0 - g * a_end + g * v_prev + i_prev) / (1 / z0 + g)
        v_end, i_end = a_end + b_end, (a_end - b_end) / z0
        if step >= (periods - 1) * n:
            k = step - (periods - 1) * n
            # node currents: the source's, the junctions' (the waves arriving there from both sides), the load's
            inner = (fwd[:-1] - bwd[1:]) / z0
            i_src = (a0 - b0) / z0
            rec_in[k] = i_src
            rec_mean[k] = (0.5 * i_src + inner.sum() + 0.5 * i_end) / sections
            rec_end[k] = v_end
        v_prev, i_prev = v_end, i_end
        fwd = np.concatenate([[a0], fwd[:-1]])
        bwd = np.concatenate([bwd[1:], [b_end]])

    def phasors(x):
        """Complex peak phasors of the harmonics 1.. of one sampled period."""
        return 2 * np.fft.rfft(x)[1:] / n
    return {"v_src": phasors(vs[:n]), "i_in": phasors(rec_in), "i_mean": phasors(rec_mean), "v_end": phasors(rec_end)}


def line():
    """The path's circuit against the travelling-wave simulation: the same line, source and load, the source's
    harmonics taken from the sampled waveform."""
    from groundtruth.emi.path import PathLine, PathLoad, PathSource, path_currents
    for name, z0, length, r_s, c_load, f_target in (
            ("100 mm, 50 Ω line, 22 Ω driver, 8 pF", 50.0, 0.100, 22.0, 8e-12, 50e6),
            ("40 mm, 65 Ω line, 10 Ω driver, 15 pF (ringing)", 65.0, 0.040, 10.0, 15e-12, 100e6),
            ("150 mm, 50 Ω line, matched 50 Ω driver, 3 pF", 50.0, 0.150, 50.0, 3e-12, 25e6)):
        print(f"Line: {name}")
        eeff, sections = 3.3, 60
        td = length * math.sqrt(eeff) / C0
        dt = td / sections
        n = int(round(1.0 / (f_target * dt)))
        f0 = 1.0 / (n * dt)
        rise = max(int(round(0.6e-9 / dt)), 4)
        vs = trapezoid_samples(n, 0.5, rise, rise, 3.3)
        ref = line_reference(z0, sections, n, vs, r_s, c_load, dt, periods=40)
        h = np.arange(1, 16)
        got = path_currents(h * f0, ref["v_src"][:15], [PathLine(z0, eeff, length)], PathSource(r_s),
                            PathLoad(c_load))
        for key, what in (("i_mean", "mean current along the line"), ("i_in", "current into the line"),
                          ("v_end", "voltage at the load")):
            want, have = ref[key][:15], getattr(got, key)
            big = np.abs(want) > 0.02 * np.abs(want).max()
            err = float(np.max(np.abs(have[big] - want[big]) / np.abs(want[big])))
            check("line", f"{what}, harmonics 1-15 (complex, worst)", 0.0, err, 0.02, rel=False)


def trace_far_field(f: float, length: float, h: float = 0.2e-3, z0: float = 50.0, eeff: float = 3.0,
                    r_s: float = 25.0, c_load: float = 5e-12, n: int = 4001) -> float:
    """The current a small loop of the trace's area (length × 2h with its image) would need for the trace's exact
    far field, per volt of source: a trace along x at height h over an infinite ground plane with its real current
    I(x) (a source behind r_s, a lossless line, a capacitor), by image theory [Balanis 2016, ch. 4]: the image
    trace at −h with −I, the vertical currents (the end posts, the displacement current −dI/dx between trace and
    plane) imaged over −h..h; the radiation vector N(r̂) = Σ I dl e^{j k r̂·r′}, its part across r̂, the maximum
    over the upper half space, over k·length·2h. For a short trace it is |mean I|."""
    w = 2 * np.pi * f
    beta = w * math.sqrt(eeff) / C0
    zl = 1 / (1j * w * c_load)
    t = np.tan(beta * length)
    zin = z0 * (zl + 1j * z0 * t) / (z0 + 1j * zl * t)
    i0 = 1.0 / (r_s + zin)
    v0 = i0 * zin
    x = np.linspace(0, length, n)
    i = i0 * np.cos(beta * x) - 1j * v0 / z0 * np.sin(beta * x)
    k = w / C0
    dx = x[1] - x[0]
    wts = np.full(n, dx)
    wts[0] = wts[-1] = dx / 2
    didx = np.gradient(i, dx)
    best = 0.0
    for th in np.linspace(0, np.pi / 2, 46):               # polar angle from the plane's normal
        for ph in np.linspace(0, np.pi, 73):
            r = np.array([np.sin(th) * np.cos(ph), np.sin(th) * np.sin(ph), np.cos(th)])
            px = np.exp(1j * k * r[0] * x)
            nx = np.sum(i * px * wts) * (np.exp(1j * k * r[2] * h) - np.exp(-1j * k * r[2] * h))
            sz = 2 * h * np.sinc(k * r[2] * h / np.pi)
            nz = sz * (i[0] * px[0] - i[-1] * px[-1] + np.sum(didx * px * wts))
            vec = np.array([nx, 0.0, nz])
            best = max(best, float(np.linalg.norm(vec - np.dot(vec, r) * r)))
    return best / (k * length * 2 * h)


def long_trace():
    """The current a path radiates with (emi.path.radiating_current) against the exact far field of the trace."""
    from groundtruth.emi.path import PathLine, PathLoad, PathSource, path_currents
    for length in (0.02, 0.05, 0.10, 0.15):
        print(f"Long trace: {length * 1e3:.0f} mm, 50 Ω, εeff 3, 25 Ω driver, 5 pF")
        fs = np.geomspace(30e6, 6e9, 60)
        got = path_currents(fs, np.ones(len(fs)), [PathLine(50.0, 3.0, length)], PathSource(25.0), PathLoad(5e-12))
        want = np.array([trace_far_field(float(f), length) for f in fs])
        err = 20 * np.log10(got.i_rad / want)
        check("long", "radiating current against the exact field, 30 MHz-6 GHz (dB, worst)", 0.0,
              float(np.max(np.abs(err))), 0.3, rel=False)
        low = 20 * np.log10(want / np.maximum(np.abs(got.i_mean), 1e-30))
        print(f"      (the mean current alone reads up to {low.max():.1f} dB low)")


# ------------------------------------------------------------------------------------------------ the field
def field():
    """A small loop's broadside field [Balanis 2016, §5.2]: |E| = η0·k²·m/(4π·r)·|1 + 1/(jkr)| with η0 = μ0·c,
    k = 2πf/c, the constants exact; the floor's reflection doubles it where the test site has a reflecting floor."""
    from groundtruth.emi.radiation import RE102_LIMITS, emission_arrays, loop_field
    print("Field: a small loop, m = 1e-6 A m^2")
    m = 1e-6
    for f, r in ((100e6, 10.0), (1e9, 10.0), (30e6, 1.0), (2e6, 1.0)):
        k = 2 * math.pi * f / C0
        want = MU0 * C0 * k * k * m / (4 * math.pi * r) * abs(1 + 1 / (1j * k * r))
        check("field", f"free space, {f / 1e6:g} MHz at {r:g} m (V/m)", want, float(loop_field(f, m, r, False)), 3e-3)
    print("Field: what the estimate reads, 1 A·mm² per harmonic")
    for f, floor, cls, what in ((500e6, 2.0, "B", "CISPR 32 at 500 MHz, 10 m: a reflecting floor (OATS / SAC)"),
                                (1.5e9, 1.0, "B", "CISPR 32 at 1.5 GHz, 3 m: absorber on the floor (FSOATS)"),
                                (4e9, 1.0, "A", "CISPR 32 class A at 4 GHz, 3 m: absorber on the floor"),
                                (1.5e9, 2.0, RE102_LIMITS["re102_ground_army"], "RE102 at 1.5 GHz, 1 m: the bench")):
        a = emission_arrays([f], [1.0], [m], cls)
        r = float(a["distance_m"][0])
        k = 2 * math.pi * f / C0
        e = floor * MU0 * C0 * k * k * m / (4 * math.pi * r) * abs(1 + 1 / (1j * k * r))
        check("field", what + " (dBµV/m)", 20 * math.log10(e / math.sqrt(2) / 1e-6), float(a["e_dbuv"][0]), 0.05,
              rel=False)


# --------------------------------------------------------------------------------------------- the receiver
def edge_harmonics(n, f0: float, duty: float, tr: float, amplitude: float) -> np.ndarray:
    """Complex Fourier coefficients X_n of a trapezoid (rise at t = 0, equal edges tr, width duty/f0 at half
    height), from its derivative: two rectangular pulses of height ±A/tr [Paul 2006]: X_n = A/(j2πn)·
    sinc(n·f0·tr)·(1 − e^(−j2πnD))·e^(−jπn·f0·tr)."""
    n = np.asarray(n, float)
    return amplitude / (2j * np.pi * n) * np.sinc(n * f0 * tr) * (1 - np.exp(-2j * np.pi * n * duty)) \
        * np.exp(-1j * np.pi * n * f0 * tr)


def envelope_peak(x_n: np.ndarray, n: np.ndarray, samples: int = 4096) -> float:
    """The peak of the envelope of Σ 2·X_n·e^(j2πn·t/T) over one period: what an ideal rectangular filter holding
    exactly these harmonics passes to a peak detector."""
    t = np.arange(samples) / samples
    env = np.abs((2 * x_n[None, :] * np.exp(2j * np.pi * (n[None, :] - n[0]) * t[:, None])).sum(axis=1))
    return float(env.max())


def _reading(freqs, peaks, **kw):
    """emission_arrays' reading (dBµV/m) of harmonics with the given peak amplitudes as the loop moment (1 A)."""
    from groundtruth.emi.radiation import emission_arrays
    if kw and "harmonic_of" not in inspect.signature(emission_arrays).parameters:
        kw = {}                         # the code before round 11: a thinned set read as it stands
    return emission_arrays(freqs, np.ones(len(freqs)), peaks, "B", **kw)["e_dbuv"]


def receiver():
    """Harmonics closer together than the receiver's bandwidth, and a thinned choice of them."""
    from groundtruth.emi.radiation import receiver_sum
    f0, tr, a = 1e3, 2.5e-9, 5.0
    print("Receiver: a 1 kHz trapezoid with 2.5 ns edges in a 120 kHz bandwidth at 100 MHz")
    for duty, tol_db, what in ((0.5, 0.1, "50 % duty: the in-phase sum is the envelope's peak"),
                               (0.3, 2.6, "30 % duty: the in-phase sum is an upper bound, about 2 dB")):
        centre = int(round(100e6 / f0))
        n = np.arange(centre - 60, centre + 61)
        x = edge_harmonics(n, f0, duty, tr, a)
        live = np.abs(x) > 1e-12 * np.abs(x).max()
        peak = envelope_peak(x[live], n[live].astype(float))
        summed = float(receiver_sum(n * f0, 2 * np.abs(x))[60])
        over = 20 * math.log10(summed / peak)
        check("receiver", what + " (dB over the envelope)", 0.0, over, tol_db, rel=False)
        check("receiver", "  and never below it", 1.0, float(over > -0.05), 0.0)
    print("Receiver: every harmonic of it to 1.3 GHz against 4000 log-spaced ones (a path through a logic buffer)")
    for duty in (0.5, 0.3):
        n_max = int(1.3e9 // f0)
        n_all = np.arange(1, n_max + 1)
        if duty == 0.5:
            n_all = n_all[n_all % 2 == 1]
        full = _reading(n_all * f0, 2 * np.abs(edge_harmonics(n_all, f0, duty, tr, a)) * 1e-9)
        from groundtruth.analysis import simulate as sim
        if hasattr(sim, "thin_harmonics"):                    # the code's own choice of harmonics
            thin = sim.thin_harmonics(n_max, duty, 4000)
        else:                                                 # before round 11
            thin = np.unique(np.round(np.geomspace(1, n_max, 4000)).astype(np.int64))
            if duty == 0.5:
                thin = np.unique(thin | 1)
            thin = thin[thin <= n_max]
        c_thin = 2 * np.abs(edge_harmonics(thin, f0, duty, tr, a)) * 1e-9
        got = _reading(thin * f0, c_thin, harmonic_of=(f0, duty))
        # the worst (highest) reading in the limit's band, both ways: what the margin is taken from
        band_all, band_thin = (n_all * f0 >= 30e6), (thin * f0 >= 30e6)
        check("receiver", f"duty {duty:g}: the highest reading from 30 MHz, thinned against all (dB)",
              float(full[band_all].max()), float(got[band_thin].max()), 1.0, rel=False)
        k = int(np.argmin(np.abs(thin * f0 - 200e6)))
        j = int(np.argmin(np.abs(n_all * f0 - thin[k] * f0)))
        lo, hi = max(j - 400, 0), j + 400
        check("receiver", f"duty {duty:g}: the reading near 200 MHz, thinned against all (dB)",
              float(full[lo:hi].max()), float(got[max(k - 3, 0):k + 4].max()), 1.5, rel=False)


# ------------------------------------------------------------------------------------- a buffered path
def buffer():
    """A 20 kHz signal through a Schmitt buffer with a 4 ns edge (16 000 harmonics to its band's top): Simulate
    with the harmonics thinned to 4000 and with all of them gives the same worst emission."""
    from boardgen import buffer_path_board
    from conftest import make
    from groundtruth.analysis import simulate as sim
    from groundtruth.config.settings import NetSpec
    print("Buffer: J1 -> 10 kΩ -> Schmitt buffer (4 ns) -> 1 kΩ -> U2, a 20 kHz signal (simulated twice)")
    fields = {"Threshold Rising": "2.5V", "Threshold Falling": "1.6V", "Rise Time": "4ns"}
    out = {}
    for label, cap in (("thinned", sim.HARMONICS_MAX), ("all", 10 ** 9)):
        tmp = Path(tempfile.mkdtemp(prefix="signal_audit_"))
        spec = NetSpec("HW", "J1.1", ["U2.1"], 20e3, 2e-6, through=["R1"], duty=0.3)
        board, sc, _ = make(tmp, buffer_path_board(**fields), "buf", nets=(spec,), solve_points=3, max_solves=4)
        old = sim.HARMONICS_MAX
        sim.HARMONICS_MAX = cap
        try:
            res = sim.simulate(board, sc.config).routes[0]
        finally:
            sim.HARMONICS_MAX = old
        out[label] = res
        print(f"    {label}: {len(res.emissions)} rows, worst {res.worst['e_dbuv']:.2f} dBµV/m at "
              f"{res.worst['f'] / 1e6:.4g} MHz (margin {res.worst['margin_db']:+.2f} dB)")
    check("buffer", "the worst emission, thinned against every harmonic (dBµV/m)", out["all"].worst["e_dbuv"],
          out["thinned"].worst["e_dbuv"], 1.0, rel=False)
    check("buffer", "its margin (dB)", out["all"].worst["margin_db"], out["thinned"].worst["margin_db"], 1.0,
          rel=False)


def main():
    """Run the chosen areas; exit 1 when a case fails."""
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--only", default="line,long,field,receiver,buffer")
    ap.add_argument("--json")
    args = ap.parse_args()
    areas = {"line": line, "long": long_trace, "field": field, "receiver": receiver, "buffer": buffer}
    for name in args.only.split(","):
        areas[name.strip()]()
    bad = [r for r in ROWS if not r["ok"]]
    print(f"\n{len(ROWS) - len(bad)} of {len(ROWS)} cases pass")
    for r in bad:
        print(f"  FAIL {r['area']}: {r['case']}")
    if args.json:
        Path(args.json).write_text(json.dumps(ROWS, indent=1, default=float), encoding="utf-8")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
