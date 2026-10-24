"""The contour map drawn on the board (results/contours.py): round levels, isolines against an analytic field, labels
along the lines with gaps, the copper mask, the segment budget, and the KiCad drawing (one group, rotated labels)."""
import math
from types import SimpleNamespace

import numpy as np
import pytest

from groundtruth.results.contours import contour_map, linear_levels, log_levels, short_value


def _grid(n=161, d=0.1, x0=0.0, y0=0.0, node=None):
    """A square stand-in for the solver's Grid: n × n nodes, d mm apart, all copper unless `node` says otherwise."""
    node = np.ones((n, n), bool) if node is None else node
    return SimpleNamespace(x0=x0, y0=y0, d=d, nx=n, ny=n, node=node,
                           extent=(x0 - d / 2, x0 + (n - 0.5) * d, y0 - d / 2, y0 + (n - 0.5) * d))


def _radial(g, cx=8.0, cy=8.0):
    """K = 10 / r (mm) around (cx, cy), indexed [x, y]: its contour at level v is the circle r = 10 / v."""
    x = g.x0 + np.arange(g.nx) * g.d
    y = g.y0 + np.arange(g.ny) * g.d
    X, Y = np.meshgrid(x, y, indexing="ij")
    return 10.0 / np.maximum(np.hypot(X - cx, Y - cy), 0.05)


def test_levels_are_round_like_a_maps():
    """1-2-5 steps over three decades below the peak, the decades bold; temperatures in round steps from the
    ambient up, every fifth (10 °C, 20 °C) bold; short labels."""
    assert log_levels(4600) == [(2000, False), (1000, True), (500, False), (200, False), (100, True), (50, False),
                                (20, False), (10, True), (5, False)]
    assert [v for v, _ in log_levels(1.0)] == [0.5, 0.2, 0.1, 0.05, 0.02, 0.01, 0.005, 0.002, 0.001]
    levels, step = linear_levels(25.0, 87.3)
    assert step == 5 and [v for v, _ in levels] == [30, 35, 40, 45, 50, 55, 60, 65, 70, 75, 80, 85]
    assert [v for v, i in levels if i] == [50, 75]
    assert [short_value(v) for v in (2000, 1500, 500, 20, 0.5, 0.002, 2.5e6)] == \
        ["2k", "1.5k", "500", "20", "0.5", "0.002", "2.5M"]


def test_isolines_of_a_radial_field_are_circles_with_their_values_written_along_them():
    """K = 10 / r: each line lies on its circle r = 10 / level (within a quarter cell: marching squares on the
    grid, then smoothing), labels sit on their lines, read along them (tangent: at right angles to the radius) and
    upright, and the line is broken where its label sits."""
    g = _grid()
    K = _radial(g)
    cm = contour_map(g, K, unit="A/m", what="test")
    assert cm.levels == [100, 50, 20, 10, 5, 2, 1, 0.5, 0.2]          # peak 200 at the centre, three decades
    assert {v for v, _, _ in cm.lines} == {100, 50, 20, 10, 5, 2, 1}  # 0.5 would be at r 20 mm: off the grid
    for value, _, pts in cm.lines:
        r = np.hypot(pts[:, 0] - 8.0, pts[:, 1] - 8.0)
        if 10 / value > 0.5:                               # circles of a few cells are polygons, not circles
            assert np.max(np.abs(r - 10 / value)) < 0.25 * g.d + 0.02 * (10 / value), value
    assert cm.labels and {t for *_, t in cm.labels} >= {"10", "5", "2"}
    for x, y, ang, text in cm.labels:
        v = float(text)
        assert abs(math.hypot(x - 8, y - 8) - 10 / v) < 0.05 + 0.03 * (10 / v)       # on its circle
        assert -90 < ang <= 90                                                         # upright
        radius_dir = math.degrees(math.atan2(-(y - 8), x - 8))                         # as seen, y down
        off = (ang - radius_dir) % 180
        assert abs(off - 90) < 12, (text, ang, radius_dir)                              # along the circle
        # the line has a gap where the label sits: no point of its own line within half the label's width
        w = (0.75 * len(text) + 0.6) * cm.text_mm
        mine = np.vstack([p for val, _, p in cm.lines if val == v])
        assert np.min(np.hypot(mine[:, 0] - x, mine[:, 1] - y)) > 0.4 * w
    # the 2 A/m circle (r 5 mm, 31 mm round) is long enough for two labels at least, none overlapping
    assert sum(1 for *_, t in cm.labels if t == "2") >= 2
    pts = [(x, y) for x, y, *_ in cm.labels]
    assert min(math.dist(a, b) for k, a in enumerate(pts) for b in pts[k + 1:]) > cm.text_mm
    assert "test: 1-2-5 steps" in cm.legend and "peak 200 A/m" in cm.legend


def test_lines_stop_at_the_copper_edge_and_follow_the_heatmaps_peak():
    """No line runs off the copper (the field is masked there, not zero: zero drew every copper edge once per
    level), and the peak over all of a result's layers sets the levels, so a quiet layer gets few lines."""
    g0 = _grid()
    node = np.ones((g0.nx, g0.ny), bool)
    node[100:, :] = False                                  # no copper right of x = 10 mm
    g = _grid(node=node)
    cm = contour_map(g, _radial(g))
    assert cm.lines and max(float(p[:, 0].max()) for _, _, p in cm.lines) <= 9.9 + 1e-6
    quiet = contour_map(g, _radial(g) * 0.01, peak=200.0)  # this layer peaks at 2 of the result's 200
    assert quiet.levels == [1, 0.5, 0.2] and all(v < 2 for v in quiet.levels)


def test_isotherms_and_the_segment_budget():
    """A temperature map gets isotherms in °C from the ambient up; a tight segment budget coarsens the lines
    (simplified harder) instead of drawing more than KiCad should take."""
    g = _grid()
    T = 25 + 3 * _radial(g) ** 0.5                         # 25 °C far away, about 67 °C at the centre
    cm = contour_map(g, T, scale="linear", low=25.0, unit="°C", what="isotherms")
    assert cm.levels[0] > 25 and all(round(v) == v for v in cm.levels) and "every 5 °C" in cm.legend
    full = contour_map(g, _radial(g))
    tight = contour_map(g, _radial(g), max_segments=150)
    assert tight.segment_count() < full.segment_count() and tight.segment_count() <= 200


def test_the_map_on_the_board_is_one_group_of_segments_and_turned_labels():
    """draw_contours: every line piece as segments (index lines thicker), each label centred and turned along its
    line, a legend; one group that Clear GT markers removes."""
    pytest.importorskip("kipy")
    from groundtruth.adapters import kipy_sink
    from kipy.util.board_layer import layer_from_canonical_name

    class Board:
        """The few KiCad board calls draw_contours makes."""
        def __init__(self):
            self.items, self.groups = [], []
            self.enabled = [layer_from_canonical_name(n) for n in ("F.Cu", "B.Cu", "User.7")]

        def get_enabled_layers(self):
            return self.enabled

        def get_groups(self):
            return self.groups

        def begin_commit(self):
            return "c"

        def create_items(self, items):
            if not isinstance(items, list):
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

        def remove_items(self, groups):
            self.groups = [g for g in self.groups if g not in groups]

    g = _grid()
    cm = contour_map(g, _radial(g), what="test")
    b = Board()
    lines, labels = kipy_sink.draw_contours(b, cm, "User.7", "test")
    assert (lines, labels) == (len(cm.lines), len(cm.labels)) and [x.name for x in b.groups] == ["groundtruth-current"]
    segs = [i for i in b.items if type(i).__name__ == "BoardSegment"]
    texts = [i for i in b.items if type(i).__name__ == "BoardText"]
    assert len(segs) == cm.segment_count() and len(texts) == len(cm.labels) + 1        # + the legend
    widths = {round(s.attributes.stroke.width / kipy_sink.MM, 3) for s in segs}
    assert widths == {kipy_sink.INDEX_LINE_MM, kipy_sink.CONTOUR_LINE_MM}
    x, y, ang, text = cm.labels[0]
    t = next(t for t in texts if t.value == text)
    assert t.attributes.angle == pytest.approx(ang)


def test_the_contour_line_widths_come_from_the_preferences():
    """Preferences' Contour line / Index contour line: 0.04 and 0.1 mm by default, out-of-range values fall back."""
    from groundtruth.ui.prefs_dialog import _contour_widths
    assert _contour_widths({}) == {"contour_line_mm": 0.04, "contour_index_mm": 0.1}
    assert _contour_widths({"contour_line_mm": 0.08, "contour_index_mm": 0.25}) == \
        {"contour_line_mm": 0.08, "contour_index_mm": 0.25}
    assert _contour_widths({"contour_line_mm": 0, "contour_index_mm": "x"}) == \
        {"contour_line_mm": 0.04, "contour_index_mm": 0.1}
