"""The report's interactive board view (report/boardview.py, in the manner of InteractiveHtmlBom): the board's copper,
parts and the analysis results as data in the page, issues numbered as in the report's Issues table."""
import json
import re
import shutil
import subprocess

import pytest

from boardgen import layer_change_board
from conftest import make
from groundtruth.analysis.simulate import simulate
from groundtruth.config.settings import NetSpec
from groundtruth.report.boardview import BOARD_JS, board_data, board_view, ordered_issues
from groundtruth.report.html import render_html


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("bv")
    board, sc, _ = make(tmp, layer_change_board(None), "bv", nets=(NetSpec("SIG", "U1.1", ["U2.1"], 50e6, 1e-9),),
                        solve_points=2, max_solves=3, capacitance=False)
    return board, sc.config, simulate(board, sc.config)


def test_the_board_its_parts_and_results_are_in_the_data(run):
    board, cfg, res = run
    d = board_data(board, res.routes, res.issues, cfg)
    assert d["layers"] == board.copper_layers and d["edges"]
    refs = [p["ref"] for p in d["parts"]]
    assert sorted(refs) == sorted(fp.reference for fp in board.footprints if fp.reference)
    u1 = next(p for p in d["parts"] if p["ref"] == "U1")
    assert u1["pads"] and u1["side"] == "F" and u1["bb"][0] <= u1["bb"][2]
    assert sum(len(v) for v in d["tracks"].values()) >= len(board.tracks)
    assert len(d["vias"]) == len(board.vias) and d["zones"]
    route, = d["routes"]
    assert route["name"] == "SIG" and route["segs"] and set(route["parts"]) == {"U1", "U2"}
    # issues: numbered as the Issues table numbers them, with the parts they concern
    order = ordered_issues(res.issues)
    assert [i["n"] for i in d["issues"]] == list(range(1, len(order) + 1))
    assert [(i["check"], i["x"]) for i in d["issues"]] == [(i.check, round(i.location[0], 2)) for i in order]


def test_the_parts_are_sorted_as_ibom_sorts_them(run):
    board, cfg, res = run
    from groundtruth.report.boardview import _natural
    assert sorted(["C10", "C2", "R1", "C1"], key=_natural) == ["C1", "C2", "C10", "R1"]


def test_the_view_is_in_the_report_and_its_data_cannot_end_its_script(run):
    board, cfg, res = run
    page = render_html(board, res.issues, res.routes, "simulation", config=cfg)
    assert "id='board'" in page and 'id="bv-data"' in page and "bv-canvas" in page
    assert "Analysed routes and issues" not in page                     # the static overview it replaces
    res.issues[0].message = "a </script><script>alert(1)</script> message"
    html = board_view(board, res.routes, res.issues, cfg)
    data = re.search(r'<script type="application/json" id="bv-data">(.*?)</script>', html, re.S).group(1)
    assert json.loads(data)["issues"]                                   # the whole data, not cut at the message
    assert "</script>" not in data


@pytest.mark.skipif(shutil.which("node") is None, reason="no Node.js to check the script's syntax")
def test_the_viewer_script_parses(tmp_path):
    f = tmp_path / "bv.js"
    f.write_text(BOARD_JS, encoding="utf-8")
    subprocess.run(["node", "--check", str(f)], check=True)
