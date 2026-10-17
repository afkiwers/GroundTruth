"""The report's images at the resolution their models have (the user: "when exporting a report or in history, make
sure you export the highest resolution images available"): each map drawn with at least PX_PER_CELL pixels per
model cell (up to MAP_DPI_MAX), the other figures at DPI, a view sharpened in the plugin drawn instead of its
original, and a click on a figure shows it at full size."""
import base64
import re
import struct

from boardgen import microstrip_board
from conftest import make
from groundtruth.analysis.simulate import simulate
from groundtruth.report import html
from groundtruth.report.plots import PX_PER_CELL, heatmap_dpi


def png_width(page: str, n: int = 0) -> int:
    """The pixel width of the n-th PNG embedded in a page (from its IHDR header)."""
    data = base64.b64decode(re.findall(r"data:image/png;base64,([A-Za-z0-9+/=]+)", page)[n])
    return struct.unpack(">I", data[16:20])[0]


def test_a_map_gets_a_pixel_per_cell_and_more(tmp_path):
    """A coarse model stays at the plots' DPI; a model with many cells gets the DPI its cells need (at least
    PX_PER_CELL pixels each across the map), at most the cap."""
    board, sc, _ = make(tmp_path, microstrip_board(), "m")
    rr = simulate(board, sc.config).routes[0]
    size = (9.5, 6.0)
    base = heatmap_dpi(rr, size, 1, 10_000)              # what this grid needs
    nx = max(rr.layer_grids[l].nx for l in rr.main_layers()[:4])
    assert base * size[0] >= PX_PER_CELL * nx * 0.5      # the figure has room for every cell (panels share it)
    assert heatmap_dpi(rr, size, 5_000, 6_000) == 5_000  # never below the base
    assert heatmap_dpi(rr, size, 1, 50) == min(50, base)  # never above the cap


def test_the_report_draws_maps_at_their_resolution_and_a_sharpened_view(tmp_path, monkeypatch):
    """The exported page: the maps' PNGs as wide as their DPI makes them; a view passed as `sharper` is drawn
    instead of the one it replaces (matched by kind, net, receiver and DC path); a click script for full size."""
    board, sc, _ = make(tmp_path, microstrip_board(), "m")
    res = simulate(board, sc.config)
    rr = res.routes[0]
    page = html.render_html(board, res.issues, res.routes, "simulation", config=sc.config)
    want = int(round(html._maps_size(rr)[0] * heatmap_dpi(rr, html._maps_size(rr), html.DPI, html.MAP_DPI_MAX)))
    assert abs(png_width(page) - want) <= 2
    assert "classList.toggle('full')" in page and "cursor:zoom-in" in page
    drawn = []
    real = html.draw_heatmap
    monkeypatch.setattr(html, "draw_heatmap", lambda fig, b, view, *a, **k: (drawn.append(view),
                                                                            real(fig, b, view, *a, **k)))
    import copy
    sharp = copy.copy(rr)
    sharp.sharper = {"d_mm": 0.1}
    html.render_html(board, res.issues, res.routes, "simulation", config=sc.config, sharper=[sharp])
    assert drawn and drawn[0] is sharp
