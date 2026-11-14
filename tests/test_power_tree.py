"""The board's power tree (analysis/power_tree.py) and its drawing (report/power_tree.py): on the buck test board
with a surge stopper, an output connector and a linear regulator after the buck, the chain from the input connector
through the pass FET, the buck and the LDO to the IC, each rail's capacitors and loads, the drawing's layout (each
box after what feeds it, nothing overlapping) in both themes, and the window."""
import pytest

from boardgen import discrete_buck_board
from conftest import make
from groundtruth.analysis.power_tree import power_tree
from groundtruth.config.settings import ConverterSpec


def _tree_board(tmp_path):
    """The buck board (J1 -> VBATT -> Q9 -> VIN -> buck -> VOUT -> J2 and U10 -> V18 -> U11) with its stage."""
    b, sc, _ = make(tmp_path, discrete_buck_board(out_connector=True, surge_fet=True, ldo=True), "tree", nets=())
    cfg = sc.config
    cfg.converters = [ConverterSpec("buck", "VIN", "GND", switch_node="SW", high_side=["Q1"], low_side=["Q2"],
                                    f_sw=500e3, i_out=2.0, v_in=12.0, v_out=3.3, input_connector=["J1.1", "J1.2"],
                                    output_connector=["J2.1", "J2.2"])]
    return b, cfg


def test_the_chain_from_the_connector_to_the_ic(tmp_path):
    """J1 feeds VBATT; the pass FET Q9 links VBATT to VIN (oriented from the source side); the buck VIN -> VOUT with
    its V out; the LDO U10 VOUT -> V18; U11 a load of V18, the output connector J2 one of VOUT; C10 on V18 as
    1 × 1 µF; every rail fed."""
    b, cfg = _tree_board(tmp_path)
    t = power_tree(b, cfg)
    edges = {(a, c) for a, c, _ in t.edges}
    assert ("src:J1", "rail:VBATT") in edges
    assert ("rail:VBATT", "link:Q9") in edges and ("link:Q9", "rail:VIN") in edges
    assert ("rail:VIN", "smps:buck") in edges and ("smps:buck", "rail:VOUT") in edges
    assert ("rail:VOUT", "ldo:U10") in edges and ("ldo:U10", "rail:V18") in edges
    assert ("rail:V18", "load:V18:U11") in edges and ("rail:VOUT", "load:VOUT:J2") in edges
    caps = [n for n in t.nodes if n.kind == "caps" and n.net == "V18"]
    assert len(caps) == 1 and caps[0].refs == ["C10"] and caps[0].lines[0].startswith("1 × 1")
    assert t.node("smps:buck").lines[0] == "3.3V out"
    assert t.node("rail:VOUT").lines == ["3.3V"]
    assert not t.notes, t.notes
    assert "SW" not in {n.net for n in t.nodes if n.kind == "rail"}          # a stage's switch node is no rail


@pytest.mark.parametrize("dark", [False, True])
def test_the_drawing(tmp_path, dark):
    """Every box after what feeds it (left to right), a rail's loads and capacitors under it, no two boxes
    overlapping; a PNG and an SVG in either theme."""
    from groundtruth.report.power_tree import figure, image, layout
    b, cfg = _tree_board(tmp_path)
    t = power_tree(b, cfg)
    pos = layout(t)
    assert set(pos) == {n.id for n in t.nodes}
    kind = {n.id: n.kind for n in t.nodes}
    for a, c, _ in t.edges:
        if kind[c] in ("load", "caps"):
            assert pos[c][1] > pos[a][1] and pos[c][0] > pos[a][0]          # under its rail, indented
        else:
            assert pos[c][0] > pos[a][0] + pos[a][2]                         # in a column to the right
    boxes = list(pos.values())
    for i, (x, y, w, h) in enumerate(boxes):
        for x2, y2, w2, h2 in boxes[i + 1:]:
            assert x + w <= x2 or x2 + w2 <= x or y + h <= y2 or y2 + h2 <= y, "boxes overlap"
    assert image(t, "png", dark).startswith(b"\x89PNG")
    assert b"<svg" in image(t, "svg", dark)[:400]
    assert figure(type(t)()) is not None                                     # an empty tree: a note, no error


@pytest.fixture(scope="module")
def wx_app():
    """A wx.App for the window (skipped without wxPython or a display)."""
    wx = pytest.importorskip("wx")
    try:
        a = wx.App(False)
    except Exception as e:   # no display (CI)
        pytest.skip(f"no display for wx: {e}")
    yield a
    a.Destroy()


def test_the_window(tmp_path, wx_app):
    """Power tree…: the window draws in its worker and shows the PNG; Fit toggles."""
    import wx

    from groundtruth.ui.power_tree_window import PowerTreeWindow
    b, cfg = _tree_board(tmp_path)
    frame = wx.Frame(None)
    win = PowerTreeWindow(frame, lambda: (b, cfg, None, "tree"))
    for _ in range(400):
        wx.Yield()
        if win.png is not None:
            break
        wx.MilliSleep(20)
    assert win.png is not None and win.tree is not None
    assert "rails" in win.status.GetLabel()
    win._toggle_fit()
    assert not win.fit
    win.Destroy()
    frame.Destroy()


def test_the_report_has_the_power_tree(tmp_path):
    """The HTML report's Power tree part (with the stages of a run): the flow chart embedded, in the contents."""
    from groundtruth.report.html import render_html
    b, cfg = _tree_board(tmp_path)
    page = render_html(b, [], [], "file", config=cfg, converters=[])
    assert "id='powertree'" not in page                      # nothing analysed: no part
    from types import SimpleNamespace
    stage = SimpleNamespace(net="buck", bridge=None, dc=[], efficiency=None)
    from groundtruth.report import html
    part = html._power_tree(b, cfg, [stage])
    assert "data:image/png;base64," in part and "power tree" in part
