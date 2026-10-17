"""Copper pours: a route through its net's own pour is solved as copper (the current spreads over the pour),
not as a line. Power stages carry their current this way."""
import math

import numpy as np
import pytest

from boardgen import BoardGen
from conftest import make
from groundtruth.analysis.route import find_route
from groundtruth.analysis.simulate import simulate
from groundtruth.config.settings import NetSpec

MU0 = 4e-7 * math.pi
WIDTH, Y, X0 = 4.0, 10.0, 4.0
SPEC = NetSpec("PWR", "U1.1", ["U2.1"], 10e6, 5e-9, current_a=1.0)


def pour_board(length: float) -> BoardGen:
    """A 4 mm wide PWR pour on F.Cu over a GND plane on In1 (0.235 mm), a pad at each end; the ground pads sit
    outside the pour with a via to the plane. No tracks: the only copper path is the pour."""
    g = BoardGen(layers=4, size=(X0 + length + 8, 20.0))
    for ref, x in (("U1", X0), ("U2", X0 + length)):
        g.footprint(ref, "IC", "Package_SO:Fake_IC", (x, Y), [
            {"number": 1, "at": (0, 0), "net": "PWR", "size": (1.0, WIDTH - 0.4)},
            {"number": 2, "at": (0, WIDTH / 2 + 1.5), "net": "GND", "size": (1.0, 1.0)},
        ])
        g.via((x, Y + WIDTH / 2 + 1.5), "GND", ("F.Cu", "B.Cu"))
    g.zone("PWR", "F.Cu", [(X0, Y - WIDTH / 2), (X0 + length, Y - WIDTH / 2), (X0 + length, Y + WIDTH / 2),
                           (X0, Y + WIDTH / 2)])
    g.plane("GND", "In1.Cu")
    return g


def test_route_runs_through_the_pour(tmp_path):
    board, sc, _ = make(tmp_path, pour_board(20.0), "p", nets=(SPEC,))
    r = find_route(board, sc.config, "PWR", "U1.1", "U2.1")
    assert [s.kind for s in r.segments] == ["zone"]
    assert r.length == pytest.approx(20.0, abs=0.05)


@pytest.fixture(scope="module")
def pour_results(tmp_path_factory):
    out = {}
    for length in (20.0, 40.0):
        tmp = tmp_path_factory.mktemp(f"pour{int(length)}")
        board, sc, _ = make(tmp, pour_board(length), "p", nets=(SPEC,), solve_points=1)
        rr = simulate(board, sc.config).routes[0]
        assert not rr.error, rr.error
        out[length] = rr
    return out


def test_pour_inductance_per_length_matches_a_wide_microstrip(pour_results):
    """The pour is solved copper: its loop inductance grows by Hammerstad's per-length value of a 4 mm wide
    microstrip 0.235 mm over its plane (end effects cancel in the difference of a 20 and a 40 mm pour)."""
    L20, L40 = (pour_results[k].solves[0]["L_nH"] * 1e-9 for k in (20.0, 40.0))
    per_m = (L40 - L20) / 0.020
    h, w = 0.235, WIDTH
    hammerstad = MU0 / (w / h + 1.393 + 0.667 * math.log(w / h + 1.444))
    assert per_m == pytest.approx(hammerstad, rel=0.10), (per_m * 1e9, hammerstad * 1e9)


def test_the_pour_carries_the_whole_current_crowded_at_its_edges(pour_results):
    """Across the middle of the pour the sheet current integrates to the full 1 A, and at 10 MHz it crowds
    into the outermost cells (the edge singularity of a strip over a plane, within ~h/pi of the edge):
    something a line model cannot show."""
    rr = pour_results[40.0]
    g = rr.layer_grids["F.Cu"]
    K = rr.sheet_current["F.Cu"]
    i = int(round((X0 + 20.0 - g.x0) / g.d))
    js = [j for j in range(g.ny) if abs(g.y0 + j * g.d - Y) <= WIDTH / 2 + g.d / 2 and g.node[i, j]]
    profile = np.array([K[i, j] for j in js])
    assert profile.sum() * g.d * 1e-3 == pytest.approx(1.0, rel=0.03)
    centre = profile[len(profile) // 2]
    assert profile[0] > 1.3 * centre and profile[-1] > 1.3 * centre


def test_return_statistics_exclude_the_pour_itself(pour_results):
    rr = pour_results[20.0]
    assert rr.reference_layer == "In1.Cu"                      # the pour's own F.Cu current is not "return"
    assert rr.layer_share.get("F.Cu", 0.0) < 0.05
    assert not any("PWR" in t["name"] for t in rr.transfers)
