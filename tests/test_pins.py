"""A through-hole pin joined to every layer around its whole barrel (simulate.add_pin; the user, of a big connector
pin's map: "the current density around that area looks odd. We need to find a way to better simulate those"),
against the analytic field of a pin feeding a plane: J(r) = I / (2π·r·t), the same in every direction."""
import math

import numpy as np
import pytest

from boardgen import BoardGen
from conftest import make
from groundtruth.analysis import simulate as sim

X0 = 25.0                       # the pin's centre (mm)


def pin_board() -> BoardGen:
    """A 40 mm square P pour on B.Cu with a 2 mm-drill, 3.5 mm through-hole pad in its middle (on F.Cu only its pad:
    the current goes down the pin) and a sink pad at each corner on B.Cu."""
    g = BoardGen(layers=2, size=(50.0, 50.0))
    g.zone("P", "B.Cu", [(5.0, 5.0), (45.0, 5.0), (45.0, 45.0), (5.0, 45.0)])
    g.footprint("J1", "Pin", "Connector:Fake_tht", (X0, X0), [
        {"number": 1, "at": (0, 0), "net": "P", "size": (3.5, 3.5), "type": "thru_hole", "drill": 2.0,
         "shape": "circle"}], attrs="through_hole")
    for k, (x, y) in enumerate(((7.0, 7.0), (43.0, 7.0), (7.0, 43.0), (43.0, 43.0))):
        g.footprint(f"S{k}", "Sink", "Connector:Fake_pad", (x, y), [
            {"number": 1, "at": (0, 0), "net": "P", "size": (2.0, 2.0)}], layer="B.Cu")
    return g


def solve(tmp_path, name, d=0.25):
    """The B.Cu current density map (A/mm² at 1 A) of pin_board, with each node's distance from the pin (mm)."""
    from groundtruth.analysis.power import _area_shares, dc_path
    board, sc, _ = make(tmp_path, pin_board(), name, nets=())
    st = sim.BoardModel(board, sc.config).build((4.0, 4.0, 46.0, 46.0), ["F.Cu", "B.Cu"], {"P"}, d, False)
    inj = _area_shares(st, [board.find_pin("J1.1")], "F.Cu", 1.0)
    for k in range(4):
        inj += _area_shares(st, [board.find_pin(f"S{k}.1")], "B.Cu", -0.25)
    _, maps = dc_path(st, inj, 1.0, "pin")
    g, m = st.grids[st.names.index("B.Cu")], maps["B.Cu"]
    ii, jj = np.meshgrid(np.arange(g.nx), np.arange(g.ny), indexing="ij")
    x, y = g.xy(ii, jj)
    return m, np.hypot(x - X0, y - X0)


def test_a_pin_feeds_a_plane_like_the_analytic_field_without_a_hot_spot(tmp_path, monkeypatch):
    """Around the pin J(r) = I / (2π·r·t), the same in every direction (3 and 5 mm: within 3 %); at the barrel the
    peak is the analytic I / (2π·(drill/2)·t) within 30 % (it converges with the grid: 3.8, 4.3, 5.1 A/mm² at 0.25,
    0.125, 0.0625 mm against 4.55). Joined at one node (the old model) the whole current entered one cell: a hot spot
    of 20.8 A/mm², four to five times what is there."""
    t, drill = 0.035, 2.0
    m, r = solve(tmp_path, "new")
    for radius in (3.0, 5.0):
        ring = m[np.abs(r - radius) < 0.125]
        assert ring.mean() == pytest.approx(1.0 / (2 * math.pi * radius * t), rel=0.03), radius
        assert ring.max() / ring.min() < 1.1, radius
    wall = 1.0 / (2 * math.pi * drill / 2 * t)
    assert 0.7 * wall < m[r < 2.5].max() < 1.3 * wall
    monkeypatch.setattr(sim, "pin_joints", lambda drill, d: 1)          # the old single joint
    old, r = solve(tmp_path, "old")
    assert old[r < 2.5].max() > 3 * wall


def test_the_joints_follow_the_ring():
    """pin_joints: one per ~1.5 cells of the barrel's circumference, 1 for a small via, at most PIN_JOINTS_MAX."""
    assert sim.pin_joints(0.3, 0.5) == 1
    assert sim.pin_joints(1.0, 0.25) == 8
    assert sim.pin_joints(5.0, 0.1) == sim.PIN_JOINTS_MAX
