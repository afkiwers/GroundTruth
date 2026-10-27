"""The current density maps from the solution to the drawn heatmap (the density audit of 2026-10-27; the user: "areas
where current density is not drawn correctly, consistently, or completely"): each node drawn at its own place in its
value's colour, copper with no current in the scale's lowest colour (not the background), non-finite values blanked
and counted instead of failing the figure, and a node where current enters a plane read from its strips' heat
instead of their mean, which cancels there (route, hot-loop, board-wide and sharper maps used the mean)."""
from __future__ import annotations

import math
from types import SimpleNamespace as N

import numpy as np
import pytest
from matplotlib import colormaps
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.colors import LogNorm, Normalize
from matplotlib.figure import Figure
from shapely.geometry import box

from groundtruth.report.plots import draw_heatmap
from groundtruth.solver.peec import Grid, PlaneLayer, PlaneStack, RouteExcitation, build_grid

BOARD = N(outline=[], footprints=[], vias=[], tracks=[], zones=[], pads=lambda: [], footprint=lambda r: None)


def _grid(nx=20, ny=10, d=1.0, holes=()):
    """A bare Grid of nx x ny nodes d mm apart from (0, 0), all copper but the (i, j) in `holes`."""
    node = np.ones((nx, ny), bool)
    for ij in holes:
        node[ij] = False
    return Grid(0.0, 0.0, d, nx, ny, node, np.zeros((nx - 1, ny), bool), np.zeros((nx, ny - 1), bool))


def _view(g, K):
    """A route-like view showing map K on F.Cu."""
    return N(kind="route", net="T", driver="a", receiver="b", grid=g, layer_grids={"F.Cu": g},
             sheet_current={"F.Cu": K}, sheet_vectors={}, layer_share={"F.Cu": 1.0}, transfers=[], route_xy=[],
             map_frequency=1e6, error="", main_layers=lambda: ["F.Cu"], switch_pins=[], view_of=None)


def _draw(g, K, scale="log"):
    """The figure of map K drawn as the plugin draws it, and its map's axes."""
    fig = Figure(figsize=(8, 5), dpi=100)
    FigureCanvasAgg(fig)
    draw_heatmap(fig, BOARD, _view(g, K), scale=scale, vectors=False)
    fig.canvas.draw()
    return fig, fig.axes[0]


def _rgb(fig, ax, x, y):
    """The drawn colour (0-255) at board point (x, y) mm."""
    px, py = ax.transData.transform((x, y))
    buf = np.asarray(fig.canvas.buffer_rgba())
    return np.array(buf[int(round(buf.shape[0] - py)), int(round(px)), :3], float)


@pytest.mark.parametrize("scale", ["log", "linear"])
def test_each_node_is_drawn_at_its_place_in_its_value_s_colour(scale):
    """Every node a different value (rising along x, faster along y): sampled at asymmetric nodes, near the corners
    and inside, the drawn colour is the colour map's at that node's value under the scale, so the image is neither
    transposed, flipped nor shifted, and its colour scale is the one the colour bar shows."""
    g = _grid()
    i, j = np.meshgrid(np.arange(g.nx), np.arange(g.ny), indexing="ij")
    K = 1.0 + i + 25.0 * j                                    # 1 ... 244, every node its own
    fig, ax = _draw(g, K, scale)
    peak = float(K.max())
    norm = LogNorm(peak * 1e-3, peak) if scale == "log" else Normalize(0.0, peak)
    for i0, j0 in ((1, 1), (18, 1), (1, 8), (17, 7), (6, 3), (12, 9)):
        want = np.array(colormaps["inferno"](float(norm(K[i0, j0])))[:3]) * 255
        got = _rgb(fig, ax, i0 * g.d, j0 * g.d)
        assert np.abs(got - want).max() <= 3, (scale, i0, j0, got, want)


def test_copper_without_current_is_the_scale_s_lowest_colour():
    """On the log scale a node at 0 (idle copper, another net's) was masked as invalid and drawn transparent: the
    figure's white, beside copper at a millionth of the peak in black. Now both are the lowest colour, on both
    scales; copper's absence stays the grey it is."""
    g = _grid(holes=[(10, 5)])
    K = np.full((g.nx, g.ny), 10.0)
    K[0:5, :] = 0.0
    K[15:20, :] = 1e-6
    lowest = np.array(colormaps["inferno"](0.0)[:3]) * 255
    for scale in ("log", "linear"):
        fig, ax = _draw(g, K, scale)
        assert np.abs(_rgb(fig, ax, 2.0, 5.0) - lowest).max() <= 3, scale            # no current
        assert np.abs(_rgb(fig, ax, 17.0, 5.0) - lowest).max() <= 3, scale           # almost none
        hole = _rgb(fig, ax, 10.0, 5.0)
        assert np.ptp(hole) <= 3 and 200 <= hole[0] <= 245, scale                     # no copper: light grey
        assert fig._gt_maps[ax][2][2, 5] == 0.0                                       # the readout says 0


def test_a_non_finite_value_is_blank_and_counted_not_the_whole_figure():
    """An inf set the colour scale's peak to inf ("Invalid vmin or vmax": no figure at all), a NaN was blank without
    a word. Now either is left blank and counted on its map's title; the other nodes keep their colours, the
    scale its peak; a map with no finite value at all still draws."""
    g = _grid()
    K = np.full((g.nx, g.ny), 10.0)
    K[3, 3], K[4, 4] = np.inf, np.nan
    fig, ax = _draw(g, K)
    assert "2 value(s) not finite: blank" in ax.get_title()
    top = np.array(colormaps["inferno"](1.0)[:3]) * 255
    assert np.abs(_rgb(fig, ax, 10.0, 5.0) - top).max() <= 3                         # the finite peak's colour
    fig, ax = _draw(g, np.full((g.nx, g.ny), np.nan))
    assert "not finite" in ax.get_title()


def test_a_node_where_current_enters_reads_its_strips_not_their_cancelling_mean():
    """1 A into the middle node of a plane, out at four nodes near its corners: each of the middle node's four strips
    carries 0.25 A outwards. Their mean (what route, hot-loop and sharper maps used) cancels to 0, a dark hole where
    the density peaks; the heat-consistent value (node_field's copper=True, the DC maps') is the strips' heat
    over the node: |K|² = Σ ½|I|² / d² = 4 · ½ · 0.25² / d², √(1/8) / d [Tellegen 1952]."""
    d, t = 0.25, 35e-6
    g = build_grid({"GND": box(0, 0, 10, 10)}, (0, 0, 10, 10), d)
    st = PlaneStack([PlaneLayer("F.Cu", 0.0, g, t)], [])
    c = (0, g.nx // 2, g.ny // 2)
    out = [(0, 2, 2), (0, g.nx - 3, 2), (0, 2, g.ny - 3), (0, g.nx - 3, g.ny - 3)]
    sol = RouteExcitation(st, [], injections=[(c, 1.0)] + [(k, -0.25) for k in out]).solve(10.0)
    i, j = c[1], c[2]
    assert sol.sheet_current(0, d)[i, j] == pytest.approx(0.0, abs=1e-6)              # the mean: a hole
    heat = sol.sheet_current(0, d, True)
    assert heat[i, j] == pytest.approx(math.sqrt(1 / 8) / (d * 1e-3), rel=1e-6)
    assert heat[i, j] > heat[i + 1, j] > heat[i + 2, j]                               # highest where it enters
    # away from the feed, in a uniform stretch, both definitions agree (full copper: per copper = per cell width)
    assert sol.sheet_current(0, d, True)[i, 5] == pytest.approx(sol.sheet_current(0, d)[i, 5], rel=0.02)


def test_a_route_s_return_map_has_no_hole_at_the_vias_carrying_it(tmp_path):
    """A signal route changing layers (boardgen.layer_change_board with a stitching via beside the change): its return
    current crosses between the planes through the via, entering each plane at the via's node and spreading. The
    route's map is the heat-consistent one: the via's node reads at least as much as the least of its neighbours,
    not the near-0 of the cancelling mean."""
    from boardgen import layer_change_board
    from conftest import make
    from groundtruth.analysis.simulate import simulate
    board, sc, _ = make(tmp_path, layer_change_board(1.0), "via", solve_points=1)
    rr = simulate(board, sc.config).routes[0]
    vias = [t for t in rr.transfers if t["kind"] == "via" and t["fraction"] > 0.2]
    assert vias
    checked = 0
    for t in vias:
        for layer in t["layers"]:
            g, K = rr.layer_grids.get(layer), rr.sheet_current.get(layer)
            if g is None or K is None:
                continue
            i, j = int(round((t["x"] - g.x0) / g.d)), int(round((t["y"] - g.y0) / g.d))
            if not g.node[i, j]:
                continue
            around = [K[i + di, j + dj] for di, dj in ((1, 0), (-1, 0), (0, 1), (0, -1)) if g.node[i + di, j + dj]]
            assert K[i, j] >= 0.9 * min(around), (layer, K[i, j], around)
            checked += 1
    assert checked
