"""A part's pads soldered to its metal (simulate.part_lid, peec.LidNode, [A65]): the current hands over to the part
where the copper brings it, as a transmission-line contact [Berger 1972], not at a few sampled points (the user, of
refboard's Q601: 54 A/mm² in a pad corner the current only reached by crowding)."""
import math

import numpy as np
import pytest
from shapely.geometry import box

from groundtruth.solver.kernel import skin_thickness
from groundtruth.solver.peec import (SIGMA_CU, Branch, LidNode, PlaneLayer, PlaneStack, RouteExcitation, SolverError,
                                     build_grid)

D, T = 0.1, 35e-6               # mm pitch, m copper


def _strip(lam_mm: float, pad_from: float = 10.0, pad_to: float = 20.0, out_at_lid: bool = False):
    """A 20 mm strip (21 node rows: 2.1 mm wide) fed with 1 A spread over its left end, its nodes from `pad_from` to
    `pad_to` mm bonded to one lid by a contact of transfer length lam_mm (r_c = R_sheet·λ²; each node's share of
    the area, the bonded run's end columns half), the lid joined by a 1 mΩ part to an island where the current
    leaves; out_at_lid: no part, the current taken out at the lid itself (a tab switch's side, power._area_shares).
    Returns (stack, solution, sheet resistance Ω/sq, strip width mm)."""
    g = build_grid({"A": box(-D / 2, -D / 2, 20 + D / 2, 2 + D / 2), "B": box(25 - D / 2, -D / 2, 26 + D / 2, 1 + D / 2)},
                   (0.0, 0.0, 26.0, 2.0), D)
    rs = 1 / (SIGMA_CU * skin_thickness(T, 10.0, SIGMA_CU))
    rc = rs * (lam_mm * 1e-3) ** 2
    lid = LidNode(0, int(15 / D), 10, 0)
    branches = []
    i0, i1 = int(round(pad_from / D)), int(round(pad_to / D))
    for i in range(i0, i1 + 1):
        area = (D * 1e-3) ** 2 * (0.5 if i in (i0, i1) else 1.0)
        for j in range(21):
            branches.append(Branch((0, i, j), lid, (lambda f, r=rc / area: complex(r)), "P1", "bond"))
    island = (0, int(round(25.5 / D)), 5)
    if not out_at_lid:
        branches.append(Branch(lid, island, lambda f: complex(1e-3), "P1", "part"))
    st = PlaneStack([PlaneLayer("F.Cu", 0.0, g, T)], branches)
    inj = [((0, 0, j), 1 / 21) for j in range(21)] + [(lid if out_at_lid else island, -1.0)]
    sol = RouteExcitation(st, [], injections=inj).solve(10.0)
    return st, sol, rs, 21 * D


def test_the_current_hands_over_like_a_transmission_line_contact():
    """Berger's transmission-line model: under a pad of length L bonded with a contact of transfer length λ, the
    sheet's current falls as I₀·sinh((L−u)/λ)/sinh(L/λ) from the pad's edge (u = 0), and the voltage from the strip
    to the part is the strip's resistance up to the pad plus (R_sheet·λ/w)·coth(L/λ). λ = 1 mm on a 0.1 mm grid."""
    st, sol, rs, w = _strip(1.0)
    L, lam = 10.0, 1.0
    current = sol.I_x[0].real.sum(axis=1)            # each column of x strips: from node i to i+1, at x = (i + ½)·d
    for u in (0.25, 0.55, 1.05, 2.05, 3.05):
        x = 10.0 + u
        i = int(round(x / D - 0.5))
        expect = math.sinh((L - u) / lam) / math.sinh(L / lam)
        assert current[i] == pytest.approx(expect, abs=0.003), u      # measured within 0.001
    assert current[int(5 / D)] == pytest.approx(1.0, abs=1e-9)            # all of it before the pad
    v_feed = float(np.mean(sol.V[0, 0, :21].real))
    r = v_feed - float(sol.V_lid[0].real)
    expect = rs * (10.0 / w) + rs * lam / w / math.tanh(L / lam)
    assert r == pytest.approx(expect, rel=0.002)                 # measured 0.01 %
    assert st.n_nodes == st.n_grid + 1 and len(sol.V_lid) == 1


def test_a_tighter_contact_hands_over_sooner_and_a_lid_is_no_grid_node():
    """Half the transfer length: the current under the pad is gone twice as fast; and a lid over a grid node's cell
    is a node of its own (a branch from that grid node to it is a branch, not a loop on one node)."""
    _, wide, _, _ = _strip(1.0)
    _, tight, _, _ = _strip(0.5)
    i = int(round(11.0 / D - 0.5))                   # 1 mm into the pad
    assert tight.I_x[0].real.sum(axis=1)[i] < 0.5 * wide.I_x[0].real.sum(axis=1)[i]
    lid = LidNode(0, 3, 4, 7)
    assert lid != (0, 3, 4) and (0, 3, 4) != lid and lid == LidNode(0, 3, 4, 7) and lid != LidNode(0, 3, 4, 8)
    assert len({lid, (0, 3, 4)}) == 2
    import pickle
    back = pickle.loads(pickle.dumps(lid))
    assert back == lid and back.lid == 7 and tuple(back) == (0, 3, 4)


def test_the_bond_is_a_pin_value_of_any_part():
    """`bond` (µm of solder) reads and writes like the other pin values, from the GT field's words, a part's
    settings entry or the board's defaults, the most specific first (analysis/pins.py)."""
    from types import SimpleNamespace
    from groundtruth.analysis.pins import format_values, params_for, parse_words, values_from_data
    assert parse_words("conv_in, bond:50") == ({"bond": 50.0}, [])
    assert parse_words("bond: 25um")[0] == {"bond": 25.0} and parse_words("bond:0")[0] == {"bond": 0.0}
    assert parse_words("bond:-3")[1] and parse_words("bond:thick")[1]
    assert format_values({"bond": 50.0, "contact": 5e-4}) == "contact:500µΩ, bond:50"
    assert values_from_data({"bond": "40"}, "x") == ({"bond": 40.0}, [])
    cfg = SimpleNamespace(components={"Q1": SimpleNamespace(tht={"bond": 30.0})}, tht={"bond": 60.0})
    assert params_for(None, cfg, "Q1").bond == 30.0 and params_for(None, cfg, "Q1").source["bond"] == "part setting"
    assert params_for(None, cfg, "Q2").bond == 60.0
    assert params_for(None, SimpleNamespace(components={}, tht={}), "Q2").bond is None       # built in (75)


def test_a_bond_to_a_lid_without_a_return_leaves_it_floating():
    """A lid that only bonds to copper (no part branch out) carries no current: the injections still have to balance
    on the copper itself."""
    g = build_grid(box(-D / 2, -D / 2, 5 + D / 2, 1 + D / 2), (0.0, 0.0, 5.0, 1.0), D)
    lid = LidNode(0, 25, 5, 0)
    st = PlaneStack([PlaneLayer("F.Cu", 0.0, g, T)], [Branch((0, i, 5), lid, lambda f: complex(1e-4), "P", "bond")
                                                      for i in range(20, 31)])
    sol = RouteExcitation(st, [], injections=[((0, 0, 5), 1.0), ((0, 50, 5), -1.0)]).solve(10.0)
    assert abs(sum(sol.I_branch)) < 1e-6                     # whatever enters the lid leaves it again
    with pytest.raises(SolverError):
        RouteExcitation(st, [], injections=[((0, 0, 5), 1.0)]).solve(10.0)


def test_a_current_taken_out_at_a_lid_hands_over_like_a_contact():
    """A switch's tab side takes its stage's current at its lid (power._area_shares): the same transmission-line
    contact [Berger 1972] as through a part, I(u) = I₀·sinh((L−u)/λ)/sinh(L/λ) under the pad and the feed-to-lid
    voltage R_strip + (R_sheet·λ/w)·coth(L/λ)."""
    st, sol, rs, w = _strip(1.0, out_at_lid=True)
    L, lam = 10.0, 1.0
    current = sol.I_x[0].real.sum(axis=1)
    for u in (0.25, 1.05, 3.05):
        i = int(round((10.0 + u) / D - 0.5))
        assert current[i] == pytest.approx(math.sinh((L - u) / lam) / math.sinh(L / lam), abs=0.003), u
    r = float(np.mean(sol.V[0, 0, :21].real)) - float(sol.V_lid[0].real)
    assert r == pytest.approx(rs * (10.0 / w) + rs * lam / w / math.tanh(L / lam), rel=0.002)


def _buck(tmp_path, name, pins=None, tab_mm=None, monkeypatch=None):
    """tests/boardgen.discrete_buck_board's F.Cu (Q1's drain on the VIN pour, its 1.2 × 1.6 mm pads) with its stage
    configured, Q1's pin values `pins`, simulate.TAB_MM as tab_mm: (board, model on a 0.1 mm grid)."""
    from boardgen import discrete_buck_board
    from conftest import make
    from groundtruth.analysis import simulate
    from groundtruth.config.settings import ConverterSpec, SettingsFile
    board, sc, _ = make(tmp_path, discrete_buck_board(), name, nets=())
    sc.set_converters([ConverterSpec("buck 5V", "VIN", "GND", "SW", ["Q1"], ["Q2"], f_sw=400e3, i_out=3.0,
                                     v_in=24.0, v_out=5.0)])
    if pins:
        sc.set_component_pins("Q1", pins)
    sc.save()
    if tab_mm is not None:
        monkeypatch.setattr(simulate, "TAB_MM", tab_mm)
    cfg = SettingsFile.load(sc.path).config
    st = simulate.BoardModel(board, cfg).build((5.0, 5.0, 25.0, 27.0), ["F.Cu"], {"VIN", "SW", "GND"}, 0.1, False)
    return board, st


def test_a_switch_with_a_tab_is_soldered_over_it_and_narrow_pads_are_not(tmp_path, monkeypatch):
    """simulate.switch_lids: a power-stage switch's side with a pad at least TAB_MM wide (a silicon FET's drain tab)
    is a lid soldered over its pads, and the stage's currents enter it there (power._area_shares); narrower pads (a
    GaN FET's bars) keep the even spread [A51]; the pin value `entry` (tab | even) overrides the size."""
    from groundtruth.analysis.power import _area_shares
    from groundtruth.solver.peec import LidNode
    board, st = _buck(tmp_path, "tab")
    assert set(st.side_lids) >= {("Q1", "VIN"), ("Q1", "SW"), ("Q2", "SW"), ("Q2", "GND")}
    assert {b.name for b in st.branches if b.kind == "bond"} >= {"Q1", "Q2"}
    drain = [p for p in board.footprint("Q1").pads if p.net == "VIN"]
    assert _area_shares(st, drain, "F.Cu", 1.0) == [(st.side_lids[("Q1", "VIN")], 1.0)]
    _, narrow = _buck(tmp_path, "narrow", tab_mm=2.0, monkeypatch=monkeypatch)
    assert not [k for k in narrow.side_lids if k[0] in ("Q1", "Q2")]      # C1's (a lumped part) stay
    assert not any(b.kind == "bond" and b.name in ("Q1", "Q2") for b in narrow.branches)
    assert all(not isinstance(n, LidNode) for n, _ in _area_shares(narrow, drain, "F.Cu", 1.0))
    _, forced = _buck(tmp_path, "forced", pins={"entry": "tab"}, tab_mm=2.0, monkeypatch=monkeypatch)
    assert {k for k in forced.side_lids if k[0] != "C1"} == {("Q1", "VIN"), ("Q1", "SW")}
    monkeypatch.setattr("groundtruth.analysis.simulate.TAB_MM", 1.0)
    _, even = _buck(tmp_path, "even", pins={"entry": "even"})
    assert ("Q1", "VIN") not in even.side_lids and ("Q2", "SW") in even.side_lids


def test_a_tab_takes_its_current_at_the_edge_the_copper_brings_it_to(tmp_path):
    """Q1's drain pad (x 14.9-16.1 mm) fed from the VIN pour on its left: soldered as a tab, its current hands over
    within the transfer length (~0.14 mm) of the pad's left edge, so the copper under the pad's middle carries
    almost none; spread evenly over the pad (entry:even, as before) about half of it still crosses the middle."""
    from groundtruth.analysis.power import _area_shares, dc_path
    board, tab = _buck(tmp_path, "edge")
    _, even = _buck(tmp_path, "edge_even", pins={"entry": "even"})
    drain = [p for p in board.footprint("Q1").pads if p.net == "VIN"]

    def density(st, x):
        """The DC density (A/mm²) at (x, 9.0) on F.Cu of 1 A from the VIN pour at (7, 9) into Q1's drain."""
        inj = _area_shares(st, drain, "F.Cu", -1.0) + [(st.node("F.Cu", 7.0, 9.0, "VIN", 0.3), 1.0)]
        _, maps = dc_path(st, inj, 1.0, "drain")
        g = st.grids[0]
        return float(maps["F.Cu"][int(round((x - g.x0) / g.d)), int(round((9.0 - g.y0) / g.d))])
    edge_t, mid_t = density(tab, 14.85), density(tab, 15.5)
    edge_e, mid_e = density(even, 14.85), density(even, 15.5)
    assert edge_t == pytest.approx(edge_e, rel=0.2)          # the same current reaches the pad
    assert mid_t < 0.1 * edge_t
    assert 0.3 * edge_e < mid_e < 0.8 * edge_e
