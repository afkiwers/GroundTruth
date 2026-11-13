"""A pin drawn from several pads is fed over all of them (Board.pin_pads, power.whole_pin) and stands for its largest
pad where one pad is wanted (Board.find_pin). The user, of a motor bridge's RMS map on an ESC whose wire tabs are an
SMD tab plus a grid of 0.5 mm plated holes, all pad "1": "there is no reason for current to pile up in that point".
find_pin gave the first pad, a hole at the tab's corner, and the whole phase current entered the copper there.

Reference: the same tab drawn as one plain pad (no holes). A pin fed over its whole copper is the same copper fed the
same way whatever primitives draw it, so the path's resistance and its peak density must match the plain tab's; the
corner hole alone gives a spreading resistance and a peak that the plain tab does not have."""
import pytest

from boardgen import BoardGen
from conftest import make
from groundtruth.analysis.power import _area_shares, dc_path, whole_pin
from groundtruth.analysis.simulate import BoardModel

TAB = (8.0, 8.0)          # the tab's centre (mm)
HOLES = [(dx, dy) for dx in (-1.5, -0.5, 0.5, 1.5) for dy in (-1.5, -0.5, 0.5, 1.5)]


def _board(tmp_path, holes: bool):
    """A 4 x 4 mm wire tab J1 on F.Cu (net N; with `holes`: listed FIRST, a 4 x 4 grid of 0.5 mm plated holes of the
    same pin, the corner one (+1.5, +1.5) first, as the ESC's footprint lists them), a 2 mm track 30 mm to a plain pad
    J2. (board, the DC model of F.Cu and B.Cu at 0.1 mm)."""
    g = BoardGen(layers=2, size=(48.0, 16.0))
    pads = [{"number": 1, "at": (dx, dy), "net": "N", "size": (0.5, 0.5), "type": "thru_hole", "shape": "circle",
             "drill": 0.25} for dx, dy in sorted(HOLES, key=lambda h: (-h[0], -h[1]))] if holes else []
    pads.append({"number": 1, "at": (0.0, 0.0), "net": "N", "size": (4.0, 4.0)})
    g.footprint("J1", "PHASE", "Connector:Fake_WireTab", TAB, pads)
    g.track((TAB[0] + 2.0, TAB[1]), (40.0, TAB[1]), "N", width=2.0)
    g.footprint("J2", "X", "Connector:Fake", (40.0, TAB[1]), [{"number": 1, "at": (0.0, 0.0), "net": "N",
                                                              "size": (2.0, 2.0)}])
    board, sc, _ = make(tmp_path, g, "tab", nets=())
    st = BoardModel(board, sc.config).build((0.0, 0.0, 48.0, 16.0), ["F.Cu", "B.Cu"], {"N"}, 0.1, False)
    return board, st


def _solve(board, st, pads):
    """1 A in over `pads` (F.Cu), out at J2: dc_path's summary."""
    inj = _area_shares(st, pads, "F.Cu", 1.0) + _area_shares(st, [board.find_pin("J2.1")], "F.Cu", -1.0)
    return dc_path(st, inj, 1.0, "tab")[0]


def test_find_pin_is_the_largest_pad_and_pin_pads_all_of_them(tmp_path):
    board, _ = _board(tmp_path, holes=True)
    tab = board.find_pin("J1.1")
    assert tab.size == (4.0, 4.0) and tuple(tab.position) == TAB
    assert len(board.pin_pads("J1.1")) == 17 == len(whole_pin(board, tab))
    assert board.pin_pads("J9.1") == [] and board.pin_pads("nodot") == []
    first = board.footprint("J1").pads[0]
    assert first.size == (0.5, 0.5) and tuple(first.position) == (TAB[0] + 1.5, TAB[1] + 1.5)


def test_the_tab_is_fed_over_its_copper_not_at_its_first_hole(tmp_path):
    """The injection's share inside the corner hole's 0.5 mm circle is its share of the tab's area (about 1 %), not
    the whole current; the tab with holes then has the plain tab's resistance and peak density (1 %, 3 %), while
    feeding the corner hole alone (the old way) adds its spreading resistance (+2.3 % on this 30 mm track) and
    raises the peak 1.7 times."""
    board, st = _board(tmp_path, holes=True)
    pads = whole_pin(board, board.find_pin("J1.1"))
    inj = _area_shares(st, pads, "F.Cu", 1.0)
    assert sum(a for _, a in inj) == pytest.approx(1.0)
    cx, cy = TAB[0] + 1.5, TAB[1] + 1.5
    near = sum(a for n, a in inj if (st.xy(n)[0] - cx) ** 2 + (st.xy(n)[1] - cy) ** 2 <= 0.3 ** 2)
    assert near < 0.03
    fixed = _solve(board, st, pads)
    old = _solve(board, st, [board.footprint("J1").pads[0]])
    (tmp_path / "plain").mkdir()
    plain_board, plain_st = _board(tmp_path / "plain", holes=False)
    plain = _solve(plain_board, plain_st, [plain_board.find_pin("J1.1")])
    # measured at 0.1 mm: R 7.529 / 7.531 / 7.703 mΩ, peak 19.10 / 19.13 / 32.6 A/mm² (fixed / plain / old)
    assert fixed["r_mohm"] == pytest.approx(plain["r_mohm"], rel=0.01)
    assert fixed["peak_a_mm2"] == pytest.approx(plain["peak_a_mm2"], rel=0.03)
    assert old["peak_a_mm2"] > 1.5 * plain["peak_a_mm2"] and old["r_mohm"] > 1.015 * plain["r_mohm"]
