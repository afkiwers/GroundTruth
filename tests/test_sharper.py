"""The Results tab's "Sharper map" (analysis/sharper.py): a view's own model solved again on a finer grid. The
user: "the individual layer view need a much higher res heatmap". Same physics, finer grid: the current that
crosses the plane must stay the same, and nothing outside the maps changes."""
import numpy as np
import pytest

from boardgen import discrete_buck_board, microstrip_board
from conftest import make
from groundtruth.analysis.detect import detect_converters
from groundtruth.analysis.power import dc_view
from groundtruth.analysis.sharper import SharperError, describe, sharper_view
from groundtruth.analysis.simulate import simulate


def _return_across(view, layer: str, x_mm: float) -> float:
    """|∫ Kx dy| across the plane at x (A per A of signal): the return current crossing that cut."""
    g = view.layer_grids[layer]
    kx, _ = view.sheet_vectors[layer]
    x0 = g.extent[0]
    i = int(round((x_mm - x0) / g.d))
    return abs(float(np.nansum(np.where(g.node[i], kx[i], 0.0)))) * g.d * 1e-3


def test_a_sharper_route_map_has_half_the_cell_and_the_same_return_current(tmp_path):
    board, sc, _ = make(tmp_path, microstrip_board(), "ms", solve_points=1)
    view = simulate(board, sc.config).routes[0]
    sharp = sharper_view(board, sc.config, view)
    d0, d1 = view.grid.d, sharp.grid.d
    assert 0.4 * d0 < d1 < 0.65 * d0                          # 4x the nodes: about half the pitch
    for layer in view.layer_grids:
        assert sharp.sheet_current[layer].shape[0] > 1.6 * view.sheet_current[layer].shape[0]
        assert abs(sharp.layer_share[layer] - view.layer_share[layer]) < 0.05
    plane = max(view.layer_share, key=view.layer_share.get)
    coarse, fine = _return_across(view, plane, 20.0), _return_across(sharp, plane, 20.0)
    assert fine == pytest.approx(coarse, rel=0.1) and 0.7 < fine < 1.1   # ~1 A returns under a 1 A signal
    assert view.grid.d == d0 and not getattr(view, "sharper", None)   # the original view is left alone
    assert sharp.worst == view.worst and sharp.solves == view.solves   # the numbers stay as analysed
    assert describe(sharp).startswith(f"sharper map: {d1:.2f} mm grid (was {d0:.2f} mm)")


def test_a_power_stage_and_its_dc_path_get_sharper_maps(tmp_path):
    board, sc, _ = make(tmp_path, discrete_buck_board(out_connector=True), "buck", nets=(), solve_points=1)
    sc.add_detected_converters([d.spec for d in detect_converters(board, sc.config)])
    c = simulate(board, sc.config).converters[0]
    assert c.dc, "no DC path modelled"
    for view in (c, dc_view(c, c.dc[0]["name"])):
        sharp = sharper_view(board, sc.config, view)
        assert sharp.grid.d < 0.7 * view.grid.d
        assert getattr(sharp, "view_of", None) == getattr(view, "view_of", None)
        assert sum(sharp.layer_share.values()) == pytest.approx(1.0, abs=0.02)


def test_a_view_whose_net_left_the_settings_says_so(tmp_path):
    board, sc, _ = make(tmp_path, microstrip_board(), "gone", solve_points=1)
    view = simulate(board, sc.config).routes[0]
    from dataclasses import replace
    with pytest.raises(SharperError, match="no longer in the settings"):
        sharper_view(board, replace(sc.config, nets=[]), view)
