"""Adaptive thermal mesh: small heated features against converged references.

The references were computed on the same boards with refinement down to 0.0125 mm
cells and a 0.01 K tolerance (two such runs agreed within about 2%, the midpoint is
used). The base cell sizes are those the planner picks for a 120 x 90 mm 4-layer
board, so these runs see the small features the way a real board would.

Run with:  python -m pytest tests -q
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from thermal_boardgen import make_board  # noqa: E402
from groundtruth.thermal.kicad.board import load_board  # noqa: E402
from groundtruth.thermal.project import BoardSetup, ComponentPower, Project, Terminal  # noqa: E402
from groundtruth.thermal.solver.common import Progress  # noqa: E402
from groundtruth.thermal.solver.coupled import run  # noqa: E402

BASE = {"draft": 0.75, "normal": 0.4, "fine": 0.2}
_ENDS = [dict(ref="A", x=2, y=5, w=0.6, h=0.6), dict(ref="B", x=18, y=5, w=0.6, h=0.6)]
_WIDE = [dict(ref="A", x=2.5, y=5, w=2, h=2), dict(ref="B", x=17.5, y=5, w=2, h=2)]
# name: (current A, board geometry, powered component, reference rise K)
CASES = {
    "trace": (1.0, dict(outline=(0, 0, 20, 10), pads=_ENDS, tracks=[((2, 5), (18, 5), 0.15, "F.Cu")]), None, 15.27),
    "trace_by_pour": (1.0, dict(outline=(0, 0, 20, 10), pads=_ENDS, tracks=[((2, 5), (18, 5), 0.15, "F.Cu")],
                                zones=[("F.Cu", [(1, 5.175), (19, 5.175), (19, 9.5), (1, 9.5)], "GND")]),
                      None, 12.78),
    "neck": (5.0, dict(outline=(0, 0, 20, 10), pads=_WIDE, tracks=[((9.5, 5), (10.5, 5), 0.25, "F.Cu")],
                       zones=[("F.Cu", [(1.5, 4), (9.5, 4), (9.5, 6), (1.5, 6)]),
                              ("F.Cu", [(10.5, 4), (18.5, 4), (18.5, 6), (10.5, 6)])]), None, 29.9),
    "small_pad": (0.0, dict(outline=(0, 0, 12, 12), pads=[dict(ref="U1", x=6, y=6, w=0.5, h=0.5)],
                            zones=[("B.Cu", [(0, 0), (12, 0), (12, 12), (0, 12)], "GND")]), "U1", 168.0),
}


def _solve(tmp_path, case, quality, shift=0.0, adaptive=True):
    amps, geo, comp, _ = CASES[case]
    dx = BASE[quality]
    x0, y0, x1, y1 = geo["outline"]
    f = make_board(tmp_path / f"{case}.kicad_pcb", **dict(geo, outline=(x0 - shift * dx, y0 - shift * dx, x1, y1)))
    bs = BoardSetup(str(f))
    if amps:
        bs.terminals = [Terminal("in", ["A.1"], "source", amps), Terminal("out", ["B.1"], "sink")]
    if comp:
        bs.components = [ComponentPower(comp, 0.1)]
    bs.materials.temperature_dependent_resistance = False
    bs.mesh.quality = quality
    bs.mesh.thermal_cell_mm = dx
    bs.mesh.adaptive = adaptive
    r = run(load_board(f), Project(boards=[bs]), 0, Progress())
    t = next(c["t_max"] for c in r.summary["components"] if c["ref"] == comp) if comp else r.summary["t_max"]
    return t - 25.0, r


@pytest.mark.parametrize("shift", [0.0, 0.5])
@pytest.mark.parametrize("case", list(CASES))
def test_small_features_normal(tmp_path, case, shift):
    """Normal quality resolves narrow traces, gaps, necks and small pads wherever they sit in a cell."""
    rise, r = _solve(tmp_path, case, "normal", shift)
    assert rise == pytest.approx(CASES[case][3], rel=0.04)
    assert r.summary["heat_out"] == pytest.approx(r.summary["power_total"], rel=1e-4)


@pytest.mark.parametrize("case", list(CASES))
def test_small_features_draft(tmp_path, case):
    rise, _ = _solve(tmp_path, case, "draft")
    assert rise == pytest.approx(CASES[case][3], rel=0.07)


def test_uniform_mesh_misses_gap(tmp_path):
    """Why refinement matters: a 0.1 mm gap inside 0.4 mm cells shorts the trace to the pour."""
    rise, _ = _solve(tmp_path, "trace_by_pour", "normal", adaptive=False)
    assert rise < 0.85 * CASES["trace_by_pour"][3]


def _planned(tmp_path):
    """Trace-by-pour board with electrical cells coarser than the finest thermal cells."""
    from groundtruth.thermal.solver.coupled import plan_mesh
    from groundtruth.thermal.solver.electrical import ElectricalModel
    from groundtruth.thermal.solver.mesh import HeatSamples, plan_refinement
    from groundtruth.thermal.solver.stack import build_stack

    f = make_board(tmp_path / "m.kicad_pcb", **CASES["trace_by_pour"][1])
    b = load_board(f)
    bs = BoardSetup(str(f), terminals=[Terminal("in", ["A.1"], "source", 1.0), Terminal("out", ["B.1"], "sink")])
    bs.mesh.thermal_cell_mm = 0.4
    bs.mesh.electrical_cell_mm = 0.06
    pl = plan_mesh(b, bs)
    stack = build_stack(b, bs.materials)
    el = ElectricalModel(b, bs, stack, pl["grid"], pl["ratio"], Progress())
    el.solve(None)
    P = el.cell_power()
    s = HeatSamples(pl["grid"].x0 + el.node_J * el.dx, pl["grid"].y0 + el.node_I * el.dx,
                    np.full(el.n_cells, el.dx), P)
    leaves, info = plan_refinement(pl["grid"], s, 0.3, 0.05, 6e5, stack)
    return b, bs, pl, stack, el, P, leaves, info


def test_refinement_is_balanced(tmp_path):
    from groundtruth.thermal.solver.mesh import DIRS, _covering_level

    *_, leaves, info = _planned(tmp_path)
    assert info["levels"] == 3  # 0.4 -> 0.2 -> 0.1 -> 0.05 mm
    covered = 0
    for l, blocks in leaves.items():
        for bi, bj in blocks:
            for di, dj in DIRS.values():
                m = _covering_level(leaves, l, bi + di, bj + dj)
                assert m is None or m >= l - 1, (l, bi, bj, m)
            covered += 4 ** -l
    n0 = len({(bi >> l, bj >> l) for l, blocks in leaves.items() for bi, bj in blocks})
    assert covered == pytest.approx(n0)  # leaves tile the level-0 blocks exactly once


def test_electrical_map_conserves_heat(tmp_path):
    from groundtruth.thermal.project import Environment
    from groundtruth.thermal.solver.mesh import Mesh
    from groundtruth.thermal.solver.thermal import ThermalModel

    b, bs, pl, stack, el, P, leaves, _ = _planned(tmp_path)
    th = ThermalModel(b, bs, Environment(), stack, Mesh(b, stack, pl["grid"], leaves, Progress()), Progress())
    M = th.electrical_map(el)
    assert np.asarray(M.sum(axis=0)).ravel() == pytest.approx(1.0, abs=1e-9)
    Q = M @ P
    assert Q.sum() == pytest.approx(P.sum(), rel=1e-12)
    # Heat lands on copper: the thermal cells that receive it are mostly covered.
    cols = np.arange(th.mesh.n_cols)
    q_cols = Q[th.cu_nodes(cols, 0)]
    assert (q_cols * th.mesh.col_cov[0]).sum() / q_cols.sum() > 0.8


def test_dielectric_split_is_graded():
    from groundtruth.thermal.solver.mesh import MAX_DIELECTRIC_SLAB_MM, Z_FIRST, dielectric_split

    for t in (0.1, 0.2, 0.5, 1.2, 1.53, 3.0):
        for dx in (0.025, 0.05, 0.2, 0.75):
            parts = dielectric_split(t, dx)
            assert sum(parts) == pytest.approx(t)
            assert parts == pytest.approx(parts[::-1])
            assert max(parts) <= MAX_DIELECTRIC_SLAB_MM + 1e-9
            if t > 4 * Z_FIRST * dx:
                assert parts[0] == pytest.approx(Z_FIRST * dx)
                assert all(b >= a - 1e-12 for a, b in zip(parts[:len(parts) // 2], parts[1:len(parts) // 2 + 1]))


def test_results_round_trip_with_patches(tmp_path):
    from groundtruth.thermal.solver.results import Results

    _, r = _solve(tmp_path, "small_pad", "normal")
    assert r.patches
    path = tmp_path / "r.npz"
    r.save(path)
    q = Results.load(path)
    assert len(q.patches) == len(r.patches)
    for a, b in zip(r.patches, q.patches):
        assert a.level == b.level and a.grid == b.grid
        np.testing.assert_array_equal(a.T, b.T)
    # The finest patch shows the pad hotter than the coarse map can.
    L = r.copper_layers[0]
    finest = max(np.nanmax(a) for a, _ in q.layer_fields("T", L))
    assert finest > np.nanmax(q.T[q.copper_slab(L)]) + 5.0
    assert math.isclose(finest, r.summary["layers"][0]["t_max"], rel_tol=1e-6)
