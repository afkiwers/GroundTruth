"""Frequency responses, poles and zeros (analysis/transfer.py, the plan's M4): vector fitting recovers known
rational functions (an RC low-pass, a series RLC impedance, a system with two resonances and a real zero, an
impedance turning inductive), the exact poles of a rational model, and the f0 / Q of a pole pair."""
import math

import numpy as np
import pytest

from groundtruth.analysis.transfer import (exact_response, fitted_response, pz_rows, rational_eval, vector_fit)

F = np.geomspace(1e3, 1e9, 300)


def close(roots, want, rel=1e-3):
    """Every wanted root has a fitted one within rel of it."""
    return all(min(abs(r - w) for r in roots) <= rel * abs(w) for w in want)


def test_an_rc_low_pass_has_its_pole_at_one_over_rc():
    """1/(1 + sRC), R 1 kΩ, C 10 nF: one real pole at -1/RC (15.9 kHz), no finite zero."""
    rc = 1e3 * 10e-9
    h = 1 / (1 + 2j * np.pi * F * rc)
    fit = vector_fit(F, h)
    assert close(fit.poles, [-1 / rc]) and fit.rms < 1e-6
    rows = pz_rows(fit.poles)
    assert rows[0]["q"] is None and rows[0]["f0"] == pytest.approx(1 / (2 * math.pi * rc), rel=1e-4)


def test_a_series_rlc_impedance_has_its_zeros_at_the_self_resonance():
    """A capacitor's Z = ESR + sL + 1/(sC) (10 nF, 0.5 nH, 10 mΩ): a pole at 0 (the capacitance), zeros at its
    self-resonance 1/(2π·sqrt(LC)) = 71.2 MHz with Q = sqrt(L/C)/ESR = 22.4; fitted with the s·e term."""
    c, l, r = 10e-9, 0.5e-9, 0.01
    s = 2j * np.pi * F
    h = r + s * l + 1 / (s * c)
    resp = fitted_response("C", "test", "impedance", F, h, use_e=True)
    (z,) = pz_rows(resp.zeros)
    assert z["f0"] == pytest.approx(1 / (2 * math.pi * math.sqrt(l * c)), rel=1e-3)
    assert z["q"] == pytest.approx(math.sqrt(l / c) / r, rel=1e-2)
    assert resp.fit_error_db < 0.01


def test_two_resonances_and_a_real_zero():
    """A 4th-order system: poles at 1 MHz (Q 5) and 20 MHz (Q 2), a real zero at 300 kHz; recovered within 0.1 %;
    the exact poles of the same N/D are its roots."""
    def pair(f0, q):
        """s² + s·w0/Q + w0² of a pole pair."""
        w0 = 2 * math.pi * f0
        return [1.0, w0 / q, w0 * w0]
    den = np.polymul(pair(1e6, 5), pair(20e6, 2))
    num = np.array([1.0, 2 * math.pi * 3e5]) * 1e14
    h = rational_eval(num, den, F)
    fit = vector_fit(F, h)
    want = np.roots(den)
    assert close(fit.poles, want, 1e-3) and close(fit.zeros, [-2 * math.pi * 3e5], 1e-3)
    exact = exact_response("x", "test", "voltage gain", F, num, den)
    rows = pz_rows(exact.poles)
    assert [round(r["f0"] / 1e6, 3) for r in rows] == [1.0, 20.0] and rows[0]["q"] == pytest.approx(5.0)
    assert exact.exact and np.allclose(exact.h, h)


def test_a_coarse_fit_says_so_and_a_delay_is_left_above_the_band():
    """A pure delay e^(-sτ) is not rational: the fit is noted as approximate, and its poles far above the band are
    not listed."""
    h = np.exp(-2j * np.pi * F * 5e-9)
    resp = fitted_response("line", "test", "voltage gain", F, h, max_order=6)
    assert any("coarse" in n or "above ten times" in n for n in resp.notes)


def test_the_bode_legend_names_the_curves_and_the_markers_it_shows():
    """The Bode plot's legend (the user: "some form of legend would help"): each curve, then "pole" and "zero" for
    the markers drawn; a curve with no pole or zero in its sweep adds neither."""
    from matplotlib.figure import Figure
    from groundtruth.analysis.transfer import BodeView
    from groundtruth.report.plots import draw_bode
    rc = 1e3 * 10e-9                                         # a pole at 15.9 kHz, a zero at 159 kHz
    lead = exact_response("net: receiver / source", "net", "voltage gain", F, [rc / 10, 1.0], [rc, 1.0])
    flat = exact_response("U1: output", "net", "voltage gain", F, [1.0], [1.0])

    def labels(responses):
        """The legend's labels of a Bode plot of `responses`."""
        fig = Figure(figsize=(9.5, 5.0))
        draw_bode(fig, BodeView("net", responses))
        return [t.get_text() for t in fig.axes[0].get_legend().get_texts()]
    assert labels([lead, flat]) == ["receiver / source", "U1: output", "pole", "zero"]
    assert labels([flat]) == ["U1: output"]


def test_hovering_a_pole_or_zero_names_it():
    """The plugin's tooltip over a Bode plot's or the pole-zero map's marker (the user: "when hovering over the
    poles and zeros with the mouse can you show a hover-over note like a tooltip?"): plots.mark_at at the marker's
    pixel gives whose pole or zero it is, its frequency, a pair's Q and ζ; away from the markers nothing."""
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure
    from groundtruth.analysis.transfer import BodeView
    from groundtruth.report.plots import draw_bode, draw_pz, mark_at
    rc = 1e3 * 10e-9                                         # a real pole at 15.9 kHz
    w0, q = 2 * math.pi * 1e6, 2.0                           # and a pair at 1 MHz, Q 2
    rc_resp = exact_response("net: receiver / source", "net", "voltage gain", F, [1.0], [rc, 1.0])
    pair = exact_response("U1: output", "net", "voltage gain", F, [w0 ** 2], [1.0, w0 / q, w0 ** 2])

    def tips(draw):
        """{text: pixel} of every recorded marker, and the figure (rendered, so its transforms hold)."""
        fig = Figure(figsize=(9.5, 5.0))
        draw(fig, BodeView("net", [rc_resp, pair]))
        FigureCanvasAgg(fig).draw()
        ax, marks = next(iter(fig._gt_marks.items()))
        return fig, ax, {m[3]: ax.transData.transform((m[0], m[1])) for m in marks}

    for draw in (draw_bode, draw_pz):
        fig, ax, at = tips(draw)
        assert "receiver / source: real pole at 15.9 kHz" in at
        assert "U1: output: pole pair at 1 MHz, Q 2 (ζ 0.25)" in at
        x, y = at["receiver / source: real pole at 15.9 kHz"]
        assert mark_at(fig, ax, x + 3, y - 2) == "receiver / source: real pole at 15.9 kHz"
        assert mark_at(fig, ax, x + 40, y + 40) == ""


def test_the_response_curves_and_summary():
    """Magnitude in dB (a ratio) or Ω (an impedance), the phase unwrapped (a delay's phase keeps falling), the JSON
    summary decimated."""
    h = np.exp(-2j * np.pi * F * 5e-9)
    resp = fitted_response("line", "test", "voltage gain", F, h, max_order=4)
    assert np.allclose(resp.magnitude(), 0.0, atol=1e-9)
    ph = resp.phase_deg()
    assert ph[-1] == pytest.approx(-360 * 1e9 * 5e-9, rel=1e-6)
    s = resp.summary(points=50)
    assert len(s["f"]) == 50 and s["quantity"] == "voltage gain"


def test_a_signal_paths_rc_filter_pole_is_one_over_the_resistance_times_c(tmp_path):
    """The series path with 1 nF from SIG_B to GND after its 33 Ω (test_path's RC low-pass): the receiver / source
    response's dominant pole is a real one at 1/(2π·(25 Ω driver + 33 Ω)·1 nF) = 2.74 MHz within 1 %; the lines
    add poles far above it."""
    from test_path import _shunt_setup
    from groundtruth.analysis.simulate import simulate
    board, cfg = _shunt_setup(tmp_path, ("C9", "1n", "C", 20.0, "SIG_B"), "rc")
    rr = simulate(board, cfg).routes[0]
    (resp,) = rr.responses
    assert resp.quantity == "voltage gain" and resp.exact             # the circuit's exact poles (analog_net)
    first = pz_rows(resp.poles)[0]
    want = 1 / (2 * math.pi * (cfg.analysis.driver_ohms + 33.0) * 1e-9)
    assert first["q"] is None and first["f0"] == pytest.approx(want, rel=0.01)
    assert resp.magnitude()[0] == pytest.approx(0.0, abs=0.1)              # unity well below the corner
    assert rr.summary()["responses"][0]["poles"][0]["f0"] == pytest.approx(first["f0"])


def test_an_op_amp_stages_pole_is_its_gbw_over_its_gain(tmp_path):
    """An op-amp stage of gain 10 with GBW 10 MHz (its Gain field: the gain block): its output per volt of the source
    has its pole at 1 MHz (the 33 Ω and its input C add nothing below 10x the sweep), and it is the whole path's
    dominant pole too."""
    from test_path import _opamp_setup
    from groundtruth.analysis.simulate import simulate
    board, cfg = _opamp_setup(tmp_path, Gain="10", GBW="10MHz")
    rr = simulate(board, cfg).routes[0]
    path, stage = rr.responses
    assert stage.name == "U5: output"
    p = pz_rows(stage.poles)[0]
    assert p["f0"] == pytest.approx(1e6, rel=1e-3)
    f = stage.f[0]                                  # the sweep starts at 200 kHz: already 0.17 dB down
    assert stage.magnitude()[0] == pytest.approx(20 * math.log10(10 / math.hypot(1, f / 1e6)), abs=1e-3)
    assert pz_rows(path.poles)[0]["f0"] == pytest.approx(1e6, rel=0.01)


def test_the_ringing_rational_is_the_snubbed_response():
    """ringing_rational evaluates to snubbed_response (and without a snubber to ringing_response): poles at the
    series resonance 1/(2π·sqrt(L·C_oss)) with Q = sqrt(L/C)/r; an RC snubber adds a real pole and a zero."""
    from groundtruth.analysis.power import ringing_rational, ringing_response, snubbed_response
    L, r, c, f = 2e-9, 0.15, 600e-12, np.geomspace(1e6, 1e10, 200)
    num, den = ringing_rational(L, r, c)
    f_ring = 1 / (2 * math.pi * math.sqrt(L * c))
    q = math.sqrt(L / c) / r
    assert np.allclose(np.abs(rational_eval(num, den, f)), ringing_response(f, f_ring, q))
    (p,) = pz_rows(np.roots(den))
    assert p["f0"] == pytest.approx(f_ring) and p["q"] == pytest.approx(q)
    snub = (2.2e-9, 1.5)
    num, den = ringing_rational(L, r, c, snub)
    assert np.allclose(np.abs(rational_eval(num, den, f)), snubbed_response(f, L, r, c, *snub))
    assert len(np.roots(den)) == 3 and len(np.roots(num)) == 1


def test_a_converters_responses(tmp_path):
    """The discrete buck with C_oss and its input connector: the ringing (exact, its pole pair at the ringing
    frequency of the result) and the input transfer at the conducted solves (fitted)."""
    from boardgen import discrete_buck_board
    from conftest import make
    from groundtruth.analysis.detect import detect_converters
    from groundtruth.analysis.simulate import simulate
    from groundtruth.config.settings import SettingsFile
    board, sc, _ = make(tmp_path, discrete_buck_board(), "buck", nets=())
    spec = detect_converters(board, sc.config)[0].spec
    spec.f_sw, spec.i_out, spec.v_in, spec.v_out, spec.c_oss = 300e3, 3.0, 12.0, 3.0, 600e-12
    sc.add_detected_converters([spec], {})
    sc.save()
    (r,) = simulate(board, SettingsFile.load(sc.path).config).converters
    ring, inp = r.responses[:2]                         # then the proposed input filter's curves (fix advisor)
    assert all("filter" in x.name or "input impedance" in x.name for x in r.responses[2:])
    assert ring.exact and "ringing" in ring.name
    (p,) = pz_rows(ring.poles)
    assert p["f0"] == pytest.approx(r.ringing["f"], rel=1e-6) and p["q"] == pytest.approx(r.ringing["q"], rel=1e-6)
    assert "input transfer" in inp.name and not inp.exact and len(inp.f) >= 6
    assert np.all(np.abs(inp.h) <= 1.01)               # a share of the loop current reaches the LISN


def test_a_pdn_rails_poles_are_its_impedance_peaks(tmp_path):
    """The one-cap PDN board (pdn_board: VRM 5 mΩ / 20 nH at J1, C1 1 µF with ESL 0.4 nH, the planes): the fitted
    Z(f) has its poles where the solve found the peaks (the VRM against C1 at ~1 MHz, C1's inductance against the
    plane capacitance at ~300 MHz) within 10 % (the solve's frequency grid: the 1 MHz peak is 7 % off the fitted
    pole), and a zero at C1's series resonance with its mounting, between them."""
    from boardgen import pdn_board
    from conftest import make
    from groundtruth.adapters.file_source import load_board
    from groundtruth.analysis.simulate import simulate
    from groundtruth.config.settings import PdnSpec, SettingsFile
    _, sc, path = make(tmp_path, pdn_board(caps=[("C1", "1u", (22.0, 15.0))], source=True), "pdn", nets=(),
                       cavity_max_nodes=8000)
    sc.set_pdn([PdnSpec("VDD", voltage=3.3, i_step=0.5)])
    sc.save()
    p = simulate(load_board(path), SettingsFile.load(sc.path).config, nets=["VDD"]).pdn[0]
    (resp,) = p.responses
    assert resp.quantity == "impedance" and resp.fit_error_db < 0.5
    poles = [x["f0"] for x in pz_rows(resp.poles) if x["q"]]
    for peak in p.ports[0]["peaks"]:
        assert min(abs(f / peak["f"] - 1) for f in poles) < 0.10, (peak, poles)
    lo, hi = sorted(x["f"] for x in p.ports[0]["peaks"])[:2]
    assert any(lo < z["f0"] < hi and z["q"] for z in pz_rows(resp.zeros))
    assert p.summary()["responses"][0]["quantity"] == "impedance"


def test_the_phase_margin_is_the_smallest_over_every_crossover_and_positive_feedback_is_unstable():
    """Audit round 7, AA-3 / AA-4 (transfer.phase_margin against closed forms, tools/dev/analog_audit.py L4 / L5):
    T = 100·ω1/(s + ω1)·(1 + 49x/(1 + x + y²)) (x = s/(Qωr), y = s/ωr, Q 20, fr 2 MHz) crosses |T| = 1 three times;
    the margin is the smallest of the three (27.4°, at 2.11 MHz), the first alone read 98°. A loop gain of −A(s)
    (positive feedback) is unstable: −180°, flagged; it read 270°."""
    import math
    from groundtruth.analysis.transfer import phase_margin
    f = np.geomspace(1.0, 1e9, 4000)
    s = 2j * math.pi * f
    w1, wr, q = 2 * math.pi * 1e3, 2 * math.pi * 2e6, 20.0
    x, y = s / (q * wr), s / wr
    m = phase_margin(f, 100 * w1 / (s + w1) * (1 + 49 * x / (1 + x + y ** 2)))
    assert len(m["crossings"]) == 3 and m["pm"] == pytest.approx(min(p for _, p in m["crossings"]))
    assert m["pm"] < 30 and m["f_c"] == pytest.approx(2.11e6, rel=0.01) and not m["positive_feedback"]
    a = lambda fr: 1e5 / (1 + 1j * fr / 10)                                # noqa: E731
    lead = np.geomspace(1e-9, 1.0, 60, endpoint=False)                     # near DC up to the sweep's start
    neg = phase_margin(f, -a(f), lead=-a(lead))
    assert neg["positive_feedback"] and neg["pm"] == -180.0
    assert phase_margin(f, a(f), lead=a(lead))["pm"] == pytest.approx(90.0, abs=0.01)     # one pole: 90°
    # two poles below the sweep's start (10 Hz, 100 Hz; sweep from 1 kHz) with |T| still above 1: ∠T is past −180°
    # there; unwrapped from DC its margin is 180 − atan(fc/10) − atan(fc/100), not 360° more
    fs = np.geomspace(1e3, 1e9, 3000)
    t2 = lambda fr: 1e9 / ((1 + 1j * fr / 10) * (1 + 1j * fr / 100))      # noqa: E731
    m2 = phase_margin(fs, t2(fs), lead=t2(np.geomspace(1e-6, 1e3, 60, endpoint=False)))
    fc = m2["f_c"]
    assert m2["pm"] == pytest.approx(180 - math.degrees(math.atan(fc / 10) + math.atan(fc / 100)), abs=0.01)


def test_a_doublet_cancels_only_where_it_cannot_change_the_response():
    """Audit round 7, AA-8 (tools/dev/analog_audit.py T1): a pole pair of Q 2000 at 1 MHz with a zero pair 0.05 %
    above it changes |H| by 7.7 dB near it: it is not cancelled (it was: within 0.1 % of |p|). A real pole and zero
    0.02 % apart (refboard's 451 Hz pair) still are; then |H| moves by less than 0.1 % anywhere."""
    import math
    from groundtruth.analysis.simulate import cancel_doublets
    wp = 2 * math.pi * 1e6
    p = list(np.roots([1, wp / 2000, wp ** 2]))
    z = list(np.roots([1, wp * 1.0005 / 2000, (wp * 1.0005) ** 2]))
    assert cancel_doublets(p, z)[2] == 0
    pr, zr = -2 * math.pi * 451.0, -2 * math.pi * 451.0 * 1.0002
    assert cancel_doublets([pr], [zr])[2] == 1
    w = 2 * math.pi * np.geomspace(1, 1e6, 2000)
    assert np.max(np.abs(np.abs((1j * w - zr) / (1j * w - pr)) * pr / zr - 1)) < 1e-3
