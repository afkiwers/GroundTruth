"""Reference transfer functions for audit round 7 (tools/dev/analog_audit.py), from Lcapy's symbolic nodal analysis,
independent of GroundTruth (it does not import it). Runs in a venv of its own with Lcapy (pip install lcapy):

    %TEMP%\\auditvenv\\Scripts\\python tools/dev/lcapy_reference.py OUT.json

Each benchmark circuit is a SPICE-like netlist; H(s) = V(out)/V(in) is Lcapy's (sympy), evaluated at the shared
frequencies. The op-amp is the same small-signal macromodel GroundTruth uses (A0, a pole at GBW/A0, an optional second
pole, R_O), built here from voltage-controlled voltage sources and RC stages: the comparison checks the solver, not
the model.
"""
import json
import math
import sys

import numpy as np
import sympy

F = list(np.geomspace(1.0, 1e9, 61))


def opamp(name, inp, inn, out, a0, gbw, fp2=None, r_out=1.0):
    """The macromodel as netlist lines: E(a0) into R-C (pole gbw/a0), an optional unity stage with pole fp2, a unity
    buffer, R_O to the output."""
    f1 = gbw / a0
    lines = [f"E{name}1 {name}x1 0 {inp} {inn} {a0}", f"R{name}1 {name}x1 {name}n1 1",
             f"C{name}1 {name}n1 0 {1 / (2 * math.pi * f1)}"]
    last = f"{name}n1"
    if fp2:
        lines += [f"E{name}2 {name}x2 0 {last} 0 1", f"R{name}2 {name}x2 {name}n2 1",
                  f"C{name}2 {name}n2 0 {1 / (2 * math.pi * fp2)}"]
        last = f"{name}n2"
    lines += [f"E{name}b {name}oi 0 {last} 0 1", f"R{name}o {name}oi {out} {r_out}"]
    return lines


CIRCUITS = {
    "rc_lowpass": ["R1 in out 1e3", "C1 out 0 10e-9"],
    "rlc_series_q2": ["R1 in a 15.811388", "L1 a out 1e-3", "C1 out 0 1e-6"],
    "inverting_x10": ["R1 in inn 1e3", "R2 inn out 10e3"] + opamp("U", "0", "inn", "out", 1e5, 1e6, 20e6, 50.0),
    "noninv_x2_cload": ["R1 inn 0 10e3", "R2 inn out 10e3", "CL out 0 1e-9"]
                       + opamp("U", "in", "inn", "out", 3.16e5, 1e6, 2e6, 100.0),
    "sallen_key": ["R1 in a 10e3", "R2 a b 10e3", "C1 a out 20e-9", "C2 b 0 10e-9"]
                  + opamp("U", "b", "out", "out", 1e5, 10e6),
    "mfb_bandpass": ["R1 in a 15.9e3", "C1 a inn 10e-9", "C2 a out 10e-9", "R2 inn out 318e3", "R3 a 0 1.6e3"]
                    + opamp("U", "0", "inn", "out", 1e6, 50e6),
    "wide_values": ["R1 in a 1e6", "C1 a 0 1e-12", "R2 a b 1e-1", "L1 b out 1e-9", "C2 out 0 1e-3", "R3 out 0 1e7"],
}


def main(path):
    """Write {name: {"f": [...], "re": [...], "im": [...]}} for every benchmark."""
    import lcapy
    s = sympy.Symbol("s")
    out = {}
    for name, lines in CIRCUITS.items():
        cct = lcapy.Circuit("\n".join(lines))
        h = cct.transfer("in", 0, "out", 0)
        expr = sympy.sympify(str(h.sympy).replace("I", "I"))
        sym = list(expr.free_symbols)
        expr = expr.subs(sym[0], s) if sym else expr
        # exact rational coefficients evaluated in 30-digit arithmetic (a float evaluation of polynomials whose
        # coefficients span 1e-30 … 1e10, as wide_values's, loses its digits to cancellation)
        expr = sympy.nsimplify(expr, rational=True)
        vals = [complex(expr.evalf(30, subs={s: sympy.I * 2 * sympy.pi * sympy.Float(f, 30)})) for f in F]
        # the poles and zeros: the roots of Lcapy's own numerator and denominator polynomials in s (rad/s), found in
        # 30-digit arithmetic (sympy nroots)
        num, den = sympy.fraction(sympy.together(expr))
        roots = lambda p: ([complex(r) for r in sympy.Poly(sympy.expand(p), s).nroots(n=30, maxsteps=500)]  # noqa
                           if sympy.Poly(p, s).degree() > 0 else [])
        out[name] = {"f": F, "re": [v.real for v in vals], "im": [v.imag for v in vals],
                     "poles": [[r.real, r.imag] for r in roots(den)], "zeros": [[r.real, r.imag] for r in roots(num)]}
        print(name, "ok", len(sym), "symbol(s)")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(out, fh)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
