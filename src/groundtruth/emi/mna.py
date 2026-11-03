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
  scaled to a reference frequency first, so parts from pF to MΩ stay well conditioned. Roots lie between 1 mHz
  and 1 THz [A82].
- **Lines** (`Circuit.y2` with `line_y`): a line over its reference is stamped as its exact π equivalent, the
  series Zc·sinh γl as an impedance branch, so a short line's shunt capacitance is not lost beside its large series
  admittance [Pozar 2012].
- **Values** are checked as they go in: a resistor's > 0, a capacitor's and inductor's ≥ 0, all finite, one input
  source (else a ValueError).
- **The op-amp** (`Circuit.opamp`): a linear macromodel from datasheet values [Franco 2015] [A52]: Rin ∥ Cin between the
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
CANCEL = 1e-6                   # a pole and a zero this close cancel: relative to max(|Re p|, CANCEL_FLOOR·|p|) (_cancel)
CANCEL_FLOOR = 1e-4             # ... so a lightly damped pair's own scale counts, not only |p| (audit M-4)
F_REF = 1e3                     # Hz: s is scaled by 2π·F_REF for the eigenvalue problems
PI_GL = 0.5                     # a line is stamped as its π below this |γl| (electrically short), else as its Y
R_BRANCH = 1e-3                 # Ω: a resistor below this is a branch (v_a − v_b − R·i = 0), not a conductance (r)
ORIGIN = 1e-6                   # a root below ORIGIN·2π·F_REF rad/s (1 mHz) is at the origin; the QZ keeps finite roots
INFINITE = 1e9                  # up to INFINITE·2π·F_REF rad/s (1 THz): fixed bounds, [A82] (audit M-5)


def _finite_value(what: str, value: float, positive: bool = False) -> float:
    """`value` as a float when it is finite (and > 0 with `positive`, else ≥ 0), else a ValueError naming `what`
    (audit M-6: a 0 Ω resistor divided by zero, a negative or NaN value went in silently)."""
    v = float(value)
    if not math.isfinite(v) or (v <= 0 if positive else v < 0):
        need = "a finite value > 0" if positive else "a finite value ≥ 0"
        raise ValueError(f"{what} must be {need}, got {value!r}")
    return v


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
    # the two-ports: ("y", a1, b1, a2, b2, y(f) -> 2x2) stamped as admittances, or ("pi", a1, a2, b, extra index,
    # pi(f) -> (z_series, y_shunt)) for a line with one reference (y2)
    _y2: list = field(default_factory=list)
    _input: Optional[int] = None                       # the input source's unknown (an extra index)
    _counter: int = 0
    # each op-amp channel: {"buf": its output buffer's extra index, "inp", "inn": its input nodes, "prm"} (loop_gain)
    opamps: dict = field(default_factory=dict)
    # the poles of (G, −C) by the circuit's size (poles_zeros: the same for every output, audit M-13)
    _pole_cache: dict = field(default_factory=dict, repr=False, compare=False)

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
        """A resistor between a and b (Ω > 0, finite: else a ValueError; a short is a small resistance, as
        part("short") makes it): its conductance stamped into G [Ho 1975]; below R_BRANCH as a branch, its current an
        unknown and v_a − v_b − R·i = 0 (as an inductor's): a conductance of 1e12 S added into a node's row beside
        milli-siemens kept only their first digits whatever the solve did after (audit 2026-11, M-11: 1e-12 Ω beside
        1 kΩ, 1e-3 off)."""
        ohms = _finite_value(f"resistor {a}-{b}", ohms, positive=True)
        if ohms < R_BRANCH:
            k = ("x", self._branch("R", f"{a}-{b}"))
            self._stamp_branch(self.node(a), self.node(b), k)
            self._g.append((k, k, -ohms))
            return
        self._admit(self.node(a), self.node(b), 1.0 / ohms, self._g)

    def c(self, a: str, b: str, farads: float):
        """A capacitor between a and b (F ≥ 0, finite: else a ValueError): its capacitance stamped into C
        (admittance sC) [Ho 1975]."""
        farads = _finite_value(f"capacitor {a}-{b}", farads)
        self._admit(self.node(a), self.node(b), farads, self._c)

    def l(self, a: str, b: str, henries: float, name: str = "L"):
        """An inductor between a and b (H ≥ 0, finite: else a ValueError; 0 H is a short): its current an unknown,
        v_a − v_b − sL·i = 0 (MNA's branch stamp) [Ho 1975]."""
        henries = _finite_value(f"inductor {name} {a}-{b}", henries)
        k = ("x", self._branch("L", name))
        self._stamp_branch(self.node(a), self.node(b), k)
        self._c.append((k, k, -henries))

    def vsource(self, p: str, n: str, name: str = "Vin") -> None:
        """The input: an ideal voltage source from n to p of 1 V (the response is per volt of it), its current an
        unknown [Ho 1975]. One only: a second one is a ValueError (it replaced the first silently: audit M-6)."""
        if self._input is not None:
            raise ValueError(f"the circuit has its input source already: {name} would be a second one")
        k = ("x", self._branch("V", name))
        self._stamp_branch(self.node(p), self.node(n), k)
        self._input = k[1]

    def vcvs(self, p: str, n: str, cp: str, cn: str, gain: float, name: str = "E"):
        """A voltage-controlled voltage source: v_p − v_n = gain·(v_cp − v_cn), its current an unknown
        [Ho 1975]. A gain that is not finite is a ValueError."""
        if not math.isfinite(float(gain)):
            raise ValueError(f"{name}: its gain must be finite, got {gain!r}")
        k = ("x", self._branch("E", name))
        self._stamp_branch(self.node(p), self.node(n), k)
        for node, sign in ((self.node(cp), -gain), (self.node(cn), gain)):
            if node >= 0:
                self._g.append((k, ("v", node), sign))

    def vccs(self, p: str, n: str, cp: str, cn: str, gm: float):
        """A voltage-controlled current source: a current gm·(v_cp − v_cn) taken out of node p and driven into
        node n (through the source), stamped into G [Ho 1975]. A gm that is not finite is a ValueError."""
        if not math.isfinite(float(gm)):
            raise ValueError(f"the VCCS {p}-{n}: its gm must be finite, got {gm!r}")
        for row, rs in ((self.node(p), 1.0), (self.node(n), -1.0)):
            if row < 0:
                continue
            for col, cs in ((self.node(cp), 1.0), (self.node(cn), -1.0)):
                if col >= 0:
                    self._g.append((("v", row), ("v", col), rs * cs * gm))

    def opamp(self, name: str, inp: str, inn: str, out: str, prm: OpAmpParams):
        """An op-amp channel: its linear macromodel [Franco 2015] [A52] (see the module docstring): inputs inp / inn,
        output out, its supplies AC ground; A(s) = A0 / ((1 + s/ω1)(1 + s/ω2)), ω1 = 2π·GBW/A0."""
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
        self.opamps[name] = {"buf": self._extra - 1, "oi": oi, "inp": inp, "inn": inn, "prm": prm}
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
            # the capacitance in the circuit (analog_net passes ComponentModel.capacitance() as C: the part at its DC
            # voltage in a run, analysis/bias.py); a C_bias in hand-made values wins, as the model's rule
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
        """A two-port by its admittance matrix Y(f) (2x2, port 1 from a1 to b1, port 2 from a2 to b2) [Pozar 2012]: a
        line, `line_y`. Frequency-dependent: such a circuit has no exact poles (poles_zeros refuses it).

        A line (`line_y`, which also gives its π equivalent) whose two ports share their reference (b1 = b2: a track
        over its plane) is stamped as that π where it is electrically short (|γl| < PI_GL): a shunt tanh(γl/2)/Zc at
        each end and the series Zc·sinh γl as a branch with its current an unknown (an impedance stamp, as an
        inductor's) [Pozar 2012] [Ho 1975]; at longer electrical lengths by its Y as before (_y2_stamps). Stamped as Y,
        a short line's series admittance csch(γl)/Zc ~ 1/(Zc·γl) swamps its shunt ~ γl/(2Zc) on the same diagonal:
        coth − csch lost a 1 mm line's input capacitance at 1 kHz to 7 % (audit M-12); the π keeps the two apart."""
        ia1, ib1, ia2, ib2 = self.node(a1), self.node(b1), self.node(a2), self.node(b2)
        if getattr(y, "pi", None) is not None and ib1 == ib2:
            k = ("x", self._branch("T", f"line {a1}-{a2}"))
            self._stamp_branch(ia1, ia2, k)                   # its row: v_a1 − v_a2 − Z(f)·i = 0 (Z in _y2_stamps)
            self._y2.append(("pi", ia1, ia2, ib1, k[1], y))
            return
        self._y2.append(("y", ia1, ib1, ia2, ib2, y))

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
        """The output vector c: v(out) − v(ref) (ref: the ground). A node the circuit does not have is a ValueError
        naming it (it was a bare KeyError: audit M-6)."""
        c = np.zeros(self.size)
        for name, sign in ((out, 1.0), (ref, -1.0)):
            if name is not None and name != self.ground:
                if name not in self.nodes:
                    raise ValueError(f"the circuit has no node {name!r}")
                c[self.nodes[name]] += sign
        return c

    # ------------------------------------------------------------------ analyses
    def _sparse(self) -> tuple:
        """(G, C) as scipy sparse matrices and b."""
        import scipy.sparse as sp
        n = self.size
        mats = []
        for stamps in (self._g, self._c):
            rows = [self._index(r) for r, _, _ in stamps]
            cols = [self._index(c) for _, c, _ in stamps]
            mats.append(sp.coo_matrix(([v for _, _, v in stamps], (rows, cols)), shape=(n, n)).tocsc())
        if self._input is None:
            raise ValueError("the circuit has no input source (vsource)")
        b = np.zeros(n, complex)
        b[len(self.nodes) + self._input] = 1.0
        return mats[0], mats[1], b

    def _y2_stamps(self, f: np.ndarray) -> tuple:
        """The two-ports' stamps at every frequency: (rows, cols, values[frequency, stamp]).

        A π-stamped line (y2) at a frequency where it is electrically short, |γl| < PI_GL (|Z·Y_sh| = |cosh γl − 1|
        below PI_GL²/2): its series impedance on its branch row, v_a1 − v_a2 − Z·i = 0, and its two shunt admittances.
        Where it is not, the π's shunt tanh(γl/2)/Zc grows without bound towards an odd number of half wavelengths
        (a half-wave line inverts, V2 = −V1, which no lumped π holds): there the line is stamped by its Y matrix as
        any two-port, and its branch row made i = 0 (1 on its diagonal, its constant ±1 of v_a1 − v_a2 cancelled)."""
        rows, cols, vals = [], [], []

        def admit(ports, m):
            """A 2x2 admittance matrix m (per frequency) between two ports (node, reference) as node stamps."""
            for i in range(2):
                for j in range(2):
                    for ni, si in ((ports[i][0], 1.0), (ports[i][1], -1.0)):
                        for nj, sj in ((ports[j][0], 1.0), (ports[j][1], -1.0)):
                            if ni >= 0 and nj >= 0:
                                rows.append(ni)
                                cols.append(nj)
                                vals.append(si * sj * m[:, i, j])
        for rec in self._y2:
            if rec[0] == "pi":
                _, a1, a2, b, kx, y = rec
                z_ser, y_sh = (np.asarray(v, complex).reshape(len(f)) for v in y.pi(f))
                short = np.abs(z_ser * y_sh) < PI_GL ** 2 / 2
                kk = len(self.nodes) + kx
                rows.append(kk)
                cols.append(kk)
                vals.append(np.where(short, -z_ser, 1.0))     # v_a1 − v_a2 − Z·i = 0, or i = 0
                for a, sign in ((a1, -1.0), (a2, 1.0)):       # where long: v_a1 − v_a2 cancelled from the row
                    if a >= 0:
                        rows.append(kk)
                        cols.append(a)
                        vals.append(np.where(short, 0.0, sign))
                sh = np.where(short, y_sh, 0.0)
                admit(((a1, b), (a2, b)), np.stack([np.stack([sh, 0 * sh], -1), np.stack([0 * sh, sh], -1)], -2))
                m = np.asarray(y(f), complex).reshape(len(f), 2, 2)
                admit(((a1, b), (a2, b)), np.where(short[:, None, None], 0.0, m))
                continue
            _, a1, b1, a2, b2, y = rec
            admit(((a1, b1), (a2, b2)), np.asarray(y(f), complex).reshape(len(f), 2, 2))   # Y(f), all f at once
        return rows, cols, (np.array(vals).T if vals else np.zeros((len(f), 0)))

    def solve(self, f) -> np.ndarray:
        """x at each frequency (rows: frequencies), the input source at 1 V: a sparse LU per frequency of
        G + j2πf·C (+ the two-ports' Y(f)) [Ho 1975] [Golub 2013]."""
        import scipy.sparse as sp
        import scipy.sparse.linalg as spl
        g, c, b = self._sparse()
        f = np.atleast_1d(np.asarray(f, float))
        rows, cols, vals = self._y2_stamps(f)
        n = self.size
        out = np.empty((len(f), n), complex)
        for k, fk in enumerate(f):
            a = g + (2j * math.pi * fk) * c
            if rows:
                a = a + sp.coo_matrix((vals[k], (rows, cols)), shape=(n, n)).tocsc()
            out[k] = scaled_solve(a, b)
        return out

    def transfer(self, f, out: str, ref: Optional[str] = None) -> np.ndarray:
        """H(f) = (v(out) − v(ref)) per volt of the input, at each frequency."""
        return self.solve(f) @ self.output(out, ref)

    def step(self, out: str, t, ref: Optional[str] = None) -> np.ndarray:
        """The step response v(out) − v(ref) at the times t (s, from 0, increasing; the steps may differ) for a 1 V
        step of the input at t = 0, everything at rest before: the descriptor system C·x' + G·x = b·u integrated by
        the second-order backward differentiation formula (Gear's BDF2, what SPICE uses for stiff circuits
        [Gear 1971]; its first step backward Euler). With h = t_{n+1} − t_n and ω = h / (t_n − t_{n−1}) the
        variable-step BDF2 [Hairer 1993, §III.5]

            ((1 + 2ω)/(1 + ω)·C/h + G)·x_{n+1} = b + C·((1 + ω)·x_n − ω²/(1 + ω)·x_{n−1})/h,

        (3C/2h + G)·x_{n+1} = b + C·(4x_n − x_{n−1})/2h on a uniform grid; one sparse factorisation per distinct
        (h, ω), so a grid of uniform stretches (simulate._step_of's, each twice the last one's step) needs a few.
        BDF2 is A-stable and damps the circuit's fast modes (a track's GHz resonances beside a filter's kHz poles)
        instead of ringing on them, and handles C's singular rows (the circuit's algebraic equations); it is
        zero-stable while the step ratios stay below 1 + √2 [Hairer 1993, §III.5] (a grid that grows faster loses
        accuracy). Steps equal to 1e-9 count as uniform. t must start at 0 and increase (else a ValueError: only
        t[1] − t[0] was read before, audit M-7). Refuses a circuit with two-ports (frequency-domain only: use its
        lumped π sections)."""
        if self._y2:
            raise ValueError("a circuit with frequency-dependent two-ports has no time-domain model: use lumped=True")
        import scipy.sparse.linalg as spl
        g, c, b = self._sparse()
        b = b.real
        t = np.asarray(t, float)
        if len(t) < 3:
            raise ValueError("a step response wants at least three times")
        hs = np.diff(t)
        if t[0] != 0.0 or not np.all(np.isfinite(t)) or np.any(hs <= 0):
            raise ValueError("a step response's times must start at 0 and increase")
        cv = self.output(out, ref)
        n = self.size
        x0 = np.zeros(n)
        h = float(hs[0])
        x1 = spl.splu((c / h + g).tocsc()).solve(b + c @ x0 / h)    # the first step: backward Euler
        y = np.empty(len(t))
        y[0], y[1] = cv @ x0, cv @ x1
        xm, xn = x0, x1
        key, lu, h_prev = None, None, h
        for k in range(2, len(t)):
            h = float(hs[k - 1])
            if abs(h - h_prev) <= 1e-9 * h_prev:
                h = h_prev                        # a uniform stretch (linspace's last-digit noise): one factorisation
            w = h / h_prev
            if key != (h, w):                     # a new step or ratio: factorise its matrix
                key = (h, w)
                lu = spl.splu(((1 + 2 * w) / (1 + w) * c / h + g).tocsc())
            xp = lu.solve(b + c @ ((1 + w) * xn - (w * w / (1 + w)) * xm) / h)
            y[k] = cv @ xp
            xm, xn, h_prev = xn, xp, h
        return y

    def loop_gain(self, name: str, f) -> np.ndarray:
        """The loop gain T(f) around op-amp channel `name` (opamp): its loop broken at its internal output, which
        is an ideal source (the buffer before R_O), so driving it with 1 V instead loads nothing and the break is
        exact [Middlebrook 1975]; with the input source at 0 V, what comes back across its inputs is
        d = v₊ − v₋ = −β·1 V, and T = −A(s)·d (Bode's return ratio −v_r/v_t [Bode 1945]: checked against A(s)·β by hand,
        audit round 7, to 1e-10), A(s) = A0 / ((1 + s/ω1)(1 + s/ω2)) its open-loop gain. Every other
        op-amp keeps working (it is part of this loop's β). Positive and real at low frequency for negative
        feedback; the phase margin is 180° + ∠T where |T| crosses 1 (transfer.phase_margin)."""
        import scipy.sparse.linalg as spl
        g, c, row, ip, im, prm = self._broken_loop(name)
        rhs = np.zeros(self.size, complex)
        rhs[row] = 1.0                            # the test source; the input source's row stays at 0 V
        f = np.atleast_1d(np.asarray(f, float))
        rows, cols, vals = self._y2_stamps(f)
        import scipy.sparse as sp
        out = np.empty(len(f), complex)
        for k, fk in enumerate(f):
            s_ = 2j * math.pi * fk
            a = g + s_ * c
            if rows:
                a = a + sp.coo_matrix((vals[k], (rows, cols)), shape=a.shape).tocsc()
            x = scaled_solve(a, rhs)
            d = (x[ip] if ip >= 0 else 0.0) - (x[im] if im >= 0 else 0.0)
            w1 = 2 * math.pi * prm.gbw / prm.a0
            a_ol = prm.a0 / (1 + s_ / w1) / (1 + s_ / (2 * math.pi * prm.fp2) if prm.fp2 else 1.0)
            out[k] = -a_ol * d
        return out

    def _broken_loop(self, name: str) -> tuple:
        """Op-amp channel `name`'s loop broken at its internal output (loop_gain): (G with the buffer's row made the
        test source's v(oi) = 1 V, C, that row, its + and − input node indices, its OpAmpParams)."""
        op = self.opamps[name]
        g, c, _ = self._sparse()
        row = len(self.nodes) + op["buf"]
        g = g.tolil()
        g[row, :] = 0                            # the buffer's row becomes v(oi) = 1 V: its control dropped
        g[row, self.node(op["oi"])] = 1.0
        return g.tocsc(), c, row, self.node(op["inp"]), self.node(op["inn"]), op["prm"]

    def loop_roots(self, name: str) -> tuple[np.ndarray, np.ndarray]:
        """The poles and zeros (rad/s) of op-amp channel `name`'s loop gain T(s) = −A(s)·d(s) (loop_gain): d's, the
        broken circuit's response from the test source to v₊ − v₋, as poles_zeros finds them [Rosenbrock 1970]
        [Golub 2013] (its unobservable modes cancelled, _cancel), and A(s)'s poles −ω1, −ω2. transfer.phase_margin
        samples the sweep densely around the lightly damped ones (a narrow |T| = 1 excursion between two sweep
        points, audit M-3).
        Raises ValueError for a circuit with two-ports (not rational: use the lumped circuit's)."""
        if self._y2:
            raise ValueError("a circuit with frequency-dependent two-ports (lines) has no exact poles")
        g, c, row, ip, im, prm = self._broken_loop(name)
        b = np.zeros(self.size)
        b[row] = 1.0
        cv = np.zeros(self.size)
        for node, sign in ((ip, 1.0), (im, -1.0)):
            if node >= 0:
                cv[node] += sign
        # d's own roots, its unseen modes cancelled (the op-amp's gain stages, which nothing reads once the loop is
        # broken, are each a pole and a zero at the same place), then A(s)'s poles
        poles, zeros = _cancel(*_pencil_roots(g.toarray(), c.toarray(), b, cv))
        own = [-2 * math.pi * prm.gbw / prm.a0] + ([-2 * math.pi * prm.fp2] if prm.fp2 else [])
        return np.concatenate([poles, np.array(own, complex)]), zeros

    def voltage(self, f, node: str) -> np.ndarray:
        """A node's voltage per volt of the input."""
        return self.transfer(f, node)

    def poles_zeros(self, out: str, ref: Optional[str] = None) -> tuple[np.ndarray, np.ndarray, complex]:
        """(poles, zeros, k) in rad/s, exact: the poles the finite generalised eigenvalues of (G, −C), the zeros those
        of the bordered system matrix [Rosenbrock 1970] [Golub 2013] (see the module docstring), in the normal form
        H(s) = k · s^m · Π(1 − s/z) / Π(1 − s/p) over the roots away from the origin, m the zeros at the origin less
        the poles there (roots at the origin are returned as 0): k is the gain at DC where m = 0. k from H at a
        test point away from every root, its products summed as logarithms (hundreds of roots stay finite). Raises
        ValueError for a circuit with two-ports (not rational).

        The poles depend on the circuit alone: they are found once and kept for its other outputs (audit M-13). An
        output the input cannot reach (no path between them through the circuit's elements) has H ≡ 0: no poles, no
        zeros, k = 0 (its bordered pencil is singular at every s, and its eigenvalues were noise: audit M-6). Roots
        below ORIGIN·2π·F_REF are put at the origin and the QZ keeps none above INFINITE·2π·F_REF: fixed bounds,
        [A82]."""
        if self._y2:
            raise ValueError("a circuit with frequency-dependent two-ports (lines) has no exact poles: fit its sweep")
        import scipy.linalg as sl
        g, c, b = self.matrices()
        cv = self.output(out, ref)
        if not _reaches(g, c, b, cv):
            return np.zeros(0, complex), np.zeros(0, complex), 0j
        w0 = 2 * math.pi * F_REF
        key = (len(self._g), len(self._c), self.size)       # elements are only ever added: their count is the state
        if key not in self._pole_cache:
            self._pole_cache = {key: _finite_eigs(g, -c * w0) * w0}
        poles, zeros = _pencil_roots(g, c, b, cv, poles=self._pole_cache[key])
        poles, zeros = _cancel(poles, zeros)
        origin = ORIGIN * w0                      # a root this close to 0 is at the origin (numerical residue)
        poles = np.where(np.abs(poles) < origin, 0.0, poles)
        zeros = np.where(np.abs(zeros) < origin, 0.0, zeros)
        # the constant k: H at a test point s0 over the normal form's factors there, as a sum of logarithms; s0 away
        # from every root (the first of three candidates whose nearest root is farthest, relative)
        pz, zz = poles[poles != 0], zeros[zeros != 0]
        roots = np.concatenate([pz, zz])

        def clearance(s):
            """How far s lies from the nearest root, relative to |s|."""
            return float(np.min(np.abs(roots - s)) / abs(s)) if len(roots) else 1.0
        tries = (w0 * complex(0.3, 0.7071), w0 * complex(0.21, 1.93), w0 * complex(0.11, 0.37))
        s0 = next((s for s in tries if clearance(s) >= 1e-2), max(tries, key=clearance))
        h = complex(cv @ sl.solve(g + s0 * c, b.astype(complex)))
        m = int(np.sum(zeros == 0) - np.sum(poles == 0))
        if h == 0:                                 # reached, yet zero at s0 to the last digit: k is lost
            return poles, zeros, 0j
        log_k = (np.log(h) - m * np.log(s0) + np.sum(np.log(1 - s0 / pz)) - np.sum(np.log(1 - s0 / zz)))
        return poles, zeros, complex(np.exp(log_k))


def scaled_solve(a, b: np.ndarray) -> np.ndarray:
    """x of a·x = b (a sparse) by a sparse LU of a with its rows, then its columns, scaled to a largest entry of 1
    [Golub 2013, §3.5.2]: Dr·a·Dc·y = Dr·b, x = Dc·y. A circuit's matrix mixes 1/(1 mΩ) beside 1/(1 TΩ) and ωC of
    femtofarads beside ωL of nanohenries: unscaled, LU pivoted on the size of entries and lost the small ones' digits
    (audit 2026-11, M-11: 1e-12 Ω and 1e12 Ω on one node, 3e-2 off)."""
    import scipy.sparse as sp
    import scipy.sparse.linalg as spl
    a = sp.csr_matrix(a)
    r = np.asarray(abs(a).max(axis=1).todense()).ravel()
    dr = sp.diags(1.0 / np.where(r > 0, r, 1.0))
    a = (dr @ a).tocsc()
    col = np.asarray(abs(a).max(axis=0).todense()).ravel()
    dc = 1.0 / np.where(col > 0, col, 1.0)
    y = spl.splu((a @ sp.diags(dc)).tocsc()).solve(dr @ np.asarray(b, complex))
    return dc * y


def _reaches(g: np.ndarray, c: np.ndarray, b: np.ndarray, cv: np.ndarray) -> bool:
    """Whether the output cv can see the input b at all: some unknown it reads lies in the same connected part of
    the circuit's graph (the pattern of |G| + |C|, both ways) as the input's row. Not reached, H(s) ≡ 0 for every s."""
    import scipy.sparse as sp
    from scipy.sparse.csgraph import connected_components
    pattern = sp.csr_matrix((np.abs(g) + np.abs(c)) > 0)
    _, label = connected_components(pattern, directed=False)
    return bool(set(label[np.nonzero(cv)[0]]) & set(label[np.nonzero(b)[0]]))


def _pencil_roots(g: np.ndarray, c: np.ndarray, b: np.ndarray, cv: np.ndarray,
                  poles: Optional[np.ndarray] = None) -> tuple[np.ndarray, np.ndarray]:
    """(poles, zeros) in rad/s of cvᵀ(G + sC)⁻¹b: the finite generalised eigenvalues of (G, −C), and those of the
    bordered system matrix [[G, −b], [cvᵀ, 0]] against [[C, 0], [0, 0]] (the transmission zeros [Rosenbrock 1970]),
    s scaled by 2π·F_REF [Golub 2013]. `poles`: already known (the same circuit, another output)."""
    w0 = 2 * math.pi * F_REF
    if poles is None:
        poles = _finite_eigs(g, -c * w0) * w0
    n = len(b)
    gz = np.zeros((n + 1, n + 1))
    cz = np.zeros((n + 1, n + 1))
    gz[:n, :n], gz[:n, n], gz[n, :n] = g, -b, cv
    cz[:n, :n] = c
    return np.asarray(poles, complex), _finite_eigs(gz, -cz * w0) * w0


def _finite_eigs(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """The finite generalised eigenvalues λ of a·x = λ·b·x by the QZ algorithm (the pencil's infinite ones, b
    singular, left out), the pencil equilibrated first (rows and columns scaled to comparable norms: eigenvalues
    unchanged) [Golub 2013]."""
    import scipy.linalg as sl
    m = np.abs(a) + np.abs(b)
    r = 1.0 / np.maximum(m.max(axis=1), 1e-300)
    a, b = a * r[:, None], b * r[:, None]
    m = np.abs(a) + np.abs(b)
    cs = 1.0 / np.maximum(m.max(axis=0), 1e-300)
    a, b = a * cs[None, :], b * cs[None, :]
    al, be = sl.eig(a, b, right=False, homogeneous_eigvals=True)
    keep = np.abs(be) > np.maximum(np.abs(al), 1e-300) / INFINITE          # |λ| beyond INFINITE: infinite [A82]
    lam = al[keep] / be[keep]
    return lam[np.isfinite(lam) & (np.abs(lam) < 1e12)]


def _cancel(poles: np.ndarray, zeros: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Poles and zeros with the coinciding pairs removed: a mode the input does not reach or the output does not
    see (an uncontrollable or unobservable mode cancels in the transfer function [Rosenbrock 1970]). Each zero
    against its nearest pole, a pair cancelling within CANCEL·max(|Re p|, CANCEL_FLOOR·|p|) (|p| for a pole at the
    origin's scale): relative to the pole's distance from the jω axis, as simulate.cancel_doublets, since a pair that
    far apart changes |H| by at most that fraction on the axis ([A54]; audit M-4: within 1e-6·|p| a Q 1e6 pole
    pair cancelled a zero pair 9e-7·|p| away and |H| at the resonance moved 6 dB). CANCEL_FLOOR·|p| keeps the
    eigenvalues' own error (~1e-13·|p|) inside the tolerance for every Q below ~1e8."""
    p, z = list(poles), list(zeros)
    for zi in list(z):
        if not p:
            break
        pi = min(p, key=lambda q: abs(q - zi))
        scale = max(abs(pi.real), CANCEL_FLOOR * abs(pi), 1e-9) if abs(pi) > 1e-9 else max(abs(zi), 1e-9)
        if abs(pi - zi) <= CANCEL * scale:
            p.remove(pi)
            z.remove(zi)
    return np.array(p, complex), np.array(z, complex)


def line_y(zc: Callable, gamma_l: Callable) -> Callable[[np.ndarray], np.ndarray]:
    """A transmission line's admittance matrix Y(f) = [[coth γl, −csch γl], [−csch γl, coth γl]] / Zc [Pozar 2012]
    from its Zc(f) and γl(f) (emi/path.PathLine.zc / gamma_l), for an array of frequencies: (n, 2, 2). The function
    also carries the line's π equivalent as `.pi(f)` → (Zc·sinh γl, tanh(γl/2)/Zc): the series impedance and each
    end's shunt admittance, coth − csch = tanh(γl/2) without the cancellation of the two ~1/γl terms at a short
    line (audit M-12); Circuit.y2 stamps a line by it."""
    def parts(f) -> tuple:
        """(f, Zc, γl) at the frequencies f, γl kept off 0."""
        f = np.atleast_1d(np.asarray(f, float))
        z = np.asarray(zc(f), complex) * np.ones(len(f))
        gl = np.asarray(gamma_l(f), complex) * np.ones(len(f))
        return f, z, np.where(np.abs(gl) > 1e-15, gl, 1e-15)

    def y(f) -> np.ndarray:
        """Y at the frequencies f, shape (len(f), 2, 2)."""
        f, z, gl = parts(f)
        csch = 1.0 / np.sinh(gl)
        # coth x = csch x + tanh(x/2) for a short line: the shunt part kept exact; 1/tanh x otherwise, as before
        coth = np.where(np.abs(gl) < PI_GL, csch + np.tanh(gl / 2), 1.0 / np.tanh(gl))
        return np.stack([np.stack([coth, -csch], -1), np.stack([-csch, coth], -1)], -2) / z[:, None, None]

    def pi(f) -> tuple:
        """The π equivalent at the frequencies f: (series impedance Zc·sinh γl, shunt admittance tanh(γl/2)/Zc)."""
        f, z, gl = parts(f)
        return z * np.sinh(gl), np.tanh(gl / 2) / z
    y.pi = pi
    return y
