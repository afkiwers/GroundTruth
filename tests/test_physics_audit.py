"""Round 5 of the validation audit (VALIDATION_AUDIT.md "Round 5"): the physics corrected beside the track width
(tests/test_track_width.py), each against a result that does not come from the code it checks.

- PA-1: a foil's AC resistance (kernel.skin_thickness, [Dowell 1966] [A2]) against the surface impedance of a
  conducting slab with the field on one face, Z_s = (k/σ)·coth(k·t), k = (1 + j)/δ, written out here with complex
  numbers;
- PA-2: the plane pairs' capacitance through two different dielectrics against two capacitors in series;
- PA-3: air's viscosity and conductivity in the convection correlations against tabulated values.
"""
from __future__ import annotations

import cmath
import math

import numpy as np
import pytest

from groundtruth.solver.kernel import MU0, skin_thickness
from groundtruth.solver.peec import SIGMA_CU


def _slab_r_ratio(t: float, f: float, sigma: float = SIGMA_CU) -> float:
    """R_ac / R_dc of a slab t thick with the magnetic field on one face, from its surface impedance
    Z_s = (k/σ)·coth(k·t), k = (1 + j)/δ (the 1D diffusion equation's solution with H = 0 on the far face)."""
    delta = 1 / math.sqrt(math.pi * f * MU0 * sigma)
    k = (1 + 1j) / delta
    zs = k / sigma / cmath.tanh(k * t)
    return zs.real * sigma * t


@pytest.mark.parametrize("f", [1e3, 1e5, 1e6, 3.6e6, 1e7, 1e8, 1e9, 1e10])
@pytest.mark.parametrize("t", [17.5e-6, 35e-6, 70e-6])
def test_PA1_a_foil_s_ac_resistance_is_the_one_sided_slab_s(t, f):
    """PA-1: t / skin_thickness(t, f) = the slab's R_ac / R_dc within 1e-6 for ½, 1 and 2 oz copper from 1 kHz to
    10 GHz (through the thin-foil series below Δ = 0.1 and the closed form above). The old effective thickness
    δ(1 - e^(-t/δ)) read up to 58 % high there (ISSUE-16)."""
    ratio = t / skin_thickness(t, f, SIGMA_CU)
    assert ratio == pytest.approx(_slab_r_ratio(t, f), rel=1e-6)


def test_PA1_the_skin_factor_s_limits_and_arrays():
    """PA-1: t at DC, δ once δ ≪ t (R_ac = R_dc·t/δ), the same values elementwise for an array of frequencies."""
    t = 35e-6
    assert skin_thickness(t, 0.0, SIGMA_CU) == pytest.approx(t)
    f = 1e10
    delta = 1 / math.sqrt(math.pi * f * MU0 * SIGMA_CU)
    assert skin_thickness(t, f, SIGMA_CU) == pytest.approx(delta, rel=1e-9)
    fs = np.array([0.0, 1e6, 1e8])
    arr = skin_thickness(t, fs, SIGMA_CU)
    assert np.allclose(arr, [skin_thickness(t, x, SIGMA_CU) for x in fs])


def test_PA2_plane_pair_capacitance_through_two_dielectrics_is_in_series(tmp_path):
    """PA-2: two planes with 0.1 mm of εr 3.5 and 0.1 mm of εr 4.5 between them (a hybrid stack): the model's
    capacitance per area is ε0 / Σ(t_i / εr_i) (two capacitors in series) within 1e-6, and its loss tangent the
    energy-weighted one; the thickness-weighted mean εr (4.0) read it 1.6 % high."""
    from boardgen import BoardGen
    from conftest import make
    from groundtruth.analysis.simulate import BoardModel
    from groundtruth.solver.peec import EPS0
    g = BoardGen(layers=3, size=(20.0, 20.0), dielectrics=[0.1, 0.1])
    g.plane("GND", "F.Cu")
    g.plane("GND", "B.Cu")
    text = g.text()
    # the generator writes one εr for every dielectric: set the two apart in the file
    first = text.index("(epsilon_r 4.5)")
    text = text[:first] + "(epsilon_r 3.5)" + text[first + len("(epsilon_r 4.5)"):]
    path = tmp_path / "hybrid.kicad_pcb"
    path.write_text(text, encoding="utf-8")
    from groundtruth.adapters.file_source import load_board
    board = load_board(path)
    stk = board.stackup
    assert [d.epsilon_r for d in stk.dielectrics] == [3.5, 4.5]
    expected = EPS0 / (0.1e-3 / 3.5 + 0.1e-3 / 4.5)            # F/m², two capacitors in series
    b, sc, _ = make(tmp_path, g, "plain", nets=())
    model = BoardModel(board, sc.config)
    st = model.build((0.0, 0.0, 20.0, 20.0), ["F.Cu", "B.Cu"], {"GND"}, 0.5, True)
    pair = st.pairs[0]
    t = stk.dielectric_between("F.Cu", "B.Cu") * 1e-3          # foil surfaces apart: the two dielectrics + In1
    assert pair.c_per_area * t == pytest.approx(expected * (0.2e-3), rel=1e-6)
    assert stk.epsilon_series("F.Cu", "B.Cu") == pytest.approx(0.2 / (0.1 / 3.5 + 0.1 / 4.5), rel=1e-12)
    assert stk.epsilon_between("F.Cu", "B.Cu") / stk.epsilon_series("F.Cu", "B.Cu") == pytest.approx(1.016, abs=0.001)


def test_PA3_air_properties_against_tables():
    """PA-3: air at 300 K and 1 atm: the dynamic viscosity from Sutherland's law with White's constants (273 K,
    111 K) [White 2006] within 0.3 % of the tabulated 184.6e-7 Pa·s, the conductivity's power law [A28] within 2 % of
    26.3e-3 W/(m·K), Pr within 2 % of 0.707 (Incropera's Table A.4 [Incropera 2011])."""
    from groundtruth.thermal.solver.convection import air
    k, nu, pr, beta = air(300.0 - 273.15)
    rho = 101325.0 / (287.05 * 300.0)
    assert nu * rho == pytest.approx(184.6e-7, rel=0.003)
    assert k == pytest.approx(26.3e-3, rel=0.02)
    assert pr == pytest.approx(0.707, rel=0.02)
    assert beta == pytest.approx(1 / 300.0)


def test_PA4_the_hot_loop_s_switching_current_heats_the_copper(tmp_path):
    """PA-4: a synchronous buck (tests/boardgen.discrete_buck_board: C1 on the VIN pour, Q1 / Q2, 24 → 5 V at 3 A, 400
    kHz): besides its DC paths it lists the hot loop's switching current, the rail capacitors' current of zero mean
    and RMS I_L·√(D(1−D)) = 3·√(5/24·19/24) = 1.218 A (the high side's current less its mean [Erickson 2001]),
    from C1 through the VIN copper into Q1 and from Q2 back through the ground into C1 [A43]; its densities are RMS,
    its loss I_rms²·R of that loop, and the heat Thermal gets from it is that loss (within 0.5 %)."""
    import numpy as np
    from boardgen import discrete_buck_board
    from conftest import make
    from groundtruth.analysis.simulate import simulate
    from groundtruth.analysis.thermal import _rho_mm
    from groundtruth.config.settings import ConverterSpec, SettingsFile
    board, sc, _ = make(tmp_path, discrete_buck_board(), "hot", nets=())
    spec = ConverterSpec("buck 5V", "VIN", "GND", "SW", ["Q1"], ["Q2"], f_sw=400e3, i_out=3.0, v_in=24.0,
                         v_out=5.0, rise_time=4e-9, c_oss=1.2e-9)
    sc.set_converters([spec])
    sc.save()
    cfg = SettingsFile.load(sc.path).config
    res = simulate(board, cfg).converters[0]
    assert not res.error, res.error
    hot = [p for p in res.dc if p.get("ac")]
    assert len(hot) == 1 and "C1" in hot[0]["name"]
    d = 5.0 / 24.0
    assert hot[0]["current_a"] == pytest.approx(3.0 * math.sqrt(d * (1 - d)), rel=1e-6)
    assert hot[0]["loss_w"] == pytest.approx(hot[0]["current_a"] ** 2 * hot[0]["r_mohm"] * 1e-3, rel=1e-9)
    # the heat of its variance maps (A²/mm⁴ per layer) over the copper: its own copper loss
    name = hot[0]["name"]
    rho = _rho_mm()
    heat = 0.0
    for layer, sq in res.dc_ripple_sq[name].items():
        g = res.dc_grids[name][layer]
        t = board.stackup.layer(layer).thickness
        heat += float(rho * np.sum(sq.astype(float) * g.copper_area()) * t * g.d * g.d)
    vias = 0.0                              # the via barrels' share: R·I² of each segment
    if name in res.dc_ripple_via:
        db, amps = res.dc_ripple_via[name]
        vias = sum(r * a * a for (_, _, r), a in zip(db.via_ends, amps))
    assert heat + vias == pytest.approx(hot[0]["loss_w"], rel=0.005)
