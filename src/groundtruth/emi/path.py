"""The current along a signal path: a Thevenin source, line sections and series parts, a load (per harmonic).

A path from a driver pin to a receiver pin may run through several nets joined by series parts (a 33 Ω damping
resistor, a ferrite bead, an AC-coupling capacitor). Each stretch of track between two parts is a line (its own Z0
and εeff [A9]) with the copper's and the dielectric's loss when its width and stackup are known (PathLine.alpha;
without them lossless, as before 2026-10-19: a quarter-wave stretch at a shunt's resonance then had an unbounded
Q, kraft_flux_mini_2's ADC_I_RAIL_1 moving 13 dB for a 7 % change of Z0), each part a series impedance (components.models.ComponentModel.impedance). The source is
a voltage behind R + jωL with an optional output capacitance; the load a resistance (optional) in parallel with
the receivers' capacitance. The cascade is solved per harmonic: from the load back to the source for the input
impedances, then forward for the voltage and current at every element.

The plane solver drives the whole loop with one current (every filament carries it), so the path's current is the
complex length-weighted mean over all elements (a part counts with its pad-to-pad length) - what line_current
returned for a single line, which this reduces to exactly with one line, no parts and no load resistance.

Shunt parts (PathShunt: a termination to ground, a filter capacitor, a part to a rail) split the path into groups.
The current that leaves the path at a shunt returns through the plane from that shunt's ground pin, so each shunt
is a current loop of its own (the path from the driver up to it), the receivers' loop the last. By superposition
[Desoer 1969] the loop currents are the drops of the groups' mean currents: loop k carries Ī_k - Ī_(k+1), the
receivers' loop Ī_last, so every group carries exactly its mean current (`loop_currents`). A group too short for a
mean of its own (below SHORT_GROUP of the path, at least SHORT_GROUP_M: a part at the receiver) takes the previous
group's mean split as the currents divide at its node: with a point current there instead, the weights grew to ±16
at 3 GHz on a 40 mm line (standing waves make the mean and the end current very different), and the loops' few per
cent of geometric difference to several dB.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable, Optional, Union

import numpy as np

from .spectrum import C0

SHORT_GROUP = 0.02          # a group shorter than this share of the path (and SHORT_GROUP_M) has no mean
SHORT_GROUP_M = 1e-3        # of its own: loop_currents


@dataclass
class PathLine:
    """A stretch of track between two parts (or a path end): a line of Z0 and εeff, lossy when width_m is given
    (the strip's copper: thickness_m, sigma; the dielectric: er, tan_d), else lossless."""
    z0: float
    eeff: float
    length_m: float
    width_m: Optional[float] = None
    thickness_m: float = 35e-6
    er: Optional[float] = None
    tan_d: float = 0.0
    sigma: float = 5.8e7
    planes_h_m: tuple = ()           # the dielectric heights to its return planes (one: microstrip, two: stripline)

    def alpha(self, f: np.ndarray) -> np.ndarray:
        """The attenuation (Np/m) at f: α = R'/(2 Z0) + G' Z0 / 2, the low-loss line [Pozar 2012].

        Conductor: the strip's R' = 1/(σ·w·t_eff), t_eff the one-sided slab's [Dowell 1966] [A2], and each return
        plane's from the current it carries spread under the strip as 1/(1 + (x/h)²) [Johnson 1993]
        (plane_resistance; a stripline's two planes share it inversely to their heights [A36]); without the heights,
        the plane as much as the strip (Pozar's wide-strip α_c = R_s/(Z0·W)). Dielectric: G' = ωC'·tan δ
        with C' = √εeff/(c·Z0), the loss tangent weighted by the field's share in the dielectric, the filling
        factor εr(εeff - 1)/(εeff(εr - 1)) (1 for a stripline, εeff = εr) [Pozar 2012]."""
        f = np.asarray(f, float)
        if self.width_m is None or self.width_m <= 0:
            return np.zeros(len(f))
        from ..solver.kernel import skin_thickness
        teff = skin_thickness(self.thickness_m, np.maximum(f, 1.0), self.sigma)
        r_sheet = 1.0 / (self.sigma * teff)                  # ohm per square
        hs = [h for h in self.planes_h_m if h > 0]
        if hs:
            # the current divides between the planes inversely to their heights (images of the strip in each)
            shares = [1.0] if len(hs) == 1 else [hs[1] / sum(hs), hs[0] / sum(hs)]
            plane = sum(s * s * plane_resistance(self.width_m, h) for s, h in zip(shares, hs))
        else:
            plane = 1.0 / self.width_m
        r = r_sheet * (1.0 / self.width_m + plane)
        er = self.er or self.eeff
        fill = 1.0 if er - 1.0 < 1e-6 or self.eeff >= er else er * (self.eeff - 1.0) / (self.eeff * (er - 1.0))
        g = 2 * np.pi * f * math.sqrt(self.eeff) / (C0 * self.z0) * self.tan_d * fill
        return r / (2 * self.z0) + g * self.z0 / 2

    def rlgc(self, f: np.ndarray) -> tuple:
        """The line's per-metre R'(f), L', G'(f), C' [Pozar 2012]: L' = Z0·√εeff/c and C' = √εeff/(c·Z0) from its
        Z0 and εeff, R' the strip's and its planes' resistance (as in alpha: 2·Z0·α_c), G' = ωC'·tan δ·filling
        factor; R' and G' zero for a lossless line (no width)."""
        f = np.asarray(f, float)
        lp = self.z0 * math.sqrt(self.eeff) / C0
        cp = math.sqrt(self.eeff) / (C0 * self.z0)
        if self.width_m is None or self.width_m <= 0:
            return np.zeros(len(f)), lp, np.zeros(len(f)), cp
        er = self.er or self.eeff
        fill = 1.0 if er - 1.0 < 1e-6 or self.eeff >= er else er * (self.eeff - 1.0) / (self.eeff * (er - 1.0))
        g = 2 * np.pi * f * cp * self.tan_d * fill
        r = 2 * self.z0 * (self.alpha(f) - g * self.z0 / 2)          # alpha's conductor part, back to R'
        return r, lp, g, cp

    def zc(self, f: np.ndarray) -> np.ndarray:
        """The characteristic impedance at f, √((R' + jωL')/(G' + jωC')) [Pozar 2012]: Z0 where the line is low
        loss (R' ≪ ωL'), larger and capacitive towards DC, where the strip's resistance dominates. The low-loss form
        (Z0 real, α = R'/2Z0 + G'Z0/2) used before 2026-10-21 made an open 86 mm track look like ~10 kΩ to ground
        at 1 Hz (Z0 / αl): a Bode sweep of kraft_flux_mini_2's ADC path read −4.6 dB at DC."""
        f = np.asarray(f, float)
        r, lp, g, cp = self.rlgc(f)
        if not np.any(r) and not np.any(g):
            return np.full(len(f), complex(self.z0))
        w = 2 * np.pi * np.maximum(f, 1e-12)
        return np.sqrt((r + 1j * w * lp) / (g + 1j * w * cp))

    def gamma_l(self, f: np.ndarray) -> np.ndarray:
        """γ·l at f, γ = √((R' + jωL')(G' + jωC')) [Pozar 2012] (its root with Re ≥ 0); a lossless line's jβ,
        β = ω√εeff / c. Where the line is low loss, γ = α + jβ with alpha's α."""
        f = np.asarray(f, float)
        r, lp, g, cp = self.rlgc(f)
        w = 2 * np.pi * f
        if not np.any(r) and not np.any(g):
            return 1j * w * math.sqrt(self.eeff) / C0 * self.length_m
        gam = np.sqrt((r + 1j * w * lp) * (g + 1j * w * cp))
        gam = np.where(gam.real < 0, -gam, gam)
        return gam * self.length_m


def plane_resistance(w: float, h: float) -> float:
    """A return plane's resistance per metre and per ohm of sheet resistance under a strip of width w at height h
    (m): its current density is the strip's uniform current spread by the plane's response 1/(1 + (x/h)²)
    [Johnson 1993], J = [atan((x + a)/h) - atan((x - a)/h)] / (π·w) with a = w/2, and ∫J² dx in closed form
    (Parseval: the transform is sinc(k·a)·e^(-h|k|)): [a·atan(a/h) - (h/2)·ln(1 + a²/h²)] / (π·a²). It tends to
    1/(2πh) for a thin strip and to 1/w for a wide one (Pozar's wide-strip plane)."""
    a = w / 2
    return (a * math.atan(a / h) - h / 2 * math.log1p(a * a / (h * h))) / (math.pi * a * a)


@dataclass
class PathPart:
    """A series part: impedance z(f) (ohms), counted with its pad-to-pad length in the mean current."""
    name: str
    z: Callable[[float], Optional[complex]]
    length_m: float = 0.0


@dataclass
class PathShunt:
    """A part from the path to a return (ground, or a rail): impedance z(f)."""
    name: str
    z: Callable[[float], Optional[complex]]


@dataclass
class PathSource:
    """A Thevenin source: R + jωL in series, an optional capacitance from its output to the return. z: an
    impedance z(f) instead of R + jωL (an op-amp stage's closed-loop output, PathAmp.z_out)."""
    r: float
    l: float = 0.0
    c: float = 0.0
    z: Optional[Callable[[float], complex]] = None


@dataclass
class PathLoad:
    """The receivers: a capacitance (all receivers together), optionally a resistance in parallel (a
    termination at the receiver; None = none). z: an impedance z(f) instead (an op-amp's input, c and r unused)."""
    c: float
    r: Optional[float] = None
    z: Optional[Callable[[float], complex]] = None


@dataclass
class PathResult:
    """The path solved per harmonic: the mean current that drives the loop model, the current into the path and
    the voltage at its end, each part's |Z|, and per group between shunts its mean current, length, and the share
    of the current that goes on past each shunt (for loop_currents)."""
    freqs: np.ndarray
    i_mean: np.ndarray          # complex mean current along the path (A peak per harmonic)
    i_in: np.ndarray            # current leaving the source's output (into the path)
    v_end: np.ndarray           # voltage at the receivers
    part_z: dict = field(default_factory=dict)   # part name -> |Z| per harmonic (ohms)
    group_mean: list = field(default_factory=list)   # complex mean current of each group (between shunts)
    shunt_i: dict = field(default_factory=dict)      # shunt name -> complex current into it
    group_len: list = field(default_factory=list)    # each group's length (m)
    split: list = field(default_factory=list)        # at each shunt: the share of the current going on past it

    def loop_currents(self) -> list[np.ndarray]:
        """The current of each loop, the shunts' in path order, then the receivers' (see the module docstring)."""
        g = list(self.group_mean)
        short = max(SHORT_GROUP * sum(self.group_len), SHORT_GROUP_M)
        # a too-short group (no meaningful mean of its own) takes the previous group's mean times the share of
        # the current that passes the shunt between them
        for k in range(1, len(g)):
            if self.group_len[k] < short:
                g[k] = g[k - 1] * self.split[k - 1]
        return [g[k] - g[k + 1] for k in range(len(g) - 1)] + [g[-1]]


def _series_z(part: PathPart, f: np.ndarray) -> np.ndarray:
    """A part's impedance at every harmonic (an open part as 1 GΩ, so the cascade's arithmetic stays finite)."""
    out = np.empty(len(f), complex)
    for k, fk in enumerate(f):
        z = part.z(float(fk))
        out[k] = 1e9 if z is None else complex(z)       # an open part: practically no current
    return out


def path_currents(freqs, v_source, elements: list, source: PathSource, load: PathLoad) -> PathResult:
    """Voltages and currents along the path per harmonic. elements: PathLine | PathPart from the source to the
    load. v_source: the source's open-circuit amplitude per harmonic."""
    f = np.asarray(freqs, float)
    w = 2 * np.pi * f
    if load.z is not None:
        zl = np.array([complex(load.z(float(fk))) for fk in f])
    else:
        zl = 1.0 / (1j * w * max(load.c, 1e-15))
        if load.r is not None:
            zl = 1.0 / (1.0 / zl + 1.0 / load.r)
    # backwards: the input impedance at each element's input
    z_in = [None] * len(elements)
    z = zl
    parts_z = {}
    for k in range(len(elements) - 1, -1, -1):
        e = elements[k]
        if isinstance(e, PathLine):
            # a line transforms the impedance behind it: Z_in = Zc (Z + Zc tanh γl) / (Zc + Z tanh γl) [Pozar 2012]
            # (lossless, γl = jβl: tanh = j tan βl); Zc and γ the full line's (zc, gamma_l): down to DC
            t = np.tanh(e.gamma_l(f))
            zc = e.zc(f)
            z = zc * (z + zc * t) / (zc + z * t)
        elif isinstance(e, PathShunt):
            zp = _series_z(e, f)
            parts_z[k] = zp
            z = 1.0 / (1.0 / z + 1.0 / zp)          # a shunt in parallel with everything behind it
        else:
            zp = _series_z(e, f)
            parts_z[k] = zp
            z = z + zp                              # a series part adds its impedance
        z_in[k] = z
    z_path = z
    zs = (np.array([complex(source.z(float(fk))) for fk in f]) if source.z is not None
          else source.r + 1j * w * source.l)
    z_node = z_path if source.c <= 0 else 1.0 / (1.0 / z_path + 1j * w * source.c)
    v = np.asarray(v_source, complex) * z_node / (zs + z_node)      # at the source's output
    i = v / z_path                                                   # into the path
    i_in = i
    total = sum(getattr(e, "length_m", 0.0) for e in elements)
    acc = np.zeros(len(f), complex)
    groups, g_acc, g_len, shunt_i, lens, split = [], np.zeros(len(f), complex), 0.0, {}, [], []

    def close_group():
        """End the current group (at a shunt or the path's end): its length-weighted mean current."""
        groups.append(g_acc / g_len if g_len > 0 else i.copy())
        lens.append(g_len)
    # forwards from the source: the voltage v and current i at each element's input, stepped through it
    for k, e in enumerate(elements):
        if isinstance(e, PathShunt):
            close_group()
            g_acc, g_len = np.zeros(len(f), complex), 0.0
            shunt_i[e.name] = v / parts_z[k]            # the shunt takes V / Z_shunt; the rest goes on
            split.append(np.where(np.abs(i) > 0, (i - shunt_i[e.name]) / np.where(np.abs(i) > 0, i, 1.0), 0.0))
            i = i - shunt_i[e.name]
            continue
        g_len += e.length_m
        if isinstance(e, PathLine):
            gl = e.gamma_l(f)
            x = np.where(np.abs(gl) > 1e-12, gl, 1e-12)
            # the line's mean current, the integral of I(z) = I1 cosh γz - (V1/Z0) sinh γz over its length (lossless:
            # γ = jβ, spectrum.line_current's form), then its ABCD matrix carries v and i to its end:
            # V2 = V1 cosh γl - Zc I1 sinh γl,  I2 = I1 cosh γl - (V1 / Zc) sinh γl  [Pozar 2012]
            ch, sh = np.cosh(gl), np.sinh(gl)
            zc = e.zc(f)
            i_avg = i * np.sinh(x) / x - (v / zc) * (np.cosh(x) - 1) / x
            acc += e.length_m * i_avg
            g_acc = g_acc + e.length_m * i_avg
            v, i = v * ch - i * zc * sh, i * ch - (v / zc) * sh
        else:
            # a series part: the same current through it, the voltage drops by I Z
            acc += e.length_m * i
            g_acc = g_acc + e.length_m * i
            v = v - i * parts_z[k]
    close_group()
    i_mean = acc / total if total > 0 else i_in
    names = {}
    for k, zp in parts_z.items():
        names[elements[k].name] = np.abs(zp)
    return PathResult(f, i_mean, i_in, v, names, groups, shunt_i, lens, split)


@dataclass
class PathAmp:
    """An op-amp stage between two stretches of a path: gain(f) (V/V, complex), the input's impedance z_in(f)
    (the load of the stage before), the output's impedance z_out (the source of the stage after: ohms, or z(f), the
    closed-loop output of ComponentModel.amp_z_out_at)."""
    name: str
    gain: Callable[[float], complex]
    z_in: Callable[[float], complex]
    z_out: Union[float, Callable[[float], complex]]
    slew: Optional[float] = None      # V/s: each output harmonic is capped at slew / 2πf (amplified_path)


def amplified_path(freqs, v_source, stages: list[list], amps: list[PathAmp], source: PathSource,
                   load: PathLoad) -> list[PathResult]:
    """A path through op-amp stages: stages[k] the elements between amps[k-1] and amps[k]. Stage 0 is driven by
    the source and loaded by the first amp's input; stage k + 1 by gain_k(f) × the voltage at amp k's input, behind
    its z_out, and loaded by the next amp's input or, the last, by the receivers. One PathResult per stage (their
    loop_currents are each stage's loops). A slew rate caps the output's amplitude per harmonic at slew / 2πf (a
    crude large-signal limit: the harmonics are not redistributed)."""
    f = np.asarray(freqs, float)
    out = []
    v = np.asarray(v_source, complex)
    src = source
    for k, elements in enumerate(stages):
        # each stage is an ordinary path: the next amplifier's input is its load (the last stage's: the receivers)
        ld = load if k == len(amps) else PathLoad(0.0, z=amps[k].z_in)
        r = path_currents(f, v, elements, src, ld)
        out.append(r)
        if k < len(amps):
            # the amplifier isolates the stages: its output is a new Thevenin source [Desoer 1969], gain x its input
            # voltage behind z_out (the stage after it does not load the stage before it)
            a = amps[k]
            v = np.array([complex(a.gain(float(fk))) for fk in f]) * r.v_end
            if a.slew:
                cap = a.slew / (2 * np.pi * np.maximum(f, 1.0))
                mag = np.abs(v)
                v = np.where(mag > cap, v * cap / np.maximum(mag, 1e-300), v)
            src = PathSource(0.0, z=a.z_out) if callable(a.z_out) else PathSource(a.z_out)
    return out
