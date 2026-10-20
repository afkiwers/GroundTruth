"""The 3D view (report/view3d.py): a self-contained, safe page; the route model's layers with the signal and the
carrying vias; with plane capacitance the board-wide model's displacement current and the vias it circulates with."""
import base64
import copy
import json
import re
import shutil
import subprocess
from pathlib import Path
from html.parser import HTMLParser

import numpy as np
import pytest

from boardgen import layer_change_board
from conftest import make
from groundtruth.analysis.simulate import simulate
from groundtruth.config.settings import NetSpec
from groundtruth.report.view3d import CSP, file_stem, has_view, view3d_data, view3d_html, write_view3d


@pytest.fixture(scope="module")
def demo(tmp_path_factory):
    """The demo board's layout (no stitching via, the GND planes joined only at the ICs) with plane capacitance."""
    tmp = tmp_path_factory.mktemp("v3d")
    board, sc, _ = make(tmp, layer_change_board(None), "v3d", nets=(NetSpec("SIG", "U1.1", ["U2.1"], 50e6, 1e-9),),
                        capacitance=True, solve_points=3, max_solves=6, cavity_max_nodes=12000)
    return board, simulate(board, sc.config).routes[0]


class _Tags(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


def _data(page: str) -> dict:
    m = re.search(r'<script type="application/json" id="gt-data">(.*?)</script>', page, re.S)
    return json.loads(m.group(1))


def test_the_page_is_self_contained_and_keeps_board_names_inert(demo):
    """Nothing from the network (a CSP allows only its own inline code and data: images), two scripts (the data and
    the code): a net name that tries to close the script and run its own stays text."""
    board, rr = demo
    rr = copy.copy(rr)
    rr.net = "</script><script>alert(1)</script>"
    page = view3d_html(board, rr)
    p = _Tags()
    p.feed(page)
    assert sum(1 for t, _ in p.tags if t == "script") == 2
    csp = [a for t, a in p.tags if t == "meta" and a.get("http-equiv") == "Content-Security-Policy"]
    assert csp and csp[0]["content"] == CSP and "default-src 'none'" in CSP
    assert not re.search(r"""(src|href)\s*=\s*["']?https?:|url\(\s*["']?https?:|@import""", page)
    assert "<script>alert(1)" not in page
    data = _data(page)
    assert data["modes"][0]["title"].startswith(rr.net)              # round-trips as text
    assert "&lt;/script&gt;" in page.split("<style>")[0]                # the <title>


def test_the_route_model_scene(demo):
    """One sheet per copper layer in stack order, a map image on each plane layer (PNG), the signal on F.Cu and
    B.Cu, its layer change at the via (x = 20 mm) as a grey bar from F.Cu to B.Cu."""
    board, rr = demo
    m = view3d_data(board, rr)["modes"][0]
    assert [s["name"] for s in m["sheets"]] == ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
    for s in m["sheets"]:
        if s["name"] in rr.sheet_current:
            assert base64.b64decode(s["img"].split(",", 1)[1])[:8] == b"\x89PNG\r\n\x1a\n"
    assert {(l["level"], l["colour"]) for l in m["lines"]} >= {(0, "#c83434"), (3, "#34a0c8")}
    via = [b for b in m["bars"] if b["title"].startswith("signal via")]
    assert len(via) == 1 and (via[0]["x"], via[0]["top"], via[0]["bottom"]) == (pytest.approx(20.0), 0, 3)
    assert m["w"] == pytest.approx(rr.grid.extent[1] - rr.grid.extent[0])


def test_the_board_wide_scene_shows_the_displacement_and_the_vias_it_circulates_with(demo):
    """With plane capacitance: the board-wide model at the plane-capacitance transfer's frequency, a displacement
    sheet between In1 and In2, and the ICs' GND vias (x = 5 and 35 mm) carrying the current between the planes,
    which the return-pin rule leaves out of the transfer list. The displacement map is the solution's capacitance
    currents at its dominant phase: summed it gives their net current (at the resonance in phase with the plane
    currents: measured 10.636 of 10.641 A per A), and the two vias carry it back with the 1 A that has to cross. The
    transfers are magnitudes, so KCL bounds them by the triangle inequality, fraction - 1 <= Σ|via| <= fraction + 1:
    equal to fraction - 1 only where the crossing current is in phase with the circulating one (4.84 + 4.82 = 10.64 - 1
    at 1450 MHz). Since the worst harmonic is always solved (VALIDATION_AUDIT.md round 6, ISSUE-32) the maps come from
    the resonance at 1350 MHz the old refinement missed (+5.6 dB margin, not +16.6): 33.12 against 32.35 ± 1."""
    board, rr = demo
    bm = rr.board_maps
    cap = next(t for t in rr.transfers if t["kind"] == "capacitance")
    assert bm["f"] == cap["f"] and ("In1.Cu", "In2.Cu") in bm["displacement"]
    D = bm["displacement"][("In1.Cu", "In2.Cu")]
    g = bm["grids"]["In1.Cu"]
    net = float(D.sum()) * (g.d * 1e-3) ** 2                     # A per A, at the dominant phase
    assert cap["fraction"] > 1 and abs(net) == pytest.approx(cap["fraction"], rel=0.02)
    vias = [t for t in bm["transfers"] if t["kind"] == "via"]
    total = sum(t["fraction"] for t in vias)
    assert 0.98 * (cap["fraction"] - 1) <= total <= 1.02 * (cap["fraction"] + 1)
    m = view3d_data(board, rr)["modes"][1]
    disp = [s for s in m["sheets"] if s.get("displacement")]
    assert len(disp) == 1 and disp[0]["level"] == 1.5           # between In1 (1) and In2 (2)
    xs = sorted(round(b["x"]) for b in m["bars"] if not b["title"].startswith("signal via"))
    assert 5 in xs and 35 in xs
    assert "board-wide model" in m["title"] and "plane displacement" in m["title"]


def test_files_stay_in_their_folder(tmp_path, demo):
    board, rr = demo
    rr = copy.copy(rr)
    rr.net, rr.receiver = "../../..\\evil", "..\\x"
    stem = file_stem(rr)
    assert re.fullmatch(r"[A-Za-z0-9_.-]+", stem) and not stem.startswith(".")
    path = write_view3d(board, rr, tmp_path / "views")
    assert path.parent == tmp_path / "views" and path.name == f"view3d_{stem}.html"
    from types import SimpleNamespace as NS
    assert not has_view(NS(kind="thermal", grid=object(), sheet_current={"F.Cu": np.ones(1)}, error=""))
    assert has_view(rr) and not has_view(NS(kind="", grid=None, sheet_current={}, error=""))


EDGE = Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe")


@pytest.mark.skipif(not (shutil.which("node") and EDGE.exists()), reason="needs node and Microsoft Edge")
def test_the_grabbed_edge_follows_the_mouse(tmp_path, demo):
    """Dragging turns the stack so the edge under the hand moves with the mouse, near half or far half (the
    user: grabbing the front, "it moves as if the user holds the back of it"). tools/dev/view3d_drag.mjs drives
    the real page in headless Edge."""
    board, rr = demo
    page = write_view3d(board, rr, tmp_path / "views")
    tool = Path(__file__).resolve().parents[1] / "tools" / "dev" / "view3d_drag.mjs"
    r = subprocess.run(["node", str(tool), str(EDGE), str(page), str(tmp_path / "edge")], capture_output=True,
                       text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.count("with the mouse") == 2
