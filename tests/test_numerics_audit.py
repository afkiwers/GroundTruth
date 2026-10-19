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


# --------------------------------------------------------------------------- the inductance kernel's near field
def _tape_reference(length, wa, wb, a, lat, dz):
    """The tapes' mutual inductance by scipy's adaptive quadrature of the same one-dimensional integral (an
    independent integration of kernel.filament_mutual, which is exact for filaments [Grover 1946])."""
    from scipy.integrate import quad
    from groundtruth.solver.kernel import filament_mutual

    def f(v):
        """The pairs' weight times the filament mutual at difference v."""
        w = max(0.0, min(wa / 2, v + wb / 2) - max(-wa / 2, v - wb / 2))
        return w * float(filament_mutual(length, np.array(a), np.array(math.hypot(lat + v, dz))))

    h, k = (wa + wb) / 2, abs(wa - wb) / 2
    pts = sorted({-h, -k, k, h} | ({-lat} if -h < -lat < h else set()))
    return sum(quad(f, p, q, limit=400, epsabs=0, epsrel=1e-12)[0]
               for p, q in zip(pts[:-1], pts[1:]) if q > p) / (wa * wb)


@pytest.mark.parametrize("case", [(1, 1, 0, 0, 0), (1, 1, 1, 0, 0), (1, 1, 0, 1, 0), (1, 1, 0, 0, 0.05),
                                  (1, 1, 1, 1, 0.02), (0.3, 1, 0, 0.6, 0), (0.1, 0.7, 2, 0.4, 0.001)])
def test_ISSUE28_tape_mutual_is_the_exact_integral(case):
    """ISSUE-28 (1): kernel.tape_mutual (the strips' near-field mutual and self inductance) against an adaptive
    quadrature of the same integral to 1e-9. The strip's self term was 24 sub-strips with the GMD on the diagonal:
    0.86 % low for a square strip (it converges as 1/n)."""
    from groundtruth.solver.kernel import strip_self_numeric, tape_mutual
    d = 0.5e-3
    wa, wb, a, lat, dz = (v * d for v in case)
    assert tape_mutual(d, wa, wb, a, lat, dz) == pytest.approx(_tape_reference(d, wa, wb, a, lat, dz), rel=1e-9)
    assert strip_self_numeric(d, d) == pytest.approx(_tape_reference(d, d, d, 0, 0, 0), rel=1e-9)


@pytest.mark.parametrize("ratio, tol", [(2.0, 0.006), (10.0, 0.002), (20.0, 0.002)])
def test_ISSUE28_a_plane_pairs_sheet_inductance_is_mu0_dz_however_thin_the_dielectric(ratio, tol):
    """ISSUE-28 (2): two planes dz apart with opposite uniform currents have μ0·dz of loop inductance per square
    [Pozar 2012]: in the kernels, 2·Σ(K(0) − K(dz)) over the plane. That is the small difference of two large sums,
    so the near field's error is magnified about d/dz times: with 8 x 8 Gauss points across the strips it read 3.9 %
    and 20 % low at d/dz = 10 and 20 (a cavity's resonance 2 and 10 % high; kraft_flux_mini_2's board-wide model has
    d/dz = 6.4 at 1x; 1.2 % low at d/dz = 2). Now within 0.2 % at 10 and 20, 0.5 % at 2: the rest is the finite
    41 x 41 plane, whose missing outside grows with dz."""
    from groundtruth.solver.kernel import MU0, strip_kernel
    d = 0.5e-3
    dz = d / ratio
    n = 41
    sheet = 2 * (strip_kernel(n, n, d, 0.0) - strip_kernel(n, n, d, dz)).sum()
    assert sheet == pytest.approx(MU0 * dz, rel=tol)


# --------------------------------------------------------------------------- degenerate inputs
def test_ISSUE30_a_stackup_without_copper_or_dielectric_thickness_is_said_and_consistent(tmp_path):
    """ISSUE-30: a stackup with 0 µm of copper on a layer was read as 0 (no warning): the PEEC models took 35 µm for
    it (simulate's fallback), Thermal's copper heat ρJ²·t·A the 0, so the two disagreed and nothing said so; a
    dielectric of 0 mm was read without a word (the layers 1 µm apart in the models). Now the reader takes the
    default 35 µm for the copper with a board warning, and warns about the dielectric."""
    from groundtruth.adapters.file_source import load_board
    g = BoardGen(layers=2, size=(46.0, 20.0), cu_thickness=0.0)
    b = load_board(g.write(tmp_path / "cu0.kicad_pcb"))
    assert all(c.thickness == pytest.approx(0.035) for c in b.stackup.copper)
    assert any("F.Cu" in w and "35 µm" in w for w in b.warnings)
    g = BoardGen(layers=2, size=(46.0, 20.0), dielectrics=[0.0])
    b = load_board(g.write(tmp_path / "d0.kicad_pcb"))
    assert any("no thickness" in w and "not physical" in w for w in b.warnings)
    g = BoardGen(layers=2, size=(46.0, 20.0))
    assert not load_board(g.write(tmp_path / "ok.kicad_pcb")).warnings


def test_ISSUE25_a_bridge_whose_leg_did_not_solve_says_its_rms_currents_lack_it(tmp_path, monkeypatch):
    """ISSUE-25 for motor bridges: the RMS currents (maps, via loads, the heat) are built from each leg's
    battery-to-motor DC solve and its PWM-band solves; one that failed was dropped without a word unless all failed.
    Now RmsBasis lists it (rms_missing), a dc_unsolved warning and Thermal's notes name it. Forced here by removing
    the first leg's high-side DC solution, as a SolverError leaves it."""
    from boardgen import bridge_board
    from groundtruth.analysis import bridge_rms
    from groundtruth.analysis.detect import detect_bridges
    from groundtruth.analysis.thermal import thermal_inputs
    from groundtruth.config.settings import MotorSpec
    board, sc, _ = make(tmp_path, bridge_board(), "brfail", nets=(), max_nodes=20000)
    (d,) = detect_bridges(board, sc.config)
    b = d.spec
    b.v_bus, b.f_pwm, b.i_phase, b.speed_rpm, b.c_oss = 48.0, 40e3, 30.0, 3000.0, 1e-9
    b.motor = MotorSpec("star", 0.05, 50e-6, 5.0, 7)
    sc.add_detected_bridges([b], d.models)
    sc.save()
    cfg = SettingsFile.load(sc.path).config
    real = bridge_rms.RmsBasis.__init__

    def failing(self, model, bs, legs, column):
        """RmsBasis after the first leg's high-side battery-to-motor solve failed."""
        src, k, run = bs.rms_runs[0]
        run.sols[power.DC_F] = None
        real(self, model, bs, legs, column)

    monkeypatch.setattr(bridge_rms.RmsBasis, "__init__", failing)
    res = simulate(board, cfg)
    (br,) = res.bridges
    assert len(br.rms_missing) == 1 and "battery-to-motor" in br.rms_missing[0]
    assert any(i.check == "dc_unsolved" and i.net == br.net for i in res.issues)
    assert any("not solved" in n for n in br.notes)
    _, _, notes = thermal_inputs(board, cfg, [br])
    assert any(br.rms_missing[0] in n and "heat" in n for n in notes)


# --------------------------------------------------------------------------- generated cases
def _random_tracks(n: int, seed: int = 20261020) -> list[tuple]:
    """n straight tracks (width mm, angle °, length mm, offset mm) drawn from a fixed seed: widths 0.05-1 mm
    log-uniform, any angle from 0 to 90°, 15-35 mm long, anywhere within a cell."""
    rng = np.random.default_rng(seed)
    return [(float(np.exp(rng.uniform(math.log(0.05), math.log(1.0)))), float(rng.uniform(0.0, 90.0)),
             float(rng.uniform(15.0, 35.0)), float(rng.uniform(0.0, 0.25))) for _ in range(n)]


@pytest.mark.parametrize("case", _random_tracks(8), ids=lambda c: f"w{c[0]:.3f}_a{c[1]:.0f}")
def test_NA8_generated_tracks_keep_the_resistance_of_their_cross_section(tmp_path, case):
    """NA-8: straight tracks with a width, angle, length and position on the grid drawn at random (fixed seed): the
    resistance per length from two lengths 20 mm apart (the pads cancel) against 1/(σ·w·t) [IEC 60028] at 1x: within
    1.5 % for a track at least a cell wide, 4 % for a narrower one. Round 5's TW-1 / TW-3 checked 0.05-0.4 mm at 0°,
    30° and 45° (within 1 %); these cases found the rest (ISSUE-31): a track narrower than a cell reads +1 to +2.5 %
    within ~10° of an axis and up to ±3.6 % at some angles and positions (57-60°), the finite-volume error of copper
    crossing a cell row over several cells; it converges (6°: +2.2 / +2.0 / +0.7 % at 1x / 2x / 4x;
    tools/dev/angle_scan.py)."""
    w, angle, length, off = case
    a = math.radians(angle)
    drops = []
    for L in (length, length + 20.0):
        x0, y0 = 6.0 + off, 6.0 + off * 0.7
        x1, y1 = x0 + L * math.cos(a), y0 + L * math.sin(a)
        g = BoardGen(layers=2, size=(x1 + 8.0, y1 + 8.0))
        for ref, at in (("U1", (x0, y0)), ("U2", (x1, y1))):
            g.footprint(ref, "IC", "Package_SO:Fake_IC", at, [{"number": 1, "at": (0, 0), "net": "PWR",
                                                               "size": (1.0, 1.0)}])
        g.track((x0, y0), (x1, y1), "PWR", "F.Cu", width=w)
        b, sc, _ = make(tmp_path, g, f"r{int(L * 10)}", nets=())
        sc.set_dc_loads([DcLoadSpec("rail", "U1.1", [DcLoad("U2.1", 1.0, "")], "")])
        sc.save()
        r = simulate(b, SettingsFile.load(sc.path).config).dc_loads[0]
        assert not r.error, r.error
        drops.append(r.loads[0]["supply_mv"] * 1e-3)
    r_per_m = (drops[1] - drops[0]) / 0.020
    tol = 0.015 if w >= 0.25 else 0.04                  # the 1x DC grid's cell here: 0.25 mm
    assert r_per_m == pytest.approx(1 / (SIGMA_CU * w * 1e-3 * T_CU), rel=tol), case


# --------------------------------------------------------------------------- the worst emission is a solved value
def test_ISSUE32_the_worst_emission_is_solved_even_when_the_budget_is_spent(tmp_path):
    """ISSUE-32: with plane capacitance the reported worst harmonic gets a solve of its own unless one lies within
    ±5 % (ARCHITECTURE "Frequency planning"), so the worst case is not an interpolation across a resonance. But
    next_frequencies returned at once when analysis.max_solves was spent, before that rule: kraft_flux_mini_2's
    SPI_SCK at 1x reported +13.2 dB at 1220 MHz, interpolated between solves at 1020 and 1580 MHz, where solving
    every harmonic gives +7.9 dB (a resonance between them). Now the worst's own solve may go up to WORST_EXTRA past
    the budget (kraft: one more, +7.85 dB at 1220 MHz), and a worst still interpolated gets a note, as do gaps the
    budget left unresolved near the limit. Here a budget of 3 solves on the cavity test board: the worst lies within
    ±5 % of a solve of the board-wide model (or is noted); on the old code it was interpolated between 150 MHz and
    3.15 GHz."""
    from boardgen import layer_change_board
    from groundtruth.analysis import simulate as sim
    from groundtruth.config.settings import NetSpec
    extra, near_log = getattr(sim, "WORST_EXTRA", 3), sim.WORST_SOLVED_LOG
    board, sc, _ = make(tmp_path, layer_change_board(None), "cavw", nets=(NetSpec("SIG", "U1.1", ["U2.1"], 50e6, 1e-9),),
                        capacitance=True, solve_points=3, max_solves=3, cavity_max_nodes=12000)
    rr = simulate(board, sc.config).routes[0]
    assert not rr.error, rr.error
    solved = [s["f"] for s in rr.cavity_solves]
    assert len(solved) <= 3 + extra
    f = rr.worst["f"]
    near = min(abs(math.log(f / x)) for x in solved) <= near_log
    assert near or any("is interpolated" in n for n in rr.notes), (f, solved)
    # the budget ended with gaps near the limit still uncertain: said, with where they are
    assert any("solve budget" in n and "not resolved" in n for n in rr.notes)
