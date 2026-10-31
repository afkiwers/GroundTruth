"""Draw hot loop on board (results/hotloop.py, kipy_sink.draw_hot_loop): a power stage's hot loop from its Simulate's
result, as an outline and one path per capacitor (the user: "draw a contour about the actual hot loop on layer
User.1")."""
from dataclasses import replace
from types import SimpleNamespace

import pytest
from shapely.geometry import Point, Polygon

from boardgen import discrete_buck_board
from conftest import make


@pytest.fixture(scope="module")
def buck(tmp_path_factory):
    """(board, the discrete buck's ConverterResult) of one Simulate."""
    from groundtruth.analysis.detect import detect_converters
    from groundtruth.analysis.simulate import simulate
    tmp = tmp_path_factory.mktemp("hotloop")
    board, sc, _ = make(tmp, discrete_buck_board(), "buck", nets=(), solve_points=1)
    spec = replace(detect_converters(board, sc.config)[0].spec, i_out=3.0, v_in=24.0, v_out=5.0, rise_time=5e-9,
                   c_oss=300e-12)
    (stage,) = simulate(board, replace(sc.config, converters=[spec])).converters
    assert not stage.error
    return board, stage


def test_the_hot_loop_encloses_its_switches_and_the_capacitors_carrying_its_current(buck):
    """The outline holds every pad of both switches on the loop's nets and of each capacitor carrying at least 5 %
    of the loop current; each such capacitor gets a path from its rail pad through the switches to its ground pad
    and its share written below it; the title gives the loop inductance; the drawn shares add up."""
    from groundtruth.results.hotloop import MIN_SHARE, OUTLINE_MM, PATH_MM, can_draw, hot_loop_drawing
    board, rr = buck
    assert can_draw(rr) == ""
    d = hot_loop_drawing(board, rr)
    (outline,) = [pts for pts, w, closed in d.lines if closed]
    assert [w for _, w, closed in d.lines if closed] == [OUTLINE_MM]
    hull = Polygon(outline).buffer(1e-6)
    assert d.area_mm2 == pytest.approx(Polygon(outline).area, rel=0.02)
    for ref in (rr.driver, rr.receiver):
        for p in board.footprint(ref).pads:
            if p.net:
                assert hull.contains(Point(p.position)), (ref, p.number)
    caps = [c for c in rr.caps if c["share_hf"] >= MIN_SHARE]
    assert caps and d.share == pytest.approx(sum(c["share_hf"] for c in caps))
    paths = [pts for pts, w, closed in d.lines if not closed]
    assert len(paths) == len(caps) and all(w == PATH_MM for _, w, closed in d.lines if not closed)
    for c, pts in zip(caps, paths):
        fp = board.footprint(c["name"])
        for p in fp.pads:
            assert hull.contains(Point(p.position))
        pads = {p.position for p in fp.pads}
        assert len(pts) == 6 and pts[0] in pads and pts[-1] in pads and pts[0] != pts[-1]   # rail pad → … → ground
    assert any(f"{rr.loop_nh:.2f} nH" in t for _, _, t, _ in d.texts)
    assert sum(t.endswith("%") for _, _, t, _ in d.texts) == len(caps)


def test_what_cannot_be_drawn_says_why(buck):
    """Before a Simulate, a view that is no power stage, and a stage whose parts share no switch node: each with
    its reason (the button's tooltip, the status line)."""
    from groundtruth.results.hotloop import can_draw, hot_loop_drawing
    board, rr = buck
    assert can_draw(None) == "Please run the simulation first"
    assert "power stage" in can_draw(SimpleNamespace(kind="route", net="SPI"))
    assert "DC paths" in can_draw(SimpleNamespace(kind="converter", net="x", view_of="DC"))
    cap = rr.caps[0]["name"]
    with pytest.raises(ValueError, match="no hot loop to draw"):
        hot_loop_drawing(board, SimpleNamespace(**{**vars(rr), "receiver": cap}))


def test_the_hot_loop_goes_on_the_board_as_one_group(buck):
    """kipy_sink.draw_hot_loop: the drawings' lines as segments (a closed outline back to its start), the texts,
    one group that Clear GT markers removes and the next drawing replaces."""
    pytest.importorskip("kipy")
    from groundtruth.adapters import kipy_sink
    from groundtruth.results.hotloop import hot_loop_drawing
    from kipy.util.board_layer import layer_from_canonical_name

    class Board:
        """The few KiCad board calls draw_hot_loop makes."""
        def __init__(self):
            self.items, self.groups = [], []
            self.enabled = [layer_from_canonical_name(n) for n in ("F.Cu", "B.Cu", "User.1")]

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

    board, rr = buck
    d = hot_loop_drawing(board, rr)
    b = Board()
    n = kipy_sink.draw_hot_loop(b, [d], "User.1")
    segs = [i for i in b.items if type(i).__name__ == "BoardSegment"]
    texts = [i for i in b.items if type(i).__name__ == "BoardText"]
    assert len(segs) == sum(len(pts) - (0 if closed else 1) for pts, _, closed in d.lines)
    assert len(texts) == len(d.texts) and n == len(segs) + len(texts)
    assert [g.name for g in b.groups] == ["groundtruth-hotloop"]
    kipy_sink.draw_hot_loop(b, [d], "User.1")
    assert [g.name for g in b.groups] == ["groundtruth-hotloop"]                  # replaced, not added
