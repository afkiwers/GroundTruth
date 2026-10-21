"""A part's current shared over its pins by copper area, each pin's copper counted once (power._area_shares,
pad_weights, [A51]). The user, of a GND via above an EPC2306's source pin 2 carrying the most: "it doesn't make much
sense, why it that via carring the most load?": the pin is a bar under three tabs, and summing the primitives gave it
42 % of the source current for 34 % of the copper."""
import pytest

from boardgen import BoardGen
from conftest import make
from groundtruth.analysis.power import _area_shares, _pad_nodes, pad_weights, pin_area
from groundtruth.analysis.simulate import BoardModel


def _board(tmp_path):
    """A pour of net N on F.Cu with U1: pin 1 a 2.0 x 0.5 mm bar under a 0.5 x 1.0 mm tab (0.25 mm² of them
    overlapping: 1.25 mm² of copper, 1.5 mm² summed), pin 2 one 1.25 x 1.0 mm pad (1.25 mm²)."""
    g = BoardGen(layers=2, size=(20.0, 12.0))
    g.zone("N", "F.Cu", [(1.0, 1.0), (19.0, 1.0), (19.0, 11.0), (1.0, 11.0)])
    g.footprint("U1", "X", "Package:Fake", (10.0, 5.0), [
        {"number": 1, "at": (0.0, 0.0), "net": "N", "size": (2.0, 0.5)},
        {"number": 1, "at": (0.0, 0.25), "net": "N", "size": (0.5, 1.0)},
        {"number": 2, "at": (0.0, 2.5), "net": "N", "size": (1.25, 1.0)}])
    board, sc, _ = make(tmp_path, g, "pins", nets=())
    return board, sc


def test_a_pin_drawn_from_overlapping_pads_counts_its_copper_once(tmp_path):
    """pin_area is the union (1.25 mm², not 1.5); pad_weights give each pin its copper, split over its pads by size;
    _area_shares feeds the two pins 1 : 1 (summing the pads gave 1.5 : 1.25, 55 %), each node once."""
    board, sc = _board(tmp_path)
    pads = [p for p in board.pads() if p.pin.startswith("U1.")]
    one = [p for p in pads if p.pin == "U1.1"]
    two = [p for p in pads if p.pin == "U1.2"]
    assert pin_area(one) == pytest.approx(1.25, rel=1e-6) and pin_area(two) == pytest.approx(1.25, rel=1e-6)
    w = pad_weights(pads)
    assert sum(w) == pytest.approx(2.5) and w[0] / w[1] == pytest.approx(2.0)    # the bar 1.0 mm², the tab 0.5
    st = BoardModel(board, sc.config).build((0.0, 0.0, 20.0, 12.0), ["F.Cu"], {"N"}, 0.1, False)
    inj = _area_shares(st, pads, "F.Cu", 1.0)
    nodes = [n for n, _ in inj]
    assert len(nodes) == len(set(nodes)) and sum(a for _, a in inj) == pytest.approx(1.0)
    at = dict(inj)
    under = {pin: {n for p in ps for n in _pad_nodes(st, p, "F.Cu")} for pin, ps in (("1", one), ("2", two))}
    assert sum(at[n] for n in under["1"]) == pytest.approx(0.5, rel=1e-9)
    assert sum(at[n] for n in under["2"]) == pytest.approx(0.5, rel=1e-9)
    assert len({round(at[n], 15) for n in under["1"]}) == 1           # evenly over pin 1, the overlap not twice
