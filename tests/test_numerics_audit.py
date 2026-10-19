"""Numerical audit, round 6 (docs/NUMERICS_AUDIT.md): solver failure reporting, convergence against independent
references, metamorphic properties of the DC and AC solves, and stale state. Each test names the issue (ISSUE-25
onwards) or the property (NA-n) it guards.

The independent references are tests/refsolvers.py (finite-difference sheet solves that share no code with
GroundTruth) and closed forms; the metamorphic tests compare GroundTruth with itself under a transformation whose
effect is known exactly (a rotation, a doubled copper thickness, a reversed feed, a reordered board file)."""
from __future__ import annotations

import math
import sys
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from boardgen import BoardGen, discrete_buck_board
from conftest import make
from groundtruth.analysis import power
from groundtruth.analysis.detect import detect_converters
from groundtruth.analysis.simulate import simulate
from groundtruth.config.settings import DcLoad, DcLoadSpec, SettingsFile
from groundtruth.solver.peec import SIGMA_CU

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools" / "dev"))

T_CU = 35e-6


# --------------------------------------------------------------------------- failures are reported, not hidden
def test_ISSUE25_a_failed_dc_path_is_named_in_the_loss_the_thermal_heat_and_the_issues(tmp_path, monkeypatch):
    """ISSUE-25: a power stage's DC path whose solve fails (GMRES unconverged, a factorisation too large) was left
    out of the stage's DC table with a note only, while the input current, the efficiency and Thermal's copper heat
    were presented as complete: on kraft_flux_mini_2 conv1's copper loss read 0.23 instead of 0.30 W when its input
    path did not converge. Now the path is in `dc_unsolved`, the input estimate lists it as missing, Thermal's inputs
    say its heat is not there, and a dc_unsolved warning names it. The solve is failed deterministically here (its
    solution removed, as solve_at records a SolverError)."""
    from groundtruth.analysis.thermal import thermal_inputs
    board, sc, _ = make(tmp_path, discrete_buck_board(out_connector=True), "dcfail", nets=())
    spec = detect_converters(board, sc.config)[0].spec
    spec.f_sw, spec.i_out, spec.v_in, spec.v_out = 300e3, 3.0, 12.0, 3.0
    spec.rds_on, spec.rds_on_low, spec.dcr = 10e-3, 5e-3, 8e-3
    sc.set_converters([spec])
    sc.save()
    cfg = SettingsFile.load(sc.path).config
    whole = simulate(board, cfg).converters[0]
    names = [p["name"] for p in whole.dc]
    lost = next(n for n in names if n.startswith("input "))
    assert not whole.dc_unsolved and not any(lost in m for m in whole.input["missing"])

    real = power.dc_analysis

    def failing(model, setup):
        """dc_analysis after the input path's DC solve failed."""
        for name, _, run in setup.dc_runs:
            if name == lost:
                run.sols[power.DC_F] = None
        return real(model, setup)

    monkeypatch.setattr(power, "dc_analysis", failing)
    res = simulate(board, cfg)
    r = res.converters[0]
    assert r.dc_unsolved == [lost] and lost not in [p["name"] for p in r.dc]
    assert any(lost in m for m in r.input["missing"])
    assert any(lost in n for n in r.notes if n.startswith("input current"))       # the input note says so
    assert any(i.check == "dc_unsolved" and lost in i.message for i in res.issues)
    _, _, notes = thermal_inputs(board, cfg, [r])
    assert any(lost in n and "heat" in n for n in notes)
    assert r.summary()["dc_unsolved"] == [lost]                                  # in the JSON report


class _Quiet:
    """A Progress that takes everything and logs into a list."""

    def __init__(self):
        """No lines yet."""
        self.lines = []

    def check(self):
        """Never cancelled."""

    def update(self, *a, **k):
        """Progress is not shown."""

    def log(self, text):
        """Keep the solver's log line."""
        self.lines.append(text)


def test_ISSUE26_a_reused_multigrid_that_used_up_its_budget_is_rebuilt_not_a_crash():
    """ISSUE-26 (1): the thermal CG reuses its AMG hierarchy between coupled iterations and rebuilds it when it
    converges too slowly. When the old hierarchy had used the whole iteration budget (a slow measured rate makes
    the cap = maxiter), the rebuilt one was given `maxiter - iterations` = 0 and pyamg raised "Number of iterations
    must be positive": the thermal solve ended with a ValueError. Now the rebuilt hierarchy gets a budget of its
    own, and the solve records whether it converged and its true residual."""
    import pyamg
    import scipy.sparse as sp
    from groundtruth.thermal.solver.common import SPDSolver
    A = pyamg.gallery.poisson((80, 80), format="csr")
    b = np.ones(A.shape[0])
    s = SPDSolver("t")
    x = s.solve(A, b, None, _Quiet(), tol=1e-10, maxiter=1000)
    assert s.last_converged and s.last_residual <= 1e-10
    A2 = ((A + sp.diags(np.linspace(0, 50, A.shape[0]))) * 1e3).tocsr()
    s.rate = 1e6                              # the old hierarchy may use the whole budget
    p = _Quiet()
    x = s.solve(A2, b, x, p, tol=1e-10, maxiter=5)
    assert any("rebuilding" in t for t in p.lines)
    assert s.last_residual == pytest.approx(np.linalg.norm(b - A2 @ x) / np.linalg.norm(b))
    assert s.last_converged == (s.last_residual <= 1e-10)


def test_ISSUE26_a_thermal_solve_whose_cg_stalls_says_so_instead_of_settling(tmp_path, monkeypatch):
    """ISSUE-26 (2): an unconverged CG returns its best x and only wrote a line to the solver log. A stalled CG (no
    progress: its x is its start) gives two answers in a row that differ by nothing, so the coupled iteration
    stopped at its second pass as "settled" with the board at ambient everywhere, and no word in the result. Now the
    iteration does not stop on an unconverged solve, the result is marked unsettled and its notes say the
    temperatures are not converged. The stall is forced by a CG that returns its start (pyamg's info > 0)."""
    import pyamg
    from groundtruth.analysis import thermal as T
    from groundtruth.thermal.solver import common
    g = BoardGen(layers=2, size=(56.0, 30.0))
    for ref, x in (("U1", 8.0), ("U2", 48.0)):
        g.footprint(ref, "IC", "Package_SO:Fake_IC", (x, 15.0),
                    [{"number": 1, "at": (0, 0), "net": "PWR", "size": (1.0, 1.0)}])
    g.track((8.0, 15.0), (48.0, 15.0), "PWR", "F.Cu", width=0.2)
    b, sc, _ = make(tmp_path, g, "cgstall", nets=())
    sc.set_dc_loads([DcLoadSpec("rail", "U1.1", [DcLoad("U2.1", 2.0, "")], "")])
    sc.doc["thermal"] = {"convection": "fixed", "h_top": 10.0, "h_bottom": 10.0, "radiation": False,
                         "ambient_c": 25.0, "quality": "draft"}
    sc.save()
    cfg = SettingsFile.load(sc.path).config
    res = simulate(b, cfg).dc_loads[0]
    good, _ = T.run_thermal(b, cfg, [res])
    assert good.t_max > 60.0 and not any("stopped short" in n for n in good.notes)

    def stalled(A, rhs, x0=None, tol=1e-5, maxiter=None, M=None, callback=None, residuals=None, **kw):
        """A CG that makes no progress: its start back, unconverged."""
        x = np.array(x0, float) if x0 is not None else np.zeros(len(rhs))
        r = float(np.linalg.norm(rhs - A @ x))
        if residuals is not None:
            residuals[:] = [r, r]
        return x, 1

    monkeypatch.setattr(common.SPDSolver, "DIRECT_LIMIT", 0)
    monkeypatch.setattr(pyamg.krylov, "cg", stalled)
    short, _ = T.run_thermal(b, cfg, [res])
    assert any("stopped short" in n and "not converged" in n for n in short.notes), short.notes
    assert any("did not settle" in n for n in short.notes)


# --------------------------------------------------------------------------- the peak density converges
def test_ISSUE27_the_peak_current_density_converges_with_the_grid(tmp_path):
    """ISSUE-27: the reported peak current density was one node's value. At a re-entrant corner the 2D density is
    singular, J ∝ r^(-1/3) [Jackson 1999, §2.11]: a 2 mm wide L at 10 A read 290 / 348 / 366 A/mm² at 1x / 2x / 4x
    (as d^(-1/3)), the independent sheet solve's own cell maximum 591 / 745 A/mm² at h = 0.025 / 0.0125 mm. The
    peak is now the RMS over a 0.25 mm window of the net's copper (peak_density [A45]), which converges: the sheet
    solve's windowed value is 319 / 320.5 A/mm² at those h; GroundTruth reads it from below, 290 / 297 A/mm² at 1x /
    4x (-10 / -7 %). The sustained value the checks use is unchanged."""
    import adversarial_dc as adv
    import refsolvers
    rows = {r["res"]: r for r in adv.case_corner(tmp_path, [1.0, 4.0])}
    a, c, b = (6.0, 6.0), (26.0, 6.0), (26.0, 26.0)
    cu = adv._copper([((a, c), 2.0, "PWR"), ((c, b), 2.0, "PWR")], [("U1", a), ("U2", b)])
    feeds = [(adv._pad(a), 10.0), (adv._pad(b), -10.0)]
    cell1, win1 = refsolvers.sheet_density_peak_fed(cu, feeds, 0.05, SIGMA_CU, T_CU, 0.25)
    cell2, win2 = refsolvers.sheet_density_peak_fed(cu, feeds, 0.025, SIGMA_CU, T_CU, 0.25)
    assert cell2 / cell1 > 1.15                                   # the singularity: no converged cell value
    assert win2 == pytest.approx(win1, rel=0.03)                  # the windowed value converges
    for res, low in ((1.0, 0.86), (4.0, 0.89)):
        assert low * win2 <= rows[res]["peak"] <= 1.02 * win2, (res, rows[res]["peak"], win2)
    assert rows[4.0]["peak"] / rows[1.0]["peak"] < 1.05          # was 1.26
    assert rows[4.0]["sustained"] == pytest.approx(rows[1.0]["sustained"], rel=0.03)


# --------------------------------------------------------------------------- metamorphic properties of the DC solve
@pytest.fixture(scope="module")
def mm_base(tmp_path_factory):
    """The metamorphic rail (tools/dev/metamorphic_dc.rail: a bent 0.3 mm track, a via, a GND plane) at 1 A, 1x:
    ((drop mV, loss W, fingerprint), its folder)."""
    import metamorphic_dc as mm
    tmp = tmp_path_factory.mktemp("mm")
    return mm.drop(tmp, mm.rail(), "base", 1.0), tmp


@pytest.mark.parametrize("kw", [dict(reverse=True), dict(split=True), dict(double=True), dict(shuffle=True)],
                         ids=["reversed", "split", "drawn_twice", "file_shuffled"])
def test_NA1_the_same_copper_described_differently_gives_the_same_drop(mm_base, kw):
    """NA-1: a track drawn end to start, split into collinear pieces at odd points, drawn twice on top of itself, or
    the board file's items in another order (as a save can reorder them) is the same copper: the same drop to 1e-9
    and, for the reordered file, the same fingerprint (the plugin's check for a changed board)."""
    import metamorphic_dc as mm
    (v0, p0, fp0), tmp = mm_base
    v, p, fp = mm.drop(tmp, mm.rail(**kw), "_".join(kw), 1.0)
    assert v == pytest.approx(v0, rel=1e-9) and p == pytest.approx(p0, rel=1e-9)
    if kw.get("shuffle"):
        assert fp == fp0


@pytest.mark.parametrize("kw", [dict(rot=1), dict(rot=2), dict(rot=3), dict(mirror=True),
                                dict(shift=(0.037, 0.011)), dict(shift=(0.11, 0.173))],
                         ids=["rot90", "rot180", "rot270", "mirror", "shift_a", "shift_b"])
def test_NA2_a_rotated_mirrored_or_shifted_board_keeps_its_drop_within_the_grid_error(mm_base, kw, request):
    """NA-2: rotating, mirroring or shifting the whole board changes only where the grid falls on the copper: the
    drop within 1 % (measured +0.02 to +0.44 % at 1x, tools/dev/metamorphic_dc.py)."""
    import metamorphic_dc as mm
    (v0, _, _), tmp = mm_base
    v, _, _ = mm.drop(tmp, mm.rail(**kw), "x" + request.node.callspec.id, 1.0)
    assert v == pytest.approx(v0, rel=0.01)


def test_NA3_reciprocity_linearity_and_energy_of_the_dc_solve(mm_base):
    """NA-3: the drop from U1 to U2 equals the one from U2 to U1 (a resistive network is reciprocal [Desoer 1969]);
    5 A gives 5 x the drop and 25 x the loss (linear); the loss is I·drop (Tellegen [Tellegen 1952]): each to
    1e-9."""
    import metamorphic_dc as mm
    (v0, p0, _), tmp = mm_base
    v, _, _ = mm.drop(tmp, mm.rail(), "swap", 1.0, swap=True)
    assert v == pytest.approx(v0, rel=1e-9)
    v5, p5, _ = mm.drop(tmp, mm.rail(), "x5", 1.0, amps=5.0)
    assert v5 == pytest.approx(5 * v0, rel=1e-9) and p5 == pytest.approx(25 * p0, rel=1e-9)
    assert p0 == pytest.approx(v0 * 1e-3 * 1.0, rel=1e-9)


def test_NA4_twice_the_copper_thickness_halves_a_tracks_resistance(tmp_path):
    """NA-4: on one layer, without vias, R = ρ·L/(w·t) everywhere (pads included): 70 µm copper halves the drop of
    35 µm to 1e-4 (the DC solve's 10 Hz skin effect is 1 + 4Δ⁴/45 with Δ = t/δ ≈ 0.003 [Dowell 1966])."""
    out = {}
    for cu in (0.035, 0.070):
        g = BoardGen(layers=2, size=(40.0, 20.0), cu_thickness=cu)
        for ref, at in (("U1", (6.0, 6.0)), ("U2", (34.0, 14.0))):
            g.footprint(ref, "IC", "Package_SO:Fake_IC", at, [{"number": 1, "at": (0, 0), "net": "PWR",
                                                               "size": (1.0, 1.0)}])
        g.polyline([(6.0, 6.0), (20.0, 6.0), (20.0, 14.0), (34.0, 14.0)], "PWR", "F.Cu", width=0.25)
        b, sc, _ = make(tmp_path, g, f"cu{int(cu * 1000)}", nets=())
        sc.set_dc_loads([DcLoadSpec("rail", "U1.1", [DcLoad("U2.1", 1.0, "")], "")])
        sc.save()
        r = simulate(b, SettingsFile.load(sc.path).config).dc_loads[0]
        assert not r.error, r.error
        out[cu] = r.loads[0]["supply_mv"]
    assert out[0.070] / out[0.035] == pytest.approx(0.5, rel=1e-4)


def test_NA5_a_repeated_simulate_gives_identical_numbers(mm_base):
    """NA-5: the same board and settings simulated twice in one process give bit-identical DC figures (the
    solver's plan of factorisations does not depend on timing or caches: ARCHITECTURE "Reproducible")."""
    import metamorphic_dc as mm
    (v0, p0, fp0), tmp = mm_base
    assert mm.drop(tmp, mm.rail(), "again", 1.0) == (v0, p0, fp0)


# --------------------------------------------------------------------------- against the independent sheet solve
def test_NA6_branches_and_a_tee_split_the_current_as_the_sheet_solve_does(tmp_path):
    """NA-6: two pads joined by a 0.4 and a 0.1 mm track in parallel, and a symmetric T feeding two loads, against
    the independent sheet solve with GroundTruth's boundary condition (each pad fed uniformly,
    refsolvers.sheet_potentials_fed, h = 0.01 mm; its own change from 0.02 mm under 0.5 %): R within 1 %, the
    narrow branch's share of the current within 2 %. Against equipotential pads (refsolvers.sheet_resistance) the
    same copper reads 1-2 % lower: that is the pads' model (A42), not the grid."""
    import adversarial_dc as adv
    par = adv.case_parallel(tmp_path, [1.0])[0]
    assert abs(par["err_fed"]) < 0.01 and par["fed_mohm"] == pytest.approx(par["fed_h002"], rel=0.005)
    assert par["share_narrow"] == pytest.approx(par["share_ref"], rel=0.02)
    tee = adv.case_tee(tmp_path, [1.0])[0]
    assert tee["err_fed"] < 0.01


def test_NA7_a_bend_converges_onto_the_sheet_solve(tmp_path):
    """NA-7: a 0.25 mm track bent through 90° with its arms at ±45° to the grid (staircases on every grid) against
    the sheet solve of the same copper turned by 45°, so the reference's arms lie on its grid (resistance does not
    depend on the orientation): 57.43 mΩ, h = 0.02 / 0.01 / 0.005 mm agree to 0.02 %. GroundTruth -0.70 / -0.14 %
    at 1x / 4x: within 1 % and converging."""
    import adversarial_dc as adv
    import refsolvers
    from shapely import affinity
    rows = {r["res"]: r for r in adv.case_acute(tmp_path, [1.0, 4.0]) if r["angle"] == 90.0}
    apex, a = (8.0, 20.0), math.radians(45.0)
    p1 = (apex[0] + 15 * math.cos(a), apex[1] + 15 * math.sin(a))
    p2 = (apex[0] + 15 * math.cos(a), apex[1] - 15 * math.sin(a))
    cu = adv._copper([((p1, apex), 0.25, "PWR"), ((apex, p2), 0.25, "PWR")], [("U1", p1), ("U2", p2)])

    def rot(g):
        """The copper turned so that the bend's arms lie on the reference's grid."""
        return affinity.rotate(g, -45, origin=apex)

    v = refsolvers.sheet_potentials_fed(rot(cu), [(rot(adv._pad(p1)), 1.0), (rot(adv._pad(p2)), -1.0)], 0.01,
                                        SIGMA_CU, T_CU)
    ref = (v[0] - v[1]) * 1e3
    e1, e4 = rows[1.0]["r_mohm"] / ref - 1, rows[4.0]["r_mohm"] / ref - 1
    assert abs(e1) < 0.01 and abs(e4) < abs(e1)
