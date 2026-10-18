"""Plane-pair capacitance: parallel-plate value, cavity resonance, and the board-wide model in a simulation."""
import math
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools" / "dev"))
from cavity_check import ER, T, plate_pair  # noqa: E402

from boardgen import layer_change_board  # noqa: E402
from conftest import make  # noqa: E402
from groundtruth.analysis.simulate import simulate  # noqa: E402
from groundtruth.config.settings import NetSpec  # noqa: E402
from groundtruth.solver.peec import EPS0  # noqa: E402


def test_parallel_plate_capacitance():
    p = plate_pair(20, 20, 0.5)
    s = p.solve(1e6)
    c = -1 / (2 * math.pi * 1e6 * s.Z_loop.imag)
    side = 39 * 0.5e-3                    # interior nodes of the rasterised 20 mm plate
    assert c == pytest.approx(EPS0 * ER * side ** 2 / T, rel=1e-3)


def test_cavity_resonance_frequency():
    """|Z| at a corner probe of a 40 x 20 mm plate pair peaks at f10 = c / (2·a·√εr)."""
    p = plate_pair(40, 20, 0.5, tan_delta=0.02)
    fs = np.linspace(1.70e9, 1.90e9, 11)
    z = [abs(p.solve(f).Z_loop) for f in fs]
    f_peak = fs[int(np.argmax(z))]
    f10 = 299_792_458 / (2 * 0.0392 * math.sqrt(ER))   # 39-40 mm effective length on the grid
    assert f_peak == pytest.approx(f10, rel=0.03)
    assert max(z) > 3 * min(z)


@pytest.fixture(scope="module")
def cavity_sim(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("cav")
    board, sc, _ = make(tmp, layer_change_board(None), "cav", nets=(NetSpec("SIG", "U1.1", ["U2.1"], 50e6, 1e-9),),
                        capacitance=True, solve_points=3, max_solves=6, cavity_max_nodes=12000)
    return simulate(board, sc.config)


@pytest.fixture(scope="module")
def cavity_run(cavity_sim):
    return cavity_sim.routes[0]


def test_board_wide_model_runs_with_the_route_model(cavity_run):
    rr = cavity_run
    assert not rr.error, rr.error
    assert rr.plane_capacitance and rr.cavity["nodes"] > 0
    assert rr.cavity["d_mm"] > rr.grid.d                   # coarser board-wide grid
    assert len(rr.cavity_solves) >= 3                       # f0 + log-spaced harmonics
    assert all(s["f"] in {h["f"] for h in rr.emissions} for s in rr.cavity_solves)   # solved at harmonics
    assert math.isfinite(rr.cavity["bias_db"])               # board-wide grid bias at f0 (cancels out)
    assert all("m_used_mm2" in s for s in rr.cavity_solves)
    assert rr.l_ratio is not None and rr.worst is not None


def test_displacement_current_grows_with_frequency(cavity_sim):
    """Without a stitching via near the layer change, the In1->In2 return crosses more and more through plane
    capacitance as frequency rises, and the 40 mm cavity (f10 ≈ 1.8 GHz) amplifies it."""
    from groundtruth.analysis.simulate import transfers
    setup = cavity_sim.setups[0]
    cav = setup.cavity
    pins = [setup.drv_ret.position, setup.rcv_ret.position]

    def capacitive(f):
        sol = cav.exc.solve(f)
        caps = [t for t in transfers(cav.build, cav.exc.stack, sol, setup.route, pins) if t["kind"] == "capacitance"]
        return (caps[0]["fraction"] if caps else 0.0), float(np.linalg.norm(sol.m))

    low, m_low = capacitive(50e6)
    high, m_high = capacitive(1.5e9)
    assert low < 0.05 < 0.5 < high
    assert m_high > 2 * m_low


def test_the_plane_crossing_obeys_kcl_and_the_tank_law(cavity_sim):
    """The return has to change from In1 to In2. Across that cut the displacement current and the vias' current add
    up to exactly the signal current at every frequency (KCL). The two GND vias at the ICs and the plane-pair
    capacitance form a parallel L-C tank, so below its resonance the capacitive share is the tank's current divider
    x/(1 - x), x = (f/f_r)^2: f_r from one frequency predicts the others. Above it the displacement current exceeds
    the signal current and the vias carry it back in antiphase: a circulating current (the demo board's "12.9×
    signal" at 1.45 GHz, tools/dev/transfer_check.py)."""
    from groundtruth.analysis.simulate import layer_crossing
    cav = cavity_sim.setups[0].cavity
    k = cav.build.names.index("In1.Cu")
    cross = {}
    for f in (200e6, 400e6, 600e6, 1.5e9):
        cap, via = layer_crossing(cav.exc.stack, cav.exc.solve(f), k)
        # the return comes from U2 on In2 and climbs to U1 on In1: -1 A per A crosses downwards (measured 1e-5)
        assert abs(cap + via + 1) < 1e-3, (f, cap, via)
        cross[f] = (cap, via)
    r0 = abs(cross[200e6][0])
    f_r = 200e6 / math.sqrt(r0 / (1 + r0))          # 1.29 GHz on this grid
    for f in (400e6, 600e6):                        # 2026-10-06: 0.8 % at 400 MHz, 4 % at 700 (distributed effects)
        x = (f / f_r) ** 2
        assert abs(cross[f][0]) == pytest.approx(x / (1 - x), rel=0.05), (f, f_r)
    cap, via = cross[1.5e9]
    assert f_r < 1.5e9 and abs(cap) > 1
    assert math.cos(np.angle(cap) - np.angle(via)) < -0.9          # antiphase: circulating


def test_a_worst_emission_from_a_plane_resonance_above_600_mhz_gets_a_grid_note():
    """Board-wide resonances above 600 MHz changed by several dB between grids on kraft_flux_mini_2 (they agreed
    within 0.5 dB at 540-580 MHz): a worst emission from one, near the warning level, says so and names the
    resolution where the grids agreed: 4x below 1 GHz (4x and 8x within 0.8 dB rms), 8x above (still ~3 dB apart,
    which the note says from 8x on)."""
    from groundtruth.analysis.simulate import grid_sensitive_note
    worst = {"f": 1.22e9, "margin_db": 4.1}
    note = grid_sensitive_note(worst, m_worst=183e-6, m_low=19.7e-6, warn_db=6.0)
    assert note and "1220 MHz" in note and "+19 dB" in note and "8x resolution" in note
    assert "8x resolution" in grid_sensitive_note(worst, 183e-6, 19.7e-6, 6.0, resolution=4)
    assert "about 3 dB" in grid_sensitive_note(worst, 183e-6, 19.7e-6, 6.0, resolution=8)
    below = {"f": 0.94e9, "margin_db": 4.1}
    assert "4x resolution" in grid_sensitive_note(below, 183e-6, 19.7e-6, 6.0, resolution=2)
    assert grid_sensitive_note(below, 183e-6, 19.7e-6, 6.0, resolution=4) is None
    assert grid_sensitive_note({"f": 0.55e9, "margin_db": 4.1}, 183e-6, 19.7e-6, 6.0) is None    # below 600 MHz
    assert grid_sensitive_note({"f": 0.94e9, "margin_db": 4.1}, 183e-6, 19.7e-6, 6.0)             # 1x's F.Cu/In1 mode
    assert grid_sensitive_note({"f": 1.22e9, "margin_db": 30.0}, 183e-6, 19.7e-6, 6.0) is None   # far below
    assert grid_sensitive_note(worst, 25e-6, 19.7e-6, 6.0) is None                               # no resonance
    assert grid_sensitive_note({"f": 1.22e9, "margin_db": None}, 183e-6, 19.7e-6, 6.0) is None
