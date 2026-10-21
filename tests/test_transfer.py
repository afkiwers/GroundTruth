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
