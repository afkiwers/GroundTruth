"""Small-signal AC analysis of a linear circuit by modified nodal analysis (MNA) [Ho 1975]: any topology of
resistors, capacitors, inductors, op-amps, controlled sources and transmission-line two-ports.

The circuit is the descriptor system (G + sC)·x = b·u: x the node voltages (ground is node 0, left out) and the
currents of the elements MNA gives an unknown of their own (voltage sources, VCVS, inductors); G the conductances
and the connections, C the capacitances and the inductors' −L; u the input source's voltage. A two-port line adds
its admittance Y(f) at each frequency (frequency-dependent and not rational: `Circuit.y2`).

- **Solve** (`Circuit.solve`): x at each frequency, s = j2πf; the response is v(out) − v(out_ref) per volt of the
  input (`transfer`), or any node's voltage.
- **Poles and zeros, exact** (`Circuit.poles_zeros`), for a circuit without frequency-dependent two-ports: the poles
  are the finite generalised eigenvalues of the pencil (G, −C) [Golub 2013], the transmission zeros those of the
  system matrix bordered with the input and output vectors, [[G + sC, −b], [cᵀ, 0]] [Rosenbrock 1970]. Modes the
  input does not reach or the output does not see cancel against a zero at the same place and are dropped, as are
  the infinite eigenvalues of the singular C. The matrices are equilibrated (row and column scaling) and s is
  scaled to a reference frequency first, so parts from pF to MΩ stay well conditioned.
- **The op-amp** (`Circuit.opamp`): a linear macromodel from datasheet values [Franco 2015]: Rin ∥ Cin between the
  inputs, Cin_cm from each input to ground, the open-loop gain A(s) = A0 / ((1 + s/ω1)(1 + s/ω2)) as a
  transconductance into R1 ∥ C1 (ω1 = 2π·GBW/A0) and an optional second stage R2 ∥ C2 (ω2), a unity-gain buffer and
  the open-loop output resistance R_O. Small signal only: no rails, saturation or slew.

Validated in tests/test_mna.py against closed forms (RC, RLC, inverting / non-inverting amplifiers with their
bandwidth, Sallen-Key and multiple-feedback filters' f0 and Q, an integrator, a line two-port)."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable, Optional

import numpy as np

GROUND = "0"                    # the ground node's name (also "GND" when a caller names it so: Circuit(ground=...))
CANCEL = 1e-6                   # a pole and a zero this close (relative) cancel
F_REF = 1e3                     # Hz: s is scaled by 2π·F_REF for the eigenvalue problems


@dataclass
class OpAmpParams:
    """An op-amp's small-signal values (SI units): a0 the DC open-loop gain (V/V), gbw (Hz), fp2 a second pole (Hz,
    None: one pole), r_out the open-loop output resistance (Ω), r_in / c_in between the inputs, c_cm from each
    input to ground."""
    gbw: float
    a0: float = 1e5
    fp2: Optional[float] = None
    r_out: float = 1.0
    r_in: float = 1e12
    c_in: float = 2e-12
    c_cm: float = 0.0


@dataclass
class Circuit:
    """A linear circuit for MNA. Nodes are named; elements are added with r, c, l, vsource, vcvs, vccs, opamp, y2,
    part; `ground` names the reference node."""
    ground: str = GROUND
    nodes: dict = field(default_factory=dict)          # name -> index (ground: -1)
    _g: list = field(default_factory=list)             # (row, col, value) stamps of G
    _c: list = field(default_factory=list)             # ... of C
    _extra: int = 0                                    # unknowns beside the node voltages
    _extras: list = field(default_factory=list)        # their (kind, name), in order
    _y2: list = field(default_factory=list)            # (node indices a1, b1, a2, b2, y(f) -> 2x2)
    _input: Optional[int] = None                       # the input source's unknown (an extra index)
    _counter: int = 0

    # ------------------------------------------------------------------ building
    def node(self, name: str) -> int:
        """The index of a node (made on first use); the ground is -1."""
        if name == self.ground:
            return -1
        if name not in self.nodes:
            self.nodes[name] = len(self.nodes)
        return self.nodes[name]

    def internal(self, prefix: str) -> str:
        """A fresh internal node's name (a part's ESR-ESL junction, an op-amp's gain stage)."""
        self._counter += 1
        return f"{prefix}#{self._counter}"

    def _branch(self, kind: str, name: str) -> int:
        """A new extra unknown (a branch current); its index among the extras."""
        self._extras.append((kind, name))
        self._extra += 1
        return self._extra - 1

    def r(self, a: str, b: str, ohms: float):
        """A resistor between a and b (Ω > 0)."""
        self._admit(self.node(a), self.node(b), 1.0 / ohms, self._g)

    def c(self, a: str, b: str, farads: float):
        """A capacitor between a and b (F)."""
        self._admit(self.node(a), self.node(b), farads, self._c)

    def l(self, a: str, b: str, henries: float, name: str = "L"):
        """An inductor between a and b: its current an unknown, v_a − v_b − sL·i = 0."""
        k = ("x", self._branch("L", name))
        self._stamp_branch(self.node(a), self.node(b), k)
        self._c.append((k, k, -henries))

    def vsource(self, p: str, n: str, name: str = "Vin") -> None:
        """The input: an ideal voltage source from n to p of 1 V (the response is per volt of it). One only."""
        k = ("x", self._branch("V", name))
        self._stamp_branch(self.node(p), self.node(n), k)
        self._input = k[1]

    def vcvs(self, p: str, n: str, cp: str, cn: str, gain: float, name: str = "E"):
        """A voltage-controlled voltage source: v_p − v_n = gain·(v_cp − v_cn)."""
        k = ("x", self._branch("E", name))
        self._stamp_branch(self.node(p), self.node(n), k)
        for node, sign in ((self.node(cp), -gain), (self.node(cn), gain)):
            if node >= 0:
                self._g.append((k, ("v", node), sign))

    def vccs(self, p: str, n: str, cp: str, cn: str, gm: float):
        """A voltage-controlled current source: a current gm·(v_cp − v_cn) taken out of node p and driven into
        node n (through the source)."""
        for row, rs in ((self.node(p), 1.0), (self.node(n), -1.0)):
            if row < 0:
                continue
            for col, cs in ((self.node(cp), 1.0), (self.node(cn), -1.0)):
                if col >= 0:
                    self._g.append((("v", row), ("v", col), rs * cs * gm))

    def opamp(self, name: str, inp: str, inn: str, out: str, prm: OpAmpParams):
        """An op-amp channel (see the module docstring): inputs inp / inn, output out, its supplies AC ground."""
        if prm.r_in and prm.r_in < 1e15:
            self.r(inp, inn, prm.r_in)
        if prm.c_in:
            self.c(inp, inn, prm.c_in)
        if prm.c_cm:
            self.c(inp, self.ground, prm.c_cm)
            self.c(inn, self.ground, prm.c_cm)
        # stage 1: gm·(v+ − v−) into R1 ∥ C1: v1 = A0·(v+ − v−)/(1 + s/ω1), gm = 1 A/V, R1 = A0
        n1 = self.internal(f"{name}.a1")
        r1 = prm.a0
        self.vccs(self.ground, n1, inp, inn, 1.0)
        self.r(n1, self.ground, r1)
        self.c(n1, self.ground, 1.0 / (2 * math.pi * (prm.gbw / prm.a0) * r1))
        last = n1
        if prm.fp2:
            # stage 2: unity gain, pole at fp2 (1 A/V into 1 Ω ∥ C2)
            n2 = self.internal(f"{name}.a2")
            self.vccs(self.ground, n2, n1, self.ground, 1.0)
            self.r(n2, self.ground, 1.0)
            self.c(n2, self.ground, 1.0 / (2 * math.pi * prm.fp2))
            last = n2
        # the output: a buffer of the last stage behind R_O
        oi = self.internal(f"{name}.o")
        self.vcvs(oi, self.ground, last, self.ground, 1.0, f"{name}.buf")
        self.r(oi, out, max(prm.r_out, 1e-6))

    def part(self, a: str, b: str, kind: str, values: dict, name: str = "part"):
        """A two-pin part from its model (components/models.ComponentModel: kind and parameters, SI): a series
        R-L-C as the model's impedance is [A24], each non-zero piece an element with internal nodes between them;
        "open" adds nothing, "short" a SHORT_OHMS resistor."""
        from ..components.models import SHORT_OHMS
        if kind == "open":
            return
        if kind in ("short", "resistor"):
            self.r(a, b, max(values.get("R", 0.0) if kind == "resistor" else 0.0, SHORT_OHMS))
            return
        chain = []
        if kind == "cap":
            c = values.get("C_bias") or values.get("C") or 0.0
            if c <= 0:
                return
            chain = [("R", values.get("ESR", 0.0)), ("L", values.get("ESL", 0.0)), ("C", c)]
        elif kind in ("inductor", "ferrite"):
            chain = [("R", max(values.get("R", 0.0), SHORT_OHMS)), ("L", values.get("L", 0.0))]
        else:
            raise ValueError(f"{name}: no circuit model for a {kind!r} part")
        chain = [(k, v) for k, v in chain if v > 0]
        pts = [a] + [self.internal(name) for _ in chain[:-1]] + [b]
        for (k, v), x, y in zip(chain, pts, pts[1:]):
            {"R": self.r, "C": self.c}.get(k, lambda x_, y_, v_: self.l(x_, y_, v_, name))(x, y, v)

    def y2(self, a1: str, b1: str, a2: str, b2: str, y: Callable[[float], np.ndarray]):
        """A two-port by its admittance matrix Y(f) (2x2, port 1 from a1 to b1, port 2 from a2 to b2): a line,
        `line_y`. Frequency-dependent: such a circuit has no exact poles (poles_zeros refuses it)."""
        self._y2.append((self.node(a1), self.node(b1), self.node(a2), self.node(b2), y))

    # ------------------------------------------------------------------ internals
    @staticmethod
    def _admit(a: int, b: int, y: float, out: list):
        """Stamp an admittance y between nodes a and b (−1: ground) into a stamp list."""
        for i, si in ((a, 1.0), (b, -1.0)):
            for j, sj in ((a, 1.0), (b, -1.0)):
                if i >= 0 and j >= 0:
                    out.append((("v", i), ("v", j), si * sj * y))

    def _stamp_branch(self, p: int, n: int, k: tuple):
        """A branch current k leaving node p and entering n, and its row's v_p − v_n part."""
        for node, sign in ((p, 1.0), (n, -1.0)):
            if node >= 0:
                self._g.append((("v", node), k, sign))
                self._g.append((k, ("v", node), sign))

    @property
    def size(self) -> int:
        """The number of unknowns: node voltages and extra branch currents."""
        return len(self.nodes) + self._extra

    def _index(self, key: tuple) -> int:
        """A stamp key's row / column: ("v", node) or ("x", extra)."""
        return key[1] if key[0] == "v" else len(self.nodes) + key[1]

    def matrices(self) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """(G, C, b): the descriptor matrices and the input vector (1 at the input source's row)."""
        n = self.size
        g, c, b = np.zeros((n, n)), np.zeros((n, n)), np.zeros(n)
        for row, col, v in self._g:
            g[self._index(row), self._index(col)] += v
        for row, col, v in self._c:
            c[self._index(row), self._index(col)] += v
        if self._input is None:
            raise ValueError("the circuit has no input source (vsource)")
        b[len(self.nodes) + self._input] = 1.0
        return g, c, b

    def output(self, out: str, ref: Optional[str] = None) -> np.ndarray:
        """The output vector c: v(out) − v(ref) (ref: the ground)."""
        c = np.zeros(self.size)
        for name, sign in ((out, 1.0), (ref, -1.0)):
            if name is not None and name != self.ground:
                c[self.nodes[name]] += sign
        return c

    # ------------------------------------------------------------------ analyses
    def solve(self, f) -> np.ndarray:
        """x at each frequency (rows: frequencies), the input source at 1 V."""
        g, c, b = self.matrices()
        f = np.atleast_1d(np.asarray(f, float))
        out = np.empty((len(f), self.size), complex)
        for k, fk in enumerate(f):
            a = g + 2j * math.pi * fk * c
            for a1, b1, a2, b2, y in self._y2:
                m = np.asarray(y(float(fk)), complex)
                ports = ((a1, b1), (a2, b2))
                for i in range(2):
                    for j in range(2):
                        for (ni, si) in ((ports[i][0], 1.0), (ports[i][1], -1.0)):
                            for (nj, sj) in ((ports[j][0], 1.0), (ports[j][1], -1.0)):
                                if ni >= 0 and nj >= 0:
                                    a[ni, nj] += si * sj * m[i, j]
            out[k] = np.linalg.solve(a, b)
        return out

    def transfer(self, f, out: str, ref: Optional[str] = None) -> np.ndarray:
        """H(f) = (v(out) − v(ref)) per volt of the input, at each frequency."""
        return self.solve(f) @ self.output(out, ref)

    def voltage(self, f, node: str) -> np.ndarray:
        """A node's voltage per volt of the input."""
        return self.transfer(f, node)

    def poles_zeros(self, out: str, ref: Optional[str] = None) -> tuple[np.ndarray, np.ndarray, complex]:
        """(poles, zeros, k) of H(s) = k·Π(s − z)/Π(s − p) in rad/s, exact (see the module docstring); k from H at
        a frequency away from every root. Raises ValueError for a circuit with two-ports (not rational)."""
        if self._y2:
            raise ValueError("a circuit with frequency-dependent two-ports (lines) has no exact poles: fit its sweep")
        import scipy.linalg as sl
        g, c, b = self.matrices()
        cv = self.output(out, ref)
        w0 = 2 * math.pi * F_REF
        poles = _finite_eigs(g, -c * w0) * w0
        n = self.size
        gz = np.zeros((n + 1, n + 1))
        cz = np.zeros((n + 1, n + 1))
        gz[:n, :n], gz[:n, n], gz[n, :n] = g, -b, cv
        cz[:n, :n] = c
        zeros = _finite_eigs(gz, -cz * w0) * w0
        poles, zeros = _cancel(poles, zeros)
        # the constant k: H at a test point s0 over the roots' product there
        s0 = 1j * w0 * 0.7071 + 0.3 * w0
        a = g + s0 * c
        h = complex(cv @ sl.solve(a, b.astype(complex)))
        k = h * np.prod(s0 - poles) / np.prod(s0 - zeros) if len(zeros) or len(poles) else h
        return poles, zeros, k


def _finite_eigs(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """The finite generalised eigenvalues λ of a·x = λ·b·x (the pencil's infinite ones, b singular, left out), the
    pencil equilibrated first (rows and columns scaled to comparable norms: eigenvalues unchanged)."""
    import scipy.linalg as sl
    m = np.abs(a) + np.abs(b)
    r = 1.0 / np.maximum(m.max(axis=1), 1e-300)
    a, b = a * r[:, None], b * r[:, None]
    m = np.abs(a) + np.abs(b)
    cs = 1.0 / np.maximum(m.max(axis=0), 1e-300)
    a, b = a * cs[None, :], b * cs[None, :]
    al, be = sl.eig(a, b, right=False, homogeneous_eigvals=True)
    keep = np.abs(be) > 1e-9 * np.maximum(np.abs(al), 1e-300)
    lam = al[keep] / be[keep]
    return lam[np.isfinite(lam) & (np.abs(lam) < 1e12)]


def _cancel(poles: np.ndarray, zeros: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Poles and zeros with the coinciding pairs removed (relative distance below CANCEL, or both within CANCEL of
    the origin's scale): a mode the input does not reach or the output does not see."""
    p, z = list(poles), list(zeros)
    for zi in list(z):
        for pi in p:
            scale = max(abs(pi), abs(zi), 1e-9)
            if abs(pi - zi) <= CANCEL * scale:
                p.remove(pi)
                z.remove(zi)
                break
    return np.array(p, complex), np.array(z, complex)


def line_y(zc: Callable, gamma_l: Callable) -> Callable[[float], np.ndarray]:
    """A transmission line's admittance matrix Y(f) = [[coth γl, −csch γl], [−csch γl, coth γl]] / Zc [Pozar 2012]
    from its Zc(f) and γl(f) (emi/path.PathLine.zc / gamma_l: arrays)."""
    def y(f: float) -> np.ndarray:
        """Y at f (2x2)."""
        z = complex(zc(np.array([f]))[0])
        gl = complex(gamma_l(np.array([f]))[0])
        gl = gl if abs(gl) > 1e-15 else 1e-15
        coth, csch = 1.0 / np.tanh(gl), 1.0 / np.sinh(gl)
        return np.array([[coth, -csch], [-csch, coth]]) / z
    return y
