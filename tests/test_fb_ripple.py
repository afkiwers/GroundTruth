"""The switching ripple at a controller's FB pin (analysis/fb_ripple.py): the sources' harmonics against FFTs of
the sampled waveforms, the parts-alone ripple against the exact steady state, the feedback network against closed
forms and a hand nodal solve, the copper against a track's resistance, and a COT stage end to end."""
import math
import sys
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parent))
from boardgen import cot_buck_board  # noqa: E402
from conftest import make  # noqa: E402
from groundtruth.analysis import fb_ripple as fr  # noqa: E402
from groundtruth.analysis.transfer import periodic_wave  # noqa: E402
from groundtruth.config.settings import ConverterSpec  # noqa: E402

F_SW, M = 500e3, 1 << 16


def _fft_coeffs(x: np.ndarray, n: np.ndarray) -> np.ndarray:
    """The complex Fourier coefficients c_n of one sampled period (x = Σ c_n e^{jnωt})."""
    return np.fft.fft(x)[n.astype(int)] / len(x)


@pytest.mark.parametrize("duty", [0.25, 0.5, 0.8])
def test_the_ripple_current_is_the_triangles_series(duty):
    """c_n of a triangle of ΔI p-p rising for D·T from −ΔI/2 at t = 0, against the FFT of the sampled triangle."""
    t = np.arange(M) / M / F_SW
    di, dt = 1.3, duty / F_SW
    i = np.where(t < dt, -di / 2 + di * t / dt, di / 2 - di * (t - dt) / (1 / F_SW - dt))
    n = np.arange(1, 60.0)
    got = fr.ripple_current(n, di, duty, F_SW, 0.0)
    want = _fft_coeffs(i, n)
    assert np.max(np.abs(got - want)) < 1e-6 * di


def test_the_switch_voltage_is_the_trapezoids_series():
    """A trapezoid from −0.5 V to 48 V with 20 ns edges, rising from t = 0, high (to the start of its fall) for D·T."""
    duty, edge = 0.3, 20e-9
    t = np.arange(M) / M / F_SW
    v = np.full(M, -0.5)
    rise = (t >= 0) & (t < edge)
    v[rise] = -0.5 + 48.5 * t[rise] / edge
    v[(t >= edge) & (t < duty / F_SW)] = 48.0
    fall = (t >= duty / F_SW) & (t < duty / F_SW + edge)
    v[fall] = 48.0 - 48.5 * (t[fall] - duty / F_SW) / edge
    n = np.arange(1, 80.0)
    got = fr.switch_voltage(n, 48.0, -0.5, duty, F_SW, edge)
    assert np.max(np.abs(got - _fft_coeffs(v, n))) < 2e-3


def test_the_parts_alone_ripple_is_the_capacitors_exact_steady_state():
    """22 µF, 5 mΩ ESR, 0.5 nH ESL (+ the mounting inductance) and a 1.65 Ω load, driven by a 1 A p-p triangle at
    D = 0.275: the peak-to-peak from the series (400 harmonics, Lanczos-weighted) against the exact steady state of
    the same circuit (the sampled triangle through Z(f) by FFT, every harmonic): within 1.5 % [A112]."""
    from groundtruth.analysis.simulate import MOUNT_L
    c, esr, esl, r, di, duty = 22e-6, 0.005, 0.5e-9, 1.65, 1.0, 0.275
    t = np.arange(M) / M / F_SW
    i = np.where(t < duty / F_SW, -di / 2 + di * t * F_SW / duty, di / 2 - di * (t * F_SW - duty) / (1 - duty))
    w = 2j * np.pi * np.fft.rfftfreq(M, 1 / (M * F_SW))
    w[0] = 1e-30
    zc = esr + w * (esl + MOUNT_L) + 1 / (w * c)
    z = 1 / (1 / zc + 1 / r)
    z[0] = 0
    exact = np.ptp(np.fft.irfft(np.fft.rfft(i) * z, M))
    n = np.arange(1, 401.0)
    zz = fr.lumped_impedance([{"C": c, "ESR": esr, "ESL": esl}], n * F_SW, r)
    got = np.ptp(periodic_wave(fr.ripple_current(n, di, duty, F_SW, 0.0) * zz, 2048))
    assert got == pytest.approx(exact, rel=0.015)
    # the textbook parts: ΔI/(8·C·f) for the capacitance alone [Erickson 2001]
    zc_only = fr.lumped_impedance([{"C": c, "ESR": 0.0, "ESL": -MOUNT_L}], n * F_SW, None)
    cap_pp = np.ptp(periodic_wave(fr.ripple_current(n, di, duty, F_SW, 0.0) * zc_only, 2048))
    assert cap_pp == pytest.approx(di / (8 * c * F_SW), rel=0.01)


def test_the_divider_with_a_feed_forward_cap_and_an_injection_network():
    """A 100k / 25k divider with 10 pF across the top: V_FB / V_tap = Z2 / (Z1 + Z2) (Z1 = R1 ∥ C_ff); with an
    injection network Rr (SW → RI), Cr (RI → tap), Cac (RI → FB), each source's transfer against a hand nodal solve of
    the two nodes FB and RI."""
    E = fr._Element
    f = np.array([5e5, 5e6, 5e7])
    w = 2j * np.pi * f
    r1, r2, cff = 100e3, 25e3, 10e-12
    els = [E("R1", "resistor", {"R": r1}, "tap:R1.1", "FB"), E("CF", "cap", {"C": cff}, "tap:CF.1", "FB"),
           E("R2", "resistor", {"R": r2}, "FB", "gnd:R2.2")]
    src = {"tap:R1.1": (None, "tap"), "tap:CF.1": (None, "tap"), "gnd:R2.2": (None, "gnd")}
    h = fr.network_transfers(els, src, f)
    y1, y2 = 1 / r1 + 0 * w, 1 / r2 + 0 * w
    assert np.allclose(h["tap:R1.1"] + h["tap:CF.1"], (y1 + w * cff) / (y1 + w * cff + y2), rtol=1e-6)
    assert np.allclose(h["gnd:R2.2"], y2 / (y1 + w * cff + y2), rtol=1e-6)
    rr, cr, cac = 220e3, 3.3e-9, 100e-9
    els = [E("R1", "resistor", {"R": r1}, "tap:R1.1", "FB"), E("R2", "resistor", {"R": r2}, "FB", "gnd:R2.2"),
           E("RR", "resistor", {"R": rr}, "SW", "n:RI"), E("CR", "cap", {"C": cr}, "n:RI", "tap:CR.2"),
           E("CAC", "cap", {"C": cac}, "n:RI", "FB")]
    src = {"tap:R1.1": (None, "tap"), "gnd:R2.2": (None, "gnd"), "SW": (None, "sw"), "tap:CR.2": (None, "tap")}
    h = fr.network_transfers(els, src, f)
    for k, fk in enumerate(f):
        s = 2j * math.pi * fk
        # nodes [FB, RI]: G·v = i from the sources (each 1 V in turn)
        g = np.array([[1 / r1 + 1 / r2 + s * cac, -s * cac], [-s * cac, 1 / rr + s * cr + s * cac]])
        for name, rhs in (("SW", [0, 1 / rr]), ("tap:R1.1", [1 / r1, 0]), ("tap:CR.2", [0, s * cr]),
                          ("gnd:R2.2", [1 / r2, 0])):
            v = np.linalg.solve(g, np.array(rhs, complex))
            assert h[name][k] == pytest.approx(v[0], rel=1e-6), (name, fk)


def _cot(tmp_path, tap="cap", injection=False, resolution=1.0, far_cap=False, **fields):
    """The COT board simulated: (its FbRippleResult, the Simulate result)."""
    from groundtruth.analysis.simulate import simulate
    g = cot_buck_board(tap=tap, injection=injection, far_cap=far_cap)
    g.fields("U1", **{"Control Mode": "COT", **fields})
    b, sc, _ = make(tmp_path, g, f"cot_{tap}_{int(injection)}_{int(far_cap)}", nets=())
    cfg = sc.config
    cfg.components["C2"].model.params.update(C=22e-6, ESR=0.002, ESL=0.5e-9)
    cfg.components["L1"].model.params.update(L=22e-6, R=0.0)
    cfg = replace(cfg, analysis=replace(cfg.analysis, resolution=resolution))
    cfg.converters[:] = [ConverterSpec("buck", "VIN", "GND", switch_node="SW", switch=["U1"], low_side=["D1"],
                                       f_sw=F_SW, i_out=0.5, v_in=48.0, v_out=12.0, rise_time=10e-9)]
    r = simulate(b, cfg)
    return r.converters[0].fb_ripple, r


def test_the_copper_between_the_inductor_and_the_capacitor_is_in_the_tap(tmp_path):
    """The divider tapping the 10 mm × 0.4 mm VOUT track at the inductor's end instead of the capacitor's: the
    difference of the tap's transfer at f_sw is that track's resistance, ρ·l/(w·t) = 12.3 mΩ, in its real part
    (within 15 %: the pads and the current's spread), and its inductance makes the inductor's end ripple far more
    (a low-ESR ceramic's ripple is mostly the copper's then)."""
    from groundtruth.analysis.loop import output_nets
    from groundtruth.analysis.power import switch_inductor
    z = {}
    for tap in ("cap", "inductor"):
        res, r = _cot(tmp_path, tap=tap)
        assert res.copper and not res.error
        z[tap] = res
        setup = r.converter_setups[0]
        model, spec = r.model, setup.spec
        sw, ind = switch_inductor(model.board, model.config, spec)
        outs = output_nets(model.board, model.config, spec)
        els, src, _ = fr.feedback_network(model.board, model.config, spec, "FB", "U1", outs, sw, "GND")
        zz, _ = fr.tap_impedances(model, spec, ind, sw, fr._controller_ground(model.board.footprint("U1"), "GND"), src, outs, "GND",
                                  np.array([F_SW, 2 * F_SW]), res.caps)
        z[tap] = (res, zz["tap:R1.1"][0])
    r_track = 1.724e-8 * 10.1e-3 / (0.4e-3 * 35e-6)
    assert (z["inductor"][1] - z["cap"][1]).real == pytest.approx(r_track, rel=0.15)
    assert z["inductor"][0].tap_pp > 2 * z["cap"][0].tap_pp


def test_a_cot_stage_is_checked_against_its_fb_ripple_min(tmp_path):
    """FB Ripple Min 25 mV on the controller: with a 2 mΩ ceramic alone the FB pin sees a few mV (an issue
    fb_ripple, its note and status "low"); an injection RC from the switch node lifts it over the minimum (no
    issue), and its share is reported. The grid is converged: twice the resolution within 2 %."""
    low, r = _cot(tmp_path, **{"FB Ripple Min": "25mV"})
    assert low.status == "low" and low.fb_pp < 25e-3 and low.limits["min"][0] == pytest.approx(25e-3)
    assert any(i.check == "fb_ripple" for i in r.issues)
    assert any(n.startswith("FB ripple at U1.") for n in r.converters[0].notes)
    assert len(low.t) == fr.WAVE_KEPT and np.ptp(low.v_fb) == pytest.approx(low.fb_pp, rel=0.05)
    ok, r2 = _cot(tmp_path, injection=True, **{"FB Ripple Min": "25mV"})
    assert ok.status == "ok" and ok.fb_pp >= 25e-3 and ok.paths["injection"] > ok.paths["output"]
    assert not any(i.check == "fb_ripple" for i in r2.issues)
    fine, _ = _cot(tmp_path, resolution=2.0)
    assert fine.fb_pp == pytest.approx(low.fb_pp, rel=0.02)
    js = r.converters[0].summary()["fb_ripple"]
    assert js["kind"] == "fb_ripple" and js["fb_pp"] == pytest.approx(low.fb_pp)


def test_the_ripple_is_shown_tiles_text_plots_and_report(tmp_path):
    """The view (its own entry after the stages), its tiles (FB ripple judged against the limit), the console line,
    both plots and the HTML report's section."""
    import matplotlib
    matplotlib.use("Agg")
    from matplotlib.figure import Figure
    from groundtruth.report.formats import converter_text, fb_ripple_text
    from groundtruth.report.plots import draw_fb_ripple, draw_fb_ripple_sources
    from groundtruth.ui.kpi import kpis
    from groundtruth.ui.main_frame import result_views
    res, r = _cot(tmp_path, injection=True, **{"FB Ripple Min": "25mV"})
    views = result_views(r)
    assert res in views and res.label().endswith("FB ripple")
    tiles = kpis(res)
    assert tiles[0][0] == "FB ripple" and tiles[0][3] == "ok" and any(t[0] == "from the injection" for t in tiles)
    assert any(t[0] == "FB ripple" for t in kpis(r.converters[0]))
    assert fb_ripple_text(res)[0].strip().startswith("FB ripple at U1.")
    assert sum("FB ripple at" in line for line in converter_text(r.converters[0])) == 1
    for draw in (draw_fb_ripple, draw_fb_ripple_sources):
        fig = Figure(figsize=(8, 3))
        draw(fig, res)
        assert fig.axes and fig.axes[0].get_lines()
    from groundtruth.report.html import render_html
    page = render_html(r.model.board, r.issues, r.routes, "simulation", converters=r.converters)
    assert "id='fbripple" in page and "FB ripple" in page and "U1." in page


def test_a_far_output_capacitor_is_left_out_of_the_copper_model(tmp_path, monkeypatch):
    """Output capacitors farther than CAP_NEAR_MM from the stage stay out of the copper model, with a note (a rail's
    capacitors across a large board would make the model the board's)."""
    monkeypatch.setattr(fr, "CAP_NEAR_MM", 9.5)            # C2 8.9 mm from the divider, C3 10.0 mm
    res, r = _cot(tmp_path, far_cap=True)
    assert {c["ref"] for c in res.caps} == {"C2", "C3"}
    assert res.copper and any("1 output capacitor(s) farther than 9.5 mm" in n for n in res.notes)
    monkeypatch.setattr(fr, "CAP_NEAR_MM", 1.0)          # none near: the nearest one stays in
    alone, _ = _cot(tmp_path)
    assert alone.copper
