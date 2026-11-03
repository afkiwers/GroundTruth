"""Every route between two points and what hangs on it (analysis/path_map.py): the user's "define start and endpoint
and it maps every single path in my pcb, including the ones going to ground and power, there are also OPamps
connected to them for bias", and two pads picked in KiCad traced into a signal path."""
import pytest

from boardgen import BoardGen, opamp_path_board, quad_follower_board
from groundtruth.adapters.file_source import loads_board
from groundtruth.analysis.path_map import (format_dot, format_text, proposal_for_route, route_text, routes_between,
                                           trace)
from groundtruth.config.settings import Config


def biased_board():
    """U1.1 (SIG_A) → R1 → SIG_B → U5 (+IN 3, OUT 1, −IN 2 on SIG_C: a follower) → SIG_C → U2.1, and SIG_B biased: R2
    100k to BIAS, driven by the follower U6 (OUT 1 and −IN 2 on BIAS) whose +IN 3 sits on DIV, R3 to +5V and R4 to
    GND, C1 from DIV to GND. C2 from SIG_B to SIG_C is a second route around U5."""
    g = BoardGen(layers=2, size=(60.0, 30.0))
    y = 10.0
    g.ic("U1", (5.0, y), "SIG_A", gnd_offset=(0.0, 1.0), pintype="output")
    g.ic("U2", (55.0, y), "SIG_C", gnd_offset=(0.0, 1.0), pintype="input")
    g.two_pin("R1", "33", (12.0, y), "SIG_A", "SIG_B", kind="R")
    for ref, at, inp, out in (("U5", (25.0, y), "SIG_B", "SIG_C"), ("U6", (25.0, y + 12.0), "DIV", "BIAS")):
        x0, y0 = at
        g.footprint(ref, "OPA376", "Package_TO_SOT_SMD:SOT-23-5", at, [
            {"number": 3, "at": (-1.5, 0.0), "net": inp, "pintype": "input", "pinfunction": "+IN"},
            {"number": 2, "at": (-1.5, 1.0), "net": out, "pintype": "input", "pinfunction": "-IN"},
            {"number": 1, "at": (1.5, 0.0), "net": out, "pintype": "output", "pinfunction": "OUT"},
            {"number": 5, "at": (0.0, -1.5), "net": "+5V", "pintype": "power_in", "pinfunction": "V+"},
            {"number": 4, "at": (0.0, 1.5), "net": "GND", "pintype": "power_in", "pinfunction": "V-"}])
    g.two_pin("R2", "100k", (18.0, y + 5.0), "SIG_B", "BIAS", kind="R", angle=90.0)
    g.two_pin("R3", "10k", (18.0, y + 14.0), "+5V", "DIV", kind="R")
    g.two_pin("R4", "10k", (20.0, y + 16.0), "DIV", "GND", kind="R")
    g.two_pin("C1", "100n", (22.0, y + 16.0), "DIV", "GND", kind="C")
    g.two_pin("C2", "1n", (25.0, y - 4.0), "SIG_B", "SIG_C", kind="C")
    g.plane("GND", g.cu[-1])
    return loads_board(g.text(), "biased.kicad_pcb")


def test_every_route_and_the_bias_stage_on_it():
    """The routes from U1.1 to U2.1: through U5 (op-amp, + input to output) and around it through C2, never through
    the bias op-amp's supply; on SIG_B the bias: R2 to BIAS, driven by U6 whose input is the divider R3 to +5V / R4
    to GND with C1; the follower's own output is no route back to its input."""
    board = biased_board()
    pm = trace(board, Config(), "U1.1", ["U2.1"])
    assert [route_text(r) for r in pm.routes] == ["SIG_A → R1 33 → SIG_B → U5 OPA376 (op-amp) → SIG_C",
                                                  "SIG_A → R1 33 → SIG_B → C2 1n → SIG_C"]
    assert not pm.notes
    bias = next(b for b in pm.branches["SIG_B"] if b.part == "R2")
    assert (bias.kind, bias.to_net) == ("net", "BIAS")
    drive = next(b for b in bias.deeper if b.kind == "opamp_out")
    assert drive.part == "U6" and drive.pin == "U6.1"
    div = next(b for b in drive.deeper if b.to_net == "DIV")
    assert {(b.kind, b.part, b.to_net) for b in div.deeper} >= {("supply", "R3", "+5V"), ("ground", "R4", "GND"),
                                                                ("ground", "C1", "GND")}
    text = format_text(pm)
    assert "driven by op-amp U6 OPA376 (U6.1)" in text and "R3 10k to supply [+5V]" in text
    dot = format_dot(pm)
    assert dot.startswith("graph path_map {") and '"SIG_B" -- "SIG_C" [label="C2 1n"]' in dot


def test_two_pads_are_traced_in_the_direction_a_signal_takes():
    """Two pads picked in KiCad in either order: the routes run from the op-amp's input side, U1.1 → U2.1; the
    proposal drives from U1.1 into U2.1, named after a real net, C2 (no signal part by default) in Pass through,
    the sig_drv / sig_rx labels added at the ends."""
    board = biased_board()
    start, end, routes = routes_between(board, Config(), "U2.1", "U1.1")
    assert (start, end) == ("U1.1", "U2.1") and len(routes) == 2
    prop = proposal_for_route(board, Config(), routes[1], start, end)       # the route through C2
    assert not prop.error
    assert (prop.spec.name, prop.spec.driver, prop.spec.receivers) == ("SIG_C", "U1.1", ["U2.1"])
    assert prop.spec.through == ["C2"] and prop.spec.source == "auto" and not prop.spec.reviewed
    assert prop.labels == {"U1": "sig_drv:1", "U2": "sig_rx:1"}
    prop = proposal_for_route(board, Config(), routes[0], start, end)       # through U5: its model an amplifier
    assert prop.spec.through == [] and prop.models["U5"].kind == "amplifier" and not prop.models["U5"].pins


@pytest.mark.parametrize("labels", ["amp_in:2, amp_inv:1, amp_out:14, amp_in:4, amp_inv:5, amp_out:6, amp_in:9, "
                                    "amp_inv:8, amp_out:7", "amp_in:[2,4,9], amp_inv:[1,5,8], amp_out:[14,6,7]"])
def test_a_route_through_one_channel_of_a_quad(labels):
    """A quad op-amp's channels from its GT labels (groups or the compact lists): the route from U2 (IN_B) to U7
    (OUT_B) runs through U5B only; the others' nets are not on it."""
    board = loads_board(quad_follower_board(labels).text(), "quad.kicad_pcb")
    pm = trace(board, Config(), "U2.1", ["U7.1"])
    assert [route_text(r) for r in pm.routes] == ["IN_B → U5B QUAD_OPAMP (op-amp) → OUT_B"]
    assert pm.routes[0].hops[0].pin_in == "U5.4" and pm.routes[0].hops[0].pin_out == "U5.6"


def test_ends_by_net_name_and_the_searchs_limits():
    """Ends by net name (or its last part); an unknown end is said; the route cap stops the search with a note."""
    board = biased_board()
    pm = trace(board, Config(), "SIG_A", ["SIG_C", "NO_SUCH"])
    assert len(pm.routes) == 2 and "NO_SUCH: no such pin or net on the board" in pm.notes
    pm = trace(board, Config(), "SIG_A", ["SIG_C"], max_routes=1)
    assert len(pm.routes) == 1 and any("stopped after 1 routes" in n for n in pm.notes)


def test_a_plain_signal_path_is_one_route():
    """The op-amp test board (U1.1 → R1 → U5, a gain block by its Gain field → U2.1): one route, nothing else."""
    board = loads_board(opamp_path_board(Gain="1").text(), "amp.kicad_pcb")
    pm = trace(board, Config(), "U1.1", ["U2.1"])
    assert [route_text(r) for r in pm.routes] == ["SIG_A → R1 33 → SIG_B → U5 OPA376 (op-amp) → SIG_C"]
