"""The transfer function in part names (analysis/symbolic.py): the ideal circuit's H(s) against textbook closed forms
[Franco 2015], and its values substituted against the numeric MNA solve of the same circuit (a near-ideal op-amp).
The user: "i want the transfer function to be shown in the results as well", then "add sympy"."""
import math

import numpy as np
import pytest
import sympy as sp

from groundtruth.analysis.symbolic import MAX_PARTS, symbolic_transfer
from groundtruth.emi.mna import Circuit, OpAmpParams

s = sp.Symbol("s")
R1, R2, R3, C1, C2 = sp.symbols("R1 R2 R3 C1 C2")


def _same(tf, closed) -> bool:
    """The symbolic result equals a closed form (their difference simplifies to 0)."""
    return sp.simplify(tf.expr - closed) == 0


SK = {"parts": [("R1", "resistor", "IN", "A", {"R": 1e4}), ("R2", "resistor", "A", "B", {"R": 1e4}),
                ("C1", "cap", "A", "OUT", {"C": 2e-8}), ("C2", "cap", "B", "0", {"C": 1e-8})],
      "opamps": [("U1", "B", "OUT", "OUT", None)], "in": "IN", "out": "OUT"}


def test_a_sallen_key_low_pass_in_its_part_names():
    """Unity-gain Sallen-Key: 1 / (R1·R2·C1·C2·s² + C2·(R1 + R2)·s + 1), written highest power first."""
    tf = symbolic_transfer(SK)
    assert _same(tf, 1 / (R1 * R2 * C1 * C2 * s ** 2 + C2 * (R1 + R2) * s + 1))
    assert tf.text == "H(s) = 1 / (C1·C2·R1·R2·s² + C2·(R1 + R2)·s + 1)"
    assert tf.latex.startswith(r"H(s) = \frac{1}{C_{1} C_{2} R_{1} R_{2} s^{2}") and "<math" in tf.mathml


def test_an_inverting_stage_and_an_mfb_low_pass():
    """Inverting with C2 across R2: −(R2/R1) / (1 + s·R2·C2); MFB low-pass (R1 in → a, R2 a → out, R3 a → b, C1 a →
    ground, C2 b → out): −(R2/R1) / (1 + s·C2·(R2 + R3 + R2·R3/R1) + s²·R2·R3·C1·C2)."""
    inv = {"parts": [("R1", "resistor", "IN", "N", {"R": 1e3}), ("R2", "resistor", "N", "OUT", {"R": 1e4}),
                     ("C2", "cap", "N", "OUT", {"C": 1e-9})],
           "opamps": [("U1", "0", "N", "OUT", None)], "in": "IN", "out": "OUT"}
    assert _same(symbolic_transfer(inv), -(R2 / R1) / (1 + s * R2 * C2))
    mfb = {"parts": [("R1", "resistor", "IN", "A", {"R": 1e4}), ("R2", "resistor", "A", "OUT", {"R": 2e4}),
                     ("R3", "resistor", "A", "B", {"R": 1.5e4}), ("C1", "cap", "A", "0", {"C": 47e-9}),
                     ("C2", "cap", "B", "OUT", {"C": 2.2e-9})],
           "opamps": [("U1", "0", "B", "OUT", None)], "in": "IN", "out": "OUT"}
    closed = -(R2 / R1) / (1 + s * C2 * (R2 + R3 + R2 * R3 / R1) + s ** 2 * R2 * R3 * C1 * C2)
    assert _same(symbolic_transfer(mfb), closed)


def test_its_values_substituted_match_the_numeric_solve():
    """The Sallen-Key's symbolic H with its values against the MNA solve of the same circuit with a near-ideal op-amp
    (A0 1e9, GBW 1 THz): 1e-6 from 1 Hz to 1 MHz."""
    tf = symbolic_transfer(SK)
    ckt = Circuit()
    ckt.vsource("IN", "0")
    ckt.r("IN", "A", 1e4)
    ckt.r("A", "B", 1e4)
    ckt.c("A", "OUT", 2e-8)
    ckt.c("B", "0", 1e-8)
    ckt.opamp("U1", "B", "OUT", "OUT", OpAmpParams(gbw=1e12, a0=1e9, r_out=1e-3, c_in=0.0))
    f = np.geomspace(1, 1e6, 40)
    assert np.allclose(tf.evaluate(f), ckt.transfer(f, "OUT"), rtol=1e-6, atol=1e-12)


def test_a_gain_block_and_too_many_parts():
    """A part with a Gain field is a VCVS of that gain (H = 3·R2/(R1 + R2) behind a divider); a circuit with more
    than MAX_PARTS parts is not solved symbolically (None)."""
    blk = {"parts": [("R1", "resistor", "IN", "A", {"R": 1e3}), ("R2", "resistor", "A", "0", {"R": 1e3})],
           "opamps": [("U1", "A", None, "OUT", 3.0)], "in": "IN", "out": "OUT"}
    assert _same(symbolic_transfer(blk), 3 * R2 / (R1 + R2))
    many = {"parts": [(f"R{i}", "resistor", "IN" if i == 0 else f"n{i}", f"n{i + 1}", {"R": 1e3})
                      for i in range(MAX_PARTS + 1)], "opamps": [], "in": "IN", "out": f"n{MAX_PARTS + 1}"}
    assert symbolic_transfer(many) is None
