"""The cross-section along a route (analysis/section.py, report/plots.draw_section): the return current each plane
layer carries past each point of the route, validated against KCL and the image-current law of a microstrip."""
import math

import numpy as np
import pytest

from boardgen import layer_change_board, microstrip_board
from conftest import make
from groundtruth.analysis.section import route_section, station_at
from groundtruth.analysis.simulate import simulate


def _run(tmp_path, gen, name):
    board, sc, _ = make(tmp_path, gen, name, solve_points=1)
    res = simulate(board, sc.config)
    return board, res, res.routes[0], res.setups[0]


def test_a_microstrip_return_follows_the_image_law_and_kcl(tmp_path):
    """A straight trace over one plane (h = 0.235 mm): inside ±W of the trace the plane carries (2/π)·atan(W/h) of
    the return (the image-current profile K(y) = h/π(h²+y²)), and a cut across the whole board all of it, in phase
    with the signal (KCL). Measured 2026-10-06: within 0.5 % at W = 2h, 5h, 20h; the whole cut 1.0000."""
    board, res, rr, setup = _run(tmp_path, microstrip_board(layers=4), "ms")
    rep = setup.fine.ok()[rr.map_frequency]
    h = setup.h_mm

    def middle(half):
        sec = route_section(rep, setup.fine.build, setup.route, set(setup.route.nets), half)
        return sec["I"].sum(axis=0)[(sec["s_mm"] > 10) & (sec["s_mm"] < 20)]
    for a in (2, 5, 20):
        v = middle(a * h).real
        assert np.abs(v / ((2 / math.pi) * math.atan(a)) - 1).max() < 0.02, (a, v.min(), v.max())
    whole = middle(40.0)
    assert whole.real == pytest.approx(np.ones(len(whole)), abs=2e-3) and np.abs(whole.imag).max() < 2e-3
    sec = rr.section                                # what the simulation keeps: ±20 h, the route's plane layers
    assert sec["half_mm"] == pytest.approx(20 * h) and sec["layers"] == setup.fine.build.names
    assert sec["f"] == rr.map_frequency and sec["length_mm"] == pytest.approx(30.0)
    assert [r[2] for r in sec["signal"]] == ["F.Cu"] and sec["vias"] == []
    assert "section" not in rr.summary()            # kept out of the JSON report


def test_a_stitching_via_hands_the_return_to_the_other_plane_where_the_signal_changes_layer(tmp_path):
    """Layer change at s = 15 mm (F.Cu over In1 → B.Cu under In2). With a GND via 0.8 mm away the return swaps
    planes right there; without one it can only change planes at the ICs' GND vias, so it crosses over gradually
    and far from the layer change ~30 % still flows on the plane the signal has left. Either way it stays under the
    trace: inside ±20 h the layers add up to (2/π)·atan(20) = 97 %."""
    out = {}
    for name, stitch in (("stitched", 0.8), ("open", None)):
        _, _, rr, _ = _run(tmp_path, layer_change_board(stitch), name)
        sec = rr.section
        s, I = sec["s_mm"], sec["I"].real
        k1, k2 = sec["layers"].index("In1.Cu"), sec["layers"].index("In2.Cu")
        assert sec["vias"] and all(v[0] == pytest.approx(15.0) for v in sec["vias"])
        assert [r[2] for r in sec["signal"]] == ["F.Cu", "B.Cu"]
        inner = (s > 2) & (s < 28)
        assert np.abs(I.sum(axis=0)[inner] - (2 / math.pi) * math.atan(20)).max() < 0.02
        cross = np.nonzero(np.diff(np.sign(I[k1] - I[k2])))[0]
        assert len(cross) == 1 and s[cross[0]] == pytest.approx(15.0, abs=0.5)
        at = {x: int(np.argmin(np.abs(s - x))) for x in (8.0, 15.0, 22.0)}
        out[name] = {x: (I[k1, i], I[k2, i]) for x, i in at.items()}
    st, op = out["stitched"], out["open"]
    assert st[8.0][0] > 0.85 and st[22.0][1] > 0.85 and st[22.0][0] < 0.15      # measured 0.89, 0.89, 0.10
    assert abs(op[15.0][0] - op[15.0][1]) < 0.1                                  # half and half at the change
    assert op[22.0][0] > 0.2 and op[8.0][1] > 0.2                                # measured 0.29, 0.28


def test_carriers_are_placed_along_the_route():
    route_xy = [("F.Cu", [(5.0, 10.0), (20.0, 10.0)]), ("B.Cu", [(20.0, 10.0), (20.0, 20.0), (30.0, 20.0)])]
    assert station_at(12.0, 11.0, route_xy) == pytest.approx((7.0, 1.0))
    assert station_at(21.0, 15.0, route_xy) == pytest.approx((20.0, 1.0))
    assert station_at(40.0, 20.0, route_xy) == pytest.approx((35.0, 10.0))       # past the end: the end
    assert station_at(0.0, 0.0, []) is None


def test_the_section_figure(tmp_path):
    """Rows are the copper layers in stack order, the signal runs on F.Cu then B.Cu with one layer change at
    s = 15 mm, the stitching via that carries the return is a green bar labelled with its share, and the value
    under the mouse lists each layer's share there. A view without a route says it has none."""
    from types import SimpleNamespace as NS
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure
    from groundtruth.report.plots import CARRIER, SECTION_NONE, draw_section, section_value
    board, res, rr, _ = _run(tmp_path, layer_change_board(0.8), "fig")
    fig = Figure(figsize=(8, 3.2))
    FigureCanvasAgg(fig)
    draw_section(fig, board, rr)
    top, side = fig.axes[0], fig.axes[1]
    assert [t.get_text() for t in side.get_yticklabels()] == ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
    lines = [(tuple(np.round(l.get_xdata(), 3)), tuple(l.get_ydata())) for l in side.lines]
    assert ((0.0, 15.0), (0, 0)) in lines and ((15.0, 30.0), (3, 3)) in lines             # the signal
    assert sum(1 for x, _ in lines if x == (15.0, 15.0)) == 3                             # F→In1→In2→B
    bars = [l for l in side.lines if l.get_color() == CARRIER]
    via = next(t for t in rr.transfers if t["kind"] == "via")
    assert len(bars) == 1 and bars[0].get_xdata()[0] == pytest.approx(15.0, abs=0.3)
    assert f"{via['fraction'] * 100:.0f} %" in [t.get_text() for t in side.texts]
    assert set(fig._gt_section) == {top, side}
    text = section_value(fig._gt_section[side], 8.0)
    assert text.startswith("s 8.") and "In1.Cu 89 %" in text and "within ±4.7 mm" in text
    draw_section(fig, board, NS(section={}, kind="converter"))
    assert fig.axes[0].texts[0].get_text() == SECTION_NONE


def test_a_bar_in_the_side_view_points_at_its_via_on_the_board(tmp_path):
    """The user: "click on the grey or green vias ... that would then zoom into the pcb and highlight that specific
    via". The section keeps each layer change's via on the board (`via_at`, the route's own via); draw_section
    records every bar's target (fig._gt_marks / _gt_mark_refs on the side view, a mark every half row): a click on
    the green bar finds the stitching via that carries the return, one on the grey bar's F.Cu end the signal's via,
    each at its board position (plots.mark_ref_at, what the Results tab's click hands to KiCad)."""
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure
    from groundtruth.analysis.route import RouteVia
    from groundtruth.report.plots import draw_section, mark_ref_at
    board, res, rr, _ = _run(tmp_path, layer_change_board(0.8), "click")
    route_via = next(st for st in res.setups[0].route.steps if isinstance(st, RouteVia)
                     and st.from_layer != st.to_layer)
    assert rr.section["via_at"][0][:2] == pytest.approx(tuple(route_via.position))
    fig = Figure(figsize=(8, 3.2))
    FigureCanvasAgg(fig).draw()
    draw_section(fig, board, rr)
    FigureCanvasAgg(fig).draw()
    side = fig.axes[1]
    carrier = next(t for t in rr.transfers if t["kind"] == "via")
    sp = next(m[0] for m, r in zip(fig._gt_marks[side], fig._gt_mark_refs[side]) if r[1] is carrier)
    rows = [m[1] for m, r in zip(fig._gt_marks[side], fig._gt_mark_refs[side]) if r[1] is carrier]
    px, py = side.transData.transform((sp, sum(rows) / len(rows)))
    kind, what, text = mark_ref_at(fig, side, px + 2, py)
    assert kind == "via" and what is carrier and "% of the current" in text
    assert mark_ref_at(fig, side, px, py + 200) is None                  # off every bar
    # the same board without its stitching via: the grey bar alone at the layer change (a green one drawn over it,
    # the stitching via beside it at the same station, wins: it is what shows there)
    board, res, rr, _ = _run(tmp_path, layer_change_board(None), "grey")
    route_via = next(st for st in res.setups[0].route.steps if isinstance(st, RouteVia)
                     and st.from_layer != st.to_layer)
    fig = Figure(figsize=(8, 3.2))
    FigureCanvasAgg(fig).draw()
    draw_section(fig, board, rr)
    FigureCanvasAgg(fig).draw()
    side = fig.axes[1]
    px, py = side.transData.transform((15.0, 0.5))                     # the grey bar, between F.Cu and In1.Cu
    kind, what, text = mark_ref_at(fig, side, px, py)
    assert kind == "via" and text.startswith("signal via")
    assert (what["x"], what["y"]) == pytest.approx(tuple(route_via.position))


def test_a_hairpin_s_two_legs_each_keep_their_own_return(tmp_path):
    """A route folding back on itself, its legs 1.5 mm apart over one plane (h = 0.235 mm; the cut ±20 h = 4.7 mm
    would cross the other leg): each cut ends halfway to the other leg [A63], so each leg shows its own return,
    near (2/π)·atan(0.75/h) = 81 % of it, instead of the two legs' opposite returns cancelling to about 0 (the
    user's SW_CLK on refboard)."""
    from boardgen import BoardGen
    g = BoardGen(layers=4, size=(40.0, 20.0))
    g.ic("U1", (5.0, 8.0), "SIG")
    g.ic("U2", (12.0, 12.5), "SIG")
    g.track((5.0, 8.0), (30.0, 8.0), "SIG", "F.Cu")
    g.track((30.0, 8.0), (30.0, 9.5), "SIG", "F.Cu")
    g.track((30.0, 9.5), (12.0, 9.5), "SIG", "F.Cu")
    g.track((12.0, 9.5), (12.0, 12.5), "SIG", "F.Cu")
    g.plane("GND", "In1.Cu")
    for x, y in ((5.0, 9.6), (12.0, 14.1)):
        g.via((x, y), "GND")
    board, res, rr, setup = _run(tmp_path, g, "hairpin")
    sec = rr.section
    total = sec["I"].sum(axis=0).real
    s = sec["s_mm"]
    legs = ((s > 12) & (s < 22)) | ((s > 32) & (s < 40))        # the middles of both legs
    assert total[legs].min() > 0.6 and total[legs].max() < 1.05, (total[legs].min(), total[legs].max())
