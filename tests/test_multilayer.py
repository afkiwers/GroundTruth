"""Multi-layer return paths (M4): the return changes plane through stitching vias and decaps."""
import pytest

from boardgen import layer_change_board, microstrip_board
from conftest import make
from groundtruth.analysis.simulate import simulate
from groundtruth.config.settings import NetSpec

SIG = (NetSpec("SIG", "U1.1", ["U2.1"], 50e6, 1e-9),)


def route_result(tmp_path, gen, name):
    board, sc, _ = make(tmp_path, gen, name, nets=SIG)
    sc.doc["analysis"]["solve_points"] = 1     # one frequency keeps the tests quick
    sc.config.analysis.solve_points = 1
    rr = simulate(board, sc.config).routes[0]
    assert not rr.error, rr.error
    return rr


@pytest.fixture(scope="module")
def stitched(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("ml")
    return {dist: route_result(tmp, layer_change_board(dist), f"s{dist}") for dist in (0.8, 5.0)}


def test_all_plane_layers_take_part(stitched):
    rr = stitched[0.8]
    assert rr.plane_layers == ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
    # F.Cu trace returns on In1, B.Cu trace on In2
    assert rr.layer_share["In1.Cu"] > 0.3 and rr.layer_share["In2.Cu"] > 0.3


def test_nearest_stitching_via_carries_the_transfer(stitched):
    near, far = stitched[0.8], stitched[5.0]
    top = near.transfers[0]
    assert top["name"].startswith("via GND @(20.00, 10.80)") and top["fraction"] > 0.5
    assert set(top["layers"]) >= {"In1.Cu", "In2.Cu"}
    assert far.transfers[0]["distance_mm"] == pytest.approx(5.0, abs=0.3)
    assert near.solves[-1]["L_nH"] < far.solves[-1]["L_nH"]       # farther via -> bigger loop
    assert near.solves[-1]["m_mm2"] < far.solves[-1]["m_mm2"]


def test_power_plane_reference_needs_decaps(tmp_path):
    with_caps = route_result(tmp_path, layer_change_board(None, "+3V3", decap_distance=1.0), "caps")
    without = route_result(tmp_path, layer_change_board(None, "+3V3"), "nocaps")
    # Without decaps the +3V3 plane only carries induced (eddy) currents, never the net return:
    # nothing connects it to GND, so no part or via carries current between the nets.
    assert not any(t["kind"] == "part" for t in without.transfers)
    caps = [t for t in with_caps.transfers if t["kind"] == "part"]
    assert caps and max(t["fraction"] for t in caps) > 0.1
    assert without.l_ratio > with_caps.l_ratio


def test_nearest_mode_uses_one_plane(tmp_path):
    board, sc, _ = make(tmp_path, microstrip_board(layers=4), "n", nets=SIG)
    sc.config.analysis.planes = "nearest"
    sc.config.analysis.solve_points = 1
    rr = simulate(board, sc.config).routes[0]
    assert rr.plane_layers == ["In1.Cu"] and rr.layer_share["In1.Cu"] == pytest.approx(1.0)
