"""A ceramic capacitor's capacitance against the DC voltage across it (its DC-bias curve).

Class II ceramics (X7R, X5R, X6S, Y5V ...) lose capacitance as the DC voltage across them rises: the barium-titanate
dielectric's permittivity falls with the field [Johnson 1962]. A part's `Cbias` gives that curve as one or more
points, written as the schematic field or the settings' C_bias:

    75nF                      one value, no voltage (GroundTruth takes it at half the Rated Voltage, [A57])
    75nF@50V                  one point: the curve C0 / (1 + (V/V1)^2) through it [A56]
    95nF@10V, 66nF@30V, 39nF@50V, 20nF@80V
                              several: interpolated (monotone cubic) from the nominal C0 at 0 V through them, the
                              one-point curve through the last one above it
    -5%@10V, -34%@30V ...     the same as a change from the nominal (read off a datasheet's ΔC curve)

Separators: commas, semicolons or spaces; a bare number is in µF (as the Cbias field always was), a bare voltage in
V. 1 to BIAS_POINTS_MAX points. KiCad-independent: analysis/bias.py finds each capacitor's DC voltage and applies
the curve per part for a run.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Optional

from .values import format_si, parse_quantity

BIAS_POINTS_MAX = 12        # more points add nothing a datasheet curve read by eye can give, and the field gets long
C_MAX_RATIO = 1.2           # a point at most this many times the nominal (some X7R curves rise ~1 % near 0 V)
PCT_RANGE = (-99.0, 20.0)   # a percent point's allowed change from the nominal


@dataclass(frozen=True)
class BiasPoint:
    """One point of a DC-bias curve: the capacitance (F) or its change from the nominal (%), at a voltage (V; None:
    not given, analysis/bias.py supplies one)."""
    c: Optional[float] = None
    pct: Optional[float] = None
    v: Optional[float] = None

    def capacitance(self, c0: float) -> float:
        """The point's capacitance with the nominal c0 (a percent point is relative to it)."""
        return c0 * (1 + self.pct / 100) if self.pct is not None else float(self.c)

    def text(self) -> str:
        """The point as the field writes it: '75nF@50V', '-50%@40V', '75nF'."""
        head = f"{self.pct:+.3g}%" if self.pct is not None else format_si(float(f"{self.c:.4g}"), "F")
        return head + (f"@{self.v:.4g}V" if self.v is not None else "")


def _token(tok: str) -> tuple[Optional[BiasPoint], str]:
    """One point from its text ('75nF@50V', '-5%@10V', '75n'), or (None, why)."""
    head, _, volt = tok.partition("@")
    v = None
    if volt:
        v, why = parse_quantity(volt, "V", 1.0)
        if v is None:
            return None, f"'{tok}': '{volt}' is not a voltage"
        if not v > 0:
            return None, f"'{tok}': the voltage must be above 0 V (0 V is the nominal C itself)"
    if head.endswith("%"):
        try:
            pct = float(head[:-1].replace(",", "."))
        except ValueError:
            return None, f"'{tok}': '{head}' is not a percentage"
        if not PCT_RANGE[0] <= pct <= PCT_RANGE[1]:
            return None, f"'{tok}': a change of {pct:g} % is outside {PCT_RANGE[0]:g} … +{PCT_RANGE[1]:g} %"
        return BiasPoint(pct=pct, v=v), ""
    c, why = parse_quantity(head, "F", 1e-6)
    if c is None:
        return None, f"'{tok}': '{head}' is not a capacitance"
    if not c > 0:
        return None, f"'{tok}': the capacitance must be above 0"
    return BiasPoint(c=c, v=v), ""


def parse_bias(text) -> tuple[list[BiasPoint], list[str]]:
    """A DC-bias curve from its text (see the module), or a plain number (F, as old settings files hold it):
    (points sorted by voltage, problems). An empty text gives no points and no problem."""
    if text is None:
        return [], []
    if isinstance(text, (int, float)):
        return ([BiasPoint(c=float(text))], []) if text > 0 else ([], ["the capacitance at bias must be above 0"])
    t = str(text).strip()
    if not t:
        return [], []
    # glue "75 nF @ 50 V" into one token, then split at commas, semicolons and the spaces left
    t = re.sub(r"\s*@\s*", "@", t)
    t = re.sub(r"(\d)\s+(?=[a-zA-Zµμ%])", r"\1", t)
    tokens = [x for x in re.split(r"[;,\s]+", t) if x]
    points, problems = [], []
    for tok in tokens:
        p, why = _token(tok)
        if p is None:
            problems.append(why)
        else:
            points.append(p)
    if len(points) > BIAS_POINTS_MAX:
        problems.append(f"{len(points)} points: at most {BIAS_POINTS_MAX}")
    if len(points) > 1 and any(p.v is None for p in points):
        problems.append("with more than one point, each needs its voltage (75nF@50V)")
    volts = [p.v for p in points if p.v is not None]
    if len(set(volts)) != len(volts):
        problems.append("two points at the same voltage")
    if problems:
        return [], problems
    return sorted(points, key=lambda p: p.v or 0.0), []


def bias_text(points: list[BiasPoint]) -> str:
    """The curve as the settings file and the Parts tab write it ('95nF@10V, 66nF@30V')."""
    return ", ".join(p.text() for p in points)


def check_points(points: list[BiasPoint], c0: float) -> list[str]:
    """Problems of a curve against its nominal c0: a point above C_MAX_RATIO × c0 (a typo: 750nF for 75nF)."""
    out = []
    for p in points:
        c = p.capacitance(c0)
        if c0 > 0 and c > C_MAX_RATIO * c0:
            out.append(f"{p.text()} is more than {C_MAX_RATIO:g} × the nominal {format_si(c0, 'F')}")
    return out


def v1_through(c0: float, c: float, v: float) -> Optional[float]:
    """V1 of the curve C0 / (1 + (V/V1)^2) [A56] through the point (v, c): V1 = v / sqrt(c0/c − 1); None when the
    point is at or above the nominal (no derating)."""
    if not (c0 > 0 and 0 < c < c0 and v > 0):
        return None
    return v / math.sqrt(c0 / c - 1)


def one_point(c0: float, v1: Optional[float], v: float) -> float:
    """The one-point curve's capacitance at v: C0 / (1 + (v/V1)^2) [A56] (low-field quadratic, as the permittivity's
    even dependence on the field [Johnson 1962]); c0 when V1 is None."""
    return c0 if v1 is None else c0 / (1 + (v / v1) ** 2)


def c_at(points: list[tuple[float, float]], c0: float, v: float) -> float:
    """The capacitance at |v| volts from a curve given as [(V, C)] (voltages > 0, sorted) and the nominal c0.

    No point: c0. One point: the curve C0 / (1 + (V/V1)^2) through it [A56]. Several: a monotone piecewise cubic
    (PCHIP, Fritsch-Carlson [Fritsch 1980]: it never overshoots between the points, so a falling datasheet curve
    stays falling) through (0, c0) and the points; above the last point the one-point curve through it (they meet
    there, so the curve is continuous)."""
    v = abs(float(v))
    if not points or c0 <= 0:
        return c0
    if len(points) == 1:
        (vb, cb), = points
        return one_point(c0, v1_through(c0, cb, vb), v)
    v_last, c_last = points[-1]
    if v >= v_last:
        v1 = v1_through(c0, c_last, v_last)
        # through the last point: C_last at v_last, falling as the one-point curve beyond it (flat when the last
        # point is at or above the nominal)
        return c_last if v1 is None else c_last * one_point(c0, v1, v) / one_point(c0, v1, v_last)
    from scipy.interpolate import PchipInterpolator
    xs = [0.0] + [p[0] for p in points]
    ys = [c0] + [p[1] for p in points]
    return float(PchipInterpolator(xs, ys)(v))


def resolve(points: list[BiasPoint], c0: float, v_default: Optional[float]) -> tuple[list[tuple[float, float]], str]:
    """The curve as [(V, C)] for c_at: percent points against c0; a single point without a voltage taken at
    v_default (analysis/bias.py: half the Rated Voltage [A57]), or, without one, as a flat value at every voltage
    (an empty curve then: the caller uses that value as it is). Returns (curve, how) with how one of 'curve',
    'default voltage', 'flat'."""
    if len(points) == 1 and points[0].v is None:
        c = points[0].capacitance(c0)
        if v_default and v_default > 0:
            return [(v_default, c)], "default voltage"
        return [], "flat"
    return [(p.v, p.capacitance(c0)) for p in points], "curve"
