"""Validation against analytic solutions.

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
from groundtruth.thermal.materials import COPPER  # noqa: E402
from groundtruth.thermal.project import BoardSetup, ComponentPower, Link, Project, Terminal  # noqa: E402
from groundtruth.thermal.solver.common import Progress  # noqa: E402
from groundtruth.thermal.solver.coupled import run  # noqa: E402

RHO = COPPER.resistivity_20c
T_CU = 35e-6


def _setup(path, **kw):
    bs = BoardSetup(str(path), **kw)
    bs.materials.temperature_dependent_resistance = False
    return bs


def _run(path, bs, env=None):
    p = Project(boards=[bs])
    if env:
        for k, v in env.items():
            setattr(p.environment, k, v)
    return run(load_board(path), p, 0, Progress())


def test_straight_trace_resistance(tmp_path):
    f = make_board(tmp_path / "t.kicad_pcb", outline=(0, 0, 60, 20),
                   pads=[dict(ref="A", x=5, y=10, w=1, h=1), dict(ref="B", x=55, y=10, w=1, h=1)],
                   tracks=[((5, 10), (55, 10), 1.0, "F.Cu")])
    bs = _setup(f, terminals=[Terminal("in", ["A.1"], "source", 0.01), Terminal("out", ["B.1"], "sink")])
    r = _run(f, bs)
    expected = RHO * 49e-3 / (1e-3 * T_CU)
    got = r.summary["electrical"]["resistance"]
    assert got == pytest.approx(expected, rel=0.02)


def test_diagonal_trace_resistance(tmp_path):
    L = 50.0
    d = L / math.sqrt(2)
    f = make_board(tmp_path / "d.kicad_pcb", outline=(0, 0, d + 10, d + 10),
                   pads=[dict(ref="A", x=5, y=5, w=1, h=1, angle=45),
                         dict(ref="B", x=5 + d, y=5 + d, w=1, h=1, angle=45)],
                   tracks=[((5, 5), (5 + d, 5 + d), 1.0, "F.Cu")])
    bs = _setup(f, terminals=[Terminal("in", ["A.1"], "source", 0.01), Terminal("out", ["B.1"], "sink")])
    r = _run(f, bs)
    expected = RHO * (L - 1) * 1e-3 / (1e-3 * T_CU)
    assert r.summary["electrical"]["resistance"] == pytest.approx(expected, rel=0.025)


@pytest.mark.parametrize("angle", [15, 30, 45])
def test_angled_trace_four_cells_wide(tmp_path, angle):
    """Cut-cell conductances keep angled traces accurate at Normal quality (4 cells across)."""
    L, W = 40.0, 1.0
    a = math.radians(angle)
    dx_, dy_ = L * math.cos(a), L * math.sin(a)
    f = make_board(tmp_path / "a.kicad_pcb", outline=(0, 0, dx_ + 10, dy_ + 10.3),
                   pads=[dict(ref="A", x=5, y=5.137, w=1, h=1, angle=-angle),
                         dict(ref="B", x=5 + dx_, y=5.137 + dy_, w=1, h=1, angle=-angle)],
                   tracks=[((5, 5.137), (5 + dx_, 5.137 + dy_), W, "F.Cu")])
    expected = RHO * (L - 1) * 1e-3 / (W * 1e-3 * T_CU)
    assert _resistance(f, W / 4) == pytest.approx(expected, rel=0.03)


def test_energy_conservation_and_via(tmp_path):
    f = make_board(tmp_path / "v.kicad_pcb", outline=(0, 0, 40, 20),
                   pads=[dict(ref="A", x=5, y=10, w=2, h=2), dict(ref="B", x=35, y=10, w=2, h=2, layer="B.Cu")],
                   tracks=[((5, 10), (20, 10), 0.5, "F.Cu"), ((20, 10), (35, 10), 0.5, "B.Cu")],
                   vias=[(20, 10, 0.6, 0.3)])
    bs = _setup(f, terminals=[Terminal("in", ["A.1"], "source", 2.0), Terminal("out", ["B.1"], "sink")])
    bs.materials.temperature_dependent_resistance = True
    r = _run(f, bs)
    e = r.summary["electrical"]
    assert e["power_input"] == pytest.approx(e["power_copper"], rel=1e-6)
    assert r.summary["heat_out"] == pytest.approx(r.summary["power_total"], rel=1e-4)
    assert len(e["vias"]) == 1 and e["vias"][0]["current"] == pytest.approx(2.0, rel=1e-6)
    # Via barrel alone: rho * L / (pi * t * (d + t))
    t = 25e-6
    r_via = RHO * 1.565e-3 / (math.pi * t * (0.3e-3 + t))
    r_tracks = RHO * 28e-3 / (0.5e-3 * T_CU)  # 2 x (15 mm - 1 mm pad half)
    assert e["resistance"] > r_via + 0.95 * r_tracks



def test_plated_hole_is_cut_out(tmp_path):
    """A plated mounting hole in solid pours: no board or copper in the drill, barrel still conducts."""
    f = make_board(tmp_path / "h.kicad_pcb", outline=(0, 0, 40, 20),
                   pads=[dict(ref="A", x=5, y=10, w=2, h=2), dict(ref="B", x=35, y=10, w=2, h=2, layer="B.Cu"),
                         dict(ref="H1", x=20, y=10, w=6, h=6, kind="thru_hole", drill=3.2)],
                   zones=[("F.Cu", [(1, 1), (20, 1), (20, 19), (1, 19)]),
                          ("B.Cu", [(20, 1), (39, 1), (39, 19), (20, 19)])])
    bs = _setup(f, terminals=[Terminal("in", ["A.1"], "source", 2.0), Terminal("out", ["B.1"], "sink")])
    r = _run(f, bs)
    e = r.summary["electrical"]
    assert r.summary["heat_out"] == pytest.approx(r.summary["power_total"], rel=1e-4)
    assert [v["current"] for v in e["vias"]] == [pytest.approx(2.0, rel=1e-6)]  # only path is the barrel
    i, j = r.grid.cell_of(20, 10)
    assert np.isnan(r.T[:, i, j]).all()  # drill is not part of the board
    i2, j2 = r.grid.cell_of(20, 12.1)  # just outside the 1.6 mm hole radius, on the annulus
    assert np.isfinite(r.T[:, i2, j2]).all()

def test_link_resistor(tmp_path):
    f = make_board(tmp_path / "l.kicad_pcb", outline=(0, 0, 40, 20),
                   pads=[dict(ref="A", x=5, y=10, w=2, h=2), dict(ref="B", x=35, y=10, w=2, h=2)],
                   tracks=[])
    bs = _setup(f, terminals=[Terminal("in", ["A.1"], "source", 1.0), Terminal("out", ["B.1"], "sink")],
                links=[Link("R1", "A.1", "B.1", 0.1)])
    r = _run(f, bs)
    e = r.summary["electrical"]
    assert e["resistance"] == pytest.approx(0.1, rel=1e-6)
    assert e["links"][0]["power"] == pytest.approx(0.1, rel=1e-6)
    assert r.summary["power_total"] == pytest.approx(0.1, rel=1e-3)


def test_fin_temperature_profile(tmp_path):
    """Long copper-clad strip heated at one end: classic fin with convective sides."""
    Lmm, Wmm = 100.0, 10.0
    zones = [(layer, [(0, 0), (Lmm, 0), (Lmm, Wmm), (0, Wmm)]) for layer in ("F.Cu", "B.Cu")]
    f = make_board(tmp_path / "fin.kicad_pcb", outline=(0, 0, Lmm, Wmm), zones=zones,
                   pads=[dict(ref="H", x=1, y=Wmm / 2, w=2, h=Wmm)])
    bs = _setup(f, components=[ComponentPower("H", 0.1)])
    bs.mesh.thermal_cell_mm = 0.5
    h = 10.0
    r = _run(f, bs, env={"convection": "fixed", "h_top": h, "h_bottom": h, "radiation": False})
    # Composite fin
    t_fr4 = 1.6e-3 - 2 * T_CU
    kA = (2 * COPPER.k * T_CU + 0.81 * t_fr4) * Wmm * 1e-3
    P = 2 * Wmm * 1e-3 + 2 * 1.6e-3
    m = math.sqrt(h * P / kA)
    Lm = Lmm * 1e-3
    xs, _ = r.grid.centers()
    T = np.nanmean(r.T[0], axis=0)  # mean across width on the top copper

    def theta(xmm):
        return T[np.argmin(abs(xs - xmm))] - 25.0

    # Compare away from the heater, where the field is one-dimensional.
    for x in (50.0, 75.0):
        expected = math.cosh(m * (Lm - x * 1e-3)) / math.cosh(m * (Lm - 25e-3))
        assert theta(x) / theta(25.0) == pytest.approx(expected, rel=0.03)
    assert r.summary["heat_out"] == pytest.approx(0.1, rel=1e-6)


def test_convection_correlations():
    from groundtruth.thermal.solver import convection as C
    h_up = C.h_horizontal(20, 0.025, 35, upper=True)
    h_dn = C.h_horizontal(20, 0.025, 35, upper=False)
    h_v = C.h_vertical(20, 0.1, 35)
    assert 5 < h_up < 12 and 2 < h_dn < h_up and 3 < h_v < 8
    assert 8 < C.h_forced(2.0, 0.1, 35) < 20


def _resistance(path, dx):
    from groundtruth.thermal.solver.coupled import plan_mesh
    from groundtruth.thermal.solver.electrical import ElectricalModel
    from groundtruth.thermal.solver.stack import build_stack

    b = load_board(path)
    bs = _setup(path, terminals=[Terminal("in", ["A.1"], "source", 1.0), Terminal("out", ["B.1"], "sink")])
    bs.mesh.thermal_cell_mm = 0.8
    bs.mesh.electrical_cell_mm = dx
    pl = plan_mesh(b, bs)
    el = ElectricalModel(b, bs, build_stack(b, bs.materials), pl["grid"], pl["ratio"], Progress())
    el.solve(None)
    return el.summary()["resistance"]


@pytest.mark.parametrize("case", ["plane", "trace"])
def test_via_spreading_mesh_independence(tmp_path, case):
    """Via-to-plane and via-on-trace resistance must hold on meshes coarser than the via."""
    if case == "plane":
        zones = [(l, [(0, 0), (40, 0), (40, 40), (0, 40)]) for l in ("F.Cu", "B.Cu")]
        f = make_board(tmp_path / "p.kicad_pcb", outline=(0, 0, 40, 40), zones=zones, vias=[(20.3, 20.1, 0.6, 0.3)],
                       pads=[dict(ref="A", x=2, y=20, w=2, h=36), dict(ref="B", x=38, y=20, w=2, h=36, layer="B.Cu")])
    else:
        f = make_board(tmp_path / "t.kicad_pcb", outline=(0, 0, 40, 20), vias=[(20.3, 10.1, 0.6, 0.3)],
                       tracks=[((2, 10.1), (20.3, 10.1), 1.0, "F.Cu"), ((20.3, 10.1), (38, 10.1), 1.0, "B.Cu")],
                       pads=[dict(ref="A", x=2, y=10.1, w=2, h=2), dict(ref="B", x=38, y=10.1, w=2, h=2, layer="B.Cu")])
    ref = _resistance(f, 0.05)
    for dx in (0.8, 0.4, 0.2):
        assert _resistance(f, dx) == pytest.approx(ref, rel=0.03), dx


def test_coverage_is_unbiased():
    from shapely.geometry import LineString, Point
    from groundtruth.thermal.solver.raster import Grid, coverage

    for dx in (0.8, 0.3, 0.1):
        g = Grid(0, 0, dx, int(40 / dx), int(20 / dx))
        tr = LineString([(2, 10.137), (38, 10.137)]).buffer(0.5, cap_style=2)
        ring = Point(20, 10).buffer(3).difference(Point(20, 10).buffer(1))
        assert coverage([tr], g).sum() * dx * dx == pytest.approx(tr.area, rel=0.005)
        assert coverage([ring], g).sum() * dx * dx == pytest.approx(ring.area, rel=0.01)
