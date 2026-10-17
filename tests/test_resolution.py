"""Resolution (the plugin's per-machine choice beside Simulate, the CLI's --resolution): every model gets N times the
nodes and cells 1/sqrt(N) in size, also where the node budget does not bind."""
import math

import pytest

from boardgen import layer_change_board
from conftest import make
from groundtruth.analysis.simulate import RESOLUTION_MIN_MM, _choose_pitch, simulate
from groundtruth.config.settings import NetSpec


def test_the_pitch_shrinks_with_the_root_of_the_factor():
    """Where the budget does not bind, the dielectric height's cell / sqrt(N); where it binds, the cells grow until
    N x the budget holds; never under RESOLUTION_MIN_MM."""
    free = (0.0, 0.0, 10.0, 10.0)
    assert _choose_pitch(0.2, free, 2, 10**7) == pytest.approx(0.2)
    assert _choose_pitch(0.2, free, 2, 10**7, resolution=4) == pytest.approx(0.1)
    assert _choose_pitch(0.2, free, 2, 10**7, resolution=1000) == pytest.approx(RESOLUTION_MIN_MM)
    big = (0.0, 0.0, 100.0, 100.0)
    for r in (1, 2, 4, 8, 16, 20, 25):
        d = _choose_pitch(0.2, big, 4, 100_000, resolution=r)
        assert 4 * (100 / d + 1) ** 2 <= 100_000 * r
        assert 4 * (100 / (d / 1.1) + 1) ** 2 > 100_000 * r or d / 1.1 < 0.2 / math.sqrt(r)


def test_a_finer_resolution_converges_on_the_same_loop(tmp_path):
    """The layer-change route at 1x and 4x: the grid is about twice as fine and the loop inductance moves by a few
    percent at most (the default grid is converged; a finer one sharpens the maps)."""
    from dataclasses import replace
    board, sc, _ = make(tmp_path, layer_change_board(None), "res", nets=(NetSpec("SIG", "U1.1", ["U2.1"], 50e6, 1e-9),),
                        solve_points=2, max_solves=3, capacitance=False)
    one = simulate(board, sc.config).routes[0]
    four = simulate(board, replace(sc.config, analysis=replace(sc.config.analysis, resolution=4.0))).routes[0]
    assert four.grid.d == pytest.approx(one.grid.d / 2, rel=0.15)
    assert four.solves[0]["L_nH"] == pytest.approx(one.solves[0]["L_nH"], rel=0.05)
