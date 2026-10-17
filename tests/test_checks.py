import pytest

from boardgen import BoardGen, layer_change_board, microstrip_board
from conftest import make
from groundtruth.analysis.route import find_route
from groundtruth.config.settings import NetSpec
from groundtruth.rules.checks import run_checks


def checks_of(result):
    return sorted((i.check, i.severity) for i in result.issues)


def test_solid_plane_has_no_issues(solid):
    board, sc, _ = solid
    assert run_checks(board, sc.config).issues == []


@pytest.mark.parametrize("layers,plane", [(2, "B.Cu"), (4, "In1.Cu")])
def test_slot_is_found_at_the_right_place(tmp_path, layers, plane):
    board, sc, _ = make(tmp_path, microstrip_board(slot=(19.5, 4, 20.5, 16), layers=layers))
    res = run_checks(board, sc.config)
    assert checks_of(res) == [("gap_crossing", "error")]
    issue = res.issues[0]
    assert issue.location == pytest.approx((20.0, 10.0))
    assert issue.length_mm == pytest.approx(1.0, abs=0.01)
    assert plane in issue.message
    assert issue.line > 0 and issue.items


@pytest.mark.parametrize("stitch,expected", [(None, [("layer_change_stitch", "error")]), (1.0, []),
                                             (5.0, [("layer_change_stitch", "error")])])
def test_layer_change_needs_stitching_via(tmp_path, stitch, expected):
    board, sc, _ = make(tmp_path, layer_change_board(stitch))
    assert checks_of(run_checks(board, sc.config)) == expected


def test_reference_net_change_needs_decap(tmp_path):
    board, sc, _ = make(tmp_path, layer_change_board(None, "+3V3"))
    assert ("layer_change_decap", "warning") in checks_of(run_checks(board, sc.config))
    board, sc, _ = make(tmp_path, layer_change_board(None, "+3V3", decap_distance=1.0), "decap")
    assert ("layer_change_decap", "warning") not in checks_of(run_checks(board, sc.config))


def test_edge_proximity(tmp_path):
    g = microstrip_board()
    g.items = [i for i in g.items if "(zone" not in i]
    g.zone("GND", "B.Cu", [(0.3, 0.3), (39.7, 0.3), (39.7, 10.5), (0.3, 10.5)])  # plane edge 0.5 mm from trace
    board, sc, _ = make(tmp_path, g)
    assert ("edge_proximity", "warning") in checks_of(run_checks(board, sc.config))


def test_route_through_series_resistor(tmp_path):
    g = BoardGen(size=(40.0, 20.0))
    g.ic("U1", (5.0, 10.0), "A")
    g.ic("U2", (35.0, 10.0), "B")
    g.two_pin("R1", "33R", (20.0, 10.0), "A", "B")
    g.track((5.0, 10.0), (19.5, 10.0), "A")
    g.track((20.5, 10.0), (35.0, 10.0), "B")
    g.plane("GND", "B.Cu")
    board, sc, _ = make(tmp_path, g, nets=(NetSpec("A", "U1.1", ["U2.1"], 1e7, 2e-9),))
    route = find_route(board, sc.config, "A", "U1.1", "U2.1")
    assert route.parts == ["R1"] and route.length == pytest.approx(30.0, abs=0.01)


def test_waiver_marks_issue(slotted):
    board, sc, _ = slotted
    fp = run_checks(board, sc.config).issues[0].fingerprint
    sc.add_waiver(fp, "accepted: low speed")
    issue = run_checks(board, sc.config).issues[0]
    assert issue.waived and issue.waiver_reason == "accepted: low speed"


def test_missing_route_is_reported(tmp_path):
    g = microstrip_board()
    g.items = [i for i in g.items if '(net "SIG")' not in i or "(footprint" in i]   # delete the trace
    board, sc, _ = make(tmp_path, g)
    assert checks_of(run_checks(board, sc.config)) == [("route_missing", "error")]


def test_a_track_ending_just_short_of_a_pad_still_reaches_it():
    """Copper that overlaps is connected (KiCad's rule): a 0.2 mm track whose centre line stops 0.05 mm before R1's
    pad, its round end over the pad. Testing only the end point left kraft_flux_mini_2's /CPU/ADC_I_RAIL_1
    without a route (a 0.4 mm track 0.02 mm outside R303's rotated pad)."""
    from boardgen import BoardGen
    from groundtruth.adapters.file_source import loads_board
    from groundtruth.config.settings import Config
    g = BoardGen(layers=2, size=(30.0, 20.0))
    g.ic("U1", (5.0, 10.0), "A", gnd_offset=(0.0, 1.0))
    g.ic("U2", (20.0, 10.0), "B", gnd_offset=(0.0, 1.0))
    g.two_pin("R1", "33", (12.0, 10.0), "A", "B", kind="R")      # pads 0.5 wide at x 11.5 and 12.5
    g.track((5.0, 10.0), (11.2, 10.0), "A")                       # ends 0.05 mm before the pad's edge at 11.25
    g.track((12.5, 10.0), (20.0, 10.0), "B")
    board = loads_board(g.text(), "short.kicad_pcb")
    route = find_route(board, Config(), "A", "U1.1", "U2.1", through=["R1"])
    assert route.parts == ["R1"]
