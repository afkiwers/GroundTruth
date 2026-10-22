"""Frequency responses for Bode and pole-zero plots (the plan's M4): a complex response H(f) of a signal path, a
power stage's filters or a PDN, with its poles and zeros.

A response computed from a rational model (a closed form in s) has exact poles and zeros (`exact_rational`: the
roots of its numerator and denominator). A response that is only known at frequencies (a field solve, a path with
transmission lines) is fitted with a rational function by **vector fitting** [Gustavsen 1999], with the relaxation
of [Gustavsen 2006] (source keys: docs/REFERENCES.md):

    H(s) ≈ Σ_n r_n / (s − a_n) + d + s·e

Starting from complex pole pairs spread logarithmically over the band, each iteration fits σ(s)·H(s) ≈ (σH)_fit(s)
with σ(s) = Σ c̃_n/(s − a_n) + d̃ on the same poles (linear least squares on the samples, each weighted by 1/|H|:
a relative fit, so a response spanning decades is fitted at its low values too); the zeros of σ are the new poles
(the eigenvalues of A − b·c̃ᵀ/d̃ in a real state-space realisation), unstable ones mirrored into the left half
plane. With the poles settled, the residues, d and e are fitted once more. The zeros of the fit are the finite
generalised eigenvalues of the Rosenbrock pencil [Rosenbrock 1970] [[A, b], [c, d]] − s·[[I, 0], [0, −e]]. Poles whose
share of the response is negligible are pruned. The order is raised (2, 4, … MAX_ORDER) until the relative RMS error is
below FIT_TOL. Validated against known rational functions in tests/test_transfer.py.

A pole or zero s = σ + jω is reported by its natural frequency f0 = |s|/2π and its quality factor Q = |s|/(−2σ)
(a real one: its corner frequency, no Q).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

MAX_ORDER = 24                 # the highest number of poles tried
FIT_TOL = 0.01                 # relative RMS error the order search stops at
ITERATIONS = 12                # pole relocations per order
PRUNE = 1e-4                   # a pole contributing less than this share of max|H| anywhere in the band is dropped


@dataclass
class Response:
    """One complex frequency response with its poles and zeros. quantity: "voltage gain", "current ratio",
    "impedance" (Ω) or "loop gain"; f in Hz, h complex; poles / zeros in rad/s (complex); exact: from a rational
    model, else fitted with `fit_error` (the largest error over the band: dB of magnitude)."""
    name: str
    owner: str
    quantity: str
    f: np.ndarray
    h: np.ndarray
    poles: list = field(default_factory=list)
    zeros: list = field(default_factory=list)
    exact: bool = False
    fit_error_db: Optional[float] = None
    notes: list = field(default_factory=list)
    # the transfer function written out: `normal` the numeric normal form (normal_form: k, m, poles, zeros, text,
    # latex), `symbolic` the ideal circuit's H(s) in its part names (analysis/symbolic.py: text, latex, mathml)
    normal: Optional[dict] = None
    symbolic: Optional[dict] = None
    margin: Optional[dict] = None          # a loop gain's crossover and phase margin (phase_margin)
    step: Optional[dict] = None            # the step response: {"t", "y" (arrays), "metrics" (step_metrics)}

    @property
    def unit(self) -> str:
        """The magnitude's unit for axes and tables: Ω for an impedance, else a ratio (dB)."""
        return "Ω" if self.quantity == "impedance" else "dB"

    def magnitude(self) -> np.ndarray:
        """|H| in its plotting unit: Ω for an impedance, else dB."""
        m = np.abs(self.h)
        return m if self.quantity == "impedance" else 20 * np.log10(np.maximum(m, 1e-30))

    def phase_deg(self) -> np.ndarray:
        """The phase, unwrapped along the frequency (degrees)."""
        return np.degrees(np.unwrap(np.angle(self.h)))

    def summary(self, points: int = 200) -> dict:
        """Plain data for the JSON report: the curve decimated to `points`, the poles and zeros as rows, a gain's
        figures (gain_figures: low-end gain, −3 dB frequency, peaking; None for an impedance or loop gain)."""
        k = np.unique(np.linspace(0, len(self.f) - 1, min(points, len(self.f))).astype(int)) if len(self.f) else []
        return {"name": self.name, "owner": self.owner, "quantity": self.quantity, "exact": self.exact,
                "fit_error_db": self.fit_error_db, "notes": list(self.notes), "gain": gain_figures(self),
                "phase_margin": getattr(self, "margin", None),
                "step": _plain_step(getattr(self, "step", None)),
                "transfer_function": {"numeric": _plain_normal(getattr(self, "normal", None)),
                                      "symbolic": {k: v for k, v in (getattr(self, "symbolic", None) or {}).items()
                                                   if k in ("text", "latex")} or None},
                "poles": pz_rows(self.poles), "zeros": pz_rows(self.zeros),
                "f": [float(self.f[i]) for i in k], "magnitude": [float(self.magnitude()[i]) for i in k],
                "phase_deg": [float(self.phase_deg()[i]) for i in k]}


def response_label(r, owner: str) -> str:
    """A response's short name in a view of `owner`'s: its name without the owner's prefix ("receiver / source"), a
    response of another part kept whole ("U301: output")."""
    return r.name[len(owner) + 2:] if r.name.startswith(owner + ": ") else r.name


def _plain_step(st: Optional[dict], points: int = 400) -> Optional[dict]:
    """A step response as JSON data: its figures and the curve decimated to `points`."""
    if not st:
        return None
    t, y = np.asarray(st["t"]), np.asarray(st["y"])
    k = np.unique(np.linspace(0, len(t) - 1, min(points, len(t))).astype(int))
    return {"metrics": st["metrics"], "t": [float(t[i]) for i in k], "y": [float(y[i]) for i in k]}


def _plain_normal(nf: Optional[dict]) -> Optional[dict]:
    """A normal form as JSON data (its K as a number or [re, im])."""
    if not nf:
        return None
    k = nf["k"]
    return {"k": [k.real, k.imag] if isinstance(k, complex) else k, "m": nf["m"], "poles": nf["poles"],
            "zeros": nf["zeros"], "text": nf["text"], "latex": nf["latex"]}


def pz_rows(roots) -> list[dict]:
    """Poles or zeros as rows {f0 (Hz), q (None for a real one), s (re, im in rad/s)}: a conjugate pair once (its
    upper member), sorted by f0; f0 = |s|/2π and Q = |s|/(−2σ), the natural frequency and quality factor of the
    pair's second-order factor [Ogata 2010]."""
    rows = []
    for s in roots:
        s = complex(s)
        if s.imag < -1e-9 * max(abs(s), 1.0):
            continue                                   # the lower member of a pair
        w = abs(s)
        real = abs(s.imag) <= 1e-9 * max(w, 1.0)
        q = None if real or s.real == 0 else w / (-2 * s.real)
        rows.append({"f0": w / (2 * math.pi), "q": q, "re": s.real, "im": s.imag})
    return sorted(rows, key=lambda r: r["f0"])


NORMAL_ROOTS = 8               # poles (and zeros) written out in the normal form; the rest summed up in a note


def normal_form(poles, zeros, k: complex, f_max: Optional[float] = None) -> dict:
    """The numeric transfer function in the engineering normal form [Franco 2015]:

        H(s) = K · s^m · Π(1 + s/ω_z) · Π(1 + s/(Q_z·ω_z) + s²/ω_z²) / [Π(1 + s/ω_p) · Π(1 + s/(Q_p·ω_p) + s²/ω_p²)]

    from exact poles and zeros (rad/s, a pair once by its upper member) and the constant k of H = k·s^m·Π(1 − s/z)/
    Π(1 − s/p) (emi/mna.Circuit.poles_zeros): real roots as first-order factors, conjugate pairs as second-order
    ones with f0 and Q; m the zeros at the origin less the poles there. Roots above f_max (Hz: ten times the sweep's
    top, say) are left out of the factors (counted in `beyond`); at most NORMAL_ROOTS of each kind are written.
    {"k", "m", "poles": [{"f", "q"}], "zeros": [...], "beyond", "text", "latex"}."""
    def rows(roots):
        """The roots as factor rows (f Hz, Q or None, its sign: a right-half-plane root), lowest first."""
        out = []
        for r in pz_rows([x for x in roots if x != 0]):
            # a right-half-plane root's factor carries the minus sign: its Q is shown as |Q|
            out.append({"f": r["f0"], "q": abs(r["q"]) if r["q"] is not None else None, "rhp": r["re"] > 0})
        return out
    m = int(sum(1 for z in zeros if z == 0) - sum(1 for p in poles if p == 0))
    ps, zs = rows(poles), rows(zeros)
    beyond = 0
    if f_max:
        beyond = sum(1 for r in ps + zs if r["f"] > f_max)
        ps, zs = [r for r in ps if r["f"] <= f_max], [r for r in zs if r["f"] <= f_max]
    kk = complex(k)
    kval = kk.real if abs(kk.imag) <= 1e-6 * max(abs(kk), 1e-300) else kk     # numerical residue: real

    def factor(r, i, kind):
        """One factor's text and LaTeX, its frequency named ω_{kind}{i}."""
        name = f"ω{kind}{i}"
        lname = rf"\omega_{{{kind}{i}}}"
        sign = "−" if r["rhp"] else "+"
        lsign = "-" if r["rhp"] else "+"
        if r["q"] is None:
            return f"(1 {sign} s/{name})", rf"\left(1 {lsign} \frac{{s}}{{{lname}}}\right)"
        return (f"(1 {sign} s/(Q{kind}{i}·{name}) + s²/{name}²)",
                rf"\left(1 {lsign} \frac{{s}}{{Q_{{{kind}{i}}} {lname}}} + \frac{{s^2}}{{{lname}^2}}\right)")
    num = [factor(r, i + 1, "z") for i, r in enumerate(zs[:NORMAL_ROOTS])]
    den = [factor(r, i + 1, "p") for i, r in enumerate(ps[:NORMAL_ROOTS])]
    sm = "" if m == 0 else ("s" if m == 1 else f"s^{m}" if m > 0 else "")
    ktext = f"{kval:.4g}" if not isinstance(kval, complex) else f"{kval:.3g}"
    head = " · ".join(x for x in [ktext, sm] + [t for t, _ in num] if x)
    tail = "".join(t for t, _ in den) + ("" if m >= 0 else ("·s" if m == -1 else f"·s^{-m}"))
    text = "H(s) = " + head + (f" / [{tail}]" if tail else "")
    lhead = " ".join(x for x in [ktext.replace("e", r"\times 10^{") + ("}" if "e" in ktext else ""),
                                 "s" if m == 1 else (f"s^{{{m}}}" if m > 1 else "")] + [l for _, l in num] if x)
    ltail = " ".join(l for _, l in den) + ("" if m >= 0 else (" s" if m == -1 else f" s^{{{-m}}}"))
    latex = "H(s) = " + (rf"\frac{{{lhead}}}{{{ltail}}}" if ltail else lhead)
    values = [f"f{kind}{i + 1} = {fmt_hz(r['f'])}" + (f", Q{kind}{i + 1} = {r['q']:.3g}" if r["q"] else "")
              + (" (RHP)" if r["rhp"] else "")
              for kind, group in (("p", ps), ("z", zs)) for i, r in enumerate(group[:NORMAL_ROOTS])]
    more = (len(ps) - NORMAL_ROOTS if len(ps) > NORMAL_ROOTS else 0) + (len(zs) - NORMAL_ROOTS
                                                                       if len(zs) > NORMAL_ROOTS else 0) + beyond
    if values:
        text += ";  " + ", ".join(values)
    if more:
        text += f";  {more} more above"
    return {"k": kval, "m": m, "poles": ps, "zeros": zs, "beyond": more, "text": text, "latex": latex}


SETTLE_BAND = 0.02             # the step response's settling band: ±2 % of its final value [Ogata 2010]
STEP_SPAN = 12.0               # a step is simulated for this many of its slowest pole's time constants 1/|σ|
STEP_POINTS = 3000             # time steps of a step response (BDF2: its error ~ (h/τ)², ~1e-5 here), at least
STEP_PER_PERIOD = 100          # ... this many per period of its fastest lightly damped pole (Q ≥ STEP_Q) [Gear 1971]
STEP_Q = 1.0                   # (audit round 7, AA-6)
STEP_MAX = 200_000             # and at most this many (a note says the ringing is then under-resolved)


def step_metrics(t, y, final: Optional[float] = None) -> dict:
    """A step response's figures [Ogata 2010]: its final value (the DC gain `final` where it is known, else the last
    sample), the 10-90 % rise time, the overshoot and the undershoot (the first move the wrong way: a right-half-plane
    zero) in % of the final value, the peak and its time, and the settling time into ±SETTLE_BAND of the final value
    (None when it has not settled within the window). Crossing times interpolated linearly between samples. A
    response without a final value (|final| below 1e-6 of its peak: AC-coupled, a high-pass) gives its peak only."""
    t = np.asarray(t, float)
    y = np.asarray(y, float)
    fin = float(final) if final is not None else float(y[-1])
    k = int(np.argmax(np.abs(y)))
    out = {"final": fin, "peak": float(y[k]), "t_peak": float(t[k]), "rise": None, "overshoot_pct": None,
           "undershoot_pct": None, "settling": None}
    if abs(fin) <= 1e-6 * max(float(np.max(np.abs(y))), 1e-300):
        return out
    u = y / fin                                    # normalised: rises from 0 to 1
    out["overshoot_pct"] = max(float(np.max(u)) - 1.0, 0.0) * 100

    def cross(level):
        """The first time u reaches `level`, interpolated (None when it never does)."""
        idx = np.nonzero(u >= level)[0]
        if not len(idx):
            return None
        i = int(idx[0])
        if i == 0:
            return float(t[0])
        return float(t[i - 1] + (level - u[i - 1]) / (u[i] - u[i - 1]) * (t[i] - t[i - 1]))
    t10, t90 = cross(0.1), cross(0.9)
    out["rise"] = t90 - t10 if t10 is not None and t90 is not None else None
    first = np.nonzero(np.abs(u) > 0.01)[0]
    if len(first) and u[first[0]] < 0:
        out["undershoot_pct"] = float(-np.min(u[: max(int(np.argmax(u > 0)), 1)])) * 100
    outside = np.nonzero(np.abs(u - 1.0) > SETTLE_BAND)[0]
    if not len(outside):
        out["settling"] = float(t[0])
    elif outside[-1] < len(t) - 1:
        i = int(outside[-1])
        a, b_ = abs(u[i] - 1.0), abs(u[i + 1] - 1.0)
        out["settling"] = float(t[i] + (a - SETTLE_BAND) / (a - b_) * (t[i + 1] - t[i])) if a != b_ else float(t[i + 1])
    return out


def fmt_s(x: Optional[float]) -> str:
    """A time with an SI prefix and three significant digits: '2.2 ms', '35.1 µs'; '—' for None."""
    if x is None:
        return "—"
    for unit, k in (("s", 1.0), ("ms", 1e-3), ("µs", 1e-6), ("ns", 1e-9)):
        if abs(x) >= k * 0.9995:
            return f"{x / k:.3g} {unit}"
    return f"{x / 1e-12:.3g} ps"


def step_text(m: dict) -> str:
    """step_metrics as one line: 'final 0.998, rise 1.62 ms, overshoot 4.3 %, settles (±2 %) in 2.91 ms'."""
    if m.get("unstable"):
        return f"unstable (a pole in the right half-plane): it grows without bound, {m['peak']:.3g} at " \
               f"{fmt_s(m['t_peak'])}"
    if m.get("rise") is None and m.get("overshoot_pct") is None:
        return f"no final value (AC-coupled): peak {m['peak']:.3g} at {fmt_s(m['t_peak'])}"
    parts = [f"final {m['final']:.4g}", f"rise {fmt_s(m['rise'])}", f"overshoot {m['overshoot_pct']:.1f} %"]
    if m.get("undershoot_pct"):
        parts.append(f"undershoot {m['undershoot_pct']:.1f} %")
    parts.append(f"settles (±{SETTLE_BAND:.0%}) in {fmt_s(m['settling'])}" if m["settling"] is not None
                 else "not settled in the window")
    return ", ".join(parts)


BW_TOL = 0.01                  # a bandwidth this much above its limit is a bandwidth_limit issue (numerics: 1 %) [A55]


def upper_3db(f, h) -> Optional[float]:
    """A response's bandwidth [Franco 2015]: the first frequency above its peak (|H|'s largest value in the sweep)
    where |H| has fallen 3 dB below its passband gain, (|H_ref|/|H|)² interpolated linearly in f² to 2 between the
    samples around it (exact for one pole, as gain_figures). The passband gain is the gain at the sweep's start when
    the response starts in its passband (flat there: within FLAT_DB over the sweep's first octave; a low-pass, with
    or without peaking: its usual bandwidth below its DC gain, as the −3 dB tile, gain_figures), else the peak (a
    band-pass or a high-pass, still rising at the sweep's start). Audit round 7, AA-5:
    measured from the peak, a low-pass with Q 2 read 22 % low (1.16 kHz for 1.48 kHz), and a peaked filter above its
    max_bandwidth passed the check. None when it does not fall that far above the peak within the sweep (a
    high-pass, or a band reaching past the sweep's top)."""
    f = np.asarray(f, float)
    a = np.abs(np.asarray(h, complex))
    if len(f) < 2:
        return None
    k0 = int(np.argmax(a))
    peak = float(a[k0])
    if peak <= 0:
        return None
    j = min(int(np.searchsorted(f, 2 * f[0])), len(f) - 1)          # one octave above the sweep's start
    flat = a[0] > 0 and abs(20 * math.log10(max(a[j], 1e-300) / a[0])) <= FLAT_DB
    ref = float(a[0]) if flat else peak
    below = np.nonzero(a[k0:] < ref / math.sqrt(2))[0]
    if not len(below):
        return None
    k = k0 + int(below[0])
    y0, y1 = (ref / max(a[k - 1], 1e-300)) ** 2, (ref / max(a[k], 1e-300)) ** 2
    u0, u1 = float(f[k - 1]) ** 2, float(f[k]) ** 2
    t = (2.0 - y0) / (y1 - y0) if y1 != y0 else 0.0
    return math.sqrt(u0 + min(max(t, 0.0), 1.0) * (u1 - u0))


PM_WARN = 45.0                 # degrees: a loop with less phase margin rings (a warning); below PM_ERROR it is close
PM_ERROR = 20.0                # to oscillating (an error) [A55]


def phase_margin(f, t, lead=None) -> Optional[dict]:
    """A loop gain's stability margin [Ogata 2010] [Nyquist 1932]: at every frequency where |T| crosses 1
    (interpolated in log f, |T| in dB linear between the samples) 180° + ∠T; the margin reported is the smallest of
    them, at its crossover (a resonance in the feedback can lift |T| through 1 again above the first crossover: audit
    round 7, AA-4, the first alone read 98° for a loop whose margin is 27°).

    `lead`: T on a grid from near DC up to the sweep's start (simulate.network_responses), so ∠T is unwrapped from DC
    (two poles below the sweep's start leave ∠T past −180° there: unwrapped from the sweep's start alone it read
    +180°, a margin 360° too high) and the sign at DC is known: a loop gain negative at DC with |T| > 1 is positive
    feedback, T(0) beyond −1 on the Nyquist plot, a closed-loop pole in the right half-plane (the stage latches): pm
    −180, "positive_feedback" (AA-3: it was given 270°). Without `lead` the sweep's start is taken as DC.
    {"f_c", "pm", "t_low_db", "crossings": [(f, margin)], "positive_feedback"}; None when |T| starts at or below 1
    (no loop to speak of) or never crosses 1 in the sweep."""
    f = np.asarray(f, float)
    t = np.asarray(t, complex)
    mag = 20 * np.log10(np.maximum(np.abs(t), 1e-300))
    if len(f) < 2 or mag[0] <= 0:
        return None
    lead = np.asarray(lead if lead is not None else [], complex)
    ph = np.degrees(np.unwrap(np.angle(np.concatenate([lead, t]))))[len(lead):]
    positive = bool((lead[0] if len(lead) else t[0]).real < 0)
    if not len(lead) and positive and abs(ph[0]) < 135:
        positive = False                 # past −90° at the sweep's start without the DC side known: a pole below it
    crossings = []
    for k in np.nonzero(np.sign(mag[1:]) != np.sign(mag[:-1]))[0] + 1:
        x0, x1 = math.log(f[k - 1]), math.log(f[k])
        u = mag[k - 1] / (mag[k - 1] - mag[k])
        phase = ph[k - 1] + u * (ph[k] - ph[k - 1])
        crossings.append((math.exp(x0 + u * (x1 - x0)), 180.0 + phase))
    if positive:
        f_c = crossings[0][0] if crossings else float(f[0])
        return {"f_c": f_c, "pm": -180.0, "t_low_db": float(mag[0]), "crossings": crossings,
                "positive_feedback": True}
    if not crossings:
        return None
    f_c, pm = min(crossings, key=lambda c: c[1])
    return {"f_c": f_c, "pm": pm, "t_low_db": float(mag[0]), "crossings": crossings, "positive_feedback": False}


PEAKING_DB = 0.5               # a gain this far above its low-frequency value is reported as peaking
FLAT_DB = 0.5                  # a response whose gain moves less than this over its sweep's first octave starts in
                               # its passband (upper_3db measures from that gain, not the peak)


def gain_figures(r) -> Optional[dict]:
    """A gain's (a voltage gain's or current ratio's) figures from its sweep: the gain at its lowest frequency (dB and
    as a ratio: the DC gain when the sweep starts low enough), the first frequency where it has fallen 3 dB below that
    (interpolated in log f between the two samples around it; None when it never does in the sweep), and its highest
    value above the low one by PEAKING_DB or more (a resonance: dB, Hz). None for an impedance or a loop gain, or a
    sweep of fewer than two points. The −3 dB point lies between two samples: (|H_low| / |H|)² is interpolated
    linearly in f² to 2 (half the power), exact for a single pole, 1 + (f/f_c)² [Franco 2015]."""
    if r.quantity not in ("voltage gain", "current ratio") or len(r.f) < 2:
        return None
    m = r.magnitude()
    ref = float(m[0])
    out = {"low_db": ref, "low_ratio": float(abs(r.h[0])), "low_f": float(r.f[0]), "f_3db": None,
           "peak_db": None, "peak_f": None}
    below = np.nonzero(m < ref - 3.0103)[0]          # 10·log10(2): half the power
    if len(below) and below[0] > 0:
        k = int(below[0])
        a0, a1 = float(abs(r.h[k - 1])), float(abs(r.h[k]))
        y0, y1 = (out["low_ratio"] / a0) ** 2, (out["low_ratio"] / a1) ** 2
        u0, u1 = float(r.f[k - 1]) ** 2, float(r.f[k]) ** 2
        t = (2.0 - y0) / (y1 - y0) if y1 != y0 else 0.0
        out["f_3db"] = math.sqrt(u0 + min(max(t, 0.0), 1.0) * (u1 - u0))
    k = int(np.argmax(m))
    if m[k] >= ref + PEAKING_DB:
        out["peak_db"], out["peak_f"] = float(m[k] - ref), float(r.f[k])
    return out


def gain_text(g: dict) -> str:
    """gain_figures as one line: '−0.01 dB (0.999×) at 1 Hz, −3 dB at 15.5 kHz, peaking +2.1 dB at 1.2 MHz'."""
    text = f"{g['low_db']:+.2f} dB ({g['low_ratio']:.3g}×) at {fmt_hz(g['low_f'])}"
    text += f", −3 dB at {fmt_hz(g['f_3db'])}" if g["f_3db"] else ", no −3 dB point in the sweep"
    if g["peak_db"]:
        text += f", peaking +{g['peak_db']:.1f} dB at {fmt_hz(g['peak_f'])}"
    return text


def fmt_hz(f: float) -> str:
    """A natural frequency with three significant digits: '2.74 MHz', '401 MHz', '3.07 GHz'."""
    for unit, k in (("GHz", 1e9), ("MHz", 1e6), ("kHz", 1e3)):
        if f >= k * 0.9995:
            return f"{f / k:.3g} {unit}"
    return f"{f:.3g} Hz"


def fmt_root(row: dict) -> str:
    """A pole or zero row as text: '2.74 MHz' (real), '401 MHz Q 740', a right-half-plane one marked 'RHP'."""
    text = fmt_hz(row["f0"])
    if row["re"] > 0:
        return text + " RHP"
    return text + (f" Q {row['q']:.3g}" if row["q"] else "")


def cancel_pairs(poles: list, zeros: list, rel: float = 0.005) -> tuple[list, list]:
    """Poles and zeros without the pairs that coincide within rel of each other (they cancel in the response: e.g.
    each of several output capacitors with its ESR adds a pole and a zero next to each other)."""
    zeros = list(zeros)
    keep = []
    for p in poles:
        k = next((i for i, z in enumerate(zeros) if abs(z - p) <= rel * max(abs(p), 1e-30)), None)
        if k is None:
            keep.append(p)
        else:
            zeros.pop(k)
    return keep, zeros


def exact_rational(num, den) -> tuple[list, list]:
    """(poles, zeros) of N(s)/D(s), coefficient arrays highest power first (numpy.poly1d order)."""
    return list(np.roots(np.atleast_1d(den))), list(np.roots(np.atleast_1d(num)))


def rational_eval(num, den, f) -> np.ndarray:
    """N(s)/D(s) at s = j2πf."""
    s = 2j * np.pi * np.asarray(f, float)
    return np.polyval(num, s) / np.polyval(den, s)


# ------------------------------------------------------------------------------------------------ vector fitting
def _basis(s: np.ndarray, poles: np.ndarray) -> np.ndarray:
    """The real basis for the poles at the samples (K × N, complex): a real pole a gives 1/(s − a); a complex pair
    (a, a*) gives 1/(s − a) + 1/(s − a*) and j/(s − a) − j/(s − a*), so real coefficients make a real response."""
    cols = []
    k = 0
    while k < len(poles):
        a = poles[k]
        if abs(a.imag) > 0:
            cols.append(1 / (s - a) + 1 / (s - np.conj(a)))
            cols.append(1j / (s - a) - 1j / (s - np.conj(a)))
            k += 2
        else:
            cols.append(1 / (s - a.real))
            k += 1
    return np.array(cols).T if cols else np.zeros((len(s), 0), complex)


def _realisation(poles: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """(A, b) of a real state-space realisation matching _basis: a real pole a → [a], b 1; a pair a' ± ja'' →
    [[a', a''], [−a'', a']], b [2, 0] (c·(sI − A)⁻¹·b then equals the basis combination with coefficients c)."""
    n = len(poles)
    A = np.zeros((n, n))
    b = np.zeros(n)
    k = 0
    while k < n:
        a = poles[k]
        if abs(a.imag) > 0:
            A[k:k + 2, k:k + 2] = [[a.real, a.imag], [-a.imag, a.real]]
            b[k:k + 2] = [2.0, 0.0]
            k += 2
        else:
            A[k, k] = a.real
            b[k] = 1.0
            k += 1
    return A, b


def _pairs(poles) -> np.ndarray:
    """The poles ordered as _basis expects: real ones, then each complex pair as (upper, lower); stable
    (an unstable one mirrored)."""
    out = []
    for a in poles:
        a = complex(a)
        a = complex(-abs(a.real) if a.real != 0 else -1e-6 * abs(a), a.imag)
        if abs(a.imag) <= 1e-12 * max(abs(a), 1.0):
            out.append(complex(a.real, 0.0))
        elif a.imag > 0:
            out += [a, a.conjugate()]
    return np.array(out, complex)


def _lstsq(M: np.ndarray, rhs: np.ndarray) -> np.ndarray:
    """Least squares on real rows with columns scaled to unit norm (the basis columns span decades)."""
    scale = np.linalg.norm(M, axis=0)
    scale[scale == 0] = 1.0
    x, *_ = np.linalg.lstsq(M / scale, rhs, rcond=None)
    return x / scale


def _stack(rows: np.ndarray) -> np.ndarray:
    """Complex equations as real rows (real parts, then imaginary parts)."""
    return np.vstack([rows.real, rows.imag])


def _relocate(s, h, w, poles, use_d, use_e) -> np.ndarray:
    """One pole relocation with relaxation: the zeros of σ(s) (see the module)."""
    phi = _basis(s, poles)
    n = phi.shape[1]
    cols = [phi] + ([np.ones((len(s), 1))] if use_d else []) + ([s[:, None]] if use_e else [])
    left = np.hstack(cols)
    right = -h[:, None] * np.hstack([phi, np.ones((len(s), 1))])
    M = _stack(w[:, None] * np.hstack([left, right]))
    # relaxation: Re Σ_k σ(s_k) = K, weighted as the data (keeps σ from collapsing to zero)
    K = len(s)
    extra = np.zeros(M.shape[1])
    extra[left.shape[1]:left.shape[1] + n] = np.real(phi).sum(axis=0)
    extra[-1] = K
    weight = np.linalg.norm(w * h) / K
    M = np.vstack([M, weight * extra])
    rhs = np.zeros(M.shape[0])
    rhs[-1] = weight * K
    x = _lstsq(M, rhs)
    ct, dt = x[left.shape[1]:left.shape[1] + n], x[-1]
    if abs(dt) < 1e-8:
        dt = 1e-8 if dt >= 0 else -1e-8
    A, b = _realisation(poles)
    return _pairs(np.linalg.eigvals(A - np.outer(b, ct) / dt))


def _residues(s, h, w, poles, use_d, use_e) -> tuple[np.ndarray, float, float]:
    """(c in the real basis, d, e) for fixed poles (weighted least squares)."""
    phi = _basis(s, poles)
    cols = [phi] + ([np.ones((len(s), 1))] if use_d else []) + ([s[:, None]] if use_e else [])
    M = _stack(w[:, None] * np.hstack(cols))
    x = _lstsq(M, _stack((w * h)[:, None]).ravel())
    n = phi.shape[1]
    d = x[n] if use_d else 0.0
    e = x[n + (1 if use_d else 0)] if use_e else 0.0
    return x[:n], float(d), float(e)


def _evaluate(s, poles, c, d, e) -> np.ndarray:
    """The fitted rational at s."""
    return _basis(s, poles) @ c + d + s * e


def _zeros(poles, c, d, e) -> list:
    """The finite zeros of the fit: generalised eigenvalues of the Rosenbrock pencil (see the module)."""
    from scipy.linalg import eig
    A, b = _realisation(poles)
    n = len(poles)
    M = np.zeros((n + 1, n + 1))
    M[:n, :n], M[:n, n], M[n, :n], M[n, n] = A, b, c, d
    N = np.zeros((n + 1, n + 1))
    N[:n, :n] = np.eye(n)
    N[n, n] = -e
    vals = eig(M, N, right=False)
    return [complex(v) for v in vals if np.isfinite(v) and abs(v) < 1e30]


@dataclass
class Fit:
    """A vector fit: poles, zeros (rad/s), the real-basis residues c, d, e, and its errors."""
    poles: np.ndarray
    zeros: list
    c: np.ndarray
    d: float
    e: float
    rms: float              # relative RMS error
    max_db: float           # the largest magnitude error (dB)

    def __call__(self, f) -> np.ndarray:
        """The fitted response at frequencies f (Hz)."""
        return _evaluate(2j * np.pi * np.asarray(f, float), self.poles, self.c, self.d, self.e)


def vector_fit(f, h, order: Optional[int] = None, use_d: bool = True, use_e: bool = False,
               max_order: int = MAX_ORDER, tol: float = FIT_TOL) -> Fit:
    """A rational fit of samples h at frequencies f (Hz, > 0): the given order, else the lowest even order from 2
    up to max_order with a relative RMS error under tol (the best one tried when none is). See the module."""
    f = np.asarray(f, float)
    h = np.asarray(h, complex)
    keep = (f > 0) & np.isfinite(h)
    f, h = f[keep], h[keep]
    if len(f) < 4:
        raise ValueError("too few frequencies to fit")
    s = 2j * np.pi * f
    w = 1.0 / np.maximum(np.abs(h), 1e-30 * max(np.max(np.abs(h)), 1e-30))
    best = None
    orders = [order] if order else list(range(2, max_order + 1, 2))
    for n in orders:
        n = min(n, 2 * (len(f) // 3))           # no more poles than the samples can pin down
        if n < 1:
            break
        beta = 2 * np.pi * np.geomspace(f.min(), f.max(), max(n // 2, 1))
        poles = _pairs([complex(-b / 100, b) for b in beta]) if n >= 2 else np.array([complex(-2 * np.pi * f.min())])
        for _ in range(ITERATIONS):
            try:
                poles = _relocate(s, h, w, poles, use_d, use_e)
            except np.linalg.LinAlgError:
                break
        fit = _finish(s, h, w, poles, use_d, use_e)
        if best is None or fit.rms < best.rms:
            best = fit
        if fit.rms < tol:
            break
    return best


def _finish(s, h, w, poles, use_d, use_e) -> Fit:
    """The residues on settled poles, insignificant poles pruned (refitted when the fit stays as good), errors and
    zeros."""
    c, d, e = _residues(s, h, w, poles, use_d, use_e)
    fit = _fit_of(s, h, poles, c, d, e)
    peak = float(np.max(np.abs(h)))
    # a pole's contribution: its basis column(s) times its coefficient(s), at its largest over the band
    phi = _basis(s, poles)
    keep, k, col = [], 0, 0
    while k < len(poles):
        width = 2 if abs(poles[k].imag) > 0 else 1
        part = np.max(np.abs(phi[:, col:col + width] @ c[col:col + width]))
        keep += [part >= PRUNE * peak] * width
        k += width
        col += width
    if not all(keep) and any(keep):
        pruned = poles[np.array(keep)]
        c2, d2, e2 = _residues(s, h, w, pruned, use_d, use_e)
        fit2 = _fit_of(s, h, pruned, c2, d2, e2)
        if fit2.rms <= max(fit.rms * 1.5, fit.rms + 1e-4):
            fit = fit2
    return fit


def _fit_of(s, h, poles, c, d, e) -> Fit:
    """A Fit with its errors and zeros."""
    model = _evaluate(s, poles, c, d, e)
    rel = np.abs(model - h) / np.maximum(np.abs(h), 1e-30)
    rms = float(np.sqrt(np.mean(rel ** 2)))
    max_db = float(np.max(np.abs(20 * np.log10(np.maximum(np.abs(model), 1e-30) / np.maximum(np.abs(h), 1e-30)))))
    return Fit(poles, _zeros(poles, c, d, e), c, d, e, rms, max_db)


def fitted_response(name: str, owner: str, quantity: str, f, h, use_e: bool = False,
                    max_order: int = MAX_ORDER) -> Response:
    """A Response with its poles and zeros by vector fitting (a note when the fit stays coarse; poles and zeros
    beyond ten times the band are left out of the lists: a transmission line's delay is not rational, the fit puts
    poles above the band for it)."""
    f = np.asarray(f, float)
    h = np.asarray(h, complex)
    r = Response(name, owner, quantity, f, h)
    try:
        fit = vector_fit(f, h, use_e=use_e, max_order=max_order)
    except (ValueError, np.linalg.LinAlgError) as e:
        r.notes.append(f"no pole-zero fit: {e}")
        return r
    band = 2 * np.pi * 10 * f.max()
    r.poles = [p for p in fit.poles if abs(p) <= band]
    r.zeros = [z for z in fit.zeros if abs(z) <= band]
    r.fit_error_db = fit.max_db
    if len(r.poles) < len(fit.poles) or len(r.zeros) < len(fit.zeros):
        r.notes.append("poles or zeros above ten times the band left out (a delay, or the fit's own)")
    if fit.rms > FIT_TOL:
        r.notes.append(f"the rational fit is coarse ({fit.rms * 100:.1f} % RMS): poles and zeros are approximate")
    return r


@dataclass
class BodeView:
    """A Results-tab view of one owner's responses (a signal path, a power stage, a PDN rail): curves, not maps
    (ui/results_panels.py draws plots.draw_bode and draw_pz for it)."""
    owner: str
    responses: list
    kind: str = "bode"
    error: str = ""
    notes: list = field(default_factory=list)
    layer_share: dict = field(default_factory=dict)

    @property
    def net(self) -> str:
        """The owner (the attribute the window's lists read)."""
        return self.owner

    def label(self) -> str:
        """Its name in the view list."""
        return f"{self.owner}  Bode / poles and zeros"


def unit_groups(owner: str, responses) -> list:
    """An owner's responses as [(name, responses)] that share a magnitude axis: the ratios (dB) under the owner's
    name, the impedances (Ω) under it too when they are all there is, else as "<owner> impedances" (a power stage's
    proposed input filter beside its loop gain: Middlebrook's two impedances)."""
    ratios = [r for r in responses if r.quantity != "impedance"]
    ohms = [r for r in responses if r.quantity == "impedance"]
    if not ratios or not ohms:
        return [(owner, list(responses))] if responses else []
    return [(owner, ratios), (f"{owner} impedances", ohms)]


def bode_views(res) -> list:
    """One BodeView per signal path, power stage (not a bridge's leg) and PDN rail of a Simulate with responses (two
    when its responses mix ratios and impedances: unit_groups)."""
    owners = list(getattr(res, "routes", ())) + [c for c in getattr(res, "converters", ())
                                                 if not getattr(c, "bridge", None)]
    views = [BodeView(name, group) for r in owners if getattr(r, "responses", None) and not r.error
             for name, group in unit_groups(r.net, r.responses)]
    views += [BodeView(name, group) for p in getattr(res, "pdn", ()) if p.responses
              for name, group in unit_groups(f"{p.name} PDN", p.responses)]
    for v in views:
        v.notes = [f"{r.name.split(': ', 1)[-1]}: {n}" for r in v.responses for n in r.notes]
    return views


def exact_response(name: str, owner: str, quantity: str, f, num, den) -> Response:
    """A Response of a rational model N(s)/D(s): its curve at f and its exact poles and zeros."""
    poles, zeros = exact_rational(num, den)
    return Response(name, owner, quantity, np.asarray(f, float), rational_eval(num, den, f), poles, zeros, exact=True)
