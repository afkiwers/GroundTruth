"""The current along a signal path: a Thevenin source, line sections and series parts, a load (per harmonic).

A path from a driver pin to a receiver pin may run through several nets joined by series parts (a 33 Ω damping
resistor, a ferrite bead, an AC-coupling capacitor). Each stretch of track between two parts is a lossless line
(its own Z0 and εeff), each part a series impedance (components.models.ComponentModel.impedance). The source is
a voltage behind R + jωL with an optional output capacitance; the load a resistance (optional) in parallel with
the receivers' capacitance. The cascade is solved per harmonic: from the load back to the source for the input
impedances, then forward for the voltage and current at every element.

The plane solver drives the whole loop with one current (every filament carries it), so the path's current is the
complex length-weighted mean over all elements (a part counts with its pad-to-pad length) - what line_current
returned for a single line, which this reduces to exactly with one line, no parts and no load resistance.

Shunt parts (PathShunt: a termination to ground, a filter capacitor, a part to a rail) split the path into groups.
The current that leaves the path at a shunt returns through the plane from that shunt's ground pin, so each shunt
is a current loop of its own (the path from the driver up to it), the receivers' loop the last. By superposition
the loop currents are the drops of the groups' mean currents: loop k carries Ī_k - Ī_(k+1), the receivers' loop
Ī_last, so every group carries exactly its mean current (`loop_currents`). A group too short for a mean of its own
(below SHORT_GROUP of the path, at least SHORT_GROUP_M: a part at the receiver) takes the previous group's mean
split as the currents divide at its node: with a point current there instead, the weights grew to ±16 at 3 GHz on a
40 mm line (standing waves make the mean and the end current very different), and the loops' few per cent of
geometric difference to several dB.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable, Optional

import numpy as np

from .spectrum import C0

SHORT_GROUP = 0.02          # a group shorter than this share of the path (and SHORT_GROUP_M) has no mean
SHORT_GROUP_M = 1e-3        # of its own: loop_currents


@dataclass
class PathLine:
    """A stretch of track between two parts (or a path end), a lossless line."""
    z0: float
    eeff: float
    length_m: float


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
    """A Thevenin source: R + jωL in series, an optional capacitance from its output to the return."""
    r: float
    l: float = 0.0
    c: float = 0.0


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
            # a line transforms the impedance behind it: Z_in = Z0 (Z + j Z0 tan bl) / (Z0 + j Z tan bl)
            t = np.tan(w * math.sqrt(e.eeff) / C0 * e.length_m)
            z = e.z0 * (z + 1j * e.z0 * t) / (e.z0 + 1j * z * t)
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
    zs = source.r + 1j * w * source.l
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
            bl = w * math.sqrt(e.eeff) / C0 * e.length_m
            b = np.maximum(bl, 1e-12)
            # the line's mean current (as spectrum.line_current), then its ABCD matrix carries v and i to its end:
            # V2 = V1 cos bl - j Z0 I1 sin bl,  I2 = I1 cos bl - j (V1 / Z0) sin bl
            i_avg = i * np.sin(b) / b - 1j * (v / e.z0) * (1 - np.cos(b)) / b
            acc += e.length_m * i_avg
            g_acc = g_acc + e.length_m * i_avg
            v, i = v * np.cos(bl) - 1j * i * e.z0 * np.sin(bl), i * np.cos(bl) - 1j * (v / e.z0) * np.sin(bl)
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
    (the load of the stage before), the output's impedance z_out (the source of the stage after)."""
    name: str
    gain: Callable[[float], complex]
    z_in: Callable[[float], complex]
    z_out: float
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
            # the amplifier isolates the stages: its output is a new Thevenin source, gain x its input voltage
            # behind z_out (the stage after it does not load the stage before it)
            a = amps[k]
            v = np.array([complex(a.gain(float(fk))) for fk in f]) * r.v_end
            if a.slew:
                cap = a.slew / (2 * np.pi * np.maximum(f, 1.0))
                mag = np.abs(v)
                v = np.where(mag > cap, v * cap / np.maximum(mag, 1e-300), v)
            src = PathSource(a.z_out)
    return out
