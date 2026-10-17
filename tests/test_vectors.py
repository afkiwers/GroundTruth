"""Current-density vectors: the solver's field at its dominant phase, the arrow field, the heatmap arrows and the
arrows drawn on the board."""
import cmath
import dataclasses
import math

import numpy as np
import pytest
from shapely.geometry import box

from conftest import make
from groundtruth.results.vectors import arrow_field
from groundtruth.solver.peec import build_grid
from test_solver import H, microstrip


def test_dc_arrows_follow_the_strip_and_match_the_density(tmp_path):
    """2 A from U1 to U2 along the 4 mm pour: in its middle every vector points along +x (from the + to the −
    injection), across-strip components vanish, and |J| equals the density map (I/(w·t) there)."""
    from test_pours import WIDTH, X0, Y, pour_board
    from groundtruth.analysis.power import DC_F, _area_shares, dc_summary, dc_vector_maps
    from groundtruth.analysis.simulate import BoardModel
    from groundtruth.solver.peec import RouteExcitation
    length = 20.0
    board, sc, _ = make(tmp_path, pour_board(length), "dcvec", nets=())
    st = BoardModel(board, sc.config).build((X0 - 2, Y - 4, X0 + length + 2, Y + 4), ["F.Cu"], {"PWR"}, 0.1, False)
    inj = _area_shares(st, [board.find_pin("U1.1")], "F.Cu", 1.0) + \
        _area_shares(st, [board.find_pin("U2.1")], "F.Cu", -1.0)
    sol = RouteExcitation(st.plane, [], injections=inj).solve(DC_F)
    _, maps = dc_summary(st, sol, 2.0, "strip")
    jx, jy = dc_vector_maps(st, sol, 2.0)["F.Cu"]
    g = st.grids[0]
    xs = g.x0 + np.arange(g.nx) * g.d
    mid = (xs > X0 + 0.3 * length) & (xs < X0 + 0.7 * length)
    sel = g.node & mid[:, None]
    assert np.all(jx[sel] > 0)
    assert np.max(np.abs(jy[sel])) < 0.02 * np.max(jx[sel])
    assert np.allclose(np.hypot(jx, jy)[sel], maps["F.Cu"][sel], rtol=1e-5)
    assert float(np.median(jx[sel])) == pytest.approx(2.0 / (WIDTH * 0.035), rel=0.03)


def test_return_current_flows_back_under_the_trace():
    """The trace runs along +x; at 100 MHz the plane's return current flows −x right under it and, summed across
    the plane, carries the whole signal current back (−1 A per A)."""
    g, pp = microstrip(box(0, 0, 40, 20))
    sol = pp.solve(1e8)
    kx, ky = sol.sheet_vectors(0, g.d)
    i = int(round((20 - g.x0) / g.d))
    j = int(round((10 - g.y0) / g.d))
    assert kx[i, j] < 0 and abs(ky[i, j]) < 0.01 * abs(kx[i, j])
    assert float(np.sum(kx[i, :]) * g.d * 1e-3) == pytest.approx(-1.0, abs=0.02)
    # under the trace the field is in phase with the signal: the arrows carry the full magnitude
    near = np.abs(g.y0 + np.arange(g.ny) * g.d - 10.0) < 3 * H
    assert np.allclose(np.hypot(kx, ky)[i, near], sol.sheet_current(0, g.d)[i, near], rtol=0.01)


def test_dominant_phase_does_not_depend_on_the_solution_phase():
    """The same currents shifted by any phase (within ±90°, where the sign rule keeps the reference) give the
    same arrows: the dominant phase follows the principal axis of the complex field."""
    g, pp = microstrip(box(0, 0, 40, 20))
    sol = pp.solve(1e8)
    base = sol.sheet_vectors(0, g.d)
    for theta in (0.6, -1.2):
        rot = cmath.exp(1j * theta)
        other = dataclasses.replace(sol, I_x=sol.I_x * rot, I_y=sol.I_y * rot)
        kx, ky = other.sheet_vectors(0, g.d)
        assert np.allclose(kx, base[0], atol=1e-9 * np.abs(base[0]).max())
        assert np.allclose(ky, base[1], atol=1e-9 * np.abs(base[0]).max())


def test_arrow_field_on_a_strip():
    """Equal arrows whose number follows the current: a uniform current along a 10 × 2 mm strip fills every spot
    with the same +x arrow; a strip whose right half carries a quarter of the density (proportional fill,
    gamma 1) gets a quarter of the arrows there; copper below the floor and empty cells get none; max_arrows
    coarsens the spots until the arrows fit."""
    from groundtruth.results.vectors import reference_density
    g = build_grid(box(0, 0, 10, 2), (0, 0, 20, 4), 0.1)
    kx = np.where(g.node, 5.0, 0.0)
    ky = np.zeros_like(kx)
    arrows = arrow_field(g, np.hypot(kx, ky), kx, ky, n_across=40)
    x, y, dx, dy = (np.array(c) for c in zip(*arrows))
    assert np.all(dx > 0) and np.allclose(dy, 0) and np.allclose(dx, dx[0])     # one size, along the current
    assert np.all(x + dx / 2 < 10.05) and np.all(y < 2.05)                     # on the copper only
    b = math.ceil(max(g.nx, g.ny) / 40)                                        # nodes per spot side
    ii, jj = np.nonzero(g.node)
    spots = len({(i // b, j // b) for i, j in zip(ii, jj)})                     # spots holding copper
    assert len(arrows) == spots                                                 # a full current: every spot on
    xs = g.x0 + np.arange(g.nx)[:, None] * g.d
    two = np.where(g.node, np.where(xs < 5, 1.0, 0.25), 0.0)
    assert reference_density([two]) == 1.0                                      # where most current flows
    a2 = arrow_field(g, two, two, np.zeros_like(two), n_across=40, gamma=1.0)
    left = sum(1 for a in a2 if a[0] + a[2] / 2 < 5)
    right = len(a2) - left
    assert 0.18 < right / left < 0.32, (left, right)                            # a quarter of the density
    floor = np.where(g.node, np.where(xs < 5, 1.0, 0.005), 0.0)                 # 0.5 %: below the floor
    a3 = arrow_field(g, floor, floor, np.zeros_like(floor), n_across=40)
    assert a3 and all(a[0] + a[2] / 2 < 5 + b * g.d for a in a3)               # (one spot may straddle x = 5)
    assert len(arrow_field(g, np.hypot(kx, ky), kx, ky, n_across=40, max_arrows=10)) <= 10


def test_heatmap_draws_arrows_and_keeps_the_colour_bar_clear(tmp_path):
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure
    from matplotlib.quiver import Quiver
    from boardgen import microstrip_board
    from groundtruth.analysis.simulate import simulate
    from groundtruth.report.plots import draw_heatmap
    board, sc, _ = make(tmp_path, microstrip_board(), "vec", solve_points=1)
    rr = simulate(board, sc.config).routes[0]
    assert rr.sheet_vectors
    for vectors in (True, False):
        fig = Figure(figsize=(8, 5))
        FigureCanvasAgg(fig)
        draw_heatmap(fig, board, rr, vectors=vectors)
        quivers = [c for ax in fig.axes for c in ax.collections if isinstance(c, Quiver)]
        assert bool(quivers) == vectors
        maps, bar = fig.axes[:-1], fig.axes[-1]
        assert bar.get_position().x0 > max(ax.get_position().x1 for ax in maps)
    # the colour scale: three decades below the peak (log, the default) or 0 to the peak (linear)
    from matplotlib.colors import LogNorm
    for scale in ("log", "linear"):
        fig = Figure(figsize=(8, 5))
        FigureCanvasAgg(fig)
        draw_heatmap(fig, board, rr, scale=scale)
        norm = fig.axes[0].images[0].norm
        assert isinstance(norm, LogNorm) == (scale == "log")
        assert norm.vmin == (pytest.approx(norm.vmax * 1e-3) if scale == "log" else 0.0)
        assert ("linear colour scale" in " ".join(fig._gt_note.get_text().split())) == (scale == "linear")


def test_a_zoomed_heatmap_shows_its_view_with_arrows_laid_out_for_it(tmp_path):
    """draw_heatmap(view=...): the maps show that part (all layers), the arrows lie inside it and get denser
    than over the whole map; the colour scale stays the whole map's. zoomed(): the wheel's view arithmetic."""
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure
    from matplotlib.quiver import Quiver
    from boardgen import microstrip_board
    from groundtruth.analysis.simulate import simulate
    from groundtruth.report.plots import draw_heatmap
    from groundtruth.ui.results_panels import zoomed
    board, sc, _ = make(tmp_path, microstrip_board(), "zoom", solve_points=1)
    rr = simulate(board, sc.config).routes[0]
    full = rr.layer_grids[next(iter(rr.layer_grids))].extent
    view = zoomed(full, full, 20.0, 10.0, 0.4)                    # 2.5x in about the middle of the trace
    assert view[1] - view[0] == pytest.approx(0.4 * (full[1] - full[0]))

    def draw(v):
        fig = Figure(figsize=(8, 5))
        FigureCanvasAgg(fig)
        draw_heatmap(fig, board, rr, view=v)
        q = next(c for c in fig.axes[0].collections if isinstance(c, Quiver))
        return fig, q.get_offsets(), fig.axes[0].images[0].norm.vmax
    fig_all, arrows_all, vmax_all = draw(None)
    fig, arrows, vmax = draw(view)
    assert sorted(fig.axes[0].get_xlim()) == pytest.approx(view[:2]) and sorted(fig.axes[0].get_ylim()) == \
        pytest.approx(view[2:])
    inside = ((arrows[:, 0] >= view[0] - 1) & (arrows[:, 0] <= view[1] + 1)
              & (arrows[:, 1] >= view[2] - 1) & (arrows[:, 1] <= view[3] + 1))
    assert inside.all() and len(arrows) > 0.4 * len(arrows_all) and vmax == vmax_all
    edge = zoomed(view, full, full[0], full[2], 0.5)               # zooming at a corner stays inside the board
    assert edge[0] >= full[0] - 1e-9 and edge[2] >= full[2] - 1e-9
    assert zoomed(view, full, 20.0, 10.0, 3.0) is None             # zoomed out past the whole map: no view


def test_heatmaps_mark_the_vias_carrying_current_with_their_share(tmp_path):
    """The user: "Only the ones that carry current should be drawn. However, the current symbol is a bit small and
    hard to see." The stitching via that carries the return between the planes: its ring and drill at true size,
    a large ▲ and its share of the current; no other via of the board."""
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.collections import EllipseCollection
    from matplotlib.figure import Figure
    from boardgen import layer_change_board
    from groundtruth.analysis.simulate import simulate
    from groundtruth.report.plots import draw_heatmap
    board, sc, _ = make(tmp_path, layer_change_board(1.0), "vias", solve_points=1)
    res = simulate(board, sc.config)
    rr = res.routes[0]
    t = next(t for t in rr.transfers if t["kind"] == "via")
    stitch = next(v for v in board.vias if v.net == "GND" and abs(v.position[0] - 20.0) < 1e-6)
    assert (t["x"], t["y"]) == pytest.approx(stitch.position, abs=0.2) and t["fraction"] > 0.3   # its grid node
    fig = Figure(figsize=(9, 5))
    FigureCanvasAgg(fig)
    draw_heatmap(fig, board, rr, res.issues, layer="In1.Cu")
    ax = fig.axes[0]
    rings = [c for c in ax.collections if isinstance(c, EllipseCollection)]
    assert len(rings) == 2 and all(len(c.get_offsets()) == 1 for c in rings)          # pad ring, drill
    assert tuple(rings[0].get_offsets()[0]) == pytest.approx(stitch.position)
    assert rings[0]._widths[0] == pytest.approx(stitch.size / 2)   # board units: true size (half widths stored)
    triangles = [l for l in ax.lines if l.get_marker() == "^"]
    assert len(triangles) == 1 and triangles[0].get_markersize() >= 9
    assert f"{t['fraction'] * 100:.0f} %" in [a.get_text() for a in ax.texts]
    # a plane pair's displacement current (board-wide model, spread over the pair) gets no marker: the user's demo
    # showed a ■ "1286 %" at its weighted centre
    rr.transfers.append({"name": "plane capacitance In1.Cu–In2.Cu", "kind": "capacitance",
                         "layers": ["In1.Cu", "In2.Cu"], "x": 20.0, "y": 10.0, "fraction": 12.86,
                         "distance_mm": 0.3, "distributed": True, "f": 1.45e9})
    draw_heatmap(fig, board, rr, res.issues, layer="In1.Cu")
    ax = fig.axes[0]
    assert not [l for l in ax.lines if l.get_marker() == "s"]
    assert "1286 %" not in [a.get_text() for a in ax.texts]


def test_arrows_on_the_board_are_one_group_of_segments():
    pytest.importorskip("kipy")
    from types import SimpleNamespace
    from groundtruth.adapters.kipy_sink import draw_arrows

    from kipy.util.board_layer import canonical_name, is_copper_layer, layer_from_canonical_name
    from groundtruth.adapters.kipy_sink import DrawingLayerError, take_enabled

    class Board:
        def __init__(self, layers=("F.Cu", "B.Cu", "User.7")):
            self.items, self.groups, self.set_calls, self.drops = [], [], [], ()
            self.enabled = [layer_from_canonical_name(n) for n in layers]

        def get_enabled_layers(self):
            return self.enabled

        def set_enabled_layers(self, copper_count, layers):
            self.set_calls.append((copper_count, list(layers)))
            copper = [l for l in self.enabled if is_copper_layer(l)]
            if self.drops:                               # a KiCad that loses a layer (must stop the drawing)
                layers = [l for l in layers if canonical_name(l) not in self.drops]
            self.enabled = copper[:copper_count] + list(layers)
            return self.enabled

        def get_groups(self):
            return self.groups

        def begin_commit(self):
            return "c"

        def create_items(self, items):
            if not isinstance(items, list):                 # the group
                self.groups.append(SimpleNamespace(name=items.proto.name, proto=SimpleNamespace(items=[])))
                return [items]
            self.items += items
            return items

        def push_commit(self, commit, message):
            pass

        def drop_commit(self, commit):
            pass

        def remove_items_by_id(self, ids):
            pass

        def remove_items(self, groups):                  # clearing the earlier drawing
            self.groups = [g for g in self.groups if g not in groups]

    b = Board()
    n = draw_arrows(b, [(1.0, 1.0, 2.0, 0.0), (1.0, 3.0, 0.0, -1.5)], "User.7", "test")
    assert n == 2 and len(b.items) == 6 and [g.name for g in b.groups] == ["groundtruth-arrows"]
    # kraft_flux_mini_2 enables User.1 to User.4 only: KiCad drew on User.7 but would not group the shapes (only
    # Undo removed them). The user: "when not enabled, enable automatically". set_enabled_layers deletes the
    # content of every layer left out, so it gets the copper count and every enabled layer back, plus User.7.
    take_enabled()
    lacking = Board(("F.Cu", "In1.Cu", "In2.Cu", "B.Cu", "F.SilkS", "User.1", "User.4"))
    assert draw_arrows(lacking, [(1.0, 1.0, 2.0, 0.0)], "User.7", "test") == 1
    (count, layers), = lacking.set_calls
    assert count == 4 and [canonical_name(l) for l in layers] == ["F.SilkS", "User.1", "User.4", "User.7"]
    assert len(lacking.items) == 3 and [g.name for g in lacking.groups] == ["groundtruth-arrows"]
    assert take_enabled() == ["User.7"] and take_enabled() == []
    draw_arrows(lacking, [(1.0, 1.0, 2.0, 0.0)], "User.7", "test")   # enabled now: no second call
    assert len(lacking.set_calls) == 1
    lossy = Board(("F.Cu", "B.Cu", "User.1"))
    lossy.drops = ("User.1",)                                           # KiCad would drop a layer: stop
    with pytest.raises(DrawingLayerError, match=r"could not enable User\.7.*User\.1"):
        draw_arrows(lossy, [(1.0, 1.0, 2.0, 0.0)], "User.7", "test")
    assert not lossy.items and not lossy.groups
    # the head strokes start at the tip and lean back by ±25°
    shaft, h1, h2 = b.items[:3]
    tip = (shaft.end.x, shaft.end.y)
    assert (h1.start.x, h1.start.y) == tip == (h2.start.x, h2.start.y)
    assert h1.end.x < tip[0] and h2.end.x < tip[0] and math.copysign(1, h1.end.y - tip[1]) != \
        math.copysign(1, h2.end.y - tip[1])


def test_heatmap_maps_use_the_space_and_zoom_together(tmp_path):
    """The user: 'the heatmaps are useless in its current state'. The layer maps go side by side for a tall,
    narrow region and in a 2x2 grid for a square or wide one, whichever makes each map largest; they share their
    axes, so zooming or panning one shows the same spot on every layer."""
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure
    from boardgen import microstrip_board
    from groundtruth.analysis.simulate import simulate
    from groundtruth.report.plots import draw_heatmap, map_grid
    assert map_grid(4, (8, 5), (30, 90)) == (1, 4)             # kraft_flux_mini_2's SCL: tall and narrow
    assert map_grid(4, (8, 5), (50, 50)) == (2, 2) and map_grid(4, (8, 5), (90, 30)) == (2, 2)
    assert map_grid(4, (5, 9), (90, 30)) == (4, 1) and map_grid(1, (8, 5), (30, 90)) == (1, 1)
    board, sc, _ = make(tmp_path, microstrip_board(layers=4), "four", solve_points=1)
    rr = simulate(board, sc.config).routes[0]
    fig = Figure(figsize=(8, 5))
    FigureCanvasAgg(fig)
    draw_heatmap(fig, board, rr)
    maps = fig.axes[:-1]
    assert len(maps) >= 2
    maps[0].set_xlim(10, 20)
    maps[0].set_ylim(15, 5)
    assert all(ax.get_xlim() == (10, 20) and ax.get_ylim() == (15, 5) for ax in maps)


def test_power_stage_maps_mark_the_switches_drain_and_source(tmp_path):
    """The user: "in the heatmap, display the drain and source locations better". The switches' pads get their
    role from their nets (a buck's high side: drain on the rail, source on the switch node; the low side: drain on
    the switch node; a boost's diode: anode on the switch node), and every converter map labels them."""
    from dataclasses import replace
    from matplotlib.figure import Figure
    from boardgen import boost_board, discrete_buck_board
    from groundtruth.analysis.detect import detect_converters
    from groundtruth.analysis.power import switch_pins
    from groundtruth.analysis.simulate import simulate
    from groundtruth.config.settings import ConverterSpec
    from groundtruth.report.plots import draw_heatmap
    board, sc, _ = make(tmp_path, discrete_buck_board(out_connector=True), "buck", nets=(), solve_points=1)
    spec = replace(detect_converters(board, sc.config)[0].spec, i_out=3.0, v_in=12.0, v_out=5.0)
    roles = {(p["ref"], p["role"]): p["net"] for p in switch_pins(board, sc.config, spec)}
    assert roles == {("Q1", "D"): spec.rail, ("Q1", "S"): spec.switch_node, ("Q2", "D"): spec.switch_node,
                     ("Q2", "S"): spec.ground}
    (tmp_path / "boost").mkdir()
    b2, sc2, _ = make(tmp_path / "boost", boost_board(), "boost", nets=())
    boost = ConverterSpec("boost", "VOUT", "GND", "SW", ["D1"], ["Q1"], topology="boost")
    roles = {(p["ref"], p["role"]): p["net"] for p in switch_pins(b2, sc2.config, boost)}
    assert roles == {("D1", "K"): "VOUT", ("D1", "A"): "SW", ("Q1", "D"): "SW", ("Q1", "S"): "GND"}
    c = simulate(board, replace(sc.config, converters=[spec])).converters[0]
    assert {p["role"] for p in c.switch_pins} == {"D", "S"} and "switch_pins" not in c.summary()
    for layer in (None, c.main_layers()[0]):
        fig = Figure(figsize=(9, 6))
        draw_heatmap(fig, board, c, layer=layer)
        texts = {t.get_text() for ax in fig.axes for t in ax.texts}
        assert {"D", "S", "Q1", "Q2"} <= texts
        labels = fig.axes[0].get_legend_handles_labels()[1]
        assert "D = drain" in labels and "S = source" in labels


def test_board_arrows_get_thicker_with_the_current_density():
    """Draw arrows on board: the line width follows the density at each arrow on the heatmap's log scale, the
    thickest at the result's peak, the thinnest three decades below and lower; the Preferences' range (defaults
    0.01 to 0.3 mm) is kept sane: out-of-range values fall back, a swapped pair is put in order."""
    from groundtruth.results.vectors import arrow_widths
    from groundtruth.ui.prefs_dialog import _arrow_range
    w = arrow_widths([100.0, 10.0, 1.0, 0.1, 0.001, 0.0], 100.0, 0.01, 0.3)
    assert w[0] == pytest.approx(0.3) and w[3] == pytest.approx(0.01) and w[4] == w[5] == 0.01
    assert w[0] > w[1] > w[2] > w[3] and w[1] - w[2] == pytest.approx(w[2] - w[3])       # log-linear
    assert arrow_widths([5.0], 0.0, 0.02, 0.4) == [0.02]
    assert _arrow_range({}) == {"arrow_min_mm": 0.01, "arrow_max_mm": 0.3}
    assert _arrow_range({"arrow_min_mm": 0.5, "arrow_max_mm": 0.1}) == {"arrow_min_mm": 0.1, "arrow_max_mm": 0.5}
    assert _arrow_range({"arrow_min_mm": -1, "arrow_max_mm": "x"}) == {"arrow_min_mm": 0.01, "arrow_max_mm": 0.3}
