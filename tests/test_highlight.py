"""What a clicked issue shows on the board (analysis/highlight.py): the copper it is about, found from the board
model because an issue names at most one segment itself; planes beside it as context; the layer to make active."""
from types import SimpleNamespace as NS

import pytest

from boardgen import discrete_buck_board, layer_change_board, microstrip_board
from conftest import make
from groundtruth.analysis.detect import detect_converters
from groundtruth.analysis.highlight import issue_highlight
from groundtruth.core.issues import Issue
from groundtruth.rules.checks import run_checks


def issues_of(board, config, check):
    return [i for i in run_checks(board, config).issues if i.check == check]


def uuids(board, *, net=None, layer=None):
    return {t.uuid for t in board.tracks if (net is None or t.net == net) and (layer is None or t.layer == layer)}


@pytest.mark.parametrize("layers,plane", [(2, "B.Cu"), (4, "In1.Cu")])
def test_gap_crossing_shows_the_trace_over_the_gap_and_the_plane_beside_it(tmp_path, layers, plane):
    board, sc, _ = make(tmp_path, microstrip_board(slot=(19.5, 4, 20.5, 16), layers=layers))
    issue, = issues_of(board, sc.config, "gap_crossing")
    h = issue_highlight(issue, board, sc.config)
    assert h.focus[0] == issue.items[0] and set(h.focus) <= uuids(board, net="SIG")
    gnd = [z.uuid for z in board.zones if z.net == "GND" and plane in z.filled]
    assert set(h.context) == set(gnd) and h.layer == plane          # the slot's plane, drawn on top
    assert h.nets and h.lit == h.focus and "over the gap" in h.text and "GND plane" in h.text


def test_edge_proximity_shows_the_trace_and_the_plane_whose_edge_it_follows(tmp_path):
    g = microstrip_board()
    g.items = [i for i in g.items if "(zone" not in i]
    g.zone("GND", "B.Cu", [(0.3, 0.3), (39.7, 0.3), (39.7, 10.5), (0.3, 10.5)])
    board, sc, _ = make(tmp_path, g)
    issue = issues_of(board, sc.config, "edge_proximity")[0]
    h = issue_highlight(issue, board, sc.config)
    assert h.focus and set(h.focus) <= uuids(board, net="SIG")
    assert h.context == [z.uuid for z in board.zones if z.net == "GND"] and h.layer == "B.Cu"


def test_layer_change_shows_the_via_once_and_a_close_stitch_in_the_zoom(tmp_path):
    board, sc, _ = make(tmp_path, layer_change_board(None))
    issue, = issues_of(board, sc.config, "layer_change_stitch")
    assert len(issue.items) > 1 and len(set(issue.items)) == 1      # the via, once per layer pair it crosses
    h = issue_highlight(issue, board, sc.config)
    assert h.focus == [issue.items[0]] and h.zoom_out == 2            # a via alone: zoomed out for context
    close, _, _ = make(tmp_path, layer_change_board(3.0), "close")    # a stitching via 3 mm away (rule: 2 mm)
    cissue, = issues_of(close, sc.config, "layer_change_stitch")
    ch = issue_highlight(cissue, close, sc.config)
    stitch = [v.uuid for v in close.vias if v.net == "GND"]
    assert ch.focus[0] == cissue.items[0] and set(ch.focus[1:]) <= set(stitch) and len(ch.focus) == 2
    assert "stitching via (3.0 mm away)" in ch.text
    assert ch.lit == [cissue.items[0]]          # KiCad lights the selected nets: the GND via must not be among them


def test_missing_route_shows_both_pins(tmp_path):
    g = microstrip_board()
    g.items = [i for i in g.items if '(net "SIG")' not in i or "(footprint" in i]
    board, sc, _ = make(tmp_path, g)
    issue, = issues_of(board, sc.config, "route_missing")
    h = issue_highlight(issue, board, sc.config)
    assert {board.find_pin("U1.1").uuid, board.find_pin("U2.1").uuid} <= set(h.focus)


def test_a_simulated_route_shows_its_copper_and_the_via_carrying_the_detour(tmp_path):
    board, sc, _ = make(tmp_path, layer_change_board(None))
    signal_via = next(v for v in board.vias if v.net == "SIG")
    x, y = signal_via.position
    rr = NS(kind="route", net="SIG", driver="U1.1", receiver="U2.1", return_pins=("", ""),
            transfers=[{"name": "plane capacitance In1.Cu–In2.Cu", "kind": "capacitance", "x": 1.0, "y": 1.0,
                        "fraction": 0.6},                       # larger, but no board item: skipped
                       {"name": f"via SIG @({x:.2f}, {y:.2f})", "kind": "via", "x": x + 0.3, "y": y,
                        "fraction": 0.4}])
    issue = Issue("return_detour", "warning", "SIG", "detour", (30.0, 10.0), layer="In1.Cu",
                  key="detour:U1.1->U2.1", details={"l_ratio": 2.0})
    h = issue_highlight(issue, board, sc.config, [rr])
    assert uuids(board, net="SIG") <= set(h.focus) and signal_via.uuid in h.focus
    assert board.find_pin("U1.1").uuid in h.focus and h.layer == "In1.Cu" and "40 % of the return" in h.text
    sig_pins = {board.find_pin(p).uuid for p in ("U1.1", "U2.1")}
    assert set(h.lit) == uuids(board, net="SIG") | {signal_via.uuid} | sig_pins   # the signal net only
    split = Issue("return_path_broken", "error", "SIG", "split", (5.0, 10.0), key="split:U1.1:U2.1")
    returns = NS(kind="route", net="SIG", driver="U1.1", receiver="U2.1", return_pins=("U1.2", "U2.2"), transfers=[])
    sh = issue_highlight(split, board, sc.config, [returns])
    gnd_pins = {board.find_pin(p).uuid for p in ("U1.2", "U2.2") if board.find_pin(p)}
    assert gnd_pins and gnd_pins <= set(sh.focus) and not gnd_pins & set(sh.lit)


@pytest.fixture
def buck(tmp_path):
    board, sc, _ = make(tmp_path, discrete_buck_board(), "buck", nets=())
    sc.add_detected_converters([d.spec for d in detect_converters(board, sc.config)])
    spec = sc.config.converters[0]
    res = NS(kind="converter", net=spec.name, caps=[{"name": "LISN", "lisn": True, "share_hf": 0.9},
                                                    {"name": "C1", "lisn": False, "share_hf": 0.6}])
    return board, sc.config, spec, res


def test_a_power_stage_shows_its_switches_capacitor_and_switch_node(buck):
    board, config, spec, res = buck
    issue = Issue("hot_loop", "warning", spec.name, "overshoot", (18.0, 15.0), key=f"power:{spec.name}:loop")
    h = issue_highlight(issue, board, config, [res])
    fps = {board.footprint(r).uuid for r in (*spec.parts, "C1")}
    assert fps <= set(h.focus)
    sw = {t.uuid for t in board.tracks if t.net == spec.switch_node} | \
        {z.uuid for z in board.zones if z.net == spec.switch_node}
    assert sw and sw <= set(h.focus) | set(h.context)
    assert h.nets and set(h.lit) <= sw                   # only the switch node lights up
    rails = {t.uuid for t in board.tracks if t.net in (spec.rail, spec.ground)}
    assert not rails & set(h.focus)                      # never the rail or ground


def test_conducted_emission_shows_the_input_connector_without_a_net_highlight(buck):
    board, config, spec, res = buck
    issue = Issue("conducted_emission", "warning", spec.name, "conducted", (18.0, 15.0),
                  key=f"power:{spec.name}:conducted")
    h = issue_highlight(issue, board, config, [res])
    assert board.footprint("J1").uuid in h.focus and not h.nets   # footprints only: KiCad would take the mouse's net


def test_ids_that_are_not_on_the_board_are_dropped_and_planes_leave_the_zoom(tmp_path):
    board, sc, _ = make(tmp_path, microstrip_board())
    gnd = next(z for z in board.zones if z.net == "GND")
    track = board.tracks[0].uuid
    issue = Issue("custom", "info", "SIG", "?", (20.0, 10.0), items=["", "whatif-3", gnd.uuid, track])
    h = issue_highlight(issue, board, sc.config)
    assert h.focus == [track] and h.context == [gnd.uuid]       # the 40 mm plane is selected, not zoomed to
    nothing = issue_highlight(Issue("custom", "info", "SIG", "?", (0.0, 0.0)), board, sc.config)
    assert nothing.empty and not nothing.nets


def test_a_pull_up_rail_is_neither_zoomed_to_nor_lit(tmp_path):
    """A 100 R pull-up ties SIG to +3V3: signal_nets reaches the rail, but the route does not pass through it, so the
    rail's copper (here a track across the board) stays out of the zoom and the highlight."""
    from groundtruth.analysis.route import signal_nets
    g = microstrip_board()
    g.two_pin("R1", "100", (20.0, 13.0), "SIG", "+3V3")
    g.track((2.0, 18.0), (38.0, 18.0), "+3V3")
    board, sc, _ = make(tmp_path, g)
    assert "+3V3" in signal_nets(board, sc.config, "SIG")          # the trap this guards against
    issue = Issue("emi_margin", "warning", "SIG", "margin", (20.0, 10.0), key="emi:U1.1->U2.1")
    h = issue_highlight(issue, board, sc.config)
    rail = uuids(board, net="+3V3")
    assert uuids(board, net="SIG") <= set(h.focus) and not rail & set(h.focus) and not rail & set(h.lit)


def test_a_hotspot_in_a_pour_anchors_on_the_pours_own_copper(tmp_path):
    """Between another net's vias the hotspot is about the pour: those vias are not picked; with none of the pour's
    small copper close, the nearest one anchors the zoom (never the plane itself)."""
    from groundtruth.analysis.highlight import _copper_at
    g = microstrip_board()
    other = g.via((20.0, 5.0), "GATE")
    board, _, _ = make(tmp_path, g)
    hot = _copper_at(board, "B.Cu", (20.0, 5.0))
    gnd_vias = sorted((abs(v.position[0] - 20.0) + abs(v.position[1] - 5.0), v.uuid) for v in board.vias
                      if v.net == "GND")
    assert other not in hot["small"] and hot["small"] == [gnd_vias[0][1]]
    assert hot["zones"] == [z.uuid for z in board.zones if z.net == "GND"]


def test_reference_net_shows_every_piece_over_the_other_nets_plane(tmp_path):
    """Two +3V3 islands under the trace: the issue's length is the sum of both pieces; every segment over either
    island is shown, none over the GND between them."""
    g = microstrip_board()
    g.items = [i for i in g.items if "(zone" not in i and '(net "SIG")' not in i or "(footprint" in i]
    xs = [5, 7, 9, 15, 17, 22, 25, 27, 30, 35]
    segs = g.polyline([(x, 10.0) for x in xs], "SIG")
    islands = [(8, 16), (24, 28)]
    g.zone("GND", "B.Cu", [(0.3, 0.3), (39.7, 0.3), (39.7, 19.7), (0.3, 19.7)],
           holes=[[(a, 4), (b, 4), (b, 16), (a, 16)] for a, b in islands])
    for a, b in islands:
        g.zone("+3V3", "B.Cu", [(a + 0.3, 4.3), (b - 0.3, 4.3), (b - 0.3, 15.7), (a + 0.3, 15.7)])
    board, sc, _ = make(tmp_path, g)
    issue = Issue("reference_net", "warning", "SIG", "over +3V3", (12.0, 10.0), layer="F.Cu", length_mm=12.0,
                  key="refnet:+3V3", details={"plane_net": "+3V3", "plane_layer": "B.Cu"})
    h = issue_highlight(issue, board, sc.config)
    over = {segs[i] for i, (a, b) in enumerate(zip(xs, xs[1:])) if any(a < hi and b > lo for lo, hi in islands)}
    assert set(h.focus) == over and len(over) == 6
    assert set(h.context) == {z.uuid for z in board.zones if z.net == "+3V3"} and h.layer == "B.Cu"


def test_nets_picked_in_the_nets_tab_show_all_their_copper(tmp_path):
    """Clicking rows of the Nets tab: every pad, track and via of those nets is selected and lit; a net's plane is
    selected after the zoom; no rows (or unknown nets) give an empty Highlight, which clears the last one."""
    from groundtruth.analysis.highlight import nets_highlight
    board, sc, _ = make(tmp_path, microstrip_board())
    h = nets_highlight(["SIG"], board, sc.config)
    sig = uuids(board, net="SIG") | {p.uuid for p in board.pads() if p.net == "SIG"}
    assert set(h.focus) == sig and set(h.lit) == sig and h.nets and not h.context
    assert "SIG: 2 pads, 1 track segment, 0 vias" == h.text
    from dataclasses import replace
    gnd = nets_highlight(["GND", "SIG"], board, replace(sc.config, reference_nets=[]))   # GND as a net of its own
    plane = {z.uuid for z in board.zones if z.net == "GND"}
    assert plane == set(gnd.context) and not plane & set(gnd.focus) and sig <= set(gnd.lit)
    assert "2 nets (GND, SIG)" in gnd.text
    for names in ([], ["/NOPE"]):
        empty = nets_highlight(names, board, sc.config)
        assert empty.empty and not empty.lit and not empty.nets


def test_power_stages_picked_in_the_converters_tab_light_their_whole_power_flow(buck):
    """Clicking a row of the Converters tab: the stage's switches and capacitors are selected, and the copper of its
    whole power flow (input, rail, switch node, inductor output) is selected and lit, but never the ground: that
    would light up the whole board. Unknown names give an empty Highlight."""
    from groundtruth.analysis.highlight import stages_highlight
    board, config, spec, res = buck
    h = stages_highlight([spec.name], board, config, [res])
    fps = {board.footprint(r).uuid for r in (*spec.parts, "C1")}
    copper = {t.uuid: t.net for t in board.tracks} | {v.uuid: v.net for v in board.vias} |         {z.uuid: z.net for z in board.zones}
    flow = {spec.rail, spec.switch_node, "VOUT"}
    assert fps <= set(h.focus) and h.nets and f"power stage {spec.name}" in h.text and "power flow" in h.text
    assert {u for u, n in copper.items() if n in flow} <= set(h.focus) | set(h.context)
    assert {copper[u] for u in h.lit if u in copper} == flow
    assert not {u for u, n in copper.items() if n == spec.ground} & (set(h.focus) | set(h.context) | set(h.lit))
    assert stages_highlight(["nope"], board, config).empty


def test_a_clicked_heatmap_marker_shows_the_copper_right_there(tmp_path):
    """Heatmap tab, a marker clicked (point_highlight): the via at the point first (a via passes every layer), else a
    pad, else a track on the map's layer; a signal's copper is lit, a ground via is not (lighting GND lights the
    whole board); nothing there: empty, so the caller falls back to the issue's own highlight."""
    from groundtruth.analysis.highlight import point_highlight
    board, sc, _ = make(tmp_path, layer_change_board(3.0), "click")
    cfg = sc.config
    sig = next(v for v in board.vias if v.net == "SIG")
    gnd = next(v for v in board.vias if v.net == "GND")
    h = point_highlight(board, cfg, sig.position[0] + 0.1, sig.position[1], "B.Cu")
    assert h.focus == [sig.uuid] and h.lit == [sig.uuid] and h.text.startswith("via SIG at") and h.layer == "B.Cu"
    h = point_highlight(board, cfg, *gnd.position)
    assert h.focus == [gnd.uuid] and h.lit == [] and not h.nets
    track = next(t for t in board.tracks if t.net == "SIG" and t.layer == "F.Cu")
    h = point_highlight(board, cfg, 12.0, 10.0, "F.Cu")
    assert h.focus == [track.uuid] and h.text == "track SIG on F.Cu"
    assert point_highlight(board, cfg, 12.0, 10.0, "B.Cu").empty          # that track is on F.Cu only
    pad = board.find_pin("U1.1")
    h = point_highlight(board, cfg, *pad.position, "F.Cu", text="error · something")
    assert h.focus == [pad.uuid] and h.text == "error · something"
    assert point_highlight(board, cfg, 38.0, 2.0).empty
