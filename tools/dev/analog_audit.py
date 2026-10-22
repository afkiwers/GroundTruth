"""Audit round 7 (docs/ANALOG_AUDIT.md): independent benchmarks of the DC-bias derating, the MNA solver, the op-amp
loop gain and phase margin, the transfer functions, the step response and the bandwidth check. Every expected value
is derived here by hand (closed forms in the comments) or by an independent tool (SciPy's LTI routines; Lcapy's
symbolic nodal analysis, `tools/dev/lcapy_reference.py` run in its own venv, JSON in --lcapy), never from
GroundTruth's own functions, then compared with what GroundTruth gives.

    .venv/Scripts/python tools/dev/analog_audit.py [--only dcbias,mna,loop,tf,step,bw] [--lcapy REF.json]
        [--json OUT.json]

Each case prints expected, actual, the deviation and PASS / FAIL against its stated tolerance. A FAIL is a finding to
investigate, not a test to loosen.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import tempfile
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests"))             # boardgen, conftest.make: the generated KiCad boards

ROWS: list[dict] = []


def check(area: str, case: str, expected, actual, tol: float, rel: bool = True, note: str = "") -> bool:
    """Record one comparison: |actual − expected| within tol (relative to |expected| when rel, else absolute);
    a None on either side compares by identity (both None passes)."""
    if expected is None or actual is None:
        ok = expected is None and actual is None
        dev = None
    else:
        dev = abs(actual - expected) / (abs(expected) if rel and expected != 0 else 1.0)
        ok = dev <= tol
    ROWS.append({"area": area, "case": case, "expected": expected, "actual": actual, "dev": dev, "tol": tol,
                 "rel": rel, "ok": ok, "note": note})
    flag = "PASS" if ok else "FAIL"
    fmt = lambda v: "None" if v is None else f"{v:.6g}"          # noqa: E731
    print(f"  [{flag}] {area:7} {case:62} expected {fmt(expected):>12}  actual {fmt(actual):>12}"
          + (f"  dev {dev:.2e}" if dev is not None else "") + (f"  ({note})" if note else ""))
    return ok


# --------------------------------------------------------------------------------------------------- DC bias
def dcbias():
    """DC-bias derating: the curve against its closed form, and the voltage across capacitors on generated boards
    against the circuit's real DC operating point (hand-derived)."""
    from groundtruth.components.bias import c_at, parse_bias
    print("DC bias: the curve")
    c0 = 100e-9
    v1 = 40.0 / math.sqrt(100 / 40 - 1)          # 40 nF @ 40 V: V1 = Vb / sqrt(C0/Cb - 1) = 32.660 V
    pts = [(40.0, 40e-9)]
    check("dcbias", "B1 zero bias: C(0) = C0", c0, c_at(pts, c0, 0.0), 1e-12)
    check("dcbias", "B2 at the point: C(40 V) = 40 nF", 40e-9, c_at(pts, c0, 40.0), 1e-12)
    check("dcbias", "B2 12 V: C0/(1+(12/V1)^2)", c0 / (1 + (12 / v1) ** 2), c_at(pts, c0, 12.0), 1e-12)
    check("dcbias", "B7 sign: C(-12 V) = C(+12 V) (class II: even in E)", c_at(pts, c0, 12.0),
          c_at(pts, c0, -12.0), 1e-12)
    curve = [(10.0, 95e-9), (30.0, 64e-9), (50.0, 39e-9), (80.0, 19e-9)]
    for v, c in curve:
        check("dcbias", f"B9 a list through its point {v:g} V", c, c_at(curve, c0, v), 1e-12)
    # B10 beyond the last point: the one-point form through (80 V, 19 nF), continuous: C(80)·f(V)/f(80)
    v1l = 80 / math.sqrt(c0 / 19e-9 - 1)
    want = 19e-9 * (1 / (1 + (100 / v1l) ** 2)) / (1 / (1 + (80 / v1l) ** 2))
    check("dcbias", "B10 beyond the last point (100 V): the tail", want, c_at(curve, c0, 100.0), 1e-12)
    for bad in ("75nF@10V, 60nF@10V", "75nF, 50nF", "-100%@5V", "0nF@5V"):
        check("dcbias", f"B11 refused: {bad!r}", 1.0, 1.0 if parse_bias(bad)[1] else 0.0, 0, rel=False)

    print("DC bias: the voltage across each capacitor (generated boards)")
    from boardgen import opamp_circuit_board
    from conftest import make
    from groundtruth.analysis.bias import at_dc, cap_voltages
    parts = [("C3", "100n", "C", "+12V", "+5V"),                     # between two rails: 7 V
             ("C10", "100n", "C", "+12V", "MIDC"), ("C11", "100n", "C", "MIDC", "GND"),   # series: DC undefined
             ("R1", "10k", "R", "+12V", "MID"), ("R2", "10k", "R", "MID", "GND"),
             ("C5", "100n", "C", "MID", "GND"),                       # a divider's midpoint: 6 V
             ("R5", "10k", "R", "+5V", "VREF"), ("R6", "10k", "R", "VREF", "GND"),       # VREF = 2.5 V
             ("C14", "1u", "C", "SIG_IN", "RIN"), ("R3", "10k", "R", "RIN", "INN"),
             ("R4", "100k", "R", "INN", "AMP_OUT"), ("C13", "1n", "C", "INN", "AMP_OUT"),  # across Rf: 0 V
             ("C7", "100n", "C", "+12V", "-12V")]                     # across the two rails: 24 V
    pins = {3: ("+IN", "VREF"), 2: ("-IN", "INN"), 1: ("OUT", "AMP_OUT"), 5: ("V+", "+5V"), 4: ("V-", "GND")}
    g = opamp_circuit_board(parts, [("U3", pins)], out_net="AMP_OUT")
    for ref in ("C3", "C10", "C11", "C5", "C13", "C7"):
        g.fields(ref, **{"Cbias": "40nF@40V" if ref != "C13" else "0.4nF@40V", "Rated Voltage": "100V"})
    with tempfile.TemporaryDirectory() as tmp:
        board, sc, _ = make(Path(tmp), g, "audit", nets=())
        from groundtruth.components.models import ComponentModel
        sc.config.components["U3"].model = ComponentModel("amplifier", {"gbw": 1e6})   # an op-amp stage
        v = cap_voltages(board, sc.config)
        _, rows, notes = at_dc(board, sc.config)
    by = {r.ref: r for r in rows}
    check("dcbias", "B3 between +12V and +5V: 7 V", 7.0, v["C3"][0], 1e-9)
    check("dcbias", "B4 series capacitors: DC undefined (leakage): unknown", None, v["C10"][0],
          0, note="both reported unknown")
    check("dcbias", "B4 ... its partner too", None, v["C11"][0], 0)
    check("dcbias", "B5 a 10k/10k divider's midpoint from 12 V: 6 V", 6.0, v["C5"][0], 1e-6,
          note="the real DC operating point")
    check("dcbias", "B6 Cf across Rf of an AC-coupled inverting stage: 0 V", 0.0, v["C13"][0], 1e-6, rel=False,
          note="virtual ground: out = in- = VREF at DC")
    check("dcbias", "B7 between +12V and -12V: 24 V", 24.0, v["C7"][0], 1e-9)
    check("dcbias", "B6 Cf's capacitance used: its nominal 1 nF (0 V)", 1e-9, by["C13"].c, 1e-6)
    print("  notes:", *notes, sep="\n    ")


# --------------------------------------------------------------------------------------------------- MNA
def _gt_circuits():
    """GroundTruth's emi/mna.Circuit of each Lcapy benchmark (tools/dev/lcapy_reference.CIRCUITS), the op-amp as
    Circuit.opamp with c_in 0 and r_in open (the reference's macromodel has neither)."""
    from groundtruth.emi.mna import Circuit, OpAmpParams

    def base():
        """A circuit with its 1 V input on node "in"."""
        c = Circuit()
        c.vsource("in", "0")
        return c
    amp = lambda a0, gbw, fp2=None, ro=1.0: OpAmpParams(gbw=gbw, a0=a0, fp2=fp2, r_out=ro, r_in=1e16, c_in=0.0)  # noqa
    out = {}
    c = base(); c.r("in", "out", 1e3); c.c("out", "0", 10e-9); out["rc_lowpass"] = c                   # noqa: E702
    c = base(); c.r("in", "a", 15.811388); c.l("a", "out", 1e-3); c.c("out", "0", 1e-6)                # noqa: E702
    out["rlc_series_q2"] = c
    c = base(); c.r("in", "inn", 1e3); c.r("inn", "out", 10e3)                                           # noqa: E702
    c.opamp("U", "0", "inn", "out", amp(1e5, 1e6, 20e6, 50.0)); out["inverting_x10"] = c                 # noqa: E702
    c = base(); c.r("inn", "0", 10e3); c.r("inn", "out", 10e3); c.c("out", "0", 1e-9)                    # noqa: E702
    c.opamp("U", "in", "inn", "out", amp(3.16e5, 1e6, 2e6, 100.0)); out["noninv_x2_cload"] = c           # noqa: E702
    c = base(); c.r("in", "a", 10e3); c.r("a", "b", 10e3); c.c("a", "out", 20e-9); c.c("b", "0", 10e-9)  # noqa: E702
    c.opamp("U", "b", "out", "out", amp(1e5, 10e6)); out["sallen_key"] = c                               # noqa: E702
    c = base(); c.r("in", "a", 15.9e3); c.c("a", "inn", 10e-9); c.c("a", "out", 10e-9)                   # noqa: E702
    c.r("inn", "out", 318e3); c.r("a", "0", 1.6e3); c.opamp("U", "0", "inn", "out", amp(1e6, 50e6))      # noqa: E702
    out["mfb_bandpass"] = c
    c = base(); c.r("in", "a", 1e6); c.c("a", "0", 1e-12); c.r("a", "b", 1e-1); c.l("b", "out", 1e-9)    # noqa: E702
    c.c("out", "0", 1e-3); c.r("out", "0", 1e7); out["wide_values"] = c                                  # noqa: E702
    return out


def _match_roots(a, b) -> float:
    """The largest relative distance between two root sets matched greedily (nearest first); inf when the counts
    differ."""
    a, b = list(a), list(b)
    if len(a) != len(b):
        return math.inf
    worst = 0.0
    for r in sorted(a, key=abs):
        j = min(range(len(b)), key=lambda k: abs(b[k] - r))
        worst = max(worst, abs(b[j] - r) / max(abs(r), 1e-30))
        b.pop(j)
    return worst


def mna(lcapy_json):
    """The MNA solver against Lcapy (H at 61 frequencies 1 Hz-1 GHz, the poles and zeros) and against closed forms."""
    print("MNA: against closed forms")
    gt = _gt_circuits()
    f = np.geomspace(1.0, 1e9, 61)
    w = 2 * np.pi * f
    h = gt["rc_lowpass"].transfer(f, "out")
    check("mna", "M1 RC low-pass 1/(1+jωRC), max rel. error", 0.0,
          float(np.max(np.abs(h - 1 / (1 + 1j * w * 1e-5)) / np.abs(1 / (1 + 1j * w * 1e-5)))), 1e-12, rel=False)
    lc, cc, rr = 1e-3, 1e-6, 15.811388
    ref = 1 / (1 - w ** 2 * lc * cc + 1j * w * rr * cc)               # series RLC, output across C
    check("mna", "M2 series RLC (Q 2) 1/(1−ω²LC+jωRC)", 0.0,
          float(np.max(np.abs(gt["rlc_series_q2"].transfer(f, "out") - ref) / np.abs(ref))), 1e-10, rel=False)
    p, z, k = gt["rlc_series_q2"].poles_zeros("out")
    want = np.roots([lc * cc, rr * cc, 1.0])
    check("mna", "M2 its poles = roots of LCs²+RCs+1", 0.0, _match_roots(p, want), 1e-9, rel=False)
    check("mna", "M2 its DC gain k = 1", 1.0, abs(k), 1e-9)
    # an inverting x10 with A(s) = A0/((1+s/ω1)(1+s/ω2)) and R_O 50 Ω: no closed form with R_O loading inn; Lcapy
    if lcapy_json is None:
        print("  (no --lcapy: the Lcapy comparisons are skipped)")
        return
    print("MNA: against Lcapy (tools/dev/lcapy_reference.py, an independent symbolic nodal analysis)")
    ref = json.loads(Path(lcapy_json).read_text(encoding="utf-8"))
    for name, c in gt.items():
        r = ref[name]
        fr = np.array(r["f"])
        hr = np.array(r["re"]) + 1j * np.array(r["im"])
        hg = c.transfer(fr, "out")
        err = float(np.max(np.abs(hg - hr) / np.maximum(np.abs(hr), 1e-300)))
        check("mna", f"M3 {name}: H(jω) vs Lcapy, max rel. error (61 freqs)", 0.0, err, 1e-8, rel=False)
        p, z, _ = c.poles_zeros("out")
        lp = [complex(*x) for x in r["poles"]]
        lz = [complex(*x) for x in r["zeros"]]
        # AA-9 (documented): a root below 1e-6·2π·F_REF (1 mHz) is listed at the origin (a board's 1 TΩ node leaks
        # make such roots residue); wide_values has a real one at 0.18 mHz: checked as that, the others as roots
        origin = 1e-6 * 2 * math.pi * 1e3
        slow = [x for x in lp if abs(x) < origin]
        if slow:
            check("mna", f"M4 {name}: its {len(slow)} root(s) below 1 mHz listed at the origin (AA-9)", float(len(slow)),
                  float(sum(1 for x in p if x == 0)), 0, rel=False, note="a documented limitation")
            lp = [x for x in lp if abs(x) >= origin]
            p = [x for x in p if x != 0]
        check("mna", f"M4 {name}: poles vs Lcapy's denominator roots", 0.0, _match_roots(p, lp), 1e-6, rel=False,
              note=f"{len(p)} vs {len(lp)}")
        # zeros: Lcapy's numerator may hold roots far beyond the circuit's band (R_O feedthrough) that the QZ
        # leaves out above 1e12 rad/s; compare those within that range
        lz = [x for x in lz if abs(x) < 1e12]
        check("mna", f"M4 {name}: zeros vs Lcapy's numerator roots (< 1e12 rad/s)", 0.0, _match_roots(z, lz),
              1e-6, rel=False, note=f"{len(z)} vs {len(lz)}")


# --------------------------------------------------------------------------------------------------- loop gain
def _a_ol(f, a0, gbw, fp2=None):
    """The macromodel's open-loop gain A(jf) = A0 / ((1 + jf/f1)(1 + jf/fp2)), f1 = gbw/a0 (the same A(s) the
    circuit is built with; its loop gain T = A·β is derived by hand per case below)."""
    f = np.asarray(f, float)
    a = a0 / (1 + 1j * f / (gbw / a0))
    return a / (1 + 1j * f / fp2) if fp2 else a


def _crossings(f, t):
    """Every |T| = 1 crossing of a dense sweep (log-linear interpolation) with 180° + ∠T there: the reference."""
    mag = np.abs(t)
    ph = np.degrees(np.unwrap(np.angle(t)))
    out = []
    for k in range(1, len(f)):
        if (mag[k - 1] - 1) * (mag[k] - 1) < 0:
            u = math.log(mag[k - 1]) / (math.log(mag[k - 1]) - math.log(mag[k]))
            fc = math.exp(math.log(f[k - 1]) + u * math.log(f[k] / f[k - 1]))
            out.append((fc, 180 + ph[k - 1] + u * (ph[k] - ph[k - 1])))
    return out


def loop():
    """Circuit.loop_gain and transfer.phase_margin against T = A(s)·β derived by hand, a capacitive load, positive
    feedback and a loop with three crossovers."""
    from scipy.optimize import brentq
    from groundtruth.analysis.transfer import phase_margin
    from groundtruth.emi.mna import Circuit, OpAmpParams
    print("Loop gain and phase margin")
    f = np.geomspace(1.0, 1e9, 4000)

    def fc_pm(tfun, lo=1.0, hi=1e9):
        """The crossover of |T(f)| = 1 by root finding on the closed form, and 180° + ∠T there."""
        fc = brentq(lambda x: abs(tfun(x)) - 1.0, lo, hi, xtol=1e-12, rtol=1e-14)
        return fc, 180 + math.degrees(np.angle(tfun(fc)))
    # L1 a unity follower, one pole: T = A(s)·1; fc = f1·sqrt(A0² − 1), PM = 180 − atan(fc/f1)
    c = Circuit(); c.vsource("in", "0")                                                         # noqa: E702
    c.opamp("U", "in", "out", "out", OpAmpParams(gbw=1e6, a0=1e5, r_out=1e-6, r_in=1e16, c_in=0.0))
    t = c.loop_gain("U", f)
    check("loop", "L1 follower: T(jf) = A(jf), max rel. error", 0.0,
          float(np.max(np.abs(t - _a_ol(f, 1e5, 1e6)) / np.abs(_a_ol(f, 1e5, 1e6)))), 1e-6, rel=False)
    fc, pm = fc_pm(lambda x: _a_ol(x, 1e5, 1e6))
    m = phase_margin(f, t)
    check("loop", "L1 its crossover", fc, m["f_c"], 1e-4)
    check("loop", "L1 its phase margin (90.0006°)", pm, m["pm"], 1e-4)
    # L2 non-inverting x10 (R2 9k / R1 1k), fp2 5 MHz: β = R1/(R1+R2) = 0.1, T = A(s)·β
    c = Circuit(); c.vsource("in", "0"); c.r("inn", "0", 1e3); c.r("inn", "out", 9e3)          # noqa: E702
    c.opamp("U", "in", "inn", "out", OpAmpParams(gbw=10e6, a0=1e5, fp2=5e6, r_out=1e-6, r_in=1e16, c_in=0.0))
    t = c.loop_gain("U", f)
    ref = lambda x: _a_ol(x, 1e5, 10e6, 5e6) * 0.1                                              # noqa: E731
    check("loop", "L2 non-inverting x10: T = A·β, max rel. error", 0.0,
          float(np.max(np.abs(t - ref(f)) / np.abs(ref(f)))), 1e-5, rel=False)
    fc, pm = fc_pm(ref)
    m = phase_margin(f, t)
    check("loop", "L2 its crossover", fc, m["f_c"], 1e-4)
    check("loop", "L2 its phase margin", pm, m["pm"], 1e-3, rel=False, note="degrees, absolute")
    # L3 a follower with R_O 100 Ω into 10 nF: T = A(s) / (1 + s·R_O·C_L) (β = 1, the output pole in the loop)
    c = Circuit(); c.vsource("in", "0"); c.c("out", "0", 10e-9)                                 # noqa: E702
    c.opamp("U", "in", "out", "out", OpAmpParams(gbw=10e6, a0=1e5, fp2=30e6, r_out=100.0, r_in=1e16, c_in=0.0))
    t = c.loop_gain("U", f)
    ref = lambda x: _a_ol(x, 1e5, 10e6, 30e6) / (1 + 2j * math.pi * x * 100 * 10e-9)           # noqa: E731
    fc, pm = fc_pm(ref)
    m = phase_margin(f, t)
    check("loop", "L3 capacitive load: crossover", fc, m["f_c"], 1e-4)
    check("loop", "L3 capacitive load: phase margin", pm, m["pm"], 1e-3, rel=False, note=f"{pm:.2f}°")
    # L4 positive feedback (a follower with its inputs swapped): T(0) = −A0 < 0: the loop latches (unstable);
    # expected: no stable phase margin reported (None, or a margin that says unstable, below 0)
    c = Circuit(); c.vsource("in", "0")                                                         # noqa: E702
    c.opamp("U", "out", "in", "out", OpAmpParams(gbw=1e6, a0=1e5, r_out=1e-6, r_in=1e16, c_in=0.0))
    t = c.loop_gain("U", f)
    # at the sweep's first frequency (1 Hz, above the 10 Hz open-loop pole): T = −A(j·1 Hz), its real part
    check("loop", "L4 positive feedback: T(1 Hz) = −A(j·1 Hz)", float(-_a_ol(f[:1], 1e5, 1e6)[0].real),
          float(t[0].real), 1e-6)
    lead_f = np.geomspace(1e-9, 1.0, 60, endpoint=False)          # as simulate.network_responses gives it
    m = phase_margin(f, t, lead=c.loop_gain("U", lead_f))
    check("loop", "L4 positive feedback reported unstable (PM < 0 or None)", 1.0,
          1.0 if (m is None or m["pm"] < 0) else 0.0, 0, rel=False,
          note=f"phase_margin gives {None if m is None else round(m['pm'], 1)}°")
    # L5 three crossovers: T = 100·ω1/(s+ω1) × (1 + 49·x/(1 + x + y²)), x = s/(Q ωr), y = s/ωr: a resonant bump
    # (x50 at fr) above the first crossover lifts |T| through 1 again; the loop's margin is the smallest
    w1, fr, q = 2 * math.pi * 1e3, 2e6, 20.0
    sv = 2j * math.pi * f
    xx, yy = sv / (q * 2 * math.pi * fr), sv / (2 * math.pi * fr)
    t = 100 * w1 / (sv + w1) * (1 + 49 * xx / (1 + xx + yy ** 2))
    cr = _crossings(f, t)
    m = phase_margin(f, t)
    check("loop", f"L5 {len(cr)} crossovers: the margin reported is the smallest", min(p for _, p in cr), m["pm"],
          1e-3, rel=False, note="crossings at " + ", ".join(f"{x / 1e3:.0f} kHz ({p:.0f}°)" for x, p in cr))


# --------------------------------------------------------------------------------------------------- tf, step
def tf():
    """The displayed normal form (its doublets cancelled) against the circuit's own response, near a high-Q
    pole-zero pair (transfer.normal_form, simulate.cancel_doublets)."""
    from groundtruth.analysis.simulate import cancel_doublets
    print("Transfer function: doublets")
    wp, qp = 2 * math.pi * 1e6, 2000.0
    for rel in (5e-4, 9e-4):
        wz = wp * (1 + rel)
        p = np.roots([1, wp / qp, wp ** 2])
        z = np.roots([1, wz / qp, wz ** 2])
        pk, zk, n = cancel_doublets(list(p), list(z))
        f = np.linspace(0.99e6, 1.01e6, 20001)
        s = 2j * math.pi * f
        h = (s ** 2 + s * wz / qp + wz ** 2) / (s ** 2 + s * wp / qp + wp ** 2) * (wp / wz) ** 2
        dev = float(np.max(np.abs(20 * np.log10(np.abs(h)))))
        # with the pair cancelled the normal form reads H = 1 (0 dB): the deviation it hides is the curve's
        check("tf", f"T1 Q {qp:g} pair {rel:.2%} apart: dB the shown H(s) misses (cancelled: {n})",
              0.0, dev if n else 0.0, 0.5, rel=False, note="the plotted curve is the circuit's own; only the "
                                                           "listed roots and written H(s) drop the pair")


def step():
    """Circuit.step and transfer.step_metrics against closed forms, at the time grid simulate._step_of picks, for
    lightly damped circuits and an unstable one."""
    from types import SimpleNamespace
    from groundtruth.analysis.simulate import _step_of
    from groundtruth.analysis.transfer import normal_form
    from groundtruth.emi.mna import Circuit
    print("Step response")
    for q in (2.0, 20.0, 100.0, 300.0):
        lc, cc = 1e-3, 1e-6
        c = Circuit(); c.vsource("in", "0"); c.r("in", "a", math.sqrt(lc / cc) / q)              # noqa: E702
        c.l("a", "b", lc); c.c("b", "0", cc)                                                        # noqa: E702
        p, z, k = c.poles_zeros("b")
        st = _step_of(SimpleNamespace(circuit=c), "b", list(p), normal_form(p, z, k), np.array([1.0, 1e6]), list(z))
        zeta = 1 / (2 * q)
        w0 = 1 / math.sqrt(lc * cc)
        os_ = 100 * math.exp(-math.pi * zeta / math.sqrt(1 - zeta ** 2))
        check("step", f"S1 series RLC Q {q:g}: overshoot {os_:.1f} % (closed form)", os_,
              st["metrics"]["overshoot_pct"], 0.005, note=f"{len(st['t'])} steps over {st['t'][-1] * w0 / (2 * math.pi):.0f} periods")
    # S2 unstable: a negative conductance (a VCCS of 2 mS on a node with 1 kΩ to the input and 1 µF): the pole at
    # +(gm − 1/R)/C = +1000 /s; the step grows without bound: no final value may be reported
    c = Circuit(); c.vsource("in", "0"); c.r("in", "a", 1e3); c.c("a", "0", 1e-6)               # noqa: E702
    c.vccs("0", "a", "a", "0", 2e-3)
    p, z, k = c.poles_zeros("a")
    check("step", "S2 its pole: +1000 /s (right half-plane)", 1000.0, float(max(p.real)), 1e-6)
    st = _step_of(SimpleNamespace(circuit=c), "a", list(p), normal_form(p, z, k), np.array([1.0, 1e6]))
    m = st["metrics"] if st else None
    check("step", "S2 unstable: no final value or settling reported", 1.0,
          1.0 if (m is None or (m["settling"] is None and m.get("unstable"))) else 0.0, 0, rel=False,
          note=(f"metrics: final {m['final']}, settling {m['settling']}, unstable {m.get('unstable')}" if m else "no step"))


def bw():
    """transfer.upper_3db (the bandwidth_limit check's) and gain_figures (the −3 dB tile's) against closed forms: a
    single pole, a second-order low-pass with peaking, a coarse sweep."""
    from groundtruth.analysis.transfer import Response, gain_figures, upper_3db
    print("Bandwidth")
    fc = 1e3
    for pts in (300, 30, 8):
        f = np.geomspace(1.0, 1e6, pts)
        h = 1 / (1 + 1j * f / fc)
        # the sweep starts at 1 Hz, not DC: its reference |H(1 Hz)| = 1/sqrt(1 + 1e-6) moves the point ~5e-7
        check("bw", f"W1 one pole at 1 kHz, {pts} points: upper_3db", fc, upper_3db(f, h), 1e-5)
    for q in (0.707, 1.0, 2.0):
        f = np.geomspace(1.0, 1e6, 300)
        x = f / fc
        h = 1 / (1 - x ** 2 + 1j * x / q)
        # the −3 dB point relative to the DC gain (the usual low-pass bandwidth [Franco 2015]): |H|² = 1/2
        # ⇔ (1 − x²)² + x²/q² = 2: x² = (2 − 1/q² + sqrt((2 − 1/q²)² + 4)) / 2
        b = 2 - 1 / q ** 2
        want = fc * math.sqrt((b + math.sqrt(b * b + 4)) / 2)
        g = gain_figures(Response("t", "t", "voltage gain", f, h))
        check("bw", f"W2 2nd order Q {q:g}: the tile's −3 dB (gain_figures)", want, g["f_3db"], 2e-3)
        check("bw", f"W2 2nd order Q {q:g}: the check's bandwidth (upper_3db)", want, upper_3db(f, h), 2e-3,
              note="from the passband (DC) gain")


# --------------------------------------------------------------------------------------------------- main
def main(argv=None) -> int:
    """Run the chosen areas and print the summary; --json writes every row."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", default="dcbias,mna,loop,tf,step,bw")
    ap.add_argument("--lcapy", type=Path, help="Lcapy reference values (tools/dev/lcapy_reference.py)")
    ap.add_argument("--json", type=Path)
    args = ap.parse_args(argv)
    areas = {"dcbias": dcbias, "mna": mna, "loop": loop, "tf": tf, "step": step, "bw": bw}
    for name in args.only.split(","):
        if name in areas:
            areas[name]() if name != "mna" else areas[name](args.lcapy)
    bad = [r for r in ROWS if not r["ok"]]
    print(f"\n{len(ROWS)} comparisons, {len(bad)} FAIL")
    if args.json:
        args.json.write_text(json.dumps(ROWS, indent=1, default=str), encoding="utf-8")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
