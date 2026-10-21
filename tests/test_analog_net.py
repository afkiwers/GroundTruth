"""The circuit a signal path runs through, read from the board (analysis/analog_net.py) and solved by MNA: op-amp
circuits on generated boards against their closed forms, the op-amps' pin roles, AC ground. The user: "I want a
generic model that can calculate any circuit made from an OPA"."""
import math

import numpy as np
import pytest

from boardgen import opamp_circuit_board
from conftest import make
from groundtruth.components.models import ComponentModel
from groundtruth.config.settings import NetSpec

TAU = 2 * math.pi


def _setup(tmp_path, parts, opamps, gbw=100e6, name="amp", **amp):
    """A generated circuit board with its path SIG_IN (U1.1) → OUT (U2.1), each op-amp an `amplifier` model."""
    from groundtruth.analysis.analog_net import analog_network
    spec = NetSpec("SIG_IN", "U1.1", ["U2.1"], 1e3, 1e-6)
    board, sc, _ = make(tmp_path, opamp_circuit_board(parts, opamps), name, nets=(spec,))
    cfg = sc.config
    for ref, _ in opamps:
        cfg.components[ref].model = ComponentModel("amplifier", {"gbw": gbw, **amp})
    return board, cfg, spec, analog_network


def _f0q(poles):
    """(f0 Hz, Q) of the lowest complex pole pair."""
    pairs = sorted((p for p in poles if p.imag > 1e-6 * abs(p)), key=abs)
    w0 = abs(pairs[0])
    return w0 / TAU, w0 / (-2 * pairs[0].real)


OPAMP = {3: ("+IN", "GND"), 2: ("-IN", "N"), 1: ("OUT", "OUT"), 5: ("V+", "+5V"), 4: ("V-", "GND")}


def test_an_inverting_amplifier_read_from_the_board(tmp_path):
    """R1 1 kΩ, R2 10 kΩ around U3 (+IN on GND): the gain −R2/(R_s + R1) with the driver's 25 Ω (0.2 %), the one pole
    at GBW·β, β = (R_s + R1)/(R_s + R1 + R2) (1 %, R_O and the pins' C in the circuit), the op-amp as the macromodel,
    the unrouted nets joined (a note)."""
    parts = [("R1", "1k", "R", "SIG_IN", "N"), ("R2", "10k", "R", "N", "OUT")]
    board, cfg, spec, analog_network = _setup(tmp_path, parts, [("U3", OPAMP)], gbw=1e6)
    net = analog_network(board, cfg, spec, "U2.1", lumped=True)
    assert net.opamps == [("U3", "macromodel")] and net.parts == ["R1", "R2"]
    assert any("joined as the netlist says" in n for n in net.notes)
    h = net.circuit.transfer([1.0], net.out_node)[0]
    assert h.real == pytest.approx(-10e3 / 1025.0, rel=2e-3)
    p, z, k = net.circuit.poles_zeros(net.out_node)
    beta = 1025.0 / 11025.0
    assert min(abs(x) for x in p) / TAU == pytest.approx(1e6 * beta, rel=0.01)
    assert k.real == pytest.approx(h.real, rel=1e-3)                    # the normal form's K: the DC gain


def test_a_sallen_key_low_pass_read_from_the_board(tmp_path):
    """Unity-gain Sallen-Key: R1 10 kΩ (+ the driver's 25 Ω), R2 10 kΩ, C1 20 nF to the output, C2 10 nF to GND, a fast
    op-amp: f0 = 1/(2π√(R1R2C1C2)) and Q = √(R1R2C1C2)/(C2(R1 + R2)) with R1 + 25 Ω (0.5 %) [Franco 2015]."""
    parts = [("R1", "10k", "R", "SIG_IN", "A"), ("R2", "10k", "R", "A", "B"), ("C1", "20n", "C", "A", "OUT"),
             ("C2", "10n", "C", "B", "GND")]
    pins = {3: ("+IN", "B"), 2: ("-IN", "OUT"), 1: ("OUT", "OUT"), 5: ("V+", "+5V"), 4: ("V-", "GND")}
    board, cfg, spec, analog_network = _setup(tmp_path, parts, [("U3", pins)])
    for ref in ("C1", "C2"):                       # ideal capacitors: the closed form has no ESR or ESL
        cfg.components[ref].model = ComponentModel("cap", {"C": 20e-9 if ref == "C1" else 10e-9, "ESR": 0.0,
                                                           "ESL": 0.0})
    net = analog_network(board, cfg, spec, "U2.1", lumped=True)
    r1, r2, c1, c2 = 10025.0, 10e3, 20e-9, 10e-9
    f0, q = _f0q(net.circuit.poles_zeros(net.out_node)[0])
    assert f0 == pytest.approx(1 / (TAU * math.sqrt(r1 * r2 * c1 * c2)), rel=5e-3)
    assert q == pytest.approx(math.sqrt(r1 * r2 * c1 * c2) / (c2 * (r1 + r2)), rel=5e-3)
    f = np.array([f0])
    assert abs(net.circuit.transfer(f, net.out_node)[0]) == pytest.approx(q, rel=0.01)     # |H(f0)| = Q


def test_a_gain_field_keeps_the_gain_block_and_the_lines_match_the_lumped_form(tmp_path):
    """The same inverting circuit with U3's Gain field: the gain block (its gain, the circuit around it not used,
    a note); tracks as lines (the default) and as π sections agree at low frequency."""
    parts = [("R1", "1k", "R", "SIG_IN", "N"), ("R2", "10k", "R", "N", "OUT")]
    board, cfg, spec, analog_network = _setup(tmp_path, parts, [("U3", OPAMP)], gbw=1e6, gain=-3.0)
    net = analog_network(board, cfg, spec, "U2.1")
    assert net.opamps == [("U3", "gain block")] and any("gain block" in n for n in net.notes)
    lumped = analog_network(board, cfg, spec, "U2.1", lumped=True)
    f = np.array([1.0, 1e3])
    assert np.allclose(net.circuit.transfer(f, net.out_node), lumped.circuit.transfer(f, lumped.out_node), rtol=1e-6)


def test_op_amp_pin_names_and_labels_give_each_channels_roles(tmp_path):
    """pin_role reads +IN, IN+, INP, +, -IN, IN-, INN, -, +INA, INB-, IN1+, OUT, VOUT, OUTA, V+, VCC (a supply); a dual
    op-amp is two channels (U3A, U3B); GT labels amp_in / amp_inv / amp_out name a part whose pins have no names."""
    from groundtruth.analysis.analog_net import opamp_channels, pin_role
    cases = {"+IN": ("inp", ""), "IN+": ("inp", ""), "INP": ("inp", ""), "+": ("inp", ""), "-IN": ("inn", ""),
             "IN-": ("inn", ""), "INN": ("inn", ""), "-": ("inn", ""), "+INA": ("inp", "A"), "INB-": ("inn", "B"),
             "IN1+": ("inp", "1"), "OUT": ("out", ""), "VOUT": ("out", ""), "OUTA": ("out", "A"),
             "V+": ("supply", ""), "VCC": ("supply", ""), "PA3": None}
    for name, want in cases.items():
        assert pin_role(name) == want, name
    dual = {3: ("+INA", "A1"), 2: ("-INA", "A2"), 1: ("OUTA", "A3"), 5: ("+INB", "B1"), 6: ("-INB", "B2"),
            7: ("OUTB", "B3"), 8: ("V+", "+5V"), 4: ("V-", "GND")}
    board, cfg, _, _ = _setup(tmp_path, [], [("U3", dual)], name="dual")
    chans = opamp_channels(board.footprint("U3"), cfg.model_for("U3"))
    assert [(c.name, c.inp.number, c.inn.number, c.out.number) for c in chans] == [("U3A", "3", "2", "1"),
                                                                                  ("U3B", "5", "6", "7")]
