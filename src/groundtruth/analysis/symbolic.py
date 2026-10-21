"""An op-amp circuit's transfer function in its part names: H(s) of the ideal circuit, solved symbolically (SymPy).
The user: "i want the transfer function to be shown in the results as well", then "add sympy".

The circuit [A54] is analysis/analog_net's schematic in net terms (its parts, op-amps, input and output nets; AC ground
"0"), idealised: resistors R, capacitors 1/(sC), inductors and ferrites sL (no ESR, ESL, DCR, C_bias: the design
values' symbols), a 0R a wire; each op-amp with its feedback an ideal one, a nullor (v₊ = v₋, its output whatever
the circuit needs) [Franco 2015], a gain block (a part with a Gain field) a VCVS of its gain; the input an ideal
voltage source on the driver's net (the source's resistance and the receivers' load left out: the circuit as
designed). Its modified nodal equations [Ho 1975] are solved in s by fraction-free elimination, and H(s) = N(s)/D(s)
is put in the form a designer reads: N and D polynomials in s, D's constant term 1 where it has one (then N's is
the DC gain), e.g. a Sallen-Key low-pass 1 / (C1·C2·R1·R2·s² + C2·(R1 + R2)·s + 1).

Up to MAX_PARTS parts (a symbolic solve grows fast beyond); the numeric transfer function (exact poles and zeros of
the full circuit, transfer.normal_form) is shown in any case. Checked in tests/test_symbolic.py: the closed forms
of an inverting amplifier, a Sallen-Key and an MFB low-pass, and its values substituted against the numeric
solve."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

MAX_PARTS = 15                 # parts in a circuit solved symbolically at most


@dataclass
class SymbolicTF:
    """H(s) in part names: `text` (plain, for the readout and the console), `latex` (matplotlib's math text and the
    report), `mathml` (the HTML report), the expression and its values; `notes`."""
    text: str
    latex: str
    mathml: str
    expr: object = None
    values: dict = field(default_factory=dict)
    notes: list = field(default_factory=list)

    def evaluate(self, f) -> "object":
        """H at frequencies f (Hz) with the parts' values (a check against the numeric solve)."""
        import numpy as np
        import sympy as sp
        s = sp.Symbol("s")
        fn = sp.lambdify(s, self.expr.subs(self.values), "numpy")
        f = np.asarray(f, float)
        return np.asarray(fn(2j * np.pi * f), complex) * np.ones(len(f))


def symbolic_transfer(schematic: dict) -> Optional[SymbolicTF]:
    """The ideal circuit's H(s) = v(out) / v(in) in its part names [A54]: its modified nodal equations [Ho 1975]
    with each op-amp a nullor [Franco 2015], solved in s (see the module docstring); None (with nothing to say)
    without a schematic, or when it has more than MAX_PARTS parts or the solve finds no unique answer."""
    import sympy as sp
    if not schematic or not schematic.get("parts") and not schematic.get("opamps"):
        return None
    parts = [p for p in schematic["parts"] if p[1] != "open"]
    if len(parts) > MAX_PARTS:
        return None
    s = sp.Symbol("s")
    # nets joined by a 0R or a short are one node
    alias = {}

    def root(n):
        """A net's node after the wires."""
        while n in alias:
            n = alias[n]
        return n
    for ref, kind, a, b, _ in parts:
        if kind == "short" and root(a) != root(b):
            ra, rb = root(a), root(b)
            if rb == "0":
                ra, rb = rb, ra
            alias[rb] = ra
    nodes: dict[str, int] = {}

    def idx(n) -> int:
        """The unknown of a node (−1: ground)."""
        n = root(n)
        if n == "0":
            return -1
        if n not in nodes:
            nodes[n] = len(nodes)
        return nodes[n]
    stamps, extras, values = [], [], {}
    for ref, kind, a, b, vals in parts:
        if kind == "short":
            continue
        sym = sp.Symbol(_name(ref))
        if kind == "resistor":
            y, values[sym] = 1 / sym, vals.get("R")
        elif kind == "cap":
            y, values[sym] = s * sym, vals.get("C")
        elif kind in ("inductor", "ferrite"):
            y, values[sym] = 1 / (s * sym), vals.get("L")
        else:
            continue
        stamps.append((idx(a), idx(b), y))
    for n in (schematic["in"], schematic["out"]):
        idx(n)
    for name, inp, inn, out, gain in schematic.get("opamps", []):
        idx(inp), idx(out)
        if inn is not None:
            idx(inn)
    n = len(nodes)
    ops = schematic.get("opamps", [])
    size = n + 1 + len(ops)                       # node voltages, the input source's current, each op-amp's output
    A = sp.zeros(size, size)
    rhs = sp.zeros(size, 1)
    for a, b, y in stamps:
        for i, si in ((a, 1), (b, -1)):
            for j, sj in ((a, 1), (b, -1)):
                if i >= 0 and j >= 0:
                    A[i, j] += si * sj * y
    # the input: v(in) = 1, its current an unknown (row n)
    k = n
    i_in = idx(schematic["in"])
    if i_in < 0:
        return None
    A[i_in, k] += 1
    A[k, i_in] += 1
    rhs[k] = 1
    for m, (name, inp, inn, out, gain) in enumerate(ops):
        k = n + 1 + m
        o = idx(out)
        if o >= 0:
            A[o, k] += 1                          # the op-amp drives its output with this current
        if gain is None:                          # a nullor: v(in+) − v(in−) = 0
            for node, sg in ((idx(inp), 1), (idx(inn), -1)):
                if node >= 0:
                    A[k, node] += sg
        else:                                     # a gain block: v(out) − gain·v(in+) = 0
            g = sp.nsimplify(gain)
            if o >= 0:
                A[k, o] += 1
            if idx(inp) >= 0:
                A[k, idx(inp)] -= g
    try:
        x = A.LUsolve(rhs) if A.det(method="berkowitz") != 0 else None
    except (ValueError, ZeroDivisionError, NotImplementedError):
        x = None
    if x is None:
        return None
    o = idx(schematic["out"])
    h = sp.Integer(0) if o < 0 else x[o]
    h = sp.cancel(sp.together(h))
    num, den = sp.fraction(h)
    num, den = sp.expand(num), sp.expand(den)
    c0 = sp.Poly(den, s).coeff_monomial(1)
    if c0 != 0:                                   # D(0) = 1: N(0) is then the DC gain
        num, den = sp.expand(num / c0), sp.expand(den / c0)
    expr = num / den
    nt, nl = _poly(num, s)
    dt, dl = _poly(den, s)
    wrap = lambda t: t if all(c not in t for c in " +") or t.startswith("(") else f"({t})"       # noqa: E731
    text = "H(s) = " + (nt if den == 1 else f"{wrap(nt)} / ({dt})")
    latex = "H(s) = " + (nl if den == 1 else rf"\frac{{{nl}}}{{{dl}}}")
    # presentation MathML for the HTML report (browsers render it natively), wrapped in its <math> element
    mathml = ('<math xmlns="http://www.w3.org/1998/Math/MathML" display="block">'
              + sp.mathml(sp.Eq(sp.Symbol("H(s)"), expr), printer="presentation") + "</math>")
    return SymbolicTF(text, latex, mathml, expr, {k_: v for k_, v in values.items() if v is not None})


def _poly(expr, s) -> tuple[str, str]:
    """A polynomial in s as (plain text, LaTeX): its terms from the highest power of s down, each coefficient
    factored (C2·(R1 + R2)·s, not s·(C2·R1 + C2·R2))."""
    import sympy as sp
    p = sp.Poly(expr, s)
    texts, latexes = [], []
    for (k,), c in sorted(p.terms(), key=lambda t: -t[0][0]):
        c = sp.factor(c)
        sk = "" if k == 0 else ("s" if k == 1 else f"s{'²' if k == 2 else '³' if k == 3 else f'^{k}'}")
        lk = "" if k == 0 else ("s" if k == 1 else f"s^{{{k}}}")
        neg = c.could_extract_minus_sign()
        a = -c if neg else c
        ct = "" if a == 1 and k else _plain(a)
        cl = "" if a == 1 and k else sp.latex(a)
        if ct and isinstance(a, sp.Add):
            ct, cl = f"({ct})", rf"\left({cl}\right)"
        term_t = "·".join(x for x in (ct, sk) if x) or "1"
        term_l = " ".join(x for x in (cl, lk) if x) or "1"
        sign = "−" if neg else "+"
        texts.append((sign, term_t))
        latexes.append(("-" if neg else "+", term_l))
    t = "".join((f"{sg} " if i else ("−" if sg == "−" else "")) + term + " "
                for i, (sg, term) in enumerate(texts)).strip()
    l_ = "".join((f"{sg} " if i else ("-" if sg == "-" else "")) + term + " "
                 for i, (sg, term) in enumerate(latexes)).strip()
    return t, l_


def _name(ref: str) -> str:
    """A part's reference as a SymPy symbol name (R303 stays R303; other characters become _)."""
    return "".join(c if c.isalnum() else "_" for c in ref)


def _plain(expr) -> str:
    """An expression as plain text with · for products and ² / ³ for small powers."""
    import sympy as sp
    t = sp.sstr(expr).replace("**2", "²").replace("**3", "³").replace("*", "·")
    return t
